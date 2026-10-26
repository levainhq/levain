"""levain.autonomic.hands_exec: the automation executor (autogrant A1b).

What runs for real here: both child scripts under the developer tools' Python 3.9 as this test's own uid (this Mac
has no hands user, so the expected uid is the operator's), the parent's disk reading after a real child, the
property script under /bin/sh, the interpreter selection and the one ``ls -lde`` call over this Mac's real trees,
and the module under root's /usr/bin/python3. What cannot run here: anything as a real hands user (sudo -n -u).
"""
from __future__ import annotations

import errno
import grp
import hashlib
import inspect
import json
import os
import platform
import subprocess
import sys
import types
from pathlib import Path

import pytest

import levain.autonomic.hands_exec as X
from levain.autonomic.executor import ExecutionResult, OutcomeUnknown
from levain.autonomic.journal import EffectNotStarted
from levain.hands_live import take_exclusive

ROOT = Path(__file__).resolve().parents[1]
DARWIN = platform.system() == "Darwin"
ME = os.getuid()


def _python() -> str:
    why, real, _rule = X._select_python(X._host() or "linux")
    if why is not None:
        pytest.skip(f"no interpreter the rule accepts here: {why}")
    return real


def _payload(user: str, *, body: str = "hello\n", name: str = "c-1.md", target: str = "notes/in",
             **over: object) -> str:
    doc = {"v": 1, "hands_user": user, "target": target, "name": name,
           "sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body}
    doc.update(over)
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _undo_payload(user: str, *, sha: str, name: str = "c-1.md", target: str = "notes/in") -> str:
    return json.dumps({"v": 1, "hands_user": user, "target": target, "name": name, "sha256": sha}, sort_keys=True)


def _executor(tmp_path: Path) -> X.HandsExecutor:
    ed = tmp_path / "ent"
    (ed / ".levain").mkdir(parents=True, exist_ok=True)
    return X.HandsExecutor(entity_dir=ed, user=X.hands_user_name(ed))


def _plan(ex: X.HandsExecutor, ws: Path, python: str = "/usr/bin/python3") -> X.HandsPlan:
    prefix = (X.SUDO, "-n", "-u", ex.user, "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", python)
    return X.HandsPlan(user=ex.user, uid=ME, home="/var/empty", workspace=ws, python=python, python_rule="strict",
                       prefix=prefix)


def _ready(ex: X.HandsExecutor, plan: X.HandsPlan) -> X.Reservation:
    return X.Reservation(ex, ready=True, plan=plan)


def _run_script(script: str, args: list, stdin: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", _python(), "-I", "-S", "-c", script,
                           *map(str, args)], input=stdin, capture_output=True, cwd="/", timeout=60)


# --- declarations ----------------------------------------------------------------------------------------------

def test_the_declared_types_and_their_defaults():
    assert issubclass(X.NotStarted, EffectNotStarted) and issubclass(EffectNotStarted, Exception)
    assert issubclass(OutcomeUnknown, Exception) and not issubclass(OutcomeUnknown, EffectNotStarted)
    r = ExecutionResult(ok=True)
    assert (r.outcome_unknown, r.started) == (False, True)


def test_preflight_takes_context_id_and_journaled_by_keyword_only_with_no_default():
    sig = inspect.signature(X.HandsExecutor.preflight)
    for name in ("context_id", "journaled"):
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty


def test_the_executor_stores_two_facts_and_cannot_be_shadowed(tmp_path):
    ex = _executor(tmp_path)
    assert not hasattr(ex, "__dict__") and X.HandsExecutor.name == "hands"
    with pytest.raises((AttributeError, TypeError)):
        ex.execute = lambda *a, **k: None   # type: ignore[method-assign]


def test_one_home_for_the_moved_hands_facts():
    from levain.firing import hands

    assert hands.HANDS_USER_RE is X.HANDS_USER_RE and hands.WORKSPACE_ROOT is X.WORKSPACE_ROOT
    assert hands.hands_user_name is X.hands_user_name
    assert (hands._DARWIN_IDS, hands._LINUX_IDS) == (X.HANDS_UID_RANGE["darwin"], X.HANDS_UID_RANGE["linux"])
    out = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "--untracked", "-E",
                          r"^(HANDS_USER_RE|WORKSPACE_ROOT|HANDS_UID_RANGE) = |^def hands_user_name\(", "--", "levain"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    # in hands_live, not the autonomic package: root's processes import setup without it
    assert {ln.split(":", 1)[0] for ln in out} == {"levain/hands_live.py"}, out
    assert X.HANDS_USER_RE is hands.HANDS_USER_RE


def test_the_plan_has_exactly_one_caller():
    # the design's grep (`\.plan(\|_plan(` over levain) also matches unrelated planners (init_plan, a cockpit's
    # self._plan(unit)); the plan takes no argument, so a call of it reads `_plan()` or `.plan()`
    def grep(*args):
        r = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "--untracked", "-F", *args],
                           capture_output=True, text=True)
        return r.stdout.splitlines()
    calls = grep("-e", "_plan()", "-e", ".plan()", "--", "levain")
    assert len(calls) == 1 and calls[0].startswith("levain/autonomic/hands_exec.py:"), calls
    assert "plan = executor._plan()" in calls[0]
    here = grep("_plan(", "--", "levain/autonomic/hands_exec.py")
    assert len(here) == 2 and any("def _plan(self)" in ln for ln in here), here


# --- preflight: each permanent reason, no I/O -------------------------------------------------------------------

def test_preflight_names_each_permanent_reason(tmp_path):
    ex = _executor(tmp_path)
    good = _payload(ex.user)
    pf = ex.preflight
    assert pf("write_document", good, context_id="c-1", journaled=True) is None
    assert pf("send_email", good, context_id="c-1", journaled=True) == "unknown_action"
    assert pf("write_document", _payload("_levain_other_abcdef"), context_id="c-1", journaled=True) == "wrong_entity"
    assert pf("write_document", "{not json", context_id="c-1", journaled=True) == "payload_mismatch"
    assert pf("write_document", good, context_id="c-2", journaled=True) == "payload_mismatch"   # name != id.md
    for target in ("", "a//b", "a/../b", ".", "/abs", "a/./b", "a\0b", ".levain-undone", ".levain-undone/x"):
        assert pf("write_document", _payload(ex.user, target=target), context_id="c-1",
                  journaled=True) == "payload_mismatch", target
    assert pf("write_document", _payload(ex.user, sha256="0" * 64), context_id="c-1",
              journaled=True) == "payload_mismatch"
    big = "x" * (X.MAX_DOCUMENT_BYTES + 1)
    assert pf("write_document", _payload(ex.user, body=big), context_id="c-1", journaled=True) == "payload_mismatch"
    assert pf("write_document", _payload(ex.user, extra="1"), context_id="c-1", journaled=True) == "payload_mismatch"
    assert pf("write_document", good, context_id="c-1", journaled=False) == "not_journaled"
    undo = _undo_payload(ex.user, sha="a" * 64)
    assert pf("undo_document", undo, context_id="c-1", journaled=False) is None
    assert pf("undo_document", undo, context_id="c-1", journaled=True) == "not_journaled"
    assert pf("undo_document", good, context_id="c-1", journaled=False) == "payload_mismatch"   # carries a body


