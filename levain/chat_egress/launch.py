"""One chat launch's egress: `start_egress` / `stop_egress` (design §3.8.5(o) items 1, 7, 8, 9; CH-D2 S8).

`start_egress`, in order (the first step that refuses, refuses, and nothing after it runs):
  1. test seams are refused unless `LEVAIN_TEST_SEAMS=1` is in the SERVICE's env, and a glue argument outside the
     glue's own test-seam list (`GLUE_SEAMS`) is refused even then; an `authority` of None is refused (the
     parameter has no default: S8D r1 KNOWN EDGES, so no launch skips 4b);
  2. the launch id must name a row of the dispatch store's `launches` table (the egress rows reference it);
  3. Node: `<node> --version` under an empty env must read `v<major>.<minor>.<patch>` with the major in
     `ACCEPTED_NODE_MAJORS` (R15-17-4: the probe suite has passed on 26 only), else refused naming the version;
  4. srt: the install at `srt_prefix` verified by `srt_pin.check` (the stamp `levain chat setup` wrote, every locked
     package's tree hash, the version `SRT_VERSION`, the vendor check: R15-10-R2 (4)); the glue runs only the `dist`
     that check returns;
  4a. the mode read once (`launch_mode`): a value outside `classify.MODES` is refused. The session header and the
     egress digest are derived from that read, never from a second input (the digest fixes the mode at launch; a
     `LiveSetting` is still read live per request by the decider, ratcheted from that read: a tightening applies
     live and stays, a loosening never applies to this launch: Q-R15-9);
  4b. 19a: from the authority replay the routes were built from, the keyed records expiring within
     `renew.RENEW_DUE` are computed (a naive `now` or an authority that does not read is refused here);
  5. the listener pair: the HTTP(S) listener (handed to the glue as an fd) and the SOCKS refuser (`socks`), both on
     127.0.0.1; the decider and observation socket pairs, the CA pipe and the lifeline pipe;
  6. the glue spawned with `env={}` (R15-10-A10, R15-12-B6) and only those fds; the CA (key + cert PEM, held by the
     caller in memory) written into its pipe and the pipe closed, so the key never touches the file system here;
  7. the decider service (`decider.Decider`) on Levain's ends, then the glue's `serving` line awaited;
  8. once the EgressLaunch owns everything: a non-empty 4b list goes to `on_renew_due` once, on its own daemon
     thread (`EgressLaunch.renew_ping`), waited for at most `RENEW_PING_WAIT_S`; a callback that raises or is still
     running then, or a thread that cannot start (renew_ping None), is logged, and the launch stands either way.
A failure at or after step 5's first allocation releases everything allocated so far (the glue killed and reaped
first, then the decider, then every descriptor and the SOCKS listener), each close attempted whatever an earlier one
raised; an OSError up to the CA write is refused as EgressRefused, a later failure propagates as itself.

`stop_egress`, in order: the lifeline's write end CLOSED FIRST (the glue's only clean end: it drains, writes the
final flush, exits 0), the glue awaited, THEN the decider closed (it drains the observation channel the glue just
closed), THEN the SOCKS listener. Every step is attempted whatever an earlier one raised; a step that raised is named
in `StopResult.failures`. The final flush is read back from the event file (P-9: written at every clean end, total 0
when no wall fired); a glue that exits non-zero, or not inside its bound, leaves none, and `StopResult` says so.

The session header (item 8, R15-18-Q4): R15-5's words when the launch is `open network`, the Node version, and the
three claims, `transport_confined` reading `unmeasured` until arm (15o)'s record exists. `egress_containment` is the
egress part of the format-3 containment JSON (item 7); the whole `containment_digest` is CH-D1a's.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from levain.chat_egress import advert, classify, keys, renew, srt_pin, strip
from levain.chat_egress import decider as dm
from levain.chat_egress import records as rec
from levain.chat_egress.resolver import Resolver, system_lookup
from levain.chat_egress.socks import SocksRefuser

HERE = Path(__file__).resolve().parent
GLUE = HERE / "node" / "glue.mjs"
DENIED_TABLE = HERE / "denied_table.json"

ACCEPTED_NODE_MAJORS = (26,)
SRT_VERSION = "0.0.81"
OPEN_NETWORK_LINE = "open network: network writes are not gated"
TRANSPORT_CONFINED = "unmeasured"           # until arm (15o)'s record exists (R15-18-Q4)
TEST_SEAMS_ENV = "LEVAIN_TEST_SEAMS"
#: the glue's own test seams (its header's TEST SEAMS list); any other glue argument is never a seam
GLUE_SEAMS = frozenset({"--tls-ports", "--http-ports", "--carve", "--resolve-map", "--upstream-ca", "--fifth-arg",
                        "--terminate-ports", "--system-roots"})
NODE_VERSION_TIMEOUT_S = 10.0
SERVING_TIMEOUT_S = 10.0
GLUE_EXIT_TIMEOUT_S = 10.0
RENEW_PING_WAIT_S = 2.0         # [judged by lane-chd2-s8d, 10-25]: a push's round trip, not its timeout

log = logging.getLogger(__name__)

_NODE_VERSION = re.compile(r"v([0-9]{1,4})\.[0-9]{1,6}\.[0-9]{1,6}")


class EgressRefused(Exception):
    """The launch's egress cannot start; nothing of it is left running."""


