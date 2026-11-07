"""A held ``claude_code`` consent request reaches his phone with something he can APPROVE (1105-17, levain 36).

Phill 11-05 14:54: a held request's push DID reach four devices, but he saw nothing to approve (and never saw the ping at
all: it went out as "levain", 1.5 s before a SIGN row's ping). The sign sheet for a consent request already existed
(``GET /sign/consent/<id>``, its ceremony ``POST /cockpit/consent/<id>/{challenge,answer}``); nothing linked to it. These
tests hold a real request in a rig and follow it: the ping the hold files (title, words), where the push opens (the center
on its own item, as for a SIGN row), what the center's focus view shows for it (a link built on the page's own origin from
the id alone), and the sheet the link opens (it renders, a passkey answers it, a decided request says so).

What stays undischarged here is a real Face ID approve of a held request on a phone."""
from __future__ import annotations

import json
import shutil
import urllib.parse
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code, codex
from levain.webpush import center, sender
from levain.webpush.store import PushStore
from tests._push_fixtures import phone
from tests.test_cockpit_consent_sheet import PAGE, RP, _call, _file, _q, _queue, _serving, _signer, home, world  # noqa: F401
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_notify_center_page import MODAL, OPEN, OPENED, PILL, Rig, _env, _json, _url  # noqa: F401  (_env: autouse)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

HELD_TITLE = "A seat waits for your approval"


def _hold(home, monkeypatch, what: str = "Write: a.txt"):
    """A request filed in the rig's queue and the hold's ping for it, the push spawn spied (nothing is sent anywhere).
    Returns (request id, the spawn's kwargs)."""
    calls: list[dict] = []
    phone(home.qdir)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 10.0)
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    rid = _file(home)
    assert claude_code.hold_ping(claude_code.AdapterSettings(), home.qdir, "claude_code", what, rid, 300.0) is True
    assert len(calls) == 1
    return rid, calls[0]


def _record(home, call: dict) -> tuple[PushStore, int]:
    """What the sender's child writes for that spawn: the ping row (so the center can read it)."""
    store = PushStore(home.qdir)
    return store, store.add_ping(call["kind"], call["ref"], call["title"], call["body"], None)


def test_a_held_requests_ping_says_what_it_is_in_the_sheets_own_words(home, monkeypatch) -> None:
    rid, call = _hold(home, monkeypatch)
    assert (call["kind"], call["ref"]) == ("consent", f"request:{rid}")
    assert call["title"] == HELD_TITLE != "levain"
    assert call["body"] == "claude_code is waiting for your approval: Write: a.txt. Open it to review and answer (held up to 300 s)."
    assert rid not in call["body"] and call["push"] is True


def test_the_words_promise_no_answer_the_sheet_may_not_offer(home, monkeypatch, tmp_path) -> None:
    """An agent-configuration write is filed at T3 (_gate_call in claude_code.py and codex.py) and a phone passkey can only DENY a T3: its
    sheet offers Deny alone. The hook that words the ping knows neither the tier of the request nor the key that opens it,
    so the words name no answer ("Allow") at all; the link says "Review", the sheet itself says what it offers."""
    rid, call = _hold(home, monkeypatch)
    t3 = _file(home, "T3", name="cfg.json")
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        t2 = [o["key"] for o in _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[1]["request"]["options"]]
        status, got = _call(url, f"/cockpit/consent/{_q(t3)}/sheet.json")
    got = got["request"]
    assert status == 200 and [o["key"] for o in got["options"]] == ["deny"] and "cannot allow a T3" in got["note"], got
    assert t2 == ["allow", "deny"]
    for words in (call["body"], claude_code.hold_message("claude_code", "Write: cfg.json", 300.0)):
        assert "Allow" not in words and "Approve" not in words, words


@pytest.mark.parametrize("kind", ["consent", "push", "reping"])
def test_every_approval_kind_ping_about_a_request_takes_the_approval_title(tmp_path, monkeypatch, kind) -> None:
    """The gate's own ping (kind consent), a push gate's and a re-ping all carry ``request:<id>``; a ping with no request
    (a prompt notice's ``prompt:`` ref, a ruling) stays "levain": there is no sheet for it."""
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    phone(q)
    calls: list[dict] = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 10.0)
    s = claude_code.AdapterSettings()
    claude_code.notify(s, "levain: x", cwd=q, kind=kind, ref="request:r1")
    claude_code.notify(s, "levain: x", cwd=q, kind=kind, ref="prompt:abc")
    claude_code.notify(s, "levain: x", cwd=q, kind="ruling", ref="request:r2")
    assert [c["title"] for c in calls] == [HELD_TITLE, "levain", "levain"]


