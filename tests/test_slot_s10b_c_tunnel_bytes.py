"""S10b slice C: root publishes ``tunnel_bytes`` at END (CONTRACT §5.1 ``teardown`` Out column).

The per-session tunnel-log reader is ``runtime_helper.read_session_total`` (``dispatch_usage`` re-imports it); root's
``teardown`` reads the session's OWN last ``passthrough_total`` through the trusted walk and adds it to a RETAINED
answer; ``HelperSlotRuntime`` keeps it, the launcher stamps it on the ended record, and a usage settle reads it."""
from __future__ import annotations

import json
import os

import pytest

from levain.agents.session_launcher import teardown_proves
from levain.slot import dispatch_usage as DU
from levain.slot import runtime_helper as H
from levain.slot import records as R
from tests.test_slot_dispatch_runtime import rt_rig  # noqa: F401
from tests.test_slot_s2f3_start import GW0, _rendered, env  # noqa: F401 - the fake-process-table world


def _open(session, tid):
    return json.dumps({"event": "tunnel-open", "session": session, "tunnel_id": tid}) + "\n"


def _close(session, tid, total, **extra):
    return json.dumps({"event": "tunnel", "session": session, "passthrough": True, "tunnel_id": tid,
                       "passthrough_total": total, **extra}) + "\n"


def _rec(session, total, tid=1):
    """One passthrough tunnel, opened and closed (the front's records since FIX ROUND 2)."""
    return _open(session, tid) + _close(session, tid, total)


def _teardown_world(env):
    w, _la, table = env
    s = _rendered(w)
    for pid in w.fronts.alive():
        table.add(pid, GW0)
    return w, s, w.L / "gw" / "slot0" / "front.log"


def test_the_reader_is_one_and_dispatch_usage_reimports_it(tmp_path):
    p = tmp_path / "front.log"
    p.write_text(_rec("A", 5) + _rec("B", 9) + _rec("A", 7, 2) + _rec("B", 11, 2))
    assert H.read_session_total(p, "A") == 7 and H.read_session_total(p, "B") == 11
    assert DU._default_read_total(str(p), "A") == 7
    assert H.read_session_total(p, "C") is None
    assert H.read_session_total(tmp_path / "missing", "A") is None


