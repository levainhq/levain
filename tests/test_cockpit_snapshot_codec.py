"""The _Snap <-> snapshot-body codec (levain.cockpit.snapshot_host).

The codec's one invariant is that the change-detection text of a decoded snapshot (``_Snap.etag_frags``) equals the
original's for every plain-JSON value; the rest pin the body's shape rules (no ``as_of``, no clock facets, a schema
version, a normalisation per non-JSON value, fresh containers on every decode). Every snapshot here is a real ``_Snap``,
and the last tests read the engine's own panels through a real refresh."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import time
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import snapshot_host as sh
from levain.cockpit import snapshots
from levain.cockpit.authority import LabelKey
from levain.cockpit.engine import Cockpit, ProviderSpec, _canon_text, _Snap
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.results import Read, RowIn
from tests.test_cockpit_k2a import _install

AS_OF = "2026-11-19T15:00:00Z"
ID = "ca1b"
FRAG_KEYS = ("rows", "value", "filtered", "skipped")


def _row(i: int, **facets: Any) -> dict[str, Any]:
    return {"id": f"r{i}", "version": f"v{i}", "group": "g", "title": f"row {i} é ü 漢字 \U0001f600",
            "truncated": False, "body": "line one\nline \"two\"" if i % 2 else None, "text_hidden": False,
            "facets": dict(facets), "emphasis": "none", "band": "now",
            "badges": [{"kind": "due", "value": "2026-11-12"}], "provenance": None, "actions": []}


def _snap(status: str = "ok", rows: Any = None, value: Any = None, **kw: Any) -> _Snap:
    base: dict[str, Any] = dict(filtered=[], skipped=[], as_of=AS_OF, error=None, note="")
    base.update(kw)
    return _Snap(status, rows, value, **base)


def _frags(s: _Snap) -> dict[str, str]:
    return {k: s.etag_frags()[k] for k in FRAG_KEYS}


def _via_store_bytes(status: str, body: dict[str, Any]) -> dict[str, Any]:
    """The body as a reader holds it: through the store's own byte encoding."""
    return json.loads(snapshots.encode_body(body))


def _trip(s: _Snap) -> _Snap:
    status, body = sh.encode_snap(s)
    return sh.decode_snap(status, _via_store_bytes(status, body), as_of=s.as_of)


def _table() -> dict[str, _Snap]:
    key_gate = LabelKey("edits/intention", {"disposition": "loop", "tier": 2, "next": None, "tags": ("a", "b")},
                        operation_id="op-1", gate="match")
    return {
        "empty": _snap("empty", rows=[]),
        "ok_rows": _snap(rows=[_row(0, repo="levain"), _row(1), _row(2, due="2026-11-12", n=3, f=1.5)]),
        "partial": _snap("partial", rows=[_row(0)], skipped=[{"count": 2, "reason": "row refused"}]),
        "error_message": _snap("error", rows=None, error="boom: refresher not reporting since 2026-11-19T14:00:00Z",
                               note="a note", as_of=None),
        "stale_hint": _snap("ok", rows=[_row(0)], stale_hint=True),
        "filtered_and_empty_text": _snap("ok", rows=[_row(0)], filtered=[{"count": 4, "reason": "future"}],
                                         empty="nothing due", note="n"),
        "hidden_rows": _snap("ok", rows=[_row(0)], hidden_skipped=[{"count": 7, "reason": "expired"}],
                             hidden_note="7 expired rows match"),
        "value_panel": _snap(rows=None, value={"lines": [{"text": "a", "n": 1}], "z": [1, 2], "a": {"y": 1, "b": "é"}},
                             value_version="abcd1234"),
        "value_absent_version": _snap(rows=None, value={"lines": []}, value_version="__absent__"),
        "label_keys_with_gate": _snap(rows=[_row(0), _row(1)], label_keys={"r0": key_gate,
                                                                          "r1": LabelKey("edits/intention", {"x": 1})}),
        "label_keys_empty": _snap(rows=[_row(0)], label_keys={}),
        "value_label_key": _snap(rows=None, value={"k": 1}, value_label_key=LabelKey("state", {"state": "fine"})),
        "view_order": _snap(rows=[_row(0), _row(1)], view_order={"v1": ["r1", "r0"], "v2": ["r0", "r1"]}),
        "unicode": _snap(rows=[_row(0, repo="日本語 ünïcödé \U0001f9ea")], note="café — ünï", value=None),
        "tuples_and_none": _snap(rows=[_row(0, pair=(1, 2), none=None)], value={"t": (1, (2, 3)), "n": None}),
        "band_age_only": _snap(rows=[_row(0, band_age="3m")]),
        "non_dict_row": _snap(rows=[_row(0, band_age="3m"), "not a dict"]),
        "str_default_values": _snap(rows=None, value={"at": datetime(2026, 11, 19, tzinfo=timezone.utc),
                                                      "d": date(2026, 11, 19), "p": Path("/tmp/x")}),
    }