def test_the_codex_gate_ping_uses_the_same_words(home, monkeypatch) -> None:
    assert codex.hold_message is claude_code.hold_message
    assert claude_code.hold_message("codex", "Bash: ls", 90.2).endswith("(held up to 91 s).")


def test_the_push_opens_the_center_on_its_own_item_as_a_sign_rows_does(home, monkeypatch) -> None:
    """The np-row rule exactly: whatever link the caller gave, the payload url is the center link ``p<ping id>``, never a
    sheet address and never an absolute one; the link is drawn by the focus view that link opens."""
    _rid, call = _hold(home, monkeypatch)
    store, pid = _record(home, call)
    assert center.push_url(store, pid, call["kind"], call["url"]) == f"/?view=notifications&n=p{pid}"
    payload = json.loads(sender.build_payload(call["title"], call["body"], call["ref"],
                                              center.push_url(store, pid, call["kind"], call["url"]), pid))
    assert payload["title"] == HELD_TITLE and payload["url"] == f"/?view=notifications&n=p{pid}"
    assert payload["tag"] == call["ref"]


def test_a_consent_ping_and_a_sign_rows_ping_cannot_replace_each_other(home, monkeypatch) -> None:
    """The device keys a notification by the payload ``tag`` (the service worker's showNotification) and the push service
    by the ``Topic``: both are the ping's ref, and a consent ping's ref (``request:<id>``) is never an np row's id."""
    _rid, call = _hold(home, monkeypatch)
    np_ref = "np-d747cc82"
    tag = lambda ref: json.loads(sender.build_payload("t", "b", ref, "/", 1))["tag"]    # noqa: E731
    assert tag(call["ref"]) != tag(np_ref)
    assert sender.topic_for(call["ref"]) != sender.topic_for(np_ref)
    assert call["ref"].startswith("request:")


def test_the_focus_items_reading_of_a_held_request_follows_the_queue(home, monkeypatch) -> None:
    rid, call = _hold(home, monkeypatch)
    store, pid = _record(home, call)
    item = lambda: center.get(store, f"p{pid}")["item"]    # noqa: E731
    assert item()["sheet"] == {"id": rid, "state": "open"} and item()["needs_open"] is True
    assert _queue(home).withdraw(rid)
    assert item()["sheet"] == {"id": rid, "state": "closed"} and item()["needs_open"] is False


def test_a_lapsed_hold_and_a_vanished_request_and_a_ping_with_no_request(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from levain.consent.queue import ConsentQueue, authority_source
    from tests._h4 import h4_home

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    then = datetime.now(timezone.utc) - timedelta(minutes=10)      # filed ten minutes ago with a one minute hold
    cq = ConsentQueue(q, authority=authority_source(q, anchor=None), home=h4_home(), clock=lambda: then)
    r = cq.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={"file_path": "/w/a", "content": "x"},
                cwd="/w", hold=timedelta(minutes=1), event_id="e1")
    soon = cq.file(agent="claude_code", agent_session_id="s2", tool_name="Write", tool_input={"file_path": "/w/b", "content": "x"},
                   cwd="/w", hold=timedelta(minutes=10, seconds=-5), event_id="e2")      # ended five seconds ago, not yet swept
    s = PushStore(q)
    s.add_ping("consent", f"request:{r.id}", "t", "b")
    s.add_ping("consent", "request:pend-nope", "t", "b")
    s.add_ping("consent", "prompt:abc", "t", "b")
    s.add_ping("test", None, "t", "b")
    s.add_ping("consent", f"request:{soon.id}", "t", "b")
    items = {i["id"]: i for i in center.list_items(s)["items"]}
    assert items["p1"]["sheet"] == {"id": r.id, "state": "expired"}
    assert items["p2"]["sheet"] == {"id": "pend-nope", "state": "gone"}
    assert items["p3"]["sheet"] is None and items["p4"]["sheet"] is None
    assert items["p5"]["sheet"] == {"id": soon.id, "state": "expired"}
    # the grace window: the queue still says "pending" past expires_at; the card and the sheet read it the same way (expired)
    assert [items[i]["needs_open"] for i in ("p1", "p2", "p5")] == [False, False, False]


