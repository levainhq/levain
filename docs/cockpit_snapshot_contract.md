# Cockpit snapshot contract

This document is about the cockpit's snapshot store, one sqlite file, in `levain/cockpit/snapshots.py`.

It holds the reasons behind the store's design, facts about sqlite itself with their qualifiers, and pointers to the
symbols to read. It is not a spec a reader can be built from. A sentence here about what the code does would be a second
statement of it, free to drift, so a pointer names a symbol and at most its role; how the symbol behaves is read in the
symbol.

Convention for this file: a backticked word is a code symbol or a repo path, and nothing else is backticked.
`tests/test_cockpit_snapshot_contract_doc.py` checks that each backticked symbol is defined in the module whose path is
named in the same paragraph or list item (`levain/cockpit/snapshots.py` when none is named), as Class.method where it is
a method, and that each backticked path is an existing file. It checks nothing else.

## Where the code is

- The store, its writer and its reader, in `levain/cockpit/snapshots.py`: `SnapshotWriter`, `SnapshotReader`, and the
  errors `WriterLocked`, `ContractMismatch` and `SnapshotReadError`.
- The file name and the lock file's suffix: `DB_FILENAME` and `LOCK_SUFFIX`. The file is one of its own so that the
  cockpit's other databases do not share its checkpoints or locks.
- The tables and columns: `_SCHEMA` and `_PANEL_COLS`. The stored row's type and its constructor: `PanelSnap` and `_snap`.
- The statuses and the body rule: `KNOWN_STATUSES`, `KEEP_BODY_STATUSES`, `NO_BODY_SHA`.
- The body's encoding: `encode_body`.
- In `levain/cockpit/snapshot_host.py`: `BODY_VERSION`, `BODY_FIELDS`, `decode_snap`, `body_for`, `encode_snap`,
  `PSEUDO_PREFIX`, `DISCOVER_ID` and `ENTITY_ID`.
- In `levain/cockpit/engine.py`: `Cockpit._attached_snap`, `Cockpit._fresh_of`, `Cockpit._worker_health` and
  `_ChangeProbe.change_seq`, with the constants beside them (`HEARTBEAT_CADENCE_S`, `WORKER_LIVE_FACTOR`,
  `WORKER_DOWN_AFTER_S`, `STALE_CADENCE_FACTOR`).
- The contract string: `CONTRACT_NAME`, `CONTRACT_MAJOR`, `CONTRACT`, `_check_contract`, `_preflight` and
  `SnapshotWriter._rebuild_if_drifted`.
- The writer's entry points: `SnapshotWriter.publish`, `SnapshotWriter.drop`, `SnapshotWriter.set_stream` and
  `SnapshotWriter.heartbeat`.

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
- In WAL mode a read transaction sees one commit, so a reader that needs several rows consistent with each other has to
  read them inside one (see `SnapshotReader.read_manifest`).
- sqlite's immutable open assumes the file cannot change, so on a database that is being written it can return stale or
  inconsistent data, so a reader of live content must not use it (see `_uri`).
- Since sqlite 3.22.0 a read-only connection can open a WAL-mode database if its shared-memory and log files already
  exist, or can be created, or the database is opened immutable.
- A sqlite connection follows the file it opened, not the path. If the file is deleted and recreated under a live reader
  (a manual reset, say), a connection kept open would read the old file forever (see `READER_REVALIDATE_S`).
- The write-ahead log's size limit is `JOURNAL_SIZE_LIMIT`; its comment says when it applies.

### Change signals

A reader needs a cheap way to ask whether the file changed and a way to ask whether a panel changed, and the file offers
more than one signal. sqlite's per-connection data_version changes when another connection commits anything, and its
values mean nothing across connections (`SnapshotReader.data_version`). The other signals to read are
`SnapshotWriter.publish` and `SnapshotWriter.drop`.

The engine's own signal to read is `_ChangeProbe.change_seq`, in `levain/cockpit/engine.py`.

## Contract string

The layout is named and carries a major version (`CONTRACT_MAJOR`). The intent is that code which does not know the
layout refuses it rather than guesses at it (`ContractMismatch`, `_check_contract`, `_preflight`); the sqlite caveat
about `_preflight` is under Known edges.

The file is a cache of derivable data, so a mismatch is meant to be survivable (`SnapshotWriter._rebuild_if_drifted`).

## Tables, in words

What each table is for; the columns, their types and what a writer puts in them are `_SCHEMA` and
`SnapshotWriter.publish`.

- meta holds the file's own facts, not a panel's content.
- panel holds a panel's latest result and its bookkeeping. write_gen is the read-your-writes marker.
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

- The limits of the input fingerprints are written next to the code, in `levain/cockpit/derive.py`: the docstrings of
  `Dir` and `GitRefs`, and the comments on `LOCK_HOLD_MAX_S`, `DERIVE_TIMEOUT_S`, `DIR_ENTRIES_MAX` and `GIT_REFS_MAX`.
- The preflight (`_preflight`) is meant to read the contract without writing. sqlite can create its shared-memory file
  even on a read-only open of a WAL-mode database, when none exists yet, and an immutable open is safe only if nothing
  writes the file meanwhile. A lock file binds only the processes that take it, so a foreign process writing the
  snapshot path is outside this contract.
- The design assumes nothing but the worker writes the snapshot path; see `_preflight`, `_check_contract` and
  `SnapshotWriter._rebuild_if_drifted`.
