"""S07 (VM sitting, row 23 / blocker B7): the Workspace stager's W1 REAL fire with his signed provider policy. Host side
prepares and judges; the guest side (the ``guest`` subcommand, run as the guest OPERATOR) fires. usage:
  python3 -m scripts.vm_harness.s07_stager_fire pack <out dir> --consent ~/.levain/consent --cockpit <host cockpit data root>
  python3 -m scripts.vm_harness.s07_stager_fire install <vm> <pack dir> [--profile darwin|linux]
  python3 -m scripts.vm_harness.s07_stager_fire run <vm> <evidence dir> --repo /abs/path/of/the/stager-test-repo [--release]
  <guest python> -I <tools dir>/s07_stager_fire.py guest policy | judge --repo R | fire --repo R --bundle-size N [--release]
(run from the repo root WITHOUT -I; ``<vm>`` is a booted ``vend-*`` clone; the guest side is copied into the clone with
``host_profile.py`` and ``runbook.py`` beside it, and the guest needs the ``tests`` tree + the pip dir, as S06.)

WHICH FILES GO TO THE GUEST (:func:`plan_pack`, the whole list): four files and the directories that hold them, all public
integrity data, no private key. ``control.db`` (a consistent sqlite backup of the operator's control store: the signed
provider policy items); a TRIMMED ``cockpit.db`` holding only ``meta`` + ``authority_log`` (the log the policy's trust root is
verified against); the anchor file; and a one-line ``claude_code.json`` naming the GUEST cockpit. NEVER, and refused in code
(:func:`forbidden_reason`, :func:`check_pack`): anything under ``.levain/keys`` or ``.levain-sign``, ``queue.db``,
``dispatch.db``, the WHOLE ``cockpit.db`` (phone tokens, paired devices, pendings), the host's own ``claude_code.json``, any
``*.key`` / ``*.pem`` / ``*.p12`` / ``id_*`` file, and any pack file whose bytes contain ``PRIVATE KEY``. No signing happens in
S07; ``dispatch config show`` is a read.

WHAT S07 OBSERVES, in order (one verdict each, written to ``EXPECTED.txt`` with its sha256 before the first guest call):
  G3  the guest operator reads the copied policy through the product's own ``stager._control_policy`` (the cheap R1 test:
      the guest's ssh-keygen must verify his key's signature on the replayed authority log). A mismatch stops the run, nothing is fired.
  J1  the stager's whole judge on the test repo (no session, no server): head, clean tree, manifest bytes, objects, commit, bundle.
  F1-F3  the real fire (``DispatchSessionLauncher.spawn`` over the real ``run.sock``): the LAUNCH answer carries a session
      (render passed), the server's run record, the staged bundle's size equal to the stager's.
  F4  with ``--release`` the vendor is released once (a pong, small spend on his subscription): the child exits 0 and the
      tunnel moved. Without it F4 is SKIP and the result says ``vendor not released``.
Where the sitting ran S07 is a PASSTHROUGH fire with the brokered server up beside it; the guest also prints which sockets exist
as an ``S07-OBSERVED`` line (not a verdict). A guest runs only ONE fire world: a second run refuses until the first world is reset.

NOT proven by a pass: the install-setting path (``session_install.json`` workspaces + ``run_session``), a non-clean repo, a
non-empty withhold (W2), the sunlnk END. Exit codes as the other runners: 0 PASS, 1 FAIL, 2 REFUSED, 3 INCOMPLETE.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import fnmatch
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote

if __package__:
    from scripts.vm_harness.driver import GuestError
    from scripts.vm_harness.host_profile import DARWIN, LINUX, PROFILES, HostProfile, pick_levain_root, profile_for_platform, profile_named
    from scripts.vm_harness.runbook import Evidence, RunbookError, Verdict, capture, judge, overall
else:   # run as a script in the clone, next to host_profile.py / runbook.py (python -I leaves the script's own dir off sys.path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from host_profile import DARWIN, LINUX, PROFILES, HostProfile, pick_levain_root, profile_for_platform, profile_named
    from runbook import Evidence, RunbookError, Verdict, capture, judge, overall

    class GuestError(RuntimeError):      # the host's transport error; a guest run never raises it
        pass

PYLIB = "/tmp/pylib"
PONG_BRIEF = "Reply with exactly the word: pong. Use no tools."
SCRIPT_NAME = "s07_stager_fire.py"
WORLD_NAME = "world"
GUEST_COCKPIT_DIR = ".levain-fire-cockpit"


class PackRefused(RuntimeError):
    """The policy pack cannot be built, is not the planned four files, or the guest already holds a store: nothing is sent."""


# ======================================================================================================================
# host side: which files go to the guest
# ======================================================================================================================

@dataclass(frozen=True)
class PackItem:
    kind: str                 # "dir" | "file"
    dest: str                 # the absolute guest path
    mode: int
    role: str                 # "dir" for a directory, else control_db | claude_code_json | cockpit_db | anchor
    src: Path | None = None   # the host file that is sent (None in a plan)


@dataclass(frozen=True)
class Pack:
    items: tuple[PackItem, ...]


def guest_home(profile: HostProfile) -> str:
    """The guest operator's home (S07 copies into ``$HOME/.levain/consent``, where ``queue_dir_for`` looks)."""
    base = {DARWIN.name: "/Users", LINUX.name: "/home"}[profile.name]
    return f"{base}/{profile.ssh_user}"


