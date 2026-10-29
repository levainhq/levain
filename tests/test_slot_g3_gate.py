"""G3-2: the identity gate in ``start`` for an interactive passthrough session (DESIGN g3 §1-§2, tests 6-14).

Built on the 2f3 harness (``World`` + the fake launches + the fake process table). The keep/ -> tree move itself is
``end.return_move``'s (tests/test_slot_g3_return.py); these tests hold what ``start`` does around it: the baseline read,
the identity call, the record, the compare, the refusals and the park-back. What only a VM run shows (R1-R6 of the
design) is not here.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import records as R
from levain.slot import start as SS
from levain.slot.ids import SlotId
from tests.test_slot_s2f3_start import _S, _blob, _rendered, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E0 = SlotId("egress", 0)
UUID = "4b8e0c1a-1d2e-4f00-9a7b-0123456789ab"       # give_credential's claude baseline
OTHER = "9d2f7a55-0000-4000-8000-00000000beef"
PASS = ["claude-sub", "github"]
CRED = {"claude": ".credentials.json", "codex": "auth.json"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(SS, "slot_owner_ok", lambda st: True)
    return _world(tmp_path)


def _keep(w, vendor="claude") -> Path:
    return E0.slot_dir(w.L) / "keep" / CRED[vendor]


def _tree(w, s, vendor="claude") -> Path:
    return _S(w, s) / "config" / vendor / CRED[vendor]


def _baseline_path(w) -> Path:
    return E0.slot_dir(w.L) / "baseline"


def _gated(w, la, vendor="claude", *, baseline_id=None, profile=UUID) -> str:
    """A rendered interactive passthrough session whose slot holds a credential and a baseline."""
    w.give_credential(E0, vendor, observed_id=baseline_id)
    la.profile_uuid = profile
    return _rendered(w, "egress", "interactive", vendor, "passthrough", blob=_blob(vendor=vendor, routes=PASS))


def _targets(la) -> list[str]:
    return [r["target"] for r in la.spawns]


def _back_in_keep(w, s, vendor="claude") -> None:
    assert _keep(w, vendor).read_bytes() == b"{}" and not _tree(w, s, vendor).exists()
    assert not (_S(w, s) / "repo").exists()
    assert "clone" not in _targets(w.la) and "vendor" not in _targets(w.la)


@pytest.fixture(autouse=True)
def _la(env):
    env[0].la = env[1]


# --- test 6: the baseline ----------------------------------------------------------------------------------------------

def test_absent_baseline_refuses_no_baseline_before_any_move(env):
    w, la, _ = env
    s = _gated(w, la)
    _baseline_path(w).unlink()
    assert w.main(["start", s])[1]["refused"] == "no-baseline:e0"
    _back_in_keep(w, s)
    assert la.spawns == []


def _legacy(w, s):
    _baseline_path(w).write_bytes(canon.canonical_bytes(
        {"vendor": "claude", "account_id": "acct-1", "confirmed_at": "2026-10-20T00:00:00Z"}))


def _bad_json(w, s):
    _baseline_path(w).write_bytes(b"{not json")


def _extra_key(w, s):
    obj = canon.parse(_baseline_path(w).read_bytes())
    obj["extra"] = "x"
    _baseline_path(w).write_bytes(canon.canonical_bytes(obj))


def _symlink(w, s):
    real = _baseline_path(w).with_name("elsewhere")
    real.write_bytes(_baseline_path(w).read_bytes())
    _baseline_path(w).unlink()
    os.symlink(real, _baseline_path(w))


def _not_trusted(w, s):
    _baseline_path(w).chmod(0o666)          # the harness's trusted() refuses a group- or world-writable file


def _oversize(w, s):
    obj = canon.parse(_baseline_path(w).read_bytes())
    obj["pad"] = "x" * ID.BASELINE_MAX_BYTES
    _baseline_path(w).write_bytes(canon.canonical_bytes(obj))


def _other_vendor(w, s):
    obj = canon.parse(_baseline_path(w).read_bytes())
    obj["vendor"] = "codex"
    _baseline_path(w).write_bytes(canon.canonical_bytes(obj))


@pytest.mark.parametrize("damage", [_legacy, _bad_json, _extra_key, _symlink, _not_trusted, _oversize, _other_vendor])
def test_a_bad_baseline_refuses_bad_baseline_before_any_move(env, damage):
    w, la, _ = env
    s = _gated(w, la)
    damage(w, s)
    line = w.main(["start", s])[1]
    assert line["refused"] == "bad-baseline", line
    _back_in_keep(w, s)
    assert la.spawns == [] and "identity" not in w.state(s).fields


def _short_reads(monkeypatch, w, first: int, step: int = 7) -> None:
    """``os.read`` of the baseline file answers at most ``first`` bytes, then ``step`` at a time (a legal short
    read); every other read is untouched."""
    real = os.read
    ino = os.stat(_baseline_path(w)).st_ino
    calls = [0]

    def read(fd, n):
        if os.fstat(fd).st_ino != ino:
            return real(fd, n)
        calls[0] += 1
        return real(fd, min(n, first if calls[0] == 1 else step))
    monkeypatch.setattr(ID.os, "read", read)


def test_a_baseline_read_short_is_read_to_its_end(env, monkeypatch):
    """L3 r1 (codex + deepseek): one ``os.read`` may answer short; the read loops to EOF."""
    w, la, _ = env
    s = _gated(w, la)
    _short_reads(monkeypatch, w, 7)
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}


def test_a_baseline_with_junk_after_a_valid_prefix_is_refused_even_read_short(env, monkeypatch):
    w, la, _ = env
    s = _gated(w, la)
    valid = _baseline_path(w).read_bytes()
    _baseline_path(w).write_bytes(valid + b" junk")
    _short_reads(monkeypatch, w, len(valid))
    assert w.main(["start", s])[1]["refused"] == "bad-baseline"
    monkeypatch.undo()
    _back_in_keep(w, s)


# --- test 7: Claude match and differ -----------------------------------------------------------------------------------

def test_claude_match_clones_and_spawns_the_vendor_once(env):
    w, la, _ = env
    s = _gated(w, la)
    cgroups_at_capture: list[int] = []
    capture_cgroups: list = []
    real = w.seams.capture

    def capture(argv, spec_json, cgroup, bound):
        cgroups_at_capture.append(len(w.cgroups))
        capture_cgroups.append(cgroup)
        return real(argv, spec_json, cgroup, bound)
    w.seams.capture = capture
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert len(la.of("vendor")) == 1 and len(la.of("clone")) == 1
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "matched"}
    # the gate's call: the cgroup existed before the child ran, and
    # the child was launched into the session's own cgroup
    assert la.identity_captures == 1 and cgroups_at_capture[0] == 1 and all(cgroups_at_capture)
    assert capture_cgroups == [w.alloc(s).cgroup] and w.alloc(s).cgroup
    assert _tree(w, s).read_bytes() == b"{}" and not _keep(w).exists()
    assert w.steps.index("repo") < w.steps.index("identity-gate") < w.steps.index("clone")


def test_claude_differ_refuses_identity_changed_with_the_credential_back_before_the_answer(env):
    """CONTRACT §7.6's test_identity_change_stops_before_prompt."""
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    before = os.lstat(_keep(w))
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    after = os.lstat(_keep(w))
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    _back_in_keep(w, s)
    assert set(_targets(la)) <= {"forwarder"}          # the identity child is a capture, never a spawn
    assert w.state(s).state == R.RENDERED
    assert w.state(s).fields["identity"] == {"observed_id": OTHER, "result": "identity-changed"}
    assert canon.parse(_baseline_path(w).read_bytes())["observed_id"] == UUID      # no baseline written


