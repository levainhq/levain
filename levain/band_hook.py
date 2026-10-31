"""The NEEDS YOU band at USER level (Q17-COLD part A): ``levain band hooks install | remove | status``, and the
band-only hook entry those hooks run (``python -m levain.band_hook <Event>``).

The band belongs to the operator, not to one install: a session in any directory should show what waits on him.
An install's hooks wire it only inside that install, so an opt-in step writes THREE band-only hooks (SessionStart,
UserPromptSubmit, Stop) into the user-level Claude Code settings (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``). They carry
no posture, spores, crystal recall or consent: only :func:`levain.band_view.hook_contract`, ``hook_prompt`` and
``hook_stop`` (the one renderer). The hooks STAND DOWN, printing nothing and taking no view, when the session's
working directory is inside an install whose band hooks are wired (that install's own hooks show it: one band, never
two), for an autonomous seat, and for a headless session Levain did not launch (:func:`stands_down`). A receipt under
the levain home records what was written and the interpreter path (``levain update`` re-writes it); ``remove`` takes
out this module's handlers, found by their module, and leaves the rest of the file as it was. Codex's hooks file is already machine-wide; its install hooks call :func:`codex_standalone`
outside their install, and only once this receipt exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODULE = "levain.band_hook"
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")
_TIMEOUT = {"SessionStart": 30, "UserPromptSubmit": 10, "Stop": 10}
_MATCHER = "startup|resume|clear|compact"          # the install's own SessionStart matcher
INSTALL_COMMAND = "levain band hooks install"
RECEIPT_NAME ="band_hooks.json"
RECEIPT_SCHEMA = 1


# ---------------------------------------------------------------- where things live
def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """The user-level Claude Code config dir, found as levain does everywhere: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    env = os.environ if env is None else env
    cfg = env.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg).expanduser()
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def settings_path(env: Mapping[str, str] | None = None) -> Path:
    return claude_config_dir(env) / "settings.json"


def receipt_path(env: Mapping[str, str] | None = None) -> Path:
    """``<levain home>/band_hooks.json`` (the levain home as the consent queue reads it)."""
    from levain.consent.queue import queue_dir_for

    return queue_dir_for(os.environ if env is None else env).parent / RECEIPT_NAME


