"""The phone port's sign requests (levain.consent.sign): what the operator reads is what is signed, and levain's own
namespaces are never signed on request."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.passkey import PasskeyError, _client_origin
from levain.consent.sign import Signer, SignRefused, namespace_problem
from levain.contracts.namespaces import FAMILIES, REGISTRY

_FP = "SHA256:passkey"


def _file(qdir: Path, rid: str, message: str, *, ns: str = "example-attest", title: str = "report") -> None:
    sign = qdir / "sign"
    sign.mkdir(exist_ok=True)
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": ns, "message": message, "title": title,
            "requester": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    (sign / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(sign / f".{rid}.tmp", sign / f"{rid}.json")


def _listed_sha(signer: Signer, rid: str) -> str:
    return next(r["sha256"] for r in signer.listing([_FP]) if r["id"] == rid)


def test_a_levain_namespace_is_refused_at_the_flag_and_at_signing_whatever_the_flags_say(tmp_path: Path) -> None:
    assert "begins 'levain'" in namespace_problem("levain-consent-answer")
    # every namespace levain verifies in (the registry the package's AST test holds every literal namespace to), its
    # families, and ssh's own conventions are refused, whatever their prefix
    for ns in [*REGISTRY, *(f + "x" for f in FAMILIES), "git", "file", "email"]:
        assert namespace_problem(ns) is not None, ns
    with pytest.raises(ValueError, match="begins 'levain'"):
        Signer(tmp_path, ("levain-consent-ruling",), "localhost", lambda: {_FP: object()})
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: object()})
    signer.namespaces = ("example-attest", "levain-consent-answer")          # flags that somehow carried one
    _file(tmp_path, "answer-forge-1", '{"decision":"allow"}', ns="levain-consent-answer")
    with pytest.raises(SignRefused, match="begins 'levain'"):
        signer.challenge("answer-forge-1", {"signer_fp": _FP, "request_sha256": "x"})
    [row] = signer.listing([_FP])
    assert "begins 'levain'" in row["refused"] and "message" not in row


def test_what_cannot_show_as_its_bytes_is_never_signed_and_a_changed_file_is_refused(tmp_path: Path) -> None:
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: object()})
    for n, text in enumerate(("pay ‮TOTAL 100‬", "a​b", "a\rb", "a b")):
        _file(tmp_path, f"unshown-{n:03d}", text)
    rows = signer.listing([_FP])
    assert len(rows) == 4 and all(r["refused"] and "message" not in r and "title" not in r for r in rows)
    with pytest.raises(SignRefused, match="U\\+202E"):
        signer.challenge("unshown-000", {"signer_fp": _FP, "request_sha256": "x"})
    # the challenge is bound to the file's bytes: a retitle after it is refused before any assertion is read
    _file(tmp_path, "retitled-1", "I attest this.")
    signer.challenge("retitled-1", {"signer_fp": _FP, "request_sha256": _listed_sha(signer, "retitled-1")})
    _file(tmp_path, "retitled-1", "I attest this.", title="another title")
    with pytest.raises(SignRefused, match="changed after its challenge"):
        signer.answer("retitled-1", {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA"})
    assert not (tmp_path / "sign" / "retitled-1.sig").exists()


def test_a_message_swapped_after_the_listing_is_never_challenged(tmp_path: Path) -> None:
    """Reproduced before the fix: the card listed message A, a same-user writer renamed message B over the file, and
    the challenge was issued over B (the binding began at the challenge, not at what the page showed)."""
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: object()})
    _file(tmp_path, "swapped-1", "I attest A.")
    shown = _listed_sha(signer, "swapped-1")
    _file(tmp_path, "swapped-1", "I attest B.")
    for req in ({"signer_fp": _FP, "request_sha256": shown}, {"signer_fp": _FP}):
        with pytest.raises(SignRefused) as refused:
            signer.challenge("swapped-1", req)
        assert refused.value.status == 409
    assert signer._challenged == {}
    signer.challenge("swapped-1", {"signer_fp": _FP, "request_sha256": _listed_sha(signer, "swapped-1")})


def test_a_passkey_origin_is_exactly_the_https_name(tmp_path: Path) -> None:
    """Reproduced before the fix: ``https://box.example:8443`` passed for rpId ``box.example`` (the check compared the
    host only), so another page on the same host name under another port could mint a signature levain accepts."""
    def cdj(origin: str) -> bytes:
        return json.dumps({"type": "webauthn.get", "challenge": "x", "origin": origin, "crossOrigin": False}).encode()

    assert _client_origin(cdj("https://box.example"), "box.example") == "https://box.example"
    for bad in ("https://box.example:8443", "https://box.example:443", "http://box.example", "https://u@box.example",
                "https://box.example/"):
        with pytest.raises(PasskeyError, match="exactly"):
            _client_origin(cdj(bad), "box.example")
    # only localhost, which no phone reaches, takes http and a port (this machine's own browser)
    assert _client_origin(cdj("http://localhost:55081"), "localhost") == "http://localhost:55081"
    with pytest.raises(PasskeyError):
        _client_origin(cdj("http://localhost:x"), "localhost")




def test_a_request_changed_or_expired_while_its_signature_verifies_is_never_recorded(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    """Reproduced before the fix: ``answer`` checked the request, then verified for up to ten seconds, then wrote
    ``.signed.json`` without reading the file again: a request retitled (or expired) during the verify was recorded
    signed."""
    import levain.authority
    import levain.consent.passkey
    import levain.consent.sign as sign

    key = type("K", (), {"public_key": "pk", "fingerprint": _FP})()
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: key})
    monkeypatch.setattr(levain.consent.passkey, "to_sshsig", lambda *a, **k: "SIG")
    monkeypatch.setattr(levain.consent.passkey, "answer_signature_problem", lambda *a, **k: None)
    during: list = []
    monkeypatch.setattr(levain.authority, "verify_link_sig", lambda *a, **k: (during.pop()(), True)[1])
    real_now = sign._now
    answer = {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA"}
    for rid, meanwhile in (
            ("retitled-during", lambda: _file(tmp_path, "retitled-during", "I attest this.", title="another")),
            ("expired-during", lambda: monkeypatch.setattr(sign, "_now", lambda: real_now() + timedelta(hours=1)))):
        _file(tmp_path, rid, "I attest this.")
        signer.challenge(rid, {"signer_fp": _FP, "request_sha256": _listed_sha(signer, rid)})
        during.append(meanwhile)
        with pytest.raises(SignRefused) as refused:
            signer.answer(rid, answer)
        assert refused.value.status == 409, str(refused.value)
        assert not (tmp_path / "sign" / f"{rid}.signed.json").exists()
        assert not (tmp_path / "sign" / f"{rid}.sig").exists()
