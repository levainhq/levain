"""The room's product default is the host (flowConnect) profile; the confined room is an explicit opt-in (levain 23,
lane ROOMFLIP; Phill's signed ruling np-97fa3dd6 (a), 11-12: "RETIRE 'NO HANDS' FOR CHAT EVERYWHERE. The levain Room
DEFAULT becomes the flowConnect profile").

What is pinned here: levain ships its own turn guard (``levain/desk/room_turn_guard.py``, a port of flow's Room fork of
flowConnect's guard) at a pinned sha256, refusing and allowing exactly the cases flowConnect's own tests name (ported, not
imported); ``default_host_profile()`` runs that guard as its one hook; ``levain serve --room DIR`` and
``_build_room_serve(...)`` open that profile unless the caller names the confined room (``--room-confined`` /
``profile=CONFINED``); and a guard that is missing or not the pinned bytes REFUSES the room, at the build and at every
open, never falling back to a room without it and never silently to the confined one. ``RoomSetting.profile=None`` and
the low-level functions keep meaning the confined room (their suites are unchanged)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from levain.desk import room_default
from levain.desk.room_default import CONFINED, GUARD_PATH, ROOM_TURN_GUARD_SHA256, default_host_profile
from levain.desk.room_launch import RoomLaunchError
from levain.desk.room_profile import HookSpec, RoomProfile

sys.path.insert(0, str(Path(__file__).parent))
import test_room_host_launch as H  # noqa: E402 - the host-launch rig (fake claude child, store, launcher)

# The guard's sha256 as this release pins it. A re-sync from flowConnect changes it here AND in room_default, on purpose.
PINNED_GUARD_SHA256 = "f74b43b35d1458449b665db94415c41035f37fb93e1eb2b22595d267a66b680e"
GUARD_MATCHER = "Bash|Agent|Task|CronCreate|Monitor|ScheduleWakeup|Workflow"


# --- the guard: pinned, standard library, flowConnect's cases -------------------------------------------------------------

def test_the_guard_is_the_pinned_bytes_inside_the_package():
    import levain.desk
    assert GUARD_PATH == Path(levain.desk.__file__).resolve().parent / "room_turn_guard.py"
    assert hashlib.sha256(GUARD_PATH.read_bytes()).hexdigest() == PINNED_GUARD_SHA256
    assert ROOM_TURN_GUARD_SHA256 == PINNED_GUARD_SHA256


def test_the_guard_imports_only_the_standard_library():
    import ast
    tree = ast.parse(GUARD_PATH.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= {"json", "re", "sys"}, names


def bash(command, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": command, **extra}}


# flowConnect's own cases (server/test_chat_guard.py, as flow's tests/test_room_turn_guard.py ported them)
REFUSED = [
    ("background bash", bash("sleep 60", run_in_background=True)),
    ("cron", {"tool_name": "CronCreate", "tool_input": {"cron": "*/5 * * * *", "prompt": "check"}}),
    ("monitor", {"tool_name": "Monitor", "tool_input": {"command": "tail -f x", "timeout_ms": 30000}}),
    ("workflow", {"tool_name": "Workflow", "tool_input": {"script": "x"}}),
    ("background agent", {"tool_name": "Agent", "tool_input": {"prompt": "x", "run_in_background": True}}),
    ("background task", {"tool_name": "Task", "tool_input": {"prompt": "x", "run_in_background": True}}),
    ("deep_review by path", bash("python3 ~/Briefcase/flow/scripts/deep_review.py --diff HEAD~1 --paths a.py")),
    ("consult after cd and &&", bash("cd ~/Briefcase/flow && python3 scripts/consult.py 'review' --agents complement")),
    ("consult through a venv python", bash("/x/venv/bin/python3 -u /x/scripts/consult.py hi")),
    ("consult run directly", bash("./scripts/consult.py hi")),
    ("nohup", bash("nohup ./job.sh > out.log 2>&1")),
    ("trailing ampersand", bash("./job.sh > out.log 2>&1 &")),
    ("disown", bash("./job.sh & disown")),
    ("ampersand then more commands", bash("python3 long.py > out.log 2>&1 & echo \"pid $!\"")),
    ("ampersand then a newline", bash("python3 long.py > out.log 2>&1 &\nsleep 2; cat out.log")),
    ("sleep & echo", bash("sleep 100 & echo started")),
    ("subshell background", bash("(sleep 100 &)")),
    ("env prefix and time", bash("FOO=1 time python3 -u ~/x/scripts/consult.py q")),
    ("subshell group backgrounded", bash("(cd x && ./long.sh) >log 2>&1 &")),
    ("group then amp, no space", bash("(sleep 100; echo done)&")),
    ("bash -lc", bash("bash -lc 'sleep 600 &'")),
    ("bash fed a heredoc", bash("bash <<'EOF'\npython3 scripts/consult.py review\nEOF")),
    ("python fed a heredoc", bash("python3 - <<'EOF'\nimport subprocess\nEOF\npython3 scripts/consult.py q")),
    ("eval", bash("eval 'sleep 600 &'")),
    ("inside an if", bash("if test -f x; then python3 scripts/consult.py review; fi")),
    ("timeout wrapper", bash("timeout 900 python3 scripts/consult.py q")),
    ("nice -n wrapper", bash("nice -n 10 python3 consult.py q")),
    ("sudo -u wrapper", bash("sudo -u me python3 consult.py q")),
    ("python -W flag", bash("python3 -W ignore scripts/consult.py q")),
    ("command substitution", bash("echo $(python3 scripts/consult.py q)")),
    ("background inside a quoted substitution", bash('echo "$(sleep 9 &)"')),
    ("background in a substitution inside arithmetic", bash("echo $(( $(sleep 9 &) + 1 ))")),
    ("the same, quoted", bash('echo "$(( $(sleep 9 &) + 1 ))"')),
    ("unquoted heredoc substitution", bash("cat <<EOF\n$(python3 scripts/deep_review.py --diff HEAD)\nEOF")),
    ("apostrophe in a comment, then a launch", bash("# don't wait\npython3 scripts/consult.py q")),
    ("scheduled wakeup", {"tool_name": "ScheduleWakeup", "tool_input": {"delaySeconds": 300}}),
    ("wrapped shell reader with a heredoc", bash("timeout 900 bash <<'EOF'\nsleep 600 &\nEOF")),
    ("multiline group backgrounded", bash("(\n  sleep 600\n) &")),
    ("inside bash -c", bash("bash -c 'cd /x && python3 scripts/deep_review.py --diff HEAD~1'")),
]
ALLOWED = [
    ("plain bash", bash("git status --short")),
    ("read the script", bash("cat ~/Briefcase/flow/scripts/consult.py")),
    ("grep the script", bash("grep -n 'def main' ~/Briefcase/flow/scripts/deep_review.py")),
    ("&& chain", bash("cd /tmp && ls")),
    ("stderr redirect", bash("ls nope 2>&1")),
    ("foreground bash flagged false", bash("ls", run_in_background=False)),
    ("commit message mentions the script", bash('git commit -m "fix python3 scripts/consult.py timeout"')),
    ("grep for a command line", bash('grep -rn "python3 consult.py" docs/')),
    ("heredoc body mentions it", bash("cat >> TOOLS.md <<'EOF'\npython3 ~/flow/scripts/consult.py \"q\"\nsleep 1 &\nEOF")),
    ("man disown", bash("man disown")),
    ("quoted ampersand in a url", bash('curl "http://x/y?a=1&b=2"')),
    ("redirect both streams", bash("make &> build.log")),
    ("python on another script", bash("python3 -u scripts/other.py --help")),
    ("quoted ampersand argument", bash('grep -rn "&" src/')),
    ("single-quoted ampersand", bash("printf '%s\\n' '&'")),
    ("ampersand in a comment", bash("echo ok # note & more")),
    ("data heredoc with a dashed delimiter", bash("cat <<'END-MARK'\npython3 scripts/consult.py q\nsleep 1 &\nEND-MARK")),
    ("python -m pytest on a test named for it", bash("python3 -m pytest test_consult.py")),
    ("apostrophe in a comment, no launch", bash("# don't wait\nls -la")),
    ("py_compile on a file named for it", bash("python3 -m py_compile server/consult.py")),
    ("another script that is passed its name", bash("python3 tools/lint.py scripts/deep_review.py")),
    ("a script searching for the word", bash("python3 scripts/hub.py search consult")),
    ("bitwise and in arithmetic", bash("echo $((a & b))")),
    ("bitwise and in quoted arithmetic", bash('echo "$((5 & 3))"')),
    ("command -v", bash("command -v consult")),
    ("a python heredoc that prints the name", bash("python3 <<'PY'\nprint('python3 scripts/consult.py')\nPY")),
    ("a non-Bash tool", {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}),
    ("a foreground agent", {"tool_name": "Agent", "tool_input": {"prompt": "x"}}),
    ("a write", {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "y"}}),
]


def _run_guard(payload: str) -> subprocess.CompletedProcess:
    """The guard as the room's child runs it: ``<python> -I <script>``, one event on stdin."""
    return subprocess.run([sys.executable, "-I", str(GUARD_PATH)], input=payload, capture_output=True, text=True,
                          timeout=30)


