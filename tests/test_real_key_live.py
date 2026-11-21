"""The real terminal key is reached only by a process a levain launcher marked live (board levain#237).

Before: ``keys.pwd_key_dir()`` reads the password database's home, so a bare child python (no pytest, so neither of the
in-process walls) calling ``terminal_sign.sign(msg)`` with no ``key_dir`` found the operator's key, spawned a real
``ssh-keygen -Y sign`` and wrote evidence beside it. Now ``refuse_real_key_unless_live`` stops it first.

NOTHING here touches the operator's key or ``~/.levain``. Each child runs with a scratch HOME and LEVAIN_HOME and an
explicit environment, and before it imports levain it points the password database's home at a directory under
``tmp_path`` (``pwd.getpwuid``), so "the real key directory" it resolves is a stand-in holding a scratch ed25519 key.
A stub ssh-keygen under ``tmp_path`` is first on the child's PATH, and the child records every spawn in a marker file
(``subprocess.run`` wrapped in the child), so a spawn is visible whichever way the binary is named. Whatever
runs, it runs on the stand-in's scratch key, so even the old code, which these tests fail on, never reaches the
operator's key.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain import home_guard
from levain.cockpit import keys as K
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import terminal_sign as ts

LEVAIN_ROOT = Path(levain.__file__).resolve().parent.parent      # the tree the test run imported, which the child imports
#: The module's own default-directory function, taken at import, before the conftest's autouse fixture replaces it for
#: each test; a test that calls it replaces ``K.pwd_key_dir`` first, so it never names the operator's directory.
_REAL_KEY_DIR = ts._real_key_dir

CHILD = '''
import pwd
import sys
from pathlib import Path

account, marker, mode = sys.argv[1], sys.argv[2], sys.argv[3]
_real = pwd.getpwuid


def _getpwuid(uid):
    e = _real(uid)
    return pwd.struct_passwd((e.pw_name, e.pw_passwd, e.pw_uid, e.pw_gid, e.pw_gecos, account, e.pw_shell))


pwd.getpwuid = _getpwuid        # the account's home is the test's stand-in, so nothing here can be the operator's
from levain.cockpit import keys as K
from levain.consent import terminal_sign as ts

import subprocess
_run = subprocess.run


def _recording_run(argv, *a, **kw):
    """Every spawn is written to the marker first, then runs the system ssh-keygen on the stand-in's scratch key. A stub
    first on PATH sees only a spawn by bare name; this sees a spawn by any path."""
    with open(marker, "a") as fh:
        fh.write(" ".join(map(str, argv)) + "\\n")
    return _run(argv, *a, **kw)


subprocess.run = _recording_run
if mode.startswith("mark-"):        # the child's own launcher: mark_live() first thing, then the act
    from levain.home_guard import mark_live
    mark_live()
    mode = mode[len("mark-"):]
if mode == "spawn-sign":            # a child of this process started without env=: it inherits both markers
    done = _run([sys.executable, sys.argv[0], account, marker, "sign"], capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    sys.exit(done.returncode)
if mode == "fork-sign":             # a fork of this process: the same environment, another pid
    import os
    pid = os.fork()
    if pid == 0:
        out = ts.sign(b"x")
        print(out.state, out.reason, flush=True)
        sys.stderr.write(out.detail + "\\n")
        sys.stderr.flush()
        os._exit(0 if out.state == "signed" else 3)
    sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
if mode == "signer":
    K.ssh_signer(Path(account) / ".levain" / "keys" / ts.KEY_LABEL)
    sys.exit(0)
if mode == "factory":
    ts.default_signer_factory(Path(account) / ".levain" / "keys" / ts.KEY_LABEL, 5.0)
    sys.exit(0)
if mode == "sign-failure":
    K._write_sign_failure(Path(account) / ".levain" / "keys" / ts.KEY_LABEL, fp="SHA256:x", message=b"x", signature="",
                          sig_type="x", flags=0, counter=1, detail="d", namespace="n", attempt=1, elapsed=0.0)
    sys.exit(0)
if mode == "create-real":           # terminal-key create with the real key directory named explicitly
    made = []
    rep = ts.create(key_dir=Path(account) / ".levain" / "keys", creator=lambda d, label: made.append(str(d)),
                    in_agent_session=lambda: False)
    print("made" if made else "not-made")
    sys.stderr.write(rep.text + "\\n")
    sys.exit(0)
out = ts.sign(b"x", key_dir=Path(sys.argv[4])) if mode == "sign-keydir" else ts.sign(b"x")
print(out.state, out.reason)
if out.state != "signed":
    sys.stderr.write(out.detail + "\\n")
    sys.exit(3)
'''


def _scratch_key(d: Path) -> Path:
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    d.chmod(0o700)
    path = d / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "scratch", "-f", str(path)], check=True)
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    return path


class Rig:
    """A stand-in account under ``tmp_path``: its ``.levain/keys`` holds a scratch key, ``marker`` records every spawn the
    child makes (and every call of the stub on PATH), and ``run`` starts a bare child python with an explicit environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.account = tmp_path / "acct"
        self.keys = self.account / ".levain" / "keys"
        self.key = _scratch_key(self.keys)
        self.marker = tmp_path / "stub-called"
        self.stub = tmp_path / "bin" / "ssh-keygen"
        self.stub.parent.mkdir()
        self.stub.write_text(f'#!/bin/sh\necho "$@" >> {self.marker}\nexec /usr/bin/ssh-keygen "$@"\n')
        self.stub.chmod(0o755)
        self.script = tmp_path / "bare_child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.home = tmp_path / "scratch-home"
        self.home.mkdir()

    def run(self, mode: str, *, live: bool = False, scratch: bool = False, extra: str = ""):
        env = {"PATH": f"{self.stub.parent}:/usr/bin:/bin", "HOME": str(self.home),
               "LEVAIN_HOME": str(self.home / "lh"), "PYTHONPATH": str(LEVAIN_ROOT)}
        assert "PYTEST_CURRENT_TEST" not in env
        if live:                    # the marker set by hand, as a parent's environment hands it down: no pid of its own
            env[home_guard.LIVE_ENV] = "1"
        # named either way: without it the keyguard marks every child of the test run a scratch run
        # (tests/spawn_keyguard.py scratch_env); these children are production runs on purpose unless ``scratch``
        env[home_guard.SCRATCH_ENV] = "1" if scratch else "0"
        argv = [sys.executable, str(self.script), str(self.account), str(self.marker), mode] + ([extra] if extra else [])
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, cwd=str(self.tmp))

    def failures(self) -> Path:
        return self.keys / "sign-failures"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- (a) the named test: an unmarked bare child is refused before anything is spawned or written ------------------------

