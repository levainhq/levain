"""ONESURFACE OS-2, the coordinator drop family on the cockpit: a drop (words, optionally with an envelope signed with a
passkey) sent through the cockpit server, by the phone port's OWN object (:class:`levain.coordinator.phone.CoordPhone`, built
by :class:`levain.web_server.CoordSheet` over the Signer's queue, keys, rpId and pinned page). These are the phone port's
coordinator route tests (tests/test_coordinator_phone.py's HTTP ones: the drop route, the credential gate on every route,
the wake through the server) ported onto ``/cockpit/coord/<action>``, run against a real authority log (a laptop key and a
software passkey enrolled on ``phone.example``), a real coordinator store and real ``ssh-keygen`` verification; plus the
cockpit's own: the sign routes' gate, the sheet's read and page, and the refusal before any envelope ceremony on a page the
verification would refuse. The phone's CoordPhone-level tests (stale, store identity, digest table, ...) run the very class
this cockpit serves and are not repeated.

Reproduced red before the build: with a Signer attached, ``POST /cockpit/coord/drop`` and
``GET /cockpit/coord/drops.json`` were 404 and ``/sign/drop`` was no page."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.envelopes import EnvelopeStore
from levain.dispatch.jobs import DB_NAME
from tests.test_cockpit_dispatch_sheet import (NS, PAGE, PORTED, RP, _serve, _signer, world)  # noqa: F401  (world: fixture)
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_dispatch_phone import repo  # noqa: F401  (a fixture the world needs)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SENT = "2026-01-01T00:00:00.000000Z"       # a sent_at in the store's past
ROOT = Path(__file__).resolve().parents[1]


def _spec(world: Any) -> "dict[str, Any]":
    repo_path = str(world["repo"][0])
    return {"where": [{"repo": repo_path, "paths": ["."]}], "lands": [{"repo": repo_path, "onto": "main"}],
            "stop": "2h", "budget": {"tool_calls": 40}}


def _plain(oid: str, words: str = "from the couch", store_id: Any = None) -> "dict[str, Any]":
    return {"words": words, "outbox_id": oid, "sent_at": SENT, "store_id": store_id}


def _review_and_sign(url: str, world: Any, words: str, oid: str, store_id: Any, *, origin: str = PAGE):
    """The sheet's calls: the review, then Sign (the challenge and the passkey's assertion). Returns (review, envelope)."""
    key = world["phone"]
    status, r = _call(url, "/cockpit/coord/envelope-review", {"words": words, "outbox_id": oid, "spec": _spec(world),
                                                              "signer_fp": key.fp, "store_id": store_id})
    assert status == 200, r
    status, c = _call(url, "/cockpit/coord/envelope-challenge", {"review_id": r["review_id"]})
    assert status == 200, c
    assert c["line"] == env.sign_message(c["payload"]).decode("utf-8")         # the exact text signed, shown by the sheet
    return r, {"body_text": r["body_text"], "payload": c["payload"], **key.assertion(c["challenge"], origin)}


def test_the_listing_makes_no_store_then_shows_the_drop_with_its_receipt_and_the_sheet_is_a_page(world, tmp_path) -> None:
    """Ported: test_coordinator_phone's listing assertions (a read makes no store; the drop and the store identity)."""
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        status, body = _call(url, "/cockpit/coord/drops.json")
        assert status == 200 and body["coord_drops"] == [] and body["coord_note"] is None, body
        assert body["coord_signers"] == [key.fp] and body["origin"] == PAGE and body["rp_id"] == RP
        assert body["coord_store_read"] is True and body["coord_store_id"] is None and body["credentials"] == {}
        assert not (world["qdir"] / DB_NAME).exists()                            # a read makes no store
        status, got = _call(url, "/cockpit/coord/drop", _plain("ph-" + "1" * 32))
        assert status == 200 and got["created"] is True and got["drop"]["surface"] == "phone", got
        listed = _call(url, "/cockpit/coord/drops.json")[1]
        assert [d["drop_id"] for d in listed["coord_drops"]] == [got["drop"]["drop_id"]]
        assert [r["stage"] for r in listed["coord_drops"][0]["receipts"]] == ["delivered"]
        assert listed["coord_store_id"] == got["store_id"] == EnvelopeStore(world["qdir"]).store_id()
        # the sheet's page is the same shell as the sign sheet's
        status, page = _call(url, "/sign/drop", token=None)
        assert status == 200 and b"/sign.js" in page


