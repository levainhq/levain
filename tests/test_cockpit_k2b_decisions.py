"""K2b-3b lane C: the ``decisions`` panel's compositor and Recent edits' broker rows (K2B3B_DESIGN rev 5
§0.4, §0.6, §0.7, §3, §8d rows 4, 5, 7). Every broker row here comes from a real ``CockpitDB`` through
the journal API (lane A's helpers), never a parallel harness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from levain.cockpit import text
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.engine import Cockpit, ProviderSpec
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.labels import BrokerLabeller
from levain.cockpit.providers import _tomb_gate, edit_label_key
from levain.cockpit.results import Absent, Read, RowIn
from levain.cockpit.verbs import VerbRegistry, VerbView
from tests.test_cockpit_k2a import Rig
from tests.test_cockpit_k2b_labels import PHONE, T0, fire, make_record

NOW = T0 + timedelta(minutes=10)
NP_FACETS = frozenset({"repo", "seat", "stopped", "due", "at", "owner_install"})


def _np_rows(extra: tuple[RowIn, ...] = ()) -> tuple[RowIn, ...]:
    return (RowIn(id="np:a1", title="ship it?", body="stopped: release", facets={
        "repo": "levain", "stopped": "release", "at": "2026-10-10T09:00:00Z", "due": "2026-10-12"},
        stored={"what": "ship it?", "np_version": "v1"}),) + extra


def _np_spec(rows: tuple[RowIn, ...]) -> ProviderSpec:
    """flow's ``ProviderSpec("decisions", ...)`` shape (flow ``scripts/flow_cockpit.py``)."""
    def read(ctx):
        return Read(rows=rows)

    def read_one(ctx, row_id):
        hit = [r for r in rows if r.id == row_id]
        return Read(rows=(hit[0],)) if hit else Absent(f"decision {row_id} is no longer open")
    return ProviderSpec("decisions", "triage-list", "Decisions waiting", "gate", read, region="operate", rank=-1,
                        order="decision.dated", facets=NP_FACETS, version_fields=("what", "np_version"),
                        rowset="needs_phill", read_one=read_one, search_fields=("title", "body"),
                        empty="nothing is waiting on you.", note="needs_phill list, dated first")


@pytest.fixture
def journal(tmp_path: Path) -> CockpitDB:
    """One pending WAITING on a signature (expires NOW+50m) and one EXPIRED (an outcome at T0+1m)."""
    j = _journal(tmp_path)
    pid, raw = make_record(intent="sign")
    j.issue(record_bytes=raw, principal=PHONE, idempotency_key="waiting", now=T0)
    xid, xraw = make_record(intent="sign", ttl=timedelta(minutes=1))
    j.issue(record_bytes=xraw, principal=PHONE, idempotency_key="expired", now=T0)
    j.ids = {"waiting": pid, "expired": xid}     # type: ignore[attr-defined]
    return j


def _journal(tmp_path: Path) -> CockpitDB:
    (tmp_path / "cockpit").mkdir()
    j = CockpitDB(tmp_path / "cockpit")
    with j.write() as conn:
        conn.execute("INSERT INTO tokens (name, generation) VALUES ('phone', 1)")
    return j


def _ck(**kw: Any) -> Cockpit:
    return Cockpit(clock=lambda: NOW, **kw)


def _panel(ck: Cockpit) -> dict[str, Any]:
    return ck.panel("decisions", credential_class="token")


# --- (a) a downstream's own registration keeps exactly today's panel ---------------------------------


def test_a_registered_downstream_decisions_panel_is_unchanged_by_broker_rows(journal):
    plain = _ck()
    plain.register(_np_spec(_np_rows()))
    with_broker = _ck()
    with_broker.register(_np_spec(_np_rows()))
    with_broker.attach_decision_rows(journal.decision_rows)
    assert journal.decision_rows(NOW).pendings and journal.decision_rows(NOW).outcomes   # not vacuous
    got = _panel(with_broker)
    assert got == _panel(plain)
    assert [r["id"] for r in got["rows"]] == ["np:a1"]
    # registered AFTER the broker attached: the downstream's panel replaces the kernel's, rows and all
    late = _ck()
    late.attach_decision_rows(journal.decision_rows)
    assert {r["id"].split(":", 1)[0] for r in _panel(late)["rows"]} == {"pend", "out"}
    late.register(_np_spec(_np_rows()))
    assert _panel(late) == _panel(plain)


