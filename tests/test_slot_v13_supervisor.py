"""V13 session 2: the supervisor's firewall self-probe (DESIGN_V13_REV2 §6; HEAD_RULINGS R2, R5, O2, N1, N4, N5).

T9: the launch probe sits between ``render`` and ``start``; any refusal, and an ``ok`` that is not ``closed``, means
no start and the one teardown. T10: the macOS re-probe inside the relay's select tick, its start-to-start schedule
and streak rule on an injected clock, and the relay against the fake helper while a probe runs.

The runtime helper is the fake script of ``test_slot_s2s_supervisor`` (its ``probe`` branch answers in the order
``FAKE_PROBES`` names). What only the VM shows (the real helper through sudo, pf, the Seatbelt) is V13's M/L rows.
"""
from __future__ import annotations

import inspect
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from levain.slot import supervisor as SV
from tests.test_slot_s2fx3_supervisor import rig as fx3_rig, stop_when
from tests.test_slot_s2s_supervisor import SESSION, Rig, bounded, isolated_git  # noqa: F401


def verbs(r: Rig) -> list:
    return [x["verb"] for x in r.verbs()]


# --- T9: the launch probe ---------------------------------------------------------------------------------------------

@bounded(60)
def test_launch_order_is_render_probe_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch)
    assert r.run() == 0
    v = r.verbs()
    assert [x["verb"] for x in v] == ["prepare", "render", "probe", "start", "teardown"]
    assert v[2]["args"] == [SESSION]
    err = r.stderr()
    assert "firewall self-probe not run" not in err
    assert "firewall self-probe: closed (api.anthropic.com 192.0.2.10 not reachable as the slot; 2 addresses)" in err


@bounded(60)
@pytest.mark.parametrize("name", ["firewall-open", "firewall-probe-inconclusive:dns",
                                  "firewall-probe-inconclusive:no-control-connect", "firewall-probe-unknown:child",
                                  "session-busy", "not-probeable:route"])
def test_a_launch_probe_refusal_means_no_start_and_one_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                name: str) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES=name)
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    err = r.stderr()
    assert f"refused {name}: runtime helper probe" in err
    assert "api.anthropic.com 192.0.2.10" in err            # the helper's stderr detail (O1), shown escaped


@bounded(60)
def test_an_ok_that_is_not_closed_is_refused_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES="malformed")
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    assert "refused firewall-probe-malformed" in r.stderr()


@bounded(60)
def test_a_launch_probe_past_its_deadline_means_no_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES="hang")
    r.seams.helper_deadline_s = {**SV.HELPER_DEADLINE_S, "probe": 0.5}
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    assert "refused helper-timeout" in r.stderr()


def test_the_probe_deadline_and_no_seam() -> None:
    assert SV.HELPER_DEADLINE_S["probe"] == 45.0
    assert "firewall_probe" not in {f.name for f in SV.Seams.__dataclass_fields__.values()}
    src = inspect.getsource(SV)
    assert "firewall self-probe not run" not in src


# --- T10: the schedule and the streak, on an injected clock ----------------------------------------------------------

def test_schedule_is_start_to_start_and_an_overrun_delays_the_next() -> None:
    rp = SV._Reprobe(100.0, 60.0)
    assert not rp.due(159.9) and rp.due(160.0)               # the first is due 60 s after the relay began
    rp.started(160.0)
    assert not rp.due(219.9) and rp.due(220.0)               # start to start, however long the run took
    rp.started(220.0)
    # a run still going at 280 + 10: the caller starts none while it runs, so the next starts at its end
    assert rp.due(290.0)
    rp.started(290.0)
    assert not rp.due(349.9) and rp.due(350.0)


@pytest.mark.parametrize("name,kind", [
    ("closed", "closed"), ("firewall-open", "open"), ("session-busy", "skipped"), ("not-probeable:BRAKED", "skipped"),
    ("firewall-probe-inconclusive:dns", "skipped"), ("firewall-probe-inconclusive:no-control-connect", "skipped"),
    ("firewall-probe-inconclusive:too-many", "skipped"), ("helper-timeout", "unknown"),
    ("helper-unrunnable", "unknown"), ("helper-output-malformed", "unknown"), ("helper-silent", "unknown"),
    ("firewall-probe-malformed", "unknown"), ("firewall-probe-unknown:child", "unknown"),
    ("firewall-probe-unknown:unreaped", "unknown"), ("slot-not-ours", "unknown"),
    ("not-probeable:alloc-changed", "unknown"), ("not-probeable:ACTIVE", "unknown"), ("internal", "unknown")])
