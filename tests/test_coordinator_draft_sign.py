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

    def sign(self, r: dict, device: str = DEV, passkey=None, **assert_kw):
        c = self.challenge(r, device)
        a = (passkey or self.world["phone"]).assertion(c["challenge"], **assert_kw)
        return c, {"review_id": r["review_id"], "body_text": r["body_text"], "payload": c["payload"], **a}

    def link(self, op: dict) -> None:
        """One authority-log link the laptop key signs (an enrol or a revoke), as the operator's own laptop would."""
        import sqlite3

        from tests._phone_fixtures import _enrol

        root = self.world["root"]
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            _enrol(conn, root, root.parent / "laptop", self.world["laptop_fp"], op)
        finally:
            conn.close()

    def second_passkey(self):
        """Another display-phone passkey enrolled in the log, so the head's key can be revoked while a signer remains."""
        from tests.test_dispatch_phone import SoftPasskey

        p = SoftPasskey()
        self.link({"op": "enrol", "fingerprint": p.fp, "public_key": p.line, "class": "display-phone",
                   "custody": "passkey"})
        return p

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
    # an assertion that is not the passkey's answer to THIS challenge: a well-formed answer to other bytes
    from levain.coordinator.phone import _b64u

    wrong = _b64u(b"\x00" * 32)
    assert wrong != c["challenge"]
    other = rig.world["phone"].assertion(wrong)
    with pytest.raises(CoordRefused) as exc:
        rig.accept({"review_id": r["review_id"], "body_text": r["body_text"], "payload": c["payload"], **other})
    assert exc.value.status == 422
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
    # a definitive refusal spends the review: the same request again is not retried against the store
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert refused(exc) == (409, "review_gone")
    assert rig.envelopes() == before


def test_a_head_key_revoked_between_review_and_sign_is_refused_at_the_challenge(rig):
    # F1: the review showed "against the head as narrowed"; the head's key revoked before Sign makes every value a new
    # grant, so the bytes the challenge would mint are not the ones the sheet described
    p2 = rig.second_passkey()
    d = rig.extend()
    r = rig.review(d, signer_fp=p2.fp)
    assert r["against"]["base_kind"] == "effective"
    rig.link({"op": "revoke_key", "fingerprint": rig.world["phone"].fp})          # the head's signing key
    before = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:
        rig.challenge(r)
    assert refused(exc) == (409, "draft_stale")
    assert "review it again" in exc.value.message
    assert rig.envelopes() == before
    # a fresh review shows the head as it is now, and that one signs
    r2 = rig.review(d, signer_fp=p2.fp)
    assert r2["against"]["base_kind"] == "revoked" and r2["against"]["wider"] is True
    _c, req = rig.sign(r2, passkey=p2)
    assert rig.accept(req)["created"] is True


def test_a_withdraw_that_meets_a_newer_revision_under_the_lock_withdraws_nothing(rig, monkeypatch):
    # F2: the revision lands between the page's read and the withdrawal's write; the write must refuse, not withdraw N+1
    from levain.coordinator import envelope_drafts as drafts

    d = rig.extend()
    real = drafts.withdraw

    def landing_first(store, draft_id, **kw):
        moved = rig.call("draft-rule", thread_id=rig.tid, rule="renew", head=rig.head)["draft"]
        assert moved["revision"] == d["revision"] + 1
        return real(store, draft_id, **kw)

    monkeypatch.setattr(drafts, "withdraw", landing_first)
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"])
    assert refused(exc) == (409, "draft_stale")
    assert rig.rows("SELECT revision FROM envelope_draft_withdrawals") == []


def test_a_withdraw_revision_must_be_an_integer(rig):
    # F5: True and 1.0 compare equal to 1 and must not stand for it
    d = rig.extend()
    for bad in (True, 1.0, "1"):
        with pytest.raises(CoordRefused) as exc:
            rig.call("draft-withdraw", draft_id=d["draft_id"], revision=bad)
        assert refused(exc) == (400, "bad_request"), bad
    assert rig.rows("SELECT revision FROM envelope_draft_withdrawals") == []


