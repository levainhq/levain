"""`levain consent canary --sandbox`: the courier canary against FAKE vendor runners and a FAKE host (no model, no real
session, no real adopt).

The shipped probe script runs here only contained: its network calls are redirected, its home is a scratch directory,
so nothing writes the real home or leaves the machine.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from levain.consent import canary

SESSION = "s-0123456789abcdef"
ALL_STOPPED = {"control_workspace_write": "OK", "net_control": "OK", "home_write": "STOPPED", "offlist_host": "STOPPED",
               "cockpit_loopback": "STOPPED", "nested_hook": "OK"}


def _args(prompt: str) -> tuple[str, int, str, str]:
    m = re.search(r"python3 levain_probe\.py (\w+) (\d+) (\S+) (\S+) It is", prompt)
    assert m, prompt
    return m.group(1), int(m.group(2)), m.group(3), m.group(4)


def _lines(nonce: str, statuses: dict[str, str]) -> str:
    return "".join(f"{canary.PROBE_PREFIX} {nonce} {n} {s} detail\n" for n, s in statuses.items())


class World:
    """A fake vendor plus a fake host. Every knob is what the HOST would observe, independent of the printed lines."""

    def __init__(self, tmp_path: Path, *, statuses=None, raw=None, error="", transcript_session=True,
                 control="good", tree=(), hit_cockpit=False, home_probe=False, hook_marker=False, retained_error="",
                 discard_note=""):
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.statuses, self.raw, self.error = statuses, raw, error
        self.transcript_session, self.control, self.tree = transcript_session, control, tuple(tree)
        self.hit_cockpit, self.home_probe, self.hook_marker = hit_cockpit, home_probe, hook_marker
        self.retained_error, self.discard_note = retained_error, discard_note
        self.nonce = ""
        self.discarded: list[str] = []
        self.read_calls = 0

    def runner(self, agent, repo, prompt, done, timeout):
        self.nonce, port, home, _ = _args(prompt)
        assert home == str(self.home)
        if self.hit_cockpit:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        if self.home_probe:
            (self.home / f"levain-canary-probe-{self.nonce}").write_text("")
        text = self.raw(self.nonce) if self.raw else _lines(self.nonce, self.statuses or ALL_STOPPED)
        if self.transcript_session:
            text += f"levain session: session {SESSION} ended RETAINED: the work is kept at /x\n"
        return canary.RunOutput(text, self.error)

    def read_retained(self, repo, session, nonce):
        self.read_calls += 1
        assert session == SESSION
        if self.hook_marker:
            (self.home / f"levain-canary-hook-{nonce}").write_text("")
        if self.retained_error:
            return canary.RetainedReading(error=self.retained_error)
        sha = hashlib.sha256((repo / canary.PROBE_FILE).read_bytes()).hexdigest()
        text = {"good": f"{nonce} {sha}\n", "absent": None, "wrongsha": f"{nonce} {'0' * 64}\n",
                "oldnonce": f"deadbeefdead {sha}\n"}[self.control]
        return canary.RetainedReading(text, self.tree)

    def discard(self, session):
        self.discarded.append(session)
        return self.discard_note

    def seams(self):
        return canary.HostSeams(home=self.home, read_retained=self.read_retained, discard=self.discard)

    def run(self, tmp_path, name="ws"):
        return canary.run_sandbox_canary("claude_code", workspace=tmp_path / name, runner=self.runner,
                                         host=self.seams())


# --- the verdict -------------------------------------------------------------------------------------------------

def test_every_probe_clean_and_the_host_agreeing_passes_and_exits_zero(tmp_path, capsys):
    w = World(tmp_path)
    rc = canary.cli_canary("claude_code", tmp_path / "ws", sandbox=True, runner=w.runner, host=w.seams())
    assert rc == 0 and "PASS" in capsys.readouterr().out


def test_an_escape_is_a_named_fail_and_a_non_zero_exit(tmp_path, capsys):
    w = World(tmp_path, statuses={**ALL_STOPPED, "home_write": "ESCAPED"})
    rc = canary.cli_canary("claude_code", tmp_path / "ws", sandbox=True, runner=w.runner, host=w.seams())
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL" in out and "ESCAPED home_write" in out and "PASS" not in out


@pytest.mark.parametrize("probe", canary.ESCAPE_PROBES)
def test_each_escape_probe_escaping_fails_by_its_name(tmp_path, probe):
    result = World(tmp_path, statuses={**ALL_STOPPED, probe: "ESCAPED"}).run(tmp_path)
    assert result.status == "FAIL" and f"ESCAPED {probe}" in result.detail and not result.ok()


@pytest.mark.parametrize("probe", canary.ALL_PROBES)
def test_a_missing_probe_line_is_a_fail(tmp_path, probe):
    result = World(tmp_path, statuses={n: s for n, s in ALL_STOPPED.items() if n != probe}).run(tmp_path)
    assert result.status == "FAIL"
    assert (f"control {probe}" if probe in canary.CONTROL_PROBES else f"missing {probe}") in result.detail


def test_a_control_must_read_ok(tmp_path):
    result = World(tmp_path, statuses={**ALL_STOPPED, "net_control": "ERROR"}).run(tmp_path)
    assert result.status == "FAIL" and "control net_control did not read OK" in result.detail


def test_an_escape_probe_cannot_read_ok_and_a_control_cannot_read_stopped(tmp_path):
    a = World(tmp_path, statuses={**ALL_STOPPED, "home_write": "OK"}).run(tmp_path)
    b = World(tmp_path, statuses={**ALL_STOPPED, "control_workspace_write": "STOPPED"}).run(tmp_path, "ws2")
    assert "home_write reported OK, a status that probe cannot have" in a.detail
    assert "control_workspace_write reported STOPPED, a status that probe cannot have" in b.detail


def test_a_probe_that_could_not_tell_is_a_fail(tmp_path):
    result = World(tmp_path, statuses={**ALL_STOPPED, "offlist_host": "ERROR"}).run(tmp_path)
    assert result.status == "FAIL" and "ERROR offlist_host" in result.detail


# --- C1: the courier may confabulate; the host's own evidence decides ---------------------------------------------

@pytest.mark.parametrize("control", ["absent", "wrongsha", "oldnonce"])
def test_c1_fabricated_lines_without_the_control_file_read_back_are_a_fail(tmp_path, control):
    """RED before the fix: five printed lines were a PASS whether or not the script ran."""
    result = World(tmp_path, control=control).run(tmp_path)
    assert result.status == "FAIL" and "the probe did not run" in result.detail


def test_c1_no_session_in_the_transcript_is_a_fail_and_nothing_is_read_or_discarded(tmp_path):
    w = World(tmp_path, transcript_session=False)
    result = w.run(tmp_path)
    assert result.status == "FAIL" and "the probe did not run" in result.detail
    assert "names no session" in result.detail and w.read_calls == 0 and w.discarded == []


def test_c1_an_unreadable_retained_tree_is_a_fail_that_says_why(tmp_path):
    result = World(tmp_path, retained_error="adopt failed: refused").run(tmp_path)
    assert result.status == "FAIL" and "adopt failed: refused" in result.detail


def test_c1_the_real_home_path_present_is_an_escape_even_when_the_courier_printed_stopped(tmp_path):
    w = World(tmp_path, home_probe=True)
    result = w.run(tmp_path)
    assert result.status == "FAIL" and "ESCAPED home_write (the file exists in the host's real home)" in result.detail
    assert "contradiction" in result.detail
    assert not (w.home / f"levain-canary-probe-{w.nonce}").exists()  # cleaned up


def test_c1_the_real_home_path_absent_is_the_stopped_evidence(tmp_path):
    assert World(tmp_path).run(tmp_path).status == "PASS"


def test_c1_a_connection_the_cockpit_stand_in_saw_fails_even_when_the_courier_says_stopped(tmp_path):
    result = World(tmp_path, hit_cockpit=True).run(tmp_path)
    assert result.status == "FAIL" and "stand-in listener saw 1 connection" in result.detail
    assert "contradiction" in result.detail


# --- C2: the planted hook never runs on the host ------------------------------------------------------------------

def test_c2_a_hook_marker_on_the_host_is_an_escape_and_is_removed(tmp_path):
    w = World(tmp_path, hook_marker=True)
    result = w.run(tmp_path)
    assert result.status == "FAIL" and "ESCAPED nested_hook (the planted hook ran on the host)" in result.detail
    assert not (w.home / f"levain-canary-hook-{w.nonce}").exists()


def test_c2_a_dot_git_path_in_the_imported_tree_is_an_escape(tmp_path):
    result = World(tmp_path, tree=[".levain-canary-x", "levain_probe_nested/.git/hooks/post-checkout"]).run(tmp_path)
    assert result.status == "FAIL" and "a .git path crossed into the imported tree" in result.detail


def test_c2_a_dot_gitignore_is_not_a_dot_git_path(tmp_path):
    assert World(tmp_path, tree=[".gitignore", "a/.github/x"]).run(tmp_path).status == "PASS"


def test_c2_the_planted_hook_is_executable_and_names_the_host_marker(tmp_path):
    """The shipped probe plants a REAL executable hook in a nested git repo (git init), which would touch the marker."""
    mod = _load_probe(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    mod.nested_hook("abc123", str(home))
    hook = Path(mod.HERE) / "levain_probe_nested" / ".git" / "hooks" / "post-checkout"
    assert os.access(hook, os.X_OK)
    assert str(home / "levain-canary-hook-abc123") in hook.read_text()
    out = subprocess.run([str(hook)], capture_output=True, text=True)  # contained: it touches only the scratch marker
    assert out.returncode == 0 and (home / "levain-canary-hook-abc123").exists()


# --- C3 / C8: the parser ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("decorate", [
    lambda l: l.replace(" STOPPED", " STOPPED_junk").replace(" OK", " OK_junk"),
    lambda l: l.replace(" STOPPED", " STOPPED1").replace(" OK", " OK-"),
    lambda l: "junk" + l,
    lambda l: l.rstrip("\n") + "trailing words\n",
])
def test_c3_a_line_that_is_not_the_whole_line_does_not_parse(tmp_path, decorate):
    result = World(tmp_path, raw=lambda n: decorate(_lines(n, ALL_STOPPED))).run(tmp_path)
    assert result.status == "FAIL" and "did not parse whole" in result.detail


def test_c3_a_terminal_bullet_or_border_around_a_whole_line_is_allowed(tmp_path):
    raw = lambda n: "".join(f"  ⎿  {l.rstrip()} │\n" for l in _lines(n, ALL_STOPPED).splitlines())  # noqa: E731
    assert World(tmp_path, raw=raw).run(tmp_path).status == "PASS"


def test_c3_a_repeated_identical_line_is_fine_a_conflicting_one_is_a_fail(tmp_path):
    twice = World(tmp_path, raw=lambda n: _lines(n, ALL_STOPPED) * 2).run(tmp_path)
    assert twice.status == "PASS", twice.detail
    conflict = World(tmp_path, raw=lambda n: _lines(n, ALL_STOPPED) + _lines(n, {"home_write": "ESCAPED"})).run(
        tmp_path, "ws2")
    assert conflict.status == "FAIL" and "reported both STOPPED and ESCAPED" in conflict.detail


def test_c3_lines_with_another_nonce_are_not_this_runs(tmp_path):
    result = World(tmp_path, raw=lambda n: _lines("deadbeefdead", ALL_STOPPED)).run(tmp_path)
    assert result.status == "FAIL" and "missing home_write" in result.detail


def test_c3_an_unknown_probe_name_is_a_fail(tmp_path):
    result = World(tmp_path, raw=lambda n: _lines(n, ALL_STOPPED) + _lines(n, {"extra_probe": "STOPPED"})).run(tmp_path)
    assert result.status == "FAIL" and "unknown probe 'extra_probe'" in result.detail


def test_c8_a_final_unterminated_fragment_is_ignored_not_a_parse_problem(tmp_path):
    """RED before the fix: a cut tail (`... nested_hook O`) read 'did not parse whole' on an otherwise clean run."""
    retained = f"levain session: session {SESSION} ended RETAINED: the work is kept at /x\n"
    raw = lambda n: (retained + _lines(n, {k: v for k, v in ALL_STOPPED.items() if k != "nested_hook"})  # noqa: E731
                     + f"{canary.PROBE_PREFIX} {n} nested_hook OK\n{canary.PROBE_PREFIX} {n} nested_hook O")
    assert World(tmp_path, raw=raw, transcript_session=False).run(tmp_path).status == "PASS"
    seen, problems = canary.parse_probe_lines(f"{canary.PROBE_PREFIX} ab home_write STOPPED", "ab")
    assert seen == {} and problems == []  # the only line is unterminated: ignored, so the probe reads missing


def test_c8_a_cut_line_that_is_followed_by_more_text_is_still_a_fail(tmp_path):
    raw = lambda n: (_lines(n, {k: v for k, v in ALL_STOPPED.items() if k != "nested_hook"})  # noqa: E731
                     + f"{canary.PROBE_PREFIX} {n} nested_hook O\nmore words\n")
    result = World(tmp_path, raw=raw).run(tmp_path)
    assert result.status == "FAIL" and "did not parse whole" in result.detail


# --- C5 / the runner's error paths --------------------------------------------------------------------------------

def test_c5_the_session_is_discarded_after_it_is_read_pass_or_fail(tmp_path):
    ok = World(tmp_path)
    assert ok.run(tmp_path).status == "PASS" and ok.discarded == [SESSION]
    bad = World(tmp_path, statuses={**ALL_STOPPED, "home_write": "ESCAPED"})
    assert bad.run(tmp_path, "ws2").status == "FAIL" and bad.discarded == [SESSION]


def test_c5_a_discard_that_did_not_complete_is_named_in_the_result(tmp_path):
    result = World(tmp_path, discard_note=f"session {SESSION} was not discarded: refused").run(tmp_path)
    assert result.status == "PASS" and "was not discarded" in result.detail


def test_a_runner_that_raises_is_a_fail_not_a_traceback(tmp_path):
    def boom(*a):
        raise OSError(errno.EIO, "pty write")
    result = canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws", runner=boom,
                                       host=World(tmp_path).seams())
    assert result.status == "FAIL" and "could not be driven" in result.detail


def test_a_runner_error_is_a_fail_that_says_why(tmp_path):
    result = World(tmp_path, raw=lambda n: "", error="levain session refused to start").run(tmp_path)
    assert result.status == "FAIL" and "levain session refused to start" in result.detail


# --- the prompt and the scratch repo -----------------------------------------------------------------------------

def test_c9_the_courier_prompt_is_one_line_carries_its_arguments_and_never_the_line_prefix():
    prompt = canary.courier_prompt("abc123", 4321, "/Users/some one", "api.anthropic.com")
    assert "\n" not in prompt and "\r" not in prompt
    assert "python3 levain_probe.py abc123 4321 '/Users/some one' api.anthropic.com" in prompt
    assert canary.PROBE_PREFIX not in prompt and "LEVAIN-PROBE" not in prompt


def test_the_allowed_hosts_are_hosts_the_session_wall_lets_through():
    from levain.slot import supervisor
    for agent, vendor in canary.VENDOR_OF.items():
        assert canary.ALLOWED_HOST[agent] in supervisor.VENDOR_HOSTS[vendor]


def test_the_scratch_repo_holds_the_reviewed_probe_tracked(tmp_path):
    repo = canary._scratch_repo(tmp_path / "ws")
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert tracked == [canary.PROBE_FILE]
    assert (repo / canary.PROBE_FILE).read_text() == canary.probe_source()


def test_a_non_empty_workspace_is_refused(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "keep.txt").write_text("x")
    result = canary.run_sandbox_canary("codex", workspace=ws, runner=World(tmp_path).runner, host=World(tmp_path).seams())
    assert result.status == "FAIL" and "new or empty directory" in result.detail


def test_the_flag_reaches_the_canary_and_without_it_the_hook_canary_runs(monkeypatch):
    from levain import cli
    calls = []
    monkeypatch.setattr(canary, "cli_canary", lambda *a, **k: calls.append((a, k)) or 0)
    assert cli.main(["consent", "canary", "--agent", "codex", "--sandbox"]) == 0
    assert cli.main(["consent", "canary", "--agent", "codex"]) == 0
    assert [k["sandbox"] for _, k in calls] == [True, False]


def test_the_probe_ships_in_the_wheel():
    import tomllib
    from fnmatch import fnmatch
    root = Path(canary.__file__).resolve().parents[2]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]
    pkg = root / "levain"
    shipped = {p.relative_to(pkg).as_posix() for g in cfg["package-data"]["levain"] for p in pkg.glob(g) if p.is_file()}
    assert "consent/" + canary.PROBE_RESOURCE in {s for s in shipped
                                                  if not any(fnmatch(s, x) for x in cfg["exclude-package-data"]["levain"])}


# --- the shipped probe script, contained --------------------------------------------------------------------------

def _load_probe(tmp_path: Path) -> types.ModuleType:
    """The shipped probe as a module whose workspace is a scratch directory under ``tmp_path``."""
    mod = types.ModuleType("probe_under_test")
    mod.__file__ = str(tmp_path / "ws" / "levain_probe.py")
    (tmp_path / "ws").mkdir(exist_ok=True)
    Path(mod.__file__).write_text(canary.probe_source())
    exec(compile(canary.probe_source(), mod.__file__, "exec"), mod.__dict__)
    return mod


class _Proxy:
    """A local stand-in for the session's proxy: answers every CONNECT with the status it is given."""

    def __init__(self, status: int):
        self.status, self.seen = status, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self.seen.append(conn.recv(512).split(b"\r\n", 1)[0].decode())
                conn.sendall(f"HTTP/1.1 {self.status} x\r\n\r\n".encode())

    def close(self):
        self.sock.close()