def test_reprobe_class(name: str, kind: str) -> None:
    assert SV.reprobe_class(name) == kind


def test_two_unknowns_in_a_row_end_and_closed_or_skipped_reset_the_streak() -> None:
    rp = SV._Reprobe(0.0, 60.0)
    assert rp.finished("firewall-probe-unknown:child") is None
    assert rp.finished("closed") is None                     # 1 unknown, then closed: no end
    assert rp.finished("helper-timeout") is None
    assert rp.finished("session-busy") is None               # a skipped tick resets the streak too (N1)
    assert rp.finished("firewall-probe-inconclusive:dns") is None
    assert rp.finished("firewall-probe-unknown:child") is None
    assert rp.finished("helper-timeout") == "firewall-probe-unknown"


def test_open_ends_at_once_and_skipped_is_noted_once_per_streak() -> None:
    rp = SV._Reprobe(0.0, 60.0)
    for _ in range(5):
        assert rp.finished("firewall-probe-inconclusive:dns") is None
    assert rp.finished("closed") is None
    assert rp.finished("session-busy") is None
    assert sum("skipped" in n for n in rp.notes) == 2           # two streaks, one note each
    assert rp.finished("firewall-open", "firewall-open api.anthropic.com 192.0.2.10") == "firewall-open"
    assert rp.notes[-1] == "firewall self-probe: firewall-open: firewall-open api.anthropic.com 192.0.2.10"


def test_notes_are_bounded() -> None:
    rp = SV._Reprobe(0.0, 60.0)
    for _ in range(SV._NOTES_MAX + 5):
        rp.finished("helper-timeout")
        rp.finished("closed")
    assert len(rp.notes) == SV._NOTES_MAX and rp.dropped == 5


# --- T10: the relay against the fake helper ---------------------------------------------------------------------------

def mac_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, every: float = 0.2, **env: str) -> Rig:
    r = fx3_rig(tmp_path, monkeypatch, **env)
    r.seams.host = "darwin"
    r.seams.reprobe_s = every
    return r


def probes(r: Rig) -> int:
    try:
        return int((r.store / "probes").read_text())
    except (OSError, ValueError):
        return 0


def stop_after_probes(r: Rig, n: int, seen: dict | None = None) -> threading.Thread:
    """SIGTERM to this process once the fake helper has answered ``n`` probes (the launch one counted); ``seen`` gets
    the operator's stderr as it stood just before."""
    def go() -> None:
        deadline = time.monotonic() + 30
        while probes(r) < n and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)                               # the n-th run's answer is read on the next tick
        if seen is not None:
            seen["err"] = r.stderr()
        os.kill(os.getpid(), signal.SIGTERM)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


@bounded(60)
def test_reprobe_kills_on_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_PROBES="closed,closed,firewall-open")
    assert r.run() == 1
    v = r.verbs()
    assert [x["verb"] for x in v] == ["prepare", "render", "probe", "start", "probe", "probe", "teardown"]
    assert all(x["null0"] and x["fds"] == [0, 1, 2] and not x["tty"] for x in v[4:6])   # N5: stdin /dev/null
    err = r.stderr()
    assert f"firewall-open: ending session {SESSION}" in err
    assert "firewall self-probe: firewall-open: levain runtime-helper: refused firewall-open: " \
           "api.anthropic.com 192.0.2.10" in err
    assert f"session {SESSION} ended RETAINED" in err
    assert err.count("ended RETAINED") == 1                    # the one teardown


@bounded(60)
def test_two_unknowns_in_a_row_end_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_PROBES="closed,firewall-probe-unknown:child")
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "start", "probe", "probe", "teardown"]
    err = r.stderr()
    assert f"firewall-probe-unknown: ending session {SESSION}" in err
    assert "firewall self-probe unknown (firewall-probe-unknown:child), 2 in a row" in err


