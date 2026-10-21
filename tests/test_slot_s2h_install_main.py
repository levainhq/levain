"""S1 session 2e (lane H): the install helper's ``__main__`` (CONTRACT §5.2, §25), the slot state files install
writes (§3.5 Pool, §2.1) and the carried Linux undo items. Fakes and pytest tmp files only: nothing here runs as
root or touches the host's directory service."""
from __future__ import annotations

import fcntl
import functools
import io
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.firing import hands
from levain.slot import Refused, install_helper as IH, records as R, runtime_helper as RH, units
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, RETIRED_IDS_PATH, SHARED_GROUPS, SlotId, allocate
from levain.slot.install_helper import (Gathered, HostFacts, Seams, gather_facts, main, operator_record,
                                        operator_record_text, plan_for, plan_replenish, plan_setup, plan_undo)
from levain.slot.intent import Intent, install_lock, intent_text
from levain.slot.layout import RUN

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501
ENV = {"SUDO_UID": str(OP_UID)}
SETUP = ["setup", "--slots", "2", "--verifier-slots", "1", "--operator", OP]
TS = "2026-10-20T12:00:00Z"
S1 = "s-0123456789abcdef"


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


def _facts(host, **kw):
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _set_up(host, **kw):
    return _facts(host, operator_record=operator_record(OP, OP_UID), **kw)


def _mine(st):
    return st.st_uid == os.getuid()


_PLAN = functools.partial(plan_for, read_sysctl=lambda p: "1", program_exists=lambda p: True)


