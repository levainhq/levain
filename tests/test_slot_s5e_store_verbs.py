"""S1 session 5 unit 5e: the store verbs (discard / restore / purge, CONTRACT §7.4, DESIGN §4.6, §4.7).

Each test comes from a failure reproduced on main first: a discard / restore / purge journal refused
``intent-unbuilt`` (nothing could finish one), and ``install-helper purge <s>`` answered ``verb-unbuilt:purge``.
"""
from __future__ import annotations

import functools
import io
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import install_helper as IH
from levain.slot import records as R
from levain.slot import runtime_helper as RH
from levain.slot import store_verbs as SV
from levain.slot.ids import SlotId
from levain.slot.root_intent import INTENT_PHASES

S = "s-" + "5e" * 8
T = "s-" + "7a" * 8
UID = os.getuid()
mine = lambda st: st.st_uid == UID   # noqa: E731


class Crash(Exception):
    pass


class Ctl:
    """CONTROL's §7.4 rule: a CAS to the state the record holds is done; any other comes from the target's one edge."""

    def __init__(self, state: str) -> None:
        self.state = state

    def cas(self, target, fields):
        if self.state == target:
            return
        if self.state != SV.CAS_FROM[target]:
            raise Refused("from-mismatch", f"{self.state} -> {target}")
        self.state = target


def _store(tmp_path: Path) -> Path:
    L = tmp_path / "L"
    for d in ("alloc", "retained", "trash", "export/operator", "export/dispatch"):
        (L / d).mkdir(parents=True)
    for s in (S, T):
        r = L / "retained" / s
        (r / "tree" / "repo" / "sub").mkdir(parents=True)
        (r / "tree" / "repo" / "sub" / "a.txt").write_bytes(b"alpha\n")
        os.symlink("sub/a.txt", r / "tree" / "repo" / "link")
        (r / "object.json").write_bytes(canon.canonical_bytes({"format": "levain-retained-object/1", "session": s}))
        (r / "retained_at").write_bytes(b"2026-10-01T00:00:00Z\n")
    return L


def _facts(L: Path, s: str = S) -> SV.StoreFacts:
    return SV.StoreFacts(session=s, store=L, kind="interactive", operator_uid=UID)


def _seams(ctl: Ctl, point: str | None = None, now: str = "2026-10-21T22:00:00Z") -> SV.StoreSeams:
    def fault(p):
        if p == point:
            raise Crash(p)
    return SV.StoreSeams(cas=ctl.cas, now=lambda: now, owner_ok=mine, fault=fault)


def _content(entry: Path) -> list:
    return sorted((str(p.relative_to(entry)), os.readlink(p) if p.is_symlink() else p.read_bytes())
                  for p in entry.rglob("*") if p.name not in ("trashed_at", "restored_at")
                  and (p.is_symlink() or p.is_file()))


def _plant(L: Path, s: str) -> None:
    e = L / "export" / "operator"
    (e / s / "verify.new").mkdir(parents=True)
    (e / s / "verify.new" / "verified.json").write_bytes(b"{}")
    (e / s / "bundle").write_bytes(b"B")
    (e / (s + ".new")).mkdir()
    (e / (s + ".new") / "bundle").write_bytes(b"old")


_POINTS = [(v, f"{v}:{w}:{ph}") for v in ("discard", "restore", "purge") for ph in INTENT_PHASES[v]
           for w in ("before", "after")] + [(v, f"{v}:{w}:cas") for v in ("discard", "restore", "purge")
                                            for w in ("before", "after")]


@pytest.mark.parametrize("verb,point", _POINTS)
def test_crash_at_every_store_phase_reconciles_to_target(tmp_path, verb, point):
    L = _store(tmp_path)
    ctl = Ctl(R.RETAINED)
    if verb != "discard":
        SV.discard(_facts(L), _seams(ctl))
        _plant(L, S)
    src = L / ("retained" if verb == "discard" else "trash") / S
    before = _content(src)
    run = {"discard": SV.discard, "restore": SV.restore,
           "purge": functools.partial(SV.purge, forced=True)}[verb]
    with pytest.raises(Crash):
        run(_facts(L), _seams(ctl, point))
    if SV.reconcile_store(_facts(L), _seams(ctl)) is None:
        assert point.endswith(":" + INTENT_PHASES[verb][0]) and _content(src) == before   # nothing journaled
        run(_facts(L), _seams(ctl))
    assert not (L / "alloc" / f"{S}.intent.json").exists()
    assert ctl.state == {"discard": R.TRASH, "restore": R.RETAINED, "purge": R.PURGED}[verb]
    e = L / "export" / "operator"
    if verb == "purge":
        assert not any(os.path.lexists(p) for p in (L / "trash" / S, L / "retained" / S, e / S, e / (S + ".new")))
    else:
        dst = L / ("trash" if verb == "discard" else "retained") / S
        assert _content(dst) == before and not os.path.lexists(src)
        assert (dst / "trashed_at").exists() == (verb == "discard")
        assert (dst / "restored_at").exists() == (verb == "restore")
    assert (L / "retained" / T).exists()   # the neighbour is never touched


def _alloc(L: Path, s: str = S) -> None:
    a = R.Alloc(session=s, slot=SlotId("egress", 3), lease_seq=7, slot_rev=12, slot_uid=3990, slot_gid=3990,
                kind="interactive", vendor="claude", auth="brokered", operator_uid=UID, supervisor_uid=UID,
                staging_uid=UID, created_at="2026-10-01T00:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
    (L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))


