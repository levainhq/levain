"""Observe-mode PermissionRequest logger: aggregate counts only, answers nothing.

A separate, minimal, stdlib-only hook, independent of the consent hook. For one bypass day it counts what the
harness would have asked about, by FAMILY only (tool family, command family, session kind, permission mode), into
``<levain home>/observe/counts-YYYY-MM-DD.json`` (UTC day). It prints nothing and exits 0 on every path, so the
harness behaves exactly as without it. Never stored: command text, arguments, paths, URLs, tokens, cwd.

Beside the counts, a bounded append-only attribution log (``events.jsonl``, at most :data:`EVENTS_CAP` bytes, one
rotation to ``events.jsonl.1``) answers "which event was that stop": per event a UTC time, the harness's session_id
and agent_id/agent_type (each kept only when it is a short identifier), the permission mode, the tool name (a closed
set; otherwise the tool class) and the same family shape the counts use. Never tool_input, in any form.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER = "levain.observe hook"  # the install's entry ends with this plus SUFFIX; uninstall removes only that exact shape
SUFFIX = ">/dev/null 2>&1 || true"
MODES = frozenset({"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"})
COMMANDS = frozenset({
    "git", "python", "python3", "pip", "pip3", "uv", "pytest", "npm", "npx", "node", "pnpm", "yarn", "ls", "cat",
    "grep", "rg", "sed", "awk", "find", "head", "tail", "wc", "sort", "echo", "cd", "mkdir", "rm", "cp", "mv",
    "touch", "chmod", "curl", "wget", "ssh", "scp", "rsync", "docker", "make", "cargo", "go", "brew", "gh", "jq",
    "tar", "diff", "xargs", "sleep", "kill", "ps", "open", "osascript", "bash", "sh", "zsh", "env", "export",
    "which", "test", "tee", "sqlite3", "ruff", "mypy", "swift", "xcodebuild", "xcrun", "launchctl",
})
FILE_WRITE = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
FILE_READ = frozenset({"Read", "Grep", "Glob"})
_CHAIN = re.compile(r"\|\||&&|[|;&\n]")
_LOOP = re.compile(r"(?:^|[\s;&|(])(?:for|while|until)\s")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
EVENTS_NAME = "events.jsonl"
EVENTS_CAP = 256 * 1024   # the live log plus one rotated copy: at most twice this on disk
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")   # a harness identifier, never free text
TOOL_NAMES = frozenset({"Bash", "Task", "Agent"}) | FILE_WRITE | FILE_READ
_KEYS = ("tool", "command", "has_var", "has_pipe_or_chain", "has_loop", "session", "mode")


def _first_word(cmd: str) -> str:
    for tok in cmd.split():
        if _ASSIGN.match(tok):
            continue
        return os.path.basename(tok.strip("'\"(")) or ""
    return ""


def family(event: Any) -> dict:
    """The closed-vocabulary description of one PermissionRequest event (no free text survives)."""
    ev = event if isinstance(event, dict) else {}
    name = ev.get("tool_name")
    name = name if isinstance(name, str) else ""
    ti = ev.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    out = {"tool": "other", "command": "", "has_var": False, "has_pipe_or_chain": False, "has_loop": False}
    if name == "Bash":
        out["tool"] = "Bash"
        cmd = ti.get("command")
        cmd = cmd if isinstance(cmd, str) else ""
        word = _first_word(cmd)
        out["command"] = word if word in COMMANDS else "other"
        out["has_var"] = "$" in cmd
        out["has_pipe_or_chain"] = bool(_CHAIN.search(cmd.strip()))
        out["has_loop"] = bool(_LOOP.search(cmd))
    elif name in FILE_WRITE:
        out["tool"] = "file-write"
    elif name in FILE_READ:
        out["tool"] = "file-read"
    elif name.startswith("mcp__"):
        out["tool"] = "mcp"  # a server name is free configuration text, so it is never stored
    elif name in ("Task", "Agent"):
        out["tool"] = "agent"
    agent_id = ev.get("agent_id")
    out["session"] = "subagent" if isinstance(agent_id, str) and agent_id else "main"
    mode = ev.get("permission_mode")
    out["mode"] = mode if isinstance(mode, str) and mode in MODES else "other"
    return out


def _ident(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENT.fullmatch(value) else None


def event_row(event: Any, now: datetime | None = None) -> dict:
    """One attribution row: identifiers and the closed-vocabulary family, never any part of ``tool_input``."""
    ev = event if isinstance(event, dict) else {}
    fam = family(ev)
    name = ev.get("tool_name")
    return {"ts": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "session_id": _ident(ev.get("session_id")), "agent_id": _ident(ev.get("agent_id")),
            "agent_type": _ident(ev.get("agent_type")), "permission_mode": fam["mode"],
            "tool_name": name if isinstance(name, str) and name in TOOL_NAMES else fam["tool"],
            **{k: v for k, v in fam.items() if k != "mode"}}   # the mode is permission_mode above


def append_event(directory: Path, event: Any, now: datetime | None = None) -> None:
    """Append one row to ``directory``/events.jsonl: O_NOFOLLOW, 0600, no temp file; at :data:`EVENTS_CAP` the log is
    renamed over ``events.jsonl.1`` first. Called with the directory's lock held. A symlink or non-file is skipped."""
    line = (json.dumps(event_row(event, now), sort_keys=True) + "\n").encode()
    log = Path(directory) / EVENTS_NAME
    try:
        st = os.lstat(log)
        if not stat.S_ISREG(st.st_mode):
            return
        if st.st_size + len(line) > EVENTS_CAP:
            os.replace(log, Path(directory) / (EVENTS_NAME + ".1"))
    except FileNotFoundError:
        pass
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):   # swapped for a FIFO or device after the lstat: write nothing
            return
        os.fchmod(fd, 0o600)   # a file made earlier by a restore or by hand is not left readable by others
        os.write(fd, line)
    finally:
        os.close(fd)


