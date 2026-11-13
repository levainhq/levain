"""Run ONE argv in a vend- clone guest and keep stdout, stderr and rc as an evidence file. Host side.

usage (from the repo root, WITHOUT -I, as the sitting's P2 rule has it for the harness modules):
  python3 -m scripts.vm_harness.guest_cmd <vm> --profile darwin|linux [--sudo] [--timeout S] --out FILE -- ARGV...

* ARGV is a list, never a shell string. The transport is the one ``vm_run`` already uses for the profile: ``make_transport``
  (ssh for darwin, the Tart guest agent for linux with the ssh fallback). Over ``tart exec`` no shell is involved; over ssh
  the harness sends ONE ``shlex.join``-ed command, so each ARGV element arrives as exactly one word (a shell-syntax
  argument is data). A guest SCRIPT with redirects or background jobs is a file the caller copies and runs as ARGV.
* ``--sudo`` prefixes ``sudo -n`` (the clone image's admin has passwordless sudo).
* Only ``vend-`` clones are addressed; any other name is refused before anything runs or is written.
* ``--out`` must not exist: evidence is never overwritten. It holds what was asked (the caller's argv and what was sent),
  where, then the guest's stdout, stderr and the rc as the LAST line (``rc=N``). A non-zero rc is a datum, not an error.
* The process exits with the guest's rc (a negative rc, from a signal, becomes 128+signal). Two codes are the host's own:
  124 = the command timed out, 125 = the host could not reach the guest at all (``tart ip`` failed, no ssh); both are
  written to the evidence as TIMEOUT / TRANSPORT-ERROR and are not a statement about the guest command.
* The guest's streams are also echoed to this process's stdout / stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.vm_harness import vm_run
from scripts.vm_harness.host_profile import PROFILES, profile_named

RC_TIMEOUT = 124
RC_TRANSPORT = 125


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        raise SystemExit("guest_cmd: the command goes after a `--` separator (usage: guest_cmd <vm> --profile P --out F -- ARGV...)")
    i = argv.index("--")
    return argv[:i], argv[i + 1:]


def _evidence_text(vm, profile, kind, sudo, argv, sent, started, rc, out, err, note) -> str:
    head = [
        "# guest_cmd evidence",
        f"vm: {vm}",
        f"profile: {profile}",
        f"transport: {kind}",
        f"sudo: {'yes' if sudo else 'no'}",
        f"argv: {json.dumps(argv)}",
        f"sent: {json.dumps(sent)}",
        f"started: {started}",
    ]
    if note:
        head.append(note)
    return ("\n".join(head) + f"\n--- stdout ({len(out.encode())} bytes) ---\n{out}"
            + ("" if out.endswith("\n") or not out else "\n")
            + f"--- stderr ({len(err.encode())} bytes) ---\n{err}" + ("" if err.endswith("\n") or not err else "\n")
            + f"--- rc ---\nrc={rc}\n")


def _exit_status(rc: int) -> int:
    return 128 - rc if rc < 0 else rc & 0xFF


def main(argv: list[str]) -> int:
    opts, command = _split(argv)
    ap = argparse.ArgumentParser(prog="guest_cmd")
    ap.add_argument("vm")
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES))
    ap.add_argument("--sudo", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(opts)
    vm_run._clone_guard(a.vm)
    if not command or any(not isinstance(c, str) or "\0" in c for c in command) or not command[0]:
        raise SystemExit("guest_cmd: refusing: an empty command, or one with a NUL byte")
    out_path = Path(a.out)
    try:
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)       # never overwrite evidence
    except FileExistsError:
        raise SystemExit(f"guest_cmd: refusing: {out_path} already exists (evidence is never overwritten)") from None
    sent = (["sudo", "-n"] if a.sudo else []) + command
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    kind, rc, out, err, note = "?", RC_TRANSPORT, "", "", ""
    try:
        with tempfile.TemporaryDirectory() as td:
            guest, _spawn = vm_run.make_transport(a.vm, profile_named(a.profile), Path(td))
            kind = getattr(guest, "kind", "?")
            rc, out, err = guest.run(sent, timeout=a.timeout)
    except subprocess.TimeoutExpired:
        rc, note = RC_TIMEOUT, f"TIMEOUT after {a.timeout}s: the guest command's own result is unknown"
    except Exception as e:      # the host could not reach the guest: not a guest rc
        stderr_tail = getattr(e, "stderr", "") or ""
        rc, note = RC_TRANSPORT, f"TRANSPORT-ERROR {type(e).__name__}: {e} {str(stderr_tail).strip()[-300:]}".rstrip()
    text = _evidence_text(a.vm, a.profile, kind, a.sudo, command, sent, started, rc, out, err, note)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    sys.stdout.write(out)
    sys.stderr.write(err + (note + "\n" if note else ""))
    return _exit_status(rc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
