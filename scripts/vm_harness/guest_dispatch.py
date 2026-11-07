"""3b-8a guest side (runs IN the clone VM as ``_levain_dispatch``): one dispatch start through the product's own
RealLauncher, split at the one point the driver must see: AFTER ``prepare`` returned the session id and BEFORE
``render`` / ``start`` spawn the vendor. At that point it writes ``<tag>.prepared.json`` and waits for the host to
write ``<tag>.journaled`` (the host writes it only after the id is fsynced into the cleanup journal). No ack inside
``ACK_S`` raises, which the product's own ``start`` unwinds with an END; the host's REVOKE walk is the second net.

Harness code. Rig differences from production (the same two the 1105-17 hold rig carried): the render blob names no
hooks (the root helper holds no hook templates) and the workspace seam is given ``stage.make_bundle`` (production
leaves it unwired). usage: guest_dispatch.py <tag>   Files live in the profile's exchange dir (owned by the dispatch user).
"""
from __future__ import annotations


import json
import os
import sys
import time
import traceback
from pathlib import Path

if __package__:
    from scripts.vm_harness.host_profile import profile_for_platform
else:   # run as a script in the clone, next to host_profile.py (python -I leaves the script's own dir off sys.path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from host_profile import profile_for_platform

PROFILE = profile_for_platform(sys.platform)
sys.path.insert(0, PROFILE.levain_roots[-1])
from levain.agents.accounting import ActionExposure  # noqa: E402
from levain.agents.session import SessionLaunch  # noqa: E402
from levain.agents.session_launcher import RealLauncher, SessionIndex  # noqa: E402
from levain.contracts import canon  # noqa: E402
from levain.slot import layout, stage  # noqa: E402
from levain.slot.dispatch_runtime import DispatchControlPort, HelperSlotRuntime  # noqa: E402
from levain.slot.runtime_grammar import RENDER_FORMAT, parse_stdin  # noqa: E402
from levain.slot.services import dispatch_run as D  # noqa: E402
from levain.slot.supervisor import pick_routes, prepare_answer, read_base_map  # noqa: E402

FH = Path(PROFILE.exchange_dir)
ACK_S = 60
STORE = Path(PROFILE.store)
CONTROL = Path(PROFILE.run_dir) / "control" / "control.sock"
tag = ""   # set by main(); one per host run, never reused (the host also empties FH first)


def say(name: str, obj: object) -> None:
    """Atomic: a fresh temp file, fsync, rename, so the host never reads a half-written record."""
    tmp = FH / f".{tag}.{name}.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FH / f"{tag}.{name}.json")


def claim() -> None:
    """Take this tag's namespace exactly once: O_EXCL on <tag>.pid. Any pre-existing rendezvous artifact of the
    tag (a stale ack above all) refuses the run before anything is prepared."""
    stale = sorted(p.name for p in FH.glob(f"{tag}.*"))
    if stale:
        raise RuntimeError(f"rendezvous artifacts of {tag} already exist: {stale[:4]}")
    fd = os.open(FH / f"{tag}.pid", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()) + "\n")


def opt(name: str, default: str) -> str:
    try:
        return (FH / name).read_text().strip() or default
    except OSError:
        return default


class RendezvousRuntime(HelperSlotRuntime):
    def _run(self, verb, *args, **kw):
        if verb == "render":   # args[0] is the session id prepare returned; no ack, no vendor
            self._wait_ack(args[0])
        out = super()._run(verb, *args, **kw)
        if verb == "prepare":   # publish the id the moment it exists: before staging, so a hang after it is revocable
            session, slot = prepare_answer(out, host=self.host, store=self._store)
            say("prepared", {"session": session, "slot": f"e{slot}", "t": time.time()})
        return out

    def _wait_ack(self, session: str) -> None:
        end = time.monotonic() + ACK_S
        while not (FH / f"{tag}.journaled").exists():
            if time.monotonic() >= end:
                raise RuntimeError(f"no journal ack for {session} within {ACK_S}s: refusing to render")
            time.sleep(0.25)

    def _render_blob(self, req):
        routes = pick_routes(read_base_map(layout.base_routes_path(self.host)), req.harness, "passthrough",
                             egress=False)   # as HelperSlotRuntime._render_blob: the ticket lists no egress route
        data = canon.canonical_bytes({"format": RENDER_FORMAT, "vendor": req.harness, "routes": list(routes),
                                      "model": req.model, "hooks": []})
        parse_stdin("render", data)
        return data


def main(t: str) -> None:
    global tag
    tag = t
    os.chdir(str(FH))
    claim()
    index_dir = STORE / "dispatch" / "sessions"
    rt = RendezvousRuntime(host=PROFILE.host_os, control_socket=CONTROL, store=STORE,
                           workspace=lambda req, staging: stage.make_bundle(
                               f"{PROFILE.exchange_dir}/repo", "main", Path(staging) / "repo.bundle",
                               tmp_parent=PROFILE.exchange_dir, git=PROFILE.git))
    launcher = RealLauncher(DispatchControlPort(CONTROL), rt,
                            D.bound_usage_sources(queue_root=STORE / "queue", store=STORE, index_dir=index_dir),
                            SessionIndex(index_dir))
    key = "s-" + format(int(time.time() * 1000) % (16 ** 16), "016x")
    exposure = ActionExposure(actions=int(opt("actions", "20")), unit="tool_calls", seconds=3600,
                              tunnel_bytes=int(opt("tunnel_bytes", "5000000")))
    child = launcher.spawn(SessionLaunch("oc-drv-" + tag, key, "claude", opt("model", "sonnet"),
                                         opt("brief.txt", "fake brief for the driver"), "passthrough", exposure))
    session = getattr(child, "session", None)
    say("spawned", {"key": key, "session": session})
    child.release()
    say("released", {"key": key, "session": session})
    end = time.monotonic() + int(opt("deadline_s", "120"))
    while time.monotonic() < end and child.process.poll() is None:
        time.sleep(1)
    rc = child.process.poll()
    say("final", {"poll": rc, "session": session})
    if rc is None:
        say("abort", {"rc": child.abort()})


if __name__ == "__main__":
    tag = sys.argv[1]
    try:
        main(tag)
    except BaseException as exc:   # noqa: BLE001 - the host reads this file; a silent exit would be a hang
        say("error", {"exc": f"{type(exc).__module__}.{type(exc).__name__}: {exc}", "tb": traceback.format_exc()[-2500:]})
