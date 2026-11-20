"""levain#125 (1118-22): the Touch ID terminal sign twice returned a signature that does not verify (flags 0x21, a byte
the authenticator never signed; check-novalidate failed too, so the bytes were not even self-consistent). The Secure
Enclave cannot be driven here, so these build the failure from a SOFTWARE sk-ecdsa key: a real SSHSIG (OpenSSH
PROTOCOL.sshsig over PROTOCOL.u2f) made in Python, whose flags byte is then flipped the way the real failure was. ssh-keygen
itself judges it, so the "error in libcrypto" is the real one. Nothing here touches a real key, the Secure Enclave or
~/.levain: the key and its signatures live in tmp_path.

Held: a corrupted first answer is retried once (a new sign over the same message) and the second, good one is returned; every
failed attempt leaves one 0600 evidence file with no message bytes; two failures raise with both flags bytes; the evidence
directory never holds more than the bound; a non-sk key is not retried."""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import stat
import os
import struct
import subprocess
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import levain.cockpit.keys as keys
from levain.authority_sig import _sshsig_type_and_flags
from levain.cockpit.keys import KeyError_, ssh_signer

SK = "sk-ecdsa-sha2-nistp256@openssh.com"
NS = "levain-authority@levain"


# levain#153: real armored signatures from ~/.levain/keys/sign-failures (public data, no message) and the real good one
_RECORDED = json.loads((Path(__file__).parent / "fixtures" / "sk_empty_signatures_1119.json").read_text())


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return _s(raw)


class SoftSk:
    """A software sk-ecdsa key: signs like a FIDO authenticator would (sha256(app) | flags | counter | sha256(data))."""

    def __init__(self, dir_: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        self.priv = ec.generate_private_key(ec.SECP256R1())
        point = self.priv.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
        self.blob = _s(SK.encode()) + _s(b"nistp256") + _s(point) + _s(b"ssh:")
        dir_.mkdir(mode=0o700, exist_ok=True)
        self.path = dir_ / "id_sk"
        self.path.write_text("not a real private key; the fake ssh-keygen sign never reads it\n")
        self.path.chmod(0o600)
        Path(str(self.path) + ".pub").write_text(f"{SK} {base64.b64encode(self.blob).decode()} test\n")

    def sign(self, message: bytes, namespace: str, *, flags: int = 0x01, counter: int = 7, sig_flags: int | None = None) -> bytes:
        """The armored SSHSIG over ``message``; ``sig_flags`` is the byte WRITTEN into the blob when it differs from the one
        the authenticator signed (the real failure's shape)."""
        data = b"SSHSIG" + _s(namespace.encode()) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(message).digest())
        signed = hashlib.sha256(b"ssh:").digest() + bytes([flags]) + struct.pack(">I", counter) + hashlib.sha256(data).digest()
        r, s = decode_dss_signature(self.priv.sign(signed, ec.ECDSA(hashes.SHA256())))
        inner = _s(SK.encode()) + _s(_mpint(r) + _mpint(s)) + bytes([flags if sig_flags is None else sig_flags]) \
            + struct.pack(">I", counter)
        blob = b"SSHSIG" + struct.pack(">I", 1) + _s(self.blob) + _s(namespace.encode()) + _s(b"") + _s(b"sha512") + _s(inner)
        b64 = base64.b64encode(blob).decode()
        lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
        return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines) + "\n-----END SSH SIGNATURE-----\n").encode()


