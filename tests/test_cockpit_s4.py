"""S4 of the shared cockpit: Open loops before the Tray, the Tray's "by age" view, and the federated
Episodes source (kernel commit daefa83). Each class names the claim it holds."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit import (
    Absent,
    Cockpit,
    CockpitRegistrationError,
    Fault,
    ProviderSpec,
    Read,
    RowIn,
)
from levain.cockpit.engine import ReadContext
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.registry import ORDERINGS, apply_ordering
from levain.cockpit.verbs import VerbRegistry, VerbView
from levain.dashboard import SubstrateSource
from levain.web_server import INSTALL_CLASS
from tests.test_dashboard import _make_levain_install

TODAY = date(2026, 10, 10)
CRED = {"class": "token", "name": "browser", "device_class": None}


def _clock() -> datetime:
    return datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _install(tmp_path: Path) -> tuple[Path, SubstrateSource, dict[str, str]]:
    root = _make_levain_install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    ids = {
        "mid": store.add(type="task", text="five days old", disposition="seed", today=date(2026, 10, 5))["id"],
        "old": store.add(type="task", text="nine days old", disposition="seed", today=date(2026, 10, 1))["id"],
        "new": store.add(type="task", text="made today", disposition="seed", today=TODAY)["id"],
        "undated": store.add(type="task", text="no created", disposition="seed", today=TODAY)["id"],
        "future": store.add(type="task", text="created ahead", disposition="seed", today=date(2026, 10, 20))["id"],
        "junk": store.add(type="task", text="unparseable created", disposition="seed", today=TODAY)["id"],
        "loop": store.add(type="task", text="a loop", tier="hot", today=TODAY)["id"],
    }
    path = root / ".levain" / "memory.spores.json"
    data = json.loads(path.read_text())
    for s in data["spores"]:
        if s["id"] == ids["undated"]:
            del s["created"]
        if s["id"] == ids["junk"]:
            s["created"] = "not a date"
    path.write_text(json.dumps(data))
    return root, SubstrateSource.local(root), ids


def _cockpit(src: SubstrateSource, feeds: dict[str, Any] | None = None) -> Cockpit:
    ck = build_default_cockpit(src, clock=_clock, episode_feeds=feeds)
    ck.attach_verbs(VerbView(VerbRegistry(None), ck, INSTALL_CLASS))
    return ck


@pytest.fixture
def rig(tmp_path: Path):
    root, src, ids = _install(tmp_path)
    return src, ids, _cockpit(src)


def _tray(ck: Cockpit, **kw: Any) -> dict[str, Any]:
    return ck.panel("tray", credential_class="token", **kw)  # type: ignore[return-value]


def _ids(panel: dict[str, Any]) -> list[str]:
    return [r["id"] for r in panel["rows"]]


class TestOperateOrder:
    def test_loops_lead_the_tray(self, rig) -> None:
        _src, _ids_, ck = rig
        zone = next(z for z in ck.manifest(CRED)["regions"]["zones"] if z["id"] == "operate")
        assert zone["panels"].index("loops") < zone["panels"].index("tray")
        heads = ck.manifest(CRED)["panels"]
        assert heads["loops"]["rank"] == 0 and heads["tray"]["rank"] == 1


class TestTrayAgeView:
    def test_head_declares_the_view_only_on_the_tray(self, rig) -> None:
        _src, _i, ck = rig
        heads = ck.manifest(CRED)["panels"]
        assert heads["tray"]["views"] == [{"id": "age", "title": "By age", "order": "spore.age"}]
        for pid in ("loops", "keep", "episodes", "edits"):
            assert heads[pid]["views"] is None

    def test_view_rows_oldest_first_undated_last(self, rig) -> None:
        _src, ids, ck = rig
        age = _tray(ck)["view_rows"]["age"]
        sid = lambda k: f"spore:{ids[k]}"  # noqa: E731
        assert age[:3] == [sid("old"), sid("mid"), sid("new")]
        assert set(age[3:]) == {sid("undated"), sid("future"), sid("junk")}
        assert age[3:] == sorted(age[3:])

    def test_default_rows_keep_the_tray_order(self, rig) -> None:
        _src, _i, ck = rig
        p = _tray(ck)
        ins = [RowIn(id=r["id"], title=r["title"], facets=r["facets"]) for r in p["rows"]]
        resorted = [r.id for r, _g in apply_ordering("spore.tray", ins, TODAY)]
        assert _ids(p) == resorted
        assert _ids(p) != p["view_rows"]["age"]

    def test_view_rows_cover_exactly_the_sent_rows(self, rig) -> None:
        _src, ids, ck = rig
        p = _tray(ck)
        assert sorted(p["view_rows"]["age"]) == sorted(_ids(p))
        q = _tray(ck, q="days old")
        assert sorted(_ids(q)) == sorted([f"spore:{ids['old']}", f"spore:{ids['mid']}"])
        assert q["view_rows"]["age"] == [f"spore:{ids['old']}", f"spore:{ids['mid']}"]

    def test_age_facet_and_badge(self, rig) -> None:
        _src, ids, ck = rig
        rows = {r["id"]: r for r in _tray(ck)["rows"]}
        old = rows[f"spore:{ids['old']}"]
        assert old["facets"]["age_days"] == 9
        assert {"kind": "age_days", "value": 9} in old["badges"]
        assert rows[f"spore:{ids['new']}"]["facets"]["age_days"] == 0
        for k in ("undated", "future", "junk"):
            r = rows[f"spore:{ids[k]}"]
            assert "age_days" not in r["facets"]
            assert all(b["kind"] != "age_days" for b in r["badges"])


def _simple(pid: str = "p", **kw: Any) -> ProviderSpec:
    base: dict[str, Any] = dict(
        id=pid, kind="triage-list", title=pid, priority="feed", read=lambda ctx: Read(rows=()),
        order="time.desc", facets=frozenset({"at"}), version_fields=("id",))
    base.update(kw)
    return ProviderSpec(**base)


class TestViewRegistration:
    def test_a_valid_view_registers(self) -> None:
        Cockpit().register(_simple(views=(("age", "By age", "spore.age"),), order="spore.tray",
                                   facets=frozenset({"age_days"})))

    @pytest.mark.parametrize("views", [
        (("Bad Id", "t", "spore.age"),),
        (("", "t", "spore.age"),),
        (("default", "t", "spore.age"),),
        (("a", "t", "spore.age"), ("a", "u", "spore.age")),
        (("a", "t", "no.such.ordering"),),
        (("a", "t", "spore.tray"),),
        (("a", "t"),),
        (("a", 1, "spore.age"),),
    ])
    def test_refused(self, views: Any) -> None:
        with pytest.raises(CockpitRegistrationError):
            Cockpit().register(_simple(views=views))

    def test_only_a_triage_list_has_views(self) -> None:
        with pytest.raises(CockpitRegistrationError):
            Cockpit().register(ProviderSpec(
                "g", "gauge", "g", "feed", lambda ctx: Read(value={}), views=(("a", "t", "spore.age"),)))


def _ep(rid: str, at: str, *, agent: str | None = None, content: str = "c", **over: Any) -> RowIn:
    facets: dict[str, Any] = {"episode_type": "observation", "source": "s", "at": at, "tags": []}
    if agent:
        facets["agent"] = agent
    return RowIn(
        id=rid, title=content, body=content, facets={**facets, **over.pop("facets", {})},
        stored=over.pop("stored", {"id": rid, "timestamp": at, "type": "observation", "source": "s",
                                   "content": content, "tags": []}))


def _feed(*rows: RowIn):
    return lambda ctx: Read(rows=tuple(rows))


def _episodes(src: SubstrateSource, feeds: dict[str, Any]) -> dict[str, Any]:
    return _cockpit(src, feeds).panel("episodes", credential_class="token")  # type: ignore[return-value]


@pytest.fixture
def src(tmp_path: Path) -> SubstrateSource:
    return _install(tmp_path)[1]


class TestFederatedEpisodes:
    def test_merge_agent_ids_and_interleave(self, src: SubstrateSource) -> None:
        feeds = {
            "ana": _feed(_ep("x1", "2999-01-01T00:00:00+00:00", agent="Anansi"),
                         _ep("x2", "2000-01-01T00:00:00Z")),
        }
        p = _episodes(src, feeds)
        assert p["status"] == "ok"
        rows = {r["id"]: r for r in p["rows"]}
        assert rows["feed:ana:x1"]["facets"]["agent"] == "Anansi"
        assert rows["feed:ana:x2"]["facets"]["agent"] == "ana"
        own = [r for r in p["rows"] if r["id"].startswith("episode:")]
        assert own and all(r["facets"]["agent"] == "Sage" for r in own)
        order = _ids(p)
        assert order[0] == "feed:ana:x1" and order[-1] == "feed:ana:x2"
        assert {i for i in order if i.startswith("episode:")} == {r["id"] for r in own}

    def test_at_is_normalised_so_the_clock_orders_not_the_string(self, src: SubstrateSource) -> None:
        feeds = {"a": _feed(
            _ep("early", "2999-01-01T01:00:00+05:00"),    # 2998-12-31 20:00Z
            _ep("late", "2998-12-31T23:00:00+00:00"),     # later in true time, earlier as a string
            _ep("naive", "2997-06-01T00:00:00"),          # no zone: kept as given
        )}
        p = _episodes(src, feeds)
        rows = {r["id"]: r for r in p["rows"]}
        assert rows["feed:a:early"]["facets"]["at"] == "2998-12-31T20:00:00.000000Z"
        assert rows["feed:a:late"]["facets"]["at"] == "2998-12-31T23:00:00.000000Z"
        assert rows["feed:a:naive"]["facets"]["at"] == "2997-06-01T00:00:00"
        order = _ids(p)
        assert order.index("feed:a:late") < order.index("feed:a:early") < order.index("feed:a:naive")

    @pytest.mark.parametrize("name,feed", [
        ("fault", lambda ctx: Fault("down")),
        ("boom", lambda ctx: (_ for _ in ()).throw(RuntimeError("kaput"))),
        ("norows", lambda ctx: Read(rows=None, value={})),
        ("badfacet", _feed(_ep("b", "2999-01-01T00:00:00Z", facets={"disposition": "seed"}))),
        ("badstored", _feed(_ep("b", "2999-01-01T00:00:00Z",
                                stored={"id": "b", "timestamp": "t", "type": "o", "source": "s",
                                        "content": "c", "tags": [], "extra": 1}))),
        ("notrowin", lambda ctx: Read(rows=({"id": "b"},))),
    ])
    def test_a_broken_feed_contributes_nothing_and_is_named(self, src: SubstrateSource, name: str, feed: Any) -> None:
        good = _feed(_ep("g", "2999-01-01T00:00:00Z"))
        p = _episodes(src, {"good": good, name: feed})
        assert p["status"] == "partial"
        assert any(name in s["reason"] for s in p["skipped"])
        ids = _ids(p)
        assert "feed:good:g" in ids
        assert not any(i.startswith(f"feed:{name}:") for i in ids)
        assert any(i.startswith("episode:") for i in ids)

    def test_a_broken_row_drops_the_whole_feed(self, src: SubstrateSource) -> None:
        feed = _feed(_ep("ok1", "2999-01-01T00:00:00Z"),
                     _ep("bad", "2999-01-01T00:00:00Z", facets={"disposition": "seed"}))
        p = _episodes(src, {"f": feed})
        assert not any(i.startswith("feed:f:") for i in _ids(p))

    def test_an_absent_feed_is_a_note_not_an_error(self, src: SubstrateSource) -> None:
        p = _episodes(src, {"gone": lambda ctx: Absent("no db")})
        assert p["status"] in ("ok", "empty")
        assert "gone: not configured: no db" in p["note"]
        assert p["skipped"] == []
        assert any(i.startswith("episode:") for i in _ids(p))

    @pytest.mark.parametrize("name", ["Bad", "", "-x", "a b", "x" * 33, "a/b"])
    def test_a_bad_feed_name_is_refused_at_build(self, src: SubstrateSource, name: str) -> None:
        with pytest.raises(ValueError):
            build_default_cockpit(src, clock=_clock, episode_feeds={name: _feed()})

    def test_a_non_callable_feed_is_refused_at_build(self, src: SubstrateSource) -> None:
        with pytest.raises(ValueError):
            build_default_cockpit(src, clock=_clock, episode_feeds={"ok": "nope"})  # type: ignore[dict-item]

    def test_the_feed_name_boundary_is_allowed(self, src: SubstrateSource) -> None:
        build_default_cockpit(src, clock=_clock, episode_feeds={"a" + "b" * 31: _feed()})


class TestTombstoneAppliesToOwnRowsOnly:
    def _verbs(self, row: dict[str, Any]) -> list[str]:
        return [a["verb"] for a in row.get("actions") or []]

    def test_offered_on_own_not_on_feed_rows(self, src: SubstrateSource) -> None:
        p = _episodes(src, {"f": _feed(_ep("x", "2999-01-01T00:00:00Z"))})
        own = next(r for r in p["rows"] if r["id"].startswith("episode:"))
        feed = next(r for r in p["rows"] if r["id"] == "feed:f:x")
        assert "episode_tombstone" in self._verbs(own)
        assert "episode_tombstone" not in self._verbs(feed)

    def test_read_one_finds_own_rows_only(self, src: SubstrateSource) -> None:
        ck = _cockpit(src, {"f": _feed(_ep("x", "2999-01-01T00:00:00Z"))})
        p = ck.panel("episodes", credential_class="token")
        own_id = next(r["id"] for r in p["rows"] if r["id"].startswith("episode:"))
        assert isinstance(ck.read_one("episodes", own_id), Read)
        assert isinstance(ck.read_one("episodes", "feed:f:x"), Absent)
