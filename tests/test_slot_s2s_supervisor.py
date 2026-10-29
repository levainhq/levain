"""S1 session 2e lane S: the interactive supervisor (CONTRACT §1 "interactive supervisor", §3.1, §5.1, §5.4 step 1;
BUILD_PLAN §2c 5, §2d 5).

The runtime helper is a FAKE script written into tmp and run where ``sudo -n <helper>`` would be (the argv prefix is
the :class:`Seams` seam): it logs every verb with its stdin and its own open fds, answers one canonical line, and its
``start`` writes to the pty the supervisor allocated. Git is real, in tmp, with the operator's global config replaced.
The lease is a real socket the fake client holds. What only the VM shows (sudo's ``use_pty``, the cross-uid walls,
TIOCSTI) is row 2g's.
"""
from __future__ import annotations

import faulthandler
import functools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from levain.slot import Refused
from levain.slot import supervisor as SV
from levain.slot.stage import make_bundle


class Hung(BaseException):
    """A test past its bound. BaseException, not TimeoutError: TimeoutError is an OSError, which the supervisor
    catches (``refused os-error``) and would turn a hang into a plain return."""


def bounded(seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """A hang FAILS within ``seconds`` instead of wedging the suite, with the stdlib alone (CI has no pytest-timeout):
    SIGALRM raises :class:`Hung` in the test (the main thread), and faulthandler dumps every thread and exits the
    process 30 s later should the test's own cleanup hang too. POSIX only, as these pty tests are."""
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            def hung(signum: int, frame: Any) -> None:
                raise Hung(f"{fn.__name__} did not finish within {seconds:g} s")
            old = signal.signal(signal.SIGALRM, hung)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            faulthandler.dump_traceback_later(seconds + 30, exit=True)
            try:
                return fn(*args, **kwargs)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                faulthandler.cancel_dump_traceback_later()
                signal.signal(signal.SIGALRM, old)
        return run
    return wrap

SESSION = "s-0123456789abcdef"

FAKE_HELPER = r'''
import json, os, sys, time
fds = []
for fd in range(256):
    try:
        os.fstat(fd)
        fds.append(fd)
    except OSError:
        pass
verb, args = sys.argv[1], sys.argv[2:]
store, log = os.environ["FAKE_STORE"], os.environ["FAKE_LOG"]
data = sys.stdin.buffer.read().decode() if verb in ("prepare", "render") else ""
with open(log, "a") as f:
    f.write(json.dumps({"verb": verb, "args": args, "stdin": data, "fds": fds, "tty": os.isatty(0),
                        "null0": os.path.samestat(os.fstat(0), os.stat(os.devnull))}) + "\n")
def line(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
refuse = os.environ.get("FAKE_REFUSE", "")
if refuse.split("=")[0] == verb:
    sys.stderr.write("levain runtime-helper: refused it: detail \x1b]52;c;eA==\x07\n")
    line({"ok": False, "refused": refuse.split("=")[1]})
    sys.exit(1)
sess = "%(session)s"
if verb == "prepare":
    staging = os.path.join(store, "staging", sess)
    os.makedirs(staging)
    line({"format": "levain-prepare-result/1", "gwport": 47100, "ok": True, "session": sess,
          "slot": {"class": "egress", "index": 0}, "staging": staging})
elif verb == "render":
    assert os.path.getsize(os.path.join(store, "staging", sess, "repo.bundle")) > 16
    line({"ok": True})
elif verb == "start":
    os.write(1, ("fds=%%s\n" %% fds).encode())
    os.write(1, b"\x1b]2;slot title\x07hello \x1b]52;c;c2VjcmV0\x07\x1b[6n\xe2\x98")
    os.write(1, b"\x95 done\n")
    mode = os.environ.get("FAKE_START")
    if mode in ("paste", "stream"):
        import select, tty
        tty.setraw(0)
        open(os.path.join(store, "raw"), "w").close()   # typed-ahead input before this sits in the cooked line
    if mode == "paste":
        for _ in range(512):
            os.write(1, b"o" * 4096)                # 2 MiB before it reads a byte of input
        need, got = int(os.environ["FAKE_PASTE"]), 0
        while got < need:
            got += len(os.read(0, 65536))
        os.write(1, b"got=%%d\n" %% got)
        line({"exit": 0, "ok": True})
    elif mode == "stream":
        buf = b""
        while b"q" not in buf:                      # output never stops until the operator's key arrives
            os.write(1, b"x" * 1024)
            if select.select([0], [], [], 0)[0]:
                buf += os.read(0, 64)
        os.write(1, b"quit seen\n")
        line({"exit": 0, "ok": True})
    elif mode == "wait":
        open(os.path.join(store, "started"), "w").close()
        while not os.path.exists(os.path.join(store, "torn")):
            time.sleep(0.02)
        os.write(1, b"killed\n")
        line({"exit": 143, "ok": True})
    else:
        os.write(1, b"bye no newline ")
        line({"exit": 3, "ok": True})
elif verb == "probe":
    # FAKE_PROBES: one outcome per probe call, in order (the last repeats): closed, malformed, raw (the line
    # FAKE_PROBE_LINE), slow:<s> (then closed), hang, or a refusal name.
    seq = os.environ.get("FAKE_PROBES", "closed").split(",")
    count = os.path.join(store, "probes")
    n = int(open(count).read()) if os.path.exists(count) else 0
    with open(count, "w") as f:
        f.write(str(n + 1))
    what = seq[min(n, len(seq) - 1)]
    if what.startswith("slow:"):
        open(os.path.join(store, "probing"), "w").close()
        time.sleep(float(what[5:]))
        os.remove(os.path.join(store, "probing"))
        what = "closed"
    if what == "hang":
        with open(os.path.join(store, "pid-probe"), "w") as f:
            f.write(str(os.getpid()))
        open(os.path.join(store, "probing"), "w").close()
        while True:
            time.sleep(0.05)
    if what == "closed":
        line({"address": "192.0.2.10", "addresses": 2, "host": "api.anthropic.com", "ok": True, "probe": "closed"})
    elif what == "malformed":
        line({"ok": True, "probe": "open"})
    elif what == "raw":
        sys.stdout.write(os.environ["FAKE_PROBE_LINE"] + "\n")
    else:
        sys.stderr.write("levain runtime-helper: refused %%s: api.anthropic.com 192.0.2.10\n" %% what)
        line({"ok": False, "refused": what})
        sys.exit(1)
elif verb == "teardown":
    open(os.path.join(store, "torn"), "w").close()
    line({"ok": True, "state": os.environ.get("FAKE_END", "RETAINED")})
''' % {"session": SESSION}


# --- fixtures ----------------------------------------------------------------------------------------------------------

def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Op Erator\n\temail = op@example.com\n[init]\n\tdefaultBranch = main\n"
                   "[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def make_repo(tmp_path: Path, settings: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "README.md").write_text("tracked\n")
    (repo / ".gitignore").write_text(".env\n")
    if settings is not None:
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(json.dumps(settings))
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "one", cwd=repo)
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "untracked.txt").write_text("not committed\n")
    git("remote", "add", "origin", "https://user:tok3n@example.com/x.git", cwd=repo)
    return repo


