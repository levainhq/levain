"""A state line SET writes one felt episode into the substrate's own episodic store.

Built from project_memory/1020-stateep/repro_before.txt (every write path set the line and left the
store at 0 episodes) and repro_after.txt (the same run after the fix). ``write_state`` is the one
place that computes "a line was set"; these tests drive the real entry points through it.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from anneal_memory import Store

from levain import cli
from levain import dashboard
from levain.dashboard import AnnealPaths, run_state, write_state
from levain.writes import EditError, WriteScope, apply_edit

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """An install root whose episodic store exists and is empty, as `levain init` leaves it."""
    (tmp_path / ".levain").mkdir()
    with Store(str(tmp_path / ".levain" / "memory.db")):
        pass
    return tmp_path


def _episodes(root: Path) -> list:
    with Store(str(root / ".levain" / "memory.db"), read_only=True) as store:
        return store.recall(limit=1000).episodes


def _set(root: Path, text: str, **extra) -> dict:
    return apply_edit(WriteScope.from_install_root(root), {"kind": "operator_state", "text": text, **extra})


def _line(root: Path) -> dict:
    return json.loads((root / ".levain" / "context.json").read_text(encoding="utf-8"))


def test_a_set_writes_one_felt_state_observation_with_the_verbatim_line(root: Path) -> None:
    res = _set(root, "Picking Som up  from work", source="tui")
    assert res["episode"] == "written" and "episode_error" not in res
    (ep,) = _episodes(root)
    assert ep.type.value == "observation"
    assert ep.content == "Picking Som up from work"      # the stored line: whitespace collapsed, nothing else
    assert ep.content == _line(root)["state"]
    assert ep.metadata["tags"] == ["felt", "state"] and ep.metadata["state_source"] == "tui"


def test_the_same_call_twice_writes_one_episode_and_refreshes_the_stamp(root: Path) -> None:
    _set(root, "Dinner and some light flow work")
    first = _line(root)["state_set_at"]
    res = _set(root, "Dinner and some light flow work")
    assert res["episode"] == "unchanged" and len(_episodes(root)) == 1
    assert _set(root, "Dinner and some light flow work")["episode"] == "unchanged" and len(_episodes(root)) == 1
    assert _line(root)["state_set_at"] >= first and _line(root)["state"] == "Dinner and some light flow work"


def test_each_changed_line_is_its_own_episode(root: Path) -> None:
    for text in ("one", "two", "one"):
        _set(root, text)
    assert sorted(e.content for e in _episodes(root)) == ["one", "one", "two"]


def test_a_clear_writes_no_episode(root: Path) -> None:
    _set(root, "something")
    res = _set(root, "   ")
    assert res["episode"] == "cleared" and len(_episodes(root)) == 1


def test_a_cleared_line_set_again_is_a_new_setting(root: Path) -> None:
    _set(root, "back again")
    _set(root, "")
    assert _set(root, "back again")["episode"] == "written" and len(_episodes(root)) == 2


def test_an_expired_line_set_again_is_a_new_setting(root: Path) -> None:
    ctx = root / ".levain" / "context.json"
    ctx.write_text(json.dumps({"state": "stale", "state_set_at": "2020-01-01T00:00:00+00:00",
                               "state_source": "web"}), encoding="utf-8")
    assert _set(root, "stale")["episode"] == "written"


def test_concurrent_identical_sets_write_one_episode(root: Path) -> None:
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(_set(root, "same words"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["episode"] for r in results) == ["unchanged"] * 5 + ["written"]
    assert len(_episodes(root)) == 1


def test_levain_state_cli_writes_the_episode(root: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["state", "--path", str(root), "on the billing refactor"]) == 0
    assert [e.content for e in _episodes(root)] == ["on the billing refactor"]
    assert _episodes(root)[0].metadata["state_source"] == "cli"
    assert cli.main(["state", "--path", str(root), "on the billing refactor"]) == 0     # a re-set
    assert cli.main(["state", "--path", str(root), "--clear"]) == 0
    assert len(_episodes(root)) == 1
    capsys.readouterr()


def test_the_cockpit_verb_writes_the_episode_and_a_stale_compare_writes_neither(root: Path) -> None:
    from levain.cockpit.verbs import _operator_state_fire

    scope = WriteScope.from_install_root(root)

    def stale() -> None:
        raise EditError("stale", 409, "changed since you loaded it")

    with pytest.raises(EditError):
        _operator_state_fire(scope, {"text": "never lands"}, {"source": "web", "check": stale}, True)
    assert not (root / ".levain" / "context.json").exists() and _episodes(root) == []
    res = _operator_state_fire(scope, {"text": "via the cockpit"}, {"source": "web", "check": lambda: None}, True)
    assert res["episode"] == "written" and [e.content for e in _episodes(root)] == ["via the cockpit"]


def test_no_episodic_store_sets_the_line_and_creates_nothing(tmp_path: Path) -> None:
    res = _set(tmp_path, "no store here")
    assert res["episode"] == "no_store" and _line(tmp_path)["state"] == "no store here"
    assert not (tmp_path / ".levain" / "memory.db").exists()


def test_a_scope_without_an_episodic_db_file_is_no_store(tmp_path: Path) -> None:
    paths = AnnealPaths.from_db(tmp_path / "elsewhere" / "memory.db")
    scope = WriteScope(anneal=paths, ledger_root=tmp_path, context_json=tmp_path / "ctx.json")
    assert apply_edit(scope, {"kind": "operator_state", "text": "x"})["episode"] == "no_store"


def test_an_unusable_store_still_sets_the_line_and_says_so(root: Path) -> None:
    (root / ".levain" / "memory.db").write_bytes(b"this is not a sqlite database" * 50)
    with pytest.warns(RuntimeWarning, match="felt episode was not written"):
        res = _set(root, "line survives")
    assert res["ok"] and res["episode"] == "failed" and res["episode_error"]
    assert _line(root)["state"] == "line survives"


def test_a_locked_store_still_sets_the_line_and_says_so(root: Path) -> None:
    holder = sqlite3.connect(str(root / ".levain" / "memory.db"), timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.warns(RuntimeWarning):
            res = _set(root, "line survives a lock")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert res["episode"] == "failed" and _line(root)["state"] == "line survives a lock"
    assert _set(root, "after the lock")["episode"] == "written"


def test_the_cli_reports_a_failed_episode_and_still_exits_zero(root: Path, capsys: pytest.CaptureFixture) -> None:
    (root / ".levain" / "memory.db").write_bytes(b"not a database" * 50)
    assert run_state(root, "cli line", source="cli") == 0
    cap = capsys.readouterr()
    assert "state set: cli line" in cap.out and "felt episode was not written" in cap.err
    assert _line(root)["state"] == "cli line"


def test_a_bare_write_state_records_no_episode(root: Path) -> None:
    done = write_state(root / ".levain" / "context.json", "bare")
    assert done.episode == "no_store" and _episodes(root) == []


def test_a_retry_after_a_locked_store_delivers_the_episode_the_first_try_lost(root: Path) -> None:
    """L1 WARNING F1: 'unchanged' was keyed on the LINE, so re-setting the text whose episode failed
    returned unchanged and the felt episode was never written."""
    holder = sqlite3.connect(str(root / ".levain" / "memory.db"), timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.warns(RuntimeWarning):
            assert _set(root, "set while locked")["episode"] == "failed"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _episodes(root) == []
    assert _set(root, "set while locked")["episode"] == "written"        # the retry the CLI's note invites
    assert [e.content for e in _episodes(root)] == ["set while locked"]
    assert _set(root, "set while locked")["episode"] == "unchanged"      # now a true duplicate
    assert len(_episodes(root)) == 1


def test_a_line_set_before_the_store_existed_gets_its_episode_once_it_does(tmp_path: Path) -> None:
    assert _set(tmp_path, "before init")["episode"] == "no_store"
    (tmp_path / ".levain").mkdir(exist_ok=True)
    with Store(str(tmp_path / ".levain" / "memory.db")):
        pass
    assert _set(tmp_path, "before init")["episode"] == "written"
    assert [e.content for e in _episodes(tmp_path)] == ["before init"]


def test_a_death_between_the_line_and_the_episode_is_repaired_by_the_next_set(root: Path) -> None:
    """The line landed, the process died before the episode: the next set of the same text writes it."""
    dashboard._write_context_line(root / ".levain" / "context.json", "state", "died here", source="web")
    assert _episodes(root) == []
    assert _set(root, "died here")["episode"] == "written"


def test_an_old_episode_of_the_same_text_does_not_stand_for_a_later_setting(root: Path) -> None:
    """The store's episode must be for THIS setting of the line (at or after its set time)."""
    _set(root, "again")
    _set(root, "other")
    assert _set(root, "again")["episode"] == "written"
    assert sorted(e.content for e in _episodes(root)) == ["again", "again", "other"]


