"""levain.cockpit.stream: the cockpit's freshness stream (``GET /cockpit/events``, server-sent events).

The stream carries no panel data. It tells a page WHICH panels moved; the page reads them through
the same panel route as any read. Its only source is the engine's freshness map (metadata, runs no
provider), diffed once per tick per stream, so a panel that moved twice between two ticks is
announced once: that is the per-panel coalescing.

Resume is by state, not by history: every connection opens with ``hello`` carrying the whole
freshness map, and the client refetches whatever differs from what it holds. Event ids carry the
server's epoch so a client can tell a restart from a blip; no event buffer is kept."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

EVENTS_PATH = "/cockpit/events"
CONTENT_TYPE = "text/event-stream; charset=utf-8"
DEFAULT_TICK_S = 2.0
DEFAULT_HEARTBEAT_S = 15.0
DEFAULT_MAX_STREAMS = 8


def frame(event: str, data: Any, event_id: str | None = None) -> bytes:
    head = (f"id: {event_id}\n" if event_id else "") + f"event: {event}\n"
    return (head + "data: " + json.dumps(data, separators=(",", ":")) + "\n\n").encode("utf-8")


def epoch_of(last_event_id: str | None) -> str:
    return (last_event_id or "").split(":", 1)[0]


def run(
    write: Callable[[bytes], None], freshness: Callable[[], dict[str, dict[str, Any]]], *, epoch: str,
    last_event_id: str | None, stop: threading.Event, tick_s: float = DEFAULT_TICK_S,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S, clock: Callable[[], float] = time.monotonic,
) -> None:
    """Drive one stream until ``stop`` is set or ``write`` raises (a gone client ends the loop).
    A failing freshness read also ends it: the client reconnects and is told the whole state again."""
    seq = 0

    def emit(event: str, data: Any) -> None:
        nonlocal seq, last_write
        seq += 1
        write(frame(event, data, f"{epoch}:{seq}"))
        last_write = clock()

    last_write = clock()
    held = freshness()
    emit("hello", {"epoch": epoch, "resumed": epoch_of(last_event_id) == epoch, "freshness": held})
    while not stop.wait(tick_s):
        now_map = freshness()
        for pid, entry in now_map.items():
            if held.get(pid) != entry:
                emit("panel-changed", {"id": pid, "as_of": entry.get("as_of"), "status": entry.get("status")})
        held = now_map
        if clock() - last_write >= heartbeat_s:
            write(b": hb\n\n")
            last_write = clock()