class FakeClock:
    """``keys.time`` with a ``monotonic`` the fake sign advances: a person's slow touch, without sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:      # a wait between attempts: recorded and advanced, never slept for real
        self.sleeps.append(s)
        self.t += s

    def __getattr__(self, name):
        return getattr(time, name)


class FakeKeygenSign:
    """Stands in for ONLY the ``ssh-keygen -Y sign`` call; every verify still runs the real ssh-keygen. ``plan`` is one
    entry per sign: the flags byte WRITTEN (None = honest), ``"cancel"`` (empty output, exit 0: a cancelled sheet) or
    ``"timeout"`` (the person never touches: the whole timeout handed to it passes, then TimeoutExpired). ``durations``
    is how long each sign's touch takes on ``clock`` (a number, or a function of the timeout the sign was handed); a
    touch slower than that timeout is a timeout. ``timeouts`` records the timeout each sign was handed.

    Every other ``ssh-keygen -Y`` call (verify, check-novalidate) takes ``check_took`` seconds on ``clock`` (``inf`` = a
    verifier that hangs: its whole timeout passes, then TimeoutExpired), and ``checks`` records its verb and timeout. Its
    verdict still comes from the real ssh-keygen, under a real bound of its own, since its time is the fake clock's."""

    def __init__(self, key: SoftSk, plan: list, real_run, clock: FakeClock | None = None) -> None:
        self.key, self.plan, self.real_run, self.signs = key, list(plan), real_run, 0
        self.clock, self.durations, self.timeouts = clock, [], []
        self.check_took, self.checks = 0.0, []

    def __call__(self, argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            self.signs += 1
            self.timeouts.append(kw.get("timeout"))
            written = self.plan.pop(0) if self.plan else None
            took = self.durations.pop(0) if self.durations else 0.0
            if callable(took):
                took = took(kw["timeout"])
            if written == "timeout" or (self.clock is not None and took > kw["timeout"]):
                if self.clock is not None:
                    self.clock.t += kw["timeout"]
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            if self.clock is not None:
                self.clock.t += took
            if written == "cancel":
                return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
            if written == "empty":      # the recorded 11-19 answer: flags 0x21, counter 1, r and s of zero length
                return subprocess.CompletedProcess(argv, 0, stdout=_RECORDED["empty"].encode(), stderr=b"")
            ns = argv[argv.index("-n") + 1]
            out = self.key.sign(kw["input"], ns, counter=self.signs, sig_flags=written)
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")
        if self.clock is not None and "-Y" in argv:
            self.checks.append((argv[argv.index("-Y") + 1], kw.get("timeout")))
            if self.check_took > kw["timeout"]:
                self.clock.t += kw["timeout"]
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            self.clock.t += self.check_took
            kw = dict(kw, timeout=30.0)
        return self.real_run(argv, **kw)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    key = SoftSk(tmp_path / "keys")
    clock = FakeClock()
    fake = FakeKeygenSign(key, [], subprocess.run, clock)
    monkeypatch.setattr(keys.subprocess, "run", fake)
    monkeypatch.setattr(keys, "time", clock)
    fp, sign = ssh_signer(key.path, namespace=NS)

    class R:
        pass

    r = R()
    r.key, r.fake, r.fp, r.sign, r.dir, r.clock = key, fake, fp, sign, key.path.parent / "sign-failures", clock
    return r


def worst_sign_step_s(where: Path, bound: float) -> float:
    """The longest the sign step (the signer terminal_sign's factory builds, bounded by ``bound``) can keep a person's Touch ID sheet
    waiting, MEASURED: a first answer (honest, or one that does not verify) arriving at every interesting moment of the
    bound, then a second answer that does not verify either, late or never; and every check of an answer (verify,
    check-novalidate) instant, slow, or hung. The checks' time is counted on the same clock as the touches. The callers'
    budgets (band_hook.PROMPT_HOOK_TIMEOUT_S, desk_click._WAIT_S) are asserted against this number, not against
    ``bound`` itself, so a sign step that grows past its bound fails them."""
    from levain.authority_sig import VerifierUnavailable
    from levain.consent import terminal_sign as ts
    where.mkdir(parents=True, exist_ok=True)
    key = SoftSk(where / "worst-case-key")
    floor = getattr(keys, "_SK_RETRY_MIN_S", 5.0)
    worst = 0.0
    with pytest.MonkeyPatch.context() as mp:
        clock = FakeClock()
        fake = FakeKeygenSign(key, [], subprocess.run, clock)
        mp.setattr(keys.subprocess, "run", fake)
        mp.setattr(keys, "time", clock)
        # what ts.default_signer_factory builds (the seat tests guard that name itself, so it is not called here)
        _fp, sign = ssh_signer(key.path, namespace=ts.NAMESPACE, timeout=bound)
        for check_took in (0.0, 1.5, float("inf")):
            for plan in ([0x21, "timeout"], [0x21, 0x21], [None]):
                for first in (0.0, bound / 2, bound - floor - 0.01, bound - floor, bound - floor + 0.01, bound * 0.99):
                    if first < 0:
                        continue
                    fake.plan, fake.durations, fake.check_took = list(plan), [first, lambda t: t * 0.99], check_took
                    start = clock.t
                    try:
                        sign(MESSAGE)
                    except (KeyError_, VerifierUnavailable):
                        pass
                    worst = max(worst, clock.t - start)
    return worst


MESSAGE = b"sign: 1267: 100% agree, a very private consent answer text that must never reach a diagnostics file\n"


def test_reproduction_a_flipped_flags_byte_does_not_verify_in_ssh_keygen(rig):
    """The failure as measured 11-18: flags 0x21 where the authenticator signed 0x01; verify and check-novalidate both fail
    with libcrypto's error. This is what the two real failures looked like, built in software."""
    sig = rig.key.sign(MESSAGE, NS, flags=0x01, sig_flags=0x21).decode()
    assert _sshsig_type_and_flags(sig) == (SK, 0x21)
    detail = keys._authority_sig.verify_failure_detail(MESSAGE, sig, public_key=SK + " " + base64.b64encode(rig.key.blob).decode(),
                                                       namespace=NS)
    assert "verify: Signature verification failed" in detail and "check-novalidate: Signature verification failed" in detail, detail


def test_an_honest_sign_returns_at_once_and_leaves_no_evidence(rig):
    sig = rig.sign(MESSAGE)
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----")
    assert rig.fake.signs == 1 and not rig.dir.exists()


def test_a_corrupted_first_answer_is_retried_once_and_the_good_second_is_returned(rig, capsys):
    rig.fake.plan = [0x21, None]
    sig = rig.sign(MESSAGE)
    assert rig.fake.signs == 2
    assert _sshsig_type_and_flags(sig) == (SK, 0x01)
    assert "the key's answer did not check out; touch again" in capsys.readouterr().err
    files = list(rig.dir.iterdir())
    assert len(files) == 1
    f = files[0]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(rig.dir.stat().st_mode) == 0o700
    ev = json.loads(f.read_text())
    assert ev["flags"] == "0x21" and ev["counter"] == 1 and ev["fingerprint"] == rig.fp and ev["attempt"] == 1
    assert ev["message_sha256"] == hashlib.sha256(MESSAGE).hexdigest()
    assert ev["signature"].startswith("-----BEGIN SSH SIGNATURE-----") and ev["time"]
    assert "verify: Signature verification failed" in ev["ssh_keygen"] and "check-novalidate:" in ev["ssh_keygen"]


def test_the_evidence_file_holds_no_message_bytes(rig):
    rig.fake.plan = [0x21, None]
    rig.sign(MESSAGE)
    raw = next(rig.dir.iterdir()).read_bytes()
    for piece in (MESSAGE, b"private consent answer", b"sign: 1267", base64.b64encode(MESSAGE)):
        assert piece not in raw


def test_two_failures_raise_with_both_flags_and_both_evidence_paths(rig):
    rig.fake.plan = [0x21, 0x23]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 2                      # one retry, never a third
    assert "both attempts" in text and "0x21" in text and "0x23" in text
    assert "the key's answer did not check out" in text
    files = sorted(rig.dir.iterdir())
    assert len(files) == 2 and all(str(f) in text for f in files)
    assert text.startswith(f"ssh-keygen returned a signature that does not verify as {rig.fp}")


def test_the_evidence_directory_is_bounded(rig):
    for _ in range(keys._SIGN_FAILURE_KEEP):        # each failing sign writes two files
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_):
            rig.sign(MESSAGE)
    assert len(list(rig.dir.iterdir())) == keys._SIGN_FAILURE_KEEP
    # the oldest go, the newest stay
    newest = max(p.name for p in rig.dir.iterdir())
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_):
        rig.sign(MESSAGE)
    names = sorted(p.name for p in rig.dir.iterdir())
    assert len(names) == keys._SIGN_FAILURE_KEEP and newest in names


