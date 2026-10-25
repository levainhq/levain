"""One chat launch's egress: `start_egress` / `stop_egress` (design §3.8.5(o) items 1, 7, 8, 9; CH-D2 S8).

`start_egress`, in order (the first step that refuses, refuses, and nothing after it runs):
  1. test seams are refused unless `LEVAIN_TEST_SEAMS=1` is in the SERVICE's env, and a glue argument outside the
     glue's own test-seam list (`GLUE_SEAMS`) is refused even then;
  2. the launch id must name a row of the dispatch store's `launches` table (the egress rows reference it);
  3. Node: `<node> --version` under an empty env must read `v<major>.<minor>.<patch>` with the major in
     `ACCEPTED_NODE_MAJORS` (R15-17-4: the probe suite has passed on 26 only), else refused naming the version;
  4. srt: the package at `srt_dist` must say version `SRT_VERSION` (R15-10-R2 (4); the install-time vendor check of
     its import names is not this module's);
  5. the listener pair: the HTTP(S) listener (handed to the glue as an fd) and the SOCKS refuser (`socks`), both on
     127.0.0.1; the decider and observation socket pairs, the CA pipe and the lifeline pipe;
  6. the glue spawned with `env={}` (R15-10-A10, R15-12-B6) and only those fds; the CA (key + cert PEM, held by the
     caller in memory) written into its pipe and the pipe closed, so the key never touches the file system here;
  7. the decider service (`decider.Decider`) on Levain's ends, then the glue's `serving` line awaited;
  8. 19a: given the authority replay the routes were built from, the keyed records expiring within
     `renew.RENEW_DUE` go to `on_renew_due` once (a failing callback is logged, the launch stands).
A failure after the spawn kills the glue, closes the decider and the SOCKS listener, and refuses the launch.

`stop_egress`, in order: the lifeline's write end CLOSED FIRST (the glue's only clean end: it drains, writes the
final flush, exits 0), the glue awaited, THEN the decider closed (it drains the observation channel the glue just
closed), THEN the SOCKS listener. The final flush is read back from the event file (P-9: written at every clean end,
total 0 when no wall fired); a glue that exits non-zero, or not inside its bound, leaves none, and `StopResult` says
so.

The session header (item 8, R15-18-Q4): R15-5's words when the launch is `open network`, the Node version, and the
three claims, `transport_confined` reading `unmeasured` until arm (15o)'s record exists. `egress_containment` is the
egress part of the format-3 containment JSON (item 7); the whole `containment_digest` is CH-D1a's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from levain.chat_egress import advert, renew, strip
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


def srt_version(srt_dist: Path) -> str | None:
    """The version srt's package.json says, for its `dist/sandbox` directory."""
    try:
        return json.loads((Path(srt_dist).parent.parent / "package.json").read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


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
    socks: SocksRefuser
    http_port: int
    socks_port: int
    node_version: str
    header: tuple[str, ...]
    egress_digest: str
    events: Path
    stderr: Path
    life_w: int | None = field(default=None, repr=False)
    renew_due: tuple[renew.RenewDue, ...] = ()


@dataclass(frozen=True)
class StopResult:
    glue_exit: int | None                   # None: it did not exit inside its bound and was killed
    decider_clean: bool
    socks_clean: bool
    final_flush: dict | None                # the clean end's summary line, or None when there is none
    order: tuple[str, ...]


def start_egress(*, store: Any, launch_id: str, node: str, srt_dist: Path, events: Path, ca_pem: bytes, routes: Any,
                 liveness: Any, credentials: Any, cards: Any, mode: Any, open_network: bool,
                 limits: dm.Limits = dm.Limits(), seams: TestSeams | None = None, authority: Any = None,
                 on_renew_due: Callable[[str, list[renew.RenewDue]], None] | None = None,
                 now: datetime | None = None) -> EgressLaunch:
    refuse_seams(seams)
    if store.launch(launch_id) is None:
        raise EgressRefused(f"no launches row for {launch_id!r}: the egress rows of a launch reference it")
    node_version = check_node(node)
    if srt_version(srt_dist) != SRT_VERSION:
        raise EgressRefused(f"chat network needs @anthropic-ai/sandbox-runtime {SRT_VERSION}; {srt_dist} is "
                            f"{srt_version(srt_dist)!r}")
    events = Path(events)
    records = rec.EgressRecords(store)
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(64)
    http_port = lsock.getsockname()[1]
    ours, theirs = socket.socketpair()
    obs_ours, obs_theirs = socket.socketpair()
    ca_r, ca_w = os.pipe()
    life_r, life_w = os.pipe()
    child = (lsock.fileno(), theirs.fileno(), obs_theirs.fileno(), ca_r, life_r)
    for fd in child:
        os.set_inheritable(fd, True)
    stderr = events.with_name(events.name + ".glue-stderr")
    argv = [node, str(GLUE), "--srt-dist", str(srt_dist), "--listen-fd", str(lsock.fileno()),
            "--decider-fd", str(theirs.fileno()), "--obs-fd", str(obs_theirs.fileno()), "--ca-fd", str(ca_r),
            "--lifeline-fd", str(life_r), "--events", str(events), "--denied-table", str(DENIED_TABLE),
            *(seams.glue_args if seams else ())]
    socks_srv = SocksRefuser()
    proc: subprocess.Popen | None = None
    decider: dm.Decider | None = None
    try:
        with stderr.open("w") as err:
            proc = subprocess.Popen(argv, pass_fds=child, env={}, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=err)
        os.write(ca_w, ca_pem)
    except OSError as exc:
        _abort(proc, None, socks_srv, life_w)
        raise EgressRefused(f"the egress glue did not start: {type(exc).__name__}") from None
    finally:
        for fd in (ca_w, ca_r, life_r):
            os.close(fd)
        theirs.close()
        obs_theirs.close()
        lsock.close()
    try:
        decider = dm.Decider(ours, obs_ours, launch_id=launch_id, records=records, routes=routes, mode=mode,
                             liveness=liveness, credentials=credentials, cards=cards,
                             resolver=Resolver(seams.lookup if seams and seams.lookup else system_lookup),
                             limits=limits, link_fault=seams.link_fault if seams else None)
        decider.start()
        socks_srv.start()
        deadline = time.monotonic() + SERVING_TIMEOUT_S
        while "glue: serving" not in stderr.read_text(errors="replace"):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise EgressRefused(f"the egress glue did not serve (exit {proc.poll()})")
            time.sleep(0.025)
    except BaseException:
        _abort(proc, decider, socks_srv, life_w)
        raise
    # 19a: each keyed record's expiry read from the authority replay the routes came from (its one home); a ping that
    # fails is logged and never ends the launch
    due = renew.renew_due(authority, routes, now=now or datetime.now(timezone.utc)) if authority is not None else []
    if due and on_renew_due is not None:
        try:
            on_renew_due(launch_id, due)
        except Exception as exc:  # noqa: BLE001
            log.warning("renew-due ping for %s failed: %s", launch_id, type(exc).__name__)
    return EgressLaunch(launch_id=launch_id, store=store, records=records, proc=proc, decider=decider,
                        socks=socks_srv, http_port=http_port, socks_port=socks_srv.port, node_version=node_version,
                        header=session_header(open_network=open_network, node_version=node_version),
                        egress_digest=egress_digest(open_network=open_network), events=events, stderr=stderr,
                        life_w=life_w, renew_due=tuple(due))


def _abort(proc: subprocess.Popen | None, decider: dm.Decider | None, socks_srv: SocksRefuser, life_w: int) -> None:
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(GLUE_EXIT_TIMEOUT_S)
    try:
        os.close(life_w)
    except OSError:
        pass
    if decider is not None:
        decider.close(timeout_s=GLUE_EXIT_TIMEOUT_S)
    socks_srv.close()


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
    order = []
    if e.life_w is not None:
        os.close(e.life_w)
        e.life_w = None
    order.append("lifeline")
    try:
        code: int | None = e.proc.wait(GLUE_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        e.proc.kill()
        e.proc.wait(GLUE_EXIT_TIMEOUT_S)
        code = None
    order.append("glue")
    clean = e.decider.close()
    order.append("decider")
    socks_clean = e.socks.close()
    order.append("socks")
    return StopResult(glue_exit=code, decider_clean=clean, socks_clean=socks_clean,
                      final_flush=final_flush(e.events) if code == 0 else None, order=tuple(order))
