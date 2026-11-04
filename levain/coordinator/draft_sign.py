"""levain.coordinator.draft_sign: H1-3, the sign flow of a draft envelope, as CoordPhone actions the cockpit carries.

The journey from a lapsed or revoked head to a signed successor, each step one :meth:`CoordPhone.step` action, written
for the cockpit's ``/cockpit/coord/<action>`` (``CoordSheet`` in ``levain/web_server.py``), which carries it by name
behind the gate the envelope drop already has. The phone port's own ``/coord/`` routes were retired in W1b slice C, so
the doors that carry these actions are the ones that build a ``CoordPhone``: ``git grep -n "CoordPhone(" -- levain``.
Nothing here is a new signer or a new credential path: the passkey ceremony, the SSHSIG conversion, the authority
read and the store's own verification are the ones ``envelope-review`` / ``envelope-challenge`` / ``drop`` use.

- ``draft-cards`` {thread_id, store_id}: the card the thread shows now (LAPSED / REVOKED / ENVELOPE, or none). Reads.
- ``draft-rule`` {thread_id, store_id, rule, head[, prefill]}: ``rule`` is ``renew`` (the LAPSED card's *extend*) or
  ``replace`` (the REVOKED card's *replace*); ``head`` is the envelope id the card showed. The rule writes an UNSIGNED
  draft (it grants nothing) from the head state read now, and a head that is not the one the card showed is refused.
- ``draft-withdraw`` {draft_id, revision, store_id}: the card's *withdraw*, of exactly ``revision``: the store compares
  it under the same write lock as the withdrawal, so a revision that landed after the card was read is refused.
- ``draft-review`` {draft_id, revision, digest, signer_fp, store_id}: runs the builder (``draft_sign_bytes``) and answers
  the diffs IT returns; the sign control renders these, never a cached card's. KEEPS the request and the diffs it
  answered under a review id.
- ``draft-challenge`` {review_id}: Sign tapped. Runs the builder again with ``signed_at`` now, so a draft that went stale
  while the sheet was read is refused before any byte is built, and so are diffs that are no longer the ones the review
  answered (the head's key revoked or the head narrowed since): the page reviews again. Answers the payload and the
  WebAuthn challenge.
- ``draft-accept`` {review_id, body_text, payload, authenticator_data, client_data_json, signature}: the assertion
  turned into an SSHSIG and handed to :meth:`EnvelopeStore.accept_envelope`, which re-derives the body from the stored
  revision and re-checks everything under its lock. The body and payload must be the ones the challenge minted for this
  review, and a review is spent by the store's answer (a definitive refusal spends it too; a fault on this machine
  keeps it for the retry). Once the envelope is stored, a fault reading the cards that follow answers the envelope with
  ``cards: None`` and a ``cards_note``, never a refusal.

A kept review is held by the phone object (``CoordPhone._draft_reviews``), bound to the ``device`` the door passes, and
expires with :data:`levain.coordinator.phone.REVIEW_TTL`. The cockpit passes one constant device for every page, so the
binding separates devices only on a door that resolves a paired device per request. No kept state is trusted past the
store: the acceptance never reads it.
"""
from __future__ import annotations

import secrets
from typing import Any, Callable

__all__ = ["DRAFT_ACTIONS", "handle"]

DRAFT_ACTIONS = ("draft-cards", "draft-rule", "draft-withdraw", "draft-review", "draft-challenge", "draft-accept")
# the actions whose reply carries or asks for a signature: a page the verification would refuse never gets one
SIGNING = ("draft-review", "draft-challenge", "draft-accept")

_KEYS = {
    "draft-cards": {"thread_id", "store_id"},
    "draft-withdraw": {"draft_id", "revision", "store_id"},
    "draft-review": {"draft_id", "revision", "digest", "signer_fp", "store_id"},
    "draft-challenge": {"review_id"},
    "draft-accept": {"review_id", "body_text", "payload", "authenticator_data", "client_data_json", "signature"},
}
_RULES = ("renew", "replace")


def _need(req: dict[str, Any], action: str) -> None:
    from levain.coordinator.phone import CoordRefused

    if action == "draft-rule":
        ok = {"thread_id", "store_id", "rule", "head"} <= set(req) <= {"thread_id", "store_id", "rule", "head", "prefill"}
        want = "thread_id, store_id, rule, head and optionally prefill"
    else:
        ok = set(req) == _KEYS[action]
        want = ", ".join(sorted(_KEYS[action]))
    if not ok:
        raise CoordRefused(400, "bad_request", f"request keys must be {want}")