def test_a_file_that_is_not_ours_is_left_alone_by_the_prune(rig):
    rig.dir.mkdir(mode=0o700)
    other = rig.dir / "notes.txt"
    other.write_text("mine")
    for _ in range(keys._SIGN_FAILURE_KEEP + 2):
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_):
            rig.sign(MESSAGE)
    assert other.read_text() == "mine"


def test_a_no_touch_signature_is_still_refused_without_a_retry(rig):
    rig.fake.plan = [0x00, None]
    with pytest.raises(KeyError_, match="without the user-presence flag"):
        rig.sign(MESSAGE)
    assert rig.fake.signs == 1


def test_an_unwritable_evidence_dir_does_not_change_the_refusal(rig, tmp_path):
    (rig.key.path.parent / "sign-failures").write_text("a file where the directory should be")
    rig.fake.plan = [0x21, None]
    assert _sshsig_type_and_flags(rig.sign(MESSAGE))[1] == 0x01        # the retry still works
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_, match="evidence not written"):
        rig.sign(MESSAGE)


def test_a_non_sk_key_that_fails_to_verify_is_not_retried(tmp_path, monkeypatch):
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(d / "k")], check=True)
    fp, sign = ssh_signer(d / "k")
    monkeypatch.setattr(keys._authority, "verify_link_sig", lambda *a, **k: False)
    calls: list[int] = []
    real = subprocess.run

    def counting(argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            calls.append(1)
        return real(argv, **kw)

    monkeypatch.setattr(keys.subprocess, "run", counting)
    with pytest.raises(KeyError_, match="does not verify"):
        sign(b"x")
    assert len(calls) == 1 and not (d / "sign-failures").exists()


# --- fix round 1 (L1 HIGH 1/2, WARNING 5, NOTE 7/11; L2 LOW): one deadline, attempt 1 named, a trusted directory -------

def _bounded(rig, bound: float):
    _fp, sign = ssh_signer(rig.key.path, namespace=NS, timeout=bound)
    return sign


def test_the_second_attempt_gets_only_what_is_left_of_the_one_bound(rig):
    sign = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations = [0x21, None], [6.0]
    assert _sshsig_type_and_flags(sign(MESSAGE))[1] == 0x01
    # each sign child waits what is left less the verify reserve (fix round 2): 20 - R, then (20 - 6) - R
    r = keys._VERIFY_RESERVE_S
    assert rig.fake.timeouts == [pytest.approx(20.0 - r), pytest.approx(14.0 - r)]


def test_the_whole_sign_step_never_outlasts_its_bound(tmp_path):
    from levain.consent import terminal_sign as ts
    for bound in (ts.SIGN_BOUND_S, keys._SIGN_TIMEOUT, 7.0, 3.0):
        worst = worst_sign_step_s(tmp_path / str(bound), bound)
        assert worst <= bound + 1e-6, (bound, worst)


def test_no_second_touch_is_asked_for_when_too_little_of_the_bound_is_left(rig, capsys):
    sign = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations = [0x21, None], [20.0 - keys._SK_RETRY_MIN_S + 0.5]
    with pytest.raises(KeyError_) as err:
        sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 1 and "touch again" not in capsys.readouterr().err
    (only,) = list(rig.dir.iterdir())
    assert str(only) in text and "0x21" in text and "no second touch" in text
    assert text.startswith(f"ssh-keygen returned a signature that does not verify as {rig.fp}")


def test_a_cancelled_second_attempt_still_names_the_first_attempts_evidence(rig, monkeypatch):
    from levain.cockpit.custody import APPLE_PROVIDER
    from levain.consent import terminal_sign as ts
    monkeypatch.setenv("SSH_SK_PROVIDER", APPLE_PROVIDER)        # a Touch ID signer, so an empty answer is a cancel
    rig.fake.plan = [0x21, "cancel"]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    (only,) = list(rig.dir.iterdir())
    assert "Touch ID was cancelled" in text and str(only) in text and "0x21" in text, text
    assert ts._classify(err.value)[0] == "cancelled"


def test_a_timed_out_second_attempt_still_names_the_first_attempts_evidence(rig):
    from levain.consent import terminal_sign as ts
    rig.fake.plan = [0x21, "timeout"]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    (only,) = list(rig.dir.iterdir())
    assert "TimeoutExpired" in text and str(only) in text and "0x21" in text, text
    assert ts._classify(err.value)[0] == "timeout"


def test_a_symlinked_evidence_dir_is_never_written_or_pruned(rig, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    planted = [elsewhere / f"sigfail-0000{i:02d}.json" for i in range(keys._SIGN_FAILURE_KEEP + 5)]
    for p in planted:
        p.write_text("{}")
    rig.dir.symlink_to(elsewhere)
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    assert sorted(elsewhere.iterdir()) == planted                  # nothing written there, nothing pruned there
    assert "evidence not written" in str(err.value)
    assert str(err.value).startswith(f"ssh-keygen returned a signature that does not verify as {rig.fp}")


def test_a_loose_evidence_dir_is_not_written(rig):
    rig.dir.mkdir()
    rig.dir.chmod(0o755)
    rig.fake.plan = [0x21, None]
    assert _sshsig_type_and_flags(rig.sign(MESSAGE))[1] == 0x01    # the sign itself is untouched
    assert list(rig.dir.iterdir()) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes into a 0500 directory anyway")
def test_a_read_only_evidence_dir_does_not_change_the_refusal(rig):
    rig.dir.mkdir(mode=0o700)
    rig.dir.chmod(0o500)
    try:
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_, match="evidence not written") as err:
            rig.sign(MESSAGE)
        assert "both attempts" in str(err.value) and rig.fake.signs == 2
    finally:
        rig.dir.chmod(0o700)


def test_the_prune_takes_only_our_regular_files(rig, tmp_path):
    rig.dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    link = rig.dir / "sigfail-00000000T000000000000-0000000001-1.json"
    link.symlink_to(outside)
    sub = rig.dir / "sigfail-00000000T000000000000-0000000002-1.json"
    sub.mkdir()
    for _ in range(keys._SIGN_FAILURE_KEEP):
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_):
            rig.sign(MESSAGE)
    assert link.is_symlink() and sub.is_dir() and outside.read_text() == "keep"


