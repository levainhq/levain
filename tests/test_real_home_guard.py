"""The real-home guard: under a test run or a scratch run with ``$LEVAIN_HOME`` unset, every resolver that leads to a
write refuses instead of falling back to the operator's own ``~/.levain`` (the 11-12 incident: a script outside
tests/ ran a test function and filed 64 questions into the real, append-only question store).

The spawned interpreters below get a scratch HOME under ``tmp_path`` and never the real one; the assertion is that
nothing was created there."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain import home_guard
from levain.home_guard import RealHomeReached

REPO = Path(__file__).resolve().parent.parent


def _spawn(tmp_path: Path, script: str, *, test_run: bool, scratch_marker: bool = False, levain_home: str | None = None):
    """Run ``script`` in a plain ``python3`` with a scratch HOME and no ``$LEVAIN_HOME`` (unless given). The script is
    written under ``tmp_path``, outside tests/, so no conftest isolates it: the incident's shape."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    path = tmp_path / "bare_script.py"
    path.write_text(script, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home), "PYTHONPATH": str(REPO)}
    if test_run:
        env["PYTEST_CURRENT_TEST"] = "tests/test_x.py::test_y (call)"
    if scratch_marker:
        env[home_guard.SCRATCH_ENV] = "1"
    if levain_home is not None:
        env["LEVAIN_HOME"] = levain_home
    done = subprocess.run([sys.executable, str(path)], env=env, capture_output=True, text=True, timeout=60,
                          cwd=str(tmp_path))
    return done, fake_home


FILE_A_QUESTION = """
import sys
from levain.consent.questions import QuestionStore
store = QuestionStore()
store.file(agent="claude_code", agent_session_id="s-1", what="stray", options=[("ship", "Ship it"), ("hold", "Hold")],
           rec="hold", blocks="the sweep", cwd="/w")
print("FILED")
"""


def test_a_bare_script_under_a_test_run_files_no_question_into_the_fallback_home(tmp_path):
    """The incident shape: no conftest, no ``$LEVAIN_HOME``, pytest's per-test variable in the environment."""
    done, fake_home = _spawn(tmp_path, FILE_A_QUESTION, test_run=True)
    assert done.returncode != 0 and "FILED" not in done.stdout
    assert "RealHomeReached" in done.stderr and "LEVAIN_HOME to a scratch directory" in done.stderr, done.stderr
    assert not (fake_home / ".levain").exists()


def test_a_bare_script_marked_a_scratch_run_files_no_question_into_the_fallback_home(tmp_path):
    done, fake_home = _spawn(tmp_path, FILE_A_QUESTION, test_run=False, scratch_marker=True)
    assert done.returncode != 0 and "RealHomeReached" in done.stderr and "scratch run" in done.stderr, done.stderr
    assert not (fake_home / ".levain").exists()


def test_a_bare_script_with_its_own_levain_home_is_untouched(tmp_path):
    scratch = tmp_path / "scratch-levain"
    done, fake_home = _spawn(tmp_path, FILE_A_QUESTION, test_run=True, scratch_marker=True, levain_home=str(scratch))
    assert done.returncode == 0 and "FILED" in done.stdout, done.stderr
    assert (scratch / "consent" / "questions.db").is_file()
    assert not (fake_home / ".levain").exists()


def test_a_plain_run_with_neither_marker_keeps_the_fallback(tmp_path):
    """The product: no pytest, no scratch marker, no ``$LEVAIN_HOME`` resolves to ``~/.levain``, byte for byte."""
    script = "from levain.consent.queue import default_queue_dir\nprint(default_queue_dir())\n"
    done, fake_home = _spawn(tmp_path, script, test_run=False)
    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()) == (fake_home / ".levain" / "consent").resolve()


# --- one test per guarded resolver --------------------------------------------------------------------------------
# (module, expression that resolves the write-capable path) — each is the resolver a writer reaches first.
RESOLVERS = [
    ("levain.consent.queue", "default_queue_dir()"),
    ("levain.autonomic.db", "default_store_dir()"),
    ("levain.cockpit.custody", "default_key_dir()"),
    ("levain.chat_egress.srt_pin", "default_prefix()"),
    ("levain.observe", "home_dir()"),
    ("levain.desk.launch", "default_desk_dir()"),
    ("levain.desk.legacy", "desk_home(create=True)"),
    ("levain.desk.move_home", "levain_home()"),
    ("levain.firing.ssh_agent", "start()"),
]


