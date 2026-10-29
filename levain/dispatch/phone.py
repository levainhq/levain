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

AUTHORISE (F11-s2b §5). A ``recommended`` job whose recommend names a branch (``onto``, and a ``run``, else the
lowest passing run) carries an ``authorise`` card: the run, the branch, its change set (computed here, from the
store's own run record: each path, +/- lines, new, deleted, binary or gitlink named, at most :data:`CHANGE_CAP`
drawn) and its landing tier, with the passkeys whose class covers THAT tier (a Face ID passkey covers T2; a T3 change
shows "needs your laptop key" and no signer). ``authorise-challenge`` builds the payload on the server
(:func:`levain.dispatch.adopt.authorise_payload_for`) and challenges it in
:data:`~levain.dispatch.adopt.DISPATCH_AUTHORISE_NAMESPACE`; ``authorise`` takes the assertion over the payload the
server built (one whose job, run, branch, digests or signer differ from a fresh build is refused), then lands it
through :func:`levain.dispatch.cli.land_job` with no data root, which verifies it against the authority source the
dispatch directory's consent hooks name. A job whose recommend named no branch has no card (the laptop's
``levain dispatch authorise``).

A dispatch directory with no store lists nothing and is never created here. Stdlib only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["CHANGE_CAP", "PENDING_CAP", "DispatchPhone", "DispatchRefused"]

