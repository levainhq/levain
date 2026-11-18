"""P1b: the hash chain is CHECKED (one checker, both folds and the writer), a break reads as torn, the writer refuses after one
naming it, and ``ack-torn`` acknowledges a torn or broken line of ``items.jsonl``. Every case was run once by hand against a
scratch store before it was written down (the unchained line is the one flow's old writer appends)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
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
    monkeypatch.setattr(bs, "_check_repo_mode", lambda store_dir, repo, key, exact_hit=False: None)


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
    [brk] = bc.check_chain(lines).breaks
    assert brk.line == 3 and brk.problem.startswith("cutover prev does not commit the legacy prefix")
    f = folded(store)
    assert f.torn and f.torn_why.startswith("line 3: cutover prev does not commit the legacy prefix")
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 3" in err and "cutover prev does not commit the legacy prefix" in err


def test_an_unchained_line_after_the_cutover_is_torn_and_the_writer_refuses_naming_an_old_writer(store, capsys):
    add(capsys, 2)
    append(store, OLD)                                                              # flow's old writer: a v1 line with no prev
    f = folded(store)
    assert f.torn and f.torn_why.startswith("line 3: unchained record after the cutover (an old writer?)")
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
    [brk] = bc.check_chain(lines).breaks
    assert brk.line == 2 and brk.have == sha(lines[0])                              # old line 3 sits at 2, chained onto the deleted line 2
    assert "line 1 was edited or deleted, or a line was inserted before line 2" in brk.problem   # no line holds that hash now
    assert folded(store).torn_why.startswith("line 2: ")
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 2" in err and "an edit, a deletion, an insertion or a reorder before line 2" in err


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
    assert mods <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}


# ------------------------------------------------------------------------------------------------ ack-torn on items


def test_ack_torn_on_items_re_anchors_the_chain_and_the_fold_derives_again(store, capsys):
    add(capsys, 2)
    append(store, OLD)
    rc, out, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "stale checkout", *ACK)
    assert rc == 0 and out.startswith("acknowledged items.jsonl line 3: it carries no prev; line 2 is now "), err
    assert "an edit, a deletion, an insertion or a reorder before line 3" in out
    ack = json.loads(raw_lines(store)[-1])
    assert list(ack)[:4] == ["v", "op", "at", "prev"] and "id" not in ack
    assert ack["op"] == "ack-torn" and ack["line"] == 3 and ack["sha256"] == sha(OLD) and ack["why"] == "stale checkout"
    assert ack["want"] is None and ack["have"] == sha(raw_lines(store)[1])
    assert ack["prev"] == sha(OLD)                                                  # at the tail the chain's prev IS the broken line's hash (7.4)
    f = folded(store)
    assert not f.torn and f.acked == (3,) and list(f.items) == ["levain#1", "levain#2", "levain#9"]   # an unchained old-writer record, acked, is KEPT
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
    assert not f.torn and f.acked == (2,) and f.torn_lines == ((2, sha(b"garbage")),)       # a torn line is DROPPED
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
    assert folded(store).torn_why.startswith("line 3: line 2 was edited or deleted, or a line was inserted before line 3")
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "line 2 was hand-edited", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (3,) and list(f.items) == ["levain#1", "levain#2", "levain#3"]   # line 3 is KEPT: the edit is what the ack reports
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
    assert f.torn and f.torn_why.startswith("line 6: unchained record after the cutover (an old writer?)") and f.acked == (3,)
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 6" in err and "an old writer?" in err
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "6", "--why", "two", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (3, 6)
    add(capsys)
    assert not folded(store).torn


def _hand_ack(store: Path, line: int, digest: str, chained: bool = True, **extra) -> bytes:
    rec = {"v": 1, "op": "ack-torn", "at": "2026-11-17T00:00:00Z", "by": "me", "line": line, "sha256": digest, "why": "w", **extra}
    if chained:
        rec = bs._with_prev(rec, bs.chain_prev((store / "items.jsonl").read_bytes()))
    return bs.canonical_bytes(rec)


def test_an_ack_that_names_the_wrong_hash_skips_nothing(store, capsys):
    add(capsys)
    append(store, OLD)
    append(store, _hand_ack(store, 2, "0" * 64))
    f = folded(store)
    assert f.torn and f.acked == () and any("different sha256" in p or "not this break" in p for p in f.problems)
    assert run(capsys, *ADD)[0] == 2
    append(store, _hand_ack(store, 2, sha(OLD)))                                    # an ack of a chain break that names no break: not honoured
    assert folded(store).torn
    append(store, _hand_ack(store, 2, sha(OLD), want=None, have=sha(raw_lines(store)[0])))   # a corrected re-ack repairs it
    assert not folded(store).torn and folded(store).acked == (2,)


def test_an_ack_torn_record_is_itself_chained_an_unchained_one_is_a_break(store, capsys):
    add(capsys)
    append(store, OLD)
    append(store, _hand_ack(store, 2, sha(OLD), chained=False))
    f = folded(store)                                                               # an ack is collected only from a line that itself chains
    assert f.torn_why.startswith("line 2: unchained record after the cutover (an old writer?)") and f.acked == ()
    assert [n for n, _ in f.torn_lines] == [2, 3]


# ------------------------------------------------------------------------------------------------ P1b fix 1: the ack binds the break


def _forge_line2(store: Path, newtitle: bytes = b"forged") -> list[bytes]:
    lines = raw_lines(store)
    lines[1] = re.sub(rb'"title":"[^"]*"', b'"title":"' + newtitle + b'"', lines[1])
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    return lines


def test_a_chain_ack_binds_the_exact_break_so_a_later_edit_to_the_line_before_breaks_again(store, capsys):
    add(capsys, 3)
    _forge_line2(store)
    out = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "hand edit", *ACK)[1]
    lines = raw_lines(store)
    assert f"it chained onto {json.loads(lines[2])['prev']}" in out and f"line 2 is now {sha(lines[1])}" in out
    ack = json.loads(lines[-1])
    assert ack["want"] == json.loads(lines[2])["prev"] and ack["have"] == sha(lines[1]) and ack["sha256"] == sha(lines[2])
    assert not folded(store).torn and list(folded(store).items) == ["levain#1", "levain#2", "levain#3"]    # line 3 KEPT
    _forge_line2(store, b"forged-again")                                                                  # the line before is edited AGAIN
    f = folded(store)
    assert f.torn and f.acked == () and "not this break as it stands now" in f.torn_why
    rc, _, err = run(capsys, *ADD)
    assert rc == 2 and "line 3" in err and "levain board ack-torn --file items.jsonl --line 3" in err


def test_a_chain_ack_stops_holding_when_the_acknowledged_line_itself_is_edited(store, capsys):
    add(capsys, 3)
    _forge_line2(store)
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "w", *ACK)[0] == 0
    lines = raw_lines(store)
    lines[2] = lines[2].replace(b'"title":"t"', b'"title":"edited-3"')
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    f = folded(store)
    assert f.torn and "line 3" in f.torn_why and f.acked == () and f.items["levain#2"].title == "forged"


def test_a_torn_style_ack_does_not_acknowledge_a_chain_break(store, capsys):
    add(capsys, 3)
    _forge_line2(store)
    append(store, _hand_ack(store, 3, sha(raw_lines(store)[2])))                    # sha256 only: names no break
    assert folded(store).torn and run(capsys, *ADD)[0] == 2


def test_the_checker_names_the_class_of_a_break(store, capsys):
    add(capsys, 4)
    ls = raw_lines(store)
    [gone] = bc.check_chain([ls[0], ls[1], ls[3]]).breaks                             # old line 3 deleted: no line holds line 4's prev
    assert gone.line == 3 and "line 2 was edited or deleted, or a line was inserted before line 3" in gone.problem
    ins = bc.check_chain([ls[0], ls[1], ls[0], ls[2], ls[3]]).breaks                  # a line inserted: line 4 chains onto line 2
    assert any(b.line == 4 and "line 3 was inserted or moved between line 2 and line 4" in b.problem for b in ins), [b.problem for b in ins]
    reord = bc.check_chain([ls[0], ls[2], ls[1], ls[3]]).breaks                       # lines 2 and 3 swapped: line 2 chains onto line 3, now later
    assert reord[0].line == 2 and "reordered (it chains onto line 3, which now follows it)" in reord[0].problem


def test_the_unterminated_legacy_tail_is_hashed_as_the_writer_completes_it(store, capsys):
    (store / "items.jsonl").write_bytes(OLD + b"\ngarbage")                          # crashed mid-append: no final newline
    assert folded_text(store).torn
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "crash", *ACK)[0] == 0
    f = folded(store)
    assert not f.torn and f.acked == (2,), f.problems                               # ONE ack is enough
    assert sum(1 for x in raw_lines(store) if b'"ack-torn"' in x) == 1
    add(capsys)
    assert not folded(store).torn


def folded_text(store: Path):
    return fold((store / "items.jsonl").read_bytes().decode().split("\n"))


def test_one_ack_parser_judges_the_writer_and_both_folds_alike(store, capsys):
    good = {"v": 1, "op": "ack-torn", "at": "t", "by": "b", "why": "w", "line": 2, "sha256": "a" * 64}
    assert bc.parse_ack(good) == bc.Ack(2, "a" * 64, "w", False)
    assert bc.parse_ack({**good, "want": None, "have": "b" * 64}).chain is True
    assert bc.parse_ack({"op": "add"}) is None
    for bad in ({**good, "v": 2}, {**good, "v": True}, {**good, "why": " "}, {**good, "by": ""}, {**good, "line": 0},
                {**good, "line": True}, {**good, "sha256": "A" * 64}, {**good, "have": "b" * 64}, {**good, "want": None},
                {**good, "want": "zz", "have": "b" * 64}, {**good, "want": None, "have": None}):
        with pytest.raises(bc.AckError):
            bc.parse_ack(bad)


def test_an_ack_with_an_escaped_op_name_is_still_an_ack_everywhere(store, capsys):
    add(capsys)
    append(store, b"garbage")
    rec = {"v": 1, "op": "ack-torn", "at": "2026-11-17T00:00:00Z", "by": "me", "line": 2, "sha256": sha(b"garbage"), "why": "w"}
    rec = bs._with_prev(rec, bs.chain_prev((store / "items.jsonl").read_bytes()))
    append(store, bs.canonical_bytes(rec).replace(b'"ack-torn"', b'"ack\\u002dtorn"'))
    assert b"ack\\u002dtorn" in raw_lines(store)[-1]
    f = folded(store)
    assert not f.torn and f.acked == (2,)                                           # the fold: no substring prefilter slips past
    assert bc.acked_lines(raw_lines(store)) == {2} and run(capsys, *ADD)[0] == 0    # and the writer agrees


def test_an_ack_is_collected_only_from_a_line_that_itself_chains(store, capsys):
    add(capsys)
    append(store, b"garbage")
    unchained = {"v": 1, "op": "ack-torn", "at": "t", "by": "b", "line": 2, "sha256": sha(b"garbage"), "why": "w"}
    append(store, bs.canonical_bytes(unchained))
    f = folded(store)
    assert f.torn and f.acked == () and bc.acked_lines(raw_lines(store)) == set()


def test_a_bom_line_is_judged_the_same_by_the_checker_the_fold_and_the_writer(store, capsys):
    add(capsys)
    lines = raw_lines(store)
    bom = b"\xef\xbb\xbf" + lines[0]
    (store / "items.jsonl").write_bytes(b"\n".join([bom] + lines[1:]) + b"\n")
    assert bc.record_of(bom) is None and bc.record_of(bom.decode()) is None
    assert folded(store).torn and run(capsys, *ADD)[0] == 2


def test_crlf_lines_are_one_line_each_and_hash_with_their_carriage_return(store, capsys):
    add(capsys, 2)
    lines = [x + b"\r" for x in raw_lines(store)]
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    f = folded(store)
    assert f.torn and f.torn_why.startswith("line 2: ")                              # the chain hashed the lines without the \r
    legacy = [OLD + b"\r", OLD.replace(b"levain#9", b"levain#8") + b"\r"]
    (store / "items.jsonl").write_bytes(b"\n".join(legacy) + b"\n")
    add(capsys)
    assert not folded(store).torn and bc.check_chain(raw_lines(store)).breaks == ()
    assert json.loads(raw_lines(store)[2])["prev"] == sha(b"\n".join(legacy) + b"\n")


def test_a_trailing_blank_line_is_a_line_and_the_next_record_chains_onto_it(store, capsys):
    add(capsys)
    with open(store / "items.jsonl", "ab") as fh:
        fh.write(b"\n")
    assert not folded(store).torn and bc.check_chain(raw_lines(store)).breaks == ()
    assert run(capsys, *ADD)[0] == 0
    last = raw_lines(store)
    assert json.loads(last[-1])["prev"] == sha(b"") and last[-2] == b"" and not folded(store).torn


def test_an_ack_of_an_ack_is_a_problem_and_skips_nothing(store, capsys):
    add(capsys, 2)
    append(store, OLD)
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "w", *ACK)[0] == 0
    rc, _, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "4", "--why", "w", *ACK)
    assert rc == 2 and "is not torn" in err
    f = folded(store)
    assert not f.torn and f.acked == (3,)


def test_a_duplicate_cutover_is_a_break(store, capsys):
    add(capsys, 2)
    lines = raw_lines(store)
    dup = dict(json.loads(lines[1]))
    dup["prev"] = sha(b"")                                                          # a second "first record": commits the empty prefix again
    (store / "items.jsonl").write_bytes(b"\n".join([lines[0], bs.canonical_bytes(dup)]) + b"\n")
    c = bc.check_chain(raw_lines(store))
    assert c.cutover == 1 and [b.line for b in c.breaks] == [2]


def test_a_break_inside_the_legacy_prefix_reads_at_the_cutover(store, capsys):
    (store / "items.jsonl").write_bytes(OLD + b"\n" + OLD.replace(b"levain#9", b"levain#8") + b"\n")
    add(capsys)
    lines = raw_lines(store)
    lines[0] = lines[0].replace(b'"title":"old"', b'"title":"edited"')               # the prefix is edited after the cutover committed it
    (store / "items.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    c = bc.check_chain(lines)
    assert [b.line for b in c.breaks] == [3] and c.breaks[0].problem.startswith("cutover prev does not commit the legacy prefix")
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "3", "--why", "prefix edit", *ACK)[0] == 0
    assert not folded(store).torn and list(folded(store).items) == ["levain#9", "levain#8", "levain#10"]
