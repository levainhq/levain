# NAMING_TABLE: "desk" and "coordinator" in user-facing text (Phill 11-07)

Rule: "desk" is the human-facing word for the seat where the operator meets Levain and holds the criteria.
"coordinator" stays as the mechanism's name (store, lease, launch, record, workspace, role, queue directory, the
`coord` verbs, `--by coordinator`, role ids). "at the desk" meaning physically present reads "at the laptop".
No identifier, file, JSON key or CLI flag was renamed.

## Classes

| Class | Reads | Examples |
|---|---|---|
| H (human seat) | desk | "Drop words for the desk", "the desk's door", "The desk escalated it to you", "Desk panel", "Desk threads", "words for the desk", the README section "The desk: wakes, replies and the ladder" |
| M (mechanism) | coordinator | coordinator store, lease, launch, record, workspace, queue directory, agent setting, role, `levain coord`, `--by coordinator`, "Codex coordinator removed", the `coordinator:` status line, paths `levain/coordinator/...` |
| P (physical presence) | laptop | "answer at the laptop", "from the laptop or your phone", "without you at the laptop" |

## What changed (H and P), by surface

- README.md: heading and section "The desk: wakes, replies and the ladder" (the wakes/replies/ladder section) H -> desk; command table rows
  `levain drop`, `levain drops`, `levain envelope` H -> desk; P edits ("at the desk" -> "at the laptop").
  Left M: `coordinator role`, `coordinator launch`, `coordinator lease`, `coordinator:` status line, source paths, `levain coord`.
- docs/: `cockpit-contract.md` (3 hits) and `contracts.md` (2 paths) are all M (the ready queue's six columns, the board's
  store, source paths); no edit.
- Cockpit (levain/templates/web): visible strings H -> desk (drop box label and placeholders, "Drop to the desk",
  "The desk replied.", error lines, "Desk threads", envelope hints, the board's empty note, the drop sheet title);
  1 P ("answer at the laptop"). Left M: "no coordinator store" (2), the `coordinator` source ids in board_view.js.
- CLI help (argparse): every H phrase in the help of `levain drop|drops|coord|envelope|team draft|ping`
  (coordinator/cli.py, team/cli_draft.py, webpush/cli.py). Left M: lease, launch, setting, workspace, session lifecycle.
- Printed and band messages (H): consent/band.py (escalated / not read / answered by the desk, "Desk panel"),
  band_view.py, coordinator/first_reader.py, coordinator/morning.py, cockpit panel titles ("Desk", "Desk morning card"),
  web_server.py and cockpit/coordinator_status|verbs messages "Desk panel".
- P in code: slot/launch.py "without you at the laptop".

## Counts

Not stored (a number here goes stale at the next edit). Recount with:
`git grep -ciw coordinator -- 'levain/*.py'` for every hit, and `git diff e8c1169f..HEAD --stat` for what the rename touched.
Docstrings and comments are mostly not user-facing and were left; the ones the rename did touch are a few module
docstrings and comments in cockpit/coordinator_status.py, cockpit/coordinator_verbs.py, coordinator/cli.py,
web_server.py and the matching tests, where they quoted a renamed string.

## Left ambiguous (not edited)

- `desk` in the presence code (`webpush/desk.py`, "desk message", "desk presence"): identifiers and docstrings, unchanged. Its user
  text ("a message you typed to a desk session") now reads "a message you typed into a session at the laptop".
- `consent/terminal_sign.py` "a desk-namespace signature on an answer that was not made at the desk" and
  `desk_click.py` "cannot be signed at the desk yet": "desk" there may mean the seat's own click path, not the laptop; not changed.
- The one line that quotes the recorded answerer keeps "coordinator": `consent/band.py` `know_line` ("the coordinator, not
  the operator, answered"), since it echoes the record value `by: coordinator`; summaries elsewhere (first_reader, team draft) say "the desk". The coordinator's handoff help
  ("Read the handoffs coordinators left", "the next coordinator") stays too: a handoff is a record.
- In the README, "the desk" is the session holding the coordinator role, the one the cockpit labels Desk (README
  "The desk: wakes, replies and the ladder"). Where text means "the session you typed into", it says "a session at the laptop".
- Message strings naming the lease holder as "no coordinator holds the lease", "a coordinator write carries the lease token":
  read as M (the lease is the mechanism) though the holder is the seat.
