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
import contextlib
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

    def monotonic(self) -> float:
        return self.t

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
        self.argvs, self.stderr_text, self.held = [], b"", []     # held: descriptors kept open like a lingering grandchild
        self.check_took, self.checks = 0.0, []

    def __call__(self, argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            self.signs += 1
            self.timeouts.append(kw.get("timeout"))
            self.argvs.append(list(argv))
            err = kw.get("stderr")
            if isinstance(err, int) and self.stderr_text:
                os.write(err, self.stderr_text)       # what ssh-keygen and its helper print, on the descriptor they were given
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
            if isinstance(written, str) and written.startswith("-----BEGIN SSH SIGNATURE-----"):
                return subprocess.CompletedProcess(argv, 0, stdout=written.encode(), stderr=b"")    # an armored answer, verbatim
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


CANNED_CONTEXT = {"console_locked": {"present": True, "value": False}, "hid_idle_s": 41.0, "system_capabilities": 15,
                  "launchctl_manager": "Aqua", "ssh_connection_set": False, "ssh_tty_set": False, "stdin_tty": True,
                  "stderr_tty": True}
_REAL_FAILURE_CONTEXT = keys._failure_context if hasattr(keys, "_failure_context") else None


@pytest.fixture(autouse=True)
def _no_real_context_probes(monkeypatch):
    """The failure context shells out to ioreg and launchctl; these tests hand it a fixed answer (the collector has its own
    tests below, over a fake runner)."""
    monkeypatch.setattr(keys, "_failure_context", lambda left: dict(CANNED_CONTEXT), raising=False)


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
    live = capsys.readouterr().err
    assert "the key's answer did not check out; signing once more (touch if asked)" in live, live
    assert "touch again" not in live
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
    assert "attempt 1 returned a signature that did not check out" in text, text
    assert "the retry's sign step lasted" in text and "and returned a signature that did not check out" in text, text
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


def test_no_second_sign_is_started_when_too_little_of_the_bound_is_left(rig, capsys):
    sign = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations = [0x21, None], [20.0 - keys._SK_RETRY_MIN_S + 0.5]
    with pytest.raises(KeyError_) as err:
        sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 1 and "touch again" not in capsys.readouterr().err
    (only,) = list(rig.dir.iterdir())
    assert str(only) in text and "0x21" in text and "no second sign was started" in text
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
    assert "check-novalidate: not run" in text and "no second sign was started" in text, text
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
    first = f"ssh-keygen returned a signature that does not verify as {rig.fp} (flags 0x21), so no verifiable signature was returned"
    for later in ("evidence: /k/sign-failures/TimeoutExpired.json", "verdict: Touch ID was cancelled",
                  "ssh-keygen could not sign with /k/id (exit 1)"):
        assert ts._classify(KeyError_(first + "\n" + later)) == ("signer-error", first), later
    assert ts._classify(KeyError_("ssh-keygen could not sign (TimeoutExpired)\nmore"))[0] == "timeout"


# --- levain#125 (1118-31): an sk answer whose ECDSA r and s are both empty -------------------------------------------------
# The shape of the saved evidence of the live failures (four files, four different messages, ONE signature text, attempt 1 and 2
# of a sign 0.34 s apart): an armored SSHSIG whose ECDSA blob is 8 bytes (an mpint r of length 0, an mpint s of length 0),
# flags 0x21, counter 1. The saved text itself named the real public key of the signing key, and this repo is public, so the
# text below carries a freshly generated throwaway P-256 point in its place and is byte-for-byte the same shape otherwise.

SAVED_EMPTY_ENVELOPE = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "U1NIU0lHAAAAAQAAAH8AAAAic2stZWNkc2Etc2hhMi1uaXN0cDI1NkBvcGVuc3NoLmNvbQ\n"
    "AAAAhuaXN0cDI1NgAAAEEE5/woii9amaNz+KgJux0+yzmVuq1Hg1fNs5TG3JzOU0WxcheB\n"
    "i01+oRn/1YRZRPRCsGdeXWDKqqdW0ht2nRJLKwAAAARzc2g6AAAAFmxldmFpbi10ZXJtaW\n"
    "5hbC1hbnN3ZXIAAAAAAAAABnNoYTUxMgAAADcAAAAic2stZWNkc2Etc2hhMi1uaXN0cDI1\n"
    "NkBvcGVuc3NoLmNvbQAAAAgAAAAAAAAAACEAAAAB\n"
    "-----END SSH SIGNATURE-----\n")
TERMINAL_NS = "levain-terminal-answer"


@pytest.fixture
def trig(tmp_path, monkeypatch):
    """``rig`` in the terminal-answer namespace the saved envelope names, with the Touch ID provider in the environment (the
    fake sign never loads it)."""
    from levain.cockpit.custody import APPLE_PROVIDER
    monkeypatch.setenv("SSH_SK_PROVIDER", APPLE_PROVIDER)
    key = SoftSk(tmp_path / "keys")
    clock = FakeClock()
    fake = FakeKeygenSign(key, [], subprocess.run, clock)
    monkeypatch.setattr(keys.subprocess, "run", fake)
    monkeypatch.setattr(keys, "time", clock)
    fp, sign = ssh_signer(key.path, namespace=TERMINAL_NS)

    class R:
        pass

    r = R()
    r.key, r.fake, r.fp, r.sign, r.dir, r.clock = key, fake, fp, sign, key.path.parent / "sign-failures", clock
    return r


