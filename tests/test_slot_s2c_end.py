"""CONTRACT §7.3-7.5 (END: revoke, freeze, kill, park, RETAIN; the reserve), V1 V2 V6 V12 V14 V17.

Real files in pytest tmp dirs and a real git repo; the root-only acts are seams: freeze / kill / the
process sweep are fakes, CONTROL's CAS is the REAL ``Control.cas`` (records' edge table, the field merge,
the "already done" rule) over a temp store, ``chown`` is the real ``fchownat`` to THIS test's uid/gid (no
root), and the session-uid reader is stage's in-process runner (no drop). The metadata reader and strip
are the host's real ones."""
from __future__ import annotations

import errno
import itertools
import os
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import end as end_mod
from levain.slot import records as R
from levain.slot import stage
from levain.slot.end import (CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED, VAR_FOLDERS_NAME, Caps,
                             EndFacts, EndSeams, Reserve, end_session, facts_from_alloc, keep_shape_ok,
                             reconcile_session, tree_manifest)
from levain.slot.ids import SlotId
from levain.slot.root_intent import INTENT_PHASES, RootIntent, read_root_intent, temp_path, write_root_intent

SESSION = "s-0123456789abcdef"
NOW = "2026-10-20T18:00:00Z"
UID, GID = os.getuid(), os.getgid()
CRED = b'{"claudeAiOauth":{"accessToken":"sk-ant-oat-TEST"}}'
DARWIN = sys.platform == "darwin"
UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x2)
LEASE_SEQ = 5
OP_UID = UID if UID != 0 else 501


class Crash(BaseException):
    """A process death at a fault point: not an Exception, so END's HELD path never catches it."""


class Control:
    """CONTROL's record as END sees it, through the REAL :meth:`Control.cas`: the edge table, the field
    merge and the "already done" rule are CONTROL's own. The client side reads the record's generation
    and state before each CAS, as a client that re-reads would; a CAS already at its target is sent
    ``from`` a predecessor (a client whose read predates the move), so CONTROL's rule decides it.
    ``history`` = the CASes that moved a generation, without the null fields. ``refuse``: refusal names
    raised before the next CASes reach CONTROL (a try-lock collision, a restart)."""

    def __init__(self, state: str = REVOKED, session: str = SESSION) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="lv-end-ctl-")
        self.session = session
        self.store = C.StateStore(Path(self._dir.name))
        self.ctl = C.Control(self.store, OP_UID)
        with self.store.lock(session):
            self.store.put(R.SessionState(session=session, generation=0, state=state, claims={}, lease_live=False,
                                          event_kind="state", updated_at=NOW))
        self.history: list = []
        self.refuse: list = []
        self.sent: list = []

    def cas(self, target, fields):
        self.sent.append((target, dict(fields)))
        if self.refuse:
            raise Refused(self.refuse.pop(0))
        rec = self.store.get(self.session)
        frm, gen0 = rec.state, rec.generation
        if frm == target and target != HELD and rec.generation > 0:
            # a read from before the move: its predecessor state AND the generation before it (S2 L3 r1 c1: the
            # "already done" rule is a lost-ack retry's, one generation behind)
            frm, gen0 = next(a for a, b in sorted(R.EDGES) if b == target), rec.generation - 1
        gen = self.ctl.cas(self.session, gen0, frm, target, dict(fields))
        if gen != rec.generation:
            self.history.append((target, {k: v for k, v in fields.items() if v is not None}))

    @property
    def rec(self):
        return self.store.get(self.session)

    @property
    def state(self):
        return self.rec.state

    @property
    def fields(self):
        return dict(self.rec.fields)

    @property
    def failed_step(self):
        return self.fields.get("failed_step") if self.state == HELD else None


def slot_state(L: Path, slot: SlotId, session: str = SESSION, lease_seq: int = LEASE_SEQ,
               state: str = "leased") -> None:
    """Root's slot state file (§3.5 Pool) for ``slot``: leased to ``session`` at ``lease_seq``."""
    d = slot.slot_dir(L)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state").write_bytes(R.slot_state_bytes(R.SlotState(
        slot, rev=lease_seq * 2, lease_seq=lease_seq, state=state, dispatch_eligible=False, changed_at=NOW,
        session=session if state == "leased" else None)))


class _Exited:
    """stage's threaded reader, whose ``wait()`` answers the exit status END requires (0: the thread ended)."""

    def __init__(self, reader) -> None:
        self._r = reader

    def kill(self) -> None:
        self._r.kill()

    def wait(self) -> int:
        self._r.wait()
        return 0


def thread_run_as(calls: list | None = None):
    """stage's in-process runner in the session-uid reader's shape (the drop is the VM's)."""
    def run_as(uid, gid, fn, wfd, keep=()):
        if calls is not None:
            calls.append((uid, gid, tuple(keep)))
        return _Exited(stage.thread_run_as(uid, gid, fn, wfd))
    return run_as


def entry_of(path: Path) -> end_mod.Entry:
    """An Entry for one path, for calling the fd-based metadata helpers in a test."""
    st = os.lstat(path)
    return end_mod.Entry(os.fsencode(path.name), st, -1, os.fsencode(path.name), os.fsencode(path))


def acl_text(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return end_mod._darwin_acl_text(fd)
    finally:
        os.close(fd)


def xattr_names(path: Path) -> list:
    if not DARWIN:
        return [os.fsencode(n) for n in os.listxattr(path)]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return end_mod._darwin_xattr_names(fd)
    finally:
        os.close(fd)


def mine(st):
    return st.st_uid == UID


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null", *args],
                   cwd=repo, env=env, check=True, capture_output=True)


