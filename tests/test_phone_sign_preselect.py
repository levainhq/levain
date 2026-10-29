"""Phone preselect (project_memory 1025-18/preselect/DESIGN.md §2): a sign link may carry ``choice=<key>``, the option
the operator tapped on his phone. The page opens on that card with a "Sign (<key>) with your screen lock" button (headless Chrome is not an iPhone; an iPhone says Face ID) and the exact
line it signs; the tap runs the same challenge as the option's own button, so the bytes signed are still built by the
server from the request file. A key the card does not offer draws nothing and says so. A linked request no longer open
says where it stands on the first listing (``GET /sign/<id>/state``).

The page half runs in headless Chrome against a real phone port on ``http://localhost:<port>``: Chrome's virtual
authenticator holds the passkey the Signer's keys name, and the ``.sig`` it writes is checked with stock
``ssh-keygen -Y verify``."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization

from levain.consent.sign import choice_line, deep_link, read_request, signed_bytes
from levain.phone_server import make_phone_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_origin import _Passkey

NS = "flow-needs-phill"
TOK = "preselect-phone-token"
RID = "np-pre00001-c9f2a1"
MESSAGE = ("flow needs-phill answer\nitem: np-pre00001\nasked: ship the sweep fix tonight or hold it?\n"
           "recommended: (b) hold for the morning\nnonce: c9f2a1")
OPTIONS = [{"key": "a", "text": "ship it tonight"}, {"key": "b", "text": "hold for the morning"}]
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
SHOTS = os.environ.get("LEVAIN_PRESELECT_SHOTS", "")      # a directory to keep the drive's screenshots in


def _file(qdir: Path, rid: str, *, options: "list[dict] | None" = OPTIONS, message: str = MESSAGE,
          expires_in: timedelta = timedelta(minutes=10), ns: str = NS, **extra: Any) -> None:
    d = qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"format": "levain-sign-request/1", "id": rid, "namespace": ns, "message": message,
                            "title": "needs phill " + rid[:11], "requester": "flow desk",
                            "expires_at": (datetime.now(timezone.utc) + expires_in).isoformat()}
    if options is not None:
        body.update(format="levain-sign-choice/1", options=options, recommended="b", **extra)
    (d / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(d / f".{rid}.tmp", d / f"{rid}.json")


class _Port:
    """A real phone port over ``qdir`` signing in NS, its sign keys the one passkey ``pk`` (the cockpit lookup the port
    does for them is replaced: the passkey's enrolment is not what is under test here)."""

    def __init__(self, qdir: Path) -> None:
        self.qdir = qdir
        self.pk = _Passkey("localhost")
        keys = {self.pk.key.fingerprint: self.pk.key}
        self.srv = make_phone_server(qdir, "localhost", port=0, token=TOK, sign_namespaces=(NS,))
        self.srv._sign_keys = lambda: dict(keys)          # what the listing names as signers
        self.srv.signer._keys = lambda: dict(keys)        # what the Signer challenges and verifies against
        self.url = f"http://localhost:{self.srv.server_address[1]}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path: str, token: "str | None" = TOK) -> "tuple[int, Any]":
        headers = {"X-Levain-Phone-Token": token} if token is not None else {}
        req = urllib.request.Request(self.url.rstrip("/") + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, body.decode(errors="replace")

    def authenticator(self) -> dict:
        pkcs8 = self.pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
        return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                                  "credentialId": base64.b64encode(b"levain-preselect-cred").decode(),
                                  "userHandle": base64.b64encode(b"levain-preselect-user").decode()}}

    def verifies(self, tmp_path: Path, rid: str, data: bytes) -> bool:
        allowed = tmp_path / "allowed"
        allowed.write_text(f'x namespaces="{NS}" {self.pk.line}\n')
        return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "x", "-n", NS, "-s",
                               str(self.qdir / "sign" / f"{rid}.sig")], input=data,
                              capture_output=True).returncode == 0


@pytest.fixture
def port(tmp_path: Path) -> Iterator[_Port]:
    (tmp_path / "signq").mkdir(mode=0o700)      # the port refuses a queue others can reach
    p = _Port(tmp_path / "signq")
    try:
        yield p
    finally:
        p.srv.shutdown()
        p.srv.server_close()


