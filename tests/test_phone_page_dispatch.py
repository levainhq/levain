"""The phone page's dispatch card, in headless Chrome at a phone's width (390).

The card shows what the operator signs (the goal in full, the facts, the digest, the read and write lists behind
"details" with their counts on the closed card) and offers Sign and Cancel at equal weight. Then the whole ceremony runs
through the page: Chrome's virtual authenticator holds the SAME software passkey the cockpit enrolled, so the page's
own challenge, ``navigator.credentials.get`` and answer reach the job store, which verifies with ssh-keygen."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from levain.dispatch.jobs import JobStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_dispatch_phone import file_job, repo, world  # noqa: F401, F811  (fixtures)
from tests.test_phone_dispatch import _serve
from tests.test_phone_server import _TOK

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

CARD = "document.querySelector('article.dispatch')"

PROBE = """(() => {
  const c = document.querySelector('article.dispatch');
  const det = c.querySelector('details');
  const btn = [...c.querySelectorAll('.acts button')];
  const small = [...document.querySelectorAll('button, summary')].filter(b => b.offsetParent !== null && b.getBoundingClientRect().height < 44).length;
  return { cls: c.className, title: c.querySelector('.title').textContent,
           chips: [...c.querySelectorAll('.chips .chip')].map(x => x.textContent),
           goal: c.querySelector('pre.display').textContent, facts: c.querySelector('.facts').textContent,
           digest: c.querySelector('.dg-val').title, summary: det.querySelector('summary').textContent, open: det.open,
           paths: [...det.querySelectorAll('li')].map(x => x.textContent),
           buttons: btn.map(b => ({ text: b.textContent, cls: b.className, w: Math.round(b.getBoundingClientRect().width),
                                    h: Math.round(b.getBoundingClientRect().height), disabled: b.disabled })),
           sideways: document.documentElement.scrollWidth > innerWidth, small: small };
})()"""


@needs_browser
def test_the_card_shows_what_is_signed_and_offers_sign_and_cancel_at_equal_weight(world, tmp_path: Path) -> None:
    job = file_job(world)
    c = job.contract
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": CARD + " && 1"},
            {"eval": PROBE},
        ])
    got = out["values"][-1]
    assert "row" in got["cls"].split() and "dispatch" in got["cls"].split()
    assert got["chips"][0] == "dispatch" and "claude_code" in got["chips"]
    assert any(ch.startswith("lapses ") for ch in got["chips"])                 # unsigned: its lease is its expiry
    assert got["goal"] == c.goal                                                 # in full, tabs and lines kept
    for fact in ("fix-lint", "R0", "glm-5, kimi-k2 (k=2)", "40 tool calls", "200000 tokens", "150 cents", "black-box"):
        assert fact in got["facts"], fact
    assert got["digest"] == job.digest
    assert got["open"] is False and f"reads {len(c.read)}" in got["summary"] and f"writes {len(c.write)}" in got["summary"]
    assert any("src/new.py" in p and "new file" in p for p in got["paths"])
    texts = [b["text"] for b in got["buttons"]]
    assert texts == ["Sign with passkey", "Cancel with passkey"]
    a, b = got["buttons"]
    assert a["cls"] == b["cls"] and abs(a["w"] - b["w"]) <= 1 and a["h"] == b["h"]   # equal weight: no recommendation
    assert not got["sideways"] and got["small"] == 0


def _authenticator(key) -> dict:
    pkcs8 = key.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption())
    return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                              "credentialId": base64.b64encode(b"levain-test-cred").decode(),
                              "userHandle": base64.b64encode(b"levain-test-user").decode()}}


NOTE = "document.querySelector('article.dispatch .note').textContent"


@needs_browser
def test_the_page_signs_then_cancels_a_job_with_the_passkey(world, tmp_path: Path) -> None:
    job = file_job(world)
    store = JobStore(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            _authenticator(world["phone"]),
            {"until": CARD + " && 1"},
            {"eval": "[...document.querySelectorAll('article.dispatch .acts button')].find(b => /^Sign/.test(b.textContent)).click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}"},
            # the card is redrawn signed: Sign is gone, Cancel stays
            {"until": "document.querySelector('article.dispatch') && ![...document.querySelectorAll('article.dispatch .acts button')].some(b => /^Sign/.test(b.textContent)) && 1", "ms": 8000},
            {"eval": "[...document.querySelectorAll('article.dispatch .acts button')].find(b => /^Cancel/.test(b.textContent)).click() || 1"},
            {"until": f"/^cancelled/.test({NOTE}) && {NOTE}"},
        ])
    signed, cancelled = out["values"][2], out["values"][5]
    assert isinstance(signed, str) and world["phone"].fp in signed, out
    assert isinstance(cancelled, str) and cancelled.startswith("cancelled"), out
    routes = [p.split(" ", 1)[0] for p in out["posts"]]
    assert routes == [f"/dispatch/{job.id}/challenge", f"/dispatch/{job.id}/answer",
                      f"/dispatch/{job.id}/cancel-challenge", f"/dispatch/{job.id}/cancel"], routes
    final = store.get(job.id)
    assert final.state == "cancelled" and final.signer_fp == world["phone"].fp
