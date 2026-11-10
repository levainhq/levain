"""THE LIST's Keep (row 182, Phill 11-10 Q1-Q4): ``intention_keep`` is a row verb on an ``intention:`` row only. It writes an anneal
``observation`` episode (the intention's words verbatim plus a source tag, keyed on the intention's origin key), THEN composts the
intention (tier C1). A crash between the two leaves the intention open and the retry lands no second episode. The List's search
reaches the entity's episodes as ``kept`` rows, which a read with no query never carries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anneal_memory import Store

from levain import writes
from levain.cockpit import list_panel
from levain.cockpit.verbs import dispatch
from levain.writes import KEEP_EPISODE_SOURCE, EditError, apply_edit
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, Rig  # noqa: F401 - the shared rig


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _keep_episodes(rig: Rig) -> list[Any]:
    import sqlite3

    db = sqlite3.connect(str(rig.src.anneal.episodic_db))
    try:
        rows = db.execute("select id, type, content, source from episodes where source = ? order by timestamp",
                          (KEEP_EPISODE_SOURCE,)).fetchall()
    finally:
        db.close()
    return [SimpleNamespace(id=i, type=t, content=c, source=s) for i, t, c, s in rows]


def _open_ids(rig: Rig) -> set[str]:
    return {s["id"] for s in rig.sp.list_open()}


def _keep(rig: Rig, sid: str, **extra: Any) -> dict[str, Any]:
    return apply_edit(rig.src.write_scope, {"kind": "intention_keep", "intention_id": sid, **extra})


# --- the verb: episode first, then compost ------------------------------------------------------------------------


def test_a_keep_writes_the_episode_with_the_words_verbatim_then_composts_the_intention(rig: Rig) -> None:
    sid = rig.ids["plain"]
    out = _keep(rig, sid)
    assert out["ok"] and out["action"] == "keep" and out["source"] == f"intention:{sid}"
    [ep] = _keep_episodes(rig)
    assert ep.type == "observation" and ep.content.startswith("a plain seed\n") and sid in ep.content
    assert out["episode_id"] == ep.id and ep.id in out["receipt_line"]
    assert sid not in _open_ids(rig)                       # composted
    assert rig.sp.get(sid)["resolution"]["kind"] == "composted"


def test_keep_is_a_row_verb_on_an_intention_row_only_and_fires_at_c1_through_the_cockpit(rig: Rig) -> None:
    rows = rig.rows()
    intention = rows[f"intention:{rig.ids['plain']}"]
    act = next(a for a in intention["actions"] if a["verb"] == "intention_keep")
    assert act["tier"] == "C1"
    for rid in (f"band:{rig.q.id}", "board:levain:a"):     # no write by construction
        assert all(a["verb"] != "intention_keep" for a in rows[rid]["actions"])
        with pytest.raises(EditError):
            dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                     install_class=INSTALL_CLASS,
                     req={"verb": "intention_keep", "panel_id": "tray", "row_id": rid, "row_version": rows[rid]["version"],
                          "params": {}, "confirm": True})
    out = dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                   install_class=INSTALL_CLASS,
                   req={"verb": "intention_keep", "panel_id": "tray", "row_id": f"intention:{rig.ids['plain']}",
                        "row_version": intention["version"], "params": {}, "confirm": True})
    assert out["ok"] is True
    assert len(_keep_episodes(rig)) == 1 and rig.ids["plain"] not in _open_ids(rig)


def test_the_episode_key_is_a_hash_of_the_intentions_origin_key_else_its_id_and_always_in_the_grammar() -> None:
    import hashlib
    import re

    h = lambda t: "keep:" + hashlib.sha256(t.encode()).hexdigest()   # noqa: E731
    assert writes.keep_origin_key({"id": "intention-1", "origin_key": "ok-9"}) == h("origin:ok-9")
    assert writes.keep_origin_key({"id": "intention-1"}) == h("id:intention-1")
    for ok in ("x" * 128, "a key with spaces", "k\u00e9y-\u65e5\u672c"):          # keys anneal's grammar would refuse with a prefix
        k = writes.keep_origin_key({"id": "intention-1", "origin_key": ok})
        assert re.fullmatch(r"[0-9A-Za-z._:-]{1,128}", k) and len(k) == 69


def test_a_keep_of_an_intention_with_an_awkward_origin_key_lands(rig: Rig) -> None:
    sid = rig.ids["plain"]
    path = rig.src.anneal.intentions_json
    import json

    data = json.loads(path.read_text())
    rows = data["spores"] if isinstance(data, dict) and "spores" in data else data     # anneal's document key (its own word)
    target = next(r for r in rows if r["id"] == sid)
    target["origin_key"] = "a " + "\u00e9" * 127           # 129 characters, spaces and Unicode
    path.write_text(json.dumps(data))
    out = _keep(rig, sid)
    assert out["ok"] and len(_keep_episodes(rig)) == 1 and sid not in _open_ids(rig)


# --- a retry lands once; the episode is written before the compost ----------------------------------------------


def _fail_compost(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("injected: the intention store went away after the episode")
    monkeypatch.setattr(writes, "_keep_compost", boom)


def test_a_failure_after_the_episode_leaves_the_intention_open_and_the_retry_lands_the_episode_once(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]
    with monkeypatch.context() as m:
        _fail_compost(m)
        with pytest.raises(EditError) as ei:
            _keep(rig, sid)
        assert ei.value.http_status == 503
    [first] = _keep_episodes(rig)                          # the episode is down ...
    assert sid in _open_ids(rig)                           # ... and the intention is still open (episode FIRST, compost after)
    _keep(rig, sid)                                        # the retry
    [again] = _keep_episodes(rig)                          # exactly one episode: the key made the retry the same one
    assert again.id == first.id
    assert sid not in _open_ids(rig)


def test_a_failure_writing_the_episode_composts_nothing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]

    def boom(self: Any, *_a: Any, **_k: Any) -> None:
        raise OSError("injected: the episode store went away")
    monkeypatch.setattr(Store, "record", boom)
    with pytest.raises(EditError):
        _keep(rig, sid)
    monkeypatch.undo()
    assert sid in _open_ids(rig) and _keep_episodes(rig) == []


def test_a_stale_row_is_refused_before_any_episode(rig: Rig) -> None:
    sid = rig.ids["plain"]
    with pytest.raises(EditError) as ei:
        _keep(rig, sid, expect_disposition="agenda")       # the row the operator saw was moved
    assert ei.value.code == "stale"
    with pytest.raises(EditError) as ei2:
        apply_edit(rig.src.write_scope, {"kind": "intention_keep", "intention_id": sid}, expected_version="0" * 16)
    assert ei2.value.code == "stale"
    assert sid in _open_ids(rig) and _keep_episodes(rig) == []


def test_keeping_an_already_resolved_intention_is_stale_and_writes_nothing_more(rig: Rig) -> None:
    sid = rig.ids["plain"]
    _keep(rig, sid)
    with pytest.raises(EditError) as ei:
        _keep(rig, sid)
    assert ei.value.code == "stale" and len(_keep_episodes(rig)) == 1


# --- the List's search reaches memory ----------------------------------------------------------------------------


def test_a_read_with_no_query_carries_no_kept_row_and_a_query_brings_them(rig: Rig) -> None:
    _keep(rig, rig.ids["plain"])
    quiet = rig.ck.panel("tray", credential_class="token")
    assert all(r["facets"]["surface"] != "kept" for r in quiet["rows"])
    assert next(g for g in quiet["groups"] if g["id"] == "kept")["count"] == 0      # the head counts none of them
    got = rig.ck.panel("tray", credential_class="token", q="noticed Z")
    kept = [r for r in got["rows"] if r["facets"]["surface"] == "kept"]
    assert [r["body"] for r in kept] == ["noticed Z"] and kept[0]["id"].startswith(list_panel.KEPT_PREFIX)
    assert kept[0]["group"] == "kept" and kept[0]["facets"]["kept_line"].startswith("memory: observation episode ")
    again = rig.ck.panel("tray", credential_class="token", q="a plain seed")
    mine = [r for r in again["rows"] if r["facets"]["surface"] == "kept"]
    assert len(mine) == 1 and mine[0]["facets"]["kept_line"].startswith("kept as memory: episode ")
    assert mine[0]["facets"]["kept_source"] == KEEP_EPISODE_SOURCE


def test_a_kept_row_is_read_only_and_not_counted(rig: Rig) -> None:
    before = rig.panel()["count"]
    got = rig.ck.panel("tray", credential_class="token", q="noticed Z")
    row = next(r for r in got["rows"] if r["facets"]["surface"] == "kept")
    assert row["actions"] == []                            # no intention verb applies to a kept row
    assert rig.panel()["count"] == before


def test_the_kept_part_is_capped_by_the_episode_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain.cockpit import providers

    rig = Rig(tmp_path, intentions=False, band=False, board=False)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        for i in range(7):
            st.record(f"memory line {i}", "observation")
    monkeypatch.setattr(providers, "EPISODE_LIMIT", 3)
    # a fresh cockpit: the view is built at the cap
    from levain.cockpit.providers import build_default_cockpit
    ck = build_default_cockpit(rig.src, clock=lambda: __import__("tests.test_cockpit_list_panel", fromlist=["NOW"]).NOW)
    got = ck.panel("tray", credential_class="token", q="memory line")
    assert len([r for r in got["rows"] if r["facets"]["surface"] == "kept"]) == 3


def test_a_retry_after_the_words_changed_composts_without_a_second_episode(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]
    with monkeypatch.context() as m:
        _fail_compost(m)
        with pytest.raises(EditError):
            _keep(rig, sid)
    [first] = _keep_episodes(rig)
    rig.sp.update(sid, text="a plain seed, reworded")
    out = _keep(rig, sid)                                    # the Keep already happened: no wedge, no second episode
    assert out["ok"] and out["earlier_words_kept"] is True and out["episode_id"] == first.id
    assert "earlier" in out["receipt_line"]
    assert [e.id for e in _keep_episodes(rig)] == [first.id] and sid not in _open_ids(rig)
    assert first.content.startswith("a plain seed\n")       # the earlier words are what memory holds


def test_a_retry_whose_episode_was_deleted_is_refused_with_the_remedy(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]
    with monkeypatch.context() as m:
        _fail_compost(m)
        with pytest.raises(EditError):
            _keep(rig, sid)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        key = writes.keep_origin_key(rig.sp.get(sid))
        _ep, version = st.read_episode_versioned(key)
        st.delete_by_origin_key(key, expected_version=version, effect_id="test-delete-1")
    with pytest.raises(EditError) as ei:
        _keep(rig, sid)
    assert ei.value.code == "keep_key_retired" and ei.value.http_status == 409
    assert "compost the item by hand" in str(ei.value)
    assert sid in _open_ids(rig) and _keep_episodes(rig) == []


def test_keep_needs_confirm_as_the_resolve_does(rig: Rig) -> None:
    rows = rig.rows()
    sid = rig.ids["plain"]
    with pytest.raises(EditError) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS,
                 req={"verb": "intention_keep", "panel_id": "tray", "row_id": f"intention:{sid}",
                      "row_version": rows[f"intention:{sid}"]["version"], "params": {}})
    assert ei.value.code == "confirm_required" and sid in _open_ids(rig)


# --- a read with no query costs and reports nothing of the kept part ----------------------------------------------


def _fail_kept_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from levain.cockpit.results import Fault

    real = list_panel._view_fault

    def fault(view: Any, *keys: str) -> Any:
        return Fault("episodes: injected") if keys == ("episodes",) else real(view, *keys)
    monkeypatch.setattr(list_panel, "_view_fault", fault)


def test_a_kept_read_fault_is_the_searchs_to_report_never_a_plain_reads(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = rig.ck.panel("tray", credential_class="token")
    _fail_kept_read(monkeypatch)
    quiet = rig.ck.panel("tray", credential_class="token")
    assert quiet["status"] == clean["status"] != "partial" and quiet["skipped"] == clean["skipped"]
    assert quiet["etag"] == clean["etag"]
    got = rig.ck.panel("tray", credential_class="token", q="a plain seed")
    assert any("kept memory could not be read" in x["reason"] for x in got["skipped"]) and got["status"] == "partial"


def test_a_new_episode_does_not_move_a_query_less_etag(rig: Rig) -> None:
    before = rig.panel()["etag"]
    with Store(str(rig.src.anneal.episodic_db)) as st:
        st.record("something new in memory", "observation")
    assert rig.panel()["etag"] == before


def test_a_search_says_it_reaches_only_the_newest_episodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain.cockpit import providers
    from levain.cockpit.providers import build_default_cockpit

    rig = Rig(tmp_path, intentions=False, band=False, board=False)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        for i in range(5):
            st.record(f"memory line {i}", "observation")
    monkeypatch.setattr(providers, "EPISODE_LIMIT", 3)
    ck = build_default_cockpit(rig.src, clock=lambda: __import__("tests.test_cockpit_list_panel", fromlist=["NOW"]).NOW)
    assert "reaches" not in ck.panel("tray", credential_class="token")["note"]
    got = ck.panel("tray", credential_class="token", q="memory line")
    assert "a search of memory reaches the entity's newest 3 episodes" in got["note"]


# --- a search counts what it shows; the box is there before the first query; a hidden row is not looked up ----------


def test_a_search_counts_the_kept_rows_it_matched_in_the_groups(rig: Rig) -> None:
    got = rig.ck.panel("tray", credential_class="token", q="noticed Z")
    kept = [r for r in got["rows"] if r["facets"]["surface"] == "kept"]
    assert kept
    assert next(g for g in got["groups"] if g["id"] == "kept")["count"] == len(kept)
    nothing = rig.ck.panel("tray", credential_class="token", q="no such words anywhere")
    assert next(g for g in nothing["groups"] if g["id"] == "kept")["count"] == 0
    assert rig.panel()["count"] == got["count"]                                   # a count of the List itself never moves


def test_a_list_with_few_rows_is_searchable_when_memory_is_there(tmp_path: Path) -> None:
    rig = Rig(tmp_path, intentions=False, band=False, board=False)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        st.record("a thing remembered", "observation")
    p = rig.panel()
    assert p["searchable"] is True and len([r for r in p["rows"]]) == 0


def test_a_kept_row_is_not_a_row_lookup_without_a_query(rig: Rig) -> None:
    from levain.cockpit.engine import RowNotFound

    got = rig.ck.panel("tray", credential_class="token", q="noticed Z")
    rid = next(r["id"] for r in got["rows"] if r["facets"]["surface"] == "kept")
    with pytest.raises(RowNotFound):
        rig.ck.panel("tray", credential_class="token", row=rid)
    with pytest.raises(RowNotFound):
        rig.ck.panel("tray", credential_class="token", row="kept:no-such")
    assert rig.ck.panel("tray", credential_class="token", row=f"intention:{rig.ids['plain']}")["rows"]


def test_kept_rows_sort_newest_first_a_missing_time_last_and_a_long_title_is_capped(tmp_path: Path) -> None:
    rig = Rig(tmp_path, intentions=False, band=False, board=False)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        st.record("first memory", "observation")
        st.record("second memory " + "x" * 400, "observation")
    got = rig.ck.panel("tray", credential_class="token", q="memory")
    kept = [r for r in got["rows"] if r["facets"]["surface"] == "kept"]
    assert [r["body"][:6] for r in kept] == ["second", "first "]
    assert all(isinstance(r["facets"]["kept_at"], str) for r in kept)
    assert len(kept[0]["title"]) <= list_panel.KEPT_TITLE_CHARS and kept[0]["body"].endswith("x" * 400)
    from levain.cockpit.registry import _list_key
    from levain.cockpit.results import RowIn

    unknown = RowIn(id="kept:z", title="t", facets={"surface": "kept", "kept_at": ""})
    known = RowIn(id="kept:a", title="t", facets={"surface": "kept", "kept_at": "2026-11-10T00:00:00.000000Z"})
    assert sorted([unknown, known], key=lambda r: _list_key(r, None))[-1] is unknown      # type: ignore[arg-type]


# --- fix round 2: the key's domain, and the ValueError label -----------------------------------------------------


def test_an_origin_key_equal_to_another_intentions_id_does_not_share_its_keep_key() -> None:
    # the two bases live in separate domains: an intention keyed "X" and a legacy one (no origin key) whose id is "X"
    a = writes.keep_origin_key({"id": "intention-1", "origin_key": "intention-2"})
    b = writes.keep_origin_key({"id": "intention-2"})
    assert a != b


def test_a_valueerror_that_is_not_a_retired_key_is_a_plain_refusal_that_names_its_reason(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = rig.ids["plain"]

    def boom(self: Any, *a: Any, **k: Any) -> Any:
        raise ValueError("content must not be empty")

    monkeypatch.setattr(Store, "record", boom)
    with pytest.raises(EditError) as ei:
        _keep(rig, sid)
    assert ei.value.code != "keep_key_retired" and "content must not be empty" in str(ei.value)
    assert "compost the item by hand" not in str(ei.value)
    assert sid in _open_ids(rig)