HOOKS_SETTINGS = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]},
                  "permissions": {"allow": ["Bash(ls:*)"]}, "sandbox": {"enabled": True}}

BASE = {"format": "levain-routes-base/1", "routes": [
    {"route": "anthropic", "type": "brokered", "hosts": ["api.anthropic.com"],
     "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": "/"}},
    {"route": "openai", "type": "brokered", "hosts": ["api.openai.com"]},
    {"route": "github", "type": "egress", "hosts": ["github.com"]},
    {"route": "pypi", "type": "egress", "hosts": ["pypi.org"]},
    {"route": "claude-sub", "type": "subscription_passthrough", "hosts": ["claude.ai", "api.anthropic.com", "platform.claude.com"]}]}


class FakeLease:
    """The CONTROL connection's shape: a real socket (so its fd is real and CLOEXEC as Python makes it)."""

    def __init__(self, path: Path, calls: list) -> None:
        self.a, self.b = socket.socketpair()
        self.calls = calls
        calls.append(("connect", str(path)))

    def fileno(self) -> int:
        return self.a.fileno()

    def open_lease(self) -> str:
        self.calls.append(("LEASE-OPEN",))
        return "nonce-XYZ"

    def close(self) -> None:
        self.calls.append(("close",))
        self.a.close()
        self.b.close()


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: dict | None = HOOKS_SETTINGS,
                 **env: str) -> None:
        self.tmp = tmp_path
        self.repo = make_repo(tmp_path, settings)
        self.store = tmp_path / "store"
        (self.store / "routes").mkdir(parents=True)
        (self.store / "routes" / "base.json").write_text(json.dumps(BASE, sort_keys=True, separators=(",", ":")))
        self.helper = tmp_path / "fake_helper.py"
        self.helper.write_text(FAKE_HELPER)
        self.log = tmp_path / "helper.log"
        self.out = tmp_path / "terminal.out"
        self.calls: list = []
        self.err = open(tmp_path / "stderr.txt", "w+")
        self.out_fd = os.open(self.out, os.O_WRONLY | os.O_CREAT, 0o600)
        self.in_r, self.in_w = os.pipe()
        self.env = {**os.environ, "FAKE_STORE": str(self.store), "FAKE_LOG": str(self.log), **env}
        (tmp_path / "scratch").mkdir()
        self.seams = SV.Seams(host="darwin" if sys.platform == "darwin" else "linux",
                              helper=(sys.executable, str(self.helper)), store=self.store,
                              base_map=self.store / "routes" / "base.json",
                              control_socket=tmp_path / "control.sock",
                              client_factory=lambda p: FakeLease(p, self.calls), stdin=self.in_r,
                              stdout=self.out_fd, err=self.err, env=self.env, tmp_parent=tmp_path / "scratch",
                              start_grace_s=5.0)

    def run(self, **req: object) -> int:
        try:
            return SV.Supervisor(SV.Request(vendor="claude", repo=self.repo, **req), self.seams).run()  # type: ignore[arg-type]
        finally:
            for fd in (self.out_fd, self.in_w, self.in_r):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def feed(self, data: bytes) -> threading.Thread:
        """The operator typing (or pasting) ``data`` once the fake vendor has put its terminal in raw mode, then
        his stdin closing."""
        def go() -> None:
            deadline = time.monotonic() + 20
            while not (self.store / "raw").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            _write_all(self.in_w, data)
            os.close(self.in_w)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def verbs(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def stderr(self) -> str:
        self.err.flush()
        return (self.tmp / "stderr.txt").read_text()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# --- the full verb sequence ----------------------------------------------------------------------------------------

def test_full_sequence_against_fake_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    code = rig.run()
    v = rig.verbs()
    assert [x["verb"] for x in v] == ["prepare", "render", "probe", "start", "teardown"]
    assert v[0]["args"] == ["egress", "interactive", "claude", "brokered"]
    assert v[0]["stdin"] == '{"lease_nonce":"nonce-XYZ"}'
    blob = json.loads(v[1]["stdin"])
    assert v[1]["args"] == [SESSION] and v[2]["args"] == [SESSION] and v[3]["args"] == [SESSION]
    assert blob == {"format": "levain-session-render/1", "vendor": "claude", "routes": ["github", "pypi", "anthropic"],
                    "hooks": [], "git_identity": {"name": "Op Erator", "email": "op@example.com"},
                    "repo_label": "repo"}
    assert v[1]["stdin"] == json.dumps(blob, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert rig.calls[0] == ("connect", str(tmp_path / "control.sock"))
    assert rig.calls[-1] == ("close",)                      # the lease closes last, after teardown
    assert code == 0
    err = rig.stderr()
    assert f"session {SESSION} ended RETAINED: the work is kept at {rig.store}/retained/{SESSION}/tree" in err
    assert "claude exited 3" in err


def test_every_child_holds_only_fds_0_1_2_and_start_has_a_pty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                              ) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.run()
    v = rig.verbs()
    assert [x["fds"] for x in v] == [[0, 1, 2]] * 5
    assert [x["tty"] for x in v] == [False, False, False, True, False]
    out = rig.out.read_bytes()
    assert b"fds=[0, 1, 2]" in out


def test_start_output_reaches_the_terminal_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.run()
    out = rig.out.read_bytes()
    assert b"\x1b]2;slot title\x07" in out                  # titles pass
    assert b"52;" not in out and b"c2VjcmV0" not in out     # OSC 52 never reaches the terminal
    assert b"\x1b[6n" not in out                            # nor a report request
    assert "hello ☕ done".encode() in out.replace(b"\r", b"")   # a UTF-8 char split across two writes, intact


@bounded(60)
def test_large_paste_while_the_vendor_writes_does_not_deadlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    n = 300_000
    rig = Rig(tmp_path, monkeypatch, FAKE_START="paste", FAKE_PASTE=str(n))
    rig.feed(b"p" * n)
    assert rig.run() == 0
    assert f"got={n}".encode() in rig.out.read_bytes()


@bounded(60)
def test_operator_input_is_not_starved_by_streaming_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Input reaches a vendor whose output never stops. NOT a guard on same-pass starvation (a `continue` after the
    # master read stays green here: the reader outpaces this writer); that shape is held by the loop's structure.
    rig = Rig(tmp_path, monkeypatch, FAKE_START="stream")
    rig.feed(b"q")
    assert rig.run() == 0
    assert b"quit seen" in rig.out.read_bytes()


def test_sigterm_mid_session_tears_down_then_closes_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                              ) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_START="wait")
    started = rig.store / "started"

    def kill_when_started() -> None:
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=kill_when_started, daemon=True)
    before = signal.getsignal(signal.SIGTERM)
    t.start()
    code = rig.run()
    t.join(5)
    assert [x["verb"] for x in rig.verbs()] == ["prepare", "render", "probe", "start", "teardown"]
    assert code == 1
    assert "SIGTERM: ending session" in rig.stderr()
    assert f"session {SESSION} ended RETAINED" in rig.stderr()
    assert rig.calls[-1] == ("close",)
    assert signal.getsignal(signal.SIGTERM) is before      # the handlers are put back


