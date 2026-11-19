# Cockpit snapshot contract

The cockpit's derive worker computes each panel and stores the result in one sqlite file. The serve, the change
stream, and any other reader (Python, Go, Rust) answer from that file instead of deriving on the request path. This
document is the contract; a reader in any language should be implementable from it alone. The writer is the Python
derive worker (`levain/cockpit/snapshots.py`); a second writer implementation would have to reproduce the body
encoding below byte for byte, which is why this contract names exactly one writer.

## Where the file lives

`snapshots.db` in the cockpit data root (the directory the cockpit keeps its own state in). It is a file of its own:
not the cockpit's other databases, so their checkpoints and locks never interact with it. Further files appear beside
it and belong to sqlite or to the writer: `snapshots.db-wal` and `snapshots.db-shm` (sqlite), and `snapshots.db.lock`
(the writer lock, see below). They must stay with it.

Exactly one writer (the derive worker). Any number of readers, each opening the file read-only.

## Single writer

The writer takes an exclusive, non-blocking `flock` on `snapshots.db.lock` (created if absent) before it opens the
database and holds it until it closes. A second writer process that cannot take the lock refuses to start
(`WriterLocked`). A restarted worker can overlap with the one it replaces, so this is the guard, not a convention.
The lock file's contents are not used.

## Mode

- The writer sets `journal_mode=WAL` when it opens the file and checks that the pragma answered `wal` (it refuses
  otherwise). Readers never set a journal mode.
- The writer sets `synchronous=NORMAL`: a power loss may drop the last publishes, which the next derive rewrites. It
  also sets `journal_size_limit` so the wal file shrinks back after a checkpoint. A busy timeout of a few seconds on
  every connection, writer and readers.
- Every writer call is one transaction. A reader that needs several rows at one moment (a manifest of all panels with
  the meta rows beside them) reads them inside one read transaction, so everything comes from the same commit.
- Readers open the file read-only with `mode=ro` in the sqlite URI, never `immutable=1` (immutable on a live WAL
  database reads stale or torn data). A read-only connection to a WAL database still needs write access to the
  directory, because sqlite creates and maps `snapshots.db-shm` there; sqlite 3.22 or newer. Meeting no `-wal` or
  `-shm` file at open is normal. URI-escape the path (`%`, `#`, `?`, spaces) when building the URI.
- A reader keeps one connection per thread (or per pinned handle) and revalidates it against the file on disk now and
  then: when the file at the path has a different device or inode than the open connection's, it reopens. The file can
  be deleted and recreated under a live reader (a contract-major bump, a manual reset).

### Change signals

Two cheap signals, different questions:

- `PRAGMA data_version` answers "the file changed". It changes whenever another connection commits anything,
  heartbeats and stream rows included, so on a live worker it moves about once per poll. It is per connection: its
  values mean nothing across connections, and the first value on a new connection says nothing (a new connection on a
  replaced file can repeat the old connection's number, so a reader that reopens must treat the reopen itself as a
  change; the Python reader's `data_version()` folds its reopen count in and is an opaque token, compared for equality
  only). In a language whose
  database pool hands out different connections (Go `database/sql`), pin one connection for the probe.
