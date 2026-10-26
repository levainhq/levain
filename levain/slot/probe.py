"""The runtime helper's ``probe <session>``: V13, the firewall self-probe (BUILD_PLAN §2c 9, §2d 10; design
``1025-6/v13/DESIGN_V13_REV2.md`` with head rulings R1-R8, O1-O4 and R3's build conditions N1-N10).

V13 is a tripwire for the loss of the slot's network wall: one TCP connect per address to the vendor's own upstream,
made as the slot uid inside its wall, judged against the same connects made by root with no wall. It is not a proof
of the whole B3 policy (R7): no UDP or ICMP is probed.

:func:`probe`, in order (each step named for ``seams.at``):

1. ``probe-read``: alloc, unlocked; the record state RENDERED or ACTIVE, class ``egress``, kind ``interactive`` or
   ``login``, else ``not-probeable:<x>``. The session's one auth route is read from ``L/routes/base.json`` through
   keys' own base-map parser (N9).
2. ``probe-resolve``: the ordered host tuple (:func:`probe_hosts`, R4 + O3) resolved in parallel inside
   :data:`RESOLVE_S` (N1), every A and AAAA answer, N8's filter applied, de-duplicated in order, at most
   :data:`MAX_PROBE_ADDRS`.
3. ``probe-control``: CONTROL, root's own connects to every address (``probe_child.connect_all``).
4. ``probe-lock``: the session END lock (``runtime_helper.session_lock``, a TRY-lock: held = ``session-busy``). Under
   it alloc is re-read (changed = ``not-probeable:alloc-changed``), the record state re-checked and the slot state
   must still name this lease (``slot-not-ours``); then ``probe-spawn``: the child through the wall's ``probe`` target
   (:data:`levain.slot.wall.PROBE_MODULE`), its result read to EOF, the child's exit seen WITHOUT reaping it, its
   process GROUP killed, then the child reaped, all inside :data:`PROBE_REAP_S` of the spawn (codex MED: EOF is not
   a reap). On a path that leaves before the exit is seen, the group is killed and then the child killed and reaped.
   Either way the group is signalled only while its leader is unreaped, so the pid cannot have been reused (X4), and
   before the lock is released (N7). The probe never kills the slot's domain: at ACTIVE that is the vendor.
   Released, then the reaper is woken (``start._wake``), as ``start`` does.
5. ``probe-control-after``: CONTROL again (codex MED: an address counts as closed only when root connected to it both
   before and after the slot could not). Not run when the slot connected to any address: that is ``firewall-open``
   at once (L3 r1 X3).
6. :func:`verdict`, and the slot's last-probe record ``L/slots/<tag>/probe`` (``levain-probe-result/1``, N10) for
   every real verdict; never for ``session-busy``, ``slot-not-ours`` or ``not-probeable:*``.

Refusal names (R6, each fullmatches ``runtime_grammar._NAME_RE``, none carries an address): ``firewall-open``,
``firewall-probe-inconclusive:{dns,no-control-connect,too-many}``, ``firewall-probe-unknown:{child,unreaped}``,
``not-probeable:<x>``, ``slot-not-ours``, ``session-busy``. On ``firewall-open`` the host and address ride the
refusal's detail, which ``runtime_helper.main`` writes to stderr escaped (O1).

Root never imports the supervisor or the operator's launch code here (R4); the child's module is
:mod:`levain.slot.probe_child` (N3). ⛔ Nothing here applies a profile or a pf rule; every OS act is a seam
(:class:`ProbeSeams`, ``runtime_helper.Seams``) and the wall's behaviour is the VM's.
"""
from __future__ import annotations

import errno
import os
import select
import selectors
import signal
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import probe_child as PC
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot.runtime_grammar import RuntimeRequest

#: R4 + O3: the probe's targets per vendor, ORDERED, the API host first. A test holds each a subset of
#: ``supervisor.VENDOR_HOSTS[vendor]`` together with every upstream host of the shipped base routes
#: (``vendor_routes.vendor_routes()``), and holds every such upstream in the tuple of each vendor it serves.
PROBE_HOSTS: Mapping[str, Tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "claude.ai", "platform.claude.com", "console.anthropic.com"),
    "codex": ("api.openai.com", "chatgpt.com", "auth.openai.com"),
}

