"""``levain board state --json``: the pure board function over REAL git evidence, end to end (slice B2).

Each world is a real bare origin plus clone (tests/_board_git_world.py); each assertion is about what the board says of
an item, read from the printed JSON. The contract flow consumes: the Board keys, the additive ``reader`` key, the exit
codes (0 clean, 3 audit, 2 unusable items file or option), and that the command writes nothing."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk.board_state import AUDIT_KINDS, COLUMNS, STATES
from tests._board_git_world import DAY, OLD, World, git

AT = "2026-11-12T10:00:00Z"
NOW = "2026-11-12T20:00:00Z"                 # evidence is judged at this instant; every world below is fetched "now"


def item(n: int, *, done: str | None = "the work lands", tier: str | None = "sonnet", kind: str = "merged",
         depends: list | None = None) -> str:
    return json.dumps({"v": 1, "op": "add", "id": f"levain#{n}", "at": AT, "by": "t", "repo": "levain", "title": f"item {n}",
                       "words": "", "origin": "desk 1", "done": done, "done_kind": kind, "tier": tier,
                       "depends_on": depends or []})


def write_items(tmp: Path, *lines: str) -> Path:
    p = tmp / "items.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def extra_file(tmp: Path, **kw) -> Path:
    body = {"seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], **kw}
    p = tmp / "extra.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def run(capsys, *argv: str):
    rc = main(["board", "state", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def board(capsys, w: World, items: Path, extra: Path, *more: str):
    rc, out, err = run(capsys, "--json", "--items", str(items), "--evidence-extra", str(extra), "--repo", str(w.w),
                       "--since", "2026-11-12T00:00:00Z", *more)
    return rc, json.loads(out), err


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.commit("root", date=OLD)
    w.push_main()
    return w


def states(doc: dict) -> dict[str, str]:
    return {s["id"]: s["state"] for s in doc["items"]}


# ------------------------------------------------------------------------------------------------ the contract
def test_a_clean_board_prints_the_contract_and_exits_zero(world, tmp_path, capsys):
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    world.branch("seat/1112-40-i2-lane")
    world.commit("lane work", trailers=("Board-Item: levain#2",))
    seats = [{"name": "lane-2", "items": ["levain#2"], "since": DAY, "branch": "seat/1112-40-i2-lane"}]
    rc, doc, err = board(capsys, world, write_items(tmp_path, item(1), item(2), item(3)), extra_file(tmp_path, seats=seats))
    assert rc == 0 and err == "", (doc["audit"], doc["problems"])
    assert set(doc) == {"schema", "as_of", "items", "audit", "unattributed", "problems", "reader"}
    assert doc["schema"] == "ready-v2" and doc["audit"] == [] and doc["problems"] == []
    assert states(doc) == {"levain#1": "done", "levain#2": "in_flight", "levain#3": "ready"}
    for s in doc["items"]:
        assert set(s) == {"id", "repo", "title", "tier", "state", "column", "reason", "refs", "as_of"}
        assert s["state"] in STATES and s["column"] in COLUMNS and s["reason"]
    assert doc["reader"]["repo"] == "levain" and doc["reader"]["main_ref"] == "origin/main"
    assert doc["reader"]["branches"] == 1 and doc["reader"]["main_records"] == 1


def test_without_json_a_table_is_printed_and_the_exit_code_is_the_same(world, tmp_path, capsys):
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    rc, out, _ = run(capsys, "--items", str(write_items(tmp_path, item(1))), "--evidence-extra", str(extra_file(tmp_path)),
                     "--repo", str(world.w), "--since", "2026-11-12T00:00:00Z")
    assert rc == 0 and "levain#1" in out and "done" in out and not out.lstrip().startswith("{")


def test_an_audit_entry_exits_three(world, tmp_path, capsys):
    world.commit("someone forgot the trailer")
    world.push_main()
    rc, doc, _ = board(capsys, world, write_items(tmp_path, item(3)), extra_file(tmp_path))
    assert rc == 3
    assert [a["kind"] for a in doc["audit"]] == ["unattributed_work"] and doc["audit"][0]["kind"] in AUDIT_KINDS
    assert doc["unattributed"] and doc["unattributed"][0]["subject"] == "someone forgot the trailer"


# ------------------------------------------------------------------------------------------------ the states, from git
def test_landed_on_main_without_a_closing_trailer_reads_landed_not_closed(world, tmp_path, capsys):
    world.commit("did the work", trailers=("Board-Item: levain#4",))
    world.push_main()
    rc, doc, _ = board(capsys, world, write_items(tmp_path, item(4)), extra_file(tmp_path))
    assert states(doc) == {"levain#4": "landed_not_closed"} and rc == 3
    assert [(a["kind"], a["id"]) for a in doc["audit"]] == [("landed_not_closed", "levain#4")]


def test_an_item_in_the_chain_reads_landing(world, tmp_path, capsys):
    world.branch("queue/1112-40/x1")
    world.commit("chain work", trailers=("Board-Item: levain#5",))
    world.push("queue/1112-40/x1")
    _, doc, _ = board(capsys, world, write_items(tmp_path, item(5)), extra_file(tmp_path))
    assert states(doc) == {"levain#5": "landing"}


def test_an_orphan_branch_with_commits_main_lacks_reads_ready_with_start(world, tmp_path, capsys):
    world.branch("seat/1112-40-i6-orphan")
    world.commit("orphan work", trailers=("Board-Item: levain#6",))
    world.push("seat/1112-40-i6-orphan")
    _, doc, _ = board(capsys, world, write_items(tmp_path, item(6)), extra_file(tmp_path))
    assert states(doc) == {"levain#6": "ready_with_start"}


def test_a_branch_whose_work_was_cherry_picked_is_not_an_orphan(world, tmp_path, capsys):
    world.branch("seat/1112-40-i7-landed")
    s = world.commit("landed work", trailers=("Board-Item: levain#7", "Board-Closes: levain#7"), file="a.txt", body="a\n")
    world.push("seat/1112-40-i7-landed")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(s)
    world.push_main()
    rc, doc, _ = board(capsys, world, write_items(tmp_path, item(7)), extra_file(tmp_path))
    assert states(doc) == {"levain#7": "done"} and rc == 0       # the stale branch is neither an orphan nor a conflict


def test_closed_on_main_and_a_live_seat_naming_it_is_unknown_with_both_refs(world, tmp_path, capsys):
    world.commit("closed", trailers=("Board-Item: levain#8", "Board-Closes: levain#8"))
    world.push_main()
    seats = [{"name": "lane-8", "items": ["levain#8"], "since": DAY, "branch": None}]
    _, doc, _ = board(capsys, world, write_items(tmp_path, item(8)), extra_file(tmp_path, seats=seats))
    s = doc["items"][0]
    assert s["state"] == "unknown" and "lane-8" in s["reason"]


def test_stale_evidence_reads_unknown_never_a_default_column(world, tmp_path, capsys):
    _, doc, _ = board(capsys, world, write_items(tmp_path, item(3)), extra_file(tmp_path), "--now", "2099-01-01T00:00:00Z")
    assert states(doc) == {"levain#3": "unknown"} and "origin/main as of" in doc["items"][0]["reason"]


def test_documents_are_stat_d_by_the_cli_and_the_extra_cannot_outvote_it(world, tmp_path, capsys):
    (tmp_path / "plan.md").write_text("the plan\n")
    items = write_items(tmp_path, item(9, done="plan.md", kind="document"), item(10, done="gone.md", kind="document"))
    extra = extra_file(tmp_path, documents={"plan.md": False, "gone.md": True, "other.md": True})
    # relative paths resolve against --repo, so put the file where the repo is
    (world.w / "plan.md").write_text("the plan\n")
    _, doc, _ = board(capsys, world, items, extra)
    assert states(doc) == {"levain#9": "done", "levain#10": "ready"}        # what the CLI saw on disk wins


# ------------------------------------------------------------------------------------------------ failure and usage
def test_a_missing_or_unreadable_items_file_exits_two(world, tmp_path, capsys):
    rc, out, err = run(capsys, "--json", "--items", str(tmp_path / "nope.jsonl"), "--repo", str(world.w))
    assert rc == 2 and out == "" and "items file" in err
    (tmp_path / "bin.jsonl").write_bytes(b"\xff\xfe\x00bad")
    rc, out, err = run(capsys, "--json", "--items", str(tmp_path / "bin.jsonl"), "--repo", str(world.w))
    assert rc == 2 and out == ""
    rc, _, err = run(capsys, "--json", "--items", str(tmp_path), "--repo", str(world.w))
    assert rc == 2


def test_no_items_path_exits_two_unless_the_environment_names_one(world, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    rc, _, err = run(capsys, "--json", "--repo", str(world.w))
    assert rc == 2 and "LEVAIN_BOARD_ITEMS" in err
    monkeypatch.setenv("LEVAIN_BOARD_ITEMS", str(write_items(tmp_path, item(3))))
    rc, out, _ = run(capsys, "--json", "--repo", str(world.w), "--evidence-extra", str(extra_file(tmp_path)))
    assert rc in (0, 3) and json.loads(out)["items"][0]["id"] == "levain#3"


def test_a_torn_items_file_is_a_board_not_an_exit_two(world, tmp_path, capsys):
    p = write_items(tmp_path, item(3), "{ this is not json")
    rc, doc, _ = board(capsys, world, p, extra_file(tmp_path))
    assert rc == 3 and states(doc) == {"levain#3": "unknown"} and "torn" in doc["items"][0]["reason"]


def test_a_bad_now_exits_two(world, tmp_path, capsys):
    rc, _, err = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w), "--now", "tomorrow")
    assert rc == 2 and "--now" in err


@pytest.mark.parametrize("body, why", [("[]", "must be a JSON object"), ("{bad", "cannot be read"),
                                       ('{"main": []}', "reads itself"), ('{"branches": []}', "reads itself"),
                                       ('{"evidence_v": 2}', "not 1"), ('{"unreadable": []}', "unreadable must be an object"),
                                       ('{"documents": []}', "documents must be an object")])
def test_an_unusable_evidence_extra_exits_two(world, tmp_path, capsys, body, why):
    p = tmp_path / "x.json"
    p.write_text(body)
    rc, out, err = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--evidence-extra", str(p), "--repo", str(world.w))
    assert rc == 2 and out == "" and why in err


def test_an_extra_that_does_not_exist_exits_two(world, tmp_path, capsys):
    rc, _, err = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--evidence-extra", str(tmp_path / "no.json"),
                     "--repo", str(world.w))
    assert rc == 2 and "--evidence-extra" in err


def test_without_the_flow_sources_every_item_reads_unknown_and_the_audit_says_why(world, tmp_path, capsys):
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w),
                     "--since", "2026-11-12T00:00:00Z")
    doc = json.loads(out)
    assert rc == 3 and states(doc) == {"levain#3": "unknown"}
    assert {"seats", "band", "waiters"} <= {a["why"].split(" ")[0] for a in doc["audit"]}


def test_a_directory_that_is_not_a_repository_is_exit_three_with_every_item_unknown(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, doc, _ = board(capsys, _NotAWorld(plain), write_items(tmp_path, item(3)), extra_file(tmp_path))
    assert rc == 3 and states(doc) == {"levain#3": "unknown"}
    assert "cannot be read" in doc["items"][0]["reason"]


class _NotAWorld:
    def __init__(self, path: Path) -> None:
        self.w = path


# ------------------------------------------------------------------------------------------------ it writes nothing
def test_the_command_writes_neither_the_items_store_nor_the_repository(world, tmp_path, capsys):
    world.branch("seat/1112-40-i2-lane")
    world.commit("lane work", trailers=("Board-Item: levain#2",))
    items = write_items(tmp_path, item(2), item(3))

    def snapshot() -> list:
        out = [(str(items), items.stat().st_size, items.stat().st_mtime_ns)]
        for base, _d, files in os.walk(world.w):
            for f in files:
                p = Path(base) / f
                st = p.stat()
                out.append((str(p), st.st_size, st.st_mtime_ns))
        return sorted(out)

    before = snapshot()
    board(capsys, world, items, extra_file(tmp_path))
    run(capsys, "--items", str(items), "--repo", str(world.w))
    assert snapshot() == before                               # items.jsonl, the work tree, every file under .git


def test_a_diverged_branch_is_named_in_the_reader_key_and_in_the_table(world, tmp_path, capsys):
    name = "seat/1112-40-i7-a"
    world.branch(name)
    a1 = world.commit("a1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    world.commit("a2", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    world.push(name)
    git(world.w, "checkout", "-q", "-B", name, a1)
    world.commit("local only", trailers=("Board-Item: levain#7",), file="l.txt", body="local\n")
    items, extra = write_items(tmp_path, item(7)), extra_file(tmp_path)
    _rc, doc, _err = board(capsys, world, items, extra)
    assert doc["reader"]["diverged"] == [name]
    rc, out, _ = run(capsys, "--items", str(items), "--evidence-extra", str(extra), "--repo", str(world.w),
                     "--since", "2026-11-12T00:00:00Z")
    assert f"diverged: {name}" in out


def test_a_shallow_checkout_is_a_board_of_unknowns_and_exit_three(world, tmp_path, capsys):
    world.commit("w1", trailers=("Board-Item: levain#1",))
    world.push_main()
    clone = world.clone(tmp_path / "shallow", "--depth", "1")
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(1))), "--evidence-extra",
                     str(extra_file(tmp_path)), "--repo", str(clone), "--since", "2026-11-12T00:00:00Z", "--now", NOW)
    doc = json.loads(out)
    assert rc == 3 and set(states(doc).values()) == {"unknown"}
    assert any("shallow clone" in p for p in doc["problems"])
