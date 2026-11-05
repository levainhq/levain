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
        assert "run(%r)" % name in text and "code = 2\n" in text and text.endswith("sys.exit(code)\n")
        assert tpl.timeout == 870


def test_the_rendered_settings_wire_both_hooks():
    blob, facts = egress(kind="dispatch", auth="passthrough", hooks=BOTH)
    facts = dataclasses.replace(facts, hook_templates=H.hook_templates_for(H.Seams(host="darwin"), blob))
    cfg = rd.render_cc_settings(blob, facts)
    assert set(cfg["hooks"]) == {"PreToolUse", "PermissionRequest"}


def test_every_rendered_slot_hook_blocks_when_it_cannot_run_and_outlives_the_hold():
    """L3 r1 codex HIGH 1: a hook that cannot START (no interpreter, no script, lost exec bit), is killed, times out or
    crashes exits with something other than 0/2, which Claude Code treats as NON-blocking unless the handler carries
    ``"onFailure": "block"`` (measured: 1017-deadline/runs2 RUN C, crash + timeout; documented for can't-start too).
    So every slot hook render writes carries it, a test template with no timeout included, and the producer's timeout
    is the adapter's gate timeout, which outlives the slot hold (300 s) plus the broker's GATE retries."""
    from levain.consent.adapters import claude_code as cc
    from levain.slot import hook_templates as HT

    blob, facts = egress(kind="dispatch", auth="passthrough", hooks=BOTH)
    facts = dataclasses.replace(facts, hook_templates=H.hook_templates_for(H.Seams(host="darwin"), blob))
    cfg = rd.render_cc_settings(blob, facts)
    handlers = [h for groups in cfg["hooks"].values() for g in groups for h in g["hooks"]]
    assert len(handlers) == 2
    for h in handlers:
        assert h["onFailure"] == "block" and h["timeout"] == HT.HOOK_TIMEOUT == cc.GATE_TIMEOUT
    # the slot gate's own worst case: GATE (6 tries, each IO-bounded, plus the backoff) then the hold
    from levain.consent import broker_client as bc
    gate_worst = (len(bc.GATE_BACKOFF) + 1) * bc.IO_TIMEOUT + sum(bc.GATE_BACKOFF)
    assert HT.HOOK_TIMEOUT > cc.DEFAULT_GATE_HOLD + gate_worst + cc.GATE_TIMEOUT_MARGIN
    # structural: a template that names no timeout still blocks on failure (Claude Code's default timeout applies)
    bare = {"levain_gate": rd.HookTemplate(b"#!/bin/sh\nexit 0\n", "PreToolUse")}
    blob2, facts2 = egress(hooks=frozenset(bare), hook_templates=bare)
    (h,) = rd.render_cc_settings(blob2, facts2)["hooks"]["PreToolUse"][0]["hooks"]
    assert h["onFailure"] == "block" and "timeout" not in h


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "870", 3601])
def test_a_template_timeout_out_of_shape_is_refused(bad):
    tmpl = {"levain_gate": rd.HookTemplate(b"x", "PreToolUse", None, bad)}
    blob, facts = egress(hooks=frozenset(tmpl), hook_templates=tmpl)
    with pytest.raises(rd.Refused) as e:
        rd.check_facts(blob, facts)
    assert e.value.name == "bad-hook-template"


def _tree(tmp_path, hook_run_src):
    pkg = tmp_path / "levain" / "slot"
    pkg.mkdir(parents=True)
    (tmp_path / "levain" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "hook_run.py").write_text(hook_run_src)


def _run_body(tmp_path, name="levain_gate"):
    text = body("darwin", name).decode()
    assert "/usr/local/lib/levain-root/current" in text      # the replace below must hit (L3 r1 deepseek LOW 4)
    script = tmp_path / "hook.py"
    script.write_text(text.replace("/usr/local/lib/levain-root/current", str(tmp_path)).split("\n", 1)[1])
    return subprocess.run([sys.executable, "-I", "-S", str(script)], input="{}", capture_output=True, text=True,
                          cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "")})