def _set_xattr(path: Path, name: str, value: bytes) -> bool:
    try:
        if DARWIN:
            subprocess.run(["/usr/bin/xattr", "-w", name, value.decode(), str(path)], check=True,
                           capture_output=True)
        else:
            os.setxattr(path, name, value, follow_symlinks=False)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _unflag(root: Path) -> None:
    if not DARWIN or not os.path.lexists(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for n in [*dirnames, *filenames]:
            try:
                os.chflags(os.path.join(dirpath, n), 0, follow_symlinks=False)
            except OSError:
                pass


class World:
    def __init__(self, tmp: Path, *, git: bool = True, var_folders: bool = True, uchg: bool = True,
                 cloned: bool = True) -> None:
        self.tmp = tmp
        self.L = tmp / "L"
        for d in ("live", "alloc", "retained", "sessions", "slots/e3/keep"):
            (self.L / d).mkdir(parents=True)
        os.chmod(self.L / "retained", 0o700)
        self.S = self.L / "live" / SESSION
        self.vf = tmp / "var-folders" / "zz" / "slot3" if var_folders else None
        self.facts = EndFacts(SESSION, SlotId("egress", 3), UID, GID, self.L, self.vf, lease_seq=LEASE_SEQ,
                              vendor="claude", auth="passthrough")
        self.xattr = False
        slot_state(self.L, self.facts.slot)
        self._build(git, uchg)
        if cloned:
            end_mod.mark_cloned(self.L, SESSION, now=lambda: NOW)

    def _build(self, git: bool, uchg: bool) -> None:
        S = self.S
        for d in ("root", "in", "repo", "config/claude", "config/codex", "xdg/cache", "tmp", "sock"):
            (S / d).mkdir(parents=True)
        (S / "root" / "settings.json").write_text('{"permissions":{}}')
        os.chmod(S / "root" / "settings.json", 0o640)
        (S / "in" / "repo.bundle").write_bytes(b"# v2 git bundle\n")
        repo = S / "repo"
        (repo / "README").write_text("committed\n")
        (repo / ".gitignore").write_text("build/\n*.log\n")
        if git:
            _git(repo, "init", "-q")
            _git(repo, "add", "README", ".gitignore")
            _git(repo, "commit", "-q", "-m", "init")
        (repo / "README").write_text("committed\nAND AN UNCOMMITTED EDIT\n")   # uncommitted edit
        (repo / "untracked.txt").write_text("never added\n")
        (repo / "build").mkdir()
        (repo / "build" / "out.bin").write_bytes(bytes(range(256)) * 4)        # ignored
        (repo / "debug.log").write_text("ignored log\n")                       # ignored
        os.symlink("README", repo / "link-to-readme")
        os.symlink("/etc/passwd", repo / "link-outside")                        # never followed
        (repo / "secret.txt").write_text("0600 bytes\n")
        os.chmod(repo / "secret.txt", 0o600)
        (repo / "private").mkdir()
        (repo / "private" / "inner.txt").write_text("in a 0700 dir\n")
        os.chmod(repo / "private", 0o700)
        (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(repo / "run.sh", 0o755)
        (S / "config" / "claude" / ".credentials.json").write_bytes(CRED)
        os.chmod(S / "config" / "claude" / ".credentials.json", 0o600)
        (S / "config" / "claude" / "settings.local.json").write_text("{}")
        (S / "xdg" / "cache" / "blob").write_bytes(b"\x00\x01cache")
        self.xattr = _set_xattr(repo / "README", "user.levain.test", b"kept-in-meta")
        if DARWIN and uchg:
            (repo / "locked.txt").write_text("immutable\n")
            os.chflags(repo / "locked.txt", UF_IMMUTABLE)
        if self.vf is not None:
            (self.vf / "T").mkdir(parents=True)
            (self.vf / "T" / "tmpfile").write_text("var folders tmp\n")
            (self.vf / "C").mkdir()

    @property
    def extra(self):
        return {VAR_FOLDERS_NAME: self.vf} if self.vf is not None else None

    def seams(self, ctl: Control, **kw) -> EndSeams:
        self.notes: list[str] = []
        self.chowned: list[bytes] = []

        def chown(dirfd, name, uid, gid):
            self.chowned.append(name)
            end_mod.os_chown(dirfd, name, uid, gid)
        base = dict(cas=ctl.cas, freeze=lambda uid, b: True, kill=lambda uid, b: True, no_process=lambda uid: True,
                    now=lambda: NOW, chown=chown, root_uid=UID, root_gid=GID, note=self.notes.append,
                    owner_ok=mine, run_as=thread_run_as(), sleep=lambda s: None)
        base.update(kw)
        return EndSeams(**base)

    def pre(self):
        """The pre-freeze state, by an independent walk (os.walk + lstat), credential left out."""
        snap = snapshot(self.S)
        snap.pop("config/claude/.credentials.json", None)
        if self.vf is not None:
            snap[".levain-var-folders"] = ("dir", stat.S_IMODE(os.lstat(self.vf).st_mode))
            snap.update({f".levain-var-folders/{k}": v for k, v in snapshot(self.vf).items()})
        return snap

    def pre_manifest(self):
        return tree_manifest(self.S, extra=self.extra, credentials=self.facts.parked_paths, skip_credentials=True)

    def manifest_file(self):
        return [canon.parse(x) for x in (self.facts.retained / "manifest.jsonl").read_bytes().splitlines()]


def snapshot(root: Path) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in [*dirnames, *filenames]:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p), mode)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", mode)
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as fh:
                    out[rel] = ("file", fh.read(), mode)
            else:
                out[rel] = ("other", mode)
    return out


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    _unflag(tmp_path)


def _assert_retained(w: World, pre, pre_manifest, ctl: Control) -> None:
    f = w.facts
    assert ctl.state == RETAINED
    assert not f.live.exists() and (w.vf is None or not w.vf.exists())
    assert snapshot(f.tree) == pre                       # byte-equal, modes kept, links kept as links
    assert w.manifest_file() == pre_manifest
    assert (f.keep / ".credentials.json").read_bytes() == CRED
    assert sorted(os.listdir(f.keep)) == [".credentials.json"]
    assert not f.intent.exists()
    assert sorted(os.listdir(f.retained)) == ["manifest.jsonl", "meta.jsonl", "object.json", "retained_at", "tree"]
    assert not [n for n in os.listdir(f.alloc_dir) if n.endswith(".tmp")]
    assert stat.S_IMODE(os.lstat(f.retained).st_mode) == 0o700
    assert HELD not in [h[0] for h in ctl.history]


# --- the happy path ------------------------------------------------------------------------------