def test_teardown_answers_this_sessions_total_not_anothers(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.write_text(_rec("other", 999) + _rec(s, 100) + _rec("other", 5000, 2) + _rec(s, 1234, 2)
                   + _rec("other", 7000, 3) + "torn{")
    code, line = w.main(["teardown", s])
    assert line == {"ok": True, "state": R.RETAINED, "tunnel_bytes": 1234}, w.last_err
    assert teardown_proves(line)                       # the extra key leaves the proof unchanged


def test_the_field_is_absent_when_the_log_has_no_record_of_the_session(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.write_text(_rec("other", 999))
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED}


def test_the_field_is_absent_when_the_log_is_unreadable_or_empty(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.unlink()
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED}


def test_a_symlinked_log_is_refused_through_the_trusted_walk(env, tmp_path):
    w, s, log = _teardown_world(env)
    real = tmp_path / "elsewhere.log"
    real.write_text(_rec(s, 4242))
    log.unlink()
    os.symlink(real, log)
    ans = w.main(["teardown", s])[1]
    assert "tunnel_bytes" not in ans and ans["ok"] is True and ans["state"] == R.RETAINED


def test_a_replaced_log_dir_is_refused(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.write_text(_rec(s, 77))
    os.chmod(w.L / "gw", 0o777)                         # L/gw no longer root's alone
    ans = w.main(["teardown", s])[1]
    assert "tunnel_bytes" not in ans


def test_a_hard_linked_log_is_refused(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.write_text(_rec(s, 77))
    os.link(log, tmp_path / "alias.log")
    assert "tunnel_bytes" not in w.main(["teardown", s])[1]


def _on_front_kill(w, table, fn):
    """Run ``fn`` when SCRUB SIGKILLs a process of the front's uid (GW0), before it dies: the last moment the front
    could still log."""
    from dataclasses import replace as _replace
    import signal
    orig = table.send

    def send(pid, sig):
        p = table.procs.get(pid)
        if sig == signal.SIGKILL and p is not None and GW0 in p.uids:
            fn()
        orig(pid, sig)
    w.seams.proc_seams = _replace(w.seams.proc_seams, send=send)


def test_fr1_the_total_is_read_only_after_scrub_proves_the_front_dead(env):
    """FIX ROUND 1 (codex HIGH): END proves the SLOT uid gone, not the front's. A second tunnel the front logs as
    it dies (after END, before SCRUB's kill completes) must be in the published total, not the earlier smaller one."""
    w, s, log = _teardown_world(env)
    log.write_text(_rec(s, 100) + _open(s, 2))

    def second_tunnel_logged():
        with open(log, "a") as f:
            f.write(_close(s, 2, 1000))
    _on_front_kill(w, env[2], second_tunnel_logged)
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED, "tunnel_bytes": 1000}, w.last_err


def test_fr1_a_front_that_survives_scrub_is_never_read_and_nothing_is_published(env, monkeypatch):
    """The front still live (its uid survives SCRUB's kill): the counter is never read, the END answers no total."""
    w, s, log = _teardown_world(env)
    _w, _la, table = env
    log.write_text(_rec(s, 100))
    for pid in table.of(GW0):
        table.immortal.add(pid)
    calls = []
    real = H.read_session_tunnel_total
    monkeypatch.setattr(H, "read_session_tunnel_total", lambda *a, **k: calls.append(a) or real(*a, **k))
    ans = w.main(["teardown", s])[1]
    assert calls == [] and "tunnel_bytes" not in ans and ans["ok"] is False, ans


def test_fr1_the_reader_lets_a_programming_error_propagate(env, monkeypatch):
    """Only the walk's refusals and OSError read as an absent total; anything else is not swallowed as None."""
    w, s, log = _teardown_world(env)
    store = H.Store(w.seams)
    monkeypatch.setattr(H, "_open_front_log_dir", lambda *a, **k: (_ for _ in ()).throw(AttributeError("bug")))
    try:
        with pytest.raises(AttributeError):
            H.read_session_tunnel_total(store, H.SlotId("egress", 0), GW0, s)
    finally:
        store.close()


def test_the_trusted_reader_never_raises_on_a_missing_slot_dir(env):
    w, s, log = _teardown_world(env)
    store = H.Store(w.seams)
    try:
        assert H.read_session_tunnel_total(store, H.SlotId("egress", 5), 898, s) is None
    finally:
        store.close()


# --- dispatch runtime and launcher ---------------------------------------------------------------------------------------

def test_the_runtime_keeps_the_published_total_from_a_proven_end_only(rt_rig):
    from tests.test_slot_dispatch_runtime import SESSION, line
    h, _lease, make = rt_rig
    h.script_("teardown", line=line(ok=True, state="RETAINED", tunnel_bytes=4096))
    rt = make()
    assert rt.final_tunnel_bytes(SESSION) is None
    assert rt.end(SESSION) is True and rt.final_tunnel_bytes(SESSION) == 4096


@pytest.mark.parametrize("bad", [True, -1, "7"])   # (a float, or an int over 2**63, the answer grammar refuses itself)
def test_a_malformed_total_is_dropped_and_the_end_still_proves(rt_rig, bad):
    from tests.test_slot_dispatch_runtime import SESSION, line
    h, _lease, make = rt_rig
    h.script_("teardown", line=line(ok=True, state="RETAINED", tunnel_bytes=bad))
    rt = make()
    assert rt.end(SESSION) is True and rt.final_tunnel_bytes(SESSION) is None


def test_an_unproven_end_keeps_no_total(rt_rig):
    from tests.test_slot_dispatch_runtime import SESSION, line
    h, _lease, make = rt_rig
    h.script_("teardown", line=line(ok=True, state="HELD", tunnel_bytes=5))
    rt = make()
    assert rt.end(SESSION) is False and rt.final_tunnel_bytes(SESSION) is None


def test_fr1_an_unproven_answer_never_leaks_its_total_into_a_later_proven_end(rt_rig):
    """FIX ROUND 1 (codex LOW): a RETAINED answer that fails teardown_proves (``released`` not a bool) carries a
    total; the retry proves with no total. The unproven 7 must not be published."""
    from tests.test_slot_dispatch_runtime import SESSION, line
    h, _lease, make = rt_rig
    h.script_("teardown", line=line(ok=True, state="RETAINED", released="no", tunnel_bytes=7))
    rt = make()
    assert rt.end(SESSION) is False and rt.final_tunnel_bytes(SESSION) is None
    h.script_("teardown", line=line(ok=True, state="RETAINED"))
    assert rt.end(SESSION) is True and rt.final_tunnel_bytes(SESSION) is None


def test_fr1_the_total_lives_and_is_evicted_with_its_proof(rt_rig, monkeypatch):
    """FIX ROUND 1 (codex MED): the measurement is kept WITH the END proof (one entry, one eviction), so a proof and
    its value never outlive each other; an evicted value reads None, the over-debit case."""
    from levain.slot import dispatch_runtime as DR
    from tests.test_slot_dispatch_runtime import SESSION, line
    monkeypatch.setattr(DR, "_ENDED_KEEP", 1)
    h, _lease, make = rt_rig
    h.script_("teardown", line=line(ok=True, state="RETAINED", tunnel_bytes=4096))
    rt = make()
    assert rt.end(SESSION) is True and rt.final_tunnel_bytes(SESSION) == 4096
    h.script_("teardown", line=line(ok=True, state="RETAINED"))          # a later END that publishes no total
    assert rt.end("s-00000000000000b2") is True
    assert rt.final_tunnel_bytes(SESSION) is None                       # evicted together with A's proof
    assert SESSION not in rt._ended


def test_fr1_concurrent_stops_never_erase_a_published_total(tmp_path):
    """FIX ROUND 1 (codex MED): launcher B reads ``started``, then launcher A writes ``ended`` with its total, then B
    writes. B must not erase A's total: the read-then-write is one step under the key's lock, and a later writer
    with a total enriches an ``ended`` record that lacks one."""
    import threading
    from levain.agents.session_launcher import RealLauncher, SessionIndex
    key = "s-0000000000000001"
    idx = SessionIndex(tmp_path / "idx")
    idx.put(key, "started", session="S1", run_id="r", started_s=1.0)

    def launcher(rt):
        lau = RealLauncher.__new__(RealLauncher)
        lau.index, lau.runtime, lau.clock = idx, rt, lambda: 5.0
        return lau

    class RtA:
        def final_tunnel_bytes(self, session):
            return 1000

    a = launcher(RtA())
    done = []

    class RtB:
        def final_tunnel_bytes(self, session):            # B is between its read and its write: A runs now
            t = threading.Thread(target=lambda: done.append(a._mark_ended(key, session="S1")))
            t.start()
            t.join(0.5)                                    # with the lock A waits; without it A writes now
            self.t = t
            return None

    rtb = RtB()
    assert launcher(rtb)._mark_ended(key, session="S1") is True
    rtb.t.join(5)
    assert done == [True]
    rec = idx.get(key)
    assert rec["state"] == "ended" and rec["tunnel_bytes"] == 1000, rec

    class RtC:                                             # a later writer never replaces a valid total
        def final_tunnel_bytes(self, session):
            return 5
    assert launcher(RtC())._mark_ended(key, session="S1") is True
    assert idx.get(key)["tunnel_bytes"] == 1000


def test_teardown_proves_is_unchanged_by_the_extra_key():
    assert teardown_proves({"state": "RETAINED", "tunnel_bytes": 9}) is True
    assert teardown_proves({"state": "RETAINED", "released": False, "tunnel_bytes": 9}) is True
    assert teardown_proves({"state": "HELD", "tunnel_bytes": 9}) is False


def test_the_launcher_stamps_the_total_on_the_ended_record(tmp_path):
    from levain.agents.session_launcher import RealLauncher, SessionIndex
    idx = SessionIndex(tmp_path / "idx")
    idx.put("s-0000000000000001", "started", session="S1", run_id="r", started_s=1.0)
    idx.put("s-0000000000000002", "started", session="S2", run_id="r", started_s=1.0)

    class Rt:
        def final_tunnel_bytes(self, session):
            return {"S1": 321, "S2": True}.get(session)

    lau = RealLauncher.__new__(RealLauncher)
    lau.index, lau.runtime, lau.clock = idx, Rt(), lambda: 5.0
    assert lau._mark_ended("s-0000000000000001", session="S1") is True
    assert lau._mark_ended("s-0000000000000002", session="S2") is True
    assert idx.get("s-0000000000000001")["tunnel_bytes"] == 321 and idx.get("s-0000000000000001")["ended_s"] == 5.0
    assert "tunnel_bytes" not in idx.get("s-0000000000000002")          # a non-count is left off
    lau.runtime = object()                                              # a runtime with no such method
    idx.put("s-0000000000000003", "started", session="S3", run_id="r", started_s=1.0)
    assert lau._mark_ended("s-0000000000000003", session="S3") is True
    assert "tunnel_bytes" not in idx.get("s-0000000000000003")


def test_a_usage_settle_reads_the_published_bytes_not_the_reservation(tmp_path):
    from levain.agents.session_launcher import SessionIndex
    from levain.slot.services import dispatch_run as D
    idx = SessionIndex(tmp_path / "idx")
    idx.put("s-0000000000000001", "ended", session="S1", run_id="r", started_s=1.0, ended_s=3.0, tunnel_bytes=555)
    src = D.bound_usage_sources(queue_root=tmp_path / "q", store=tmp_path / "L", index_dir=tmp_path / "idx")
    assert src.tunnel_bytes("S1") == 555


# --- FIX ROUND 2: a total is published only when the log proves every tunnel of the session closed ---------------

def test_fr2_a_tunnel_open_at_read_time_leaves_the_field_absent(env):
    """The under-debit: SCRUB SIGKILLs the front while a tunnel waits out HALF_CLOSE_S; it has an open, no close."""
    w, s, log = _teardown_world(env)
    log.write_text(_rec(s, 100) + _open(s, 2))
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED}, w.last_err


def test_fr2_all_tunnels_closed_publishes_the_total(env, tmp_path):
    """Overlapping tunnels closed out of order: the last close holds the total (passes before and after)."""
    w, s, log = _teardown_world(env)
    log.write_text(_open(s, 1) + _open(s, 2) + _close(s, 2, 500) + _close(s, 1, 900))
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED, "tunnel_bytes": 900}, w.last_err


