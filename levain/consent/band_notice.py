"""levain.consent.band_notice — the two events a held consent request sends its notify command, for a band row to follow.

Phill 11-05: a held request's ping was a bare ping; the row he can act on is a NEEDS YOU band row whose approve / deny he
SIGNS on levain's own consent sheet (``/sign/consent/<id>``). Levain keeps the only consent signature; the notify command
(the one ``notify_argv`` names in ``claude_code.json`` / ``codex.json``) files and resolves that row, and holds no
decision. This module gives the command what it needs, as environment variables added to the child it already runs (its
argv and message are unchanged):

=============================  =====================================================================================
``LEVAIN_CONSENT_EVENT``       ``held`` | ``settled``
``LEVAIN_CONSENT_REQUEST``     the request id (both events)
``LEVAIN_CONSENT_AGENT``       held: the agent that waits (``claude_code``, ``codex``)
``LEVAIN_CONSENT_WHAT``        held: the call as the sheet shows it (``request.display`` made visible, cut to
                               :data:`WHAT_MAX` characters, one line)
``LEVAIN_CONSENT_EXPIRES``     held: the end of the request's hold, an ISO UTC stamp
``LEVAIN_CONSENT_SHEET_URL``   held: ``<cockpit origin>/sign/consent/<url-encoded id>``; left out when the install
                               stores no cockpit origin (:mod:`levain.consent.cockpit_origin`), never a guessed host
``LEVAIN_CONSENT_OUTCOME``     settled: ``allowed`` | ``denied`` | ``withdrawn`` | ``expired``
=============================  =====================================================================================

The HELD event rides the hold's ping (:func:`levain.consent.adapters.claude_code.hold_ping` and the PreToolUse gates'
:func:`~levain.consent.adapters.claude_code.notify`). The SETTLED event is sent when the request leaves ``pending`` for
good, from whichever process moved it: the queue calls :func:`announce_settled` after the write that moved it has
committed (:meth:`levain.consent.queue.ConsentQueue._write`), because the answer, the withdrawal and the expiry are made
by different processes (the hook, the cockpit server, a sweep) and the queue's own transition is the one place they share.
A request that leaves ``pending`` as ``undecided`` (its hold ended with no verified decision) is announced as ``expired``:
the call was not run, and nothing he signs from then on allows it.

Only a request whose HELD event started is ever announced as settled. That is recorded as a mark file in the queue
directory (``band-held/<digest of the id>``), made before the held command starts and removed by the first settle to claim
it, which is what makes the announcement happen once. It is not a second copy of the request's state (the queue still holds
that, and says whether the request is pending); it records only that a notify command was told of this request. Every
request levain decides at once (a grant, a policy) or hands to the agent's own dialog with no hold has no mark, so the
queue's commonest transitions cost one failed ``unlink`` and start nothing.

Nothing here decides anything. A command that cannot start, or a mark that cannot be written or removed, changes no
decision and raises into no gate.
"""
from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["MARK_DIR", "OUTCOMES", "WHAT_MAX", "announce_settled", "held_env", "mark_held", "settled_env", "sheet_url",
           "unmark_held"]

PREFIX = "LEVAIN_CONSENT_"
MARK_DIR = "band-held"
WHAT_MAX = 300                       # the sheet's own text can be long; a variable carries a line of it
# the queue's state a request moved to -> the outcome the command is told (undecided: the hold ended unanswered)
OUTCOMES: Mapping[str, str] = {"allowed": "allowed", "denied": "denied", "withdrawn": "withdrawn", "expired": "expired",
                               "undecided": "expired"}
_CONTROL = re.compile("[\x00-\x1f\x7f]")


def _line(text: object, limit: int) -> str:
    """``text`` on one line, no control character (a NUL cannot ride an environment), cut in the middle past ``limit``."""
    clean = _CONTROL.sub(" ", str(text)).strip()
    if len(clean) <= limit:
        return clean
    head = limit // 3
    return clean[:head] + "…" + clean[-(limit - head - 1):]


def sheet_url(queue_dir: Path | str, request_id: str) -> str | None:
    """The request's sign sheet on the cockpit origin this install stores, else None."""
    from levain.consent.cockpit_origin import read_origin

    origin = read_origin(queue_dir)
    return None if origin is None else f"{origin}/sign/consent/{urllib.parse.quote(request_id, safe='')}"


def held_env(queue_dir: Path | str, request: Any) -> dict[str, str]:
    """The HELD event's variables for ``request`` (a sealed :class:`~levain.consent.queue.ExternalGateRequest`)."""
    from levain.cockpit.hintview import visible

    env = {PREFIX + "EVENT": "held", PREFIX + "REQUEST": request.id, PREFIX + "AGENT": _line(request.agent, 80),
           PREFIX + "WHAT": _line(visible(request.display), WHAT_MAX), PREFIX + "EXPIRES": request.expires_at}
    url = sheet_url(queue_dir, request.id)
    if url is not None:
        env[PREFIX + "SHEET_URL"] = url
    return env


def settled_env(request_id: str, outcome: str) -> dict[str, str]:
    """The SETTLED event's variables."""
    return {PREFIX + "EVENT": "settled", PREFIX + "REQUEST": request_id, PREFIX + "OUTCOME": outcome}


def _mark_path(queue_dir: Path | str, request_id: str) -> Path:
    return Path(queue_dir) / MARK_DIR / hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]


def mark_held(queue_dir: Path | str, request_id: str) -> bool:
    """Record that a notify command is about to be told of ``request_id`` (see the module words). False when the mark
    could not be made: the caller then sends no held event, so no settle is owed."""
    path = _mark_path(queue_dir, request_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    except OSError:
        return False
    return True


def unmark_held(queue_dir: Path | str, request_id: str) -> bool:
    """Remove the mark; True only for the caller that removed it (the claim that makes a settle once)."""
    try:
        _mark_path(queue_dir, request_id).unlink()
    except OSError:
        return False
    return True


def announce_settled(queue_dir: Path | str, request: Any, state: str) -> bool:
    """Run the request's agent's notify command once with the SETTLED event, when a HELD event went out for it. True
    when the command started. Never raises."""
    try:
        outcome = OUTCOMES.get(state)
        if outcome is None or not unmark_held(queue_dir, request.id):
            return False
        from levain.consent.adapters import claude_code, codex

        names = {claude_code.AGENT: claude_code.SETTINGS_NAME, codex.AGENT: codex.SETTINGS_NAME}
        name = names.get(request.agent)
        if name is None:
            return False
        settings = claude_code.read_settings_file(Path(queue_dir) / name)
        return claude_code.run_notify_command(settings, f"levain: {request.agent}'s approval request was {outcome}.",
                                              cwd=queue_dir, env_extra=settled_env(request.id, outcome),
                                              phone_url=False)
    except Exception:  # noqa: BLE001 - a notice never changes a decision
        return False
