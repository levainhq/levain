"""TERMSIGN L5a: the desk click route (levain.desk_click.desk_answer) and its write (band_view.answer_item_desk,
answer_text_desk), then the HTTP wiring.

No test reaches a Secure Enclave key, ``sc_auth``, ``osascript`` or a Touch ID sheet: the key is a plain ed25519 key
from ssh-keygen in a tmp dir, signers are fakes or a real ``ssh-keygen -Y sign`` over that key, the notice runner is a
list, and both real signer factories raise if reached.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from levain import band_view, desk_click
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.cockpit.keys import KeyError_
from levain.consent import band as consent_band
from levain.consent import terminal_sign as ts
from levain.consent.questions import Link, QuestionStore
from tests.test_cockpit_k2a import TOKEN, _install, _serve

OPTS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]
ORIGIN = "http://localhost:8765"


@pytest.fixture(autouse=True)
def _guards(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "keys")
    monkeypatch.setattr(consent_band, "deliver_answer", lambda store, ref, **kw: None)

    def boom(path, timeout=None):
        raise AssertionError("a test reached a real signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)
    monkeypatch.setattr(ts, "default_desk_signer_factory", boom)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.setattr(band_view, "_cockpit_origins", lambda: frozenset({ORIGIN}), raising=False)   # their cockpit


@pytest.fixture
def store(tmp_path):
    return QuestionStore(tmp_path / "lh" / "consent")


@pytest.fixture
def keydir(tmp_path):
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    path = d / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True)
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    return d


def real_desk(path, timeout=None):
    return ckeys.ssh_signer(path, namespace=ts.DESK_NAMESPACE, timeout=timeout)


class Calls:
    """A factory that counts how often the key was asked and may do something in the middle of the touch."""

    def __init__(self, during=None, fail=None):
        self.n, self.during, self.fail = 0, during, fail

    def __call__(self, path, timeout=None):
        fp, signer = real_desk(path, timeout)

        def sign(message):
            self.n += 1
            if self.during:
                self.during()
            if self.fail:
                raise self.fail
            return signer(message)
        return fp, sign


def file_q(store, what="Ship the fix?", **kw):
    return store.file(agent="claude_code", agent_session_id="s1", what=what, options=OPTS, rec="hold",
                      blocks="the sweep", cwd="/w", **kw)


def body(q, *, pick="a", txn="txn-0000001", version=None, **extra):
    version = version or band_view.subject_version(store_get(q))
    return {"subject": {"id": q.id, "version": version}, "pick": pick, "client_txn": txn, **extra}


_STORE: dict = {}


def store_get(q):
    return _STORE["s"].get(q.id)


def req(b, *, local=True, surface="web"):
    return desk_click.DeskRequest(local, surface, ORIGIN, b)


@pytest.fixture
def rig(store, keydir):
    _STORE["s"] = store
    notes: list[str] = []
    state = desk_click.DeskState()

    def go(b, *, host=None, factory=None, policy="all", local=True, delivered=None, **kw):
        return desk_click.desk_answer(req(b, local=local), host or desk_click.LevainDeskHost(store), state,
                                      policy=policy, signer_factory=factory, key_dir=keydir, notify=notes.append,
                                      deliver=delivered, **kw)
    go.notes, go.state = notes, state
    return go


def answers(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT question_id, value FROM answers").fetchall()


def context(store, qid):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT via, viewer, sign_state, sig IS NOT NULL, message FROM answer_context "
                         "WHERE question_id = ?", (qid,)).fetchone()


# --- the signed click -------------------------------------------------------------------------------------

def test_a_signed_click_is_recorded_signed_and_classed_signed_desk(rig, store, keydir):
    q = file_q(store)
    calls = Calls()
    got, status = rig(body(q, pick="b"), factory=calls)
    assert status == 200 and got["signed"] and got["channel"] == "desk" and got["recorded"] == ["b"], got
    assert got["badge"] == "signed: Touch ID, laptop (desk)" and calls.n == 1
    assert answers(store) == [(q.id, "hold")]
    via, viewer, state, has_sig, message = context(store, q.id)
    assert (via, viewer, state, has_sig) == ("cockpit", "cockpit:web", "signed", 1)
    # the message is what the SERVER built from the row it read, in the desk format, naming that row and option
    assert message.startswith("levain-desk-answer/1\n") and "gesture: click\n" in message
    assert f"  item: {q.id}\n" in message and "  answer: (b) Hold for the morning\n" in message
    assert "typed" not in message and "via:" not in message and "view: item\n" in message
    # and it classifies signed on the desk channel through the one reader
    path = keydir / ts.KEY_LABEL
    pub = " ".join(Path(str(path) + ".pub").read_text().split()[:2])
    sign = band_view.answer_sign_of(store, q.id)
    ev = {"ts": "2026-11-03T21:00:00Z", "answer": "(b) Hold for the morning", "via": "cockpit",
          "signed": {"format": ts.DESK_FORMAT, "message": sign["message"], "sig": sign["sig"], "entry": 1}}
    msg = ts.parse_desk_verified(sign["message"].encode())
    ev["ts"] = msg.at.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = {"signers": f'phill namespaces="{ts.DESK_NAMESPACE}" {pub}\n', "principal": "phill",
           "ctk": lambda pk: (True, "bio"), **{k: getattr(msg, k) for k in ts.DESK_SCOPE_FIELDS}}
    row = {"id": q.id, "row_version": msg.entries[0].row_version, "what": q.what}
    row_ev = {**ev, "answer": "(b) Hold for the morning"}
    got = ts.answer_class(row_ev, row, ctx)
    assert got.state == "signed-terminal" and got.channel == "desk", got


def test_a_free_text_click_signs_his_words_and_the_notice_names_the_desk(rig, store):
    q = file_q(store)
    b = body(q)
    del b["pick"]
    b["text"] = "hold it, but tell Jess first"
    got, status = rig(b, factory=Calls())
    assert status == 200 and got["recorded"] == ["text"]
    assert rig.notes == ["Desk: Touch ID to sign: " + q.id + " hold it, but tell Jess first"]
    assert "  text: hold it, but tell Jess first\n" in context(store, q.id)[4]


def test_delivery_starts_on_a_thread_after_a_signed_click(rig, store):
    q = file_q(store)
    seen: list = []
    done = threading.Event()
    got, status = rig(body(q), factory=Calls(), delivered=lambda d: (seen.append(d.answered), done.set()))
    assert status == 200 and got["delivery"] == "started" and done.wait(5) and seen == [(q.id,)]


# --- refusals before any prompt ---------------------------------------------------------------------------

def test_an_off_box_request_is_403_and_never_reaches_the_signer(rig, store):
    q = file_q(store)
    calls = Calls()
    got, status = rig(body(q), factory=calls, local=False)
    assert (status, got["error"]) == (403, "desk_remote") and calls.n == 0 and rig.notes == []
    assert answers(store) == []


def test_a_moved_row_version_is_409_stale_with_no_prompt(rig, store):
    q = file_q(store)
    calls = Calls()
    got, status = rig(body(q, version="rv-old-version"), factory=calls)
    assert (status, got["error"]) == (409, "stale") and calls.n == 0 and rig.notes == []
    store.answer(q.id, option="ship", channel="cockpit")
    got, status = rig(body(q, version=band_view.subject_version(store.get(q.id)), txn="txn-0000002"), factory=calls)
    assert (status, got["error"]) == (409, "stale") and calls.n == 0
    assert answers(store) == [(q.id, "ship")]


def test_the_body_names_only_row_version_option_and_request_id(rig, store):
    q = file_q(store)
    calls = Calls()
    for extra in ({"message": "x"}, {"sig": "x"}, {"surface": "evil"}, {"view": "7"}, {"via": "cockpit-desk"}):
        got, status = rig(body(q, **extra), factory=calls)
        assert (status, got["error"]) == (400, "bad_request"), extra
    assert rig(body(q, pick="z"), factory=calls)[1] == 400
    assert rig(body(q, txn="x"), factory=calls)[1] == 400
    both = body(q)
    both["text"] = "x"
    assert rig(both, factory=calls)[1] == 400
    assert rig("not an object", factory=calls)[1] == 400
    assert calls.n == 0 and answers(store) == []


def test_policy_b_an_information_click_is_not_signed_and_the_page_uses_the_ordinary_path(rig, store):
    q = file_q(store)
    calls = Calls()
    got, status = rig(body(q), factory=calls, policy="authority")
    assert (status, got["error"]) == (422, "desk_unsigned") and calls.n == 0 and rig.notes == []
    assert answers(store) == []


def test_a_levain_authority_item_is_refused_before_any_prompt_until_L4(rig, store):
    q = file_q(store, link=Link(option="ship", kind="push", request_id="req-1", queue_id="q-1"))
    calls = Calls()
    got, status = rig(body(q), factory=calls, policy="authority")
    assert (status, got["error"]) == (422, "desk_authority_pending") and calls.n == 0 and rig.notes == []


def test_no_enrolled_key_is_409_no_key_and_asks_for_nothing(store, tmp_path):
    q = file_q(store)
    _STORE["s"] = store
    calls = Calls()
    state = desk_click.DeskState()
    got, status = desk_click.desk_answer(req(body(q)), desk_click.LevainDeskHost(store), state, policy="all",
                                         signer_factory=calls, key_dir=tmp_path / "no-keys", notify=lambda t: None)
    assert (status, got["error"]) == (409, "no_key") and calls.n == 0 and answers(store) == []


# --- policy (b): an authority row DOES sign (a host that can record it) -------------------------------------

class FakeHost:
    """A host with an authority row it can record (what flow's `authorises` rows will be once F7 lands)."""

    def __init__(self, kind="authority"):
        self.recorded, self.kind, self.version = [], kind, "rv-auth-1"

    def scope(self):
        return ("flow-needs-phill", "flow-3ce2832a8fe94ea7")

    def row(self, subject_id):
        if subject_id != "np-auth":
            return None
        return desk_click.DeskRow("np-auth", self.version, "Push the tag?", self.kind,
                                  (("a", "Yes, push it"), ("b", "No")))

    def record(self, row, pick, text, viewer, signing):
        self.recorded.append((row.id, pick, text, viewer, signing.state, signing.message))
        return type("Done", (), {"answered": (), "recorded": [pick or "text"], "echo": "ok"})()


def test_policy_b_an_authority_row_signs_and_is_recorded_with_its_message(rig):
    host, calls = FakeHost(), Calls()
    path_calls = []

    def factory(path, timeout=None):
        path_calls.append(path)
        return calls(path, timeout)
    got, status = rig({"subject": {"id": "np-auth", "version": "rv-auth-1"}, "pick": "a", "client_txn": "txn-auth-001"},
                      host=host, factory=factory, policy="authority")
    assert status == 200 and calls.n == 1 and len(host.recorded) == 1
    msg = ts.parse_desk_verified(host.recorded[0][5].encode())
    assert msg.store == "flow-needs-phill" and msg.entries[0].kind == "authority"
    assert msg.entries[0].answer == "(a) Yes, push it" and msg.gesture == "click"


# --- cancel, timeout, a version that moves under the touch, two clicks, a replay ---------------------------------

@pytest.mark.parametrize("fail,reason,words", [
    (KeyError_("Touch ID was cancelled"), "cancelled", "Touch ID cancelled: nothing recorded"),
    (KeyError_("TimeoutExpired: ssh-keygen"), "timeout", "Touch ID timed out: nothing recorded"),
    (KeyError_("could not sign with x (exit 255)"), "unavailable", "Touch ID did not sign (unavailable): nothing recorded"),
])
def test_cancel_or_timeout_records_nothing_and_leaves_the_card_open(rig, store, fail, reason, words):
    q = file_q(store)
    got, status = rig(body(q), factory=Calls(fail=fail))
    assert (status, got["error"], got["sign_reason"], got["message"]) == (409, "unsigned", reason, words)
    assert answers(store) == [] and store.get(q.id).answer is None
    # a click costs nothing to repeat: a fresh request id signs
    again, status = rig(body(q, txn="txn-0000009"), factory=Calls())
    assert status == 200 and answers(store) == [(q.id, "ship")]


def test_a_row_that_moves_after_the_touch_records_nothing_and_discards_the_signature(rig, store):
    q = file_q(store)
    calls = Calls(during=lambda: store.answer(q.id, option="hold", channel="phone"))
    got, status = rig(body(q), factory=calls)
    assert (status, got["error"]) == (409, "stale") and "discarded" in got["message"] and calls.n == 1
    assert answers(store) == [(q.id, "hold")] and store.get(q.id).answer.channel == "phone"   # his other answer stands


def test_a_second_click_while_a_sign_is_pending_is_sign_busy_and_raises_no_second_prompt(rig, store):
    q1, q2 = file_q(store, "First?"), file_q(store, "Second?")
    inner: list = []
    calls_b = Calls()
    calls = Calls(during=lambda: inner.append(rig(body(q2, txn="txn-0000002"), factory=calls_b)))
    got, status = rig(body(q1), factory=calls)
    assert status == 200
    (busy, bstatus), = inner
    assert (bstatus, busy["error"]) == (409, "sign_busy") and calls_b.n == 0
    assert answers(store) == [(q1.id, "ship")]


def test_a_replayed_request_id_returns_its_first_result_without_a_second_prompt(rig, store):
    q = file_q(store)
    calls = Calls()
    sent = body(q)                       # a replay is the same request, byte for byte
    first = rig(sent, factory=calls)
    second = rig(sent, factory=calls)
    assert first == second and calls.n == 1 and len(answers(store)) == 1
    cancelled = Calls(fail=KeyError_("Touch ID was cancelled"))
    q2 = file_q(store, "Another?")
    sent2 = body(q2, txn="txn-0000077")
    one = rig(sent2, factory=cancelled)
    two = rig(sent2, factory=cancelled)
    assert one == two and cancelled.n == 1


# --- the band box: a signed line ----------------------------------------------------------------------------------

def _shown(store):
    b = band_view.band_payload(store, "cockpit:web")
    return b["store_id"], b["view_id"]


def test_a_signed_line_in_the_band_box_is_one_typed_message_over_its_entries(store, keydir):
    q1, q2 = file_q(store, "First?"), file_q(store, "Second?")
    sid, vid = _shown(store)
    notes: list[str] = []
    done = band_view.answer_text_desk(store, "1a, 2b", viewer="cockpit:web", store_id=sid, view_id=vid, surface="web",
                                      origin=ORIGIN, signer_factory=real_desk, key_dir=keydir, notify=notes.append,
                                      policy="all")
    assert sorted(done.recorded) == ["1a", "2b"] and notes == ["Desk: Touch ID to sign: " + q1.id + " (a) Ship it tonight (+1 more)"]
    msg = ts.parse_desk_verified(band_view.answer_sign_of(store, q1.id)["message"].encode())
    assert msg.gesture == "typed" and msg.typed == "1a, 2b" and msg.view == str(vid) and len(msg.entries) == 2
    assert context(store, q2.id)[:3] == ("cockpit", "cockpit:web", "signed")


def test_a_cancelled_line_records_nothing_and_keeps_the_card_open(store, keydir):
    q = file_q(store)
    sid, vid = _shown(store)
    with pytest.raises(ValueError, match="Touch ID cancelled: nothing was recorded"):
        band_view.answer_text_desk(store, "1a", viewer="cockpit:web", store_id=sid, view_id=vid, surface="web",
                                   origin=ORIGIN, key_dir=keydir, notify=lambda t: None, policy="all",
                                   signer_factory=Calls(fail=KeyError_("Touch ID was cancelled")))
    assert answers(store) == [] and store.get(q.id).answer is None


def test_answer_item_desk_refuses_an_unsigned_attempt_and_a_moved_version(store):
    q = file_q(store)
    subject = {"kind": "question", "id": q.id, "version": band_view.subject_version(q)}
    with pytest.raises(ValueError):
        band_view.answer_item_desk(store, subject, pick="a", text=None, viewer="cockpit:web",
                                   signing=band_view.Signing("unsigned", "cancelled", "x", versions={q.id: subject["version"]}))
    signed = band_view.Signing("signed", None, "", "m", "s", "SHA256:x", {q.id: "rv-other"})
    with pytest.raises(Exception, match="changed after you saw it"):
        band_view.answer_item_desk(store, subject, pick="a", text=None, viewer="cockpit:web", signing=signed)
    assert answers(store) == []


# --- the HTTP wiring ---------------------------------------------------------------------------------------------

def _http(url, payload, token=TOKEN, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["X-Levain-Write-Token"] = token
    r = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:  # noqa: S310 - loopback
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _band(base, token=TOKEN):
    r = urllib.request.Request(f"{base}/cockpit/band.json", headers={"X-Levain-Write-Token": token})
    with urllib.request.urlopen(r, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def test_http_route_needs_the_token_refuses_a_proxied_request_and_reports_desk_sign(tmp_path, store, keydir, monkeypatch):
    _STORE["s"] = store
    q = file_q(store)
    _root, src, _ids = _install(tmp_path)
    good = {"subject": {"id": q.id, "version": band_view.subject_version(q)}, "pick": "a", "client_txn": "txn-http-001"}
    with _serve(src, write_token=TOKEN) as (base, _h):
        url = f"{base}/cockpit/desk/answer"
        assert _http(url, good, token=None)[0] == 403
        assert _http(url, good, token="not-the-token")[0] == 403
        assert _band(base)["desk_sign"] is True                                    # local and a key is enrolled
        status, got = _http(url, good, headers={"X-Forwarded-For": "100.64.0.9"})
        # a proxied request is off the laptop: the owner key is not honoured through a proxy, so the gate refuses it
        # before the route (desk_remote is the route's own answer, tested on the pure function above)
        assert status == 403 and got["error"] in ("credential_required", "forbidden", "desk_remote")
        status, got = _http(url, good)                                              # local, policy (b): information
        assert (status, got["error"]) == (422, "desk_unsigned")
        status, got = _http(url, {**good, "subject": {"id": q.id, "version": "rv-old"}, "client_txn": "txn-http-002"})
        assert (status, got["error"]) == (409, "stale")
    assert answers(store) == []


def test_http_signed_click_end_to_end_under_policy_a(tmp_path, store, keydir, monkeypatch):
    _STORE["s"] = store
    q = file_q(store)
    _root, src, _ids = _install(tmp_path)
    monkeypatch.setattr(desk_click, "DESK_POLICY", "all")
    monkeypatch.setattr(ts, "default_desk_signer_factory", real_desk)
    monkeypatch.setattr(ts, "post_notice", lambda text, **kw: None)
    good = {"subject": {"id": q.id, "version": band_view.subject_version(q)}, "pick": "b", "client_txn": "txn-http-003"}
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, got = _http(f"{base}/cockpit/desk/answer", good)
    assert status == 200 and got["signed"] and answers(store) == [(q.id, "hold")]
    assert context(store, q.id)[:3] == ("cockpit", "cockpit:browser", "signed") or context(store, q.id)[2] == "signed"


def test_desk_sign_is_false_without_a_key_and_off_the_laptop(tmp_path, keydir):
    assert desk_click.desk_sign_available(True, key_dir=keydir) is True
    assert desk_click.desk_sign_available(False, key_dir=keydir) is False
    assert desk_click.desk_sign_available(True, key_dir=tmp_path / "none") is False

    def broken(_):
        raise KeyError_("unreadable")
    assert desk_click.desk_sign_available(True, find_key=broken) is False


# --- the band-box route (desk_text) and the history badge --------------------------------------------------------

def _text_req(store, text, txn="txn-text-001", local=True, **over):
    sid, vid = _shown(store)
    b = {"text": text, "store_id": sid, "view_id": vid, "client_txn": txn}
    b.update(over)
    return desk_click.DeskRequest(local, "web", ORIGIN, b)


def _text(store, keydir, request, factory=None, policy="all", state=None):
    return desk_click.desk_text(request, store, state or desk_click.DeskState(), policy=policy, signer_factory=factory,
                                key_dir=keydir, notify=lambda t: None)


def test_text_route_signs_a_line_and_policy_b_leaves_it_unsigned(store, keydir):
    q = file_q(store)
    calls = Calls()
    got, status = _text(store, keydir, _text_req(store, "1a"), factory=calls, policy="authority")
    assert (status, got["error"]) == (422, "desk_unsigned") and calls.n == 0 and answers(store) == []
    got, status = _text(store, keydir, _text_req(store, "1a", txn="txn-text-002"), factory=calls)
    assert status == 200 and got["signed"] and got["recorded"] == ["1a"] and calls.n == 1
    assert answers(store) == [(q.id, "ship")]


def test_text_route_walls(store, keydir, tmp_path):
    q = file_q(store)
    calls = Calls()
    assert _text(store, keydir, _text_req(store, "1a", local=False), factory=calls)[1] == 403
    assert _text(store, keydir, _text_req(store, "1a", message="x"), factory=calls)[1] == 400
    got, status = _text(store, keydir, _text_req(store, "1a", txn="txn-text-003", view_id=999), factory=calls)
    assert (status, got["error"]) == (409, "stale_band")
    cancel = Calls(fail=KeyError_("Touch ID was cancelled"))
    got, status = _text(store, keydir, _text_req(store, "1a", txn="txn-text-004"), factory=cancel)
    assert (status, got["error"]) == (409, "unsigned") and "nothing was recorded" in got["message"]
    nokey, status = desk_click.desk_text(_text_req(store, "1a", txn="txn-text-005"), store, desk_click.DeskState(),
                                         policy="all", key_dir=tmp_path / "none", notify=lambda t: None)
    assert (status, nokey["error"]) == (409, "no_key")
    assert calls.n == 0 and answers(store) == [] and store.get(q.id).answer is None


def test_the_history_badge_reads_the_signature_through_answer_class(rig, store, keydir, monkeypatch):
    q = file_q(store)
    rig(body(q, pick="b"), factory=Calls())
    ok = lambda pk: (True, "bio")        # noqa: E731
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok) == "signed: Touch ID, laptop (desk)"
    # a tampered stored message reads as a failed claim, never as signed
    real = band_view.answer_sign_of(store, q.id)
    monkeypatch.setattr(band_view, "answer_sign_of", lambda st, qid: {
        **real, "message": real["message"].replace("Hold for the morning", "Ship it tonight")})
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok).startswith("signature failed")


def test_the_badge_is_empty_for_an_unsigned_answer_and_without_a_key(rig, store, keydir, tmp_path):
    q = file_q(store)
    band_view.answer_item(store, {"kind": "question", "id": q.id, "version": band_view.subject_version(q)},
                          pick="a", text=None, channel="cockpit", viewer="cockpit:web")
    assert band_view.answer_badge(store, q.id, key_dir=keydir) == ""
    assert band_view.answer_badge(store, "q-nope", key_dir=keydir) == ""
    assert band_view.answer_badge(store, q.id, key_dir=tmp_path / "none") == ""


def test_the_delta_line_shows_the_badge(rig, store, keydir, monkeypatch):
    q = file_q(store)
    band_view.band_payload(store, "cockpit:web")
    prev, _vid = band_view._newest_view(store, "cockpit:web")
    rig(body(q), factory=Calls())
    monkeypatch.setattr(ts, "_real_key_dir", lambda: keydir)
    monkeypatch.setattr(ts, "ctk_status", lambda pk, **kw: (True, "bio"))
    line = band_view._delta_line(store, prev, [])
    assert "answered via cockpit, signed: Touch ID, laptop (desk)" in line, line


def test_a_slow_verifier_never_holds_the_delta_line_past_its_deadline(rig, store, keydir, monkeypatch):
    """Three closed signed rows behind a CTK check that takes 5 s each: the delta line returns within its deadline
    with no badge, and the check never runs while the render's read transaction is open."""
    import time

    qs = [file_q(store, what=f"Ship fix {i}?") for i in range(3)]
    band_view.band_payload(store, "cockpit:web")
    prev, _vid = band_view._newest_view(store, "cockpit:web")
    for i, q in enumerate(qs):
        rig(body(q, txn=f"txn-slow-00{i}"), factory=Calls())
    open_reads = [0]
    real_peek = type(store)._peek

    def counting_peek(self):
        cm = real_peek(self)

        class _C:
            def __enter__(s):
                open_reads[0] += 1
                return cm.__enter__()

            def __exit__(s, *a):
                open_reads[0] -= 1
                return cm.__exit__(*a)
        return _C()
    monkeypatch.setattr(type(store), "_peek", counting_peek)
    seen_open: list[int] = []

    def slow(pk, **kw):
        seen_open.append(open_reads[0])
        time.sleep(5)
        return True, "bio"
    monkeypatch.setattr(ts, "_real_key_dir", lambda: keydir)
    monkeypatch.setattr(ts, "ctk_status", slow)
    band_view._badge_cache.clear()
    t0 = time.monotonic()
    line = band_view._delta_line(store, prev, [], time.monotonic() + 0.4)
    took = time.monotonic() - t0
    assert took < 1.0, took
    assert "answered via cockpit" in line and "signed:" not in line, line
    deadline = time.monotonic() + 3
    while not seen_open and time.monotonic() < deadline:      # the worker reaches the check after the render left
        time.sleep(0.05)
    assert seen_open and set(seen_open) == {0}, seen_open      # one shared check, no read transaction held


def test_badges_share_one_ctk_check_and_cache_their_verdict(rig, store, keydir, monkeypatch):
    qs = [file_q(store, what=f"Ship fix {i}?") for i in range(3)]
    for i, q in enumerate(qs):
        rig(body(q, txn=f"txn-share-0{i}"), factory=Calls())
    asked: list[str] = []

    def ctk(pk):
        asked.append(pk)
        return True, "bio"
    band_view._badge_cache.clear()
    got = band_view.answer_badges(store, [q.id for q in qs], key_dir=keydir, ctk=ctk)
    assert got == {q.id: "signed: Touch ID, laptop (desk)" for q in qs} and len(asked) == 1
    again = band_view.answer_badges(store, [q.id for q in qs], key_dir=keydir, ctk=ctk)
    assert again == got and len(asked) == 1                    # cached per (row, signature digest)


# --- L3 r1: request-bound replay, one prompt per request, policy on the typed path, one lock -------------------

def test_a_request_id_reused_for_a_different_answer_is_refused(rig, store, keydir):
    q1, q2 = file_q(store, "First?"), file_q(store, "Second?")
    calls = Calls()
    sent = body(q1, txn="txn-reuse-01")
    first, status = rig(sent, factory=calls)
    assert status == 200
    for other in (body(q2, txn="txn-reuse-01"), {**sent, "pick": "b"}):
        got, status = rig(other, factory=calls)
        assert (status, got["error"]) == (409, "txn_reused"), got
    got, status = desk_click.desk_answer(req(sent, surface="phone"),
                                         desk_click.LevainDeskHost(store), rig.state, policy="all", signer_factory=calls,
                                         key_dir=keydir, notify=lambda t: None)
    assert (status, got["error"]) == (409, "txn_reused")                   # another surface
    text, status = _text(store, keydir, _text_req(store, "2a", txn="txn-reuse-01"), factory=calls, state=rig.state)
    assert (status, text["error"]) == (409, "txn_reused")                  # the other route
    assert rig(sent, factory=calls) == (first, 200)                         # the same request still replays
    assert calls.n == 1 and answers(store) == [(q1.id, "ship")]


def test_a_concurrent_duplicate_gets_one_prompt_and_the_first_result(rig, store):
    q = file_q(store)
    dup: list = []
    threads: list[threading.Thread] = []
    sent = body(q)

    def during():
        t = threading.Thread(target=lambda: dup.append(rig(sent, factory=calls)))
        threads.append(t)
        t.start()
        t.join(0.3)                     # the duplicate is waiting on the first, not prompting
        assert t.is_alive()
    calls = Calls(during=during)
    first = rig(sent, factory=calls)
    threads[0].join(10)
    assert first[1] == 200 and dup == [first] and calls.n == 1 and answers(store) == [(q.id, "ship")]


def test_a_delivery_thread_that_cannot_start_leaves_the_answer_and_says_deferred(rig, store, monkeypatch):
    q = file_q(store)

    def no_thread(deliver, done):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(desk_click, "_start_delivery", no_thread)
    sent = body(q)
    got, status = rig(sent, factory=Calls(), delivered=lambda done: None)
    assert status == 200 and got["signed"] and got["delivery"] == "deferred", got
    assert answers(store) == [(q.id, "ship")]
    assert rig(sent, factory=Calls()) == (got, 200)


def test_an_unexpected_record_error_is_remembered_and_never_prompts_again(rig):
    class Boom(FakeHost):
        def record(self, row, pick, text, viewer, signing):
            raise RuntimeError("disk on fire")
    calls = Calls()
    b = {"subject": {"id": "np-auth", "version": "rv-auth-1"}, "pick": "a", "client_txn": "txn-boom-001"}
    one = rig(b, host=Boom(), factory=calls, policy="authority")
    two = rig(b, host=Boom(), factory=calls, policy="authority")
    assert one == two and one[1] == 500 and one[0]["error"] == "internal" and calls.n == 1


def test_a_line_answered_elsewhere_during_the_touch_is_refused_not_signed(store, keydir):
    q = file_q(store)
    calls = Calls(during=lambda: store.answer(q.id, option="hold", channel="phone"))
    got, status = _text(store, keydir, _text_req(store, "1a", txn="txn-text-gone"), factory=calls)
    assert (status, got["error"]) == (422, "answer_refused") and "signed" not in got, got
    assert calls.n == 1 and answers(store) == [(q.id, "hold")]


def test_the_typed_path_follows_the_desk_policy(store, keydir):
    """NP-A (b): an information line typed in the band box records unsigned and raises no sheet, as a click does."""
    q = file_q(store)
    sid, vid = _shown(store)
    calls = Calls()
    done = band_view.answer_text_desk(store, "1a", viewer="cockpit:web", store_id=sid, view_id=vid, surface="web",
                                      origin=ORIGIN, signer_factory=calls, key_dir=keydir, notify=lambda t: None)
    assert done.recorded == ["1a"] and calls.n == 0
    assert context(store, q.id)[2] != "signed" and band_view.answer_sign_of(store, q.id)["sig"] is None


def test_a_line_typed_while_a_click_signs_is_sign_busy(rig, store, keydir):
    q1, q2 = file_q(store, "First?"), file_q(store, "Second?")
    inner: list = []
    calls_b = Calls()
    calls = Calls(during=lambda: inner.append(
        _text(store, keydir, _text_req(store, "2a", txn="txn-text-busy"), factory=calls_b, state=rig.state)))
    got, status = rig(body(q1), factory=calls)
    assert status == 200
    (busy, bstatus), = inner
    assert (bstatus, busy["error"]) == (409, "sign_busy") and calls_b.n == 0
    assert answers(store) == [(q1.id, "ship")]


# --- L3 r1: the signed origin is the page's own; a surface outside the form keeps its own viewer ---------------

def test_a_request_with_no_origin_of_this_server_asks_for_nothing(rig, store, keydir):
    q = file_q(store)
    calls = Calls()
    for origin in (None, "http://evil.example"):
        got, status = desk_click.desk_answer(desk_click.DeskRequest(True, "web", origin, body(q)),
                                             desk_click.LevainDeskHost(store), rig.state, policy="all",
                                             signer_factory=calls, key_dir=keydir, notify=lambda t: None)
        assert (status, got["error"]) == (403, "desk_origin")
        got, status = desk_click.desk_text(desk_click.DeskRequest(True, "web", origin, _text_req(store, "1a").body),
                                           store, rig.state, policy="all", signer_factory=calls, key_dir=keydir,
                                           notify=lambda t: None)
        assert (status, got["error"]) == (403, "desk_origin")
    assert calls.n == 0 and answers(store) == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_http_signs_the_origin_the_page_was_served_at(tmp_path, store, keydir, monkeypatch, host):
    _STORE["s"] = store
    q = file_q(store)
    _root, src, _ids = _install(tmp_path)
    monkeypatch.setattr(desk_click, "DESK_POLICY", "all")
    monkeypatch.setattr(ts, "default_desk_signer_factory", real_desk)
    monkeypatch.setattr(ts, "post_notice", lambda text, **kw: None)
    good = {"subject": {"id": q.id, "version": band_view.subject_version(q)}, "pick": "b", "client_txn": "txn-http-0o1"}
    with _serve(src, write_token=TOKEN) as (base, httpd):
        port = httpd.server_address[1]
        status, got = _http(f"{base}/cockpit/desk/answer", good, headers={"Host": f"{host}:{port}"})
    assert status == 200 and got["signed"], got
    msg = ts.parse_desk_verified(context(store, q.id)[4].encode())
    assert msg.origin == f"http://{host}:{port}"


def test_a_credential_named_outside_the_form_answers_under_its_own_viewer(store, keydir):
    """A surface token named `work laptop`: the band box finds the view band.json recorded for `cockpit:work laptop`
    (never `cockpit:web`), the answer is audited under that viewer, and the badge verifies it."""
    q = file_q(store)
    b = band_view.band_payload(store, "cockpit:work laptop")
    request = desk_click.DeskRequest(True, "work laptop", ORIGIN, {"text": "1a", "store_id": b["store_id"],
                                                                   "view_id": b["view_id"], "client_txn": "txn-wl-0001"})
    got, status = desk_click.desk_text(request, store, desk_click.DeskState(), policy="all", signer_factory=Calls(),
                                       key_dir=keydir, notify=lambda t: None)
    assert status == 200 and got["signed"], got
    via, viewer, state, _has, message = context(store, q.id)
    assert viewer == "cockpit:work laptop" and state == "signed"
    assert ts.parse_desk_verified(message.encode()).surface == ts.desk_surface("work laptop")
    ok = lambda pk: (True, "bio")        # noqa: E731
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok) == "signed: Touch ID, laptop (desk)"


