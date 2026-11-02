"""The notification policy was RETIRED by presence 1b: the kind -> class map stays, the cells are replaced by each preset's
`lets_through`, and the old cells file is only read by the migration (which reports it and keeps it in the backup)."""
from __future__ import annotations

import json

import pytest

from levain.webpush import intent, presence
from levain.webpush.presence import DEFAULT_CELLS

NOW = 1_000_000.0
ACTIVE, INACTIVE = 10.0, intent.FOLLOW_DEFAULT + 1.0
PHILL = {"quiet": dict(DEFAULT_CELLS["quiet"]), "sleep": {"approval": "push", "attention": "push", "other": "hold"}}
HOLD_ALL = {"quiet": {"approval": "hold", "attention": "hold", "other": "hold"},
            "sleep": {"approval": "hold", "attention": "hold", "other": "hold"}}


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _write(tmp_path, text):
    d = tmp_path / "webpush"
    d.mkdir(exist_ok=True)
    (d / "policy.json").write_text(text if isinstance(text, str) else json.dumps(text))


def _file(cells):
    return {"version": 1, "cells": cells}


def test_class_map_and_urgent_kinds_derive_from_it():
    # `dated` is a deliberate NEW kind (flow sends it for dated items), not part of the old rule; it is class `attention`.
    for k in ("consent", "push", "reping"):
        assert presence.kind_class(k) == "approval"
    for k in ("attention", "dated"):
        assert presence.kind_class(k) == "attention"
    for k in ("question", "ruling", "summary", "ping", "test", "never-heard-of-it", None, 7):
        assert presence.kind_class(k) == "other"
    assert presence.URGENT_KINDS == {k for k, c in presence.KIND_CLASSES.items() if c != "other"}
    assert "dated" in presence.URGENT_KINDS


@pytest.mark.parametrize("idle", [ACTIVE, INACTIVE, None])
@pytest.mark.parametrize("preset", ["quiet", "sleep"])
def test_the_mutes_let_approvals_and_attention_through_and_hold_the_rest_whatever_the_laptop_does(tmp_path, preset, idle):
    """The cells' job is the presets' lets_through now (Phill 10-25: in sleep an approval or high-priority ping still reaches him)."""
    intent.apply_preset(tmp_path, preset, now=NOW, idle=lambda: 5.0)
    got = {c: presence.should_push(tmp_path, k, now=NOW + 1, idle=lambda: idle, suggest=False)
           for c, k in (("approval", "consent"), ("attention", "attention"), ("other", "question"))}
    assert got["approval"].push and got["attention"].push
    assert (got["other"].push, got["other"].state, got["other"].by) == (False, "muted", preset)
    assert intent.PRESET_TABLE[preset]["lets_through"] == "needs"
    assert {c for c in presence.CLASSES if got[c].push} == set(intent.LETS_CLASSES["needs"])


def test_a_policy_file_on_disk_decides_nothing_any_more(tmp_path):
    """The decision reads the record only: a cells file that holds every class is ignored (the mutation: reading it again)."""
    intent.apply_preset(tmp_path, "sleep", now=NOW, idle=lambda: 5.0)
    _write(tmp_path, _file(HOLD_ALL))
    for kind in ("consent", "attention", "dated"):
        assert presence.should_push(tmp_path, kind, now=NOW + 1, idle=lambda: INACTIVE, suggest=False).push is True, kind
    for kind in ("question", "mystery"):
        assert presence.should_push(tmp_path, kind, now=NOW + 1, idle=lambda: INACTIVE, suggest=False).push is False, kind


def test_read_policy_default_loaded_and_malformed(tmp_path):
    """The reader stays for the migration (it reports a non-default table)."""
    assert presence.read_policy(tmp_path) == (DEFAULT_CELLS, "default")
    assert presence.read_policy(None)[1] == "default"
    _write(tmp_path, _file(HOLD_ALL))
    assert presence.read_policy(tmp_path) == (HOLD_ALL, "loaded")
    for bad in ("{nope", "[]", json.dumps({"version": 2, "cells": PHILL}), json.dumps({"version": True, "cells": PHILL}),
                json.dumps({"version": 1}), " " * 5000, json.dumps({"version": 1, "cells": PHILL, "pad": "x" * 5000})):
        _write(tmp_path, bad)
        cells, status = presence.read_policy(tmp_path)
        assert cells == DEFAULT_CELLS and status.startswith("malformed"), bad[:30]


def test_a_bad_cell_makes_the_whole_file_the_default(tmp_path):
    cells = {"quiet": {"approval": "hold", "attention": "banana", "other": "push"},
             "sleep": {"approval": "push", "other": None}}
    _write(tmp_path, _file(cells))
    got, status = presence.read_policy(tmp_path)
    assert status.startswith("malformed") and "quiet.attention" in status and "sleep.other" in status
    assert "whole default" in status
    assert got == DEFAULT_CELLS   # no valid cell of a malformed file stands


def test_policy_view_is_the_retirement_view_and_the_setters_refuse_with_the_pointer(tmp_path):
    v = presence.policy_view(tmp_path)
    assert v["retired"] is True and v["status"] == "retired" and "levain ping lets" in v["message"]
    assert v["cells"] == DEFAULT_CELLS and v["default_class"] == "other" and v["classes"] == presence.KIND_CLASSES
    assert v["presets"]["sleep"] == {"devices": "on", "lets_through": "needs", "until": "input"}
    with pytest.raises(presence.PresenceError, match="levain ping lets"):
        presence.set_policy(tmp_path, "quiet", "other", "push")
    with pytest.raises(presence.PresenceError, match="levain ping lets"):
        presence.reset_policy(tmp_path)
    assert not (tmp_path / "webpush" / "policy.json").exists()


def test_the_migration_reports_a_non_default_table_line_by_line_and_keeps_the_file_in_the_backup(tmp_path):
    _write(tmp_path, _file({"quiet": {"approval": "push", "attention": "hold", "other": "hold"}, "sleep": HOLD_ALL["sleep"]}))
    raw = (tmp_path / "webpush" / "policy.json").read_bytes()
    got = intent.migrate(tmp_path, now=NOW)
    assert got["status"] == "migrated"
    text = "\n".join(got["lines"])
    assert "your quiet held attention items; the quiet preset now pushes them by default" in text
    assert "your sleep held approvals; the sleep preset now pushes them by default" in text
    assert not (tmp_path / "webpush" / "policy.json").exists()
    assert (tmp_path / "webpush" / intent.BACKUP_DIR).is_dir()
    assert intent.unmigrate(tmp_path, now=NOW + 1)["status"] == "undone"
    assert (tmp_path / "webpush" / "policy.json").read_bytes() == raw
