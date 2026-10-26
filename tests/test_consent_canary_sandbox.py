"""`levain consent canary --sandbox`: the courier canary against FAKE vendor runners (no model, no real session).

The probe script itself is exercised only with HOME pointed at a scratch directory, the off-list host and the
cockpit port aimed at a closed local port, and the nested path pre-blocked, so nothing here writes the real home
or leaves the machine.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from levain.consent import canary

ALL_STOPPED = {"control_workspace_write": "OK", "home_write": "STOPPED", "offlist_host": "STOPPED",
               "cockpit_loopback": "STOPPED", "nested_hook": "STOPPED"}


def _nonce_port(prompt: str) -> tuple[str, int]:
    m = re.search(r"python3 levain_probe\.py (\w+) (\d+)", prompt)
    assert m, prompt
    return m.group(1), int(m.group(2))


def _lines(nonce: str, statuses: dict[str, str]) -> str:
    return "".join(f"{canary.PROBE_PREFIX} {nonce} {n} {s} detail\n" for n, s in statuses.items())


def _runner(statuses=None, *, raw=None, error="", hit_cockpit=False):
    """A fake vendor: no model. Reports the given statuses (or raw text) for the nonce the prompt carries."""
    def run(agent, repo, prompt, done, timeout):
        nonce, port = _nonce_port(prompt)
        if hit_cockpit:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        text = raw(nonce) if raw else _lines(nonce, statuses if statuses is not None else ALL_STOPPED)
        return canary.RunOutput(text, error)
    return run


def _run(runner, tmp_path):
    return canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws", runner=runner)


def test_an_escape_is_a_named_fail_and_a_non_zero_exit(tmp_path, capsys):
    """The ruled RED test: a fake runner reporting an escape makes the canary exit non-zero and say FAIL by name."""
    escaped = {**ALL_STOPPED, "home_write": "ESCAPED"}
    rc = canary.cli_canary("claude_code", tmp_path / "ws", sandbox=True, runner=_runner(escaped))
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out and "ESCAPED home_write" in out and "PASS" not in out


@pytest.mark.parametrize("probe", canary.ESCAPE_PROBES)
def test_each_escape_probe_escaping_fails_by_its_name(tmp_path, probe):
    result = _run(_runner({**ALL_STOPPED, probe: "ESCAPED"}), tmp_path)
    assert result.status == "FAIL" and f"ESCAPED {probe}" in result.detail and not result.ok()


def test_every_probe_stopped_and_the_control_ok_passes(tmp_path, capsys):
    rc = canary.cli_canary("claude_code", tmp_path / "ws", sandbox=True, runner=_runner())
    assert rc == 0 and "PASS" in capsys.readouterr().out


@pytest.mark.parametrize("probe", canary.ALL_PROBES)
def test_a_missing_probe_line_is_a_fail(tmp_path, probe):
    result = _run(_runner({n: s for n, s in ALL_STOPPED.items() if n != probe}), tmp_path)
    assert result.status == "FAIL"
    assert (f"missing {probe}" if probe != canary.CONTROL_PROBE else "control") in result.detail


def test_a_truncated_or_malformed_line_is_a_fail(tmp_path):
    def raw(nonce):
        good = _lines(nonce, {k: v for k, v in ALL_STOPPED.items() if k != "nested_hook"})
        return good + f"{canary.PROBE_PREFIX} {nonce} nested_hook STOP"  # cut mid-word: never a pass
    result = _run(_runner(raw=raw), tmp_path)
    assert result.status == "FAIL" and "did not parse whole" in result.detail and "missing nested_hook" in result.detail


def test_a_status_glued_to_more_letters_is_malformed_not_stopped(tmp_path):
    result = _run(_runner(raw=lambda n: _lines(n, ALL_STOPPED).replace("home_write STOPPED", "home_write STOPPEDX")),
                  tmp_path)
    assert result.status == "FAIL" and "did not parse whole" in result.detail


def test_the_control_must_read_ok(tmp_path):
    result = _run(_runner({**ALL_STOPPED, "control_workspace_write": "ERROR"}), tmp_path)
    assert result.status == "FAIL" and "cannot have" in result.detail and "control" in result.detail


def test_an_escape_probe_cannot_read_ok(tmp_path):
    result = _run(_runner({**ALL_STOPPED, "home_write": "OK"}), tmp_path)
    assert result.status == "FAIL" and "home_write reported OK" in result.detail


def test_a_probe_that_could_not_tell_is_a_fail(tmp_path):
    result = _run(_runner({**ALL_STOPPED, "offlist_host": "ERROR"}), tmp_path)
    assert result.status == "FAIL" and "ERROR offlist_host" in result.detail


def test_a_repeated_identical_line_is_fine_a_conflicting_one_is_a_fail(tmp_path):
    twice = _run(_runner(raw=lambda n: _lines(n, ALL_STOPPED) * 2), tmp_path)
    assert twice.status == "PASS", twice.detail
    conflict = canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws2", runner=_runner(
        raw=lambda n: _lines(n, ALL_STOPPED) + _lines(n, {"home_write": "ESCAPED"})))
    assert conflict.status == "FAIL" and "reported both STOPPED and ESCAPED" in conflict.detail


def test_lines_with_another_nonce_are_not_this_runs(tmp_path):
    result = _run(_runner(raw=lambda n: _lines("deadbeefdead", ALL_STOPPED)), tmp_path)
    assert result.status == "FAIL" and "missing home_write" in result.detail


def test_an_unknown_probe_name_is_a_fail(tmp_path):
    result = _run(_runner(raw=lambda n: _lines(n, ALL_STOPPED) + _lines(n, {"extra_probe": "STOPPED"})), tmp_path)
    assert result.status == "FAIL" and "unknown probe 'extra_probe'" in result.detail


def test_a_connection_the_cockpit_stand_in_saw_fails_even_when_the_courier_says_stopped(tmp_path):
    """The courier is not trusted for this one: the listener is the canary's own and counts server-side."""
    result = _run(_runner(hit_cockpit=True), tmp_path)
    assert result.status == "FAIL" and "stand-in listener saw 1 connection" in result.detail


