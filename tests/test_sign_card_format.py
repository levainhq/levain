"""The sign sheet's card format (Phill 10-30: the consent/sign card was one solid block of text; "they need formatting and
space"). The parser is one pure file, ``levain/templates/web/card_format.js``, ported from flowConnect's ``NPCard.swift``
(the same spec he approved on glass, "8a"). Three things are pinned here:

* the parser never changes a word: ``tests/card_format_check.js`` runs it under node against two REAL needs-phill rows
  (``tests/card_format_rows.json``: np-ab37059b long, np-b70bec0b short) and compares the words in with the words out;
* the sheet draws a needs-phill card as headline, then the option rows, then Detail (collapsed), while the signed text,
  the signer line and the controls keep their place and behaviour;
* a message that is not a needs-phill card (a consent request's effect block, a plain sign request) is NOT re-cut: its
  text stays one visible block above the controls.

``CARDFMT_SHOTS=<dir>`` makes ``test_the_card_shots`` save the 390 px screenshots the review reads."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import NS, STRIP_FACET, TOK, _Rig, needs_ssh  # noqa: F401  (STRIP_FACET: the rig's door)

HERE = Path(__file__).resolve().parent
ROWS = json.loads((HERE / "card_format_rows.json").read_text())
LONG, SHORT = ROWS["np-ab37059b"], ROWS["np-b70bec0b"]
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}
STATE = "document.getElementById('sheet').dataset.state"


def _file_card(rig: _Rig, row: dict[str, Any]) -> str:
    """One open choice request carrying the row's message, options and recommendation, as flow files it."""
    rid = row["id"]
    d = rig.qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    body = {"format": "levain-sign-choice/1", "id": rid, "namespace": NS, "message": row["message"], "title": "t",
            "requester": "flow", "options": [{"key": o["key"], "text": o["text"]} for o in row["options"]],
            "recommended": row["rec"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
    (d / f".{rid}.tmp").write_text(json.dumps(body))
    (d / f".{rid}.tmp").replace(d / f"{rid}.json")
    return rid


def _open(rig: _Rig, rid: str) -> list[dict]:
    keep = f"window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
    return [{"eval": f"(() => {{ {keep}location.href = '/sign/{rid}'; return 1; }})()"},
            {"until": f"location.pathname === '/sign/{rid}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


def test_the_parser_never_changes_a_word_on_real_rows() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    out = subprocess.run([node, str(HERE / "card_format_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ALL PASS" in out.stdout, out.stdout


@needs_browser
@needs_ssh
def test_the_card_shots(tmp_path: Path, monkeypatch) -> None:
    shots = os.environ.get("CARDFMT_SHOTS")
    if not shots:
        pytest.skip("CARDFMT_SHOTS names the folder the screenshots go to")
    folder = Path(shots)
    folder.mkdir(parents=True, exist_ok=True)
    stem = os.environ.get("CARDFMT_STEM", "after")
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, LONG)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *_open(rig, rid),
            {"shot": str(folder / f"{stem}_long.png"), "full": True},
            {"eval": "(() => { const d = document.getElementById('sheet-detail'); if (d) d.open = true; return 1; })()"},
            {"shot": str(folder / f"{stem}_long_expanded.png"), "full": True},
        ])
    assert out["values"], out


def _order(a: str, b: str) -> str:
    """true when element a comes before element b in the document (a's selector, b's selector)."""
    return (f"(() => {{ const a = document.querySelector({json.dumps(a)}), b = document.querySelector({json.dumps(b)}); "
            f"return !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); }})()")


@needs_browser
@needs_ssh
def test_a_card_is_headline_then_rows_then_collapsed_detail_and_the_signed_text_stays_above_the_controls(
        tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, LONG)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *_open(rig, rid),
            {"eval": "document.getElementById('sheet-headline').textContent"},
            {"eval": "document.getElementById('sheet-detail').open"},
            {"eval": "document.getElementById('sheet-signed').open"},
            {"eval": _order("#sheet-headline", "#sheet-actions")},
            {"eval": _order("#sheet-signed", "#sheet-actions")},          # the signed text is above the controls
            {"eval": _order("#sheet-actions", "#sheet-detail")},          # Detail follows the option rows
            {"eval": "document.querySelector('#sheet-signed .sheet-message').textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice + ':' + b.textContent).join('|')"},
            {"eval": "document.querySelector('#sheet-actions .sheet-rec').dataset.choice"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        ])
    v = out["values"][2:]
    assert v[0] == LONG["what"], v
    assert v[1] is False and v[2] is False, v                            # Detail and the signed text start collapsed
    assert v[3] is True and v[4] is True and v[5] is True, v
    assert v[6] == LONG["message"], "the signed text is shown byte for byte"
    assert v[7] == "|".join(f"{o['key']}:({o['key']}) {o['text']}" for o in LONG["options"]), v[7]
    assert v[8] == LONG["rec"] and v[9] is True, v


@needs_browser
@needs_ssh
def test_a_message_that_is_not_a_card_stays_one_visible_block_above_the_controls(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    plain = dict(LONG, id="np-plain-0001", message="tool: Write\nfile: /w/a.txt\neffect: writes 2 bytes\nnonce: 9a1f")
    rid = _file_card(rig, plain)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *_open(rig, rid),
            {"eval": "!!document.getElementById('sheet-headline') || !!document.getElementById('sheet-detail')"},
            {"eval": _order("#sheet-body > .sheet-message", "#sheet-actions")},
            {"eval": "document.querySelector('#sheet-body > .sheet-message').textContent"},
            {"eval": "getComputedStyle(document.querySelector('#sheet-body > .sheet-message')).whiteSpace"},
        ])
    v = out["values"][2:]
    assert v[0] is False and v[1] is True, v
    assert v[2] == plain["message"] and v[3] == "pre-wrap", v
