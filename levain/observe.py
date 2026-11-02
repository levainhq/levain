"""Observe-mode PermissionRequest logger: aggregate counts only, answers nothing.

A separate, minimal, stdlib-only hook, independent of the consent hook. For one bypass day it counts what the
harness would have asked about, by FAMILY only (tool family, command family, session kind, permission mode), into
``<levain home>/observe/counts-YYYY-MM-DD.json`` (UTC day). It prints nothing and exits 0 on every path, so the
harness behaves exactly as without it. Never stored: command text, arguments, paths, URLs, tokens, cwd, session ids.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER = "levain.observe hook"  # the install's entry carries this in its command; uninstall removes only it
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
        server = name.split("__")[1] if name.count("__") >= 1 else ""
        server = re.sub(r"[^A-Za-z0-9_.-]", "", server)[:40]
        out["tool"] = f"mcp:{server}" if server else "mcp:other"
    elif name in ("Task", "Agent"):
        out["tool"] = "agent"
    agent_id = ev.get("agent_id")
    out["session"] = "subagent" if isinstance(agent_id, str) and agent_id else "main"
    mode = ev.get("permission_mode")
    out["mode"] = mode if isinstance(mode, str) and mode in MODES else "other"
    return out


def _key(fam: dict) -> str:
    return "|".join(str(int(fam[k])) if isinstance(fam[k], bool) else str(fam[k]) for k in _KEYS)


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def home_dir() -> Path:
    h = os.environ.get("LEVAIN_HOME")
    return Path(h).expanduser() if h else Path.home() / ".levain"


def counts_path(home: Path | None = None, day: str | None = None) -> Path:
    return (home or home_dir()) / "observe" / f"counts-{day or _day()}.json"


def record(path: Path, event: Any) -> None:
    """Add one to the event's family counter, under an flock, replacing the file atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _key(family(event))
    with open(path.parent / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def is_live_root(root: Path) -> bool:
    try:
        return Path(root).expanduser().resolve() == (Path.home() / ".claude").resolve()
    except OSError:
        return False


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a JSON object")
    return doc


def _write_settings(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def install(root: Path, *, live: bool = False, python: str | None = None) -> int:
    if is_live_root(root) and not live:
        print(f"refused: {root} is the live ~/.claude; pass --live to write there", file=sys.stderr)
        return 2
    path = Path(root).expanduser() / "settings.json"
    try:
        doc = _load_settings(path)
        groups = doc.setdefault("hooks", {}).setdefault("PermissionRequest", [])
        if any(_is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", [])):
            print(f"already installed in {path}")
            return 0
        cmd = f"{python or sys.executable} -m {MARKER}"
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
    try:
        doc = _load_settings(path)
        hooks = doc.get("hooks", {})
        groups = hooks.get("PermissionRequest", []) if isinstance(hooks, dict) else []
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