def _shot(tmp_path: Path, name: str) -> dict:
    return {"shot": str(Path(SHOTS) / name if SHOTS else tmp_path / name)}     # the viewport: what he sees on arrival


# the linked card's picked block, read off the page: what it says and what it would sign
_PICKED = ("(() => { const c = document.querySelector('.req.sign.linked'); if (!c) return null; "
           "const p = c.querySelector('.picked'); const m = c.querySelector('.pick-miss'); "
           "const t = c.querySelector('.pick-top'); "
           "return { top: t ? t.textContent : null, topAbove: !!t && !!(t.compareDocumentPosition("
           "c.querySelector('pre.display')) & 4), picked: p ? { button: p.querySelector('button').textContent, "
           "line: p.querySelector('.line').textContent, words: p.querySelector('.why').textContent } : null, "
           "miss: m ? m.textContent : null, missTop: !!m && m.previousElementSibling === c.querySelector('.req-head'), "
           "options: c.querySelectorAll('.actions .option').length, "
           "shownAbove: !!p && !!(c.querySelector('pre.display').compareDocumentPosition(p) & 4), "
           "first: document.getElementById('requests').firstChild === c }; })()")


def _legacy_link(page: str, rid: str, token: str) -> str:
    """The phone page's own link shape (phone.js reads it until OS-6 deletes the page): ``deep_link`` builds the
    cockpit sheet's now, with no token, so these page tests build the legacy one by hand."""
    from urllib.parse import quote

    return page.split("#", 1)[0] + f"#phone_token={quote(token, safe='')}&sign={rid}"


def _signed(qdir: Path, rid: str) -> "dict | None":
    f = qdir / "sign" / f"{rid}.signed.json"
    return json.loads(f.read_text()) if f.exists() else None


# --- the link --------------------------------------------------------------------------------------------------------


def test_the_deep_link_carries_his_pick_and_its_version_after_the_sheet_and_only_an_option_key() -> None:
    sha = "0123456789abcdef" + "f" * 48
    # the version alone is the base link an app adds `&choice=<key>` to; with a choice the link is that base plus it
    base = deep_link("https://h.ts.net:8443/", RID, version=sha)
    assert base == f"https://h.ts.net:8443/sign/{RID}#v=0123456789abcdef"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha[:16]) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID) == f"https://h.ts.net:8443/sign/{RID}"
    for bad in ("*", "", "B", "toolongkey", "a&sign=x", "a b"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice=bad, version=sha)
    for bad_v in ("", "0123", sha.upper(), sha[:17], "stat:" + sha, sha + "&x=1"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice="b", version=bad_v)
    with pytest.raises(ValueError):                 # a pick holds only on a version: never a choice without one
        deep_link("https://h.ts.net/", RID, choice="b")


# --- the state route -------------------------------------------------------------------------------------------------


def test_the_state_route_says_where_a_linked_request_stands_behind_the_token(port: _Port) -> None:
    q = port.qdir
    _file(q, "np-open0001-aaaa")
    _file(q, "np-sign0001-aaaa")
    (q / "sign" / "np-sign0001-aaaa.signed.json").write_text(json.dumps({"namespace": NS, "choice": "b"}) + "\n")
    _file(q, "np-deny0001-aaaa")
    (q / "sign" / "np-deny0001-aaaa.denied").write_text("{}\n")
    _file(q, "np-expd0001-aaaa", expires_in=timedelta(minutes=-1))
    _file(q, "np-othr0001-aaaa", ns="another-namespace")        # not this port's: never said to exist
    for rid, want in (("np-open0001-aaaa", {"state": "open"}), ("np-sign0001-aaaa", {"state": "signed", "choice": "b"}),
                      ("np-deny0001-aaaa", {"state": "denied"}), ("np-expd0001-aaaa", {"state": "expired"}),
                      ("np-gone0001-aaaa", {"state": "gone"}), ("np-othr0001-aaaa", {"state": "gone"})):
        status, body = port.get(f"/sign/{rid}/state")
        assert (status, body) == (200, {"id": rid, **want}), rid
    assert port.get("/sign/np-open0001-aaaa/state", token=None)[0] == 403
    assert port.get("/sign/np-open0001-aaaa/state", token="wrong")[0] == 403
    assert port.get("/sign/x/state")[0] == 404                   # not a request id
    assert port.get("/sign/np-open0001-aaaa/challenge")[0] == 404  # a POST action read by GET is no route


