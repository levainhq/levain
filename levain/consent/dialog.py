"""levain.consent.dialog — record, unsigned, what the agent's own dialog did with a request levain left to it.

Levain's activation hooks call :func:`observe` from the agent's ``PostToolUse`` (and Claude Code's
``PostToolUseFailure``), ``Stop`` and ``UserPromptSubmit``: a tool run closes the request filed for that same
call (agent, session, tool name, exact tool input) as ``ran``; the end of a turn, or the start of a later one,
closes every other request left to the dialog in that session as ``did_not_run`` (:meth:`ConsentQueue.record_ran`,
:meth:`ConsentQueue.close_unrun`). No hook payload says how a dialog was answered (measured on Claude Code 2.1.302
and Codex 0.162.1): a run is the only sign of a yes, and its absence cannot tell a no from an abandoned prompt.
Vendor transcripts are never read. Nothing here is authority: an outcome never answers, grants or changes a
request's decision.

Only an existing queue is written: no queue directory or database means levain filed nothing, and nothing is
created. Every failure is silent: these run inside the agent's hooks.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["LABELS", "observe", "session_key"]

# how each outcome reads wherever a request is listed: never a signed answer's words
LABELS = {
    "ran": "ran: answered yes in the agent's dialog, unsigned",
    "ran_unasked": "ran with no dialog answer (the agent's own standing approval), unsigned",
    "did_not_run": "did not run: no or abandoned in the agent's dialog, unsigned",
    None: "in the agent's dialog: no outcome seen yet, unsigned",
}
TOOL_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
TURN_EVENTS = frozenset({"Stop", "UserPromptSubmit"})


def session_key(payload: Mapping[str, Any]) -> str | None:
    """The session a consent hook files under for this payload: ``session_id``, with ``#agent:<agent_id>`` for a
    Claude Code subagent (the adapter's own rule)."""
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    agent_id = payload.get("agent_id")
    return f"{session}#agent:{agent_id}" if isinstance(agent_id, str) and agent_id else session


def observe(agent: str, event: str, payload: Any, queue_dir: Path | None = None) -> Any:
    """Record what ``event`` (``payload`` as the agent sent it) says about requests ``agent`` filed: the
    (id, outcome) a tool run closed, the ids a turn boundary closed, or ``None`` (nothing to record, no queue,
    or any failure)."""
    try:
        if not isinstance(payload, Mapping) or (event not in TOOL_EVENTS and event not in TURN_EVENTS):
            return None
        session = session_key(payload)
        if session is None:
            return None
        from levain.consent.queue import DB_NAME, ConsentQueue, authority_source, default_queue_dir

        qdir = Path(queue_dir) if queue_dir is not None else default_queue_dir()
        if not (qdir / DB_NAME).is_file():
            return None
        # no authority: recording an outcome verifies nothing and decides nothing
        queue = ConsentQueue(qdir, home=Path.home(), authority=authority_source(qdir, anchor=None))
        if event in TOOL_EVENTS:
            name = payload.get("tool_name")
            if not isinstance(name, str) or not name:
                return None
            return queue.record_ran(agent=agent, agent_session_id=session, tool_name=name,
                                    tool_input=payload.get("tool_input"), event=event,
                                    tool_use_id=payload.get("tool_use_id"))
        return queue.close_unrun(agent=agent, agent_session_id=session, event=event)
    except Exception:  # noqa: BLE001 - a hook never fails the agent over a record
        return None