def test_a_request_is_read_from_the_queue_once_per_page_whatever_reads_it(tmp_path, monkeypatch) -> None:
    """The card's ``needs_open`` and the sheet's state are one reading per request per page (a list of N approval pings
    is N queue reads, as before the sheet existed), not one each."""
    from datetime import timedelta

    from levain.consent.queue import ConsentQueue, authority_source
    from tests._h4 import h4_home

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    cq = ConsentQueue(q, authority=authority_source(q, anchor=None), home=h4_home())
    r = cq.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={"file_path": "/w/a", "content": "x"},
                cwd="/w", hold=timedelta(minutes=5), event_id="e1")
    s = PushStore(q)
    s.add_ping("consent", f"request:{r.id}", "t", "b")
    s.add_ping("reping", f"request:{r.id}", "t", "b")
    reads: list[str] = []
    real = ConsentQueue.get
    monkeypatch.setattr(ConsentQueue, "get", lambda self, rid: reads.append(rid) or real(self, rid))
    items = center.list_items(s)["items"]
    assert [i["sheet"]["state"] for i in items] == ["open", "open"] and [i["needs_open"] for i in items] == [True, True]
    assert reads == [r.id]


@pytest.mark.parametrize("row", [
    ("none_expires", lambda: type("R", (), {"expires_at": None})(), "pending"),
    ("int_expires", lambda: type("R", (), {"expires_at": 5})(), "pending"),
    ("no_expires", lambda: object(), "pending"),
    ("odd_tuple", lambda: (1, 2, 3), None),
], ids=lambda r: r[0] if isinstance(r, tuple) else None)
def test_an_unreadable_row_answers_unread_and_never_fails_the_list(tmp_path, monkeypatch, row) -> None:
    """A row whose ``expires_at`` is not a stamp, or one the store hands back in a shape the reader does not expect, is
    ``unread`` for that item; the list is still served (its other items intact)."""
    from levain.consent.queue import ConsentQueue

    _name, make, state = row
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    s.add_ping("consent", "request:pend-x", "t", "b")
    s.add_ping("dated", None, "t", "Due today")
    monkeypatch.setattr(ConsentQueue, "get", lambda self, rid: make() if state is None else (make(), state))
    doc = center.list_items(s)
    by = {i["id"]: i for i in doc["items"]}
    assert by["p1"]["sheet"] == {"id": "pend-x", "state": "unread"} and by["p1"]["needs_open"] is False
    assert by["p2"]["sheet"] is None and center.get(s, "p1")["item"]["sheet"]["state"] == "unread"


def test_a_prompt_notice_is_not_a_request_ping_and_the_center_draws_no_sheet_for_it(home, monkeypatch) -> None:
    """The agent's own dialog still waits (no hold): ``prompt_notice`` used to record under ``request:<id>`` whenever the hook
    had filed a request, which the center reads as a held request (a sheet box saying "Already decided" for a request the
    hook withdrew as it left). It records under ``prompt:`` now; the hold's own ping for the same request still dedupes it."""
    calls: list[dict] = []
    phone(home.qdir)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 10.0)
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    rid = _file(home)
    assert _queue(home).withdraw(rid)                      # as the hook leaves the call to the dialog
    ref = claude_code.prompt_ref(rid)
    assert ref == f"prompt:{rid}" and claude_code.prompt_ref(None).startswith("prompt:")
    assert claude_code.prompt_notice(home.qdir, "claude_code", "Bash: touch x", ref) is True
    assert [(c["kind"], c["ref"], c["title"]) for c in calls] == [("consent", f"prompt:{rid}", "levain")]
    store, pid = _record(home, calls[0])
    item = center.get(store, f"p{pid}")["item"]
    assert item["sheet"] is None and item["needs_ref"] is None and item["needs_open"] is False
    assert item["dialog"] == {"agent": "claude_code"}                  # the focus view says what this is (1107-32)
    assert item["source"] == "approval" and item["body"].startswith("claude_code asks in its own dialog")
    # the hold's ping for the very request keeps the notice from repeating it
    other = _file(home, name="b.txt")
    PushStore(home.qdir).add_ping("consent", f"request:{other}", "t", "b")
    assert claude_code.prompt_notice(home.qdir, "claude_code", "Bash: touch x", claude_code.prompt_ref(other)) is False


