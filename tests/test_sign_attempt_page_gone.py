"""The cockpit keeps asking about an unsettled answer after its request leaves the listing (L3 r1 codex MED on
dashboard_core.js): headless Chrome (its own profile, tests/_page_drive_run.py through ``_drive``) on a real cockpit
server, over the rig of test_sign_attempt_page.

The class: an answer from the "Needs you" card whose reply was lost reads ``pending`` (it is still verifying); the
request then leaves the open listing (it signed, or its file was removed) and a facet for it is drawn fresh: the
Decisions panel's row for the question, opened ("open"). (The strip's own card is kept by id, never redrawn, and asks
through its own settle.) That fresh facet used to drop the unsettled attempt and say only "no longer open", which also
left the strip card frozen with nothing asking. Now it asks the attempt on every board read until the cockpit says
signed or refused; and the attempt still reads after its request file is gone (the record keeps its namespace)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import FACET_NOTE, OPTION_B, QRID, STRIP_FACET, TOK, _Rig, needs_ssh
from tests.test_cockpit_sign_card_r5 import AFTER
from tests.test_sign_attempt_page import CARD_SENT_THEN_LOST, _hold_verify
from tests.test_sign_sheet_page import DESKTOP

HISTORY = "(document.querySelector('#ck-needs .needs-history').click(), 1)"   # "all decisions": the Decisions panel
ROW = "document.querySelector('[data-question-row]')"
OPEN_ROW = f"({ROW}.querySelector('.consent-details').click(), 1)"
ROW_NOTE = f"(({ROW} || document).querySelector('.sign-note') || {{}}).textContent"
ROW_OPTIONS = f"(({ROW} || document).querySelectorAll('.sign-option')).length"
# the row's note and how many options it drew, read together once the note says `re` (a board read repaints the row)
ROW_SAYS = "({re}.test({note}) && JSON.stringify([{note}, {opts}]))"
READS_EVERY_1500 = "window.__reads = setInterval(() => window.LevainCockpitTransport.reload(), 1500) && 1"


@needs_browser
@needs_ssh
def test_a_fresh_facet_finds_the_answer_signed_after_the_request_left_the_listing(tmp_path: Path,
                                                                                  monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        go = _hold_verify(monkeypatch, 8.0)
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": CARD_SENT_THEN_LOST.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=9000)},                  # the verify is let through: it signs, the row leaves
            {"eval": HISTORY},
            {"until": f"{ROW} && 1", "ms": 30000},
            {"eval": OPEN_ROW},                               # a facet drawn fresh while the attempt is unsettled
            {"until": ROW_SAYS.format(re="/the cockpit confirmed it|no longer open/", note=ROW_NOTE, opts=ROW_OPTIONS),
             "ms": 30000},
            # the strip's own card, frozen meanwhile, is told the same answer
            {"until": f"/the cockpit confirmed it|was not signed|no longer open/.test({FACET_NOTE}) && {FACET_NOTE}",
             "ms": 30000},
        ])
        go.set()
    v = out["values"]
    assert "still verifying" in v[3], v
    row, drawn = json.loads(v[-2])
    assert row.startswith("signed (b)") and "the cockpit confirmed it" in row, v
    assert drawn == 0, v                              # the request is no longer open: the fresh facet drew no options
    assert v[-1].startswith("signed (b)") and "the cockpit confirmed it" in v[-1], v
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_fresh_facet_asks_on_every_board_read_until_the_answer_reads_refused(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    asked: list[str] = []
    real = type(rig.signer).attempt

    def attempt(self, request_id: str, req: Any) -> Any:
        out = real(self, request_id, req)
        asked.append(out["state"])
        if out["state"] == "pending":                 # the request leaves while its answer still verifies
            (rig.qdir / "sign" / f"{request_id}.json").unlink(missing_ok=True)
        return out

    monkeypatch.setattr(type(rig.signer), "attempt", attempt)
    with rig.serving():
        # the verify is let through 20 s from now: the fresh facet has asked more than once by then
        go = _hold_verify(monkeypatch, 20.0)
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": CARD_SENT_THEN_LOST.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=2500)},                  # past the page's 2 s listing reuse
            {"eval": HISTORY},
            {"until": f"{ROW} && 1", "ms": 30000},
            {"eval": OPEN_ROW},
            {"until": ROW_SAYS.format(re="/asked about again/", note=ROW_NOTE, opts=ROW_OPTIONS), "ms": 30000},
            {"eval": READS_EVERY_1500},
            # the board reads go on asking; when the answer is refused the strip's frozen card is told and given back
            {"until": f"/not signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 45000},
            {"eval": "clearInterval(window.__reads) || 1"},
        ])
        go.set()
    v = out["values"]
    assert "still verifying" in v[3], v
    row, drawn = json.loads(v[8])
    assert "no longer open" in row and "asked about again" in row and drawn == 0, v
    assert v[10].startswith("not signed:") and "nothing was signed" in v[10], v
    assert asked.count("pending") >= 2 and asked[-1] == "refused", asked   # asked on later board reads, not dropped
    assert rig.nothing_signed(QRID)