def test_a_bare_child_not_marked_live_cannot_sign_with_the_real_terminal_key(rig):
    done = rig.run("sign")
    assert done.returncode != 0, done.stdout
    assert "RealKeyUnmarked" not in done.stdout
    assert "operator's real key" in done.stderr and "mark_live" in done.stderr, done.stderr   # the refusal names its fix
    assert "the real key directory" in done.stderr       # refused where the default key directory is picked, before any look at it
    assert str(rig.keys) in done.stderr
    assert not rig.marker.exists(), "an ssh-keygen was spawned"
    assert not rig.failures().exists()


def test_the_refusal_is_a_traceback_that_names_the_cause_from_ssh_signer(rig):
    done = rig.run("signer")
    assert done.returncode != 0
    assert "RealKeyUnmarked" in done.stderr and "mark_live" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


def test_the_signer_factory_door_refuses_too(rig):
    done = rig.run("factory")
    assert done.returncode != 0 and "RealKeyUnmarked" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


def test_an_unmarked_child_cannot_write_into_the_real_sign_failures_directory(rig):
    done = rig.run("sign-failure")
    assert done.returncode != 0 and "RealKeyUnmarked" in done.stderr, done.stderr
    assert not rig.failures().exists()


# --- (b) marked live: the opt-in works --------------------------------------------------------------------------------

def test_a_child_marked_live_signs_with_a_key_dir_under_tmp_through_the_stub(rig, tmp_path):
    other = _scratch_key(tmp_path / "other-keys")
    done = rig.run("mark-sign-keydir", extra=str(other.parent))
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["signed", "None"]
    assert rig.marker.exists() and "-Y sign" in rig.marker.read_text()


def test_a_child_marked_live_signs_with_the_real_key_directory_it_finds_by_default(rig):
    done = rig.run("mark-sign")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["signed", "None"]
    assert rig.marker.exists() and "-Y sign" in rig.marker.read_text()


# --- the mark is this process's own: an inherited LEVAIN_LIVE does not open the key (levain#237 L2 MED 2) -------------

