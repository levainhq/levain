"""The NEEDS YOU band at USER level (Q17-COLD part A): ``levain band hooks install | remove | status``, and the
band-only hook entry those hooks run (``python -m levain.band_hook <Event>``).

The band belongs to the operator, not to one install: a session in any directory should show what waits on him.
An install's hooks wire it only inside that install, so an opt-in step writes THREE band-only hooks (SessionStart,
UserPromptSubmit, Stop) into the user-level Claude Code settings (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``). They carry
no posture, spores, crystal recall or consent: only :func:`levain.band_view.hook_contract`, ``hook_prompt`` and
``hook_stop`` (the one renderer). The hooks STAND DOWN, printing nothing and taking no view, when the install whose
hooks the session loads has its band wired for that session (:func:`inside_wired_install`: that install's own hooks
show it, one band, never two), for an autonomous seat, and for a headless session Levain did not launch
(:func:`stands_down`). A receipt under
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
_TIMEOUT = {"SessionStart": 30, "UserPromptSubmit": 30, "Stop": 10}
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


def _write_atomic(path: Path, content: str | bytes) -> None:
    path = _target(path)
    data = content.encode("utf-8") if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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


def _receipt_settings(receipt: Mapping[str, Any] | None) -> Path | None:
    s = receipt.get("settings") if receipt else None
    return Path(s) if isinstance(s, str) and s else None


def install(env: Mapping[str, str] | None = None, python: str | None = None,
            settings: Path | None = None) -> tuple[int, str]:
    """Write the three hooks into ``settings`` (default: the current user settings file) and the receipt. Either
    both land or neither: a receipt that cannot be written puts the settings file back as it was."""
    env = os.environ if env is None else env
    path = _target(settings if settings is not None else settings_path(env))
    try:
        doc, text = _load(path)
    except Refused as exc:
        return 1, f"levain band hooks install: refused: {exc}"
    try:
        original = path.read_bytes() if text is not None else None
    except OSError as exc:
        return 1, f"levain band hooks install: refused: {path} cannot be read ({exc}). Nothing was written"
    prior = read_receipt(env)
    python = python or sys.executable
    _strip_ours(doc)
    # Read after our own handlers are out: a re-install over a file the first install created (or found blank) still
    # finds nothing of the operator's in it, and keeps the first install's word for what was there before.
    again = bool(prior) and not doc and _receipt_settings(prior) == path
    created = text is None or (again and bool(prior.get("created_file")))
    if text is not None and not text.strip():
        blank: str | None = original.decode("utf-8") if original is not None else text
    else:
        blank = prior.get("blank_file") if again and isinstance(prior.get("blank_file"), str) else None
    hooks = doc.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append(_entry(python, event))
    try:
        _write_atomic(path, _dump(doc, text))
    except OSError as exc:
        return 1, f"levain band hooks install: refused: {path} cannot be written ({exc}). Nothing was written"
    receipt = {
        "schema": RECEIPT_SCHEMA, "levain_version": _version(),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": str(path), "python": python, "events": list(EVENTS),
        "commands": {e: command_for(python, e) for e in EVENTS},
        "created_file": bool(created),
        "codex": "the Codex hooks of an install run the band outside that install while this receipt exists",
    }
    if blank is not None:
        receipt["blank_file"] = blank
    rp = receipt_path(env)
    try:
        _write_atomic(rp, json.dumps(receipt, indent=2) + "\n")
    except OSError as exc:
        try:
            if original is None:
                path.unlink()
            else:
                _write_atomic(path, original)
        except OSError as back:
            return 1, (f"levain band hooks install: the receipt {rp} could not be written ({exc}), and {path} could "
                       f"not be put back ({back}): it holds levain's band hooks with no receipt; run levain band "
                       f"hooks remove once the receipt's directory is writable")
        return 1, (f"levain band hooks install: refused: the receipt {rp} could not be written ({exc}); {path} was "
                   f"put back as it was")
    return 0, (f"levain band hooks install: wrote SessionStart, UserPromptSubmit and Stop to {path}\n"
               f"  band only (no posture, spores, crystal recall or consent). They stand down where an install's own "
               f"band hooks are wired for the session, for autonomous seats, and in a headless session (`claude -p`) "
               f"Levain did not launch. The Stop hook may hold a turn once to have the band reprinted.\n"
               f"  interpreter: {python} (recorded; `levain update` re-writes it after an upgrade)\n"
               f"  receipt: {rp}\n  check: levain band hooks status\n"
               f"  undo: levain band hooks remove (takes out levain's band hook handlers, found by their module)")


def remove(env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Take this module's handlers out of the receipt's settings file and the current one, each on its own: one
    that cannot be read or written is named and the other is still cleaned. The receipt goes only when both are."""
    env = os.environ if env is None else env
    receipt = read_receipt(env)
    raw = _receipt_settings(receipt)
    paths: list[Path] = [_target(raw)] if raw is not None else []
    current = _target(settings_path(env))
    if current not in paths:                  # CLAUDE_CONFIG_DIR moved since install: that file is cleaned too
        paths.append(current)
    total, where, failed = 0, [], []
    for i, path in enumerate(paths):
        try:
            doc, text = _load(path)
        except Refused as exc:
            failed.append(f"refused: {exc}")
            continue
        n = _strip_ours(doc) if text is not None else 0
        if not n:
            continue
        try:
            # only the file the receipt names can have been created (or found blank) by install
            if i == 0 and raw is not None and not doc and receipt.get("created_file") and not raw.is_symlink():
                raw.unlink()                  # the plain file install made; never what a later symlink points at
            elif i == 0 and raw is not None and not doc and isinstance(receipt.get("blank_file"), str):
                _write_atomic(path, receipt["blank_file"])
            else:
                _write_atomic(path, _dump(doc, text))
        except OSError as exc:
            failed.append(f"{path} could not be written ({exc}); levain's band hooks are still in it")
            continue
        total += n
        where.append(str(path))
    rp = receipt_path(env)
    had = os.path.lexists(rp)
    lines = []
    if total:
        lines.append(f"levain band hooks remove: took {total} levain band hook handler(s) out of "
                     f"{' and '.join(where)}; the other settings in it were not changed")
    if failed:
        lines += [f"levain band hooks remove: {f}" for f in failed]
        if had:
            lines.append(f"levain band hooks remove: the receipt {rp} is kept until every file is cleaned; run this "
                         "again once the file above is fixed")
        return 1, "\n".join(lines)
    try:
        rp.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        lines.append(f"levain band hooks remove: the receipt {rp} could not be removed ({exc}); while it exists the "
                     "Codex hooks still show the band outside their install")
        return 1, "\n".join(lines)
    if not total and not had:
        return 0, "levain band hooks remove: nothing to remove (not installed)"
    return 0, "\n".join(lines) or (f"levain band hooks remove: no levain band hook handlers left in {paths[0]}; "
                                    "receipt removed")


