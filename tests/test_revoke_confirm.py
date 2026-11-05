"""A device revoke is bound to what the operator saw (levain 30, 11-04): the family a revoke takes is named by the
server before it runs (``revoke-preview``), a request carries that set back as ``expect`` plus a ``mode``, and a
request without them moves nothing. ``mode: "only"`` revokes the target alone and re-parents its children."""
from __future__ import annotations

import pytest

from levain.cockpit.firejournal import PAIRED_PREFIX
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve
from tests.test_cockpit_k2a import _install

REVOKE = "/cockpit/pair/revoke"
PREVIEW = "/cockpit/pair/revoke-preview"


@pytest.fixture
def src(tmp_path):
    return _install(tmp_path)[1]


def _family(port):
    """laptop (minted by the browser surface) -> iphone, ipad, pixel."""
    laptop, tl = _pair(port, label="laptop")
    kids = {}
    for label in ("iphone", "ipad", "pixel"):
        kids[label] = _pair(port, token=tl, label=label)
    return laptop, tl, kids


def _live(httpd):
    return {d["id"]: d for d in httpd.broker.active_db().list_devices() if d["revoked_at"] is None}


def test_the_old_request_shape_no_longer_takes_the_whole_family(src) -> None:
    """The 11-04 failure: the phone sends {id} for the laptop and all four devices are revoked at once."""
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        phone, tphone = kids["iphone"]
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop}, token=tphone, headers=PROXY)
        assert (st, out.get("error")) == (400, "confirm_required"), (st, out)
        assert len(_live(httpd)) == 4          # nothing moved


def test_a_stale_expect_moves_nothing_and_names_the_fresh_set(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=tphone, headers=PROXY)
        assert st == 200 and prev["includes_caller"] is True, prev
        seen = sorted(f["id"] for f in prev["family"])
        assert seen == sorted([laptop] + [v[0] for v in kids.values()])
        _pair(port, token=tl, label="late")     # a fifth device joins after the operator looked
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect": seen, "mode": "family"},
                           token=tphone, headers=PROXY)
        assert (st, out.get("error")) == (409, "family_changed"), (st, out)
        assert len(out["family"]) == 5
        assert len(_live(httpd)) == 5


def test_family_mode_with_the_seen_set_takes_exactly_that_set(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=tphone, headers=PROXY)
        seen = sorted(f["id"] for f in prev["family"])
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect": seen, "mode": "family"}, token=T)
        assert st == 200 and sorted(out["revoked"]) == seen, out
        assert _live(httpd) == {}


def test_only_mode_detaches_the_children_to_the_targets_parent(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=tphone, headers=PROXY)
        seen = sorted(f["id"] for f in prev["family"])
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect": seen, "mode": "only"},
                           token=tphone, headers=PROXY)
        assert st == 200 and out["revoked"] == [laptop], out
        assert sorted(out["detached"]) == sorted(v[0] for v in kids.values())
        live = _live(httpd)
        assert sorted(live) == sorted(v[0] for v in kids.values())
        assert all(d["paired_by"] == "browser" for d in live.values())      # the laptop's own parent
        for _id, tok in kids.values():                                       # their credentials still work
            assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200
        assert _call(port, "GET", "/cockpit/manifest.json", token=tl, headers=PROXY)[0] == 403
        db = httpd.broker.active_db()
        assert db.token_generation(PAIRED_PREFIX + kids["iphone"][0]) in (None, 1)


def test_preview_marks_the_caller_only_when_the_caller_is_in_the_set(src) -> None:
    with _serve(src) as (port, _h):
        laptop, tl, kids = _family(port)
        ipad, _t = kids["ipad"]
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": ipad}, token=tphone, headers=PROXY)
        assert st == 200 and prev["includes_caller"] is False and [f["id"] for f in prev["family"]] == [ipad]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": ipad}, token=T)
        assert prev["includes_caller"] is False
        assert prev["target"] == ipad and set(prev["family"][0]) >= {"id", "label", "paired_by", "last_seen"}


