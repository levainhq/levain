"""CH-D2 S5b: the decider service (`levain.chat_egress.decider`) through srt's framing, by hand (`decider_rig.SrtEnd`).

Rows (the S5b row of `1024-18/CHD2_BUILD_PLAN_DRAFT.md`): C1, R9-A4, R15-10-A4, R15-10-A7, R15-10-A13, R15-12-B8,
R15-12-B15, R15-19-1, R15-19-2; and the five items `1024-20/HANDOFF.md` carries for S5b. The same rows run against
the REAL glue and srt in `test_decider_glue.py`.
"""

from __future__ import annotations

import threading
import time

import pytest

from levain.chat_egress import advert, classify, decider as dm, strip, wire
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
        r.close()


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
    assert time.monotonic() - t0 < 1.0
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
    lim = dm.Limits(card_poll_s=0.05, body_timeout_s=0.6, max_body_readers=2, stop_drain_s=0.5)
    r = rig(limits=lim)
    stalled = [r.srt.req("POST", path=f"/s{i}", headers={"transfer-encoding": ["chunked"]})[0] for i in range(3)]
    time.sleep(0.2)
    g, _ = r.srt.req("GET", path="/ok")
    t0 = time.monotonic()
    assert action(r.srt.wait(g)) == "allow" and time.monotonic() - t0 < 0.5
    finals = [r.srt.wait(s, 2, timeout=5)[-1] for s in stalled]
    reasons = sorted(f["reason"] for f in finals if f["action"] == "deny")
    assert reasons == ["wall: body_readers", "wall: body_timeout", "wall: body_timeout"]
    assert sorted(f["status"] for f in finals) == [408, 408, 503]
    t1 = time.monotonic()
    assert r.close() and time.monotonic() - t1 < 3.0


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
    r = rig(lookup=Lookup(hang={"slow.test"}), resolver_timeout=0.5)
    s, srid = r.srt.req("GET", host="slow.test")
    time.sleep(0.05)
    g, _ = r.srt.req("GET", host="fast.test")
    t0 = time.monotonic()
    assert action(r.srt.wait(g)) == "allow" and time.monotonic() - t0 < 0.4
    v = r.srt.wait(s, timeout=3)
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
        assert r.d.link.dead == "a verdict frame was cut mid-send"
    else:
        assert [x["action"] for x in v] == ["allow"]


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
