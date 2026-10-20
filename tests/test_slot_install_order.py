"""The install helper's plans, CONTRACT c7.5 §22 + §23: boot-time run dirs, the tool preflight, re-runs of
an interrupted setup or undo, undo's id and group hygiene, the --root record, one filesystem, and the
root tree's checks. Plan and text assertions; the only files touched are pytest tmp files."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from levain.firing import hands
from levain.slot import Refused, bwrap, root_tree, units
from levain.slot.grammar import MAX_ROOTS, check_roots, parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, SINGLETONS, SlotId
from levain.slot.install_helper import (HostFacts, check_tools, operator_record, plan_replenish, plan_setup,
                                        plan_tools, plan_undo)
from levain.slot.layout import (RESERVE_BYTES, RUN, STORE, TMPFILES_CONF, base_run_rows, roots_path,
                                same_filesystem, single_filesystem_paths, slot_run_rows)
from levain.slot.sudoers import INSTALL_SUDOERS, RUNTIME_SUDOERS

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    """A plan for the OTHER host names tools this test host lacks; every tool resolves here."""
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


def _setup(n=8, m=2, *roots):
    argv = ["setup", "--slots", str(n), "--verifier-slots", str(m), "--operator", OP]
    for r in roots:
        argv += ["--root", r]
    return parse_install_argv(argv)


def _facts(host, **kw):
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _set_up(host, **kw):
    return _facts(host, operator_record=operator_record(OP, OP_UID), **kw)


def _plan(host, n=8, m=2, **kw):
    return plan_setup(_setup(n, m), _facts(host, **kw), read_sysctl=lambda p: "1", program_exists=lambda p: True)


def _undo(host, accounts, shared=SHARED_GROUPS, **kw):
    return plan_undo(parse_install_argv(["undo"]), _set_up(host, **kw), accounts=accounts, shared_groups=shared)


def _refusal(fn, *a, **kw) -> str:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value.name


# --- §22 Linux: R/ at boot --------------------------------------------------------------------------------


def test_tmpfiles_conf_matches_run_dirs():
    plan = _plan("linux", 2, 1)
    w = [s for s in plan.steps if s.write and s.write[0] == TMPFILES_CONF]
    assert len(w) == 1 and w[0].write[2] == 0o644
    lines = [ln for ln in w[0].write[1].splitlines() if not ln.startswith("#")]
    rows = base_run_rows("linux") + [r for s in plan.allocation.slots for r in slot_run_rows("linux", s)]
    assert lines == [f"d {r.path} {r.mode:04o} {r.owner} {r.group} -" for r in rows]
    assert {r.path for r in rows} == {r.path for r in plan.rows if str(r.path).startswith(str(RUN["linux"]))}
    assert "d /run/levain/gw/slot1 2750 _levain_gw1 _levain_s1 -" in lines            # setgid kept
    paths = [ln.split()[1] for ln in lines]
    assert all(paths.index(str(Path(p).parent)) < paths.index(p) for p in paths[1:])   # parents first
    i = plan.steps.index(w[0])
    assert plan.steps[i + 1].argv[1:] == ("--create", "levain.conf")
    assert plan.steps[i + 1].argv[0].endswith("systemd-tmpfiles")
    # after every account it names exists
    last_acct = max(j for j, s in enumerate(plan.steps) if s.argv and s.argv[0].endswith("useradd"))
    assert last_acct < i
    # replenish re-writes it for every slot in service plus the new ones; undo removes it
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up("linux", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)})
    text = next(s.write[1] for s in rep.steps if s.write and s.write[0] == TMPFILES_CONF)
    assert "d /run/levain/gw/slot2 2750 _levain_gw2 _levain_s2 -" in text
    assert "d /run/levain/gw/slot0 2750 _levain_gw0 _levain_s0 -" in text
    undo = _undo("linux", ids)
    assert [s for s in undo.steps if s.argv[-2:] == ("-f", str(TMPFILES_CONF))]
    assert not [s for s in _plan("darwin").steps if s.write and s.write[0] == TMPFILES_CONF]


# --- §23 preflight -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_preflight_refuses_missing_tool(host, monkeypatch):
    plan = _plan(host)
    tools = plan_tools(plan.steps)
    assert all(t.startswith("/") for t in tools)
    for t in tools:      # each tool, missing alone, refuses the whole plan by name
        facts = _facts(host, tool_present=lambda p, gone=t: p != gone)
        with pytest.raises(Refused) as ei:
            plan_setup(_setup(), facts, read_sysctl=lambda p: "1", program_exists=lambda p: True)
        assert ei.value.name == "tool-missing" and t in ei.value.detail
    # a tool _abs could not resolve (a bare name) is refused, present or not
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: None)
    if host == "linux":
        with pytest.raises(Refused) as ei:
            _plan(host)
        assert ei.value.name == "tool-missing" and "groupadd" in ei.value.detail
    # macOS: /usr/bin/python3 needs the developer tools
    if host == "darwin":
        assert _refusal(plan_setup, _setup(), _facts(host, xcode_ready=lambda: False),
                        program_exists=lambda p: True) == "tool-missing"
    # undo and replenish check theirs too
    assert _refusal(check_tools, _facts(host, tool_present=lambda p: False), plan.steps) == "tool-missing"


# --- §23 re-runs --------------------------------------------------------------------------------------------


_NOT_IDEMPOTENT = ("groupadd", "useradd", "dd", "netns", "bootstrap")


@pytest.mark.parametrize("host", HOSTS)
def test_partial_setup_reruns(host):
    """A setup that failed after making accounts re-runs: what it made is kept at its ids, nothing is
    refused as account-exists, and every step that is not idempotent carries a skip_if."""
    first = _plan(host, 3, 2)
    ids = dict(first.allocation.ids)
    live = {**ids, **first.allocation.shared}
    names = frozenset(live)
    again = _plan(host, 3, 2, existing_names=names, used_ids=frozenset(live.values()), levain_live=live)
    assert dict(again.allocation.ids) == ids and dict(again.allocation.shared) == dict(first.allocation.shared)
    assert set(again.kept) == set(live) and again.revived == ()
    assert [s.tag for s in again.allocation.slots] == [s.tag for s in first.allocation.slots]
    # the same steps, so the re-run converges on the same install
    assert [s.why for s in again.steps] == [s.why for s in first.steps]
    for s in again.steps:
        if s.argv and (Path(s.argv[0]).name in _NOT_IDEMPOTENT or "netns" in s.argv[1:3] and "add" in s.argv
                       or "bootstrap" in s.argv):
            assert s.skip_if, s.why
        if s.argv and Path(s.argv[0]).name == "mkdir" and "-p" not in s.argv:
            assert s.skip_if, s.why
        if s.argv and Path(s.argv[0]).name == "mv":
            assert s.skip_if or s.argv[-2].endswith("levain.new"), s.why
        if s.write and s.write[1] == "":            # an empty file row (a lock): never replaced under a holder
            assert s.skip_if[-2:] == ("-e", str(s.write[0])), s.why
    # what an interrupted run made at an out-of-range id is not ours to keep
    bad = {**live, "_levain_control": 0}
    assert _refusal(_plan, host, 3, 2, existing_names=names, levain_live=bad) == "account-exists"
    # it made more slots than this run asks for: refused, never silently orphaned
    assert _refusal(_plan, host, 2, 2, existing_names=names, levain_live=live) == "account-exists"
    # a complete install (the record written last) is already-set-up
    assert _refusal(plan_setup, _setup(3, 2), _set_up(host)) == "already-set-up"


def test_reserve_and_lock_files_skip_when_present(tmp_path):
    plan = _plan("darwin")
    dd = next(s for s in plan.steps if s.argv and s.argv[0].endswith("/dd"))
    assert dd.skip_if[:2] == ("/bin/sh", "-c")
    # the skip_if, run: skipped exactly when the file exists at RESERVE_BYTES
    f = tmp_path / "reserve"
    probe = (*dd.skip_if[:-1], str(f))
    if Path("/usr/bin/stat").exists() and os.uname().sysname == "Darwin":
        assert subprocess.run(probe).returncode != 0                     # absent
        f.write_bytes(b"\0" * 10)
        assert subprocess.run(probe).returncode != 0                     # wrong size
        with open(f, "wb") as fh:
            fh.truncate(RESERVE_BYTES)
        assert subprocess.run(probe).returncode == 0                     # present at size: skipped
    locks = [s for s in plan.steps if s.write and s.write[0].name in ("lock", "pool.lock")]
    assert locks and all(s.skip_if == ("/bin/test", "-e", str(s.write[0])) for s in locks)


@pytest.mark.parametrize("host", HOSTS)
def test_undo_reruns_after_failure(host):
    """The install rule is undo's last removal before the record: a failed undo re-runs through it,
    and the re-run (accounts already tombstoned, groups gone) plans and skips what is done."""
    plan = _plan(host, 2, 1)
    ids = dict(plan.allocation.ids)
    undo = _undo(host, ids)
    assert undo.steps[0].argv[-1] == str(RUNTIME_SUDOERS)
    assert undo.steps[-2].argv[-1] == str(INSTALL_SUDOERS)
    assert [i for i, s in enumerate(undo.steps) if s.argv and str(INSTALL_SUDOERS) in s.argv] == [len(undo.steps) - 2]
    for s in undo.steps:
        if s.argv and Path(s.argv[0]).name in ("groupdel", "apparmor_parser") or "-delete" in s.argv:
            assert s.skip_if, s.why
        if s.argv and "netns" in s.argv and "delete" in s.argv:
            assert s.skip_if, s.why
    # the re-run after a failure past the tombstones: no live accounts left, half the groups gone
    again = _undo(host, {}, shared=SHARED_GROUPS[:3], retired=undo.retired_after)
    assert again.steps[0].argv[-1] == str(RUNTIME_SUDOERS) and again.steps[-2].argv[-1] == str(INSTALL_SUDOERS)
    assert again.retired_after == undo.retired_after


@pytest.mark.parametrize("host", HOSTS)
def test_undo_refuses_out_of_range_uid(host):
    r = RANGES[host]
    good = {"_levain_s0": r.slots[0], "_levain_control": r.singletons[0]}
    _undo(host, good)                                                    # positive control
    for name, uid in (("_levain_s0", 0), ("_levain_control", 0), ("_levain_s0", r.singletons[0]),
                      ("_levain_control", r.slots[0]), ("_levain_gw3", r.shared[0]), ("_levain_s0", -1),
                      ("_levain_s0", True), ("_levain_s0", "3999"), ("_levain_c1", r.slots[-1] - 1)):
        assert _refusal(_undo, host, {**good, name: uid}) == "bad-token", (name, uid)
    assert _refusal(plan_replenish, parse_install_argv(["replenish"]), _set_up(host), slots=1, verifier_slots=1,
                    live={"egress": [0], "none": [0]}, accounts={"_levain_s0": 0}) == "bad-token"


@pytest.mark.parametrize("host", HOSTS)
def test_undo_leaves_no_shared_gid_on_store(host):
    """Before a shared group is deleted, every store (and run) dir whose row names one is chgrp'd to
    the host's gid-0 group, so no deleted gid is left on a dir for a later group to inherit."""
    plan = _plan(host, 1, 1)
    named = {r.path for r in plan.rows if r.group in SHARED_GROUPS}
    L = STORE[host]
    assert {L / "export/operator", L / "export/dispatch", L / "queue/requests", L / "queue/answers",
            L / "lineage"} <= named
    undo = _undo(host, dict(plan.allocation.ids))
    root_grp = "wheel" if host == "darwin" else "root"
    chgrp = {Path(s.argv[-1]): i for i, s in enumerate(undo.steps) if s.argv and s.argv[0].endswith("chgrp")}
    assert set(chgrp) == named
    # the whole subtree, never following a link (F4): BSD and GNU chgrp both take -R -h -P
    assert all(undo.steps[i].argv[1:5] == ("-R", "-h", "-P", root_grp) for i in chgrp.values())
    last_kill = max(i for i, s in enumerate(undo.steps) if "stop every process" in s.why)
    assert last_kill < min(chgrp.values())
    assert all(undo.steps[i].skip_if[-3:] == ("!", "-e", str(p)) for p, i in chgrp.items())
    first_delete = next(i for i, s in enumerate(undo.steps) if "delete the shared group" in s.why)
    assert max(chgrp.values()) < first_delete