def test_a_child_holding_only_an_inherited_live_marker_cannot_sign(rig):
    """``LEVAIN_LIVE=1`` alone, as a marked parent's spawn without ``env=`` hands it down: refused, nothing spawned."""
    done = rig.run("sign", live=True)
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert done.stdout.split() == ["unsigned", "signer-error"]
    assert "did not mark itself live" in done.stderr and "LEVAIN_LIVE_PID" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


@pytest.mark.parametrize("mode", ["mark-spawn-sign", "mark-fork-sign"])
def test_a_child_or_a_fork_of_a_marked_process_cannot_sign_until_it_marks_itself(rig, mode):
    """The parent called ``mark_live()``; its child (started without ``env=``: both markers inherited) or its fork (the
    same environment, another pid) signs nothing and spawns no ``ssh-keygen -Y sign``."""
    done = rig.run(mode)
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert done.stdout.split() == ["unsigned", "signer-error"]
    assert "did not mark itself live" in done.stderr, done.stderr
    assert not rig.marker.exists() or "-Y sign" not in rig.marker.read_text()
    assert not rig.failures().exists()


def test_an_unmarked_child_with_a_key_dir_outside_the_real_one_still_signs(rig, tmp_path):
    other = _scratch_key(tmp_path / "other-keys")
    done = rig.run("sign-keydir", extra=str(other.parent))
    assert done.returncode == 0, done.stderr
    assert rig.marker.exists()


# --- (c) the evidence writer, in process ------------------------------------------------------------------------------

def _write(key_path: Path) -> str:
    return K._write_sign_failure(key_path, fp="SHA256:x", message=b"x", signature="", sig_type="x", flags=0, counter=1,
                                 detail="d", namespace="n", attempt=1, elapsed=0.0)


def test_write_sign_failure_refuses_the_real_key_dir_when_unmarked_and_writes_elsewhere(tmp_path, monkeypatch):
    real = tmp_path / "acct" / ".levain" / "keys"
    real.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.delenv(home_guard.LIVE_ENV, raising=False)
    with pytest.raises(K.RealKeyUnmarked, match="mark_live"):
        _write(real / ts.KEY_LABEL)
    assert not (real / "sign-failures").exists()
    other = tmp_path / "other-keys"
    other.mkdir(mode=0o700)
    written = _write(other / ts.KEY_LABEL)
    assert Path(written).parent == other / "sign-failures" and Path(written).is_file()


def test_write_sign_failure_in_the_real_key_dir_is_allowed_to_a_child_marked_live(rig):
    done = rig.run("mark-sign-failure")
    assert done.returncode == 0, done.stderr
    assert len(list(rig.failures().glob("sigfail-*.json"))) == 1


# --- a test run or a scratch run never reaches the real key, marked or not --------------------------------------------

def test_a_scratch_run_that_calls_mark_live_is_not_marked_and_cannot_sign(rig):
    done = rig.run("mark-sign", scratch=True)
    assert done.returncode != 0, done.stdout
    assert done.stdout.split() == ["unsigned", "signer-error"]     # the outcome flow's hook sees, from a scratch run
    assert "scratch run" in done.stderr and "operator's real key" in done.stderr, done.stderr
    assert not rig.marker.exists(), "an ssh-keygen was spawned"
    assert not rig.failures().exists()


def test_a_scratch_run_with_the_marker_set_directly_is_refused(rig):
    done = rig.run("sign", live=True, scratch=True)
    assert done.returncode != 0, done.stdout
    assert done.stdout.split() == ["unsigned", "signer-error"]
    assert "scratch run" in done.stderr and "operator's real key" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


def test_a_scratch_run_with_the_marker_set_directly_cannot_write_the_real_sign_failures_directory(rig):
    done = rig.run("sign-failure", live=True, scratch=True)
    assert done.returncode != 0 and "RealKeyUnmarked" in done.stderr, done.stderr
    assert not rig.failures().exists()


def test_a_scratch_run_with_a_key_dir_outside_the_real_one_still_signs(rig, tmp_path):
    other = _scratch_key(tmp_path / "other-keys")
    done = rig.run("sign-keydir", scratch=True, extra=str(other.parent))
    assert done.returncode == 0, done.stderr
    assert rig.marker.exists()


def test_a_normal_child_that_calls_mark_live_signs_through_the_recorded_spawn(rig):
    done = rig.run("mark-sign")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["signed", "None"]
    assert rig.marker.exists() and "-Y sign" in rig.marker.read_text()