# --- (b) a source attached through the seam -----------------------------------------------------------


def test_an_attached_source_shows_as_open_with_its_own_versions_beside_broker_rows(journal):
    plain = _ck()
    plain.register(_np_spec(_np_rows()))
    own_version = _panel(plain)["rows"][0]["version"]
    planted = RowIn(id="pend:planted", title="a downstream row in the broker's id space",
                    facets={"repo": "x", "stopped": "y", "at": "2026-10-10T09:00:00Z"},
                    stored={"what": "p", "np_version": "v"})
    ck = _ck()
    ck.attach_decision_source(_np_spec(_np_rows((planted,))))
    ck.attach_decision_rows(journal.decision_rows)
    ck.attach_verbs(VerbView(VerbRegistry(), ck, "reported"))
    p = _panel(ck)
    rows = {r["id"]: r for r in p["rows"]}
    pend, out = f"pend:{journal.ids['waiting']}", f"out:{journal.ids['expired']}"
    assert [r["id"] for r in p["rows"]] == ["np:a1", pend, out]
    assert rows["np:a1"]["group"] == "open" and rows["np:a1"]["version"] == own_version
    assert rows[pend]["group"] == "waiting" and rows[out]["group"] == "outcomes"
    assert "pend:planted" not in rows
    assert p["status"] == "partial"
    assert any("broker prefix" in s["reason"] and s["count"] == 1 for s in p["skipped"])
    assert p["count"] == p["actionable_count"] == 2
    # a pending: titled from its record, versioned over (state, expires_at), cancel at its own tier
    assert rows[pend]["title"] == "spore_update on spore-1"
    assert rows[pend]["facets"]["pending_tier"] == "T2" and rows[pend]["facets"]["principal"] == PHONE
    (act,) = rows[pend]["actions"]
    assert act["verb"] == "pending_cancel" and act["tier"] == "T2" and act["gesture"] == "direct-reported"
    assert act["target"] == {"kind": "pending", "pending_id": journal.ids["waiting"]}
    assert act["route"] == f"/cockpit/pending/{journal.ids['waiting']}/cancel"
    assert rows[out]["actions"] == [] and rows[out]["facets"]["outcome"] == "EXPIRED"
    # read_one routes by prefix to the owning contributor, and answers what the panel rendered
    for rid in ("np:a1", pend, out):
        one = ck.read_one("decisions", rid).value
        assert (one["version"], one["group"]) == (rows[rid]["version"], rows[rid]["group"])
    assert isinstance(ck.read_one("decisions", "pend:planted"), Absent)
    # the text surface draws the queue, never the history
    lines = "\n".join(t for _s, t in text.render_lines(text.snapshot(ck), now=NOW))
    assert "Waiting on a signature (1)" in lines and "spore_update on spore-1" in lines
    assert "Outcomes" not in lines and "EXPIRED" not in lines


def test_a_broker_fault_leaves_the_open_rows_and_notes_it(journal):
    def broken(now):
        raise RuntimeError("journal busy")
    ck = _ck()
    ck.attach_decision_source(_np_spec(_np_rows()))
    ck.attach_decision_rows(broken)
    p = _panel(ck)
    assert p["status"] == "ok" and [r["id"] for r in p["rows"]] == ["np:a1"]
    assert "broker decisions unavailable: RuntimeError: journal busy" in p["note"]


# --- (c) count, actionable_count and empty with only history --------------------------------------------


def test_only_outcomes_is_empty_with_the_history_still_carried(tmp_path):
    j = _journal(tmp_path)
    xid, xraw = make_record(intent="sign", ttl=timedelta(minutes=1))
    j.issue(record_bytes=xraw, principal=PHONE, idempotency_key="expired", now=T0)
    ck = _ck()
    ck.attach_decision_rows(j.decision_rows)
    p = _panel(ck)
    assert p["status"] == "empty" and p["count"] == 0 and p["actionable_count"] == 0
    assert p["title"] == "Decisions waiting (0)"
    assert [r["id"] for r in p["rows"]] == [f"out:{xid}"]
    groups = {g["id"]: g for g in p["groups"]}
    assert groups["outcomes"] == {"id": "outcomes", "title": "Outcomes", "count": 1, "uncounted": True}
    assert groups["resolved"]["count"] == 0 and "uncounted" not in groups["waiting"]
    now_rows = ck.panel("now", credential_class="token")["rows"]
    assert not [r for r in now_rows if r.get("panel_id") == "decisions"]
    assert "no cockpit broker" not in p["note"]