def test_the_sheet_the_link_opens_renders_and_a_passkey_approves_the_held_request(home, monkeypatch, tmp_path) -> None:
    """End to end in the rig, by the link's own path: the cross-site navigation a tap makes is admitted, the sheet reads the
    request, and the challenge then answer routes accept its id (a ":" in it, as the link encodes it)."""
    rid, call = _hold(home, monkeypatch)
    fp = home.phone.fp
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        status, page = _call(url, f"/sign/consent/{_q(rid)}", token=None, extra=nav)
        assert status == 200 and b"<html" in page, status
        status, got = _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")
        assert status == 200 and got["request"]["id"] == rid and got["request"]["state"] == "pending", got
        status, ch = _call(url, f"/cockpit/consent/{_q(rid)}/challenge", {"decision": "allow", "signer_fp": fp})
        assert status == 200, ch
        status, out = _call(url, f"/cockpit/consent/{_q(rid)}/answer",
                            {"payload": ch["payload"], **home.phone.assertion(ch["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "allow", out
        assert _queue(home).get(rid)[1] == "allowed"
        # decided: the page shell still loads (it says "no longer open" itself) and the read is a clean 404
        assert _call(url, f"/sign/consent/{_q(rid)}", token=None, extra=nav)[0] == 200
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json") == (404, {"error": "not_open",
                                                                              "message": "this consent request is not open"})
    assert "This consent request is no longer open" in (Path(__file__).parents[1] / "levain/templates/web/sign.js").read_text()


# --- the focus view on the real page ----------------------------------------------------------------------------------
def _sheet_probe() -> str:
    return (f"JSON.stringify({{ link: [...{MODAL}.querySelectorAll('.nc-sheetbox a.nc-signgo')].map(a => [a.textContent, a.getAttribute('href'),"
            f" a.getAttribute('rel'), a.getBoundingClientRect().height]), notes: [...{MODAL}.querySelectorAll('.nc-sheetbox .nc-note')].map(x => x.textContent),"
            f" box: !!{MODAL}.querySelector('.nc-sheetbox'), origin: location.origin,"
            f" order: [...{MODAL}.querySelector('.nc-detail').children].map(c => c.className || c.tagName) }})")


def _drive_consent_row(tmp_path: Path, setup=None) -> dict:
    r = Rig(tmp_path)
    if setup:
        setup(r)
    title = "Write /w/a waits on you"
    row = f"[...{MODAL}.querySelectorAll('.nc-row')].find(r => r.querySelector('.nc-title').textContent === {json.dumps(title)})"
    steps = [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
             {"until": f"{MODAL}.querySelectorAll('.nc-row').length >= 5 && 1", "ms": 30000},
             {"until": f"{row} && 1", "ms": 20000}, {"eval": f"{row}.querySelector('.nc-main').click(); 1"},
             {"until": f"({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent === {json.dumps(title)} && 1"},
             {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": _sheet_probe()}]
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, *steps])
    got = _json(out)
    assert len(got) == 1, out["values"]
    got[0]["rid"] = r.req.id
    return got[0]


@needs_browser
def test_the_focus_view_of_a_held_requests_ping_links_the_sheet_on_the_pages_own_origin(tmp_path: Path) -> None:
    d = _drive_consent_row(tmp_path)
    href = f"{d['origin']}/sign/consent/{urllib.parse.quote(d['rid'], safe='')}"
    assert [(t, h, rel) for t, h, rel, _ in d["link"]] == [("Review with Face ID", href, "noopener noreferrer")], d
    assert d["link"][0][3] >= 44 and "Nothing is sent from here" in d["notes"][0], d       # the HIG hit height; no approve from the tap
    assert d["order"][:3] == ["nc-dmeta", "nc-dtitle", "nc-tools"] and d["order"][3] == "nc-sheetbox", d


@needs_browser
def test_the_focus_view_of_a_decided_request_says_so_and_links_nothing(tmp_path: Path) -> None:
    d = _drive_consent_row(tmp_path, setup=lambda r: _queue_withdraw(r))
    assert d["link"] == [] and d["box"] is True and d["notes"] == ["Already decided: this request was answered, ruled or withdrawn."], d


@needs_browser
@pytest.mark.parametrize("sheet", [{"id": "..", "state": "open"}, {"id": ".", "state": "open"}, {"id": "...", "state": "open"}],
                         ids=["dotdot", "dot", "dots"])
def test_an_id_made_only_of_dots_draws_no_sheet_box(tmp_path: Path, monkeypatch, sheet) -> None:
    """``/sign/consent/..`` would resolve as a path segment, never an id: the focus view draws nothing for it."""
    d = _drive_consent_row(tmp_path, setup=lambda r: monkeypatch.setattr(center._Reader, "consent_sheet", lambda self, ref: dict(sheet)))
    assert d["box"] is False and d["link"] == [], d


@needs_browser
@pytest.mark.parametrize("state", ["constructor", "toString", "__proto__", "hasOwnProperty"])
def test_a_state_named_like_an_inherited_property_is_not_a_known_state(tmp_path: Path, monkeypatch, state) -> None:
    """The state notes are looked up by own property: a state that is the name of something every object inherits reads as
    an unreadable one (a note, no link to the sheet from the button), never as a function's source."""
    d = _drive_consent_row(tmp_path, setup=lambda r: monkeypatch.setattr(
        center._Reader, "consent_sheet", lambda self, ref: {"id": r.req.id, "state": state}))
    assert d["link"] == [] and d["box"] is True, d
    assert len(d["notes"]) == 1 and d["notes"][0].startswith("Could not check this request") and "function" not in d["notes"][0], d


def _queue_withdraw(r) -> None:
    from datetime import datetime, timezone

    from levain.consent.queue import ConsentQueue, authority_source
    from tests._h4 import h4_home

    cq = ConsentQueue(r.q, authority=authority_source(r.q, anchor=None), home=h4_home(), clock=lambda: datetime.now(timezone.utc))
    assert cq.withdraw(r.req.id)


# --- a prompt notice's focus view says what it is (1107-32: Phill 11-07 16:58, a tap on "asks in its own dialog" showed no buttons) ---
DIALOG_TITLE = "claude_code asks in its own dialog: Bash {command: ls}"


def _dialog_probe() -> str:
    return (f"JSON.stringify({{ head: [...{MODAL}.querySelectorAll('.nc-dialogbox .nc-dialoghead')].map(x => x.textContent),"
            f" notes: [...{MODAL}.querySelectorAll('.nc-dialogbox .nc-note')].map(x => x.textContent),"
            f" signLinks: {MODAL}.querySelectorAll('.nc-detail .nc-signgo, .nc-detail .nc-sheetbox').length,"
            f" body: ({MODAL}.querySelector('.nc-detail').textContent.includes('command: ls')) }})")


def _drive_title(tmp_path: Path, title: str, setup=None) -> dict:
    r = Rig(tmp_path)
    if setup:
        setup(r)
    row = f"[...{MODAL}.querySelectorAll('.nc-row')].find(r => r.querySelector('.nc-title').textContent === {json.dumps(title)})"
    steps = [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
             {"until": f"{MODAL}.querySelectorAll('.nc-row').length >= 6 && 1", "ms": 30000},
             {"until": f"{row} && 1", "ms": 20000}, {"eval": f"{row}.querySelector('.nc-main').click(); 1"},
             {"until": f"({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent === {json.dumps(title)} && 1"},
             {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": _dialog_probe()}]
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, *steps])
    got = _json(out)
    assert len(got) == 1, out["values"]
    return got[0]


def _add_prompt(r) -> None:
    PushStore(r.q).add_ping("consent", claude_code.prompt_ref("abc123"), "levain", DIALOG_TITLE)


@needs_browser
def test_the_focus_view_of_a_prompt_notice_says_the_dialog_waits_and_offers_nothing_to_press(tmp_path: Path) -> None:
    d = _drive_title(tmp_path, DIALOG_TITLE, setup=_add_prompt)
    assert d["head"] == ["Waiting in the seat's own dialog"], d
    assert len(d["notes"]) == 1 and d["notes"][0].startswith("claude_code is asking in its own dialog. Levain did not hold this request, so there is nothing to answer here."), d
    assert d["signLinks"] == 0 and d["body"] is True, d                  # no sign link or sheet box; the raw command stays below


@needs_browser
def test_a_held_requests_focus_view_has_its_sheet_box_and_no_dialog_section(tmp_path: Path) -> None:
    d = _drive_title(tmp_path, "Write /w/a waits on you", setup=_add_prompt)
    assert d["head"] == [] and d["signLinks"] >= 1, d
