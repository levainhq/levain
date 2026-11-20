"""The doctor's ``dispatch ceiling`` row (levain#199): with no ``L/policy/dispatch_ceiling`` every mint refuses
``ticket-ceiling:file``, and nothing said which command ends it. The row names the install helper's ``set-ceiling``
and a minimal document; it never writes the file (the fail-closed default stands)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from levain import doctor
from levain.slot import layout
from levain.slot import tickets as T

ME = os.getuid()
DOC = {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 1000000,
       "max_open_tickets": 4, "max_runs_per_day": 50}


def _store(tmp: Path) -> Path:
    store = tmp / "store"
    (store / "policy").mkdir(parents=True)
    os.chmod(store / "policy", 0o755)
    return store


def _rows(store: Path):
    return doctor._check_dispatch_ceiling(host="linux", store=store, owner=ME)


def test_an_absent_ceiling_names_set_ceiling_and_a_document(tmp_path):
    store = _store(tmp_path)
    rows = _rows(store)
    assert len(rows) == 1 and rows[0].name == "dispatch ceiling"
    r = rows[0]
    assert r.ok and r.warn
    hint = r.hint or ""
    assert f"sudo {layout.install_helper_path('linux')} set-ceiling" in hint
    assert T.CEILING_FORMAT in hint
    assert "ticket-ceiling:file" in r.detail


def test_the_hinted_document_is_one_the_ticket_store_accepts(tmp_path):
    hint = _rows(_store(tmp_path))[0].hint or ""
    start = hint.index("{")
    doc = hint[start:hint.index("}", start) + 1]
    T.parse_ceiling(doc.encode())      # raises Refused when set-ceiling would refuse it


def test_doctor_never_writes_the_ceiling(tmp_path):
    store = _store(tmp_path)
    _rows(store)
    assert not (store / "policy" / "dispatch_ceiling").exists()


def test_a_present_valid_ceiling_leaves_no_row(tmp_path):
    store = _store(tmp_path)
    p = store / "policy" / "dispatch_ceiling"
    p.write_text(json.dumps(DOC, sort_keys=True, separators=(",", ":")))
    os.chmod(p, 0o644)
    assert _rows(store) == []


def test_a_present_unusable_ceiling_is_a_row_with_the_same_remedy(tmp_path):
    store = _store(tmp_path)
    p = store / "policy" / "dispatch_ceiling"
    p.write_text("not json")
    os.chmod(p, 0o644)
    rows = _rows(store)
    assert len(rows) == 1 and "set-ceiling" in (rows[0].hint or "")


def test_no_isolation_install_no_row(tmp_path):
    assert doctor._check_dispatch_ceiling(host="linux", store=tmp_path / "absent", owner=ME) == []
