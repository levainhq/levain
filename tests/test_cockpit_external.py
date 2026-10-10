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
        {"id": "decisions", "title": "Decisions waiting (4)", "note": "dated first",
         "lines": [{"meta": "due", "text": f"d{i}"} for i in range(4)]},
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
    assert dec["note"] == "dated first" and dec["title"] == "Decisions waiting (4)"   # the kernel's count


def test_a_title_clause_the_note_already_carries_is_said_once() -> None:
    fn = _Counted([{"id": "inbox", "title": "Inbox (10 unread)", "note": "10 unread · 50 shown", "lines": []}])
    ck, _ = _cockpit(fn)
    assert snapshot(ck)["panels"]["ext:inbox"]["note"] == "10 unread · 50 shown"   # whole-segment match


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
        t0 = time.monotonic()
        for pid in ids:
            assert _http(f"{base}/cockpit/panel/{pid}.json?profile=full")[0] == 200
        if time.monotonic() - t0 < 4:            # inside one reuse window the burst is one call
            assert fn.n == 1 and httpd.external_panels.calls == 1


# --- L1 + L2 review findings, each reproduced before it was fixed ---------------------------------

def test_a_hung_callable_costs_one_wait_for_the_whole_manifest_not_one_per_panel() -> None:
    fn = _Counted()
    ck, ext = _cockpit(fn, wait_s=1.0)
    snapshot(ck)                                   # discovered healthy
    fn.gate.clear()                                # now it hangs
    ext._last = None                               # the reuse window is over
    t0 = time.monotonic()
    snap = snapshot(ck)
    took = time.monotonic() - t0
    assert took < 2.5, took                        # one shared deadline (was ~1 s per panel)
    assert all(snap["panels"][p]["status"] == "error" for p in snap["panels"] if p.startswith("ext:"))
    fn.gate.set()


def test_an_abandoned_flight_does_not_wedge_the_next_window_and_a_recovered_source_reads_again() -> None:
    forever = threading.Event()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] == 1:
            forever.wait(30)                       # the first call never answers in this test's lifetime
        return _panels()
    now = [0.0]
    ext = ExternalPanels(fn, reuse_s=5, wait_s=0.2, clock=lambda: now[0])
    assert ext.take().panels is None               # times out; that flight is abandoned, its thread parked
    now[0] = 10.0                                  # a later refresh starts a fresh flight
    got = ext.take()
    assert got.panels is not None and n["c"] == 2
    forever.set()


def test_parked_threads_are_bounded() -> None:
    fn = _Counted()
    fn.gate.clear()
    ext = ExternalPanels(fn, reuse_s=1, wait_s=0.05, max_parked=2, clock=time.monotonic)
    for _ in range(6):
        ext.take()
        ext._last = None
        time.sleep(0.06)
    assert ext.calls == 2 and "not starting another" in (ext.take().error or "")
    fn.gate.set()


def test_a_thread_that_cannot_start_is_a_fault_and_the_next_call_retries(monkeypatch) -> None:
    fn = _Counted()
    ext = ExternalPanels(fn, reuse_s=0)

    def boom(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, "start", boom)
    assert "could not start" in (ext.take().error or "")
    monkeypatch.undo()
    assert ext.take().panels is not None


def test_a_dash_title_is_a_name_and_a_live_clause_and_never_a_collision() -> None:
    fn = _Counted([{"id": "stream", "title": "Stream", "note": "", "lines": [{"meta": "a", "text": "x"}],
                    "empty": "no constellation episodes."}])
    ck, _ = _cockpit(fn, reuse_s=0)
    snapshot(ck)
    fn.panels = [{"id": "stream", "title": "Stream — no digest", "note": "⚠ no digest", "lines": [],
                  "empty": "constellation feed age unknown."}]
    snap = snapshot(ck)
    p = snap["panels"]["ext:stream"]
    assert snap["manifest"]["errors"] == []
    assert p["note"] == "no digest · ⚠ no digest" and p["title"].startswith("Stream")
    assert p["empty"] == "constellation feed age unknown."      # the source's sentence for THIS read


def test_the_empty_sentence_follows_the_source_per_read() -> None:
    fn = _Counted([{"id": "decisions", "title": "Decisions waiting", "lines": [],
                    "empty": "nothing is waiting on you."}])
    ck, _ = _cockpit(fn, reuse_s=0)
    assert snapshot(ck)["panels"]["ext:decisions"]["empty"] == "nothing is waiting on you."
    fn.panels = [{"id": "decisions", "title": "Decisions waiting", "lines": [],
                  "note": "⚠ 2 unreadable log line(s): this list may be INCOMPLETE",
                  "empty": "no readable decision rows — the log has unreadable lines."}]
    p = snapshot(ck)["panels"]["ext:decisions"]
    assert p["empty"].startswith("no readable decision rows") and "INCOMPLETE" in p["note"]


def test_a_note_segment_is_matched_whole_not_as_a_substring() -> None:
    fn = _Counted([{"id": "w", "title": "W (3 unread)", "note": "13 unread", "lines": []}])
    ck, _ = _cockpit(fn)
    assert snapshot(ck)["panels"]["ext:w"]["note"] == "3 unread · 13 unread"


