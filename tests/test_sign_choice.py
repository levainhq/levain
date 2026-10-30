"""A choice sign request (levain-sign-choice/1): one card, several options, the chosen option's line bound into the
signed bytes, so a verifier reads the decision from the bytes stock ``ssh-keygen -Y verify`` accepted. Built from the
1020+36 run (a software passkey through the real Signer, then stock ssh-keygen); and the signed-answers setting."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.authority import AuthorityKey
from levain.cockpit.keys import fingerprint_of
from levain.consent import sign_setting
from levain.consent.passkey import public_key_line
from levain.consent.sign import CHOICE_MARK, Signer, SignRefused, choice_line, deep_link, parse_choice

RP, ORIGIN, NS = "localhost", "http://localhost:7999", "flow-needs-phill"
MESSAGE = "flow needs-phill answer\nitem: np-run0001\nasked: scratch run of the choice card\nnonce: c9f2a1"
OPTIONS = [{"key": "a", "text": "keep it"}, {"key": "b", "text": "drop it"}]


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _file(qdir: Path, rid: str, *, message: str = MESSAGE, options: object = OPTIONS, ns: str = NS,
          **extra: object) -> None:
    sign = qdir / "sign"
    sign.mkdir(exist_ok=True)
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": ns, "message": message, "title": "choice",
            "requester": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    if options is not None:
        body.update(format="levain-sign-choice/1", options=options, **extra)
    (sign / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(sign / f".{rid}.tmp", sign / f"{rid}.json")


@pytest.fixture
def passkey(tmp_path: Path):
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    line = public_key_line(spki, RP)
    key = AuthorityKey(fingerprint=fingerprint_of(line), public_key=line, key_class="display-phone", enrolled_seq=1)
    signer = Signer(tmp_path, (NS,), RP, lambda: {key.fingerprint: key})
    signer.page_origin = ORIGIN

    def assertion(challenge: str) -> dict:
        auth = hashlib.sha256(RP.encode()).digest() + bytes([0x05]) + (1).to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": ORIGIN, "crossOrigin": False},
                         separators=(",", ":")).encode()
        der = priv.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"authenticator_data": _b64u(auth), "client_data_json": _b64u(cdj), "signature": _b64u(der)}

    def sign(rid: str, choice: "str | None", words: "str | None" = None) -> dict:
        row = next(r for r in signer.listing([key.fingerprint]) if r["id"] == rid)
        req = {"signer_fp": key.fingerprint, "request_sha256": row["sha256"]}
        if choice is not None:
            req["choice"] = choice
        if words is not None:
            req["write_in"] = words
        return signer.answer(rid, assertion(signer.challenge(rid, req)["challenge"]))

    sign.assertion = assertion

    (tmp_path / "allowed").write_text(f'x namespaces="{NS}" {line}\n')
    return signer, key, sign


def _verify(tmp_path: Path, rid: str, data: bytes, ns: str = NS) -> int:
    return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(tmp_path / "allowed"), "-I", "x", "-n", ns,
                           "-s", str(tmp_path / "sign" / f"{rid}.sig")], input=data, capture_output=True).returncode


def test_the_chosen_option_is_in_the_bytes_stock_ssh_keygen_verifies(tmp_path: Path, passkey) -> None:
    """The run: option (b) signed; Good over the signed message, and the decision read from those bytes alone. One
    changed byte, another namespace, option (a)'s bytes and the bare message all fail."""
    signer, key, sign = passkey
    _file(tmp_path, "np-run0001-c9f2a1")
    [row] = signer.listing([key.fingerprint])
    assert row["options"] == [{**o, "line": choice_line(o["key"], o["text"])} for o in OPTIONS]
    assert sign("np-run0001-c9f2a1", "b")["choice"] == "b"
    rec = json.loads((tmp_path / "sign" / "np-run0001-c9f2a1.signed.json").read_text())
    signed = rec["signed_message"].encode()
    assert rec["choice"] == "b" and hashlib.sha256(signed).hexdigest() == rec["message_sha256"]
    assert _verify(tmp_path, "np-run0001-c9f2a1", signed) == 0
    assert parse_choice(signed) == (MESSAGE, "b", "drop it")
    changed = bytearray(signed)
    changed[5] ^= 0x01
    assert _verify(tmp_path, "np-run0001-c9f2a1", bytes(changed)) != 0
    assert _verify(tmp_path, "np-run0001-c9f2a1", signed, "claphamdigital-care-report") != 0
    assert _verify(tmp_path, "np-run0001-c9f2a1", (MESSAGE + "\n" + choice_line("a", "keep it")).encode()) != 0
    assert _verify(tmp_path, "np-run0001-c9f2a1", MESSAGE.encode()) != 0