# live jobs a listing shows, newest first; the rest are counted, never drawn
PENDING_CAP = 20
# change-set entries an authorise card draws; the rest are counted, never drawn
CHANGE_CAP = 50
_ANSWER_KEYS = frozenset({"payload", "authenticator_data", "client_data_json", "signature"})
_NEED = {"challenge": frozenset({"signer_fp"}), "answer": _ANSWER_KEYS,
         "cancel-challenge": frozenset({"signer_fp", "reason"}), "cancel": _ANSWER_KEYS,
         "authorise-challenge": frozenset({"signer_fp"}), "authorise": _ANSWER_KEYS}


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
                    "signers": signers, "signer_note": signer_note,
                    "authorise": shown(self._authorise_card(job))})
        out["sha256"] = self.version(out)       # the row version a challenge names (:meth:`step_shown`)
        return out

    @staticmethod
    def version(rec: "dict[str, Any]") -> str:
        """The version a listed job (and the cockpit's sheet row) is named by: its id, digest, state and lease, and the
        run, branch and tier of its authorise card (so a recommendation that moved reads as changed: a pick made on the
        old card is never offered on the new one). The card's staged change set needs no field here: it is the diff of
        the run's stored base and staged commit, and a run record is written once (``run_records`` refuses an update and
        a second row for the same run), so the run pins it."""
        card = rec.get("authorise") if isinstance(rec.get("authorise"), dict) else {}
        return hashlib.sha256(json.dumps([rec.get("id"), rec.get("digest"), rec.get("state"), rec.get("lease_end"),
                                          card.get("run"), card.get("onto"), card.get("tier")]).encode()).hexdigest()

    CHALLENGES = ("challenge", "cancel-challenge", "authorise-challenge")

    def _current(self, job_id: str) -> "dict[str, Any] | None":
        """``{state, sha256}`` of one live job as :meth:`pending` would list it, else None (no store, no such job, or a
        final state). A job whose record cannot be read is ``state`` ``unreadable``."""
        from levain.cockpit.consent import utc_stamp
        from levain.dispatch import DispatchError
        from levain.dispatch.jobs import TERMINAL

        store = self._store()
        job = None if store is None else store.get(job_id)
        if job is None:
            return None
        try:
            if job.state in TERMINAL:
                return None
            signers, note = self._signers()
            rec = self.record(job, utc_stamp(self._clock()), list(signers), note)
        except (DispatchError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            return {"state": "unreadable", "sha256": ""}
        return {"state": rec["state"], "sha256": self.version(rec)}

    def shown(self, job_id: str, action: str, version: str,
              row: "Callable[[str], dict[str, Any] | None] | None" = None) -> None:
        """Refuse a challenge for job ``job_id`` unless its row's ``sha256`` (``row(job_id)``, default
        :meth:`_current`) is ``version``, the one the page showed: a job amended, moved or ended since is a 409 in the
        plain sign route's words, so a passkey never signs a job the page did not show. A job no longer open has no
        row, which is also a change; an unreadable one is never challenged; a store fault is a 503 naming the exception
        type only (its text names paths and tables)."""
        from levain.consent.sign import error_word
        from levain.dispatch import DispatchError

        not_done = f"job {job_id} is NOT cancelled" if action == "cancel-challenge" else "nothing was signed"
        try:
            now = (row or self._current)(job_id)
        except (DispatchError, OSError, sqlite3.Error) as exc:
            raise DispatchRefused(503, "unavailable", f"the dispatch store cannot answer now ({type(exc).__name__}): "
                                                      f"{not_done}") from None
        if now is not None and now.get("state") == "unreadable":
            raise DispatchRefused(409, error_word(409), f"dispatch job {job_id} could not be read, so nothing is signed "
                                                        f"from this page: read it on the laptop (levain dispatch show "
                                                        f"{job_id}); {not_done}")
        if now is None or now.get("sha256") != version:
            raise DispatchRefused(409, error_word(409), f"dispatch job {job_id} changed since it was shown: reload and "
                                                        f"read it again; {not_done}")

    def step_shown(self, job_id: str, action: str, req: Any,
                   row: "Callable[[str], dict[str, Any] | None] | None" = None) -> dict[str, Any]:
        """:meth:`step`, a challenge gated on the version the page showed: ``request_sha256`` in the body is taken off
        before the body is checked and compared with the job's version now (:meth:`shown`) before the payload is built
        and again after (an amendment while it was built). A challenge that names none (a body that is not an object
        included) is the plain 409 before :meth:`step` is asked anything. Other actions pass through unchanged."""
        from levain.consent.sign import error_word

        version = None
        if action in self.CHALLENGES:
            version = req.get("request_sha256") if isinstance(req, dict) else None
            if not isinstance(version, str) or not version:
                raise DispatchRefused(409, error_word(409), f"the challenge for dispatch job {job_id} names no listed "
                                                            "version: reload and read it again; "
                                                            + (f"job {job_id} is NOT cancelled" if action == "cancel-challenge"
                                                               else "nothing was signed"))
            req = {k: v for k, v in req.items() if k != "request_sha256"}
            self.shown(job_id, action, version, row)
        out = self.step(job_id, action, req)
        if version is not None:
            self.shown(job_id, action, version, row)
        return out

    # --- the authorise card (F11-s2b §5) ---------------------------------------------------------------------------
    def _proposal(self, store: Any, job: Any) -> tuple[str, int] | None:
        """``(onto, run)`` the planner's recommend proposed for a ``recommended`` job: its ``run``, else the lowest
        passing run; ``None`` when it named no branch (no card: the laptop authorises it)."""
        import json

        if job.state != "recommended":
            return None
        rows = [h for h in job.history if h.to_state == "recommended"]
        try:
            proposal = json.loads(rows[-1].evidence) if rows and rows[-1].evidence else {}
        except ValueError:
            return None
        onto = proposal.get("onto") if isinstance(proposal, dict) else None
        if not isinstance(onto, str):
            return None
        run = proposal.get("run")
        if run is None:
            passing = [r.run for r in store.run_records(job.id) if r.verifier.get("result") == "pass"]
            run = min(passing) if passing else None
        return (onto, run) if type(run) is int else None

    def _judged(self, store: Any, job: Any, run: int) -> tuple[Any, Any, str]:
        """``(record, changes, tier)``: run ``run``'s stored record, its change set and its landing tier, as the floor
        will judge them; a run with no record, a change set over a cap or a tier that cannot be read is a 422."""
        from levain.dispatch import adopt
        from levain.dispatch.cli import _landing_tier

        record = store.run_record(job.id, run)
        if record is None or record.verifier.get("result") != "pass":
            raise DispatchRefused(422, "land_refused", f"run {run} of job {job.id} has no verified record")
        changes = adopt.change_set(job.repo, record.base, record.staged_commit)
        if isinstance(changes, str):
            raise DispatchRefused(422, "land_refused", changes)
        tier = adopt._tier(_landing_tier(store.directory), job.repo, changes.paths)
        if tier is None:
            raise DispatchRefused(422, "land_refused", "the landing tier could not be read; nothing lands")
        return record, changes, tier

    def _covering(self, tier: str) -> list[str]:
        """The passkeys that may sign an authorise at ``tier``: their class covers it."""
        from levain.consent.queue import class_covers

        return sorted(fp for fp, k in self._keys().items() if class_covers(k.key_class, tier))

    def _authorise_card(self, job: Any) -> dict[str, Any] | None:
        """The authorise card of a recommended job whose recommend named a branch, else ``None``. What cannot be read
        (git, the store, the keys) is the card's ``problem`` and no signer, never the listing's failure."""
        from levain.authority import AuthorityError
        from levain.consent.sign import SignRefused
        from levain.dispatch import DispatchError

        if job.state != "recommended":
            return None
        # bound before the try: every failure below returns this card with its problem set, onto and run kept
        card: dict[str, Any] = {"onto": None, "run": None, "tier": None, "changes": [], "change_overflow": 0,
                                "files": 0, "changed_lines": None, "signers": [], "note": "", "problem": None}
        try:
            store = self._store()
            proposal = None if store is None else self._proposal(store, job)
            if proposal is None:
                return None
            card["onto"], card["run"] = onto, run = proposal
            record, changes, tier = self._judged(store, job, run)
            entries = [{"path": c.path, "status": c.status, "added": c.added, "deleted": c.deleted,
                        "binary": c.binary, "gitlink": c.non_blob} for c in changes.entries]
            card.update({"tier": tier, "staged": record.staged_commit, "base": record.base,
                         "changes": entries[:CHANGE_CAP], "change_overflow": max(len(entries) - CHANGE_CAP, 0),
                         "files": len(entries), "changed_lines": changes.changed_lines})
            card["signers"] = self._covering(tier)
            if not card["signers"]:
                card["note"] = (f"needs your laptop key: no passkey enrolled for {self.rp_id} covers {tier} "
                                f"(levain dispatch authorise {job.id} --run {run} --onto {onto})")
            return card
        except DispatchRefused as exc:
            return {**card, "signers": [], "problem": exc.message}
        except (DispatchError, SignRefused, AuthorityError, OSError, sqlite3.Error) as exc:
            return {**card, "signers": [],
                    "problem": f"the authorise card cannot be read now ({type(exc).__name__}: {str(exc)[:200]})"}

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
        if action.startswith("authorise"):
            not_done = "nothing was landed" if action == "authorise-challenge" else \
                f"whether job {job_id} landed is not known: read it with `levain dispatch show`"
        try:
            store = self._store()
            job = None if store is None else store.get(job_id)
            if job is None:
                raise DispatchRefused(404, "not_found", f"no dispatch job {job_id}")
            if action.startswith("authorise"):
                return self._authorise(store, job, action, req, not_done)
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

    def _authorise(self, store: Any, job: Any, action: str, req: dict[str, Any], not_done: str) -> dict[str, Any]:
        """``authorise-challenge`` {signer_fp} -> the server-built authorise payload and its WebAuthn challenge;
        ``authorise`` {payload, ...assertion} -> the land's outcome (adopted, merge, failed), a refused land a 422.
        Faults (git, the configuration, the verifier) are a 503 saying what was not done."""
        from levain.authority import VerifierUnavailable
        from levain.consent.passkey import b64url_decode, challenge, to_sshsig
        from levain.dispatch import DispatchError
        from levain.dispatch import adopt
        from levain.dispatch.cli import CockpitUnnamed, land_job
        from levain.dispatch.control import ControlStateUnavailable
        from levain.dispatch.jobs import JobStoreError

        try:
            proposal = self._proposal(store, job)
            if proposal is None:
                raise DispatchRefused(409, "conflict", f"job {job.id} is {job.state} and has no recommended branch "
                                                       "to authorise from the phone: authorise it from the laptop "
                                                       "(levain dispatch authorise)")
            onto, run = proposal
            _record, _changes, tier = self._judged(store, job, run)
            if action == "authorise-challenge":
                key = self._key(req["signer_fp"], tier)
                payload = adopt.authorise_payload_for(store, job.id, run=run, onto=onto, signer_fp=key.fingerprint)
                return {"payload": payload, "rp_id": self.rp_id, "tier": tier,
                        "challenge": _b64u(challenge(adopt.authorise_message(payload),
                                                     namespace=adopt.DISPATCH_AUTHORISE_NAMESPACE))}
            payload = req["payload"]
            if not isinstance(payload, dict) or set(payload) != adopt.AUTHORISE_KEYS:
                raise DispatchRefused(400, "bad_request", "the payload is not the authorise item this server built")
            key = self._key(payload.get("signer_fp"), tier)
            # the payload is the server's: a fresh build for the same signer must agree on every field but the time
            fresh = adopt.authorise_payload_for(store, job.id, run=run, onto=onto, signer_fp=key.fingerprint)
            if any(payload[k] != fresh[k] for k in adopt.AUTHORISE_KEYS - {"signed_at"}):
                raise DispatchRefused(422, "sign_refused", "the payload is not the one this server built for the "
                                                           f"card's run and branch: {not_done}")
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"),
                                  namespace=adopt.DISPATCH_AUTHORISE_NAMESPACE, page_origin=self.page_origin)
            landing = land_job(store, job.id, onto=onto, signed=(payload, signature), data_root=None)
        except CockpitUnnamed as exc:
            raise DispatchRefused(503, "unavailable", f"{exc.door}: {not_done}") from None
        except (adopt.GitFault, ControlStateUnavailable, VerifierUnavailable, JobStoreError) as exc:
            raise DispatchRefused(503, "unavailable", f"this machine could not do it ({type(exc).__name__}: {exc}): "
                                                      f"{not_done}") from None
        except DispatchError as exc:
            raise DispatchRefused(422, "land_refused", f"{exc}: {not_done}") from None
        if landing.outcome == "refused":
            raise DispatchRefused(422, "land_refused", landing.message)
        return {"job_id": landing.job.id, "state": landing.job.state, "outcome": landing.outcome,
                "moved": landing.moved, "message": landing.message, "command": landing.command or None,
                "signer_fp": payload["signer_fp"], "signed_at": payload["signed_at"]}

    def _key(self, fp: Any, tier: str | None = None) -> Any:
        """The passkey ``fp`` names, when its class covers ``tier`` (default: :data:`SIGN_TIER`, a contract's
        signature); else a 422 saying why (for a T3 authorise: the laptop's key)."""
        from levain.consent.queue import class_covers
        from levain.dispatch.sign import SIGN_TIER

        keys = self._keys()
        key = keys.get(fp) if isinstance(fp, str) else None
        if key is None:
            raise DispatchRefused(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                     f"{self.rp_id}")
        need = SIGN_TIER if tier is None else tier
        if not class_covers(key.key_class, need):
            raise DispatchRefused(422, "sign_refused", f"a {key.key_class} passkey does not cover {need}: "
                                                       + ("sign it from the laptop" if tier is None
                                                          else "this needs your laptop key (levain dispatch "
                                                               "authorise)"))
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