def _evidence(rig) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(rig.dir.iterdir())]


def test_the_saved_envelope_is_an_sk_signature_with_empty_r_and_s():
    from levain.authority_sig import _sshsig_type_flags_counter, sshsig_ecdsa_lengths
    assert _sshsig_type_flags_counter(SAVED_EMPTY_ENVELOPE) == (SK, 0x21, 1)
    assert sshsig_ecdsa_lengths(SAVED_EMPTY_ENVELOPE) == (0, 0)


def test_the_lengths_of_a_real_envelope_are_its_r_and_s(rig):
    from levain.authority_sig import sshsig_ecdsa_lengths
    r_len, s_len = sshsig_ecdsa_lengths(rig.key.sign(MESSAGE, NS).decode())
    assert 31 <= r_len <= 33 and 31 <= s_len <= 33


def test_the_lengths_are_none_for_what_is_not_an_ecdsa_envelope(tmp_path):
    from levain.authority_sig import sshsig_ecdsa_lengths
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(tmp_path / "k")], check=True)
    out = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(tmp_path / "k"), "-n", NS], input=b"x",
                         stdout=subprocess.PIPE, check=True).stdout.decode()
    assert sshsig_ecdsa_lengths(out) is None and sshsig_ecdsa_lengths("not a signature") is None


