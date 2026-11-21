"""A paired device's page-load burst is verified once, not once per read (levain#228).

The defect: a page load sends its reads together, all with one paired token, and every read took one of the
``_PAIR_VERIFY_SLOTS`` lookup slots without waiting, so the reads past the slots were answered ``503 broker_unavailable``
(``gate=credential:paired lookups are full``). A phone's browser alone reaches more concurrent reads than the slots.

The contract these tests pin:

* a token's concurrent lookups are ONE (a flight per token hash), and a live-device answer is cached briefly, keyed by
  the token's hash; nothing unverified is ever cached;
* an unauthenticated caller still costs at most ``_PAIR_VERIFY_SLOTS`` lookups in flight, refused at once when full;
* a revoke through the server, or by any other process, is seen by the next request (the journal's revocation-revision
  file; the TTL only bounds an entry's age);
* the refusal line carries its time, a slow lookup is logged with the lookups in flight, and the 503s a verb and a sign
  answer are logged like every other.
"""
from __future__ import annotations

import hashlib
import http.client
import logging
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from levain import web_server
from levain.cockpit.broker import Broker, Lifecycle
from levain.cockpit.firejournal import CockpitDB, revoke_digest
from levain.consent.sign import SignRefused
from levain.writes import EditError
from tests._waits import scaled
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve

LOG = web_server.__name__
PAGE_READS = ("/cockpit/panel/tray.json", "/cockpit/manifest.json", "/cockpit/band.json", "/cockpit/board.json")
PER_PAGE = 28                       # the reads one page load sends together
BROWSER_CONNECTIONS = 6             # a browser's connections to one host
AT = re.compile(r" at=(\S+)$")


@pytest.fixture
def src(tmp_path: Path):
    return _install(tmp_path)[1]


@pytest.fixture
def stats(monkeypatch):
    """A fresh route counter: the refusal log is rate-limited per (route, gate), and a count is this test's alone."""
    fresh = web_server._RouteStats()
    monkeypatch.setattr(web_server, "_ROUTE_STATS", fresh)
    return fresh


@pytest.fixture
def rig(src, stats):
    """A serve with its broker active and one paired device: (port, httpd, device id, token)."""
    with _serve(src) as (port, httpd):
        assert httpd.broker.start() is Lifecycle.ACTIVE
        device, tok = _pair(port)
        yield port, httpd, device, tok


class _Spy:
    """``CockpitDB.verify_device`` counted: calls, the most in flight at once, and the tokens looked up. ``hold`` keeps
    each call in its slot so overlap is forced on any box; ``before(n)`` and ``after(n)`` run around call number n."""

    def __init__(self, monkeypatch, hold: float = 0.0) -> None:
        self.calls = 0
        self.inflight = 0
        self.max_inflight = 0
        self.tokens: list[str] = []
        self.hold = hold
        self.before: "Callable[[int], None] | None" = None
        self.after: "Callable[[int], None] | None" = None
        self._mu = threading.Lock()
        real = CockpitDB.verify_device

        def spy(db: CockpitDB, token: str, *, now: datetime) -> "str | None":
            with self._mu:
                self.calls += 1
                n = self.calls
                self.tokens.append(token)
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                if self.before is not None:
                    self.before(n)
                if self.hold:
                    time.sleep(self.hold)
                got = real(db, token, now=now)
                if self.after is not None:
                    self.after(n)
                return got
            finally:
                with self._mu:
                    self.inflight -= 1

        monkeypatch.setattr(CockpitDB, "verify_device", spy)


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOG and r.getMessage().startswith("refused 503")]


def _page_burst(port: int, tok: str, per_page: int = PER_PAGE) -> list[tuple[int, str]]:
    """``per_page`` reads at once, each on a connection of its own behind a barrier: (status, error word) of each."""
    out: list[tuple[int, str]] = []
    mu = threading.Lock()
    gate = threading.Barrier(per_page)

    def one(i: int) -> None:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            c.connect()
            gate.wait(30)
            st, body, _h = _call(port, "GET", PAGE_READS[i % len(PAGE_READS)], token=tok, headers=PROXY, conn=c)
            with mu:
                out.append((st, str(body.get("error", ""))))
        finally:
            c.close()

    ths = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(per_page)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(90)
    assert len(out) == per_page, f"{per_page - len(out)} reads never answered"
    return out


