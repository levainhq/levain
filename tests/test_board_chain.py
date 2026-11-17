"""P1b: the hash chain is CHECKED (one checker, both folds and the writer), a break reads as torn, the writer refuses after one
naming it, and ``ack-torn`` acknowledges a torn or broken line of ``items.jsonl``. Every case was run once by hand against a
scratch store before it was written down (the unchained line is the one flow's old writer appends)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_chain as bc
from levain.desk import board_store as bs
from levain.desk.board_state import fold

ADD = ("add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "b", "--author-kind", "agent")
ACK = ("--by", "me", "--author-kind", "operator")
OLD = b'{"v":1,"op":"add","id":"levain#9","at":"2026-11-17T00:00:00Z","by":"flow","repo":"levain","title":"old","origin":"o"}'


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
    raw = (store / "items.jsonl").read_bytes()
    return raw.split(b"\n")[:-1]


def folded(store: Path):
    return fold([x.decode() for x in raw_lines(store)])


def append(store: Path, payload: bytes) -> None:
    with open(store / "items.jsonl", "ab") as fh:
        fh.write(payload + b"\n")


# ------------------------------------------------------------------------------------------------ the checker, pure


def test_a_clean_chain_has_no_breaks_and_the_cutover_is_the_first_line(store, capsys):
    add(capsys, 3)
    c = bc.check_chain(raw_lines(store))
    assert c.breaks == () and c.cutover == 1 and c.legacy == 0
    assert not folded(store).torn


def test_an_empty_or_all_legacy_file_has_no_cutover_and_no_breaks():
    assert bc.check_chain([]) == bc.Chain((), None, 0)
    c = bc.check_chain([b'{"a":1}', b"garbage", b""])
    assert c.breaks == () and c.cutover is None and c.legacy == 3


def test_a_legacy_prefix_is_committed_by_the_cutover_and_a_clean_cutover_is_no_break(store, capsys):
    legacy = [OLD, OLD.replace(b"levain#9", b"levain#8")]
    (store / "items.jsonl").write_bytes(b"\n".join(legacy) + b"\n")
    add(capsys)
    lines = raw_lines(store)
    assert json.loads(lines[2])["prev"] == sha(b"\n".join(legacy) + b"\n")           # the WHOLE prefix, each line's newline included
    c = bc.check_chain(lines)
    assert c.breaks == () and c.cutover == 3 and c.legacy == 2 and not folded(store).torn


def test_a_cutover_prev_that_does_not_commit_the_prefix_is_named_and_the_fold_is_torn(store, capsys):
    (store / "items.jsonl").write_bytes(OLD + b"\n")
    add(capsys)
    lines = raw_lines(store)
    lines.insert(0, OLD.replace(b"levain#9", b"levain#8"))                          # a legacy line inserted behind the cutover's back
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    assert bc.check_chain(lines).breaks == (bc.ChainBreak(3, "cutover prev does not commit the legacy prefix"),)
    f = folded(store)
    assert f.torn and f.torn_why == "line 3: cutover prev does not commit the legacy prefix"
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 3" in err and "cutover prev does not commit the legacy prefix" in err


def test_an_unchained_line_after_the_cutover_is_torn_and_the_writer_refuses_naming_an_old_writer(store, capsys):
    add(capsys, 2)
    append(store, OLD)                                                              # flow's old writer: a v1 line with no prev
    f = folded(store)
    assert f.torn and f.torn_why == "line 3: unchained record after the cutover (an old writer?)"
    assert [i for i in f.items] == ["levain#1", "levain#2"] and f.torn_lines == ((3, sha(OLD)),)
    before = (store / "items.jsonl").read_bytes()
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "an old writer?" in err and 'levain board ack-torn --file items.jsonl --line 3 --why "..."' in err
    assert (store / "items.jsonl").read_bytes() == before
    for argv in (("amend", "--id", "levain#1", "--why", "w", "--by", "b", "--title", "x", "--author-kind", "agent"),
                 ("decide", "--id", "levain#1", "--decision", "cut", "--words", "w", "--by", "b", "--author-kind", "agent")):
        rc, _, err = run(capsys, *argv)
        assert rc == 2 and "an old writer?" in err and (store / "items.jsonl").read_bytes() == before, argv


def test_a_prev_that_does_not_match_the_line_before_is_named(store, capsys):
    add(capsys, 3)
    lines = raw_lines(store)
    del lines[1]                                                                    # a middle line deleted
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    assert bc.check_chain(lines).breaks == (bc.ChainBreak(2, "prev does not match line 1"),)
    assert folded(store).torn_why == "line 2: prev does not match line 1"
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "prev does not match line 1" in err


def test_an_edited_line_breaks_the_chain_at_the_record_after_it_and_a_blank_line_does_too(store, capsys):
    add(capsys, 3)
    lines = raw_lines(store)
    edited = lines[:]
    edited[0] = edited[0].replace(b'"title":"t"', b'"title":"u"')
    assert [b.line for b in bc.check_chain(edited).breaks] == [2]
    blank = lines[:1] + [b""] + lines[1:]
    assert [b.line for b in bc.check_chain(blank).breaks] == [3]                    # a blank line hashes as empty bytes


def test_the_two_files_are_two_chains():
    a = bc.check_chain([b'{"op":"x","prev":"' + sha(b"").encode() + b'"}'])
    assert a.breaks == () and a.cutover == 1                                         # a new file's cutover is the hash of nothing, whatever the other file holds


def test_the_checker_is_pure():
    src = Path(bc.__file__).read_text("utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {"__future__", "hashlib", "json", "dataclasses", "typing"}


# ------------------------------------------------------------------------------------------------ ack-torn on items


def test_ack_torn_on_items_re_anchors_the_chain_and_the_fold_derives_again(store, capsys):
    add(capsys, 2)
    append(store, OLD)
    rc, out, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "stale checkout", *ACK)
    assert rc == 0 and out == "acknowledged items.jsonl line 3", err
    ack = json.loads(raw_lines(store)[-1])
    assert list(ack)[:4] == ["v", "op", "at", "prev"] and "id" not in ack
    assert ack["op"] == "ack-torn" and ack["line"] == 3 and ack["sha256"] == sha(OLD) and ack["why"] == "stale checkout"
    assert ack["prev"] == sha(OLD)                                                  # at the tail the chain's prev IS the torn line's hash (7.4)
    f = folded(store)
    assert not f.torn and f.acked == (3,) and list(f.items) == ["levain#1", "levain#2"]          # skipped exactly: the old writer's record is not applied
    add(capsys)                                                                     # writes work again, chained past the ack
    assert not folded(store).torn and json.loads(raw_lines(store)[-1])["prev"] == sha(raw_lines(store)[-2])
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "again", *ACK)[1].endswith("already acknowledged")


def test_ack_torn_on_items_also_accepts_a_line_that_is_not_json(store, capsys):
    add(capsys)
    append(store, b"garbage")
    assert folded(store).torn
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "torn line (2)" in err and "ack-torn --file items.jsonl --line 2" in err
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "crash", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (2,) and f.torn_lines == ((2, sha(b"garbage")),)
    add(capsys)
    assert list(folded(store).items) == ["levain#1", "levain#2"]


def test_ack_torn_on_items_refuses_a_line_that_is_not_torn_or_does_not_exist(store, capsys):
    add(capsys)
    before = (store / "items.jsonl").read_bytes()
    rc, _, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "1", "--why", "w", *ACK)
    assert rc == 2 and "is not torn" in err
    rc, _, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "7", "--why", "w", *ACK)
    assert rc == 2 and "there is no line 7" in err
    assert (store / "items.jsonl").read_bytes() == before


def test_a_break_in_the_middle_is_acknowledged_by_naming_that_line_and_later_lines_stay_valid(store, capsys):
    add(capsys, 3)
    lines = raw_lines(store)
    forged = lines[1].replace(b'"title":"t"', b'"title":"forged"')                  # line 2 edited: line 3 no longer chains onto it
    (store / "items.jsonl").write_bytes(b"\n".join([lines[0], forged, lines[2]]) + b"\n")
    assert folded(store).torn_why == "line 3: prev does not match line 2"
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "line 2 was hand-edited", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (3,) and list(f.items) == ["levain#1", "levain#2"]          # line 3 (the one that does not chain) is skipped; line 2 reads as written
    assert f.items["levain#2"].title == "forged"
    add(capsys)
    assert not folded(store).torn


def test_a_second_break_after_an_ack_refuses_again_until_it_is_acked_too(store, capsys):
    add(capsys, 2)
    append(store, OLD)
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "one", *ACK)[0] == 0
    add(capsys)
    append(store, OLD.replace(b"levain#9", b"levain#8"))                            # the stale checkout writes again
    f = folded(store)
    assert f.torn and f.torn_why == "line 6: unchained record after the cutover (an old writer?)" and f.acked == (3,)
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 6" in err and "an old writer?" in err
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "6", "--why", "two", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (3, 6)
    add(capsys)
    assert not folded(store).torn


def _hand_ack(store: Path, line: int, digest: str, chained: bool = True) -> bytes:
    rec = {"v": 1, "op": "ack-torn", "at": "2026-11-17T00:00:00Z", "by": "me", "line": line, "sha256": digest, "why": "w"}
    if chained:
        rec = bs._with_prev(rec, bs.chain_prev((store / "items.jsonl").read_bytes()))
    return bs.canonical_bytes(rec)


def test_an_ack_that_names_the_wrong_hash_skips_nothing(store, capsys):
    add(capsys)
    append(store, OLD)
    append(store, _hand_ack(store, 2, "0" * 64))
    f = folded(store)
    assert f.torn and f.acked == () and any("different sha256" in p for p in f.problems)
    assert run(capsys, *ADD)[0] == 2
    append(store, _hand_ack(store, 2, sha(OLD)))                                    # a corrected re-ack repairs it
    assert not folded(store).torn and folded(store).acked == (2,)


def test_an_ack_torn_record_is_itself_chained_an_unchained_one_is_a_break(store, capsys):
    add(capsys)
    append(store, OLD)
    append(store, _hand_ack(store, 2, sha(OLD), chained=False))
    assert folded(store).torn_why == "line 3: unchained record after the cutover (an old writer?)"
