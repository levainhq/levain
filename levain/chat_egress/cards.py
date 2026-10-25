"""The `levain_egress` consent card (CH-D2 S7; design §3.8.5(o) item 5, rows R15-10-A16, R15-23-5, R15-22-3, R15-24-2,
C9 / R15-10-A3, R15-10-A8, Q-R15-7).

A non-read the classifier cards is filed in the consent queue as one request of tool `levain_egress`. What the card
shows is what the operator approves, so every field the agent controls (method, host, path, header and query NAMES)
goes through three rules before it is filed:
  * every unshowable character (control, bidi, format, invisible: `levain.unshowable`, the one home) is shown as a
    visible `⟨U+XXXX⟩` marker, never dropped (a dropped character would make the card read as a request the origin
    is not sent) and never left for the page to decode (a JSON `\\u0007` turns back into a raw control in the page);
  * each field is bounded, and a cut says so (`…(+N chars)…`, `(+N more)`); a path keeps its head and its tail and
    adds `path_chars` and `path_sha256` (the whole path as sent), so the middle a cut hides is still bound;
  * the fields have one FIXED order, the short ones that decide the answer first (method, scheme, host, port, the key
    Levain adds, the names it removes, why, the hold) and the path, the one field an agent can make long, last. So a
    long path can never push the removed names or the key line off the card, on any surface.

Values never reach a card: no header value, no query value (R15-22-3), only names; the query names are the keys as
the request spelled them on the wire (R15-23-5). `stripped` is R-5's names as `strip.strip` returns them, which hold
no `x-levain-` name (R15-24-2).

The display the queue seals (`card_display`) is the card as one JSON object in that order, prefixed by the tool name,
so `levain.consent.listing.filed_input` reads it back whole (a display it cannot read is offered no terminal answer).
The bounds keep it under `levain.consent.record.DISPLAY_MAX`; `card_display` refuses one over it, and the worst case
is pinned by its test.

`card_state` is the one mapping from the queue's state and the request's egress rows (`records.settled`) to what the
card says it became: ENDED, AMBIGUOUS and FAILED included.

Who decides one (Q-R15-7): only a signed operator answer. `levain.consent.queue` refuses a standing grant for the
tool; away mode answers nothing anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from levain import unshowable
from levain.chat_egress import wire
from levain.chat_egress.lifecycle import EGRESS_CARD_HOLD_S, Settled

if TYPE_CHECKING:
    from levain.chat_egress.classify import Verdict
    from levain.chat_egress.parse import ParsedRequest
    from levain.consent.queue import ConsentQueue
    from levain.consent.record import ExternalGateRequest

__all__ = ["CardError", "CardState", "EGRESS_AGENT", "EGRESS_TIER", "EGRESS_TOOL", "card_display", "card_input",
           "card_state", "file_card"]

#: the queue tool a card is filed under
EGRESS_TOOL = "levain_egress"
#: the agent the chat launch runs (design item 5's `queue.file(agent="claude_code", ...)`)
EGRESS_AGENT = "claude_code"
#: The tier a card is filed at. T2, so the phone's passkey (Face ID) can allow it, as it can a push (push_gate's
#: PUSH_TIER): a card is decided by his tap. The design names EGRESS_TIER and gives no value; this is S7's.
EGRESS_TIER = "T2"

# --- bounds (characters of the MARKED text) -------------------------------------------------------------------------
METHOD_MAX = 24
HOST_MAX = 253          # a DNS name's longest spelling
WHY_MAX = 64
NAME_MAX = 30
NAMES_SHOWN = 6
LAUNCH_MAX = 40
PATH_HEAD = 100
PATH_TAIL = 60

_HOLD_TEXT = f"{EGRESS_CARD_HOLD_S} s; the agent's tool call ends at its own timeout"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MARK = "⟨U+{:04X}⟩"
#: the card's keys, in display order. `path_chars` / `path_sha256` are present only when the path was cut.
_KEYS = ("method", "scheme", "host", "port", "reattached", "stripped", "why", "hold", "body_len", "body_sha256",
         "header_names", "query_keys", "path", "path_chars", "path_sha256", "launch", "request")
_OPTIONAL = frozenset({"path_chars", "path_sha256"})
#: each key's type in a filed card (a key not named here is a string)
_KIND = {"port": "int", "body_len": "int", "path_chars": "int", "stripped": "names", "header_names": "names",
         "query_keys": "names", "reattached": "str?", "body_sha256": "str?"}
_TYPE_OK = {
    "int": lambda v: type(v) is int and v >= 0,
    "names": lambda v: type(v) is list and all(type(n) is str for n in v),
    "str?": lambda v: v is None or type(v) is str,
    "str": lambda v: type(v) is str,
}


class CardError(ValueError):
    """A card cannot be built from these inputs, or a tool input filed as `levain_egress` is not a card."""


class CardState(StrEnum):
    """What a card says it became (the cockpit draws the words; `card_state` decides which)."""
    PENDING = "pending"            # waiting on his answer
    ALLOWED = "allowed"            # he allowed it; no send is recorded yet (or the service stopped first)
    COMPLETED = "completed"        # allowed and sent: the origin answered
    DENIED = "denied"              # he denied it: nothing was sent
    EXPIRED = "expired"            # the hold ran out with no answer: a deny, nothing was sent
    ENDED = "ended"                # the request's client left: nothing was sent after it left
    AMBIGUOUS = "ambiguous"        # allowed; the service stopped before the outcome: unknown whether it was sent
    FAILED = "failed"              # an effect of Levain's failed: the rows are incomplete
    INCONSISTENT = "inconsistent"  # the queue and the rows disagree (a bug or a tampered store)


def _marked(text: str) -> str:
    return unshowable.mark(text, _MARK, allow="", selectors=False)


def _cut_at(text: str, i: int, *, forward: bool) -> int:
    """`i` moved off the inside of a `⟨U+XXXX⟩` marker: back to its start, or (`forward`) past its end."""
    start = text.rfind("⟨", 0, i)
    if start != -1 and text.find("⟩", start) >= i:
        return text.find("⟩", start) + 1 if forward else start
    return i


def _bounded(raw: str, limit: int, tail: int = 0) -> tuple[str, bool]:
    """`raw` marked, then cut to `limit` characters keeping `tail` of its end, a cut saying how much it left out."""
    text = _marked(raw)
    if len(text) <= limit:
        return text, False
    budget = limit - tail - len(f"…(+{len(text)} chars)…")    # the count left out is at most the text's length
    head_end = _cut_at(text, max(budget, 1), forward=False)
    tail_start = _cut_at(text, len(text) - tail, forward=True) if tail else len(text)
    left = tail_start - head_end
    return f"{text[:head_end]}…(+{left} chars)…{text[tail_start:]}", True


def _names(names: Sequence[str]) -> list[str]:
    """Each name once, marked and bounded, at most NAMES_SHOWN, then one `(+N more)` entry."""
    seen: list[str] = []
    known: set[str] = set()
    for n in names:
        if not isinstance(n, str):
            raise CardError(f"a name is not a string: {n!r}")
        if n not in known:
            known.add(n)
            seen.append(n)
    shown = [_bounded(n, NAME_MAX)[0] for n in seen[:NAMES_SHOWN]]
    if len(seen) > NAMES_SHOWN:
        shown.append(f"(+{len(seen) - NAMES_SHOWN} more)")
    return shown


def card_input(parsed: "ParsedRequest", verdict: "Verdict", stripped: Sequence[str], *, body_len: int,
               body_sha256: str | None, launch_id: str) -> dict[str, Any]:
    """The card for one carded request: the `tool_input` the queue files and digests (an answer binds that digest).

    `parsed` is the request's one parse, `verdict` the classifier's card verdict for it (its `method` is the
    effective one, its `reattached` the key line), `stripped` R-5's removed names (`strip.strip(parsed).stripped`),
    `body_len` / `body_sha256` the spooled body's decoded length and lower-hex SHA-256 (None with `body_len` 0 when
    the request has no body), `launch_id` the chat launch. Raises `CardError` when the verdict is not a card, the
    request has no wrapper request id, or the body pair is malformed."""
    if verdict.kind != "card" or not verdict.why:
        raise CardError(f"only a card verdict files a card: {verdict.kind!r}")
    if not wire.is_request_id(parsed.request_id):
        raise CardError("a card needs the wrapper's request id")
    if not isinstance(launch_id, str) or not launch_id:
        raise CardError("a card needs its launch id")
    if type(body_len) is not int or body_len < 0:
        raise CardError(f"body_len must be a non-negative int: {body_len!r}")
    if body_sha256 is None:
        if body_len:
            raise CardError("a body of non-zero length needs its sha256")
    elif not (isinstance(body_sha256, str) and _SHA256.fullmatch(body_sha256)):
        raise CardError("body_sha256 is 64 lower-case hex characters")
    path, cut = _bounded(parsed.path, PATH_HEAD + PATH_TAIL, tail=PATH_TAIL)
    card: dict[str, Any] = {
        "method": _bounded(verdict.method, METHOD_MAX)[0],
        "scheme": parsed.scheme,
        "host": _bounded(parsed.canonical_host or parsed.host, HOST_MAX)[0],
        "port": parsed.port,
        "reattached": None if verdict.reattached is None else _bounded(verdict.reattached, HOST_MAX)[0],
        "stripped": _names(stripped),
        "why": _bounded(verdict.why, WHY_MAX)[0],
        "hold": _HOLD_TEXT,
        "body_len": body_len,
        "body_sha256": None if body_sha256 is None else "sha256:" + body_sha256,
        "header_names": _names(verdict.card_header_names),
        "query_keys": _names([q.raw_key for q in parsed.params]),
        "path": path,
    }
    if cut:
        card["path_chars"] = len(parsed.path)
        card["path_sha256"] = "sha256:" + hashlib.sha256(parsed.path.encode("utf-8", "surrogatepass")).hexdigest()
    card["launch"] = _bounded(launch_id, LAUNCH_MAX)[0]
    card["request"] = parsed.request_id
    return card


def _check(tool_input: Any) -> dict[str, Any]:
    """`tool_input` if it is a card as `card_input` makes one, else CardError."""
    if not isinstance(tool_input, dict):
        raise CardError("a levain_egress tool input is a card (a mapping)")
    keys = [k for k in _KEYS if k in tool_input]
    if list(tool_input) != keys or not set(_KEYS) - _OPTIONAL <= set(keys) or \
            ("path_chars" in tool_input) != ("path_sha256" in tool_input):
        raise CardError(f"not a card's keys, in a card's order: {list(tool_input)!r}")
    for k, v in tool_input.items():
        if not _TYPE_OK[_KIND.get(k, "str")](v):
            raise CardError(f"card field {k} has the wrong type")
        for s in (v if isinstance(v, list) else [v] if isinstance(v, str) else []):
            if unshowable.first_hidden(s, selectors=False) is not None:
                raise CardError(f"card field {k} holds an unshowable character: mark it with card_input")
    return tool_input


def card_display(tool_input: Any) -> str:
    """What the queue seals as the request's display: `levain_egress <the card as JSON, in its order>`. Raises
    CardError for anything that is not a card, or a card whose display would pass the queue's display limit."""
    from levain.consent.record import DISPLAY_MAX

    text = f"{EGRESS_TOOL} {json.dumps(_check(tool_input), ensure_ascii=False, separators=(', ', ': '))}"
    if len(text) > DISPLAY_MAX:
        raise CardError(f"a card's display is {len(text)} characters, over the queue's {DISPLAY_MAX}")
    return text


