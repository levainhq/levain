"""Cockpit reads wait for a gate slot instead of an instant 503 (a page load bursts ~28 panel reads)."""
from __future__ import annotations

import collections
import threading
import time
import urllib.error
import urllib.request

import levain.web_server as ws
from tests.test_cockpit_k2a import _install, _post, _serve


def _slow(monkeypatch, seconds: float) -> None:
    real = ws.handle_cockpit_get

    def slow(*a, **k):
        time.sleep(seconds)
        return real(*a, **k)

    monkeypatch.setattr(ws, "handle_cockpit_get", slow)


def _burst(url: str, n: int) -> collections.Counter:
    got: list[int] = []

    def one() -> None:
        try:
            with urllib.request.urlopen(url + "/cockpit/panel/decisions.json?profile=full", timeout=30) as r:  # noqa: S310
                got.append(r.status)
        except urllib.error.HTTPError as e:
            got.append(e.code)

    ts = [threading.Thread(target=one) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return collections.Counter(got)


def test_a_burst_above_the_cap_all_answers_200_within_the_wait(tmp_path, monkeypatch):
    _slow(monkeypatch, 0.1)
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (url, _httpd):
        assert _burst(url, ws._MAX_INFLIGHT * 3) == {200: ws._MAX_INFLIGHT * 3}


def test_past_the_waiter_bound_a_read_is_still_refused_503(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_MAX_INFLIGHT", 1)
    monkeypatch.setattr(ws, "_MAX_READ_WAITERS", 2)
    _slow(monkeypatch, 0.6)
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (url, httpd):
        got = _burst(url, 8)
        deadline = time.monotonic() + 3.0                     # a handler may still be in its finally
        while time.monotonic() < deadline:
            with httpd.read_waiters_lock:
                if httpd.read_waiters == 0:
                    break
            time.sleep(0.02)
        with httpd.read_waiters_lock:
            assert httpd.read_waiters == 0                    # every waiter left the room
    assert got[503] >= 1 and got[200] >= 3 and got[200] + got[503] == 8


def test_a_write_during_a_full_read_burst_is_answered_not_503(tmp_path, monkeypatch):
    _slow(monkeypatch, 0.8)
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (url, _httpd):
        burst = threading.Thread(target=_burst, args=(url, ws._MAX_INFLIGHT * 3))
        burst.start()
        time.sleep(0.3)                                       # the reads now hold every slot they may
        status, _body = _post(f"{url}/edit", {})
        burst.join()
    assert status != 503                                      # two slots stay free for the instant-503 routes
