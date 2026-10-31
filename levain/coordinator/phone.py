"""levain.coordinator.phone — the phone's drop sheet: a drop from the phone, its outbox, and an envelope signed with Face ID.

The phone port (:mod:`levain.phone_server`) carries the coordinator's phone half (C1/C2, BUILD_PLAN rows C1 and C2):

- **Who may call** (L3 r1 codex HIGH): every coordinator route needs a PAIRED DEVICE's token. The phone port resolves
  its credential once and passes the device id in (``step(..., device=)``); this run's transient phone token opens the
  queue, never the coordinator store, and a step with no device is refused (403) before anything is read or written.
- **A drop** (``POST /coord/drop`` {words, outbox_id, sent_at, store_id[, envelope]}) goes into the SAME store call
  ``levain drop`` makes (:meth:`levain.coordinator.envelopes.EnvelopeStore.accept_drop`), with ``surface="phone"``.
  There is no second write path: the drop, its ``delivered`` drop receipt and any coordinator wake are that call's one
  transaction.
- **The store identity** (L3 r1 codex HIGH): the listing reports ``coord_store_id`` (the store's random id, None while
  there is no store). The page binds every outbox item to the identity it held when the item was made and sends it as
  ``store_id``; a drop whose ``store_id`` is not this store's (or that names a store where there is none, or names none
  where there is one) is refused 409 ``other_store`` with nothing written, so a drop made against one Levain is never
  filed into another that later answers on the same name. Every reply that makes or names the store carries
  ``store_id``, and the review and challenge replies also ``store_created`` (L3 r2, both seats: on a fresh install the
  REVIEW makes the store, so the page binds the signed drop to the id its review reply names, and rebinds every item
  it made while there was no store only when that reply says this request made it). An item bound to None is
  accepted by the store the same request creates; on a store it did not see made, only its resend lookup answers.
  The REVIEW carries the page's ``store_id`` too and passes the same :meth:`CoordPhone._bound_store` the drop does
  (L3 r3 codex HIGH): a page that read store A while the name now answers for store B is refused ``other_store``
  before anything is built or kept, so its reply never names B and no envelope is signed for a store the page did
  not read. A None identity becomes the reply's only when this review made the store (``store_created``). The
  CHALLENGE binds to the store id KEPT with its review (L3 r4, both seats), never one a request names: a store replaced
  or gone between the review and Sign is ``other_store`` with no payload minted, and the challenge never makes a store
  (its ``store_created`` is always false).
- **The outbox** (gap 6): the page keeps a drop until this route answers 2xx. The page picks the ``outbox_id`` once,
  when the drop is made, and resends it unchanged. A resend is looked up FIRST (:meth:`CoordStore.resent_drop`, the
  words and surface compared) and answered with the stored drop (``created: false``) with nothing else checked, so a
  carrier that has since gone stale (a revoked passkey, an old signature) never turns a delivered drop into a refusal;
  the same id with other words is a conflict (409). A retry after a lost reply never files twice, and the 2xx IS the
  delivered receipt. The resend lookup runs BEFORE the store-id check (L3 r2 codex MED): a first drop that made the
  store and whose reply was lost is sent again still bound to None, and gets its stored receipt and the store's id;
  one that matches nothing is still ``other_store``, so no new write ever passes the identity check.
- **A structured envelope signed at send** (T4, C0 §4), in two steps so the signature is as fresh as the tap:
  ``POST /coord/envelope-review`` {words, outbox_id, spec, signer_fp, store_id} builds the body with the CLI's own
  builder (:func:`levain.coordinator.cli._spec_body`, the one ``levain drop --envelope`` signs), holds it to the
  store's disk checks (``check_body``), KEEPS it here under a ``review_id`` and answers its canonical text, its digest and the
  plain-words view the CLI prints before signing (:func:`levain.coordinator.cli._view`). ``POST
  /coord/envelope-challenge`` {review_id}, sent when Sign is tapped, mints the payload
  (:func:`levain.coordinator.envelope.sign_payload`) with ``signed_at`` = now over the KEPT body and answers it with the
  WebAuthn challenge over it in :data:`~levain.coordinator.envelope.SIGN_NAMESPACE`. The drop then carries {body_text,
  payload, assertion}; the server turns the assertion into an SSHSIG (:func:`levain.consent.passkey.to_sshsig`) and
  hands body, payload and signature to the same ``accept_drop(envelope=...)`` the CLI calls, verified against the same
  authority source the CLI's :func:`levain.coordinator.cli._snapshot` reads
  (:func:`levain.dispatch.cli._authority_source` over the store's directory). No verification code lives here.
- **A signed drop is never stored plain** (L3 r1, both seats): the phone's outbox still holds the words, so the store
  is asked with ``keep_plain=False`` and an envelope it would not take writes nothing: a stale signature is 422
  ``envelope-stale`` (the page offers Sign again), a verifier that did not answer is 503 (the page sends it again), a
  drop or envelope id already stored is 409, any other refusal 422. The page then offers Sign again or Send as a plain
  drop (the same outbox id, ``envelope: null``).
- **The wake** (head ruling Q1): a drop the store ACCEPTED whose own transaction asked for a coordinator launch is
  started by the exact call ``levain drop`` makes (:func:`levain.coordinator.launch.launch_session`, the CLI's
  ``_launch_woken``), once: only a drop this call created names one (``woken_launch`` is read once), so a resend, a
  refusal and the run token wake nothing. It runs on its own thread, so the reply never waits on a spawn, and its
  outcome, a failure included, is a ``woken`` drop receipt (:meth:`CoordStore.record_wake`). A thread that cannot be
  started (L3 r2 codex MED) is that receipt too, and the reply is still the stored drop's.

A land from the phone is always ``from: "named"``, ``onto_from: "inferred"``: the phone sheet has no repo/branch picker
yet, so the drop records no source, and C0 §4.3 allows ``from: "source"`` only for a drop that recorded one. The body
the passkey signs carries that ``inferred`` mark, and the view says it in words (C0 §1, Phill 10-20: *"for 4 I'll take
your recs across all items"*, item 1).

A paired device's token only opens the routes: an envelope's authority is the passkey's assertion, which the store
verifies.
The listing (:meth:`CoordPhone.listing`) never creates the store; a drop does, exactly as ``levain drop`` does. Stdlib
only.

- **The review queue and the morning card** (C4-c, C6-c; READ-ONLY on the phone): ``GET /coord.json``
  (:meth:`CoordPhone.coord_listing`, not ``/consent.json``, so the page's poll pays for it only while the Review tab is
  shown) carries ``coord_review`` (the queue's open count and its bounded pages, from :func:`levain.coordinator.review.queue_page`) and
  ``coord_morning`` (the card, :func:`levain.coordinator.morning.card` and its own
  :func:`~levain.coordinator.morning.render` lines), each with a ``*_note`` when the store could not be read. The queue
  is read once per response and both come from that read. "Waiting on you" is the card's own derived count, never
  computed here. Nothing on this port clears or reopens an item (tests/test_coordinator_phone_review.py pins that).

Known edges: a browser without Web Locks (``navigator.locks``) edits the outbox unlocked, so two tabs of it can each
overwrite the other's change; the page says "one tab at a time" there. iOS Safari ships Web Locks from 15.4
[judged by coordphone-fix2, 2026-10-23, against the head's ruling 4].
"""
from __future__ import annotations