def test_the_drop_route_delivers_through_the_server_and_a_resend_files_once(world, tmp_path) -> None:
    """Ported: test_coordinator_phone.test_the_drop_route_needs_a_paired_device_and_delivers_through_the_server."""
    body = _plain("ph-" + "5" * 32)
    with _serve(world, tmp_path) as (url, _httpd):
        assert _call(url, "/cockpit/coord/drop", body, token="wrong")[0] == 403
        status, got = _call(url, "/cockpit/coord/drop", body)
        assert status == 200 and got["created"] is True and got["drop"]["surface"] == "phone"
        assert got["store_id"] == EnvelopeStore(world["qdir"]).store_id()
        status, again = _call(url, "/cockpit/coord/drop", {**body, "store_id": got["store_id"]})
        assert status == 200 and again["created"] is False and again["drop"]["drop_id"] == got["drop"]["drop_id"]
        # the same outbox id for other words is a conflict and writes nothing
        status, bad = _call(url, "/cockpit/coord/drop", {**body, "words": "other", "store_id": got["store_id"]})
        assert (status, bad["error"]) == (409, "conflict"), bad
    store = EnvelopeStore(world["qdir"])
    assert [d.drop_id for d in store.drops()] == [got["drop"]["drop_id"]]
    assert store.drop(got["drop"]["drop_id"]).words == "from the couch"


@pytest.mark.parametrize("action", ["drop", "envelope-review", "envelope-challenge"])
def test_the_gate_refuses_every_coordinator_route_and_writes_nothing(world, tmp_path, action) -> None:
    """Ported: test_r1_the_run_token_is_refused_on_every_coordinator_route_and_writes_nothing, with the cockpit's own
    gate (the sign routes': a token on every bind, JSON only, same-origin)."""
    bodies = {"drop": _plain("ph-" + "6" * 32),
              "envelope-review": {"words": "w", "outbox_id": "ph-" + "6" * 32, "signer_fp": world["phone"].fp,
                                  "store_id": None, "spec": _spec(world)},
              "envelope-challenge": {"review_id": "rv-" + "0" * 32}}
    path = f"/cockpit/coord/{action}"
    with _serve(world, tmp_path) as (url, _httpd):
        for token in (None, "wrong-token"):
            status, body = _call(url, path, bodies[action], token=token)
            assert (status, body.get("error")) == (403, "credential_required"), (token, status, body)
        assert _call(url, path, bodies[action], sfs="cross-site")[0] == 403
        assert _call(url, path, bodies[action], ctype="text/plain")[0] == 415
        assert _call(url, "/cockpit/coord/nothing", bodies[action])[0] == 404
        assert _call(url, "/cockpit/coord/drops.json", token=None)[0] == 403
        assert _call(url, "/cockpit/coord/drops.json", sfs="cross-site")[0] == 403
    assert not (world["qdir"] / DB_NAME).exists()


def test_no_coordinator_route_is_served_without_a_signer(tmp_path) -> None:
    with _serving(tmp_path / "inst", extra_hosts=[RP]) as (url, httpd):
        assert httpd.coord_sheet is None
        assert _call(url, "/cockpit/coord/drop", _plain("ph-" + "7" * 32))[0] == 404
        assert _call(url, "/cockpit/coord/drops.json")[0] == 404
        assert _call(url, "/sign/drop", token=None)[0] == 404


def test_an_envelope_is_signed_with_a_passkey_and_the_store_takes_it(world, tmp_path) -> None:
    """The sheet's ceremony over HTTP: review, challenge, the passkey's assertion, the drop carrying the envelope. The
    store's own verification (ssh-keygen) accepts it; a replay under another outbox id is a conflict, never filed plain."""
    words, oid = "build it and land it", "ph-" + "e" * 32
    with _serve(world, tmp_path) as (url, _httpd):
        r, envelope = _review_and_sign(url, world, words, oid, None)
        assert r["store_created"] is True and r["rp_id"] == RP and r["digest"].startswith("sha256:")
        assert any(line.startswith("NOT IN FORCE YET") for line in r["view"]), r["view"]
        status, got = _call(url, "/cockpit/coord/drop", {**_plain(oid, words, r["store_id"]), "envelope": envelope})
        assert status == 200 and got["envelope"]["taken"] is True and got["created"] is True, got
        # the reply is lost and the sheet sends it again, unchanged: the stored drop and its envelope, once
        status, again = _call(url, "/cockpit/coord/drop", {**_plain(oid, words, r["store_id"]), "envelope": envelope})
        assert status == 200 and again["created"] is False and again["envelope"]["taken"] is True
        # the same signed envelope under another outbox id is refused and filed nowhere
        status, replay = _call(url, "/cockpit/coord/drop", {**_plain("ph-" + "f" * 32, words, r["store_id"]),
                                                            "envelope": envelope})
        assert status in (409, 422), replay
    store = EnvelopeStore(world["qdir"])
    assert len(store.drops()) == 1
    stored = store.envelope(got["envelope"]["envelope_id"])
    assert stored.signer_class == "display-phone" and stored.digest == r["digest"] and stored.drop_id == got["drop"]["drop_id"]