def test_withdraw_of_a_signed_or_withdrawn_series_says_so_in_words(rig):
    # F9: the table's trigger refuses it; the operator reads what happened, not the trigger's text
    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    assert rig.accept(req)["created"] is True
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"])
    assert refused(exc) == (409, "draft_stale")
    assert "already signed" in exc.value.message and "unsigned draft" not in exc.value.message
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-withdraw", draft_id="dr-" + "0" * 32, revision=1)
    assert refused(exc) == (409, "draft_stale")


def test_a_second_withdraw_tap_says_the_series_is_withdrawn(rig):
    d = rig.extend()
    assert rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"])["withdrawn"]
    with pytest.raises(CoordRefused) as exc:
        rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"])
    assert refused(exc) == (409, "draft_stale")
    assert "already withdrawn" in exc.value.message and "never replaced" not in exc.value.message


def test_a_cards_fault_after_the_envelope_is_stored_answers_the_envelope(rig, monkeypatch):
    # F3: the acceptance committed; a verifier that cannot run for the cards that follow must not read as unsigned
    from levain.authority import VerifierUnavailable
    from levain.coordinator import draft_sign

    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    before = rig.envelopes()

    def down(*_a, **_k):
        raise VerifierUnavailable("ssh-keygen is not answering")

    monkeypatch.setattr(draft_sign, "_cards", down)
    got = rig.accept(req)
    assert got["created"] is True and got["cards"] is None and "read the cards again" in got["cards_note"]
    assert got["envelope"]["envelope_id"] in rig.envelopes() and len(rig.envelopes()) == len(before) + 1
    monkeypatch.undo()
    with pytest.raises(CoordRefused) as exc:               # the review was spent by the acceptance
        rig.accept(req)
    assert refused(exc) == (409, "review_gone")


def _store_id_down_after(monkeypatch, cls, name: str) -> None:
    """`store_id()` raises sqlite3.Error once `cls.name` has returned (its write committed); reads before it answer."""
    import sqlite3

    real, real_id = getattr(cls, name), EnvelopeStore.store_id
    committed: dict = {}

    def write(*a, **k):
        out = real(*a, **k)
        committed["yes"] = True
        return out

    def store_id(self):
        if committed:
            raise sqlite3.OperationalError("database is locked")
        return real_id(self)

    monkeypatch.setattr(cls, name, write)
    monkeypatch.setattr(EnvelopeStore, "store_id", store_id)


def test_a_store_id_fault_after_the_envelope_is_stored_answers_the_envelope(rig, monkeypatch):
    # G1: the store identity read after the commit is a store read that can fault; the reply must not say unsigned
    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    before = rig.envelopes()
    want_sid = sid(rig.cp)
    _store_id_down_after(monkeypatch, EnvelopeStore, "accept_envelope")
    got = rig.accept(req)
    assert got["created"] is True and got["store_id"] == want_sid
    assert got["cards"] is not None or "read the cards again" in got["cards_note"]
    assert got["envelope"]["envelope_id"] in rig.envelopes() and len(rig.envelopes()) == len(before) + 1
    monkeypatch.undo()
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert refused(exc) == (409, "review_gone")


def test_a_stored_body_with_an_odd_binds_shape_answers_the_envelope(rig, monkeypatch):
    # G1: the thread id is read from the stored body after the commit; a shape the reader does not expect is a fault
    # of the reply, never of the signature
    from dataclasses import replace

    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    before = rig.envelopes()
    real = EnvelopeStore.accept_envelope

    def odd(self, *a, **k):
        stored, created = real(self, *a, **k)
        try:
            return replace(stored, body={**stored.body, "binds": []}), created
        except TypeError:          # not a dataclass: a namespace carrying the same fields
            return SimpleNamespace(**{**vars(stored), "body": {**stored.body, "binds": []}}), created

    monkeypatch.setattr(EnvelopeStore, "accept_envelope", odd)
    got = rig.accept(req)
    assert got["created"] is True and got["cards"] is None and "read the cards again" in got["cards_note"]
    assert got["store_id"] == sid(rig.cp)
    assert len(rig.envelopes()) == len(before) + 1


