"""The cold cockpit's progressive boot, L3 round 1 fixes (levain#27): what a cold board keeps between its reads, and what it lets go.

Under node with the real boot and the real view mapper and a fake page (the `_PAGE` harness of test_cockpit_progressive_boot.py):
- a body read under a dropped token is never drawn again under the new credential (codex HIGH);
- the retry backoff grows while the shells keep being drawn (complement MED);
- a superseded read neither writes into the kept bodies nor restarts the watchdog (deepseek MED, MED-LOW);
- the watchdog releases only the shells whose reads have not landed (complement LOW-MED);
- a panel's own failure is drawn before a loading input of another panel (codex MED)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_cockpit_progressive_boot_shells import _MAPPER, _PREFIX, NODE, WEB, _run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_BOOT = _PREFIX + r"""
(async () => {
  const out = {};
  const quiet = console.warn; console.warn = () => {};
  const titles = (v) => v.layout.map((x) => x.title).sort();
  const loadingOf = (v) => Object.keys(v.extra_panels).filter((k) => v.extra_panels[k].loading === true).sort();

  // T1: a token-read cold boot lands decisions and the state line (a part), an edit opens, the loops read answers the token 403 and the
  // prompt is declined, so the token is dropped; the whole draw waits on the edit. When the edit closes the board is read again as the
  // credential the page now holds: nothing read under the dropped token is drawn, not even for a moment
  { const e = await makeEnv({ holdLoops: true, loopsRefuse: true, credTitles: true }).start();
    await sleep(450);
    out.t1Part = { titles: titles(e.renders[e.renders.length - 1]), whole: e.whole() };
    e.setEditing(true); e.release(); await settle();
    out.t1Dropped = { token: e.ls.levain_write_token === undefined, whole: e.whole() };
    const before = e.renders.length;
    e.hold(); e.setEditing(false); e.listeners["levain:editor-closed"][0](); await sleep(200);
    const after = e.renders.slice(before);
    out.t1Reread = { added: after.length, tokenTitles: after.map(titles).flat().filter((t) => /@token/.test(t)), loading: after.length ? loadingOf(after[0]) : null };
    e.open(); await settle();
    const f = e.renders[e.renders.length - 1];
    out.t1Whole = { whole: e.whole(), tokenTitles: titles(f).filter((t) => /@token/.test(t)) }; }

  // T2: the shells are drawn on every read of a board not yet whole, and a later step of the read keeps failing (the whole draw throws):
  // the retry still backs off 2, 4, 8 s, it is not reset to 2 s by each read's shell draw
  { const e = await makeEnv({ throwOnRender: [2, 4, 6, 8] }).start();
    const delays = [];
    for (let i = 0; i < 3; i++) { delays.push(e.timers.filter((t) => t.ms !== HOLD_MS).map((t) => t.ms)[0] || null); await e.fire(); }
    out.t2Delays = delays; out.t2Renders = e.renders.length; }

  // T3: the watchdog fires while the whole draw waits on an edit, with every read landed: no landed panel is released as "no answer";
  // a read that is still out is
  { const e = await makeEnv({ holdLoops: true }).start();
    await sleep(450);
    e.setEditing(true); e.release(); await settle();
    out.t3Refused = { whole: e.whole() };
    await e.fireHold();
    out.t3 = { released: e.released.slice(), keep: e.releasedKeep.slice() }; }
  { const e = await makeEnv({ holdLoops: true }).start();
    await sleep(450);
    await e.fireHold();
    out.t3Hung = { released: e.released.slice(), keep: e.releasedKeep.slice() }; }

  // T4: a read superseded by a foreground answers late: it writes nothing into what the board keeps (the next read is drawn from the newer
  // read's body) and it does not restart the watchdog
  { const e = await makeEnv({ holdLoops: true, loopsNumbered: true }).start();
    await sleep(450);
    e.hold();
    e.listeners["levain:resume"][0]({ detail: { hiddenAt: Date.now() + 5000 } }); await sleep(100);
    e.setEditing(true); e.open(); await settle();          // the newer read lands loops#2; its whole draw waits on the edit
    out.t4Refused = { whole: e.whole() };
    const arms = e.holdArms;
    e.release(); await settle();                            // the superseded read's loops#1 answers now
    out.t4Arms = e.holdArms - arms;
    const before = e.renders.length;
    e.hold(); e.setEditing(false); e.listeners["levain:editor-closed"][0](); await sleep(200);
    const first = e.renders[before];
    out.t4Seeded = first ? (first.layout.find((x) => x.id === "loops") || {}).title : null;
    e.open(); await settle(); }
  console.warn = quiet;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def boot(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _run(tmp_path_factory.mktemp("review"), "review.js", _BOOT, str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"))


def test_nothing_read_under_a_dropped_token_is_drawn_under_the_new_credential(boot: dict) -> None:
    # the part was read as the token, and the token was dropped while the edit held the whole draw
    assert "decisions@token" in boot["t1Part"]["titles"], boot["t1Part"]
    assert boot["t1Dropped"] == {"token": True, "whole": False}, boot["t1Dropped"]
    # the re-read draws its shells at once, and none of them is a body read under the dropped token
    r = boot["t1Reread"]
    assert r["added"] >= 1 and r["tokenTitles"] == [], r
    assert r["loading"] == ["decisions", "loops"], r
    assert boot["t1Whole"] == {"whole": True, "tokenTitles": []}, boot["t1Whole"]


def test_the_retry_backs_off_while_the_shells_keep_being_drawn(boot: dict) -> None:
    assert boot["t2Delays"] == [2000, 4000, 8000], boot["t2Delays"]


def test_the_watchdog_releases_only_the_shells_whose_reads_have_not_landed(boot: dict) -> None:
    assert boot["t3Refused"] == {"whole": False}
    # every read landed, the draw waits on the edit: the release keeps every landed panel
    assert boot["t3"] == {"released": ["no answer in 60 s"], "keep": [["decisions", "loops", "state"]]}, boot["t3"]
    # a read still out (loops, hung) is released; the panels that landed are kept
    assert boot["t3Hung"] == {"released": ["no answer in 60 s"], "keep": [["decisions", "state"]]}, boot["t3Hung"]


def test_a_superseded_read_writes_nothing_the_board_keeps_and_does_not_restart_the_watchdog(boot: dict) -> None:
    assert boot["t4Refused"] == {"whole": False}
    assert boot["t4Arms"] == 0, boot
    assert boot["t4Seeded"] == "loops#2", boot


# ---- the mapper: own loading, then own failure, then a loading input -------------------------------------------------------------------
_PRECEDENCE = _MAPPER.replace("console.log(JSON.stringify(out));", r"""
const hBad = draw({ health: { error: "HTTP 500" } });          // health failed, wraps (its input) still out
out.hBad = { ex: hBad.extra_panels.health, failed: keys(hBad.failed), loading: keys(hBad.loading) };
const hBadW = draw({ health: { error: "HTTP 500" }, wraps: body.wraps });
out.hBadW = { ex: hBadW.extra_panels.health, failed: keys(hBadW.failed) };
console.log(JSON.stringify(out));""")


def test_a_panels_own_failure_is_drawn_before_a_loading_input_of_another_panel(tmp_path: Path) -> None:
    out = _run(tmp_path, "prec.js", _PRECEDENCE, str(WEB / "cockpit_view.js"))
    # health's own read failed while wraps is out: health is its failure (and the vital reads failed.health), never loading
    assert str(out["hBad"]["ex"].get("error", "")).startswith("HTTP 500") and "loading" not in out["hBad"]["ex"], out["hBad"]
    assert "health" in out["hBad"]["failed"] and "health" not in out["hBad"]["loading"], out["hBad"]
    assert str(out["hBadW"]["ex"].get("error", "")).startswith("HTTP 500") and out["hBadW"]["failed"] == ["health"], out["hBadW"]
