"""levain#159: the freshness line's pure half, run in node. freshFacts() builds the one facts object the line paints from (v1: the shim's
and the stream's facts plus the manifest heads); toMs() reads a manifest timestamp whether it is an ISO string (today) or epoch seconds
(a later server); decide() turns facts and a clock into the state, the words and the age. Nothing here touches a DOM."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const L = require(process.argv[2]);
const out = {};
const NOW = Date.parse("2026-11-19T12:00:00Z");
const base = { wholeDrawn: true, lastWholeAt: NOW - 60000, inflight: false, inflightAt: 0, refused: false, failed: "", failedAt: 0 };
const live = { state: "live", lastChunk: NOW - 2000, faults: [] };
const facts = (o) => L.freshFacts(Object.assign({ now: NOW, boot: base, stream: live, held: {}, heads: {}, titleOf: (id) => id.toUpperCase() }, o));
const key = (o) => L.decide(facts(o), NOW).key;

// toMs: ISO, epoch seconds, epoch ms, and what is neither
out.toMs = [L.toMs("2026-11-19T12:00:00Z"), L.toMs("2026-11-19T12:00:00+00:00"), L.toMs(1795089600), L.toMs(1795089600000), L.toMs("1795089600"), L.toMs("nope"), L.toMs(null), L.toMs(0), L.toMs(NaN), L.toMs(undefined)];
out.nowMs = NOW;

// freshFacts v1: the shape the painter reads, and `worker` null
const f = facts({ heads: { board: { status: "stale", as_of: "2026-11-19T11:57:00Z" }, inbox: { status: "ok", as_of: "2026-11-19T11:59:00Z" } } });
out.shape = { keys: Object.keys(f).sort(), worker: f.worker, readKeys: Object.keys(f.read).sort(), streamKeys: Object.keys(f.stream).sort(), panelKeys: Object.keys(f.panels[0]).sort() };
out.panels = f.panels.map((p) => [p.id, p.title, p.asOfMs, p.stale, p.pending, p.kind]);
// confirmedAt is the newest of the whole draw, a panel's applied refetch, a good poll and the last bytes
out.confirm = [
  facts({}).confirmedAt - NOW,
  facts({ held: { a: { etag: "1", at: NOW - 500 } } }).confirmedAt - NOW,
  facts({ stream: { state: "down", lastChunk: 0, pollOkAt: NOW - 9000 } }).confirmedAt - NOW,
  facts({ boot: Object.assign({}, base, { lastWholeAt: 0 }), stream: { state: "connecting" } }).confirmedAt,
];
// lastReadAt never takes the heartbeat
out.lastRead = [facts({}).lastReadAt - NOW, facts({ held: { a: { at: NOW - 100 } } }).lastReadAt - NOW];

// the state, first match wins
out.keys = {
  live: key({}),
  connecting: key({ boot: Object.assign({}, base, { wholeDrawn: false, lastWholeAt: 0 }) }),
  refused: key({ boot: Object.assign({}, base, { refused: true, wholeDrawn: false }) }),
  refusedBeatsFailed: key({ boot: Object.assign({}, base, { refused: true, failed: "x", failedAt: NOW }) }),
  failed: key({ boot: Object.assign({}, base, { failed: "HTTP 502", failedAt: NOW }) }),
  failedColdBeatsConnecting: key({ boot: Object.assign({}, base, { wholeDrawn: false, lastWholeAt: 0, failed: "x", failedAt: NOW }) }),
  downState: key({ stream: { state: "down", since: NOW - 4000, lastChunk: NOW - 4000 } }),
  downConnecting: key({ stream: { state: "connecting" } }),
  paused: key({ stream: { state: "paused", everLive: true } }),
  noStreamScript: key({ stream: {} }),
  stale: key({ heads: { board: { status: "stale", as_of: "2026-11-19T11:57:00Z" } } }),
  fault: key({ stream: Object.assign({}, live, { faults: ["inbox"] }) }),
  downBeatsStale: key({ stream: { state: "down", lastChunk: NOW - 4000 }, heads: { board: { status: "stale" } } }),
  failedBeatsDown: key({ boot: Object.assign({}, base, { failed: "x", failedAt: NOW }), stream: { state: "down", lastChunk: NOW - 4000 } }),
  unreadIsNotStale: key({ heads: { board: { status: "unread", as_of: null } } }),
  errorHead: key({ heads: { board: { status: "error", as_of: "2026-11-19T11:57:00Z" } } }),
  // a page that never starts a change stream has no stream words: the stream facts say so with a null state
  noStreamConfigured: key({ stream: { state: null } }),
};

// the words and ages, from a fake clock
const d = (o, at) => L.decide(facts(o), at === undefined ? NOW : at);
const down = { stream: { state: "down", since: NOW - 40000, lastChunk: NOW - 40000 } };
out.down = [d(down).words, d(down).age, d(down, NOW + 5000).age, d(down, NOW + 20000).age, d(down, NOW + 80000).age, d(down, NOW + 3 * 3600000).age];
const staleH = { heads: { board: { status: "stale", as_of: new Date(NOW - 180000).toISOString() }, inbox: { status: "stale", as_of: new Date(NOW - 300000).toISOString() } } };
const st = d(staleH);
out.stale = [st.words, st.age, d({ heads: { board: staleH.heads.board } }).words];
out.updating = [d({ boot: Object.assign({}, base, { inflight: true, inflightAt: NOW - 500 }) }).age, d({ boot: Object.assign({}, base, { inflight: true, inflightAt: NOW - 1500 }) }).age,
  d({ boot: Object.assign({}, base, { wholeDrawn: false, inflight: true, inflightAt: NOW - 5000 }) }).age];
out.liveAge = d({}).age;
out.titleHasClock = /Last read/.test(d({}).title);
out.faultAge = d({ stream: Object.assign({}, live, { faults: ["inbox"] }), held: { inbox: { at: NOW - 70000 } } }).age;

// an error head is named like a stale one, in its own words
const errH = d({ heads: { board: { status: "error", as_of: new Date(NOW - 180000).toISOString() } } });
const mixH = d({ heads: { board: { status: "error", as_of: new Date(NOW - 180000).toISOString() }, inbox: { status: "stale", as_of: new Date(NOW - 300000).toISOString() } } });
out.error = [errH.words, errH.age, errH.title, mixH.words, mixH.title];

// the phone's forms keep the number: no words around it
const short = (o, at) => L.decide(facts(o), at === undefined ? NOW : at, { short: true });
out.short = [short(down).words, short(down).age, short(down, NOW + 5000).age, short(staleH).words, short(staleH).age,
  short({ boot: Object.assign({}, base, { failed: "x", failedAt: NOW - 1000 }) }, NOW + 1000).age,
  short({ stream: { state: "down", since: NOW, lastChunk: NOW - 1000 } }, NOW).age];

// a head's read time is the server's clock: the age is measured against the server's now (a phone clock 10 minutes ahead does not age it)
const skew = (offset) => d({ serverOffsetMs: offset, heads: { board: { status: "stale", as_of: new Date(NOW - 180000).toISOString() } } }, NOW + 600000).age;
out.skew = [skew(-600000), skew(0)];

// a failed read ages from the last READ: heartbeats on a live stream do not move it
const failedLive = { boot: Object.assign({}, base, { lastWholeAt: NOW - 120000, failed: "HTTP 502", failedAt: NOW - 1000 }), stream: Object.assign({}, live, { lastChunk: NOW - 500 }) };
out.failedAge = [d(failedLive).words, d(failedLive).age, d(failedLive, NOW + 60000).age];

// the first connection of a stream that was never live waits longer than a reconnect before it speaks
const holdOf = (st) => d({ stream: st }).holdMs;
out.hold = [holdOf({ state: "connecting" }), holdOf({ state: "connecting", everLive: true }), holdOf({ state: "down" }), L.HOLD_MS, L.HOLD_FIRST_MS];
// a total order over the stale panels: those without a read time sort last, in the order given, whichever way they arrive
const noRead = (ids) => { const h = {}; ids.forEach((i) => { h[i] = { status: "stale" }; }); return h; };
out.order = [d({ heads: noRead(["a", "b"]) }).words, d({ heads: noRead(["b", "a"]) }).words,
  d({ heads: Object.assign(noRead(["a", "b"]), { c: { status: "stale", as_of: new Date(NOW - 60000).toISOString() } }) }).words];
// a panel the page could not read at all (the whole draw held no etag for it) is "unavailable"; one it read once is "stale"
const unread = d({ stream: Object.assign({}, live, { faults: ["inbox"] }), held: { inbox: { etag: null } } });
const reread = d({ stream: Object.assign({}, live, { faults: ["inbox"] }), held: { inbox: { etag: "7", at: NOW - 70000 } } });
out.unread = [unread.words, unread.age, reread.words];
console.log(JSON.stringify(out));
"""


