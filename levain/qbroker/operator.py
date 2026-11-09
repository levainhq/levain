"""levain.qbroker.operator — the operator's side of the broker's queue, for the cockpit and phone writers.

Reads ``L/queue`` without writing to it (the queue id and the receipts log must already be there: the broker makes
them) and writes only ``answers/<request_id>.json``:

- :func:`rows`: every ``pend-*.json`` request row whose seal holds (any other name, a ``grant-*.json`` among them,
  is skipped). A row is labelled by its ``session`` / ``slot`` / ``kind`` / ``run``, which the broker took from
  CONTROL; the request's ``agent``, ``agent_session_id``, ``cwd`` and ``display`` are the hook's own claims and are
  returned under ``claims``, never as identity. A row that is not whole by
  :func:`levain.qbroker.broker.stored_request` (its broker labels not the shape the broker writes among them) is
  skipped here and refused by :func:`answer_payload` and :func:`write_answer`. Its state is the last one the
  broker's receipts record for it (a decision the broker accepted reads settled), ``expired`` once the hold has ended with none, and ``filing`` while it has no receipt at all: such a row
  is not answerable (the broker records no decision without the filing). A request the broker granted under its
  run's signed contract reads ``allowed`` from its grant's own receipt (head ruling 1020+49): settled, never offered
  as a question, and refused by :func:`answer_payload` and :func:`write_answer` like any settled request.
- :func:`answer_payload`: the record to sign, over the broker's current receipts head (never one of size 0), for a
  ``pending`` request only.
- :func:`write_answer`: the signed answer row, for a ``pending`` request only, checked first with
  :func:`levain.qbroker.seam.answer_refusal` and the head against the receipts as they stand (reaching the request's
  filing receipt), then linked no-replace, mode 0640: an answer that fails any check is never written, since nothing
  can replace it once it is there. Any ``levain-queue`` member can create a file at an answer's name, so a file
  already there that is not a signed answer for that request is
  renamed aside to ``answers/.quarantine-<request_id>-<random>.json``, a name no reader takes for an answer, and the
  signed answer is linked in its place. One signed by a key in the current signer snapshot (over a head the receipts
  hold) is never moved or replaced; one signed by a key no longer in that snapshot is not a valid answer (the broker
  refuses it too), so it is cleared like any other file.

A receipts log that cannot be read is :class:`OperatorError`.

Stdlib only.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent import record
from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.qbroker.broker import (RUN_CALLS_KIND, RUN_DEADLINE_KIND, RUN_STATE_FORMAT, _calls_ok, _deadline_ok,
                                   answer_row_refusal, run_state_path, stored_request)
from levain.qbroker.receipts import Receipts, ReceiptsError
from levain.qbroker.seam import answer_refusal
from levain.qbroker.signers import SignersSnapshot
from levain.qbroker.store import (ANSWER_ROW_FORMAT, ROW_MAX, QueueStore, _parsed, _read_untrusted, fsync_dir,
                                  valid_request_id)

__all__ = ["CLAIM_KEYS", "Listed", "OperatorError", "answer_payload", "rows", "run_state", "write_answer"]

# the request fields only the hook vouches for
CLAIM_KEYS = ("agent", "agent_session_id", "cwd", "display")
# the keys of a published run state (tests/test_qbroker_run_caps.py reads GATE's own through run_state)
_RUN_STATE_KEYS = frozenset({"format", "run_id", "calls", "cap", "deadline", "tripped"})
_SETTLED = frozenset({"allowed", "denied", "expired"})
# how many times write_answer clears a squatted answer name before it gives up: another member recreating the file
# between the move and the link each time is a fight the operator's writer does not loop on forever
_SQUAT_TRIES = 4


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


def _pending(receipts: Receipts, request: record.ExternalGateRequest, stamp: str) -> None:
    state = _state(_recorded(receipts, request.id), request.expires_at, stamp)
    if state == "filing":
        raise OperatorError(f"request {request.id} has no filing receipt yet: the broker records no answer for it")
    if state != "pending":
        raise OperatorError(f"request {request.id} is already {state}")


def rows(root: Path | str, *, signers: SignersSnapshot | None, now: datetime | None = None,
         ssh_keygen: Path | str | None = None) -> list[Listed]:
    """Every request row in the broker's queue at ``root`` (``L/queue``), oldest first.

    ``answered`` is True only for an answer row that verifies for its request against ``signers`` (the check the
    writer and the broker make, :func:`~levain.qbroker.broker.answer_row_refusal`) over a head the receipts hold
    now, reaching the request's filing (:meth:`~levain.qbroker.receipts.Receipts.head_refusal`, as the broker's
    ``Receipts.decided`` requires): any ``levain-queue`` member can
    create a file at an answer's name, so a file being there says nothing. ``signers`` has no default, so no caller
    gets that by omission: ``None`` (no snapshot) verifies nothing, and every row lists as unanswered."""
    store, receipts = _open(root)
    recorded = _recorded(receipts)
    when = _now(now)
    stamp = utc_stamp(when)
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
        request = stored_request(row, store.queue_id, rid)
        if request is None:
            continue
        out.append(Listed(
            request_id=rid, session=row["session"], slot=dict(row["slot"]), kind=row.get("kind"),
            run=None if row.get("run") is None else dict(row["run"]), tier=request.tier, tool_name=request.tool_name,
            received_at=request.received_at, expires_at=request.expires_at,
            state=_state(recorded.get(rid), request.expires_at, stamp),
            answered=signers is not None and store.has_answer(rid)
            and _signed_answer(row, store.answers / f"{rid}.json", signers, when, ssh_keygen, receipts, rid),
            claims={k: getattr(request, k) for k in CLAIM_KEYS}))
    return sorted(out, key=lambda r: (r.received_at, r.request_id))


def run_state(root: Path | str, run_id: str, *, now: float | None = None) -> dict[str, Any]:
    """``RUN-STATE {run_id}`` (P7-A3, read-only): ``{calls, cap, tripped, kind, known}`` for one run, read from the
    state GATE published beside the request rows (the operator cannot open the broker's own counter directory).
    ``tripped`` is True once the run's call cap latched or its deadline passed (``kind`` names which). ``known`` is
    False for a run GATE has not yet judged: that reads ``tripped`` False and is NOT evidence of health, only of no
    call so far. A queue that cannot be opened, or a state file that is there and unreadable, not this run's, or not
    one GATE could have written, is :class:`OperatorError` (fail closed), never a quiet zero."""
    if not isinstance(run_id, str) or not run_id:
        raise OperatorError("RUN-STATE wants a run id")
    try:
        store = QueueStore(root, create=False)
        path = run_state_path(store.requests, run_id)
        data = _read_untrusted(path)
        doc = None if data is None else _parsed(data)
    except (ValueError, OSError) as exc:
        raise OperatorError(f"the state of run {run_id} cannot be read ({exc})") from None
    if doc is None:
        return {"calls": 0, "cap": None, "tripped": False, "kind": None, "known": False}
    calls, cap, deadline, kind = doc.get("calls"), doc.get("cap"), doc.get("deadline"), doc.get("tripped")
    # the broker's own range checks: a state GATE publishes holds the caps it resolved, and a kind only for a cap the
    # run has
    if set(doc) != _RUN_STATE_KEYS or doc["format"] != RUN_STATE_FORMAT or doc["run_id"] != run_id \
            or type(calls) is not int or calls < 0 \
            or (cap is None and deadline is None) \
            or (cap is not None and not _calls_ok(cap)) \
            or (deadline is not None and not _deadline_ok(deadline)) \
            or kind not in (None, RUN_CALLS_KIND, RUN_DEADLINE_KIND) \
            or (kind == RUN_CALLS_KIND and cap is None) \
            or (kind == RUN_DEADLINE_KIND and deadline is None):
        raise OperatorError(f"the state of run {run_id} is not a run state")
    if kind is None and deadline is not None and (time.time() if now is None else now) >= deadline:
        kind = RUN_DEADLINE_KIND
    return {"calls": calls, "cap": cap, "tripped": kind is not None, "kind": kind, "known": True}


def _request(store: QueueStore, request_id: str) -> tuple[dict[str, Any], record.ExternalGateRequest]:
    if not valid_request_id(request_id):
        raise OperatorError(f"{request_id!r} is not a request id")
    try:
        row = store.read_request(request_id)
    except (ValueError, OSError) as exc:
        raise OperatorError(f"the row for {request_id} cannot be read ({exc})") from None
    request = stored_request(row, store.queue_id, request_id)
    if request is None:
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
    now = _now(now)
    for _ in range(_SQUAT_TRIES):
        try:
            store._link_new(path, data)
            return path
        except FileExistsError:
            pass
        if _clear_squat(store, receipts, row, request_id, path, signers, now, ssh_keygen):
            raise OperatorError(f"request {request_id} already has an answer row")
    raise OperatorError(f"request {request_id}'s answer name was taken again each time it was cleared")


def _signed_answer(row: Mapping[str, Any], path: Path, signers: SignersSnapshot | None, now: datetime,
                   ssh_keygen: Path | str | None, receipts: Receipts, request_id: str) -> bool:
    """Whether ``path`` holds a signed answer for ``request_id`` (whose request row is ``row``) that the broker could
    accept: it verifies, and its ``receipts_head`` is a head of ``receipts`` now reaching the request's filing (an
    answer over another branch's head is refused by ``Receipts.decided``). False for a symlink, a non-regular file,
    an unreadable one, or none."""
    try:
        got = _read_untrusted(path)
    except (ValueError, OSError):
        return False
    if got is None:
        return False
    try:
        answer = _parsed(got)
    except ValueError:
        return False
    if answer_row_refusal(row, answer, signers, now, ssh_keygen=ssh_keygen) is not None:
        return False
    return receipts.head_refusal(answer["payload"]["receipts_head"], request_id) is None


def _clear_squat(store: QueueStore, receipts: Receipts, row: Mapping[str, Any], request_id: str, path: Path,
                 signers: SignersSnapshot | None, now: datetime, ssh_keygen: Path | str | None) -> bool:
    """True when ``path`` holds an answer signed by a key in the current snapshot ``signers`` (left in place); an
    answer signed by a key no longer in it is no valid answer (the broker refuses it too) and is cleared. Otherwise
    whatever is there is renamed aside and False is returned, for the caller to link its own answer again. What the
    rename moved is judged again from its own fd, never assumed to be the thing judged at the name (a swap in
    between, or a freed inode reused, makes them differ while the name or the (dev, ino) look the same); should it
    be a signed answer, it is linked back and True is returned (a verified answer is in place).
    Should the link-back fail (the name taken again, or any other ``OSError`` but the quarantine file gone),
    :class:`OperatorError` names the quarantine file that holds it. An answer signed by a key in the current
    snapshot is never left in quarantine unreported: an ``OSError`` syncing the directory after the rename is held
    until ``aside`` is judged (a signed answer is linked back regardless) and then names ``aside``.

    ``answers/`` has no sticky bit, so the member that squatted can delete or rename the quarantine file once it
    exists: a quarantine file gone is a name cleared, never an error. Any other ``OSError`` is :class:`OperatorError`,
    the one failure the cockpit and phone writers handle."""
    try:
        return _clear_squat_os(store, receipts, row, request_id, path, signers, now, ssh_keygen)
    except OSError as exc:
        raise OperatorError(f"request {request_id}'s answer name could not be cleared ({exc})") from None


def _clear_squat_os(store: QueueStore, receipts: Receipts, row: Mapping[str, Any], request_id: str, path: Path,
                    signers: SignersSnapshot | None, now: datetime, ssh_keygen: Path | str | None) -> bool:
    if _signed_answer(row, path, signers, now, ssh_keygen, receipts, request_id):
        return True
    aside = store.answers / f".quarantine-{request_id}-{os.urandom(8).hex()}.json"
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return False
    try:
        fsync_dir(store.answers)
    except OSError as exc:                       # the rename happened: judge ``aside`` first, never lose its path
        unsynced: OSError | None = exc
    else:
        unsynced = None
    if _signed_answer(row, aside, signers, now, ssh_keygen, receipts, request_id):
        try:
            os.link(aside, path, follow_symlinks=False)
        except FileExistsError:
            raise OperatorError(f"request {request_id}'s answer name was taken again before the signed answer moved "
                                f"aside could be linked back: it is at {aside}") from None
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OperatorError(f"request {request_id}'s signed answer moved aside could not be linked back ({exc}): "
                                f"it is at {aside}") from None
        try:
            os.unlink(aside)
        except FileNotFoundError:
            pass
        fsync_dir(store.answers)
        return True                              # a verified answer is in place: the caller writes none of its own
    if unsynced is not None:
        raise OperatorError(f"request {request_id}'s answer name could not be cleared ({unsynced}): "
                            f"what was moved aside is at {aside}") from None
    return False
