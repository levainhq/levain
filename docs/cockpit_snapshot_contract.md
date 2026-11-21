# Cockpit snapshot contract

The cockpit's derive worker computes each panel and stores the result in one sqlite file. The serve, the change
stream, and any other reader answer from that file instead of deriving on the request path.

This document holds the reasons behind the store's design and points at the code that holds the facts. It is not a
spec a reader can be built from: the layout, the encodings and the freshness rules are stated by the code, and a
second statement of them here would drift from it. Read the symbols named below.

Convention for this file: a backticked word is either a code symbol (looked up in `levain/cockpit/snapshots.py`,
`levain/cockpit/snapshot_host.py`, `levain/cockpit/engine.py` or `levain/cockpit/derive.py`, as Class.method where
it is a method) or a repo path, and nothing else is backticked. `tests/test_cockpit_snapshot_contract_doc.py` fails when one of them stops
existing.

## Where the code is

- The store, its writer and its reader: `levain/cockpit/snapshots.py` (`SnapshotWriter`, `SnapshotReader`).
- The file name and the lock file's suffix: `DB_FILENAME` and `LOCK_SUFFIX`. The file sits in the cockpit's data root
  and is a file of its own, so the cockpit's other databases never share its checkpoints or locks. The files sqlite and
  the writer lock keep beside it belong with it.
- The tables and columns: `_SCHEMA` (the statement a writer runs) and `_PANEL_COLS`; a row as Python sees it is
  `PanelSnap`.
- The statuses a panel row can carry, and which ones keep the stored body: `KNOWN_STATUSES`, `KEEP_BODY_STATUSES`,
  `NO_BODY_SHA`.
- The body's encoding on the way in: `encode_body` (canonical bytes, so equal content is equal bytes and an equal
  hash). The body's field list and schema, and how a body becomes a snapshot: `BODY_VERSION`, `BODY_FIELDS` and
  `decode_snap` in `levain/cockpit/snapshot_host.py`, with `body_for` and `encode_snap` for the other direction.
- The host's pseudo-rows, ids that are never a panel's: `PSEUDO_PREFIX`, `DISCOVER_ID` and `ENTITY_ID` in
  `levain/cockpit/snapshot_host.py`.
