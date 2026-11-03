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


BAND_SCRIPTS = ("session_start.py", "user_prompt_submit.py", "stop.py")
# the line of each band script that actually reaches the band; each script also names levain.band_view in a comment
BAND_CALL = {"session_start.py": "from levain.band_view import hook_contract",
             "user_prompt_submit.py": "from levain.band_view import hook_prompt",
             "stop.py": "from levain.band_view import hook_stop"}


class Rig:
    def __init__(self, tmp: Path, monkeypatch, capsys):
        self.tmp = tmp
        self.capsys = capsys
        from levain import host_guard

        monkeypatch.setattr(host_guard, "load_managed_settings", lambda platform=None: [])
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
        for k in ("LEVAIN_SEAT", "LEVAIN_HOOK_SUPPRESS", "LEVAIN_SCOPE", "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_ENTRYPOINT"):
            monkeypatch.delenv(k, raising=False)
        self.settings = self.cfg / "settings.json"
        # an interpreter that imports this checkout's levain when started as the user hooks start it (-EP drops
        # PYTHONPATH, and the test interpreter may only find this checkout through it)
        self.py = tmp / "hook-python"
        self.py.write_text(f'#!/bin/sh\nif [ "$1" = "-EP" ]; then shift; set -- -P "$@"; fi\n'
                           f'PYTHONPATH={shlex.quote(str(SRC))} exec {shlex.quote(sys.executable)} "$@"\n')
        self.py.chmod(0o755)

    def ask(self, what="Which retry policy?"):
        return QuestionStore(self.qdir).file(agent="claude_code", agent_session_id="other-seat", what=what,
                                             options=[("a", "yes"), ("b", "no")], rec="a", blocks="x", cwd=str(self.home))

    def make_install(self, agent="claude_code"):
        """A real install (``levain init`` with an answer file): its hooks registered where init puts them."""
        from levain.install import run_init
        from tests.test_init_answers import _filled

        inst = self.tmp / f"install-{agent}"
        af = self.tmp / f"{agent}-answers.json"
        self.capsys.readouterr()                                # _filled reads the template from captured output
        af.write_text(json.dumps(_filled(self.capsys)), encoding="utf-8")
        assert run_init(inst, "codex" if agent == "codex" else "claude-code", force=False, answers_file=af) == 0
        self.capsys.readouterr()
        return inst

    @staticmethod
    def cut_band_calls(inst):
        """Each band script with its real call taken out and its comments kept (an edit, or an older release)."""
        for n in BAND_SCRIPTS:
            f = inst / "activation" / "hooks" / n
            text = f.read_text()
            assert BAND_CALL[n] in text
            name = BAND_CALL[n].rsplit(" ", 1)[1]
            f.write_text(text.replace(BAND_CALL[n], f"{name} = lambda *a, **k: None"))
            assert "levain.band_view" in f.read_text()

    def verb(self, *args, capsys=None):
        return main(["band", "hooks", *args])

    def run_entry(self, event, payload, cwd, env_more=None):
        """The band-only entry as the user-settings command runs it (the command read back from the settings file)."""
        cmd = shlex.split(band_hook.live_commands()[event])
        cmd = cmd[:cmd.index(event) + 1]                                       # the shell tail is the shell's
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
def rig(tmp_path, monkeypatch, capsys):
    return Rig(tmp_path, monkeypatch, capsys)


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
    assert band_hook.install(python=str(rig.py))[0] == 0
    assert rig.verb("status") == 0
    out = capsys.readouterr().out
    assert "installed" in out and str(rig.settings) in out and levain.__version__ in out
    doc = json.loads(rig.settings.read_text())
    del doc["hooks"]["Stop"]                       # live settings no longer match the receipt
    rig.settings.write_text(json.dumps(doc))
    assert rig.verb("status") == 1 and "broken" in capsys.readouterr().err


def _set_prompt_timeout(settings, value):
    doc = json.loads(settings.read_text())
    for g in doc["hooks"]["UserPromptSubmit"]:
        for h in g["hooks"]:
            if band_hook.is_ours(h):
                if value is None:
                    h.pop("timeout", None)
                else:
                    h["timeout"] = value
    settings.write_text(json.dumps(doc, indent=2) + "\n")