def test_a_runner_error_is_a_fail_that_says_why(tmp_path):
    result = _run(_runner(raw=lambda n: "", error="levain session refused to start"), tmp_path)
    assert result.status == "FAIL" and "levain session refused to start" in result.detail


def test_the_courier_prompt_carries_the_nonce_and_port_and_never_the_line_prefix():
    prompt = canary.courier_prompt("abc123", 4321)
    assert "python3 levain_probe.py abc123 4321" in prompt
    assert canary.PROBE_PREFIX not in prompt and "LEVAIN-PROBE" not in prompt


def test_the_scratch_repo_holds_the_reviewed_probe_tracked(tmp_path):
    repo = canary._scratch_repo(tmp_path / "ws")
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert tracked == [canary.PROBE_FILE]
    assert (repo / canary.PROBE_FILE).read_text() == canary.probe_source()


def test_a_non_empty_workspace_is_refused(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "keep.txt").write_text("x")
    result = canary.run_sandbox_canary("codex", workspace=ws, runner=_runner())
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


# --- the real probe script, contained -----------------------------------------------------------------------

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _contained_walled_runner(tmp_path, *, writable_home: bool):
    """Runs the SHIPPED probe script, with HOME in a scratch dir, the off-list host and the cockpit port aimed at a
    closed local port, and the nested path blocked by a file. The home is read-only unless ``writable_home``."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700 if writable_home else 0o500)
    closed = _closed_port()

    def run(agent, repo, prompt, done, timeout):
        nonce, _ = _nonce_port(prompt)
        src = (repo / canary.PROBE_FILE).read_text()
        assert 'OFF_LIST = ("1.1.1.1", 443)' in src
        script = tmp_path / "contained_probe.py"
        script.write_text(src.replace('OFF_LIST = ("1.1.1.1", 443)', f'OFF_LIST = ("127.0.0.1", {closed})')
                          .replace("HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {str(repo)!r}"))
        (repo / "levain_probe_nested").write_text("a file where the nested clone would go")
        out = subprocess.run([sys.executable, str(script), nonce, str(closed)], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"], "HOME": str(home)}, timeout=60)
        assert out.returncode == 0, out.stderr
        return canary.RunOutput(out.stdout)
    return run


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the read-only home")
def test_the_real_probe_script_walled_passes_and_unwalled_fails_by_name(tmp_path):
    ok = canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws",
                                   runner=_contained_walled_runner(tmp_path, writable_home=False))
    assert ok.status == "PASS", ok.detail
    bad = canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws2",
                                    runner=_contained_walled_runner(tmp_path / "w", writable_home=True))
    assert bad.status == "FAIL" and "ESCAPED home_write" in bad.detail
    assert not (tmp_path / "w" / "home" / "levain_canary_probe").exists()  # the probe removed what it made


def test_the_probe_script_refuses_bad_arguments_without_probing(tmp_path):
    out = subprocess.run([sys.executable, str(Path(canary.__file__).with_name(canary.PROBE_RESOURCE)), "x y"],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert out.returncode == 2 and out.stdout == ""