# --- the invariant ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(_table()))
def test_the_etag_text_of_a_decoded_snapshot_equals_the_originals(name: str) -> None:
    s = _table()[name]
    back = _trip(s)
    assert _frags(back) == _frags(s)
    assert back.as_of == s.as_of


@pytest.mark.parametrize("name", list(_table()))
def test_the_body_is_accepted_by_the_stores_encoder_and_decodes_to_the_same_snapshot_without_the_byte_trip(
        name: str) -> None:
    s = _table()[name]
    status, body = sh.encode_snap(s)
    snapshots.encode_body(body)
    assert _frags(sh.decode_snap(status, body, as_of=s.as_of)) == _frags(s)


def test_a_snapshot_with_no_odd_values_decodes_to_an_equal_snapshot_field_for_field() -> None:
    s = _snap("partial", rows=[_row(0, repo="levain"), _row(1)], skipped=[{"count": 1, "reason": "x"}],
              filtered=[{"count": 2, "reason": "y"}], note="n", empty="e", view_order={"v": ["r1", "r0"]},
              label_keys={"r0": LabelKey("res", {"a": 1}, "op", "match")}, hidden_skipped=[{"count": 1, "reason": "h"}],
              hidden_note="hn", stale_hint=True)
    back = _trip(s)
    for f in dataclasses.fields(_Snap):
        if f.init:
            assert getattr(back, f.name) == getattr(s, f.name), f.name


def _big_snap(n: int) -> _Snap:
    rows = [_row(i, band_age="5m", repo=f"repo{i % 9}", alpha=[i, i + 1], mid={"b": i, "a": 2}) for i in range(n)]
    return _snap(rows=rows, label_keys={r["id"]: LabelKey("edits/x", {"id": r["id"], "n": 1}) for r in rows})


def _round_trip(s: _Snap) -> tuple[float, _Snap]:
    t0 = time.perf_counter()
    status, body = sh.encode_snap(s)
    raw = snapshots.encode_body(body)
    back = sh.decode_snap(status, json.loads(raw), as_of=s.as_of)
    return time.perf_counter() - t0, back


def test_a_large_rows_list_round_trips_in_linear_time() -> None:
    """The round trip of a big rows list must cost time in proportion to the rows, not the square of them.

    A fixed wall-clock bound measures the runner, not the code: a loaded box fails it with no codec change. So this
    times the same round trip at a small row count and at a much larger one, in the same test on the same box, and
    asserts the time grows by a small multiple of the size ratio, far short of the square of it. A slow or loaded box
    slows both runs together and leaves the ratio alone, and each size is timed several times with the fastest kept so
    a stall that hits one run is not read as growth. Linear growth gives a time ratio near the size ratio; a quadratic
    step in the encode or the decode gives one near its square, well past the bound. The bound leaves a multiple of
    the size ratio as room for allocation and collection noise. The big round trip must also decode back to the same
    change-detection text as the original."""
    small_n, big_n, attempts = 500, 5000, 5
    small, big = _big_snap(small_n), _big_snap(big_n)
    small_took = min(_round_trip(small)[0] for _ in range(attempts))
    timed = [_round_trip(big) for _ in range(attempts)]
    big_took = min(t for t, _ in timed)
    ratio = big_took / small_took
    assert ratio < 3 * (big_n / small_n), (small_took, big_took, ratio)
    assert _frags(timed[-1][1]) == _frags(big)