def file_card(queue: "ConsentQueue", card: dict[str, Any], *, agent_session_id: str,
              cwd: str) -> "ExternalGateRequest":
    """File `card` (`card_input`'s) in `queue`: the agent, tool, tier, hold (EGRESS_CARD_HOLD_S) and event id
    (`chat:egress:<request id>`, so one request files one card) bound here. Raises CardError (not a card) or the
    queue's ConsentError (a second filing of the same request, a store refusal)."""
    _check(card)
    return queue.file(agent=EGRESS_AGENT, agent_session_id=agent_session_id, tool_name=EGRESS_TOOL,
                      tool_input=card, cwd=cwd, hold=timedelta(seconds=EGRESS_CARD_HOLD_S), tier=EGRESS_TIER,
                      event_id="chat:egress:" + card["request"])




#: the card state a queue state reads as when the request has no egress row (`settled` None)
_QUEUE_ONLY = {"pending": CardState.PENDING, "allowed": CardState.ALLOWED, "denied": CardState.DENIED,
               "expired": CardState.EXPIRED, "withdrawn": CardState.ENDED}
#: Settled.DENIED (the decider's deny verdict) per queue state. A deny verdict on a card is his deny or the hold's
#: expiry; a pending or allowed queue row with a deny verdict is the hold running out before the answer (or the
#: queue's sweep) reached the request: nothing was sent. A withdrawn card never gets a deny verdict (the composer
#: withdraws only at C0's CARD_ENDED, which writes an UNDECIDED attempt).
_DENY_VERDICT = {"denied": CardState.DENIED, "expired": CardState.EXPIRED, "pending": CardState.EXPIRED,
                 "allowed": CardState.EXPIRED}