@pytest.mark.parametrize("text", [
    "{close}",                                   # a close with no earlier open
    "{close}{open}",                             # the open after its close
])
def test_fr2_a_close_without_a_matching_open_is_absent(tmp_path, text):
    p = tmp_path / "front.log"
    p.write_text(_rec("S", 10) + text.replace("{close}", _close("S", 2, 50)).replace("{open}", _open("S", 2)))
    assert H.read_session_total(p, "S") is None


def test_fr2_another_sessions_records_are_ignored(tmp_path):
    p = tmp_path / "front.log"
    p.write_text(_open("other", 1) + _rec("S", 40) + _close("other", 7, 9999) + _open("other", 1) + "garbage\n")
    assert H.read_session_total(p, "S") == 40
    assert H.read_session_total(p, "other") is None


@pytest.mark.parametrize("text", [
    _rec("S", 10) + _rec("S", 20),                                  # id 1 twice (a second front, or forged)
    _open("S", 1) + _open("S", 1) + _close("S", 1, 10),             # one id opened twice
    _rec("S", 10) + _close("S", 1, 20),                             # one id closed twice
    _open("S", True) + _close("S", True, 10),                       # a bool id
    _open("S", "1") + _close("S", "1", 10),                         # a string id
    _open("S", 0) + _close("S", 0, 10),                             # id 0
    _open("S", -1) + _close("S", -1, 10),                           # a negative id
    _open("S", 2**63) + _close("S", 2**63, 10),                     # past int64
    _rec("S", 10, 2),                                               # no id 1 (out of the window, rotated or torn)
    _rec("S", 10) + _rec("S", 30, 3),                               # a gap
    _rec("S", 50) + _rec("S", 20, 2),                               # a total that goes down
    _rec("S", 10) + _open("S", 2) + _close("S", 2, -5),             # a negative total
    _rec("S", 10) + _open("S", 2) + _close("S", 2, True),           # a bool total
    _rec("S", 10) + _open("S", 2) + _close("S", 2, 2**63),          # past int64
    _rec("S", 10) + _open("S", 2) + _close("S", 2, 30, passthrough="yes"),
    _rec("S", 10) + json.dumps({"event": "tunnel", "session": "S", "passthrough": False,
                                "passthrough_total": 99}) + "\n",   # a non-passthrough record with a total
    _rec("S", 10) + json.dumps({"event": "tunnel", "session": "S", "passthrough": False,
                                "tunnel_id": 2}) + "\n",            # a non-passthrough record with an id
])
def test_fr2_a_hostile_duplicate_or_malformed_record_is_absent(tmp_path, text):
    p = tmp_path / "front.log"
    p.write_text(text)
    assert H.read_session_total(p, "S") is None


