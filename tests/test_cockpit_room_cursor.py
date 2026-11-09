"""The room read's resume cursor: ``after=<gseq>`` returns only newer items, ``next`` chains the reads."""
from __future__ import annotations

import pytest

from levain.cockpit import room as cockpit_room
from tests.test_cockpit_room import World, _call, _serve_room
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

GET = cockpit_room.ROOM_PATH


@pytest.fixture
def w(qdir):
    return World(qdir)


def _send(base, w, n, first=1):
    for i in range(first, first + n):
        status, _ = _call(base, "/cockpit/room/send", w.body(f"line {i}", f"ob-{i:08d}"))
        assert status == 200


def _read(base, query=""):
    return _call(base, GET + query, method="GET")


def test_a_cold_read_carries_next_and_after_returns_only_newer(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 3)
        status, cold = _read(base)
        seqs = [i["gseq"] for i in cold["stream"]]
        assert status == 200 and len(seqs) == 3 and cold["next"] == max(seqs) and "reset" not in cold
        status, part = _read(base, f"?after={seqs[0]}")
        assert [i["gseq"] for i in part["stream"]] == seqs[1:] and part["next"] == seqs[-1]
        assert part["room_thread"] == w.thread and part["composer"] == cold["composer"]
        assert part["proposals"] == cold["proposals"] and part["model"] == cold["model"]
        status, none = _read(base, f"?after={seqs[-1]}")
        assert none["stream"] == [] and none["next"] == seqs[-1] and none["composer"] == cold["composer"]


def test_chained_reads_never_repeat_or_skip_a_stored_item(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        got, cursor = [], None
        for batch in (2, 1, 3):
            _send(base, w, batch, first=len(got) + 1 if not got else 10 * batch + 1)
            query = "" if cursor is None else f"?after={cursor}&thread={w.thread}"
            _status, body = _read(base, query)
            got += [i["gseq"] for i in body["stream"]]
            cursor = body["next"]
        _status, whole = _read(base)
        assert got == [i["gseq"] for i in whole["stream"]] and len(set(got)) == len(got)


def test_a_gap_from_the_filtered_open_probe_does_not_break_chaining(tmp_path, w, monkeypatch):
    real = cockpit_room._without_open_check
    hidden: set[int] = set()
    monkeypatch.setattr(cockpit_room, "_without_open_check",
                        lambda rows, rl=None: [r for r in real(rows, rl) if r.gseq not in hidden])
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 3)
        _s, cold = _read(base)
        seqs = [i["gseq"] for i in cold["stream"]]
        hidden.add(seqs[1])                                    # the middle one is a probe turn: a hole in the gseqs
        _s, a = _read(base, f"?after={seqs[0]}")
        assert [i["gseq"] for i in a["stream"]] == [seqs[2]] and a["next"] == seqs[2]
        _send(base, w, 1, first=4)
        _s, b = _read(base, f"?after={a['next']}")
        assert len(b["stream"]) == 1 and b["stream"][0]["gseq"] > seqs[2] and b["next"] == b["stream"][0]["gseq"]


def test_a_rotated_room_or_a_foreign_thread_is_a_cold_read_with_reset(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 2)
        _s, cold = _read(base)
        cursor, old = cold["next"], w.thread
        _s, same = _read(base, f"?after={cursor}&thread={old}")
        assert "reset" not in same
        _s, other = _read(base, f"?after={cursor}&thread=not-this-room")
        assert other["reset"] is True and [i["gseq"] for i in other["stream"]] == [i["gseq"] for i in cold["stream"]]
        new = w._rotate()
        _s, rot = _read(base, f"?after={cursor}&thread={old}")
        assert rot["reset"] is True and rot["room_thread"] == new and rot["stream"] == []
        assert rot["next"] is None


@pytest.mark.parametrize("bad", ["-1", "abc", "", "1.5", "1e3", "%2B1"])
def test_a_bad_cursor_is_a_400_with_a_reason(tmp_path, w, bad):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _read(base, f"?after={bad}")
        assert status == 400 and body["error"] == "bad_request" and body["message"]


def test_the_cursor_read_keeps_the_gate(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, _b = _call(base, GET + "?after=0", method="GET", token=None)
        assert status == 403
