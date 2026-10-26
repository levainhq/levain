"""S1 3b-8a fix round 3 (head rulings F1, F3, S-1 on the s3b8a L3 r2 triage).

F1: ``prepare_front_log`` acts below ``L/gw/`` only when it is root's alone (``seams.trusted``), as
``write_front_policy`` does for ``R/gw/``. F3: the front's log writer runs on its event loop only. S-1: a missing
``L/gw/slot<n>/`` (an install made before the store log dir existed) is refused ``store-missing`` naming
``levain repair``, and ``levain doctor`` reports it with that remedy."""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from levain import doctor
from levain.broker import front as FR
from levain.slot import layout
from levain.slot import runtime_helper as H
from levain.slot.grammar import parse_install_argv
from tests.test_slot_3b8a_front_wiring import _world
from tests.test_slot_s2f2_runtime_helper import _blob, _prepared


# --- F1: L/gw must be root's alone ------------------------------------------------------------------------------------

@pytest.mark.parametrize("plant", ["group-writable", "symlink"])
def test_an_untrusted_store_gw_is_refused_before_the_log_is_touched(tmp_path, plant):
    w = _world(tmp_path)
    gw = w.L / "gw"
    if plant == "group-writable":
        os.chmod(gw, 0o775)
    else:
        moved = tmp_path / "gw-elsewhere"
        os.rename(gw, moved)
        os.symlink(moved, gw)
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"}, w.last_err
    assert w.fronts.procs == {}
    assert all(rel != "gw/slot0/front.log" for rel, _u, _g in w.chowns)   # nothing chowned below an untrusted L/gw


def test_prepare_front_log_checks_store_gw_itself(tmp_path):
    """At the point of use: called directly, not only through render's earlier checks."""
    w = _world(tmp_path)
    os.chmod(w.L / "gw", 0o777)
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.prepare_front_log(store, H.SlotId("egress", 0), 898)
    finally:
        store.close()
    assert e.value.name == "bad-root" and "gw" in str(e.value)


# --- F3: the log writer runs on the event loop only --------------------------------------------------------------------

def test_every_log_write_is_made_on_the_event_loop():
    """complement LOW: ``Front._append`` is not atomic across threads, so it must never be reached from one. Every call
    of ``_log`` / ``_append`` in the front's module sits directly in an ``async def`` (a coroutine runs on the loop's
    thread), except ``_append``'s one call, inside ``_log``; the resolver's thread (``system_resolve``'s ``work``) and
    every other plain function call neither."""
    tree = ast.parse(Path(FR.__file__).read_text())
    seen = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr in ("_log", "_append"):
                seen.append((child.func.attr, fn.name if fn else None, isinstance(fn, ast.AsyncFunctionDef)))
            walk(child, fn)

    walk(tree, None)
    assert sum(1 for a, _f, _c in seen if a == "_log") >= 5            # the probe found the callers it pins
    bad = [s for s in seen if not (s[2] or (s[0] == "_append" and s[1] == "_log"))]
    assert bad == [], bad
    assert [s for s in seen if s[0] == "_append"] == [("_append", "_log", False)]


# --- S-1: a missing L/gw/slot<n>/ names its remedy --------------------------------------------------------------------

def test_repair_line_is_a_command_the_install_helper_parses():
    for host in ("linux", "darwin"):
        line = layout.repair_line(host)
        sudo, helper, *argv = line.split()
        assert sudo == "sudo" and helper == str(layout.install_helper_path(host))
        assert parse_install_argv(argv).verb == "repair"


@pytest.mark.parametrize("gone", ["slot0", "gw"])
def test_a_missing_store_log_dir_is_refused_naming_repair(tmp_path, gone):
    """An install made before b732b63a has neither ``L/gw/`` nor ``L/gw/slot<n>/``."""
    w = _world(tmp_path)
    os.rmdir(w.L / "gw" / "slot0")
    if gone == "gw":
        for d in (w.L / "gw").iterdir():
            os.rmdir(d)
        os.rmdir(w.L / "gw")
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "store-missing"} and w.fronts.procs == {}
    assert layout.repair_line(w.seams.host) in w.last_err and "gw/slot0" in w.last_err, w.last_err


def _doctor_store(tmp: Path, tags: dict) -> Path:
    from tests.test_doctor_slot_isolation import _store
    live = [t for t, st in tags.items() if st != "retired"]
    return _store(tmp, tags, pool=(sum(1 for t in live if t[0] == "e"), sum(1 for t in live if t[0] == "v")))


def _front_row(store: Path):
    rows = {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30)}
    return rows.get("agent front logs"), rows


def test_doctor_reports_a_missing_slot_log_dir_with_the_repair_line(tmp_path):
    store = _doctor_store(tmp_path, {"e0": "reusable", "e1": "leased", "e2": "retired", "v1": "reusable"})
    (store / "gw" / "slot0").mkdir(parents=True)
    row, rows = _front_row(store)
    assert row is not None and not row.ok and row.upgrade_pending
    assert "e1" in row.detail and "e0" not in row.detail and "e2" not in row.detail   # a tombstone is not counted
    assert layout.repair_line("linux") in row.hint
    assert rows["agent slots"].ok                                                      # the slots row is kept


def test_doctor_front_log_row_green_absent_and_linked(tmp_path):
    store = _doctor_store(tmp_path / "a", {"e0": "reusable", "e1": "reusable"})
    for n in (0, 1):
        (store / "gw" / f"slot{n}").mkdir(parents=True)
    row, _ = _front_row(store)
    assert row.ok and not row.warn and "2 egress slot" in row.detail
    # no egress slot (a verifier-only pool): no row at all
    assert _front_row(_doctor_store(tmp_path / "b", {"v0": "reusable"}))[0] is None
    # L/gw is a link: the runtime refuses it, so the doctor does too, never green through it
    store = _doctor_store(tmp_path / "c", {"e0": "reusable"})
    (tmp_path / "realgw" / "slot0").mkdir(parents=True)
    os.symlink(tmp_path / "realgw", store / "gw")
    row, _ = _front_row(store)
    assert not row.ok and "symbolic link" in row.detail
    # a whole store with no gw/ (made before it existed): every egress slot named
    store = _doctor_store(tmp_path / "d", {"e0": "reusable", "e3": "scrubbed"})
    row, _ = _front_row(store)
    assert not row.ok and "e0, e3" in row.detail