def test_fr2_a_non_passthrough_tunnel_counts_nothing_and_blocks_nothing(tmp_path):
    p = tmp_path / "front.log"
    plain = json.dumps({"event": "tunnel", "session": "S", "passthrough": False, "bytes_up": 3}) + "\n"
    p.write_text(plain + _rec("S", 10) + plain)
    assert H.read_session_total(p, "S") == 10


def test_fr2_a_log_from_a_front_without_open_records_is_absent(tmp_path):
    """The old-log rule: closes with no id and no open (a front older than the open record) prove nothing."""
    p = tmp_path / "front.log"
    old = json.dumps({"event": "tunnel", "session": "S", "passthrough": True, "passthrough_total": 70}) + "\n"
    p.write_text(old)
    assert H.read_session_total(p, "S") is None
    p.write_text(old + _rec("S", 90))                    # an old line then new ones: still unproven
    assert H.read_session_total(p, "S") is None


def test_fr2_an_open_cut_out_of_the_tail_window_is_absent(tmp_path):
    """Open 1 lies before the 1 MiB tail: its close may be missing too, so the window proves nothing."""
    p = tmp_path / "front.log"
    pad = json.dumps({"event": "refused", "session": "other", "detail": "x" * 1000}) + "\n"
    p.write_text(_open("S", 1) + pad * 1100 + _close("S", 1, 10) + _rec("S", 20, 2))
    assert H.read_session_total(p, "S") is None


