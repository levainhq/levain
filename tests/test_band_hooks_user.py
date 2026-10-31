"""Q17-COLD A: `levain band hooks install|remove|status` (the opt-in user-level, band-only hooks), the band-only entry
they run, the Codex install hooks' band outside their install, the doctor row and the panel line. The hook entries are
driven as the real hooks are: a stdin JSON payload, the script (or the command as written to settings) run as a
subprocess in a scratch HOME / CLAUDE_CONFIG_DIR / CODEX_HOME / LEVAIN_HOME."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain import band_hook, doctor
from levain.cli import main
from levain.consent.questions import QuestionStore

TEMPLATES = Path(levain.__file__).parent / "templates"
SRC = Path(levain.__file__).resolve().parent.parent
HOOKS = {"claude_code": TEMPLATES / "activation" / "hooks",
         "codex": TEMPLATES / "adapters" / "codex" / "activation" / "hooks"}
SID = "sess-cold"


class Rig:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.home = tmp / "home"
        self.cfg = tmp / "claude-cfg"
        self.codex = tmp / "codex-home"
        self.lv = tmp / "lv"
        self.outside = tmp / "elsewhere" / "repo"
        for d in (self.home, self.cfg, self.codex, self.outside):
            d.mkdir(parents=True)
        self.qdir = self.lv / "consent"
        self.qdir.mkdir(parents=True, mode=0o700)
        self.qdir.chmod(0o700)
        self.env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.cfg), "CODEX_HOME": str(self.codex),
                    "LEVAIN_HOME": str(self.lv), "PATH": os.environ["PATH"], "PYTHONPATH": str(SRC)}
        for k, v in self.env.items():
            monkeypatch.setenv(k, v)
        for k in ("LEVAIN_SEAT", "LEVAIN_HOOK_SUPPRESS", "LEVAIN_SCOPE", "CLAUDE_PROJECT_DIR"):
            monkeypatch.delenv(k, raising=False)
        self.settings = self.cfg / "settings.json"

    def ask(self, what="Which retry policy?"):
        return QuestionStore(self.qdir).file(agent="claude_code", agent_session_id="other-seat", what=what,
                                             options=[("a", "yes"), ("b", "no")], rec="a", blocks="x", cwd=str(self.home))

    def make_install(self, agent="claude_code", wired=True):
        inst = self.tmp / f"install-{agent}"
        shutil.copytree(HOOKS[agent], inst / "activation" / "hooks")
        if not wired:
            for n in ("session_start.py", "user_prompt_submit.py", "stop.py"):
                p = inst / "activation" / "hooks" / n
                p.write_text(p.read_text().replace("levain.band_view", "levain.gone"))
        return inst

    def verb(self, *args, capsys=None):
        return main(["band", "hooks", *args])

    def run_entry(self, event, payload, cwd, env_more=None):
        """The band-only entry as the user-settings command runs it (the command read back from the settings file)."""
        cmd = shlex.split(band_hook.live_commands()[event])
        cmd = [sys.executable if i == 0 else ("-P" if a == "-EP" else a) for i, a in enumerate(cmd)]   # keep PYTHONPATH
        return self._run(cmd, payload, cwd, env_more)

    def run_script(self, agent, script, payload, cwd, install, env_more=None):
        cmd = [sys.executable, str(install / "activation" / "hooks" / script)]
        return self._run(cmd, payload, cwd, env_more)

    def _run(self, cmd, payload, cwd, env_more):
        env = {**self.env, **(env_more or {})}
        p = subprocess.run(cmd, input=json.dumps({"session_id": SID, "cwd": str(cwd), **payload}), capture_output=True,
                           text=True, env=env, cwd=str(cwd), timeout=60)
        return p.stdout.strip()

    def views(self):
        try:
            conn = sqlite3.connect(self.qdir / "questions.db")
            try:
                return conn.execute("SELECT COUNT(*) FROM band_views").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def ctx(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


UNRELATED = {"theme": "dark", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
                                         "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}}


# --- install / remove ----------------------------------------------------------------------------------------
def test_install_writes_the_three_hooks_and_a_receipt_and_keeps_the_operators_own(rig, capsys):
    rig.settings.write_text(json.dumps(UNRELATED, indent=2) + "\n")
    assert rig.verb("install") == 0
    doc = json.loads(rig.settings.read_text())
    assert doc["theme"] == "dark" and doc["hooks"]["PreToolUse"] == UNRELATED["hooks"]["PreToolUse"]
    ups = doc["hooks"]["UserPromptSubmit"]
    assert ups[0] == UNRELATED["hooks"]["UserPromptSubmit"][0] and len(ups) == 2
    for ev in band_hook.EVENTS:
        cmds = [h["command"] for g in doc["hooks"][ev] for h in g["hooks"] if band_hook.is_ours(h)]
        assert len(cmds) == 1 and f"-m levain.band_hook {ev}" in cmds[0]
        assert "activation" not in cmds[0] and "hookrun" not in cmds[0]        # no install script, no posture/recall
    r = json.loads((rig.lv / "band_hooks.json").read_text())
    assert r["settings"] == str(rig.settings) and r["levain_version"] == levain.__version__ and r["installed_at"]
    assert set(r["events"]) == set(band_hook.EVENTS)
    assert rig.verb("install") == 0                                           # idempotent: still one of each
    again = json.loads(rig.settings.read_text())
    assert again == doc
    out = capsys.readouterr().out
    assert "levain band hooks remove" in out


def test_remove_restores_the_settings_byte_for_byte(rig):
    for text in (json.dumps(UNRELATED, indent=2) + "\n", json.dumps(UNRELATED, indent=4), "{}\n",
                 json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}, indent=2) + "\n"):
        rig.settings.write_text(text)
        assert rig.verb("install") == 0 and rig.settings.read_text() != text
        assert rig.verb("remove") == 0
        assert rig.settings.read_text() == text
        assert not (rig.lv / "band_hooks.json").exists()


def test_remove_deletes_a_settings_file_install_created_and_touches_nothing_it_did_not_add(rig):
    assert not rig.settings.exists()
    assert rig.verb("install") == 0 and rig.settings.exists()
    assert rig.verb("remove") == 0 and not rig.settings.exists()
    # a hook the operator adds AFTER install survives remove
    rig.verb("install")
    doc = json.loads(rig.settings.read_text())
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "later"}]})
    rig.settings.write_text(json.dumps(doc, indent=2) + "\n")
    rig.verb("remove")
    left = json.loads(rig.settings.read_text())
    assert left == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "later"}]}]}}


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}'])
def test_a_settings_file_it_cannot_parse_is_refused_untouched(rig, capsys, bad):
    rig.settings.write_text(bad)
    assert rig.verb("install") == 1
    assert rig.settings.read_text() == bad and not (rig.lv / "band_hooks.json").exists()
    assert "refused" in capsys.readouterr().err
    assert rig.verb("remove") == 1 and rig.settings.read_text() == bad


def test_status_reads_the_receipt_and_the_live_settings(rig, capsys):
    assert rig.verb("status") == 0 and "not installed" in capsys.readouterr().out
    rig.verb("install")
    capsys.readouterr()
    assert rig.verb("status") == 0
    out = capsys.readouterr().out
    assert "installed" in out and str(rig.settings) in out and levain.__version__ in out
    doc = json.loads(rig.settings.read_text())
    del doc["hooks"]["Stop"]                       # live settings no longer match the receipt
    rig.settings.write_text(json.dumps(doc))
    assert rig.verb("status") == 1 and "broken" in capsys.readouterr().err


# --- the band-only entry -------------------------------------------------------------------------------------
def test_the_entry_renders_the_band_outside_any_install_and_its_stop_verifies(rig):
    rig.ask()
    rig.verb("install")
    sc = ctx(rig.run_entry("SessionStart", {"source": "startup"}, rig.outside))
    assert "NEEDS YOU band" in sc and "posture" not in sc.lower()
    band = ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hello"}, rig.outside))
    assert band.startswith("NEEDS YOU · 1 open") and "Which retry policy?" in band
    assert "wrap nudge" not in band and "[spore" not in band.lower()          # band only
    block = rig.run_entry("Stop", {"last_assistant_message": "done, nothing to add", "stop_hook_active": False}, rig.outside)
    assert json.loads(block)["decision"] == "block"                            # the reply dropped the band: reprint
    ok = rig.run_entry("Stop", {"last_assistant_message": band + "\n\ndone", "stop_hook_active": False}, rig.outside)
    assert ok == "" or json.loads(ok).get("decision") != "block"


def test_the_entry_stands_down_inside_an_install_whose_band_hooks_are_wired(rig):
    rig.ask()
    rig.verb("install")
    inst = rig.make_install(wired=True)
    inside = inst / "work" / "sub"
    inside.mkdir(parents=True)
    before = rig.views()
    assert rig.run_entry("SessionStart", {"source": "startup"}, inside) == ""
    assert rig.run_entry("UserPromptSubmit", {"prompt": "hello"}, inside) == ""
    assert rig.run_entry("Stop", {"last_assistant_message": "x", "stop_hook_active": False}, inside) == ""
    assert rig.views() == before                                              # no view was taken
    # the check can fail: the same directory with STALE band scripts is not covered, so the user-level band shows
    for n in ("session_start.py", "user_prompt_submit.py", "stop.py"):
        f = inst / "activation" / "hooks" / n
        f.write_text(f.read_text().replace("levain.band_view", "levain.gone"))
    assert "NEEDS YOU" in ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hello"}, inside))


def test_one_band_not_two_when_the_install_hook_and_the_user_hook_both_run(rig):
    rig.ask()
    rig.verb("install")
    inst = rig.make_install(wired=True)
    inside = inst / "x"
    inside.mkdir()
    own = ctx(rig.run_script("claude_code", "user_prompt_submit.py", {"prompt": "hi"}, inside, inst))
    user = ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, inside))
    assert (own + user).count("NEEDS YOU · ") == 1 and user == ""
    # and outside the install only the user-level one shows
    assert ctx(rig.run_script("claude_code", "user_prompt_submit.py", {"prompt": "hi"}, rig.outside, inst)) == ""
    assert ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside)).count("NEEDS YOU · ") == 1


def test_the_entry_is_silent_for_an_autonomous_seat_and_a_suppressed_spawn(rig):
    rig.ask()
    rig.verb("install")
    assert rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside, {"LEVAIN_SEAT": "autonomous"}) == ""
    assert rig.run_entry("SessionStart", {}, rig.outside, {"LEVAIN_SEAT": "autonomous"}) == ""
    assert rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside, {"LEVAIN_HOOK_SUPPRESS": "1"}) == ""
    assert rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside, {"LEVAIN_SEAT": "attended"}) != ""


def test_a_worker_launch_row_is_autonomous_for_the_entry(rig):
    from levain.coordinator.store import CoordStore

    rig.ask()
    rig.verb("install")
    store = CoordStore(rig.qdir)
    store.coord_set_agent("claude", model="haiku", cwd=str(rig.outside))
    d, _ = store.accept_drop("do a thing", surface="cli")
    lid = store.woken_launch(d.drop_id)
    store.launch_activate(lid, harness_session_id=SID)
    ln = store.launch_by_harness_session(SID)
    from levain import band_view

    if ln.role == "worker":
        assert rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside) == ""
    else:                                                                      # a coordinator row is attended
        assert band_view.is_autonomous(SID, rig.qdir, {}) is False
        assert rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, rig.outside) != ""


# --- Codex ---------------------------------------------------------------------------------------------------
def test_the_codex_hook_shows_the_band_in_the_coordinator_workspace_only_after_install(rig):
    rig.ask()
    inst = rig.make_install("codex")
    ws = rig.tmp / "levain-home" / "coordinator-codex"
    ws.mkdir(parents=True)
    run = lambda script, payload, cwd=ws: rig.run_script("codex", script, payload, cwd, inst)  # noqa: E731
    assert run("user_prompt_submit.py", {"prompt": "hi"}) == ""               # before opt-in: the old silence
    assert rig.run_script("codex", "session_start.py", {"source": "startup"}, ws, inst) == ""
    rig.verb("install")
    band = ctx(run("user_prompt_submit.py", {"prompt": "hi"}))
    assert band.startswith("NEEDS YOU · 1 open")
    assert "NEEDS YOU band" in ctx(run("session_start.py", {"source": "startup"}))
    block = run("stop.py", {"last_assistant_message": "nothing", "stop_hook_active": False})
    assert json.loads(block)["decision"] == "block"
    # an autonomous seat and a suppressed spawn stay silent even after install
    assert rig.run_script("codex", "user_prompt_submit.py", {"prompt": "hi"}, ws, inst, {"LEVAIN_SEAT": "autonomous"}) == ""
    assert rig.run_script("codex", "user_prompt_submit.py", {"prompt": "hi"}, ws, inst, {"LEVAIN_HOOK_SUPPRESS": "1"}) == ""
    rig.verb("remove")
    assert run("user_prompt_submit.py", {"prompt": "hi"}) == ""               # removed: silent again


def test_the_codex_band_is_not_doubled_inside_its_own_install(rig):
    rig.ask()
    inst = rig.make_install("codex")
    rig.verb("install")
    out = rig.run_script("codex", "user_prompt_submit.py", {"prompt": "hi"}, inst, inst)
    assert ctx(out).count("NEEDS YOU · ") == 1


# --- doctor and the panel line -------------------------------------------------------------------------------
def test_doctor_row_not_installed_is_info_installed_passes_and_broken_fails(rig):
    row = doctor._check_user_band_hooks()
    assert row.name == doctor.USER_BAND_ROW and row.ok and row.neutral and "levain band hooks install" in row.detail
    rig.verb("install")
    row = doctor._check_user_band_hooks()
    assert row.ok and not row.neutral and not row.warn and row.detail.startswith("installed")
    doc = json.loads(rig.settings.read_text())
    del doc["hooks"]["SessionStart"]
    rig.settings.write_text(json.dumps(doc))
    row = doctor._check_user_band_hooks()
    assert not row.ok and "broken" in row.detail and row.hint == "levain band hooks install"
    rig.verb("remove")
    rig.settings.write_text("{}")
    # an interpreter that cannot run: installed, then broken by the import probe
    assert band_hook.install(python=str(rig.tmp / "no-such-python"))[0] == 0
    row = doctor._check_user_band_hooks()
    assert not row.ok and "not a runnable interpreter" in row.detail


def test_the_panel_line_names_the_install_command_for_a_codex_workspace_outside_the_install(rig, monkeypatch, tmp_path):
    from levain.cockpit import coordinator_status as cs

    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setattr(cs, "_band_install", lambda setting: (owner, "codex"))
    monkeypatch.setattr(cs, "_band_cache", {})
    setting = {"agent": "codex", "cwd": str(rig.outside)}
    row = cs.band_row(setting)
    assert row is not None and "levain band hooks install" in str(row)
    rig.verb("install")
    monkeypatch.setattr("levain.coordinator.launch.band_failure", lambda *a, **k: None)
    assert cs.band_row(setting) is None
