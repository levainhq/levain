"""The board item store (levain board add|amend|decide|use): the writer is a port of flow's scripts/board.py, the store has
one pointer, one identity and one lock, and every record it writes folds cleanly through board_state.fold."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_store as bs
from levain.desk.board_state import fold

FIXTURE = Path(__file__).with_name("board_store_flow_records.jsonl")     # flow's board.py output for the commands below
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "lhome"
    monkeypatch.setenv("LEVAIN_HOME", str(h))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    return h


def run(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def use(capsys, d: Path):
    rc, out, err = run(capsys, "use", str(d))
    assert rc == 0, err
    return Path(os.path.realpath(d))


def lines(d: Path) -> list[str]:
    p = d / "items.jsonl"
    return p.read_text("utf-8").splitlines() if p.exists() else []


def sans_at(line: str) -> dict:
    rec = json.loads(line)
    rec.pop("at")
    return rec


FLOW_CALLS = [
    ("add", "--repo", "levain", "--title", "Port me", "--origin", "desk 17", "--words", "his words  verbatim — é",
     "--done", "docs/x.md", "--done-kind", "document", "--tier", "opus", "--depends", "item:levain#1",
     "--depends", "gate:2026-12-01:wait for it", "--by", "levain 22"),
    ("add", "--repo", "levain", "--title", "Second", "--origin", "desk 17", "--by", "levain 22"),
    ("amend", "--id", "levain#1", "--why", "re-scope", "--by", "levain 22", "--title", "Port me 2", "--clear", "tier",
     "--depends", "band:np-1a2b3c4d"),
    ("decide", "--id", "levain#2", "--decision", "parked", "--words", "later, ok", "--by", "levain 22", "--date", "2026-11-17"),
]


def test_port_writes_the_records_flow_wrote_byte_for_byte_but_the_stamp(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "store")
    for call in FLOW_CALLS:
        rc, _, err = run(capsys, *call)
        assert rc == 0, err
    ours, theirs = lines(store), FIXTURE.read_text("utf-8").splitlines()
    assert len(ours) == len(theirs) == 4
    for a, b in zip(ours, theirs):
        assert sans_at(a) == sans_at(b)
        # the text after the stamp is identical too: same key order, same separators, same escapes
        assert a.split('"at":"', 1)[0] == b.split('"at":"', 1)[0]
        assert a.split('"by":', 1)[1] == b.split('"by":', 1)[1]
        assert a.isascii()


def test_every_verb_folds_cleanly(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "store")
    for call in FLOW_CALLS:
        assert run(capsys, *call)[0] == 0
    run(capsys, "decide", "--id", "levain#2", "--decision", "reopen", "--words", "back", "--by", "x")
    run(capsys, "add", "--repo", "flow", "--title", "k", "--origin", "o", "--by", "x", "--key", "k1", "--payload-hash", HASH_A)
    folded = fold(lines(store))
    assert not folded.torn and not folded.problems
    assert set(folded.items) == {"levain#1", "levain#2", "flow#1"}
    assert folded.items["levain#1"].title == "Port me 2"


def test_flow_refusals_are_kept(home, tmp_path, capsys):
    use(capsys, tmp_path / "store")
    base = ["add", "--repo", "levain", "--origin", "o", "--by", "b"]
    assert run(capsys, *base, "--title", "t", "--done-kind", "document", "--done", "has space")[0] == 2
    assert run(capsys, *base, "--title", "   ")[0] == 2
    assert run(capsys, *base, "--title", "t\tx")[0] == 2
    assert run(capsys, "amend", "--id", "levain#9", "--why", "w", "--by", "b", "--title", "x")[0] == 2
    assert run(capsys, "decide", "--id", "levain#9", "--decision", "cut", "--words", "w", "--by", "b")[0] == 2
    assert run(capsys, "decide", "--id", "levain#9", "--decision", "cut", "--words", "w", "--by", "b", "--date", "2026-02-30")[0] == 2
    run(capsys, *base, "--title", "t")
    rc, _, err = run(capsys, "amend", "--id", "levain#1", "--why", "w", "--by", "b", "--tier", "opus", "--clear", "tier")
    assert rc == 2 and "name the same field" in err


def test_add_refuses_until_the_pointer_exists(home, tmp_path, capsys):
    rc, out, err = run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    assert rc == 2 and out == "" and bs.NO_STORE in err
    explicit = tmp_path / "x" / "items.jsonl"
    rc, _, err = run(capsys, "add", "--items", str(explicit), "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    assert rc == 2 and bs.NO_STORE in err and not explicit.exists()
    store = use(capsys, tmp_path / "s")
    rc, out, _ = run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    assert rc == 0 and out == "levain#1" and len(lines(store)) == 1


def test_use_writes_store_id_once_and_the_pointer_last_and_is_idempotent(home, tmp_path, capsys):
    d = tmp_path / "store"
    store = use(capsys, d)
    ident = (store / "store.id").read_text("utf-8")
    assert len(ident.strip()) == 32
    assert (home / "board" / "STORE").read_text("utf-8") == f"{store}\n"
    run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    mtime = (home / "board" / "STORE").stat().st_mtime_ns
    rc, out, _ = run(capsys, "use", str(d))
    assert rc == 0 and "already using" in out
    assert (store / "store.id").read_text("utf-8") == ident
    assert (home / "board" / "STORE").stat().st_mtime_ns == mtime
    other = use(capsys, tmp_path / "other")                               # a second store has its own identity
    assert (other / "store.id").read_text("utf-8") != ident
    assert not [p for p in (home / "board").iterdir() if p.name.startswith(".STORE")]


def test_pointer_is_not_written_when_the_store_cannot_be_set_up(home, tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rc, _, err = run(capsys, "use", str(blocker / "under"))
    assert rc == 2 and "refused" in err
    assert not (home / "board" / "STORE").exists()


@pytest.mark.parametrize("form", ["file", "dir"])
def test_two_stores_refuses_an_explicit_path_elsewhere(home, tmp_path, capsys, form, monkeypatch):
    store = use(capsys, tmp_path / "mine")
    elsewhere = tmp_path / "theirs"
    elsewhere.mkdir()
    (elsewhere / "items.jsonl").write_text("")
    given = elsewhere / "items.jsonl" if form == "file" else elsewhere
    want = f"two stores: --items names {os.path.realpath(elsewhere)}, this install's store is {store} (levain board use)"
    for verb in (("state", "--json"), ("add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b"),
                 ("decide", "--id", "levain#1", "--decision", "cut", "--words", "w", "--by", "b")):
        rc, out, err = run(capsys, verb[0], "--items", str(given), *verb[1:])
        assert rc == 2 and want in err, (verb, err)
    monkeypatch.setenv("LEVAIN_BOARD_ITEMS", str(given))
    rc, _, err = run(capsys, "state", "--json")
    assert rc == 2 and want in err
    # the agreeing spellings pass, file or directory
    monkeypatch.setenv("LEVAIN_BOARD_ITEMS", str(store / "items.jsonl" if form == "file" else store))
    assert run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")[2] == ""


def test_state_reads_through_the_pointer(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, out, err = run(capsys, "state", "--json", "--repo", str(repo), "--repo-name", "levain")
    assert rc in (0, 3), err
    assert [i["id"] for i in json.loads(out)["items"]] == ["levain#1"]
    assert (store / "store.lock").exists()


def test_state_on_a_fresh_pointered_store_is_an_empty_board(home, tmp_path, capsys):
    use(capsys, tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, out, _ = run(capsys, "state", "--json", "--repo", str(repo), "--repo-name", "levain")
    assert rc in (0, 3) and json.loads(out)["items"] == []


def test_key_same_hash_returns_the_id_different_hash_refuses(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    args = ["add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b", "--key", "k1"]
    rc, out, _ = run(capsys, *args, "--payload-hash", HASH_A)
    assert (rc, out) == (0, "levain#1")
    before = (store / "items.jsonl").read_bytes()
    rc, out, _ = run(capsys, *args, "--payload-hash", HASH_A)
    assert (rc, out) == (0, "levain#1") and (store / "items.jsonl").read_bytes() == before
    rc, out, err = run(capsys, *args, "--payload-hash", HASH_B)
    assert rc == 2 and out == "" and HASH_A in err and HASH_B in err
    assert (store / "items.jsonl").read_bytes() == before
    rc, out, _ = run(capsys, *args[:-2], "--key", "k2", "--payload-hash", HASH_B)
    assert out == "levain#2"
    assert run(capsys, *args, "--payload-hash", "nothex")[0] == 2
    assert run(capsys, *args)[0] == 2                                       # a key with no hash
    rec = json.loads(lines(store)[0])
    assert rec["key"] == "k1" and rec["payload_hash"] == HASH_A


def test_a_torn_store_refuses_writes(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")
    with open(store / "items.jsonl", "ab") as fh:
        fh.write(b'{"v":1,"op":"add","id":"levain#2"')
    before = (store / "items.jsonl").read_bytes()
    rc, _, err = run(capsys, "add", "--repo", "levain", "--title", "t2", "--origin", "o", "--by", "b")
    assert rc == 2 and "torn" in err and (store / "items.jsonl").read_bytes() == before


def _worker(store: str, tag: str, n: int) -> None:
    target = bs.Target(Path(store), Path(store) / "items.jsonl", True)
    for i in range(n):
        bs.add(target, "levain", f"{tag} {i}", "o", tag)


def test_two_processes_appending_lose_no_line_and_take_no_id_twice(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(str(store), tag, 15)) for tag in ("p", "q")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    recs = [json.loads(x) for x in lines(store)]
    assert len(recs) == 30
    ids = [r["id"] for r in recs]
    assert len(set(ids)) == 30 and set(ids) == {f"levain#{n}" for n in range(1, 31)}
    assert not fold(lines(store)).torn


def test_the_inode_recheck_retries_then_refuses(tmp_path, monkeypatch):
    f = tmp_path / "x"
    f.write_text("")
    real = os.stat
    calls = {"n": 0}

    def swapped(path, *a, **k):
        calls["n"] += 1
        st = real(path, *a, **k)
        return os.stat_result((st.st_mode, st.st_ino + 1, *st[2:]))

    monkeypatch.setattr(bs.os, "stat", swapped)
    with pytest.raises(bs.BoardError, match="keeps being replaced"):
        bs._open_checked(f, os.O_RDONLY)
    assert calls["n"] == bs.OPEN_RETRIES


def test_store_lock_is_exclusive_against_a_shared_holder(tmp_path):
    import fcntl
    d = tmp_path / "s"
    with bs.store_lock(d, exclusive=True):
        fd = os.open(d / bs.LOCK_NAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    with bs.store_lock(d, exclusive=False):
        with bs.store_lock(d, exclusive=False):
            pass


def test_explicit_items_with_no_pointer_still_reads(home, tmp_path, capsys):
    p = tmp_path / "items.jsonl"
    p.write_text(FIXTURE.read_text("utf-8"))
    repo = tmp_path / "repo"
    repo.mkdir()
    for given in (p, tmp_path):
        rc, out, err = run(capsys, "state", "--json", "--items", str(given), "--repo", str(repo), "--repo-name", "levain")
        assert rc in (0, 3), err
        assert len(json.loads(out)["items"]) == 2
    assert not (tmp_path / bs.LOCK_NAME).exists()          # a store nobody set up is not given a lock file
