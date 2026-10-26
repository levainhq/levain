"""Q6b: the phone port (``levain consent phone``), a consent-only listener answered with a passkey.

These pin its boundary and its rules: what it serves without the token (a page with no data) and with it (the pending
requests and passkey routes, nothing else), which passkey may answer which request, and what it refuses to be. The
signed allow/deny journey runs in a real browser with a virtual authenticator (project memory Q6B_E2E) and on a
real iPhone (Q6B_PHONE_RUN); a recorded assertion cannot answer a server-issued challenge.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import challenge, public_key_line
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.record import answer_message
from levain.phone_server import PHONE_TOKEN_HEADER, make_phone_server
from tests._h4 import h4_home

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

_FIX = json.loads((Path(__file__).parent / "fixtures" / "passkey_assertion_virtual.json").read_text(encoding="utf-8"))
_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + bytes.fromhex(_FIX["pub_point_hex"])
_TOK = "phone-test-token"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _enrol(conn: sqlite3.Connection, root: Path, laptop: Path, lfp: str, op: dict) -> None:
    auth.sign_and_append(
        conn, op, signer_fp=lfp,
        sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(laptop), "-n", auth.NAMESPACE],
                                      input=m, capture_output=True, check=True).stdout.decode(),
        clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))


@pytest.fixture
def world(tmp_path: Path):
    """A cockpit whose authority log holds a laptop key and the fixture's passkey (rpId localhost, display-phone,
    custody passkey), and a consent queue whose claude_code hook verifies against it."""
    laptop = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    passkey = public_key_line(_SPKI, "localhost")
    pfp = fingerprint_of(passkey)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                     "class": "laptop-presence", "custody": "passphrase"})
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": pfp, "public_key": passkey,
                                     "class": "display-phone", "custody": "passkey"})
    conn.close()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)), home=h4_home())
    seq = iter(range(1, 100))

    def file(tier: str = "T2") -> str:
        n = next(seq)
        return queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                          tool_input={"file_path": f"/tmp/phone{n}.txt", "content": "hi"}, cwd="/w",
                          hold=timedelta(minutes=5), tier=tier, event_id=f"phone-{tier}-{n}").id

    return {"qdir": qdir, "root": root, "passkey_fp": pfp, "passkey": passkey, "file": file}


@contextmanager
def _serving(qdir: Path, name: str = "localhost"):
    httpd = make_phone_server(qdir, name, port=0, token=_TOK, dispatch_dir=qdir)   # never the real dispatch store
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(url: str, body: object = None, *, token: str | None = _TOK, headers: dict | None = None,
          content_type: str = "application/json") -> tuple[int, dict | str]:
    h = dict(headers or {})
    if token is not None:
        h[PHONE_TOKEN_HEADER] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def test_the_page_carries_no_data_and_every_data_route_needs_the_phone_token(world) -> None:
    with _serving(world["qdir"]) as (base, _):
        status, page = _call(base + "/", token=None)
        assert status == 200 and "phone.js" in page and "request" not in page.split("<main")[0].lower()
        assert _call(base + "/phone.js", token=None)[0] == 200
        assert _call(base + "/consent.json", token=None)[0] == 403
        assert _call(base + "/consent.json", token="wrong")[0] == 403
        status, body = _call(base + "/passkey/key-line", {"spki": _b64u(_SPKI)}, token=None)
        assert (status, body["error"]) == (403, "credential_required")
        # nothing of the cockpit is here, with or without the token
        for route in ("/substrate.json", "/cockpit/verb", "/edit", "/cockpit/consent/x.json"):
            assert _call(base + route)[0] == 404
        assert _call(base + "/edit", {"kind": "section"})[0] == 404


def test_host_cross_site_and_content_type_refusals(world) -> None:
    with _serving(world["qdir"], name="box.example.ts.net") as (base, _):
        assert _call(base + "/consent.json", headers={"Host": "box.example.ts.net"})[0] == 200
        assert _call(base + "/consent.json", headers={"Host": "evil.example"})[0] == 403
        assert _call(base + "/consent.json", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        status, _ = _call(base + "/passkey/key-line", {"spki": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403
        assert _call(base + "/passkey/key-line", {"spki": "x"}, content_type="text/plain")[0] == 415


def test_the_listing_names_the_passkeys_enrolled_on_this_name_only(world) -> None:
    rid = world["file"]("T2")
    with _serving(world["qdir"]) as (base, _):
        status, body = _call(base + "/consent.json")
    assert status == 200 and body["rp_id"] == "localhost"
    (row,) = body["pending"]
    assert row["id"] == rid and row["tier"] == "T2" and row["signers"] == [world["passkey_fp"]]
    assert "answer_allow" not in row          # the terminal command is the laptop's, not the phone's
    with _serving(world["qdir"], name="other.example.ts.net") as (base, _):
        (row,) = _call(base + "/consent.json")[1]["pending"]
    assert row["signers"] == [] and "other.example.ts.net" in row["signer_note"]


def test_a_challenge_is_the_answer_record_and_a_passkey_never_allows_t3(world) -> None:
    t2, t3 = world["file"]("T2"), world["file"]("T3")
    fp = world["passkey_fp"]
    with _serving(world["qdir"]) as (base, _):
        status, body = _call(base + f"/consent/{t2}/challenge", {"decision": "allow", "signer_fp": fp})
        assert status == 200 and body["rp_id"] == "localhost" and body["payload"]["signer_fp"] == fp
        assert body["challenge"] == _b64u(challenge(answer_message(body["payload"])))
        status, body = _call(base + f"/consent/{t3}/challenge", {"decision": "allow", "signer_fp": fp})
        assert (status, body["error"]) == (422, "answer_refused") and "T3" in body["message"]
        assert _call(base + f"/consent/{t3}/challenge", {"decision": "deny", "signer_fp": fp})[0] == 200
        status, body = _call(base + f"/consent/{t2}/challenge", {"decision": "allow", "signer_fp": "SHA256:nope"})
        assert (status, body["error"]) == (422, "no_passkey")
        status, body = _call(base + f"/consent/{t2}/challenge", {"decision": "allow"})
        assert (status, body["error"]) == (400, "bad_request")
        assert _call(base + "/consent/nope/challenge", {"decision": "deny", "signer_fp": fp})[0] == 404
    # the same passkey on another name answers nothing
    with _serving(world["qdir"], name="other.example.ts.net") as (base, _):
        status, body = _call(base + f"/consent/{t2}/challenge", {"decision": "deny", "signer_fp": fp},
                             headers={"Host": "other.example.ts.net"})
    assert (status, body["error"]) == (422, "no_passkey")


def test_an_answer_whose_signature_is_not_the_passkeys_is_refused_and_records_nothing(world) -> None:
    rid = world["file"]("T2")
    fp = world["passkey_fp"]
    with _serving(world["qdir"]) as (base, _):
        payload = _call(base + f"/consent/{rid}/challenge", {"decision": "allow", "signer_fp": fp})[1]["payload"]
        status, body = _call(base + f"/consent/{rid}/answer", {
            "payload": payload, "authenticator_data": _b64u(bytes.fromhex(_FIX["authData_hex"])),
            "client_data_json": _b64u(_FIX["clientDataJSON"].encode()), "signature": _b64u(bytes.fromhex(_FIX["sig_der_hex"]))})
        assert (status, body["error"]) == (422, "answer_refused")
        assert _call(base + "/consent.json")[1]["pending"][0]["id"] == rid          # still waiting


def test_key_line_gives_the_key_its_fingerprint_and_the_laptop_enrol_command(world) -> None:
    with _serving(world["qdir"]) as (base, _):
        status, body = _call(base + "/passkey/key-line", {"spki": _b64u(_SPKI)})
    assert status == 200
    assert body["public_key"] == world["passkey"] and body["fingerprint"] == world["passkey_fp"]
    assert any(re.search(r"cockpit enroll passkey-localhost-[0-9a-f]{8}\.pub --class display-phone --custody passkey", c)
               and str(world["root"]) in c for c in body["commands"])


def test_the_phone_port_binds_loopback_and_takes_only_a_dns_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback only"):
        make_phone_server(tmp_path, "box.example.ts.net", host="0.0.0.0", port=0)
    for bad in ("https://box.example/", "127.0.0.1", "Box.Example", "box.example:443"):
        with pytest.raises(ValueError, match="DNS name"):
            make_phone_server(tmp_path, bad, port=0)


def test_a_passkey_key_signs_no_authority_link_but_a_revoke(world) -> None:
    # L3 r2 HIGH: a display-phone key's rank covers a renew or a tighten; its passkey custody refuses all but a revoke
    conn = sqlite3.connect(world["root"] / "cockpit.db", isolation_level=None)
    try:
        with pytest.raises(auth.AuthorityError, match="consent answers and grant revokes only, never another"):
            auth.sign_and_append(conn, {"op": "raise", "verb": "spore_touch", "tier": "T3"},
                                 signer_fp=world["passkey_fp"], sign=lambda m: "not a signature",
                                 clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(),
                                 anchor=anchor_path(world["root"]))
    finally:
        conn.close()


def test_a_passkey_answer_must_be_a_verified_same_origin_webauthn_signature() -> None:
    from levain.consent.passkey import answer_signature_problem, to_sshsig

    key = public_key_line(_SPKI, _FIX["rp_id"])
    auth_data = bytes.fromhex(_FIX["authData_hex"])
    good = to_sshsig(key, auth_data, _FIX["clientDataJSON"].encode(), bytes.fromhex(_FIX["sig_der_hex"]),
                     namespace=_FIX["namespace"])
    assert answer_signature_problem(good, key) is None
    # the same signature with the user-verification bit cleared (a wrapper outside the phone port could send it)
    blob = bytearray(base64.b64decode("".join(good.strip().splitlines()[1:-1])))
    at = blob.rfind(bytes([auth_data[32]]) + auth_data[33:37])
    blob[at] &= ~0x04
    no_uv = "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(bytes(blob)).decode() + "\n-----END SSH SIGNATURE-----\n"
    assert "did not verify the user" in answer_signature_problem(no_uv, key)
    assert answer_signature_problem("garbage", key) is not None


def test_a_passkey_answer_that_is_a_plain_ssh_signature_is_refused(tmp_path: Path) -> None:
    from levain.consent.passkey import answer_signature_problem

    plain = tmp_path / "plain"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(plain)], check=True)
    sig = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(plain), "-n", "levain-consent"],
                         input=b"x", capture_output=True, check=True).stdout.decode()
    assert "WebAuthn" in answer_signature_problem(sig, public_key_line(_SPKI, "localhost"))


def test_redeems_count_only_failures_a_device_store_fault_is_503_and_app_reads_take_the_gate(tmp_path: Path) -> None:
    import os

    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    with _serving(qdir) as (url, httpd):
        for _ in range(25):                                           # a GET never charges the pairing limiter
            assert _call(url + "/webpush/pair/redeem", token=None)[0] == 403
        code = _call(url + "/webpush/pair", {"label": "p"})[1]["code"]
        status, dev = _call(url + "/webpush/pair/redeem", {"code": code}, token=None)
        assert status == 200
        for _ in range(20):
            assert _call(url + "/webpush/pair/redeem", {"code": "x" * 16}, token=None)[0] == 403
        assert _call(url + "/webpush/pair/redeem", {"code": "y" * 16}, token=None)[0] == 429

        assert _call(url + "/webpush/record", token="z" * 32)[0] == 403              # a token no device holds
        db = qdir / "webpush" / "webpush.db"
        os.chmod(db, 0)
        try:
            status, body = _call(url + "/webpush/record", token=dev["token"])     # a paired phone hears "try again"
            assert status == 503 and body["error"] == "unavailable"
        finally:
            os.chmod(db, 0o600)
        assert _call(url + "/webpush/record", token=dev["token"])[0] == 200

        # the server replies before its handler's finally frees the slot, so the last request may still hold one: wait
        held = [httpd.request_gate.acquire(timeout=5) for _ in range(4)]
        try:
            assert all(held)
            assert _call(url + "/webpush/record")[0] == 503 and _call(url + "/app/state.json")[0] == 503
            assert _call(url + "/sw.js", token=None)[0] == 200                       # static stays ungated
        finally:
            for got in held:
                if got:
                    httpd.request_gate.release()


def test_a_device_store_that_cannot_be_stat_ed_is_503_never_an_unknown_token(tmp_path: Path) -> None:
    import os

    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    with _serving(qdir) as (url, _):
        code = _call(url + "/webpush/pair", {"label": "p"})[1]["code"]
        dev = _call(url + "/webpush/pair/redeem", {"code": code}, token=None)[1]
        folder = qdir / "webpush"
        os.chmod(folder, 0)
        try:
            status, body = _call(url + "/webpush/record", token=dev["token"])
            assert status == 503 and body["error"] == "unavailable"
        finally:
            os.chmod(folder, 0o700)
        assert _call(url + "/webpush/record", token=dev["token"])[0] == 200
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    with _serving(empty) as (url, _):
        assert _call(url + "/webpush/record", token="z" * 32)[0] == 403              # no store at all: unknown


def test_the_page_opened_from_another_app_loads_and_its_manifest_is_allowed(tmp_path: Path) -> None:
    from levain.http_guards import _CSP

    assert "manifest-src 'self'" in _CSP
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    with _serving(qdir) as (url, _):
        assert _call(url + "/", token=None, headers=nav)[0] == 200                 # a link opened from another app
        assert _call(url + "/app/state.json", headers=nav)[0] == 403               # a data route stays refused
        assert _call(url + "/phone.js", token=None, headers=nav)[0] == 403         # not a page: refused
        assert _call(url + "/", token=None, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                                                      "Sec-Fetch-Dest": "document"})[0] == 403
        assert _call(url + "/", token=None, headers={**nav, "Host": "evil.example"})[0] == 403