- meta `publish_seq` answers "a panel changed". It is an integer (as decimal text) that goes up by one in the same
  transaction as every `gen` change (including a panel's first publish), so it does not move on heartbeats, stream
  rows, or a republish of unchanged content. A reader that polls `data_version` and sees a change then reads
  `publish_seq`, and reads panels only when that moved.

## Contract string

Table `meta`, key `contract`, value `levain-cockpit-snapshots/1`: the name, a `/`, and the major version as a decimal
integer. The string carries a major only; there is no minor in it. The reader compares the name and the major with
what it speaks. On an unknown major, or a different name, it refuses: it serves no snapshots and says so in its own
health, rather than guessing at a layout it does not know. Minor, additive changes never change the major; a reader
ignores columns and meta keys it does not know. A writer that opens an existing file whose contract it does not speak
refuses too and never migrates in place: it reads the contract row before it sets any pragma or writes any table, so a
refusal leaves the file exactly as it found it. A file with other tables and no contract row is refused as well.

A file with no contract row (the writer has not finished opening, or it is some other database) is empty to a
reader: no panels, no meta, not a mismatch.

## Tables

The exact DDL (a writer creates these with `IF NOT EXISTS`; a reader never creates anything):

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS panel (
    id TEXT PRIMARY KEY,
    gen INTEGER NOT NULL,
    derived_at REAL NOT NULL,
    started_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    valid_until REAL,
    inputs_fp TEXT NOT NULL,
    write_gen INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    body_sha TEXT NOT NULL,
    body BLOB
);
CREATE TABLE IF NOT EXISTS stream (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    last_event_at REAL,
    detail TEXT NOT NULL DEFAULT ''
);
```

All times are epoch seconds as REAL (meta holds them as decimal text; parse as a 64-bit float, which may print in
exponent form).

### `meta`

| key | meaning |
| --- | --- |
| `contract` | the contract string above |
| `worker_pid` | process id of the worker that opened the file |
| `worker_started_at` | when that worker opened it, epoch seconds |
| `heartbeat_at` | the worker's last heartbeat, epoch seconds; absent until the first one (a worker that opens the file removes its predecessor's, so it is not live until its own first heartbeat) |
| `publish_seq` | integer, +1 with every `gen` change (see Change signals); `0` on a new file |

### `panel`

One row per panel, updated in place on every publish.

| column | meaning |
| --- | --- |
| `id` | the panel id |
| `gen` | generation, starts at 1; changes only when the content or status changes (see the gen rule) |
| `derived_at` | when this snapshot was last derived (moves on every publish, even with the same content) |
| `started_at` | when the derive that produced it began |
| `duration_ms` | how long that derive took, milliseconds |
| `valid_until` | null means no time limit; else the time after which the content should be re-derived |
| `inputs_fp` | fingerprint of the panel's inputs at derive time; opaque to readers |
| `write_gen` | the highest write generation this snapshot reflects (read-your-writes marker); never decreases. An `error` or `timeout` publish carries none: it reflects no write |
| `status` | the outcome: `ok`, `empty`, `partial`, `error`, `timeout`; a reader treats an unknown value as `partial` |
| `error` | the error text when the derive failed or timed out, else null |
| `body_sha` | lowercase hex sha256 of `body`; the empty string when `body` is NULL |
| `body` | the panel content, canonical JSON bytes stored as a BLOB (see below); NULL when no good body was ever stored |

A `timeout` or `error` publish keeps the previous `body`, whatever body the deriving code had: the panel shows its last
good content under that status. A panel whose first publish is an `error` or `timeout` has a NULL `body` (and an empty
`body_sha`): a reader treats it as a panel with no content, and `status` and `error` say why. The first `ok`, `empty` or
`partial` publish after it stores the body and moves `gen`.

### `stream`

One row per input-watch stream, the worker's report on how well it sees changes. Written only when it changes.

| column | meaning |
| --- | --- |
| `id` | the stream id |
| `state` | its health: `ok` or `degraded` |
| `last_event_at` | epoch seconds of the last input change it saw; null while it has seen none |
| `detail` | a human-readable note naming what degraded it (a panel and input, a stale lock path, a timed-out derive, a write error); empty when there is nothing to say |

## The gen rule

On each publish the writer compares the new body's sha256 with the stored `body_sha` and the new status with the
stored one:

- Same sha and same status: `gen` is kept. The timing and bookkeeping columns (`derived_at`, `started_at`,
  `duration_ms`, `valid_until`, `inputs_fp`, `error`) take the new values, `write_gen` becomes the larger of the stored
  and the given value, and the body is not rewritten.
- Different sha or different status: `gen` goes up by one (and `publish_seq` with it).
- No stored row: `gen` is 1.

So a changed `gen` is the one reliable signal that a panel's content or status changed; a reader announces a change
to its own clients exactly when `gen` moves. Re-deriving an unchanged panel never produces a false change. A change
of `error` text alone, with the same status, does not move `gen`. `gen` is unique only within one file lifetime
(a recreated file starts again at 1): a reader that caches decoded bodies keys them `(id, gen, body_sha)`.

## Body encoding

`body` is one JSON document, as ASCII bytes: object keys sorted by code point, no whitespace between tokens (separators
`,` and `:`), every non-ASCII character written as a `\uXXXX` escape (a character above U+FFFF as a surrogate pair; a
lone surrogate is escaped the same way and stays valid JSON), and no `NaN` or `Infinity` (a derive that produces one
fails with status `error`). Equal values give equal bytes and equal `body_sha`. Numbers print as the Python writer
prints them (integers without a point, floats by Python's shortest round-trip form), which is another reason a second
writer is out of scope. A reader only needs a JSON parser and should accept the body as BLOB or TEXT (and NULL, see above). Bodies carry no
credentials: the serve adds per-request, per-credential layers on top.

## Freshness fields a reader derives

The store holds timestamps; the reader turns them into freshness. With `now` the reader's clock:

- `as_of` is the panel's `derived_at`.
- `valid_until` is the panel's `valid_until`, passed through.
- Worker health comes from `meta.heartbeat_at`. The staleness threshold is the reader's own policy (a few poll
  intervals of the worker; the serve owns the number); a heartbeat older than it, or a missing one, means the worker is
  stale or down.
- A panel is `stale` when any of these holds: the worker heartbeat is older than the reader's threshold;
  `valid_until` is set and `now >= valid_until`; `now - derived_at` exceeds twice the panel's refresh cadence (the
  cadence is the reader's policy, the file holds none); a newer write generation is pending (the reader has told the
  worker about a write with a `write_gen` greater than the panel's stored one and the worker has not yet published
  it). The pending-write clause lives in the reader process, not in the file; a reader in another process applies the
  first three.
- A stale panel is still served, with its age (`now - as_of`); a dead worker makes panels stale, never errors.

A cold reader that starts with the worker not yet running answers from whatever snapshots the file already holds,
each marked stale by the rules above. A missing file means the worker has never run: no panels, no meta. Only an
absent file or table reads as "not there": a locked, busy or unreadable database is an error the reader reports, never
"the worker has not run".

## Limits and known edges

- Dir input fingerprints see the entries of the directory itself (name, mtime, size), not edits inside nested
  files or directories, and not a rewritten symlink target.
- GitRefs input fingerprints see HEAD, packed-refs, the index, and the loose refs under the common dir, and a held
  `index.lock`. They do not see working-tree edits, per-worktree refs beyond HEAD, or the reftable ref backend.
- A provider that never returns pins its derive thread for the worker's life; the panel reads `timeout` and the
  `inputs` stream reads degraded, but the thread is not reclaimed.
- An `index.lock` holds a GitRefs panel only while it is younger than the worker's hold limit; past it the panel derives
  anyway and the `inputs` stream names the lock path.
- Heartbeats commit once per worker poll, so `data_version` moves on an idle store; use `publish_seq` for "a panel
  changed".
