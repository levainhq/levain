"""H1-5 lane 3: what the signer reads before ONE signature covers several threads, and how the cards after it are grouped.
Each test names the failure it reproduces. The words are the server's (``draft_sign._sheet_lines``, in the review's
``view``); the page draws them. A one-thread draft's sheet and replies are pinned unchanged. Nothing here signs a real
key: the passkey is the phone fixture's software one."""
from __future__ import annotations

import json
import shutil
from datetime import timedelta
from types import SimpleNamespace

import pytest

from levain.desk import draft_sign as ds
from levain.desk import envelope_drafts as drafts
from levain.desk import missions
from levain.desk.envelope import utc_stamp
from levain.desk.store import Part, Source
from tests.test_desk_draft_sign import Rig
from tests.test_desk_envelope import BUDGET
from tests.test_desk_envelope_draft import cites_for
from tests.test_dispatch_contract import make_repo
from tests.test_dispatch_phone import world  # noqa: F401  (fixture)
from tests.test_mission_draft import MissionRig

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SIX_KEYS = {"review_id", "body_text", "rp_id", "since_parent", "against", "view", "store_id"}


@pytest.fixture
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("mission-glass-repo"))


def mission_rig(world, n=3):
    rig = MissionRig(world)
    d, _ = rig.store.accept_drop("land the readme change in parts", surface="cli", source=Source())
    ts = sorted(rig.store.split(d.drop_id, [Part("work", f"part {i}") for i in range(n)]), key=lambda t: t.thread_id)
    m = missions.create_mission(rig.store, [t.thread_id for t in ts])
    f = {"where": [{"repo": rig.repo, "paths": ["."]}], "lands": [], "pushes": [], "publishes": [],
         "budget": dict(BUDGET), "stop_at": utc_stamp(rig.clock.t + timedelta(hours=2))}
    dr = drafts.create_mission_draft(rig.store, m.mission_id, f, cites_for(d, f), by="operator")
    return rig, ts, m, dr


def test_the_sheet_lists_every_bound_thread_by_title_and_says_what_one_signature_covers(world):
    """Failure (L1 F1/F2 on the base): the sheet for a three-thread draft named no thread, no count and no multiplier."""
    rig, ts, _m, dr = mission_rig(world, 3)
    r = rig.review({"draft_id": dr.draft_id, "revision": dr.revision, "digest": dr.digest})
    text = "\n".join(r["view"])
    for t in ts:
        assert f"thread {t.thread_id}: {t.title}" in r["view"]
    each = "200 tool calls, 2000000 tokens, $5.00, 2 hours"
    assert (f"this one signature covers these 3 threads, each up to {each}, up to 3 x ({each}) together "
            "(600 tool calls, 6000000 tokens, $15.00, 6 hours); a thread added later is not covered") in r["view"]
    assert "NOT covered" in text and "needs a signature of its own" in text
    # the per-thread change text is not built: said plainly, naming the field to read
    assert 'what changed for each thread is not shown on this sheet' in text and '"binds"' in text
    # the lane-1 refusals, in plain words
    assert "none of these threads can hold a draft of its own" in text
    assert r["view"][0] == "no head"                       # the base's own line stays first


def test_a_sheet_with_no_budget_value_says_so_instead_of_a_multiplier(world):
    rig, ts, _m, dr = mission_rig(world, 2)
    lines = ds._sheet_lines(rig.store, json.dumps(
        {"binds": [{"thread_id": t.thread_id} for t in ts], "budget": {"tool_calls": None, "tokens": None,
                                                                       "dollars_cents": None, "hours": None}}))
    assert lines[0] == "this one signature covers these 2 threads, with no budget limit set for any of them"


def test_a_one_thread_sheet_is_exactly_what_it_was(world):
    """Pinned: a one-thread draft's review reply has no new key and its ``view`` is the head's own words, nothing added."""
    rig = Rig(world)
    r = rig.review(rig.extend())
    assert set(r) == SIX_KEYS
    assert r["view"] == ds._view_lines(SimpleNamespace(**r["against"]))
    assert not any("signature covers" in line or "binds" in line for line in r["view"])
    assert ds._sheet_lines(rig.store, r["body_text"]) == []


def test_the_signed_reply_groups_the_cards_by_thread_and_a_one_thread_reply_has_no_groups(world, monkeypatch):
    """Failure (L1 WARNING 1 on the base): one flat list of every child's cards, no thread on a card, so the page drew them
    under one thread and posted a renew or replace to the anchor."""
    rig, ts, _m, dr = mission_rig(world, 3)
    r = rig.review({"draft_id": dr.draft_id, "revision": dr.revision, "digest": dr.digest})
    monkeypatch.setattr(ds, "_cards", lambda store, tid: [{"on": tid}])
    _c, req = rig.sign(r)
    got = rig.accept(req)
    assert got["created"] is True
    assert [g["thread_id"] for g in got["groups"]] == [t.thread_id for t in ts]
    assert [g["title"] for g in got["groups"]] == [t.title for t in ts]
    assert [g["cards"] for g in got["groups"]] == [[{"on": t.thread_id}] for t in ts]
    assert got["cards"] == [{"on": t.thread_id} for t in ts]                       # the flat list stays
    assert all(g["shared"]["mission_id"] and len(g["shared"]["threads"]) == 3 for g in got["groups"])
    # the one-thread reply: unchanged, no groups
    assert ds._cards_after(rig.store, lambda: ts[0].thread_id) == {"cards": [{"on": ts[0].thread_id}]}
    assert ds._cards_after(rig.store, lambda: [ts[0].thread_id]) == {"cards": [{"on": ts[0].thread_id}]}


def test_a_signed_childs_card_read_says_the_head_is_shared_and_a_one_thread_head_does_not(world):
    rig, ts, m, dr = mission_rig(world, 3)
    r = rig.review({"draft_id": dr.draft_id, "revision": dr.revision, "digest": dr.digest})
    _c, req = rig.sign(r)
    assert rig.accept(req)["created"] is True
    got = rig.call("draft-cards", thread_id=ts[1].thread_id)
    shared = got["shared"]
    assert shared["mission_id"] == m.mission_id and shared["envelope_id"].startswith("ev-")
    assert shared["threads"] == [{"thread_id": t.thread_id, "title": t.title} for t in ts]
    one = Rig(world, "2")
    assert "shared" not in one.call("draft-cards", thread_id=one.tid)
