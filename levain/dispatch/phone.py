"""levain.dispatch.phone — the phone's dispatch card: sign or cancel a filed dispatch job with a passkey.

The phone port (:mod:`levain.phone_server`) lists every live dispatch job (:meth:`DispatchPhone.pending`) and carries
two passkey ceremonies for one (:meth:`DispatchPhone.step`): ``challenge`` then ``answer`` signs the contract's digest
(:func:`levain.dispatch.sign.sign_payload`, in :data:`~levain.dispatch.sign.DISPATCH_SIGN_NAMESPACE`), and
``cancel-challenge`` then ``cancel`` is the human cancel (:func:`~levain.dispatch.sign.cancel_payload`, in
:data:`~levain.dispatch.sign.DISPATCH_CANCEL_NAMESPACE`). The page's token only opens the route: the authority is the
passkey's assertion, which the job store verifies (:meth:`levain.dispatch.jobs.JobStore.sign`,
:meth:`~levain.dispatch.jobs.JobStore.cancel_by_human`) against the authority source the server hands in, so a token
without an assertion that verifies signs nothing.

``keys`` and ``authority`` are the server's callables over the ONE cockpit its sign requests use (the active passkeys
on its https name, and that cockpit's :class:`levain.consent.queue.AuthoritySource`); either may raise
:class:`levain.consent.sign.SignRefused`, answered with its status. A key is offered and challenged only when its class
covers :data:`~levain.dispatch.sign.SIGN_TIER`, the rule the store holds a signature and a cancel to; the store checks
it again on the signature itself.

A dispatch directory with no store lists nothing and is never created here. Stdlib only.
"""
from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["PENDING_CAP", "DispatchPhone", "DispatchRefused"]

# live jobs a listing shows, newest first; the rest are counted, never drawn
PENDING_CAP = 20
_ANSWER_KEYS = frozenset({"payload", "authenticator_data", "client_data_json", "signature"})
_NEED = {"challenge": frozenset({"signer_fp"}), "answer": _ANSWER_KEYS,
         "cancel-challenge": frozenset({"signer_fp", "reason"}), "cancel": _ANSWER_KEYS}