# --- rule 1: as_of is never in the body --------------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(_table()))
def test_as_of_is_not_in_the_body_and_the_decoder_takes_the_callers(name: str) -> None:
    s = _table()[name]
    status, body = sh.encode_snap(s)
    assert "as_of" not in body
    assert "2026-11-19T15:00:00Z" not in snapshots.encode_body(body).decode()
    other = sh.decode_snap(status, body, as_of="2027-01-01T00:00:00Z")
    assert other.as_of == "2027-01-01T00:00:00Z"
    assert sh.decode_snap(status, body, as_of=None).as_of is None


def test_two_snapshots_that_differ_only_in_as_of_have_one_body() -> None:
    a = _snap(rows=[_row(0)], as_of="2026-11-19T15:00:00Z")
    b = _snap(rows=[_row(0)], as_of="2026-11-19T15:01:00Z")
    assert snapshots.encode_body(sh.encode_snap(a)[1]) == snapshots.encode_body(sh.encode_snap(b)[1])


# --- rule 2: the clock facets are stripped before encoding ---------------------------------------------------------------
def test_a_body_is_byte_identical_across_two_derives_a_minute_apart() -> None:
    first = _snap(rows=[_row(0, band_age="58m", repo="x"), _row(1, band_age="1h"), _row(2)])
    later = _snap(rows=[_row(0, band_age="59m", repo="x"), _row(1, band_age="1h"), _row(2)])
    later.rows[1]["facets"]["band_age"] = "2h"
    b1, b2 = sh.encode_snap(first)[1], sh.encode_snap(later)[1]
    assert b1 == b2
    e1, e2 = snapshots.encode_body(b1), snapshots.encode_body(b2)
    assert e1 == e2 and hashlib.sha256(e1).hexdigest() == hashlib.sha256(e2).hexdigest()
    assert all("band_age" not in r["facets"] for r in b1["rows"])


def test_the_strip_is_the_engines_own_function_and_leaves_every_other_facet_and_the_input_rows_alone() -> None:
    from levain.cockpit import engine
    rows = [_row(0, band_age="58m", repo="x"), _row(1, due="2026-11-12")]
    s = _snap(rows=rows)
    body = sh.encode_snap(s)[1]
    assert body["rows"] == engine._etag_rows(rows) == [_row(0, repo="x"), _row(1, due="2026-11-12")]
    assert rows[0]["facets"] == {"band_age": "58m", "repo": "x"}          # the snapshot the engine holds is untouched
    assert engine._CLOCK_FACETS == ("band_age",)


# --- rule 3: determinism and the normalisations -----------------------------------------------------------------------
def _value_after(v: Any) -> Any:
    s = _snap(rows=None, value=v)
    return json.loads(snapshots.encode_body(sh.encode_snap(s)[1]))["value"]


def test_a_datetime_a_date_and_a_path_become_str_of_themselves_exactly_as_the_engines_canonical_text_has_them() -> None:
    v = {"dt": datetime(2026, 11, 19, 15, 0, 1, 5, tzinfo=timezone.utc), "d": date(2026, 11, 19), "p": Path("/tmp/a b")}
    got = _value_after(v)
    assert got == {"dt": str(v["dt"]), "d": "2026-11-19", "p": "/tmp/a b"}
    assert got == json.loads(_canon_text(v))


def test_any_other_object_becomes_str_of_itself() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd-thing"

    v = {"o": Odd(), "s": {3, 3}, "b": b"x", "n": [Odd()]}
    got = _value_after(v)
    assert got == {"o": "odd-thing", "s": "{3}", "b": "b'x'", "n": ["odd-thing"]}
    assert got == json.loads(_canon_text(v))


def test_a_non_finite_float_becomes_none() -> None:
    got = _value_after({"a": math.nan, "b": math.inf, "c": -math.inf, "d": [math.nan, 1.5], "ok": 0.0})
    assert got == {"a": None, "b": None, "c": None, "d": [None, 1.5], "ok": 0.0}


def test_a_tuple_becomes_a_list_at_every_depth() -> None:
    got = _value_after({"t": (1, (2, (3,))), "l": [(4, 5)]})
    assert got == {"t": [1, [2, [3]]], "l": [[4, 5]]}


def test_a_non_string_dict_key_becomes_the_string_json_makes_of_it() -> None:
    for v, want in (({1: "a", 2.5: "b", None: "d", "s": "e"}, {"1", "2.5", "null", "s"}), ({True: "c"}, {"true"}),
                    ({False: "c"}, {"false"})):
        got = _value_after(v)
        assert got == json.loads(json.dumps(v))
        assert set(got) == want


