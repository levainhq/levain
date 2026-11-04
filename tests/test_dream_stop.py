"""levain.dream.executor D1-1s: the dream's ``Executor`` (the stated exposure, ``stop()``), D1_EXECUTOR §2.4, §2.6.

Every stop case runs against REAL stand-in processes this file spawns (a python "fire" that starts a python "child"
leading its own group, the way ``ClaudeMind.compose`` does, with the occurrence dir in the argv): never the real dream,
never ``claude``. Every process is killed in teardown, pass or fail.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

import pytest

from levain.agents.accounting import Executor, StopResult, Usage
from levain.agents.controller import fire_record_path
from levain.consent.delivery import process_identity
from levain.dream import executor as ex

REPO = Path(__file__).resolve().parent.parent
OCC_ID = "oc-" + "ab" * 16
MODEL = "claude-test-1"
ROW = ex.PriceRow(context_window=200_000, max_output=32_000, input_micros_per_mtok=18_000_000,
                  output_micros_per_mtok=75_000_000, source="https://example.invalid/price", read_on="2026-11-03")
TABLE = {MODEL: ROW}

FIRE = textwrap.dedent('''
    import json, os, subprocess, sys, time
    from pathlib import Path
    from levain.consent.delivery import process_identity
    from levain.dream import executor as ex
    from levain.firing.confinement import _Leader

    store_dir, occ_dir, occ_id, phase, mode = sys.argv[1:6]
    occ = Path(occ_dir)
    me = process_identity(os.getpid())
    rec = Path(store_dir) / "fires" / (occ_id + ".json")
    rec.parent.mkdir(exist_ok=True)
    rec.write_text(json.dumps({"occurrence_id": occ_id, "pid": os.getpid(), "started": me[0], "args": me[1]}))
    prog = ex.Progress(occ)
    prog.update(phase=phase)
    # /bin/sh, not python: a venv python re-execs as Python.app, so its ps argv is not what was started
    argv = ["/bin/sh", "-c", "sleep 300; :", "--add-dir", str(occ)]
    proc = subprocess.Popen(argv, start_new_session=True)
    leader = _Leader(proc)
    ident = process_identity(proc.pid)
    prog.update(child_pid=proc.pid, child_started_at=ident[0], child_argv=argv)
    (occ / "ready").write_text("1")
    if mode == "ignore-term":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(300)
    if mode == "save-write-held":
        # the REAL Progress.update(phase="saving"), its os.replace held open until stop() has re-read progress.json
        # before its SIGKILL: the SIGTERM lands while the saving write is in flight (r2, three lineages)
        real_replace = os.replace

        def held_replace(src, dst):
            if Path(dst).name == "progress.json" and '"saving"' in Path(src).read_text():
                (occ / "in-write").write_text("1")
                end = time.monotonic() + 20
                while not (occ / "reread-done").exists() and time.monotonic() < end:
                    time.sleep(0.01)
            return real_replace(src, dst)

        os.replace = held_replace
        try:
            with ex.fire_stop_handler(prog):
                prog.update(phase="saving")
                (occ / "saving").write_text("1")
                time.sleep(300)                              # the save, under its own reserve
        except ex.FireStopRequested:
            (occ / "stopped-before-save").write_text("1")
        sys.exit(0)
    if mode == "entering-save":
        # a handler that reads "saving" while progress.json still says "composing" (the r1 ordering, which
        # Progress.update no longer has): kept to pin stop()'s own re-read before each escalation
        with ex.fire_stop_handler(prog) as requested:
            prog.data["phase"] = "saving"
            while not requested:
                time.sleep(0.01)
            prog.update(phase="saving")
            (occ / "saving").write_text("1")
            time.sleep(300)                                  # the save, under its own reserve
    try:
        with ex.fire_stop_handler(prog):
            try:
                leader.wait(300)
            finally:
                if ex.end_child(leader):
                    prog.update(child_pid=None, child_started_at=None, child_argv=None)
    except ex.FireStopRequested:
        pass
    (occ / "fire-exit").write_text("clean")
''')

#: A fire that runs the REAL ClaudeMind.compose and is SIGKILLed the instant Popen returns: the spawn-to-journal window.
COMPOSE_FIRE = textwrap.dedent('''
    import json, os, signal, subprocess, sys
    from pathlib import Path
    from levain.consent.delivery import process_identity
    from levain.dream import executor as ex

    store_dir, occ_dir, occ_id, fake_claude = sys.argv[1:5]
    occ = Path(occ_dir)
    me = process_identity(os.getpid())
    rec = Path(store_dir) / "fires" / (occ_id + ".json")
    rec.write_text(json.dumps({"occurrence_id": occ_id, "pid": os.getpid(), "started": me[0], "args": me[1]}))
    prog = ex.Progress(occ)
    prog.update(phase="composing")
    real = subprocess.Popen

    def popen_then_die(*a, **k):
        p = real(*a, **k)
        (occ / "child.pid").write_text(str(p.pid))
        os.kill(os.getpid(), signal.SIGKILL)

    subprocess.Popen = popen_then_die
    ex.ClaudeMind("m", claude_bin=fake_claude).compose(ex.ComposeRequest(
        package_text="p", instructions="i", occ_dir=occ, cap_usd_micros=1_000_000, timeout_s=60, progress=prog))
''')

_spawned: list[subprocess.Popen] = []
_groups: list[int] = []        # compose-child groups the stand-in fires started (their own sessions)


@pytest.fixture(autouse=True)
def _reap():
    yield
    for g in _groups:
        with contextlib.suppress(Exception):
            os.killpg(g, signal.SIGKILL)
    _groups.clear()
    for p in _spawned:
        with contextlib.suppress(Exception):
            os.killpg(p.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            p.kill()
        with contextlib.suppress(Exception):
            p.wait(timeout=5)
    _spawned.clear()


@pytest.fixture
def world(tmp_path):
    store = tmp_path / "agents"
    (store / "fires").mkdir(parents=True)
    occ = tmp_path / "occ"
    occ.mkdir()
    script = tmp_path / "fire.py"
    script.write_text(FIRE)
    facts = tmp_path / "first_run.json"
    ex.write_first_run_facts(facts, model=MODEL, models_charged=[MODEL], m_usd_micros=ex.one_call_ceiling(MODEL, TABLE))
    return store, occ, script, facts


def start_fire(world, phase="composing", mode="normal"):
    store, occ, script, _ = world
    env = dict(os.environ, PYTHONPATH=str(REPO))
    p = subprocess.Popen([sys.executable, str(script), str(store), str(occ), OCC_ID, phase, mode], env=env, cwd=REPO,
                         start_new_session=True)
    _spawned.append(p)
    end = time.monotonic() + 20
    while not (occ / "ready").exists():
        assert time.monotonic() < end and p.poll() is None, "the stand-in fire did not come up"
        time.sleep(0.05)
    prog = json.loads((occ / "progress.json").read_text())
    _groups.append(prog["child_pid"])
    return p, prog


def executor(world, **kw):
    store, occ, _, facts = world
    return ex.DreamExecutor(agent_store_dir=store, occurrence_id=OCC_ID, occ_dir=occ, model=MODEL, facts_path=facts,
                            table=TABLE, **kw)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    ident = process_identity(pid)
    return not (ident is not None and "<defunct>" in ident[1])


def group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return None
    return False


def wait_until(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return bool(fn())


# ------------------------------------------------------------------------------------------------- the exposure (§2.4)

def test_is_an_executor_and_states_r(world):
    e = executor(world)
    assert isinstance(e, Executor)
    assert e.max_exposure_usd_micros == ex.PER_RUN_LIMIT_USD_MICROS == 20_000_000


def test_no_price_row_has_no_stated_exposure(world):
    store, occ, _, facts = world
    with pytest.raises(ex.ExposureRefused, match="no price row"):
        ex.DreamExecutor(agent_store_dir=store, occurrence_id=OCC_ID, occ_dir=occ, model="claude-unknown",
                         facts_path=facts, table=TABLE)


def test_the_shipped_table_prices_the_default_so_the_default_launch_stops_at_the_first_run_facts(world):
    store, occ, _, facts = world
    assert ex.one_call_ceiling(ex.DEFAULT_CLAUDE_MODEL) is not None
    with pytest.raises(ex.ExposureRefused, match="first-run facts"):
        ex.DreamExecutor(agent_store_dir=store, occurrence_id=OCC_ID, occ_dir=occ, facts_path=facts)


def test_missing_first_run_facts_refuse(world):
    store, occ, _, facts = world
    facts.unlink()
    with pytest.raises(ex.ExposureRefused, match="not recorded"):
        executor(world)


@pytest.mark.parametrize("edit,match", [
    ({"models_charged": [MODEL, "claude-other"]}, "fact 1"),
    ({"m_usd_micros": 1}, "fact 2"),
    ({"r_usd_micros": 1}, "fact 2"),
    ({"model": "claude-other"}, "not 'claude-test-1'"),
])
def test_unmet_first_run_facts_refuse(world, edit, match):
    facts = world[3]
    doc = json.loads(facts.read_text())
    doc.update(edit)
    facts.write_text(json.dumps(doc))
    with pytest.raises(ex.ExposureRefused, match=match):
        executor(world)


def test_torn_facts_refuse(world):
    world[3].write_text("{not json")
    with pytest.raises(ex.ExposureRefused, match="cannot be read"):
        executor(world)


def test_r_minus_m_below_the_cheapest_compose_refuses(world):
    with pytest.raises(ex.ExposureRefused, match="cheapest compose"):
        executor(world, limit_usd_micros=ex.one_call_ceiling(MODEL, TABLE) + 1_000_000)


def test_ollama_route_states_no_dollars(world):
    store, occ, _, _ = world
    e = ex.DreamExecutor(agent_store_dir=store, occurrence_id=OCC_ID, occ_dir=occ, route="ollama")
    assert e.max_exposure_usd_micros == 0


def test_usage_reads_usage_json_and_never_raises(world):
    e = executor(world)
    assert e.usage().usd_micros is None                     # no usage.json: unreadable, whole reservation debited
    (world[1] / "usage.json").write_text(json.dumps({"unit": "usd_list", "readable": True, "actual_usd_micros": 5,
                                                     "input_tokens": 1, "output_tokens": 2, "source": "dream:claude-p"}))
    u = e.usage()
    assert isinstance(u, Usage) and u.usd_micros == 5
    with mock.patch("levain.dream.usage.read_usage", side_effect=RuntimeError("boom")):
        assert e.usage().usd_micros is None


# ------------------------------------------------------------------------------------------- stop(): composing, with a child

def test_composing_fire_with_live_child_is_ended_by_the_fire_not_by_stop(world):
    fire, prog = start_fire(world)
    child = prog["child_pid"]
    assert alive(fire.pid) and alive(child)
    res = executor(world).stop(20)
    assert res.terminated is True, res.detail
    assert fire.wait(timeout=5) == 0                         # the fire exited normally through its handler
    assert (world[1] / "fire-exit").read_text() == "clean"   # its own finally ran: it ended the child itself
    assert group_gone(child) is True
    assert json.loads((world[1] / "progress.json").read_text())["child_pid"] is None
    assert "SIGTERM to fire" in res.detail and "orphaned compose child" not in res.detail
    assert "fire ended: True" in res.detail and "compose child group ended: True" in res.detail


def test_fire_killed_first_leaves_an_orphan_whose_group_stop_signals(world):
    fire, prog = start_fire(world)
    child = prog["child_pid"]
    os.kill(fire.pid, signal.SIGKILL)                        # kill -9: no handler, no finally
    fire.wait(timeout=5)
    assert alive(child) and group_gone(child) is False
    res = executor(world).stop(20)
    assert res.terminated is True, res.detail
    assert group_gone(child) is True
    assert "orphaned compose child: ended" in res.detail


def test_orphan_with_a_stranger_at_the_recorded_place_gets_no_signal(world):
    fire, prog = start_fire(world)
    child = prog["child_pid"]
    os.kill(fire.pid, signal.SIGKILL)
    fire.wait(timeout=5)
    p = world[1] / "progress.json"
    doc = json.loads(p.read_text())
    doc["child_started_at"] = "Mon Jan  1 00:00:00 2001"     # what a reused pid looks like: another start time
    p.write_text(json.dumps(doc))
    with mock.patch("os.killpg", wraps=os.killpg) as kp:
        res = executor(world).stop(1)
    assert res.terminated is False                           # the group lives, nothing was assumed
    assert alive(child) and group_gone(child) is False       # and nothing was signalled
    assert [c for c in kp.call_args_list if c.args[1] != 0] == []
    assert "compose child group ended: False" in res.detail


def test_a_fire_in_saving_gets_no_signal(world):
    fire, prog = start_fire(world, phase="saving")
    child = prog["child_pid"]
    res = executor(world).stop(1)
    assert res.terminated is False
    assert alive(fire.pid) and alive(child)                  # neither was signalled
    assert "no signal" in res.detail and "'saving'" in res.detail


def test_a_saved_phase_gets_no_signal_either(world):
    fire, prog = start_fire(world, phase="saved")
    res = executor(world).stop(1)
    assert res.terminated is False and alive(fire.pid) and "'saved'" in res.detail


def test_unreadable_ps_sends_no_signal_and_answers_false(world):
    fire, prog = start_fire(world)
    with mock.patch("levain.consent.delivery.process_identity", return_value=None):
        res = executor(world).stop(1)
    assert res.terminated is False
    assert alive(fire.pid) and alive(prog["child_pid"])
    assert "process table cannot be read" in res.detail


def test_a_fire_whose_identity_mismatches_is_read_as_ended(world):
    store, occ, _, _ = world
    # a live process that is NOT the recorded fire holds the recorded pid
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    _spawned.append(other)
    ident = process_identity(other.pid)
    fire_record_path(store, OCC_ID).write_text(json.dumps(
        {"occurrence_id": OCC_ID, "pid": other.pid, "started": "Mon Jan  1 00:00:00 2001", "args": ident[1]}))
    res = executor(world).stop(1)
    assert res.terminated is True                            # no progress.json: no child; the fire read as ended
    assert alive(other.pid)                                  # and the stranger was never signalled
    assert "another process holds the number" in res.detail and "no child recorded" in res.detail


def test_a_matching_start_time_with_a_different_command_line_is_not_the_fire(world):
    store, occ, _, _ = world
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    _spawned.append(other)
    ident = process_identity(other.pid)
    fire_record_path(store, OCC_ID).write_text(json.dumps(
        {"occurrence_id": OCC_ID, "pid": other.pid, "started": ident[0], "args": ident[1]}))
    # same start, same args, but the argv does not carry the occurrence id: not this occurrence's fire
    res = executor(world).stop(1)
    assert alive(other.pid)
    assert res.terminated is True and "another process holds the number" in res.detail


def test_zombie_only_group_is_not_confirmed(world):
    store, occ, _, _ = world
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "--add-dir", str(occ)],
                              start_new_session=True)
    _spawned.append(leader)
    ident = process_identity(leader.pid)
    (occ / "progress.json").write_text(json.dumps({**dict.fromkeys(ex.PROGRESS_KEYS), "phase": "composing",
                                                   "child_pid": leader.pid, "child_started_at": ident[0],
                                                   "child_argv": [sys.executable, "-c", "import time; time.sleep(300)",
                                                                  "--add-dir", str(occ)]}))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    fire_record_path(store, OCC_ID).write_text(json.dumps(
        {"occurrence_id": OCC_ID, "pid": dead.pid, "started": "Mon Jan  1 00:00:00 2001", "args": f"x {OCC_ID}"}))
    leader.kill()                                            # a zombie: dead, deliberately not reaped
    assert wait_until(lambda: (process_identity(leader.pid) or ("", ""))[1].startswith("(")
                      or "defunct" in (process_identity(leader.pid) or ("", ""))[1])
    res = executor(world).stop(1)
    assert res.terminated is False, res.detail               # PermissionError (macOS) or a live-looking group: not gone
    assert "compose child group ended: False" in res.detail
    leader.wait()


def test_no_fire_record_is_not_terminated(world):
    res = executor(world).stop(0.2)
    assert res.terminated is False and "no fire identity record" in res.detail


def test_stop_never_raises_and_is_idempotent(world):
    e = executor(world)
    with mock.patch.object(ex.DreamExecutor, "_stop", side_effect=RuntimeError("boom")):
        r = e.stop(1)
    assert isinstance(r, StopResult) and r.terminated is False and "boom" in r.detail
    fire, prog = start_fire(world)
    a = e.stop(20)
    b = e.stop(1)
    assert a.terminated is True and b.terminated is True


def test_a_fire_that_ignores_sigterm_is_killed_at_half_the_deadline(world):
    fire, prog = start_fire(world, mode="ignore-term")
    child = prog["child_pid"]
    t = time.monotonic()
    res = executor(world).stop(4)
    assert time.monotonic() - t < 4 + 0.5                    # the deadline, plus one poll and one return
    assert not alive(fire.pid)
    assert "SIGKILL to fire" in res.detail
    # the child was orphaned by the SIGKILL; stop() then ended it through the identity-checked path in the same call
    assert res.terminated is True, res.detail
    assert group_gone(child) is True


def test_a_fire_that_enters_saving_after_the_sigterm_is_never_sigkilled(world):
    """r1 (three lineages): stop() read "composing", the SIGTERM landed as the fire entered saving (its handler deferred),
    and the SIGKILL at deadline/2 killed the save. progress.json is re-read before every escalation."""
    fire, prog = start_fire(world, mode="entering-save")
    res = executor(world).stop(2)
    assert (world[1] / "saving").exists()                    # the SIGTERM landed and the fire went on into its save
    assert alive(fire.pid), res.detail                       # and the save was not killed
    assert res.terminated is False
    assert "SIGTERM to fire" in res.detail and "SIGKILL to fire" not in res.detail
    assert "phase 'saving' on the re-read before SIGKILL: no SIGKILL" in res.detail


def test_a_fire_killed_between_spawn_and_journal_is_not_read_as_childless(world, tmp_path):
    """r1 codex HIGH 1: the real ClaudeMind.compose, its fire SIGKILLed the instant Popen returned. progress.json must
    not read as "no child" while the spawned child runs."""
    store, occ, _, _ = world
    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\nsleep 300\n")
    fake.chmod(0o755)
    script = tmp_path / "compose_fire.py"
    script.write_text(COMPOSE_FIRE)
    p = subprocess.Popen([sys.executable, str(script), str(store), str(occ), OCC_ID, str(fake)],
                         env=dict(os.environ, PYTHONPATH=str(REPO)), cwd=REPO, start_new_session=True)
    _spawned.append(p)
    assert p.wait(timeout=20) == -signal.SIGKILL
    child = int((occ / "child.pid").read_text())
    _groups.append(child)
    assert alive(child)
    res = executor(world).stop(1)
    assert res.terminated is False, res.detail
    assert alive(child)                                      # unidentified: nothing signalled it
    assert "compose child group ended: False" in res.detail and "starting" in res.detail


def test_stop_honours_its_deadline_when_ps_stalls(world):
    """r1 codex MED 3: every identity probe's ps is capped to the time left, so a stalled ps cannot carry stop() past
    deadline_s. The stall is a real process (sleep) standing in for ps."""
    fire, prog = start_fire(world)
    from levain.consent import delivery
    real_run = subprocess.run

    def stalled(args, **kw):
        return real_run(["/bin/sleep", "3"], **kw) if args and args[0] == "ps" else real_run(args, **kw)

    t = time.monotonic()
    with mock.patch.object(delivery.subprocess, "run", side_effect=stalled):
        res = executor(world).stop(0.5)
    assert time.monotonic() - t < 0.5 + 0.5, res.detail
    assert res.terminated is False
    assert alive(fire.pid) and alive(prog["child_pid"])      # unreadable identity: no signal


@pytest.mark.parametrize("edit", [{"phase": "savng"}, "drop-phase", "drop-child_pid"])
def test_a_progress_record_off_the_schema_is_unreadable_and_gets_no_signal(world, edit):
    """r1 codex MED 4: an unknown phase or a missing key is not "pre-save"."""
    fire, prog = start_fire(world)
    p = world[1] / "progress.json"
    doc = json.loads(p.read_text())
    if isinstance(edit, dict):
        doc.update(edit)
    else:
        del doc[edit.split("-", 1)[1]]
    p.write_text(json.dumps(doc))
    res = executor(world).stop(1)
    assert res.terminated is False
    assert alive(fire.pid) and alive(prog["child_pid"])
    assert "cannot be read" in res.detail and "SIGTERM" not in res.detail
    # r2 deepseek 2: the note names what is off the schema, so an operator can see which side is stale
    assert ("phase 'savng' is not one of" if isinstance(edit, dict) else
            f"missing key(s) ['{edit.split('-', 1)[1]}']") in res.detail, res.detail


def test_a_sigterm_landing_while_saving_is_written_never_lets_a_save_start_under_a_sigkill(world):
    """r2 (complement 1, codex 2, deepseek 1): the handler must not defer on a "saving" the disk does not yet show.
    The real Progress.update(phase="saving") is held mid-write by the stand-in until stop() has re-read progress.json
    (still "composing") before its SIGKILL; then the write lands. A SIGTERM that arrived before the disk said "saving"
    must cancel the save, so the SIGKILL can never land in one."""
    fire, _ = start_fire(world, mode="save-write-held")
    occ = world[1]
    assert wait_until(lambda: (occ / "in-write").exists())
    real = ex.DreamExecutor._escalation_refused
    calls: list = []

    def gated(self):
        out = real(self)
        calls.append(out)
        if len(calls) == 2:                                  # the re-read before SIGKILL: release the write, then
            (occ / "reread-done").write_text("1")            # give a save (if one starts) time to start
            wait_until(lambda: (occ / "saving").exists() or (occ / "stopped-before-save").exists(), 10)
        return out

    with mock.patch.object(ex.DreamExecutor, "_escalation_refused", gated):
        res = executor(world).stop(2)
    assert calls == [None, None], (calls, res.detail)        # both re-reads saw "composing": the interleave happened
    assert not (occ / "saving").exists(), res.detail         # no save began after the SIGTERM
    assert (occ / "stopped-before-save").exists(), res.detail
    assert json.loads((occ / "progress.json").read_text())["phase"] == "saving"   # the write did land first


def _fake_claude(tmp_path, body):
    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\n" + body + "\n")
    fake.chmod(0o755)
    return str(fake)


def _compose(claude_bin, occ, prog):
    return ex.ClaudeMind(MODEL, claude_bin=claude_bin).compose(ex.ComposeRequest(
        package_text="p", instructions="i", occ_dir=occ, cap_usd_micros=1_000_000, timeout_s=60, progress=prog))


def _child_keys(occ):
    doc = json.loads((occ / "progress.json").read_text())
    return {k: doc[k] for k in ("child_pid", "child_started_at", "child_argv")}


def test_a_stderr_open_that_fails_clears_the_child_argv_marker(tmp_path):
    """r2 codex MED: the marker is journalled before the spawn; a failure positively before any child exists must
    clear it, or every later stop()/recovery reads "a child may be starting"."""
    occ = tmp_path / "occ"
    occ.mkdir()
    (occ / "claude-1.stderr").mkdir()                        # open(..., "wb") raises IsADirectoryError
    prog = ex.Progress(occ)
    prog.update(phase="composing")
    with pytest.raises(OSError):
        _compose(_fake_claude(tmp_path, "sleep 300"), occ, prog)
    assert _child_keys(occ) == dict.fromkeys(("child_pid", "child_started_at", "child_argv")), _child_keys(occ)


def test_a_sigterm_before_popen_returns_is_held_until_the_child_is_journalled_then_ends_it(tmp_path):
    """r2 codex MED: a SIGTERM between the child_argv journal and Popen's return must not unwind there (no child, a
    marker left forever, or a child started and never journalled). It is held, raised once the pid is journalled, and
    compose's finally ends the child and clears every child key."""
    occ = tmp_path / "occ"
    occ.mkdir()
    fake = _fake_claude(tmp_path, "sleep 300")
    prog = ex.Progress(occ)
    prog.update(phase="composing")
    real = subprocess.Popen
    spawned: list[int] = []

    def term_then_popen(args, *a, **k):
        if args and args[0] == fake and not spawned:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)                                  # the handler runs here unless it is held
        p = real(args, *a, **k)
        if args and args[0] == fake:
            spawned.append(p.pid)
            _groups.append(p.pid)
        return p

    with ex.fire_stop_handler(prog) as requested, mock.patch.object(ex.subprocess, "Popen", term_then_popen), \
            pytest.raises(ex.FireStopRequested):
        _compose(fake, occ, prog)
    assert requested == [True]
    assert spawned, "the child was never started"
    assert group_gone(spawned[0]) is True
    assert _child_keys(occ) == dict.fromkeys(("child_pid", "child_started_at", "child_argv")), _child_keys(occ)