def plan_pack(home: str) -> list[PackItem]:
    """The ONE list of what goes to the guest, with its modes and the order they are made in. Pure: names, no bytes."""
    levain, consent = f"{home}/.levain", f"{home}/.levain/consent"
    cock, anchor_dir = f"{home}/{GUEST_COCKPIT_DIR}", f"{home}/{GUEST_COCKPIT_DIR}/anchor"
    return [
        PackItem("dir", levain, 0o700, "dir"),
        PackItem("dir", consent, 0o700, "dir"),              # ControlStore: owned by the running uid, no group/other bit
        PackItem("file", f"{consent}/control.db", 0o600, "control_db"),
        PackItem("file", f"{consent}/claude_code.json", 0o600, "claude_code_json"),
        PackItem("dir", cock, 0o700, "dir"),
        PackItem("dir", anchor_dir, 0o700, "dir"),
        PackItem("file", f"{cock}/cockpit.db", 0o600, "cockpit_db"),
        PackItem("file", f"{anchor_dir}/authority.anchor", 0o600, "anchor"),
    ]


#: a path COMPONENT SEQUENCE that is private: ``.levain/keys`` (his keys), ``.levain-sign`` (his signing cockpit and keys)
FORBIDDEN_PARTS = ((".levain", "keys"), (".levain-sign",))
#: a file NAME that is never copied whole (``cockpit.db`` is read by :func:`trim_cockpit` only, never sent as it is)
FORBIDDEN_NAMES = ("queue.db", "dispatch.db", "cockpit.db", "*.key", "*.p12", "*.pem", "id_*")


def forbidden_reason(path: str | Path, *, names: bool = True) -> str | None:
    """Why ``path`` must never be sent (None: it may). Both the path as given and where it really lands are checked, so a
    link into a private tree is as forbidden as the tree."""
    for p in {os.path.abspath(str(path)), os.path.realpath(str(path))}:
        parts = Path(p).parts
        for seq in FORBIDDEN_PARTS:
            for i in range(len(parts) - len(seq) + 1):
                if tuple(parts[i:i + len(seq)]) == seq:
                    return f"{p} is under a private tree ({'/'.join(seq)}): never copied"
        if names:
            base = os.path.basename(p)
            for pat in FORBIDDEN_NAMES:
                if fnmatch.fnmatch(base, pat):
                    return f"{base} is never copied whole (matches {pat})"
    return None


def read_anchor(path: Path) -> bytes:
    return Path(path).read_bytes()


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def trim_cockpit(src: Path, dst: Path) -> int:
    """Write ``dst``: ``meta`` + ``authority_log`` of the cockpit db ``src`` (their stored DDL and every row) and NOTHING else.
    Returns the authority log's row count."""
    s = _ro(src)
    d = sqlite3.connect(str(dst))
    try:
        n = 0
        for name in ("meta", "authority_log"):
            row = s.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
            if row is None or not row[0]:
                raise PackRefused(f"the host cockpit db has no {name} table")
            d.execute(row[0])
            rows = s.execute(f'SELECT * FROM "{name}"').fetchall()
            if rows:
                d.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * len(rows[0]))})', rows)
            if name == "authority_log":
                n = len(rows)
        d.commit()
        return n
    finally:
        s.close()
        d.close()


