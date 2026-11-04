"""S4MD-2: a flow Needs-you ping's detail view. flow's push body is `repo: what` / an options line / a sign line, each one line
(the row itself is one line), and the detail view used to draw it as one run-on paragraph with a raw address. Three behaviours:
the body is read back into parts (and the title is plain), the text's list and link draw as blocks, and the options draw as rows
that open the existing sign page with the pick appended. What stays undischarged here is how it reads in his hands on his iPhone."""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

from levain.webpush import center
from levain.webpush.store import PushStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_notify_center_page import MODAL, OPEN, OPENED, PILL, Rig, _env, _json, _url  # noqa: F401  (_env: the autouse fixture)

SIGN = "https://missinglayer.example.ts.net/sign/np-7121be43-4a9b2da2#sign=np-7121be43-4a9b2da2&v=7daebba8dcab02c8"
WHAT = ("**S4MD test ping** (desk 18). Tap into this item's *detail view* and judge how the markdown renders: - **bold** and *italic* "
        "- a list (this one) - `inline code` - a link with its address shown: [Levain repo](https://github.com/levainhq/levain) "
        "Then answer (a) if it reads right, or (b) and say what looks wrong.")
BODY = (f"levain: {WHAT}\na) renders right, S4MD discharged · b) something is off (say what, on which device) · Rec a\n"
        f"Sign your answer (Face ID): {SIGN}")
NOOPTS = "levain: Talk about the hosting move when you are back at the desk.\nNo listed options.\nAnswer in flowConnect: /desk + your words (no id needed)"


def _add(q: Path, body: str, ref: str) -> None:
    PushStore(q).add_ping("attention", ref, "levain", body)


def test_a_needs_ping_is_read_back_into_its_parts_and_its_title_is_plain(tmp_path: Path, monkeypatch) -> None:
    """The push's words come back as repo, text, options, Rec and the base sign link; the stop text and the options' whole text come
    from the sign request the link names; the title is the text's first sentence with no Markdown; a body off the shape is left alone."""
    got = center.needs_wire(BODY)
    assert got and got["repo"] == "levain" and got["what"] == WHAT and got["rec"] == "a" and got["sign_url"] == SIGN, got
    assert [o["key"] for o in got["options"]] == ["a", "b"] and got["title"] == "S4MD test ping (desk 18).", got
    assert got["stopped"] is None and got["sign_open"] is None            # no request read: the push's own words, the link still live
    req = types.SimpleNamespace(refused=None, namespace="flow-needs-phill", options=(("a", "renders right, S4MD discharged IN FULL"), ("b", "something is off")),
                                message=f"flow needs-phill answer\nitem: np-7121be43\nrow_version: v\nrepo: levain\nasked: {WHAT}\nstopped: nothing: S4MD sign-off\n")
    from levain.consent import sign
    monkeypatch.setattr(sign, "read_request", lambda q, rid: req)
    monkeypatch.setattr(sign, "state_of", lambda q, rid: "open")
    full = center.needs_wire(BODY, tmp_path)
    assert full["stopped"] == "nothing: S4MD sign-off" and full["options"][0]["text"].endswith("IN FULL") and full["sign_open"] is True, full
    assert center.needs_wire("claude_code asks: Ship it?", tmp_path) is None and center.needs_wire("just text\nmore", tmp_path) is None
    assert center.needs_wire(NOOPTS)["options"] == []
    store = PushStore(tmp_path)
    store.add_ping("attention", "np-7121be43", "levain", BODY)
    item = center.list_items(store)["items"][0]
    assert item["title"] == "S4MD test ping (desk 18)." and "*" not in item["title"] and item["needs"]["repo"] == "levain", item


