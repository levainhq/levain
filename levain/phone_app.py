"""levain.phone_app: the phone app's state line and board (P2, P3), served by the phone port.

Phill ruled the shape 10-19 (project_memory/1019-app/SHAPE.md, a1 and b1, *"that is approved"*):

- a1: the phone sets the SAME state line the cockpit, ``levain state`` and the TUI set. No second store.
- b1: the board is the cockpit's Tray, Open loops and Keep panels; the Tray takes resolve and snooze from the phone
  (capture stays on the laptop), Loops and Keep are read there.

Both go through the cockpit's own verb kernel (:func:`levain.cockpit.verbs.dispatch`) over the install the queue's consent
hook names, never around it: the kernel computes each verb's tier, and the phone fires only what the kernel says is a
DIRECT gesture for a surface token. Anything higher (a signed or laptop-only gesture) is shown to the phone as laptop-only and
refused here; the phone has no path to the broker. The tiers come from the install's signed authority log, read
read-only and checked against its anchor per request (``RequestAuthority.capture``), so a verb the laptop raised is
laptop-only here too; on a governed install whose log cannot be read the phone takes no writes. On top of that the
phone has an allowlist (:data:`PHONE_VERBS`), so a verb added to a panel later does not reach the phone until someone
decides it should.

The phone port authenticates the request (its run token or a paired device token); this module is only called for a
request that passed, and records the write's source as ``phone``.

K8 (the phone as a mobile cockpit, B5's conditions): beside that token path, :meth:`PhoneApp.device_verb`,
:meth:`PhoneApp.pending`, :meth:`PhoneApp.pending_signature` and :meth:`PhoneApp.pending_cancel` give the phone the
laptop's write route for the kernel's device verbs (:data:`levain.cockpit.verbs.DEVICE_VERBS`). Each write goes
through the SAME ``dispatch`` and the SAME broker as the laptop's, with a ``device`` credential naming the passkey
that will sign; it becomes a broker pending, C1 included, and applies only once that passkey's signature over the
pending's STORED bytes verifies (the broker's check: the ``levain-cockpit-phone`` namespace, WebAuthn user
verification and origin, T2 at most). The challenge is built here from the stored record, fetched by its id, never
from bytes a page sends. No tier logic lives here: the kernel tiers, the broker decides. A broker is started on the
install's data root for each request and stopped after it, so it holds the write lock only while it answers; while
the laptop's cockpit (``levain serve --write``) holds that lock, the phone's write is refused as ``standby``.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["PHONE_VERBS", "BOARD_PANELS", "PhoneApp", "PhoneAppError"]

# panel -> the verbs the phone may fire on it (b1: Tray resolve / snooze; capture stays on the laptop; a1: the state line)
PHONE_VERBS: dict[str, frozenset[str]] = {
    "tray": frozenset({"spore_descend", "spore_surface_at"}),
    "state": frozenset({"operator_state"}),
}
BOARD_PANELS = ("tray", "loops", "keep")          # b1's order: the Tray you act on, then the two you read
_CREDENTIAL = {"class": "token", "name": "phone", "device_class": None}
_ROWS_SHOWN = 50
_PENDING_ID = re.compile(r"cp[0-9a-f]{32}")          # the broker's pending id (``new_pending_id``)
_ASSERTION = frozenset({"fingerprint", "authenticator_data", "client_data_json", "signature"})


def transport_of(kind: str, device_id: "str | None", *, run_token: "str | None" = None) -> str:
    """The transport a phone request came over, as a device principal records it: ``run-<hash>`` for this run's
    phone token (a hash of the token, so a restart's fresh token is a fresh transport, with its own pending cap and
    idempotency namespace, and the token itself is never recorded), ``paired-<hash>`` for a paired device (a hash
    of its id). Either keeps the principal one well-formed word."""
    if kind == "run":
        if not isinstance(run_token, str) or not run_token:
            raise ValueError("a run transport is named by its run's token")
        return "run-" + hashlib.sha256(b"levain-phone-run\0" + run_token.encode("utf-8")).hexdigest()[:16]
    return "paired-" + hashlib.sha256(str(device_id).encode("utf-8")).hexdigest()[:16]


class PhoneAppError(Exception):
    """A refusal: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error = status, error


def install_of(cockpit_root: Path) -> Path | None:
    """The install a consent hook's ``cockpit_data_root`` belongs to, when it has an install's shape
    (``<install>/.levain/cockpit``, levain.install ``consent_cockpit_root``); None for any other cockpit, whose state
    line and board this port does not guess at."""
    p = Path(os.path.realpath(cockpit_root))
    if p.name == "cockpit" and p.parent.name == ".levain":
        return p.parent.parent
    return None


