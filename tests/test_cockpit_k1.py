"""K1 of the shared cockpit (design ``cockpit_manifest_DESIGN_1009.md`` §9, row K1): the manifest read
core. Every test below is named for the K1 DONE case it discharges (the case text is quoted in each
class docstring); nothing here covers verbs, tiers or authority, which are K2a/K2b."""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
from levain.cockpit.engine import NOW_ID
from levain.cockpit.providers import build_default_cockpit
from levain.dashboard import SubstrateSource
from levain.jobs import JobStore
from levain.web_server import make_server
from tests.test_dashboard import _make_levain_install

NONE_CRED = {"class": "none", "device_class": None}


# --- fixtures -----------------------------------------------------------------------------------


def _install(tmp_path: Path) -> tuple[Path, SubstrateSource]:
    root = _make_levain_install(tmp_path)
    spores = SporeStore(root / ".levain" / "memory.spores.json")
    spores.add(type="task", text="loop one", tier="hot", salience=2)
    spores.add(type="task", text="seed overdue", disposition="seed", next="2020-01-01")
    spores.add(type="task", text="handoff undated", disposition="handoff")
    spores.add(type="task", text="a long reference note " + "word " * 40 + "NEEDLEINBODY tail", disposition="note")
    (root / ".levain" / "memory.crystal.json").write_text(json.dumps(
        {"crystal": [{"name": "a_pattern", "status": "crystallized", "level": 2,
                      "explanation": "It means a thing. More.", "permanence": "graduated",
                      "activation_mode": "always", "tags": ["x"]}]}))
    (root / ".levain" / "edits.jsonl").write_text(
        "\n".join(json.dumps({"id": f"e{i}", "ts": f"2026-10-0{i}T00:00:00+00:00", "kind": "state",
                              "action": "edit", "source": "s"}) for i in range(1, 4)) + "\n")
    (root / ".levain" / "context.json").write_text(json.dumps({
        "focus": "ship K1", "focus_set_at": datetime.now(timezone.utc).isoformat(), "focus_source": "cli",
        "state": "ok", "state_set_at": datetime.now(timezone.utc).isoformat(), "state_source": "cli"}))
    return root, SubstrateSource.local(root)


@pytest.fixture
def env(tmp_path: Path):
    root, src = _install(tmp_path)
    jobs = JobStore(root / ".levain" / "jobs.json")
    jobs.create("j1", "consult", datetime.now(timezone.utc).isoformat())
    return root, src, build_default_cockpit(src, job_store=jobs)


def _status(ck: Cockpit, pid: str) -> dict[str, Any]:
    return ck.panel(pid)  # type: ignore[return-value]


def _simple(pid: str = "p", *, rows=(), priority="feed", **kw: Any) -> ProviderSpec:
    def read(ctx):
        return Read(rows=tuple(rows))
    base: dict[str, Any] = dict(
        id=pid, kind="triage-list", title=pid, priority=priority, read=read, order="time.desc",
        facets=frozenset({"at"}), version_fields=("id",))
    base.update(kw)
    return ProviderSpec(**base)


# --- every built-in provider: removed / unreadable / bad line -----------------------------------

def _rm(path: Path):
    return lambda: path.unlink()


def _garble(path: Path):
    return lambda: path.write_bytes(b"\xff\xfe not json, not sqlite, not utf8 \x00\x01")


def _dirify(path: Path):
    def f() -> None:
        path.unlink()
        path.mkdir()
    return f


def _cases(root: Path):
    lv = root / ".levain"
    spores, db, crystal = lv / "memory.spores.json", lv / "memory.db", lv / "memory.crystal.json"
    edits, ctxj, jobs = lv / "edits.jsonl", lv / "context.json", lv / "jobs.json"
    cont = lv / "memory.continuity.md"
    out = []
    for pid in ("tray", "loops", "keep"):
        out.append((pid, "spores", _rm(spores), _garble(spores)))
    for pid in ("episodes", "health", "graph", "wraps"):
        out.append((pid, "db", _rm(db), _garble(db)))
    out.append(("crystals", "crystal", _rm(crystal), _garble(crystal)))
    out.append(("edits", "edits", _rm(edits), _dirify(edits)))
    out.append(("focus", "ctx", _rm(ctxj), _garble(ctxj)))
    out.append(("state", "ctx", _rm(ctxj), _garble(ctxj)))
    out.append(("jobs", "jobs", _rm(jobs), _garble(jobs)))
    out.append(("section:state", "cont", _rm(cont), _garble(cont)))
    out.append(("config:origin", "seed", lambda: shutil.rmtree(root / "seed"), lambda: None))
    return out


CASE_IDS = [c[0] for c in _cases(Path("/x"))]


