"""Codex's PreToolUse gate (Q14 B5): the per-session latch and the hook trust levain writes."""
from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta, timezone

from levain.consent import ConsentQueue, authority_source
from levain.consent.adapters import codex as cx
from levain.consent.ledger import Ledger
import pytest

from tests._h4 import h4_env, h4_home


@pytest.fixture(autouse=True)
def _no_band_notice(monkeypatch):
    """These tests are about the latch: the band's fallback notice (an open item, no Codex band hooks) is not theirs."""
    monkeypatch.setenv("LEVAIN_SEAT", "autonomous")


def _event(session: str, tool_name: str, tool_input: dict, cwd: str) -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": session, "turn_id": "t1", "transcript_path": None,
            "cwd": cwd, "model": "m", "permission_mode": "bypassPermissions", "tool_name": tool_name,
            "tool_input": tool_input, "tool_use_id": f"call-{session}"}


def _run(qdir, event: dict) -> str:
    out = io.StringIO()
    assert cx.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
    return out.getvalue()


def test_a_halted_session_is_latched_and_no_other_session_is(tmp_path):
    # Reproduced on 5d330ec8: Codex's hook routed every event to the PermissionRequest path, so a PreToolUse of a
    # session halted on gap #1 printed nothing and the call ran (runs3-codex RUN 1: only the latch stops a run).
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    source = authority_source(qdir, anchor=None)
    filed = ConsentQueue(qdir, authority=source, clock=lambda: t0, home=h4_home())
    request = filed.file(agent="codex", agent_session_id="S1", tool_name="apply_patch",
                         tool_input={"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"},
                         cwd=str(tmp_path), hold=timedelta(seconds=20), tier="T3")
    later = ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21), home=h4_home())
    assert later.mark_undecided(request.id, halt=True).gap == 1

    benign = {"command": "ls -a"}
    halted = json.loads(_run(qdir, _event("S1", "Bash", benign, str(tmp_path))))
    spec = halted["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"] == cx.HALT_TEXT.format(n=1)
    assert "continue" not in halted  # Codex drops a deny that carries continue:false
    assert _run(qdir, _event("S2", "Bash", benign, str(tmp_path))) == ""  # another session runs

    rows = Ledger(qdir).window("codex", "S1", t0 - timedelta(days=1))
    assert [(r.phase, r.stamp, r.outcome, (r.stamp_detail or {}).get("decision")) for r in rows] == [
        ("denied_by_gate", "unknown", "refused", "halted")]
    assert Ledger(qdir).window("codex", "S2", t0 - timedelta(days=1)) == []


def test_trusted_hash_is_codex_own_and_doctor_reads_trust(tmp_path):
    # Measured on codex-cli 0.162.1 (10-18, scratch CODEX_HOME): hooks/list reported these currentHash values, and
    # each hook was "untrusted" (skipped silently) until config.toml held exactly that hash.
    pre = {"matcher": ".*", "hooks": [{"type": "command", "command": "/usr/bin/true", "timeout": 870}]}
    stop = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
    assert cx.hook_trusted_hash("PreToolUse", pre, pre["hooks"][0]) == \
        "sha256:d4e6b7e713ce9b37af4e036c2649efa8f9c825fdb0d699b3a4d666263a5325c4"
    assert cx.hook_trusted_hash("Stop", stop, stop["hooks"][0]) == \
        "sha256:e2250e1310df8c544c98765af45ec9400886a9f3cf53958a4b1edc99d0dc2d5b"

    home = tmp_path / "codex-home"
    home.mkdir()
    script = tmp_path / "inst" / "activation" / "hooks" / "consent_codex.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(cx.GATE_SCRIPT_SOURCE.read_bytes())
    handler = {"type": "command", "command": f"/py -EP -m levain.hookrun {script}", "timeout": cx.GATE_TIMEOUT}
    hooks = home / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [handler]}]}}))
    no_codex = "/nonexistent/levain-test-codex"  # config.toml is read; Codex's own verdict cannot be
    assert cx.gate_trust(hooks, script, home=home, codex_bin=no_codex, python="/py").status == "untrusted"
    digest = cx.hook_trusted_hash("PreToolUse", {"matcher": "*"}, handler)
    (home / "config.toml").write_text(f'[hooks.state."{hooks}:pre_tool_use:0:0"]\ntrusted_hash = "{digest}"\n')
    got = cx.gate_trust(hooks, script, home=home, codex_bin=no_codex, python="/py")
    assert got.status == "unconfirmed"  # never GREEN
    script.write_text(script.read_text() + "\n")  # Codex hashes the command, so only levain sees this
    assert cx.gate_trust(hooks, script, home=home, codex_bin=no_codex, python="/py").status == "script_changed"
    script.write_bytes(cx.GATE_SCRIPT_SOURCE.read_bytes())
    for group in ({"matcher": "Bash"}, {"matcher": "*", "async": True}, {"matcher": "*", "timeout": 600}):
        h = {**handler, **{k: v for k, v in group.items() if k != "matcher"}}
        hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": group["matcher"], "hooks": [h]}]}}))
        got = cx.gate_trust(hooks, script, home=home, codex_bin=no_codex, python="/py")
        assert got.status == "changed" and "differs from levain's render" in got.detail, (group, got)


