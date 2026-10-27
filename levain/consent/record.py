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
from levain.consent.receipt_log import head_problem

__all__ = ["AGENTS", "ANSWER_FORMAT", "ANSWER_FORMAT_UNCHAINED", "ANSWER_NAMESPACE", "AWAY_LEAVE_FORMAT",
           "AWAY_NAMESPACE", "DECISIONS", "DISPLAY_MAX", "GRANT_ANSWER_FORMAT", "POSTURE", "RULING_FORMAT",
           "RULING_FORMAT_UNCHAINED", "RULING_NAMESPACE", "UNCHAINED_FORMATS", "ExternalGateRequest",
           "answer_message", "answer_payload", "away_leave_message", "away_leave_payload", "grant_answer_payload",
           "redact_display", "ruling_message", "ruling_payload", "tool_input_digest"]

AGENTS: tuple[str, ...] = ("claude_code", "codex")
DECISIONS: tuple[str, ...] = ("allow", "deny")
# the posture name inside the seal: an external gate's request never seals equal to a manual pending
POSTURE = "external-gate"
# /2 (Q11): the answer carries ``receipts_head``, the head of the queue's receipt log (levain.consent.receipt_log)
ANSWER_FORMAT = "levain-consent-answer/2"
# the format before the receipts were chained: refused when applied (the request is answered again); one applied
# before the upgrade still delivers, its signature re-verified with no head check, and `levain consent receipts
# verify` lists it as signed before chaining
ANSWER_FORMAT_UNCHAINED = "levain-consent-answer/1"
# its own SSHSIG namespace: an answer verifies as nothing else, and nothing else verifies as an answer
ANSWER_NAMESPACE = "levain-consent-answer"
# a late ruling on a request that went undecided (its hold ended with no verified answer): signed after the hold
# ended, so it is never an answer; its own format and its own SSHSIG namespace, so an answer never verifies as a
# ruling and a ruling never verifies as an answer
# /2 (Q11): the ruling carries ``receipts_head``, as an answer does
RULING_FORMAT = "levain-consent-ruling/2"
RULING_FORMAT_UNCHAINED = "levain-consent-ruling/1"
RULING_NAMESPACE = "levain-consent-ruling"
UNCHAINED_FORMATS = frozenset({ANSWER_FORMAT_UNCHAINED, RULING_FORMAT_UNCHAINED})
# leaving an away mode the operator set with `levain consent away on` (its away mark): signed, since leaving it
# lowers what the hook holds back to the operator's own presence; its own format and its own SSHSIG namespace, so a
# leave verifies as nothing else and no answer or ruling verifies as a leave
AWAY_LEAVE_FORMAT = "levain-consent-away-leave/1"
AWAY_NAMESPACE = "levain-consent-away"
# what the queue stores when a standing consent grant answered (Q4): never signed per request; the grant's own
# link in the authority log is the signature, re-verified at every delivery
GRANT_ANSWER_FORMAT = "levain-consent-grant-answer/1"
DISPLAY_MAX = 2000
# A key is secret-named when one of these words occurs inside one of its tokens, or inside two adjacent tokens
# joined. Tokens: the key split at ``_``, ``-``, any other non-alphanumeric and camelCase (``apiKey``, ``APIKey``,
# ``x-api-key`` -> api, key), lower-cased. Inside a token, so a run-together ``accesstoken`` or ``secretkey`` is
# masked; split first, so ``author``, ``auth_url`` and a ``private`` flag are shown. ``max_tokens`` and
# ``tokenizer`` are masked too: a secret shown costs more than a count hidden.
_SECRET_WORDS = frozenset({
    "password", "passwd", "secret", "token", "apikey", "authorization", "credential", "cookie", "privatekey",
    "accesskey", "bearer", "passphrase", "pwd", "jwt"})
# exact words that are secrets only on their own (`pass`, not `passenger` or `compass`)
_SECRET_EXACT = frozenset({"pass"})
# a word that is or ends in `auth` is a credential (auth, basic_auth, x-auth, basicauth, auth_header) unless the next
# word says what kind of thing it is about rather than the thing itself (auth_url, auth_type); `author` is not one
_AUTH_ABOUT = frozenset({"url", "uri", "type", "method", "mode", "scheme", "provider", "endpoint", "server", "host",
                         "required", "enabled", "domain", "realm"})
