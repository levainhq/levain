"""CH-D2 S5b: the decider service (`levain.chat_egress.decider`) through srt's framing, by hand (`decider_rig.SrtEnd`).

Rows (the S5b row of `1024-18/CHD2_BUILD_PLAN_DRAFT.md`): C1, R9-A4, R15-10-A4, R15-10-A7, R15-10-A13, R15-12-B8,
R15-12-B15, R15-19-1, R15-19-2; and the five items `1024-20/HANDOFF.md` carries for S5b. The same rows run against
the REAL glue and srt in `test_decider_glue.py`.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from levain.chat_egress import advert, classify, decider as dm, keys, strip, wire
from levain.chat_egress.lifecycle import Effect, Settled

from .decider_rig import Lookup, Rig

UP_CT = {"content-type": ["application/x-git-upload-pack-request"],
         "accept": ["application/x-git-upload-pack-result"]}


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


def action(v: list[dict]) -> str:
    return v[-1]["action"] if v else "none"


# --- R15-10-A13: a thread per request ------------------------------------------------------------------------------

def test_two_held_cards_do_not_stall_a_get(rig) -> None:
    r = rig()
    c1, _ = r.srt.req("POST", path="/a")
    c2, _ = r.srt.req("POST", path="/b")
    for _ in range(100):
        if len(r.desk.filed) == 2:
            break
        time.sleep(0.02)
    assert len(r.desk.filed) == 2
    t0 = time.monotonic()
    g, rid = r.srt.req("GET", path="/c")
    assert action(r.srt.wait(g)) == "allow"
    assert time.monotonic() - t0 < 2.0
    assert r.srt.got(c1) == [] and r.srt.got(c2) == []          # still held
    r.desk.answer["card-1"] = "allowed"
    r.desk.answer["card-2"] = "denied"
    assert action(r.srt.wait(c1)) == "allow"
    v2 = r.srt.wait(c2)
    assert action(v2) == "deny" and v2[-1]["status"] == 403
    assert r.kinds(rid) == ["egress_attempt"]


# --- R15-19-1: the observation channel is drained at the decision ---------------------------------------------------

def test_an_advertisement_already_in_the_socket_is_seen_at_the_decision_with_no_reader_thread(rig) -> None:
    r = rig(start_obs=False)
    g, rid = r.srt.req("GET", path="/r.git/info/refs", query="service=git-upload-pack")
    assert action(r.srt.wait(g)) == "allow"
    r.obs_line(wire.obs_response_line(rid, 200, advert.ADVERTISEMENT_TYPE, None))
    p, prid = r.srt.req("POST", path="/r.git/git-upload-pack", headers=UP_CT | {"content-length": ["9"]})
    v = r.srt.wait(p)
    assert v == [{"t": "verdict", "id": p, "action": "need_body", "max": dm.Limits().card_body_cap}]
    r.srt.body(p, b"0009done\n")
    assert action(r.srt.wait(p, 2)) == "allow"
    assert r.desk.filed == []                                  # zero cards
    assert r.kinds(rid) == ["egress_attempt", "egress_outcome"]


def test_a_post_whose_advertisement_is_not_drained_is_a_card_never_a_reach(rig) -> None:
    r = rig(start_obs=False)
    p, _ = r.srt.req("POST", path="/r.git/git-upload-pack", headers=UP_CT | {"content-length": ["9"]})
    v = r.srt.wait(p)
    assert v[0]["action"] == "need_body"                        # the card spools its body first
    r.srt.body(p, b"0009done\n")
    for _ in range(100):
        if r.desk.filed:
            break
        time.sleep(0.02)
    assert r.desk.filed[0]["why"] == "body" and r.srt.got(p) == v


# --- R15-19-2: the spool, in decoded bytes, before any verdict -------------------------------------------------------

def _advertise(r: Rig, path: str = "/r.git") -> None:
    g, rid = r.srt.req("GET", path=path + "/info/refs", query="service=git-upload-pack")
    assert action(r.srt.wait(g)) == "allow"
    r.obs_line(wire.obs_response_line(rid, 200, advert.ADVERTISEMENT_TYPE, None))


def test_an_upload_pack_body_over_two_mib_decoded_is_a_card_and_nothing_is_allowed(rig) -> None:
    r = rig()
    _advertise(r)
    p, _ = r.srt.req("POST", path="/r.git/git-upload-pack", headers=UP_CT | {"transfer-encoding": ["chunked"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"x" * (advert.UPLOAD_PACK_BODY_CAP + 1))
    for _ in range(100):
        if r.desk.filed:
            break
        time.sleep(0.02)
    card = r.desk.filed[0]
    assert card["body_len"] == advert.UPLOAD_PACK_BODY_CAP + 1 and card["body_sha256"].startswith("sha256:")
    assert [x["action"] for x in r.srt.got(p)] == ["need_body"]   # no allow, no deny: the card holds


def test_exactly_the_cap_is_silent_and_one_byte_chunks_count_decoded(rig) -> None:
    r = rig()
    _advertise(r)
    p, _ = r.srt.req("POST", path="/r.git/git-upload-pack", headers=UP_CT | {"transfer-encoding": ["chunked"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    time.sleep(0.2)
    assert [x["action"] for x in r.srt.got(p)] == ["need_body"]   # nothing before the body
    r.srt.body(p, b"y" * 1_900_000)                              # srt hands decoded bytes: 1,900,000 counts as such
    assert action(r.srt.wait(p, 2)) == "allow" and r.desk.filed == []


def test_a_cut_spool_is_503_body_cap_and_no_card(rig) -> None:
    r = rig()
    p, rid = r.srt.req("POST", path="/up", headers={"content-type": ["application/octet-stream"],
                                                    "transfer-encoding": ["chunked"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"z" * 100, cut=True)
    v = r.srt.wait(p, 2)
    assert v[-1]["action"] == "deny" and v[-1]["status"] == 503 and v[-1]["reason"] == "wall: body_cap"
    assert r.desk.filed == []
    assert r.rows(rid)[0].deciding_step == "wall:body_cap"


# --- R15-10-A7: body deadlines are the decider's; a cap on concurrent spools -----------------------------------------

def test_stalled_bodies_become_body_timeout_walls_while_a_get_completes(rig) -> None:
    lim = dm.Limits(card_poll_s=0.05, body_timeout_s=3.0, max_body_readers=2, stop_drain_s=0.5)
    r = rig(limits=lim)
    stalled = [r.srt.req("POST", path=f"/s{i}", headers={"transfer-encoding": ["chunked"]})[0] for i in range(3)]
    time.sleep(0.2)
    g, _ = r.srt.req("GET", path="/ok")
    t0 = time.monotonic()
    assert action(r.srt.wait(g)) == "allow" and time.monotonic() - t0 < 2.0
    finals = [r.srt.wait(s, 2, timeout=8)[-1] for s in stalled]
    reasons = sorted(f["reason"] for f in finals if f["action"] == "deny")
    assert reasons == ["wall: body_readers", "wall: body_timeout", "wall: body_timeout"]
    assert sorted(f["status"] for f in finals) == [408, 408, 503]
    t1 = time.monotonic()
    assert r.close() and time.monotonic() - t1 < 3.0


def test_an_ended_line_while_the_body_is_awaited_is_ended_never_body_timeout(rig, monkeypatch) -> None:
    """ENDED fires before the spool is woken: a slow ENDED transition never reads as a body_timeout deny."""
    real = dm.Decider._fire

    def slow_ended(self, req, event):
        if event is dm.Event.ENDED:
            time.sleep(0.5)
        return real(self, req, event)

    monkeypatch.setattr(dm.Decider, "_fire", slow_ended)
    r = rig(limits=dm.Limits(card_poll_s=0.05, body_timeout_s=5.0, stop_drain_s=0.5))
    p, rid = r.srt.req("POST", path="/s", headers={"transfer-encoding": ["chunked"]})
    first = r.srt.wait(p)
    assert first[0]["action"] == "need_body", first
    r.obs_line(wire.obs_ended_line(rid))
    v = r.srt.wait(p, 2)
    assert v[-1]["action"] == "deny" and v[-1]["reason"] == "decider: the client left before a verdict"
    assert r.records.settled(r.launch, rid) is Settled.ENDED


# --- R9-A4: caps --------------------------------------------------------------------------------------------------

def test_cards_past_the_cap_are_503_card_cap_and_none_queued(rig) -> None:
    r = rig(limits=dm.Limits(card_poll_s=0.05, max_cards=2, stop_drain_s=0.5))
    ids = [r.srt.req("POST", path=f"/c{i}")[0] for i in range(4)]
    time.sleep(0.5)
    denied = [i for i in ids if action(r.srt.got(i)) == "deny"]
    assert len(denied) == 2 and len(r.desk.filed) == 2
    assert all(r.srt.got(i)[-1]["status"] == 503 and r.srt.got(i)[-1]["reason"] == "wall: card_cap" for i in denied)


def test_a_stop_with_held_cards_completes_inside_its_bound(rig) -> None:
    r = rig(limits=dm.Limits(card_poll_s=0.05, stop_drain_s=0.3))
    ids = [r.srt.req("POST", path=f"/h{i}")[1] for i in range(5)]
    for _ in range(100):
        if len(r.desk.filed) == 5:
            break
        time.sleep(0.02)
    t0 = time.monotonic()
    assert r.close()
    assert time.monotonic() - t0 < 2.0
    assert dm.service_threads() == []
    assert sorted(r.desk.ended) == sorted(f"card-{i}" for i in range(1, 6))
    for rid in ids:
        assert r.records.settled(r.launch, rid) is Settled.UNDECIDED


# --- R15-10-A4 / R15-12-B15: the decider resolves before any card ------------------------------------------------------

def test_a_post_to_a_name_answering_loopback_walls_and_files_no_card(rig) -> None:
    r = rig(lookup=Lookup({"localtest.me": ["127.0.0.1"], "mixed.test": ["93.184.216.34", "10.0.0.1"]}))
    for host in ("localtest.me", "mixed.test"):
        p, _ = r.srt.req("POST", host=host, path="/x")
        v = r.srt.wait(p)
        assert v[-1]["action"] == "deny" and v[-1]["reason"] == "wall: non_public_address", host
    assert r.desk.filed == []


def test_a_hung_resolve_denies_that_request_and_another_host_completes(rig) -> None:
    r = rig(lookup=Lookup(hang={"slow.test"}), resolver_timeout=3.0)
    s, srid = r.srt.req("GET", host="slow.test")
    time.sleep(0.05)
    g, _ = r.srt.req("GET", host="fast.test")
    t0 = time.monotonic()
    assert action(r.srt.wait(g)) == "allow" and time.monotonic() - t0 < 2.0
    v = r.srt.wait(s, timeout=6)
    assert v[-1]["status"] == 502 and v[-1]["reason"] == "wall: resolve_timeout"
    assert r.rows(srid)[0].deciding_step == "wall:resolve_timeout"


# --- R15-12-B8: the decider is total ---------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"query": "a=%zz"}, {"query": "a=%ff%fe"}, {"query": "a\x00=1"}, {"path": "/a\x00b"},
    {"path": "/a%zzb"}, {"path": "/a%e"}, {"headers": {"x-a\x00b": ["1"]}}, {"headers": {"Upper": ["1"]}},
    {"headers": {"x": [1]}}, {"method": ""}, {"port": 0},
])
def test_a_malformed_frame_is_one_well_formed_deny_and_the_next_get_is_served(rig, bad) -> None:
    r = rig()
    r.srt.next_id += 1
    f = {"t": "req", "id": r.srt.next_id, "method": "GET", "host": "ok.test", "port": 443, "sni": "ok.test",
         "path": "/", "headers": {wire.AUDIT_HEADER: ["0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"]}}
    for k, v in bad.items():
        f[k] = (f.get(k) or {}) | v if k == "headers" else v
    r.srt.send(f)
    v = r.srt.wait(r.srt.next_id)
    assert len(v) == 1 and v[0]["action"] == "deny" and 400 <= v[0]["status"] <= 599
    wire.deny_frame(v[0]["id"], v[0]["status"], v[0]["reason"])   # well-formed by srt's own schema
    g, _ = r.srt.req("GET", path="/next")
    assert action(r.srt.wait(g)) == "allow"


def test_an_exception_inside_a_decision_denies_that_request_only(rig, monkeypatch) -> None:
    r = rig()
    real = classify.decide

    def boom(parsed, *a, **kw):
        if parsed.path == "/boom":
            raise RuntimeError("seam")
        return real(parsed, *a, **kw)

    monkeypatch.setattr(classify, "decide", boom)
    b, rid = r.srt.req("GET", path="/boom")
    v = r.srt.wait(b)
    assert v[-1]["action"] == "deny" and v[-1]["status"] == 500
    assert r.rows(rid)[0].deciding_step == "internal"
    g, _ = r.srt.req("GET", path="/fine")
    assert action(r.srt.wait(g)) == "allow"


def test_audit_missing_is_denied_with_its_row(rig) -> None:
    r = rig()
    a, _ = r.srt.req("GET", rid=None)
    assert r.srt.wait(a)[-1]["action"] == "deny"
    rows = r.rows()
    assert [(x.request_id, x.deciding_step, x.attach.value) for x in rows] == [(None, "audit-missing",
                                                                                  "not-evaluated")]


# --- the handoff items ------------------------------------------------------------------------------------------------

def test_a_stray_allow_through_the_composer_is_denied(rig) -> None:
    r = rig()
    p, rid = r.srt.req("POST", path="/x", headers={"content-length": ["3"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    with r.d._live_lock:
        req = r.d._live[rid]
    req.strip = strip.strip(req.parsed)
    r.d._allow(req)                                             # an allow while the body is awaited
    v = r.srt.wait(p, 2)
    assert v[-1]["action"] == "deny" and v[-1]["reason"] == "decider: an allow arrived where none may"
    assert r.d.counters["stray_allow"] == 1


def test_close_drains_the_observation_channel_before_closing_requests(rig) -> None:
    r = rig(start_obs=False)
    g, rid = r.srt.req("GET", path="/done")
    assert action(r.srt.wait(g)) == "allow"
    r.obs_line(wire.obs_response_line(rid, 200, "text/plain", "2"))
    assert r.close()
    assert r.records.settled(r.launch, rid) is Settled.COMPLETED


def test_an_ended_card_releases_srt_and_ends_the_card(rig) -> None:
    r = rig()
    p, rid = r.srt.req("POST", path="/held")
    for _ in range(100):
        if r.desk.filed:
            break
        time.sleep(0.02)
    r.obs_line(wire.obs_ended_line(rid))
    v = r.srt.wait(p)
    assert v[-1]["action"] == "deny" and v[-1]["reason"] == "decider: the client left before a verdict"
    for _ in range(100):
        if r.desk.ended:
            break
        time.sleep(0.02)
    assert r.desk.ended == ["card-1"]
    assert r.records.settled(r.launch, rid) is Settled.ENDED
    r.desk.answer["card-1"] = "allowed"                         # a late allow forwards nothing (C9)
    time.sleep(0.3)
    assert len(r.srt.got(p)) == 1


def test_an_ended_line_before_its_request_frame_is_applied(rig) -> None:
    r = rig(start_obs=False)
    rid = "11111111-2222-4333-8444-555555555555"
    r.obs_line(wire.obs_ended_line(rid))
    r.d.drain()
    p, _ = r.srt.req("POST", path="/x", rid=rid)
    v = r.srt.wait(p)
    assert v[-1]["action"] == "deny" and r.desk.filed == []
    assert r.records.settled(r.launch, rid) is Settled.ENDED


@pytest.mark.parametrize("left, expect", [(0, "owed-deny"), (5, "cut"), (None, "left")])
def test_a_raised_send_allow_measures_what_left(rig, left, expect) -> None:
    armed = {"on": False}

    def fault(n: int):
        if armed["on"]:
            armed["on"] = False
            return n if left is None else left
        return None

    r = rig(link_fault=fault)
    if left is None:                                             # the whole frame left, then the send raised
        orig = r.d.link.send_bytes

        def whole_then_raise(frame: bytes) -> None:
            if armed["on"]:
                armed["on"] = False
                orig(frame)
                raise dm.SendFailed(len(frame), len(frame), "test seam: after the whole frame")
            orig(frame)

        r.d.link.send_bytes = whole_then_raise
    armed["on"] = True
    g, rid = r.srt.req("GET", path="/f")
    time.sleep(0.5)
    v = r.srt.got(g)
    rows = r.rows(rid)
    assert [x.kind for x in rows] == ["egress_attempt", "egress_failed"]
    assert rows[1].failed_effect == Effect.SEND_ALLOW.value
    assert r.records.settled(r.launch, rid) is Settled.AMBIGUOUS
    if expect == "owed-deny":
        assert [x["action"] for x in v] == ["deny"] and v[0]["status"] == 503
    elif expect == "cut":
        assert r.srt.eof and v == [] and len(r.srt.raw_tail) == 1          # 4 header bytes + 1 body byte left
        assert r.d.link.dead == "a frame was cut mid-send"
    else:
        assert [x["action"] for x in v] == ["allow"]


@pytest.mark.parametrize("left, expect", [(0, "owed-deny"), (5, "cut")])
def test_a_raised_release_measures_what_left(rig, left, expect) -> None:
    armed = {"on": False}

    def fault(n: int):
        if armed["on"]:
            armed["on"] = False
            return left
        return None

    r = rig(link_fault=fault)
    p, rid = r.srt.req("POST", path="/held")
    for _ in range(100):
        if r.desk.filed:
            break
        time.sleep(0.02)
    assert r.desk.filed
    armed["on"] = True
    r.obs_line(wire.obs_ended_line(rid))
    time.sleep(0.5)
    v = r.srt.got(p)
    failed = [x for x in r.rows(rid) if x.kind == "egress_failed"]
    assert [x.failed_effect for x in failed] == [Effect.RELEASE.value]
    assert r.desk.ended == ["card-1"]
    if expect == "owed-deny":
        assert [(x["action"], x["status"]) for x in v] == [("deny", 503)]
    else:
        assert r.srt.eof and v == [] and len(r.srt.raw_tail) == 1
        assert r.d.link.dead == "a frame was cut mid-send"


# --- C1: the standing default, read live -------------------------------------------------------------------------------

def test_a_stale_read_of_the_standing_default_never_reopens_and_close_arms_nothing() -> None:
    gate = threading.Event()
    calls = {"n": 0}
    values = ["open network", "gated"]

    def read() -> str:
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            gate.wait(5)                                         # the old read: started before the revoke
        return values[min(n - 1, 1)]

    s = dm.LiveSetting(read, timeout_s=3)
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("a", s.get()))
    t.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=lambda: out.setdefault("b", s.get()))   # arrives after the revoke
    t2.start()
    time.sleep(0.1)
    gate.set()
    t.join(5)
    t2.join(5)
    assert out == {"a": "open network", "b": "gated"}             # B never answered from the read A started
    s.close()
    calls["n"] = 0
    assert s.get() == "gated" and calls["n"] == 0                 # after close: no read, fail closed


def test_a_setting_that_errors_or_answers_garbage_fails_closed() -> None:
    assert dm.LiveSetting(lambda: (_ for _ in ()).throw(OSError("x"))).get() == "gated"
    assert dm.LiveSetting(lambda: "wide open").get() == "gated"
    assert dm.LiveSetting(lambda: "open network").get() == "open network"


def test_open_network_forwards_an_unkeyed_post_silently(rig) -> None:
    r = rig(mode="open network")
    p, _ = r.srt.req("POST", path="/w")
    assert action(r.srt.wait(p)) == "allow" and r.desk.filed == []


# --- L3 r1 (1025+16 RULINGS_R1): the link, admission, audit ids, the form, the card, the body ------------------------

def _until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() >= end:
            return False
        time.sleep(0.01)
    return True


def test_fix1_a_close_while_a_send_is_blocked_on_a_full_buffer_returns_inside_its_bound(rig) -> None:
    """srt stops reading: a send blocks holding the write lock. kill (and so close) never waits on that lock."""
    r = rig(srt_reads=False)
    err: dict = {}

    def big() -> None:
        try:
            r.d.link.send_bytes(b"\0" * (32 << 20))         # past any socket buffer: it blocks
        except dm.SendFailed as exc:
            err["e"] = exc

    t = threading.Thread(target=big, daemon=True)
    t.start()
    assert _until(r.d.link._wlock.locked)
    out: dict = {}
    c = threading.Thread(target=lambda: out.setdefault("ok", r.d.close(timeout_s=2)), daemon=True)
    try:
        c.start()
        c.join(4)
        assert not c.is_alive() and out["ok"] is True
        t.join(2)
        assert isinstance(err.get("e"), dm.SendFailed) and r.d.link.dead == "the launch closed"
    finally:
        r.srt.sock.close()                                  # unblocks the old code's send so the rig can close


def test_fix2_a_need_body_cut_mid_send_cuts_the_link_and_nothing_follows(rig) -> None:
    armed = {"on": False}

    def fault(n: int):
        if armed["on"]:
            armed["on"] = False
            return 5
        return None

    r = rig(link_fault=fault)
    armed["on"] = True
    p, rid = r.srt.req("POST", path="/x", headers={"content-length": ["3"]})
    with r.srt.cv:
        r.srt.cv.wait_for(lambda: r.srt.eof, timeout=5)
    assert r.srt.eof and r.srt.got(p) == [] and len(r.srt.raw_tail) == 1   # 4 header bytes + 1: nothing after
    assert r.d.link.dead is not None


class _PausedSlots:
    """`Decider._decisions` with its acquire held at `go` (the link thread paused past the closing check); records
    whether close() had begun (`_closing`) when the pause ended."""

    def __init__(self, real, decider) -> None:
        self.real, self.decider, self.reached, self.go = real, decider, threading.Event(), threading.Event()
        self.closing_at_resume: bool | None = None

    def acquire(self, blocking: bool = True) -> bool:
        self.reached.set()
        self.go.wait(2)
        self.closing_at_resume = self.decider._closing.is_set()
        return self.real.acquire(blocking=blocking)

    def release(self) -> None:
        self.real.release()


def test_fix3_close_waits_for_an_admission_in_progress(rig) -> None:
    """The pause ends on a timer while close() runs: close may not begin (set `_closing`, take its snapshot) while a
    request is between the closing check and its thread's start."""
    r = rig()
    slots = _PausedSlots(r.d._decisions, r.d)
    r.d._decisions = slots
    p, rid = r.srt.req("POST", path="/late")
    assert slots.reached.wait(5)
    timer = threading.Timer(0.3, slots.go.set)
    timer.start()
    try:
        ok = r.d.close(timeout_s=4)
    finally:
        timer.cancel()
        slots.go.set()
    assert slots.closing_at_resume is False, "close began while an admission was in progress"
    assert ok, "a decision admitted during close outlived it"
    assert r.records.settled(r.launch, rid) is Settled.UNDECIDED
    assert sorted(r.desk.ended) == sorted(f"card-{i + 1}" for i in range(len(r.desk.filed)))


