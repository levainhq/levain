"""TERMSIGN L5b: a ruling ALLOW carried inside a verified ``levain-desk-answer/1``.

Two layers. The queue's accept branch (:meth:`ConsentQueue.answer_terminal_ruling`) judges a desk-carried ruling by the
desk parser and in the desk SSHSIG namespace, with the refusals of the terminal path; those cases use the shared real
keys of ``tests/test_consent_queue.py`` and real ``ssh-keygen -Y sign``. Then the desk click and the typed line apply
that ruling after signing and record the answer only if it applied. No test reaches a Secure Enclave key, ``sc_auth``,
``osascript`` or a Touch ID sheet: the live check is a fake and both real signer factories raise if reached.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import timedelta

import pytest

from levain import band_view, desk_click
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import AnswerRefused, ConsentQueue
from levain.consent import band as consent_band
from levain.consent import terminal_sign as ts
from levain.consent.questions import Link, QuestionStore
from levain.consent.record import ruling_message
from tests._h4 import h4_home
from tests.test_consent_queue import NOW, Key, Log
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests.test_consent_queue_v4 import undecided

SCOPE = {"store": "levain-questions:s1", "install": "s1", "surface": "web", "origin": "http://localhost:8765",
         "view": "item"}
ITEM, VERSION = "q-1", "v1"


def fake_ctk(_public_key):
    return None, "test"


def carried(q, request_id, key, *, ruling_edit=None, kind="authority", at=None, sign_key=None, scope=None,
            payload_edit=None, item=ITEM, version=VERSION, namespace=ts.DESK_NAMESPACE, extra=()):
    """The desk message and signature a click would produce: one authority entry carrying the queue's own ruling."""
    payload = q.ruling_payload(request_id, "allow", key.fp)
    payload.update(payload_edit or {})
    text = ruling_message(payload).decode("utf-8")
    if ruling_edit is not None:
        text = ruling_edit(text)
    sc = scope or SCOPE
    entry = ts.Entry(item, version, "push it?", kind, answer="(a) push", ruling=text if kind == "authority" else None)
    entries = [entry, *extra]
    if extra:
        message = ts.build_desk_message(store=sc["store"], install=sc["install"], surface=sc["surface"],
                                        origin=sc["origin"], view="7", gesture="typed", typed="1a", entries=entries,
                                        at=at or NOW)
    else:
        message = ts.build_desk_message(store=sc["store"], install=sc["install"], surface=sc["surface"],
                                        origin=sc["origin"], view=sc["view"], gesture="click", entries=entries,
                                        at=at or NOW)
    return message, (sign_key or key).sign(message, namespace)


def apply(q, request_id, message, sig, **kw):
    kw.setdefault("item", ITEM)
    kw.setdefault("row_version", VERSION)
    kw.setdefault("scope", SCOPE)
    kw.setdefault("ctk", fake_ctk)
    return q.answer_terminal_ruling(request_id, message, sig, **kw)


def refused(q, request_id, message, sig, **kw):
    with pytest.raises(AnswerRefused) as got:
        apply(q, request_id, message, sig, **kw)
    assert q.get(request_id)[1] == "undecided"           # a refusal moves nothing
    return str(got.value)


# --- the queue's accept branch, desk format ---------------------------------------------------------------------

def test_a_valid_desk_carried_ruling_is_applied_once_and_delivers_as_any_ruling(queue, keys):
    request, gap = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    ruled = apply(queue, request.id, message, sig)
    assert (ruled.ruling, ruled.signer_fp, ruled.gap) == ("allow", keys["laptop"].fp, gap)
    assert queue.get(request.id)[1] == "allowed"
    taken = queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd="/work/proj")
    assert taken is not None and taken.signer_fp == keys["laptop"].fp
    with pytest.raises(AnswerRefused, match="not undecided"):             # a second apply is refused
        apply(queue, request.id, message, sig)
    assert [r["state"] for r in queue.receipts(request.id)] == ["undecided", "allowed"]


def test_receipts_verify_reads_a_desk_carried_ruling_as_the_take_reads_it(queue, keys, authority):
    from levain.consent.receipt_log import verify

    request, _ = undecided(queue)
    apply(queue, request.id, *carried(queue, request.id, keys["laptop"]))
    report = verify(queue.directory, authority_for=lambda r: authority)
    assert report.mismatches == () and report.ok
    assert report.signed == 1 and report.last_signed.request_id == request.id


