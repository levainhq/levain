"""``levain board state --json``: the pure board function over REAL git evidence, end to end (slice B2).

Each world is a real bare origin plus clone (tests/_board_git_world.py); each assertion is about what the board says of
an item, read from the printed JSON. The contract flow consumes: the Board keys, the additive ``reader`` key, the exit
codes (0 clean, 3 audit, 2 unusable items file or option), and that the command writes nothing."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_producers
from levain.desk import board_store as bs
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


@pytest.fixture(autouse=True)
def native_stub(monkeypatch):
    """Levain's own reads would open the real consent store and run claude: every test here gets a native producer that
    read nothing and was whole (seats and band; waiters only when declared), as a quiet install would."""
    def stub(*, now, store_id, no_land_waiter=False, **_kw):
        out = {s: board_producers._block(now, store_id, [], None) for s in ("seats", "band")}
        if no_land_waiter:
            out["waiters"] = board_producers._block(now, store_id, [], None)
        return out
    monkeypatch.setattr(board_producers, "native_blocks", stub)

@pytest.fixture(autouse=True)
def _registry_not_under_test(monkeypatch):
    """These tests are about the item writer and the CLI, not the registry: I1 (``add`` needs a tracked repo) is off here and
    is tested live in tests/test_board_registry.py."""
    monkeypatch.setattr(bs, "_check_repo_mode", lambda store_dir, repo, key: None)


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
    assert doc["schema"] == "ready-v3" and doc["audit"] == [] and doc["problems"] == []
    assert states(doc) == {"levain#1": "done", "levain#2": "in_flight", "levain#3": "ready"}
    for s in doc["items"]:
        assert set(s) == {"id", "repo", "title", "tier", "state", "column", "reason", "refs", "as_of", "kind"}
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
    assert [(s["column"], s["kind"]) for s in doc["items"]] == [("ready", "close")]


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
    rc, out, err = run(capsys, "--json", "--items", str(tmp_path / "nope" / "items.jsonl"), "--repo", str(world.w))
    assert rc == 2 and out == "" and "items file" in err
    rc, out, err = run(capsys, "--json", "--items", str(tmp_path / "nope.jsonl"), "--repo", str(world.w))     # a missing path gets the neutral refusal
    assert rc == 2 and out == "" and "nope.jsonl: no such file or directory" in err
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "items.jsonl").write_bytes(b"\xff\xfe\x00bad")
    rc, out, err = run(capsys, "--json", "--items", str(tmp_path / "b" / "items.jsonl"), "--repo", str(world.w))
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
                                       ('{"evidence_v": 3}', "not 1 or 2"), ('{"unreadable": []}', "unreadable must be an object"),
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


def test_without_flow_the_native_producer_leaves_only_the_waiter_dependent_items_unknown(world, tmp_path, capsys):
    """T4's user-visible point: no flow, no external producer. Seats and band are native; waiters are not declared, so an
    item that rests on the absence of a waiter reads unknown naming ``waiters``, and a closed item is still done."""
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(1), item(3))), "--repo", str(world.w),
                     "--since", "2026-11-12T00:00:00Z")
    doc = json.loads(out)
    assert rc == 3 and states(doc) == {"levain#1": "done", "levain#3": "unknown"}
    assert [s["reason"] for s in doc["items"] if s["id"] == "levain#3"] == ["waiters: not supplied"]
    assert [a["why"] for a in doc["audit"] if a["id"] is None and "unreadable" in a["why"]] == ["waiters unreadable: not supplied"]


def test_declaring_no_land_waiter_lets_the_native_producer_conclude_the_board(world, tmp_path, capsys):
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w),
                     "--since", "2026-11-12T00:00:00Z", "--no-land-waiter")
    doc = json.loads(out)
    assert states(doc) == {"levain#3": "ready"} and doc["audit"] == [] and rc == 0


def _producer_file(tmp_path, name, *, store_id=None, age_s=0, complete=True, **rows):
    when = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    blocks = {s: {"producer": name, "store_id": store_id, "captured_at": when, "complete": complete, "rows": r}
              for s, r in {"seats": [], "band": [], "waiters": [], **rows}.items()}
    (tmp_path / f"evidence.{name}.json").write_text(json.dumps({"evidence_v": 2, "blocks": blocks}), encoding="utf-8")


def test_an_external_producer_file_beside_the_items_supplies_the_waiters(world, tmp_path, capsys):
    _producer_file(tmp_path, "acme", waiters=[{"branch": "queue/x", "items": ["levain#3"]}])
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3), item(4))), "--repo", str(world.w),
                     "--since", "2026-11-12T00:00:00Z", "--producer", "acme")
    assert states(json.loads(out)) == {"levain#3": "landing", "levain#4": "ready"}


def test_an_external_producer_that_is_missing_or_stale_blocks_absence_and_names_why(world, tmp_path, capsys):
    argv = ("--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w), "--since", "2026-11-12T00:00:00Z",
            "--no-land-waiter", "--producer", "acme")
    doc = json.loads(run(capsys, *argv)[1])
    assert "producer acme unreadable (no evidence.acme.json in the store)" in doc["items"][0]["reason"]
    _producer_file(tmp_path, "acme", age_s=16 * 60)
    doc = json.loads(run(capsys, *argv)[1])
    assert "producer acme unreadable (stale 16 min)" in doc["items"][0]["reason"]


def test_a_block_from_another_store_is_refused_by_the_store_id(world, tmp_path, capsys):
    (tmp_path / "store.id").write_text("mine\n", encoding="utf-8")
    _producer_file(tmp_path, "acme", store_id="theirs")
    doc = json.loads(run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w),
                         "--since", "2026-11-12T00:00:00Z", "--no-land-waiter", "--producer", "acme")[1])
    assert "evidence from another store" in doc["items"][0]["reason"]


def test_a_store_with_no_id_says_so_when_a_block_names_one(world, tmp_path, capsys):
    _producer_file(tmp_path, "acme", store_id="theirs")
    rc, out, err = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--repo", str(world.w),
                       "--since", "2026-11-12T00:00:00Z", "--no-land-waiter", "--producer", "acme")
    assert "has no id" in err and states(json.loads(out)) == {"levain#3": "ready"}


def test_the_blocks_form_of_the_extra_is_appended_and_a_block_cannot_claim_the_flat_form(world, tmp_path, capsys):
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    good = {"evidence_v": 2, "waiters": [{"producer": "flow", "store_id": None, "captured_at": when, "complete": True, "rows": []}]}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(good))
    argv = ("--json", "--items", str(write_items(tmp_path, item(3))), "--evidence-extra", str(p), "--repo", str(world.w),
            "--since", "2026-11-12T00:00:00Z")
    rc, out, err = run(capsys, *argv)
    assert states(json.loads(out)) == {"levain#3": "ready"}          # flow speaks for waiters only, so only waiters are wanted of it
    p.write_text(json.dumps({"evidence_v": 2, "waiters": [dict(good["waiters"][0], form="flat")]}))
    rc, out, err = run(capsys, *argv)
    assert rc == 2 and "no form key" in err


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


# ================================================================================================ B2 L3 round 1 fixes (1112-44)
def test_a_closing_commit_that_was_later_reverted_leaves_the_item_open(world, tmp_path, capsys):
    """codex 1, end to end: the item read `done` while its closing work had been taken back."""
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    items, extra = write_items(tmp_path, item(1)), extra_file(tmp_path)
    assert states(board(capsys, world, items, extra)[1]) == {"levain#1": "done"}
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T14:00:00Z")
    world.push_main()
    assert states(board(capsys, world, items, extra)[1]) == {"levain#1": "ready"}
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T15:00:00Z")     # the revert of the revert: closed again
    world.push_main()
    assert states(board(capsys, world, items, extra)[1]) == {"levain#1": "done"}


def test_a_failure_board_prints_the_whole_reader_key(world, tmp_path, capsys):
    world.commit("w1")
    world.push_main()
    clone = world.clone(tmp_path / "shallow", "--depth", "1")
    rc, out, _ = run(capsys, "--json", "--items", str(write_items(tmp_path, item(1))), "--evidence-extra",
                     str(extra_file(tmp_path)), "--repo", str(clone), "--since", "2026-11-12T00:00:00Z", "--now", NOW)
    reader = json.loads(out)["reader"]
    assert rc == 3 and set(reader) == {"repo", "since", "main_ref", "main_commits", "main_records", "chain_refs", "chain_commits",
                                       "branches", "refs", "merged", "unattributed", "diverged", "matched_by_key"}
    assert reader["main_commits"] == 0 and reader["diverged"] == [] and reader["matched_by_key"] == 0


@pytest.mark.parametrize("body, why", [
    ('{"unreadable": {"seats": 42}}', "unreadable[seats] must be a string"),
    ('{"unreadable": {"seats": null}}', "unreadable[seats] must be a string"),
    ('{"unreadable": {"seats": ["x"]}}', "unreadable[seats] must be a string"),
    ('{"unreadable": {"main": "stale"}}', "reads itself"),
    ('{"unreadable": {"branches": "stale"}}', "reads itself"),
    ('{"reader": "evil"}', "reader"),
    ('{"surprise": 1}', "surprise"),
    ('{"seats": [], "typo_band": {}}', "typo_band"),
])
def test_a_wrong_typed_or_unknown_extra_is_refused_with_a_clear_message(world, tmp_path, capsys, body, why):
    """deepseek LOW-3 + codex 9: a non-string reason, an unknown key or an `unreadable` entry that names a source the CLI
    reads itself never reaches Evidence.from_dict."""
    p = tmp_path / "x.json"
    p.write_text(body)
    rc, out, err = run(capsys, "--json", "--items", str(write_items(tmp_path, item(3))), "--evidence-extra", str(p), "--repo", str(world.w))
    assert rc == 2 and out == "" and why in err and "Traceback" not in err


def test_the_keys_flow_supplies_are_still_accepted(world, tmp_path, capsys):
    extra = extra_file(tmp_path, unreadable={"seats": "the roster could not be read"}, documents={"x.md": True}, evidence_v=1)
    rc, doc, err = board(capsys, world, write_items(tmp_path, item(3)), extra)
    assert rc == 3 and err == "" and states(doc) == {"levain#3": "unknown"}
    assert any("seats unreadable: the roster could not be read" in p for p in doc["problems"])


def test_closed_on_main_with_a_chain_link_and_an_orphan_seat_branch_reads_done(world, tmp_path, capsys):
    """flow 9's finding: the Board-Closes trailer is on origin/main, a queue ref still carries the item and a seat branch
    beside the landed chain has commits main lacks. The closure wins; the item is not unknown."""
    world.branch("queue/1114-land3/x1")
    world.commit("chain work", trailers=("Board-Item: levain#9",), file="c.txt", body="c\n")
    world.push("queue/1114-land3/x1")
    git(world.w, "checkout", "-q", "main")
    world.branch("seat/1114-44-orphan")
    world.commit("orphan work", trailers=("Board-Item: levain#9",), file="o.txt", body="o\n")
    world.commit("more orphan work", trailers=("Board-Item: levain#9",), file="o2.txt", body="o2\n")
    world.push("seat/1114-44-orphan")
    git(world.w, "checkout", "-q", "main")
    world.commit("closed", trailers=("Board-Item: levain#9", "Board-Closes: levain#9"), file="m.txt", body="m\n")
    world.push_main()
    rc, doc, _ = board(capsys, world, write_items(tmp_path, item(9)), extra_file(tmp_path))
    assert states(doc) == {"levain#9": "done"} and rc == 0


def test_a_chain_link_and_an_orphan_branch_with_no_closing_trailer_still_conflict(world, tmp_path, capsys):
    world.branch("queue/1114-land3/x2")
    world.commit("chain work", trailers=("Board-Item: levain#10",), file="c.txt", body="c\n")
    world.push("queue/1114-land3/x2")
    git(world.w, "checkout", "-q", "main")
    world.branch("seat/1114-44-orphan2")
    world.commit("orphan work", trailers=("Board-Item: levain#10",), file="o.txt", body="o\n")
    world.push("seat/1114-44-orphan2")
    _, doc, _ = board(capsys, world, write_items(tmp_path, item(10)), extra_file(tmp_path))
    assert states(doc) == {"levain#10": "unknown"} and "orphan branch seat/1114-44-orphan2" in doc["items"][0]["reason"]


# ------------------------------------------------------------------------------------------------ the cross-repo board
def _flow_world(tmp: Path) -> Path:
    """A second repository, named ``flow``: a clone moved to a directory of that name (the name item ids use)."""
    fw = World(tmp / "flowworld")
    fw.commit("root", date=OLD)
    fw.push_main()
    dest = tmp / "flowworld" / "flow"
    fw.w.rename(dest)
    return dest


def _flow_item(n: int, depends: list) -> str:
    return json.dumps({"v": 1, "op": "add", "id": f"flow#{n}", "at": AT, "by": "t", "repo": "flow", "title": f"flow {n}",
                       "words": "", "origin": "desk 1", "done": "x", "done_kind": "merged", "tier": "sonnet", "depends_on": depends})


def test_also_repo_judges_a_dependency_under_its_own_repo(world, tmp_path, capsys):
    """flow#5 depends on levain#1, which is closed on LEVAIN's main. Reading flow alone leaves levain#1 not done (flow's
    main knows nothing of it); --also-repo reads each item from its own repo, so the dependency is met."""
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    flow = _flow_world(tmp_path)
    items = write_items(tmp_path, item(1), _flow_item(5, [{"kind": "item", "ref": "levain#1"}]))
    extra = extra_file(tmp_path)
    common = ("--json", "--items", str(items), "--evidence-extra", str(extra), "--repo", str(flow), "--since", "2026-11-12T00:00:00Z")
    rc, out, err = run(capsys, *common)
    assert states(json.loads(out))["flow#5"] == "blocked"                       # the old answer, kept without the flag
    rc, out, err = run(capsys, *common, "--also-repo", str(world.w))
    doc = json.loads(out)
    assert states(doc) == {"levain#1": "done", "flow#5": "ready"}, (doc["items"], err)
    assert [r["repo"] for r in doc["reader"]["also"]] == ["levain"]


def test_also_repo_takes_the_repo_name_from_name_equals_path(world, tmp_path, capsys):
    """A clone under another directory name (a worktree, /tmp/levain-review) is named explicitly, NAME=PATH."""
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    flow = _flow_world(tmp_path)
    items = write_items(tmp_path, item(1), _flow_item(5, [{"kind": "item", "ref": "levain#1"}]))
    common = ("--json", "--items", str(items), "--evidence-extra", str(extra_file(tmp_path)), "--repo", str(flow),
              "--since", "2026-11-12T00:00:00Z")
    rc, out, err = run(capsys, *common, "--also-repo", f"levain={world.w}")
    assert states(json.loads(out)) == {"levain#1": "done", "flow#5": "ready"}, err
    review = tmp_path / "levain-review"
    world.w.rename(review)
    rc, out, err = run(capsys, *common, "--also-repo", f"levain={review}")
    assert states(json.loads(out)) == {"levain#1": "done", "flow#5": "ready"}, err
    rc, out, err = run(capsys, *common, "--also-repo", str(review))              # a bare PATH keeps its basename: not levain
    assert states(json.loads(out))["levain#1"] == "unknown"


def test_also_repo_with_an_empty_primary_name_is_refused_not_all_unknown(world, tmp_path, capsys):
    items = write_items(tmp_path, item(1))
    rc, out, err = run(capsys, "--json", "--items", str(items), "--evidence-extra", str(extra_file(tmp_path)),
                       "--repo", str(world.w), "--repo-name", "", "--also-repo", str(_flow_world(tmp_path)))
    assert rc == 2 and "repo name is empty" in err


def test_extra_documents_apply_to_the_primary_repo_only():
    """A document path is repo-relative: the flow-supplied one (the fallback when this process could not stat) must not
    mark another repo's same path checked."""
    from levain.desk.board_cli import _merge_extra
    extra = {"documents": {"result.md": True}}
    primary, other = {"documents": {}}, {"documents": {}}
    assert _merge_extra(primary, extra) is None and _merge_extra(other, extra, primary=False) is None
    assert primary["documents"] == {"result.md": True} and other["documents"] == {}


