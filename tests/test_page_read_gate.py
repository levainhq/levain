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
import shutil
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


# === round 1 fixes (L1/L2): EVERY GET read a page load fires waits on the page gate ==================================
# Only writes, voice, notify, the legacy substrate reads and the room's per-tick read keep ``request_gate``'s instant 503.

from tests.test_cockpit_room import TOKEN as ROOM_TOKEN  # noqa: E402
from tests.test_cockpit_room import World, _serve_room  # noqa: E402
from tests.test_cockpit_room import _call as _room_call  # noqa: E402
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401  (fixture)
from tests.test_cockpit_webpush import T as PUSH_TOKEN  # noqa: E402
from tests.test_cockpit_webpush import _signed  # noqa: E402
from levain.consent.notes import NoteStore  # noqa: E402
from tests.test_cockpit_grant_revoke import _signer as _grant_signer  # noqa: E402
from tests.test_cockpit_sign_routes import _call as _sign_call  # noqa: E402
from tests.test_cockpit_sign_routes import _serving  # noqa: E402
from tests.test_grant_phone import RP as GRANT_RP  # noqa: E402
from tests.test_grant_phone import world  # noqa: E402,F401  (fixture)


@pytest.fixture
def w(qdir):  # noqa: F811
    return World(qdir)


@pytest.fixture
def wide_rig(tmp_path: Path, lh: Path):
    """One server with a Signer (so the sign family and the push reads are served), a question and a note on file, and
    the surface token the push rig uses. Yields (base, httpd, question n, note n)."""
    _root, src, _ids = _install(tmp_path)
    q = QuestionStore(lh).file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix?", options=OPTS,
                               rec="hold", blocks="the sweep", cwd="/w")
    n = NoteStore(lh).file(agent="claude_code", agent_session_id="s1", what="Merged the fix", detail="Tip 1a2b3c4", cwd="/w")
    signq = tmp_path / "signq"
    signq.mkdir(mode=0o700)
    with _signed(src, signq) as (port, httpd):
        httpd.host_grants = _FakeHostGrants()       # mounted: the real surface would start a host helper
        yield f"http://127.0.0.1:{port}", httpd, q.n, n.n


class _FakeHostGrants:
    def listing(self, _deadline, release_gate=None):
        return {"sessions": []}


def _wide_paths(qn: int, nn: int) -> "list[str]":
    """The converted routes beyond round 0's six that this rig serves (the grant listing has its own rig below)."""
    return [f"/cockpit/question/{qn}.json", f"/cockpit/note/{nn}.json", "/cockpit/consent/grants.json",
            "/cockpit/consent/proposals.json", "/cockpit/consent/nonesuch.json", "/cockpit/envelope/list.json",
            "/cockpit/hostgrant/list.json", "/cockpit/dispatch.json", "/cockpit/coord/drops.json",
            "/cockpit/desk/drops.json", "/cockpit/sign.json", "/webpush/center", "/webpush/key", "/webpush/presence"]


# --- (a) every request_gate slot held: each converted read is still served ---------------------------------------------