@pytest.mark.parametrize("src", ["def run(name):\n    raise RuntimeError('x')\n",
                                 "def run(name):\n    raise KeyboardInterrupt\n",
                                 "def run(name):\n    return 1\n",
                                 "def run(name):\n    return None\n"])
def test_the_body_turns_any_failure_past_the_import_into_exit_2(tmp_path, src):
    """L3 r1 complement MED 1 / gemini LOW 3 / deepseek LOW 3: ``sys.exit(run(...))`` sat outside the try, so a run
    that raised exited 1 (non-blocking on a Claude Code without onFailure). Now any outcome but 0 or 2 is 2."""
    _tree(tmp_path, src)
    p = _run_body(tmp_path)
    assert p.returncode == 2 and p.stdout == "", (p.returncode, p.stderr)
    assert "refused" in p.stderr


def test_the_body_passes_0_and_2_through(tmp_path):
    _tree(tmp_path, "import sys\ndef run(name):\n    sys.stdout.write('ok')\n    return 0\n")
    p = _run_body(tmp_path)
    assert (p.returncode, p.stdout) == (0, "ok")


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
        def main(argv, stdin=None, stdout=None, environ=None):
            raise RuntimeError("x")

    class Bad:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            stdout.write("{}")
            return 1

    class Exits:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            raise SystemExit(0)

    for adapter, why in ((Boom, "hook-adapter-failed"), (Bad, "hook-adapter-exit"), (Exits, "hook-adapter-failed")):
        code, out, err = _run(lambda a=adapter: a)
        assert code == 2 and out == "" and why in err


def test_only_the_exact_int_zero_is_success_a_zero_equal_return_is_refused():
    """L3 r2 codex MED: ``False != 0`` is False, so an adapter that regressed to return False (or 0.0, or an object
    equal to 0) with no output exited 0 and the gate allowed. Only the int 0 is an answer."""
    class ZeroEq:
        def __eq__(self, other):
            return other == 0

        def __ne__(self, other):
            return not self.__eq__(other)

        __hash__ = None  # type: ignore[assignment]

    for value in (False, 0.0, None, ZeroEq(), "0", 1):
        class Quiet:
            @staticmethod
            def main(argv, stdin=None, stdout=None, environ=None, _v=value):
                return _v

        code, out, err = _run(lambda a=Quiet: a)
        assert code == 2 and out == "" and "hook-adapter-exit" in err, (value, code, err)


def test_outside_a_slot_the_operator_path_is_never_reached():
    class Never:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            raise AssertionError("the operator path ran")

    code, out, err = _run(lambda: Never, environ={})
    assert code == 2 and "hook-not-in-slot" in err and out == ""


def test_an_answering_adapter_passes_through_unchanged():
    class Ok:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            stdout.write('{"deny": 1}\n')
            return 0

    assert _run(lambda: Ok) == (0, '{"deny": 1}\n', "")


def test_the_real_adapter_denies_a_gate_event_with_no_reachable_broker(monkeypatch):
    """No stub: the real adapter in a slot environment whose socket does not exist denies (never an allow). The
    environment is handed to ``run`` only: the process environment has NO socket variable, so an adapter that read
    ``os.environ`` itself would take the operator path (L3 r1 gemini MED 1 / deepseek MED 1 / complement LOW 4)."""
    ev = ('{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, '
          '"cwd": "/tmp", "session_id": "s1"}')
    monkeypatch.delenv("LEVAIN_QUEUE_SOCKET", raising=False)
    monkeypatch.delenv("LEVAIN_QUEUE_CAP", raising=False)
    env = {"LEVAIN_QUEUE_SOCKET": "/nonexistent/q.sock", "LEVAIN_QUEUE_CAP": "c"}
    code, out, _ = _run(None, environ=env, stdin=ev)
    assert code == 0 and '"permissionDecision": "deny"' in out