# ------------------------------------------------------------------------------------------------ the fix round (levain#91)
def _items_argv(world, tmp_path, *more, n=3):
    return ("--json", "--items", str(write_items(tmp_path, item(n))), "--repo", str(world.w), "--since", "2026-11-12T00:00:00Z", *more)


def test_no_land_waiter_stands_unless_a_producer_actually_supplied_waiters(world, tmp_path, capsys):
    """F5: flow is expected only for the sources its extra carries; an extra with no waiters leaves the declaration standing."""
    p = tmp_path / "extra.json"
    p.write_text(json.dumps({"seats": [], "band": {"open": {}, "resolved": {}}}))
    rc, out, _ = run(capsys, *_items_argv(world, tmp_path, "--evidence-extra", str(p), "--no-land-waiter"))
    assert states(json.loads(out)) == {"levain#3": "ready"} and rc == 0
    p.write_text(json.dumps({"seats": [], "band": {"open": {}, "resolved": {}}, "waiters": []}))     # now flow speaks for waiters
    old = datetime.now(timezone.utc).timestamp() - 20 * 60
    os.utime(p, (old, old))
    doc = json.loads(run(capsys, *_items_argv(world, tmp_path, "--evidence-extra", str(p), "--no-land-waiter"))[1])
    assert doc["items"][0]["state"] == "unknown" and "producer flow unreadable (stale 20 min)" in doc["items"][0]["reason"]


