"""The Coordinator panel's Start, Stop and Hold (C10 S3): downstream panel verbs over the real coordinator store, a fake
``claude`` on PATH recording every call (no test here runs the real one, a real serve loop, the watcher thread or a
browser). The verbs are fired directly and through a real cockpit server that registers them."""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.cockpit.coordinator_status import (COORD_STATUS_ID, COORD_VERBS, HOLD_VERB, START_VERB, STOP_VERB,
                                               _read_status, register_status_verbs, status_rows, status_verbs)
from levain.cockpit.coordinator_verbs import HOLD, CoordinatorVerbs
from levain.coordinator import launch as cl
from levain.coordinator import lease
from levain.coordinator.launch import Outcome
from levain.coordinator.store import CoordStore
from levain.writes import EditError
from tests.test_coordinator_launch import HID, _coordinator_dir
from tests.test_coordinator_supervise import _install_fake

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _levain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(cl, "READBACK_DELAY", 0.0)
    monkeypatch.setattr(cl, "UNLISTED_RECHECK", 0.0)


@pytest.fixture
def fake(tmp_path, monkeypatch, quick):
    f = _install_fake(tmp_path / "fakebin")
    monkeypatch.setenv("PATH", f"{f.dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return f


@pytest.fixture
def live_hook(monkeypatch):
    monkeypatch.setattr(cl, "hook_live", lambda d: (True, "wired to this install"))


@pytest.fixture
def qdir(tmp_path):
    """The default consent queue under LEVAIN_HOME: the directory a served cockpit's coordinator panels read."""
    d = tmp_path / "levain-home" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "coord-install"
    d.mkdir()
    return _coordinator_dir(d)


@pytest.fixture
def clock():
    return [T0]


@pytest.fixture
def store(qdir, workdir, clock):
    s = CoordStore(qdir, clock=lambda: clock[0])
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    return s


def _verbs(qdir, clock, **kw) -> CoordinatorVerbs:
    return CoordinatorVerbs(lambda: qdir, clock=lambda: clock[0], emit=kw.pop("emit", lambda line: None), **kw)


def _fire(v: CoordinatorVerbs, name: str) -> dict:
    return {START_VERB: v.start, STOP_VERB: v.stop, HOLD_VERB: v.hold}[name](None, {}, {}, False)


def _held_back(store_, lid: str) -> Outcome:
    """A launcher that starts nothing, as a start refused before its spawn claim does: the launch stays pending."""
    return Outcome(lid, False, refusal="held back by the test")


def _snapshot(qdir: Path) -> dict:
    """Every byte of the store: the database, its write-ahead log (absent and empty are the same: a reader may make an
    empty one), and every other file in the directory but SQLite's shared-memory index."""
    out: dict = {}
    for p in sorted(qdir.iterdir()):
        if p.name.endswith("-shm"):
            continue
        body = p.read_bytes()
        if p.name.endswith("-wal") and not body:
            continue
        out[p.name] = body
    return out


def _rows(qdir: Path, sql: str, *args) -> list:
    c = sqlite3.connect(f"file:{qdir / 'dispatch.db'}?mode=ro", uri=True)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _panel_state(store_, clock) -> tuple[str, str, str]:
    st = _read_status(store_, clock[0])
    return st["wake"], st["why"], status_rows(st, clock[0])[0].body


def _deaths(store_, fake, clock, n: int) -> None:
    """``n`` coordinator launches that die (the harness exits 2 after the spawn claim), an hour apart."""
    fake.mode("unknown")
    for i in range(n):
        d, _ = store_.accept_drop(f"drop {i}", surface="cli")
        assert not cl.launch_session(store_, store_.woken_launch(d.drop_id)).started
        if i < n - 1:
            clock[0] += timedelta(hours=1)
    fake.mode("ok")


# --- Start ---------------------------------------------------------------------------------------------------------
def test_start_on_a_wake_store_writes_one_pending_launch_and_starts_it(store, qdir, fake, live_hook, clock):
    assert _panel_state(store, clock)[0] == "wake"
    lines: list[str] = []
    v = _verbs(qdir, clock, emit=lines.append)
    out = _fire(v, START_VERB)
    v.wait_starts(30)
    assert out["state"] == "launching" and out["launch_id"].startswith("ln-"), out
    assert _rows(qdir, "SELECT launch_id, role FROM launches") == [(out["launch_id"], "coordinator")]
    assert _rows(qdir, "SELECT count(*) FROM launch_events WHERE kind = 'pending'") == [(1,)]
    (argv,) = fake.spawns()
    assert argv[:3] == ["--bg", "-n", f"levain-coordinator-{out['launch_id'].removeprefix('ln-')}"]
    assert store.launch(out["launch_id"]).state == "active"
    assert lines == [f"launch {out['launch_id']} started: claude attach {HID}"]


@pytest.mark.parametrize("deaths, word", [(1, "backoff"), (lease.MAX_DEATHS, "stopped")])
def test_start_in_backoff_or_stopped_says_the_panels_why_and_writes_nothing(store, qdir, fake, live_hook, clock,
                                                                           deaths, word):
    _deaths(store, fake, clock, deaths)
    got_word, why, row = _panel_state(store, clock)
    assert got_word == word
    before, spawns = _snapshot(qdir), len(fake.spawns())
    called: list[str] = []
    v = _verbs(qdir, clock, launcher=lambda s, lid: called.append(lid))
    with pytest.raises(EditError) as ei:
        _fire(v, START_VERB)
    assert (ei.value.code, ei.value.http_status) == ("refused", 409)
    assert str(ei.value) == f"{word}: {why}" and row == f"Coordinator: {word}: {why}"   # the panel's own line
    assert ei.value.extra == {"state": word, "why": why}
    assert _snapshot(qdir) == before and len(fake.spawns()) == spawns and called == []


def test_a_second_start_while_one_is_pending_or_held_says_so_and_writes_nothing(store, qdir, clock):
    called: list[str] = []
    v = _verbs(qdir, clock, launcher=lambda s, lid: called.append(lid) or _held_back(s, lid))
    first = _fire(v, START_VERB)
    v.wait_starts(30)
    assert store.launch(first["launch_id"]).state == "pending"
    for expect in ("launching", "held"):
        if expect == "held":
            pressed = _snapshot(qdir)
            _fire(v, HOLD_VERB)
            assert _snapshot(qdir) != pressed           # the snapshot sees a write: its equality below is evidence
        before = _snapshot(qdir)
        with pytest.raises(EditError) as ei:
            _fire(v, START_VERB)
        word, why, _row = _panel_state(store, clock)
        assert word == expect and str(ei.value) == f"{word}: {why}", (expect, str(ei.value))
        assert _snapshot(qdir) == before
    assert called == [first["launch_id"]]
    assert _rows(qdir, "SELECT count(*) FROM launches") == [(1,)]


def test_two_starts_racing_past_the_read_ask_for_one_launch(store, qdir, clock, monkeypatch):
    """Both Starts read ``wake`` before either writes; the wake's own transaction lets one launch through."""
    from levain.cockpit import coordinator_status

    real = coordinator_status._read_status
    both_read = threading.Barrier(2, timeout=10)

    def read_then_meet(s, now):
        got = real(s, now)
        if got["wake"] == "wake":
            both_read.wait()
        return got

    monkeypatch.setattr(coordinator_status, "_read_status", read_then_meet)
    v = _verbs(qdir, clock, launcher=_held_back)
    results: list = []

    def press() -> None:
        try:
            results.append(_fire(v, START_VERB))
        except EditError as exc:
            results.append(exc)

    racers = [threading.Thread(target=press) for _ in range(2)]
    for r in racers:
        r.start()
    for r in racers:
        r.join(30)
    v.wait_starts(30)
    started = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, EditError)]
    assert len(started) == 1 and len(refused) == 1, results
    assert str(refused[0]).startswith(f"launching: coordinator launch {started[0]['launch_id']} is pending until")
    assert _rows(qdir, "SELECT count(*) FROM launches") == [(1,)]