def test_end_retains_uncommitted(world):
    pre, pre_manifest = world.pre(), world.pre_manifest()
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED
    _assert_retained(world, pre, pre_manifest, ctl)
    tree = world.facts.tree
    assert (tree / "repo" / "README").read_text() == "committed\nAND AN UNCOMMITTED EDIT\n"
    assert (tree / "repo" / "untracked.txt").exists() and (tree / "repo" / "debug.log").exists()
    assert (tree / "repo" / "build" / "out.bin").read_bytes() == bytes(range(256)) * 4
    assert (tree / "repo" / ".git" / "HEAD").exists()
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    obj = canon.parse((world.facts.retained / "object.json").read_bytes())
    assert obj["format"] == "levain-retained-object/1" and obj["created_at"] == NOW
    assert obj["entries"] == len(pre_manifest) and obj["bytes"] == sum(r["size"] for r in pre_manifest)
    assert ctl.history[-1][1] == {"manifest": {"digest": obj["manifest_digest"], "entries": obj["entries"],
                                               "bytes": obj["bytes"]}}
    assert obj["manifest_digest"] == end_mod.digest_bytes((world.facts.retained / "manifest.jsonl").read_bytes())
    assert (world.facts.retained / "retained_at").read_text() == NOW + "\n"
    # every entry, and tree/ itself, went through the chown seam
    assert len(world.chowned) == len(pre_manifest) + 1


def test_retained_manifest_equals_prefreeze(world):
    pre_manifest = world.pre_manifest()
    by = {canon.parse(canon.canonical_bytes(r))["path_b64"]: r for r in pre_manifest}
    import base64
    names = {base64.b64decode(k).decode(): v for k, v in by.items()}
    assert names["repo/link-to-readme"]["type"] == "link"
    assert names["repo/link-to-readme"]["digest"] == end_mod.digest_bytes(b"README")
    assert names["repo/link-outside"]["digest"] == end_mod.digest_bytes(b"/etc/passwd")
    assert names["repo/secret.txt"]["mode"] == 0o600 and names["repo/private"]["mode"] == 0o700
    assert names["repo/private"]["size"] == 0 and names["repo/private"]["digest"] == ""
    assert names["repo/run.sh"]["mode"] == 0o755
    assert names[".levain-var-folders/T/tmpfile"]["type"] == "file"
    assert "config/claude/.credentials.json" not in names
    ctl = Control()
    end_session(world.facts, world.seams(ctl), record_state=REVOKED)
    assert world.manifest_file() == pre_manifest
    assert tree_manifest(world.facts.tree) == pre_manifest   # the retained tree itself hashes the same


def test_meta_captured_before_strip(world):
    ctl = Control()
    end_session(world.facts, world.seams(ctl), record_state=REVOKED)
    meta = [canon.parse(x) for x in (world.facts.retained / "meta.jsonl").read_bytes().splitlines()]
    import base64
    by = {base64.b64decode(m["path_b64"]).decode(): m for m in meta}
    if world.xattr:
        assert {"name": "user.levain.test", "value_b64": base64.b64encode(b"kept-in-meta").decode()} in \
            by["repo/README"]["xattrs"]
        listed = xattr_names(world.facts.tree / "repo" / "README")
        assert b"user.levain.test" not in listed           # stripped from the retained tree
    if DARWIN:
        assert by["repo/locked.txt"]["flags"] & UF_IMMUTABLE
        assert os.lstat(world.facts.tree / "repo" / "locked.txt").st_flags == 0   # uchg cleared first


@pytest.mark.skipif(not DARWIN, reason="macOS ACL / FinderInfo / BSD flags")
def test_uchg_ace_finderinfo_and_unstrippable_xattr_do_not_wedge_end(world, monkeypatch):
    f = world.S / "repo" / "decorated"
    f.write_text("x")
    subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(f)], check=True)
    subprocess.run(["/usr/bin/xattr", "-wx", "com.apple.FinderInfo", "54455854" + "00" * 28, str(f)], check=True)
    os.chflags(f, UF_IMMUTABLE)
    real = end_mod._darwin_libc()
    monkeypatch.setattr(real, "fremovexattr", lambda *a: -1)    # every xattr unstrippable
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED
    import base64
    meta = {base64.b64decode(m["path_b64"]).decode(): m
            for m in map(canon.parse, (world.facts.retained / "meta.jsonl").read_bytes().splitlines())}
    rec = meta["repo/decorated"]
    assert "deny" in rec["acl"] and rec["flags"] & UF_IMMUTABLE
    assert any(x["name"] == "com.apple.FinderInfo" for x in rec["xattrs"])
    assert any("not removed" in n for n in world.notes)
    assert os.lstat(world.facts.tree / "repo" / "decorated").st_flags == 0
    assert acl_text(world.facts.tree / "repo" / "decorated") is None


def test_hardlinks_inside_kept_outside_copied(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("shared with a path outside the tree\n")
    os.chmod(outside, 0o644)
    os.link(outside, w.S / "repo" / "hl-out")
    os.link(w.S / "repo" / "run.sh", w.S / "repo" / "hl-in")
    before = os.lstat(outside)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    after = os.lstat(outside)
    assert (after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode), after.st_nlink) == \
        (before.st_ino, before.st_uid, 0o644, 1)          # the outside file is no longer shared, untouched
    copy = os.lstat(w.facts.tree / "repo" / "hl-out")
    assert copy.st_ino != before.st_ino and (w.facts.tree / "repo" / "hl-out").read_text() == outside.read_text()
    a, b = os.lstat(w.facts.tree / "repo" / "run.sh"), os.lstat(w.facts.tree / "repo" / "hl-in")
    assert a.st_ino == b.st_ino and a.st_nlink == 2         # an in-tree pair is chowned in place


def test_link_copy_cap_refused_before_mutation(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("x" * 100)
    os.link(outside, w.S / "repo" / "hl-out")
    pre = snapshot(w.S)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED, caps=Caps(link_copy_bytes=99)) == HELD
    assert ctl.failed_step == "manifest_budget"
    pre.pop("config/claude/.credentials.json")
    now = snapshot(w.S)
    assert now == pre and not w.facts.tree.exists()


