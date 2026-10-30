"""The needs-phill card in the cockpit's Needs-you modal. flow's desk reader files each needs-phill item as a question
whose ``sign`` facet names its sign request; the focus view (questionCard) drew the question's title, a "blocks:" line
holding the stopped text, a REC line, then the request's option buttons (signFacet), and never the signed message.
Measured first on seat/1030-24-stack (98b0c0d3): no card, the stop text one run-on line.

Now, when the request's message is a card (card_format.js ``parseMessage``, the sheet's rule), the modal draws the
sheet's card in that place: headline, the signed text collapsed above the controls, Detail after the option rows. A
message the parser does not read exactly leaves the modal as it was. ``CARDFMT_SHOTS=<dir>`` keeps the shots."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import OPEN_Q, QRID, _Rig, _write_request, needs_ssh

HERE = Path(__file__).resolve().parent
ROWS = json.loads((HERE / "card_format_rows.json").read_text())
LONG = ROWS["np-ab37059b"]
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}
M = "#levain-modal .needs-question"
FACET = f"{OPEN_Q} && document.querySelector('{M} .sign-facet .sign-option')"


def _refile(rig: _Rig, message: str) -> None:
    """QRID (the rig question's sign request) filed again as the real row: its message, its options, its rec."""
    _write_request(rig.qdir, QRID, message, options=[{"key": o["key"], "text": o["text"]} for o in LONG["options"]])
    p = rig.qdir / "sign" / f"{QRID}.json"
    body = json.loads(p.read_text())
    body["recommended"] = LONG["rec"]
    p.write_text(json.dumps(body))


def _open(rig: _Rig, url: str) -> list[dict]:
    keep = "window.localStorage.setItem('levain_write_token', 'T'); "
    return [{"eval": f"(() => {{ {keep}location.href = '{rig.origin}/'; return 1; }})()"},
            {"until": FACET, "ms": 30000}]


def _shot(tmp_path: Path, name: str) -> list[dict]:
    folder = Path(os.environ.get("CARDFMT_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return [{"until": "new Promise((r) => setTimeout(() => r('settled'), 900))"},     # the modal's open fade has ended
            {"shot": str(folder / f"{os.environ.get('CARDFMT_STEM', 'consent_modal_after')}_{name}.png"), "full": False}]


def _order(a: str, b: str) -> str:
    return (f"(() => {{ const a = document.querySelector({json.dumps(a)}), b = document.querySelector({json.dumps(b)}); "
            f"return !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); }})()")


@needs_browser
@needs_ssh
def test_the_modal_draws_a_needs_phill_sign_request_as_the_card(tmp_path: Path, monkeypatch) -> None:
    """RED on 98b0c0d3: no ``.card-headline`` in the modal; the stop text was one "blocks:" line."""
    rig = _Rig(tmp_path, monkeypatch)
    _refile(rig, LONG["message"])
    with rig.serving() as (url, _h):
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, *_open(rig, url),
            {"until": f"document.querySelector('{M} .card-headline')", "ms": 15000, "may_timeout": True},
            *_shot(tmp_path, "long"),
            {"eval": f"(() => {{ const h = document.querySelector('{M} .card-headline'); return h && h.textContent; }})()"},
            {"eval": f"document.querySelector('{M} .card-signed').open"},
            {"eval": f"document.querySelector('{M} .card-detail').open"},
            {"eval": _order(f"{M} .card-signed", f"{M} .sign-actions")},
            {"eval": _order(f"{M} .sign-actions", f"{M} .card-detail")},
            {"eval": f"document.querySelector('{M} .card-signed pre').textContent"},
            {"eval": f"document.querySelectorAll('{M} .needs-q-title, {M} .needs-q-blocks, {M} .needs-rec').length"},
            {"eval": f"[...document.querySelectorAll('{M} .sign-option.question-rec')].map(b => b.textContent).join()"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
            {"eval": f"(() => {{ document.querySelector('{M} .card-detail').open = true; "
                     f"document.querySelector('{M} .card-detail').scrollIntoView(); return 1; }})()"},
            *_shot(tmp_path, "long_expanded"),
            {"eval": f"document.querySelector('{M} .card-detail').textContent"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        ])
    v = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x) and x != "settled"][3:]   # after the open and two waits
    assert v[0] == LONG["what"], v
    assert v[1] is False and v[2] is False, v
    assert v[3] is True and v[4] is True, v
    assert v[5] == LONG["message"], "the signed text is shown byte for byte"
    assert v[6] == 0, "the question's title, blocks and REC lines are replaced by the card"
    assert v[7].startswith("(a) "), v
    assert v[8] is True and v[11] is True, v
    assert "Stopped" in v[10] and "BLOCKS" in v[10], v


@needs_browser
@needs_ssh
def test_a_message_the_card_cannot_read_exactly_leaves_the_modal_as_it_was(tmp_path: Path, monkeypatch) -> None:
    """An extra signed line (``effect:``) is no card: the question's own title, blocks and REC lines stay, no card nodes."""
    rig = _Rig(tmp_path, monkeypatch)
    lines = LONG["message"].split("\n")
    _refile(rig, "\n".join(lines[:6] + ["effect: delete production data"] + lines[6:]))
    with rig.serving() as (url, _h):
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, *_open(rig, url),
            {"eval": f"document.querySelectorAll('{M} .card-head, {M} .card-detail, {M} .card-signed').length"},
            {"eval": f"document.querySelectorAll('{M} .needs-q-blocks, {M} .needs-rec').length"},
            {"eval": f"document.querySelector('{M} .needs-q-title').textContent"},
        ])
    v = out["values"][-3:]
    assert v[0] == 0 and v[1] == 2 and v[2] == "Ship the sweep fix tonight or hold it?", v


@needs_browser
@needs_ssh
def test_a_missing_card_module_leaves_the_modal_as_it_was(tmp_path: Path, monkeypatch) -> None:
    """/card_format.js not served (404): the modal draws the question's own lines and buttons, never a broken card."""
    from levain import web_server
    monkeypatch.delitem(web_server._ASSETS, "/card_format.js")
    rig = _Rig(tmp_path, monkeypatch)
    _refile(rig, LONG["message"])
    with rig.serving() as (url, _h):
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, *_open(rig, url),
            {"eval": "typeof window.LevainCardFormat"},
            {"eval": f"document.querySelectorAll('{M} .card-head, {M} .card-detail').length"},
            {"eval": f"document.querySelectorAll('{M} .needs-q-title').length"},
            {"eval": f"document.querySelectorAll('{M} .sign-option').length"},
        ])
    assert out["values"][-4:] == ["undefined", 0, 1, 3], out["values"]
