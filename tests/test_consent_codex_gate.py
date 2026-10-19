"""Codex's PreToolUse gate (Q14 B5): the per-session latch and the hook trust levain writes."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from levain.consent import ConsentQueue, authority_source
from levain.consent.adapters import codex as cx
from levain.consent.ledger import Ledger


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
    filed = ConsentQueue(qdir, authority=source, clock=lambda: t0)
    request = filed.file(agent="codex", agent_session_id="S1", tool_name="apply_patch",
                         tool_input={"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"},
                         cwd=str(tmp_path), hold=timedelta(seconds=20), tier="T3")
    later = ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21))
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
    but default (and acceptEdits) gets the floor's PLAIN deny (Codex drops a deny that carries continue:false,
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
    assert _run(qdir, {**event, "permission_mode": "acceptEdits"}) == ""  # L2-3: treated as default
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(cx.UNCONFINED_ENV, "1")
    assert _run(qdir, event) == ""
    assert ConsentQueue(qdir, authority=authority_source(qdir, anchor=None)).pending() == []


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
    request = ConsentQueue(qdir, authority=source, clock=lambda: t0).file(
        agent="codex", agent_session_id="S1", tool_name="apply_patch", tool_input=patch, cwd=str(tmp_path),
        hold=timedelta(seconds=20), tier="T3")
    assert ConsentQueue(qdir, authority=source, clock=lambda: t0 + timedelta(seconds=21)).mark_undecided(
        request.id, halt=True).gap == 1
    assert cx.unconfined(str(tmp_path), codex_home=tmp_path / "codex-home")
    for tool, inp in (("apply_patch", patch), ("Bash", {"command": "ls"})):
        out = json.loads(_run(qdir, _event("S1", tool, inp, str(tmp_path))))
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == cx.HALT_TEXT.format(n=1), tool
    assert _run(qdir, _event("S2", "apply_patch", patch, str(tmp_path))) == ""  # another session is unconfined