# --- crashes ---------------------------------------------------------------------------------------

RETAIN_POINTS = ([f"retain:{w}:{p}" for p in INTENT_PHASES["retain"] for w in ("before", "after")]
                 + ["retain:manifest:written", "retain:tree:renamed", "retain:tree:var-folders", "retain:tree:links",
                    "retain:tree:stripped", "retain:before:cas", "retain:after:cas"])
PARK_POINTS = ([f"park:{w}:{p}" for p in INTENT_PHASES["park"] for w in ("before", "after")]
               + ["park:before:cas", "park:after:cas"])


@pytest.mark.parametrize("point", RETAIN_POINTS + PARK_POINTS)
def test_crash_at_every_phase(world, point):
    """A crash at the point, then what a restart does: reconciliation from the journal and the
    filesystem only (helper start / reaper step 2), then the reaper continuing END from the record
    (step 3). Nothing lost; the final tree is byte-equal to the pre-END tree."""
    pre, pre_manifest = world.pre(), world.pre_manifest()
    ctl = Control()
    fired = []

    def fault(p):
        if p == point and not fired:
            fired.append(p)
            raise Crash(p)
    with pytest.raises(Crash):
        end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED)
    assert fired == [point]
    seams = world.seams(ctl)
    reconcile_session(world.facts, seams)
    assert end_session(world.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    _assert_retained(world, pre, pre_manifest, ctl)


def test_crash_mid_manifest_leaves_no_partial(world):
    """The manifest streams to a temp file: a crash before the rename leaves no manifest a later phase
    could trust."""
    ctl = Control()
    seen: list = []

    def fault(p):
        if p == "retain:manifest:written":
            os.unlink(world.facts.retained / "manifest.jsonl")   # as if the rename never happened
            partial = temp_path(world.facts.retained / "manifest.jsonl")
            partial.write_bytes(b"partial")
            seen.append(partial)
            raise Crash(p)
    with pytest.raises(Crash):
        end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED)
    pre_manifest = world.pre_manifest()
    seams = world.seams(ctl)
    reconcile_session(world.facts, seams)
    assert ctl.state == RETAINED and world.manifest_file() == pre_manifest
    assert seen and not seen[0].exists()                  # the dead writer's temp is swept under the lock


# --- a forged state record cannot steer root -------------------------------------------------------


def _trace(monkeypatch, needle: str, calls=("open", "stat", "lstat", "listdir", "scandir", "unlink", "rmdir",
                                            "rename", "replace"), builtin_open: bool = True) -> list:
    """Every call of ``os.<calls>`` (and builtin / ``io`` ``open``, which ``Path.read_bytes`` and ``Path.open``
    use) naming a path that holds ``needle``."""
    import builtins
    import io
    seen: list = []

    def wrap(real):
        def wrapped(*a, **k):
            for x in a[:2]:
                if isinstance(x, (str, bytes, os.PathLike)) and needle in os.fsdecode(x):
                    seen.append(x)
            return real(*a, **k)
        return wrapped
    for name in calls:
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    if builtin_open:
        monkeypatch.setattr(builtins, "open", wrap(builtins.open))
        monkeypatch.setattr(io, "open", wrap(io.open))
    return seen


def test_forged_intent_cannot_purge(world, monkeypatch):
    ctl = Control()
    end_session(world.facts, world.seams(ctl), record_state=REVOKED)
    retained = snapshot(world.facts.retained)
    forged = world.L / "sessions" / f"{SESSION}.json"
    forged.write_text(canon.canonical({"session": SESSION, "generation": 9, "state": "TRASH", "intent": "purge",
                                       "phase": "delete", "trashed_at": "2000-01-01T00:00:00Z",
                                       "failed_step": "commit", "artifact_acked": True}))
    seen = _trace(monkeypatch, "sessions")
    seams = world.seams(ctl)
    # (a) reconciliation never reads the record: nothing to finish, nothing touched
    assert reconcile_session(world.facts, seams) is None
    # (b) the reaper fed the forged state: END refuses a state it does not continue from ...
    with pytest.raises(Refused) as e:
        end_session(world.facts, seams, record_state="TRASH")
    assert e.value.name == "not-ending"
    # ... and fed a forged HELD it re-proves, finds the tree already retained (the filesystem's fact),
    # and sends RETAINED: no second retain, no journal left behind
    held = Control(HELD)
    assert end_session(world.facts, world.seams(held), record_state=HELD, failed_step="commit") == RETAINED
    obj = canon.parse((world.facts.retained / "object.json").read_bytes())
    assert held.history == [(RETAINED, {"manifest": {"digest": obj["manifest_digest"], "entries": obj["entries"],
                                                     "bytes": obj["bytes"]}})]
    assert not world.facts.intent.exists()
    # (c) a purge journal root wrote is session 5's: refused by name, the trash entry untouched
    write_root_intent(world.facts.intent, RootIntent("purge", "delete", NOW))
    with pytest.raises(Refused) as e:
        reconcile_session(world.facts, seams)
    assert e.value.name == "intent-unbuilt"
    # (d) a journal root does not own (the only way CONTROL could plant one) is refused, never run
    with pytest.raises(Refused) as e:
        reconcile_session(world.facts, world.seams(ctl, owner_ok=lambda st: False))
    assert e.value.name == "bad-root"
    os.unlink(world.facts.intent)
    assert snapshot(world.facts.retained) == retained
    assert seen == []                                    # no path under L/sessions was ever opened


# --- caps ------------------------------------------------------------------------------------------


def _untouched(w: World, pre_live) -> None:
    assert snapshot(w.S) == pre_live and not w.facts.tree.exists()
    assert not (w.facts.retained / "manifest.jsonl").exists()
    assert w.vf is None or w.vf.exists()


