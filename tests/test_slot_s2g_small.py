"""S1 session 2 fix round 1 (lane G), the small modules: records (L1-F5 ``touched``, the slot state file),
render (L2 M3 tty paths, M6 ``disableAllHooks``, the verifier driver's TMPDIR), root_intent (M7 unique temp
names) and CONTROL (handles dropped at lease EOF before the writes; the "already done" shortcut's lease check).
Profile tests are TEXT assertions: no profile is applied here (trg-069)."""
from __future__ import annotations

import os
import re
import time

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as rd
from levain.slot import root_intent as RI
from levain.slot.ids import SlotId
from tests.test_slot_s2a_control import S1, _seed, attach_claims, cas, env, settle, to_rendered  # noqa: F401
from tests.test_slot_s2b_render import egress, verifier

OP_UID = os.getuid() if os.getuid() != 0 else 501


# --- records -----------------------------------------------------------------------------------------------


def test_touched_accepts_every_render_discovery_name():
    """L1-F5: ``touched`` and §5.5's discovery names come from ONE list (render's), nested names included."""
    names = sorted({n for ns in rd.DISCOVERY.values() for n in ns})
    assert ".config/git" in names and ".config/anthropic" in names
    for n in names:
        assert R.FIELD_CHECKS["touched"]([n]) == [n]
    assert R.FIELD_CHECKS["touched"](names) == names
    for bad in ("x", "../CLAUDE.md", "/etc/passwd", ".config"):
        with pytest.raises(Refused) as e:
            R.FIELD_CHECKS["touched"]([bad])
        assert e.value.name == "bad-value", bad


def test_touched_rides_a_real_cas(tmp_path):
    store = C.StateStore(tmp_path)
    ctl = C.Control(store, OP_UID)
    _seed(store, S1, "REVOKED")
    ctl.cas(S1, 4, "REVOKED", "REVOKED", {"touched": [".config/git", "settings.json"]})
    assert store.get(S1).touched == (".config/git", "settings.json")


def test_failed_step_names():
    for ok in ("kill", "manifest_budget", "slot-not-ours"):
        assert R.FIELD_CHECKS["failed_step"](ok) == ok
    for bad in ("Kill", "-x", "has space", "a" * 65, ""):
        with pytest.raises(Refused):
            R.FIELD_CHECKS["failed_step"](bad)


def test_slot_state_round_trip_and_refusals():
    s = R.SlotState(SlotId("egress", 3), rev=7, lease_seq=4, state="leased", dispatch_eligible=False,
                    changed_at="2026-10-20T18:00:00Z", session="s-0123456789abcdef")
    data = R.slot_state_bytes(s)
    assert R.parse_slot_state(data) == s
    free = R.SlotState(SlotId("none", 1), 2, 1, "reusable", True, "2026-10-20T18:00:00Z")
    assert R.parse_slot_state(R.slot_state_bytes(free)) == free
    with pytest.raises(Refused) as e:
        R.slot_state_bytes(R.SlotState(SlotId("egress", 3), 1, 1, "leased", False, "2026-10-20T18:00:00Z"))
    assert e.value.name == "bad-value"                       # leased names its session
    with pytest.raises(Refused) as e:
        R.parse_slot_state(data.replace(b'"leased"', b'"borrowed"'))
    assert e.value.name == "slot-state-malformed" and "bad-value" in e.value.detail
    with pytest.raises(Refused) as e:
        R.parse_slot_state(data.replace(b"{", b"{ ", 1))
    assert e.value.name == "slot-state-malformed"


# --- render ------------------------------------------------------------------------------------------------


def _dev_writes(profile: str) -> list[str]:
    """Every /dev path the profile's file-write* allow block names, and any regex there."""
    block = profile[profile.index("(allow file-write*"):profile.index("(deny network-outbound)")]
    assert "regex" not in block
    return re.findall(r'"(/dev/[^"]*)"', block)


@pytest.mark.parametrize("make", [lambda: egress(), lambda: egress(kind="dispatch"), lambda: verifier()])
def test_profile_writes_no_tty_path(make):
    """L2 M3: no write rule for /dev/ttys* (or any tty path but /dev/tty). What /dev allows is exactly this."""
    blob, facts = make()
    text = rd.render_profile(blob, facts).decode()
    assert "ttys" not in text
    assert _dev_writes(text) == ["/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/ptmx"]