# --- §23 / §2.1 the --root record, and one filesystem ------------------------------------------------------


def _dirstat(uid):
    return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, uid, 0, 0, 0, 0, 0))


def test_roots_recorded_and_patterned():
    roots = ("/Users/alice/a", "/Users/alice/b")
    plan = plan_setup(_setup(1, 1, *roots), _facts("darwin"), realpath=lambda p: p, lstat=lambda p: _dirstat(OP_UID))
    w = [s for s in plan.steps if s.write and s.write[0] == roots_path("darwin")]
    assert len(w) == 1 and w[0].write[1:] == ('{"format":"levain-roots/1","roots":["/Users/alice/a","/Users/alice/b"]}',
                                             0o644)
    assert roots_path("darwin") == STORE["darwin"] / "policy" / "roots"
    # the exact pattern: no control character, no quote
    for bad in ("/a\nb", "/a\tb", '/a"b', "/a'b", "/a\x7fb", "relative", "/a/../b"):
        assert _refusal(_setup, 1, 1, bad) == "bad-root", repr(bad)
    _setup(1, 1, "/a b/c-d_e.f")                                         # positive control: spaces are fine
    many = [f"/r{i}" for i in range(MAX_ROOTS)]
    _setup(1, 1, *many)
    assert _refusal(_setup, 1, 1, *many, "/one-more") == "out-of-range"
    # a resolved path is held to the same pattern
    assert _refusal(check_roots, ["/x"], OP_UID, realpath=lambda p: "/x\ny", lstat=lambda p: _dirstat(OP_UID)) == "bad-root"


