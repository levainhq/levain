"""levain#159: the cockpit has no Sync button; the masthead's freshness line says how current the page is.

Static: the button, its CSS rule sites and its JS are gone; the line is the page's one .livechip (#freshline), adopted by the stream.
Browser (real Chrome on a served cockpit): every state is driven from a FAKED clock and faked facts through the painter's own seam
(LevainFreshLine.seam), so the states, the ages at 5 s / 45 s / 2 m, the 3 s hold on a worse state, what is spoken, the cost of the fine
state (no timer, no write) and the layout at 390 / 481 / 720 / 1100 / 1440 are measured, not read from the source.

NOT held (a person judges them): whether the wording reads right at arm's length on the phone, whether a quiet board's "live" is comfort
or noise, whether the line jitters while panels repaint."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._ke_dom import WEB
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import drive_touch
from tests.test_cockpit_places import READY, SETTLE

HTML = (WEB / "dashboard.html").read_text(encoding="utf-8")
CSS = (WEB / "dashboard.css").read_text(encoding="utf-8")
BOOT = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
STREAM = (WEB / "cockpit_stream.js").read_text(encoding="utf-8")
FRESH = (WEB / "fresh_line.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------- static

def test_the_sync_button_is_gone_from_the_markup_the_css_and_the_scripts() -> None:
    assert 'id="refresh"' not in HTML and ">Sync<" not in HTML
    for name, text in (("css", CSS), ("boot", BOOT), ("stream", STREAM), ("fresh", FRESH)):
        assert "#refresh" not in text and 'getElementById("refresh")' not in text, name
    assert "btn.disabled" not in BOOT and "btn.addEventListener" not in BOOT                     # the button's handler and its disabled writes
    assert re.search(r"button#jumpbtn, button#calmbtn \{", CSS)                                  # Jump and Calm keep the shared rule, without Sync
    assert "window.LevainCockpitTransport = { readJson: authedReadJson, headers: readHeaders, reload: () => load({ passive: true })" in BOOT


def test_the_line_is_the_one_livechip_with_the_state_words_alone_in_the_live_region() -> None:
    m = re.search(r'<span class="livechip" id="freshline" data-state="connecting"[^>]*>(.*?)</span><span class="fl-age"[^>]*></span></span>', HTML, re.S)
    assert m, "the static markup of the line"
    assert HTML.count('class="livechip"') == 1 and HTML.count('role="status"') == 1
    assert 'role="status"' in m.group(1) and 'class="fl-state"' in m.group(1)               # the words, and only the words, are spoken
    assert HTML.index('id="presencebtn"') < HTML.index('id="freshline"') < HTML.index('id="jumpbtn"')
    assert HTML.index('<script src="/fresh_line.js">') < HTML.index('<script src="/cockpit_stream.js">')
    assert "createElement" not in STREAM and "getElementById" not in STREAM                       # the stream creates no chip and looks none up: it only says what it knows


def test_the_phone_rules_that_hid_the_stamp_and_zeroed_the_chip_font_are_gone() -> None:
    assert not re.search(r"body\.ke \.masthead \.stamp \{ display: none; \}", CSS)
    assert "livechip { font-size: 0" not in CSS


def test_the_wording_lives_in_one_object() -> None:
    block = FRESH.split("var STRINGS = {", 1)[1].split("};", 1)[0]
    for word in ("connecting", "live", "not signed in", "can't reach", "stream down", "stale", "updated {age} ago", "updating"):
        assert word in block, word


# ---------------------------------------------------------------------------------------------------------------- browser

def _vals(out: dict) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]


def _open(tmp_path: Path, steps: list, touch: bool = False) -> dict:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        run = drive_touch if touch else _drive
        return run(tmp_path, f"{url}/#write_token=T", steps)


# The page after its own boot: the sign-in and board tests wait on `.livechip[data-state="live"]`; so does this one.
LIVE = {"until": "document.querySelector('.livechip[data-state=\"live\"]') && 1", "ms": 40000}

STRUCTURE = r"""(() => { const m = document.querySelector('.masthead'), l = document.getElementById('freshline'), st = l && l.querySelector('.fl-state'), ag = l && l.querySelector('.fl-age');
  return JSON.stringify({ probe: 'structure', w: innerWidth, refresh: !!document.getElementById('refresh'),
    syncText: [...m.querySelectorAll('*')].filter((n) => n.children.length === 0 && n.textContent.trim() === 'Sync').length,
    chips: document.querySelectorAll('.livechip').length, adopted: document.querySelector('.livechip') === l, inMasthead: m.contains(l),
    role: st && st.getAttribute('role'), ageOutside: !!(ag && !st.contains(ag) && ag.closest('[role=status]') === null), roleCount: l.querySelectorAll('[role=status]').length,
    state: l.dataset.state, words: st.textContent, shown: l.getClientRects().length > 0,
    jump: !!document.getElementById('jumpbtn'), calm: !!document.getElementById('calmbtn'), jumpRadius: getComputedStyle(document.getElementById('jumpbtn')).borderRadius !== '0px',
    transport: typeof window.LevainCockpitTransport.reload }); })()"""


@needs_browser
def test_no_sync_button_at_any_width_and_the_line_is_the_one_chip(tmp_path: Path) -> None:
    steps: list = [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, LIVE, SETTLE]
    for w in (390, 1440):
        steps += [{"emulate": {"w": w, "h": 844, "dpr": 1}}, SETTLE, {"eval": STRUCTURE}]
    rows = _vals(_open(tmp_path, steps))
    assert len(rows) == 2, rows
    for g in rows:
        assert g["refresh"] is False and g["syncText"] == 0, g
        assert g["chips"] == 1 and g["adopted"] and g["inMasthead"] and g["shown"], g
        assert g["role"] == "status" and g["roleCount"] == 1 and g["ageOutside"] is True, g
        assert g["state"] == "live" and g["words"] == "live", g                                      # real facts, no seam: the page's own stream is live
        assert g["jump"] and g["calm"] and g["jumpRadius"] and g["transport"] == "function", g


# A fake clock and a fake scheduler handed to the painter: it asks the seam's timers for every timeout and interval, so the test fires
# them by advancing the clock. `facts` is the page's inputs (what the shim, the stream and the view would say), changed step by step.
HARNESS = r"""
const F = window.LevainFreshLine.seam, el = document.getElementById('freshline'), st = el.querySelector('.fl-state'), ag = el.querySelector('.fl-age');
const fake = { now: Date.parse('2026-11-19T12:00:00Z'), timers: [], seq: 0, intervalsSet: 0, timeoutsSet: 0 };
const sched = {
  setInterval: (f, ms) => { fake.intervalsSet++; const t = { id: ++fake.seq, f, ms, at: fake.now + ms, every: true }; fake.timers.push(t); return t.id; },
  clearInterval: (id) => { fake.timers = fake.timers.filter((t) => t.id !== id); },
  setTimeout: (f, ms) => { fake.timeoutsSet++; const t = { id: ++fake.seq, f, ms, at: fake.now + ms, every: false }; fake.timers.push(t); return t.id; },
  clearTimeout: (id) => { fake.timers = fake.timers.filter((t) => t.id !== id); },
};
const advance = (ms) => { const end = fake.now + ms;
  for (;;) { const due = fake.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break; fake.now = due.at; if (due.every) due.at += due.ms; else fake.timers = fake.timers.filter((t) => t !== due); due.f(); }
  fake.now = end; };
