"""Voice V1-7: a Needs-you card read aloud (card_voice.js, levain/cockpit/card_script.py, the ``card_id`` branch of
``POST /cockpit/voice/speak``, and the speaker on the opened card).

Three layers. (1) The script: the NPCardVoice port, pinned from the REAL row np-ab37059b that flowConnect's
``tools/verify_np_voice.swift`` pinned (the expected text below is that file's, word for word), and the page's JS and the
server's Python built from the same cards and compared. (2) The route, through the real ``levain serve`` envelope over the
voice-routes rig: the words are the filed question's, a body cannot carry any. (3) The page, in headless Chrome on the real
cockpit: a notification deep link opens the card; with the setting on and the page allowed to play, ``LevainVoice.play`` runs
and the script is spoken; with the setting off, the card shows Play and nothing is spoken; a locked page shows "Tap to hear this
card" and speaks on the tap.

No audio is played and no provider is reached (``speechSynthesis`` and the media element are recorders, the providers' HTTP is
the rig's fake transport). Whether iOS lets a page opened from a notification tap play without its own tap is residue only the
iPhone can answer (V1_DESIGN section 4.3); these tests pin both outcomes of that unknown. Each gating test names the line whose
revert turns it red (the RETURN records each run)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from levain.consent.questions import QuestionStore  # noqa: E402
from levain.cockpit import card_script  # noqa: E402
from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.voice.text import speakable  # noqa: E402
from tests.test_cockpit_voice_routes import (  # noqa: E402,F401  (rig, qdir: fixtures taken by name)
    SPEAK, TOKEN, call, js, qdir, rig, served,
)

HERE = Path(__file__).resolve().parent
JS = HERE.parent / "levain" / "templates" / "web" / "card_voice.js"

# np-ab37059b, the row flowConnect's verify_np_voice.swift pinned (1030+7 flow-seat, backup-detection)
LONG_WHAT = ("Your rulings are in the backup-detection design, and it is ready for your read before its build. I need your yes on "
             "how it lets the estate fail. WHAT: Your 4a + Q1a Q2c Q3a Q4a are absorbed. STEP1's I3a row now has the VPS run backup "
             "only (no forget or prune) on both repositories, with your §9 (c) encrypted volume. I3b design v5.3 went through round "
             "5 (three lineages, all NOT CLEAN) and three narrow fix verifies; the last one returned complement CLEAN and codex fixed "
             "on 3 of 4, and the 4th was a one-line arithmetic fix I checked myself, not a seat. Landed on flow main 6ea7ed58. The "
             "build brief is written and goes to the desk; it follows I3a and I4. What only you can settle, in one read of the page: "
             "- Backups are kept forever until a retention design exists, so B2 and argushub storage only grow. OPTIONS: (a) accept "
             "the failure shape as written; the build brief stands · (b) accept, but change one named item (say which) before the "
             "build · (c) hold: talk it through first REC: a BLOCKS: the I3b build's acceptance drills and its caps.")
LONG_OPTS = [("a", "accept the failure shape as written; the build brief stands"),
             ("b", "accept, but change one named item (say which) before the build"),
             ("c", "hold: talk it through first")]
LONG_EXPECTED = """Your rulings are in the backup-detection design, and it is ready for your read before its build.
a, accept the failure shape as written; the build brief stands, recommended
b, accept, but change one named item (say which) before the build
c, hold: talk it through first"""

# (what, options, rec): every shape the script has a rule for
CARDS = [
    (LONG_WHAT, LONG_OPTS, "a"),
    (LONG_WHAT, LONG_OPTS, None),                                              # no rec: "recommended" is never spoken
    ("A question?", [], None),                                                 # no options: the headline alone
    ("", [], None),                                                            # nothing arrived: nothing is read
    ("Yes. Then what happens next, once the build lands?", [("go", "Go"), ("wait", "")], "wait"),   # a short first sentence stays in the headline; an empty option is skipped; the rec is skipped with it
    ("x" * 300, [("a", "one")], "a"),                                          # no sentence end in 200: cut at a space near 160
    (("word " * 60).strip(), [("a", "  padded  ")], "zz"),                     # cut at a space; a rec naming no option adds nothing
    ("  Spaced   out \n\n headline. And more words follow here.  ", [("a", "A"), ("b", "B")], "b"),
    ("A headline long enough to stand alone.", [("a", "\n lead and trail \r\n"), ("b", "\tbar\t")], "a"),   # r1: both tiers trim the same set
]


def _js_scripts() -> list[str]:
    """card_voice.js's script() over CARDS, run by node on the page's own file (the page's card_format.js beside it)."""
    cards = [{"what": w, "options": [{"key": k, "text": t} for k, t in o], "rec": r} for w, o, r in CARDS]
    prog = ("const V = require(%s); const cards = %s; process.stdout.write(JSON.stringify(cards.map((c) => V.script(c.what, c.options, c.rec))));"
            % (json.dumps(str(JS)), json.dumps(cards)))
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_script_is_the_real_rows_words_in_the_pinned_order() -> None:
    """The card script pinned from flowConnect's verify_np_voice.swift: headline verbatim (none of the WHAT body), each option
    as "key, full text", the rec's line ending ", recommended". Red with: the `+= ", recommended"` line of card_script.script
    (or card_voice.js script) removed; `headline(what)` replaced by the whole `what` (the WHAT/BLOCKS body leaks in)."""
    assert card_script.script(LONG_WHAT, LONG_OPTS, "a") == LONG_EXPECTED
    assert _js_scripts()[0] == LONG_EXPECTED
    assert "recommended" not in card_script.script(LONG_WHAT, LONG_OPTS, None)
    assert card_script.script("A question?", [], None) == "A question?"
    assert card_script.script("", [], None) == ""


def test_the_page_and_the_server_build_the_same_script_on_every_shape() -> None:
    """The premium route reads card_script.py's script and the on-device voice reads card_voice.js's: one card, one reading.
    Red with: either file's headline rule changed alone (the 20-character floor, the 200 cap, the cut near 160), or either
    one's handling of an empty option or a rec that names no option."""
    got = _js_scripts()
    want = [card_script.script(w, o, r) for w, o, r in CARDS]
    assert got == want, [(i, a, b) for i, (a, b) in enumerate(zip(got, want)) if a != b]
    assert want[4].splitlines() == ["Yes. Then what happens next, once the build lands?", "go, Go"]
    assert want[5].splitlines()[0] == "x" * 300 and want[3] == ""


def test_a_store_that_cannot_be_read_says_the_card_not_the_room() -> None:
    """r1 (deepseek, gemini LOW): a question store that raises was answered with the ROOM's words. Red with: the ``_card_chunks``
    failure refusal back on ``room_unreadable``."""
    class Store:
        def get(self, n):
            raise RuntimeError("db gone")

    chunks, refused = cockpit_voice._card_chunks(Store(), {"card_id": "question:3", "chunk": 0})
    assert chunks is None and refused.status == 500
    assert "card" in json.loads(refused.body)["message"].lower() and "room" not in json.loads(refused.body)["message"].lower()


def _file(rig, **kw):
    store = QuestionStore(rig.qdir)
    base = dict(agent="claude_code", agent_session_id="s1", what=LONG_WHAT.split(" WHAT:")[0] + " WHAT: Your rulings are absorbed.", options=LONG_OPTS, rec="a", blocks="the I3b build", cwd="/w")
    return store.file(**{**base, **kw})


def _card(port, n=None, chunk=0, *, card_id=None, provider="openai", voice="nova", speed=1.0, extra=None, drop=()):
    body = {"card_id": card_id or f"question:{n}", "chunk": chunk, "provider": provider, "voice": voice, "speed": speed, **(extra or {})}
    for k in drop:
        body.pop(k)
    return call(port, "POST", SPEAK, json.dumps(body).encode())


def test_the_route_speaks_the_filed_questions_script_and_nothing_a_body_carries(rig) -> None:
    """The words are the filed question's: every chunk the provider is sent is a chunk of ``card_script.question_script``, in
    order, and a body that adds words (``text``, ``input``) or mixes the room's keys with ``card_id`` is refused unsent.
    Red with: the ``CARD_KEYS`` exact-key check loosened (an extra key is served); ``_card_chunks`` reading ``req["text"]``."""
    rig.add_key()
    q = _file(rig)
    want = speakable(card_script.question_script(q))
    assert want[0].startswith("Your rulings are in the backup-detection design") and "recommended" in "".join(want)
    with served(rig) as (port, _httpd):
        for i, words in enumerate(want):
            status, hdrs, body = _card(port, q.n, i)
            assert status == 200 and hdrs["content-type"] == "audio/mpeg" and hdrs["x-voice-chunks"] == str(len(want)), (i, status, body[:200])
            assert rig.transport.sent_text() == words and rig.transport.seen[-1]["keyed"]
        sent = len(rig.transport.seen)
        for extra in ({"text": "say this instead"}, {"input": "or this"}, {"room_thread_id": rig.w.thread}, {"reply_gseq": 5}):
            status, _h, body = _card(port, q.n, extra=extra)
            assert (status, js(body)["error"]) == (400, "bad_request"), extra
        status, _h, body = _card(port, q.n, len(want))
        assert (status, js(body)["error"]) == (404, "no_such_chunk")
    assert len(rig.transport.seen) == sent


def test_the_route_refuses_a_card_that_is_not_filed_or_not_named_as_one(rig) -> None:
    """Nothing is reserved or sent for a card that does not exist or an id that is not ``question:<n>``.
    Red with: ``CARD_ID`` loosened to accept ``consent:<id>`` or a signed number; the ``q is None`` refusal removed."""
    rig.add_key()
    q = _file(rig)
    with served(rig) as (port, _httpd):
        for cid in ("question:999", ):
            status, _h, body = _card(port, card_id=cid)
            assert (status, js(body)["error"]) == (404, "not_found"), cid
        for cid in ("consent:abc", "question:0", "question:-1", "question:01", "question:1x", "q-1", "question:" + "9" * 13, 7, None):
            status, _h, body = _card(port, card_id=cid) if isinstance(cid, str) else call(
                port, "POST", SPEAK, json.dumps({"card_id": cid, "chunk": 0, "provider": "openai", "voice": "nova", "speed": 1.0}).encode())
            assert (status, js(body)["error"]) == (400, "bad_request"), cid
        status, _h, body = _card(port, q.n, drop=("card_id",))
        assert (status, js(body)["error"]) == (400, "bad_request")
    assert rig.transport.seen == [] and rig.rows() == []


def test_a_card_with_no_words_reads_nothing() -> None:
    """A stored question whose ask is blank (the store refuses to file one, but a foreign store may hold one) has no script:
    ``nothing_to_read``, no chunk. Red with: the ``not chunks`` refusal of ``_card_chunks`` removed (an IndexError instead)."""
    class Blank:
        what, options, rec = " ", (), ""

    class Store:
        def get(self, n):
            return Blank()

    chunks, refused = cockpit_voice._card_chunks(Store(), {"card_id": "question:3", "chunk": 0})
    assert chunks is None and refused is not None and refused.status == 404
    assert json.loads(refused.body)["error"] == "nothing_to_read"


# --- the page -------------------------------------------------------------------------------------------------------------

from tests.test_cockpit_band_title import LONG as Q2_WHAT  # noqa: E402
from tests.test_cockpit_band_title import _open as _click_row  # noqa: E402
from tests.test_cockpit_band_title import _two_questions  # noqa: E402
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_k2a import _install, _serve  # noqa: E402
from tests.test_cockpit_k4a_needs import DESKTOP  # noqa: E402

Q2_SCRIPT = card_script.script(Q2_WHAT, [("go", "Go"), ("wait", "Wait")], "wait")
STUBS = """
(() => {
  window.__spoken = []; window.__plays = 0; window.__speakCalls = [];
  Object.defineProperty(HTMLMediaElement.prototype, 'src', { get() { return this._s || ''; }, set(v) { this._s = v; }, configurable: true });
  HTMLMediaElement.prototype.play = function () { window.__plays++; return Promise.resolve(); };
  HTMLMediaElement.prototype.pause = function () {};
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; this.voice = null; this.rate = 1; } };
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
    speak(u) { if (u.text) { window.__spoken.push(u.text); } },      // the unlock speaks an empty one
    cancel() {},
    getVoices() { return [{ name: 'local-en', lang: 'en-US', localService: true }]; } } });
  try { localStorage.setItem('levain_voice_settings', JSON.stringify({ tier: 'device', notify: %s })); } catch (_) {}
  %s
})();
"""
ACTIVE = "Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true, hasBeenActive: true } });"
IDLE = "Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: false, hasBeenActive: false } });"
CARD = "#levain-modal .card-voice"
BTN = CARD + " .card-voice-btn"
READ = ("JSON.stringify({ btn: (document.querySelector(%s) || {getAttribute: () => null}).getAttribute('aria-label'), large: !!document.querySelector(%s),"
        " spoken: window.__spoken, modal: !!document.getElementById('levain-modal') })" % (json.dumps(BTN), json.dumps(CARD + ".card-voice-large")))
SETTLE = {"eval": "new Promise((r) => setTimeout(() => r(1), 1500))"}


def _page(tmp_path: Path, monkeypatch, *, notify: bool, activation: str, via_link: bool, then=()) -> list:
    """The page driven to the opened card; the values of READ (before ``then``), of each eval in ``then``, and READ again."""
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    steps = [{"init": STUBS % ("true" if notify else "false", activation)}, DESKTOP]
    if via_link:       # a navigation inside the driven page, as tests/test_band_read_timeout.py does it
        steps += [{"eval": "location.assign('/?view=needs&item=question:2#write_token=T'); 1"}, {"until": "document.querySelector(%s) && 1" % json.dumps(BTN), "ms": 40000}]
    else:
        steps += [{"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 40000},
                  {"eval": _click_row(2)}, {"until": "document.querySelector(%s) && 1" % json.dumps(BTN), "ms": 40000}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, url + "/#write_token=T", [*steps, SETTLE, {"eval": READ}, *then, {"eval": READ}])["values"]
    return [json.loads(v) for v in (out[-2 - len(then)], out[-1])]


@needs_browser
def test_a_card_opened_from_a_notification_is_read_when_the_setting_is_on_and_the_page_may_play(tmp_path: Path, monkeypatch) -> None:
    """The notification deep link opens the card; the setting is on and the page holds a user activation, so ``LevainVoice.play``
    runs and the on-device voice is handed the card script (the NPCardVoice order). Red with: the ``opened(...)`` call in
    dashboard_core.js ``needsItemJump`` removed (the card shows Play and nothing is spoken); ``takeOpened`` ignored in
    ``speaker``; the ``V.settings().notify`` test dropped (the next test goes red instead)."""
    got = _page(tmp_path, monkeypatch, notify=True, activation=ACTIVE, via_link=True)[-1]
    assert got["spoken"] == [Q2_SCRIPT], got
    assert got["btn"] in ("Stop", "Play"), got            # reading, or already over: never the tap cue


@needs_browser
def test_the_setting_off_shows_play_and_speaks_nothing(tmp_path: Path, monkeypatch) -> None:
    """Off: the opened card shows a Play button, not the large cue, and nothing is spoken even though the page may play.
    Red with: ``V.settings().notify === true`` removed from ``speaker`` (the card is read anyway)."""
    got = _page(tmp_path, monkeypatch, notify=False, activation=ACTIVE, via_link=True)[-1]
    assert got == {"btn": "Play", "large": False, "spoken": [], "modal": True}, got


@needs_browser
def test_a_locked_page_degrades_to_tap_to_hear_this_card_and_the_tap_reads_it(tmp_path: Path, monkeypatch) -> None:
    """On: but the freshly loaded page holds no gesture (what a notification tap gives, the iOS unknown), so the card shows the
    large "Tap to hear this card" and speaks nothing; the tap plays it. Red with: ``mayPlay`` returning true on a locked page
    (the card is spoken at load, so the first assertion fails); ``large = true`` not set (the label reads Play)."""
    click = {"eval": "document.querySelector(%s).click(); new Promise((r) => setTimeout(() => r(1), 800))" % json.dumps(BTN)}
    before, after = _page(tmp_path, monkeypatch, notify=True, activation=IDLE, via_link=True, then=[click])
    assert before == {"btn": "Tap to hear this card", "large": True, "spoken": [], "modal": True}, before
    assert after["spoken"] == [Q2_SCRIPT] and after["large"] is False, after


@needs_browser
def test_a_card_opened_any_other_way_never_reads_itself(tmp_path: Path, monkeypatch) -> None:
    """The strip's own row opens the card: Play, and nothing spoken, setting on and page allowed. Red with: ``takeOpened`` returning
    true without an ``opened(id)`` call (every card opened is read)."""
    got = _page(tmp_path, monkeypatch, notify=True, activation=ACTIVE, via_link=False)[-1]
    assert got == {"btn": "Play", "large": False, "spoken": [], "modal": True}, got


# --- the speaker's lifecycle (r1): a node run of the page half over a fake document ----------------------------------------

_VM = r"""
const vm = require("vm"), fs = require("fs");
const log = [];
const el = () => { const e = { children: [], cls: new Set(), isConnected: false, hidden: false, textContent: "", attrs: {}, on: {},
  appendChild(c) { this.children.push(c); return c; }, setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.on[t] = f; },
  classList: { toggle() {}, add(c) { log.push("cls:" + c); } } }; return e; };
let listeners = new Set(), playing = false, unlockedFlag = %(unlocked)s;
const V = { settings: () => ({ notify: %(notify)s }), isUnlocked: () => unlockedFlag, unlock() { log.push("unlock"); }, noteFor: () => "",
  play(r) { log.push("play"); playing = true; return new Promise((res) => { V._done = () => { playing = false; res(); }; }); },
  stop() { log.push("stop"); playing = false; if (V._done) V._done(); }, onChange: (f) => { listeners.add(f); return () => { listeners.delete(f); log.push("off"); }; } };
const win = { LevainVoice: V, document: { hidden: false, createElement: el }, navigator: { userActivation: { isActive: true } },
  Date, setTimeout, clearTimeout, setInterval, clearInterval, Promise, Object, Array, String, JSON, require: undefined };
win.window = win;
vm.runInNewContext(fs.readFileSync(%(js)s, "utf8"), win);
win.window.LevainCardFormat = undefined;
const CV = win.LevainCardVoice;
const body = { what: "A question that is long enough to be a sentence.", options: [{ key: "a", label: "yes" }], rec: "a" };
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 0));
const fire = () => { for (const f of [...listeners]) f(); };
(async () => {
%(prog)s
process.stdout.write(JSON.stringify({ log, listeners: listeners.size })); process.exit(0);
})();
"""


def _run_vm(prog: str, *, notify: bool = True, unlocked: bool = True) -> dict:
    # headline() needs card_format.js: load it into the same context first
    cf = HERE.parent / "levain" / "templates" / "web" / "card_format.js"
    head = _VM % {"notify": "true" if notify else "false", "unlocked": "true" if unlocked else "false", "js": json.dumps(str(JS)),
                  "prog": prog}
    head = head.replace('vm.runInNewContext(fs.readFileSync(%s, "utf8"), win);' % json.dumps(str(JS)),
                        'vm.createContext(win); vm.runInContext(fs.readFileSync(%s, "utf8"), win); vm.runInContext(fs.readFileSync(%s, "utf8"), win);'
                        % (json.dumps(str(cf)), json.dumps(str(JS))))
    head = head.replace("win.window.LevainCardFormat = undefined;", "")
    out = subprocess.run(["node", "-e", head], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_card_whose_page_closed_before_its_words_landed_is_not_read() -> None:
    """r1 (codex/deepseek/complement HIGH, reachable: a slow body fetch and a closed modal): the speaker is drawn after the card
    left the page. Nothing is played, billed or kept listening. Red with: the ``wrap.isConnected`` test in the arm step removed."""
    got = _run_vm("""
      CV.opened("question:2"); const w = CV.speaker("question:2", body);   // never inserted: the card was closed
      await tick(20); fire(); await tick(20);""")
    assert "play" not in got["log"] and got["listeners"] == 0, got


def test_a_card_that_is_on_the_page_is_read_after_it_is_inserted() -> None:
    """The arm waits for the insert, and reads once the card is there (the guard must not turn the notification read off)."""
    got = _run_vm("""
      CV.opened("question:2"); const w = CV.speaker("question:2", body); w.isConnected = true; await tick(20);""")
    assert got["log"].count("play") == 1, got


def test_closing_a_card_while_it_reads_stops_the_reading() -> None:
    """r1 (codex/gemini HIGH, reachable: close or refresh the modal mid-read): the audio kept playing and fetching chunks after the card was
    gone. Red with: the ``V.stop()`` of the detached-while-reading step removed."""
    got = _run_vm("""
      CV.opened("question:2"); const w = CV.speaker("question:2", body); w.isConnected = true; await tick(20);
      w.isConnected = false; await tick(700);""")
    assert got["log"].count("play") == 1 and "stop" in got["log"] and got["listeners"] == 0, got


def test_a_card_closed_without_a_click_lets_go_of_its_listener() -> None:
    """r1 (codex/deepseek MED): a speaker painted before it was inserted never learned it was attached, so closing it unread kept a
    listener (and the card's closures) for the life of the page. Red with: the attach step after the insert removed."""
    got = _run_vm("""
      const w = CV.speaker("question:2", body); w.isConnected = true; await tick(20);
      w.isConnected = false; fire(); await tick(20);""", notify=False)
    assert got["listeners"] == 0 and "play" not in got["log"], got


def test_an_opened_flag_is_spent_by_the_first_speaker_even_when_it_builds_nothing() -> None:
    """r1 (deepseek/complement MED): a notification open whose speaker returned early left the flag live, so a strip open of the same
    card within 60 s read itself. Red with: ``takeOpened`` moved back below the early returns."""
    got = _run_vm("""
      CV.opened("question:2"); const none = CV.speaker("question:2", { error: "x" });
      const w = CV.speaker("question:2", body); w.isConnected = true; await tick(20);""")
    assert "play" not in got["log"], got


def test_an_answered_card_mounts_no_speaker() -> None:
    """An answered question's card is a receipt: nothing is left to read aloud (and one ask per card). The page mounts the speaker
    only for a card that is not settled. Red with: ``!settled`` removed from the ``CV.speaker`` guard in dashboard_core.js
    ``questionCard`` (the receipt shows a Play button; tests/test_answered_card.py goes red in the browser)."""
    src = (HERE.parent / "levain" / "templates" / "web" / "dashboard_core.js").read_text(encoding="utf-8")
    start = src.index("function questionCard(q)")
    body = src[start:src.index("function clearNeeds", start)]
    assert "const settled = q.state === \"answered\";" in body
    assert "CV && !b.error && !settled ? CV.speaker(" in body, body


def test_a_card_button_without_the_icon_helper_keeps_the_padded_words_pill_and_is_named() -> None:
    """A cached voice.js with no `face` leaves card_voice.js drawing words itself. Red with: the `voice-words` class (or the
    aria-label) dropped from that fallback in card_voice.js (the words touch the pill's edges, no accessible name)."""
    got = _run_vm("""
      const w = CV.speaker("question:2", body); await tick(5);
      log.push("name:" + (w.children[0].attrs["aria-label"] || ""));""", notify=False)
    assert "cls:voice-words" in got["log"] and any(x.startswith("name:") and len(x) > 5 for x in got["log"]), got
