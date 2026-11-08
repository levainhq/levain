"""3b-8a: run the driver against ONE clone VM. Host side. usage:
  python3 -I -m scripts.vm_harness.vm_run <vm> <evidence-dir> [--slot 8] [--lease NAME] [--profile darwin|linux]
(run from the repo root; the VM must be booted, staged, installed and seeded (the 1105-17 vmend rig for darwin) and the
two guest scripts plus ``host_profile.py`` copied into the profile's tools dir). Prints RESULT.txt.
The darwin profile logs in by ssh with the Tart image's public default, handed over by an askpass file, as vmend.py does.
The linux profile talks through ``tart exec`` (the guest agent; no argv is ever shell-parsed) and falls back to the same
ssh login when the agent does not answer."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.vm_harness.driver import AdapterError, GuestError, SlotPlan, VmAdapters, run_on_vm
from scripts.vm_harness.host_profile import DARWIN, PROFILES, HostProfile, ProfileMismatch, profile_named

TART = os.environ.get("TART_BIN", "/Users/phillipclapham/.local/bin/tart")
CLONE_RE = re.compile(r"vend-[A-Za-z0-9_][A-Za-z0-9._-]*")
SSH = "/usr/bin/ssh"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
            "-o", "ConnectTimeout=15"]
# A multi-command staging script is piped to this (``tart exec -i``). ``bash -s`` would let a stdin-reading command in the
# script eat the rest of it, so the guest first reads ALL of stdin into a file, then runs that with stdin closed.
SCRIPT_ARGV = ("sudo", "-n", "bash", "-c", 'f=$(mktemp) && cat >"$f" && bash "$f" </dev/null; rc=$?; rm -f "$f"; exit $rc')

_now, _nap = time.monotonic, time.sleep   # module names so a test can drive the probe's clock


def _clone_guard(vm: str) -> None:
    if not CLONE_RE.fullmatch(vm):
        raise SystemExit(f"refusing: clone VM names start with 'vend-' (got {vm!r}); existing VMs are never touched")


def make_ssh(vm: str, askpass_dir: Path, profile: HostProfile = DARWIN):
    askpass = askpass_dir / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(profile.ssh_password)}\n")
    os.chmod(askpass, 0o700)
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="x")

    cached: list[str] = []

    def ip() -> str:
        if cached:
            return cached[0]
        r = subprocess.run([TART, "ip", vm, "--wait", "60"], capture_output=True, text=True, timeout=90, check=True)
        lines = r.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"tart ip {vm} printed nothing")
        cached.append(lines[-1])
        return cached[0]

    def remote(argv) -> list[str]:
        # OpenSSH joins its trailing args with spaces and the remote shell re-parses them: send ONE quoted command
        return [shlex.join(str(a) for a in argv)]

    def guest(argv, timeout=60) -> str:
        r = subprocess.run([SSH, *SSH_OPTS, f"{profile.ssh_user}@{ip()}", *remote(argv)], capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode:
            raise GuestError(r.returncode, f"guest {list(argv)[:7]}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def spawn(argv):
        return subprocess.Popen([SSH, *SSH_OPTS, f"{profile.ssh_user}@{ip()}", *remote(argv)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    return guest, spawn


def tart_exec_argv(vm: str, argv, *, stdin: bool = False) -> list[str]:
    """``tart exec [-i] <vm> <argv...>``: the guest agent execs the argv directly, so there is no shell to quote for.
    ``-i`` keeps stdin open for a script piped to :data:`SCRIPT_ARGV`."""
    return [TART, "exec", *(["-i"] if stdin else []), vm, *(str(a) for a in argv)]


def make_tart_exec(vm: str):
    """The ``tart exec`` transport, the same ``(guest, spawn)`` pair as :func:`make_ssh`. ``guest.script(text, timeout)``
    runs a multi-command staging script as root (``tart exec -i <vm> sudo bash -s`` with the text on stdin). Only a
    ``vend-*`` clone is ever addressed."""
    _clone_guard(vm)

    def guest(argv, timeout=60) -> str:
        r = subprocess.run(tart_exec_argv(vm, argv), capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise GuestError(r.returncode, f"guest {list(argv)[:7]}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def script(text: str, timeout=300) -> str:
        r = subprocess.run(tart_exec_argv(vm, SCRIPT_ARGV, stdin=True), input=text, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise GuestError(r.returncode, f"guest script: {r.stderr.strip()[-300:]}")
        return r.stdout

    def spawn(argv):
        return subprocess.Popen(tart_exec_argv(vm, argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    guest.script = script
    return guest, spawn


def agent_answers(vm: str, timeout: float = 30, *, wait: float = 90) -> bool:
    """Whether the guest agent runs a trivial command (the probe the ssh fallback hangs on). A freshly booted clone's
    agent is often not up yet, so the probe retries for ``wait`` seconds before saying no."""
    deadline = _now() + wait
    while True:
        try:
            r = subprocess.run(tart_exec_argv(vm, ["/usr/bin/true"]), capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        if _now() >= deadline:
            return False
        _nap(3)


def make_transport(vm: str, profile: HostProfile, askpass_dir: Path):
    """``(guest, spawn)`` for ``profile``: ssh for an ssh profile; for a ``tart_exec`` profile the guest agent, or ssh
    when the agent does not answer and the profile allows the fallback."""
    _clone_guard(vm)
    if profile.transport == "ssh":
        return make_ssh(vm, askpass_dir, profile)
    if profile.transport != "tart_exec":
        raise ValueError(f"unknown transport {profile.transport!r} in profile {profile.name!r}")
    if agent_answers(vm) or not profile.ssh_fallback:
        return make_tart_exec(vm)
    print(f"vm_run: guest agent of {vm} did not answer; falling back to ssh as {profile.ssh_user}", file=sys.stderr)
    return make_ssh(vm, askpass_dir, profile)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("evidence")
    ap.add_argument("--slot", type=int, default=8)
    ap.add_argument("--lease", default="lease-vmdriver-real")
    ap.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    a = ap.parse_args(argv)
    _clone_guard(a.vm)
    profile = profile_named(a.profile)
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = make_transport(a.vm, profile, Path(td))
        adapters = VmAdapters(guest, spawn, profile=profile)
        try:
            adapters.verify_host()
        except (ProfileMismatch, AdapterError) as e:
            print(f"vm_run: refusing: {e}", file=sys.stderr)
            return 2
        res = run_on_vm(a.evidence, [SlotPlan(a.slot, a.lease)], adapters)
    print(res.result_path.read_text())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
