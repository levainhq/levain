"""Claude Code's PermissionRequest hook on the consent queue (Q2): with a fake core, the hook delivers allow only for
the core's verified allow, deny only for its verified deny, and nothing for silence, an exception or anything it was
merely sent; an agent-config target never reaches the policy seam. One end-to-end case runs the real core with a
real signed answer through ``main``.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cockpit.firejournal import CockpitDB
from levain.consent import ConsentQueue, Decision, answer_message, authority_source
from levain.consent.adapters import claude_code as cc
from levain.consent.record import ANSWER_NAMESPACE, ExternalGateRequest

SESSION = "ceedbaa9-2e6d-433f-b374-0ad96dd5f62d"


def event(tool_name="Bash", tool_input=None, **extra):
    """A PermissionRequest stdin as measured in Q0 (no tool_use_id on this event)."""
    e = {"session_id": SESSION, "transcript_path": "/x.jsonl", "cwd": "/work/proj", "permission_mode": "default",
         "hook_event_name": "PermissionRequest", "tool_name": tool_name,
         "tool_input": {"command": "touch x"} if tool_input is None else tool_input,
         "permission_suggestions": [{"type": "addRules", "behavior": "allow"}]}
    e.update(extra)
    return e


class FakeQueue:
    """The core's surface the adapter uses. ``verdict`` is what the core verified: None, 'allow', 'deny', or
    an exception to raise from the wait."""

    def __init__(self, verdict=None, *, at="await"):
        self.verdict, self.at = verdict, at
        self.filed: list[dict] = []
        self.requests: list[ExternalGateRequest] = []
        self.waited: list[float] = []
        self.withdrawn: list[str] = []

    def file(self, **kw):
        self.filed.append(kw)
        r = ExternalGateRequest.create(
            agent=kw["agent"], agent_session_id=kw["agent_session_id"], event_id=kw["event_id"] or "rand:1",
            tool_name=kw["tool_name"], tool_input_digest="jcs:x", display="d", cwd=kw["cwd"], tier=kw["tier"],
            received_at="2026-10-13T20:00:00.000000Z", expires_at="2026-10-13T20:10:00.000000Z")
        self.requests.append(r)
        return r

    def _decision(self, request_id):
        if isinstance(self.verdict, BaseException):
            raise self.verdict
        if self.verdict is None:
            return None
        digest = next(r.digest for r in self.requests if r.id == request_id) if self.requests else "sha256:none"
        return Decision(request_id=request_id, decision=self.verdict, signer_fp="SHA256:k", decided_at="t",
                        request_digest=digest)

    def await_decision(self, request_id, timeout, **kw):
        self.waited.append(timeout)
        return self._decision(request_id) if self.at == "await" else None

    def withdraw(self, request_id, note=""):
        self.withdrawn.append(request_id)
        return self.at != "race"

    def decision(self, request_id):
        return self._decision(request_id) if self.at == "race" else None


AWAY = cc.AdapterSettings(away_mode=True, away_hold_seconds=20)


def run_main(monkeypatch, queue, stdin_text, settings=AWAY, policy=None):
    monkeypatch.setattr(cc, "_queue_for", lambda d, s: queue)
    monkeypatch.setattr(cc, "load_settings", lambda d: settings)
    if policy is not None:
        monkeypatch.setattr(cc, "decide_by_policy", policy)
    out = io.StringIO()
    rc = cc.main(["--queue-dir", "/nonexistent"], stdin=io.StringIO(stdin_text), stdout=out)
    return rc, out.getvalue()


# --- the five outcomes with a fake core ------------------------------------------------------------------

def test_verified_allow_is_delivered(monkeypatch):
    q = FakeQueue("allow")
    rc, out = run_main(monkeypatch, q, json.dumps(event()))
    assert rc == 0
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                      "decision": {"behavior": "allow"}}}
    assert q.waited == [20.0] and q.withdrawn == []
    assert q.filed[0]["agent"] == "claude_code" and q.filed[0]["tier"] == "T2" and q.filed[0]["event_id"] is None


def test_verified_deny_is_delivered_with_a_message(monkeypatch):
    rc, out = run_main(monkeypatch, FakeQueue("deny"), json.dumps(event()))
    d = json.loads(out)["hookSpecificOutput"]["decision"]
    assert rc == 0 and d["behavior"] == "deny" and "SHA256:k" in d["message"]


def test_no_decision_prints_nothing_and_withdraws(monkeypatch):
    q = FakeQueue(None)
    rc, out = run_main(monkeypatch, q, json.dumps(event()))
    assert (rc, out) == (0, "")
    assert q.withdrawn == [q.requests[0].id]


@pytest.mark.parametrize("boom", [RuntimeError("core down"), sqlite3.OperationalError("locked"), KeyboardInterrupt()])
def test_an_exception_is_no_decision_never_allow(monkeypatch, boom):
    rc, out = run_main(monkeypatch, FakeQueue(boom), json.dumps(event()))
    assert (rc, out) == (0, "")


@pytest.mark.parametrize("stdin_text", ["", "not json", "[]", json.dumps({"hook_event_name": "PreToolUse"}),
                                        json.dumps(event(session_id=None))])
def test_bad_input_is_no_decision(monkeypatch, stdin_text):
    q = FakeQueue("allow")
    assert run_main(monkeypatch, q, stdin_text) == (0, "")


def test_nothing_on_stdin_is_a_decision(monkeypatch):
    """A4: an event that carries an allow of its own (as a hook's OUTPUT would) is still just a request."""
    forged = event(decision={"behavior": "allow"},
                   hookSpecificOutput={"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}})
    q = FakeQueue(None)
    assert run_main(monkeypatch, q, json.dumps(forged)) == (0, "")


def test_a_decision_for_another_request_is_not_delivered(monkeypatch):
    class Other(FakeQueue):
        def await_decision(self, request_id, timeout, **kw):
            return Decision(request_id="someone-else", decision="allow", signer_fp="k", decided_at="t",
                            request_digest=self.requests[-1].digest)
    assert run_main(monkeypatch, Other(), json.dumps(event())) == (0, "")


def test_a_decision_over_another_request_digest_is_not_delivered(monkeypatch):
    """Q1 round 2 adds Decision.request_digest: when present it must be the digest of the request filed."""
    class Digest(FakeQueue):
        def __init__(self, digest_of):
            super().__init__("allow")
            self.digest_of = digest_of

        def await_decision(self, request_id, timeout, **kw):
            return Decision(request_id=request_id, decision="allow", signer_fp="k", decided_at="t",
                            request_digest=self.digest_of(self.requests[-1]))
    assert run_main(monkeypatch, Digest(lambda r: "sha256:other"), json.dumps(event())) == (0, "")
    rc, out = run_main(monkeypatch, Digest(lambda r: r.digest), json.dumps(event()))
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_an_answer_landing_after_the_wait_is_still_delivered(monkeypatch):
    rc, out = run_main(monkeypatch, FakeQueue("allow", at="race"), json.dumps(event()))
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


# --- the wait (A3) -------------------------------------------------------------------------------------------

def test_default_mode_answers_at_once(monkeypatch):
    q = FakeQueue("allow")
    rc, out = run_main(monkeypatch, q, json.dumps(event()), settings=cc.AdapterSettings())
    assert (rc, out) == (0, "") and q.waited == []
    assert q.filed[0]["hold"] == cc.MIN_FILE_HOLD and q.withdrawn == [q.requests[0].id]


def test_the_wait_never_exceeds_the_installed_hold():
    q = FakeQueue(None)
    cc.decide(event(), q, cc.AdapterSettings(away_mode=True, away_hold_seconds=5000), max_hold=60 + cc.CLEANUP_RESERVE,
              clock=lambda: 0.0)
    assert q.waited == [60.0] and q.filed[0]["hold"] == timedelta(seconds=60)


def test_settings_default_to_away_off(tmp_path):
    assert cc.load_settings(tmp_path) == cc.AdapterSettings()
    (tmp_path / cc.SETTINGS_NAME).write_text(json.dumps({"away_mode": "yes", "away_hold_seconds": float("nan")}))
    assert cc.load_settings(tmp_path) == cc.AdapterSettings()
    (tmp_path / cc.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "away_hold_seconds": 90,
                                                         "cockpit_data_root": "/r"}))
    assert cc.load_settings(tmp_path) == cc.AdapterSettings(True, 90.0, "/r")


def test_policy_answer_is_delivered_without_a_wait(monkeypatch):
    def policy(request):
        return Decision(request_id=request.id, decision="allow", signer_fp="policy", decided_at="t",
                        request_digest=request.digest)
    q = FakeQueue(None)
    rc, out = run_main(monkeypatch, q, json.dumps(event()), settings=cc.AdapterSettings(), policy=policy)
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"} and q.waited == []


def test_subagent_and_mcp_requests_are_filed(monkeypatch):
    q = FakeQueue(None)
    run_main(monkeypatch, q, json.dumps(event(agent_id="a1", agent_type="general-purpose")))
    assert q.filed[0]["agent_session_id"] == f"{SESSION}#agent:a1"
    q2 = FakeQueue(None)
    e = event(tool_name="mcp__q0srv__q0_ping", mcp_server={"name": "q0srv", "source": "dynamic"})
    del e["tool_input"]
    run_main(monkeypatch, q2, json.dumps(e))
    assert q2.filed[0]["tool_name"] == "mcp__q0srv__q0_ping" and q2.filed[0]["tool_input"] == {}


def test_a_hook_event_id_is_passed_through(monkeypatch):
    q = FakeQueue(None)
    run_main(monkeypatch, q, json.dumps(event(tool_use_id="toolu_01")))
    assert q.filed[0]["event_id"] == "toolu_01"


# --- ruling 3b item 1: agent config ---------------------------------------------------------------------------

HOME = Path("/Users/someone")


@pytest.mark.parametrize("tool,path", [
    ("Edit", ".claude/settings.json"), ("Write", ".claude/settings.local.json"), ("MultiEdit", "./.claude/hooks/x.sh"),
    ("Write", "/work/proj/.claude/hooks/sub/y.py"), ("Edit", "~/.claude/settings.json"),
    ("Write", "/Users/someone/.claude/hooks/levain_consent.py"), ("Edit", ".codex/hooks.json"),
    ("Write", "~/.codex/config.toml"), ("Edit", "src/../.claude/settings.json"), ("Write", ".CLAUDE/Settings.json"),
])
def test_agent_config_targets_are_tagged(tool, path):
    assert cc.targets_agent_config(tool, {"file_path": path}, "/work/proj", home=HOME)


@pytest.mark.parametrize("tool,inp", [
    ("Edit", {"file_path": "src/app.py"}), ("Write", {"file_path": ".claude/agents/x.md"}),
    ("Edit", {"file_path": "docs/settings.json"}), ("Write", {"file_path": ".codex/notes.md"}),
    ("Bash", {"command": "echo {} > .claude/settings.json"}),  # Bash is never parsed (ruling 1)
])
def test_other_targets_are_not_tagged(tool, inp):
    assert not cc.targets_agent_config(tool, inp, "/work/proj", home=HOME)


def test_a_symlink_into_agent_config_is_tagged(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / "innocent.json").symlink_to(tmp_path / ".claude" / "settings.json")
    assert cc.targets_agent_config("Write", {"file_path": "innocent.json"}, str(tmp_path))


def test_an_unreadable_file_target_is_tagged():
    assert cc.targets_agent_config("Edit", {"path": "x"}, "/w") and cc.targets_agent_config("Write", "x", "/w")


def test_agent_config_never_reaches_the_policy_seam(monkeypatch):
    calls = []

    def policy(request):
        calls.append(request)
        return Decision(request_id=request.id, decision="allow", signer_fp="policy", decided_at="t",
                        request_digest=request.digest)
    q = FakeQueue(None)
    rc, out = run_main(monkeypatch, q, json.dumps(event("Edit", {"file_path": ".claude/settings.json",
                                                                  "old_string": "a", "new_string": "b"})),
                       settings=cc.AdapterSettings(), policy=policy)
    assert (rc, out) == (0, "") and calls == []
    assert q.filed[0]["tier"] == cc.AGENT_CONFIG_TIER
    # the seam itself refuses one, should a future caller route it there
    with pytest.raises(cc.PolicyNotEligible):
        cc.guarded_policy(q.requests[0], policy)
    assert calls == []


def test_agent_config_still_takes_a_signed_human_answer(monkeypatch):
    q = FakeQueue("allow")
    rc, out = run_main(monkeypatch, q, json.dumps(event("Write", {"file_path": ".claude/hooks/h.sh"})))
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


# --- install ----------------------------------------------------------------------------------------------------

def test_install_writes_the_hook_and_keeps_other_settings(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"model": "haiku", "hooks": {"Stop": [{"hooks": []}]}}))
    script, settings = cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)  # idempotent
    data = json.loads(settings.read_text())
    assert data["model"] == "haiku" and "Stop" in data["hooks"]
    [entry] = data["hooks"]["PermissionRequest"]
    [hook] = entry["hooks"]
    assert entry["matcher"] == "*" and hook["timeout"] == 60 + cc.TIMEOUT_MARGIN
    assert hook["command"] == f"/py/bin/python3 {script}"
    text = script.read_text()
    assert text.startswith("#!/py/bin/python3\n") and "'--max-hold', '60.0'" in text
    compile(text, str(script), "exec")


