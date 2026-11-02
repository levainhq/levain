"""levain.consent.terminal_sign (TERMSIGN L1): the message, the strict parse, the key lookup, the sign reasons,
the verify and the classifier.

No test here reaches a Secure Enclave key or raises a Touch ID prompt: the keys are plain ed25519 keys made with
ssh-keygen into a tmp dir, signers are injected, and the real-home lookup is pointed at a tmp dir for every test.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.authority_sig import VerifierUnavailable
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.cockpit.keys import KeyError_
from levain.consent import terminal_sign as ts

AT = datetime(2026, 11, 2, 18, 52, 23, tzinfo=timezone.utc)
NONCE = "0123456789abcdef"

E1 = ts.Entry("np-9b9b9662", "rv-aaaa1111", "Add a Levain 1.0 row?", "information",
              answer="(a) Add a Levain 1.0 row", note="for sure 100%")
E2 = ts.Entry("np-2", "rv-bbbb2222", "Push it?", "authority", text="yes, push", ruling='{"verb":"allow"}')


def _msg(entries=(E1,), typed="1a for sure 100%", **kw) -> bytes:
    args = dict(store="flow-needs-phill", install="flow-3ce2832a8fe94ea7", session="sess-1", view="view-1",
                typed=typed, entries=entries, nonce=NONCE, at=AT)
    args.update(kw)
    return ts.build_message(**args)


@pytest.fixture(autouse=True)
def _no_real_key(tmp_path, monkeypatch):
    """The real home's key directory and the real signer are unreachable from every test."""
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "no-real-keys")

    def boom(path):
        raise AssertionError("a test reached the default (real) signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)


def _plain_key(d: Path, name: str = ts.KEY_LABEL, *, sidecar: bool = True) -> Path:
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    path = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True)
    if sidecar:
        (d / (name + SIDECAR_SUFFIX)).write_text(json.dumps(
            {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER,
             "ctk_hash": "A" * 40, "label": name}))
    return path


def _signers(path: Path, principal="phill", ns=ts.NAMESPACE) -> str:
    pub = " ".join(Path(str(path) + ".pub").read_text().split()[:2])
    return f'{principal} namespaces="{ns}" {pub}\n'


def _sign_with(path: Path, message: bytes, ns=ts.NAMESPACE) -> str:
    fp, signer = ckeys.ssh_signer(path, namespace=ns)
    return signer(message)


# --- golden bytes -----------------------------------------------------------------------------------

GOLDEN_1 = (
    "levain-terminal-answer/1\n"
    "store: flow-needs-phill\n"
    "install: flow-3ce2832a8fe94ea7\n"
    "session: sess-1\n"
    "view: view-1\n"
    "typed-sha256: " + hashlib.sha256(b"1a for sure 100%").hexdigest() + "\n"
    "typed: 1a for sure 100%\n"
    "nonce: 0123456789abcdef\n"
    "at: 2026-11-02T18:52:23Z\n"
    "entry: 1\n"
    "  item: np-9b9b9662\n"
    "  row_version: rv-aaaa1111\n"
    "  asked: Add a Levain 1.0 row?\n"
    "  kind: information\n"
    "  answer: (a) Add a Levain 1.0 row\n"
    "  note: for sure 100%\n"
).encode()


def test_golden_one_entry():
    assert _msg() == GOLDEN_1


def test_golden_two_entries_free_text_and_ruling():
    got = _msg((E1, E2), typed="1a, 2 yes, push").decode()
    assert got.endswith(
        "entry: 2\n  item: np-2\n  row_version: rv-bbbb2222\n  asked: Push it?\n  kind: authority\n"
        "  text: yes, push\n  ruling: {\"verb\":\"allow\"}\n")
    assert got.count("entry: ") == 2 and got.startswith(GOLDEN_1.decode().split("typed-sha256")[0])
    assert "typed: 1a, 2 yes, push\n" in got


def test_golden_levain_store_countersign_and_block_hash():
    block = "line one\n1a"
    got = _msg(store="levain-questions:abc-1", typed="1a", typed_block=block, countersign_of="2026-11-02T18:00:00Z")
    text = got.decode()
    assert text.splitlines()[1] == "store: levain-questions:abc-1"
    assert f"typed-sha256: {hashlib.sha256(block.encode()).hexdigest()}\ntyped: 1a\n" in text
    assert "at: 2026-11-02T18:52:23Z\ncountersign-of: 2026-11-02T18:00:00Z\nentry: 1\n" in text
    parsed = ts.parse_verified(got)
    assert parsed.countersign_of == "2026-11-02T18:00:00Z" and parsed.typed_matches_hash is False


