# Cockpit snapshot contract

The cockpit's derive worker computes each panel and stores the result in one sqlite file. The serve, the change
stream and any other reader answer from that file instead of deriving on the request path.

This document holds the reasons behind the store's design and points at the code that holds the facts. It is not a
spec a reader can be built from. A sentence here about what the code does would be a second statement of it, free to
drift, so the sentences below are reasons, sqlite's own behaviour, or names of the symbols to read.

Convention for this file: a backticked word is a code symbol or a repo path, and nothing else is backticked. A symbol is
looked up in the module whose path is named in the same paragraph or list item (`levain/cockpit/snapshots.py` when none
is named), as Class.method where it is a method. `tests/test_cockpit_snapshot_contract_doc.py` fails when one of them
stops being defined there.

## Where the code is

- The store, its writer and its reader, in `levain/cockpit/snapshots.py`: `SnapshotWriter`, `SnapshotReader`, and the
  errors they raise, `WriterLocked`, `ContractMismatch` and `SnapshotReadError`.
- The file name and the lock file's suffix: `DB_FILENAME` and `LOCK_SUFFIX`. The file is one of its own so that the
  cockpit's other databases do not share its checkpoints or locks.
- The tables and columns: `_SCHEMA` and `_PANEL_COLS`. A stored row becomes a `PanelSnap` in `_snap`, which is also where
  a status the code does not know is dealt with.
- The statuses and the body rule that goes with them: `KNOWN_STATUSES`, `KEEP_BODY_STATUSES`, `NO_BODY_SHA`.
- The body's encoding on the way in: `encode_body`.
- The body's field list and schema, and how a body becomes a snapshot and back, in `levain/cockpit/snapshot_host.py`:
  `BODY_VERSION`, `BODY_FIELDS`, `decode_snap`, `body_for` and `encode_snap`. The host's pseudo-rows: `PSEUDO_PREFIX`,
  `DISCOVER_ID` and `ENTITY_ID`.
- The freshness fields a reader derives (as_of, valid_until, stale, the worker's health), in `levain/cockpit/engine.py`:
  `Cockpit._attached_snap`, `Cockpit._fresh_of` and `Cockpit._worker_health`, with the constants beside them
  (`HEARTBEAT_CADENCE_S`, `WORKER_LIVE_FACTOR`, `WORKER_DOWN_AFTER_S`, `STALE_CADENCE_FACTOR`); what the engine announces
  a change on is `_ChangeProbe.change_seq`.
- The contract string: `CONTRACT_NAME`, `CONTRACT_MAJOR`, `CONTRACT` and the comparison `_check_contract`; what the
  writer reads before it changes anything is `_preflight`; what it does with tables that differ from the schema is
  `SnapshotWriter._rebuild_if_drifted`.
- What moves a panel's gen and the file's change counter: `SnapshotWriter.publish` and `SnapshotWriter.drop`. What the
  writer does for a stream row and a heartbeat: `SnapshotWriter.set_stream` and `SnapshotWriter.heartbeat`.

The code is the contract for all of these. A prose copy of them here would be a second source that the code can leave
behind without a failing test.

## Single writer

Exactly one writer (the derive worker) and any number of read-only readers. The design guards the file against a second
writer with a lock (`SnapshotWriter`, `LOCK_SUFFIX`, `WriterLocked`) because a restarted worker can overlap the one it
replaces, so the single writer is a guard and not a convention. The design has one encoder (`encode_body`) for a related
reason: a body hash that is compared across publishes means something only if one function made every body.

## Mode

- The file is meant to be a WAL-mode database so that one writer and many readers do not block each other. It is a
  cache of derivable data, so durability is not a goal of the file.
- In WAL mode a read transaction sees one commit, so a reader that needs several rows consistent with each other reads
  them inside one (`SnapshotReader.read_manifest`).
- sqlite's immutable open assumes the file cannot change, so on a database that is being written it can return stale or
  inconsistent data, so a reader of live content must not use it (the reader's URI is `_uri`).
- Since sqlite 3.22.0 a read-only connection can open a WAL-mode database if its shared-memory and log files already
  exist, or can be created, or the database is opened immutable.
- A sqlite connection follows the file it opened, not the path. If the file is deleted and recreated under a live reader
  (a manual reset, say), a connection kept open would read the old file forever; `READER_REVALIDATE_S` is the reader's
  answer to that.
- The write-ahead log's size limit is `JOURNAL_SIZE_LIMIT`; its comment says when it applies.

### Change signals

Two questions, two signals. Whether the file changed: sqlite's per-connection data_version changes when another
connection commits anything, and its values mean nothing across connections (`SnapshotReader.data_version`). Whether a
panel changed: the writer's change counter in the file (`SnapshotWriter.publish`, `SnapshotWriter.drop`). They answer
different questions.

## Contract string

The layout is named and carries a major version in the file. The intent is that code which does not know the layout
refuses it rather than guesses at it (`ContractMismatch`, `_check_contract`), and that a refusal costs the file nothing
(`_preflight`; the sqlite caveat is under Known edges). The policy is that an additive change, a column or a meta key,
does not change the major and a change that is not additive does.

The file is a cache of derivable data, so the design prefers recreating it to failing a publish;
`SnapshotWriter._rebuild_if_drifted` is where that choice is made.

## Tables, in words

What each table is for; the columns, their types and what a writer puts in them are `_SCHEMA` and
`SnapshotWriter.publish`.

- meta holds the file's own facts, not a panel's content.
- panel holds a panel's latest result and its bookkeeping. write_gen is the read-your-writes marker: it is how a reader
  that has just made a write learns that the panel was derived for it.
- stream holds the worker's report on how well it sees changes (`SnapshotWriter.set_stream`).

## The gen rule

The generation (gen) exists so that re-deriving an unchanged panel does not look like a change to a reader. What counts
as a change, what a panel with no stored row starts at, and what a dropped panel does to the counters are
`SnapshotWriter.publish` and `SnapshotWriter.drop`. What a reader caches decoded bodies on is
`SnapshotReader.decoded`.

Do not treat gen as the whole change signal: what the engine announces on and what it caches on are
`_ChangeProbe.change_seq` and `Cockpit._attached_snap`, in `levain/cockpit/engine.py`.

## Freshness

A stale panel is better served with its age than withheld, and a reader that starts before the worker has should answer
from what the file already holds rather than wait for it. An unreadable store is not the same thing as a worker that has
not run, and the two are meant to be told apart (`SnapshotReadError`).

What the code does in each case is `Cockpit._attached_snap`, `Cockpit._fresh_of` and `Cockpit._worker_health`, in
`levain/cockpit/engine.py`.

## Known edges

- The limits of the input fingerprints are written next to the code, in `levain/cockpit/derive.py`: what a watched
  directory and a git repository do and do not show are the docstrings of `Dir` and `GitRefs`, how long an index lock
  holds a panel is `LOCK_HOLD_MAX_S`, and what a derive that never returns costs is `DERIVE_TIMEOUT_S`. The size limits
  are `DIR_ENTRIES_MAX` and `GIT_REFS_MAX`.
- The preflight (`_preflight`) is meant to read the contract without writing. sqlite can create its shared-memory file
  even on a read-only open of a WAL-mode database, when none exists yet, and an immutable open is safe only if nothing
  writes the file meanwhile. A lock file binds only the processes that take it, so a foreign process writing the
  snapshot path is outside this contract.
- The design assumes nothing but the worker writes the snapshot path; what the code does with a foreign file there is
  `_preflight`, `_check_contract` and `SnapshotWriter._rebuild_if_drifted`.