def test_the_previously_failing_case_the_saved_text_fails_check_novalidate_in_libcrypto():
    """The measured failure, offline: ssh-keygen's own verdict on the saved text, whatever the message is."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sig = Path(d) / "s.sig"
        sig.write_text(SAVED_EMPTY_ENVELOPE)
        cp = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "check-novalidate", "-n", TERMINAL_NS, "-s", str(sig)],
                            input=b"any message at all", capture_output=True, check=False)
    assert cp.returncode == 255 and b"error in libcrypto" in cp.stderr + cp.stdout, (cp.returncode, cp.stderr, cp.stdout)


def test_an_empty_envelope_is_its_own_named_refusal(trig):
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.34]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    exc = err.value
    assert isinstance(exc, KeyError_) and trig.fake.signs == 2          # still one retry, never a third
    assert (exc.flags, exc.r_len, exc.s_len) == (0x21, 0, 0) and exc.elapsed_s == pytest.approx(0.34)
    first = str(exc).splitlines()[0]
    assert "ssh-keygen returned a signature envelope with r and s empty after 0.3s for the key configured for Touch ID" in first, first
    assert "no verifiable signature was returned" in first and "flags 0x21 are in the envelope" in first, first
    for gone in ("it did not sign", "Touch ID's key returned", "the flags byte does not show", "a presence prompt takes seconds"):
        assert gone not in first, (gone, first)
    # the context stays in the 0600 evidence file; the message (it reaches agent-visible channels) carries only the path
    assert "context:" not in str(exc) and "console locked" not in str(exc) and "HID idle" not in str(exc), str(exc)
    low = str(exc).lower()
    assert "cancel" not in low and "touching again did not help" not in low and "a touch was made" not in low, str(exc)
    assert all(str(p) in str(exc) for p in trig.dir.iterdir()) and len(list(trig.dir.iterdir())) == 2


def test_the_empty_refusal_is_not_filed_as_a_cancel_or_a_timeout(trig):
    from levain.consent import terminal_sign as ts
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.34]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    assert ts._classify(err.value)[0] == "signer-error"


# every sentence the refusal emits is a measured one: no sentence is decided by how long the sign step lasted
_REMOVED_PHRASES = ("returned at once", "no prompt can have been answered", "a second touch was asked for",
                    "a presence prompt takes seconds", "does not show whether a touch happened", "touching again did not help",
                    "the same kind of answer", "did not sign", "Touch ID's key returned", "no second touch was asked for", "nothing was signed")


def _armored(key: SoftSk, namespace: str, r: bytes, s: bytes, *, flags: int = 0x21, counter: int = 1) -> str:
    """An sk SSHSIG whose ECDSA r and s are exactly these bytes (the signature is not meant to verify)."""
    inner = _s(SK.encode()) + _s(_s(r) + _s(s)) + bytes([flags]) + struct.pack(">I", counter)
    blob = (b"SSHSIG" + struct.pack(">I", 1) + _s(key.blob) + _s(namespace.encode()) + _s(b"") + _s(b"sha512") + _s(inner))
    b64 = base64.b64encode(blob).decode()
    return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
            + "\n-----END SSH SIGNATURE-----\n")


@pytest.mark.parametrize("retry_s", [0.3, 0.999, 1.0, 2.4, 8.0])
def test_no_refusal_text_holds_an_inference_from_how_long_the_retry_lasted(trig, retry_s):
    assert not hasattr(keys, "_SK_PROMPT_MIN_S")
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, retry_s]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    text = str(err.value)
    for gone in _REMOVED_PHRASES:
        assert gone not in text, (gone, retry_s, text)
    assert f"the retry's sign step lasted {retry_s:.1f}s and returned a signature envelope with r and s empty" in text, text


@pytest.mark.parametrize("retry_s", [0.3, 0.999, 1.0, 2.4, 8.0])
def test_no_non_empty_refusal_text_holds_an_inference_from_how_long_the_retry_lasted(rig, retry_s):
    rig.fake.plan, rig.fake.durations = [0x21, 0x21], [0.2, retry_s]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    for gone in _REMOVED_PHRASES:
        assert gone not in text, (gone, retry_s, text)
    assert f"the retry's sign step lasted {retry_s:.1f}s and returned a signature that did not check out" in text, text


_CONFIGURED_KEY = "the key configured for Touch ID"


def _assert_states_only_what_was_measured(label: str, text: str, *dirs: Path) -> None:
    """No claim of a touch and no claim that nothing was signed. The only ``Touch ID`` allowed is the configured-key
    phrase a Touch ID key's ``who`` carries; the evidence paths come from the test's own name, so they are masked."""
    for where in dirs:
        text = text.replace(str(where), "<dir>")
    assert "Touch ID's" not in text and "nothing was signed" not in text, (label, text)
    assert "touch" not in text.replace(_CONFIGURED_KEY, "").lower(), (label, text)


def test_no_emitted_empty_refusal_text_claims_a_touch_or_that_nothing_was_signed(trig):
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    retried = str(err.value)
    _assert_states_only_what_was_measured("empty, retried", retried, trig.dir)
    assert "no verifiable signature was returned" in retried and _CONFIGURED_KEY in retried, retried
    bounded = ssh_signer(trig.key.path, namespace=TERMINAL_NS, timeout=20.0)[1]
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE], [20.0 - keys._SK_RETRY_MIN_S + 0.5]
    with pytest.raises(keys.EmptySkSignature) as err:
        bounded(MESSAGE)
    unretried = str(err.value)
    _assert_states_only_what_was_measured("empty, no retry", unretried, trig.dir)
    assert "no second sign was started" in unretried, unretried


def test_no_emitted_non_empty_refusal_text_claims_a_touch_or_that_nothing_was_signed(rig):
    rig.fake.plan, rig.fake.durations = [0x21, 0x21], [0.2, 0.3]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    _assert_states_only_what_was_measured("non-empty, retried", str(err.value), rig.dir)
    assert "no verifiable signature was returned" in str(err.value), str(err.value)
    bounded = _bounded(rig, 20.0)
    rig.fake.plan, rig.fake.durations = [0x21, None], [20.0 - keys._SK_RETRY_MIN_S + 0.5]
    with pytest.raises(KeyError_) as err:
        bounded(MESSAGE)
    _assert_states_only_what_was_measured("non-empty, no retry", str(err.value), rig.dir)
    assert "no second sign was started" in str(err.value), str(err.value)
    rig.fake.plan = [0x00, None]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    _assert_states_only_what_was_measured("no presence flag", str(err.value), rig.dir)


def test_each_attempt_is_described_by_its_own_measured_answer(trig):
    """A mixed pair: neither text says "either" nor "the same kind"; each answer is named as that attempt measured it."""
    good_but_wrong = trig.key.sign(MESSAGE, TERMINAL_NS, sig_flags=0x21).decode()
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, good_but_wrong], [0.34, 0.5]
    with pytest.raises(KeyError_) as err:                                  # empty first, then a non-empty one that fails
        trig.sign(MESSAGE)
    text = str(err.value)
    assert not isinstance(err.value, keys.EmptySkSignature)
    assert "attempt 1 returned a signature envelope with r and s empty" in text, text
    assert "the retry's sign step lasted 0.5s and returned a signature that did not check out" in text, text
    assert "either" not in text and "the same kind" not in text, text
    trig.fake.plan, trig.fake.durations = [good_but_wrong, SAVED_EMPTY_ENVELOPE], [0.5, 0.34]
    with pytest.raises(keys.EmptySkSignature) as err:                      # a non-empty first, then an empty one
        trig.sign(MESSAGE)
    text = str(err.value)
    assert "attempt 1 returned a signature that did not check out" in text, text
    assert "the retry's sign step lasted 0.3s and returned a signature envelope with r and s empty" in text, text
    assert "either" not in text and "the same kind" not in text, text


@pytest.mark.parametrize("r, s, which", [(b"", b"\x01" * 32, "r empty"), (b"\x01" * 32, b"", "s empty"), (b"", b"", "r and s empty")])
def test_the_refusal_says_which_of_r_and_s_is_empty(trig, r, s, which):
    sig = _armored(trig.key, TERMINAL_NS, r, s)
    trig.fake.plan, trig.fake.durations = [sig, sig], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    first = str(err.value).splitlines()[0]
    assert f"ssh-keygen returned a signature envelope with {which} after 0.3s" in first, first
    assert (err.value.r_len, err.value.s_len) == (len(r), len(s))
    assert "no r or s" not in first and "empty signature" not in first, first


def test_the_empty_refusal_names_whose_key_it_was_as_a_configuration_fact(rig, monkeypatch):
    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)
    sig = _armored(rig.key, NS, b"", b"")
    rig.fake.plan, rig.fake.durations = [sig, sig], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature) as err:
        rig.sign(MESSAGE)
    assert "for the security key: no verifiable signature was returned" in str(err.value), str(err.value)


def test_an_empty_envelope_with_no_time_for_a_retry_is_the_named_refusal_and_says_so(trig):
    sign = ssh_signer(trig.key.path, namespace=TERMINAL_NS, timeout=20.0)[1]
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE], [20.0 - keys._SK_RETRY_MIN_S + 0.5]
    with pytest.raises(keys.EmptySkSignature) as err:
        sign(MESSAGE)
    text = str(err.value)
    assert trig.fake.signs == 1 and "no second sign was started" in text and "with r and s empty" in text, text


def test_the_evidence_file_gains_the_timing_the_lengths_the_hash_and_the_provider(trig):
    from levain.cockpit.custody import APPLE_PROVIDER
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature):
        trig.sign(MESSAGE)
    first, second = _evidence(trig)
    for ev, took in ((first, 0.34), (second, 0.3)):
        assert ev["format"] == "levain-sign-failure/1"                   # the existing fields stay, the format is not bumped
        assert ev["flags"] == "0x21" and ev["counter"] == 1 and ev["namespace"] == TERMINAL_NS
        assert ev["elapsed_s"] == pytest.approx(took, abs=1e-3)
        assert (ev["r_len"], ev["s_len"]) == (0, 0)
        assert ev["signature_sha256"] == hashlib.sha256(SAVED_EMPTY_ENVELOPE.encode()).hexdigest()
        assert ev["provider"] == APPLE_PROVIDER
    assert first["signature_sha256"] == second["signature_sha256"] and (first["attempt"], second["attempt"]) == (1, 2)
    assert first["context"] == {**CANNED_CONTEXT, "agent_socket_in_child": False}, first["context"]
    assert first["flags_note"] == keys._FLAGS_NOTE == (
        "observed 2026-11-18 (one sigdiag run, one good signature): flags 0x21, counter 1")
    # the board row's DONE line: the evidence file says why no root cause is named (a dated judgement, never a cause)
    assert first["cause_note"] == keys._CAUSE_NOTE and first["cause_note"].startswith("root cause not determined [judged by")
    for p in trig.dir.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_the_evidence_of_a_real_envelope_holds_its_r_and_s_lengths_and_a_null_provider(rig, monkeypatch):
    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)
    rig.fake.plan, rig.fake.durations = [0x21, None], [1.5]
    rig.sign(MESSAGE)
    (ev,) = _evidence(rig)
    assert 31 <= ev["r_len"] <= 33 and 31 <= ev["s_len"] <= 33 and ev["elapsed_s"] == pytest.approx(1.5, abs=1e-3)
    assert ev["provider"] is None and len(ev["signature_sha256"]) == 64


def test_an_empty_first_answer_then_a_good_second_one_is_returned(trig):
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, None], [0.34, 2.0]
    assert _sshsig_type_and_flags(trig.sign(MESSAGE))[1] == 0x01 and trig.fake.signs == 2


def test_an_empty_first_answer_then_a_different_failure_is_not_the_named_refusal(trig):
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, 0x21], [0.34, 3.0]
    with pytest.raises(KeyError_) as err:
        trig.sign(MESSAGE)
    assert not isinstance(err.value, keys.EmptySkSignature)
    assert "both attempts" in str(err.value) and "attempt 1 returned a signature envelope with r and s empty" in str(err.value)


def test_a_flipped_flags_answer_is_still_the_old_refusal_not_the_named_one(rig):
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    assert not isinstance(err.value, keys.EmptySkSignature) and "r and s empty" not in str(err.value)


# --- levain#125 F, G: the failure context and the provider trace -----------------------------------------------------------

class _ProbeRunner:
    """A ``subprocess.run`` for the context probes: one canned stdout per tool, or a failure. ``took`` is how long each call
    takes on ``clock``."""

    def __init__(self, clock, outputs: dict, took: float = 0.0) -> None:
        self.clock, self.outputs, self.took, self.calls = clock, outputs, took, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw.get("timeout")))
        if self.took > kw["timeout"]:
            self.clock.t += kw["timeout"]
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        self.clock.t += self.took
        out = self.outputs[tuple(argv)]
        if isinstance(out, BaseException):
            raise out
        rc, text = out
        return subprocess.CompletedProcess(argv, rc, stdout=text.encode(), stderr=b"")


ROOT = ("/usr/sbin/ioreg", "-n", "Root", "-d1", "-a")
HID = ("/usr/sbin/ioreg", "-c", "IOHIDSystem", "-r", "-d1")
PM = ("/usr/sbin/ioreg", "-r", "-n", "IOPMrootDomain", "-d1")
MGR = ("/bin/launchctl", "managername")
GOOD_OUTPUTS = {
    ROOT: (0, '<plist><dict>\n\t<key>IOConsoleLocked</key>\n\t<true/>\n\t<key>IOConsoleUsers</key><array><dict>'
              '<key>kCGSSessionUserNameKey</key><string>someone</string></dict></array></dict></plist>'),
    HID: (0, '    | {\n      "HIDIdleTime" = 5000000000\n    }\n'),
    PM: (0, '    | {\n      "System Capabilities" = 15\n    }\n'),
    MGR: (0, "Aqua\n"),
}


@pytest.fixture
def probes(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(keys, "time", clock)
    runner = _ProbeRunner(clock, dict(GOOD_OUTPUTS))
    monkeypatch.setattr(keys.subprocess, "run", runner)
    monkeypatch.setenv("SSH_CONNECTION", "10.1.2.3 5000 10.9.9.9 22")
    monkeypatch.delenv("SSH_TTY", raising=False)
    runner.clock = clock
    return runner


def test_the_failure_context_reads_the_probes_and_names_the_environment_by_name_only(probes):
    ctx = _REAL_FAILURE_CONTEXT(lambda: 1000.0)
    assert ctx["console_locked"] == {"present": True, "value": True}
    assert ctx["hid_idle_s"] == 5.0 and ctx["system_capabilities"] == 15 and ctx["launchctl_manager"] == "Aqua"
    assert ctx["ssh_connection_set"] is True and ctx["ssh_tty_set"] is False
    assert isinstance(ctx["stdin_tty"], bool) and isinstance(ctx["stderr_tty"], bool)
    dumped = json.dumps(ctx)
    assert "10.1.2.3" not in dumped and "someone" not in dumped              # no value of the environment, no user name
    for argv, _t in probes.calls:                                             # absolute tools, argv lists
        assert isinstance(argv, list) and argv[0].startswith("/")


def test_a_missing_console_flag_is_absent_not_unavailable(probes):
    probes.outputs[ROOT] = (0, "<plist><dict><key>IOKitBuildVersion</key><string>x</string></dict></plist>")
    assert _REAL_FAILURE_CONTEXT(lambda: 1000.0)["console_locked"] == {"present": False, "value": None}


@pytest.mark.parametrize("bad", [OSError(2, "no such file"), (1, ""), (0, "nothing recognisable"), (0, "")])
def test_every_probe_that_fails_is_recorded_unavailable(probes, bad):
    for key in (ROOT, HID, PM, MGR):
        probes.outputs[key] = bad
    ctx = _REAL_FAILURE_CONTEXT(lambda: 1000.0)
    for name in ("console_locked", "hid_idle_s", "system_capabilities", "launchctl_manager"):
        assert ctx[name] == "unavailable", (name, ctx)
    assert ctx["ssh_connection_set"] is True                                   # the environment facts never need a probe


def test_a_hung_probe_is_cut_and_the_whole_context_stays_inside_its_budget(probes):
    probes.took = float("inf")
    start = probes.clock.t
    ctx = _REAL_FAILURE_CONTEXT(lambda: 1000.0)
    assert probes.clock.t - start <= keys._CONTEXT_BUDGET_S + 1e-6
    assert all(t <= keys._CONTEXT_PROBE_S + 1e-9 for _a, t in probes.calls), probes.calls
    assert ctx["hid_idle_s"] == "unavailable" and ctx["launchctl_manager"] == "unavailable"


def test_the_context_never_runs_a_probe_past_what_is_left_of_the_sign_deadline(probes):
    ctx = _REAL_FAILURE_CONTEXT(lambda: 0.0)
    assert probes.calls == [] and ctx["console_locked"] == "unavailable" and ctx["ssh_connection_set"] is True
    ctx = _REAL_FAILURE_CONTEXT(lambda: 0.25)
    assert probes.calls and all(t <= 0.25 + 1e-9 for _a, t in probes.calls), probes.calls


def test_the_context_budget_is_under_three_seconds_and_inside_the_probe_count():
    assert keys._CONTEXT_BUDGET_S < 3.0 and keys._CONTEXT_PROBE_S <= keys._CONTEXT_BUDGET_S


def test_an_empty_answer_without_a_context_probe_still_refuses_and_the_message_holds_no_context(trig, monkeypatch):
    monkeypatch.setattr(keys, "_failure_context", lambda left: (_ for _ in ()).throw(RuntimeError("probe blew up")))
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    assert "context:" not in str(err.value) and "unavailable" not in str(err.value).splitlines()[0]
    assert _evidence(trig)[0]["context"] == "unavailable"


PROMPT = b"Signing data on standard input\nConfirm user presence for key ECDSA-SK SHA256:abc\n"
DEBUG = (b"debug3: ssh_get_authentication_socket_path: path '/var/run/x/Listeners'\n"
         b"debug3: hash_file: final hash: a969ccc0\ndebug1: sshsk_sign: provider \"/p\", key ECDSA-SK, flags 0x21\n")


def test_a_sk_sign_is_run_with_the_provider_trace_and_a_plain_key_is_not(rig, tmp_path, monkeypatch):
    rig.fake.plan = [None]
    rig.sign(MESSAGE)
    argv = rig.fake.argvs[0]
    assert "-vvv" in argv and argv.index("-vvv") < argv.index("-Y")
    # the real ssh-keygen, a plain key: no trace flag is added
    d = tmp_path / "plain"
    d.mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(d / "k")], check=True)
    seen: list = []
    real = rig.fake.real_run

    def spy(argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            seen.append((list(argv), kw.get("stderr")))
        return real(argv, **kw)

    monkeypatch.setattr(keys.subprocess, "run", spy)
    ssh_signer(d / "k")[1](b"x")
    assert seen and "-vvv" not in seen[0][0] and seen[0][1] is None


def test_the_presence_prompt_still_reaches_the_real_stderr_and_the_debug_lines_do_not(rig, capfd):
    rig.fake.stderr_text = PROMPT + DEBUG
    rig.fake.plan = [None]
    rig.sign(MESSAGE)
    err = capfd.readouterr().err
    assert "Signing data on standard input\nConfirm user presence for key ECDSA-SK SHA256:abc\n" in err, err
    assert "debug" not in err, err


def test_a_failed_sign_keeps_the_debug_lines_and_whether_the_prompt_was_printed(trig, capfd):
    trig.fake.stderr_text = PROMPT + DEBUG
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature):
        trig.sign(MESSAGE)
    first, _second = _evidence(trig)
    assert first["provider_trace"] == DEBUG.decode() and first["stderr_prompt_line"] is True
    assert "Confirm user presence" not in first["provider_trace"]
    assert "Confirm user presence" in capfd.readouterr().err                   # and the person still saw it live


def test_a_failed_sign_with_no_prompt_line_says_so(trig):
    trig.fake.stderr_text = DEBUG
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature):
        trig.sign(MESSAGE)
    assert _evidence(trig)[0]["stderr_prompt_line"] is False


def test_the_kept_trace_is_the_last_stretch_of_the_debug_lines(trig):
    lines = b"".join(b"debug1: line %05d padding padding padding\n" % i for i in range(1000))
    trig.fake.stderr_text = lines
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature):
        trig.sign(MESSAGE)
    trace = _evidence(trig)[0]["provider_trace"]
    assert len(trace.encode()) <= keys._TRACE_KEEP_BYTES and trace.endswith("line 00999 padding padding padding\n")
    assert trace.startswith("debug1: line ")                               # a cut that fell mid-line drops that line's start


def _wait(pred, seconds=3.0):
    import time as _t
    end = _t.monotonic() + seconds
    while _t.monotonic() < end:
        if pred():
            return True
        _t.sleep(0.02)
    return pred()


def test_the_tee_echoes_a_prompt_without_a_newline_at_once_and_holds_a_possible_debug_prefix(capfd):
    tee = keys._StderrTee()
    try:
        os.write(tee.write_fd, b"Enter PIN for key: ")                     # no newline: still the person's prompt
        got = []
        assert _wait(lambda: got.append(capfd.readouterr().err) or "Enter PIN for key: " in "".join(got))
        os.write(tee.write_fd, b"\ndeb")                                  # could be the start of a debug line: held back
        import time as _t
        _t.sleep(0.2)
        assert "deb" not in capfd.readouterr().err
        os.write(tee.write_fd, b"ug1: split across two reads\nde\n")        # then it is debug; a short plain line is echoed
        got = []
        assert _wait(lambda: got.append(capfd.readouterr().err) or "de\n" in "".join(got))
        assert "split across" not in "".join(got)
    finally:
        trace, prompt_seen = tee.finish()
    assert trace == "debug1: split across two reads\n" and prompt_seen is False


def test_a_sign_that_times_out_leaves_no_reader_running_and_stays_inside_the_deadline(rig):
    import threading
    import time as _t
    rig.fake.stderr_text = PROMPT + DEBUG
    rig.fake.plan = ["timeout"]
    start = _t.monotonic()
    with pytest.raises(KeyError_, match="TimeoutExpired"):
        rig.sign(MESSAGE)
    assert _t.monotonic() - start < 2.0
    assert not [t for t in threading.enumerate() if t.name == keys._TEE_THREAD_NAME and t.is_alive()]


def test_a_lingering_descriptor_holder_does_not_keep_the_reader_alive_past_the_sign(rig, monkeypatch):
    """A helper that outlives ssh-keygen keeps the pipe's write end open, so the reader never sees end-of-file: it must
    stop on its own once the sign step is over."""
    import threading
    import time as _t
    kept: list[int] = []
    orig = rig.fake.__call__

    def holder(argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign" and isinstance(kw.get("stderr"), int):
            kept.append(os.dup(kw["stderr"]))                              # never closed until the test is over
        return orig(argv, **kw)

    monkeypatch.setattr(keys.subprocess, "run", holder)
    rig.fake.plan = [None]
    try:
        start = _t.monotonic()
        rig.sign(MESSAGE)
        assert _t.monotonic() - start < 2.0
        assert _wait(lambda: not [t for t in threading.enumerate() if t.name == keys._TEE_THREAD_NAME and t.is_alive()])
    finally:
        for fd in kept:
            os.close(fd)


def test_a_real_child_and_a_lingering_grandchild_through_the_tee(capfd):
    """Real processes, no key: a child prints a prompt and debug lines on the tee's descriptor and leaves a grandchild
    holding it open; the person's lines come out live, the debug lines are kept, and the tee stops without the pipe ending."""
    import sys
    import threading
    import time as _t
    code = ("import subprocess,sys\n"
            "sys.stderr.write('Confirm user presence for key X\\n'); sys.stderr.write('debug1: hello\\n'); sys.stderr.flush()\n"
            "subprocess.Popen(['/bin/sleep','2'])\n")
    tee = keys._StderrTee()
    start = _t.monotonic()
    try:
        subprocess.run([sys.executable, "-I", "-c", code], stderr=tee.write_fd, check=True)
    finally:
        trace, prompt_seen = tee.finish()
    assert _t.monotonic() - start < 1.5
    assert trace == "debug1: hello\n" and prompt_seen is True
    assert "Confirm user presence for key X\n" in capfd.readouterr().err
    assert not [t for t in threading.enumerate() if t.name == keys._TEE_THREAD_NAME and t.is_alive()]