def _text(req: dict[str, Any], *names: str) -> None:
    from levain.coordinator.phone import CoordRefused

    for n in names:
        if not isinstance(req[n], str) or not req[n]:
            raise CoordRefused(400, "bad_request", f"{n} is text")


def _store(phone: Any, store_id: Any) -> Any:
    """The store, only when it is the one the page read. A draft is never the first thing in a store, so a missing one
    is refused too (the drop's :meth:`CoordPhone._bound_store` would make it)."""
    from levain.coordinator.phone import CoordRefused

    if not isinstance(store_id, str) or not store_id:
        raise CoordRefused(400, "bad_request", "store_id is the store identity the listing reported")
    store = phone._store(create=False)
    here = None if store is None else store.store_id()
    if here != store_id:
        raise CoordRefused(409, "other_store", f"this was read from another Levain (store {store_id[:40]}), not this "
                                               f"one ({here or 'no store yet'}): nothing was changed")
    return store


def _authority(store: Any) -> Any:
    from levain.dispatch.cli import _authority_source

    return _authority_source(store.directory, None)()


def _view_lines(against: Any) -> list[str]:
    """The words above the diffs: what the head is, and each widening named. The diffs themselves ride beside as data."""
    heads = {"effective": "against the head as narrowed",
             "revoked": "the head no longer authorises anything (killed / its key revoked / its thread ended): every "
                        "value below is a new grant",
             "none": "no head"}
    if against.base_kind not in heads:
        # a kind the sheet has no words for: the builder grew one this page cannot describe, so it shows no Sign.
        # Not DraftStale: reviewing again would meet the same kind.
        from levain.coordinator.store import CoordError

        raise CoordError(f"the head is of a kind this sheet cannot describe ({str(against.base_kind)[:40]})")
    lines = [heads[against.base_kind] + (f" ({against.reason})" if against.reason and against.base_kind == "revoked"
                                         else "")]
    if against.wider:
        lines.append("this signature grants MORE than the head it replaces")
    lines += [f"thread member added: {d}" for d in against.members_added]
    lines += [f"thread member removed: {d}" for d in against.members_removed]
    return lines


def _built(phone: Any, store: Any, kept: dict[str, Any], key: Any, now: Any) -> tuple[str, dict[str, str], Any, Any]:
    from levain.coordinator import envelope_drafts as drafts

    return drafts.draft_sign_bytes(store, kept["draft_id"], kept["revision"], kept["digest"],
                                   authority=_authority(store), signer_fp=key.fingerprint, signed_at=now)


def _cards(store: Any, thread_id: str) -> list[dict[str, Any]]:
    from levain.coordinator import envelope_drafts as drafts

    return [c.as_dict() for c in drafts.cards(store, thread_id, authority=_authority(store),
                                              queue_dir=store.directory)]


def _cards_after(store: Any, thread_id: Callable[[], str | None]) -> dict[str, Any]:
    """The cards for a reply to a write that has COMMITTED. Finding the thread (a store read, or a field of the stored
    body) and reading its cards (another verifier call) both run inside the one guard: whatever either raises, the
    write stands, so the reply says what was written and asks for the cards again rather than becoming a refusal that
    claims nothing changed. A thread of None answers no cards. The caller's ``store_id`` is the one ``_store`` already
    matched before the write, never a fresh read after it."""
    try:
        tid = thread_id()
        return {} if tid is None else {"cards": _cards(store, tid)}
    except Exception as exc:  # noqa: BLE001  (any fault here is after the commit; the reply must still report it)
        return {"cards": None, "cards_note": f"the cards could not be read just now ({type(exc).__name__}): "
                                              "read the cards again"}


def _against(against: Any) -> dict[str, Any]:
    return {"base_kind": against.base_kind, "reason": against.reason, "diff": against.diff,
            "members_added": against.members_added, "members_removed": against.members_removed,
            "wider": against.wider}


def handle(phone: Any, action: str, req: dict[str, Any], device: str) -> dict[str, Any]:
    from levain.coordinator.phone import CoordRefused

    _need(req, action)
    if action == "draft-cards":
        _text(req, "thread_id")
        store = _store(phone, req["store_id"])
        return {"cards": _cards(store, req["thread_id"]), "store_id": store.store_id()}
    if action == "draft-rule":
        return _rule(phone, req)
    if action == "draft-withdraw":
        return _withdraw(phone, req)
    if action == "draft-review":
        return _review(phone, req, device)
    if action == "draft-challenge":
        return _challenge(phone, req, device)
    if action == "draft-accept":
        return _accept(phone, req, device)
    raise CoordRefused(404, "not_found", "no such coordinator action")