@pytest.mark.parametrize("name,event", REFUSED, ids=[n for n, _ in REFUSED])
def test_the_guard_run_as_a_hook_refuses(name, event):
    got = _run_guard(json.dumps({"hook_event_name": "PreToolUse", **event}))
    assert got.returncode == 2, (got.returncode, got.stdout, got.stderr)
    out = json.loads(got.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "Long work is not allowed in a Room turn" in reason and "Hand it to a seat or the desk" in reason
    assert "phone chat" not in reason
    assert "Phill" not in reason and "tell the user they can drop it" in reason     # levain's one wording change


@pytest.mark.parametrize("name,event", ALLOWED, ids=[n for n, _ in ALLOWED])
def test_the_guard_run_as_a_hook_allows(name, event):
    got = _run_guard(json.dumps({"hook_event_name": "PreToolUse", **event}))
    assert got.returncode == 0 and got.stdout == "", (got.returncode, got.stdout, got.stderr)


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", ""])
def test_a_payload_the_guard_cannot_read_is_let_through(junk):
    got = _run_guard(junk)
    assert got.returncode == 0 and got.stdout == ""


# --- the default host profile ---------------------------------------------------------------------------------------------

def test_the_default_host_profile_is_flowconnects_shape_with_levains_own_guard():
    prof = default_host_profile()
    assert isinstance(prof, RoomProfile)
    assert prof.name == "host" and prof.permission_mode == "bypassPermissions"
    assert prof.setting_sources == ("user", "project", "local")
    assert prof.hooks == (HookSpec(GUARD_MATCHER, str(GUARD_PATH), 10, sha256=PINNED_GUARD_SHA256),)
    assert prof.required_tools == ("Bash", "Read", "Edit", "Write")
    # levain names no host's prompt or dials: the vendor's defaults and the setting's model apply
    assert (prof.system_prompt, prof.model, prof.effort, prof.max_turns, prof.turn_budget_usd, prof.recycle_at_usd) == \
        (None, None, None, None, None, None)


def test_the_matcher_covers_every_tool_the_guard_refuses_by_name():
    assert set(default_host_profile().hooks[0].matcher.split("|")) == set(GUARD_MATCHER.split("|"))


def test_a_missing_guard_refuses_the_default_profile_naming_the_file(tmp_path, monkeypatch):
    gone = tmp_path / "room_turn_guard.py"
    monkeypatch.setattr(room_default, "GUARD_PATH", gone)
    with pytest.raises(ValueError) as exc:
        default_host_profile()
    msg = str(exc.value)
    assert str(gone) in msg and "--room-confined" in msg and "turn guard" in msg


def test_a_guard_that_is_not_the_pinned_bytes_refuses_the_default_profile(tmp_path, monkeypatch):
    other = tmp_path / "room_turn_guard.py"
    other.write_bytes(GUARD_PATH.read_bytes() + b"# drift\n")
    monkeypatch.setattr(room_default, "GUARD_PATH", other)
    with pytest.raises(ValueError) as exc:
        default_host_profile()
    assert str(other) in str(exc.value) and "pinned" in str(exc.value)


# --- HookSpec's pin ---------------------------------------------------------------------------------------------------

def test_a_hook_spec_with_a_pin_refuses_other_bytes_and_one_without_is_unchanged(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("import sys\nsys.exit(0)\n")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    spec = HookSpec("Bash", str(script), sha256=sha)
    spec.check()
    assert HookSpec("Bash", str(script)).sha256 is None
    script.write_text("import sys\nsys.exit(1)\n")
    with pytest.raises(ValueError, match="pinned"):
        spec.check()
    with pytest.raises(ValueError, match="pinned"):
        HookSpec("Bash", str(script), sha256=sha)
    HookSpec("Bash", str(script)).check()          # no pin: any readable script, as before


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, 5])
def test_a_hook_pin_must_be_a_sha256(tmp_path, bad):
    script = tmp_path / "hook.py"
    script.write_text("x = 1\n")
    with pytest.raises(ValueError, match="pinned sha256 is 64 lowercase hex"):
        HookSpec("Bash", str(script), sha256=bad)


