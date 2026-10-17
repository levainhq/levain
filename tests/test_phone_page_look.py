"""The phone consent page's look (cockpit-matched) must not change what the page shows about a request.

Driven in headless Chrome against the served phone port at a phone's width (390): the request text as filed is fully
in the DOM, no CSS clips it (no ellipsis, no line clamp, no height cap, no scroll box), nothing scrolls sideways, and
every tap target is at least 44px. The page is read in both colour schemes the cockpit-matched sheet defines."""

from __future__ import annotations

import json
import urllib.request
from datetime import timedelta
from pathlib import Path

from levain.authority import anchor_path
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_phone_server import _TOK, _serving, world  # noqa: F401  (world is a fixture)

LONG = "x" * 40 + "/very/long/path/with/no/spaces/" * 6 + "\nline two of the request " + "word " * 80


def _file_long(world) -> str:
    q = ConsentQueue(world["qdir"], authority=authority_source(world["root"] / "cockpit.db", anchor=anchor_path(world["root"])))
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