@pytest.mark.parametrize("target", ["notes/\ud800x", "\udbff", "a/b\ud83d"])
def test_a_target_the_filesystem_cannot_encode_is_a_permanent_mismatch(tmp_path, monkeypatch, target):
    # B1-9 (complement LOW 2): a lone high surrogate passed the target rule, then the child's argv failed to encode
    # in Popen: spawn_failed, "may be delivered again", forever
    assert json.loads(json.dumps(target)) == target   # a JSON payload can carry it
    with pytest.raises(UnicodeEncodeError):
        os.fsencode(target)
    rec = _Recorder().install(monkeypatch)
    ex = _executor(tmp_path)
    p = _payload(ex.user, target=target)
    assert ex.preflight("write_document", p, context_id="c-1", journaled=True) == "payload_mismatch"
    with pytest.raises(X.NotStarted) as e:
        ex.execute_reserved("write_document", p, context_id="c-1", reservation=_ready(ex, _plan(ex, tmp_path)),
                            journaled=True)
    assert str(e.value) == "permanent:payload_mismatch" and rec.events == []


# --- execute: starts nothing; execute_reserved's non-starts record no hands argv -----------------------------------

class _Recorder:
    """Stands in for subprocess.Popen / run and the liveness and stop calls, recording each."""

    def __init__(self, popen_raises: BaseException | None = None, communicate_raises: BaseException | None = None,
                 kill_raises: BaseException | None = None, sweep_raises: BaseException | None = None):
        self.events: list = []
        self.popen_raises = popen_raises
        self.communicate_raises = communicate_raises
        self.kill_raises = kill_raises
        self.sweep_raises = sweep_raises

    def install(self, monkeypatch):
        rec = self
        real = subprocess

        class Pipe:
            def __init__(self, which):
                self.which = which

            def close(self):
                rec.events.append(("close", self.which))

        class P:
            def __init__(self, argv, **kw):
                rec.events.append(("popen", list(argv)))
                if rec.popen_raises is not None:
                    raise rec.popen_raises
                self.returncode = None
                self.stdin = Pipe("stdin") if kw.get("stdin") is real.PIPE else None
                self.stdout, self.stderr = Pipe("stdout"), Pipe("stderr")

            def communicate(self, input=None, timeout=None):
                rec.events.append(("communicate", timeout))
                if rec.communicate_raises is not None:
                    raise rec.communicate_raises
                self.returncode = 0
                return b"", b""

            def poll(self):
                return self.returncode

            def kill(self):
                rec.events.append(("kill",))
                if rec.kill_raises is not None:
                    raise rec.kill_raises

            def wait(self, timeout=None):
                rec.events.append(("wait", timeout))
                return -9

        def run(argv, **kw):
            rec.events.append(("run", list(argv)))
            return real.CompletedProcess(argv, 0, b"", b"")

        def sweep(user, **kw):
            rec.events.append(("sweep", user))
            if rec.sweep_raises is not None:
                raise rec.sweep_raises
            return None

        fake = types.SimpleNamespace(Popen=P, run=run, PIPE=real.PIPE, DEVNULL=real.DEVNULL,
                                     TimeoutExpired=real.TimeoutExpired, CompletedProcess=real.CompletedProcess)
        monkeypatch.setattr(X, "subprocess", fake)
        monkeypatch.setattr(X, "entity_session_live",
                            lambda uid, **kw: rec.events.append(("live", uid)) or False)
        monkeypatch.setattr(X, "sweep_hands_user", sweep)
        return self

    def kinds(self):
        return [k for k, *_ in self.events]

    def argvs(self):
        return [e for e in self.events if e[0] in ("popen", "run")]


def test_execute_starts_nothing(tmp_path, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    ex = _executor(tmp_path)
    r = ex.execute("write_document", _payload(ex.user), context_id="c-1")
    assert (r.ok, r.error, r.started) == (False, "not_reserved", False)
    assert rec.events == []


def test_every_not_started_path_records_no_hands_argv(tmp_path, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    ex = _executor(tmp_path)
    plan = _plan(ex, tmp_path)
    good = _payload(ex.user)
    other = X.HandsExecutor(entity_dir=ex.entity_dir, user=ex.user)
    cases = [
        (None, "no_reservation"),
        (X.Reservation(other, ready=True, plan=plan), "no_reservation"),          # another executor's
        (X.Reservation(ex, ready=False, reason="hands_busy:lock"), "no_reservation"),
        (X.Reservation(ex, ready=True, plan=None), "no_reservation"),
    ]
    closed = _ready(ex, plan)
    closed.open = False
    cases.append((closed, "reservation_closed"))
    for res, why in cases:
        with pytest.raises(X.NotStarted) as e:
            ex.execute_reserved("write_document", good, context_id="c-1", reservation=res, journaled=True)
        assert str(e.value) == why
    for args, why in ((("send_email", good, "c-1", True), "permanent:unknown_action"),
                      (("write_document", _payload("_levain_other_abcdef"), "c-1", True), "permanent:wrong_entity"),
                      (("write_document", good, "c-9", True), "permanent:payload_mismatch"),
                      (("write_document", good, "c-1", False), "permanent:not_journaled")):
        with pytest.raises(X.NotStarted) as e:
            ex.execute_reserved(args[0], args[1], context_id=args[2], reservation=_ready(ex, plan), journaled=args[3])
        assert str(e.value) == why
    assert rec.argvs() == [] and rec.events == []


def test_a_popen_constructor_oserror_is_a_non_start_with_its_errno_and_no_sweep(tmp_path, monkeypatch):
    rec = _Recorder(popen_raises=OSError(errno.EAGAIN, "Resource temporarily unavailable")).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(X.NotStarted) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert str(e.value) == f"spawn_failed:{errno.EAGAIN}"
    assert (str(e.value) == "spawn_failed:35") is DARWIN or not DARWIN
    assert [k for k, *_ in rec.events] == ["popen"]   # the attempt itself, no sweep, no liveness, no run


def test_a_popen_constructor_non_oserror_is_a_non_start_by_its_type_name(tmp_path, monkeypatch):
    # RED against r6.4's `except OSError` (mutation-checked in this lane's RETURN): the ValueError escaped
    rec = _Recorder(popen_raises=ValueError("embedded null byte")).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(X.NotStarted) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert str(e.value) == "spawn_failed:ValueError"
    assert not any(k == "sweep" for k, *_ in rec.events)


def test_a_child_that_outlives_its_wait_is_unknown_and_the_hands_user_is_swept(tmp_path, monkeypatch):
    rec = _Recorder(communicate_raises=subprocess.TimeoutExpired(["x"], 1)).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert str(e.value) == "timeout:not_read"
    # the sweep before the reap: a parent the kill cannot stop (sudo, root) exits once its hands child is gone
    assert rec.kinds() == ["popen", "communicate", "kill", "sweep", "wait", "close", "close", "close"]


def test_a_kill_the_root_parent_refuses_still_sweeps_then_reaps_and_closes_the_pipes(tmp_path, monkeypatch):
    # B1-3 (complement LOW 1): the child's parent is sudo (root), so kill() from the operator uid can raise
    # PermissionError; the reap and the pipe close must not depend on it
    rec = _Recorder(communicate_raises=subprocess.TimeoutExpired(["x"], 1),
                    kill_raises=PermissionError(errno.EPERM, "Operation not permitted")).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert str(e.value) == "timeout:not_read"
    kinds = rec.kinds()
    assert kinds.index("kill") < kinds.index("sweep") < kinds.index("wait")
    assert sorted(e[1] for e in rec.events if e[0] == "close") == ["stderr", "stdin", "stdout"]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1)])