const facts = { boot: {}, stream: {}, heads: {}, held: {} };
const WHOLE = fake.now - 60000;
const live = () => { facts.boot = { wholeDrawn: true, lastWholeAt: WHOLE, inflight: false, inflightAt: 0, refused: false, failed: '', failedAt: 0 };
  facts.stream = { state: 'live', lastChunk: fake.now, faults: [] }; facts.heads = {}; facts.held = {}; };
F.setClock(() => fake.now); F.setTimers(sched);
F.setInputs((now) => ({ now, boot: facts.boot, stream: facts.stream, held: facts.held, heads: facts.heads, titleOf: (id) => id[0].toUpperCase() + id.slice(1) }));
const snap = () => ({ state: el.dataset.state, words: st.textContent, age: ag.textContent, ageHidden: ag.hasAttribute('hidden'), armed: F.armed(), timers: fake.timers.length });
const tell = () => { F.recompute(); };
let mutations = 0; const obs = new MutationObserver((rs) => { mutations += rs.length; }); obs.observe(el, { subtree: true, childList: true, characterData: true, attributes: true });
const flush = () => { const n = obs.takeRecords().length; mutations += n; return n; };
const out = {};
"""

STATES = HARNESS + r"""
// 1. the first paint and the first connecting -> live are silent
F.reset(); flush();
facts.boot = { wholeDrawn: false, lastWholeAt: 0 }; facts.stream = { state: 'connecting' };
tell(); out.cold = snap();
live(); tell(); out.firstLive = Object.assign(snap(), { writes: F.writes() });

// 2. the fine state costs nothing: no timer, no interval ever set, no write over a simulated minute of heartbeats and events
flush(); const i0 = fake.intervalsSet, t0 = fake.timeoutsSet, w0 = F.writes().length;
for (let k = 0; k < 4; k++) { advance(15000); facts.stream.lastChunk = fake.now; document.dispatchEvent(new CustomEvent('levain:fresh')); }
out.fine = { mutations: flush(), intervals: fake.intervalsSet - i0, timeouts: fake.timeoutsSet - t0, writes: F.writes().length - w0, timers: fake.timers.length, snap: snap() };