def test_the_file_just_written_is_never_pruned_even_when_it_sorts_oldest(rig):
    rig.dir.mkdir(mode=0o700)
    for i in range(keys._SIGN_FAILURE_KEEP + 3):                    # a clock that later stepped back: these sort newest
        (rig.dir / f"sigfail-99991231T235959{i:06d}-0000000001-1.json").write_text("{}")
    rig.fake.plan = [0x21, None]
    rig.sign(MESSAGE)
    ours = [p for p in rig.dir.iterdir() if not p.name.startswith("sigfail-9999")]
    assert len(ours) == 1 and json.loads(ours[0].read_text())["flags"] == "0x21"
    assert len(list(rig.dir.iterdir())) == keys._SIGN_FAILURE_KEEP


# --- fix round 2 (L3 r1): the deadline governs the checks too; attempt 1 named past a verifier failure; no truncated
# evidence; _classify reads the first line only -------------------------------------------------------------------------

def test_a_late_touch_and_a_hung_verifier_end_at_the_bound(rig):
    from levain.authority_sig import VerifierUnavailable
    sign = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations, rig.fake.check_took = [None], [lambda t: t * 0.99], float("inf")
    start = rig.clock.t
    with pytest.raises(VerifierUnavailable):
        sign(MESSAGE)
    assert rig.clock.t - start <= 20.0 + 1e-6
    assert rig.fake.checks and all(0 < t <= 20.0 for _verb, t in rig.fake.checks), rig.fake.checks


