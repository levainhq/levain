"""np-69bb37ba ask 4 (levain#68): on a brokered route the broker mints the credential, and a credential the client
sends (Authorization, x-api-key, in any case) never reaches the upstream. Three layers, each graded here:

  1. `norm.check_headers` with a policy that names the header: FORBIDDEN beats an allow-list ("whatever a policy says",
     norm.py). `authorization` is also in CONSUMED, so removing it from FORBIDDEN alone leaves that name stripped;
     the other names rest on FORBIDDEN alone.
  2. the front's brokered exchange: what it hands keys in `Q` carries none of the client's credential values.
  3. keys' own re-check against a scripted upstream: a client credential in `Q` is refused and no byte reaches the
     upstream, while the credential keys mints is what the upstream does see.

The CONNECT (`egress`) routes have no equivalent and cannot: see the known edge in docs/seat-profile.md, not graded here."""
from __future__ import annotations

import asyncio
import copy
import json
import re

import pytest

from levain.broker import norm
from tests import test_broker_front as TF
from tests import test_broker_keys_upstream as TK

CLIENT_VALUE = "client-sent-CRED-0123456789"
#: the credential-bearing request headers the broker's own injection replaces (creds.INJECT_HEADERS and its kin), in
#: the case variants a client may send; `Authorization` is also the slot ticket's header, handled below.
NAMES = ["x-api-key", "X-Api-Key", "X-API-KEY", "api-key", "Api-Key", "Cookie"]
ALLOW_ALL = {n: re.compile(".*") for n in ("authorization", "x-api-key", "api-key", "cookie", "content-type")}


@pytest.mark.parametrize("name", NAMES + ["authorization", "Authorization", "AUTHORIZATION"])
def test_a_policy_naming_a_credential_header_still_does_not_forward_it(name):
    out = norm.check_headers([("Content-Type", "application/json"), (name, CLIENT_VALUE)], ALLOW_ALL, upgrade_ok=False)
    assert out == [("content-type", "application/json")], out


@pytest.mark.parametrize("name", NAMES)
def test_every_case_variant_of_a_credential_header_is_forbidden(name):
    assert norm.is_forbidden(name.lower())
    out = norm.check_headers([(name, CLIENT_VALUE)], ALLOW_ALL, upgrade_ok=False)
    assert out == []


