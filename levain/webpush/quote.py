"""levain.webpush.quote — is the quote a setter gives for a presence word something the operator typed?

A seat or the coordinator sets ``away`` or ``remote`` (the coordinator also ``quiet`` and ``sleep``) on "his words" and
quotes them (:func:`levain.webpush.presence.set_word`). This module reads the setter's own session transcript and finds
the quote in one of the operator's turns there, so the record can say whether the quote is his and when he typed it
(P2, ``project_memory/1030-2-presence/DESIGN.md`` section 4).

What counts as an operator turn:

- Claude Code: a ``type == "user"`` entry whose ``origin.kind`` is ``human``, not ``isMeta``, not a sidechain, whose
  content is text (no ``tool_result``). A relay from another session (``origin.kind`` ``peer``), a task notification, a
  teammate message and a transcript from before ``origin`` was written all fail that test and read as no turn: a desk's
  retelling of what he said is not him saying it.
- Codex: an ``event_msg`` ``item_completed`` whose item is a ``UserMessage`` (the rollout format of October 2026), or an
  ``event_msg`` ``user_message`` (the format until about 10-10). Codex marks no origin, so every such turn counts.
- The coordinator: the text of a drop it names (``--drop``), because a drop reaches the coordinator in its brief file
  and never as a prompt. The drop's ``received_at`` is when he said it.

The quote matches when, after case and whitespace are folded, it is at least :data:`MIN_CHARS` long and a substring of
one of the last :data:`TURNS` operator turns, and that turn is no more than :data:`WINDOW_SECS` old. A transcript that
is missing, unreadable, oversized beyond :data:`MAX_BYTES` (its tail is read) or of an unknown agent is a
:class:`Match` with ``verified`` False and the reason, never an error: the cheap error stays cheap.

Stdlib only.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["MAX_BYTES", "MIN_CHARS", "Match", "TURNS", "WINDOW_SECS", "locate_transcript", "match", "match_drop",
           "match_transcript"]

MIN_CHARS = 12
TURNS = 20
WINDOW_SECS = 6 * 3600
MAX_BYTES = 128 << 20
_LINE_CAP = 1 << 20
_SID = re.compile(r"[A-Za-z0-9_-]{8,80}")
_DROP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FILTER = {"claude_code": (b'"human"',), "codex": (b'"UserMessage"', b'"user_message"')}


@dataclass(frozen=True)
class Match:
    """``verified``: the quote is in an operator turn no older than :data:`WINDOW_SECS`; ``said_at``: when that turn was
    typed (epoch seconds, the newest matching turn), ``None`` when not verified; ``why``: the reason, for the log."""
    verified: bool
    said_at: float | None
    why: str


def _no(why: str) -> Match:
    return Match(False, None, why)


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def _stamp(value: object) -> float | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        t = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return (t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)).timestamp()


def _texts(content: object) -> str | None:
    """The text of a turn's content (a string, or a list of blocks with no ``tool_result``), else ``None``."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") == "tool_result":
            return None
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(parts) if parts else None


def _claude_turn(d: dict) -> tuple[float, str] | None:
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    origin = d.get("origin")
    if not (isinstance(origin, dict) and origin.get("kind") == "human"):
        return None
    msg = d.get("message")
    text = _texts(msg.get("content")) if isinstance(msg, dict) else None
    at = _stamp(d.get("timestamp"))
    return (at, text) if text and at is not None else None


def _codex_turn(d: dict) -> tuple[float, str] | None:
    p = d.get("payload")
    if d.get("type") != "event_msg" or not isinstance(p, dict):
        return None
    text = None
    if p.get("type") == "user_message" and isinstance(p.get("message"), str):
        text = p["message"]
    elif p.get("type") == "item_completed" and isinstance(p.get("item"), dict) and p["item"].get("type") == "UserMessage":
        text = _texts(p["item"].get("content"))
    at = _stamp(d.get("timestamp"))
    return (at, text) if text and at is not None else None