# --- fail closed at the open --------------------------------------------------------------------------------------------

def _pinned_copy(tmp_path) -> tuple[Path, RoomProfile]:
    """The default profile, re-pointed at a copy of the guard this test may change (same bytes, same pin)."""
    copy = tmp_path / "guard_copy.py"
    copy.write_bytes(GUARD_PATH.read_bytes())
    prof = default_host_profile()
    spec = prof.hooks[0]
    import dataclasses
    return copy, dataclasses.replace(prof, hooks=(HookSpec(spec.matcher, str(copy), spec.timeout, sha256=spec.sha256),))


def test_the_default_profile_opens_a_room_whose_settings_run_a_copy_of_the_pinned_guard(tmp_path):
    w = H.build(tmp_path, profile=default_host_profile())
    try:
        seen, spawn = H.argv_spy()
        h = H.launcher(w, spawn=spawn).ensure(fresh=True)
        assert h.launch_id
        copy = H.hook_copy(spawn.settings[-1])
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == PINNED_GUARD_SHA256
        argv = seen[-1][0]
        assert "--dangerously-skip-permissions" in argv and "--tools" not in argv
        assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


def test_a_guard_changed_after_the_launcher_was_built_refuses_the_open_and_spawns_nothing(tmp_path):
    copy, prof = _pinned_copy(tmp_path)
    w = H.build(tmp_path, profile=prof)
    try:
        lr = H.launcher(w)
        copy.write_bytes(copy.read_bytes() + b"# drift\n")
        with pytest.raises(RoomLaunchError) as exc:
            lr.ensure(fresh=True)
        assert str(copy) in str(exc.value) and "pinned" in str(exc.value)
        assert H.spawns(w) == []
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


