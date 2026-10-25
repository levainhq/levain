"""levain.consent.grant_phone — the phone's grants: each standing grant, and a passkey revoke of one.

The phone port (:mod:`levain.phone_server`) lists every grant the install's cockpit holds (:meth:`GrantPhone.listing`,
:func:`levain.consent.grants.grant_view` without its terminal line, plus the passkeys that may revoke) and carries one
passkey ceremony (:meth:`GrantPhone.step`): ``revoke-challenge`` then ``revoke``.

``revoke-challenge`` names the grant's ``instance`` as the listing showed it, and is refused ``grant_changed`` when
the grant under that id is no longer that one (renewed, or revoked and made again). It builds the ``revoke_grant``
link on the server (:func:`levain.consent.grants.prepare_revoke`, judged by every rule of the log but the signature,
on the head the instance was checked on) and HOLDS it, one per grant, for :data:`HOLD`; it answers with the
challenge (the SSHSIG signed-data blob of the link's message in the authority namespace), ``line``, the link's
record, which is exactly what is signed, and ``prev_hash``, the head it chains to (the message is ``prev_hash``, a
newline, then ``line``), so the page recomputes the challenge from what it shows. The page never sends a link: ``revoke`` takes only the assertion, converts
it (:func:`levain.consent.passkey.to_sshsig`, which checks the origin is the page's own) and appends the HELD link with
it (:func:`levain.consent.grants.append_revoke`). The log verifies the signature itself, so an assertion over any
other bytes is refused there, and a head that moved since the challenge refuses the link (the page asks again). A
held link is taken on its first use, whatever the outcome.

The page's token only opens the route: the authority is the passkey's signature, which the replay verifies. The one
link a passkey may sign in the authority log is ``revoke_grant`` of a consent or inert grant (:mod:`levain.authority`);
it only ever narrows what an agent may do. The log takes it only from a passkey made on an https name, so a phone
port on ``localhost`` lists the grants with no signer and says why. It takes one signed on that name on any port
(:func:`levain.consent.passkey.authority_signature_problem`), so a cockpit served on ``https://<name>:8443`` signs
here too, held to its own page by ``page_origin``. ``keys`` and ``root`` are the server's callables over the ONE
cockpit the queue's consent hooks name; either may raise :class:`levain.consent.sign.SignRefused`, answered with its
status. Stdlib only.
"""
from __future__ import annotations

import base64
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["HOLD", "GrantPhone", "GrantRefused", "grant_route"]

# how long a held link waits for its signature: a Face ID ceremony takes seconds
HOLD = 120.0
_ANSWER_KEYS = frozenset({"authenticator_data", "client_data_json", "signature"})
_NEED = {"revoke-challenge": frozenset({"signer_fp", "instance"}), "revoke": _ANSWER_KEYS}


