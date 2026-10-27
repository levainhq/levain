"""The runtime helper's ``start`` and ``teardown`` (CONTRACT §5.1 start / teardown, §3.2, §5.4 step 3, §5.5, §7.3,
§7.4, §8.2, §9.1).

``start`` is the moment a confined vendor process comes to exist; ``teardown`` is when its work must be kept and its
uids proven empty. Both are lease verbs on a session the caller prepared (:func:`runtime_helper.read_alloc`), and
both act on ALLOC facts only (§7.1): the state record is read as a hint for a CAS's generation and ``from``, which
CONTROL checks, never as a fact root acts on.

``start``, in §5.1's order, each step named for ``seams.at`` (tests observe the order and inject faults there):

1. ``kind``: kind ``verifier`` answers ``verb-unbuilt:verifier`` (§5.6, session 2f); interactive passthrough answers
   ``verb-unbuilt:identity-gate`` (the ``keep/`` return + the §7.6 gate are 2f's). A ``login`` session proceeds
   with no ``keep/`` return: its credential is the one the vendor's login writes.
   ``vendor``: root's copy of the session's vendor CLI (its install unit) in ``VENDOR_PREFIX`` is not whole
   (:func:`check_vendor_installed`; setup and repair copy it there) = ``vendor-not-installed:<vendor>``, before any
   clone.
2. ``discovery``: a planted §5.5 name under ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME`` or ``HOME`` is refused by name,
   from listings read through dirfds (render's check decides).
3. ``egress-ready`` (macOS egress): wall's live pf anchor + alias check, refused by its name.
4. ``repo``: ``already-started`` when the record is ACTIVE or BRAKED or root's clone marker exists (never the dir's
   existence), ``not-rendered`` when it is not RENDERED; else root makes ``<S>/repo`` empty (a dir a failed or
   interrupted clone left is removed first), slot-owned, 0700 (the slot cannot create it in a root-owned ``<S>``).
5. ``clone``: ``git clone`` from ``<S>/in/repo.bundle`` AS THE SLOT through wall's clone target (the same path as
   the vendor), with the read-set template for alloc kind ``dispatch|planner|verifier``; a non-zero exit =
   ``clone-failed``, state unchanged, the session's kill domain killed and then ``<S>/repo`` emptied again, so the
   start can be retried. For those three kinds a zero exit is not success until ``clone-check`` passes
   (:func:`clone_check`, §5.4 step 3: (a) the clone's attributes file is render's line; kind ``dispatch`` also
   (b), every index entry's worktree bytes hashed with no filter equal to its blob, inside a window no other
   slot-uid process may be alive in); a failed check goes down the same ``clone-failed`` path. Success = end's
   ``mark_cloned``, written only after the check.
6. ``forwarder`` (Linux egress): the slot's TCP -> ``gw.sock`` relay in the netns, detached (§9.1); the helper
   waits (bounded) for its ready byte, written after bind + listen, and refuses ``forwarder-not-ready`` without it.
7. ``secrets``: ``SECRETS`` from CONTROL (refused ``not-rendered`` unless RENDERED); the handles go ONLY into the
   vendor's extra env, under render's names (§3.2).
8. ``exec``: the vendor forked through the wall with fds 0, 1, 2 (sudo's pty) and the exec pipe; the exec happened
   only on the POSITIVE marker ``+`` (wall's last hop writes it just before the vendor's execve) followed by EOF
   (the pipe is close-on-exec in that hop, so it closes AT the exec). EOF first, ``!`` anywhere, or no ``+``-then-EOF
   within the bound = ``exec-failed``, never ACTIVE. Known edge: a last hop killed after ``+`` and before its execve
   also reads as the exec; the vendor's exit is then observed at once.
9. ``active``: CAS RENDERED -> ACTIVE; then the helper stays the vendor's parent until it exits: ``{ok, exit}``.
   A vendor that exec'd but is not recorded ACTIVE is killed with its whole domain (Linux: the session cgroup;
   macOS: every process of the slot uid), then reaped.
10. ``identity`` (kind ``login`` only, after the vendor exits): :func:`levain.slot.identity.record_login_identity`
   CASes what the vendor's identity call observed into the state record, for ``confirm-identity`` to read. A
   refused CAS is noted, never the verb's answer: the login ran.

Steps 4-9 run under the session END lock (:func:`_start_fenced`, the spawn fence), released after the CAS's answer.

``teardown`` = END (§7.4): CAS live -> REVOKED, the END driver (end.py) with proc.py's real freeze / kill / proof,
then SCRUBBED (proc.scrub over every uid of the slot, under the slot lock) and the slot's state file back to
``reusable`` (rev + 1, under a TRY of the pool lock). ``{ok, state}``.
"""
from __future__ import annotations

import errno
import os
import re
import select
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import canon
from levain.slot import Refused, wall
from levain.slot import end as E
from levain.slot import identity as ID
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import runtime_helper as H
from levain.slot.runtime_grammar import RenderBlob, RuntimeRequest

_SECRET_CAP = 512                 # DECISION: a handle is opaque printable ASCII up to this many bytes
_SECRET_KEY = {RD.QUEUE_CAP_ENV: "queue_cap"}   # every other secret env name carries the session token

#: DECISION: the bound on the vendor's exec, ``+`` and then the pipe's EOF (macOS: asuser + the stage + sandbox-exec +
#: the trampoline, two interpreter starts; Linux: nsenter + bwrap + the stage). Past it the start failed and the
#: domain is killed, in case the vendor would still exec.
EXEC_MARKER_S = 60.0
#: DECISION: the bound on the forwarder's ready byte (one interpreter start, a bind and a listen, in the netns).
FORWARDER_READY_S = 10.0
#: H8 (head ruling): the bound on the clone child, spawn to reap. The clone runs under the session END lock (the spawn
#: fence), so a hung clone would hold every END at ``end-busy``; past it the child is SIGKILLed and reaped (not
#: killpg: the clone child is not a group leader, its group is the helper's), then ``clone-failed: timeout``, whose
#: domain kill ends whatever the clone started as the slot uid.
CLONE_S = 600.0


# --- small reads ------------------------------------------------------------------------------------------------------

def read_record(ctx: H.Ctx, session: str) -> Optional[R.SessionState]:
    """The state record as a HINT (§7.1: generation + ``from`` for a CAS CONTROL checks), or None when absent."""
    return ID.read_record(ctx, session)


