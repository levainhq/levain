"""The panel etag keeps its heavy parts (rows, value, filtered, skipped) as canonical text on the snapshot, so a read over
an unchanged snapshot serialises only the light parts again. The etag must be byte for byte what it was: the oracle below
is the formula as it stood before, copied here, and every case asserts the engine's answer equals it."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

import pytest

from levain.cockpit.labels import LabelSnapshot
from levain.cockpit.engine import SCHEMA, Cockpit, ProviderSpec, ReadContext, _Snap
from levain.cockpit.results import Read, RowIn

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 11)


# --- the oracle: the formula as it was, verbatim ---------------------------------------------------------------------
def _old_canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _old_sha(obj: Any) -> str:
    return hashlib.sha256(_old_canon(obj)).hexdigest()


def _old_etag_rows(rows):
    if not rows:
        return rows
    out = []
    for row in rows:
        facets = row.get("facets") if isinstance(row, dict) else None
        if isinstance(facets, dict) and any(k in facets for k in ("band_age",)):
            row = {**row, "facets": {k: v for k, v in facets.items() if k not in ("band_age",)}}
        out.append(row)
    return out


def _old_etag_of(ck, snap, groups, cred, today, views=(), labels=None, face="", compose=None, search=None, *, auth):
    return _old_sha({"schema": SCHEMA, "status": snap.status, "rows": _old_etag_rows(snap.rows), "value": snap.value,
                     "filtered": snap.filtered, "skipped": snap.skipped, "error": snap.error,
                     "note": snap.note, "empty": snap.empty, "groups": groups, "credential": cred, "date": today.isoformat(),
                     "verbs": ck._verb_rev(auth),
                     **({"views": [list(v) for v in views]} if views else {}),
                     **({"face": face} if face else {}),
                     **({"compose": compose} if compose else {}),
                     **({"search": search} if search else {}),
                     **({"labels": [list(labels.revision) if labels.revision is not None else None, labels.error]}
                        if labels is not None and (labels.revision is not None or labels.error is not None)
                        else {})})


# --- the matrix ------------------------------------------------------------------------------------------------------
def _row(i: int, **facets: Any) -> dict[str, Any]:
    return {"id": f"r{i}", "version": f"v{i}", "group": "g", "title": f"row {i} é ü 漢字", "truncated": False,
            "body": "line one\nline \"two\"" if i % 2 else None, "text_hidden": False, "facets": dict(facets),
            "emphasis": "none", "band": "now", "badges": [{"kind": "due", "value": "2026-11-12"}],
            "provenance": None, "actions": []}


def _rows() -> list[dict[str, Any]]:
    return [_row(0, band_age="58m", repo="levain"), _row(1, band_age="1h"), _row(2, repo="x", due="2026-11-12"),
            _row(3), _row(4, zeta=1, alpha=[3, 2, 1], band_age="2h", mid={"b": 1, "a": 2})]


def _snaps() -> dict[str, _Snap]:
    def s(status="ok", rows=None, value=None, **kw):
        base = dict(filtered=[], skipped=[], as_of="2026-11-11T15:00:00Z", error=None, note="")
        base.update(kw)
        return _Snap(status, rows, value, **base)

    return {
        "rows": s(rows=_rows()),
        "rows_with_filtered_and_skipped": s("partial", rows=_rows(), filtered=[{"count": 2, "reason": "x"}],
                                            skipped=[{"count": 1, "reason": "row refused"}], note="a note", empty="none"),
        "empty_rows": s("empty", rows=[]),
        "no_rows": s("ok", rows=None),
        "band_age_only": s(rows=[_row(0, band_age="3m")]),
        "non_dict_row": s(rows=[_row(0, band_age="3m"), "not a dict"]),
        "value_panel": s(rows=None, value={"rows": [{"name": "a", "n": 1}], "z": [1, 2], "a": {"y": 1, "b": "é"}},
                         value_version="abcd"),
        "value_with_str_default": s(rows=None, value={"at": datetime(2026, 11, 11, tzinfo=timezone.utc), "d": TODAY}),
        "error": s("error", rows=None, error="refresher not reporting since 2026-11-11T14:00:00Z", note="n"),
        "stale": s("stale", rows=_rows(), stale_hint=True),
    }


def _light() -> list[dict[str, Any]]:
    labelled = LabelSnapshot(revision=("reported", 3, 7, "9:abc"), error=None)
    errored = LabelSnapshot(revision=None, error="journal unreadable")
    unlabelled = LabelSnapshot(revision=None, error=None)
    return [
        dict(groups=None, cred="none"),
        dict(groups=[{"id": "a", "title": "A", "count": 2}, {"id": "b", "title": "B", "count": 0, "uncounted": True}],
             cred="token"),
        dict(groups=None, cred="token", views=(("v1", "By age", "decision.dated"), ("v2", "By repo", "x"))),
        dict(groups=None, cred="token", face="count"),
        dict(groups=None, cred="token", compose={"verb": "note_add", "fields": [{"kind": "text", "name": "t"}]}),
        dict(groups=None, cred="token", search={"fields": ["title", "body"], "scope": "rows", "default_visible": 10,
                                                "hook": False}),
        dict(groups=None, cred="token", labels=labelled),
        dict(groups=None, cred="token", labels=errored),
        dict(groups=None, cred="token", labels=unlabelled),
        dict(groups=[{"id": "a", "title": "A", "count": 1}], cred="token", views=(("v1", "V", "o"),), face="count",
             compose={"verb": "w", "fields": []}, search={"fields": ["title"], "scope": "store", "default_visible": 3,
                                                          "hook": True}, labels=labelled),
    ]


@pytest.fixture()
def ck() -> Cockpit:
    return Cockpit(clock=lambda: NOW)


@pytest.mark.parametrize("snap_name", list(_snaps()))
def test_the_etag_is_byte_identical_to_the_old_formula_over_the_whole_matrix(ck, snap_name) -> None:
    auth = ck.authority()
    for light in _light():
        kw = dict(light)
        snap = _snaps()[snap_name]
        want = _old_etag_of(ck, snap, kw.pop("groups"), kw.pop("cred"), TODAY, **kw, auth=auth)
        kw = dict(light)
        got = ck._etag_of(snap, kw.pop("groups"), kw.pop("cred"), TODAY, **kw, auth=auth)
        assert got == want, (snap_name, light)
        # and again over the same snapshot (the kept text): still the same
        kw = dict(light)
        assert ck._etag_of(snap, kw.pop("groups"), kw.pop("cred"), TODAY, **kw, auth=auth) == want


def test_a_second_etag_over_one_snapshot_does_not_serialise_its_rows_again(ck, monkeypatch) -> None:
    from levain.cockpit import engine
    snap = _snaps()["rows"]
    auth = ck.authority()
    ck._etag_of(snap, None, "token", TODAY, auth=auth)
    kept = snap._etag_frags
    assert kept is not None and set(kept) == {"rows", "value", "filtered", "skipped"}
    seen: list[Any] = []
    real = engine._canon_text
    monkeypatch.setattr(engine, "_canon_text", lambda o: (seen.append(o), real(o))[1])
    ck._etag_of(snap, None, "token", TODAY, auth=auth)
    assert snap._etag_frags is kept
    assert not any(isinstance(o, list) and o and isinstance(o[0], dict) and "version" in o[0] for o in seen)


def test_the_light_parts_still_move_the_etag_over_one_snapshot(ck) -> None:
    snap = _snaps()["rows"]
    auth = ck.authority()
    base = ck._etag_of(snap, None, "token", TODAY, auth=auth)
    assert ck._etag_of(snap, None, "none", TODAY, auth=auth) != base                      # the credential class
    assert ck._etag_of(snap, None, "token", date(2026, 11, 12), auth=auth) != base        # the date
    assert ck._etag_of(dataclasses.replace(snap, status="stale"), None, "token", TODAY, auth=auth) != base
    assert ck._etag_of(snap, None, "token", TODAY, face="count", auth=auth) != base


def test_a_clock_facet_does_not_move_the_etag_but_a_real_facet_does(ck) -> None:
    auth = ck.authority()
    a, b, c = (_Snap("ok", [_row(0, band_age=age, repo=repo)], None, [], [], "t", None, "")
               for age, repo in (("1m", "r"), ("2h", "r"), ("1m", "other")))
    ea, eb, ec = (ck._etag_of(x, None, "token", TODAY, auth=auth) for x in (a, b, c))
    assert ea == eb and ea != ec


def test_a_stale_copy_of_a_cached_snapshot_carries_the_kept_text_and_the_same_etag(ck) -> None:
    rows = tuple(RowIn(id=f"np:{i}", title=f"t {i}", facets={"band_age": "5m", "repo": "levain"}) for i in range(5))
    spec = ProviderSpec("p", "triage-list", "P", "gate", lambda ctx: Read(rows=rows), refresh_every_s=20.0,
                        stale_after_s=30.0, order="decision.dated", version_fields=("what",), facets=frozenset({"band_age", "repo"}))
    c = Cockpit(clock=lambda: NOW)
    c.register(spec)
    c.refresh("p")
    auth = c.authority()
    fresh = c._snap_for(spec, ReadContext(NOW, authority=auth))
    assert fresh.status == "ok"
    from datetime import timedelta
    late = c._snap_for(spec, ReadContext(NOW + timedelta(seconds=40), authority=auth))
    assert late is not fresh and late.status == "stale"
    assert late._etag_frags is fresh._etag_frags and fresh._etag_frags is not None
    want = _old_etag_of(c, late, None, "token", TODAY, auth=auth)
    assert c._etag_of(late, None, "token", TODAY, auth=auth) == want