def test_the_adapter_is_handed_the_environment_run_checked():
    """One source: the mapping ``run`` checked ``in_slot`` against is the one the adapter's ``main`` gets."""
    seen = []

    class Rec:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            seen.append(environ)
            return 0

    env = {"LEVAIN_QUEUE_SOCKET": "/elsewhere/q.sock"}
    assert _run(lambda: Rec, environ=env)[0] == 0
    assert seen == [env] and seen[0] is env


def test_an_os_environ_socket_does_not_put_run_in_a_slot(monkeypatch):
    """The reverse divergence: the process environment names a socket, the mapping handed to ``run`` does not."""
    monkeypatch.setenv("LEVAIN_QUEUE_SOCKET", "/x/q.sock")

    class Never:
        @staticmethod
        def main(argv, stdin=None, stdout=None, environ=None):
            raise AssertionError("reached")

    code, out, err = _run(lambda: Never, environ={})
    assert code == 2 and "hook-not-in-slot" in err and out == ""


# ---- the codex TOML check on a Python without tomllib (L3 r1 codex HIGH 2) -----------------------------------

@pytest.fixture
def no_tomllib(monkeypatch):
    monkeypatch.setitem(sys.modules, "tomllib", None)       # `import tomllib` raises ImportError, as on 3.9


def _toml(tmp_path, text, encoding="utf-8"):
    p = tmp_path / "config.toml"
    p.write_bytes(text.encode(encoding))
    return p


def test_a_benign_codex_toml_is_not_an_attack_without_tomllib(tmp_path, no_tomllib):
    from levain.consent.adapters.claude_code import _sets_unconfined

    p = _toml(tmp_path, 'model = "gpt-5"\n[sandbox_workspace_write]\nnetwork_access = false\n')
    assert _sets_unconfined("toml", p, "LEVAIN_QUEUE_SOCKET") is False


@pytest.mark.parametrize("text", [
    'x = "LEVAIN_QUEUE_SOCKET"\n',                                 # the literal: caught on any Python
    'x = "LEVAIN_QUEUE_SOCK\\u0045T"\n',                           # an escape spells it: cannot be ruled out
    '"LEVAIN_QUEUE_\\u0053OCKET" = 1\n',                           # in a quoted key
    'x = """LEVAIN_QUEUE_\\\n    SOCKET"""\n',                      # a line-ending backslash joins it
])
def test_what_could_spell_the_variable_still_refuses_without_tomllib(tmp_path, no_tomllib, text):
    from levain.consent.adapters.claude_code import _sets_unconfined

    assert _sets_unconfined("toml", _toml(tmp_path, text), "LEVAIN_QUEUE_SOCKET") is True


def test_a_toml_with_any_escape_or_not_utf8_refuses_and_names_why_without_tomllib(tmp_path, no_tomllib, capsys):
    """Closed, not guessed: with no parser, a file holding a backslash (a benign Windows path included) or bytes that
    are not UTF-8 cannot be ruled out, and the refusal says it is the interpreter, not an attack."""
    from levain.consent.adapters.claude_code import _sets_unconfined

    assert _sets_unconfined("toml", _toml(tmp_path, 'p = "C:\\\\x"\n'), "LEVAIN_QUEUE_SOCKET") is True
    assert "TOML" in capsys.readouterr().err
    assert _sets_unconfined("toml", _toml(tmp_path, 'x = "LEVAIN_QUEUE_SOCKET"\n', "utf-16"),
                            "LEVAIN_QUEUE_SOCKET") is True


def test_with_tomllib_an_escaped_spelling_is_still_found_and_a_benign_backslash_passes(tmp_path):
    pytest.importorskip("tomllib")
    from levain.consent.adapters.claude_code import _sets_unconfined

    assert _sets_unconfined("toml", _toml(tmp_path, 'x = "LEVAIN_QUEUE_SOCK\\u0045T"\n'), "LEVAIN_QUEUE_SOCKET")
    assert not _sets_unconfined("toml", _toml(tmp_path, 'p = "C:\\\\x"\n'), "LEVAIN_QUEUE_SOCKET")


