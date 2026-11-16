"""Board levain#66, S5b: the Host grants block on the cockpit's Decisions view, in a real headless Chrome at 390 px and 1280 px
(the page-drive harness only). The cockpit is the real ``make_server`` with the real ``HostGrantSurface`` over a FAKE root spawn
(no sudo, no network), so what the page draws is what S5a's routes answer. ``LEVAIN_HOSTGRANT_SHOTS`` names a folder to leave
a screenshot per state in (a review look)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.consent import host_grants as HG
from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _serve
from tests.test_host_grants_surface import G1, G2, G3, S1, S2, FakeRoot, _slot

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NARROW = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
WIDE = {"emulate": {"w": 1280, "h": 900, "dpr": 1, "mobile": False}}
_SHOTS = os.environ.get("LEVAIN_HOSTGRANT_SHOTS")
SLOT = "document.querySelector('#levain-modal .hostgrant-slot')"
FILLED = f"!!{SLOT} && {SLOT}.dataset.filled === '1'"
_FADED = "getComputedStyle(document.querySelector('#levain-modal .modal') || document.querySelector('#levain-modal')).opacity === '1'"
RELAY = "surface-token-" + "r" * 24


def _btn(grant: str) -> str:
    return f"{SLOT}.querySelector('[data-hostgrant=\"{grant}\"] .hostgrant-revoke')"


def _all_btn(session: str) -> str:
    return f"{SLOT}.querySelector('[data-hostgrant-session=\"{session}\"] .hostgrant-all .hostgrant-revoke')"


def _shot(name: str) -> list[dict[str, Any]]:
    return [{"until": _FADED}, {"shot": str(Path(_SHOTS) / name)}] if _SHOTS else []


def _slots(*specs: Any) -> list[dict[str, Any]]:
    """Slot rows whose grants end in the future, as the page reads the clock."""
    out = [_slot(*s) if isinstance(s, tuple) else s for s in specs]
    at = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for s in out:
        for e in s["entries"]:
            e["expires"] = at
    return out


def _direct(session: str, target: str) -> dict[str, Any]:
    """A revoke sent by the page's own transport, outside its buttons (another device's tap, or a hand-made POST)."""
    body = "JSON.stringify({session: '%s', target: '%s'})" % (session, target)
    return {"eval": "fetch('/cockpit/hostgrant/revoke', {method: 'POST', headers: Object.assign({'Content-Type': 'application/json'},"
                    f" window.LevainCockpitTransport.headers()), body: {body}}}).then((r) => r.status)"}


def _open(tmp_path: Path, port: int, *steps: Any, token: str = T, view: Any = NARROW) -> list[Any]:
    out = _drive(tmp_path, f"http://127.0.0.1:{port}/#write_token={token}", [
        view, {"until": TILE}, {"eval": OPEN_DECISIONS}, {"until": FILLED}, {"eval": "'__start__'"}, *steps])
    vals = out["values"]
    vals = vals[vals.index("__start__") + 1:]
    return [v for v in vals if not (isinstance(v, dict) and "shot" in v)]


@pytest.fixture
def src(tmp_path: Path) -> Any:
    return _install(tmp_path)[1]


def _mounted(src: Any, root: FakeRoot, **kw: Any) -> Any:
    return _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=HG.HostGrantSurface(spawn=root), **kw)


def _slow_revoke(root: FakeRoot, seconds: float) -> Any:
    """The fake root with only the revoke slow, so a list read is quick while a revoke stays on the wire."""
    def spawn(argv: Any, **kw: Any) -> Any:
        if list(argv)[3] == "egress-grant-revoke":
            time.sleep(seconds)
        return root(argv, **kw)
    return spawn


_ROWS = ("[...document.querySelectorAll('#levain-modal .hostgrant-row')].map((r) => ({session: r.dataset.hostgrantSession,"
         " head: r.querySelector('.consent-head').textContent, paused: !!r.querySelector('.hostgrant-paused'),"
         " grants: [...r.querySelectorAll('.hostgrant-grant')].map((g) => g.dataset.hostgrant)}))")
_SAID = f"(() => {{ const n = {SLOT}.querySelector('.hostgrant-said'); return n ? n.textContent : null; }})()"
_TEXT = f"{SLOT}.textContent"
_METRICS = ("(() => { const s = " + SLOT + "; const bs = [...s.querySelectorAll('button')].map((b) => b.getBoundingClientRect());"
            " return {sideways: document.documentElement.scrollWidth > window.innerWidth + 1,"
            " bodySideways: document.querySelector('#levain-modal').scrollWidth > window.innerWidth + 1,"
            " minH: Math.min(...bs.map((b) => b.height)), minW: Math.min(...bs.map((b) => b.width)),"
            " maxRight: Math.max(...bs.map((b) => b.right)), vw: window.innerWidth}; })()")


@needs_browser
@pytest.mark.parametrize("viewport,tag", [(NARROW, "390"), (WIDE, "desktop")])
def test_live_and_paused_rows_by_slot_and_the_confirm_tap_revokes_one_grant(src, tmp_path, viewport, tag) -> None:
    """LIVE and PAUSED rows by slot, an UNREAD slot beside them, the per-row confirm tap (the first tap only arms), the X8 sentence."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3], "BRAKED")))
    root.unread = [{"slot": 2, "name": "egress-store-unreadable"}]
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port,
                       {"eval": _ROWS}, {"eval": _METRICS}, {"eval": _TEXT}, *_shot(f"hostgrants-live-{tag}.png"),
                       {"tap": _btn(G1)}, {"eval": f"{_btn(G1)}.textContent"},
                       {"eval": f"document.querySelectorAll('#levain-modal .hostgrant-grant').length"},   # one tap alone revoked nothing
                       {"tap": _btn(G1)},
                       {"until": f"!{_btn(G1)} && !!{SLOT}.querySelector('.hostgrant-said')"},
                       {"eval": _SAID}, {"eval": _ROWS}, *_shot(f"hostgrants-revoked-{tag}.png"), view=viewport)
    rows = next(v for v in values if isinstance(v, list))
    metrics = next(v for v in values if isinstance(v, dict) and "sideways" in v)
    text = next(v for v in values if isinstance(v, str) and "Host grants" in v)
    armed = next(v for v in values if v == "Tap again to revoke")
    after = [v for v in values if isinstance(v, list)][-1]
    said = next(v for v in values if isinstance(v, str) and v.startswith("Root revoked"))
    assert [(r["session"], r["paused"], r["grants"]) for r in rows] == [(S1, False, [G1, G2]), (S2, True, [G3])], rows
    assert "slot 0 · interactive" in rows[0]["head"] and "slot 1 · interactive" in rows[1]["head"], rows
    assert "paused" in text and "Slot 2 could not be read by root (egress-store-unreadable)" in text, text
    assert re.search(r"ends in 3 h", text), text      # 3 h 20 min when built; the minutes tick down, so only the hours are pinned
    assert "for the session" in text, text
    assert armed == "Tap again to revoke"
    assert [r["grants"] for r in after] == [[G2], [G3]], after
    assert said == ("Root revoked the grant. A healthy running front closes its granted tunnels on its next check, "
                    "normally within about five seconds."), said
    assert metrics["sideways"] is False and metrics["bodySideways"] is False, metrics
    assert metrics["minH"] >= 43.9 and metrics["minW"] >= 43.9 and metrics["maxRight"] <= metrics["vw"], metrics
    revokes = [c for c in root.calls if c[0] == "egress-grant-revoke"]
    assert revokes == [["egress-grant-revoke", S1, G1]], root.calls     # one tap alone sent nothing; the second sent one revoke


