"""3b-8a guest side (runs IN the clone VM as ``_levain_dispatch``): one dispatch start through the product's own
RealLauncher, split at the one point the driver must see: AFTER ``prepare`` returned the session id and BEFORE
``render`` / ``start`` spawn the vendor. At that point it writes ``<tag>.prepared.json`` and waits for the host to
write ``<tag>.journaled`` (the host writes it only after the id is fsynced into the cleanup journal). No ack inside
``ACK_S`` raises, which the product's own ``start`` unwinds with an END; the host's REVOKE walk is the second net.

Harness code. Rig differences from production (the same two the 1105-17 hold rig carried): the render blob names no
hooks (the root helper holds no hook templates) and the workspace seam is given ``stage.make_bundle`` (production
leaves it unwired). usage: guest_dispatch.py <tag>   Files live in /private/tmp/fh (owned by _levain_dispatch).
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/tmp/lv")
from levain.agents.accounting import ActionExposure  # noqa: E402
from levain.agents.session import SessionLaunch  # noqa: E402
from levain.agents.session_launcher import RealLauncher, SessionIndex  # noqa: E402
from levain.contracts import canon  # noqa: E402
from levain.slot import layout, stage  # noqa: E402
from levain.slot.dispatch_runtime import DispatchControlPort, HelperSlotRuntime  # noqa: E402
from levain.slot.runtime_grammar import RENDER_FORMAT, parse_stdin  # noqa: E402
from levain.slot.services import dispatch_run as D  # noqa: E402
from levain.slot.supervisor import pick_routes, read_base_map  # noqa: E402

FH = Path("/private/tmp/fh")
ACK_S = 60
STORE = Path("/private/var/levain")
CONTROL = Path("/private/var/run/levain/control/control.sock")
tag = sys.argv[1]


def say(name: str, obj: object) -> None:
    (FH / f"{tag}.{name}.json").write_text(json.dumps(obj, default=str) + "\n")


def opt(name: str, default: str) -> str:
    try:
        return (FH / name).read_text().strip() or default
    except OSError:
        return default


class RendezvousRuntime(HelperSlotRuntime):
    def _run(self, verb, *args, **kw):
        if verb == "render":   # args[0] is the session id prepare returned
            say("prepared", {"session": args[0], "t": time.time()})
            end = time.monotonic() + ACK_S
            while not (FH / f"{tag}.journaled").exists():
                if time.monotonic() >= end:
                    raise RuntimeError(f"no journal ack for {args[0]} within {ACK_S}s: refusing to render")
                time.sleep(0.25)
        return super()._run(verb, *args, **kw)

    def _render_blob(self, req):
        routes = pick_routes(read_base_map(layout.base_routes_path(self.host)), req.harness, "passthrough")
        data = canon.canonical_bytes({"format": RENDER_FORMAT, "vendor": req.harness, "routes": list(routes),
                                      "model": req.model, "hooks": []})
        parse_stdin("render", data)
        return data


def main() -> None:
    os.chdir(str(FH))
    index_dir = STORE / "dispatch" / "sessions"
    rt = RendezvousRuntime(host="darwin", control_socket=CONTROL, store=STORE,
                           workspace=lambda req, staging: stage.make_bundle(
                               "/private/tmp/fh/repo", "main", Path(staging) / "repo.bundle", tmp_parent="/private/tmp/fh",
                               git="/Library/Developer/CommandLineTools/usr/bin/git"))
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


try:
    main()
except BaseException as exc:   # noqa: BLE001 - the host reads this file; a silent exit would be a hang
    say("error", {"exc": f"{type(exc).__module__}.{type(exc).__name__}: {exc}", "tb": traceback.format_exc()[-2500:]})
