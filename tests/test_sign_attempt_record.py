"""The attempt record answers for itself (levain.consent.sign: Signer.answer's ``attempt``, Signer.attempt): a record
keeps the namespace of the request it was made for, so a status read finds a settled answer after the request file
is gone; the fence loop is bounded; a failing settle never hides the refusal it was settling; and an answer refused
before it wrote anything never settles signed on another answer's signature."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from levain.consent import sign as signmod
from levain.consent.sign import SignRefused, attempts_dir
from tests.test_sign_attempt import AID, RID, _challenged, _record
from tests.test_sign_choice import _file, passkey  # noqa: F401 -- the fixture


def test_a_signed_attempt_reads_signed_after_its_request_file_is_gone(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    (tmp_path / "sign" / f"{RID}.json").unlink()         # the caller took its result and removed the request
    st = signer.attempt(RID, {"attempt": AID})
    assert st["state"] == "signed" and st["choice"] == "b", st


def test_a_record_for_a_namespace_this_port_does_not_sign_is_404_with_or_without_the_file(tmp_path: Path,
                                                                                           passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID, ns="other-namespace")
    with pytest.raises(SignRefused):                     # an answer on it is refused, and its attempt recorded so
        signer.answer(RID, {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA",
                            "attempt": AID})
    assert _record(tmp_path)["state"] == "refused"
    for gone in (False, True):
        if gone:
            (tmp_path / "sign" / f"{RID}.json").unlink()
        with pytest.raises(SignRefused) as exc:
            signer.attempt(RID, {"attempt": AID})
        assert exc.value.status == 404


def test_the_fence_loop_stops_at_its_deadline(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.2)
    spins = {"n": 0}

    def never(path, text):                           # the record is always there when written, gone when read
        spins["n"] += 1
        if spins["n"] > 200000:
            raise RuntimeError("the fence loop spun with no deadline")
        raise FileExistsError(path)

    monkeypatch.setattr(signmod, "_read_attempt", lambda path: None)
    monkeypatch.setattr(signmod, "_write_new", never)
    st = signer.attempt(RID, {"attempt": AID})
    assert st["state"] == "unknown", st


def test_a_failing_settle_never_replaces_the_refusal(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)

    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(signmod, "_settle_attempt", broken)
    with pytest.raises(SignRefused) as exc:              # no challenge was asked: a clean 409
        signer.answer(RID, {"authenticator_data": "AA", "client_data_json": "AA", "signature": "AA",
                            "attempt": AID})
    assert exc.value.status == 409 and "no challenge" in str(exc.value)


def test_an_answer_refused_before_it_wrote_never_settles_signed_on_the_same_signature(tmp_path: Path, passkey,
                                                                                      monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    import levain.authority as authority
    real = authority.verify_link_sig
    both = threading.Barrier(2, timeout=10)

    def together(*a, **k):                               # both answers have verified and marked before either commits
        ok = real(*a, **k)
        both.wait()
        return ok

    monkeypatch.setattr(authority, "verify_link_sig", together)
    body = _challenged(signer, key, sign)                # ONE assertion, sent as two attempts
    aids = (AID, AID[:-1] + "2")
    out: dict = {}

    def run(aid: str) -> None:
        try:
            out[aid] = signer.answer(RID, {**body, "attempt": aid})
        except SignRefused as exc:
            out[aid] = exc

    ts = [threading.Thread(target=run, args=(a,)) for a in aids]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    won = [a for a in aids if isinstance(out.get(a), dict)]
    lost = [a for a in aids if isinstance(out.get(a), SignRefused)]
    assert len(won) == 1 and len(lost) == 1, out
    assert (attempts_dir(tmp_path) / f"{RID}.{lost[0]}.sig256").exists()     # it did mark the same signature
    assert signer.attempt(RID, {"attempt": won[0]})["state"] == "signed"
    st = signer.attempt(RID, {"attempt": lost[0]})
    assert st["state"] == "refused", st