_KEY_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")


def tool_input_digest(tool_input: Any) -> str:
    """The digest of the whole tool input the agent asked about. JCS (``cockpit.consent``) when the value is
    in its domain; a float (outside it) falls back to sorted compact JSON, under another prefix so the two
    schemes never compare equal. The fallback takes only what a JSON text could have decoded to: a mapping key
    that is not a string, or an object JSON cannot encode, raises ``TypeError`` (json would coerce ``{1: "a"}``
    to ``{"1": "a"}``, a digest of an input the agent never sent: L3 r2 codex D4)."""
    try:
        return "jcs:" + value_sha256(tool_input)
    except ConsentError:
        _string_keys_only(tool_input, set())
        text = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return "json:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _string_keys_only(value: Any, path: set[int]) -> None:
    """Raise ``TypeError`` at a dict key that is not a string, anywhere in ``value``; a cycle raises ``ValueError``
    as ``json.dumps`` does."""
    if isinstance(value, dict):
        children: Any = value.values()
        if any(not isinstance(k, str) for k in value):
            raise TypeError("a mapping key is not a string")
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        return
    if id(value) in path:
        raise ValueError("Circular reference detected")
    path.add(id(value))
    for child in children:
        _string_keys_only(child, path)
    path.discard(id(value))


def _secret_named(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    tokens = [t.lower() for t in _KEY_TOKEN.findall(key)]
    spans = tokens + [a + b for a, b in zip(tokens, tokens[1:])]
    if any((t.endswith("auth") or (t.startswith("auth") and not t.startswith(("author", "authent"))))
           and (i + 1 == len(tokens) or tokens[i + 1] not in _AUTH_ABOUT) for i, t in enumerate(tokens)):
        return True
    if any(t in _SECRET_EXACT for t in tokens):
        return True
    return any(word in span for span in spans for word in _SECRET_WORDS)


def _masked(value: Any) -> str:
    """A secret-named key's value as the operator sees it: its type, and a string's length."""
    kind = type(value).__name__
    return f"[redacted: str, {len(value)} chars]" if isinstance(value, str) else f"[redacted: {kind}]"


def _private_flagless(key: Any, value: Any) -> bool:
    """A key whose word is `private` is a flag only while its value is a bool; holding anything else, it may be
    the key itself (``{"private": "-----BEGIN ... PRIVATE KEY-----"}``)."""
    return isinstance(key, str) and "private" in (t.lower() for t in _KEY_TOKEN.findall(key)) \
        and not isinstance(value, bool)


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_masked(v) if _secret_named(k) or _private_flagless(k, v) else _mask(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v) for v in value]
    return value


def redact_display(tool_name: str, tool_input: Any) -> str:
    """What the operator is shown: the tool and its input with secret-named keys masked (a secret word inside a
    key token, each masked value shown as its type and a string's length, ``[redacted: str, 12 chars]``),
    bounded to :data:`DISPLAY_MAX` characters. A display, never what is signed: the answer binds the digest of the
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


def _need_head(receipts_head: Any) -> dict[str, Any]:
    problem = head_problem(receipts_head)
    if problem is not None:
        raise ConsentError(f"the {problem}")
    return {"size": receipts_head["size"], "root": receipts_head["root"]}


def answer_payload(*, queue_id: str, request: ExternalGateRequest, decision: str, signer_fp: str,
                   signed_at: str, receipts_head: Any) -> dict[str, Any]:
    """The record an answer signs: this queue, this request, its digest and expiry, the decision, the key, when
    it was made (``signed_at``, a :func:`~levain.cockpit.consent.utc_stamp`), and the head of the queue's receipt
    log (``receipts_head``, :func:`levain.consent.receipt_log.head`). The queue's
    :meth:`~levain.consent.queue.ConsentQueue.answer_payload` sets ``signed_at`` from its clock and the head from a
    read of its receipts; applying the answer refuses it unless that head still holds. Delivery checks
    ``signed_at`` against the expiry, never the row's ``decided_at``, which any raw writer can set."""
    if decision not in DECISIONS:
        raise ConsentError(f"decision must be one of {DECISIONS}: {decision!r}")
    parse_stamp(signed_at)
    return {"format": ANSWER_FORMAT, "queue_id": _need("queue_id", queue_id), "request_id": request.id,
            "request_digest": request.digest, "decision": decision, "expires_at": request.expires_at,
            "signer_fp": _need("signer_fp", signer_fp), "signed_at": signed_at,
            "receipts_head": _need_head(receipts_head)}