def test_a_plain_request_signs_its_message_as_before(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, "plain-0001", options=None)
    assert "choice" not in sign("plain-0001", None)
    rec = json.loads((tmp_path / "sign" / "plain-0001.signed.json").read_text())
    assert set(rec) == {"signer_fp", "public_key", "namespace", "message_sha256", "signed_at"}
    assert _verify(tmp_path, "plain-0001", MESSAGE.encode()) == 0


def test_a_challenge_names_exactly_one_offered_option(tmp_path: Path, passkey) -> None:
    signer, key, _ = passkey
    _file(tmp_path, "np-run0002-aaaa")
    _file(tmp_path, "plain-0002", options=None)
    sha = {r["id"]: r["sha256"] for r in signer.listing([key.fingerprint])}
    for rid, choice in (("np-run0002-aaaa", None), ("np-run0002-aaaa", "c"), ("plain-0002", "a")):
        req = {"signer_fp": key.fingerprint, "request_sha256": sha[rid]}
        if choice is not None:
            req["choice"] = choice
        with pytest.raises(SignRefused) as refused:
            signer.challenge(rid, req)
        assert refused.value.status == 400
    assert signer._challenged == {}


@pytest.mark.parametrize("message,options,why", [
    ("x " + CHOICE_MARK + "(a): yes", OPTIONS, "only levain writes"),
    (MESSAGE, [{"key": "a", "text": "one"}], "2 to 8"),
    (MESSAGE, [{"key": "a", "text": "one"}, {"key": "a", "text": "two"}], "distinct"),
    (MESSAGE, [{"key": "a", "text": "same"}, {"key": "b", "text": "same"}], "distinct"),
    (MESSAGE, [{"key": "a", "text": "one\nchosen"}, {"key": "b", "text": "two"}], "U+000A"),
    (MESSAGE, [{"key": "A", "text": "one"}, {"key": "b", "text": "two"}], "[a-z0-9]"),
])
def test_a_choice_that_could_read_two_ways_is_refused(tmp_path: Path, passkey, message, options, why) -> None:
    signer, key, _ = passkey
    _file(tmp_path, "np-bad0001-aaaa", message=message, options=options)
    [row] = signer.listing([key.fingerprint])
    assert why in row["refused"] and "options" not in row


def test_a_plain_message_holding_the_choice_mark_never_verifies_as_a_choice(tmp_path: Path, passkey) -> None:
    signer, key, _ = passkey
    _file(tmp_path, "plain-forge1", message="I sign this.\n" + choice_line("a", "merge it"), options=None)
    [row] = signer.listing([key.fingerprint])
    assert "only levain writes" in row["refused"]
    with pytest.raises(ValueError):
        parse_choice(("x\n" + choice_line("a", "y") + "\n" + choice_line("b", "z")).encode())


