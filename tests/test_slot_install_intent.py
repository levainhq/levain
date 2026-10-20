"""CONTRACT §25 (c7.7): the install lock and the intent journal. Plan assertions, fakes, and pytest tmp
files only."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain.firing import hands
from levain.slot import Refused, intent as intent_mod
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, SINGLETONS
from levain.slot.install_helper import HostFacts, operator_record, plan_replenish, plan_setup, plan_undo
from levain.slot.intent import (INSTALL_LOCK, INTENT_PATH, Intent, install_lock, intent_text, parse_intent,
                                read_intent, write_intent_atomically)
from levain.slot.layout import operator_record_path

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501


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


def _setup_cmd(n, m):
    return parse_install_argv(["setup", "--slots", str(n), "--verifier-slots", str(m), "--operator", OP])


def _plan(host, n=3, m=2, **kw):
    return plan_setup(_setup_cmd(n, m), _facts(host, **kw), read_sysctl=lambda p: "1", program_exists=lambda p: True)


def _refusal(fn, *a, **kw):
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value


def _mine(st):
    return st.st_uid == os.getuid()


# --- the lock ------------------------------------------------------------------------------------------


def test_lock_paths_sit_beside_the_retired_ids():
    assert INSTALL_LOCK == {"darwin": Path("/var/db/levain/install.lock"),
                            "linux": Path("/var/lib/levain/install.lock")}
    assert INTENT_PATH == {"darwin": Path("/var/db/levain/install-intent"),
                           "linux": Path("/var/lib/levain/install-intent")}


_HOLDER = """\
import os, sys
from pathlib import Path
from levain.slot.intent import install_lock
with install_lock(path=Path(sys.argv[1]), owner_ok=lambda st: st.st_uid == os.getuid()):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_install_lock_refuses_second_holder(tmp_path):
    lock = tmp_path / "state" / "install.lock"
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(levain.__file__).resolve().parent.parent)}
    child = subprocess.Popen([sys.executable, str(script), str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env, cwd=tmp_path)
    try:
        assert child.stdout.readline().strip() == "held", child.stderr.read()
        busy = _refusal(lambda: install_lock(path=lock, owner_ok=_mine).__enter__())
        assert busy.name == "install-busy"
    finally:
        child.stdin.close()
        assert child.wait(timeout=30) == 0
    # the holder gone, the lock is free again; the file is 0600 and the dir it made 0755
    with install_lock(path=lock, owner_ok=_mine):
        pass
    assert (lock.stat().st_mode & 0o777) == 0o600 and (lock.parent.stat().st_mode & 0o777) == 0o755


def test_install_lock_refuses_bad_root(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = state / "install.lock"
    link.symlink_to(target)
    assert _refusal(lambda: install_lock(path=link, owner_ok=_mine).__enter__()).name == "bad-root"
    link.unlink()
    link.mkdir()                                                      # not a regular file
    assert _refusal(lambda: install_lock(path=link, owner_ok=_mine).__enter__()).name == "bad-root"
    link.rmdir()
    # the lock file not root's (here: an owner check that accepts the dir and no file)
    import stat as _stat
    dir_only = lambda st: _stat.S_ISDIR(st.st_mode)                    # noqa: E731
    assert _refusal(lambda: install_lock(path=link, owner_ok=dir_only).__enter__()).name == "bad-root"
    link.unlink()
    # the state dir not root's
    assert _refusal(lambda: install_lock(path=link, owner_ok=lambda st: False).__enter__()).name == "bad-root"
    # the state dir itself a link
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    assert _refusal(lambda: install_lock(path=tmp_path / "linked" / "install.lock",
                                         owner_ok=_mine).__enter__()).name == "bad-root"
    # a lock file left at another mode is brought to 0600
    link.write_text("")
    link.chmod(0o644)
    with install_lock(path=link, owner_ok=_mine):
        assert (link.stat().st_mode & 0o777) == 0o600


# --- the journal record --------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_intent_record_round_trips_and_is_strict(host):
    plan = _plan(host)
    j = plan.intent
    assert j == Intent("setup", OP, dict(plan.allocation.ids), dict(plan.allocation.shared))
    assert parse_intent(intent_text(j).encode(), host) == j
    r = RANGES[host]
    good = {"format": "levain-intent/1", "verb": "replenish", "operator": OP,
            "accounts": {"_levain_s1": r.slots[0]}, "groups": {}}
    import json
    assert parse_intent(json.dumps(good).encode(), host).accounts == {"_levain_s1": r.slots[0]}
    for bad in ({**good, "verb": "undo"}, {**good, "format": "levain-intent/2"}, {**good, "extra": 1},
                {**good, "operator": "a b"}, {**good, "accounts": {"bob": r.slots[0]}},
                {**good, "accounts": {"_levain_s1": r.singletons[0]}},
                {**good, "accounts": {"_levain_s1": True}},
                {**good, "accounts": {"_levain_s1": r.slots[0], "_levain_gw1": r.slots[0]}},
                {**good, "groups": {"levain-slots": r.shared[0]}},                 # replenish makes no group
                {**good, "verb": "setup", "groups": {"levain-slots": r.slots[0]}},
                {**good, "verb": "setup", "groups": {"wheel": r.shared[0]}}):
        assert _refusal(parse_intent, json.dumps(bad).encode(), host).name == "intent-malformed", bad
    assert _refusal(parse_intent, b"not json", host).name == "intent-malformed"


def test_intent_written_atomically_0600(tmp_path):
    j = Intent("replenish", OP, {"_levain_s1": 899})
    path = tmp_path / "state" / "install-intent"
    (path.parent).mkdir()
    (path.parent / "install-intent.tmp").write_text("a dead run's temp file")
    assert write_intent_atomically(path, intent_text(j)) == (True, "")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert read_intent("linux", path=path) == j
    assert not (path.parent / "install-intent.tmp").exists()
    assert read_intent("linux", path=tmp_path / "absent") is None
    (tmp_path / "link").symlink_to(path)
    assert _refusal(read_intent, "linux", path=tmp_path / "link").name == "bad-root"


# --- step order ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_intent_written_first_removed_last(host, tmp_path, monkeypatch):
    monkeypatch.setitem(intent_mod.INTENT_PATH, host, tmp_path / "install-intent")
    plan = _plan(host)
    first, last = plan.steps[0], plan.steps[-1]
    assert first.call and not first.argv and not first.write and "journal" in first.why
    assert first.call() == (True, "")
    assert read_intent(host, path=tmp_path / "install-intent") == plan.intent
    assert last.argv[1:] == ("-f", str(tmp_path / "install-intent"))
    assert plan.steps[-2].write and plan.steps[-2].write[0] == operator_record_path(host)
    assert sum(1 for s in plan.steps if s.why.startswith("journal what")) == 1
    # replenish: written first, removed after its confinement steps
    sl = RANGES[host].slots
    rep = plan_replenish(parse_install_argv(["replenish"]), _set_up(host), slots=2, verifier_slots=1,
                         live={"egress": [0], "none": [0]},
                         accounts={"_levain_s0": sl[0], "_levain_gw0": sl[1], "_levain_v0": sl[2],
                                   "_levain_c0": sl[3]})
    assert rep.steps[0].call and "journal" in rep.steps[0].why
    assert rep.steps[-1].argv[1:] == ("-f", str(tmp_path / "install-intent"))
    assert rep.intent == Intent("replenish", OP, dict(rep.allocation.ids))
    confinement = [i for i, s in enumerate(rep.steps)
                   if s.argv and ("netns" in s.argv or "pfctl" in s.argv[0] or "ifconfig" in s.argv[0])
                   or "bwrap" in s.why]
    assert confinement and max(confinement) < len(rep.steps) - 1


# --- setup reads the journal ---------------------------------------------------------------------------


def _interrupted_setup(host):
    """A first setup that ran while someone else held the top slot ids, journaled, then died after the
    shared groups (Linux: no label) and the singletons were made."""
    r = RANGES[host]
    others = frozenset(list(r.slots)[:3])
    first = _plan(host, used_ids=others)
    j = first.intent
    made = {**{g: j.groups[g] for g in SHARED_GROUPS}, **{n: j.accounts[n] for n in SINGLETONS}}
    return first, j, made


@pytest.mark.parametrize("host", HOSTS)
def test_setup_resumes_intent_groups(host):
    first, j, made = _interrupted_setup(host)
    names, used = frozenset(made), frozenset(made.values())
    # without the journal the unlabelled groups (and singletons) are someone else's
    if host == "linux":
        assert _refusal(_plan, host, existing_names=names, used_ids=used).name == "account-exists"
    again = _plan(host, existing_names=names, used_ids=used, intent=j, host_ids=made)
    # the journal's allocation, not a fresh one (the top ids are free now, and still not taken)
    assert dict(again.allocation.ids) == dict(j.accounts) and dict(again.allocation.shared) == dict(j.groups)
    assert set(again.kept) == set(made) and again.intent == j
    # a journaled name present at another id is not ours
    moved = {**made, "levain-slots": made["levain-slots"] - 1}
    assert _refusal(_plan, host, existing_names=names, used_ids=used, intent=j, host_ids=moved).name == "account-exists"
    # a journaled name absent while its id is held by something else
    taken = used | {j.accounts["_levain_s0"]}
    assert _refusal(_plan, host, existing_names=names, used_ids=taken, intent=j, host_ids=made).name == "account-exists"
    # re-run as first asked, or refused by name
    with pytest.raises(Refused) as ei:
        plan_setup(_setup_cmd(4, 2), _facts(host, existing_names=names, used_ids=used, intent=j, host_ids=made),
                   read_sysctl=lambda p: "1", program_exists=lambda p: True)
    assert ei.value.name == "install-interrupted" and "--slots 3" in ei.value.detail
    # another verb's journal: that verb finishes it
    rj = Intent("replenish", OP, {"_levain_s5": RANGES[host].slots[0]})
    err = _refusal(_plan, host, intent=rj)
    assert err.name == "install-interrupted" and err.detail == "replenish"
    err = _refusal(plan_setup, _setup_cmd(3, 2), _set_up(host, intent=rj))
    assert err.name == "install-interrupted"


# --- replenish reads the journal -----------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_replenish_resumes_intent(host):
    r = RANGES[host]
    ids = list(r.slots)
    live_accts = {**{f"_levain_s{i}": ids[2 * i] for i in range(1, 8)},
                  **{f"_levain_gw{i}": ids[2 * i + 1] for i in range(1, 8)}}
    j = Intent("replenish", OP, {"_levain_s8": ids[40], "_levain_gw8": ids[41]})
    # the interrupted run made _levain_s8, not its front; s0 is a tombstone
    names = frozenset(live_accts) | {"_levain_s0", "_levain_gw0", "_levain_s8"}
    used = frozenset(live_accts.values()) | {ids[0], ids[1], ids[40]}
    kw = dict(slots=8, verifier_slots=0, live={"egress": range(1, 8), "none": ()}, accounts=live_accts)
    cmd = parse_install_argv(["replenish"])
    blind = plan_replenish(cmd, _set_up(host, existing_names=names, used_ids=used), **kw)
    assert [s.tag for s in blind.allocation.slots] == ["e9"]          # without the journal: index 8 spent
    rep = plan_replenish(cmd, _set_up(host, existing_names=names, used_ids=used, intent=j,
                                      host_ids={"_levain_s8": ids[40]}), **kw)
    assert [s.tag for s in rep.allocation.slots] == ["e8"]
    assert dict(rep.allocation.ids) == dict(j.accounts) and rep.intent == j
    # no new index while the journal stands, even with more configured; the next run fills
    more = plan_replenish(cmd, _set_up(host, existing_names=names, used_ids=used, intent=j,
                                       host_ids={"_levain_s8": ids[40]}), **{**kw, "slots": 10})
    assert [s.tag for s in more.allocation.slots] == ["e8"]
    # a caller that already counts the resumed slot as live plans it once
    again = plan_replenish(cmd, _set_up(host, existing_names=names, used_ids=used, intent=j,
                                        host_ids={"_levain_s8": ids[40]}),
                           **{**kw, "live": {"egress": range(1, 9), "none": ()}})
    assert [s.why for s in again.steps] == [s.why for s in rep.steps]
    # a journaled name present at another id
    assert _refusal(plan_replenish, cmd, _set_up(host, existing_names=names, used_ids=used, intent=j,
                                                 host_ids={"_levain_s8": ids[42]}), **kw).name == "account-exists"
    # a stale setup journal (setup died after its record) does not stop a replenish
    sj = Intent("setup", OP, {}, {})
    plan_replenish(cmd, _set_up(host, existing_names=names, used_ids=used, intent=sj), **kw)


# --- undo reads the journal ----------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_undo_reads_intent(host):
    first, j, made = _interrupted_setup(host)
    s0 = j.accounts["_levain_s0"]
    present = {**made, "_levain_s0": s0, "_levain_gw0": j.accounts["_levain_gw0"] - 1}   # gw0 is not ours
    # the directory service reports only the labelled accounts; Linux reports no shared group
    labelled = {n: made[n] for n in SINGLETONS}
    shared = () if host == "linux" else SHARED_GROUPS
    undo = plan_undo(parse_install_argv(["undo"]), _set_up(host, intent=j, host_ids=present),
                     accounts=labelled, shared_groups=shared)
    tombed = {s.why.split(":")[0].removeprefix("tombstone ") for s in undo.steps if s.why.startswith("tombstone ")}
    assert "_levain_s0" in tombed and "_levain_gw0" not in tombed and set(SINGLETONS) <= tombed
    assert s0 in undo.retired_after and j.accounts["_levain_gw0"] not in undo.retired_after
    deleted = [s.why.removeprefix("delete the shared group ") for s in undo.steps if "delete the shared group" in s.why]
    assert sorted(deleted) == sorted(SHARED_GROUPS)
    assert undo.steps[-2].argv[1:] == ("-f", str(INTENT_PATH[host]))
    assert undo.steps[-1].argv[1:] == ("-f", str(operator_record_path(host)))
    # without the journal, Linux would leave the unlabelled groups and the unlabelled-yet account behind
    blind = plan_undo(parse_install_argv(["undo"]), _set_up(host), accounts=labelled, shared_groups=shared)
    assert s0 not in blind.retired_after
    # a replenish journal is read the same way
    rj = Intent("replenish", OP, {"_levain_s0": s0, "_levain_gw0": j.accounts["_levain_gw0"]})
    undo2 = plan_undo(parse_install_argv(["undo"]), _set_up(host, intent=rj, host_ids={"_levain_s0": s0}),
                      accounts={}, shared_groups=())
    assert s0 in undo2.retired_after and any(s.why.startswith("tombstone _levain_s0") for s in undo2.steps)