def _keep_alive_burst(port: int, tok: str) -> list[tuple[int, str]]:
    """A browser's shape: ``BROWSER_CONNECTIONS`` kept-alive connections carry the page's reads between them; a 503
    closes its connection, so that worker opens another."""
    out: list[tuple[int, str]] = []
    mu = threading.Lock()
    gate = threading.Barrier(BROWSER_CONNECTIONS)

    def worker(k: int) -> None:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        gate.wait(30)
        for i in range(k, PER_PAGE, BROWSER_CONNECTIONS):
            st, body, hdrs = _call(port, "GET", PAGE_READS[i % len(PAGE_READS)], token=tok, headers=PROXY, conn=c)
            with mu:
                out.append((st, str(body.get("error", ""))))
            if st == 503 or hdrs.get("connection", "").lower() == "close":
                c.close()
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.close()

    ths = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range(BROWSER_CONNECTIONS)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(120)
    assert len(out) == PER_PAGE, f"{PER_PAGE - len(out)} reads never answered"
    return out


def _flood(port: int, stop: threading.Event, threads: int) -> "tuple[list[threading.Thread], list[tuple[int, str, float]]]":
    """``threads`` callers each sending a fresh random header-safe token until ``stop``: (threads, the answers)."""
    answers: list[tuple[int, str, float]] = []
    mu = threading.Lock()

    def run() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            st, body, _h = _call(port, "GET", "/cockpit/manifest.json", token=secrets.token_urlsafe(32), headers=PROXY)
            with mu:
                answers.append((st, str(body.get("error", "")), time.monotonic() - t0))

    ths = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
    for t in ths:
        t.start()
    return ths, answers


# --- 1, 2. the defect: one page load's reads are never refused at the credential ------------------------------------

def test_a_paired_page_burst_of_28_reads_is_never_refused_at_the_credential(rig, monkeypatch, caplog) -> None:
    port, _httpd, _dev, tok = rig
    spy = _Spy(monkeypatch, hold=0.05)
    caplog.set_level(logging.INFO, logger=LOG)
    got = _page_burst(port, tok)
    assert [g for g in got if g[0] == 503] == [], got
    assert all(st == 200 for st, _e in got), got
    assert not [ln for ln in _refusals(caplog) if "gate=credential:" in ln], _refusals(caplog)
    assert spy.calls <= 2, f"the burst's lookups were shared, not {spy.calls}"


def test_a_browser_shaped_paired_page_load_on_six_kept_alive_connections_is_never_refused(rig, monkeypatch, caplog) -> None:
    port, _httpd, _dev, tok = rig
    spy = _Spy(monkeypatch, hold=0.05)
    caplog.set_level(logging.INFO, logger=LOG)
    got = _keep_alive_burst(port, tok)
    assert [g for g in got if g[0] == 503] == [], got
    assert not [ln for ln in _refusals(caplog) if "gate=credential:" in ln], _refusals(caplog)
    assert spy.calls <= 2, f"the page's lookups were shared, not {spy.calls}"


# --- 3, 4, 5. the unauthenticated bound holds; a known device does not pay for a flood ------------------------------

def test_an_unknown_token_flood_is_still_bounded_and_refused_fast(rig, monkeypatch) -> None:
    port, _httpd, _dev, _tok = rig
    spy = _Spy(monkeypatch, hold=0.02)
    stop = threading.Event()
    ths, answers = _flood(port, stop, threads=16)
    time.sleep(1.0)
    stop.set()
    for t in ths:
        t.join(30)
    assert answers, "the flood sent nothing"
    assert spy.max_inflight <= web_server._PAIR_VERIFY_SLOTS, spy.max_inflight
    assert {st for st, _e, _t in answers} <= {403, 503}, {st for st, _e, _t in answers}
    refused = [(e, took) for st, e, took in answers if st == 503]
    assert refused and all(e == "broker_unavailable" for e, _t in refused), refused[:3]
    assert max(took for _e, took in refused) < scaled(1.0), "a refusal waits for nothing"


