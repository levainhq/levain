"""3b-8a: run the driver against ONE clone VM. Host side. usage:
  python3 -I -m scripts.vm_harness.vm_run <vm> <evidence-dir> [--slot 8] [--lease NAME]
(run from the repo root; the VM must be booted, staged, installed and seeded (the 1105-17 vmend rig) and the two
guest scripts copied: scp scripts/vm_harness/guest_ops.py guest_dispatch.py -> /tmp/fire/). Prints RESULT.txt.
The ssh login is the Tart image's public default, handed over by an askpass file, as vmend.py does."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.vm_harness.driver import SlotPlan, VmAdapters, run_on_vm

TART = "/Users/phillipclapham/.local/bin/tart"
SSH = "/usr/bin/ssh"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
            "-o", "ConnectTimeout=15"]


def make_ssh(vm: str, askpass_dir: Path):
    askpass = askpass_dir / "askpass.sh"
    askpass.write_text("#!/bin/sh\necho admin\n")
    os.chmod(askpass, 0o700)
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="x")

    def ip() -> str:
        r = subprocess.run([TART, "ip", vm, "--wait", "60"], capture_output=True, text=True, timeout=90, check=True)
        return r.stdout.strip().splitlines()[-1]

    def guest(argv, timeout=60) -> str:
        r = subprocess.run([SSH, *SSH_OPTS, f"admin@{ip()}", *argv], capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode:
            raise RuntimeError(f"guest {list(argv)[:7]} rc={r.returncode}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def spawn(argv):
        return subprocess.Popen([SSH, *SSH_OPTS, f"admin@{ip()}", *argv], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    return guest, spawn


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("evidence")
    ap.add_argument("--slot", type=int, default=8)
    ap.add_argument("--lease", default="lease-vmdriver-real")
    a = ap.parse_args(argv)
    if not a.vm.startswith("vend-"):
        raise SystemExit(f"refusing: clone VM names start with 'vend-' (got {a.vm!r}); existing VMs are never touched")
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = make_ssh(a.vm, Path(td))
        res = run_on_vm(a.evidence, [SlotPlan(a.slot, a.lease)], VmAdapters(guest, spawn))
    print(res.result_path.read_text())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
