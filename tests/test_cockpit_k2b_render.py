"""K2b-3b render (lane B): provenance and tiers from ONE authority snapshot per request (K2B3B_DESIGN.md
§0.1, §0.5, §8d; K2B3B_BUILD_PLAN.md "Render (lane B)"). The labeller here is a fake satisfying exactly
the contract's consumer surface; lane A's journal-backed one replaces it at the merge. With no labeller
the output is today's: the K1/K2a suites hold that, unchanged."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from anneal_memory import Store
from anneal_memory.origin import canonical_section_markdown

from levain.cockpit import providers
from levain.cockpit.anneal_effects import SectionEffectStore, section_effects
from levain.cockpit.authority import LabelKey
from levain.cockpit.effects import ApplyOutcome, ApplyUnit
from levain.cockpit.verbs import VerbView, retier, row_actions
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, Rig


@dataclass
class FakeLabels:
    """A snapshot that labels the rows whose resource key is in ``labelled``; every other key reads
    ``unverified`` with nulls, as the contract's snapshot does for a value with no label."""

    labelled: Mapping[str, str] = field(default_factory=dict)
    revision: tuple[str, int, int, str] | None = ("reported", 1, 7, "1:stand-in")
    error: str | None = None
    calls: list[int] = field(default_factory=list)

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        if key is None:
            return None
        label = "unverified" if self.error else self.labelled.get(key.resource_key, "unverified")
        f = {"label": label, "signer": None, "fired_at": None, "operation_id": None}
        return {**f, "credential": None, "fields": {n: dict(f) for n in key.values}}

    def label(self, key: LabelKey | None) -> str:
        p = self.provenance(key)
        return p["label"] if p else "unverified"


def _fake_spore_key(stored: Mapping[str, Any]) -> LabelKey:
    # keyed on the id with one field, to test render apart from the spore key function
    return LabelKey(f"spore:{stored['id']}", {"text": stored["text"]})


@pytest.fixture
def rig(tmp_path: Path, monkeypatch) -> Rig:
    monkeypatch.setattr(providers, "spore_label_key", _fake_spore_key)
    return Rig(tmp_path)


def _attach(rig: Rig, snap: FakeLabels) -> FakeLabels:
    def fn() -> FakeLabels:
        snap.calls.append(1)
        return snap
    rig.ck.attach_labeller(fn)
    return snap


# --- (1) the section label is over the text read_section returns ---------------------------------


def test_a_section_edit_labels_the_canonical_body_read_section_returns(tmp_path: Path) -> None:
    """§0.5: the label is written over canonical_section_markdown(body), and render hashes the text
    read_section returns; the two must be one string, and the settle must name ``body``."""
    rig = Rig(tmp_path)
    db = Path(rig.src.anneal.episodic_db)
    body = "  \r\nfirst line   \r\n- second\t \r\n\r\n"
    with Store(str(db), read_only=True) as st:
        _text, version = st.read_section("State")
    (effect,) = section_effects("State", version, body)
    store = SectionEffectStore(db)
    result = store.apply_unit(ApplyUnit("sections", (0,), (effect,)))
    assert result.outcome is ApplyOutcome.APPLIED
    with Store(str(db), read_only=True) as st:
        text, _v = st.read_section("State")
    assert text == canonical_section_markdown(body)
    assert effect.projection["body"] == text
    assert store.label_fields(effect, result.members[0].stored) == ("body",)
    key = providers.section_label_key("State")({"markdown": text, "headline": "State"})
    assert key is not None and key.values == {"body": effect.projection["body"]}


# --- (2) a labelled row renders its provenance; the etag carries the revision ----------------------


