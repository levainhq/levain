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
2. ``discovery``: a planted §5.5 name under ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME`` or ``HOME`` is refused by name,
   from listings read through dirfds (render's check decides).
3. ``egress-ready`` (macOS egress): wall's live pf anchor + alias check, refused by its name.
4. ``repo``: ``already-started`` when the record is ACTIVE or BRAKED or root's clone marker exists (never the dir's
   existence), ``not-rendered`` when it is not RENDERED; else root makes ``<S>/repo`` empty (a dir a failed or
   interrupted clone left is removed first), slot-owned, 0700 (the slot cannot create it in a root-owned ``<S>``).
5. ``clone``: ``git clone`` from ``<S>/in/repo.bundle`` AS THE SLOT through wall's clone target (the same path as
   the vendor); a non-zero exit = ``clone-failed``, state unchanged, the session's kill domain killed and then
   ``<S>/repo`` emptied again, so the start can be retried. Success = end's ``mark_cloned``.
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

``teardown`` = END (§7.4): CAS live -> REVOKED, the END driver (end.py) with proc.py's real freeze / kill / proof,
then SCRUBBED (proc.scrub over every uid of the slot) and the slot's state file back to ``reusable`` (rev + 1, under
the pool lock). ``{ok, state}``.
"""
from __future__ import annotations

import errno
import os
import select
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from levain.slot import Refused, wall
from levain.slot import end as E
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


# --- small reads ------------------------------------------------------------------------------------------------------

def read_record(ctx: H.Ctx, session: str) -> Optional[R.SessionState]:
    """The state record as a HINT (§7.1: generation + ``from`` for a CAS CONTROL checks), or None when absent."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    return R.parse_state(data) if data is not None else None


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

def vendor_argv(host: str, vendor: str, session: str) -> Tuple[str, ...]:
    """The vendor's exec: its binary under the root-owned vendor prefix (§2.1). Claude Code reads the rendered
    ``--settings`` layer (render's ``<S>/root/settings.json``); Codex reads ``CODEX_HOME/config.toml``."""
    from levain.slot.layout import STORE
    from levain.slot.root_tree import VENDOR_PREFIX

    binary = f"{VENDOR_PREFIX[host]}/bin/{vendor}"
    if vendor == "claude":
        return (binary, "--settings", f"{STORE[host]}/live/{session}/root/settings.json")
    return (binary,)


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
                            shared={"levain-slots": seams.group_gid("levain-slots")}, **kw)


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


def kill_domain(ctx: H.Ctx, a: R.Alloc) -> bool:
    """Every process of the session's kill domain, by proc.py's END kill (Linux: ``cgroup.kill`` on alloc's cgroup,
    proven by ``populated 0``; macOS: SIGKILL sweeps of the slot uid). Never only one pid: on Linux the pid the
    helper holds is bwrap's monitor, and the vendor is another process in the session's PID namespace."""
    seams = ctx.seams
    _, kill, _ = PR.end_seams(seams.host, a.cgroup if seams.host == "linux" else None,
                              seams.proc_seams)   # type: ignore[arg-type]
    return kill(a.slot_uid, PR.V12_SECONDS)