def test_a_store_id_fault_after_a_rule_answers_the_draft(rig, monkeypatch):
    # G1 (the rule half, same class): the draft row committed; the reply names it
    from levain.coordinator import envelope_drafts as drafts

    rig.lapse()
    assert rig.card()["kind"] == "LAPSED"
    want_sid = sid(rig.cp)
    _store_id_down_after(monkeypatch, drafts, "renew_rule")
    got = rig.call("draft-rule", thread_id=rig.tid, rule="renew", head=rig.head, store_id=want_sid)
    assert got["draft"]["revision"] >= 1 and got["store_id"] == want_sid
    assert got["cards"] is not None or "read the cards again" in got["cards_note"]


def test_a_fault_after_a_withdrawal_answers_the_withdrawal(rig, monkeypatch):
    # G2: the withdrawal committed; reading the series back for the cards' thread id, or the store id, may fault
    import sqlite3

    from levain.coordinator import envelope_drafts as drafts

    d = rig.extend()
    want_sid = sid(rig.cp)

    def latest_down(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(drafts, "latest", latest_down)
    got = rig.call("draft-withdraw", draft_id=d["draft_id"], revision=d["revision"], store_id=want_sid)
    assert got["withdrawn"]["revision"] == d["revision"]
    assert got["cards"] is None and "read the cards again" in got["cards_note"] and got["store_id"] == want_sid
    assert rig.rows("SELECT revision FROM envelope_draft_withdrawals") == [d["revision"]]
    monkeypatch.undo()
    d2 = rig.call("draft-rule", thread_id=rig.tid, rule="renew", head=rig.head)["draft"]
    _store_id_down_after(monkeypatch, drafts, "withdraw")
    got = rig.call("draft-withdraw", draft_id=d2["draft_id"], revision=d2["revision"], store_id=want_sid)
    assert got["withdrawn"]["revision"] == d2["revision"] and got["store_id"] == want_sid
    assert got["cards"] is not None or "read the cards again" in got["cards_note"]


def test_a_head_kind_the_sheet_cannot_describe_keeps_no_review(rig, monkeypatch):
    # G3: the builder answering a base_kind the sheet has no words for is refused in words, with no review to sign
    from dataclasses import replace

    from levain.coordinator import envelope_drafts as drafts

    d = rig.extend()
    real = drafts.draft_sign_bytes

    def drifted(*a, **k):
        body_text, payload, since, against = real(*a, **k)
        return body_text, payload, since, replace(against, base_kind="partial")

    monkeypatch.setattr(drafts, "draft_sign_bytes", drifted)
    with pytest.raises(CoordRefused) as exc:
        rig.review(d)
    assert refused(exc) == (422, "refused") and "cannot describe" in exc.value.message
    assert rig.cp._draft_reviews == {}


def test_a_body_that_differs_from_the_review_mints_no_challenge(rig, monkeypatch):
    # G4: the challenge signs only the body the review showed, compared as well as the diffs
    from levain.coordinator import envelope_drafts as drafts

    d = rig.extend()
    r = rig.review(d)
    real = drafts.draft_sign_bytes

    def other_body(*a, **k):
        body_text, payload, since, against = real(*a, **k)
        return body_text + " ", payload, since, against

    monkeypatch.setattr(drafts, "draft_sign_bytes", other_body)
    with pytest.raises(CoordRefused) as exc:
        rig.challenge(r)
    assert refused(exc) == (409, "draft_stale")
    assert rig.cp._draft_reviews[r["review_id"]]["minted"] is None


def test_a_machine_fault_at_accept_keeps_the_review_for_the_retry(rig, monkeypatch):
    # F9: the spend/keep split: a verifier that cannot run is not a verdict, so the same request may be sent again
    from levain.authority import VerifierUnavailable

    d = rig.extend()
    r = rig.review(d)
    _c, req = rig.sign(r)
    before = rig.envelopes()
    real = EnvelopeStore.accept_envelope

    def down(self, *a, **k):
        raise VerifierUnavailable("ssh-keygen is not answering")

    monkeypatch.setattr(EnvelopeStore, "accept_envelope", down)
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req)
    assert exc.value.status == 503
    assert rig.envelopes() == before
    monkeypatch.setattr(EnvelopeStore, "accept_envelope", real)
    assert rig.accept(req)["created"] is True