@pytest.mark.parametrize("how", ["no thread", "refused before the spawn claim"])
def test_a_start_that_did_not_run_leaves_the_launch_to_the_watchers_next_pass(store, qdir, fake, live_hook, clock,
                                                                              monkeypatch, how):
    from levain.cockpit import coordinator_verbs

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False

    real_thread = threading.Thread
    if how == "no thread":
        monkeypatch.setattr(coordinator_verbs.threading, "Thread", NoThread)
    lines: list[str] = []
    v = _verbs(qdir, clock, launcher=_held_back, emit=lines.append)
    out = _fire(v, START_VERB)
    v.wait_starts(30)
    lid = out["launch_id"]
    if how == "no thread":
        assert "watcher starts it on its next pass" in out["said"] and lines == []
    else:
        assert lines == [f"launch {lid} not started: held back by the test"]      # the serve log has the words
    assert store.launch(lid).state == "pending" and fake.spawns() == []
    monkeypatch.setattr(threading, "Thread", real_thread)
    cl.watch_once(store)
    assert store.launch(lid).state == "active" and len(fake.spawns()) == 1


# --- Stop ----------------------------------------------------------------------------------------------------------
def test_stop_then_one_watcher_pass_stops_the_session(store, qdir, fake, live_hook, clock):
    v = _verbs(qdir, clock)
    lid = _fire(v, START_VERB)["launch_id"]
    v.wait_starts(30)
    assert store.launch(lid).state == "active"
    out = _fire(v, STOP_VERB)
    ln = store.launch(lid)
    assert (ln.state, ln.cause) == ("ended", "operator release") and fake.stops() == []
    assert out["released"] == {"holder": ln.session_key, "token": out["released"]["token"]}
    assert store.coord_lease() is None
    lines = cl.watch_once(store)
    assert fake.stops() == [["stop", HID]]
    assert any(line.startswith(f"launch {lid} ended (operator release)") for line in lines), lines