def test_end_byte_cap_refused(world):
    pre_live = {k: v for k, v in snapshot(world.S).items() if k != "config/claude/.credentials.json"}
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED, caps=Caps(manifest_bytes=300)) == HELD
    assert ctl.failed_step == "manifest_budget"
    assert any("manifest-byte-cap" in n for n in world.notes)
    _untouched(world, pre_live)
    assert read_root_intent(world.facts.intent, owner_ok=mine).phase == "manifest"
    # meta has its own V17 cap
    ctl2 = Control(HELD)
    big = lambda e, note: {"flags": 1, "xattrs": [{"name": "user.x", "value_b64": "A" * 64}]}  # noqa: E731
    assert end_session(world.facts, world.seams(ctl2, meta_reader=big), record_state=HELD,
                       failed_step="manifest_budget", caps=Caps(meta_bytes=500)) == HELD
    assert any("meta-byte-cap" in n for n in world.notes)
    assert ctl2.failed_step == "manifest_budget" and snapshot(world.S) == pre_live
    # force-retire (a): END again with larger caps retains the whole tree
    pre, pre_manifest = world.pre(), world.pre_manifest()
    assert end_session(world.facts, world.seams(ctl), record_state=HELD, failed_step="manifest_budget") == RETAINED
    ctl.history = [h for h in ctl.history if h[0] != HELD]
    _assert_retained(world, pre, pre_manifest, ctl)


def test_end_inode_and_time_caps_refused(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    pre_live = {k: v for k, v in snapshot(w.S).items() if k != "config/claude/.credentials.json"}
    n = len(w.pre_manifest())
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED, caps=Caps(inodes=n - 1)) == HELD
    assert ctl.failed_step == "manifest_budget" and any("manifest-inode-cap" in x for x in w.notes)
    _untouched(w, pre_live)
    ticks = iter(range(10_000))
    assert end_session(w.facts, w.seams(ctl, clock=lambda: float(next(ticks))), record_state=HELD,
                       failed_step="manifest_budget", caps=Caps(seconds=5)) == HELD
    assert any("manifest-time-cap" in x for x in w.notes)
    _untouched(w, pre_live)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="manifest_budget",
                       caps=Caps(inodes=n)) == RETAINED   # exactly at the cap ends normally


def _chain(base: Path, depth: int) -> None:
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    for name in ["deep"] + ["d"] * (depth - 1):    # openat all the way down: PATH_MAX never limits it
        os.mkdir(name, dir_fd=fd)
        nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    os.close(fd)


def _open_fds() -> set[int]:
    """The process's open fds from /dev/fd, each confirmed by fstat (so the listing's own fd is not counted)."""
    used = set()
    for n in os.listdir("/dev/fd"):
        try:
            os.fstat(int(n))
            used.add(int(n))
        except OSError:
            pass
    return used


def _fd_users() -> str:
    """What else holds or takes fds in this process, for a failure message under a squeezed limit."""
    try:
        fds = sorted(_open_fds())
    except OSError as e:
        fds = [f"unlistable: {e}"]
    others = sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())
    return f"other threads alive: {others}; open fds: {fds}"


def _fd_targets(fds) -> list[str]:
    """What each fd is (its /proc link on Linux, its path on macOS), for a failure message."""
    out = []
    for fd in sorted(fds):
        try:
            if sys.platform == "darwin":
                import fcntl
                what = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
            else:
                what = os.readlink(f"/proc/self/fd/{fd}")
        except OSError as e:
            what = f"? {e}"
        out.append(f"{fd}={what}")
    return out


def _fd_limit(free: int) -> int:
    """The soft RLIMIT_NOFILE that leaves exactly ``free`` fd numbers open to new fds at the moment it is
    computed (the open fds counted by :func:`_open_fds`); skipped past the hard limit."""
    used = _open_fds()
    lim = got = 0
    while got < free:
        if lim not in used:
            got += 1
        lim += 1
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < lim:
        pytest.skip(f"RLIMIT_NOFILE hard {hard} < {lim}")
    return lim


@contextmanager
def _nofile(free: int):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (_fd_limit(free), hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


class _WalkFds:
    """``end_mod.os`` for one walk, counting the fds END's own code holds: each ``os.open``'s fd until its
    ``os.close``, plus the one ``os.listdir(fd)`` duplicates for as long as the listing runs. Only END's calls are
    counted, so another thread's fds in the same process never enter the peak."""

    def __init__(self) -> None:
        self.held: set[int] = set()
        self.peak = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, *a, **k):
        fd = os.open(*a, **k)
        self.held.add(fd)
        self.peak = max(self.peak, len(self.held))
        return fd

    def close(self, fd):
        os.close(fd)
        self.held.discard(fd)

    def listdir(self, path):
        if isinstance(path, int):
            self.peak = max(self.peak, len(self.held) + 1)
        return os.listdir(path)


# DECISION: free fd numbers left beside a measured need under a squeezed RLIMIT_NOFILE, for what else the process
# opens meanwhile (END's own fds outside the walk, and whatever else a full suite's process holds or takes: on
# Linux CI the walk with zero to spare raised EMFILE, and END with eight to spare failed at ``manifest``). The
# exact bound is the measured peak's assertion, not this; the squeezed limit proves only that the walk does not
# grow with depth, so each chain below is asserted deeper than the limit it runs under.
FD_SPARE = 32
CHAIN = 150


def test_walk_fd_peak_is_bounded(world, monkeypatch):
    """Reproduced red on the r1 tip fa9251c0 (codex r1 MED): the walk pruned an ancestor only after opening and
    listing the child, so its peak was two past WALK_OPEN_DIRS, and with exactly WALK_PEAK_FDS fds free it raised
    EMFILE part-way down a chain. It now prunes first. The peak is MEASURED (END's own opens, closes and
    listings, counted) and must not pass WALK_PEAK_FDS; then the whole tree walks under a real limit far below the
    chain's depth."""
    _chain(world.S / "repo", CHAIN)
    _chain(world.vf / "T", CHAIN)                   # under .levain-var-folders, the frame opened by path
    count = _WalkFds()
    with monkeypatch.context() as m:
        m.setattr(end_mod, "os", count)
        expected = len(list(end_mod.walk(world.S, world.extra)))
    assert not count.held, f"the walk left {sorted(count.held)} open"
    assert count.peak <= end_mod.WALK_PEAK_FDS, f"measured peak {count.peak} > WALK_PEAK_FDS"
    free = end_mod.WALK_PEAK_FDS + FD_SPARE
    assert CHAIN > free
    err = None
    pre = _open_fds()
    with _nofile(free):
        try:
            got = len(list(end_mod.walk(world.S, world.extra)))
        except OSError as e:
            err = f"{e}; {_fd_users()}"      # while the limit is still squeezed
    if err is not None:
        # under the squeeze /dev/fd itself may be unlistable (CI on the 1026+23 landing chain), so the fds are
        # listed again with the limit restored, each named, those opened since the squeeze began marked "+"
        now = _open_fds()
        err += (f"; {len(pre)} fds open before the squeeze; after it: "
                f"{[('+' if fd not in pre else '') + w for fd, w in zip(sorted(now), _fd_targets(now))]}")
    assert err is None, f"{err} with {free} fds free (measured peak {count.peak})"
    assert got == expected


