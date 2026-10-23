"""levain.coordinator.phone — the phone's drop sheet: a drop from the phone, its outbox, and an envelope signed with Face ID.

The phone port (:mod:`levain.phone_server`) carries the coordinator's phone half (C1/C2, BUILD_PLAN rows C1 and C2):

- **A drop** (``POST /coord/drop`` {words, outbox_id, sent_at}) goes into the SAME store call ``levain drop`` makes
  (:meth:`levain.coordinator.envelopes.EnvelopeStore.accept_drop`), with ``surface="phone"``. There is no second write
  path: the drop, its ``delivered`` drop receipt and any coordinator wake are that call's one transaction.
- **The outbox** (gap 6): the page keeps a drop until this route answers 2xx. The page picks the ``outbox_id`` once,
  when the drop is made, and resends it unchanged; the store's outbox rule makes a resend of a delivered id a no-op that
  returns the stored drop (``created: false``), and the same id with other words a conflict (409). So a retry after a
  lost reply never files twice, and the 2xx IS the delivered receipt.
- **A structured envelope signed at send** (T4, C0 §4): ``POST /coord/envelope-challenge`` {words, outbox_id, spec,
  signer_fp} builds the body with the CLI's own builder (:func:`levain.coordinator.cli._spec_body`, the one ``levain
  drop --envelope`` signs), holds it to the store's disk checks (``check_body``) and answers its canonical text, its
  digest, the plain-words view the CLI prints before signing (:func:`levain.coordinator.cli._view`), the payload
  (:func:`levain.coordinator.envelope.sign_payload`) and the WebAuthn challenge over that payload in
  :data:`~levain.coordinator.envelope.SIGN_NAMESPACE`. The drop then carries {body_text, payload, assertion}; the server
  turns the assertion into an SSHSIG (:func:`levain.consent.passkey.to_sshsig`) and hands body, payload and signature
  to the same ``accept_drop(envelope=...)`` the CLI calls, verified against the same authority source the CLI's
  :func:`levain.coordinator.cli._snapshot` reads (:func:`levain.dispatch.cli._authority_source` over the store's
  directory). No verification code lives here.

A land from the phone is always ``from: "named"``, ``onto_from: "inferred"``: the phone sheet has no repo/branch picker
yet, so the drop records no source, and C0 §4.3 allows ``from: "source"`` only for a drop that recorded one. The body
the passkey signs carries that ``inferred`` mark, and the view says it in words (C0 §1, Phill 10-20: *"for 4 I'll take
your recs across all items"*, item 1).

The page's token only opens the routes: an envelope's authority is the passkey's assertion, which the store verifies.
The listing (:meth:`CoordPhone.listing`) never creates the store; a drop does, exactly as ``levain drop`` does. Stdlib
only.
"""
from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["DROPS_SHOWN", "CoordPhone", "CoordRefused"]

