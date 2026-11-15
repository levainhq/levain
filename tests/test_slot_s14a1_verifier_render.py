"""S14 A1: render's §5.6 half. A verifier session's render holds the staged bundle to the ticket's
``input_bundle_digest`` and stages ``driver_input.json`` into ``<S>/driver/input.json`` against the ticket's
``driver_input_digest``; either mismatch first sends a field-only CAS of ``verifier_report_refused`` and then
refuses. The probe this inverts (``p1``): render accepted a mismatched bundle and never staged the driver input.

The real chown and the 0440 / 0700 modes on a real uid are VM residue; the rig records the chown the helper asks for."""
from __future__ import annotations

import hashlib
import os

import pytest

from levain.contracts import canon
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests import test_slot_s2f2_runtime_helper as T2
from tests.test_slot_s2f2_runtime_helper import world  # noqa: F401 - fixture

S = "s-00000000000000c1"
BLOB = dict(routes=[], verifier_steps=[{"argv": ["pytest", "-q"], "wall_s": 60}], driver="output_compare")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rig(world, request, *, ticket_bundle=T2.BUNDLE, ticket_driver=T2.DRIVER_INPUT, staged_driver=T2.DRIVER_INPUT):
    """A bound verifier ticket pinning the digests of ``ticket_*``, a prepared session, and ``staged_driver`` in staging
    (the staged bundle is always ``T2.BUNDLE``)."""
    slot = SlotId("none", 0)
    (world.L / "policy" / "dispatch_ceiling").write_bytes(canon.canonical_bytes(
        {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 5_000_000, "max_open_tickets": 4,
         "max_runs_per_day": 50}))
    os.chmod(world.L / "policy" / "dispatch_ceiling", 0o644)
    tickets = T.TicketStore(world.L / "sessions" / T.JOURNAL, world.L / "policy" / "dispatch_ceiling",
                            ceiling_uid=os.getuid())
    tickets.start()
    request.addfinalizer(tickets.close)
    world.control.tickets = tickets
    entry = dict(T2.VERIFIER_ENTRY, driver_input_digest=_sha(ticket_driver))
    facts = T.check_facts({"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
                           "kind": "verifier", "egress_routes": [], "reserved_microdollars": 0,
                           "verifier_generation": 1, "verifier_id": "pytest-report",
                           "verifier_steps": [{"argv": ["pytest", "-q"], "wall_s": 60}],
                           "input_bundle_digest": _sha(ticket_bundle), "driver_input_digest": _sha(ticket_driver)})
    ticket = tickets.mint("a-verifier-1", facts, C.new_handle, resolved=entry).ticket
    tickets.bind(ticket, S)
    a = R.Alloc(session=S, slot=slot, lease_seq=1, slot_rev=1, slot_uid=893, slot_gid=893, kind="verifier",
                vendor="claude", auth="none", operator_uid=T2.OP_UID, supervisor_uid=T2.DISPATCH_UID,
                staging_uid=T2.OP_UID, created_at="2026-10-20T00:00:00Z", boot_id=T2.BOOT, child_uid=892,
                ticket=ticket, netns=slot.netns, cgroup=R.cgroup_path(slot, S),
                ticket_facts=dict(facts, verifier=entry))
    (world.L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(a))
    claims = {"operator_uid": T2.OP_UID, "supervisor_uid": T2.DISPATCH_UID, "slot": R.slot_obj(slot),
              "class": "none", "kind": "verifier", "vendor": "claude", "auth": "none", "lease_seq": 1,
              "slot_rev": 1, "ticket": ticket}
    world.control.store.put(R.SessionState(session=S, generation=0, state=R.ALLOCATED, claims=claims,
                                           lease_live=True, event_kind="state", updated_at="2026-10-20T00:00:00Z"))
    world.control._session_lease[S] = C.Lease(C.new_handle(), T2.DISPATCH_UID, used=True, session=S, ticket=ticket)
    store = H.Store(world.seams)
    request.addfinalizer(store.close)
    ctx = H.Ctx(world.seams, store, T2.DISPATCH_UID, "dispatch", T2.OP_UID, {})
    H.make_session_dirs(ctx, S, H.session_dir_rows(slot, {"_levain_v0": 893, "_levain_c0": 892}))
    (world.L / "staging" / S).mkdir()
    world.stage(S)
    (world.L / "staging" / S / "driver_input.json").write_bytes(staged_driver)
    return S