def test_with_no_key_a_codex_config_patch_is_a_plain_deny_unless_default_or_unconfined(tmp_path, monkeypatch):
    """Q10 D1 + D2 on Codex: with no enrolled key a gated call answered nothing and the mode ran it. Now any mode
    but default gets the floor's PLAIN deny (Codex drops a deny that carries continue:false,
    runs2 BC_1), nothing filed; default leaves it to Codex's approval; LEVAIN_UNCONFINED=1 passes it."""
    monkeypatch.delenv(cx.UNCONFINED_ENV, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    patch = {"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"}
    event = _event("S1", "apply_patch", patch, str(tmp_path))
    out = json.loads(_run(qdir, event))
    assert "continue" not in out
    spec = out["hookSpecificOutput"]
    assert (spec["permissionDecision"], spec["permissionDecisionReason"]) == (
        "deny", cx.FLOOR_TEXT.format(x="apply_patch of .codex/hooks.json"))
    rows = Ledger(qdir).window("codex", "S1", datetime.now(timezone.utc) - timedelta(days=1))
    assert [(r.phase, (r.stamp_detail or {}).get("decision")) for r in rows] == [("denied_by_gate", "floor")]
    assert _run(qdir, {**event, "permission_mode": "default"}) == ""
    assert "deny" in _run(qdir, {**event, "permission_mode": "acceptEdits"})  # run 4 C5: floored, not attended
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    # gemini r3 MED: a relative or blank cwd anchors nothing, so the event is malformed and denied
    for bad in (".", " ", "ws"):
        spec = json.loads(_run(qdir, {**event, "cwd": bad}))["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny" and "no valid cwd" in spec["permissionDecisionReason"], bad

    # codex r3 HIGH: benign headers in many directories whose git query blocks, then .codex/hooks.json. The gate
    # call's git budget is ONE (the description and the classification share it), a blocked query is cut at the
    # deadline and tags its header, so the patch is denied well inside Codex's hook timeout instead of running
    from levain import host_guard
    from levain.consent.adapters import claude_code as cc

    fake = tmp_path / "fake-git"
    fake.write_text('#!/bin/sh\ncase "$2" in *slow*) exec /bin/sleep 5;; esac\n'
                    'echo "fatal: not a git repository (or any of the parent directories): .git" >&2\nexit 128\n')
    fake.chmod(0o755)
    monkeypatch.setattr(host_guard, "_GIT_BINARY", [str(fake)])
    headers = []
    for i in range(20):
        (tmp_path / f"slow{i}").mkdir()
        (tmp_path / f"slow{i}" / "HEAD").write_text("ref: refs/heads/main\n")   # each could start a repository
        headers.append(f"*** Add File: slow{i}/a.txt\n+x\n")
    many = {"command": "*** Begin Patch\n" + "".join(headers) + "*** Add File: .codex/hooks.json\n+{}\n*** End Patch"}
    started = time.monotonic()
    spec = json.loads(_run(qdir, _event("S9", "apply_patch", many, str(tmp_path))))["hookSpecificOutput"]
    took = time.monotonic() - started
    assert spec["permissionDecision"] == "deny" and took < cc.GIT_BUDGET_SECONDS + 1, took

    monkeypatch.setenv(cx.UNCONFINED_ENV, "1")
    assert _run(qdir, event) == ""
    assert ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), home=h4_home()).pending() == []