def _list_dir(ctx: H.Ctx, parts: Sequence[str]) -> List[str]:
    """The names in ``L/<parts>`` (the last component may be the slot's own dir); none when it is absent."""
    try:
        fd = ctx.store.open_dir(*parts, trusted=False)
    except Refused as e:
        if e.name == "store-missing":
            return []
        raise
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def discovery_listings(ctx: H.Ctx, session: str) -> Dict[str, List[str]]:
    """§5.5's three roots as render's check reads them: ``claude`` = ``<S>/config/claude``, ``codex`` =
    ``<S>/config/codex``, ``home`` = ``<S>`` with ``.config/<name>`` for the nested names, plus the two XDG dirs
    the env points the vendors' lookups at (render's ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME``), as
    ``xdg/config/<name>`` and ``xdg/data/<name>``."""
    S = ("live", session)
    home = _list_dir(ctx, S)
    if ".config" in home:
        home += [f".config/{n}" for n in _list_dir(ctx, (*S, ".config"))]
    if "xdg" in home:
        for sub in ("config", "data"):
            home += [f"xdg/{sub}/{n}" for n in _list_dir(ctx, (*S, "xdg", sub))]
    return {"claude": _list_dir(ctx, (*S, "config", "claude")), "codex": _list_dir(ctx, (*S, "config", "codex")),
            "home": home}


def check_discovery(ctx: H.Ctx, a: R.Alloc) -> None:
    """§5.5 through render's check. A Codex session's ``CODEX_HOME/config.toml`` passes only when its bytes equal
    the ones render wrote into the root layer (``<S>/root/codex.toml``, root-owned); a Claude session gets no
    exception (render pre-seeds no config.toml for it)."""
    listings = discovery_listings(ctx, a.session)
    kw: Dict[str, Optional[bytes]] = {}
    if a.vendor == "codex":
        with ctx.store.dir("live", a.session, "config", "codex", trusted=False) as fd:
            kw["codex_config"] = H.read_at(fd, "config.toml", f"live/{a.session}/config/codex/config.toml")
        kw["rendered_codex_config"] = ctx.store.read(("live", a.session, "root"), "codex.toml")
    RD.refuse_discovery(listings, **kw)


# --- the launch path (wall) ----------------------------------------------------------------------------------------

def check_vendor_installed(host: str, vendor: str, *, prefix: Optional[Path] = None, root_uid: int = 0) -> None:
    """Root's copy of the vendor's install unit in the vendor prefix can be run
    (:func:`levain.slot.root_tree.unit_problem`, the one predicate doctor reads too: the binary :func:`vendor_argv`
    execs: Claude Code's a regular file owned by root, never a link; Codex's root's link to one whole version of its
    package, every file of it root's): else
    ``vendor-not-installed:<vendor>``, so a missing copy is named before the clone instead of as the wall's
    ``exec-failed: errno 2`` after it. ``root_uid``: tests pass their own uid."""
    from levain.slot.root_tree import unit_problem

    problem = unit_problem(host, vendor, prefix=prefix, root_uid=root_uid)   # type: ignore[arg-type]
    if problem:
        raise Refused(f"vendor-not-installed:{vendor}",
                      f"{problem}; the install helper's setup or repair copies it there")


#: The check :func:`start` runs at its ``vendor`` step (tests replace it: their store has no vendor prefix).
vendor_ready: Callable[[str, str], None] = check_vendor_installed


def vendor_argv(host: str, vendor: str, session: str) -> Tuple[str, ...]:
    """The vendor's exec: its binary under the root-owned vendor prefix (§2.1). Claude Code reads the rendered
    ``--settings`` layer (render's ``<S>/root/settings.json``); Codex reads ``CODEX_HOME/config.toml`` and takes
    render's :data:`~levain.slot.render.CODEX_LAUNCH_ARGS` on its command line."""
    from levain.slot.layout import STORE
    from levain.slot.root_tree import VENDOR_PREFIX

    binary = f"{VENDOR_PREFIX[host]}/bin/{vendor}"
    if vendor == "claude":
        return (binary, "--settings", f"{STORE[host]}/live/{session}/root/settings.json")
    return (binary, *RD.CODEX_LAUNCH_ARGS)


def launch(ctx: H.Ctx, a: R.Alloc, spec: wall.Spec) -> Tuple[Tuple[str, ...], Optional[str]]:
    """The per-OS launch argv for ``spec`` and the cgroup its forked child joins (Linux; None on macOS)."""
    seams = ctx.seams
    if seams.host == "darwin":
        return wall.macos_launch_argv(spec), None
    pol = a.policy
    ll = wall.linux_launch_argv(spec, slot=a.slot, session=a.session, kind=a.kind,
                                queue_grant=bool(pol and pol.queue_grant), netns_names=seams.netns_names,
                                host_links=seams.host_links())
    if ll.cgroup is not None and ll.cgroup != a.cgroup:
        raise Refused("cgroup-mismatch", f"{ll.cgroup} is not alloc's {a.cgroup}")   # END kills alloc's
    return ll.argv, ll.cgroup


def _spec(ctx: H.Ctx, a: R.Alloc, target: str, **kw) -> wall.Spec:
    seams = ctx.seams
    return wall.launch_spec(seams.host, target, slot=a.slot, session=a.session, ids=H.slot_ids(ctx, a.slot),
                            shared={"levain-slots": seams.group_gid("levain-slots")}, kind=a.kind, **kw)


def secret_env(ctx: H.Ctx, a: R.Alloc) -> Dict[str, str]:
    """§3.2: ``SECRETS`` -> the vendor's extra env, under render's names for this session's policy (alloc's
    routes and hooks; the base map's route types). A name the policy carries that the answer lacks is refused
    ``secrets-missing:<name>``; a handle the policy does not name is never passed."""
    pol = a.policy
    assert pol is not None
    ans = ctx.call("SECRETS", session=a.session)
    facts = RD.RenderFacts(host=ctx.seams.host, session=a.session, slot=a.slot, kind=a.kind, vendor=a.vendor,
                           auth=a.auth, slot_uid=a.slot_uid, slot_gid=a.slot_gid,
                           route_types=H.base_route_types(ctx.store))
    names = RD.secret_env_names(RenderBlob(vendor=a.vendor, routes=pol.routes, hooks=pol.hooks), facts)
    out: Dict[str, str] = {}
    for name in names:
        value = ans.get(_SECRET_KEY.get(name, "token"))
        if value is None:
            raise Refused("secrets-missing:" + ("queue-cap" if name in _SECRET_KEY else "token"), name)
        if type(value) is not str or not value or len(value) > _SECRET_CAP \
                or not all(0x21 <= ord(ch) < 0x7f for ch in value):
            raise Refused("secrets-malformed", name)     # never the value
        out[name] = value
    return out


def _readable(fd: int, bound_s: float) -> bool:
    return bool(select.select([fd], [], [], max(bound_s, 0.0))[0])


def await_exec(fd: int, *, bound_s: float, clock: Callable[[], float] = time.monotonic) -> str:
    """The exec-marker pipe's answer, all within ONE bound: ``"exec"`` (``+``, then EOF: the last hop's copy is
    close-on-exec, so the pipe closes at the vendor's execve), ``"failed"`` (EOF or ``!`` first, or ``+`` followed by
    any byte: fail closed) or ``"timeout"`` (no ``+``-then-EOF in ``bound_s``: never taken as an exec)."""
    end = clock() + bound_s
    if not _readable(fd, end - clock()):
        return "timeout"
    if os.read(fd, 1) != wall.EXEC_OK:
        return "failed"
    if not _readable(fd, end - clock()):
        return "timeout"
    return "exec" if os.read(fd, 1) == b"" else "failed"