def test_end_deep_chain_holds_bounded_fds_and_refuses_past_depth_cap(world):
    """A slot-built directory chain deeper than the fd limit (reproduced on main 42585459: a 2000-deep chain at
    RLIMIT_NOFILE 1024, and a 300-deep one at macOS's default 256, made the manifest walk raise EMFILE, so END
    landed HELD at the bare ``manifest`` phase and failed the same way on every pass). The walk now holds a
    bounded number of fds, so the chain is retained under a low limit; past the depth cap it is a budget
    refusal (HELD ``manifest_budget``, nothing mutated), whose exit is END again with a larger cap. That pass
    failing after the rename, then a pass at a cap SMALLER than the chain, still retains it (reproduced on the
    r1 tip fa9251c0, complement r1 MED 1: the second pass refused ``manifest-depth-cap`` on the renamed tree):
    every walk after the manifest takes the depth the manifest accepted. The chain is under
    ``.levain-var-folders``, whose own frame is opened by path, not from the root's fd."""
    _chain(world.vf / "T", CHAIN)
    pre_live = {k: v for k, v in snapshot(world.S).items() if k != "config/claude/.credentials.json"}
    pre, pre_manifest = world.pre(), world.pre_manifest()
    ctl = Control()

    def fault(p):
        if p == "retain:tree:renamed":
            raise OSError(errno.EIO, "a transient failure after the rename")
    # No fd squeeze here: the fd bound is proven by the measurement in test_walk_fd_peak_is_bounded; this test
    # proves only the depth-cap refusal and the retained exit.
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED, caps=Caps(depth=100)) == HELD
    assert ctl.failed_step == "manifest_budget" and any("manifest-depth-cap" in n for n in world.notes), \
        f"{ctl.failed_step}; notes {world.notes[-3:]}"
    _untouched(world, pre_live)
    assert end_session(world.facts, world.seams(ctl, fault=fault), record_state=HELD,
                       failed_step="manifest_budget", held_attempts=1, caps=Caps(depth=200)) == HELD
    assert ctl.failed_step == "tree" and world.facts.tree.exists() and not world.S.exists()
    assert end_session(world.facts, world.seams(ctl), record_state=HELD, failed_step="tree", held_attempts=2,
                       caps=Caps(depth=100)) == RETAINED
    ctl.history = [h for h in ctl.history if h[0] != HELD]
    _assert_retained(world, pre, pre_manifest, ctl)


def _node_modules_tree(root: Path, packages: int, targets: int) -> None:
    nm = root / "node_modules"
    for i in range(packages):
        p = nm / f"pkg-{i:03d}"
        (p / "lib").mkdir(parents=True)
        (p / "package.json").write_text(f'{{"name":"pkg-{i}","version":"1.0.{i}"}}')
        (p / "index.js").write_text(f"module.exports = require('./lib/a');// {i}\n")
        (p / "lib" / "a.js").write_text("exports.a = 1;\n" * 8)
        (p / "lib" / "b.js").write_text("exports.b = 2;\n" * 8)
        dep = p / "node_modules" / "dep"
        dep.mkdir(parents=True)
        (dep / "index.js").write_text("module.exports = 0;\n")
        os.symlink("../../index.js", dep / "parent-link")
    t = root / "target" / "debug" / "deps"
    t.mkdir(parents=True)
    for i in range(targets):
        (t / f"crate_{i}.rlib").write_bytes(bytes([i % 256]) * 64)


def test_end_node_modules_class_tree(tmp_path):
    """SCALE 1:1000. V1's 2,000,000 inodes / V2's 600 s / V17's 512 MiB + 64 MiB become 2,000 inodes,
    0.6 s and ~512 KiB + ~64 KiB (``Caps().scaled(1/1000)``, real clock); the tree is a `node_modules`
    + `target/` class tree of ~1,000 entries, standing for the plan's "several hundred thousand"
    (plan §2k 3) at the same ratio to the cap. It ends normally; the same tree past the inode cap is
    refused by name before any mutation.

    On a macOS test host every file this process makes carries ``com.apple.provenance``, which puts
    a meta line on every entry (~110 B) and passes V17's meta cap at this scale (see
    ``test_provenance_on_every_entry_passes_meta_cap``); the tree run here filters that one
    xattr out of the reader so it measures the inode / time / manifest caps it is about."""
    caps = Caps().scaled(1 / 1000)
    assert (caps.inodes, caps.manifest_bytes, caps.meta_bytes) == (2000, (512 << 20) // 1000, (64 << 20) // 1000)
    w = World(tmp_path, var_folders=False, uchg=False)
    _node_modules_tree(w.S / "repo", packages=80, targets=300)
    n = len(w.pre_manifest())
    assert 900 <= n <= caps.inodes
    pre, pre_manifest = w.pre(), w.pre_manifest()
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, meta_reader=_no_provenance), record_state=REVOKED,
                       caps=caps) == RETAINED
    _assert_retained(w, pre, pre_manifest, ctl)

    w2 = World(tmp_path / "over", var_folders=False, uchg=False)
    _node_modules_tree(w2.S / "repo", packages=80, targets=1200)
    assert len(w2.pre_manifest()) > caps.inodes
    ctl2 = Control()
    assert end_session(w2.facts, w2.seams(ctl2), record_state=REVOKED, caps=caps) == HELD
    assert ctl2.failed_step == "manifest_budget" and any("manifest-inode-cap" in x for x in w2.notes)
    assert w2.S.exists() and not w2.facts.tree.exists()


