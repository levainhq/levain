"""Intention controls, S4, in headless Chrome over the real server: a band row that an open stamped agenda rings is drawn with
Snooze, Edit, Done and Clear in place of an answer box, a machine-filed row keeps its options, and one tap of Done resolves the
agenda and closes the row. Desktop and phone width."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from anneal_memory import SporeStore

from levain.consent.questions import ANSWER_CHANNELS, QuestionStore
from tests.test_cockpit_flip_parity import _drive, _eventually, needs_browser
from tests.test_cockpit_k2a import _install, _serve

DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
TODAY_ISO = "2026-11-08"

# the Decisions panel opens in the modal; each open row's body is read on its button
OPEN_HISTORY = "(document.getElementById('levain-modal') || window.LevainNeeds.history(null)) && 1"
ROWS = "[...document.querySelectorAll('#levain-modal [data-question-row]')]"
OPEN_ALL = "(" + ROWS + ".forEach(r => { const b = r.querySelector('.consent-details'); if (b && !b.disabled) b.click(); }), 1)"
PROBE = ("JSON.stringify({sets: [...document.querySelectorAll('#levain-modal .intention-actions')].map(w => [...w.querySelectorAll('button')]"
         ".map(b => b.textContent)), options: document.querySelectorAll('#levain-modal .question-option').length,"
         " words: document.querySelectorAll('#levain-modal .question-words').length,"
         " per: [...document.querySelectorAll('#levain-modal [data-question-row]')].map(r => [r.dataset.questionN, r.querySelectorAll('.question-option').length, r.querySelectorAll('.intention-actions').length, r.querySelectorAll('.question-words').length]),"
         " listBand: document.querySelectorAll('[data-surface=band]').length,"
         " listSets: document.querySelectorAll('[data-surface=band] .intention-actions').length,"
         " fits: document.documentElement.scrollWidth <= window.innerWidth + 1, probe: 1})")
READY = ("document.querySelectorAll('#levain-modal .intention-actions button').length >= 4 && "
         "document.querySelectorAll('#levain-modal .question-option').length >= 2 && 1")


def _steps(width: dict, *more: dict) -> list[dict]:
    return [width, {"until": "window.LevainNeeds && window.LevainNeeds.history && 1", "ms": 30000},
            {"do": OPEN_HISTORY, "until": f"{ROWS}.length === 2 && 1", "ms": 30000},
            {"do": OPEN_ALL, "until": READY, "ms": 30000, "every": 2000}, *more]


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    (tmp_path / "lh" / "consent").mkdir(parents=True, mode=0o700)
    return tmp_path / "lh" / "consent"


def _rig(tmp_path: Path, lh: Path):
    root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    ring, _ = store.file_once(agent="claude_code", key="levain-germinate:k1", what="Due 11-08: renew the thing (spore)",
                              options=[("done", "Done")], rec="done", blocks="the agenda came due", cwd=str(root),
                              held_by=ANSWER_CHANNELS)
    store.file(agent="claude_code", agent_session_id="s2", what="Ship the sweep fix tonight?",
               options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the sweep", cwd=str(root))
    sp = SporeStore(root / ".levain" / "memory.spores.json")
    sid = sp.add(type="task", text="renew the thing", disposition="agenda", next=TODAY_ISO)["id"]
    sp.update(sid, germinated={"on": TODAY_ISO, "target": "band", "ref": ring.id})
    return src, store, sp, sid, ring


def _probe(out: dict) -> dict:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"probe"' in v][0]


@needs_browser
@pytest.mark.parametrize("width", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_a_ringing_row_is_drawn_with_the_four_and_a_plain_row_keeps_its_options(tmp_path: Path, lh: Path, width) -> None:
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(width, {"eval": PROBE}))
    seen = _probe(out)
    assert seen["sets"] and all(labels == ["Snooze", "Edit", "Done", "Clear"] for labels in seen["sets"]), seen
    # per row: [question n, option buttons, intention control sets, words boxes]. The ringing row has the four and no answer box; the
    # machine-filed row keeps its two options and its box. The List draws the same four on the ringing band row, and only there.
    assert seen["per"] == [["1", 0, 1, 0], ["2", 2, 0, 1]], json.dumps(seen)
    assert seen["listSets"] == 1, json.dumps(seen)
    assert seen["fits"], seen


@needs_browser
def test_one_tap_of_done_resolves_the_agenda_and_closes_the_row(tmp_path: Path, lh: Path) -> None:
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    click = "document.querySelector('#levain-modal .intention-actions button[data-verb=intention_done]').click(); 1"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(DESKTOP, {"eval": click}, {"posts": 1}))
        closed = _eventually(lambda: store.get(ring.id).answer is not None, True)
    assert any(p.startswith("/cockpit/verb ") and "intention_done" in p for p in out["posts"]), out["posts"]
    assert closed and sp.get(sid)["status"] != "open" and sp.get(sid)["resolution"]["kind"] == "done"


# --- fix round 1 ---------------------------------------------------------------------------------------------------------

RING = "document.querySelector('#levain-modal [data-question-row] .intention-actions')"
CARD = "document.querySelector('#levain-modal [data-question-row]')"


def _tap(verb: str) -> str:
    return f"{RING}.querySelector('button[data-verb={verb}]').click(); 1"


@needs_browser
def test_edit_starts_from_the_reminders_words_and_an_untouched_save_writes_nothing(tmp_path: Path, lh: Path) -> None:
    """H1 + K1: the card reads as the reminder's own words, Edit starts from them (never the "Due ...: ..." the row was filed
    with), and Save on an untouched field sends nothing."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    probe = ("JSON.stringify({ta: (" + RING + ".querySelector('textarea') || {}).value, title: " + CARD + ".textContent,"
             " msg: (" + RING + ".querySelector('.edit-msg') || {}).textContent, probe: 1})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_edit"), "until": RING + ".querySelector('textarea') && 1", "ms": 10000},
            {"do": RING + ".querySelector('.verb-confirm').click(); 1",
             "until": "(" + RING + ".querySelector('.edit-msg') || {}).textContent === 'nothing changed' && 1", "ms": 10000},
            {"eval": probe}))
    seen = _probe(out)
    assert seen["ta"] == "renew the thing", seen
    assert "renew the thing" in seen["title"] and "Due 11-08" not in seen["title"], seen
    assert not [p for p in out["posts"] if "intention_edit" in p], out["posts"]
    assert sp.get(sid)["text"] == "renew the thing"


