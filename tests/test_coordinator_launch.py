"""Starting a launch's session, and watching it (C3b; C0 §3.4, §7 L2 L8 L9). Built from what C3b's runs measured on
Claude Code 2.1.305 (1020+50): the trust refusal, by realpath; the spawn, the first agents row with no pid and no
status, stop; --bg ignoring --session-id. A fake ``claude`` on PATH replays those outputs; no test here ever spawns
the real one."""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.coordinator import launch as cl
from levain.coordinator import lease
from levain.coordinator.store import CoordStore

HID = "2be178ea"
SID = "2be178ea-7208-46be-8ef7-5b0e49a354e0"

# the fake replays the measured outputs (Claude Code 2.1.305); its mode and its log sit next to it
_FAKE = r'''#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
mode = (here / "mode").read_text().strip()
state = here / "state.json"
with open(here / "log.jsonl", "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\n")
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("2.1.305 (Claude Code)"); sys.exit(0)
if args[:1] == ["--bg"]:
    name = args[args.index("-n") + 1]
    if mode == "trust":
        print(f"Workspace not trusted. Run `claude` in {{os.path.realpath(os.getcwd())}} once and accept the trust "
              "prompt, then retry.")
        sys.exit(1)
    if mode == "unknown":
        print("error: the harness fell over\nsecond line"); sys.exit(2)
    if mode == "slow":
        time.sleep(0.5)
    if mode != "never":
        state.write_text(json.dumps({{"name": name, "cwd": os.getcwd(), "reads": 0}}))
    print(f"backgrounded · {HID} · {{name}}")
    print(f"  claude attach {HID}    open in this terminal")
    sys.exit(0)
if args == ["agents", "--json"]:
    if (here / "agents_fail").exists():
        print("error: could not reach the sessions", file=sys.stderr); sys.exit(1)
    extra = json.loads((here / "others.json").read_text()) if (here / "others.json").exists() else []
    if not state.exists():
        print(json.dumps(extra)); sys.exit(0)
    if (here / "miss_once").exists():             # one partial list: our row missing from this read only
        (here / "miss_once").unlink()
        print(json.dumps(extra)); sys.exit(0)
    s = json.loads(state.read_text())
    cwd = (here / "row_cwd").read_text() if (here / "row_cwd").exists() else s["cwd"]
    row = {{"id": "{HID}", "cwd": cwd, "kind": "background", "startedAt": 1792552347854,
           "sessionId": "{SID}", "name": s["name"], "state": "working"}}
    if s["reads"]:
        row.update(pid=54960, status="busy")      # the first row right after the spawn has neither
    if (here / "done").exists():                  # a finished turn stays listed (run_c3b_first_b)
        row.update(pid=54960, status="idle", state="done")
    s["reads"] += 1
    state.write_text(json.dumps(s))
    print(json.dumps(extra + [row], indent=2)); sys.exit(0)
if args[:1] == ["stop"]:
    if (here / "stop_noop").exists():             # the stop is accepted, the row stays listed
        print(f"stopped {{args[1]}}"); sys.exit(0)
    if (here / "fail_after_stop").exists():       # the list cannot be read once the stop has run
        (here / "agents_fail").write_text("")
    state.unlink(missing_ok=True)
    print(f"stopped {{args[1]}}"); sys.exit(0)
print("unexpected", args); sys.exit(3)
'''


class Fake:
    def __init__(self, d: Path) -> None:
        self.dir = d

    def mode(self, m: str) -> None:
        (self.dir / "mode").write_text(m)

    def calls(self) -> list[dict]:
        p = self.dir / "log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def spawns(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:1] == ["--bg"]]

    def stops(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:1] == ["stop"]]

    def others(self, rows: list[dict]) -> None:
        (self.dir / "others.json").write_text(json.dumps(rows))

    def flag(self, name: str, text: str = "") -> None:
        (self.dir / name).write_text(text)

    def gone(self) -> None:
        (self.dir / "state.json").unlink()