def _proc_seams(ctx: H.Ctx) -> PR.ProcSeams:
    return ctx.seams.proc_seams or PR.default_seams(ctx.seams.host)   # type: ignore[return-value]


def kill_domain(ctx: H.Ctx, a: R.Alloc, children: Sequence[int] = ()) -> Tuple[PR.Domain, frozenset]:
    """Every process of the session's kill domain ended and proven, and which of ``children`` (the start's own
    unreaped children) that reaped. Linux: proc.py's END kill, ``cgroup.kill`` on alloc's cgroup proven by
    ``populated 0`` (it reaps nothing). macOS: :func:`proc.end_domain` on the slot uid, which freezes first, boots the
    uid's launchd domain out and reaps ``children`` before its proof. Never only one pid: on Linux the pid the helper
    holds is bwrap's monitor, and the vendor is another process in the session's PID namespace."""
    seams = ctx.seams
    if seams.host == "linux":
        _, kill, _ = PR.end_seams("linux", a.cgroup, seams.proc_seams)   # type: ignore[arg-type]
        return (PR.PROVEN if kill(a.slot_uid, PR.V12_SECONDS) else PR.Domain(False, "kill-unproven")), frozenset()
    return PR.end_domain(a.slot_uid, PR.V12K_SECONDS, children, seams=_proc_seams(ctx))


def _abandon(ctx: H.Ctx, a: R.Alloc, pid: int, what: str) -> None:
    """A launch the start will not keep: its domain killed with ``pid`` as the kill's child (macOS reaps it before the
    proof: a zombie child of the start is a process of the uid until reaped), then ``seams.kill(pid)`` (SIGKILL + a
    reap polled for a bounded grace) ONLY for a pid that kill did not reap, its kill seam's own reap included: a
    reaped pid may already be another process's. The launched pid is one process across ``launchctl asuser``'s exec,
    the stage's drop and its exec of the target: root until the stage drops, the slot uid after. macOS:
    :func:`proc.end_domain` SIGKILLs it FIRST, before its freeze (still the start's unreaped child, so the pid cannot
    be reused), so a pid still root is ended before it can drop and fork, and the reap is never waited on to the
    deadline. A failure here is noted, never raised over the refusal the
    caller is about to raise.
    Runs under the session END lock (:func:`start`)."""
    reaped: frozenset = frozenset()
    try:
        reaped = _kill_quiet(ctx, a, what, (pid,))
    finally:
        if pid not in reaped:
            ctx.seams.kill(pid)


_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
#: A non-directory unlink on a name that is a directory: Linux says EISDIR, macOS EPERM.
_UNLINK_DIR = frozenset({errno.EISDIR, errno.EPERM})