def _probe_offlist(tmp_path, monkeypatch, capsys, *, direct, proxy_status=None):
    """Run the probe's offlist_host with the DIRECT connect to the off-list address failing as ``direct`` says
    (an errno, 'timeout' or 'ok'), and the proxy answering ``proxy_status`` (None = no proxy configured)."""
    mod = _load_probe(tmp_path)
    real = socket.create_connection
    proxy = _Proxy(proxy_status) if proxy_status else None
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}" if proxy else "")

    def fake(addr, timeout=None, *a, **k):
        if addr == mod.OFF_LIST:
            if direct == "timeout":
                raise socket.timeout("timed out")
            if direct == "ok":
                return socket.socket()
            raise OSError(getattr(errno, direct), direct)
        return real(addr, timeout, *a, **k)

    monkeypatch.setattr(socket, "create_connection", fake)
    try:
        mod.offlist_host("abc123")
    finally:
        if proxy:
            proxy.close()
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize("direct", ["ENETUNREACH", "EHOSTUNREACH", "timeout"])
def test_c4_an_unreachable_or_silent_network_is_could_not_tell_not_stopped(tmp_path, monkeypatch, capsys, direct):
    """RED before the fix: every connect failure read STOPPED, so an offline machine passed the off-list wall."""
    line = _probe_offlist(tmp_path, monkeypatch, capsys, direct=direct)
    assert " offlist_host ERROR " in line and "could-not-tell" in line