# --- levain#125 fix lane 1 (L1/L2 findings): the tee's reader, fd limits, thread start, the dated notes ----------------------

def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@contextlib.contextmanager
def _fill_fds_to(n: int):
    """Open descriptors on /dev/null until the next free number is at least ``n`` (raising the soft limit when it must),
    and yield the list. On exit the descriptors are closed first, then the soft limit is put back to what it was."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < n + 64:
        if hard != resource.RLIM_INFINITY and hard < n + 64:
            pytest.skip("the descriptor limit cannot be raised past 1100")
        resource.setrlimit(resource.RLIMIT_NOFILE, (n + 64, hard))
    held: list[int] = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd >= n:
                break
        yield held
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_descriptor_helper_puts_the_soft_limit_back():
    """The helper raises the process-wide soft limit; a test that runs after it sees the original, also on an error."""
    import resource
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    with _fill_fds_to(1100) as held:
        assert held and max(held) >= 1100
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before
    with pytest.raises(RuntimeError):
        with _fill_fds_to(1100):
            raise RuntimeError("a test body failed")
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


def test_a_pipe_descriptor_at_or_above_1024_still_signs_and_keeps_the_trace(rig, capfd):
    """L1#1: select.select raises ValueError for a descriptor >= FD_SETSIZE; the reader died and the child was left a closed
    read end. The reader polls now."""
    with _fill_fds_to(1100):
        before = _open_fds()
        seen: list[int] = []
        real_init = keys._StderrTee.__init__

        def init(self):
            real_init(self)
            seen.append(self._r)

        orig = rig.fake.__call__

        def late_writer(argv, **kw):
            if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign" and isinstance(kw.get("stderr"), int):
                _wait(lambda: False, 0.3)                                  # a reader that was going to die has died by now
            return orig(argv, **kw)

        import pytest as _pt
        with _pt.MonkeyPatch.context() as mp:
            mp.setattr(keys._StderrTee, "__init__", init)
            mp.setattr(keys.subprocess, "run", late_writer)
            rig.fake.stderr_text = PROMPT + DEBUG
            rig.fake.plan = [0x21, 0x21]
            with pytest.raises(KeyError_):
                rig.sign(MESSAGE)
        assert seen and min(seen) >= 1024, seen
        ev = _evidence(rig)
        assert ev[0]["provider_trace"] == DEBUG.decode(), ev[0]["provider_trace"]
        assert "Confirm user presence" in capfd.readouterr().err
        assert _open_fds() == before


def test_a_real_child_signs_to_a_tee_whose_descriptors_are_at_or_above_1024():
    import sys
    with _fill_fds_to(1100):
        code = ("import signal,sys,time\nsignal.signal(signal.SIGPIPE, signal.SIG_DFL)\ntime.sleep(0.3)\n"
                "sys.stderr.write('debug1: x\\n'); sys.stderr.flush()\n")
        tee = keys._StderrTee()
        try:
            assert tee._r >= 1024
            cp = subprocess.run([sys.executable, "-I", "-c", code], stderr=tee.write_fd, check=False)
        finally:
            trace, _p = tee.finish()
        assert cp.returncode == 0 and trace == "debug1: x\n", (cp.returncode, trace)


def test_a_reader_that_dies_does_not_leave_the_child_a_closed_pipe(monkeypatch):
    """L1#1: whatever ends the reader early, ssh-keygen must not be sent SIGPIPE for writing to it afterwards."""
    import sys
    import threading

    def boom(self, chunk):
        raise RuntimeError("the reader's own bug")

    monkeypatch.setattr(keys._StderrTee, "_feed", boom)
    code = ("import signal,sys,time\nsignal.signal(signal.SIGPIPE, signal.SIG_DFL)\ntime.sleep(0.3)\n"
            "sys.stderr.write('Confirm user presence for key X\\n'); sys.stderr.write('x' * 5000); sys.stderr.flush()\n")
    before = _open_fds()
    tee = keys._StderrTee()
    try:
        os.write(tee.write_fd, b"first bytes\n")                           # the reader meets its bug on these
        _wait(lambda: False, 0.3)                                          # and is gone (or draining) before the child writes
        cp = subprocess.run([sys.executable, "-I", "-c", code], stderr=tee.write_fd, check=False)
    finally:
        tee.finish()
    assert cp.returncode == 0, cp.returncode                              # -13 would be SIGPIPE
    assert not [t for t in threading.enumerate() if t.name == keys._TEE_THREAD_NAME and t.is_alive()]
    assert _open_fds() == before