# --- Hold ----------------------------------------------------------------------------------------------------------
def test_hold_voids_a_pending_launch_and_holds_the_lease_for_thirty_minutes(store, qdir, clock):
    assert HOLD == timedelta(minutes=30)
    v = _verbs(qdir, clock, launcher=_held_back)
    lid = _fire(v, START_VERB)["launch_id"]
    v.wait_starts(30)
    assert store.launch(lid).state == "pending"
    out = _fire(v, HOLD_VERB)
    ln = store.launch(lid)
    assert (ln.state, ln.cause) == ("failed", "takeover")
    held = store.coord_lease()
    until = utc_stamp(T0 + timedelta(minutes=30))
    assert (held.holder, held.until, held.token, held.live) == ("operator", until, out["token"], True)
    assert out["until"] == until
    assert _panel_state(store, clock)[0] == "held"


# --- the server: registration, the manifest, the write token -------------------------------------------------------
def _serving(tmp_path, qdir, **kw):
    from tests.test_cockpit_k2a import _install, _serve

    _root, src, _ids = _install(tmp_path)
    return _serve(src, surface_tokens={"browser": "T"}, install_class="reported",
                  extra_verbs=status_verbs(lambda: qdir, **kw))


def _panel(url: str) -> dict:
    from tests.test_cockpit_k2a import _get

    _get(f"{url}/cockpit/manifest.json", token="T")           # the coordinator panels are discovered on a manifest read
    return _get(f"{url}/cockpit/panel/{COORD_STATUS_ID}.json", token="T")


def _post_verb(url: str, verb: str, token: "str | None" = "T", key: str = "k1") -> tuple[int, dict]:
    from tests.test_cockpit_k2a import _post

    return _post(f"{url}/cockpit/verb", {"verb": verb, "panel_id": COORD_STATUS_ID, "params": {},
                                         "idempotency_key": key}, token=token)


