"""levain#138: one on-demand provider read serves every reader inside the read cache window after it commits.

A page load read each on-demand panel's provider at least twice (its manifest, then the panel route: two cache keys), the
change stream's watcher read it again on its own tick, and every open device's credential computed its own manifest. These
pin the sharing and, above all, its edges: a write admitted after a read STARTED is never answered with that read, a failed
read is never shared, the window counts from the commit, and nothing is shared where the read cache is off."""
from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def test_one_parallel_reads_timeout_abandons_only_the_shared_reads_it_used() -> None:
    # codex L3 r1: the manifest's panels read side by side over one cycle; a panel that times out must not kill a
    # shared read another panel started and that is still inside its own deadline
    from datetime import datetime, timezone

    from levain.cockpit.engine import ReadContext

    root = ReadContext(datetime.now(timezone.utc))
    a, b, c = root.child(), root.child(), root.child()
    gate, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert gate.wait(10)
        return "x-value"

    t = threading.Thread(target=lambda: a.memo("x", slow))
    t.start()
    assert started.wait(10)
    b.memo("y", lambda: "y-value")
    b.abandon()                                   # b timed out: it never asked for "x"
    gate.set()
    t.join(10)
    assert c.memo("x", lambda: "recomputed") == "x-value"
    hung = threading.Event()
    t2 = threading.Thread(target=lambda: a.memo("z", lambda: hung.wait(10)))
    t2.start()
    time.sleep(0.1)
    a.abandon()                                   # a's own hung read is dead for the rest of the cycle
    with pytest.raises(TimeoutError):
        c.memo("z", lambda: None)
    hung.set()
    t2.join(10)


def _clock_on(ck: Cockpit, day: int) -> None:
    ck._clock = lambda: datetime(2026, 11, day, 12, 0, tzinfo=timezone.utc)


def test_a_page_manifest_after_a_watcher_build_is_never_the_entry_cached_before_it(ck) -> None:
    # gemini L3 r1: the watcher's fresh build announced; the page's manifest inside the window must not be the older entry
    m1 = ck.manifest(NONE)
    assert ck.manifest(NONE) is m1
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck.manifest(NONE) is not m1


def test_a_refresher_panels_refetch_after_a_watcher_build_is_a_new_entry(ck) -> None:
    # codex L3 r1: a head can move with no provider read (the day turning, a refresher's snapshot ageing): the watcher's
    # build is what announces it, and the page's refetch must not be the entry cached before that build
    from levain.cockpit.engine import Read
    from tests.test_cockpit_k1 import _simple

    ck.register(_simple("cyc", rows=(), read=lambda ctx: Read(rows=(_row(1),)), refresh_every_s=60.0, stale_after_s=120.0))
    ck.refresh("cyc")
    p1 = ck.panel("cyc", credential_class="none")
    assert ck.panel("cyc", credential_class="none") is p1
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck.panel("cyc", credential_class="none") is not p1


def test_the_first_watcher_build_moves_the_generation_once(tmp_path: Path) -> None:
    # a page cached manifest A, a provider changed before the stream's first build: that build announces nothing to diff
    # against, but the page's refetch after the stream's hello must still not be A
    c, state = _external_cockpit(tmp_path)
    a = c.manifest(NONE)
    state["rows"] = [_row(1), _row(2)]                       # another process changes a value after the page cached A
    c.manifest(NONE, read_after=time.monotonic())            # the stream's first build
    assert c._obs_gen == 1
    b = c.manifest(NONE)
    assert b is not a
    assert b["panels"]["od"]["etag"] != a["panels"]["od"]["etag"]


def test_a_later_build_that_announces_nothing_leaves_the_page_sharing_what_it_cached(ck, monkeypatch) -> None:
    # an equal later build announces nothing: it must not move the generation, or the page's reads right after the
    # stream starts recompute (what a bump on every build did)
    n = _provider_reads(monkeypatch, ck)
    ck.manifest(NONE, read_after=time.monotonic())           # the first build bumps once
    assert ck._obs_gen == 1
    m1 = ck.manifest(NONE)                                   # cached under the generation the first build left
    assert n[PID] >= 1
    ck.manifest(NONE, read_after=time.monotonic())           # nothing moved since
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck._obs_gen == 1
    reads = n[PID]
    assert ck.manifest(NONE) is m1                           # still the entry cached after the first build
    assert n[PID] == reads


def test_a_watcher_build_whose_etags_differ_moves_the_generation_once(tmp_path: Path) -> None:
    c, state = _external_cockpit(tmp_path)
    c.manifest(NONE, read_after=time.monotonic())            # the first build
    gen = c._obs_gen
    state["rows"] = [_row(1), _row(2)]                       # a provider value moves (another process)
    c.manifest(NONE, read_after=time.monotonic())
    assert c._obs_gen == gen + 1
    c.manifest(NONE, read_after=time.monotonic())            # the same etags again: announced once, bumped once
    assert c._obs_gen == gen + 1


def test_a_watcher_build_after_the_day_turns_moves_the_generation_once(ck) -> None:
    _clock_on(ck, 10)
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck._obs_gen == 1
    _clock_on(ck, 11)                                        # no provider read moved; the head's date did
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck._obs_gen == 2
    ck.manifest(NONE, read_after=time.monotonic())
    assert ck._obs_gen == 2