def test_the_body_runs_isolated_with_only_the_tree_on_its_path_and_fails_closed(tmp_path):
    """The script, run as a subprocess with a tree that holds no levain: exit 2 and the named refusal."""
    text = body("darwin", "levain_gate").decode().replace("/usr/local/lib/levain-root/current", str(tmp_path))
    script = tmp_path / "hook.py"
    script.write_text(text.split("\n", 1)[1])                                  # drop the shebang line
    p = subprocess.run([sys.executable, "-I", "-S", str(script)], input="{}", capture_output=True, text=True,
                       cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "")})
    assert p.returncode == 2 and "hook-adapter-unavailable" in p.stderr and p.stdout == ""


_E2E = """
import io, json, os, sys
sys.path.insert(0, {repo!r})
os.environ.update({{"LEVAIN_QUEUE_SOCKET": "/nonexistent/q.sock", "LEVAIN_QUEUE_CAP": "c"}})
from pathlib import Path
from levain.slot.hook_run import run
from levain.consent.adapters.claude_code import queue_socket_planted
cwd = {cwd!r}
res = [sys.version_info[0], sys.version_info[1], "tomllib" in sys.modules or None,
       queue_socket_planted(cwd, home=Path(os.environ["HOME"]), environ=os.environ)]
for name, event in (("levain_gate", "PreToolUse"), ("levain_queue", "PermissionRequest")):
    ev = json.dumps({{"hook_event_name": event, "tool_name": "Bash", "tool_input": {{"command": "ls"}},
                     "cwd": cwd, "session_id": "s1"}})
    o, e = io.StringIO(), io.StringIO()
    code = run(name, stdin=io.StringIO(ev), stdout=o, stderr=e)
    res += [code, "deny" in o.getvalue(), o.getvalue() == ""]
print(json.dumps(res))
"""


def _floor_python():
    """The interpreter the slot hook runs under on macOS (``ROOT_PYTHON``, 3.9.6), else this one when it is 3.9."""
    py = ROOT_PYTHON["darwin"]
    probe = subprocess.run([py, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True) if os.path.exists(py) else None
    if probe is not None and probe.returncode == 0 and probe.stdout.split()[:2] == ["3", "9"]:
        return py
    if sys.version_info[:2] == (3, 9):
        return sys.executable
    if sys.platform == "darwin":       # the hook's host: its floor interpreter must be here, never a silent skip
        pytest.fail("no Python 3.9 at %s on macOS, the interpreter the slot hook runs under" % py)
    pytest.skip("no Python 3.9 on this host (%s is %s)" % (py, probe.stdout.strip() if probe else "absent"))


def test_the_adapter_answers_on_the_floor_interpreter_the_hook_runs_under(tmp_path):
    """The hook runs under ``root_tree.ROOT_PYTHON`` with ``PY_FLAGS`` (3.9 on macOS, no tomllib), and the adapter is
    outside the root-py39 closure, so this is its floor test: import, the planted-socket scan over a repository with a
    BENIGN ``.codex/config.toml`` (L3 r1 codex HIGH 2: it read as an attack on 3.9), and both events through ``run``:
    the gate denies (no broker), the queue hook answers nothing."""
    py = _floor_python()
    repo = tmp_path / "repo"
    (repo / ".codex").mkdir(parents=True)
    (repo / ".codex" / "config.toml").write_text('model = "gpt-5"\n[sandbox_workspace_write]\nnetwork_access = false\n')
    home = tmp_path / "home"
    home.mkdir()
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([py, PY_FLAGS, "-c", _E2E.format(repo=src, cwd=str(repo))], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    assert p.returncode == 0, p.stderr
    got = __import__("json").loads(p.stdout)
    assert got == [3, 9, None, False, 0, True, False, 0, False, True], (got, p.stderr)