def _run(tmp_path: Path) -> dict:
    drv = tmp_path / "fresh_facts_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "fresh_line.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_to_ms_reads_an_iso_string_and_epoch_seconds(tmp_path: Path) -> None:
    got = _run(tmp_path)
    now = got["nowMs"]
    iso, iso_off, secs, ms, sec_str, nope, nul, zero, nan, undef = got["toMs"]
    assert iso == now and iso_off == now          # an ISO string, with Z or an offset
    assert secs == now and ms == now and sec_str == now     # epoch seconds, epoch milliseconds, a numeric string
    assert nope is None and nul is None and zero is None and nan is None and undef is None


def test_fresh_facts_has_the_interface_shape_and_v1_fills_it_from_the_page(tmp_path: Path) -> None:
    got = _run(tmp_path)
    assert got["shape"]["keys"] == ["confirmedAt", "lastReadAt", "panels", "read", "serverOffsetMs", "stream", "worker"]
    assert got["shape"]["worker"] is None                                      # v1: the worker is not known to the page
    assert got["shape"]["readKeys"] == ["inflightAt", "state", "why"] and got["shape"]["streamKeys"] == ["everLive", "state"]
    assert got["shape"]["panelKeys"] == ["asOfMs", "id", "kind", "onServerClock", "pending", "stale", "title"]
    now = got["nowMs"]
    assert got["panels"] == [["board", "BOARD", now - 180000, True, False, "stale"], ["inbox", "inbox", now - 60000, False, False, ""]]   # a stale panel is titled, a quiet one is not looked up
    assert got["confirm"] == [-2000, -500, -9000, None]
    assert got["lastRead"] == [-60000, -100]