def test_a_stale_writers_episode_never_outranks_the_newer_lines(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 WARNING F2: the episode was stamped by ``record()`` after the lock was released, so a slow
    writer's older line left the NEWEST episode contradicting the live line. Driven through
    ``write_state`` itself (``apply_edit`` serialises in-process; two processes do not)."""
    real = dashboard._record_state_episode

    def slow(*args, **kwargs):
        if "x" in args:
            time.sleep(0.8)         # the first writer's episode lands after the second's
        return real(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_record_state_episode", slow)
    ctx, db = root / ".levain" / "context.json", root / ".levain" / "memory.db"
    first = threading.Thread(target=write_state, args=(ctx, "x"), kwargs={"episodic_db": db})
    first.start()
    time.sleep(0.3)
    write_state(ctx, "y", episodic_db=db)
    first.join()
    assert sorted(e.content for e in _episodes(root)) == ["x", "y"]
    newest = max(_episodes(root), key=lambda e: e.timestamp)
    assert newest.content == _line(root)["state"] == "y"


def test_the_episode_is_stamped_with_the_instant_the_line_is(root: Path) -> None:
    _set(root, "stamped")
    (ep,) = _episodes(root)
    at = dashboard.datetime.fromisoformat(_line(root)["state_set_at"])
    assert ep.timestamp == at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_the_episode_joins_anneals_audit_chain_alongside_other_writers(root: Path) -> None:
    """L2 F3: ``audit=False`` rested on a false premise (the audit chain takes concurrent writers
    across processes). Threads and a second process set distinct lines at once; every episode has
    its audit record and the chain's seq numbers run unbroken."""
    code = ("import sys; from pathlib import Path; from levain.writes import WriteScope, apply_edit;"
            "apply_edit(WriteScope.from_install_root(Path(sys.argv[1])), {'kind':'operator_state','text':'from a process'})")
    proc = subprocess.Popen([sys.executable, "-c", code, str(root)], cwd=str(REPO),
                            env={**__import__("os").environ, "PYTHONPATH": str(REPO)})
    threads = [threading.Thread(target=_set, args=(root, f"line {i}")) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert proc.wait(timeout=60) == 0
    assert len(_episodes(root)) == 4
    records = [json.loads(line) for line in (root / ".levain" / "memory.audit.jsonl").read_text().splitlines()]
    assert [r["seq"] for r in records] == list(range(4)) and {r["event"] for r in records} == {"record"}


def _calls_to_write_state(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == "write_state")
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == "write_state"))]


