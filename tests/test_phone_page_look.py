"""The phone consent page's look (cockpit-matched) must not change what the page shows about a request.

Driven in headless Chrome against the served phone port at a phone's width (390): the request text as filed is fully
in the DOM, no CSS clips it (no ellipsis, no line clamp, no height cap, no scroll box), nothing scrolls sideways, and
every tap target is at least 44px. Request data (agent, tool, where, display) is shown as filed: no case transform, no
clipping. The page is read in both colour schemes (the phone's setting, emulated) and the card must differ between them."""

from __future__ import annotations

import json
import shutil
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from levain.authority import anchor_path
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_phone_server import _TOK, _serving, world  # noqa: F401  (world is a fixture)
from tests._h4 import h4_home

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")  # as test_phone_server

LONG = "x" * 40 + "/very/long/path/with/no/spaces/" * 6 + "\nline two of the request " + "word " * 80


def _file_long(world) -> str:
    q = ConsentQueue(world["qdir"], authority=authority_source(world["root"] / "cockpit.db", anchor=anchor_path(world["root"])), home=h4_home())
    return q.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                  tool_input={"file_path": "/w/" + "d/" * 30 + "f.txt", "content": LONG}, cwd="/w",
                  hold=timedelta(minutes=5), tier="T2", event_id="look-T2-1").id


PROBE = """(() => {
  const d = document.querySelector('article.req .display'); const cs = getComputedStyle(d);
  const small = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null && b.getBoundingClientRect().height < 44).length;
  return { text: d.textContent, textOverflow: cs.textOverflow, clamp: cs.webkitLineClamp, maxHeight: cs.maxHeight,
           overflowY: cs.overflowY, whiteSpace: cs.whiteSpace, fits: d.scrollHeight <= d.clientHeight + 1,
           sideways: document.documentElement.scrollWidth > innerWidth, small: small, width: innerWidth };
})()"""


@needs_browser
def test_request_text_is_whole_and_unclipped_at_390(world, tmp_path: Path) -> None:
    rid = _file_long(world)
    with _serving(world["qdir"]) as (url, _httpd):
        req = urllib.request.Request(url + "/consent.json", headers={"X-Levain-Phone-Token": _TOK})
        shown = next(r["display"] for r in json.loads(urllib.request.urlopen(req, timeout=20).read())["pending"] if r["id"] == rid)
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('article.req .display') && 1"},
            {"eval": PROBE},
        ])
    got = out["values"][-1]
    assert got["text"] == shown, "the page must show the request text exactly as the server filed it"
    assert len(shown) > 400
    assert got["textOverflow"] == "clip" and got["clamp"] in ("none", ""), got
    assert got["maxHeight"] == "none" and got["overflowY"] in ("visible", "hidden") and got["fits"], got
    assert got["whiteSpace"] == "pre-wrap"
    assert not got["sideways"], "the page scrolls sideways at 390"
    assert got["small"] == 0, "a tap target is under 44px"


# request data: what is filed must be what is drawn, byte for byte (innerText applies text-transform, so it catches a
# CSS uppercase that textContent would not)
DATA_PROBE = """(() => {
  const card = document.querySelector('article.req'); const wheres = [...card.querySelectorAll('.where')];
  const pick = (n) => { const cs = getComputedStyle(n);
    return { text: n.innerText, transform: cs.textTransform, textOverflow: cs.textOverflow, clamp: cs.webkitLineClamp,
             clipped: n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1,
             overflow: cs.overflowX + '/' + cs.overflowY }; };
  return { meta: pick(card.querySelector('.req-head .meta')), tool: pick(card.querySelector('.tool')),
           display: pick(card.querySelector('.display')), where: wheres.map(pick),
           card: getComputedStyle(card).backgroundColor, page: getComputedStyle(document.body).backgroundColor };
})()"""


@needs_browser
def test_request_data_is_shown_as_filed_in_both_colour_schemes(world, tmp_path: Path) -> None:
    rid = _file_long(world)
    with _serving(world["qdir"]) as (url, _httpd):
        req = urllib.request.Request(url + "/consent.json", headers={"X-Levain-Phone-Token": _TOK})
        row = next(r for r in json.loads(urllib.request.urlopen(req, timeout=20).read())["pending"] if r["id"] == rid)
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('article.req .display') && 1"},
            {"scheme": "light"}, {"eval": DATA_PROBE},
            {"scheme": "dark"}, {"eval": DATA_PROBE},
        ])
    light, dark = out["values"][-2], out["values"][-1]
    assert row["agent"] == "claude_code" and row["tool"] == "Write" and row["cwd"] == "/w"
    assert row.get("targets"), "the fixture request must touch a path, or the second where line is never drawn"
    for scheme, got in (("light", light), ("dark", dark)):
        for name, want in (("meta", row["agent"]), ("tool", row["tool"]), ("display", row["display"])):
            assert got[name]["transform"] == "none", (scheme, name, got[name])
            assert got[name]["text"] == want, (scheme, name, "shown differently from the server's value", got[name]["text"], want)
            assert not got[name]["clipped"] and got[name]["textOverflow"] == "clip" and got[name]["clamp"] in ("none", ""), (scheme, name, got[name])
        assert got["where"], (scheme, "the request's where lines are missing")
        want_where = ["in " + row["cwd"]] + (["touches " + str(row["targets"])] if row.get("targets") else [])
        assert [w["text"] for w in got["where"]] == want_where, (scheme, got["where"], want_where)
        for w in got["where"]:
            assert w["transform"] == "none" and not w["clipped"], (scheme, w)
    assert light["card"] != dark["card"], "the card drew the same in both colour schemes: the dark block is missing"
    assert light["page"] != dark["page"]