def test_refusal_after_prepare_is_named_and_still_tears_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                            ) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_REFUSE="render=lease-dead")
    code = rig.run()
    assert [x["verb"] for x in rig.verbs()] == ["prepare", "render", "teardown"]
    assert code == 1
    err = rig.stderr()
    assert "refused lease-dead" in err
    assert "\x1b" not in err and "⟨U+001B⟩]52" in err     # the helper's stderr is shown escaped


def test_prepare_refused_means_no_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_REFUSE="prepare=no-free-slot:egress")
    assert rig.run() == 1
    assert [x["verb"] for x in rig.verbs()] == ["prepare"]
    assert "refused no-free-slot:egress" in rig.stderr()
    assert rig.calls[-1] == ("close",)


def test_teardown_held_is_exit_1_and_says_where_the_work_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_END="HELD")
    assert rig.run() == 1
    assert f"ended HELD: a step did not prove, the slot stays leased and the work stays in {rig.store}/live/{SESSION}" \
        in rig.stderr()


def test_slot_and_login_reach_prepare_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = json.loads(json.dumps(BASE))
    rig = Rig(tmp_path, monkeypatch)
    rig.run(auth="passthrough", kind="login", slot=3)
    v = rig.verbs()
    assert v[0]["args"] == ["egress", "login", "claude", "passthrough", "3"]
    assert json.loads(v[1]["stdin"])["routes"] == ["github", "pypi", "claude-sub"]
    assert base == BASE


