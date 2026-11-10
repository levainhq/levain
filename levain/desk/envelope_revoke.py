"""levain.desk.envelope_revoke — the cockpit's envelope list and a passkey-signed revoke of one.

An envelope is a signed grant of desk authority (:mod:`levain.desk.envelopes`). Narrowing one needs no
signature at the store (``attenuate(..., "kill")``), and until now the only door to it was the terminal. This is the
cockpit's door, and it is the grant revoke's own ceremony (:class:`levain.consent.grant_phone.GrantPhone`): the same
passkeys may sign, the same pinned page, the same refusal words and statuses; the difference is only what is signed
and where it lands.

``revoke-challenge`` names the envelope's ``instance`` (its digest, as the listing showed it), builds a revoke message
on the server (store id, envelope id, the time, a nonce) and HOLDS it, one per envelope, for :data:`HOLD`, beside the
store id and digest it read in the same snapshot. A second challenge for the same envelope by the same signer while one
is held answers the held message again, so two tabs or a double tap sign the same bytes. It answers with the challenge,
``line`` (the exact text signed) and the namespace. ``revoke`` takes only the assertion, converts it
(:func:`levain.consent.passkey.to_sshsig`, held to the pinned page), verifies it with ssh-keygen against the key it
names, and only then writes the kill, in ONE ``BEGIN IMMEDIATE`` on a connection that never makes a store: there it
checks that the store's id and the envelope's digest are still the held ones and the envelope is still live, and a
store that is gone or is another one refuses with nothing written. A held message is taken on its first use, whatever
the outcome, and no path here kills an envelope without a verified passkey signature.

One envelope that cannot be read in full is one row (``state: unreadable``), never an empty list: the operator keeps
the revoke of every other envelope, and of that one by id.

The store is the one the Signer's queue names (the directory the phone's drops and signed envelopes are written to),
never the consent reader's ``directory``: an embedder that swaps the reader for one with no ``directory`` (flow's bridge)
does not blank this. Stdlib plus the package's own verifier.
"""
from __future__ import annotations

import base64
import logging
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority_sig import verify_link_sig
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent.grant_phone import HOLD, GrantPhone, GrantRefused
from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import CoordError, StoreAbsent
from levain.dispatch.jobs import JobStore, JobStoreError

_log = logging.getLogger(__name__)

__all__ = ["HOLD", "NAMESPACE", "REVOCABLE", "EnvelopeRevoke", "GrantRefused", "envelope_state", "revoke_message"]

NAMESPACE = "levain-envelope-revoke"
_ID = re.compile(r"ev-[0-9a-f]{32}")
_LIST_CAP = 50
_LABEL_MAX = 90
# the states a revoke is offered for: a stop that could not be read, or a row that could not be read, is not an ending
REVOCABLE = ("live", "live-unknown", "unreadable")
_ENDED_AFTER = "this envelope ended before your signature landed; nothing was revoked"


def revoke_message(store_id: str, envelope_id: str, at: str, nonce: str) -> str:
    """The exact text a passkey signs to end one envelope."""
    return f"levain-envelope-revoke/1\nstore: {store_id}\nenvelope: {envelope_id}\nat: {at}\nnonce: {nonce}\n"


def envelope_state(stop_at: str, rows: "list[tuple[str, str]]", now: datetime) -> str:
    """``killed``, ``expired``, ``live`` or ``live-unknown``, from the stored stop and the envelope-wide narrowings
    (``kind``, ``value``). A ``stop_earlier`` whose stamp cannot be read is not applied, and the envelope reads
    ``live-unknown`` (never hidden, never called ended on a guess); a stored stop that cannot be read raises."""
    if any(k == "kill" for k, _v in rows):
        return "killed"
    stops, unknown = [parse_stamp(stop_at)], False
    for k, v in rows:
        if k == "stop_earlier":
            try:
                stops.append(parse_stamp(v))
            except (ValueError, TypeError):
                unknown = True
    if min(stops) <= now:
        return "expired"
    return "live-unknown" if unknown else "live"