# a session of yours Levain did not start: the list is never empty unless a test empties it
OTHER = {"id": "77aa0011", "sessionId": "77aa0011-0000-4000-8000-000000000000", "name": "my own session",
         "cwd": "/elsewhere", "kind": "interactive", "state": "working", "status": "busy"}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "claude"
    exe.write_text(_FAKE.format(python=sys.executable, HID=HID, SID=SID))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cl, "READBACK_DELAY", 0.0)
    monkeypatch.setattr(cl, "UNLISTED_RECHECK", 0.0)
    f = Fake(d)
    f.mode("ok")
    f.others([OTHER])
    return f


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture(autouse=True)
def _levain_home(tmp_path, monkeypatch):
    """The coordinator record a launch reads and writes is this test's own."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))


def _coordinator_dir(d):
    """``d`` as `levain coord agent` leaves it: the role hint in its own config, and in the coordinator record."""
    from levain.coordinator import door

    (d / ".levain").mkdir(parents=True, exist_ok=True)
    (d / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
    door.record(d)
    return d


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "coord-install"
    d.mkdir()
    return _coordinator_dir(d)


@pytest.fixture
def live_hook(monkeypatch):
    """The hook check passes (the real check against a real install is test_coord_agent_sets_up_the_install)."""
    monkeypatch.setattr(cl, "hook_live", lambda d, **kw: (True, "wired to this install"))


@pytest.fixture
def store(qdir, workdir):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    return s


def woken(store, words="fix the build"):
    d, _ = store.accept_drop(words, surface="cli")
    launch_id = store.woken_launch(d.drop_id)
    assert launch_id is not None
    return launch_id


def rows(qdir, sql, *args):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# --- run_c3b_first_b: the spawn, the read back by name, activation ---------------------------------------------
def test_a_woken_launch_starts_and_reads_its_ids_back_by_name(store, fake, live_hook, qdir, workdir):
    launch_id = woken(store, "please fix the flaky test")
    got = cl.launch_session(store, launch_id)
    assert got.started and got.attach == f"claude attach {HID}" and got.harness_session_id == SID, got
    ln = store.launch(launch_id)
    assert ln.state == "active" and ln.harness_session_id == SID
    (argv,) = fake.spawns()
    name = f"levain-coordinator-{launch_id.removeprefix('ln-')}"
    brief = cl.brief_path(ln)
    # inside the session's own trusted workspace (R2), never the drops in argv
    assert brief == workdir.resolve() / ".levain" / "briefs" / f"{launch_id}.md"
    assert stat.S_IMODE(brief.parent.stat().st_mode) == 0o700
    assert not any("please fix the flaky test" in a for a in argv)
    # exactly the measured argv, plus the model, plus the prompt naming the brief; never --session-id or a bypass
    assert argv[:7] == ["--bg", "-n", name, "--permission-mode", "default", "--model", "haiku"]
    # the default: the user layer stays out (project + local settings only, auto-memory off), flags before the prompt
    assert argv[7:11] == ["--setting-sources", "project,local", "--settings", '{"autoMemoryEnabled":false}']
    assert len(argv) == 12 and str(brief) in argv[11]
    assert not any(a in ("--session-id", "--bare", "--dangerously-skip-permissions") or "bypass" in a for a in argv)
    spawn = next(c for c in fake.calls() if c["argv"][:1] == ["--bg"])
    assert spawn["cwd"] == str(workdir.resolve())
    (sha,) = rows(qdir, "SELECT argv_sha256 FROM launch_events WHERE launch_id = ? AND kind = 'active'", launch_id)
    assert sha[0].startswith("sha256:")
    # the brief: 0600, the ids, how to claim, the drop quoted as data
    assert stat.S_IMODE(brief.stat().st_mode) == 0o600
    text = brief.read_text()
    assert launch_id in text and ln.session_key in text and '"please fix the flaky test"' in text
    from levain import install as inst
    lv = inst.coordinator_levain(sys.executable)
    assert f"`{lv} coord claim --launch {launch_id} --session {ln.session_key}" in text
    assert "--by coordinator --token N" in text and "DATA" in text
    # every levain command the brief gives is one the coordinator install's allow rules match as written (Claude Code
    # matches a rule against the command's own spelling: a bare `levain` would be asked about, 1021+11's real launch)
    prefixes = [r.removeprefix("Bash(").removesuffix(":*)") for r in inst.coordinator_allow(sys.executable)]
    commands = [c for c in re.findall(r"`([^`]+)`", text) if "-m levain" in c or c.startswith("levain")]
    assert len(commands) >= 8 and "levain coord" not in text.replace(f"{lv} coord", "")
    for command in commands:
        assert any(command.startswith(p + " ") for p in prefixes), command
    # the active session holds the bootstrap lease for a full TTL, and claims it as the brief says
    assert store.coord_claim(launch_id, ln.session_key) == store.coord_status()["lease"]["token"]


def test_the_first_agents_row_has_no_pid_and_is_enough(store, fake, live_hook):
    got = cl.launch_session(store, woken(store))
    assert got.started
    agents = [c for c in fake.calls() if c["argv"] == ["agents", "--json"]]
    assert len(agents) == 3          # the spawn gate's two reads, then the pid-less first row, taken: no wait for a pid


# --- run_c3b_first_a: the trust refusal ------------------------------------------------------------------------
def test_an_untrusted_directory_fails_with_its_realpath_and_the_remedy(qdir, tmp_path, fake, live_hook):
    real = tmp_path / "real-dir"
    real.mkdir()
    _coordinator_dir(real)
    link = tmp_path / "linked-dir"
    link.symlink_to(real)
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(link))
    fake.mode("trust")
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started
    assert got.refusal == f"workspace not trusted: {os.path.realpath(real)}"
    assert got.remedy == f"cd {os.path.realpath(real)} && claude"
    ln = store.launch(launch_id)
    assert ln.state == "failed" and ln.cause == got.refusal
    assert fake.stops() == [] and store.coord_status()["lease"] is None          # the bootstrap claim is released


def test_another_failure_records_the_exit_and_first_line(store, fake, live_hook):
    fake.mode("unknown")
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and got.remedy is None
    assert got.refusal == "claude --bg exited 2: error: the harness fell over"
    assert store.launch(launch_id).state == "failed"


def test_a_session_never_listed_fails(store, fake, live_hook):
    fake.mode("never")
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "started no session listed" in got.refusal
    # the gate's two reads, the read back's tries, then one more list to stop anything under its name (nothing was listed)
    assert len([c for c in fake.calls() if c["argv"] == ["agents", "--json"]]) == 2 + cl.READBACK_TRIES + 1
    assert "no session named" in got.refusal and fake.stops() == []
    assert store.launch(launch_id).state == "failed"


def test_three_failed_launches_stop_the_wakes(store, fake, live_hook, monkeypatch):
    # a failed launch is a death (C3a): the backoff, then "until you look", hold for launches that do start a process
    fake.mode("unknown")
    t = [datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_clock", lambda: t[0])
    for i in range(lease.MAX_DEATHS):
        assert not cl.launch_session(store, woken(store, f"drop {i}")).started
        t[0] = t[0].replace(hour=t[0].hour + 1)
    assert store.coord_status()["wake"] == "stopped"


# --- the one-spawner rule and the lease ------------------------------------------------------------------------
def test_two_racing_launch_calls_spawn_once(qdir, store, fake, live_hook):
    fake.mode("slow")
    launch_id = woken(store)
    out: list = []
    racers = [threading.Thread(target=lambda: out.append(cl.launch_session(CoordStore(qdir), launch_id)))
              for _ in range(2)]
    for r in racers:
        r.start()
    for r in racers:
        r.join(timeout=60)
    assert len(fake.spawns()) == 1
    assert sorted(o.started for o in out) == [False, True]
    loser = next(o for o in out if not o.started)
    assert "already being started" in loser.refusal or "not pending" in loser.refusal
    assert rows(qdir, "SELECT launch_id, pid FROM launch_spawns") == [(launch_id, os.getpid())]
    assert store.launch(launch_id).state == "active"


def test_a_spawn_row_is_never_replaced(qdir, store, fake, live_hook):
    launch_id = woken(store)
    assert cl.launch_session(store, launch_id).started
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        for sql in ("UPDATE launch_spawns SET pid = 1", "DELETE FROM launch_spawns",
                    f"INSERT OR REPLACE INTO launch_spawns VALUES ('{launch_id}', 99999, 1, 'x')"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
    finally:
        c.close()


def test_the_lease_lost_between_spawn_and_activate_stops_the_session(store, fake, live_hook, monkeypatch):
    launch_id = woken(store)
    real = cl._read_back

    def read_back_then_take(*a, **kw):
        got = real(*a, **kw)
        store.coord_take()          # you took the role while the session was starting
        return got

    monkeypatch.setattr(cl, "_read_back", read_back_then_take)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "not pending" in got.refusal and f"stopped {HID}" in got.refusal
    assert fake.stops() == [["stop", HID]]
    assert store.launch(launch_id).state == "failed"


def test_a_launch_no_longer_pending_spawns_nothing(store, fake, live_hook):
    launch_id = woken(store)
    store.coord_take()                                   # voids every pending launch
    got = cl.launch_session(store, launch_id)
    assert not got.started and "not pending" in got.refusal
    assert fake.spawns() == []


def test_a_directory_whose_hook_is_not_live_refuses(qdir, tmp_path, fake, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _coordinator_dir(plain)
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(plain.resolve()))
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "the consent hook is not live" in got.refusal and "no CLAUDE.md" in got.refusal
    # G7: checked before the spawn claim, so nothing is written: the launch stays pending, unclaimed
    assert f"launch {launch_id} left pending" in got.refusal
    assert fake.spawns() == [] and store.launch(launch_id).state == "pending"
    assert rows(qdir, "SELECT count(*) FROM launch_spawns") == [(0,)]
    # fixed while it is still pending, the same launch starts
    monkeypatch.setattr(cl, "hook_live", lambda d, **kw: (True, "wired to this install"))
    assert cl.launch_session(store, launch_id).started


def test_a_coordinator_is_never_launched_into_an_unmarked_reused_install(qdir, tmp_path, fake, live_hook):
    """Reproduced (coordint/tests_new_on_merged.txt): a setting naming a live install with no coordinator record and
    no role (written before `coord agent` refused one, or straight into the store) started a coordinator there, where
    the door is open."""
    own = tmp_path / "own-install"
    own.mkdir()
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(own.resolve()))
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and got.refusal.startswith("review item: ") and "not a coordinator's workspace" in got.refusal
    assert f"launch {launch_id} left pending" in got.refusal
    assert fake.spawns() == [] and store.launch(launch_id).state == "pending"
    # a workspace rendered before the record (the role hint only) is recorded at its launch and starts
    (own / ".levain").mkdir()
    (own / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
    assert cl.launch_session(store, launch_id).started
    from levain.coordinator import door
    assert str(own.resolve()) in door.recorded()


# --- the setting -----------------------------------------------------------------------------------------------
def test_with_no_setting_a_drop_only_queues(qdir, fake):
    store = CoordStore(qdir)
    d, _ = store.accept_drop("words for later", surface="cli")
    assert store.woken_launch(d.drop_id) is None
    st = store.coord_status()
    assert st["wake"] == "unset" and st["launches"] == [] and st["lease"] is None
    store.coord_set_agent("none")
    store.accept_drop("more words", surface="cli")
    assert store.coord_status()["wake"] == "unset" and store.coord_status()["launches"] == []
    assert rows(qdir, "SELECT count(*) FROM launches") == [(0,)] and fake.calls() == []


def test_the_setting_is_yours_and_append_only(qdir, workdir):
    store = CoordStore(qdir)
    with pytest.raises(lease.LeaseRefused, match="absolute path"):          # codex runs in a directory too (C3c)
        store.coord_set_agent("codex", cwd="relative")
    with pytest.raises(lease.LeaseRefused, match="absolute path"):
        store.coord_set_agent("claude", cwd="relative")
    with pytest.raises(lease.LeaseRefused, match="no --model"):
        store.coord_set_agent("none", model="haiku")
    store.coord_set_agent("claude", model="haiku", cwd=str(workdir))
    store.coord_set_agent("none")
    assert store.coord_setting().agent == "none"
    assert rows(qdir, "SELECT agent, by FROM coord_settings ORDER BY seq") == [("claude", "operator"),
                                                                              ("none", "operator")]
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        for sql in ("UPDATE coord_settings SET agent = 'claude'", "DELETE FROM coord_settings",
                    "INSERT INTO coord_settings (gseq, agent, model, cwd, by, at) "
                    "VALUES (99999, 'claude', NULL, '/x', 'coordinator', 'x')"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
    finally:
        c.close()


def test_a_wake_asks_for_the_set_agent_model_and_directory(store, workdir):
    launch_id = woken(store)
    ln = store.launch(launch_id)
    assert (ln.harness, ln.model, ln.cwd, ln.role) == ("claude", "haiku", str(workdir.resolve()), "coordinator")


# --- the CLI ---------------------------------------------------------------------------------------------------
def _main(qdir, capsys, *argv, rc=0):
    from levain.cli import main

    got = main([*argv, "--queue-dir", str(qdir)])
    out = capsys.readouterr()
    assert got == rc, (argv, out.out, out.err)
    return out


def test_levain_drop_starts_the_coordinator_its_drop_woke(store, qdir, fake, live_hook, capsys):
    out = _main(qdir, capsys, "drop", "--no-source", "hello", "coordinator")
    assert "drop receipt:" in out.out and f"started: claude attach {HID}" in out.err
    # the next drop finds the lease held and starts nothing
    out = _main(qdir, capsys, "drop", "--no-source", "a second drop")
    assert "started" not in out.err and len(fake.spawns()) == 1


def test_levain_drop_still_exits_0_when_the_launch_is_refused(store, qdir, fake, live_hook, capsys):
    fake.mode("trust")
    out = _main(qdir, capsys, "drop", "--no-source", "hello")
    assert "not started: workspace not trusted" in out.err and "&& claude" in out.err


def test_levain_coord_launch_starts_the_pending_coordinator(store, qdir, fake, live_hook, capsys):
    store.accept_drop("queued, launch by hand", surface="cli")          # asked for, not started
    out = _main(qdir, capsys, "coord", "launch")
    assert out.out.strip() == f"claude attach {HID}"
    out = _main(qdir, capsys, "coord", "launch", rc=1)
    assert "no coordinator launch is pending" in out.err


def test_levain_coord_launch_prints_the_refusal_and_remedy(store, qdir, fake, live_hook, capsys):
    fake.mode("trust")
    launch_id = woken(store)
    out = _main(qdir, capsys, "coord", "launch", launch_id, rc=1)
    assert "workspace not trusted" in out.out and "&& claude" in out.out


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """A tmp HOME for `levain coord agent`'s install, as test_repin_on_install's make_install sets one up; claude's
    version is never read from the real binary."""
    from levain import host_guard as hg
    from levain.consent import canary

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    monkeypatch.setattr(canary, "agent_version", lambda agent, **_: None)
    return home


def test_coord_agent_sets_up_the_install(tmp_home, qdir, fake, capsys):
    from levain import doctor

    out = _main(qdir, capsys, "coord", "agent", "claude", "--model", "haiku")
    target = (tmp_home / ".levain" / "coordinator").resolve()
    assert (target / "CLAUDE.md").is_file()
    (hook,) = [r for r in doctor._check_claude_code(target) if r.name == cl.HOOK_ROW]
    assert hook.detail.startswith("wired to this install"), hook
    # the last line is the trust remedy, literally
    assert out.out.strip().splitlines()[-1] == f"cd {target} && claude"
    s = CoordStore(qdir).coord_setting()
    assert (s.agent, s.model, s.cwd) == ("claude", "haiku", str(target))
    assert not (tmp_home / ".claude.json").exists()
    # a second run finds the install live and only records the setting again
    _main(qdir, capsys, "coord", "agent", "claude")
    assert CoordStore(qdir).coord_setting().model is None
    # and its sessions launch there, with doctor's real check
    d, _ = CoordStore(qdir).accept_drop("first real-check drop", surface="cli")
    got = cl.launch_session(CoordStore(qdir), CoordStore(qdir).pending_coordinator().launch_id)
    assert got.started, got
    assert next(c for c in fake.calls() if c["argv"][:1] == ["--bg"])["cwd"] == str(target)


def test_the_coordinator_install_pre_approves_its_own_three_verbs_and_a_reused_install_gets_none(
        tmp_home, qdir, tmp_path, capsys):
    # np-6843ca07 (a), signed: the coordinator's own install pre-approves only levain coord / drops / thread
    from levain import install as inst
    from levain.cli import main as levain_main

    def allow(d):
        return json.loads((d / ".claude" / "settings.json").read_text())["permissions"]["allow"]

    _main(qdir, capsys, "coord", "agent", "claude")
    target = (tmp_home / ".levain" / "coordinator").resolve()
    bash = [r for r in allow(target) if r.startswith("Bash(")]
    lv = inst.coordinator_levain(sys.executable)
    assert lv == f"{shlex.quote(sys.executable)} -P -m levain"
    assert bash == [f"Bash({lv} coord:*)", f"Bash({lv} drops:*)", f"Bash({lv} thread:*)"]
    # the brief reads the spelling back from the install itself (a launch from another interpreter tells the session
    # what THIS install allows), and an install from before the interpreter was named keeps its bare levain
    assert inst.installed_coordinator_levain(target, sys.executable) == lv
    # np-be8ebe0a (a): Levain's own project MCP servers, and only those, are approved for the background session
    settings = json.loads((target / ".claude" / "settings.json").read_text())
    mcp = json.loads((target / ".mcp.json").read_text())["mcpServers"]
    assert settings["enabledMcpjsonServers"] == list(inst.COORDINATOR_MCP_SERVERS) == sorted(mcp)
    assert inst.persisted_role(target) == "coordinator"
    assert levain_main(["update", "--path", str(target)]) == 0      # a re-render keeps them
    assert [r for r in allow(target) if r.startswith("Bash(")] == bash
    # a live install made by plain init and then named as the coordinator's is REFUSED, never marked (door r2 (a)):
    # the door is open in an unmarked directory, and marking the operator's install would door his own sessions
    plain = tmp_path / "plain"
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(cl._install_answers()))
    assert inst.run_init(plain, "claude-code", force=False, answers_file=answers, default_grants="no") == 0
    before = CoordStore(qdir).coord_setting()
    out = _main(qdir, capsys, "coord", "agent", "claude", "--cwd", str(plain), rc=1)
    assert "not a coordinator's own" in out.err and "leave out --cwd" in out.err
    assert CoordStore(qdir).coord_setting() == before                  # nothing recorded
    # the workspace that already has the role is reused as is
    _main(qdir, capsys, "coord", "agent", "claude", "--cwd", str(target))
    assert CoordStore(qdir).coord_setting().cwd == str(target)
    assert not [r for r in allow(plain.resolve()) if r.startswith("Bash(")]
    assert "enabledMcpjsonServers" not in json.loads((plain.resolve() / ".claude" / "settings.json").read_text())
    assert inst.persisted_role(plain.resolve()) is None
    assert inst.installed_coordinator_levain(plain.resolve(), sys.executable) is None
    capsys.readouterr()
    # a damaged or hand-edited record never reads as the role
    config = target / ".levain" / "config.json"
    for body in ("{not json", '["coordinator"]', '{"role": "Coordinator"}'):
        config.write_text(body)
        assert inst.persisted_role(target) is None, body


def test_a_session_levain_launched_keeps_to_the_coordinators_door(store, qdir, workdir, capsys, monkeypatch):
    # the pre-approved trees grant nothing only if a launched session cannot write as the operator (L3 r1 codex HIGH);
    # the session runs in the launch's directory, the coordinator's workspace, and that is what the door reads
    launch_id = woken(store)
    _, why = store.launch_activate(launch_id, harness_session_id="sess-coord")
    assert why is None
    key = store.launch_by_harness_session("sess-coord").session_key
    (workdir / ".levain").mkdir(exist_ok=True)
    (workdir / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
    monkeypatch.chdir(workdir)
    tok = json.loads(_main(qdir, capsys, "coord", "claim", "--launch", launch_id, "--session", key, "--json").out)
    _main(qdir, capsys, "thread", "list")
    for argv in (("coord", "take"), ("coord", "wake"), ("coord", "agent", "none"),
                 ("coord", "release", "--token", str(tok["token"]))):
        out = _main(qdir, capsys, *argv, rc=1)
        assert "the coordinator's workspace" in out.err, (argv, out.err)
    _main(qdir, capsys, "coord", "release", "--by", "coordinator", "--token", str(tok["token"]))
    # the operator, outside that workspace: his take stands
    monkeypatch.chdir(workdir.parent)
    _main(qdir, capsys, "coord", "take")


def test_coord_agent_never_installs_over_a_directory_with_files(tmp_home, qdir, tmp_path, capsys):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    out = _main(qdir, capsys, "coord", "agent", "claude", "--cwd", str(busy), rc=1)
    assert "holds files" in out.err and CoordStore(qdir).coord_setting() is None
    assert sorted(p.name for p in busy.iterdir()) == ["notes.txt"]


def test_coord_agent_none_and_codex(qdir, capsys, tmp_path, monkeypatch):
    _main(qdir, capsys, "coord", "agent", "none")
    assert CoordStore(qdir).coord_setting().agent == "none"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))      # a Codex with no Levain hook
    out = _main(qdir, capsys, "coord", "agent", "codex", rc=1)
    assert "not set: Codex's consent hook is not live" in out.err
    assert CoordStore(qdir).coord_setting().agent == "none"


# --- the brief's directory (R2) --------------------------------------------------------------------------------
def test_a_linked_briefs_directory_refuses_the_launch(store, fake, live_hook, workdir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (workdir / ".levain").mkdir(exist_ok=True)
    (workdir / ".levain" / "briefs").symlink_to(elsewhere)
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "not a private directory" in got.refusal
    assert fake.spawns() == [] and list(elsewhere.iterdir()) == []
    assert store.launch(launch_id).state == "pending"           # G7: refused before the spawn claim


# --- the watcher (levain coord watch) --------------------------------------------------------------------------
@pytest.fixture
def clock(store, monkeypatch):
    t = [datetime.now(timezone.utc)]
    monkeypatch.setattr(store, "_clock", lambda: t[0])
    return t


def started(store):
    got = cl.launch_session(store, woken(store))
    assert got.started, got
    return store.launch(got.launch_id)


def test_watch_starts_a_pending_launch_nobody_started(store, fake, live_hook):
    # a drop from the phone or the cockpit writes the pending launch and starts nothing
    store.accept_drop("from the phone", surface="cli")
    (pending,) = store.launches_unspawned()
    lines = cl.watch_once(store)
    assert f"launch {pending.launch_id} started: claude attach {HID}" in lines
    assert store.launch(pending.launch_id).state == "active"
    # the next pass finds nothing to start, and the one-spawner row stands
    cl.watch_once(store)
    assert len(fake.spawns()) == 1 and store.launches_unspawned() == []


def test_watch_leaves_a_launch_another_process_is_starting(store, fake, live_hook, qdir):
    launch_id = woken(store)
    assert store.launch_claim_spawn(launch_id, 1)[1] is None        # someone else won the spawn
    cl.watch_once(store)
    assert fake.spawns() == [] and store.launch(launch_id).state == "pending"


def test_a_listed_working_holder_that_writes_keeps_the_lease(store, fake, live_hook, clock):
    from datetime import timedelta

    from levain.coordinator.store import Part

    ln = started(store)
    token = store.coord_claim(ln.launch_id, ln.session_key)
    for i in range(4):                                  # 4 x 20 min: well past one LEASE_TTL with no beat of its own
        clock[0] += timedelta(minutes=20)
        d, _ = store.accept_drop(f"drop {i}", surface="cli")
        store.split(d.drop_id, [Part("work", f"w{i}")], by="coordinator", token=token)    # a write, not a beat
        lines = cl.watch_once(store)
        assert any(line.startswith(f"lease {token} held by {ln.launch_id}: beat") for line in lines), lines
    held = store.coord_status()["lease"]
    assert held["token"] == token and held["live"] and held["holder"] == ln.session_key
    assert store.launch(ln.launch_id).state == "active"


def test_a_listed_done_holder_gets_no_beat_and_its_launch_ends(store, fake, live_hook, clock):
    # run_c3b_first_b: a finished turn stays listed, status idle, state done
    from datetime import timedelta

    ln = started(store)
    token = store.coord_claim(ln.launch_id, ln.session_key)
    fake.flag("done")
    clock[0] += timedelta(minutes=10)
    lines = cl.watch_once(store)
    assert any("its session is done, not beaten" in line for line in lines), lines
    clock[0] += lease.LEASE_TTL
    assert not store.coord_lease().live and store.coord_status()["lease"] is None      # lapsed, never beaten
    # C3a's IDLE_GRACE path: the next drop past it ends the launch (it wrote, so no death) and asks for a fresh one
    clock[0] += lease.IDLE_GRACE
    store.accept_drop("a later drop", surface="cli")
    got = store.launch(ln.launch_id)
    assert got.state == "ended" and got.cause == "lost the lease"
    assert store.coord_status()["wake"] == "launching" and token != store.coord_status()["lease"]["token"]
    # and the watcher stops the done session it left (the fresh launch's spawn lists nothing here: the fake has one
    # sessionId, which the fresh launch would otherwise take over)
    fake.mode("never")
    lines = cl.watch_once(store)
    assert any(line.startswith(f"launch {ln.launch_id} ended (lost the lease): claude stop {HID}") for line in lines)


def test_a_listed_working_holder_silent_past_two_ttls_lapses(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    token = store.coord_claim(ln.launch_id, ln.session_key)             # its last write
    for _ in range(3):                                  # t+20, t+40, t+60: the claim is within 2 x TTL
        clock[0] += timedelta(minutes=20)
        assert any(line.startswith(f"lease {token} held by {ln.launch_id}: beat") for line in cl.watch_once(store))
    clock[0] += timedelta(minutes=20)                   # t+80: silent 80 min, listed and working all along
    lines = cl.watch_once(store)
    assert any("it wrote nothing as coordinator in 60 min, not beaten" in line for line in lines), lines
    clock[0] += timedelta(minutes=20)                   # past the until the t+60 beat gave
    assert not store.coord_lease().live
    assert not any("beat to" in line for line in cl.watch_once(store))


def test_a_launch_just_started_that_never_wrote_gets_no_beat(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert any(f"held by {ln.launch_id}: it wrote nothing" in line for line in lines), lines


def test_a_holder_that_only_split_under_its_token_ends_gone_not_0_turns(store, fake, live_hook, clock):
    # L2 MED-3: a fenced thread write counts as a turn, not only a lease row
    from datetime import timedelta

    from levain.coordinator.store import Part

    ln = started(store)
    token = store.coord_status()["lease"]["token"]      # the bootstrap token its brief names; it never claimed
    d, _ = store.accept_drop("split me", surface="cli")
    store.split(d.drop_id, [Part("work", "w")], by="coordinator", token=token)
    fake.gone()
    clock[0] += timedelta(minutes=1)
    cl.watch_once(store)
    got = store.launch(ln.launch_id)
    assert got.state == "ended" and got.cause == "gone"
    assert store.coord_status()["wake"] == "wake"


def test_a_refused_fenced_write_records_nothing(store, fake, live_hook, qdir):
    from levain.coordinator.store import CoordError, Part

    ln = started(store)
    token = store.coord_status()["lease"]["token"]
    d, _ = store.accept_drop("split me", surface="cli")
    store.split(d.drop_id, [Part("work", "w")], by="coordinator", token=token)
    before = rows(qdir, "SELECT count(*) FROM lease_events")
    with pytest.raises(CoordError):                     # already split: the write rolls back, and its record with it
        store.split(d.drop_id, [Part("work", "w2")], by="coordinator", token=token)
    assert rows(qdir, "SELECT count(*) FROM lease_events") == before
    assert rows(qdir, "SELECT kind, cause, by, holder FROM lease_events ORDER BY seq DESC LIMIT 1") == [
        ("beat", "write", "coordinator", ln.session_key)]


def test_an_unlisted_holder_loses_the_lease_and_dies_at_0_turns(store, fake, live_hook, clock, qdir):
    from datetime import timedelta

    ln = started(store)
    (fake.dir / "state.json").unlink()                  # the session went away (crashed, or stopped by hand)
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id} ended: 0 turns (its session is no longer listed)" in lines
    got = store.launch(ln.launch_id)
    assert got.state == "ended" and got.cause == "0 turns"
    assert store.coord_status()["lease"] is None                       # released with its end
    assert rows(qdir, "SELECT by, cause FROM lease_events ORDER BY seq DESC LIMIT 1") == [("service", "launch ended")]
    # 0 turns is a death: the next drop waits out the backoff
    assert store.coord_status()["wake"] == "backoff"


def test_an_unlisted_holder_that_wrote_ends_gone(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    store.coord_claim(ln.launch_id, ln.session_key)     # it did something as coordinator
    (fake.dir / "state.json").unlink()
    clock[0] += timedelta(minutes=1)
    cl.watch_once(store)
    got = store.launch(ln.launch_id)
    assert got.state == "ended" and got.cause == "gone"
    assert store.coord_status()["wake"] == "wake"       # not a death


def test_an_unreadable_list_ends_nothing(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    (fake.dir / "state.json").unlink()
    (fake.dir / "agents_fail").write_text("")
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert "claude agents --json could not be read: no session observed" in lines
    assert store.launch(ln.launch_id).state == "active" and fake.stops() == []


def test_an_ended_launch_still_listed_is_stopped_and_no_other_session_is_touched(store, fake, live_hook, clock):
    from datetime import timedelta

    other = {"id": "9f00aa11", "sessionId": "9f00aa11-0000-4000-8000-000000000000", "name": "levain-coordinator-x",
             "cwd": "/elsewhere", "kind": "background", "state": "done", "status": "idle"}
    (fake.dir / "others.json").write_text(json.dumps([other]))
    ln = started(store)
    store.coord_take()                                  # you took over: its launch is ended, its session still runs
    assert store.launch(ln.launch_id).state == "ended"
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id} ended (takeover): claude stop {HID}: stopped {HID}" in lines
    assert fake.stops() == [["stop", HID]]              # never the session Levain did not start
    cl.watch_once(store)
    assert fake.stops() == [["stop", HID]]              # gone from the list: nothing more to stop
    assert store.coord_status()["lease"]["holder"] == "operator"


def _over_with_successor(store, clock):
    """An ended launch whose session still runs, and a successor wake waiting pending behind it."""
    from datetime import timedelta

    ln = started(store)
    store.coord_take()                                  # you took over: its launch is ended, its session still runs
    store.coord_release()                               # and handed it back: the next drop wakes a successor
    clock[0] += timedelta(minutes=1)
    store.accept_drop("next", surface="cli")
    (pending,) = store.launches_unspawned()
    return ln, pending


def test_a_stopped_session_still_listed_refuses_the_handover(store, fake, live_hook, clock):
    ln, pending = _over_with_successor(store, clock)
    fake.flag("stop_noop")
    lines = cl.watch_once(store)
    assert fake.stops() == [["stop", HID]]
    assert any(f"launch {pending.launch_id} not started: the session of an ended launch is still listed" in x
               for x in lines)
    assert store.launch(pending.launch_id).state == "pending" and len(fake.spawns()) == 1


def test_launch_session_itself_refuses_while_the_old_name_is_listed(store, fake, live_hook, clock):
    ln, pending = _over_with_successor(store, clock)             # not through the watcher: any caller hits the gate
    got = cl.launch_session(store, pending.launch_id)
    assert not got.started and f"still listed ({HID})" in got.refusal
    assert store.launch(pending.launch_id).state == "pending" and len(fake.spawns()) == 1


def test_a_session_that_clears_lets_the_handover_proceed(store, fake, live_hook, clock):
    ln, pending = _over_with_successor(store, clock)
    first = len(fake.spawns())
    cl.watch_once(store)                                # stops it; the fake's stop clears the row
    lines = cl.watch_once(store)
    assert f"launch {pending.launch_id} started: claude attach {HID}" in lines
    assert len(fake.spawns()) == first + 1 and store.launch(pending.launch_id).state == "active"


def test_an_unreadable_list_refuses_the_handover(store, fake, live_hook, clock):
    ln, pending = _over_with_successor(store, clock)
    fake.flag("agents_fail")
    got = cl.launch_session(store, pending.launch_id)
    assert not got.started and "`claude agents` could not be read" in got.refusal
    assert len(fake.spawns()) == 1 and store.launch(pending.launch_id).state == "pending"


def test_one_partial_list_does_not_clear_a_stale_session(store, fake, live_hook, clock):
    ln, pending = _over_with_successor(store, clock)
    fake.flag("miss_once")                              # the first read omits the stale row, the second lists it
    got = cl.launch_session(store, pending.launch_id)
    assert not got.started and f"still listed ({HID})" in got.refusal
    assert len(fake.spawns()) == 1 and store.launch(pending.launch_id).state == "pending"


def test_a_codex_row_does_not_block_a_claude_launch(store, fake, live_hook, clock, monkeypatch):
    from types import SimpleNamespace

    ln, pending = _over_with_successor(store, clock)
    monkeypatch.setattr(cl, "_ours", lambda st, row: SimpleNamespace(harness="codex", state="ended"))
    got = cl.launch_session(store, pending.launch_id)
    assert got.started, got


def test_a_row_with_no_usable_id_never_blocks(store, fake, live_hook, clock, monkeypatch):
    ln, pending = _over_with_successor(store, clock)
    row = {"id": None, "sessionId": SID, "name": "x", "cwd": "/x"}
    monkeypatch.setattr(cl, "_agents", lambda exe, **kw: [row])
    assert cl._handover_blocked(store, "claude", {}) == ""


def test_the_first_ever_spawn_with_an_empty_list_proceeds(store, fake, live_hook):
    fake.others([])
    got = cl.launch_session(store, woken(store))
    assert got.started, got


def test_watch_runs_its_passes(store, fake, live_hook, monkeypatch):
    monkeypatch.setattr(cl.time, "sleep", lambda s: None)
    store.accept_drop("later", surface="cli")
    out: list[str] = []
    cl.watch(store, every=5, emit=out.append, passes=2)
    assert len(fake.spawns()) == 1 and any("started" in line for line in out)


def test_levain_coord_watch_once_and_wake(store, qdir, fake, live_hook, capsys):
    out = _main(qdir, capsys, "coord", "watch", "--once")
    assert out.out.strip() == "nothing to do"
    # a wake starts the launch it wrote (R1), as a drop does
    out = _main(qdir, capsys, "coord", "wake")
    assert "asked for coordinator launch" in out.out and f"started: claude attach {HID}" in out.out
    assert len(fake.spawns()) == 1
    out = _main(qdir, capsys, "coord", "watch", "--once")
    assert "it wrote nothing as coordinator" in out.out and "beat to" not in out.out
    ln = next(x for x in CoordStore(qdir).launches_active() if x.role == "coordinator")
    CoordStore(qdir).coord_claim(ln.launch_id, ln.session_key)
    out = _main(qdir, capsys, "coord", "watch", "--once")
    assert "beat to" in out.out


def test_levain_coord_wake_exits_1_when_its_launch_is_refused(store, qdir, fake, live_hook, capsys):
    fake.mode("trust")
    out = _main(qdir, capsys, "coord", "wake", rc=1)
    assert "not started: workspace not trusted" in out.out and "&& claude" in out.out


# --- C3b fix 1: no session left running without a row; the list's evidence ------------------------------------
def test_an_interrupt_after_the_spawn_stops_the_session_and_reraises(store, fake, live_hook, monkeypatch):
    launch_id = woken(store)
    real = cl._read_back

    def read_back_then_ctrl_c(*a, **kw):
        real(*a, **kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(cl, "_read_back", read_back_then_ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        cl.launch_session(store, launch_id)
    assert fake.stops() == [["stop", HID]]
    assert store.launch(launch_id).state == "pending"           # it expires at LAUNCH_TTL; nothing runs under it


def test_a_fault_in_activation_stops_the_session(store, fake, live_hook, monkeypatch):
    import sqlite3 as sq

    launch_id = woken(store)

    def boom(*a, **kw):
        raise sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "launch_activate", boom)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "the launch faulted" in got.refusal and f"stopped {HID}" in got.refusal
    assert fake.stops() == [["stop", HID]] and store.launch(launch_id).state == "failed"


def test_a_row_without_a_usable_id_is_stopped_by_name(store, fake, live_hook, monkeypatch):
    launch_id = woken(store)
    real = cl._read_back
    monkeypatch.setattr(cl, "_read_back", lambda *a, **kw: [dict(real(*a, **kw)[0], sessionId=None)])
    got = cl.launch_session(store, launch_id)
    assert not got.started and "without a usable id" in got.refusal
    assert fake.stops() == [["stop", HID]] and store.launch(launch_id).state == "failed"


def test_a_row_in_another_directory_fails_and_is_never_stopped(store, fake, live_hook, workdir):
    # G4: a name is a string anyone can choose; a row elsewhere is not shown to be Levain's
    fake.flag("row_cwd", "/somewhere/else")
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and f"not {os.path.realpath(workdir)}" in got.refusal
    assert "not Levain's to stop" in got.refusal
    assert fake.stops() == [] and store.launch(launch_id).state == "failed"


def test_the_watcher_stops_a_session_a_failed_launch_left_by_name(store, fake, live_hook, clock, workdir):
    from datetime import timedelta

    fake.mode("never")                                   # listed only after the read back gave up
    launch_id = woken(store)
    assert not cl.launch_session(store, launch_id).started
    name = f"levain-coordinator-{launch_id.removeprefix('ln-')}"
    # G4: our name in ANOTHER directory is never stopped by name
    (fake.dir / "state.json").write_text(json.dumps({"name": name, "cwd": "/x", "reads": 1}))
    stranger = {"id": "5e5e5e5e", "sessionId": "5e5e5e5e-0000-4000-8000-000000000000",
                "name": "levain-coordinator-" + "ab" * 16, "cwd": "/y", "state": "working"}  # another store's
    fake.others([OTHER, stranger])
    clock[0] += timedelta(minutes=1)
    cl.watch_once(store)
    assert fake.stops() == []
    # in the launch's own directory it is
    (fake.dir / "state.json").write_text(json.dumps({"name": name, "cwd": str(workdir.resolve()), "reads": 1}))
    lines = cl.watch_once(store)
    assert any(line.startswith(f"launch {launch_id} failed (") and f"stopped {HID}" in line for line in lines), lines
    assert fake.stops() == [["stop", HID]]


def test_the_watcher_never_stops_a_pending_launchs_session(store, fake, live_hook, clock, workdir):
    launch_id = woken(store)
    assert store.launch_claim_spawn(launch_id, 1)[1] is None   # another process is starting it right now
    name = f"levain-coordinator-{launch_id.removeprefix('ln-')}"
    (fake.dir / "state.json").write_text(json.dumps({"name": name, "cwd": str(workdir.resolve()), "reads": 1}))
    cl.watch_once(store)
    assert fake.stops() == [] and store.launch(launch_id).state == "pending"


def test_a_starter_killed_outright_leaves_a_session_the_watcher_stops_after_launch_ttl(store, fake, live_hook, clock,
                                                                                      workdir):
    # SIGTERM / SIGKILL run no finally: the spawn row stands, the launch stays pending, its session runs
    from datetime import timedelta

    launch_id = woken(store)
    assert store.launch_claim_spawn(launch_id, 1)[1] is None
    name = f"levain-coordinator-{launch_id.removeprefix('ln-')}"
    (fake.dir / "state.json").write_text(json.dumps({"name": name, "cwd": str(workdir.resolve()), "reads": 1}))
    cl.watch_once(store)
    assert fake.stops() == []                           # in flight: never stopped
    clock[0] += lease.LAUNCH_TTL + timedelta(seconds=1)
    lines = cl.watch_once(store)
    assert store.launch(launch_id).state == "expired"
    assert any(line.startswith(f"launch {launch_id} expired (") for line in lines), lines
    assert fake.stops() == [["stop", HID]]


def test_an_empty_list_is_a_listing_and_two_of_them_end_the_launch(store, fake, live_hook, clock):
    # G2: a list read successfully with no row is valid (the coordinator may be the only session); the two reads
    # UNLISTED_RECHECK apart are what guard a death, never the list's length
    from datetime import timedelta

    ln = started(store)
    fake.gone()
    fake.others([])
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id} ended: 0 turns (its session is no longer listed)" in lines, lines
    assert store.launch(ln.launch_id).state == "ended"
    agents = [c for c in fake.calls() if c["argv"] == ["agents", "--json"]]
    assert len(agents) == 5                  # the spawn gate's two reads, the read back, then the two reads of this pass


def test_one_empty_read_then_a_listing_ends_nothing(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    fake.others([])
    fake.flag("miss_once")                               # the first read lists nothing at all, the second lists it
    clock[0] += timedelta(minutes=1)
    cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active"


def test_one_missed_read_ends_nothing(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    fake.flag("miss_once")                               # a partial first list; the second read lists it again
    clock[0] += timedelta(minutes=1)
    cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active"
    agents = [c for c in fake.calls() if c["argv"] == ["agents", "--json"]]
    assert len(agents) == 5                  # the spawn gate's two reads, the read back, then the two reads of this pass


def test_a_worker_launch_is_never_started(qdir, store, fake, live_hook):
    from levain.coordinator.store import Part

    d, _ = store.accept_drop("work for a worker", surface="cli")
    token = store.coord_take()
    (t,) = store.split(d.drop_id, [Part("work", "w")], by="operator")
    ln = store.launch_request(token, harness="claude", threads=[t.thread_id])
    got = cl.launch_session(store, ln.launch_id)
    assert not got.started and "the worker brief is not built" in got.refusal
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id}: a worker launch; the worker brief is not built, left pending" in lines
    assert fake.spawns() == [] and store.launch(ln.launch_id).state == "pending"
    assert rows(qdir, "SELECT count(*) FROM launch_spawns") == [(0,)]


def test_the_briefs_directory_is_gitignored(store, fake, live_hook, workdir):
    (workdir / ".levain").mkdir(exist_ok=True)
    (workdir / ".levain" / ".gitignore").write_text("answers.json\n")
    assert cl.launch_session(store, woken(store)).started
    lines = (workdir / ".levain" / ".gitignore").read_text().splitlines()
    assert [x for x in lines if x] == ["answers.json", "briefs/"]          # the install's own helper wrote it


def test_a_multi_line_cause_is_written_as_one_line(store, fake, live_hook, monkeypatch):
    from levain.coordinator.store import CoordError

    def boom(*a, **kw):
        raise CoordError("first line\nsecond\tline ‮")

    monkeypatch.setattr(cl, "_read_back", boom)
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    ln = store.launch(launch_id)
    assert ln.state == "failed" and ln.cause == got.refusal and "\n" not in ln.cause
    assert "first line second line" in ln.cause and "‮" not in ln.cause


def test_claude_not_on_path_refuses_before_the_spawn_claim(store, fake, live_hook, monkeypatch, qdir):
    # L3 r2: a local refusal like the hook's: nothing written, the launch left pending (an unspawned expiry is no death)
    monkeypatch.setattr(cl.shutil, "which", lambda *a, **k: None)
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "claude is not on PATH" in got.refusal and "left pending" in got.refusal
    assert store.launch(launch_id).state == "pending"
    assert rows(qdir, "SELECT COUNT(*) FROM launch_spawns") == [(0,)]


def test_a_session_found_gone_loses_its_brief_in_the_same_pass(store, fake, live_hook, clock):
    from datetime import timedelta

    ln = started(store)
    brief = cl.brief_path(ln)
    assert brief.is_file()
    (fake.dir / "state.json").unlink()
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id} ended: 0 turns (its session is no longer listed)" in lines
    assert not brief.exists()


def test_a_refusal_before_the_spawn_claim_is_one_line_too(store, fake, monkeypatch):
    monkeypatch.setattr(cl, "hook_live", lambda d, **kw: (False, "first line\nsecond\tline ‮"))
    got = cl.launch_session(store, woken(store))
    assert "\n" not in got.refusal and "first line second line" in got.refusal and "‮" not in got.refusal


@pytest.mark.parametrize("model", ["--dangerously-skip-permissions", "-x", "haiku sonnet", "a;b", ""])
def test_a_model_is_checked_where_the_setting_is_written(qdir, workdir, model):
    with pytest.raises(lease.LeaseRefused, match="never starting with -"):
        CoordStore(qdir).coord_set_agent("claude", model=model, cwd=str(workdir))


def test_a_model_name_the_harness_takes_is_kept(qdir, workdir):
    for model in ("haiku", "claude-opus-5-5", "claude-sonnet-5-5[1m]", "us.anthropic.x_1"):
        assert CoordStore(qdir).coord_set_agent("claude", model=model, cwd=str(workdir)).model == model


def test_coord_agent_refuses_a_bad_model_before_any_install(tmp_home, qdir, capsys):
    out = _main(qdir, capsys, "coord", "agent", "claude", "--model=--bad", rc=1)
    assert "never starting with -" in out.err and not (tmp_home / ".levain" / "coordinator").exists()


# --- C3b fix 2 (L3 r1) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_hooks_turned_off_by_a_settings_file_refuse_the_launch(tmp_home, qdir, fake, capsys, scope):
    # G1: doctor's real check of a real install; the consent hook row still reads "wired", a later row says off
    from levain import doctor

    # a user-level file reaches the coordinator only with --user-scope on (the default no longer loads it)
    _main(qdir, capsys, "coord", "agent", "claude", *(["--user-scope", "on"] if scope == "user" else []))
    target = (tmp_home / ".levain" / "coordinator").resolve()
    assert cl.hook_live(target)[0]
    f = tmp_home / ".claude" / "settings.json" if scope == "user" else target / ".claude" / "settings.local.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(f.read_text()) if f.is_file() else {}
    f.write_text(json.dumps({**data, "disableAllHooks": True}))
    checked = doctor._check_claude_code(target)
    (hook,) = [r for r in checked if r.name == cl.HOOK_ROW]
    assert hook.detail.startswith("wired to this install")          # the trap: wired, and still not running
    live, why = cl.hook_live(target)
    assert not live and "turned off by" in why and str(f) in why
    store = CoordStore(qdir)
    d, _ = store.accept_drop("hooks are off", surface="cli")
    launch_id = store.woken_launch(d.drop_id)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "turned off by" in got.refusal
    assert fake.spawns() == [] and store.launch(launch_id).state == "pending"


def test_agents_and_stop_run_in_the_store_directory_and_a_missing_launch_directory_blocks_no_stop(
        store, fake, live_hook, clock, qdir, workdir):
    # G3: only the spawn runs in the launch's directory
    import shutil
    from datetime import timedelta

    ln = started(store)
    spawn = next(c for c in fake.calls() if c["argv"][:1] == ["--bg"])
    assert spawn["cwd"] == os.path.realpath(workdir)
    store.coord_take()                                   # its launch is over, its session still listed
    shutil.rmtree(workdir)                               # and its directory is gone
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert any(line.startswith(f"launch {ln.launch_id} ended (takeover)") and f"stopped {HID}" in line
               for line in lines), lines
    assert fake.stops() == [["stop", HID]]
    mgmt = [c["cwd"] for c in fake.calls() if c["argv"][:1] != ["--bg"]]
    assert mgmt and set(mgmt) == {os.path.realpath(store.directory)}


def test_a_watch_pass_that_raises_anything_is_reported_and_the_next_runs(store, monkeypatch):
    # G5: a JobStoreError from the shared dispatch.db is no CoordError
    from levain.dispatch.jobs import JobStoreError

    monkeypatch.setattr(cl.time, "sleep", lambda s: None)
    calls = []

    def once(s):
        calls.append(1)
        if len(calls) == 1:
            raise JobStoreError("dispatch.db said no")
        return ["fine"]

    monkeypatch.setattr(cl, "watch_once", once)
    out: list[str] = []
    cl.watch(store, every=5, emit=out.append, passes=2)
    assert len(calls) == 2 and out[-1] == "fine"
    assert out[0].startswith("watch pass faulted (JobStoreError: dispatch.db said no)")

    def interrupted(s):
        raise KeyboardInterrupt

    monkeypatch.setattr(cl, "watch_once", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cl.watch(store, every=5, emit=out.append, passes=2)


def test_launches_that_expire_unspawned_are_no_deaths(store, clock):
    # G6: nobody tried to start them (no watcher ran): three in a row neither stop the wakes nor back them off
    from datetime import timedelta

    gap = max(lease.LAUNCH_TTL, lease.LEASE_TTL) + timedelta(seconds=1)
    ids = []
    for i in range(lease.MAX_DEATHS):
        ids.append(woken(store, f"drop {i}"))
        clock[0] += gap
        assert store.coord_status()["wake"] == "wake"
    assert [store.launch(x).state for x in ids] == ["expired"] * lease.MAX_DEATHS


def test_spawned_expiries_still_count_in_a_row_across_unspawned_ones(store, clock):
    # [seat] an unspawned lapse is passed over, neither a death nor a run: it never resets the count either
    from datetime import timedelta

    gap = max(lease.LAUNCH_TTL, lease.LEASE_TTL) + timedelta(seconds=1)
    for i in range(lease.MAX_DEATHS):
        launch_id = woken(store, f"spawned {i}")
        assert store.launch_claim_spawn(launch_id, 1)[1] is None      # a starter claimed it, then died
        clock[0] += gap + timedelta(days=1)                           # it expires; any backoff passes
        store.coord_status()
        if i + 1 < lease.MAX_DEATHS:
            woken(store, f"unspawned {i}")                            # asked for, nobody tried
            clock[0] += gap
    st = store.coord_status()
    assert st["wake"] == "stopped" and f"{lease.MAX_DEATHS} coordinator launches in a row died" in st["why"]


def test_the_woken_entry_is_read_once(store):
    # G8
    d, _ = store.accept_drop("once", surface="cli")
    assert store.woken_launch(d.drop_id) is not None
    assert store.woken_launch(d.drop_id) is None and store._woken == {}


def test_the_watcher_removes_the_brief_once_its_launch_is_over(store, fake, live_hook, clock, workdir):
    # G9: a brief quotes the drops verbatim; once its launch is over nothing reads it
    from datetime import timedelta

    ln = started(store)
    brief = cl.brief_path(ln)
    keep = brief.parent / "notes.md"
    keep.write_text("not a brief")
    cl.watch_once(store)
    assert brief.is_file()                               # active: kept
    store.coord_take()
    clock[0] += timedelta(minutes=1)
    lines = cl.watch_once(store)
    assert f"launch {ln.launch_id} ended: its brief removed" in lines
    assert not brief.exists() and keep.read_text() == "not a brief"
    assert not any("brief removed" in line for line in cl.watch_once(store))


# --- the pin (C0 §3.4) -----------------------------------------------------------------------------------------
from tests.test_coordinator_envelope import _lease, ctx, keys, repo, snapshot, structured_thread  # noqa: E402,F401


def test_a_one_thread_worker_is_pinned_to_its_threads_head_envelope(ctx, structured_thread):
    _d, b, t = structured_thread
    st = ctx.store
    token = _lease(st)
    _d2, t2 = ctx.plain_thread("a thread with no envelope")
    from levain.coordinator.store import CoordError

    one = st.launch_request(token, harness="claude", threads=[t.thread_id])
    # several threads carry no envelope authority, so no budget could meter them: refused (C6 L3 r1 B1)
    with pytest.raises(CoordError, match="a worker holds one thread"):
        st.launch_request(token, harness="claude", threads=[t.thread_id, t2.thread_id])
    bare = st.launch_request(token, harness="claude", threads=[t2.thread_id])
    coord = st.launch_request(token, harness="claude", role="coordinator")
    for i, ln in enumerate((one, bare, coord)):
        got, refused = st.launch_activate(ln.launch_id, harness_session_id=f"sid-{i}")
        assert refused is None and got.state == "active"
    assert st.launch(one.launch_id).pinned_envelope_id == b["envelope_id"] == st.head(t.thread_id).envelope_id
    # a thread with no envelope and the coordinator pin nothing
    assert [st.launch(ln.launch_id).pinned_envelope_id for ln in (bare, coord)] == [None, None]
    assert ctx.rows(f"SELECT kind, pinned_envelope_id FROM launch_events WHERE launch_id = '{one.launch_id}' "
                    "ORDER BY seq") == [("pending", None), ("active", b["envelope_id"])]


def test_a_store_without_envelopes_pins_nothing(qdir, tmp_path):
    from levain.coordinator.store import Part

    s = CoordStore(qdir)
    d, _ = s.accept_drop("plain words", surface="cli")
    token = s.coord_take()
    (t,) = s.split(d.drop_id, [Part("work", "w")], by="operator")
    ln = s.launch_request(token, harness="claude", threads=[t.thread_id])
    got, refused = s.launch_activate(ln.launch_id, harness_session_id="sid-x")
    assert refused is None and got.pinned_envelope_id is None


def test_the_brief_spelling_is_looked_up_as_exact_rules_never_parsed(tmp_path):
    # what the install allows, found as exact strings: this interpreter's rules, else an older install's bare ones;
    # anything else (no role, another interpreter's rules, a look-alike) is None and the caller's commands ask
    from levain import install as inst
    here, other = "/venv-a/bin/python", "/venv b/bin/python"
    d = tmp_path / "install"
    (d / ".claude").mkdir(parents=True)

    def rules(allow, role="coordinator"):
        (d / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": allow}}))
        (d / ".levain").mkdir(exist_ok=True)
        (d / ".levain" / "config.json").write_text(json.dumps({"role": role} if role else {}))
        return inst.installed_coordinator_levain(d, here)

    assert rules(list(inst.coordinator_allow(here))) == inst.coordinator_levain(here)
    assert rules(["Bash(levain coord:*)", "Bash(levain drops:*)", "Bash(levain thread:*)"]) == "levain"
    assert rules(list(inst.coordinator_allow(other))) is None          # another interpreter's install: asks
    assert rules(list(inst.coordinator_allow(here)), role=None) is None  # a reused install: the operator's rules
    for partial in (["Bash(make coord:*)", "Bash(make drops:*)", "Bash(make thread:*)"],
                    list(inst.coordinator_allow(here))[:2]):
        assert rules(partial) is None, partial


def test_a_claude_coordinator_may_not_edit_its_own_record(tmp_path, monkeypatch):
    # reproduced: the rendered coordinator settings carried allow rules only, so Claude's Write/Edit tools could
    # rewrite the record that marks its workspace. Claude Code consults Edit(path) rules for every editing tool.
    from levain import install as inst
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    doc = json.loads(inst._apply_role(json.dumps({"permissions": {"allow": []}}), "coordinator", "/venv/bin/python"))
    record = (tmp_path / "lh").resolve() / "consent" / "coordinator_workspaces.json"
    assert f"Edit(/{record})" in doc["permissions"]["deny"]
    assert f"Edit(/{record}.lock)" in doc["permissions"]["deny"]
    assert all(r.startswith("Edit(//") for r in doc["permissions"]["deny"])
    assert "deny" not in json.loads(inst._apply_role(json.dumps({"permissions": {}}), None, "/venv/bin/python"))["permissions"]


def test_every_levain_verb_the_brief_names_passes_the_coordinators_door(store, fake, live_hook, monkeypatch):
    """C5 L3 r1 (codex HIGH): the brief told the coordinator to run `coord questions|answer|escalate`, and the door
    refused all three inside its workspace. Every command path the brief names is read off the real CLI parser and
    must be in the door: a brief that names a verb the door refuses fails here."""
    from levain import install as inst
    from levain.cli import main
    from levain.coordinator import door

    launch_id = woken(store)
    assert cl.launch_session(store, launch_id).started
    text = cl.brief_path(store.launch(launch_id)).read_text()
    # the real parser, caught at the door's own call (the refusal stops the command before it runs)
    caught = []

    def catch(parser, args):
        caught.append(parser)
        return "caught"

    monkeypatch.setattr(door, "refusal", catch)
    assert main(["coord", "status"]) == 1 and caught
    parser = caught[0]

    def path_of(words):
        import argparse

        level, path = parser, []
        for word in words:
            subs = [a for a in level._actions if isinstance(a, argparse._SubParsersAction)]
            if not subs or word not in subs[0].choices:
                break
            nxt = subs[0].choices[word]
            path.append(next(name for name, p in subs[0].choices.items() if p is nxt))
            level = nxt
        return tuple(path)

    lv = inst.coordinator_levain(sys.executable)
    commands = [c for c in re.findall(r"`([^`]+)`", text) if c.startswith(lv + " ")]
    paths = set()
    for command in commands:
        words = command[len(lv) + 1:].split()
        # `thread state|set|merge|detach|add ...`: each alternative is a verb the brief names
        alts = next(((i, w.split("|")) for i, w in enumerate(words) if "|" in w), None)
        for alt in (alts[1] if alts else [None]):
            got = path_of(words if alts is None else [*words[:alts[0]], alt])
            assert len(got) >= 2, command
            paths.add(got)
    assert {("coord", "questions"), ("coord", "answer"), ("coord", "escalate"), ("coord", "claim")} <= paths
    refused = sorted(p for p in paths if p not in door.DOOR)
    assert not refused, f"the brief names verbs the coordinator's door refuses: {refused}"


# --- 1030-42: the coordinator spawns without your user layer unless --user-scope on -----------------------------
def _spawn_argv_and_sha(store, fake, qdir):
    launch_id = woken(store, "layers")
    assert cl.launch_session(store, launch_id).started
    argv = fake.spawns()[-1]
    (sha,) = rows(qdir, "SELECT argv_sha256 FROM launch_events WHERE launch_id = ? AND kind = 'active'", launch_id)
    return argv, sha[0]


def test_user_scope_off_is_the_default_and_on_restores_the_old_argv(store, fake, live_hook, qdir, workdir):
    assert store.coord_setting().user_scope is False
    off_argv, off_sha = _spawn_argv_and_sha(store, fake, qdir)
    assert off_argv.index("--setting-sources") < off_argv.index("--settings") < len(off_argv) - 1
    assert off_argv[off_argv.index("--setting-sources") + 1] == "project,local"
    assert off_argv[off_argv.index("--settings") + 1] == '{"autoMemoryEnabled":false}'
    assert len(off_argv) == 12 and off_sha.startswith("sha256:")


def test_user_scope_on_spawns_with_neither_flag(qdir, workdir, fake, live_hook):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()), user_scope=True)
    argv, _sha = _spawn_argv_and_sha(s, fake, qdir)
    assert argv[:7] == ["--bg", "-n", argv[2], "--permission-mode", "default", "--model", "haiku"]
    assert len(argv) == 8 and "--setting-sources" not in argv and "--settings" not in argv


def test_the_recorded_argv_sha_differs_between_on_and_off(qdir, workdir, fake, live_hook, tmp_path):
    shas = {}
    for scope in (False, True):
        q = tmp_path / f"q-{scope}"
        q.mkdir(mode=0o700)
        s = CoordStore(q)
        s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()), user_scope=scope)
        argv, sha = _spawn_argv_and_sha(s, fake, q)
        shas[scope] = (argv, sha)
    assert "--setting-sources" in shas[False][0] and "--settings" in shas[False][0]
    assert "--setting-sources" not in shas[True][0] and "--settings" not in shas[True][0]
    assert shas[False][1] != shas[True][1]


def test_a_pre_existing_store_reads_as_off_and_keeps_its_row(tmp_path):
    q = tmp_path / "old"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_setting()                                   # makes the store with the current schema
    c = sqlite3.connect(q / "dispatch.db")
    try:
        # coord_settings as it was before user_scope: no column, one standing row (its triggers go with the table)
        c.execute("DROP TABLE coord_settings")
        c.execute("CREATE TABLE coord_settings (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
                  "agent TEXT NOT NULL, model TEXT, cwd TEXT, by TEXT NOT NULL, at TEXT NOT NULL)")
        c.execute("INSERT INTO gseq (at) VALUES ('2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO coord_settings (gseq, agent, model, cwd, by, at) VALUES (1, 'claude', 'haiku', '/x', "
                  "'operator', '2026-01-01T00:00:00Z')")
        c.commit()
        row = lease.setting(c)                          # a reader on the old format, before any writer migrates it
    finally:
        c.close()
    assert (row.agent, row.model, row.user_scope) == ("claude", "haiku", False)
    fresh = CoordStore(q)                               # a writer's opener migrates, the old row reads off
    got = fresh.coord_setting()
    assert (got.agent, got.model, got.user_scope) == ("claude", "haiku", False)
    assert fresh.coord_set_agent("claude", model="haiku", cwd="/x", user_scope=True).user_scope is True
    assert fresh.coord_set_agent("claude", model="sonnet", cwd="/x").user_scope is True      # None keeps the choice
    assert fresh.coord_set_agent("none").user_scope is False


def test_coord_status_says_the_user_layer(tmp_home, qdir, fake, capsys):
    _main(qdir, capsys, "coord", "agent", "claude")
    text = _main(qdir, capsys, "coord", "status").out
    assert "user layer: off (project settings only)" in text
    js = json.loads(_main(qdir, capsys, "coord", "status", "--json").out)
    assert js["setting"]["user_scope"] is False
    out = _main(qdir, capsys, "coord", "agent", "claude", "--user-scope", "on").out
    assert "user layer: on" in out
    assert json.loads(_main(qdir, capsys, "coord", "status", "--json").out)["setting"]["user_scope"] is True
    s = CoordStore(qdir).coord_setting()
    # --user-scope alone keeps the agent, model and directory
    _main(qdir, capsys, "coord", "agent", "--user-scope", "off")
    t = CoordStore(qdir).coord_setting()
    assert (t.agent, t.model, t.cwd, t.user_scope) == (s.agent, s.model, s.cwd, False)
    # a bad value, no agent and nothing, and --user-scope on none, are refused
    with pytest.raises(SystemExit) as bad:
        _main(qdir, capsys, "coord", "agent", "claude", "--user-scope", "maybe")
    assert bad.value.code == 2
    _main(qdir, capsys, "coord", "agent", rc=1)
    _main(qdir, capsys, "coord", "agent", "none", "--user-scope", "on", rc=1)


def test_a_user_level_disable_all_hooks_is_not_consulted_without_user_scope(tmp_home, qdir, fake, capsys):
    _main(qdir, capsys, "coord", "agent", "claude")
    target = (tmp_home / ".levain" / "coordinator").resolve()
    user = tmp_home / ".claude"
    user.mkdir(exist_ok=True)
    (user / "settings.json").write_text('{"disableAllHooks": true}')
    live, detail = cl.hook_live(target, user_scope=True)
    assert not live and "turned off by" in detail
    live, detail = cl.hook_live(target, user_scope=False)
    assert live, detail
    assert "project,local only" in detail and "user-level disableAllHooks is not consulted" in detail
    # a project-level file still refuses, with or without the user layer
    (target / ".claude" / "settings.local.json").write_text('{"disableAllHooks": true}')
    assert not cl.hook_live(target, user_scope=False)[0]
