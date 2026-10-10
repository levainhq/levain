"""Panel parity P1: a server's ``extra_panels`` callable as manifest panels (design §6.2)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from levain.cockpit import Cockpit, ProviderSpec, Read, RowIn
from levain.cockpit.external import ExternalPanels, discoverer
from levain.cockpit.text import snapshot

from tests.test_cockpit_k1 import _http, _install, _serve


def _panels() -> list[dict[str, Any]]:
    return [
        {"id": "tray", "zone": "operate", "title": "Tray by age (2 overdue)", "note": "40 pending",
         "lines": [{"meta": "spore-9", "text": "zeta first", "accent": True},
                   {"meta": "", "text": "alpha second", "dim": True},
                   {"meta": "m", "text": "middle"}],
         "empty": "nothing pending."},
        {"id": "brief", "zone": "mind", "title": "Brief", "markdown": "# hello"},
        {"id": "consult", "title": "Consult", "lines": [], "empty": "Compose a consult above.",
         "action": {"verb": "consult", "fields": []}},
        {"id": "decisions", "title": "Decisions waiting (4)", "note": "dated first", "lines": []},
    ]


class _Counted:
    def __init__(self, panels=None, delay: float = 0.0, exc: Exception | None = None) -> None:
        self.panels = panels if panels is not None else _panels()
        self.delay, self.exc, self.n = delay, exc, 0
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self):
        self.n += 1
        self.gate.wait(10)
        time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.panels


def _cockpit(fn, **kw) -> tuple[Cockpit, ExternalPanels]:
    ext = ExternalPanels(fn, **kw)
    ck = Cockpit()
    ck.register(ProviderSpec("tray", "triage-list", "Tray", "gate",
                             lambda c: Read(rows=(RowIn("k1", "kernel row", stored={"id": "k1"}),)),
                             order="time.desc", version_fields=("*",)))
    ck.discover(discoverer(ext))
    return ck, ext


def test_one_refresh_calls_the_callable_once_for_the_manifest_and_every_panel() -> None:
    fn = _Counted(delay=0.05)
    ck, ext = _cockpit(fn)
    snap = snapshot(ck)
    assert {"ext:tray", "ext:brief", "ext:consult", "ext:decisions"} <= set(snap["panels"])
    assert fn.n == 1 and ext.calls == 1


def test_a_kernel_panel_and_an_adapted_one_with_the_same_id_both_stand() -> None:
    ck, _ = _cockpit(_Counted())
    snap = snapshot(ck)
    assert snap["panels"]["tray"]["rows"][0]["title"] == "kernel row"
    assert [r["title"] for r in snap["panels"]["ext:tray"]["rows"]] == ["zeta first", "alpha second", "middle"]


def test_lines_become_rows_in_the_source_order_with_meta_and_emphasis() -> None:
    ck, _ = _cockpit(_Counted())
    p = snapshot(ck)["panels"]["ext:tray"]
    assert [(r["facets"].get("legacy_meta"), r["emphasis"]) for r in p["rows"]] == [
        ("spore-9", "accent"), (None, "dim"), ("m", "none")]
    assert p["order"] == "legacy.source" and p["kind"] == "triage-list"


def test_a_live_title_clause_moves_to_the_note_and_a_bare_count_is_left_to_the_kernel() -> None:
    ck, _ = _cockpit(_Counted())
    snap = snapshot(ck)
    tray, dec = snap["panels"]["ext:tray"], snap["panels"]["ext:decisions"]
    assert tray["note"] == "2 overdue · 40 pending" and tray["title"].startswith("Tray by age")
    assert dec["note"] == "dated first" and "(4)" not in dec["title"]


def test_a_title_clause_the_note_already_carries_is_said_once() -> None:
    fn = _Counted([{"id": "inbox", "title": "Inbox (10 unread)", "note": "10 unread · 50 shown", "lines": []}])
    ck, _ = _cockpit(fn)
    assert snapshot(ck)["panels"]["ext:inbox"]["note"] == "10 unread · 50 shown"


def test_a_changing_live_title_keeps_one_panel_and_no_collision_error() -> None:
    fn = _Counted()
    ck, _ = _cockpit(fn, reuse_s=0)
    snapshot(ck)
    fn.panels = [dict(p, title="Tray by age (5 overdue)") if p["id"] == "tray" else p for p in _panels()]
    snap = snapshot(ck)
    assert snap["manifest"]["errors"] == [] and snap["panels"]["ext:tray"]["note"].startswith("5 overdue")


def test_markdown_is_a_prose_panel_in_its_zone() -> None:
    ck, _ = _cockpit(_Counted())
    snap = snapshot(ck)
    zones = {z["id"]: z["panels"] for z in snap["manifest"]["regions"]["zones"]}
    assert "ext:brief" in zones["mind"] and "ext:tray" in zones["operate"]
    assert snap["panels"]["ext:brief"]["value"]["markdown"] == "# hello"


def test_an_action_panel_drops_its_compose_sentence() -> None:
    ck, _ = _cockpit(_Counted())
    p = snapshot(ck)["panels"]["ext:consult"]
    assert p["status"] == "empty" and "Compose" not in p["empty"]


def test_a_panel_error_and_a_vanished_panel_are_errors_never_empty() -> None:
    fn = _Counted()
    ck, _ = _cockpit(fn, reuse_s=0)
    snapshot(ck)
    fn.panels = [dict(_panels()[0], error="argushub down"), _panels()[1]]
    snap = snapshot(ck)
    assert snap["panels"]["ext:tray"]["status"] == "error" and "argushub down" in snap["panels"]["ext:tray"]["error"]
    assert snap["panels"]["ext:consult"]["status"] == "error"


def test_a_failing_callable_is_one_call_and_a_manifest_error() -> None:
    fn = _Counted(exc=OSError("connection refused"))
    ck, ext = _cockpit(fn, reuse_s=60)
    snap = snapshot(ck)
    assert any("connection refused" in e["message"] for e in snap["manifest"]["errors"])
    assert not [p for p in snap["panels"] if p.startswith("ext:")] and ext.calls == 1


def test_a_callable_that_starts_failing_turns_every_adapted_panel_to_error() -> None:
    fn = _Counted()
    ck, _ = _cockpit(fn, reuse_s=0)
    snapshot(ck)
    fn.exc = OSError("connection refused")
    for pid in ("ext:tray", "ext:brief", "ext:consult", "ext:decisions"):
        got = ck.panel(pid)
        assert got["status"] == "error" and "connection refused" in got["error"], pid


def test_concurrent_readers_join_one_flight() -> None:
    fn = _Counted()
    fn.gate.clear()
    ext = ExternalPanels(fn, reuse_s=60)
    out: list[Any] = []
    ts = [threading.Thread(target=lambda: out.append(ext.take())) for _ in range(8)]
    for t in ts:
        t.start()
    time.sleep(0.2)
    fn.gate.set()
    for t in ts:
        t.join(5)
    assert fn.n == 1 and len(out) == 8 and all(o is out[0] for o in out)


def test_a_finished_call_is_reused_only_inside_the_window() -> None:
    now = [0.0]
    fn = _Counted()
    ext = ExternalPanels(fn, reuse_s=5, clock=lambda: now[0])
    a = ext.take()
    now[0] = 4.9
    assert ext.take() is a and fn.n == 1
    now[0] = 5.0
    assert ext.take() is not a and fn.n == 2


def test_a_hung_callable_parks_one_flight_and_readers_fault_at_the_wait_bound() -> None:
    fn = _Counted()
    fn.gate.clear()
    ext = ExternalPanels(fn, wait_s=0.2)
    t0 = time.monotonic()
    a, b = ext.take(), ext.take()
    assert a.panels is None and "did not answer" in (a.error or "") and b.panels is None
    assert fn.n == 1 and time.monotonic() - t0 < 2
    fn.gate.set()


def test_the_server_adapts_extra_panels_into_its_manifest(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    fn = _Counted()
    with _serve(src, extra_panels=fn) as (base, httpd):
        st, _h, body = _http(f"{base}/cockpit/manifest.json")
        assert st == 200
        ids = json.loads(body)["panels"]
        assert "ext:tray" in ids
        for pid in ids:
            assert _http(f"{base}/cockpit/panel/{pid}.json?profile=full")[0] == 200
        assert fn.n == 1 and httpd.external_panels.calls == 1