# --- test 8: every failure of the call --------------------------------------------------------------------------------

def _ans(status=200, body=None):
    body = {"account": {"uuid": UUID}} if body is None else body
    return json.dumps({"status": status, "body": body if isinstance(body, str) else json.dumps(body)}).encode() + b"\n"


@pytest.mark.parametrize("answer", [
    (1, b"{}"), (0, b"x" * (ID.PROFILE_ANSWER_MAX + 1)), (0, b"not json"), (0, _ans(500)), (0, _ans(body={})),
    (0, _ans(body={"account": {"uuid": "bad uuid"}})), (0, b'{"status":200}\n')])
def test_each_unparsable_answer_refuses_and_parks_back(env, answer):
    w, la, _ = env
    s = _gated(w, la)
    w.seams.capture = lambda argv, spec_json, cgroup, bound: answer
    assert w.main(["start", s])[1]["refused"] == "identity-unparsable"
    _back_in_keep(w, s)
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert set(_targets(la)) <= {"forwarder"}


@pytest.mark.parametrize("status,name", [(401, "identity-expired"), (403, "identity-expired"),
                                         (500, "identity-unparsable")])
def test_an_auth_failure_answer_says_the_login_looks_expired(status, name):
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(0, _ans(status))
    assert e.value.name == name
    assert f"HTTP {status}" in str(e.value) and ("expired or revoked" in str(e.value)) is (name == "identity-expired")


