"""The agent authority source and the invariant 1 predicate (A0 section 2.3; BUILD_PLAN P5c-1a).

Every log here is real: ssh-keygen keys and links verified through the replay (the ``World`` of the agent-link tests).
The occurrence store is the real agent DDL on a scratch SQLite file.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.agent_authority import AgentAuthority, AgentAuthorityError, AgentCoverMoved, agent_fence
from levain.autonomic.agent_tables import create_agent_objects
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, STORE, World, create, revoke_agent  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LINK = "al-000000000001"


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _occurrence(tmp_path, world: World) -> tuple[sqlite3.Connection, dict]:
    """A launched occurrence of AGENT under LINK, in a store holding the real agent tables."""
    conn = sqlite3.connect(tmp_path / "autonomic.db", isolation_level=None)
    create_agent_objects(conn)
    state = world.state()
    row = {"occurrence_id": "occ-1", "agent_id": AGENT, "link_id": LINK, "link_incarnation":
           auth.agent_link_incarnation(state, LINK), "spec_hash": "b" * 64, "binding_id": "bind-0123456789abcdef",
           "launch_epoch": 0}
    conn.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, epoch, window_key,"
                 " spec_hash, binding_id, run_id, admitted_at) VALUES ('occ-1', ?, 'manual', 'k', 'admitted', 0, 'w',"
                 " ?, ?, 'run-1', 't')", (AGENT, row["spec_hash"], row["binding_id"]))
    conn.execute("UPDATE agent_occurrences SET state = 'launched', launch_epoch = 0, link_id = ?,"
                 " link_incarnation = ?, launched_at = 't' WHERE occurrence_id = 'occ-1'",
                 (LINK, row["link_incarnation"]))
    return conn, row


def _fence(world: World, conn, row, authority):
    return agent_fence(row, authority=authority, store_id=STORE, now=lambda: world.clock.now)


def test_a_valid_link_admits(world, tmp_path):
    # behaviour: a live, unexpired link read through the verified replay gives a fence_now equal to the launch fence
    create(world, 1)
    conn, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    authority.refresh()
    fence, fence_now = _fence(world, conn, row, authority)
    assert fence_now(conn) == fence
    assert authority.peek(LINK, agent_id=AGENT, store_id=STORE, now=world.clock.now)[0] is True


def test_expiry_and_a_revoke_each_refuse_the_next_check(world, tmp_path):
    # behaviour: the fence moves at the link's expiry instant, and at a revoke_agent once the replay is refreshed;
    # a log that moved and was not refreshed refuses with AgentCoverMoved (an UNCLASSIFIED, never a pass)
    create(world, 1)
    conn, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    authority.refresh()
    clock = {"now": world.clock.now}
    fence, fence_now = agent_fence(row, authority=authority, store_id=STORE, now=lambda: clock["now"])
    assert fence_now(conn) == fence
    clock["now"] = world.clock.now + timedelta(days=30)
    assert fence_now(conn) != fence                                    # expired: the next check is STALE
    clock["now"] = world.clock.now
    world.link(revoke_agent())
    with pytest.raises(AgentCoverMoved):
        fence_now(conn)                                                # the head moved: refuse until refreshed
    authority.refresh()
    assert fence_now(conn) != fence                                    # revoked: STALE


def test_a_renew_keeps_the_incarnation_and_a_resign_leaves_the_admitted_link_live(world, tmp_path):
    # behaviour: a renew changes only expires_at, so the fence is unchanged; a second link for the same agent
    # does not touch the admitted one
    create(world, 1)
    conn, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    authority.refresh()
    fence, fence_now = _fence(world, conn, row, authority)
    at = world.clock.now + timedelta(minutes=1)
    world.link({"op": "renew", "grant_id": LINK, "expires_at": utc_stamp(at + timedelta(days=30)),
                "signed_at": utc_stamp(at)})
    create(world, 2)
    authority.refresh()
    assert authority.incarnation(LINK) == row["link_incarnation"]
    assert fence_now(conn) == fence


def test_an_unreadable_store_refuses(world, tmp_path):
    # behaviour: a log that cannot be replayed raises at refresh, caches nothing, and every later check refuses
    create(world, 1)
    conn, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    authority.refresh()
    fence, fence_now = _fence(world, conn, row, authority)
    world.db.write_bytes(b"not a database")
    with pytest.raises(AgentCoverMoved):
        fence_now(conn)                                                # a held replay over a log that cannot be read
    with pytest.raises(Exception):
        authority.refresh()
    with pytest.raises(AgentCoverMoved):
        fence_now(conn)                                                # and nothing is held after the failed refresh
    with pytest.raises(AgentAuthorityError):
        agent_fence({"agent_id": AGENT}, authority=authority, store_id=STORE)   # not a launched occurrence


def test_a_mapping_that_is_not_the_stored_launch_refuses(world, tmp_path):
    # behaviour: fence_now reads every launch column from the store, so a mapping carrying occ-1's id with another
    # live link (or another spec) of the same agent never matches: the check runs on the row, not on the caller's copy
    create(world, 1)
    create(world, 2)
    conn, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    authority.refresh()
    other = "al-000000000002"
    mixed = {**row, "link_id": other, "link_incarnation": authority.incarnation(other)}
    assert authority.peek(other, agent_id=AGENT, store_id=STORE, now=world.clock.now)[0] is True
    fence, fence_now = _fence(world, conn, mixed, authority)
    assert fence_now(conn) != fence
    fence, fence_now = _fence(world, conn, {**row, "spec_hash": "c" * 64}, authority)
    assert fence_now(conn) != fence


def test_a_closed_bar_revoke_seq_that_is_no_integer_refuses(world, tmp_path):
    # behaviour: a closed bar's revoke_seq reaches agent_link_arms as stored; a REAL is never truncated into an
    # integer that passes, so the check is STALE
    create(world, 1)
    conn, row = _occurrence(tmp_path, world)
    conn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar, revoke_seq, closed_at)"
                 " VALUES ('bar-1', ?, 't', 0, 0.5, 't')", (AGENT,))
    authority = AgentAuthority(world.root)
    authority.refresh()
    fence, fence_now = _fence(world, conn, row, authority)
    assert fence_now(conn) != fence


def test_null_launch_columns_are_never_launched(world, tmp_path):
    # behaviour: a mapping whose link columns are NULL raises, never coerced to the string "None"
    create(world, 1)
    _, row = _occurrence(tmp_path, world)
    authority = AgentAuthority(world.root)
    for column in ("link_id", "link_incarnation"):
        with pytest.raises(AgentAuthorityError):
            agent_fence({**row, column: None}, authority=authority, store_id=STORE)
