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


def make_flow_db(path: Path, tags_for: str = "alpha#1") -> None:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE episodes (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, agent TEXT NOT NULL, type TEXT NOT NULL,"
              " source TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL DEFAULT '')")
    c.execute("INSERT INTO episodes VALUES ('e1', ?, 'flow', 'observation', 'flow-seat', ?, ?, '')",
              (now_stamp(), f"flow side note for {tags_for}", json.dumps({"tags": [f"item:{tags_for}", "repo:alpha"]})))
    c.execute("INSERT INTO episodes VALUES ('e2', ?, 'flow', 'observation', 'flow-seat', 'not tagged', NULL, '')", (now_stamp(),))
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
    assert len(out) // 4 < 10_000 * 2          # the JSON wrapper aside, the parts' text is under 8K tokens
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
    assert "record[anneal]: unreadable (no path named" in out
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
    assert f"record[anneal]: unreadable ({tmp_path / 'nope.db'} is not a file)" in out
    assert f"record[flow]: unavailable ({tmp_path / 'nope2.db'} is not a file)" in out
    # the other parts still print
    assert "--- 1. charter" in out and "- C1: it works" in out


def test_a_flow_file_that_is_not_a_database_is_unavailable_not_a_crash(world, capsys, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("this is not sqlite")
    rc, out, _ = board(capsys, "context", "alpha#1", "--no-land-waiter", "--anneal", str(world.anneal), "--flow-episodes", str(bad))
    assert rc == 0 and "record[flow]: unavailable (" in out and "worked alpha#1 step" in out


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
