"""levain.qbroker.operator — the operator's side of the broker's queue, for the cockpit and phone writers.

Reads ``L/queue`` without writing to it (the queue id and the receipts log must already be there: the broker makes
them) and writes only ``answers/<request_id>.json``:

- :func:`rows`: every ``pend-*.json`` request row whose seal holds (any other name, a ``grant-*.json`` among them,
  is skipped). A row is labelled by its ``session`` / ``slot`` / ``kind`` / ``run``, which the broker took from
  CONTROL; the request's ``agent``, ``agent_session_id``, ``cwd`` and ``display`` are the hook's own claims and are
  returned under ``claims``, never as identity. A row whose broker labels are not the shape the broker writes is
  skipped. Its state is the last one the broker's receipts record for it (a decision the broker accepted reads
  settled), ``expired`` once the hold has ended with none, and ``filing`` while it has no receipt at all: such a row
  is not answerable (the broker records no decision without the filing).
- :func:`answer_payload`: the record to sign, over the broker's current receipts head (never one of size 0), for a
  ``pending`` request only.
- :func:`write_answer`: the signed answer row, for a ``pending`` request only, checked first with
  :func:`levain.qbroker.seam.answer_refusal` and the head against the receipts as they stand (reaching the request's
  filing receipt), then linked no-replace, mode 0640: an answer that fails any check is never written, since nothing
  can replace it once it is there.

A receipts log that cannot be read is :class:`OperatorError`.

Stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent import record
from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.qbroker.broker import SLOT_CLASS, resolved_from
from levain.qbroker.receipts import Receipts, ReceiptsError
from levain.qbroker.seam import answer_refusal, request_of
from levain.qbroker.signers import SignersSnapshot
from levain.qbroker.store import ANSWER_ROW_FORMAT, ROW_MAX, QueueStore, valid_request_id

__all__ = ["CLAIM_KEYS", "Listed", "OperatorError", "answer_payload", "rows", "write_answer"]

# the request fields only the hook vouches for
CLAIM_KEYS = ("agent", "agent_session_id", "cwd", "display")
_SETTLED = frozenset({"allowed", "denied", "expired"})


class OperatorError(Exception):
    """The operator's action on the broker's queue is refused; the message says why."""


@dataclass(frozen=True)
class Listed:
    request_id: str
    session: str
    slot: Mapping[str, Any]
    kind: str
    run: Mapping[str, Any] | None
    tier: str
    tool_name: str
    received_at: str
    expires_at: str
    state: str
    answered: bool
    claims: Mapping[str, str] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.state in _SETTLED


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _open(root: Path | str) -> tuple[QueueStore, Receipts]:
    return QueueStore(root, create=False), Receipts(root, create=False)


def _state(recorded: str | None, expires_at: str, stamp: str) -> str:
    if recorded is None:
        return "filing"
    return "expired" if recorded == "pending" and expires_at <= stamp else recorded


def _recorded(receipts: Receipts, request_id: str | None = None) -> Any:
    try:
        return receipts.states() if request_id is None else receipts.state(request_id)
    except ReceiptsError as exc:
        raise OperatorError(str(exc)) from None


def _labelled(row: Mapping[str, Any]) -> bool:
    """Whether the row's broker labels are the shape the broker writes (``slot`` a ``{class, index}``)."""
    slot = row.get("slot")
    if not isinstance(slot, Mapping) or set(slot) != {"class", "index"} or slot["class"] != SLOT_CLASS \
            or type(slot["index"]) is not int:
        return False
    return resolved_from({"session": row.get("session"), "kind": row.get("kind"), "run": row.get("run")}) is not None


def _pending(receipts: Receipts, request: record.ExternalGateRequest, stamp: str) -> None:
    state = _state(_recorded(receipts, request.id), request.expires_at, stamp)
    if state == "filing":
        raise OperatorError(f"request {request.id} has no filing receipt yet: the broker records no answer for it")
    if state != "pending":
        raise OperatorError(f"request {request.id} is already {state}")


