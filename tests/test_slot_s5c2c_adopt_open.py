"""S1 session 5 piece 5c-ii-c session 1: ``adopt-open`` wired end to end (DESIGN §1 steps 1-12, §4.1, §4.5, §5b).

Every row runs the REAL per-run image (hdiutil, unprivileged: R4), the REAL walker, the REAL importer module run by
its root-tree entry script (``root_tree.entry_text``, the 3.9 floor interpreter) and the REAL publish into a tmp
``L/export/operator/``, as this uid. The seams stand in for root only: the export uid is this uid, the domain proof
kills the importer's process group (no ``end_domain`` of a real uid), the launch skips ``launchctl asuser``, the
stage and ``sandbox-exec`` (no drop, no profile applied), and the walker trusts this uid's tree. Those are 5g rows.

Built from the first run of the wiring (``project_memory/1022-19/adopt_cc/run_s1.txt``); steps 1-5 had never run.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import end
from levain.slot import export_run as ER
from levain.slot import install_helper as IH
from levain.slot import proc as P
from levain.slot import records as R
from levain.slot import root_tree
from levain.slot import runtime_helper as RH
from levain.slot import walker as WK
from levain.slot.ids import SlotId

S = "s-" + "c2" * 8
UID = os.getuid()
GIT = shutil.which("git")
WT = Path(__file__).resolve().parents[1]
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or GIT is None or not os.path.exists(ER.HDIUTIL),
                                reason="the seam run needs macOS hdiutil and a git")


def _git(*args, cwd=None):
    return subprocess.run([GIT, *args], cwd=cwd, env=ENV, check=True, capture_output=True).stdout.decode().strip()


# --- the fixture: a store with one retained, scrubbed session whose alloc records its base -------------------------

def make_repo(tmp: Path, kind: str) -> tuple:
    """(base oid, base bundle path, the session repo dir). ``kind``: ``delta`` (a clone of the base bundle, one
    commit past it), ``unrelated`` (its own history, none of the base's), ``nothing-new`` (the base's files, no
    ``.git``)."""
    base = tmp / "base"
    _git("init", "-q", "-b", "main", str(base))
    (base / "README").write_text("base\n")
    _git("add", ".", cwd=base)
    _git("commit", "-qm", "base", cwd=base)
    oid = _git("rev-parse", "main", cwd=base)
    bundle = tmp / "base.bundle"
    _git("bundle", "create", "-q", str(bundle), "HEAD", "main", cwd=base)
    repo = tmp / "repo"
    if kind == "delta":
        _git("clone", "-q", str(bundle), str(repo))
        (repo / "a.txt").write_text("a\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "one", cwd=repo)
    elif kind == "unrelated":
        _git("init", "-q", "-b", "main", str(repo))
        (repo / "other.txt").write_text("other\n")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "other", cwd=repo)
    else:
        repo.mkdir()
        (repo / "README").write_text("base\n")
    return oid, bundle, repo


def make_store(tmp: Path, kind: str) -> Path:
    L = tmp / "L"
    for d in ("alloc", "slots/e3", "retained", "trash", "export/operator", "export/dispatch", "policy"):
        (L / d).mkdir(parents=True)
    (L / "policy" / "operator").write_text(IH.operator_record_text("operator", UID))
    oid, bundle, repo = make_repo(tmp, kind)
    r = L / "retained" / S
    tree = r / "tree"
    shutil.copytree(repo, tree / "repo", symlinks=True)
    (tree / "in").mkdir()
    shutil.copyfile(bundle, tree / "in" / "repo.bundle")
    end._stream_jsonl(r / "manifest.jsonl", end.iter_manifest(tree, caps=end.Caps(depth=end.MANIFEST_DEPTH)),
                      1 << 40, "manifest")
    recs = [canon.parse(line) for line in (r / "manifest.jsonl").read_bytes().splitlines()]
    (r / "object.json").write_bytes(canon.canonical_bytes({
        "format": "levain-retained-object/1", "session": S, "depth": end.MANIFEST_DEPTH, "entries": len(recs),
        "bytes": sum(x["size"] for x in recs)}))
    (r / "retained_at").write_bytes(b"2026-10-01T00:00:00Z\n")
    data = bundle.read_bytes()
    base = R.AllocBase(oid=oid, ref_b64=base64.b64encode(b"refs/heads/main").decode(), object_format="sha1",
                       bundle_sha256="sha256:" + hashlib.sha256(data).hexdigest(), bundle_bytes=len(data))
    slot = SlotId("egress", 3)
    a = R.Alloc(session=S, slot=slot, lease_seq=7, slot_rev=12, slot_uid=3990, slot_gid=3990, kind="interactive",
                vendor="claude", auth="brokered", operator_uid=UID, supervisor_uid=UID, staging_uid=UID,
                created_at="2026-10-01T00:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f",
                policy=R.AllocPolicy(routes=("anthropic", "github"), egress_routes=("github",), queue_grant=True,
                                     hooks=frozenset({"levain_gate", "levain_queue"}), incarnation=1,
                                     front=R.Front(uid=3989, pending=True)),
                base=base)
    (L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(a))
    (L / "slots" / slot.tag / "state").write_bytes(R.slot_state_bytes(R.SlotState(
        slot=slot, rev=12, lease_seq=7, state="scrubbed", dispatch_eligible=False, changed_at="2026-10-01T00:00:00Z")))
    return L


# --- the root seams ------------------------------------------------------------------------------------------------

class GroupProver:
    """The export domain, as this uid can prove it: every process group a spawn made (the child ``setsid``s, so the
    importer and anything it forks share one), SIGKILLed and polled until ``killpg(pg, 0)`` finds none; the caller's
    unreaped children reaped. ``fail_at``: the call (1 = seal-in, 2 = seal-out) that answers unproven."""

    def __init__(self, log, fail_at: int = 0) -> None:
        self.pgs: set = set()
        self.log, self.fail_at, self.calls = log, fail_at, 0
        self.children: list = []        # the children each call was handed, in call order

    def __call__(self, children):
        self.calls += 1
        self.children.append(tuple(children))
        for pg in self.pgs:
            try:
                os.killpg(pg, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for c in children:
            try:
                os.kill(c, signal.SIGKILL)
                os.waitpid(c, 0)
            except ChildProcessError:
                pass
            except ProcessLookupError:
                pass
        end_t = time.monotonic() + 5
        while time.monotonic() < end_t:
            alive = [pg for pg in self.pgs if _pg_alive(pg)]
            if not alive:
                break
            time.sleep(0.02)
        else:
            return P.Domain(False, "processes-remain", repr(alive))
        self.log(f"prove {self.calls}")
        if self.calls == self.fail_at:
            return P.Domain(False, "processes-remain", "seam")
        return P.Domain(True)


def _pg_alive(pg: int) -> bool:
    try:
        os.killpg(pg, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class QuietWatchdog:
    """macOS: the footprint watchdog samples EVERY process of its uid; as this uid that is the whole desktop. 5g."""

    killed = False
    blind = False

    def __init__(self, uid, cap, end_, *, log) -> None:
        self.log = log

    def start(self) -> None:
        self.log("watchdog start")

    def stop(self, timeout: float = 0) -> None:
        self.log("watchdog stop")


def entry_script(tmp: Path) -> Path:
    """The real ``export-importer`` entry (``root_tree.entry_text``) over a tree whose ``current`` is this worktree."""
    tree = tmp / "import-tree"
    tree.mkdir(exist_ok=True)
    if not (tree / "current").exists():
        os.symlink(str(WT), tree / "current")
    path = tmp / "export-importer"
    path.write_text(root_tree.entry_text("darwin", "export-importer", tree=tree))
    os.chmod(path, 0o755)
    return path


def seam_exec(argv, spec_fd, cgroup=None):
    """The child's last act with no stage: the spec fd closed (the stage reads and closes it), an empty env."""
    os.close(spec_fd)
    os.execve(argv[0], list(argv), {})