def _rule(phone: Any, req: dict[str, Any]) -> dict[str, Any]:
    from levain.coordinator import envelope_drafts as drafts
    from levain.coordinator.phone import CoordRefused

    _text(req, "thread_id", "rule", "head")
    if req["rule"] not in _RULES or not isinstance(req.get("prefill", True), bool):
        raise CoordRefused(400, "bad_request", f"rule is one of {list(_RULES)} and prefill a boolean")
    store = _store(phone, req["store_id"])
    hs = drafts.head_state(store, req["thread_id"], authority=_authority(store), queue_dir=store.directory)
    if hs.envelope_id != req["head"]:
        # the card the page drew named another head (or none): a rule over a head it did not show would sign a
        # successor for something the operator never looked at
        raise drafts.DraftStale(f"thread {req['thread_id']}'s head is not the envelope the card showed "
                                f"({req['head'][:40]}); read the card again")
    if req["rule"] == "renew":
        d = drafts.renew_rule(store, req["thread_id"], hs, by="operator")
    else:
        d = drafts.replace_rule(store, req["thread_id"], hs, prefill=req.get("prefill", True), by="operator")
    return {"draft": {"draft_id": d.draft_id, "revision": d.revision, "digest": d.digest, "thread_id": d.thread_id},
            **_cards_after(store, lambda: req["thread_id"]), "store_id": req["store_id"]}


def _withdraw(phone: Any, req: dict[str, Any]) -> dict[str, Any]:
    from levain.coordinator import envelope_drafts as drafts
    from levain.coordinator.phone import CoordRefused
    from levain.coordinator.store import CoordError

    _text(req, "draft_id")
    if type(req["revision"]) is not int:
        raise CoordRefused(400, "bad_request", "revision is an integer")
    store = _store(phone, req["store_id"])
    try:
        revision = drafts.withdraw(store, req["draft_id"], by="operator", expect_revision=req["revision"])
    except drafts.DraftStale:
        raise
    except CoordError as exc:
        raise drafts.DraftStale(_not_withdrawn(store, req["draft_id"], exc)) from None

    def thread() -> str | None:
        newest = drafts.latest(store, req["draft_id"])
        return None if newest is None else newest.thread_id

    return {"withdrawn": {"draft_id": req["draft_id"], "revision": revision}, **_cards_after(store, thread),
            "store_id": req["store_id"]}


def _not_withdrawn(store: Any, draft_id: str, exc: Exception) -> str:
    """The words for a withdrawal the store refused: what the series is (signed, withdrawn, absent), never the table's
    own refusal text. Signed and withdrawn are terminal, so reading them after the refusal names what refused it."""
    from levain.coordinator import envelope_drafts as drafts
    from levain.coordinator.store import CoordError

    try:
        st = drafts.state_of(store, draft_id)
    except CoordError:
        return f"there is no draft {draft_id}"
    if st.kind == "signed":
        return f"draft {draft_id} is already signed (envelope {st.reason}); there is nothing to withdraw"
    if st.kind == "withdrawn":
        return f"draft {draft_id} is already withdrawn"
    raise exc


def _review(phone: Any, req: dict[str, Any], device: str) -> dict[str, Any]:
    from levain.coordinator.phone import REVIEW_TTL, REVIEWS_KEPT, CoordRefused

    _text(req, "draft_id", "digest", "signer_fp")
    if type(req["revision"]) is not int:
        raise CoordRefused(400, "bad_request", "revision is an integer")
    key = phone._key(req["signer_fp"])
    store = _store(phone, req["store_id"])
    now = phone._clock()
    kept = {"device": device, "draft_id": req["draft_id"], "revision": req["revision"], "digest": req["digest"],
            "signer_fp": key.fingerprint, "store_id": store.store_id(), "until": now + REVIEW_TTL, "minted": None}
    # the builder IS the check: it raises DraftStale before any verifier call or byte when the draft is not signable
    body_text, _payload, since, against = _built(phone, store, kept, key, now)
    view = _view_lines(against)          # before the review is kept: a head the sheet cannot describe keeps nothing
    kept["against"] = _against(against)
    kept["body_text"] = body_text
    review_id = "dv-" + secrets.token_hex(16)
    with phone._lock:
        for rid in [r for r, v in phone._draft_reviews.items() if v["until"] <= now]:
            del phone._draft_reviews[rid]
        while len(phone._draft_reviews) >= REVIEWS_KEPT:
            del phone._draft_reviews[next(iter(phone._draft_reviews))]
        phone._draft_reviews[review_id] = kept
    return {"review_id": review_id, "body_text": body_text, "rp_id": phone.rp_id, "since_parent": since,
            "against": kept["against"], "view": view, "store_id": kept["store_id"]}


