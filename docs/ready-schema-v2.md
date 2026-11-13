# READY_SCHEMA v2: the board stores items and derives their state

*Contract v2. It supersedes v1 (flow's `READY_SCHEMA.md`, kept readable until the readers switch). Phill signed the move on 11-12 ("Approve the page's recommendation on all seven and start the build", np-897f0936): a changed derivation rule is a schema version, and his sign is the announcement v1 section 8 asks for. Implementation: `levain/desk/board_state.py`. Conformance: `tests/fixtures/board_state/*.json` (any reader can run them) and `tests/fixtures/ready_schema/ready_v2.json` (the closed sets).*

## 1. What changes from v1

- **No stored lifecycle for board items.** v1 kept a category a writer set and a journal of the moves. v2 stores the *item* (what was asked, in whose words, what closes it, what it waits on as typed links, where it came from) and never where the work stands. State is computed on every read from git, the live seats, the band and the land waiters.
- **New states:** `landing`, `landed_not_closed`, `unknown`, and the three off-board decision states. `needs_you`, `ready`, `done` and the column names carry over.
- **Unknown is a state, with a reason.** Missing, stale or contradictory evidence never falls back to a default column.
- **A decision replaces a move.** `cut`, `parked` and `out-of-1.0` are the only lifecycle-like facts that are stored, because a ruling cannot be derived from git; each carries its words and a date, and `reopen` is a new record.

## 2. States and columns

The set is closed. `unknown` sits outside the six board columns on purpose: the board shows it in a band of its own and the gates fail closed on it.

| state | column | meaning |
|---|---|---|
| `cut` | `off` | a decision record says cut |
| `parked` | `off` | a decision record says parked |
| `out_of_1_0` | `off` | a decision record says out of 1.0 |
| `done` | `done` | the item's closing evidence is met |
| `needs_you` | `needs_you` | an open band row names the item |
| `landing` | `waiting` | its commits are in the landing chain, or a land waiter runs on its branch |
| `in_flight` | `running` | a live seat or lane names the item or holds a branch carrying it |
| `landed_not_closed` | `backlog` | commits carrying the id are on origin/main, none closes it: the audit fails |
| `blocked` | `backlog` | a dependency is unmet; the reason reads through to the dependency's own state |
| `ready_with_start` | `ready` | an orphan branch carries the id: cleared to start, with a starting point |
| `needs_grooming` | `backlog` | no DONE line or no valid tier |
| `ready` | `ready` | none of the above |
| `unknown` | `unknown` | the evidence cannot say, or contradicts itself (section 4) |

## 3. Precedence

The first rule that fires wins. The checks in section 4 run before this table, so a state is never derived from evidence that cannot be trusted. A decided item skips those checks (a ruling is stored, not derived) except a torn items file.

| # | rule | fires when |
|---|---|---|
| 1 | decision | a decision record is in force |
| 2 | done | `merged`: a commit on origin/main carries `Board-Closes: <id>` · `document`/`run`: the path in `done` exists and is non-empty · `band`: the np row in `done` is resolved |
| 3 | needs_you | an open band row names the id (or a `band`-kind item's np row is still open) |
| 4 | landing | commits carrying the id are in a live `queue/*` ref beyond origin/main, or a land waiter names the id |
| 5 | in_flight | a live seat names the id or holds a branch carrying it |
| 6 | landed_not_closed | commits carrying the id are on origin/main, none closes it, and no orphan branch carries more |
| 7 | blocked | a `depends_on` link is unmet |
| 8 | ready_with_start | a branch carries the id, with commits of its own, no live seat holds it, and its tip is on neither main nor the chain |
| 9 | needs_grooming | no `done`, or a tier outside `sonnet`/`opus`/`haiku` |
| 10 | ready | none of the above |

A dependency is met when: an `item:` link's own state is `done`; a `band:` link's np row is resolved; a `link:` ref's tip is an ancestor of origin/main; a `gate:` is never met by evidence (it is removed by an amend, and only lives until its recheck date).

## 4. Unknown, and its causes

Checked in this order; the first that applies is the reason line.

1. the items file is torn (a line that is not JSON, not an object, of an unknown version or op, or missing a required field): every item reads unknown, decided ones included. A clean record that merely names an unknown id is reported in `problems` and nothing else.
2. a source the item's rule needs is unreadable, malformed or not supplied (`evidence`, `main`, `chain`, `branches`, `seats`, `band`, `waiters`, plus `refs` for a `link:` dependency and `documents` for a `document`/`run` item). `git` stands for the four git sources and `notes` for the trailers in `refs/notes/board`.
3. the evidence is older than `MAX_EVIDENCE_AGE_S` (6 h) or carries no fetch time.
4. the item is in a `depends_on` cycle (`cycle a -> b -> a`).
5. a typed link names nothing: `item:` not in the store, `band:` not in the band, `link:` not a ref on origin and not merged.
6. a `document`/`run` item's path was never checked.
7. a `gate:` is past its `recheck` date.
8. evidence contradicts itself, neither side winning: closed on main and a live seat names the id · closed on main and an open band row names it · in the chain and an orphan branch with commits the chain lacks.

## 5. The item store record (`items.jsonl`, one JSON object per line, append-only)

Common: `{"v": 1, "op": "add"|"amend"|"decide", "id": "<repo>#<n>", "at": "<UTC ISO8601 Z>", "by": "<who>"}`.

- `add`: `repo`, `title`, `origin` (required); `words`, `done`, `done_kind` (`merged` default, `document`, `band`, `run`), `tier`, `depends_on`.
- `amend`: `fields` (any of `title`, `words`, `done`, `done_kind`, `tier`, `depends_on`, replacing those fields) and `why`.
- `decide`: `decision` (`cut`, `parked`, `out-of-1.0`, `reopen`), `words`, `date`.
- Links: `{"kind":"item","ref":"levain#163"}` · `{"kind":"band","ref":"np-xxxxxxxx"}` · `{"kind":"link","ref":"queue/1112-31/d10"}` · `{"kind":"gate","words":"...","recheck":"YYYY-MM-DD"}`.
- Never stored: a category, a status, "in flight", "held", a branch or a sha as a claim.

## 6. The evidence object (`"evidence_v": 1`)

`fetched_at`, `unreadable` ({source: reason}), `main` ([{sha, date, items, closes}]), `chain` ([{ref, tip, commits:[{sha, items}]}]), `refs` and `merged` (ref names), `branches` ([{name, tip, date, ahead, items}]), `seats` ([{name, items, since, branch}]), `band` ({open: {np: [ids]}, resolved: {np: by}}), `waiters` ([{branch, items}]), `documents` ({path: bool}), `unattributed` ([{sha, subject}]). A source that is missing is not an empty one: it is unreadable.

## 7. The board and the audit

`{"schema": "ready-v2", "as_of", "items": [{id, repo, title, tier, state, column, reason, refs, as_of}], "audit": [{kind, id, why}], "unattributed", "problems"}`. Items are ordered by repo, then item number. The audit kinds are `unknown`, `landed_not_closed`, `gate_expired`, `unattributed_work` and `unknown_id_in_trailer`; a non-empty audit fails the pace gate's no-ready verdict.

## 8. Versioning

`schema: 2` for the board. Additive fields do not bump it. A new state or column, a removed or retyped field, or a changed rule is a version, announced by a needs-phill and never made by one repo alone.