def _head(extra: bytes, ticket: bytes = b"Bearer " + TF._TOKEN.encode()) -> bytes:
    return (b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: " + ticket + b"\r\n" + extra
            + b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n")


def _loaded_with_credential_names_allowed(monkeypatch) -> dict:
    """A policy the loader refuses today (it names forbidden headers), loaded anyway for this test by turning the
    loader's check off for the load alone: the exchange then runs against a route whose allow-list names every
    credential header, so only FORBIDDEN stands between the client's value and keys."""
    pol = copy.deepcopy(TF._EX_POLICY)
    pol["routes"][0]["requests"]["headers"] = {"content-type": ".*", "x-api-key": ".*", "api-key": ".*",
                                                "cookie": ".*"}
    real_load, real_forbidden = TF.load_policy, norm.is_forbidden

    def load(raw):
        monkeypatch.setattr(norm, "is_forbidden", lambda n: False)
        try:
            return real_load(raw)
        finally:
            monkeypatch.setattr(norm, "is_forbidden", real_forbidden)

    monkeypatch.setattr(TF, "load_policy", load)
    return pol


def test_the_front_hands_keys_no_client_credential(monkeypatch):
    pol = _loaded_with_credential_names_allowed(monkeypatch)
    extra = b"".join(b"%s: %s\r\n" % (n.encode(), CLIENT_VALUE.encode()) for n in ("x-api-key", "api-key", "Cookie"))
    _, got, seen = asyncio.run(TF._exchange(TF._unix_slot_pair, _head(extra), b"{}", None, pol))
    assert got.startswith(b"HTTP/1.1 200 OK\r\n"), got[:200]
    assert [q["headers"] for q in seen] == [[["content-type", "application/json"]]], seen
    assert CLIENT_VALUE not in json.dumps(seen)


@pytest.mark.parametrize("name", ["X-Api-Key", "x-api-key", "X-API-KEY"])
def test_the_stock_route_allow_list_drops_x_api_key_in_any_case(name):
    # The stock route allows only content-type, so this is the allow-list default at work, not the FORBIDDEN guard
    # (that is graded by the loader-bypass test above and by layer 1).
    extra = b"%s: %s\r\n" % (name.encode(), CLIENT_VALUE.encode())
    _, got, seen = asyncio.run(TF._exchange(TF._unix_slot_pair, _head(extra), b"{}"))
    assert got.startswith(b"HTTP/1.1 200 OK\r\n"), got[:200]
    assert [q["headers"] for q in seen] == [[["content-type", "application/json"]]], seen
    assert CLIENT_VALUE not in json.dumps(seen)


@pytest.mark.parametrize("auth", [b"Bearer " + TF._TOKEN.encode(), b"Bearer " + b"B" * 43])
def test_the_front_never_forwards_the_authorization_it_was_sent(auth):
    """The one Authorization a request may carry is the slot ticket: it goes to keys as `Q.token` (keys judges it),
    never as a forwarded header."""
    _, got, seen = asyncio.run(TF._exchange(TF._unix_slot_pair, _head(b"", auth), b"{}"))
    assert len(seen) == 1, seen
    assert seen[0]["token"] == auth[len(b"Bearer "):].decode()
    assert seen[0]["headers"] == [["content-type", "application/json"]], seen


def test_a_second_authorization_is_refused_before_keys():
    extra = b"Authorization: Bearer %s\r\n" % CLIENT_VALUE.encode()
    rec, got, seen = asyncio.run(TF._exchange(TF._unix_slot_pair, _head(extra), b"{}"))
    assert seen == [] and rec.get("refused") in ("token-invalid", "malformed"), (rec, got[:120])
    assert CLIENT_VALUE.encode() not in got


def test_a_client_authorization_sent_before_the_ticket_is_refused_before_keys():
    head = (b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer " + CLIENT_VALUE.encode()
            + b"\r\nAuthorization: Bearer " + TF._TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n")
    rec, got, seen = asyncio.run(TF._exchange(TF._unix_slot_pair, head, b"{}"))
    assert CLIENT_VALUE not in json.dumps(seen) and CLIENT_VALUE.encode() not in got
    assert seen == [] and rec.get("refused"), (rec, got[:120])


@pytest.mark.parametrize("name", ["x-api-key", "authorization", "Authorization", "X-Api-Key"])
def test_keys_refuses_a_client_credential_and_sends_the_upstream_nothing(monkeypatch, name):
    up = TK._Up()

    async def go(d: str) -> list:
        TK._scripted(monkeypatch, up)
        async with TK._Rig(d) as rig:
            rig.front.feed_data(TK.body_frames())
            q = TK._q()
            q["headers"] = [["content-type", "application/json"], [name, CLIENT_VALUE]]
            await asyncio.wait_for(rig.run(q), 5)
            return await rig.got()

    frames = TK._run(go)
    assert b"".join(up.writes).find(CLIENT_VALUE.encode()) == -1 and up.writes == [], up.writes
    assert b"policy-header" in b"".join(p for _, p in frames), frames


def test_keys_sends_the_upstream_the_credential_it_minted_and_only_that(monkeypatch):
    up = TK._Up()

    async def go(d: str) -> None:
        TK._scripted(monkeypatch, up)
        async with TK._Rig(d) as rig:
            rig.front.feed_data(TK.body_frames())
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
            await asyncio.wait_for(rig.run(TK._q()), 5)
            await rig.got()

    TK._run(go)
    head = b"".join(up.writes).lower()
    assert head.count(b"x-api-key:") == 1 and TK.SECRET.encode().lower() in head, up.writes
    assert CLIENT_VALUE.encode().lower() not in head and b"authorization:" not in head