def test_golden_sha_is_pinned():
    # a change to the signed bytes is a change of protocol: it must be a visible edit here
    assert hashlib.sha256(GOLDEN_1).hexdigest() == hashlib.sha256(_msg()).hexdigest()
    assert len(GOLDEN_1) < ts.MAX_BYTES


# --- build refusals ---------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    dict(entries=()),
    dict(typed="two\nlines"),
    dict(typed="x" * (ts.LINE_MAX + 1)),
    dict(typed=""),
    dict(store="other-store"),
    dict(install="has space"),
    dict(nonce="XYZ"),
    dict(entries=(ts.Entry("np 1", "rv", "q", "information", answer="a"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q\nr", "information", answer="a"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q", "information"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q", "information", answer="a", text="b"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q", "bogus", answer="a"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q", "information", answer="a", ruling="{}"),)),
    dict(entries=(ts.Entry("np-1", "rv", "q", "information", answer="a\x00"),)),
])
def test_build_refuses(bad):
    with pytest.raises(ts.MessageError):
        _msg(**bad)


def test_build_enforces_size_bound():
    many = tuple(ts.Entry(f"np-{i}", "rv", "x" * 1500, "information", answer="a") for i in range(8))
    with pytest.raises(ts.MessageError, match="bound"):
        _msg(many)


def test_typed_is_nfc():
    got = _msg(typed="café").decode()
    assert "typed: café\n" in got
    assert ts.parse_verified(got.encode()).typed_matches_hash


# --- parse ------------------------------------------------------------------------------------------

def test_parse_roundtrip():
    p = ts.parse_verified(_msg((E1, E2), typed="1a, 2 yes, push"))
    assert p.store == "flow-needs-phill" and p.nonce == NONCE and p.at == AT and p.view == "view-1"
    assert p.entries == (E1, E2) and p.message_sha256 == hashlib.sha256(_msg((E1, E2), typed="1a, 2 yes, push")).hexdigest()


def _lines(entries=(E1,)) -> list[str]:
    return _msg(entries).decode().rstrip("\n").split("\n")


@pytest.mark.parametrize("mutate", [
    lambda L: L[:1] + ["junk"] + L[1:],                          # an unknown line
    lambda L: [L[0].replace("/1", "/2")] + L[1:],                # another format
    lambda L: L[:2] + L[3:],                                     # a missing field
    lambda L: L[:2] + [L[3], L[2]] + L[4:],                      # fields out of order
    lambda L: L + ["trailer: x"],                                # a field after the last entry
    lambda L: [x.replace("entry: 1", "entry: 2") for x in L],    # entry numbering
    lambda L: L[:9] + [L[9], "item: loose"] + L[10:],            # a field outside its block (unindented)
    lambda L: [x.replace("kind: information", "kind: other") for x in L],
    lambda L: L + L[9:],                                         # entry 1 repeated
    lambda L: L[:9],                                             # no entry at all
])
def test_parse_refuses(mutate):
    text = "\n".join(mutate(_lines())) + "\n"
    with pytest.raises(ts.MessageError):
        ts.parse_verified(text.encode())


def test_parse_refuses_shape():
    good = _msg()
    for bad in (good + b"\n", good[:-1], b"\xff\xfe", good + b"x" * ts.MAX_BYTES, "str", good.replace(b"Z\n", b"Q\n")):
        with pytest.raises(ts.MessageError):
            ts.parse_verified(bad)


def test_parse_refuses_ruling_on_information_entry():
    text = GOLDEN_1.decode() + "  ruling: {}\n"
    with pytest.raises(ts.MessageError, match="ruling"):
        ts.parse_verified(text.encode())


def test_fresh_bounds():
    p = ts.parse_verified(_msg())
    assert ts.fresh(p, AT) and ts.fresh(p, AT + timedelta(seconds=ts.STALE_S))
    assert not ts.fresh(p, AT + timedelta(seconds=ts.STALE_S + 1)) and not ts.fresh(p, AT - timedelta(seconds=1))


# --- find_key / status ------------------------------------------------------------------------------

def test_find_key_absent_dir_and_files(tmp_path):
    assert ts.find_key(tmp_path / "nope") is None
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    assert ts.find_key(d) is None
    assert not (d / ts.KEY_LABEL).exists()          # read-only: nothing created


def test_find_key_by_label_with_sidecar(tmp_path):
    d = tmp_path / "keys"
    _plain_key(d, "other-key")
    assert ts.find_key(d) is None                   # another label is not the terminal key
    path = _plain_key(d)
    k = ts.find_key(d)
    assert k is not None and k.path == path and k.public_key.startswith("ssh-ed25519 ")


def test_find_key_needs_touch_id_sidecar(tmp_path):
    d = tmp_path / "keys"
    _plain_key(d, sidecar=False)
    assert ts.find_key(d) is None
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text("not json")
    with pytest.raises(KeyError_):
        ts.find_key(d)


def test_find_key_refuses_open_dir_and_ignores_env(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    _plain_key(d)
    d.chmod(0o755)
    with pytest.raises(KeyError_, match="private"):
        ts.find_key(d)
    d.chmod(0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "evil"))
    monkeypatch.setenv("SSH_SK_PROVIDER", "/tmp/evil.dylib")
    assert ts.find_key() is None                    # the default dir is the (patched) real one, not LEVAIN_HOME


def test_real_key_dir_ignores_environment(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "x"))
    assert ts._real_key_dir() != tmp_path / ".levain" / "keys"
    assert ts._real_key_dir().parts[-2:] == (".levain", "keys")