# --- the real core, end to end through main ----------------------------------------------------------------

class Key:
    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.public_key = " ".join((d / f"{name}.pub").read_text().split()[:2])
        self.fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.path)], check=True,
                                 capture_output=True, text=True).stdout.split()[1]

    def sign(self, message: bytes, namespace: str) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, check=True, timeout=30).stdout.decode()


def test_real_core_signed_allow_through_main(tmp_path):
    key = Key(tmp_path, "laptop")
    root = tmp_path / "cockpit"
    root.mkdir()
    with CockpitDB(root).read():  # a real cockpit journal (its meta table): capture reads it, DESIGN §3.4
        pass
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    auth.sign_and_append(conn, {"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key,
                                "class": "laptop-presence", "custody": "passphrase"},
                         signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE),
                         clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
    conn.close()
    qdir = tmp_path / "home" / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    (qdir / cc.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "away_hold_seconds": 20,
                                                     "cockpit_data_root": str(root)}))
    observer = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=None))

    def answer():
        for _ in range(100):
            try:
                pending = observer.pending()
            except Exception:
                pending = []
            if pending:
                r = pending[0]
                p = observer.answer_payload(r.id, "allow", key.fp)
                observer.answer(r.id, p, key.sign(answer_message(p), ANSWER_NAMESPACE))
                return
            time.sleep(0.1)

    t = threading.Thread(target=answer)
    t.start()
    out = io.StringIO()
    rc = cc.main(["--queue-dir", str(qdir), "--max-hold", "90"], stdin=io.StringIO(json.dumps(event())), stdout=out)
    t.join()
    assert rc == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_real_core_without_authority_is_no_decision(tmp_path):
    qdir = tmp_path / "consent"
    out = io.StringIO()
    rc = cc.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(event())), stdout=out)
    assert (rc, out.getvalue()) == (0, "")
    q = ConsentQueue(qdir, authority=authority_source(qdir, anchor=None))
    assert q.pending() == []  # filed, then withdrawn: the native dialog stands
    assert q._fresh_authority() is None  # the no-root source never replays


