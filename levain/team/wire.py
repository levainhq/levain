"""Wire the team hooks into a repository's ``.claude/settings.local.json``, idempotently, and check them.

settings.local.json, not settings.json: the command carries this engineer's own interpreter path, so it is
per-person and must never be committed (Claude Code's local settings file is the per-person one; if git does
not already ignore it, the path is added to ``<git common dir>/info/exclude``, never to a tracked file).
Entries this module owns are recognised by ``levain.team.hook`` in the command; nothing else is touched.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from .transport import Repo, git

MARK = "levain.team.hook"
PRE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
START_MATCHER = "startup|resume|clear|compact"
SETTINGS_REL = Path(".claude") / "settings.local.json"


def command(python: str, event: str) -> str:
    return f"{shlex.quote(python)} -P -m {MARK} {event}"


def _strip_ours(groups: list) -> list:
    out = []
    for g in groups:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hooks = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and MARK in str(h.get("command", "")))]
        if hooks:
            out.append({**g, "hooks": hooks})
        elif not g.get("hooks"):
            out.append(g)
    return out


def install(repo: Repo, python: str | None = None) -> list[str]:
    python = python or sys.executable
    path = repo.toplevel / SETTINGS_REL
    notes = []
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}); fix it, then rerun `levain team install`")
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' is not an object")
    before = json.dumps(data, sort_keys=True)
    pre = _strip_ours(list(hooks.get("PreToolUse", [])))
    pre.append({"matcher": PRE_MATCHER,
                "hooks": [{"type": "command", "command": command(python, "pretooluse"), "timeout": 30}]})
    start = _strip_ours(list(hooks.get("SessionStart", [])))
    start.append({"matcher": START_MATCHER,
                  "hooks": [{"type": "command", "command": command(python, "sessionstart"), "timeout": 60}]})
    hooks["PreToolUse"] = pre
    hooks["SessionStart"] = start
    if json.dumps(data, sort_keys=True) != before or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        notes.append(f"wired PreToolUse + SessionStart in {path}")
    else:
        notes.append(f"hooks already wired in {path}")
    rel = SETTINGS_REL.as_posix()
    if git(["check-ignore", "-q", rel], repo.toplevel, check=False).returncode != 0:
        exclude = repo.common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if f"/{rel}" not in existing.splitlines():
            with open(exclude, "a", encoding="utf-8") as fh:
                fh.write(("" if existing.endswith("\n") or not existing else "\n") + f"/{rel}\n")
            notes.append(f"added /{rel} to {exclude} (it holds your interpreter path; never commit it)")
    return notes


def worktrees(repo: Repo) -> list[Path]:
    """Every working tree of the project (main clone + `git worktree add` ones), never the ledger's own."""
    cp = git(["worktree", "list", "--porcelain"], repo.toplevel, check=False)
    out = []
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree "):])
            if p.is_dir() and not str(os.path.realpath(p)).startswith(os.path.realpath(repo.base)):
                out.append(p)
    return out or [repo.toplevel]


def install_all(repo: Repo, python: str | None = None) -> list[str]:
    """Wire every working tree: a Claude session started in any of them must be governed."""
    notes = []
    for wt in worktrees(repo):
        notes += install(Repo(wt, repo.common), python)
    notes.append("after a new `git worktree add`, run `levain team install` again to wire it")
    return notes


def check(repo: Repo) -> list[tuple[bool, str]]:
    """(ok, line) per check: both hooks wired once, the interpreter exists, git ignores the file."""
    path = repo.toplevel / SETTINGS_REL
    out: list[tuple[bool, str]] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [(False, f"{path} missing: run `levain team install`")]
    except ValueError as exc:
        return [(False, f"{path} is not valid JSON ({exc})")]
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    for event, matcher, arg in (("PreToolUse", PRE_MATCHER, "pretooluse"),
                                ("SessionStart", START_MATCHER, "sessionstart")):
        ours = [(g.get("matcher"), h.get("command", "")) for g in hooks.get(event, []) if isinstance(g, dict)
                for h in g.get("hooks", []) if isinstance(h, dict) and MARK in str(h.get("command", ""))]
        if len(ours) != 1:
            out.append((False, f"{event}: expected one levain team hook, found {len(ours)}: run `levain team install`"))
            continue
        m, cmd = ours[0]
        if m != matcher or not cmd.endswith(f"{MARK} {arg}"):
            out.append((False, f"{event}: hook is wired with matcher {m!r} / command {cmd!r}: rerun `levain team install`"))
            continue
        try:
            py = shlex.split(cmd)[0]
        except ValueError:
            py = ""
        if not (py and os.access(py, os.X_OK)):
            out.append((False, f"{event}: interpreter {py!r} is not executable: rerun `levain team install`"))
            continue
        out.append((True, f"{event} hook wired ({py})"))
    ignored = git(["check-ignore", "-q", SETTINGS_REL.as_posix()], repo.toplevel, check=False).returncode == 0
    out.append((ignored, f"{SETTINGS_REL} is {'ignored by git' if ignored else 'NOT ignored by git (it holds a per-person path)'}"))
    return out