def test_two_signatures_over_one_review_store_one_envelope(rig, monkeypatch):
    # F7: two accepts on one review that both pass the review check before either spends it (a second tab, a double
    # tap): two DIFFERENT passkey signatures over the one payload. The store keeps one; the other is refused.
    d = rig.extend()
    r = rig.review(d)
    c, req1 = rig.sign(r)
    req2 = {**req1, **rig.world["phone"].assertion(c["challenge"])}
    assert req2["signature"] != req1["signature"]
    before = rig.envelopes()
    real = EnvelopeStore.accept_envelope
    second: dict = {}

    def racing(self, *a, **k):
        out = real(self, *a, **k)
        if not second:
            second["seen"] = True
            try:
                second["got"] = rig.accept(req2)
            except CoordRefused as exc:
                second["refused"] = (exc.status, exc.error)
        return out

    monkeypatch.setattr(EnvelopeStore, "accept_envelope", racing)
    got = rig.accept(req1)
    assert got["created"] is True
    assert "got" not in second and second["refused"][0] == 409, second
    assert len(rig.envelopes()) == len(before) + 1


def test_a_review_expires_with_the_ttl(rig):
    from levain.coordinator.phone import REVIEW_TTL

    d = rig.extend()
    r = rig.review(d)
    rig.clock.t += REVIEW_TTL
    with pytest.raises(CoordRefused) as exc:
        rig.challenge(r)
    assert refused(exc) == (409, "review_gone")


def test_a_double_sign_tap_mints_a_second_challenge_and_only_the_latest_signs(rig):
    d = rig.extend()
    r = rig.review(d)
    rig.clock.t += timedelta(seconds=2)
    c1, req1 = rig.sign(r)
    rig.clock.t += timedelta(seconds=2)             # a second tap: signed_at moves, so the payload is another one
    c2, req2 = rig.sign(r)
    assert c1["payload"] != c2["payload"]
    before = rig.envelopes()
    with pytest.raises(CoordRefused) as exc:
        rig.accept(req1)
    assert refused(exc) == (422, "refused")
    assert rig.envelopes() == before
    assert rig.accept(req2)["created"] is True


def test_the_oldest_review_is_dropped_past_reviews_kept(rig, monkeypatch):
    from levain.coordinator import phone as phone_mod

    monkeypatch.setattr(phone_mod, "REVIEWS_KEPT", 2)
    d = rig.extend()
    first, second, third = rig.review(d), rig.review(d), rig.review(d)
    with pytest.raises(CoordRefused) as exc:
        rig.challenge(first)
    assert refused(exc) == (409, "review_gone")
    assert rig.challenge(second)["payload"] and rig.challenge(third)["payload"]


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
        sheet.step("draft-review", {"draft_id": "x"}, device="cockpit")
    assert refused(exc) == (422, "sign_refused")
    with pytest.raises(CoordRefused) as exc:                      # a read signs nothing, so it is not refused for it
        sheet.step("draft-cards", {"thread_id": "x"}, device="cockpit")
    assert exc.value.status == 400          # past the page check, the phone port's own request check answers