def _prompt_timeouts(settings):
    doc = json.loads(settings.read_text())
    return [h.get("timeout") for g in doc["hooks"]["UserPromptSubmit"] for h in g["hooks"] if band_hook.is_ours(h)]


def test_an_install_from_before_signing_keeps_its_short_timeout_until_reinstalled_and_status_names_it(rig, capsys):
    """TERMSIGN L2 r1 (complement MED): the timeout is written into settings.json at install, so an install made
    while it was 10 s keeps 10 s. Status (and so doctor's row) names it; install, re-run or by `levain update`,
    rewrites it, idempotently."""
    assert band_hook.install(python=str(rig.py))[0] == 0
    assert _prompt_timeouts(rig.settings) == [band_hook.PROMPT_HOOK_TIMEOUT_S]
    _set_prompt_timeout(rig.settings, 10)                                     # what an earlier release wrote
    state, detail = band_hook.user_band_state(probe=False)
    assert state == "broken" and "timeout 10" in detail and "levain band hooks install" in detail
    assert doctor._check_user_band_hooks().ok is False
    assert rig.verb("install") == 0
    assert _prompt_timeouts(rig.settings) == [band_hook.PROMPT_HOOK_TIMEOUT_S]
    once = rig.settings.read_text()
    assert rig.verb("install") == 0 and rig.settings.read_text() == once      # idempotent
    assert band_hook.user_band_state(probe=False)[0] == "installed"
    _set_prompt_timeout(rig.settings, 10)
    assert "re-written" in band_hook.refresh_after_update()                   # `levain update` rewrites it too
    assert _prompt_timeouts(rig.settings) == [band_hook.PROMPT_HOOK_TIMEOUT_S]
    _set_prompt_timeout(rig.settings, None)                                   # none named: the harness's default
    assert band_hook.user_band_state(probe=False)[0] == "installed"
    capsys.readouterr()


def test_a_symlinked_settings_file_stays_a_symlink_and_its_target_is_edited(rig):
    dot = rig.tmp / "dotfiles"
    dot.mkdir()
    target = dot / "claude-settings.json"
    original = json.dumps(UNRELATED, indent=2) + "\n"
    target.write_text(original)
    target.chmod(0o640)
    rig.settings.symlink_to(target)
    assert rig.verb("install") == 0
    assert rig.settings.is_symlink() and os.readlink(rig.settings) == str(target)
    assert set(band_hook.live_commands()) == set(band_hook.EVENTS)
    assert "levain.band_hook" in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o640
    assert json.loads((rig.lv / "band_hooks.json").read_text())["settings"] == os.path.realpath(target)
    assert sorted(p.name for p in dot.iterdir()) == [target.name]            # no temp file left beside it
    assert sorted(p.name for p in rig.cfg.iterdir()) == ["settings.json"]
    assert rig.verb("remove") == 0
    assert rig.settings.is_symlink() and os.readlink(rig.settings) == str(target)
    assert target.read_text() == original and target.stat().st_mode & 0o777 == 0o640


def test_install_twice_then_remove_leaves_what_was_there(rig):
    assert not rig.settings.exists()
    for _ in range(2):
        assert rig.verb("install") == 0
    assert json.loads((rig.lv / "band_hooks.json").read_text())["created_file"] is True
    assert rig.verb("remove") == 0 and not rig.settings.exists()            # never a {} the operator did not have
    for text in ("{}\n", json.dumps(UNRELATED, indent=2) + "\n"):
        rig.settings.write_text(text)
        for _ in range(2):
            assert rig.verb("install") == 0
        assert rig.verb("remove") == 0 and rig.settings.read_text() == text


def test_remove_also_cleans_the_current_settings_file_when_the_receipt_names_another(rig, monkeypatch):
    assert rig.verb("install") == 0
    old = rig.settings
    moved = rig.tmp / "claude-cfg-2"
    moved.mkdir()
    doc = json.loads(old.read_text())
    doc["theme"] = "light"
    (moved / "settings.json").write_text(json.dumps(doc, indent=2) + "\n")     # the operator copied the file across
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    assert rig.verb("remove") == 0
    assert not old.exists()                                                   # the receipt's file: install created it
    assert json.loads((moved / "settings.json").read_text()) == {"theme": "light"}