def _main(argv, seams, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, env=env, seams=seams, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


class _Order:
    """Records the call order of the seams main takes."""

    def __init__(self, facts=None, failed=None):
        self.calls: list[str] = []
        self.facts = facts
        self.failed = failed

    @contextmanager
    def lock(self, host):
        self.calls.append("lock")
        try:
            yield
        finally:
            self.calls.append("unlock")

    def gather(self, cmd, host, caller_uid):
        self.calls.append("gather")
        assert caller_uid == OP_UID
        return Gathered(self.facts if self.facts is not None else _facts(host))

    def plan(self, cmd, g):
        self.calls.append("plan")
        return _PLAN(cmd, g)

    def apply(self, plan, err):
        self.calls.append("apply")
        return self.failed

    def seams(self, host="darwin", euid=0):
        return Seams(euid=lambda: euid, host=lambda: host, lock=self.lock, gather=self.gather, plan=self.plan,
                     apply=self.apply)


# --- the order: lock, then facts, then plan + every step, then unlock (§25) ----------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_lock_taken_before_facts_and_held_through_every_step(host):
    o = _Order()
    rc, out, err = _main(SETUP, o.seams(host))
    assert rc == 0, err
    assert o.calls == ["lock", "gather", "plan", "apply", "unlock"]
    assert out.startswith('{"ok":true,') and out.endswith("\n") and out.count("\n") == 1
    assert '"verb":"setup"' in out and '"slots":["e0","e1","v0"]' in out


def test_refusals_before_the_lock_read_nothing():
    o = _Order()
    assert _main(SETUP, o.seams(euid=501))[:2] == (1, '{"ok":false,"refused":"not-root"}\n')
    assert _main(["setup", "--bogus"], o.seams())[1] == '{"ok":false,"refused":"extra-argv"}\n'
    assert _main(SETUP, o.seams(), env={})[1] == '{"ok":false,"refused":"no-sudo-uid"}\n'
    assert _main(SETUP, o.seams(), env={"SUDO_UID": "0"})[1] == '{"ok":false,"refused":"no-sudo-uid"}\n'
    assert o.calls == []


@pytest.mark.parametrize("argv", [["repair"], ["set-retention", "30"], ["set-dispatch-retain", "3"], ["set-ceiling"],
                                  ["set-signers"], ["purge", S1], ["force-retire", "egress", "0"]])
def test_unbuilt_verbs_refuse_by_name_before_the_lock(argv):
    o = _Order()
    rc, out, _ = _main(argv, o.seams())
    assert (rc, out) == (1, f'{{"ok":false,"refused":"verb-unbuilt:{argv[0]}"}}\n')
    assert o.calls == []
    assert argv[0] not in IH.BUILT_VERBS


def test_a_failed_step_refuses_and_still_releases_the_lock():
    o = _Order(failed="[3/9] create /x FAILED: no")
    rc, out, err = _main(SETUP, o.seams())
    assert (rc, out) == (1, '{"ok":false,"refused":"step-failed"}\n')
    assert "FAILED: no" in err and o.calls[-1] == "unlock"


def test_an_unexpected_error_is_internal_exit_70():
    o = _Order()
    o.gather = lambda cmd, host, uid: (_ for _ in ()).throw(RuntimeError("boom"))
    rc, out, err = _main(SETUP, o.seams())
    assert (rc, out) == (70, '{"ok":false,"refused":"internal"}\n')
    assert "RuntimeError: boom" in err and o.calls == ["lock", "unlock"]


@pytest.mark.parametrize("verb", ["setup", "undo", "replenish", "purge", "force-retire"])
def test_install_verbs_refuse_with_live_lease(verb):
    """CONTRACT §5.2 through main: every built verb refuses ``live-lease`` before any step; ``purge`` and
    ``force-retire`` are lease-exempt and not built (session 5), so they answer ``verb-unbuilt``."""
    argv = {"setup": SETUP, "purge": ["purge", S1], "force-retire": ["force-retire", "egress", "0"]}.get(verb, [verb])
    facts = _facts("linux", live_leases=1) if verb == "setup" else _set_up("linux", live_leases=1)
    o = _Order(facts)
    o.plan = lambda cmd, g: _PLAN(cmd, Gathered(g.facts, live={"egress": (0,), "none": (0,)}))
    rc, out, _ = _main(argv, o.seams("linux"))
    want = f"verb-unbuilt:{verb}" if verb in ("purge", "force-retire") else "live-lease"
    assert (rc, out) == (1, f'{{"ok":false,"refused":"{want}"}}\n')
    assert "apply" not in o.calls


def test_second_run_refuses_install_busy_between_journal_and_record_and_after_the_record(tmp_path):
    """The real lock (§25): a second main while the first is past its journal step, and again past its
    operator record but before the journal's removal, refuses ``install-busy`` and never gathers; once the
    first returns, the lock is free."""
    lock_path = tmp_path / "state" / "install.lock"
    gathers: list[str] = []
    seen: list[tuple[int, str, int, str]] = []
    holder: dict = {}

    def gather(cmd, host, uid):
        gathers.append(cmd.verb)
        return Gathered(_facts(host))

    def apply(plan, err):
        if holder.get("nested"):           # a second run that got in (the lock broken) stops here, not recursing
            return None
        holder["nested"] = True
        whys = [s.why for s in plan.steps]
        journal = 0
        record = next(i for i, w in enumerate(whys) if w.startswith(f"record {OP} as this machine's one operator"))
        removal = len(whys) - 1
        assert whys[journal].startswith("journal what this setup will create")
        assert whys[removal].startswith("remove the intent journal") and journal < record < removal
        for i in (journal + 1, removal):
            rc, out, _ = _main(SETUP, holder["s"])
            seen.append((i, whys[i], rc, out))
        return None

    holder["s"] = Seams(euid=lambda: 0, host=lambda: "darwin",
                        lock=lambda host: install_lock(path=lock_path, owner_ok=_mine),
                        gather=gather, plan=_PLAN, apply=apply)
    rc, out, err = _main(SETUP, holder["s"])
    assert rc == 0, err
    assert [(rc2, out2) for _, _, rc2, out2 in seen] == [(1, '{"ok":false,"refused":"install-busy"}\n')] * 2
    assert gathers == ["setup"]
    with install_lock(path=lock_path, owner_ok=_mine):
        pass


def test_apply_steps_runs_the_install_plan_through_run_plan():
    ran: list[str] = []
    ok = hands.Step("first", call=lambda: (ran.append("first"), (True, ""))[1])
    fails = hands.Step("second", call=lambda: (ran.append("second"), (False, "it broke"))[1])
    never = hands.Step("third", call=lambda: (ran.append("third"), (True, ""))[1])
    err = io.StringIO()
    plan = IH.InstallPlan("setup", "darwin", (ok,))
    assert IH.apply_steps(plan, err) is None and ran == ["first"]
    ran.clear()
    failed = IH.apply_steps(IH.InstallPlan("setup", "darwin", (ok, fails, never)), err)
    assert ran == ["first", "second"]
    assert failed == "[2/3] second FAILED: it broke" and "[1/3] first" in err.getvalue()


# --- the slot state file: one definition, the runtime helper's (§3.5 Pool, §2.1) ---------------------------


def _store(tmp_path, *slots: SlotId) -> Path:
    L = tmp_path / "L"
    (L / "slots").mkdir(parents=True)
    (L / "slots" / "pool.lock").write_text("")
    for s in slots:
        s.slot_dir(L).mkdir()
    return L


def _store_seams(L, host="darwin"):
    return RH.Seams(host=host, store=L, trusted=lambda st: True, lock_wait_s=0.05)


def test_state_file_round_trips_through_the_runtime_helper_reader(tmp_path):
    e0, v0 = SlotId("egress", 0), SlotId("none", 0)
    L = _store(tmp_path, e0, v0)
    for s in (e0, v0):
        assert IH.write_initial_slot_state(L, s, clock=lambda: R.epoch_of(TS)) == (True, "")
    path = e0.slot_dir(L) / "state"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert R._parse_canonical(path.read_bytes(), "slot-state") == {
        "format": "levain-slot-state/1", "class": "egress", "index": 0, "rev": 0, "lease_seq": 0,
        "state": "reusable", "dispatch_eligible": True, "changed_at": TS}
    store = RH.Store(_store_seams(L))
    try:
        views = [RH.read_view(store, s) for s in (e0, v0)]
    finally:
        store.close()
    assert [v.state.state for v in views] == ["reusable", "reusable"]
    assert RH.pick_slot(views, slot_class="egress", kind="interactive", vendor="claude", auth="brokered",
                        named=None, ticketed=False) == e0
    assert RH.pick_slot(views, slot_class="none", kind="verifier", vendor="claude", auth="none",
                        named=None, ticketed=False) == v0


def test_state_file_eligibility_and_rerun_never_resets(tmp_path):
    e0, e1 = SlotId("egress", 0), SlotId("egress", 1)
    L = _store(tmp_path, e0, e1)
    (e0.slot_dir(L) / "keep").mkdir()
    (e0.slot_dir(L) / "keep" / ".credentials.json").write_text("{}")
    assert IH.write_initial_slot_state(L, e0)[0]
    assert RH.parse_slot_state_of((e0.slot_dir(L) / "state").read_bytes(), e0).dispatch_eligible is False
    newer = R.SlotState(e1, rev=7, lease_seq=3, state="leased", dispatch_eligible=True, changed_at=TS, session=S1)
    (e1.slot_dir(L) / "state").write_bytes(R.slot_state_bytes(newer))
    ok, why = IH.write_initial_slot_state(L, e1)
    assert ok and "already has its state (leased, rev 7)" in why
    assert RH.parse_slot_state_of((e1.slot_dir(L) / "state").read_bytes(), e1) == newer
    (e1.slot_dir(L) / "state").write_bytes(b"{}")
    with pytest.raises(Refused) as ei:
        IH.write_initial_slot_state(L, e1)
    assert ei.value.name == "slot-state-malformed"


def test_state_writes_wait_for_the_pool_lock(tmp_path):
    e0 = SlotId("egress", 0)
    L = _store(tmp_path, e0)
    fd = os.open(L / "slots" / "pool.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for fn in (IH.write_initial_slot_state, IH.remove_slot_state):
            with pytest.raises(Refused) as ei:
                fn(L, e0, waits=_store_seams(L))
            assert ei.value.name == "pool-locked"
        assert not (e0.slot_dir(L) / "state").exists()
    finally:
        os.close(fd)


def test_remove_slot_state(tmp_path):
    e0 = SlotId("egress", 0)
    L = _store(tmp_path, e0)
    IH.write_initial_slot_state(L, e0)
    assert IH.remove_slot_state(L, e0) == (True, "")
    assert not (e0.slot_dir(L) / "state").exists()
    assert IH.remove_slot_state(L, e0) == (True, "already gone")
    (L / "slots" / "pool.lock").unlink()                       # a store emptied by hand: undo still runs
    assert IH.remove_slot_state(L, e0) == (True, "already gone")
    with pytest.raises(FileNotFoundError):                    # setup / replenish never write unlocked
        IH.write_initial_slot_state(L, e0)


def test_state_steps_run_on_their_store(tmp_path):
    e0, v0 = SlotId("egress", 0), SlotId("none", 0)
    L = _store(tmp_path, e0, v0)
    steps = IH.slot_state_steps("linux", [v0, e0, e0], store=L)
    assert [s.call() for s in steps] == [(True, ""), (True, "")]
    assert all((s.slot_dir(L) / "state").exists() for s in (e0, v0))
    undo = IH.slot_state_undo_steps("linux", [e0, v0], store=L)
    assert [s.call()[0] for s in undo] == [True, True]
    assert not any((s.slot_dir(L) / "state").exists() for s in (e0, v0))


def _index(whys, prefix):
    return [i for i, w in enumerate(whys) if w.startswith(prefix)]


@pytest.mark.parametrize("host", HOSTS)
def test_setup_marks_every_slot_after_its_confinement_and_before_the_runtime_rule(host):
    plan = plan_setup(parse_install_argv(SETUP), _facts(host), read_sysctl=lambda p: "1", program_exists=lambda p: True)
    whys = [s.why for s in plan.steps]
    marks = _index(whys, "mark ")
    assert [whys[i].split()[1] for i in marks] == ["e0", "e1", "v0"]
    sudoers = [i for i, s in enumerate(plan.steps) if s.write and "sudoers.d" in str(s.write[0])]
    record = _index(whys, f"record {OP}")[0]
    confinement = [i for i, w in enumerate(whys) if "pf" in w or "netns" in w or "bwrap" in w or "lo0" in w]
    assert max(confinement) < min(marks) and max(marks) < min(sudoers) < record


@pytest.mark.parametrize("host", HOSTS)
def test_replenish_marks_its_new_slots_after_confinement_just_before_the_journal_goes(host):
    al = allocate(host, egress=(SlotId("egress", 0),), verifier=(SlotId("none", 0),), used=(), retired=())
    plan = plan_replenish(parse_install_argv(["replenish"]), _set_up(host), slots=2, verifier_slots=1,
                          live={"egress": (0,), "none": (0,)}, accounts=dict(al.ids))
    whys = [s.why for s in plan.steps]
    marks = _index(whys, "mark ")
    assert [whys[i].split()[1] for i in marks] == ["e1"]       # the new slot only; e0 / v0 keep theirs
    assert marks == [len(whys) - 2] and whys[-1].startswith("remove the intent journal")


def _undo_plan(host):
    al = allocate(host, egress=(SlotId("egress", 0),), verifier=(SlotId("none", 0),), used=(), retired=())
    return plan_undo(parse_install_argv(["undo"]), _set_up(host), accounts=dict(al.ids), shared_groups=SHARED_GROUPS)


@pytest.mark.parametrize("host", HOSTS)
def test_undo_removes_each_slot_state_once_the_services_are_stopped(host):
    whys = [s.why for s in _undo_plan(host).steps]
    removes = _index(whys, "remove /")
    states = [i for i in removes if whys[i].endswith("/state (under the pool lock)")]
    assert [whys[i].split()[1].split("/")[-2] for i in states] == ["e0", "v0"]
    stopped = [i for i, w in enumerate(whys) if w.startswith(("stop ", "check ", "tell systemd"))
               and "every process" not in w]
    retire = _index(whys, "retire every account id")[0]
    assert max(stopped) < min(states) and max(states) < retire


def test_linux_undo_clears_failed_units_and_removes_the_run_dir():
    steps = _undo_plan("linux").steps
    whys = [s.why for s in steps]
    reload = _index(whys, "tell systemd")[0]
    resets = [s for s in steps if s.argv[1:2] == ("reset-failed",)]
    assert [s.argv[2] for s in resets] == list(units.start_order("linux"))
    assert all(s.allow_fail and s.argv[0].endswith("systemctl") for s in resets)
    assert min(whys.index(s.why) for s in resets) > reload
    rmrun = [i for i, s in enumerate(steps) if s.argv[-1:] == (str(RUN["linux"]),)]
    assert len(rmrun) == 1 and steps[rmrun[0]].argv[1:] == ("-rf", "--one-file-system", "--", "/run/levain")
    assert not steps[rmrun[0]].allow_fail
    tombstones = [i for i, w in enumerate(whys) if "every process of" in w]
    tmpfiles = _index(whys, "remove /etc/tmpfiles.d/levain.conf")[0]
    assert max(tombstones) < tmpfiles < rmrun[0] < _index(whys, "remove the operator record")[0]


def test_darwin_undo_has_neither_linux_item():
    steps = _undo_plan("darwin").steps
    assert not any("reset-failed" in s.argv for s in steps)
    assert not any(str(RUN["darwin"]) in s.argv for s in steps)


# --- gathering the facts under the lock -------------------------------------------------------------------


class _Run:
    def __init__(self, outputs, fail=()):
        self.outputs, self.fail, self.calls = outputs, fail, []

    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        key = " ".join(argv[1:])
        if key in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="dscl: nope")
        return SimpleNamespace(returncode=0, stdout=self.outputs[key], stderr="")


def _session_record(lease_live):
    return R.state_bytes(R.SessionState(session=S1, generation=0, state=R.ALLOCATED, claims={}, lease_live=lease_live,
                                        event_kind="state", updated_at=TS))


def test_count_live_leases(tmp_path):
    d = tmp_path / "sessions"
    assert IH.count_live_leases(d) == 0                        # not set up: no sessions dir
    d.mkdir()
    (d / f"{S1}.json").write_bytes(_session_record(True))
    (d / "s-1111111111111111.json").write_bytes(_session_record(False).replace(S1.encode(), b"s-1111111111111111"))
    (d / "tickets.jsonl").write_text("not a record")
    (d / f"{S1}.lock").write_text("")
    assert IH.count_live_leases(d) == 1
    (d / "s-2222222222222222.json").write_text("garbage")    # unreadable: counted live, so install refuses
    assert IH.count_live_leases(d) == 2


def _darwin_fixture(tmp_path):
    r = RANGES["darwin"]
    s3, s1, keys, slots_gid = r.slots[3], r.slots[9], r.singletons[0], r.shared[0]
    run = _Run({
        ". -list /Users UniqueID": f"root 0\n{OP} {OP_UID}\n_levain_s3 {s3}\n_levain_s1 {s1}\n",
        ". -list /Groups PrimaryGroupID": f"wheel 0\nlevain-slots {slots_gid}\n",
        ". -list /Users RealName": f"root System  Administrator\n{OP} Alice\n_levain_s3 Levain session account\n"
                                   "_levain_s1 Levain session account (retired)\n",
        ". -list /Groups RealName": "wheel\nlevain-slots Levain session account\n",
    })
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "policy" / "operator").write_text(operator_record_text(OP, OP_UID))
    (L / "sessions").mkdir()
    (L / "sessions" / f"{S1}.json").write_bytes(_session_record(True))
    state = tmp_path / "state"
    state.mkdir()
    (state / RETIRED_IDS_PATH["darwin"].name).write_text("7\n")
    (state / "install-intent").write_text(intent_text(Intent("setup", OP, {"_levain_keys": keys},
                                                             {"levain-slots": slots_gid})))
    return run, L, state, dict(s3=s3, s1=s1, gid=slots_gid)