def test_an_unknown_flood_does_not_starve_a_known_device_with_a_warm_entry(rig, monkeypatch) -> None:
    port, _httpd, _dev, tok = rig
    monkeypatch.setattr(web_server, "_PAIR_VERIFIED_TTL_S", 60.0)    # the entry outlives the test on any box
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    _Spy(monkeypatch, hold=0.05)
    stop = threading.Event()
    ths, answers = _flood(port, stop, threads=16)
    try:
        time.sleep(0.3)
        got = [_call(port, "GET", PAGE_READS[2 + i % 2], token=tok, headers=PROXY)[0] for i in range(20)]
    finally:
        stop.set()
        for t in ths:
            t.join(30)
    assert got == [200] * 20, got
    assert any(st == 503 for st, _e, _t in answers), "the flood never filled the slots: the test proved nothing"


def test_a_repeated_junk_token_flood_coalesces(rig, monkeypatch) -> None:
    port, _httpd, _dev, _tok = rig
    spy = _Spy(monkeypatch, hold=0.05)
    junk = "j" * 43
    got = _page_burst(port, junk)
    assert {st for st, _e in got} == {403}, got
    assert spy.calls <= 2, spy.calls
    before = spy.calls
    assert _call(port, "GET", "/cockpit/manifest.json", token=junk, headers=PROXY)[0] == 403
    assert spy.calls == before + 1, "a miss is never cached: the next request looks it up again"


# --- 6, 7, 8. revocation -------------------------------------------------------------------------------------------

def test_an_in_process_revoke_is_seen_by_the_next_request_after_a_cached_hit(rig, monkeypatch) -> None:
    port, _httpd, device, tok = rig
    spy = _Spy(monkeypatch)
    keep = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    for _ in range(2):
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)[0] == 200
    assert spy.calls == 1, "the second read was served from the verified entry"
    sock = keep.sock
    body = {"id": device, "expect_digest": revoke_digest([device], "family"), "mode": "family"}
    assert _call(port, "POST", "/cockpit/pair/revoke", body, token=T)[0] == 200
    st, out, _h = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)
    assert keep.sock is sock, "the next request rides the same connection"
    assert st == 403, out
    keep.close()


def test_a_cross_process_revoke_is_seen_within_the_ttl(rig, monkeypatch, capsys) -> None:
    from levain.cli import main

    port, httpd, _dev, tok = rig
    monkeypatch.setattr(web_server, "_PAIR_VERIFIED_TTL_S", 0.5)
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    assert main(["cockpit", "pair", "--data-root", str(httpd.broker.data_root), "--revoke-all"]) == 0
    capsys.readouterr()
    time.sleep(scaled(0.5) + 0.3)
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 403


def test_a_revoke_committing_during_a_lookup_leaves_no_live_entry(rig, monkeypatch) -> None:
    port, _httpd, device, tok = rig
    spy = _Spy(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def hold_first(n: int) -> None:
        if n == 1:                      # the lookup has its answer (a live device); the revoke lands before it returns
            entered.set()
            assert release.wait(30)

    spy.after = hold_first
    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(
        _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0]), daemon=True)
    t.start()
    assert entered.wait(30)
    body = {"id": device, "expect_digest": revoke_digest([device], "family"), "mode": "family"}
    assert _call(port, "POST", "/cockpit/pair/revoke", body, token=T)[0] == 200
    release.set()
    t.join(30)
    assert first, "the lookup in flight never answered"
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 403
    assert spy.calls == 2, "the entry the revoked lookup left was stale: the next request looked up again"


# --- 9, 10. a failed or unavailable lookup is never an answer ------------------------------------------------------

