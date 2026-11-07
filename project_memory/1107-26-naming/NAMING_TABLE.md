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

- README.md: heading and section 481-508 (the wakes/replies/ladder section) H -> desk; command table rows
  `levain drop`, `levain drops`, `levain envelope` H -> desk; five P edits (lines 11, 25, 197, 452, 505).
  Left M: `coordinator role`, `coordinator launch`, `coordinator lease`, `coordinator:` status line, source paths, `levain coord`.
- docs/: `cockpit-contract.md` (3 hits) and `contracts.md` (2 paths) are all M (the ready queue's six columns, the board's
  store, source paths); no edit.
- Cockpit (levain/templates/web): 22 visible strings H -> desk (drop box label and placeholders, "Drop to the desk",
  "The desk replied.", error lines, "Desk threads", envelope hints, the board's empty note, the drop sheet title);
  1 P ("answer at the laptop"). Left M: "no coordinator store" (2), the `coordinator` source ids in board_view.js.
- CLI help (argparse): 16 H edits in `levain drop|drops|thread|coord|envelope|team draft|ping` help
  (coordinator/cli.py, team/cli_draft.py, webpush/cli.py). Left M: lease, launch, setting, workspace, session lifecycle.
- Printed and band messages (H): consent/band.py (escalated / not read / answered by the desk, "Desk panel"),
  band_view.py, coordinator/first_reader.py, coordinator/morning.py, cockpit panel titles ("Desk", "Desk morning card"),
  web_server.py and cockpit/coordinator_status|verbs messages "Desk panel".
- P in code: slot/launch.py "without you at the laptop".

## Counts

String-literal hits for "coordinator" in `levain/**/*.py` (docstrings excluded): 467; 161 are bare identifiers
or SQL (M, untouched); of the 306 prose strings, about 45 were classed H and edited; the rest are M (store, lease,
launch, record, workspace, setting, role, verbs) and kept. Docstrings and comments (about 1,300 hits) are not user-facing
and were not touched.

## Left ambiguous (not edited)

- `desk` in the presence system (`webpush/desk.py`, "desk message", "desk session", "desk presence"): already the seat meaning; unchanged.
- `consent/terminal_sign.py:1473` "a desk-namespace signature on an answer that was not made at the desk" and
  `desk_click.py:300` "cannot be signed at the desk yet": "desk" there may mean the seat's own click path, not the laptop; not changed.
- Message strings naming the lease holder as "no coordinator holds the lease", "a coordinator write carries the lease token":
  read as M (the lease is the mechanism) though the holder is the seat.
