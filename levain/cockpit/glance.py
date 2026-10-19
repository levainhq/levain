"""levain.cockpit.glance: two of the At a glance tiles a plain install fills from its own data (SHAPE F5).

``memory_glance`` (Memory): episodes today, episodes in all, open spores, and the top open item named. ``doctor``
(Levain health): doctor's own checks, read-only, grouped into status dots. The tile runs only checks ``levain
doctor`` runs and that write nothing (the hook-pin check is left out: its first run records a pin), and shows only
those: what doctor has no check for gets no dot at all, never a permanent unknown.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.results import Fault, Read, Result

__all__ = ["DOCTOR_CACHE_S", "DOCTOR_FAULT_CACHE_S", "HOOK_LAYOUT_CHECKS", "doctor_reader", "memory_glance_reader"]

DOCTOR_CACHE_S = 60.0
DOCTOR_FAULT_CACHE_S = 30.0         # a check run that raised is not retried on every poll
# the install-layout results the hooks dot holds, by doctor's result names: the install root (a missing install
# fails every hook) and the activation tree the hooks live in
HOOK_LAYOUT_CHECKS = frozenset({"install root", "activation/", "activation/hooks/", "hook scripts"})
TOP_MAX = 80


def memory_glance_reader(view: Callable[[Any], Any]) -> Callable[[Any], Result]:
    """``read(ctx)`` over the cycle's substrate view (``view(ctx)``): the store's episode counts and the open
    spores (Tray and Open loops), with the first open loop named."""
    def read(ctx: Any) -> Result:
        v = view(ctx)
        for k in ("store", "health"):
            if k in v.errors:
                return Fault(f"{k}: {v.errors[k]}")
        h = v.health
        if h is None:
            return Fault("memory: no health read")
        numbers: list[dict[str, Any]] = []
        today = v.jar.today if v.jar is not None else None      # known on a young store too (status no_history)
        if today is not None:
            numbers.append({"label": "today", "value": today, "unit": "episodes"})
        numbers.append({"label": "episodes", "value": h.total_episodes, "unit": None})
        out: dict[str, Any] = {"numbers": numbers}
        if "open_spores" in v.errors:
            out["note"] = "open spores unreadable"
        else:
            numbers.append({"label": "open spores", "value": len(v.tray) + len(v.open_spores), "unit": None})
            top = (v.tray or v.open_spores or [None])[0]
            if top is not None:
                text = " ".join(str(top.text).split())
                out["top"] = text if len(text) <= TOP_MAX else text[:TOP_MAX - 1] + "…"
        if today is None:
            out.setdefault("note", "today's count unread: " + (v.jar.label if v.jar is not None else "no read"))
        return Read(value=out)
    return read


def _checks(install: Path) -> list[tuple[str, list[Any]]]:
    """doctor's read-only checks, grouped as the tile shows them."""
    from levain import doctor
    from levain.install import effective_adapter

    hookless = effective_adapter(install) == "openhands"
    # a hookless install has no hooks to draw as healthy: only a failing layout check reaches its dot
    hooks = [r for r in doctor._check_install_layout(install, expect_hooks=not hookless)
             if r.name in HOOK_LAYOUT_CHECKS and not (hookless and r.ok)]
    if not hookless:
        hooks += doctor._check_hook_freshness(install)
        hooks += doctor._check_consent_hooks(install)
    return [("hooks", hooks), ("memory store", doctor._check_store(install)),
            ("cockpit keys", doctor._check_cockpit_authority(install))]


def _dot(label: str, results: list[Any]) -> dict[str, Any]:
    if not results:
        return {"label": label, "value": "nothing here to check", "unit": None, "status": "unknown", "read": None}
    failed = [r for r in results if not r.ok]
    warned = [r for r in results if r.ok and r.warn]
    if failed:
        # names only: doctor's details carry absolute paths, which never go on the wire
        return {"label": label, "value": f"{len(failed)} failing: {failed[0].name}", "unit": None, "status": "bad",
                "read": "levain doctor"}
    if warned:
        return {"label": label, "value": f"{len(warned)} warning(s): {warned[0].name}", "unit": None,
                "status": "warn", "read": "levain doctor"}
    return {"label": label, "value": "ok", "unit": None, "status": "ok", "read": None}


def doctor_reader(install_root: Path | None) -> Callable[[Any], Result]:
    """``read(ctx)``: doctor's checks over the install as status dots, rebuilt at most once per
    ``DOCTOR_CACHE_S`` (a write's ``fresh`` read rebuilds)."""
    lock = threading.Lock()
    cache: dict[str, Any] = {"at": 0.0, "value": None, "fault": None, "fault_at": 0.0}

    def read(ctx: Any) -> Result:
        if install_root is None:
            return Fault("not configured: no install root, doctor checks an install")
        with lock:
            fresh = getattr(ctx, "fresh", False)
            if cache["fault"] is not None and not fresh and time.monotonic() - cache["fault_at"] <= DOCTOR_FAULT_CACHE_S:
                return Fault(cache["fault"])
            if cache["value"] is None or fresh or time.monotonic() - cache["at"] > DOCTOR_CACHE_S:
                try:
                    groups = _checks(Path(install_root))
                except Exception as exc:  # noqa: BLE001 - a check that raises is the tile's fault, named
                    cache["fault"], cache["fault_at"] = f"doctor's checks did not run: {type(exc).__name__}", time.monotonic()
                    cache["value"] = None       # the fault's TTL ending rebuilds: an older ok is never served
                    return Fault(cache["fault"])
                cache["fault"] = None
                metrics = [_dot(label, rs) for label, rs in groups]
                cache["value"], cache["at"] = {"metrics": metrics, "alerts": []}, time.monotonic()
            return Read(value={"metrics": list(cache["value"]["metrics"]), "alerts": []})
    return read
