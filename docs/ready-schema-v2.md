# READY_SCHEMA v2: the board stores items and derives their state

*Contract v2. It supersedes v1 (flow's `READY_SCHEMA.md`, kept readable until the readers switch). Phill signed the move on 11-12 ("Approve the page's recommendation on all seven and start the build", np-897f0936): a changed derivation rule is a schema version, and his sign is the announcement v1 section 8 asks for. Implementation: `levain/desk/board_state.py`. Conformance: `tests/fixtures/board_state/*.json` (any reader can run them) and `tests/fixtures/ready_schema/ready_v2.json` (the closed sets).*

## 1. What changes from v1

- **No stored lifecycle for board items.** v1 kept a category a writer set and a journal of the moves. v2 stores the *item* (what was asked, in whose words, what closes it, what it waits on as typed links, where it came from) and never where the work stands. State is computed on every read from git, the live seats, the band and the land waiters.
- **New states:** `landing`, `landed_not_closed`, `unknown`, and the three off-board decision states. `ready` and `done` carry over by name, and the six board columns are v1's six categories, except that `needs-you` (v1's hyphen) is `needs_you` here; `off` and `unknown` are the two columns v2 adds.
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
| 6 | landed_not_closed | commits carrying the id are on origin/main and rule 2 did not close it by the item's own `done_kind` (a closing trailer closes only a `merged` item; for `document`, `run` and `band` the file or the np row closes it), and no orphan branch carries more (when one does, the state follows rules 7 and 8 and the audit still lists the landed work, section 7) |
| 7 | blocked | a `depends_on` link is unmet; if one of them is a `gate:` past its `recheck` date (judged against the UTC date of `now`) the item reads `unknown` instead, with the gate as the reason |
| 8 | ready_with_start | an orphan branch carries the id: `ahead` > 0 (section 6) and no live seat holds it |
| 9 | needs_grooming | no `done`, or a tier outside `sonnet`/`opus`/`haiku` |
| 10 | ready | none of the above |

A dependency is met when: an `item:` link's own state is `done`; a `band:` link's np row is resolved; a `link:` ref's tip is an ancestor of origin/main; a `gate:` is never met by evidence (it is removed by an amend, and only lives until its recheck date).

## 4. Unknown, and its causes

Checked in this order; the first that applies is the reason line.

1. the items file is torn (a line that is not JSON, not an object, of an unknown version or op, or missing a required field): every item reads unknown, decided ones included. A clean record that merely names an unknown id is reported in `problems` and nothing else.
2. a source the item's rule needs is unreadable, malformed or not supplied (`evidence`, `main`, `chain`, `branches`, `seats`, `band`, `waiters`, plus `refs` for a `link:` dependency and `documents` for a `document`/`run` item). `git` stands for the four git sources and `notes` for the trailers in `refs/notes/board`. An `unreadable` value that is not an object of source to reason fails closed: `evidence` is unreadable (every item unknown), and any source it names stays unreadable.
3. the evidence is older than `MAX_EVIDENCE_AGE_S` (6 h), carries no fetch time, or was fetched in the future: `fetched_at` more than `FUTURE_SKEW_S` (5 min, ordinary clock skew) ahead of `now` reads `fetched_at is in the future`, never fresh.
4. the item is in a `depends_on` cycle (`cycle a -> b -> a`).
5. a typed link names nothing: `item:` not in the store, `band:` not in the band, `link:` not a ref on origin and not merged.
6. a `document`/`run` item's path was never checked.
7. evidence contradicts itself, neither side winning: closed on main and a live seat names the id · closed on main and an open band row names it · in the chain and an orphan branch (`ahead` > 0) with commits the chain lacks. "Closed on main" is a `Board-Closes` trailer on a `merged` item; for a `document`, `run` or `band` item the closure is its own file or np row, so a trailer beside an absent file is landed work (rule 6), not a conflict.

## 5. The item store record (`items.jsonl`, one JSON object per line, append-only)

Common: `{"v": 1, "op": "add"|"amend"|"decide", "id": "<repo>#<n>", "at": "<UTC ISO8601 Z>", "by": "<who>"}`.