MAX_PROBE_ADDRS = PC.MAX_ADDRS
#: N1: resolution is root's own, every host in parallel, inside this bound; nothing answered = ``:dns``.
RESOLVE_S = 5.0
#: N6 + codex MED: from the spawn, the child's result read to EOF AND the child reaped inside this bound; past it the
#: process group is killed and the child reaped within ``proc.REAP_GRACE_S``. A test holds this plus that grace
#: under END's lock wait (``end.END_LOCK_WAIT_S``), and the child's own alarm under this.
PROBE_REAP_S = 8.0

PROBEABLE_STATES = (R.RENDERED, R.ACTIVE)
PROBE_KINDS = ("interactive", "login")

OPEN = "firewall-open"
INCONCLUSIVE = "firewall-probe-inconclusive"
UNKNOWN = "firewall-probe-unknown"
NOT_PROBEABLE = "not-probeable"

RECORD_NAME = "probe"
RECORD_FORMAT = "levain-probe-result/1"
#: the refusals that are a verdict about the wall (recorded), by name or by base name
_RECORDED = {OPEN: "open", INCONCLUSIVE: "inconclusive", UNKNOWN: "unknown"}


def exited_unreaped(pid: int, timeout: float) -> bool:
    """True when ``pid``, the caller's own child, has exited within ``timeout`` seconds, WITHOUT reaping it (X4: the
    group is killed while the leader is a zombie, so neither its pid nor its group id can have been reused). macOS:
    a kqueue ``NOTE_EXIT`` watch (on a child that is already a zombie the registration itself answers ``ESRCH``,
    measured on this Mac 10-25); Linux: a pidfd, readable once the process has exited (kernel 5.3+). Anything that
    cannot watch answers False, which the probe reads as unknown, never as the wall."""
    try:
        if hasattr(select, "kqueue"):
            kq = select.kqueue()
            try:
                ev = select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                   fflags=select.KQ_NOTE_EXIT)
                got = kq.control([ev], 1, timeout)
            finally:
                kq.close()
            for g in got:
                if g.flags & select.KQ_EV_ERROR:
                    return g.data == errno.ESRCH          # registered on our own zombie: it has exited
                if g.fflags & select.KQ_NOTE_EXIT:
                    return True
            return False
        fd = os.pidfd_open(pid)                           # type: ignore[attr-defined]  # Linux, Python 3.9+
        try:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            return bool(poller.poll(int(timeout * 1000)))
        finally:
            os.close(fd)
    except OSError as e:
        return e.errno == errno.ESRCH and hasattr(select, "kqueue")
    except AttributeError:                                # no kqueue and no pidfd_open on this host
        return False


@dataclass
class ProbeSeams:
    """The probe's own OS acts (None in ``runtime_helper.Seams.probe`` = these defaults). ``connect`` is CONTROL's
    round of connects; ``killpg`` ends the child's process group; ``exited`` waits for the child's exit without
    reaping it (:func:`exited_unreaped`)."""

    getaddrinfo: Callable[..., list] = socket.getaddrinfo
    connect: Callable[[Sequence[str]], Dict[str, PC.Outcome]] = PC.connect_all
    killpg: Callable[[int, int], None] = os.killpg
    exited: Callable[[int, float], bool] = exited_unreaped
    resolve_s: float = RESOLVE_S
    reap_s: float = PROBE_REAP_S


# --- the target (R2, R4, O3, N1, N8, N9) ---------------------------------------------------------------------------

def probe_hosts(vendor: str, upstream: Optional[str], hosts: Sequence[str]) -> Tuple[str, ...]:
    """The hosts probed, in order: the route's upstream, then the route's hosts in list order, then the vendor's API
    host; each kept only when it is in :data:`PROBE_HOSTS` (R4), each once. Pure; never iterates a set."""
    allowed = PROBE_HOSTS[vendor]
    out: List[str] = []
    for h in ([upstream] if upstream else []) + list(hosts) + [allowed[0]]:
        if h in allowed and h not in out:
            out.append(h)
    return tuple(out)