// 3. the stream drops: a worse state is held 3 s, then written and spoken; the age moves in 5 s steps under a minute, then 30 s steps
flush(); const w1 = F.writes().length;
const lastBytes = fake.now - 2000; facts.stream = { state: 'down', since: fake.now, lastChunk: lastBytes, faults: [] }; tell();
out.heldAt0 = snap(); advance(2900); out.heldAt2900 = snap(); advance(200);
out.down = Object.assign(snap(), { writes: F.writes().slice(w1) });
const ages = {};
const at = (secs) => { advance(lastBytes + secs * 1000 - fake.now); return snap(); };
ages.s45 = at(45); ages.s60 = at(60); ages.m2 = at(120); ages.m2b = at(150);
out.ages = ages; out.downTitle = el.title;
// the words did not change while the age ran: one write for the whole run (the spoken half is untouched by a ticking age)
out.speakCount = F.writes().length - w1;

// 4. recovery: at once, and spoken because the worse state was
live(); tell(); out.recovered = Object.assign(snap(), { last: F.writes()[F.writes().length - 1] });

// 5. a flap shorter than the hold writes nothing at all
flush(); const w2 = F.writes().length;
facts.stream = { state: 'down', since: fake.now, lastChunk: fake.now - 2000, faults: [] }; tell(); advance(1500); live(); tell(); advance(5000);
out.flap = { writes: F.writes().length - w2, mutations: flush(), snap: snap() };

// 6. a stale panel: named (oldest first, +N for more), aged from its own read
facts.heads = { board: { status: 'stale', as_of: new Date(fake.now - 180000).toISOString() } }; tell(); advance(3100);
out.stale1 = snap();
facts.heads = { board: { status: 'stale', as_of: new Date(fake.now - 180000).toISOString() }, inbox: { status: 'stale', as_of: new Date(fake.now - 300000).toISOString() } }; tell(); advance(5000);
out.stale2 = snap();
live(); tell();

// 7. a failed read, then a refusal (a worse one still)
facts.boot = Object.assign({}, facts.boot, { failed: 'HTTP 502', failedAt: fake.now }); tell(); advance(3100); out.failed = snap();
facts.boot = Object.assign({}, facts.boot, { refused: true }); tell(); advance(3100); out.refused = snap();
live(); tell(); out.recovered2 = snap();

// 8. a read that takes longer than a second says "updating", and says nothing before that
facts.boot = Object.assign({}, facts.boot, { inflight: true, inflightAt: fake.now }); tell(); out.upd0 = snap(); advance(1100); out.upd1 = snap();
live(); tell(); out.upd2 = snap();

// 9. a hidden tab is frozen: no write, no interval; seen again, it is repainted
Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
facts.stream = { state: 'down', since: fake.now, lastChunk: fake.now - 2000, faults: [] }; flush(); tell(); advance(10000);
out.hidden = { snap: snap(), mutations: flush() };
Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
document.dispatchEvent(new Event('visibilitychange')); advance(3100);
out.seen = snap();

// 10. a stream that has never been live is given the longer hold: a slow first hello is not "stream down"
F.reset(); facts.boot = { wholeDrawn: false, lastWholeAt: 0 }; facts.stream = { state: 'connecting' }; tell();
facts.boot = { wholeDrawn: true, lastWholeAt: fake.now, inflight: false, inflightAt: 0, refused: false, failed: '', failedAt: 0 };
facts.stream = { state: 'connecting', everLive: false, faults: [] }; tell(); advance(5000); out.slowHello5 = snap(); advance(4100); out.slowHello9 = snap();
F.reset(); facts.boot = { wholeDrawn: false, lastWholeAt: 0 }; facts.stream = { state: 'connecting' }; tell();
live(); facts.stream = { state: 'connecting', everLive: true, faults: [] }; tell(); advance(3100); out.reconnect = snap();

// 11. a paused stream (a tab that has just returned, the stream not yet run again) is not live: it paints no "live" before the stream has spoken
F.reset(); live(); tell();
facts.stream = { state: 'down', since: fake.now, lastChunk: fake.now - 2000, faults: [], everLive: true }; tell(); advance(3100);
const w3 = F.writes().length;
facts.stream = { state: 'paused', faults: [], everLive: true }; tell(); advance(100);
out.pausedFromDown = { snap: snap(), liveWrites: F.writes().slice(w3).filter((w) => w.key === 'live').length };
facts.stream = { state: 'connecting', faults: [], everLive: true }; tell(); advance(500); live(); tell();
out.resumedLive = { snap: snap(), last: F.writes()[F.writes().length - 1] };
F.reset(); live(); tell();
facts.stream = { state: 'paused', faults: [], everLive: true }; tell(); advance(1000); facts.stream = { state: 'connecting', faults: [], everLive: true }; tell(); advance(500); live(); tell(); advance(5000);
out.pausedFromLive = { writes: F.writes().length, snap: snap() };

// 12. entering "down" waits the hold whatever state it comes from: a recovery from a failed read into a stream that is still connecting is not "stream down" yet
F.reset(); live(); tell();
facts.boot = Object.assign({}, facts.boot, { failed: 'HTTP 502', failedAt: fake.now }); tell(); advance(3100); out.failedFirst = snap().state;
facts.boot = Object.assign({}, facts.boot, { failed: '', failedAt: 0 }); facts.stream = { state: 'connecting', faults: [], everLive: true }; tell();
out.afterFail0 = snap().state; advance(2900); out.afterFail2900 = snap().state; advance(300); out.afterFail3200 = snap().state;

