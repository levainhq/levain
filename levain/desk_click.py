"""levain.desk_click: a click in the cockpit served AT THE LAPTOP, signed with the laptop's Touch ID key (TERMSIGN L5a;
design project_memory/1103-19/L5_DESIGN.md sections 1-3 and 7).

This module is the route's whole logic as one pure function, :func:`desk_answer`, over an injected request and an
injected host; ``web_server`` only turns an HTTP request into a :class:`DeskRequest` and the result into a reply. The
signer, the notice and the key lookup are injected too, so nothing here reaches a Secure Enclave key unless the caller
passes the real factory (:func:`levain.consent.terminal_sign.default_desk_signer_factory`, the default).

The page chooses only WHICH row, at WHICH version, with WHICH option (or its words): the server re-reads the row,
refuses a version that moved before any prompt, resolves the letter to the option's text from the row it read and builds
the bytes (:func:`levain.consent.terminal_sign.build_desk_message`). Which rows raise a Touch ID sheet is the policy
switch :data:`DESK_POLICY` (NP-A, ruled (b) 11-03): the signature is the act on an authority item, so those sign;
an information click stays on the unsigned ``cockpit`` path.
"""
from __future__ import annotations

import collections
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["DESK_POLICY", "DeskHost", "DeskRequest", "DeskRow", "DeskState", "LevainDeskHost", "desk_answer",
           "desk_sign_available", "requires_sign"]

DESK_POLICY = "authority"          # NP-A (b): "authority" signs authority items; "all" (a) signs every desk answer
POLICIES = ("authority", "all")
_TXN = re.compile(r"[A-Za-z0-9._-]{8,64}")
_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TXN_KEEP = 64                     # finished requests remembered for replay


@dataclass(frozen=True)
class DeskRow:
    """One row as the host reads it now. ``options`` are ``(display letter, full text)``. ``recordable`` False: the
    host cannot record a desk answer for this row (so no prompt is raised for it)."""
    id: str
    version: str
    what: str
    kind: str                      # "information" | "authority"
    options: tuple[tuple[str, str], ...]
    answered: bool = False
    recordable: bool = True


class DeskHost(Protocol):
    def scope(self) -> tuple[str, str] | None:
        """``(store, install)`` as the message names them, or None when this host cannot take a desk answer."""

    def row(self, subject_id: str) -> DeskRow | None: ...

    def record(self, row: DeskRow, pick: str | None, text: str | None, viewer: str, signing: Any) -> Any:
        """Record the signed answer, checking the row's version again inside the write. Raises
        :class:`levain.band_view.VersionMoved` when it moved (nothing recorded)."""


@dataclass(frozen=True)
class DeskRequest:
    local: bool                    # routing only: False for any request that came through a proxy or off the box
    surface: str                   # the cockpit credential's label, or "web"
    origin: str                    # the page origin the server is pinned to
    body: Any                      # the parsed JSON body


