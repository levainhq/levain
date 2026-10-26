"""Q-R15-9 (HEAD_RULINGS_R15): "tightening the standing default applies live; loosening applies from the next
launch". The decider's mode per request is the stricter of the live setting and the strictest mode the launch has
held, starting at the one it launched with (chd2fix, after S10 xi(4) measured a gated launch forwarding writes once
the standing default was flipped open; the ratchet after L3 r1: a revoke undone mid-launch is a loosening)."""

from __future__ import annotations

import time

import pytest

from levain.chat_egress import classify, decider as dm

from .decider_rig import Rig


@pytest.fixture
def rig(tmp_path):
    rigs: list[Rig] = []

    def make(**kw) -> Rig:
        r = Rig(tmp_path / f"r{len(rigs)}", **kw)
        rigs.append(r)
        return r

    yield make
    for r in rigs:
        assert r.close(), "a decider thread outlived the close"
    assert dm.service_threads() == []


def _until(cond, s: float = 5.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _action(v: list[dict]) -> str:
    return v[-1]["action"] if v else "none"


def test_loosening_mid_launch_does_not_apply_to_the_running_launch(rig) -> None:
    standing = {"mode": classify.GATED}
    r = rig(setting=dm.LiveSetting(lambda: standing["mode"]))
    standing["mode"] = classify.OPEN_NETWORK                 # the standing default loosened after the launch began
    p, rid = r.srt.req("POST", path="/after-loosen")
    assert _until(lambda: len(r.desk.filed) == 1), "a POST in a gated launch must card"
    assert r.srt.got(p) == []                                # held, not forwarded
    assert all(row.open_network is False for row in r.rows(rid))


def test_tightening_mid_launch_applies_live(rig) -> None:
    standing = {"mode": classify.OPEN_NETWORK}
    r = rig(setting=dm.LiveSetting(lambda: standing["mode"]))
    p, _ = r.srt.req("POST", path="/before")
    assert _action(r.srt.wait(p)) == "allow" and r.desk.filed == []
    standing["mode"] = classify.GATED                        # revoked mid-launch
    p2, _ = r.srt.req("POST", path="/after-revoke")
    assert _until(lambda: len(r.desk.filed) == 1)
    assert r.srt.got(p2) == []


def test_a_revoke_undone_mid_launch_stays_gated(rig) -> None:
    standing = {"mode": classify.OPEN_NETWORK}
    r = rig(setting=dm.LiveSetting(lambda: standing["mode"]))
    standing["mode"] = classify.GATED
    p, _ = r.srt.req("POST", path="/revoked")
    assert _until(lambda: len(r.desk.filed) == 1)
    standing["mode"] = classify.OPEN_NETWORK                 # undoing the revoke is a loosening: next launch only
    p2, rid2 = r.srt.req("POST", path="/reopened")
    assert _until(lambda: len(r.desk.filed) == 2), "a POST after the revoke was undone must still card"
    assert r.srt.got(p2) == []
    assert all(row.open_network is False for row in r.rows(rid2))


def test_the_launch_mode_passed_is_the_cap_and_no_second_read_is_made(rig) -> None:
    """start_egress passes the one read its header and digest came from: the decider caps at that value and makes
    no read of its own at construction (a second read could disagree with the digest). `LiveSetting` does not poll:
    its read thread is started only by a `get()`, so no read before the first request is the property asserted."""
    reads = {"n": 0}

    def read() -> str:
        reads["n"] += 1
        return classify.OPEN_NETWORK

    s = dm.LiveSetting(read)
    r = rig(setting=s, launch_mode=classify.GATED)
    assert reads["n"] == 0 and s.reader is None and r.d.mode.launched == classify.GATED
    p, _ = r.srt.req("POST", path="/w")
    assert _until(lambda: len(r.desk.filed) == 1)            # live says open; the launch said gated: a card
    assert reads["n"] >= 1                                   # the request read the live setting; the cap won


def test_the_decider_takes_the_launch_mode_and_never_reads_at_construction() -> None:
    """RULINGS 1026-10 Q3: `launch_mode` is keyword-only and required, so no constructor does a live read."""
    import inspect

    p = inspect.signature(dm.Decider.__init__).parameters["launch_mode"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty


def test_a_later_read_never_answers_looser_than_an_earlier_reads_tightening() -> None:
    """L3 r1 (codex 1; RULINGS 1026-10 Q1): request A's read returns gated; request B, arriving while A's read is in
    flight, reads after the standing default reopened. A is descheduled between its read returning and the ratchet
    (the seam below holds A there); B must still answer gated, because an earlier-started read returned gated."""
    import threading

    g, o = classify.GATED, classify.OPEN_NETWORK
    standing = {"mode": g}
    first_in, first_go = threading.Event(), threading.Event()
    calls = {"n": 0}

    def read() -> str:
        v = standing["mode"]                                 # the value as of the read's start
        calls["n"] += 1
        if calls["n"] == 1:
            first_in.set()
            first_go.wait(10)
        return v

    a_paused, a_go = threading.Event(), threading.Event()

    class DescheduledA(dm.LiveSetting):
        def get_ex(self):
            got = super().get_ex()
            if threading.current_thread().name == "A":
                a_paused.set()
                a_go.wait(10)
            return got

    c = dm.Capped(DescheduledA(read, timeout_s=5), o)
    out: dict[str, str] = {}
    ta = threading.Thread(target=lambda: out.__setitem__("A", c.get()), name="A")
    tb = threading.Thread(target=lambda: out.__setitem__("B", c.get()), name="B")
    ta.start()
    assert first_in.wait(5)                                  # A's read is in flight, about to answer gated
    tb.start()                                               # B arrives during A's read
    time.sleep(0.05)
    standing["mode"] = o                                     # the revoke is undone before B's own read starts
    first_go.set()
    assert a_paused.wait(5)
    tb.join(5)
    a_go.set()
    ta.join(5)
    assert calls["n"] == 2                                   # two real reads: A's (gated) and B's (open)
    assert out == {"A": g, "B": g}


def test_capped_is_the_stricter_of_the_two() -> None:
    assert set(dm.STRICTEST_FIRST) == set(classify.MODES)
    g, o = classify.GATED, classify.OPEN_NETWORK
    for launched, live, want in ((g, g, g), (g, o, g), (o, g, g), (o, o, o)):
        assert dm.Capped(dm.Fixed(live), launched).get() == want, (launched, live)
    assert dm.Capped(dm.Fixed("wide open"), o).get() == g     # an unknown live value is the strictest
    assert dm.Capped(dm.Fixed(o), "wide open").launched == g  # so is an unknown launch mode


def test_capped_ratchets_to_the_strictest_mode_seen() -> None:
    g, o = classify.GATED, classify.OPEN_NETWORK
    live = dm.Fixed(o)
    c = dm.Capped(live, o)
    assert c.get() == o
    live.value = g                                           # tightened: applies live
    assert c.get() == g
    live.value = o                                           # loosened again: never this launch
    assert c.get() == g and c.launched == o
    live.value = "wide open"
    assert c.get() == g


def test_capped_ratchet_holds_under_concurrent_gets() -> None:
    import threading

    g, o = classify.GATED, classify.OPEN_NETWORK
    live = dm.Fixed(g)
    c = dm.Capped(live, o)
    assert c.get() == g
    live.value = o
    got: list[str] = []
    ts = [threading.Thread(target=lambda: got.extend(c.get() for _ in range(200))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert got and set(got) == {g}


def test_capped_closes_and_reports_the_live_settings_reader() -> None:
    s = dm.LiveSetting(lambda: classify.OPEN_NETWORK)
    c = dm.Capped(s, classify.OPEN_NETWORK)
    assert c.get() == classify.OPEN_NETWORK
    c.close()
    assert c.get() == classify.GATED                         # the live setting fails closed after close

    class Stub:                                              # a fixed reader: LiveSetting's is alive only mid-read
        reader = object()

        def get(self) -> str:
            return classify.GATED

    assert dm.Capped(Stub(), classify.GATED).reader is Stub.reader


def _stalls_once(value: str):
    """A read of the standing default that stalls (past the setting's timeout) the first time, then reads `value`."""
    import threading

    release, calls = threading.Event(), {"n": 0}

    def read() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            release.wait(10)
        return value

    return read, release


def test_a_timed_out_read_gates_that_request_only(rig) -> None:
    """HEAD ruling (1026+8 p2f2): a read that timed out answers gated for THAT request and does not move the held
    mode; only a real read of the setting tightens the launch."""
    read, release = _stalls_once(classify.OPEN_NETWORK)
    r = rig(setting=dm.LiveSetting(read, timeout_s=0.3), launch_mode=classify.OPEN_NETWORK)
    p, _ = r.srt.req("POST", path="/during-stall")
    assert _until(lambda: len(r.desk.filed) == 1), "a request whose read timed out must card"
    release.set()                                            # the stalled read finishes; the next one is real
    p2, rid2 = r.srt.req("POST", path="/after-stall")
    assert _action(r.srt.wait(p2)) == "allow", "a timed-out read must not gate the rest of an open launch"
    assert len(r.desk.filed) == 1
    assert all(row.open_network is True for row in r.rows(rid2))


def test_capped_ratchets_only_on_a_real_read() -> None:
    g, o = classify.GATED, classify.OPEN_NETWORK
    read, release = _stalls_once(o)
    c = dm.Capped(dm.LiveSetting(read, timeout_s=0.2), o)
    assert c.get() == g                                      # timed out: gated for this get
    release.set()
    assert c.get() == o                                      # the held mode did not move
    bad = {"v": "wide open"}
    c2 = dm.Capped(dm.LiveSetting(lambda: bad["v"]), o)
    assert c2.get() == g                                     # a value outside the allowed modes: this get only
    bad["v"] = o
    assert c2.get() == o
    errs = {"raise": True}

    def flaky() -> str:
        if errs["raise"]:
            raise OSError("read failed")
        return o

    c3 = dm.Capped(dm.LiveSetting(flaky), o)
    assert c3.get() == g                                     # a read error: this get only
    errs["raise"] = False
    assert c3.get() == o


def test_fixed_reads_are_real() -> None:
    assert dm.Fixed(classify.GATED).get_ex() == (classify.GATED, True)


def _stalls_once_reading(standing: dict):
    """A read that captures the standing default when it starts and, the first time, stalls past the timeout before
    answering that captured value."""
    import threading

    release, calls = threading.Event(), {"n": 0}

    def read() -> str:
        v = standing["mode"]
        calls["n"] += 1
        if calls["n"] == 1:
            release.wait(10)
        return v

    return read, release


def test_a_timed_out_gated_read_finishing_late_does_not_tighten_the_launch() -> None:
    """P1 verify r2 (codex, introduced by Q1's watcher; RULINGS 1026-10 Q6): an open launch's read returns gated but
    only after it timed out for its request; the setting reopened meanwhile. The late answer reached no request, so
    it must not move the hold: the next fresh read answers open."""
    g, o = classify.GATED, classify.OPEN_NETWORK
    standing = {"mode": g}
    read, release = _stalls_once_reading(standing)
    s = dm.LiveSetting(read, timeout_s=0.2)
    c = dm.Capped(s, o)
    assert c.get() == g                                      # timed out: gated for this get alone
    standing["mode"] = o                                     # the setting reopens
    release.set()                                            # the stalled read completes with its captured gated
    assert _until(lambda: s.reader is None)
    assert c.get() == o


def test_a_timed_out_gated_read_finishing_late_leaves_the_launch_open(rig) -> None:
    """The same, through a running decider: the request whose read timed out cards; the stalled read then answers
    gated after the reopen; the next request is allowed."""
    standing = {"mode": classify.GATED}
    read, release = _stalls_once_reading(standing)
    s = dm.LiveSetting(read, timeout_s=0.3)
    r = rig(setting=s, launch_mode=classify.OPEN_NETWORK)
    p, _ = r.srt.req("POST", path="/during-stall")
    assert _until(lambda: len(r.desk.filed) == 1), "a request whose read timed out must card"
    standing["mode"] = classify.OPEN_NETWORK
    release.set()
    assert _until(lambda: s.reader is None)
    p2, rid2 = r.srt.req("POST", path="/after-stall")
    assert _action(r.srt.wait(p2)) == "allow", "a late answer no request accepted must not gate the launch"
    assert len(r.desk.filed) == 1
    assert all(row.open_network is True for row in r.rows(rid2))
