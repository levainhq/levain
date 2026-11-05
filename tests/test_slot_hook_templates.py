"""1105-32 Finding 2: render's two hook templates have a producer, wired into the helper's REAL default seams, and the
hook FAILS CLOSED (a gate inside an unattended slot that opens when it breaks is not a gate)."""
from __future__ import annotations

import dataclasses
import io
import os
import subprocess
import sys
import types

import pytest

from levain.slot import hook_run
from levain.slot import render as rd
from levain.slot import runtime_helper as H
from levain.slot.hook_templates import EVENTS, body, hook_templates
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON
from tests.test_slot_s2b_render import egress

BOTH = frozenset({"levain_gate", "levain_queue"})


def test_default_seams_pass_render_check_facts(monkeypatch):
    """The cross-contract test: the REAL default Seams (no injected hook seam) -> the helper's own ``render_facts`` ->
    ``check_facts``. On 23c79b16 this raised ``hook-template-missing``."""
    blob, facts = egress(kind="dispatch", auth="passthrough", hooks=BOTH)
    monkeypatch.setattr(H, "base_routes", lambda store: (dict(facts.route_types), dict(facts.route_hosts)))
    alloc = types.SimpleNamespace(slot=facts.slot, slot_uid=facts.slot_uid, child_uid=None, var_folders=facts.var_folders,
                                  session=facts.session, kind=facts.kind, vendor=facts.vendor, auth=facts.auth)
    ctx = types.SimpleNamespace(seams=H.Seams(host="darwin"), env={}, store=None)
    got = H.render_facts(ctx, alloc, blob)
    rd.check_facts(blob, got)
    assert set(got.hook_templates) == BOTH


def test_a_blob_naming_no_hook_gets_none_and_an_explicit_empty_seam_is_still_missing():
    blob, _ = egress(hooks=BOTH)
    assert H.hook_templates_for(H.Seams(host="linux"), egress()[0]) == {}
    assert H.hook_templates_for(H.Seams(host="linux", hook_templates={}), blob) == {}


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_templates_shape(host):
    t = hook_templates(host)
    assert {k: v.event for k, v in t.items()} == EVENTS == {"levain_gate": "PreToolUse",
                                                           "levain_queue": "PermissionRequest"}
    for name, tpl in t.items():
        assert tpl.matcher is None                      # every tool: a matcher can only make the gate look at less
        text = tpl.body.decode()
        assert text.startswith("#!%s %s\n" % (ROOT_PYTHON[host], PY_FLAGS))
        assert "run(%r)" % name in text and "sys.exit(2)" in text


def test_the_rendered_settings_wire_both_hooks():
    blob, facts = egress(kind="dispatch", auth="passthrough", hooks=BOTH)
    facts = dataclasses.replace(facts, hook_templates=H.hook_templates_for(H.Seams(host="darwin"), blob))
    cfg = rd.render_cc_settings(blob, facts)
    assert set(cfg["hooks"]) == {"PreToolUse", "PermissionRequest"}


# ---- fail closed ----------------------------------------------------------------------------------------------

def _run(load, environ=None, stdin="{}"):
    out, err = io.StringIO(), io.StringIO()
    env = {"LEVAIN_QUEUE_SOCKET": "/x/q.sock"} if environ is None else environ
    code = hook_run.run("levain_gate", stdin=io.StringIO(stdin), stdout=out, stderr=err, environ=env, load_adapter=load)
    return code, out.getvalue(), err.getvalue()


def _cannot_import():
    raise ImportError("no adapter here")


def test_an_adapter_that_cannot_import_is_a_named_refusal_and_no_allow():
    code, out, err = _run(_cannot_import)
    assert code == hook_run.REFUSED_EXIT == 2 and out == ""
    assert "refused: hook-adapter-unavailable" in err and "allow" not in out


def test_an_adapter_that_raises_or_exits_nonzero_is_refused():
    class Boom:
        @staticmethod
        def main(argv, stdin=None, stdout=None):
            raise RuntimeError("x")

    class Bad:
        @staticmethod
        def main(argv, stdin=None, stdout=None):
            stdout.write("{}")
            return 1

    class Exits:
        @staticmethod
        def main(argv, stdin=None, stdout=None):
            raise SystemExit(0)

    for adapter, why in ((Boom, "hook-adapter-failed"), (Bad, "hook-adapter-exit"), (Exits, "hook-adapter-failed")):
        code, out, err = _run(lambda a=adapter: a)
        assert code == 2 and out == "" and why in err


def test_outside_a_slot_the_operator_path_is_never_reached():
    class Never:
        @staticmethod
        def main(argv, stdin=None, stdout=None):
            raise AssertionError("the operator path ran")

    code, out, err = _run(lambda: Never, environ={})
    assert code == 2 and "hook-not-in-slot" in err and out == ""


def test_an_answering_adapter_passes_through_unchanged():
    class Ok:
        @staticmethod
        def main(argv, stdin=None, stdout=None):
            stdout.write('{"deny": 1}\n')
            return 0

    assert _run(lambda: Ok) == (0, '{"deny": 1}\n', "")


def test_the_real_adapter_denies_a_gate_event_with_no_reachable_broker(monkeypatch):
    """No stub: the real adapter in a slot environment whose socket does not exist denies (never an allow)."""
    ev = ('{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, '
          '"cwd": "/tmp", "session_id": "s1"}')
    env = {"LEVAIN_QUEUE_SOCKET": "/nonexistent/q.sock", "LEVAIN_QUEUE_CAP": "c"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)                       # the adapter reads os.environ itself
    code, out, _ = _run(None, environ=env, stdin=ev)
    assert code == 0 and '"permissionDecision": "deny"' in out


def test_the_body_runs_isolated_with_only_the_tree_on_its_path_and_fails_closed(tmp_path):
    """The script, run as a subprocess with a tree that holds no levain: exit 2 and the named refusal."""
    text = body("darwin", "levain_gate").decode().replace("/usr/local/lib/levain-root/current", str(tmp_path))
    script = tmp_path / "hook.py"
    script.write_text(text.split("\n", 1)[1])                                  # drop the shebang line
    p = subprocess.run([sys.executable, "-I", "-S", str(script)], input="{}", capture_output=True, text=True,
                       cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "")})
    assert p.returncode == 2 and "hook-adapter-unavailable" in p.stderr and p.stdout == ""


_E2E = """
import io, os, sys
sys.path.insert(0, {repo!r})
os.environ.update({{"LEVAIN_QUEUE_SOCKET": "/nonexistent/q.sock", "LEVAIN_QUEUE_CAP": "c"}})
from levain.slot.hook_run import run
ev = ('{{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {{"command": "ls"}}, '
      '"cwd": "/tmp", "session_id": "s1"}}')
o = io.StringIO()
code = run("levain_gate", stdin=io.StringIO(ev), stdout=o)
print(sys.version_info[0], sys.version_info[1], code, "deny" in o.getvalue())
"""


def test_the_adapter_answers_on_the_floor_interpreter_the_hook_runs_under():
    """The hook runs under ``root_tree.ROOT_PYTHON`` (3.9 on macOS), and the adapter is outside the root-py39 closure
    (it imports what root keeps out), so this is its floor test: import + a real gate event, denied (no broker)."""
    py = ROOT_PYTHON["darwin"]
    probe = subprocess.run([py, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.split()[:2] != ["3", "9"]:
        pytest.skip("no 3.9 %s on this host" % py)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([py, "-I", "-c", _E2E.format(repo=repo)], capture_output=True, text=True)
    assert p.stdout.split() == ["3", "9", "0", "True"], (p.stdout, p.stderr)
