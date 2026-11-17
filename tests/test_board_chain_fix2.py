"""P1b fix 2: the class "code that re-parses raw lines after the fold judged them". Every consumer takes what the fold produced
(records, keys, per-line classification); a chain ack clears only the chain break; the writer never appends an ack its own parser
rejects; the fold says what it dropped and what it kept. Each case was run once by hand on a scratch store first."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_chain as bc
from levain.desk import board_registry as br
from levain.desk import board_store as bs
from levain.desk import charter
from levain.desk import repos_fold as rf
from levain.desk.board_state import fold

ADD = ("add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b", "--author-kind", "agent")
ACK = ("--by", "me", "--author-kind", "operator")
WHOLE = {"v": 1, "op": "add", "at": "2026-11-17T00:00:00Z", "by": "b", "repo": "levain", "title": "t", "origin": "o"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _registry_not_under_test(monkeypatch):
    monkeypatch.setattr(bs, "_check_repo_mode", lambda store_dir, repo, key: None)


@pytest.fixture
def store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    assert main(["board", "use", str(tmp_path / "s")]) == 0
    capsys.readouterr()
    return Path(os.path.realpath(tmp_path / "s"))


def run(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def add(capsys, n=1):
    for _ in range(n):
        assert run(capsys, *ADD)[0] == 0


def raw_lines(store: Path) -> list[bytes]:
    return (store / "items.jsonl").read_bytes().split(b"\n")[:-1]


def folded(store: Path):
    return fold([x.decode() for x in raw_lines(store)])


def append(store: Path, payload: bytes) -> None:
    with open(store / "items.jsonl", "ab") as fh:
        fh.write(payload + b"\n")


def chained(store: Path, rec: dict, prev: str | None = None) -> bytes:
    """``rec`` chained onto the file's last line (or onto ``prev``, which may be a lie)."""
    return bs.canonical_bytes(bs._with_prev(dict(rec), bs.chain_prev((store / "items.jsonl").read_bytes()) if prev is None else prev))


def hand_ack(store: Path, line: int, digest: str, **extra) -> bytes:
    rec = {"v": 1, "op": "ack-torn", "at": "2026-11-17T00:00:00Z", "by": "me", "line": line, "sha256": digest, "why": "w", **extra}
    return chained(store, rec)


# ------------------------------------------------------------------------------------------------ 1: the re-parse class


@pytest.mark.parametrize("bad_id", [None, ["levain#2"], {"a": 1}, 7])
def test_an_acked_malformed_add_does_not_crash_the_registry_reader(store, capsys, bad_id):
    add(capsys)
    rec = {**WHOLE}
    if bad_id is not None:
        rec["id"] = bad_id
    line = chained(store, rec)
    append(store, line)
    append(store, hand_ack(store, 2, sha(line)))                                  # a torn-style ack: the fold drops line 2
    f = folded(store)
    assert not f.torn and f.dropped == ((2, 3),) and list(f.items) == ["levain#1"]
    adds, decision = br._items_facts(store)                                       # used to raise KeyError / TypeError on line 2
    assert list(adds) == ["levain#1"] and decision == {"levain#1": None}
    assert [r["id"] for r in bs._records_from((store / "items.jsonl").read_bytes())] == ["levain#1"]


def test_no_consumer_reparses_a_raw_store_line_after_the_fold():
    """The class, pinned: the three consumers that used to ``json.loads`` raw store lines no longer do."""
    root = Path(bc.__file__).parent
    for name, token in (("board_registry.py", "def _items_facts"), ("charter.py", "def _store"), ("board_store.py", "def _records_from")):
        src = (root / name).read_text()
        body = src[src.index(token):]
        body = body[:body.index("\ndef ", 10)]
        assert "json.loads" not in body, name