def test_a_review_names_no_store_it_cannot_sign_for_and_a_challenge_needs_its_review(world, tmp_path) -> None:
    """Ported: r2 (an item for another store is refused, nothing written), r4 (a challenge for a review no longer held is
    409), and a challenge for an outbox id already delivered signs nothing."""
    key = world["phone"]
    with _serve(world, tmp_path) as (url, _httpd):
        got = _call(url, "/cockpit/coord/drop", _plain("ph-" + "a" * 32))[1]
        review = {"words": "w", "outbox_id": "ph-" + "b" * 32, "spec": _spec(world), "signer_fp": key.fp}
        status, out = _call(url, "/cockpit/coord/envelope-review", {**review, "store_id": "st-another"})
        assert (status, out["error"]) == (409, "other_store"), out
        status, out = _call(url, "/cockpit/coord/envelope-review", {**review, "store_id": None})   # a store exists now
        assert (status, out["error"]) == (409, "other_store"), out
        status, out = _call(url, "/cockpit/coord/envelope-review",
                            {**review, "outbox_id": "ph-" + "a" * 32, "store_id": got["store_id"]})
        assert (status, out["error"]) == (409, "conflict"), out                    # already delivered: nothing to sign
        status, out = _call(url, "/cockpit/coord/envelope-challenge", {"review_id": "rv-" + "0" * 32})
        assert (status, out["error"]) == (409, "review_gone"), out
        status, out = _call(url, "/cockpit/coord/envelope-review",                 # a malformed body is the phone's 400
                            {"words": "w"})
        assert (status, out["error"]) == (400, "bad_request"), out
    assert len(EnvelopeStore(world["qdir"]).drops()) == 1


def test_an_assertion_from_another_origin_signs_nothing_and_writes_nothing(world, tmp_path) -> None:
    """Ported: test_an_assertion_from_another_origin_signs_nothing_and_writes_nothing."""
    with _serve(world, tmp_path) as (url, _httpd):
        r, envelope = _review_and_sign(url, world, "w", "ph-" + "4" * 32, None, origin=PORTED)
        status, out = _call(url, "/cockpit/coord/drop", {**_plain("ph-" + "4" * 32, "w", r["store_id"]),
                                                         "envelope": envelope})
        assert status == 422, out
    assert EnvelopeStore(world["qdir"]).drops() == []


def test_a_widened_body_under_the_signature_is_refused_and_never_stored_plain(world, tmp_path) -> None:
    """Ported: test_a_widened_body_under_the_phone_signature_is_refused_and_never_stored_plain."""
    from levain.contracts.canon import canonical

    with _serve(world, tmp_path) as (url, _httpd):
        r, envelope = _review_and_sign(url, world, "narrow work", "ph-" + "3" * 32, None)
        wide = canonical({**env.parse_body(r["body_text"]), "where": [{"repo": str(world["repo"][0]), "paths": ["/"]}]})
        status, out = _call(url, "/cockpit/coord/drop", {**_plain("ph-" + "3" * 32, "narrow work", r["store_id"]),
                                                         "envelope": {**envelope, "body_text": wide}})
        assert status == 422, out
        assert EnvelopeStore(world["qdir"]).drops() == []
        # "send as a plain drop": the same outbox id, no envelope, is then delivered
        status, got = _call(url, "/cockpit/coord/drop", _plain("ph-" + "3" * 32, "narrow work", r["store_id"]))
        assert status == 200 and got["envelope"] is None


