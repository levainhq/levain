"""A device revoke is bound to what the operator saw (levain 30, 11-04): the family a revoke takes is named by the
server before it runs (``revoke-preview``), a request carries that set's digest back (``expect_digest``) plus a ``mode``, and a
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


def test_a_stale_digest_moves_nothing_and_names_the_fresh_set(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=tphone, headers=PROXY)
        assert st == 200 and prev["includes_caller"] is True, prev
        seen = sorted(f["id"] for f in prev["family"])
        assert seen == sorted([laptop] + [v[0] for v in kids.values()])
        dg = prev["digests"]["family"]
        _pair(port, token=tl, label="late")     # a fifth device joins after the operator looked
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect_digest": dg, "mode": "family"},
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
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect_digest": prev["digests"]["family"], "mode": "family"}, token=T)
        assert st == 200 and sorted(out["revoked"]) == seen, out
        assert _live(httpd) == {}


def test_only_mode_detaches_the_children_to_the_targets_parent(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        _phone, tphone = kids["iphone"]
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=tphone, headers=PROXY)
        seen = sorted(f["id"] for f in prev["family"])
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect_digest": prev["digests"]["only"], "mode": "only"},
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
        assert _call(port, "POST", REVOKE, {"id": "d-0123456789ab", "expect_digest": "0" * 64, "mode": "family"}, token=T)[0] == 404
        assert _call(port, "POST", REVOKE, {"id": "x", "expect_digest": "0" * 64, "mode": "weird"}, token=T)[0] == 400


def test_an_unhashable_mode_is_a_400_never_a_500(src) -> None:
    """L3 r1 (all three lineages): a list or object mode raised TypeError inside the REVOKE_MODES check."""
    with _serve(src) as (port, httpd):
        laptop, _tl, _kids = _family(port)
        for mode in (["family"], {"m": 1}, 7, None):
            st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect_digest": "0" * 64, "mode": mode}, token=T)
            assert (st, out.get("error")) == (400, "bad_request"), (mode, st, out)
        assert len(_live(httpd)) == 4


def test_only_mode_on_an_already_revoked_target_moves_nothing_and_reports_nothing(src) -> None:
    """L3 r1 (complement 2, deepseek 4): the re-parent ran and committed before the revoke found nothing to revoke."""
    from datetime import datetime, timezone

    with _serve(src) as (port, httpd):
        laptop, _tl, kids = _family(port)
        db = httpd.broker.active_db()
        with db.write() as conn:        # a revoked target whose children are still live (a state no route makes today)
            conn.execute("UPDATE paired_devices SET revoked_at = '2026-11-04T00:00:00Z' WHERE id = ?", (laptop,))
        before = {d["id"]: d["paired_by"] for d in db.list_devices()}
        out = db.revoke_confirmed(laptop, expect_digest=db.revoke_preview(laptop)["digests"]["only"], mode="only", by="browser", now=datetime.now(timezone.utc))
        assert out == {"revoked": [], "detached": []}, out
        assert {d["id"]: d["paired_by"] for d in db.list_devices()} == before      # nobody was re-parented


def test_a_failed_subscribe_undoes_the_new_device_even_when_it_has_gained_a_child(tmp_path, monkeypatch) -> None:
    """L3 r1 (complement 4, deepseek HIGH 2): the undo of a pairing whose subscribe failed has no operator to confirm a
    set, so it must take the device whatever its family is now; a confirm-bound undo refused and left it LIVE."""
    from datetime import datetime, timezone

    from levain.webpush.store import PushStore
    from tests.test_cockpit_webpush import _signed, _sub

    qdir = tmp_path / "signq"
    qdir.mkdir(mode=0o700)
    src2 = _install(tmp_path)[1]
    with _signed(src2, qdir) as (port, httpd):
        def broken(self, endpoint, p256dh, auth, device_id, **kw):
            # the new device pairs a child of its own before the subscribe fails (a second tab, a fast claim)
            now = datetime.now(timezone.utc)
            jdb = httpd.broker.active_db()
            assert jdb.redeem_offer(jdb.mint_offer(label="child", minted_by=device_id, now=now), now=now)
            raise OSError("push store unwritable")

        monkeypatch.setattr(PushStore, "add_subscription", broken)
        st, out, _ = _call(port, "POST", "/webpush/subscribe", _sub(1), token=T)
        assert (st, out.get("error")) == (500, "internal") and "token" not in out, out
        live = _live(httpd)
        assert live == {}, live          # the new device and the child it minted are both revoked


def test_a_family_of_one_hundred_revokes_through_the_confirm_inside_the_pair_body_cap(src) -> None:
    """L3 (codex HIGH x3): the confirm listed every id, so a family of ~58 outgrew the 1 KiB pair-body cap and was
    refused before routing. The digest is the same size for any family."""
    import json

    from levain.web_server import _PAIR_BODY_MAX

    with _serve(src) as (port, httpd):
        laptop, tl = _pair(port, label="laptop")
        for i in range(99):
            _pair(port, token=tl, label=f"kid{i}")
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=T)
        assert st == 200 and len(prev["family"]) == 100, (st, len(prev.get("family", [])))
        body = {"id": laptop, "expect_digest": prev["digests"]["family"], "mode": "family"}
        assert len(json.dumps(body)) < _PAIR_BODY_MAX // 4
        st, out, _ = _call(port, "POST", REVOKE, body, token=T)
        assert st == 200 and len(out["revoked"]) == 100, (st, out.get("error"), out.get("message"))
        assert _live(httpd) == {}


def test_the_digest_differs_by_mode_and_by_set(src) -> None:
    with _serve(src) as (port, _h):
        laptop, tl, kids = _family(port)
        st, a, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=T)
        assert a["digests"]["only"] != a["digests"]["family"]
        assert all(len(v) == 64 for v in a["digests"].values())
        _pair(port, token=tl, label="late")
        st, b, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=T)
        assert b["digests"]["family"] != a["digests"]["family"]


def test_a_digest_for_the_other_mode_is_a_family_changed_never_a_revoke(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, _tl, _kids = _family(port)
        st, prev, _ = _call(port, "POST", PREVIEW, {"id": laptop}, token=T)
        st, out, _ = _call(port, "POST", REVOKE,
                           {"id": laptop, "expect_digest": prev["digests"]["only"], "mode": "family"}, token=T)
        assert (st, out.get("error")) == (409, "family_changed"), (st, out)
        assert len(_live(httpd)) == 4


def test_the_old_expect_list_and_a_malformed_digest_are_refused(src) -> None:
    with _serve(src) as (port, httpd):
        laptop, _tl, _kids = _family(port)
        st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect": [laptop], "mode": "family"}, token=T)
        assert st == 400, (st, out)          # an unknown key, and no digest: nothing moves
        for bad in ("short", 7, None, ["a" * 64], "G" * 64):
            st, out, _ = _call(port, "POST", REVOKE, {"id": laptop, "expect_digest": bad, "mode": "family"}, token=T)
            assert st == 400, (bad, st, out)
        assert len(_live(httpd)) == 4


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
        " badge: [...document.querySelectorAll('.dev-self')].map((n) => n.textContent),"
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
    # the caller is a CHILD of the target: "Revoke only" re-parents it and it stays signed in, so neither that button nor
    # the badge says it will be signed out (head 30, codex LOW r1)
    assert "sign" not in four["btns"][0], four
    assert "you will be signed out" not in four["badge"][0] and "Revoke only" in four["badge"][0], four
    assert four["focus"].startswith("Revoke only"), four
    assert four["overflow"] <= 0, four
    solo = json.loads([x for x in v if isinstance(x, str) and x.startswith("{")][1])
    assert len(solo["btns"]) == 1 and "only" not in solo["btns"][0], solo
    assert sorted(live) == sorted(x[0] for x in kids.values())             # "only" left the three live
    assert "stay paired" in v[-1], v[-1]


WAIT = "new Promise((r) => setTimeout(() => r(1), %d))"
HEAD = "(document.querySelector('.dev-confirm-h') || {}).textContent || null"


@needs_browser
def test_the_card_follows_the_last_click_warns_on_this_device_and_closes_on_an_already_revoked_target(
        tmp_path, monkeypatch, src) -> None:
    """L3 r1: (complement 6) "Revoke only X" on THIS device says it signs this one out; (codex) a slow preview answered
    after a later Revoke or Cancel never reopens the card; (complement 3, deepseek, codex) a target revoked elsewhere
    after its card was drawn closes the card with "already revoked", never "Revoked X"."""
    import time

    from levain.cockpit.firejournal import CockpitDB

    shots = _shots(tmp_path)
    real = CockpitDB.revoke_preview
    ids: dict = {}

    def preview(self, device_id):
        out = real(self, device_id)
        if device_id == ids.get("iphone"):
            time.sleep(2.0)                     # an answer that lands after the operator's next click
        elif device_id == ids.get("ipad"):      # revoked from another device right after its card was drawn
            from datetime import datetime, timezone
            self.revoke_devices([device_id], by="browser", now=datetime.now(timezone.utc))
        return out

    monkeypatch.setattr(CockpitDB, "revoke_preview", preview)
    with _serve(src) as (port, httpd):
        laptop, tl, kids = _family(port)
        ids.update({k: v[0] for k, v in kids.items()})
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={tl}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": CLICK_REVOKE % "laptop"},                                   # the caller IS the target
            {"until": "document.querySelector('.dev-confirm')", "ms": 10000},
            {"eval": CARD},
            {"eval": "document.querySelector('.dev-linkbtn').click() || 1"},
            {"eval": CLICK_REVOKE % "iphone"},                                   # slow preview ...
            {"eval": CLICK_REVOKE % "pixel"},                                    # ... overtaken by this one
            {"until": "document.querySelector('.dev-confirm')", "ms": 10000},
            {"eval": HEAD},
            {"eval": "document.querySelector('.dev-linkbtn').click() || 1"},     # and then Cancel
            {"eval": WAIT % 3000},
            {"eval": "!!document.querySelector('.dev-confirm')"},
            {"eval": CLICK_REVOKE % "ipad"},
            {"until": "document.querySelector('.dev-confirm .dev-danger')", "ms": 10000},
            {"eval": "document.querySelector('.dev-confirm .dev-danger').click() || 1"},
            {"until": "!document.querySelector('.dev-confirm') && document.querySelector('.dev-note').textContent", "ms": 20000},
            {"eval": "document.querySelector('.dev-note').scrollIntoView({block: 'center'}) || 1"},
            {"eval": "document.querySelector('.dev-note').textContent"},
            {"shot": str(shots / "revoke_already_revoked_close_390.png"), "full": False},
        ])
        live = _live(httpd)
    v = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)]
    card = json.loads(next(x for x in v if isinstance(x, str) and x.startswith("{")))
    assert card["self"] == ["laptop"], card
    assert card["btns"][0].startswith("Revoke only") and card["btns"][0].endswith("and sign this one out"), card
    assert card["btns"][1].endswith("and sign this one out"), card
    assert card["badge"] == ["THIS DEVICE: you will be signed out"], card          # the caller IS the target
    heads = [x for x in v if isinstance(x, str) and x.startswith("Revoke ")]
    assert len(heads) == 1 and "pixel" in heads[0], v
    assert False in v, v                     # no card reopened after Cancel
    assert "already revoked" in v[-1] and "Revoked" not in v[-1], v[-1]
    assert ids["ipad"] not in live and len(live) == 3