def test_charter_reads_the_key_from_the_fold_and_survives_an_acked_malformed_add(store, capsys):
    rec = {**WHOLE, "id": "levain#1", "key": "M:a"}
    first = chained(store, rec) if (store / "items.jsonl").exists() else bs.canonical_bytes(bs._with_prev(dict(rec), sha(b"")))
    (store / "items.jsonl").write_bytes(first + b"\n")
    junk = chained(store, {**WHOLE, "id": ["x"], "key": "M:z"})
    append(store, junk)
    append(store, hand_ack(store, 2, sha(junk)))
    items, why = charter._store(store / "items.jsonl")
    assert why == "" and items == {"levain#1": ("levain", None, "M:a")}


def test_charter_store_catches_recursion_in_the_fold(monkeypatch, tmp_path):
    from levain.desk import board_state
    def boom(lines):
        raise RecursionError("deep")
    monkeypatch.setattr(board_state, "fold", boom)
    p = tmp_path / "items.jsonl"
    p.write_text("")
    items, why = charter._store(p)
    assert items is None and "RecursionError" in why


# ------------------------------------------------------------------------------------------------ 2: a chain ack clears ONLY the chain break


def _broken_malformed(store: Path) -> bytes:
    """Line 3: an unknown op AND a prev that does not match line 2."""
    line = bs.canonical_bytes({**WHOLE, "op": "frobnicate", "id": "levain#3", "prev": "0" * 64})
    append(store, line)
    return line


def test_a_chain_ack_leaves_a_line_that_is_also_malformed_torn_until_a_torn_style_ack(store, capsys):
    add(capsys, 2)
    line = _broken_malformed(store)
    f = folded(store)
    assert f.torn and f.malformed == (3,) and [n for n, _ in f.torn_lines] == [3]
    append(store, hand_ack(store, 3, sha(line), want="0" * 64, have=sha(raw_lines(store)[1])))     # the chain ack, exactly the break
    f = folded(store)
    assert f.torn and f.acked == () and f.kept == () and f.dropped == () and "torn for itself" in f.problems[0]
    rc, _, err = run(capsys, *ADD)                                                                   # the writer agrees with the fold
    assert rc == 2 and "torn line (3)" in err and "frobnicate" in err
    append(store, hand_ack(store, 3, sha(line)))                                                     # the torn-style ack drops it
    f = folded(store)
    assert not f.torn and f.dropped == ((3, 5),) and f.kept == () and list(f.items) == ["levain#1", "levain#2"]
    assert run(capsys, *ADD)[0] == 0


def test_the_writer_acks_a_broken_malformed_line_torn_style_and_the_store_reads_whole(store, capsys):
    add(capsys, 2)
    _broken_malformed(store)
    rc, out, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "hand written", *ACK)
    assert rc == 0, err
    ack = json.loads(raw_lines(store)[-1])
    assert "want" not in ack and "have" not in ack and ack["line"] == 3
    f = folded(store)
    assert not f.torn and f.dropped == ((3, 4),) and bs._records_from((store / "items.jsonl").read_bytes())


def test_the_repos_fold_does_the_same(tmp_path):
    reg = {"v": rf.SCHEMA, "op": "register", "repo": "a", "at": "2026-11-17T00:00:00Z", "by": "b", "words": "", "path": "/x",
           "origin": "", "root": ["abc"], "author": {"kind": "agent"}, "grade": "asserted"}
    good = bs.canonical_bytes(bs._with_prev({**reg}, sha(b"")))
    bad = bs.canonical_bytes({**reg, "op": "frobnicate", "repo": "b", "prev": "0" * 64})
    lines = [good.decode(), bad.decode()]
    def ack(line, digest, **extra):
        rec = {"v": rf.SCHEMA, "op": rf.ACK, "at": "2026-11-17T00:00:00Z", "by": "me", "line": line, "sha256": digest, "why": "w", **extra}
        return bs.canonical_bytes(bs._with_prev(rec, sha("\n".join(lines).encode() + b"\n" if False else lines[-1].encode()))).decode()
    lines.append(ack(2, sha(bad), want="0" * 64, have=sha(good)))
    f = rf.fold_repos(lines)
    assert f.torn and f.malformed == (2,) and f.kept == () and f.dropped == ()
    lines.append(ack(2, sha(bad)))
    f = rf.fold_repos(lines)
    assert not f.torn and f.dropped == ((2, 4),) and f.kept == ()