def test_a_late_touch_with_a_slow_verify_still_signs_inside_the_bound(rig):
    """The verify reserve: a touch in the last moment the sign child is given still leaves its check room to finish."""
    sign = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations, rig.fake.check_took = [None], [lambda t: t * 0.99], 0.5
    start = rig.clock.t
    assert _sshsig_type_and_flags(sign(MESSAGE))[1] == 0x01
    assert rig.clock.t - start <= 20.0 + 1e-6


def test_the_diagnostic_checks_get_only_what_is_left_and_none_once_it_is_spent(rig):
    from levain.consent import terminal_sign as ts
    sign = _bounded(rig, 20.0)
    # touched at 18.5 s; each check takes 1 s: the verify ends at 19.5, the diagnostic verify is cut at 20, and
    # check-novalidate is not run at all
    rig.fake.plan, rig.fake.durations, rig.fake.check_took = [0x21], [18.5], 1.0
    start = rig.clock.t
    with pytest.raises(KeyError_) as err:
        sign(MESSAGE)
    text = str(err.value)
    assert rig.clock.t - start == pytest.approx(20.0)
    assert [verb for verb, _t in rig.fake.checks] == ["verify", "verify"], rig.fake.checks
    assert "check-novalidate: not run" in text and "no second touch" in text, text
    assert "TimeoutExpired" not in text, text
    assert ts._classify(err.value)[0] == "signer-error"        # a refused answer, not a timed-out sign


def test_the_diagnostic_detail_runs_nothing_when_no_time_is_left(rig, monkeypatch):
    sig = rig.key.sign(MESSAGE, NS, flags=0x01, sig_flags=0x21).decode()
    pub = SK + " " + base64.b64encode(rig.key.blob).decode()
    detail = keys._authority_sig.verify_failure_detail(MESSAGE, sig, public_key=pub, namespace=NS, left=lambda: 0.0)
    assert "verify: not run" in detail and "check-novalidate: not run" in detail, detail
    assert rig.fake.checks == []