def test_a_swapped_kept_desk_message_fails_the_row_closed(queue, keys, authority):
    from levain.consent.receipt_log import verify

    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    other, _ = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(seconds=9))
    apply(queue, request.id, message, sig)
    conn = sqlite3.connect(queue.path)
    try:
        conn.execute("DROP TRIGGER terminal_rulings_frozen")
        conn.execute("UPDATE terminal_rulings SET message = ?", (other.decode("utf-8"),))
        conn.commit()
    finally:
        conn.close()
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                  cwd="/work/proj") is None
    assert [rid for rid, _why in verify(queue.directory, authority_for=lambda r: authority).mismatches] == [request.id]


def test_a_forged_entry_signed_by_a_stranger_or_over_other_bytes_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], sign_key=keys["stranger"])
    assert "does not verify" in refused(queue, request.id, message, sig)
    message, _sig = carried(queue, request.id, keys["laptop"])
    _other, other_sig = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(seconds=5))
    assert "does not verify" in refused(queue, request.id, message, other_sig)


def test_a_signature_in_the_other_namespace_does_not_carry_a_desk_message(queue, keys):
    """The two formats are told apart by namespace: a terminal-namespace signature over desk bytes is no desk answer,
    and a desk-namespace signature over terminal bytes is no terminal answer."""
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], namespace=ts.NAMESPACE)
    assert "does not verify" in refused(queue, request.id, message, sig)
    payload = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)
    entry = ts.Entry(ITEM, VERSION, "push it?", "authority", answer="(a) push",
                     ruling=ruling_message(payload).decode("utf-8"))
    tmessage = ts.build_message(store="levain-questions:s1", install="s1", session="sess-1", view="7", typed="1a",
                                entries=[entry], at=NOW)
    scope = {"store": "levain-questions:s1", "install": "s1", "session": "sess-1", "view": "7"}
    wrong = keys["laptop"].sign(tmessage, ts.DESK_NAMESPACE)
    assert "does not verify" in refused(queue, request.id, tmessage, wrong, scope=scope)


def test_a_swapped_request_digest_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], payload_edit={"request_digest": "0" * 64})
    assert refused(queue, request.id, message, sig) == "the ruling signs another version of this request"


def test_a_desk_ruling_replayed_to_another_gap_or_queue_is_refused(queue, keys):
    request, gap = undecided(queue)
    other, _ = undecided(queue, "touch /tmp/y")
    message, sig = carried(queue, request.id, keys["laptop"])
    assert refused(queue, other.id, message, sig) == "the ruling is for another request"
    wrong = carried(queue, request.id, keys["laptop"], payload_edit={"gap": gap + 5})
    assert "gap" in refused(queue, request.id, *wrong)
    elsewhere = carried(queue, request.id, keys["laptop"], payload_edit={"queue_id": "cq-elsewhere"})
    assert refused(queue, request.id, *elsewhere) == "the ruling names another queue"


def test_a_display_phone_a_revoked_and_a_stranger_key_cannot_carry_a_desk_ruling(queue, keys):
    request, _ = undecided(queue)
    assert "display-phone" in refused(queue, request.id, *carried(queue, request.id, keys["phone"]))
    assert refused(queue, request.id, *carried(queue, request.id, keys["revoked"])) == "the signer's key is revoked"
    assert refused(queue, request.id, *carried(queue, request.id, keys["stranger"])) == \
        "the signer is not an enrolled key"


def test_a_stale_receipts_head_is_refused(queue, keys):
    request, _ = undecided(queue)
    head = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)["receipts_head"]
    message, sig = carried(queue, request.id, keys["laptop"],
                           payload_edit={"receipts_head": {"size": head["size"], "root": "ab" * 32}})
    assert "receipts" in refused(queue, request.id, message, sig)


def test_a_desk_ruling_that_differs_by_one_byte_is_refused(queue, keys):
    request, _ = undecided(queue)
    for edit in (lambda t: t.replace('"allow"', '"deny"'), lambda t: t + " ", lambda t: t.replace('"gap":', '"gap" :')):
        message, sig = carried(queue, request.id, keys["laptop"], ruling_edit=edit)
        with pytest.raises(AnswerRefused):
            apply(queue, request.id, message, sig)
        assert queue.get(request.id)[1] == "undecided"


def test_a_desk_deny_is_not_carried(queue, keys):
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "deny", keys["laptop"].fp)
    entry = ts.Entry(ITEM, VERSION, "push it?", "authority", answer="(a) push", ruling=ruling_message(payload).decode())
    message = ts.build_desk_message(**SCOPE_KW, gesture="click", entries=[entry], at=NOW)
    assert "ALLOW only" in refused(queue, request.id, message, keys["laptop"].sign(message, ts.DESK_NAMESPACE))


