"""C13 S1: ``levain coord ready show|add|move|correct``, the writer's CLI verb over ``ready.add/move/correct``. Built from
the first-spend RUN (a scratch store, a lease taken, the three calls from a script, then the same through the CLI):
refusals print plainly, exit 2, and write nothing; ``show`` is a read and needs no lease."""
from __future__ import annotations

import json

import pytest

from levain.cli import main
from levain.coordinator import ready
from levain.coordinator.store import CoordStore


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return CoordStore(qdir)


@pytest.fixture
def token(store):
    return store.coord_take()


def _run(qdir, capsys, *argv):
    code = main(["coord", "ready", *argv, "--queue-dir", str(qdir)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _add(qdir, capsys, token, item="levain:a", *extra):
    return _run(qdir, capsys, "add", item, "--repo", "levain", "--title", "Item A", "--who", "desk", "--why", "queued",
                "--token", str(token), *extra)


def test_add_prints_the_journal_id_and_show_reads_it(qdir, token, store, capsys):
    code, out, err = _add(qdir, capsys, token, "levain:a", "--to", "ready", "--tier", "sonnet", "--done-line", "green")
    assert (code, err) == (0, "") and "journal levain:a@1" in out
    code, out, _ = _run(qdir, capsys, "show")
    assert code == 0 and out.startswith("levain:a  levain  ready  sonnet  rank 1  Item A")
    assert [r["source"] for r in ready.journal(store)] == ["add"]


def test_move_then_correct_journal_each_step(qdir, token, store, capsys):
    _add(qdir, capsys, token, "levain:a", "--to", "ready", "--tier", "sonnet", "--done-line", "green")
    code, out, _ = _run(qdir, capsys, "move", "levain:a", "--to", "running", "--who", "seat", "--why", "it took it",
                        "--token", str(token))
    assert code == 0 and "ready -> running; journal levain:a@2" in out
    code, out, _ = _run(qdir, capsys, "correct", "levain:a", "--to", "ready", "--who", "desk", "--why", "never started",
                        "--token", str(token), "--json")
    got = json.loads(out)
    assert code == 0 and got["journal_id"] == "levain:a@3" and got["record"]["source"] == "correction"
    assert [(r["source"], r["to_category"], r["who"], r["why"]) for r in ready.journal(store)] == [
        ("add", "ready", "desk", "queued"), ("move", "running", "seat", "it took it"),
        ("correction", "ready", "desk", "never started")]


def test_move_changes_the_fields_it_is_given_and_keeps_the_rest(qdir, token, capsys):
    _add(qdir, capsys, token, "levain:a", "--tier", "sonnet")
    _add(qdir, capsys, token, "levain:b")
    code, _, _ = _run(qdir, capsys, "move", "levain:a", "--to", "ready", "--who", "d", "--why", "w", "--token",
                      str(token), "--done-line", "green", "--depends-on", "levain:b")
    assert code == 0
    code, out, _ = _run(qdir, capsys, "show", "--all", "--json")
    a = {i["id"]: i for i in json.loads(out)["items"]}["levain:a"]
    assert (a["tier"], a["done_line"], a["depends_on"], a["declared_category"]) == ("sonnet", "green", ["levain:b"],
                                                                                     "ready")
    assert a["status_category"] == "backlog" and "depends on levain:b" in a["held_reason"]


def _journal_len(store):
    return len(ready.journal(store))


@pytest.mark.parametrize("verb,argv,message", [
    ("move", ["--to", "ready", "--who", "d", "--why", "w"], "needs the coordinator's identity"),             # no token
    ("move", ["--to", "ready", "--who", "d", "--why", "w", "--token", "999"], "is not the live lease's"),    # stale
    ("move", ["--to", "ready", "--who", "d", "--why", ""], "why is required"),
    ("move", ["--to", "ready", "--who", "  ", "--why", "w"], "who is required"),
    ("move", ["--to", "frozen", "--who", "d", "--why", "w"], "unknown category 'frozen'"),
    ("move", ["--to", "needs-you", "--who", "d", "--why", "w"], "needs-you is a projection"),
    ("move", ["--to", "backlog", "--who", "d", "--why", "w"], "already reads so"),
    ("correct", ["--to", "ready", "--who", "d", "--why", "w", "--token", "999"], "is not the live lease's"),
])
def test_refusals_exit_2_print_plainly_and_write_nothing(qdir, token, store, capsys, verb, argv, message):
    _add(qdir, capsys, token)
    before = _journal_len(store)
    if "--token" not in argv and message != "needs the coordinator's identity":
        argv = [*argv, "--token", str(token)]
    code, out, err = _run(qdir, capsys, verb, "levain:a", *argv)
    assert code == 2 and out == "" and message in err and err.startswith(f"levain coord ready {verb}: ")
    assert _journal_len(store) == before


def test_add_refusals(qdir, token, store, capsys):
    assert _add(qdir, capsys, token)[0] == 0
    code, _, err = _add(qdir, capsys, token)
    assert code == 2 and "already exists" in err
    code, _, err = _run(qdir, capsys, "add", "levain:z", "--repo", "levain", "--title", "Z", "--who", "d", "--why", "w")
    assert code == 2 and "needs the coordinator's identity" in err
    code, _, err = _add(qdir, capsys, token, "seat:1")
    assert code == 2 and "projection id" in err
    code, _, err = _add(qdir, capsys, token, "levain:q", "--to", "needs-you")
    assert code == 2 and "projection of the band" in err
    assert _journal_len(store) == 1


def test_move_of_an_unknown_item_says_add_it_first(qdir, token, capsys):
    code, _, err = _run(qdir, capsys, "move", "levain:nope", "--to", "done", "--who", "d", "--why", "w", "--token",
                        str(token))
    assert code == 2 and "add it first" in err


def test_a_lost_lease_refuses_and_writes_nothing(qdir, token, store, capsys):
    _add(qdir, capsys, token)
    store.coord_release(token, by="coordinator")
    code, _, err = _run(qdir, capsys, "move", "levain:a", "--to", "done", "--who", "d", "--why", "w", "--token",
                        str(token))
    assert code == 2 and "no coordinator holds the lease" in err
    assert _journal_len(store) == 1


def test_show_works_with_no_coordinator_live(qdir, token, store, capsys):
    _add(qdir, capsys, token, "levain:a", "--to", "ready", "--tier", "sonnet", "--done-line", "green")
    store.coord_release(token, by="coordinator")
    assert store.coord_status()["lease"] is None
    code, out, err = _run(qdir, capsys, "show")
    assert (code, err) == (0, "") and "levain:a" in out


def test_show_filters_and_says_when_nothing_is_queued(qdir, token, capsys):
    _add(qdir, capsys, token)
    code, out, _ = _run(qdir, capsys, "show")
    assert code == 0 and out.strip() == "nothing queued"          # backlog is not ready
    code, out, _ = _run(qdir, capsys, "show", "--all", "--repo", "flow")
    assert out.strip() == "nothing queued"
    code, out, _ = _run(qdir, capsys, "show", "--all", "--repo", "levain")
    assert "levain:a" in out


def test_show_with_no_store_makes_nothing(tmp_path, capsys):
    q = tmp_path / "never"
    code, out, err = _run(q, capsys, "show")
    assert (code, err) == (0, "") and "no coordinator store here" in out and not q.exists()
    code, out, _ = _run(q, capsys, "show", "--json")
    assert json.loads(out)["items"] == [] and not q.exists()


def test_the_printed_version_is_the_one_the_write_produced_not_a_second_read(qdir, token, store, capsys, monkeypatch):
    seen = []
    real = ready.add

    def spy(*a, **k):
        rec = real(*a, **k)
        seen.append(rec.ver)
        return rec

    monkeypatch.setattr(ready, "add", spy)
    _add(qdir, capsys, token, "levain:a")
    code, out, err = _add(qdir, capsys, token, "levain:b")
    assert (code, err) == (0, "") and seen == [1, 1]
    assert f"journal levain:b@{seen[-1]}" in out
    # a second write moves the version on; the printed one must follow the write's own record, not a constant
    code, out, _ = _run(qdir, capsys, "move", "levain:a", "--to", "ready", "--who", "d", "--why", "w", "--token",
                        str(token))
    assert "journal levain:a@2" in out


def test_show_never_says_nothing_queued_beside_an_unreadable_store(qdir, token, store, capsys):
    import sqlite3
    _add(qdir, capsys, token)
    c = sqlite3.connect(qdir / "dispatch.db")
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'ready_items%'").fetchall():
        c.execute(f"DROP TRIGGER {name}")
    c.execute("DELETE FROM ready_items")
    c.commit()
    c.close()
    code, out, _ = _run(qdir, capsys, "show")
    assert "unreadable: " in out and "nothing queued" not in out


def test_a_stale_token_on_a_missing_queue_dir_says_no_state_was_written(tmp_path, capsys):
    q = tmp_path / "never"
    code, out, err = _run(q, capsys, "move", "levain:a", "--to", "done", "--who", "d", "--why", "w", "--token", "5")
    assert code == 2 and out == "" and err.rstrip().endswith("; no state was written")
    assert "nothing was written" not in err


def test_a_lifecycle_write_on_a_missing_store_makes_nothing(tmp_path, capsys):
    q = tmp_path / "never"
    for argv in (("add", "levain:a", "--repo", "levain", "--title", "A"),
                 ("move", "levain:a", "--to", "done"), ("correct", "levain:a", "--to", "done")):
        code, out, err = _run(q, capsys, *argv, "--who", "d", "--why", "w", "--token", "1")
        assert code == 2 and out == "" and "no coordinator store at" in err and err.rstrip().endswith("no state was written")
        assert not q.exists()


def test_a_write_with_no_token_says_no_state_was_written(qdir, token, capsys):
    code, _, err = _run(qdir, capsys, "move", "levain:a", "--to", "done", "--who", "d", "--why", "w")
    assert code == 2 and "no state was written" in err and "nothing was written" not in err


def test_no_tier_and_no_done_line_clear_and_omitting_them_keeps(qdir, token, capsys):
    _add(qdir, capsys, token, "levain:a", "--tier", "sonnet", "--done-line", "green")

    def item():
        _, out, _ = _run(qdir, capsys, "show", "--all", "--json")
        return {i["id"]: i for i in json.loads(out)["items"]}["levain:a"]

    code, _, _ = _run(qdir, capsys, "move", "levain:a", "--to", "ready", "--who", "d", "--why", "w", "--token", str(token))
    assert code == 0 and (item()["tier"], item()["done_line"]) == ("sonnet", "green")      # omitted: unchanged
    code, _, _ = _run(qdir, capsys, "correct", "levain:a", "--to", "backlog", "--who", "d", "--why", "w", "--token",
                      str(token), "--no-tier", "--no-done-line")
    assert code == 0 and (item()["tier"], item()["done_line"]) == (None, None)


@pytest.mark.parametrize("a,b", [("--no-tier", "--tier"), ("--no-done-line", "--done-line")])
def test_the_clearing_flags_are_exclusive_with_their_setters(qdir, token, capsys, a, b):
    with pytest.raises(SystemExit):
        main(["coord", "ready", "move", "levain:a", "--to", "done", a, b, "x", "--queue-dir", str(qdir)])
    assert "not allowed with" in capsys.readouterr().err


def test_move_with_a_title_changes_the_title(qdir, token, capsys):
    _add(qdir, capsys, token, "levain:a")
    code, _, err = _run(qdir, capsys, "move", "levain:a", "--to", "ready", "--who", "d", "--why", "w", "--token",
                        str(token), "--title", "Renamed")
    assert (code, err) == (0, "")
    _, out, _ = _run(qdir, capsys, "show", "--json")
    assert [i["title"] for i in json.loads(out)["items"]] == ["Renamed"]


def test_show_json_has_the_same_keys_with_and_without_a_store(tmp_path, qdir, token, capsys):
    _add(qdir, capsys, token)
    _, present, _ = _run(qdir, capsys, "show", "--json")
    _, absent, _ = _run(tmp_path / "never", capsys, "show", "--json")
    assert set(json.loads(present)) == set(json.loads(absent)) and json.loads(absent)["store"] is None and json.loads(present)["store"]
