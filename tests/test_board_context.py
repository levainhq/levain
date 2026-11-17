"""``levain board context NAME#N`` (T9): the five parts, the bound, each `unreadable` path, the read-only flow source, a refused id.

The world is a scratch levain home, a scratch repo with a bare origin, registered and tracked through the real verbs with a
CHARTER.md on its origin/main, a scratch anneal store and a scratch copy of an episodic.db (never flow's own file)."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.cli import main
from levain.desk import board_context as bc

A = ["--by", "desk 1", "--author-kind", "operator"]
CHARTER = """# Charter: alpha
Charter-Version: 1
Export: repo

## Purpose
Test.

## Criteria
- C1: it works [judged: phill]

## Constraints
- K1: none

## Plan
{plan}
"""


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env).stdout.decode().strip()


def board(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def now_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def make_repo(root: Path, name: str) -> Path:
    bare = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    d = root / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    (d / "README.md").write_text("hi\n")
    (d / "AGENTS.md").write_text("a\n")
    git(d, "add", ".")
    git(d, "commit", "-qm", "init")
    git(d, "remote", "add", "origin", str(bare))
    git(d, "push", "-q", "origin", "main")
    git(d, "fetch", "-q")
    return d


FLOW_SCHEMA = """
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    agent TEXT NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_episodes_timestamp ON episodes(timestamp);
CREATE TABLE episode_tags (
    episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (episode_id, tag)
);
CREATE INDEX idx_tags_tag ON episode_tags(tag);
"""  # copied from flow's real episodic.db (.schema episodes, .schema episode_tags), read-only


def flow_add(c, eid, ts, typ, content, tags=()):
    c.execute("INSERT INTO episodes (id, timestamp, agent, type, source, content, metadata) VALUES (?, ?, 'flow', ?, 'flow-seat', ?, NULL)",
              (eid, ts, typ, content))
    for t in tags:
        c.execute("INSERT INTO episode_tags VALUES (?, ?)", (eid, t))


def make_flow_db(path: Path, tags_for: str = "alpha#1") -> None:
    c = sqlite3.connect(path)
    c.executescript(FLOW_SCHEMA)
    flow_add(c, "e1", now_stamp(), "observation", f"flow side note for {tags_for}", (f"item:{tags_for}", "repo:alpha"))
    flow_add(c, "e2", now_stamp(), "observation", "not tagged")
    flow_add(c, "e3", now_stamp(), "decision", "flow repo decision", ("repo:alpha",))
    flow_add(c, "e4", now_stamp(), "observation", "other item note", ("item:beta#9",))
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S-05:00")
    flow_add(c, "e5", old, "observation", "ancient tagged note", (f"item:{tags_for}",))
    c.commit()
    c.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    for k in ("LEVAIN_BOARD_ITEMS", "LEVAIN_ANNEAL_DB", "LEVAIN_FLOW_EPISODES", "LEVAIN_LAUNCH_KEY"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "w"
    root.mkdir()
    repo = make_repo(root, "alpha")
    store = root / "store"
    assert main(["board", "use", str(store)]) == 0
    (store / "repos.jsonl").touch()
    assert main(["board", "register", "alpha", "--path", str(repo), "--words", "first", *A]) == 0
    man = root / "m.json"
    man.write_text(json.dumps({"plan_items": ["a", "b"], "contract_items": []}))
    assert main(["board", "begin-track", "alpha", "--manifest", str(man), "--words", "yes", *A]) == 0
    recs = [json.loads(ln) for ln in (store / "repos.jsonl").read_text().splitlines()]
    mig = [r for r in recs if r["op"] == "begin-track"][-1]["migration"]
    for k, t in (("a", "first thing"), ("b", "second thing")):
        assert main(["board", "add", "--repo", "alpha", "--title", t, "--origin", "desk 1", "--words", f"do the {k}", "--done", "it is done",
                     "--key", f"{mig}:{k}", "--payload-hash", "a" * 64, *A]) == 0
    (repo / "CHARTER.md").write_text(CHARTER.format(plan="- alpha#1\n- alpha#2"))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "charter")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")
    assert main(["board", "track", "alpha", "--words", "go", *A]) == 0
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "work\n\nBoard-Item: alpha#1")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")

    from anneal_memory import Store
    anneal = root / "anneal.db"
    s = Store(anneal)
    s.record("worked alpha#1 step", "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.record("chose approach X", "decision", source="seat-a", metadata={"tags": ["repo:alpha"]})
    s.record("elsewhere", "observation", source="seat-b", metadata={"tags": ["repo:beta"]})
    s.close()
    flow = root / "flow.db"
    make_flow_db(flow)
    capsys_args = ["--no-land-waiter", "--anneal", str(anneal), "--flow-episodes", str(flow)]
    return SimpleNamespace(root=root, repo=repo, store=store, anneal=anneal, flow=flow, args=capsys_args)


def test_the_five_parts_in_order(world, capsys):
    rc, out, err = board(capsys, "context", "alpha#1", *world.args)
    assert rc == 0, err
    heads = [ln for ln in out.splitlines() if ln.startswith("--- ")]
    assert heads == ["--- 1. charter", "--- 2. item", "--- 3. state", "--- 4. record", "--- 5. conventions"]
    assert "## Purpose\nTest." in out and "- C1: it works" in out and "- K1: none" in out
    assert "- alpha#1  landed_not_closed  first thing" in out and "- alpha#2  needs_grooming  second thing" in out
    assert "words: do the a" in out and "origin: desk 1" in out and "done: it is done  done_kind: merged" in out
    assert "depends on: nothing" in out
    assert "state: landed_not_closed" in out and "commits on main: 1" in out
    assert "worked alpha#1 step" in out and "chose approach X" in out and "elsewhere" not in out
    assert "flow side note for alpha#1" in out and "not tagged" not in out
    assert "flow repo decision" in out and "other item note" not in out and "ancient tagged note" not in out
    assert "record[artifacts]: unavailable (no record directory)" in out
    assert "conventions: AGENTS.md" in out
    assert "untracked" not in out


def test_json_carries_the_same_parts(world, capsys):
    rc, out, _ = board(capsys, "context", "alpha#2", "--json", *world.args)
    doc = json.loads(out)
    assert rc == 0 and [p["part"] for p in doc["parts"]] == ["charter", "item", "state", "record", "conventions"]
    assert doc["untracked"] is False and doc["item"] == "alpha#2"


def test_a_dependency_is_shown_with_its_derived_state(world, capsys):
    assert main(["board", "add", "--repo", "alpha", "--title", "third", "--origin", "desk 1", "--depends", "item:alpha#1", *A,
                 "--key", "k:1", "--payload-hash", "b" * 64]) == 0
    capsys.readouterr()
    rc, out, _ = board(capsys, "context", "alpha#3", *world.args)
    assert "depends on: alpha#1: landed_not_closed" in out, out


def test_the_bound_and_truncated(world, capsys):
    from anneal_memory import Store
    s = Store(world.anneal)
    for i in range(40):
        s.record(f"long {i} " + "word " * 200, "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.close()
    big = "\n".join(f"- alpha#{n}" for n in range(1, 400))
    (world.repo / "CHARTER.md").write_text(CHARTER.format(plan=big))
    git(world.repo, "add", ".")
    git(world.repo, "commit", "-qm", "big plan")
    git(world.repo, "push", "-q", "origin", "main")
    git(world.repo, "fetch", "-q")
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", *world.args)
    doc = json.loads(out)
    parts = {p["part"]: p for p in doc["parts"]}
    for name, p in parts.items():
        assert sum(len(ln) + 1 for ln in p["lines"]) <= bc.PART_CHARS[name], name
    assert sum(bc.PART_CHARS.values()) <= bc.TOTAL_CHARS
    assert doc["total_chars"] <= bc.TOTAL_CHARS
    # the item cap (10 of 40 matches) is not a scan-bound truncation; the plan is, by the part budget
    assert parts["charter"]["truncated"] and any("[truncated" in ln for ln in parts["charter"]["lines"])
    assert parts["record"]["lines"] and sum(len(ln) + 1 for ln in parts["record"]["lines"]) <= bc.PART_CHARS["record"]
    text = sum(len(ln) + 1 for p in doc["parts"] for ln in p["lines"])
    assert text <= bc.TOTAL_CHARS


def test_the_record_part_says_truncated_when_the_part_budget_cuts_it(world, capsys, monkeypatch):
    monkeypatch.setitem(bc.PART_CHARS, "record", 400)
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", *world.args)
    rec = [p for p in json.loads(out)["parts"] if p["part"] == "record"][0]
    assert rec["truncated"] and any(ln.startswith("[truncated") for ln in rec["lines"])


def test_the_scan_bound_says_truncated(world, capsys, monkeypatch):
    monkeypatch.setattr(bc, "MAX_SCAN", 1)
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert "[truncated: the scan bound was hit" in out


def test_no_charter_is_unreadable_and_an_untracked_repo_is_said(world, tmp_path, capsys):
    other = make_repo(world.root, "beta")
    assert main(["board", "register", "beta", "--path", str(other), "--words", "first", *A]) == 0
    assert main(["board", "begin-track", "beta", "--manifest", str(world.root / "m.json"), "--words", "yes", *A]) == 0
    capsys.readouterr()
    recs = [json.loads(ln) for ln in (world.store / "repos.jsonl").read_text().splitlines()]
    mig = [r for r in recs if r["op"] == "begin-track" and r["repo"] == "beta"][-1]["migration"]
    assert main(["board", "add", "--repo", "beta", "--title", "b one", "--origin", "desk 1", "--key", f"{mig}:a", "--payload-hash", "c" * 64, *A]) == 0
    capsys.readouterr()
    rc, out, err = board(capsys, "context", "beta#1", "--no-land-waiter")
    assert rc == 0, err
    assert out.splitlines()[0].startswith("repo beta is untracked (mode tracking)")
    assert "charter: unreadable (no CHARTER.md on origin/main)" in out
    assert "record[anneal]: unavailable (no path named" in out
    assert "record[flow]: unavailable (no path named" in out
    assert "item beta#1: b one" in out


def test_a_repo_missing_from_the_registry_says_so(world, capsys):
    (world.store / "repos.jsonl").write_text("")
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter")
    assert rc == 0
    assert out.splitlines()[0].startswith("repo alpha is untracked: alpha is not in the registry")
    assert "charter: unreadable (alpha is not in the registry)" in out
    assert "state: unreadable (alpha is not in the registry; pass --repo PATH to read git)" in out
    assert "conventions: unreadable" in out


def test_no_anneal_store_and_no_flow_db(world, capsys, tmp_path):
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(tmp_path / "nope.db"),
                       "--flow-episodes", str(tmp_path / "nope2.db"))
    assert rc == 0
    assert f"record[anneal]: unreadable ({tmp_path / 'nope.db'} is not a file)" in out      # named and failed: unreadable
    assert f"record[flow]: unreadable ({tmp_path / 'nope2.db'} is not a file)" in out
    # the other parts still print
    assert "--- 1. charter" in out and "- C1: it works" in out


def test_a_flow_file_that_is_not_a_database_is_unreadable_not_a_crash(world, capsys, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("this is not sqlite")
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal), "--flow-episodes", str(bad))
    assert rc == 0 and "record[flow]: unreadable (" in out and "worked alpha#1 step" in out


def test_the_flow_db_is_opened_read_only(world, capsys):
    before_bytes = hashlib.sha256(world.flow.read_bytes()).hexdigest()
    before = os.stat(world.flow)
    os.utime(world.flow, ns=(before.st_atime_ns, before.st_mtime_ns))
    before = os.stat(world.flow)
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert "flow side note for alpha#1" in out
    after = os.stat(world.flow)
    assert hashlib.sha256(world.flow.read_bytes()).hexdigest() == before_bytes
    assert after.st_mtime_ns == before.st_mtime_ns and after.st_size == before.st_size
    # the connection itself refuses a write
    c = sqlite3.connect("file:" + str(world.flow) + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM episodes")
    c.close()


def test_the_flow_db_in_wal_mode_is_read_without_writing_the_file(world, capsys):
    c = sqlite3.connect(world.flow)
    c.execute("PRAGMA journal_mode=WAL")
    c.commit()
    c.close()
    before = hashlib.sha256(world.flow.read_bytes()).hexdigest(), os.stat(world.flow).st_mtime_ns
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert "flow side note for alpha#1" in out
    assert (hashlib.sha256(world.flow.read_bytes()).hexdigest(), os.stat(world.flow).st_mtime_ns) == before


def test_an_unknown_id_and_a_bad_id_are_refused(world, capsys):
    rc, out, err = board(capsys, "context", "alpha#99", "--no-land-waiter")
    assert rc == 2 and "no item alpha#99 in the store" in err and out == ""
    rc, out, err = board(capsys, "context", "alpha-1")
    assert rc == 2 and "alpha-1" in err


def test_artifacts_by_name_only(tmp_path):
    rec = tmp_path / "rec"
    (rec / "pre-track-2026-11-17").mkdir(parents=True)
    (rec / "2026-11-17_alpha-1_notes.md").write_text("secret body")
    (rec / "2026-11-17_alpha-2_notes.md").write_text("x")
    (rec / "undated_alpha-1.md").write_text("x")
    out = bc._artifact_lines(SimpleNamespace(record_dir=str(rec)), "alpha#1")
    assert out[1:] == ["  2026-11-17_alpha-1_notes.md", "  pre-track-2026-11-17"]
    assert "secret body" not in "\n".join(out)
    assert bc._artifact_lines(SimpleNamespace(record_dir=str(tmp_path / "gone")), "alpha#1")[0].startswith("record[artifacts]: unreadable (")
    assert bc._artifact_lines(SimpleNamespace(record_dir=None), "alpha#1") == ["record[artifacts]: unavailable (no record directory)"]


def test_context_does_not_edit_run_state():
    import inspect
    from levain.desk import board_cli
    assert "board_context" not in inspect.getsource(board_cli.run_state)


# ------------------------------------------------------------------------------------------------- fix 2 (L1/L2 rulings)


def test_cap_clips_a_long_line_and_never_evicts_the_lines_after_it():
    out, cut = bc._cap(["x" * 20000], 9000)
    assert cut and len(out) == 1 and out[0].startswith("xxx") and "[clipped " in out[0] and len(out[0]) <= 9000
    out, _ = bc._cap(["x" * 20000, "after", "done: kept"], 9000)
    assert out[-2:] == ["after", "done: kept"] and "[clipped " in out[0]


def test_long_words_never_evict_the_done_line_or_dependencies(world, capsys):
    assert main(["board", "add", "--repo", "alpha", "--title", "third", "--origin", "desk 1", "--depends", "item:alpha#1",
                 "--words", "w" * 20000, "--done", "the real done", *A, "--key", "k:2", "--payload-hash", "d" * 64]) == 0
    capsys.readouterr()
    rc, out, _ = board(capsys, "context", "alpha#3", *world.args)
    assert "[clipped " in out and "done: the real done  done_kind:" in out and "depends on: alpha#1: landed_not_closed" in out


def test_the_whole_page_is_checked_against_the_total(world, capsys, monkeypatch):
    from anneal_memory import Store
    s = Store(world.anneal)
    for i in range(40):
        s.record(f"long {i} " + "word " * 200, "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.close()
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert len(out.rstrip("\n")) <= bc.TOTAL_CHARS
    monkeypatch.setattr(bc, "TOTAL_CHARS", 3000)          # the final check cuts, whatever the part caps say
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert len(out.rstrip("\n")) <= 3000 and "--- 5. conventions" in out
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", *world.args)
    assert json.loads(out)["total_chars"] <= 3000


def test_one_record_source_cannot_evict_the_other_or_the_artifacts(world, capsys):
    from anneal_memory import Store
    s = Store(world.anneal)
    for i in range(40):
        s.record(f"anneal {i} " + "word " * 200, "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.close()
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", *world.args)
    lines = [p for p in json.loads(out)["parts"] if p["part"] == "record"][0]["lines"]
    assert any(ln.startswith("record[flow]:") for ln in lines) and any("flow side note" in ln for ln in lines)
    assert any(ln.startswith("record[artifacts]:") for ln in lines)


def test_an_episode_in_both_stores_or_both_sections_prints_once(world, capsys):
    from anneal_memory import Store
    s = Store(world.anneal)
    s.record("flow side note for alpha#1", "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.record("tagged and a decision", "decision", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.close()
    rc, out, _ = board(capsys, "context", "alpha#1", *world.args)
    assert out.count("flow side note for alpha#1") == 1 and out.count("tagged and a decision") == 1


def test_a_cap_that_drops_matches_prints_how_many_more(world, capsys):
    from anneal_memory import Store
    s = Store(world.anneal)
    for i in range(14):
        s.record(f"n{i}", "observation", source="seat-a", metadata={"tags": ["repo:alpha", "item:alpha#1"]})
    s.close()
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal))
    assert "  [5 more]" in out          # 15 distinct matches (14 + the fixture's), 10 shown


def test_flow_window_and_order_are_chronological_across_offsets(tmp_path):
    now = datetime.datetime(2026, 11, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)
    d = datetime.timedelta

    def at(delta, offset_h):
        tz = datetime.timezone(d(hours=offset_h))
        return (now + delta).astimezone(tz).isoformat().replace("+00:00", "Z")
    db = tmp_path / "f.db"
    c = sqlite3.connect(db)
    c.executescript(FLOW_SCHEMA)
    flow_add(c, "a", at(d(days=-10), 14), "observation", "a +14")                       # instant -10d
    flow_add(c, "b", at(d(days=-10, hours=2), 0), "observation", "b Z")                 # instant -10d+2h
    flow_add(c, "c", at(d(days=-10, hours=1), -5), "observation", "c -05")              # instant -10d+1h
    flow_add(c, "old", at(d(days=-90, hours=-1), 14), "observation", "old +14")         # just outside the window
    flow_add(c, "edge", at(d(days=-90, hours=1), -5), "observation", "edge -05")        # just inside, string-wise far outside
    flow_add(c, "future", at(d(hours=3), 0), "observation", "future")                   # after now
    c.commit()
    c.close()
    rows, total, bad = bc._read_flow(db, now)
    assert [r.content for r in rows] == ["b Z", "c -05", "a +14", "edge -05"] and total == 4 and bad == 0


def test_flow_read_failure_is_unreadable_with_no_second_attempt_and_no_stale_rows(world, capsys):
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind root")
    d = world.root / "wal"
    d.mkdir()
    db = d / "f.db"
    make_flow_db(db)
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.commit()
    c.close()
    for side in ("-wal", "-shm"):
        (d / ("f.db" + side)).unlink(missing_ok=True)      # no side files, and the directory cannot make them
    os.chmod(d, 0o555)
    try:
        rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal), "--flow-episodes", str(db))
    finally:
        os.chmod(d, 0o755)
    assert rc == 0 and "record[flow]: unreadable (" in out and "flow side note" not in out
    assert "worked alpha#1 step" in out          # the other source still reads


def test_an_empty_flag_is_honoured_and_unconfigured_is_unavailable(world, capsys, monkeypatch):
    monkeypatch.setenv("LEVAIN_FLOW_EPISODES", str(world.flow))
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal), "--flow-episodes", "")
    assert "record[flow]: unreadable (an empty path was named)" in out and "flow side note" not in out
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal))
    assert "flow side note for alpha#1" in out                     # no flag: the environment
    monkeypatch.delenv("LEVAIN_FLOW_EPISODES")
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal))
    assert "record[flow]: unavailable (no path named" in out


# ------------------------------------------------------------------------------------------------------------- fix 3 (L3 r1)


def test_f3_1_item_id_grammar_is_the_stores(world, capsys):
    from levain.desk import board_store as bs
    assert bc._ID is bs.ID_RE
    rc, out, err = board(capsys, "context", "alpha#01", *world.args)
    assert rc == 2 and "not an item id" in err
    rc, out, err = board(capsys, "context", "3d.x#1", "--no-land-waiter")       # valid grammar (digit lead, dot): reaches the store lookup
    assert rc == 2 and "no item 3d.x#1 in the store" in err


def test_f3_2_non_utc_now_is_normalised(world, capsys):
    rc, out, err = board(capsys, "context", "alpha#1", "--now", "2026-11-17T12:00:00-05:00", *world.args)
    assert rc == 0, err
    assert bc._window(datetime.datetime(2026, 11, 17, 17, 0, tzinfo=datetime.timezone.utc)).startswith("2026-08-19T17:00:00")
    # the run itself: an episode at 16:30Z (11:30-05:00) is inside, one at 18:00Z is after the instant 17:00Z
    c = sqlite3.connect(world.flow)
    flow_add(c, "n1", "2026-11-17T16:30:00Z", "observation", "inside nonutc", ("item:alpha#1",))
    flow_add(c, "n2", "2026-11-17T18:00:00Z", "observation", "after nonutc", ("item:alpha#1",))
    c.commit()
    c.close()
    rc, out, err = board(capsys, "context", "alpha#1", "--now", "2026-11-17T12:00:00-05:00", *world.args)
    assert "inside nonutc" in out and "after nonutc" not in out


def test_f3_3_an_item_form_does_not_match_a_longer_number(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    for n in ("2026-11-17_alpha-10_x.md", "2026-11-17_alpha_12.md", "2026-11-17_alpha#100.md", "2026-11-17_alpha-1.md", "2026-11-17_alpha-1_x.md"):
        (rec / n).write_text("x")
    out = bc._artifact_lines(SimpleNamespace(record_dir=str(rec)), "alpha#1")
    assert out[1:] == ["  2026-11-17_alpha-1.md", "  2026-11-17_alpha-1_x.md"]


def test_f3_4_one_part_failing_costs_only_that_part(world, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(bc, "_item_part", boom)
    rc, out, err = board(capsys, "context", "alpha#1", *world.args)
    assert rc == 0 and "item: unreadable (RuntimeError: kaput)" in out
    assert "- C1: it works" in out and "state: landed_not_closed" in out and "conventions: AGENTS.md" in out


def test_f3_4_a_bad_record_dir_and_a_none_title_do_not_crash(world, capsys, tmp_path):
    item = SimpleNamespace(id="alpha#1", title=None, words=None, origin=None, done=None, done_kind=None, decision=None, depends_on=[])
    p = bc._item_part(item, None)
    assert "None" not in "\n".join(p.lines) and "item alpha#1: —" in p.lines[0]
    part = bc._guarded("record", lambda: bc._record_part(SimpleNamespace(record_dir="bad\x00dir"), "alpha", "alpha#1",
                                                          SimpleNamespace(anneal=None, flow_episodes=None), datetime.datetime.now(datetime.timezone.utc)))
    assert part.status in ("unreadable", "unavailable")


def test_f3_5_no_source_named_is_unavailable_a_failed_named_one_is_unreadable(world, capsys, tmp_path):
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", "--no-land-waiter")
    rec = [p for p in json.loads(out)["parts"] if p["part"] == "record"][0]
    assert rec["status"] == "unavailable"
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", "--no-land-waiter", "--anneal", str(tmp_path / "nope.db"))
    rec = [p for p in json.loads(out)["parts"] if p["part"] == "record"][0]
    assert rec["status"] == "unreadable"


def test_f3_6_repo_flag_is_labelled(world, capsys):
    store = world.store
    (store / "repos.jsonl").rename(store / "repos.jsonl.off")
    (store / "repos.jsonl").touch()
    rc, out, err = board(capsys, "context", "alpha#1", "--repo", str(world.repo), "--no-land-waiter")
    assert rc == 0, err
    assert f"git evidence was read from {world.repo}, given by --repo; the path's identity is not checked" in out


def test_f3_7_render_fixes():
    out, cut = bc._cap(["x" * 20000], 150)
    assert not any("0 more line" in ln for ln in out)
    lines = [f"line {i} " + "z" * 80 for i in range(40)]
    once, _ = bc._cap(lines, 1500)
    twice, _ = bc._cap(once, 700)
    assert sum(1 for ln in twice if ln.startswith("[truncated: ")) == 1
    first = [ln for ln in once if ln.startswith("[truncated: ")][0]
    kept_once = len(once) - 1
    n_twice = int(twice[-1].split()[1])
    assert n_twice == (len(lines) - kept_once) + (kept_once - (len(twice) - 1))
    for n in (0, 1, 5, 20):
        assert len(bc._clip_marked("y" * 100, n)) <= n
    assert bc._s(None) == "—"


def test_f3_7_last_resort_slice_ends_in_a_marker_inside_the_bound(monkeypatch):
    monkeypatch.setattr(bc, "TOTAL_CHARS", 120)
    parts = [bc.Part("a", ["a"]), bc.Part("b", ["b"])]
    monkeypatch.setattr(bc, "_cap", lambda lines, budget: (lines, False))      # cannot shrink: the fallback must hold the bound
    text = bc._enforce_total("m" * 300, parts)
    assert len(text) <= 120 and text.endswith("[truncated: page bound]")


def test_f3_8_anneal_total_matching_is_pre_limit(tmp_path):
    from anneal_memory import Store
    s = Store(tmp_path / "a.db")
    for i in range(12):
        s.record(f"ep {i}", "observation", source="x", metadata={"tags": ["a"]})
    r = s.recall(limit=5)
    s.close()
    assert len(r.episodes) == 5 and r.total_matching == 12


def test_f4_1_a_none_dependency_label_is_a_dash():
    link = SimpleNamespace(kind="gate", ref=None, label=None)
    item = SimpleNamespace(id="alpha#1", title="t", words="w", origin="o", done="d", done_kind="k", decision=None, depends_on=[link])
    p = bc._item_part(item, None)
    assert "depends on: —" in p.lines and "None" not in "\n".join(p.lines)


def test_f4_2_a_bad_user_path_costs_only_its_source(world, capsys):
    now = datetime.datetime.now(datetime.timezone.utc)
    for bad in ("~nosuchuser_zz/x", "a\x00b"):
        lines, cut, read = bc._source_lines("flow", bad, "X", bc._read_flow, "alpha#1", "alpha", now, set())
        assert not read and lines[0].startswith("record[flow]: unreadable (")
        assert bc._artifact_lines(SimpleNamespace(record_dir=bad), "alpha#1")[0].startswith("record[artifacts]: unreadable (")
    rc, out, err = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal), "--flow-episodes", "~nosuchuser_zz/x")
    assert rc == 0, err
    assert "record[flow]: unreadable (" in out and "record[anneal]:" in out and "worked alpha#1 step" in out


def test_f4_2_a_bad_repo_flag_is_the_state_parts_unreadable(world, capsys):
    store = world.store
    (store / "repos.jsonl").rename(store / "repos.jsonl.off")
    (store / "repos.jsonl").touch()
    rc, out, err = board(capsys, "context", "alpha#1", "--repo", "~nosuchuser_zz/x", "--no-land-waiter", "--anneal", str(world.anneal))
    assert rc == 0, err
    assert "state: unreadable (--repo cannot be used (RuntimeError" in out and "worked alpha#1 step" in out


def test_f4_3_a_failed_named_source_is_named_while_the_other_reads(world, capsys, tmp_path):
    rc, out, _ = board(capsys, "context", "alpha#1", "--json", "--no-land-waiter", "--anneal", str(world.anneal),
                       "--flow-episodes", str(tmp_path / "nope.db"))
    rec = [p for p in json.loads(out)["parts"] if p["part"] == "record"][0]
    assert rec["status"] == "ok" and "record[flow]" in rec["why"] and "unreadable" in rec["why"] and "anneal" not in rec["why"]


def test_f4_4_rows_dropped_for_bad_metadata_do_not_claim_the_scan_bound():
    rows = [bc._Ep("e1", "2026-11-17T00:00:00Z", "observation", "s", "c", ("item:alpha#1",))]
    lines, cut = bc._select(rows, 3, "alpha#1", "alpha", 2)
    assert not cut and not any("scan bound" in ln for ln in lines)
    lines, cut = bc._select(rows, 3, "alpha#1", "alpha", 1)
    assert cut


def test_f4_5_an_artifact_form_needs_a_leading_boundary(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    for n in ("2026-11-17_xalpha-1.md", "2026-11-17_xalpha_1.md", "2026-11-17_alpha-1.md"):
        (rec / n).write_text("x")
    out = bc._artifact_lines(SimpleNamespace(record_dir=str(rec)), "alpha#1")
    assert out[1:] == ["  2026-11-17_alpha-1.md"]


def test_f4_6_a_naive_now_is_utc(world, capsys):
    c = sqlite3.connect(world.flow)
    flow_add(c, "m1", "2026-11-17T16:30:00Z", "observation", "inside naive", ("item:alpha#1",))
    flow_add(c, "m2", "2026-11-17T18:00:00Z", "observation", "after naive", ("item:alpha#1",))
    c.commit()
    c.close()
    rc, out, err = board(capsys, "context", "alpha#1", "--now", "2026-11-17T17:00:00", *world.args)
    assert rc == 0, err
    assert "inside naive" in out and "after naive" not in out