def test_the_state_is_the_first_match_of_the_table(tmp_path: Path) -> None:
    k = _run(tmp_path)["keys"]
    assert k["live"] == "live" and k["connecting"] == "connecting"
    assert k["refused"] == "refused" and k["refusedBeatsFailed"] == "refused" and k["failed"] == "failed"
    assert k["failedColdBeatsConnecting"] == "failed"              # a refusal or a failure is said before the board has ever been drawn
    assert k["downState"] == "down" and k["downConnecting"] == "down"
    assert k["paused"] == "down" and k["noStreamScript"] == "live"  # a paused stream is not live (the hold keeps the page's words until it has run again); a page without the script says nothing
    assert k["stale"] == "stale" and k["fault"] == "stale"          # a stale head, or a panel the page could not read
    assert k["downBeatsStale"] == "down" and k["failedBeatsDown"] == "failed"
    assert k["unreadIsNotStale"] == "live"
    assert k["errorHead"] == "stale"                                # a head that says error is not "live": it is named like a stale panel
    assert k["noStreamConfigured"] == "live"                        # a page that never runs a stream says nothing about one


def test_the_age_steps_by_five_seconds_then_minutes_and_only_trouble_states_carry_it(tmp_path: Path) -> None:
    got = _run(tmp_path)
    assert got["down"][0] == "stream down"
    assert got["down"][1:] == ["updated 40s ago", "updated 45s ago", "updated 1m ago", "updated 2m ago", "updated 3h ago"], got["down"]
    assert got["stale"] == ["INBOX stale +1", "read 5m ago", "BOARD stale"]    # the oldest panel is named, with its age
    assert got["liveAge"] == "" and got["titleHasClock"] is True    # a quiet board carries no age; the absolute time rides in the title
    assert got["faultAge"] == "read 1m ago"                         # a panel the page could not read ages from its own last read
    assert got["updating"] == ["", "updating", ""]                  # a read shorter than a second shows no cue, and the first read says "connecting" already


def test_an_error_head_is_named_in_its_own_words_and_the_two_kinds_share_one_line(tmp_path: Path) -> None:
    words, age, title, mixed_words, mixed_title = _run(tmp_path)["error"]
    assert words == "BOARD unavailable" and age == "read 3m ago", (words, age)
    assert title.startswith("A panel could not be read: BOARD."), title
    assert mixed_words == "INBOX stale +1"                                     # the oldest is named, whatever its kind
    assert mixed_title.startswith("A panel's data is old: INBOX. A panel could not be read: BOARD."), mixed_title


def test_the_phone_forms_keep_the_age_number_without_its_words(tmp_path: Path) -> None:
    got = _run(tmp_path)["short"]
    assert got == ["stream down", "40s", "45s", "INBOX stale +1", "5m", "1m", "<5s"], got


def test_a_stale_head_ages_on_the_servers_clock(tmp_path: Path) -> None:
    ahead, plain = _run(tmp_path)["skew"]
    assert ahead == "read 3m ago" and plain == "read 13m ago", (ahead, plain)   # a phone clock 10 minutes fast, told by the offset, does not age the panel


def test_a_failed_read_ages_from_the_last_read_never_from_heartbeats(tmp_path: Path) -> None:
    got = _run(tmp_path)["failedAge"]
    assert got == ["can't reach", "updated 2m ago", "updated 3m ago"], got


def test_the_first_connection_of_a_stream_waits_longer_than_a_reconnect(tmp_path: Path) -> None:
    first, reconnect, down, hold, hold_first = _run(tmp_path)["hold"]
    assert first == hold_first and reconnect == hold and down == hold and hold_first > hold


def test_stale_panels_without_a_read_time_have_a_total_order(tmp_path: Path) -> None:
    a_first, b_first, with_time = _run(tmp_path)["order"]
    assert a_first == "A stale +1" and b_first == "B stale +1", (a_first, b_first)   # no NaN order: the given order stands
    assert with_time == "C stale +2", with_time                                       # the one with a read time is the oldest known


def test_a_panel_the_page_never_read_is_unavailable_and_one_it_read_once_is_stale(tmp_path: Path) -> None:
    unread, age, reread = _run(tmp_path)["unread"]
    assert unread == "INBOX unavailable" and reread == "INBOX stale", (unread, reread)