# --- (d) Recent edits: broker fires from the journal ----------------------------------------------------


def test_edits_with_no_ledger_shows_the_journal_fire(tmp_path):
    rig = Rig(tmp_path)
    j = _journal(tmp_path)
    pid = fire(j, "k1")
    assert not (rig.src.write_scope.ledger_root / "edits.jsonl").exists()
    rig.ck.attach_edits_reader(j.edits)
    labeller = BrokerLabeller(j)

    def capture(now):
        with j.read() as conn:
            return RequestAuthority.capture(conn, now=now, anchor=None, labeller=labeller, source=str(j.path))
    rig.ck.attach_authority(capture)
    assert rig.ck.authority().labels.error is None       # the journal's labels, not a degraded stand-in
    p = rig.ck.panel("edits", credential_class="token")
    assert p["status"] == "ok", p
    (row,) = p["rows"]
    assert row["id"] == f"fire:{pid}:0"
    assert row["facets"] == {"edit_kind": "spore_update", "at": "2026-10-10T12:01:00.000000Z", "undoable": False}
    assert row["provenance"] is None               # a fire: row is history; only tomb: rows carry a label (L2 F2)
    assert "token" in row["title"]                 # who made the edit: its credential
    assert row["actions"] == []                    # undo applies to undoable rows only


def test_the_journal_is_read_once_per_request_across_write_probes(tmp_path):
    rig = Rig(tmp_path)
    j = _journal(tmp_path)
    pid = fire(j, "k1")
    calls: list[int] = []

    def reader(limit):
        calls.append(limit)
        return j.edits(limit)
    rig.ck.attach_edits_reader(reader)
    auth = RequestAuthority(rig.clock())
    for _ in range(3):
        assert rig.ck.read_one("edits", f"fire:{pid}:0", authority=auth).value["id"] == f"fire:{pid}:0"
    assert len(calls) == 1


def test_a_journal_fault_keeps_the_ledger_rows_partial_and_both_failing_is_a_fault(tmp_path):
    rig = Rig(tmp_path)

    def broken(limit):
        raise RuntimeError("journal busy")
    rig.ck.attach_edits_reader(broken)
    p = rig.ck.panel("edits", credential_class="token")
    assert p["status"] == "error" and "broker edits unreadable: RuntimeError: journal busy" in p["error"]
    ledger = rig.src.write_scope.ledger_root / "edits.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"id": "e1", "kind": "spore", "action": "touch", "ts": "2026-10-10T11:00:00+00:00"}\n')
    p = rig.ck.panel("edits", credential_class="token")
    assert p["status"] == "partial" and [r["id"] for r in p["rows"]] == ["edit:e1"]
    assert [s["reason"] for s in p["skipped"]] == ["broker edits unreadable: RuntimeError: journal busy"]


# --- the tomb gate (§8d row 5): read once into stored, and both the key and the version read it -----------


def test_the_tomb_gate_matches_only_this_ops_retirement():
    def store(st=None, exc=None):
        def status(ok):
            if exc:
                raise exc
            return st
        return SimpleNamespace(origin_key_status=status)
    retired = SimpleNamespace(state="retired", effect_id="eff-1", episode_id="ep-1")
    assert _tomb_gate(store(retired), "ok", "eff-1", "ep-1") == "match"
    assert _tomb_gate(store(retired), "ok", "eff-2", "ep-1") == "mismatch"
    assert _tomb_gate(store(retired), "ok", "eff-1", "ep-9") == "mismatch"
    assert _tomb_gate(store(SimpleNamespace(state="live", effect_id=None, episode_id="ep-1")),
                      "ok", "eff-1", "ep-1") == "mismatch"
    assert _tomb_gate(store(exc=OSError("gone")), "ok", "eff-1", "ep-1") == "unavailable"
    stored = {"_fire": True, "operation_id": "op", "resource_key": "tomb:ok", "origin_key": "ok",
              "_tomb_gate": "mismatch"}
    key = edit_label_key(stored)
    assert (key.resource_key, dict(key.values), key.operation_id, key.gate) == \
        ("tomb:ok", {"origin_key": "ok"}, "op", "mismatch")
    assert edit_label_key({"id": "ledger-row", "operation_id": "x", "resource_key": "y"}) is None