def test_a_guard_gone_after_the_launcher_was_built_refuses_the_open_and_spawns_nothing(tmp_path):
    copy, prof = _pinned_copy(tmp_path)
    w = H.build(tmp_path, profile=prof)
    try:
        lr = H.launcher(w)
        copy.unlink()
        with pytest.raises(RoomLaunchError) as exc:
            lr.ensure(fresh=True)
        assert str(copy)[:100] in str(exc.value)
        assert H.spawns(w) == []
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


def test_the_bytes_copied_for_the_child_are_checked_against_the_pin_not_only_the_file_checked_before(tmp_path, monkeypatch):
    """The race between the check and the copy: the launcher compares the bytes it COPIES with the pin, so a guard that
    changed after ``HookSpec.check`` passed still never runs."""
    from levain.desk.room_launch import RoomLauncher
    copy, prof = _pinned_copy(tmp_path)
    w = H.build(tmp_path, profile=prof)
    try:
        lr = H.launcher(w)
        copy.write_bytes(copy.read_bytes() + b"# drift\n")
        monkeypatch.setattr(RoomLauncher, "_check_hook", staticmethod(lambda spec: None))
        with pytest.raises(RoomLaunchError) as exc:
            lr.ensure(fresh=True)
        assert str(copy)[:100] in str(exc.value) and "pinned" in str(exc.value)
        assert H.spawns(w) == []
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


# --- the product entry points ---------------------------------------------------------------------------------------------

@pytest.fixture
def homes(tmp_path, monkeypatch):
    (tmp_path / "h").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def test_build_room_serve_names_no_profile_and_gets_the_host_default(homes):
    from levain.web_server import _build_room_serve
    rs = _build_room_serve(homes)
    try:
        assert rs.status()["profile"] == "host" and rs.service._room_mode == "bypassPermissions"
        assert rs.launcher._setting.profile == default_host_profile()
        assert json.loads(rs.launcher._settings_json) == {}           # no deny list, no read block
    finally:
        rs.stop()


def test_build_room_serve_with_confined_is_the_no_shell_room(homes):
    from levain.web_server import _build_room_serve
    rs = _build_room_serve(homes, profile=CONFINED)
    try:
        assert rs.status()["profile"] is None and rs.service._room_mode == "dontAsk"
        assert rs.launcher._setting.profile is None
        assert json.loads(rs.launcher._settings_json)["permissions"]["deny"]
    finally:
        rs.stop()


