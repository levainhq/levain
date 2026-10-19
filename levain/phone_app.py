"""levain.phone_app: the phone app's state line and board (P2, P3), served by the phone port.

Phill ruled the shape 10-19 (project_memory/1019-app/SHAPE.md, a1 and b1, *"that is approved"*):

- a1: the phone sets the SAME state line the cockpit, ``levain state`` and the TUI set. No second store.
- b1: the board is the cockpit's Tray, Open loops and Keep panels; the Tray takes resolve, snooze and capture from the
  phone, Loops and Keep are read there.

Both go through the cockpit's own verb kernel (:func:`levain.cockpit.verbs.dispatch`) over the install the queue's consent
hook names, never around it: the kernel computes each verb's tier, and the phone fires only what the kernel says is a
DIRECT gesture for a surface token. Anything higher (a signed or laptop-only gesture) is shown to the phone as laptop-only and
refused here; the phone has no path to the broker. On top of that the phone has an allowlist (:data:`PHONE_VERBS`), so a
verb added to a panel later does not reach the phone until someone decides it should.

The phone port authenticates the request (its run token or a paired device token); this module is only called for a
request that passed, and records the write's source as ``phone``.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["PHONE_VERBS", "BOARD_PANELS", "PhoneApp", "PhoneAppError"]

# panel -> the verbs the phone may fire on it (b1: Tray resolve / snooze / capture; a1: the state line)
PHONE_VERBS: dict[str, frozenset[str]] = {
    "tray": frozenset({"spore_descend", "spore_surface_at", "spore_seed"}),
    "state": frozenset({"operator_state"}),
}
BOARD_PANELS = ("tray", "loops", "keep")          # b1's order: the Tray you act on, then the two you read
_CREDENTIAL = {"class": "token", "name": "phone", "device_class": None}
_ROWS_SHOWN = 50


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
    kernel calls (the phone port is threaded and the cockpit snapshot is built per request)."""

    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = Path(queue_dir)
        self._lock = threading.Lock()
        self._built: tuple[Any, Any, Any] | None = None

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
        source = SubstrateSource.local(install)
        cockpit = build_default_cockpit(source)
        registry = VerbRegistry({})
        # no broker here: the class web_server's broker_install_class gives a server without one, so no reported
        # gesture renders that this port would then refuse
        cockpit.attach_verbs(VerbView(registry, cockpit, "none"))
        self._built = (source, cockpit, registry)
        return self._built

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
