"""Board row 64, remote governance B2: the sign card for a band answer typed at a REMOTE terminal.

Ruling 103a stands: a remote answer is recorded at once and UNSIGNED (``sign_state='unsigned'``,
``sign_reason='remote'``); nothing here gates or delays it. After the write the seat hook files ONE card per typed
line in the cockpit Signer's queue: levain's own plain request in :data:`ANSWER_COUNTERSIGN_NAMESPACE`, whose message
is the ordinary ``levain-terminal-answer/1`` bytes plus a ``countersign-of:`` line naming the recorded answers. The
phone passkey's signature on it is EVIDENCE: :func:`verdict` reads it back (at badge time, no poller) and only a
verified signature over exactly the recorded answer reads as countersigned. Everything else (no card, an open card, a
deny, a bad signature, any error) is NOT countersigned.

Intent only, not established here: the phone's push subscription lives in the signing queue's own webpush store, so
the push goes to that queue dir explicitly (never to the hook's queue).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from levain.contracts.namespaces import ANSWER_COUNTERSIGN_NAMESPACE as NS, LEVAIN_SIGN_REQUESTS

__all__ = ["CARD_TITLE", "CARD_TTL_S", "CardResult", "Verdict", "build_card", "file_card", "nonce_of",
           "sign_queue_dir", "verdict", "badge_words"]

CARD_TTL_S = 24 * 3600
CARD_TITLE = "Countersign your terminal answer"
ENV_QUEUE = "LEVAIN_SIGN_QUEUE_DIR"
SETTINGS_KEY = "sign_queue_dir"
_TITLE_CUT = 120


@dataclass(frozen=True)
class CardResult:
    """What filing a card did. ``sent`` is true only when the card was filed, its pointers written and the push child
    started (which says nothing of arrival); ``why`` says what stopped it otherwise."""
    sent: bool
    why: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    """``state``: countersigned | open | expired | no-card | disowned | failed. Only ``countersigned`` reads as signed."""
    state: str
    reason: str = ""
    signed_at: str | None = None


def sign_queue_dir(environ: Mapping[str, str]) -> Path | None:
    """The cockpit Signer's queue dir: ``$LEVAIN_SIGN_QUEUE_DIR``, else the ``sign_queue_dir`` key of the hook
    queue's ``claude_code.json``, else None (no card). Absolute paths only: a relative one is never resolved, since
    the hook runs in a project's directory."""
    def absolute(value: Any) -> Path | None:
        if isinstance(value, str) and value and "\x00" not in value and Path(value).is_absolute():
            return Path(value)
        return None

    got = absolute(environ.get(ENV_QUEUE))
    if got is not None:
        return got
    try:
        from levain.consent.queue import queue_dir_for

        qdir = queue_dir_for(environ)
        try:
            data = json.loads((qdir / "claude_code.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        named = absolute(data.get(SETTINGS_KEY)) if isinstance(data, dict) else None
        if named is not None:
            return named
        # Nothing names a queue (a standard install: init writes no ``sign_queue_dir``). A cockpit serving the hook
        # queue itself (``levain serve --sign-queue-dir <that queue>``) has published its origin in it, which is
        # the owner saying this queue is the one it signs: derive from that, never from a guessed path.
        from levain.consent.cockpit_origin import read_origin

        return qdir if read_origin(qdir) is not None else None
    except Exception:  # noqa: BLE001 - no readable settings: no card
        return None


def sign_queue_hint(environ: Mapping[str, str]) -> str:
    """Why :func:`sign_queue_dir` is not usable and the exact fix, for a refusal: the named directory that is missing
    (and the flag that overrides it), or the hook queue to pass when nothing is named."""
    try:
        from levain.consent.queue import queue_dir_for

        qdir = queue_dir_for(environ)
    except Exception:  # noqa: BLE001
        return "pass --sign-queue-dir DIR (the directory `levain serve --sign-queue-dir` was started with)"
    found = sign_queue_dir(environ)
    if found is not None and not found.is_dir():
        return (f"the named signing queue {found} does not exist (from {ENV_QUEUE} or sign_queue_dir in "
                f"{qdir / 'claude_code.json'}): fix that value, or pass --sign-queue-dir DIR")
    return (f"pass --sign-queue-dir DIR (the directory `levain serve --sign-queue-dir` was started with; on a "
            f"standard install that is {qdir}), or set {ENV_QUEUE}, or add \"{SETTINGS_KEY}\": \"<DIR>\" to "
            f"{qdir / 'claude_code.json'}")


def nonce_of(store_id: str, session: str, stamp: str) -> str:
    """The card's nonce, a pure function of the recorded line, so the card's bytes are too: filing the same line again
    while its card is open returns the same request (once it is signed, denied or expired, a refile would file anew;
    no caller refiles, since a recorded answer is never answered again)."""
    return hashlib.sha256(f"{store_id}|{session}|{stamp}".encode("utf-8")).hexdigest()[:16]


def build_card(card: Mapping[str, Any], entries: Sequence[Any], stamp: str) -> bytes:
    """The card's message: ``terminal_sign.build_message`` over ``card`` (``store``, ``install``, ``session``,
    ``view``, ``typed``, ``typed_block``) and ``entries``, with ``countersign-of`` the answers' stamp. Raises
    ``terminal_sign.MessageError`` (a field out of form, or over the 8 KiB bound)."""
    from levain.cockpit.consent import parse_stamp
    from levain.consent import terminal_sign as ts

    return ts.build_message(store=card["store"], install=card["install"], session=card["session"], view=card["view"],
                            typed=card["typed"], typed_block=card.get("typed_block"), entries=list(entries),
                            nonce=nonce_of(card["install"], card["session"], stamp), at=parse_stamp(stamp),
                            countersign_of=stamp)


def file_card(store: Any, card: Mapping[str, Any] | None, recorded: Sequence[Any], *, push: bool = True,
              deadline: float | None = None) -> CardResult:
    """File the card for one typed line whose answers ``recorded`` (``questions.Answer`` values) were just written
    unsigned-remote, point each answer at it, and push it to the phone. ``card`` is ``Signing.card``: the message
    fields, the information ``entries``, and the ``environ`` subset that names the signing queue. The pointers' write
    waits for the store's lock only until ``deadline`` (``time.monotonic()``; default now + ``HOOK_BUSY_MS``): it runs
    in the seat hook, after the answer is already recorded. Never raises; a ``CardResult`` that is not ``sent`` says
    why in words an echo can carry."""
    try:
        return _file_card(store, card, recorded, push, deadline)
    except Exception as exc:  # noqa: BLE001 - a card is evidence; its failure never undoes an answer
        if isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc) or "busy" in str(exc)):
            # the lock was held past the deadline: the card was filed but nothing points at it; it lapses unsigned
            return CardResult(False, "the question store stayed busy")
        return CardResult(False, f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")


def _file_card(store: Any, card: Mapping[str, Any] | None, recorded: Sequence[Any], push: bool,
               deadline: float | None) -> CardResult:
    from levain.band_view import BAND_SCHEMA
    from levain.cockpit.consent import utc_stamp
    from levain.consent.cockpit_origin import read_origin
    from levain.consent.questions import HOOK_BUSY_MS
    from levain.consent.sign import deep_link, levain_request_for

    if card is None or not recorded:
        return CardResult(False, "no card to file")
    qdir = sign_queue_dir(card.get("environ") or {})
    if qdir is None:
        return CardResult(False, "no sign queue is named (LEVAIN_SIGN_QUEUE_DIR, or sign_queue_dir in claude_code.json)")
    stamps = {a.answered_at for a in recorded}
    if len(stamps) != 1:
        return CardResult(False, "the answers carry more than one time")
    stamp = stamps.pop()
    ids = {a.question_id for a in recorded}
    entries = [e for e in card["entries"] if e.item in ids]
    if not entries:
        return CardResult(False, "no recorded answer is on the card")
    message = build_card(card, entries, stamp)
    rid = levain_request_for(qdir, NS, message.decode("utf-8"), title=CARD_TITLE, ttl_s=CARD_TTL_S)
    sha = hashlib.sha256(message).hexdigest()
    by = deadline if deadline is not None else time.monotonic() + HOOK_BUSY_MS / 1000
    store.ready_by(by)
    with store._write_by(by) as conn:
        for stmt in BAND_SCHEMA:
            conn.execute(stmt)
        for e in entries:
            conn.execute("INSERT OR IGNORE INTO answer_countersign (question_id, request_id, message_sha256, "
                         "row_version, filed_at) VALUES (?, ?, ?, ?, ?)",
                         (e.item, rid, sha, e.row_version, utc_stamp(datetime.now(timezone.utc))))
    if not push:
        return CardResult(False, "no push was asked for", rid)
    origin = read_origin(qdir)
    if origin is None:
        return CardResult(False, "the cockpit has published no origin to link the card to", rid)
    from levain import webpush
    from levain.webpush import sender

    if not webpush.live(qdir):
        return CardResult(False, "no phone is subscribed to the signing queue", rid)
    body = str(card["typed"])[:_TITLE_CUT]
    if not sender.spawn(qdir, kind="question", ref=entries[0].item, title=CARD_TITLE, body=body,
                        url=deep_link(origin, rid)):
        return CardResult(False, "the push could not be started", rid)
    return CardResult(True, None, rid)


# --- the read-time verifier ---------------------------------------------------------------------------------------
_VERIFIED: "dict[str, bool]" = {}          # sha256(message, signature, key, namespace) -> the ssh verifier's answer
_VERIFIED_MAX = 128


def _verified(message: bytes, signature: str, key: Any) -> bool:
    """:func:`levain.authority.verify_link_sig`, remembered: the answer is a pure function of these inputs. A raise
    (the verifier unavailable) is never remembered."""
    from levain.authority import verify_link_sig

    k = hashlib.sha256(json.dumps([message.hex(), signature, key.public_key, key.fingerprint, NS]).encode()).hexdigest()
    if k in _VERIFIED:
        return _VERIFIED[k]
    ok = bool(verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint, namespace=NS))
    while len(_VERIFIED) >= _VERIFIED_MAX:
        _VERIFIED.pop(next(iter(_VERIFIED)))
    _VERIFIED[k] = ok
    return ok