def test_the_command_is_quiet_when_its_interpreter_or_module_is_gone(rig):
    gone = rig.tmp / "venv-moved" / "bin" / "python"
    no_module = rig.tmp / "bare" / "python"
    no_module.parent.mkdir()
    no_module.write_text("#!/bin/sh\necho \"No module named levain\" >&2\nexit 1\n")
    no_module.chmod(0o755)
    for py in (gone, no_module):
        cmd = band_hook.command_for(str(py), "UserPromptSubmit")
        assert band_hook.is_ours({"type": "command", "command": cmd})          # still found by remove
        p = subprocess.run(["/bin/sh", "-c", cmd], input='{"session_id": "x"}', capture_output=True, text=True,
                           env=rig.env, cwd=str(rig.outside), timeout=30)
        assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
        # doctor still FAILS the row: the interpreter is probed, not trusted
        assert band_hook.install(python=str(py))[0] == 0
        row = doctor._check_user_band_hooks()
        assert not row.ok and row.hint == "levain band hooks install"
        assert ("not a runnable interpreter" if py == gone else "cannot import levain.band_hook") in row.detail
    # the check can fail: the command without its tail is the notice Claude Code would show
    bare = band_hook.command_for(str(gone), "UserPromptSubmit").replace(band_hook.QUIET_TAIL, "")
    p = subprocess.run(["/bin/sh", "-c", bare], input="{}", capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and p.stderr


def test_update_rewrites_the_recorded_interpreter_only_when_installed(rig):
    assert band_hook.refresh_after_update() is None and not rig.settings.exists()     # not opted in: untouched
    assert band_hook.install(python="/old/venv/bin/python")[0] == 0
    line = band_hook.refresh_after_update()
    assert line and sys.executable in line
    r = json.loads((rig.lv / "band_hooks.json").read_text())
    assert r["python"] == sys.executable and r["created_file"] is True
    assert all(shlex.split(c)[0] == sys.executable for c in band_hook.live_commands().values())
    assert "/old/venv" not in rig.settings.read_text()


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


def test_the_entry_stands_down_for_a_session_started_at_an_install_whose_band_is_wired(rig):
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    inside = inst / "work" / "sub"
    inside.mkdir(parents=True)
    at = {"CLAUDE_PROJECT_DIR": str(inst)}                     # started at the install, working below it
    before = rig.views()
    assert rig.run_entry("SessionStart", {"source": "startup"}, inside, at) == ""
    assert rig.run_entry("UserPromptSubmit", {"prompt": "hello"}, inside, at) == ""
    assert rig.run_entry("Stop", {"last_assistant_message": "x", "stop_hook_active": False}, inside, at) == ""
    assert rig.views() == before                                              # no view was taken
    # the check can fail: the same session with band scripts whose real call is gone is not covered
    rig.cut_band_calls(inst)
    assert "NEEDS YOU" in ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hello"}, inside, at))


def test_one_band_not_two_when_the_install_hook_and_the_user_hook_both_run(rig):
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    inside = inst / "x"
    inside.mkdir()
    at = {"CLAUDE_PROJECT_DIR": str(inst)}
    own = ctx(rig.run_script("claude_code", "user_prompt_submit.py", {"prompt": "hi"}, inside, inst, at))
    user = ctx(rig.run_entry("UserPromptSubmit", {"prompt": "hi"}, inside, at))
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


def _coordinator_row(rig, sid):
    from levain.coordinator.store import CoordStore

    store = CoordStore(rig.qdir)
    store.coord_set_agent("claude", model="haiku", cwd=str(rig.outside))
    d, _ = store.accept_drop("do a thing", surface="cli")
    store.launch_activate(store.woken_launch(d.drop_id), harness_session_id=sid)
    assert store.launch_by_harness_session(sid).role == "coordinator"


def _worker_row(rig, sid):
    from levain.coordinator.store import CoordStore, Part

    store = CoordStore(rig.qdir)
    store.coord_set_agent("none")
    token = store.coord_take()
    drop = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(drop.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id=sid)
    assert why is None and store.launch_by_harness_session(sid).role == "worker"


def _three(rig, sid, env_more=None, cwd=None):
    """SessionStart, UserPromptSubmit and a Stop whose reply dropped the band, in one session, as Claude Code runs them."""
    cwd = rig.outside if cwd is None else cwd
    return [rig.run_entry("SessionStart", {"session_id": sid, "source": "startup"}, cwd, env_more),
            rig.run_entry("UserPromptSubmit", {"session_id": sid, "prompt": "hi"}, cwd, env_more),
            rig.run_entry("Stop", {"session_id": sid, "last_assistant_message": "nothing", "stop_hook_active": False},
                          cwd, env_more)]


def test_a_worker_launch_row_is_autonomous_for_the_entry(rig):
    rig.ask()
    rig.verb("install")
    assert "NEEDS YOU" in ctx(_three(rig, "w-1")[1])           # the same seat with no row is shown the band
    _worker_row(rig, "w-1")
    assert _three(rig, "w-1") == ["", "", ""]
    assert _three(rig, "w-1", {"CLAUDE_CODE_ENTRYPOINT": "cli"}) == ["", "", ""]   # the row decides, not the entrypoint


def test_a_headless_session_without_a_launch_row_is_left_alone(rig):
    rig.ask()
    rig.verb("install")
    before = rig.views()
    assert _three(rig, "h-1", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) == ["", "", ""]
    assert rig.views() == before                                  # no view was taken
    out = _three(rig, "h-2", {"CLAUDE_CODE_ENTRYPOINT": "cli"})  # interactive or --bg: the band
    assert "NEEDS YOU band" in ctx(out[0]) and ctx(out[1]).startswith("NEEDS YOU · 1 open")
    assert json.loads(out[2])["decision"] == "block"
    _coordinator_row(rig, "c-1")                                  # a coordinator Levain launched headless: the band
    out = _three(rig, "c-1", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert "NEEDS YOU" in ctx(out[1])
    assert band_hook.stands_down("h-1", {**rig.env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) is True
    assert band_hook.stands_down("h-1", {**rig.env, "CLAUDE_CODE_ENTRYPOINT": ""}) is False   # absent: as before
    assert band_hook.stands_down("c-1", {**rig.env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) is False


# --- "is this install's band wired?" has one answer (doctor.install_band_wiring) ----------------------------------
def _registrations_out(inst, events=band_hook.EVENTS):
    """Take this install's band registrations for ``events`` out of its project settings; the scripts stay."""
    f = inst / ".claude" / "settings.json"
    doc = json.loads(f.read_text())
    for ev in events:
        doc["hooks"][ev] = [g for g in doc["hooks"][ev]
                            if not any(n in json.dumps(g) for n in BAND_SCRIPTS)]
    f.write_text(json.dumps(doc, indent=2))


def test_a_session_started_in_a_subfolder_loads_none_of_the_install_hooks_so_the_user_band_shows(rig):
    # complement #6: Claude Code reads a project's .claude/settings.json from the folder a session starts in only;
    # this install keeps its hooks there (not a git root), so a session started in inst/work runs none of them
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    assert json.loads((inst / ".claude" / "settings.json").read_text())["hooks"]["UserPromptSubmit"]
    proj = inst / "work"
    proj.mkdir()
    out = _three(rig, "p-1", {"CLAUDE_PROJECT_DIR": str(proj)})
    assert "NEEDS YOU band" in ctx(out[0]) and ctx(out[1]).startswith("NEEDS YOU · 1 open")
    assert _three(rig, "p-2", {"CLAUDE_PROJECT_DIR": str(inst)}, cwd=proj) == ["", "", ""]   # started at the install
    assert "NEEDS YOU" in ctx(_three(rig, "p-3", {"CLAUDE_PROJECT_DIR": str(rig.home)})[1])  # no install at all
    assert doctor.install_band_wiring(inst, proj).state == "absent"
    assert doctor.install_band_wiring(inst).state == "wired"


def test_a_subfolder_session_is_covered_when_the_install_hooks_sit_in_the_git_roots_local_file(rig, monkeypatch):
    # ... and where they sit in .claude/settings.local.json at the repository root, Claude Code loads them for a
    # session started anywhere below it (host_guard.cc_project_settings_files): there the install shows the band
    from levain import host_guard

    monkeypatch.setattr(host_guard, "cc_levain_hooks_file",
                        lambda ws: Path(os.path.abspath(ws)) / ".claude" / "settings.local.json")
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    (inst / ".git").mkdir()
    local = json.loads((inst / ".claude" / "settings.local.json").read_text())
    assert any(str(inst) in json.dumps(g) for g in local["hooks"]["UserPromptSubmit"])     # absolute script paths
    proj = inst / "work"
    proj.mkdir()
    assert _three(rig, "l-1", {"CLAUDE_PROJECT_DIR": str(proj)}) == ["", "", ""]
    assert doctor.install_band_wiring(inst, proj).state == "wired"


def test_scripts_present_but_registrations_removed_is_not_wired(rig):
    # codex #4: the scripts on disk are no evidence the hooks run
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    at = {"CLAUDE_PROJECT_DIR": str(inst)}
    assert _three(rig, "r-1", at) == ["", "", ""]
    _registrations_out(inst)
    assert all((inst / "activation" / "hooks" / n).is_file() for n in BAND_SCRIPTS)
    assert "NEEDS YOU" in ctx(_three(rig, "r-2", at)[1])
    row = doctor._check_band_hooks(inst, "claude_code")
    assert not row.ok and row.detail.startswith("absent") and row.hint == "levain update"
    w = doctor.install_band_wiring(inst)
    assert w.state == "absent" and "no SessionStart, UserPromptSubmit or Stop hook" in w.detail


def test_a_script_whose_band_call_is_gone_but_whose_comment_remains_is_not_wired(rig):
    # codex #5: a script that only mentions levain.band_view in a comment never renders the band
    rig.ask()
    rig.verb("install")
    inst = rig.make_install()
    rig.cut_band_calls(inst)
    assert "NEEDS YOU" in ctx(_three(rig, "c-1", {"CLAUDE_PROJECT_DIR": str(inst)})[1])
    row = doctor._check_band_hooks(inst, "claude_code")
    assert not row.ok and row.detail.startswith("stale") and row.hint == "levain update"
    w = doctor.install_band_wiring(inst)
    assert w.state == "stale" and "differ from the package" in w.detail


def test_a_missing_user_prompt_submit_registration_fails_doctor(rig):
    # codex #3: doctor once passed the row when it found no interpreter to probe
    inst = rig.make_install()
    assert doctor._check_band_hooks(inst, "claude_code").ok
    _registrations_out(inst, ["UserPromptSubmit"])
    row = doctor._check_band_hooks(inst, "claude_code")
    assert not row.ok and "UserPromptSubmit" in row.detail and row.hint == "levain update"
    w = doctor.install_band_wiring(inst)
    assert w.state == "stale" and w.detail.startswith("UserPromptSubmit is not registered")


def test_a_codex_install_band_is_wired_inside_it_and_absent_outside_it_unless_global(rig):
    inst = rig.make_install("codex")
    assert doctor.install_band_wiring(inst, None, "codex", rig.codex).state == "wired"
    assert doctor.install_band_wiring(inst, inst / "sub", "codex", rig.codex).state == "wired"
    w = doctor.install_band_wiring(inst, rig.outside, "codex", rig.codex)
    assert w.state == "absent" and "outside" in w.detail
    (inst / ".levain" / "config.json").write_text(json.dumps({"scope": "global"}))
    assert doctor.install_band_wiring(inst, rig.outside, "codex", rig.codex).state == "wired"


# --- the headless rule -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry,silent", [("claude-vscode", False), ("claude-desktop", False), ("cli", False),
                                          ("", False), ("sdk-cli", True), ("sdk-ts", True), ("sdk-py", True)])
def test_only_an_sdk_entrypoint_stands_down(rig, entry, silent):
    assert band_hook.stands_down("e-1", {**rig.env, "CLAUDE_CODE_ENTRYPOINT": entry}) is silent


def test_an_ide_session_is_shown_the_band(rig):
    rig.ask()
    rig.verb("install")
    out = _three(rig, "ide-1", {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"})
    assert ctx(out[1]).startswith("NEEDS YOU · 1 open")


# --- Codex ---------------------------------------------------------------------------------------------------
def test_the_codex_hook_shows_the_band_in_the_coordinator_workspace_only_after_install(rig):
    rig.ask()
    inst = rig.make_install("codex")
    ws = rig.tmp / "levain-home" / "codex-workspace"
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
    assert band_hook.install(python=str(rig.py))[0] == 0
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


# --- round 2: remove and install never lose the operator's file, and say what they could not do ------------------
def test_remove_leaves_a_symlink_made_after_install_and_never_deletes_its_target(rig):
    assert not rig.settings.exists()
    assert rig.verb("install") == 0
    assert json.loads((rig.lv / "band_hooks.json").read_text())["created_file"] is True
    dot = rig.tmp / "dotfiles"
    dot.mkdir()
    target = dot / "claude.json"
    target.write_text(rig.settings.read_text())          # the operator moves the file into dotfiles and links it back
    rig.settings.unlink()
    rig.settings.symlink_to(target)
    assert rig.verb("remove") == 0
    assert target.is_file() and json.loads(target.read_text()) == {}
    assert rig.settings.is_symlink() and os.readlink(rig.settings) == str(target)


@pytest.mark.parametrize("text", ["", "   \n", "\n\n", "\t"])
def test_a_blank_settings_file_comes_back_byte_for_byte(rig, text):
    rig.settings.write_bytes(text.encode())
    for _ in range(2):
        assert rig.verb("install") == 0
    assert rig.verb("remove") == 0
    assert rig.settings.read_bytes() == text.encode()


@pytest.fixture
def locked():
    """chmod a directory read-only for the test, and back."""
    if os.geteuid() == 0:
        pytest.skip("chmod does not stop root from writing")
    held = []

    def lock(d):
        d.chmod(0o500)
        held.append(d)

    yield lock
    for d in held:
        d.chmod(0o700)


def test_install_puts_the_settings_back_when_the_receipt_cannot_be_written(rig, capsys, locked):
    original = (json.dumps(UNRELATED, indent=2) + "\n").encode()
    rig.settings.write_bytes(original)
    locked(rig.lv)
    assert rig.verb("install") == 1
    err = capsys.readouterr().err
    assert "receipt" in err and "could not be written" in err and "put back as it was" in err
    assert "Traceback" not in err
    assert rig.settings.read_bytes() == original and not (rig.lv / "band_hooks.json").exists()
    rig.settings.unlink()                                # a file install would have created is not left behind
    assert rig.verb("install") == 1 and not rig.settings.exists()


def test_install_into_a_settings_directory_it_cannot_write_is_a_refusal(rig, capsys, locked):
    locked(rig.cfg)
    assert rig.verb("install") == 1
    err = capsys.readouterr().err
    assert "cannot be written" in err and "Traceback" not in err and not (rig.lv / "band_hooks.json").exists()


def test_remove_that_cannot_write_the_settings_fails_and_keeps_the_receipt(rig, capsys, locked):
    rig.settings.write_text(json.dumps(UNRELATED, indent=2) + "\n")
    assert rig.verb("install") == 0
    capsys.readouterr()
    locked(rig.cfg)
    assert rig.verb("remove") == 1
    err = capsys.readouterr().err
    assert "could not be written" in err and "receipt" in err and "Traceback" not in err
    assert (rig.lv / "band_hooks.json").exists() and "levain.band_hook" in rig.settings.read_text()


def test_remove_says_when_the_receipt_cannot_be_removed(rig, capsys, locked):
    assert rig.verb("install") == 0
    capsys.readouterr()
    locked(rig.lv)
    assert rig.verb("remove") == 1
    err = capsys.readouterr().err
    assert "could not be removed" in err and str(rig.lv / "band_hooks.json") in err
    assert not rig.settings.exists()                     # the settings file was still cleaned


def test_remove_cleans_one_file_when_the_other_does_not_parse(rig, monkeypatch, capsys):
    rig.settings.write_text(json.dumps(UNRELATED, indent=2) + "\n")
    assert rig.verb("install") == 0
    moved = rig.tmp / "claude-cfg-2"
    moved.mkdir()
    (moved / "settings.json").write_text("{not json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    capsys.readouterr()
    assert rig.verb("remove") == 1
    err = capsys.readouterr().err
    assert str(moved / "settings.json") in err and "refused" in err
    assert json.loads(rig.settings.read_text()) == UNRELATED                     # the receipt's file was cleaned
    assert (moved / "settings.json").read_text() == "{not json"


def test_update_refreshes_the_receipts_settings_file_and_never_writes_a_new_one(rig, monkeypatch):
    assert band_hook.install(python="/old/venv/bin/python")[0] == 0
    moved = rig.tmp / "claude-cfg-2"
    moved.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    line = band_hook.refresh_after_update()
    assert not (moved / "settings.json").exists()
    assert str(rig.settings) in line and str(moved / "settings.json") in line and "levain band hooks remove" in line
    assert "/old/venv" not in rig.settings.read_text() and sys.executable in rig.settings.read_text()
    assert json.loads((rig.lv / "band_hooks.json").read_text())["settings"] == str(rig.settings)


def test_the_probe_starts_python_as_the_hook_does(rig):
    # an interpreter that finds levain only through PYTHONPATH: the hook's -E drops it, so the band never imports
    nosite = rig.tmp / "nosite-python"
    nosite.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n')
    nosite.chmod(0o755)
    assert os.environ["PYTHONPATH"] == str(SRC)
    assert doctor._band_import_problem(str(nosite)) is None                  # started as the probe once started it
    assert band_hook.install(python=str(nosite))[0] == 0
    state, detail = band_hook.user_band_state()
    assert state == "broken" and f"{nosite} -EP cannot import levain.band_hook" in detail
    assert band_hook.install(python=str(rig.py))[0] == 0
    assert band_hook.user_band_state()[0] == "installed"


# --- round 3 ---------------------------------------------------------------------------------------------------
def _band_handlers(doc, event, script):
    return [h for g in doc["hooks"][event] for h in g["hooks"] if script in h.get("command", "")]


@pytest.mark.parametrize("change", [{"if": "Bash(*)"}, {"type": "prompt"}, {"type": "http"}, {"type": None}],
                         ids=["if-conditioned", "prompt-type", "http-type", "no-type"])
def test_a_band_handler_claude_code_would_not_run_is_not_wired(rig, change):
    # Claude Code skips a handler with an `if` on these events, and only a "command" handler runs a script: a
    # registration whose command names the script is not enough
    inst = rig.make_install()
    assert doctor.install_band_wiring(inst).state == "wired"
    f = inst / ".claude" / "settings.json"
    doc = json.loads(f.read_text())
    handlers = _band_handlers(doc, "UserPromptSubmit", "user_prompt_submit.py")
    assert handlers
    for h in handlers:
        h.update(change)
        if h["type"] is None:
            del h["type"]
    f.write_text(json.dumps(doc, indent=2))
    w = doctor.install_band_wiring(inst)
    assert w.state == "stale" and w.detail.startswith("UserPromptSubmit is not registered")
    assert not doctor._check_band_hooks(inst, "claude_code").ok


def test_a_codex_install_wired_through_a_project_hooks_file_stands_the_user_band_down(rig, capsys):
    # the install is found through the project's .codex/hooks.json, so that is the file its wiring is read from
    from levain.band_view import session_band_state

    inst = rig.make_install("codex")
    user = rig.codex / "hooks.json"
    project = inst / ".codex" / "hooks.json"
    project.parent.mkdir(exist_ok=True)
    project.write_text(user.read_text())
    user.write_text("{}")
    assert session_band_state("codex", str(inst), rig.env) == "wired"
    assert band_hook.inside_wired_install("codex", str(inst), rig.env)
    rig.ask()
    assert band_hook.install(python=str(rig.py))[0] == 0
    capsys.readouterr()
    band_hook.codex_standalone("UserPromptSubmit", {"session_id": "pj-1", "cwd": str(inst), "prompt": "hi"}, rig.env)
    assert capsys.readouterr().out == ""                                   # one band: the install's own


def test_two_threads_inside_band_probe_once_keep_their_own_results(rig, monkeypatch):
    import threading

    runs = []

    def fake_probe(python, flags, modules):
        runs.append(python)
        return f"result {len(runs)}"

    monkeypatch.setattr(doctor, "_run_band_probe", fake_probe)
    a_in, b_done = threading.Event(), threading.Event()
    seen = {}

    def a():
        with doctor.band_probe_once():
            seen["a1"] = doctor._band_import_problem("/py")
            a_in.set()
            b_done.wait(10)
            seen["a2"] = doctor._band_import_problem("/py")

    def b():
        a_in.wait(10)
        with doctor.band_probe_once():
            seen["b1"] = doctor._band_import_problem("/py")
            seen["b2"] = doctor._band_import_problem("/py")
        b_done.set()

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert seen == {"a1": "result 1", "a2": "result 1", "b1": "result 2", "b2": "result 2"}
    with doctor.band_probe_once():                           # this thread had none open: its own, fresh
        assert doctor._band_import_problem("/py") == "result 3"
    assert doctor._band_import_problem("/py") == "result 4"  # outside any block nothing is kept