def test_a_signed_record_from_another_namespace_or_with_a_forged_choice_says_no_more_than_it_may(port: _Port) -> None:
    q = port.qdir
    _file(q, "np-sign0002-aaaa")
    (q / "sign" / "np-sign0002-aaaa.signed.json").write_text(json.dumps({"namespace": "another", "choice": "b"}))
    assert port.get("/sign/np-sign0002-aaaa/state")[1] == {"id": "np-sign0002-aaaa", "state": "gone"}
    _file(q, "np-sign0003-aaaa")
    (q / "sign" / "np-sign0003-aaaa.signed.json").write_text(json.dumps({"namespace": NS, "choice": "<b>x"}))
    assert port.get("/sign/np-sign0003-aaaa/state")[1] == {"id": "np-sign0003-aaaa", "state": "signed"}
    _file(q, "np-sign0004-aaaa")
    (q / "sign" / "np-sign0004-aaaa.signed.json").write_text("not json")
    assert port.get("/sign/np-sign0004-aaaa/state")[1] == {"id": "np-sign0004-aaaa", "state": "signed"}


# --- the page --------------------------------------------------------------------------------------------------------


@needs_browser
@needs_ssh
def test_his_pick_opens_as_one_sign_button_whose_tap_signs_exactly_that_option(tmp_path: Path, port: _Port) -> None:
    """RED on 0c24b6f5: the page read only ``sign=`` and drew no picked block (``picked`` null)."""
    _file(port.qdir, RID)
    for i in range(3):                       # cards that sort before it: the linked one must still come first
        _file(port.qdir, f"np-aaa0000{i}-aaaa", options=None, message=f"filler {i}")
    link = _legacy_link(port.url, RID, TOK) + f"&choice=b&v={_v(port.qdir, RID)}"   # by hand: the page is under test
    got = _drive(tmp_path, link, [
        PHONE, port.authenticator(),
        {"until": "document.querySelector('.req.sign.linked')"},
        {"eval": _PICKED}, _shot(tmp_path, "pick-b-390.png"),
        {"eval": "document.querySelector('.req.sign.linked .picked button').click(), true"},
        {"until": "/last: signed \\(b\\)/.test(document.getElementById('status').textContent)", "ms": 30000,
         "may_timeout": True},
        _shot(tmp_path, "pick-b-signed-390.png")])
    seen = next(v for v in got["values"] if isinstance(v, dict) and "miss" in v)
    want = choice_line("b", "hold for the morning")
    assert seen["picked"] == {"button": "Sign (b) with your screen lock", "line": want,
                              "words": "you picked (b) on your phone; any other option below still signs instead"}, seen
    assert seen["miss"] is None and seen["options"] == 2 and seen["shownAbove"] and seen["first"], seen
    # r1 item 5: the card's first screen says the tap carried his pick, and where its button is
    assert seen["top"] == "Your pick: (b). Read the item; Sign (b) is below it." and seen["topAbove"], seen
    challenge = [p for p in got["posts"] if p.startswith(f"/sign/{RID}/challenge ")]
    assert len(challenge) == 1 and json.loads(challenge[0].split(" ", 1)[1])["choice"] == "b", got["posts"]
    record = _signed(port.qdir, RID)
    assert record is not None and record["choice"] == "b", record
    req = read_request(port.qdir, RID)
    data = signed_bytes(req, "b")
    assert record["signed_message"].encode() == data and record["signed_message"].endswith("\n" + want)
    assert port.verifies(tmp_path, RID, data)


