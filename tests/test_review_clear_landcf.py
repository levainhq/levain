"""c4mut landing integration L3 fixes (head rulings project_memory/1025-43/landc/RULINGS_INTEGRATION.md).

P1: a listing never shows a request levain filed in its own namespace, so its omission from a listing is not closure:
an open challenge on one survives a concurrent listing read (the cockpit polls /cockpit/sign.json, the phone page
/consent.json) and its answer lands.
P2: a device revoked while its signed clear (or sign request) is in flight commits nothing."""
from __future__ import annotations

from pathlib import Path


from levain.consent.sign import Signer, read_request
from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE
from levain.coordinator import review

from tests.test_cockpit_sign_origin import RP, _Passkey
from tests.test_review_clear_signed import ORIGIN, _compose, _events, _mine, _q, _send, cockpit  # noqa: F401
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




# --- P2 -----------------------------------------------------------------------------------------------------------