def test_a_thread_that_cannot_start_leaves_no_descriptor_and_the_sign_runs_untraced(rig, monkeypatch):
    """L1#6, L2#9: Thread.start raising RuntimeError closes both pipe ends and the sign goes on without a trace."""
    import threading

    orig_start = threading.Thread.start

    def no_thread(self):
        if self.name == keys._TEE_THREAD_NAME:                             # the verifier's own helper thread still starts
            raise RuntimeError("can't start new thread")
        return orig_start(self)

    before = _open_fds()
    monkeypatch.setattr(threading.Thread, "start", no_thread)
    rig.fake.plan = [None]
    sig = rig.sign(MESSAGE)
    assert _sshsig_type_and_flags(sig) == (SK, 0x01)
    assert "-vvv" not in rig.fake.argvs[0]
    assert _open_fds() == before
    with pytest.raises(RuntimeError):
        keys._StderrTee()
    assert _open_fds() == before


def test_finish_closes_the_write_end_and_the_read_end():
    before = _open_fds()
    tee = keys._StderrTee()
    assert _open_fds() == before + 2
    w = tee.write_fd
    tee.finish()
    assert _wait(lambda: _open_fds() == before)
    with pytest.raises(OSError):
        os.fstat(w)
    tee.finish()                                                           # twice is a no-op
    assert _open_fds() == before


