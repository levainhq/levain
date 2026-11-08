"""levain.coordinator.envelope_revoke — the cockpit's envelope list and a passkey-signed revoke of one.

An envelope is a signed grant of coordinator authority (:mod:`levain.coordinator.envelopes`). Narrowing one needs no
signature at the store (``attenuate(..., "kill")``), and until now the only door to it was the terminal. This is the
cockpit's door, and it is the grant revoke's own ceremony (:class:`levain.consent.grant_phone.GrantPhone`): the same
passkeys may sign, the same pinned page, the same refusal words and statuses; the difference is only what is signed
and where it lands.

``revoke-challenge`` names the envelope's ``instance`` (its digest, as the listing showed it), builds a revoke message
on the server (store id, envelope id, the time, a nonce) and HOLDS it, one per envelope, for :data:`HOLD`. It answers
with the challenge, ``line`` (the exact text signed) and the namespace. ``revoke`` takes only the assertion, converts it
(:func:`levain.consent.passkey.to_sshsig`, held to the pinned page), verifies it with ssh-keygen against the key it
names, and only then calls ``EnvelopeStore.attenuate(id, "kill", by="operator")``. A held message is taken on its first
use, whatever the outcome, and no path here kills an envelope without a verified passkey signature.

The store is the one the Signer's queue names (the directory the phone's drops and signed envelopes are written to),
never the consent reader's ``directory``: an embedder that swaps the reader for one with no ``directory`` (flow's bridge)
does not blank this. Stdlib plus the package's own verifier.
"""
from __future__ import annotations

import base64
import logging
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority_sig import verify_link_sig
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent.grant_phone import HOLD, GrantPhone, GrantRefused
from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, StoreAbsent

_log = logging.getLogger(__name__)

__all__ = ["HOLD", "NAMESPACE", "EnvelopeRevoke", "GrantRefused", "envelope_state", "revoke_message"]

NAMESPACE = "levain-envelope-revoke"
_ID = re.compile(r"ev-[0-9a-f]{32}")
_LIST_CAP = 50
_LABEL_MAX = 90


def revoke_message(store_id: str, envelope_id: str, at: str, nonce: str) -> str:
    """The exact text a passkey signs to end one envelope."""
    return f"levain-envelope-revoke/1\nstore: {store_id}\nenvelope: {envelope_id}\nat: {at}\nnonce: {nonce}\n"


def envelope_state(stop_at: str, rows: "list[tuple[str, str]]", now: datetime) -> str:
    """``killed``, ``expired`` or ``live``, from the stored stop and the envelope-wide narrowings (``kind``, ``value``)."""
    if any(k == "kill" for k, _v in rows):
        return "killed"
    stops = [parse_stamp(stop_at)] + [parse_stamp(v) for k, v in rows if k == "stop_earlier"]
    return "expired" if min(stops) <= now else "live"


def _scope(body: dict[str, Any]) -> str:
    repos = sorted({Path(w.get("repo", "")).name for w in body.get("where", []) if isinstance(w, dict)} - {""})
    bits = [", ".join(repos[:3])] if repos else []
    launches = sum(int(x.get("count", 0)) for x in body.get("launches", []) if isinstance(x, dict))
    if launches:
        bits.append(f"{launches} launches")
    hours = (body.get("budget") or {}).get("hours")
    if isinstance(hours, int) and hours:
        bits.append(f"{hours} h")
    return " · ".join(bits)