def card_state(queue_state: str, settled: Settled | None) -> CardState:
    """What the card says the request became, from the queue row's state (`levain.consent.listing`'s display state)
    and the request's egress rows (`records.EgressRecords.settled`, None when it has none).

    The rows prove what was sent, so they decide whenever there are any: FAILED and INCONSISTENT as they read;
    ENDED and UNDECIDED (the client left, or the launch closed, before anything was forwarded after it) are ENDED
    on any queue state, because his answer and the client's close race; COMPLETED and AMBIGUOUS need an allowed
    row (only his allow forwards a card's request), else INCONSISTENT; a deny verdict as `_DENY_VERDICT`. An egress
    card never goes `undecided` (its hold's expiry is a deny), so that state reads INCONSISTENT. Raises ValueError
    for a state the queue does not have, or a `settled` that is not a `Settled`."""
    if queue_state not in _QUEUE_ONLY and queue_state != "undecided":
        raise ValueError(f"not a consent queue state: {queue_state!r}")
    if settled is not None and type(settled) is not Settled:
        raise ValueError(f"not a settled reading: {settled!r}")
    if queue_state == "undecided":
        return CardState.INCONSISTENT
    if settled is None:
        return _QUEUE_ONLY[queue_state]
    if settled in (Settled.FAILED, Settled.INCONSISTENT):
        return CardState(settled.value)
    if settled in (Settled.ENDED, Settled.UNDECIDED):
        return CardState.ENDED
    if settled in (Settled.COMPLETED, Settled.AMBIGUOUS):
        return CardState(settled.value) if queue_state == "allowed" else CardState.INCONSISTENT
    return _DENY_VERDICT.get(queue_state, CardState.INCONSISTENT)