@needs_browser
def test_a_stale_row_answers_already_gone_and_revoke_all_says_how_many(src, tmp_path) -> None:
    """GONE: a tap on a row another device already revoked says so, never "revoked", and sends no revoke. Revoke all counts."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3, "eh-" + "d" * 24])))
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port,
                       _direct(S1, G2),                                          # revoked elsewhere: the page's row is now stale
                       {"tap": _btn(G2)}, {"tap": _btn(G2)},
                       {"until": f"!{_btn(G2)} && !!{SLOT}.querySelector('.hostgrant-said')"},
                       {"eval": _SAID}, *_shot("hostgrants-gone-390.png"),
                       {"tap": _all_btn(S2)}, {"eval": f"{_all_btn(S2)}.textContent"}, {"tap": _all_btn(S2)},
                       {"until": f"!{_all_btn(S2)} && /Root revoked the 2 grants/.test({SLOT}.textContent)"},
                       {"eval": _SAID}, {"eval": _ROWS})
    saids = [v for v in values if isinstance(v, str) and ("already gone" in v or v.startswith("Root revoked"))]
    assert saids[0] == "That grant was already gone: expired or revoked."
    assert saids[-1].startswith("Root revoked the 2 grants that were in force. A healthy running front closes"), saids
    assert "Tap again to revoke all 2" in values
    assert [r["grants"] for r in values[-1]] == [[G1]], values[-1]
    revokes = [c for c in root.calls if c[0] == "egress-grant-revoke"]
    assert revokes == [["egress-grant-revoke", S1, G2], ["egress-grant-revoke", S2, "all"]], root.calls   # the stale tap spawned none


@needs_browser
def test_helper_unreadable_is_a_note_never_an_empty_list(src, tmp_path) -> None:
    root = FakeRoot()
    root.fail = "silent"
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": _TEXT}, {"eval": f"{SLOT}.querySelectorAll('.hostgrant-row, .hostgrant-empty').length"},
                       *_shot("hostgrants-unreadable-390.png"))
    text = next(v for v in values if isinstance(v, str))
    assert "Host grants" in text and "cannot be read now" in text, text
    assert values[1] == 0, "a failed read drew a row or the empty line"


@needs_browser
def test_not_set_up_names_the_doctor_line(src, tmp_path) -> None:
    root = FakeRoot()
    root.fail = "refused:egress-grant-not-ready"
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": _TEXT}, *_shot("hostgrants-notsetup-390.png"))
    text = values[0]
    assert "Host grants are not set up on this machine" in text and text.count("not set up") == 1 and "levain doctor" in text and "names the one command" in text, text
    assert "No live host grants" not in text


@needs_browser
def test_busy_and_rate_limited_say_so_and_the_row_stays(src, tmp_path) -> None:
    root = FakeRoot(_slots((S1, 0, [G1])))
    root.revoke_refuse = "session-busy"
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port,
                       {"tap": _btn(G1)}, {"tap": _btn(G1)},
                       {"until": f"!!{SLOT}.querySelector('.hostgrant-said')"}, {"eval": _SAID},
                       {"until": f"!!{_btn(G1)}"}, *_shot("hostgrants-busy-390.png"),
                       {"eval": "(async () => { const r = []; for (let i = 0; i < 10; i++) r.push(await fetch('/cockpit/hostgrant/revoke',"
                                " {method: 'POST', headers: Object.assign({'Content-Type': 'application/json'}, window.LevainCockpitTransport.headers()),"
                                f" body: JSON.stringify({{session: '{S1}', target: '{G1}'}})}}).then((x) => x.status)); return r.join(','); }})()"},
                       {"tap": _btn(G1)}, {"tap": _btn(G1)},
                       {"until": f"/wait a minute/.test({SLOT}.textContent)"}, {"eval": _SAID}, *_shot("hostgrants-ratelimited-390.png"),
                       {"eval": f"!!{_btn(G1)}"})
    saids = [v for v in values if isinstance(v, str) and v.startswith("Not revoked") or isinstance(v, str) and v.startswith("Too many")]
    assert saids[0] == "Not revoked; busy, tap again", saids
    assert saids[-1] == "Too many revokes from this device; wait a minute.", saids
    assert values[-1] is True, "the row was taken away by a refused revoke"


@needs_browser
def test_a_server_that_does_not_mount_the_route_or_does_not_admit_the_credential_draws_no_block(src, tmp_path) -> None:
    """NOT MOUNTED (404) and a credential the rule refuses (403, a non-browser surface token): the slot is empty, no heading."""
    root = FakeRoot()
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}) as (port, _h):
        values = _open(tmp_path, port, {"eval": f"{SLOT}.children.length"})
    assert values[0] == 0, values
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": f"{SLOT}.children.length"}, token=RELAY)
    assert values[0] == 0, values
    assert root.calls == [], "a refused credential reached the helper"


@needs_browser
def test_server_strings_are_text_never_markup(src, tmp_path) -> None:
    """T12: a host shaped like markup is drawn as text; nothing it carries runs."""
    evil = "<img src=x onerror=window.__pwn=1>.example.com"
    root = FakeRoot(_slots((S1, 0, [G1])))
    root.slots[0]["entries"][0]["host"] = evil
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": f"{SLOT}.querySelector('.hostgrant-host').textContent"},
                       {"eval": f"{SLOT}.querySelectorAll('img').length"}, {"eval": "window.__pwn === undefined"})
    assert values[0] == evil and values[1] == 0 and values[2] is True, values


@needs_browser
def test_no_live_grants_says_so(src, tmp_path) -> None:
    with _mounted(src, FakeRoot(_slots((S1, 0, [])))) as (port, _h):
        values = _open(tmp_path, port, {"eval": _TEXT}, *_shot("hostgrants-empty-390.png"))
    assert "No live host grants" in values[0], values


_ALL_SLOTS = "[...document.querySelectorAll('.hostgrant-slot')].filter((s) => s.isConnected)"
_LISTS = "performance.getEntriesByType('resource').filter((e) => e.name.includes('hostgrant/list.json')).length"
_STATE = ("(() => " + _ALL_SLOTS + ".map((s) => ({filled: s.dataset.filled === '1', text: s.textContent,"
          " grants: [...s.querySelectorAll('.hostgrant-grant')].map((g) => g.dataset.hostgrant)})))()")
_REOPEN = ("(() => { const b = document.querySelector('#ck-needs:not([hidden]) .needs-history') || " + TILE + "; b.click(); return 1; })()")
_MARK_OLD = "(() => { window.__old = " + SLOT + "; return 1; })()"
_NEW = f"!!{SLOT} && {SLOT} !== window.__old"


@needs_browser
def test_a_rerender_during_a_revoke_leaves_the_new_slot_drawn_live_and_told(src, tmp_path) -> None:
    """Per-slot state: a revoke in flight on a slot the page then replaced (the Decisions view drawn again) finishes without
    discarding the new slot's own fill, without leaving the new slot's buttons inert, and draws its result on every slot still
    on the page."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3])))
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY},
                host_grants=HG.HostGrantSurface(spawn=_slow_revoke(root, 1.5))) as (port, _h):
        values = _open(tmp_path, port,
                       {"eval": _MARK_OLD}, {"tap": _btn(G1)}, {"tap": _btn(G1)},      # the revoke is on the wire (1.5 s at the fake root)
                       {"eval": _REOPEN}, {"until": _NEW}, {"eval": _ALL_SLOTS + ".length"},
                       {"until": FILLED}, {"eval": _STATE},
                       {"tap": _btn(G2)}, {"eval": f"{_btn(G2)}.textContent"},         # the first tap arms: the old revoke's busy is not this slot's
                       {"until": f"/Root revoked/.test({SLOT}.textContent)"}, {"eval": _STATE})
    states = [v for v in values if isinstance(v, list)]
    assert all(s["filled"] for s in states[0]), states[0]                                # the new slot was not left blank
    assert "Tap again to revoke" in values, values                                       # its buttons were not inert
    assert all(G1 not in s["grants"] and "Root revoked" in s["text"] for s in states[-1]), states[-1]