def test_a_halted_session_stays_halted_when_started_unconfined(tmp_path, monkeypatch):
    """L1-3: the halt is read before the unconfined check, so LEVAIN_UNCONFINED=1 never releases a halted Codex
    session: a gated call and an ungated one both get HALT_TEXT."""
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv(cx.UNCONFINED_ENV, "1")
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    source = authority_source(qdir, anchor=None)
    patch = {"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"}
    request = ConsentQueue(qdir, authority=source, clock=lambda: t0, home=h4_home()).file(
        agent="codex", agent_session_id="S1", tool_name="apply_patch", tool_input=patch, cwd=str(tmp_path),
        hold=timedelta(seconds=20), tier="T3")
    assert ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21), home=h4_home()).mark_undecided(
        request.id, halt=True).gap == 1
    assert cx.unconfined(str(tmp_path), codex_home=tmp_path / "codex-home", environ=h4_env(), home=h4_home())
    for tool, inp in (("apply_patch", patch), ("Bash", {"command": "ls"})):
        out = json.loads(_run(qdir, _event("S1", tool, inp, str(tmp_path))))
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == cx.HALT_TEXT.format(n=1), tool
    assert _run(qdir, _event("S2", "apply_patch", patch, str(tmp_path))) == ""  # another session is unconfined

    # L3 r1 G2: a halt set AFTER the gate's first read (a sibling call's undecided deny) still wins the unconfined
    # pass and the floor's default/plan pass: each re-checks the halt just before returning
    real = cx.unconfined_state
    for session, mode, unconfined in (("S3", "bypassPermissions", True), ("S4", "default", False),
                                      ("S5", "plan", False)):
        def halting(*a, _s=session, _u=unconfined, **kw):
            r = ConsentQueue(qdir, authority=source, clock=lambda: t0, home=h4_home()).file(
                agent="codex", agent_session_id=_s, tool_name="apply_patch", tool_input=patch, cwd=str(tmp_path),
                hold=timedelta(seconds=20), tier="T3")
            ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21), home=h4_home()).mark_undecided(
                r.id, halt=True)
            return real(*a, **kw) if _u else None
        monkeypatch.setattr(cx, "unconfined_state", halting)
        out = json.loads(_run(qdir, {**_event(session, "apply_patch", patch, str(tmp_path)), "permission_mode": mode}))
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == cx.HALT_TEXT.format(n=1), session


def test_every_codex_gate_call_checks_for_held_phone_pings(tmp_path, monkeypatch):
    """Held phone pings (levain.webpush.held) are delivered without a daemon: this gate calls the check each time too."""
    from pathlib import Path

    calls = []
    monkeypatch.setattr(cx, "flush_held_pings", lambda q: calls.append(q.directory))
    queue = ConsentQueue(tmp_path / "q", authority=authority_source(tmp_path / "q", anchor=None), home=h4_home())
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": "/work/proj", "session_id": "s1", "turn_id": "t1", "tool_use_id": "c1"}
    cx.gate(event, queue, cx.AdapterSettings(), home=Path("/home/u"), codex_dir=Path("/home/u/.codex"), environ=h4_env())
    assert calls == [queue.directory]


def test_a_halted_session_hands_the_gate_notice_its_cwd_and_environ(tmp_path, monkeypatch):
    # The halted branch called _with_rulings without cwd/environ, so the one-time notice was judged (and its claim
    # taken) on ambient state instead of the event's own.
    from levain import band_view

    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    source = authority_source(qdir, anchor=None)
    filed = ConsentQueue(qdir, authority=source, clock=lambda: t0, home=h4_home())
    request = filed.file(agent="codex", agent_session_id="S1", tool_name="apply_patch",
                         tool_input={"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"},
                         cwd=str(tmp_path), hold=timedelta(seconds=20), tier="T3")
    later = ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21), home=h4_home())
    assert later.mark_undecided(request.id, halt=True).gap == 1
    seen = []
    monkeypatch.setattr(band_view, "gate_notice", lambda d, a, s, cwd, environ=None, **kw: seen.append((cwd, environ)))
    _run(qdir, _event("S1", "Bash", {"command": "ls -a"}, str(tmp_path)))
    assert seen and seen[0][0] == str(tmp_path) and seen[0][1] is not None, seen