// 13. a hold pending when the tab hides is dropped; the tab seen again judges afresh and waits a whole hold of its own
F.reset(); live(); tell();
facts.stream = { state: 'down', since: fake.now, lastChunk: fake.now - 2000, faults: [], everLive: true }; tell(); advance(1000);
out.holdArmed = F.armed().hold;
Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange'));
out.holdOnHide = F.armed().hold;
advance(1950);
Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
facts.stream = { state: 'connecting', faults: [], everLive: true }; document.dispatchEvent(new Event('visibilitychange'));
advance(100); out.afterSeen100 = snap().state; advance(2700); out.afterSeen2800 = snap().state; advance(400); out.afterSeen3200 = snap().state;

F.setInputs(null); F.setClock(null); F.setTimers({ setInterval: (f, m) => setInterval(f, m), clearInterval: (t) => clearInterval(t), setTimeout: (f, m) => setTimeout(f, m), clearTimeout: (t) => clearTimeout(t) });
out.probe = 'states';
return JSON.stringify(out); })()
"""


@needs_browser
def test_every_state_from_a_faked_clock_and_faked_facts(tmp_path: Path) -> None:
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, LIVE, SETTLE, {"eval": "(() => {" + STATES}])
    (g,) = _vals(out)
    # first paint and the first leave of "connecting" are silent
    assert g["cold"]["state"] == "connecting" and g["cold"]["words"] == "connecting"
    assert g["firstLive"]["state"] == "live" and g["firstLive"]["words"] == "live" and g["firstLive"]["writes"][-1] == {"key": "live", "announce": False}
    # the fine state: no timer, no interval ever set, not one write
    assert g["fine"]["mutations"] == 0 and g["fine"]["intervals"] == 0 and g["fine"]["timeouts"] == 0 and g["fine"]["writes"] == 0 and g["fine"]["timers"] == 0, g["fine"]
    assert g["fine"]["snap"]["ageHidden"] is True
    # the worse state waits out its hold, then is written and spoken
    assert g["heldAt0"]["state"] == "live" and g["heldAt2900"]["state"] == "live" and g["heldAt2900"]["armed"]["hold"] is True
    d = g["down"]
    assert d["state"] == "down" and d["words"] == "stream down" and d["writes"] == [{"key": "down", "announce": True}], d
    assert d["armed"]["tick"] == 5000 and d["armed"]["hold"] is False and d["timers"] == 1, d             # ONE interval, 5 s steps
    a = g["ages"]
    assert d["age"] == "updated 5s ago" and a["s45"]["age"] == "updated 45s ago", (d, a)
    assert a["s60"]["age"] == "updated 1m ago" and a["s60"]["armed"]["tick"] == 30000 and a["s60"]["timers"] == 1, a   # then 30 s steps, still one timer
    assert a["m2"]["age"] == "updated 2m ago" and a["m2b"]["age"] == "updated 2m ago", a
    assert all(a[k]["words"] == "stream down" for k in a)
    assert g["speakCount"] == 1, g["speakCount"]                                                         # the spoken words were written once across the whole age run
    assert "change stream is down" in g["downTitle"] and "Updated" in g["downTitle"]                                 # the full sentence, with the absolute time, rides in the title
    r = g["recovered"]
    assert r["state"] == "live" and r["words"] == "live" and r["armed"]["tick"] == 0 and r["timers"] == 0 and r["ageHidden"] is True, r   # recovery is at once and stops the clock
    assert r["last"] == {"key": "live", "announce": True}                                                  # spoken: the worse state had been
    f = g["flap"]
    assert f["writes"] == 0 and f["mutations"] == 0 and f["snap"]["state"] == "live", f                    # a reconnect flap does not speak
    assert g["stale1"]["state"] == "stale" and g["stale1"]["words"] == "Board stale" and g["stale1"]["age"] == "read 3m ago", g["stale1"]
    assert g["stale2"]["words"] == "Inbox stale +1" and g["stale2"]["age"] == "read 5m ago", g["stale2"]
    assert g["failed"]["state"] == "failed" and g["failed"]["words"] == "can't reach", g["failed"]
    assert g["refused"]["state"] == "refused" and g["refused"]["words"] == "not signed in", g["refused"]
    assert g["recovered2"]["state"] == "live" and g["recovered2"]["timers"] == 0
    assert g["upd0"]["age"] == "" and g["upd1"]["age"] == "updating" and g["upd1"]["state"] == "live" and g["upd2"]["age"] == "", (g["upd0"], g["upd1"], g["upd2"])
    assert g["hidden"]["snap"]["state"] == "live" and g["hidden"]["mutations"] == 0 and g["hidden"]["snap"]["armed"]["tick"] == 0, g["hidden"]   # a hidden tab is frozen
    assert g["seen"]["state"] == "down", g["seen"]                                                         # and repainted when seen
    assert g["slowHello5"]["state"] == "connecting" and g["slowHello9"]["state"] == "down", (g["slowHello5"], g["slowHello9"])   # a first connection waits out the longer hold
    assert g["reconnect"]["state"] == "down", g["reconnect"]                                               # a reconnect after the stream was live waits only the flap hold
    # D(a): a paused stream is never "live": the state the page was in stays until the stream has run again, and no "live" is written meanwhile
    assert g["pausedFromDown"]["snap"]["state"] == "down" and g["pausedFromDown"]["liveWrites"] == 0, g["pausedFromDown"]
    assert g["resumedLive"]["snap"]["state"] == "live" and g["resumedLive"]["last"] == {"key": "live", "announce": True}, g["resumedLive"]
    assert g["pausedFromLive"]["writes"] == 1 and g["pausedFromLive"]["snap"]["state"] == "live", g["pausedFromLive"]     # only the first paint: the return flap writes nothing
    # D(b): entering "down" waits the hold even from a failed read
    assert (g["failedFirst"], g["afterFail0"], g["afterFail2900"], g["afterFail3200"]) == ("failed", "failed", "failed", "down"), g
    # D(c): a pending hold is dropped on hide and starts again on seen
    assert g["holdArmed"] is True and g["holdOnHide"] is False, (g["holdArmed"], g["holdOnHide"])
    assert (g["afterSeen100"], g["afterSeen2800"], g["afterSeen3200"]) == ("live", "live", "down"), g


# the layout: the longest words with a long panel name, at the widths the page has rules for
LAYOUT = r"""(() => { const F = window.LevainFreshLine.seam, el = document.getElementById('freshline');
  const pb = document.getElementById('presencebtn'); pb.hidden = false; pb.querySelector('.pr-chip-t').textContent = 'Follow here';
  const long = (mode) => { const now = Date.now();
    F.setInputs(() => mode === 'live' ? { now, boot: { wholeDrawn: true, lastWholeAt: now }, stream: { state: 'live', lastChunk: now }, held: {}, heads: {}, titleOf: (i) => i }
      : { now, boot: { wholeDrawn: true, lastWholeAt: now - 5000 }, stream: { state: 'live', lastChunk: now, faults: [] }, held: {},
          heads: { a: { status: 'stale', as_of: new Date(now - 9000000).toISOString() }, b: { status: 'stale', as_of: new Date(now - 100).toISOString() } }, titleOf: () => 'Notifications and the long panel name' }); };
  const R = (e) => e.getBoundingClientRect();
  const boxes = () => [...document.querySelectorAll('.masthead .ia-act, .masthead #jumpbtn, .masthead .themesw, .masthead #presencebtn, .masthead .wordmark')].filter((e) => e.getClientRects().length > 0);
  const measure = (label) => { const l = R(el), m = R(document.querySelector('.masthead')), st = el.querySelector('.fl-state'), ag = el.querySelector('.fl-age');
    const overlap = boxes().filter((b) => { const r = R(b); return l.left < r.right - 0.5 && r.left < l.right - 0.5 && l.top < r.bottom - 0.5 && r.top < l.bottom - 0.5; }).map((b) => b.id || b.className);
    const lh = parseFloat(getComputedStyle(el).lineHeight) || parseFloat(getComputedStyle(el).fontSize) * 1.4;
    return { label, w: innerWidth, text: st.textContent + ' | ' + ag.textContent, inside: l.left >= m.left - 0.5 && l.right <= m.right + 0.5 && l.top >= m.top - 0.5 && l.bottom <= m.bottom + 0.5,
      overlap, oneLine: l.height <= lh * 1.5, stCut: st.scrollWidth > st.clientWidth, mastOver: document.querySelector('.masthead').scrollWidth > document.querySelector('.masthead').clientWidth,
      docOver: document.documentElement.scrollWidth > document.documentElement.clientWidth, line: [Math.round(l.left), Math.round(l.top), Math.round(l.right), Math.round(l.bottom)], shown: l.width > 0 && l.height > 0 }; };
  const res = []; long('live'); F.reset(); F.recompute(); res.push(measure('live'));
  long('long'); F.reset(); F.recompute();
  return new Promise((r) => setTimeout(r, 3300)).then(() => { F.recompute(); res.push(measure('long'));
    F.setInputs(null); return JSON.stringify({ probe: 'layout', rows: res }); }); })()"""

@needs_browser
def test_the_line_fits_the_masthead_at_every_width_and_never_wraps(tmp_path: Path) -> None:
    for touch, widths in ((True, (390, 481, 720)), (False, (1100, 1440))):
        steps: list = [{"emulate": {"w": widths[0], "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}] + ([{"touch": True}] if touch else []) + [READY, LIVE, SETTLE]
        for w in widths:
            steps += [{"emulate": {"w": w, "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}, SETTLE, {"eval": LAYOUT}]
        (tmp_path / str(touch)).mkdir()
        for g in _vals(_open(tmp_path / str(touch), steps, touch=touch)):
            for row in g["rows"]:
                where = (row["w"], row["label"], row)
                assert row["shown"] and row["inside"] and row["overlap"] == [], where     # in the masthead, over no control
                assert row["oneLine"], where                                              # words and age stay one line (an ellipsis, never a wrap)
                assert not row["mastOver"] and not row["docOver"], where                  # no horizontal scroll


# The trouble states at the widths between the phone and the desktop: the masthead is pinned there, so its height must not move when a
# state appears, and the state words (never the age) are what must always fit. Names are the page's real ones, not a stress string.
BANDS = r"""((presence) => { const F = window.LevainFreshLine.seam, el = document.getElementById('freshline'), m = document.querySelector('.masthead');
  const pb = document.getElementById('presencebtn'); pb.hidden = !presence; pb.querySelector('.pr-chip-t').textContent = 'Follow here';
  const now = Date.now(), R = (e) => e.getBoundingClientRect();
  const base = { now, boot: { wholeDrawn: true, lastWholeAt: now - 5000 }, stream: { state: 'live', lastChunk: now, faults: [], everLive: true }, held: {}, heads: {}, titleOf: (id) => id === 'p1' ? 'Inbox' : 'Work board' };
  const stale = (n) => { const h = {}; for (let i = 0; i < n; i++) h['p' + i] = { status: 'stale', as_of: new Date(now - 180000 - i * 60000).toISOString() }; return h; };
  const states = { live: base,
    down: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 60000 }, stream: { state: 'down', lastChunk: now - 40000, faults: [], everLive: true } }),
    failed: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 125000, failed: 'HTTP 502', failedAt: now - 1000 } }),
    refused: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 5000, refused: true } }),
    stale1: Object.assign({}, base, { heads: stale(1) }),
    stale2: Object.assign({}, base, { heads: stale(2) }),
    updating: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 5000, inflight: true, inflightAt: now - 3000 } }) };
  const rows = [];
  for (const k of Object.keys(states)) { F.setInputs(() => states[k]); F.reset(); F.recompute();
    const st = el.querySelector('.fl-state'), ag = el.querySelector('.fl-age'), l = R(el);
    rows.push({ k, w: innerWidth, h: Math.round(R(m).height * 10) / 10, stCut: st.scrollWidth > st.clientWidth, ageCut: !ag.hidden && ag.scrollWidth > ag.clientWidth,
      words: st.textContent, age: ag.textContent, line: [Math.round(l.left), Math.round(l.right)], oneLine: l.height <= (parseFloat(getComputedStyle(el).fontSize) || 12) * 2 }); }
  F.setInputs(null);
  return JSON.stringify({ probe: 'bands', presence, rows }); })"""


def _bands(tmp_path: Path, widths: tuple, touch: bool) -> list[dict]:
    """Every trouble state at each width, with the presence chip shown and with it hidden (the chip is what takes the row's room)."""
    steps: list = [{"emulate": {"w": widths[0], "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}] + ([{"touch": True}] if touch else []) + [READY, LIVE, SETTLE]
    for w in widths:
        for presence in ("true", "false"):
            steps += [{"emulate": {"w": w, "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}, SETTLE, {"eval": BANDS + "(" + presence + ")"}]
    (tmp_path / ("touch" if touch else "mouse")).mkdir()
    return [g for g in _vals(_open(tmp_path / ("touch" if touch else "mouse"), steps, touch=touch)) if g.get("probe") == "bands"]


@needs_browser
def test_the_masthead_keeps_its_height_when_a_trouble_state_appears_at_tablet_widths(tmp_path: Path) -> None:
    """The pinned masthead is the same height in every state at 721 / 768 / 790 / 820 (iPad portrait and a narrow desktop window)."""
    for touch in (True, False):
        for g in _bands(tmp_path, (721, 768, 790, 820), touch):
            heights = {r["k"]: r["h"] for r in g["rows"]}
            assert max(heights.values()) - min(heights.values()) <= 1, (g["rows"][0]["w"], touch, g["presence"], heights)


@needs_browser
def test_the_state_words_never_clip_between_the_phone_and_the_tablet(tmp_path: Path) -> None:
    """At 481 / 540 / 600 / 640 (a folded or split screen) the age may be cut; the state words never are, in every trouble state."""
    for touch in (True, False):
        for g in _bands(tmp_path, (481, 540, 600, 640), touch):
            for r in g["rows"]:
                assert not r["stCut"], (r["w"], touch, g["presence"], r)
                assert r["oneLine"], (r["w"], touch, g["presence"], r)


@needs_browser
def test_the_phone_keeps_the_age_number_and_the_words_at_390(tmp_path: Path) -> None:
    """At 390 the age is the short form and survives beside the words in every state that carries one (a trouble state now has its own row: see below)."""
    g = [g for g in _bands(tmp_path, (390,), True) if g["presence"]][0]
    rows = {r["k"]: r for r in g["rows"]}
    for k in ("down", "failed", "stale1", "stale2"):
        assert not rows[k]["stCut"] and not rows[k]["ageCut"] and rows[k]["age"], (k, rows[k])
    assert rows["down"]["words"] == "stream down" and rows["down"]["age"] == "40s", rows["down"]
    assert rows["stale1"]["words"] == "Work board stale" and rows["stale1"]["age"] == "3m", rows["stale1"]
    assert rows["stale2"]["words"] == "Inbox stale +1" and rows["stale2"]["age"] == "4m", rows["stale2"]
    assert rows["failed"]["age"] == "2m" and rows["refused"]["age"] == "", rows
    assert all(not r["stCut"] for r in g["rows"]), g["rows"]
    heights = {r["k"]: r["h"] for r in g["rows"]}                                  # the live state's 185 px is held in the round-6 test; a trouble state adds one row
    assert heights["live"] == 185 and heights["updating"] == 185, heights


# levain#159 round 6: on a phone the line's words were cut with an ellipsis exactly when they mattered ("Work board unavaila..."), and a phone has
# no hover for the title. A state that names a panel takes its own row after the buttons' row, whole; the live state keeps the shared row (a live read that says "updating" wraps to its own row at the narrowest phone widths).
# Names and ages are the page's real ones. Measured at the true device width (mobile + touch), the presence chip shown and hidden.
PHONE = r"""((presence) => { const F = window.LevainFreshLine.seam, el = document.getElementById('freshline'), m = document.querySelector('.masthead');
  const pb = document.getElementById('presencebtn'); pb.hidden = !presence; pb.querySelector('.pr-chip-t').textContent = 'Follow here';
  const now = Date.now(), R = (e) => e.getBoundingClientRect();
  const base = { now, boot: { wholeDrawn: true, lastWholeAt: now - 5000 }, stream: { state: 'live', lastChunk: now, faults: [], everLive: true }, held: {}, heads: {}, titleOf: () => 'Work board' };
  const ago = (ms) => new Date(now - ms).toISOString();
  const states = { live: base,
    unavailable2: Object.assign({}, base, { heads: { p0: { status: 'error', as_of: ago(240000) }, p9: { status: 'stale', as_of: ago(180000) } } }),
    stale2: Object.assign({}, base, { heads: { p0: { status: 'stale', as_of: ago(180000) }, p9: { status: 'stale', as_of: ago(240000) } } }),
    stale1: Object.assign({}, base, { heads: { p0: { status: 'stale', as_of: ago(180000) } } }),
    down: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 60000 }, stream: { state: 'down', lastChunk: now - 40000, faults: [], everLive: true } }),
    failed: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 125000, failed: 'HTTP 502', failedAt: now - 1000 } }),
    refused: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 5000, refused: true } }),
    updating: Object.assign({}, base, { boot: { wholeDrawn: true, lastWholeAt: now - 5000, inflight: true, inflightAt: now - 3000 } }),
    connecting: Object.assign({}, base, { boot: { wholeDrawn: false, lastWholeAt: 0 } }) };
  const rows = [];
  for (const k of Object.keys(states)) { F.setInputs(() => states[k]); F.reset(); F.recompute();
    const st = el.querySelector('.fl-state'), ag = el.querySelector('.fl-age'), l = R(el);
    const btns = [...m.querySelectorAll('#jumpbtn, .ia-act, .themesw')].filter((e) => e.getClientRects().length > 0), rowBottom = Math.max(...btns.map((e) => R(e).bottom));
    rows.push({ k, w: innerWidth, state: el.dataset.state, h: Math.round(R(m).height * 10) / 10, words: st.textContent, age: ag.textContent,
      stCut: st.scrollWidth > st.clientWidth, ageCut: !ag.hidden && ag.scrollWidth > ag.clientWidth, ageShown: !ag.hidden && ag.getClientRects().length > 0,
      ownRow: l.top >= rowBottom - 0.5, sharesRow: l.top < rowBottom - 0.5, left: Math.round(l.left), brandLeft: Math.round(R(m.querySelector('.brand')).left), right: Math.round(l.right), top: Math.round(l.top), bottom: Math.round(l.bottom),
      ageRight: Math.round(R(ag).right * 10) / 10, lineRight: Math.round(l.right * 10) / 10, vw: document.documentElement.clientWidth,
      docOver: document.documentElement.scrollWidth > document.documentElement.clientWidth, mastOver: m.scrollWidth > m.clientWidth,
      presenceBelow: !presence || R(pb).top >= l.bottom - 0.5 }); }
  F.setInputs(null);
  return JSON.stringify({ probe: 'phone', presence, rows }); })"""


def _phone(tmp_path: Path, widths: tuple, touch: bool = True) -> list[dict]:
    steps: list = [{"emulate": {"w": widths[0], "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}] + ([{"touch": True}] if touch else []) + [READY, LIVE, SETTLE]
    for w in widths:
        for presence in ("true", "false"):
            steps += [{"emulate": {"w": w, "h": 900, "dpr": 2 if touch else 1, "mobile": touch}}, SETTLE, {"eval": PHONE + "(" + presence + ")"}]
    (tmp_path / ("touch" if touch else "mouse")).mkdir()
    return [g for g in _vals(_open(tmp_path / ("touch" if touch else "mouse"), steps, touch=touch)) if g.get("probe") == "phone"]


@needs_browser
def test_a_panel_trouble_state_is_whole_on_its_own_row_on_a_phone(tmp_path: Path) -> None:
    """'Work board unavailable +1' and 'Work board stale +1' with their ages: nothing cut, a row of their own, no sideways scroll, at 390 / 360 / 320."""
    for g in _phone(tmp_path, (390, 360, 320)):
        rows = {r["k"]: r for r in g["rows"]}
        for k in ("unavailable2", "stale2", "stale1", "down", "failed", "refused"):
            r = rows[k]
            where = (r["w"], g["presence"], r)
            assert r["state"] == ("stale" if k.startswith("stale") or k.startswith("unavail") else k), where
            assert not r["stCut"] and not r["ageCut"], where                       # nothing truncated: the words and the age are read whole
            assert r["ownRow"] and not r["sharesRow"], where                        # its own row, below the buttons'
            assert not r["docOver"] and not r["mastOver"], where
            assert r["presenceBelow"], where                                        # the presence chip follows it
            assert r["left"] <= r["brandLeft"] + 1, where                           # left-aligned: it starts where the brand does, not at the right edge
            assert r["ageShown"] == (k != "refused"), where                         # "not signed in" carries no age; every other trouble state does
        assert rows["unavailable2"]["words"] == "Work board unavailable +1" and rows["stale2"]["words"] == "Work board stale +1", rows
        for k in ("live", "updating"):                                              # live keeps the buttons' row, and so does "updating" except at the narrowest widths
            r = rows[k]
            # the one exception (levain#169): at the narrowest phone width the "updating" cue does not fit beside the buttons, and an age is never cut, so it wraps to its own row
            assert r["sharesRow"] == (not (k == "updating" and r["w"] == 320)) and not r["stCut"] and not r["docOver"] and not r["mastOver"], (r["w"], g["presence"], r)


@needs_browser
def test_the_live_line_keeps_the_buttons_row_and_the_phone_masthead_height_at_390(tmp_path: Path) -> None:
    """The accepted look at 390, as the tip had it: live (and "updating", which fits here) shares the buttons' row and leaves the masthead 185 px (presence chip shown)."""
    g = [g for g in _phone(tmp_path, (390,)) if g["presence"]][0]
    rows = {r["k"]: r for r in g["rows"]}
    assert rows["live"]["sharesRow"] and rows["live"]["h"] == 185, rows["live"]
    assert rows["updating"]["sharesRow"] and rows["updating"]["h"] == 185, rows["updating"]


@needs_browser
def test_a_trouble_state_adds_one_row_to_the_phone_masthead_at_390(tmp_path: Path) -> None:
    """The accepted cost: the phone masthead (not pinned) grows when a trouble state arrives, by the line's row, and no more."""
    g = [g for g in _phone(tmp_path, (390,)) if g["presence"]][0]
    rows = {r["k"]: r for r in g["rows"]}
    for k in ("unavailable2", "stale2", "stale1", "down", "failed", "refused"):
        assert rows[k]["ownRow"] and rows[k]["h"] > rows["live"]["h"], rows[k]
        assert rows[k]["h"] - rows["live"]["h"] < 60, rows[k]


@needs_browser
def test_a_trouble_state_moves_nothing_at_768_and_1440(tmp_path: Path) -> None:
    """The pinned masthead (721 and up) is the same height whatever the line says; the phone's own-row rule is not in force there."""
    for touch, widths in ((True, (768,)), (False, (1440,))):
        for g in _phone(tmp_path, widths, touch):
            heights = {r["k"]: r["h"] for r in g["rows"]}
            assert max(heights.values()) - min(heights.values()) <= 1, (g["rows"][0]["w"], g["presence"], heights)


@needs_browser
def test_the_live_and_updating_line_shows_its_age_whole_on_a_phone_at_every_width(tmp_path: Path) -> None:
    """levain#169: at 320 the "live . updating" line was cut to "live . up..." (the age's ellipsis); the age is the cue, so it is never cut: the line
    wraps to its own row where the buttons' row has no room for it, and a live line with nothing to say still shares the row."""
    for g in _phone(tmp_path, (390, 360, 320)):
        rows = {x["k"]: x for x in g["rows"]}
        for k in ("live", "updating"):
            r = rows[k]
            where = (r["w"], g["presence"], r)
            assert not r["ageCut"], where                                            # the age's text is not cut (scrollWidth within clientWidth)
            assert r["ageRight"] <= r["lineRight"] + 0.5 and r["ageRight"] <= r["vw"], where   # inside its container and the screen
            assert not r["docOver"] and not r["mastOver"], where                     # no sideways scroll
            assert r["ageShown"] == (k == "updating"), where
        # the first "connecting" has no age to protect, so it shares the buttons' row as it always did (no jump when the page then goes live)
        c = rows["connecting"]
        assert c["state"] == "connecting" and c["sharesRow"] and not c["docOver"] and not c["mastOver"], (c["w"], g["presence"], c)