def test_gather_facts_darwin(tmp_path):
    run, L, state, ids = _darwin_fixture(tmp_path)
    for verb in (SETUP, ["undo"]):
        g = gather_facts(parse_install_argv(verb), "darwin", OP_UID, run=run, store_seams=_store_seams(L),
                         state_dir=state, user_name=lambda uid: OP)
        f = g.facts
        assert f.levain_live == {"_levain_s3": ids["s3"], "levain-slots": ids["gid"]}
        assert f.levain_tombstones == {"_levain_s1": ids["s1"]}
        assert f.host_ids == {"levain-slots": ids["gid"]}           # _levain_keys is journaled, not present
        assert f.intent is not None and f.intent.verb == "setup"
        assert f.operator_record == operator_record(OP, OP_UID) and f.live_leases == 1
        assert f.retired == frozenset({7}) and (f.caller_uid, f.caller_name) == (OP_UID, OP)
        assert {0, OP_UID, ids["s3"], ids["s1"], ids["gid"]} == set(f.used_ids)
        assert "wheel" in f.existing_names and "root" in f.existing_names
    assert f.operator_uid is None                                      # undo names no operator
    assert (g.accounts, g.shared_groups) == ({"_levain_s3": ids["s3"]}, ("levain-slots",))
    g = gather_facts(parse_install_argv(SETUP), "darwin", OP_UID, run=run, store_seams=_store_seams(L),
                     state_dir=state, user_name=lambda uid: OP)
    assert g.facts.operator_uid == OP_UID


