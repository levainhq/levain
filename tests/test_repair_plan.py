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
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import root_tree
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS
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
    # ...while an unfinished one (txn differs) is resumed at its own txn
    f = facts_with(TXN_B)
    rplan = _plan(f, rf)
    assert rplan.intent.txn == TXN_B and rplan.steps[0].why.startswith("journal what this repair")