def _recorded_text(q: Any) -> str:
    """The recorded answer as the card's entry writes it: ``(a) option text`` or the free words."""
    from levain.band_view import _LETTERS

    a = q.answer
    if a.kind == "option":
        i = [k for k, _ in q.options].index(a.value)
        return f"({_LETTERS[i]}) {q.options[i][1]}"
    return a.value


def verdict(store: Any, question_id: str, *, sign_queue: Path | str | None,
            keys: Any = None, now: datetime | None = None) -> Verdict:
    """Whether the phone countersigned the recorded answer ``question_id``. Never raises; the first failing check is
    the verdict. ``keys`` (a callable returning the active passkeys, fingerprint -> key) and ``now`` are for tests."""
    try:
        return _verdict(store, question_id, sign_queue, keys, now)
    except Exception as exc:  # noqa: BLE001 - fail closed
        return Verdict("failed", f"{type(exc).__name__}")


def _verdict(store: Any, question_id: str, sign_queue: Path | str | None, keys: Any, now: datetime | None) -> Verdict:
    from levain.authority import VerifierUnavailable
    from levain.cockpit.consent import parse_stamp
    from levain.consent import terminal_sign as ts
    from levain.consent.cockpit_origin import read_origin
    from levain.consent.queue import class_covers
    from levain.consent.sign import SignRefused, passkey_key_source, read_request, sign_dir, state_of
    from levain.coordinator.review import SIGN_TIER

    if sign_queue is None:
        return Verdict("no-card", "no sign queue is named")
    qdir = Path(sign_queue)
    with store._peek() as conn:
        if conn is None:
            return Verdict("no-card", "no store")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'answer_countersign'").fetchone() is None:
            return Verdict("no-card", "no card was filed")
        ptr = conn.execute("SELECT request_id, message_sha256, row_version FROM answer_countersign "
                           "WHERE question_id = ?", (question_id,)).fetchone()
    if ptr is None:
        return Verdict("no-card", "no card was filed")
    rid, want_sha, row_version = ptr
    q = store.get(question_id)
    if q is None or q.answer is None:
        return Verdict("failed", "the answer is not recorded")
    try:
        request = read_request(qdir, rid)
    except SignRefused:
        return Verdict("failed", "the card is gone")
    if request.refused is not None or request.namespace != NS or request.requester != "levain":
        return Verdict("failed", "the card is not levain's own")
    if re.fullmatch(LEVAIN_SIGN_REQUESTS[NS], request.message) is None:
        return Verdict("failed", "the card is not in its grammar")
    message = request.message.encode("utf-8")
    if hashlib.sha256(message).hexdigest() != want_sha:
        return Verdict("failed", "the card is not the one filed")
    state = state_of(qdir, rid)
    moment = now or datetime.now(timezone.utc)
    if state == "denied":
        return Verdict("disowned", "denied on the phone")
    if state != "signed":
        return Verdict("expired" if request.expires_at is not None and request.expires_at <= moment else "open")
    d = sign_dir(qdir)
    try:
        record = json.loads((d / f"{rid}.signed.json").read_text(encoding="utf-8"))
        signature = (d / f"{rid}.sig").read_text(encoding="ascii")
    except (OSError, ValueError):
        return Verdict("failed", "the signature cannot be read")
    if (not isinstance(record, dict) or record.get("namespace") != NS
            or record.get("message_sha256") != hashlib.sha256(message).hexdigest()):
        return Verdict("failed", "the record is not over the card")
    origin = read_origin(qdir)
    host = urlsplit(origin).hostname if origin else None
    if not host:
        return Verdict("failed", "no cockpit origin to name the passkey's site")
    try:
        active = (keys or passkey_key_source(qdir, host))()
    except SignRefused:
        return Verdict("failed", "the passkeys cannot be read now")
    fp = record.get("signer_fp")
    key = active.get(fp) if isinstance(fp, str) else None
    if key is None:
        return Verdict("failed", "not signed by an enrolled passkey")
    if not class_covers(key.key_class, SIGN_TIER):
        return Verdict("failed", "the passkey's class does not cover signing")
    try:
        ok = _verified(message, signature, key)
    except VerifierUnavailable:
        return Verdict("failed", "the signature could not be verified now")
    if not ok:
        return Verdict("failed", "the signature does not verify")
    try:
        parsed = ts.parse_verified(message)
    except ts.MessageError:
        return Verdict("failed", "the card does not parse")
    entry = next((e for e in parsed.entries if e.item == question_id), None)
    sid = _store_id(store)
    stamp = q.answer.answered_at
    if (parsed.countersign_of != stamp or sid is None or parsed.store != f"levain-questions:{sid}"
            or parsed.install != sid or entry is None or entry.row_version != row_version or entry.asked != q.what
            or (entry.answer if entry.answer is not None else entry.text) != _recorded_text(q)):
        return Verdict("failed", "the card is for another answer")
    try:
        signed_at = parse_stamp(record.get("signed_at"))
        expires = request.expires_at
        if signed_at < parse_stamp(stamp) or (expires is not None and signed_at > expires):
            return Verdict("failed", "signed outside its window")
    except Exception:  # noqa: BLE001 - a record without a readable time is not evidence
        return Verdict("failed", "the signing time cannot be read")
    return Verdict("countersigned", "", record["signed_at"])


def _store_id(store: Any) -> str | None:
    with store._peek() as conn:
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        return row[0] if row else None


def badge_words(v: Verdict) -> str:
    """The badge text for a verdict on a remote-unsigned answer."""
    if v.state == "countersigned":
        return f"countersigned on your phone {v.signed_at or ''}".rstrip()
    if v.state == "disowned":
        return "DISOWNED on your phone"
    if v.state == "failed":
        return f"countersign FAILED: {v.reason}"
    return "remote, unsigned"
