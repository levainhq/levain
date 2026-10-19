"""The phone port's sign requests (levain.consent.sign): what the operator reads is what is signed, and levain's own
namespaces are never signed on request."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.sign import Signer, SignRefused, namespace_problem

_FP = "SHA256:passkey"


def _file(qdir: Path, rid: str, message: str, *, ns: str = "example-attest", title: str = "report") -> None:
    sign = qdir / "sign"
    sign.mkdir(exist_ok=True)
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": ns, "message": message, "title": title,
            "requester": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    (sign / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(sign / f".{rid}.tmp", sign / f"{rid}.json")


def test_a_levain_namespace_is_refused_at_the_flag_and_at_signing_whatever_the_flags_say(tmp_path: Path) -> None:
    assert "begins 'levain'" in namespace_problem("levain-consent-answer")
    with pytest.raises(ValueError, match="begins 'levain'"):
        Signer(tmp_path, ("levain-consent-ruling",), "localhost", lambda: {_FP: object()})
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: object()})
    signer.namespaces = ("example-attest", "levain-consent-answer")          # flags that somehow carried one
    _file(tmp_path, "answer-forge-1", '{"decision":"allow"}', ns="levain-consent-answer")
    with pytest.raises(SignRefused, match="begins 'levain'"):
        signer.challenge("answer-forge-1", {"signer_fp": _FP})
    [row] = signer.listing([_FP])
    assert "begins 'levain'" in row["refused"] and "message" not in row


def test_what_cannot_show_as_its_bytes_is_never_signed_and_a_changed_file_is_refused(tmp_path: Path) -> None:
    signer = Signer(tmp_path, ("example-attest",), "localhost", lambda: {_FP: object()})
    for n, text in enumerate(("pay ‮TOTAL 100‬", "a​b", "a\rb", "a b")):
        _file(tmp_path, f"unshown-{n:03d}", text)
    rows = signer.listing([_FP])
    assert len(rows) == 4 and all(r["refused"] and "message" not in r and "title" not in r for r in rows)
    with pytest.raises(SignRefused, match="U\\+202E"):
        signer.challenge("unshown-000", {"signer_fp": _FP})
    # the challenge is bound to the file's bytes: a retitle after it is refused before any assertion is read
    _file(tmp_path, "retitled-1", "I attest this.")
    signer.challenge("retitled-1", {"signer_fp": _FP})
    _file(tmp_path, "retitled-1", "I attest this.", title="another title")
    with pytest.raises(SignRefused, match="changed after its challenge"):
        signer.answer("retitled-1", {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA"})
    assert not (tmp_path / "sign" / "retitled-1.sig").exists()