class Rig:
    """One store + the seams; ``run()`` answers ``(exit, stdout line, stderr)`` of ``runtime_helper.main``."""

    def __init__(self, tmp: Path, kind: str = "delta", *, argv_wrap=None, fail_prove_at: int = 0,
                 fault=None, image_run=None) -> None:
        self.tmp, self.L = tmp, make_store(tmp, kind)
        self.image_run = image_run      # None: the real hdiutil runner
        self.fault = fault or (lambda point: None)
        self.log: list = []
        self.prover = GroupProver(self.log.append, fail_prove_at)
        self.entry = entry_script(tmp)
        self.argv_wrap = argv_wrap
        self.pids: list = []
        self.hooks: dict = {}
        self.times: dict = {}

    def at(self, step: str) -> None:
        self.log.append(step)
        self.times[step] = time.monotonic()
        hook = self.hooks.get(step)
        if hook is not None:
            hook()

    def launch(self, ctx, args, export_class):
        argv = (str(self.entry),) + tuple(args)
        if self.argv_wrap is not None:
            argv = self.argv_wrap(argv)
        return RH.ExportCmd(argv, b"{}", cwd=ER.mount_path(str(self.L / "export"), "importer"), exec_child=seam_exec)

    def spawn(self, cmd, **kw):
        pid = RH.spawn_export(cmd, **kw)
        self.prover.pgs.add(pid)
        self.pids.append(pid)
        self.log.append("spawned")
        return pid

    def seal(self, facts):
        return ER.SealSeams(prove=self.prover, watchdog=QuietWatchdog, log=self.log.append,
                            image=(ER.ImageSeams(log=self.log.append) if self.image_run is None
                                   else ER.ImageSeams(log=self.log.append, run=self.image_run)),
                            publish=ER.PublishSeams(log=self.log.append, fault=self.fault))

    def seams(self) -> RH.Seams:
        return RH.Seams(host="darwin", store=self.L, euid=lambda: 0, trusted=lambda st: True,
                        account_uid=lambda name: UID, at=self.at,
                        adopt_run=RH.AdoptRunSeams(seal=self.seal, launch=self.launch, spawn=self.spawn,
                                                   walk_seams=lambda: WK.WalkSeams(owner_uid=UID),
                                                   publish_gid=lambda ctx, k: None))

    def run(self):
        import io
        out, err = io.StringIO(), io.StringIO()
        rc = RH.main(["adopt-open", S], stdin=b"", env={"SUDO_UID": str(UID)}, seams=self.seams(), out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def cleanup(self) -> None:
        mnt = ER.mount_path(str(self.L / "export"), "importer")
        if os.path.ismount(mnt):
            subprocess.run([ER.HDIUTIL, "detach", "-quiet", "-force", mnt], check=False)
        for pg in self.prover.pgs:
            try:
                os.killpg(pg, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --- the rows ------------------------------------------------------------------------------------------------------

#: The order DESIGN §1 states, as the log's subsequence: lock -> seal-in -> spawn -> walk -> close -> line -> seal-out
#: -> classify -> publish (detach before the commit) -> teardown -> adopt unlock -> session unlock.
ORDER = ["adopt:2", "adopt:4", "adopt:5", "adopt:6", "seal-in done", "adopt:7", "spawned", "adopt:8",
         "adopt:frame-closed", "adopt:10", "adopt:11", "seal-out domain proven empty", "adopt:classify",
         "adopt:publish", "fsync staged", "image detached", "noreplace", "adopt:12", "adopt:teardown",
         "adopt:adopt-unlocked", "adopt:session-unlocked"]


def _subsequence(want, log) -> bool:
    it = iter(log)
    return all(any(x == w for x in it) for w in want)


def _answer(line: str) -> dict:
    return canon.parse(line.strip().encode())


@pytest.fixture
def rigs(tmp_path):
    made = []

    def make(kind="delta", **kw):
        r = Rig(tmp_path / f"r{len(made)}", kind, **kw)
        made.append(r)
        return r
    (tmp_path / "r0").mkdir()
    for i in range(1, 4):
        (tmp_path / f"r{i}").mkdir()
    yield make
    for r in made:
        r.cleanup()


@pytest.mark.parametrize("kind,result,refs", [("delta", "delta", ["head"]), ("unrelated", "unrelated", ["head"]),
                                              ("nothing-new", "adopt-nothing-new", [])])
def test_adopt_open_publishes_root_copies_and_answers_from_root_facts(rigs, kind, result, refs):
    rig = rigs(kind)
    seen = {}

    def snap():
        out = rig.L / "export" / ".work" / "out"
        seen.update({n: os.lstat(out / n).st_ino for n in ("bundle", "report.json") if (out / n).exists()})
    rig.hooks["adopt:publish"] = snap
    rc, line, err = rig.run()
    assert rc == 0, (line, err, rig.log)
    a = _answer(line)
    base = R.parse_alloc((rig.L / "alloc" / f"{S}.json").read_bytes()).base
    assert (a["ok"], a["result"], a["refs"]) == (True, result, refs)
    assert (a["base_oid"], a["object_format"]) == (base.oid, base.object_format)
    pub = rig.L / "export" / "operator" / S
    assert a["repo"] == str(pub) and "warnings" not in a
    assert stat.S_IMODE(os.lstat(pub).st_mode) == 0o750
    names = ["bundle", "report.json"] if kind != "nothing-new" else ["report.json"]
    assert sorted(os.listdir(pub)) == names
    for n in names:
        st = os.lstat(pub / n)
        assert stat.S_IMODE(st.st_mode) == 0o640 and st.st_ino != seen[n]
        digest = "sha256:" + hashlib.sha256((pub / n).read_bytes()).hexdigest()
        assert digest == a["bundle_digest" if n == "bundle" else "report_digest"]
    if kind == "nothing-new":
        assert a["bundle_digest"] is None
    else:   # the answer's refs are the published header's (root's own parse)
        hdr = (pub / "bundle").read_bytes().split(b"\n\n", 1)[0]
        assert f"refs/levain/{S}/head".encode() in hdr
    exp = rig.L / "export"
    assert not (exp / "work.sparseimage").exists() and not os.path.ismount(exp / ".work")
    assert (exp / "importer.sb").exists()
    assert _subsequence(ORDER, rig.log), rig.log
    # 5b1 carry 3: the frame pipe closed right after the terminal frame, so the importer never sits in its
    # TRAILING_WAIT (5 s) before it answers.
    from levain.slot import importer
    assert rig.times["adopt:11"] - rig.times["adopt:frame-closed"] < importer.TRAILING_WAIT


def test_a_process_the_importer_leaves_is_killed_at_seal_out_before_the_publish(rigs):
    # The importer's wrapper leaves a sleeper holding fd 4 (the result pipe) and a writable fd into .work/out: the
    # line is still read at the importer's exit (not at the pipe's EOF), and the sleeper is dead before the copy.
    script = 'exec 5>>"$PWD/out/held"; /bin/sleep 300 & exec "$0" "$@"'
    rig = rigs("delta", argv_wrap=lambda argv: ("/bin/sh", "-c", script) + tuple(argv))
    alive = []
    rig.hooks["adopt:10"] = lambda: alive.append(("10", any(_pg_alive(pg) for pg in rig.prover.pgs)))
    rig.hooks["adopt:publish"] = lambda: alive.append(("publish", any(_pg_alive(pg) for pg in rig.prover.pgs)))
    t0 = time.monotonic()
    rc, line, err = rig.run()
    assert rc == 0, (line, err, rig.log)
    assert alive == [("10", True), ("publish", False)]
    assert time.monotonic() - t0 < 60      # never the sleeper's 300 s, never the V7 wall
    assert _answer(line)["result"] == "delta"


def test_an_unproven_seal_out_publishes_nothing_and_leaves_the_image(rigs):
    rig = rigs("delta", fail_prove_at=2)
    rc, line, err = rig.run()
    assert (rc, _answer(line)["refused"]) == (RH.EXIT_REFUSED, "export-domain-unproven"), (line, rig.log)
    exp = rig.L / "export"
    assert not (exp / "operator" / S).exists() and not (exp / "operator" / (S + ".new")).exists()
    assert (exp / "work.sparseimage").exists() and os.path.ismount(exp / ".work")
    assert "image left attached: the domain is not proven empty" in rig.log
    assert rig.log[-2:] == ["adopt:adopt-unlocked", "adopt:session-unlocked"]


def test_a_class_dir_fsync_failure_after_the_commit_reaches_the_answer(rigs):
    # Head ruling A2: the publish stands and the answer says a crash may undo it.
    def fault(point):
        if point == "fsync:class":
            raise OSError(errno.EIO, "seam")
    rig = rigs("delta", fault=fault)
    rc, line, err = rig.run()
    a = _answer(line)
    assert rc == 0 and a["ok"] is True and a["result"] == "delta", (line, err)
    assert len(a["warnings"]) == 1 and a["warnings"][0]


PROBE = ("import os, resource, sys\n"
         "fds = []\n"
         "for fd in range(256):\n"
         "    try:\n"
         "        os.fstat(fd)\n"
         "        fds.append(fd)\n"
         "    except OSError:\n"
         "        pass\n"
         "lim = [resource.getrlimit(r) for r in (resource.RLIMIT_CORE, resource.RLIMIT_FSIZE, resource.RLIMIT_CPU)]\n"
         "with open(sys.argv[1], 'w') as f:\n"
         "    f.write(repr((fds, lim, os.getcwd())))\n"
         "os.execv(sys.argv[2], sys.argv[2:])\n")


def test_the_importer_holds_exactly_fds_0_1_2_4_and_the_three_rlimits(rigs, tmp_path):
    # A7 and head ruling Q3, read from inside the child just before the importer's own exec.
    seen = tmp_path / "probe.txt"
    rig = rigs("delta", argv_wrap=lambda argv: ("/usr/bin/python3", "-I", "-S", "-c", PROBE, str(seen)) + tuple(argv))
    rc, line, err = rig.run()
    assert rc == 0, (line, err)
    fds, lim, cwd = eval(seen.read_text())   # noqa: S307 - our own probe's repr
    assert fds == [0, 1, 2, 4]
    image = RH.adopt_image_bytes("darwin", **{k: v for k, v in _front_sizes(rig).items()})
    assert lim == [(0, 0), (image, image), (int(RH.V7_SECONDS), int(RH.V7_SECONDS))]
    assert os.path.realpath(cwd) == os.path.realpath(rig.L / "export" / ".work")


def _front_sizes(rig) -> dict:
    obj = canon.parse((rig.L / "retained" / S / "object.json").read_bytes())
    base = R.parse_alloc((rig.L / "alloc" / f"{S}.json").read_bytes()).base
    return dict(entries=obj["entries"], nbytes=obj["bytes"],
                manifest_bytes=(rig.L / "retained" / S / "manifest.jsonl").stat().st_size,
                bundle_bytes=base.bundle_bytes)


def _wrong_base_bundle(rig) -> None:
    import dataclasses
    p = rig.L / "alloc" / f"{S}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(dataclasses.replace(a, base=dataclasses.replace(a.base, bundle_sha256="sha256:" + "0" * 64))))


@pytest.mark.parametrize("fail_prove_at,name", [(0, "base-bundle-mismatch"), (2, "export-domain-unproven")])
def test_a_walker_refusal_answers_its_own_name_unless_seal_out_is_unproven(rigs, fail_prove_at, name):
    # Head ruling Q5: seal-out still runs; an unproven domain wins; else the walker's name; nothing published.
    rig = rigs("delta", fail_prove_at=fail_prove_at)
    _wrong_base_bundle(rig)
    rc, line, err = rig.run()
    assert (rc, _answer(line)["refused"]) == (RH.EXIT_REFUSED, name), (line, rig.log)
    assert "adopt:11" in rig.log and "adopt:classify" not in rig.log
    assert not (rig.L / "export" / "operator" / S).exists()


def test_both_locks_are_held_through_the_publish_and_released_adopt_first(rigs):
    import fcntl
    rig = rigs("delta")
    held = {}

    def probe(tag):
        for name, path in (("session", rig.L / "alloc" / f"{S}.lock"), ("adopt", rig.L / "export" / "adopt.lock")):
            fd = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held[(tag, name)] = False
            except BlockingIOError:
                held[(tag, name)] = True
            finally:
                os.close(fd)
    rig.hooks["adopt:publish"] = lambda: probe("publish")
    rig.hooks["adopt:adopt-unlocked"] = lambda: probe("between")
    rc, line, err = rig.run()
    assert rc == 0, (line, err)
    assert held == {("publish", "session"): True, ("publish", "adopt"): True,
                    ("between", "session"): True, ("between", "adopt"): False}


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_install_writes_the_export_importer_entry_the_launch_names(host):
    # Head ruling Q4: the entry joins ENTRY_MODULES and entry_steps; its path is the argv[0] export_launch_spec builds.
    from levain.slot import wall
    from levain.slot.layout import HELPER_DIR
    path = HELPER_DIR[host] / wall.EXPORT_ENTRY["importer"]
    steps = {s.write[0]: s.write for s in root_tree.entry_steps(host)}
    assert path in steps and steps[path][2] == 0o755
    assert 'runpy.run_module(\'levain.slot.importer\', run_name="__main__"' in steps[path][1]
    # 5d-i session 1 added the verifier entry: an ENTRY, never a sudo helper.
    assert root_tree.ENTRY_MODULES["export-verifier"] == "levain.slot.verify_run"
    assert "export-verifier" not in root_tree.HELPER_MODULES


# --- fix round 1 (L3 r1): each row built red against 58e5e3f2 -------------------------------------------------------

def _failing_detach(argv, umask):
    """The real hdiutil, except ``detach`` answers rc 1 and leaves the image mounted (a busy mount, say)."""
    if len(argv) > 1 and argv[1] == "detach":
        return 1, "hdiutil: detach failed - Resource busy (seam)"
    return ER._run_cmd(argv, umask)


def test_a_failed_detach_before_the_commit_publishes_nothing(rigs):
    # L3 r1 codex HIGH: release_image is best-effort; the publish path requires the image RELEASED before the commit.
    rig = rigs("delta", image_run=_failing_detach)
    rc, line, err = rig.run()
    a = _answer(line)
    pub = rig.L / "export" / "operator" / S
    assert (rc, a.get("refused")) == (RH.EXIT_REFUSED, "export-image-unavailable"), (line, pub.exists(), rig.log)
    assert "detach" in err, err
    exp = rig.L / "export"
    assert not (exp / "operator" / S).exists() and not (exp / "operator" / (S + ".new")).exists()
    assert "noreplace" not in rig.log and "exchange" not in rig.log
    assert rig.log[-2:] == ["adopt:adopt-unlocked", "adopt:session-unlocked"]


@pytest.mark.parametrize("fail_prove_at,name", [(2, "export-domain-unproven"), (0, "internal")])
def test_a_cleanup_seal_out_that_is_unproven_replaces_the_earlier_failure(rigs, monkeypatch, fail_prove_at, name):
    # L3 r1 codex MED / ruling Q5: a failure before step 11 (here a BrokenPipeError once the frames are closed) still
    # runs seal-out in the finally; an unproven domain there REPLACES the earlier error, which rides as its context.
    rig = rigs("delta", fail_prove_at=fail_prove_at)

    def broken():
        raise BrokenPipeError(32, "seam")
    rig.hooks["adopt:frame-closed"] = broken
    seen = []
    orig = RH.VERB_HANDLERS["adopt-open"]

    def wrapped(ctx, req, blob):
        try:
            return orig(ctx, req, blob)
        except BaseException as e:
            seen.append(e)
            raise
    monkeypatch.setitem(RH.VERB_HANDLERS, "adopt-open", wrapped)
    rc, line, err = rig.run()
    assert _answer(line)["refused"] == name, (line, err, rig.log)
    assert not (rig.L / "export" / "operator" / S).exists()
    assert "adopt:11" not in rig.log and "adopt:teardown" in rig.log
    if fail_prove_at:
        assert isinstance(seen[0].__context__, BrokenPipeError), repr(seen[0].__context__)
        assert os.path.ismount(rig.L / "export" / ".work")      # unproven: the image is left for the next seal-in


def _open_fds() -> set:
    return set(int(n) for n in os.listdir("/dev/fd"))


def test_a_failed_second_pipe_leaks_no_fd_of_the_first(rigs, monkeypatch):
    # L3 r1 complement LOW 1: the two pipes and their four fds are registered in one guarded block.
    rig = rigs("delta")
    real, calls = os.pipe, []

    def pipe():
        calls.append(1)
        if len(calls) == 2:
            monkeypatch.setattr(os, "pipe", real)     # one failure only: teardown's hdiutil runs pipe() too
            raise OSError(errno.EMFILE, "seam")
        return real()
    launch = rig.launch

    def launch_then_patch(ctx, args, export_class):   # the launch is root's last act before the two pipes
        cmd = launch(ctx, args, export_class)
        monkeypatch.setattr(os, "pipe", pipe)
        return cmd
    rig.launch = launch_then_patch
    before = _open_fds()
    rc, line, err = rig.run()
    assert _answer(line)["refused"] == "internal" and "Errno 24" in err, (line, err)
    assert len(calls) == 2
    assert _open_fds() == before


def test_an_importer_reaped_before_collect_export_fails_is_never_handed_to_seal_out(rigs, monkeypatch):
    # L3 r1 complement LOW 2: once collect_export's waitpid has reaped the importer, the pid is never signalled again.
    rig = rigs("delta")
    real = os.WIFSIGNALED

    def once(status):
        monkeypatch.setattr(os, "WIFSIGNALED", real)
        raise RuntimeError("seam: after the reap")
    rig.hooks["adopt:10"] = lambda: monkeypatch.setattr(os, "WIFSIGNALED", once)
    rc, line, err = rig.run()
    assert _answer(line)["refused"] == "internal" and "after the reap" in err, (line, err)
    assert rig.prover.children[-1] == (), rig.prover.children