SCOPE_KW = {k: SCOPE[k] for k in ("store", "install", "surface", "origin", "view")}


def test_an_information_or_unsigned_entry_cannot_carry_a_ruling(queue, keys):
    request, _ = undecided(queue)
    with pytest.raises(ts.MessageError):                                  # the format refuses it at build
        ts.build_desk_message(**SCOPE_KW, gesture="click", at=NOW, entries=[
            ts.Entry(ITEM, VERSION, "q", "information", answer="(a) x", ruling="{}")])
    message, sig = carried(queue, request.id, keys["laptop"], kind="information")   # and the queue finds none
    assert "no ruling for this question" in refused(queue, request.id, message, sig)
    for bad in ("", "not a signature", None):                              # an unsigned message carries nothing
        with pytest.raises(AnswerRefused):
            apply(queue, request.id, message, bad)
    assert queue.get(request.id)[1] == "undecided"


def test_a_desk_ruling_for_another_item_version_or_scope_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    assert "no ruling for this question" in refused(queue, request.id, message, sig, item="q-2")
    assert "no ruling for this question" in refused(queue, request.id, message, sig, row_version="v2")
    for name in SCOPE:
        assert name in refused(queue, request.id, message, sig, scope={**SCOPE, name: "other"})
    terminal_scope = {"store": SCOPE["store"], "install": SCOPE["install"], "session": "sess-1", "view": "item"}
    assert "surface" in refused(queue, request.id, message, sig, scope=terminal_scope)


def test_a_message_signed_long_ago_or_by_a_failing_enclave_is_refused(queue, keys, clock):
    request, _ = undecided(queue)
    old, sig = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(minutes=30))
    assert "ten minutes" in refused(queue, request.id, old, sig)
    message, sig = carried(queue, request.id, keys["laptop"])
    assert "biometric" in refused(queue, request.id, message, sig, ctk=lambda pk: (False, "no such identity"))


def test_a_desk_typed_message_with_a_second_entry_judges_each_by_its_class(queue, keys):
    request, _ = undecided(queue)
    info = ts.Entry("q-9", "v9", "ok?", "information", answer="(a) fine")
    message, sig = carried(queue, request.id, keys["laptop"], extra=[info])
    scope = {**SCOPE, "view": "7"}
    assert apply(queue, request.id, message, sig, scope=scope).ruling == "allow"


def test_a_desk_countersign_carries_no_ruling(queue, keys):
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)
    entry = ts.Entry(ITEM, VERSION, "push it?", "authority", answer="(a) push", ruling=ruling_message(payload).decode())
    message = ts.build_desk_message(**SCOPE_KW, gesture="click", entries=[entry], at=NOW,
                                    countersign_of="2026-11-03T20:00:00Z", countersign="confirm")
    assert "countersign" in refused(queue, request.id, message, keys["laptop"].sign(message, ts.DESK_NAMESPACE))


# --- the desk click and the typed line ----------------------------------------------------------------------------

OPTS = [("push", "push it"), ("hold", "hold off")]
ORIGIN = "http://localhost:8765"


@pytest.fixture(autouse=True)
def _no_live_check(monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "ctk_status", lambda public_key: (None, "test"))
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "keys")
    monkeypatch.setattr(consent_band, "deliver_answer", lambda store, ref, **kw: None)

    def boom(path, timeout=None):
        raise AssertionError("a test reached a real signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)
    monkeypatch.setattr(ts, "default_desk_signer_factory", boom)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))


def real_desk(path, timeout=None):
    return ckeys.ssh_signer(path, namespace=ts.DESK_NAMESPACE, timeout=timeout)


