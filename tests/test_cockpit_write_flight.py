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


def test_a_write_to_another_panel_leaves_the_flight_shared() -> None:
    store, early, late, after = _read_across_a_write(lambda ck: ck.invalidate_reads(["other"]))
    assert store.calls == ["OLD"] and early == late == after == "OLD"