class DispatchRefused(Exception):
    """A refusal a dispatch route answers with: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class DispatchPhone:
    """The dispatch routes of one phone port, over the store in ``dispatch_dir``, for passkeys made on ``rp_id``."""

    def __init__(self, dispatch_dir: Path | str, rp_id: str, page_origin: str | None,
                 keys: Callable[[], dict[str, Any]], authority: Callable[[], Any], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.dispatch_dir = Path(dispatch_dir)
        self.rp_id = rp_id
        self.page_origin = page_origin
        self._keys = keys
        self._authority = authority
        self._clock = clock

    # --- the store, only when it exists ----------------------------------------------------------------------------
    def _store(self) -> Any:
        """The job store, or None when the directory holds none (opening one would create it)."""
        from levain.dispatch.jobs import DB_NAME, JobStore

        try:
            (self.dispatch_dir / DB_NAME).lstat()
        except FileNotFoundError:
            return None
        return JobStore(self.dispatch_dir, clock=self._clock)

    def _signers(self) -> tuple[dict[str, Any], str]:
        """The passkeys that may sign here (class covering SIGN_TIER), by fingerprint, and a note when there are none."""
        from levain.authority import AuthorityError
        from levain.consent.queue import class_covers
        from levain.consent.sign import SignRefused
        from levain.dispatch.sign import SIGN_TIER

        try:
            keys = self._keys()
        except (SignRefused, AuthorityError, OSError, sqlite3.Error) as exc:
            return {}, str(exc)
        ok = {fp: k for fp, k in keys.items() if class_covers(k.key_class, SIGN_TIER)}
        if not ok:
            return {}, (f"no passkey enrolled for {self.rp_id} covers {SIGN_TIER}: sign dispatch jobs from the laptop "
                        "(levain dispatch sign)")
        return ok, ""

    # --- the listing -----------------------------------------------------------------------------------------------
    def record(self, job: Any, now: str, signers: list[str], signer_note: str) -> dict[str, Any]:
        """One job as the card shows it: every field of :func:`levain.dispatch.cli.job_view` (what ``levain dispatch
        show`` prints), the filer under ``agent`` and ``session``, plus what the card acts on."""
        from levain.dispatch.cli import job_view
        from levain.dispatch.jobs import TERMINAL

        from levain.cockpit.display_safe import visible

        def shown(x: Any) -> Any:
            if isinstance(x, str):
                return visible(x)
            if isinstance(x, dict):
                return {k: shown(e) for k, e in x.items()}
            if isinstance(x, (list, tuple)):
                return tuple(map(shown, x)) if isinstance(x, tuple) else [shown(e) for e in x]
            return x

        # display only, every string the planner or the filing agent wrote (goal, repo, paths, template, grant, the
        # filer, each history cause and its evidence) included: what the card signs is built from the stored job
        # (``sign_payload``), never from this record
        v = shown(job_view(job))
        out = {k: v[k] for k in v if k not in ("filer_agent", "filer_session")}
        out.update({"kind": "dispatch", "agent": v["filer_agent"], "session": v["filer_session"],
                    "read_count": len(v["read"]), "write_count": len(v["write"]),
                    "can_sign": job.state == "filed" and job.lease_end > now and bool(signers), "can_cancel": job.state not in TERMINAL,
                    "signers": signers, "signer_note": signer_note})
        return out

    def pending(self) -> tuple[list[dict[str, Any]], str | None, int]:
        """``(records, note, overflow)``: every live job, newest first, at most :data:`PENDING_CAP`, the rest counted.
        No store is ``([], None, 0)``; a store that cannot be read (a dispatch directory that is a file included) is
        ``([], why, 0)``, never an empty list alone. A job whose stored row cannot be read is left off and counted in
        the note; the rest are listed."""
        from levain.cockpit.consent import utc_stamp
        from levain.dispatch import DispatchError
        from levain.dispatch.jobs import TERMINAL

        try:
            store = self._store()
            if store is None:
                return [], None, 0
            jobs = store.list()
            now = utc_stamp(self._clock())
        except (DispatchError, OSError, sqlite3.Error) as exc:
            return [], f"the dispatch store cannot be read: {type(exc).__name__}: {exc}", 0
        # state, filtering and sorting are per job: a row with no history is left off and counted, never the listing
        bad: list[str] = []
        live: list[Any] = []
        for j in jobs:
            try:
                if j.state not in TERMINAL:
                    live.append((str(j.filed_at), str(j.id), j))
            except (DispatchError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                bad.append(f"{getattr(j, 'id', '?')} ({type(exc).__name__})")
        live = [j for _f, _i, j in sorted(live, key=lambda t: (t[0], t[1]), reverse=True)]
        shown = live[:PENDING_CAP]
        signers, note = self._signers() if shown else ({}, "")
        rows: list[dict[str, Any]] = []
        for j in shown:
            try:
                rows.append(self.record(j, now, list(signers), note))
            except (DispatchError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                bad.append(f"{j.id} ({type(exc).__name__})")
        skipped = None if not bad else (f"{len(bad)} dispatch job{'s' if len(bad) != 1 else ''} could not be read "
                                        f"and {'are' if len(bad) != 1 else 'is'} not shown: {', '.join(bad)}")
        return rows, skipped, max(len(live) - PENDING_CAP, 0)

    # --- the ceremonies --------------------------------------------------------------------------------------------
    def step(self, job_id: str, action: str, req: Any) -> dict[str, Any]:
        """``challenge`` {signer_fp} -> the sign payload and its WebAuthn challenge; ``answer`` {payload,
        authenticator_data, client_data_json, signature} -> the job signed; ``cancel-challenge`` {signer_fp, reason}
        and ``cancel`` the same for the human cancel. Raises :class:`DispatchRefused`: 400 a malformed request, 404
        no such job, 409 a job in the wrong state, 422 a refusal, 503 a fault on this machine (nothing changed)."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.consent.passkey import PasskeyError
        from levain.consent.sign import SignRefused
        from levain.dispatch.jobs import JobStoreError

        need = _NEED.get(action)
        if need is None:
            raise DispatchRefused(404, "not_found", "no such dispatch action")
        if not isinstance(req, dict) or set(req) != need:
            raise DispatchRefused(400, "bad_request", f"request keys must be {sorted(need)}")
        cancel = action.startswith("cancel")
        not_done = f"job {job_id} is NOT cancelled" if cancel else "nothing was signed"
        try:
            store = self._store()
            job = None if store is None else store.get(job_id)
            if job is None:
                raise DispatchRefused(404, "not_found", f"no dispatch job {job_id}")
            if action in ("challenge", "cancel-challenge"):
                return self._challenge(job, req, cancel)
            return self._answer(store, job, req, cancel)
        except SignRefused as exc:
            word = "unavailable" if exc.status == 503 else "sign_refused"
            raise DispatchRefused(exc.status, word, str(exc) + ("" if exc.status != 503 else f": {not_done}")) from None
        except PasskeyError as exc:
            raise DispatchRefused(422, "sign_refused", f"{exc}: {not_done}") from None
        except (VerifierUnavailable, AuthorityError, JobStoreError, OSError, sqlite3.Error) as exc:
            raise DispatchRefused(503, "unavailable", f"the dispatch store cannot answer now ({type(exc).__name__}: "
                                                      f"{exc}): {not_done}") from None

    def _key(self, fp: Any) -> Any:
        """The passkey ``fp`` names, when it may sign here; else a 422 saying why."""
        from levain.consent.queue import class_covers
        from levain.dispatch.sign import SIGN_TIER

        keys = self._keys()
        key = keys.get(fp) if isinstance(fp, str) else None
        if key is None:
            raise DispatchRefused(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                     f"{self.rp_id}")
        if not class_covers(key.key_class, SIGN_TIER):
            raise DispatchRefused(422, "sign_refused", f"a {key.key_class} passkey does not cover {SIGN_TIER}: sign "
                                                       "it from the laptop")
        return key

    def _challenge(self, job: Any, req: dict[str, Any], cancel: bool) -> dict[str, Any]:
        from levain.consent.passkey import challenge
        from levain.dispatch import sign as dsign
        from levain.dispatch.jobs import TERMINAL

        if cancel:
            if job.state in TERMINAL:
                raise DispatchRefused(409, "conflict", f"job {job.id} is {job.state}, which is final")
            problem = dsign.reason_problem(req["reason"])
            if problem is not None:
                raise DispatchRefused(422, "sign_refused", problem)
        elif job.state != "filed":
            raise DispatchRefused(409, "conflict", f"job {job.id} is {job.state}; only a filed job can be signed")
        fp = self._key(req["signer_fp"]).fingerprint
        now = self._clock()
        if cancel:
            payload = dsign.cancel_payload(job, signer_fp=fp, signed_at=now, reason=req["reason"])
            namespace = dsign.DISPATCH_CANCEL_NAMESPACE
        else:
            payload = dsign.sign_payload(job, signer_fp=fp, signed_at=now)
            namespace = dsign.DISPATCH_SIGN_NAMESPACE
        return {"payload": payload, "rp_id": self.rp_id,
                "challenge": _b64u(challenge(dsign.sign_message(payload), namespace=namespace))}

    def _answer(self, store: Any, job: Any, req: dict[str, Any], cancel: bool) -> dict[str, Any]:
        from levain.consent.passkey import b64url_decode, to_sshsig
        from levain.dispatch import sign as dsign
        from levain.dispatch.jobs import TERMINAL, SignatureRefused, TransitionRefused

        if cancel and job.state in TERMINAL:
            raise DispatchRefused(409, "conflict", f"job {job.id} is {job.state}, which is final")
        if not cancel and job.state != "filed":
            raise DispatchRefused(409, "conflict", f"job {job.id} is {job.state}; only a filed job can be signed")
        payload = req["payload"]
        key = self._key(payload.get("signer_fp") if isinstance(payload, dict) else None)
        signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                              b64url_decode(req["client_data_json"], "client_data_json"),
                              b64url_decode(req["signature"], "signature"),
                              namespace=dsign.DISPATCH_CANCEL_NAMESPACE if cancel else dsign.DISPATCH_SIGN_NAMESPACE,
                              page_origin=self.page_origin)
        try:
            if cancel:
                out = store.cancel_by_human(job.id, payload, signature, self._authority())
            else:
                out = store.sign(job.id, payload, signature, self._authority())
        except (SignatureRefused, TransitionRefused) as exc:
            # a job another writer moved while this one verified is a conflict, never "not your signature"
            now = store.get(job.id)
            moved = now is None or (now.state in TERMINAL if cancel else now.state != "filed")
            raise DispatchRefused(409 if moved else 422, "conflict" if moved else "sign_refused",
                                  f"{exc}: " + (f"job {job.id} is NOT cancelled" if cancel else "nothing was signed")
                                  ) from None
        result = {"job_id": out.id, "state": out.state, "signer_fp": payload["signer_fp"],
                  "signed_at": payload["signed_at"]}
        if cancel:
            result["reason"] = payload["reason"]
        return result
