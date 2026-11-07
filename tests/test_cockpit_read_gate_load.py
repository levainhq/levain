"""Load test + counters for the cockpit GET read gate (1106 solid, levain half).

The shape is the reproduced failure: a decisions panel with 40 open and 200 answered questions, and a page load's
burst of ~28 concurrent GETs, five times over. It asserts no 503 from the read gate and a bounded p95.

What it does NOT prove: flow's slowness was a per-answered-row signature-check subprocess in the HOST's question
type; this test serves levain's own QuestionStore (no such subprocess) and forks nothing itself, so it cannot
reproduce that cost. It proves the gate/handler path stays flat under the burst shape, nothing about the host.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from levain.consent.questions import QuestionStore
from levain.cockpit.engine import _question_rows_in
from levain import web_server
from levain.web_server import _RouteStats
from tests.test_cockpit_k2a import _install, _serve

BURSTS, WIDTH = 5, 28
# p95 ceiling is 3 s, not the 1 s goal: a loaded shared CI runner stretches thread scheduling by multiples,
# and this guards "no pile-up / no 503", not absolute speed.
P95_CEILING_S = 3.0


def _fill(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    for i in range(240):
        q = store.file(agent="claude_code", agent_session_id=f"s{i}", what=f"Question {i}?",
                       options=[("a", "Yes"), ("b", "No")], rec="a", blocks="b", cwd="/w")
        if i >= 40:
            store.answer(q.n, option="a", channel="phone")


def _get(url: str) -> tuple[int, float]:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - loopback
            r.read()
            return r.status, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.monotonic() - t0


def test_burst_of_page_loads_has_no_503_and_bounded_p95(tmp_path: Path, monkeypatch) -> None:
    _fill(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    paths = ["/cockpit/panel/decisions.json", "/cockpit/panel/tray.json", "/cockpit/manifest.json"]
    with _serve(src, install_class="reported") as (url, httpd):
        assert httpd.question_store is not None
        results: list[tuple[int, float]] = []
        lock = threading.Lock()

        def one(p: str) -> None:
            r = _get(url + p)
            with lock:
                results.append(r)

        for _ in range(BURSTS):
            threads = [threading.Thread(target=one, args=(paths[i % 3],)) for i in range(WIDTH)]
            [t.start() for t in threads]
            [t.join(60) for t in threads]
    assert len(results) == BURSTS * WIDTH
    assert [s for s, _ in results if s == 503] == []
    assert all(s == 200 for s, _ in results), sorted({s for s, _ in results})
    lat = sorted(t for _, t in results)
    assert lat[int(len(lat) * 0.95) - 1] < P95_CEILING_S, lat[-5:]


def test_counters_log_one_slow_line_and_stay_bounded(caplog) -> None:
    st = _RouteStats()
    with caplog.at_level(logging.WARNING, logger=web_server.__name__):
        st.record("/cockpit/panel/decisions.json?x=secret", 10.0)
        st.record("/cockpit/panel/decisions.json", 2500.0, gate_wait_ms=1800.0)
        st.record("/cockpit/manifest.json", 5.0, refused=True)
    slow = [r for r in caplog.records if "slow cockpit read" in r.getMessage()]
    assert len(slow) == 1
    msg = slow[0].getMessage()
    assert "route=panel/decisions" in msg and "gate_wait_ms=1800" in msg and "secret" not in msg
    snap = st.snapshot()
    assert snap["panel/decisions"]["count"] == 2 and snap["panel/decisions"]["max_ms"] == 2500.0
    assert snap["manifest"]["refused_503"] == 1
    for i in range(500):
        st.record(f"/cockpit/panel/p{i}.json", 1.0)
        st.record(f"/cockpit/question/{i}.json", 1.0)
        st.record(f"/cockpit/elsewhere/{i}", 1.0)
    assert len(st.snapshot()) <= web_server._ROUTE_PANEL_IDS_MAX + len(_RouteStats._FIXED) + 1


def test_question_rows_ignore_signed_and_signer_on_a_light_question() -> None:
    """The engine reads only answer.answered_at from an answered question: a host's light question type with
    no ``signed``/``signer`` on its answer yields the same row."""
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=5)).isoformat()

    def q(answer):
        return SimpleNamespace(n=7, agent="codex", filed_at=stamp, options=[("a", "x")], answer=answer)

    class Store:
        def __init__(self, item): self.item = item
        def open_questions(self): return []
        def recent(self, limit): return [self.item]

    full = SimpleNamespace(answered_at=stamp, signed=True, signer="k", option="a")
    light = SimpleNamespace(answered_at=stamp)
    rows_full, _ = _question_rows_in(Store(q(full)), now)
    rows_light, _ = _question_rows_in(Store(q(light)), now)
    assert [r.facets for r in rows_light] == [r.facets for r in rows_full] and len(rows_light) == 1
    assert rows_light[0].facets["question_state"] == "answered"


def _raw_get_and_close(url: str, path: str) -> None:
    """A client that connects, sends the request and closes before reading the response."""
    import socket
    host, port = url.split("//", 1)[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    time.sleep(0.05)
    s.close()


def _count(key: str, field: str = "count") -> int:
    return web_server._ROUTE_STATS.snapshot().get(key, {}).get(field, 0)


def _wait_for(pred, secs: float = 5.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_a_slow_read_the_client_abandons_is_still_counted(tmp_path: Path, monkeypatch) -> None:
    """The slowest reads are the ones clients abandon: a send that raises must not skip the record."""
    _root, src, _ids = _install(tmp_path)
    real_send = web_server._Handler._send

    def dying_send(self, body, ctype, *a, **kw):
        if kw.get("status", 200) == 200 and "json" in ctype:
            raise BrokenPipeError("client left")
        return real_send(self, body, ctype, *a, **kw)

    monkeypatch.setattr(web_server._Handler, "_send", dying_send)
    before, before_ab = _count("panel/tray"), _count("panel/tray", "aborted")
    with _serve(src, install_class="reported") as (url, _httpd):
        _raw_get_and_close(url, "/cockpit/panel/tray.json")
        assert _wait_for(lambda: _count("panel/tray") == before + 1)
    assert _count("panel/tray", "aborted") == before_ab + 1


def test_a_503_is_counted_when_the_client_has_gone(tmp_path: Path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    real_send = web_server._Handler._send

    def dying_send(self, body, ctype, *a, **kw):
        if kw.get("status") == 503:
            raise BrokenPipeError("client left")
        return real_send(self, body, ctype, *a, **kw)

    monkeypatch.setattr(web_server._Handler, "_send", dying_send)
    monkeypatch.setattr(web_server._Handler, "_acquire_read_slot", lambda self: False)
    before = _count("panel/tray", "refused_503")
    with _serve(src, install_class="reported") as (url, _httpd):
        _raw_get_and_close(url, "/cockpit/panel/tray.json")
        assert _wait_for(lambda: _count("panel/tray", "refused_503") == before + 1)


def test_panel_other_does_not_steal_a_named_slot_and_the_log_names_the_folded_key(caplog) -> None:
    st = _RouteStats()
    st.record("/cockpit/panel/bad!id.json", 1.0)   # lands in panel/other first
    for i in range(web_server._ROUTE_PANEL_IDS_MAX):
        st.record(f"/cockpit/panel/p{i}.json", 1.0)
    snap = st.snapshot()
    assert all(f"panel/p{i}" in snap for i in range(web_server._ROUTE_PANEL_IDS_MAX))
    with caplog.at_level(logging.WARNING, logger=web_server.__name__):
        st.record("/cockpit/panel/extra.json", 2500.0)
    assert any("route=panel/other" in r.getMessage() for r in caplog.records)


def test_28_concurrent_manifests_share_one_compose(tmp_path: Path, monkeypatch) -> None:
    """The shared read cache: a page load's burst of identical manifest GETs composes the 36 panels ONCE."""
    from levain.cockpit.engine import Cockpit

    _fill(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    composes = [0]
    real = Cockpit._manifest_compute

    def counting(self, *a, **kw):
        composes[0] += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(Cockpit, "_manifest_compute", counting)
    with _serve(src, install_class="reported") as (url, httpd):
        assert _get(url + "/cockpit/manifest.json")[0] == 200            # builds the cockpit, warms it
        httpd.cockpit.invalidate_reads()
        composes[0] = 0
        results: list[int] = []
        lock = threading.Lock()

        def one() -> None:
            s, _t = _get(url + "/cockpit/manifest.json")
            with lock:
                results.append(s)

        threads = [threading.Thread(target=one) for _ in range(WIDTH)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
    assert results == [200] * WIDTH
    assert composes[0] == 1, composes[0]
