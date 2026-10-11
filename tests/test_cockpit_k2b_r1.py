"""K2b-3b code L3 r1 fixes (codex): one instant per write, and the Now view explains a degraded label
snapshot as every source panel does."""

from __future__ import annotations

from datetime import datetime, timedelta

import levain.cockpit.verbs as verbs
from levain.cockpit.authority import unavailable
from levain.cockpit.verbs import dispatch, retier
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import home  # noqa: F401 - fixture
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture

_EVE = datetime(2026, 10, 11, 23, 59, 59).astimezone()


def _midnight_clock():
    """The first read is 23:59:59 local; every later read is the next day."""
    calls = []

    def clock():
        calls.append(None)
        return _EVE if len(calls) == 1 else _EVE + timedelta(seconds=2)
    return clock


def _spy_held(monkeypatch):
    seen = []
    real = verbs._held_date

    def spy(row, today):
        seen.append(today)
        return real(row, today)
    monkeypatch.setattr(verbs, "_held_date", spy)
    return seen


def _row(rig):
    return next((p, r) for p in ("loops", "tray", "keep") for r in (rig.ck.panel(p)["rows"] or []))


def test_dispatch_tiers_on_the_snapshots_date_not_a_second_clock_read(rig_broker, monkeypatch) -> None:
    rig, b, _db = rig_broker
    pid, row = _row(rig)
    seen = _spy_held(monkeypatch)
    monkeypatch.setattr(rig.ck, "_clock", _midnight_clock())
    req = {"verb": "spore_surface_at", "panel_id": pid, "row_id": row["id"], "row_version": row["version"],
           "params": {"surface_at": "2026-10-20"}, "intent": "reported"}
    dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
             credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)
    assert seen and set(seen) == {_EVE.date()}


def test_retier_tiers_on_the_snapshots_date_not_a_second_clock_read(rig_broker, monkeypatch) -> None:
    rig, _b, _db = rig_broker
    pid, row = _row(rig)
    seen = _spy_held(monkeypatch)
    monkeypatch.setattr(rig.ck, "_clock", _midnight_clock())
    retier(rig.reg, rig.ck, "spore_surface_at", {"surface_at": "2026-10-20"},
           {"kind": "row", "panel_id": pid, "row_id": row["id"]})
    assert seen and set(seen) == {_EVE.date()}


def test_the_now_view_notes_an_unreadable_label_snapshot(rig_broker) -> None:
    rig, _b, _db = rig_broker
    rig.ck.attach_labeller(lambda: unavailable("journal busy"))
    note = rig.ck.panel("now")["note"] or ""
    assert "provenance unavailable: journal busy" in note


class _Degraded:
    """A readable snapshot with unreadable label rows (§8d row 3)."""
    revision = ("levain-cockpit/2", 7, 3)
    error = None
    degraded = 2

    def provenance(self, key):
        return None

    def label(self, key):
        return "unverified"


def test_the_now_view_notes_degraded_rows_and_its_etag_moves_with_the_snapshot(rig_broker) -> None:
    rig, _b, _db = rig_broker
    rig.ck.attach_labeller(lambda: _Degraded())
    degraded = rig.ck.panel("now")
    assert "2 label row(s) unreadable" in (degraded["note"] or "")
    rig.ck.attach_labeller(lambda: unavailable("journal busy"))
    busy = rig.ck.panel("now")
    assert busy["etag"] != degraded["etag"]       # a 304 never hides the changed note