def test_check_roots_oserror_is_root_missing():
    def raises(exc):
        def lstat(p):
            raise exc
        return lstat

    for exc in (FileNotFoundError(2, "x"), NotADirectoryError(20, "x"), PermissionError(13, "x")):
        assert _refusal(check_roots, ["/a/b"], OP_UID, realpath=lambda p: p, lstat=raises(exc)) == "root-missing"


def test_store_single_filesystem():
    plan = _plan("linux", 2, 1)
    chk = [s for s in plan.steps if s.call and "one filesystem" in s.why]
    assert len(chk) == 1
    paths = single_filesystem_paths("linux", plan.allocation.slots)
    L = STORE["linux"]
    assert {L / d for d in ("live", "alloc", "retained", "trash", "staging", "export")} <= set(paths)
    assert L / "slots/e0/keep" in paths and L / "slots/e1/keep" in paths and L / "slots/v0/keep" not in paths
    # after every dir it stats exists
    last_mkdir = max(i for i, s in enumerate(plan.steps) if s.argv and s.argv[0].endswith("mkdir")
                     and str(s.argv[-1]).startswith(str(L)))
    assert last_mkdir < plan.steps.index(chk[0])

    def st(dev):
        return os.stat_result((stat.S_IFDIR | 0o700, 0, dev, 0, 0, 0, 0, 0, 0, 0))

    assert same_filesystem(paths, stat=lambda p: st(7)) == (True, "")
    ok, why = same_filesystem(paths, stat=lambda p: st(8 if p.endswith("keep") else 7))
    assert not ok and "spans filesystems" in why
    assert same_filesystem(paths, stat=lambda p: (_ for _ in ()).throw(FileNotFoundError(2, "absent")))[0] is False