class TestEveryBuiltInProviderFailsLoudly:
    """DONE: "for EACH built-in provider: source removed → error; whole source unreadable → error;
    a JSONL source with one bad line → partial with skipped: 1"."""

    @pytest.mark.parametrize("pid", CASE_IDS)
    def test_source_removed_is_error(self, tmp_path: Path, pid: str) -> None:
        root, src = _install(tmp_path)
        ck = build_default_cockpit(src, job_store=JobStore(root / ".levain" / "jobs.json"))
        JobStore(root / ".levain" / "jobs.json").create("j1", "consult", datetime.now(timezone.utc).isoformat())
        assert _status(ck, pid)["status"] in ("ok", "empty"), "baseline must read clean"
        dict((c[0], c) for c in _cases(root))[pid][2]()
        got = _status(ck, pid)
        assert got["status"] == "error", got["error"]
        assert got["error"]

    @pytest.mark.parametrize("pid", [p for p in CASE_IDS if p != "config:origin"])
    def test_whole_source_unreadable_is_error(self, tmp_path: Path, pid: str) -> None:
        root, src = _install(tmp_path)
        ck = build_default_cockpit(src, job_store=JobStore(root / ".levain" / "jobs.json"))
        JobStore(root / ".levain" / "jobs.json").create("j1", "consult", datetime.now(timezone.utc).isoformat())
        assert _status(ck, pid)["status"] in ("ok", "empty")
        dict((c[0], c) for c in _cases(root))[pid][3]()
        got = _status(ck, pid)
        assert got["status"] == "error", got
        assert got["rows"] is None and got["value"] is None  # an error never carries stale rows

    def test_a_jsonl_source_with_one_bad_line_is_partial_with_skipped_1(self, env) -> None:
        root, _src, ck = env
        with (root / ".levain" / "edits.jsonl").open("a") as f:
            f.write("{this is not json\n")
        got = _status(ck, "edits")
        assert got["status"] == "partial"
        assert got["skipped"] == [{"count": 1, "reason": "unreadable ledger line"}]
        assert got["count"] == 3  # the readable rows still render

    def test_a_readable_record_with_an_empty_id_is_a_row_not_a_skipped_line(self, env) -> None:
        root, _src, ck = env
        with (root / ".levain" / "edits.jsonl").open("a") as f:
            f.write(json.dumps({"id": "", "ts": "2026-10-09", "kind": "action", "action": "consult"}) + "\n")
        got = _status(ck, "edits")
        assert got["status"] == "ok" and got["count"] == 4 and got["skipped"] == []

    def test_an_optional_source_never_seen_is_not_configured_not_nothing_waiting(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        (root / ".levain" / "edits.jsonl").unlink()
        got = _status(build_default_cockpit(src), "edits")
        assert got["note"].startswith("not configured:") and got["error"] is None

    def test_a_source_present_at_a_previous_read_and_now_absent_is_error_never_not_configured(self, env) -> None:
        root, _src, ck = env
        assert _status(ck, "edits")["status"] == "ok"
        (root / ".levain" / "edits.jsonl").unlink()
        got = _status(ck, "edits")
        assert got["status"] == "error" and "disappeared" in got["error"]


# --- registration refusals -----------------------------------------------------------------------


class TestWhyProvidersCheckTheirOwnSources:
    """providers.py's docstring says SubstrateView hides a missing spore file, crystal file and
    context file as empty rather than as an error. This makes that claim fail when it stops holding."""

    def test_the_view_hides_missing_sources(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        for name in ("memory.spores.json", "memory.crystal.json", "context.json"):
            (root / ".levain" / name).unlink()
        view = src.build()
        assert view.tray == [] and view.crystal_index == [] and (view.focus is None or view.focus.text is None)
        assert not ({"open_spores", "crystal_index"} & set(view.errors))

    def test_recent_edits_swallows_a_directory_in_place_of_the_ledger(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        (root / ".levain" / "edits.jsonl").unlink()
        (root / ".levain" / "edits.jsonl").mkdir()
        assert src.build().recent_edits == []


class TestRegistrationRefusals:
    def test_a_gate_panel_registering_an_optional_source_is_refused(self) -> None:
        with pytest.raises(CockpitRegistrationError, match="optional"):
            Cockpit().register(_simple(priority="gate", optional=True))

    def test_stale_after_longer_than_twice_the_refresh_interval_is_refused(self) -> None:
        with pytest.raises(CockpitRegistrationError, match="2 x refresh_every_s"):
            Cockpit().register(_simple(refresh_every_s=10, stale_after_s=21))
        Cockpit().register(_simple(refresh_every_s=10, stale_after_s=20))  # the boundary is allowed

    def test_an_unregistered_facet_is_refused_at_registration(self) -> None:
        with pytest.raises(CockpitRegistrationError, match="unregistered facet"):
            Cockpit().register(_simple(facets=frozenset({"made_up"})))

    def test_an_unregistered_facet_on_a_row_makes_the_panel_an_error(self) -> None:
        ck = Cockpit()
        ck.register(_simple(rows=[RowIn("r1", "t", {"made_up": 1}, stored={"id": "r1"})]))
        got = ck.panel("p")
        assert got["status"] == "error" and "made_up" in got["error"]

    def test_a_row_facet_the_panel_did_not_declare_is_refused(self) -> None:
        ck = Cockpit()
        ck.register(_simple(rows=[RowIn("r1", "t", {"domain": "x"}, stored={"id": "r1"})]))
        assert ck.panel("p")["status"] == "error"

    def test_a_stored_field_neither_versioned_nor_excluded_is_refused(self) -> None:
        ck = Cockpit()
        ck.register(_simple(rows=[RowIn("r1", "t", {"at": "2026"}, stored={"id": "r1", "sneaky": 1})]))
        got = ck.panel("p")
        assert got["status"] == "error" and "sneaky" in got["error"]

    def test_a_downstream_ordering_under_a_kernel_prefix_is_refused(self) -> None:
        from levain.cockpit import Ordering, register_ordering
        with pytest.raises(CockpitRegistrationError, match="kernel-owned"):
            register_ordering(Ordering("spore.mine", lambda r, t: (0,)))

    def test_one_panel_per_row_set(self) -> None:
        ck = Cockpit()
        ck.register(_simple("a", rowset="spores"))
        with pytest.raises(CockpitRegistrationError, match="one panel per row set"):
            ck.register(_simple("b", rowset="spores"))

    def test_empty_comes_only_from_a_read(self) -> None:
        ck = Cockpit()
        ck.register(_simple("ok"))
        ck.register(ProviderSpec(**{**_simple("gone").__dict__, "read": lambda c: Absent("no source")}))
        assert ck.panel("ok")["status"] == "empty"
        assert ck.panel("gone")["status"] == "error"


# --- refreshers and liveness ---------------------------------------------------------------------


class TestRefreshersAndTimeouts:
    def test_a_refresher_thread_killed_is_error_within_twice_the_interval(self) -> None:
        ck = Cockpit()
        ck.register(_simple(refresh_every_s=0.2, stale_after_s=0.4, rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ck.start()
        try:
            assert ck.panel("p")["status"] == "ok"
            ck.refresher("p").halt()
            deadline = time.monotonic() + 2 * 0.2 + 0.3
            while time.monotonic() < deadline and ck.panel("p")["status"] != "error":
                time.sleep(0.02)
            got = ck.panel("p")
            assert got["status"] == "error" and "refresher not reporting" in got["error"]
            assert got["as_of"] is not None  # the last good read is still named
        finally:
            ck.stop()

    def test_a_hung_source_call_is_error_at_its_timeout(self) -> None:
        release = threading.Event()
        ck = Cockpit()
        ck.register(ProviderSpec(**{**_simple("h").__dict__, "read": lambda c: (release.wait(30), Read(rows=()))[1],
                                    "timeout_s": 0.2}))
        t0 = time.monotonic()
        got = ck.panel("h")
        assert got["status"] == "error" and "timed out" in got["error"]
        assert time.monotonic() - t0 < 1.5
        again = ck.panel("h")  # waits on the SAME read: no second thread is stacked on a hung call
        assert again["status"] == "error" and "timed out" in again["error"]
        assert sum(t.name == "cockpit-read" and t.is_alive() for t in threading.enumerate()) == 1
        release.set()
        time.sleep(0.1)
        assert ck.panel("h")["status"] == "empty"   # and it recovers when the source does

    def test_a_failed_refresh_is_error_at_once_carrying_the_last_good_read(self) -> None:
        state = {"fail": False}

        def read(ctx):
            if state["fail"]:
                raise OSError("disk gone")
            return Read(rows=(RowIn("r", "t", {"at": "1"}, stored={"id": "r"}),))
        ck = Cockpit()
        ck.register(ProviderSpec(**{**_simple().__dict__, "read": read, "refresh_every_s": 60, "stale_after_s": 60}))
        ck.refresh("p")
        good = ck.panel("p")["as_of"]
        state["fail"] = True
        ck.refresh("p")
        got = ck.panel("p")
        assert got["status"] == "error" and "disk gone" in got["error"] and got["as_of"] == good
        state["fail"] = False
        ck.refresh("p")
        assert ck.panel("p")["status"] == "ok"  # errors are not debounced either way

    def test_the_provider_exception_is_a_fault_not_a_crash(self) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec(**{**_simple("x").__dict__, "read": lambda c: 1 / 0}))
        assert ck.panel("x")["status"] == "error"
        assert ck.manifest(NONE_CRED)["panels"]["x"]["status"] == "error"


# --- ETag, 304, credential classes -----------------------------------------------------------------


@contextmanager
def _serve(src: SubstrateSource, **kw: Any):
    httpd = make_server(src, host="127.0.0.1", port=0, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _http(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class TestHttpRoutes:
    def test_a_client_gets_304_while_a_refresher_runs_over_unchanged_rows(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        ck.register(_simple("slow", refresh_every_s=0.1, stale_after_s=0.2, rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ck.start()
        try:
            with _serve(src, cockpit=ck) as (base, _h):
                st, hdr, body = _http(f"{base}/cockpit/panel/slow.json")
                assert st == 200
                a0 = json.loads(body)["as_of"]
                time.sleep(0.45)  # several refreshes later
                st2, hdr2, _ = _http(f"{base}/cockpit/panel/slow.json", {"If-None-Match": hdr["ETag"]})
                assert st2 == 304
                assert json.loads(_http(f"{base}/cockpit/panel/slow.json")[2])["as_of"] != a0  # the clock moved, the etag did not
        finally:
            ck.stop()

    def test_two_credential_classes_get_two_manifest_etags_and_never_share_a_cache_entry(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serve(src, write_token="s3cret") as (base, _h):
            st1, h1, b1 = _http(f"{base}/cockpit/manifest.json")
            st2, h2, b2 = _http(f"{base}/cockpit/manifest.json", {"X-Levain-Write-Token": "s3cret"})
            assert (st1, st2) == (200, 200)
            assert json.loads(b1)["credential"]["class"] == "none"
            assert json.loads(b2)["credential"]["class"] == "token"
            assert json.loads(b1)["etag"] != json.loads(b2)["etag"]
            assert h1["ETag"] != h2["ETag"]
            # the token caller's validator never revalidates the no-credential caller's copy
            assert _http(f"{base}/cockpit/manifest.json", {"If-None-Match": h2["ETag"]})[0] == 200
            # what a shared proxy must obey: private, and keyed on the credential header
            for h in (h1, h2):
                assert "private" in h["Cache-Control"] and "no-store" not in h["Cache-Control"]
                assert "X-Levain-Write-Token" in h["Vary"]

    def test_the_cockpit_rides_the_existing_read_gate(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        from levain.writes import WriteScope
        gated = SubstrateSource(anneal=src.anneal, write_scope=WriteScope(
            anneal=src.anneal, ledger_root=root / ".levain", install_root=None), context_json=src.context_json)
        with _serve(gated, write_token="tok") as (base, httpd):
            httpd.is_loopback_bind = False  # what a real off-box bind sets (see TestOffBoxWriteToken)
            assert _http(f"{base}/cockpit/manifest.json")[0] == 403
            assert _http(f"{base}/cockpit/panel/tray.json")[0] == 403
            assert _http(f"{base}/cockpit/manifest.json", {"X-Levain-Write-Token": "tok"})[0] == 200

    def test_unknown_panel_and_bad_profile_and_missing_row(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serve(src) as (base, _h):
            assert _http(f"{base}/cockpit/panel/nope.json")[0] == 404
            assert _http(f"{base}/cockpit/panel/tray.json?profile=huge")[0] == 400
            assert _http(f"{base}/cockpit/panel/tray.json?row=spore:nope")[0] == 404

    def test_a_cockpit_route_cannot_be_claimed_by_a_downstream(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with pytest.raises(ValueError, match="collides"):
            make_server(src, host="127.0.0.1", port=0, extra_json={"/cockpit/manifest.json": lambda: b"{}"})


# --- the `now` view --------------------------------------------------------------------------------


def _decision_rows(n: int, due: str = "2020-01-01") -> list[RowIn]:
    return [RowIn(f"np:{i}", f"decision {i}", {"due": due, "overdue_days": 5}, stored={"id": f"np:{i}"}) for i in range(n)]


class TestNowView:
    def _ck(self, env, decisions: Any) -> Cockpit:
        _root, _src, ck = env
        ck.register(ProviderSpec(
            id="decisions", kind="triage-list", title="Decisions", priority="gate", read=decisions,
            region="operate", rank=-1, order="decision.dated", facets=frozenset({"due", "overdue_days"}),
            version_fields=("id",)))
        return ck

    def test_now_rows_are_the_concatenation_of_the_gate_panels_band_now_rows_in_source_order(self, env) -> None:
        ck = self._ck(env, lambda c: Read(rows=tuple(_decision_rows(2))))
        now = ck.panel(NOW_ID)
        expect = []
        for pid in ("decisions", "tray"):  # decisions rank -1 comes before tray rank 0
            expect += [(pid, r["id"]) for r in ck.panel(pid)["rows"] if r["band"] == "now"]
        assert [(r["panel_id"], r["id"]) for r in now["rows"]] == expect
        assert expect and {p for p, _ in expect} == {"decisions", "tray"}
        assert now["count"] == len(expect)

    def test_with_decisions_in_error_the_view_reads_error_and_names_it(self, env) -> None:
        def boom(ctx):
            raise OSError("needs_phill log gone")
        ck = self._ck(env, boom)
        now = ck.panel(NOW_ID)
        assert now["status"] == "error" and "decisions" in now["error"] and now["degraded"] == ["decisions"]
        assert [r["panel_id"] for r in now["rows"]] == ["tray", "tray"]  # the sources still ok still render
        assert "decisions" in now["note"]

    def test_the_manifest_lists_now_first_in_operate(self, env) -> None:
        _root, _src, ck = env
        m = ck.manifest(NONE_CRED)
        assert m["regions"]["zones"][0]["panels"][0] == NOW_ID and NOW_ID in m["panels"]


# --- row versions ----------------------------------------------------------------------------------


class TestRowVersions:
    """DONE: "for each row type, mutating each declared version_field changes Row.version and
    mutating each excluded field (last_seen, every clock facet) does not"."""

    def _specs(self, env) -> dict[str, ProviderSpec]:
        _r, _s, ck = env
        return {pid: ck._specs[pid] for pid in ck.panel_ids if ck._specs[pid].kind == "triage-list"}

    def test_each_declared_field_changes_the_version_and_each_excluded_one_does_not(self, env) -> None:
        _r, _s, ck = env
        sample = {
            "id": "i", "text": "t", "disposition": "seed", "tier": "hot", "type": "task", "domain": "d",
            "next": "2026-10-01", "pointer": "p", "salience": 1, "seen": "2026-10-01", "timestamp": "t0",
            "source": "s", "content": "c", "tags": ["a"], "name": "n", "level": 1, "one_clause": "o",
            "permanence": "x", "activation_mode": "m", "last_activated_on": "2026-01-01", "job_id": "j",
            "verb": "v", "status": "pending", "created_at": "c", "started_at": None, "finished_at": None,
            "result": None, "error": None, "kind": "k", "action": "a", "ts": "t",
        }
        checked = 0
        for pid, spec in self._specs(env).items():
            fields = [f for f in spec.version_fields if f != "*"]
            if "*" in spec.version_fields:
                fields = list(sample)  # every stored field is versioned, bar the named exclusions
            stored = {f: sample[f] for f in set(fields) | set(spec.version_excluded)}
            base = ck.row_version(spec, stored)
            for f in fields:
                if f in spec.version_excluded:
                    continue
                assert ck.row_version(spec, {**stored, f: ["changed"]}) != base, (pid, f)
                checked += 1
            for f in spec.version_excluded:
                assert ck.row_version(spec, {**stored, f: ["changed"]}) == base, (pid, f)
                checked += 1
        assert checked > 30

    def test_a_touch_and_the_clock_do_not_change_a_spore_version_and_an_edit_does(self, env) -> None:
        root, _s, ck = env
        v0 = {r["id"]: r["version"] for r in ck.panel("tray")["rows"]}
        path = root / ".levain" / "memory.spores.json"
        data = json.loads(path.read_text())
        for s in data["spores"]:
            s["seen"] = "2031-01-01"          # a spore_touch / session-open
        path.write_text(json.dumps(data))
        assert {r["id"]: r["version"] for r in ck.panel("tray")["rows"]} == v0
        for s in data["spores"]:
            if s["text"] == "seed overdue":
                s["text"] = "seed overdue, edited"
        path.write_text(json.dumps(data))
        v2 = {r["id"]: r["version"] for r in ck.panel("tray")["rows"]}
        changed = [k for k in v0 if v2.get(k) != v0[k]]
        assert changed == ["spore:spore-002"]

    def test_the_version_is_the_same_in_every_profile(self, env) -> None:
        _r, _s, ck = env
        full = {r["id"]: r["version"] for r in ck.panel("tray")["rows"]}
        comp = {r["id"]: r["version"] for r in ck.panel("tray", profile="compact")["rows"]}
        assert full == comp

    def test_read_one_reads_the_source_not_the_snapshot(self, env) -> None:
        root, _s, ck = env
        ck.start()
        row_id = ck.panel("tray")["rows"][0]["id"]
        before = ck.read_one("tray", row_id)
        assert isinstance(before, Read)
        path = root / ".levain" / "memory.spores.json"
        data = json.loads(path.read_text())
        for s in data["spores"]:
            if f"spore:{s['id']}" == row_id:
                s["text"] = "changed under the snapshot"
        path.write_text(json.dumps(data))
        after = ck.read_one("tray", row_id)
        assert isinstance(after, Read) and after.value["version"] != before.value["version"]
        assert isinstance(ck.read_one("tray", "spore:ghost"), Absent)


# --- profiles and search ---------------------------------------------------------------------------


class TestProfilesAndSearch:
    def test_no_compact_profile_panel_carries_a_row_body(self, env) -> None:
        _r, _s, ck = env
        for pid in ck.panel_ids + [NOW_ID]:
            panel = ck.panel(pid, profile="compact")
            for row in panel["rows"] or []:
                assert row["body"] is None, pid
        assert any(r["body"] for r in ck.panel("tray")["rows"]), "full profile does carry bodies"

    def test_compact_gate_rows_feed_pointer_and_gauge_value(self, env) -> None:
        _r, _s, ck = env
        assert ck.panel("tray", profile="compact")["rows"] is not None          # gate: every row
        keep = ck.panel("keep", profile="compact")                              # feed: count + pointer
        assert keep["rows"] is None and keep["count"] == 1 and "profile=full" in keep["next"]
        assert ck.panel("health", profile="compact")["value"]["metrics"]        # gauge: the metric
        assert "data" not in ck.panel("graph", profile="compact")["value"]      # visual: its text projection

    def test_a_keep_q_search_matches_body_text_the_compact_rows_do_not_carry(self, env) -> None:
        _r, _s, ck = env
        hit = ck.panel("keep", profile="compact", q="needleinbody")
        assert hit["matched"] == 1 and hit["rows"][0]["body"] is None
        assert "NEEDLEINBODY" not in json.dumps(ck.panel("keep", profile="compact"))  # not in the compact rows
        assert ck.panel("keep", profile="compact", q="zzz-nothing")["rows"] == []

    def test_row_returns_one_row_in_full(self, env) -> None:
        _r, _s, ck = env
        rid = ck.panel("keep")["rows"][0]["id"]
        got = ck.panel("keep", profile="compact", row=rid)
        assert [r["id"] for r in got["rows"]] == [rid] and "NEEDLEINBODY" in got["rows"][0]["body"]

    def test_the_title_is_cut_by_the_kernel_and_flagged(self, env) -> None:
        _r, _s, ck = env
        row = ck.panel("keep")["rows"][0]
        assert len(row["title"]) <= 160 and row["truncated"] is True


# --- ordering, groups, bands -----------------------------------------------------------------------


class TestOrderingAndGroups:
    def test_spore_tray_groups_and_order(self, env) -> None:
        _r, _s, ck = env
        p = ck.panel("tray")
        by_group = {r["id"]: r["group"] for r in p["rows"]}
        assert by_group == {"spore:spore-003": "today", "spore:spore-002": "overdue"}
        assert [g["id"] for g in p["groups"]] == ["today", "overdue", "also"]
        assert [r["group"] for r in p["rows"]] == ["today", "overdue"]  # group order leads
        assert all(r["band"] == "now" for r in p["rows"])

    def test_overdue_is_ordered_most_overdue_first_and_providers_do_not_sort(self) -> None:
        ck = Cockpit()
        rows = [RowIn(f"s:{i}", f"r{i}", {"due": d, "disposition": "seed", "tier": "hot", "salience": 0,
                                           "handoff_expired": False},
                      stored={"id": f"s:{i}"}) for i, d in enumerate(["2026-09-01", "2026-01-01", "2026-05-01"])]
        ck.register(ProviderSpec(
            id="t", kind="triage-list", title="t", priority="gate", read=lambda c: Read(rows=tuple(rows)),
            order="spore.tray", facets=frozenset({"due", "disposition", "tier", "salience", "handoff_expired"}),
            version_fields=("id",)))
        assert [r["id"] for r in ck.panel("t")["rows"]] == ["s:1", "s:2", "s:0"]

    def test_an_undated_stale_handoff_is_also_not_today(self) -> None:
        ck = Cockpit()
        rows = [RowIn("h", "h", {"disposition": "handoff", "handoff_expired": True, "tier": "warm", "salience": 0},
                      stored={"id": "h"})]
        ck.register(ProviderSpec(
            id="t", kind="triage-list", title="t", priority="gate", read=lambda c: Read(rows=tuple(rows)),
            order="spore.tray", facets=frozenset({"disposition", "handoff_expired", "tier", "salience"}),
            version_fields=("id",)))
        row = ck.panel("t")["rows"][0]
        assert row["group"] == "also" and row["band"] == "later"

    def test_a_future_surface_date_is_filtered_not_hidden(self, env) -> None:
        root, _s, ck = env
        SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="later", disposition="seed", next="2999-01-01")
        p = ck.panel("tray")
        assert p["status"] == "ok" and p["filtered"] == [{"count": 1, "reason": "surface date not reached"}]
        assert p["count"] == 2


# --- the JobStore list -----------------------------------------------------------------------------


class TestJobStoreList:
    def test_list_recent_is_newest_first_and_applies_the_lease_backstop(self, tmp_path: Path) -> None:
        js = JobStore(tmp_path / "jobs.json", lease_s=60)
        t0 = datetime(2026, 10, 9, tzinfo=timezone.utc)
        js.create("old", "consult", t0.isoformat())
        js.create("new", "consult", (t0 + timedelta(seconds=10)).isoformat())
        out = js.list_recent((t0 + timedelta(seconds=30)).isoformat())
        assert [r["job_id"] for r in out] == ["new", "old"] and out[0]["status"] == "pending"
        late = js.list_recent((t0 + timedelta(seconds=500)).isoformat())
        assert {r["status"] for r in late} == {"failed"}
        assert JobStore(tmp_path / "none.json").list_recent(t0.isoformat()) == []

    def test_a_corrupt_store_raises_never_a_short_list(self, tmp_path: Path) -> None:
        from levain.jobs import JobStoreCorruptError
        (tmp_path / "jobs.json").write_text("{nope")
        with pytest.raises(JobStoreCorruptError):
            JobStore(tmp_path / "jobs.json").list_recent(datetime.now(timezone.utc).isoformat())


class TestRev9EtagAndProse:
    """Design rev 9 §3.4 / §3.2.2: the panel etag covers the credential class and the date; Prose
    carries a ``provenance`` (null until K2b)."""

    def test_the_panel_etag_covers_credential_class_and_the_date(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        day = {"t": datetime(2026, 10, 9, 12, tzinfo=timezone.utc)}
        ck = build_default_cockpit(src, clock=lambda: day["t"])
        a = ck.panel("tray")["etag"]
        assert ck.panel("tray")["etag"] == a
        assert ck.panel("tray", credential_class="token")["etag"] != a
        day["t"] += timedelta(days=2)
        assert ck.panel("tray")["etag"] != a

    def test_prose_values_carry_a_null_provenance(self, env) -> None:
        _r, _s, ck = env
        assert ck.panel("section:state")["value"]["provenance"] is None


# --- L3 round 1 fixes: each test is built from a finding that was reproduced or read on disk -----


class TestL3R1Fixes:
    def test_concurrent_requests_do_not_turn_a_healthy_panel_into_an_error(self) -> None:
        gate = threading.Event()

        def slow(ctx):
            gate.wait(2)
            return Read(rows=())
        ck = Cockpit()
        ck.register(ProviderSpec(**{**_simple("s").__dict__, "read": slow, "timeout_s": 5}))
        out: list[str] = []
        ts = [threading.Thread(target=lambda: out.append(ck.panel("s")["status"])) for _ in range(4)]
        for t in ts:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in ts:
            t.join()
        assert out == ["empty"] * 4

    def test_a_hung_shared_view_costs_one_timeout_not_one_per_panel(self, tmp_path: Path, monkeypatch) -> None:
        import levain.cockpit.providers as prov
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        ck.manifest(NONE_CRED)                    # discover the prose panels while the source is healthy
        release = threading.Event()
        real = src.build
        monkeypatch.setattr(prov, "_view", lambda source, ctx: ctx.memo(
            "view", lambda: (release.wait(30), real())[1]))
        ck._entity_timeout_s = 0.3
        for spec in ck._specs.values():
            spec.timeout_s = 0.3
        t0 = time.monotonic()
        m = ck.manifest(NONE_CRED)
        took = time.monotonic() - t0
        release.set()
        assert took < 3.0, took                    # ~25 panels x 0.3 s, if each paid its own
        assert m["panels"]["tray"]["status"] == "error"
        assert any(e["source"] == "entity" for e in m["errors"])

    def test_a_cached_refresher_snapshot_ages_into_stale(self) -> None:
        clock = {"t": datetime(2026, 10, 9, 12, tzinfo=timezone.utc)}
        ck = Cockpit(clock=lambda: clock["t"])
        ck.register(_simple(refresh_every_s=10, stale_after_s=1, rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ck.refresh("p")
        assert ck.panel("p")["status"] == "ok"
        clock["t"] += timedelta(seconds=5)
        assert ck.panel("p")["status"] == "stale"

    def test_the_manifest_etag_covers_entity_and_manifest_errors(self) -> None:
        name = {"n": "A"}
        ck = Cockpit(entity=lambda ctx: {"name": name["n"]})
        ck.register(_simple())
        e1 = ck.manifest(NONE_CRED)["etag"]
        name["n"] = "B"
        assert ck.manifest(NONE_CRED)["etag"] != e1

    def test_prose_panels_are_discovered_late_and_a_discovery_fault_is_a_manifest_error(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        cont = root / ".levain" / "memory.continuity.md"
        text = cont.read_text()
        cont.write_bytes(b"\xff\xfe\x00 not utf8")
        ck = build_default_cockpit(src)
        m = ck.manifest(NONE_CRED)
        assert any(e["source"].startswith("discovery") for e in m["errors"]), m["errors"]
        assert not any(p.startswith("section:") for p in m["panels"])
        cont.write_text(text)                       # the file heals: the panels appear, no restart
        m2 = ck.manifest(NONE_CRED)
        assert "section:state" in m2["panels"] and m2["errors"] == []

    def test_read_one_runs_the_list_pipeline_so_group_and_band_agree(self, env) -> None:
        _r, _s, ck = env
        listed = {r["id"]: r for r in ck.panel("tray")["rows"]}
        for rid, row in listed.items():
            one = ck.read_one("tray", rid)
            assert isinstance(one, Read)
            assert (one.value["group"], one.value["band"], one.value["version"]) == (row["group"], row["band"], row["version"])

    def test_a_spore_rescheduled_into_the_future_is_still_found_by_read_one(self, env) -> None:
        root, _s, ck = env
        path = root / ".levain" / "memory.spores.json"
        data = json.loads(path.read_text())
        for s in data["spores"]:
            if s["text"] == "seed overdue":
                s["next"] = "2999-01-01"
        path.write_text(json.dumps(data))
        assert "spore:spore-002" not in [r["id"] for r in ck.panel("tray")["rows"]]   # held out of the list
        assert isinstance(ck.read_one("tray", "spore:spore-002"), Read)                # but a write finds it

    def test_malformed_provider_output_is_a_fault_not_a_crash(self) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec("m", "metric", "m", "gauge", lambda c: Read(value={"metrics": ["bad"]})))
        ck.register(_simple("r", rows=["not a RowIn"]))
        assert ck.panel("m")["status"] == "error" or ck.panel("m")["status"] in ("ok", "empty")
        assert ck.panel("r")["status"] == "error"
        assert ck.manifest(NONE_CRED)["panels"]["r"]["status"] == "error"

    def test_a_refresher_survives_a_cycle_that_raises_outside_the_provider(self) -> None:
        ck = Cockpit()
        calls = {"n": 0}

        def read(ctx):
            calls["n"] += 1
            return Read(rows=())
        ck.register(ProviderSpec(**{**_simple().__dict__, "read": read, "refresh_every_s": 0.05, "stale_after_s": 0.1}))
        ck.start()
        orig = ck._process
        flaky = {"n": 0}

        def process(*a, **k):
            flaky["n"] += 1
            if flaky["n"] == 2:
                raise AttributeError("boom")
            return orig(*a, **k)
        ck._process = process  # type: ignore[method-assign]
        try:
            time.sleep(0.5)
            assert ck.refresher("p").thread.is_alive() and calls["n"] >= 4
        finally:
            ck.stop()

    def test_a_slow_but_alive_refresh_is_not_reported_dead(self) -> None:
        clock = {"t": datetime(2026, 10, 9, 12, tzinfo=timezone.utc)}
        ck = Cockpit(clock=lambda: clock["t"])
        ck.register(_simple(refresh_every_s=1, stale_after_s=2, timeout_s=10, rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ck.refresh("p")
        clock["t"] += timedelta(seconds=2.5)
        st = ck._state["p"]
        st.refresh_started = clock["t"] - timedelta(seconds=1.5)    # a read 1.5 s into a 10 s timeout
        assert "not reporting" not in (ck.panel("p")["error"] or "")
        st.refresh_started = None
        assert "not reporting" in ck.panel("p")["error"]

    def test_stop_keeps_a_refresher_that_is_still_inside_a_read(self) -> None:
        release = threading.Event()
        ck = Cockpit()
        ck.register(ProviderSpec(**{**_simple().__dict__, "read": lambda c: (release.wait(10), Read(rows=()))[1],
                                    "refresh_every_s": 0.05, "stale_after_s": 0.1, "timeout_s": 0.1}))
        ck.refresh("p")  # times out at 0.1 s; the read thread lingers
        ck._refreshers["p"] = type("R", (), {"spec": ck._specs["p"], "halt": lambda self: None,
                                              "thread": threading.Thread(target=release.wait, args=(5,), daemon=True)})()
        ck._refreshers["p"].thread.start()
        assert ck.stop() == ["p"] and "p" in ck._refreshers
        release.set()

    def test_a_row_with_a_wrongly_typed_facet_is_skipped_and_counted_not_a_blanked_panel(self) -> None:
        ck = Cockpit()
        rows = [RowIn("a", "a", {"tier": "hot"}, stored={"id": "a"}), RowIn("b", "b", {"tier": "scorching"}, stored={"id": "b"})]
        ck.register(ProviderSpec(**{**_simple().__dict__, "read": lambda c: Read(rows=tuple(rows)),
                                    "facets": frozenset({"tier"}), "order": "spore.keep"}))
        got = ck.panel("p")
        assert got["status"] == "partial" and [r["id"] for r in got["rows"]] == ["a"] and got["skipped"][0]["count"] == 1

    def test_q_on_the_now_view_is_a_no_op_not_zero_rows(self, env) -> None:
        _r, _s, ck = env
        assert ck.panel(NOW_ID, q="anything")["rows"] == ck.panel(NOW_ID)["rows"]

    def test_a_context_file_that_is_valid_json_of_the_wrong_shape_is_error(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "context.json").write_text("[]")
        assert ck.panel("focus")["status"] == "error"

    def test_a_malformed_crystal_row_is_counted_not_hidden(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(json.dumps({"crystal": [
            {"name": "ok", "status": "crystallized", "level": 1, "explanation": "x"},
            {"name": "bad", "status": "crystallized", "level": "high", "explanation": "y"}]}))
        got = ck.panel("crystals")
        assert got["status"] == "partial" and got["skipped"][0]["count"] == 1

    def test_the_etag_variant_cannot_be_forged_through_a_delimiter(self) -> None:
        from levain.cockpit.routes import _etag_header
        a = _etag_header("e", ["panel", "full", "a|b", None, "none"])
        b = _etag_header("e", ["panel", "full", "a", "b|", "none"])
        assert a != b and a.startswith('W/"')

    def test_exactly_the_cap_is_not_reported_as_truncated(self, env, monkeypatch) -> None:
        import levain.cockpit.providers as prov
        monkeypatch.setattr(prov, "SPORE_CAP", 3)
        _r, _s, ck = env
        got = ck.panel("loops")   # one loop, cap 3
        assert got["status"] == "ok" and got["skipped"] == []
        monkeypatch.setattr(prov, "SPORE_CAP", 0)
        assert ck.panel("loops")["skipped"]