def _tables(path: Path) -> set[str]:
    c = _ro(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        c.close()


def build_pack(out: Path, *, host_consent: Path, host_cockpit: Path, guest_home: str) -> Pack:
    """Build the four files under ``out`` from the host's control store and cockpit, then :func:`check_pack` them. Refuses
    (:class:`PackRefused`) a source that is private or absent; nothing half-built is left in ``out``. The anchor is read
    FIRST, then the log (the log may run ahead of the anchor, never behind)."""
    out, host_consent, host_cockpit = Path(out), Path(host_consent), Path(host_cockpit)
    control = host_consent / "control.db"
    cockpit_db = host_cockpit / "cockpit.db"
    anchor = host_cockpit / "anchor" / "authority.anchor"
    for p in (host_consent, host_cockpit, control, anchor):
        why = forbidden_reason(p)
        if why:
            raise PackRefused(f"private source refused: {why}")
    if not control.is_file():
        raise PackRefused(f"no signed policy on this host: {control} does not exist (nothing to send; S07 needs his signed provider policy)")
    if not anchor.is_file():
        raise PackRefused(f"the cockpit anchor {anchor} does not exist (the authority log cannot be checked against it)")
    if not cockpit_db.is_file():
        raise PackRefused(f"the cockpit db {cockpit_db} does not exist (no authority log to verify the policy's trust root)")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PackRefused(f"{out} exists and is not empty: a pack is built into a fresh directory")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".s07-pack-", dir=out.parent))
    try:
        anchor_bytes = read_anchor(anchor)                    # FIRST
        if trim_cockpit(cockpit_db, tmp / "cockpit.db") == 0:  # THEN the log
            raise PackRefused("the host authority log is empty: nothing to verify the policy against")
        (tmp / "authority.anchor").write_bytes(anchor_bytes)
        src = _ro(control)
        dst = sqlite3.connect(str(tmp / "control.db"))
        try:
            src.backup(dst)                                    # a consistent copy, not a cp of a possibly-open file
        finally:
            src.close()
            dst.close()
        (tmp / "claude_code.json").write_text('{"cockpit_data_root": "%s/%s"}\n' % (guest_home, GUEST_COCKPIT_DIR))
        for f in tmp.iterdir():
            os.chmod(f, 0o600)
        if out.exists():
            out.rmdir()
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    files = {"control_db": "control.db", "claude_code_json": "claude_code.json", "cockpit_db": "cockpit.db",
             "anchor": "authority.anchor"}
    pack = Pack(tuple(dataclasses.replace(i, src=out / files[i.role]) if i.kind == "file" else i for i in plan_pack(guest_home)))
    check_pack(pack)
    return pack


def load_pack(pack_dir: Path, guest_home_: str) -> Pack:
    """A pack previously built by :func:`build_pack` (``pack`` subcommand), re-read from its directory and re-checked."""
    files = {"control_db": "control.db", "claude_code_json": "claude_code.json", "cockpit_db": "cockpit.db",
             "anchor": "authority.anchor"}
    pack = Pack(tuple(dataclasses.replace(i, src=Path(pack_dir) / files[i.role]) if i.kind == "file" else i
                      for i in plan_pack(guest_home_)))
    check_pack(pack)
    return pack


def check_pack(pack: Pack) -> None:
    """Raise :class:`PackRefused` unless ``pack`` is EXACTLY the planned items, each at its planned mode, the cockpit db holds
    only ``meta`` + ``authority_log``, the control db only ``meta`` + ``items``, and no sent byte is a private key."""
    ctl = next((i for i in pack.items if i.role == "control_db"), None)
    if ctl is None or not ctl.dest.endswith("/.levain/consent/control.db"):
        raise PackRefused("the pack has no control_db at the planned place")
    plan = {i.dest: i for i in plan_pack(ctl.dest[:-len("/.levain/consent/control.db")])}
    seen = set()
    for it in pack.items:
        want = plan.get(it.dest)
        if want is None:
            raise PackRefused(f"{it.dest} ({it.role}) is not in the plan: the pack is exactly the planned files")
        if (it.kind, it.role) != (want.kind, want.role) or it.mode != want.mode:
            raise PackRefused(f"{it.dest}: kind/role/mode {it.kind}/{it.role}/{oct(it.mode)} is not the planned "
                              f"{want.kind}/{want.role}/{oct(want.mode)}")
        seen.add(it.dest)
        if it.kind != "file":
            continue
        if it.src is None or not Path(it.src).is_file():
            raise PackRefused(f"{it.dest}: no source file to send")
        why = forbidden_reason(it.src, names=False)
        if why:
            raise PackRefused(f"private source refused: {why}")
        if b"PRIVATE KEY" in Path(it.src).read_bytes():
            raise PackRefused(f"{it.dest}: the bytes of {it.src} contain a private key marker: never sent")
        if it.role == "cockpit_db" and _tables(it.src) != {"meta", "authority_log"}:
            raise PackRefused(f"{it.dest}: the cockpit db must hold exactly the tables meta + authority_log, not {sorted(_tables(it.src))} "
                              "(the whole cockpit.db carries his phone tokens and paired devices)")
        if it.role == "control_db" and _tables(it.src) != {"meta", "items"}:
            raise PackRefused(f"{it.dest}: the control db must hold exactly the tables meta + items, not {sorted(_tables(it.src))}")
    if seen != set(plan):
        raise PackRefused(f"the pack is missing planned items: {sorted(set(plan) - seen)}")