def test_mark_live_is_a_no_op_in_a_scratch_run_and_marks_otherwise(rig, tmp_path):
    script = tmp_path / "mark_probe.py"
    script.write_text("import os\nfrom levain.home_guard import LIVE_ENV, LIVE_PID_ENV, live_marked_here, mark_live\n"
                      "mark_live()\nprint(os.environ.get(LIVE_ENV), os.environ.get(LIVE_PID_ENV) == str(os.getpid()), "
                      "live_marked_here())\n")
    base = {"PATH": "/usr/bin:/bin", "HOME": str(rig.home), "LEVAIN_HOME": str(rig.home / "lh"), "PYTHONPATH": str(LEVAIN_ROOT)}
    for extra, want in (({home_guard.SCRATCH_ENV: "0"}, "1 True True"), ({home_guard.SCRATCH_ENV: "1"}, "None False False"),
                        ({}, "None False False")):      # no marker named: the keyguard makes it a scratch run
        done = subprocess.run([sys.executable, str(script)], env={**base, **extra}, capture_output=True, text=True,
                              timeout=120, cwd=str(tmp_path))
        assert done.returncode == 0 and done.stdout.strip() == want, (extra, done.stdout, done.stderr)


def test_refuse_real_key_unless_live_judges_the_resolved_path(tmp_path, monkeypatch):
    real = tmp_path / "acct" / ".levain" / "keys"
    real.mkdir(parents=True, mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.delenv(home_guard.LIVE_ENV, raising=False)
    for path in (real, real / ts.KEY_LABEL, link / ts.KEY_LABEL, real / ".." / "keys" / ts.KEY_LABEL):
        with pytest.raises(K.RealKeyUnmarked):
            K.refuse_real_key_unless_live(path)
    K.refuse_real_key_unless_live(tmp_path / "elsewhere" / ts.KEY_LABEL)
    K.refuse_real_key_unless_live(real.parent / "keys-sibling" / ts.KEY_LABEL)
    monkeypatch.setenv(home_guard.LIVE_ENV, "1")        # this process is a test run: the marker does not open the key
    with pytest.raises(K.RealKeyUnmarked, match="test run"):
        K.refuse_real_key_unless_live(real / ts.KEY_LABEL)


def test_the_refusal_is_a_real_key_reached_and_a_key_error():
    assert issubclass(K.RealKeyUnmarked, K.RealKeyReached) and issubclass(K.RealKeyUnmarked, K.KeyError_)


# --- levain#237 review round 1 ----------------------------------------------------------------------------------------
# In process (a test run, so the gate refuses inside the real directory whatever the markers say); "the real key
# directory" is a stand-in under tmp_path (``pwd_key_dir`` replaced), never the operator's.

@pytest.fixture
def real(tmp_path, monkeypatch):
    r = tmp_path / "acct" / ".levain" / "keys"
    r.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: r)
    return r


def test_the_gate_judges_a_case_different_spelling_by_identity(real, tmp_path):
    """A case-different spelling (on a volume that folds case) is the real key directory (L1 F1). Skipped where the
    volume does not fold case: there no such spelling names the directory."""
    (real / ts.KEY_LABEL).write_text("stub, not a key")
    folded = tmp_path / "ACCT" / ".LEVAIN" / "KEYS"
    if not folded.exists():
        pytest.skip("a case-sensitive volume: no case-different spelling of the directory exists")
    assert str(folded.resolve()) != str(real.resolve())     # resolve() does not fold case: the spelling check alone passes it
    for path in (folded, folded / ts.KEY_LABEL, folded / "sign-failures"):     # the evidence directory, not made yet
        with pytest.raises(K.RealKeyUnmarked, match="real key directory"):
            K.refuse_real_key_unless_live(path)


def test_the_gate_judges_identity_not_spelling(real, tmp_path):
    """A link-made spelling (a firmlink's shape, made with a symlink here) and a hard link of the key file are the real
    key; a COPY is another file and is out of scope (L1 F1, L2 MED 1). Runs on any volume (L3 r1: the case-folded
    check, which needs a folding volume, is its own test above)."""
    import os

    key = real / ts.KEY_LABEL
    key.write_text("stub, not a key")
    firm = tmp_path / "firm"
    firm.symlink_to(tmp_path)
    with pytest.raises(K.RealKeyUnmarked, match="real key directory"):
        K.refuse_real_key_unless_live(firm / "acct" / ".levain" / "keys" / ts.KEY_LABEL)
    other = tmp_path / "other"
    other.mkdir()
    hard = other / "renamed"
    os.link(key, hard)
    with pytest.raises(K.RealKeyUnmarked):
        K.refuse_real_key_unless_live(hard)
    copy = other / "copy"
    copy.write_bytes(key.read_bytes())
    assert K.refuse_real_key_unless_live(copy) == copy.resolve()