def test_an_interrupt_during_the_wait_kills_sweeps_reaps_and_closes_then_passes_through(tmp_path, monkeypatch,
                                                                                         interrupt):
    # B1-3 (codex MED): a BaseException in communicate skipped the kill and the sweep, and the reservation's lock
    # was released with the child still running. After the cleanup the interrupt passes through, as every
    # BaseException does in this module; the journal records the effect `unknown` on it
    rec = _Recorder(communicate_raises=interrupt).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(type(interrupt)):
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert rec.kinds() == ["popen", "communicate", "kill", "sweep", "wait", "close", "close", "close"]


def test_a_sweep_that_raises_after_the_child_ran_is_unknown_by_its_type(tmp_path, monkeypatch):
    # B1-6: sweep_hands_user can raise what it does not catch (entity_session_live's text-mode run() decodes the
    # process table and catches only OSError and TimeoutExpired: a UnicodeDecodeError passes both)
    bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    rec = _Recorder(sweep_raises=bad).install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    assert str(e.value) == "exit_0:sweep:UnicodeDecodeError"
    assert rec.kinds()[-4:] == ["wait", "close", "close", "close"]


def test_nothing_between_the_reservation_and_the_child_calls_liveness_or_run(tmp_path, monkeypatch):
    rec = _Recorder().install(monkeypatch)
    ex = _executor(tmp_path)
    with pytest.raises(OutcomeUnknown):   # the recorder's child "exits 0" but writes nothing: disk says absent
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                            reservation=_ready(ex, _plan(ex, tmp_path)), journaled=True)
    kinds = [k for k, *_ in rec.events]
    assert kinds[0] == "popen" and "live" not in kinds and "run" not in kinds
    popen_argv = rec.events[0][1]
    assert popen_argv[:4] == [X.SUDO, "-n", "-u", ex.user] and popen_argv[8:12] == ["-I", "-S", "-c", X._WRITE_SCRIPT]


# --- the scripts, for real, under the developer tools' Python 3.9 ------------------------------------------------

def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def test_write_script_rows(tmp_path):
    ws = _ws(tmp_path)
    body = b"one document\n"
    ok = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-1.md", "tok", "a", "b"], body)
    assert ok.returncode == 0, ok.stderr
    doc = ws / "a" / "b" / "c-1.md"
    assert doc.read_bytes() == body and (doc.stat().st_mode & 0o777) == 0o644
    assert json.loads(ok.stdout)["size"] == len(body)
    assert sorted(os.listdir(ws / "a" / "b")) == ["c-1.md"]
    # exists: never replaced, the tmp removed
    again = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-1.md", "tok", "a", "b"], b"other")
    assert again.returncode == 6 and doc.read_bytes() == body and sorted(os.listdir(ws / "a" / "b")) == ["c-1.md"]
    # a symlink at the name: link refuses, nothing written through it
    (ws / "a" / "b" / "c-2.md").symlink_to(tmp_path / "outside")
    assert _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-2.md", "tok", "a", "b"], body).returncode == 6
    assert not (tmp_path / "outside").exists()
    # not the expected uid: exit 3 before touching anything
    assert _run_script(X._WRITE_SCRIPT, [ME + 1, 30, ws, "c-3.md", "tok", "z"], body).returncode == 3
    assert not (ws / "z").exists()
    # a workspace owned by another uid: exit 4
    other = "/private/tmp" if DARWIN else "/tmp"
    assert _run_script(X._WRITE_SCRIPT, [ME, 30, other, "c-4.md", "tok"], body).returncode == 4
    # over the cap: exit 5, no tmp left
    big = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-5.md", "tok", "a"], b"x" * (X.MAX_DOCUMENT_BYTES + 1))
    assert big.returncode == 5 and os.listdir(ws / "a") == ["b"]
    # past the deadline: exit 7, no document, no tmp
    late = _run_script(X._WRITE_SCRIPT, [ME, 0, ws, "c-6.md", "tok", "a"], body)
    assert late.returncode == 7 and os.listdir(ws / "a") == ["b"]
    # a symlinked component: refused, nothing written past it
    (ws / "link").symlink_to(ws / "a")
    bad = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-7.md", "tok", "link", "b"], body)
    assert bad.returncode not in (0, 6) and not (ws / "a" / "b" / "c-7.md").exists()
    # B1-7: the temporary file is the parent's name (the token), so an earlier run's leftover beside it is neither
    # touched nor in the way; and this run's own name already taken is never written through (O_EXCL)
    stale = ws / "a" / "b" / ".c-8.md.levain-write-4242"
    stale.write_bytes(b"from a crashed run")
    ok8 = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-8.md", "t8", "a", "b"], body)
    assert ok8.returncode == 0, ok8.stderr
    assert (ws / "a" / "b" / "c-8.md").read_bytes() == body and stale.read_bytes() == b"from a crashed run"
    assert not (ws / "a" / "b" / ".c-8.md.levain-write-t8").exists()
    taken = ws / "a" / "b" / ".c-9.md.levain-write-t9"
    taken.write_bytes(b"in the way")
    t9 = _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-9.md", "t9", "a", "b"], body)
    assert t9.returncode not in (0, 6) and not (ws / "a" / "b" / "c-9.md").exists()
    assert taken.read_bytes() == b"in the way"


