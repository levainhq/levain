"""The agent link and the ``revoke_agent`` op in the authority log (A0 §3.2, §3.3; BUILD_PLAN P5a-1).

An ``agent-link`` is a signed authority for one named agent at one spec version; ``revoke_agent`` removes every link of
an agent in one link. Both are written ``levain-authority/4``, so a replay that knows only /1 to /3 stops at the first
instead of reading it as something inert. Every log here is real: ssh-keygen keys, links verified by ssh-keygen through
the replay (the ``World`` of the graduation kind's tests). The older replay is stood up by narrowing the accepted
formats to the three it knew, which is the exact check it had.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import STORE, V1, V2, V3, World  # noqa: E402
from tests.test_consent_queue import Key  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

V4 = "levain-authority/4"
AGENT = "ag-" + "a1" * 16
OTHER_AGENT = "ag-" + "b2" * 16
OTHER_STORE = "store-" + "cd" * 16


def link(world: World, n: int = 1, *, agent: str = AGENT, store: str = STORE, days: int = 29, **over) -> dict:
    """An agent link record (A0 §3.2's example, filled) expiring ``days`` after the clock's now."""
    g = {"id": f"al-{n:012x}", "kind": "agent-link", "agent_id": agent, "store_id": store,
         "spec_hash": "b" * 64, "role_hash": "c" * 64, "binding_id": "bind-0123456789abcdef",
         "capabilities": {"harness": "claude", "model": "sonnet",
                          "where": [{"repo": "/work/repo", "paths": ["src/", "docs/a.md"]}],
                          "lands": [], "pushes": [], "publishes": []},
         "budget_run_usd_micros": 1_000_000, "budget_window_usd_micros": 5_000_000, "run_ceiling": 3,
         "expires_at": utc_stamp(world.clock.now + timedelta(days=days)), "guard": []}
    g.update(over)
    return g


def create(world: World, n: int = 1, signer: str = "laptop", **kw) -> auth.AuthoritySnapshot:
    return world.link({"op": "create_grant", "grant": link(world, n, **kw)}, signer)


def revoke_agent(agent: str = AGENT, store: str = STORE) -> dict:
    return {"op": "revoke_agent", "agent_id": agent, "agent_store_id": store}


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --- the format: /4 first, and an older replay stops at it ----------------------------------------------------------

def test_an_agent_link_and_a_revoke_agent_are_written_v4_and_a_v3_only_replay_stops_at_them(world, monkeypatch):
    first = create(world, 1)
    gone = world.link(revoke_agent())
    assert world.record(first.seq)["format"] == world.record(gone.seq)["format"] == V4 == auth.FORMAT_V4
    # the replay this tree had before /4: the same check over the three formats it knew
    monkeypatch.setattr(auth, "_KNOWN_FORMATS", (V1, V2, V3))
    with pytest.raises(auth.ReplayError, match=r"link %d: the record's format is not levain-authority/1, "
                                               r"levain-authority/2 or levain-authority/3" % first.seq):
        world.state()
    # it cannot append anything after the /4 head, a revoke included
    conn = sqlite3.connect(world.db, isolation_level=None)
    try:
        with pytest.raises(auth.ReplayError, match="format is not"):
            auth.prepare(conn, {"op": "revoke_grant", "grant_id": "al-000000000001"}, signer_fp=world.fp("laptop"),
                         now=world.clock.now + timedelta(minutes=5), cache=ReplayCache())
    finally:
        conn.close()
    monkeypatch.undo()
    assert world.state().seq == gone.seq


def test_each_link_has_the_one_format_the_rule_gives_it(world):
    # a revoke_agent written /1 or /3, and an agent link written /3, are refused by the replay: V4 is tested first
    create(world, 1)
    for fmt in (V1, V3):
        db = world.plant(revoke_agent(), fmt=fmt)
        assert f"this revoke_agent link carries {fmt}; a link like it is written {V4}" in world.replay_verdict(db)
    db = world.plant({"op": "create_grant", "grant": link(world, 2)}, fmt=V3)
    assert f"this create_grant link carries {V3}; a link like it is written {V4}" in world.replay_verdict(db)
    # and a link with nothing of the agent kinds is not written /4
    db = world.plant({"op": "raise", "verb": "spore_touch", "tier": "T3"}, fmt=V4)
    assert f"this raise link carries {V4}; a link like it is written {V1}" in world.replay_verdict(db)


def test_a_loosen_that_brings_in_an_agent_link_is_written_v4(world):
    policy = world.state().policy.as_dict()
    policy["grants"]["al-000000000001"] = link(world, 1)
    snap = world.link({"op": "loosen", "policy": policy})
    assert world.record(snap.seq)["format"] == V4
    assert auth.agent_links(snap, AGENT, STORE) == ["al-000000000001"]


# --- the validators ----------------------------------------------------------------------------------------------------

def test_expiry_beyond_30_days_is_refused(world):
    assert "may last at most 30 days past its link's time" in world.refused(
        {"op": "create_grant", "grant": link(world, 1, days=31)})
    assert create(world, 1, days=30).seq        # the limit itself stands (the link is stamped a minute after the clock)
    # a renew is held to 30 days from its own signing time too, whatever the generic renew bound says
    at = world.clock.now + timedelta(minutes=1)
    assert "at most 30 days" in world.refused({"op": "renew", "grant_id": "al-000000000001",
                                                "expires_at": utc_stamp(at + timedelta(days=31)),
                                                "signed_at": utc_stamp(at)})


def test_an_unknown_field_is_refused_and_so_is_a_float(world):
    assert "must carry exactly" in world.refused(
        {"op": "create_grant", "grant": link(world, 1, surprise=1)})
    for name in ("budget_run_usd_micros", "budget_window_usd_micros", "run_ceiling"):
        for bad in (1.5, 2.0, True, -1, 10**13, "5", None):
            assert f"{name} must be an integer from 0 to" in world.refused(
                {"op": "create_grant", "grant": link(world, 1, **{name: bad})}), (name, bad)
    # a missing field is the same refusal as an unknown one
    short = link(world, 1)
    del short["guard"]
    assert "must carry exactly" in world.refused({"op": "create_grant", "grant": short})


def test_a_record_carrying_review_by_is_refused(world):
    # the review date is expires_at (A0 §3.2); a stored review_by would move the incarnation on every renew
    assert "must carry exactly" in world.refused(
        {"op": "create_grant", "grant": link(world, 1, review_by=utc_stamp(world.clock.now))})


@pytest.mark.parametrize("name,bad", [
    ("id", "gp-0123456789ab"), ("id", "al-0123456789AB"), ("id", "al-0123456789a"),
    ("agent_id", "ag-" + "a1" * 15), ("agent_id", "ag-" + "A1" * 16), ("agent_id", "al-0123456789ab"),
    ("store_id", "ab" * 16), ("store_id", "store-" + "ab" * 15),
    ("spec_hash", "b" * 63), ("spec_hash", "B" * 64), ("role_hash", "c" * 65), ("role_hash", ""),
    ("binding_id", ""), ("binding_id", "has space"), ("binding_id", "x" * 129), ("binding_id", 7),
    ("kind", "graduation"), ("expires_at", "tomorrow"), ("guard", {"a": 1}), ("guard", ["x"]), ("guard", None),
])
def test_every_id_hash_and_field_is_held_to_its_form(world, name, bad):
    refusal = world.refused({"op": "create_grant", "grant": link(world, 1, **{name: bad})})
    assert refusal            # the exact words are the validator's; what matters is that no bad shape is signed


@pytest.mark.parametrize("caps", [
    {"harness": "claude", "model": "m", "where": [], "lands": [], "pushes": []},                       # a key short
    {"harness": "claude", "model": "m", "where": [], "lands": [], "pushes": [], "publishes": [], "x": 1},
    {"harness": "gemini", "model": "m", "where": [], "lands": [], "pushes": [], "publishes": []},
    {"harness": "claude", "model": 3, "where": [], "lands": [], "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "relative", "paths": ["a/"]}], "lands": [],
     "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r/../x", "paths": ["a/"]}], "lands": [],
     "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": []}], "lands": [], "pushes": [],
     "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": ["../a"]}], "lands": [], "pushes": [],
     "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": ["a/.GIT/hooks"]}], "lands": [],
     "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": ["/abs"]}], "lands": [], "pushes": [],
     "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": [".", "a/"]}], "lands": [], "pushes": [],
     "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": ["a/"], "extra": 1}], "lands": [],
     "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [{"repo": "/r", "paths": ["a/"]}, {"repo": "/r", "paths": ["b/"]}],
     "lands": [], "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [], "lands": "main", "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [], "lands": [""], "pushes": [], "publishes": []},
    {"harness": "claude", "model": "m", "where": [], "lands": [], "pushes": ["a‮b"], "publishes": []},
    {"harness": "claude", "model": "m", "where": [], "lands": [], "pushes": [], "publishes": [1]},
    {"harness": "claude", "model": "m\n", "where": [], "lands": [], "pushes": [], "publishes": []},
])
def test_capabilities_is_a_closed_mapping(world, caps):
    assert world.refused({"op": "create_grant", "grant": link(world, 1, capabilities=caps)})


def test_a_non_session_agent_signs_empty_harness_and_model(world):
    caps = {"harness": "", "model": "", "where": [], "lands": [], "pushes": [], "publishes": []}
    snap = create(world, 1, capabilities=caps)
    assert snap.policy.grants["al-000000000001"]["capabilities"]["harness"] == ""


def test_the_store_id_binds_the_record(world):
    # the grant's store_id is the autonomic store's (the record's own is the cockpit's): a malformed one is refused,
    # and a revoke names it, so a link signed for one store is untouched by a revoke for another
    assert "store_id must be store- and 32 lowercase hex digits" in world.refused(
        {"op": "create_grant", "grant": link(world, 1, store="store-xyz")})
    mine = create(world, 1)
    both = create(world, 2, store=OTHER_STORE)
    assert auth.agent_links(both, AGENT, STORE) == ["al-000000000001"]
    assert auth.agent_links(both, AGENT, OTHER_STORE) == ["al-000000000002"]
    assert "changes nothing" in world.refused(revoke_agent(store="store-" + "ee" * 16))
    after = world.link(revoke_agent(store=OTHER_STORE))
    assert auth.agent_links(after, AGENT, STORE) == ["al-000000000001"] and auth.agent_links(
        after, AGENT, OTHER_STORE) == []
    # the store id is part of what a link means: the same fields under another store are another incarnation
    other = auth.agent_link_incarnation(both, "al-000000000002")
    assert other is not None and other.split(":")[1] != auth.agent_link_incarnation(mine, "al-000000000001").split(":")[1]


# --- the op ---------------------------------------------------------------------------------------------------------------

def test_a_revoke_agent_naming_store_id_is_refused_at_prepare(world):
    create(world, 1)
    conn = sqlite3.connect(world.db, isolation_level=None)
    try:
        with pytest.raises(auth.AuthorityError, match="never format/store_id/seq/at"):
            auth.prepare(conn, {"op": "revoke_agent", "agent_id": AGENT, "store_id": STORE},
                         signer_fp=world.fp("laptop"), now=world.clock.now + timedelta(minutes=1),
                         cache=ReplayCache())
        # and the spelling of the field the plan names is the only one a record takes
        with pytest.raises(auth.AuthorityError, match="carries exactly"):
            auth.prepare(conn, {"op": "revoke_agent", "agent_id": AGENT}, signer_fp=world.fp("laptop"),
                         now=world.clock.now + timedelta(minutes=1), cache=ReplayCache())
    finally:
        conn.close()


@pytest.mark.parametrize("agent,store", [("ag-xyz", STORE), (AGENT, "store-xyz"), (7, STORE), (AGENT, None)])
def test_a_revoke_agent_with_a_malformed_id_is_refused(world, agent, store):
    create(world, 1)
    assert world.refused(revoke_agent(agent, store))


def test_a_revoke_agent_removing_two_links_signs_at_rank_1(tmp_path):
    world = World(tmp_path)
    presence = Key(tmp_path / "keys", "presence")
    world.link({"op": "enrol", "fingerprint": presence.fp, "public_key": presence.public_key, "class": "presence",
                "custody": "unstated"})
    world.keys["presence"] = (presence.fp, lambda m: presence.sign(m, auth.NAMESPACE))
    create(world, 1)
    create(world, 2)
    other = create(world, 3, agent=OTHER_AGENT)
    # the key class that can sign only a one-grant revoke (rank 1) signs the two-link revoke, and only that agent's
    # links go
    assert auth._ranks()["presence"] == 1 < auth._DELTA_RANK[auth.TIGHTEN]   # the class is below a TIGHTEN's rank
    after = world.link(revoke_agent(), "presence")
    assert auth.agent_links(after, AGENT, STORE) == [] and auth.agent_links(other, OTHER_AGENT, STORE)
    assert set(after.policy.grants) == {"al-000000000003"}
    # the key still signs nothing else of the policy
    assert "cannot sign this create_grant" in world.refused({"op": "create_grant", "grant": link(world, 4)},
                                                            "presence")


def test_any_enrolled_key_but_a_passkey_revokes_an_agent_through_the_new_op(world):
    # the passkey's own revoke_agent branch is P5a-2's; until it is built a passkey's revoke_agent is refused whole
    create(world, 1)
    create(world, 2)
    assert "signs consent answers and grant revokes only" in world.refused(revoke_agent(), "pk")
    after = world.link(revoke_agent(), "phone")
    assert auth.agent_links(after, AGENT, STORE) == []


def test_a_revoke_agent_with_no_agent_link_present_is_refused(world):
    assert "this revoke_agent changes nothing" in world.refused(revoke_agent())
    create(world, 1, agent=OTHER_AGENT)
    assert "this revoke_agent changes nothing" in world.refused(revoke_agent())
    # and a second revoke_agent over what the first removed finds nothing
    world.link(revoke_agent(OTHER_AGENT))
    assert "this revoke_agent changes nothing" in world.refused(revoke_agent(OTHER_AGENT))


def test_a_revoke_agent_over_an_expired_or_signer_revoked_link_is_accepted_and_removes_it(tmp_path):
    world = World(tmp_path)
    second = Key(tmp_path / "keys", "laptop2")
    world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key, "class": "laptop-presence",
                "custody": "passphrase"})
    world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
    create(world, 1, days=1)                                        # lapses
    create(world, 2, signer="laptop2", agent=OTHER_AGENT)         # its signer is revoked next
    world.link({"op": "revoke_key", "fingerprint": second.fp})
    world.clock.now += timedelta(days=2)
    snap = world.state()
    assert auth.agent_links(snap, AGENT, STORE) == ["al-000000000001"]          # present, though lapsed
    assert auth.agent_links(snap, OTHER_AGENT, STORE) == ["al-000000000002"]    # present, its signer revoked
    for agent, link_id in ((AGENT, "al-000000000001"), (OTHER_AGENT, "al-000000000002")):
        after = world.link(revoke_agent(agent))
        assert link_id not in after.policy.grants


# --- the replay semantics --------------------------------------------------------------------------------------------

def test_a_revoke_voids_an_earlier_incarnation(world):
    first = create(world, 1)
    old = auth.agent_link_incarnation(first, "al-000000000001")
    gone = world.link(revoke_agent())
    assert auth.agent_last_revoke(gone, AGENT, STORE) == gone.seq
    assert auth.agent_link_incarnation(gone, "al-000000000001") is None
    assert auth.agent_link_arms(gone, "al-000000000001", now=gone.head_at) is False       # no standing link
    again = create(world, 1)                                             # the same id signed again
    new = auth.agent_link_incarnation(again, "al-000000000001")
    assert new is not None and new != old and new.startswith(f"{again.seq}:")
    assert auth.agent_link_arms(again, "al-000000000001", now=again.head_at) is True
    assert auth.agent_last_revoke(again, AGENT, STORE) == gone.seq      # the revoke stays on the record
    assert auth.agent_last_revoke(again, OTHER_AGENT, STORE) == 0
    # a renew keeps the incarnation
    at = world.clock.now + timedelta(minutes=1)
    renewed = world.link({"op": "renew", "grant_id": "al-000000000001",
                          "expires_at": utc_stamp(at + timedelta(days=30)), "signed_at": utc_stamp(at)})
    assert auth.agent_link_incarnation(renewed, "al-000000000001") == new


def test_a_link_prepared_before_a_revoke_agent_cannot_be_appended_after_it(world):
    create(world, 1)
    conn = sqlite3.connect(world.db, isolation_level=None)
    try:
        cache = ReplayCache()
        pending = auth.prepare(conn, {"op": "create_grant", "grant": link(world, 2)}, signer_fp=world.fp("laptop"),
                               now=world.clock.now + timedelta(minutes=1), cache=cache)
        signature = world.keys["laptop"][1](pending.message)
        world.link(revoke_agent())
        with pytest.raises(auth.StaleHead):
            auth.append(conn, pending, signature, now=world.clock.now + timedelta(minutes=2),
                        anchor=auth.anchor_path(world.root), cache=ReplayCache())
    finally:
        conn.close()
    assert auth.agent_links(world.state(), AGENT, STORE) == []


def test_a_link_born_at_or_below_the_last_revoke_or_a_closed_bar_never_arms(world):
    snap = create(world, 1)
    born = snap.grant_born["al-000000000001"]
    assert auth.agent_link_arms(snap, "al-000000000001", now=snap.head_at) is True
    # a closed bar's revoke_seq at or above the birth: the link never arms; below it, it does
    assert auth.agent_link_arms(snap, "al-000000000001", now=snap.head_at, closed_bar_revoke_seq=born) is False
    assert auth.agent_link_arms(snap, "al-000000000001", now=snap.head_at, closed_bar_revoke_seq=born + 5) is False
    assert auth.agent_link_arms(snap, "al-000000000001", now=snap.head_at, closed_bar_revoke_seq=born - 1) is True
    # the same for the agent's own last revoke_agent (a state the replay cannot produce with the link still standing,
    # so it is made by hand to hold the comparison)
    for last, expect in ((born, False), (born + 3, False), (born - 1, True)):
        marked = replace(snap, agent_revoked={(AGENT, STORE): last})
        assert auth.agent_link_arms(marked, "al-000000000001", now=marked.head_at) is expect, last
    # fails closed on anything that is not a standing agent link, and on a bar seq that is no integer
    assert auth.agent_link_arms(snap, "al-ffffffffffff", now=snap.head_at) is False
    assert auth.agent_link_arms(snap, "al-000000000001", now=snap.head_at, closed_bar_revoke_seq=None) is False


def test_an_agent_link_has_fixed_terms_and_only_a_renew_changes_it(world):
    create(world, 1)
    policy = world.state().policy.as_dict()
    policy["grants"]["al-000000000001"]["run_ceiling"] = 9
    assert "an agent link's terms are fixed: sign a new link" in world.refused({"op": "loosen", "policy": policy})
    # the same id signed again while it stands is refused too
    assert "already exists" in world.refused({"op": "create_grant", "grant": link(world, 1, run_ceiling=9)})
    # a kind cannot be turned into an agent link in place
    other = world.state().policy.as_dict()
    other["grants"]["al-000000000001"] = {**link(world, 1), "kind": "unknown-kind"}
    assert world.refused({"op": "loosen", "policy": other})


# --- review round 1: only a renew moves a link, arming is bounded by time ---------------------------------------------

AGENT_RENEW_ONLY = "an agent link's expiry and terms move only by its own renew link"


def test_only_a_renew_moves_an_existing_agent_links_expiry(world):
    # a loosen that also brings in another grant computes to LOOSEN, which would skip renew's lapse rule and bring a
    # lapsed link back under its old born seq and incarnation; it is refused at append and at replay, live or lapsed
    first = create(world, 1, days=1)
    world.clock.now += timedelta(days=2)
    policy = world.state().policy.as_dict()
    policy["grants"]["al-000000000001"]["expires_at"] = utc_stamp(world.clock.now + timedelta(days=20))
    policy["grants"]["al-000000000002"] = link(world, 2)
    assert AGENT_RENEW_ONLY in world.refused({"op": "loosen", "policy": policy})
    db = world.plant({"op": "loosen", "policy": policy}, fmt=V4)
    assert AGENT_RENEW_ONLY in world.replay_verdict(db)
    # a live link's expiry is no different: moved inside a loosen, refused
    create(world, 3)
    live = world.state().policy.as_dict()
    live["grants"]["al-000000000003"]["expires_at"] = utc_stamp(world.clock.now + timedelta(days=29, hours=23))
    live["grants"]["al-000000000004"] = link(world, 4)
    assert AGENT_RENEW_ONLY in world.refused({"op": "loosen", "policy": live})
    # nor shortened by a loosen (the kind refuses any change but a later expiry before the step checks run)
    short = world.state().policy.as_dict()
    short["grants"]["al-000000000003"]["expires_at"] = utc_stamp(world.clock.now + timedelta(days=1))
    short["grants"]["al-000000000004"] = link(world, 4)
    assert "an agent link's terms are fixed: sign a new link" in world.refused({"op": "loosen", "policy": short})
    # the lapsed link is never renewed either; it comes back only as a new link: a create, with a new incarnation
    at = world.clock.now + timedelta(minutes=1)
    assert "has expired" in world.refused({"op": "renew", "grant_id": "al-000000000001",
                                           "expires_at": utc_stamp(at + timedelta(days=10)),
                                           "signed_at": utc_stamp(at)})
    old = auth.agent_link_incarnation(first, "al-000000000001")
    world.link({"op": "revoke_grant", "grant_id": "al-000000000001"})
    again = create(world, 1)
    assert auth.agent_link_incarnation(again, "al-000000000001") != old
    # a loosen that leaves every agent link as it was still goes through
    keep = world.state().policy.as_dict()
    keep["grants"]["al-000000000005"] = link(world, 5)
    assert "al-000000000005" in world.link({"op": "loosen", "policy": keep}).policy.grants


def test_an_expired_or_not_yet_valid_link_never_arms(world):
    snap = create(world, 1, days=2)
    at = snap.head_at
    expires = auth._parse_stamp(snap.policy.grants["al-000000000001"]["expires_at"], "expires_at")
    assert auth.agent_link_arms(snap, "al-000000000001", now=at) is True
    assert auth.agent_link_arms(snap, "al-000000000001", now=expires - timedelta(seconds=1)) is True
    # the boundary instant: at its expiry a link is no longer live (the shape every expiry in the log has)
    assert auth.agent_link_arms(snap, "al-000000000001", now=expires) is False
    assert auth.agent_link_arms(snap, "al-000000000001", now=expires + timedelta(days=20)) is False
    # whatever the bar says
    assert auth.agent_link_arms(snap, "al-000000000001", now=expires, closed_bar_revoke_seq=0) is False
    # a clock behind the log's head cannot rule out a time before the link's own: it does not arm
    assert auth.agent_link_arms(snap, "al-000000000001", now=at - timedelta(seconds=1)) is False
    # a now that is no aware datetime fails closed, and so does a negative bar seq
    from datetime import datetime
    for bad in (None, "2026-01-01T00:00:00Z", at.replace(tzinfo=None), datetime.min):
        assert auth.agent_link_arms(snap, "al-000000000001", now=bad) is False, bad
    assert auth.agent_link_arms(snap, "al-000000000001", now=at, closed_bar_revoke_seq=-1) is False
    with pytest.raises(TypeError):
        auth.agent_link_arms(snap, "al-000000000001")            # now is required: no reader forgets the clock


def test_a_renew_already_expired_at_its_signing_time_or_past_max_life_is_refused(world):
    create(world, 1, days=1)
    # signed after the link lapsed, its new expiry past the old one: the lapse rule refuses it
    world.clock.now += timedelta(days=1, minutes=5)
    at = world.clock.now + timedelta(minutes=1)
    assert "has expired" in world.refused({"op": "renew", "grant_id": "al-000000000001",
                                           "expires_at": utc_stamp(at + timedelta(minutes=1)),
                                           "signed_at": utc_stamp(at)})
    # a renew landing at or before its own signing time cannot be built from a live link (it must move the expiry
    # later than one still ahead), and one past max_life is refused at the boundary plus a second
    create(world, 2, days=1)
    at = world.clock.now + timedelta(minutes=1)
    assert "must move the grant's expiry later" in world.refused(
        {"op": "renew", "grant_id": "al-000000000002", "expires_at": utc_stamp(at), "signed_at": utc_stamp(at)})
    at = world.clock.now + timedelta(minutes=1)
    assert "at most 30 days" in world.refused(
        {"op": "renew", "grant_id": "al-000000000002",
         "expires_at": utc_stamp(at + auth.AGENT_LINK_MAX + timedelta(seconds=1)), "signed_at": utc_stamp(at)})
    at = world.clock.now + timedelta(minutes=1)
    renewed = world.link({"op": "renew", "grant_id": "al-000000000002",
                          "expires_at": utc_stamp(at + auth.AGENT_LINK_MAX), "signed_at": utc_stamp(at)})
    assert renewed.policy.grants["al-000000000002"]["expires_at"] == utc_stamp(at + auth.AGENT_LINK_MAX)
    # a renew written by hand with signed_at off its own at is refused at replay
    later = world.clock.now + timedelta(days=1)
    db = world.plant({"op": "renew", "grant_id": "al-000000000002", "expires_at": utc_stamp(later + timedelta(days=29)),
                      "signed_at": utc_stamp(later)}, fmt=V1)
    assert "signed_at must equal its at" in world.replay_verdict(db)


def test_a_link_brought_in_already_expired_is_refused(world):
    # a create refuses one; a loosen that brings one in is held to the same rule
    assert "must expire after the link that creates it" in world.refused(
        {"op": "create_grant", "grant": link(world, 1, days=-1)})
    policy = world.state().policy.as_dict()
    policy["grants"]["al-000000000001"] = link(world, 1, days=0)
    assert "must expire after the link that brings it in" in world.refused({"op": "loosen", "policy": policy})


def test_a_revoke_grant_of_an_agents_last_link_does_not_set_agent_revoked(world):
    # A0 §3.2's invariant 2 names the agent's last revoke_agent; a revoke_grant removes one link and is no agent revoke,
    # so a later link of the agent arms (its own born seq is above the removed one's anyway)
    create(world, 1)
    gone = world.link({"op": "revoke_grant", "grant_id": "al-000000000001"})
    assert auth.agent_links(gone, AGENT, STORE) == []
    assert auth.agent_last_revoke(gone, AGENT, STORE) == 0 and dict(gone.agent_revoked) == {}
    again = create(world, 2)
    assert auth.agent_link_arms(again, "al-000000000002", now=again.head_at) is True


def test_a_repo_reaching_into_git_and_a_non_finite_guard_are_refused(world):
    for repo in ("/x/.git", "/x/.GIT/modules/y"):
        caps = {"harness": "claude", "model": "m", "where": [{"repo": repo, "paths": ["."]}], "lands": [],
                "pushes": [], "publishes": []}
        assert "never reaches into .git" in world.refused({"op": "create_grant",
                                                           "grant": link(world, 1, capabilities=caps)}), repo
    for bad in (float("nan"), float("inf")):
        assert "finite JSON" in world.refused({"op": "create_grant", "grant": link(world, 1, guard=[{"v": bad}])})


# --- the two link shapes (P7-A §2.2, §4): the shipped one and the one with the route's three fields ---------------------

ROUTE = {"billing": "api_key", "budget_run_calls": 0, "budget_run_tunnel_bytes": 0}
SUB = {"billing": "subscription", "budget_run_usd_micros": 0, "budget_window_usd_micros": 0,
       "budget_run_calls": 20, "budget_run_tunnel_bytes": 5_000_000}


def test_the_shipped_shape_is_accepted_and_reads_as_api_key_with_zero_caps(world):
    snap = create(world, 1)    # link() builds the shipped shape
    assert auth.agent_links(snap, AGENT, STORE) == ["al-000000000001"]
    assert auth.agent_link_terms(snap.policy.grants["al-000000000001"]) == ("api_key", 0, 0)


def test_the_new_shape_is_accepted_for_both_billings(world):
    snap = create(world, 1, **ROUTE)
    assert auth.agent_link_terms(snap.policy.grants["al-000000000001"]) == ("api_key", 0, 0)
    snap = create(world, 2, **SUB)
    assert auth.agent_link_terms(snap.policy.grants["al-000000000002"]) == ("subscription", 20, 5_000_000)


def test_a_partial_or_mixed_shape_is_refused(world):
    for keep in (("billing",), ("budget_run_calls",), ("budget_run_tunnel_bytes",),
                 ("billing", "budget_run_calls"), ("budget_run_calls", "budget_run_tunnel_bytes")):
        grant = link(world, 1, **{k: ROUTE[k] for k in keep})
        assert "must carry exactly" in world.refused({"op": "create_grant", "grant": grant}), keep
    # the full new set minus one shipped field is neither shape either
    short = link(world, 1, **ROUTE)
    del short["run_ceiling"]
    assert "must carry exactly" in world.refused({"op": "create_grant", "grant": short})


def test_the_route_rule_binds_the_new_shape(world):
    cases = [
        ({**SUB, "budget_run_usd_micros": 1}, "subscription link"),
        ({**SUB, "budget_window_usd_micros": 1}, "subscription link"),
        ({**SUB, "budget_run_calls": 0}, "subscription link"),
        ({**SUB, "budget_run_tunnel_bytes": 0}, "subscription link"),
        ({**ROUTE, "budget_run_calls": 1}, "api_key link"),
        ({**ROUTE, "budget_run_tunnel_bytes": 1}, "api_key link"),
        ({**ROUTE, "billing": "oauth"}, "billing must be one of"),
        ({**ROUTE, "billing": None}, "billing must be one of"),
        ({**ROUTE, "billing": ["api_key"]}, "billing must be one of"),
    ]
    for over, text in cases:
        assert text in world.refused({"op": "create_grant", "grant": link(world, 1, **over)}), over
    for name in ("budget_run_calls", "budget_run_tunnel_bytes"):
        for bad in (1.5, True, -1, 10**13, "5", None):
            assert f"{name} must be an integer from 0 to" in world.refused(
                {"op": "create_grant", "grant": link(world, 1, **{**SUB, name: bad})}), (name, bad)


def test_a_link_of_either_shape_replays_and_its_terms_stay_fixed(world):
    create(world, 1)
    create(world, 2, **SUB)
    snap = world.state()
    assert auth.agent_link_terms(snap.policy.grants["al-000000000001"])[0] == "api_key"
    assert auth.agent_link_terms(snap.policy.grants["al-000000000002"])[0] == "subscription"