@pytest.fixture
def world(tmp_path):
    kd = tmp_path / "keys"
    kd.mkdir(mode=0o700)
    path = kd / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True)
    (kd / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    key = Key.__new__(Key)
    key.path = path
    key.public_key = " ".join((kd / (ts.KEY_LABEL + ".pub")).read_text().split()[:2])
    key.fp = ckeys.fingerprint_of(key.public_key)
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": key})
    log.enrol("laptop", "laptop-presence")
    store = QuestionStore(tmp_path / "lh" / "consent")
    queue = ConsentQueue(tmp_path / "queue", authority=log.source, home=h4_home())
    request, _gap = undecided(queue)
    q = store.file(agent="claude_code", agent_session_id="s1", what="Push the branch?", options=OPTS, rec="push",
                   blocks="the sweep", cwd="/w",
                   link=Link(option="push", kind="push", request_id=request.id, queue_id=queue.queue_id))
    return {"store": store, "queue": queue, "request": request, "q": q, "key": key, "keydir": kd}


def state_of(world):
    return world["queue"].get(world["request"].id)[1]


def answers(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT question_id, value FROM answers").fetchall()


def click(world, *, pick="a", factory=None, host=None, txn="txn-0000001", policy="authority", notes=None):
    store, q = world["store"], world["q"]
    got = store.get(q.id)
    b = {"subject": {"id": q.id, "version": band_view.subject_version(got)}, "pick": pick, "client_txn": txn}
    return desk_click.desk_answer(
        desk_click.DeskRequest(True, "web", ORIGIN, b),
        host or desk_click.LevainDeskHost(store, queue_for=lambda _q: world["queue"]), desk_click.DeskState(),
        policy=policy, signer_factory=factory or real_desk, key_dir=world["keydir"],
        notify=(notes if notes is not None else []).append)


def test_a_desk_click_on_the_linked_pick_signs_the_ruling_applies_it_then_records_the_answer(world):
    notes: list[str] = []
    body, status = click(world, notes=notes)
    assert status == 200 and body["signed"] and body["ruled"] is True and body["channel"] == "desk"
    assert state_of(world) == "allowed"
    assert [v for _, v in answers(world["store"])] == ["push"]
    rec = band_view.answer_sign_of(world["store"], world["q"].id)
    parsed = ts.parse_desk_verified(rec["message"].encode())
    (entry,) = parsed.entries
    assert entry.kind == "authority" and json.loads(entry.ruling)["request_id"] == world["request"].id
    assert notes                                                           # the Touch ID notice was posted first
    assert band_view.answer_badge(world["store"], world["q"].id, key_dir=world["keydir"],
                                  origins=("http://localhost:8765",),
                                  ctk=lambda pk: (None, "test")) == "signed: Touch ID, laptop (desk)"
    taken = world["queue"].take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                            cwd="/work/proj")
    assert taken is not None and taken.signer_fp == world["key"].fp


def test_the_unlinked_pick_of_a_linked_question_still_refuses_with_no_prompt(world):
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    body, status = click(world, pick="b", factory=factory)
    assert status == 422 and body["error"] == "desk_authority_pending" and asked == []
    assert state_of(world) == "undecided" and answers(world["store"]) == []


def test_a_queue_that_would_not_build_the_ruling_raises_no_sheet(world):
    class Elsewhere:
        queue_id = "cq-elsewhere"

    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    host = desk_click.LevainDeskHost(world["store"], queue_for=lambda _q: Elsewhere())
    body, status = click(world, factory=factory, host=host)
    assert status == 422 and body["error"] == "desk_authority_pending" and asked == []
    assert answers(world["store"]) == [] and state_of(world) == "undecided"


def test_a_queue_that_refuses_the_signed_ruling_records_no_answer_and_says_so(world):
    class Refusing:
        queue_id = world["queue"].queue_id

        def ruling_payload(self, *a):
            return world["queue"].ruling_payload(*a)

        def answer_terminal_ruling(self, *a, **kw):
            raise AnswerRefused("the signer's key is revoked")

    host = desk_click.LevainDeskHost(world["store"], queue_for=lambda _q: Refusing())
    body, status = click(world, host=host)
    assert status == 409 and body["error"] == "ruling_refused" and body["signed"] is True and body["ruled"] is False
    assert "the signer's key is revoked" in body["message"]
    assert answers(world["store"]) == [] and state_of(world) == "undecided"
    assert world["store"].get(world["q"].id).answer is None


def test_a_cancelled_touch_applies_no_ruling_and_records_nothing(world):
    from levain.cockpit.keys import KeyError_

    def factory(path, timeout=None):
        def signer(message):
            raise KeyError_("Touch ID was cancelled or did not complete")
        return "SHA256:" + "A" * 43, signer

    body, status = click(world, factory=factory)
    assert status == 409 and body["error"] == "unsigned"
    assert state_of(world) == "undecided" and answers(world["store"]) == []


def test_the_ruling_is_applied_before_the_answer_and_an_answer_refused_after_it_is_said_so(world):
    """Ruling first (the phone's order): a row that moves between the touch and the write leaves the ruling applied
    and the answer unrecorded, and the reply says both."""
    order: list[str] = []
    queue = world["queue"]
    real = queue.answer_terminal_ruling

    class Spy:
        queue_id = queue.queue_id
        ruling_payload = staticmethod(queue.ruling_payload)

        def answer_terminal_ruling(self, *a, **kw):
            order.append("ruling")
            out = real(*a, **kw)
            world["store"].answer_with_context(
                [{"ref": world["q"].id, "via": "cockpit", "viewer": "x", "seen_through": None, "note": None,
                  "option": "hold"}], channel="cockpit", schema=band_view.BAND_SCHEMA)
            order.append("raced")
            return out

    host = desk_click.LevainDeskHost(world["store"], queue_for=lambda _q: Spy())
    body, status = click(world, host=host)
    assert order == ["ruling", "raced"]
    assert status == 409 and body["error"] == "ruled_not_recorded" and body["ruled"] is True
    assert state_of(world) == "allowed"
    assert [v for _, v in answers(world["store"])] == ["hold"]            # the racing answer, never the click's


def test_a_second_click_after_the_ruling_is_stale_and_rules_nothing_again(world):
    first, status = click(world)
    assert status == 200
    again, status = click(world, txn="txn-0000002")
    assert status == 409 and again["error"] == "stale"
    assert len(world["queue"].receipts(world["request"].id)) == 2


def test_a_typed_line_in_the_band_box_signs_and_applies_the_linked_pick_under_the_default_policy(world):
    store = world["store"]
    b = band_view.band_payload(store, "cockpit:web")
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    done = band_view.answer_text_desk(store, "1a", viewer="cockpit:web", store_id=b["store_id"], view_id=b["view_id"],
                                      surface="web", origin=ORIGIN, signer_factory=factory, key_dir=world["keydir"],
                                      notify=lambda t: None, policy="authority",
                                      queue_for=lambda _q: world["queue"])
    assert asked == [1] and done.recorded == ["1a"]
    assert state_of(world) == "allowed" and [v for _, v in answers(store)] == ["push"]
    rec = band_view.answer_sign_of(store, world["q"].id)
    parsed = ts.parse_desk_verified(rec["message"].encode())
    assert parsed.gesture == "typed" and parsed.entries[0].kind == "authority"
    assert "ruling ALLOW signed in the same touch and applied: 1a" in done.echo


def test_a_typed_line_whose_queue_refuses_keeps_the_sheet_and_records_nothing(world):
    store = world["store"]
    b = band_view.band_payload(store, "cockpit:web")

    class Refusing:
        queue_id = world["queue"].queue_id
        ruling_payload = staticmethod(world["queue"].ruling_payload)

        def answer_terminal_ruling(self, *a, **kw):
            raise AnswerRefused("no ruling is authority")

    done = band_view.answer_text_desk(store, "1a", viewer="cockpit:web", store_id=b["store_id"], view_id=b["view_id"],
                                      surface="web", origin=ORIGIN, signer_factory=real_desk, key_dir=world["keydir"],
                                      notify=lambda t: None, policy="authority", queue_for=lambda _q: Refusing())
    assert done.recorded == [] and answers(store) == [] and state_of(world) == "undecided"
    assert "no signed ruling was applied: no ruling is authority" in done.echo


# --- L5b r1 R1: the band-box ROUTE reaches the ruling path under the default policy ------------------------------

def _route_line(world, text, *, txn="txn-typed-01", factory=None, policy="authority"):
    store = world["store"]
    b = band_view.band_payload(store, "cockpit:web")
    body = {"text": text, "store_id": b["store_id"], "view_id": b["view_id"], "client_txn": txn}
    return desk_click.desk_text(desk_click.DeskRequest(True, "web", ORIGIN, body), store, desk_click.DeskState(),
                                policy=policy, signer_factory=factory or real_desk, key_dir=world["keydir"],
                                notify=lambda t: None)


@pytest.fixture
def own_queue(world, monkeypatch):
    """The install's own queue IS the world's (the route takes no queue from its caller)."""
    monkeypatch.setattr(band_view, "_default_queue_for", lambda environ: (lambda _q: world["queue"]))
    return world["queue"]


def test_the_route_signs_a_typed_linked_pick_under_the_default_policy(world, own_queue):
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    body, status = _route_line(world, "1a", factory=factory)
    assert status == 200 and body["signed"] is True and body["recorded"] == ["1a"], body
    assert asked == [1] and state_of(world) == "allowed"
    assert [v for _, v in answers(world["store"])] == ["push"]
    parsed = ts.parse_desk_verified(band_view.answer_sign_of(world["store"], world["q"].id)["message"].encode())
    assert parsed.gesture == "typed" and parsed.entries[0].kind == "authority"


def test_the_route_still_refuses_a_line_with_nothing_to_sign_and_asks_for_nothing(world, own_queue):
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    body, status = _route_line(world, "1b", factory=factory)              # the unlinked pick of the linked row
    assert (status, body["error"]) == (422, "desk_unsigned") and asked == []
    assert state_of(world) == "undecided" and answers(world["store"]) == []
    again, status = _route_line(world, "1b", factory=factory)             # the request id was left free
    assert (status, again["error"]) == (422, "desk_unsigned")


def test_answer_text_desk_for_the_route_never_writes_a_line_unsigned(world):
    store = world["store"]
    b = band_view.band_payload(store, "cockpit:web")
    with pytest.raises(band_view.DeskLineUnsigned):
        band_view.answer_text_desk(store, "1b", viewer="cockpit:web", store_id=b["store_id"], view_id=b["view_id"],
                                   surface="web", origin=ORIGIN, signer_factory=real_desk, key_dir=world["keydir"],
                                   notify=lambda t: None, policy="authority", unsigned=False,
                                   queue_for=lambda _q: world["queue"])
    assert answers(store) == [] and state_of(world) == "undecided"


# --- L5b r2: a ruling applied whose answer did not record is a KNOWN EDGE, never closed from the desk -------------

def _apply_then_fail_the_record(world, monkeypatch):
    """Click the linked pick; the queue applies the ruling, then the answer's write fails (store busy)."""
    real = band_view.answer_item_desk
    calls = {"n": 0}

    def busy_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)
    monkeypatch.setattr(band_view, "answer_item_desk", busy_once)
    body, status = click(world)
    assert (status, body["error"], body["ruled"]) == (409, "ruled_not_recorded", True), body
    assert state_of(world) == "allowed" and answers(world["store"]) == []
    return len(world["queue"].receipts(world["request"].id))