def test_a_capture_that_times_out_refuses_identity_unparsable(env):
    w, la, _ = env
    s = _gated(w, la)

    def capture(argv, spec_json, cgroup, bound):
        raise Refused("child-timeout")
    w.seams.capture = capture
    assert w.main(["start", s])[1]["refused"] == "identity-unparsable"
    _back_in_keep(w, s)


@pytest.mark.parametrize("where", ["read_alloc", "read_record", "kill_domain", "_no_slot_process"])
def test_a_window_check_that_raises_refuses_identity_window(env, monkeypatch, where):
    w, la, _ = env
    s = _gated(w, la)
    target = SS.H if where == "read_alloc" else SS

    def boom(*a, **k):
        raise OSError(errno.EIO, where)

    def arm(step):
        if step == "identity-gate":
            monkeypatch.setattr(target, where, boom)
    w.on_step = arm
    assert w.main(["start", s])[1]["refused"] == "identity-window"
    monkeypatch.undo()
    _back_in_keep(w, s)
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert not hasattr(la, "identity_captures")


def test_an_unproven_kill_refuses_identity_window(env):
    w, la, table = env

    def plant(step):
        if step == "identity-gate":      # a slot-uid process in the cgroup that cgroup.kill does not end
            table.add(99001, w.alloc(w.session).slot_uid, immortal=True, cg=True)
    s = _gated(w, la)
    w.on_step = plant
    assert w.main(["start", s])[1]["refused"] == "identity-window"
    _back_in_keep(w, s)


def test_a_vendor_outside_the_calls_refuses_identity_call_unbuilt():
    with pytest.raises(Refused) as e:
        ID.identity_call(None, "gemini", held=True)
    assert e.value.name == "identity-call-unbuilt"


# --- test 9: Codex is not a check -------------------------------------------------------------------------------------

def test_codex_spawns_no_identity_child_and_records_not_a_check(env):
    w, la, _ = env
    s = _gated(w, la, "codex")
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert not hasattr(la, "identity_captures")
    assert w.state(s).fields["identity"] == {"observed_id": "not-a-check", "result": "not-a-check"}
    assert len(la.of("vendor")) == 1


def test_codex_with_a_uuid_baseline_is_identity_changed(env):
    w, la, _ = env
    s = _gated(w, la, "codex", baseline_id=UUID)
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    _back_in_keep(w, s, "codex")
    assert w.state(s).fields["identity"] == {"observed_id": "not-a-check", "result": "identity-changed"}


