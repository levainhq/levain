"""The agent-link signing card and the older-reader audit (A0 §6.4; BUILD_PLAN P5a-2b).

The audit's result is the table in ``test_every_audited_older_reader_fails_closed_on_a_v4_head``: an older build's
readers of ``cockpit.db`` stood up by narrowing the accepted formats to the three it knew (the exact check it had; the
stand-in P5a-1's tests use), each driven through its real failure path against a log whose head is a real FORMAT_V4
agent link signed through the one signing entry. The same rows were also run against the last release before the
agent-link kind, unpatched, from a separate process; all closed.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
import levain.cockpit.authority as card_mod
from levain.agents.signing import sign_agent_link
from levain.cockpit.authority import CardRefused, RequestAuthority, agent_link_card, sign_card
from levain.cockpit.firejournal import CockpitDB

pytest.importorskip("cryptography")
from tests.autonomic_test_only_coverage import TEST_GUARD  # noqa: E402
from tests.test_authority_agent_link import link  # noqa: E402
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

OUTWARD = {"harness": "claude", "model": "sonnet", "where": [{"repo": "/work/repo", "paths": ["src/"]}],
           "lands": [], "pushes": ["origin"], "publishes": []}


@pytest.fixture
def world(tmp_path) -> World:
    w = World(tmp_path)
    CockpitDB(w.root).set_install_class("reported")
    return w


def _sign(world: World, grant: dict) -> auth.AuthoritySnapshot:
    fp, sign = world.keys["laptop"]
    return sign_agent_link(world.root, grant, signer_fp=fp, sign=sign, clock=world.clock)


def _rows(world: World, tmp_path):
    """``(reader, probe)`` per audited older-reader path; each probe returns the reader's own failure outcome."""
    from levain.chat_egress.keys import AuthorityLiveness, LivenessUnreadable
    from levain.consent.queue import AnswerRefused, ConsentQueue, authority_source
    from levain.cockpit.keys import KeyError_, _capture, _journal, _signing_authority
    from levain.dispatch import control as dctl
    from levain.dispatch.jobs import SignatureRefused, _replay

    def source():
        return authority_source(world.db, anchor=auth.anchor_path(world.root), cache=auth.ReplayCache())

    def queue(name):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o700)
        return ConsentQueue(d, authority=source(), home=tmp_path / "home")

    def answer():
        q = queue("answer")
        req = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"},
                     cwd="/tmp", hold=timedelta(hours=1))
        with pytest.raises(AnswerRefused) as exc:
            q.answer(req.id, {"decision": "allow", "signer_fp": "SHA256:x", "signed_at": "2026-01-01T00:00:00Z"}, "sig")
        return "cannot be replayed" in str(exc.value)

    def raises(exc_type, fn):
        with pytest.raises(exc_type):
            fn()
        return True

    def view():
        conn = sqlite3.connect(f"{world.db.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
        try:
            conn.execute("BEGIN")
            got = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=auth.anchor_path(world.root))
        finally:
            conn.close()
        return got.authority is None and got.policy.apply("any", "T1") == "T3"

    def control():
        store = dctl.ControlStore.__new__(dctl.ControlStore)
        store._authority = source()
        return raises(dctl.ControlStateUnavailable, store._snapshot)

    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    return {
        "consent queue: _fresh_authority": lambda: queue("fresh")._fresh_authority() is None,
        "consent queue: answer": answer,
        "consent queue: answer_by_grant": lambda: queue("grant").answer_by_grant("x", tool_input={}, posture="p") is None,
        "consent queue: take_ruled_allow":
            lambda: queue("take").take_ruled_allow("claude_code", "s", "Bash", {}, cwd="/tmp") is None,
        "consent queue: _claim": lambda: queue("claim")._claim("claude_code", "s", step=None) == [],
        "qbroker grants (via the queue's grant decision)":
            lambda: queue("qb").answer_by_grant("x", tool_input={}, posture="p") is None,
        "chat egress key liveness": lambda: raises(LivenessUnreadable,
                                                   lambda: AuthorityLiveness(source(), clock=clock).check("gp-x", "i")),
        "chat egress key setup (signing authority)": lambda: raises(
            KeyError_, lambda: _signing_authority(_capture(_journal(world.root), clock(), shutil.which("ssh-keygen")))),
        "dispatch jobs signature replay": lambda: raises(SignatureRefused, lambda: _replay(source())),
        "dispatch control store snapshot": control,
        "cockpit views (RequestAuthority.capture)": view,
    }