def test_int_and_str_subclasses_encode_as_the_plain_value() -> None:
    class Color(str, Enum):
        RED = "red"

    class Level(int, Enum):
        ONE = 1

    got = _value_after({"c": Color.RED, "l": Level.ONE, "b": True})
    assert got == {"c": "red", "l": 1, "b": True}
    assert type(got["c"]) is str


def test_a_plain_json_value_is_returned_unchanged() -> None:
    v = {"a": [1, 2.5, "x", None, True, {"b": []}], "é": "ü", "big": 2 ** 70}
    assert _value_after(v) == v


def test_every_table_body_is_json_clean_and_the_encoder_never_raises() -> None:
    for name, s in _table().items():
        status, body = sh.encode_snap(s)
        assert status in snapshots.KNOWN_STATUSES, name
        assert json.loads(json.dumps(body, allow_nan=False)) == body, name
        assert snapshots.encode_body(body)


def test_encode_leaves_the_snapshot_it_was_given_unchanged() -> None:
    s = _snap(rows=[_row(0, band_age="3m", t=(1, 2))], value=None, label_keys={"r0": LabelKey("r", {"a": (1,)})})
    before = copy.deepcopy(s)
    sh.encode_snap(s)
    assert s == before


# --- the status mapping -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("engine_status,skipped,want,hint", [
    ("ok", [], "ok", False), ("empty", [], "empty", False), ("partial", [{"count": 1, "reason": "x"}], "partial", False),
    ("error", [], "error", False), ("timeout", [], "timeout", False),
    ("stale", [], "ok", True), ("stale", [{"count": 1, "reason": "x"}], "partial", True),
    ("surprising", [], "partial", False), ("", [], "partial", False),
])
def test_the_engine_status_maps_to_a_known_status(engine_status, skipped, want, hint) -> None:
    s = _snap(engine_status, rows=[_row(0)], skipped=skipped)
    status, body = sh.encode_snap(s)
    assert (status, body["stale_hint"]) == (want, hint)
    assert status in snapshots.KNOWN_STATUSES


def test_a_provider_stale_hint_survives_on_a_non_stale_snapshot() -> None:
    status, body = sh.encode_snap(_snap("ok", rows=[_row(0)], stale_hint=True))
    assert (status, body["stale_hint"]) == ("ok", True)


def test_a_declared_source_instant_rides_the_body_and_a_read_with_none_carries_none() -> None:
    declared = _snap("stale", rows=[_row(0)], source_as_of="2026-11-19T10:00:00+00:00")
    status, body = sh.encode_snap(declared)
    assert body["source_as_of"] == "2026-11-19T10:00:00+00:00"
    assert sh.decode_snap(status, _via_store_bytes(status, body), as_of=AS_OF).source_as_of == declared.source_as_of
    assert sh.encode_snap(_snap(rows=[_row(0)]))[1]["source_as_of"] is None
    other = sh.encode_snap(_snap("stale", rows=[_row(0)], source_as_of="2026-11-19T09:00:00+00:00"))[1]
    assert snapshots.encode_body(other) != snapshots.encode_body(body)


def test_decode_takes_the_status_it_is_given_and_maps_an_unknown_one_to_partial() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    assert sh.decode_snap("error", body, as_of=AS_OF).status == "error"
    assert sh.decode_snap("novel", body, as_of=AS_OF).status == "partial"
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap(None, body, as_of=AS_OF)           # type: ignore[arg-type]


def test_the_rows_own_error_text_replaces_the_bodys_when_given() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    assert sh.decode_snap("error", body, as_of=AS_OF).error is None
    assert sh.decode_snap("error", body, as_of=AS_OF, error="derive raised").error == "derive raised"


# --- label keys -------------------------------------------------------------------------------------------------------
def test_a_label_key_is_encoded_field_by_field() -> None:
    key = LabelKey("edits/intention", {"disposition": "loop", "n": 3, "none": None, "t": (1, 2)}, "op-9", "match")
    s = _snap(rows=[_row(0)], label_keys={"r0": key}, value_label_key=LabelKey("v", {"a": 1}))
    body = sh.encode_snap(s)[1]
    assert body["label_keys"] == {"r0": {"resource_key": "edits/intention", "operation_id": "op-9", "gate": "match",
                                         "values": {"disposition": "loop", "n": 3, "none": None, "t": [1, 2]}}}
    assert body["value_label_key"] == {"resource_key": "v", "values": {"a": 1}, "operation_id": None, "gate": None}
    back = _trip(s)
    assert back.label_keys == {"r0": LabelKey("edits/intention", {"disposition": "loop", "n": 3, "none": None,
                                                                  "t": [1, 2]}, "op-9", "match")}
    assert back.value_label_key == LabelKey("v", {"a": 1})


