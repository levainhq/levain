"""Intention controls, S4, in headless Chrome over the real server: a band row that an open stamped agenda rings is drawn with
Snooze, Edit, Done and Clear in place of an answer box, a machine-filed row keeps its options, and one tap of Done resolves the
agenda and closes the row. Desktop and phone width."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from anneal_memory import IntentionStore

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
# Ready is read PER ROW, never from modal-wide counts: the band strip at the head of the Decisions panel draws its own
# `.question-option` buttons (three of them here), so a modal-wide ">= 2 options" holds before a row's body is drawn and a probe
# then saw `[["2", 0, 0, 0]]` (CI run 42872732445). A row's body is drawn once its "open" button is gone and its slot no longer
# reads "reading the question…"; the plain row must also hold its own option buttons and answer box.
READY = ("(() => { const rows = " + ROWS + "; return rows.length === 2 && rows.every(r => !r.querySelector('.consent-details')"
         " && r.querySelector('.consent-body') && !/reading the question/.test(r.querySelector('.consent-body').textContent))"
         " && rows.some(r => r.querySelectorAll('.intention-actions button').length >= 4)"
         " && rows.some(r => r.querySelectorAll('.question-option').length >= 2 && r.querySelector('.question-words')) && 1; })()")


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
    ring, _ = store.file_once(agent="claude_code", key="levain-germinate:k1", what="Due 11-08: renew the thing (intention)",
                              options=[("done", "Done")], rec="done", blocks="the agenda came due", cwd=str(root),
                              held_by=ANSWER_CHANNELS)
    store.file(agent="claude_code", agent_session_id="s2", what="Ship the sweep fix tonight?",
               options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the sweep", cwd=str(root))
    sp = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
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


# a slow read of the plain row's question (the loaded-runner order, forced): the band strip's own option buttons are on the page
# before that row's body is, and the probe must still wait for the row
SLOW_ROW_READ = ("(() => { const f = window.fetch; window.fetch = (u, ...a) => /cockpit\\/question\\/2/.test(String(u)) ? "
                 "new Promise(r => setTimeout(r, 4000)).then(() => f(u, ...a)) : f(u, ...a); })()")


@needs_browser
def test_the_probe_waits_for_a_row_whose_question_read_is_slow(tmp_path: Path, lh: Path) -> None:
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    steps = _steps(PHONE, {"eval": PROBE})
    steps.insert(1, {"init": SLOW_ROW_READ})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    seen = _probe(out)
    assert seen["per"] == [["1", 0, 1, 0], ["2", 2, 0, 1]], json.dumps(seen)


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
def test_a_timed_snooze_reads_back_its_minute_not_the_date_alone(tmp_path: Path, lh: Path) -> None:
    """Diogenes c4e1124a: a timed pick (a date plus the time field) names the minute in the read-back; a date-only pick
    does not. The day is far ahead so the test never meets the wall clock."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    q = RING + ".querySelector('input[type=date]')"
    set_day = q + ".value = '2030-01-05'; " + q + ".dispatchEvent(new Event('input')); 1"
    t = RING + ".querySelector('input[type=time]')"
    set_time = t + ".value = '09:30'; " + t + ".dispatchEvent(new Event('input')); 1"
    probe = "JSON.stringify({back: (" + RING + ".querySelector('.intention-back') || {}).textContent, probe: 1})"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": q + " && 1", "ms": 10000},
            {"do": set_day, "until": "(" + RING + ".querySelector('.intention-back') || {}).textContent.length > 0 && 1", "ms": 10000},
            {"eval": probe},
            {"do": set_time, "until": "/ at 09:30$/.test((" + RING + ".querySelector('.intention-back') || {}).textContent || '') && 1", "ms": 10000},
            {"eval": probe}))
    vals = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"probe"' in v]
    assert vals[0]["back"].startswith("comes back ") and " at " not in vals[0]["back"] and "2030" not in vals[0]["back"], vals[0]
    assert vals[1]["back"].endswith(" at 09:30"), vals[1]


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