class DeskState:
    """The process-wide state of the route: one non-blocking sign lock (one Touch ID sheet at a time) and the finished
    requests, so a replayed ``client_txn`` returns its first result and raises no second prompt."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._done: collections.OrderedDict[str, tuple[dict[str, Any], int]] = collections.OrderedDict()
        self._guard = threading.Lock()

    def finished(self, txn: str) -> tuple[dict[str, Any], int] | None:
        with self._guard:
            return self._done.get(txn)

    def remember(self, txn: str, result: tuple[dict[str, Any], int]) -> None:
        with self._guard:
            self._done[txn] = result
            while len(self._done) > _TXN_KEEP:
                self._done.popitem(last=False)


def requires_sign(kind: str, policy: str = DESK_POLICY) -> bool:
    """Whether a desk answer on a row of ``kind`` raises a sign. Anything the policy does not name signs: an unknown
    policy or kind fails toward the signature, never toward an unsigned write."""
    if policy == "authority":
        return kind != "information"
    return True


def _refuse(error: str, message: str, status: int, **extra: Any) -> tuple[dict[str, Any], int]:
    return {"error": error, "message": message, **extra}, status


def _read_click(body: Any) -> tuple[dict[str, str], str | None, str | None, str] | tuple[None, str]:
    """``(subject, pick, text, client_txn)`` of a click body, else ``(None, why)``. The body is exactly
    ``{subject: {id, version}, pick | text, client_txn}``: nothing the page could use to name bytes."""
    if not isinstance(body, Mapping):
        return None, "the body is not an object"
    keys = set(body)
    subject = body.get("subject")
    if keys not in ({"subject", "pick", "client_txn"}, {"subject", "text", "client_txn"}):
        return None, "answer with exactly {subject, pick or text, client_txn}"
    if not isinstance(subject, Mapping) or set(subject) != {"id", "version"} \
            or not all(isinstance(subject[k], str) and subject[k] for k in ("id", "version")):
        return None, "subject is exactly {id, version}"
    txn = body["client_txn"]
    if not isinstance(txn, str) or _TXN.fullmatch(txn) is None:
        return None, "client_txn is 8 to 64 letters, digits, dots, dashes or underscores"
    pick, text = body.get("pick"), body.get("text")
    if "pick" in body and (not isinstance(pick, str) or len(pick) != 1):
        return None, "pick is one displayed letter"
    if "text" in body and (not isinstance(text, str) or not text.strip() or len(text) > 2000):
        return None, "text is his words, 1 to 2000 characters"
    return {"id": subject["id"], "version": subject["version"]}, pick, text, txn


def desk_sign_available(local: bool, *, key_dir: Any = None, find_key: Callable[..., Any] | None = None) -> bool:
    """What ``band.json`` reports as ``desk_sign``: this request is local AND a Touch ID key is enrolled. Never
    raises (a key directory that cannot be read is False)."""
    if not local:
        return False
    try:
        from levain.consent import terminal_sign as ts
        return (find_key or ts.find_key)(key_dir) is not None
    except Exception:  # noqa: BLE001 - a display flag, never a crash of the band read
        return False


def desk_answer(req: DeskRequest, host: DeskHost, state: DeskState, *, policy: str = DESK_POLICY,
                signer_factory: Any = None, key_dir: Any = None, notify: Callable[[str], Any] | None = None,
                deliver: Callable[[Any], None] | None = None, find_key: Callable[..., Any] | None = None,
                at: Any = None, nonce: str | None = None) -> tuple[dict[str, Any], int]:
    """``POST /cockpit/desk/answer``: the reply body and HTTP status. Steps (design section 7):

    1. a request that is not local is 403 ``desk_remote`` and nothing below runs, the signer factory never called;
    2. the body is read strictly (400 ``bad_request``); a replayed ``client_txn`` returns its first result;
    3. the row is re-read: gone, answered or a moved version is 409 ``stale`` with NO prompt; a pick the row does not
       offer is 400; a row the policy does not sign is 422 ``desk_unsigned`` (the page uses the unsigned path); a row
       the host cannot record is 422 ``desk_authority_pending``;
    4. no enrolled key is 409 ``no_key`` (the page falls back to the unsigned path);
    5. one sign at a time: a held lock is 409 ``sign_busy``;
    6. the bytes are built from the row as read, the notice posted, the key asked; cancel, timeout or any unsigned
       outcome records NOTHING and is 409 ``unsigned`` with the reason; the card stays open;
    7. signed: recorded with the signing columns, the version checked again inside the write (moved: 409 ``stale``,
       nothing recorded, the signature discarded), then delivery on a daemon thread.

    ``at`` and ``nonce`` pin the bytes in tests. Never raises."""
    try:
        return _desk_answer(req, host, state, policy, signer_factory, key_dir, notify, deliver, find_key, at, nonce)
    except Exception as exc:  # noqa: BLE001 - a route answers, it never leaks a traceback
        return _refuse("internal", type(exc).__name__, 500)


def _desk_answer(req: DeskRequest, host: DeskHost, state: DeskState, policy: str, signer_factory: Any, key_dir: Any,
                 notify: Callable[[str], Any] | None, deliver: Callable[[Any], None] | None,
                 find_key: Callable[..., Any] | None, at: Any, nonce: str | None) -> tuple[dict[str, Any], int]:
    from levain import band_view
    from levain.cockpit.keys import KeyError_
    from levain.consent import terminal_sign as ts

    if not req.local:
        return _refuse("desk_remote", "a click that did not come from the laptop's own cockpit never asks for Touch ID", 403)
    if policy not in POLICIES:
        return _refuse("internal", "unknown desk policy", 500)
    got = _read_click(req.body)
    if got[0] is None:
        return _refuse("bad_request", str(got[1]), 400)
    subject, pick, text, txn = got
    first = state.finished(txn)
    if first is not None:
        return first
    surface = req.surface if isinstance(req.surface, str) and _ID.fullmatch(req.surface) else "web"

    def finish(result: tuple[dict[str, Any], int]) -> tuple[dict[str, Any], int]:
        state.remember(txn, result)
        return result

    scope = host.scope()
    if scope is None:
        return _refuse("desk_unsupported", "this store takes no desk answer", 422)
    row = host.row(subject["id"])
    if row is None or row.answered or row.id != subject["id"] or row.version != subject["version"]:
        return _refuse("stale", "the item changed since it was shown to you; nothing was recorded", 409)
    if pick is not None and pick not in {letter for letter, _ in row.options}:
        return _refuse("bad_request", "that option is not offered", 400)
    if not requires_sign(row.kind, policy):
        return _refuse("desk_unsigned", "this answer is recorded unsigned; use the ordinary path", 422)
    if not row.recordable:
        return _refuse("desk_authority_pending", "this item cannot be signed at the desk yet; nothing was recorded", 422)
    try:
        key = (find_key or ts.find_key)(key_dir)
    except (KeyError_, OSError) as exc:
        return _refuse("key_unreadable", f"the Touch ID key cannot be read: {type(exc).__name__}", 503)
    if key is None:
        return _refuse("no_key", "no Touch ID key is enrolled; use the ordinary path", 409)
    if not state.lock.acquire(blocking=False):
        return _refuse("sign_busy", "a Touch ID sheet is already open", 409)
    try:
        said = {"answer": f"({pick}) " + dict(row.options)[pick]} if pick is not None else {"text": text}
        entry = ts.Entry(row.id, row.version, row.what, row.kind, **said)
        try:
            message = ts.build_desk_message(store=scope[0], install=scope[1], surface=surface, origin=req.origin,
                                            view="item", gesture="click", entries=[entry], at=at, nonce=nonce)
        except ts.MessageError as exc:
            return finish(_refuse("unsigned", f"the message could not be built: {exc}", 409, sign_reason="signer-error"))
        try:
            (notify or ts.post_notice)(ts.notice_text([entry], channel="desk"))
        except Exception:  # noqa: BLE001 - a notice is never worth the answer
            pass
        out = ts.sign(message, key=key, signer_factory=signer_factory or ts.default_desk_signer_factory)
        if out.state != "signed":
            words = {"cancelled": "Touch ID cancelled", "timeout": "Touch ID timed out"}.get(
                out.reason or "", f"Touch ID did not sign ({out.reason})")
            return finish(_refuse("unsigned", f"{words}: nothing recorded", 409, sign_reason=out.reason))
        signing = band_view.Signing("signed", None, "", message.decode("utf-8"), out.signature, out.fingerprint,
                                    {row.id: row.version})
        try:
            done = host.record(row, pick, text, f"cockpit:{surface}", signing)
        except band_view.VersionMoved:
            return finish(_refuse("stale", "the item changed while you signed; the signature was discarded and "
                                           "nothing was recorded", 409))
        except band_view.AuthorityItem as exc:
            return finish(_refuse("desk_authority_pending", str(exc), 422))
        except band_view.PickNotOffered:
            return finish(_refuse("bad_request", "that option is not offered", 400))
        except ValueError as exc:
            return finish(_refuse("answer_refused", str(exc), 422))
    finally:
        state.lock.release()
    started = bool(getattr(done, "answered", ())) and deliver is not None
    if started:
        threading.Thread(target=deliver, args=(done,), name="levain-answer-delivery", daemon=True).start()
    return finish(({"recorded": list(getattr(done, "recorded", [])), "signed": True, "channel": "desk",
                    "badge": "signed: Touch ID, laptop (desk)", "echo": getattr(done, "echo", ""),
                    "delivery": "started" if started else "none"}, 200))


class LevainDeskHost:
    """The desk host over a levain :class:`~levain.consent.questions.QuestionStore`. A foreign host store (flow's
    ``NeedsQuestions``) has no ``scope`` here: it takes a desk answer through capabilities it does not offer yet, so
    :meth:`scope` is None and the route reports ``desk_unsupported``. An authority item is not ``recordable``: its
    ruling travels in the signed message only after L4."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def scope(self) -> tuple[str, str] | None:
        from levain import band_view

        if band_view._foreign(self.store):
            return None
        sid = band_view._store_id_of(self.store)
        return (f"levain-questions:{sid}", sid) if sid else None

    def row(self, subject_id: str) -> DeskRow | None:
        from levain import band_view

        q = self.store.get(subject_id)
        if q is None or q.id != subject_id:
            return None
        kind = band_view._question_kind(q)
        options = tuple((band_view._LETTERS[i], label) for i, (_, label) in enumerate(q.options[:len(band_view._LETTERS)]))
        return DeskRow(q.id, band_view.subject_version(q), q.what, kind, options, q.answer is not None,
                       recordable=kind == "information")

    def record(self, row: DeskRow, pick: str | None, text: str | None, viewer: str, signing: Any) -> Any:
        from levain import band_view

        return band_view.answer_item_desk(self.store, {"kind": "question", "id": row.id, "version": row.version},
                                          pick=pick, text=text, viewer=viewer, signing=signing)