def test_an_absent_label_key_set_stays_absent_and_an_empty_one_stays_empty() -> None:
    assert _trip(_snap(rows=[_row(0)])).label_keys is None
    assert _trip(_snap(rows=[_row(0)], label_keys={})).label_keys == {}
    assert _trip(_snap(rows=[_row(0)])).value_label_key is None


def test_a_label_value_that_json_would_change_is_unverifiable_not_silently_stronger() -> None:
    """A value the consent encoding refuses (a datetime) is no match for any signed value; as its string it could be."""
    for odd in (datetime(2026, 11, 19, tzinfo=timezone.utc), math.nan, {1: "x"}):
        s = _snap(rows=[_row(0)], label_keys={"r0": LabelKey("r", {"at": odd}, None, "match")})
        key = _trip(s).label_keys["r0"]
        assert key.gate == "unavailable", odd
    s = _snap(rows=[_row(0)], label_keys={"r0": LabelKey("r", {"at": datetime(2026, 11, 19)}, None, None)})
    assert _trip(s).label_keys["r0"].gate == "unavailable"
    kept = _snap(rows=[_row(0)], label_keys={"r0": LabelKey("r", {"at": datetime(2026, 11, 19)}, None, "mismatch")})
    assert _trip(kept).label_keys["r0"].gate == "mismatch"
    clean = _snap(rows=[_row(0)], label_keys={"r0": LabelKey("r", {"a": (1, 2), "b": None}, None, "match")})
    assert _trip(clean).label_keys["r0"].gate == "match"


def test_a_label_key_that_is_not_a_label_key_is_refused_at_encode() -> None:
    s = _snap(rows=[_row(0)], label_keys={"r0": {"resource_key": "r"}})      # type: ignore[dict-item]
    with pytest.raises(sh.SnapshotBodyError):
        sh.encode_snap(s)


# --- rule 5: a decode returns fresh containers ------------------------------------------------------------------------
def test_mutating_one_decoded_snapshot_changes_neither_the_body_nor_another_decode() -> None:
    s = _snap(rows=[_row(0, repo="x")], value=None, skipped=[{"count": 1, "reason": "r"}],
              label_keys={"r0": LabelKey("res", {"a": [1]})}, view_order={"v": ["r0"]},
              hidden_skipped=[{"count": 1, "reason": "h"}])
    status, body = sh.encode_snap(s)
    body = _via_store_bytes(status, body)
    pristine = copy.deepcopy(body)
    one = sh.decode_snap(status, body, as_of=AS_OF)
    one.rows[0]["title"] = "changed"
    one.rows[0]["facets"]["repo"] = "changed"
    one.rows.append({"id": "extra"})
    one.skipped.append({"count": 9, "reason": "z"})
    one.view_order["v"].append("zzz")
    one.hidden_skipped.clear()
    one.label_keys["r0"].values["a"].append(2)
    one.label_keys["new"] = LabelKey("n", {})
    assert body == pristine
    two = sh.decode_snap(status, body, as_of=AS_OF)
    assert two.rows == pristine["rows"] and two.skipped == pristine["skipped"]
    assert two.view_order == {"v": ["r0"]} and two.label_keys["r0"].values == {"a": [1]}
    assert two.hidden_skipped == [{"count": 1, "reason": "h"}] and set(two.label_keys) == {"r0"}
    assert two.rows is not one.rows and two.rows[0] is not pristine["rows"][0]


def test_a_decoded_snapshot_carries_no_etag_text_of_another() -> None:
    s = _snap(rows=[_row(0)])
    s.etag_frags()
    back = _trip(s)
    assert back._etag_frags is None and back._read_mark is None
    assert _frags(back) == _frags(s)


