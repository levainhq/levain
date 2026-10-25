"""The adapters' pure rules that the queue broker reads too: what is agent config by its PATH, the paths a Codex
``apply_patch`` names, and how long a hook holds. A leaf (the standard library only), so the broker's import closure,
which ROOT_PYTHON runs, does not take in the adapters themselves. :mod:`levain.consent.adapters.claude_code` and
:mod:`levain.consent.adapters.codex` import every name here under the name they always had.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

CLAUDE_CODE_AGENT = "claude_code"

# ``max_hold`` (the installed ``--max-hold``) is the hook's TOTAL internal deadline, counted from the moment
# ``decide`` starts. Before it waits, the hook reserves this many seconds of it for what follows the wait: the
# withdraw (one sqlite write, busy timeout 30 s) and the re-read of a decision that landed meanwhile (one
# signature verification, 10 s, ``levain.authority`` verify timeout), plus 20 s of slack: 30 + 10 + 20 = 60. So
# the wait is at most ``max_hold - CLEANUP_RESERVE`` (0 when that is negative) and the hook ends by
# ``max(max_hold, CLEANUP_RESERVE)``.
CLEANUP_RESERVE = 60.0
DEFAULT_MAX_HOLD = 900.0
# the longest wait the hook makes: installed with DEFAULT_MAX_HOLD, it stops waiting CLEANUP_RESERVE before that
MAX_HOLD = DEFAULT_MAX_HOLD - CLEANUP_RESERVE

# Under a ``.claude`` directory (the workspace's or ~'s), what Claude Code loads and runs: hooks, subagents, skills,
# slash commands, and the plugin install tree (``~/.claude/plugins``: each plugin's hooks, MCP servers, agents).
CLAUDE_CONFIG_DIRS = frozenset({"hooks", "agents", "skills", "commands", "plugins"})
# MCP server definitions: a ``.mcp.json`` anywhere (a workspace's). The user's ``~/.claude.json`` is matched by
# its full path in :func:`levain.consent.adapters.claude_code.targets_agent_config`: a ``.claude.json`` elsewhere
# (a test fixture) is not config.
CONFIG_FILE_NAMES = frozenset({".mcp.json"})

# Under a ``.git`` directory component, what git itself writes and never executes from: its object store, refs and
# reflogs (directories), and its state files. Everything else there (config, config.worktree, hooks, info/, a
# linked worktree's or a submodule's git directory) is agent config (L3 r2 G1'): tagged by path, never parsed.
GIT_STATE_DIRS = frozenset({"objects", "refs", "logs"})
GIT_STATE_FILES = frozenset({"head", "index", "packed-refs", "orig_head", "fetch_head", "commit_editmsg"})

CODEX_DIR_NAME = ".codex"
PATCH_TOOL = "apply_patch"
# The file headers of Codex's patch format; their paths are the files a patch touches. Indentation is allowed, in
# case Codex's parser trims it: any other line starting ``***`` that is not a patch marker makes the patch unreadable.
_PATCH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
_PATCH_MARKERS = frozenset({"*** Begin Patch", "*** End Patch", "*** End of File"})


def _is_git_config(rest: list[str]) -> bool:
    """What follows a ``.git`` directory component (lower-cased) is something git reads or executes from: anything
    but its object store, refs and reflogs (:data:`GIT_STATE_DIRS`) and its state files (:data:`GIT_STATE_FILES`)
    directly in it. A linked worktree's or a submodule's git directory (``worktrees/<name>/``, ``modules/<name>/``)
    is tagged whole, its objects and refs included: an over-match costs a gated call, and git writes those itself."""
    if not rest:
        return False
    if len(rest) > 1 and rest[0] in GIT_STATE_DIRS:
        return False
    return not (len(rest) == 1 and rest[0] in GIT_STATE_FILES)


def _is_config_path(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    if parts and parts[-1] in CONFIG_FILE_NAMES:
        return True
    if parts and parts[-1] == ".git":
        return True  # a gitfile (a linked worktree's or a submodule's ``.git``): it names the git directory
    for i, part in enumerate(parts[:-1]):
        rest = parts[i + 1:]
        if part == ".claude":
            if rest[0] in CLAUDE_CONFIG_DIRS or (len(rest) == 1 and rest[0].startswith("settings")
                                                  and rest[0].endswith(".json")):
                return True
        if part == ".codex" and len(rest) == 1 and rest[0] in ("hooks.json", "config.toml"):
            return True
        if part == "activation" and rest[0] == "hooks" and len(rest) > 1:
            return True  # a levain install's hook scripts (the consent hook among them) run in every agent session
        if part == ".git" and _is_git_config(rest):
            return True  # git runs what these name, unsandboxed, at the operator's next git command
        if part == ".claude-plugin":
            return True  # a plugin's manifest directory (plugin.json can declare hooks and MCP servers inline)
        if part == "hooks" and i > 0 and os.path.isdir(Path(*path.parts[:i]) / ".claude-plugin"):
            return True  # a plugin outside ~/.claude/plugins (a local marketplace, --plugin-dir): its hooks/
    return False


def _in_codex_config(path: str, cwd: str, home: Path, codex_dir: Path) -> bool:
    raw = Path(str(home) + path[1:]) if path == "~" or path.startswith("~/") else Path(path)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    roots = {os.path.normpath(codex_dir).lower(), os.path.realpath(codex_dir).lower()}
    for p in (os.path.normpath(raw), os.path.realpath(raw)):
        low = p.lower()
        if CODEX_DIR_NAME in Path(low).parts[:-1]:
            return True  # anything under any .codex/ directory (the workspace's or ~/.codex)
        if any(low == r or low.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            return True  # anything under CODEX_HOME, whatever its name
    return False


def patch_targets(patch: Any) -> list[str] | None:
    """The file paths a Codex ``apply_patch`` names in its headers (``*** Add File:``, ``Update File:``,
    ``Delete File:``, ``Move to:``), or ``None`` when ``patch`` is not a string, names no file, has a line starting
    ``***`` (indented or not) that is neither a header nor a patch marker, or has a line holding ``***`` and any
    whitespace character but a space: what it names cannot be told. Lines are read as Codex reads them, split on
    ``\n`` alone with one trailing ``\r`` dropped, so only a line holding ``***`` can be a header to either. On such a
    line every other whitespace character is one the two parsers trim, split or resolve differently (Codex drops a tab
    inside a path, so ``.<tab>./x`` is ``../x`` to it), so the patch names nothing. A header is read by its literal
    prefix, never a regular expression, so a long line costs linear time (a hook that stalls fails open on Codex)."""
    if not isinstance(patch, str):
        return None
    found: list[str] = []
    for raw in patch.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if "***" not in line:
            continue
        if any(c.isspace() and c != " " for c in line):
            return None
        text = line.strip(" ")
        head = next((h for h in _PATCH_HEADERS if text.startswith(h)), None)
        if head is not None and text[len(head):]:
            found.append(text[len(head):])      # the line's ends are trimmed, never the path's own leading space
        elif text.startswith("***") and text not in _PATCH_MARKERS:
            return None
    return found or None
