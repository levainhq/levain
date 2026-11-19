# Cockpit snapshot contract

The cockpit's derive worker computes each panel and stores the result in one sqlite file. The serve, the change
stream, and any other reader (Python, Go, Rust) answer from that file instead of deriving on the request path. This
document is the contract; a reader or writer in any language should be implementable from it alone. The Python
implementation is `levain/cockpit/snapshots.py`.

## Where the file lives

`snapshots.db` in the cockpit data root (the directory the cockpit keeps its own state in). It is a file of its own:
not the cockpit's other databases, so their checkpoints and locks never interact with it. Two further files,
`snapshots.db-wal` and `snapshots.db-shm`, appear beside it while it is open; they belong to sqlite and must stay with
it.

Exactly one writer (the derive worker). Any number of readers, each opening the file read-only.

## Mode

- `journal_mode=WAL`, so readers never block the writer and the writer never blocks readers.
- The writer uses `synchronous=NORMAL`: a power loss may drop the last publishes, which the next derive rewrites.
- Every writer call is one transaction. A reader that needs several rows at one moment (a manifest of all panels)
  reads them inside one read transaction, so every row comes from the same commit.
- Readers keep one connection per thread. `PRAGMA data_version` on a reader's own connection changes whenever another
  connection commits, which is a cheap probe for "anything was published"; it says nothing about which panel.

## Contract string

Table `meta`, key `contract`, value `levain-cockpit-snapshots/1`. The text after the last `/` is the major version.

A reader compares the major with the one it speaks. On an unknown major it refuses: it serves no snapshots and says so
in its own health, rather than guessing at a layout it does not know. Minor, additive changes never change the major;
a reader ignores columns and meta keys it does not know. A writer that opens an existing file whose major it does not
speak refuses too and never migrates in place. A file with no contract row yet (the writer has not finished opening)
is treated as empty.

## Tables

### `meta`

`key TEXT PRIMARY KEY, value TEXT`. All values are text.

| key | meaning |
| --- | --- |
| `contract` | the contract string above |
| `worker_pid` | process id of the worker that opened the file |
| `worker_started_at` | when that worker opened it, epoch seconds as a decimal string |
| `heartbeat_at` | the worker's last heartbeat, epoch seconds as a decimal string; absent until the first one |

### `panel`

One row per panel, replaced in place on every publish.

| column | type | meaning |
| --- | --- | --- |
| `id` | TEXT primary key | the panel id |
| `gen` | INTEGER | generation, starts at 1; changes only when the content changes (see the gen rule) |
| `derived_at` | REAL | epoch seconds when this snapshot was last derived (moves on every publish, even with the same content) |
| `started_at` | REAL | epoch seconds when the derive that produced it began |
| `duration_ms` | REAL | how long that derive took, milliseconds |
| `valid_until` | REAL, nullable | epoch seconds after which the content should be re-derived (an expiry, a next-due time, a day rollover); null means no time limit |
| `inputs_fp` | TEXT | fingerprint of the panel's inputs at derive time; opaque to readers |
| `write_gen` | INTEGER | the highest write generation this snapshot reflects (read-your-writes marker); never decreases |
| `status` | TEXT | the provider outcome: ok, empty, partial, error, timeout |
| `error` | TEXT, nullable | the error text when the derive failed, else null |
| `body_sha` | TEXT | lowercase hex sha256 of `body` |
| `body` | BLOB | the panel content, canonical JSON (see below) |

### `stream`

One row per input-watch stream, the worker's report on how well it sees changes.

| column | type | meaning |
| --- | --- | --- |
| `id` | TEXT primary key | the stream id |
| `state` | TEXT | its health, for example ok or degraded |
| `last_event_at` | REAL, nullable | epoch seconds of the last change it saw |
| `detail` | TEXT | a human-readable note, empty when there is nothing to say |

## The gen rule

On each publish the writer compares the new body's sha256 with the stored `body_sha` and the new status with the
stored one:

- Same sha and same status: `gen` is kept. The timing and bookkeeping columns (`derived_at`, `started_at`,
  `duration_ms`, `valid_until`, `inputs_fp`, `error`) take the new values, and `write_gen` becomes the larger of the
  stored and the given value.
- Different sha or different status: `gen` goes up by one.
- No stored row: `gen` is 1.

So a changed `gen` is the one reliable signal that a panel's content or status changed; a reader may cache anything
derived from a body under `(id, gen)` and announce a change to its own clients exactly when `gen` moves. Re-deriving
an unchanged panel never produces a false change.

## Body encoding

`body` is one JSON document as UTF-8 with object keys sorted, no whitespace between tokens (separators `,` and `:`),
and non-ASCII characters written as themselves, not as escapes. Equal values therefore give equal bytes and equal
`body_sha`. A writer must produce exactly this form, since the gen rule depends on byte equality. A reader only needs
a JSON parser. Bodies carry no credentials: the serve adds per-request, per-credential layers on top.

## Freshness fields a reader derives

The store holds timestamps; the reader turns them into freshness. With `now` the reader's clock:

- `as_of` is the panel's `derived_at`.
- `valid_until` is the panel's `valid_until`, passed through.
- Worker health comes from `meta.heartbeat_at`. A reader that owns the policy picks a staleness threshold; a heartbeat
  older than it (or a missing one) means the worker is stale or down. The threshold belongs to the reader, not to the
  file.
- A panel is `stale` when any of these holds: the worker heartbeat is older than the reader's threshold; `valid_until`
  is set and `now >= valid_until`; a newer write generation is pending (the reader has told the worker about a write
  with a `write_gen` greater than the panel's stored one and the worker has not yet published it).
- A stale panel is still served, with its age (`now - as_of`); a dead worker makes panels stale, never errors.

A cold reader that starts with the worker not yet running answers from whatever snapshots the file already holds,
each marked stale by the rules above. A missing file means the worker has never run: no panels, no meta.