# ------------------------------------------------------------------------------------------------ 3: the writer never appends an ack its parser rejects


def test_a_prev_that_is_not_a_hash_is_acked_torn_style_and_the_ack_parses(store, capsys):
    add(capsys)
    line = chained(store, {**WHOLE, "id": "levain#2"}, prev="not-a-hash")
    append(store, line)
    f = folded(store)
    assert f.torn and f.malformed == (2,) and bc.PREV_BAD in f.torn_why
    rc, _, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "w", *ACK)
    assert rc == 0, err
    last = json.loads(raw_lines(store)[-1])
    assert bc.parse_ack(last).chain is False and not folded(store).torn


def test_ack_body_refuses_a_chain_ack_its_own_parser_would_reject(tmp_path):
    parts = [b'{"a":1}']
    brk = bc.ChainBreak(1, "x", "ZZ", "a" * 64)                                    # a want that is not 64 lowercase hex
    with pytest.raises(bs.BoardError) as e:
        br._ack_body(parts, 1, "why", brk, "items.jsonl", None)
    assert "cannot be acknowledged as a chain break" in str(e.value) and "torn line instead" in str(e.value)
    cut = bc.ChainBreak(1, "cutover", "5", "a" * 64)                                # the cutover break, same rule
    with pytest.raises(bs.BoardError):
        br._ack_body(parts, 1, "why", cut, "repos.jsonl", None)
    ok = bc.ChainBreak(1, "x", "b" * 64, "a" * 64)
    assert br._ack_body(parts, 1, "why", ok, "items.jsonl", None)["want"] == "b" * 64


# ------------------------------------------------------------------------------------------------ 4: the fold says what it kept


def test_the_fold_separates_lines_it_dropped_from_lines_it_kept_each_with_its_acks_line(store, capsys):
    add(capsys, 3)
    lines = raw_lines(store)
    lines[1] = lines[1].replace(b'"title":"t"', b'"title":"forged"')                # line 3 now breaks, and parses: it is KEPT
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    junk = chained(store, {**WHOLE, "op": "frobnicate", "id": "levain#9"})
    append(store, junk)                                                              # line 4: torn by itself: DROPPED
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "w", *ACK)[0] == 0      # line 5
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "4", "--why", "w", *ACK)[0] == 0      # line 6
    f = folded(store)
    assert not f.torn and f.kept == ((3, 5),) and f.dropped == ((4, 6),) and f.acked == (3, 4)
    assert [n for n, _ in f.torn_lines] == [3, 4]


# ------------------------------------------------------------------------------------------------ 6: _why_class is linear


def _chain(n: int) -> list[bytes]:
    out, prev = [], hashlib.sha256(b"").hexdigest()
    for i in range(n):
        line = json.dumps({"v": 1, "op": "add", "id": f"a#{i + 1}", "prev": prev}).encode()
        out.append(line)
        prev = hashlib.sha256(line).hexdigest()
    return out


def test_naming_every_break_is_linear_and_unchanged():
    l1, l2, l3 = _chain(3)
    assert bc.check_chain([l1, l2, l3]).breaks == ()
    inserted = bc.check_chain([l1, b"x", l2, l3]).breaks                           # something inserted between line 1 and its successor
    assert "line 2 was inserted or moved between line 1 and line 3" in inserted[0].problem
    reordered = bc.check_chain([l1, l3, l2]).breaks
    assert "reordered (it chains onto line 3" in reordered[0].problem
    n = 8000
    lines = _chain(n)
    lines = lines[:1] + lines[2:] + lines[1:2]                                     # one reorder: a long run of breaks to name
    t = time.monotonic()
    c = bc.check_chain(lines)
    assert c.breaks and time.monotonic() - t < 3.0                                 # the old per-break scan of every hash was quadratic
    flat = [json.dumps({"v": 1, "op": "add", "id": f"a#{i}", "prev": hashlib.sha256(str(i).encode()).hexdigest()}).encode() for i in range(n)]
    t = time.monotonic()
    assert len(bc.check_chain(flat).breaks) >= n - 1 and time.monotonic() - t < 3.0