@needs_browser
@needs_ssh
def test_with_a_pick_another_option_still_signs_instead(tmp_path: Path, port: _Port) -> None:
    _file(port.qdir, RID)
    got = _drive(tmp_path, _legacy_link(port.url, RID, TOK) + f"&choice=b&v={_v(port.qdir, RID)}", [
        PHONE, port.authenticator(),
        {"until": "document.querySelector('.req.sign.linked .picked')"},
        {"eval": "[...document.querySelectorAll('.req.sign.linked .actions .option')]"
                 ".find((b) => b.textContent.startsWith('(a) ')).click(), true"},
        {"until": "/last: signed \\(a\\)/.test(document.getElementById('status').textContent)", "ms": 30000}])
    assert [json.loads(p.split(" ", 1)[1])["choice"] for p in got["posts"] if "/challenge " in p] == ["a"]
    record = _signed(port.qdir, RID)
    assert record is not None and record["choice"] == "a"
    assert record["signed_message"].encode() == signed_bytes(read_request(port.qdir, RID), "a")


@needs_browser
def test_a_pick_the_card_does_not_offer_draws_nothing_and_says_so(tmp_path: Path, port: _Port) -> None:
    """A forged or stale ``choice=`` (a key not on the card, the write-in's ``*``, markup, a pick on a plain request)
    never draws a sign button: the card is drawn as without a pick, with one line saying the link's option is not on
    it. Each link carries the card's current version, so the key is what is refused. Each link is opened in turn on the
    same tab (a fragment change reloads the page)."""
    _file(port.qdir, RID, write_in=True)
    plain = "np-pln00001-aaaa"
    _file(port.qdir, plain, options=None, message="I attest this.\nnonce: 41f0")
    cases = [(RID, "z"), (RID, "%2A"), (RID, "%3Cb%3E"), (plain, "a")]
    steps: list[dict] = [PHONE]
    for n, (rid, pick) in enumerate(cases):
        if n:
            steps.append({"eval": f"location.hash = 'sign={rid}&choice={pick}&v={_v(port.qdir, rid)}', true"})
        steps += [{"until": f"(() => {{ const c = document.querySelector('.req.sign.linked'); "
                            f"return c && c.querySelector('.req-head') && document.getElementById('requests')"
                            f".firstChild === c && location.hash === '' && c; }})()"},
                  {"eval": _PICKED}]
        if n == 0:
            steps.append(_shot(tmp_path, "pick-forged-390.png"))
    first = _legacy_link(port.url, cases[0][0], TOK) + "&choice=" + cases[0][1] + "&v=" + _v(port.qdir, cases[0][0])
    got = _drive(tmp_path, first, steps)
    seen = [v for v in got["values"] if isinstance(v, dict) and "miss" in v]
    assert len(seen) == 4, got["values"]
    for (rid, pick), s in zip(cases, seen):
        shown = {"z": "z", "%2A": "*", "%3Cb%3E": "<b>", "a": "a"}[pick]
        assert s["picked"] is None and s["miss"] == f"the option in the link ({shown}) is not on this card: pick one below", s
    assert seen[0]["options"] == 3 and seen[3]["options"] == 0      # two options and the write-in
    assert all(s["missTop"] for s in seen), seen                    # under the card's head, on the first screen
    assert not [p for p in got["posts"] if "/challenge " in p]


@needs_browser
def test_a_picked_link_to_a_request_no_longer_open_says_where_it_stands_on_the_first_listing(
        tmp_path: Path, port: _Port) -> None:
    """Today's rule lets a linked id go after three listings (ten seconds apart) with one sentence for every case; with
    a pick the page asks the state route and says a terminal case (signed, denied, expired) on the first. ``gone`` is
    not terminal (r1 item 3): it is said by no word of its own, and the three-listing rule runs."""
    q = port.qdir
    _file(q, "np-sign0001-aaaa")
    (q / "sign" / "np-sign0001-aaaa.signed.json").write_text(json.dumps({"namespace": NS, "choice": "b"}) + "\n")
    _file(q, "np-deny0001-aaaa")
    (q / "sign" / "np-deny0001-aaaa.denied").write_text("{}\n")
    _file(q, "np-expd0001-aaaa", expires_in=timedelta(minutes=-1))
    want = {"np-sign0001-aaaa": "the linked sign request np-sign0001-aaaa is already signed here (b)",
            "np-deny0001-aaaa": "the linked sign request np-deny0001-aaaa was denied here",
            "np-expd0001-aaaa": "the linked sign request np-expd0001-aaaa expired: a fresh link comes with its next filing"}
    steps: list[dict] = [PHONE]
    for n, (rid, words) in enumerate(want.items()):
        if n:
            steps.append({"eval": f"location.hash = 'sign={rid}&choice=b', true"})
        # well inside one poll (10 s): the first listing says it
        steps.append({"until": f"document.getElementById('status').textContent.includes({json.dumps(words)})",
                      "ms": 6000})
        if n == 0:
            steps.append(_shot(tmp_path, "pick-signed-already-390.png"))
    _drive(tmp_path, _legacy_link(port.url, "np-sign0001-aaaa", TOK) + "&choice=b", steps)


