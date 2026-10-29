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


# --- FIX ROUND 1: the result identity is the store's; undo is guarded by the State section version --------

def _rows(root: Path) -> list[dict]:
    import json
    p = root / ".levain" / "edits.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _backup_dirs(root: Path) -> list[Path]:
    b = root / ".levain" / "backups"
    return sorted(b.iterdir()) if b.exists() else []


def _wrap_rewrite(cont: Path, *, state: str | None, facts: str) -> None:
    """A consolidation landing: rewrites the continuity file, other sections changed, State as given."""
    text = cont.read_text(encoding="utf-8").replace("fact one", facts)
    if state is not None:
        text = text.replace(_expected(cont), state)
    cont.write_text(text, encoding="utf-8")


def test_a_wrap_between_the_write_and_any_reread_is_not_recorded_as_this_edit(rig, monkeypatch) -> None:
    """A wrap lands the moment anneal releases its lock: the audit row still names the version anneal
    returned for OUR body, and undo then refuses instead of restoring over the wrap's State."""
    root, db, cont, scope = rig
    orig = Store.replace_section
    seen = []

    def then_wrap(self, *a, **k):
        res = orig(self, *a, **k)
        seen.append(res.version)
        _wrap_rewrite(cont, state="wrap state", facts="wrap fact")
        return res

    monkeypatch.setattr(Store, "replace_section", then_wrap)
    r = apply_edit(scope, _state_req(_expected(cont), "new state"))
    monkeypatch.setattr(Store, "replace_section", orig)
    rec = _rows(root)[-1]
    assert rec["section_version"] == seen[0] and "new_sha256" not in rec
    assert rec["section_version"] != _state(db)[1]  # the wrap's State is not this edit's result
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "stale" and ei.value.http_status == 409
    assert _state(db)[0].strip() == "wrap state" and "wrap fact" in cont.read_text(encoding="utf-8")
    assert len(_rows(root)) == 1 and len(_backup_dirs(root)) == 1  # the refused undo left no trace


def test_undo_after_a_state_preserving_wrap_restores_state_alone(rig) -> None:
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    _wrap_rewrite(cont, state=None, facts="wrap fact")
    u = apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert u["ok"] and u["undid"] == r["id"]
    text = cont.read_text(encoding="utf-8")
    assert _state(db)[0].strip() == "old state"
    assert "wrap fact" in text and "fact one" not in text  # the wrap's other sections stand
    undo_row = _rows(root)[-1]
    assert undo_row["prev_section_version"] == _rows(root)[0]["section_version"]
    assert undo_row["section_version"] == _state(db)[1]


def test_undo_after_a_state_changing_wrap_is_refused_409(rig) -> None:
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    _wrap_rewrite(cont, state="consolidated", facts="wrap fact")
    before = cont.read_text(encoding="utf-8")
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "stale" and ei.value.http_status == 409
    assert cont.read_text(encoding="utf-8") == before
    assert len(_rows(root)) == 1 and len(_backup_dirs(root)) == 1


def test_a_failed_audit_append_after_the_edit_says_committed_and_keeps_the_backup(rig, monkeypatch) -> None:
    import levain.writes as w
    root, db, cont, scope = rig

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(w, "_append_audit", boom)
    with pytest.raises(EditError) as ei:
        apply_edit(scope, _state_req(_expected(cont), "landed"))
    e = ei.value
    assert e.code == "committed_unaudited" and "COMMITTED" in str(e)
    assert e.extra["committed"] is True and e.extra["section_version"] == _state(db)[1]
    assert _state(db)[0].strip() == "landed"
    kept = root / ".levain" / e.extra["backup"]
    assert kept.is_file() and "old state" in kept.read_text() and e.extra["backup"] in str(e)


def test_a_failed_audit_append_after_an_undo_says_committed_and_keeps_the_backup(rig, monkeypatch) -> None:
    import levain.writes as w
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    monkeypatch.setattr(w, "_append_audit", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "committed_unaudited" and ei.value.extra["committed"] is True
    assert _state(db)[0].strip() == "old state"
    assert (root / ".levain" / ei.value.extra["backup"]).is_file()


def test_close_raising_after_a_written_result_is_success_with_a_warning(rig, monkeypatch) -> None:
    from anneal_memory import StoreError
    root, db, cont, scope = rig
    orig_close = Store.close
    armed = [True]

    def close_then_raise(self):
        orig_close(self)
        if armed[0]:  # the edit's own Store is the only one apply_edit opens
            armed[0] = False
            raise StoreError("unable to close due to unfinalized statements", operation="close")

    monkeypatch.setattr(Store, "close", close_then_raise)
    r = apply_edit(scope, _state_req(_expected(cont), "closed badly"))
    monkeypatch.setattr(Store, "close", orig_close)
    assert r["ok"] and "closing the anneal store" in r["warning"]
    assert _state(db)[0].strip() == "closed badly"
    rec = _rows(root)[-1]
    assert rec["section_version"] == _state(db)[1] and rec["warning"] == r["warning"]
    assert (root / ".levain" / rec["backup"]).is_file()
    assert apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})["ok"]
    assert _state(db)[0].strip() == "old state"


def test_close_raising_after_a_refusal_reports_the_refusal(rig, monkeypatch) -> None:
    from anneal_memory import StoreError
    root, db, cont, scope = rig
    with Store(str(db)) as wr:
        wr.wrap_started(token="t", episode_ids=[])
    orig_close = Store.close

    def close_then_raise(self):
        orig_close(self)
        raise StoreError("close failed", operation="close")

    monkeypatch.setattr(Store, "close", close_then_raise)
    with pytest.raises(EditError) as ei:
        apply_edit(scope, _state_req(_expected(cont), "during wrap"))
    monkeypatch.setattr(Store, "close", orig_close)
    assert ei.value.code == "wrap_in_progress"
    assert not _rows(root) and not _backup_dirs(root)


def test_open_wrap_refusal_leaves_no_backup_or_audit(rig) -> None:
    root, db, cont, scope = rig
    with Store(str(db)) as wr:
        wr.wrap_started(token="t", episode_ids=[])
    with pytest.raises(EditError):
        apply_edit(scope, _state_req(_expected(cont), "during wrap"))
    assert not _rows(root) and not _backup_dirs(root)


def test_undo_of_a_store_row_without_a_section_version_is_refused(rig) -> None:
    import json
    root, db, cont, scope = rig
    r = apply_edit(scope, _state_req(_expected(cont), "edited"))
    p = root / ".levain" / "edits.jsonl"
    rec = json.loads(p.read_text().splitlines()[-1])
    rec.pop("section_version")
    p.write_text(json.dumps(rec) + "\n")
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "undo", "edit_id": r["id"]})
    assert ei.value.code == "not_undoable"
    assert _state(db)[0].strip() == "edited"