# --- §23 the root tree's checks -------------------------------------------------------------------------------


def _fake_tree(entries):
    """``entries``: path -> stat_result; walk yields them in order."""
    return dict(lstat=lambda p: entries[p], walk=lambda t: iter(entries))


def _st(kind, mode=0o644, uid=0):
    return os.stat_result((kind | mode, 0, 0, 0, uid, 0, 0, 0, 0, 0))


def test_root_tree_checks_fail_closed():
    plain = {"/t/levain": _st(stat.S_IFDIR, 0o755), "/t/levain/a.py": _st(stat.S_IFREG)}
    assert root_tree._no_links(Path("/t"), **_fake_tree(plain)) == (True, "")
    assert root_tree._no_foreign_entries(Path("/t"), **_fake_tree(plain)) == (True, "")
    link = {**plain, "/t/levain/x": _st(stat.S_IFLNK, 0o777)}
    assert root_tree._no_links(Path("/t"), **_fake_tree(link))[0] is False
    assert root_tree._no_foreign_entries(Path("/t"), **_fake_tree(link))[0] is False
    fifo = {**plain, "/t/levain/f": _st(stat.S_IFIFO)}
    assert root_tree._no_links(Path("/t"), **_fake_tree(fifo))[0] is False
    for bad in (_st(stat.S_IFREG, uid=501), _st(stat.S_IFREG, 0o664), _st(stat.S_IFREG, 0o646)):
        assert root_tree._no_foreign_entries(Path("/t"), **_fake_tree({**plain, "/t/levain/b.py": bad}))[0] is False
    # ancestors: every existing one root's alone; a link is judged by where it resolves (F12)
    anc = {"/usr": _st(stat.S_IFDIR, 0o755), "/usr/local": _st(stat.S_IFDIR, 0o755),
           "/usr/local/lib": _st(stat.S_IFDIR, 0o755), "/": _st(stat.S_IFDIR, 0o755)}

    def lstat_of(table):
        def lstat(p):
            if p not in table:
                raise FileNotFoundError(2, "absent", p)
            return table[p]
        return lstat

    same = lambda p: p
    target = Path("/usr/local/lib/levain-root")
    assert root_tree._root_owned_ancestors(target, lstat=lstat_of(anc), realpath=same) == (True, "")
    brew = {**anc, "/usr/local/lib": _st(stat.S_IFDIR, 0o755, uid=501)}
    assert root_tree._root_owned_ancestors(target, lstat=lstat_of(brew), realpath=same)[0] is False
    wide = {**anc, "/usr/local": _st(stat.S_IFDIR, 0o775)}
    assert root_tree._root_owned_ancestors(target, lstat=lstat_of(wide), realpath=same)[0] is False