@bounded(60)
def test_unknown_then_closed_and_skipped_ticks_do_not_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq = ("closed,firewall-probe-unknown:child,closed,firewall-probe-unknown:child,session-busy,"
           "firewall-probe-inconclusive:dns,firewall-probe-unknown:child,closed")
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_PROBES=seq)
    seen: dict = {}
    stop_after_probes(r, 9, seen)
    assert r.run() == 1
    err = r.stderr()
    assert f"SIGTERM: ending session {SESSION}" in err
    assert "ending session" in err and "firewall-probe-unknown: ending" not in err
    assert err.count("firewall self-probe skipped") == 1    # one streak of two skipped ticks, said once
    assert "firewall self-probe" not in seen["err"].replace("firewall self-probe: closed", "")   # N5: held mid-relay


@bounded(60)
def test_relay_bytes_flow_and_a_winsize_poke_applies_while_a_slow_probe_runs(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="forever", FAKE_PROBES="closed,slow:2.5,closed")
    sup = SV.Supervisor(SV.Request(vendor="claude", repo=r.repo), r.seams)
    applied: list = []
    real = sup._winsize
    monkeypatch.setattr(sup, "_winsize", lambda m: (applied.append((r.store / "probing").exists()), real(m)))
    sizes: dict = {}

    def go() -> None:
        deadline = time.monotonic() + 30
        while not (r.store / "probing").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        sizes["a"] = r.out.stat().st_size
        sup._poke(b"w")
        time.sleep(1.0)
        sizes["b"] = r.out.stat().st_size
        sizes["still"] = (r.store / "probing").exists()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=go, daemon=True).start()
    try:
        assert sup.run() == 1
    finally:
        for fd in (r.out_fd, r.in_w, r.in_r):
            try:
                os.close(fd)
            except OSError:
                pass
    assert sizes["still"]                                    # the probe was still running a second later
    assert sizes["b"] > sizes["a"] + 65536                   # and the vendor's bytes kept reaching the terminal
    assert True in applied                                   # the poke was applied during the probe, not taken by it


@bounded(60)
def test_stop_mid_probe_ends_the_probe_and_tears_down_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_slot_s2fx3_supervisor import gone
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_PROBES="closed,hang")
    r.seams.kill_grace_s = 0.5
    stop_when(r.store / "pid-probe", 0.2)
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "start", "probe", "teardown"]
    assert gone(int((r.store / "pid-probe").read_text()))   # ended and reaped before teardown
    err = r.stderr()
    assert f"SIGTERM: ending session {SESSION}" in err and err.count("ended RETAINED") == 1


@bounded(60)
def test_a_reprobe_past_its_deadline_counts_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = mac_rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_PROBES="closed,hang")
    r.seams.helper_deadline_s = {**SV.HELPER_DEADLINE_S, "probe": 0.5}
    r.seams.kill_grace_s = 0.3
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "start", "probe", "probe", "teardown"]
    assert f"firewall-probe-unknown: ending session {SESSION}" in r.stderr()


@bounded(60)
def test_linux_runs_no_reprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = mac_rig(tmp_path, monkeypatch, every=0.05, FAKE_START="wait")
    r.seams.host = "linux"
    stop_when(r.store / "started", 1.5)
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "start", "teardown"]


# --- N4: an open wall's end is not held by a slow terminal ------------------------------------------------------------

def _trickle_reader(fd: int, stop: threading.Event) -> threading.Thread:
    def go() -> None:
        while not stop.is_set():
            try:
                os.read(fd, 512)
            except OSError:
                return
            time.sleep(0.1)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("reason,low,high", [("firewall-open", 0.8, 1.8), ("firewall-probe-unknown", 2.5, 6.0)])
def test_firewall_open_bounds_the_final_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
                                              low: float, high: float) -> None:
    monkeypatch.setattr(SV, "FINAL_DRAIN_MAX_S", 3.0)
    rfd, wfd = os.pipe()
    os.set_blocking(wfd, False)
    try:
        while True:
            os.write(wfd, b"x" * 65536)
    except BlockingIOError:
        pass                                                  # the pipe is full: the terminal is slow from here on
    sup = SV.Supervisor(SV.Request(vendor="claude", repo=tmp_path), SV.Seams(host="darwin", stdout=wfd,
                                                                               err=open(os.devnull, "w")))
    sup.end_reason = reason
    stop = threading.Event()
    _trickle_reader(rfd, stop)
    t0 = time.monotonic()
    try:
        left, _untaken, err = sup._last_output(bytearray(b"y" * (1 << 20)), b"")
    finally:
        stop.set()
        os.close(wfd)
        os.close(rfd)
    took = time.monotonic() - t0
    assert err is None and left > 0
    assert low <= took <= high