def answer_message(payload: dict[str, str]) -> bytes:
    """The exact bytes signed, in :data:`ANSWER_NAMESPACE`: the payload's JCS encoding."""
    return encode(payload)


def grant_answer_payload(*, queue_id: str, request: ExternalGateRequest, grant_id: str, grant_seq: int,
                         grant_hash: str, posture: str) -> dict[str, Any]:
    """The record the queue stores when a standing consent grant answered ``request``: this queue, the request
    and its digest, the grant, the authority link (seq and hash) that created or last renewed it, and the posture
    the agent reported. Always an allow: a grant never denies. Its JCS bytes are what the row's ``answer`` holds."""
    if type(grant_seq) is not int or grant_seq < 1:
        raise ConsentError("grant_seq must be an authority seq (an integer >= 1)")
    return {"format": GRANT_ANSWER_FORMAT, "queue_id": _need("queue_id", queue_id), "request_id": request.id,
            "request_digest": request.digest, "decision": "allow", "expires_at": request.expires_at,
            "grant_id": _need("grant_id", grant_id), "grant_seq": grant_seq,
            "grant_hash": _need("grant_hash", grant_hash), "posture": _need("posture", posture)}


def ruling_payload(*, queue_id: str, request: ExternalGateRequest, gap: int, decision: str, signer_fp: str,
                   signed_at: str, receipts_head: Any) -> dict[str, Any]:
    """The record a late ruling signs: this queue, this request and its digest, the gap number the request was
    given when it went undecided, the decision, the key, when it was made (``signed_at``, a
    :func:`~levain.cockpit.consent.utc_stamp`), and the head of the queue's receipt log (``receipts_head``), as an
    answer carries it. No expiry: a ruling is made after the request's hold ended."""
    if decision not in DECISIONS:
        raise ConsentError(f"decision must be one of {DECISIONS}: {decision!r}")
    if type(gap) is not int or gap < 1:
        raise ConsentError("gap must be a gap number (an integer >= 1)")
    parse_stamp(signed_at)
    return {"format": RULING_FORMAT, "queue_id": _need("queue_id", queue_id), "request_id": request.id,
            "request_digest": request.digest, "gap": gap, "decision": decision,
            "signer_fp": _need("signer_fp", signer_fp), "signed_at": signed_at,
            "receipts_head": _need_head(receipts_head)}


def ruling_message(payload: dict[str, Any]) -> bytes:
    """The exact bytes a ruling signs, in :data:`RULING_NAMESPACE`: the payload's JCS encoding."""
    return encode(payload)


def away_leave_payload(*, queue_id: str, mark_id: str, agent: str, signer_fp: str, signed_at: str) -> dict[str, str]:
    """The record a leave of away mode signs: this queue, the away mark it ends (set by ``levain consent away on``
    for ``agent``), the key, and when it was made (``signed_at``, a :func:`~levain.cockpit.consent.utc_stamp`)."""
    if agent not in AGENTS:
        raise ConsentError(f"agent must be one of {AGENTS}: {agent!r}")
    parse_stamp(signed_at)
    return {"format": AWAY_LEAVE_FORMAT, "queue_id": _need("queue_id", queue_id),
            "mark_id": _need("mark_id", mark_id), "agent": agent, "decision": "leave", "signer_fp": _need("signer_fp", signer_fp), "signed_at": signed_at}


def away_leave_message(payload: dict[str, str]) -> bytes:
    """The exact bytes a leave signs, in :data:`AWAY_NAMESPACE`: the payload's JCS encoding."""
    return encode(payload)