def test_every_audited_older_reader_fails_closed_on_a_v4_head(world, tmp_path, monkeypatch):
    first = _sign(world, link(world, 1))
    assert world.record(first.seq)["format"] == auth.FORMAT_V4
    rows = _rows(world, tmp_path)
    # the probe can fail: the newer replay reads the same V4 head, so an unnarrowed reader is not closed
    live = rows["consent queue: _fresh_authority"]
    assert live() is False
    # the older replay: the three formats it knew
    monkeypatch.setattr(auth, "_KNOWN_FORMATS", (auth.FORMAT, auth.FORMAT_V2, auth.FORMAT_V3))
    # an older process has never verified this chain: this one has (signing it), and its cache would answer for it
    monkeypatch.setattr(auth, "PROCESS_CACHE", auth.ReplayCache())
    outcomes = {}
    for name, probe in rows.items():
        outcomes[name] = probe()
    assert outcomes == {name: True for name in rows}, outcomes


def test_the_card_shows_every_signed_field_the_review_date_and_the_upgrade_order(world):
    grant = link(world, 1, capabilities=OUTWARD, guard=[dict(e) for e in TEST_GUARD], billing="api_key",
                 budget_run_calls=0, budget_run_tunnel_bytes=0)
    card = agent_link_card(grant)
    assert card["fields"] == grant
    assert card["review"] == f"review by {grant['expires_at']}" and grant["expires_at"] in card["until"]
    assert "acts outward: pushes" in card["outward"] and "$1.00 a run" in card["cost"]
    up = card["upgrade"]
    assert card["upgrade"] == card_mod.UPGRADE_ORDER_LINE
    assert "levain-authority/4" in up and "a revoke included" in up and "OLDER levain" in up
    # the review date is derived: a link that calls no model and acts on nothing outward carries none
    plain = link(world, 2, capabilities={**OUTWARD, "model": "", "pushes": []})
    assert agent_link_card(plain)["review"] is None
    # the card cannot be made from a record that is not an agent link
    with pytest.raises(CardRefused):
        agent_link_card({"kind": "graduation"})


def test_a_confirm_class_link_with_no_guard_is_refused(world):
    unguarded = link(world, 1, capabilities=OUTWARD, guard=[])
    with pytest.raises(CardRefused, match="complete guard"):
        agent_link_card(unguarded)
    fp, sign = world.keys["laptop"]
    before = world.state().seq
    with pytest.raises(CardRefused):
        sign_card(world.root, unguarded, signer_fp=fp, sign=sign, clock=world.clock)
    assert world.state().seq == before                           # nothing was signed
    # a complete guard passes the card and the signing path
    guarded = link(world, 1, capabilities=OUTWARD, guard=[dict(e) for e in TEST_GUARD])
    assert agent_link_card(guarded)["fields"]["guard"]
    assert auth.agent_links(sign_card(world.root, guarded, signer_fp=fp, sign=sign, clock=world.clock),
                            guarded["agent_id"], guarded["store_id"]) == [guarded["id"]]


def test_the_card_refuses_to_sign_on_an_open_bar_through_the_seam(world, monkeypatch):
    """P5c-2b fills ``open_bar_problem``; until then it is a stub that answers None. The seam is what is asserted:
    the card's sign path consults it before anything is signed, and a reason it gives stops the signing."""
    fp, sign = world.keys["laptop"]
    grant = link(world, 1)
    assert card_mod.open_bar_problem(grant) is None               # the stub: no bar is read yet
    monkeypatch.setattr(card_mod, "open_bar_problem", lambda g: "a revoke is open for this agent: finish or abandon it")
    before = world.state().seq
    with pytest.raises(CardRefused, match="a revoke is open"):
        sign_card(world.root, grant, signer_fp=fp, sign=sign, clock=world.clock)
    assert world.state().seq == before