def test_root_tree_ancestry_follows_links_to_their_real_parents():
    """F12: a root-owned link above the tree, pointing into a dir an operator owns, is judged by the
    resolved path's ancestry, and a destination that is itself a link is refused."""
    D, L = stat.S_IFDIR, stat.S_IFLNK
    table = {"/": _st(D, 0o755), "/usr": _st(D, 0o755), "/usr/local": _st(D, 0o755),
             "/usr/local/lib": _st(L, 0o755),                         # root's link ...
             "/Users": _st(D, 0o755), "/Users/alice": _st(D, 0o755, uid=501),
             "/Users/alice/lib": _st(D, 0o755)}                     # ... to a root dir under alice's

    def lstat(p):
        if p not in table:
            raise FileNotFoundError(2, "absent", p)
        return table[p]

    def realpath(p):
        return p.replace("/usr/local/lib", "/Users/alice/lib", 1)

    target = Path("/usr/local/lib/levain-root")
    ok, why = root_tree._root_owned_ancestors(target, lstat=lstat, realpath=realpath)
    assert ok is False and "/Users/alice" in why
    # the same link into a tree root owns all the way up passes
    table["/Users/alice"] = _st(D, 0o755)
    assert root_tree._root_owned_ancestors(target, lstat=lstat, realpath=realpath) == (True, "")
    # the destination itself a link: refused, wherever it points
    table["/usr/local/lib/levain-root"] = _st(L, 0o777)
    ok, why = root_tree._root_owned_ancestors(target, lstat=lstat, realpath=realpath)
    assert ok is False and "symlink" in why


@pytest.mark.parametrize("host", HOSTS)
def test_tree_links_refused_before_chown_and_renamed_into_place(host):
    steps = root_tree.tree_steps(host, Path("/src/levain"))
    tree = f"{'/usr/local/lib' if host == 'darwin' else '/usr/lib'}/levain-root"
    why = [s.why for s in steps]
    no_link = next(i for i, w in enumerate(why) if "holds no link" in w)
    chown_r = next(i for i, s in enumerate(steps) if s.argv and s.argv[0].endswith("chown") and "-R" in s.argv)
    chmod_r = next(i for i, s in enumerate(steps) if s.argv and s.argv[0].endswith("chmod") and "-R" in s.argv)
    assert no_link < chown_r < chmod_r
    # never rm -rf the live package then mv: rename it aside, rename the staged one in, remove the old
    argvs = [s.argv for s in steps if s.argv]
    assert not [a for a in argvs if a[0].endswith("rm") and a[-1] == f"{tree}/levain"]
    aside = argvs.index(next(a for a in argvs if a[1:] == (f"{tree}/levain", f"{tree}/levain.old")))
    into = argvs.index(next(a for a in argvs if a[1:] == (f"{tree}/levain.new", f"{tree}/levain")))
    drop = argvs.index(next(a for a in argvs if a[1:] == ("-rf", f"{tree}/levain.old")))
    assert aside < into < drop
    assert next(s for s in steps if s.argv == argvs[aside]).skip_if[-3:] == ("!", "-e", f"{tree}/levain")
    entries = {s.write[0].name for s in steps if s.write}
    assert entries == {"install-helper", "runtime-helper", "service-entry"}


