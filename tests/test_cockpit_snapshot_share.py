"""levain#138: one on-demand provider read serves every reader inside the read cache window after it commits.

A page load read each on-demand panel's provider at least twice (its manifest, then the panel route: two cache keys), the
change stream's watcher read it again on its own tick, and every open device's credential computed its own manifest. These
pin the sharing and, above all, its edges: a write admitted after a read STARTED is never answered with that read, a failed
read is never shared, the window counts from the commit, and nothing is shared where the read cache is off."""
from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from levain.cockpit.engine import Cockpit, Fault
from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_k2a import _install

PID = "tray"     # an on-demand list panel of the default cockpit
NONE = {"class": "none", "device_class": None}
TOKEN = {"class": "token", "name": "browser", "device_class": None}


@pytest.fixture()
def ck(tmp_path: Path) -> Cockpit:
    _root, src, _ids = _install(tmp_path)
    c = build_default_cockpit(src)
    c.read_cache_ttl_s = 1.5
    assert c._specs[PID].refresh_every_s is None
    return c


def _provider_reads(monkeypatch, ck: Cockpit, *, hold: threading.Event | None = None,
                    started: threading.Event | None = None, fail_first: bool = False) -> Counter:
    """Counts provider reads per panel (``_normalize`` runs once per provider call). ``hold`` parks the PID read after its
    provider ran until set; ``fail_first`` turns the first PID read into a Fault."""
    n: Counter = Counter()
    real = ck._normalize

    def counting(spec, ctx, res):
        n[spec.id] += 1
        if spec.id == PID:
            if started is not None:
                started.set()
            if hold is not None:
                assert hold.wait(10)
            if fail_first and n[PID] == 1:
                return Fault("boom")
        return real(spec, ctx, res)

    monkeypatch.setattr(ck, "_normalize", counting)
    return n


def test_manifest_panel_route_and_another_credential_share_one_provider_read(ck, monkeypatch) -> None:
    n = _provider_reads(monkeypatch, ck)
    m = ck.manifest(NONE)
    assert n[PID] == 1
    p = ck.panel(PID, credential_class="none")              # the page's panel read right after its manifest
    ck.manifest(TOKEN)                                       # another device: its own manifest, the same snapshot
    ck.panel(PID, credential_class="token")
    assert n[PID] == 1
    assert p["etag"] == m["panels"][PID]["etag"]             # the body is the one the manifest's head described


def test_a_write_after_the_read_sends_the_next_reader_to_the_provider(ck, monkeypatch) -> None:
    n = _provider_reads(monkeypatch, ck)
    ck.manifest(NONE)
    ck.invalidate_reads()
    ck.panel(PID, credential_class="none")
    assert n[PID] == 2


def test_a_read_that_started_before_a_write_is_never_shared_after_it(ck, monkeypatch) -> None:
    hold, started = threading.Event(), threading.Event()
    n = _provider_reads(monkeypatch, ck, hold=hold, started=started)
    t = threading.Thread(target=lambda: ck.panel(PID, credential_class="none"))
    t.start()
    assert started.wait(10)
    ck.invalidate_reads()                                    # the write lands while the read is in flight
    hold.set()
    t.join(10)
    assert n[PID] == 1
    ck.panel(PID, credential_class="token")                  # a reader after the write: never the pre-write read
    assert n[PID] == 2


def test_a_failed_read_is_never_shared(ck, monkeypatch) -> None:
    n = _provider_reads(monkeypatch, ck, fail_first=True)
    first = ck.panel(PID, credential_class="none")
    assert first["status"] == "error"
    again = ck.panel(PID, credential_class="token")
    assert n[PID] == 2 and again["status"] != "error"


def test_the_window_counts_from_the_commit_and_then_ends(ck, monkeypatch) -> None:
    ck.read_cache_ttl_s = 0.3
    hold = threading.Event()
    n = _provider_reads(monkeypatch, ck, hold=hold)
    timer = threading.Timer(0.5, hold.set)                   # the read takes longer than the window
    timer.start()
    ck.panel(PID, credential_class="none")
    ck.manifest(NONE)                                        # right after the commit: shared, though the read STARTED >0.3 s ago
    assert n[PID] == 1
    time.sleep(0.4)
    ck.manifest(TOKEN)                                       # past the window: a fresh read
    assert n[PID] == 2


@pytest.mark.parametrize("ttl", [None, 0])
def test_nothing_is_shared_where_the_read_cache_is_off(ck, monkeypatch, ttl) -> None:
    ck.read_cache_ttl_s = ttl
    n = _provider_reads(monkeypatch, ck)
    ck.manifest(NONE)
    ck.panel(PID, credential_class="none")
    assert n[PID] == 2