def test_an_external_producer_is_expected_only_for_the_sources_its_file_holds(world, tmp_path, capsys):
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    only = {"waiters": {"producer": "acme", "store_id": None, "captured_at": when, "complete": True, "rows": []}}
    (tmp_path / "evidence.acme.json").write_text(json.dumps({"evidence_v": 2, "blocks": only}))
    rc, out, _ = run(capsys, *_items_argv(world, tmp_path, "--producer", "acme"))
    assert states(json.loads(out)) == {"levain#3": "ready"}                       # acme says nothing of seats, so none is wanted of it
    (tmp_path / "evidence.acme.json").write_text("{torn")
    doc = json.loads(run(capsys, *_items_argv(world, tmp_path, "--producer", "acme"))[1])
    assert "producer acme unreadable (evidence.acme.json cannot be read" in doc["items"][0]["reason"]   # a failed file is wanted for all


def test_a_flat_extra_is_as_old_as_its_own_fetched_at_or_its_file_never_the_moment_it_was_read(world, tmp_path, capsys):
    """F19."""
    extra = extra_file(tmp_path)
    old = datetime.now(timezone.utc).timestamp() - 3600
    os.utime(extra, (old, old))
    doc = json.loads(run(capsys, *_items_argv(world, tmp_path, "--evidence-extra", str(extra)))[1])
    assert doc["items"][0]["state"] == "unknown" and "producer flow unreadable (stale 60 min)" in doc["items"][0]["reason"]
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra = extra_file(tmp_path, fetched_at=fresh)
    os.utime(extra, (old, old))
    doc = json.loads(run(capsys, *_items_argv(world, tmp_path, "--evidence-extra", str(extra)))[1])
    assert doc["items"][0]["state"] == "ready"           # its own fetched_at is fresh, whatever its file's age


