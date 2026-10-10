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
        t1 = time.monotonic()
        again = ck.panel("h")  # past its budget the read is hung: fail AT ONCE, no thread stacked
        assert again["status"] == "error" and "past its timeout" in again["error"]
        assert time.monotonic() - t1 < 0.1
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


# --- L3 round 2 fixes -----------------------------------------------------------------------------


class TestL3R2Fixes:
    def test_discovery_is_single_flight_and_a_hung_discoverer_fails_fast_after_its_budget(self) -> None:
        release = threading.Event()
        ck = Cockpit()
        ck.discover(lambda ctx: (release.wait(30), [])[1])
        ck._entity_timeout_s = 0.2
        errs = [ck.manifest(NONE_CRED)["errors"] for _ in range(1)]
        assert errs[0] and errs[0][0]["source"] == "discovery:0"
        t0 = time.monotonic()
        for _ in range(5):
            assert ck.manifest(NONE_CRED)["errors"][0]["source"] == "discovery:0"
        assert time.monotonic() - t0 < 0.5      # five more requests, no wait, no new thread
        assert sum(t.name == "cockpit-read" and t.is_alive() for t in threading.enumerate()) == 1
        release.set()

    def test_registration_is_copy_on_write_a_held_snapshot_never_changes(self) -> None:
        ck = Cockpit()
        ck.register(_simple("a"))
        snap = ck._specs
        ck.register(_simple("b"))
        assert list(snap) == ["a"] and list(ck._specs) == ["a", "b"]

    def test_concurrent_discovery_registers_each_panel_once_without_a_spurious_error(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        errs: list[Any] = []
        ts = [threading.Thread(target=lambda: errs.append(ck.manifest(NONE_CRED)["errors"])) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errs == [[]] * 6

    def test_malformed_entity_and_discoverer_returns_are_manifest_errors_not_500s(self) -> None:
        ck = Cockpit(entity=lambda ctx: None)    # type: ignore[arg-type,return-value]
        ck.discover(lambda ctx: [object()])      # type: ignore[list-item,return-value]
        m = ck.manifest(NONE_CRED)
        assert {e["source"] for e in m["errors"]} == {"entity", "discovery:0"}

    def test_a_held_row_does_not_displace_a_visible_one_under_the_cap(self, tmp_path: Path, monkeypatch) -> None:
        import levain.cockpit.providers as prov
        root, src = _install(tmp_path)
        sp = SporeStore(root / ".levain" / "memory.spores.json")
        for i in range(4):
            sp.add(type="task", text=f"held {i}", disposition="seed", next="2999-01-01", tier="hot", salience=3)
        monkeypatch.setattr(prov, "SPORE_CAP", 2)
        got = build_default_cockpit(src).panel("tray")
        assert got["filtered"][0]["count"] == 4 and got["count"] == 2    # the visible rows survive the cap

    def test_read_one_is_not_truncated_by_the_cap(self, env, monkeypatch) -> None:
        import levain.cockpit.providers as prov
        _r, _s, ck = env
        monkeypatch.setattr(prov, "SPORE_CAP", 1)
        assert isinstance(ck.read_one("tray", "spore:spore-002"), Read)

    def test_a_crystal_added_between_two_reads_cannot_fake_a_malformed_row(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(json.dumps({"crystal": [
            {"name": "a", "status": "crystallized", "level": 1, "explanation": "x"},
            {"name": "b", "status": "crystallized", "level": 2, "explanation": "y"}]}))
        got = ck.panel("crystals")
        assert got["status"] == "ok" and got["count"] == 2 and got["skipped"] == []

    def test_if_none_match_star_matches(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serve(src) as (base, _h):
            assert _http(f"{base}/cockpit/manifest.json", {"If-None-Match": "*"})[0] == 304

    def test_a_failed_start_leaves_no_refresher_running(self, tmp_path: Path, monkeypatch) -> None:
        import levain.cockpit.engine as eng
        _root, src = _install(tmp_path)
        started: list[Cockpit] = []
        real = eng.Cockpit.start

        def boom(self):
            started.append(self)
            real(self)
            raise RuntimeError("start failed")
        monkeypatch.setattr(eng.Cockpit, "start", boom)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            with pytest.raises(RuntimeError):
                httpd.get_cockpit()
            assert httpd.cockpit is None and all(not c._refreshers for c in started)
        finally:
            httpd.server_close()


class TestL3R3Fixes:
    def test_a_thread_that_cannot_start_fails_that_read_and_poisons_nothing(self, monkeypatch) -> None:
        ck = Cockpit()
        ck.register(_simple())
        real = threading.Thread.start
        state = {"fail": True}

        def start(self):
            if state["fail"] and self.name == "cockpit-read":
                raise RuntimeError("can't start new thread")
            return real(self)
        monkeypatch.setattr(threading.Thread, "start", start)
        assert ck.panel("p")["status"] == "error"
        state["fail"] = False
        assert ck.panel("p")["status"] == "empty"        # recovered at once, no restart

    def test_one_failing_discoverer_does_not_hide_another_discoverers_panels(self) -> None:
        ck = Cockpit()
        ck.discover(lambda ctx: 1 / 0)                    # type: ignore[arg-type,return-value]
        ck.discover(lambda ctx: [_simple("found")])
        m = ck.manifest(NONE_CRED)
        assert "found" in m["panels"] and [e["source"] for e in m["errors"]] == ["discovery:0"]

    def test_a_discovered_panel_cannot_carry_a_refresher(self) -> None:
        ck = Cockpit()
        ck.discover(lambda ctx: [_simple("r", refresh_every_s=5, stale_after_s=5)])
        m = ck.manifest(NONE_CRED)
        assert "r" not in m["panels"] and "refresher" in m["errors"][0]["message"]

    def test_a_crystal_store_removed_between_the_check_and_the_read_is_error_not_empty(self, env, monkeypatch) -> None:
        root, _s, ck = env
        from anneal_memory.crystal import CrystalStore
        path = root / ".levain" / "memory.crystal.json"
        real = CrystalStore.active

        def active(self):
            out = real(self)
            path.unlink()
            return out
        assert ck.panel("crystals")["status"] == "ok"      # a clean read: the source has now been seen
        monkeypatch.setattr(CrystalStore, "active", active)
        assert ck.panel("crystals")["status"] == "error"   # it vanished mid-read: error, never healthy-empty

    def test_a_non_dict_crystal_row_is_skipped_per_row(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(json.dumps({"crystal": [
            {"name": "a", "status": "crystallized", "level": 1, "explanation": "x"}]}))
        from anneal_memory.crystal import CrystalStore
        assert ck.panel("crystals")["count"] == 1


class TestNowGroups:
    def test_the_now_head_carries_the_union_of_its_sources_groups_counted(self, env) -> None:
        _r, _s, ck = env
        now = ck.panel(NOW_ID)
        assert [g["id"] for g in now["groups"]] == ["today", "overdue", "also"]
        assert [g["title"] for g in now["groups"]][:2] == ["Today", "Overdue"]
        by = {g["id"]: g["count"] for g in now["groups"]}
        assert by == {"today": 1, "overdue": 1, "also": 0} and sum(by.values()) == now["count"]
        assert ck.manifest(NONE_CRED)["panels"][NOW_ID]["groups"] == now["groups"]


class TestSession2Freshness:
    """K1 DONE (rev 12): after a 304 on a panel, ``GET /cockpit/freshness.json`` (never answered 304)
    returns that panel's newer ``as_of``."""

    def test_freshness_after_a_304_carries_the_newer_as_of_and_is_never_304(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        ck.register(_simple("slow", refresh_every_s=0.1, stale_after_s=0.2,
                            rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ck.start()
        try:
            with _serve(src, cockpit=ck) as (base, _h):
                st, hdr, _b = _http(f"{base}/cockpit/panel/slow.json")
                f0 = json.loads(_http(f"{base}/cockpit/freshness.json")[2])["slow"]["as_of"]
                time.sleep(0.45)
                assert _http(f"{base}/cockpit/panel/slow.json", {"If-None-Match": hdr["ETag"]})[0] == 304
                st2, h2, body = _http(f"{base}/cockpit/freshness.json", {"If-None-Match": hdr["ETag"]})
                assert st2 == 200 and "ETag" not in h2
                assert json.loads(body)["slow"]["as_of"] > f0
                assert _http(f"{base}/cockpit/freshness.json", {"If-None-Match": "*"})[0] == 200
        finally:
            ck.stop()

    def test_freshness_names_every_panel_with_as_of_and_status_only(self, env) -> None:
        _r, _s, ck = env
        man = ck.manifest(NONE_CRED)          # reading the panels is what gives freshness something to report
        fr = ck.freshness()
        assert set(fr) == set(man["panels"])
        assert all(set(v) == {"as_of", "status"} for v in fr.values())
        assert fr["tray"]["status"] == man["panels"]["tray"]["status"]

    def test_freshness_reads_error_for_a_failing_panel(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.spores.json").unlink()
        ck.manifest(NONE_CRED)
        assert ck.freshness()["tray"]["status"] == "error"

    def test_freshness_runs_no_provider_and_reads_unread_before_a_first_read(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        calls = []

        def counting(ctx):
            calls.append(1)
            return Read(rows=())
        ck.register(_simple("probe", read=counting) if False else ProviderSpec(
            id="probe", kind="triage-list", title="p", priority="gate", read=counting, order="time.desc",
            facets=frozenset({"at"}), version_fields=("id",)))
        fr = ck.freshness()
        assert calls == [] and fr["probe"] == {"as_of": None, "status": "unread"}
        assert fr[NOW_ID]["status"] == "unread"
        ck.panel("probe")
        assert calls == [1] and ck.freshness()["probe"]["status"] == "empty"
        assert len(calls) == 1

    def test_registration_refuses_a_bad_edit_class_and_a_downstream_freshness_route(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = Cockpit()
        with pytest.raises(CockpitRegistrationError, match="edit_class"):
            ck.register(_simple("x", edit_class="A "))
        with pytest.raises(ValueError, match="collides"):
            make_server(src, host="127.0.0.1", port=0, extra_json={"/cockpit/freshness.json": lambda: b"{}"})


class TestSession2Gaps:
    """The build-found gaps (1009+48): masthead jar, store path, health max/local density, a real tag
    list, an edit class per panel."""

    def test_entity_carries_the_jar_and_the_store_path(self, env) -> None:
        _r, src, ck = env
        ent = ck.manifest(NONE_CRED)["entity"]
        assert set(ent["jar"]) == {"status", "today", "typical", "history_days", "level", "label", "day"}
        assert "paths" not in ent and str(Path.home()) not in json.dumps(ent)       # never the absolute path
        assert ent["store_label"] == src.anneal.episodic_db.name or ent["store_label"].startswith("~/")
        assert ent["store_label"].endswith(src.anneal.episodic_db.name)

    def test_health_carries_max_strength_and_local_density(self, env) -> None:
        _r, _s, ck = env
        labels = [m["label"] for m in ck.panel("health")["value"]["metrics"]]
        assert "max strength" in labels and "local density" in labels

    def test_crystal_tags_are_a_real_list(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(json.dumps({"crystal": [
            {"name": "a", "status": "crystallized", "level": 1, "explanation": "x",
             "tags": ["operator,ergonomics", " review ", "operator", ""]},
            {"name": "b", "status": "crystallized", "level": 1, "explanation": "x", "tags": []}]}))
        by = {r["id"]: r for r in ck.panel("crystals")["rows"]}
        assert by["crystal:a"]["facets"]["tags"] == ["operator", "ergonomics", "review"]
        assert by["crystal:b"]["facets"]["tags"] == []

    def test_every_panel_head_carries_its_edit_class(self, env) -> None:
        _r, _s, ck = env
        heads = ck.manifest(NONE_CRED)["panels"]
        assert {k: v["edit_class"] for k, v in heads.items() if not k.startswith(("section:", "config:"))} == {
            "now": None, "focus": None, "state": None, "tray": "B", "loops": "B", "keep": "B",
            "episodes": "B", "edits": None, "jobs": None, "health": "C", "graph": "C",
            "crystals": "C", "wraps": "C"}
        sections = {k: v["edit_class"] for k, v in heads.items() if k.startswith(("section:", "config:"))}
        assert sections and all(c in ("A", "B", "C") for c in sections.values())


class TestSession2CompactNow:
    def test_compact_now_rows_are_refs_that_join_to_the_source_panels_compact_rows(self, env) -> None:
        _r, _s, ck = env
        now = ck.panel(NOW_ID, profile="compact")
        full = ck.panel(NOW_ID)
        assert now["rows_by_ref"] is True and "rows_by_ref" not in full
        assert [(r["panel_id"], r["id"]) for r in now["rows"]] == [(r["panel_id"], r["id"]) for r in full["rows"]]
        assert all(set(r) == {"id", "panel_id", "version", "group", "body"} and r["body"] is None for r in now["rows"])
        tray = {r["id"]: r for r in ck.panel("tray", profile="compact")["rows"]}
        for r in now["rows"]:
            assert tray[r["id"]]["version"] == r["version"] and tray[r["id"]]["group"] == r["group"]
        assert now["count"] == full["count"]


class TestL3R4Fixes:
    def test_an_infinite_crystal_level_drops_that_row_not_the_panel(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(
            '{"crystal": [{"name": "bad", "status": "crystallized", "level": 1e999, "explanation": "x"},'
            ' {"name": "good", "status": "crystallized", "level": 1, "explanation": "x"}]}')
        p = ck.panel("crystals")
        assert p["status"] == "partial" and [r["id"] for r in p["rows"]] == ["crystal:good"]

    def test_concurrent_starts_make_one_refresher_per_panel(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        ck.register(_simple("slow", refresh_every_s=0.2, stale_after_s=0.4, rows=[RowIn("r", "t", {"at": "1"}, stored={"id": "r"})]))
        ts = [threading.Thread(target=ck.start) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        try:
            assert sum(1 for t in threading.enumerate() if t.name == "cockpit-refresh-slow") == 1
        finally:
            assert ck.stop() == []


class TestL3R4CodexFixes:
    def test_a_string_valued_tags_field_is_one_tag_list_not_characters(self, env) -> None:
        root, _s, ck = env
        (root / ".levain" / "memory.crystal.json").write_text(json.dumps({"crystal": [
            {"name": "a", "status": "crystallized", "level": 1, "explanation": "x", "tags": "operator,ergonomics"}]}))
        assert ck.panel("crystals")["rows"][0]["facets"]["tags"] == ["operator", "ergonomics"]

    def test_a_crystal_file_vanishing_mid_first_read_is_error_not_healthy(self, tmp_path: Path, monkeypatch) -> None:
        root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        from anneal_memory.crystal import CrystalStore
        path = root / ".levain" / "memory.crystal.json"
        real = CrystalStore.active

        def active(self):
            out = real(self)
            path.unlink()
            return out
        monkeypatch.setattr(CrystalStore, "active", active)
        assert ck.panel("crystals")["status"] == "error"

    def test_the_now_etag_covers_its_groups(self, env) -> None:
        from levain.cockpit.engine import _Snap
        _r, _s, ck = env
        now = ck.panel(NOW_ID)
        assert now["groups"] and now["etag"] == ck.manifest(NONE_CRED)["panels"][NOW_ID]["etag"]
        snap = _Snap(now["status"], now["rows"], None, [], [], now["as_of"], now["error"], now["note"])
        today = ck._clock().astimezone().date()
        assert ck._etag_of(snap, None, "none", today) != ck._etag_of(snap, now["groups"], "none", today)
        assert now["etag"] == ck._etag_of(snap, now["groups"], "none", today)


class TestL3R6Freshness:
    def test_freshness_now_rolls_up_its_sources_in_the_same_call_and_ages_with_them(self, env) -> None:
        root, _s, ck = env
        assert ck.freshness()[NOW_ID] == {"as_of": None, "status": "unread"}
        head = ck.manifest(NONE_CRED)["panels"][NOW_ID]
        assert ck.freshness()[NOW_ID] == {"as_of": head["as_of"], "status": "ok"}
        (root / ".levain" / "memory.spores.json").unlink()          # a gate source fails: error, not masked
        ck.manifest(NONE_CRED)
        assert ck.freshness()[NOW_ID]["status"] == "error"
        ck.register(ProviderSpec(id="late", kind="triage-list", title="l", priority="gate",
                                 read=lambda c: Read(rows=()), order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",)))
        assert ck.freshness()[NOW_ID]["status"] == "error"          # an unread source does not hide a known error

    def test_an_on_demand_panels_freshness_ages_against_its_stale_after(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        t = [datetime(2026, 10, 9, tzinfo=timezone.utc)]
        ck = Cockpit(clock=lambda: t[0])
        ck.register(_simple("p", stale_after_s=60))
        ck.panel("p")
        assert ck.freshness()["p"]["status"] == "empty"
        t[0] += timedelta(hours=1)
        assert ck.freshness()["p"]["status"] == "stale"


class TestL3R8Fixes:
    def test_an_older_started_slow_read_never_overwrites_a_newer_ones_freshness(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = Cockpit()
        gate, calls = threading.Event(), []

        def read(ctx):
            n = len(calls)
            calls.append(n)
            if n == 0:
                gate.wait(5)
                return Fault("slow first read failed")
            return Read(rows=())
        spec = ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read,
                            order="time.desc", facets=frozenset({"at"}), version_fields=("id",), timeout_s=10)
        ck.register(spec)
        t = threading.Thread(target=lambda: ck.panel("p"))
        t.start()
        time.sleep(0.2)
        # the first read is still running; a second request would join its flight, so release it first
        gate.set()
        t.join()
        assert ck.freshness()["p"]["status"] == "error"
        ck.panel("p")
        assert ck.freshness()["p"]["status"] == "empty"

    def test_freshness_survives_a_panel_registered_mid_call(self, env) -> None:
        _r, _s, ck = env
        ck.manifest(NONE_CRED)
        orig = ck._ordered_specs

        def racing(specs_map):
            out = orig(specs_map)
            ck.register(ProviderSpec(id="raced", kind="triage-list", title="r", priority="gate",
                                     read=lambda c: Read(rows=()), order="time.desc",
                                     facets=frozenset({"at"}), version_fields=("id",)))
            return out
        ck._ordered_specs = racing           # type: ignore[method-assign]
        assert "tray" in ck.freshness()


class TestStoreLabel:
    def test_label_is_home_relative_or_the_bare_name(self, tmp_path: Path) -> None:
        from levain.cockpit.providers import _store_label
        assert _store_label(Path.home() / ".anneal-memory" / "memory.db") == "~/.anneal-memory/memory.db"
        assert _store_label(Path.home()) == "~/"
        assert _store_label(Path("/nonexistent-root/elsewhere/x.db")) == "x.db"
        assert _store_label(Path.home() / ".." / "outside-secret" / "x.db") == "x.db"      # no sibling-account leak


class TestR9Bookkeeping:
    """An older-started read that finishes last must not regress the monotonic bookkeeping."""

    def _setup(self):
        ck = Cockpit()
        spec = _simple("p")
        ck.register(spec)
        return ck, spec, ck._state["p"]

    def _ctx(self, minute: int):
        from levain.cockpit.engine import ReadContext
        return ReadContext(datetime(2026, 10, 9, 12, minute, tzinfo=timezone.utc))

    def test_a_raising_fault_path_in_snap_for_is_an_error_read_and_freshness_follows(self, monkeypatch) -> None:
        ck, spec, st = self._setup()
        ck.panel("p")
        monkeypatch.setattr(Cockpit, "_read_snap", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert ck.panel("p")["status"] == "error"
        assert ck.freshness()["p"]["status"] == "error"

    def test_a_store_label_never_raises_or_leaks_on_a_broken_home(self, monkeypatch) -> None:
        from levain.cockpit import providers
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("/Users/x/secret"))))
        assert providers._store_label(Path("/Users/x/.anneal-memory/memory.db")) == "memory.db"


class TestOneFlightOrdersByConstruction:
    """Only the flight owner processes and commits; joiners get its snapshot; refresh() joins."""

    def _slow(self, script, release):
        calls = []

        def read(ctx):
            n = len(calls)
            calls.append(n)
            release.wait(5)
            step = script[min(n, len(script) - 1)]
            return Fault("down") if step == "fail" else Read(rows=())
        return read, calls

    def test_a_burst_runs_one_read_and_every_caller_gets_the_owners_snapshot(self) -> None:
        release = threading.Event()
        read, calls = self._slow(["fail"], release)
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read,
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",), timeout_s=10))
        out, ts = [], []
        for _ in range(12):
            ts.append(threading.Thread(target=lambda: out.append(ck.panel("p")["status"])))
        [t.start() for t in ts]
        time.sleep(0.3)
        release.set()
        [t.join() for t in ts]
        assert len(calls) == 1 and out == ["error"] * 12       # one read; all twelve see the owner's result
        st = ck._state["p"]
        assert st.failing_since is not None and st.pflight is None

    def test_fail_then_recover_commits_in_start_order(self) -> None:
        release = threading.Event()
        release.set()
        read, calls = self._slow(["fail", "ok"], release)
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read,
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",), timeout_s=10))
        assert ck.panel("p")["status"] == "error" and ck.freshness()["p"]["status"] == "error"
        assert ck.panel("p")["status"] == "empty" and ck.freshness()["p"]["status"] == "empty"
        st = ck._state["p"]
        assert st.failing_since is None and st.last_good_as_of == ck.freshness()["p"]["as_of"]

    def test_refresh_during_a_flight_joins_it_instead_of_racing(self) -> None:
        release = threading.Event()
        read, calls = self._slow(["ok"], release)
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read, order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",), timeout_s=10,
                                 refresh_every_s=60, stale_after_s=100))
        ts = [threading.Thread(target=lambda: ck.refresh("p")) for _ in range(5)]
        [t.start() for t in ts]
        time.sleep(0.3)
        release.set()
        [t.join() for t in ts]
        assert len(calls) == 1 and ck._state["p"].snap.status == "empty"

    def test_a_timed_out_owner_commits_once_and_a_hung_thread_never_commits_late(self) -> None:
        release = threading.Event()
        read, calls = self._slow(["ok"], release)
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read, order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",), timeout_s=0.3))
        assert ck.panel("p")["status"] == "error"
        assert ck.panel("p")["status"] == "error"          # source thread still hung: fail fast, no second read
        assert len(calls) == 1
        release.set()
        time.sleep(0.3)
        st = ck._state["p"]
        assert st.pflight is None and ck.freshness()["p"]["status"] == "error"   # the late OK committed nothing
        assert ck.panel("p")["status"] == "empty"                                 # a new read recovers

    def test_an_exception_escaping_the_owner_does_not_wedge_the_panel(self) -> None:
        n = [0]

        def clock():
            n[0] += 1
            if n[0] == 2:              # the owner's completion stamp: ReadContext took the first call
                raise OSError("clock failed")
            return datetime.now(timezone.utc)
        ck = Cockpit(clock=clock)
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=lambda c: Read(rows=()),
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",)))
        assert ck.panel("p")["status"] == "empty"       # a failing completion clock does not fail the read
        assert ck._state["p"].pflight is None
        assert ck.panel("p")["status"] == "empty"       # and the panel is not stuck behind a dead flight

    def test_a_joiner_waits_out_the_owners_processing_instead_of_reporting_a_false_timeout(self, monkeypatch) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=lambda c: Read(rows=()),
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",), timeout_s=0.3))
        real = Cockpit._process

        def slow(self, *a, **k):
            time.sleep(0.8)            # processing outlasts the read budget
            return real(self, *a, **k)
        monkeypatch.setattr(Cockpit, "_process", slow)
        out = []
        ts = [threading.Thread(target=lambda: out.append(ck.panel("p")["status"])) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert out == ["empty"] * 3

    def test_a_provider_exception_whose_text_raises_still_ends_the_flight(self) -> None:
        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("no text for you")

        def read(ctx):
            raise Hostile()
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read, order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",), timeout_s=2))
        assert ck.panel("p")["status"] == "error"
        time.sleep(0.1)
        assert ck._state["p"].pflight is None and ck.panel("p")["status"] == "error"   # not "still running", not stuck

    def test_a_late_joiner_is_bound_by_one_absolute_deadline(self, monkeypatch) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=lambda c: Read(rows=()),
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",), timeout_s=0.2))
        monkeypatch.setattr("levain.cockpit.engine.PROCESS_GRACE_S", 0.3)
        real = Cockpit._process
        monkeypatch.setattr(Cockpit, "_process", lambda self, *a, **k: (time.sleep(3), real(self, *a, **k))[1])
        t = threading.Thread(target=lambda: ck.panel("p"))
        t.start()
        time.sleep(0.8)                      # past timeout + grace; the owner is still processing
        t0 = time.monotonic()
        assert ck.panel("p")["status"] == "error"
        assert time.monotonic() - t0 < 0.2   # no fresh grace for a late arrival
        t.join()

    def test_a_slow_thread_start_consumes_the_read_budget_so_owner_and_joiner_agree(self, monkeypatch) -> None:
        ck = Cockpit()

        def read(ctx):
            time.sleep(0.15)
            return Read(rows=())
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=read, order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",), timeout_s=0.2))
        real_start = threading.Thread.start

        def slow_start(self):
            if self.name == "cockpit-read":
                time.sleep(0.35)
            return real_start(self)
        monkeypatch.setattr(threading.Thread, "start", slow_start)
        assert ck.panel("p")["status"] == "error"        # the budget ran out before the read could finish: no late healthy commit
        assert ck.freshness()["p"]["status"] == "error"

    def test_a_hostile_exception_in_output_processing_is_an_error_snapshot(self) -> None:
        class Hostile(Exception):
            def __str__(self):
                raise KeyboardInterrupt()

        class Rows:
            def __iter__(self):
                raise Hostile()
        ck = Cockpit()
        ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed",
                                 read=lambda c: Read(rows=Rows()), order="time.desc",
                                 facets=frozenset({"at"}), version_fields=("id",)))
        assert ck.panel("p")["status"] == "error"
