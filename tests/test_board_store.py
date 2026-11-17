"""The board item store (levain board add|amend|decide|use): the writer is a port of flow's scripts/board.py, the store has
one pointer, one identity and one lock, and every record it writes folds cleanly through board_state.fold."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_cli
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
    d.mkdir()
    (d / bs.STORE_ID_NAME).write_text("x\n")
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


# --------------------------------------------------------------------------------------------------- fix round 1


ADD = ("add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b")


def _replace_with_copy(path: Path) -> None:
    """What a git checkout does: the same bytes under a new inode."""
    copy = path.with_name("items.copy")
    copy.write_bytes(path.read_bytes())
    os.replace(copy, path)


def test_a_file_replaced_before_the_write_is_reread_and_written_once(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    assert run(capsys, *ADD)[0] == 0
    items = store / "items.jsonl"
    real, calls = bs._read_fd, {"n": 0}

    def read_then_swap(fd):
        raw = real(fd)
        calls["n"] += 1
        if calls["n"] == 1:
            _replace_with_copy(items)           # the replacement arrives after the open and read, before the write
        return raw

    monkeypatch.setattr(bs, "_read_fd", read_then_swap)
    rc, out, err = run(capsys, *ADD)
    assert rc == 0 and out == "levain#2", err
    assert calls["n"] == 2
    assert [json.loads(x)["id"] for x in lines(store)] == ["levain#1", "levain#2"]


def test_a_store_replaced_before_every_write_refuses_and_writes_nothing(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    real = bs._read_fd
    monkeypatch.setattr(bs, "_read_fd", lambda fd: (_replace_with_copy(store / "items.jsonl"), real(fd))[1])
    rc, out, err = run(capsys, *ADD)
    assert rc == 2 and "was replaced" in err and "nothing was written" in err
    assert lines(store) == []


def test_a_file_replaced_after_the_write_refuses_and_appends_nothing_to_the_new_file(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    assert run(capsys, *ADD)[0] == 0
    items = store / "items.jsonl"
    real, calls = bs._write_all, {"n": 0}

    def write_then_swap(fd, payload):
        calls["n"] += 1
        real(fd, payload)
        _replace_with_copy(items)               # a copy that already holds the written line, or (for a checkout) none

    monkeypatch.setattr(bs, "_write_all", write_then_swap)
    rc, out, err = run(capsys, *ADD)
    assert rc == 2 and out == "" and calls["n"] == 1
    assert f"was replaced while levain#2 was being written" in err and 'levain board state' in err and "--key" in err
    assert [json.loads(x)["id"] for x in lines(store)] == ["levain#1", "levain#2"]      # the copy it got; nothing was appended to it


def test_a_file_replaced_after_the_write_by_one_without_the_record_gets_nothing_appended(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    assert run(capsys, *ADD)[0] == 0
    items = store / "items.jsonl"
    old = items.read_bytes()
    real = bs._write_all

    def write_then_swap_back(fd, payload):
        real(fd, payload)
        items.with_name("items.old").write_bytes(old)
        os.replace(items.with_name("items.old"), items)      # the checkout restored the file without our line

    monkeypatch.setattr(bs, "_write_all", write_then_swap_back)
    rc, out, err = run(capsys, *ADD)
    assert rc == 2 and "may or may not be in the new file" in err
    assert items.read_bytes() == old


def test_a_read_of_a_replaced_file_is_retried_once_then_refused(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    run(capsys, *ADD)
    target = bs.resolve_store()
    real, calls = bs._read_fd, {"n": 0}

    def swap_once(fd):
        raw = real(fd)
        calls["n"] += 1
        if calls["n"] == 1:
            _replace_with_copy(store / "items.jsonl")
        return raw

    monkeypatch.setattr(bs, "_read_fd", swap_once)
    assert len(bs.read_item_lines(target)) == 1 and calls["n"] == 2
    calls["n"] = -100                           # now every read is followed by a replacement
    monkeypatch.setattr(bs, "_read_fd", lambda fd: (_replace_with_copy(store / "items.jsonl"), real(fd))[1])
    with pytest.raises(bs.BoardError, match="was replaced while it was read"):
        bs.read_item_lines(target)


@pytest.mark.parametrize("damage", ["gone", "no-store-id"])
def test_a_dangling_pointer_refuses_every_reader_and_writer_and_recreates_nothing(home, tmp_path, capsys, damage):
    import shutil
    store = use(capsys, tmp_path / "s")
    if damage == "gone":
        shutil.rmtree(store)
    else:
        (store / "store.id").unlink()
    want = f"pointer names {store}, which holds no store (store.id missing); run levain board use"
    for verb in (("state", "--json"), ADD, ("decide", "--id", "levain#1", "--decision", "cut", "--words", "w", "--by", "b"),
                 ("amend", "--id", "levain#1", "--why", "w", "--by", "b", "--title", "x")):
        rc, out, err = run(capsys, *verb)
        assert rc == 2 and want in err, (verb, err)
    assert not store.exists() or not (store / "items.jsonl").exists()
    assert not (store / "store.id").exists()
    assert run(capsys, "use", str(store))[0] == 0                       # only `use` makes a store
    assert run(capsys, *ADD)[1] == "levain#1"


def test_an_explicit_file_must_be_called_items_jsonl(home, tmp_path, capsys):
    shadow = tmp_path / "shadow.jsonl"
    shadow.write_text("")
    want = "--items must name a store directory or its items.jsonl"
    rc, _, err = run(capsys, "state", "--json", "--items", str(shadow))
    assert rc == 2 and want in err
    store = use(capsys, tmp_path / "s")
    for verb in (("state", "--json"), ADD):
        rc, _, err = run(capsys, verb[0], "--items", str(shadow), *verb[1:])
        assert rc == 2 and want in err
    assert lines(store) == []


def test_a_symlinked_items_file_is_the_store_its_parent_is(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "real.jsonl").write_text("")
    (store / "items.jsonl").symlink_to(elsewhere / "real.jsonl")
    link_dir = tmp_path / "alias"
    link_dir.symlink_to(store)
    rc, out, err = run(capsys, "add", "--items", str(link_dir / "items.jsonl"), *ADD[1:])
    assert rc == 0 and out == "levain#1", err
    assert len((elsewhere / "real.jsonl").read_text("utf-8").splitlines()) == 1


@pytest.mark.parametrize("bad", ["", "relative/path\n", "/a\n/b\n", "DIR"])
def test_use_repairs_a_pointer_that_cannot_be_read(home, tmp_path, capsys, bad):
    (home / "board").mkdir(parents=True)
    pointer = home / "board" / "STORE"
    if bad == "DIR":
        pointer.mkdir()
    else:
        pointer.write_text(bad)
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2
    d = tmp_path / "s"
    store = use(capsys, d)
    assert pointer.read_text("utf-8") == f"{store}\n"
    assert run(capsys, *ADD)[1] == "levain#1"


def test_a_reader_does_not_create_store_lock(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    (store / "store.lock").unlink()
    assert run(capsys, "state", "--json")[0] in (0, 3)
    assert board_cli._store_id(store) is not None
    assert bs.known_ids(bs.resolve_store()) == set()
    assert not (store / "store.lock").exists()
    assert run(capsys, *ADD)[1] == "levain#1"           # a writer takes it
    assert (store / "store.lock").exists()


def test_short_writes_are_looped_to_the_end(home, tmp_path, capsys, monkeypatch):
    real = os.write
    monkeypatch.setattr(bs.os, "write", lambda fd, data: real(fd, bytes(data)[:1]))
    store = use(capsys, tmp_path / "s")
    assert len((store / "store.id").read_text("utf-8")) == 33
    assert (home / "board" / "STORE").read_text("utf-8") == f"{store}\n"


def test_a_failed_store_id_write_is_undone_and_no_pointer_is_written(home, tmp_path, capsys, monkeypatch):
    def boom(fd, data):
        raise OSError(28, "no space")

    monkeypatch.setattr(bs.os, "write", boom)
    with pytest.raises(bs.BoardError, match="cannot set up the store"):
        bs.use(tmp_path / "s")
    assert not (tmp_path / "s" / "store.id").exists() and not (home / "board" / "STORE").exists()


def test_a_failed_pointer_write_leaves_no_temp_file(home, tmp_path, capsys, monkeypatch):
    real = os.write
    store = tmp_path / "s"
    use(capsys, store)
    (home / "board" / "STORE").unlink()

    def boom(fd, data):
        if bytes(data).startswith(str(store).encode()):
            raise OSError(28, "no space")
        return real(fd, data)

    monkeypatch.setattr(bs.os, "write", boom)
    with pytest.raises(bs.BoardError, match="cannot write the store pointer"):
        bs.use(store)
    assert [p.name for p in (home / "board").iterdir() if p.name.startswith(".STORE")] == []
    assert not (home / "board" / "STORE").exists()


def test_the_pointer_round_trips_a_path_with_edge_spaces_and_refuses_a_newline(home, tmp_path, capsys):
    odd = tmp_path / " spaced "
    store = use(capsys, odd)
    assert bs.read_pointer() == store and str(store).endswith(" spaced ")
    assert run(capsys, *ADD)[1] == "levain#1"
    rc, _, err = run(capsys, "use", str(tmp_path / "a\nb"))
    assert rc == 2 and "newline" in err and bs.read_pointer() == store


def test_the_directory_is_fsynced_after_a_created_entry(home, tmp_path, capsys, monkeypatch):
    import stat
    real, dirs = os.fsync, []

    def spy(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dirs.append(os.fstat(fd).st_ino)
        return real(fd)

    monkeypatch.setattr(bs.os, "fsync", spy)
    store = use(capsys, tmp_path / "s")
    assert os.stat(store).st_ino in dirs and os.stat(home / "board").st_ino in dirs       # store.id, then the pointer
    dirs.clear()
    assert run(capsys, *ADD)[0] == 0
    assert dirs == [os.stat(store).st_ino]                                                 # items.jsonl was created
    dirs.clear()
    run(capsys, *ADD)
    assert dirs == []                                                                      # nothing new was created


def test_an_empty_items_argument_is_not_given(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    assert run(capsys, "add", "--items", "", *ADD[1:])[1] == "levain#1"
    assert run(capsys, "state", "--json", "--items", "")[0] in (0, 3)
    assert len(lines(store)) == 1


def test_a_key_used_by_another_repo_refuses_naming_both(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    base = ("--title", "t", "--origin", "o", "--by", "b", "--key", "k1", "--payload-hash", HASH_A)
    assert run(capsys, "add", "--repo", "levain", *base)[1] == "levain#1"
    assert run(capsys, "add", "--repo", "levain", *base)[1] == "levain#1"
    rc, out, err = run(capsys, "add", "--repo", "flow", *base)
    assert rc == 2 and "levain#1" in err and "'levain'" in err and "'flow'" in err
    assert len(lines(store)) == 1


def test_an_unreadable_store_does_not_abort_an_add_through_the_depends_warning(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    real = bs.known_ids

    def boom(target):
        raise bs.BoardError("listing failed")

    monkeypatch.setattr(bs, "known_ids", boom)
    rc, out, err = run(capsys, *ADD, "--depends", "item:levain#9")
    assert rc == 0 and out == "levain#1" and "could not check depends_on" in err and "listing failed" in err
    monkeypatch.setattr(bs, "known_ids", real)


def test_charter_check_prints_the_stores_own_refusal(home, tmp_path, capsys):
    from levain.desk import charter
    use(capsys, tmp_path / "mine")
    other = tmp_path / "theirs"
    other.mkdir()
    (other / "items.jsonl").write_text("")
    items, why = charter._store(other / "items.jsonl")
    assert items is None and "two stores" in why and "BoardError" not in why


# --------------------------------------------------------------------------------------------------- fix round 2


def _dir_fsync_fails(monkeypatch, only):
    real = bs._fsync_dir

    def flaky(path):
        if Path(path) == only:
            raise OSError(5, "io error")
        return real(path)

    monkeypatch.setattr(bs, "_fsync_dir", flaky)


def test_a_failed_directory_fsync_after_a_durable_write_is_a_warning(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    _dir_fsync_fails(monkeypatch, store)
    rc, out, err = run(capsys, *ADD)                       # items.jsonl is created here: the directory fsync runs and fails
    assert rc == 0 and out == "levain#1"
    assert str(store) in err and "io error" in err and err.count("\n") == 0
    assert [json.loads(x)["id"] for x in lines(store)] == ["levain#1"]


def test_a_failed_directory_fsync_after_the_pointer_is_replaced_is_a_warning(home, tmp_path, capsys, monkeypatch):
    store = tmp_path / "s"
    _dir_fsync_fails(monkeypatch, home / "board")
    rc, out, err = run(capsys, "use", str(store))
    assert rc == 0 and str(home / "board") in err and "io error" in err
    assert bs.read_pointer() == Path(os.path.realpath(store))


@pytest.mark.parametrize("damage", ["gone", "no-store-id"])
def test_a_write_never_recreates_a_store_that_vanished_before_the_lock(home, tmp_path, capsys, damage):
    import shutil
    store = use(capsys, tmp_path / "s")
    target = bs.resolve_store()
    if damage == "gone":
        shutil.rmtree(store)
    else:
        (store / "store.id").unlink()
    with pytest.raises(bs.BoardError, match="holds no store"):
        bs.add(target, "levain", "t", "o", "b")
    assert not store.exists() or sorted(p.name for p in store.iterdir()) in ([], ["store.lock"])
    assert not (store / "items.jsonl").exists() and not (store / "store.id").exists()


def test_a_shared_reader_opens_the_lock_without_creating_it(home, tmp_path, capsys, monkeypatch):
    store = use(capsys, tmp_path / "s")
    real, seen = os.open, []

    def spy(path, flags, *a, **k):
        if str(path).endswith(bs.LOCK_NAME):
            seen.append(flags)
        return real(path, flags, *a, **k)

    monkeypatch.setattr(bs.os, "open", spy)
    with bs.store_lock(store, exclusive=False):
        pass
    assert seen and all(not f & os.O_CREAT for f in seen)
    (store / bs.LOCK_NAME).unlink()
    seen.clear()
    with bs.store_lock(store, exclusive=False):
        pass
    assert not (store / bs.LOCK_NAME).exists()


def test_a_write_into_a_zero_length_existing_file_fsyncs_the_directory(home, tmp_path, capsys, monkeypatch):
    import stat
    store = use(capsys, tmp_path / "s")
    (store / "items.jsonl").write_text("")
    real, dirs = os.fsync, []

    def spy(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dirs.append(os.fstat(fd).st_ino)
        return real(fd)

    monkeypatch.setattr(bs.os, "fsync", spy)
    assert run(capsys, *ADD)[0] == 0
    assert dirs == [os.stat(store).st_ino]
    dirs.clear()
    run(capsys, *ADD)
    assert dirs == []


def test_an_invalid_depends_is_a_refusal_printed_once_and_nothing_is_written(home, tmp_path, capsys):
    store = use(capsys, tmp_path / "s")
    rc, out, err = run(capsys, *ADD, "--depends", "bogus:x")
    assert rc == 2 and err.count("refused") == 1 and "could not check" not in err and out == ""
    assert lines(store) == []
    assert run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b", "--depends", "item:levain#9")[0] == 0
    rc, out, err = run(capsys, "amend", "--id", "levain#1", "--why", "w", "--by", "b", "--depends", "bogus:x")
    assert rc == 2 and err.count("refused") == 1 and "could not check" not in err


@pytest.mark.parametrize("name", ["nope", "nope.jsonl", "nope.txt"])
def test_a_missing_explicit_path_gets_one_neutral_refusal(home, tmp_path, capsys, name):
    missing = tmp_path / name
    for verb in (("state", "--json"), ADD):
        rc, _, err = run(capsys, verb[0], "--items", str(missing), *verb[1:])
        assert rc == 2 and f"--items {missing}: no such file or directory" in err and "must name a store" not in err


@pytest.mark.parametrize("damage", ["gone", "no-store-id"])
def test_a_shared_reader_of_a_vanished_store_refuses_instead_of_reading_empty(home, tmp_path, capsys, damage):
    import shutil
    store = use(capsys, tmp_path / "s")
    target = bs.resolve_store()
    (store / bs.LOCK_NAME).unlink()
    if damage == "gone":
        shutil.rmtree(store)
    else:
        (store / "store.id").unlink()
    with pytest.raises(bs.BoardError, match="holds no store"):
        with bs.store_lock(store, exclusive=False):
            pass
    with pytest.raises(bs.BoardError, match="holds no store"):
        bs.read_item_lines(target)


def test_state_reads_store_id_under_the_same_shared_hold_as_the_items(home, tmp_path, capsys, monkeypatch):
    """Deterministic interleaving: at the moment `state` reads store.id, a writer (`use`) must still be locked out."""
    import fcntl
    store = use(capsys, tmp_path / "s")
    seen = []
    real = board_cli._store_id

    def spy(store_dir):
        fd = os.open(store_dir / bs.LOCK_NAME, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("writer got in")
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                seen.append("locked out")
        finally:
            os.close(fd)
        return real(store_dir)

    monkeypatch.setattr(board_cli, "_store_id", spy)
    run(capsys, "state", "--json")
    assert seen == ["locked out"]


def test_a_failed_directory_fsync_after_a_new_store_id_is_a_warning(home, tmp_path, capsys, monkeypatch):
    store = tmp_path / "s"
    _dir_fsync_fails(monkeypatch, store)
    rc, out, err = run(capsys, "use", str(store))
    assert rc == 0 and "io error" in err and str(store) in err
    assert (store / "store.id").read_text().strip()


def test_same_file_needs_device_and_inode_to_match(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_text("x")
    fd = os.open(p, os.O_RDONLY)
    try:
        assert bs._same_file(fd, p)
        real = os.stat(p)
        other_dev = os.stat_result((real.st_mode, real.st_ino, real.st_dev + 1) + tuple(real)[3:])
        monkeypatch.setattr(bs.os, "stat", lambda *_a, **_k: other_dev)
        assert other_dev.st_ino == os.fstat(fd).st_ino
        assert not bs._same_file(fd, p)
    finally:
        os.close(fd)
