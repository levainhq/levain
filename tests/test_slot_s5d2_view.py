"""5d-ii S4a: the operator CLI's view and ``--repo`` checks (:mod:`levain.slot.adopt_view`), from failures the RUN
reproduced (``project_memory/1023-8/adopt/s4a_expected.md``, its revisions): an unopenable repository was named
``repo-not-a-root``; the A4 header printed twice; each takeable ref printed as ``refs/heads/refs/heads/...``."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levain.slot import Refused, adopt_verify
from levain.slot import adopt_take as AT
from levain.slot import adopt_view as AVW
from levain.slot import bundlefmt as BF
from levain.slot import runtime_helper as RH

S = "s-" + "a4" * 8
BASE = {"oid": "d" * 40, "object_format": "sha1"}


def _seams(answers):
    """His git answered from ``answers`` (argv words after ``-c core.hooksPath=/dev/null`` -> (rc, stdout, stderr));
    every call recorded."""
    calls = []

    def spawn(argv, **kw):
        assert "env" in kw
        assert argv[3:5] == [f"--git-dir={kw['cwd']}/.git", f"--work-tree={kw['cwd']}"]     # never discovered
        words = tuple(argv[5:7])
        calls.append(words)
        rc, out, err = answers[words]
        return subprocess.CompletedProcess(argv, rc, out, err)

    return AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git="git", spawn=spawn), calls


def test_view_unopenable_repo_is_repo_unreadable(tmp_path: Path) -> None:
    # git 2.54 without Rust refuses a repo with extensions.compatObjectFormat at its first command (s4a run 1, R5b).
    (tmp_path / ".git").mkdir()
    seams, calls = _seams({("config", "--local"): (128, b"", b"fatal: compatibility hash algorithm support requires "
                                                              b"Rust\n")})
    try:
        AVW.check_repo(seams, tmp_path, BASE)
    except Refused as e:
        assert e.name == "repo-unreadable:128" and "requires Rust" in e.detail
    else:
        raise AssertionError("an unopenable repository passed")
    assert calls == [("config", "--local")]           # nothing after the read that failed


def test_view_render_header_once_and_ref_names(tmp_path: Path) -> None:
    preview = (adopt_verify.PREVIEW_HEADER + "\nresult delta\nevil \x1b[31m\x07\n").encode()
    lines = AVW.render(session=S, result="delta", refs=["head", "wip"], preview=preview,
                       report={"named": [], "truncated": False}, repo=tmp_path, private=tmp_path / "p")
    text = "\n".join(lines)
    assert text.count(adopt_verify.PREVIEW_HEADER) == 1 and lines[0] == adopt_verify.PREVIEW_HEADER
    assert f"refs you can take: head -> refs/heads/levain/{S}, wip -> refs/heads/levain/{S}-wip" in lines
    assert "\x1b" not in text and "\x07" not in text and "\\u001b" in text


# --- fix1 (L3 s4a r1), each from a row that failed on the pre-fix tip (1023-8/adopt/s4af1_red_prefix.txt) ---------

GIT = shutil.which("git")
_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}


def _real_repo(d: Path) -> str:
    for a in (["init", "-q", str(d)], ["-C", str(d), "commit", "-q", "--allow-empty", "-m", "b"]):
        subprocess.run([GIT, *a], env=_ENV, check=True, capture_output=True)
    return subprocess.run([GIT, "-C", str(d), "rev-parse", "HEAD"], env=_ENV, check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.mark.skipif(GIT is None, reason="no git")
def test_view_bare_promisor_is_partial(tmp_path: Path) -> None:
    # complement MED 1: a bare `promisor` line is git's true; the pre-fix hand parser read it as false (row R8).
    oid = _real_repo(tmp_path)
    with open(tmp_path / ".git" / "config", "a") as fh:
        fh.write('[remote "origin"]\n\turl = /nonexistent\n\tpromisor\n')
    with pytest.raises(Refused) as e:
        AVW.check_repo(AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git=GIT), tmp_path,
                       {"oid": oid, "object_format": "sha1"})
    assert e.value.name == "repo-partial"


@pytest.mark.skipif(GIT is None, reason="no git")
def test_empty_git_dir_never_reaches_the_parent(tmp_path: Path) -> None:
    # complement MED 2: an empty .git inside a real repo; the pre-fix git walked up and wrote the parent (row R9).
    oid = _real_repo(tmp_path)
    child = tmp_path / "child"
    (child / ".git").mkdir(parents=True)
    before = subprocess.run([GIT, "-C", str(tmp_path), "for-each-ref"], env=_ENV, capture_output=True).stdout
    with pytest.raises(Refused) as e:
        AVW.check_repo(AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git=GIT), child,
                       {"oid": oid, "object_format": "sha1"})
    assert e.value.name.startswith("repo-unreadable:")
    assert AT._git(GIT, child, "update-ref", "refs/heads/probe", oid).returncode != 0
    assert subprocess.run([GIT, "-C", str(tmp_path), "for-each-ref"], env=_ENV, capture_output=True).stdout == before


def test_verify_wall_derives_from_the_helpers_bounds() -> None:
    # codex HIGH 2: the pre-fix fixed 4,200 s was below a valid adopt-verify deadline at V7's sizes (row H1).
    mx = RH.V7_BYTES + BF.HEADER_MAX
    got = AVW.helper_timeout("adopt-verify", bundle_bytes=mx, base_bytes=mx)
    assert got == RH.VerifySeams().lock_wait_s + RH.verify_wall_s(mx, mx) + RH.RESULT_GRACE_S + AVW.HELPER_SLACK_S
    assert got > 4200
    assert AVW.helper_timeout("adopt-open") == RH.AdoptSeams().lock_wait_s + RH.RESULT_GRACE_S + AVW.HELPER_SLACK_S


HOLDER = """import os, sys, time
os.setsid()                                  # out of the helper's group: no group signal reaches it
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(60)                               # and it holds the helper's stdout and stderr
"""


def test_timeout_stops_waiting_on_a_pipe_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # L3 s4a r2 codex HIGH + complement LOW: on 1e3a37a5 a helper that ignores TERM with a setsid'd grandchild holding
    # the pipe hung the CLI forever (the unbounded communicate after SIGKILL). The CLI returns within wall + grace.
    import threading

    monkeypatch.setattr(AVW, "STOP_GRACE_S", 1.0, raising=False)
    holder = tmp_path / "holder.py"
    holder.write_text(HOLDER)
    gpid, hpid = tmp_path / "g.pid", tmp_path / "h.pid"
    helper = tmp_path / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntrap '' TERM\necho $$ > '{hpid}'\n'{sys.executable}' -I '{holder}' '{gpid}' &\n"
                      "exec sleep 60\n")
    helper.chmod(0o755)
    seams = AVW.Seams(helper=(str(helper),), store=Path("/nonexistent"), helper_timeout_s=1.0)
    got: list = []

    def call() -> None:
        try:
            AVW.call_helper(seams, "status", S, err=io.StringIO())
            got.append(None)
        except Refused as e:
            got.append(e)

    t0 = time.monotonic()
    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(20)                                  # the test's own bound: the pre-fix code never returns
    took = time.monotonic() - t0
    try:
        assert got, "the CLI never stopped waiting"
        assert got[0] is not None and got[0].name == "helper-timeout", got
        assert "root's own wall reaps its sealed child" in got[0].detail
        assert took < 1.0 + 1.0 + 5, took       # wall + grace + slack for the spawn
    finally:
        for f in (gpid, hpid):
            try:
                os.kill(int(f.read_text()), 9)
            except (OSError, ValueError):
                pass


# --- fix3 (L3 s4a r3): the CLI never signals root; his own git is TERMed then KILLed ----------------------------

RECORDER = """
import os, signal, sys, time
log = sys.argv[1]
def got(n, _f):
    with open(log, "a") as f:
        f.write(signal.Signals(n).name + "\\n")
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
    signal.signal(sig, got)                   # records every signal it gets, and so ignores each one