# --- rule 6: the schema version ---------------------------------------------------------------------------------------
def test_the_body_leads_with_its_version() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    assert next(iter(body)) == "v" and body["v"] == sh.BODY_VERSION == 1
    assert tuple(body) == sh.BODY_FIELDS


@pytest.mark.parametrize("bad_v", [2, 0, 1.0, "1", None, True, [1], 99])
def test_an_unknown_or_malformed_version_is_refused_by_name(bad_v) -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    body["v"] = bad_v
    with pytest.raises(sh.SnapshotBodyError) as ei:
        sh.decode_snap("ok", body, as_of=AS_OF)
    assert isinstance(ei.value, ValueError)


def test_a_missing_version_and_a_missing_field_are_refused() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    no_v = {k: v for k, v in body.items() if k != "v"}
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap("ok", no_v, as_of=AS_OF)
    for f in sh.BODY_FIELDS[1:]:
        with pytest.raises(sh.SnapshotBodyError, match=f):
            sh.decode_snap("ok", {k: v for k, v in body.items() if k != f}, as_of=AS_OF)


@pytest.mark.parametrize("field,bad", [
    ("rows", "x"), ("rows", {}), ("filtered", None), ("skipped", {}), ("hidden_skipped", "x"), ("error", 5),
    ("empty", []), ("value_version", 1), ("note", None), ("hidden_note", 3), ("stale_hint", 1), ("stale_hint", None), ("source_as_of", 3),
    ("view_order", []), ("label_keys", []), ("label_keys", {"r": "x"}), ("label_keys", {"r": {"resource_key": 1}}),
    ("value_label_key", "x"), ("value_label_key", {"resource_key": "r", "values": []}),
    ("value", {"x": float("nan")}),
])
def test_a_mistyped_field_is_refused_not_built_into_a_half_snapshot(field, bad) -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)], label_keys={"r0": LabelKey("r", {})}))[1]
    body[field] = bad
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap("ok", body, as_of=AS_OF)


@pytest.mark.parametrize("junk", [None, [], "x", 3, b"{}"])
def test_a_body_that_is_not_an_object_is_refused(junk) -> None:
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap("ok", junk, as_of=AS_OF)


def test_a_body_that_is_not_json_is_refused() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    body["value"] = object()
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap("ok", body, as_of=AS_OF)


def test_extra_fields_in_a_known_version_are_ignored() -> None:
    body = sh.encode_snap(_snap(rows=[_row(0)]))[1]
    body["added_later"] = {"x": 1}
    assert sh.decode_snap("ok", body, as_of=AS_OF).rows == [_row(0)]


# --- through the real store -------------------------------------------------------------------------------------------
def test_a_snapshot_published_and_read_through_the_real_store_decodes_to_the_originals_etag_text(tmp_path: Path) -> None:
    writer = snapshots.SnapshotWriter(tmp_path / "snapshots.db")
    reader = snapshots.SnapshotReader(tmp_path / "snapshots.db")
    try:
        for name, s in _table().items():
            status, body = sh.encode_snap(s)
            writer.publish(name, status=status, body=snapshots.encode_body(body), started_at=1.0, derived_at=2.0,
                           duration_ms=1.0, error=s.error if status in snapshots.KEEP_BODY_STATUSES else None)
        for name, s in _table().items():
            row = reader.read(name)
            back = sh.decode_snap(row.status, reader.decoded(row), as_of=AS_OF, error=row.error)
            if row.status in snapshots.KEEP_BODY_STATUSES:
                # the store keeps no body for a first derive that failed: the row's own message is the result
                assert row.body is None and back.error == s.error and back.rows is None, name
                continue
            assert _frags(back) == _frags(s), name
    finally:
        reader.close()
        writer.close()


def test_an_unchanged_derive_keeps_its_gen_and_a_clock_tick_does_not_move_it(tmp_path: Path) -> None:
    """The reason as_of and the clock facets stay out of the body: the store's no-false-announcement rule."""
    writer = snapshots.SnapshotWriter(tmp_path / "snapshots.db")
    try:
        def publish(s: _Snap, at: float) -> int:
            status, body = sh.encode_snap(s)
            return writer.publish("p", status=status, body=snapshots.encode_body(body), started_at=at, derived_at=at,
                                  duration_ms=1.0)

        g1 = publish(_snap(rows=[_row(0, band_age="58m")], as_of="2026-11-19T15:00:00Z"), 1.0)
        g2 = publish(_snap(rows=[_row(0, band_age="59m")], as_of="2026-11-19T15:01:00Z"), 61.0)
        g3 = publish(_snap(rows=[_row(0, band_age="59m", repo="new")], as_of="2026-11-19T15:02:00Z"), 121.0)
        assert g1 == g2 and g3 == g1 + 1
    finally:
        writer.close()


