"""levain#157 W: a read that starts after a write is never answered by a provider read that started before it. The provider
itself is held across the invalidate (not ``_panel_compute``, which sits above the one-flight layer under test)."""
from __future__ import annotations

import threading
import time

import pytest

from levain.cockpit import Cockpit, ProviderSpec, Read
from levain.cockpit.engine import RowIn


class _Store:
    """A provider whose value is read at the START of the call, then held on the first call until ``gate`` opens."""

    def __init__(self) -> None:
        self.value = "OLD"
        self.gate, self.started = threading.Event(), threading.Event()
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._mu = threading.Lock()

    def read(self, ctx) -> Read:
        with self._mu:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            seen = self.value
            self.calls.append(seen)
            first = len(self.calls) == 1
        try:
            if first:
                self.started.set()
                self.gate.wait(10)
            return Read(rows=(RowIn(id="r1", title=seen, facets={"at": "2026-11-19T00:00:00+00:00"}),))
        finally:
            with self._mu:
                self.active -= 1


def _cockpit(store: _Store, *, refresh_every_s: float | None = None) -> Cockpit:
    spec = ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=store.read, order="time.desc",
                        facets=frozenset({"at"}), version_fields=("id",), refresh_every_s=refresh_every_s)
    ck = Cockpit()
    ck.register(spec)
    ck.read_cache_ttl_s = 1.5
    return ck


def _title(body: dict) -> str:
    return body["rows"][0]["title"]


def _read_across_a_write(invalidate) -> tuple[_Store, str, str, str]:
    store = _Store()
    ck = _cockpit(store)
    early: list = []
    t = threading.Thread(target=lambda: early.append(ck.panel("p", credential_class="token")))
    t.start()
    assert store.started.wait(5)
    store.value = "NEW"                                  # the write lands; the provider already read OLD
    invalidate(ck)
    late: list = []
    t2 = threading.Thread(target=lambda: late.append(ck.panel("p", credential_class="token")))
    t2.start()
    time.sleep(0.3)                                      # the late reader is now waiting on, or joined to, the held read
    store.gate.set()
    t.join(10)
    t2.join(10)
    assert not t.is_alive() and not t2.is_alive()
    after = ck.panel("p", credential_class="token")      # inside the TTL: whatever the late reader stored
    return store, _title(early[0]), _title(late[0]), _title(after)


@pytest.mark.parametrize("invalidate", [lambda ck: ck.invalidate_reads(["p"]), lambda ck: ck.invalidate_reads()],
                         ids=["keyed", "global"])
def test_a_read_after_a_write_does_not_join_the_provider_read_begun_before_it(invalidate) -> None:
    store, early, late, after = _read_across_a_write(invalidate)
    assert early == "OLD"                                # the pre-write caller keeps its own answer
    assert late == "NEW" and after == "NEW"
    assert store.calls == ["OLD", "NEW"]                 # the post-write caller ran a read of its own
    assert store.max_active == 1                         # and only after the older one had ended


def test_a_caller_that_keeps_finding_a_flight_older_than_a_write_gives_up_at_its_own_deadline(monkeypatch) -> None:
    import levain.cockpit.engine as eng
    from levain.cockpit.engine import _Flight, _State

    monkeypatch.setattr(eng, "PROCESS_GRACE_S", 0.0)
    ck, st = Cockpit(), _State()
    st.epoch = 1

    def stale() -> _Flight:
        fl = _Flight(0.3)
        fl.epoch = 0                                       # begun before the write the state has seen
        return fl

    st.pflight = stale()
    stop = threading.Event()

    def churn() -> None:                                   # each stale flight ends and the next one is already in place
        end = time.monotonic() + 1.5
        while time.monotonic() < end and not stop.is_set():
            time.sleep(0.05)
            nxt = stale()
            with st.lock:
                old, st.pflight = st.pflight, nxt
            old.out.set_result(None)
        with st.lock:
            old, st.pflight = st.pflight, None
        old.out.set_result(None)

    ran: list[int] = []
    t = threading.Thread(target=churn)
    t.start()
    t0 = time.monotonic()
    try:
        out = ck._single_flight(st, 0.3, lambda: ran.append(1) or "read", lambda raw: raw, lambda m: ("refused", m))
        took = time.monotonic() - t0
    finally:
        stop.set()
        t.join(10)
    assert out[0] == "refused" and ran == [], out
    assert took < 1.0, took                                # the caller's own budget (0.3s), not the churn's length


def test_a_write_to_another_panel_leaves_the_flight_shared() -> None:
    store, early, late, after = _read_across_a_write(lambda ck: ck.invalidate_reads(["other"]))
    assert store.calls == ["OLD"] and early == late == after == "OLD"


# --- a refresher panel: the write's background refresh -------------------------------------------------------