#: Run on the guest as the OPERATOR (``python -I -c``): make a directory (mode set, our own, never a link) or write ONE file from
#: base64 on stdin (``O_EXCL``: never an overwrite, no symlink followed). Nothing of the payload is in the argv.
INSTALL_PY = (
    "import base64, os, stat, sys\n"
    "kind, dest, mode = sys.argv[1], sys.argv[2], int(sys.argv[3], 8)\n"
    "if kind == 'dir':\n"
    "    try:\n"
    "        os.mkdir(dest, mode)\n"
    "    except FileExistsError:\n"
    "        st = os.lstat(dest)\n"
    "        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():\n"
    "            raise SystemExit('refusing: ' + dest + ' exists and is not our directory')\n"
    "    os.chmod(dest, mode)\n"
    "    print('dir-ok', dest)\n"
    "else:\n"
    "    data = base64.b64decode(sys.stdin.read())\n"
    "    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)\n"
    "    try:\n"
    "        os.write(fd, data)\n"
    "        os.fchmod(fd, mode)\n"
    "    finally:\n"
    "        os.close(fd)\n"
    "    print('file-ok', dest, len(data))\n")
EXISTS_SCRIPT = 'for p in "$@"; do if [ -e "$p" ] || [ -L "$p" ]; then echo "exists $p"; fi; done'


def install_pack(guest, stdin_run, profile: HostProfile, pack: Pack) -> None:
    """Send ``pack`` to the guest as the operator. REFUSES (nothing sent) when any destination file already exists: a guest
    store is never overwritten (``ControlStore`` pins one trust root once)."""
    check_pack(pack)
    files = [i for i in pack.items if i.kind == "file"]
    lines, _rc = capture(guest, ("/bin/sh", "-c", EXISTS_SCRIPT, "sh", *(i.dest for i in files)), 60)
    there = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("exists ")]
    if there:
        raise PackRefused(f"already exists in the guest: {there}: STOP and name it; a guest store is never overwritten")
    for it in pack.items:
        argv = (profile.python, "-I", "-c", INSTALL_PY, it.kind, it.dest, format(it.mode, "o"))
        payload = base64.b64encode(Path(it.src).read_bytes()).decode() if it.kind == "file" else ""
        out = stdin_run(argv, payload, 60)
        ok = any(ln.startswith(f"file-ok {it.dest} ") for ln in out.splitlines()) if it.kind == "file" else \
            f"dir-ok {it.dest}" in out.splitlines()
        if not ok:
            raise PackRefused(f"installing {it.dest} printed no ok line ({out.strip()[:80]!r})")


# ======================================================================================================================
# guest side (the ``guest`` subcommand): runs as the guest operator, with levain importable
# ======================================================================================================================

def _stage_paths(profile: HostProfile) -> str | None:
    root = pick_levain_root(profile)
    for p in (PYLIB, root):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    return root


def guest_policy() -> str:
    """The operator's active provider policy as the stager reads it (the signed control store copied into this guest)."""
    from levain.dispatch import stager
    try:
        pol = stager._control_policy()
    except stager.StageRefused as exc:
        return f"S07-POLICY refused={exc.name}"
    return f"S07-POLICY never={len(pol.never)} hash={pol.content_hash()}"


def guest_judge(repo: str, *, git: str | None = None) -> str:
    """The stager's whole judge on ``repo``: head, clean tree, and the upload's manifest/objects/commit/bundle. No session."""
    from levain.agents.session import Workspace
    from levain.dispatch import stager
    git = git or profile_for_platform(sys.platform).git
    try:
        head = stager.resolve_head(repo)
        porcelain = subprocess.run([git, "-c", "safe.directory=*", "-C", repo, "status", "--porcelain"], capture_output=True,
                                   text=True, timeout=60)
        clean = "unknown" if porcelain.returncode else ("yes" if not porcelain.stdout.strip() else "no")
        up = stager.build_upload(Workspace(repo=repo, head=head), signed_withhold=())
    except stager.StageRefused as exc:
        return f"S07-JUDGE refused={exc.name}"
    return (f"S07-JUDGE head={head} clean={clean} manifest={len(up.manifest)} objects={up.objects} commit={up.commit} "
            f"bundle={len(up.bundle)}")