def session_route(ctx: H.Ctx, a: R.Alloc) -> Tuple[Optional[str], Tuple[str, ...]]:
    """The session's one auth route (alloc's routes less its egress routes) as ``(upstream host, hosts)``, read from
    root's own ``L/routes/base.json`` through keys' base-map parser (N9; both formats). Not exactly one such route,
    or a route the map no longer holds, = ``not-probeable:route``."""
    from levain.broker.policy import PolicyError, parse_base_map

    pol = a.policy
    own = [r for r in pol.routes if r not in pol.egress_routes] if pol is not None else []
    if len(own) != 1:
        raise Refused(f"{NOT_PROBEABLE}:route", f"{len(own)} auth routes")
    data = ctx.store.read(("routes",), "base.json")
    if data is None:
        raise Refused("base-map-missing", "L/routes/base.json")
    try:
        routes = parse_base_map(H._canon_obj(data, "base-map"))
    except PolicyError as e:
        raise Refused("base-map-malformed", str(e)[:128]) from None
    route = routes.get(own[0])
    if route is None:
        raise Refused(f"{NOT_PROBEABLE}:route", f"{own[0]} is not in the base map")
    return (route.upstream.host if route.upstream is not None else None), tuple(route.hosts)


def resolve(hosts: Sequence[str], seams: ProbeSeams) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``(addresses, dropped)``: every A and AAAA answer of ``hosts`` for TCP/443 as ``(host, ip)``, in host order
    then answer order, each address once (its first host kept), N8's filter applied (``dropped`` names what it
    removed). Every host resolves in its own daemon thread, all inside ``seams.resolve_s`` (N1): a host with no
    answer by then adds nothing, and a hung lookup never holds the helper (the thread is abandoned)."""
    answers: Dict[str, list] = {}

    def one(host: str) -> None:
        try:
            answers[host] = seams.getaddrinfo(host, PC.PORT, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            answers[host] = []

    threads = [threading.Thread(target=one, args=(h,), daemon=True) for h in hosts]
    for t in threads:
        t.start()
    end = time.monotonic() + seams.resolve_s
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    out: List[Tuple[str, str]] = []
    seen, dropped = set(), []
    for host in hosts:
        for info in list(answers.get(host, ())):
            sockaddr = info[4]
            raw = sockaddr[0] if isinstance(sockaddr[0], str) else ""
            scoped = len(sockaddr) >= 4 and sockaddr[3]
            ip = None if scoped else PC.probe_address(raw)
            if ip is None:
                dropped.append(f"{host}={raw[:64]}")
            elif ip not in seen:
                seen.add(ip)
                out.append((host, ip))
    return out, dropped


# --- the child's result and the verdict (§4, N2, codex MED CONTROL-after) --------------------------------------------

def _errno_word(word: str) -> bool:
    """A ``u`` line's cause as the child spells it: an errno name, ``errno<n>`` or ``bad-address``."""
    return (word in _ERRNO_NAMES or word == "bad-address"
            or (word.startswith("errno") and word[5:].isdigit() and len(word) <= 11))


_ERRNO_NAMES = frozenset(errno.errorcode.values())


def parse_result(raw: bytes, addrs: Sequence[str]) -> Dict[str, str]:
    """The child's result as ``{ip: "c" | "b" | "u"}`` for exactly ``addrs``, or ``firewall-probe-unknown:child``:
    more than ``RESULT_MAX`` bytes, not ASCII, no final ``end`` line, a ``b`` whose cause is not N2's set, a ``u``
    whose cause is not an errno word, an address missing, repeated or not asked for, any other line, or a ``u`` line
    with no ``c`` line (X2: a valid ``c`` dominates, so a host without IPv6 still reports the address it reached)."""
    def unknown(why: str) -> Refused:
        return Refused(f"{UNKNOWN}:child", why)

    if len(raw) > PC.RESULT_MAX:
        raise unknown(f"more than {PC.RESULT_MAX} bytes")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise unknown("not ASCII") from None
    if not text.endswith("\nend\n") and text != "end\n":
        raise unknown("no end line")
    blocked = {PC.errno_name(c) for c in PC.BLOCKED_ERRNOS} | {PC.TIMEOUT}
    want = set(addrs)
    got: Dict[str, str] = {}
    for line in text[:-len("end\n")].splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[0] == PC.CONNECTED:
            kind = PC.CONNECTED
        elif len(parts) == 3 and parts[0] == PC.BLOCKED and parts[2] in blocked:
            kind = PC.BLOCKED
        elif len(parts) == 3 and parts[0] == PC.UNKNOWN and _errno_word(parts[2]):
            kind = PC.UNKNOWN
        else:
            raise unknown(f"line {line[:80]!r}")
        if parts[1] not in want or parts[1] in got:
            raise unknown(f"address {parts[1][:64]!r} not asked for or repeated")
        got[parts[1]] = kind
    if set(got) != want:
        raise unknown(f"{len(want - set(got))} address(es) missing")
    kinds = set(got.values())
    if PC.UNKNOWN in kinds and PC.CONNECTED not in kinds:
        raise unknown(f"{sum(k == PC.UNKNOWN for k in got.values())} address(es) unknown, none connected")
    return got