def test_a_repeated_producer_is_one_producer_and_the_native_name_is_refused(world, tmp_path, capsys):
    _producer_file(tmp_path, "acme", waiters=[])
    twice = run(capsys, *_items_argv(world, tmp_path, "--producer", "acme", "--producer", "acme"))
    once = run(capsys, *_items_argv(world, tmp_path, "--producer", "acme"))
    assert twice[1] == once[1]
    (tmp_path / "evidence.acme.json").unlink()
    doc = json.loads(run(capsys, *_items_argv(world, tmp_path, "--producer", "acme", "--producer", "acme"))[1])
    assert doc["items"][0]["reason"].count("producer acme unreadable") == 3          # once for each of the three sources
    rc, out, err = run(capsys, *_items_argv(world, tmp_path, "--producer", "levain"))
    assert rc == 2 and out == "" and "Levain's own producer" in err and "Traceback" not in err


def test_a_store_id_that_cannot_be_read_refuses_the_run_but_a_missing_one_does_not(world, tmp_path, capsys):
    """F10."""
    rc, out, err = run(capsys, *_items_argv(world, tmp_path, "--no-land-waiter"))
    assert rc == 0
    (tmp_path / "store.id").mkdir()
    rc, out, err = run(capsys, *_items_argv(world, tmp_path, "--no-land-waiter"))
    assert rc == 2 and out == "" and "store.id could not be read" in err and "Traceback" not in err