def test_fix3_a_decision_admitted_before_the_snapshot_and_registered_after_it_is_closed(rig, monkeypatch) -> None:
    """The snapshot window: the decision thread is admitted (in close's snapshot) but pauses before it registers until
    close() has taken its snapshot (mode.close comes after it); it is then CLOSED, files no card, and is joined."""
    reached, go = threading.Event(), threading.Event()
    real = keys.lookup

    def paused(routes, host):
        if host == "late.test":
            reached.set()
            go.wait(3)
        return real(routes, host)

    monkeypatch.setattr(keys, "lookup", paused)
    r = rig()

    class Mode:
        def get(self) -> str:
            return "gated"

        def close(self) -> None:
            go.set()

    r.d.mode = Mode()
    p, rid = r.srt.req("POST", host="late.test", path="/late")
    assert reached.wait(5)
    ok = r.d.close(timeout_s=4)
    go.set()
    assert ok, "a decision admitted during close outlived it"
    assert r.records.settled(r.launch, rid) is Settled.UNDECIDED and r.desk.filed == []


@pytest.mark.parametrize("cts, method", [
    (["application/json", "application/x-www-form-urlencoded"], "POST"),
    (["application/x-www-form-urlencoded, application/json"], "POST"),
    (["application/x-www-form-urlencoded"], "GET"),
])
def test_fix4_a_form_override_is_read_only_under_one_form_content_type(rig, cts, method) -> None:
    r = rig()
    p, _ = r.srt.req("POST", path="/f", headers={"content-type": cts, "content-length": ["11"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"_method=GET")
    assert _until(lambda: bool(r.desk.filed))
    assert r.desk.filed[0]["method"] == method


def test_fix5_an_audit_id_is_spent_for_the_launch(rig) -> None:
    r = rig(start_obs=False)
    rid = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"
    g, _ = r.srt.req("GET", path="/once", rid=rid)
    assert action(r.srt.wait(g)) == "allow"
    r.obs_line(wire.obs_response_line(rid, 200, "text/plain", "2"))
    r.d.drain()
    assert r.records.settled(r.launch, rid) is Settled.COMPLETED
    g2, _ = r.srt.req("GET", path="/once", rid=rid)
    v = r.srt.wait(g2)
    assert [(x["action"], x["reason"]) for x in v] == [("deny", "decider: a second request with one audit value")]


def test_fix5_an_attempt_write_that_is_refused_fails_before_any_allow(rig, monkeypatch) -> None:
    r = rig()
    monkeypatch.setattr(r.records, "write_attempt", lambda *a, **kw: False)
    g, _ = r.srt.req("GET", path="/refused")
    v = r.srt.wait(g)
    assert [x["action"] for x in v] == ["deny"] and v[0]["status"] == 503


def test_fix6_an_owed_deny_that_sends_nothing_cuts_the_link(rig) -> None:
    armed = {"n": 0}

    def fault(n: int):
        if armed["n"]:
            armed["n"] -= 1
            return 0
        return None

    r = rig(link_fault=fault)
    armed["n"] = 2                                          # the allow and then the owed deny send 0 bytes
    g, _ = r.srt.req("GET", path="/f")
    with r.srt.cv:
        r.srt.cv.wait_for(lambda: r.srt.eof, timeout=5)
    assert r.srt.eof and r.srt.got(g) == []
    assert r.d.link.dead is not None


def test_fix10_a_form_that_reclassifies_silent_is_allowed(rig, monkeypatch) -> None:
    """Seam: no real input reclassifies silent today (FIX_RETURN, VERIFY 10); the path is still the silent one."""
    import dataclasses
    real = classify.decide

    def seam(parsed, *a, **kw):
        v = real(parsed, *a, **kw)
        return dataclasses.replace(v, kind="silent", why=None) if kw.get("form_params") is not None else v

    monkeypatch.setattr(classify, "decide", seam)
    r = rig()
    p, rid = r.srt.req("POST", path="/f", headers={"content-type": ["application/x-www-form-urlencoded"],
                                                   "content-length": ["3"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"a=1")
    assert action(r.srt.wait(p, 2)) == "allow" and r.desk.filed == []
    assert r.rows(rid)[0].deciding_step == "silent"


def test_fix11_a_card_the_hold_expires_is_withdrawn(rig) -> None:
    r = rig(limits=dm.Limits(card_poll_s=0.05, card_hold_s=0.3, stop_drain_s=0.5))
    p, rid = r.srt.req("POST", path="/held")
    v = r.srt.wait(p)
    assert v[-1]["action"] == "deny" and v[-1]["reason"] == "card: expired"
    assert _until(lambda: r.desk.ended == ["card-1"])


def test_fix11_a_body_past_the_cap_srt_did_not_cut_is_body_cap(rig) -> None:
    r = rig(limits=dm.Limits(card_poll_s=0.05, card_body_cap=10, stop_drain_s=0.5))
    p, rid = r.srt.req("POST", path="/big", headers={"content-type": ["application/octet-stream"],
                                                     "content-length": ["11"]})
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"x" * 11)
    v = r.srt.wait(p, 2)
    assert v[-1]["action"] == "deny" and v[-1]["reason"] == "wall: body_cap" and r.desk.filed == []


def test_fix12_an_early_ended_row_has_the_decided_rows_path_shape(rig) -> None:
    route = keys.RouteSnapshot(record_id="rec-1", canonical_host="ok.test", credential_source="svc",
                               declared_repos=frozenset({"/r.git"}), attach=False, credential_account=None,
                               incarnation=None)
    r = rig(start_obs=False, routes=keys.RouteSet.of([route]))
    g, grid = r.srt.req("GET", path="/plain")
    assert action(r.srt.wait(g)) == "allow"
    rid = "11111111-2222-4333-8444-555555555555"
    r.obs_line(wire.obs_ended_line(rid))
    r.d.drain()
    p, _ = r.srt.req("GET", path="/plain", rid=rid)
    r.srt.wait(p)
    assert r.records.settled(r.launch, rid) is Settled.ENDED
    assert r.rows(rid)[0].path == r.rows(grid)[0].path


def test_fix9_close_wakes_a_waiting_setting_read_and_reports_a_reader_that_lives(rig) -> None:
    hang = threading.Event()
    s = dm.LiveSetting(lambda: (hang.wait(30), "gated")[1], timeout_s=20)
    r = rig()
    r.d.mode = s
    g, _ = r.srt.req("GET", path="/m")
    assert _until(lambda: any(t.name == "levain-egress-setting" for t in threading.enumerate()))
    t0 = time.monotonic()
    ok = r.d.close(timeout_s=1.5)
    assert not ok and time.monotonic() - t0 < 3.0
    assert [t.name for t in dm.service_threads()] == ["levain-egress-setting"]   # the waiting decision was woken
    hang.set()


# --- L3 r2 (1025+16 RULINGS_R2): close cuts first, the audit-id cap, the verdict kinds, the route lookup -------------

def _fill_send_buffer(sock: socket.socket) -> None:
    """srt stopped reading long ago: the decider's send buffer is full (junk below any frame boundary srt would read;
    it never reads again). The fd is non-blocking only while it fills (macOS ignores MSG_DONTWAIT on this socket)."""
    sock.setblocking(False)
    try:
        for size in (65536, 1024, 64, 1):
            while True:
                try:
                    sock.send(b"\0" * size)
                except BlockingIOError:
                    break
    finally:
        sock.setblocking(True)


def test_r2_fix1_close_with_a_decision_blocked_in_its_verdict_send_returns_inside_its_bound(rig) -> None:
    """Through the request path: a GET's SEND_ALLOW blocks on the full buffer inside its transition (holding the
    request's transition lock). close() cuts the link before it fires CLOSE, so it never waits on that lock."""
    r = rig(srt_reads=False)
    _fill_send_buffer(r.d.link._sock)
    g, rid = r.srt.req("GET", path="/blocked")

    def blocked() -> bool:
        q = r.d._live.get(rid)
        return q is not None and q.lc._transition_lock.locked() and r.d.link._wlock.locked()

    assert _until(blocked), "the verdict send did not block"
    out: dict = {}
    c = threading.Thread(target=lambda: out.setdefault("ok", r.d.close(timeout_s=2)), daemon=True)
    try:
        c.start()
        c.join(4)
        assert not c.is_alive(), "close() waited on the blocked request"
        assert out["ok"] is True and r.d.link.dead == "the launch closed"
        rows = r.rows(rid)
        assert [x.kind for x in rows] == ["egress_attempt", "egress_failed"]
        assert rows[1].failed_effect == Effect.SEND_ALLOW.value
    finally:
        r.srt.sock.close()                                  # unblocks the old code's send so the rig can close


def test_r2_fix2_past_the_audit_id_cap_a_request_is_denied_and_no_id_is_reused(rig) -> None:
    r = rig(limits=dm.Limits(card_poll_s=0.05, stop_drain_s=0.5, max_audit_ids=3))
    rids = []
    for i in range(3):
        g, rid = r.srt.req("GET", path=f"/n{i}")
        assert action(r.srt.wait(g)) == "allow"
        rids.append(rid)
    g, rid4 = r.srt.req("GET", path="/n3")
    assert [(x["action"], x["status"], x["reason"]) for x in r.srt.wait(g)] == [("deny", 503, "wall: audit_cap")]
    assert r.rows(rid4) == [] and r.d.counters["audit_cap"] == 1
    g, _ = r.srt.req("GET", path="/again", rid=rids[0])      # an id below the cap: still a duplicate
    assert [x["reason"] for x in r.srt.wait(g)] == ["decider: a second request with one audit value"]
    g, rid5 = r.srt.req("GET", path="/n4")
    assert [x["reason"] for x in r.srt.wait(g)] == ["wall: audit_cap"]
    assert r.d.counters["audit_cap"] == 2 and len(r.d._spent) == 3


def test_r2_d2_an_audit_id_whose_route_lookup_raised_is_spent(rig) -> None:
    class Boom(dict):
        def get(self, host, default=None):
            if host == "boom.test":
                raise RuntimeError("the route table")
            return super().get(host, default)

    class Routes:
        routes = Boom()

    r = rig(routes=Routes())
    rid = "2c1f9a0e-7b6d-4e5f-8a9b-0c1d2e3f4a5b"
    g, _ = r.srt.req("GET", host="boom.test", path="/a", rid=rid)
    assert [(x["action"], x["status"]) for x in r.srt.wait(g)] == [("deny", 500)]
    g2, _ = r.srt.req("GET", path="/b", rid=rid)
    assert [x["reason"] for x in r.srt.wait(g2)] == ["decider: a second request with one audit value"]


@pytest.mark.parametrize("where", ["first", "form"])
def test_r2_d3_a_verdict_kind_outside_silent_card_wall_is_denied(rig, monkeypatch, where) -> None:
    """Seam: classify's kinds are silent / card / wall today; any other kind fails closed, at either classification."""
    import dataclasses
    real = classify.decide

    def seam(parsed, *a, **kw):
        v = real(parsed, *a, **kw)
        hit = where == "first" or kw.get("form_params") is not None
        return dataclasses.replace(v, kind="allow") if hit else v

    monkeypatch.setattr(classify, "decide", seam)
    r = rig()
    if where == "first":
        p, rid = r.srt.req("POST", path="/k")
    else:
        p, rid = r.srt.req("POST", path="/k", headers={"content-type": ["application/x-www-form-urlencoded"],
                                                       "content-length": ["3"]})
        assert r.srt.wait(p)[0]["action"] == "need_body"
        r.srt.body(p, b"a=1")
    v = r.srt.wait(p, 2 if where == "form" else 1)
    assert (v[-1]["action"], v[-1]["status"], v[-1]["reason"]) == ("deny", 403, "wall: verdict_kind")
    assert r.desk.filed == [] and r.rows(rid)[0].deciding_step == "wall:verdict_kind"


# --- S9 r2 item A: a lifted read stores the NAMES the exemption lifted (never a value) ---------------------------

_AZ = "sig=AZV1&sv=2022-11-02&se=2030-01-01&sp=r"


@pytest.mark.parametrize("host,query,expect", [
    ("acct.blob.core.windows.net", _AZ, ["sig"]),
    ("acct.blob.core.windows.net", _AZ + "&X-Amz-Signature=SV9", ["sig", "x-amz-signature"]),
    ("ok.test", "X-Amz-Signature=SV9&X-Amz-Credential=SV8", ["x-amz-signature", "x-amz-credential"]),
])
def test_a_lifted_read_stores_the_lifted_names_and_no_value(rig, host, query, expect) -> None:
    r = rig()
    g, rid = r.srt.req("GET", host=host, path="/c/b", query=query)
    assert action(r.srt.wait(g)) == "allow"
    row = [x for x in r.rows(rid) if x.kind == "egress_attempt"][0]
    stored = json.loads(row.names)
    assert [n for n in stored if n in ("sig", "x-amz-signature", "x-amz-credential")] == expect
    assert "SV9" not in row.names and "AZV1" not in row.names and "SV8" not in row.names


# --- S9 r2 item B: the over-cap reason labels the over-cap card only ----------------------------------------------

@pytest.mark.parametrize("query,extra,why", [
    (None, {}, "upload_pack_over_cap"),
    ("token=QV1", {}, "credential_in_query"),
    ("_method=DELETE", {}, "_method"),
    (None, {"x-http-method-override": ["DELETE"]}, "override:x-http-method-override"),
])
def test_a_cut_pack_body_labels_its_row_by_the_card_it_filed(rig, query, extra, why) -> None:
    r = rig()
    _advertise(r)
    p, rid = r.srt.req("POST", path="/r.git/git-upload-pack", query=query,
                       headers=UP_CT | {"transfer-encoding": ["chunked"]} | extra)
    assert r.srt.wait(p)[0]["action"] == "need_body"
    r.srt.body(p, b"x" * (advert.UPLOAD_PACK_BODY_CAP + 1))
    assert _until(lambda: r.desk.filed)
    r.desk.answer["card-1"] = "denied"
    assert _until(lambda: [x for x in r.rows(rid) if x.kind == "egress_attempt"])
    assert [x.deciding_step for x in r.rows(rid) if x.kind == "egress_attempt"] == [f"card:{why}:denied"]
