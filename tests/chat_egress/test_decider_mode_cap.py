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


def test_with_no_launch_mode_passed_the_decider_reads_it_once_at_construction(rig) -> None:
    standing = {"mode": classify.OPEN_NETWORK}
    r = rig(setting=dm.LiveSetting(lambda: standing["mode"]))
    assert r.d.mode.launched == classify.OPEN_NETWORK


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
