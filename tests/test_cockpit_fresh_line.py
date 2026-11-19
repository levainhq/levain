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
    assert 'document.createElement("span")' not in STREAM.split("function ensureChip")[1].split("function held")[0]   # the stream adopts, it does not create a second chip
    assert 'getElementById("freshline")' in STREAM


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