def test_a_leader_that_raises_wakes_its_followers_with_the_503(rig, monkeypatch) -> None:
    port, _httpd, _dev, tok = rig
    spy = _Spy(monkeypatch)

    def fail_first(n: int) -> None:
        if n == 1:
            time.sleep(0.5)             # long enough for every follower to join the flight
            raise sqlite3.OperationalError("disk I/O error")

    spy.before = fail_first
    got = _page_burst(port, tok, per_page=5)
    assert got == [(503, "broker_unavailable")] * 5, got
    assert spy.calls == 1, "the followers took the leader's refusal, not a lookup of their own"
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    assert spy.calls == 2, "no entry was stored by the failed lookup"


def test_no_hit_while_the_broker_is_not_active(rig, monkeypatch) -> None:
    port, httpd, _dev, tok = rig
    spy = _Spy(monkeypatch)
    monkeypatch.setattr(web_server, "_PAIR_WAIT_S", scaled(0.3))
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    monkeypatch.setattr(Broker, "start_async", lambda self: None)       # nothing brings the broker back during the test
    with httpd.broker._mu:
        httpd.broker._state = Lifecycle.STANDBY
    try:
        st, out, hdrs = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)
        assert (st, out.get("error")) == (503, "broker_unavailable"), (st, out)
        assert hdrs.get("retry-after") == "2"
    finally:
        with httpd.broker._mu:
            httpd.broker._state = Lifecycle.ACTIVE
    assert spy.calls == 1, "the 503 came from the broker's state, not from a lookup"
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200   # the entry is live again with the broker


# --- 11. the instruments ------------------------------------------------------------------------------------------

def test_the_instruments_stamp_a_refusal_name_a_slow_lookup_and_log_the_verb_and_sign_503s(
        rig, stats, monkeypatch, caplog) -> None:
    port, httpd, _dev, tok = rig
    caplog.set_level(logging.INFO, logger=LOG)

    # the refusal line ends with its time, in UTC, to the second
    stats.record_refusal("band", "page_wait")
    line = _refusals(caplog)[-1]
    m = AT.search(line)
    assert line.startswith("refused 503 route=band gate=page_wait n=1 at=") and m, line
    stamp = datetime.fromisoformat(m.group(1))
    assert stamp.tzinfo is not None and abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60, line

    # a lookup that held its slot a long while says so, with the lookups in flight and nothing of the token
    _Spy(monkeypatch, hold=web_server._PAIR_SLOW_LOOKUP_MS / 1000 + 0.05)
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    slow = [r.getMessage() for r in caplog.records if r.getMessage().startswith("paired lookup slow ms=")]
    assert len(slow) == 1 and re.fullmatch(r"paired lookup slow ms=\d+ inflight=\d+ at=\S+", slow[0]), slow
    assert tok not in caplog.text and hashlib.sha256(tok.encode()).hexdigest() not in caplog.text

    # the 503s a verb and a sign answer are counted and logged, as every other 503 is
    def store_fault(**_kw: Any) -> Any:
        raise EditError("store_busy", 503, "the store is busy")

    monkeypatch.setattr(web_server, "dispatch_verb", store_fault)
    assert _call(port, "POST", "/cockpit/verb", {"verb": "x"}, token=T)[0] == 503
    assert "refused 503 route=verb gate=edit:store_busy" in " | ".join(_refusals(caplog)), _refusals(caplog)

    def sign_busy(_rid: str, _req: Any) -> Any:
        raise SignRefused("the queue is locked", 503)

    httpd.signer = SimpleNamespace(answer=sign_busy)
    assert _call(port, "POST", "/cockpit/sign/some-request/answer", {}, token=T)[0] == 503
    assert [ln for ln in _refusals(caplog) if ln.startswith("refused 503 route=sign gate=sign:")], _refusals(caplog)


# --- 12. a revoke by ANY process is seen by the very next request (the revocation-revision file) -----------------------

_REVOKE_IN_ANOTHER_PROCESS = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from levain.cli import main\n"
    "sys.exit(main(['cockpit', 'pair', '--data-root', sys.argv[2], '--revoke', sys.argv[3]]))\n")


