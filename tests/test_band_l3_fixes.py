"""10-29 L3 (62b45ef6..680fee95) codex MEDs on levain/band_view.py: the empty transition, the Stop claim, the empty-view
dedupe, and the reprint check (which keeps no tail rule)."""
from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from levain import band_view
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


def ask(store, what="Which retry policy?"):
    return store.file(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
                      blocks="the feed PR", cwd="/tmp/proj")


def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def launch_rows(monkeypatch):
    monkeypatch.setattr(band_view, "is_autonomous", lambda *_a, **_k: False)


def deliver(sid, e):
    text, commit = band_view.hook_prompt({"session_id": sid, "prompt": "hi"}, e)
    if text:                                   # the installed hook commits only what it emitted
        commit()
    return text


def test_item_closed_and_notice_consumed_by_another_seat_still_ends_the_old_view(qdir, store, monkeypatch):
    launch_rows(monkeypatch)
    e = env(qdir)
    q = ask(store)
    assert "1." in deliver("A", e) and "1." in deliver("B", e)
    store.answer(q.id, option="fix", channel="cli")
    assert "closed 1" in deliver("B", e)            # B consumes the global notice
    out = deliver("A", e)                           # A's render is now empty: it must still be told, and commit
    assert out.startswith("NEEDS YOU · 0 open") and "closed 1" in out
    view, _gen, _blk = band_view._view_row(store, "claude_code:A")
    assert view.open_list == ()
    assert band_view.hook_stop({"session_id": "A", "last_assistant_message": "no band"}, e) is None
    assert deliver("A", e) == ""                    # and no row/line per further empty prompt


def _claim_proc(args):
    d, viewer, gen, start = args
    while time.time() < start:
        pass
    return band_view._claim_retry(d, viewer, gen)


def test_concurrent_claimants_for_one_view_exactly_one_wins_threads(qdir):
    results = []
    barrier = threading.Barrier(16)

    def go():
        barrier.wait()
        results.append(band_view._claim_retry(qdir, "claude_code:A", 7))

    ts = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1


def test_concurrent_claimants_for_one_view_exactly_one_wins_processes(qdir):
    start = time.time() + 1.0
    with multiprocessing.get_context("spawn").Pool(6) as pool:
        got = pool.map(_claim_proc, [(str(qdir), "claude_code:A", 9, start)] * 6)
    assert got.count(True) == 1


def test_an_old_generation_claimant_cannot_overwrite_a_newer_claim(qdir):
    assert band_view._claim_retry(qdir, "v", 10) is True
    assert band_view._claim_retry(qdir, "v", 5) is True          # a different view: its own claim
    assert band_view._claim_retry(qdir, "v", 10) is False        # the newer claim is intact
    assert band_view._claimed(qdir, "v", 10)


def test_a_fresh_store_without_the_claim_parent_still_claims(tmp_path):
    assert band_view._claim_retry(tmp_path / "a" / "b" / "consent", "v", 1) is True


def test_an_empty_view_is_still_recorded_when_a_nonempty_one_commits_between_read_and_write(qdir, store):
    ask(store)
    from levain.consent.questions import QuestionStore as _QS
    band_view.render_full(store)                    # numbers it
    band_view.record_view(store, "v", 0, [], gen=1)  # first empty view
    real = _QS._write_by
    state = {"done": False}

    def interleaved(self, deadline):
        if not state["done"]:
            state["done"] = True
            band_view.record_view(store, "v", 1, [1], gen=2)     # another render commits a nonempty view first
        return real(self, deadline)

    _QS._write_by = interleaved
    try:
        band_view.record_view(store, "v", 1, [], gen=3)           # the delivered empty render
    finally:
        _QS._write_by = real
    conn = sqlite3.connect(str(store.path) if hasattr(store, "path") else str(next(qdir.glob("*.db"))))
    try:
        gens = [r[0] for r in conn.execute("SELECT gen FROM band_views WHERE viewer = 'v' ORDER BY gen")]
    finally:
        conn.close()
    assert gens == [1, 2, 3]
    assert band_view.last_view(store, "v").open_list == ()


def test_reprint_followed_by_a_trailing_blank_line_or_text_passes(qdir, store, monkeypatch):
    launch_rows(monkeypatch)
    e = env(qdir)
    ask(store)
    band = deliver("s1", e)
    assert band_view.hook_stop({"session_id": "s1", "last_assistant_message": f"done\n\n{band}\n\n"}, e) is None
    deliver("s3", e)
    assert band_view.hook_stop({"session_id": "s3", "last_assistant_message": f"{band}\nand more text"}, e) is None


def test_legacy_view_whose_last_item_has_sublines_passes():
    view = SimpleNamespace(open_list=(1, 2), seen_through=2)
    msg = "band@2\n1. a\n2. b\n    a) opt\n    ! not read\n"
    assert band_view._reprint_check(view, None, msg) == ([], False)


def test_legacy_reprint_in_a_different_item_order_passes():
    view = SimpleNamespace(open_list=(1, 2), seen_through=2)
    assert band_view._reprint_check(view, None, "band@2\n2. b\n1. a") == ([], False)


def test_claim_of_a_newer_generation_never_frees_an_older_one(qdir):
    assert band_view._claim_retry(qdir, "v", 7) is True
    assert band_view._claim_retry(qdir, "v", 8) is True
    assert band_view._claim_retry(qdir, "v", 7) is False          # no inline pruning: gen 7's claim still stands