def test_gather_facts_linux_groups_carry_no_label(tmp_path):
    r = RANGES["linux"]
    s0 = r.slots[0]
    run = _Run({
        "passwd": f"root:x:0:0:root:/root:/bin/bash\n{OP}:x:{OP_UID}:{OP_UID}:Alice,,,:/home/a:/bin/bash\n"
                  f"_levain_s0:x:{s0}:{s0}:Levain session account:/nonexistent:/usr/sbin/nologin\n",
        "group": f"root:x:0:\nlevain-slots:x:{r.shared[0]}:{OP}\n",
    })
    L = tmp_path / "L"
    L.mkdir()
    g = gather_facts(parse_install_argv(["undo"]), "linux", OP_UID, run=run, store_seams=_store_seams(L, "linux"),
                     state_dir=tmp_path, user_name=lambda uid: OP)
    assert g.facts.levain_live == {"_levain_s0": s0}               # no group: Linux groups have no label
    assert g.shared_groups == ("levain-slots",) and g.facts.live_leases == 0 and g.facts.operator_record is None
    assert run.calls[0][0].endswith("getent")


def test_gather_replenish_reads_the_slots_in_service(tmp_path):
    run, L, state, ids = _darwin_fixture(tmp_path)
    e0, e1, v0 = SlotId("egress", 0), SlotId("egress", 1), SlotId("none", 0)
    (L / "slots").mkdir()
    (L / "slots" / "pool.lock").write_text("")
    for s in (e0, e1, v0):
        s.slot_dir(L).mkdir()
    IH.write_initial_slot_state(L, e0)
    retired = R.SlotState(e1, rev=4, lease_seq=2, state="retired", dispatch_eligible=True, changed_at=TS)
    (e1.slot_dir(L) / "state").write_bytes(R.slot_state_bytes(retired))
    (state / "install-intent").unlink()
    g = gather_facts(parse_install_argv(["replenish"]), "darwin", OP_UID, run=run, store_seams=_store_seams(L),
                     state_dir=state, user_name=lambda uid: OP)
    assert g.live == {"egress": (0,), "none": (0,)}                 # e1 retired; v0 has no state file yet
    assert g.accounts == {}                                         # _levain_s3 is no slot in service