@needs_browser
def test_a_stale_snooze_keeps_its_words_and_retires_the_form(tmp_path: Path, lh: Path) -> None:
    """U6: a snooze refused as stale (the reminder moved under the open form) leaves the stale words showing and the form
    dead, as the Edit path does: no button left to send the old request again."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    # the transport answers the next write as the host does for a moved reminder
    make_stale = ("window.LevainCockpitTransport.rowVerb = async () => ({ ok: false, error: 'stale' }); 1")
    set_date = (RING + ".querySelector('input[type=date]').value = '2030-01-05'; " + RING
                + ".querySelector('input[type=date]').dispatchEvent(new Event('input')); " + RING
                + ".querySelector('.verb-confirm').click(); 1")
    probe = ("JSON.stringify({msg: (" + RING + ".querySelector('.edit-msg') || {}).textContent,"
             " live: [...(" + RING + ".querySelector('.verb-form') || document.body).querySelectorAll('button')].filter(b => !b.disabled).length,"
             " forms: " + RING + ".querySelectorAll('.verb-form').length, probe: 1})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"eval": make_stale},
            {"do": set_date, "until": "(" + RING + ".querySelector('.edit-msg') || {}).textContent.includes('changed') && 1", "ms": 10000},
            {"eval": probe}))
    seen = _probe(out)
    assert seen["msg"] == "this reminder changed: reload", seen
    assert seen["forms"] == 1 and seen["live"] == 0, seen
    assert store.get(ring.id).answer is None


@needs_browser
def test_a_snooze_form_carries_no_who_control(tmp_path: Path, lh: Path) -> None:
    """The snooze form is the date form only: a who control is never drawn into it."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    probe = ("JSON.stringify({form: !!" + RING + ".querySelector('.verb-form input[type=date]'),"
             " who: " + RING + ".querySelectorAll('.who-ctl, .who-opt').length, probe: 1})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"eval": probe}))
    seen = _probe(out)
    assert seen["form"] and seen["who"] == 0, seen


@needs_browser
def test_a_timed_snooze_to_today_is_posted_and_the_host_judges_the_minute(tmp_path: Path, lh: Path) -> None:
    """Row 184: a date-only snooze to today is refused in the page (nothing sent); the same day with a time is sent to the host,
    which judges the minute (00:00 is never after now, so it refuses and the row stays open)."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    today = "(() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); })()"
    fill = (RING + ".querySelector('input[type=date]').value = " + today + "; " + RING
            + ".querySelector('input[type=date]').dispatchEvent(new Event('input')); ")
    set_time = (RING + ".querySelector('.verb-time').value = '00:00'; " + RING
                + ".querySelector('.verb-time').dispatchEvent(new Event('input')); ")
    click = RING + ".querySelector('.verb-confirm').click(); 1"
    msg = "(" + RING + ".querySelector('.edit-msg') || {}).textContent"
    probe = "JSON.stringify({msg: " + msg + ", form: " + RING + ".parentElement.textContent, probe: 1})"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"do": fill + click, "until": msg + ".length > 0 && 1", "ms": 10000},
            {"eval": probe}))
        first_posts = [p for p in out["posts"] if "intention_snooze" in p]
        out2 = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"do": fill + set_time + click, "until": "1", "ms": 1000},          # one tap: a repeated action would re-set the busy mark
            {"ms": 3000, "until": "1"},
            {"eval": probe}))
    assert _probe(out)["msg"] == "pick a later day" and not first_posts, (out["values"], out["posts"])
    sent = [x for x in out2["posts"] if "intention_snooze" in x]
    assert len(sent) == 1 and "T00:00" in sent[0], sent        # the timed pick to today went to the host; the host decides the minute
    assert store.get(ring.id).answer is None and sp.get(sid)["germinated"]      # 00:00 is never after now: the host refused it


@needs_browser
def test_a_past_minute_snooze_shows_the_hosts_words_and_the_form_comes_back_usable(tmp_path: Path, lh: Path) -> None:
    """Row 184 fix 1: the host refuses a past minute today with "pick a time after now"; the form must show those words (not a
    generic line), end its busy state with the buttons enabled and the typed day and time kept, and a later minute then goes."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    today = "(() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); })()"
    fill = (RING + ".querySelector('input[type=date]').value = " + today + "; " + RING
            + ".querySelector('input[type=date]').dispatchEvent(new Event('input')); ")
    set_time = lambda hhmm: (RING + ".querySelector('.verb-time').value = '" + hhmm + "'; " + RING
                             + ".querySelector('.verb-time').dispatchEvent(new Event('input')); ")
    click = RING + ".querySelector('.verb-confirm').click(); 1"
    probe = ("JSON.stringify({msg: (" + RING + ".querySelector('.edit-msg') || {}).textContent, cls: (" + RING
             + ".querySelector('.edit-msg') || {}).className, live: [...(" + RING + ".querySelector('.verb-form') || document.body)"
             ".querySelectorAll('button')].filter(b => !b.disabled).length, day: " + RING + ".querySelector('input[type=date]').value,"
             " time: " + RING + ".querySelector('.verb-time').value, probe: 1})")
    wait = lambda ms: {"until": f"Date.now() - window.__t0 > {ms} && 1", "ms": ms + 5000, "every": 100}   # a real wait: `until` is polled
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"do": "window.__t0 = Date.now(); " + fill + set_time("00:00") + click, "until": "1", "ms": 1000},
            wait(1000), {"eval": probe}, wait(5000), {"eval": probe},
            {"do": "window.__t0 = Date.now(); " + set_time("23:59") + click, "until": "1", "ms": 1000},   # one tap: a repeated action would re-set the busy mark
            wait(3000)))
    seen = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"probe"' in v]
    at1, at5 = seen[-2], seen[-1]
    for s in (at1, at5):
        assert s["msg"] == "pick a time after now" and "busy" not in s["cls"], s
        assert s["live"] >= 2 and s["time"] == "00:00", s          # the buttons are enabled again, the typed time kept
    sent = [x for x in out["posts"] if "intention_snooze" in x]
    assert len(sent) == 2 and "T00:00" in sent[0] and "T23:59" in sent[1], sent
    assert store.get(ring.id).answer is not None and sp.get(sid)["next"].endswith("T23:59"), sp.get(sid)