def remove_tree_at(parent: int, name: str) -> None:
    """``name`` below the dirfd ``parent`` removed whole, every act relative to an fd and none following a link: a
    directory is opened ``O_DIRECTORY|O_NOFOLLOW`` and emptied through its own fd, anything else is unlinked (an
    open that meets a link fails, and the link itself is unlinked). A name that changes kind under the walk is
    taken as what it is now; a directory is never handed to ``unlink``. Root's 3.9 has no
    ``shutil.rmtree(dir_fd=)`` (3.11+), so this is the fd-relative remover, 3.9's calls only."""
    try:
        fd = os.open(name, _O_DIR, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        return
    try:
        with os.scandir(fd) as it:
            entries = [(ent.name, ent.is_dir(follow_symlinks=False)) for ent in it]
        for n, is_dir in entries:
            if is_dir:
                remove_tree_at(fd, n)
                continue
            try:
                os.unlink(n, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as e:
                if e.errno not in _UNLINK_DIR:
                    raise
                remove_tree_at(fd, n)        # it became a directory (or EPERM on a file: the open says ENOTDIR)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _fresh_repo(ctx: H.Ctx, a: R.Alloc, sfd: int) -> None:
    """``<S>/repo`` empty, slot-owned, 0700 (dispatch: 0711): whatever a failed or interrupted clone left there is removed first, by
    dirfd and never following a link (:func:`remove_tree_at`; ``<S>`` is root's: the slot can fill ``repo`` but
    never replace it)."""
    try:
        os.stat("repo", dir_fd=sfd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        remove_tree_at(sfd, "repo")
    # §26 S1-c: a dispatch session's repo is 0711, so the broker (search on <S> by its ACL) can walk into it.
    mode = 0o711 if a.kind == "dispatch" else 0o700
    ctx.store.mkdir(sfd, "repo", a.slot_uid, a.slot_gid, mode, f"live/{a.session}/repo")
    ctx.seams.fsync(sfd)


def _kill_quiet(ctx: H.Ctx, a: R.Alloc, what: str, children: Sequence[int] = ()) -> frozenset:
    """:func:`kill_domain` before a cleanup, answering the children it reaped; a failure is noted, never raised over
    the caller's refusal."""
    try:
        res, reaped = kill_domain(ctx, a, children)
    except Exception as e:   # noqa: BLE001 - the caller's refusal is the answer
        _note(f"{a.session}: {what}: kill domain failed: {type(e).__name__}")
        return frozenset()
    if not res.proven:
        _note(f"{a.session}: {what}: the kill domain was not proven empty: {res.name} {res.detail}".rstrip())
    return reaped


# --- the post-clone check (§5.4 step 3) --------------------------------------------------------------------------------

class CheckFailed(Exception):
    """A post-clone check that did not pass; the message names the half ((a), (b), the window) and the entry."""


#: DECISION: the read set's caps (§5.4, a ticket's read set: at most 512 paths of at most 1024 bytes each), restated
#: here because the root interpreter cannot import the dispatch package. A clone holding more entries, or a longer
#: path, fails the check.
READSET_PATHS_MAX, READSET_PATH_BYTES = 512, 1024
#: One ``ls-files -s -z`` record's fixed bytes: ``<mode 6> SP <sha 40> SP <stage 1> TAB`` and the closing NUL.
_LS_FIXED = 6 + 1 + 40 + 1 + 1 + 1 + 1
#: V9: the bound on ``ls-files``' stdout: the read set's caps, each path with its record's fixed fields.
LS_FILES_BOUND = READSET_PATHS_MAX * (READSET_PATH_BYTES + _LS_FIXED)
#: V9: ``hash-object`` prints one 40-hex id and a newline per path.
ID_LINE_BYTES = 41
#: The two index modes a read set holds, each with the owner execute bit its worktree file must carry.
_MODES = {"100644": False, "100755": True}
_LS_RECORD = re.compile(rb"([0-7]{6}) ([0-9a-f]{40}) ([0-3])\t(.+)", re.S)
_ID_LINE = re.compile(rb"[0-9a-f]{40}\n")
_O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


@dataclass(frozen=True)
class IndexEntry:
    mode: str
    sha: str
    stage: int
    path: str


@dataclass(frozen=True)
class CheckStats:
    """What a passed check covered (U6: the run records the bytes hashed and the batches)."""

    entries: int
    batches: int
    bytes_hashed: int


def _name(path: str) -> str:
    return repr(path if len(path) <= 96 else path[:96] + "...")


def parse_ls_files(out: bytes) -> List[IndexEntry]:
    """``git ls-files -s -z`` parsed by root: NUL-terminated ``<mode> SP <sha> SP <stage> TAB <path>`` records, so
    no git path quoting is ever read. A record of any other shape, or a path that is not UTF-8, fails."""
    if out and not out.endswith(b"\0"):
        raise CheckFailed("(b) ls-files: the output does not end in NUL")
    entries = []
    for rec in out.split(b"\0")[:-1]:
        m = _LS_RECORD.fullmatch(rec)
        if m is None:
            raise CheckFailed(f"(b) ls-files: a malformed record {rec[:96]!r}")
        if len(m.group(4)) > READSET_PATH_BYTES:
            raise CheckFailed(f"(b) ls-files: a path of {len(m.group(4))} bytes, past the read set's "
                              f"{READSET_PATH_BYTES}: {m.group(4)[:96]!r}")
        try:
            path = m.group(4).decode("utf-8")
        except UnicodeDecodeError:
            raise CheckFailed(f"(b) ls-files: a path that is not UTF-8 {m.group(4)[:96]!r}") from None
        entries.append(IndexEntry(m.group(1).decode(), m.group(2).decode(), int(m.group(3)), path))
    return entries


def check_index(entries: Sequence[IndexEntry]) -> None:
    """Every entry at stage 0 with mode ``100644`` or ``100755``: no symlink (120000), no gitlink (160000), no
    conflict stage."""
    for e in entries:
        if e.stage != 0 or e.mode not in _MODES:
            raise CheckFailed(f"(b) {_name(e.path)}: index mode {e.mode} stage {e.stage}")


def _lstat_beneath(repo_fd: int, path: str) -> os.stat_result:
    """``path`` lstat'ed from ``repo_fd``: every parent opened ``O_DIRECTORY|O_NOFOLLOW`` (no symlink parent), the
    entry itself never followed."""
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise CheckFailed(f"(b) {_name(path)}: not a plain relative path")
    fd = os.dup(repo_fd)
    try:
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as e:
                raise CheckFailed(f"(b) {_name(path)}: parent {comp!r} is not a plain directory "
                                  f"(errno {e.errno})") from None
            os.close(fd)
            fd = nfd
        try:
            return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except OSError as e:
            raise CheckFailed(f"(b) {_name(path)}: no worktree entry (errno {e.errno})") from None
    finally:
        os.close(fd)


def check_worktree(repo_fd: int, entries: Sequence[IndexEntry]) -> int:
    """Each entry's worktree path, lstat'ed fd-relative: a regular file whose owner execute bit equals its index
    mode's. Returns the bytes the hash will read."""
    total = 0
    for e in entries:
        st = _lstat_beneath(repo_fd, e.path)
        if not stat.S_ISREG(st.st_mode):
            raise CheckFailed(f"(b) {_name(e.path)}: the worktree entry is not a regular file "
                              f"(mode {stat.S_IFMT(st.st_mode):o})")
        if bool(st.st_mode & stat.S_IXUSR) != _MODES[e.mode]:
            raise CheckFailed(f"(b) {_name(e.path)}: exec bit {stat.S_IMODE(st.st_mode):o} vs index {e.mode}")
        total += st.st_size
    return total


def hash_batches(paths: Sequence[str], base: int, cap: int = wall.SPEC_CAP) -> List[List[str]]:
    """``paths`` in order, cut into ``hash-object`` batches whose ``clone-check`` spec serialises to at most ``cap``
    bytes (the stage's spec cap; V9: a fixed cap, never sysconf arithmetic). ``base``: that spec's size with the
    bare :data:`wall.HASH_OBJECT_PREFIX` argv; each path adds its JSON string, escaping included, and one comma,
    exactly as ``Spec.to_json`` writes the argv list."""
    out: List[List[str]] = []
    cur: List[str] = []
    size = base
    for p in paths:
        n = len(canon.canonical_bytes(p)) + 1
        if base + n > cap:
            raise CheckFailed(f"(b) {_name(p)}: one path past the {cap}-byte spec cap")
        if cur and size + n > cap:
            out.append(cur)
            cur, size = [], base
        cur.append(p)
        size += n
    if cur:
        out.append(cur)
    return out


def check_attributes(sfd: int) -> None:
    """(a): ``repo/.git/info/attributes`` opened fd-relative from the session dir fd, each directory
    ``O_NOFOLLOW``, the file ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY`` (a FIFO never blocks), then ``fstat``: a
    regular file whose bytes are render's line, exactly. A missing template clones with exit 0 and no such file."""
    want = RD.READSET_ATTRIBUTES
    fds = [sfd]
    try:
        for comp in ("repo", ".git", "info"):
            try:
                fds.append(os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fds[-1]))
            except OSError as e:
                raise CheckFailed(f"(a) {comp}: not a plain directory (errno {e.errno})") from None
        try:
            fd = os.open("attributes", _O_FILE, dir_fd=fds[-1])
        except OSError as e:
            raise CheckFailed(f"(a) info/attributes: absent or not a plain file (errno {e.errno})") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CheckFailed("(a) info/attributes: not a regular file")
            got = b""
            while len(got) <= len(want):   # one byte past the line proves it holds nothing more
                b = os.read(fd, len(want) + 1 - len(got))
                if not b:
                    break
                got += b
        finally:
            os.close(fd)
        if got != want:
            raise CheckFailed(f"(a) info/attributes: {got[:96]!r} is not the template's line")
    finally:
        for fd in fds[1:]:
            os.close(fd)


def _hash_ids(out: bytes, n: int) -> List[str]:
    if len(out) != ID_LINE_BYTES * n:
        raise CheckFailed(f"(b) hash-object: {len(out)} bytes for {n} paths")
    lines = [out[i:i + ID_LINE_BYTES] for i in range(0, len(out), ID_LINE_BYTES)]
    if not all(_ID_LINE.fullmatch(x) for x in lines):
        raise CheckFailed("(b) hash-object: a line that is not one id")
    return [x[:-1].decode() for x in lines]


def clone_check(sfd: int, kind: str, run: Callable[[Tuple[str, ...], int], bytes], no_process: Callable[[], bool],
                *, spec_base: Callable[[], int], cap: int = wall.SPEC_CAP) -> Optional[CheckStats]:
    """§5.4 step 3 after a zero clone exit, in order: for kind ``dispatch`` the slot uid has no process (V4); for
    ``dispatch|planner|verifier`` (a) :func:`check_attributes`; for ``dispatch`` only (V3) (b): ``ls-files -s -z``
    run as the slot (``run(argv, bound)`` returns its stdout or raises :class:`CheckFailed`), parsed and checked
    here, every worktree entry lstat'ed fd-relative, then ``hash-object --no-filters`` in batches whose spec is at
    most ``cap`` bytes (``spec_base()``: :func:`hash_batches`' base), every id equal to its entry's blob, in order;
    then no slot-uid process again (V4). Any failure raises :class:`CheckFailed`. ``interactive|login``: no check
    (None)."""
    if kind not in RD.READSET_KINDS:
        return None
    window = kind == "dispatch"
    if window and not no_process():
        raise CheckFailed("window: a slot-uid process is alive after the clone, before the check")
    check_attributes(sfd)
    if not window:
        return CheckStats(0, 0, 0)
    entries = parse_ls_files(run(wall.ls_files_argv(), LS_FILES_BOUND))
    if len(entries) > READSET_PATHS_MAX:
        raise CheckFailed(f"(b) ls-files: {len(entries)} entries, past the read set's {READSET_PATHS_MAX}")
    check_index(entries)
    try:
        repo = os.open("repo", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=sfd)
    except OSError as e:
        raise CheckFailed(f"(b) repo: not a plain directory (errno {e.errno})") from None
    try:
        size = check_worktree(repo, entries)
    finally:
        os.close(repo)
    batches = hash_batches([ent.path for ent in entries], spec_base(), cap)
    got: List[str] = []
    for batch in batches:
        got += _hash_ids(run(wall.hash_object_argv(batch), ID_LINE_BYTES * len(batch)), len(batch))
    for ent, h in zip(entries, got):
        if ent.sha != h:
            raise CheckFailed(f"(b) {_name(ent.path)}: blob {ent.sha[:12]} != worktree --no-filters {h[:12]}")
    if not no_process():
        raise CheckFailed("window: a slot-uid process is alive after the last check child")
    return CheckStats(len(entries), len(batches), size)


def _run_check(ctx: H.Ctx, a: R.Alloc, argv: Tuple[str, ...], bound: int) -> bytes:
    """One check child as the slot, by the clone's own launch path (wall's ``clone-check`` target), its stdout
    drained by root within ``bound``."""
    what = "ls-files" if argv == wall.ls_files_argv() else "hash-object"
    spec = _spec(ctx, a, "clone-check", argv=argv)
    largv, cgroup = launch(ctx, a, spec)
    try:
        code, out = ctx.seams.capture(largv, spec.to_json(), cgroup, bound)
    except Refused as e:
        raise CheckFailed(f"(b) {what}: {e.name}" + (f" ({e.detail})" if e.detail else "")) from None
    if code != 0:
        raise CheckFailed(f"(b) {what}: exit {code}")
    return out


def _hash_spec_base(ctx: H.Ctx, a: R.Alloc) -> int:
    """The ``clone-check`` spec's serialised size with the bare ``hash-object`` prefix as its argv: every field the
    batch's spec carries besides its paths (:func:`hash_batches`)."""
    spec = _spec(ctx, a, "clone-check", argv=wall.ls_files_argv())
    return len(replace(spec, argv=wall.HASH_OBJECT_PREFIX).to_json())


def _no_slot_process(ctx: H.Ctx, a: R.Alloc) -> bool:
    """proc.py's END proof, unchanged (Linux the session cgroup; macOS the uid sweep): no process of the slot uid."""
    seams = ctx.seams
    host = seams.host
    _, _, no_process = PR.end_seams(host, a.cgroup if host == "linux" else None,  # type: ignore[arg-type]
                                    seams.proc_seams)   # type: ignore[arg-type]
    return no_process(a.slot_uid)


def _discard_clone(ctx: H.Ctx, a: R.Alloc, what: str) -> Optional[str]:
    """The kill domain killed, then ``<S>/repo`` emptied (so the start can be retried). Never raises an Exception
    over the caller's error: a failure to empty is noted and returned (the next start's own ``_fresh_repo``
    removes what is left; no clone marker is written either way)."""
    _kill_quiet(ctx, a, what)   # macOS: a stray slot-uid process could still write into repo
    try:
        with ctx.store.dir("live", a.session) as sfd:
            _fresh_repo(ctx, a, sfd)
    except Exception as e:   # noqa: BLE001 - the caller's error is the answer
        why = f"{type(e).__name__}" + (f" errno {e.errno}" if isinstance(e, OSError) else "")
        _note(f"{a.session}: {what}: <S>/repo was not emptied: {why}")
        return why
    return None


def _clone_failed(ctx: H.Ctx, a: R.Alloc, detail: str) -> None:
    """The ``clone-failed`` path: :func:`_discard_clone`, no clone marker; then the refusal, naming a repo left
    unemptied."""
    why = _discard_clone(ctx, a, "clone-failed")
    raise Refused("clone-failed", detail if why is None else f"{detail}; <S>/repo not emptied ({why})")


# --- start (§5.1) ------------------------------------------------------------------------------------------------------

def start(ctx: H.Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    assert req.session is not None
    seams = ctx.seams
    a = H.read_alloc(ctx, req.session)
    if a.policy is None:
        raise Refused("not-rendered", a.session)
    seams.at("kind")
    if a.kind == "verifier":
        raise Refused("verb-unbuilt:verifier", "a verifier session's start is §5.6, session 2f's")
    if a.auth == "passthrough" and a.kind == "interactive":
        raise Refused("verb-unbuilt:identity-gate", "the keep/ return + the §7.6 gate are session 2f's")
    seams.at("vendor")
    vendor_ready(seams.host, a.vendor)
    egress = a.slot.slot_class == "egress"
    seams.at("discovery")
    check_discovery(ctx, a)
    if egress and seams.host == "darwin":
        seams.at("egress-ready")
        seams.egress_ready(a.slot, H.slot_ids(ctx, a.slot))

    seams.at("repo")
    try:
        with H.session_lock(ctx, a.session):
            pid = _start_fenced(ctx, a)
    finally:
        _wake(seams)        # every release: an END that waited on the lock, or refused end-busy, is retried now
    seams.at("wait")
    code = seams.wait(pid)
    if a.kind == "login":
        seams.at("identity")
        try:
            ID.record_login_identity(ctx, a, code)
        except Refused as e:
            # The login ran; what it observed is not recorded, so confirm-identity yes refuses no-observed-id.
            _note(f"identity of {a.session} not recorded: {e.name}")
    return {"exit": code}


def _start_fenced(ctx: H.Ctx, a: R.Alloc) -> int:
    """``start`` from the repo step through the ACTIVE CAS, under the session END lock (``L/alloc/<session>.lock``,
    the one render takes and END waits on; reaper DESIGN RH1, the spawn fence). Every spawn into the slot (the
    clone, its check children, the forwarder, the vendor) and every kill this start makes run under it, so an END
    cannot prove, and SCRUB cannot release the slot, while a launch is in flight or being abandoned; and a start that
    takes the lock after an END re-reads what END changed and refuses. Released right after the CAS's answer: the
    vendor wait never holds it. Returns the vendor's pid."""
    seams = ctx.seams
    egress = a.slot.slot_class == "egress"
    if H.read_alloc(ctx, a.session) != a:                  # END or the reaper ran between the unlocked read and here
        raise Refused("not-rendered", f"{a.session}: the alloc changed")
    rec = read_record(ctx, a.session)
    if E.cloned_path(ctx.store.path, a.session).exists() or (rec is not None and rec.state in (R.ACTIVE, R.BRAKED)):
        # DECISION: a second start never clones twice. "Started" is what root and CONTROL recorded (ACTIVE or
        # BRAKED, or root's clone marker: a repo a vendor may already have worked in), never the dir's existence.
        raise Refused("already-started", a.session)
    if rec is None or rec.state != R.RENDERED:
        raise Refused("not-rendered", f"{a.session}: {rec.state if rec else 'no record'}")   # SECRETS' own rule
    with ctx.store.dir("live", a.session) as sfd:
        _fresh_repo(ctx, a, sfd)

    seams.at("clone")
    spec = _spec(ctx, a, "clone")
    argv, cgroup = launch(ctx, a, spec)
    if cgroup is not None:
        seams.make_cgroup(cgroup)
    try:
        code = seams.wait_within(seams.spawn(argv, spec.to_json(), cgroup, None, False), CLONE_S)
    except Refused as e:
        if e.name != "child-timeout":
            raise
        _clone_failed(ctx, a, "timeout")
    if code != 0:
        _clone_failed(ctx, a, f"exit {code}")
    if a.kind in RD.READSET_KINDS:
        seams.at("clone-check")
        passed, detail, why = False, "", None
        try:
            with ctx.store.dir("live", a.session) as sfd:
                clone_check(sfd, a.kind, lambda av, bound: _run_check(ctx, a, av, bound),
                            lambda: _no_slot_process(ctx, a), spec_base=lambda: _hash_spec_base(ctx, a))
            passed = True
        except (CheckFailed, Refused, OSError) as err:
            # A listing or launch refusal inside the check is a failed check too: the clone is never kept unproven.
            detail = str(err) if isinstance(err, CheckFailed) else (
                f"check: {err.name}" if isinstance(err, Refused) else f"check: {type(err).__name__} errno {err.errno}")
        finally:
            # Any other error (an internal bug, an interrupt) propagates as itself: the discard never raises over it.
            if not passed:
                why = _discard_clone(ctx, a, "clone-check")
        if not passed:
            raise Refused("clone-failed",
                          detail[:512] if why is None else f"{detail[:448]}; <S>/repo not emptied ({why})")
    E.mark_cloned(ctx.store.path, a.session)

    if egress and seams.host == "linux":
        seams.at("forwarder")
        spec = _spec(ctx, a, "forwarder")
        argv, cgroup = launch(ctx, a, spec)
        ready_r, ready_w = os.pipe()
        fpid: Optional[int] = None
        try:
            try:
                fpid = seams.spawn(argv, spec.to_json(), cgroup, ready_w, True)   # lives until the helper exits
            finally:
                os.close(ready_w)
            ready = _readable(ready_r, FORWARDER_READY_S) and os.read(ready_r, 1) == wall.EXEC_OK
        except BaseException:
            if fpid is not None:
                _abandon(ctx, a, fpid, "forwarder-not-ready")      # spawned, then the ready read failed
            raise
        finally:
            os.close(ready_r)
        if not ready:
            _abandon(ctx, a, fpid, "forwarder-not-ready")
            raise Refused("forwarder-not-ready", f"no ready byte within {FORWARDER_READY_S:g}s")

    seams.at("secrets")
    handles = secret_env(ctx, a)

    seams.at("exec")
    spec = _spec(ctx, a, "vendor", argv=vendor_argv(seams.host, a.vendor, a.session), extra_env=handles,
                 exec_fd=wall.EXEC_FD)
    argv, cgroup = launch(ctx, a, spec)
    exec_r, exec_w = os.pipe()
    try:
        try:
            pid = seams.spawn(argv, spec.to_json(), cgroup, exec_w, False)
        finally:
            os.close(exec_w)
        got = await_exec(exec_r, bound_s=EXEC_MARKER_S)
    finally:
        os.close(exec_r)
    if got == "timeout":
        _abandon(ctx, a, pid, "exec-failed")
        raise Refused("exec-failed", f"no exec within {EXEC_MARKER_S:g}s")
    if got != "exec":
        # DECISION: under the lock no wait is unbounded (seat ruling r2-2), so a failed marker is abandoned like a
        # timeout (the domain killed, the pid reaped) rather than waited on for its exit status.
        _abandon(ctx, a, pid, "exec-failed")
        raise Refused("exec-failed", "the exec marker failed")

    try:
        seams.at("active")
        gen = H.state_generation(ctx, a.session)
        ctx.call("CAS", session=a.session, generation=gen, **{"from": R.RENDERED, "to": R.ACTIVE}, fields={})
    except BaseException:
        # A vendor that exec'd but is not recorded ACTIVE (the lease died, the record moved, anything between the
        # exec and the CAS) is killed with its whole domain, then its pid reaped.
        _abandon(ctx, a, pid, "active-refused")
        raise
    return pid


def _wake(seams: H.Seams) -> None:
    """Best-effort: a wake that fails is noted, never the verb's answer (it runs in a ``finally``, so raising would
    mask an exception already leaving the lock, or fail a start whose vendor already runs)."""
    try:
        if seams.wake_reaper is not None:
            seams.wake_reaper()
        else:
            H.wake_reaper(seams.R)
    except Exception as e:   # noqa: BLE001 - a wake never changes the verb's answer
        _note(f"reaper wake failed: {type(e).__name__}")


# --- teardown (§7.4 END, §7.3 SCRUBBED) ----------------------------------------------------------------------------

def _note(msg: str) -> None:
    try:
        sys.stderr.write(f"levain runtime-helper: {msg}\n")
    except Exception:   # noqa: BLE001 - a note never changes the verb's answer
        pass


def end_facts(ctx: H.Ctx, a: R.Alloc) -> E.EndFacts:
    """END's facts from alloc. DECISION (macOS): the ``/var/folders`` END retains is the session owner's (egress:
    the slot; verifier: the child, who owns its work), alloc's first ``var_folders`` path, read at prepare: END
    never asks getconf, which runs as the uid and would re-create a dir END already moved (D1)."""
    var = None
    if ctx.seams.host == "darwin":
        if a.var_folders is None:
            raise Refused("var-folders-unknown", a.session)
        var = Path(a.var_folders[0])
    return E.facts_from_alloc(R.alloc_record(a), store=ctx.store.path, var_folders=var)


def end_seams(ctx: H.Ctx, a: R.Alloc) -> E.EndSeams:
    """END's seams: proc.py's freeze / kill / proof (Linux: the session cgroup alloc names; macOS: the uid sweep),
    and a CAS that reads the record's generation and state as its hint on every call (end.send_cas re-reads on a
    stale generation)."""
    seams = ctx.seams
    freeze, kill, no_process = PR.end_seams(seams.host, a.cgroup if seams.host == "linux" else None,
                                            seams.proc_seams)   # type: ignore[arg-type]

    def cas(target: str, fields: Mapping[str, object]) -> None:
        rec = read_record(ctx, a.session)
        if rec is None:
            raise Refused("no-session", a.session)
        ctx.call("CAS", session=a.session, generation=rec.generation, **{"from": rec.state, "to": target},
                 fields=dict(fields))

    es = E.EndSeams(cas=cas, freeze=freeze, kill=kill, no_process=no_process, note=_note)
    return replace(es, **seams.end_overrides) if seams.end_overrides else es


def revoke(ctx: H.Ctx, session: str) -> None:
    """§7.3 ``... -> REVOKED`` (teardown): a live record is CASed to REVOKED; an alloc with no record gets
    ``REVOKE`` (CONTROL creates it at REVOKED, §3.1). A record already past live is left as it is. DECISION: a CAS
    the lease's own EOF beat (the record moved under it) is re-read once."""
    for _ in range(2):
        rec = read_record(ctx, session)
        if rec is None:
            ctx.call("REVOKE", session=session)
            return
        if rec.state not in R.LIVE_STATES:
            return
        try:
            ctx.call("CAS", session=session, generation=rec.generation, **{"from": rec.state, "to": R.REVOKED},
                     fields={})
            return
        except Refused as e:
            if e.name not in ("stale-generation", "lease-dead"):
                raise
    rec = read_record(ctx, session)
    if rec is not None and rec.state in R.LIVE_STATES:
        raise Refused("revoke-raced", session)


def _drop_unretained_live(ctx: H.Ctx, session: str) -> None:
    """``L/live/<session>`` left after END reached RETAINED: a session that never cloned retains nothing (END
    renames a cloned ``<S>`` into the retained tree), so what is still there is the rendered layer and the slot's
    empty dirs [measured: Linux L62 (20+ such dirs) and macOS M69 left it]. Removed by dirfd, never following a
    link, once every process of the slot is proven gone and before the slot is reusable. Only with no retained tree
    for the session: END's rename moves all of ``<S>``, so ``<S>`` beside a tree is not this case and is kept."""
    with ctx.store.dir("live") as live:
        try:
            os.stat(session, dir_fd=live, follow_symlinks=False)
        except FileNotFoundError:
            return
        if os.path.lexists(ctx.store.path / "retained" / session / "tree"):
            _note(f"scrub {session}: L/live/{session} kept beside its retained tree")
            return
        remove_tree_at(live, session)
        ctx.seams.fsync(live)


#: ``slots/<tag>/front.lock`` (root 0600): serialises a front's start and every sweep of the slot's gateway uid
#: (reaper DESIGN §4.7). No holder of it takes any other lock.
FRONT_LOCK = "front.lock"
#: ``slots/<tag>/front-revoked`` (root 0600): the lease (``session``, ``lease_seq``) whose front the revoker ended, for
#: a front start of that lease to refuse ``front-revoked`` on.
FRONT_REVOKED = "front-revoked"
#: DECISION (reaper DESIGN RH6): SCRUB's wait for ``front.lock``, above the revoker's bound, so a SCRUB queued behind
#: a revoker that ran to its bound does not time out.
FRONT_LOCK_WAIT_S = 7.0
#: §4.5: the revoker's bound on ending a front.
REVOKE_FRONT_S = 5.0


@contextmanager
def front_lock(ctx: H.Ctx, slot, wait_s: Optional[float]) -> Iterator[None]:
    """The slot's ``front.lock`` (made when absent, root 0600), TRIED (``wait_s`` None: held = ``front-busy``) or
    waited for up to ``wait_s`` (else ``front-locked``)."""
    seams = ctx.seams
    with ctx.store.dir("slots", slot.tag) as sfd:
        try:
            fd = os.open(FRONT_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600,
                         dir_fd=sfd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO):
                raise Refused("bad-root", f"slots/{slot.tag}/{FRONT_LOCK} is a link or not a file") from None
            raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.trusted(st):
            raise Refused("bad-root", f"slots/{slot.tag}/{FRONT_LOCK}")
        if wait_s is None:
            if not H.try_flock(fd):
                raise Refused("front-busy", slot.tag)
        else:
            deadline = seams.monotonic() + wait_s
            delay = H.LOCK_SLICE_S
            while not H.try_flock(fd):
                if seams.monotonic() >= deadline:
                    raise Refused("front-locked", f"slots/{slot.tag}/{FRONT_LOCK} held past {wait_s:g} s")
                seams.sleep(delay)
                delay = min(delay * 2, 0.5)
        yield
    finally:
        os.close(fd)


def _slot_state(ctx: H.Ctx, slot) -> Optional[R.SlotState]:
    raw = ctx.store.read(("slots", slot.tag), "state")
    return H.parse_slot_state_of(raw, slot) if raw is not None else None


def _names_lease(st: Optional[R.SlotState], a: R.Alloc) -> bool:
    return st is not None and st.state == "leased" and st.session == a.session and st.lease_seq == a.lease_seq


def _marker(a: R.Alloc) -> bytes:
    return canon.canonical_bytes({"lease_seq": a.lease_seq, "session": a.session})


def scrub_and_release(ctx: H.Ctx, a: R.Alloc) -> Optional[bool]:
    """§7.3 ``slot: LEASED -> SCRUBBED -> REUSABLE``. Under the SLOT lock only, and only while root's slot state file
    shows this slot leased to THIS session at alloc's ``lease_seq``: proc.scrub (macOS: :func:`proc.end_domain` on
    every uid of the slot, the gateway's under ``front.lock`` waited for up to :data:`FRONT_LOCK_WAIT_S`; Linux: the
    gateway uid SIGKILLed, then none of the slot's uids may own a process), then this lease's ``front-revoked``
    marker removed under ``front.lock``; then a TRY of the pool lock (§3.5: the pool lock is never held across
    SCRUBBED's kills; held by a ``prepare`` that only TRIES slot locks, so a try is the order's retake path) and,
    with the state file unchanged since the check, the file back to ``reusable`` with ``rev + 1``, no session.

    Returns whether it released the slot. A pool TRY that fails releases the slot lock and returns ``False``: the
    slot stays leased to a RETAINED session with no process, which the reaper's pass rule ("leased + RETAINED + alloc
    -> SCRUB again") owns. DECISION: a slot that has already left this lease (a second teardown, the reaper first) is
    not touched and not refused, and answers ``None`` (nothing of this lease was left to release). A verifier pair has no gateway uid: its child takes that place, with no
    ``front.lock`` (it has no front)."""
    seams = ctx.seams
    slot = a.slot
    ids = H.slot_ids(ctx, slot)
    first, second = slot.accounts
    egress = slot.slot_class == "egress"
    with ctx.store.dir("slots", slot.tag) as sfd:
        lock = H._open_lock(sfd, "lock", f"slots/{slot.tag}/lock")
    try:
        H.wait_flock(lock, seams, "slot-locked")
        st = _slot_state(ctx, slot)
        if not _names_lease(st, a):
            return None
        seams.at("scrubbed")
        gw_lock = (lambda: front_lock(ctx, slot, FRONT_LOCK_WAIT_S)) if egress else None
        PR.scrub(ids[first], ids[second], seams=seams.proc_seams, gw_lock=gw_lock)   # type: ignore[arg-type]
        if egress:
            with front_lock(ctx, slot, FRONT_LOCK_WAIT_S):
                if ctx.store.read(("slots", slot.tag), FRONT_REVOKED) == _marker(a):
                    with ctx.store.dir("slots", slot.tag) as sfd:
                        os.unlink(FRONT_REVOKED, dir_fd=sfd)
                        seams.fsync(sfd)
        _drop_unretained_live(ctx, a.session)
        with ctx.store.dir("slots") as slots_fd:
            pool = H._open_lock(slots_fd, "pool.lock", "slots/pool.lock")
        try:
            seams.at("pool")
            if not H.try_flock(pool):
                _note(f"scrub {a.session}: the pool lock is held; the slot stays leased for the reaper's next pass")
                return False
            if _slot_state(ctx, slot) != st:
                return False
            assert st is not None
            new = replace(st, rev=st.rev + 1, state="reusable", session=None, changed_at=R.timestamp(seams.clock()))
            seams.at("reusable")
            ctx.store.replace(("slots", slot.tag), "state", R.slot_state_bytes(new), mode=0o644)
        finally:
            os.close(pool)
    finally:
        os.close(lock)
    _wake(seams)                               # the Pool sync (§3.5): CONTROL's mirror learns the slot is free
    return True


def revoke_front(ctx: H.Ctx, a: R.Alloc) -> str:
    """The revoker's front kill at REVOKED (reaper DESIGN §4.7): every process of the slot's gateway uid, never only
    ``front.pid`` (a front that forked or setsid'd escapes a pid). Under ``front.lock``, TRIED (held = ``front-busy``:
    a front start or SCRUB's gateway sweep has it; the revoker tries again on its next wake), the slot state must
    still name this lease (else ``front-lease-moved``, nothing signalled); then the durable ``front-revoked`` marker
    naming the lease is written BEFORE the sweep, for a front start of this lease to refuse on (that check, and the
    front start's own hold of ``front.lock``, are render's, session 3a's); then :func:`proc.end_domain` on the
    gateway uid within :data:`REVOKE_FRONT_S`.
    Answers ``"front-ended"``; Unproven = ``front-unproven:<step>`` (the session stays REVOKED and SCRUB ends the uid
    again). A slot with no front (class ``none``) refuses ``no-front``."""
    slot = a.slot
    if slot.slot_class != "egress":
        raise Refused("no-front", slot.tag)
    gw = H.slot_ids(ctx, slot)[slot.accounts[1]]
    with front_lock(ctx, slot, None):
        if not _names_lease(_slot_state(ctx, slot), a):
            raise Refused("front-lease-moved", f"{slot.tag} no longer names {a.session} at lease {a.lease_seq}")
        ctx.seams.at("front-revoked")
        ctx.store.replace(("slots", slot.tag), FRONT_REVOKED, _marker(a), mode=0o600)
        res, _ = PR.end_domain(gw, REVOKE_FRONT_S, seams=_proc_seams(ctx))
    if not res.proven:
        raise Refused(f"front-unproven:{res.name}", res.detail)
    return "front-ended"


def teardown(ctx: H.Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """END (§7.4) then SCRUBBED. ``{ok, state}`` (§5.1's Out column) with the state END left the record in:
    ``RETAINED``, or ``HELD`` (the slot stays leased; ``failed_step`` is in the record, CONTROL's STATUS shows it).
    A RETAINED end whose SCRUB lost the pool TRY (the slot stays leased to this session) adds ``released: false``
    (H7); a slot that had already left this lease answers as before.
    DECISION: a HELD end answers ``ok`` with ``state:"HELD"`` (the Out column's shape carries the state for this),
    while a refusal (END busy, a CAS CONTROL never took, processes surviving SCRUBBED) answers ``ok:false``."""
    assert req.session is not None
    seams = ctx.seams
    a = H.read_alloc(ctx, req.session)
    seams.at("revoke")
    revoke(ctx, a.session)
    seams.at("end")
    rec = read_record(ctx, a.session)
    if rec is None:
        raise Refused("no-session", a.session)
    try:
        out = E.end_session(end_facts(ctx, a), end_seams(ctx, a), record_state=rec.state,
                            failed_step=rec.fields.get("failed_step"),            # type: ignore[arg-type]
                            held_attempts=rec.fields.get("held_attempts"))        # type: ignore[arg-type]
    except E.CasNotSent as e:
        raise Refused("end-cas-not-sent", str(e)[:256]) from None
    if out != R.RETAINED:
        return {"state": out}
    seams.at("scrub")
    if scrub_and_release(ctx, a) is False:
        # The record is RETAINED either way; the slot stays leased to it (the pool TRY lost), and the reaper's pass
        # owns the next SCRUB.
        return {"state": R.RETAINED, "released": False}
    return {"state": R.RETAINED}


__all__ = ["CheckFailed", "CheckStats", "EXEC_MARKER_S", "FORWARDER_READY_S", "FRONT_LOCK", "FRONT_LOCK_WAIT_S",
           "FRONT_REVOKED", "ID_LINE_BYTES", "IndexEntry", "LS_FILES_BOUND", "READSET_PATHS_MAX", "READSET_PATH_BYTES",
           "REVOKE_FRONT_S", "await_exec", "check_attributes", "check_discovery", "check_index", "check_worktree",
           "check_vendor_installed", "clone_check",
           "discovery_listings", "end_facts", "end_seams", "front_lock", "hash_batches", "kill_domain",
           "launch", "parse_ls_files", "read_record", "remove_tree_at", "revoke", "revoke_front", "scrub_and_release",
           "secret_env", "start", "teardown", "vendor_argv"]