def test_gather_reads_the_operator_record_without_following_a_link(tmp_path):
    run, L, state, _ = _darwin_fixture(tmp_path)
    real = tmp_path / "elsewhere"          # an intermediate dir planted as a link, which a path read follows
    (L / "policy").rename(real)
    (L / "policy").symlink_to(real)
    with pytest.raises(Refused) as ei:
        gather_facts(parse_install_argv(["undo"]), "darwin", OP_UID, run=run, store_seams=_store_seams(L),
                     state_dir=state, user_name=lambda uid: OP)
    assert ei.value.name == "bad-root"
    g = gather_facts(parse_install_argv(["undo"]), "darwin", OP_UID, run=run,
                     store_seams=_store_seams(tmp_path / "absent"), state_dir=state, user_name=lambda uid: OP)
    assert g.facts.operator_record is None and g.facts.live_leases == 0     # no store yet: not set up


@pytest.mark.parametrize("bad", ["fail", "short"])
def test_a_directory_read_that_fails_or_does_not_parse_refuses(bad):
    outputs = {"passwd": "root:x:0:0:root:/root:/bin/bash\n", "group": "root:x:0:\n"}
    if bad == "short":
        outputs["group"] += "broken-line\n"
    run = _Run(outputs, fail={"passwd"} if bad == "fail" else ())
    with pytest.raises(Refused) as ei:
        IH.read_directory("linux", run=run)
    assert ei.value.name == "host-read-failed"