def test_fr2_the_front_logs_an_open_before_the_bytes_and_the_close_carries_its_id():
    """The real front: while tunnel 2 is open the reader answers None; once it closes, the front's own total."""
    import asyncio
    from tests.test_broker_front_passthrough_limits import _records, _run, _settled, client_hello, policy

    async def body(open_tunnel, front, log) -> None:
        hello_len = len(client_hello("api.anthropic.com"))
        st1, _, r1, w1 = await open_tunnel()
        got = b""
        while len(got) < hello_len:
            got += await r1.read(65536)
        w1.close()
        await _settled(front)
        st2, _, r2, w2 = await open_tunnel()
        got = b""
        while len(got) < hello_len:
            got += await r2.read(65536)
        assert st1 == st2 == 200
        recs = [x for x in _records(log) if x.get("event") in ("tunnel-open", "tunnel")]
        assert [(x["event"], x.get("tunnel_id")) for x in recs] == [
            ("tunnel-open", 1), ("tunnel", 1), ("tunnel-open", 2)]
        assert H.read_session_total(log, "s-test") is None      # tunnel 2 has no close yet
        w2.close()
        await _settled(front)
        assert H.read_session_total(log, "s-test") == front.pt_total == 4 * hello_len

    asyncio.run(_run(policy("interactive"), body))