def test_status_not_enrolled_and_enrolled(tmp_path):
    r = ts.status(tmp_path / "nope", identities=[])
    assert r.state == "not enrolled" and r.text.startswith("not enrolled")
    d = tmp_path / "keys"
    _plain_key(d)
    w = ts.status(d, identities=[], platform="darwin")
    assert w.state == "warning" and "WARNING" in w.text          # a plain key has no CTK identity here
    assert ts.status(d, identities=[], platform="linux").state == "warning"


# --- sign: every reason ------------------------------------------------------------------------------

def _factory(exc=None, signature="SIG"):
    def make(path):
        def signer(message):
            if exc is not None:
                raise exc
            return signature
        return "SHA256:" + "A" * 43, signer
    return make


def test_sign_remote_never_looks_for_a_key():
    def never(path):
        raise AssertionError("looked")
    out = ts.sign(_msg(), remote=True, signer_factory=never)
    assert (out.state, out.reason) == ("unsigned", "remote")


def test_sign_no_key(tmp_path):
    out = ts.sign(_msg(), key_dir=tmp_path / "nope", signer_factory=_factory())
    assert (out.state, out.reason) == ("unsigned", "no-key")


@pytest.mark.parametrize("message,reason", [
    ("Touch ID was cancelled or did not complete: no signature was made with SHA256:x, so nothing was signed",
     "cancelled"),
    ("ssh-keygen could not sign (TimeoutExpired)", "timeout"),
    ("ssh-keygen could not sign with /k (exit 1)", "unavailable"),
    ("ssh-keygen could not sign (FileNotFoundError)", "signer-error"),
    ("ssh-keygen returned no valid signature by SHA256:x (/k), so nothing was signed", "signer-error"),
])
def test_sign_reasons(tmp_path, message, reason):
    d = tmp_path / "keys"
    _plain_key(d)
    out = ts.sign(_msg(), key_dir=d, signer_factory=_factory(KeyError_(message)))
    assert (out.state, out.reason) == ("unsigned", reason) and out.signature is None and out.detail


def test_sign_other_failures_are_signer_errors(tmp_path):
    d = tmp_path / "keys"
    _plain_key(d)
    for exc in (VerifierUnavailable("ssh-keygen gone"), OSError(2, "no such file"),
                subprocess.TimeoutExpired("x", 1)):
        out = ts.sign(_msg(), key_dir=d, signer_factory=_factory(exc))
        assert out.state == "unsigned" and out.reason in ("signer-error", "timeout")
    # a malformed sidecar is a signer-error too, never a raise
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text("junk")
    out = ts.sign(_msg(), key_dir=d, signer_factory=_factory())
    assert (out.state, out.reason) == ("unsigned", "signer-error")


def test_sign_refuses_oversize_message(tmp_path):
    out = ts.sign(b"x" * (ts.MAX_BYTES + 1), key_dir=tmp_path, signer_factory=_factory())
    assert (out.state, out.reason) == ("unsigned", "signer-error")


