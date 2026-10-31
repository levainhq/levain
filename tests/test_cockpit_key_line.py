"""ONESURFACE OS-2, the key-line family on the cockpit: ``POST /cockpit/passkey/key-line`` gives a new credential's SPKI
its OpenSSH public key line and the laptop commands that enrol it, by the phone port's OWN method
(``_PhoneServer.key_line``) over the Signer's queue and rpId, behind the sign routes' gate. Ported from the phone port's
tests (tests/test_phone_server.py key-line, tests/test_cockpit_keyfacts.py the attested creation).

Reproduced red before the build: with a Signer attached, ``POST /cockpit/passkey/key-line`` was 404."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from levain.cockpit import keyfacts
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import public_key_line
from tests.test_cockpit_dispatch_sheet import RP, _serve, _signer, world  # noqa: F401  (world is a fixture)
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_dispatch_phone import repo  # noqa: F401  (a fixture the world needs)
from tests._phone_fixtures import _SPKI, _b64u

ROUTE = "/cockpit/passkey/key-line"


def test_key_line_gives_the_key_its_fingerprint_and_the_laptop_enrol_command(world, tmp_path) -> None:
    """Ported: test_phone_server.test_key_line_gives_the_key_its_fingerprint_and_the_laptop_enrol_command."""
    with _serve(world, tmp_path) as (url, _httpd):
        status, body = _call(url, ROUTE, {"spki": _b64u(_SPKI)})
    line = public_key_line(_SPKI, RP)
    assert status == 200
    assert body["public_key"] == line and body["fingerprint"] == fingerprint_of(line)
    assert any(re.search(r"cockpit enroll passkey-phone-[0-9a-f]{8}\.pub --class display-phone --custody passkey", c)
               and str(world["root"]) in c for c in body["commands"]), body
    assert all("--attested" not in c for c in body["commands"]) and body["note"] is None


def test_the_route_needs_the_token_json_and_a_same_origin_page(world, tmp_path) -> None:
    """Ported: the phone's token / cross-site / content-type refusals, on the sign routes' gate."""
    with _serve(world, tmp_path) as (url, _httpd):
        for token in (None, "wrong-token"):
            status, body = _call(url, ROUTE, {"spki": _b64u(_SPKI)}, token=token)
            assert (status, body["error"]) == (403, "credential_required"), (token, status, body)
        assert _call(url, ROUTE, {"spki": "x"}, sfs="cross-site")[0] == 403
        assert _call(url, ROUTE, {"spki": "x"}, ctype="text/plain")[0] == 415


def test_refusals_carry_the_phones_status_and_words(world, tmp_path) -> None:
    with _serve(world, tmp_path) as (url, _httpd):
        status, body = _call(url, ROUTE, {"spki": "x"})
        assert (status, body["error"]) == (422, "bad_passkey"), body
        status, body = _call(url, ROUTE, {"spki": _b64u(_SPKI), "raw_id": "x"})
        assert (status, body["error"]) == (400, "bad_request"), body
        status, body = _call(url, ROUTE, [1])
        assert (status, body["error"]) == (400, "bad_request"), body
        # an authenticator_data that is not this key's creation is refused, never put on a command
        status, body = _call(url, ROUTE, {"spki": _b64u(_SPKI), "authenticator_data": _b64u(b"\0" * 40)})
        assert (status, body["error"]) == (422, "bad_passkey"), body


def test_a_queue_naming_no_cockpit_gets_the_key_and_a_note_and_no_enrol_command(world, tmp_path) -> None:
    lone = tmp_path / "lone-queue"
    lone.mkdir(mode=0o700)
    with _serve(world, tmp_path, signer=_signer(world, qdir=lone, keys=lambda: {})) as (url, _httpd):
        status, body = _call(url, ROUTE, {"spki": _b64u(_SPKI)})
    assert status == 200 and body["public_key"] == public_key_line(_SPKI, RP)
    assert body["commands"] == [f"printf '%s\\n' '{body['public_key']}' > {body['file']}"]
    assert "no consent hook names a cockpit" in body["note"]


def test_without_a_signer_the_route_does_not_exist(tmp_path) -> None:
    with _serving(tmp_path / "inst") as (url, _httpd):
        assert _call(url, ROUTE, {"spki": _b64u(_SPKI)})[0] == 404


@pytest.mark.parametrize("rp", ["phone.example", "dash72.example"])
def test_an_attested_creation_rides_the_enrol_command_and_records_nothing(world, tmp_path, rp) -> None:
    """Ported: test_cockpit_keyfacts.test_the_key_line_puts_a_checked_creation_on_the_enrol_command_and_records_nothing
    (the HTTP half: dash72.example's creation base64url starts with "-", which rides ``--attested=``)."""
    from tests.test_cockpit_keyfacts import Passkey

    from levain.consent.sign import Signer

    pk = Passkey(rp=rp)
    signer = Signer(world["qdir"], ("flow-needs-phill",), rp, lambda: {})
    signer.page_origin = f"https://{rp}"
    with _serving(tmp_path / "inst", signer=signer, extra_hosts=[rp]) as (url, _httpd):
        status, plain = _call(url, ROUTE, {"spki": _b64u(pk.spki)})
        assert status == 200 and all("--attested" not in c for c in plain["commands"])
        status, bad = _call(url, ROUTE, {"spki": _b64u(pk.spki), "authenticator_data": _b64u(Passkey(rp=rp).creation)})
        assert (status, bad["error"]) == (422, "bad_passkey") and "not the key being enrolled" in bad["message"]
        status, got = _call(url, ROUTE, {"spki": _b64u(pk.spki), "authenticator_data": _b64u(pk.creation) + "=="})
    assert status == 200, got
    assert keyfacts.read(world["root"]) == {}                              # the call records nothing
    enrol = [c for c in got["commands"] if c.startswith("levain cockpit enroll")]
    assert enrol, got
    attested = [a for a in shlex.split(enrol[0]) if a.startswith("--attested")]
    assert len(attested) == 1 and attested[0].split("=", 1) == ["--attested", _b64u(pk.creation)]
