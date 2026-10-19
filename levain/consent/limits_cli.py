"""``levain limits``: show, set, unset and extend a session's budgets (Q13 session 3; design F4a).

``show`` prints the limits in force per agent and where they came from, then every counted session against them.
``set`` / ``unset`` edit ``<queue dir>/limits.json`` (the operator's defaults; a raise of ONE session is only ever a
signed answer, ``extend``). ``extend`` files a ``levain_limit`` request for a session a limit HALTED, pings the
operator, holds for a signed answer and, on a verified allow, lifts the halt and resumes the session the way an
answered question resumes it. A path the operator runs; the file is the operator's, like the queue beside it (without
the S1 sandbox an agent's Bash can rewrite it: a brake on a runaway, not a bound).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from levain.consent import limits as L
from levain.consent.adapters import claude_code, codex
from levain.consent.queue import ConsentQueue, authority_source, default_queue_dir

__all__ = ["add_parser", "run"]

_ADAPTERS = {"claude_code": claude_code, "codex": codex}
DEFAULT_EXTEND_HOLD = 600.0


# --- the file ------------------------------------------------------------------------------------------------------

def _read_doc(path: Path) -> dict[str, Any]:
    """The current ``limits.json`` document, or a fresh one when there is none. A symlink, a non-file or a document
    that is not this format is refused (never rewritten over)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"format": L.LIMITS_FORMAT}
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{path} is not a regular file (a symlink or a directory is never written through)")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path} is unreadable ({type(exc).__name__}); fix or move it aside first") from exc
    if not isinstance(doc, dict) or doc.get("format") != L.LIMITS_FORMAT:
        raise ValueError(f"{path} is not a {L.LIMITS_FORMAT} document; fix or move it aside first")
    return doc