def test_an_empty_unreadable_reason_in_the_extra_is_replaced_not_refused(world, tmp_path, capsys):
    extra = extra_file(tmp_path, unreadable={"seats": "   "})
    rc, doc, err = board(capsys, world, write_items(tmp_path, item(3)), extra)
    assert rc == 3 and doc["items"][0]["state"] == "unknown"
    assert any("seats unreadable: unreadable (no reason given)" in p for p in doc["problems"])


# ------------------------------------------------------------------------------------------------ the fix round 2 (levain#91)
@pytest.mark.parametrize("body", ["", "  \n"])
def test_a_store_id_that_exists_but_is_empty_refuses_the_run(world, tmp_path, capsys, body):
    """K2 (F10): only a missing file means no id; a blank one is a damaged store."""
    (tmp_path / "store.id").write_text(body)
    rc, out, err = run(capsys, *_items_argv(world, tmp_path, "--no-land-waiter"))
    assert rc == 2 and out == "" and "store.id is empty: the store is damaged" in err and "Traceback" not in err


def test_a_flat_extra_without_fetched_at_takes_its_time_from_the_descriptor_it_was_read_from(tmp_path, monkeypatch):
    """K3: one fstat on the open file, never a second stat by pathname."""
    from levain.desk import board_cli

    p = tmp_path / "extra.json"
    p.write_text(json.dumps({"seats": []}))
    os.utime(p, (1_700_000_000, 1_700_000_000))
    import types
    seen = []
    proxy = types.SimpleNamespace(fstat=lambda fd: seen.append(fd) or os.fstat(fd), environ=os.environ)     # no stat by pathname exists on it
    monkeypatch.setattr(board_cli, "os", proxy)
    extra, stamp = board_cli._read_extra(p)
    assert extra == {"seats": []} and stamp == "2023-11-14T22:13:20Z" and len(seen) == 1
    p.write_text(json.dumps({"seats": [], "fetched_at": "2026-11-12T10:00:00Z"}))
    assert board_cli._read_extra(p)[1] == "2026-11-12T10:00:00Z"