def test_build_room_serve_still_takes_a_hosts_own_profile(homes, tmp_path):
    from levain.web_server import _build_room_serve
    prof = H.make_profile(tmp_path)
    rs = _build_room_serve(homes, profile=prof)
    try:
        assert rs.launcher._setting.profile is prof and rs.status()["profile"] == "host"
    finally:
        rs.stop()


@pytest.mark.parametrize("junk", ["host", "CONFINED", 0, object()])
def test_build_room_serve_refuses_a_profile_it_does_not_know(homes, junk):
    from levain.web_server import _build_room_serve
    with pytest.raises(ValueError, match="profile"):
        _build_room_serve(homes, profile=junk)


def test_build_room_serve_refuses_when_the_guard_is_missing_and_opens_no_confined_room(homes, tmp_path, monkeypatch):
    from levain.web_server import _build_room_serve
    monkeypatch.setattr(room_default, "GUARD_PATH", tmp_path / "gone.py")
    with pytest.raises(ValueError, match="turn guard"):
        _build_room_serve(homes)


def _serve_args(argv):
    from levain.cli import build_parser
    return build_parser().parse_args(argv)


def test_the_serve_flag_room_confined_parses_and_defaults_off(tmp_path):
    a = _serve_args(["serve", "--write", "--room", str(tmp_path)])
    assert a.room_confined is False
    b = _serve_args(["serve", "--write", "--room", str(tmp_path), "--room-confined"])
    assert b.room_confined is True


def _run_serve(tmp_path, monkeypatch, **kw):
    """run_web_server up to the room's build, recording what it asked _build_room_serve for (then refused there)."""
    import levain.web_server as ws
    from levain.dashboard import AnnealPaths, SubstrateSource
    (tmp_path / "memory.db").write_bytes(b"")
    monkeypatch.setattr(ws, "_resolve_source", lambda p: SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db")))
    asked = []

    def fake(room, host_commands=None, profile=None):
        asked.append(profile)
        raise ValueError("stopped by the test")
    monkeypatch.setattr(ws, "_build_room_serve", fake)

    def no_server(*_a, **_k):
        raise AssertionError("run_web_server reached make_server: the room's refusal did not stop it")
    monkeypatch.setattr(ws, "make_server", no_server)
    rc = ws.run_web_server(tmp_path, open_browser=False, write=True, **kw)
    return rc, asked


def test_levain_serve_room_builds_the_host_default_and_room_confined_builds_the_confined_room(tmp_path, monkeypatch):
    rc, asked = _run_serve(tmp_path, monkeypatch, room=tmp_path)
    assert rc == 1 and asked == [None]                     # None: _build_room_serve's host default
    rc, asked = _run_serve(tmp_path, monkeypatch, room=tmp_path, room_confined=True)
    assert rc == 1 and asked == [CONFINED]


def test_room_confined_without_a_room_is_refused(tmp_path, monkeypatch, capsys):
    rc, asked = _run_serve(tmp_path, monkeypatch, room_confined=True)
    assert rc == 2 and asked == []
    assert "--room-confined" in capsys.readouterr().err


def test_the_cli_passes_room_confined_to_the_server(tmp_path, monkeypatch):
    import levain.web_server as ws
    from levain import cli
    got = {}
    monkeypatch.setattr(ws, "run_web_server", lambda **kw: got.update(kw) or 0)
    assert cli.main(["serve", "--write", "--room", str(tmp_path), "--room-confined", "--no-open"]) == 0
    assert got["room_confined"] is True and got["room"] == tmp_path


# --- what the product says --------------------------------------------------------------------------------------------

def test_the_serve_help_says_what_the_default_room_is_and_names_the_confined_opt_in():
    from levain.cli import build_parser
    parser = build_parser()
    serve = next(a for a in parser._subparsers._group_actions[0].choices.values() if a.prog.endswith(" serve"))
    helps = {a.dest: a.help for a in serve._actions}
    assert "no shell" not in helps["room"].split("--room-confined")[0]
    assert "--room-confined" in helps["room"] and "shell" in helps["room"]
    assert "no shell" in helps["room_confined"]


def test_the_readme_describes_the_default_room_and_the_confined_opt_in():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "--room-confined" in readme
    assert "Read and search tools and no shell" not in readme
    line = next(l for l in readme.splitlines() if l.startswith("- **Cockpit room:**"))
    assert "--room-confined" in line and "turn guard" in line
