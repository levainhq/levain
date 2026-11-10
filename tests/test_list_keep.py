"""THE LIST's Keep (row 182, Phill 11-10 Q1-Q4): ``spore_keep`` is a row verb on a ``spore:`` row only. It writes an anneal
``observation`` episode (the spore's words verbatim plus a source tag, keyed on the spore's origin key), THEN composts the spore
(tier C1). A crash between the two leaves the spore open and the retry lands no second episode. The List's search reaches the
entity's episodes as ``kept`` rows, which a read with no query never carries."""

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
    return apply_edit(rig.src.write_scope, {"kind": "spore_keep", "spore_id": sid, **extra})


# --- the verb: episode first, then compost ------------------------------------------------------------------------


def test_a_keep_writes_the_episode_with_the_words_verbatim_then_composts_the_spore(rig: Rig) -> None:
    sid = rig.ids["plain"]
    out = _keep(rig, sid)
    assert out["ok"] and out["action"] == "keep" and out["source"] == f"spore:{sid}"
    [ep] = _keep_episodes(rig)
    assert ep.type == "observation" and ep.content.startswith("a plain seed\n") and sid in ep.content
    assert out["episode_id"] == ep.id and ep.id in out["receipt_line"]
    assert sid not in _open_ids(rig)                       # composted
    assert rig.sp.get(sid)["resolution"]["kind"] == "composted"


def test_keep_is_a_row_verb_on_a_spore_row_only_and_fires_at_c1_through_the_cockpit(rig: Rig) -> None:
    rows = rig.rows()
    spore = rows[f"spore:{rig.ids['plain']}"]
    act = next(a for a in spore["actions"] if a["verb"] == "spore_keep")
    assert act["tier"] == "C1"
    for rid in (f"band:{rig.q.id}", "board:levain:a"):     # no write by construction
        assert all(a["verb"] != "spore_keep" for a in rows[rid]["actions"])
        with pytest.raises(EditError):
            dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                     install_class=INSTALL_CLASS,
                     req={"verb": "spore_keep", "panel_id": "tray", "row_id": rid, "row_version": rows[rid]["version"],
                          "params": {}})
    out = dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                   install_class=INSTALL_CLASS,
                   req={"verb": "spore_keep", "panel_id": "tray", "row_id": f"spore:{rig.ids['plain']}",
                        "row_version": spore["version"], "params": {}})
    assert out["ok"] is True
    assert len(_keep_episodes(rig)) == 1 and rig.ids["plain"] not in _open_ids(rig)


def test_the_episode_key_is_the_spores_origin_key_else_its_id() -> None:
    assert writes.keep_origin_key({"id": "spore-1", "origin_key": "ok-9"}) == "keep:ok-9"
    assert writes.keep_origin_key({"id": "spore-1"}) == "keep:id:spore-1"


# --- a retry lands once; the episode is written before the compost ----------------------------------------------


def _fail_compost(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("injected: the spore store went away after the episode")
    monkeypatch.setattr(writes, "_keep_compost", boom)


def test_a_failure_after_the_episode_leaves_the_spore_open_and_the_retry_lands_the_episode_once(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]
    with monkeypatch.context() as m:
        _fail_compost(m)
        with pytest.raises(EditError) as ei:
            _keep(rig, sid)
        assert ei.value.http_status == 503
    [first] = _keep_episodes(rig)                          # the episode is down ...
    assert sid in _open_ids(rig)                           # ... and the spore is still open (episode FIRST, compost after)
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
        apply_edit(rig.src.write_scope, {"kind": "spore_keep", "spore_id": sid}, expected_version="0" * 16)
    assert ei2.value.code == "stale"
    assert sid in _open_ids(rig) and _keep_episodes(rig) == []


def test_keeping_an_already_resolved_spore_is_stale_and_writes_nothing_more(rig: Rig) -> None:
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
    assert row["actions"] == []                            # no spore verb applies to a kept row
    assert rig.panel()["count"] == before


def test_the_kept_part_is_capped_by_the_episode_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain.cockpit import providers

    rig = Rig(tmp_path, spores=False, band=False, board=False)
    with Store(str(rig.src.anneal.episodic_db)) as st:
        for i in range(7):
            st.record(f"memory line {i}", "observation")
    monkeypatch.setattr(providers, "EPISODE_LIMIT", 3)
    # a fresh cockpit: the view is built at the cap
    from levain.cockpit.providers import build_default_cockpit
    ck = build_default_cockpit(rig.src, clock=lambda: __import__("tests.test_cockpit_list_panel", fromlist=["NOW"]).NOW)
    got = ck.panel("tray", credential_class="token", q="memory line")
    assert len([r for r in got["rows"] if r["facets"]["surface"] == "kept"]) == 3


def test_a_retry_after_the_words_changed_is_refused_not_duplicated(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = rig.ids["plain"]
    with monkeypatch.context() as m:
        _fail_compost(m)
        with pytest.raises(EditError):
            _keep(rig, sid)
    rig.sp.update(sid, text="a plain seed, reworded")
    with pytest.raises(EditError) as ei:
        _keep(rig, sid)
    assert ei.value.code == "keep_conflict" and len(_keep_episodes(rig)) == 1 and sid in _open_ids(rig)
