"""The State write routed through anneal's ``Store.replace_section`` (a real Store in tmp), its file-edit
fallback for a store without the method, a stale refusal, and undo through the store."""

from __future__ import annotations

from pathlib import Path

import pytest
from anneal_memory import Store

from levain.writes import EditError, WriteScope, apply_edit
from tests.test_writes import _make_install, _section_body, _state_req

CONT = """# Memory

## State

old state

## Durable Facts

fact one

## Patterns

pat

## Decisions

dec

## Context

ctx
"""


@pytest.fixture()
def rig(tmp_path: Path):
    root = _make_install(tmp_path)
    db = root / ".levain" / "memory.db"
    with Store(str(db)):
        pass
    cont = root / ".levain" / "memory.continuity.md"
    cont.write_text(CONT, encoding="utf-8")
    return root, db, cont, WriteScope.from_install_root(root)


def _state(db: Path) -> tuple[str, str]:
    with Store(str(db), read_only=True) as st:
        return st.read_section("State")


def _expected(cont: Path) -> str:
    from levain.dashboard import _split_sections
    return dict(_split_sections(cont.read_text(encoding="utf-8")))["State"]


def test_state_edit_goes_through_the_store(rig, monkeypatch) -> None:
    root, db, cont, scope = rig
    calls = []
    orig = Store.replace_section

    def spy(self, *a, **k):
        calls.append((a, k))
        return orig(self, *a, **k)

    monkeypatch.setattr(Store, "replace_section", spy)
    r = apply_edit(scope, _state_req(_expected(cont), "new state"))
    assert r["ok"] and r["heading"] == "State"
    assert len(calls) == 1 and "expected_version" in calls[0][1]
    assert _state(db)[0].strip() == "new state"
    text = cont.read_text(encoding="utf-8")
    assert "fact one" in text and "## Durable Facts" in text
    rec = (root / ".levain" / "edits.jsonl").read_text().splitlines()[-1]
    assert '"via": "store"' in rec


def test_fallback_when_store_lacks_replace_section(rig, monkeypatch) -> None:
    root, db, cont, scope = rig
    monkeypatch.delattr(Store, "replace_section")
    r = apply_edit(scope, _state_req(_expected(cont), "file edited"))
    assert r["ok"]
    assert _section_body(cont.read_text(encoding="utf-8"), "State") == "file edited"
    assert '"via"' not in (root / ".levain" / "edits.jsonl").read_text()


def test_stale_expected_body_is_refused_409_and_leaves_no_trace(rig) -> None:
    root, db, cont, scope = rig
    with pytest.raises(EditError) as ei:
        apply_edit(scope, _state_req("not what is there", "x"))
    assert ei.value.code == "stale" and ei.value.http_status == 409
    assert _state(db)[0].strip() == "old state"
    assert not (root / ".levain" / "edits.jsonl").exists()
    assert not (root / ".levain" / "backups").exists() or not list((root / ".levain" / "backups").iterdir())


def test_a_version_race_is_refused_stale(rig, monkeypatch) -> None:
    """Another writer lands between our read_section and replace_section: anneal's version check refuses."""
    root, db, cont, scope = rig
    orig = Store.replace_section

    def racing(self, heading, body, *, expected_version):
        orig(self, heading, "someone else", expected_version=expected_version)
        return orig(self, heading, body, expected_version=expected_version)

    monkeypatch.setattr(Store, "replace_section", racing)
    with pytest.raises(EditError) as ei:
        apply_edit(scope, _state_req(_expected(cont), "mine"))
    assert ei.value.code == "stale" and ei.value.http_status == 409
    assert _state(db)[0].strip() == "someone else"


def test_open_wrap_is_refused_in_the_operator_shape(rig) -> None:
    root, db, cont, scope = rig
    with Store(str(db)) as w:
        w.wrap_started(token="t", episode_ids=[])
    with pytest.raises(EditError) as ei:
        apply_edit(scope, _state_req(_expected(cont), "during wrap"))
    assert ei.value.code == "wrap_in_progress" and ei.value.http_status == 409
    assert _state(db)[0].strip() == "old state"


def test_undo_restores_the_section_through_the_store(rig, monkeypatch) -> None:
    root, db, cont, scope = rig
    before = cont.read_text(encoding="utf-8")
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    calls = []
    orig = Store.replace_section
    monkeypatch.setattr(Store, "replace_section",
                        lambda self, *a, **k: (calls.append(1), orig(self, *a, **k))[1])
    u = apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert u["ok"] and u["undid"] == r["id"] and len(calls) == 1
    assert _state(db)[0].strip() == "old state"
    assert cont.read_text(encoding="utf-8") == before
    with pytest.raises(EditError) as ei:  # already undone -> not the latest change
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.http_status == 409


def test_undo_after_a_newer_change_is_refused(rig) -> None:
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    text, version = _state(db)
    with Store(str(db)) as w:
        w.replace_section("State", "newer", expected_version=version)
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "stale" and ei.value.http_status == 409
    assert _state(db)[0].strip() == "newer"


def test_undo_of_a_store_edit_without_the_store_says_so(rig, monkeypatch) -> None:
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    monkeypatch.delattr(Store, "replace_section")
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "not_undoable"
    assert "edited" in cont.read_text(encoding="utf-8")