def test_verifier_profile_lets_the_driver_write_its_tmpdir():
    """L1 note: the driver's TMPDIR (``<S>/driver``) is writable under the session's profile; an egress
    session's profile names no driver dir."""
    blob, facts = verifier()
    text = rd.render_profile(blob, facts).decode()
    tmpdir = rd.render_env(blob, facts)["driver"]["TMPDIR"]
    assert tmpdir == f"{facts.S}/driver" and f'(subpath "{tmpdir}")' in text
    eb, ef = egress()
    assert "/driver" not in rd.render_profile(eb, ef).decode()


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_disable_all_hooks_pinned_and_read_back(host):
    """L2 M6: pinned false in the flag layer, held by the read-back, a repo layer setting it is refused."""
    blob, facts = egress(host=host)
    assert rd.render_cc_settings(blob, facts)["disableAllHooks"] is False
    exp = rd.expected_effective_cc(blob, facts)
    assert exp["disableAllHooks"] is False
    good = rd.render_cc_settings(blob, facts)
    assert rd.readback_refusals(exp, good) == []
    assert rd.readback_refusals(exp, {**good, "disableAllHooks": True}) == ["disableAllHooks"]
    missing = {k: v for k, v in good.items() if k != "disableAllHooks"}
    assert rd.readback_refusals(exp, missing) == ["disableAllHooks"]


def test_repo_layer_disabling_hooks_refused():
    for bad in (True, "true", 1):
        out = rd.repo_loosening_refusals({".claude/settings.local.json": {"disableAllHooks": bad}})
        assert [r.name for r in out] == ["loosening:disable-all-hooks"], bad
    assert rd.repo_loosening_refusals({".claude/settings.json": {"disableAllHooks": False}}) == []


# --- root_intent: unique temp names (M7) ---------------------------------------------------------------------


def test_atomic_write_never_touches_another_writers_temp(tmp_path):
    p = tmp_path / "s-0123456789abcdef.intent.json"
    other_fixed = tmp_path / (p.name + ".tmp")            # the old fixed name another writer could hold
    other_fixed.write_bytes(b"theirs")
    other_unique = RI.temp_path(p)
    other_unique.write_bytes(b"theirs too")
    RI.atomic_write(p, b"mine")
    assert p.read_bytes() == b"mine"
    assert other_fixed.read_bytes() == b"theirs" and other_unique.read_bytes() == b"theirs too"
    assert RI.temp_path(p) != RI.temp_path(p)
    # the sweep (run under the per-session lock) removes only temp names of that base, owned as expected
    keep = tmp_path / "s-0123456789abcdef.json"
    keep.write_bytes(b"alloc")
    gone = RI.sweep_temps(tmp_path, "s-0123456789abcdef.", owner_ok=lambda st: st.st_uid == os.getuid())
    assert gone == [other_unique.name]
    assert other_fixed.exists() and keep.exists() and p.exists()
    assert RI.sweep_temps(tmp_path, "s-0123456789abcdef.", owner_ok=lambda st: False) == []


# --- CONTROL ----------------------------------------------------------------------------------------------


def test_lease_eof_drops_handles_before_the_durable_write(env):  # noqa: F811
    """L1 note: the revoke cannot write (the session lock is held, as a full disk would stall it); the
    session's token is already unresolvable."""
    op, root = to_rendered(env)
    assert env.control.handles(S1).token is not None
    holder = env.store.lock(S1)
    holder.__enter__()
    try:
        op.close()
        deadline = time.time() + 5
        while time.time() < deadline and env.control.handles(S1) is not None:
            time.sleep(0.01)
        assert env.control.handles(S1) is None
        assert env.record(S1).state == "RENDERED"             # no durable write has landed yet
    finally:
        holder.__exit__(None, None, None)
    settle(env, S1, "REVOKED")
    assert env.control.handles(S1) is None


def test_already_done_into_live_state_answers_lease_dead(tmp_path):
    """L2 L1: a retried RENDERED -> ACTIVE after the lease died answers lease-dead, not "done"."""
    store = C.StateStore(tmp_path)
    ctl = C.Control(store, OP_UID)
    _seed(store, S1, "ACTIVE")
    with pytest.raises(Refused) as e:
        ctl.cas(S1, 4, "RENDERED", "ACTIVE", {})
    assert e.value.name == "lease-dead"
    _seed(store, S1, "REVOKED")
    assert ctl.cas(S1, 0, "ACTIVE", "REVOKED", {}) == 4      # a non-live target's shortcut still answers


def test_timestamps_refuse_leap_seconds():
    """L1 F10: strptime takes seconds 60 and 61; a record's timestamp is a real calendar time."""
    assert R.check_timestamp("2026-10-20T23:59:59Z", "t") == "2026-10-20T23:59:59Z"
    for bad in ("2026-10-20T23:59:60Z", "2026-10-20T23:59:61Z", "2026-02-30T00:00:00Z"):
        with pytest.raises(Refused) as e:
            R.check_timestamp(bad, "t")
        assert e.value.name == "bad-value", bad
