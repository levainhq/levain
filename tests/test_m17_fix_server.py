"""M17 L3 r1, the server half: what the grants read and the revoke challenge must carry for the pages to hold their
own rules.

M-5 (ruling M17-1): a grant's refusals are counted over its whole instance (``answered.refused_count``), not read off
the newest calls the listing sends, so a refusal older than those still flags the grant.
M-1: the revoke challenge carries the head the link chains to (``prev_hash``), so the page can recompute the WebAuthn
challenge from the line it shows and refuse any other."""
from __future__ import annotations

import json

from levain import authority as auth
from levain.consent.grants import grant_view
from levain.consent.passkey import challenge
from tests.test_dispatch_phone import _b64u
from tests.test_grant_phone import _now, world  # noqa: F401  (world is a fixture)
from tests.test_phone_grants_tab import _answered, _grant


def test_a_refusal_older_than_the_newest_calls_is_still_counted(world):
    """Reproduced red on 7ae3def0: ``answered`` had no ``refused_count`` (KeyError), and the newest 5 calls held no
    refused one, so neither page could tell the grant had a refused call."""
    _grant(world, "cg-0000000000bb", "Edit")
    _grant(world, "cg-0000000000aa", "Write")
    assert _answered(world, "Edit", "r.txt", "refused") == "cg-0000000000bb"
    for i in range(5):
        assert _answered(world, "Edit", f"a{i}.txt", "accepted") == "cg-0000000000bb"
    rows = {g["id"]: g for g in grant_view(world.qdir, now=_now())["grants"]}
    hit, quiet = rows["cg-0000000000bb"]["answered"], rows["cg-0000000000aa"]["answered"]
    assert hit["count"] == 6 and len(hit["recent"]) == 5
    assert [c["outcome"] for c in hit["recent"]] == ["accepted"] * 5          # the refusal fell out of the newest 5
    assert hit["refused_count"] == 1
    assert quiet == {"count": 0, "last_at": "", "recent": [], "refused_count": 0}
    # the phone's listing carries the same count
    phone = {g["id"]: g for g in world.gp.listing()["grants"]}
    assert phone["cg-0000000000bb"]["answered"]["refused_count"] == 1


def test_the_revoke_challenge_carries_the_head_its_link_chains_to(world):
    """Reproduced red on 7ae3def0: the reply had no ``prev_hash``, so the page could not recompute the challenge
    from the line it shows."""
    gid = world.grant("cg-0123456789ab")
    c = world.gp.step(gid, "revoke-challenge", world.shown(gid))
    assert set(c) == {"grant_id", "rp_id", "signer_fp", "line", "prev_hash", "challenge"}
    assert json.loads(c["line"])["grant_id"] == gid
    # the challenge is exactly the SSHSIG blob of (prev_hash, line) in the authority namespace: the page's recompute
    assert c["challenge"] == _b64u(challenge(auth._message(c["prev_hash"], c["line"]), auth.NAMESPACE))
    # and the revoke still lands with an assertion over it
    got = world.gp.step(gid, "revoke", world.phone.assertion(c["challenge"], origin="https://phone.example"))
    assert got["revoked"] == gid