def read_receipt(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(receipt_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- the hook command
def _quote(value: str) -> str:
    if os.name != "nt":
        value = re.sub(r'([\\"])', r"\\\1", value)
        value = re.sub(r"([$`])", "\"'\\1'\"", value)
    return '"' + value + '"'


# Claude Code runs a hook command through a shell and shows a notice for every hook that exits non-zero or writes
# stderr. The interpreter path is recorded at install: after a venv move, a pipx upgrade or a python bump it can be
# gone, and every session on the machine would then show that notice on every event. The tail makes a missing
# interpreter or module quiet; doctor's row still fails it (it probes the interpreter), and `levain update`
# re-writes the path.
QUIET_TAIL = "2>/dev/null || true"


def command_for(python: str, event: str) -> str:
    return f"{_quote(python)} -EP -m {MODULE} {event} {QUIET_TAIL}"


def is_ours(handler: Any) -> bool:
    """A hook handler this module wrote: its command runs ``-m levain.band_hook``."""
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        tokens = shlex.split(handler["command"])
    except ValueError:
        return False
    return any(a == "-m" and b == MODULE for a, b in zip(tokens, tokens[1:]))


def _entry(python: str, event: str) -> dict[str, Any]:
    e: dict[str, Any] = {}
    if event == "SessionStart":
        e["matcher"] = _MATCHER
    e["hooks"] = [{"type": "command", "command": command_for(python, event), "timeout": _TIMEOUT[event]}]
    return e


# ---------------------------------------------------------------- settings file edits
class Refused(Exception):
    """The settings file cannot be edited safely; nothing was written."""


def _load(path: Path) -> tuple[dict[str, Any], str | None]:
    """``(document, text)``; ``({}, None)`` for a file that does not exist. :class:`Refused` for one that cannot be read
    or parsed, or whose ``hooks`` is not the shape Claude Code reads."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"{path} cannot be read ({exc})")
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise Refused(f"{path} is not valid JSON ({exc}); fix or move it, then run this again. Nothing was written")
    if not isinstance(doc, dict):
        raise Refused(f"{path} is not a JSON object. Nothing was written")
    hooks = doc.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise Refused(f"{path}: \"hooks\" is not an object. Nothing was written")
    for event, entries in (hooks or {}).items():
        if not isinstance(entries, list):
            raise Refused(f"{path}: hooks.{event} is not a list. Nothing was written")
    return doc, text


def _indent_of(text: str | None) -> int | str:
    if text:
        m = re.search(r"^([ \t]+)\S", text, re.M)
        if m:
            return m.group(1)
    return 2


def _dump(doc: dict[str, Any], text: str | None) -> str:
    out = json.dumps(doc, indent=_indent_of(text), ensure_ascii=False)
    return out + ("\n" if text is None or text.endswith("\n") else "")


def _target(path: Path) -> Path:
    """The file ``path`` names once its symlinks are followed: a settings file kept in a dotfiles repo and linked into
    place is edited where it lives, so the link stays a link."""
    return Path(os.path.realpath(path))


def _write_atomic(path: Path, content: str) -> None:
    path = _target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _strip_ours(doc: dict[str, Any]) -> int:
    """Remove this module's handlers from ``doc``; drop entries, events and ``hooks`` they alone filled. Returns how many handlers went."""
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            hs = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(hs, list):
                kept.append(entry)
                continue
            mine = [h for h in hs if is_ours(h)]
            if not mine:
                kept.append(entry)
                continue
            removed += len(mine)
            rest = [h for h in hs if not is_ours(h)]
            if rest:
                kept.append({**entry, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed and not hooks:
        del doc["hooks"]
    return removed


# ---------------------------------------------------------------- verbs
def _version() -> str:
    from levain import __version__

    return __version__


def install(env: Mapping[str, str] | None = None, python: str | None = None) -> tuple[int, str]:
    env = os.environ if env is None else env
    path = _target(settings_path(env))
    try:
        doc, text = _load(path)
    except Refused as exc:
        return 1, f"levain band hooks install: refused: {exc}"
    prior = read_receipt(env)
    python = python or sys.executable
    _strip_ours(doc)
    # Read after our own handlers are out: a re-install over a file the first install created still finds nothing of
    # the operator's in it, and keeps the first install's word that the file was not there before.
    created = text is None or (bool(prior) and bool(prior.get("created_file")) and not doc)
    hooks = doc.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append(_entry(python, event))
    _write_atomic(path, _dump(doc, text))
    receipt = {
        "schema": RECEIPT_SCHEMA, "levain_version": _version(),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": str(path), "python": python, "events": list(EVENTS),
        "commands": {e: command_for(python, e) for e in EVENTS},
        "created_file": bool(created),
        "codex": "the Codex hooks of an install run the band outside that install while this receipt exists",
    }
    rp = receipt_path(env)
    _write_atomic(rp, json.dumps(receipt, indent=2) + "\n")
    return 0, (f"levain band hooks install: wrote SessionStart, UserPromptSubmit and Stop to {path}\n"
               f"  band only (no posture, spores, crystal recall or consent). They stand down inside an install whose "
               f"band hooks are wired, for autonomous seats, and in a headless session (`claude -p`) Levain did not "
               f"launch. The Stop hook may hold a turn once to have the band reprinted.\n"
               f"  interpreter: {python} (recorded; `levain update` re-writes it after an upgrade)\n"
               f"  receipt: {rp}\n  check: levain band hooks status\n"
               f"  undo: levain band hooks remove (takes out levain's band hook handlers, found by their module)")


def remove(env: Mapping[str, str] | None = None) -> tuple[int, str]:
    env = os.environ if env is None else env
    receipt = read_receipt(env)
    paths: list[Path] = []
    if receipt and isinstance(receipt.get("settings"), str):
        paths.append(_target(Path(receipt["settings"])))
    current = _target(settings_path(env))
    if current not in paths:                  # CLAUDE_CONFIG_DIR moved since install: that file is cleaned too
        paths.append(current)
    loaded: list[tuple[Path, dict[str, Any], str | None]] = []
    for path in paths:
        try:
            doc, text = _load(path)
        except Refused as exc:
            return 1, f"levain band hooks remove: refused: {exc}"
        loaded.append((path, doc, text))
    total, where = 0, []
    for i, (path, doc, text) in enumerate(loaded):
        n = _strip_ours(doc) if text is not None else 0
        if not n:
            continue
        total += n
        where.append(str(path))
        # only the file the receipt names can have been created by install
        if i == 0 and receipt and receipt.get("created_file") and not doc:
            path.unlink()
        else:
            _write_atomic(path, _dump(doc, text))
    rp = receipt_path(env)
    had = rp.exists()
    try:
        rp.unlink()
    except OSError:
        pass
    if not total and not had:
        return 0, "levain band hooks remove: nothing to remove (not installed)"
    return 0, (f"levain band hooks remove: took {total} levain band hook handler(s) out of "
               f"{' and '.join(where) or paths[0]}; the other settings in it were not changed")


def refresh_after_update(env: Mapping[str, str] | None = None) -> str | None:
    """What ``levain update`` does for the user-level band: when the receipt exists, install again from this
    interpreter (the recorded path follows an upgrade). Returns the line update prints, ``None`` when not installed."""
    env = os.environ if env is None else env
    try:
        if read_receipt(env) is None:
            return None
        code, text = install(env)
    except Exception as exc:  # noqa: BLE001 - said, never the reason update fails
        code, text = 1, f"{type(exc).__name__}: {exc}"
    if code == 0:
        return f"• user-level band hooks re-written for {sys.executable} (levain band hooks status)"
    return f"• user-level band hooks NOT refreshed: {text.splitlines()[0] if text else 'unknown error'}"


def live_commands(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """``{event: command}`` of this module's handlers in the live user settings (``None``: the file is unreadable)."""
    try:
        doc, text = _load(settings_path(env))
    except Refused:
        return None
    out: dict[str, str] = {}
    for event, entries in ((doc.get("hooks") or {}).items() if text is not None else ()):
        for entry in entries:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) and isinstance(entry.get("hooks"), list) else []:
                if is_ours(h):
                    out.setdefault(event, h["command"])
    return out


def user_band_state(env: Mapping[str, str] | None = None, probe: bool = True) -> tuple[str, str]:
    """``(state, detail)``: ``not installed`` | ``installed`` | ``broken``. Broken: a receipt or hooks without the other,
    a missing event, a settings file that cannot be read, or an interpreter that cannot import ``levain.band_view``
    (doctor's own probe, :func:`levain.doctor._band_import_problem`)."""
    env = os.environ if env is None else env
    receipt, live = read_receipt(env), live_commands(env)
    path = settings_path(env)
    if live is None:
        return ("broken" if receipt else "not installed",
                f"{path} cannot be read as settings" if receipt else "no user-level band hooks")
    if not receipt and not live:
        return "not installed", "no user-level band hooks (opt-in: levain band hooks install)"
    if not receipt:
        return "broken", f"band hooks in {path} but no receipt: run levain band hooks install (or remove)"
    missing = [e for e in EVENTS if e not in live]
    if missing:
        return "broken", f"{', '.join(missing)} missing from {path}: run levain band hooks install"
    try:
        python = shlex.split(live["UserPromptSubmit"])[0]
    except (ValueError, IndexError):
        return "broken", "UserPromptSubmit command cannot be read"
    if probe:
        from levain.doctor import _band_import_problem

        problem = _band_import_problem(python)
        if problem:
            return "broken", f"{problem}: the band hooks drop the band silently; run levain band hooks install again"
    return "installed", f"SessionStart + UserPromptSubmit + Stop in {path} ({python})"


def status(env: Mapping[str, str] | None = None) -> tuple[int, str]:
    env = os.environ if env is None else env
    state, detail = user_band_state(env)
    receipt = read_receipt(env)
    lines = [f"levain band hooks: {state}: {detail}"]
    if receipt:
        lines.append(f"  receipt {receipt_path(env)}: levain {receipt.get('levain_version')}, "
                     f"written {receipt.get('installed_at')} to {receipt.get('settings')}")
    return (0 if state != "broken" else 1), "\n".join(lines)


# ---------------------------------------------------------------- the hook entry
def inside_wired_install(agent: str, cwd: str | None, env: Mapping[str, str]) -> bool:
    """Whether the session is inside an install whose band hooks are wired (that install's own hooks show the band):
    ``cwd`` is inside one, or (Claude Code) ``CLAUDE_PROJECT_DIR`` is. A session started at an install keeps that
    install's project hooks when it later works in a directory outside it, so its project dir decides too."""
    from levain.band_view import _find_hooks_dir, band_hooks_state

    if agent == "claude_code":
        project = env.get("CLAUDE_PROJECT_DIR")
        if project:
            hooks = _find_hooks_dir(agent, project, {})
            if hooks is not None and band_hooks_state(hooks, agent)[0] == "wired":
                return True
        env = {k: v for k, v in env.items() if k != "CLAUDE_PROJECT_DIR"}     # the cwd walk below is the cwd's own
    hooks = _find_hooks_dir(agent, cwd, env)
    if hooks is None or not cwd:
        return False
    try:
        Path(cwd).resolve().relative_to(hooks.parent.parent.resolve())
    except (OSError, ValueError):
        return False
    return band_hooks_state(hooks, agent)[0] == "wired"


def stands_down(session: str, env: Mapping[str, str]) -> bool:
    """Whether the user-level band stays silent for this session (its seat, not its directory). A session with a
    Levain launch row is decided by the row (a coordinator gets the band, a worker does not). One without a row
    stands down when ``CLAUDE_CODE_ENTRYPOINT`` is set and is not ``cli``: a headless run (``claude -p`` carries
    ``sdk-cli``), another tool's or the operator's own script, where a band injected every prompt and a Stop that
    holds a turn would cost a model turn and change the output. Otherwise the seat marker decides
    (:func:`levain.band_view.seat_fact`)."""
    from levain.band_view import seat_fact
    from levain.consent.queue import queue_dir_for

    fact = seat_fact(session, queue_dir_for(env), env)
    if fact.role is not None:
        return fact.autonomous
    entry = (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip()
    if entry and entry != "cli":
        return True
    return fact.autonomous


def _write_json(obj: dict[str, Any]) -> bool:
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
        return True
    except (OSError, ValueError, AttributeError):
        return False


def run_event(event: str, payload: dict[str, Any], env: Mapping[str, str], agent: str = "claude_code") -> None:
    """The band-only hook for one event. Never raises; prints nothing when it stands down."""
    try:
        if env.get("LEVAIN_HOOK_SUPPRESS", "").strip() == "1":
            return
        session = payload.get("session_id")
        if not isinstance(session, str) or not session:
            return
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        from levain.band_view import hook_contract, hook_prompt, hook_stop

        if inside_wired_install(agent, cwd, env):
            return
        if stands_down(session, env):
            return
        if event == "SessionStart":
            text = hook_contract(payload, env, agent)
            if text:
                _write_json({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})
        elif event == "UserPromptSubmit":
            text, commit = hook_prompt(payload, env, agent)
            if text and _write_json({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}):
                commit()
        elif event == "Stop":
            reason = hook_stop(payload, env, agent)
            if reason:
                _write_json({"decision": "block", "reason": reason})
    except Exception:
        pass


def read_payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def codex_standalone(event: str, payload: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
    """What a Codex install hook does where its own cwd gate says no (a session outside the install: the coordinator's
    workspace): the band alone, only when the operator ran ``levain band hooks install`` (the receipt), with the
    same stand-down rules. Never raises."""
    try:
        env = os.environ if env is None else env
        if read_receipt(env) is None:
            return
        run_event(event, payload, env, "codex")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] in EVENTS:
            run_event(argv[0], read_payload(), os.environ)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