# phone drops a listing shows, newest first
DROPS_SHOWN = 10
# repos the sheet suggests for an envelope's "where" (the store's own threads and drop sources)
_REPOS_SHOWN = 20
_DROP_KEYS = frozenset({"words", "outbox_id", "sent_at"})
_ENVELOPE_KEYS = frozenset({"body_text", "payload", "authenticator_data", "client_data_json", "signature"})
_CHALLENGE_KEYS = frozenset({"words", "outbox_id", "spec", "signer_fp"})
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
    ``keys`` is the server's callable over the active passkeys of the one cockpit its sign requests use."""

    def __init__(self, dispatch_dir: Path | str, rp_id: str, page_origin: str | None,
                 keys: Callable[[], dict[str, Any]], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.dispatch_dir = Path(dispatch_dir)
        self.rp_id = rp_id
        self.page_origin = page_origin
        self._keys = keys
        self._clock = clock

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
        return {"stage": b.get("stage"), "at": r.at, "envelope_refused": visible(b.get("envelope_refused")),
                "threads": [{"thread_id": t.get("thread_id"), "kind": t.get("kind"), "title": visible(t.get("title"))}
                            for t in b.get("threads", []) if isinstance(t, dict)]}

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
        out: dict[str, Any] = {"coord_drops": [], "coord_note": None, "coord_repos": [],
                               "coord_signers": signers, "coord_signer_note": signer_note}
        try:
            store = self._store(create=False)
            if store is None:
                return out
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
            out["coord_note"] = f"the coordinator store cannot be read: {type(exc).__name__}: {str(exc)[:200]}"
        return out

    # --- the routes --------------------------------------------------------------------------------------------------
    def step(self, action: str, req: Any) -> dict[str, Any]:
        """``drop`` {words, outbox_id, sent_at[, envelope]} -> the drop delivered (or the stored one, on a resend);
        ``envelope-challenge`` {words, outbox_id, spec, signer_fp} -> the body to sign and its WebAuthn challenge.
        Raises :class:`CoordRefused`: 400 a malformed request, 409 an outbox id already used otherwise, 422 a
        refusal, 503 a fault on this machine (nothing written)."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.consent.passkey import PasskeyError
        from levain.coordinator.envelope import EnvelopeError
        from levain.coordinator.store import CoordError, DropConflict
        from levain.dispatch.cli import CockpitUnnamed
        from levain.dispatch.jobs import JobStoreError

        if action not in ("drop", "envelope-challenge"):
            raise CoordRefused(404, "not_found", "no such coordinator action")
        if not isinstance(req, dict):
            raise CoordRefused(400, "bad_request", "the request is a JSON object")
        not_done = "nothing was signed" if action == "envelope-challenge" else "the drop was not delivered"
        try:
            if action == "envelope-challenge":
                return self._challenge(req)
            return self._drop(req)
        except DropConflict as exc:
            raise CoordRefused(409, "conflict", f"{exc}") from None
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

    def _challenge(self, req: dict[str, Any]) -> dict[str, Any]:
        from levain.consent.passkey import challenge
        from levain.contracts.canon import canonical
        from levain.coordinator import cli as ccli
        from levain.coordinator import envelope as env
        from levain.coordinator.store import CoordError, Source, check_words

        if set(req) != _CHALLENGE_KEYS:
            raise CoordRefused(400, "bad_request", f"request keys must be {sorted(_CHALLENGE_KEYS)}")
        words = check_words(req["words"])          # a drop the store would refuse is never signed for
        key = self._key(req["signer_fp"])
        if not isinstance(req["outbox_id"], str):
            raise CoordError("an outbox id is text")
        store = self._store(create=True)
        if (sent := store.drop_by_outbox(req["outbox_id"])) is not None:
            # a resend returns the stored drop as it was, so an envelope signed now would never be read
            raise CoordRefused(409, "conflict", f"this drop is already delivered as {sent.drop_id}; nothing new is "
                                                "signed for it")
        body = ccli._spec_body(self._spec(req["spec"]), words, Source(), store)
        text = canonical(body)
        body = store.check_body(text)          # a body the store can only refuse is never put to the passkey
        payload = env.sign_payload(body, signer_fp=key.fingerprint, signed_at=self._clock())
        return {"body_text": text, "digest": env.envelope_digest(body), "payload": payload, "rp_id": self.rp_id,
                "view": ccli._view(body, store, words=words).split("\n"),
                "challenge": _b64u(challenge(env.sign_message(payload), namespace=env.SIGN_NAMESPACE))}

    def _drop(self, req: dict[str, Any]) -> dict[str, Any]:
        from levain.consent.passkey import b64url_decode, to_sshsig
        from levain.coordinator import envelope as env
        from levain.coordinator.store import Source
        from levain.dispatch.cli import _authority_source

        signed = req.get("envelope")
        if set(req) - {"envelope"} != _DROP_KEYS or ("envelope" in req and not isinstance(signed, dict)):
            raise CoordRefused(400, "bad_request", f"request keys must be {sorted(_DROP_KEYS)} and an optional "
                                                   "envelope")
        if not isinstance(req["sent_at"], str):
            raise CoordRefused(400, "bad_request", "sent_at is a timestamp")
        store = self._store(create=True)
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
        drop, created = store.accept_drop(req["words"], surface="phone", outbox_id=req["outbox_id"],
                                          sent_at=req["sent_at"], source=Source(),
                                          envelope=(signed["body_text"], signed["payload"], signature),
                                          authority=authority)
        return self._delivered(store, drop, created, signed["body_text"])

    def _delivered(self, store: Any, drop: Any, created: bool, body_text: str | None) -> dict[str, Any]:
        # a coordinator launch this drop's transaction asked for is named, never started by the phone port: the
        # laptop's `levain coord wake` (or its next CLI drop) starts it [judged by coordphone, 10-22, against the
        # CLI's _launch_woken spawning a session in the caller's process]
        out = {"created": created, "drop": self.drop_record(store, drop.drop_id), "envelope": None,
               "woken": store.woken_launch(drop.drop_id)}
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