def _key(fam: dict) -> str:
    return "|".join(str(int(fam[k])) if isinstance(fam[k], bool) else str(fam[k]) for k in _KEYS)


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


OBSERVE_DIR = "observe"  # named in firing.confinement.LEVAIN_HOME_STATE_DIRS (a test ties the two)


def home_dir() -> Path:
    from levain.home_guard import refuse_real_home_fallback
    refuse_real_home_fallback("the observe counters' home")
    h = os.environ.get("LEVAIN_HOME")
    return Path(h).expanduser() if h else Path.home() / ".levain"


def counts_path(home: Path | None = None, day: str | None = None) -> Path:
    return (home or home_dir()) / OBSERVE_DIR / f"counts-{day or _day()}.json"


def record(path: Path, event: Any) -> None:
    """Add one to the event's family counter, under an flock, replacing the file atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _key(family(event))
    with open(path.parent / ".lock", "a") as lock:
        for attempt in range(20):  # a stuck holder must not hold the harness: drop this sample after ~1 s
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 19:
                    return
                time.sleep(0.05)
        try:
            counts = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        counts[key] = int(counts.get(key, 0)) + 1
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".counts-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(counts, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            append_event(path.parent, event)
        except (OSError, ValueError):
            pass   # attribution is best effort; the counts above are already written


def load(path: Path) -> list[dict]:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    rows = []
    for key, n in (raw.items() if isinstance(raw, dict) else []):
        parts = str(key).split("|")
        if len(parts) == len(_KEYS) and isinstance(n, int):
            row = dict(zip(_KEYS, parts))
            for k in ("has_var", "has_pipe_or_chain", "has_loop"):
                row[k] = row[k] == "1"
            row["count"] = n
            rows.append(row)
    return sorted(rows, key=lambda r: -r["count"])


def candidate(row: dict) -> bool:
    """Class-3 candidate (NOT measured): a Bash request with a variable or an unlisted command, or a loop."""
    return row["tool"] == "Bash" and (row["has_var"] or row["command"] == "other" or row["has_loop"])


def summarise(path: Path) -> str:
    rows = load(path)
    if not rows:
        return "no counts recorded"
    head = ("count", "tool", "command", "var", "chain", "loop", "session", "mode", "class-3 candidate, not measured")
    lines = [head]
    total = cand = 0
    for r in rows:
        c = candidate(r)
        total += r["count"]
        cand += r["count"] if c else 0
        lines.append((str(r["count"]), r["tool"], r["command"] or "-", "y" if r["has_var"] else "-",
                      "y" if r["has_pipe_or_chain"] else "-", "y" if r["has_loop"] else "-", r["session"],
                      r["mode"], "candidate" if c else "-"))
    widths = [max(len(x[i]) for x in lines) for i in range(len(head))]
    out = ["  ".join(x[i].ljust(widths[i]) for i in range(len(head))).rstrip() for x in lines]
    out.append(f"total {total}; flagged class-3 candidates {cand} (candidate, not measured)")
    return "\n".join(out)


def hook_main() -> int:
    """Read the event on stdin, count it, print nothing, exit 0 whatever happens."""
    try:
        record(counts_path(), json.loads(sys.stdin.read()))
    except BaseException:  # noqa: BLE001 - the contract is that the harness never notices this hook
        pass
    return 0


# -- settings.json install / uninstall -------------------------------------------------------------------------

def _is_ours(handler: Any) -> bool:
    cmd = handler.get("command") if isinstance(handler, dict) else None
    return isinstance(cmd, str) and cmd.endswith(f" -m {MARKER} {SUFFIX}")


def is_live_root(root: Path) -> bool:
    """``root`` IS the live ~/.claude (not below it, not above it), by identity and not by spelling: on a case-insensitive
    volume ``/USERS/me/.CLAUDE`` is the same directory and ``Path.resolve()`` keeps the case it is given (casesweep row 13,
    key 1106-1). DENY direction (this refuses a write): ``pathid.maybe_inside`` both ways is "the same directory", and a
    question the filesystem would not answer counts as live. Identity cannot answer while ``~/.claude`` does not exist
    yet (two absent paths are two paths), so a canonical spelling that equals the live one with case folded is live too
    (L3 casefix r1 F2): on a case-sensitive volume that over-refuses a ``~/.CLAUDE``, which only demands ``--live``."""
    from levain import pathid       # a function-local import: the hook entry point (``-m levain.observe hook``) never needs it
    try:
        live, given = Path.home() / ".claude", Path(root).expanduser()
    except (OSError, RuntimeError):
        return False
    if pathid.maybe_inside(live, given) and pathid.maybe_inside(given, live):
        return True
    return str(pathid.canonical(given)).casefold() == str(pathid.canonical(live)).casefold()


def _is_live_settings(path: Path) -> bool:
    """The settings file the root names resolves into the live ~/.claude (through any symlink chain, in any letter case).
    DENY direction: ``pathid.maybe_same`` (equal, the same file, or a resolution that was unsure), or the file's
    directory is the live root (:func:`is_live_root`). ``canonical`` swallows its own failures, so the except below
    sees only a home that cannot be found."""
    from levain import pathid
    try:
        live = Path.home() / ".claude"
        return pathid.maybe_same(path, live / "settings.json") or is_live_root(pathid.canonical(path).parent)
    except (OSError, RuntimeError):
        return True  # cannot tell: treat as live


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a JSON object")
    return doc


def _write_settings(path: Path, doc: dict) -> None:
    path = path.resolve()  # a dotfile-managed symlink is written through, never replaced
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _can_import(py: str) -> bool:
    from levain.launch import child_env  # install-time only; the hook path never calls this

    try:
        return subprocess.run([py, "-I", "-c", "import levain.observe"], capture_output=True, timeout=30,
                              env=child_env()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install(root: Path, *, live: bool = False, python: str | None = None) -> int:
    if is_live_root(root) and not live:
        print(f"refused: {root} is the live ~/.claude; pass --live to write there", file=sys.stderr)
        return 2
    path = Path(root).expanduser() / "settings.json"
    if not live and _is_live_settings(path):  # a symlinked settings.json into the live dir
        print(f"refused: {path} resolves into the live ~/.claude; pass --live to write there", file=sys.stderr)
        return 2
    try:
        doc = _load_settings(path)
        hooks_doc = doc.setdefault("hooks", {})
        groups = hooks_doc.setdefault("PermissionRequest", [])
        if not isinstance(hooks_doc, dict) or not isinstance(groups, list):
            raise ValueError("hooks.PermissionRequest is not a list; fix it by hand first")
        py = python or sys.executable
        # a missing interpreter or a failed import must not show the harness "Hook failed": the logger answers nothing
        if not _can_import(py):
            raise ValueError(f"{py} cannot import levain.observe; install levain there first")
        ours = [h for g in groups if isinstance(g, dict) for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else [])
                if _is_ours(h)]
        if ours:
            if any(" -I -m " not in h["command"] for h in ours):
                raise ValueError("an older observe entry is installed; run `levain observe uninstall` first")
            print(f"already installed in {path}")
            return 0
        # -I: the hook runs in the project's cwd, so a levain/ directory there must not shadow the installed package
        cmd = f"{shlex.quote(py)} -I -m {MARKER} {SUFFIX}"
        groups.append({"matcher": "", "hooks": [{"type": "command", "command": cmd}]})
        _write_settings(path, doc)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"refused, nothing written: {exc}", file=sys.stderr)
        return 1
    print(f"installed the observe logger in {path}")
    return 0


def uninstall(root: Path, *, live: bool = False) -> int:
    if is_live_root(root) and not live:
        print(f"refused: {root} is the live ~/.claude; pass --live to write there", file=sys.stderr)
        return 2
    path = Path(root).expanduser() / "settings.json"
    if not path.exists():
        print(f"nothing to remove: {path} does not exist")
        return 0
    if not live and _is_live_settings(path):
        print(f"refused: {path} resolves into the live ~/.claude; pass --live to write there", file=sys.stderr)
        return 2
    try:
        doc = _load_settings(path)
        hooks = doc.get("hooks", {})
        groups = hooks.get("PermissionRequest", []) if isinstance(hooks, dict) else []
        if not isinstance(groups, list):
            raise ValueError("hooks.PermissionRequest is not a list; fix it by hand first")
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                mine = [h for h in g["hooks"] if not _is_ours(h)]
                if len(mine) != len(g["hooks"]):
                    if mine:
                        kept.append({**g, "hooks": mine})
                    continue
            kept.append(g)
        if kept != groups:
            if kept:
                hooks["PermissionRequest"] = kept
            else:
                hooks.pop("PermissionRequest", None)
                if not hooks:
                    doc.pop("hooks", None)
            _write_settings(path, doc)
        else:
            print(f"nothing to remove: no observe entry in {path}")
            return 0
    except (OSError, ValueError, AttributeError) as exc:
        print(f"refused, nothing written: {exc}", file=sys.stderr)
        return 1
    print(f"removed the observe logger from {path}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"]:
        return hook_main()
    print("usage: python -m levain.observe hook", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
