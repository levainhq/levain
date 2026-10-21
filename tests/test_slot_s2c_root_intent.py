"""CONTRACT §7.4: root's per-session intent journal (``levain-root-intent/1``) and its reconciler.
Real files in pytest tmp dirs; the CAS is a fake CONTROL; ownership is checked against this test's uid."""
from __future__ import annotations

import os
import stat

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import root_intent as ri_mod
from levain.slot.root_intent import (INTENT_PHASES, Phase, PhaseFailed, Plan, RootIntent, begin, intent_bytes,
                                     intent_path, parse_root_intent, pending_sessions, read_root_intent,
                                     reconcile, write_root_intent)

SESSION = "s-0123456789abcdef"
NOW = "2026-10-20T18:00:00Z"


def mine(st: os.stat_result) -> bool:
    return st.st_uid == os.getuid()


class Crash(BaseException):
    pass


class Cas:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, target, fields):
        self.calls.append((target, dict(fields)))


def _rec(**kw) -> bytes:
    obj = {"format": "levain-root-intent/1", "intent": "retain", "phase": "init", "started_at": NOW, **kw}
    return canon.canonical_bytes({k: v for k, v in obj.items() if v is not None})


def test_parse_round_trip():
    ri = RootIntent("retain", "tree", NOW)
    assert parse_root_intent(intent_bytes(ri)) == ri
    p = RootIntent("purge", "check", NOW, True)
    assert parse_root_intent(intent_bytes(p)) == p


@pytest.mark.parametrize("data", [
    b"",
    b'{"format":"levain-root-intent/1", "intent":"retain","phase":"init","started_at":"2026-10-20T18:00:00Z"}',
    _rec(phase="delete"),                        # a purge phase on a retain
    _rec(intent="nuke"),
    _rec(format="levain-root-intent/2"),
    _rec(started_at="2026-10-20 18:00:00"),
    _rec(forced=True),                           # forced only on a purge
    _rec(intent="purge", phase="check", forced=False),
    _rec(extra="x"),
    canon.canonical_bytes({"format": "levain-root-intent/1", "intent": "retain", "phase": "init"}),
])
def test_malformed_refused_by_name(data):
    with pytest.raises(Refused) as e:
        parse_root_intent(data)
    assert e.value.name == "intent-malformed"


def test_write_is_0600_and_read_back(tmp_path):
    p = intent_path(tmp_path, SESSION)
    write_root_intent(p, RootIntent("park", "move", NOW))
    assert stat.S_IMODE(os.lstat(p).st_mode) == 0o600
    assert read_root_intent(p, owner_ok=mine) == RootIntent("park", "move", NOW)
    assert not (tmp_path / f"{SESSION}.intent.json.tmp").exists()
    assert pending_sessions(tmp_path) == [SESSION]


def test_read_refuses_link_foreign_owner_and_oversize(tmp_path):
    p = intent_path(tmp_path, SESSION)
    assert read_root_intent(p, owner_ok=mine) is None
    (tmp_path / "real").write_bytes(intent_bytes(RootIntent("retain", "init", NOW)))
    os.symlink("real", p)
    with pytest.raises(Refused) as e:
        read_root_intent(p, owner_ok=mine)
    assert e.value.name == "bad-root"
    os.unlink(p)
    write_root_intent(p, RootIntent("retain", "init", NOW))
    with pytest.raises(Refused) as e:
        read_root_intent(p, owner_ok=lambda st: False)   # not root's: CONTROL could have written it
    assert e.value.name == "bad-root"
    p.write_bytes(b" " * (ri_mod.MAX_INTENT_BYTES + 1))
    with pytest.raises(Refused) as e:
        read_root_intent(p, owner_ok=mine)
    assert e.value.name == "intent-malformed"


def test_bad_session_id_refused(tmp_path):
    with pytest.raises(Refused):
        intent_path(tmp_path, "../x")


