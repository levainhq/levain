"""TS-64: an intention's ``next`` may be a date or anneal 1.0.4's timed ``YYYY-MM-DDTHH:MM`` (local, no zone,
minute precision). Both write-path validators pass either through verbatim and still refuse garbage."""
from __future__ import annotations

import pytest
from anneal_memory.spores import _validate_date

from levain.cockpit.effects import _norm_date
from levain.writes import EditError, _validate_surface_at

OK = ["2026-11-07", "2026-11-07T09:30", "2026-11-07T00:00", "2026-11-07T23:59"]
BAD = ["2026-11-07T24:00", "2026-11-07T09:60", "2026-13-01", "2026-11-31T09:30", "2026-11-07T9:30",
       "2026-11-07T09:30:00", "2026-11-07T09:30Z", "2026-11-07 09:30", "2026-11-07T09:30 ", " 2026-11-07",
       "2026-06- 1", "tomorrow", "2026-11-07T09", 20261107, ["2026-11-07"]]


@pytest.mark.parametrize("v", OK)
def test_surface_at_accepts_date_and_timed_verbatim(v):
    assert _validate_surface_at(v) == v == _validate_date(v)


@pytest.mark.parametrize("v", OK)
def test_norm_date_accepts_date_and_timed_verbatim(v):
    assert _norm_date(v) == v


@pytest.mark.parametrize("v", [None, ""])
def test_both_clear_on_null_or_empty(v):
    assert _validate_surface_at(v) is None
    assert _norm_date(v) is None


@pytest.mark.parametrize("v", BAD)
def test_surface_at_refuses_garbage(v):
    with pytest.raises(EditError) as ei:
        _validate_surface_at(v)
    assert ei.value.code == "bad_verb_arg" if hasattr(ei.value, "code") else True


@pytest.mark.parametrize("v", BAD)
def test_norm_date_refuses_garbage(v):
    with pytest.raises(ValueError):
        _norm_date(v)


@pytest.mark.parametrize("v", BAD)
def test_levain_refuses_everything_anneal_refuses(v):
    # never wider than the store: whatever levain passes, anneal's own validator accepts
    for fn in (_validate_surface_at, _norm_date):
        try:
            out = fn(v)
        except (EditError, ValueError):
            continue
        assert _validate_date(out) == out