def test_after_ruled_not_recorded_a_desk_retry_is_refused_by_click_and_typed_line(world, monkeypatch, own_queue):
    """The honest known edge (as the terminal path): once the queue applied the ruling and the answer failed to
    record, the item stays open and the desk cannot close it. A click retry is refused ``desk_authority_pending``
    before any sheet; a typed retry builds no ruling entry, so nothing on the line is signed and it records nothing
    (the band's ``signature needed, nothing recorded`` echo, as for any linked pick whose ruling cannot be built).
    Neither raises a sheet or writes to the queue, and the request stays allowed once. Pinned so nobody "fixes" the
    dead end by loosening the queue's judge."""
    receipts = _apply_then_fail_the_record(world, monkeypatch)
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    body, status = click(world, txn="txn-0000002", factory=factory)
    assert (status, body["error"]) == (422, "desk_authority_pending"), body
    typed, status = _route_line(world, "1a", txn="txn-typed-09", factory=factory)
    assert status == 200 and typed["recorded"] == [] and "nothing recorded" in typed["echo"], (status, typed)
    assert asked == []                                                     # no Touch ID sheet on either route
    assert answers(world["store"]) == [] and world["store"].get(world["q"].id).answer is None
    assert len(world["queue"].receipts(world["request"].id)) == receipts  # no second queue write
    assert state_of(world) == "allowed"


def test_a_line_on_a_gone_view_under_the_default_policy_is_stale_band_not_desk_unsigned(world, own_queue):
    """A view the store no longer holds is a stale band (the page re-reads it), never ``desk_unsigned`` (the page
    would resend the line down the ordinary path against a view that is gone). Nothing is asked or recorded."""
    store = world["store"]
    b = band_view.band_payload(store, "cockpit:web")
    asked = []

    def factory(path, timeout=None):
        asked.append(1)
        return real_desk(path, timeout)

    body = {"text": "1a", "store_id": b["store_id"], "view_id": b["view_id"] + 999, "client_txn": "txn-gone-01"}
    got, status = desk_click.desk_text(desk_click.DeskRequest(True, "web", ORIGIN, body), store,
                                       desk_click.DeskState(), policy="authority", signer_factory=factory,
                                       key_dir=world["keydir"], notify=lambda t: None)
    assert (status, got.get("error")) == (409, "stale_band"), (status, got)
    assert asked == [] and answers(store) == [] and state_of(world) == "undecided"
