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
    assert stamps.claude_stamp(got, "bypassPermissions") == "hook"
    cfg = stamps.claude_decision(FIX / "cc_pairs.jsonl", BY_CASE["R3-allowrule2-proj"][0]["tool_use_id"])
    assert cfg == {"decision": "reject", "source": "config"}
    assert stamps.claude_stamp(cfg, "default") == "own_rules"


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
    assert st(None, "bypassPermissions") == "unknown"
    assert st({}, "bypassPermissions") == "unknown"
    assert st({"decision": "accept", "source": "mystery", "reasonType": "mystery"}, "default") == "unknown"


def test_missing_file_and_missing_line_are_unknown_not_allow(tmp_path):
    some_id = META[0]["tool_use_id"]
    assert stamps.claude_decision(tmp_path / "absent.jsonl", some_id) is None
    assert stamps.claude_result_state(tmp_path / "absent.jsonl", some_id) == ("missing_file", None)
    assert stamps.claude_result_state(None, some_id) == ("missing_file", None)
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


def test_codex_stamp_mapping_and_uncovered_cases():
    cs = stamps.codex_stamp
    assert cs({"approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"},
               "approvals_reviewer": "user"}) == "own_rules"
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
    assert ledger.counts() == [("claude_code", "bypass", 2), ("codex", "sandbox", 1)]
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
    assert reconcile(ledger) == {"filled": 0, "unknown": 0, "still_pending": 1}
    assert ledger.get(row).stamp == "pending"
    transcript.write_text("".join(lines[: res_i + 1]))                    # the next hook finds it written
    assert reconcile(ledger) == {"filled": 1, "unknown": 0, "still_pending": 0}
    done = ledger.get(row)
    assert done.stamp == "classifier" and done.stamp_detail == m["permissionDecision"]


def test_reconcile_missing_file_is_unknown(tmp_path):
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "t1", tmp_path / "never-written.jsonl")
    nosrc = _cc_row(ledger, "t2", None)
    assert reconcile(ledger) == {"filled": 2, "unknown": 2, "still_pending": 0}
    assert ledger.get(row).stamp == "unknown" and ledger.get(row).stamp_detail == {"reason": "transcript_missing"}
    assert ledger.get(nosrc).stamp == "unknown"


def test_reconcile_gives_up_on_a_line_that_never_comes(tmp_path):
    ledger = _ledger(tmp_path)
    row = _cc_row(ledger, "toolu_absent", FIX / "cc_pairs.jsonl", at=NOW - timedelta(hours=3))
    young = _cc_row(ledger, "toolu_young", FIX / "cc_pairs.jsonl", at=NOW - timedelta(minutes=2))
    assert reconcile(ledger) == {"filled": 1, "unknown": 1, "still_pending": 1}
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
            cwd=pre["cwd"], phase="pre", tool_use_id=pre["tool_use_id"], source_path=str(FIX / f"codex_{tag}_rollout.jsonl"),
            turn_id=pre["turn_id"], permission_mode=pre["permission_mode"])
    gone = ledger.record_call(agent="codex", agent_session_id="g", tool_name="Bash", tool_input={}, cwd="/w",
                              phase="pre", tool_use_id="gone", source_path=str(tmp_path / "no-rollout.jsonl"))
    assert reconcile(ledger) == {"filled": 3, "unknown": 1, "still_pending": 0}
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


# --- the command ----------------------------------------------------------------------------------------
def test_cli_counts_digest_window_reconcile(tmp_path, capsys):
    ledger = Ledger(tmp_path / "consent")
    ledger.counts()
    os.chmod(tmp_path / "consent", 0o700)
    now = datetime.now(timezone.utc)
    ledger.record_call(agent="codex", agent_session_id="sess-1", tool_name="Bash", tool_input={}, cwd="/w",
                       phase="post", stamp="grant", received_at=now - timedelta(minutes=5), request_id="r1")
    ledger.record_call(agent="codex", agent_session_id="sess-1", tool_name="Bash", tool_input={}, cwd="/w",
                       phase="post", source_path=str(tmp_path / "none.jsonl"))
    q = ["--queue-dir", str(tmp_path / "consent")]
    assert levain_main(["consent", "ledger", "counts", *q, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"agent": "codex", "stamp": "grant", "rows": 1}, {"agent": "codex", "stamp": "pending", "rows": 1}]
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
    assert reconcile(ledger) == {"filled": 10, "unknown": 0, "still_pending": 0}
    # hand count: 2 bypass, 2 classifier, 2 subcommandResults(own_rules), 2 config rejects(own_rules), 1 hook reject,
    # 1 bypass accept after the hook reject
    assert ledger.counts() == [("claude_code", "bypass", 3), ("claude_code", "classifier", 2),
                               ("claude_code", "hook", 1), ("claude_code", "own_rules", 4)]
