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
import warnings
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


def test_the_same_call_twice_writes_one_episode_and_refreshes_the_stamp_not_the_run(root: Path) -> None:
    _set(root, "Dinner and some light flow work")
    first = _line(root)
    assert _set(root, "Dinner and some light flow work")["episode"] == "written" and len(_episodes(root)) == 1
    assert _set(root, "Dinner and some light flow work")["episode"] == "written" and len(_episodes(root)) == 1
    now = _line(root)
    assert now["state_set_at"] > first["state_set_at"] and now["state_run"] == first["state_run"] == first["state_set_at"]
    assert now["state"] == "Dinner and some light flow work"


def test_a_changed_cleared_or_expired_line_begins_a_new_run_and_a_clear_pops_all_four_keys(root: Path) -> None:
    _set(root, "a")
    run_a = _line(root)["state_run"]
    _set(root, "b")
    assert _line(root)["state_run"] != run_a
    _set(root, "")
    assert not {"state", "state_set_at", "state_source", "state_run"} & _line(root).keys()
    _set(root, "a")
    assert _line(root)["state_run"] != run_a


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
    assert [r["episode"] for r in results] == ["written"] * 6
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
    res = _set(root, "line survives")
    assert res["ok"] and res["episode"] == "failed" and res["episode_error"]
    assert _line(root)["state"] == "line survives"


