"""levain.desk.room_readguard — the room's own PreToolUse hook: the sentence its model gets for a read outside the
folder (levain 44, key 1105-49; design ``project_memory/1105-49/DESIGN_roomdeny.md``, option A).

The vendor's refusal for ``permissions.blockReadsOutsideWorkingDirectories`` is a sentence that names the setting and
a remedy ("Ask the user to add the directory with /add-dir, or to remove that setting."). The room's model reads it and
quotes it to the operator. This hook answers first, for a path it is SURE is outside the room's folder, with the one fixed
sentence :data:`ROOM_READ_REFUSAL`; the vendor block stays on as the backstop and is never turned off.

NARROWING ONLY. The hook can add a refusal and nothing else: it never prints an ``allow`` decision, and a hook's allow
could not loosen a deny rule or the block anyway (the permissions docs). Every failure of the hook itself (an unparseable
event, a missing field, an exception, a missing or unreadable exemption list) prints nothing and exits 0, so the vendor
decides: a hook failure is a cosmetic leak of the vendor's sentence, never a hole. It never exits 2.

Two paths are left to the vendor on purpose. (1) The open check's two decoys: the room's start-up test reads them so that
the DENY RULES and the outside-the-folder BLOCK are each seen answering. A hook that answered them would pass that check
without either holding (a hook deny is an ``is_error`` result AND a ``permission_denials`` entry on the turn's result,
measured in the recorded run), so the launcher gives the child the exempt paths in :data:`EXEMPT_ENV`, and a hook with no
such list is inert. (2) The vendor's config directory (``$CLAUDE_CONFIG_DIR`` or ``<home>/.claude``): the vendor exempts
"files Claude Code itself needs" there (a spilled tool result), and the hook must not be stricter than the vendor.

This file is standard library only and self-contained: the launcher copies it as it is into the room's run directory, under
a name carrying its sha256, and the child runs the copy with ``python -I`` (``__main__`` at the bottom). The sha256 of the
bytes written for a launch is part of that launch's containment digest.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

__all__ = ["EXEMPT_ENV", "HOOK_MATCHER", "ROOM_READ_REFUSAL", "decide", "main"]

# the one sentence the model gets (the hook's permissionDecisionReason): no path, no folder, no setting key, no command.
# room_view.js REFUSED holds the whole sentence, end-anchored, inside the vendor's recorded wrapper (a test ties the two).
ROOM_READ_REFUSAL = ("That is outside the folder this room can read, so nothing was read. Tell the user so in plain "
                     "words; do not suggest commands or ways to change what the room can read.")
# the child's environment names the open check's decoy paths (a JSON list of absolute paths); no list, no refusals
EXEMPT_ENV = "LEVAIN_ROOM_EXEMPT"
# the settings entry's matcher: the three tools the vendor's block covers
HOOK_MATCHER = "Read|Grep|Glob"
_PATH_KEY = {"Read": "file_path", "Grep": "path", "Glob": "path"}


def _real(path: str) -> str:
    return os.path.realpath(path)


def _lexically_within(child: str, root: str) -> bool:
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _within(child: str, root: str) -> bool:
    """``child`` is ``root`` or beneath it. ``realpath`` keeps the casing it is given, so on a case-insensitive volume
    the lexical test alone judges the folder in other casing outside (L3 r1 codex MED). Ancestry is by filesystem
    identity: each EXISTING ancestor of ``child`` (itself first) is compared with ``root`` by ``os.path.samefile``; the
    components that do not exist yet are below the nearest one that does, so only the lexical test can speak for them."""
    if _lexically_within(child, root):
        return True
    at = child
    while True:
        if _same(at, root):                    # a component that does not exist is no file: False, walk on up
            return True
        up = os.path.dirname(at)
        if up == at:
            return False
        at = up


def decide(event: Any, *, exempt: Iterable[str], config_dir: str | None) -> str | None:
    """:data:`ROOM_READ_REFUSAL` for a Read, Grep or Glob of a path surely outside the event's ``cwd``, else None (the
    vendor decides). ``exempt`` are the paths left to the vendor (compared by ``realpath``); ``config_dir`` is the
    vendor's config directory (left to the vendor too). Relative and ``~`` paths are resolved; every comparison is on
    ``realpath`` of both sides, so a symlink out of the folder is outside. Anything unclear returns None."""
    try:
        if not isinstance(event, Mapping):
            return None
        tool, tool_input, cwd = event.get("tool_name"), event.get("tool_input"), event.get("cwd")
        key = _PATH_KEY.get(tool) if isinstance(tool, str) else None
        if key is None or not isinstance(tool_input, Mapping) or not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        given = tool_input.get(key)
        if not isinstance(given, str) or not given.strip() or "\x00" in given:
            return None
        target = os.path.expanduser(given)
        if not os.path.isabs(target):
            target = os.path.join(cwd, target)
        real, root = _real(target), _real(cwd)
        if _within(real, root):
            return None
        # the exemption likewise by identity (the decoy in other casing is the decoy), the lexical test for a missing file
        if any(isinstance(p, str) and p and (real == _real(p) or _same(real, p)) for p in exempt):
            return None
        if config_dir and _within(real, _real(config_dir)):
            return None
        return ROOM_READ_REFUSAL
    except Exception:  # noqa: BLE001  (any doubt is the vendor's to decide)
        return None


def _config_dir(environ: Mapping[str, str]) -> str | None:
    given = environ.get("CLAUDE_CONFIG_DIR")
    if given and os.path.isabs(given):
        return given
    # HOME from the mapping main was given, never os.path.expanduser (it reads the process's os.environ, L3 r1
    # deepseek); with none there, the OS user's home (the passwd entry, the vendor's own fallback too)
    home = environ.get("HOME") or _user_home()
    return os.path.join(home, ".claude") if home and os.path.isabs(home) else None


def _user_home() -> str | None:
    try:
        import pwd                                           # POSIX only; elsewhere there is no config-dir exemption

        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001
        return None


def _exempt(environ: Mapping[str, str]) -> list[str] | None:
    """The exempt paths, or None when the list is missing, EMPTY, or not a JSON list of absolute path strings (the hook
    is then inert). An empty list exempts neither decoy, so the hook would answer the open check's canary and mask it
    (L3 r1 deepseek): it is no valid list."""
    raw = environ.get(EXEMPT_ENV)
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(got, list) or not got or not all(isinstance(p, str) and os.path.isabs(p) for p in got):
        return None
    return got


def main(stdin: TextIO | None = None, stdout: TextIO | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Read one hook event, print the deny decision for a surely-outside path, and return 0 in every case."""
    try:
        env = os.environ if environ is None else environ
        exempt, config_dir = _exempt(env), _config_dir(env)
        if exempt is None or config_dir is None:     # no config dir known: refusing there could be stricter than the vendor
            return 0
        reason = decide(json.loads((stdin or sys.stdin).read()), exempt=exempt, config_dir=config_dir)
        if reason is not None:
            (stdout or sys.stdout).write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
