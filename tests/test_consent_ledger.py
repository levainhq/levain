"""The authority ledger (Q15): the append-only store, the stamp readers and the reconciler, driven by REAL
fixtures (tests/fixtures/ledger/): tool_use/tool_result line pairs copied from scratch Claude Code runs (10-17) and
two Codex rollouts + hook payloads from scratch ``codex exec`` runs (10-18, one read-only, one bypass)."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cli import main as levain_main
from levain.consent import stamps
from levain.consent.ledger import STORE_FORMAT, Ledger, LedgerStoreError, parse_when, reconcile
from levain.consent.record import tool_input_digest
from tests._h4 import h4_home

FIX = Path(__file__).parent / "fixtures" / "ledger"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
META = json.loads((FIX / "cc_pairs.meta.json").read_text())
BY_CASE = {}
for _m in META:
    BY_CASE.setdefault(_m["case"], []).append(_m)


def _lines(name: str) -> list[str]:
    return (FIX / name).read_text().splitlines(keepends=True)


def _ledger(tmp_path: Path, clock=lambda: NOW) -> Ledger:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return Ledger(d, clock=clock)


def _cc_row(ledger: Ledger, tool_use_id: str, path: Path | str | None, *, mode: str | None = "bypassPermissions",
            session: str = "s-cc", at: str | datetime | None = None, **kw) -> str:
    return ledger.record_call(agent="claude_code", agent_session_id=session, tool_name="Bash",
                              tool_input={"command": "echo x"}, cwd="/w", phase="post", tool_use_id=tool_use_id,
                              source_path=str(path) if path else None, permission_mode=mode, received_at=at, **kw)


# --- the readers, on real Claude Code lines ---------------------------------------------------------------
def test_fixture_is_real_shape():
    assert len(META) == 10 and (FIX / "cc_pairs.jsonl").stat().st_size < 100_000
    # every fixture result carries the key spellings the reader depends on
    for m in META:
        assert set(m["permissionDecision"]) <= {"decision", "source", "reasonType"}


@pytest.mark.parametrize("case,expect_stamp", [
    ("R3-bypass-proj", "bypass"),
    ("R3TX-cases-BG1-proj", "classifier"),
    ("R3-allowrule-proj", "own_rules"),
])
def test_decision_found_and_stamped(case, expect_stamp):
    for m in BY_CASE[case]:
        got = stamps.claude_decision(FIX / "cc_pairs.jsonl", m["tool_use_id"])
        assert got == m["permissionDecision"]
        assert stamps.claude_stamp(got, "bypassPermissions") == expect_stamp


def test_refusals_keep_their_decision():
    hook = BY_CASE["R3-denied-proj"][0]
    got = stamps.claude_decision(FIX / "cc_pairs.jsonl", hook["tool_use_id"])
    assert got["decision"] == "reject" and got["reasonType"] == "hook"
    # a refusal authorised nothing: no authority stamp, the outcome and the detail say who refused
    assert stamps.claude_stamp(got, "bypassPermissions") == "unknown" and stamps.claude_outcome(got) == "refused"
    cfg = stamps.claude_decision(FIX / "cc_pairs.jsonl", BY_CASE["R3-allowrule2-proj"][0]["tool_use_id"])
    assert cfg == {"decision": "reject", "source": "config"}
    assert stamps.claude_stamp(cfg, "default") == "unknown" and stamps.claude_outcome(cfg) == "refused"


def test_claude_stamp_mapping():
    st = stamps.claude_stamp
    assert st({"decision": "accept", "source": "config", "reasonType": "mode"}, "bypassPermissions") == "bypass"
    # mode allowed it, but the session was not in bypass: the mode's own rules, not bypass
    assert st({"decision": "accept", "source": "config", "reasonType": "mode"}, "acceptEdits") == "own_rules"
    assert st({"decision": "accept", "source": "user_permanent"}, "default") == "dialog_unsigned"
    assert st({"decision": "accept", "source": "user_temporary", "reasonType": "x"}, None) == "dialog_unsigned"
    assert st({"decision": "accept", "source": "hook", "reasonType": "hook"}, "default") == "hook"
    assert st({"decision": "accept", "source": "config", "reasonType": "rule"}, "default") == "own_rules"
    assert st({"decision": "accept", "source": "config", "reasonType": "classifier"}, "auto") == "classifier"
    # a reject whose reason is the mode, under bypass, is a refusal and never bypass authority
    assert st({"decision": "reject", "source": "config", "reasonType": "mode"}, "bypassPermissions") == "unknown"
    assert stamps.claude_outcome({"decision": "reject", "reasonType": "mode"}) == "refused"
    assert stamps.claude_outcome({"decision": "accept"}) == "accepted" and stamps.claude_outcome(None) == "unknown"
    assert st(None, "bypassPermissions") == "unknown"
    assert st({}, "bypassPermissions") == "unknown"
    assert st({"decision": "accept", "source": "mystery", "reasonType": "mystery"}, "default") == "unknown"


def test_missing_file_and_missing_line_are_unknown_not_allow(tmp_path):
    some_id = META[0]["tool_use_id"]
    assert stamps.claude_decision(tmp_path / "absent.jsonl", some_id) is None
    assert stamps.claude_result_state(tmp_path / "absent.jsonl", some_id) == ("missing_file", None)
    assert stamps.claude_result_state(None, some_id) == ("no_source", None)
    assert stamps.claude_result_state(FIX / "cc_pairs.jsonl", None) == ("no_tool_use_id", None)
    assert stamps.claude_result_state(tmp_path, some_id) == ("unreadable", None)            # a directory: an OSError
    assert stamps.claude_result_state(FIX / "cc_pairs.jsonl", "toolu_nope") == ("no_result", None)


def test_partial_last_line_is_tolerated(tmp_path):
    m = META[0]
    lines = _lines("cc_pairs.jsonl")
    ids = [i for i, l in enumerate(lines) if m["tool_use_id"] in l]
    use_i, res_i = ids[0], ids[-1]
    p = tmp_path / "t.jsonl"
    p.write_text("".join(lines[: res_i]) + lines[res_i][:200])           # the result line half written
    assert use_i < res_i
    assert stamps.claude_result_state(p, m["tool_use_id"])[0] == "no_result"
    p.write_text("".join(lines[: res_i + 1]))
    assert stamps.claude_decision(p, m["tool_use_id"]) == m["permissionDecision"]


def test_read_is_bounded(tmp_path):
    m = META[0]
    assert stamps.claude_result_state(FIX / "cc_pairs.jsonl", m["tool_use_id"], max_bytes=50) == ("no_result", None)


# --- the readers, on real Codex rollouts ------------------------------------------------------------------
def _codex_turn(name: str) -> str:
    return json.loads(_lines(name)[0])["turn_id"]


def test_codex_sandbox_vs_bypass():
    ro = stamps.codex_turn_policy(FIX / "codex_readonly_rollout.jsonl", _codex_turn("codex_readonly_hooks.jsonl"))
    by = stamps.codex_turn_policy(FIX / "codex_bypass_rollout.jsonl", _codex_turn("codex_bypass_hooks.jsonl"))
    assert ro["approval_policy"] == "never" and ro["sandbox_policy"] == {"type": "read-only"}
    assert by["sandbox_policy"] == {"type": "danger-full-access"}
    assert set(ro) == {"approval_policy", "sandbox_policy", "permission_profile", "approvals_reviewer"}
    assert stamps.codex_stamp(ro) == "sandbox" and stamps.codex_stamp(by) == "bypass"
    # no turn given: the last turn recorded; a turn that is not there: None
    assert stamps.codex_turn_policy(FIX / "codex_readonly_rollout.jsonl") == ro
    assert stamps.codex_turn_policy(FIX / "codex_readonly_rollout.jsonl", "no-such-turn") is None
    assert stamps.codex_turn_policy(FIX / "no-file.jsonl", None) is None


def test_codex_policy_keeps_only_the_whitelisted_shape(tmp_path):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text(json.dumps({"type": "turn_context", "payload": {
        "turn_id": "t1", "approval_policy": {"granular": {"sandbox_approval": True}}, "approvals_reviewer": "user",
        "sandbox_policy": {"type": "workspace-write", "writable_roots": ["/home/someone/secret"]},
        "permission_profile": {"type": "managed", "paths": ["/x"]}, "cwd": "/home/someone"}}) + "\n")
    got = stamps.codex_turn_policy(rollout, "t1")
    assert got == {"approval_policy": "granular", "sandbox_policy": {"type": "workspace-write"},
                   "permission_profile": {"type": "managed"}, "approvals_reviewer": "user"}
    assert b"someone" not in json.dumps(got).encode() and stamps.codex_stamp(got) == "unknown"


def test_codex_stamp_mapping_and_uncovered_cases():
    cs = stamps.codex_stamp
    # a policy that can ask: the call may have been escalated and approved by the user, and the rollout records no
    # per-call approval, so the stamp is unknown and never own_rules
    for ask in ("on-request", "untrusted", "on-failure"):
        assert cs({"approval_policy": ask, "sandbox_policy": {"type": "workspace-write"},
                   "approvals_reviewer": "user"}) == "unknown"
    assert cs({"approval_policy": "never", "sandbox_policy": {"type": "workspace-write"}}) == "sandbox"
    # not covered by the mapping: unknown, never a guessed authority
    assert cs({"approval_policy": {"granular": {}}, "sandbox_policy": {"type": "read-only"}}) == "unknown"
    assert cs({"approval_policy": "never", "sandbox_policy": {"type": "novel"}}) == "unknown"
    assert cs({"approval_policy": "never", "sandbox_policy": {"type": "read-only"},
               "approvals_reviewer": "auto_review"}) == "unknown"
    assert cs(None) == "unknown"


def test_codex_hook_payload_names_its_rollout_and_turn():
    for tag in ("readonly", "bypass"):
        recs = [json.loads(l) for l in _lines(f"codex_{tag}_hooks.jsonl")]
        assert {r["hook_event_name"] for r in recs} == {"PreToolUse", "PostToolUse"}
        for r in recs:
            assert {"session_id", "turn_id", "transcript_path", "tool_use_id", "cwd", "tool_name", "tool_input",
                    "permission_mode", "model"} <= set(r)
        # the payload's permission_mode is NOT the sandbox: both runs read bypassPermissions
        assert {r["permission_mode"] for r in recs} == {"bypassPermissions"}


# --- the store ------------------------------------------------------------------------------------------
def test_store_is_private_and_keeps_digests_only(tmp_path):
    ledger = _ledger(tmp_path)
    secret = "echo SUPER-SECRET-COMMAND-TEXT"
    row = ledger.record_call(agent="claude_code", agent_session_id="s", tool_name="Bash",
                             tool_input={"command": secret}, cwd="/w", phase="post", tool_use_id="t1")
    call = ledger.get(row)
    assert call.tool_input_digest == tool_input_digest({"command": secret}) and call.stamp == "pending"
    assert stat.S_IMODE(os.stat(ledger.path).st_mode) == 0o600
    blob = b"".join(p.read_bytes() for p in ledger.directory.iterdir())
    assert b"SUPER-SECRET" not in blob
    with sqlite3.connect(ledger.path) as c:
        assert c.execute("SELECT value FROM meta WHERE key='format'").fetchone()[0] == STORE_FORMAT


def test_directory_reachable_by_others_is_refused(tmp_path):
    d = tmp_path / "open"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(LedgerStoreError):
        Ledger(d).counts()


def test_foreign_format_is_refused(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.counts()
    with sqlite3.connect(ledger.path) as c:
        c.execute("UPDATE meta SET value='something-else' WHERE key='format'")
    with pytest.raises(LedgerStoreError):
        Ledger(ledger.directory).counts()


def test_record_call_is_idempotent_per_call_and_phase(tmp_path):
    ledger = _ledger(tmp_path)
    a = _cc_row(ledger, "t1", None)
    assert _cc_row(ledger, "t1", None) == a
    assert ledger.record_call(agent="claude_code", agent_session_id="s-cc", tool_name="Bash", tool_input={}, cwd="/w",
                              phase="post_failure", tool_use_id="t1") != a
    # no tool_use_id: every filing is its own row
    n1 = ledger.record_call(agent="codex", agent_session_id="x", tool_name="Bash", tool_input={}, cwd="/w", phase="pre")
    n2 = ledger.record_call(agent="codex", agent_session_id="x", tool_name="Bash", tool_input={}, cwd="/w", phase="pre")
    assert n1 != n2
    assert sum(n for *_, n in ledger.counts()) == 4


def test_bad_values_are_refused(tmp_path):
    ledger = _ledger(tmp_path)
    for kw in ({"agent": "nobody"}, {"phase": "later"}, {"stamp": "vibes"}):
        args = dict(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w", phase="pre")
        args.update(kw)
        with pytest.raises(ValueError):
            ledger.record_call(**args)


def test_append_only_triggers_refuse_edits_and_deletes(tmp_path):
    ledger = _ledger(tmp_path)
    final = ledger.record_call(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w",
                               phase="pre", tool_use_id="a", stamp="grant", request_id="req-1")
    pending = _cc_row(ledger, "b", None)
    with sqlite3.connect(ledger.path) as c:
        for sql, args in (
            ("UPDATE calls SET tool_name='Edit' WHERE id=?", (final,)),
            ("UPDATE calls SET stamp='bypass' WHERE id=?", (final,)),                       # a final stamp is final
            ("UPDATE calls SET stamp_detail='{}' WHERE id=?", (final,)),
            ("UPDATE calls SET tool_name='Edit' WHERE id=?", (pending,)),                    # a pending row's content
            ("UPDATE calls SET request_id='r' WHERE id=?", (pending,)),
            ("UPDATE calls SET stamp='bypass', stamped_at='2026-10-18T00:00:00.000000Z', tool_input_digest='x' "
             "WHERE id=?", (pending,)),                                                       # stamp + another change
            ("DELETE FROM calls WHERE id=?", (final,)),
            ("DELETE FROM calls WHERE id=?", (pending,)),
        ):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql, args)


def test_pending_to_final_exactly_once(tmp_path):
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "t1", None)
    assert ledger.fill_stamp(row, "bypass", {"decision": "accept"}) is True
    done = ledger.get(row)
    assert done.stamp == "bypass" and done.stamp_detail == {"decision": "accept"} and done.stamped_at
    assert ledger.fill_stamp(row, "own_rules") is False                                 # once only
    assert ledger.get(row).stamp == "bypass"
    assert ledger.fill_stamp("lc-missing", "bypass") is False
    with pytest.raises(ValueError):
        ledger.fill_stamp(row, "pending")


def test_pending_for_lists_only_that_sessions_open_rows(tmp_path):
    ledger = _ledger(tmp_path)
    a = _cc_row(ledger, "t1", None, session="one")
    _cc_row(ledger, "t2", None, session="two")
    b = _cc_row(ledger, "t3", None, session="one")
    ledger.fill_stamp(b, "bypass")
    assert [c.id for c in ledger.pending_for("claude_code", "one")] == [a]


# --- the views ------------------------------------------------------------------------------------------
def test_digest_returns_grant_and_ruling_rows_only(tmp_path):
    ledger = _ledger(tmp_path)
    for i, stamp in enumerate(("grant", "answer", "ruling", "own_rules", "bypass", "classifier", "unknown")):
        _cc_row(ledger, f"t{i}", None, stamp=stamp, at=NOW - timedelta(hours=1, minutes=i))
    _cc_row(ledger, "old", None, stamp="grant", at=NOW - timedelta(days=30))
    got = ledger.digest("7d")
    assert sorted(c.stamp for c in got) == ["grant", "ruling"]
    assert len(ledger.digest("90d")) == 3


def test_window_is_one_sessions_rows_between_two_times(tmp_path):
    ledger = _ledger(tmp_path)
    for i in range(6):
        _cc_row(ledger, f"t{i}", None, session="s1", at=NOW - timedelta(minutes=60 - 10 * i))
    _cc_row(ledger, "o", None, session="s2", at=NOW - timedelta(minutes=35))
    got = ledger.window("claude_code", "s1", NOW - timedelta(minutes=35), NOW - timedelta(minutes=15))
    assert [c.tool_use_id for c in got] == ["t3", "t4"]
    assert ledger.window("claude_code", "s2", "1h")[0].tool_use_id == "o"


def test_counts_by_agent_and_stamp(tmp_path):
    ledger = _ledger(tmp_path)
    _cc_row(ledger, "a", None, stamp="bypass")
    _cc_row(ledger, "b", None, stamp="bypass")
    ledger.record_call(agent="codex", agent_session_id="x", tool_name="Bash", tool_input={}, cwd="/w", phase="post",
                       stamp="sandbox")
    assert ledger.counts() == [("claude_code", "bypass", "accepted", 2), ("codex", "sandbox", "accepted", 1)]
    assert ledger.counts(since="1h", until=NOW - timedelta(minutes=30)) == []


def test_parse_when():
    assert parse_when("7d", now=NOW) == NOW - timedelta(days=7)
    assert parse_when("36h", now=NOW) == NOW - timedelta(hours=36)
    assert parse_when("2026-10-17") == datetime(2026, 10, 17, tzinfo=timezone.utc)
    assert parse_when("2026-10-17T05:00:00Z") == datetime(2026, 10, 17, 5, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_when(datetime(2026, 1, 1))


# --- reconcile ------------------------------------------------------------------------------------------
def test_reconcile_fills_pending_when_the_decision_lands(tmp_path):
    m = BY_CASE["R3TX-cases-BG1-proj"][0]
    lines = _lines("cc_pairs.jsonl")
    res_i = max(i for i, l in enumerate(lines) if m["tool_use_id"] in l)
    transcript = tmp_path / "live.jsonl"
    transcript.write_text("".join(lines[:res_i]))                         # the call is there, its result is not
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, m["tool_use_id"], transcript, mode="auto", at=NOW)
    assert reconcile(ledger) == {"filled": 0, "unknown": 0, "still_pending": 1, "already_stamped": 0}
    assert ledger.get(row).stamp == "pending"
    transcript.write_text("".join(lines[: res_i + 1]))                    # the next hook finds it written
    assert reconcile(ledger) == {"filled": 1, "unknown": 0, "still_pending": 0, "already_stamped": 0}
    done = ledger.get(row)
    assert done.stamp == "classifier" and done.stamp_detail == m["permissionDecision"]


def test_reconcile_missing_file_is_unknown(tmp_path):
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "t1", tmp_path / "never-written.jsonl")
    nosrc = _cc_row(ledger, "t2", None)
    # a transcript that does not exist YET stays pending; a row with no locator at all can never be answered
    assert reconcile(ledger) == {"filled": 1, "unknown": 1, "still_pending": 1, "already_stamped": 0}
    assert ledger.get(row).stamp == "pending" and ledger.get(nosrc).stamp_detail == {"reason": "no_source"}
    assert reconcile(ledger, give_up_after=timedelta(0))["unknown"] == 1
    assert ledger.get(row).stamp == "unknown"
    assert ledger.get(row).stamp_detail == {"reason": "transcript_missing", "gave_up": True}


def test_reconcile_gives_up_on_a_line_that_never_comes(tmp_path):
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "toolu_absent", FIX / "cc_pairs.jsonl", at=NOW - timedelta(hours=3))
    young = _cc_row(ledger, "toolu_young", FIX / "cc_pairs.jsonl", at=NOW - timedelta(minutes=2))
    assert reconcile(ledger) == {"filled": 1, "unknown": 1, "still_pending": 1, "already_stamped": 0}
    assert ledger.get(row).stamp == "unknown" and ledger.get(young).stamp == "pending"
    assert reconcile(ledger, give_up_after=None)["still_pending"] == 1


def test_reconcile_result_without_decision_is_unknown(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]}}) + "\n")
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "toolu_x", t)
    reconcile(ledger)
    assert ledger.get(row).stamp == "unknown"
    assert ledger.get(row).stamp_detail == {"reason": "result_without_decision"}


def test_reconcile_codex_rows_from_the_rollout(tmp_path):
    ledger = _ledger(tmp_path)
    rows = {}
    for tag in ("readonly", "bypass"):
        pre = json.loads(_lines(f"codex_{tag}_hooks.jsonl")[0])
        rows[tag] = ledger.record_call(
            agent="codex", agent_session_id=pre["session_id"], tool_name=pre["tool_name"], tool_input=pre["tool_input"],
            cwd=pre["cwd"], phase="post", tool_use_id=pre["tool_use_id"], source_path=str(FIX / f"codex_{tag}_rollout.jsonl"),
            turn_id=pre["turn_id"], permission_mode=pre["permission_mode"])
    gone = ledger.record_call(agent="codex", agent_session_id="g", tool_name="Bash", tool_input={}, cwd="/w",
                              phase="pre", tool_use_id="gone", source_path=str(tmp_path / "no-rollout.jsonl"),
                              turn_id="t")
    assert reconcile(ledger) == {"filled": 2, "unknown": 0, "still_pending": 1, "already_stamped": 0}
    assert reconcile(ledger, give_up_after=timedelta(0))["unknown"] == 1
    assert ledger.get(gone).stamp_detail == {"reason": "rollout_missing", "gave_up": True}
    # the hook payload says bypassPermissions for both; the rollout's turn policy is what decides
    assert ledger.get(rows["readonly"]).stamp == "sandbox"
    assert ledger.get(rows["bypass"]).stamp == "bypass"
    assert ledger.get(gone).stamp == "unknown"


def test_reconcile_limits_to_one_session(tmp_path):
    ledger = _ledger(tmp_path)
    _cc_row(ledger, "t1", None, session="a")
    _cc_row(ledger, "t2", None, session="b")
    assert reconcile(ledger, agent="claude_code", agent_session_id="a")["filled"] == 1
    assert [c.agent_session_id for c in ledger.pending()] == ["b"]
    # a session filter alone is honoured too (it used to be ignored unless the agent was also given)
    _cc_row(ledger, "t3", None, session="c")
    assert reconcile(ledger, agent_session_id="c")["filled"] == 1
    assert [c.agent_session_id for c in ledger.pending()] == ["b"]


# --- the command ----------------------------------------------------------------------------------------
def test_cli_counts_digest_window_reconcile(tmp_path, capsys):
    ledger = Ledger(tmp_path / "consent")
    ledger.counts()
    os.chmod(tmp_path / "consent", 0o700)
    now = datetime.now(timezone.utc)
    ledger.record_call(agent="codex", agent_session_id="sess-1", tool_name="Bash", tool_input={}, cwd="/w",
                       phase="post", stamp="grant", received_at=now - timedelta(minutes=5), request_id="r1")
    ledger.record_call(agent="codex", agent_session_id="sess-1", tool_name="Bash", tool_input={}, cwd="/w",
                       phase="post")
    q = ["--queue-dir", str(tmp_path / "consent")]
    assert levain_main(["consent", "ledger", "counts", *q, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"agent": "codex", "stamp": "grant", "outcome": "accepted", "rows": 1},
        {"agent": "codex", "stamp": "pending", "outcome": "unknown", "rows": 1}]
    assert levain_main(["consent", "ledger", "digest", "--since", "7d", *q, "--json"]) == 0
    assert [c["stamp"] for c in json.loads(capsys.readouterr().out)] == ["grant"]
    assert levain_main(["consent", "ledger", "window", "--agent", "codex", "--session", "sess-1", "--since", "1h",
                        *q, "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert levain_main(["consent", "ledger", "reconcile", *q]) == 0
    assert "filled 1 (1 unknown)" in capsys.readouterr().out
    assert levain_main(["consent", "ledger", "counts", *q]) == 0
    assert "unknown" in capsys.readouterr().out


def test_every_fixture_call_stamps_to_the_hand_count(tmp_path):
    """Ingest each real call as a pending row, reconcile, and match the count worked out by hand from the fixture
    metadata (the DONE line: stamps matching a hand count of the transcript)."""
    ledger = _ledger(tmp_path)
    for m in META:
        mode = "auto" if m["case"].startswith("R3TX") else "bypassPermissions"
        _cc_row(ledger, m["tool_use_id"], FIX / "cc_pairs.jsonl", mode=mode, session=m["session"], at=NOW)
    assert reconcile(ledger) == {"filled": 10, "unknown": 3, "still_pending": 0, "already_stamped": 0}
    # hand count: 2 bypass, 2 classifier, 2 subcommandResults(own_rules), 2 config rejects(own_rules), 1 hook reject,
    # 1 bypass accept after the hook reject
    # refusals (1 hook reject, 2 config rejects) carry no authority stamp: unknown + refused
    assert ledger.counts() == [("claude_code", "bypass", "accepted", 3), ("claude_code", "classifier", "accepted", 2),
                               ("claude_code", "own_rules", "accepted", 2), ("claude_code", "unknown", "refused", 3)]


# --- the fix round (B3 L1 + L3 r1) -----------------------------------------------------------------------
def _codex_row(ledger, rollout, *, turn_id, tool_use_id="c1", at=NOW):
    return ledger.record_call(agent="codex", agent_session_id="cs", tool_name="Bash", tool_input={}, cwd="/w",
                              phase="pre", tool_use_id=tool_use_id, source_path=str(rollout), turn_id=turn_id,
                              received_at=at)


def _turn_context(turn, approval, sandbox):
    return json.dumps({"type": "turn_context", "payload": {"turn_id": turn, "approval_policy": approval,
                                                           "approvals_reviewer": "user",
                                                           "sandbox_policy": {"type": sandbox}}}) + "\n"


def test_codex_row_without_turn_id_is_never_stamped_from_the_last_turn(tmp_path):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text(_turn_context("t1", "never", "read-only") + _turn_context("t2", "never", "danger-full-access"))
    ledger = _ledger(tmp_path)
    row = _codex_row(ledger, rollout, turn_id=None)
    assert reconcile(ledger) == {"filled": 0, "unknown": 0, "still_pending": 1, "already_stamped": 0}
    assert ledger.get(row).stamp == "pending"
    assert reconcile(ledger, give_up_after=timedelta(0))["unknown"] == 1
    assert ledger.get(row).stamp == "unknown" and ledger.get(row).stamp_detail == {"reason": "no_turn_id",
                                                                                 "gave_up": True}
    # the explicit option keeps the old last-turn reading
    row2 = _codex_row(ledger, rollout, turn_id=None, tool_use_id="c2")
    assert reconcile(ledger, codex_last_turn=True)["filled"] == 1 and ledger.get(row2).stamp == "bypass"


def test_codex_call_in_a_turn_that_can_ask_is_unknown_never_own_rules(tmp_path):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text(_turn_context("t1", "on-request", "workspace-write"))
    ledger = _ledger(tmp_path)
    row = _codex_row(ledger, rollout, turn_id="t1")
    reconcile(ledger)
    done = ledger.get(row)
    assert done.stamp == "unknown" and done.stamp_detail["approval_policy"] == "on-request"


def test_refusal_never_holds_an_authority_stamp(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.record_call(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w",
                           phase="denied_by_gate", stamp="bypass", outcome="refused")
    row = ledger.record_call(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w",
                             phase="denied_by_gate", stamp="unknown")
    assert ledger.get(row).outcome == "refused"
    pending = _cc_row(ledger, "t1", None)
    with pytest.raises(ValueError):
        ledger.fill_stamp(pending, "hook", outcome="refused")
    with sqlite3.connect(ledger.path) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE calls SET stamp='hook', outcome='refused', stamped_at='2026-10-18T00:00:00.000000Z' "
                  "WHERE id=?", (pending,))


def test_reconcile_stamps_a_refusal_unknown_with_outcome_refused(tmp_path):
    hook = BY_CASE["R3-denied-proj"][0]
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, hook["tool_use_id"], FIX / "cc_pairs.jsonl")
    reconcile(ledger)
    done = ledger.get(row)
    assert (done.stamp, done.outcome) == ("unknown", "refused")
    assert done.stamp_detail == {"decision": "reject", "source": "hook", "reasonType": "hook"}


def test_reconcile_keeps_a_row_filed_refused_as_refused(tmp_path):
    ledger = _ledger(tmp_path)
    ids = [h["tool_use_id"] for h in sum(BY_CASE.values(), []) if h.get("tool_use_id")]
    rows = [ledger.record_call(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w",
                               phase="pre", tool_use_id=t, source_path=str(FIX / "cc_pairs.jsonl"),
                               outcome="refused") for t in ids]
    reconcile(ledger)
    done = [ledger.get(r) for r in rows]
    assert done and all(d.outcome == "refused" and d.stamp in ("unknown", "pending") for d in done)
    assert any(d.stamp == "unknown" for d in done)


def test_stamp_detail_is_whitelisted_and_no_free_text_reaches_the_db(tmp_path):
    ledger = _ledger(tmp_path)
    bait = "FREE-TEXT-LEAK-MARKER"
    decision = {"decision": "accept", "source": "config", "reasonType": "mode", "message": bait,
                "updatedInput": {"command": bait}, "ruleContent": bait, "reason": bait,
                "sandbox_policy": {"type": "read-only", "writable_roots": [bait]}}
    a = ledger.record_call(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={}, cwd="/w",
                           phase="post", tool_use_id="a", stamp="bypass", stamp_detail=decision)
    b = _cc_row(ledger, "b", None)
    ledger.fill_stamp(b, "bypass", decision)
    for row in (a, b):
        assert ledger.get(row).stamp_detail == {"decision": "accept", "source": "config", "reasonType": "mode",
                                                "sandbox_policy": {"type": "read-only"}}
    ledger.counts()
    blob = b"".join(p.read_bytes() for p in ledger.directory.iterdir())               # the WAL included
    assert bait.encode() not in blob


def test_record_call_validates_its_fields_up_front(tmp_path):
    ledger = _ledger(tmp_path)
    base = dict(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={}, phase="post")
    for tool_use_id in (None, "t1"):
        for bad in (None, "", 7):
            with pytest.raises(ValueError):
                ledger.record_call(**base, cwd=bad, tool_use_id=tool_use_id)
    with pytest.raises(ValueError):
        ledger.record_call(**base, cwd="/w", tool_use_id="")
    assert ledger.counts() == []                       # nothing phantom was filed


def test_record_call_dedup_is_only_the_idempotency_index(tmp_path):
    ledger = _ledger(tmp_path)
    first = _cc_row(ledger, "t1", None)
    assert _cc_row(ledger, "t1", None) == first
    with sqlite3.connect(ledger.path) as c:                  # INSERT OR REPLACE by id cannot rewrite a row
        cols = "id, agent, agent_session_id, received_at, tool_name, tool_input_digest, cwd, phase, stamp, outcome"
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"INSERT OR REPLACE INTO calls ({cols}) VALUES (?, 'codex', 'x', 'y', 'Edit', 'd', '/z', 'pre', "
                      "'unknown', 'unknown')", (first,))
    assert ledger.get(first).tool_name == "Bash"


def test_relative_ages_follow_the_ledger_clock(tmp_path):
    ledger = _ledger(tmp_path, clock=lambda: datetime(2031, 1, 1, tzinfo=timezone.utc))
    _cc_row(ledger, "t1", None, at=datetime(2030, 12, 31, 23, 0, tzinfo=timezone.utc))
    assert len(ledger.window("claude_code", "s-cc", "2h")) == 1
    assert ledger.digest("2h") == [] and ledger.counts("2h") == [("claude_code", "pending", "unknown", 1)]
    with pytest.raises(ValueError):
        ledger.digest(None)
    with pytest.raises(ValueError):
        parse_when("99999999999d")
    with pytest.raises(ValueError):
        parse_when("\u00b2d")


def test_transient_oserror_and_a_late_transcript_stay_pending(tmp_path):
    m = BY_CASE["R3TX-cases-BG1-proj"][0]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text((FIX / "cc_pairs.jsonl").read_text())
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, m["tool_use_id"], transcript, mode="auto")
    os.chmod(transcript, 0)
    try:
        assert reconcile(ledger)["still_pending"] == 1 and ledger.get(row).stamp == "pending"
        assert reconcile(ledger, give_up_after=timedelta(0))["unknown"] == 1
    finally:
        os.chmod(transcript, 0o600)
    assert ledger.get(row).stamp_detail == {"reason": "transcript_unreadable", "gave_up": True}


def test_reader_skips_overlong_and_non_utf8_lines_and_prefers_the_copy_with_a_decision(tmp_path):
    m = BY_CASE["R3-bypass-proj"][0]
    lines = _lines("cc_pairs.jsonl")
    res = next(l for l in lines if m["tool_use_id"] in l and '"tool_result"' in l)
    bare = json.loads(res)
    bare.pop("permissionDecision")
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"x" * 5000 + b"\n"                                            # over-long, no id
                  + (m["tool_use_id"]).encode() + b" \xff\xfe not utf8\n"      # the id on a non-UTF8 line
                  + (json.dumps(bare) + "\n").encode()                           # a copy WITHOUT a decision
                  + res.encode())                                                 # a later copy WITH one
    scan = stamps.claude_scan(t, [m["tool_use_id"]], max_line_bytes=4096)
    assert scan.states[m["tool_use_id"]] == ("decided", m["permissionDecision"])
    assert scan.skipped == {"too_long": 1, "not_utf8": 1, "not_json": 0}
    only_bare = tmp_path / "b.jsonl"
    only_bare.write_text(json.dumps(bare) + "\n")
    assert stamps.claude_result_state(only_bare, m["tool_use_id"]) == ("no_decision", None)


def test_overlong_line_is_never_held_whole(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"a" * (3 << 20) + b"\n" + b'{"type":"user"}\n')
    seen = list(stamps._lines(t, 1 << 30, 1 << 20, dict.fromkeys(stamps.SKIP_REASONS, 0)))
    assert seen == [b'{"type":"user"}\n']


def test_reconcile_scans_one_transcript_once_per_pass(tmp_path, monkeypatch):
    calls = []
    real = stamps.claude_scan
    monkeypatch.setattr(stamps, "claude_scan", lambda path, ids, **kw: (calls.append((str(path), list(ids))),
                                                                       real(path, ids, **kw))[1])
    ledger = _ledger(tmp_path)
    for m in META:
        _cc_row(ledger, m["tool_use_id"], FIX / "cc_pairs.jsonl", session=m["session"])
    assert reconcile(ledger)["filled"] == 10
    assert len(calls) == 1 and len(calls[0][1]) == 10


def test_cli_rejects_a_giant_age_and_an_absolute_give_up(tmp_path, capsys):
    ledger = Ledger(tmp_path / "consent")
    ledger.counts()
    os.chmod(tmp_path / "consent", 0o700)
    q = ["--queue-dir", str(tmp_path / "consent")]
    assert levain_main(["consent", "ledger", "counts", "--since", "99999999999d", *q]) == 1
    assert levain_main(["consent", "ledger", "reconcile", "--give-up-after", "2026-10-17", *q]) == 1
    assert levain_main(["consent", "ledger", "reconcile", "--give-up-after", "999999999999999999d", *q]) == 1
    assert "levain consent ledger:" in capsys.readouterr().err


# --- B3 round-2 fixes ---------------------------------------------------------------------------------------
def test_pre_outcome_ledger_is_refused_with_a_clear_error(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    conn = sqlite3.connect(d / "ledger.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('format', ?)", (STORE_FORMAT,))
    conn.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, agent TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerStoreError, match="outcome"):
        Ledger(d, clock=lambda: NOW).counts()


def test_oversized_line_holding_the_id_is_result_oversized_never_allow(tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    big = b'{"type":"user","pad":"' + b"x" * (3 << 20) + b'","tool_use_id":"toolu_BIG"}\n'
    t.write_bytes(big)
    scan = stamps.claude_scan(t, ["toolu_BIG", "toolu_OTHER"], max_line_bytes=1 << 20)
    assert scan.states["toolu_BIG"] == ("result_oversized", None)
    assert {st for st, _ in scan.states.values()} <= set(stamps.CLAUDE_STATES)   # the published vocabulary holds it
    assert scan.states["toolu_OTHER"] == ("no_result", None)
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "toolu_BIG", t, at=NOW - timedelta(hours=2))
    import functools
    monkeypatch.setattr(stamps, "claude_scan", functools.partial(stamps.claude_scan, max_line_bytes=1 << 20))
    assert reconcile(ledger, give_up_after=timedelta(hours=1))["unknown"] == 1
    got = ledger.get(row)
    assert got.stamp == "unknown"
    assert got.stamp_detail["reason"] == "result_oversized"


def test_reconciled_codex_sandbox_and_bypass_are_accepted_like_record_call(tmp_path):
    ledger = _ledger(tmp_path)
    for tag in ("readonly", "bypass"):
        pre = json.loads(_lines(f"codex_{tag}_hooks.jsonl")[0])
        ledger.record_call(
            agent="codex", agent_session_id=pre["session_id"], tool_name=pre["tool_name"], tool_input=pre["tool_input"],
            cwd=pre["cwd"], phase="post", tool_use_id=pre["tool_use_id"], source_path=str(FIX / f"codex_{tag}_rollout.jsonl"),
            turn_id=pre["turn_id"], permission_mode=pre["permission_mode"])
    reconcile(ledger)
    assert {(a, s, o) for a, s, o, _ in ledger.counts()} == {("codex", "sandbox", "accepted"),
                                                              ("codex", "bypass", "accepted")}


def test_clean_detail_drops_an_unhashable_reason():
    from levain.consent.ledger import clean_detail
    assert clean_detail({"reason": ["no_source"], "gave_up": True}) == {"gave_up": True}
    assert clean_detail({"reason": {"a": 1}}) is None


def test_a_ruled_call_reads_as_its_ruling_not_the_mode_the_transcript_names(tmp_path):
    """Reproduced in the B3h run (10-18, b3h/RUN.txt): a gated Edit ruled allow ran in a bypass session, and the
    transcript's decision for it was {accept, config, mode}: its post row stamps ``bypass``. The gate's pre row says
    ``ruling``; window, digest and counts read the call once, as its ruling."""
    from types import SimpleNamespace

    from levain.consent.ledger import gate_row, hook_fill, hook_record

    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    tx = tmp_path / "s.jsonl"
    tx.write_text(json.dumps({"type": "user", "permissionDecision": {"decision": "accept", "source": "config",
                                                                      "reasonType": "mode"},
                              "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_ruled"}]}}) + "\n")
    edit = {"file_path": "/w/.claude/settings.json", "old_string": "{", "new_string": "{\"env\": {},"}
    event = {"session_id": "s1", "tool_use_id": "toolu_ruled", "transcript_path": str(tx),
             "permission_mode": "bypassPermissions", "tool_name": "Edit", "tool_input": edit, "cwd": "/w"}
    ruling = SimpleNamespace(decision="allow", via="ruling", request_id="pend-1")
    assert gate_row("claude_code", qdir, {**event, "hook_event_name": "PreToolUse"}, "s1", "Edit", edit, "/w",
                    decision=ruling)
    assert hook_record("claude_code", {**event, "hook_event_name": "PostToolUse"}, queue_dir=qdir)
    assert hook_fill("claude_code", {**event, "hook_event_name": "Stop"}, queue_dir=qdir)["filled"] == 1
    ledger = Ledger(qdir)
    raw = sqlite3.connect(ledger.path).execute("SELECT phase, stamp FROM calls ORDER BY rowid").fetchall()
    assert raw == [("pre", "ruling"), ("post", "bypass")]          # both rows kept, as filed
    (one,) = ledger.window("claude_code", "s1", "1h")
    assert (one.phase, one.stamp, one.outcome, one.request_id) == ("pre", "ruling", "accepted", "pend-1")
    assert [c.stamp for c in ledger.digest("1h")] == ["ruling"]
    assert ledger.counts() == [("claude_code", "ruling", "accepted", 1)]


from tests.test_consent_queue import authority, clock, keys, queue  # noqa: E402,F401 - the shared queue fixtures


def test_a_locked_ledger_changes_neither_the_gate_answer_nor_its_time(queue, keys, authority, clock, tmp_path,
                                                                     monkeypatch):
    """Reproduced in B3h fix r1 (10-18): with ledger.db's write lock held by another process, the gate's ruled allow
    waited the store's 30 s busy timeout on its ledger row before the answer was written, inside a hook whose kill
    at its timeout is a block. The same answer now prints, byte for byte, within the hook-path busy budget."""
    import io
    import shutil
    import time

    from levain.consent import ConsentQueue
    from levain.consent.adapters import claude_code as cc
    from tests.test_consent_queue_v4 import ruled, undecided

    edit = {"file_path": "/work/proj/.claude/settings.json", "old_string": "a", "new_string": "b"}
    request, _ = undecided(queue, tool_input=edit)
    queue.answer_late(request.id, *ruled(queue, request.id, keys["laptop"]))
    qa = Path(queue.directory)
    qb = tmp_path / "locked" / "consent"
    shutil.copytree(qa, qb)
    tx = tmp_path / "t.jsonl"
    tx.write_text("")
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-1", "tool_use_id": "toolu_lock",
             "transcript_path": str(tx), "cwd": "/work/proj", "permission_mode": "bypassPermissions",
             "tool_name": "Edit", "tool_input": edit}

    def answer(qdir: Path) -> tuple[str, float]:
        monkeypatch.setattr(cc, "_queue_for", lambda d, s, **kw: ConsentQueue(qdir, authority=authority, clock=clock, home=h4_home()))
        out, t0 = io.StringIO(), time.monotonic()
        assert cc.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
        return out.getvalue(), time.monotonic() - t0

    working, _ = answer(qa)
    assert '"permissionDecision": "allow"' in working
    assert [(c.phase, c.stamp) for c in Ledger(qa).pending()] == []
    assert Ledger(qa).counts() == [("claude_code", "ruling", "unknown", 1)]   # not seen to run: no post row yet
    Ledger(qb).counts()                                                      # the store exists, then is locked
    lock = sqlite3.connect(qb / "ledger.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        locked, took = answer(qb)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert locked == working
    assert took < 10, took
    assert Ledger(qb).counts() == []                                          # the record is missing, never the answer
