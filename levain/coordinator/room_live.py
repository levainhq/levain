"""levain.coordinator.room_live: the room's EPHEMERAL live text (492 R3, ruling R-C).

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
stored ``agent_text`` cap, ``claude_session._BODY_MAX``; a test holds the two equal), at most :data:`LAUNCH_MAX`
launches held (the oldest-touched is dropped past it). There is NO redaction here because there is none on stored
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

_LOCK = threading.Lock()
_BUF: dict[str, dict[str, Any]] = {}      # launch_id -> {"turn", "text", "bytes", "seq"}; text "" = nothing live
_SEQ: dict[str, int] = {}                 # launch_id -> last seq handed out; survives a clear so seq only grows


def _entry(launch_id: str) -> dict[str, Any]:
    e = _BUF.pop(launch_id, None)          # re-insert last: dict order is the recency order
    if e is None:
        e = {"turn": "", "text": "", "bytes": 0, "seq": _SEQ.get(launch_id, 0)}
    _BUF[launch_id] = e
    while len(_BUF) > LAUNCH_MAX:
        old = next(iter(_BUF))
        _BUF.pop(old)
        _SEQ.pop(old, None)
    return e


def _bump(launch_id: str, e: dict[str, Any]) -> None:
    e["seq"] += 1
    _SEQ[launch_id] = e["seq"]


class Sink:
    """What one :class:`~levain.coordinator.claude_session.ClaudeSession` writes to: its launch's buffer."""

    def __init__(self, launch_id: str) -> None:
        self.launch_id = launch_id

    def delta(self, turn: Optional[str], text: str) -> None:
        """Append ``text`` to the open buffer (capped at :data:`LIVE_MAX` bytes; a delta past the cap is dropped whole
        or cut at a character edge, the head kept). ``seq`` rises on every change."""
        if not text:
            return
        with _LOCK:
            e = _entry(self.launch_id)
            room = LIVE_MAX - e["bytes"]
            if room <= 0:
                return
            raw = text.encode("utf-8", "replace")[:room]
            piece = raw.decode("utf-8", "ignore")
            if not piece:
                return
            e["turn"] = turn or ""
            e["text"] += piece
            e["bytes"] += len(piece.encode("utf-8"))
            _bump(self.launch_id, e)

    def clear(self) -> None:
        clear(self.launch_id)


def clear(launch_id: str) -> None:
    """Drop the live text (a whole frame, a turn end, a stop). A no-op, and no ``seq`` change, when nothing is live."""
    with _LOCK:
        e = _BUF.get(launch_id)
        if e is None or not e["text"]:
            return
        e["text"], e["bytes"] = "", 0
        _bump(launch_id, e)


def forget(launch_id: str) -> None:
    """The launch is over: drop its buffer and its counter."""
    with _LOCK:
        _BUF.pop(launch_id, None)
        _SEQ.pop(launch_id, None)


def snapshot(launch_id: str) -> Optional[dict[str, Any]]:
    """``{"turn", "text", "seq"}`` while text is live for the launch, else None."""
    with _LOCK:
        e = _BUF.get(launch_id)
        if e is None or not e["text"]:
            return None
        return {"turn": e["turn"], "text": e["text"], "seq": e["seq"]}