@pytest.mark.parametrize("r_len, s_len, empty", [(0, 32, True), (32, 0, True), (0, 0, True), (32, 32, False),
                                                 (None, None, False), (None, 0, True), (0, None, True)])
def test_a_refusal_is_empty_when_r_or_s_is_empty(r_len, s_len, empty):
    a = keys._SkRefusal(0x21, "carried", "detail", "evidence", 0.3, r_len, s_len)
    assert a.empty is empty


def test_the_trace_never_starts_in_the_middle_of_a_line_or_a_character():
    tee = keys._StderrTee()
    line = ("debug1: " + "\u00e9" * 12 + "\n").encode()
    try:
        for _ in range(2000):
            os.write(tee.write_fd, line)
    finally:
        trace, _p = tee.finish()
    assert len(trace.encode()) <= keys._TRACE_KEEP_BYTES and "\ufffd" not in trace and trace.startswith("debug1: ")
    assert trace.endswith("\n")


@pytest.mark.parametrize("bad", [None, 5, b"x", ["a"], object()])
def test_the_lengths_function_never_raises(bad):
    from levain.authority_sig import sshsig_ecdsa_lengths
    assert sshsig_ecdsa_lengths(bad) is None


def test_the_context_names_whether_an_agent_socket_was_in_the_signers_environment(trig, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/never-printed/agent.sock")
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature) as err:
        trig.sign(MESSAGE)
    assert _evidence(trig)[0]["context"]["agent_socket_in_child"] is True
    assert "never-printed" not in "".join(p.read_text() for p in trig.dir.iterdir()) + str(err.value)
    monkeypatch.delenv("SSH_AUTH_SOCK")
    trig.fake.plan, trig.fake.durations = [SAVED_EMPTY_ENVELOPE, SAVED_EMPTY_ENVELOPE], [0.34, 0.3]
    with pytest.raises(keys.EmptySkSignature):
        trig.sign(MESSAGE)
    assert _evidence(trig)[-1]["context"]["agent_socket_in_child"] is False