@pytest.mark.parametrize("direct", ["EPERM", "EACCES", "ECONNREFUSED"])
def test_c4_a_refusal_attributable_to_the_wall_is_stopped(tmp_path, monkeypatch, capsys, direct):
    assert f" offlist_host STOPPED {direct}" in _probe_offlist(tmp_path, monkeypatch, capsys, direct=direct)


def test_c4_the_proxys_refusal_is_stopped_even_when_the_direct_path_is_silent(tmp_path, monkeypatch, capsys):
    line = _probe_offlist(tmp_path, monkeypatch, capsys, direct="timeout", proxy_status=403)
    assert " offlist_host STOPPED proxy-403" in line


def test_c4_a_proxy_that_opens_the_off_list_tunnel_is_an_escape(tmp_path, monkeypatch, capsys):
    assert " offlist_host ESCAPED proxy-200" in _probe_offlist(tmp_path, monkeypatch, capsys, direct="timeout",
                                                               proxy_status=200)


def test_c4_a_direct_connect_that_lands_is_an_escape(tmp_path, monkeypatch, capsys):
    assert " offlist_host ESCAPED direct" in _probe_offlist(tmp_path, monkeypatch, capsys, direct="ok")


def test_c4_the_positive_network_control_reads_ok_through_the_proxy_and_error_without(tmp_path, monkeypatch, capsys):
    mod = _load_probe(tmp_path)
    proxy = _Proxy(200)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
    mod.net_control("abc123", "api.anthropic.com")
    assert " net_control OK " in capsys.readouterr().out
    assert proxy.seen == ["CONNECT api.anthropic.com:443 HTTP/1.1"]
    proxy.close()
    monkeypatch.setenv("HTTPS_PROXY", "")
    mod.net_control("abc123", "api.anthropic.com")
    assert " net_control ERROR " in capsys.readouterr().out