@pytest.mark.parametrize("pin", [None, PORTED])
def test_an_envelope_ceremony_is_refused_before_any_ceremony_on_a_page_that_cannot_verify_it(world, tmp_path, pin) -> None:
    """Ported from the dispatch family's rule: a cockpit pinned to no page, or to a ported origin, signs no envelope
    (the review and the challenge are refused before a passkey is asked, and a drop carrying one is not delivered); a
    plain drop signs nothing and is delivered; the read says why."""
    signer = _signer(world, origin=PORTED if pin is None else pin)
    with _serve(world, tmp_path, signer=signer) as (url, httpd):
        httpd.coord_sheet.page_origin = pin      # a server cannot start unpinned; the sheet is unpinned after
        problem = httpd.coord_sheet.elsewhere()
        assert problem and ("not pinned" in problem if pin is None else "no port" in problem), problem
        assert _call(url, "/cockpit/coord/drops.json")[1]["elsewhere"] == problem
        review = {"words": "w", "outbox_id": "ph-" + "9" * 32, "spec": _spec(world), "signer_fp": world["phone"].fp,
                  "store_id": None}
        status, out = _call(url, "/cockpit/coord/envelope-review", review)
        assert (status, out["error"]) == (422, "sign_refused") and "nothing was signed" in out["message"], out
        status, out = _call(url, "/cockpit/coord/envelope-challenge", {"review_id": "rv-" + "0" * 32})
        assert (status, out["error"]) == (422, "sign_refused"), out
        junk = {"body_text": "{}", "payload": {}, "authenticator_data": "AAAA", "client_data_json": "AAAA",
                "signature": "AAAA"}
        status, out = _call(url, "/cockpit/coord/drop", {**_plain("ph-" + "9" * 32), "envelope": junk})
        assert (status, out["error"]) == (422, "sign_refused") and "not delivered" in out["message"], out
        assert not (world["qdir"] / DB_NAME).exists()
        status, out = _call(url, "/cockpit/coord/drop", _plain("ph-" + "9" * 32))
        assert status == 200 and out["created"] is True


def test_the_coord_sheet_is_the_signers_queue(world, tmp_path) -> None:
    """Ported: the dispatch directory is the Signer's queue (the keys offered and the store written share one directory)."""
    with _serve(world, tmp_path) as (_url, httpd):
        assert httpd.coord_sheet.phone.dispatch_dir == world["qdir"] == httpd.coord_sheet.queue_dir
        assert httpd.coord_sheet.phone.page_origin == PAGE and httpd.coord_sheet.phone.rp_id == RP


def test_through_the_server_the_real_launch_session_records_its_refusal(world, tmp_path) -> None:
    """Ported: test_r8_through_the_server_the_real_launch_session_records_its_refusal: an accepted drop starting the
    coordinator launch it asked for, the exact call `levain drop` makes, on an install whose hook is not live."""
    d = tmp_path / "coord-install"
    d.mkdir()
    EnvelopeStore(world["qdir"]).coord_set_agent("claude", model="haiku", cwd=str(d.resolve()))
    sid_now = EnvelopeStore(world["qdir"]).store_id()
    with _serve(world, tmp_path) as (url, httpd):
        status, got = _call(url, "/cockpit/coord/drop", _plain("ph-" + "c8" * 16, "wake from the cockpit", sid_now))
        assert status == 200 and got["woken"] is not None, got
        httpd.coord_sheet.phone.wait_wakes(30)
    (w,) = [r.body for r in EnvelopeStore(world["qdir"]).drop_receipts(got["drop"]["drop_id"])
            if r.body.get("stage") == "woken"]
    assert w["launch_id"] == got["woken"] and w["started"] is False and w["said"], w


def test_the_sheet_scripts_parse_and_name_the_routes() -> None:
    """The page's code is two served files; nothing here drives a browser (see the return's residue): they parse, and the
    drop sheet names the phone's routes and the sheet's page."""
    sign = (ROOT / "levain/templates/web/sign.js").read_text()
    core = (ROOT / "levain/templates/web/dashboard_core.js").read_text()
    for route in ("/cockpit/coord/drops.json", '"/cockpit/coord/" + action', '"envelope-review"', '"envelope-challenge"',
                  "/sign/drop"):
        assert route in sign or route in core, route
    assert "openDrop: openDropSheet" in core and "/cockpit/coord/drops.json" in core
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node to parse the scripts")
    for name in ("sign.js", "dashboard_core.js"):
        out = subprocess.run([node, "--check", str(ROOT / "levain/templates/web" / name)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
