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
from tests.test_routemaps import st  # noqa: F401
from tests.test_slot_p7a1_passthrough_ticket import pt_args
from tests.test_tickets import Clock, Rig, ticket_of

ME = os.getuid()
DOC = {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 1000000,
       "max_open_tickets": 4, "max_runs_per_day": 50}


def _store(tmp: Path) -> Path:
    store = tmp / "store"
    (store / "policy").mkdir(parents=True)
    os.chmod(store / "policy", 0o755)
    (store / "policy" / layout.operator_record_path("linux").name).write_text("{}")   # the install's last write
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


def _hinted_doc(hint: str) -> bytes:
    start = hint.index("{")
    return hint[start:hint.index("}", start) + 1].encode()


def _passthrough(store: Path) -> None:
    p = store / "policy" / layout.POLICY_DISPATCH_AUTH
    p.write_bytes(b"passthrough\n")
    os.chmod(p, 0o644)


def test_a_passthrough_install_hints_a_slash_two_document(tmp_path):
    store = _store(tmp_path)
    _passthrough(store)
    hint = _rows(store)[0].hint or ""
    c = T.parse_ceiling(_hinted_doc(hint))
    assert c.max_calls_per_run is not None and T.CEILING_FORMAT_2 in hint


def test_a_brokered_install_keeps_the_slash_one_document_and_says_a_subscription_run_needs_two(tmp_path):
    hint = _rows(_store(tmp_path))[0].hint or ""
    assert T.parse_ceiling(_hinted_doc(hint)).max_calls_per_run is None
    assert "/2" in hint


def test_the_passthrough_hint_passes_the_check_a_passthrough_mint_applies(tmp_path, st):
    store = _store(tmp_path)
    _passthrough(store)
    doc = _hinted_doc(_rows(store)[0].hint or "")
    (st.L / "policy").mkdir(exist_ok=True)
    (st.L / "policy" / "dispatch_ceiling").write_bytes(doc)
    (st.L / "policy" / "dispatch_ceiling").chmod(0o644)
    ticket_of(Rig(st, Clock()).ask("RUN-TICKET-MINT", **pt_args()))   # a /1 ceiling refuses ceiling-lacks-subscription


def test_a_retained_post_undo_store_gives_no_row(tmp_path):
    store = _store(tmp_path)
    (store / "policy" / layout.operator_record_path("linux").name).unlink()
    assert _rows(store) == []


def test_an_unreadable_ceiling_row_keeps_the_hint(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(T, "read_ceiling", boom)
    (row,) = _rows(store)
    assert "set-ceiling" in (row.hint or "")


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


def test_the_hint_says_the_operator_picks_the_figures(tmp_path):
    assert "not a recommendation" in (_rows(_store(tmp_path))[0].hint or "")


def test_run_doctor_prints_the_ceiling_row_when_the_ceiling_is_absent(tmp_path, capsys, monkeypatch):
    # deleting the row's core.extend line in _run_doctor turns this red
    store = _store(tmp_path)
    real = doctor._check_dispatch_ceiling
    monkeypatch.setattr(doctor, "_check_dispatch_ceiling", lambda: real(host="linux", store=store, owner=ME))
    doctor.run_doctor(tmp_path / "install")
    assert "dispatch ceiling:" in capsys.readouterr().out


def test_no_isolation_install_no_row(tmp_path):
    assert doctor._check_dispatch_ceiling(host="linux", store=tmp_path / "absent", owner=ME) == []


def test_a_malformed_dispatch_auth_file_keeps_the_row_and_its_hint(tmp_path, monkeypatch):
    """An unreadable dispatch_auth leaves the /1 hint; the row never degrades to a hint-less generic one."""
    import levain.slot.services.dispatch_run as DR
    store = _store(tmp_path)

    def boom(*_a, **_k):
        raise ValueError("malformed dispatch_auth")

    monkeypatch.setattr(DR, "read_dispatch_auth", boom)
    rows = _rows(store)
    assert len(rows) == 1 and "set-ceiling" in (rows[0].hint or "")
    assert T.CEILING_FORMAT in (rows[0].hint or "")


def test_a_valid_ceiling_never_reads_dispatch_auth(tmp_path, monkeypatch):
    import levain.slot.services.dispatch_run as DR
    store = _store(tmp_path)
    (store / "policy" / layout.policy_dispatch_ceiling_path("linux").name).write_text(json.dumps(DOC))
    os.chmod(store / "policy" / layout.policy_dispatch_ceiling_path("linux").name, 0o644)
    monkeypatch.setattr(DR, "read_dispatch_auth", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    assert _rows(store) == []
