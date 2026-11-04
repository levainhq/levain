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


def _rec(session, total, event="tunnel"):
    return json.dumps({"event": event, "session": session, "passthrough_total": total}) + "\n"


def _teardown_world(env):
    w, _la, table = env
    s = _rendered(w)
    for pid in w.fronts.alive():
        table.add(pid, GW0)
    return w, s, w.L / "gw" / "slot0" / "front.log"


def test_the_reader_is_one_and_dispatch_usage_reimports_it(tmp_path):
    p = tmp_path / "front.log"
    p.write_text(_rec("A", 5) + _rec("B", 9) + _rec("A", 7) + _rec("B", 11))
    assert H.read_session_total(p, "A") == 7 and H.read_session_total(p, "B") == 11
    assert DU._default_read_total(str(p), "A") == 7
    assert H.read_session_total(p, "C") is None
    assert H.read_session_total(tmp_path / "missing", "A") is None


def test_teardown_answers_this_sessions_total_not_anothers(env, tmp_path):
    w, s, log = _teardown_world(env)
    log.write_text(_rec("other", 999) + _rec(s, 100) + _rec("other", 5000) + _rec(s, 1234)
                   + _rec("other", 7000) + "torn{")
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