def test_a_duplicate_waits_longer_than_a_touch_can_take():
    assert desk_click._WAIT_S > ts.SIGN_BOUND_S


# --- L3 r2 W1: the history reader's scope is its own, never the signed message's ---------------------------------

def _resigned(keydir, message: str, old: str, new: str) -> tuple[str, str]:
    """The stored message with one line moved, signed again by the enrolled key: a valid signature over other scope."""
    assert old in message
    moved = message.replace(old, new)
    return moved, ckeys.ssh_signer(keydir / ts.KEY_LABEL, namespace=ts.DESK_NAMESPACE)[1](moved.encode())


@pytest.mark.parametrize("what", ["origin", "view", "surface"])
def test_a_stored_signature_under_another_origin_surface_or_view_is_failed(store, keydir, monkeypatch, what):
    q = file_q(store)
    sid, vid = _shown(store)
    other_view = band_view.band_payload(store, "cockpit:other")["view_id"]        # a view another credential holds
    got, status = _text(store, keydir, _text_req(store, "1a"), factory=Calls())
    assert status == 200 and got["signed"], got
    ok = lambda pk: (True, "bio")        # noqa: E731
    band_view._badge_cache.clear()
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok) == "signed: Touch ID, laptop (desk)"
    real = band_view.answer_sign_of(store, q.id)
    old, new = {"origin": (f"origin: {ORIGIN}\n", "origin: http://127.0.0.1:8765\n"),
                "view": (f"view: {vid}\n", f"view: {other_view}\n"),
                "surface": ("surface: web\n", "surface: other\n")}[what]
    message, sig = _resigned(keydir, real["message"], old, new)
    monkeypatch.setattr(band_view, "answer_sign_of", lambda st, qid: {**real, "message": message, "sig": sig})
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok) == "signature failed (scope-mismatch)", what


def test_the_reader_names_the_origins_it_accepts(rig, store, keydir):
    q = file_q(store)
    rig(body(q), factory=Calls())
    ok = lambda pk: (True, "bio")        # noqa: E731
    band_view._badge_cache.clear()
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok,
                                  origins=["http://localhost:7420"]) == "signature failed (scope-mismatch)"
    assert band_view.answer_badge(store, q.id, key_dir=keydir, ctk=ok, origins=[ORIGIN]) == "signed: Touch ID, laptop (desk)"
