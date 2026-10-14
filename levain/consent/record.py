"""levain.consent.record — an external agent's consent gate, as the queue holds it.

Claude Code and Codex each fire a ``PermissionRequest`` hook when they are about to ask the user to approve
a tool call. The hook files the request here; the cockpit (or the operator's phone) answers it with a signed
record; the hook delivers that answer, or nothing (the native dialog stands). This module is the request
record and the bytes an answer signs. The queue (:mod:`levain.consent.queue`) holds the state.

A request's id is sealed by :func:`levain.autonomic.pending.seal_pending_id`, the same content fingerprint a
manual pending carries: recomputing it from the stored fields detects any alteration. It is keyless, so it
catches a buggy writer and corruption, not a hostile process that can recompute it; what makes an answer
authority is its signature (:func:`answer_message`), never the request row.

Each request carries an ``event_id``: the hook's own unique event id when its agent supplies one, else an
unpredictable random one. It is sealed (as the seal's ``proposal_id``) and inside the digest an answer signs,
so two permission events with identical content are two requests, and one answer authorises one of them.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from levain.autonomic.pending import seal_pending_id
from levain.cockpit.consent import ConsentError, encode, parse_stamp, record_sha256, value_sha256

__all__ = ["AGENTS", "ANSWER_FORMAT", "ANSWER_NAMESPACE", "DECISIONS", "DISPLAY_MAX", "POSTURE",
           "ExternalGateRequest", "answer_message", "answer_payload", "redact_display", "tool_input_digest"]

AGENTS: tuple[str, ...] = ("claude_code", "codex")
DECISIONS: tuple[str, ...] = ("allow", "deny")
# the posture name inside the seal: an external gate's request never seals equal to a manual pending
POSTURE = "external-gate"
ANSWER_FORMAT = "levain-consent-answer/1"
# its own SSHSIG namespace: an answer verifies as nothing else, and nothing else verifies as an answer
ANSWER_NAMESPACE = "levain-consent-answer"
DISPLAY_MAX = 2000
_SECRET_KEY = re.compile(r"(?i)(token|secret|passw(or)?d|api[_-]?key|auth|credential|private|cookie)")
_MASK = "[redacted]"


def tool_input_digest(tool_input: Any) -> str:
    """The digest of the whole tool input the agent asked about. JCS (``cockpit.consent``) when the value is
    in its domain; a float (outside it) falls back to sorted compact JSON, under another prefix so the two
    schemes never compare equal."""
    try:
        return "jcs:" + value_sha256(tool_input)
    except ConsentError:
        text = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                          default=str)
        return "json:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_MASK if isinstance(k, str) and _SECRET_KEY.search(k) else _mask(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v) for v in value]
    return value


def redact_display(tool_name: str, tool_input: Any) -> str:
    """What the operator is shown: the tool and its input with secret-named keys masked, bounded to
    :data:`DISPLAY_MAX` characters. A display, never what is signed: the answer binds the digest of the
    whole input. A secret inside a free-text value (a shell command) is NOT masked: the queue does not parse
    a command (K3)."""
    body = json.dumps(_mask(tool_input), sort_keys=True, ensure_ascii=False, default=str)
    text = f"{tool_name} {body}"
    return text if len(text) <= DISPLAY_MAX else text[:DISPLAY_MAX - 1] + "…"


def _need(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConsentError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ExternalGateRequest:
    """One gate an external agent is waiting on. Build it with :meth:`create`, the only way to mint a
    sealed id; :meth:`seal_matches` re-derives it from the fields."""

    id: str
    agent: str
    agent_session_id: str
    event_id: str
    tool_name: str
    tool_input_digest: str
    display: str
    cwd: str
    tier: str
    received_at: str
    expires_at: str

    @staticmethod
    def _seal(*, agent: str, agent_session_id: str, event_id: str, tool_name: str, tool_input_digest: str,
              display: str, cwd: str, tier: str, received_at: str, expires_at: str) -> str:
        return seal_pending_id(
            created_at=received_at, context_id=agent_session_id, action_name=f"{agent}:{tool_name}",
            payload=tool_input_digest, proposal_id=event_id, posture=POSTURE, fail_open=False,
            requires_typed=False, expires_at=expires_at, authority={"agent": agent, "cwd": cwd, "tier": tier},
            query_text=display, query_date=received_at[:10], producers=(agent,))

    @classmethod
    def create(cls, *, agent: str, agent_session_id: str, event_id: str, tool_name: str, tool_input_digest: str,
               display: str, cwd: str, tier: str, received_at: str, expires_at: str) -> "ExternalGateRequest":
        if agent not in AGENTS:
            raise ConsentError(f"agent must be one of {AGENTS}: {agent!r}")
        for name, value in (("agent_session_id", agent_session_id), ("event_id", event_id),
                            ("tool_name", tool_name), ("tool_input_digest", tool_input_digest), ("cwd", cwd),
                            ("tier", tier)):
            _need(name, value)
        if not isinstance(display, str):
            raise ConsentError("display must be a string")
        if parse_stamp(received_at) >= parse_stamp(expires_at):
            raise ConsentError("expires_at must be after received_at")
        fields = dict(agent=agent, agent_session_id=agent_session_id, event_id=event_id, tool_name=tool_name,
                      tool_input_digest=tool_input_digest, display=display, cwd=cwd, tier=tier,
                      received_at=received_at, expires_at=expires_at)
        return cls(id=cls._seal(**fields), **fields)

    def fields(self) -> dict[str, str]:
        return {"id": self.id, "agent": self.agent, "agent_session_id": self.agent_session_id,
                "event_id": self.event_id, "tool_name": self.tool_name, "tool_input_digest": self.tool_input_digest,
                "display": self.display, "cwd": self.cwd, "tier": self.tier, "received_at": self.received_at,
                "expires_at": self.expires_at}

    def seal_matches(self) -> bool:
        f = self.fields()
        f.pop("id")
        return self.id == self._seal(**f)

    @property
    def digest(self) -> str:
        """sha256 of the JCS bytes of every field: what an answer binds."""
        return record_sha256(encode(self.fields()))


def answer_payload(*, queue_id: str, request: ExternalGateRequest, decision: str, signer_fp: str) -> dict[str, str]:
    """The record an answer signs: this queue, this request, its digest and expiry, the decision, the key."""
    if decision not in DECISIONS:
        raise ConsentError(f"decision must be one of {DECISIONS}: {decision!r}")
    return {"format": ANSWER_FORMAT, "queue_id": _need("queue_id", queue_id), "request_id": request.id,
            "request_digest": request.digest, "decision": decision, "expires_at": request.expires_at,
            "signer_fp": _need("signer_fp", signer_fp)}


def answer_message(payload: dict[str, str]) -> bytes:
    """The exact bytes signed, in :data:`ANSWER_NAMESPACE`: the payload's JCS encoding."""
    return encode(payload)
