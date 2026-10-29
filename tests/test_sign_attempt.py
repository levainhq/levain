"""The attempt-status construct on the Signer (levain.consent.sign: Signer.answer's ``attempt``, Signer.attempt,
settle_orphan_attempts), over test_sign_choice's software passkey: an answer names an attempt id, its outcome is kept
durably (pending -> signed | refused), and a status read for it is authoritative: it waits (bounded) while the answer
runs, fences an attempt it never saw, and a pending one whose process stopped is settled by what the signature store
proves."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from levain.consent import sign as signmod
from levain.consent.sign import Signer, SignRefused, attempts_dir, settle_orphan_attempts
from tests.test_sign_choice import _file, passkey  # noqa: F401 -- the fixture

RID = "np-att0001-aa11"
AID = "attempt-0000000000000001"


def _challenged(signer: Signer, key, sign, rid: str = RID, choice: "str | None" = "b") -> dict:
    """An assertion over a fresh challenge for ``rid`` (option ``choice``), not yet answered."""
    row = next(r for r in signer.listing([key.fingerprint]) if r["id"] == rid)
    req = {"signer_fp": key.fingerprint, "request_sha256": row["sha256"], "choice": choice}
    return sign.assertion(signer.challenge(rid, req)["challenge"])


def _record(tmp_path: Path, aid: str = AID, rid: str = RID) -> dict:
    return json.loads((attempts_dir(tmp_path) / f"{rid}.{aid}.json").read_text())


def test_an_answer_with_an_attempt_signs_and_its_status_reads_signed(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    done = signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    assert done["choice"] == "b"
    st = signer.attempt(RID, {"attempt": AID})
    assert st["state"] == "signed" and st["choice"] == "b" and st["signer_fp"] == key.fingerprint, st
    assert _record(tmp_path)["state"] == "signed"


def test_an_attempt_the_server_never_saw_is_fenced_refused_and_its_late_answer_is_refused(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    late = {**_challenged(signer, key, sign), "attempt": AID}
    st = signer.attempt(RID, {"attempt": AID})          # the status read arrives first: the answer is still on its way
    assert st["state"] == "refused" and "never reached" in st["reason"], st
    with pytest.raises(SignRefused) as exc:
        signer.answer(RID, late)                         # then the answer arrives: "refused" stays true
    assert exc.value.status == 409
    assert not (tmp_path / "sign" / f"{RID}.signed.json").exists()
    assert signer.attempt(RID, {"attempt": AID})["state"] == "refused"
    # and a fresh attempt still signs
    assert signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID[:-1] + "2"})["choice"] == "b"


def test_a_refused_answer_reads_refused_with_its_reason(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    bad = _challenged(signer, key, sign)
    bad["signature"] = bad["signature"][:-4] + ("AAAA" if not bad["signature"].endswith("AAAA") else "BBBB")
    with pytest.raises(SignRefused) as exc:
        signer.answer(RID, {**bad, "attempt": AID})
    st = signer.attempt(RID, {"attempt": AID})
    assert st["state"] == "refused" and st["reason"] == str(exc.value), st


def test_a_status_read_waits_while_the_answer_verifies_and_reads_the_outcome(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    import levain.authority as authority
    go, entered = threading.Event(), threading.Event()
    real = authority.verify_link_sig

    def slow(*a, **k):
        entered.set()
        go.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(authority, "verify_link_sig", slow)
    body = {**_challenged(signer, key, sign), "attempt": AID}
    t = threading.Thread(target=signer.answer, args=(RID, body))
    t.start()
    assert entered.wait(10)
    threading.Timer(0.5, go.set).start()
    t0 = time.monotonic()
    st = signer.attempt(RID, {"attempt": AID})           # pending when asked: it waits for the answer
    t.join(10)
    assert st["state"] == "signed", st
    assert time.monotonic() - t0 >= 0.4


def test_past_the_bound_a_running_answer_reads_pending_never_refused(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    import levain.authority as authority
    go, entered = threading.Event(), threading.Event()
    real = authority.verify_link_sig

    def slow(*a, **k):
        entered.set()
        go.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(authority, "verify_link_sig", slow)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.3)
    t = threading.Thread(target=signer.answer, args=(RID, {**_challenged(signer, key, sign), "attempt": AID}))
    t.start()
    try:
        assert entered.wait(10)
        assert signer.attempt(RID, {"attempt": AID})["state"] == "pending"
        # a second Signer on the queue (the other port, another process's view) does not settle a running answer either
        other = Signer(tmp_path, signer.namespaces, signer.rp_id, signer.keys)
        assert other.attempt(RID, {"attempt": AID})["state"] == "pending"
    finally:
        go.set()
        t.join(10)
    assert signer.attempt(RID, {"attempt": AID})["state"] == "signed"


def test_a_pending_attempt_whose_process_stopped_settles_refused_at_the_next_start(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    fd = signmod._begin_attempt(tmp_path, RID, AID)     # an answer began ...
    os.close(fd)                                        # ... and its process ended: the lock is free, nothing written
    assert _record(tmp_path)["state"] == "pending"
    assert settle_orphan_attempts(tmp_path) == 1
    rec = _record(tmp_path)
    assert rec["state"] == "refused" and "stopped" in rec["reason"], rec


def test_a_pending_attempt_whose_signature_landed_settles_signed_at_the_next_start(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    # the process stopped after both files were written and before the record settled: put it back to pending
    path = attempts_dir(tmp_path) / f"{RID}.{AID}.json"
    path.write_text(json.dumps({"id": RID, "attempt": AID, "state": "pending", "at": "x"}))
    Signer(tmp_path, signer.namespaces, signer.rp_id, signer.keys)     # a restart
    rec = _record(tmp_path)
    assert rec["state"] == "signed" and rec["choice"] == "b", rec


def test_a_signature_another_attempt_wrote_never_proves_this_one(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    fd = signmod._begin_attempt(tmp_path, RID, AID)
    (attempts_dir(tmp_path) / f"{RID}.{AID}.sig256").write_text("0" * 64 + "\n")   # it marked a signature, then stopped
    os.close(fd)
    signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID[:-1] + "9"})   # another attempt signed it
    assert signer.attempt(RID, {"attempt": AID})["state"] == "refused"


def test_one_attempt_is_one_answer(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    with pytest.raises(SignRefused):
        signer.answer(RID, {"attempt": AID, "authenticator_data": "x"})     # refused, and settled refused
    with pytest.raises(SignRefused) as exc:
        signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    assert exc.value.status == 409 and not (tmp_path / "sign" / f"{RID}.signed.json").exists()


@pytest.mark.parametrize("bad", ["short", "x" * 65, "has.dot.in.it.xxxx", 7, None])
def test_a_malformed_attempt_id_is_refused_400(tmp_path: Path, passkey, bad) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    for call in (lambda: signer.answer(RID, {**_challenged(signer, key, sign), "attempt": bad}),
                 lambda: signer.attempt(RID, {"attempt": bad})):
        with pytest.raises(SignRefused) as exc:
            call()
        assert exc.value.status == 400
    assert not attempts_dir(tmp_path).exists() or not list(attempts_dir(tmp_path).glob("*.json"))


def test_a_status_read_for_a_request_this_port_does_not_sign_is_404(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID, ns="other-namespace")
    with pytest.raises(SignRefused) as exc:
        signer.attempt(RID, {"attempt": AID})
    assert exc.value.status == 404
    with pytest.raises(SignRefused) as exc:
        signer.attempt("nope-0000-0000", {"attempt": AID})
    assert exc.value.status == 404


def test_records_are_bounded_by_age_and_count(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    monkeypatch.setattr(signmod, "ATTEMPTS_MAX", 3)
    for n in range(3):
        assert signer.attempt(RID, {"attempt": f"fence-00000000000{n}"})["state"] == "refused"
    with pytest.raises(SignRefused) as exc:
        signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    assert exc.value.status == 429 and not (tmp_path / "sign" / f"{RID}.signed.json").exists()
    # past ATTEMPT_KEEP_S a settled record is dropped, and the answer is taken again
    old = time.time() - signmod.ATTEMPT_KEEP_S - 60
    for p in attempts_dir(tmp_path).glob("*.json"):
        os.utime(p, (old, old))
    assert signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})["choice"] == "b"
    assert len(list(attempts_dir(tmp_path).glob("*.json"))) == 1


def test_a_pending_record_is_never_dropped_for_its_age(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    fd = signmod._begin_attempt(tmp_path, RID, AID)
    try:
        old = time.time() - signmod.ATTEMPT_KEEP_S - 60
        os.utime(attempts_dir(tmp_path) / f"{RID}.{AID}.json", (old, old))
        signmod._attempt_room(tmp_path)
        assert _record(tmp_path)["state"] == "pending"
    finally:
        os.close(fd)


def test_both_ports_route_the_attempt_read() -> None:
    from levain import phone_server, web_server

    assert phone_server._sign_route("/sign/np-att0001-aa11/attempt") == (RID, "attempt")
    assert web_server._sign_route("/cockpit/sign/np-att0001-aa11/attempt") == (RID, "attempt")