def test_a_directory_with_no_root_refuses():
    run = _Run({"passwd": "alice:x:501:501::/h:/bin/sh\n", "group": "root:x:0:\n"})
    with pytest.raises(Refused) as ei:
        IH.read_directory("linux", run=run)
    assert ei.value.name == "host-read-failed"


def test_live_lease_refusal_names_the_sessions(tmp_path):
    """VM-Y Y7a (levain-memory 1019-s1design/runs/s3-macos-d1d2/raw/Y7_undo_orphan.txt): undo met a lease whose
    holder had died and refused live-lease naming no session. The refusal now names what it counted."""
    d = tmp_path / "sessions"
    d.mkdir()
    (d / f"{S1}.json").write_bytes(_session_record(True))
    (d / "s-2222222222222222.json").write_text("garbage")
    assert IH.live_lease_sessions(d) == (S1, "s-2222222222222222")
    assert IH.count_live_leases(d) == 2
    facts = IH.HostFacts(host="linux", caller_uid=501, live_leases=2, live_sessions=IH.live_lease_sessions(d))
    with pytest.raises(IH.Refused) as e:
        IH.preflight(IH.parse_install_argv(["undo"]), facts)
    assert e.value.name == "live-lease"
    assert S1 in str(e.value) and "s-2222222222222222" in str(e.value)