def test_launchd_job_absent_reads_launchctl():
    class R:
        def __init__(self, rc):
            self.returncode = rc

    seen = []
    assert units._launchd_job_absent("com.levain.control", lambda argv, **kw: seen.append(argv) or R(113)) == (True, "")
    assert seen == [["/bin/launchctl", "print", "system/com.levain.control"]]
    ok, why = units._launchd_job_absent("com.levain.control", lambda argv, **kw: R(0))
    assert not ok and "still loaded" in why


# --- §23 the slots' bwrap: digest, validate -----------------------------------------------------------------


def test_bwrap_digest_recorded_and_apparmor_validated(tmp_path, monkeypatch):
    plan = _plan("linux")
    prof = next(s for s in plan.steps if s.write and s.write[0] == bwrap.APPARMOR_PROFILE)
    assert prof.validate[0].endswith("apparmor_parser") and prof.validate[1:] == ("-Q", "-K")
    copy = next(s for s in plan.steps if s.argv and s.argv[0].endswith("/install"))
    rec = next(s for s in plan.steps if s.call and "record the digests" in s.why)
    assert copy.skip_if[1:] == ("--check", "--status", "--strict", str(bwrap.BWRAP_DIGEST))
    assert rec.skip_if == copy.skip_if and plan.steps.index(copy) < plan.steps.index(rec)
    # replenish runs the same copy steps (re-copies when the system bwrap changed)
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up("linux", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=8, verifier_slots=2, live={"egress": range(8), "none": range(2)})
    assert [s.argv for s in rep.steps if s.argv and s.argv[0].endswith("/install")] == [copy.argv]
    # the record: sha256sum's check format, source AND copy; a differing copy is refused
    src, cp = tmp_path / "bwrap-src", tmp_path / "bwrap-copy"
    src.write_bytes(b"bwrap v1")
    cp.write_bytes(b"bwrap v1")
    monkeypatch.setattr(bwrap, "LEVAIN_BWRAP", cp)
    out = tmp_path / "rec" / "bwrap.sha256"
    assert bwrap.record_digest(str(src), out) == (True, "")
    d = bwrap.file_sha256(src)
    assert out.read_text() == f"{d}  {src}\n{d}  {cp}\n" and stat.S_IMODE(out.stat().st_mode) == 0o644
    sha = hands.shutil.which("sha256sum") if Path(str(hands.shutil.which("sha256sum"))).exists() else None
    if sha:
        assert subprocess.run([sha, "--check", "--status", "--strict", str(out)]).returncode == 0
        src.write_bytes(b"bwrap v2")                                   # the system bwrap changed
        assert subprocess.run([sha, "--check", "--status", "--strict", str(out)]).returncode != 0
    cp.write_bytes(b"other")
    assert bwrap.record_digest(str(src), out)[0] is False
    undo = _undo("linux", {"_levain_s0": RANGES["linux"].slots[0]})
    assert [s for s in undo.steps if s.argv[-2:] == ("-f", str(bwrap.BWRAP_DIGEST))]


def test_services_named_in_pending_line():
    plan = plan_setup(_setup(), _facts("linux"), read_sysctl=lambda p: "0", program_exists=lambda p: False)
    assert plan.services == ()
    for s in units.services_for("linux"):
        assert any(f"services/{s.name}.py" in p and f"{units.systemd_name(s)} not installed" in p
                   for p in plan.pending), s.name


def test_singletons_shared_and_slots_reused_ids_disjoint():
    """keep/revive never hand one id to two names."""
    r = RANGES["linux"]
    live = {"_levain_control": r.singletons[0]}
    tombs = {"_levain_keys": r.singletons[0]}
    assert _refusal(_plan, "linux", existing_names=frozenset(live) | frozenset(tombs), levain_live=live,
                    levain_tombstones=tombs) == "tombstone-id-conflict"
    assert SINGLETONS and SlotId("egress", 0).primary == "_levain_s0"