@needs_browser
def test_a_revoke_redraws_every_slot_on_the_page_not_only_the_one_that_asked(src, tmp_path) -> None:
    """The card and its focus modal are two slots on one page: a revoke from one leaves neither stale, neither blank."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3])))
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port,
                       {"eval": _MARK_OLD}, {"eval": _REOPEN}, {"until": _NEW}, {"until": FILLED},
                       {"eval": "(() => { document.body.appendChild(window.__old); return 1; })()"},      # the old slot is back on the page: two live slots
                       {"tap": _btn(G1)}, {"tap": _btn(G1)},
                       {"until": f"/Root revoked/.test({SLOT}.textContent)"},
                       {"until": f"!window.__old.querySelector('[data-hostgrant=\"{G1}\"]') && window.__old.dataset.filled === '1'"},
                       {"eval": _STATE})
    states = values[-1]
    assert len(states) >= 2 and all(s["filled"] and G1 not in s["grants"] for s in states), states


def test_the_block_draws_with_textcontent_only() -> None:
    """The block's source never assigns markup: no innerHTML, outerHTML or insertAdjacentHTML between its two markers."""
    js = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard_core.js").read_text()
    start = js.index("// Board levain#66 (S5b): the Host grants block")
    end = js.index("// OS-2, the dispatch family", start)
    block = js[start:end]
    assert "hostGrantSlot" in block
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", block)
    assert "const hostGrantChoice" in block and "HOSTGRANT_FRONT" in block
    # the page says root revoked it, never that a tunnel closed (X8)
    assert "closes its granted tunnels on its next check, normally within about five seconds" in block
    assert not re.search(r"tunnels? (is|are|was|were) closed|tunnel closed", block, re.I)


