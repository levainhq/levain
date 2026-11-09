"""The brokered-link doctor rows: per-run dollars against the dispatch ceiling, and a brokered route for the model."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from levain import doctor
from levain.contracts import canon
from levain.slot import tickets as T

L = doctor._ApiKeyLink


def _route(rid: str, kind: str, *models: str):
    return rid, SimpleNamespace(policy=SimpleNamespace(type=kind), backends={m: m for m in models})


def _map(*entries):
    return SimpleNamespace(routes=dict(entries), generation=1, digests={})


def _rows(links, limit=5_000_000, active=None, store=Path("/nonexistent-store")):
    active = active if active is not None else _map(_route("or", "brokered", "m1"))
    return doctor._check_brokered_links(Path("/x"), host="linux", store=store, links=lambda: links,
                                        ceiling=(limit if callable(limit) else (lambda: limit)),
                                        routes=(active if callable(active) else (lambda: active)))


def test_a_link_over_the_ceiling_fails():
    rows = _rows([L("al-1", 5_000_001, "m1")])
    assert not rows[0].ok and "per-run $5.00" in rows[0].detail and "vs ceiling $5.00" in rows[0].detail
    assert rows[1].ok


def test_a_link_equal_to_the_ceiling_passes():
    rows = _rows([L("al-1", 5_000_000, "m1")])
    assert rows[0].ok and not rows[0].warn and all(r.ok for r in rows)


def test_a_model_with_no_brokered_route_fails():
    rows = _rows([L("al-1", 1_000_000, "m9")])
    assert rows[0].ok
    assert not rows[1].ok and "no brokered route for model m9" in rows[1].detail


def test_a_non_brokered_route_does_not_serve_the_model():
    rows = _rows([L("al-1", 1_000_000, "m1")], active=_map(_route("pass", "passthrough", "m1")))
    assert not rows[1].ok


def test_no_links_is_neutral():
    rows = doctor._check_brokered_links(Path("/x"), host="linux", store=Path("/s"), links=lambda: [])
    assert len(rows) == 1 and rows[0].neutral and rows[0].ok


def test_an_unreadable_store_warns_not_crashes():
    def boom():
        raise OSError("unreadable")
    rows = doctor._check_brokered_links(Path("/x"), host="linux", store=Path("/s"), links=boom)
    assert len(rows) == 1 and rows[0].ok and rows[0].warn and "could not be read" in rows[0].detail


def test_an_unreadable_ceiling_and_map_warn_per_link():
    def boom():
        raise OSError("no")
    rows = _rows([L("al-1", 1, "m1")], limit=boom, active=boom)
    assert [r.warn for r in rows] == [True, True] and all(r.ok for r in rows)


def test_the_real_ceiling_file_is_the_one_read(tmp_path):
    (tmp_path / "policy").mkdir()
    f = tmp_path / "policy" / "dispatch_ceiling"
    f.write_bytes(canon.canonical_bytes({"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 2_000_000,
                                         "max_open_tickets": 4, "max_runs_per_day": 50}))
    os.chmod(f, 0o644)
    kw = dict(host="linux", store=tmp_path, routes=lambda: _map(_route("or", "brokered", "m1")),
              ceiling_owner=os.getuid())
    over = doctor._check_brokered_links(tmp_path, links=lambda: [L("al-1", 2_000_001, "m1")], **kw)
    at = doctor._check_brokered_links(tmp_path, links=lambda: [L("al-1", 2_000_000, "m1")], **kw)
    assert not over[0].ok and at[0].ok and "$2.00" in at[0].detail
