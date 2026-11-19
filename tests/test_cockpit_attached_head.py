"""The head layer of the attached mode: what a manifest and a panel read build from the rows a derive host published.

Every table is built from the engine's own types over a real snapshot file (the harness is ``test_cockpit_attached_engine``'s).
What the file pins: a manifest reads the store once; an attached head's etag stands on the row's body hash and the freshness
verdict, so a derive that changed nothing, a clock tick of a row's age, and idle time move no etag while a new body, a stale or
pending flip, the credential class, the day and the verb revision each do; the head layer keeps the composed head under the
snapshot's own key and misses on each input; the manifest and heads carry the D11 freshness; a bare cockpit's answers are
today's."""
from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from levain.cockpit import engine
from levain.cockpit import snapshot_host as sh
from levain.cockpit.derive import DeriveResult
from levain.cockpit.engine import Cockpit, ProviderSpec, ReadContext
from levain.cockpit.list_panel import _age_text
from levain.cockpit.results import Read, RowIn
from tests.test_cockpit_attached_engine import CRED, PANELS, T0, FakeVerbView, Recorder, Rig, list_spec
from tests.test_cockpit_ck1 import ROW

TOKEN = {"class": "token", "device_class": None}


def beat(rig: Rig, seconds: float = 1.0) -> None:
    """Let ``seconds`` pass with the worker alive: its heartbeat is written at the new time (a derive that is due runs)."""
    rig.clock.advance(seconds)
    rig.loop.run_once(rig.clock.epoch())


def head(rig: Rig, pid: str = "alpha", cred: dict[str, Any] = CRED) -> dict[str, Any]:
    return rig.ck.manifest(cred)["panels"][pid]


class ValidRig(Rig):
    """A rig whose derives publish a ``valid_until`` for the panels named in ``valid`` (epoch seconds)."""
    valid: "dict[str, float] | None" = None

    def derive_fn(self, pid: str) -> DeriveResult:
        snap = self.ck.derive_panel(pid)
        status, body = sh.encode_snap(snap)
        return DeriveResult(status, body, valid_until=(self.valid or {}).get(pid), error=snap.error)


@pytest.fixture()
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.close()


def clocked_spec(pid: str, ref: dict[str, str]) -> ProviderSpec:
    """A band-like panel: one row whose ``band_age`` is a clock reading the provider rewrites, over a stable ``band_since``."""
    def read(ctx: ReadContext) -> Read:
        return Read(rows=(RowIn(f"{pid}:a", "a", {"at": "2026-11-19", "band_age": ref["age"],
                                                   "band_since": (T0 - timedelta(minutes=58)).isoformat()},
                                stored={"id": f"{pid}:a"}),))

    return ProviderSpec(id=pid, kind="triage-list", title=pid, priority="feed", read=read, order="time.desc",
                        facets=frozenset({"at", "band_age", "band_since"}), version_fields=("id",))


# --- 2. the etag: what moves it and what does not -----------------------------------------------------------------------------
def test_the_etag_is_unmoved_by_a_rederive_of_an_unchanged_body(rig: Rig) -> None:
    rig.derive()
    before, gen = head(rig), rig.reader.read("alpha").gen
    top = rig.ck.manifest(CRED)["etag"]
    beat(rig, 5)
    rig.derive("alpha")
    after = head(rig)
    assert rig.reader.read("alpha").gen == gen
    assert after["as_of"] != before["as_of"]                      # the row was derived again, and says so
    assert after["etag"] == before["etag"] and rig.ck.manifest(CRED)["etag"] == top


def test_the_etag_is_unmoved_by_a_tick_of_a_clock_facet(tmp_path: Path) -> None:
    ref = {"age": "58m"}
    r = Rig(tmp_path, ("band",), specs={"band": clocked_spec("band", ref)})
    try:
        r.derive()
        before, gen = head(r, "band"), r.reader.read("band").gen
        ref["age"] = "59m"
        beat(r, 5)
        r.derive("band")
        assert r.reader.read("band").gen == gen                  # the codec leaves the age out of the body
        assert head(r, "band")["etag"] == before["etag"]
    finally:
        r.close()


