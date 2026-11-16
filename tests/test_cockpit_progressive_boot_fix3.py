"""The cold cockpit's progressive boot, L3 round 2 fix (levain#27): a page never draws a body read under a credential it no longer holds.

Under node with the real boot and the real view mapper and a fake page (the `_PAGE` harness of test_cockpit_progressive_boot.py). Every
draw of a read (each part, and the whole) takes its bodies through one credential check, so a body this read landed under a token the
page has since dropped is drawn in no later draw, partial or whole (with an open edit: test_cockpit_progressive_boot_review.py T1); the
read is then made again at once as the credential the page holds."""

from __future__ import annotations

import pytest

from tests.test_cockpit_progressive_boot_shells import _PREFIX, NODE, WEB, _run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_BOOT = _PREFIX + r"""
(async () => {
  const out = {};
  const quiet = console.warn; console.warn = () => {};
  const titles = (v) => v.layout.map((x) => x.title).sort();
  const tokenTitles = (vs) => vs.map(titles).flat().filter((t) => /@token/.test(t));

  // D1: a token-read cold boot, no edit open: decisions and the state line land as the token, the loops read answers the token 403 and the
  // prompt is declined, so the token is dropped. The read's own whole draw would draw the bodies it landed before the drop: it draws none
  // of them; the read is made again at once as the credential the page now holds, and that whole draw is the board
  { const e = await makeEnv({ holdLoops: true, loopsRefuse: true, credTitles: true }).start();
    await sleep(450);
    out.d1Part = { titles: titles(e.renders[e.renders.length - 1]), whole: e.whole() };
    const before = e.renders.length;
    e.release(); await settle();
    out.d1Dropped = { token: e.ls.levain_write_token === undefined, tokenTitles: tokenTitles(e.renders.slice(before)), whole: e.whole() };
    await settle();
    const f = e.renders[e.renders.length - 1];
    out.d1Reread = { whole: e.whole(), titles: titles(f), tokenTitles: tokenTitles(e.renders.slice(before)), retries: e.timers.filter((t) => t.ms !== HOLD_MS).length }; }

  // D2: the token goes (another tab dropped it) after two bodies landed as it and before the part that draws them: that part, and every
  // later draw of this read, draws neither of them
  { const e = await makeEnv({ holdLoops: true, credTitles: true }).start();
    const before = e.renders.length;
    delete e.ls.levain_write_token;
    await sleep(450);
    const parts = e.renders.slice(before);
    out.d2Part = { added: parts.length, tokenTitles: tokenTitles(parts), whole: e.whole() };
    e.release(); await settle();
    out.d2Whole = { tokenTitles: tokenTitles(e.renders.slice(before)), whole: e.whole() };
    await settle();
    out.d2Reread = { whole: e.whole(), titles: titles(e.renders[e.renders.length - 1]), tokenTitles: tokenTitles(e.renders.slice(before)), retries: e.timers.filter((t) => t.ms !== HOLD_MS).length }; }
  console.warn = quiet;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def boot(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _run(tmp_path_factory.mktemp("fix3"), "fix3.js", _BOOT, str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"))


def test_a_read_whose_token_is_dropped_draws_none_of_the_bodies_it_read_as_that_token(boot: dict) -> None:
    # the part was drawn as the token, before the drop
    assert "decisions@token" in boot["d1Part"]["titles"], boot["d1Part"]
    d = boot["d1Dropped"]
    assert d["token"] is True and d["tokenTitles"] == [], d
    # the read is made again at once as the credential the page holds (not a failure: no backoff is queued), and that is the whole board
    r = boot["d1Reread"]
    assert r["whole"] is True and r["tokenTitles"] == [] and r["retries"] == 0, r
    assert r["titles"] == ["decisions@none", "loops@none"], r


def test_a_part_drawn_after_the_token_went_draws_none_of_the_bodies_read_as_it(boot: dict) -> None:
    p = boot["d2Part"]
    # a part was drawn after the token went (the pacer's next draw), and it holds no body read as the token
    assert p["added"] >= 1 and p["tokenTitles"] == [] and p["whole"] is False, p
    assert boot["d2Whole"]["tokenTitles"] == [], boot["d2Whole"]
    r = boot["d2Reread"]
    assert r["whole"] is True and r["tokenTitles"] == [] and r["retries"] == 0 and r["titles"] == ["decisions@none", "loops@none"], r