def test_a_title_number_that_is_not_the_rows_drawn_is_kept() -> None:
    lines = [{"meta": "", "text": f"t{i}"} for i in range(12)]
    fn = _Counted([{"id": "tray", "title": "Tray (40)", "lines": lines},
                   {"id": "keep", "title": "Keep (2)", "lines": lines[:2]}])
    ck, _ = _cockpit(fn)
    snap = snapshot(ck)
    assert snap["panels"]["ext:tray"]["note"] == "40" and snap["panels"]["ext:tray"]["count"] == 12
    assert snap["panels"]["ext:keep"]["note"] == ""


def test_bounds_on_ids_titles_notes_and_meta() -> None:
    fn = _Counted([{"id": "a/b c", "title": "bad", "lines": []},
                   {"id": "ok", "title": "", "note": "n" * 5000, "lines": [{"meta": "m" * 5000, "text": "x"}]}])
    ck, _ = _cockpit(fn)
    snap = snapshot(ck)
    assert "ext:a/b c" not in snap["panels"]
    p = snap["panels"]["ext:ok"]
    assert p["title"].startswith("ok") and len(p["note"]) <= 500
    assert len(p["rows"][0]["facets"]["legacy_meta"]) <= 200


# --- L3 r1 findings ---------------------------------------------------------------------------------

def test_a_late_answer_never_replaces_a_newer_flights() -> None:
    release_a = threading.Event()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] == 1:
            release_a.wait(10)
            return [{"id": "old", "title": "Old", "lines": []}]
        return [{"id": "new", "title": "New", "lines": []}]
    now = [0.0]
    ext = ExternalPanels(fn, reuse_s=5, wait_s=0.2, clock=lambda: now[0])
    ext.take()                                     # flight A abandoned
    now[0] = 10.0
    b = ext.take()
    assert [p["id"] for p in b.panels] == ["new"]
    release_a.set()
    time.sleep(0.2)                                # A answers late
    assert [p["id"] for p in ext.take().panels] == ["new"]


def test_lines_that_are_not_a_list_are_an_error_never_empty() -> None:
    fn = _Counted([{"id": "x", "title": "X", "lines": {"bad": "shape"}, "empty": "nothing pending."}])
    ck, _ = _cockpit(fn)
    p = snapshot(ck)["panels"]["ext:x"]
    assert p["status"] == "error" and "not a list" in p["error"]


def test_an_exception_whose_text_raises_does_not_wedge_the_source() -> None:
    class Nasty(Exception):
        def __str__(self):
            raise ValueError("no text")
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] <= 3:
            raise Nasty()
        return _panels()
    ext = ExternalPanels(fn, reuse_s=0, max_parked=2)
    for _ in range(3):
        assert ext.take().error == "Nasty"
    assert ext.take().panels is not None


def test_one_manifest_build_is_one_call_even_past_the_reuse_window() -> None:
    fn = _Counted()
    ck, ext = _cockpit(fn, reuse_s=0)
    ck.register(ProviderSpec("slow", "line", "Slow", "feed",
                             lambda c: (time.sleep(0.05), Read(value={"lines": []}))[1]))
    from levain.cockpit.text import LOCAL_CREDENTIAL
    ck.manifest(LOCAL_CREDENTIAL)
    assert ext.calls == 1


def test_a_per_read_empty_sentence_survives_the_snapshot_going_stale() -> None:
    # the aging branch: a refresher alive, its last snapshot older than stale_after_s
    from datetime import datetime, timedelta, timezone

    from levain.cockpit.engine import _Snap
    ck = Cockpit()
    ck.register(ProviderSpec("r", "line", "R", "feed", lambda c: Read(value={"lines": []}),
                             refresh_every_s=60, stale_after_s=1))
    now = datetime.now(timezone.utc)
    st = ck._state["r"]
    st.snap = _Snap("empty", None, {"lines": []}, [], [], (now - timedelta(seconds=30)).isoformat(), None, "",
                    False, "live sentence")
    st.last_completion = now
    got = ck.panel("r")
    assert got["status"] == "stale" and got["empty"] == "live sentence", got


# --- L3 r2: the singleflight rule (an abandoned call answers only its own waiters) -----------------

def test_an_abandoned_answer_never_reaches_the_reuse_while_a_newer_flight_runs() -> None:
    release_a, release_b, b_inside = threading.Event(), threading.Event(), threading.Event()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] == 1:
            release_a.wait(10)
            return [{"id": "old", "title": "T", "lines": []}]
        b_inside.set()
        release_b.wait(10)
        return [{"id": "new", "title": "T", "lines": []}]
    now = [0.0]
    ext = ExternalPanels(fn, reuse_s=5, wait_s=0.2, clock=lambda: now[0])
    ext.take()                                     # A abandoned
    (flight_a,) = tuple(ext._parked)
    ext._wait_s = 5.0                              # B gets a wide deadline: no race with this test's steps
    now[0] = 10.0
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(ext.take()))
    t.start()
    assert b_inside.wait(5)                        # B is inside the callable
    release_a.set()
    assert flight_a.done.wait(5)                   # A has answered late
    with ext._lock:
        assert ext._last is not None and ext._last[1].panels is None   # still A's timeout: A published nothing
    release_b.set()
    t.join(5)
    assert [p["id"] for p in out[0].panels] == ["new"]