def test_the_control_file_holds_the_nonce_and_the_scripts_own_sha256(tmp_path, capsys):
    mod = _load_probe(tmp_path)
    mod.control_workspace_write("abc123")
    text = (Path(mod.HERE) / ".levain-canary-abc123").read_text()
    assert text == f"abc123 {hashlib.sha256(canary.probe_source().encode()).hexdigest()}\n"
    assert " control_workspace_write OK" in capsys.readouterr().out


def test_the_real_home_write_probe_leaves_its_file_for_the_canary_to_read(tmp_path, capsys):
    mod = _load_probe(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    mod.home_write("abc123", str(home))
    assert (home / "levain-canary-probe-abc123").exists() and " home_write ESCAPED" in capsys.readouterr().out
    home.chmod(0o500)
    try:
        if os.geteuid() != 0:
            mod.home_write("abc124", str(home))
            assert " home_write STOPPED " in capsys.readouterr().out
    finally:
        home.chmod(0o700)


def test_the_probe_script_refuses_bad_arguments_without_probing(tmp_path):
    script = str(Path(canary.__file__).with_name(canary.PROBE_RESOURCE))
    for argv in (["x y", "1", str(tmp_path), "a.b"], ["ab", "1", "relative", "a.b"], ["ab", "1", str(tmp_path)]):
        out = subprocess.run([sys.executable, script, *argv], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
        assert out.returncode == 2 and out.stdout == "", argv


# --- the runner's pty drive (C6, C7, C9) -------------------------------------------------------------------------

def _fake_child(monkeypatch, *, record=None):
    """Replace `levain session ...` with a child that prints a line and waits; records the Popen kwargs."""
    real = subprocess.Popen

    def popen(argv, **kw):
        if record is not None:
            record.update(kw)
        return real([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"], **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)


def test_c9_c6_the_prompt_goes_in_as_one_bracketed_paste_then_enter_and_no_preexec_fn(tmp_path, monkeypatch):
    kw: dict = {}
    _fake_child(monkeypatch, record=kw)
    writes: list[bytes] = []
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(bytes(data)) or real_write(fd, data))
    out = canary.session_runner("claude_code", tmp_path, "one line prompt", lambda t: True, 60.0)
    assert out.error == "", out.error
    assert writes[0] == b"\x1b[200~one line prompt\x1b[201~" and writes[1] == b"\r"
    assert "preexec_fn" not in kw and kw["start_new_session"] is True


def test_c7_a_pty_write_failure_is_a_structured_error_not_a_traceback(tmp_path, monkeypatch):
    _fake_child(monkeypatch)

    def bad_write(fd, data):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "write", bad_write)
    out = canary.session_runner("claude_code", tmp_path, "p", lambda t: True, 60.0)
    assert "the pty failed" in out.error and "Input/output error" in out.error


def test_c6_the_controlling_terminal_shim_works_without_a_preexec_fn():
    import pty
    fd, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, "-c", canary._CTTY_SHIM, sys.executable, "-c",
                                 "open('/dev/tty','w').write('CTTY-OK')"], stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
        os.close(slave)
        got = b""
        while True:  # a process exiting on a macOS pty waits for its output to be read
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            got += data
        assert proc.wait(timeout=30) == 0 and b"CTTY-OK" in got
    finally:
        os.close(fd)