def test_a_surface_token_and_an_unknown_id_are_refused_by_the_preview(src) -> None:
    with _serve(src) as (port, _h):
        assert _call(port, "POST", PREVIEW, {"id": "browser"}, token=T)[0] == 400
        assert _call(port, "POST", PREVIEW, {"id": "d-0123456789ab"}, token=T)[0] == 404
        assert _call(port, "POST", REVOKE, {"id": "d-0123456789ab", "expect": [], "mode": "family"}, token=T)[0] == 404
        assert _call(port, "POST", REVOKE, {"id": "x", "expect": [], "mode": "weird"}, token=T)[0] == 400


# --- the confirm card, in a browser at phone width ---------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

from tests.test_pair_card_w1b import _drive, needs_browser  # noqa: E402

READY = "document.querySelectorAll('.dev-list .dev-row').length === 4"
CLICK_REVOKE = ("(() => { const b = [...document.querySelectorAll('.dev-list .dev-btn')].find((x) =>"
                " x.getAttribute('aria-label').includes('%s')); b.click(); return 1; })()")
CARD = ("JSON.stringify({items: [...document.querySelectorAll('.dev-confirm-item .dev-name')].map((n) => n.textContent),"
        " self: [...document.querySelectorAll('.dev-self')].map((n) => n.closest('.dev-confirm-item').querySelector('.dev-name').textContent),"
        " btns: [...document.querySelectorAll('.dev-confirm-acts .dev-btn')].map((b) => b.textContent),"
        " focus: document.activeElement.textContent,"
        " overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth})")


def _shots(tmp_path: Path) -> Path:
    d = Path(os.environ.get("REVOKE_SHOTS") or tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


@needs_browser
def test_the_confirm_card_names_the_family_marks_this_device_and_focuses_the_safe_button(tmp_path, monkeypatch, src) -> None:
    shots = _shots(tmp_path)
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={tphone}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": CLICK_REVOKE % "laptop"},
            {"until": "document.querySelector('.dev-confirm')", "ms": 10000},
            {"eval": CARD},
            {"shot": str(shots / "revoke_confirm_family_of_four_390.png"), "full": False},
            {"eval": "document.querySelector('.dev-linkbtn').click() || 1"},
            {"eval": CLICK_REVOKE % "pixel"},
            {"until": "document.querySelector('.dev-confirm')", "ms": 10000},
            {"eval": CARD},
            {"shot": str(shots / "revoke_confirm_no_children_390.png"), "full": False},
            {"eval": "document.querySelector('.dev-linkbtn').click() || 1"},
            {"eval": CLICK_REVOKE % "laptop"},
            {"until": "document.querySelector('.dev-confirm')", "ms": 10000},
            {"eval": "document.querySelector('.dev-safe').click() || 1"},
            {"until": "document.querySelectorAll('.dev-list .dev-row').length === 3 && !document.querySelector('.dev-confirm')", "ms": 20000},
            {"eval": "document.querySelector('.dev-note').textContent"},
        ])
        live = _live(httpd)
    v = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)]
    four = json.loads(next(x for x in v if isinstance(x, str) and x.startswith("{")))
    assert sorted(four["items"]) == ["ipad", "iphone", "laptop", "pixel"], four
    assert four["self"] == ["iphone"], four
    assert four["btns"][0].startswith("Revoke only") and "sign this one out" in four["btns"][1], four
    assert four["focus"].startswith("Revoke only"), four
    assert four["overflow"] <= 0, four
    solo = json.loads([x for x in v if isinstance(x, str) and x.startswith("{")][1])
    assert len(solo["btns"]) == 1 and "only" not in solo["btns"][0], solo
    assert sorted(live) == sorted(x[0] for x in kids.values())             # "only" left the three live
    assert "stay paired" in v[-1], v[-1]