def test_sign_narrows_and_restores_the_ssh_signer_bound(tmp_path):
    d = tmp_path / "keys"
    _plain_key(d)
    seen = []

    def make(path):
        def signer(message):
            seen.append(ckeys._SIGN_TIMEOUT)
            return "SIG"
        return "SHA256:" + "A" * 43, signer
    before = ckeys._SIGN_TIMEOUT
    out = ts.sign(_msg(), key_dir=d, signer_factory=make)
    assert out.state == "signed" and seen == [ts.SIGN_BOUND_S] and ckeys._SIGN_TIMEOUT == before
    ts.sign(_msg(), key_dir=d, signer_factory=_factory(KeyError_("x")))
    assert ckeys._SIGN_TIMEOUT == before            # restored on the failure path too


def test_default_factory_is_ssh_signer_in_our_namespace(monkeypatch):
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(ckeys, "ssh_signer", lambda p, **kw: calls.append((p, kw)) or ("fp", None))
    assert ts.default_signer_factory(Path("/k")) == ("fp", None)
    assert calls == [(Path("/k"), {"namespace": ts.NAMESPACE})]


def test_is_remote():
    assert ts.is_remote({}) is False
    assert ts.is_remote({"SSH_CONNECTION": "1 2 3 4"}) and ts.is_remote({"SSH_TTY": "/dev/ttys001"})
    assert ts.is_remote({}, presence="remote desk") and not ts.is_remote({}, presence="at the desk")


# --- end to end with a real (plain) signature -------------------------------------------------------

@pytest.fixture
def signed_world(tmp_path):
    d = tmp_path / "keys"
    path = _plain_key(d)
    message = _msg()
    out = ts.sign(message, key_dir=d, signer_factory=lambda p: ckeys.ssh_signer(p, namespace=ts.NAMESPACE))
    assert out.state == "signed", out
    return path, message, out, _signers(path)


def test_sign_then_verify(signed_world):
    path, message, out, signers = signed_world
    got = ts.verify(message, out.signature, signers, "phill", ctk=lambda pk: (True, "bio"))
    assert got.ok and got.fingerprint == out.fingerprint and got.ctk is True and got.flags is None
    assert "biometric-protected" in got.claim and got.badge == "signed: Touch ID, laptop"
    off = ts.verify(message, out.signature, signers, "phill", ctk=lambda pk: (None, "not on this Mac"))
    assert off.ok and off.ctk is None and "as recorded" in off.claim


def test_verify_refusals(signed_world, tmp_path):
    path, message, out, signers = signed_world
    ok = lambda pk: (True, "bio")        # noqa: E731
    sig = out.signature
    assert not ts.verify(message + b"x", sig, signers, "phill", ctk=ok).ok            # other bytes
    assert not ts.verify(message, sig, signers, "someone", ctk=ok).ok                 # other principal
    assert not ts.verify(message, sig, _signers(path, ns="flow-needs-phill"), "phill", ctk=ok).ok   # namespace-limited
    other = _plain_key(tmp_path / "k2")
    assert not ts.verify(message, sig, _signers(other), "phill", ctk=ok).ok           # another key
    wrong_ns = _sign_with(path, message, ns="flow-needs-phill")
    assert not ts.verify(message, wrong_ns, signers, "phill", ctk=ok).ok             # signed in another namespace
    assert not ts.verify(message, "garbage", signers, "phill", ctk=ok).ok
    assert not ts.verify(b"x" * (ts.MAX_BYTES + 1), sig, signers, "phill", ctk=ok).ok
    assert not ts.verify(message, sig, signers, "bad principal", ctk=ok).ok
    live_false = ts.verify(message, sig, signers, "phill", ctk=lambda pk: (False, "protection none"))
    assert not live_false.ok and live_false.ctk is False and "biometric" in live_false.problem


def test_verify_ctk_failure_weakens_not_fails(signed_world):
    path, message, out, signers = signed_world

    def broken(pk):
        raise KeyError_("sc_auth died")
    got = ts.verify(message, out.signature, signers, "phill", ctk=broken)
    assert got.ok and got.ctk is None and "sc_auth died" in got.ctk_detail


def test_verify_unavailable_verifier_raises(signed_world, tmp_path):
    path, message, out, signers = signed_world
    with pytest.raises(VerifierUnavailable):
        ts.verify(message, out.signature, signers, "phill", ssh_keygen=tmp_path / "missing")


# --- answer_class ------------------------------------------------------------------------------------

ROW = {"id": "np-9b9b9662", "row_version": "rv-aaaa1111", "what": "Add a Levain 1.0 row?"}


def _event(out, message, **kw):
    ev = {"ts": "2026-11-02T18:52:30Z", "answer": "(a) Add a Levain 1.0 row", "via": "terminal",
          "signed": {"format": ts.FORMAT, "message": message.decode(), "sig": out.signature, "entry": 1}}
    ev.update(kw)
    return ev