# --- the engine's own snapshots, from real reads ----------------------------------------------------------------------
@pytest.fixture()
def default_cockpit(tmp_path: Path) -> Cockpit:
    _root, src, _ids = _install(tmp_path)
    return build_default_cockpit(src)


def test_every_panel_build_default_cockpit_registers_round_trips_after_one_real_refresh(default_cockpit) -> None:
    ck = default_cockpit
    ids = list(ck._specs)
    assert ids
    done, skipped = [], {}
    for pid in ids:
        try:
            ck.refresh(pid)
        except Exception as exc:                                   # a panel that needs a live service this rig lacks
            skipped[pid] = f"{type(exc).__name__}: {exc}"
            continue
        snap = ck._state[pid].snap
        assert snap is not None, pid
        status, body = sh.encode_snap(snap)
        assert status in snapshots.KNOWN_STATUSES, pid
        back = sh.decode_snap(status, _via_store_bytes(status, body), as_of=snap.as_of)
        assert _frags(back) == _frags(snap), pid
        done.append(pid)
    assert not skipped, skipped
    assert sorted(done) == sorted(ids)


def test_a_real_provider_with_label_keys_clock_facets_and_skips_round_trips() -> None:
    def read(ctx) -> Read:
        rows = tuple(RowIn(f"s:{i}", f"t{i}", {"at": f"2026-11-1{i}", "band_age": f"{i}m"}, stored={"id": f"s:{i}"})
                     for i in range(1, 4))
        return Read(rows=rows, skipped=((2, "unreadable"),), filtered=((1, "old"),))

    ck = Cockpit(clock=lambda: datetime(2026, 11, 19, 15, tzinfo=timezone.utc))
    ck.register(ProviderSpec(id="t", kind="triage-list", title="t", priority="feed", read=read, order="time.desc",
                             facets=frozenset({"at", "band_age"}), version_fields=("id",),
                             label_key=lambda stored: LabelKey("t/row", {"id": stored["id"], "tags": ("a",)}, None,
                                                               "match")))
    ck.refresh("t")
    snap = ck._state["t"].snap
    assert snap.status == "partial" and snap.label_keys and snap.skipped and snap.filtered
    assert any("band_age" in r["facets"] for r in snap.rows)
    status, body = sh.encode_snap(snap)
    assert status == "partial" and all("band_age" not in r["facets"] for r in body["rows"])
    back = sh.decode_snap(status, _via_store_bytes(status, body), as_of=snap.as_of)
    assert _frags(back) == _frags(snap)
    assert set(back.label_keys) == set(snap.label_keys)
    assert back.label_keys["s:1"] == LabelKey("t/row", {"id": "s:1", "tags": ["a"]}, None, "match")


def test_a_failed_first_derive_has_no_body_and_decodes_to_the_bare_error_result() -> None:
    back = sh.decode_snap("error", None, as_of="2026-11-19T15:00:00Z", error="ValueError: boom")
    assert (back.status, back.rows, back.value, back.filtered, back.skipped, back.error, back.note) == (
        "error", None, None, [], [], "ValueError: boom", "")
    assert sh.decode_snap("timeout", None, as_of=None).error
    with pytest.raises(sh.SnapshotBodyError):
        sh.decode_snap("ok", None, as_of=AS_OF)


def test_a_real_value_panel_with_a_value_label_key_round_trips(default_cockpit) -> None:
    ck = Cockpit()
    ck.register(default_cockpit._specs["state"].replaced(label_key=lambda stored: LabelKey("state/line", dict(stored))))
    ck.refresh("state")
    snap = ck._state["state"].snap
    assert snap.value_label_key is not None and snap.value is not None
    status, body = sh.encode_snap(snap)
    back = sh.decode_snap(status, _via_store_bytes(status, body), as_of=snap.as_of)
    assert _frags(back) == _frags(snap)
    assert back.value_label_key.resource_key == "state/line"
    assert back.value_version == snap.value_version