def test_the_gate_fails_closed_when_the_real_key_directory_cannot_be_named(monkeypatch, tmp_path):
    """The same direction as the in-test guard (L1 F3): no password entry, so nothing can be shown to lie elsewhere."""
    def unnamed():
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(K, "pwd_key_dir", unnamed)
    with pytest.raises(K.RealKeyUnmarked, match="cannot be named"):
        K.refuse_real_key_unless_live(tmp_path / "elsewhere" / ts.KEY_LABEL)


def test_the_gate_returns_the_path_it_judged_and_sign_hands_that_to_the_signer(real, tmp_path):
    """L1 F2 (TOCTOU): what is spawned is the path the gate judged, never the unresolved one, so a link retargeted
    after the check is not followed."""
    elsewhere = tmp_path / "elsewhere-keys"
    elsewhere.mkdir(mode=0o700)
    link = tmp_path / "keys-link"
    link.symlink_to(elsewhere)
    judged = (elsewhere / ts.KEY_LABEL).resolve()
    assert K.refuse_real_key_unless_live(link / ts.KEY_LABEL) == judged
    seen = []

    def factory(path, timeout=None):
        seen.append(path)
        return "SHA256:x", lambda message: "sig"

    out = ts.sign(b"x", key=ts.TerminalKey(link / ts.KEY_LABEL, "pub"), signer_factory=factory)
    assert out.state == "signed" and seen == [judged], (out, seen)


def test_through_terminal_sign_the_refusal_is_an_unsigned_signer_error_outcome(real, monkeypatch):
    """What flow's hook sees (L1 F6, Q5): ``sign`` never raises; the refusal is ``unsigned``/``signer-error`` with the
    gate's words, and no signer was made. The in-test allowlist would refuse the stand-in first, so it is stepped
    aside here (outside a test run it returns the path unchanged): the refusal is this gate's."""
    monkeypatch.setattr(K, "refuse_real_key_in_tests", lambda path, what="the key": None if path is None else Path(path))
    seen = []

    def factory(path, timeout=None):
        seen.append(path)
        return "SHA256:x", lambda message: "sig"

    out = ts.sign(b"x", key=ts.TerminalKey(real / ts.KEY_LABEL, "pub"), signer_factory=factory)
    assert (out.state, out.reason) == ("unsigned", "signer-error"), out
    assert "real key directory" in out.detail and "test run" in out.detail, out.detail
    assert seen == []


def test_live_marked_here_needs_this_processes_own_pid(monkeypatch):
    import os

    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    monkeypatch.setenv(home_guard.LIVE_PID_ENV, str(os.getpid()))
    assert home_guard.live_marked_here()
    monkeypatch.setenv(home_guard.LIVE_PID_ENV, str(os.getpid() + 1))      # a parent's pid, inherited
    assert not home_guard.live_marked_here() and home_guard.live_marked()  # the home resolvers' mark is unchanged
    monkeypatch.delenv(home_guard.LIVE_PID_ENV)
    assert not home_guard.live_marked_here()
    monkeypatch.setenv(home_guard.LIVE_PID_ENV, str(os.getpid()))
    monkeypatch.delenv(home_guard.LIVE_ENV)
    assert not home_guard.live_marked_here()


def test_the_real_key_file_names_are_the_terminal_keys_label():
    assert K._REAL_KEY_FILES == (ts.KEY_LABEL,)


# --- a REAL levain entry point a test starts, with a hand-built environment (levain#237 L2 HIGH) ----------------------