def test_claude_with_a_not_a_check_baseline_is_identity_changed(env):
    w, la, _ = env
    s = _gated(w, la, baseline_id="not-a-check")
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    _back_in_keep(w, s)


def test_a_claude_answer_of_not_a_check_never_matches_a_not_a_check_baseline(env):
    w, la, _ = env
    s = _gated(w, la, baseline_id="not-a-check", profile="not-a-check")
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    assert w.state(s).fields["identity"] == {"observed_id": "not-a-check", "result": "identity-changed"}
    _back_in_keep(w, s)


# --- test 10: the record CAS refused ----------------------------------------------------------------------------------

def test_a_refused_record_cas_refuses_by_its_name_and_parks_back(env):
    w, la, _ = env
    s = _gated(w, la)
    real = w.seams.client_factory

    class Dead:
        def __init__(self, inner):
            self.inner = inner

        def call(self, op, **f):
            if op == "CAS" and "identity" in (f.get("fields") or {}):
                raise Refused("lease-dead", "test")
            return self.inner.call(op, **f)

        def close(self):
            self.inner.close()
    w.seams.client_factory = lambda p: Dead(real(p))
    assert w.main(["start", s])[1]["refused"] == "lease-dead"
    _back_in_keep(w, s)
    assert "identity" not in w.state(s).fields


# --- test 11: the park-back fails --------------------------------------------------------------------------------------

def test_a_failed_park_back_keeps_the_gates_name_notes_it_and_end_parks(env, capsys):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    overrides = dict(w.seams.end_overrides)

    def fault(point):
        if point == "park:keep:made":
            raise OSError(errno.EIO, "injected")
    w.seams.end_overrides = {**overrides, "fault": fault}
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    assert "not parked back" in capsys.readouterr().err
    assert _tree(w, s).read_bytes() == b"{}" and not _keep(w).exists()        # the one copy is in the tree
    w.seams.end_overrides = overrides
    assert w.state(s).state == R.RENDERED       # g3fx1 VERIFY (a): END parks the tree of a session never started
    assert w.main(["teardown", s])[1]["ok"]
    assert _keep(w).read_bytes() == b"{}"                                      # END's park is the backstop


# --- g3fx1 F1: return_move fails after its rename ---------------------------------------------------------------------

@pytest.mark.parametrize("point", ["return:renamed", "return:source-synced", "return:done"])
def test_a_return_that_fails_after_the_rename_is_parked_back(env, point):
    """L3 r1 (3 lineages): an fsync (or a fault) after the rename refuses ``bad-root`` with the credential in the
    tree; start parks it back before it answers."""
    w, la, _ = env
    s = _gated(w, la)
    overrides = dict(w.seams.end_overrides)

    def fault(p):
        if p == point:
            raise OSError(errno.EIO, "injected")
    w.seams.end_overrides = {**overrides, "fault": fault}
    assert w.main(["start", s])[1]["refused"] == "bad-root"
    _back_in_keep(w, s)
    assert la.spawns == [] and "identity" not in w.state(s).fields


def test_a_return_whose_tree_fsync_fails_is_parked_back(env, monkeypatch):
    w, la, _ = env
    s = _gated(w, la)
    from levain.slot import end as E
    real = os.fsync
    tree_dir = _tree(w, s).parent
    armed = [True]

    def fsync(fd):
        if armed[0] and os.fstat(fd).st_ino == os.stat(tree_dir).st_ino and _tree(w, s).exists():
            armed[0] = False
            raise OSError(errno.EIO, "injected")
        return real(fd)
    monkeypatch.setattr(E.os, "fsync", fsync)
    assert w.main(["start", s])[1]["refused"] == "bad-root"
    monkeypatch.undo()
    _back_in_keep(w, s)