def _no_provenance(e, note):
    rec = end_mod.default_meta_reader()(e, note)
    xs = [x for x in rec.get("xattrs", []) if x["name"] != "com.apple.provenance"]
    rec = {k: v for k, v in rec.items() if k != "xattrs"}
    return {**rec, "xattrs": xs} if xs else rec


def test_provenance_on_every_entry_passes_meta_cap(tmp_path):
    """A CONTRACT finding, pinned: when every entry carries an xattr (macOS ``com.apple.provenance`` on
    this host), meta costs ~110 B per entry, so V17's 64 MiB meta cap holds ~600,000 entries, not V1's
    2,000,000. END refuses it BY NAME before any mutation (never truncates meta); the exit is
    force-retire's larger caps. Simulated with a reader that gives every entry one such xattr."""
    w = World(tmp_path, var_folders=False, uchg=False)
    _node_modules_tree(w.S / "repo", packages=80, targets=300)
    one = lambda e, note: {"xattrs": [{"name": "com.apple.provenance", "value_b64": "AQIANGshcIylgIU="}]}  # noqa: E731
    caps = Caps().scaled(1 / 1000)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, meta_reader=one), record_state=REVOKED, caps=caps) == HELD
    assert ctl.failed_step == "manifest_budget" and any("meta-byte-cap" in x for x in w.notes)
    assert w.S.exists() and not w.facts.tree.exists()
    assert end_session(w.facts, w.seams(ctl, meta_reader=one), record_state=HELD, failed_step="manifest_budget",
                       caps=replace(caps, meta_bytes=caps.meta_bytes * 4)) == RETAINED


# --- freeze / kill / HELD --------------------------------------------------------------------------


@pytest.mark.parametrize("seam,failed", [
    # reaper H1: an unproven freeze no longer stops END; the kill runs, and only its failure lands HELD, still named
    # "freeze" while no FROZEN edge was sent.
    ({"freeze": lambda uid, b: False, "kill": lambda uid, b: False}, "freeze"),
    ({"kill": lambda uid, b: False}, "kill"),
    ({"no_process": lambda uid: False}, "kill"),          # killed, but a process of the uid remains
])
def test_kill_failure_held(world, seam, failed):
    pre_live = snapshot(world.S)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, **seam), record_state=REVOKED) == HELD
    assert ctl.history[-1] == (HELD, {"failed_step": failed, "held_attempts": 1})
    assert snapshot(world.S) == pre_live                  # tree untouched, credential not moved
    assert os.listdir(world.facts.keep) == [] and not world.facts.intent.exists()
    assert not world.facts.retained.exists()
    assert world.vf.exists()


def test_bound_passed_is_not_proven(world):
    now = [0.0]

    def slow_kill(uid, b):                                       # the kill seam returns after 11 s (V12 = 10 s)
        now[0] += 11.0
        return True
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, clock=lambda: now[0], kill=slow_kill),
                       record_state=REVOKED) == HELD
    assert ctl.failed_step == "kill"


def test_unbuilt_seams_hold(world):
    ctl = Control()
    seams = world.seams(ctl)
    seams.freeze, seams.kill = end_mod.unbuilt_freeze, end_mod.unbuilt_kill
    assert end_session(world.facts, seams, record_state=REVOKED) == HELD
    assert ctl.failed_step == "freeze"
    # reaper H1: a freeze that cannot prove (here: unbuilt) does not keep END from its kill; the kill and its proof
    # pass, so FROZEN is sent after them, before DEAD.
    edges_before = len(ctl.history)
    seams = world.seams(ctl)
    seams.freeze = end_mod.unbuilt_freeze
    assert end_session(world.facts, seams, record_state=HELD, failed_step="freeze") == RETAINED
    assert [h[0] for h in ctl.history[edges_before:]] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]


_RETAIN_STEP_FNS = {"init": "_init_step", "manifest": "_manifest_step", "meta": "_meta_step",
                    "object": "_object_step", "tree": "_tree_step", "commit": "_commit_step"}