def _ctx(signers_text, **kw):
    c = {"signers": signers_text, "principal": "phill", "ctk": lambda pk: (True, "bio"),
         "event_ts": AT + timedelta(seconds=7)}
    c.update(kw)
    return c


def test_class_signed_terminal(signed_world):
    path, message, out, signers = signed_world
    got = ts.answer_class(_event(out, message), ROW, _ctx(signers))
    assert got.state == "signed-terminal" and got.signer_fp == out.fingerprint and not got.late


@pytest.mark.parametrize("row_patch,ev_patch,ctx_patch,why", [
    ({"id": "np-other"}, {}, {}, "item"),
    ({"row_version": "rv-moved"}, {}, {}, "version"),
    ({"what": "Another question?"}, {}, {}, "asked"),
    ({}, {"answer": "(b) other"}, {}, "recorded answer"),
    ({}, {"signed_entry": 2}, {}, "entry out of range"),
    ({}, {}, {"event_ts": AT + timedelta(minutes=11)}, "stale"),
    ({}, {}, {"event_ts": AT - timedelta(seconds=5)}, "future-dated"),
    ({}, {}, {"ctk": lambda pk: (False, "none")}, "ctk"),
    ({}, {}, {"signers": ""}, "no signers"),
])
def test_class_failed(signed_world, row_patch, ev_patch, ctx_patch, why):
    path, message, out, signers = signed_world
    ev = _event(out, message)
    if "signed_entry" in ev_patch:
        ev["signed"]["entry"] = ev_patch.pop("signed_entry")
    ev.update(ev_patch)
    got = ts.answer_class(ev, {**ROW, **row_patch}, _ctx(signers, **ctx_patch))
    assert got.state == "failed", why


def test_class_failed_for_forged_blocks(signed_world):
    path, message, out, signers = signed_world
    ctx = _ctx(signers)
    ev = _event(out, message)
    ev["signed"]["message"] = message.decode().replace("np-9b9b9662", "np-other")     # moved to another row
    assert ts.answer_class(ev, {**ROW, "id": "np-other"}, ctx).state == "failed"
    ev = _event(out, message)
    ev["signed"]["format"] = "levain-sign-choice/1"
    assert ts.answer_class(ev, ROW, ctx).state == "failed"
    ev = _event(out, message)
    ev["signed"]["message_sha256"] = "0" * 64
    assert ts.answer_class(ev, ROW, ctx).state == "failed"
    assert ts.answer_class(_event(out, message), None, ctx).state == "failed"
    junk = ckeys.ssh_signer(path, namespace=ts.NAMESPACE)[1](b"not a terminal message")   # verifies, does not parse
    ev = _event(out, b"not a terminal message")
    ev["signed"]["sig"] = junk
    assert ts.answer_class(ev, ROW, ctx).state == "failed"
    ev = {"via": "terminal", "signed": {"anything": 1}}
    assert ts.answer_class(ev, ROW, ctx).state == "failed"       # a label never rescues a bad signed block


def test_class_labels_confer_nothing():
    assert ts.answer_class({"via": "terminal", "unsigned": {"reason": "cancelled"}}).state == "unsigned-terminal"
    assert ts.answer_class({"via": "terminal"}).detail == ""
    assert ts.answer_class({"via": "cli", "note": "x"}).state == "agent-resolved"
    legacy = ts.answer_class({"ts": "2026-10-01T00:00:00Z", "note": "x"})
    assert legacy.state == "legacy" and "before signing existed" in legacy.detail


def test_class_countersign_makes_late(signed_world):
    path, message, out, signers = signed_world
    base = {"ts": "2026-11-02T18:40:00Z", "answer": "(a) Add a Levain 1.0 row", "via": "terminal",
            "unsigned": {"reason": "cancelled"}}
    cs = {"of_ts": base["ts"], "signed": {"format": ts.FORMAT, "message": message.decode(), "sig": out.signature,
                                           "entry": 1}}
    got = ts.answer_class(base, ROW, _ctx(signers, countersigns=[cs]))
    assert got.state == "signed-terminal" and got.late
    other = ts.answer_class(base, ROW, _ctx(signers, countersigns=[{**cs, "of_ts": "elsewhere"}]))
    assert other.state == "unsigned-terminal"
    bad_row = ts.answer_class(base, {**ROW, "id": "np-x"}, _ctx(signers, countersigns=[cs]))
    assert bad_row.state == "unsigned-terminal"