@pytest.mark.parametrize("make", ["conflict", "absent"])
def test_a_pre_move_refusal_of_the_return_moves_nothing(env, make):
    w, la, _ = env
    s = _gated(w, la)
    if make == "conflict":
        _tree(w, s).write_bytes(b"[]")
        assert w.main(["start", s])[1]["refused"] == "keep-return-conflict"
        assert _keep(w).read_bytes() == b"{}" and _tree(w, s).read_bytes() == b"[]"
    else:
        _keep(w).unlink()
        assert w.main(["start", s])[1]["refused"] == "no-credential:e0"
        assert not _keep(w).exists() and not _tree(w, s).exists()


# --- g3fx1 VERIFY (b): a retry after a gate refusal --------------------------------------------------------------------

def test_a_retry_after_identity_changed_is_not_blocked_by_the_earlier_record(env):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    assert w.state(s).fields["identity"]["result"] == "identity-changed"
    la.profile_uuid = UUID                   # the stored login answers as the baseline's account again
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "matched"}
    assert len(la.of("vendor")) == 1


# --- g3fx1 F7: the record CAS refused after the call refused ----------------------------------------------------------

def test_a_refused_cas_after_a_refused_call_keeps_the_calls_name_in_the_detail(env, monkeypatch):
    w, la, _ = env
    s = _gated(w, la)
    recorded: list = []

    def call(ctx, vendor, a=None, *, held=False):
        raise Refused("identity-unparsable", "claude")

    def cas(ctx, session, identity):
        recorded.append(dict(identity))
        raise Refused("lease-dead", "test")
    monkeypatch.setattr(ID, "identity_call", call)
    monkeypatch.setattr(ID, "_cas_gate_identity", cas)
    with pytest.raises(Refused) as e:
        ID.gate_check(None, w.alloc(s), {"vendor": "claude", "observed_id": UUID})   # type: ignore[arg-type]
    assert recorded == [{"result": "identity-unparsable"}]
    assert e.value.name == "lease-dead" and "identity-unparsable" in e.value.detail


# --- test 12: the lock -------------------------------------------------------------------------------------------------

def test_the_gate_takes_no_nested_lock_and_an_end_during_it_is_refused_end_busy(env):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    seen: list[dict] = []
    real = w.seams.capture

    def capture(argv, spec_json, cgroup, bound):
        seen.append(w.main(["teardown", s])[1])          # an END while the gate runs
        return real(argv, spec_json, cgroup, bound)
    w.seams.capture = capture
    # the END's own revoke moved the record, so the gate's record CAS is refused: never session-busy (no second lock)
    assert w.main(["start", s])[1]["refused"] == "not-rendered"
    assert len(seen) == 1 and seen[0]["ok"] is False and seen[0]["refused"].startswith("end-busy"), seen
    w.seams.capture = real
    assert w.main(["teardown", s])[1]["ok"]
    assert _keep(w).read_bytes() == b"{}"


# --- test 13: paths the gate does not touch ---------------------------------------------------------------------------

def test_a_login_session_runs_no_gate(env):
    w, la, _ = env
    s = _rendered(w, "egress", "login", "claude", "passthrough", blob=_blob(routes=PASS))
    assert w.main(["start", s])[1]["ok"] and "identity-gate" not in w.steps


def test_a_brokered_interactive_session_touches_no_keep(env):
    w, la, _ = env
    w.give_credential(E0)
    s = _rendered(w)                         # brokered interactive
    assert w.main(["start", s])[1]["ok"]
    assert "identity-gate" not in w.steps and _keep(w).read_bytes() == b"{}"
    assert not hasattr(la, "identity_captures")


# --- test 14: status ---------------------------------------------------------------------------------------------------

def test_status_carries_identity_for_an_interactive_passthrough_session(env):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    ans = w.main(["status", s])[1]
    assert ans["identity"] == {"observed_id": OTHER, "result": "identity-changed"}


def test_status_of_a_brokered_interactive_session_has_no_identity(env):
    w, la, _ = env
    s = _rendered(w)
    w.state(s)
    assert w.main(["start", s])[1]["ok"]
    assert "identity" not in w.main(["status", s])[1]
