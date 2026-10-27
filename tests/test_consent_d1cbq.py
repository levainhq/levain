"""CH-D1c-B, the queue half (1027-11/d1cbq): a contained-shell grant (SH1) answers a chat Bash call through the
real queue (``answer_by_contained_shell_grant``), the answer is delivered only to a caller holding the launch proof
it was answered under (``decision(..., contained_launch=)``), a row levain allowed that this call's proof cannot
take is ``Declined(contained_delivery_refused)`` (R8-6), and the store's ``via`` check learns ``contained_shell``
(``levain-consent-queue/5``, migrated in place)."""
from __future__ import annotations

import json
import os
import sqlite3
import typing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.cockpit.consent import utc_stamp
from levain.consent.adapters import claude_code as cc
from levain.consent.contained import ContainedLaunchProof
from levain.consent.queue import (CONTAINED_SHELL_ANSWER_FORMAT, MIGRATES_FROM, PRIOR_FORMAT, STORE_FORMAT, VIA,
                                  ConsentQueue, Decision, _CONTAINED_SHELL_ANSWER_KEYS, _ROW_COLUMNS, _SCHEMA,
                                  _requests_table)
from tests._decide_scenarios import _event
from tests.chat_egress.keys_rig import Cockpit

NONCE = "a" * 32
DIGEST = "d" * 64
INPUT = {"command": "pytest -q"}


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"
    ws = home / "proj"
    ws.mkdir(parents=True)
    ws = os.path.realpath(ws)
    ck = Cockpit(tmp_path)
    ck.link({"op": "create_grant", "grant": {
        "id": "sh1-a", "kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
        "workspace": ws, "postures": ["default"], "containment_digest": DIGEST,
        "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=7))}})
    q = ConsentQueue(tmp_path / "levain-home" / "consent", authority=ck.source(), home=os.path.realpath(home))
    settings = cc.AdapterSettings(cockpit_data_root=str(ck.root), away_mode=True, away_hold_seconds=1)
    return {"ws": ws, "home": Path(os.path.realpath(home)), "ck": ck, "q": q, "settings": settings}


def _proof(ws, **kw):
    base = {"launch_id": "L1", "spawn_nonce": NONCE, "profile_digest": DIGEST, "canary_verdict_id": "c" * 64,
            "cwd": ws}
    return ContainedLaunchProof(**{**base, **kw})


def _chat(rig, *, proof, nonce=NONCE, tool_use_id="toolu_1", **kw):
    e = _event("Bash", INPUT, cwd=rig["ws"], tool_use_id=tool_use_id)
    return cc.decide_ex(e, rig["q"], rig["settings"], environ={"HOME": str(rig["home"]), "PATH": "/usr/bin:/bin"},
                        home=rig["home"], launch_nonce=nonce, contained_launch=proof, idle=lambda: None,
                        max_hold=cc.CLEANUP_RESERVE + 0.3, **kw)


def _filed(rig, tool_use_id="toolu_1"):
    return rig["q"].file(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                         cwd=rig["ws"], hold=timedelta(minutes=5), event_id=tool_use_id, event_source="chat")


def _row(q, rid):
    conn = sqlite3.connect(q.path)
    try:
        return conn.execute("SELECT state, via, answer, event_id FROM requests WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()


# --- the seven tests REV15_EXTRACT names for the queue -------------------------------------------------------

def test_contained_shell_grant_answers_chat_bash_no_card(rig):
    out = _chat(rig, proof=_proof(rig["ws"]))
    assert isinstance(out, cc.Decided) and out.decision.decision == "allow"
    assert out.decision.via == "contained_shell" and out.decision.grant_id == "sh1-a"
    state, via, _, event_id = _row(rig["q"], out.request.id)
    assert (state, via, event_id) == ("allowed", "contained_shell", "chat:toolu_1")


def test_contained_shell_grant_never_matches_hook_path(rig):
    # the hook's own entry (decide: launch_nonce "", no proof) on a Bash call in the same workspace: no decision,
    # and the row it filed is never answered by the SH1 grant
    e = _event("Bash", INPUT, cwd=rig["ws"], tool_use_id="toolu_9")
    got = cc.decide(e, rig["q"], rig["settings"], environ={"HOME": str(rig["home"]), "PATH": "/usr/bin:/bin"},
                    home=rig["home"], idle=lambda: None, max_hold=cc.CLEANUP_RESERVE + 0.3)
    assert got is None
    conn = sqlite3.connect(rig["q"].path)
    try:
        rows = conn.execute("SELECT state, via, event_id FROM requests").fetchall()
    finally:
        conn.close()
    assert rows == [("withdrawn", None, "hook:toolu_9")]


def test_hook_decision_reread_of_contained_shell_answer_is_none(rig):
    q, proof = rig["q"], _proof(rig["ws"])
    r = _filed(rig)
    assert q.answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default",
                                             contained_launch=proof).via == "contained_shell"
    assert q.decision(r.id, tool_input=INPUT, posture="default", contained_launch=None) is None
    assert q.decision(r.id, tool_input=INPUT, posture="default") is None        # the hook's call: no keyword
    assert q.await_decision(r.id, 0.0, tool_input=INPUT, posture="default") is None
    assert q.decision(r.id, tool_input=INPUT, posture="default", contained_launch=proof).decision == "allow"


def test_hook_withdraw_race_branch_delivers_nothing_for_a_contained_shell_row(rig):
    # decide()'s withdraw-race re-read: an answer lands between the hold and the withdraw; the hook has no proof
    class Racing(ConsentQueue):
        def withdraw(self, request_id, note=""):
            self.answer_by_contained_shell_grant(request_id, tool_input=INPUT, posture="default",
                                                 contained_launch=_proof(rig["ws"]))
            return super().withdraw(request_id, note=note)

    q = Racing(rig["q"].directory, authority=rig["ck"].source(), home=rig["home"])
    e = _event("Bash", INPUT, cwd=rig["ws"], tool_use_id="toolu_9")
    got = cc.decide(e, q, rig["settings"], environ={"HOME": str(rig["home"]), "PATH": "/usr/bin:/bin"},
                    home=rig["home"], idle=lambda: None, max_hold=cc.CLEANUP_RESERVE + 0.3)
    assert got is None
    conn = sqlite3.connect(q.path)
    try:
        assert conn.execute("SELECT state, via FROM requests").fetchall() == [("withdrawn", None)]
    finally:
        conn.close()   # the SH1 grant refused the hook: row (no chat: id) and so the withdraw went through


def test_withdraw_race_reread_with_matching_proof_delivers(rig):
    proof = _proof(rig["ws"])

    class Racing(ConsentQueue):
        def withdraw(self, request_id, note=""):
            self.answer_by_contained_shell_grant(request_id, tool_input=INPUT, posture="default",
                                                 contained_launch=proof)
            return super().withdraw(request_id, note=note)

    rig["q"] = Racing(rig["q"].directory, authority=rig["ck"].source(), home=rig["home"])
    out = _chat(rig, proof=proof, policy=lambda request, queue, **kw: None)
    assert isinstance(out, cc.Decided) and out.decision.via == "contained_shell"


@pytest.mark.parametrize("field,value", [("launch_id", "L2"), ("spawn_nonce", "b" * 32),
                                         ("profile_digest", "e" * 64), ("canary_verdict_id", "f" * 64),
                                         ("cwd", None)])
def test_contained_shell_reread_with_other_proof_is_none(rig, tmp_path, field, value):
    q, proof = rig["q"], _proof(rig["ws"])
    r = _filed(rig)
    assert q.answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default",
                                             contained_launch=proof) is not None
    if field == "cwd":
        other = tmp_path / "home" / "proj2"
        other.mkdir()
        value = os.path.realpath(other)
    other_proof = replace(proof, **{field: value})
    assert other_proof.problem() is None
    assert q.decision(r.id, tool_input=INPUT, posture="default", contained_launch=other_proof) is None
    assert q.decision(r.id, tool_input=INPUT, posture="default", contained_launch=proof) is not None


def test_contained_shell_answer_payload_binds_five_fields(rig):
    q, proof = rig["q"], _proof(rig["ws"])
    r = _filed(rig)
    q.answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default", contained_launch=proof)
    payload = json.loads(_row(q, r.id)[2])
    assert set(payload) == _CONTAINED_SHELL_ANSWER_KEYS and payload["format"] == CONTAINED_SHELL_ANSWER_FORMAT
    assert {k: payload[k] for k in ("launch_id", "spawn_nonce", "containment_digest", "canary_verdict_id", "cwd")} \
        == {"launch_id": "L1", "spawn_nonce": NONCE, "containment_digest": DIGEST, "canary_verdict_id": "c" * 64,
            "cwd": rig["ws"]}
    assert (payload["grant_id"], payload["posture"], payload["decision"]) == ("sh1-a", "default", "allow")
    receipt = q.receipts(r.id)[-1]
    assert receipt["state"] == "allowed" and "contained-shell-grant:sh1-a@" in json.dumps(receipt)


@pytest.mark.parametrize("withheld", [None, {"canary_verdict_id": "f" * 64}])
def test_refused_contained_delivery_is_declined_not_card(rig, withheld):
    # the row is allowed by SH1 under the launch's proof; this call holds no proof (withheld) or another one
    answered_with = _proof(rig["ws"])

    def answers_then_none(request, queue, **kw):
        queue.answer_by_contained_shell_grant(request.id, tool_input=INPUT, posture="default",
                                              contained_launch=answered_with)
        return None

    proof = None if withheld is None else replace(answered_with, **withheld)
    out = _chat(rig, proof=proof, policy=answers_then_none)
    assert isinstance(out, cc.Declined) and out.reason == "contained_delivery_refused"
    assert _row(rig["q"], _only_id(rig["q"]))[:2] == ("allowed", "contained_shell")


def _only_id(q):
    conn = sqlite3.connect(q.path)
    try:
        (rid,), = conn.execute("SELECT id FROM requests").fetchall()
    finally:
        conn.close()
    return rid


def test_the_refused_delivery_reason_is_in_the_union_with_a_message():
    hints = typing.get_type_hints(cc.Declined)
    assert set(typing.get_args(hints["reason"])) == cc.DECLINED_REASONS
    assert "contained_delivery_refused" in cc.DECLINED_REASONS and cc.DECLINE_MESSAGES["contained_delivery_refused"]


def test_a_chat_call_with_no_proof_is_never_answered_by_sh1(rig):
    # the service withholds the proof (R7-2): SH1 cannot answer, the call holds and ends with no decision
    out = _chat(rig, proof=None)
    assert isinstance(out, cc.Abstained) and out.reason == "hold_expired"
    assert _row(rig["q"], out.request.id)[:2] == ("withdrawn", None)


def test_other_vias_ignore_contained_launch(rig):
    q = rig["q"]
    r = _filed(rig)
    q.answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default", contained_launch=_proof(rig["ws"]))
    assert "contained_shell" in VIA
    # a request that is not pending is not answered again
    assert q.answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default",
                                             contained_launch=_proof(rig["ws"])) is None


def test_a_misshapen_proof_answers_nothing(rig):
    r = _filed(rig)
    assert rig["q"].answer_by_contained_shell_grant(r.id, tool_input=INPUT, posture="default",
                                                    contained_launch=_proof(rig["ws"], cwd="rel")) is None
    assert _row(rig["q"], r.id)[:2] == ("pending", None)


# --- the store: /4 -> /5 ---------------------------------------------------------------------------------

def _as_v4(path):
    """Rewrite a current store as /4 wrote it: the requests table's via check without ``contained_shell`` (the
    one difference between the two formats), every row copied with its rowid, the indexes and triggers the
    schema puts on it recreated, the format marked /4."""
    cols = ", ".join(_ROW_COLUMNS)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = _requests_table("requests_v4").replace(",'contained_shell'", "")
        assert "contained_shell" not in old
        conn.execute(old)
        conn.execute(f"INSERT INTO requests_v4 (rowid, {cols}) SELECT rowid, {cols} FROM requests ORDER BY rowid")
        conn.execute("DROP TABLE requests")
        conn.execute("ALTER TABLE requests_v4 RENAME TO requests")
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (PRIOR_FORMAT,))
        conn.execute("COMMIT")
    finally:
        conn.close()


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("requests", "receipts", "dialog_outcomes")}
    finally:
        conn.close()