# --- the repo layer refusal fires before anything is staged -----------------------------------------------------------

def test_loosening_layer_refused_before_any_lease_or_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch, settings={"sandbox": {"allowUnsandboxedCommands": True}})
    assert rig.run() == 1
    assert rig.verbs() == []                               # no helper verb ran
    assert rig.calls == []                                 # no lease was opened
    assert not (rig.store / "staging").exists()
    assert "refused loosening:" in rig.stderr() and "allow-unsandboxed-commands" in rig.stderr().lower() \
        or "allowUnsandboxedCommands" in rig.stderr()


def _plant(repo: Path, path: str, body: bytes, mode: str = "100644") -> None:
    sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=body, check=True,
                         capture_output=True).stdout.decode().strip()
    git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", cwd=repo)
    git("commit", "-q", "-m", "plant", cwd=repo)


def _docs(tmp_path: Path, repo: Path) -> dict:
    top, branch = SV.repo_toplevel(repo, uid=os.getuid())
    SV.operator_clone(top, branch, tmp_path / "c")
    return SV.repo_layer_docs(tmp_path / "c")


def test_case_folded_settings_name_is_checked(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _plant(repo, ".Claude/Settings.Local.json", b'{"sandbox":{"excludedCommands":["curl"]}}')
    docs = _docs(tmp_path, repo)
    assert docs == {".Claude/Settings.Local.json": {"sandbox": {"excludedCommands": ["curl"]}}}
    with pytest.raises(Refused) as e:
        SV.check_repo_layer(docs)
    assert e.value.name.startswith("loosening:")


@pytest.mark.parametrize("path,mode,body,why", [
    (".claude", "120000", b"elsewhere", "not a directory"),
    (".claude/settings.json", "120000", b"/etc/x", "not a regular file"),
    (".claude/settings.json", "100644", b"{not json", "not JSON"),
    (".claude/settings.json", "100644", b'{"a":1,"a":2}', "not JSON"),
])
def test_settings_entry_shapes_refused(tmp_path: Path, path: str, mode: str, body: bytes, why: str) -> None:
    repo = make_repo(tmp_path)
    _plant(repo, path, body, mode)
    with pytest.raises(Refused) as e:
        _docs(tmp_path, repo)
    assert e.value.name == "repo-settings-malformed" and why in e.value.detail


def test_hooks_and_permissions_layer_launches(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, HOOKS_SETTINGS)
    docs = _docs(tmp_path, repo)
    assert docs == {".claude/settings.json": HOOKS_SETTINGS}
    SV.check_repo_layer(docs)


# --- the operator-side clone (§2c 5) --------------------------------------------------------------------------------

def test_clone_is_tracked_only_shares_no_inode_and_has_no_remote(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, HOOKS_SETTINGS)
    top, branch = SV.repo_toplevel(repo, uid=os.getuid())
    assert branch == "main"
    clone = tmp_path / "clone"
    commit = SV.operator_clone(top, branch, clone)
    assert commit == git("rev-parse", "HEAD", cwd=repo).strip()
    assert git("remote", cwd=clone).strip() == ""
    assert "tok3n" not in git("config", "--local", "--list", cwd=clone)
    assert "tok3n" not in (clone / ".git" / "config").read_text()
    names = set(git("ls-tree", "-r", "--name-only", "HEAD", cwd=clone).split())
    assert names == {"README.md", ".gitignore", ".claude/settings.json"}
    mine = {(s.st_dev, s.st_ino) for p in (repo / ".git" / "objects").rglob("*") if p.is_file() for s in [p.stat()]}
    theirs = {(s.st_dev, s.st_ino) for p in (clone / ".git" / "objects").rglob("*") if p.is_file()
              for s in [p.stat()]}
    assert mine and theirs and not (mine & theirs)
    # and what the slot gets: the bundle made from this clone, cloned back
    make_bundle(clone, branch, tmp_path / "repo.bundle", tmp_parent=tmp_path)
    git("clone", "-q", str(tmp_path / "repo.bundle"), str(tmp_path / "slot"))
    got = {p.relative_to(tmp_path / "slot").as_posix() for p in (tmp_path / "slot").rglob("*")
           if ".git" not in p.parts and p.is_file()}
    assert got == {"README.md", ".gitignore", ".claude/settings.json"}
    assert git("remote", "-v", cwd=tmp_path / "slot").count("tok3n") == 0


def test_repo_must_be_a_work_tree_owned_on_a_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(Refused) as e:
        SV.repo_toplevel(repo, uid=os.getuid() + 1)
    assert e.value.name == "repo-not-owned"
    with pytest.raises(Refused) as e:
        SV.repo_toplevel(tmp_path / "nope", uid=os.getuid())
    assert e.value.name == "repo-not-a-work-tree"
    (tmp_path / "plain").mkdir()
    with pytest.raises(Refused) as e:
        SV.repo_toplevel(tmp_path / "plain", uid=os.getuid())
    assert e.value.name == "repo-not-a-work-tree"
    git("checkout", "-q", "--detach", cwd=repo)
    with pytest.raises(Refused) as e:
        SV.repo_toplevel(repo, uid=os.getuid())
    assert e.value.name == "repo-detached-head"


# --- the lease fd never reaches a child (§3.1 4) -------------------------------------------------------------------

_LIST_FDS = ("import os\nfds=[]\nfor fd in range(256):\n    try:\n        os.fstat(fd); fds.append(fd)\n"
             "    except OSError: pass\nprint(fds)")


def test_lease_fd_absent_from_a_real_child(tmp_path: Path) -> None:
    a, b = socket.socketpair()
    stray = os.open(os.devnull, os.O_RDONLY)
    os.set_inheritable(stray, True)                       # an inheritable fd the parent forgot: still not passed
    try:
        assert not os.get_inheritable(a.fileno())
        p = SV.spawn([sys.executable, "-c", _LIST_FDS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, lease_fds=(a.fileno(),))
        out, _ = p.communicate(timeout=20)
        assert out.decode().strip() == "[0, 1, 2]"
        # the pty form: the slave is 0, 1 and 2 and the child's controlling terminal
        m, s = os.openpty()
        script = _LIST_FDS + "\nprint(os.isatty(0), os.tcgetpgrp(0) == os.getpgrp())"
        p = SV.spawn([sys.executable, "-c", script], stdin=s, stdout=s, stderr=s, lease_fds=(a.fileno(),), ctty=True)
        os.close(s)
        got = b""                                         # read BEFORE wait: macOS drops a closed slave's unread bytes
        while True:
            try:
                chunk = os.read(m, 4096)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
        p.wait(timeout=20)
        os.close(m)
        assert got.replace(b"\r", b"").split(b"\n")[:2] == [b"[0, 1, 2]", b"True True"]
    finally:
        os.close(stray)
        a.close()
        b.close()


def test_spawn_refuses_an_inheritable_lease_or_one_given_as_stdio() -> None:
    a, b = socket.socketpair()
    try:
        with pytest.raises(Refused) as e:
            SV.spawn(["true"], stdin=a.fileno(), stdout=None, stderr=None, lease_fds=(a.fileno(),))
        assert e.value.name == "lease-fd-to-child"
        os.set_inheritable(a.fileno(), True)
        with pytest.raises(Refused) as e:
            SV.spawn(["true"], stdin=None, stdout=None, stderr=None, lease_fds=(a.fileno(),))
        assert e.value.name == "lease-fd-inheritable"
    finally:
        a.close()
        b.close()


# --- the helper's lines -----------------------------------------------------------------------------------------------

def test_parse_helper_line() -> None:
    assert SV.parse_helper_line(b'{"ok":true,"state":"RETAINED"}\n', "teardown") == {"state": "RETAINED"}
    with pytest.raises(Refused) as e:
        SV.parse_helper_line(b'{"ok":false,"refused":"lease-dead"}\n', "render")
    assert e.value.name == "lease-dead"
    for bad, name in ((b"", "helper-silent"), (b'{"ok":true}', "helper-output-malformed"),
                      (b'{"ok":true}\n{"ok":true}\n', "helper-output-malformed"),
                      (b'{"ok": true}\n', "helper-output-malformed"), (b'{"state":"x","ok":true}\n',
                                                                       "helper-output-malformed"),
                      (b'{"ok":false,"refused":"x","detail":"y"}\n', "helper-output-malformed"),
                      (b"[1]\n", "helper-output-malformed")):
        with pytest.raises(Refused) as e:
            SV.parse_helper_line(bad, "x")
        assert e.value.name == name, bad


def test_start_result_read_off_the_pty_tail() -> None:
    assert SV.start_result(b'bye\r\n\x1b[0m$ {"exit":3,"ok":true}\r\r\n') == {"exit": 3}
    assert SV.start_result(b'x{"a":1} {"exit":0,"ok":true}\r\n\r\n') == {"exit": 0}
    with pytest.raises(Refused) as e:
        SV.start_result(b'out\r\n{"ok":false,"refused":"clone-failed"}\r\n')
    assert e.value.name == "clone-failed"
    for tail in (b"", b"vendor output only\r\n", b'{"exit":0,"ok":true}\r\nmore after\r\n'):
        with pytest.raises(Refused) as e:
            SV.start_result(tail)
        assert e.value.name == "start-result-unreadable"


# --- the render blob's inputs ----------------------------------------------------------------------------------------

def test_pick_routes() -> None:
    r = BASE["routes"]
    assert SV.pick_routes(r, "claude", "brokered") == ("github", "pypi", "anthropic")
    assert SV.pick_routes(r, "codex", "brokered") == ("github", "pypi", "openai")
    assert SV.pick_routes(r, "claude", "passthrough") == ("github", "pypi", "claude-sub")
    with pytest.raises(Refused) as e:
        SV.pick_routes(r, "codex", "passthrough")
    assert e.value.name == "no-route:passthrough"
    with pytest.raises(Refused) as e:
        SV.pick_routes(r + [{"route": "anthropic2", "type": "brokered", "hosts": ["api.anthropic.com"]}],
                       "claude", "brokered")
    assert e.value.name == "route-ambiguous:brokered"


def _lacking(*hosts: str) -> list:
    return [r for r in BASE["routes"] if r["route"] != "claude-sub"] + [
        {"route": "claude-sub", "type": "subscription_passthrough", "hosts": list(hosts)}]


def test_claude_passthrough_route_without_the_token_host_is_refused_naming_it() -> None:
    """1029 ruling (np-e5b2ebed (a)): an operator's Claude passthrough route without the refresh's token host is
    refused when the routes load, naming the host and the route; nothing is merged into it."""
    from levain.slot.vendor_routes import CLAUDE_PASSTHROUGH_HOSTS
    assert SV._TOKEN_HOST in CLAUDE_PASSTHROUGH_HOSTS      # the check and the shipped route cannot drift apart
    with pytest.raises(Refused) as e:
        SV.pick_routes(_lacking("claude.ai", "api.anthropic.com"), "claude", "passthrough")
    assert e.value.name == "passthrough-route-lacks-token-host"
    assert e.value.detail == ("add platform.claude.com to the hosts of route claude-sub in the base map: "
                              "the login refresh needs it")
    ok = SV.pick_routes(_lacking("claude.ai", "api.anthropic.com", "platform.claude.com"), "claude", "passthrough")
    assert ok == ("github", "pypi", "claude-sub")
    assert SV._PROFILE_HOST == "api.anthropic.com" and SV._TOKEN_HOST == "platform.claude.com"
    with pytest.raises(Refused) as e:                       # neither identity host: both named
        SV.pick_routes(_lacking("claude.ai"), "claude", "passthrough")
    assert e.value.name == "passthrough-route-lacks-token-host"
    assert e.value.detail == ("add platform.claude.com and api.anthropic.com to the hosts of route claude-sub in the "
                              "base map: the login refresh and the login check need them")


def test_a_token_host_only_claude_passthrough_route_is_refused_naming_the_profile_host() -> None:
    """codex MED (L3 on 58622ca7): the token host alone passes the refresh but the profile call to api.anthropic.com
    is denied by the tunnel, so every launch ends identity-unparsable; refuse at load and name the profile host."""
    with pytest.raises(Refused) as e:
        SV.pick_routes(_lacking("platform.claude.com"), "claude", "passthrough")
    assert e.value.name == "passthrough-route-lacks-token-host"
    assert e.value.detail == ("add api.anthropic.com to the hosts of route claude-sub in the base map: "
                              "the login check needs it")


def test_the_token_host_check_reads_the_selected_route_not_an_entry_sharing_its_id() -> None:
    """L3 on 58622ca7 (all three lineages): the selected route is checked itself, never re-found by id."""
    good = [{"route": "claude-sub", "type": "egress", "hosts": ["github.com"]}] + _lacking(
        "claude.ai", "api.anthropic.com", "platform.claude.com")
    assert SV.pick_routes(good, "claude", "passthrough")[-1] == "claude-sub"   # no false refusal
    bad = [{"route": "claude-sub", "type": "egress", "hosts": ["platform.claude.com", "api.anthropic.com"]}] + _lacking(
        "claude.ai", "api.anthropic.com")
    with pytest.raises(Refused) as e:                       # no false pass off the egress entry's hosts
        SV.pick_routes(bad, "claude", "passthrough")
    assert e.value.name == "passthrough-route-lacks-token-host"


def test_the_token_host_rule_binds_only_a_claude_passthrough_route() -> None:
    lacking = _lacking("claude.ai", "api.anthropic.com")
    assert SV.pick_routes(lacking, "claude", "brokered") == ("github", "pypi", "anthropic")   # not passthrough
    base = lacking + [{"route": "codex-sub", "type": "subscription_passthrough", "hosts": ["chatgpt.com"]}]
    assert SV.pick_routes(base, "codex", "passthrough") == ("github", "pypi", "codex-sub")   # another vendor


def test_read_base_map(tmp_path: Path) -> None:
    p = tmp_path / "base.json"
    with pytest.raises(Refused) as e:
        SV.read_base_map(p)
    assert e.value.name == "base-map-unreadable"
    for fmt in ("levain-routes-base/1", "levain-routes-base/2"):   # 3c-2a: the writer flipped to /2, /1 still read
        p.write_text('{"format":"%s","routes":[]}' % fmt)
        assert SV.read_base_map(p) == []
    p.write_text('{"format":"other/1","routes":[]}')
    with pytest.raises(Refused) as e:
        SV.read_base_map(p)
    assert e.value.name == "base-map-malformed"


def test_no_hook_is_named_while_the_helper_holds_no_template() -> None:
    """HOOKS_BY_AUTH is empty BECAUSE the helper has no template to render one from; when it gains one, this fails
    and the supervisor's choice is made again."""
    from levain.slot.runtime_helper import Seams as HelperSeams

    assert HelperSeams.__dataclass_fields__["hook_templates"].default_factory() == {}   # type: ignore[misc]
    assert SV.HOOKS_BY_AUTH == {"brokered": (), "passthrough": ()}


def test_render_blob() -> None:
    data = SV.render_blob("claude", "brokered", ["anthropic"], None, "bad‮label")
    assert json.loads(data) == {"format": "levain-session-render/1", "vendor": "claude", "routes": ["anthropic"],
                                "hooks": []}                    # a label the grammar refuses is dropped
    with pytest.raises(Refused) as e:
        SV.render_blob("claude", "brokered", ["anthropic"], {"name": "x<y>", "email": "a@b"}, None)
    assert e.value.name == "git-identity-invalid"