def _lines(path: Path):
    """The transcript's lines (bytes), at most :data:`MAX_BYTES` of its tail, a line over :data:`_LINE_CAP` skipped."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        start = max(0, st.st_size - MAX_BYTES)
        with os.fdopen(os.dup(fd), "rb") as fh:
            fh.seek(start)
            if start:
                fh.readline(_LINE_CAP)   # the cut first line
            while True:
                line = fh.readline(_LINE_CAP + 1)
                if not line:
                    return
                if len(line) > _LINE_CAP and not line.endswith(b"\n"):
                    while True:
                        rest = fh.readline(_LINE_CAP)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                yield line
    finally:
        os.close(fd)


def match_transcript(path: Path | str, agent: str, said: str, *, now: float) -> Match:
    """Find ``said`` in the last :data:`TURNS` operator turns of the transcript at ``path``."""
    needle = _fold(said)
    if len(needle) < MIN_CHARS:
        return _no(f"the quote is under {MIN_CHARS} characters")
    if agent not in _FILTER:
        return _no(f"no transcript reader for agent {agent!r}")
    turn = _claude_turn if agent == "claude_code" else _codex_turn
    turns: collections.deque[tuple[float, str]] = collections.deque(maxlen=TURNS)
    try:
        for line in _lines(Path(path)):
            if not any(tok in line for tok in _FILTER[agent]):
                continue
            try:
                d = json.loads(line)
            except (ValueError, RecursionError):
                continue
            got = turn(d) if isinstance(d, dict) else None
            if got is not None:
                turns.append(got)
    except OSError as e:
        return _no(f"transcript unreadable ({type(e).__name__})")
    if not turns:
        return _no("no operator turn in the transcript")
    hits = [at for at, text in turns if now - at <= WINDOW_SECS and needle in _fold(text)]
    if hits:
        return Match(True, max(hits), "the quote is in an operator turn of the transcript")
    return _no(f"the quote is in none of the last {min(len(turns), TURNS)} operator turns within "
               f"{WINDOW_SECS // 3600} h")


def match_drop(directory: Path | str, drop_id: str, said: str, *, now: float) -> Match:
    """Find ``said`` in the words of drop ``drop_id`` in the coordinator store under ``directory`` (read-only)."""
    needle = _fold(said)
    if len(needle) < MIN_CHARS:
        return _no(f"the quote is under {MIN_CHARS} characters")
    db = Path(directory) / "dispatch.db"
    if not isinstance(drop_id, str) or _DROP_ID.fullmatch(drop_id) is None:
        return _no("the drop id is not a drop id")
    if not db.exists():
        return _no("no coordinator store here")
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT words, received_at FROM drops WHERE drop_id = ?", (drop_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return _no(f"the coordinator store cannot be read ({type(e).__name__})")
    if row is None:
        return _no(f"no drop {drop_id}")
    at = _stamp(row[1])
    if at is None:
        return _no(f"drop {drop_id} has no readable time")
    if now - at > WINDOW_SECS:
        return _no(f"drop {drop_id} is older than {WINDOW_SECS // 3600} h")
    if needle not in _fold(str(row[0])):
        return _no(f"the quote is not in drop {drop_id}")
    return Match(True, at, f"the quote is in drop {drop_id}")


def locate_transcript(agent: str, session: str, roots: list[Path]) -> Path | None:
    """The setter's transcript: the path its session record holds (``<root>/.levain/sessions/<key>.json``, under any
    of ``roots`` or their parents), else the agent's own default location for the session."""
    if not session or _SID.fullmatch(session) is None:
        return None
    from levain.cockpit.session_context import find_transcript

    key = hashlib.sha1(f"{agent}:{session}".encode("utf-8", "replace")).hexdigest()[:24]
    cwd = ""
    for root in roots:
        for d in (root, *root.parents):
            rec = d / ".levain" / "sessions" / f"{key}.json"
            try:
                with open(rec, "rb") as fh:
                    data = json.loads(fh.read(65536))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(data, dict) and data.get("session_id") == session and data.get("agent") == agent:
                path, _why = find_transcript(data)
                if path is not None and path.is_file():
                    return path
                cwd = cwd or str(data.get("cwd") or "")
    for c in ([cwd] if cwd else []) + [str(r) for r in roots]:
        path, _why = find_transcript({"agent": agent, "session_id": session, "cwd": c})
        if path is not None and path.is_file():
            return path
    if agent == "claude_code":   # the session's working directory is not this process's: the id names the file
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
        base = Path(cfg) if cfg and os.path.isabs(cfg) else Path.home() / ".claude"
        hits = sorted(base.glob(f"projects/*/{session}.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
        hits = [h for h in hits if h.is_file() and not h.is_symlink()]
        if hits:
            return hits[-1]
    return None


def match(agent: str, session: str, said: str, *, now: float, roots: list[Path] | None = None,
          directory: Path | str | None = None, drop: str | None = None) -> Match:
    """The strongest evidence for ``said`` from the setter's transcript and, for the coordinator, the named drop:
    verified when either finds it (the newer ``said_at`` wins), else the transcript's reason."""
    path = locate_transcript(agent, session, roots or [])
    got = match_transcript(path, agent, said, now=now) if path is not None else _no("no transcript found for the session")
    if drop and directory is not None:
        other = match_drop(directory, drop, said, now=now)
        if other.verified and (not got.verified or (other.said_at or 0.0) > (got.said_at or 0.0)):
            return other
        if not got.verified:
            return Match(False, None, f"{got.why}; {other.why}")
    return got