def test_the_verbs_are_offered_by_the_panel_and_listed_in_the_manifest(store, qdir, tmp_path):
    from tests.test_cockpit_k2a import _get

    with _serving(tmp_path, qdir) as (url, _httpd):
        actions = _panel(url).get("actions") or []
        assert [a["verb"] for a in actions] == list(COORD_VERBS)
        assert {(a["tier"], a["gesture"]) for a in actions} == {("C1", "direct")}
        assert all(a["target"] == {"panel_id": COORD_STATUS_ID} for a in actions)
        verbs = _get(f"{url}/cockpit/manifest.json", token="T")["verbs"]
        assert {n: (verbs[n]["label"], verbs[n]["tier"], verbs[n]["route"], verbs[n]["fields"]) for n in COORD_VERBS} == {
            START_VERB: ("Start", "C1", "cockpit", []), STOP_VERB: ("Stop", "C1", "cockpit", []),
            HOLD_VERB: ("Hold it myself", "C1", "cockpit", [])}


def test_a_server_that_registers_none_offers_none(store, qdir, tmp_path):
    from tests.test_cockpit_k2a import _install, _serve

    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        panel = _panel(url)
        assert panel["rows"] and (panel.get("actions") or []) == []
        status, body = _post_verb(url, START_VERB)
        assert (status, body["error"]) == (404, "unknown_verb"), body


@pytest.mark.parametrize("verb", COORD_VERBS)
def test_every_verb_is_refused_without_the_write_token_and_writes_nothing(store, qdir, tmp_path, verb):
    """Mirrors tests/test_cockpit_coord_drop.py's gate test: no token, and a wrong one, are 403 credential_required."""
    with _serving(tmp_path, qdir, launcher=_held_back) as (url, _httpd):
        _panel(url)
        before = _snapshot(qdir)
        for token in (None, "wrong-token"):
            status, body = _post_verb(url, verb, token=token)
            assert (status, body.get("error")) == (403, "credential_required"), (token, status, body)
        assert _snapshot(qdir) == before


def test_start_through_the_server_starts_the_coordinator_and_a_second_says_why(store, qdir, fake, live_hook, clock,
                                                                               tmp_path):
    v = _verbs(qdir, clock)
    from tests.test_cockpit_k2a import _install, _serve

    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", extra_verbs=v.specs()) as (url, _h):
        _panel(url)
        status, out = _post_verb(url, START_VERB, key="s1")
        assert (status, out.get("ok")) == (200, True), out
        v.wait_starts(30)
        lid = out["result"]["launch_id"]
        assert store.launch(lid).state == "active" and len(fake.spawns()) == 1
        # the same key again is the first fire's answer, never a second wake
        status, again = _post_verb(url, START_VERB, key="s1")
        assert status == 200 and again["result"]["launch_id"] == lid, again
        before = _snapshot(qdir)
        status, refused = _post_verb(url, START_VERB, key="s2")
        word, why, _row = _panel_state(store, clock)
        assert (status, refused["error"], refused["message"], refused["state"]) == (409, "refused", f"{word}: {why}",
                                                                                    "held"), refused
        assert _snapshot(qdir) == before and len(fake.spawns()) == 1
        status, stopped = _post_verb(url, STOP_VERB, key="p1")
        assert status == 200 and store.launch(lid).state == "ended", stopped
        status, held = _post_verb(url, HOLD_VERB, key="h1")
        assert status == 200 and store.coord_lease().holder == "operator", held


def test_register_status_verbs_registers_each_as_a_downstream_panel_verb(qdir):
    from levain.cockpit.verbs import KERNEL_VERBS, VerbRegistry

    reg = VerbRegistry()
    specs = register_status_verbs(reg, lambda: qdir)
    assert list(specs) == list(COORD_VERBS)
    for name in COORD_VERBS:
        spec = reg.get(name)
        assert reg.is_downstream(name) and spec.binding == "panel" and spec.device_ok is False
        assert spec.tier({}, None, "unverified", T0.date(), policy=_no_policy()) == "C1"
    assert not {v.name for v in KERNEL_VERBS} & set(COORD_VERBS)


def _no_policy():
    class _P:
        def apply(self, verb, base):
            return base
    return _P()