def test_an_older_build_finishing_after_a_newer_one_never_rewinds_the_baseline(tmp_path: Path, monkeypatch) -> None:
    # codex/gemini L3 r1: overlapping builds complete out of order. X (older read_after) stalls, Y (newer) completes with
    # different etags and bumps; X then completes holding the stale etags: it neither overwrites Y's nor decides a bump
    c, state = _external_cockpit(tmp_path)
    base = time.monotonic() - 1000.0                         # X's read_after: far older than any read here
    c.manifest(NONE, read_after=base)                        # a baseline build
    real, gate, computed = c._manifest_compute, threading.Event(), threading.Event()

    def compute(credential, **kw):
        out = real(credential, **kw)
        if kw.get("read_after") == base + 1:                 # build X: reads the old rows, then stalls
            computed.set()
            assert gate.wait(10)
        return out

    monkeypatch.setattr(c, "_manifest_compute", compute)
    x_out: list = []
    x = threading.Thread(target=lambda: x_out.append(c.manifest(NONE, read_after=base + 1)))
    x.start()
    assert computed.wait(10)
    state["rows"] = [_row(1), _row(2)]
    gen0 = c._obs_gen
    try:
        y = c.manifest(NONE, read_after=time.monotonic())    # build Y: newer, begun after X read; other etags
        gen = c._obs_gen
        assert gen == gen0 + 1                               # Y announced
        y_etags = dict(c._obs_etags)
    finally:
        gate.set()
        x.join(10)
    assert x_out and not x.is_alive()
    assert x_out[0]["panels"]["od"]["etag"] != y["panels"]["od"]["etag"]    # X really held the stale etags
    assert c._obs_etags == y_etags
    assert c._obs_gen == gen
    c.manifest(NONE, read_after=time.monotonic())         # equal to Y: announces nothing
    assert c._obs_gen == gen


def test_a_build_with_no_panels_or_a_non_mapping_head_does_not_raise(ck, monkeypatch) -> None:
    for bad in ({"x": 1}, {"panels": None}, {"panels": {"a": None, "b": "x", "c": {"etag": "e"}}}):
        monkeypatch.setattr(ck, "_manifest_compute", lambda credential, _b=bad, **kw: _b)
        assert ck.manifest(NONE, read_after=time.monotonic()) is bad


def test_concurrent_manifests_share_one_bounded_pool_of_readers(tmp_path: Path, monkeypatch) -> None:
    # codex L3 r2: each cache-distinct manifest (one per device credential) started its own pool of _MANIFEST_POOL_THREADS
    from levain.cockpit import engine as eng

    monkeypatch.setattr(eng, "_MANIFEST_POOL_THREADS", 3)
    _r, src, _ids = _install(tmp_path)
    c = build_default_cockpit(src)          # cache off: every manifest computes
    real = c._snap_for
    peak = [0]
    lk = threading.Lock()

    def slow(spec, ctx, read_after=None):
        with lk:
            peak[0] = max(peak[0], sum(1 for t in threading.enumerate()
                                       if t.name.startswith("manifest-read") and t.name.split("_")[0] in ("manifest-read",
                                                                                                       "manifest-read-3")))
        time.sleep(0.05)
        return real(spec, ctx, read_after)

    monkeypatch.setattr(c, "_snap_for", slow)
    creds = [{"class": "token", "name": f"device-{i}", "device_class": None} for i in range(4)]
    done: dict[str, Any] = {}

    def run(cr: dict) -> None:
        try:
            done[cr["name"]] = c.manifest(cr)
        except BaseException as e:          # noqa: BLE001 - recorded, asserted below
            done[cr["name"]] = e

    ts = [threading.Thread(target=run, args=(cr,)) for cr in creds]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not any(t.is_alive() for t in ts)
    assert len(done) == 4 and all(isinstance(v, dict) for v in done.values()), done
    assert 0 < peak[0] <= 3


@pytest.mark.parametrize("ttl", [None, 0])
def test_nothing_is_shared_where_the_read_cache_is_off(ck, monkeypatch, ttl) -> None:
    ck.read_cache_ttl_s = ttl
    n = _provider_reads(monkeypatch, ck)
    ck.manifest(NONE)
    ck.panel(PID, credential_class="none")
    assert n[PID] == 2


def test_a_manifest_stuck_on_its_panels_does_not_hold_another_credentials_manifest(tmp_path: Path) -> None:
    # levain#138 L3 r3: pool tasks mostly wait on a panel's flight, so a shared pool too small for two manifests queued a
    # healthy device's manifest behind another device's hung panels
    _r, src, _ids = _install(tmp_path)
    stuck, healthy = build_default_cockpit(src), build_default_cockpit(src)
    release = threading.Event()
    parked = threading.Event()
    parked_n = [0]
    lock = threading.Lock()
    real = stuck._snap_for

    def hang(spec, ctx, read_after=None):
        with lock:
            parked_n[0] += 1
            if parked_n[0] >= 16:           # the old pool's whole 16-thread cap is held by this one manifest
                parked.set()
        assert release.wait(30)
        return real(spec, ctx, read_after)

    stuck._snap_for = hang                  # type: ignore[method-assign]
    out: dict[str, Any] = {}

    def run(name: str, ck: Cockpit, cred: dict) -> None:
        try:
            out[name] = ck.manifest(cred)
        except BaseException as e:          # noqa: BLE001 - recorded, asserted below
            out[name] = e

    ta = threading.Thread(target=run, args=("stuck", stuck, {**TOKEN, "name": "device-a"}))
    tb = threading.Thread(target=run, args=("healthy", healthy, {**TOKEN, "name": "device-b"}))
    try:
        ta.start()
        assert parked.wait(10), "the stuck manifest never parked enough panel reads to saturate the old 16-thread cap"
        tb.start()
        tb.join(10)
        assert not tb.is_alive() and "healthy" in out, "the healthy manifest queued behind the stuck one"
        assert isinstance(out["healthy"], dict)
    finally:
        release.set()
        ta.join(30)
        tb.join(30)
    assert isinstance(out["stuck"], dict) and not ta.is_alive()