def _abandon(ctx: H.Ctx, a: R.Alloc, pid: int, what: str) -> None:
    """A launch the start will not keep: its domain killed, then its pid reaped (``seams.kill``: SIGKILL + waitpid).
    A failure here is noted, never raised over the refusal the caller is about to raise."""
    try:
        _kill_quiet(ctx, a, what)
    finally:
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
    """``<S>/repo`` empty, slot-owned, 0700: whatever a failed or interrupted clone left there is removed first, by
    dirfd and never following a link (:func:`remove_tree_at`; ``<S>`` is root's: the slot can fill ``repo`` but
    never replace it)."""
    try:
        os.stat("repo", dir_fd=sfd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        remove_tree_at(sfd, "repo")
    ctx.store.mkdir(sfd, "repo", a.slot_uid, a.slot_gid, 0o700, f"live/{a.session}/repo")
    ctx.seams.fsync(sfd)


def _kill_quiet(ctx: H.Ctx, a: R.Alloc, what: str) -> None:
    """:func:`kill_domain` before a cleanup; a failure is noted, never raised over the caller's refusal."""
    try:
        if not kill_domain(ctx, a):
            _note(f"{a.session}: {what}: the kill domain was not proven empty within V12")
    except Exception as e:   # noqa: BLE001 - the caller's refusal is the answer
        _note(f"{a.session}: {what}: kill domain failed: {type(e).__name__}")


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
    egress = a.slot.slot_class == "egress"
    seams.at("discovery")
    check_discovery(ctx, a)
    if egress and seams.host == "darwin":
        seams.at("egress-ready")
        seams.egress_ready(a.slot, H.slot_ids(ctx, a.slot))

    seams.at("repo")
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
    code = seams.wait(seams.spawn(argv, spec.to_json(), cgroup, None, False))
    if code != 0:
        _kill_quiet(ctx, a, "clone-failed")   # macOS: a stray slot-uid process could still write into repo
        with ctx.store.dir("live", a.session) as sfd:
            _fresh_repo(ctx, a, sfd)          # emptied, so the start can be retried
        raise Refused("clone-failed", f"exit {code}")
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
        code = seams.wait(pid)
        raise Refused("exec-failed", f"exit {code}")

    try:
        seams.at("active")
        gen = H.state_generation(ctx, a.session)
        ctx.call("CAS", session=a.session, generation=gen, **{"from": R.RENDERED, "to": R.ACTIVE}, fields={})
    except BaseException:
        # A vendor that exec'd but is not recorded ACTIVE (the lease died, the record moved, anything between the
        # exec and the CAS) is killed with its whole domain, then its pid reaped.
        _abandon(ctx, a, pid, "active-refused")
        raise
    seams.at("wait")
    return {"exit": seams.wait(pid)}


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


def scrub_and_release(ctx: H.Ctx, a: R.Alloc) -> bool:
    """§7.3 ``slot: LEASED -> SCRUBBED -> REUSABLE``, under the pool lock then the slot lock (§3.5 order): only
    while root's slot state file shows this slot leased to THIS session at alloc's ``lease_seq``, proc.scrub
    (every process of the gateway uid SIGKILLed, then none of the slot's uids may own one), then the file back to
    ``reusable`` with ``rev + 1``, no session. Returns whether it released the slot. DECISION: a slot that has
    already left this lease (a second teardown, the reaper first) is not touched and not refused. A verifier
    pair has no gateway uid: its child takes that place (killed, then proven gone with the driver)."""
    seams = ctx.seams
    slot = a.slot
    ids = H.slot_ids(ctx, slot)
    first, second = slot.accounts
    with ctx.store.dir("slots") as slots_fd:
        pool = H._open_lock(slots_fd, "pool.lock", "slots/pool.lock")
        try:
            H.wait_flock(pool, seams, "pool-locked")
            with ctx.store.dir("slots", slot.tag) as sfd:
                lock = H._open_lock(sfd, "lock", f"slots/{slot.tag}/lock")
            try:
                H.wait_flock(lock, seams, "slot-locked")
                raw = ctx.store.read(("slots", slot.tag), "state")
                st = H.parse_slot_state_of(raw, slot) if raw is not None else None
                if st is None or st.state != "leased" or st.session != a.session or st.lease_seq != a.lease_seq:
                    return False
                seams.at("scrubbed")
                PR.scrub(ids[first], ids[second], seams=seams.proc_seams)   # type: ignore[arg-type]
                new = replace(st, rev=st.rev + 1, state="reusable", session=None,
                              changed_at=R.timestamp(seams.clock()))
                seams.at("reusable")
                ctx.store.replace(("slots", slot.tag), "state", R.slot_state_bytes(new), mode=0o644)
            finally:
                os.close(lock)
        finally:
            os.close(pool)
    if seams.wake_reaper is not None:          # the Pool sync (§3.5): CONTROL's mirror learns the slot is free
        seams.wake_reaper()
    else:
        H.wake_reaper(seams.R)
    return True


def teardown(ctx: H.Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """END (§7.4) then SCRUBBED. ``{ok, state}`` (§5.1's Out column) with the state END left the record in:
    ``RETAINED``, or ``HELD`` (the slot stays leased; ``failed_step`` is in the record, CONTROL's STATUS shows it).
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
    scrub_and_release(ctx, a)
    return {"state": R.RETAINED}


__all__ = ["EXEC_MARKER_S", "FORWARDER_READY_S", "await_exec", "check_discovery", "discovery_listings", "end_facts",
           "end_seams", "kill_domain", "launch", "read_record", "remove_tree_at", "revoke", "scrub_and_release",
           "secret_env", "start", "teardown", "vendor_argv"]