def test_a_compose_that_exits_normally_clears_every_child_key_and_stop_reads_no_child(world, tmp_path):
    """r2 deepseek MED 1: a successful compose must not leave child_argv set (stop() would answer terminated=False
    forever). A real stand-in claude that answers and exits 0."""
    _, occ, _, _ = world
    out = json.dumps({"result": "# memory", "total_cost_usd": 0.01, "subtype": "success", "num_turns": 1})
    prog = ex.Progress(occ)
    prog.update(phase="composing")
    res = _compose(_fake_claude(tmp_path, f"cat >/dev/null\nprintf '%s' '{out}'"), occ, prog)
    assert res.error is None and res.text.strip(), res
    assert _child_keys(occ) == dict.fromkeys(("child_pid", "child_started_at", "child_argv")), _child_keys(occ)
    assert "compose child group ended: True" in executor(world).stop(0.5).detail


def test_a_stop_raised_as_the_saving_write_closes_cancels_the_wrap_and_sets_the_journal_back(tmp_path):
    """r2: the REAL consolidate_store (Ollama mind, fake client, scratch anneal store), a SIGTERM landing while the
    "saving" write is in flight. It is held, raised as the write closes (before the save), the wrap is cancelled by
    token and progress.json must not be left reading "saving" for a save that never started."""
    from anneal_memory import FLOW_SCHEMA, Store
    d = tmp_path / "store"
    d.mkdir()
    with Store(str(d / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
        before = s.last_wrap_id()
    text ="\n\n".join(f"## {h}\n{b}" for h, b in [
        ("State", "First consolidation."), ("Active Threads", "- Bearings."), ("Patterns", "None yet."),
        ("Decisions", "None yet."), ("Context", "The first dream."), ("Understanding", "Early days.")]) + "\n"

    class _R:
        def __init__(self):
            self.message = type("M", (), {"content": text})()
            self.usage = None

    mind = ex.OllamaMind("glm-test:cloud", client=lambda system, user: _R())
    occ = tmp_path / "occ"
    real_replace = os.replace

    def term_mid_write(src, dst):
        if Path(dst).name == "progress.json" and '"saving"' in Path(src).read_text():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)                                  # unheld, the handler would run (and decide) here
        return real_replace(src, dst)

    real_init = ex.Progress.__init__
    requested: list = []

    def init(self, occ_dir):                                 # the handler goes on the Progress consolidate_store makes
        real_init(self, occ_dir)
        requested.append(stack.enter_context(ex.fire_stop_handler(self)))

    with contextlib.ExitStack() as stack, mock.patch.object(ex.Progress, "__init__", init), \
            mock.patch.object(ex.os, "replace", term_mid_write), pytest.raises(ex.FireStopRequested):
        ex.consolidate_store(d / "memory.db", mind=mind, occurrence_dir=occ)
    assert requested == [[True]]
    doc = json.loads((occ / "progress.json").read_text())
    assert (doc["phase"], doc["wrap_token"], doc["composed_sha256"]) == (None, None, None), doc
    with Store(str(d / "memory.db"), section_schema=None) as s:
        assert s.get_wrap_started_at() is None and s.last_wrap_id() == before   # cancelled, and nothing was saved


def test_stop_zero_still_delivers_the_first_sigterm(world):
    """r2 deepseek 3: stop(0) means "signal now, do not wait": the first identity probe is floored, so it runs."""
    fire, _ = start_fire(world)
    res = executor(world).stop(0)
    assert "SIGTERM to fire" in res.detail, res.detail
    assert wait_until(lambda: (world[1] / "fire-exit").exists())   # the fire ended its own child and exited


def test_no_signal_goes_out_once_the_deadline_has_passed_during_the_probe(world):
    """r2 complement 5, codex 3: a probe that returns after the deadline is not followed by a signal."""
    fire, prog = start_fire(world)
    from levain.consent import delivery
    real = delivery.process_identity

    def slow(pid, **kw):
        out = real(pid, **kw)
        time.sleep(0.6)
        return out

    with mock.patch.object(delivery, "process_identity", slow):
        res = executor(world).stop(0.5)
    assert "; SIGTERM to fire" not in res.detail and "SIGKILL to fire" not in res.detail, res.detail
    assert "deadline passed during the identity check: no SIGTERM" in res.detail
    assert res.terminated is False
    assert alive(fire.pid) and alive(prog["child_pid"])


def test_signal_sites_are_the_ones_the_spec_allows():
    """L1's list: every signal in the D1-1s block of executor.py, each placed under §2.6's rule."""
    src = (REPO / "levain/dream/executor.py").read_text()
    block = src[src.index("D1-1s: the Executor"):]
    sites = [l.strip() for l in block.splitlines() if "os.kill(" in l or "os.killpg(" in l]
    # two probes (kill(pid, 0), killpg(pid, 0)) and two signals to the FIRE after its identity check (SIGTERM, SIGKILL)
    assert len(sites) == 4, sites
    assert sum("signal.SIG" in l for l in sites) == 2 and all("os.kill(" in l for l in sites if "signal.SIG" in l)
    assert not any("killpg(" in l and "SIG" in l for l in block.splitlines()), "stop() signals no group by number"


# ------------------------------------------------------------------------------------------ the fire's SIGTERM handler

def test_the_handler_does_not_unwind_a_save():
    prog = ex.Progress(None)
    prog.update(phase="saving")
    with ex.fire_stop_handler(prog) as requested:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        assert requested == [True]                           # recorded, the save went on
    prog.update(phase="composing")
    with ex.fire_stop_handler(prog), pytest.raises(ex.FireStopRequested):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)


