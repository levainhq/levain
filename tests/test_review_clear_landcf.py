"""c4mut landing integration L3 fixes (head rulings project_memory/1025-43/landc/RULINGS_INTEGRATION.md).

P1: a listing never shows a request levain filed in its own namespace, so its omission from a listing is not closure:
an open challenge on one survives a concurrent listing read (the cockpit polls /cockpit/sign.json, the phone page
/consent.json) and its answer lands.
P2: a device revoked while its signed clear (or sign request) is in flight commits nothing."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from levain.consent.sign import Signer, read_request
from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE
from levain.coordinator import review
from levain.coordinator.store import CoordStore

from tests.test_cockpit_sign_origin import RP, _Passkey
from tests.test_review_clear_signed import (ORIGIN, _compose, _events, _mine, _q, _send, cockpit,  # noqa: F401
                                            _phone_sign)
from tests.test_cockpit_pairing_http import PROXY, _call
from tests.test_review_clear_signed import FC
from tests.test_cockpit_sign_origin import PINNED
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)


# --- P1 -----------------------------------------------------------------------------------------------------------

def test_a_listing_read_between_challenge_and_answer_keeps_a_levain_requests_challenge(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    s = Signer(qdir, ("flow-needs-phill",), RP, lambda: {pk.key.fingerprint: pk.key})
    ch = s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(qdir, rid).sha256})
    assert all(r["id"] != rid for r in s.page([pk.key.fingerprint])["rows"])     # hidden, and read
    out = s.answer(rid, pk.assertion(ch["challenge"], ORIGIN))
    assert out["namespace"] == REVIEW_CLEAR_NAMESPACE, out


def test_a_listing_still_drops_the_challenge_of_a_request_no_longer_open(tmp_path: Path) -> None:
    qdir, store = _q(tmp_path)
    pk, item = _Passkey(), _mine(store)
    rid = review.file_clear_request(store, item.item_id, queue_dir=qdir)
    s = Signer(qdir, ("flow-needs-phill",), RP, lambda: {pk.key.fingerprint: pk.key})
    s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(qdir, rid).sha256})
    s.deny(rid, {"request_sha256": read_request(qdir, rid).sha256})
    s.page([pk.key.fingerprint])
    assert rid not in s._challenged


def test_the_cockpit_poll_between_challenge_and_answer_does_not_cost_the_signed_clear(cockpit) -> None:  # noqa: F811
    c = cockpit
    rid = _compose(c, c["mine"])[1]["request"]
    st, one, _ = _call(c["port"], "GET", f"/cockpit/sign/{rid}.json", token=FC, headers=PROXY)
    assert st == 200, one
    fp = c["pk"].key.fingerprint
    st, ch, _ = _call(c["port"], "POST", f"/cockpit/sign/{rid}/challenge",
                      {"signer_fp": fp, "request_sha256": one["request"]["sha256"]}, token=FC, headers=PROXY)
    assert st == 200, ch
    st, listed, _ = _call(c["port"], "GET", "/cockpit/sign.json", token=FC, headers=PROXY)    # the page's poll
    assert st == 200 and all(r["id"] != rid for r in listed["requests"]), listed
    st, ans, _ = _call(c["port"], "POST", f"/cockpit/sign/{rid}/answer", c["pk"].assertion(ch["challenge"], PINNED),
                       token=FC, headers=PROXY)
    assert st == 200, ans
    st, done = _send(c, c["mine"], rid)
    assert (st, done.get("mode")) == (200, "signed"), done


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_phone_poll_between_challenge_and_answer_does_not_cost_the_signed_clear(world) -> None:  # noqa: F811
    import threading

    from levain.phone_server import make_phone_server
    from tests.test_coordinator_phone import _post, device_token
    from tests.test_phone_server import _TOK, _call as _phone_call

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    httpd = make_phone_server(world["qdir"], "localhost", port=0, token=_TOK, dispatch_dir=world["qdir"],
                              sign_namespaces=("flow-needs-phill",))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base, port = f"http://127.0.0.1:{httpd.server_address[1]}", httpd.server_address[1]
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        row = got["request"]
        st, ch = _post(base, f"/sign/{row['id']}/challenge", {"signer_fp": world["phone"].fp,
                                                               "request_sha256": row["sha256"]}, dev)
        assert st == 200, ch
        st, listed = _phone_call(base + "/consent.json")                                      # the page's poll
        assert st == 200, listed
        st, ans = _post(base, f"/sign/{row['id']}/answer",
                        world["phone"].assertion(ch["challenge"], f"http://localhost:{port}"), dev)
        assert st == 200 and ans["namespace"] == REVIEW_CLEAR_NAMESPACE, ans
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": row["id"]}, dev)
        assert (st, out.get("mode")) == (200, "signed"), out
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert _events(store, mine.item_id) == [("cleared", "signed", "operator", row["id"])]


# --- P2 -----------------------------------------------------------------------------------------------------------

def _revoke_devices(qdir: Path) -> None:
    from datetime import datetime, timezone

    from levain.webpush import devices
    from levain.webpush.store import PushStore

    conn = PushStore(qdir).connection()
    try:
        for d in devices.list_devices(conn):
            devices.revoke(conn, d["id"], datetime.now(timezone.utc))
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_device_revoked_while_its_signed_clear_verifies_clears_nothing(world, monkeypatch) -> None:  # noqa: F811
    import levain.authority as authority
    from tests.test_coordinator_phone import _post, device_token
    from tests.test_phone_dispatch import _serve

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        verify = authority.verify_link_sig

        def revoked_mid_verify(*a, **kw):        # the device's token passed; it is revoked while the door verifies
            _revoke_devices(world["qdir"])
            return verify(*a, **kw)

        monkeypatch.setattr(authority, "verify_link_sig", revoked_mid_verify)
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
    assert (st, out.get("error")) == (403, "device_revoked"), out
    assert _events(store, mine.item_id) == []
    assert review.get_item(store, mine.item_id).state == "open"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_device_revoked_while_its_sign_request_is_checked_files_nothing(world, monkeypatch) -> None:  # noqa: F811
    import levain.cockpit.verbs as verbs
    from tests.test_coordinator_phone import _post, device_token
    from tests.test_phone_dispatch import _serve

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    refuse = verbs.refuse_raised_review

    def revoked_mid_check(*a, **kw):             # the device's token passed; it is revoked while the policy is read
        _revoke_devices(world["qdir"])
        return refuse(*a, **kw)

    monkeypatch.setattr(verbs, "refuse_raised_review", revoked_mid_check)
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
    assert (st, out.get("error")) == (403, "device_revoked"), out
    assert list((world["qdir"] / "sign").glob("lv-*")) == []