def test_every_reader_of_an_abandoned_flight_gets_that_flights_own_timeout() -> None:
    fn = _Counted()
    fn.gate.clear()
    now = [0.0]
    ext = ExternalPanels(fn, reuse_s=0, wait_s=0.2, clock=lambda: now[0])
    ext.take()                                     # A abandoned by its first reader
    with ext._lock:
        (flight_a,) = tuple(ext._parked)
    fn.gate.set()                                  # a newer flight B can now answer
    assert ext.take().panels is not None
    with ext._lock:                                # a second reader of A resumes after B published
        late = ext._abandon_locked(flight_a, now[0])
    assert late is flight_a.timeout and late.panels is None


def test_two_readers_of_one_flight_never_disagree_when_it_answers_after_its_deadline(monkeypatch) -> None:
    # reader 2 waits on the flight; reader 1 arrives past the deadline and gets its timeout; the call then
    # answers late and reader 2 resumes afterwards: both must hold the flight's one verdict, the timeout
    import levain.cockpit.external as external
    r2_waiting, release_r2 = threading.Event(), threading.Event()

    class _HeldDone(threading.Event):         # only this flight's reader-2 is held; no global patch
        def wait(self, timeout=None):
            if threading.current_thread().name == "reader-2":
                r2_waiting.set()
                release_r2.wait(5)
            return super().wait(timeout)

    class _Flight(external._Flight):
        def __init__(self, deadline: float) -> None:
            super().__init__(deadline)
            self.done = _HeldDone()
    monkeypatch.setattr(external, "_Flight", _Flight)
    fn = _Counted()
    fn.gate.clear()
    now = [0.0]
    ext = ExternalPanels(fn, reuse_s=0, wait_s=0.5, clock=lambda: now[0])
    out: dict[str, Any] = {}
    r2 = threading.Thread(target=lambda: out.__setitem__("r2", ext.take()), name="reader-2")
    r2.start()
    assert r2_waiting.wait(5)
    now[0] = 1.0                                   # past the flight's deadline
    r1 = ext.take()                                # reader 1: the flight's timeout
    assert r1.panels is None
    with ext._lock:
        (flight,) = tuple(ext._parked)
    fn.gate.set()                                  # the late answer lands
    assert threading.Event.wait(flight.done, 5)
    release_r2.set()
    r2.join(5)
    assert not r2.is_alive()
    assert out["r2"] is r1                         # one verdict for both readers


def test_an_answer_that_wins_the_lock_past_the_deadline_is_a_timeout_not_published() -> None:
    # the call answers after its deadline before any reader abandoned it: no reader and no reuse sees it
    now = [0.0]

    def fn():
        now[0] = 1.0                               # the deadline (0.5) passes while the call runs
        return _panels()
    ext = ExternalPanels(fn, reuse_s=10, wait_s=0.5, clock=lambda: now[0])
    got = ext.take()
    assert got.panels is None and "did not answer" in (got.error or "")
    assert ext._last is not None and ext._last[1] is got
    assert ext.take() is got                       # the reuse holds the timeout, not the late answer
    for _ in range(500):                           # the worker's cleanup may still be running
        if not ext._parked:
            break
        time.sleep(0.01)
    assert not ext._parked                         # the finished flight parks no thread


def test_an_answer_on_time_whose_publication_waits_on_the_lock_is_still_the_answer() -> None:
    # the call answers before its deadline; its worker then waits on the lock until past it (codex r5)
    now = [0.0]
    stamped = threading.Event()

    def clock() -> float:
        if threading.current_thread().name == "levain-external-panels":
            stamped.set()
        return now[0]
    fn = _Counted()
    fn.gate.clear()
    ext = ExternalPanels(fn, reuse_s=0, wait_s=0.5, clock=clock)
    out: dict[str, Any] = {}
    r = threading.Thread(target=lambda: out.__setitem__("r", ext.take()))
    r.start()
    while not fn.n:
        time.sleep(0.001)
    with ext._lock:                                # hold the lock across the deadline
        fn.gate.set()
        stamped.wait(1)                            # the worker reads the clock (before the deadline) ...
        time.sleep(0.02)                           # ... and then waits on the lock
        now[0] = 1.0
    r.join(5)
    assert not r.is_alive()
    assert out["r"].panels is not None and out["r"].error is None


def test_absent_or_null_lines_are_no_lines_not_an_error() -> None:
    fn = _Counted([{"id": "a", "title": "A", "empty": "none here."},
                   {"id": "b", "title": "B", "lines": None, "empty": "none here."}])
    ck, _ = _cockpit(fn)
    snap = snapshot(ck)
    assert [snap["panels"][p]["status"] for p in ("ext:a", "ext:b")] == ["empty", "empty"]
    assert snap["panels"]["ext:a"]["empty"] == "none here."