def test_a_second_process_that_revokes_is_refused_on_the_very_next_request(rig, monkeypatch) -> None:
    port, httpd, device, tok = rig
    spy = _Spy(monkeypatch)
    keep = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    for _ in range(3):
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)[0] == 200
    assert spy.calls == 1, "warm: the reads after the first were served from the verified entry"
    done = subprocess.run([sys.executable, "-I", "-c", _REVOKE_IN_ANOTHER_PROCESS, str(Path(__file__).resolve().parents[1]),
                           str(httpd.broker.data_root), device], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and f"revoked 1 paired device(s): {device}" in done.stdout, (done.stdout, done.stderr)
    assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)[0] == 403
    keep.close()


def _hold_leader_after_its_answer(port: int, tok: str, spy: "_Spy") -> "tuple[threading.Event, threading.Thread, list[int]]":
    """A leader whose lookup has its (live) answer and waits: (release, its thread, its status once it answers)."""
    entered, release = threading.Event(), threading.Event()

    def hold_first(n: int) -> None:
        if n == 1:
            entered.set()
            assert release.wait(30)

    spy.after = hold_first
    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(
        _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0]), daemon=True)
    t.start()
    assert entered.wait(30)
    return release, t, first


def _late_follower(port: int, tok: str, release: threading.Event, leader: threading.Thread) -> int:
    """A request sent now, after a revoke has returned: it parks on the leader's flight, then the leader is released."""
    out: list[int] = []
    t = threading.Thread(target=lambda: out.append(
        _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0]), daemon=True)
    t.start()
    time.sleep(0.5)                     # parked on the flight
    release.set()
    leader.join(30)
    t.join(30)
    assert out, "the follower never answered"
    return out[0]


def test_a_request_after_a_revoke_through_the_server_is_not_admitted_by_a_lookup_that_began_before_it(
        rig, monkeypatch) -> None:
    port, _httpd, device, tok = rig
    spy = _Spy(monkeypatch)
    release, leader, _first = _hold_leader_after_its_answer(port, tok, spy)
    body = {"id": device, "expect_digest": revoke_digest([device], "family"), "mode": "family"}
    assert _call(port, "POST", "/cockpit/pair/revoke", body, token=T)[0] == 200      # the revoke has returned
    assert _late_follower(port, tok, release, leader) in (403, 503)


def test_a_request_after_a_revoke_through_the_journal_is_not_admitted_by_a_lookup_that_began_before_it(
        rig, monkeypatch) -> None:
    port, httpd, device, tok = rig
    spy = _Spy(monkeypatch)
    release, leader, _first = _hold_leader_after_its_answer(port, tok, spy)
    httpd.broker.active_db().revoke_devices([device], by="box", now=datetime.now(timezone.utc))     # no epoch moves here
    assert _late_follower(port, tok, release, leader) in (403, 503)


def test_a_cache_hit_costs_one_stat_of_the_revision_file_and_no_lookup(rig, monkeypatch) -> None:
    port, _httpd, _dev, tok = rig
    spy = _Spy(monkeypatch)
    stats_made: list[int] = []
    real = web_server.revision_identity

    def counted(root: Any) -> Any:
        stats_made.append(1)
        return real(root)

    monkeypatch.setattr(web_server, "revision_identity", counted)
    reads = 12
    for _ in range(reads):
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
    assert spy.calls == 1
    assert len(stats_made) == reads + spy.calls, "one stat per request, and the one lookup read it once more before it ran"


def test_slow_lookup_warnings_are_limited_to_one_per_interval(rig, monkeypatch, caplog) -> None:
    port, _httpd, _dev, _tok = rig
    monkeypatch.setattr(web_server, "_PAIR_SLOW_LOG_EVERY_S", 3600.0, raising=False)
    _Spy(monkeypatch, hold=web_server._PAIR_SLOW_LOOKUP_MS / 1000 + 0.05)
    caplog.set_level(logging.DEBUG, logger=LOG)
    for _ in range(3):
        assert _call(port, "GET", "/cockpit/manifest.json", token=secrets.token_urlsafe(32), headers=PROXY)[0] == 403
    slow = [r for r in caplog.records if r.getMessage().startswith("paired lookup slow ms=")]
    assert [r.levelno for r in slow] == [logging.WARNING, logging.DEBUG, logging.DEBUG], [r.levelname for r in slow]
