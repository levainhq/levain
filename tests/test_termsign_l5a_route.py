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
    first = rig(body(q), factory=calls)
    second = rig(body(q), factory=calls)
    assert first == second and calls.n == 1 and len(answers(store)) == 1
    cancelled = Calls(fail=KeyError_("Touch ID was cancelled"))
    q2 = file_q(store, "Another?")
    one = rig(body(q2, txn="txn-0000077"), factory=cancelled)
    two = rig(body(q2, txn="txn-0000077"), factory=cancelled)
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
                                      origin=ORIGIN, signer_factory=real_desk, key_dir=keydir, notify=notes.append)
    assert sorted(done.recorded) == ["1a", "2b"] and notes == ["Desk: Touch ID to sign: " + q1.id + " (a) Ship it tonight (+1 more)"]
    msg = ts.parse_desk_verified(band_view.answer_sign_of(store, q1.id)["message"].encode())
    assert msg.gesture == "typed" and msg.typed == "1a, 2b" and msg.view == str(vid) and len(msg.entries) == 2
    assert context(store, q2.id)[:3] == ("cockpit", "cockpit:web", "signed")


def test_a_cancelled_line_records_nothing_and_keeps_the_card_open(store, keydir):
    q = file_q(store)
    sid, vid = _shown(store)
    with pytest.raises(ValueError, match="Touch ID cancelled: nothing was recorded"):
        band_view.answer_text_desk(store, "1a", viewer="cockpit:web", store_id=sid, view_id=vid, surface="web",
                                   origin=ORIGIN, key_dir=keydir, notify=lambda t: None,
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
