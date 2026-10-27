"""TEST ONLY: the PermissionRequest scenarios whose ``decide()`` output is pinned byte for byte (CH-D1c-B: ``decide``
becomes ``decide_ex`` + ``hook_output``, and must say exactly what it said before).

:func:`run_all` drives ``decide`` over every exit of its body with a deterministic fake core and returns each
scenario's output as compact sorted JSON (or ``"RAISE:<type>"``). It uses only names ``decide`` has had since before
the union, so the same function runs against an older checkout: ``tests/fixtures/decide_golden_be0af189.json`` is its
output at be0af189 (written by running this file against that commit), and
``tests/test_consent_core_union.py`` asserts the current code returns the same bytes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SESSION = "ceedbaa9-2e6d-433f-b374-0ad96dd5f62d"
CWD = "/work/proj"


def _event(tool_name="Bash", tool_input=None, **extra):
    e = {"session_id": SESSION, "transcript_path": "/x.jsonl", "cwd": CWD, "permission_mode": "default",
         "hook_event_name": "PermissionRequest", "tool_name": tool_name,
         "tool_input": {"command": "touch x"} if tool_input is None else tool_input,
         "permission_suggestions": [{"type": "addRules", "behavior": "allow"}]}
    e.update(extra)
    return e


def run_all(cc: Any, Decision: Any, ExternalGateRequest: Any, home: Path) -> dict[str, str]:
    environ = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

    class Q:
        directory = Path("/nonexistent/levain-consent")

        def __init__(self, verdict=None, *, at="await", via="signature", take=False):
            self.verdict, self.at, self.via, self.take = verdict, at, via, take
            self.requests: list[Any] = []

        def file(self, **kw):
            r = ExternalGateRequest.create(
                agent=kw["agent"], agent_session_id=kw["agent_session_id"], event_id=kw["event_id"] or "rand:1",
                tool_name=kw["tool_name"], tool_input_digest="jcs:x", display="d", cwd=kw["cwd"], tier=kw["tier"],
                received_at="2026-10-13T20:00:00.000000Z", expires_at="2026-10-13T20:10:00.000000Z")
            self.requests.append(r)
            return r

        def _d(self, request_id):
            if self.verdict is None:
                return None
            digest = next(r.digest for r in self.requests if r.id == request_id)
            if self.via == "grant":
                return Decision(request_id=request_id, decision="allow", signer_fp=None, decided_at="t",
                                request_digest=digest, via="grant", grant_id="g1", grant_seq=3, grant_hash="h",
                                posture="default")
            return Decision(request_id=request_id, decision=self.verdict, signer_fp="SHA256:k", decided_at="t",
                            request_digest=digest)

        def await_decision(self, request_id, timeout, **kw):
            return self._d(request_id) if self.at == "await" else None

        def withdraw(self, request_id, note=""):
            return self.at != "race"

        def decision(self, request_id, **kw):
            return self._d(request_id) if self.at == "race" else None

        def answer_by_grant(self, request_id, **kw):
            return self._d(request_id) if self.at == "grant" else None

        def take_gate_allow(self, agent, session, tool_name, tool_input, **kw):
            if not self.take:
                return None
            r = self.file(agent=agent, agent_session_id=session, tool_name=tool_name, event_id="hook:t", cwd=kw["cwd"],
                          tier="T3")
            return Decision(request_id=r.id, decision="allow", signer_fp="SHA256:k", decided_at="t",
                            request_digest=r.digest)

        def get(self, request_id):
            for r in self.requests:
                if r.id == request_id:
                    return r, "allowed"
            return None

    away = cc.AdapterSettings(away_mode=True, away_hold_seconds=20)
    off = cc.AdapterSettings()
    config_write = ("Write", {"file_path": CWD + "/.claude/settings.json", "content": "{}"})
    cases: dict[str, tuple[Any, Any, Any, dict[str, Any]]] = {
        "allow_at_await": (_event(), Q("allow"), away, {}),
        "deny_at_await": (_event(), Q("deny"), away, {}),
        "silence": (_event(), Q(None), away, {}),
        "race_allow_after_withdraw": (_event(), Q("allow", at="race"), away, {}),
        "grant_answer_says_so": (_event("Write", {"file_path": CWD + "/a.txt", "content": "x"}),
                                 Q("allow", at="grant", via="grant"), away, {}),
        "not_a_mapping": (["PermissionRequest"], Q("allow"), away, {}),
        "other_hook_event": (_event(hook_event_name="PreToolUse"), Q("allow"), away, {}),
        "no_cwd": (_event(cwd=""), Q("allow"), away, {}),
        "no_session": (_event(session_id=None), Q("allow"), away, {}),
        "tool_name_not_str": (_event(tool_name=7), Q("allow"), away, {}),
        "mcp_without_input": (_event("mcp__fs__list", tool_input=None), Q("allow"), away, {}),
        "subagent": (_event(agent_id="a1", tool_use_id="toolu_1"), Q("allow"), away, {}),
        "nothing_can_decide": (_event(), Q("allow"), off, {}),
        "unconfined_agent_config": (_event(*config_write), Q("allow"), away,
                                    {"environ": {**environ, "LEVAIN_UNCONFINED": "1"}}),
        "agent_config_take": (_event(*config_write), Q(None, take=True), away, {}),
        "agent_config_signed": (_event(*config_write), Q("allow"), away, {}),
        "escape_key_on_hook_path": (_event(tool_input={"command": "ls", "dangerouslyDisableSandbox": True}),
                                    Q("allow"), away, {}),
        "sandbox_net_on_hook_path": (_event("SandboxNetworkAccess", {"host": "127.0.0.1"}), Q("allow"), away, {}),
        "zero_hold_deny": (_event(), Q("deny"), away, {"max_hold": 0}),
    }
    out: dict[str, str] = {}
    for name, (event, queue, settings, kw) in cases.items():
        kw = {"environ": environ, "home": home, "idle": lambda: None, **kw}
        try:
            got = cc.decide(event, queue, settings, **kw)
        except Exception as exc:  # noqa: BLE001 - the raise is part of what is pinned
            out[name] = "RAISE:" + type(exc).__name__
            continue
        out[name] = json.dumps(got, sort_keys=True, separators=(",", ":"))
    return out