- `add`: `repo`, `title`, `origin` (required); `words`, `done`, `done_kind` (`merged` default, `document`, `band`, `run`), `tier`, `depends_on`.
- `amend`: `fields` (any of `title`, `words`, `done`, `done_kind`, `tier`, `depends_on`, replacing those fields) and `why`.
- `decide`: `decision` (`cut`, `parked`, `out-of-1.0`, `reopen`), `words`, `date` (a real calendar date, `YYYY-MM-DD`), and an `at` that is an ISO-8601 UTC timestamp (`...Z` or `...+00:00`); a decision whose `at` or `date` is not one tears the file. A line too deeply nested for the parser tears it too.
- Links: `{"kind":"item","ref":"levain#163"}` · `{"kind":"band","ref":"np-xxxxxxxx"}` · `{"kind":"link","ref":"queue/1112-31/d10"}` · `{"kind":"gate","words":"...","recheck":"YYYY-MM-DD"}`.
- Never stored: a category, a status, "in flight", "held", a branch or a sha as a claim.

## 6. The evidence object (`"evidence_v": 1`)

`fetched_at`, `unreadable` ({source: reason}), `main` ([{sha, date, items, closes}]), `chain` ([{ref, tip, commits:[{sha, items}]}]), `refs` and `merged` (ref names), `branches` ([{name, tip, date, ahead, items}]), `seats` ([{name, items, since, branch}]), `band` ({open: {np: [ids]}, resolved: {np: by}}), `waiters` ([{branch, items}]), `documents` ({path: bool}), `unattributed` ([{sha, subject}]). A source that is missing is not an empty one: it is unreadable, `unattributed` included. Every field that carries a signal is REQUIRED where its record appears: `items` and `closes` on a main commit, `commits` on a chain link and `items` on each of its commits, `ahead` (a non-negative integer) and `items` on a branch, `items` on a seat and on a waiter. A record that omits one makes its whole source `malformed` (unreadable), never an empty list or a zero. Only `date` (main, branch), `tip` (chain), `since` and `branch` (seat) and `subject` (unattributed) may be absent. `ahead` is the number of the branch's own commits whose work is on neither origin/main nor any chain ref, counted by the reader (a one-to-one patch-id match; the `Board-Item` trailer and subject only for a commit with no patch-id, section 6a), never by tip sha; a branch is an orphan exactly when `ahead` > 0 and no live seat holds it, so a branch that was cherry-picked or re-chained and never deleted (`ahead` 0) is invisible to every rule. `unreadable` is an object of source to reason; `now` is a timezone-aware datetime (a naive one is a TypeError); every other timestamp in the evidence without a zone is read as UTC.

## 6a. The readers and `levain board state --json`

`levain/desk/board_evidence.py` reads the git half of the evidence; `levain/desk/board_cli.py` is the command. It only reads (no fetch, no ref, no note, no index, no file written).

`levain board state --json [--items PATH] [--evidence-extra PATH] [--repo PATH] [--repo-name NAME] [--since ISO] [--now ISO]` prints the board (section 7) plus one additive key, `reader`: `{repo, since, main_ref, main_commits, main_records, chain_refs, chain_commits, branches, refs, merged, unattributed, diverged, matched_by_key}` (`diverged`: the branch names whose local head and `origin/` copy each hold commits the other lacks; `matched_by_key`: how many commits were matched by trailer ids + subject because they have no patch-id at all). `--items` defaults to `$LEVAIN_BOARD_ITEMS`; one of the two is required. Exit **0** the audit is empty, **3** the audit holds an entry, **2** the items file cannot be read (or is a directory, or is not UTF-8), no items path was given, `--now` is not a timestamp, or `--evidence-extra` names a file that cannot be read, is not an object, names a source the command reads itself, carries `evidence_v` other than 1, or an `unreadable`/`documents` that is not an object. A torn items file is NOT exit 2: it is a board on which every item reads unknown (exit 3). Git that cannot be read is NOT exit 2: every source it feeds is named in `unreadable`, every item reads unknown, exit 3. Without `--json` a table is printed with the same exit codes.