def test_undo_script_rows(tmp_path):
    ws = _ws(tmp_path)
    body = b"one document\n"
    sha = hashlib.sha256(body).hexdigest()
    assert _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-1.md", "tok", "a"], body).returncode == 0
    # changed bytes: refused, nothing moved
    assert _run_script(X._UNDO_SCRIPT, [ME, 30, ws, "0" * 64, "c-1.md", "a"]).returncode == 8
    assert (ws / "a" / "c-1.md").exists() and not (ws / X.UNDONE_DIR).exists()
    ok = _run_script(X._UNDO_SCRIPT, [ME, 30, ws, sha, "c-1.md", "a"])
    assert ok.returncode == 0, ok.stderr
    assert not (ws / "a" / "c-1.md").exists() and (ws / X.UNDONE_DIR / "c-1.md").read_bytes() == body
    # absent now: exit 9
    assert _run_script(X._UNDO_SCRIPT, [ME, 30, ws, sha, "c-1.md", "a"]).returncode == 9
    # a second document of the same name: the moved one is never replaced
    assert _run_script(X._WRITE_SCRIPT, [ME, 30, ws, "c-1.md", "tok", "a"], body).returncode == 0
    assert _run_script(X._UNDO_SCRIPT, [ME, 30, ws, sha, "c-1.md", "a"]).returncode == 6
    assert (ws / "a" / "c-1.md").exists()
    assert _run_script(X._UNDO_SCRIPT, [ME + 1, 30, ws, sha, "c-1.md", "a"]).returncode == 3


def test_both_scripts_compile_under_the_selected_python():
    for script in (X._WRITE_SCRIPT, X._UNDO_SCRIPT):
        r = subprocess.run(["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", _python(), "-I", "-S", "-c",
                            "import sys; compile(sys.stdin.read(), 's', 'exec'); print(sys.version_info[:2])"],
                           input=script.encode(), capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr
    print(f"  {_python()}: {r.stdout.decode().strip()}")


# --- the parent end to end, the real child as this uid ------------------------------------------------------------

def _real_child(monkeypatch, *, after=None, swept=None):
    """Popen runs the real child, with the sudo / env prefix cut off (it runs as this test's uid)."""
    real_popen = subprocess.Popen
    seen = []

    def popen(argv, **kw):
        if "-I" not in argv:   # an operator-side call (the interpreter selection's ls): as it is
            return real_popen(argv, **kw)
        i = argv.index("-I")
        cut = ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", argv[i - 1], *argv[i:]]
        seen.append(cut)
        p = real_popen(cut, **kw)
        if after is not None:
            orig = p.communicate

            def communicate(*a, **k):
                out = orig(*a, **k)
                after()
                return out
            p.communicate = communicate
        return p

    fake = types.SimpleNamespace(Popen=popen, run=subprocess.run, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                                 TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess)
    monkeypatch.setattr(X, "subprocess", fake)
    monkeypatch.setattr(X, "sweep_hands_user", lambda user, **kw: swept)
    return seen


def test_a_real_write_is_ok_only_as_the_disk_shows_it(tmp_path, monkeypatch):
    seen = _real_child(monkeypatch)
    ex = _executor(tmp_path)
    ws = _ws(tmp_path)
    plan = _plan(ex, ws, _python())
    r = ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1", reservation=_ready(ex, plan),
                            journaled=True)
    assert r.ok and r.detail == str(ws / "notes" / "in" / "c-1.md") and r.downstream_id == f"{ex.user}:notes/in/c-1.md"
    assert (ws / "notes" / "in" / "c-1.md").read_text() == "hello\n" and len(seen) == 1
    # the same name again: the child exits 6, which is UNKNOWN, never ok and never a plain failure
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user, body="other\n"), context_id="c-1",
                            reservation=_ready(ex, plan), journaled=True)
    assert str(e.value) == "exit_6:exists:not_read"


def test_not_quiescent_after_the_child_is_unknown_and_the_disk_is_not_read(tmp_path, monkeypatch):
    _real_child(monkeypatch, swept="processes of x are still running")
    ex = _executor(tmp_path)
    plan = _plan(ex, _ws(tmp_path), _python())
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1", reservation=_ready(ex, plan),
                            journaled=True)
    assert str(e.value) == "exit_0:not_quiescent"


def test_a_second_link_or_a_tmp_left_after_an_exit_0_is_unknown(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    ws = _ws(tmp_path)
    plan = _plan(ex, ws, _python())
    doc = ws / "notes" / "in" / "c-1.md"
    _real_child(monkeypatch, after=lambda: os.link(doc, ws / "hard"))
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1", reservation=_ready(ex, plan),
                            journaled=True)
    assert str(e.value) == "exit_0:linked"
    # THIS child's temporary file left beside the document (its name is the one the parent gave the child)
    seen = _real_child(monkeypatch, after=lambda: (ws / "notes" / "in" / _tmp_of(seen[-1])).write_text("left"))
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user, name="c-2.md"), context_id="c-2",
                            reservation=_ready(ex, plan), journaled=True)
    assert str(e.value) == "exit_0:tmp_left"
    _real_child(monkeypatch, after=lambda: (ws / "notes" / "in" / "c-3.md").write_text("swapped"))
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("write_document", _payload(ex.user, name="c-3.md"), context_id="c-3",
                            reservation=_ready(ex, plan), journaled=True)
    assert str(e.value) == "exit_0:other_bytes"


def _tmp_of(argv: list) -> str:
    """The temporary name the parent gave the write child: ``.<name>.levain-write-<token>``."""
    i = argv.index("-c")
    return "." + argv[i + 5] + ".levain-write-" + argv[i + 6]


def test_a_stale_tmp_from_an_earlier_run_is_reported_and_never_tmp_left(tmp_path, monkeypatch):
    # B1-7: a crashed or killed earlier write leaves `.<name>.levain-write-<old>`; every later success read it as
    # this child's and answered tmp_left, forever. The parent judges only the name it gave this child
    ex = _executor(tmp_path)
    ws = _ws(tmp_path)
    plan = _plan(ex, ws, _python())
    d = ws / "notes" / "in"
    d.mkdir(parents=True)
    (d / ".c-1.md.levain-write-4242").write_text("from a crashed run")
    _real_child(monkeypatch)
    r = ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1", reservation=_ready(ex, plan),
                            journaled=True)
    assert r.ok and r.downstream_id == f"{ex.user}:notes/in/c-1.md"
    assert r.detail == f"{d / 'c-1.md'} (left by an earlier run: .c-1.md.levain-write-4242)"
    assert (d / "c-1.md").read_text() == "hello\n" and (d / ".c-1.md.levain-write-4242").exists()


