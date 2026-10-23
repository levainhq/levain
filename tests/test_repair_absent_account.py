"""Row 33 R2 entry criterion: a repair plan never uses a principal it removes earlier in the same plan.

The Linux VM run of 1022+20 (``project_memory/1022-20/linuxvm/r1b3_raw.txt``): ``userdel _levain_s7`` on a set-up host
(the slot still in service), then ``repair``. The plan retired and tombstoned ``_levain_s7`` (the R7 ``account/1``
removal: the account was recorded and not live, so not desired) and then failed on ``chown _levain_gw7:_levain_s7``
(R8's slot rows: the slot was still in service, so it was still rendered). §2.4: repair never creates or removes
slots, so it can neither re-create that account nor drop the slot; it refuses ``account-missing`` before any act.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import units
from levain.slot.accounts import Account, membership_pairs, supplementary_groups
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, SINGLETONS, SlotId

OP, OP_UID = "phill", 501
TXN_A = "a" * 32


@pytest.fixture(autouse=True)
def _linux_tools(monkeypatch) -> None:
    from levain.firing import hands

    real = hands.shutil.which                   # a Linux plan built here names tools this host lacks
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


def _package(tmp: Path) -> tuple[Path, Path]:
    lv = tmp / "src" / "levain"
    for rel in ("__init__.py", "slot/__init__.py"):
        (lv / rel).parent.mkdir(parents=True, exist_ok=True)
        (lv / rel).write_text("__version__ = \"1.0.0\"\n")
    h11 = tmp / "src" / "h11"
    h11.mkdir(parents=True, exist_ok=True)
    (h11 / "__init__.py").write_text("")
    (h11 / "_version.py").write_text('__version__ = "0.16.0"\n')
    return lv, h11


def _host(tmp: Path, host: str, *, egress=(0, 1), verifier=(0,), gone=(), out_of_service=()):
    """A host set up at this code (every shared group, the record holding setup's whole desired set) with slots
    ``egress`` + ``verifier`` set up. ``gone``: accounts deleted by hand (absent from the directory: no name, no uid,
    and on Linux userdel took the same-named group). ``out_of_service``: egress slots retired at runtime (their
    accounts tombstoned, so not live; their records stay in the shape)."""
    r = RANGES[host]
    ids = {n: u for n, u in zip(SINGLETONS, r.singletons)}
    slots = [SlotId("egress", i) for i in egress] + [SlotId("none", i) for i in verifier]
    ids.update((n, u) for n, u in zip([n for s in slots for n in s.accounts], r.slots))
    groups = {g: gid for g, gid in zip(SHARED_GROUPS, r.shared)}
    services = [s.name for s in units.services_for(host)]
    managed = IH.desired_managed(host, accounts=ids, shared=groups, operator=OP, services=services, slots=slots)
    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", TXN_A, {}, tuple(managed))
    dead = set(gone) | {n for i in out_of_service for n in SlotId("egress", i).accounts}
    live = {n: u for n, u in ids.items() if n not in dead}
    members: dict[str, set[str]] = {g: set() for g in groups}
    for a, g in membership_pairs([Account(n, u, supplementary_groups(n)) for n, u in live.items()], OP):
        members[g].add(a)
    present = {n: u for n, u in ids.items() if n not in set(gone)}       # a tombstone keeps its name and uid
    facts = IH.HostFacts(host=host, caller_uid=OP_UID, caller_name=OP,
                         operator_record={"format": "levain-operator/1", "name": OP, "uid": OP_UID},
                         used_ids=frozenset({*present.values(), *groups.values(), OP_UID, 0}),
                         existing_names=frozenset({*present, *groups, OP, "root"}),
                         levain_live={**live, **groups} if host == "darwin" else dict(live),
                         install_shape=shape, user_ids={**present, OP: OP_UID, "root": 0},
                         tool_present=lambda p: True, xcode_ready=lambda: True)
    lv, h11 = _package(tmp)
    in_service = [i for i in egress if i not in out_of_service]
    rf = IH.RepairFacts(accounts=live, groups=groups, members={g: frozenset(m) for g, m in members.items()},
                        egress=tuple(in_service), verifier=tuple(verifier), current=shape.generation,
                        source=lv, h11=h11)
    return facts, rf


def _plan(facts, rf):
    return IH.plan_repair(parse_install_argv(["repair", "--source", "/opt/x/levain"]), facts, rf,
                          program_exists=lambda p: True, read_sysctl=lambda k: "0")


def _names(step) -> str:
    """Everything a step names a principal by, as text: its argv, its probe, and a written file's text."""
    parts = [*step.argv, *step.skip_if]
    if step.write is not None:
        parts.append(step.write[1])
    return " ".join(parts)


def _used_after_removal(plan, name: str) -> list[str]:
    """The steps AFTER ``name``'s account removal (its uid retired) that still name ``name`` as a principal."""
    whys = [s.why for s in plan.steps]
    at = [i for i, w in enumerate(whys) if w.startswith(f"retire {name}'s uid")]
    if not at:
        return []
    word = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
    return [s.why for s in plan.steps[at[0] + 1:] if word.search(_names(s)) and not s.why.startswith("tombstone ")]


@pytest.mark.parametrize("host", ["linux", "darwin"])
@pytest.mark.parametrize("gone", [("_levain_s1",), ("_levain_gw1",), ("_levain_keys",), ("_levain_c0",)])
def test_repair_refuses_when_an_account_the_plan_needs_is_gone(tmp_path: Path, host, gone) -> None:
    """The VM row, from the plan layer: the slot's account was deleted by hand while its slot is in service. RED at
    801ea8d9: the plan removes the account at R7 and then renders the slot's rows (and confinement) for it."""
    facts, rf = _host(tmp_path, host, gone=gone)
    with pytest.raises(Refused) as e:
        _plan(facts, rf)
    assert e.value.name == "account-missing", e.value
    assert gone[0] in e.value.detail and "undo" in e.value.detail


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_a_retired_slot_is_removed_and_never_used_after(tmp_path: Path, host) -> None:
    """The positive control: a slot retired at runtime (out of service, accounts tombstoned, still recorded) is not
    refused. Its accounts go through R7's removal and no later step names either of them."""
    facts, rf = _host(tmp_path, host, out_of_service=(1,))
    plan = _plan(facts, rf)
    whys = [s.why for s in plan.steps]
    for name in SlotId("egress", 1).accounts:
        assert any(w.startswith(f"retire {name}'s uid") for w in whys), name
        assert _used_after_removal(plan, name) == [], name


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_an_intact_host_removes_no_account(tmp_path: Path, host) -> None:
    facts, rf = _host(tmp_path, host)
    plan = _plan(facts, rf)
    assert not [s.why for s in plan.steps if s.why.startswith("retire ") and "'s uid" in s.why]