class _Gated:
    """A refresher panel's provider: reads ``value`` at the start of a call and holds the calls named in ``hold`` until released."""

    def __init__(self, hold: set[int]) -> None:
        self.value = "OLD"
        self.hold = {n: threading.Event() for n in hold}
        self.entered = {n: threading.Event() for n in hold}
        self.calls: list[str] = []
        self._mu = threading.Lock()

    def read(self, ctx) -> Read:
        with self._mu:
            self.calls.append(self.value)
            n, seen = len(self.calls), self.value
        if n in self.hold:
            self.entered[n].set()
            self.hold[n].wait(10)
        return Read(rows=(RowIn(id="r1", title=seen, facets={"at": "2026-11-19T00:00:00+00:00"}),))


def _refresher(prov: _Gated) -> Cockpit:
    ck = _cockpit(prov, refresh_every_s=600.0)
    ck.start()                                             # the first read, taken here, is call 1
    return ck


def _settled(ck: Cockpit, pid: str = "p") -> bool:
    for _ in range(200):
        with ck._rc_lock:
            if pid not in ck._rc_refreshing:
                return True
        time.sleep(0.02)
    return False


def test_the_refetch_that_races_the_write_refresh_is_not_what_the_page_keeps() -> None:
    prov = _Gated({2})
    ck = _refresher(prov)
    try:
        assert _title(ck.panel("p", credential_class="token")) == "OLD"
        prov.value = "NEW"
        ck.invalidate_reads(["p"])                         # the write's refresh (call 2) is now held in the provider
        assert prov.entered[2].wait(5)
        assert _title(ck.panel("p", credential_class="token")) == "OLD"     # the page's refetch: the snapshot so far
        prov.hold[2].set()
        assert _settled(ck)
        assert _title(ck.panel("p", credential_class="token")) == "NEW"     # not the stale entry, inside the TTL
    finally:
        ck.stop()


def test_a_manifest_read_cached_during_the_write_refresh_is_dropped_when_the_refresh_commits() -> None:
    prov = _Gated({2})
    ck = _refresher(prov)
    cred = {"class": "token", "name": "browser", "device_class": None}
    try:
        prov.value = "NEW"
        ck.invalidate_reads(["p"])                         # the write's refresh (call 2) is held in the provider
        assert prov.entered[2].wait(5)
        during = ck.manifest(cred)                         # cached now, carrying the head from before the refresh
        assert ck.manifest(cred) is during
        prov.hold[2].set()
        assert _settled(ck)
        assert ck.manifest(cred) is not during             # the refresh committed: the manifest is read again
    finally:
        ck.stop()


def test_a_write_that_lands_while_a_refresh_is_running_gets_a_read_after_it() -> None:
    prov = _Gated({2})
    ck = _refresher(prov)
    try:
        t = threading.Thread(target=lambda: ck.refresh("p"))          # a periodic cycle, inside its provider read
        t.start()
        assert prov.entered[2].wait(5)
        prov.value = "NEW"                                 # the write commits after that read began
        ck.invalidate_reads(["p"])
        prov.hold[2].set()
        t.join(10)
        assert _settled(ck)
        assert prov.calls == ["OLD", "OLD", "NEW"], prov.calls
        assert _title(ck.panel("p", credential_class="token")) == "NEW"
    finally:
        ck.stop()


def test_a_second_write_during_the_write_refresh_gets_its_own_read() -> None:
    prov = _Gated({2})
    ck = _refresher(prov)
    try:
        prov.value = "NEW"
        ck.invalidate_reads(["p"])
        assert prov.entered[2].wait(5)
        prov.value = "NEWER"                               # a second write, while the first one's refresh is out
        ck.invalidate_reads(["p"], refresh=True)
        prov.hold[2].set()
        assert _settled(ck)
        assert prov.calls == ["OLD", "NEW", "NEWER"], prov.calls
        assert _title(ck.panel("p", credential_class="token")) == "NEWER"
    finally:
        ck.stop()


def test_a_refresh_thread_that_cannot_start_leaves_no_mark_and_raises_nothing(monkeypatch) -> None:
    prov = _Gated(set())
    ck = _refresher(prov)
    try:
        real = threading.Thread

        class Refusing(real):
            def start(self):
                if self.name.startswith("cockpit-write-refresh"):
                    raise RuntimeError("can't start new thread")
                super().start()

        monkeypatch.setattr(threading, "Thread", Refusing)
        ck.invalidate_reads(["p"])                         # must not raise out of the server's response path
        assert not ck._rc_refreshing
        monkeypatch.setattr(threading, "Thread", real)
        prov.value = "NEW"
        ck.invalidate_reads(["p"])                         # and a later write still refreshes
        assert _settled(ck)
        assert prov.calls[-1] == "NEW"
    finally:
        ck.stop()
