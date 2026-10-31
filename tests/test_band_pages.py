"""Q17-ALL S5b: the NEEDS YOU band on the two human pages, in headless Chrome (its own profile, tests/_page_drive.mjs
through ``_drive``) over the real servers: the cockpit's Decisions panel and the phone's Needs-you list.

Q1 RULED (a): "Information answers stay unsigned and are accepted only from human channels; agent-typed or relayed
answers are refused and the item stays open." The pages are the human channels; these tests drive the real shorthand
box and the real tap, and read the stores. Screenshots go to ``LEVAIN_SHOT_DIR`` when it is set, else the tmp dir."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from levain import band_view
from levain.consent import band
from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
# the Needs-you item's focus view is a modal that holds the page while it is open: it is closed first, then "all decisions"
# opens the Decisions panel
HISTORY = ("(document.querySelector('#levain-modal .modal-close') && document.querySelector('#levain-modal .modal-close').click(), "
           "document.querySelector('#ck-needs .needs-history').click(), 1)")
BAND_ROWS = "document.querySelectorAll('[data-band-row]').length"


def _shot_dir(tmp_path: Path) -> Path:
    d = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    (tmp_path / "lh" / "consent").mkdir(parents=True, mode=0o700)
    return tmp_path / "lh" / "consent"


@pytest.fixture(autouse=True)
def _no_delivery(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (
        calls.append(ref), type("D", (), {"route": "hook", "reason": "next hook delivers it"})())[1])
    return calls


def _file(store: QuestionStore, what: str, **over):
    kw = dict(agent="claude_code", agent_session_id="s1", what=what, options=OPTS, rec="exp", blocks="the feed PR", cwd="/w")
    kw.update(over)
    return store.file(**kw)


def _answer(store: QuestionStore, n: int):
    return store.get(n).answer


def _views(store: QuestionStore, viewer: str) -> list:
    try:
        with sqlite3.connect(store.path) as c:
            return c.execute("SELECT seq FROM band_views WHERE viewer = ?", (viewer,)).fetchall()
    except sqlite3.OperationalError:
        return []


# --- the cockpit's Decisions panel --------------------------------------------------------------------------------------

@needs_browser
def test_the_decisions_panel_shows_the_band_by_number_and_the_shorthand_box_answers_it(tmp_path: Path, lh) -> None:
    shots = _shot_dir(tmp_path)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1, q2 = _file(store, "Which retry policy?"), _file(store, "Ship the sweep fix tonight?", rec="fix")
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 2 && 1", "ms": 30000},
            {"until": "[...document.querySelectorAll('[data-question-row] .band-num')].every(b => !b.hidden && b.textContent) && 1", "ms": 30000},
            {"shot": str(shots / "cockpit_decisions_band.png"), "full": True},
            {"eval": "[...document.querySelectorAll('[data-band-row]')].map(r => [r.dataset.bandRow, r.querySelector('.band-num').textContent])"},
            {"eval": "[...document.querySelectorAll('[data-question-row]')].map(r => [r.querySelector('.band-num').textContent, r.querySelector('.consent-tool').textContent])"},
            {"eval": "(() => { const b = document.querySelector('.band-box'); b.value = '2a'; b.dispatchEvent(new Event('input')); document.querySelector('.band-send').click(); return 1; })()"},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "document.querySelector('[data-band-row]').dataset.bandRow"},
        ])
    v = out["values"]
    assert v[5] == [["1", "1"], ["2", "2"]], v
    assert v[6] == [["1", "question #1"], ["2", "question #2"]], v     # the band number leads; #n is the source tag
    assert v[9] == "1", v                                            # the band redrew without the closed item
    a = _answer(store, q2.n)
    assert (a.kind, a.value, a.channel) == ("option", "fix", "cockpit")
    assert _answer(store, q1.n) is None
    assert len(_views(store, "cockpit:browser")) >= 1                 # the Decisions panel being shown recorded this credential's view


@needs_browser
def test_a_tap_on_a_band_option_sends_the_shorthand_for_that_option(tmp_path: Path, lh) -> None:
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "document.querySelector('[data-band-row] .band-option:nth-child(3)').click() || 1"},
            {"until": f"{BAND_ROWS} === 0 && 1", "ms": 30000},
        ])
    assert _answer(store, q1.n).value == "none"


@needs_browser
def test_a_stale_cockpit_answer_redraws_the_band_says_why_and_records_nothing(tmp_path: Path, lh) -> None:
    shots = _shot_dir(tmp_path)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        # the box holds "1a" and the page's own view is made stale from outside: a second look by the same credential
        # (another tab) that sees a new item records a newer view before "send" is pressed
        import threading
        import time
        fired = threading.Event()

        def other_tab() -> None:
            time.sleep(6)
            _file(store, "A newer question arrives?")
            band_view.band_payload(store, "cockpit:browser")
            fired.set()
        t = threading.Thread(target=other_tab, daemon=True)
        t.start()
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "(() => { const b = document.querySelector('.band-box'); b.value = '1a'; b.dispatchEvent(new Event('input')); return 1; })()"},
            # an open state editor is unsaved work: the page defers its own passive re-reads while it is open, so the page's
            # picture of the band cannot refresh itself before the send (a self-refresh would make the view current again)
            {"eval": "(document.querySelector('.state-edit').click(), document.querySelector('.state-editor').nodeType)"},   # throws if the editor did not open
            {"eval": "new Promise(r => setTimeout(r, 9000)).then(() => 1)"},     # the other tab's look lands in the meantime
            {"eval": "(document.querySelector('.band-send').click(), 1)"},
            {"until": "/changed since it was shown/.test((document.querySelector('.band-msg') || {}).textContent || '') && 1", "ms": 30000},
            {"until": f"{BAND_ROWS} === 2 && 1", "ms": 30000},
            {"shot": str(shots / "cockpit_decisions_band_stale.png"), "full": True},
            {"eval": "document.querySelector('.band-box').value"},
            {"eval": "(document.querySelector('.band-send').click(), 1)"},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
        ])
        t.join(timeout=5)
    v = out["values"]
    assert v[10] == "1a", v                                           # the words are kept across the redraw
    assert _answer(store, q1.n).value == "fix"                        # the second send, from the fresh view


# --- the phone's Needs-you list -----------------------------------------------------------------------------------------



PHONE_READY = "document.getElementById('status').textContent !== 'reading the queue…' && 1"
PHONE_ROWS = "document.querySelectorAll('#requests [data-band-row]').length"










# --- L3 r2 fix round: a success reply that carries refusals or sheets is not read as plain success -------------------------

def _authority(store: QuestionStore):
    """A question that leads to a push is an authority item: its shorthand opens the existing sheet, records nothing."""
    from levain.consent.questions import Link
    return store.file(agent="claude_code", agent_session_id="sess-1", what="Push it?", options=[("go", "Push"), ("no", "No")],
                      rec="go", blocks="x", cwd="/tmp/proj", link=Link("go", "push", "req-1", "qid-1"))


TYPE_AND_SEND = ("(() => { const b = document.querySelector('%s'); b.value = '%s'; b.dispatchEvent(new Event('input')); "
                 "document.querySelector('%s').click(); return 1; })()")
# the page's own re-reads fail from here on (the verb post still works): a send that lands must not wait for a redraw
BLOCK_READS = ("(() => { const f = window.fetch; window.fetch = (u, ...r) => (/\\/cockpit\\/(?!verb)|consent\\.json/.test(String(u && u.url || u)) "
               "? Promise.reject(new TypeError('blocked')) : f(u, ...r)); return 1; })()")


@needs_browser
def test_a_cockpit_mixed_answer_names_the_authority_item_and_keeps_its_words(tmp_path: Path, lh) -> None:
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1, q2 = _file(store, "Which retry policy?"), _authority(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 2 && 1", "ms": 30000},
            {"eval": TYPE_AND_SEND % (".band-box", "1a 2a", ".band-send")},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "new Promise(r => setTimeout(r, 1500)).then(() => 1)"},
            {"eval": "(document.querySelector('.band-msg') || {}).textContent"},
            {"eval": "document.querySelector('.band-box').value"},
        ])
    v = out["values"]
    assert _answer(store, q1.n).value == "fix" and _answer(store, q2.n) is None
    assert "recorded: 1a" in v[-2] and "2 needs your signature" in v[-2] and "Decisions" in v[-2], v   # names the item
    assert "2a" in v[-1] and "1a" not in v[-1], v                    # the unrecorded part stays in the box


@needs_browser
def test_a_cockpit_sheets_only_answer_does_not_read_as_success_and_frees_the_controls(tmp_path: Path, lh) -> None:
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _authority(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": BLOCK_READS},
            {"eval": TYPE_AND_SEND % (".band-box", "1a", ".band-send")},
            {"eval": "new Promise(r => setTimeout(r, 2500)).then(() => 1)"},
            {"eval": "(document.querySelector('.band-msg') || {}).textContent"},
            {"eval": "document.querySelector('.band-box').value"},
            {"eval": "document.querySelector('.band-box').disabled || document.querySelector('.band-send').disabled"},
        ])
    v = out["values"]
    assert "nothing recorded" in v[-3] and "1 needs your signature" in v[-3], v
    assert v[-2] == "1a", v
    assert v[-1] is False, v                                         # re-enabled without waiting on a redraw that failed








@needs_browser
def test_a_cockpit_store_fault_shows_a_fixed_line_never_the_raw_error(tmp_path: Path, lh, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")

    def boom(*a, **k):
        raise OSError("disk fault at /Users/secret/path/questions.db")
    monkeypatch.setattr(band_view, "answer_text", boom)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": TYPE_AND_SEND % (".band-box", "1a", ".band-send")},
            {"until": "(document.querySelector('.band-msg') || {}).textContent && 1", "ms": 30000},
            {"eval": "document.querySelector('.band-msg').textContent"},
        ])
    msg = out["values"][-1]
    assert "/Users/secret" not in msg and "disk fault" not in msg, msg
