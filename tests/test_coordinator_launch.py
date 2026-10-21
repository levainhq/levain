"""Starting a launch's session (C3b; C0 §3.4, §7 L2 L8). Built from the runs in project_memory
1020-coordinator/c3/: run_c3b_first_a.txt (the trust refusal, by realpath), run_c3b_first_b.txt (the spawn, the first
agents row with no pid and no status, stop) and run_owed2_claude_bg.txt (--bg ignores --session-id). A fake ``claude``
on PATH replays those outputs; no test here ever spawns the real one."""
from __future__ import annotations

import json
import os
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
    if not state.exists():
        print("[]"); sys.exit(0)
    s = json.loads(state.read_text())
    row = {{"id": "{HID}", "cwd": s["cwd"], "kind": "background", "startedAt": 1792552347854,
           "sessionId": "{SID}", "name": s["name"], "state": "working"}}
    if s["reads"]:
        row.update(pid=54960, status="busy")      # the first row right after the spawn has neither
    s["reads"] += 1
    state.write_text(json.dumps(s))
    print(json.dumps([row], indent=2)); sys.exit(0)
if args[:1] == ["stop"]:
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


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "claude"
    exe.write_text(_FAKE.format(python=sys.executable, HID=HID, SID=SID))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cl, "READBACK_DELAY", 0.0)
    f = Fake(d)
    f.mode("ok")
    return f


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "coord-install"
    d.mkdir()
    return d


@pytest.fixture
def live_hook(monkeypatch):
    """The hook check passes (the real check against a real install is test_coord_agent_sets_up_the_install)."""
    monkeypatch.setattr(cl, "hook_live", lambda d: (True, "wired to this install"))


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
    brief = cl.brief_path(store, launch_id)
    # exactly the measured argv, plus the model, plus the prompt naming the brief; never --session-id or a bypass
    assert argv[:7] == ["--bg", "-n", name, "--permission-mode", "default", "--model", "haiku"]
    assert len(argv) == 8 and str(brief) in argv[7]
    assert not any(a in ("--session-id", "--bare", "--dangerously-skip-permissions") or "bypass" in a for a in argv)
    spawn = next(c for c in fake.calls() if c["argv"][:1] == ["--bg"])
    assert spawn["cwd"] == str(workdir.resolve())
    (sha,) = rows(qdir, "SELECT argv_sha256 FROM launch_events WHERE launch_id = ? AND kind = 'active'", launch_id)
    assert sha[0].startswith("sha256:")
    # the brief: 0600, the ids, how to claim, the drop quoted as data
    assert stat.S_IMODE(brief.stat().st_mode) == 0o600
    text = brief.read_text()
    assert launch_id in text and ln.session_key in text and '"please fix the flaky test"' in text
    assert f"levain coord claim --launch {launch_id} --session {ln.session_key}" in text
    assert "--by coordinator --token N" in text and "DATA" in text
    # the active session holds the bootstrap lease for a full TTL, and claims it as the brief says
    assert store.coord_claim(launch_id, ln.session_key) == store.coord_status()["lease"]["token"]


def test_the_first_agents_row_has_no_pid_and_is_enough(store, fake, live_hook):
    got = cl.launch_session(store, woken(store))
    assert got.started
    agents = [c for c in fake.calls() if c["argv"] == ["agents", "--json"]]
    assert len(agents) == 1          # the pid-less first row was taken: no wait for a pid


# --- run_c3b_first_a: the trust refusal ------------------------------------------------------------------------
def test_an_untrusted_directory_fails_with_its_realpath_and_the_remedy(qdir, tmp_path, fake, live_hook):
    real = tmp_path / "real-dir"
    real.mkdir()
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
    assert len([c for c in fake.calls() if c["argv"] == ["agents", "--json"]]) == cl.READBACK_TRIES
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


def test_a_directory_whose_hook_is_not_live_refuses(qdir, tmp_path, fake):
    plain = tmp_path / "plain"
    plain.mkdir()
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(plain))
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "the consent hook is not live" in got.refusal and "no CLAUDE.md" in got.refusal
    assert fake.spawns() == [] and store.launch(launch_id).state == "failed"


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
    with pytest.raises(lease.LeaseRefused, match="only claude is built"):
        store.coord_set_agent("codex", cwd=str(workdir))
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


def test_coord_agent_never_installs_over_a_directory_with_files(tmp_home, qdir, tmp_path, capsys):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    out = _main(qdir, capsys, "coord", "agent", "claude", "--cwd", str(busy), rc=1)
    assert "holds files" in out.err and CoordStore(qdir).coord_setting() is None
    assert sorted(p.name for p in busy.iterdir()) == ["notes.txt"]


def test_coord_agent_none_and_codex(qdir, capsys):
    _main(qdir, capsys, "coord", "agent", "none")
    assert CoordStore(qdir).coord_setting().agent == "none"
    out = _main(qdir, capsys, "coord", "agent", "codex", rc=1)
    assert "only claude is built" in out.err
