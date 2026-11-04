"""H1-3: the sign flow of a draft envelope (levain.coordinator.draft_sign) on the phone port's CoordPhone, which the
cockpit's CoordSheet carries by name.

The world is test_coordinator_phone's: a real authority log holding a laptop key and a SOFTWARE passkey that answers a
WebAuthn challenge as a phone's authenticator does. The head is signed by that passkey through the existing drop flow;
every successor signature is one the server issued a challenge for and the store's one verifier accepts or refuses.
The refusals come first. What no instrument here holds: the passkey on his own device, and the cards on glass."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.phone import CoordRefused
from levain.coordinator.store import Part, Source
from tests.test_coordinator_envelope import BUDGET
from tests.test_coordinator_phone import DEV, coord_for, signed_drop, sid
from tests.test_dispatch_phone import world, repo  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

OTHER_DEV = "d-ffffffffffff"


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.t


class Rig:
    """A thread whose head the passkey signed, a phone port and a store on one clock the test moves."""

    def __init__(self, world, n: str = "1") -> None:
        self.world = world
        self.clock = Clock()
        self.cp = coord_for(world, clock=self.clock)
        self.repo = str(world["repo"][0])
        spec = {"where": [{"repo": self.repo, "paths": ["."]}], "budget": dict(BUDGET), "stop": "2h"}
        _r, _e, got = signed_drop(world, self.cp, "land the readme change", "ph-" + n * 32, spec)
        assert got["envelope"]["taken"] is True, got
        self.head = got["envelope"]["envelope_id"]
        self.store = EnvelopeStore(world["qdir"], clock=self.clock)
        (self.thread,) = self.store.split(got["drop"]["drop_id"], [Part("work", "hello")])
        self.tid = self.thread.thread_id

    def call(self, action: str, device: str = DEV, **req):
        if action in ("draft-cards", "draft-rule", "draft-withdraw", "draft-review"):
            req.setdefault("store_id", sid(self.cp))
        return self.cp.step(action, req, device=device)

    def lapse(self) -> None:
        self.clock.t += timedelta(hours=3)

    def card(self) -> dict:
        (c,) = self.call("draft-cards", thread_id=self.tid)["cards"]
        return c

    def extend(self) -> dict:
        self.lapse()
        c = self.card()
        assert c["kind"] == "LAPSED" and "extend" in c["actions"], c
        return self.call("draft-rule", thread_id=self.tid, rule="renew", head=self.head)["draft"]

    def review(self, d: dict, device: str = DEV, **over):
        req = {"draft_id": d["draft_id"], "revision": d["revision"], "digest": d["digest"],
               "signer_fp": self.world["phone"].fp, **over}
        return self.call("draft-review", device, **req)

    def challenge(self, r: dict, device: str = DEV):
        return self.call("draft-challenge", device, review_id=r["review_id"])

    def sign(self, r: dict, device: str = DEV, **assert_kw):
        c = self.challenge(r, device)
        a = self.world["phone"].assertion(c["challenge"], **assert_kw)
        return c, {"review_id": r["review_id"], "body_text": r["body_text"], "payload": c["payload"], **a}

    def accept(self, req: dict, device: str = DEV):
        return self.call("draft-accept", device, **req)

    def envelopes(self) -> list[str]:
        return self.rows("SELECT envelope_id FROM envelopes")

    def rows(self, sql: str) -> list[str]:
        import sqlite3

        from levain.dispatch.jobs import DB_NAME

        conn = sqlite3.connect(self.world["qdir"] / DB_NAME)
        try:
            return [r[0] for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()


@pytest.fixture
def rig(world):
    return Rig(world)


def refused(exc: pytest.ExceptionInfo) -> tuple[int, str]:
    return exc.value.status, exc.value.error


# --- the refusals ---------------------------------------------------------------------------------------------------

def test_an_unsigned_accept_stores_nothing(rig):
    d = rig.extend()
    r = rig.review(d)
    before = rig.envelopes()
    # no challenge was asked for: nothing was minted to sign
    with pytest.raises(CoordRefused) as exc:
        rig.accept({"review_id": r["review_id"], "body_text": r["body_text"], "payload": {}, "authenticator_data": "",
                    "client_data_json": "", "signature": ""})
    assert refused(exc) == (409, "no_challenge")
    # a challenge asked for, but no assertion in the request: a malformed request, not a signature
    c = rig.challenge(r)
    with pytest.raises(CoordRefused) as exc:
        rig.accept({"review_id": r["review_id"], "body_text": r["body_text"], "payload": c["payload"]})
    assert exc.value.status == 400
    # an assertion that is not the passkey's answer to THIS challenge (another challenge's, so wrong bytes)
    other = rig.world["phone"].assertion(c["challenge"][:-2] + "AA")
    with pytest.raises(CoordRefused) as exc:
        rig.accept({"review_id": r["review_id"], "body_text": r["body_text"], "payload": c["payload"], **other})
    assert exc.value.status in (422, 503)
    assert rig.envelopes() == before


def test_an_assertion_from_another_origin_stores_nothing(rig):
    d = rig.extend()
    r = rig.review(d)
    before = rig.envelopes()
    _c, req = rig.sign(r, origin="http://localhost:9999")
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert exc.value.status == 422
    assert rig.envelopes() == before


def test_a_signed_review_is_used_once_and_a_signed_draft_takes_no_second_review(rig):
    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    got = rig.accept(req)
    assert got["created"] is True
    after = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:          # the same assertion again: the review is spent
        rig.accept(req)
    assert refused(exc) == (409, "review_gone")
    with pytest.raises(CoordRefused) as exc:          # and the signed draft is no longer signable by a new review
        rig.review(d)
    assert refused(exc) == (409, "draft_stale")
    assert rig.envelopes() == after


def test_a_thread_that_changed_after_render_refuses_at_challenge_and_at_accept(rig):
    d = rig.extend()
    r, r2 = rig.review(d), rig.review(d)
    c, req = rig.sign(r)                       # the challenge was minted while the thread still matched
    late = rig.store.accept_drop("one more thing", surface="cli", source=Source())[0]
    rig.store.split(late.drop_id, [Part("work", attach=rig.tid)])          # the thread's revision moves
    before = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:   # a challenge asked for now builds no bytes
        rig.challenge(r2)
    assert refused(exc) == (409, "draft_stale")
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert refused(exc) == (409, "draft_stale")
    with pytest.raises(CoordRefused) as exc:
        rig.review(d)
    assert refused(exc) == (409, "draft_stale")
    assert rig.envelopes() == before


def test_a_head_narrowed_after_render_is_refused(rig):
    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    assert rig.store.attenuate(rig.head, "kill")           # he kills the head while the sheet is open
    before = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert refused(exc) == (409, "draft_stale")
    assert rig.envelopes() == before


def test_a_rule_over_a_head_the_card_did_not_show_is_refused_and_writes_no_draft(rig):
    rig.lapse()
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-rule", thread_id=rig.tid, rule="renew", head="ev-" + "0" * 32)
    assert refused(exc) == (409, "draft_stale")
    assert rig.rows("SELECT draft_id FROM envelope_drafts") == []


def test_extend_on_a_killed_head_is_refused_and_replace_is_the_way(rig):
    assert rig.store.attenuate(rig.head, "kill")
    c = rig.card()
    assert c["kind"] == "REVOKED" and c["actions"] == ["replace", "close"]
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-rule", thread_id=rig.tid, rule="renew", head=rig.head)
    assert exc.value.status == 422 and "no extend" in exc.value.message
    assert rig.rows("SELECT draft_id FROM envelope_drafts") == []


def test_a_review_is_bound_to_the_device_that_made_it(rig):
    d = rig.extend()
    r = rig.review(d)
    with pytest.raises(CoordRefused) as exc:
        rig.challenge(r, OTHER_DEV)
    assert refused(exc) == (409, "review_gone")
    _c, req = rig.sign(r)
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req, OTHER_DEV)
    assert refused(exc) == (409, "review_gone")
    assert rig.accept(req)["created"] is True              # the right device still can


def test_a_swapped_body_or_payload_is_not_what_the_challenge_minted(rig):
    d = rig.extend()
    r = rig.review(d)
    c, req = rig.sign(r)
    before = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:
        rig.accept({**req, "body_text": req["body_text"].replace('"hours":2', '"hours":9')})
    assert exc.value.status == 422
    with pytest.raises(CoordRefused) as exc:
        rig.accept({**req, "payload": {**req["payload"], "signed_at": "2026-01-01T00:00:00.000000Z"}})
    assert exc.value.status == 422
    assert rig.envelopes() == before


def test_a_key_that_is_not_enrolled_and_another_store_sign_nothing(rig):
    d = rig.extend()
    with pytest.raises(CoordRefused) as exc:
        rig.review(d, signer_fp="SHA256:nobody")
    assert refused(exc) == (422, "no_passkey")
    with pytest.raises(CoordRefused) as exc:
        rig.review(d, store_id="not-this-store")
    assert refused(exc) == (409, "other_store")
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-cards", thread_id=rig.tid, store_id="not-this-store")
    assert refused(exc) == (409, "other_store")


def test_a_request_with_a_missing_or_extra_key_is_a_400_and_no_device_is_a_403(rig):
    with pytest.raises(CoordRefused) as exc:
        rig.cp.step("draft-review", {"draft_id": "x"}, device=DEV)
    assert exc.value.status == 400
    with pytest.raises(CoordRefused) as exc:
        rig.cp.step("draft-cards", {"thread_id": rig.tid, "store_id": sid(rig.cp), "extra": 1}, device=DEV)
    assert exc.value.status == 400
    with pytest.raises(CoordRefused) as exc:
        rig.cp.step("draft-cards", {"thread_id": rig.tid, "store_id": sid(rig.cp)}, device=None)
    assert exc.value.status == 403


# --- the journey ------------------------------------------------------------------------------------------------------

def test_a_lapsed_head_is_extended_and_signed_end_to_end(rig):
    d = rig.extend()
    card = rig.card()
    assert card["kind"] == "ENVELOPE" and card["actions"] == ["sign", "withdraw"]
    r = rig.review(d)
    assert r["against"]["base_kind"] == "effective" and r["view"] and r["body_text"]
    c, req = rig.sign(r)
    assert env.parse_body(req["body_text"])["draft"]["draft_id"] == d["draft_id"]
    got = rig.accept(req)
    assert got["created"] is True and got["envelope"]["envelope_id"] != rig.head
    assert got["envelope"]["signer_class"] == "display-phone"
    assert got["cards"] == []                                       # a live successor: no card
    head = rig.store.envelope(got["envelope"]["envelope_id"])
    assert head.body["supersedes"]["envelope_id"] == rig.head


def test_a_killed_head_is_replaced_and_signed_and_the_killed_one_stays_killed(rig):
    assert rig.store.attenuate(rig.head, "kill")
    d = rig.call("draft-rule", thread_id=rig.tid, rule="replace", head=rig.head)["draft"]
    r = rig.review(d)
    assert r["against"]["base_kind"] == "revoked" and r["against"]["wider"] is True
    assert any("grants MORE" in line for line in r["view"])
    _c, req = rig.sign(r)
    got = rig.accept(req)
    assert got["created"] is True
    assert rig.rows("SELECT kind FROM envelope_attenuations") == ["kill"]       # nothing un-killed the old head


def test_withdraw_closes_the_series_and_a_moved_revision_is_refused(rig):
    d = rig.extend()
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"] + 1)
    assert refused(exc) == (409, "draft_stale")
    got = rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"])
    assert got["withdrawn"]["revision"] == d["revision"]
    with pytest.raises(CoordRefused) as exc:
        rig.review(d)
    assert refused(exc) == (409, "draft_stale")


# --- the cockpit carries the same actions behind its own page check -----------------------------------------------------

def test_the_cockpit_sheet_names_the_actions_and_refuses_a_signing_one_off_its_page(world):
    from levain.web_server import CoordSheet, _coord_route

    for a in ("draft-cards", "draft-rule", "draft-withdraw", "draft-review", "draft-challenge", "draft-accept"):
        assert _coord_route("/cockpit/coord/" + a) == a
    sheet = CoordSheet(world["qdir"], "localhost", None, SimpleNamespace(keys=lambda: {}))     # pinned to no page
    with pytest.raises(CoordRefused) as exc:
        sheet.step("draft-review", {"draft_id": "x"})
    assert refused(exc) == (422, "sign_refused")
    with pytest.raises(CoordRefused) as exc:                      # a read signs nothing, so it is not refused for it
        sheet.step("draft-cards", {"thread_id": "x"})
    assert exc.value.status == 400          # past the page check, the phone port's own request check answers