def test_a_verifier_failure_on_the_retry_still_names_the_first_attempts_evidence(rig, monkeypatch):
    from levain.authority_sig import VerifierUnavailable
    real = keys._authority.verify_link_sig
    calls: list[int] = []

    def second_cannot_run(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise VerifierUnavailable("ssh-keygen could not verify an authority signature (TimeoutExpired)")
        return real(*a, **k)

    monkeypatch.setattr(keys._authority, "verify_link_sig", second_cannot_run)
    rig.fake.plan = [0x21, None]
    with pytest.raises(VerifierUnavailable) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    (only,) = list(rig.dir.iterdir())
    assert type(err.value) is VerifierUnavailable
    assert text.startswith("ssh-keygen could not verify an authority signature") and "0x21" in text and str(only) in text


class _OsWithFullDisk:
    """``keys.os`` whose ``fdopen`` gives a file that takes a few bytes and then fails as a full disk would."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def fdopen(fd, mode="r", *a, **k):
        fh = os.fdopen(fd, mode, *a, **k)

        class Full:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()
                return False

            def write(self, data):
                fh.write(data[:10])
                fh.flush()
                raise OSError(errno.ENOSPC, "No space left on device")

        return Full()


def test_a_failed_evidence_write_leaves_no_truncated_file(rig, monkeypatch):
    rig.dir.mkdir(mode=0o700)
    monkeypatch.setattr(keys, "os", _OsWithFullDisk())
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert text.count("evidence not written (OSError: No space left on device)") == 2, text
    assert list(rig.dir.iterdir()) == []


def test_classify_reads_only_the_first_line(rig):
    from levain.consent import terminal_sign as ts
    first = f"ssh-keygen returned a signature that does not verify as {rig.fp} (flags 0x21), so nothing was signed"
    for later in ("evidence: /k/sign-failures/TimeoutExpired.json", "verdict: Touch ID was cancelled",
                  "ssh-keygen could not sign with /k/id (exit 1)"):
        assert ts._classify(KeyError_(first + "\n" + later)) == ("signer-error", first), later
    assert ts._classify(KeyError_("ssh-keygen could not sign (TimeoutExpired)\nmore"))[0] == "timeout"


# ---- levain#153: the EMPTY signature (r and s of zero length), named -------------------------------------------------

def _resigned(armored: str, sig_blob: bytes) -> str:
    """``armored`` with its signature string replaced by ``sig_blob`` (public key, namespace and the rest kept)."""
    body = base64.b64decode("".join(armored.strip().splitlines()[1:-1]))
    pos = 10                                        # "SSHSIG" + uint32 version
    for _ in range(4):                              # publickey, namespace, reserved, hash_algorithm
        pos += 4 + struct.unpack(">I", body[pos:pos + 4])[0]
    b64 = base64.b64encode(body[:pos] + _s(sig_blob)).decode()
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70)) \
        + "\n-----END SSH SIGNATURE-----\n"


def _sk_blob(r: bytes, s: bytes, tail: bytes = b"\x21\x00\x00\x00\x01") -> bytes:
    return _s(SK.encode()) + _s(_s(r) + _s(s)) + tail


def test_the_classifier_says_empty_for_the_recorded_failure_and_not_for_the_good_signature():
    from levain.authority_sig import _sshsig_type_flags_counter, sk_ecdsa_signature_empty
    # two records, 08:44 ET at a terminal and 02:09 ET with nobody there, were these same bytes: one is kept
    assert sk_ecdsa_signature_empty(_RECORDED["empty"])
    assert _sshsig_type_flags_counter(_RECORDED["empty"]) == (SK, 0x21, 1)      # flags and counter look good: not the tell
    assert _sshsig_type_flags_counter(_RECORDED["good"]) == (SK, 0x21, 1)
    assert not sk_ecdsa_signature_empty(_RECORDED["good"])


def test_the_classifier_is_true_only_for_a_well_formed_empty_signature_that_ends_after_flags_and_counter():
    from levain.authority_sig import sk_ecdsa_signature_empty
    assert sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"")))      # the hand-built twin: True
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"\x01" * 32)))   # empty r, real s
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"\x01" * 32, b"")))   # real r, empty s
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"", b"\x21\x00\x00\x00\x01junk")))
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"", b"")))          # no flags/counter
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"", b"\x21\x00\x00")))   # truncated
    assert not sk_ecdsa_signature_empty(_resigned(_RECORDED["empty"], _sk_blob(b"", b"", b"\x21")))          # truncated


def test_the_classifier_is_false_for_a_non_empty_unverifiable_or_malformed_signature(rig):
    from levain.authority_sig import sk_ecdsa_signature_empty
    flipped = rig.key.sign(MESSAGE, NS, flags=0x01, sig_flags=0x21).decode()
    assert not sk_ecdsa_signature_empty(flipped)
    for junk in ("", "nonsense", "-----BEGIN SSH SIGNATURE-----\n!!!\n-----END SSH SIGNATURE-----\n"):
        assert not sk_ecdsa_signature_empty(junk)


def test_two_empty_answers_say_the_key_returned_no_signature_and_the_evidence_says_why(rig):
    rig.fake.plan = ["empty", "empty"]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 2
    assert text.startswith("the key returned no signature for ")      # no sidecar here: Touch ID is not named
    assert "empty ECDSA signature (r and s have zero length)" in text and "nothing was signed" in text
    assert "try again" in text and "phone" in text
    assert "libcrypto" not in text and "does not verify" not in text
    files = sorted(rig.dir.iterdir())
    assert len(files) == 2 and all(str(f) in text for f in files)
    for f in files:
        ev = json.loads(f.read_text())
        assert ev["format"] == "levain-sign-failure/1" and ev["reason"] == "empty ECDSA signature (r and s have zero length)"
        assert ev["flags"] == "0x21" and ev["counter"] == 1 and "ssh_keygen" in ev and "message_sha256" in ev


def test_every_sign_failure_record_carries_the_sign_step_elapsed_seconds_and_every_old_key(rig):
    rig.fake.plan = ["empty", "empty"]
    rig.fake.durations = [1.2344, 2.3456]  # known touches on the fake clock: a hardcoded or dropped elapsed fails
    with pytest.raises(KeyError_):
        rig.sign(MESSAGE)
    files = sorted(rig.dir.iterdir())
    assert len(files) == 2
    evs = [json.loads(f.read_text()) for f in files]
    assert {e["attempt"]: e["elapsed_s"] for e in evs} == {1: 1.234, 2: 2.346}
    for ev in evs:
        assert isinstance(ev["elapsed_s"], float) and not isinstance(ev["elapsed_s"], bool)
        for key in ("format", "time", "fingerprint", "namespace", "algorithm", "flags", "counter", "attempt",
                    "message_sha256", "message_bytes", "ssh_keygen", "signature", "reason"):
            assert key in ev


def test_an_empty_first_answer_is_retried_and_a_good_second_is_returned(rig):
    rig.fake.plan = ["empty", None]
    assert _sshsig_type_and_flags(rig.sign(MESSAGE)) == (SK, 0x01)
    assert rig.fake.signs == 2


def test_a_non_empty_unverifiable_answer_keeps_its_old_text_and_records_no_reason(rig):
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    assert "does not verify" in str(err.value) and "returned no signature" not in str(err.value)
    for f in rig.dir.iterdir():
        assert json.loads(f.read_text())["reason"] is None


def test_an_empty_answer_with_no_time_for_a_second_touch_is_named_too(rig):
    rig.fake.plan, rig.fake.durations = ["empty"], [keys._SIGN_TIMEOUT - 1.0]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    assert "the key returned no signature" in str(err.value) and "no second touch was asked for" in str(err.value)


def test_the_words_name_touch_id_only_for_a_touch_id_signer(rig, monkeypatch):
    from levain.cockpit.custody import APPLE_PROVIDER
    rig.fake.plan = ["empty"] * 2
    with pytest.raises(KeyError_) as plain:         # no sidecar, no Apple provider: the code cannot know it is Touch ID
        rig.sign(MESSAGE)
    assert str(plain.value).startswith("the key returned no signature for ") and "Touch ID" not in str(plain.value)
    monkeypatch.setenv("SSH_SK_PROVIDER", APPLE_PROVIDER)
    rig.fake.plan, rig.fake.signs = ["empty"] * 2, 0
    with pytest.raises(KeyError_) as touch:
        rig.sign(MESSAGE)
    assert str(touch.value).startswith("the key returned no signature (Touch ID gave nothing back) for ")


@pytest.mark.parametrize("plan", [["empty", 0x21], [0x21, "empty"]], ids=["empty-then-bad", "bad-then-empty"])
def test_a_mixed_outcome_names_both_and_keeps_the_phone_guidance(rig, plan):
    rig.fake.plan = list(plan)
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 2
    assert "one attempt returned no signature" in text and "the other returned a signature that did not verify" in text
    first_empty = plan[0] == "empty"
    assert ("attempt 1 returned no signature" in text) == first_empty
    assert ("attempt 2 returned no signature" in text) == (not first_empty)
    assert "empty ECDSA signature (r and s have zero length)" in text         # the empty one's own diagnosis
    assert "try again" in text and "phone" in text and "nothing was signed" in text
    files = sorted(rig.dir.iterdir())
    assert len(files) == 2 and all(str(f) in text for f in files)
    assert sorted(json.loads(f.read_text())["reason"] is None for f in files) == [False, True]


def test_the_classifier_never_raises_whatever_a_malformed_signature_does(monkeypatch):
    """L3 r2 (complement LOW): it classifies EVIDENCE for the sign path, so no exception type may escape it."""
    from levain import authority_sig as sig
    for exc in (IndexError("short read"), struct.error("unpack"), RuntimeError("x")):
        def boom(_armored, _exc=exc):
            raise _exc
        monkeypatch.setattr(sig, "_sshsig_type_flags_counter", boom)
        assert sig.sk_ecdsa_signature_empty("-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----") is False


# levain#158: an EMPTY answer is the provider answering without a usable touch (probe2_run1_1119.txt); the retry waits for it

def test_empty_then_ok_waits_one_second_once(rig):
    rig.fake.plan = ["empty", None]
    assert _sshsig_type_and_flags(rig.sign(MESSAGE)) == (SK, 0x01)
    assert rig.fake.signs == 2 and rig.clock.sleeps == [1.0]


def test_an_empty_answer_with_a_deadline_too_short_for_the_wait_does_not_sleep_past_it(rig):
    sign = _bounded(rig, keys._SK_RETRY_MIN_S + 0.5)         # 5.5 s: enough for a touch, not for a 1 s wait and a touch
    rig.fake.plan = ["empty", None]
    with pytest.raises(KeyError_) as err:
        sign(MESSAGE)
    assert rig.fake.signs == 1 and rig.clock.sleeps == []
    assert "no second touch was asked for" in str(err.value) and "try again" in str(err.value)
    # no wait happened here, so the words say what WOULD remain after it, not what was left after it
    assert "bound would remain after the 1s wait, under 5s" in str(err.value) and "was left after" not in str(err.value)


def test_a_non_empty_bad_signature_keeps_the_single_immediate_retry(rig):
    rig.fake.plan = [0x21, 0x21, None]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    assert rig.fake.signs == 2 and rig.clock.sleeps == []
    assert "both attempts failed" in str(err.value)
    rig.fake.plan, rig.fake.signs = [0x21, None], 0
    assert _sshsig_type_and_flags(rig.sign(MESSAGE)) == (SK, 0x01)
    assert rig.fake.signs == 2 and rig.clock.sleeps == []


def test_empty_then_bad_ends_at_two_attempts_after_one_wait(rig):
    rig.fake.plan = ["empty", 0x21, None]
    with pytest.raises(KeyError_):
        rig.sign(MESSAGE)
    assert rig.fake.signs == 2 and rig.clock.sleeps == [1.0]


def test_each_failed_attempt_writes_its_evidence_with_attempt_number_and_elapsed(rig):
    rig.fake.plan, rig.fake.durations = ["empty"] * 2, [0.4, 1.1]
    with pytest.raises(KeyError_):
        rig.sign(MESSAGE)
    evs = sorted((json.loads(f.read_text()) for f in rig.dir.iterdir()), key=lambda e: e["attempt"])
    assert [(e["attempt"], e["elapsed_s"]) for e in evs] == [(1, 0.4), (2, 1.1)]


# levain#153 stopgap (11-20): the empty-answer retry is ONE retry, two signing calls in all, never a third touch

def test_an_unbroken_run_of_empty_answers_makes_exactly_two_signing_calls_then_raises(rig):
    rig.fake.plan = ["empty"] * 4
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 2 and rig.clock.sleeps == [1.0]
    assert text.startswith("the key returned no signature for ") and "attempt 3" not in text
    assert "nothing was signed" in text and "phone" in text


def test_the_wait_table_holds_one_wait_per_retry_the_cap_allows():
    """The import-time assert's own proof: a cap raised without its wait would be an IndexError in the middle of a sign."""
    assert len(keys._EMPTY_RETRY_WAITS_S) == keys._SK_EMPTY_ATTEMPTS - 1