def _kept(phone: Any, review_id: Any, device: str) -> dict[str, Any]:
    from levain.coordinator.phone import CoordRefused

    if not isinstance(review_id, str):
        raise CoordRefused(400, "bad_request", "review_id is text")
    now = phone._clock()
    with phone._lock:
        kept = phone._draft_reviews.get(review_id)
    if kept is None or kept["until"] <= now or kept["device"] != device:
        raise CoordRefused(409, "review_gone", "this draft's review is no longer held here (it expired, it was already "
                                               "used, or the port restarted): review it again, then sign")
    return kept


def _challenge(phone: Any, req: dict[str, Any], device: str) -> dict[str, Any]:
    from levain.consent.passkey import challenge
    from levain.coordinator import envelope as env
    from levain.coordinator import envelope_drafts as drafts
    from levain.coordinator.phone import _b64u

    _text(req, "review_id")
    kept = _kept(phone, req["review_id"], device)
    key = phone._key(kept["signer_fp"])
    store = _store(phone, kept["store_id"])          # the store the review read, never one a later request names
    now = phone._clock()
    # again, with signed_at now: a long read of the sheet is never a stale signature, and a draft that moved is refused
    body_text, payload, _since, against = _built(phone, store, kept, key, now)
    if _against(against) != kept["against"] or body_text != kept["body_text"]:
        # the sheet said what this signature grants against the head (as narrowed, or every value new); a head whose
        # key was revoked or that was narrowed since makes those words untrue for the same bytes, so no challenge.
        # The body is compared too: the sheet showed it, and the challenge signs only the body the review showed
        raise drafts.DraftStale("what this signature grants against the head is no longer what the sheet showed (its "
                                "key was revoked, or it was narrowed or ended, since the review): review it again")
    kept["minted"] = {"body_text": body_text, "payload": payload}
    return {"payload": payload, "rp_id": phone.rp_id,
            "challenge": _b64u(challenge(env.sign_message(payload), namespace=env.SIGN_NAMESPACE)),
            "store_id": kept["store_id"]}


def _accept(phone: Any, req: dict[str, Any], device: str) -> dict[str, Any]:
    from levain.consent.passkey import b64url_decode, to_sshsig
    from levain.coordinator import envelope as env
    from levain.coordinator.phone import CoordRefused

    kept = _kept(phone, req["review_id"], device)
    minted = kept["minted"]
    if minted is None:
        raise CoordRefused(409, "no_challenge", "this review was never asked for a challenge: tap Sign first")
    if req["body_text"] != minted["body_text"] or req["payload"] != minted["payload"]:
        raise CoordRefused(422, "refused", "the body or payload is not the one this review's challenge was minted for: "
                                           "nothing was signed")
    key = phone._key(kept["signer_fp"])          # the payload is the minted one, so it already names this signer
    store = _store(phone, kept["store_id"])
    signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                          b64url_decode(req["client_data_json"], "client_data_json"),
                          b64url_decode(req["signature"], "signature"),
                          namespace=env.SIGN_NAMESPACE, page_origin=phone.page_origin)
    # read right before the write: a key revoked while the phone waited signs nothing
    authority = _authority(store)
    try:
        stored, created = store.accept_envelope(req["body_text"], req["payload"], signature, authority=authority)
    except Exception as exc:
        # a definitive refusal spends the review (sign again from a fresh one); a machine fault leaves it for the retry
        from levain.authority import AuthorityError, VerifierUnavailable
        import sqlite3

        if not isinstance(exc, (VerifierUnavailable, AuthorityError, OSError, sqlite3.Error)):
            with phone._lock:
                phone._draft_reviews.pop(req["review_id"], None)
        raise
    with phone._lock:
        phone._draft_reviews.pop(req["review_id"], None)
    return {"created": created, "envelope": {"envelope_id": stored.envelope_id, "signer_fp": stored.signer_fp,
                                              "signer_class": stored.signer_class},
            **_cards_after(store, lambda: _bound_thread(stored)), "store_id": kept["store_id"]}


def _bound_thread(stored: Any) -> str:
    """The thread a draft envelope was bound to (a drafted body binds exactly one)."""
    return stored.body["binds"][0]["thread_id"]