@needs_browser
def test_snooze_names_when_it_comes_back_refuses_today_and_clear_quotes_the_reminder(tmp_path: Path, lh: Path) -> None:
    """U2, U3, U5: the snooze form says when the row comes back, takes no day before tomorrow (in plain words, nothing sent),
    and Clear asks about the reminder by its words."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    set_date = (RING + ".querySelector('input[type=date]').value = {v}; " + RING
                + ".querySelector('input[type=date]').dispatchEvent(new Event('input')); 1")
    today = "(() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); })()"
    probe = ("JSON.stringify({min: " + RING + ".querySelector('input[type=date]').min, back: (" + RING
             + ".querySelector('.intention-back') || {}).textContent, msg: (" + RING + ".querySelector('.edit-msg') || {}).textContent, probe: 1})")
    ask = "JSON.stringify({ask: " + RING + ".querySelector('.verb-form').textContent, probe: 2})"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"do": set_date.format(v="'2026-12-03'"), "until": "1", "ms": 1000},
            {"eval": probe},
            {"do": set_date.format(v=today) + " && " + RING + ".querySelector('.verb-confirm').click(); 1",
             "until": "(" + RING + ".querySelector('.edit-msg') || {}).textContent.length > 0 && 1", "ms": 10000},
            {"eval": probe},
            {"do": RING + ".querySelector('.verb-form .verb-cancel').click(); 1", "until": "1", "ms": 500},
            {"do": _tap("intention_clear"), "until": RING + ".querySelector('.verb-form') && 1", "ms": 10000},
            {"eval": ask}))
    vals = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"probe"' in v]
    first, second, cleared = vals[0], vals[1], vals[2]
    assert first["back"].startswith("comes back ") and "Dec" in first["back"] and first["min"] > "2026-01-01", first
    assert second["msg"] == "pick a later day", second
    assert "renew the thing" in cleared["ask"] and "for good" in cleared["ask"], cleared
    assert not [p for p in out["posts"] if "intention_snooze" in p or "intention_clear" in p], out["posts"]
    assert store.get(ring.id).answer is None


@needs_browser
def test_on_a_phone_each_reminder_control_is_a_44px_target(tmp_path: Path, lh: Path) -> None:
    """U4: at phone width the reminder row's controls meet a 44px tap target; other verb buttons keep their look."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    probe = ("JSON.stringify({sizes: [...document.querySelectorAll('#levain-modal .intention-actions button')].filter(b => b.offsetParent)"
             ".map(b => [b.getBoundingClientRect().height, b.getBoundingClientRect().width]), probe: 1})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(PHONE, {"eval": probe}))
    sizes = _probe(out)["sizes"]
    assert sizes and all(h >= 44 and w >= 44 for h, w in sizes), sizes
