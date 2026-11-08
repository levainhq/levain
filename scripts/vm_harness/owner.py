"""The VM bound (levain half, 1107-63): every tart VM the harness clones gets ``<TART_HOME>/vms/<name>/owner.json``.

Keys: ``owner`` (the seat), ``created`` (ISO), ``purpose``, ``expires`` (created + 7 days, ISO datetime) and ``pin``
(JSON boolean). Flow's reaper reads exactly two of them, ``expires`` and ``pin`` (only the literal JSON true counts);
a malformed file keeps the VM, a missing one falls to its 7-day untouched rule. This module never deletes anything.
Base images (names starting ``p72-vm`` or ``vend-real-``) are written pinned.

CLI: ``python3 -m scripts.vm_harness.owner clone <source> <name> --owner SEAT --purpose TEXT``,
``... pin <name> [--owner SEAT] [--purpose TEXT]``, ``... extend <name> --until YYYY-MM-DD|ISO``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

BASE_PREFIXES = ("p72-vm", "vend-real-")
DEFAULT_TTL = timedelta(days=7)
OWNER_FILE = "owner.json"


def tart_home() -> Path:
    return Path(os.environ.get("TART_HOME") or Path.home() / ".tart")


def is_base(name: str) -> bool:
    return name.startswith(BASE_PREFIXES)


def owner_path(name: str, home: Path | str | None = None) -> Path:
    return Path(home or tart_home()) / "vms" / name / OWNER_FILE


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".owner.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_owner(name: str, owner: str, purpose: str, *, home: Path | str | None = None,
                now: Callable[[], datetime] = datetime.now) -> dict:
    created = now().replace(microsecond=0)
    rec = {"owner": owner, "created": created.isoformat(), "purpose": purpose,
           "expires": (created + DEFAULT_TTL).isoformat(), "pin": is_base(name)}
    _write_atomic(owner_path(name, home), rec)
    return rec


def clone_vm(source: str, name: str, owner: str, purpose: str, *, home: Path | str | None = None,
             tart: str | list[str] = "tart", run: Callable = subprocess.run,
             now: Callable[[], datetime] = datetime.now) -> dict:
    """``tart clone <source> <name>``, then the sidecar. A failed clone writes nothing."""
    cmd = [tart] if isinstance(tart, str) else list(tart)
    run([*cmd, "clone", source, name], check=True)
    return write_owner(name, owner, purpose, home=home, now=now)


def _load(name: str, home) -> dict:
    p = owner_path(name, home)
    try:
        rec = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(rec, dict):
        raise ValueError(f"{p} is not a JSON object; refusing to rewrite it")
    return rec


def pin(name: str, *, owner: str = "", purpose: str = "", home: Path | str | None = None,
        now: Callable[[], datetime] = datetime.now) -> dict:
    """Set ``pin: true``. A VM with no sidecar (an existing base image) gets a full one, pinned."""
    rec = _load(name, home)
    if not rec:
        created = now().replace(microsecond=0)
        rec = {"owner": owner or "unknown", "created": created.isoformat(), "purpose": purpose or "pinned",
               "expires": (created + DEFAULT_TTL).isoformat()}
    rec["pin"] = True
    _write_atomic(owner_path(name, home), rec)
    return rec


def extend(name: str, until: str, *, home: Path | str | None = None) -> dict:
    """Move ``expires`` to ``until`` (YYYY-MM-DD or ISO datetime); every other key is left as it is."""
    datetime.fromisoformat(until)          # refuse a value the reaper could not read
    rec = _load(name, home)
    if not rec:
        raise FileNotFoundError(f"no {OWNER_FILE} for {name}; run pin or clone first")
    rec["expires"] = until
    _write_atomic(owner_path(name, home), rec)
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vm_harness.owner")
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("clone"); c.add_argument("source"); c.add_argument("name")
    c.add_argument("--owner", required=True); c.add_argument("--purpose", required=True)
    p = sub.add_parser("pin"); p.add_argument("name")
    p.add_argument("--owner", default=""); p.add_argument("--purpose", default="")
    e = sub.add_parser("extend"); e.add_argument("name"); e.add_argument("--until", required=True)
    a = ap.parse_args(argv)
    if a.verb == "clone":
        rec = clone_vm(a.source, a.name, a.owner, a.purpose)
    elif a.verb == "pin":
        rec = pin(a.name, owner=a.owner, purpose=a.purpose)
    else:
        rec = extend(a.name, a.until)
    print(json.dumps(rec, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
