"""levain.desk.room_live: the room's EPHEMERAL live text (492 R3, ruling R-C).

While the room's child is writing a reply, its ``stream_event`` text deltas accumulate here, one buffer per launch, so a
reader can show the words as they are written. The buffer is NEVER recorded: the append-only store holds only the whole
``agent_text`` the child sends when a block is done, and the buffer is cleared the moment that frame arrives (so the text
is never in both), at the turn's end, when the child's output ends, and when the launch ends.

**Process topology (why this is memory, not a file).** One ``levain serve`` process runs both the pump thread that reads
the child's stdout (``RoomServe.pump_once`` -> ``ChatService.read_next`` -> ``ClaudeSession._ingest``) and the HTTP
handler threads that serve ``GET /cockpit/room.json`` (``web_server.make_server`` builds the ``RoomServe`` and mounts it
as ``httpd.room_host``). The two share this module's dict under :data:`_LOCK`. A reader in another process would see
nothing here; none exists today.

**Bounds.** At most :data:`LIVE_MAX` UTF-8 bytes of text per launch (the head, the same rule and the same number as the
stored ``agent_text`` cap, ``claude_session._BODY_MAX``; a test holds the two equal; once a delta is cut there, the
buffer takes nothing more until it is cleared), at most :data:`LAUNCH_MAX` launches held (past it the oldest-touched
buffer with no live text is dropped first). A launch :func:`forget` ended never gets a buffer again. There is NO redaction here because there is none on stored
``agent_text`` either: both carry the model's words as written, and the room's containment is the child's argv, not a
scrub of its output.

Stdlib only; imports no levain module."""
from __future__ import annotations

import threading
from typing import Any, Optional

__all__ = ["LIVE_MAX", "LAUNCH_MAX", "Sink", "snapshot", "clear", "forget"]

# [seat] equals claude_session._BODY_MAX (a test holds the two equal)
LIVE_MAX = 16 * 1024
# [seat, UNMEASURED] launches whose buffer is held at once; a room holds one, so this only bounds a leak
LAUNCH_MAX = 8

# [seat] ended launch ids remembered so a delta already in flight when the launch ended cannot re-create its buffer
# (L3 stream r1 codex + complement MED); launch ids are never reused, so this only needs to outlive the pump's last read;
# edge: past 4096 ended launches in one process the oldest tombstone goes, and a delta still in flight for it could re-create a buffer
ENDED_MAX = 4096

_LOCK = threading.Lock()
_BUF: dict[str, dict[str, Any]] = {}      # launch_id -> {"turn", "text", "bytes", "seq"}; text "" = nothing live
_SEQ = 0                                 # ONE process-wide counter: every change takes the next number, so no seq ever goes back
_ENDED: dict[str, None] = {}              # launch ids :func:`forget` ended, oldest first (an ordered set)


def _entry(launch_id: str) -> Optional[dict[str, Any]]:
    """The launch's buffer, made most recent; None for an ended launch (it is never re-created). Past
    :data:`LAUNCH_MAX` the oldest buffer with NO live text is dropped first; an open buffer (mid-reply) goes only when
    every other one is open too (L3 stream r1 gemini MED). ``seq`` comes from the one global
    counter ``_SEQ``, so a re-created buffer's ``seq`` still only grows."""
    if launch_id in _ENDED:
        return None
    e = _BUF.pop(launch_id, None)          # re-insert last: dict order is the recency order
    if e is None:
        e = {"turn": "", "text": "", "bytes": 0, "seq": 0}
    _BUF[launch_id] = e
    while len(_BUF) > LAUNCH_MAX:
        idle = [k for k, v in _BUF.items() if k != launch_id and not v["text"]]
        _BUF.pop(idle[0] if idle else next(iter(_BUF)))
    return e


def _bump(e: dict[str, Any]) -> None:
    global _SEQ
    _SEQ += 1
    e["seq"] = _SEQ


class Sink:
    """What one :class:`~levain.desk.claude_session.ClaudeSession` writes to: its launch's buffer."""

    def __init__(self, launch_id: str) -> None:
        self.launch_id = launch_id

    def delta(self, turn: Optional[str], text: str) -> None:
        """Append ``text`` to the open buffer (capped at :data:`LIVE_MAX` bytes; a delta past the cap is dropped whole
        or cut at a character edge, the head kept). ``seq`` rises on every change."""
        if not text:
            return
        with _LOCK:
            e = _entry(self.launch_id)
            if e is None:                  # the launch ended while this delta was in flight: nothing is live for it
                return
            room = LIVE_MAX - e["bytes"]
            if room <= 0:
                return
            whole = text.encode("utf-8", "replace")
            raw = whole[:room]
            piece = raw.decode("utf-8", "ignore")       # a character split at the cap is dropped, never half-kept
            if len(raw) < len(whole):
                # the cap is reached: the buffer is the head of the reply (the same bytes the stored agent_text keeps,
                # claude_session._cap), so no later, shorter delta may land after a dropped character
                e["bytes"] = LIVE_MAX
            else:
                e["bytes"] += len(raw)
            if not piece:
                return
            e["turn"] = turn or ""
            e["text"] += piece
            _bump(e)

    def clear(self) -> None:
        clear(self.launch_id)


def clear(launch_id: str) -> None:
    """Drop the live text (a whole frame, a turn end, a stop). A no-op, and no ``seq`` change, when nothing is live."""
    with _LOCK:
        e = _BUF.get(launch_id)
        if e is None or not e["text"]:
            return
        e["text"], e["bytes"] = "", 0
        _bump(e)


def forget(launch_id: str) -> None:
    """The launch is over: drop its buffer, for good. A delta that arrives after this (the pump had
    already polled it when the launch ended) is refused under the same lock, so an ended launch never shows text again,
    whatever order ``forget`` and the session's close run in."""
    with _LOCK:
        _BUF.pop(launch_id, None)
        _ENDED.pop(launch_id, None)
        _ENDED[launch_id] = None
        while len(_ENDED) > ENDED_MAX:
            _ENDED.pop(next(iter(_ENDED)))


def _purge(launch_id: str) -> None:
    """Every trace of ``launch_id``, its end included. Tests only (they reuse one id); production never reuses an id."""
    with _LOCK:
        _BUF.pop(launch_id, None)
        _ENDED.pop(launch_id, None)


def snapshot(launch_id: str) -> Optional[dict[str, Any]]:
    """``{"turn", "text", "seq"}`` while text is live for the launch, else None."""
    with _LOCK:
        e = _BUF.get(launch_id)
        if e is None or not e["text"]:
            return None
        return {"turn": e["turn"], "text": e["text"], "seq": e["seq"]}
