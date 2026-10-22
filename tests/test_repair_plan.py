"""Row 33 R1 (project_memory/1022-8/upgrade/DESIGN.md §5 R1): the repair plan layer.

Two tests, each with every sub-case the design names:

* ``test_repair_plans_from_pre_change_host``: (a) a host set up at 32939b5e (8 shared groups, keys not in gwfronts,
  the legacy tree, no ``install-shape``) plans the addition path in §2.3's order; (b) re-planned from its own journal
  it keeps the journaled gid; (c) the record and journal refusals (``downgrade-unsupported:<kind>``, ``bad-record``)
  and the generation id's execute-bit and empty-directory cases.
* ``test_repair_completion_is_the_txn``: the journal's ``txn`` against the record's ``completed_txn`` is the only
  completion proof (§8 N1, N1a): replenish refuses an unfinished repair journal and overwrites a finished one, and a
  repair meeting its own finished journal removes it first and journals a fresh ``txn``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import intent as I
from levain.slot import root_tree, units
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, loopback_alias
from levain.slot.intent import Intent, parse_intent, intent_text

OP, OP_UID = "phill", 501
HOST = "darwin"
OLD_GROUPS = SHARED_GROUPS[:8]                     # setup at 32939b5e: no levain-keysadmin
SINGLETON_UIDS = {"_levain_keys": 3799, "_levain_export": 3798, "_levain_control": 3797, "_levain_qbroker": 3796,
                  "_levain_dispatch": 3795}
SLOT_UIDS = {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_v0": 3997, "_levain_c0": 3996}
#: CONTRACT §1.2 as it stood at 32939b5e: keys was in control only (E12 and 3b-3b came later).
OLD_SUPPLEMENTARY = {"_levain_keys": ("levain-control",), "_levain_export": (),
                     "_levain_control": ("levain-wake",), "_levain_qbroker": ("levain-control", "levain-queue"),
                     "_levain_dispatch": ("levain-control", "levain-dispatch-export", "levain-lineage", "levain-queue"),
                     "_levain_s0": ("levain-slots",), "_levain_gw0": ("levain-gwfronts",),
                     "_levain_v0": ("levain-slots",), "_levain_c0": ("levain-slots",)}
OPERATOR_GROUPS = ("levain-control", "levain-export", "levain-queue")
TXN_A = "a" * 32
TXN_B = "b" * 32


def _package(tmp: Path) -> tuple[Path, Path]:
    """A source package pair: a ``levain`` dir holding every service program the units name, and ``h11``."""
    lv = tmp / "src" / "levain"
    for rel in ("__init__.py", "slot/__init__.py"):
        (lv / rel).parent.mkdir(parents=True, exist_ok=True)
        (lv / rel).write_text("__version__ = \"1.0.0\"\n")
    h11 = tmp / "src" / "h11"
    h11.mkdir(parents=True, exist_ok=True)
    (h11 / "__init__.py").write_text("")
    (h11 / "_version.py").write_text('__version__ = "0.16.0"\n')
    return lv, h11


def _members(groups: dict[str, int]) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {g: set() for g in groups}
    for acct, gs in OLD_SUPPLEMENTARY.items():
        for g in gs:
            out[g].add(acct)
    for g in OPERATOR_GROUPS:
        out[g].add(OP)
    return {g: frozenset(m) for g, m in out.items()}


def _pre_change(tmp: Path, *, foreign: dict[str, int] | None = None, intent: Intent | None = None,
                shape: G.InstallShape | None = None) -> tuple[IH.HostFacts, IH.RepairFacts]:
    shared = list(RANGES[HOST].shared)
    groups = {g: shared[i] for i, g in enumerate(OLD_GROUPS)}
    accounts = {**SINGLETON_UIDS, **SLOT_UIDS}
    foreign = dict(foreign or {})
    names = {*accounts, *groups, *foreign, OP, "root", "wheel"}
    used = {*accounts.values(), *groups.values(), *foreign.values(), OP_UID, 0}
    host_ids = {}
    if intent is not None:
        host_ids = {n: i for n, i in {**groups, **accounts}.items() if n in intent.names}
    facts = IH.HostFacts(host=HOST, caller_uid=OP_UID, caller_name=OP,
                         operator_record={"format": "levain-operator/1", "name": OP, "uid": OP_UID},
                         used_ids=frozenset(used), existing_names=frozenset(names),
                         levain_live={**accounts, **groups}, intent=intent, host_ids=host_ids, install_shape=shape,
                         tool_present=lambda p: True, xcode_ready=lambda: True)
    lv, h11 = _package(tmp)
    rf = IH.RepairFacts(accounts=accounts, groups={**groups, **foreign}, members=_members(groups),
                        egress=(0,), verifier=(0,), current=None, legacy=True, source=lv, h11=h11)
    return facts, rf


def _plan(facts, rf, argv=("repair", "--source", "/opt/x/levain"), **kw):
    kw.setdefault("program_exists", lambda p: True)
    return IH.plan_repair(parse_install_argv(list(argv)), facts, rf, **kw)


def _first(whys: list[str], pred) -> int:
    return next(i for i, w in enumerate(whys) if pred(w))


def _all(whys: list[str], pred) -> list[int]:
    return [i for i, w in enumerate(whys) if pred(w)]


def test_repair_plans_from_pre_change_host(tmp_path: Path) -> None:
    # --- (a) the addition path, in §2.3's order -----------------------------------------------------------------
    facts, rf = _pre_change(tmp_path)
    plan = _plan(facts, rf)
    whys = [s.why for s in plan.steps]
    shared = list(RANGES[HOST].shared)
    gid = plan.intent.groups["levain-keysadmin"]
    assert gid == shared[8] and gid not in facts.used_ids            # a NEW in-range gid, the next one down
    assert plan.intent.groups == {**{g: shared[i] for i, g in enumerate(OLD_GROUPS)}, "levain-keysadmin": gid}
    assert whys[0].startswith("journal what this repair") and plan.intent.verb == "repair"
    assert G.TXN_RE.fullmatch(plan.intent.txn)

    stops = _all(whys, lambda w: w.startswith("stop com.levain."))
    assert {whys[i] for i in stops} == {f"stop com.levain.{n}" for n in
                                        ("control", "keys", "qbroker", "dispatch", "reaper", "pf")}
    assert [whys[i] for i in stops][0] == "stop com.levain.pf"       # reverse start order
    creates_group = _all(whys, lambda w: w.startswith("create the group"))
    assert [whys[i] for i in creates_group] == ["create the group levain-keysadmin (shared)"]
    pairs = _all(whys, lambda w: w.startswith("put "))
    assert sorted(whys[i] for i in pairs) == ["put _levain_control in levain-keysadmin",
                                              "put _levain_keys in levain-gwfronts",
                                              "put _levain_keys in levain-keysadmin"]
    flips = _all(whys, lambda w: w.startswith("flip "))
    assert len(flips) == 1 and whys[flips[0]] == f"flip {root_tree.current_path(HOST)} -> {plan.generation}"
    stage = _all(whys, lambda w: "staging.partial" in w and w.startswith("copy the "))
    assert len(stage) == 2                                           # ONE generation holds both packages
    renames = _all(whys, lambda w: w.startswith("rename ") and plan.generation in w)
    assert len(renames) == 1
    entries = _all(whys, lambda w: w.startswith("write the ") and "entry script" in w)
    sudoers = _all(whys, lambda w: "(sudoers drop-in" in w)
    starts = _all(whys, lambda w: w.startswith("start com.levain."))
    shape = _first(whys, lambda w: w.startswith("write the install record"))
    gone = _first(whys, lambda w: w.startswith("remove the intent journal"))
    mutations = creates_group + pairs + _all(whys, lambda w: w.startswith(("create /", "own /", "set /"))
                                             and root_tree.STAGING not in w)
    # every service stops FIRST: before any group, membership, row, flip, entry, sudoers or unit act
    assert max(stops) < min(mutations + flips + entries + sudoers)
    assert max(creates_group) < min(pairs)
    assert renames[0] < flips[0] < min(entries)                     # staged, then ONE flip before any entry write
    assert max(sudoers) < min(starts)                                # sudoers before any start
    assert len(starts) == 6
    assert max(starts) < shape < gone == len(whys) - 1               # install-shape, then the journal goes, last
    assert plan.generation_change is True                            # no record: a generation change

    # the write-ahead managed set (R4b): desired, typed, with the new gid
    m = set(plan.intent.managed)
    assert ("group/1", ("levain-keysadmin", gid)) in m
    assert ("member/1", ("_levain_keys", "levain-keysadmin", gid)) in m
    assert ("member/1", (OP, "levain-queue", shared[7])) in m
    assert {("entry/1", n) for n in ("install-helper", "runtime-helper", "service-entry")} <= m
    assert {("sudoers/1", "levain-runtime"), ("sudoers/1", "levain-install"), ("pf-enable/1", "/var/db/levain/pf.token"),
            ("pf-anchor/1", ("com.apple/levain", "/etc/pf.anchors/levain")), ("pf-alias/1", "127.0.1.1")} <= m
    G.check_managed(HOST, plan.intent.managed, operator=OP)          # what it journals passes its own bounds

    # --- (b) re-planned from its own journal: the same gid, the same txn ------------------------------------------
    # The first plan met a foreign group at the next free gid, so keysadmin went one lower; on the re-plan that
    # group is gone, and a fresh allocation would take it. The journal decides.
    facts1, rf1 = _pre_change(tmp_path / "b1", foreign={"admins": shared[8]})
    first = _plan(facts1, rf1)
    taken = first.intent.groups["levain-keysadmin"]
    assert taken == shared[9]
    journaled = parse_intent(intent_text(first.intent).encode(), HOST)
    facts2, rf2 = _pre_change(tmp_path / "b2", intent=journaled)
    again = _plan(facts2, rf2)
    assert again.intent.groups["levain-keysadmin"] == taken
    assert again.intent.txn == first.intent.txn                       # resumed, never minted afresh
    assert [s.why for s in again.steps][0].startswith("journal what this repair")

    # --- (c) the refusals: unknown kinds and broken bounds, before any act ------------------------------------------
    def shape_with(*recs) -> G.InstallShape:
        return G.InstallShape(generation="gen-" + "0" * 16, levain="1.0", h11="0.16.0", completed_txn=TXN_A,
                              started_after={}, managed=tuple(recs))

    for recs, name in (
        ([("apparmor/9", "levain-slot-bwrap")], "downgrade-unsupported:apparmor/9"),
        ([("group/1", ("admin", 80))], "bad-record"),
        ([("member/1", ("_levain_keys", "levain-control", 80))], "bad-record"),     # gid outside the shared range
    ):
        f, r = _pre_change(tmp_path / f"c-{name}-{recs[0][0].replace('/', '')}", shape=shape_with(*recs))
        with pytest.raises(Refused) as e:
            _plan(f, r)
        assert e.value.name == name, (recs, e.value)
    # an unknown but well-formed kind in a JOURNAL parses, its identity opaque, and the plan refuses it
    j = Intent("repair", OP, txn=TXN_B, generation=None, managed=(("widget/3", ("anything", {"k": [1, 2]})),))
    parsed = parse_intent(intent_text(j).encode(), HOST)
    assert parsed.managed[0][0] == "widget/3" and isinstance(parsed.managed[0][1], G.Opaque)
    f, r = _pre_change(tmp_path / "c-widget", intent=parsed)
    with pytest.raises(Refused) as e:
        _plan(f, r)
    assert e.value.name == "downgrade-unsupported:widget/3"

    # the id: one file's execute bit, or one empty directory, moves it (r2's algorithm kept both ids equal)
    def tree(root: Path, *, x: bool = False, empty: bool = False) -> str:
        lv, h11 = root / "levain", root / "h11"
        (lv / "templates").mkdir(parents=True)
        (lv / "templates" / "hook").write_text("#!/bin/sh\n")
        os.chmod(lv / "templates" / "hook", 0o755 if x else 0o644)
        if empty:
            (lv / "empty").mkdir()
        (lv / "__pycache__").mkdir()                                  # dropped: never part of the id
        (lv / "__pycache__" / "m.pyc").write_bytes(os.urandom(8))
        h11.mkdir()
        (h11 / "_version.py").write_text('__version__ = "0.16.0"\n')
        return G.generation_id(lv, h11)

    base = tree(tmp_path / "id0")
    assert G.GEN_RE.fullmatch(base) and base == tree(tmp_path / "id0b")
    assert tree(tmp_path / "id1", x=True) != base
    assert tree(tmp_path / "id2", empty=True) != base


def test_repair_completion_is_the_txn(tmp_path: Path) -> None:
    gen = "gen-" + "1" * 16
    shape = G.InstallShape(generation=gen, levain="1.0", h11="0.16.0", completed_txn=TXN_A, started_after={},
                           managed=())

    def facts_with(journal_txn: str) -> IH.HostFacts:
        j = Intent("repair", OP, txn=journal_txn, generation=gen, groups={}, managed=())
        f, _ = _pre_change(tmp_path / journal_txn[:1], intent=parse_intent(intent_text(j).encode(), HOST),
                           shape=shape)
        return f

    replenish = parse_install_argv(["replenish"])
    # same generation, different txns: the repair did not reach R14, so replenish refuses it by name
    with pytest.raises(Refused) as e:
        IH.plan_replenish(replenish, facts_with(TXN_B), slots=1, verifier_slots=1, live={"egress": (0,), "none": (0,)},
                          accounts=SLOT_UIDS)
    # U7 (HEAD 1022-12): one shape for the refusal, setup's (name install-interrupted, the verb as detail), so the
    # rendered text is identical whichever verb meets the unfinished repair
    assert (e.value.name, e.value.detail) == ("install-interrupted", "repair")
    with pytest.raises(Refused) as s_e:
        IH.plan_setup(parse_install_argv(["setup", "--slots", "1", "--verifier-slots", "1", "--operator", OP]),
                      facts_with(TXN_B))
    assert str(s_e.value) == str(e.value) == "install-interrupted: repair"
    # equal txns: the repair finished; replenish overwrites its journal with its own
    plan = IH.plan_replenish(replenish, facts_with(TXN_A), slots=1, verifier_slots=1,
                             live={"egress": (0,), "none": (0,)}, accounts=SLOT_UIDS)
    assert plan.steps[0].why.startswith("journal what this replenish") and plan.intent.verb == "replenish"
    assert G.TXN_RE.fullmatch(plan.intent.txn) and plan.intent.txn != TXN_A

    # a repair meeting a journal whose txn == record.completed_txn: its first act retires that journal, and it
    # journals a FRESH txn, never resuming the finished one
    f = facts_with(TXN_A)
    _, rf = _pre_change(tmp_path / "r")
    rplan = _plan(f, rf)
    whys = [s.why for s in rplan.steps]
    assert whys[0].startswith("remove the finished repair journal")
    assert whys[1].startswith("journal what this repair")
    assert rplan.intent.txn != TXN_A and G.TXN_RE.fullmatch(rplan.intent.txn)
    # a repair meeting an interrupted replenish renders that refusal exactly as setup does (U7)
    rj = Intent("replenish", OP, {"_levain_s1": RANGES[HOST].slots[0]}, txn=TXN_B)
    rjf, _ = _pre_change(tmp_path / "rj", intent=parse_intent(intent_text(rj).encode(), HOST), shape=shape)
    with pytest.raises(Refused) as r_e:
        _plan(rjf, rf)
    with pytest.raises(Refused) as s_e:
        IH.plan_setup(parse_install_argv(["setup", "--slots", "1", "--verifier-slots", "1", "--operator", OP]), rjf)
    assert str(r_e.value) == str(s_e.value) == "install-interrupted: replenish"
    # ...while an unfinished one (txn differs) is resumed at its own txn. U13: its journal names no generation yet
    # (killed before R3), so --source may still establish one; a journaled generation would bind it (see below)
    j = Intent("repair", OP, txn=TXN_B, generation=None, groups={}, managed=())
    f, _ = _pre_change(tmp_path / "u", intent=parse_intent(intent_text(j).encode(), HOST), shape=shape)
    rplan = _plan(f, rf)
    assert rplan.intent.txn == TXN_B and rplan.steps[0].why.startswith("journal what this repair")


# --- L3 r1 fix round (HEAD 1022-12 rulings U13-U18, triage R-a..R-k) ------------------------------------------------
# A crash after step k is the journal the plan's OWN journal step bodies 0..k leave (the install helper's plan is data;
# its crash state is the journal plus what the earlier steps made), so each crash point below runs those bodies with the
# write captured and re-plans on the journal they left. No timer, no hand-built journal.


def _crash_after(plan, k: int, monkeypatch) -> Intent | None:
    written: list[str] = []

    def capture(path, text):
        written.append(text)
        return True, ""

    with monkeypatch.context() as m:
        m.setattr(I, "write_intent_atomically", capture)
        m.setattr(IH, "write_intent_atomically", capture)
        for s in plan.steps[:k + 1]:
            if s.why.startswith("journal "):
                assert s.call() == (True, "")
    return parse_intent(written[-1].encode(), HOST) if written else None


def _step(plan, prefix: str) -> int:
    return _first([s.why for s in plan.steps], lambda w: w.startswith(prefix))


def _shape(gen: str, txn: str = TXN_A, *, levain: str = "0.9.0", h11: str = "0.16.0", managed=()) -> G.InstallShape:
    return G.InstallShape(gen, levain, h11, txn, {}, tuple(managed))


def _other_package(tmp: Path) -> tuple[Path, Path]:
    lv, h11 = _package(tmp)
    (lv / "slot" / "changed.py").write_text("X = 1\n")
    return lv, h11


def _written_record(plan, monkeypatch) -> G.InstallShape:
    got: list[str] = []
    with monkeypatch.context() as m:
        m.setattr(G, "write_record_durably", lambda p, t, **kw: (got.append(t), (True, ""))[1])
        assert plan.steps[_step(plan, "write the install record")].call() == (True, "")
    return G.parse_shape(got[-1].encode())


def test_resumed_repair_target_is_the_journals(tmp_path: Path, monkeypatch) -> None:
    """U13 (codex HIGH 1, 2): an unfinished repair journal fixes the target; never the old shape, never a new source."""
    gen_a = "gen-" + "a" * 16
    facts, rf = _pre_change(tmp_path / "a", shape=_shape(gen_a))
    rf = replace(rf, current=gen_a, legacy=False)
    plan_b = _plan(facts, rf)                                                 # A -> B
    gen_b = plan_b.generation
    assert gen_b != gen_a

    # (1) killed after the first journal (generation None): a bare repair must not fall back to A and commit it
    j0 = _crash_after(plan_b, 0, monkeypatch)
    assert j0 is not None and j0.generation is None and j0.txn == plan_b.intent.txn
    f0 = replace(facts, intent=j0)
    with pytest.raises(Refused) as e:
        _plan(f0, replace(rf, source=None, h11=None), argv=("repair",))
    assert e.value.name == "source-required:none"

    # (2) killed after the R3 journal (generation B): a retry with another package C is refused, B stays the target
    j3 = _crash_after(plan_b, _step(plan_b, "journal the generation"), monkeypatch)
    assert j3.generation == gen_b and j3.txn == plan_b.intent.txn
    f3 = replace(facts, intent=j3)
    lv_c, h11_c = _other_package(tmp_path / "c")
    with pytest.raises(Refused) as e:
        _plan(f3, replace(rf, source=lv_c, h11=h11_c))
    assert e.value.name == f"source-mismatch:{gen_b}"

    # (3) the same package resumes the same txn and the same target
    again = _plan(f3, rf)
    assert (again.generation, again.intent.txn) == (gen_b, plan_b.intent.txn)
    # ... and a journal that has no generation yet takes the supplied package's (only --source may establish it)
    est = _plan(f0, replace(rf, source=lv_c, h11=h11_c))
    assert est.intent.txn == plan_b.intent.txn and est.generation == G.generation_id(lv_c, h11_c)

    # (4) class check: a FINISHED journal (its txn is the record's completed_txn) never binds a fresh repair
    done = replace(facts, intent=j3, install_shape=_shape(gen_b, j3.txn))
    fresh = _plan(done, replace(rf, current=gen_b, source=lv_c, h11=h11_c))
    assert fresh.generation == G.generation_id(lv_c, h11_c) and fresh.intent.txn != j3.txn
    assert fresh.steps[0].why.startswith("remove the finished repair journal")


def _gen_tree(tmp: Path, src_lv: Path, src_h11: Path, gen: str, monkeypatch) -> Path:
    tree = tmp / "tree"
    (tree / gen).mkdir(parents=True)
    shutil.copytree(src_lv, tree / gen / "levain")
    shutil.copytree(src_h11, tree / gen / "h11")
    os.symlink(gen, tree / root_tree.CURRENT)
    monkeypatch.setitem(root_tree.IMPORT_TREE, HOST, tree)
    return tree


def test_bare_repair_reads_versions_from_the_target_tree(tmp_path: Path, monkeypatch) -> None:
    """U17 (complement MED 1): the record's versions are the target generation's own, read as text from its tree."""
    # a pre-record host (no install-shape) killed after the flip: journal names B, current is B
    facts, rf = _pre_change(tmp_path / "p")
    plan = _plan(facts, rf)
    gen_b = plan.generation
    j = _crash_after(plan, _step(plan, "flip "), monkeypatch)
    assert j.generation == gen_b
    _gen_tree(tmp_path / "p", rf.source, rf.h11, gen_b, monkeypatch)
    bare = replace(rf, current=gen_b, legacy=False, source=None, h11=None)
    facts_j, _ = _pre_change(tmp_path / "pj", intent=j)                     # the journaled groups present at their gids
    resumed = _plan(facts_j, bare, argv=("repair",))                        # 2a122d24: AssertionError (no shape)
    rec = _written_record(resumed, monkeypatch)
    assert (rec.generation, rec.levain, rec.h11) == (gen_b, "1.0.0", "0.16.0")
    assert rec.completed_txn == j.txn
    # an older shape (A, older versions) whose flip to B landed: the record carries B's versions, never A's
    facts_a, _ = _pre_change(tmp_path / "q", shape=_shape("gen-" + "a" * 16, levain="0.9.0", h11="0.16.0"),
                             intent=j)
    rec = _written_record(_plan(facts_a, bare, argv=("repair",)), monkeypatch)
    assert (rec.generation, rec.levain, rec.h11) == (gen_b, "1.0.0", "0.16.0")


class _Proc:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_stop_steps_verify_the_end_state(monkeypatch) -> None:
    """U18 (codex MED 5 = complement MED 2): a service with no unit is stopped already; the stop's exit status is never
    the verdict, the end state is."""
    calls: list[list[str]] = []
    # systemd's answers: a unit with no file reads ActiveState=inactive; `stop` exits 5 for it
    state = {"active": "inactive", "after": "inactive", "stop_rc": 5, "print_rc": 113}

    def run(argv, *a, **kw):
        argv = list(argv)
        calls.append(argv)
        if argv[1:4] == ["show", "-p", "ActiveState"]:
            return _Proc(0, state["active"])
        if argv[1] == "stop":
            state["active"] = state["after"]
            return _Proc(state["stop_rc"])
        if argv[1] == "print":
            return _Proc(state["print_rc"])
        return _Proc(0)

    monkeypatch.setattr(subprocess, "run", run)
    svc = units.services_for("linux")[0]
    noted: dict[str, int] = {}
    step, = IH._stop_steps("linux", [svc], noted, lambda: 1234.9)
    assert step.call() == (True, "")                                 # no unit: stop exits 5 on 2a122d24, aborting
    assert noted == {svc.name: 1234} and not [c for c in calls if c[1] == "stop"]
    # running (also: a unit whose file was deleted after a daemon-reload, LoadState not-found): stopped, and a
    # non-zero stop status is not the verdict; the state after it is
    state.update(active="active", after="failed", stop_rc=1)
    assert step.call() == (True, "") and [c for c in calls if c[1] == "stop"]
    state.update(active="active", after="active", stop_rc=0)
    ok, why = step.call()
    assert not ok and "active" in why                                # still running after the stop: the step fails
    # macOS: a job launchd does not know is stopped already; nothing is booted out
    calls.clear()
    dsvc = units.services_for("darwin")[0]
    dstep, = IH._stop_steps("darwin", [dsvc], noted, lambda: 7.0)
    assert dstep.call() == (True, "") and not [c for c in calls if c[1] == "bootout"]


def test_every_mutating_verb_checks_the_records(tmp_path: Path) -> None:
    """U15 (codex HIGH 4): undo and replenish refuse a well-formed record naming a kind this code cannot remove, or an
    out-of-bound record, before step one, as repair does."""
    undo = parse_install_argv(["undo"])
    replenish = parse_install_argv(["replenish"])
    for recs, name in (([("widget/3", G.Opaque('"x"'))], "downgrade-unsupported:widget/3"),
                       ([("group/1", ("admin", 80))], "bad-record")):
        f, _ = _pre_change(tmp_path / name.replace(":", "-").replace("/", ""), shape=_shape("gen-" + "0" * 16,
                                                                                            managed=recs))
        with pytest.raises(Refused) as e:
            IH.plan_undo(undo, f, accounts=SLOT_UIDS, shared_groups=OLD_GROUPS)
        assert e.value.name == name
        with pytest.raises(Refused) as e:
            IH.plan_replenish(replenish, f, slots=1, verifier_slots=1, live={"egress": (0,), "none": (0,)},
                              accounts=SLOT_UIDS)
        assert e.value.name == name
    # an unfinished repair journal naming a kind this code lacks: undo refuses it too
    j = Intent("repair", OP, txn=TXN_B, managed=(("widget/3", ("anything",)),))
    f, _ = _pre_change(tmp_path / "j", intent=parse_intent(intent_text(j).encode(), HOST))
    with pytest.raises(Refused) as e:
        IH.plan_undo(undo, f, accounts=SLOT_UIDS, shared_groups=OLD_GROUPS)
    assert e.value.name == "downgrade-unsupported:widget/3"


def test_replenish_extends_the_install_shape(tmp_path: Path, monkeypatch) -> None:
    """U16 (codex MED 7): what replenish made is in install-shape.managed before its journal goes, the rest kept."""
    shared = list(RANGES[HOST].shared)
    groups = {g: shared[i] for i, g in enumerate(SHARED_GROUPS)}
    old = _shape("gen-" + "c" * 16, TXN_A, managed=[("group/1", (g, i)) for g, i in groups.items()])
    old = replace(old, started_after={"keys": 5})
    f, _ = _pre_change(tmp_path, shape=old)
    plan = IH.plan_replenish(parse_install_argv(["replenish"]), f, slots=2, verifier_slots=1,
                             live={"egress": (0,), "none": (0,)}, accounts=SLOT_UIDS)
    whys = [s.why for s in plan.steps]
    ext = _first(whys, lambda w: w.startswith("extend the install record"))
    assert max(_all(whys, lambda w: "under the pool lock" in w)) < ext == len(whys) - 2
    assert whys[-1].startswith("remove the intent journal")
    rec = _written_record_at(plan, ext, monkeypatch)
    assert (rec.generation, rec.levain, rec.h11, rec.completed_txn, dict(rec.started_after)) == \
           (old.generation, old.levain, old.h11, TXN_A, {"keys": 5})
    made = set(rec.managed) - set(old.managed)
    assert set(old.managed) <= set(rec.managed)
    new = [s for s in plan.allocation.slots]
    assert {("account/1", (n, u)) for n, u in plan.allocation.ids.items()} <= made
    assert {("pf-alias/1", loopback_alias(s)) for s in new if s.slot_class == "egress"} <= made
    assert all(k in ("account/1", "member/1", "pf-alias/1") for k, _ in made)
    for k, i in made:
        if k == "member/1":
            assert groups[i[1]] == i[2]
    G.check_managed(HOST, rec.managed, operator=OP)
    # no shape (a pre-record host): nothing to extend; repair writes the record
    f2, _ = _pre_change(tmp_path / "n")
    p2 = IH.plan_replenish(parse_install_argv(["replenish"]), f2, slots=2, verifier_slots=1,
                           live={"egress": (0,), "none": (0,)}, accounts=SLOT_UIDS)
    assert not [s for s in p2.steps if s.why.startswith("extend the install record")]


def _written_record_at(plan, i: int, monkeypatch) -> G.InstallShape:
    got: list[str] = []
    with monkeypatch.context() as m:
        m.setattr(G, "write_record_durably", lambda p, t, **kw: (got.append(t), (True, ""))[1])
        assert plan.steps[i].call() == (True, "")
    return G.parse_shape(got[-1].encode())


def test_replenish_mints_its_own_txn_over_a_stale_setup_journal(tmp_path: Path) -> None:
    """R-i (complement LOW 5): a txn is reused only from a journal of the verb being planned."""
    stale = Intent("setup", OP, {}, {}, "c" * 32)
    f, _ = _pre_change(tmp_path, intent=stale)
    plan = IH.plan_replenish(parse_install_argv(["replenish"]), f, slots=1, verifier_slots=1,
                             live={"egress": (0,), "none": (0,)}, accounts=SLOT_UIDS)
    assert G.TXN_RE.fullmatch(plan.intent.txn) and plan.intent.txn != "c" * 32


def test_current_is_validated_at_gather(tmp_path: Path) -> None:
    """R-g (codex MED 6): ``current`` and ``current.new`` are read once, at gather, and must be relative links to a
    generation; anything else refuses ``bad-root`` before any act (it was read as absent)."""
    def tree(name: str) -> Path:
        t = tmp_path / name
        t.mkdir()
        return t

    t = tree("none")
    assert root_tree.read_current(t) is None
    t = tree("ok")
    os.symlink("gen-" + "1" * 16, t / "current")
    os.symlink("gen-" + "2" * 16, t / "current.new")                 # a stray from a flip killed mid-way: fine
    assert root_tree.read_current(t) == "gen-" + "1" * 16
    for name, make in (("file", lambda t: (t / "current").write_text("")),
                       ("dir", lambda t: (t / "current").mkdir()),
                       ("up", lambda t: os.symlink("../etc", t / "current")),
                       ("abs", lambda t: os.symlink(str(t / ("gen-" + "1" * 16)), t / "current")),
                       ("newfile", lambda t: (os.symlink("gen-" + "1" * 16, t / "current"),
                                              (t / "current.new").write_text("")))):
        t = tree(name)
        make(t)
        with pytest.raises(Refused) as e:
            root_tree.read_current(t)
        assert e.value.name == "bad-root", name


def test_journal_read_is_bounded(tmp_path: Path) -> None:
    """R-h (codex MED 9): a journal larger than the one named record cap refuses ``record-too-large`` unread."""
    p = tmp_path / "install-intent"
    p.write_bytes(b" " * (G.MAX_RECORD_BYTES + 1))
    with pytest.raises(Refused) as e:
        I.read_intent(HOST, path=p)
    assert e.value.name == "record-too-large"
    j = Intent("repair", OP, txn=TXN_B)
    p.write_text(intent_text(j))
    assert I.read_intent(HOST, path=p) == j


def test_generation_id_walk_errors_and_pycache_files(tmp_path: Path) -> None:
    """R-j (complement LOW 6, codex LOW 11): an unreadable subdir refuses ``bad-source``; ANY entry named
    ``__pycache__`` is dropped, the rule staging's ``find -name __pycache__`` applies."""
    lv, h11 = _package(tmp_path)
    base = G.generation_id(lv, h11)
    (lv / "slot" / "__pycache__").write_bytes(b"a plain file")
    assert G.generation_id(lv, h11) == base
    if os.geteuid() != 0:
        sub = lv / "sealed"
        sub.mkdir()
        (sub / "m.py").write_text("")
        os.chmod(sub, 0)
        try:
            with pytest.raises(Refused) as e:
                G.generation_id(lv, h11)
            assert e.value.name == "bad-source"
        finally:
            os.chmod(sub, 0o755)


def test_missing_or_unreadable_source_is_bad_source(tmp_path: Path) -> None:
    """R-k (codex LOW 10): a missing package, or a file that cannot be read, is ``bad-source``, never a raw OSError."""
    lv, h11 = _package(tmp_path)
    with pytest.raises(Refused) as e:
        G.generation_id(tmp_path / "missing" / "levain", h11)
    assert e.value.name == "bad-source"
    if os.geteuid() != 0:
        os.chmod(lv / "slot" / "__init__.py", 0)
        try:
            with pytest.raises(Refused) as e:
                G.generation_id(lv, h11)
            assert e.value.name == "bad-source"
        finally:
            os.chmod(lv / "slot" / "__init__.py", 0o644)