def rows(root: Path | str, *, now: datetime | None = None) -> list[Listed]:
    """Every request row in the broker's queue at ``root`` (``L/queue``), oldest first."""
    store, receipts = _open(root)
    recorded = _recorded(receipts)
    stamp = utc_stamp(_now(now))
    out = []
    for path in store.requests.iterdir():
        name = path.name
        if not (name.startswith("pend-") and name.endswith(".json")) or not valid_request_id(name[:-len(".json")]):
            continue
        rid = name[:-len(".json")]
        try:
            row = store.read_request(rid)
        except (ValueError, OSError):
            continue
        request = None if row is None else request_of(row.get("request"))
        if request is None or request.id != rid or row.get("queue_id") != store.queue_id or not _labelled(row):
            continue
        out.append(Listed(
            request_id=rid, session=row["session"], slot=dict(row["slot"]), kind=row.get("kind"),
            run=None if row.get("run") is None else dict(row["run"]), tier=request.tier, tool_name=request.tool_name,
            received_at=request.received_at, expires_at=request.expires_at,
            state=_state(recorded.get(rid), request.expires_at, stamp), answered=store.has_answer(rid),
            claims={k: getattr(request, k) for k in CLAIM_KEYS}))
    return sorted(out, key=lambda r: (r.received_at, r.request_id))


def _request(store: QueueStore, request_id: str) -> tuple[dict[str, Any], record.ExternalGateRequest]:
    if not valid_request_id(request_id):
        raise OperatorError(f"{request_id!r} is not a request id")
    try:
        row = store.read_request(request_id)
    except (ValueError, OSError) as exc:
        raise OperatorError(f"the row for {request_id} cannot be read ({exc})") from None
    request = None if row is None else request_of(row.get("request"))
    if request is None or row.get("queue_id") != store.queue_id:
        raise OperatorError(f"no sealed request {request_id!r} in this queue")
    return row, request


def answer_payload(root: Path | str, request_id: str, decision: str, signer_fp: str, *,
                   now: datetime | None = None) -> dict[str, Any]:
    """The record to sign to answer ``request_id``: ``signed_at`` now, ``receipts_head`` the broker's current head."""
    store, receipts = _open(root)
    _, request = _request(store, request_id)
    stamp = utc_stamp(_now(now))
    _pending(receipts, request, stamp)
    try:
        head = receipts.head()
    except ReceiptsError as exc:
        raise OperatorError(str(exc)) from None
    if head["size"] == 0:
        raise OperatorError("the broker's receipts log is empty: a head of size 0 attests nothing")
    try:
        return record.answer_payload(queue_id=store.queue_id, request=request, decision=decision, signer_fp=signer_fp,
                                     signed_at=stamp, receipts_head=head)
    except ConsentError as exc:
        raise OperatorError(str(exc)) from None


def write_answer(root: Path | str, request_id: str, payload: Mapping[str, Any], signature: str,
                 signers: SignersSnapshot | None, *, now: datetime | None = None,
                 ssh_keygen: Path | str | None = None) -> Path:
    """Write the signed answer row for ``request_id`` once the broker would accept it now; its path."""
    store, receipts = _open(root)
    row, request = _request(store, request_id)
    _pending(receipts, request, utc_stamp(_now(now)))
    try:
        payload = parse(canonical_bytes(dict(payload)))   # one detached copy: checked and written alike
    except (ContractError, TypeError, ValueError) as exc:
        raise OperatorError(f"the answer is not a record ({exc})") from None
    why = answer_refusal(row, payload, signature, signers, _now(now), ssh_keygen=ssh_keygen)
    if why is None:
        why = receipts.head_refusal(payload["receipts_head"], request_id)
    if why is not None:
        raise OperatorError(why)
    data = canonical_bytes({"format": ANSWER_ROW_FORMAT, "payload": payload, "signature": signature})
    if len(data) > ROW_MAX:
        raise OperatorError(f"the answer row is over {ROW_MAX} bytes")
    path = store.answers / f"{request_id}.json"
    try:
        store._link_new(path, data)
    except FileExistsError:
        raise OperatorError(f"request {request_id} already has an answer row") from None
    return path
