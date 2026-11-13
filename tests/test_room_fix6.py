"""ROOMFIX6 (levain 23, key 1112-40): fixes from the L3 verify round r2 on the Room deploy tip.

1. A host room whose profile carries hooks opens only on a Claude Code that honours ``"onFailure": "block"``
   (2.1.295 or later): ``host_probe`` refuses an older, missing or unparseable ``claude_code_version``.
2. The turn guard denies an event it cannot read as a JSON object (a nested-JSON bomb, a non-object, not JSON).
3. ``room_view.js`` draws a host room's ``dontAsk`` report (mode drift); the confined room still hides it.
4. ``room_read``'s empty body carries ``room_mode`` too.
5. The README and CHANGELOG say what the hooks-off refusal does not see."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from levain.cockpit import room as cockpit_room
from levain.desk import claude_session as cs
from levain.desk import room_turn_guard
from levain.desk.room_default import GUARD_PATH
from levain.desk.room_launch import RoomLaunchError
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_room import World
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_room_host_launch import launcher, mode, spawns, world  # noqa: F401  (world is a fixture)
from tests.test_room_mode_noise_1112 import _hidden, _row, _service
from tests.test_room_profile import profile, recorded_init, script  # noqa: F401  (script is a fixture)

REPO = GUARD_PATH.parents[2]


# --- 1. the Claude Code floor for a host room with hooks ------------------------------------------------------------
def _init(version):
    init = recorded_init()
    if version is None:
        init.pop("claude_code_version", None)
    else:
        init["claude_code_version"] = version
    return init


def test_the_floor_is_the_onfailure_version():
    assert cs.ONFAILURE_BLOCK_MIN_CC == (2, 1, 295)


@pytest.mark.parametrize("version", ["2.1.295", "2.1.323", "2.2.0", "3.0.0"])
def test_a_host_room_on_the_floor_or_later_is_live(script, version):
    assert cs.host_probe(_init(version), profile(script)).live


@pytest.mark.parametrize("version, shown", [("2.1.294", "'2.1.294'"), ("2.0.999", "'2.0.999'"), (None, "none"),
                                            ("garbage", "'garbage'"), ("", "''"), ("2.1", "'2.1'"),
                                            ("v2.1.323x", "'v2.1.323x'"), (2.1, "2.1"), ("2.1.295 ", "'2.1.295 '")])
def test_an_older_missing_or_unparseable_version_refuses_naming_found_and_required(script, version, shown):
    got = cs.host_probe(_init(version), profile(script))
    assert not got.live
    assert "2.1.295" in got.reason and shown in got.reason and "onFailure" in got.reason


def test_a_host_profile_with_no_hooks_owes_no_floor(script):
    assert cs.host_probe(_init("2.1.200"), profile(script, hooks=())).live
    assert cs.host_probe(_init(None), profile(script, hooks=())).live


def test_the_confined_probe_is_untouched():
    """The confined room maps to ``profile=None`` and :func:`open_probe`, which reads no version."""
    import inspect
    assert "claude_code_version" not in inspect.getsource(cs.open_probe)


def test_a_host_open_on_an_older_claude_code_refuses_and_is_not_live(world):
    mode(world, "old_cc")
    lr = launcher(world)
    with pytest.raises(RoomLaunchError, match=r"2\.1\.294.*2\.1\.295|2\.1\.295.*2\.1\.294"):
        lr.ensure()
    assert not lr._live
    mode(world, "ok")
    assert launcher(world).ensure(fresh=True).harness_session_id


# --- 2. the guard's own parse stage ---------------------------------------------------------------------------------
def _run_guard(payload: str):
    return subprocess.run([sys.executable, "-I", str(GUARD_PATH)], input=payload, capture_output=True, text=True,
                          timeout=60)


_BOMB = '{"a":' * 200000 + "1" + "}" * 200000


@pytest.mark.parametrize("payload", [_BOMB, "[" * 200000 + "]" * 200000, "[]", '"x"', "null", "7", "not json", ""])
def test_an_event_the_guard_cannot_read_as_an_object_is_denied(payload):
    got = _run_guard(payload)
    assert got.returncode == 2, (got.returncode, got.stderr[-400:])
    out = json.loads(got.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "could not read this call" in out["permissionDecisionReason"]
    assert "Traceback" not in got.stderr


def test_a_bash_call_with_a_list_tool_input_is_still_denied():
    got = _run_guard(json.dumps({"tool_name": "Bash", "tool_input": [1, 2]}))
    assert got.returncode == 2 and "could not check this Bash call" in got.stdout


def test_a_parse_crash_of_any_kind_denies(monkeypatch, capsys):
    def boom(_f):
        raise MemoryError("big")
    monkeypatch.setattr(room_turn_guard.json, "load", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert room_turn_guard.main() == 2
    assert "MemoryError" in capsys.readouterr().out


def test_an_object_still_decides_as_before():
    assert _run_guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})).returncode == 0
    assert _run_guard(json.dumps({"tool_name": "Read", "tool_input": {}})).returncode == 0
    assert _run_guard(json.dumps({})).returncode == 0
    assert _run_guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "sleep 9 &"}})).returncode == 2


def test_the_guard_header_names_the_parse_deny_and_the_matcher():
    text = GUARD_PATH.read_text()
    assert "(3)" in text and "HOOK_MATCHER" in text.split('"""')[1]
    assert "including a payload it cannot parse as a JSON object, exits 0" not in text


# --- 3. room_view.js: a host room's dontAsk report draws ------------------------------------------------------------
@needs_browser
def test_a_host_room_reporting_dontask_draws_it_and_the_confined_room_still_hides_it(tmp_path, qdir):
    rows = [_row("error:mode:u1", "dontAsk", 1), _row("error:mode:u2", "bypassPermissions", 2)]
    host = tmp_path / "host"
    host.mkdir()
    assert _hidden(host, qdir, {"room_thread": "t", "room_mode": "bypassPermissions", "stream": rows}) == [
        "error:mode:u1"]
    for n, body in enumerate(({"room_mode": "dontAsk"}, {}, {"room_mode": None}, {"room_mode": ""})):
        d = tmp_path / ("c%d" % n)
        d.mkdir()
        assert _hidden(d, qdir, {"room_thread": "t", "stream": rows, **body}) == ["error:mode:u2"], body


# --- 4. the empty body names the mode too ---------------------------------------------------------------------------
def test_the_empty_body_carries_room_mode(qdir, tmp_path):
    w = World(qdir)
    nowhere = tmp_path / "no-store"
    nowhere.mkdir()
    assert cockpit_room.room_read(nowhere, _service(w, "bypassPermissions"))["room_mode"] == "bypassPermissions"
    assert cockpit_room.room_read(nowhere, None)["room_mode"] is None
    empty = cockpit_room.room_read(nowhere, w.svc)
    assert empty["room_thread"] is None and empty["room_mode"] == "dontAsk"


# --- 5. what the hooks-off refusal does not see ---------------------------------------------------------------------
def test_the_readme_and_changelog_say_what_the_hooks_off_refusal_does_not_see():
    readme = (REPO / "README.md").read_text()
    assert "it does not see server-managed settings or a `policyHelper`'s output" in readme
    assert "a settings change made after the room opened" in readme and "not a count per call" in readme
    assert "The room also opens only on Claude Code 2.1.295 or later" in readme
    changelog = (REPO / "CHANGELOG.md").read_text().split("\n## [", 2)[1]
    assert "policyHelper" in changelog and "2.1.295" in changelog