BAND_CHILD = '''
import os
import pwd
import runpy
import subprocess
import sys

account, marker = sys.argv[1], sys.argv[2]
del sys.argv[1:3]                   # what is left is the entry's own argv: the event
_real = pwd.getpwuid


def _getpwuid(uid):
    e = _real(uid)
    return pwd.struct_passwd((e.pw_name, e.pw_passwd, e.pw_uid, e.pw_gid, e.pw_gecos, account, e.pw_shell))


pwd.getpwuid = _getpwuid            # before levain loads: the account's home is the stand-in, never the operator's
from levain.cockpit import keys as K
assert str(K.pwd_key_dir()).startswith(account), K.pwd_key_dir()
_init = subprocess.Popen.__init__


def _recording_init(self, args, *a, **kw):
    """Every spawn is written to the marker first; a notice is never shown (osascript runs /usr/bin/true)."""
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    with open(marker, "a") as fh:
        fh.write(" ".join(map(str, argv)) + "\\n")
    if os.path.basename(str(argv[0])) == "osascript":
        args = ["/usr/bin/true"]
    return _init(self, args, *a, **kw)


subprocess.Popen.__init__ = _recording_init
runpy.run_module("levain.band_hook", run_name="__main__", alter_sys=True)     # the module's own __main__ block
'''