def test_every_get_read_a_page_load_fires_is_served_while_every_request_and_read_slot_is_held(wide_rig) -> None:
    base, httpd, qn, nn = wide_rig
    free = {p: _http(base, p, PUSH_TOKEN)[0] for p in _wide_paths(qn, nn)}
    assert free[f"/cockpit/question/{qn}.json"] == free[f"/cockpit/note/{nn}.json"] == free["/cockpit/consent/grants.json"] \
        == free["/cockpit/consent/proposals.json"] == free["/cockpit/envelope/list.json"] == free["/webpush/center"] == 200
    assert free["/cockpit/consent/nonesuch.json"] == 404 and 503 not in free.values(), free
    req_held = _hold(httpd.request_gate, web_server._MAX_INFLIGHT)
    read_held = _hold(httpd.read_gate, web_server._MAX_READ_INFLIGHT)
    try:
        got = {p: _http(base, p, PUSH_TOKEN)[0] for p in _wide_paths(qn, nn)}
    finally:
        _release(httpd.read_gate, read_held)
        _release(httpd.request_gate, req_held)
    assert got == free, got                       # the same answers with every request and read slot held


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_grant_listing_is_served_under_request_gate_saturation_and_refused_on_page_gate(world, tmp_path: Path,
                                                                                         monkeypatch) -> None:
    """L1 F5 (grant): ``/cockpit/grant/list.json`` is behind a Signer's passkey world; the sign listing and the room are
    pinned in the test below."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.2)
    with _serving(tmp_path / "inst", signer=_grant_signer(world), extra_hosts=[GRANT_RP]) as (url, httpd):
        assert _sign_call(url, "/cockpit/grant/list.json")[0] == 200
        req_held = _hold(httpd.request_gate, web_server._MAX_INFLIGHT)
        try:
            assert _sign_call(url, "/cockpit/grant/list.json")[0] == 200
        finally:
            _release(httpd.request_gate, req_held)
        page_held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
        try:
            assert _sign_call(url, "/cockpit/grant/list.json")[0] == 503
        finally:
            _release(httpd.page_gate, page_held)
        _all_page_slots_free(httpd)


def test_the_sign_listing_is_served_under_request_gate_saturation_and_refused_on_page_gate(wide_rig, monkeypatch) -> None:
    """L1 F5: the sign listing, converted in round 0 but pinned by no test."""
    base, httpd, _qn, _nn = wide_rig
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.2)
    sign_paths = ("/cockpit/sign.json",)
    req_held = _hold(httpd.request_gate, web_server._MAX_INFLIGHT)
    try:
        assert [_http(base, p, PUSH_TOKEN)[0] for p in sign_paths] == [200]
    finally:
        _release(httpd.request_gate, req_held)
    page_held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    try:
        assert [_http(base, p, PUSH_TOKEN)[0] for p in sign_paths] == [503]
    finally:
        _release(httpd.page_gate, page_held)


def test_the_room_is_served_under_request_gate_saturation_and_refused_on_page_gate(tmp_path: Path, w, monkeypatch) -> None:
    """L1 F5: the room, converted in round 0 but pinned by no test."""
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.2)
    with _serve_room(tmp_path, w) as (rbase, rhttpd):
        req_held = _hold(rhttpd.request_gate, web_server._MAX_INFLIGHT)
        try:
            assert _room_call(rbase, "/cockpit/room.json", token=ROOM_TOKEN, method="GET")[0] == 200
        finally:
            _release(rhttpd.request_gate, req_held)
        page_held = _hold(rhttpd.page_gate, web_server._PAGE_READS_INFLIGHT)
        try:
            st, hdrs, body = _http(rbase, "/cockpit/room.json", ROOM_TOKEN)
            assert (st, body, hdrs.get("retry-after")) == (503, b"busy\n", "2")
        finally:
            _release(rhttpd.page_gate, page_held)


def test_a_wide_read_with_every_page_slot_held_waits_then_is_503_busy_named_by_its_template_key(
        wide_rig, stats, monkeypatch, caplog) -> None:
    base, httpd, qn, nn = wide_rig
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.2)
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    try:
        with caplog.at_level(logging.WARNING, logger=LOG):
            outs = [_http(base, p, PUSH_TOKEN) for p in _wide_paths(qn, nn)]
    finally:
        _release(httpd.page_gate, held)
    assert all(st == 503 and body == b"busy\n" and hdrs.get("retry-after") == "2" for st, hdrs, body in outs), outs
    keys = sorted({ln.split(" gate=")[0].removeprefix("refused 503 route=") for ln in _refusal_lines(caplog)})
    assert keys == sorted({"question/<n>", "note/<n>", "consent/standing", "consent/<id>", "envelope", "hostgrant",
                           "dispatch", "coord", "sign", "push"}), keys
    assert len(_refusal_lines(caplog)) >= 10, _refusal_lines(caplog)
    assert "other" not in keys


def test_the_route_keys_of_the_page_reads_are_bounded_templates() -> None:
    key = web_server._RouteStats.key_for
    assert key("/cockpit/envelope/list.json") == "envelope"
    assert key("/cockpit/hostgrant/list.json") == key("/cockpit/hostgrant/revoke") == "hostgrant"
    assert key("/cockpit/dispatch.json") == key("/cockpit/dispatch/dj-" + "a" * 32 + "/sheet.json") == "dispatch"
    assert key("/cockpit/coord/drops.json") == key("/cockpit/desk/drops.json") == key("/cockpit/coord/drop") == "coord"
    assert key("/cockpit/pair/devices.json") == key("/cockpit/pair/offer") == "pair"
    assert key("/webpush/center?id=SECRET") == key("/webpush/key") == key("/webpush/presence") == "push"
    assert key("/cockpit/consent/grants.json") == key("/cockpit/consent/proposals.json") == "consent/standing"
    assert key("/cockpit/consent/cr-123.json") == key("/cockpit/consent/cr-123/sheet.json") == "consent/<id>"
    assert key("/cockpit/desk.json") == "desk"
    assert key("/cockpit/nonesuch.json") == "other"


# --- (b) a page slot comes back after an error and after a timeout ----------------------------------------------------

def _all_page_slots_free(httpd) -> None:
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)      # asserts none leaked
    _release(httpd.page_gate, held)
    assert httpd.page_waiters == 0


def test_a_page_read_whose_store_raises_is_500_and_gives_its_slot_back(wide_rig, monkeypatch) -> None:
    base, httpd, qn, nn = wide_rig

    def boom(*_a, **_k):
        raise RuntimeError("store gone")

    monkeypatch.setattr(QuestionStore, "get", boom)
    monkeypatch.setattr(NoteStore, "get", boom)
    for _ in range(web_server._PAGE_READS_INFLIGHT + 2):      # more than the gate holds: a leak would turn the last ones into 503s
        assert _http(base, f"/cockpit/question/{qn}.json", PUSH_TOKEN)[0] == 500
        assert _http(base, f"/cockpit/note/{nn}.json", PUSH_TOKEN)[0] == 500
    _all_page_slots_free(httpd)


def test_a_band_read_whose_store_raises_is_500_and_gives_its_slot_back(band_rig, monkeypatch) -> None:
    base, httpd = band_rig

    def boom(*_a, **_k):
        raise RuntimeError("store gone")

    monkeypatch.setattr(band_view, "band_payload", boom)
    for _ in range(web_server._PAGE_READS_INFLIGHT + 2):
        assert _http(base, "/cockpit/band.json")[0] == 500
    _all_page_slots_free(httpd)


def test_a_page_gate_timeout_leaves_the_gate_whole(wide_rig, monkeypatch) -> None:
    base, httpd, qn, _nn = wide_rig
    monkeypatch.setattr(web_server, "_PAGE_READ_WAIT_SECONDS", 0.1)
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    try:
        assert _http(base, f"/cockpit/question/{qn}.json", PUSH_TOKEN)[0] == 503
        assert httpd.page_waiters == 0
    finally:
        _release(httpd.page_gate, held)
    _all_page_slots_free(httpd)
    assert _http(base, f"/cockpit/question/{qn}.json", PUSH_TOKEN)[0] == 200


# --- (c) a waiting page read whose client left does not run the store read --------------------------------------------

def test_an_abandoned_waiting_page_read_gives_its_slot_back_and_never_reads_the_store(wide_rig, monkeypatch) -> None:
    import socket

    base, httpd, qn, _nn = wide_rig
    reads: list = []
    real_get = QuestionStore.get

    def spy(self, n):
        reads.append(n)
        return real_get(self, n)

    monkeypatch.setattr(QuestionStore, "get", spy)
    held = _hold(httpd.page_gate, web_server._PAGE_READS_INFLIGHT)
    host, port = base.removeprefix("http://").split(":")
    s = socket.create_connection((host, int(port)), timeout=10)
    try:
        s.sendall(f"GET /cockpit/question/{qn}.json HTTP/1.1\r\nHost: {host}:{port}\r\n{H}: {PUSH_TOKEN}\r\n\r\n".encode())
        end = time.monotonic() + 5.0
        while httpd.page_waiters < 1 and time.monotonic() < end:       # it is waiting for a slot
            time.sleep(0.01)
        assert httpd.page_waiters == 1
        s.close()                                                     # the client gives up
        time.sleep(0.1)
        httpd.page_gate.release()                                     # a slot frees: the waiter takes it, sees nobody, gives it back
        held.pop()
        end = time.monotonic() + 5.0
        while httpd.page_waiters > 0 and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.2)
        assert reads == [], "the store was read for a client that had gone"
        free = _hold(httpd.page_gate, 1)                              # the abandoned wait released its slot
        _release(httpd.page_gate, free)
        assert httpd.page_gate.acquire(blocking=False)
        httpd.page_gate.release()
    finally:
        s.close()
        _release(httpd.page_gate, held)
    _all_page_slots_free(httpd)
    assert _http(base, f"/cockpit/question/{qn}.json", PUSH_TOKEN)[0] == 200 and reads == [qn]


# --- (d) the refusal log: a burst's tail is reported by the next read of ANY route ------------------------------------

def test_a_burst_then_silence_is_reported_by_the_next_cockpit_read_after_the_interval(caplog) -> None:
    clock = _Clock()
    st = web_server._RouteStats(clock=clock)
    with caplog.at_level(logging.INFO, logger=LOG):
        for _ in range(50):
            st.record_refusal("band", "page_wait")
        assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_wait n=1"]
        clock.t += 0.5
        st.record("manifest", 1.0)                                       # not due yet: nothing logged
        assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_wait n=1"]
        clock.t += 0.6
        st.record("/cockpit/panel/tray.json", 1.0)                       # any read, any route, after the interval
        assert _refusal_lines(caplog)[1:] == ["refused 503 route=band gate=page_wait n=49"]
        clock.t += 5.0
        st.record("manifest", 1.0)
        st.record_refusal("board", "page_wait")
        assert len(_refusal_lines(caplog)) == 3                          # no pending: no repeat line
    assert st.snapshot()["band"]["refused_503"] == 50


def test_a_refusal_of_another_pair_flushes_a_due_tail_and_a_flush_is_cheap_when_nothing_is_pending(caplog) -> None:
    clock = _Clock()
    st = web_server._RouteStats(clock=clock)
    with caplog.at_level(logging.INFO, logger=LOG):
        st.flush_due()
        for _ in range(3):
            st.record_refusal("band", "page_wait")
        clock.t += 2.0
        st.record_refusal("board", "page_wait")
        assert _refusal_lines(caplog) == ["refused 503 route=band gate=page_wait n=1",
                                          "refused 503 route=board gate=page_wait n=1",      # the refusal's own line
                                          "refused 503 route=band gate=page_wait n=2"]       # the due tail, flushed beside it
    assert st._pending_n == 0


def test_a_refusal_the_page_tolerates_by_design_logs_at_info_not_warning(caplog) -> None:
    st = web_server._RouteStats(clock=_Clock())
    with caplog.at_level(logging.INFO, logger=LOG):
        st.record_refusal("/cockpit/desk.json", "desk_gate")
        st.record_refusal("/cockpit/band.json", "page_wait")
    by_gate = {r.getMessage().split(" gate=")[1].split(" ")[0]: r.levelno for r in caplog.records
               if r.getMessage().startswith("refused 503")}
    assert by_gate == {"desk_gate": logging.INFO, "page_wait": logging.WARNING}, by_gate
    assert st.snapshot()["desk"]["refused_503"] == 1


# --- the silent refusal sites now count ------------------------------------------------------------------------------

def test_the_room_waiter_cap_503_is_counted_and_logged(tmp_path: Path, w, stats, caplog) -> None:
    from levain.cockpit import room as cockpit_room

    with _serve_room(tmp_path, w) as (base, httpd):
        assert _room_call(base, "/cockpit/room/send", w.body("line 1", "ob-00000001"))[0] == 200
        httpd.room_waiter_gate = threading.BoundedSemaphore(1)
        assert httpd.room_waiter_gate.acquire(blocking=False)
        cursor = _room_call(base, cockpit_room.ROOM_PATH, token=ROOM_TOKEN, method="GET")[1]["next"]
        with caplog.at_level(logging.WARNING, logger=LOG):
            st, _body = _room_call(base, f"{cockpit_room.ROOM_PATH}?after={cursor}&wait=5", token=ROOM_TOKEN, method="GET")
        httpd.room_waiter_gate.release()
    assert st == 503
    assert _refusal_lines(caplog) == ["refused 503 route=room gate=room_waiter_gate n=1"], _refusal_lines(caplog)
    assert stats.snapshot()["room"]["refused_503"] == 1


def test_a_pair_route_whose_broker_is_not_settled_is_counted_and_logged(tmp_path: Path, stats, caplog) -> None:
    _root, src, _ids = _install(tmp_path)
    with _pair_serve(src) as (port, httpd):
        real = httpd.broker

        class Stuck:
            def start_async(self):
                pass

            def wait_settled(self, _s):
                return web_server.Lifecycle.ACTIVATING

            def active_db(self):
                return None

        httpd.broker = Stuck()
        try:
            with caplog.at_level(logging.WARNING, logger=LOG):
                st, out, _hdrs = _call(port, "GET", "/cockpit/pair/devices.json", token="pairing-browser-token")
        finally:
            httpd.broker = real
    assert (st, out.get("error")) == (503, "broker_unavailable"), (st, out)
    assert _refusal_lines(caplog) == ["refused 503 route=pair gate=pair_broker n=1"], _refusal_lines(caplog)
