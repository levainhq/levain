"""The page's per-load reads (band, board, ...) on a gate of their own, and the 503 they used to answer at once (levain#157).

The defect: ``/cockpit/band.json`` and its siblings took ``request_gate.acquire(blocking=False)`` and answered an
instant plain-text ``busy`` 503 when no slot was free. ``request_gate`` has ``_MAX_INFLIGHT`` slots; a page-load burst
of panel reads holds up to ``_MAX_READ_INFLIGHT`` of them, so the instant routes share the last two and lose at once
(an iPad showed "HTTP 503" on band items, and nothing logged the refusal).

The contract these tests pin: a page read takes a slot of ``page_gate`` (never ``request_gate``) and WAITS up to
``_PAGE_READ_WAIT_SECONDS`` behind at most ``_MAX_PAGE_WAITERS`` others; a refusal is 503 ``busy`` with ``Retry-After: 2``
and ONE bounded warning line per (route key, gate) per second, ``refused 503 route=<key> gate=<gate> n=<count>``.

Tests 1 and 9 touch no new name: they are red on the old code by the 503 itself. The others reach new names.
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from levain import band_view, web_server
from levain.consent.questions import QuestionStore
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_pairing_http import PROXY, _call, _pair
from tests.test_cockpit_pairing_http import _serve as _pair_serve

H = "X-Levain-Write-Token"
OPTS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]
LOG = web_server.__name__


@pytest.fixture
def lh(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    return tmp_path / "lh" / "consent"


@pytest.fixture
def stats(monkeypatch):
    """A fresh route counter, so a count is this test's and nothing else's."""
    fresh = web_server._RouteStats()
    monkeypatch.setattr(web_server, "_ROUTE_STATS", fresh)
    return fresh


@pytest.fixture
def band_rig(tmp_path: Path, lh: Path):
    """A serve with a question store holding one open question (the band has an item) and a write token."""
    _root, src, _ids = _install(tmp_path)
    QuestionStore(lh).file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix?", options=OPTS,
                           rec="hold", blocks="the sweep", cwd="/w")
    with _serve(src, write_token=TOKEN, install_class="reported") as (base, httpd):
        yield base, httpd


def _http(base: str, path: str, token: "str | None" = TOKEN, timeout: float = 30.0) -> tuple[int, dict, bytes]:
    c = http.client.HTTPConnection(base.removeprefix("http://"), timeout=timeout)
    try:
        c.request("GET", path, headers={H: token} if token else {})
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def _hold(gate, n: int) -> list:
    """Take every slot of ``gate`` (non-blocking); the caller releases them in a finally."""
    got = []
    for _ in range(n):
        assert gate.acquire(blocking=False), "a slot was already taken"
        got.append(1)
    return got


def _release(gate, held: list) -> None:
    for _ in held:
        gate.release()
    held.clear()


def _refusal_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOG and r.getMessage().startswith("refused 503")]


# --- 1. the mechanism: the page's own reads are not starved by a page-load burst --------------------------------------

def test_a_page_read_is_served_while_every_request_and_read_slot_is_held(band_rig) -> None:
    """A page-load panel burst holds the read pool and, with two other instant reads, every request slot. The band is
    still served. (Before: the instant ``request_gate`` 503. Touches no new name.)"""
    base, httpd = band_rig
    req_held = _hold(httpd.request_gate, web_server._MAX_INFLIGHT)
    read_held = _hold(httpd.read_gate, web_server._MAX_READ_INFLIGHT)
    try:
        st, _h, body = _http(base, "/cockpit/band.json")
        assert st == 200, (st, body[:200])
        assert json.loads(body)["items"], body[:200]
        st, _h, body = _http(base, "/cockpit/board.json")
        assert st == 200, (st, body[:200])
    finally:
        _release(httpd.read_gate, read_held)
        _release(httpd.request_gate, req_held)


# --- 2. a page read waits for a slot rather than being refused -------------------------------------------------------

def test_a_page_read_waits_for_a_slot_and_is_served_with_no_refusal_counted(band_rig, stats) -> None:
    base, httpd = band_rig
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    out: list = []
    t = threading.Thread(target=lambda: out.append(_http(base, "/cockpit/band.json")), daemon=True)
    try:
        t.start()
        end = time.monotonic() + 3.0
        while httpd.page_waiters < 1 and time.monotonic() < end:     # it is waiting, not yet served
            time.sleep(0.01)
        assert httpd.page_waiters == 1 and not out
        time.sleep(0.3)
        assert not out, "the read was answered while every slot was held"
        httpd.page_gate.release()
        held.pop()
        t.join(10)
        assert out and out[0][0] == 200, out
    finally:
        _release(httpd.page_gate, held)
        t.join(10)
    assert stats.snapshot().get("band", {}).get("refused_503", 0) == 0     # a served read records no refusal row at all
    assert httpd.page_waiters == 0


# --- 3. a page-gate timeout: 503 busy, Retry-After, one line, one count ----------------------------------------------

def test_a_page_gate_timeout_is_503_busy_with_retry_after_one_log_line_and_one_count(band_rig, stats, monkeypatch,
                                                                                   caplog) -> None:
    base, httpd = band_rig
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.2)
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    try:
        with caplog.at_level(logging.WARNING, logger=LOG):
            st, hdrs, body = _http(base, "/cockpit/band.json")
    finally:
        _release(httpd.page_gate, held)
    assert st == 503 and body == b"busy\n" and hdrs.get("retry-after") == "2", (st, hdrs, body)
    assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_wait n=1"], _refusal_lines(caplog)
    assert stats.snapshot()["band"]["refused_503"] == 1


# --- 4. the waiting room is bounded: a full one is refused at once ---------------------------------------------------

def test_a_full_waiting_room_is_refused_at_once_and_named(band_rig, stats, caplog) -> None:
    base, httpd = band_rig
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    with httpd.page_waiters_lock:
        before = httpd.page_waiters
        httpd.page_waiters = web_server._MAX_PAGE_WAITERS
    try:
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=LOG):
            st, hdrs, body = _http(base, "/cockpit/band.json")
        took = time.monotonic() - t0
    finally:
        with httpd.page_waiters_lock:
            httpd.page_waiters = before
        _release(httpd.page_gate, held)
    assert st == 503 and body == b"busy\n" and hdrs.get("retry-after") == "2", (st, hdrs, body)
    assert took < 1.5, f"refused after {took:.2f}s: the default wait is {web_server._PAGE_READ_WAIT_SECONDS}s"
    assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_waiters_full n=1"], _refusal_lines(caplog)
    assert stats.snapshot()["band"]["refused_503"] == 1


# --- 5. the instrument is bounded and carries nothing of the request -------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_instrument_logs_one_line_per_route_and_gate_per_second_and_counts_the_rest(caplog) -> None:
    clock = _Clock()
    st = web_server._RouteStats(clock=clock)
    with caplog.at_level(logging.WARNING, logger=LOG):
        for _ in range(50):
            st.record_refusal("band", "page_wait")
        assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_wait n=1"]
        clock.t += 1.0
        st.record_refusal("band", "page_wait")
        assert _refusal_lines(caplog)[1:] == ["refused 503 route=band gate=page_wait n=50"]   # the 49 held back + this one
        st.record_refusal("band", "page_waiters_full")                  # another gate: its own line
        st.record_refusal("board", "page_wait")                         # another route: its own line
        assert _refusal_lines(caplog)[2:] == ["refused 503 route=band gate=page_waiters_full n=1",
                                              "refused 503 route=board gate=page_wait n=1"]
    assert st.snapshot()["band"]["refused_503"] == 52 and st.snapshot()["board"]["refused_503"] == 1


def test_the_refusal_line_carries_no_raw_path_query_or_token(caplog) -> None:
    st = web_server._RouteStats(clock=_Clock())
    with caplog.at_level(logging.WARNING, logger=LOG):
        st.record_refusal("/cockpit/band.json?token=SECRET&x=1", "page_wait")
        st.record_refusal("/cockpit/panel/tray.json?q=SECRET", "read_wait")
        st.record_refusal("/cockpit/consent/abc123SECRET.json?token=SECRET", "request_gate")
    lines = _refusal_lines(caplog)
    assert len(lines) == 3, lines
    assert all("SECRET" not in ln and "token" not in ln and "?" not in ln for ln in lines), lines
    assert lines[0] == "refused 503 route=band gate=page_wait n=1"
    assert lines[1] == "refused 503 route=panel/tray gate=read_wait n=1"


# --- 6. a panel read refused at the read gate is logged --------------------------------------------------------------

def test_a_panel_read_refused_at_the_read_gate_is_logged_with_its_route(tmp_path: Path, monkeypatch, stats, caplog) -> None:
    _root, src, _ids = _install(tmp_path)
    monkeypatch.setattr(web_server, "_READ_WAIT_SECONDS", 0.2)
    with _serve(src, write_token=TOKEN, install_class="reported") as (base, httpd):
        held = _hold(httpd.read_gate, web_server._MAX_READ_INFLIGHT)
        try:
            with caplog.at_level(logging.WARNING, logger=LOG):
                st, _h, body = _http(base, "/cockpit/panel/tray.json")
        finally:
            _release(httpd.read_gate, held)
    assert st == 503 and body == b"busy\n"
    assert _refusal_lines(caplog) == ["refused 503 route=panel/tray gate=read_wait n=1"], _refusal_lines(caplog)
    assert stats.snapshot()["panel/tray"]["refused_503"] == 1


# --- 7. a credential refusal is logged -------------------------------------------------------------------------------

def test_a_credential_refusal_is_logged_with_a_credential_gate(tmp_path: Path, stats, caplog) -> None:
    _root, src, _ids = _install(tmp_path)
    with _pair_serve(src) as (port, httpd):
        _dev, tok = _pair(port)
        held = _hold(httpd.pair_verify_gate, web_server._PAIR_VERIFY_SLOTS)
        try:
            with caplog.at_level(logging.WARNING, logger=LOG):
                st, out, _hdrs = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)
        finally:
            _release(httpd.pair_verify_gate, held)
    assert (st, out.get("error")) == (503, "broker_unavailable"), (st, out)
    lines = _refusal_lines(caplog)
    assert len(lines) == 1 and " gate=credential:" in lines[0], lines
    assert "route=manifest" in lines[0] and tok not in lines[0], lines


# --- 8. a page read that gets its slot gives it back -----------------------------------------------------------------

def test_a_served_page_read_leaves_no_slot_held(band_rig) -> None:
    base, httpd = band_rig
    for _ in range(20):
        assert _http(base, "/cockpit/band.json")[0] == 200
    assert _http(base, "/cockpit/board.json")[0] == 200
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)         # every slot is free again
    _release(httpd.page_gate, held)
    assert httpd.page_waiters == 0
    held = _hold(httpd.request_gate, web_server._MAX_INFLIGHT)             # and the page reads never took the request gate
    _release(httpd.request_gate, held)


# --- 9. several pages at once: a real concurrent burst ---------------------------------------------------------------

PAGES, PER_PAGE, SLOW_S, INSTANT_WAVES = 3, 28, 0.3, 6


def test_three_pages_bursting_at_once_do_not_starve_the_band_and_board_reads(tmp_path: Path, lh: Path,
                                                                            monkeypatch) -> None:
    """Three open pages (the iPad's installed app, the iPhone, the laptop) each fire a page load's ~28 panel reads in
    parallel at the one server, while another device keeps reading the band and the board. The gate is server-wide,
    so any page's burst could starve any device's band read: every band and board read still answers 200.

    The overlap: each panel read is held 0.3 s in the read path (the rig's own seam, ``handle_cockpit_get``), the band
    and board reads 50 ms in their store reads, and the fourth thread sends its reads in waves of four at once (two
    devices' band + board), so more than the two request slots a read burst leaves free are wanted together."""
    _root, src, _ids = _install(tmp_path)
    QuestionStore(lh).file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix?", options=OPTS,
                           rec="hold", blocks="the sweep", cwd="/w")
    real_get = web_server.handle_cockpit_get
    real_band, real_board = band_view.band_payload, web_server._board_value

    def slow_get(*a, **kw):
        time.sleep(SLOW_S)
        return real_get(*a, **kw)

    def slow_band(*a, **kw):
        time.sleep(0.05)
        return real_band(*a, **kw)

    def slow_board(*a, **kw):
        time.sleep(0.05)
        return real_board(*a, **kw)

    monkeypatch.setattr(web_server, "handle_cockpit_get", slow_get)
    monkeypatch.setattr(band_view, "band_payload", slow_band)
    monkeypatch.setattr(web_server, "_board_value", slow_board)
    panel_ids = ["decisions", "tray", "loops", "manifest"]
    panel_paths = [f"/cockpit/panel/{p}.json" if p != "manifest" else "/cockpit/manifest.json" for p in panel_ids]
    lock = threading.Lock()
    instant: list[tuple[str, int]] = []
    panels: list[int] = []
    threads: list[threading.Thread] = []
    stop = threading.Event()

    def panel_read(base: str, path: str) -> None:
        try:
            st = _http(base, path, timeout=30)[0]
        except OSError:
            st = -1
        with lock:
            panels.append(st)

    def page(base: str) -> None:
        reads = [threading.Thread(target=panel_read, args=(base, panel_paths[i % len(panel_paths)]), daemon=True)
                 for i in range(PER_PAGE)]
        [r.start() for r in reads]
        [r.join(30) for r in reads]

    def instant_read(base: str, path: str) -> None:
        try:
            st = _http(base, path, timeout=30)[0]
        except OSError:
            st = -1
        with lock:
            instant.append((path, st))

    def instant_waves(base: str) -> None:
        for _ in range(INSTANT_WAVES):
            if stop.is_set():
                return
            wave = [threading.Thread(target=instant_read, args=(base, p), daemon=True)
                    for p in ("/cockpit/band.json", "/cockpit/board.json") * 2]
            [w.start() for w in wave]
            [w.join(30) for w in wave]
            time.sleep(0.2)

    t0 = time.monotonic()
    with _serve(src, write_token=TOKEN, install_class="reported") as (base, _httpd):
        try:
            threads = [threading.Thread(target=page, args=(base,), daemon=True) for _ in range(PAGES)]
            threads.append(threading.Thread(target=instant_waves, args=(base,), daemon=True))
            [t.start() for t in threads]
            for t in threads:
                t.join(25)
        finally:
            stop.set()
            for t in threads:
                t.join(10)
    took = time.monotonic() - t0
    assert not any(t.is_alive() for t in threads), "a burst thread did not finish"
    assert len(instant) == INSTANT_WAVES * 4 and len(panels) == PAGES * PER_PAGE, (len(instant), len(panels))
    bad = sorted({(p, s) for p, s in instant if s != 200})
    assert bad == [], f"band/board reads refused during the burst: {bad} ({sum(1 for _, s in instant if s != 200)} of {len(instant)})"
    assert took < 20.0, took
