"""C10 S2: the read-only Coordinator panel, one fixture store per wake_state, built with the store's own API."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.coordinator_panels import coordinator_discoverer
from levain.cockpit.coordinator_status import COORD_STATUS_ID
from levain.cockpit.engine import Cockpit
from levain.coordinator import lease
from levain.coordinator.store import CoordStore

T0 = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def qdir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir: Path, clock: Clock) -> CoordStore:
    s = CoordStore(qdir, clock=clock)
    s.coord_set_agent("claude", cwd=str(qdir.parent))
    return s


def _drop(store: CoordStore, words: str = "fix the build") -> None:
    store.accept_drop(words, surface="cli")


def _claimed(store: CoordStore) -> None:
    ln = store.pending_coordinator()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None


def _boot(store: CoordStore) -> object:
    _drop(store)
    ln = store.coord_status()["launches"][0]
    got, why = store.launch_activate(ln["launch_id"], harness_session_id="probe-A")
    assert why is None
    return got


def _panel(qdir: Path, clock: Clock) -> dict:
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(coordinator_discoverer(lambda: qdir, clock=clock), retire=True)
    return ck.panel(COORD_STATUS_ID)


def _rows(panel: dict) -> dict[str, str]:
    return {r["id"]: r["body"] for r in panel["rows"]}


def _state_line(panel: dict) -> str:
    return _rows(panel)["state"]


def test_wake_names_the_next_drop_in_the_stores_own_words(store, qdir, clock) -> None:
    body = _state_line(_panel(qdir, clock))
    assert body == "Coordinator: wake: no coordinator holds the lease; the next drop asks for one"
    assert _rows(_panel(qdir, clock))["lease"] == "Lease: none held"
    assert _rows(_panel(qdir, clock))["setting"].startswith("Setting: claude, in ")


def test_launching_names_the_pending_launch_and_its_until(store, qdir, clock) -> None:
    _drop(store)
    ln = store.pending_coordinator()
    panel = _panel(qdir, clock)
    assert _state_line(panel) == (f"Coordinator: launching: coordinator launch {ln.launch_id} is pending until "
                                  f"{ln.until}")
    rows = _rows(panel)
    assert f"launch:{ln.launch_id}" in rows and "pending until " + ln.until in rows[f"launch:{ln.launch_id}"]
    assert "bootstrap" in rows["lease"]


def test_held_names_the_holder_until_and_token(store, qdir, clock) -> None:
    got = _boot(store)
    held = store.coord_lease()
    panel = _panel(qdir, clock)
    assert _state_line(panel) == (f"Coordinator: held: {got.session_key} holds the lease until {held.until} "
                                  f"(token {held.token})")
    assert f"{got.session_key} until {held.until} (token {held.token}" in _rows(panel)["lease"]
    assert "past its until" not in _rows(panel)["lease"]


def test_idle_is_a_running_coordinator_without_the_lease(store, qdir, clock) -> None:
    got = _boot(store)
    clock.advance(seconds=(lease.LEASE_TTL + timedelta(minutes=1)).total_seconds())
    panel = _panel(qdir, clock)
    body = _state_line(panel)
    assert body.startswith(f"Coordinator: idle: coordinator launch {got.launch_id} is running without the lease; ")
    assert "levain coord claim" in body
    assert "past its until, the lapse is not yet recorded" in _rows(panel)["lease"]    # nothing wrote the lapse


def test_unset_with_no_setting_shows_the_cli_line_verbatim(qdir, clock) -> None:
    s = CoordStore(qdir, clock=clock)
    _drop(s)                                    # the store exists; a drop with no agent set only queues
    panel = _panel(qdir, clock)
    assert _state_line(panel) == ("Coordinator: unset: no coordinator agent is set; drops queue for you "
                                  "(levain coord agent claude sets one)")
    assert _rows(panel)["setting"] == "Setting: none set (levain coord agent claude sets one)"


def test_unset_with_agent_none_says_so(store, qdir, clock) -> None:
    store.coord_set_agent("none")
    assert _state_line(_panel(qdir, clock)) == ("Coordinator: unset: the coordinator agent is none; drops queue "
                                                "for you (levain coord agent claude sets one)")


def test_backoff_names_when_the_next_wake_is_allowed(store, qdir, clock) -> None:
    _drop(store)
    _claimed(store)
    clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
    _drop(store, "one death")                   # a drop is what writes the expiry the view then reads
    panel = _panel(qdir, clock)
    assert _state_line(panel).startswith("Coordinator: backoff: 1 coordinator launch(es) died; the next waits until ")
    assert any(r["id"].startswith("launch:") and r["body"].startswith("Launch · expired: claude coordinator · ")
               for r in panel["rows"])


def test_stopped_after_three_deaths_names_the_two_ways_to_look(store, qdir, clock) -> None:
    for n, wait in enumerate((None, lease.BACKOFF[0], lease.BACKOFF[1])):
        if wait is not None:
            _drop(store, f"too soon {n}")
            clock.advance(seconds=wait.total_seconds())
        _drop(store, f"drop {n}")
        _claimed(store)
        clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
    _drop(store, "after three deaths")
    panel = _panel(qdir, clock)
    assert _state_line(panel) == ("Coordinator: stopped: 3 coordinator launches in a row died; nothing launches "
                                  "until you look (levain coord take, or levain coord release)")
    assert sum(r["id"].startswith("launch:") for r in panel["rows"]) == 3


def test_launches_carry_state_and_cause_newest_first(store, qdir, clock) -> None:
    got = _boot(store)
    store.launch_end(got.launch_id, "ended", "0 turns")
    rows = [r for r in _panel(qdir, clock)["rows"] if r["id"].startswith("launch:")]
    assert len(rows) == 1 and rows[0]["body"].startswith("Launch · ended: claude coordinator · 0 turns · ")


def test_no_store_draws_no_panel_and_makes_none(qdir, clock) -> None:
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(coordinator_discoverer(lambda: qdir, clock=clock), retire=True)
    assert COORD_STATUS_ID not in [p for z in ck.manifest({"class": "none", "device_class": None})["regions"]["zones"]
                                   for p in z["panels"]]
    assert list(qdir.iterdir()) == []


def test_the_read_writes_nothing_even_where_coord_status_would(store, qdir, clock) -> None:
    """A lapsed lease: ``CoordStore.coord_status`` would record the lapse; the panel must leave the file as it was."""
    _boot(store)
    clock.advance(seconds=(lease.LEASE_TTL + timedelta(minutes=1)).total_seconds())

    def counts() -> tuple[int, int]:
        c = sqlite3.connect(qdir / "dispatch.db")
        try:
            return (c.execute("SELECT COUNT(*) FROM lease_events").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM launch_events").fetchone()[0])
        finally:
            c.close()

    before = counts()
    _panel(qdir, clock)
    assert counts() == before


def test_the_provider_reaches_the_store_only_through_the_readonly_opener(store, qdir, clock, monkeypatch) -> None:
    def refuse(*a, **k):
        raise AssertionError("the status panel reached a write path")

    for name in ("_write", "_connect", "coord_status", "coord_wake", "coord_take", "coord_release"):
        monkeypatch.setattr(CoordStore, name, refuse)
    assert _state_line(_panel(qdir, clock)).startswith("Coordinator: wake: ")


def test_the_readonly_connection_refuses_a_write(store) -> None:
    conn = store.open_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO coord_settings (agent, model, cwd, at) VALUES ('none', NULL, NULL, 'x')")
    finally:
        conn.close()


def test_a_row_version_moves_with_the_store_not_the_clock(store, qdir, clock) -> None:
    first = _panel(qdir, clock)
    clock.advance(seconds=5)
    assert [r["version"] for r in _panel(qdir, clock)["rows"]] == [r["version"] for r in first["rows"]]
    store.coord_set_agent("none")
    assert [r["version"] for r in _panel(qdir, clock)["rows"]] != [r["version"] for r in first["rows"]]