def test_the_deep_link_opens_the_sheet_and_carries_no_secret() -> None:
    # ONESURFACE OS-4: the cockpit's sheet on the one origin, the id in the path; a link never carries a token
    assert deep_link("https://h.ts.net:8443/", "np-run0001-c9f2a1") == "https://h.ts.net:8443/sign/np-run0001-c9f2a1"
    assert deep_link("https://h.ts.net:8443", "np-run0001-c9f2a1") == "https://h.ts.net:8443/sign/np-run0001-c9f2a1"
    with pytest.raises(TypeError):
        deep_link("https://h.ts.net/", "np-run0001-c9f2a1", "a/b")     # no token argument any more
    for bad in ("x&phone_token=evil", "../../x", "np-run0001-c9f2a1#v=0"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", bad)
    for page in ("https://h.ts.net/#old", "https://h.ts.net/phone.html", "https://h.ts.net/?t=1", "h.ts.net",
                 "https://u@h.ts.net/", "https://h ts.net/", "https://a.com\\.b/", "https://box.ts.net\\evil",
                 "https://good..com", "http://evil.example", "https://10.0.0.1", "https://h.ts.net:0"):
        with pytest.raises(ValueError):
            deep_link(page, "np-run0001-c9f2a1")
    # the rule the redirect's target is configured by (phone_server.redirect_origin), stated once
    assert deep_link("HTTP://LocalHost:8080/", "np-run0001-c9f2a1") == "http://localhost:8080/sign/np-run0001-c9f2a1"


def test_the_signed_answers_setting_reads_off_authority_only_or_all(tmp_path: Path) -> None:
    assert sign_setting.read_setting(tmp_path) == ("authority-only", "default", "")
    for value in sign_setting.VALUES:
        sign_setting.write_setting(tmp_path, value)
        assert sign_setting.read_setting(tmp_path) == (value, "file", "")
    assert oct((tmp_path / sign_setting.SETTING_NAME).stat().st_mode & 0o777) == "0o600"
    (tmp_path / sign_setting.SETTING_NAME).write_text('{"signed_answers": "nope"}')
    value, source, note = sign_setting.read_setting(tmp_path)
    assert (value, source) == ("authority-only", "default") and "the default stands" in note
    with pytest.raises(ValueError):
        sign_setting.write_setting(tmp_path, "sometimes")
    assert [sign_setting.should_sign(v, a) for v in sign_setting.VALUES for a in (False, True)] == \
        [False, False, False, True, True, True]


def test_the_cli_shows_and_sets_it(tmp_path: Path, capsys) -> None:
    from levain.cli import main

    assert main(["consent", "signed-answers", "--queue-dir", str(tmp_path), "--set", "off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"signed_answers": "off", "source": "file", "note": ""}


def test_a_kept_token_survives_a_restart(tmp_path: Path) -> None:
    """The 1020+36 restart minted a new token and the phone's open page lost its link: --token-file keeps it."""
    from levain.phone_server import _kept_token

    path = tmp_path / "token"
    first = _kept_token(path)
    assert _kept_token(path) == first and oct(path.stat().st_mode & 0o777) == "0o600"
    path.write_text("short\n")
    with pytest.raises(ValueError, match="holds no token"):
        _kept_token(path)


def test_an_answer_for_option_a_after_a_challenge_for_b_records_nothing(tmp_path: Path, passkey) -> None:
    """L1 r1: a real passkey assertion over option (a)'s challenge, answered while (b) is the bound one, verifies over
    (b)'s bytes or not at all: it is refused and nothing is recorded; (b) then signs as (b)."""
    signer, key, sign = passkey
    _file(tmp_path, "np-ab000001-aaaa")
    [row] = signer.listing([key.fingerprint])
    base = {"signer_fp": key.fingerprint, "request_sha256": row["sha256"]}
    ca = signer.challenge("np-ab000001-aaaa", {**base, "choice": "a"})
    cb = signer.challenge("np-ab000001-aaaa", {**base, "choice": "b"})
    assert ca["challenge"] != cb["challenge"]
    with pytest.raises(SignRefused):
        signer.answer("np-ab000001-aaaa", sign.assertion(ca["challenge"]))
    assert not (tmp_path / "sign" / "np-ab000001-aaaa.signed.json").exists()
    assert signer.answer("np-ab000001-aaaa", sign.assertion(cb["challenge"]))["choice"] == "b"


def test_an_option_padded_with_spaces_is_refused(tmp_path: Path, passkey) -> None:
    signer, key, _ = passkey
    _file(tmp_path, "np-pad00001-aaaa", options=[{"key": "a", "text": "approve"}, {"key": "b", "text": "approve "}])
    [row] = signer.listing([key.fingerprint])
    assert "space" in row["refused"]


def test_a_token_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    from levain.phone_server import _kept_token

    path = tmp_path / "token"
    path.write_text("x" * 43 + "\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="readable by others"):
        _kept_token(path)


def test_the_link_file_goes_when_its_run_stops(tmp_path: Path) -> None:
    from levain.phone_server import _drop_link_file

    mine, theirs = tmp_path / "mine.json", tmp_path / "theirs.json"
    mine.write_text(json.dumps({"page": "p", "phone_token": "t", "pid": os.getpid()}))
    theirs.write_text(json.dumps({"page": "p", "phone_token": "t", "pid": os.getpid() + 1}))
    _drop_link_file(mine)
    _drop_link_file(theirs)
    assert not mine.exists() and theirs.exists()


def test_the_recommended_option_is_marked_never_selected(tmp_path: Path, passkey) -> None:
    """Phill 6a: the card shows the requester's recommendation. The phone gets the key to mark and nothing that
    selects it (no selected or default field, and the card's code neither focuses nor checks a button); a
    recommended key the request does not offer refuses the request by name."""
    signer, key, sign = passkey
    _file(tmp_path, "np-rec00001-aaaa", recommended="b")
    _file(tmp_path, "np-rec00002-aaaa", recommended="z")
    rows = {r["id"]: r for r in signer.listing([key.fingerprint])}
    row = rows["np-rec00001-aaaa"]
    assert row["recommended"] == "b" and "write_in" not in row
    assert not {"selected", "default", "choice"} & set(row) and all(set(o) == {"key", "text", "line"}
                                                                    for o in row["options"])
    assert "recommended 'z' is not one of the request's option keys" in rows["np-rec00002-aaaa"]["refused"]
    js = (Path(__file__).parents[1] / "levain" / "templates" / "web" / "phone.js").read_text()
    card = js[js.index("function signCard("):js.index("function signWith(")]
    # a needs-phill card marks the signed message's rec (card_format.js parseMessage refuses a request disagreeing with it)
    assert 'o.key === (nc ? nc.recommended : r.recommended)' in card and '" recommended"' in card and 'el("span", "rec", "recommended")' in card
    assert not any(w in card for w in (".focus(", "autofocus", ".checked", ".click()", ".selected", "aria-pressed"))
    rec = sign("np-rec00001-aaaa", "a")     # the recommendation is not signed: the bytes are option (a)'s alone
    signed = json.loads((tmp_path / "sign" / "np-rec00001-aaaa.signed.json").read_text())["signed_message"]
    assert rec["choice"] == "a" and signed == MESSAGE + "\n" + choice_line("a", "keep it")


def test_a_write_in_signs_as_the_reserved_key_and_bad_words_are_refused(tmp_path: Path, passkey) -> None:
    """Phill 7a: his own words, signed as (*), verify under stock ssh-keygen and parse as the write-in, never as an
    option. Words holding an unshowable character are refused at the challenge, and in hand-built bytes at parse;
    a write-in line on a request that offered none does not parse."""
    signer, key, sign = passkey
    words = "ship it friday, not before"
    _file(tmp_path, "np-wi000001-aaaa", write_in=True)
    [row] = signer.listing([key.fingerprint])
    assert row["write_in"] == {"key": "*", "line": CHOICE_MARK + "(*): ", "max": 200}
    with pytest.raises(SignRefused, match="U\\+200B") as refused:
        signer.challenge("np-wi000001-aaaa", {"signer_fp": key.fingerprint, "request_sha256": row["sha256"],
                                              "choice": "*", "write_in": "ship\u200bit"})
    assert refused.value.status == 400 and signer._challenged == {}
    assert sign("np-wi000001-aaaa", "*", words)["choice"] == "*"
    signed = json.loads((tmp_path / "sign" / "np-wi000001-aaaa.signed.json").read_text())["signed_message"].encode()
    assert signed == (MESSAGE + "\n" + CHOICE_MARK + "(*): " + words).encode()
    assert _verify(tmp_path, "np-wi000001-aaaa", signed) == 0
    assert parse_choice(signed, write_in=True) == (MESSAGE, "*", words)
    with pytest.raises(ValueError, match="offered no write-in"):
        parse_choice(signed)
    with pytest.raises(ValueError, match="U\\+200B"):
        parse_choice((MESSAGE + "\n" + choice_line("*", "ship\u200bit")).encode(), write_in=True)


def test_the_page_keeps_write_in_words_for_their_version_only_and_sends_them_trimmed() -> None:
    """Typed words belong to the version of the request they were typed on: a replaced file (same id, new sha256)
    or a request gone from the listing drops them. The page sends the words trimmed (autocorrect's trailing space
    is not his answer), and the server's no-edge-space rule stays as strict."""
    js = (Path(__file__).parents[1] / "levain" / "templates" / "web" / "phone.js").read_text()
    card = js[js.index("function signCard("):js.index("function signWith(")]
    assert 'writeIns.set(r.id, { sha256: r.sha256 || "", words: box.value })' in card
    assert 'draft.sha256 !== (r.sha256 || "")) writeIns.delete(r.id)' in card
    assert "const w = box.value.trim();" in card and "wi.key, freeze)" in card
    draw = js[js.index("function draw("):js.index("function questionCard(")]
    assert "if (!signs.some((r) => r.id === id)) writeIns.delete(id)" in draw


def test_the_challenge_carries_the_exact_line_the_signature_appends(tmp_path: Path, passkey) -> None:
    """CHOICE L3 r2 (What You See Is What You Sign): the page shows the line the server will sign, never its own copy.
    The challenge reply's ``line`` is the last line of the bytes signed, for an option and for a write-in, and a plain
    request's reply carries none."""
    signer, key, sign = passkey
    words = "ship it friday, not before"
    _file(tmp_path, "np-ln000001-aaaa", write_in=True)
    _file(tmp_path, "np-ln000002-aaaa", options=None)
    rows = {r["id"]: r for r in signer.listing([key.fingerprint])}
    base = {"signer_fp": key.fingerprint, "request_sha256": rows["np-ln000001-aaaa"]["sha256"]}
    assert signer.challenge("np-ln000001-aaaa", {**base, "choice": "b"})["line"] == choice_line("b", "drop it")
    c = signer.challenge("np-ln000001-aaaa", {**base, "choice": "*", "write_in": words})
    assert c["line"] == CHOICE_MARK + "(*): " + words
    assert signer.answer("np-ln000001-aaaa", sign.assertion(c["challenge"]))["choice"] == "*"
    signed = json.loads((tmp_path / "sign" / "np-ln000001-aaaa.signed.json").read_text())["signed_message"]
    assert signed.split("\n")[-1] == c["line"] and signed == MESSAGE + "\n" + c["line"]
    plain = {"signer_fp": key.fingerprint, "request_sha256": rows["np-ln000002-aaaa"]["sha256"]}
    assert "line" not in signer.challenge("np-ln000002-aaaa", plain)


def test_an_option_may_hold_1000_characters_and_a_write_in_still_200(tmp_path: Path, passkey) -> None:
    """Phone preselect Q5 (a), ruled 1025-18: a desk option ran about 500 characters and was cut at 200 on the card,
    so flow repeated every option whole in the message. An option's text may now be 1000 characters; a title, a
    requester and a write-in (typed on a phone) stay at 200."""
    from levain.consent.sign import OPTION_TEXT_MAX, WRITE_IN_MAX, read_request, write_in_problem

    assert (OPTION_TEXT_MAX, WRITE_IN_MAX) == (1000, 200)
    signer, key, sign = passkey
    long_b = ("hold it: " + "a reason that runs the length of a desk option " * 30)[:OPTION_TEXT_MAX - 4] + " END"
    assert len(long_b) == 1000
    options = [{"key": "a", "text": "keep it"}, {"key": "b", "text": long_b}]
    _file(tmp_path, "np-long0001-aaaa", options=options, write_in=True)
    [row] = [r for r in signer.listing([key.fingerprint]) if r["id"] == "np-long0001-aaaa"]
    assert "refused" not in row and row["options"][1]["line"] == choice_line("b", long_b)
    assert row["write_in"]["max"] == WRITE_IN_MAX
    assert sign("np-long0001-aaaa", "b")["choice"] == "b"
    signed = json.loads((tmp_path / "sign" / "np-long0001-aaaa.signed.json").read_text())["signed_message"]
    assert parse_choice(signed.encode()) == (MESSAGE, "b", long_b)
    assert _verify(tmp_path, "np-long0001-aaaa", signed.encode()) == 0
    _file(tmp_path, "np-long0002-aaaa", options=[{"key": "a", "text": "keep it"}, {"key": "b", "text": long_b + "x"}])
    assert "over 1000 characters" in read_request(tmp_path, "np-long0002-aaaa").refused
    assert write_in_problem("w" * WRITE_IN_MAX) is None
    assert "over 200 characters" in write_in_problem("w" * (WRITE_IN_MAX + 1))
    _file(tmp_path, "np-long0003-aaaa", write_in=True)
    sha = next(r for r in signer.listing([key.fingerprint]) if r["id"] == "np-long0003-aaaa")["sha256"]
    with pytest.raises(SignRefused, match="over 200 characters"):
        signer.challenge("np-long0003-aaaa", {"signer_fp": key.fingerprint, "request_sha256": sha, "choice": "*",
                                              "write_in": "w" * (WRITE_IN_MAX + 1)})