def test_the_stderr_prompt_line_field_records_what_ssh_keygen_printed_and_is_documented_so():
    doc = " ".join((keys._write_sign_failure.__doc__ or "").split())
    assert "does NOT show that no prompt appeared" in doc, doc
    assert "nothing secret" not in doc and "2026-11-18" in doc


def test_the_probe_children_get_no_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=b"x", stderr=b"")

    monkeypatch.setattr(keys.subprocess, "run", fake_run)
    assert keys._probe_output(["/bin/echo"], keys.time.monotonic() + 5, lambda: 5.0) == "x"
    assert seen["stdin"] == subprocess.DEVNULL


# --- levain#125 fix lane 3: an answer that is EMPTY (exit 0, no stdout) proves only that no signature text came back -----
# What the key did is not measured; the two refusals say the measured part and nothing more.

_EMPTY_MEASURED = "no signature text was returned"


def _empty_answer_error(sign) -> KeyError_:
    with pytest.raises(KeyError_) as err:
        sign(MESSAGE)
    assert not isinstance(err.value, keys.EmptySkSignature)
    return err.value


def test_an_empty_answer_from_a_touch_id_sk_key_states_only_that_no_signature_text_was_returned(trig):
    from levain.consent import terminal_sign as ts
    trig.fake.plan, trig.fake.durations = ["cancel"], [0.4]
    exc = _empty_answer_error(trig.sign)
    text = str(exc)
    assert trig.fake.signs == 1 and "ssh-keygen ran 0.4s" in text, text
    assert _EMPTY_MEASURED in text and "nothing was signed" not in text, text
    # the first line is the one terminal_sign reads, unchanged: it still files this answer as a cancel
    assert text.startswith(f"Touch ID was cancelled or did not complete for {trig.fp} "), text
    assert "no signature was made" not in text and "nothing was signed" not in text, text
    assert ts._classify(exc) == ("cancelled", text.splitlines()[0])