def test_the_handler_raises_once_so_a_second_stop_cannot_abort_the_unwind():
    """r1 complement MED 2: a second SIGTERM (stop() is called again next tick) while the fire unwinds through
    end_child must not raise inside it and abandon the child's kill and reap."""
    prog = ex.Progress(None)
    prog.update(phase="composing")
    with ex.fire_stop_handler(prog) as requested:
        with pytest.raises(ex.FireStopRequested):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)                 # the unwind is under way: recorded, not raised
        time.sleep(0.05)
        assert requested == [True, True]


# --------------------------------------------------- the production seam: consolidate_store(handle_stop=True)

STORE_FIRE = textwrap.dedent('''
    import fcntl, sys
    from pathlib import Path
    from levain.dream import executor as ex

    root = Path(sys.argv[1])
    pin = "test-model-1"
    row = ex.PriceRow(context_window=200_000, max_output=40_000, input_micros_per_mtok=20_000_000,
                      output_micros_per_mtok=25_000_000, source="t", read_on="2026-10-31")
    try:
        ex.consolidate_store(root / "store" / "memory.db", mind=ex.ClaudeMind(pin, claude_bin=str(root / "claude")),
                             max_seconds=3000, budget=ex.Budget.for_model(pin, {pin: row}),
                             occurrence_dir=root / "occ", handle_stop=sys.argv[2] == "stop")
    except ex.FireStopRequested:
        with open(root / "store" / "wrap.lock", "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)        # raises if the unwind left the lock held
        (root / "unwound").write_text("1")
        sys.exit(0)
''')


