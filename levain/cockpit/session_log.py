"""levain.cockpit.session_log — one agent session's transcript, read back for the token-gated cockpit route (Q8).

The hooks append ``<install>/.levain/sessions/<stem>.log.jsonl`` beside the Q7 registry file ``<stem>.json``, one
line per event (DESIGN_Q8.md, the Contract): ``{"v": 1, "at", "kind": "prompt"|"reply"|"tool"|"wake"|"wake_reply", "text", "cut"}``.
This reader is a display, never an authority: nothing here decides anything. Every read is bounded (the registry
file through the Q7 reader, the log's last :data:`LOG_TAIL` bytes), never follows a symlink, and never writes.

The body names the session id and its full working directory and carries the operator's prompts, so it is served
only behind a surface token on every bind (``web_server``'s ``/cockpit/session/<stem>.json``), never in a
token-free panel.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.providers import SESSION_SKEW_S, _session_record, _session_stamp

__all__ = ["LOG_TAIL", "EVENTS_SHOWN", "STEM", "read_session"]

LOG_TAIL = 256 * 1024               # bytes of the log read from its end; the hooks keep it near this size
EVENTS_SHOWN = 300                  # the newest events returned
STEM = re.compile(r"[0-9a-f]{24}")
EVENT_KINDS = ("prompt", "reply", "tool", "wake", "wake_reply")


def _tail(path: Path) -> bytes | None:
    """The last :data:`LOG_TAIL` bytes of a regular file as of one fstat, cut to whole lines. The read is a pread
    bounded to that size, so an append racing it never shifts the window; when the window starts mid-file the byte
    before it is read too and everything through the first newline is dropped (a line the cut split is not a line),
    and an unterminated last line (an append in flight) is dropped too. The hooks rewrite the log by tmp + replace,
    never in place, so the open file is never cut under this read. None when there is no log (or it is not a
    regular file of ours)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:     # missing, a symlink (O_NOFOLLOW), unreadable: no log
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        start = max(0, st.st_size - LOG_TAIL)
        if start == 0:
            raw = os.pread(fd, st.st_size, 0)
        else:
            raw = os.pread(fd, st.st_size - start + 1, start - 1)
            nl = raw.find(b"\n")
            raw = b"" if nl < 0 else raw[nl + 1:]
    finally:
        os.close(fd)
    end = raw.rfind(b"\n")
    return raw[:end + 1] if end >= 0 else b""


def _event(line: bytes) -> dict[str, Any] | None:
    """One log line as the Contract's event, or None for any other shape."""
    try:
        d = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or type(d.get("v")) is not int or d["v"] != 1 or d.get("kind") not in EVENT_KINDS:
        return None
    at, text, cut = d.get("at"), d.get("text"), d.get("cut")
    if not (isinstance(at, str) and at.endswith("Z") and _session_stamp(at) is not None and isinstance(text, str)):
        return None
    if not isinstance(cut, int) or isinstance(cut, bool) or cut < 0:
        return None
    return {"at": at, "kind": d["kind"], "text": text, "cut": cut}


def _resume(agent: str, cwd: str, sid: str) -> str:
    verb = "claude --resume" if agent == "claude_code" else "codex resume"
    return f"cd -- {shlex.quote(cwd)} && {verb} {shlex.quote(sid)}"


def _consent(consent_reader: Callable[[datetime], Any] | None, now: datetime, agent: str, sid: str
             ) -> list[dict[str, Any]] | None:
    """This session's consent requests, pending and decided, oldest first by when each was filed (``at``;
    ``decided_at`` is when it was answered or expired, None while pending); None when the queue could not be read
    (said, never drawn as "no requests")."""
    if consent_reader is None:
        return []
    try:
        listing = consent_reader(now)
        rows = [{"at": e.request.received_at, "tool": e.request.tool_name, "state": e.state, "via": e.via,
                 "decided_at": e.decided_at}
                for e in (*listing.pending, *listing.undecided, *listing.recent)
                if e.request.agent == agent and e.request.agent_session_id == sid]
        rows.sort(key=lambda r: r["at"])
    except Exception:  # noqa: BLE001 - an unread queue is an answer (None), never a dead transcript
        return None
    return rows


def read_session(install_root: Path, stem: str, now: datetime,
                 consent_reader: Callable[[datetime], Any] | None) -> dict[str, Any] | None:
    """The Route body of DESIGN_Q8.md's Contract for one session, or None when ``stem`` is not a stem or its
    registry file is missing or is not a record of ours (the Q7 reader's checks, plus the listing's clock-skew
    check). A missing log is a session with no events yet, not an error. ``skipped`` counts the log lines in the
    window read that are not events; the partial first line a mid-file read cuts is not counted."""
    if not isinstance(stem, str) or not STEM.fullmatch(stem):
        return None
    sdir = Path(install_root) / ".levain" / "sessions"
    rec = _session_record(sdir / f"{stem}.json")
    if rec is None or (now - rec["_seen"]).total_seconds() < -SESSION_SKEW_S:
        return None
    events: list[dict[str, Any]] = []
    skipped = 0
    raw = _tail(sdir / f"{stem}.log.jsonl")
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        ev = _event(line)
        if ev is None:
            skipped += 1
        else:
            events.append(ev)
    agent, sid, cwd = rec["agent"], rec["session_id"], rec["cwd"]
    from levain.cockpit.session_context import CONTEXT_WARN_TOKENS, find_transcript, last_context

    try:
        path, why = find_transcript(rec)
        ctx, why = last_context(path, agent) if path is not None else (None, why)
    except Exception:  # noqa: BLE001 - a meter that cannot read is said, never a dead session view
        ctx, why = None, "unreadable"
    context = ({"tokens": ctx.tokens, "window": ctx.window, "model": ctx.model, "warn": ctx.tokens >= CONTEXT_WARN_TOKENS,
                "compacted": ctx.compacted, "read_at": now.isoformat()}
               if ctx else {"tokens": None, "window": None, "model": None, "warn": False, "compacted": False,
                            "read_at": now.isoformat(), "why": why})
    return {"context": context, "agent": agent, "workspace": cwd, "first_seen": rec["first_seen"], "last_seen": rec["last_seen"],
            "resume": _resume(agent, cwd, sid), "events": events[-EVENTS_SHOWN:],
            "consent": _consent(consent_reader, now, agent, sid), "skipped": skipped}
