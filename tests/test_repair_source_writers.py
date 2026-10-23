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

OP_UID, LEVAIN_GID, OTHER_GID = 501, 3999, 80


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
    root_tree.check_source_writers(parent, operator_uid=OP_UID, levain_gids={LEVAIN_GID}, lstat=lstat)


@pytest.mark.parametrize("where, owner, why", [
    ("ancestor", (0, LEVAIN_GID, 0o775), "a Levain account holds"),         # group-writable by a Levain group
    ("ancestor", (0, 0, 0o1777), "every user"),                              # a sticky /tmp-style ancestor
    ("ancestor", (1234, 0, 0o755), "neither root nor the operator"),
    ("parent", (OP_UID, 0, 0o757), "every user"),
    ("levain", (1234, 0, 0o755), "neither root nor the operator"),
    ("h11", (OP_UID, LEVAIN_GID, 0o775), "a Levain account holds"),
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
    # /opt/homebrew/lib is `drwxrwxr-x <operator> admin` (§2.1 (b)): group-writable by a group no Levain account
    # reaches is accepted, on an ancestor and below the package alike [judged by r2repair, 10-22: Q2]
    init = tmp_path / "pkg" / "levain" / "__init__.py"
    parent, lstat = _fs(tmp_path, {tmp_path: (OP_UID, OTHER_GID, 0o775), init: (OP_UID, OTHER_GID, 0o664)})
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


def test_reach_is_every_levain_primary_gid_and_listed_group(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "levain").mkdir()
    (tmp_path / "h11").mkdir()
    got: dict = {}
    monkeypatch.setattr(root_tree, "check_source_writers", lambda parent, **kw: got.update(kw))
    monkeypatch.setattr(root_tree, "read_current", lambda tree: None)
    d = IH.Directory(users={"_levain_s0": 3999, "_levain_keys": 3799, "phill": OP_UID},
                     groups={"levain-slots": 3788, "admin": 80, "staff": 20},
                     group_members={"levain-slots": frozenset({"_levain_s0"}), "admin": frozenset({"phill"}),
                                    "staff": frozenset({"phill", "_levain_keys"})})
    cmd = parse_install_argv(["repair", "--source", str(tmp_path / "levain")])
    IH._repair_facts(cmd, "darwin", d, {}, {}, operator_uid=OP_UID)
    assert got["operator_uid"] == OP_UID and set(got["levain_gids"]) == {3999, 3799, 3788, 20}