def refresh_after_update(env: Mapping[str, str] | None = None) -> str | None:
    """What ``levain update`` does for the user-level band: when the receipt exists, install again from this
    interpreter (the recorded path follows an upgrade) into the settings file the receipt names, never a new one.
    Returns the line update prints, ``None`` when not installed."""
    env = os.environ if env is None else env
    moved, where, current = "", None, None
    try:
        receipt = read_receipt(env)
        if receipt is None:
            return None
        where = _receipt_settings(receipt)
        current = _target(settings_path(env))
        if where is not None and _target(where) != current:
            moved = (f"; Claude Code's user settings are now {current}, which these hooks are not in: run "
                     f"levain band hooks remove, then levain band hooks install")
        code, text = install(env, settings=where)
    except Exception as exc:  # noqa: BLE001 - said, never the reason update fails
        code, text = 1, f"{type(exc).__name__}: {exc}"
    if code == 0:
        return (f"• user-level band hooks re-written in {where or current} for {sys.executable} "
                f"(levain band hooks status){moved}")
    return f"• user-level band hooks NOT refreshed: {text.splitlines()[0] if text else 'unknown error'}{moved}"


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
    a missing event, a settings file that cannot be read, or an interpreter that, started with the hook's own flags,
    cannot import ``levain.band_hook`` and ``levain.band_view`` (doctor's own probe,
    :func:`levain.doctor._band_import_problem`)."""
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
    from levain.doctor import _band_import_problem, band_probe_spec

    spec = band_probe_spec(live["UserPromptSubmit"])
    if spec is None:
        return "broken", "UserPromptSubmit command cannot be read"
    python = spec[0]
    if probe:
        problem = _band_import_problem(*spec)        # started as the hook starts it: -EP, -m levain.band_hook
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
    """Whether the install whose hooks this session loads shows it the band itself: that install's band is wired FOR
    THIS SESSION (:func:`levain.band_view.session_band_state`, which asks :func:`levain.doctor.install_band_wiring`).
    For Claude Code the project dir decides, as it decides which project settings Claude Code loads: a session
    started in a subfolder of an install whose hooks sit in its ``settings.json`` loads none of them, so it is not
    covered and the user-level band shows."""
    from levain.band_view import session_band_state

    return session_band_state(agent, cwd, env) == "wired"


def stands_down(session: str, env: Mapping[str, str]) -> bool:
    """Whether the user-level band stays silent for this session (its seat, not its directory). A session with a
    Levain launch row is decided by the row (a coordinator gets the band, a worker does not). One without a row
    stands down when ``CLAUDE_CODE_ENTRYPOINT`` starts ``sdk-``: a headless run (``claude -p`` carries ``sdk-cli``;
    the SDKs ``sdk-ts`` and ``sdk-py``), another tool's or the operator's own script, where a band injected every
    prompt and a Stop that holds a turn would cost a model turn and change the output. An interactive session of any
    other entrypoint (the terminal's ``cli``, an IDE's or the desktop app's) keeps the band. Otherwise the seat
    marker decides (:func:`levain.band_view.seat_fact`)."""
    from levain.band_view import seat_fact
    from levain.consent.queue import queue_dir_for

    fact = seat_fact(session, queue_dir_for(env), env)
    if fact.role is not None:
        return fact.autonomous
    if (env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip().startswith("sdk-"):
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
