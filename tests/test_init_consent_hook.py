"""S1436: `levain init` and `levain update` wire the PermissionRequest consent hook for Claude Code and Codex
through the same renderers as the other hooks, create the adapter settings file once (away mode off), and doctor
reports the wiring, away mode and the cockpit grants verify against."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain import install as inst
from levain.consent.adapters import claude_code, codex
from levain.install import refresh_adapter
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)

from tests.test_subfolder_hooks import cc_local_layout  # noqa: E402,F401  (fixture)
from tests._h4 import h4_env, h4_home

# levain's hooks live in settings.local.json in these tests: a git-root workspace's layout (test_subfolder_hooks)
pytestmark = pytest.mark.usefixtures("cc_local_layout")


def _cj(tmp_path: Path) -> Path:
    return tmp_path / "codex-home" / "hooks.json"


def _consent_groups(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["hooks"].get("PermissionRequest", [])


def _rows(results, name: str):
    return [r for r in results if r.name == name]


def _row(results, name: str):
    (r,) = _rows(results, name)
    return r


# --- the render ----------------------------------------------------------------------------------------------

def test_claude_code_init_wires_the_consent_hook(make_install):
    cc = make_install("cc")
    (group,) = _consent_groups(cc / ".claude" / "settings.local.json")
    script = cc / "activation" / "hooks" / "consent_claude_code.py"
    assert group == {"matcher": "*", "hooks": [{
        "type": "command", "command": claude_code.hookrun_command(sys.executable, script),
        "timeout": claude_code.hook_timeout(claude_code.DEFAULT_MAX_HOLD)}]}
    assert script.is_file()
    # the other hooks are untouched by the merge
    hooks = json.loads((cc / ".claude" / "settings.local.json").read_text())["hooks"]
    assert {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"} <= set(hooks)


def test_codex_init_wires_the_consent_hook_in_the_global_file(make_install, tmp_path):
    cx = make_install("cx", adapter="codex")
    (group,) = _consent_groups(_cj(tmp_path))
    script = cx / "activation" / "hooks" / "consent_codex.py"
    (handler,) = group["hooks"]
    # machine-wide: through levain.hookrun, so a gone install never blocks Codex (exit 2)
    assert handler["command"] == claude_code.hookrun_command(sys.executable, script)
    assert handler["timeout"] == claude_code.hook_timeout(claude_code.DEFAULT_MAX_HOLD)
    assert script.is_file() and not (cx / "activation" / "hooks" / "consent_claude_code.py").exists()
    assert set(json.loads(_cj(tmp_path).read_text())["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "PermissionRequest", "PreToolUse"}
    (gate,) = json.loads(_cj(tmp_path).read_text())["hooks"]["PreToolUse"]
    # the gate (Q14 B5): the same script, the ceiling's timeout, and no onFailure (Codex has none)
    assert gate["hooks"] == [{"type": "command", "command": handler["command"], "timeout": codex.GATE_TIMEOUT}]


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_init_and_update_render_the_same_bytes(make_install, tmp_path, adapter):
    install = make_install("x", adapter=adapter)
    target = install / ".claude" / "settings.local.json" if adapter == "claude-code" else _cj(tmp_path)
    before = target.read_bytes()
    r = refresh_adapter(install, apply=True, emit=lambda _l: None)
    assert r.refreshed == [] and r.review == [], r
    assert target.read_bytes() == before


def test_a_render_with_another_interpreter_is_still_levains(make_install):
    """The consent command carries the interpreter, and lives with the other hooks in settings.local.json, so
    settings.json renders the same bytes on every machine and only levain's hook entries differ (levain's own,
    decided by doctor.levain_hook_handler, not by the interpreter)."""
    cc = make_install("cc")
    root = inst._templates_root
    with root() as templates_root:
        adapter_root = templates_root / "adapters" / "claude-code"
        args = (cc, adapter_root)
        seed = [inst.SeedEntry(name="world.md", path=templates_root / "seed" / "world.md", mode="render")]
        want = inst._claude_code_files(*args, sys.executable, seed, [])[cc / ".claude" / "settings.json"]
        other = inst._claude_code_files(*args, "/opt/other/python3", seed, [])[cc / ".claude" / "settings.json"]
        hooks = inst.claude_code_local_hooks(cc, adapter_root, "/opt/other/python3")
    assert other == want
    (group,) = hooks["PermissionRequest"]
    assert group["hooks"][0]["command"].startswith("/opt/other/python3 ")
    assert all(doctor.levain_hook_handler(h, cc) for gs in hooks.values() for g in gs for h in g["hooks"])


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_the_pin_matches_after_init(make_install, adapter):
    install = make_install("x", adapter=adapter)
    (res,) = doctor._check_agent_hook_pins(install)
    assert res.ok and not res.warn and "match the pin" in res.detail, res


def test_the_codex_owner_test_still_reads_the_consent_command(make_install):
    a = make_install("a", adapter="codex")
    b = make_install("b", adapter="codex")
    assert hg.codex_hooks_owner(a) == b and hg.codex_hooks_owner(b) is None


def _make_pre_consent(cc: Path) -> Path:
    """Turn a fresh claude-code install into one made before the consent hook: no entry, no script, receipts and
    pin saying so."""
    settings = cc / ".claude" / "settings.local.json"
    doc = json.loads(settings.read_text())
    del doc["hooks"]["PermissionRequest"]
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    settings.write_text(text, encoding="utf-8")
    rec = cc.joinpath(*inst.ADAPTER_RECEIPT_REL)
    data = json.loads(rec.read_text())
    data["files"][inst.CC_LOCAL_HOOKS_KEY] = hashlib.sha256(inst._canonical_hooks(doc["hooks"])).hexdigest()
    rec.write_text(json.dumps(data))
    script = cc / "activation" / "hooks" / "consent_claude_code.py"
    script.unlink()
    act = inst.activation_receipt_path(cc)
    adata = json.loads(act.read_text())
    adata["files"].pop("hooks/consent_claude_code.py")
    act.write_text(json.dumps(adata))
    hg.repin_agent_hooks(cc)
    return settings


def test_update_wires_a_pre_consent_install_and_keeps_the_pin(make_install):
    cc = make_install("cc")
    want = (cc / ".claude" / "settings.local.json").read_bytes()
    settings = _make_pre_consent(cc)
    assert _row(doctor._check_claude_code(cc), "Claude Code consent hook").warn
    r = refresh_adapter(cc, apply=True, emit=lambda _l: None)
    assert ".claude/settings.local.json (levain's hooks)" in r.refreshed and "activation/hooks/consent_claude_code.py" in r.refreshed, r
    assert settings.read_bytes() == want
    wired = _row(doctor._check_claude_code(cc), "Claude Code consent hook")  # no key: the Q10 floor's WARN (L2-7)
    assert wired.detail.startswith("wired to this install, but no active key") and "LEVAIN_UNCONFINED=1" in wired.hint
    (res,) = doctor._check_agent_hook_pins(cc)
    assert res.ok and not res.warn, res


def test_update_stages_an_edited_settings_file_and_doctor_says_where(make_install):
    cc = make_install("cc")
    settings = _make_pre_consent(cc)
    doc = json.loads(settings.read_text())
    doc["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 5  # the operator's edit of levain's own entry
    settings.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    r = refresh_adapter(cc, apply=True, emit=lambda _l: None)
    assert ".claude/settings.local.json" in r.review, r
    assert "PermissionRequest" not in json.loads(settings.read_text())["hooks"]
    row = _row(doctor._check_claude_code(cc), "Claude Code consent hook")
    assert row.warn and "staged" in row.hint and "pending" in row.hint, row


# --- the settings file ---------------------------------------------------------------------------------------

def _settings_file(name: str) -> Path:
    return inst._consent_queue_dir() / name


def test_init_creates_the_settings_file_away_off(make_install):
    cc = make_install("cc")
    path = _settings_file(claude_code.SETTINGS_NAME)
    assert json.loads(path.read_text()) == {"cockpit_data_root": os.path.realpath(cc / ".levain" / "cockpit"),
                                            "away_mode": False, "away_word": "auto", "away_hold_seconds": 0}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_init_never_rewrites_an_existing_settings_file(make_install, capsys, tmp_path):
    path = _settings_file(codex.SETTINGS_NAME)
    path.parent.mkdir(mode=0o700, parents=True)
    mine = {"cockpit_data_root": "/somewhere/else/cockpit", "away_mode": True, "away_hold_seconds": 15}
    path.write_text(json.dumps(mine), encoding="utf-8")
    lines: list[str] = []
    cx = make_install("cx", adapter="codex")
    inst._write_consent_settings(cx, "codex", lines.append)
    assert json.loads(path.read_text()) == mine
    assert any("name another cockpit" in ln and "kept" in ln for ln in lines), lines
    rows = doctor._check_codex(cx)
    auth = _row(rows, "Codex consent authority")
    assert not auth.warn and "nothing is answered by a grant" in auth.detail, auth  # no cockpit database anywhere
    away = _row(rows, "Codex away mode")
    assert away.warn and "NO dialog during the hold" in away.detail and "15s" in away.detail, away
    other = tmp_path / "other" / "cockpit"
    other.mkdir(parents=True)
    (other / "cockpit.db").write_bytes(b"")
    path.write_text(json.dumps({**mine, "cockpit_data_root": str(other)}), encoding="utf-8")
    auth = _row(doctor._check_codex(cx), "Codex consent authority")
    assert auth.warn and "not this install's" in auth.detail, auth


def test_a_settings_file_naming_no_cockpit_is_kept_and_reported(make_install):
    path = _settings_file(claude_code.SETTINGS_NAME)
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text('{"away_mode": false}', encoding="utf-8")
    cc = make_install("cc")
    lines: list[str] = []
    inst._write_consent_settings(cc, "claude-code", lines.append)
    assert path.read_text() == '{"away_mode": false}'
    assert any("no cockpit_data_root" in ln for ln in lines), lines
    assert "names no cockpit" in _row(doctor._check_claude_code(cc), "Claude Code consent authority").detail


def test_a_symlinked_settings_file_is_refused_not_followed(make_install, tmp_path):
    path = _settings_file(claude_code.SETTINGS_NAME)
    path.parent.mkdir(mode=0o700, parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    path.symlink_to(elsewhere)
    cc = make_install("cc")
    lines: list[str] = []
    inst._write_consent_settings(cc, "claude-code", lines.append)
    assert not elsewhere.exists() and path.is_symlink()
    assert any("not written" in ln for ln in lines), lines


def test_a_group_readable_queue_dir_is_refused(make_install):
    qdir = inst._consent_queue_dir()
    qdir.mkdir(mode=0o755, parents=True)
    os.chmod(qdir, 0o755)
    cc = make_install("cc")
    lines: list[str] = []
    inst._write_consent_settings(cc, "claude-code", lines.append)
    assert not (qdir / claude_code.SETTINGS_NAME).exists()
    assert any("not written" in ln and "0700" in ln for ln in lines), lines


# --- the version gate: always rendered, WARN when untested ------------------------------------------------------

@pytest.mark.parametrize("version,tested", [(None, False), ("9.9.9", False), ("2.1.301", True)])
def test_init_reports_the_agent_version_and_renders_anyway(make_install, monkeypatch, capsys, version, tested):
    monkeypatch.setattr(inst, "_consent_agent_version", lambda adapter: version)
    lines: list[str] = []
    cc = make_install("cc")
    inst._emit_consent_hook(cc, "claude-code", lines.append)
    (line,) = lines
    assert line.startswith("  consent hook (Claude Code): wired; ")
    assert ("is a tested version" in line) is tested and ("WARN" in line) is (not tested), line
    assert _consent_groups(cc / ".claude" / "settings.local.json")


def test_codex_init_says_trust_and_machine_wide(make_install, monkeypatch):
    lines: list[str] = []
    cx = make_install("cx", adapter="codex")
    inst._emit_consent_hook(cx, "codex", lines.append)
    assert any("writes Codex's trust" in ln and "every Codex session on this machine" in ln for ln in lines), lines


# --- doctor --------------------------------------------------------------------------------------------------

def test_doctor_rows_on_a_fresh_install(make_install):
    cc = make_install("cc")
    rows = doctor._check_claude_code(cc)
    hook = _row(rows, "Claude Code consent hook")  # a fresh install has no key: the Q10 floor's WARN (L2-7)
    assert hook.warn and "refused outright outside default mode" in hook.detail
    away = _row(rows, "Claude Code away mode")
    assert not away.warn and away.detail.startswith("follows input idle:"), away
    auth = _row(rows, "Claude Code consent authority")
    assert not auth.warn and "no cockpit.db there yet" in auth.detail, auth
    (Path(os.path.realpath(cc / ".levain" / "cockpit"))).mkdir(parents=True, exist_ok=True)
    (cc / ".levain" / "cockpit" / "cockpit.db").write_bytes(b"")
    assert "no cockpit.db" not in _row(doctor._check_claude_code(cc), "Claude Code consent authority").detail


def test_doctor_claude_away_on_is_not_a_warning(make_install):
    cc = make_install("cc")
    path = _settings_file(claude_code.SETTINGS_NAME)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "away_mode": True, "away_word": "on", "away_hold_seconds": 5000}))
    away = _row(doctor._check_claude_code(cc), "Claude Code away mode")
    assert not away.warn and "on, hold 840s" in away.detail and "dialog during the hold" in away.detail, away


def test_a_codex_install_that_does_not_own_the_file_gets_no_consent_rows(make_install):
    a = make_install("a", adapter="codex")
    make_install("b", adapter="codex")
    assert not _rows(doctor._check_codex(a), "Codex consent hook")


def test_canary_findings_cover_the_init_shape(make_install):
    from levain.consent.canary import doctor_findings
    cc = make_install("cc")
    names = [f.name for f in doctor_findings(cc)]
    assert names == ["consent hook (Claude Code)"], names  # the version row (no version in tests: a WARN)
    settings = cc / ".claude" / "settings.local.json"
    doc = json.loads(settings.read_text())
    del doc["hooks"]["PermissionRequest"]
    settings.write_text(json.dumps(doc))
    assert doctor_findings(cc) == []  # the wiring row says it; the static script alone says nothing


def test_canary_findings_ask_trust_for_the_global_codex_hook(make_install, tmp_path):
    from levain.consent.canary import doctor_findings
    cx = make_install("cx", adapter="codex")
    found = {f.name: f for f in doctor_findings(cx, codex_home=tmp_path / "codex-home")}
    assert set(found) == {"consent hook (Codex)", "consent hook trust (Codex)"}, found
    assert "consent hook script (Codex)" not in found  # the static script is not install()'s rendering
    st = codex.trust_status(cx, home=tmp_path / "codex-home", codex_bin="/nonexistent",
                            hooks_path=_cj(tmp_path), script=cx / "activation" / "hooks" / "consent_codex.py")
    assert st.status == "unknown" and st.script_intact is None
    assert st.key is not None and st.key.startswith(f"{os.path.realpath(_cj(tmp_path))}:permission_request:")


# --- the static scripts --------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel,module", [
    ("templates/activation/hooks/consent_claude_code.py", "levain.consent.adapters.claude_code"),
    ("templates/adapters/codex/activation/hooks/consent_codex.py", "levain.consent.adapters.codex")])
def test_the_static_scripts_carry_the_isolate_prelude(rel, module):
    text = (Path(inst.__file__).parent / rel).read_text(encoding="utf-8")
    assert claude_code.ISOLATE_PRELUDE in text
    assert f"    from {module} import main\n" in text and text.endswith("sys.exit(main([]))\n")
    assert "{{" not in text  # nothing for the activation copy to substitute


# --- update creates the settings file; --consent-hook off ----------------------------------------------------

def test_update_creates_a_missing_settings_file_create_only(make_install, capsys):
    cc = make_install("cc")
    path = _settings_file(claude_code.SETTINGS_NAME)
    path.unlink()
    refresh_adapter(cc, apply=True, emit=print)
    assert json.loads(path.read_text())["cockpit_data_root"] == os.path.realpath(cc / ".levain" / "cockpit")
    assert "consent settings:" in capsys.readouterr().out
    path.write_text(json.dumps({"cockpit_data_root": "/elsewhere", "away_mode": True, "away_hold_seconds": 5}))
    refresh_adapter(cc, apply=True, emit=lambda _l: None)
    assert json.loads(path.read_text())["cockpit_data_root"] == "/elsewhere"


def test_update_of_an_install_whose_codex_file_another_holds_writes_no_settings(make_install):
    a = make_install("a", adapter="codex")
    make_install("b", adapter="codex")
    path = _settings_file(codex.SETTINGS_NAME)
    path.unlink()
    refresh_adapter(a, apply=True, emit=lambda _l: None)
    assert not path.exists()


def test_update_writes_no_codex_settings_when_the_live_file_is_an_edited_copy_of_another_install_s(make_install,
                                                                                                    tmp_path):
    """L3 r1 codex HIGH: an edited hooks.json still running install B's consent hook has no owner by the receipt
    test, and "not another install's" was read as "this install's": updating A named A's cockpit for B's hook."""
    a = make_install("a", adapter="codex")
    make_install("b", adapter="codex")
    cj = _cj(tmp_path)
    doc = json.loads(cj.read_text())
    doc["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": "echo hand-added"}]})
    cj.write_text(json.dumps(doc))
    assert hg.codex_hooks_owner(a) is None  # the premise: an edit leaves no owner
    path = _settings_file(codex.SETTINGS_NAME)
    path.unlink()
    refresh_adapter(a, apply=True, emit=lambda _l: None)
    assert not path.exists()
    assert not inst._codex_runs_this_consent_hook(a.resolve())


def test_update_writes_codex_settings_when_the_live_file_runs_this_install_s_hook(make_install):
    """The control for the test above: the one Codex install whose consent hook the live hooks.json runs gets its
    missing settings file back from `levain update`, naming its own cockpit."""
    a = make_install("a", adapter="codex")
    assert inst._codex_runs_this_consent_hook(a.resolve())
    path = _settings_file(codex.SETTINGS_NAME)
    path.unlink()
    refresh_adapter(a, apply=True, emit=lambda _l: None)
    assert json.loads(path.read_text())["cockpit_data_root"] == os.path.realpath(a / ".levain" / "cockpit")


def _init(tmp_path, capsys, name, adapter, **kw):
    from tests.test_init_answers import _filled

    af = tmp_path / f"{name}-answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    rc = inst.run_init(tmp_path / name, adapter, answers_file=af, **kw)
    out = capsys.readouterr().out
    return rc, tmp_path / name, out


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_consent_hook_off_wires_nothing_and_update_keeps_it_off(make_install, tmp_path, capsys, adapter):
    rc, install, out = _init(tmp_path, capsys, "x", adapter, force=False, consent_hook="off")
    assert rc == 0, out
    target = install / ".claude" / "settings.local.json" if adapter == "claude-code" else _cj(tmp_path)
    assert _consent_groups(target) == []
    assert json.loads((install / ".levain" / "config.json").read_text())["consent_hook"] == "off"
    assert "consent hook: off" in out
    for name in (claude_code.SETTINGS_NAME, codex.SETTINGS_NAME):
        assert not _settings_file(name).exists()
    before = target.read_bytes()
    r = refresh_adapter(install, apply=True, emit=lambda _l: None)
    assert r.review == [], r
    assert target.read_bytes() == before
    label = "Claude Code" if adapter == "claude-code" else "Codex"
    checks = doctor._check_claude_code(install) if adapter == "claude-code" else doctor._check_codex(install)
    row = _row(checks, f"{label} consent hook")
    assert row.ok and not row.warn and "off" in row.detail, row
    (pin,) = doctor._check_agent_hook_pins(install)
    assert pin.ok and not pin.warn, pin
    # and back on, explicitly
    rc, _, out = _init(tmp_path, capsys, "x", adapter, force=True, consent_hook="on")
    assert rc == 0, out
    assert len(_consent_groups(target)) == 1
    assert json.loads((install / ".levain" / "config.json").read_text())["consent_hook"] == "on"


def test_init_force_without_the_flag_keeps_off(make_install, tmp_path, capsys):
    rc, install, _ = _init(tmp_path, capsys, "x", "claude-code", force=False, consent_hook="off")
    assert rc == 0
    rc, _, out = _init(tmp_path, capsys, "x", "claude-code", force=True)
    assert rc == 0, out
    assert _consent_groups(install / ".claude" / "settings.local.json") == []


def test_a_bad_consent_hook_value_is_refused_before_anything_is_written(make_install, tmp_path, capsys):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code", force=False, consent_hook="maybe")
    assert rc == 1 and "--consent-hook must be one of" in out
    assert not install.exists()


@pytest.mark.parametrize("shape", ["plain", "flags", "hookrun"])
def test_every_levain_consent_shape_is_ours_and_lookalikes_are_not(tmp_path, shape):
    script = tmp_path / "activation" / "hooks" / "consent_codex.py"
    cmd = {"plain": f"/usr/bin/python3 {script}", "flags": claude_code.hook_command("/usr/bin/python3", script),
           "hookrun": claude_code.hookrun_command("/usr/bin/python3", script)}[shape]
    assert claude_code._is_ours({"command": cmd}, "other", script)
    for bad in (f"/usr/bin/python3 -m levain.hookrun {script}", f"/usr/bin/python3 -EP -m evil {script}",
                f"/usr/bin/python3 -EP -m levain.hookrun {script}x"):
        assert not claude_code._is_ours({"command": bad}, "other", script), bad


# --- L1/L2 r1 fixes ------------------------------------------------------------------------------------------

def test_a_moved_claude_install_s_consent_command_falls_through(make_install, tmp_path):
    import subprocess

    cc = make_install("cc")
    (group,) = _consent_groups(cc / ".claude" / "settings.local.json")
    cmd = group["hooks"][0]["command"]
    os.rename(cc, tmp_path / "moved")
    r = subprocess.run(["/bin/sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout) == (0, ""), r


@pytest.mark.parametrize("name", ["consent_claude_code.py", "consent_codex.py", "user_prompt_submit.py"])
def test_a_write_to_an_install_s_hook_scripts_is_agent_config(tmp_path, name):
    target = tmp_path / "inst" / "activation" / "hooks" / name
    assert claude_code.targets_agent_config("Write", {"file_path": str(target), "content": "x"}, str(tmp_path / "inst"),
                                            home=tmp_path / "home", environ=h4_env())
    patch = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch\n"
    assert codex.targets_agent_config("apply_patch", {"command": patch}, str(tmp_path / "inst"),
                                      home=tmp_path / "home", codex_dir=tmp_path / "codex-home", environ=h4_env())


@pytest.mark.parametrize("mod", [claude_code, codex])
def test_away_off_with_no_cockpit_files_nothing(tmp_path, mod):
    from levain.consent.queue import ConsentQueue, authority_source

    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    queue = ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), home=h4_home())
    cockpit = tmp_path / "cockpit"
    settings = mod.AdapterSettings(away_mode=False, away_hold_seconds=0, cockpit_data_root=str(cockpit))
    event = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": str(tmp_path), "session_id": "s1"}
    import sqlite3

    def rows() -> int:
        if not (qdir / "queue.db").exists():
            return 0
        with sqlite3.connect(qdir / "queue.db") as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'requests'").fetchone():
                return 0
            return c.execute("SELECT count(*) FROM requests").fetchone()[0]

    assert mod.decide(event, queue, settings, home=tmp_path / "home", environ=h4_env()) is None
    assert rows() == 0
    # control: away mode on files the request (and, with nothing to answer it, withdraws it)
    away = mod.AdapterSettings(away_mode=True, away_hold_seconds=0, cockpit_data_root=None)
    assert mod.decide(event, queue, away, home=tmp_path / "home", environ=h4_env()) is None
    assert rows() == 1
    cockpit.mkdir()
    (cockpit / "cockpit.db").write_bytes(b"")
    assert claude_code.nothing_can_decide(settings) is True  # a database that does not replay verifies nothing


def test_a_cockpit_with_no_key_and_no_grant_files_nothing(tmp_path):
    """L3 r1 codex MED: a cockpit.db the cockpit made before any key was enrolled is not something that can decide,
    so a default install files no request (each kept for good, a shell command's secrets unmasked); one enrolled key
    makes it one."""
    import sqlite3
    from datetime import datetime, timezone

    from datetime import timedelta

    import levain.authority as auth
    from levain.authority import ReplayCache
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.firejournal import CockpitDB
    from tests.test_consent_cc_adapter import Key

    root = tmp_path / "cockpit"
    root.mkdir()
    with CockpitDB(root).read():
        pass
    settings = claude_code.AdapterSettings(away_mode=False, away_hold_seconds=0, cockpit_data_root=str(root))
    assert (root / "cockpit.db").is_file()
    assert claude_code.nothing_can_decide(settings) is True
    key = Key(tmp_path, "laptop")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    auth.sign_and_append(conn, {"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key,
                                "class": "laptop-presence", "custody": "passphrase"},
                         signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE),
                         clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
    ws = Path(os.path.realpath(tmp_path)) / "ws"
    ws.mkdir()
    auth.sign_and_append(conn, {"op": "create_grant", "grant": {
        "id": "g1", "kind": "consent", "agent": "codex", "tools": ["apply_patch"], "workspace": str(ws),
        "postures": ["default"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=1))}},
        signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE),
        clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
    conn.close()
    assert claude_code.nothing_can_decide(settings) is False
    # the stored shape the predicate reads: a real consent grant replays as one in force
    from levain.consent.queue import authority_source
    snap = authority_source(root / "cockpit.db", anchor=None)()
    assert claude_code._a_consent_grant_in_force(snap.policy.grants.values()) is True


def test_only_an_unexpired_consent_grant_is_one_that_can_decide():
    """L3 r2 codex MED: with no active key, an expired grant or a grant of another kind answers nothing."""
    from datetime import datetime, timedelta, timezone

    from levain.cockpit.consent import utc_stamp

    later = utc_stamp(datetime.now(timezone.utc) + timedelta(days=1))
    earlier = utc_stamp(datetime.now(timezone.utc) - timedelta(days=1))
    f = claude_code._a_consent_grant_in_force
    assert f([{"kind": "consent", "expires_at": later}]) is True
    assert f([{"kind": "consent", "expires_at": earlier}]) is False
    assert f([{"verb": "fire", "expires_at": later}]) is False
    assert f([{"kind": "consent", "expires_at": "not a stamp"}, "junk"]) is False
    assert f([]) is False


@pytest.mark.parametrize("body,expected", [
    (None, "on"), ("{}", "on"), ('{"consent_hook": "on"}', "on"), ('{"consent_hook": "off"}', "off"),
    ('{"consent_hook": "Off"}', "off"), ('{"consent_hook": "of', "off"), ("[]", "off"),
])
def test_a_damaged_consent_hook_record_reads_as_off(tmp_path, body, expected):
    """L3 r1 codex HIGH: a config that may hold the operator's opt-out is never read as the hook switched on; only
    a missing file or key is the default."""
    install = tmp_path / "i"
    (install / ".levain").mkdir(parents=True)
    if body is not None:
        (install / ".levain" / "config.json").write_text(body, encoding="utf-8")
    assert inst.persisted_consent_hook(install) == expected