def workspace_for(repo: str, role: dict):
    """The launch's workspace member: the repo's checked-out commit and the SIGNED role's withhold (never anything else:
    the client and the server both re-derive it and refuse another)."""
    from levain.agents.role import role_withhold
    from levain.agents.session import Workspace
    from levain.dispatch import stager
    return Workspace(repo=repo, head=stager.resolve_head(repo), withhold=role_withhold(role), branch=None)


def make_launcher(rig, sock: str):
    """The production client of the run server, holding the signed-role and signed-link sources (without them ``spawn``
    refuses ``link-proof-unavailable``). ``build_workspace`` is the CLASS's: it reads the copied signed policy. (The 11-06
    driver replaced it with a stand-in NEVER list; deleting that is what makes this the real fire.)"""
    from levain.agents import session_client as C
    return C.DispatchSessionLauncher(
        C.unix_connector(sock),
        lambda occ: C.link_proof(rig.store, rig.world.root, occ),
        C.AuthorityFeed(rig.store, rig.world.root),
        lambda occ: C.signed_role_of(rig.store, occ),
        lambda occ: C.signed_link_of(rig.store, rig.world.root, occ))


def _record_line(profile: HostProfile, key: str) -> str:
    import json
    from levain.agents.session_launcher import SessionIndex
    index = Path(profile.store) / "dispatch" / "sessions"
    path = getattr(SessionIndex(index), "_path", lambda k: index / f"{k}.json")(key)
    r = subprocess.run(["sudo", "-n", "/bin/cat", str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode:
        return f"S07-RECORD unreadable={r.returncode}"
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return "S07-RECORD unreadable=not-json"
    ws = doc.get("workspace") or {}
    g = lambda v: "null" if v in (None, "") else v   # noqa: E731
    return (f"S07-RECORD state={g(doc.get('state'))} head={g(ws.get('head'))} manifest_digest={g(ws.get('manifest_digest'))} "
            f"withhold_digest={g(ws.get('withhold_digest'))} auth={g(doc.get('auth') or ws.get('auth'))}")


def _bundle_line(profile: HostProfile, session: str, stager_size: int) -> str:
    path = f"{profile.store}/live/{session}/in/repo.bundle"
    r = subprocess.run(["sudo", "-n", "/bin/sh", "-c", 'wc -c < "$1"', "sh", path], capture_output=True, text=True, timeout=60)
    size = r.stdout.strip() if not r.returncode and r.stdout.strip().isdigit() else "unreadable"
    return f"S07-BUNDLE size={size} stager={stager_size}"


def guest_fire(repo: str, bundle_size: int, *, release: bool, sock: str | None = None, profile: HostProfile | None = None) -> int:
    """Launch one real passthrough session with the stager's workspace, read the server's record and the staged bundle, and
    (with ``release``) let the vendor run once. Prints the ``S07-*`` lines. Returns 0, or 2 when a precondition is absent
    (a refusal, nothing fired)."""
    profile = profile or profile_for_platform(sys.platform)
    sock = sock or f"{profile.run_dir}/dispatch/run.sock"
    try:
        is_sock = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        is_sock = False
    if not is_sock:
        print(f"S07-FIRE refused=no run socket at {sock}: not a booted guest with the dispatch service")
        return 2
    if os.geteuid() == 0:
        print("S07-FIRE refused=running as root: run as the guest operator")
        return 2
    _stage_paths(profile)
    brokered = os.path.exists(f"{profile.run_dir}/dispatch/run-brokered.sock")
    print(f"S07-OBSERVED sockets run=yes brokered={'yes' if brokered else 'no'}")
    world = Path(profile.tools_dir) / WORLD_NAME
    if world.exists():
        print(f"S07-FIRE refused=a fire world already exists at {world}: the server's authority replica is stale; reset it first")
        return 2
    from levain.agents import session_client as C
    from levain.agents.role import add_role, parse_role
    from levain.agents.session import SessionLaunch
    from tests import test_agents_session_launch as TL
    from tests.test_agents_launch import Rig
    world.mkdir(parents=True, mode=0o700)
    rig = Rig(world)
    oid = TL._occurrence(rig, billing="subscription", name="fire-s07")
    add_role(rig.store, parse_role(TL.ROLE))
    launcher = make_launcher(rig, sock)
    key = "s-" + os.urandom(8).hex()
    child = None
    try:
        launch = SessionLaunch(oid, key, TL.ROLE["harness"], TL.ROLE["model"], PONG_BRIEF, "passthrough", TL.EXP,
                               workspace_for(repo, TL.ROLE))
        try:
            child = launcher.spawn(launch)
        except C.LauncherRefused as exc:
            print(f"S07-LAUNCH refused={exc.name} detail={str(exc)[:160]!r}")
            return 0
        except Exception as exc:  # noqa: BLE001 - a driver fault is a named refusal line, never a silent exit
            print(f"S07-LAUNCH refused=driver:{type(exc).__name__} detail={str(exc)[:160]!r}")
            return 0
        print(f"S07-LAUNCH launched=true session={child.session} key={key}")
        print(_record_line(profile, key))
        print(_bundle_line(profile, child.session, bundle_size))
        if release:
            child.release()
            try:
                rc = child.process.wait(180)
            except subprocess.TimeoutExpired:
                rc = "timeout"
            usage = launcher.usage(key)
            tunnel = getattr(usage, "tunnel_bytes", None)
            print(f"S07-VENDOR wait_rc={rc} tunnel_bytes={0 if tunnel is None else tunnel}")
    finally:
        if child is not None:
            ended = launcher.end_session(key, 30.0)
            print(f"S07-END terminated={str(bool(ended.terminated)).lower()}")
    return 0


def guest_main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="s07_stager_fire.py guest")
    ap.add_argument("op", choices=("policy", "judge", "fire"))
    ap.add_argument("--repo")
    ap.add_argument("--bundle-size", type=int, default=0)
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--sock")
    a = ap.parse_args(list(argv))
    profile = profile_for_platform(sys.platform)
    if a.op == "fire":
        if not a.repo:
            print("S07-FIRE refused=no --repo")
            return 2
        return guest_fire(a.repo, a.bundle_size, release=a.release, sock=a.sock, profile=profile)
    _stage_paths(profile)
    if a.op == "policy":
        print(guest_policy())
        return 0
    if not a.repo:
        print("S07-JUDGE refused=no --repo")
        return 2
    print(guest_judge(a.repo, git=profile.git))
    return 0


def guest_argv(profile: HostProfile, op: str, *args: str) -> tuple[str, ...]:
    """The guest invocation of one op: the OPERATOR runs it (no sudo: ControlStore wants the running uid to own its dir)."""
    return (profile.python, "-I", f"{profile.tools_dir}/{SCRIPT_NAME}", "guest", op, *args)


# ======================================================================================================================
# host side: the run, one verdict per step
# ======================================================================================================================

@dataclass(frozen=True)
class Expect:
    """What the page's derivation says the real run reads (gen 2, 13 never-patterns; the test repo's own numbers)."""
    never: int = 13
    policy_hash: str = "sha256:0ed3b20a53dfb6f333d7d97950c1e388999db9c9135f8e66e6e4bf8788ca8940"
    manifest: int = 1669
    objects: int = 28
    commit: str = "fdfa5ab0aa75449f8e4d594762909f381f324946"
    head: str = "cff31aa075ece49b329761eb55c9be46b230c5ff"


@dataclass
class S07Config:
    profile: HostProfile
    repo: str = ""
    release: bool = False
    expect: Expect = field(default_factory=Expect)


@dataclass
class Step:
    id: str
    name: str
    status: str            # PASS | FAIL | UNBOUND | SKIP | ERROR
    raw: str = ""
    expected: str = ""
    detail: str = ""

    def line(self) -> str:
        tail = f" raw={self.raw!r} expected={self.expected}" if self.status in ("PASS", "FAIL") else ""
        return f"{self.id} {self.name}: {self.status}{tail}" + (f" ({self.detail})" if self.detail else "")


def verdict_table(cfg: S07Config) -> list[tuple[str, str, str, str]]:
    """``(id, name, pattern, selector)``: one verdict each, fixed before any run (no alternation)."""
    e = cfg.expect
    sha = r"sha256:[0-9a-f]{64}"
    return [
        ("G3", "the copied signed policy reads (13 patterns, gen 2)", rf"S07-POLICY never={e.never} hash={re.escape(e.policy_hash)}", r"^S07-POLICY "),
        ("J1", "the stager judges the test repo", rf"S07-JUDGE head={e.head} clean=yes manifest={e.manifest} objects={e.objects} "
                                                  rf"commit={e.commit} bundle=[0-9]+", r"^S07-JUDGE "),
        ("F1", "render passes and the run starts (LAUNCH carries a session)", r"S07-LAUNCH launched=true session=s-\S+", r"^S07-LAUNCH "),
        ("F2", "the server's run record", rf"S07-RECORD state=(?!refused\b)\S+ head={e.head} manifest_digest={sha} "
                                          rf"withhold_digest={sha} auth=passthrough", r"^S07-RECORD "),
        ("F3", "the staged bundle is the stager's", r"S07-BUNDLE size=([0-9]+) stager=\1", r"^S07-BUNDLE "),
        ("F4", "the released vendor exits 0 and the tunnel moved", r"S07-VENDOR wait_rc=0 tunnel_bytes=[1-9][0-9]*", r"^S07-VENDOR "),
    ]


def expected_text(cfg: S07Config) -> str:
    out = [f"S07: expected verdicts, os={cfg.profile.name}, repo={cfg.repo}, release={cfg.release}",
           "step | name | expected raw line (regex), exit code 0"]
    for sid, name, pat, _sel in verdict_table(cfg):
        out.append(f"{sid} | {name} | /{pat}/" + (" (SKIP without --release: vendor not released)" if sid == "F4" and not cfg.release else ""))
    out.append("G3 failing stops the run: nothing is fired (the policy copy or the guest ssh-keygen, not the product)")
    out.append("PASS word: 'S07: PASS (render + LAUNCH; vendor not released)' without --release, else '(render + LAUNCH + vendor released)'")
    return "\n".join(out)


#: a launch refusal's name -> the cause it points at (from the page's derivation)
CAUSES = (
    ("workspace-policy-unavailable", "the policy copy (G2), not the product"),
    ("snapshot-", "the repo or the NEVER list: do not edit the repo; report it"),
    ("link-proof-unavailable", "driver edit 1: the launcher lacks the signed-role sources"),
    ("staging-missing", "the workspace seam is unwired on the server (a regression of workspace=uploads.stage)"),
    ("ticket-routes-mismatch", "a 11-05 render prerequisite (routes): re-measure S05; a render refusal costs nothing"),
    ("hook-template-missing", "a 11-05 render prerequisite (hook templates): re-measure S05; a render refusal costs nothing"),
    ("ticket-ceiling", "a 11-05 render prerequisite (the ceiling file): re-measure S05; a render refusal costs nothing"),
)


def cause_of(raw: str) -> str:
    return next((why for needle, why in CAUSES if needle in raw), "")


def run_s07(guest, evidence_dir, cfg: S07Config):
    """Run G3, J1 and the fire through ``guest`` (``guest(argv, timeout) -> stdout``); ``(status, [Step], RESULT path)``.
    EXPECTED (with its sha256) is written before the first guest call. A repo that is missing or not absolute refuses before
    even that. G3 or J1 failing stops the run before the fire."""
    if not cfg.repo:
        raise RunbookError("--repo is required: the absolute path of the stager test repo in the guest")
    if not os.path.isabs(cfg.repo):
        raise RunbookError(f"--repo {cfg.repo!r} is not an absolute path")
    table = verdict_table(cfg)
    for _sid, _n, pat, _sel in table:
        Verdict(0, pat).check()
    ev = Evidence(evidence_dir)
    digest = ev.write_expected(expected_text(cfg))
    verdicts = {sid: (Verdict(0, pat), sel) for sid, _n, pat, sel in table}
    names = {sid: n for sid, n, _p, _s in table}
    results: dict[str, Step] = {}

    def done(sid, status, raw="", detail=""):
        v = verdicts[sid][0]
        results[sid] = Step(sid, names[sid], status, raw, f"rc=0 /{v.pattern}/", detail)
        ev.log(results[sid].line())

    def judged(sid, lines, detail_on_fail=""):
        v, sel = verdicts[sid]
        ok, raw = judge(lines, 0, v, sel)
        done(sid, "PASS" if ok else "FAIL", raw, "" if ok else (detail_on_fail or cause_of(raw)))
        return ok

    def stop_rest(ids, why):
        for sid in ids:
            done(sid, "UNBOUND", detail=why)

    def call(sid, argv, timeout):
        try:
            lines, rc = capture(guest, argv, timeout)
        except (GuestError, RunbookError, OSError, subprocess.SubprocessError) as exc:
            done(sid, "ERROR", detail=f"{type(exc).__name__}: {exc}")
            return None
        if rc != 0:
            done(sid, "FAIL", raw=lines[-1] if lines else "", detail=f"the guest op exited {rc}")
            return None
        return lines

    p = cfg.profile
    later = ("J1", "F1", "F2", "F3", "F4")
    bundle = None
    lines = call("G3", guest_argv(p, "policy"), 120)
    if lines is None:
        stop_rest(later, "G3 did not complete: stop, nothing is fired")
    elif not judged("G3", lines, "stop: this is the policy copy or the guest's ssh-keygen (R1), not the product; do not fire "
                                 + cause_of(" ".join(lines))):
        stop_rest(later, "G3 did not pass: stop, nothing is fired")
    else:
        lines = call("J1", guest_argv(p, "judge", "--repo", cfg.repo), 300)
        if lines is None:
            stop_rest(later[1:], "J1 did not complete: nothing is fired")
        elif not judged("J1", lines):
            stop_rest(later[1:], "J1 did not pass: nothing is fired")
        else:
            m = re.search(r"bundle=([0-9]+)", results["J1"].raw)
            bundle = m.group(1) if m else "0"
            fire = guest_argv(p, "fire", "--repo", cfg.repo, "--bundle-size", bundle, *(("--release",) if cfg.release else ()))
            lines = call("F1", fire, 900)
            if lines is None:
                stop_rest(later[2:], "F1 did not complete")
            elif not judged("F1", lines):
                stop_rest(later[2:], "no session was launched: no record or bundle to read")
            else:
                judged("F2", lines)
                judged("F3", lines)
                if cfg.release:
                    judged("F4", lines)
                else:
                    done("F4", "SKIP", detail="vendor not released (no --release)")
    ordered = [results[sid] for sid, *_ in table]
    status = overall([r.status for r in ordered])
    word = {"PASS": "PASS (render + LAUNCH + vendor released)" if cfg.release else "PASS (render + LAUNCH; vendor not released)"}.get(status, status)
    body = [f"RESULT: {status}", f"S07, os={p.name}, expected-table sha256 {digest} (EXPECTED.txt, written before the first guest call)",
            *(r.line() for r in ordered), f"S07: {word}"]
    return status, ordered, ev.write_result("\n".join(body) + "\n")


# ======================================================================================================================
# command line
# ======================================================================================================================

def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="s07_stager_fire")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("pack")
    pk.add_argument("out")
    pk.add_argument("--consent", default=str(Path.home() / ".levain" / "consent"))
    pk.add_argument("--cockpit", required=True, help="the host's cockpit data root (holds cockpit.db and anchor/)")
    pk.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    ins = sub.add_parser("install")
    ins.add_argument("vm")
    ins.add_argument("pack")
    ins.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    rn = sub.add_parser("run")
    rn.add_argument("vm")
    rn.add_argument("evidence")
    rn.add_argument("--repo", default="")
    rn.add_argument("--release", action="store_true")
    rn.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    gd = sub.add_parser("guest")
    gd.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(list(argv))
    if a.cmd == "guest":
        return guest_main(a.rest)
    profile = profile_named(a.profile)
    if a.cmd == "pack":
        try:
            pack = build_pack(Path(a.out), host_consent=Path(a.consent), host_cockpit=Path(a.cockpit), guest_home=guest_home(profile))
        except PackRefused as exc:
            print(f"s07: refusing: {exc}", file=sys.stderr)
            return 2
        for it in pack.items:
            if it.kind == "file":
                print(f"{it.dest} mode={oct(it.mode)} sha256={hashlib.sha256(Path(it.src).read_bytes()).hexdigest()}")
        return 0
    from scripts.vm_harness import vm_run
    vm_run._clone_guard(a.vm)                                  # before any transport or any read
    from scripts.vm_harness.driver import AdapterError, VmAdapters
    from scripts.vm_harness.host_profile import ProfileMismatch
    from scripts.vm_harness.runbook import make_stdin_runner
    try:
        pack = load_pack(Path(a.pack), guest_home(profile)) if a.cmd == "install" else None
    except PackRefused as exc:
        print(f"s07: refusing: {exc}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = vm_run.make_transport(a.vm, profile, Path(td))
        try:
            VmAdapters(guest, spawn, profile=profile).verify_host()
        except (ProfileMismatch, AdapterError) as exc:
            print(f"s07: refusing: {exc}", file=sys.stderr)
            return 2
        try:
            if a.cmd == "install":
                install_pack(guest, make_stdin_runner(a.vm, profile, Path(td), guest.kind), profile, pack)
                print(f"s07: installed {len([i for i in pack.items if i.kind == 'file'])} files into the guest")
                return 0
            status, _steps, path = run_s07(guest, a.evidence, S07Config(profile, repo=a.repo, release=a.release))
        except (PackRefused, RunbookError) as exc:
            print(f"s07: refusing: {exc}", file=sys.stderr)
            return 2
    print(path.read_text())
    return {"PASS": 0, "FAIL": 1, "REFUSED": 2}.get(status, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