def verdict(addrs: Sequence[Tuple[str, str]], before: Mapping[str, bool], probed: Mapping[str, str],
            after: Mapping[str, bool]) -> Dict[str, object]:
    """Pure (§4, codex MED): no address = ``:dns``; any address the slot connected to = ``firewall-open`` (its host
    and address in the detail); one address root connected to before AND after the slot could not = ``closed``;
    otherwise ``:no-control-connect``."""
    if not addrs:
        raise Refused(f"{INCONCLUSIVE}:dns", "no address resolved")
    check_open(addrs, probed)
    for host, ip in addrs:
        if before.get(ip) and after.get(ip) and probed.get(ip) == PC.BLOCKED:
            return {"probe": "closed", "address": ip, "host": host, "addresses": len(addrs)}
    raise Refused(f"{INCONCLUSIVE}:no-control-connect",
                  f"root connected to no address both before and after the probe ({len(addrs)} tried)")


def check_open(addrs: Sequence[Tuple[str, str]], probed: Mapping[str, str]) -> None:
    """``firewall-open`` (host and address in the detail) for the first address the slot connected to."""
    for host, ip in addrs:
        if probed.get(ip) == PC.CONNECTED:
            raise Refused(OPEN, f"{host} {ip}")


def control(seams: ProbeSeams, ips: Sequence[str]) -> Dict[str, bool]:
    got = seams.connect(ips)
    return {ip: got.get(ip, (PC.UNKNOWN, ""))[0] == PC.CONNECTED for ip in ips}


# --- the fence (R3, N6, N7, codex MED reap) --------------------------------------------------------------------------

def _seams(ctx: H.Ctx) -> ProbeSeams:
    return getattr(ctx.seams, "probe", None) or ProbeSeams()


def _check_probeable(ctx: H.Ctx, a: R.Alloc) -> None:
    rec = SS.read_record(ctx, a.session)
    if rec is None or rec.state not in PROBEABLE_STATES:
        # the state's own name, "_" spelled "-": CREDENTIAL_PARKED would not parse as a wire name (_NAME_RE)
        state = rec.state.replace("_", "-") if rec is not None else "unrecorded"
        raise Refused(f"{NOT_PROBEABLE}:{state}", a.session)
    if a.slot.slot_class != "egress":
        raise Refused(f"{NOT_PROBEABLE}:class", a.slot.slot_class)
    if a.kind not in PROBE_KINDS:
        raise Refused(f"{NOT_PROBEABLE}:kind", a.kind)


def _read_result(fd: int, end: float, monotonic: Callable[[], float]) -> bytes:
    """The child's result pipe read to EOF by ``end``. No EOF by then = ``firewall-probe-unknown:unreaped``; more than
    ``RESULT_MAX`` bytes = ``firewall-probe-unknown:child`` (the read stops at the first byte over it)."""
    chunks: List[bytes] = []
    size = 0
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while True:
            left = end - monotonic()
            if left <= 0 or not sel.select(left):
                raise Refused(f"{UNKNOWN}:unreaped", "no result EOF within the bound")
            b = os.read(fd, PC.RESULT_MAX + 1 - size)
            if not b:
                return b"".join(chunks)
            size += len(b)
            if size > PC.RESULT_MAX:
                raise Refused(f"{UNKNOWN}:child", f"more than {PC.RESULT_MAX} bytes")
            chunks.append(b)