# --- L3 r1 (project_memory 1025-18/preselect/RULINGS_R1.md) -----------------------------------------------------------


def _v(qdir: Path, rid: str) -> str:
    """The version a link carries for ``rid`` as filed now: the first 16 hex of its listing ``sha256``."""
    return read_request(qdir, rid).sha256[:16]


def _asks(port: _Port, answers: "list[Any]") -> "list[str]":
    """The port's state route answers ``answers`` in turn (the last one from then on; an exception is a 500), and the
    ids it was asked for are returned, one per ask."""
    asked: list[str] = []

    def state(rid: str) -> dict:
        asked.append(rid)
        a = answers[min(len(asked), len(answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return {"id": rid, "state": a}

    port.srv.signer.state = state
    return asked


_SLEEP = {"until": "false", "may_timeout": True}        # with "ms": the drive waits that long (a poll is ten seconds)
_ALL_PICKS = ("(() => ({ picked: document.querySelectorAll('.picked').length, "
              "miss: document.querySelectorAll('.pick-miss').length, top: document.querySelectorAll('.pick-top').length, "
              "linked: document.querySelectorAll('.req.sign.linked').length, "
              "cards: document.querySelectorAll('.req.sign').length }))()")


@needs_browser
def test_a_pick_for_a_request_not_listed_draws_nothing_on_another_card_offering_that_key(
        tmp_path: Path, port: _Port) -> None:
    """r1 item 1 (deepseek HIGH): the link names an id that is not listed (here kept open by the state route, as one
    past the listing's cap), and another listed request offers the same key: no card draws the Sign shortcut, the pick
    line or the miss line, on the first listing and on the next."""
    _file(port.qdir, RID)                                           # offers (a) and (b)
    asked = _asks(port, ["open"])
    link = _legacy_link(port.url, "np-unls0001-aaaa", TOK) + "&choice=a&v=0123456789abcdef"
    got = _drive(tmp_path, link, [
        PHONE, {"until": "document.querySelector('.req.sign .req-head')"}, {"eval": _ALL_PICKS},
        _shot(tmp_path, "pick-unlisted-r1-390.png"), {**_SLEEP, "ms": 11000}, {"eval": _ALL_PICKS}])
    seen = [v for v in got["values"] if isinstance(v, dict) and "cards" in v]
    assert seen == [{"picked": 0, "miss": 0, "top": 0, "linked": 0, "cards": 1}] * 2, seen
    assert asked and set(asked) == {"np-unls0001-aaaa"}, asked


@needs_browser
def test_a_pick_made_on_an_earlier_version_of_the_item_draws_no_shortcut(tmp_path: Path, port: _Port) -> None:
    """r1 item 2 (codex MED 1): the pick is bound to the request version it was tapped on (``v=``, the first 16 hex of
    the listing's sha256). A same-id refile where (b) now means something else opens with no shortcut and one line;
    a pick link carrying no version draws no shortcut either (fail closed)."""
    _file(port.qdir, RID)
    old = _v(port.qdir, RID)
    _file(port.qdir, RID, options=[{"key": "a", "text": "ship it tonight"}, {"key": "b", "text": "delete the branch"}])
    assert _v(port.qdir, RID) != old
    steps: list[dict] = [PHONE]
    for n, frag in enumerate((f"choice=b&v={old}", "choice=b")):
        if n:
            steps.append({"eval": f"location.hash = 'sign={RID}&{frag}', true"})
        steps += [{"until": "(() => { const c = document.querySelector('.req.sign.linked'); "
                            "return c && c.querySelector('.req-head') && location.hash === '' && c; })()"},
                  {"eval": _PICKED}]
        if n == 0:
            steps.append(_shot(tmp_path, "pick-stale-version-r1-390.png"))
    got = _drive(tmp_path, _legacy_link(port.url, RID, TOK) + f"&choice=b&v={old}", steps)
    seen = [v for v in got["values"] if isinstance(v, dict) and "miss" in v]
    assert len(seen) == 2, got["values"]
    assert seen[0]["picked"] is None and seen[0]["miss"] == "this item changed since your tap: read it and pick again", seen
    assert seen[1]["picked"] is None and seen[1]["miss"] == \
        "the link does not say which version of this item you tapped: read it and pick below", seen
    assert seen[0]["options"] == 2 and seen[1]["options"] == 2
    # head ruling on r1: the line is the card's first block under its head, so the first screen says the tap was not carried
    assert seen[0]["missTop"] and seen[1]["missTop"], seen


@needs_browser
def test_only_a_terminal_state_lets_a_picked_link_go_and_a_failed_or_open_answer_is_asked_again(
        tmp_path: Path, port: _Port) -> None:
    """r1 item 3 (complement MED 1 + codex MED 2): the state route answers a 500, then ``gone``, then ``signed``: the
    page asks again on each listing, says nothing for the first two (``gone`` keeps the three-listing rule), and says
    the terminal one when it comes."""
    asked = _asks(port, [RuntimeError("a passing fault"), "gone", "signed"])
    rid = "np-late0001-aaaa"
    words = f"the linked sign request {rid} is already signed here"
    got = _drive(tmp_path, _legacy_link(port.url, rid, TOK) + f"&choice=b&v=0123456789abcdef", [
        PHONE, {"until": "document.getElementById('status').textContent.includes('waiting on you')"},
        {**_SLEEP, "ms": 2000}, {"eval": "document.getElementById('status').textContent"},
        {"until": f"document.getElementById('status').textContent.includes({json.dumps(words)})", "ms": 30000},
        {"eval": "document.getElementById('status').textContent"}])
    first, last = [v for v in got["values"] if isinstance(v, str)]
    assert "linked sign request" not in first, first
    assert "replaced or withdrawn" not in last and words in last, last
    assert asked == [rid] * 3, asked


@needs_browser
def test_a_picked_link_asks_nothing_while_the_sign_side_has_a_fault(tmp_path: Path, port: _Port) -> None:
    """r1 item 3: while the listing carries ``sign_note`` (here: no passkey enrolled), the state route is not asked."""
    asked = _asks(port, ["signed"])
    port.srv._sign_keys = lambda: {}
    _drive(tmp_path, _legacy_link(port.url, "np-flt00001-aaaa", TOK) + "&choice=b&v=0123456789abcdef", [
        PHONE, {"until": "document.getElementById('status').textContent.includes('waiting on you')"},
        {**_SLEEP, "ms": 11000}])
    assert asked == [], asked


def test_the_state_reads_the_marker_once(port: _Port, monkeypatch: pytest.MonkeyPatch) -> None:
    """r1 item 4 (deepseek/codex LOW): one ``state_of`` read decides the answer (two reads could straddle a sign)."""
    import levain.consent.sign as sign

    _file(port.qdir, "np-race0001-aaaa")
    seen: list[str] = []
    real = sign.state_of

    def once(qdir: Path, rid: str) -> str:
        seen.append(rid)
        return real(qdir, rid)

    monkeypatch.setattr(sign, "state_of", once)
    assert port.srv.signer.state("np-race0001-aaaa") == {"id": "np-race0001-aaaa", "state": "open"}
    assert seen == ["np-race0001-aaaa"], seen


@needs_browser
def test_a_bare_choice_fragment_is_stripped_and_does_not_reload(tmp_path: Path, port: _Port) -> None:
    """r1 item 4 (complement LOW 4): ``#choice=`` with no ``sign=`` is never a pick: it leaves the address bar and the
    page is not reloaded."""
    got = _drive(tmp_path, port.url + f"#phone_token={TOK}", [
        PHONE, {"until": "document.getElementById('status').textContent.includes('waiting on you')"},
        {"eval": "window.__kept = 1, location.hash = 'choice=b', true"},
        {"until": "location.hash === ''", "ms": 3000, "may_timeout": True},
        {**_SLEEP, "ms": 1500}, {"eval": "[location.hash, window.__kept === 1]"}])
    assert got["values"][-1] == ["", True], got["values"]


def test_a_signed_request_whose_record_and_file_cannot_be_read_is_gone_which_the_page_never_words(port: _Port) -> None:
    """r1 item 4 (complement LOW 2): the marker says signed, its record is unreadable and the request file is gone:
    the route answers ``gone`` (it says no more than it can read), and ``gone`` has no words of its own on the page:
    the three-listing rule's sentence, which names no cause, is what he sees."""
    _file(port.qdir, "np-lost0001-aaaa")
    (port.qdir / "sign" / "np-lost0001-aaaa.signed.json").write_text("not json")
    (port.qdir / "sign" / "np-lost0001-aaaa.json").unlink()
    assert port.get("/sign/np-lost0001-aaaa/state")[1] == {"id": "np-lost0001-aaaa", "state": "gone"}


@needs_browser
def test_the_app_built_link_sign_v_choice_draws_the_shortcut_and_v_alone_draws_no_pick(tmp_path: Path, port: _Port) -> None:
    """Cross-repo contract (head, 10-25): the ping's link is ``sign=<id>&v=<16 hex>`` and the app appends
    ``&choice=<key>`` after it, so the page reads the fragment's keys in any order. ``v=`` alone is a plain card: no
    shortcut, no pick line, no miss line."""
    _file(port.qdir, RID)
    base = _legacy_link(port.url, RID, TOK) + f"&v={_v(port.qdir, RID)}"     # by hand: the page is under test here
    steps: list[dict] = [PHONE]
    for n, frag in enumerate((f"sign={RID}&v={_v(port.qdir, RID)}&choice=b", f"sign={RID}&v={_v(port.qdir, RID)}")):
        if n:
            steps.append({"eval": f"location.hash = '{frag}', true"})
        steps += [{"until": "(() => { const c = document.querySelector('.req.sign.linked'); "
                            "return c && c.querySelector('.req-head') && location.hash === '' && c; })()"},
                  {"eval": _PICKED}]
    got = _drive(tmp_path, base + "&choice=b", steps)
    seen = [v for v in got["values"] if isinstance(v, dict) and "miss" in v]
    assert len(seen) == 2, got["values"]
    assert seen[0]["picked"] is not None and seen[0]["picked"]["button"] == "Sign (b) with your screen lock", seen
    assert seen[0]["top"] == "Your pick: (b). Read the item; Sign (b) is below it." and seen[0]["miss"] is None, seen
    assert seen[1]["picked"] is None and seen[1]["top"] is None and seen[1]["miss"] is None, seen
    assert seen[1]["options"] == 2 and seen[1]["first"], seen


@needs_browser
def test_the_linked_sign_card_stays_first_when_the_needs_you_band_is_drawn_too(tmp_path: Path, port: _Port) -> None:
    """A sign link he followed lands on THAT card at the top: the band card (drawn while a question is open) follows it,
    it never displaces it."""
    from levain.consent.questions import QuestionStore

    _file(port.qdir, RID)
    QuestionStore(port.qdir).file(agent="claude_code", agent_session_id="s1", what="Ship it?",
                                  options=[("yes", "Ship"), ("no", "Hold")], rec="yes", blocks="the release", cwd="/w")
    link = _legacy_link(port.url, RID, TOK) + f"&v={_v(port.qdir, RID)}"
    order = ("(() => { const k = [...document.getElementById('requests').children]; "
             "const b = k.findIndex((c) => c.matches('.req.band')); "
             "return { band: b, linked: k.findIndex((c) => c.matches('.req.sign.linked')) }; })()")
    got = _drive(tmp_path, link, [PHONE, {"until": "document.querySelector('.req.sign.linked')"},
                                  {"until": "document.querySelector('.req.band')"}, {"eval": order}])
    seen = next(v for v in got["values"] if isinstance(v, dict) and "band" in v)
    assert seen == {"band": 1, "linked": 0}, seen