def test_a_real_undo_moves_the_document_only_with_its_recorded_bytes(tmp_path, monkeypatch):
    _real_child(monkeypatch)
    ex = _executor(tmp_path)
    ws = _ws(tmp_path)
    plan = _plan(ex, ws, _python())
    assert ex.execute_reserved("write_document", _payload(ex.user), context_id="c-1", reservation=_ready(ex, plan),
                               journaled=True).ok
    sha = hashlib.sha256(b"hello\n").hexdigest()
    with pytest.raises(OutcomeUnknown) as e:
        ex.execute_reserved("undo_document", _undo_payload(ex.user, sha="1" * 64), context_id="c-1",
                            reservation=_ready(ex, plan), journaled=False)
    assert str(e.value) == "exit_8:changed:not_read" and (ws / "notes" / "in" / "c-1.md").exists()
    r = ex.execute_reserved("undo_document", _undo_payload(ex.user, sha=sha), context_id="c-1",
                            reservation=_ready(ex, plan), journaled=False)
    assert r.ok and r.downstream_id == f"{ex.user}:{X.UNDONE_DIR}/c-1.md"
    assert (ws / X.UNDONE_DIR / "c-1.md").read_text() == "hello\n" and not (ws / "notes" / "in" / "c-1.md").exists()


# --- last_check ----------------------------------------------------------------------------------------------------

def test_last_check_answers_live_cannot_tell_or_none_and_never_raises(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    res = _ready(ex, _plan(ex, tmp_path))
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: True)
    assert ex.last_check(res) == "live"
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: False)
    assert ex.last_check(res) is None

    def boom(uid, **kw):
        raise X.HandsLiveError("cannot tell")
    monkeypatch.setattr(X, "entity_session_live", boom)
    assert ex.last_check(res) == "cannot_tell"
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: 1 / 0)
    assert ex.last_check(res) == "cannot_tell"
    assert ex.last_check(None) == "cannot_tell" and ex.last_check(X.Reservation(ex, ready=False)) == "cannot_tell"


# --- hands_reserved -------------------------------------------------------------------------------------------------

def _stub_plan(monkeypatch, ex, events, answer):
    monkeypatch.setattr(X.HandsExecutor, "_plan", lambda self: events.append("plan") or answer)


def test_busy_on_a_held_shared_lock_and_the_plan_never_runs(tmp_path, monkeypatch):
    import fcntl

    ex = _executor(tmp_path)
    events: list = []
    _stub_plan(monkeypatch, ex, events, _plan(ex, tmp_path))
    fd = os.open(ex.entity_dir / ".levain" / "hands.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)   # a session's take (hold_session_lock)
    try:
        with X.hands_reserved(ex) as r:
            assert (r.ready, r.reason, r.open) == (False, "hands_busy:lock", False)
    finally:
        os.close(fd)
    assert events == []


def test_a_lock_file_that_cannot_be_opened_is_unready_by_name(tmp_path):
    ed = tmp_path / "ent"
    ed.mkdir()   # no .levain directory
    ex = X.HandsExecutor(entity_dir=ed, user=X.hands_user_name(ed))
    with X.hands_reserved(ex) as r:
        assert (r.ready, r.reason) == (False, "hands_unready:lock:FileNotFoundError")


def test_the_order_lock_then_plan_then_liveness_and_the_lock_held_only_while_ready(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    events: list = []
    plan = _plan(ex, tmp_path)
    _stub_plan(monkeypatch, ex, events, plan)
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: events.append("live") or False)
    with X.hands_reserved(ex) as r:
        assert r.ready and r.plan is plan and r.open and events == ["plan", "live"]
        assert take_exclusive(ex.entity_dir) is None   # held for the block
    assert not r.open
    fd = take_exclusive(ex.entity_dir)
    assert fd is not None   # released at exit
    os.close(fd)


@pytest.mark.parametrize("answer, reason", [("live", "hands_busy:live"), ("raise", "hands_busy:cannot_tell"),
                                            ("plan", "python_unproven:probe_exit_3")])
def test_not_ready_releases_the_lock_before_the_block(tmp_path, monkeypatch, answer, reason):
    ex = _executor(tmp_path)
    events: list = []
    _stub_plan(monkeypatch, ex, events, "python_unproven:probe_exit_3" if answer == "plan" else _plan(ex, tmp_path))

    def live(uid, **kw):
        events.append("live")
        if answer == "raise":
            raise X.HandsLiveError("x")
        return answer == "live"
    monkeypatch.setattr(X, "entity_session_live", live)
    with X.hands_reserved(ex) as r:
        assert (r.ready, r.reason, r.open) == (False, reason, False)
        fd = take_exclusive(ex.entity_dir)
        assert fd is not None
        os.close(fd)
    assert events == (["plan"] if answer == "plan" else ["plan", "live"])


def test_a_held_handle_is_never_taken_again(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    events: list = []
    _stub_plan(monkeypatch, ex, events, _plan(ex, tmp_path))
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: False)
    takes = []
    real_take = X.take_exclusive
    monkeypatch.setattr(X, "take_exclusive", lambda d: takes.append(d) or real_take(d))
    with X.hands_reserved(ex) as outer:
        with X.hands_reserved(ex, held=outer) as inner:
            assert inner is outer and inner.open
        assert outer.open   # the nested block does not close the taker's reservation
    assert len(takes) == 1 and events == ["plan"]


def test_a_subclass_is_refused_at_every_proof_boundary(tmp_path, monkeypatch):
    # B1-4 (codex MED + complement LOW 3): `isinstance` let a subclass's own _plan run inside hands_reserved, and
    # its forged plan was yielded as proven; the inherited execute_reserved then launched the forged prefix
    ex = _executor(tmp_path)
    forged = X.HandsPlan(user=ex.user, uid=ME, home="/", workspace=tmp_path, python="/bin/sh", python_rule="strict",
                         prefix=("/bin/sh",))
    ran: list = []

    class Sub(X.HandsExecutor):
        __slots__ = ()

        def _plan(self):
            ran.append("plan")
            return forged

    sub = Sub(entity_dir=ex.entity_dir, user=ex.user)
    monkeypatch.setattr(X, "entity_session_live", lambda uid, **kw: False)
    monkeypatch.setattr(X, "take_exclusive", lambda d: pytest.fail("took the lock for a subclass"))
    with X.hands_reserved(sub) as r:
        assert (r.ready, r.reason, r.plan, r.open) == (False, "hands_unready:executor_type", None, False)
    assert ran == []
    rec = _Recorder().install(monkeypatch)
    with pytest.raises(X.NotStarted) as e:   # a reservation it built for itself
        sub.execute_reserved("write_document", _payload(ex.user), context_id="c-1",
                             reservation=X.Reservation(sub, ready=True, plan=forged), journaled=True)
    assert str(e.value) == "no_reservation" and rec.events == []
    assert sub.last_check(X.Reservation(sub, ready=True, plan=forged)) == "cannot_tell"


def test_another_executor_type_is_ready_and_takes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "take_exclusive", lambda d: pytest.fail("took the lock"))
    with X.hands_reserved(object()) as r:
        assert r.ready and r.plan is None and r.open
    assert not r.open


