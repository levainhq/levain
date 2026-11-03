"""The notification detail view's markdown, and the push text's stripping.

The renderer is the page's own hand-written one (DOM nodes and textContent only), so these tests drive the real page in headless Chrome
over a real server on a scratch queue: hostile bodies stay text, a seat's OPTIONS/REC shape draws as blocks, and a body that is not in that
shape draws as plain markdown. The push stripping is mechanical and runs without a browser. What stays undischarged here is how it reads in
his hands on desktop, iPad and iPhone."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from levain.webpush import sender
from levain.webpush.store import PushStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_notify_center_page import (MODAL, OPEN, OPENED, PILL, Rig, _env, _json, _row, _url)  # noqa: F401  (_env: the autouse fixture)

SEAT = ("Rocket.net reply is ready to send\n\n"
        "## What happened\n\nJess answered the **hosting** question and wants a *yes or no* by Friday. See [the thread](https://mail.example/t/42) "
        "and https://example.org/plan.\n\n"
        "- she asked about `ssl` renewal\n- she attached the invoice\n\n"
        "1. confirm the plan\n2. send the reply\n\n"
        "```\nrocket plan --site 357460\nOPTIONS: (x) this is code\n```\n\n"
        "WHAT: send the reply today. OPTIONS: (a) send it as drafted · (b) hold until **Wednesday** · (c) ask Jess for the invoice "
        "REC: a (the draft already answers her question)")

HOSTILE = ("Hostile\n\n<script>window.pwnd = 1</script> <img src=x onerror=\"window.pwnd = 2\"> <b>raw</b>\n\n"
           "[js](javascript:window.pwnd=3) [data](data:text/html,<script>1</script>) [tab](java\tscript:alert(1)) [rel](/x/y)\n\n"
           "**never closed and _this [too and `this\n\n[](\n\n* * *\n\n> quoted <i>x</i>")

NOT_THE_SHAPE = {
    "No keys": "No keys here\n\nOPTIONS: do something sensible REC: fine",
    "One key": "One key here\n\nOPTIONS: (a) only one choice REC: a",
    "In a fence": "In a fence\n\n```\nOPTIONS: (a) one · (b) two REC: a\n```\n\nplain after",
}


def _add(q: Path, body: str) -> None:
    PushStore(q).add_ping("dated", None, "levain", body)


def _open(title: str) -> list:
    return [{"eval": f"{_row(title, '.nc-main')}.click(); 1"},
            {"until": f"{MODAL}.querySelector('.nc-dtitle').textContent === {json.dumps(title)} && 1"}]


def test_the_push_text_is_plain() -> None:
    """Headers, list markers, emphasis, backticks and link syntax come off the push body; the words stay; a hostile or huge body is bounded."""
    body = "## Heading\n- **bold** and *it* and `code`\n1. [link text](https://x.example/p) here\n> quoted\n```\nfenced line\n```\nsnake_case stays"
    got = json.loads(sender.build_payload("t", body, "tag", "/", 1))["body"]
    assert got == "Heading\nbold and it and code\nlink text here\nquoted\nfenced line\nsnake_case stays", got
    keep = "/var/T/3_/x_y --opt <key> a*b 2 * 3 [unclosed (x) _lone"
    assert json.loads(sender.build_payload("t", keep, "tag", "/", 1))["body"] == keep       # plain text with lone marks goes through whole
    assert json.loads(sender.build_payload("t", "<script>1</script> [a](javascript:x)", "tag", "/", 1))["body"] == "<script>1</script> a"
    started = time.monotonic()
    for hostile in ("[(" * 50000, "**" * 60000, "[" * 100000 + "](" * 10):
        out = json.loads(sender.build_payload("t", hostile, "tag", "/", 1))
        assert len(sender._dumps(out)) <= 4096 + 600
    assert time.monotonic() - started < 5.0


@needs_browser
def test_a_seats_notice_draws_headers_lists_code_a_visible_link_and_the_options_and_rec_as_blocks(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _add(r.q, SEAT)
    shots = os.environ.get("S4MD_SHOTS")
    probe = (f"JSON.stringify({{ h: [...{MODAL}.querySelectorAll('.nc-body h3,.nc-body h4')].map(x => x.textContent),"
             f" ul: {MODAL}.querySelectorAll('.nc-body ul:not(.nc-optlist) li').length, ol: {MODAL}.querySelectorAll('.nc-body ol li').length,"
             f" strong: [...{MODAL}.querySelectorAll('.nc-body strong')].map(x => x.textContent), em: [...{MODAL}.querySelectorAll('.nc-body em')].map(x => x.textContent),"
             f" pre: [...{MODAL}.querySelectorAll('.nc-body pre')].map(x => x.textContent), full: [...{MODAL}.querySelectorAll('.nc-fullurl')].map(x => x.textContent),"
             f" links: [...{MODAL}.querySelectorAll('.nc-body a')].map(a => a.getAttribute('href')),"
             f" opts: [...{MODAL}.querySelectorAll('.nc-opt')].map(o => [o.querySelector('.nc-optkey').textContent, o.querySelector('.nc-opttext').textContent, o.classList.contains('nc-opt-rec')]),"
             f" rec: ({MODAL}.querySelector('.nc-rec') || {{}}).textContent, before: !!{MODAL}.querySelector('.nc-body > h3, .nc-body > p'),"
             f" order: [...{MODAL}.querySelector('.nc-body').children].map(c => c.className || c.tagName),"
             f" sw: {MODAL}.scrollWidth <= {MODAL}.clientWidth + 1 }})")
    steps = []
    for name, emu in (("390", {"w": 390, "h": 844, "dpr": 2, "mobile": True}), ("1280", {"w": 1280, "h": 900, "dpr": 1})):
        steps += [{"emulate": emu}, {"until": f"{ROWS_}.length >= 5 && 1", "ms": 30000}, *_open("Rocket.net reply is ready to send"),
                  {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"eval": probe}]
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            steps += [{"shot": str(Path(shots) / f"detail_{name}_top.png"), "full": False},
                      {"eval": f"for (const e of [{MODAL}.querySelector('.nc-detailpane'), {MODAL}]) e.scrollTop = 99999; 1"},
                      {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"},
                      {"shot": str(Path(shots) / f"detail_{name}_end.png"), "full": False}]
        steps += [{"eval": f"{MODAL}.querySelector('.nc-detailpane').scrollTop = 0; 1"}]
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, *steps])
    for got in _json(out):
        assert got["h"] == ["What happened"] and got["ul"] == 2 and got["ol"] == 2, got
        assert got["strong"] == ["hosting", "Wednesday"] or got["strong"] == ["hosting"], got
        assert got["em"] == ["yes or no"] and got["pre"] == ["rocket plan --site 357460\nOPTIONS: (x) this is code"], got
        assert got["links"] == ["https://mail.example/t/42", "https://example.org/plan"], got
        assert got["full"] == [" (https://mail.example/t/42)"], got                         # the address shows beside text that is not the address
        assert got["opts"] == [["(a)", "send it as drafted", True], ["(b)", "hold until Wednesday", False],
                               ["(c)", "ask Jess for the invoice", False]], got
        assert got["rec"] == "Recommendationa (the draft already answers her question)", got
        assert got["order"][-2:] == ["nc-opts", "nc-rec"] and got["sw"] is True, got      # the point first, then the options, then the rec


ROWS_ = f"{MODAL}.querySelectorAll('.nc-row')"


@needs_browser
def test_hostile_and_huge_bodies_stay_text_and_a_body_off_the_shape_is_plain_markdown(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _add(r.q, HOSTILE)
    _add(r.q, "Huge\n\n" + ("**x** [y](https://a.example) `z` _w_ [[( \n" * 3000))
    for body in NOT_THE_SHAPE.values():
        _add(r.q, body)
    probe = (f"JSON.stringify({{ pwn: window.pwnd || 0, tags: {MODAL}.querySelectorAll('.nc-body script, .nc-body img, .nc-body b, .nc-body i, .nc-body iframe').length,"
             f" hrefs: [...{MODAL}.querySelectorAll('.nc-body a')].map(a => a.getAttribute('href')).slice(0, 5), links: {MODAL}.querySelectorAll('.nc-body a').length,"
             f" text: {MODAL}.querySelector('.nc-body').textContent.slice(0, 600),"
             f" onattr: [...{MODAL}.querySelectorAll('.nc-body *')].filter(e => [...e.attributes].some(a => /^on/i.test(a.name))).length,"
             f" blocks: {MODAL}.querySelectorAll('.nc-opts, .nc-rec').length }})")
    steps = []
    for title in ["Hostile", "Huge", *[b.split("\n")[0] for b in NOT_THE_SHAPE.values()]]:
        steps += [*_open(title), {"eval": probe}]
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        started = time.monotonic()
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN,
                                            {"until": f"{ROWS_}.length >= 9 && 1", "ms": 30000}, *steps])
        took = time.monotonic() - started
    hostile, huge, nokeys, one, fence = _json(out)
    assert hostile["pwn"] == 0 and hostile["tags"] == 0 and hostile["onattr"] == 0, hostile
    assert hostile["hrefs"] == [], hostile                                    # javascript:, data:, a tab-split scheme and a relative path: none is live
    assert "<script>window.pwnd = 1</script>" in hostile["text"] and "<img" in hostile["text"] and "<b>raw</b>" in hostile["text"], hostile
    assert huge["pwn"] == 0 and huge["tags"] == 0 and huge["links"] == 3000 and took < 60, (huge["tags"], huge["links"], took)
    for odd in (nokeys, one, fence):
        assert odd["blocks"] == 0, odd
    assert "OPTIONS: do something sensible" in nokeys["text"] and "OPTIONS: (a) one" in fence["text"], (nokeys, fence)