def _count(x: Any) -> int:
    """A launch count as the card adds it: anything that is not a whole number counts as none."""
    if isinstance(x, bool):
        return 0
    try:
        return max(0, int(x))
    except (TypeError, ValueError):
        return 0


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _scope(body: dict[str, Any]) -> str:
    repos = sorted({Path(w["repo"]).name for w in _list(body.get("where"))
                    if isinstance(w, dict) and isinstance(w.get("repo"), str)} - {""})
    bits = [", ".join(repos[:3])] if repos else []
    launches = sum(_count(x.get("count", 0)) for x in _list(body.get("launches")) if isinstance(x, dict))
    if launches:
        bits.append(f"{launches} launches")
    budget = body.get("budget")
    hours = budget.get("hours") if isinstance(budget, dict) else None
    if isinstance(hours, int) and hours:
        bits.append(f"{hours} h")
    return " · ".join(bits)


def _has_envelopes(conn: Any) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelopes'").fetchone() is not None


def _wide(conn: Any, eid: str) -> "list[tuple[str, str]]":
    """The envelope-wide narrowings (``kind``, ``value``), oldest first."""
    return [(k, v) for k, v in conn.execute(
        "SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND thread_id IS NULL ORDER BY gseq",
        (eid,)).fetchall()]


class EnvelopeRevoke(GrantPhone):
    """The envelope list and revoke of one cockpit, over the store in ``queue_dir`` (the Signer's queue)."""

    _THING = "an envelope"
    _WHY_HTTPS = "a passkey's revoke is taken only from an https page"
    _LAPTOP = "levain envelope kill <id>"

    def __init__(self, queue_dir: Path | str, rp_id: str, page_origin: "str | None", keys: Any, **kw: Any) -> None:
        super().__init__(queue_dir, rp_id, page_origin, keys, lambda: Path(queue_dir), **kw)
        self.ssh_keygen: Any = None      # None: the package's own default verifier

    def _store(self) -> EnvelopeStore:
        return EnvelopeStore(self.queue_dir)

    def _read(self) -> Any:
        """A read-only snapshot of the store: it never makes or migrates anything (a GET must not write)."""
        store = self._store()
        return read_transaction(store.open_readonly)

    def _row_of(self, conn: Any, store: EnvelopeStore, eid: str, now: datetime) -> "dict[str, Any] | None":
        """One envelope as the list shows it, read by id. ``state`` is the envelope-wide state; a narrowing written for
        one bound thread only (``kill`` or ``thread_done``) is counted in ``ended_threads`` of ``threads``, never read as
        the whole envelope ending (it is what the desk's effective stop reads per thread)."""
        e = store._envelope(conn, eid)
        if e is None:
            return None
        rows = _wide(conn, eid)
        threads = conn.execute("SELECT count(DISTINCT thread_id) FROM envelope_binds WHERE envelope_id = ?",
                               (eid,)).fetchone()[0]
        ended = conn.execute(
            "SELECT count(DISTINCT thread_id) FROM envelope_attenuations WHERE envelope_id = ? AND thread_id IS NOT NULL "
            "AND kind IN ('kill', 'thread_done')", (eid,)).fetchone()[0]
        words = conn.execute("SELECT words FROM drops WHERE drop_id = ?", (e.drop_id,)).fetchone()
        label = " ".join(str(words[0]).split())[:_LABEL_MAX] if words else ""
        body = e.body if isinstance(e.body, dict) else {}
        issued = body.get("issued_at", e.accepted_at)
        return {"id": eid, "label": label, "scope": _scope(body),
                "issued": issued if isinstance(issued, str) else e.accepted_at, "expires": e.stop_at,
                "state": envelope_state(e.stop_at, rows, now), "instance": e.digest,
                "threads": int(threads), "ended_threads": int(ended)}

    def _safe_row(self, conn: Any, store: EnvelopeStore, eid: str, now: datetime) -> "dict[str, Any] | None":
        """:meth:`_row_of`, or, for a row whose content cannot be read, the row as ``unreadable`` with its id and digest
        (its revoke reads by id). A store fault is not a row's: it raises, and the whole read is the note."""
        try:
            return self._row_of(conn, store, eid, now)
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad row is one row, never the whole list
            got = conn.execute("SELECT digest FROM envelopes WHERE envelope_id = ?", (eid,)).fetchone()
            if got is None:
                return None
            return {"id": eid, "label": "", "scope": "", "issued": "", "expires": "", "state": "unreadable",
                    "instance": str(got[0]), "threads": 0, "ended_threads": 0,
                    "note": f"this envelope cannot be read in full ({type(exc).__name__}); it can still be revoked"}

    @staticmethod
    def _cheap_state(conn: Any, eid: str, stop_at: Any, now: datetime) -> str:
        """The envelope-wide state from the stop and the narrowings alone, without building the row."""
        try:
            return envelope_state(stop_at, _wide(conn, eid), now)
        except (ValueError, TypeError):
            return "unreadable"

    def _rows(self) -> "list[dict[str, Any]]":
        """Every envelope a revoke is offered for, newest first, then at most :data:`_LIST_CAP` of the ended ones: the
        cap bounds the history, never hides an envelope that can still act. Only the rows shown are built in full."""
        store = self._store()
        if not store.desk_present():
            return []
        now = self._clock()
        out: list[dict[str, Any]] = []
        ended = 0
        with self._read() as conn:
            if not _has_envelopes(conn):
                return []
            for eid, stop_at in conn.execute("SELECT envelope_id, stop_at FROM envelopes ORDER BY gseq DESC").fetchall():
                if self._cheap_state(conn, eid, stop_at, now) not in REVOCABLE:
                    if ended >= _LIST_CAP:
                        continue
                    ended += 1
                row = self._safe_row(conn, store, eid, now)
                if row is not None:
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
        signers, note = self._signers() if any(r["state"] in REVOCABLE for r in rows) else ({}, "")
        return {"envelopes": rows, "signers": sorted(signers), "note": note}

    def _one(self, envelope_id: str) -> "tuple[dict[str, Any], str]":
        """One envelope by id, whatever its age, and the store's id, read in one snapshot. A store with no envelope
        tables holds no envelope (404); a store that cannot be read answers 503, nothing was signed."""
        if not _ID.fullmatch(envelope_id):
            raise GrantRefused(404, "not_found", "no such envelope")
        try:
            store = self._store()
            if not store.desk_present():
                raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}")
            with self._read() as conn:
                if not _has_envelopes(conn):
                    raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}")
                row = self._safe_row(conn, store, envelope_id, self._clock())
                store_id = JobStore._store_id(conn) if row is not None else ""
        except GrantRefused:
            raise
        except StoreAbsent:
            raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}") from None
        except Exception as exc:  # noqa: BLE001 - a store fault is a plain refusal, never a 500
            raise GrantRefused(503, "unavailable", f"the envelope store cannot be read ({type(exc).__name__}); "
                                                   "nothing was signed") from None
        if row is None:
            raise GrantRefused(404, "not_found", f"no envelope {envelope_id[:40]}")
        return row, store_id

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
        row, store_id = self._one(grant_id)
        self._live_or_refuse(row)
        if instance != row["instance"]:
            raise GrantRefused(409, "grant_changed", "this envelope is no longer the one the list showed; nothing "
                                                     "was signed")
        fresh = revoke_message(store_id, grant_id, utc_stamp(self._clock()), secrets.token_hex(16))
        with self._lock:
            now = self._monotonic()
            self._held = {g: h for g, h in self._held.items() if h[2] >= now}
            held = self._held.get(grant_id)
            # the same signer asking again for the same store and envelope signs the held bytes (two tabs, a double
            # tap); anything else is a new message, and the old one is no longer held
            if held is not None and held[1:2] + held[3:] == (signer_fp, store_id, row["instance"]):
                message = held[0]
            else:
                message = fresh
                self._held[grant_id] = (message, signer_fp, now + HOLD, store_id, row["instance"])
        return {"envelope_id": grant_id, "rp_id": self.rp_id, "signer_fp": signer_fp, "line": message,
                "namespace": NAMESPACE, "challenge": _b64u_challenge(message)}

    def _revoke(self, grant_id: str, req: dict[str, str]) -> dict[str, Any]:
        from levain.authority import VerifierUnavailable

        with self._lock:
            held = self._held.pop(grant_id, None)
        if held is None or held[2] < self._monotonic():
            raise GrantRefused(409, "no_challenge", "no revoke of this envelope is waiting for a signature (it was "
                                                    "used, or it lapsed): ask again")
        message, signer_fp, _deadline, store_id, instance = held
        nonce = next((ln[7:] for ln in message.splitlines() if ln.startswith("nonce: ")), "")
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
        try:
            self._kill(grant_id, store_id, instance)
        except GrantRefused as exc:
            # a passkey did sign: the refusal and the signed nonce are kept beside each other
            _log.warning("envelope %s: a passkey-signed revoke was refused after its signature verified: signer %s, "
                         "challenge nonce %s, namespace %s: %s", grant_id, signer_fp, nonce, NAMESPACE, exc.message)
            raise
        # the attenuation row can name only the actor ("operator"); the proof that a passkey signed it is this line
        _log.warning("envelope %s killed by a passkey-signed revoke: signer %s, challenge nonce %s, namespace %s",
                     grant_id, signer_fp, nonce, NAMESPACE)
        return {"revoked": grant_id, "warning": "", "signer": signer_fp, "nonce": nonce}

    def _kill(self, grant_id: str, store_id: str, instance: str) -> None:
        """The kill, in ONE ``BEGIN IMMEDIATE`` on a connection that never makes a store: written only when the store's
        id and the envelope's digest are the ones the signed message was built over and the envelope is still live.
        Every refusal here comes after a verified signature, and says so."""
        store = self._store()
        try:
            with store._write(create=False) as (conn, at):
                try:
                    here = JobStore._store_id(conn)
                except JobStoreError:
                    here = None
                if here != store_id:
                    raise GrantRefused(409, "store_changed", "the envelope store is no longer the one your signature "
                                                             "named; nothing was revoked")
                got = conn.execute("SELECT digest, stop_at FROM envelopes WHERE envelope_id = ?",
                                   (grant_id,)).fetchone() if _has_envelopes(conn) else None
                if got is None:
                    raise GrantRefused(409, "store_changed", "the envelope is no longer in the store your signature "
                                                             "named; nothing was revoked")
                if got[0] != instance:
                    raise GrantRefused(409, "grant_changed", "this envelope changed after your signature; nothing was "
                                                             "revoked")
                try:
                    state = envelope_state(got[1], _wide(conn, grant_id), self._clock())
                except (ValueError, TypeError):
                    state = "unreadable"         # a stop that cannot be read is no reason to keep an envelope alive
                if state not in REVOCABLE or not EnvelopeStore._attenuate(conn, at, grant_id, "kill", "", None,
                                                                          "operator"):
                    raise GrantRefused(409, "expired" if state == "expired" else "already_ended", _ENDED_AFTER)
        except GrantRefused:
            raise
        except StoreAbsent:
            raise GrantRefused(409, "store_gone", "the envelope store is no longer there; nothing was revoked") from None
        except CoordError as exc:
            raise GrantRefused(409, "refused", f"{str(exc)[:160]}; nothing was revoked") from None
        except Exception as exc:  # noqa: BLE001 - a store fault is a plain refusal; the write is one transaction
            raise GrantRefused(503, "unavailable", f"{type(exc).__name__}; nothing was revoked") from None


def _b64u_challenge(message: str) -> str:
    return base64.urlsafe_b64encode(challenge(message.encode("utf-8"), NAMESPACE)).decode("ascii").rstrip("=")