# --- fix round 1 (L3 Q2 r1, codex 1/2/4/6/7/8/9) ----------------------------------------------------------------

@pytest.mark.parametrize("rel", ["consent/claude_code.json", "consent/queue.db", "consent/queue.db-wal",
                                 "cockpit/cockpit.db", "cockpit/cockpit.db-wal", "cockpit/anchor/authority.anchor"])
def test_the_adapters_own_trust_root_is_agent_config(tmp_path, rel):
    """P1 (codex 1): the adapter settings, the queue db, the authority db and its anchor are T3, policy-ineligible."""
    settings = cc.AdapterSettings(cockpit_data_root=str(tmp_path / "cockpit"))
    protected = cc.protected_paths(tmp_path / "consent", settings)
    assert cc.targets_agent_config("Write", {"file_path": str(tmp_path / rel)}, "/work/proj", protected=protected)
    assert not cc.targets_agent_config("Write", {"file_path": str(tmp_path / "other.json")}, "/work/proj",
                                       protected=protected)


def test_main_files_a_write_to_the_adapter_settings_at_t3_without_policy(monkeypatch, tmp_path):
    calls = []
    q = FakeQueue(None)
    target = str(tmp_path / "consent" / cc.SETTINGS_NAME)
    monkeypatch.setattr(cc, "_queue_for", lambda d, s: q)
    monkeypatch.setattr(cc, "decide_by_policy", lambda r: calls.append(r))
    cc.main(["--queue-dir", str(tmp_path / "consent")],
            stdin=io.StringIO(json.dumps(event("Write", {"file_path": target, "content": "{}"}))), stdout=io.StringIO())
    assert q.filed[0]["tier"] == cc.AGENT_CONFIG_TIER and calls == []


