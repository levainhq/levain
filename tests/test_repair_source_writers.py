"""Row 33 §2.1 [r2: X-8; r3: F9]: who may write the ``--source`` package, checked at the repair gather (R0), before
any other root read of the source. RED before this lane: nothing checked it, and plan_repair hashed the source."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.slot import Refused
from levain.slot import install_helper as IH
from levain.slot import root_tree
from levain.slot.grammar import parse_install_argv

OP_UID, OP_GID, LEVAIN_GID = 501, 1501, 3999


def _fs(tmp: Path, overrides: dict[str, tuple[int, int, int]] | None = None):
    """A source at ``tmp/pkg/{levain,h11}`` on a fake lstat: every path root 0755 unless overridden as
    ``path -> (uid, gid, mode)``; the tree below the packages is read with the real walk."""
    parent = tmp / "pkg"
    for rel in ("levain/__init__.py", "levain/slot/__init__.py", "h11/_version.py"):
        (parent / rel).parent.mkdir(parents=True, exist_ok=True)
        (parent / rel).write_text("")
    over = {str(k): v for k, v in (overrides or {}).items()}

    def lstat(p: str):
        uid, gid, mode = over.get(p, (OP_UID if p.startswith(str(parent)) else 0, 0, 0o755))
        kind = stat.S_IFDIR if os.path.isdir(p) else stat.S_IFREG
        return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=kind | mode)
    return parent, lstat


def _check(parent: Path, lstat) -> None:
    root_tree.check_source_writers(parent, operator_uid=OP_UID, operator_gids={OP_GID}, lstat=lstat)


@pytest.mark.parametrize("where, owner, why", [
    ("ancestor", (0, LEVAIN_GID, 0o775), "other than the operator"),         # group-writable by a Levain group
    ("ancestor", (0, 0, 0o1777), "every user"),                              # a sticky /tmp-style ancestor
    ("ancestor", (1234, 0, 0o755), "neither root nor the operator"),
    ("parent", (OP_UID, 0, 0o757), "every user"),
    ("levain", (1234, 0, 0o755), "neither root nor the operator"),
    ("h11", (OP_UID, LEVAIN_GID, 0o775), "other than the operator"),
    ("file", (OP_UID, 0, 0o666), "every user"),
    ("deep", (1234, 0, 0o644), "neither root nor the operator"),
])
def test_a_source_a_non_operator_can_write_is_refused(tmp_path: Path, where, owner, why) -> None:
    parent = tmp_path / "pkg"
    path = {"ancestor": tmp_path, "parent": parent, "levain": parent / "levain", "h11": parent / "h11",
            "file": parent / "h11" / "_version.py", "deep": parent / "levain" / "slot" / "__init__.py"}[where]
    parent, lstat = _fs(tmp_path, {path: owner})
    with pytest.raises(Refused) as e:
        _check(parent, lstat)
    assert e.value.name == "bad-source" and why in e.value.detail and str(path) in e.value.detail


def test_an_operator_or_root_source_passes(tmp_path: Path) -> None:
    # group-writable by a group whose whole membership is the operator (Ubuntu's umask 002 on its user-private
    # group) is accepted, on an ancestor and below the package alike [judged by r2repair, 10-22: Q2; L3 r1]
    init = tmp_path / "pkg" / "levain" / "__init__.py"
    parent, lstat = _fs(tmp_path, {tmp_path: (OP_UID, OP_GID, 0o775), init: (OP_UID, OP_GID, 0o664)})
    _check(parent, lstat)


def test_the_gather_checks_the_resolved_chain_before_any_read(tmp_path: Path, monkeypatch) -> None:
    """§2.1 (a): the check runs on realpath(PATH/..), at R0, and a refusal leaves the source unhashed."""
    real = tmp_path / "real"
    (real / "levain").mkdir(parents=True)
    (real / "h11").mkdir()
    (tmp_path / "lnk").symlink_to(real)
    seen: list[Path] = []
    monkeypatch.setattr(root_tree, "check_source_writers",
                        lambda parent, **kw: (seen.append(parent), (_ for _ in ()).throw(Refused("bad-source", "x"))))
    hashed: list = []
    monkeypatch.setattr(IH.gen_mod, "generation_id", lambda *a: hashed.append(a))
    d = IH.Directory(users={"_levain_s0": 3999, "phill": OP_UID}, groups={"levain-slots": 3788},
                     group_members={"levain-slots": frozenset({"_levain_s0"})})
    cmd = parse_install_argv(["repair", "--source", str(tmp_path / "lnk" / "levain")])
    monkeypatch.setattr(root_tree, "read_current", lambda tree: None)
    with pytest.raises(Refused) as e:
        IH._repair_facts(cmd, "darwin", d, {}, {}, operator_uid=OP_UID)
    assert e.value.name == "bad-source"
    assert seen == [Path(os.path.realpath(real))] and hashed == []


def test_operator_only_gids_is_the_complete_membership() -> None:
    """Which gids pass: the member list AND every user holding the gid as primary must be the operator alone."""
    d = IH.Directory(users={"root": 0, "phill": OP_UID, "bob": 1001, "_levain_s0": 3999, "_levain_keys": 3799},
                     groups={"root": 0, "phill": OP_GID, "bob": 1001, "devs": 1002, "_levain_s0": 3999,
                             "levain-slots": 3788, "alias": OP_GID, "solo": 600, "ghost": 601},
                     group_members={"levain-slots": frozenset({"_levain_s0"}), "solo": frozenset({"phill"}),
                                    "ghost": frozenset({"gone"})},
                     user_gids={"root": 0, "phill": OP_GID, "bob": 1002, "_levain_s0": 3999, "_levain_keys": 3799})
    assert IH.operator_only_gids(d, OP_UID) == {OP_GID, 600}           # bob's own 1001: empty proves nothing
    nest = IH.Directory(**{**d.__dict__, "nested": frozenset({"solo"})})
    assert IH.operator_only_gids(nest, OP_UID) == {OP_GID}
    two = IH.Directory(**{**d.__dict__, "group_members": {**d.group_members, "alias": frozenset({"bob"})}})
    assert OP_GID not in IH.operator_only_gids(two, OP_UID)             # every group name at the gid must pass


# --- L3 r1 (both seats): a group-writable path is accepted only when the group's COMPLETE membership (its member list
# AND every user whose primary gid it is) is the operator alone. RED at 24b86068, which asked only whether a Levain
# account reaches the group: macOS `staff` (gid 20, the primary group of every local user, whom its list never names)
# and a second Linux user's primary group passed. ---------------------------------------------------------------------

def _dir(host: str, *, users, groups, members=None, nested=()):
    """``users``: name -> (uid, primary gid); ``groups``: name -> gid."""
    return IH.Directory(users={n: u for n, (u, _) in users.items()}, groups=dict(groups),
                        group_members={g: frozenset((members or {}).get(g, ())) for g in groups},
                        user_gids={n: g for n, (_, g) in users.items()}, nested=frozenset(nested))


MAC = dict(users={"root": (0, 0), "phill": (OP_UID, 20), "_levain_s0": (3999, 3999)},
           groups={"wheel": 0, "staff": 20, "admin": 80, "_levain_s0": 3999, "levain-slots": 3788},
           members={"wheel": {"root"}, "staff": {"root"}, "admin": {"root", "phill"}, "levain-slots": {"_levain_s0"}})


_REAL = root_tree.check_source_writers


def _gather(tmp: Path, monkeypatch, host: str, d, over: dict) -> None:
    """``_repair_facts`` on a source at ``tmp/pkg``, its lstat faked by ``over`` (path -> (uid, gid, mode))."""
    parent, lstat = _fs(tmp, over)
    monkeypatch.setattr(root_tree, "check_source_writers", lambda p, **kw: _REAL(p, lstat=lstat, **kw))
    monkeypatch.setattr(root_tree, "read_current", lambda tree: None)
    cmd = parse_install_argv(["repair", "--source", str(parent / "levain")])
    IH._repair_facts(cmd, host, d, {}, {}, operator_uid=OP_UID)


def _refused(tmp, monkeypatch, host, d, path, gid, mode=0o775, uid=OP_UID) -> str:
    with pytest.raises(Refused) as e:
        _gather(tmp, monkeypatch, host, d, {path: (uid, gid, mode)})
    assert e.value.name == "bad-source" and str(path) in e.value.detail
    return e.value.detail


def test_macos_staff_with_a_second_user_is_refused(tmp_path: Path, monkeypatch) -> None:
    mac = {**MAC, "users": {**MAC["users"], "alice": (502, 20)}}
    why = _refused(tmp_path, monkeypatch, "darwin", _dir("darwin", **mac), tmp_path, 20)
    assert "staff" in why and f"`chmod g-w {tmp_path}`" in why                 # an ancestor: never -R
    sub = tmp_path / "b" / "pkg" / "levain" / "slot"                          # below the package: -R, that exact dir
    why = _refused(tmp_path / "b", monkeypatch, "darwin", _dir("darwin", **mac), sub, 20)
    assert f"`chmod -R g-w {sub}`" in why


def test_macos_staff_is_refused_even_with_the_operator_alone(tmp_path: Path, monkeypatch) -> None:
    # staff lists root, and every account created later joins it by primary gid: it is not a trust boundary
    _refused(tmp_path, monkeypatch, "darwin", _dir("darwin", **MAC), tmp_path, 20)


def test_macos_admin_listing_root_and_the_operator_is_refused(tmp_path: Path, monkeypatch) -> None:
    # the head's ruling: a subset of {operator}; root is a member, so admin is refused [judged by r2repair-fix1, Q1]
    _refused(tmp_path, monkeypatch, "darwin", _dir("darwin", **MAC), tmp_path / "pkg" / "h11" / "_version.py", 80,
             mode=0o664)


def test_macos_a_group_that_nests_another_is_refused(tmp_path: Path, monkeypatch) -> None:
    mac = {**MAC, "groups": {**MAC["groups"], "solo": 600}, "members": {**MAC["members"], "solo": {"phill"}}}
    _gather(tmp_path, monkeypatch, "darwin", _dir("darwin", **mac), {tmp_path: (OP_UID, 600, 0o775)})   # control
    _refused(tmp_path / "n", monkeypatch, "darwin", _dir("darwin", **mac, nested={"solo"}), tmp_path / "n", 600)


LINUX = dict(users={"root": (0, 0), "phill": (OP_UID, 1000), "bob": (1001, 1001), "_levain_s0": (3999, 3999)},
             groups={"root": 0, "phill": 1000, "bob": 1001, "_levain_s0": 3999, "devs": 1002})


def _linux(**kw):
    return _dir("linux", users={**LINUX["users"], **kw.pop("users", {})}, groups=LINUX["groups"], **kw)


def test_linux_the_operators_user_private_group_is_accepted(tmp_path: Path, monkeypatch) -> None:
    init = tmp_path / "pkg" / "levain" / "__init__.py"
    _gather(tmp_path, monkeypatch, "linux", _linux(), {tmp_path: (OP_UID, 1000, 0o775), init: (OP_UID, 1000, 0o664),
                                                       tmp_path / "pkg" / "h11": (OP_UID, 1000, 0o2775)})


def test_linux_a_second_users_primary_group_is_refused(tmp_path: Path, monkeypatch) -> None:
    d = _linux(users={"bob": (1001, 1002)})                # devs lists nobody; bob holds it as his primary group
    why = _refused(tmp_path, monkeypatch, "linux", d, tmp_path / "pkg" / "levain" / "__init__.py", 1002, mode=0o664)
    assert "devs" in why and f"chmod g-w {tmp_path / 'pkg' / 'levain' / '__init__.py'}" in why


def test_linux_an_explicit_extra_member_is_refused(tmp_path: Path, monkeypatch) -> None:
    d = _linux(members={"phill": {"bob"}})
    _refused(tmp_path, monkeypatch, "linux", d, tmp_path / "pkg", 1000)


def test_macos_a_computed_group_listing_no_one_is_refused(tmp_path: Path, monkeypatch) -> None:
    # `everyone` (gid 12) lists no one and holds every user; the first cut of this fix read that empty list as
    # "no one but the operator" and accepted it [measured on Phill's Mac: 1022-20/r2repair/fix1_realmac_raw.txt]
    mac = {**MAC, "groups": {**MAC["groups"], "everyone": 12, "localaccounts": 61}}
    _refused(tmp_path, monkeypatch, "darwin", _dir("darwin", **mac), tmp_path, 12)
    _refused(tmp_path / "l", monkeypatch, "darwin", _dir("darwin", **mac), tmp_path / "l", 61)


def test_a_gid_no_group_names_is_refused(tmp_path: Path, monkeypatch) -> None:
    _refused(tmp_path, monkeypatch, "linux", _linux(), tmp_path, 4242)


def test_read_primary_groups_on_macos() -> None:
    out = {"/Users PrimaryGroupID": "root 0\nphill 20\n_nobody\nalice 20\n",
           "/Groups NestedGroups": "admin\n_lpadmin ABCDEFAB-CDEF-ABCD-EFAB-CDEF00000050\n"}
    calls: list = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout=out[" ".join(argv[3:])], stderr="")
    d = IH.read_primary_groups(IH.Directory(users={"root": 0}, groups={}), run=run)
    assert d.user_gids == {"root": 0, "phill": 20, "alice": 20} and d.nested == {"_lpadmin"}
    assert all(c[:3] == ("/usr/bin/dscl", ".", "-list") for c in calls)
    out["/Users PrimaryGroupID"] = "phill 20 21\n"
    with pytest.raises(Refused) as e:
        IH.read_primary_groups(IH.Directory(users={"root": 0}, groups={}), run=run)
    assert e.value.name == "host-read-failed"


@pytest.mark.parametrize("host, argv, read", [("darwin", ["repair", "--source", "/x/levain"], True),
                                              ("darwin", ["repair"], False), ("linux", ["repair", "--source",
                                                                                         "/x/levain"], False)])
def test_gather_reads_macos_primary_groups_only_for_a_repair_source(monkeypatch, tmp_path, host, argv, read) -> None:
    seen: list = []
    base = IH.Directory(users={"root": 0}, groups={})
    monkeypatch.setattr(IH, "read_directory", lambda h, run: base)
    monkeypatch.setattr(IH, "read_primary_groups", lambda d, run: seen.append(d) or d)
    monkeypatch.setattr(IH, "_gather_held", lambda cmd, h, uid, d, *a, **kw: d)
    seams = SimpleNamespace(L=tmp_path / "L")
    IH.gather_facts(parse_install_argv(argv), host, OP_UID, run=lambda *a, **k: None, store_seams=seams)
    assert seen == ([base] if read else [])


def test_the_printed_remedy_quotes_the_path(tmp_path: Path, monkeypatch) -> None:
    """L3 r2 (both seats LOW, head 1022+21 ruling 1): a printed remedy is a command the operator will run, so the path
    is shell-quoted; unquoted, `chmod g-w <dir>/a b.py` names two paths and leaves the offending one writable."""
    import shlex

    d = _linux(users={"bob": (1001, 1002)})
    spaced = tmp_path / "pkg" / "levain" / "a b.py"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("")
    why = _refused(tmp_path, monkeypatch, "linux", d, spaced, 1002, mode=0o664)
    assert f"`chmod g-w {shlex.quote(str(spaced))}`" in why and shlex.quote(str(spaced)) != str(spaced)
    sub = tmp_path / "s" / "pkg" / "levain" / "my dir"
    sub.mkdir(parents=True)
    why = _refused(tmp_path / "s", monkeypatch, "linux", d, sub, 1002)
    assert f"`chmod -R g-w {shlex.quote(str(sub))}`" in why
