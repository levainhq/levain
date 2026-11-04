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
           "desk_sign_available", "desk_text", "requires_sign"]

DESK_POLICY = "authority"          # NP-A (b): "authority" signs authority items; "all" (a) signs every desk answer
POLICIES = ("authority", "all")
_TXN = re.compile(r"[A-Za-z0-9._-]{8,64}")
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
    surface: str                   # the cockpit credential's name as the server knows it (its viewer), or "web"
    origin: str | None             # the page's origin from the validated Host (terminal_sign.desk_origin); None refuses
    body: Any                      # the parsed JSON body


class DeskState:
    """The process-wide state of the route: one non-blocking sign lock (one Touch ID sheet at a time) and the request
    ids it has seen. A ``client_txn`` is bound to the request it first named (route, surface and the canonical body):
    the same request again returns its first result and raises no second prompt (a duplicate that arrives while the
    first is still in flight waits for it); the same id naming a different request is refused."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._done: collections.OrderedDict[str, tuple[str, tuple[dict[str, Any], int]]] = collections.OrderedDict()
        self._inflight: dict[str, tuple[str, threading.Event]] = {}
        self._guard = threading.Lock()

    def begin(self, txn: str, fingerprint: str, *, wait: float = 0.0) -> tuple[dict[str, Any], int] | None:
        """Reserve ``txn`` for the request ``fingerprint``: None when this caller now owns it (it must call
        :meth:`end`), else the reply to give instead (the first result, or a refusal)."""
        import time

        until = time.monotonic() + wait
        while True:
            with self._guard:
                done = self._done.get(txn)
                if done is not None:
                    return done[1] if done[0] == fingerprint else _reused()
                flight = self._inflight.get(txn)
                if flight is None:
                    self._inflight[txn] = (fingerprint, threading.Event())
                    return None
                if flight[0] != fingerprint:
                    return _reused()
                event = flight[1]
            left = until - time.monotonic()
            if left <= 0 or not event.wait(left):
                return _refuse("sign_busy", "this request is already being answered", 409)

    def end(self, txn: str, result: tuple[dict[str, Any], int] | None) -> None:
        """Release ``txn``; with a ``result``, remember it for replay (a refusal before any prompt is not kept)."""
        with self._guard:
            flight = self._inflight.pop(txn, None)
            if result is not None and flight is not None:
                self._done[txn] = (flight[0], result)
                self._done.move_to_end(txn)
                while len(self._done) > _TXN_KEEP:
                    self._done.popitem(last=False)
        if flight is not None:
            flight[1].set()

    def replace(self, txn: str, result: tuple[dict[str, Any], int]) -> None:
        with self._guard:
            if txn in self._done:
                self._done[txn] = (self._done[txn][0], result)

    def finished(self, txn: str) -> tuple[dict[str, Any], int] | None:
        with self._guard:
            done = self._done.get(txn)
            return done[1] if done is not None else None


def _origin_ok(origin: Any) -> bool:
    from levain.consent import terminal_sign as ts

    return isinstance(origin, str) and ts._ORIGIN.fullmatch(origin) is not None


def _reused() -> tuple[dict[str, Any], int]:
    return _refuse("txn_reused", "this request id was already used for a different answer; nothing was recorded", 409)


def _fingerprint(route: str, req: "DeskRequest") -> str:
    import hashlib
    import json

    canon = json.dumps([route, req.surface, req.origin, req.body], sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=repr)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


_WAIT_S = 40.0          # how long a duplicate waits for its first request (a test holds it above SIGN_BOUND_S)


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
    if not req.local:
        return _refuse("desk_remote", "a click that did not come from the laptop's own cockpit never asks for Touch ID", 403)
    if not _origin_ok(req.origin):
        return _refuse("desk_origin", "the page's origin is not this server's own; nothing was asked or recorded", 403)
    if policy not in POLICIES:
        return _refuse("internal", "unknown desk policy", 500)
    got = _read_click(req.body)
    if got[0] is None:
        return _refuse("bad_request", str(got[1]), 400)
    subject, pick, text, txn = got
    first = state.begin(txn, _fingerprint("answer", req), wait=_WAIT_S)
    if first is not None:
        return first
    try:
        result, done = _desk_answer_owned(req, host, state, policy, signer_factory, key_dir, notify, find_key, at,
                                          nonce, subject, pick, text, txn, deliver is not None)
    finally:
        state.end(txn, None)            # a result from the prompt on was remembered already; this frees the rest
    return _deliver(state, txn, result, done, deliver)


def _deliver(state: DeskState, txn: str, result: tuple[dict[str, Any], int], done: Any,
             deliver: Callable[[Any], None] | None) -> tuple[dict[str, Any], int]:
    """Start delivery for a recorded answer AFTER its result is remembered. A thread that cannot start leaves the
    answer as recorded and says delivery was deferred (``levain question deliver`` retries it)."""
    body, status = result
    if status != 200 or done is None or body.get("delivery") != "started" or deliver is None:
        return body, status
    try:
        _start_delivery(deliver, done)
    except Exception:  # noqa: BLE001 - the answer is recorded; only its delivery waits
        body = {**body, "delivery": "deferred"}
        state.replace(txn, (body, status))
    return body, status


def _start_delivery(deliver: Callable[[Any], None], done: Any) -> None:
    threading.Thread(target=deliver, args=(done,), name="levain-answer-delivery", daemon=True).start()


def _desk_answer_owned(req: DeskRequest, host: DeskHost, state: DeskState, policy: str, signer_factory: Any,
                       key_dir: Any, notify: Callable[[str], Any] | None, find_key: Callable[..., Any] | None, at: Any,
                       nonce: str | None, subject: dict[str, str], pick: str | None, text: str | None, txn: str,
                       can_deliver: bool
                       ) -> tuple[tuple[dict[str, Any], int], Any]:
    """The click from the row re-read on, for the caller that owns ``client_txn``: its result and what was recorded
    (None when nothing was). A result from the prompt on is remembered for replay BEFORE the sign lock is
    released; a refusal before it is not kept, so the same request may be sent again."""
    from levain import band_view
    from levain.cockpit.keys import KeyError_
    from levain.consent import terminal_sign as ts

    viewer = f"cockpit:{req.surface if isinstance(req.surface, str) and req.surface else 'web'}"
    surface = ts.desk_surface(req.surface)      # the signed identity; the viewer keeps the credential's own name

    def finish(result: tuple[dict[str, Any], int], done: Any = None) -> tuple[tuple[dict[str, Any], int], Any]:
        state.end(txn, result)          # remembered while the sign lock is still held
        return result, done

    def refuse(*a: Any, **kw: Any) -> tuple[tuple[dict[str, Any], int], Any]:
        return _refuse(*a, **kw), None

    scope = host.scope()
    if scope is None:
        return refuse("desk_unsupported", "this store takes no desk answer", 422)
    row = host.row(subject["id"])
    if row is None or row.answered or row.id != subject["id"] or row.version != subject["version"]:
        return refuse("stale", "the item changed since it was shown to you; nothing was recorded", 409)
    if pick is not None and pick not in {letter for letter, _ in row.options}:
        return refuse("bad_request", "that option is not offered", 400)
    if not requires_sign(row.kind, policy):
        return refuse("desk_unsigned", "this answer is recorded unsigned; use the ordinary path", 422)
    if not row.recordable:
        return refuse("desk_authority_pending", "this item cannot be signed at the desk yet; nothing was recorded", 422)
    try:
        key = (find_key or ts.find_key)(key_dir)
    except (KeyError_, OSError) as exc:
        return refuse("key_unreadable", f"the Touch ID key cannot be read: {type(exc).__name__}", 503)
    if key is None:
        return refuse("no_key", "no Touch ID key is enrolled; use the ordinary path", 409)
    if not state.lock.acquire(blocking=False):
        return refuse("sign_busy", "a Touch ID sheet is already open", 409)
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
            done = host.record(row, pick, text, viewer, signing)
        except band_view.VersionMoved:
            return finish(_refuse("stale", "the item changed while you signed; the signature was discarded and "
                                           "nothing was recorded", 409))
        except band_view.AuthorityItem as exc:
            return finish(_refuse("desk_authority_pending", str(exc), 422))
        except band_view.PickNotOffered:
            return finish(_refuse("bad_request", "that option is not offered", 400))
        except ValueError as exc:
            return finish(_refuse("answer_refused", str(exc), 422))
        except Exception as exc:  # noqa: BLE001 - remembered, so a retry of this request never prompts again
            return finish(_refuse("internal", type(exc).__name__, 500))
        will = can_deliver and bool(getattr(done, "answered", ()))
        return finish(({"recorded": list(getattr(done, "recorded", [])), "signed": True, "channel": "desk",
                        "badge": "signed: Touch ID, laptop (desk)", "echo": getattr(done, "echo", ""),
                        "delivery": "started" if will else "none"}, 200), done)
    finally:
        state.lock.release()


def desk_text(req: DeskRequest, store: Any, state: DeskState, *, policy: str = DESK_POLICY, signer_factory: Any = None,
              key_dir: Any = None, notify: Callable[[str], Any] | None = None,
              deliver: Callable[[Any], None] | None = None, find_key: Callable[..., Any] | None = None
              ) -> tuple[dict[str, Any], int]:
    """``POST /cockpit/desk/text``: a line typed in the cockpit's band box, signed with the laptop key when the policy
    signs information answers (``all``). Body exactly ``{text, store_id, view_id, client_txn}``. Same walls as
    :func:`desk_answer`: not local 403 ``desk_remote``; the policy not signing information lines 422 ``desk_unsigned``
    (the page uses the ordinary band path); no key 409 ``no_key``; a held lock 409 ``sign_busy``; a replayed request
    id returns its first result. A cancelled or timed-out touch records NOTHING (409 ``unsigned``, the box keeps his
    words); a stale view is 409 ``stale_band``; a line that does not parse is 422. Never raises."""
    try:
        return _desk_text(req, store, state, policy, signer_factory, key_dir, notify, deliver, find_key)
    except Exception as exc:  # noqa: BLE001 - a route answers, it never leaks a traceback
        return _refuse("internal", type(exc).__name__, 500)


def _desk_text(req: DeskRequest, store: Any, state: DeskState, policy: str, signer_factory: Any, key_dir: Any,
               notify: Callable[[str], Any] | None, deliver: Callable[[Any], None] | None,
               find_key: Callable[..., Any] | None) -> tuple[dict[str, Any], int]:
    if not req.local:
        return _refuse("desk_remote", "a click that did not come from the laptop's own cockpit never asks for Touch ID", 403)
    if not _origin_ok(req.origin):
        return _refuse("desk_origin", "the page's origin is not this server's own; nothing was asked or recorded", 403)
    b = req.body
    if (not isinstance(b, Mapping) or set(b) != {"text", "store_id", "view_id", "client_txn"}
            or not isinstance(b["text"], str) or not b["text"].strip() or len(b["text"]) > 2000
            or not isinstance(b["store_id"], str) or not isinstance(b["view_id"], int) or isinstance(b["view_id"], bool)
            or not isinstance(b["client_txn"], str) or _TXN.fullmatch(b["client_txn"]) is None):
        return _refuse("bad_request", "answer with exactly {text, store_id, view_id, client_txn}", 400)
    txn = b["client_txn"]
    if policy not in POLICIES:
        return _refuse("internal", "unknown desk policy", 500)
    first = state.begin(txn, _fingerprint("text", req), wait=_WAIT_S)
    if first is not None:
        return first
    try:
        result, done = _desk_text_owned(req, store, state, policy, signer_factory, key_dir, notify, find_key, b, txn,
                                        deliver is not None)
    finally:
        state.end(txn, None)
    return _deliver(state, txn, result, done, deliver)


def _desk_text_owned(req: DeskRequest, store: Any, state: DeskState, policy: str, signer_factory: Any, key_dir: Any,
                     notify: Callable[[str], Any] | None, find_key: Callable[..., Any] | None, b: Mapping[str, Any],
                     txn: str, can_deliver: bool) -> tuple[tuple[dict[str, Any], int], Any]:
    import sqlite3

    from levain import band_view
    from levain.consent import terminal_sign as ts
    from levain.consent.questions import QuestionStoreError

    def finish(result: tuple[dict[str, Any], int], done: Any = None) -> tuple[tuple[dict[str, Any], int], Any]:
        state.end(txn, result)          # remembered while the sign lock is still held
        return result, done

    if not requires_sign("information", policy):
        return _refuse("desk_unsigned", "this line is recorded unsigned; use the ordinary path", 422), None
    viewer = f"cockpit:{req.surface if isinstance(req.surface, str) and req.surface else 'web'}"
    surface = ts.desk_surface(req.surface)      # the signed identity; the viewer keeps the credential's own name
    try:
        key = (find_key or ts.find_key)(key_dir)
    except Exception as exc:  # noqa: BLE001
        return _refuse("key_unreadable", f"the Touch ID key cannot be read: {type(exc).__name__}", 503), None
    if key is None:
        return _refuse("no_key", "no Touch ID key is enrolled; use the ordinary path", 409), None
    if not state.lock.acquire(blocking=False):
        return _refuse("sign_busy", "a Touch ID sheet is already open", 409), None
    try:
        signings: list[Any] = []
        try:
            done = band_view.answer_text_desk(
                store, b["text"], viewer=viewer, store_id=b["store_id"], view_id=b["view_id"],
                surface=surface, origin=req.origin, signer_factory=signer_factory, key_dir=key_dir, notify=notify,
                policy=policy, outcome=signings)
        except band_view.StaleBand as exc:       # before the prompt (no view) or inside the write after it
            got = _refuse("stale_band", str(exc), 409)
            return finish(got) if signings else (got, None)
        except ValueError as exc:
            if str(exc).startswith("Touch ID"):
                return finish(_refuse("unsigned", str(exc), 409))
            got = _refuse("answer_refused", str(exc), 422)
            return finish(got) if signings else (got, None)
        except (QuestionStoreError, OSError, sqlite3.Error) as exc:
            return finish(_refuse("source_unavailable", f"the question store cannot answer now: {exc}", 503))
        except Exception as exc:  # noqa: BLE001 - remembered, so a retry of this request never prompts again
            return finish(_refuse("internal", type(exc).__name__, 500))
        signed = bool(done.recorded) and any(getattr(g, "state", None) == "signed" for g in signings)
        if not done.recorded and not done.sheets:
            # every row the line named was answered elsewhere while the sheet was open: nothing was recorded, so
            # the box keeps his words
            return finish(_refuse("answer_refused", "; ".join(done.refused) or "nothing was recorded", 422,
                                  refused=list(done.refused)))
        will = can_deliver and bool(done.answered)
        return finish(({"recorded": done.recorded, "refused": done.refused, "sheets": list(done.sheets),
                        "echo": done.echo, "signed": signed, "channel": "desk",
                        "delivery": "started" if will else "none"}, 200), done)
    finally:
        state.lock.release()


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