def _run_store_fire(tmp_path, handle_stop):
    from anneal_memory import FLOW_SCHEMA, Store
    root = tmp_path / "w"
    (root / "store").mkdir(parents=True)
    with Store(str(root / "store" / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped.", episode_type="observation", source="t")
    claude = root / "claude"
    claude.write_text(f'#!/bin/sh\necho $$ > "{root}/child.pid"\nsleep 300\n')
    claude.chmod(0o755)
    (root / "fire.py").write_text(STORE_FIRE)
    p = subprocess.Popen([sys.executable, str(root / "fire.py"), str(root), "stop" if handle_stop else "plain"],
                         env=dict(os.environ, PYTHONPATH=str(REPO)), cwd=REPO, start_new_session=True)
    _spawned.append(p)
    end = time.monotonic() + 30
    while not (root / "child.pid").exists():
        assert time.monotonic() < end and p.poll() is None, "the compose child did not start"
        time.sleep(0.05)
    time.sleep(0.3)
    cpid = int((root / "child.pid").read_text())
    _groups.append(cpid)
    os.kill(p.pid, signal.SIGTERM)
    p.wait(timeout=30)
    return root, p, cpid


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_a_real_consolidate_unwinds_on_sigterm_with_handle_stop(tmp_path):
    root, p, cpid = _run_store_fire(tmp_path, True)
    assert p.returncode == 0 and (root / "unwound").exists()      # FireStopRequested reached the caller, lock free
    deadline = time.monotonic() + 5
    while _alive(cpid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(cpid)                                       # the compose child was ended through its handle
    prog = json.loads((root / "occ" / "progress.json").read_text())
    assert prog["phase"] is None and prog["wrap_token"] is None and prog["child_pid"] is None
    assert (root / "occ" / "usage.json").exists()                 # the finally ran


def test_without_handle_stop_a_sigterm_is_a_plain_kill(tmp_path):
    root, p, cpid = _run_store_fire(tmp_path, False)
    assert p.returncode == -signal.SIGTERM and not (root / "unwound").exists()
    assert _alive(cpid)                                           # the failure this seam closes
    assert json.loads((root / "occ" / "progress.json").read_text())["phase"] == "composing"