@dataclass(frozen=True)
class TestSeams:
    """CH-D2 test seams, refused in production (`refuse_seams`): glue arguments from `GLUE_SEAMS`, the resolver's
    lookup, the decider link's send fault."""
    __test__ = False
    glue_args: tuple[str, ...] = ()
    lookup: Callable[[str, int], list[str]] | None = None
    link_fault: Callable[[int], int | None] | None = None


def refuse_seams(seams: TestSeams | None) -> None:
    if seams is None:
        return
    named = [a for a in seams.glue_args if a.startswith("--")]
    if os.environ.get(TEST_SEAMS_ENV) != "1":
        what = named + (["lookup"] if seams.lookup else []) + (["link_fault"] if seams.link_fault else [])
        raise EgressRefused(f"test seams refused outside a test ({TEST_SEAMS_ENV} is not 1): {', '.join(what)}")
    bad = [a for a in named if a not in GLUE_SEAMS]
    if bad:
        raise EgressRefused(f"not a glue test seam: {', '.join(bad)}")


def check_node(node: str) -> str:
    """`node --version` under an empty env -> the version, when its major is accepted; else EgressRefused naming
    what was found."""
    try:
        out = subprocess.run([node, "--version"], env={}, capture_output=True, text=True,
                             timeout=NODE_VERSION_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EgressRefused(f"chat network needs Node 26 on PATH; {node!r} did not run: {type(exc).__name__}") \
            from None
    got = out.stdout.strip()
    m = _NODE_VERSION.fullmatch(got)
    if out.returncode != 0 or m is None:
        raise EgressRefused(f"chat network needs Node 26; {node!r} answered no version ({got[:40]!r})")
    if int(m.group(1)) not in ACCEPTED_NODE_MAJORS:
        raise EgressRefused(f"chat network needs Node 26; this is Node {got}, a major the egress probe suite has not "
                            f"passed on (R15-17-4)")
    return got


def launch_mode(mode: Any) -> str:
    """The launch's mode, read once from the decider's own setting (`Fixed` / `LiveSetting`): the one home of
    `open network` for the header and the digest. A value outside `classify.MODES` is refused."""
    got = mode.get()
    if got not in classify.MODES:
        raise EgressRefused(f"not a chat network mode: {got!r} (one of {', '.join(classify.MODES)})")
    return got


def session_header(*, open_network: bool, node_version: str) -> tuple[str, ...]:
    """The launch's session header lines (item 8, R15-18-Q4, R15-10-A1)."""
    lines = (OPEN_NETWORK_LINE,) if open_network else ()
    return lines + (f"node {node_version}", f"transport_confined: {TRANSPORT_CONFINED}",
                    f"known_credentials_scrubbed: v{strip.CRED_STRIP_VERSION}",
                    f"write_prompting: {'off' if open_network else 'http-shape'}")


def egress_containment(*, open_network: bool) -> dict[str, Any]:
    """The egress entries of the format-3 containment JSON (item 7). `transport_confined` is not one: it comes from
    arm (15o)'s record, never from the digest."""
    return {"egress": "proxy", "cred_strip": strip.strip_policy(), "read_shaped": advert.READ_SHAPED_VERSION,
            "open_network": bool(open_network)}


def egress_digest(*, open_network: bool) -> str:
    raw = json.dumps(egress_containment(open_network=open_network), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class EgressLaunch:
    launch_id: str
    store: Any
    records: rec.EgressRecords
    proc: subprocess.Popen
    decider: dm.Decider
    socks: SocksRefuser | None             # None only on a launch torn down before its refuser was made
    http_port: int
    socks_port: int
    node_version: str
    header: tuple[str, ...]
    egress_digest: str
    events: Path
    stderr: Path
    life_w: int | None = field(default=None, repr=False)
    renew_due: tuple[renew.RenewDue, ...] = ()
    renew_ping: threading.Thread | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StopResult:
    glue_exit: int | None                   # None: it did not exit inside its bound and was killed
    decider_clean: bool
    socks_clean: bool
    final_flush: dict | None                # the clean end's summary line, or None when there is none
    order: tuple[str, ...]
    failures: tuple[str, ...] = ()          # "<step>: <exception type>" for each teardown step that raised
    socks_wall_failures: int = 0            # the refuser's `wall_failures`, read after its close(); report.build_from_stop shows it
    socks_count_exact: bool = True          # False: the refuser's close() did not end its thread, so a late write may be uncounted


def start_egress(*, store: Any, launch_id: str, node: str, srt_prefix: Path, events: Path, ca_pem: bytes, routes: Any,
                 liveness: Any, credentials: Any, cards: Any, mode: Any, authority: Any,
                 limits: dm.Limits = dm.Limits(), seams: TestSeams | None = None,
                 on_renew_due: Callable[[str, list[renew.RenewDue]], None] | None = None,
                 now: datetime | None = None) -> EgressLaunch:
    refuse_seams(seams)
    if authority is None:                   # S8D r1 KNOWN EDGES: no launch without the authority its routes came from
        raise EgressRefused("no authority: a launch names the authority replay its routes were built from")
    node = shutil.which(node) or node       # the glue runs under env={}: a bare name resolves on the service's PATH
    if store.launch(launch_id) is None:
        raise EgressRefused(f"no launches row for {launch_id!r}: the egress rows of a launch reference it")
    node_version = check_node(node)
    try:
        srt_dist = srt_pin.check(srt_prefix).dist
    except srt_pin.SrtProblem as exc:
        raise EgressRefused(f"chat network refused: {exc}") from None
    launched = launch_mode(mode)
    open_network = launched == classify.OPEN_NETWORK
    events = Path(events)
    if not events.name:
        raise EgressRefused(f"the events path names no file: {str(events)!r}")
    stderr = events.with_name(events.name + ".glue-stderr")
    try:
        due = renew.renew_due(authority, routes, now=now or datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - nothing is allocated yet: refused whole
        raise EgressRefused(f"the launch's renew-due data did not read: {type(exc).__name__}: {exc}") from None
    records = rec.EgressRecords(store)
    placements = json.dumps(keys.credential_placements(routes), separators=(",", ":"))
    failures: list[str] = []
    deciders: list[dm.Decider] = []
    # `owned` holds Levain's ends from the first allocation: unwound on any failure (the glue killed first, then the
    # decider, then the rest, newest first), handed to the EgressLaunch by pop_all() only once the glue serves
    with contextlib.ExitStack() as owned:
        def own(what: str, step: Callable[[], Any]) -> None:
            owned.callback(_guarded, failures, what, step)
        try:
            wall_ours, wall_theirs = socket.socketpair()
            own("socks wall channel", wall_theirs.close)
            own("socks wall channel (Levain's end)", wall_ours.close)
            socks_srv = SocksRefuser(refusals=wall_ours)       # owns its end: closed by its close()
            own("socks", socks_srv.close)
            # `spawn` holds the glue's ends and the CA pipe: closed in this process once the glue is spawned and the CA
            # written (the glue holds its own copies), or on a refusal
            with contextlib.ExitStack() as spawn:
                def theirs_(what: str, step: Callable[[], Any]) -> None:
                    spawn.callback(_guarded, failures, what, step)
                lsock = socket.socket()
                theirs_("listener", lsock.close)
                lsock.bind(("127.0.0.1", 0))
                lsock.listen(64)
                http_port = lsock.getsockname()[1]
                ours, theirs = socket.socketpair()
                own("decider socket", ours.close)
                theirs_("glue decider socket", theirs.close)
                obs_ours, obs_theirs = socket.socketpair()
                own("observation socket", obs_ours.close)
                theirs_("glue observation socket", obs_theirs.close)
                theirs_("glue socks wall channel", wall_theirs.close)
                ca_r, ca_w = os.pipe()
                theirs_("CA pipe", lambda: os.close(ca_r))
                theirs_("CA pipe write end", lambda: os.close(ca_w))
                life_r, life_w = os.pipe()
                theirs_("glue lifeline end", lambda: os.close(life_r))
                own("lifeline", lambda: os.close(life_w))
                own("decider", lambda: [d.close(timeout_s=GLUE_EXIT_TIMEOUT_S) for d in deciders])
                child = (lsock.fileno(), theirs.fileno(), obs_theirs.fileno(), wall_theirs.fileno(), ca_r, life_r)
                for fd in child:
                    os.set_inheritable(fd, True)
                argv = [node, str(GLUE), "--srt-dist", str(srt_dist), "--listen-fd", str(lsock.fileno()),
                        "--decider-fd", str(theirs.fileno()), "--obs-fd", str(obs_theirs.fileno()),
                        "--wall-fd", str(wall_theirs.fileno()),
                        "--ca-fd", str(ca_r), "--lifeline-fd", str(life_r), "--events", str(events),
                        "--denied-table", str(DENIED_TABLE), "--credential-placements", placements,
                        *(seams.glue_args if seams else ())]
                with stderr.open("w") as err:
                    proc = subprocess.Popen(argv, pass_fds=child, env={}, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=err)
                    own("glue", lambda: _end_glue(proc))    # inside the block: a failing close of `err` still ends it
                pem = memoryview(ca_pem)
                while pem:                       # a short write would hand the glue a truncated CA
                    pem = pem[os.write(ca_w, pem):]
        except OSError as exc:
            raise EgressRefused(f"the egress glue did not start: {type(exc).__name__}") from None
        decider = dm.Decider(ours, obs_ours, launch_id=launch_id, records=records, routes=routes, mode=mode,
                             liveness=liveness, credentials=credentials, cards=cards,
                             resolver=Resolver(seams.lookup if seams and seams.lookup else system_lookup),
                             limits=limits, link_fault=seams.link_fault if seams else None, launch_mode=launched)
        deciders.append(decider)
        decider.start()
        socks_srv.start()
        deadline = time.monotonic() + SERVING_TIMEOUT_S
        while "glue: serving" not in stderr.read_text(errors="replace"):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise EgressRefused(f"the egress glue did not serve (exit {proc.poll()})")
            time.sleep(0.025)
        owned.pop_all()
    e = EgressLaunch(launch_id=launch_id, store=store, records=records, proc=proc, decider=decider,
                     socks=socks_srv, http_port=http_port, socks_port=socks_srv.port, node_version=node_version,
                     header=session_header(open_network=open_network, node_version=node_version),
                     egress_digest=egress_digest(open_network=open_network), events=events, stderr=stderr,
                     life_w=life_w, renew_due=tuple(due))
    if due and on_renew_due is not None:
        e.renew_ping = _renew_ping(on_renew_due, launch_id, due)
    return e


def _renew_ping(on_renew_due: Callable[[str, list[renew.RenewDue]], None], launch_id: str,
                due: list[renew.RenewDue]) -> threading.Thread | None:
    """`on_renew_due(launch_id, due)` on its own daemon thread, waited for at most `RENEW_PING_WAIT_S`: a ping that
    raises, or is still running then, is logged and never ends the launch. The launch is already owned by the caller's
    EgressLaunch here, so a thread that cannot be made or started is logged and None returned, never raised."""
    def run() -> None:
        try:
            on_renew_due(launch_id, list(due))
        except BaseException as exc:  # noqa: BLE001 - a ping never ends the launch
            log.warning("renew-due ping for %s failed: %s", launch_id, type(exc).__name__)

    try:
        t = threading.Thread(target=run, name="levain-renew-ping", daemon=True)
        t.start()
    except Exception as exc:  # noqa: BLE001 - e.g. RuntimeError "can't start new thread": the launch goes on
        log.warning("renew-due ping for %s did not start: %s", launch_id, type(exc).__name__)
        return None
    t.join(RENEW_PING_WAIT_S)
    if t.is_alive():
        log.warning("renew-due ping for %s still running after %ss; the launch goes on", launch_id, RENEW_PING_WAIT_S)
    return t


def _guarded(failures: list[str], what: str, step: Callable[[], Any]) -> None:
    """One teardown step: a failure is recorded under `what` and logged, never raised, so every later step still
    runs and the exception that ended the launch is the one that propagates."""
    try:
        step()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{what}: {type(exc).__name__}")       # the type only: a TimeoutExpired's text is the argv
        log.warning("egress teardown: %s failed: %s", what, type(exc).__name__)


def _end_glue(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(GLUE_EXIT_TIMEOUT_S)


def final_flush(events: Path) -> dict | None:
    """The last event line, when it is the clean end's final flush (`"final": true`); else None."""
    try:
        lines = Path(events).read_bytes().splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        last = json.loads(lines[-1])
    except ValueError:
        return None
    return last if isinstance(last, dict) and last.get("t") == "summary" and last.get("final") is True else None


def stop_egress(e: EgressLaunch) -> StopResult:
    order: list[str] = []
    failures: list[str] = []
    code: int | None = None
    decider_clean = socks_clean = False

    def lifeline() -> None:
        fd, e.life_w = e.life_w, None
        if fd is not None:
            os.close(fd)

    def glue() -> None:
        nonlocal code
        try:
            code = e.proc.wait(GLUE_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            e.proc.kill()
            e.proc.wait(GLUE_EXIT_TIMEOUT_S)

    def decider() -> None:
        nonlocal decider_clean
        decider_clean = e.decider.close()

    def socks_() -> None:
        nonlocal socks_clean
        socks_clean = True if e.socks is None else e.socks.close()

    # The SOCKS refuser stops (thread joined, wall end closed: the glue's EOF on the wall channel) before the lifeline closes,
    # so a refusal in flight is on the wall channel ahead of that EOF and the glue's final flush counts it.
    for what, step in (("socks", socks_), ("lifeline", lifeline), ("glue", glue), ("decider", decider)):
        _guarded(failures, what, step)
        order.append(what)
    wall_failures = 0 if e.socks is None else e.socks.wall_failures      # after close(): the thread that counts has ended when it returned True
    return StopResult(glue_exit=code, decider_clean=decider_clean, socks_clean=socks_clean,
                      final_flush=final_flush(e.events) if code == 0 else None, order=tuple(order),
                      failures=tuple(failures), socks_wall_failures=wall_failures, socks_count_exact=socks_clean)