def test_purge_after_a_failed_commit_cas_is_finished_by_the_next_purge(tmp_path):
    # Reproduced on 62859049: the second purge refused intent-in-flight purge/commit, every time after.
    L = _store(tmp_path)
    ctl = Ctl(R.RETAINED)
    SV.discard(_facts(L), _seams(ctl))
    _alloc(L)
    down = [1]

    def cas(target, fields):
        if down[0]:
            down[0] -= 1
            raise OSError("CONTROL down")
        ctl.cas(target, fields)
    purge = functools.partial(IH.run_purge, S, UID, "darwin",
                              store_seams=RH.Seams(host="darwin", store=L, trusted=lambda st: True),
                              verb_seams=lambda c: SV.StoreSeams(cas=cas, owner_ok=mine))
    with pytest.raises(Refused) as e:
        purge()
    assert e.value.name == "purge-incomplete:commit" and not (L / "trash" / S).exists()
    assert purge() == {"verb": "purge", "session": S, "state": R.PURGED}
    assert ctl.state == R.PURGED and not (L / "alloc" / f"{S}.intent.json").exists()


class _Os:
    """``os`` as store_verbs sees it, counting the directory entries it reads; every stat of an inode in
    ``mounted`` reports another device (a mount there, without mounting one)."""

    def __init__(self, mounted=frozenset()):
        self.mounted, self.reads = mounted, 0

    def __getattr__(self, n):
        return getattr(os, n)

    def _fake(self, st):
        if st.st_ino not in self.mounted:
            return st
        return type("St", (), {"st_dev": st.st_dev + 1, "__getattr__": lambda _, n: getattr(st, n)})()

    def stat(self, *a, **k):
        return self._fake(os.stat(*a, **k))

    def fstat(self, fd):
        return self._fake(os.fstat(fd))

    def scandir(self, fd):
        outer, it = self, os.scandir(fd)

        class It:
            def __enter__(self):
                return self

            def __exit__(self, *x):
                it.close()

            def __iter__(self):
                for e in it:
                    outer.reads += 1
                    yield e
        return It()


def test_delete_reads_each_entry_once_and_refuses_a_mounted_target(tmp_path, monkeypatch):
    # Reproduced on 62859049: 2,000 sibling dirs cost 2,001,000 entry reads; a trash entry on another device was
    # emptied and removed.
    wide = tmp_path / "wide"
    for i in range(2000):
        (wide / f"d{i:05d}").mkdir(parents=True)
    (wide / "d00007" / "f").write_bytes(b"x")
    fake = _Os()
    monkeypatch.setattr(SV, "os", fake)
    dfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert SV.delete_at(dfd, "wide", _seams(Ctl(R.TRASH)))
    finally:
        os.close(dfd)
    assert not wide.exists() and fake.reads <= 2002
    L = _store(tmp_path)
    SV.discard(_facts(L), _seams(Ctl(R.RETAINED)))
    top = L / "trash" / S
    monkeypatch.setattr(SV, "os", _Os({os.lstat(p).st_ino for p in [top, *top.rglob("*")]}))
    with pytest.raises(Refused) as e:
        SV.delete_named(L, ("trash",), (S,), _seams(Ctl(R.TRASH)))
    assert e.value.name == "delete-crosses-mount" and (top / "tree" / "repo" / "sub" / "a.txt").exists()


def test_purge_verb_removes_export_staging_and_flagged_trash(tmp_path):
    L = _store(tmp_path)
    ctl = Ctl(R.RETAINED)
    with pytest.raises(Refused) as e:   # RETAINED is not purgeable
        SV.purge(_facts(L), _seams(ctl), forced=True)
    assert e.value.name == "not-trash:retained"
    SV.discard(_facts(L), _seams(ctl))
    _plant(L, S)
    _plant(L, T)
    if sys.platform == "darwin":   # a user flag on a file and a dir the caller owns: the owner clears it, no root
        subprocess.run(["chflags", "uchg", str(L / "trash" / S / "tree" / "repo" / "sub" / "a.txt")], check=True)
        subprocess.run(["chflags", "uchg", str(L / "trash" / S / "tree" / "repo" / "sub")], check=True)
    fd = os.open(L / "export" / "adopt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    with pytest.raises(Refused) as e:
        SV.purge(_facts(L), _seams(ctl), forced=True)
    os.close(fd)
    assert e.value.name == "adopt-busy" and (L / "trash" / S).exists()
    a = R.Alloc(session=S, slot=SlotId("egress", 3), lease_seq=7, slot_rev=12, slot_uid=3990, slot_gid=3990,
                kind="interactive", vendor="claude", auth="brokered", operator_uid=UID, supervisor_uid=UID,
                staging_uid=UID, created_at="2026-10-01T00:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
    (L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(a))
    purge = functools.partial(IH.run_purge, store_seams=RH.Seams(host="darwin", store=L, trusted=lambda st: True),
                              verb_seams=lambda cas: SV.StoreSeams(cas=ctl.cas, owner_ok=mine))
    out = io.StringIO()
    rc = IH.main(["purge", S], env={"SUDO_UID": str(UID)}, out=out, err=io.StringIO(),
                 seams=IH.Seams(euid=lambda: 0, host=lambda: "darwin", lock=lambda h: nullcontext(), purge=purge))
    assert (rc, out.getvalue()) == (0, '{"ok":true,"session":"%s","state":"PURGED","verb":"purge"}\n' % S)
    e = L / "export" / "operator"
    assert not any(os.path.lexists(p) for p in (L / "trash" / S, e / S, e / (S + ".new")))
    assert (e / T / "verify.new" / "verified.json").exists() and (e / (T + ".new")).exists()
    assert ctl.state == R.PURGED and not (L / "alloc" / f"{S}.intent.json").exists()