| source | read by | how |
|---|---|---|
| `fetched_at` | levain | the newer of `FETCH_HEAD`'s mtime (only when that fetch carried `main`) and the last reflog entry of `origin/main` (the entry's time, not the commit's); absent when neither can be read, so items read `no fetch time` |
| `main` | levain | `git log refs/remotes/origin/main`, the newest `MAX_MAIN_COMMITS` commits (a COUNT, never a date: a commit written with a wrong clock cannot hide the history behind it; if the count is reached while the oldest commit read is still inside the window, `main` and what depends on it read unreadable): a record for every commit with a `Board-Item` / `Board-Closes` trailer (git's own trailer parser, last paragraph only) or a line of the same names in `refs/notes/board`; every record carries BOTH `items` and `closes` (either may be `[]`) |
| `refs`, `merged` | levain | every ref under `origin/` (names without the remote; `queue/1112-31/d10`), and those whose tip is an ancestor of `origin/main` |
| `chain` | levain | each unmerged `origin/queue/*` ref whose tip is not older than `--since`, with the commits beyond main whose work main does not already hold (a one-to-one patch-id match, see `branches`): a ref whose every commit main holds is not live. A commit is `{sha, items}`, `items` the trailer ids (`[]` when it names none) |
| `branches` | levain | `seat/*` and `lane/*` with a tip not older than `--since` (the local head and its `origin/` copy: when one is an ancestor of the other the evidence reads from the newer; when they have diverged BOTH are read, `ahead` is the union and the name is listed in `reader.diverged` for the head), when an id is named by the fixed `i<n>` token of `seat/<stamp>-<N>-i<n>-<slug>` (id `<repo>#<n>`) or by a trailer on a commit of its own. `ahead` is section 6's count: own commits on neither main nor any chain ref by sha, whose work is on neither. A commit's work is on main only by a ONE-TO-ONE patch-id match (`git patch-id --stable`; each main commit accounts for at most one branch commit) with a main commit AFTER the branch's merge-base with main (`git cherry`'s upstream side) that no later main commit reverted (`This reverts commit <sha>`; a revert of the revert re-applies the patch, so it matches through that second commit); the trailer ids + subject key matches ONLY a commit that has no patch-id at all (an empty diff), never one whose content is known A branch with no commits of its own is a record with `ahead` 0 |
| `unattributed` | levain | non-merge commits at or after `--since` on main, in the chain, or on a branch without an `i<n>` token that carry no `Board-Item` / `Board-Closes` and no `Board-Item: none - <why>` |
| `documents` | levain | for `document`/`run` items: the `done` path (relative to `--repo`, `~` expanded) exists and is a non-empty file or directory; a path that cannot be examined for any reason but being absent is left out |
| `seats`, `band`, `waiters` | flow, via `--evidence-extra` | an object holding any of them (and `unreadable`, `documents`, `evidence_v`); it may not name a source levain reads |

`--since` defaults to `BOARD_EPOCH` (2026-11-12T00:00:00Z, the first day a commit could carry a trailer): older commits, branch tips and queue refs are not read for attribution, and a commit older than the epoch less a day is never classified. Every ref is resolved fully qualified (`refs/remotes/origin/main`, `refs/remotes/origin/queue/...`), so a tag or a branch named `origin/main` cannot swap the evidence. A shallow clone or a partial clone (its history is not whole) names every source unreadable, and the reader forbids lazy fetches and every transport (`GIT_NO_LAZY_FETCH=1`, an empty `GIT_ALLOW_PROTOCOL`: read-only means read-only). The whole read has one deadline (`READ_DEADLINE_S`, 300 s; one git call is also capped at 120 s): a stage it stops reads unreadable with the stages that depend on it, never a partial list.

**Trailers (the contract with whatever writes the commits).** `Board-Item:` and `Board-Closes:` count only in the commit's TRAILER block, which git defines as the LAST paragraph of the message. A line above the `Co-Authored-By:` / `Claude-Session:` block (a paragraph of its own) is body text, not a trailer: that commit reads unattributed. Put the `Board-Item:` line in the same final paragraph as the attribution lines (flow's commit-msg hook should refuse a message where `git interpret-trailers --parse` does not return it).

A source git cannot read is named in `unreadable` with the reason and left out of the object: `chain` and `branches` and `unattributed` fail with `main` or `refs`, `unattributed` with `chain` or `branches`.