class EnvelopeRevoke(GrantPhone):
    """The envelope list and revoke of one cockpit, over the store in ``queue_dir`` (the Signer's queue)."""

    def __init__(self, queue_dir: Path | str, rp_id: str, page_origin: "str | None", keys: Any, **kw: Any) -> None:
        super().__init__(queue_dir, rp_id, page_origin, keys, lambda: Path(queue_dir), **kw)
        self.ssh_keygen: Any = None      # None: the package's own default verifier

    def _store(self) -> EnvelopeStore:
        return EnvelopeStore(self.queue_dir)

    def _read(self) -> Any:
        """A read-only snapshot of the store: it never makes or migrates anything (a GET must not write)."""
        store = self._store()
        return read_transaction(store.open_readonly)

    def _row_of(self, conn: Any, eid: str, now: datetime) -> "dict[str, Any] | None":
        """One envelope as the list shows it, read by id. ``state`` is the envelope-wide state; a narrowing written for
        one bound thread only (``kill`` or ``thread_done``) is counted in ``ended_threads`` of ``threads``, never read as
        the whole envelope ending (it is what the coordinator's effective stop reads per thread)."""
        store = self._store()
        e = store._envelope(conn, eid)
        if e is None:
            return None
        rows = [(k, v) for k, v in conn.execute(
            "SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND thread_id IS NULL "
            "ORDER BY gseq", (eid,)).fetchall()]
        threads = conn.execute("SELECT count(*) FROM envelope_binds WHERE envelope_id = ?", (eid,)).fetchone()[0]
        ended = conn.execute(
            "SELECT count(DISTINCT thread_id) FROM envelope_attenuations WHERE envelope_id = ? AND thread_id IS NOT NULL "
            "AND kind IN ('kill', 'thread_done')", (eid,)).fetchone()[0]
        words = conn.execute("SELECT words FROM drops WHERE drop_id = ?", (e.drop_id,)).fetchone()
        label = " ".join(str(words[0]).split())[:_LABEL_MAX] if words else ""
        return {"id": eid, "label": label, "scope": _scope(e.body),
                "issued": e.body.get("issued_at", e.accepted_at), "expires": e.stop_at,
                "state": envelope_state(e.stop_at, rows, now), "instance": e.digest,
                "threads": int(threads), "ended_threads": int(ended)}

    def _rows(self) -> "list[dict[str, Any]]":
        """Every live envelope, newest first, then at most :data:`_LIST_CAP` of the ended ones: the cap bounds the
        history, never hides an envelope that can still act."""
        store = self._store()
        if not store.coordinator_present():
            return []
        now = self._clock()
        out: list[dict[str, Any]] = []
        ended = 0
        with self._read() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelopes'").fetchone() is None:
                return []
            for (eid,) in conn.execute("SELECT envelope_id FROM envelopes ORDER BY gseq DESC").fetchall():
                row = self._row_of(conn, eid, now)
                if row is None:
                    continue
                if row["state"] != "live":
                    if ended >= _LIST_CAP:
                        continue
                    ended += 1
                out.append(row)
        return out

    def listing(self) -> dict[str, Any]:
        """``{"envelopes", "note", "signers"}``: each envelope newest first with its state; ``signers`` the passkeys that
        may revoke and ``note`` why there are none. A store that cannot be read is the note, never a raise."""
        try:
            rows = self._rows()
        except Exception as exc:  # noqa: BLE001 - a store that cannot be read is a note on the page, never a raise
            return {"envelopes": [], "signers": [],
                    "note": f"the envelopes cannot be read now ({type(exc).__name__}: {str(exc)[:120]})"}
        signers, note = self._signers() if any(r["state"] == "live" for r in rows) else ({}, "")
        return {"envelopes": rows, "signers": sorted(signers), "note": note}

    def _one(self, envelope_id: str) -> "dict[str, Any]":
        """One envelope by id, whatever its age. A store that cannot be read answers 503, nothing was signed."""
        if not _ID.fullmatch(envelope_id):
            raise GrantRefused(404, "not_found", "no such envelope")
        try:
            store = self._store()
            if not store.coordinator_present():
                raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}")
            with self._read() as conn:
                row = self._row_of(conn, envelope_id, self._clock())
        except GrantRefused:
            raise
        except StoreAbsent:
            raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}") from None
        except Exception as exc:  # noqa: BLE001 - a store fault is a plain refusal, never a 500
            raise GrantRefused(503, "unavailable", f"the envelope store cannot be read ({type(exc).__name__}); "
                                                   "nothing was signed") from None
        if row is None:
            raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}")
        return row

    def _live_or_refuse(self, row: "dict[str, Any]") -> None:
        if row["state"] == "killed":
            raise GrantRefused(409, "already_ended", "this envelope is already killed; nothing was signed")
        if row["state"] == "expired":
            raise GrantRefused(409, "expired", "this envelope has already expired; nothing was signed")

    def _challenge(self, grant_id: str, signer_fp: str, instance: str) -> dict[str, Any]:
        keys, note = self._signers()
        if signer_fp not in keys:
            raise GrantRefused(403, "not_a_signer", note or f"{signer_fp[:80]} is not a passkey enrolled for "
                                                           f"{self.rp_id} in the cockpit the consent hook names")
        row = self._one(grant_id)
        self._live_or_refuse(row)
        if instance != row["instance"]:
            raise GrantRefused(409, "grant_changed", "this envelope is no longer the one the list showed; nothing "
                                                     "was signed")
        try:
            store_id = self._store().store_id()
        except Exception as exc:  # noqa: BLE001 - JobStoreError, CoordError, sqlite and OS faults are all one plain refusal
            raise GrantRefused(503, "unavailable", f"{type(exc).__name__}: {str(exc)[:160]}; nothing was signed") from None
        message = revoke_message(store_id, grant_id, utc_stamp(self._clock()), secrets.token_hex(16))
        with self._lock:
            now = self._monotonic()
            self._held = {g: h for g, h in self._held.items() if h[2] >= now}
            self._held[grant_id] = (message, signer_fp, now + HOLD)
        return {"envelope_id": grant_id, "rp_id": self.rp_id, "signer_fp": signer_fp, "line": message,
                "namespace": NAMESPACE, "challenge": _b64u_challenge(message)}

    def _revoke(self, grant_id: str, req: dict[str, str]) -> dict[str, Any]:
        from levain.authority import VerifierUnavailable

        with self._lock:
            held = self._held.pop(grant_id, None)
        if held is None or held[2] < self._monotonic():
            raise GrantRefused(409, "no_challenge", "no revoke of this envelope is waiting for a signature (it was "
                                                    "used, or it lapsed): ask again")
        message, signer_fp, _deadline = held
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
            kw = {} if self.ssh_keygen is None else {"ssh_keygen": self.ssh_keygen}
            good = verify_link_sig(message.encode("utf-8"), signature, public_key=key.public_key,
                                   fingerprint=signer_fp, namespace=NAMESPACE, **kw)
        except VerifierUnavailable as exc:
            raise GrantRefused(503, "unavailable", f"{exc}; nothing was revoked") from None
        if not good:
            raise GrantRefused(403, "refused", "the signature does not verify over this revoke; nothing was revoked")
        row = self._one(grant_id)
        self._live_or_refuse(row)
        try:
            wrote = self._store().attenuate(grant_id, "kill", by="operator")
        except CoordError as exc:
            raise GrantRefused(409, "refused", f"{str(exc)[:160]}; nothing was revoked") from None
        except Exception as exc:  # noqa: BLE001 - a store fault is a plain refusal; the write is one transaction
            raise GrantRefused(503, "unavailable", f"{type(exc).__name__}; nothing was revoked") from None
        if not wrote:
            raise GrantRefused(409, "already_ended", "this envelope is already killed; nothing was signed")
        # the attenuation row can name only the actor ("operator"); the proof that a passkey signed it is this line
        nonce = next((ln[7:] for ln in message.splitlines() if ln.startswith("nonce: ")), "")
        _log.warning("envelope %s killed by a passkey-signed revoke: signer %s, challenge nonce %s, namespace %s",
                     grant_id, signer_fp, nonce, NAMESPACE)
        return {"revoked": grant_id, "warning": "", "signer": signer_fp, "nonce": nonce}


def _b64u_challenge(message: str) -> str:
    return base64.urlsafe_b64encode(challenge(message.encode("utf-8"), NAMESPACE)).decode("ascii").rstrip("=")