def _write_doc(path: Path, doc: dict[str, Any]) -> None:
    """Atomic: a temp file in the same directory (0600), fsync, rename; then the directory is synced."""
    if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError(f"{path} is not a regular file (a symlink or a directory is never written through)")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".limits-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _parse_value(kind: str, text: str) -> float:
    try:
        v: Any = float(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None
    got = L._limit_value(kind, v)
    if got is None:
        want = "a positive number" if kind == "active_hours" else "a positive whole number"
        raise ValueError(f"{kind} must be {want} (finite); {text!r} is not")
    return got


def _stored(kind: str, v: float) -> int | float:
    return int(v) if kind != "active_hours" and v == int(v) else v


# --- show (output) -----------------------------------------------------------------------------------------

def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _fmt(kind: str, v: float) -> str:
    return f"{v:.1f}" if kind == "active_hours" else f"{int(v):,}"


def _show_text(qdir: Path, agent: str | None, say: Callable[[str], None]) -> None:
    say(f"limits in force ({qdir}):")
    for a in ([agent] if agent else list(L.AGENTS)):
        lim, src = L.load_limits(qdir, a)
        say(f"  {a}: {src}")
        for k in L.KINDS:
            say(f"    {k:<18} {_fmt(k, lim[k])}")
    rows = L.snapshot(qdir, agent)
    if not rows:
        say("no session counted yet")
        return
    say(f"{len(rows)} counted session(s):")
    for r in rows:
        halt = r["halt"]
        tag = ""
        if halt:
            gap = f", gap #{halt['gap']}" if halt.get("gap") is not None else ", ended"
            tag = f"  HALTED on {halt['kind']}{gap}"
        say(f"  {r['agent']} {r['root']}  last {_iso(r['last_at']) or '-'}{tag}")
        say("    " + "  ".join(f"{k} {_fmt(k, r['counts'][k])}/{_fmt(k, r['limits'][k])}" for k in L.KINDS))


# --- the commands --------------------------------------------------------------------------------------------------

def _queue_dir(args: argparse.Namespace) -> Path:
    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _cmd_show(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    if args.json:
        say(json.dumps(L.panel(qdir), indent=2, sort_keys=True))
    else:
        _show_text(qdir, args.agent, say)
    return 0


def _cmd_set(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    try:
        value = _parse_value(args.kind, args.value)
        path = qdir / L.LIMITS_NAME
        doc = _read_doc(path)
        block = doc.get(args.agent)
        if block is None:
            block = doc[args.agent] = {}
        if not isinstance(block, dict):
            raise ValueError(f"{path}'s {args.agent!r} block is not an object; fix or move the file aside first")
        block[args.kind] = _stored(args.kind, value)
        _write_doc(path, doc)
    except (ValueError, OSError) as exc:
        say(f"levain limits: refused, nothing changed: {exc}")
        return 1
    say(f"levain limits: {args.agent} {args.kind} = {_fmt(args.kind, value)} (in {qdir / L.LIMITS_NAME}). "
        "Sessions already counted keep their count; the new value applies to their next call.")
    return 0


def _cmd_unset(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    try:
        path = qdir / L.LIMITS_NAME
        doc = _read_doc(path)
        block = doc.get(args.agent)
        if not isinstance(block, dict) or args.kind not in block:
            say(f"levain limits: {args.agent} has no {args.kind} set; nothing changed.")
            return 0
        del block[args.kind]
        if not block:
            del doc[args.agent]
        _write_doc(path, doc)
    except (ValueError, OSError) as exc:
        say(f"levain limits: refused, nothing changed: {exc}")
        return 1
    say(f"levain limits: {args.agent} {args.kind} unset (the default {_fmt(args.kind, L.DEFAULTS[args.kind])} "
        "applies unless the other block sets it).")
    return 0


def _signable(settings: Any) -> bool:
    from levain.consent.push_gate import _signable as signable
    return signable(settings)


def _make_queue(qdir: Path, settings: Any) -> ConsentQueue:
    from levain.authority import anchor_path

    root = Path(settings.cockpit_data_root)
    return ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))


def _find_halt(qdir: Path, agent: str | None, session: str) -> tuple[str, dict[str, Any]]:
    root = L.session_root(session)
    hits = [r for r in L.snapshot(qdir, agent) if r["root"] == root and r["halt"]]
    if not hits:
        raise ValueError("the session is not halted by a limit: there is nothing to extend")
    if len(hits) > 1:
        raise ValueError(f"more than one agent counted {root}; name one with --agent")
    return hits[0]["agent"], hits[0]["halt"]


def _resume(queue: ConsentQueue, agent: str, root: str, halt_request: str, text: str, say: Callable[[str], None], *,
            listing: Any, running: Any, launch: Any) -> None:
    """Start the halted session again with ``text`` as its prompt when it is ended and was recorded resumable;
    otherwise say where the text lands (its next hook)."""
    from levain.consent import delivery as dv

    meta = queue.resume_meta(halt_request) or {}
    where, why, argv, cwd = dv.route(agent, root, meta, noun="extend", listing=listing, running=running)
    if where == "stop_resume":
        stopped = dv._stop(dv.base_session(root), meta, listing)
        if stopped is not None:
            say(f"levain limits: the background session was not stopped ({stopped}); it goes on at its next step.")
            return
        where, why, argv, cwd = dv.route(agent, root, meta, noun="extend", listing=listing, running=running)
    if where != "resumed":
        say(f"levain limits: the session was not resumed: {why}.")
        return
    if agent == "claude_code":
        from levain.consent.band import _claude_facts

        now = _claude_facts(meta.get("config_dir") if isinstance(meta.get("config_dir"), str) else None,
                            dv.base_session(root))
        if now != (meta.get("cwd"), meta.get("permission_mode")):
            say("levain limits: the session's transcript no longer shows the start directory and permission mode "
                "recorded when it halted, so it is not resumed; it goes on at its next step.")
            return
    if not os.path.isdir(cwd or ""):
        say(f"levain limits: the session's start directory {cwd} is gone, so it is not resumed; it goes on at its "
            "next step.")
        return
    full = [*argv, text]
    logdir = Path(queue.directory) / "resumes"
    logdir.mkdir(mode=0o700, exist_ok=True)
    try:
        env = dv._env(agent, meta)
        failed = launch(full, cwd or "", env, logdir / f"limits-{halt_request}.log")
    except Exception as exc:  # noqa: BLE001 - nothing running is known to have started
        failed = f"{type(exc).__name__}: {exc}"
    if failed is None:
        say("levain limits: the session was resumed with the Extend.")
    else:
        say(f"levain limits: the resume did not start ({failed}); the session goes on at its next step.")


def _cmd_extend(args: argparse.Namespace, say: Callable[[str], None], *, queue: ConsentQueue | None = None,
                listing: Any = None, running: Any = None, launch: Any = None) -> int:
    from levain.consent import delivery as dv

    qdir = _queue_dir(args)
    try:
        hold = DEFAULT_EXTEND_HOLD if args.hold is None else args.hold
        if not 0 < hold < float("inf"):
            raise ValueError("--hold must be a positive number of seconds")
        hold = min(hold, claude_code.GATE_HOLD_CEILING)
        agent, halt = _find_halt(qdir, args.agent, args.session)
    except ValueError as exc:
        say(f"levain limits: refused, nothing was filed: {exc}")
        return 1
    root = L.session_root(args.session)
    adapter = _ADAPTERS[agent]
    settings = adapter.load_settings(qdir)
    if queue is None:
        if not _signable(settings):
            say("levain limits: NOT filed. No enrolled key can answer (the queue's "
                f"{adapter.SETTINGS_NAME} names no cockpit with an active key), so nothing can allow it.")
            return 1
        queue = _make_queue(qdir, settings)
    old = queue.resume_meta(halt["request_id"]) if halt.get("request_id") else None
    try:
        request_id, call = L.file_extend(queue, agent, root, hold=hold, cwd=(old or {}).get("cwd") or os.getcwd())
    except (ValueError, OSError, RuntimeError) as exc:
        say(f"levain limits: refused, nothing was filed: {exc}")
        return 1
    say(f"levain limits: filed request {request_id}: {L.limit_what(call)}. Sign it on the phone card, or "
        f"`levain consent answer {request_id} allow`. Waiting up to {hold:g} s.")
    try:
        claude_code.notify(settings, f"levain: {L.limit_what(call)}? (request {request_id}, held up to {hold:g} s)",
                           cwd=qdir)
    except Exception:  # noqa: BLE001 - a ping never fails the command
        pass
    queue.await_decision(request_id, hold + 5.0, poll=0.25)
    if not L.verified_allow(queue, request_id, root=root, kind=halt["kind"]):
        say("levain limits: not extended: no verified allow (denied, or no answer before the hold ended). Nothing "
            "changed; a late allow on this request is applied at the session's next call.")
        return 1
    if not L.release_if_extended(queue, agent, root):
        say("levain limits: the allow verified but the halt was not lifted; the session stays stopped.")
        return 1
    say(f"levain limits: EXTENDED {L.LABELS.get(halt['kind'], halt['kind'])} for {root}.")
    text = L.ruling_text("allow", halt.get("gap"), queue.get(request_id)[0].display)    # type: ignore[index]
    if text is None:
        return 0
    _resume(queue, agent, root, halt["request_id"] or "", text, say,
            listing=listing if listing is not None else dv.claude_listing,
            running=running if running is not None else dv.process_running,
            launch=launch if launch is not None else dv._launch)
    return 0


def run(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    sub = args.limits_cmd
    if sub == "show":
        return _cmd_show(args, say)
    if sub == "set":
        return _cmd_set(args, say)
    if sub == "unset":
        return _cmd_unset(args, say)
    if sub == "extend":
        return _cmd_extend(args, say)
    say("levain limits: choose show, set, unset or extend")
    return 2


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """``levain limits``, under the top-level subparsers."""
    p = subparsers.add_parser(
        "limits", help="Show or set a session's budgets (tool calls, active hours, tokens, repeats), or extend a "
                       "halted session.",
        description="The per-session budgets Levain counts at every tool call. `show` prints the limits in force and "
                    "each counted session against them; `set` / `unset` edit the operator's defaults; `extend` asks "
                    "for a signed Extend of a session a limit halted, and resumes it on a verified allow.")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    sp = p.add_subparsers(dest="limits_cmd")
    kinds = list(L.KINDS)
    agents = list(L.AGENTS)
    s = sp.add_parser("show", help="Limits in force per agent, and every counted session.")
    s.add_argument("--agent", choices=agents, default=None)
    s.add_argument("--json", action="store_true", help="The cockpit panel's JSON instead of text.")
    s = sp.add_parser("set", help="Set a limit in limits.json.")
    s.add_argument("kind", choices=kinds)
    s.add_argument("value")
    s.add_argument("--agent", choices=[*agents, "default"], default="default")
    s = sp.add_parser("unset", help="Remove a limit from limits.json (the default applies again).")
    s.add_argument("kind", choices=kinds)
    s.add_argument("--agent", choices=[*agents, "default"], default="default")
    s = sp.add_parser("extend", help="Ask for a signed Extend of a halted session, and resume it on an allow.")
    s.add_argument("session", help="The halted session id.")
    s.add_argument("--agent", choices=agents, default=None)
    s.add_argument("--hold", type=float, default=None,
                   help=f"Seconds to wait for the answer (default {DEFAULT_EXTEND_HOLD:g}; at most "
                        f"{claude_code.GATE_HOLD_CEILING:g}).")
    p.set_defaults(func=run)