def _probe() -> str:
    return (f"JSON.stringify({{ title: {MODAL}.querySelector('.nc-dtitle').textContent, repo: ({MODAL}.querySelector('.nc-repo') || {{}}).textContent,"
            f" li: [...{MODAL}.querySelectorAll('.nc-nlist li')].map(x => x.textContent), strong: [...{MODAL}.querySelectorAll('.nc-nbody strong')].map(x => x.textContent),"
            f" code: [...{MODAL}.querySelectorAll('.nc-nbody code')].map(x => x.textContent), full: [...{MODAL}.querySelectorAll('.nc-nbody .nc-fullurl')].map(x => x.textContent),"
            f" picks: [...{MODAL}.querySelectorAll('.nc-pick')].map(a => [a.querySelector('.nc-pickkey').textContent, a.querySelector('.nc-picktext').textContent,"
            f" a.getAttribute('href'), !!a.querySelector('.nc-recmark'), a.getBoundingClientRect().height]),"
            f" raw: {MODAL}.querySelector('.nc-detail').textContent.includes('https://missinglayer'), stars: {MODAL}.querySelector('.nc-detail').textContent.includes('**'),"
            f" order: [...{MODAL}.querySelector('.nc-detail').children].map(c => c.className || c.tagName), sw: {MODAL}.scrollWidth <= {MODAL}.clientWidth + 1 }})")


def _drive_detail(tmp_path: Path, title: str, body: str, ref: str, width: int = 390) -> dict:
    r = Rig(tmp_path)
    _add(r.q, body, ref)
    shots = os.environ.get("S4MD_SHOTS")
    row = f"[...{MODAL}.querySelectorAll('.nc-row')].find(r => r.querySelector('.nc-title').textContent === {json.dumps(title)})"
    steps = [{"emulate": {"w": width, "h": 844, "dpr": 2, "mobile": width < 700}}, {"until": f"{MODAL}.querySelectorAll('.nc-row').length >= 6 && 1", "ms": 30000},
             {"eval": f"JSON.stringify({{ rowTitle: {row}.querySelector('.nc-title').textContent }})"},
             {"eval": f"{row}.querySelector('.nc-main').click(); 1"}, {"until": f"{MODAL}.querySelector('.nc-dtitle') && 1"},
             {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": _probe()}]
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        steps.append({"shot": str(Path(shots) / f"needs_{ref}_{width}.png"), "full": False})
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, *steps])
    docs = _json(out)
    return {"row": docs[0], "detail": docs[1]}


@needs_browser
def test_the_texts_list_and_link_draw_as_blocks_and_the_title_has_no_raw_markdown(tmp_path: Path) -> None:
    got = _drive_detail(tmp_path, "S4MD test ping (desk 18).", BODY, "np-7121be43")
    d = got["detail"]
    assert got["row"]["rowTitle"] == "S4MD test ping (desk 18)." and d["title"] == "S4MD test ping (desk 18).", got
    assert d["repo"] == "levain" and d["stars"] is False and d["raw"] is False, d
    assert d["li"] == ["bold and italic", "a list (this one)", "inline code", "a link with its address shown: Levain repo (github.com)"], d     # the one-line list is a list again
    assert d["strong"] == ["bold"] and d["code"] == ["inline code"] and d["full"] == [" (github.com)"], d
    assert d["sw"] is True, d


@needs_browser
def test_the_options_draw_as_large_rows_that_open_the_sign_page_with_the_pick(tmp_path: Path) -> None:
    got = _drive_detail(tmp_path, "S4MD test ping (desk 18).", BODY, "np-7121be43")
    d = got["detail"]
    assert [(p[0], p[1], p[2], p[3]) for p in d["picks"]] == [("a", "renders right, S4MD discharged", SIGN + "&choice=a", True),
                                                             ("b", "something is off (say what, on which device)", SIGN + "&choice=b", False)], d
    assert all(p[4] >= 44 for p in d["picks"]), d                                                            # the HIG hit height
    assert d["order"][:3] == ["nc-dmeta", "nc-dtitle", "nc-ask"] and d["order"][-1] == "nc-tools nc-foot", d      # the ask first, the housekeeping verbs last