class GrantRefused(Exception):
    """A refusal a grant route answers with: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def grant_route(route: str, prefix: str) -> "tuple[str, str] | None":
    """``(grant_id, action)`` for ``<prefix><id>/<action>``, else None: the phone port's ``/grant/`` and the cockpit's
    ``/cockpit/grant/``. A grant's id is its creator's choice, so the page sends encodeURIComponent(id); the decoded id
    is looked up in the log, never trusted further. Its length is the log's to bound, not the route's: any id a grant
    was made with can be revoked here."""
    if not route.startswith(prefix):
        return None
    from urllib.parse import unquote

    quoted, sep, action = route[len(prefix):].partition("/")
    gid = unquote(quoted)
    if not sep or not gid or action not in _NEED:
        return None
    return gid, action


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class GrantPhone:
    """The grant routes of one phone port, over the consent queue in ``queue_dir``, for passkeys made on ``rp_id``."""

    def __init__(self, queue_dir: Path | str, rp_id: str, page_origin: str | None,
                 keys: Callable[[], dict[str, Any]], root: Callable[[], Path], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.queue_dir = Path(queue_dir)
        self.rp_id = rp_id
        self.page_origin = page_origin
        self._keys = keys
        self._root = root
        self._clock = clock
        self._monotonic = monotonic
        self._held: dict[str, tuple[Any, str, float]] = {}
        self._lock = threading.Lock()

    def _signers(self) -> tuple[dict[str, Any], str]:
        """The passkeys that may revoke here, by fingerprint, and a note when there are none."""
        from levain.authority import AuthorityError
        from levain.consent.passkey import page_origin_problem
        from levain.consent.sign import SignRefused

        if self.rp_id == "localhost":
            return {}, ("a passkey made on localhost cannot revoke a grant (the authority log takes a passkey's revoke "
                        "only from an https page): revoke from the laptop (levain consent grant revoke)")
        if self.page_origin is not None:
            problem = page_origin_problem(self.rp_id, self.page_origin)
            if problem is not None:
                # to_sshsig refuses every assertion against such a pin, so none is asked for
                return {}, f"{problem}: revoke from the phone page or the laptop (levain consent grant revoke)"
        try:
            keys = self._keys()
        except (SignRefused, AuthorityError, OSError, sqlite3.Error) as exc:
            return {}, str(exc)
        if not keys:
            return {}, (f"no passkey is enrolled for {self.rp_id} in the cockpit the consent hook names: revoke from "
                        "the laptop (levain consent grant revoke)")
        return keys, ""

    def listing(self) -> dict[str, Any]:
        """``{"grants", "grants_note", "inert_note"}`` for ``consent.json``: each grant as the cockpit page shows it,
        minus the terminal line, with ``signers``, the passkeys that may revoke it. A fault is the note, not a raise."""
        from levain.consent.grants import INERT_NOTE, grant_view

        try:
            view = grant_view(self.queue_dir, now=self._clock())
        except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a note on the page, never a raise
            return {"grants": [], "grants_note": f"the grants cannot be read now ({type(exc).__name__}: {exc})",
                    "inert_note": INERT_NOTE}
        signers, note = self._signers() if view["grants"] else ({}, "")
        rows = []
        for row in view["grants"]:
            row = {k: v for k, v in row.items() if k != "revoke"}
            row["signers"] = sorted(signers)
            rows.append(row)
        return {"grants": rows, "grants_note": view["note"] or note, "inert_note": view["inert_note"]}

    def step(self, grant_id: str, action: str, req: Any) -> dict[str, Any]:
        """``/grant/<id>/revoke-challenge`` with ``{signer_fp, instance}``, then ``/grant/<id>/revoke`` with the
        assertion."""
        need = _NEED.get(action)
        if need is None:
            raise GrantRefused(404, "not_found", "no such route")
        if not isinstance(req, dict) or set(req) != need or not all(isinstance(req[k], str) for k in need):
            raise GrantRefused(400, "bad_request", f"the body must carry exactly {', '.join(sorted(need))}, as text")
        if action == "revoke-challenge":
            return self._challenge(grant_id, req["signer_fp"], req["instance"])
        return self._revoke(grant_id, req)

    def _challenge(self, grant_id: str, signer_fp: str, instance: str) -> dict[str, Any]:
        from levain.authority import NAMESPACE, StaleHead, VerifierUnavailable
        from levain.cockpit.keys import KeyError_
        from levain.consent.grants import GrantChanged, prepare_revoke
        from levain.consent.passkey import challenge
        from levain.consent.sign import SignRefused

        keys, note = self._signers()
        if signer_fp not in keys:
            raise GrantRefused(403, "not_a_signer", note or f"{signer_fp[:80]} is not a passkey enrolled for "
                                                           f"{self.rp_id} in the cockpit the consent hook names")
        try:
            link = prepare_revoke(self._root(), grant_id, signer_fp=signer_fp, clock=self._clock, instance=instance)
        except SignRefused as exc:
            raise GrantRefused(exc.status, "unavailable", str(exc)) from None
        except VerifierUnavailable as exc:
            raise GrantRefused(503, "unavailable", f"{exc}; nothing was signed") from None
        except StaleHead:
            raise GrantRefused(409, "stale", "the authority log moved while this revoke was asked for: ask "
                                             "again") from None
        except GrantChanged as exc:
            raise GrantRefused(409, "grant_changed", f"{exc}; nothing was signed") from None
        except KeyError_ as exc:
            word = "not_found" if str(exc).startswith("no consent grant") else "refused"
            raise GrantRefused(404 if word == "not_found" else 409, word, str(exc)) from None
        with self._lock:
            now = self._monotonic()
            # a lapsed hold is dropped here, so the held links never outgrow the grants being revoked right now
            self._held = {g: h for g, h in self._held.items() if h[2] >= now}
            self._held[grant_id] = (link, signer_fp, now + HOLD)
        # prev_hash is the head the link chains to: with ``line`` it is the whole signed message, so the page recomputes
        # the challenge from the line it shows and refuses one that is not over it
        return {"grant_id": grant_id, "rp_id": self.rp_id, "signer_fp": signer_fp, "line": link.record,
                "prev_hash": link.prev_hash, "challenge": _b64u(challenge(link.message, NAMESPACE))}

    def _revoke(self, grant_id: str, req: dict[str, str]) -> dict[str, Any]:
        from levain.authority import NAMESPACE, StaleHead, VerifierUnavailable
        from levain.cockpit.keys import KeyError_
        from levain.consent.grants import append_revoke
        from levain.consent.passkey import PasskeyError, b64url_decode, to_sshsig
        from levain.consent.sign import SignRefused

        with self._lock:
            held = self._held.pop(grant_id, None)
        if held is None or held[2] < self._monotonic():
            raise GrantRefused(409, "no_challenge", "no revoke of this grant is waiting for a signature (it was "
                                                    "used, or it lapsed): ask again")
        link, signer_fp, _deadline = held
        keys, note = self._signers()
        key = keys.get(signer_fp)
        if key is None:
            raise GrantRefused(403, "not_a_signer", note or f"{signer_fp[:80]} is no longer an enrolled passkey here")
        try:
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), namespace=NAMESPACE,
                                  page_origin=self.page_origin)
        except PasskeyError as exc:
            raise GrantRefused(400, "bad_signature", f"{exc}; nothing was revoked") from None
        try:
            seq, warning = append_revoke(self._root(), link, signature, clock=self._clock)
        except StaleHead:
            raise GrantRefused(409, "stale", "the authority log moved since this revoke was asked for: revoke "
                                             "again") from None
        except SignRefused as exc:
            raise GrantRefused(exc.status, "unavailable", str(exc)) from None
        except VerifierUnavailable as exc:
            raise GrantRefused(503, "unavailable", f"{exc}; nothing was revoked") from None
        except KeyError_ as exc:
            raise GrantRefused(403, "refused", f"{exc}; nothing was revoked") from None
        return {"revoked": grant_id, "seq": seq, "warning": warning or ""}