def test_the_etag_is_unmoved_by_idle_time_while_the_worker_beats(rig: Rig) -> None:
    rig.derive()
    before = head(rig)["etag"], head(rig, "beta")["etag"]
    for _ in range(45):                 # past the loop's own backstop re-derive
        beat(rig, 1)
    assert (head(rig)["etag"], head(rig, "beta")["etag"]) == before
    assert head(rig)["stale"] is False


def test_the_etag_moves_with_a_new_body_and_only_that_panels(rig: Rig) -> None:
    rig.derive()
    a, b = head(rig)["etag"], head(rig, "beta")["etag"]
    top = rig.ck.manifest(CRED)["etag"]
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.derive("alpha")
    assert head(rig)["etag"] != a and head(rig, "beta")["etag"] == b
    assert rig.ck.manifest(CRED)["etag"] != top


def test_the_etag_moves_when_stale_flips_and_comes_back(rig: Rig) -> None:
    rig.derive()
    fresh = head(rig)
    assert fresh["stale"] is False
    rig.clock.advance(10)                                         # the worker has not beaten for longer than it may
    gone = head(rig)
    assert gone["stale"] is True and gone["etag"] != fresh["etag"]
    assert rig.ck.manifest(CRED)["etag"] != ""
    beat(rig, 1)
    rig.derive("alpha")
    back = head(rig)
    assert back["stale"] is False and back["etag"] == fresh["etag"]


def test_the_etag_moves_when_pending_flips(tmp_path: Path) -> None:
    flag = {"on": False}
    r = Rig(tmp_path, pending=lambda pid: flag["on"] and pid == "alpha")
    try:
        r.derive()
        quiet, other = head(r), head(r, "beta")
        flag["on"] = True
        busy = head(r)
        assert busy["pending"] is True and quiet["pending"] is False
        assert busy["etag"] != quiet["etag"] and head(r, "beta")["etag"] == other["etag"]
        flag["on"] = False
        assert head(r)["etag"] == quiet["etag"]
    finally:
        r.close()


def test_the_etag_moves_with_the_credential_class_the_day_and_the_verb_revision(rig: Rig) -> None:
    view = FakeVerbView()
    rig.ck.attach_verbs(view)
    rig.derive()
    base = head(rig)["etag"]
    top = rig.ck.manifest(CRED)["etag"]
    assert head(rig, cred=TOKEN)["etag"] != base                  # (g) class
    view.rev = "r2"
    assert head(rig)["etag"] != base and rig.ck.manifest(CRED)["etag"] != top      # (i) verb revision
    view.rev = "r1"
    assert head(rig)["etag"] == base
    rig.clock.advance(days=1)
    beat(rig, 1)
    rig.derive("alpha", "beta")
    assert head(rig)["etag"] != base                              # (h) day