def _names_a_store(call: ast.Call) -> bool:
    """A call passes ``episodic_db=`` and it is not the literal None."""
    return any(kw.arg == "episodic_db" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
               for kw in call.keywords)


def test_every_write_state_call_in_the_package_names_its_store() -> None:
    """The class closes by a bound: a new entry point that calls ``write_state`` without
    ``episodic_db`` (or with the literal None) would set a line no dream can read, so each call in
    levain/ must name one, whether it is spelled ``write_state(``, ``dashboard.write_state(`` or an
    aliased import."""
    sites = []
    for path in sorted((REPO / "levain").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {a.asname for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   for a in n.names if a.name == "write_state" and a.asname}
        for call in _calls_to_write_state(tree) + [
                n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id in aliases]:
            sites.append(path.relative_to(REPO))
            assert _names_a_store(call), f"{path.relative_to(REPO)}:{call.lineno} calls write_state without a store"
    assert {p.name for p in sites} >= {"dashboard.py", "writes.py"}


@pytest.mark.parametrize("source", [
    "write_state(p, 'x')",
    "write_state(p, 'x', episodic_db=None)",
    "dashboard.write_state(p, 'x')",
    "dashboard.write_state(p, 'x', episodic_db=None)",
    "ws(p, 'x', source='cli')",
])
def test_the_source_guard_catches_the_shapes_the_old_regex_missed(source: str) -> None:
    tree = ast.parse("from levain.dashboard import write_state as ws\n" + source)
    calls = _calls_to_write_state(tree) or [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                                           and isinstance(n.func, ast.Name) and n.func.id == "ws"]
    assert calls and not any(_names_a_store(c) for c in calls)
    assert _names_a_store(ast.parse("dashboard.write_state(p, 'x', episodic_db=scope.db)").body[0].value)
