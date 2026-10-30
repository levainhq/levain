"""R7 wall 1 (project_memory/1030-24-r7/SLOT_HOME_DESIGN.md, option (a)): on macOS the accounts a vendor CLI runs as
(``_levain_s<n>``, ``_levain_c<n>``) get the root-owned, empty ``L/vendorhome`` as their directory-record home, because
Codex's managed-preferences sync fails with ``/var/empty`` and a ChatGPT login's ``account/read`` then fails -32603.
Every other account, and Linux, keep ``NO_HOME``. Setup makes the tree before any account, repair re-points existing
accounts, doctor reads it back. The VM run (design §8) is the end-to-end proof; these are the plan- and row-level ones.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from levain import doctor
from levain.firing import hands
from levain.slot import accounts as A
from levain.slot import layout as LY
from levain.slot import records as R
from levain.slot import render
from levain.slot.accounts import Account, account_steps, supplementary_groups, tombstone_steps, vendor_home_steps
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import SINGLETONS, SlotId
from levain.slot.install_helper import HostFacts, operator_record, plan_setup, plan_undo

HOME = "/private/var/levain/vendorhome"
OP, OP_UID = "alice", 501


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    """A plan for the other host names tools this test host lacks; every tool resolves here."""
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


def _homes(host: str, names) -> dict[str, str]:
    out = {}
    for n in names:
        for s in account_steps(host, [Account(n, 3900, supplementary_groups(n))]):
            if "NFSHomeDirectory" in s.argv:
                out[n] = s.argv[-1]
            elif "--home-dir" in s.argv:
                out[n] = s.argv[s.argv.index("--home-dir") + 1]
    return out


# --- 1, 2: the account spec ------------------------------------------------------------------------------------------


def test_vendor_accounts_get_the_vendor_home_on_macos_only():
    names = ["_levain_s0", "_levain_s12", "_levain_c0", "_levain_c3", "_levain_gw0", "_levain_v0", *SINGLETONS]
    vendor = {"_levain_s0", "_levain_s12", "_levain_c0", "_levain_c3"}
    assert _homes("darwin", names) == {n: HOME if n in vendor else "/var/empty" for n in names}
    assert _homes("linux", names) == {n: "/nonexistent" for n in names}
    assert str(LY.vendor_home("darwin")) == HOME and LY.vendor_home("linux") is None
    # the step text says which home it is
    whys = [s.why for s in account_steps("darwin", [Account("_levain_s0", 3999, ("levain-slots",)),
                                                    Account("_levain_gw0", 3998, ("levain-gwfronts",))])]
    assert "give _levain_s0 the root-owned vendor home" in whys and "give _levain_gw0 no home" in whys


def test_linux_account_spec_is_unchanged():
    # pinned argv: byte-identical to the spec before R7 (no Linux run was made, so nothing there moves)
    steps = account_steps("linux", [Account("_levain_s2", 897, ("levain-slots",))])
    useradd = next(s.argv for s in steps if s.argv and s.argv[0].endswith("useradd"))
    assert useradd[1:] == ("--system", "--uid", "897", "--gid", "897", "--no-create-home", "--home-dir",
                           "/nonexistent", "--shell", hands._nologin(), "--comment", A.SLOT_MARKER, "_levain_s2")
    assert not any("vendorhome" in " ".join(s.argv) for s in steps)


def test_a_tombstone_goes_back_to_var_empty():
    for name in ("_levain_s3", "_levain_c1"):
        homes = [s.argv[-1] for s in tombstone_steps("darwin", name) if "NFSHomeDirectory" in s.argv]
        assert homes == ["/var/empty"]


# --- 3: the rows, and setup's order -----------------------------------------------------------------------------------


def test_vendor_home_rows_are_root_0755_dirs_in_base_and_early_rows():
    want = [Path(HOME), Path(HOME) / "Library", Path(HOME) / "Library" / "Preferences"]
    for rows in (LY.base_store_rows("darwin", "op"), LY.policy_rows("darwin")):
        got = [r for r in rows if "vendorhome" in str(r.path)]
        assert [r.path for r in got] == want
        assert {(r.owner, r.group, r.mode, r.kind) for r in got} == {("root", "wheel", 0o755, "dir")}
    assert not [r for r in LY.base_store_rows("linux", "op") + LY.policy_rows("linux") if "vendorhome" in str(r.path)]


def _setup_plan(host: str):
    cmd = parse_install_argv(["setup", "--slots", "2", "--verifier-slots", "1", "--operator", OP])
    facts = HostFacts(host, caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                      xcode_ready=lambda: True)
    return plan_setup(cmd, facts, read_sysctl=lambda p: "1", program_exists=lambda p: True)


def test_setup_makes_the_vendor_home_before_any_account_names_it():
    steps = _setup_plan("darwin").steps
    whys = [s.why for s in steps]
    made = [i for i, w in enumerate(whys) if w.startswith("create ") and "vendorhome" in w]
    assert [whys[i] for i in made] == [f"create {HOME}", f"create {HOME}/Library", f"create {HOME}/Library/Preferences"]
    homes = [i for i, s in enumerate(steps) if "NFSHomeDirectory" in s.argv]
    assert homes and max(made) < min(homes)
    for p in (HOME, f"{HOME}/Library", f"{HOME}/Library/Preferences"):    # owned and moded before any account, too
        assert whys.index(f"set {p} to 0755") < min(homes) and whys.index(f"own {p} as root:wheel") < min(homes)
    pointed = sorted(s.argv[3].rsplit("/", 1)[1] for s in steps if "NFSHomeDirectory" in s.argv and s.argv[-1] == HOME)
    assert pointed == ["_levain_c0", "_levain_s0", "_levain_s1"]
    assert not [w for w in _setup_plan("linux").steps if "vendorhome" in w.why]


# --- 4: repair's re-point ----------------------------------------------------------------------------------------------


def test_repair_repoints_only_in_service_vendor_accounts_after_the_rows(tmp_path: Path):
    from tests.test_repair_plan import _plan, _pre_change

    facts, rf = _pre_change(tmp_path)                      # slots e0 + v0 in service
    plan = _plan(facts, rf)
    whys = [s.why for s in plan.steps]
    points = [s for s in plan.steps if "NFSHomeDirectory" in s.argv]
    assert [(s.argv[3], s.argv[-1]) for s in points] == [("/Users/_levain_s0", HOME), ("/Users/_levain_c0", HOME)]
    rows_done = whys.index(f"set {HOME}/Library/Preferences to 0755")
    first_point = whys.index(points[0].why)
    flush = whys.index("flush the directory cache, so the new homes are read")
    ends = [i for i, w in enumerate(whys) if w.endswith("cfprefsd, so it re-reads the home (none running is success)")]
    assert rows_done < first_point < flush < min(ends) and len(ends) == 2
    shape = next(i for i, w in enumerate(whys) if w.startswith("write the install record"))
    assert max(ends) < shape
    assert [plan.steps[i].argv[-1] for i in ends] == ["_levain_s0", "_levain_c0"]
    assert plan.steps[flush].argv == ("/usr/bin/dscacheutil", "-flushcache")
    # idempotent by construction: the same steps on a re-plan, no skip guard
    again = _plan(*_pre_change(tmp_path / "again"))
    assert [s.argv for s in again.steps if "NFSHomeDirectory" in s.argv] == [s.argv for s in points]
    assert all(not s.skip_if and not s.allow_fail for s in points)


def test_replenish_makes_the_vendor_home_before_its_new_accounts_name_it(tmp_path: Path):
    # L3 r1 (codex MED): a store whose L/vendorhome is gone got new s/c accounts pointing at a missing dir
    from tests.test_repair_plan import SLOT_UIDS, _pre_change
    from levain.slot.install_helper import plan_replenish

    facts, _ = _pre_change(tmp_path)
    plan = plan_replenish(parse_install_argv(["replenish"]), facts, slots=2, verifier_slots=1,
                          live={"egress": (0,), "none": (0,)}, accounts=SLOT_UIDS)
    whys = [s.why for s in plan.steps]
    homes = [i for i, s in enumerate(plan.steps) if "NFSHomeDirectory" in s.argv and s.argv[-1] == HOME]
    assert [plan.steps[i].argv[3] for i in homes] == ["/Users/_levain_s1"]
    for p in (HOME, f"{HOME}/Library", f"{HOME}/Library/Preferences"):
        for w in (f"create {p}", f"set {p} to 0755", f"own {p} as root:wheel"):
            assert whys.index(w) < min(homes)
    assert whys.index(f"create {HOME}") > 0 and whys[0].startswith("journal what this replenish")   # the journal stays FIRST


def test_vendor_home_steps_scope():
    slots = [SlotId("egress", 2), SlotId("none", 1)]
    names = [s.argv[3] for s in vendor_home_steps("darwin", slots) if "NFSHomeDirectory" in s.argv]
    assert names == ["/Users/_levain_s2", "/Users/_levain_c1"]       # never gw2, v1, a singleton
    assert vendor_home_steps("linux", slots) == [] and vendor_home_steps("darwin", []) == []


def _end_script(pkill: str) -> str:
    return A._END_CFPREFSD.replace("/usr/bin/pkill", pkill)


@pytest.mark.parametrize("code,ok", [(0, True), (1, True), (2, False), (3, False)])
def test_ending_cfprefsd_counts_no_match_as_success(tmp_path: Path, code: int, ok: bool):
    stub = tmp_path / "pkill"
    stub.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / 'args'}\nexit {code}\n")
    stub.chmod(0o755)
    rc = subprocess.run(["/bin/sh", "-c", _end_script(str(stub)), "sh", "_levain_s0"], capture_output=True).returncode
    assert (rc == 0) is ok
    assert (tmp_path / "args").read_text().split() == ["-u", "_levain_s0", "-x", "cfprefsd"]


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/pkill"), reason="macOS pkill")
def test_ending_cfprefsd_for_an_account_with_none_running_succeeds_on_this_mac():
    # the real program: `nobody` runs no cfprefsd, so pkill exits 1 (no match) and the step is a success
    [step] = [s for s in vendor_home_steps("darwin", [SlotId("egress", 0)]) if s.argv[0] == "/bin/sh"]
    argv = (*step.argv[:-1], "nobody")
    assert subprocess.run(list(argv), capture_output=True).returncode == 0


# --- 5: the doctor row --------------------------------------------------------------------------------------------------


def _store(tmp: Path, states: dict[str, str], *, tree: bool = True) -> Path:
    store = tmp / "store"
    (store / "slots").mkdir(parents=True)
    for tag, state in states.items():
        d = store / "slots" / tag
        d.mkdir()
        slot = SlotId("egress" if tag[0] == "e" else "none", int(tag[1:]))
        sess = "s-a1b2c3d4e5f60718" if state == "leased" else None
        rec = R.SlotState(slot, 1, 1, state, True, R.timestamp(1_800_000_000.0), sess)
        (d / "state").write_bytes(R.slot_state_bytes(rec))
    if tree:
        (store / "vendorhome" / "Library" / "Preferences").mkdir(parents=True)
        for p in (store / "vendorhome", store / "vendorhome" / "Library", store / "vendorhome" / "Library" / "Preferences"):
            p.chmod(0o755)
    return store


def _as_root(path: str) -> os.stat_result:
    st = list(os.lstat(path))
    st[4] = 0                                   # st_uid: the tree is root's on a host
    return os.stat_result(st)


class _Pw:
    def __init__(self, d: str):
        self.pw_dir = d


def _row(store: Path, homes: dict[str, str] | Exception, **kw):
    def getpwnam(n):
        if isinstance(homes, Exception):
            raise homes
        return _Pw(homes[n])
    kw.setdefault("lstat", _as_root)
    return doctor._check_slot_homes(host="darwin", store=store, getpwnam=getpwnam, **kw)


def test_doctor_slot_homes_row(tmp_path: Path):
    store = _store(tmp_path / "a", {"e0": "reusable", "e1": "leased", "e2": "retired", "v0": "scrubbed"})
    home = str(store / "vendorhome")
    right = {"_levain_s0": home, "_levain_s1": home, "_levain_c0": home}     # e2 retired: not asked
    [ok] = _row(store, right)
    assert ok.name == "agent slot homes" and ok.ok and not ok.warn and "3 vendor accounts" in ok.detail
    [old] = _row(store, {**right, "_levain_s1": "/var/empty"})
    assert not old.ok and "_levain_s1 home is /var/empty" in old.detail and "-32603" in old.detail
    assert old.hint and "levain upgrade --no-pip" in old.hint
    for exc in (KeyError("_levain_s0"), OSError(5, "io")):
        [unread] = _row(store, exc)
        assert unread.warn and "could not be read" in unread.detail
    (store / "vendorhome" / "Library" / "Preferences" / "com.openai.codex.plist").write_text("")
    [dirty] = _row(store, right)
    assert not dirty.ok and "is not empty" in dirty.detail and dirty.hint


def test_doctor_slot_homes_tree_must_be_roots_0755(tmp_path: Path):
    store = _store(tmp_path / "a", {"e0": "reusable"})
    right = {"_levain_s0": str(store / "vendorhome")}
    [mine] = _row(store, right, lstat=os.lstat)                 # owned by the test's uid, not root
    assert not mine.ok and "not a root-owned 0755 directory" in mine.detail
    (store / "vendorhome" / "Library").chmod(0o775)
    [wide] = _row(store, right)
    assert not wide.ok and f"{store / 'vendorhome' / 'Library'} is not a root-owned 0755" in wide.detail
    gone = _store(tmp_path / "b", {"e0": "reusable"}, tree=False)
    [missing] = _row(gone, {"_levain_s0": str(gone / "vendorhome")})
    assert not missing.ok and "is missing" in missing.detail and "levain upgrade --no-pip" in missing.hint


def test_doctor_slot_homes_confirmed_mismatch_fails_unread_warns(tmp_path: Path):
    # L3 r1 (codex MED): a confirmed mismatch is a failing row, so `levain doctor` does not exit 0 on a host whose Codex
    # sessions fail -32603. What an install from before R7 has (accounts at /var/empty, no tree) is upgrade-pending; a
    # tree or home that is wrong otherwise is broken; a fact that cannot be read stays a warning.
    store = _store(tmp_path / "a", {"e0": "reusable"})
    home = str(store / "vendorhome")
    [old] = _row(store, {"_levain_s0": "/var/empty"})
    assert (old.ok, old.upgrade_pending) == (False, True) and "levain upgrade --no-pip" in old.hint
    gone = _store(tmp_path / "b", {"e0": "reusable"}, tree=False)
    [missing] = _row(gone, {"_levain_s0": "/var/empty"})
    assert (missing.ok, missing.upgrade_pending) == (False, True) and "is missing" in missing.detail
    [elsewhere] = _row(store, {"_levain_s0": "/private/tmp/r7roothome"})
    assert (elsewhere.ok, elsewhere.upgrade_pending) == (False, False) and "-32603" in elsewhere.detail
    (store / "vendorhome" / "Library").chmod(0o775)
    [wide] = _row(store, {"_levain_s0": home})
    assert (wide.ok, wide.upgrade_pending) == (False, False) and wide.hint
    (store / "vendorhome" / "Library").chmod(0o755)
    (store / "vendorhome" / "Library" / "Preferences" / "com.openai.codex.plist").write_text("")
    [dirty] = _row(store, {"_levain_s0": home})
    assert (dirty.ok, dirty.upgrade_pending) == (False, False) and "does not empty" in dirty.hint
    [unread] = _row(_store(tmp_path / "c", {"e0": "reusable"}), KeyError("_levain_s0"))
    assert unread.ok and unread.warn
    # a confirmed mismatch beside an unread fact still fails, and says both
    def one_unread(n):
        raise OSError(5, "io")
    [both] = doctor._check_slot_homes(host="darwin", store=gone, getpwnam=one_unread, lstat=_as_root)
    assert not both.ok and "is missing" in both.detail and "could not be read" in both.detail


def test_doctor_slot_homes_a_bad_entry_keeps_its_finding_and_the_account_checks(tmp_path: Path):
    # L3 r1 (all three seats): once an entry is wrong the walk stops, and listdir's own error is a finding, so the
    # precise diagnosis, the fix hint and the per-account checks are never lost to a generic "could not be read"
    store = _store(tmp_path / "a", {"e0": "reusable"}, tree=False)
    (store / "vendorhome").mkdir()
    (store / "vendorhome" / "Library").write_text("")                    # a file where a dir belongs
    [r] = _row(store, {"_levain_s0": "/var/empty"})
    assert not r.ok and f"{store / 'vendorhome' / 'Library'} is not a root-owned 0755 directory" in r.detail
    assert "_levain_s0 home is /var/empty" in r.detail and r.hint
    good = _store(tmp_path / "b", {"e0": "reusable"})

    def no_read(p):
        raise PermissionError(13, "Permission denied")
    [r2] = _row(good, {"_levain_s0": "/var/empty"}, listdir=no_read)
    assert not r2.ok and "could not be read" in r2.detail and "_levain_s0 home is /var/empty" in r2.detail
    [r3] = _row(good, {"_levain_s0": str(good / "vendorhome")}, listdir=no_read)
    assert r3.ok and r3.warn and "Preferences could not be read" in r3.detail


def test_doctor_slot_homes_no_row_on_linux_or_without_an_install(tmp_path: Path):
    store = _store(tmp_path / "a", {"e0": "reusable"})
    assert doctor._check_slot_homes(host="linux", store=store) == []
    assert doctor._check_slot_homes(host="darwin", store=tmp_path / "none") == []


# --- 6: render: the home is no slot-writable path, and sessions' env does not change ------------------------------------


def test_vendor_home_is_outside_every_slot_writable_path():
    home, live = LY.vendor_home("darwin"), LY.STORE["darwin"] / "live"
    assert live not in home.parents and home != live
    assert not any(w in home.parts for w in render.SLOT_WRITABLE)


# --- 7: undo keeps the store and tombstones to /var/empty ------------------------------------------------------------


def test_undo_leaves_the_vendor_home_and_tombstones_to_var_empty():
    from levain.slot.ids import RANGES, SHARED_GROUPS

    accounts = {"_levain_s0": 3999, "_levain_gw0": 3998, **{n: 3799 - i for i, n in enumerate(SINGLETONS)}}
    facts = HostFacts("darwin", caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                      xcode_ready=lambda: True, operator_record=operator_record(OP, OP_UID))
    plan = plan_undo(parse_install_argv(["undo"]), facts, accounts=accounts,
                     shared_groups={g: RANGES["darwin"].shared[i] for i, g in enumerate(SHARED_GROUPS)})
    whys = [s.why for s in plan.steps]
    assert "tombstone _levain_s0: no home" in whys
    assert not [w for w in whys if "vendorhome" in w or "vendor home" in w]