import base64
import secrets
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = ["DROPS_SHOWN", "REVIEWS_KEPT", "REVIEW_CLEARED_SHOWN", "REVIEW_OPEN_SHOWN", "REVIEW_TTL", "CoordPhone",
           "CoordRefused"]

# phone drops a listing shows, newest first
DROPS_SHOWN = 10
# review items a listing shows: the open ones (oldest first, as the queue lists them) and the newest cleared ones
REVIEW_OPEN_SHOWN = 50
REVIEW_CLEARED_SHOWN = 10
# repos the sheet suggests for an envelope's "where" (the store's own threads and drop sources)
_REPOS_SHOWN = 20
_DROP_KEYS = frozenset({"words", "outbox_id", "sent_at", "store_id"})
_ENVELOPE_KEYS = frozenset({"body_text", "payload", "authenticator_data", "client_data_json", "signature"})
_REVIEW_KEYS = frozenset({"words", "outbox_id", "spec", "signer_fp", "store_id"})
_CHALLENGE_KEYS = frozenset({"review_id"})
_ACTIONS = ("drop", "envelope-review", "envelope-challenge")
# a reviewed body is kept this long for its Sign, and at most this many at once (oldest dropped first)
REVIEW_TTL = timedelta(minutes=15)
REVIEWS_KEPT = 32
# what the phone sheet may set in an envelope (pushes and publishes stay the laptop's: a push's url is resolved there)
_SPEC_KEYS = frozenset({"where", "lands", "budget", "stop"})


