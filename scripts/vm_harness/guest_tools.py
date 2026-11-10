"""Copy the guest-side scripts into the guest's tools dir (``/tmp/fire``). Host side. usage:
  python3 -I -m scripts.vm_harness.guest_tools <vm> [--profile darwin|linux]
(run from the repo root; the VM must be booted and its guest agent answering). Closes the hand step ``vm_run.py`` names:
``guest_ops.py``, ``guest_dispatch.py`` and ``host_profile.py`` must sit in the profile's ``tools_dir`` first.

Every command is a ``tart exec`` ARGUMENT LIST (the guest agent execs it directly; nothing is shell-parsed). A file goes in
as ``tart exec -i <vm> sudo -n /usr/bin/tee <dest>`` with its bytes on stdin. Idempotent: ``mkdir -p`` and an overwriting
``tee`` leave the same end state however often it runs. A missing source raises BEFORE the guest is touched."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from scripts.vm_harness.host_profile import HostProfile, profile_named
from scripts.vm_harness.vm_run import TART, _clone_guard, tart_exec_argv

GUEST_TOOLS = ("guest_ops.py", "guest_dispatch.py", "host_profile.py")
SRC_DIR = Path(__file__).resolve().parent


def copy_guest_tools(vm: str, profile: HostProfile, src_dir: Path = SRC_DIR, *, run=subprocess.run) -> list[str]:
    """Copy :data:`GUEST_TOOLS` from ``src_dir`` into ``profile.tools_dir`` in ``vm``; returns the guest paths written."""
    _clone_guard(vm)
    src_dir = Path(src_dir)
    missing = [n for n in GUEST_TOOLS if not (src_dir / n).is_file()]
    if missing:
        raise FileNotFoundError(f"guest tool source missing in {src_dir}: {', '.join(missing)}")
    data = {n: (src_dir / n).read_bytes() for n in GUEST_TOOLS}
    d = profile.tools_dir

    def step(label: str, argv, *, stdin: bytes | None = None) -> None:
        full = tart_exec_argv(vm, argv, stdin=stdin is not None)
        full[0] = TART   # tart_exec_argv reads the module-level TART of vm_run; keep this module's (patchable) name authoritative
        kw = {"capture_output": True, "timeout": 120}
        if stdin is not None:
            kw["input"] = stdin
        r = run(full, **kw)
        if r.returncode:
            err = r.stderr.decode(errors="replace").strip()[-300:] if isinstance(r.stderr, bytes) else str(r.stderr)[-300:]
            raise RuntimeError(f"guest tools copy: {label} failed rc={r.returncode}: {err}")

    step(f"mkdir {d}", ["sudo", "-n", "/bin/mkdir", "-p", d])
    out = []
    for n in GUEST_TOOLS:
        step(f"write {d}/{n}", ["sudo", "-n", "/usr/bin/tee", f"{d}/{n}"], stdin=data[n])
        out.append(f"{d}/{n}")
    step(f"chmod {d}", ["sudo", "-n", "/bin/chmod", "-R", "u=rwX,go=rX", d])
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("--profile", default="darwin")
    a = ap.parse_args(argv)
    for p in copy_guest_tools(a.vm, profile_named(a.profile)):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
