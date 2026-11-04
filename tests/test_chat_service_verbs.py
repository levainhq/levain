"""The public verbs a composing surface uses on ChatService (turn_view, turn_claim, turn_release, message_thread,
session): each verb, its refusals, that a claim goes through the same lease and the same send fence as any other, and
that what a read verb returns is a copy."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import ChatService, ChatStore, MessageThread, TurnRefused, record_check
from levain.coordinator.driver import ScriptedSession
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

LIVE = {"probe": lambda _l, _e: chat.Verdict(True, "scripted probe: live"), "sleep": lambda _s: None}
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
ME = "cockpit:tab-a"
PHONE = "phone:iphone-1"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return EnvelopeStore(d, clock=clock)


def _launch(store: EnvelopeStore, token: int, thread: str) -> str:
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-" + ln.launch_id[-6:])
    assert why is None and got.state == "active"
    return ln.launch_id


@pytest.fixture
def world(store):
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0].thread_id
    return {"thread": thread, "launch": _launch(store, token, thread), "token": token}


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


@pytest.fixture
def svc(cs, world):
    with cs._write() as (conn, now):
        record_check(conn, now, world["launch"], "live", "activation", "test")
    session = ScriptedSession()
    s = ChatService(cs, **LIVE)
    s.attach(world["launch"], session)
    s.ingest(world["launch"], session.read(None))
    s.scripted = session
    return s


def send(svc, world, epoch, *, outbox="ob-00000001", owner=ME, launch=None):
    surface, client = owner.split(":", 1)
    return svc.send(launch or world["launch"], author_kind="operator", author_surface=surface, client_id=client,
                    outbox_id=outbox, body="go", expected_epoch=epoch)


# --- turn_view ------------------------------------------------------------------------------------------------------
def test_turn_view_free_yours_taken(svc, world):
    launch = world["launch"]
    assert svc.turn_view(launch, ME) == chat.TurnView("free", None, None)
    epoch = svc.turn_claim(launch, ME)
    assert svc.turn_view(launch, ME) == chat.TurnView("yours", epoch, None)
    other = svc.turn_view(launch, PHONE)
    assert other.state == "taken" and other.epoch == epoch and other.holder


def test_turn_view_refuses_a_non_client_owner(svc, world):
    with pytest.raises(TurnRefused):
        svc.turn_view(world["launch"], "not-an-owner")


def test_turn_view_is_a_copy(svc, world):
    svc.turn_claim(world["launch"], ME)
    v = svc.turn_view(world["launch"], ME)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.epoch = 999                                                      # type: ignore[misc]
    assert svc.turn_view(world["launch"], ME).epoch != 999


# --- turn_claim: the lease path and the send fence ---------------------------------------------------------------------
def test_claim_is_the_stores_claim_and_send_accepts_its_epoch(svc, cs, world):
    epoch = svc.turn_claim(world["launch"], ME)
    assert cs.turn(world["launch"]).epoch == epoch and cs.turn(world["launch"]).owner == ME
    assert send(svc, world, epoch).state == "accepted"


def test_claim_refused_while_the_turn_is_held(svc, world):
    svc.turn_claim(world["launch"], PHONE)
    with pytest.raises(TurnRefused, match="held"):
        svc.turn_claim(world["launch"], ME)


def test_claim_refused_for_a_non_client_owner_and_an_unknown_launch(svc, world):
    with pytest.raises(TurnRefused):
        svc.turn_claim(world["launch"], "coordinator:coord-1")
    with pytest.raises(CoordError):
        svc.turn_claim("ln-" + "0" * 16, ME)


def test_claim_refused_on_an_ended_launch_as_send_refuses(svc, store, world):
    epoch = svc.turn_claim(world["launch"], ME)
    store.launch_end(world["launch"], "ended", "done")
    with pytest.raises(CoordError):
        svc.turn_claim(world["launch"], PHONE)
    r = send(svc, world, epoch)                                            # send refuses the same launch
    assert r.state == "refused" and "only a running worker" in r.cause


def test_claim_against_a_replaced_launch_is_refused_and_the_new_launch_is_unaffected(svc, store, world):
    old = world["launch"]
    store.launch_end(old, "ended", "replaced")
    new = _launch(store, world["token"], world["thread"])
    with pytest.raises(CoordError):
        svc.turn_claim(old, ME)
    assert svc.turn_claim(new, ME) > 0 and svc.turn_view(new, ME).state == "yours"


def test_a_lapsed_epoch_is_refused_by_send_as_before_a_new_claim_does_not_revive_it(svc, world, clock):
    old = svc.turn_claim(world["launch"], ME)
    clock.advance(hours=2)                                                 # past the lease's ttl and hold cap
    assert svc.turn_view(world["launch"], ME).state == "free"
    stale = send(svc, world, old)
    assert stale.state == "refused" and stale.cause                       # the fence, as it was without the verb
    new = svc.turn_claim(world["launch"], ME)
    assert new != old
    again = send(svc, world, old, outbox="ob-00000002")
    assert again.state == "refused"                                        # the old epoch is still the stale one
    assert send(svc, world, new, outbox="ob-00000003").state == "accepted"


def test_the_wrong_owners_epoch_is_refused_by_send(svc, world):
    epoch = svc.turn_claim(world["launch"], PHONE)
    r = send(svc, world, epoch, owner=ME)
    assert r.state == "refused" and "not yours" in r.cause


# --- turn_release -----------------------------------------------------------------------------------------------------
def test_release_frees_the_turn_and_refuses_a_stranger(svc, world):
    epoch = svc.turn_claim(world["launch"], ME)
    with pytest.raises(TurnRefused):
        svc.turn_release(world["launch"], epoch, PHONE)
    assert svc.turn_view(world["launch"], ME).state == "yours"
    svc.turn_release(world["launch"], epoch, ME)
    assert svc.turn_view(world["launch"], ME).state == "free"
    with pytest.raises(TurnRefused):
        svc.turn_release(world["launch"], epoch, ME)                       # already ended


# --- message_thread ------------------------------------------------------------------------------------------------------
def test_message_thread_names_where_a_send_went(svc, world):
    assert svc.message_thread(ME, "ob-00000001") is None
    epoch = svc.turn_claim(world["launch"], ME)
    r = send(svc, world, epoch)
    got = svc.message_thread(ME, "ob-00000001")
    assert got == MessageThread(r.message_id, world["thread"], world["launch"], epoch)
    assert svc.message_thread(PHONE, "ob-00000001") is None              # another owner's id answers nothing
    assert svc.message_thread("tab-a", "ob-00000001") is None              # a bare client id is no owner
    assert svc.message_thread("phone:tab-a", "ob-00000001") is None        # same client id, another surface (codex MED 5)


def test_message_thread_is_a_copy(svc, world):
    send(svc, world, svc.turn_claim(world["launch"], ME))
    got = svc.message_thread(ME, "ob-00000001")
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.thread_id = "th-other"                                         # type: ignore[misc]
    assert svc.message_thread(ME, "ob-00000001").thread_id == world["thread"]


# --- session: a snapshot of the meta read at attach, never the live session ----------------------------------------
def test_session_is_the_attached_ones_meta_or_none(svc, world, cs):
    assert svc.session(world["launch"]) == svc.scripted.meta() and svc.session(world["launch"]) is not svc.scripted
    assert svc.session("ln-" + "0" * 16) is None
    svc.detach(world["launch"])
    assert svc.session(world["launch"]) is None