@pytest.mark.parametrize("module,call", RESOLVERS, ids=[m.rsplit(".", 1)[-1] + ":" + c for m, c in RESOLVERS])
def test_each_write_capable_resolver_refuses_the_fallback_home_under_a_test_run(tmp_path, module, call):
    script = f"from {module} import {call.split('(')[0]}\n{call}\nprint('RESOLVED')\n"
    done, fake_home = _spawn(tmp_path, script, test_run=True)
    assert done.returncode != 0 and "RESOLVED" not in done.stdout, done.stdout
    assert "RealHomeReached" in done.stderr, done.stderr
    assert not (fake_home / ".levain").exists()


@pytest.mark.parametrize("module,call", RESOLVERS, ids=[m.rsplit(".", 1)[-1] + ":" + c for m, c in RESOLVERS])
def test_each_write_capable_resolver_refuses_the_fallback_home_in_a_scratch_run(tmp_path, module, call):
    script = f"from {module} import {call.split('(')[0]}\n{call}\nprint('RESOLVED')\n"
    done, fake_home = _spawn(tmp_path, script, test_run=False, scratch_marker=True)
    assert done.returncode != 0 and "RESOLVED" not in done.stdout, done.stdout
    assert "RealHomeReached" in done.stderr, done.stderr
    assert not (fake_home / ".levain").exists()


# --- the helper, in process ---------------------------------------------------------------------------------------

def test_the_helper_refuses_only_when_the_home_is_unset_and_the_run_is_marked():
    refuse = home_guard.refuse_real_home_fallback
    refuse("x", {"LEVAIN_HOME": "/somewhere", "PYTEST_CURRENT_TEST": "t"})          # set: untouched
    refuse("x", {"LEVAIN_HOME": "/somewhere", home_guard.SCRATCH_ENV: "1"})
    with pytest.raises(RealHomeReached, match="LEVAIN_HOME to a scratch directory"):
        refuse("the key directory", {"PYTEST_CURRENT_TEST": "t"})
    with pytest.raises(RealHomeReached, match="the key directory"):
        refuse("the key directory", {"LEVAIN_HOME": "", "PYTEST_CURRENT_TEST": "t"})    # empty is unset, as the resolvers read it
    with pytest.raises(RealHomeReached):        # pytest is loaded in this process, so this run is a test run either way
        refuse("x", {home_guard.SCRATCH_ENV: "1"})
    # the marker is exactly "1": a loose value does not mark a run
    assert not home_guard.scratch_run({home_guard.SCRATCH_ENV: "yes"})
    assert not home_guard.scratch_run({})


def test_the_suite_isolation_still_sets_levain_home_for_every_test():
    """The conftest's autouse isolation is what keeps this guard from firing on the suite: a test that reaches the
    default resolvers resolves under a per-test scratch home, never the fallback."""
    from levain.consent.queue import default_queue_dir

    assert os.environ.get("LEVAIN_HOME")
    assert default_queue_dir().parent == Path(os.environ["LEVAIN_HOME"]).resolve()


def test_a_test_that_unsets_levain_home_under_the_operators_home_meets_the_refusal(monkeypatch, tmp_path):
    """The conftest vouches only for a ``$HOME`` inside the temp root; any other home is the operator's, and the
    refusal comes before anything is made (the stand-in below does not exist)."""
    from levain.consent.queue import default_queue_dir

    not_scratch = "/Users/nobody-levain-guard-test"
    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", not_scratch)
    with pytest.raises(RealHomeReached):
        default_queue_dir()
    assert not Path(not_scratch).exists()


def test_a_test_that_unsets_levain_home_under_a_scratch_home_takes_the_scratch_fallback(monkeypatch, tmp_path):
    """The tests OF the fallback: the home is inside the temp root the conftest vouches for."""
    from levain.consent.queue import default_queue_dir

    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_queue_dir() == (tmp_path.resolve() / ".levain" / "consent")


def test_the_voucher_is_the_conftests_alone():
    refuse = home_guard.refuse_real_home_fallback
    root = str(Path(os.environ[home_guard.TEST_SCRATCH_ROOT_ENV]))
    inside = {"PYTEST_CURRENT_TEST": "t", "HOME": root + "/some-scratch", home_guard.TEST_SCRATCH_ROOT_ENV: root}
    refuse("x", inside)                                                           # vouched: a scratch fallback
    with pytest.raises(RealHomeReached):                                          # no voucher in the environment
        refuse("x", {k: v for k, v in inside.items() if k != home_guard.TEST_SCRATCH_ROOT_ENV})
    with pytest.raises(RealHomeReached):                                          # a home outside the vouched root
        refuse("x", {**inside, "HOME": "/Users/somebody"})
    with pytest.raises(RealHomeReached):                                          # `..` out of the root is resolved
        refuse("x", {**inside, "HOME": root + "/../../Users/somebody"})
    with pytest.raises(RealHomeReached):                                          # an empty voucher vouches for nothing
        refuse("x", {**inside, home_guard.TEST_SCRATCH_ROOT_ENV: ""})
