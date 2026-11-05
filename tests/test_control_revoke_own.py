"""1104-30 X1: CONTROL's operator verb ``REVOKE-OWN {session}``. The operator asks, CONTROL decides, root's reaper
ENDs. Real CONTROL over a real socket (the harness of test_slot_s2a_control)."""
from __future__ import annotations

from levain.slot import control as C
from levain.slot import records as R
from tests.test_slot_s2a_control import (  # noqa: F401 - env is a fixture
    OP_UID, S1, S2, S3, attach, attach_claims, cas, env, to_rendered)


def test_the_verb_is_the_operators_alone(env):
    assert "REVOKE-OWN" in C.PEER_VERBS["operator"]
    for kind, verbs in C.PEER_VERBS.items():
        if kind != "operator":
            assert "REVOKE-OWN" not in verbs, kind
    for role in ("root", "qbroker", "foreign"):
        ans = env.client(role).ask("REVOKE-OWN", session=S1)
        assert ans["refused"] == "verb-not-allowed-for-peer", role


def test_own_live_record_moves_to_revoked_keeping_the_lease_and_waking_the_reaper(env):
    op, root = to_rendered(env)
    env.seams.calls.clear()
    gen = env.record(S1).generation
    ans = op.ask("REVOKE-OWN", session=S1)
    assert ans["ok"] and ans["generation"] == gen + 1
    rec = env.record(S1)
    assert rec.state == "REVOKED" and rec.lease_live is True      # only the holder closes its lease
    assert env.seams.calls == [("wake",), ("drop", S1)]           # the reaper is woken before keys is told


def test_a_second_ask_and_a_past_live_record_are_no_ops_answering_the_generation(env):
    op, root = to_rendered(env)
    g = op.ask("REVOKE-OWN", session=S1)["generation"]
    env.seams.calls.clear()
    assert op.ask("REVOKE-OWN", session=S1)["generation"] == g
    g = cas(root, S1, g, "REVOKED", "FROZEN")["generation"]
    assert op.ask("REVOKE-OWN", session=S1)["generation"] == g
    assert env.record(S1).state == "FROZEN" and env.seams.calls == []


def test_an_unknown_session_creates_no_record(env):
    op = env.client("op")
    before = list(env.seams.calls)
    ans = op.ask("REVOKE-OWN", session=S3)
    assert ans["refused"] == "session-unknown"
    assert not (env.store_dir / f"{S3}.json").exists()
    assert env.seams.calls == before
    # contrast: root's REVOKE does create the alloc-only orphan
    assert env.client("root").ask("REVOKE", session=S3)["ok"]


def test_a_record_the_operator_does_not_own_is_session_unknown_and_untouched(env):
    op, root = attach(env, S1)
    env.control._put(R.SessionState(session=S2, generation=3, state="ACTIVE", claims=attach_claims(operator_uid=OP_UID + 7),
                                    lease_live=False, event_kind="state", updated_at=R.timestamp(0)))
    assert op.ask("REVOKE-OWN", session=S2)["refused"] == "session-unknown"
    assert env.record(S2).state == "ACTIVE"
    # a REVOKE-created orphan has claims={} and belongs to nobody the operator can name
    root.ask("REVOKE", session=S3)
    assert op.ask("REVOKE-OWN", session=S3)["refused"] == "session-unknown"
    # the refusal is the one STATUS would imply: neither record is in the operator's STATUS
    assert [r["session"] for r in op.ask("STATUS")["records"]] == [S1]


def test_it_takes_exactly_one_session_argument(env):
    op = env.client("op")
    assert op.ask("REVOKE-OWN")["ok"] is False
    assert op.ask("REVOKE-OWN", session=S1, extra=1)["ok"] is False
    assert op.ask("REVOKE-OWN", session="bad")["ok"] is False


def test_root_revoke_is_unchanged(env):
    op, root = to_rendered(env)
    assert root.ask("REVOKE", session=S1)["generation"] == 5
    rec = env.record(S1)
    assert rec.state == "REVOKED" and rec.lease_live is True