def _insert_with_via(path, via, rid):
    # a fresh pending row with ``via`` set: no trigger fires on INSERT, so only the via CHECK can refuse it
    cols = ", ".join(_ROW_COLUMNS)
    picked = ", ".join(f"'{rid}'" if c == "id" else f"'chat:{rid}'" if c == "event_id" else
                       "'pending'" if c == "state" else f"'{via}'" if c == "via" else c for c in _ROW_COLUMNS)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"INSERT INTO requests ({cols}) SELECT {picked} FROM requests LIMIT 1")
        conn.rollback()
    finally:
        conn.close()


def test_a_v4_store_opens_keeps_every_row_and_seal_and_refuses_an_unknown_via(rig):
    q = rig["q"]
    first = _filed(rig, "toolu_1")
    q.withdraw(first.id)
    second = _filed(rig, "toolu_2")
    _as_v4(q.path)
    before = _dump(q.path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):   # a /4 store cannot hold the new via
        _insert_with_via(q.path, "contained_shell", "x1")

    new = ConsentQueue(q.directory, authority=rig["ck"].source(), home=rig["home"])
    assert new.get(second.id)[1] == "pending"               # the first open migrates
    assert _dump(new.path) == before                        # every row, with its rowid / seq, unchanged
    conn = sqlite3.connect(new.path)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == STORE_FORMAT
    finally:
        conn.close()
    _insert_with_via(new.path, "contained_shell", "x2")     # accepted (and rolled back)
    for unknown in ("sandbox_net", "bogus"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert_with_via(new.path, unknown, "x3")
    assert new.get(first.id)[1] == "withdrawn"              # its seal still matches
    # the migrated store takes a contained-shell answer, and the chat index came back with the schema
    got = new.answer_by_contained_shell_grant(second.id, tool_input=INPUT, posture="default",
                                              contained_launch=_proof(rig["ws"]))
    assert got is not None and got.via == "contained_shell"
    with pytest.raises(Exception, match="once"):
        _filed({**rig, "q": new}, "toolu_2")


def test_the_formats_this_store_migrates_from():
    assert MIGRATES_FROM == ("levain-consent-queue/3", "levain-consent-queue/4") and PRIOR_FORMAT in MIGRATES_FROM
    assert STORE_FORMAT == "levain-consent-queue/5" and STORE_FORMAT not in MIGRATES_FROM


def test_a_decision_is_a_dataclass_with_the_new_via():
    d = Decision(request_id="r", decision="allow", signer_fp=None, decided_at="t", request_digest="x",
                 via="contained_shell")
    assert d.via == "contained_shell"