def _plan(log, fail_at=None):
    def step(name):
        def run():
            if name == fail_at:
                raise OSError(5, "boom")
            log.append(name)
        return run
    return Plan("retain", tuple(Phase(n, step(n)) for n in INTENT_PHASES["retain"]),
                lambda: {"manifest": {"digest": "sha256:" + "0" * 64, "entries": 1, "bytes": 0}})


def test_plan_must_carry_contract_phases():
    with pytest.raises(ValueError):
        Plan("park", (Phase("move", lambda: None),))


def test_begin_journals_each_phase_before_its_step(tmp_path):
    p = intent_path(tmp_path, SESSION)
    log: list[str] = []
    seen: list[tuple[str, str | None]] = []

    def fault(point):
        if ":after:" in point and not point.endswith(":cas"):   # just after a phase write: the journal must already name that phase
            seen.append((point.rsplit(":", 1)[1], read_root_intent(p, owner_ok=mine).phase))
            assert point.rsplit(":", 1)[1] not in log   # ... and its step has not run
    cas = Cas()
    begin(p, _plan(log), started_at=NOW, cas=cas, fault=fault)
    assert log == list(INTENT_PHASES["retain"])
    assert all(a == b for a, b in seen) and len(seen) == 6
    assert cas.calls == [("RETAINED", {"manifest": {"digest": "sha256:" + "0" * 64, "entries": 1, "bytes": 0}})]
    assert not p.exists()


def test_begin_refuses_a_pending_journal(tmp_path):
    p = intent_path(tmp_path, SESSION)
    write_root_intent(p, RootIntent("retain", "tree", NOW))
    with pytest.raises(Refused) as e:
        begin(p, _plan([]), started_at=NOW, cas=Cas())
    assert e.value.name == "intent-pending"
    assert read_root_intent(p, owner_ok=mine).phase == "tree"


def test_failed_step_leaves_journal_at_its_phase(tmp_path):
    p = intent_path(tmp_path, SESSION)
    log: list[str] = []
    with pytest.raises(PhaseFailed) as e:
        begin(p, _plan(log, fail_at="meta"), started_at=NOW, cas=Cas())
    assert e.value.phase == "meta"
    assert read_root_intent(p, owner_ok=mine) == RootIntent("retain", "meta", NOW)
    # reconciliation moves FORWARD from the recorded phase: meta is redone, init/manifest are not
    log.clear()
    cas = Cas()
    assert reconcile(p, lambda ri: _plan(log), cas=cas, owner_ok=mine) == RootIntent("retain", "meta", NOW)
    assert log == ["meta", "object", "tree", "commit"]
    assert [c[0] for c in cas.calls] == ["RETAINED"] and not p.exists()


@pytest.mark.parametrize("point", [f"retain:{w}:{ph}" for ph in INTENT_PHASES["retain"] for w in ("before", "after")]
                         + ["retain:before:cas", "retain:after:cas"])
def test_crash_anywhere_reconciles_forward(tmp_path, point):
    p = intent_path(tmp_path, SESSION)
    log: list[str] = []

    def fault(pt):
        if pt == point:
            raise Crash(pt)
    cas = Cas()
    with pytest.raises(Crash):
        begin(p, _plan(log), started_at=NOW, cas=cas, fault=fault)
    if point == "retain:before:init":
        assert not p.exists()          # nothing written, nothing done: the caller begins again
        return
    done = list(log)
    reconcile(p, lambda ri: _plan(log), cas=cas, owner_ok=mine)
    assert not p.exists()
    assert cas.calls[-1][0] == "RETAINED"
    # every phase ran at least once, in order, and nothing ran before the phase it follows
    assert set(log) == set(INTENT_PHASES["retain"])
    order = [INTENT_PHASES["retain"].index(x) for x in log[len(done):]]
    assert order == sorted(order)


def test_unbuilt_intent_refused_untouched(tmp_path):
    p = intent_path(tmp_path, SESSION)
    write_root_intent(p, RootIntent("purge", "delete", NOW))
    with pytest.raises(Refused) as e:
        reconcile(p, lambda ri: pytest.fail("no plan may be built for a purge"), cas=Cas(), owner_ok=mine)
    assert e.value.name == "intent-unbuilt"
    assert p.exists()