def test_a_labelled_row_carries_its_provenance_and_the_etag_moves(rig: Rig) -> None:
    seed = f"spore:{rig.ids['seed']}"
    before = rig.ck.panel("tray", credential_class="token")
    assert all(r["provenance"] is None for r in before["rows"])
    snap = _attach(rig, FakeLabels({seed: "verified"}))
    after = rig.ck.panel("tray", credential_class="token")
    assert len(snap.calls) == 1                                     # one snapshot for the request
    by_id = {r["id"]: r for r in after["rows"]}
    assert by_id[seed]["provenance"]["label"] == "verified"
    assert by_id[seed]["provenance"]["fields"] == {"text": {"label": "verified", "signer": None,
                                                            "fired_at": None, "operation_id": None}}
    assert all(r["provenance"]["label"] == "unverified" for i, r in by_id.items() if i != seed)
    assert after["etag"] != before["etag"]
    # a panel that declares no label key renders as read
    assert all(r["provenance"] is None
               for r in rig.ck.panel("episodes", credential_class="token")["rows"])
    # the now view takes its rows' provenance from the same snapshot
    now = {r["id"]: r for r in rig.ck.panel("now", credential_class="token")["rows"]}
    if seed in now:
        assert now[seed]["provenance"]["label"] == "verified"


def test_a_failing_labeller_is_an_error_snapshot_never_a_failed_panel(rig: Rig) -> None:
    clean = rig.ck.panel("tray", credential_class="token")

    def boom() -> FakeLabels:
        raise RuntimeError("journal busy")
    rig.ck.attach_labeller(boom)
    out = rig.ck.panel("tray", credential_class="token")
    assert out["status"] == clean["status"] and out["rows"]
    assert "provenance unavailable: RuntimeError: journal busy" in out["note"]
    assert all(r["provenance"]["label"] == "unverified" for r in out["rows"])
    assert out["etag"] != clean["etag"]


# --- (3) the label reaches the tier, at render and at issue ----------------------------------------


def test_a_verified_held_row_snoozes_earlier_as_c1_at_render_and_issue(rig: Rig) -> None:
    """``_date_tier``: on an UNVERIFIED held row an earlier date or a clear is T2 (K2a); on a verified
    one it is C1. The label must reach tier_fn at issue (dispatch's fresh snapshot) and at render."""
    held = f"spore:{rig.ids['held']}"
    assert rig.tier("spore_surface_at", "tray", "held", {"surface_at": None}) == "T2"
    snap = _attach(rig, FakeLabels({held: "verified"}))
    snap.calls.clear()
    assert rig.tier("spore_surface_at", "tray", "held", {"surface_at": None}) == "C1"
    # Rig.tier reads the row once itself (one snapshot) and dispatch takes its own fresh one
    assert len(snap.calls) == 2
    row = rig.ck.read_one("tray", held).value
    assert row["provenance"]["label"] == "verified"
    acts = {a["verb"]: a for a in row_actions(rig.reg, rig.ck, "tray", row, "token", INSTALL_CLASS, rig.ck.today(),
                                                    policy=rig.ck.authority().policy)}
    assert acts["spore_surface_at"]["escalates"] == []
    snap.labelled = {}
    row = rig.ck.read_one("tray", held).value
    acts = {a["verb"]: a for a in row_actions(rig.reg, rig.ck, "tray", row, "token", INSTALL_CLASS, rig.ck.today(),
                                                    policy=rig.ck.authority().policy)}
    assert acts["spore_surface_at"]["escalates"]


def test_retier_waits_on_an_unreadable_journal_only_in_a_governed_install(rig: Rig) -> None:
    held = f"spore:{rig.ids['held']}"
    target = {"kind": "row", "panel_id": "tray", "row_id": held}
    _attach(rig, FakeLabels({held: "verified"}))
    assert retier(rig.reg, rig.ck, "spore_surface_at", {"surface_at": None}, target) == "C1"
    _attach(rig, FakeLabels({held: "verified"}, revision=None, error="provenance unavailable: busy"))
    assert retier(rig.reg, rig.ck, "spore_surface_at", {"surface_at": None}, target) == "T2"   # reported
    rig.ck.attach_verbs(VerbView(rig.reg, rig.ck, "governed"))
    assert retier(rig.reg, rig.ck, "spore_surface_at", {"surface_at": None}, target) is None
