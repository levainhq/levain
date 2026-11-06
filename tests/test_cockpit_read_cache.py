"""The cockpit's shared read cache (1106 solid2): single-flight plus a ~1.5 s TTL for identical manifest and
panel reads. The security half is the point: the key holds everything the response depends on, a response for one
credential is never served to another, and a write invalidates at once."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from levain.cockpit import engine as cockpit_engine
from levain.cockpit.engine import Cockpit, QueryError
from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_k2a import _install, _serve

T_A, T_B = "a" * 40, "b" * 40


@pytest.fixture()
def ck(tmp_path: Path) -> Cockpit:
    _root, src, _ids = _install(tmp_path)
    c = build_default_cockpit(src)
    c.read_cache_ttl_s = 1.5
    return c


def _count_composes(monkeypatch, ck: Cockpit, delay: float = 0.0) -> list[int]:
    n = [0]
    real = ck._manifest_compute

    def counting(*a, **kw):
        n[0] += 1
        time.sleep(delay)
        return real(*a, **kw)

    monkeypatch.setattr(ck, "_manifest_compute", counting)
    return n


def test_concurrent_identical_manifests_share_one_compute_and_the_ttl_reuses_it(ck, monkeypatch) -> None:
    n = _count_composes(monkeypatch, ck, delay=0.3)
    cred = {"class": "token", "name": "browser", "device_class": None}
    out: list = []
    ts = [threading.Thread(target=lambda: out.append(ck.manifest(cred))) for _ in range(28)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert n[0] == 1 and len(out) == 28 and all(o is out[0] for o in out)
    assert ck.manifest(cred) is out[0] and n[0] == 1          # inside the TTL
    ck.read_cache_ttl_s = 0.05
    time.sleep(0.1)
    assert ck.manifest(cred) is not out[0] and n[0] == 2      # past it: a fresh compute


def test_a_cached_response_is_never_served_to_another_credential_transport_or_install_class(ck) -> None:
    a = {"class": "token", "name": "surface-a", "device_class": None}
    b = {"class": "token", "name": "surface-b", "device_class": None}
    none = {"class": "none", "device_class": None}
    ma, mb, mn = ck.manifest(a), ck.manifest(b), ck.manifest(none)
    assert ma["credential"] == a and mb["credential"] == b and mn["credential"] == none
    assert len({ma["etag"], mb["etag"], mn["etag"]}) == 3
    assert ck.manifest(a) is ma and ck.manifest(b) is mb and ck.manifest(none) is mn   # each reuses only its own
    w = ck.manifest(a, transport={"writable": True})
    r = ck.manifest(a, transport={"writable": False})
    assert w is not ma and w is not r and w["transport"] == {"writable": True} and r["transport"] == {"writable": False}
    assert ck.manifest(a, install_class="reported") is not ma


def test_a_write_invalidates_at_once_and_an_older_flight_is_not_joined(ck, monkeypatch) -> None:
    cred = {"class": "token", "name": "browser", "device_class": None}
    first = ck.manifest(cred)
    ck.invalidate_reads()
    assert ck.manifest(cred) is not first                      # no wait for the TTL
    # a compute in flight when the write lands is not joined by a request that arrives after it
    gate, started = threading.Event(), threading.Event()
    real, calls = ck._manifest_compute, []

    def slow(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            gate.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(ck, "_manifest_compute", slow)
    ck.invalidate_reads()
    res: list = []
    t1 = threading.Thread(target=lambda: res.append(ck.manifest(cred)))
    t1.start()
    assert started.wait(10)
    ck.invalidate_reads()                                      # the write
    after = ck.manifest(cred)                                  # arrives after it: computes for itself
    assert len(calls) == 2
    gate.set()
    t1.join(10)
    assert ck.manifest(cred) is after                          # the pre-write owner's result was never stored


def test_a_failed_compute_is_not_cached(ck, monkeypatch) -> None:
    cred = {"class": "none", "device_class": None}
    real, n = ck._manifest_compute, [0]

    def flaky(*a, **kw):
        n[0] += 1
        if n[0] == 1:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(ck, "_manifest_compute", flaky)
    with pytest.raises(RuntimeError):
        ck.manifest(cred)
    assert ck.manifest(cred)["panels"] and n[0] == 2


def test_panel_cache_keys_on_class_profile_row_limit_and_skips_search_unknown_and_replay(ck, monkeypatch) -> None:
    pid = "tray"
    n, real = [0], ck._panel_compute

    def counting(*a, **kw):
        n[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(ck, "_panel_compute", counting)
    p1 = ck.panel(pid, credential_class="token")
    assert ck.panel(pid, credential_class="token") is p1 and n[0] == 1
    assert ck.panel(pid, credential_class="none") is not p1 and n[0] == 2
    assert ck.panel(pid, credential_class="token", profile="compact") is not p1 and n[0] == 3
    # limit bounds only a search: without q it is validated, never keyed (r1: distinct limits minted entries)
    assert ck.panel(pid, credential_class="token", limit=5) is p1 and ck.panel(pid, credential_class="token",
                                                                                 limit=7) is p1 and n[0] == 3
    with pytest.raises(QueryError):
        ck.panel(pid, credential_class="token", limit=0)
    ck.panel(pid, credential_class="token", q="seed")
    ck.panel(pid, credential_class="token", q="seed")
    assert n[0] == 5                                           # a search is never shared
    assert ck.panel("no-such-panel") is None and ck.panel("no-such-panel") is None and n[0] == 7
    ck.invalidate_reads()
    assert ck.panel(pid, credential_class="token") is not p1


def test_a_full_table_evicts_its_oldest_finished_entry_instead_of_reading_uncached(ck) -> None:
    # r1: caller-minted keys (one per real row id) filled the 64 slots and every honest read then ran unshared
    for i in range(cockpit_engine._READ_CACHE_MAX):
        ck._shared_read(("filler", i), lambda: object())
    assert len(ck._rc) == cockpit_engine._READ_CACHE_MAX
    n = [0]

    def honest() -> object:
        n[0] += 1
        return object()

    first = ck._shared_read(("honest",), honest)
    assert ck._shared_read(("honest",), honest) is first and n[0] == 1      # stored, and shared
    assert len(ck._rc) == cockpit_engine._READ_CACHE_MAX
    assert (ck._rc_gen, ("filler", 0)) not in ck._rc                        # the oldest finished one gave way


def test_off_by_default_so_a_directly_built_cockpit_reads_fresh(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    c = build_default_cockpit(src)
    cred = {"class": "none", "device_class": None}
    assert c.read_cache_ttl_s == 0 and c.manifest(cred) is not c.manifest(cred)


def _get(url: str, tok: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"X-Levain-Write-Token": tok} if tok else {})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - loopback
        return r.status, json.loads(r.read())


def test_over_http_two_surface_tokens_never_cross_and_a_post_invalidates(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, install_class="reported", surface_tokens={"browser": T_A, "other": T_B}) as (url, httpd):
        _, ma = _get(url + "/cockpit/manifest.json", T_A)
        _, mb = _get(url + "/cockpit/manifest.json", T_B)
        assert ma["credential"]["name"] == "browser" and mb["credential"]["name"] == "other"
        _, ma2 = _get(url + "/cockpit/manifest.json", T_A)
        assert ma2["credential"]["name"] == "browser"
        # a wrong token is a refusal or class none, and never receives a token-class body
        try:
            _, bad = _get(url + "/cockpit/manifest.json", "x" * 40)
            assert bad["credential"]["class"] == "none"
        except urllib.error.HTTPError as e:
            assert e.code in (401, 403)
        ck = httpd.cockpit
        # r1: an ADMITTED write (a verified token reached the write route) drops the cache BEFORE its status line,
        # so a client holding the answer never reads the pre-write entry. The invalidation is slowed here: were it
        # still run after the response, urlopen would return before it had happened
        calls, real = [], ck.invalidate_reads

        def slow_invalidate() -> None:
            time.sleep(0.5)
            calls.append(time.monotonic())
            real()

        ck.invalidate_reads = slow_invalidate
        gen = ck._rc_gen
        assert _post_status(url + "/edit", T_B) < 500
        assert calls and ck._rc_gen > gen and not ck._rc


def _post_status(url: str, tok: str | None = None, method: str = "POST") -> int:
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-Levain-Write-Token"] = tok
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_a_request_refused_at_the_gate_never_invalidates(tmp_path: Path) -> None:
    # r1: any parsed non-GET request used to flush the cache, so an unauthenticated client could keep it cold
    _root, src, _ids = _install(tmp_path)
    with _serve(src, install_class="reported", surface_tokens={"browser": T_A, "other": T_B}) as (url, httpd):
        _get(url + "/cockpit/manifest.json", T_B)
        ck = httpd.cockpit
        gen, warm = ck._rc_gen, dict(ck._rc)
        assert warm
        assert _post_status(url + "/cockpit/manifest.json", method="OPTIONS") == 501
        assert _post_status(url + "/edit", "x" * 40) == 403          # a wrong token
        assert _post_status(url + "/edit") == 403                    # none at all
        assert _post_status(url + "/no-such-route", "x" * 40) in (403, 404)
        time.sleep(0.2)                                              # past every request's finally
        assert ck._rc_gen == gen and ck._rc == warm