@needs_browser
def test_only_the_snoozes_own_refusal_sentences_reach_the_form(tmp_path: Path, lh: Path) -> None:
    """The fault() rule for row 184: a bad_request on a snooze shows the host's words only when they are one of its three plain
    refusals; any other text from a bad_request (a path, an internal string) keeps the page's fixed line, and the form is usable."""
    src, store, sp, sid, ring = _rig(tmp_path, lh)
    stub = ("(() => { const of = window.fetch; window.fetch = async (u, o) => (u === '/cockpit/verb' && o && /intention_snooze/.test(o.body || '')) "
            "? new Response(JSON.stringify({error: 'bad_request', message: '/Users/x/.levain/secret.json: boom'}), {status: 400, headers: {'content-type': 'application/json'}})"
            " : of(u, o); return 1; })()")
    fill = (RING + ".querySelector('input[type=date]').value = '2030-01-05'; " + RING
            + ".querySelector('input[type=date]').dispatchEvent(new Event('input')); " + RING + ".querySelector('.verb-confirm').click(); 1")
    probe = ("JSON.stringify({msg: (" + RING + ".querySelector('.edit-msg') || {}).textContent, live: [...(" + RING
             + ".querySelector('.verb-form') || document.body).querySelectorAll('button')].filter(b => !b.disabled).length, probe: 1})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", _steps(
            DESKTOP, {"do": _tap("intention_snooze"), "until": RING + ".querySelector('input[type=date]') && 1", "ms": 10000},
            {"eval": stub},
            {"do": fill, "until": "(" + RING + ".querySelector('.edit-msg') || {}).textContent.includes('not understood') && 1", "ms": 10000},
            {"eval": probe}))
    seen = _probe(out)
    assert seen["msg"] == "That request was not understood. Reload and try again." and "secret" not in seen["msg"], seen
    assert seen["live"] >= 2, seen