def test_an_empty_answer_from_an_sk_key_without_the_touch_id_provider_states_only_that_no_signature_text_was_returned(
        rig, monkeypatch):
    from levain.consent import terminal_sign as ts
    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)
    rig.fake.plan, rig.fake.durations = ["cancel"], [0.4]
    exc = _empty_answer_error(rig.sign)
    text = str(exc)
    assert rig.fake.signs == 1 and _EMPTY_MEASURED in text and "nothing was signed" not in text, text
    assert "Touch ID was cancelled" not in text and str(rig.key.path) in text, text
    assert ts._classify(exc) == ("signer-error", text.splitlines()[0])


def test_an_empty_answer_from_a_plain_key_states_only_that_no_signature_text_was_returned(tmp_path, monkeypatch):
    from levain.consent import terminal_sign as ts
    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(d / "k")], check=True)
    fp, sign = ssh_signer(d / "k")
    real = subprocess.run
    signs: list[int] = []

    def empty_sign(argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            signs.append(1)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        return real(argv, **kw)

    monkeypatch.setattr(keys.subprocess, "run", empty_sign)
    exc = _empty_answer_error(sign)
    text = str(exc)
    assert len(signs) == 1 and _EMPTY_MEASURED in text and "nothing was signed" not in text, text
    assert fp in text and ts._classify(exc) == ("signer-error", text.splitlines()[0])