class CoordRefused(Exception):
    """A refusal a coordinator route answers with: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class CoordPhone:
    """The coordinator routes of one phone port, over the store in ``dispatch_dir``, for passkeys made on ``rp_id``.
    ``keys`` is the server's callable over the active passkeys of the one cockpit its sign requests use. ``launcher``
    starts a woken coordinator launch; it is :func:`levain.coordinator.launch.launch_session` (what ``levain drop``
    calls) unless a test hands in another."""

    def __init__(self, dispatch_dir: Path | str, rp_id: str, page_origin: str | None,
                 keys: Callable[[], dict[str, Any]], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 launcher: Callable[[Any, str], Any] | None = None) -> None:
        from levain.coordinator.launch import launch_session

        self.dispatch_dir = Path(dispatch_dir)
        self.rp_id = rp_id
        self.page_origin = page_origin
        self._keys = keys
        self._clock = clock
        self._launcher = launch_session if launcher is None else launcher
        self._lock = threading.Lock()
        self._reviews: dict[str, dict[str, Any]] = {}
        self._wakes: list[threading.Thread] = []

    # --- the store ---------------------------------------------------------------------------------------------------
    def _store(self, *, create: bool) -> Any:
        """The coordinator store; None when the directory holds none and ``create`` is False (a read never makes one)."""
        from levain.coordinator.envelopes import EnvelopeStore
        from levain.dispatch.jobs import DB_NAME

        if not create:
            try:
                (self.dispatch_dir / DB_NAME).lstat()
            except FileNotFoundError:
                return None
        return EnvelopeStore(self.dispatch_dir, clock=self._clock)

    def _signers(self) -> tuple[list[str], str]:
        """The passkeys that may sign an envelope here (class covering the envelope's floor), and a note when none."""
        from levain.authority import AuthorityError
        from levain.consent.queue import class_covers
        from levain.consent.sign import SignRefused
        from levain.coordinator.envelope import SIGN_TIER

        try:
            keys = self._keys()
        except (SignRefused, AuthorityError, OSError, sqlite3.Error) as exc:
            return [], str(exc)
        ok = sorted(fp for fp, k in keys.items() if class_covers(k.key_class, SIGN_TIER))
        if not ok:
            return [], (f"no passkey enrolled for {self.rp_id} covers {SIGN_TIER}: send the drop plain, or sign its "
                        "envelope from the laptop (levain drop --envelope)")
        return ok, ""

    def _key(self, fp: Any) -> Any:
        from levain.consent.queue import class_covers
        from levain.coordinator.envelope import SIGN_TIER

        key = self._keys().get(fp) if isinstance(fp, str) else None
        if key is None:
            raise CoordRefused(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for {self.rp_id}")
        if not class_covers(key.key_class, SIGN_TIER):
            raise CoordRefused(422, "sign_refused", f"a {key.key_class} passkey does not cover {SIGN_TIER}: sign the "
                                                    "envelope from the laptop")
        return key

    # --- the listing -------------------------------------------------------------------------------------------------
    @staticmethod
    def _receipt(r: Any) -> dict[str, Any]:
        from levain.cockpit.display_safe import visible

        b = r.body
        out = {"stage": b.get("stage"), "at": r.at, "envelope_refused": visible(b.get("envelope_refused")),
               "threads": [{"thread_id": t.get("thread_id"), "kind": t.get("kind"), "title": visible(t.get("title"))}
                           for t in b.get("threads", []) if isinstance(t, dict)]}
        if b.get("stage") == "noted":          # a drop read with nothing to do (levain drops note): no threads, a why
            out.update(why=visible(b.get("why")))
        if b.get("stage") == "woken":          # how the coordinator launch this drop asked for went (record_wake)
            out.update(launch_id=b.get("launch_id"), started=b.get("started") is True, said=visible(b.get("said")))
        return out

    def drop_record(self, store: Any, drop_id: str) -> dict[str, Any]:
        """One drop as the sheet shows it: its words (display-safe), when, its outbox id, its envelope, every drop
        receipt (delivered, split, resplit) and the threads it is in now."""
        from levain.cockpit.display_safe import visible

        drop, threads, receipts = store.drop_view(drop_id)
        refused = next((r.body["envelope_refused"] for r in receipts
                        if r.body.get("stage") == "delivered" and "envelope_refused" in r.body), None)
        return {"drop_id": drop.drop_id, "outbox_id": drop.outbox_id, "surface": drop.surface,
                "words": visible(drop.words), "sent_at": drop.sent_at, "received_at": drop.received_at,
                "envelope_id": drop.envelope_id, "envelope_refused": visible(refused),
                "receipts": [self._receipt(r) for r in receipts],
                "threads": [{"thread_id": t.thread_id, "kind": t.kind, "state": t.state, "title": visible(t.title)}
                            for t in threads]}

    def listing(self) -> dict[str, Any]:
        """The ``coord_*`` keys of ``/consent.json``: the newest phone drops with their receipts, the repos an
        envelope may name, and who may sign one. A store that cannot be read is ``coord_note``, never an empty list
        alone; no store yet is no drops and no note."""
        signers, signer_note = self._signers()
        # coord_store_read False: the identity is unknown, and the page sends nothing until it reads one
        out: dict[str, Any] = {"coord_drops": [], "coord_note": None, "coord_repos": [], "coord_store_id": None,
                               "coord_store_read": False, "coord_signers": signers, "coord_signer_note": signer_note}
        try:
            store = self._store(create=False)
            if store is None:
                out["coord_store_read"] = True
                return out
            out["coord_store_id"] = store.store_id()
            out["coord_store_read"] = True
            drops = [d for d in store.drops(limit=200) if d.surface == "phone"][:DROPS_SHOWN]
            out["coord_drops"] = [self.drop_record(store, d.drop_id) for d in drops]
            repos: list[str] = []
            for t in store.threads():
                if t.repo and t.repo not in repos:
                    repos.append(t.repo)
            for d in store.drops(limit=200):
                if d.source.repo and d.source.repo not in repos:
                    repos.append(d.source.repo)
            out["coord_repos"] = repos[:_REPOS_SHOWN]
        except Exception as exc:  # noqa: BLE001 - any store fault is the listing's note, never the page's failure
            out["coord_store_id"], out["coord_store_read"] = None, False
            out["coord_note"] = f"the coordinator store cannot be read: {type(exc).__name__}: {str(exc)[:200]}"
        return out

    def coord_listing(self) -> dict[str, Any]:
        """``GET /coord.json``: the review queue (``coord_review``) and the morning card (``coord_morning``), each with a
        ``*_note`` when it cannot be read, so a queue or card that fails is never an empty panel. The page asks for it
        only while the Review tab is shown. The queue is read ONCE and both views are made from that read."""
        from levain.autonomic.durable_sqlite import read_transaction
        from levain.coordinator import review
        from levain.coordinator.store import StoreAbsent

        out: dict[str, Any] = {"coord_review": None, "coord_review_note": None,
                               "coord_morning": None, "coord_morning_note": None}
        try:
            store = self._store(create=False)
            if store is None:
                return out
            # the count and the two bounded pages: the queue keeps every item it ever held, and a view reads only what it
            # shows. The card is made inside the same read transaction, so the response is one snapshot of the store
            with read_transaction(store.open_readonly) as conn:
                page = review.queue_page(conn, open_limit=REVIEW_OPEN_SHOWN, cleared_limit=REVIEW_CLEARED_SHOWN)
                # each is its own note: a card that cannot be read leaves the queue shown
                for key, make in (("coord_review", lambda: self.review_view(store, page)),
                                  ("coord_morning", lambda: self.morning_view(store, page, conn))):
                    try:
                        out[key] = make()
                    except Exception as exc:  # noqa: BLE001 - shown as the panel's note, never the page's failure
                        out[key + "_note"] = f"cannot be read: {type(exc).__name__}: {str(exc)[:200]}"
        except StoreAbsent:          # the file holds no current coordinator tables yet: nothing to show, nothing made
            return {**out, "coord_review": None, "coord_morning": None}
        except Exception as exc:  # noqa: BLE001 - shown as both panels' note, never the page's failure
            note = f"cannot be read: {type(exc).__name__}: {str(exc)[:200]}"
            return {"coord_review": None, "coord_review_note": note, "coord_morning": None, "coord_morning_note": note}
        return out

    def review_view(self, store: Any, page: Any) -> dict[str, Any]:
        """The review queue as the phone shows it, from ``page`` (one :func:`levain.coordinator.review.queue_page`
        read): ``{"open": [...], "open_total", "cleared": [...], "now"}``, an item
        ``{item_id, feed, label, authority_relevant, text, at, state, cleared_at, version}`` with its text display-safe.
        ``version`` is the row version the cockpit's queue panel draws (:func:`levain.cockpit.coordinator_panels.
        review_version`): a Clear or Reopen sends it back, and the kernel refuses it once the item has moved."""
        from levain.cockpit.coordinator_panels import review_version
        from levain.cockpit.display_safe import visible
        from levain.cockpit.consent import utc_stamp
        from levain.coordinator import review

        def row(i: Any) -> dict[str, Any]:
            return {"item_id": i.item_id, "feed": i.feed, "label": review.FEED_LABELS.get(i.feed, i.feed),
                    "authority_relevant": i.authority_relevant,
                    "text": visible(str(review.body_fields(i).get("text", ""))[:300]), "at": i.at, "state": i.state,
                    "cleared_at": i.last[3] if i.state == "cleared" and i.last is not None else None,
                    "version": review_version(i)}

        return {"open": [row(i) for i in page.open], "open_total": page.open_total,
                "cleared": [row(i) for i in page.cleared], "now": utc_stamp(self._clock())}

    def morning_view(self, store: Any, page: Any, conn: Any = None) -> dict[str, Any]:
        """The coordinator's morning card as the phone shows it: its own counts and its own ``render`` lines,
        display-safe. The card is made from ``page`` (the one queue read the queue view is made from), so the card's
        count and the queue's open total cannot disagree."""
        from levain.coordinator import morning
        from levain.cockpit.display_safe import visible

        c = morning.card(store, items=page.open, review_total=page.open_total, conn=conn)
        w = c["waiting"]
        # a section the card could not read is None, never the empty list it fell back to (card() records it in errors)
        unread = {e.split(":", 1)[0] for e in c["errors"]}
        return {"at": c["at"], "lines": visible(morning.render(c)).split("\n"),
                "away": [a for a, s in c["away"].items() if s.get("away")],
                "away_unknown": [a for a, s in c["away"].items() if s.get("away") is None],
                "undecided": None if "undecided" in unread else len(c["undecided"]),
                "budgets_crossed": sum(1 for b in c["budgets"] if b["refusal"] and not b["unreadable"]),
                "budgets_unreadable": sum(1 for b in c["budgets"] if b["unreadable"]),
                "budgets": len(c["budgets"]),
                "waiting": sum(len(v) for v in w["threads"].values()) + len(w["loose"]),
                "waiting_unreadable": len(w["unresolved"]), "review_open": c["review_total"], "errors": c["errors"]}

    # --- the routes --------------------------------------------------------------------------------------------------
    def step(self, action: str, req: Any, *, device: str | None) -> dict[str, Any]:
        """``drop`` {words, outbox_id, sent_at, store_id[, envelope]} -> the drop delivered (or the stored one, on a
        resend); ``envelope-review`` {words, outbox_id, spec, signer_fp, store_id} -> the body to sign, kept under a
        review id; ``envelope-challenge`` {review_id} -> its payload, signed_at now, and the WebAuthn challenge.
        ``device`` is the
        paired device the request's token belongs to (None for any other credential: refused). Raises
        :class:`CoordRefused`: 400 a malformed request, 403 no paired device, 409 an outbox id already used otherwise or
        another store's item, 422 a refusal, 503 a fault on this machine (nothing written)."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.consent.passkey import PasskeyError
        from levain.consent.sign import SignRefused
        from levain.coordinator.envelope import EnvelopeError
        from levain.coordinator.envelopes import EnvelopeConflict, EnvelopeStale
        from levain.coordinator.store import CoordError, DropConflict
        from levain.dispatch.cli import CockpitUnnamed
        from levain.dispatch.jobs import JobStoreError

        if action not in _ACTIONS:
            raise CoordRefused(404, "not_found", "no such coordinator action")
        if not isinstance(device, str) or not device:
            raise CoordRefused(403, "device_required", "a drop is sent from a paired device (this run's phone link "
                                                       "opens the queue only): pair this phone, then send it again")
        if not isinstance(req, dict):
            raise CoordRefused(400, "bad_request", "the request is a JSON object")
        not_done = "the drop was not delivered" if action == "drop" else "nothing was signed"
        try:
            if action == "envelope-review":
                return self._review(req, device)
            if action == "envelope-challenge":
                return self._challenge(req, device)
            return self._drop(req)
        except (DropConflict, EnvelopeConflict) as exc:
            raise CoordRefused(409, "conflict", f"{exc}: {not_done}") from None
        except EnvelopeStale as exc:
            raise CoordRefused(422, "envelope-stale", f"{exc}: {not_done}") from None
        except SignRefused as exc:
            raise CoordRefused(503, "unavailable", f"the passkeys cannot be read now ({str(exc)[:200]}): "
                                                   f"{not_done}") from None
        except CockpitUnnamed as exc:
            raise CoordRefused(503, "unavailable", f"{exc.door}: {not_done}") from None
        except PasskeyError as exc:
            raise CoordRefused(422, "sign_refused", f"{exc}: {not_done}") from None
        except (CoordError, EnvelopeError) as exc:
            raise CoordRefused(422, "refused", f"{exc}: {not_done}") from None
        except (VerifierUnavailable, AuthorityError, JobStoreError, OSError, sqlite3.Error) as exc:
            raise CoordRefused(503, "unavailable", f"the coordinator store cannot answer now ({type(exc).__name__}: "
                                                   f"{str(exc)[:200]}): {not_done}") from None

    def _spec(self, spec: Any) -> dict[str, Any]:
        """The sheet's spec as the CLI builder takes it: where (absolute repos), lands always named and inferred."""
        from levain.coordinator.store import CoordError

        if not isinstance(spec, dict) or not set(spec) <= _SPEC_KEYS:
            raise CoordError(f"an envelope from the phone sets only {sorted(_SPEC_KEYS)}")
        where = spec.get("where", [])
        lands = spec.get("lands", [])
        if not isinstance(where, list) or not isinstance(lands, list):
            raise CoordError("where and lands are lists")
        for w in where:
            if not isinstance(w, dict) or not isinstance(w.get("repo"), str) or not w["repo"].startswith("/"):
                raise CoordError('each where entry is {"repo": "/an/absolute/path", "paths": [...]}')
        out_lands = []
        for land in lands:
            if not isinstance(land, dict) or not set(land) <= {"repo", "onto", "criteria"} \
                    or not isinstance(land.get("repo"), str) or not land["repo"].startswith("/") \
                    or not isinstance(land.get("onto"), str):
                raise CoordError('each land is {"repo": "/an/absolute/path", "onto": "<branch>"}')
            # the phone records no source, so where a land goes is never "where you dropped from" (C0 §4.3)
            out_lands.append({"repo": land["repo"], "onto": land["onto"], "from": "named", "onto_from": "inferred",
                              "criteria": land.get("criteria", [])})
        return {**{k: spec[k] for k in ("budget", "stop") if k in spec}, "where": where, "lands": out_lands}

    def _review(self, req: dict[str, Any], device: str) -> dict[str, Any]:
        from levain.contracts.canon import canonical
        from levain.coordinator import cli as ccli
        from levain.coordinator import envelope as env
        from levain.coordinator.store import CoordError, Source, check_words

        if set(req) != _REVIEW_KEYS:
            raise CoordRefused(400, "bad_request", f"request keys must be {sorted(_REVIEW_KEYS)}")
        words = check_words(req["words"])          # a drop the store would refuse is never signed for
        key = self._key(req["signer_fp"])
        if not isinstance(req["outbox_id"], str):
            raise CoordError("an outbox id is text")
        if req["store_id"] is not None and not isinstance(req["store_id"], str):
            raise CoordRefused(400, "bad_request", "store_id is the store identity the listing reported, or null")
        # the drop's own identity rule (L3 r3 codex HIGH): a page that read another store is refused here, and None
        # passes only where there is no store, which this call then creates, so created is exactly "store_id was None"
        store = self._bound_store(req["store_id"])
        created = req["store_id"] is None
        if (sent := store.drop_by_outbox(req["outbox_id"])) is not None:
            # a resend returns the stored drop as it was, so an envelope signed now would never be read
            raise CoordRefused(409, "conflict", f"this drop is already delivered as {sent.drop_id}; nothing new is "
                                                "signed for it")
        body = ccli._spec_body(self._spec(req["spec"]), words, Source(), store)
        text = canonical(body)
        body = store.check_body(text)          # a body the store can only refuse is never put to the passkey
        review_id = "rv-" + secrets.token_hex(16)
        # the store this review was bound to, after it was made if this review made it: the challenge binds to this id
        # (L3 r4, both seats), never to one a later request names
        bound = store.store_id()
        now = self._clock()
        with self._lock:
            for rid in [r for r, v in self._reviews.items() if v["until"] <= now]:
                del self._reviews[rid]
            while len(self._reviews) >= REVIEWS_KEPT:
                del self._reviews[next(iter(self._reviews))]
            self._reviews[review_id] = {"device": device, "body": body, "outbox_id": req["outbox_id"],
                                        "signer_fp": key.fingerprint, "store_id": bound,
                                        "until": now + REVIEW_TTL}
        return {"review_id": review_id, "body_text": text, "digest": env.envelope_digest(body), "rp_id": self.rp_id,
                "view": ccli._view(body, store, words=words).split("\n"),
                "store_id": bound, "store_created": created}

    def _challenge(self, req: dict[str, Any], device: str) -> dict[str, Any]:
        """Sign tapped: the payload over the KEPT body, signed_at now (L3 r1 codex MED: a long read of the sheet is
        never a stale signature)."""
        from levain.consent.passkey import challenge
        from levain.coordinator import envelope as env

        if set(req) != _CHALLENGE_KEYS or not isinstance(req["review_id"], str):
            raise CoordRefused(400, "bad_request", f"request keys must be {sorted(_CHALLENGE_KEYS)}")
        now = self._clock()
        with self._lock:
            kept = self._reviews.get(req["review_id"])
        if kept is None or kept["until"] <= now or kept["device"] != device:
            raise CoordRefused(409, "review_gone", "this envelope's review is no longer held here (it expired, or the "
                                                   "phone port restarted): review it again, then sign")
        key = self._key(kept["signer_fp"])         # still an active passkey covering the envelope's floor
        # the review's own store (L3 r4, both seats): one replaced since is other_store and one gone is other_store too,
        # since the kept id is never None and _bound_store creates a store only for None; the challenge never makes one
        store = self._bound_store(kept["store_id"])
        if (sent := store.drop_by_outbox(kept["outbox_id"])) is not None:
            raise CoordRefused(409, "conflict", f"this drop is already delivered as {sent.drop_id}; nothing new is "
                                                "signed for it")
        payload = env.sign_payload(kept["body"], signer_fp=key.fingerprint, signed_at=now)
        return {"payload": payload, "rp_id": self.rp_id,
                "challenge": _b64u(challenge(env.sign_message(payload), namespace=env.SIGN_NAMESPACE)),
                "store_id": store.store_id(), "store_created": False}

    def _drop(self, req: dict[str, Any]) -> dict[str, Any]:
        from levain.consent.passkey import b64url_decode, to_sshsig
        from levain.coordinator import envelope as env
        from levain.coordinator.store import Source
        from levain.dispatch.cli import _authority_source

        signed = req.get("envelope")
        if set(req) - {"envelope"} != _DROP_KEYS or (signed is not None and not isinstance(signed, dict)):
            raise CoordRefused(400, "bad_request", f"request keys must be {sorted(_DROP_KEYS)} and an optional "
                                                   "envelope")
        if not isinstance(req["sent_at"], str) or not isinstance(req["outbox_id"], str):
            raise CoordRefused(400, "bad_request", "sent_at is a timestamp and outbox_id is text")
        if req["store_id"] is not None and not isinstance(req["store_id"], str):
            raise CoordRefused(400, "bad_request", "store_id is the store identity the listing reported, or null")
        # a resend first, with nothing else checked, the store identity included: a delivered drop is answered as
        # delivered, whatever has happened to its carrier since (L3 r1 codex MED), and a first drop that made the
        # store and lost its reply gets its receipt though it is still bound to no store (L3 r2 codex MED). A match
        # writes nothing; everything that writes is behind _bound_store
        if (here := self._store(create=False)) is not None \
                and (again := here.resent_drop(req["outbox_id"], req["words"], surface="phone")) is not None:
            return self._delivered(here, again, False, None if signed is None else signed.get("body_text"))
        store = self._bound_store(req["store_id"])
        if signed is None:
            drop, created = store.accept_drop(req["words"], surface="phone", outbox_id=req["outbox_id"],
                                              sent_at=req["sent_at"], source=Source())
            return self._delivered(store, drop, created, None)
        if set(signed) != _ENVELOPE_KEYS or not isinstance(signed["payload"], dict):
            raise CoordRefused(400, "bad_request", f"an envelope carries {sorted(_ENVELOPE_KEYS)}")
        key = self._key(signed["payload"].get("signer_fp"))
        signature = to_sshsig(key.public_key, b64url_decode(signed["authenticator_data"], "authenticator_data"),
                              b64url_decode(signed["client_data_json"], "client_data_json"),
                              b64url_decode(signed["signature"], "signature"),
                              namespace=env.SIGN_NAMESPACE, page_origin=self.page_origin)
        # the CLI's own authority source (levain.coordinator.cli._snapshot with no --data-root), read right before the
        # write: a key revoked while the phone waited signs nothing
        authority = _authority_source(store.directory, None)()
        # keep_plain=False: an envelope the store would not take writes nothing (the outbox still holds the words)
        drop, created = store.accept_drop(req["words"], surface="phone", outbox_id=req["outbox_id"],
                                          sent_at=req["sent_at"], source=Source(),
                                          envelope=(signed["body_text"], signed["payload"], signature),
                                          authority=authority, keep_plain=False)
        return self._delivered(store, drop, created, signed["body_text"])

    def _bound_store(self, store_id: str | None) -> Any:
        """The store, only when it is the one the outbox item was made against (L3 r1 codex HIGH). An item made while
        there was no store (``store_id`` None) creates it, as ``levain drop`` does; an item bound to a store never
        creates a new one. The envelope review passes the same rule, with the identity the page read (L3 r3)."""
        from levain.coordinator.envelopes import EnvelopeStore

        store = self._store(create=False)
        here = None if store is None else store.store_id()
        if store_id != here:
            raise CoordRefused(409, "other_store", (
                f"this drop was made for another Levain (store {str(store_id)[:40]}), not this one "
                f"({here or 'no store yet'}): it is not sent here"))
        return store if store is not None else EnvelopeStore(self.dispatch_dir, clock=self._clock)

    def _delivered(self, store: Any, drop: Any, created: bool, body_text: str | None) -> dict[str, Any]:
        # only a drop this call created can name a launch (woken_launch is read once, and only an insert sets it)
        woken = store.woken_launch(drop.drop_id) if created else None
        out = {"created": created, "drop": self.drop_record(store, drop.drop_id), "envelope": None,
               "woken": woken, "store_id": store.store_id()}
        if woken is not None:
            self._wake(drop.drop_id, woken)
        if body_text is not None:
            from levain.coordinator import envelope as env

            try:
                wanted = env.parse_body(body_text)["envelope_id"]
            except (env.EnvelopeError, TypeError, ValueError, KeyError):
                wanted = None
            taken = drop.envelope_id is not None and drop.envelope_id == wanted
            refused = None
            if not taken:
                refused = (out["drop"]["envelope_refused"]
                           or ("this drop was delivered before, without this envelope" if not created
                               else "the envelope was not taken"))
            out["envelope"] = {"taken": taken, "envelope_id": drop.envelope_id if taken else None,
                               "refused": refused}
        return out

    # --- the wake ----------------------------------------------------------------------------------------------------
    def _wake(self, drop_id: str, launch_id: str) -> None:
        """Start ``launch_id`` with the CLI's own call on a daemon thread (a spawn may block; the reply never waits on
        it), and write how it went as the drop's ``woken`` receipt."""
        def run() -> None:
            try:
                store = self._store(create=True)
            except Exception:  # noqa: BLE001 - nowhere to record it; the launch stays pending until LAUNCH_TTL
                return
            try:
                got = self._launcher(store, launch_id)
                started = bool(got.started)
                said = (got.attach or "started") if started else (got.refusal or "not started")
                if not started and getattr(got, "remedy", None):
                    said += f"; run this once on the laptop: {got.remedy}"
            except Exception as exc:  # noqa: BLE001 - a failed start is the receipt's words, never the drop's failure
                started, said = False, f"not started: {type(exc).__name__}: {str(exc)[:200]}"
            try:
                store.record_wake(drop_id, launch_id, started=started, said=said)
            except Exception:  # noqa: BLE001 - the drop is delivered either way
                pass

        try:
            t = threading.Thread(target=run, name=f"levain-coord-wake-{launch_id}", daemon=True)
            with self._lock:
                self._wakes = [w for w in self._wakes if w.is_alive()] + [t]
            t.start()
        except Exception as exc:  # noqa: BLE001 - the drop has committed: a start that fails is its receipt (L3 r2)
            with self._lock:
                self._wakes = [w for w in self._wakes if w.is_alive()]
            try:
                self._store(create=True).record_wake(
                    drop_id, launch_id, started=False,
                    said=f"not started: the wake could not start its thread: {type(exc).__name__}: {str(exc)[:200]}")
            except Exception:  # noqa: BLE001 - the drop is delivered either way; the launch stays pending
                pass

    def wait_wakes(self, timeout: float) -> None:
        """Wait for the wakes started so far (tests, and a server shutting down)."""
        with self._lock:
            wakes = list(self._wakes)
        for w in wakes:
            w.join(timeout)
