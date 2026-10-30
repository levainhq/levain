"""The Coordinator panel's Start, Stop and Hold (C10 S3): downstream panel verbs over the real coordinator store, a fake
``claude`` on PATH recording every call (no test here runs the real one, a real serve loop, the watcher thread or a
browser). The verbs are fired directly and through a real cockpit server that registers them."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
_REAL_THREAD = threading.Thread


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

    if how == "no thread":
        # the verbs module's own name for threading: no other module, pytest's included, sees the stand-in
        monkeypatch.setattr(coordinator_verbs, "threading", SimpleNamespace(Thread=NoThread, Lock=threading.Lock))
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
    assert threading.Thread is _REAL_THREAD
    cl.watch_once(store)
    assert store.launch(lid).state == "active" and len(fake.spawns()) == 1


@pytest.mark.parametrize("moved", ["back to wake", "to launching"])
def test_a_wake_that_lost_to_another_writer_says_try_again_only_when_the_state_would_accept(store, qdir, clock,
                                                                                         monkeypatch, moved):
    from levain.coordinator.envelopes import EnvelopeStore

    real_wake = EnvelopeStore.coord_wake

    def lost(self):
        if moved == "to launching":
            real_wake(self)                 # the other writer's wake, committed under the store's write lock
        return None

    monkeypatch.setattr(EnvelopeStore, "coord_wake", lost)
    v = _verbs(qdir, clock, launcher=_held_back)
    with pytest.raises(EditError) as ei:
        _fire(v, START_VERB)
    word, why, _row = _panel_state(store, clock)
    if moved == "back to wake":
        assert word == "wake"
        assert (ei.value.code, ei.value.http_status) == ("unavailable", 503)
        assert str(ei.value).startswith("the coordinator store changed under the start; try again")
    else:
        assert word == "launching"
        assert (ei.value.code, ei.value.http_status, str(ei.value)) == ("refused", 409, f"{word}: {why}")
    assert v._starts == []


def test_wait_starts_passes_over_a_listed_thread_that_never_started(qdir, clock):
    v = _verbs(qdir, clock)
    v._starts.append(threading.Thread(target=lambda: None))
    v.wait_starts(1)


def test_a_start_thread_runs_before_it_is_listed_and_process_exit_waits_for_it(store, qdir, clock, monkeypatch):
    from levain.cockpit import coordinator_verbs

    v = _verbs(qdir, clock, launcher=_held_back)
    seen: list = []

    class Watched(_REAL_THREAD):
        def start(self):
            seen.append((self in v._starts, self.daemon))
            super().start()

    monkeypatch.setattr(coordinator_verbs, "threading", SimpleNamespace(Thread=Watched, Lock=threading.Lock))
    _fire(v, START_VERB)
    v.wait_starts(30)
    assert seen == [(False, False)]
    assert len(v._starts) == 1 and v._starts[0].ident is not None


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
    assert ln.session_key not in out["said"]                # a launch's session key is no one to name
    assert out["said"].startswith(f"the coordinator lease (token {out['released']['token']}) was released; ")
    assert store.coord_lease() is None
    again = _fire(v, STOP_VERB)
    assert again["released"] is None and again["said"].startswith("there was no coordinator lease to release; ")
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


def _claims(qdir: Path) -> int:
    return _rows(qdir, "SELECT count(*) FROM lease_events WHERE kind = 'claim'")[0][0]


@pytest.mark.parametrize("after", ["the read fails", "another take", "a release"])
def test_hold_after_its_take_commits_never_raises_and_claims_no_hold_it_did_not_see(store, qdir, clock, monkeypatch,
                                                                                    after):
    from levain.coordinator.envelopes import EnvelopeStore

    def read_back(self):
        if after == "the read fails":
            raise sqlite3.OperationalError("database is locked")
        if after == "another take":
            store.coord_take(hold=HOLD)
        else:
            store.coord_release()
        return CoordStore.coord_lease(self)

    monkeypatch.setattr(EnvelopeStore, "coord_lease", read_back)
    v = _verbs(qdir, clock)
    out = _fire(v, HOLD_VERB)
    assert isinstance(out["token"], int) and out["until"] is None, out
    assert "you hold" not in out["said"] and f"the coordinator lease was taken (token {out['token']})" in out["said"]
    now = store.coord_lease()
    if after == "the read fails":
        assert now.token == out["token"] and "could not be read back now (OperationalError)" in out["said"]
    elif after == "another take":
        assert now.token != out["token"] and "already superseded" in out["said"]
        assert now.holder not in out["said"]
    else:
        assert now is None and "already released" in out["said"]


def test_a_hold_whose_read_back_failed_is_answered_again_under_its_key_never_taken_twice(store, qdir, tmp_path,
                                                                                        monkeypatch):
    from levain.coordinator.envelopes import EnvelopeStore

    def unreadable(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(EnvelopeStore, "coord_lease", unreadable)
    with _serving(tmp_path, qdir) as (url, _h):
        _panel(url)
        claims = _claims(qdir)
        status, first = _post_verb(url, HOLD_VERB, key="h1")
        assert (status, first.get("ok")) == (200, True), first
        status, again = _post_verb(url, HOLD_VERB, key="h1")
        assert status == 200 and again["result"]["token"] == first["result"]["token"], again
    assert _claims(qdir) == claims + 1
    assert store.coord_lease().token == first["result"]["token"]


# --- the server: registration, the manifest, the write token -------------------------------------------------------
@contextlib.contextmanager
def _serving(tmp_path, qdir, specs=None, **kw):
    """A write-scoped serve: it registers the verbs itself, over the directory its consent reader names (the fixture's).
    ``specs`` (or seams in ``kw``) replace what the serve builds, through the one function it builds them with."""
    from unittest import mock

    from levain.cockpit import coordinator_status as cs
    from tests.test_cockpit_k2a import _install, _serve

    real = cs.status_verbs
    _root, src, _ids = _install(tmp_path)
    with mock.patch.object(cs, "status_verbs", (lambda d, **k: specs) if specs is not None else (lambda d, **k: real(d, **{**kw, **k}))):
        with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", coordinator_verbs=True) as pair:
            yield pair


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


def test_a_caller_cannot_take_a_coordinator_verbs_name(store, qdir, tmp_path):
    from tests.test_cockpit_k2a import _install, _serve

    _root, src, _ids = _install(tmp_path)
    mine = status_verbs(lambda: qdir)
    with pytest.raises(ValueError, match="those names are the Coordinator panel's verbs"):
        with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", extra_verbs={START_VERB: mine[START_VERB]},
                    coordinator_verbs=True):
            pass


def test_a_writable_server_registers_the_coordinator_verbs_only_beside_a_watcher(store, qdir, tmp_path):
    from tests.test_cockpit_k2a import _get, _install, _serve

    _root, src, _ids = _install(tmp_path)
    assert src.write_scope is not None
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        assert not set(COORD_VERBS) & set(httpd.extra_verbs)
        assert not set(COORD_VERBS) & set(_get(f"{url}/cockpit/manifest.json", token="T")["verbs"])
        before = _snapshot(qdir)
        status, body = _post_verb(url, STOP_VERB)
        assert status != 200 and body.get("ok") is not True, body
        assert _snapshot(qdir) == before
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", coordinator_verbs=True) as (url, httpd):
        assert set(COORD_VERBS) <= set(httpd.extra_verbs)
        assert set(COORD_VERBS) <= set(_get(f"{url}/cockpit/manifest.json", token="T")["verbs"])


def test_the_coordinator_directory_lookup_answers_before_the_server_is_bound(store, qdir, tmp_path, monkeypatch):
    from levain.cockpit import coordinator_status as cs
    from levain.cockpit.providers import _dispatch_dir
    from tests.test_cockpit_k2a import _install, _serve

    real = cs.status_verbs
    looked: list = []

    def build(directory, **kw):
        looked.append(directory())          # during registration: the server is not bound yet
        return real(directory, **kw)

    monkeypatch.setattr(cs, "status_verbs", build)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", coordinator_verbs=True) as (_u, httpd):
        assert looked == [_dispatch_dir(None)]
        assert httpd.extra_verbs[START_VERB] is not None


@pytest.mark.parametrize("write", [True, False])
def test_the_serve_registers_the_coordinator_verbs_exactly_when_it_runs_the_watcher(tmp_path, monkeypatch, write):
    import levain.web_server as ws
    from levain.coordinator import supervise
    from tests.test_coordinator_supervise import _source

    asked: list = []
    started: list = []

    class _Watcher:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(1)
            return self

        def stop(self, *a, **k):
            return True

    class _Httpd:
        server_address = ("127.0.0.1", 7466)
        write_token = None

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(supervise, "Watcher", _Watcher)
    monkeypatch.setattr(ws, "_resolve_source", lambda p: _source(tmp_path))
    monkeypatch.setattr(ws, "make_server", lambda *a, **k: asked.append(k.get("coordinator_verbs")) or _Httpd())
    assert ws.run_web_server(tmp_path, open_browser=False, write=write) == 0
    assert asked == [write] and bool(started) is write


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
    with _serving(tmp_path, qdir, specs=v.specs()) as (url, _h):
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
