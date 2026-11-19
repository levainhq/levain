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


def _external_cockpit(tmp_path: Path):
    """A served-style cockpit (window on) with one on-demand panel ``od`` whose rows change WITHOUT a write through the
    server (another process: a phone answer, a seat's log line), so nothing calls ``invalidate_reads``. ``hold`` parks a
    provider call after it took its rows, until set."""
    from levain.cockpit.engine import Read
    from tests.test_cockpit_k1 import _install as _k1_install
    from tests.test_cockpit_k1 import _simple

    _r, src = _k1_install(tmp_path)
    c = build_default_cockpit(src)
    c.read_cache_ttl_s = 1.5
    state = {"rows": [_row(1)], "reads": 0, "hold": None, "in_read": threading.Event()}

    def read(ctx):
        state["reads"] += 1
        took = tuple(state["rows"])
        hold = state["hold"]
        if hold is not None:
            state["in_read"].set()
            assert hold.wait(10)
        return Read(rows=took)

    c.register(_simple("od", rows=(), read=read))
    return c, state


def _row(n: int):
    from levain.cockpit.engine import RowIn
    return RowIn(f"r{n}", f"row {n}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r{n}"})


def test_the_watcher_build_never_answers_with_a_read_begun_before_it(tmp_path: Path) -> None:
    # the stale-band-row timeline: a page read seeds the window, another process changes the store, the token moves and
    # the watcher builds ONCE. Sharing the page's snapshot there would leave the etag where it was: no event until the
    # next token move or the backstop
    c, state = _external_cockpit(tmp_path)
    page = c.panel("od", credential_class="none")
    state["rows"] = [_row(1), _row(2)]
    built = c.manifest(NONE, read_after=time.monotonic())
    assert state["reads"] == 2
    assert built["panels"]["od"]["etag"] != page["etag"]
    # the page refetches what was announced, inside the panel cache's window of its own first read: it gets the
    # watcher's read (a newer committed read is a new cache key), never its own cached older body, and no new read
    assert c.panel("od", credential_class="none")["etag"] == built["panels"]["od"]["etag"]
    assert state["reads"] == 2


def test_a_watcher_that_joins_a_flight_begun_before_it_reads_again(tmp_path: Path) -> None:
    c, state = _external_cockpit(tmp_path)
    state["hold"] = hold = threading.Event()
    page_out: list = []
    page = threading.Thread(target=lambda: page_out.append(c.panel("od", credential_class="none")))
    page.start()
    assert state["in_read"].wait(10)                        # the page's read took the old rows and is still running
    state["rows"] = [_row(1), _row(2)]                      # the other process's change lands
    real, watcher_reads, joined = c._read_panel, [], threading.Event()

    def watching(spec, st, ctx):
        if spec.id == "od" and threading.current_thread().name != page.name:
            watcher_reads.append(1)
            joined.set()
        return real(spec, st, ctx)

    c._read_panel = watching                                 # type: ignore[method-assign]
    out: list = []
    w = threading.Thread(target=lambda: out.append(c.manifest(NONE, read_after=time.monotonic())))
    w.start()
    assert joined.wait(10)
    time.sleep(0.2)                                          # the watcher is inside the page's flight now
    state["hold"] = None
    hold.set()
    page.join(10)
    w.join(10)
    assert len(watcher_reads) == 2 and state["reads"] == 2
    fresh = c.manifest(NONE, read_after=time.monotonic())["panels"]["od"]["etag"]
    assert out[0]["panels"]["od"]["etag"] == fresh != page_out[0]["etag"]


def test_a_reader_after_a_write_never_gets_the_flight_the_write_straddled(tmp_path: Path) -> None:
    # read-your-writes at the join (L1 r1): the page answers a row, the write's response reaches it, it reads at once,
    # and a read begun before the write is still running: the reader must not be handed that read's result
    c, state = _external_cockpit(tmp_path)
    state["hold"] = hold = threading.Event()
    first: list = []
    t1 = threading.Thread(target=lambda: first.append(c.panel("od", credential_class="none")))
    t1.start()
    assert state["in_read"].wait(10)                        # a read began, with the old rows
    state["rows"] = [_row(1), _row(2)]
    c.invalidate_reads()                                    # the write is admitted (its own store change above)
    second: list = []
    t2 = threading.Thread(target=lambda: second.append(c.panel("od", credential_class="none")))
    t2.start()
    time.sleep(0.2)                                         # the second reader is inside the old flight now
    state["hold"] = None
    hold.set()
    t1.join(10)
    t2.join(10)
    assert state["reads"] == 2
    assert [r["id"] for r in second[0]["rows"]] != [r["id"] for r in first[0]["rows"]]
    assert {r["id"] for r in second[0]["rows"]} == {"r1", "r2"}


@pytest.mark.parametrize("ttl", [None, 0])
def test_nothing_is_shared_where_the_read_cache_is_off(ck, monkeypatch, ttl) -> None:
    ck.read_cache_ttl_s = ttl
    n = _provider_reads(monkeypatch, ck)
    ck.manifest(NONE)
    ck.panel(PID, credential_class="none")
    assert n[PID] == 2
