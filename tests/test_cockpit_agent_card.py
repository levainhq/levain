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
        try:
            q.answer(req.id, {"decision": "allow", "signer_fp": "SHA256:x", "signed_at": "2026-01-01T00:00:00Z"}, "sig")
        except AnswerRefused as exc:
            return "cannot be replayed" in str(exc)
        return False

    def raises(exc_type, fn):
        """True when the reader refused (``exc_type``), False when it did not: a probe the control run can fail."""
        try:
            fn()
        except exc_type:
            return True
        return False

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
        # answer_by_grant, take_ruled_allow, _claim and the qbroker grant read were audited by reading (each goes through
        # _fresh_authority, probed above) and have NO row: on an empty queue they answer None / [] with or without the
        # narrowing (the control in the test below measured answer_by_grant doing so), so a row could not fail.
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
    # the control: the newer replay reads the same V4 head, so with no narrowing NO probe reports closed. A row that
    # answers True here cannot tell the worlds apart and proves nothing.
    control = {name: probe() for name, probe in rows.items()}
    assert not any(control.values()), control
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


def test_a_confirm_class_link_with_no_guard_is_refused(world, monkeypatch):
    unguarded = link(world, 1, capabilities=OUTWARD, guard=[])
    with pytest.raises(CardRefused, match="complete guard"):
        agent_link_card(unguarded)
    fp, sign = world.keys["laptop"]
    before = world.state().seq
    with pytest.raises(CardRefused):
        sign_card(world.root, unguarded, signer_fp=fp, sign=sign, clock=world.clock)
    assert world.state().seq == before                           # nothing was signed
    # a complete guard passes the card; the signing path needs the open-bar seam to answer (P5c-2b fills it)
    monkeypatch.setattr(card_mod, "open_bar_problem", lambda g, store_dir=None: None)
    guarded = link(world, 1, capabilities=OUTWARD, guard=[dict(e) for e in TEST_GUARD])
    assert agent_link_card(guarded)["fields"]["guard"]
    assert auth.agent_links(sign_card(world.root, guarded, signer_fp=fp, sign=sign, clock=world.clock),
                            guarded["agent_id"], guarded["store_id"]) == [guarded["id"]]


def test_the_open_bar_seam_reads_a_real_store_and_a_reason_stops_the_signing(world, tmp_path):
    from levain.agent_authority import OPEN_BAR_REFUSAL
    from levain.agents import bars
    from levain.autonomic.agent_tables import create_agent_objects

    fp, sign = world.keys["laptop"]
    grant = link(world, 1)
    store_dir = tmp_path / "autonomic"
    store_dir.mkdir()
    store = sqlite3.connect(store_dir / "autonomic.db", isolation_level=None)
    create_agent_objects(store)
    now = world.clock.now + timedelta(minutes=5)
    before = world.state().seq
    # no bar: the seam passes
    assert card_mod.open_bar_problem(grant, store_dir) is None
    # an open bar: refused with the reason, and sign_card signs nothing
    bar = bars.open_bar(store, grant["agent_id"], head_seq=before, now=now)
    assert card_mod.open_bar_problem(grant, store_dir) == OPEN_BAR_REFUSAL
    with pytest.raises(CardRefused, match="a revoke is open"):
        sign_card(world.root, grant, signer_fp=fp, sign=sign, clock=world.clock, store_dir=store_dir)
    assert world.state().seq == before
    # another agent's bar does not bar this one
    assert card_mod.open_bar_problem({**grant, "agent_id": "someone-else"}, store_dir) is None
    # a closed bar: the seam passes and the card signs
    assert bars.close_bar(store, bar.bar_id, revoke_seq=None, reason="abandoned", now=now)
    assert card_mod.open_bar_problem(grant, store_dir) is None
    snap = sign_card(world.root, grant, signer_fp=fp, sign=sign, clock=world.clock, store_dir=store_dir)
    assert snap.seq == before + 1
    # a store that cannot be read refuses (never taken for no bar)
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "autonomic.db").write_bytes(b"this is not a database" * 100)
    assert "nothing was signed" in card_mod.open_bar_problem(grant, junk)


def test_what_is_signed_is_the_snapshot_the_card_rendered(world, monkeypatch):
    fp, sign = world.keys["laptop"]
    grant = link(world, 1)
    shown = agent_link_card(grant)["fields"]
    # the seam runs between the card and the signing: a mutation there must not reach the signature
    monkeypatch.setattr(card_mod, "open_bar_problem", lambda g, store_dir=None: grant.update(run_ceiling=999) or None)
    snap = sign_card(world.root, grant, signer_fp=fp, sign=sign, clock=world.clock)
    assert grant["run_ceiling"] == 999 and shown["run_ceiling"] != 999
    assert snap.policy.grants[grant["id"]]["run_ceiling"] == shown["run_ceiling"]


def test_a_malformed_record_is_a_card_refusal_never_another_exception(world):
    good = link(world, 1)
    for bad in ({**good, "capabilities": {**good["capabilities"], "where": [3]}},
                {**good, "capabilities": {**good["capabilities"], "where": 7}},
                {**good, "budget_run_usd_micros": "1"}, {**good, "budget_window_usd_micros": -1},
                {**good, "budget_run_usd_micros": True}, {**good, "guard": object(), "capabilities": OUTWARD},
                {**good, "guard": [{1: "x"}]}, {**good, "expires_at": None}, {k: v for k, v in good.items() if k != "expires_at"}, []):
        with pytest.raises(CardRefused):
            agent_link_card(bad)


def test_dollars_are_exact_micros_never_rounded_through_a_float():
    assert card_mod._dollars(1_004_999) == "$1.004999" and card_mod._dollars(1_000_000) == "$1.00"
    assert card_mod._dollars(500_000) == "$0.50" and card_mod._dollars(0) == "$0.00"