@pytest.mark.parametrize("failed", ["freeze", "kill", "park", *INTENT_PHASES["retain"]])
def test_held_retried_each_step(world, monkeypatch, failed):
    """Each END step fails once -> HELD with that ``failed_step``; the retry resumes from that step
    idempotently, sends only the edges from there on, and retains the whole tree."""
    pre, pre_manifest = world.pre(), world.pre_manifest()
    ctl = Control()
    kw: dict = {}
    if failed == "freeze":
        kw["freeze"] = kw["kill"] = lambda uid, b: False          # H1: an unproven freeze alone is not HELD
    elif failed == "kill":
        kw["kill"] = lambda uid, b: False
    else:
        name = "park_move" if failed == "park" else _RETAIN_STEP_FNS[failed]
        real = getattr(end_mod, name)
        calls = {"n": 0}

        def once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EIO, f"{failed} fails once")
            return real(*a, **k)
        monkeypatch.setattr(end_mod, name, once)
    assert end_session(world.facts, world.seams(ctl, **kw), record_state=REVOKED) == HELD
    assert ctl.failed_step == failed
    edges_before = len(ctl.history)
    assert end_session(world.facts, world.seams(ctl), record_state=HELD, failed_step=failed) == RETAINED
    sent = [h[0] for h in ctl.history[edges_before:]]
    full = [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    assert sent == full[{"freeze": 0, "kill": 1, "park": 2}.get(failed, 3):]
    ctl.history = [h for h in ctl.history if h[0] != HELD]
    _assert_retained(world, pre, pre_manifest, ctl)


def test_never_cloned_retains_nothing(tmp_path):
    w = World(tmp_path, git=False, var_folders=False, uchg=False, cloned=False)
    shutil.rmtree(w.S / "repo")
    (w.S / "repo").mkdir()                                 # start made it empty; the clone never ran (no marker)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    # nothing retained, nothing deleted (who removes live/<s> of a never-cloned session is SCRUBBED's, 2f)
    assert not w.facts.retained.exists() and w.S.exists()
    assert (w.facts.keep / ".credentials.json").read_bytes() == CRED   # a login session parks its credential


# --- keep / park -----------------------------------------------------------------------------------


def test_keep_holds_only_credential(world, monkeypatch):
    codex_dir = world.S / "config" / "codex" / "auth.json"
    codex_dir.mkdir()                                     # a directory at a credential path is work, not a credential
    (codex_dir / "note").write_text("x")
    opened = _trace(monkeypatch, ".credentials.json", calls=("open",))
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED
    during = list(opened)
    assert during == []                                   # moved by rename (dir fds), never opened
    assert sorted(os.listdir(world.facts.keep)) == [".credentials.json"]
    assert (world.facts.keep / ".credentials.json").read_bytes() == CRED
    assert not (world.facts.tree / "config" / "claude" / ".credentials.json").exists()
    assert (world.facts.tree / "config" / "codex" / "auth.json" / "note").exists()
    import base64
    paths = {base64.b64decode(r["path_b64"]) for r in world.manifest_file()}
    assert b"config/claude/.credentials.json" not in paths and b"config/codex/auth.json/note" in paths


def test_park_never_follows_a_planted_link(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / ".credentials.json").write_text("someone else's")
    shutil.rmtree(w.S / "config" / "claude")
    os.symlink(decoy, w.S / "config" / "claude")
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert os.listdir(w.facts.keep) == [] and (decoy / ".credentials.json").exists()


def test_none_class_parks_nothing(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    (w.S / "config" / "claude" / ".credentials.json").unlink()
    facts = EndFacts(SESSION, SlotId("none", 2), UID, GID, w.L, lease_seq=LEASE_SEQ, child_uid=UID,
                     vendor="claude", auth="none")
    slot_state(w.L, facts.slot)
    end_mod.mark_cloned(w.L, SESSION, now=lambda: NOW)
    ctl = Control()
    assert end_session(facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert not (w.L / "slots" / "v2" / "keep").exists()


def test_keep_shape_check(tmp_path):
    k = tmp_path / "keep"
    k.mkdir()
    (k / "ok").write_bytes(b"x" * end_mod.V14_BYTES)
    assert keep_shape_ok(k, "ok", UID) == (True, "")
    assert keep_shape_ok(k, "ok", UID + 1)[0] is False                      # not the slot's
    (k / "big").write_bytes(b"x" * (end_mod.V14_BYTES + 1))
    assert keep_shape_ok(k, "big", UID) == (False, f"{end_mod.V14_BYTES + 1} bytes > {end_mod.V14_BYTES}")
    os.mkfifo(k / "fifo")
    assert keep_shape_ok(k, "fifo", UID) == (False, "not a regular file")
    os.symlink("ok", k / "link")
    assert keep_shape_ok(k, "link", UID) == (False, "not a regular file")
    os.link(k / "ok", k / "two")
    assert keep_shape_ok(k, "two", UID) == (False, "2 links")
    assert keep_shape_ok(k, "absent", UID) == (False, "absent")


# --- the reserve -----------------------------------------------------------------------------------


def test_reserve_releases_on_enospc(tmp_path, world, monkeypatch):
    r = Reserve(tmp_path / "reserve", size=8 * 1024)
    assert r.refill(lambda: 10 << 30) is True
    assert (tmp_path / "reserve").read_bytes() == bytes(8 * 1024)           # written zeros
    fails = {"n": 2}

    def write():
        if fails["n"]:
            fails["n"] -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
    r.retry(write)
    assert r.current() == 8 * 1024 - 2 * 1024                              # two V6/8 steps given back
    always = lambda: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))  # noqa: E731
    with pytest.raises(OSError):
        r.retry(always)                                                    # nothing left: the error stands
    assert r.current() == 0
    assert r.refill(lambda: 1 << 30) is False and r.current() == 0          # below the V5 floor: no refill
    assert r.refill(lambda: 9 << 30) is True and r.current() == 8 * 1024

    # END's transition writes go through it: one ENOSPC on the manifest write costs one step, not the END
    real = end_mod._stream_jsonl
    hit = {"n": 0}

    def full_once(path, *a, **k):
        if path.name == "manifest.jsonl" and not hit["n"]:
            hit["n"] = 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, *a, **k)
    monkeypatch.setattr(end_mod, "_stream_jsonl", full_once)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, reserve=r), record_state=REVOKED) == RETAINED
    assert hit["n"] == 1 and r.current() == 8 * 1024 - 1024


# --- facts come from alloc -------------------------------------------------------------------------


def test_facts_from_alloc(tmp_path):
    alloc = {"session": SESSION, "slot": {"class": "egress", "index": 3}, "slot_uid": 3996, "slot_gid": 3996,
             "kind": "interactive", "vendor": "claude", "auth": "passthrough", "lease_seq": 4}
    f = facts_from_alloc(alloc, store=tmp_path)
    assert (f.live, f.tree, f.keep, f.intent) == (
        tmp_path / "live" / SESSION, tmp_path / "retained" / SESSION / "tree", tmp_path / "slots" / "e3" / "keep",
        tmp_path / "alloc" / f"{SESSION}.intent.json")
    assert (f.lease_seq, f.child_uid, f.vendor, f.auth, f.uids) == (4, None, "claude", "passthrough", (3996,))
    v = facts_from_alloc({**alloc, "slot": {"class": "none", "index": 1}, "child_uid": 3981, "auth": "none"},
                         store=tmp_path)
    assert v.uids == (3996, 3981) and not v.parks
    for bad in ({**alloc, "slot_uid": 0}, {**alloc, "session": "../x"}, {k: v for k, v in alloc.items() if k != "slot"},
                {**alloc, "slot": {"class": "egress", "index": 99}}, {**alloc, "child_uid": 3981},
                {k: v for k, v in alloc.items() if k != "lease_seq"}, {**alloc, "lease_seq": True},
                {k: v for k, v in alloc.items() if k != "auth"}):
        with pytest.raises(Refused):
            facts_from_alloc(bad, store=tmp_path)