## 7. The board and the audit

`{"schema": "ready-v2", "as_of", "items": [{id, repo, title, tier, state, column, reason, refs, as_of}], "audit": [{kind, id, why}], "unattributed", "problems"}`. Items are ordered by repo, then item number (compared as digits, so an id of any length sorts). Every unreadable source is reported on the board itself, whether or not an item needed it: one audit `unknown` entry (no id) and one `problems` line, `<source> unreadable: <why>`, so an empty store or an unread `unattributed` scan is never a clean board. The same goes for evidence that cannot be trusted by its age (older than `MAX_EVIDENCE_AGE_S`, no fetch time, or fetched in the future): one audit `unknown` entry (no id) and one `problems` line, `stale_evidence: <why>`, whether or not an item reads it. An item appears in the audit once: an item that reads unknown because its gate expired is listed as `gate_expired`, any other unknown item as `unknown`. Landed work stays audible: an item with commits on main that none closes, which an orphan branch moved to `blocked` or `ready_with_start`, is still listed as `landed_not_closed` (the state keeps rules 6 to 8, the audit does not go quiet). A gate past its recheck date beside a state that does not rest on it (`needs_you`, `landing`, `in_flight`, `landed_not_closed`) leaves the state alone and adds one `gate_expired` entry, in place of any other entry for that item (including `landed_not_closed`: `gate_expired` wins, the state column still says landed); a `done` or decided item is never touched by a gate. An item that reads unknown on an expired gate while commits on main name it (an orphan branch skipped rule 6) carries the landed text too, in its one `gate_expired` entry: `gate "..." past its recheck ...; N commits on main, last <sha>; close it or name what remains`. Where evidence ties (equal dates, several seats, refs or rows), the choice is made by sha, name or ref, never by list position, so two readers print one line for one world. The audit kinds are `unknown`, `landed_not_closed`, `gate_expired`, `unattributed_work` and `unknown_id_in_trailer`; a non-empty audit fails the pace gate's no-ready verdict.

## 8. Versioning

The board carries `"schema": "ready-v2"`; the integer `schema: 2` is the fixture file's (`ready_v2.json`) own version. Additive fields do not bump it. A new state or column, a removed or retyped field, or a changed rule is a version, announced by a needs-phill and never made by one repo alone.

## 9. Known edges

Recorded, not built (none is reachable in real use at board sizes the store holds, or each is the documented choice):

- A `blocked` reason nests the reason of the dependency it waits on, so a wait chain of n items holds O(n squared) characters of reason text.
- A dependent of an `unknown` dependency reads `blocked`, with the dependency's `unknown` reason nested inside: the unknown does not propagate, and the dependent stays out of `ready`.
- `decide: reopen` clears only a ruling (`cut`, `parked`, `out-of-1.0`); it does not undo a `Board-Closes` commit, so reopening a git-closed item is a no-op (revert the closing commit, or add a new item, to reopen the work).
- The readers: a squash merge (one combined diff that no single branch commit matches, the same answer as `git cherry`) leaves the branch's commits counted as `ahead` until the branch ref is deleted; a manual revert whose message carries no `This reverts commit <sha>` line is not recognised as a revert; a commit dated before the window ends the reachability walk (the comparison pool can only grow, and a branch tip or own commit dated more than a day before `--since` is not read at all); the per-worktree `FETCH_HEAD` of a linked worktree is not read (`fetched_at` errs toward stale); a git older than 2.31 may not understand `--path-format` or the trailer placeholders and is not guarded (verify trailers on the oldest supported git before relying on it); the patch-id set grows with how far back the live branches forked (named by `MAX_MAIN_COMMITS`); the board's own birthday is the `BOARD_EPOCH` constant, not a record; a commit older than the epoch less a day on a branch is never classified; `fetched_at` can only err toward stale (a fetch of one other branch is not counted, an `origin/main` reflog entry is a floor); the readers walk every unmerged queue ref newer than the epoch, so a re-chained line that still holds unlanded work stays in `chain` until it is deleted or lands.
- A `document` or `run` item whose file exists reads `done` even with a live seat on it (rule 2 runs first); only a `merged` item's closing trailer contradicts a live seat or an open band row.