- The derived freshness fields (as_of, valid_until, stale, the worker's health): `Cockpit._attached_snap`,
  `Cockpit._fresh_of` and `Cockpit._worker_health` in `levain/cockpit/engine.py`, with the thresholds they read
  (`WORKER_LIVE_FACTOR`, `WORKER_DOWN_AFTER_S`, `STALE_CADENCE_FACTOR`).

The reader's code is the contract for the body schema, the pseudo-rows, as_of and stale. A prose copy of them here
would be a second source that the code can leave behind without a failing test.

## Single writer

Exactly one writer (the derive worker), any number of read-only readers. `SnapshotWriter` takes an exclusive,
non-blocking lock on the lock file (`DB_FILENAME` plus `LOCK_SUFFIX`) before it opens the database, and a second writer
that cannot take it raises `WriterLocked`. The lock exists because a restarted worker can overlap with the one it
replaces, so it is a guard and not a convention. A second writer implementation would also have to reproduce the body
encoding of `encode_body` byte for byte, which is another reason the contract names one writer.

## Mode

- The writer puts the file in WAL mode and refuses when sqlite does not answer that it did. Readers never set a journal
  mode. The writer relaxes durability on purpose: a power loss may drop the last publishes, and the next derive
  rewrites them. `JOURNAL_SIZE_LIMIT` keeps the wal file from growing without bound.
- A reader that needs several rows at one moment reads them inside one read transaction, so they come from the same
  commit: `SnapshotReader.read_manifest` is that read.
- Readers open the file read-only (`_uri` builds the URI) and never as immutable: on a live WAL database an immutable
  open reads stale or torn data. A read-only connection to a WAL database still needs write access to the directory,
  because sqlite maps its shared-memory file there.
- A reader keeps one connection per thread and checks it against the file on disk now and then
  (`READER_REVALIDATE_S`): the file can be deleted and recreated under a live reader (a contract-major bump, a manual
  reset), and a connection to the old file would read it forever.

### Change signals

Two cheap signals, different questions.

- "The file changed" is sqlite's per-connection data_version. It moves whenever another connection commits anything,
  heartbeats included, so on a live worker it moves on an idle store. Its values mean nothing across connections, and
  a reopened connection can repeat the old number, so `SnapshotReader.data_version` folds the reopen count in and
  returns an opaque token to be compared for equality only. In a language whose database pool hands out different
  connections, a reader must pin one connection for the probe.
- "A panel changed" is the meta key publish_seq. It does not move on heartbeats, stream rows or a republish of
  unchanged content; `SnapshotWriter.publish` and `SnapshotWriter.drop` are what move it. A reader that sees
  data_version change reads publish_seq, and reads panels only when that moved.

## Contract string

The meta row named contract holds the layout's name and its major version, built from `CONTRACT_NAME`,
`CONTRACT_MAJOR` and `CONTRACT`; `_check_contract` is the comparison. The string carries a major only. On a different
name or major both the writer and the reader raise `ContractMismatch` rather than guess at a layout they do not know.
Additive changes never change the major, so a reader ignores columns and meta keys it does not know.

A writer never migrates a file in place. It reads the contract row with `_preflight`, on a connection that writes
nothing and before it sets any pragma or writes any table, so a refusal leaves the file exactly as it found it. A file
with other tables and no contract row is refused as well. A file with no contract row and no tables of its own is empty
to a reader: no panels, no meta, not a mismatch.

The file is a cache of derivable data. A writer that opens a file of its own major whose tables differ from `_SCHEMA`
drops all of ours and recreates them in one transaction (`SnapshotWriter._rebuild_if_drifted`, which says so on
stderr), rather than failing its publishes.

## Tables, in words

What each table is for; the columns and their types are `_SCHEMA`.

- meta holds the file's own facts: the contract, which worker opened the file and when, the worker's last heartbeat,
  and publish_seq. A worker that opens the file removes its predecessor's heartbeat, so a worker is not live until its
  own first one.
- panel has one row per panel, updated in place on every publish. derived_at moves on every publish, even with the same
  content. write_gen is the read-your-writes marker and never decreases: an error or timeout publish carries the one it
  was derived for, so a reader waiting on a write is answered by that row and its status. A status a reader does not
  know is read as partial (`PanelSnap` and `KNOWN_STATUSES`). An error or timeout keeps the previous body, so the panel
  shows its last good content under that status, and a panel whose first publish is an error has no body at all; its
  status and error say why.
- stream has one row per input-watch stream, the worker's report on how well it sees changes. It is written only when
  it changes (`SnapshotWriter.set_stream`).

## The gen rule

On each publish the writer compares the new body's hash and the new status with the stored ones. If both match, the
generation (gen) is kept and only the timing and bookkeeping columns take new values; if either differs, gen goes up by
one and so does publish_seq; a panel with no stored row starts at 1. The exact comparison is `SnapshotWriter.publish`.

So a changed gen is the one reliable signal that a panel's content or status changed: a reader announces a change to
its own clients exactly when gen moves, and re-deriving an unchanged panel never produces a false change. gen is unique
only within one file lifetime, because a recreated file starts again, so a reader that caches decoded bodies keys them
by panel id, gen and body hash (`SnapshotReader.decoded`).

## Freshness

The store holds timestamps and the reader turns them into freshness; the derivation is in the engine symbols listed
under "Where the code is". The intent that outlives their detail: a stale panel is still served, with its age, and a
dead worker makes panels stale and never errors; a cold reader answers from whatever the file already holds. Only an
absent file or table reads as "the worker has not run": a locked, busy or unreadable database is an error the reader
reports (`SnapshotReadError`).

## Known edges

- The limits of the input fingerprints (what a watched directory or a git repository does and does not show, and how
  long an index lock holds a panel) belong with `Dir`, `GitRefs` and `LOCK_HOLD_MAX_S` in `levain/cockpit/derive.py`,
  which is where they are decided.
- The preflight is a read-only open that writes nothing to the contract, but a read-only open of a WAL database can
  still create sqlite's shared-memory file, and it opens as immutable when no log file exists. That is safe only against
  cooperating writers; the writer lock is taken before the preflight, which excludes the ones this code starts, and a
  foreign process writing the path is outside this contract.
- A foreign file at the snapshot path whose only tables are ours, with a matching meta schema, is adopted, because
  nothing but the worker writes that path.