with open(log + ".pid", "w") as f:
    f.write(str(os.getpid()))
end = time.monotonic() + 60
while time.monotonic() < end:
    time.sleep(0.05)
"""


def _recorder(tmp_path: Path) -> "tuple[Path, Path]":
    script = tmp_path / "rec.py"
    script.write_text(RECORDER)
    log = tmp_path / "signals"
    exe = tmp_path / "rec"
    exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' -I '{script}' '{log}'\n")
    exe.chmod(0o755)
    return exe, log


def _pid(log: Path) -> int:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            return int((log.parent / (log.name + ".pid")).read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the fake never started")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _signals(log: Path) -> "list[str]":
    try:
        return log.read_text().split()
    except FileNotFoundError:
        return []


def test_helper_timeout_sends_no_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # L3 s4a r3 codex HIGH: on 8fa0203e the CLI SIGTERMed the helper on its timeout (sudo relays it to root, which dies
    # before its seal-out). The CLI signals nothing and returns within wall + grace.
    monkeypatch.setattr(AVW, "STOP_GRACE_S", 1.0, raising=False)
    exe, log = _recorder(tmp_path)
    seams = AVW.Seams(helper=(str(exe),), store=Path("/nonexistent"), helper_timeout_s=2.0)
    t0 = time.monotonic()
    try:
        with pytest.raises(Refused) as e:
            AVW.call_helper(seams, "status", S, err=io.StringIO())
        took = time.monotonic() - t0
        pid = _pid(log)
        time.sleep(0.5)                          # a signal sent at the stop has time to land in the log
        assert e.value.name == "helper-timeout"
        assert took < 2.0 + 1.0 + 5, took
        assert _signals(log) == [] and _alive(pid), _signals(log)
    finally:
        try:
            os.kill(_pid(log), 9)
        except (OSError, AssertionError):
            pass


def test_helper_interrupt_sends_no_signal(tmp_path: Path) -> None:
    # L3 s4a r3 codex HIGH, its Ctrl-C half: on 8fa0203e the interrupt's stop SIGTERMed the helper too. The
    # KeyboardInterrupt is re-raised and the helper got nothing from the CLI (the SIGINT here is sent to the test's
    # own pid only; the terminal's Ctrl-C reaching the helper's group is root's to ignore, ``runtime_helper.wait_child``).
    import signal
    import threading

    exe, log = _recorder(tmp_path)
    seams = AVW.Seams(helper=(str(exe),), store=Path("/nonexistent"), helper_timeout_s=30.0)

    def interrupt() -> None:
        _pid(log)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            AVW.call_helper(seams, "status", S, err=io.StringIO())
        took = time.monotonic() - t0
        pid = _pid(log)
        time.sleep(0.5)
        assert _signals(log) == [] and _alive(pid), _signals(log)
        assert took < 10, took
    finally:
        try:
            os.kill(_pid(log), 9)
        except (OSError, AssertionError):
            pass


def test_local_git_ignoring_term_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # L3 s4a r3 codex MED: on 8fa0203e his own git that ignored TERM was never KILLed and ran on past the CLI's
    # repo-unreadable:timeout. It gets TERM, then KILL within the grace, and is reaped before the CLI returns.
    monkeypatch.setattr(AVW, "STOP_GRACE_S", 1.0, raising=False)
    monkeypatch.setattr(AVW, "REPO_GIT_TIMEOUT", 1.0, raising=False)
    exe, log = _recorder(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    seams = AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git=str(exe))
    t0 = time.monotonic()
    try:
        with pytest.raises(Refused) as e:
            AVW._git(seams, tmp_path / "repo", "config", "--local")
        took = time.monotonic() - t0
        pid = _pid(log)
        assert e.value.name == "repo-unreadable:timeout"
        assert took < 1.0 + 2 * 1.0 + 5, took
        assert _signals(log) == ["SIGTERM"], _signals(log)
        assert not _alive(pid)                   # KILLed and reaped (a zombie would still answer kill 0)
    finally:
        try:
            os.kill(_pid(log), 9)
        except (OSError, AssertionError):
            pass


def test_run_stays_in_the_callers_session() -> None:
    # L3 s4a r2 complement MED: a new session detaches the tty a requiretty / tty_tickets sudoers policy needs.
    r = AVW._run([sys.executable, "-I", "-c", "import os; print(os.getsid(0))"], env={"PATH": "/bin:/usr/bin"},
                 timeout=30)
    assert int(r.stdout) == os.getsid(0)


def test_object_format_needs_git_238(tmp_path: Path) -> None:
    # L3 s4a r2 complement LOW: a git before 2.38 has no --show-object-format; the refusal names the requirement.
    (tmp_path / ".git").mkdir()
    seams, _ = _seams({("config", "--local"): (1, b"", b""),
                       ("rev-parse", "--show-object-format"): (129, b"", b"error: unknown option\n")})
    with pytest.raises(Refused) as e:
        AVW.check_repo(seams, tmp_path, BASE)
    assert e.value.name == "repo-unreadable:129" and "needs git >= 2.38" in e.value.detail


def test_copy_private_unlinks_a_bad_copy(tmp_path: Path) -> None:
    # codex MED 7: the pre-fix copy left an unauthenticated destination behind (row C2).
    src_path = tmp_path / "src"
    src_path.write_bytes(b"x" * 100)
    fd = os.open(str(src_path), os.O_RDONLY)
    try:
        with pytest.raises(Refused) as e:
            AVW.copy_private(fd, 100, tmp_path / "dst", "0" * 64)
    finally:
        os.close(fd)
    assert e.value.name == "export-changed" and not (tmp_path / "dst").exists()


def test_warnings_shown_escaped_and_typed() -> None:
    # codex MED 6: the pre-fix CLI dropped every helper warning (rows W1, W2).
    err = io.StringIO()
    assert AVW.show_warnings({"warnings": ["fsync \x1b[31m"]}, "adopt-verify", err) == ["fsync \\u001b[31m"]
    assert err.getvalue() == "WARNING (adopt-verify): fsync \\u001b[31m\n"
    with pytest.raises(Refused) as e:
        AVW.show_warnings({"warnings": [7]}, "adopt-open", io.StringIO())
    assert e.value.name == "adopt-answer-malformed"
    with pytest.raises(Refused) as e:      # L3 s4a r2 complement LOW: a lone surrogate outside surrogateescape's range
        AVW.show_warnings({"warnings": ["\ud800"]}, "adopt-open", io.StringIO())
    assert e.value.name == "adopt-answer-malformed"


# --- S4b (1023+13): the prompt, the removal line, the store-verb walls; each from a row that failed on bac69c2a
# (project_memory/1023-13/adopt/s4b_red_base_git2540.txt: I1-I4, RM, W1) ----------------------------------------------


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_the_prompt_offers_only_the_carried_refs_reasks_and_reads_eof_as_none() -> None:
    out = io.StringIO()
    assert AVW.ask_take(["head"], _Tty("wip\nhead\n"), out) == "head"      # row I4: wip not offered, so asked again
    assert out.getvalue().count("take head / none? ") == 2
    out = io.StringIO()
    assert AVW.ask_take(["head", "wip"], _Tty("bogus\n"), out) == "none"    # rows I2 + I3: re-asked, then EOF
    assert out.getvalue().count("take head / wip / none? ") == 2


def test_the_removal_line_removes_the_private_dir(tmp_path: Path) -> None:
    import shlex

    priv = tmp_path / "levain-adopt-x y"          # a space: the printed command quotes it
    (priv).mkdir()
    (priv / "preview.txt").write_text("p")
    line = AVW.removal_line(priv)
    subprocess.run(shlex.split(line.split("with: ", 1)[1]), check=True)
    assert not priv.exists()


def test_store_verb_walls_derive_from_control_bounds_not_v7() -> None:
    import inspect

    from levain.slot.control_client import ControlClient

    answer = inspect.signature(ControlClient.__init__).parameters["timeout"].default
    want = answer + AVW.STORE_VERB_CAS_CALLS * (ControlClient.retry_window_s + answer) + AVW.STORE_SLACK_S
    assert AVW.helper_timeout("discard") == AVW.helper_timeout("restore") == want
    assert AVW.helper_timeout("status") == AVW.STORE_SLACK_S
    assert max(want, AVW.STORE_SLACK_S) < RH.V7_SECONDS