def test_the_etag_is_never_built_from_a_serialisation_of_the_value(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The text a bare head hashes (``_Snap.etag_frags``) is never built for an attached one."""
    rig.derive()
    built: list[int] = []
    real = engine._Snap.etag_frags

    def spy(self: Any) -> Any:
        built.append(1)
        return real(self)

    monkeypatch.setattr(engine._Snap, "etag_frags", spy)
    rig.ck.manifest(CRED)
    rig.ck.panel("alpha")
    rig.ck.panel("alpha", q="a")
    rig.ck.change_probe().snapshot()
    assert built == []


def test_the_probe_etags_are_the_manifest_etags_and_read_the_store_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.derive()
    calls = _count_reads(rig, monkeypatch)
    etags, _version = rig.ck.change_probe().snapshot()
    assert calls["read_manifest"] == 1 and calls["read"] == calls["meta"] == calls["read_all"] == 0
    assert etags == {pid: head(rig, pid)["etag"] for pid in PANELS}


# --- 1. one read transaction per manifest ------------------------------------------------------------------------------------
def _count_reads(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Counter:
    calls: Counter = Counter()
    for name in ("read", "read_all", "read_manifest", "meta", "streams"):
        orig = getattr(rig.reader, name)

        def wrapped(*a: Any, _name: str = name, _orig: Any = orig, **k: Any) -> Any:
            calls[_name] += 1
            return _orig(*a, **k)
        monkeypatch.setattr(rig.reader, name, wrapped)
    return calls


def test_a_manifest_reads_the_store_in_one_transaction_however_many_panels(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ids = tuple(f"p{i}" for i in range(12))
    r = Rig(tmp_path, ids)
    try:
        r.derive()
        calls = _count_reads(r, monkeypatch)
        manifest = r.ck.manifest(CRED)
        assert list(manifest["panels"]) == [s.id for s in r.ck._ordered_specs(r.ck._specs)] and set(manifest["panels"]) == set(ids)
        assert all(h["status"] == "ok" for h in manifest["panels"].values())
        assert calls["read_manifest"] == 1
        assert calls["read"] == calls["meta"] == calls["read_all"] == 0
    finally:
        r.close()


def test_the_stream_row_is_read_at_most_once_per_its_window(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.derive()
    calls = _count_reads(rig, monkeypatch)
    for _ in range(5):
        rig.ck.manifest(CRED)
    assert calls["streams"] == 1 and calls["read_manifest"] == 5
    monkeypatch.setattr(engine, "STREAMS_READ_TTL_S", 0.0)
    rig.ck.manifest(CRED)
    assert calls["streams"] == 2


def test_no_provider_and_no_pool_runs_for_a_panel_the_rows_stand_behind(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, ("alpha", "beta", "mine"))
    try:
        r.ck.mark_serve_composed(["mine"])
        r.derive()
        derived = r.rec.count()
        pooled: list[list[str]] = []
        real = Cockpit._parallel

        def spy(fn: Any, items: list[Any]) -> list[Any]:
            pooled.append([s.id for s in items])
            return real(fn, items)
        monkeypatch.setattr(Cockpit, "_parallel", staticmethod(spy))
        errors: list[BaseException] = []

        def request() -> None:
            try:
                for _ in range(3):
                    r.ck.manifest(CRED)
                    r.ck.panel("alpha")
                    r.ck.panel("mine")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=request, name=f"request-{i}") for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        assert pooled and all(ids == ["mine"] for ids in pooled)      # only the serve-composed panel is read in the pool
        made = [c for c in r.rec.request_side()]
        assert made and {c[0] for c in made} == {"mine"}               # and only its provider ran on a request
        assert r.rec.count("alpha") == r.rec.count("beta") == derived // 3
        got = r.ck.manifest(CRED)["panels"]["mine"]
        assert (got["valid_until"], got["stale"], got["pending"]) == (None, False, False)
    finally:
        r.close()


def test_an_attached_read_never_touches_the_shared_read_cache_the_obs_generation_or_read_after(
        rig: Rig) -> None:
    rig.ck.read_cache_ttl_s = 1.5
    rig.derive()
    rig.ck.manifest(CRED)
    rig.ck.manifest(CRED, read_after=engine.time.monotonic())
    rig.ck.panel("alpha")
    assert rig.ck._rc == {} and rig.ck._obs_gen == 0
    rig.rec.rows["alpha"] = ["new"]
    rig.derive("alpha")
    # no TTL lag: the read right after the derive is the new rows, though the shared read cache's window is open
    assert [r["id"] for r in rig.ck.panel("alpha")["rows"]] == ["alpha:new"]
    assert head(rig)["count"] == 1


# --- 5. D11: the head and the manifest --------------------------------------------------------------------------------------
def test_each_head_carries_its_rows_freshness(tmp_path: Path) -> None:
    r = ValidRig(tmp_path)
    try:
        r.valid = {"alpha": (T0 + timedelta(seconds=60)).timestamp()}
        r.derive()
        a, b = head(r), head(r, "beta")
        assert a["valid_until"] == (T0 + timedelta(seconds=60)).isoformat() and b["valid_until"] is None
        assert (a["stale"], a["pending"], a["as_of"]) == (False, False, T0.isoformat())
        r.clock.advance(61)                                       # past the bound, the worker beating (the loop would re-derive)
        r.writer.heartbeat(r.clock.epoch())
        assert head(r)["stale"] is True and head(r, "beta")["stale"] is False
    finally:
        r.close()


def test_a_panel_without_a_row_is_stale_and_says_so(rig: Rig) -> None:
    got = head(rig)
    assert (got["status"], got["error"], got["stale"], got["as_of"], got["valid_until"]) == (
        "error", "not derived yet", True, None, None)


def test_the_manifest_carries_the_workers_freshness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "STREAMS_READ_TTL_S", 0.0)
    r = Rig(tmp_path, deriving=lambda: ["beta", "alpha"])
    try:
        r.derive()
        fresh = r.ck.manifest(CRED)["freshness"]
        assert fresh["worker"] == "live" and fresh["heartbeat_age_s"] == 0.0 and fresh["deriving"] == ["alpha", "beta"]
        assert fresh["stream"] == {"state": "ok", "last_event_at": None}
        r.writer.set_stream("inputs", state="degraded", last_event_at=T0.timestamp() + 5, detail="x")
        assert r.ck.manifest(CRED)["freshness"]["stream"] == {
            "state": "degraded", "last_event_at": (T0 + timedelta(seconds=5)).isoformat()}
        r.clock.advance(10)
        stale = r.ck.manifest(CRED)["freshness"]
        assert stale["worker"] == "stale" and stale["heartbeat_age_s"] == 10.0
        r.clock.advance(60)
        assert r.ck.manifest(CRED)["freshness"]["worker"] == "down"
    finally:
        r.close()


def test_the_freshness_line_is_in_no_etag(rig: Rig) -> None:
    rig.derive()
    top = rig.ck.manifest(CRED)["etag"]
    for _ in range(3):
        beat(rig, 1)                  # the heartbeat age and the stream row move; no panel does
        assert rig.ck.manifest(CRED)["etag"] == top


def test_with_no_worker_at_all_the_line_says_down_and_every_panel_stale(tmp_path: Path) -> None:
    r = Rig(tmp_path, worker=False)
    try:
        m = r.ck.manifest(CRED)
        assert m["freshness"]["worker"] == "down" and m["freshness"]["heartbeat_age_s"] is None
        assert m["freshness"]["stream"] == {"state": None, "last_event_at": None}
        assert all(h["stale"] is True for h in m["panels"].values())
    finally:
        r.close()


# --- 4. band_age ------------------------------------------------------------------------------------------------------------
def test_a_band_rows_age_is_set_per_request_from_its_band_since_and_never_stored(tmp_path: Path) -> None:
    ref = {"age": "58m"}
    r = Rig(tmp_path, ("band",), specs={"band": clocked_spec("band", ref)})
    try:
        r.derive()
        assert "band_age" not in r.snap("band").rows[0]["facets"]          # the decoded row holds none
        first = r.ck.panel("band")["rows"][0]["facets"]
        assert first["band_age"] == "58m" == _age_text(first["band_since"], r.clock())
        beat(r, 180)
        later = r.ck.panel("band")["rows"][0]["facets"]
        assert later["band_age"] == "1h" == _age_text(later["band_since"], r.clock())
        assert r.ck._panel_cache
        for _spec, value in r.ck._panel_cache.values():
            assert all("band_age" not in row["facets"] for row in value["rows"])      # no cached payload ever carried one
        assert "band_age" in r.ck.panel("band", q="a")["rows"][0]["facets"]
        assert "band_age" in r.ck.panel("band", row="band:a")["rows"][0]["facets"]
        assert "band_age" in r.ck.panel("band", profile="compact", q="a")["rows"][0]["facets"]
    finally:
        r.close()


# --- 3. the head-layer cache -------------------------------------------------------------------------------------------------
class Ins:
    """The inputs of one head composition, each replaceable by itself."""

    def __init__(self, rig: Rig, pid: str = "alpha") -> None:
        self.rig = rig
        self.spec = rig.spec(pid)
        ctx = ReadContext(rig.clock())
        self.kw: dict[str, Any] = {"spec": self.spec, "snap": rig.snap(pid), "cred": "none", "today": ctx.today,
                                   "auth": rig.ck.authority(), "now": ctx.now}

    def head(self, **over: Any) -> dict[str, Any]:
        k = {**self.kw, **over}
        return self.rig.ck._attached_head(k["spec"], k["snap"], k["cred"], k["today"], k["auth"], k["now"])


def test_the_head_cache_returns_one_object_and_misses_on_each_input(rig: Rig) -> None:
    view = FakeVerbView()
    rig.ck.attach_verbs(view)
    rig.derive()
    ins = Ins(rig)
    snap, auth = ins.kw["snap"], ins.kw["auth"]
    base = ins.head()
    assert ins.head() is base and head(rig) is head(rig)
    gen, sha = snap.row_key
    fresh = snap.fresh_info
    other_labels = SimpleNamespace(revision=("x", 1, 2, "y"), error=None, provenance=lambda k: None, label=lambda k: "unverified")
    changes: dict[str, dict[str, Any]] = {
        "body_sha": {"snap": dataclasses.replace(snap, row_key=(gen, "0" * 64))},
        "status": {"snap": dataclasses.replace(snap, status="partial")},
        "error": {"snap": dataclasses.replace(snap, error="boom")},
        "stale": {"snap": dataclasses.replace(snap, fresh_info=dataclasses.replace(fresh, stale=True))},
        "pending": {"snap": dataclasses.replace(snap, fresh_info=dataclasses.replace(fresh, pending=True))},
        "valid_until": {"snap": dataclasses.replace(snap, fresh_info=dataclasses.replace(fresh, valid_until="2030-01-01T00:00:00+00:00"))},
        "as_of": {"snap": dataclasses.replace(snap, as_of="2026-11-19T15:00:01+00:00")},
        "class": {"cred": "token"},
        "today": {"today": ins.kw["today"] + timedelta(days=1)},
        "authority revision": {"auth": dataclasses.replace(auth, authority_error="unreadable")},
        "labels": {"auth": dataclasses.replace(auth, labels=other_labels)},
        "spec identity": {"spec": ins.spec.replaced(title=ins.spec.title)},
    }
    seen = [base]
    for name, over in changes.items():
        got = ins.head(**over)
        assert all(got is not s for s in seen), name
        assert ins.head(**over) is got, name              # and each is itself a hit the second time
        seen.append(got)
    view.rev = "r2"                                       # the verb revision
    revised = ins.head()
    assert all(revised is not s for s in seen)
    view.rev = "r1"
    assert ins.head() is base                             # nothing above evicted it
    newer = ins.head(snap=dataclasses.replace(snap, row_key=(gen + 1, sha)))      # a later generation of the panel
    assert all(newer is not s for s in seen) and newer is not revised
    assert ins.head(snap=dataclasses.replace(snap, row_key=(gen + 1, sha))) is newer


def test_the_head_cache_is_bounded_and_least_recently_used(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "HEAD_CACHE_MAX", 4)
    rig.derive()
    ins = Ins(rig)
    day0 = ins.kw["today"]
    first = ins.head()
    for i in range(1, 4):
        ins.head(today=day0 + timedelta(days=i))
    assert ins.head() is first                            # touched: now the most recent
    for i in range(4, 10):
        ins.head(today=day0 + timedelta(days=i))
    assert len(rig.ck._head_cache) == 4
    assert ins.head() is not first                        # long gone
    assert ins.head(today=day0 + timedelta(days=9)) is ins.head(today=day0 + timedelta(days=9))


def test_the_panel_cache_is_bounded(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "PANEL_CACHE_MAX", 3)
    rig.derive()
    for pid in PANELS:
        for row in (None, f"{pid}:a", f"{pid}:b"):
            rig.ck.panel(pid, row=row)
    assert len(rig.ck._panel_cache) == 3


def test_a_new_generation_drops_the_old_ones_entries(rig: Rig) -> None:
    rig.derive()
    rig.ck.manifest(CRED)
    rig.ck.manifest(TOKEN)
    rig.ck.panel("alpha")
    gen = rig.reader.read("alpha").gen
    rig.rec.rows["alpha"] = ["q"]
    rig.derive("alpha")
    rig.ck.manifest(CRED)
    rig.ck.panel("alpha")
    new = rig.reader.read("alpha").gen
    assert new == gen + 1
    for cache in (rig.ck._head_cache, rig.ck._panel_cache):
        assert {k[2] for k in cache if k[1] == "alpha"} == {new}
    assert {k[2] for k in rig.ck._head_cache if k[1] == "beta"} == {1}         # another panel's entries stay


def test_a_panel_read_is_one_object_per_key_and_a_search_is_never_kept(rig: Rig) -> None:
    rig.derive()
    full = rig.ck.panel("alpha")
    assert rig.ck.panel("alpha") is full
    assert rig.ck.panel("alpha", profile="compact") is not full
    row = rig.ck.panel("alpha", row="alpha:a")
    assert rig.ck.panel("alpha", row="alpha:a") is row and row is not full
    assert rig.ck.panel("beta") is not full
    kept = dict(rig.ck._panel_cache)
    one, two = rig.ck.panel("alpha", q="a"), rig.ck.panel("alpha", q="a")
    assert one is not two and one == two and dict(rig.ck._panel_cache) == kept
    with pytest.raises(engine.RowNotFound):
        rig.ck.panel("alpha", row="alpha:nope")
    assert dict(rig.ck._panel_cache) == kept                           # a refusal stores nothing


def test_a_rosters_health_is_set_per_request_never_into_a_cached_head(tmp_path: Path) -> None:
    lw = "2026-10-18T00:00:00+00:00"
    row = {**ROW, "digest": {"available": True, "episodes_24h": 4, "last_wrap_at": lw}, "memory": "stateful",
           "silence_is_signal": True, "cadence": "resident"}
    spec = ProviderSpec(id="fleet", kind="roster", title="Fleet", priority="feed", read=lambda c: Read(value={"rows": [row]}))
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    r = Rig(tmp_path, ("fleet",), specs={"fleet": spec}, start=start)
    try:
        r.derive()
        ins = Ins(r, "fleet")
        soon, late = start, start + timedelta(days=2)
        a, b = ins.head(now=soon), ins.head(now=late)
        assert a["etag"] != b["etag"]                                  # the tone turned with the clock
        (_s, cached), = r.ck._head_cache.values()
        assert cached["etag"] not in (a["etag"], b["etag"])            # the cached head holds neither
        assert r.ck.panel("fleet")["value"]["rows"][0]["health"]["tone"] == "ok"
        assert not r.ck._panel_cache                                   # the payload of a roster is composed per request
        r.clock.now = late
        assert r.ck.panel("fleet")["value"]["rows"][0]["health"]["tone"] == "alarm"
    finally:
        r.close()


# --- (j) a bare cockpit is today's; an attached one differs only in what is new ---------------------------------------------
_NEW_HEAD_FIELDS = ("etag", "valid_until", "stale", "pending")


def _strip_heads(manifest: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in manifest.items() if k not in ("etag", "freshness")}
    out["panels"] = {i: {k: v for k, v in h.items() if k not in _NEW_HEAD_FIELDS} for i, h in manifest["panels"].items()}
    return out


def test_an_attached_manifest_and_panels_equal_a_bare_ones_but_for_the_new_fields_and_the_etags(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        r.ck.attach_verbs(FakeVerbView())
        bare = Cockpit(clock=r.clock)
        for pid in PANELS:
            bare.register(list_spec(pid, Recorder()))
        bare.attach_verbs(FakeVerbView())
        r.derive()
        got, want = r.ck.manifest(CRED), bare.manifest(CRED)
        assert _strip_heads(got) == _strip_heads(want)
        assert got["etag"] != want["etag"] and all(got["panels"][i]["etag"] != want["panels"][i]["etag"] for i in PANELS)
        assert "freshness" in got and "freshness" not in want
        assert all(set(got["panels"][i]) - set(want["panels"][i]) == {"valid_until", "stale", "pending"} for i in PANELS)
        for pid in PANELS:
            for kw in ({}, {"profile": "compact"}, {"row": f"{pid}:a"}, {"q": "a"}):
                a, b = r.ck.panel(pid, **kw), bare.panel(pid, **kw)
                assert {k: v for k, v in a.items() if k not in _NEW_HEAD_FIELDS} == \
                       {k: v for k, v in b.items() if k not in _NEW_HEAD_FIELDS}, (pid, kw)
    finally:
        r.close()


def test_a_bare_cockpit_makes_no_head_layer_state_and_carries_no_freshness(tmp_path: Path) -> None:
    rec = Recorder()
    ck = Cockpit(clock=Rig(tmp_path, attach=False).clock)
    ck.register(list_spec("alpha", rec))
    manifest = ck.manifest(CRED)
    ck.panel("alpha")
    ck.panel("alpha", q="a")
    assert "freshness" not in manifest
    assert not ({"valid_until", "stale", "pending"} & set(manifest["panels"]["alpha"]))
    assert not ck._head_cache and not ck._panel_cache and ck._reader is None