def _kill_group(ps: ProbeSeams, pid: int) -> None:
    """The child's process GROUP SIGKILLed (it was spawned detached, so the group is the child's own: whatever it
    forked across ``launchctl asuser`` or the stage dies with it, N7; codex MED). Called only while the leader is
    unreaped, so ``pid`` still names it and its group (X4). Never the slot's domain: at ACTIVE that is the vendor."""
    try:
        ps.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _probe_fenced(ctx: H.Ctx, a: R.Alloc, ips: Sequence[str]) -> Dict[str, str]:
    """Under the session END lock: the re-reads, the spawn, the result and the reap. Returns the child's result."""
    seams, ps = ctx.seams, _seams(ctx)
    if H.read_alloc(ctx, a.session) != a:
        raise Refused(f"{NOT_PROBEABLE}:alloc-changed", a.session)
    _check_probeable(ctx, a)
    if not SS._names_lease(SS._slot_state(ctx, a.slot), a):
        raise Refused("slot-not-ours", f"{a.slot.tag} is not leased to {a.session}")
    spec = SS._spec(ctx, a, "probe", probe_addrs=tuple(ips))
    argv, cgroup = SS.launch(ctx, a, spec)
    if cgroup is not None:
        seams.make_cgroup(cgroup)          # alloc's, made at RENDERED before clone has (idempotent)
    seams.at("probe-spawn")
    res_r, res_w = os.pipe()
    pid: Optional[int] = None
    reaped = False
    try:
        try:
            pid = seams.spawn(argv, spec.to_json(), cgroup, res_w, True)
        except OSError as e:                 # a fork failure (RLIMIT_NPROC, §8) is one unknown, never the wall
            raise Refused(f"{UNKNOWN}:child", f"spawn: errno {e.errno}") from None
        finally:
            os.close(res_w)
        end = seams.monotonic() + ps.reap_s
        raw = _read_result(res_r, end, seams.monotonic)
        if not ps.exited(pid, max(0.0, end - seams.monotonic())):
            raise Refused(f"{UNKNOWN}:unreaped", "the child did not exit after its result")
        _kill_group(ps, pid)                 # X4: the leader exited and is not reaped yet, so pid is still its own
        try:
            status = seams.wait_within(pid, max(0.0, end - seams.monotonic()))
        except Refused:
            reaped = True                    # wait_within killed the pid and polled its reap itself
            raise Refused(f"{UNKNOWN}:unreaped", "the child did not exit after its result") from None
        reaped = True
        if status != 0:
            raise Refused(f"{UNKNOWN}:child", f"exit {status}")
        return parse_result(raw, ips)
    finally:
        os.close(res_r)
        if pid is not None and not reaped:   # the leader is unreaped here: group first, then the child killed + reaped
            _kill_group(ps, pid)
            seams.kill(pid)
        seams.at("probe-reaped")


def _run(ctx: H.Ctx, a: R.Alloc) -> Dict[str, object]:
    seams, ps = ctx.seams, _seams(ctx)
    seams.at("probe-resolve")
    addrs, dropped = resolve(probe_hosts(a.vendor, *session_route(ctx, a)), ps)
    if len(addrs) > MAX_PROBE_ADDRS:
        raise Refused(f"{INCONCLUSIVE}:too-many", f"{len(addrs)} addresses, past {MAX_PROBE_ADDRS}")
    if not addrs:
        raise Refused(f"{INCONCLUSIVE}:dns", "no usable address" + (f" (dropped {', '.join(dropped)})"
                                                                     if dropped else ""))
    ips = [ip for _, ip in addrs]
    seams.at("probe-control")
    before = control(ps, ips)
    seams.at("probe-lock")
    try:
        with H.session_lock(ctx, a.session):
            probed = _probe_fenced(ctx, a, ips)
    finally:
        seams.at("probe-release")
        SS._wake(seams)
    check_open(addrs, probed)      # X3: decided before the second CONTROL, whose failure must not hide it
    seams.at("probe-control-after")
    after = control(ps, ips)
    return verdict(addrs, before, probed, after)


def _record(ctx: H.Ctx, a: R.Alloc, outcome: str, cause: str) -> None:
    """N10: the slot's last-probe record, root 0644, replaced whole. A failed write is noted, never the answer."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.seams.clock()))
    data = canon.canonical_bytes({"format": RECORD_FORMAT, "at": stamp, "session": a.session,
                                  "outcome": outcome, "cause": cause})
    try:
        ctx.store.replace(("slots", a.slot.tag), RECORD_NAME, data, mode=0o644)
    except (OSError, Refused) as e:
        SS._note(f"probe record not written: {type(e).__name__}")


def probe(ctx: H.Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    assert req.session is not None
    a = H.read_alloc(ctx, req.session)
    ctx.seams.at("probe-read")
    _check_probeable(ctx, a)
    try:
        answer = _run(ctx, a)
    except Refused as e:
        outcome = _RECORDED.get(e.name.split(":", 1)[0])
        if outcome is not None:
            _record(ctx, a, outcome, e.name)
        raise
    _record(ctx, a, "closed", "closed")
    return answer


__all__ = ["MAX_PROBE_ADDRS", "PROBE_HOSTS", "PROBE_REAP_S", "ProbeSeams", "RECORD_FORMAT", "RESOLVE_S",
           "parse_result", "probe", "probe_hosts", "resolve", "session_route", "verdict"]