def test_attach_producers_gives_each_call_its_own_blocks():
    """K5: the primary and every --also-repo get copies; no block is shared or duplicated across calls."""
    from levain.desk import board_cli

    native = {"seats": {"producer": "levain", "rows": []}}
    external = {"seats": [{"producer": "acme", "rows": []}]}
    first, second = {"seats": []}, {"seats": []}
    board_cli._attach_producers(first, native, external, {})
    board_cli._attach_producers(second, native, external, {})
    for ev in (first, second):
        assert [b["producer"] for b in ev["seats"]] == ["levain", "acme"]
    assert first["seats"][0] is not second["seats"][0] and first["seats"][0] is not native["seats"]
    first["seats"][0]["rows"].append("x")
    assert native["seats"]["rows"] == [] and second["seats"][0]["rows"] == []
    assert external["seats"][0]["rows"] == [] and len(native) == 1 and len(external["seats"]) == 1


def test_attach_producers_copies_the_blocks_already_in_the_evidence_too():
    """M2: a block the extra put in evidence[source] is not shared between the primary and an --also-repo evidence."""
    import copy

    from levain.desk import board_cli

    shared = {"producer": "flow", "rows": []}
    first, second = {"seats": [shared]}, {"seats": [shared]}       # what _merge_extra does: the same parsed block in both
    board_cli._attach_producers(first, {}, {}, {})
    board_cli._attach_producers(second, {}, {}, {})
    assert first["seats"][0] is not second["seats"][0] and first["seats"][0] is not shared
    first["seats"][0]["rows"].append("x")
    assert second["seats"][0]["rows"] == [] and shared["rows"] == []