def _render(world):
    return world.main(["render", S], canon.canonical_bytes(
        {"format": "levain-session-render/1", "vendor": "claude", **BLOB}), uid=T2.DISPATCH_UID)


def test_matching_digests_stage_the_driver_input(world, request):  # noqa: F811
    _rig(world, request)
    code, line = _render(world)
    assert line == {"ok": True}, world.last_err
    inp = world.L / "live" / S / "driver" / "input.json"
    assert inp.read_bytes() == T2.DRIVER_INPUT
    assert world.chown_of(f"live/{S}/driver/input.json") == (0, 893)      # root : _levain_v<n> (the slot gid)
    assert world.mode(f"live/{S}/driver/input.json") == 0o440
    assert world.chown_of(f"live/{S}/in/repo.bundle") == (0, 892)
    assert os.listdir(world.L / "staging" / S) == []
    assert world.state(S).state == R.RENDERED
    assert "verifier_report_refused" not in world.state(S).fields


def test_mismatched_bundle_is_refused_and_named(world, request):  # noqa: F811
    """p1 inverted: the ticket pins another bundle than the one staged."""
    _rig(world, request, ticket_bundle=T2.BUNDLE + b"x")
    code, line = _render(world)
    assert line == {"ok": False, "refused": "input-bundle-mismatch"}
    st = world.state(S)
    assert st.fields["verifier_report_refused"] == "input-bundle-mismatch"
    assert st.state == R.REVOKED
    assert not (world.L / "live" / S / "driver" / "input.json").exists()
    assert not (world.L / "live" / S / "in" / "repo.bundle").exists()
    ops = [(op, f.get("fields")) for op, f in world.calls]
    assert ops[1] == ("CAS", {"verifier_report_refused": "input-bundle-mismatch"})
    assert ops[-1][0] == "REVOKE"
    assert not any(op == "MINT" for op, _ in ops)


@pytest.mark.parametrize("how", ["bytes-differ", "not-canonical"])
def test_driver_input_that_is_not_the_tickets_is_refused_and_named(world, request, how):  # noqa: F811
    if how == "bytes-differ":
        _rig(world, request, staged_driver=b'{"a":1}')
    else:                                     # digest matches the bytes, the bytes are not canonical JSON
        _rig(world, request, ticket_driver=b'{ "a": 1 }', staged_driver=b'{ "a": 1 }')
    code, line = _render(world)
    assert line == {"ok": False, "refused": "driver-input-mismatch"}
    st = world.state(S)
    assert st.fields["verifier_report_refused"] == "driver-input-mismatch"
    assert st.state == R.REVOKED
    assert not (world.L / "live" / S / "driver" / "input.json").exists()   # a refused copy is unlinked


def test_driver_dir_not_the_drivers_is_refused_closed(world, request, monkeypatch):  # noqa: F811
    """The driver dir must be the owner alloc names: a dir owned by another uid is never written."""
    _rig(world, request)
    monkeypatch.setattr(world.seams, "chowned_uid", lambda uid: os.getuid() + 1)
    code, line = _render(world)
    assert line["ok"] is False and line["refused"] == "bad-root"
    assert not (world.L / "live" / S / "driver" / "input.json").exists()
    assert world.state(S).state == R.REVOKED


def test_alloc_without_the_digests_is_refused_closed():
    from types import SimpleNamespace
    for facts in (None, {"kind": "verifier"}, {"input_bundle_digest": "sha256:" + "1" * 64}):
        with pytest.raises(H.Refused) as e:
            H.verifier_digests(SimpleNamespace(session=S, ticket_facts=facts))
        assert e.value.name == "bad-root"