# --- static_problem: each clause by name ----------------------------------------------------------------------------

def _pw(uid=450, gid=450, home="/var/empty"):
    return types.SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_dir=home)


def _static_env(monkeypatch, tmp_path, ex, *, pw=None, groups=(450,)):
    real_pwd = X.pwd
    monkeypatch.setattr(X, "pwd", types.SimpleNamespace(
        getpwnam=lambda n: (pw or _pw()) if n == ex.user else real_pwd.getpwnam(n), getpwuid=real_pwd.getpwuid))
    monkeypatch.setattr(X.os, "getgrouplist", lambda user, gid: list(groups))
    host = X._host()
    monkeypatch.setitem(X.WORKSPACE_ROOT, host, tmp_path / "wsroot")
    return host


def test_static_problem_names_each_identity_and_workspace_clause(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    host = _static_env(monkeypatch, tmp_path, ex)
    assert X.HandsExecutor(entity_dir=ex.entity_dir, user="bob").static_problem() == "hands_user_unproven:name"
    other = X.HandsExecutor(entity_dir=ex.entity_dir, user="_levain_other_abcdef")
    assert other.static_problem() == "hands_user_unproven:not_this_entity"
    _static_env(monkeypatch, tmp_path, ex, pw=_pw(uid=ME))
    assert ex.static_problem() == "hands_user_unproven:uid"
    _static_env(monkeypatch, tmp_path, ex, pw=_pw(uid=0))
    assert ex.static_problem() == "hands_user_unproven:uid"
    _static_env(monkeypatch, tmp_path, ex, pw=_pw(uid=600))
    assert ex.static_problem() == "hands_user_unproven:uid_range"
    staff = grp.getgrnam("staff").gr_gid if DARWIN else os.getgid()
    _static_env(monkeypatch, tmp_path, ex, groups=(450, 12, 61, staff))
    assert ex.static_problem() == f"hands_user_unproven:group:{min({staff, os.getgid()} & {450, 12, 61, staff})}"
    if DARWIN:
        admin = grp.getgrnam("admin").gr_gid
        _static_env(monkeypatch, tmp_path, ex, groups=(450, 12, 61, 701, admin))
        assert ex.static_problem() == f"hands_user_unproven:group:{admin}"
        # Darwin's universal memberships (everyone 12, localaccounts 61, 100, 701) are not refused
        _static_env(monkeypatch, tmp_path, ex, groups=(450, 12, 61, 100, 701))
    else:
        _static_env(monkeypatch, tmp_path, ex)
    assert ex.static_problem() == "workspace_unproven"            # absent
    ws = tmp_path / "wsroot" / ex.user / "workspace"
    ws.parent.mkdir(parents=True)
    ws.symlink_to(tmp_path)
    assert ex.static_problem() == "workspace_unproven"            # a link
    ws.unlink()
    ws.mkdir()
    assert ex.static_problem() == "workspace_unproven"            # owned by this uid, not the hands uid
    monkeypatch.setattr(X.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    assert ex.static_problem() == "hands_user_unproven:no_user"
    assert host in ("darwin", "linux")


def test_an_untrusted_anchor_is_named(tmp_path):
    f = tmp_path / "sudo"
    f.write_text("#!/bin/sh\n")
    why = X._anchor(str(f))
    assert why is not None and why.startswith("not_owned:")
    assert X._anchor("/bin/sh") is None and X._anchor(X.SUDO) is None


# --- the interpreter ------------------------------------------------------------------------------------------------

@pytest.mark.skipif(not DARWIN, reason="this Mac's measured trees")
def test_on_this_mac_c1_is_selected_under_the_strict_rule():
    why, real, rule = X._select_python("darwin")
    if not os.path.lexists(X._CLT_ROOT):
        pytest.skip("no Command Line Tools here")
    assert (why, rule) == (None, "strict") and real.startswith(X._CLT_ROOT + "/")
    print(f"  selected {real} ({rule})")


@pytest.mark.skipif(not DARWIN or not os.path.exists("/Applications/Xcode.app/Contents/Developer/usr/bin/python3"),
                    reason="this Mac's Xcode tree")
def test_the_xcode_chain_passes_only_under_the_applications_exception():
    from levain.trust_anchor import trust_anchor_fault, trust_anchor_problem

    c2 = "/Applications/Xcode.app/Contents/Developer/usr/bin/python3"
    admin = grp.getgrnam("admin").gr_gid
    assert trust_anchor_fault(c2, what="python3") == ("group_writable:/Applications", None)
    why, real = trust_anchor_fault(c2, what="python3", group_write_exempt=("/Applications", admin))
    assert why is None and real.startswith("/Applications/Xcode.app/")
    assert X.acl_problem(X._chain(real), host="darwin") is None
    # the exemption is the exact path and gid, nothing else
    assert trust_anchor_fault(c2, what="python3", group_write_exempt=("/Applications/Xcode.app", admin))[0] == \
        "group_writable:/Applications"
    assert trust_anchor_fault(c2, what="python3", group_write_exempt=("/Applications", admin + 1))[0] == \
        "group_writable:/Applications"
    # the sentence form is unchanged for every other caller
    assert trust_anchor_problem(c2, what="python3")[0] == "python3 directory /Applications is group- or world-writable"


@pytest.mark.skipif(not DARWIN, reason="this Mac's Xcode tree")
def test_xcode_app_with_the_applications_mode_and_gid_still_fails(monkeypatch):
    import levain.trust_anchor as ta

    c2 = "/Applications/Xcode.app/Contents/Developer/usr/bin/python3"
    if not os.path.exists(c2):
        pytest.skip("no Xcode here")
    admin = grp.getgrnam("admin").gr_gid
    real_lstat = os.lstat

    def lstat(p):
        st = real_lstat(p)
        if p == "/Applications/Xcode.app":
            vals = list(st)
            vals[0] = st.st_mode | 0o020
            vals[5] = admin
            return os.stat_result(vals)
        return st
    monkeypatch.setattr(ta.os, "lstat", lstat)
    why, _ = ta.trust_anchor_fault(c2, what="python3", group_write_exempt=("/Applications", admin))
    assert why == "group_writable:/Applications/Xcode.app"


def _tree(root: Path, *, gw: str | None = None) -> Path:
    """A scratch interpreter tree <root>/usr/bin/python3, every component 0755 except ``gw`` (0775)."""
    py = root / "usr" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    py.chmod(0o755)
    for p in (root, root / "usr", root / "usr" / "bin"):
        p.chmod(0o755)
    if gw is not None:
        (root / gw).chmod(0o775)
    return py


def _selection_env(monkeypatch, tmp_path, *, c1_root: Path, c2_dev: Path | None, xs_exit: int = 0):
    """The selection over scratch trees: owners root or this uid (the strict rule otherwise unchanged), the ACL
    call stood in (its own tests are above and below), xcode-select answering ``c2_dev``."""
    import levain.trust_anchor as ta

    monkeypatch.setattr(X, "_CLT_ROOT", str(c1_root))
    real_fault = ta.trust_anchor_fault
    monkeypatch.setattr(X, "trust_anchor_fault", lambda p, **kw: real_fault(p, owners=frozenset({0, ME}), **kw))
    monkeypatch.setattr(X, "acl_problem", lambda paths, host: None)
    monkeypatch.setattr(X, "_anchor", lambda p: None)
    out = (str(c2_dev) + "\n").encode() if c2_dev is not None else b""
    monkeypatch.setattr(X, "_call", lambda argv, **kw: subprocess.CompletedProcess(argv, xs_exit, out, b""))


@pytest.mark.skipif(not DARWIN, reason="the Darwin candidates")
def test_a_failing_c1_refuses_and_c2_is_never_tried(tmp_path, monkeypatch):
    c1 = tmp_path / "clt"
    _tree(c1, gw="usr")
    _tree(tmp_path / "dev")
    _selection_env(monkeypatch, tmp_path, c1_root=c1, c2_dev=tmp_path / "dev")
    why, real, _ = X._select_python("darwin")
    assert (why, real) == (f"python_unproven:group_writable:{os.path.realpath(c1 / 'usr')}", None)


@pytest.mark.skipif(not DARWIN, reason="the Darwin candidates")
def test_an_absent_clt_root_falls_through_to_c2(tmp_path, monkeypatch):
    dev = tmp_path / "dev"
    _tree(dev)
    _selection_env(monkeypatch, tmp_path, c1_root=tmp_path / "no-clt", c2_dev=dev)
    why, real, rule = X._select_python("darwin")
    assert (why, rule) == (None, "strict") and real == os.path.realpath(dev / "usr" / "bin" / "python3")


@pytest.mark.skipif(not DARWIN, reason="the Darwin candidates")
def test_an_entry_at_the_clt_root_that_does_not_resolve_refuses(tmp_path, monkeypatch):
    # RED against D16's letter (a full-path ENOENT read as absent would select C2): mutation-checked in RETURN
    dev = tmp_path / "dev"
    _tree(dev)
    partial = tmp_path / "partial"
    (partial / "usr").mkdir(parents=True)
    _selection_env(monkeypatch, tmp_path, c1_root=partial, c2_dev=dev)
    assert X._select_python("darwin") == ("python_unproven:c1:ENOENT", None, "")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "unmounted-volume")
    _selection_env(monkeypatch, tmp_path, c1_root=dangling, c2_dev=dev)
    assert X._select_python("darwin") == ("python_unproven:c1:ENOENT", None, "")


@pytest.mark.skipif(not DARWIN, reason="the Darwin candidates")
def test_no_developer_tools_refuses_by_name(tmp_path, monkeypatch):
    _selection_env(monkeypatch, tmp_path, c1_root=tmp_path / "no-clt", c2_dev=None, xs_exit=2)
    why, real, _ = X._select_python("darwin")
    assert (why, real) == ("python_unproven:no_developer_tools:exit_2", None)
    _selection_env(monkeypatch, tmp_path, c1_root=tmp_path / "no-clt", c2_dev=tmp_path / "missing")
    assert X._select_python("darwin")[0] == "python_unproven:no_developer_tools:absent"
    assert "xcode-select --install" in X.CLT_REMEDY and "/Library/Developer/CommandLineTools" in X.CLT_REMEDY
    assert X.NO_TOOLS_REMEDY == "install them with xcode-select --install"
    assert X.remedy("python_unproven:c1:ENOENT") == X.CLT_REMEDY
    assert X.remedy("python_unproven:no_developer_tools:exit_2") == X.NO_TOOLS_REMEDY
    assert X.remedy("python_unproven:acl:/Library") is None


# --- acl_problem: one ls call ------------------------------------------------------------------------------------

@pytest.mark.skipif(not DARWIN, reason="chmod +a is Darwin's")
def test_an_acl_on_any_component_refuses(tmp_path):
    real = _python()
    f = tmp_path / "acl_file"
    f.write_text("x")
    subprocess.run(["/bin/chmod", "+a", "staff allow write", str(f)], check=True)
    ops = [*X._chain(real), os.path.realpath(f)]
    assert X.acl_problem(list(reversed(ops)), host="darwin") == f"python_unproven:acl:{os.path.realpath(f)}"
    assert X.acl_problem(X._chain(real), host="darwin") is None
    assert X.acl_problem(["/", str(tmp_path / "absent")], host="darwin") == "python_unproven:acl:unreadable"
    assert X.acl_problem(["/a\nb"], host="darwin") == "python_unproven:acl:unreadable"


def _ls(monkeypatch, answers):
    calls = []

    class P:
        stdin = stdout = stderr = None

        def __init__(self, argv, **kw):
            calls.append(argv)
            self.answer = answers.pop(0)
            self.returncode = None

        def communicate(self, input=None, timeout=None):
            if isinstance(self.answer, BaseException):
                raise self.answer
            self.returncode = self.answer[0]
            return self.answer[1].encode(), b""

        def kill(self):
            pass

        def wait(self, timeout=None):
            return self.returncode
    fake = types.SimpleNamespace(Popen=P, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                                 TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess)
    monkeypatch.setattr(X, "subprocess", fake)
    monkeypatch.setattr(X, "_anchor", lambda p: None)
    return calls


def test_acl_ls_timeout_is_retried_once(monkeypatch):
    t = subprocess.TimeoutExpired(["ls"], 5)
    good = "drwxr-xr-x 1 root wheel 1 Jan 1 00:00 /\n-rwxr-xr-x 1 root wheel 1 Jan 1 00:00 /x\n"
    calls = _ls(monkeypatch, [t, (0, good)])
    assert X.acl_problem(["/x", "/"], host="darwin") is None and len(calls) == 2
    assert calls[0][:3] == ["/bin/ls", "-lde", "--"] and calls[0][3:] == ["/", "/x"]
    calls = _ls(monkeypatch, [t, t])
    assert X.acl_problem(["/x", "/"], host="darwin") == "python_unproven:acl:timeout" and len(calls) == 2


def test_acl_ls_answers_this_rule_cannot_read_refuse(monkeypatch):
    one = "drwxr-xr-x 1 root wheel 1 Jan 1 00:00 /\n"
    for out in (one, one + one + "-rw 1 r w 1 J 1 0 /y\n", "garbage\n" + one,
                "drwxr-xr-x 1 root wheel 1 Jan 1 00:00 /x\n" + one):
        _ls(monkeypatch, [(0, out)])
        assert X.acl_problem(["/", "/x"], host="darwin") == "python_unproven:acl:unreadable", out
    _ls(monkeypatch, [(1, one)])
    assert X.acl_problem(["/"], host="darwin") == "python_unproven:acl:unreadable"
    _ls(monkeypatch, [(0, one + " 0: group:staff allow write\n")])
    assert X.acl_problem(["/"], host="darwin") == "python_unproven:acl:/"
    _ls(monkeypatch, [(0, "drwxr-xr-x+ 1 root root 1 Jan 1 00:00 /\n")])
    assert X.acl_problem(["/"], host="linux") == "python_unproven:acl:/"


# --- root's interpreter -------------------------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="root's interpreter is /usr/bin/python3")
def test_the_module_imports_under_roots_interpreter():
    out = subprocess.run(["/usr/bin/python3", "-I", "-c",
                          "import sys; sys.path.insert(0, sys.argv[1]); import levain.autonomic.hands_exec as X; "
                          "import levain.firing.hands as H; assert H.HANDS_USER_RE is X.HANDS_USER_RE; "
                          "print(sys.version_info[:2])", str(ROOT)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    print(f"  /usr/bin/python3 {out.stdout.strip()}: imported")


def test_the_executor_is_a_protocol_executor():
    from levain.autonomic.executor import Executor

    assert isinstance(X.HandsExecutor(entity_dir=Path("/x"), user="_levain_x_abcdef"), Executor)
    assert sys.modules["levain.autonomic.hands_exec"] is X


# --- the plan's two hands probes (stood in: there is no hands user on this Mac) --------------------------------

@pytest.mark.parametrize("prop, interp, want", [
    ((0, ""), (0, ""), "plan"),
    ((1, "violated chain:/usr\n"), None, "hands_user_unproven:property:chain:/usr"),
    ((1, "violated home\n"), None, "hands_user_unproven:property:home"),
    ((1, ""), None, "sudo_refused"),                       # sudo -n's own refusal also exits 1, with no such line
    ((2, ""), None, "sudo_refused"),
    ("timeout", None, "sudo_refused"),
    ("oserror", None, "sudo_refused"),
    ((0, ""), (3, ""), "python_unproven:probe_exit_3"),
    ((0, ""), (1, ""), "python_unproven:probe_exit_1"),
    ((0, ""), "timeout", "python_unproven:probe_timeout"),
])
def test_the_plan_reads_each_probe_answer(tmp_path, monkeypatch, prop, interp, want):
    ex = _executor(tmp_path)
    static = X._Static(user=ex.user, uid=450, home="/var/empty", host="darwin", workspace=tmp_path / "ws",
                       python="/opt/py/bin/python3.9", python_rule="strict")
    monkeypatch.setattr(X.HandsExecutor, "_static", lambda self: static)
    calls = []

    def call(argv, **kw):
        calls.append(argv)
        a = prop if len(calls) == 1 else interp
        if a == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if a == "oserror":
            raise OSError(errno.ENOENT, "no sudo")
        return subprocess.CompletedProcess(argv, a[0], a[1].encode(), b"")
    monkeypatch.setattr(X, "_call", call)
    monkeypatch.setattr(X, "sweep_hands_user", lambda user, **kw: None)
    got = ex._plan()
    if want == "plan":
        assert isinstance(got, X.HandsPlan) and got.uid == 450 and got.python == static.python
        assert got.prefix == (X.SUDO, "-n", "-u", ex.user, "/usr/bin/env", "-i", "HOME=/var/empty",
                              "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "TERM=dumb", f"USER={ex.user}",
                              f"LOGNAME={ex.user}", static.python)
    else:
        assert got == want
    p = calls[0]
    assert p[:7] == [X.SUDO, "-n", "-u", ex.user, "/usr/bin/env", "-i", "PATH=/usr/bin:/bin"]
    assert p[7:10] == ["/bin/sh", "-c", X._PROPERTY_SH] and p[10] == "levain-property"
    assert p[12:15] == [str(tmp_path / "ws"), str(X.WORKSPACE_ROOT["darwin"]), "--"]
    assert p[15:] == X._chain(static.python)
    if len(calls) > 1:
        assert calls[1][-4:-1] == ["-I", "-S", "-c"] and "os.geteuid() == 450" in calls[1][-1]


@pytest.mark.parametrize("which, want", [(1, "sudo_refused"), (2, "python_unproven:probe_timeout")])
def test_a_probe_timeout_sweeps_the_hands_user_before_answering(tmp_path, monkeypatch, which, want):
    # B1-8: a probe's timeout stops sudo at most; what sudo started as the hands user outlived it, and every later
    # reservation answered hands_busy:live with nothing to stop it
    ex = _executor(tmp_path)
    static = X._Static(user=ex.user, uid=450, home="/var/empty", host="darwin", workspace=tmp_path / "ws",
                       python="/opt/py/bin/python3.9", python_rule="strict")
    monkeypatch.setattr(X.HandsExecutor, "_static", lambda self: static)
    events: list = []

    def call(argv, **kw):
        events.append("call")
        if len(events) == which:
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(X, "_call", call)
    monkeypatch.setattr(X, "sweep_hands_user", lambda user, **kw: events.append(("sweep", user, kw.get("uid"))))
    assert ex._plan() == want
    assert events == ["call"] * which + [("sweep", ex.user, 450)]
    # a sweep that raises still answers the probe's clause (the answer is a refusal either way)
    events.clear()
    monkeypatch.setattr(X, "sweep_hands_user", lambda user, **kw: events.append("sweep") or 1 / 0)
    assert ex._plan() == want and events[-1] == "sweep"


def test_call_never_waits_without_bound_on_a_child_it_cannot_kill(monkeypatch):
    # with B1-8: subprocess.run's timeout path kills the child, then waits for it WITHOUT a bound. A probe's child
    # is sudo (root): kill() from the operator uid can raise PermissionError, and the wait then lasts as long as
    # the hands-user process does, so the sweep after the timeout is never reached
    victims: list = []
    real_kill = subprocess.Popen.kill

    def refused(self):
        victims.append(self)
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(subprocess.Popen, "kill", refused)
    monkeypatch.setattr(X, "_REAP_TIMEOUT", 0.2, raising=False)
    import time
    t0 = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            X._call(["/bin/sleep", "4"], env={"PATH": "/usr/bin:/bin"}, timeout=0.3)
        assert time.monotonic() - t0 < 2.0
    finally:
        for p in victims:
            real_kill(p)
            p.wait()


def test_a_plan_that_raises_is_not_ready_and_releases_the_lock(tmp_path, monkeypatch):
    ex = _executor(tmp_path)
    monkeypatch.setattr(X.HandsExecutor, "_plan", lambda self: 1 / 0)
    with X.hands_reserved(ex) as r:
        assert (r.ready, r.reason) == (False, "hands_unready:plan:ZeroDivisionError")
        fd = take_exclusive(ex.entity_dir)
        assert fd is not None
        os.close(fd)