@pytest.mark.parametrize("root", ["authority", "./cockpit", "cockpit/../x", "", 7])
def test_a_relative_cockpit_root_is_no_authority(tmp_path, root):
    """P2 (codex 2): never resolved against the hook's cwd (the project)."""
    (tmp_path / cc.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "cockpit_data_root": root}))
    assert cc.load_settings(tmp_path).cockpit_data_root is None


def test_a_home_relative_cockpit_root_is_expanded_and_canonical(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / cc.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": "~/link"}))
    assert cc.load_settings(tmp_path).cockpit_data_root == os.path.realpath(real)


@pytest.mark.parametrize("link", [".claude", ".claude/hooks", ".claude/settings.json",
                                  ".claude/hooks/levain_consent.py"])
def test_install_refuses_a_symlink_on_the_path(tmp_path, link):
    """P3 (codex 4): a workspace symlink never redirects the install (e.g. .claude -> ~/.claude)."""
    ws, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    ws.mkdir()
    elsewhere.mkdir()
    (elsewhere / "victim").write_text("{}")
    parent = (ws / link).parent
    parent.mkdir(parents=True, exist_ok=True)
    (ws / link).symlink_to(elsewhere if not link.endswith((".json", ".py")) else elsewhere / "victim")
    with pytest.raises(ValueError):
        cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    assert (elsewhere / "victim").read_text() == "{}" and sorted(p.name for p in elsewhere.iterdir()) == ["victim"]


def test_reinstall_removes_only_the_exact_levain_handler(tmp_path):
    """P4 (codex 8): other handlers in a shared group and group fields stay; a lookalike command stays."""
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    script, settings = cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    data = json.loads(settings.read_text())
    [group] = data["hooks"]["PermissionRequest"]
    audit = {"type": "command", "command": "/usr/bin/audit backup-levain_consent.py"}
    group["hooks"].append(audit)
    group["note"] = "kept"
    lone = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/levain_consent.py.bak"}]}
    data["hooks"]["PermissionRequest"].append(lone)
    settings.write_text(json.dumps(data))
    cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    groups = json.loads(settings.read_text())["hooks"]["PermissionRequest"]
    assert groups[0] == {"matcher": "*", "hooks": [audit], "note": "kept"}
    assert groups[1] == lone
    assert groups[2]["hooks"][0]["command"] == f"/py/bin/python3 {script}" and len(groups) == 3


def test_settings_update_is_an_atomic_replace(tmp_path, monkeypatch):
    """P4 (codex 9): written to a temp file, fsynced, renamed over; never truncated in place."""
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"model": "haiku"}))
    before = os.stat(ws / ".claude" / "settings.json").st_ino
    synced, replaced = [], []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(cc.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(cc.os, "replace", lambda a, b: (replaced.append(Path(b).name), real_replace(a, b))[1])
    cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    assert "settings.json" in replaced and cc.HOOK_SCRIPT_NAME in replaced and len(synced) >= 2
    assert os.stat(ws / ".claude" / "settings.json").st_ino != before
    assert json.loads((ws / ".claude" / "settings.json").read_text())["model"] == "haiku"
    assert [p.name for p in (ws / ".claude").iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("where", ["policy", "await"])
def test_an_exception_after_filing_withdraws(where):
    """P5 (codex 6): no ghost request stays pending (and answerable) after the hook has gone."""
    q = FakeQueue(RuntimeError("boom") if where == "await" else None)

    def policy(request):
        raise RuntimeError("policy boom")
    with pytest.raises(RuntimeError):
        cc.decide(event(), q, AWAY, policy=policy if where == "policy" else (lambda r: None))
    assert q.withdrawn == [q.requests[0].id]


def test_a_withdraw_that_raises_is_retried_by_the_cleanup():
    """L3 q2 r2 (fix-introduced MED): `settled` was set before the normal-path withdraw, so a withdraw that raised
    (a locked store past its busy timeout) skipped the cleanup and left a ghost request pending."""
    q = FakeQueue(None)
    calls = []

    def withdraw(request_id, note=""):
        calls.append(request_id)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        q.withdrawn.append(request_id)
        return True
    q.withdraw = withdraw
    with pytest.raises(RuntimeError):
        cc.decide(event(), q, AWAY, policy=lambda r: None)
    assert len(calls) == 2 and q.withdrawn == [q.requests[0].id]


def test_reinstall_under_another_python_replaces_the_old_levain_handler(tmp_path):
    """L3 q2 r2 (fix-introduced MED): matching only the exact command left the old interpreter's handler running
    beside the new one. A lookalike script path is still never ours."""
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    script, settings = cc.install(ws, python="/old/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    data = json.loads(settings.read_text())
    lookalike = {"type": "command", "command": f"/old/bin/python3 {script}.bak"}
    data["hooks"]["PermissionRequest"].append({"matcher": "*", "hooks": [lookalike]})
    settings.write_text(json.dumps(data))
    cc.install(ws, python="/new/bin/python3", queue_dir=tmp_path / "q", max_hold=60)
    commands = [h["command"] for g in json.loads(settings.read_text())["hooks"]["PermissionRequest"] for h in g["hooks"]]
    assert commands.count(f"/new/bin/python3 {script}") == 1
    assert not any(c == f"/old/bin/python3 {script}" for c in commands)
    assert lookalike["command"] in commands


def test_max_hold_is_a_total_deadline_and_the_cc_timeout_exceeds_it():
    """P6 (codex 7): the wait leaves CLEANUP_RESERVE of max_hold for withdraw + re-read; Claude Code's timeout
    is above wait + reserve, so it never cancels a hook that is delivering a verified answer."""
    for max_hold in (0, 30, 60, 90, 900, 1800.5):
        q = FakeQueue(None)
        cc.decide(event(), q, cc.AdapterSettings(away_mode=True, away_hold_seconds=10**6), max_hold=max_hold)
        wait = q.waited[0] if q.waited else 0.0
        assert wait <= max(0.0, max_hold - cc.CLEANUP_RESERVE) + 1e-6
        assert wait + cc.CLEANUP_RESERVE < cc.hook_timeout(max_hold)
    assert cc.CLEANUP_RESERVE >= 30 + 10  # sqlite busy timeout (withdraw) + one signature verification


def test_the_deadline_counts_time_already_spent():
    t = [1000.0]
    q = FakeQueue(None)

    def slow_policy(request):
        t[0] += 50
        return None
    cc.decide(event(), q, cc.AdapterSettings(away_mode=True, away_hold_seconds=10**6), max_hold=200,
              policy=slow_policy, clock=lambda: t[0])
    assert q.waited == [200 - cc.CLEANUP_RESERVE - 50]


def test_a_failing_stdout_is_still_exit_0(monkeypatch):
    class Broken(io.StringIO):
        def write(self, s):
            raise BrokenPipeError
    monkeypatch.setattr(cc, "_queue_for", lambda d, s: FakeQueue("allow"))
    monkeypatch.setattr(cc, "load_settings", lambda d: AWAY)
    assert cc.main(["--queue-dir", "/nonexistent"], stdin=io.StringIO(json.dumps(event())), stdout=Broken()) == 0
