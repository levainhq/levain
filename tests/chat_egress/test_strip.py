"""CH-D2 S6: R-5's strip lists, the backstop and `CRED_STRIP_VERSION` (design §3.8.5(o) item 3).

Rows: R15-10-A1 (the backstop strips; table-driven folded and duplicate variants, the Python half: every listed name
and every backstop match is in the allow's `removeHeaders`; the through-srt-to-a-recording-origin half is S9's
composed run), R15-10-A6 / R15-12-B12's strip half (the strip is scheme-independent: plain HTTP strips the same), and
the version pin (a change to any list without a version bump is red: "a change re-signs").
"""
from __future__ import annotations

import pytest

from levain.chat_egress import strip, wire
from levain.chat_egress.parse import parse

from .keys_rig import frame


def _strip(headers, **kw):
    return strip.strip(parse(frame(headers=headers, **kw)))


# --- R15-10-A1: listed names and the backstop, in every spelling srt forwards ------------------------------------

LISTED = ["authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-goog-api-key",
          "ocp-apim-subscription-key", "private-token", "job-token", "x-auth-token", "x-access-token",
          "x-amz-security-token"]
BACKSTOP = ["x_api_key", "x-auth-key", "x-vault-token", "x.client.secret", "x-user-password", "x-passwd",
            "x-my-credential", "signature", "x-session-id", "sessionid", "authx", "x-token"]


@pytest.mark.parametrize("name", LISTED + BACKSTOP)
def test_r15_10_a1_every_listed_and_backstop_name_is_removed(name):
    s = _strip({name: ["v"]})
    assert strip.fold(name) in s.remove_headers and strip.fold(name) in s.stripped
    assert strip.header_is_credential(name) and strip.header_is_credential(name.upper())


def test_r15_10_a1_duplicates_and_case_variants_are_one_removal():
    s = _strip({"authorization": ["Bearer a", "Bearer b"], "x_api_key": ["k"], "x-api-key": ["k2"]})
    assert s.stripped == ("authorization", "x-api-key")
    assert s.remove_headers.count("authorization") == 1 and s.remove_headers.count("x-api-key") == 1


def test_r15_10_a1_the_three_always_go_in_the_removal_even_when_absent():
    s = _strip({"accept": ["*/*"]})
    assert {"authorization", "proxy-authorization", "cookie"} <= set(s.remove_headers)
    assert s.stripped == ()


def test_r15_10_a1_codex_named_edge_x_deploy_key_is_not_stripped():
    """`X-Deploy-Key` matches no list and no backstop word: it is forwarded (a card's header-name list shows it, a
    silent forward under `open network`): the named edge, asserted so a change to it is seen."""
    s = _strip({"x-deploy-key": ["token-from-workspace"]})
    assert "x-deploy-key" not in s.remove_headers and s.stripped == ()
    assert not strip.header_is_credential("X-Deploy-Key")


@pytest.mark.parametrize("name", ["accept", "content-type", "user-agent", "x-request-id", "accept-encoding",
                                  "x-github-api-version", "if-none-match"])
def test_ordinary_headers_are_kept(name):
    assert not strip.header_is_credential(name)
    assert strip.fold(name) not in _strip({name: ["v"]}).stripped


def test_levain_plumbing_is_never_listed():
    """R15-24-2: an `x-levain-` name is the wrapper's, never among the names removed or matched."""
    s = _strip({"x-levain-auth": ["x"]})
    assert s.stripped == () and not any(n.startswith("x-levain-") for n in s.remove_headers)


def test_the_removal_is_a_valid_allow():
    s = _strip({n: ["v"] for n in LISTED + BACKSTOP})
    f = wire.allow_frame(1, s.remove_headers)
    assert set(f["removeHeaders"]) == set(s.remove_headers)


@pytest.mark.parametrize("port, sni", [(80, None), (443, "default")])
def test_r15_12_b12_the_strip_is_the_same_on_plain_http(port, sni):
    s = _strip({"authorization": ["Bearer x"], "cookie": ["a=1"], "x-api-key": ["k"], "x_api_key": ["k"]},
               port=port, sni=sni)
    assert s.stripped == ("authorization", "cookie", "x-api-key")


# --- the query side: names only, never removed (A2); the exemptions are S3a's ---------------------------------

@pytest.mark.parametrize("key", ["access_token", "access%5Ftoken", "api_key", "apikey", "key", "token", "auth",
                                 "private_token", "client_secret", "password", "sig", "signature", "x-goog-signature",
                                 "id_token", "session"])
def test_credential_query_names(key):
    p = parse(frame(query=f"{key}=v"))
    assert strip.credential_query_names(p) == (strip.fold(p.params[0].key),)


@pytest.mark.parametrize("key", ["page_token", "next_token", "continuation_token", "cursor", "pageToken",
                                 "nextPageToken", "q", "page", "per_page", "monkey"])
def test_pagination_and_ordinary_query_names_are_not_credentials(key):
    assert strip.credential_query_names(parse(frame(query=f"{key}=v"))) == ()


def test_listed_query_names_are_known_as_listed():
    assert strip.query_name_is_listed("Access_Token") and not strip.query_name_is_listed("x-amz-signature")


def test_sig_joins_the_query_backstop_only():
    assert strip.query_name_is_credential("sig") and not strip.header_is_credential("sig")


# --- CRED_STRIP_VERSION: a change re-signs -----------------------------------------------------------------------

def test_the_strip_policy_is_pinned_to_its_version():
    assert strip.CRED_STRIP_VERSION == 1
    assert strip.strip_policy()["version"] == strip.CRED_STRIP_VERSION
    assert strip.strip_policy_digest() == strip.PINNED_POLICY_SHA256[strip.CRED_STRIP_VERSION]


def test_a_list_change_without_a_version_bump_is_red(monkeypatch):
    monkeypatch.setattr(strip, "CRED_STRIP_HEADERS", strip.CRED_STRIP_HEADERS | {"x-new-key"})
    assert strip.strip_policy_digest() != strip.PINNED_POLICY_SHA256[strip.CRED_STRIP_VERSION]