def test_a_locked_store_still_sets_the_line_and_says_so(root: Path) -> None:
    holder = sqlite3.connect(str(root / ".levain" / "memory.db"), timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
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
    """L1 WARNING F1: the skip was keyed on the LINE, so re-setting the text whose episode failed
    skipped and the felt episode was never written."""
    holder = sqlite3.connect(str(root / ".levain" / "memory.db"), timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        assert _set(root, "set while locked")["episode"] == "failed"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _episodes(root) == []
    assert _set(root, "set while locked")["episode"] == "written"        # the retry the CLI's note invites
    assert [e.content for e in _episodes(root)] == ["set while locked"]
    assert _set(root, "set while locked")["episode"] == "written"        # now a true duplicate: the run's key is held
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
    ``write_state`` itself (``apply_edit`` serialises in-process; two processes do not); the first
    writer is held on an event, not a sleep."""
    real = dashboard._record_state_episode
    reached, release = threading.Event(), threading.Event()

    def gated(*args, **kwargs):
        if args[1] == "x":
            reached.set()
            assert release.wait(30)         # the first writer's episode lands after the second's
        return real(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_record_state_episode", gated)
    ctx, db = root / ".levain" / "context.json", root / ".levain" / "memory.db"
    first = threading.Thread(target=write_state, args=(ctx, "x"), kwargs={"episodic_db": db})
    first.start()
    assert reached.wait(30)
    write_state(ctx, "y", episodic_db=db)
    release.set()
    first.join()
    assert sorted(e.content for e in _episodes(root)) == ["x", "y"]
    newest = max(_episodes(root), key=lambda e: e.timestamp)
    assert newest.content == _line(root)["state"] == "y"


def test_the_episode_is_stamped_with_the_instant_its_run_began(root: Path) -> None:
    _set(root, "stamped")
    _set(root, "stamped")       # a re-set moves the line's stamp, never the run's
    (ep,) = _episodes(root)
    at = dashboard.datetime.fromisoformat(_line(root)["state_run"])
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


# ---- L3 r1: an episode's identity is the run it belongs to, never inferred from text and time ----

def _locked(root: Path):
    holder = sqlite3.connect(str(root / ".levain" / "memory.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def _release(holder) -> None:
    holder.execute("ROLLBACK")
    holder.close()


def test_a_retry_is_not_satisfied_by_an_episode_from_before_a_clear(root: Path) -> None:
    """codex HIGH 1: set x, clear, set x with the episode failing, retry: the pre-clear episode
    stood for the new setting and the retry reported it held."""
    assert _set(root, "x")["episode"] == "written"
    _set(root, "")
    holder = _locked(root)
    try:
        assert _set(root, "x")["episode"] == "failed"
    finally:
        _release(holder)
    assert _set(root, "x")["episode"] == "written"
    assert [e.content for e in _episodes(root)] == ["x", "x"]


def test_x_then_y_then_x_is_three_episodes_though_the_first_writers_lands_last(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex HIGH 2: the first writer's late episode read the THIRD write's x as its own."""
    real = dashboard._record_state_episode
    reached, release, calls = threading.Event(), threading.Event(), []

    def gated(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:             # the first writer, x
            reached.set()
            assert release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_record_state_episode", gated)
    ctx, db = root / ".levain" / "context.json", root / ".levain" / "memory.db"
    first = threading.Thread(target=write_state, args=(ctx, "x"), kwargs={"episodic_db": db})
    first.start()
    assert reached.wait(30)
    write_state(ctx, "y", episodic_db=db)
    write_state(ctx, "x", episodic_db=db)
    release.set()
    first.join()
    assert sorted(e.content for e in _episodes(root)) == ["x", "x", "y"]


def test_a_failed_episode_never_raises_under_warnings_as_errors(root: Path) -> None:
    """codex HIGH 3: a warnings.warn on the failed path raised after the line was committed."""
    holder = _locked(root)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            res = _set(root, "set under -W error")
    finally:
        _release(holder)
    assert res["ok"] and res["episode"] == "failed" and res["episode_error"]
    assert _line(root)["state"] == "set under -W error"


def test_more_than_fifty_newer_state_episodes_do_not_duplicate_a_retried_run(root: Path) -> None:
    """codex MED 5: the recall scan read the newest 50 levain-state episodes only."""
    _set(root, "mine")
    with Store(str(root / ".levain" / "memory.db")) as store:
        for i in range(60):
            store.record(content=f"someone else {i}", episode_type="observation",
                         source=dashboard.STATE_EPISODE_SOURCE, timestamp=f"2099-01-01T00:00:{i:02d}.000000Z")
    _set(root, "mine")
    assert [e.content for e in _episodes(root)].count("mine") == 1


def test_a_context_file_from_an_older_levain_keeps_working(root: Path) -> None:
    """Three keys, no state_run: the run began at the line's own stamp."""
    ctx = root / ".levain" / "context.json"
    stamp = (dashboard.datetime.now(dashboard.timezone.utc) - dashboard.timedelta(hours=2)).isoformat()
    ctx.write_text(json.dumps({"state": "old build", "state_set_at": stamp, "state_source": "cli"}), encoding="utf-8")
    assert _set(root, "old build")["episode"] == "written"
    assert _set(root, "old build")["episode"] == "written"       # the same run: the store already holds it, one episode
    (ep,) = _episodes(root)
    assert ep.content == "old build" and _line(root)["state_run"] == stamp
    assert ep.timestamp == dashboard.datetime.fromisoformat(stamp).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_a_tombstoned_runs_episode_is_not_rewritten_for_the_same_unchanged_text(root: Path) -> None:
    """The known edge, stated in ``_record_state_episode``: anneal never reuses a key."""
    _set(root, "tombstone me")
    key = dashboard._state_origin_key(_line(root)["state_run"], "tombstone me")
    with Store(str(root / ".levain" / "memory.db")) as store:
        _, version = store.read_episode_versioned(key)
        store.delete_by_origin_key(key, expected_version=version, effect_id="test-tombstone")
    res = _set(root, "tombstone me")
    assert res["ok"] and res["episode"] == "failed" and "never reused" in res["episode_error"]
    assert _episodes(root) == []
    _set(root, "tombstone me, changed")
    assert [e.content for e in _episodes(root)] == ["tombstone me, changed"]


def test_a_store_that_cannot_be_derived_sets_the_line_without_an_episode(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """complement #7: a failure deriving the store must not crash the CLI."""
    def boom(*a, **k):
        raise RuntimeError("no store for you")

    monkeypatch.setattr(dashboard.SubstrateSource, "local", boom)
    assert run_state(root, "line stands", source="cli") == 0
    assert _line(root)["state"] == "line stands" and _episodes(root) == []
    capsys.readouterr()


# ── the episode sink: an embedder whose episode store is not an anneal store levain can open ──
# (flow keeps its episodes in a different schema; flow's 1020+23 flow-seat asked for this seam,
# and the shape is its proposal: WriteScope.state_episode(text, source, run))

def _sink_scope(root: Path, sink) -> WriteScope:
    base = WriteScope.from_install_root(root)
    return WriteScope(anneal=base.anneal, ledger_root=base.ledger_root, install_root=base.install_root,
                      context_json=base.context_json, state_episode=sink)


def test_a_sink_gets_text_source_and_run_once_per_set_and_the_store_gets_nothing(root: Path) -> None:
    calls: list = []
    scope = _sink_scope(root, lambda *a: calls.append(a))
    assert apply_edit(scope, {"kind": "operator_state", "text": "Picking Som up", "source": "tui"})["episode"] == "written"
    (text, source, run), = calls
    assert (text, source) == ("Picking Som up", "tui") and run == _line(root)["state_run"]
    assert _episodes(root) == []        # the sink is used INSTEAD of the scope's episodic store


def test_a_reset_of_the_live_text_passes_the_same_run_a_changed_text_a_new_one(root: Path) -> None:
    calls: list = []
    scope = _sink_scope(root, lambda *a: calls.append(a))
    for text in ("a", "a", "b"):
        apply_edit(scope, {"kind": "operator_state", "text": text})
    assert [c[0] for c in calls] == ["a", "a", "b"]
    assert calls[0][2] == calls[1][2] != calls[2][2]


def test_a_clear_does_not_call_the_sink(root: Path) -> None:
    calls: list = []
    scope = _sink_scope(root, lambda *a: calls.append(a))
    apply_edit(scope, {"kind": "operator_state", "text": "x"})
    res = apply_edit(scope, {"kind": "operator_state", "text": ""})
    assert res["cleared"] is True and res["episode"] == "cleared" and len(calls) == 1


def test_a_sink_that_raises_leaves_the_line_set_and_says_failed(root: Path) -> None:
    def boom(*_a):
        raise RuntimeError("flow store is busy")
    res = apply_edit(_sink_scope(root, boom), {"kind": "operator_state", "text": "line survives"})
    assert res["ok"] and res["episode"] == "failed" and "flow store is busy" in res["episode_error"]
    assert _line(root)["state"] == "line survives"


def test_a_scope_without_a_sink_records_into_its_store_as_before(root: Path) -> None:
    assert _set(root, "no sink")["episode"] == "written" and [e.content for e in _episodes(root)] == ["no sink"]