class PhoneApp:
    """The state line and the board for one phone port. Built lazily on first use and cached; one lock serialises the
    kernel calls (the phone port is threaded and the cockpit snapshot is built per request), except a signature's
    decision, which runs under the write lock alone so the reads answer while ssh-keygen runs (its retier reads the
    cockpit from that thread, as the laptop's server reads its cockpit from several)."""

    def __init__(self, queue_dir: Path, *, marker_directory: Path | None = None) -> None:
        from levain.authority import ReplayCache

        self.queue_dir = Path(queue_dir)
        self._marker_directory = marker_directory
        self._lock = threading.Lock()
        # the phone's writes take one broker at a time (each holds the data root's write lock while it answers); a
        # signature's check runs under this one only, so the reads (``_lock``) answer while ssh-keygen runs. Order:
        # ``_write_lock`` before ``_lock``, never the other way
        self._write_lock = threading.Lock()
        self._reader: Any = None
        self._built: tuple[Any, Any, Any] | None = None
        self._governed = False
        self._replays = ReplayCache()
        self._data_root: Path | None = None

    # --- the cockpit this port reads and writes ------------------------------------------------------------------
    def _parts(self) -> tuple[Any, Any, Any]:
        if self._built is not None:
            return self._built
        from levain.consent.answering import hook_roots
        from levain.cockpit.providers import build_default_cockpit
        from levain.cockpit.verbs import VerbRegistry, VerbView
        from levain.dashboard import SubstrateSource

        roots = hook_roots(self.queue_dir)
        if len(roots) != 1:
            raise PhoneAppError(503, "no_install", "the consent hooks name no cockpit" if not roots else
                                "the consent hooks name more than one cockpit, so which board is not one answer")
        install = install_of(roots[0])
        if install is None:
            raise PhoneAppError(503, "no_install", "the consent hook's cockpit is not a Levain install's, so this "
                                "port does not know its state line or board")
        from levain.cockpit.install import marker_path, marker_present

        try:
            self._governed = marker_present(marker_path(roots[0], self._marker_directory))
        except OSError:
            self._governed = True       # an unreadable marker is never taken for a reported install
        source = SubstrateSource.local(install)
        cockpit = build_default_cockpit(source)
        registry = VerbRegistry({})
        # no broker here: the class web_server's broker_install_class gives a server without one, so no reported
        # gesture renders that this port would then refuse
        cockpit.attach_verbs(VerbView(registry, cockpit, "none"))
        data_root = Path(roots[0])
        cockpit.attach_authority(lambda now: self._capture(data_root, now))
        self._data_root = data_root
        self._built = (source, cockpit, registry)
        return self._built

    def _capture(self, data_root: Path, now: datetime) -> Any:
        """The authority one request tiers under, read-only from the install's cockpit journal: the anchor first, then
        the verified replay. No trace of a log on the data root is the shipped default; a fault raises."""
        from levain.authority import anchor_path
        from levain.cockpit.authority import RequestAuthority, inactive
        from levain.cockpit.firejournal import DB_NAME

        journal, anchor = data_root / DB_NAME, anchor_path(data_root)
        trace = False
        for p in (journal, journal.with_name(DB_NAME + "-wal"), anchor):
            try:
                p.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                pass
            trace = True
        if not trace:
            return RequestAuthority(now, inactive("no authority log is kept on this install"), no_log=True,
                                    log_empty=True)
        conn = sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=30.0)
        try:
            conn.execute("BEGIN")
            got = RequestAuthority.capture(conn, now=now, anchor=anchor, source=str(journal), cache=self._replays)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return got

    # --- reads ---------------------------------------------------------------------------------------------------
    def _panel(self, cockpit: Any, panel_id: str) -> dict[str, Any]:
        got = cockpit.panel(panel_id, profile="full", credential_class=_CREDENTIAL["class"], limit=_ROWS_SHOWN)
        if got is None:
            raise PhoneAppError(404, "not_found", f"no panel {panel_id!r}")
        allowed = PHONE_VERBS.get(panel_id, frozenset())

        def phone_actions(actions: Any) -> list[dict[str, Any]]:
            # what the phone may DO is the allowlist AND the kernel's direct gesture; everything else the kernel
            # offers is still named, as laptop-only, so the page never hides that a row has more verbs
            out = []
            for a in actions or ():
                if not isinstance(a, dict):
                    continue
                ok = a.get("verb") in allowed and a.get("gesture") == "direct"
                out.append({**a, "gesture": "direct" if ok else "laptop-only"})
            return out

        rows = [{**r, "actions": phone_actions(r.get("actions"))} for r in got.get("rows") or ()]
        return {**got, "rows": rows, "actions": phone_actions(got.get("actions"))}

    def state(self) -> dict[str, Any]:
        with self._lock:
            _source, cockpit, _registry = self._parts()
            return self._panel(cockpit, "state")

    def board(self) -> dict[str, Any]:
        with self._lock:
            _source, cockpit, _registry = self._parts()
            return {"panels": [self._panel(cockpit, pid) for pid in BOARD_PANELS],
                    "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    # --- the one write path ----------------------------------------------------------------------------------------
    def fire(self, req: Any) -> dict[str, Any]:
        """One verb from the phone: refused unless the panel and verb are on :data:`PHONE_VERBS` and the kernel's dry
        run says the gesture is direct; then the same request, for real, through the same kernel."""
        from levain.cockpit.verbs import dispatch
        from levain.writes import EditError

        if not isinstance(req, dict):
            raise PhoneAppError(400, "bad_request", "request must be a JSON object")
        panel_id, verb = req.get("panel_id"), req.get("verb")
        if not isinstance(panel_id, str) or not isinstance(verb, str) or verb not in PHONE_VERBS.get(panel_id, ()):
            raise PhoneAppError(403, "laptop_only", f"{verb!r} on {panel_id!r} is not offered on the phone")
        keys = ("verb", "panel_id", "row_id", "row_version", "panel_version", "params", "idempotency_key", "confirm")
        body = {k: req[k] for k in keys if k in req}
        with self._lock:
            source, cockpit, registry = self._parts()
            install_class = cockpit.install_class() or "none"
            if self._governed:
                auth = cockpit.authority()
                if auth.authority is None and auth.log_empty:
                    raise PhoneAppError(503, "authority_absent", "this install is governed and its authority log is "
                                        "not there yet; open the cockpit once on the laptop. Reading still works")
                if auth.authority is None:
                    raise PhoneAppError(503, "authority_unreadable", "this install is governed and its authority log "
                                        f"cannot be read now ({auth.authority_error}), so the phone takes no writes; "
                                        "reading still works")
            try:
                dry = dispatch(registry=registry, cockpit=cockpit, scope=source.write_scope,
                               req={**body, "dry_run": True}, credential=dict(_CREDENTIAL),
                               install_class=install_class, route="cockpit")
                if dry.get("gesture") != "direct":
                    raise PhoneAppError(403, "laptop_only", f"{verb!r} here needs the laptop (tier {dry.get('tier')})")
                return dispatch(registry=registry, cockpit=cockpit, scope=source.write_scope, req=body,
                                credential=dict(_CREDENTIAL), install_class=install_class, route="cockpit")
            except EditError as exc:
                raise PhoneAppError(exc.http_status, exc.code, str(exc)) from None

    # --- K8: the laptop's write route for the device verbs, each a pending the phone's passkey signs -----------------
    def _governed_refusal(self, cockpit: Any) -> None:
        if not self._governed:
            return
        auth = cockpit.authority()
        if auth.authority is None:
            raise PhoneAppError(503, "authority_unreadable", "this install is governed and its authority log cannot be "
                                f"read now ({auth.authority_error}), so the phone takes no writes; reading still works")

    @contextlib.contextmanager
    def _broker(self, source: Any, cockpit: Any, registry: Any) -> Any:
        """The install's broker for one request: the laptop's stores and tier recompute (``web_server._build_broker``'s
        shape), started on the consent hook's cockpit data root and stopped when the request is answered, so the write
        lock is held only meanwhile. A lock another broker holds is ``standby`` (503), never a wait."""
        from levain.cockpit.anneal_effects import (EPISODES, SECTIONS, EpisodeEffectStore, SectionEffectStore,
                                                   section_paths_agree)
        from levain.cockpit.broker import Broker, Lifecycle
        from levain.cockpit.effects import SPORES, SporeEffectStore
        from levain.cockpit.install import REPORTED
        from levain.cockpit.verbs import retier

        scope = source.write_scope
        if scope is None or self._data_root is None:
            raise PhoneAppError(422, "read_only", "this install's source is read-only; nothing editable")
        if os.path.realpath(scope.ledger_root / "cockpit") != os.path.realpath(self._data_root):
            raise PhoneAppError(503, "no_install", "the consent hook's cockpit is not this install's own cockpit, so "
                                "its signers are not the ones this install's writes are checked against")
        spores, episodes = scope.anneal.spores_json, scope.anneal.episodic_db
        stores = {SPORES: SporeEffectStore(spores), EPISODES: EpisodeEffectStore(episodes),
                  **({SECTIONS: SectionEffectStore(episodes)} if section_paths_agree(scope.anneal) else {})}
        broker = Broker(self._data_root, stores=stores, install_class=REPORTED, store_paths=[spores, episodes],
                        marker_directory=self._marker_directory, lock_wait=0.0,
                        tier_of=lambda verb, params, target: retier(registry, cockpit, verb, dict(params),
                                                                    dict(target)))
        try:
            state = broker.start()
            if state is Lifecycle.STANDBY:
                raise PhoneAppError(503, "standby", "the laptop's cockpit holds this install's write lock (levain serve "
                                    "--write), so the phone's write waits until it is not running; reading still works")
            if state is not Lifecycle.ACTIVE:
                raise PhoneAppError(403, "install_disabled", f"the phone cannot write here: "
                                    f"{broker.disabled_reason or state.value}")
            yield broker
        finally:
            broker.stop()

    @staticmethod
    def _device(fingerprint: Any, transport: str) -> dict[str, Any]:
        if not isinstance(fingerprint, str) or not fingerprint:
            raise PhoneAppError(400, "bad_request", "a phone write names the passkey that signs it ('fingerprint')")
        return {"class": "device", "fingerprint": fingerprint, "transport": transport}

    @staticmethod
    def _broker_error(exc: Any) -> PhoneAppError:
        return PhoneAppError(exc.status, exc.code, str(exc))

    def device_verb(self, req: Any, *, transport: str) -> dict[str, Any]:
        """``POST /cockpit/verb`` from the phone: the laptop's verb body plus ``fingerprint`` (the passkey that will
        sign), through the same ``dispatch`` with a ``device`` credential and a broker. A device verb is a WAITING
        pending until its signature verifies; anything else the kernel refuses (not a device verb, a T3)."""
        from levain.cockpit.verbs import dispatch
        from levain.writes import EditError

        if not isinstance(req, dict):
            raise PhoneAppError(400, "bad_request", "request must be a JSON object")
        credential = self._device(req.get("fingerprint"), transport)
        body = {k: v for k, v in req.items() if k != "fingerprint"}
        with self._write_lock, self._lock:
            source, cockpit, registry = self._parts()
            self._governed_refusal(cockpit)
            with self._broker(source, cockpit, registry) as broker:
                try:
                    return dispatch(registry=registry, cockpit=cockpit, scope=source.write_scope, req=body,
                                    credential=credential, install_class=broker.install_class or "none",
                                    route="cockpit", broker=broker)
                except EditError as exc:
                    raise PhoneAppError(exc.http_status, exc.code, str(exc)) from None

    def _journal(self) -> Any:
        """The port's one journal reader (each of its reads opens and closes its own connection); kept, so a pending
        read does not open a new reader, and run its schema check, per GET. Called under ``_lock``."""
        from levain.cockpit.firejournal import DB_NAME, CockpitDB

        if self._data_root is None or not (self._data_root / DB_NAME).is_file():
            raise PhoneAppError(404, "not_found", "no cockpit journal on this install yet")
        if self._reader is None:
            self._reader = CockpitDB(self._data_root)
        return self._reader

    def pending(self, pending_id: str) -> dict[str, Any]:
        """``GET /cockpit/pending/<id>``: a phone pending's STORED record and the WebAuthn challenge built from those
        same bytes in ``levain-cockpit-phone``, for the sign sheet to render and the passkey to sign (B5 condition 3).
        Only a pending a device issued is shown: a passkey signs no other. The state is the effective one, as
        ``Broker.result`` answers it (a ``WAITING`` past its signed ``expires_at`` is ``EXPIRED``; a decided one is
        ``applying``), and the challenge is given only for an effective ``WAITING``: nothing else can be signed."""
        from levain.autonomic.confirm import COCKPIT_PHONE_NAMESPACE
        from levain.cockpit.broker import Broker
        from levain.cockpit.consent import parse_stamp
        from levain.cockpit.firejournal import UnknownPending
        from levain.consent.passkey import challenge

        if not _PENDING_ID.fullmatch(pending_id):
            raise PhoneAppError(404, "not_found", "no such pending")
        with self._lock:
            self._parts()
            try:
                view = self._journal().load(pending_id)
            except UnknownPending:
                raise PhoneAppError(404, "not_found", "no such pending") from None
        signer = Broker.claimed_fingerprint(view.principal)
        if signer is None:
            raise PhoneAppError(404, "not_found", "that pending was not issued from a phone")
        raw = bytes(view.record_bytes)
        state, reason = ("applying" if view.state == "DECIDED" else view.state), view.reason
        if view.state == "WAITING" and parse_stamp(view.record["expires_at"]) <= datetime.now(timezone.utc):
            state, reason = "EXPIRED", "expired"
        out = {"pending_id": view.pending_id, "state": state, "verb": view.record.get("verb"),
               "tier": view.record.get("tier"), "signer_fp": signer, "record": view.record,
               "record_text": raw.decode("utf-8"), "reason": reason}
        if state == "WAITING":
            out["challenge"] = base64.urlsafe_b64encode(challenge(raw, COCKPIT_PHONE_NAMESPACE)).decode("ascii") \
                .rstrip("=")
        return out

    def pending_signature(self, pending_id: str, req: Any, *, page_origin: "str | None") -> dict[str, Any]:
        """``POST /cockpit/pending/<id>/signature`` from the phone: the passkey's assertion (``fingerprint``,
        ``authenticator_data``, ``client_data_json``, ``signature``, base64url), wrapped as an SSHSIG in
        ``levain-cockpit-phone`` over the key enrolled in the install's authority log, then handed to the broker's
        ``submit`` with this port's ``page_origin``, which verifies it over its own stored copy and decides. The
        answer's ``signer_fp`` is the signer the journal PERSISTED for the pending (None when no signature decided
        it, e.g. it had expired), never the fingerprint this request named.

        Only building the cockpit parts takes the read lock: ssh-keygen (two runs, 10 s each at most) runs under the
        write lock alone, so the board and pending reads answer meanwhile."""
        from levain.autonomic.confirm import COCKPIT_PHONE_NAMESPACE
        from levain.authority import PASSKEY
        from levain.cockpit.broker import BrokerError
        from levain.consent.passkey import PasskeyError, b64url_decode, to_sshsig

        if not _PENDING_ID.fullmatch(pending_id):
            raise PhoneAppError(404, "not_found", "no such pending")
        if not isinstance(req, dict) or set(req) != _ASSERTION:
            raise PhoneAppError(400, "bad_request", f"request keys must be {sorted(_ASSERTION)}")
        with self._lock:
            source, cockpit, registry = self._parts()
            self._governed_refusal(cockpit)
        with self._write_lock, self._broker(source, cockpit, registry) as broker:
            try:
                snap = broker.capture().authority
                key = snap.key(req["fingerprint"]) if snap is not None and isinstance(req["fingerprint"], str) \
                    else None
                if key is None or not key.active or key.custody != PASSKEY:
                    raise PhoneAppError(422, "no_passkey", "the signer is not an active passkey enrolled on this "
                                        "install")
                signature = to_sshsig(key.public_key,
                                      b64url_decode(req["authenticator_data"], "authenticator_data"),
                                      b64url_decode(req["client_data_json"], "client_data_json"),
                                      b64url_decode(req["signature"], "signature"),
                                      namespace=COCKPIT_PHONE_NAMESPACE, page_origin=page_origin)
                out = broker.submit(pending_id, signature, page_origin=page_origin)
                db = broker.db
                return {**out, "signer_fp": db.load(pending_id).signer_fp if db is not None else None}
            except PasskeyError as exc:
                raise PhoneAppError(422, "bad_signature", str(exc)) from None
            except BrokerError as exc:
                raise self._broker_error(exc) from None

    def pending_cancel(self, pending_id: str, req: Any, *, transport: str) -> dict[str, Any]:
        """``POST /cockpit/pending/<id>/cancel`` from the phone: the broker's cancel with a ``device`` credential, which
        is always a pending the passkey signs, never a cancel on the claim alone, and only of a pending this same
        passkey's phone issued (the broker refuses any other)."""
        from levain.cockpit.broker import BrokerError

        if not _PENDING_ID.fullmatch(pending_id):
            raise PhoneAppError(404, "not_found", "no such pending")
        if not isinstance(req, dict) or not set(req) <= {"fingerprint", "idempotency_key"}:
            raise PhoneAppError(400, "bad_request", "request keys are 'fingerprint' and an optional 'idempotency_key'")
        credential = self._device(req.get("fingerprint"), transport)
        with self._write_lock, self._lock:
            source, cockpit, registry = self._parts()
            self._governed_refusal(cockpit)
            with self._broker(source, cockpit, registry) as broker:
                try:
                    return broker.cancel(pending_id, credential=credential,
                                         idempotency_key=req.get("idempotency_key"))
                except BrokerError as exc:
                    raise self._broker_error(exc) from None