def _unknown_root(root: FakeRoot, *, list_after: bool) -> Any:
    """A revoke root applied but answered with junk (an unknown outcome); `list_after` False makes the one read after it fail (the listing the answer carries), later reads work."""
    state = {"revoked": False, "failed": 0}

    def spawn(argv: Any, **kw: Any) -> Any:
        if list(argv)[3] == "egress-grant-revoke":
            root(argv, **kw)
            state["revoked"] = True
            return subprocess.CompletedProcess(argv, 0, stdout=b"not json\n", stderr=b"")
        if state["revoked"] and not list_after and state["failed"] < 1:     # the one read the revoke answer carries; later reads work
            state["failed"] += 1
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"sudo: a password is required\n")
        return root(argv, **kw)
    return spawn


_UNK_STEPS = lambda: [{"tap": _btn(G1)}, {"tap": _btn(G1)}, {"until": f"!!{SLOT}.querySelector('.hostgrant-said')"},   # noqa: E731
                      {"eval": _SAID}, {"eval": _STATE}, {"eval": f"'inert:' + !![...{SLOT}.querySelectorAll('button')].find((b) => b.disabled)"}]


@needs_browser
def test_an_unknown_outcome_with_a_readable_listing_says_unknown_and_repaints_every_slot(src, tmp_path) -> None:
    """502 unknown with a readable listing: OUTCOME UNKNOWN (never "not revoked"), every connected slot repainted from the listing."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3])))
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=HG.HostGrantSurface(spawn=_unknown_root(root, list_after=True))) as (port, _h):
        values = _open(tmp_path, port, {"eval": _MARK_OLD}, {"eval": _REOPEN}, {"until": _NEW}, {"until": FILLED},
                       {"eval": "(() => { document.body.appendChild(window.__old); return 1; })()"}, {"eval": "'lists0:' + " + _LISTS}, *_UNK_STEPS(),
                       {"until": f"!window.__old.querySelector('[data-hostgrant=\"{G1}\"]') && window.__old.dataset.filled === '1'"},
                       {"eval": _STATE}, {"eval": "'lists1:' + " + _LISTS})
    said = next(v for v in values if isinstance(v, str) and v.startswith("Outcome unknown"))
    assert "may or may not be revoked" in said and "not revoked." not in said.replace("or may not be revoked", ""), said
    states = [v for v in values if isinstance(v, list)][-1]
    assert len(states) >= 2 and all(s["filled"] and G1 not in s["grants"] and "Outcome unknown" in s["text"] for s in states), states
    assert "inert:false" in values, "a button was left inert"
    before = next(v for v in values if isinstance(v, str) and v.startswith("lists0:"))[7:]
    assert "lists1:" + before in values, values      # drawn from the listing the answer carried: no second trip to the list route


@needs_browser
def test_an_unknown_outcome_with_an_unreadable_listing_says_to_refresh_and_keeps_no_stale_list(src, tmp_path) -> None:
    """502 unknown whose listing is a note: OUTCOME UNKNOWN plus refresh once the helper finishes, no stale row with live buttons."""
    root = FakeRoot(_slots((S1, 0, [G1, G2])))
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=HG.HostGrantSurface(spawn=_unknown_root(root, list_after=False))) as (port, _h):
        values = _open(tmp_path, port, *_UNK_STEPS())
    said = next(v for v in values if isinstance(v, str) and v.startswith("Outcome unknown"))
    assert "Refresh once root's helper has finished" in said and "cannot be read now" in said, said
    state = [v for v in values if isinstance(v, list)][-1]
    assert state[0]["grants"] == [] and "may or may not" in state[0]["text"], state
    assert not any(isinstance(v, str) and v.startswith("Not revoked") for v in values)


@needs_browser
def test_helper_running_is_its_own_state_not_a_generic_busy(src, tmp_path) -> None:
    """409 host-grants-helper-running: a distinct line (a root helper is still working, try later); the row stays and is tappable."""
    root = FakeRoot(_slots((S1, 0, [G1])))
    surf = HG.HostGrantSurface(spawn=root)
    surf.revoke = lambda *a, **k: ({"error": "busy", "name": "host-grants-helper-running",   # type: ignore[method-assign]
                                    "message": "not revoked; a previous root helper is still running, tap again once it has finished"}, 409)
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=surf) as (port, _h):
        values = _open(tmp_path, port, {"tap": _btn(G1)}, {"tap": _btn(G1)}, {"until": f"!!{SLOT}.querySelector('.hostgrant-said')"},
                       {"eval": _SAID}, {"until": f"!!{_btn(G1)} && !{_btn(G1)}.disabled"}, {"eval": f"{SLOT}.querySelector('.hostgrant-said').className"})
    said = next(v for v in values if isinstance(v, str) and v.startswith("Root's previous"))
    assert said == "Root's previous helper is still running: this tap was not sent. Try again later, once it has finished.", values
    assert "busy, tap again" not in said and values[-1].endswith("busy") and "hostgrant-said" in values[-1], values


_LOSE_REPLY = ("(() => { const f = window.fetch; window.fetch = function (u) { const p = f.apply(this, arguments);"
               " return /hostgrant\\/revoke/.test(String(u)) ? p.then(() => { throw new TypeError('lost'); }) : p; }; return 1; })()")


@needs_browser
def test_a_revoke_whose_reply_is_lost_reads_outcome_unknown_and_reads_the_list_again(src, tmp_path) -> None:
    """The POST reached root and was applied, the response never came back: never "Not revoked", a fresh read repaints every slot."""
    root = FakeRoot(_slots((S1, 0, [G1, G2]), (S2, 1, [G3])))
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": _MARK_OLD}, {"eval": _REOPEN}, {"until": _NEW}, {"until": FILLED},
                       {"eval": "(() => { document.body.appendChild(window.__old); return 1; })()"}, {"eval": _LOSE_REPLY},
                       {"eval": "'lists0:' + " + _LISTS}, *_UNK_STEPS(),
                       {"until": f"!window.__old.querySelector('[data-hostgrant=\"{G1}\"]') && window.__old.dataset.filled === '1'"},
                       {"eval": _STATE}, {"eval": "'lists1:' + " + _LISTS})
    said = next(v for v in values if isinstance(v, str) and v.startswith("Outcome unknown"))
    assert "may or may not be revoked" in said and "The list below was read again" in said, said
    assert not any(isinstance(v, str) and v.startswith("Not revoked") for v in values), values
    states = [v for v in values if isinstance(v, list)][-1]
    assert len(states) >= 2 and all(s["filled"] and G1 not in s["grants"] and "Outcome unknown" in s["text"] for s in states), states
    before = int(next(v for v in values if isinstance(v, str) and v.startswith("lists0:"))[7:])
    after = int(next(v for v in values if isinstance(v, str) and v.startswith("lists1:"))[7:])
    assert after == before + 1, values                     # exactly one fresh read of the list
    assert [c for c in root.calls if c[0] == "egress-grant-revoke"] == [["egress-grant-revoke", S1, G1]], root.calls


@needs_browser
def test_unread_slots_with_no_grants_do_not_claim_there_are_none(src, tmp_path) -> None:
    """Root could not read a slot that may hold live grants: the page says what it did read, never "No live host grants"."""
    root = FakeRoot(_slots((S1, 0, [])))
    root.unread = [{"slot": 2, "name": "egress-store-unreadable"}]
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": _TEXT}, {"eval": f"{SLOT}.querySelectorAll('.hostgrant-unread').length"})
    assert "Slot 2 could not be read by root" in values[0] and values[1] == 1, values
    assert "No live host grants" not in values[0] and "no live grants in the slots it could read" in values[0], values[0]


_OLD_BTN = lambda g: f"window.__old.querySelector('[data-hostgrant=\"{g}\"] .hostgrant-revoke')"   # noqa: E731
_MODAL_BTNS = f"[...{SLOT}.querySelectorAll('button')]"
# the revoke of G2 is held on the page's own wire until released: the answer to another slot's revoke lands while it is pending
_HOLD_G2 = ("(() => { const f = window.fetch; window.__held = null; window.fetch = function (u, o) { if (/hostgrant\\/revoke/.test(String(u))"
            f" && String((o || {{}}).body).includes('{G2}')) return new Promise((go) => {{ window.__held = () => go(f.call(window, u, o)); }});"
            " return f.apply(this, arguments); }; return 1; })()")


@needs_browser
def test_a_slot_repainted_while_its_own_revoke_is_on_the_wire_has_no_enabled_looking_inert_buttons(src, tmp_path) -> None:
    """Two slots each revoke: A's answer lands while B's revoke is still pending and repaints B. B's new buttons must be disabled, then live."""
    root = FakeRoot(_slots((S1, 0, [G1, G2, G3])))
    with _mounted(src, root) as (port, _h):
        values = _open(tmp_path, port, {"eval": _MARK_OLD}, {"eval": _REOPEN}, {"until": _NEW}, {"until": FILLED},
                       {"eval": "(() => { document.body.appendChild(window.__old); return 1; })()"},
                       {"tap": _btn(G3)}, {"tap": _btn(G3)},                          # a first revoke: its settle draws the detached old slot too
                       {"until": "!!window.__old.querySelector('.hostgrant-said')"}, {"eval": _HOLD_G2},
                       {"tap": _btn(G2)}, {"tap": _btn(G2)},                          # B (the modal): its revoke is held on the wire
                       {"until": "!!window.__held"},
                       {"eval": _OLD_BTN(G1) + ".click()"}, {"eval": _OLD_BTN(G1) + ".click()"},   # A (the old slot): answers, repainting B
                       {"until": f"!{_OLD_BTN(G1)}"}, {"until": f"!{_btn(G1)}"},
                       {"eval": "'mid:' + JSON.stringify(" + _MODAL_BTNS + ".map((b) => b.disabled))"},
                       {"eval": "window.__held(); 1"}, {"until": f"!{_btn(G2)}"},
                       {"eval": "'end:' + JSON.stringify(" + _MODAL_BTNS + ".map((b) => b.disabled))"})
    mid = next(v for v in values if isinstance(v, str) and v.startswith("mid:"))[4:]
    end = next(v for v in values if isinstance(v, str) and v.startswith("end:"))[4:]
    assert mid != "[]" and "false" not in mid, f"the repainted slot's buttons look enabled while its own revoke is busy: {mid}"
    assert "true" not in end, f"its buttons stayed disabled after its revoke settled: {end}"
