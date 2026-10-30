"""The needs-phill card on the phone app (the tailnet bare origin serves phone.html + phone_app.js + phone.js; the
sheet's card_format.js was served only beside /sign/<id>, so the surface he signs from drew the signed message as one
solid block). Measured first on seat/1030-24-stack (98b0c0d3): GET /card_format.js on the phone port was 404 and the
card was one ``pre.display``.

The rule is card_format.js's own (``parseMessage``): a card only for a message in flow's verifier grammar whose request
agrees; anything else is the message whole and visible as before. ``CARDFMT_SHOTS=<dir>`` keeps the 390 px shots."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_phone_sign_preselect import NS, TOK, _Port, needs_ssh  # noqa: F401  (NS: the port's namespace)

HERE = Path(__file__).resolve().parent
ROWS = json.loads((HERE / "card_format_rows.json").read_text())
LONG, SHORT = ROWS["np-ab37059b"], ROWS["np-b70bec0b"]
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}
CARD = "document.querySelector('.req.sign')"


@pytest.fixture
def port(tmp_path: Path):
    (tmp_path / "signq").mkdir(mode=0o700)      # the port refuses a queue others can reach
    p = _Port(tmp_path / "signq")
    try:
        yield p
    finally:
        p.srv.shutdown()
        p.srv.server_close()


def _file(port: _Port, row: dict, **over) -> str:
    """One open choice request carrying the row's message, options and recommendation, as flow files it."""
    rid = over.pop("id", row["id"]) + "-c9f2a1"
    d = port.qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone
    body = {"format": "levain-sign-choice/1", "id": rid, "namespace": NS, "message": over.get("message", row["message"]),
            "title": "needs phill " + row["id"], "requester": "flow desk",
            "options": [{"key": o["key"], "text": o["text"]} for o in row["options"]],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    rec = over.get("rec", row.get("rec"))
    if rec is not None:
        body["recommended"] = rec
    (d / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(d / f".{rid}.tmp", d / f"{rid}.json")
    return rid


def _page(port: _Port) -> str:
    return port.url + f"#phone_token={TOK}"


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            return res.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_phone_port_serves_the_card_parser_byte_for_byte_and_the_shell_loads_it_before_phone_js(port: _Port) -> None:
    """RED on 98b0c0d3: 404, and phone.html named no /card_format.js."""
    with urllib.request.urlopen(port.url + "card_format.js", timeout=10) as res:
        body, ctype = res.read(), res.headers.get("Content-Type")
    src = HERE.parent / "levain" / "templates" / "web"
    assert body == (src / "card_format.js").read_bytes()
    assert ctype == "text/javascript; charset=utf-8"
    shell = (src / "phone.html").read_text()
    assert shell.index('<script src="/card_format.js"></script>') < shell.index('<script src="/phone.js"></script>')


def _order(a: str, b: str) -> str:
    return (f"(() => {{ const a = document.querySelector({json.dumps(a)}), b = document.querySelector({json.dumps(b)}); "
            f"return !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); }})()")


def _shot(tmp_path: Path, name: str) -> list[dict]:
    folder = Path(os.environ.get("CARDFMT_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return [{"shot": str(folder / f"{os.environ.get('CARDFMT_STEM', 'phone_after')}_{name}.png"), "full": True}]


@needs_browser
@needs_ssh
def test_a_needs_phill_card_on_the_phone_is_headline_rows_then_collapsed_detail_signed_text_above_the_controls(
        tmp_path: Path, port: _Port) -> None:
    """RED on 98b0c0d3: the card was one ``pre.display`` holding the whole message, no headline."""
    _file(port, LONG)
    out = _drive(tmp_path, _page(port), [
        PHONE, port.authenticator(),
        {"until": CARD},
        *_shot(tmp_path, "long"),
        {"eval": "(() => { const h = document.querySelector('.req.sign .card-headline'); return h && h.textContent; })()"},
        {"eval": "document.querySelector('.req.sign .card-detail').open"},
        {"eval": "document.querySelector('.req.sign .card-signed').open"},
        {"eval": _order(".req.sign .card-headline", ".req.sign .actions")},
        {"eval": _order(".req.sign .card-signed", ".req.sign .actions")},
        {"eval": _order(".req.sign .actions", ".req.sign .card-detail")},
        {"eval": "document.querySelector('.req.sign .card-signed pre.display').textContent"},
        {"eval": "[...document.querySelectorAll('.req.sign .actions .option')].map(b => b.textContent).join('|')"},
        {"eval": "[...document.querySelectorAll('.req.sign .actions .option.recommended')].length"},
        {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        {"eval": "document.querySelectorAll('.req.sign > pre.display').length"},
        {"eval": "(() => { document.querySelector('.req.sign .card-detail').open = true; return 1; })()"},
        *_shot(tmp_path, "long_expanded"),
        {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        {"eval": "document.querySelector('.req.sign .card-detail').textContent"},
    ])
    v = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)][1:]
    assert v[0] == LONG["what"], v
    assert v[1] is False and v[2] is False, v                 # Detail and the signed text start collapsed
    assert v[3] is True and v[4] is True and v[5] is True, v
    assert v[6] == LONG["message"], "the signed text is shown byte for byte"
    assert v[7] == "|".join(f"({o['key']}) {o['text']}" + ("recommended" if o["key"] == LONG["rec"] else "")
                            for o in LONG["options"]), v[7]
    assert v[8] == 1 and v[9] is True and v[10] == 0, v
    assert v[12] is True and "Stopped" in v[13] and "BLOCKS" in v[13], v


@needs_browser
@needs_ssh
@pytest.mark.parametrize("case", ["effect_line", "rec_disagrees", "not_needs_phill"])
def test_a_message_the_card_cannot_read_exactly_stays_one_visible_block_on_the_phone(
        case: str, tmp_path: Path, port: _Port) -> None:
    """An extra signed line (``effect:``), a JSON rec that disagrees with the signed one, and a plain message are each
    the pre-card draw: the whole message in one visible ``pre.display``, no card nodes."""
    lines = SHORT["message"].split("\n")
    over = {"effect_line": {"message": "\n".join(lines[:6] + ["effect: delete production data"] + lines[6:])},
            "rec_disagrees": {"rec": "b"},
            "not_needs_phill": {"message": "tool: Write\nfile: /w/a.txt\neffect: writes 2 bytes\nnonce: 9a1f"}}[case]
    # a choice request carries 2 to 8 options; (a) is the row's signed recommendation, word for word
    row = dict(SHORT, options=[{"key": "a", "text": "sign the note '- Phill', keep flow's byline on the essay"},
                               {"key": "b", "text": "leave it unsigned"}])
    _file(port, row, **over)
    want = over.get("message", row["message"])
    out = _drive(tmp_path, _page(port), [
        PHONE, {"until": CARD},
        {"eval": "document.querySelectorAll('.req.sign .card-head, .req.sign .card-detail, .req.sign .card-signed').length"},
        {"eval": "(() => { const p = document.querySelector('.req.sign > pre.display'); return p && p.textContent; })()"},
        {"eval": _order(".req.sign > pre.display", ".req.sign .actions")},
        {"eval": "getComputedStyle(document.querySelector('.req.sign > pre.display')).whiteSpace"},
        {"eval": "document.querySelector('.req.sign').innerText"},
    ])
    v = out["values"][1:]
    assert v[0] == 0 and v[1] == want and v[2] is True and v[3] == "pre-wrap", v
    assert "refused" not in v[4], v[4]


@needs_browser
@needs_ssh
def test_the_same_short_row_with_its_two_options_is_a_card(tmp_path: Path, port: _Port) -> None:
    """The control for the cases above: the unaltered row with the same two options draws a card, so each case above is
    no card because of what it changed, not because of the request around it."""
    _file(port, dict(SHORT, options=[{"key": "a", "text": "sign the note '- Phill', keep flow's byline on the essay"},
                                     {"key": "b", "text": "leave it unsigned"}]))
    out = _drive(tmp_path, _page(port), [
        PHONE, {"until": CARD}, *_shot(tmp_path, "short"),
        {"eval": "(() => { const h = document.querySelector('.req.sign .card-headline'); return h && h.textContent; })()"},
        {"eval": "document.querySelectorAll('.req.sign > pre.display').length"},
    ])
    # the headline is the question to its first sentence end; the rest is in Detail
    assert out["values"][-2:] == ["Cost-parity page: the Oct 4 correction note is in your voice on a page bylined flow.", 0], \
        out["values"]


@needs_browser
@needs_ssh
def test_a_missing_card_module_falls_back_to_the_whole_message(tmp_path: Path, port: _Port, monkeypatch) -> None:
    """The parser not served (a 404: an older port, a dropped route): the card draws the message whole, as before."""
    from levain import phone_server
    monkeypatch.delitem(phone_server._STATIC, "/card_format.js")
    assert _status(port.url + "card_format.js") == 404
    _file(port, LONG)
    out = _drive(tmp_path, _page(port), [
        PHONE, {"until": CARD},
        {"eval": "typeof window.LevainCardFormat"},
        {"eval": "document.querySelectorAll('.req.sign .card-head, .req.sign .card-detail').length"},
        {"eval": "document.querySelector('.req.sign > pre.display').textContent"},
    ])
    assert out["values"][-3:] == ["undefined", 0, LONG["message"]], out["values"]