def test_a_real_band_hook_a_test_starts_with_its_own_environment_cannot_sign_with_the_real_key(tmp_path):
    """``python -m levain.band_hook UserPromptSubmit`` (its ``__main__`` block, which calls ``mark_live()``), started as
    tests/test_band_hooks_user.py starts it: an explicit environment that names nothing of a test. An answer typed to an
    open band row reaches the terminal sign with no ``key_dir``, so the default key directory, "the real key", is a
    stand-in under tmp_path holding a scratch key. Before the keyguard marked every child a scratch run the child
    marked itself live and SIGNED (the stand-in's scratch key); now the answer is recorded unsigned/signer-error and
    no ``ssh-keygen -Y sign`` is spawned."""
    import sqlite3

    from levain.consent.questions import QuestionStore

    account = tmp_path / "acct"
    keys = account / ".levain" / "keys"
    _scratch_key(keys)
    lv = tmp_path / "lv"
    qdir = lv / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    qdir.chmod(0o700)
    QuestionStore(qdir).file(agent="claude_code", agent_session_id="other-seat", what="Which retry policy?",
                             options=[("a", "yes"), ("b", "no")], rec="a", blocks="x", cwd=str(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "spawns"
    script = tmp_path / "band_child.py"
    script.write_text(BAND_CHILD, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LEVAIN_HOME": str(lv), "PYTHONPATH": str(LEVAIN_ROOT)}
    assert home_guard.SCRATCH_ENV not in env and "PYTEST_CURRENT_TEST" not in env     # nothing here says "test"

    def prompt(text: str) -> subprocess.CompletedProcess:
        payload = json.dumps({"session_id": "sess-237", "cwd": str(work), "prompt": text})
        return subprocess.run([sys.executable, str(script), str(account), str(marker), "UserPromptSubmit"], input=payload,
                              env=env, capture_output=True, text=True, timeout=120, cwd=str(work))

    shown = prompt("hello")
    assert shown.returncode == 0 and "Which retry policy?" in shown.stdout, (shown.stdout, shown.stderr)
    answered = prompt("1a")
    assert answered.returncode == 0, answered.stderr
    with sqlite3.connect(qdir / "questions.db") as c:
        rows = c.execute("SELECT x.sign_state, x.sign_reason FROM answer_context x").fetchall()
    assert rows == [("unsigned", "signer-error")], (rows, answered.stdout, answered.stderr)
    assert not marker.exists() or "-Y sign" not in marker.read_text(), marker.read_text()
    assert not (keys / "sign-failures").exists()


# --- levain#237 L3 round 1 --------------------------------------------------------------------------------------------

def _clear_keys(d: Path) -> None:
    """The stand-in's key directory, emptied (scratch files under tmp_path): nothing enrolled, so create would make one."""
    for p in d.iterdir():
        p.unlink()


def test_create_with_the_real_key_directory_named_explicitly_is_refused_before_anything_is_made(real, monkeypatch):
    """L3 r1 F1 (codex, gemini): the gate sat only on the default branch, so ``create(key_dir=<the real dir>)`` reached
    the creator. In process this is a test run, which the gate refuses inside the real directory whatever the markers;
    the in-test allowlist is stepped aside (outside a test run it returns the path unchanged), so the refusal is the
    gate's."""
    monkeypatch.setattr(K, "refuse_real_key_in_tests", lambda path, what="the key": None if path is None else Path(path))
    made = []
    rep = ts.create(key_dir=real, creator=lambda d, label: made.append(d), in_agent_session=lambda: False)
    assert made == [], rep.text
    assert not rep.created and "real key directory" in rep.text and "nothing was created" in rep.text, rep.text


def test_an_unmarked_production_child_naming_the_real_key_directory_cannot_create_a_key(rig):
    """The same in a bare child that is not a test run and not a scratch run, and did not mark itself live."""
    _clear_keys(rig.keys)
    done = rig.run("create-real")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["not-made"], (done.stdout, done.stderr)
    assert "real key directory" in done.stderr and "mark_live" in done.stderr, done.stderr
    assert list(rig.keys.iterdir()) == []


def test_a_child_marked_live_naming_the_real_key_directory_reaches_the_creator(rig):
    _clear_keys(rig.keys)
    done = rig.run("mark-create-real")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["made"], (done.stdout, done.stderr)


def _relocating_gate(given: Path, judged: Path):
    """A stand-in for ``refuse_real_key_unless_live`` that answers every path under ``given`` with the same place under
    ``judged``: whatever is written or returned under ``judged`` came from the gate's answer, never the caller's path."""
    def gate(path, what="the key"):
        p = Path(path)
        try:
            return judged / p.relative_to(given)
        except ValueError:
            return p
    return gate


def test_write_sign_failure_writes_only_under_the_path_the_gate_returned(tmp_path, monkeypatch):
    """L3 r1 F2 (codex, gemini): the writer discarded the gate's answer and rebuilt the path it was given, so a link
    retargeted between the gate and the mkdir was followed. A real retarget between the two is a race; the gate's
    answer is replaced by another directory instead, which shows the write lands where the gate said."""
    given = tmp_path / "given-keys"
    given.mkdir(mode=0o700)
    judged = tmp_path / "judged-keys"
    judged.mkdir(mode=0o700)
    monkeypatch.setattr(K, "refuse_real_key_unless_live", _relocating_gate(given, judged))
    written = _write(given / ts.KEY_LABEL)
    assert Path(written).parent == judged / "sign-failures", written
    assert not (given / "sign-failures").exists()


def test_the_default_key_directory_is_the_path_the_gate_returned(tmp_path, monkeypatch):
    """L3 r1 F2: ``_real_key_dir`` discarded the gate's answer too."""
    real = tmp_path / "acct" / ".levain" / "keys"
    judged = tmp_path / "judged"
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.setattr(K, "refuse_real_key_unless_live", _relocating_gate(real.parent, judged))
    assert _REAL_KEY_DIR() == judged / "keys"


def test_no_password_entry_is_a_key_error_of_levains_family_not_a_bare_key_error(monkeypatch):
    """L3 r1 F3 (deepseek, gemini): ``_real_key_dir`` asked the password database before the gate, so a uid with no
    entry raised a bare ``KeyError`` out of ``find_key``, past every caller that handles ``KeyError_``; ``sign`` turned
    it into a signer-error naming only the bare error."""
    def unnamed():
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(K, "pwd_key_dir", unnamed)
    monkeypatch.setattr(ts, "_real_key_dir", _REAL_KEY_DIR)     # the module's own, not the conftest's stand-in
    with pytest.raises(K.KeyError_, match="cannot be named"):
        ts.find_key()
    out = ts.sign(b"x", signer_factory=lambda path, timeout=None: pytest.fail("a signer was made"))
    assert (out.state, out.reason) == ("unsigned", "signer-error"), out
    assert "cannot be named" in out.detail, out.detail


def test_the_gate_folds_case_for_a_real_key_directory_not_made_yet(tmp_path, monkeypatch):
    """L3 r1 F4 (deepseek, gemini): with the real directory not made yet there is no inode to compare, and the identity
    check returned False, so a case-different spelling passed and could make the directory the key later lands in.
    Fail closed: a case-folded spelling is refused, on any volume."""
    real = tmp_path / "acct" / ".levain" / "keys"
    real.parent.mkdir(parents=True)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    for path in (tmp_path / "ACCT" / ".LEVAIN" / "KEYS", tmp_path / "acct" / ".levain" / "Keys" / "sign-failures"):
        with pytest.raises(K.RealKeyUnmarked, match="real key directory"):
            K.refuse_real_key_unless_live(path)
    assert not real.exists()
    sibling = tmp_path / "acct" / ".levain" / "keys-other" / ts.KEY_LABEL
    assert K.refuse_real_key_unless_live(sibling) == sibling.resolve()
