"""The cockpit keeps asking about an unsettled answer after its request leaves the listing (L3 r1 codex MED on
dashboard_core.js): headless Chrome (its own profile, tests/_page_drive_run.py through ``_drive``) on a real cockpit
server, over the rig of test_sign_attempt_page.

The class: an answer from the "Needs you" card whose reply was lost reads ``pending`` (it is still verifying); the
request then leaves the open listing (it signed, or its file was removed) and a facet for it is drawn fresh: the
Decisions panel's row for the question, opened ("open"). (The focus view's card asks through its own settle, and is
gone once the focus view is closed to reach the panel.) That fresh facet used to drop the unsettled attempt and say only "no longer open", which also
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

# the Needs-you item's focus view is a modal that holds the page while it is open: it is closed first, then "all decisions"
# opens the Decisions panel
HISTORY = ("(document.querySelector('#levain-modal .modal-close') && document.querySelector('#levain-modal .modal-close').click(), "
           "document.querySelector('#ck-needs .needs-history').click(), 1)")
ROW = "document.querySelector('[data-question-row]')"
OPEN_ROW = f"({ROW}.querySelector('.consent-details').click(), 1)"
ROW_NOTE = f"(({ROW} || document).querySelector('.sign-note') || {{}}).textContent"
ROW_OPTIONS = f"(({ROW} || document).querySelectorAll('.sign-option')).length"
# the "not signed:" note, the row opened again whenever it reads closed (a repaint removes the note with the open state)
NOTE_REOPENING = (f"({ROW} && !{ROW}.querySelector('.sign-note') && ({ROW}.querySelector('.consent-details') || {{}}).click?.(), "
                  f"/not signed:/.test({ROW_NOTE}) && {ROW_NOTE})")
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
            {"eval": READS_EVERY_1500},                       # the board reads go on: the fresh facet is asked again
            # wait for the TARGET (the signed text), never the interim "no longer open"; a board read repaints the
            # row and its note with it, so the row is opened again whenever it reads closed
            {"until": f"({ROW} && !{ROW}.querySelector('.sign-note') && {ROW}.querySelector('.consent-details').click(), "
                      + ROW_SAYS.format(re="/the cockpit confirmed it/", note=ROW_NOTE, opts=ROW_OPTIONS) + ")",
             "ms": 45000},
            {"eval": "clearInterval(window.__reads) || 1"},
        ])
        go.set()
    v = out["values"]
    assert "still verifying" in v[3], v
    row, drawn = json.loads(v[-2])
    assert row.startswith("signed (b)") and "the cockpit confirmed it" in row, v
    assert drawn == 0, v                              # the request is no longer open: the fresh facet drew no options
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
            # the board reads go on asking; when the answer is refused the fresh facet is told
            # (a board read repaints the row and its note with it: the row is opened again whenever it reads closed)
            {"until": f"({ROW} && !{ROW}.querySelector('.sign-note') && {ROW}.querySelector('.consent-details').click(), "
                      f"/not signed/.test({ROW_NOTE}) && {ROW_NOTE})", "ms": 45000},
            {"eval": "clearInterval(window.__reads) || 1"},
        ])
        go.set()
    v = out["values"]
    assert "still verifying" in v[3], v
    row, drawn = json.loads(v[8])
    assert "no longer open" in row and "asked about again" in row and drawn == 0, v
    assert "was not signed" in v[10] or v[10].startswith("not signed:"), v
    assert asked.count("pending") >= 2 and asked[-1] == "refused", asked   # asked on later board reads, not dropped
    assert rig.nothing_signed(QRID)


REOPEN_PANEL = f"({ROW} || ({HISTORY}, 1)) && 1"


@needs_browser
@needs_ssh
def test_a_facet_drawn_after_the_answer_settled_refused_says_so_and_keeps_saying_it(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    """L3 r2 codex MED: once the answer settled and its request is gone, a facet drawn afterwards found nothing
    pending and said only "no longer open"; a repaint kept that. It now says what the cockpit settled."""
    rig = _Rig(tmp_path, monkeypatch)

    def attempt(self, request_id: str, req: Any) -> Any:
        out = real(self, request_id, req)
        if out["state"] == "pending":                 # the request leaves while its answer still verifies
            (rig.qdir / "sign" / f"{request_id}.json").unlink(missing_ok=True)
        return out

    real = type(rig.signer).attempt
    monkeypatch.setattr(type(rig.signer), "attempt", attempt)
    with rig.serving():
        go = _hold_verify(monkeypatch, 8.0)
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": CARD_SENT_THEN_LOST.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": READS_EVERY_1500},
            {"until": f"/not signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 45000},   # settled, nobody drawn yet
            {"eval": "clearInterval(window.__reads) || 1"},
            {"eval": AFTER.format(ms=2500)},                  # past the page's 2 s listing reuse
            {"eval": HISTORY},
            {"until": f"{ROW} && 1", "ms": 30000},
            {"eval": OPEN_ROW},                               # a facet drawn fresh after the answer settled
            {"until": NOTE_REOPENING, "ms": 30000},
            {"eval": AFTER.format(ms=300)},
            # an until that reopens the row, not a bare sample or a bare wait: a board-read repaint may have removed the
            # note at this instant along with the row's open state, and nothing else would open it again
            {"until": NOTE_REOPENING, "ms": 30000},
            {"eval": "window.LevainCockpitTransport.reload() && 1"},                      # a repaint
            {"eval": AFTER.format(ms=2500)},
            {"eval": REOPEN_PANEL},
            {"until": f"{ROW} && 1", "ms": 30000},
            {"eval": f"({ROW}.querySelector('.sign-note') ? 1 : ({OPEN_ROW}, 1))"},
            {"until": NOTE_REOPENING, "ms": 30000},
        ])
        go.set()
    v = out["values"]
    for note in (v[11], v[13], v[-1]):
        assert note.startswith("not signed:") and "nothing was signed" in note, v
    assert rig.nothing_signed(QRID)


# the first POST ending in `tail` is answered 503 by the page itself: it never reaches the cockpit, so the page does not
# know whether it signed (the first-pass settle, signSettle, asks the cockpit by the attempt id)
CARD_ANSWER_503 = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
                   "if (armed && String(u).endsWith({tail})) {{ armed = false; "
                   "return Promise.resolve(new Response('{{}}', {{status: 503}})); }} "
                   "return real.apply(this, arguments); }}; return 1; }})()")


@needs_browser
@needs_ssh
def test_a_facet_drawn_after_the_first_pass_settle_refused_still_says_why(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 deepseek MED: signSettle's own `refused` (the first pass, not the board-read retry) did not mark the outcome
    settled, so a card drawn after the request left the listing said only "no longer open"."""
    rig = _Rig(tmp_path, monkeypatch)
    real = type(rig.signer).attempt

    def attempt(self, request_id: str, req: Any) -> Any:
        out = real(self, request_id, req)
        if out["state"] == "refused":                 # the request leaves the listing once it is settled refused
            (rig.qdir / "sign" / f"{request_id}.json").unlink(missing_ok=True)
        return out

    monkeypatch.setattr(type(rig.signer), "attempt", attempt)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": CARD_ANSWER_503.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"eval": AFTER.format(ms=3000)},
            {"eval": HISTORY},
            {"until": f"{ROW} && 1", "ms": 30000},
            {"eval": OPEN_ROW},                               # a facet drawn fresh after the request left
            {"until": NOTE_REOPENING, "ms": 30000},
        ])
    note = out["values"][-1]
    assert note.startswith("not signed:") and "nothing was signed" in note, out["values"]
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_a_signed_request_leaves_nothing_frozen_behind_for_a_reused_id(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 deepseek LOW: a signed outcome left its signInFlight entry for the life of the page, so the same request id
    filed again drew a card frozen by a ceremony that ended long ago."""
    import shutil
    import threading
    import time

    from tests.test_cockpit_sign_card import _write_request
    from tests.test_cockpit_sign_card_r5 import BUTTONS

    rig = _Rig(tmp_path, monkeypatch)
    d = rig.qdir / "sign"
    saved = (d / f"{QRID}.json").read_text()

    def refile() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (d / f"{QRID}.signed.json").exists():
            time.sleep(0.1)
        time.sleep(1.0)
        for f in d.glob(f"{QRID}.*"):                 # the request is filed again, open, under the same id
            f.unlink(missing_ok=True)
        (d / f".{QRID}.re").write_text(saved)
        (d / f".{QRID}.re").replace(d / f"{QRID}.json")

    with rig.serving():
        t = threading.Thread(target=refile, daemon=True)
        t.start()
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=3500)},                  # the request is filed again meanwhile
            {"eval": "window.LevainCockpitTransport.reload() && 1"},
            {"eval": "(document.querySelector('#levain-modal .modal-close') && document.querySelector('#levain-modal .modal-close').click(), 1)"},
            {"until": f"({STRIP_FACET}) && {BUTTONS}", "ms": 30000},   # opened again: a card drawn fresh
        ])
        t.join(5)
    buttons = out["values"][-1]
    assert buttons and not any(buttons), out["values"]
