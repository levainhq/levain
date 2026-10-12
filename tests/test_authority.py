"""The authority log (trust-tiering DESIGN §3, §4.3, §3.8): verified replay, the recompute classifier,
append, the per-process cache and the anchor.

Keys are real ssh-keygen keys made in a temp directory. A laptop-presence key must be an ``sk-`` FIDO key,
which cannot be generated without a security key attached, so most tests widen the class floor to admit
software ed25519 keys (``_widen_laptop_floor``); the floor itself is tested on its own with the real set,
on a public-key line constructed with an ``sk-ssh-ed25519`` blob (no private half exists for it).

Every replay refusal is shown on a DIRECTLY INSERTED, validly signed link, so the rule is proven to live
in the replay and not only in ``prepare``/``append``.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import struct
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import (AnchorError, AuthorityError, HeadBelowCache, ReplayCache, ReplayError, StaleHead)

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _widen_laptop_floor(monkeypatch, request):
    """TEST ONLY: admit software ed25519 keys as laptop-presence (see the module docstring)."""
    if "real_floor" not in request.node.name:
        monkeypatch.setattr(auth, "_LAPTOP_KEY_TYPES", auth._LAPTOP_KEY_TYPES | {"ssh-ed25519"})


class Key:
    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.public_key = " ".join((d / f"{name}.pub").read_text().split()[:2])
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.path)], check=True,
                             capture_output=True, text=True).stdout
        self.fp = out.split()[1]

    def sign(self, message: bytes, namespace: str = auth.NAMESPACE) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, check=True, timeout=30).stdout.decode()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("authority-keys")
    return {n: Key(d, n) for n in ("laptop", "laptop2", "phone", "presence", "other")}


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "cockpit.db", isolation_level=None)
    auth.create_schema(c)
    yield c
    c.close()


def _enrol(k: Key, cls: str) -> dict:
    return {"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": cls}


def link(conn, op, signer: Key, *, cache=None, now=NOW, anchor=None):
    return auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=signer.sign, clock=lambda: now,
                                cache=cache or ReplayCache(), anchor=anchor)


@pytest.fixture
def base(conn, keys):
    """laptop (genesis) + phone + presence enrolled."""
    link(conn, _enrol(keys["laptop"], "laptop-presence"), keys["laptop"])
    link(conn, _enrol(keys["phone"], "display-phone"), keys["laptop"])
    link(conn, _enrol(keys["presence"], "presence"), keys["laptop"])
    return conn


def head(conn) -> auth.AuthoritySnapshot:
    return auth.snapshot(conn, cache=ReplayCache())


def insert(conn, op: dict, signer: Key, *, seq=None, prev=None, store_id=None, at=None, raw_record=None,
           namespace=auth.NAMESPACE, signer_fp=None) -> None:
    """A validly signed link inserted directly, past every append-side check."""
    rows = conn.execute("SELECT seq, hash, record FROM authority_log ORDER BY seq").fetchall()
    last_seq, last_hash = (rows[-1][0], rows[-1][1]) if rows else (0, "")
    sid = store_id or (json.loads(rows[0][2])["store_id"] if rows else "store-0001")
    seq = last_seq + 1 if seq is None else seq
    prev = last_hash if prev is None else prev
    rec = {**op, "format": auth.FORMAT, "store_id": sid, "seq": seq, "at": at or "2026-10-12T09:00:00.000000Z"}
    record = raw_record if raw_record is not None else auth._canonical(rec)
    sig = signer.sign(auth._message(prev, record), namespace)
    conn.execute("INSERT INTO authority_log VALUES (?, ?, ?, ?, ?, ?)",
                 (seq, prev, record, signer_fp or signer.fp, sig, auth._link_hash(prev, record)))


def refused(conn, match: str) -> None:
    with pytest.raises(ReplayError, match=match):
        head(conn)


def _stamp(dt: datetime) -> str:
    return auth._stamp(dt)


def _grant(gid="g1", *, expires=NOW + timedelta(days=10), refspecs=("refs/heads/entity/*",), verbs=("push",)):
    return {"id": gid, "entity": "e1", "remote_url": "git@github.com:x/y.git", "deploy_key_sha256": "ab" * 32,
            "refspecs": sorted(refspecs), "verbs": sorted(verbs), "expires_at": _stamp(expires)}


@pytest.fixture
def granted(base, keys):
    link(base, {"op": "create_grant", "grant": _grant()}, keys["laptop"])
    return base


# --- the happy path ------------------------------------------------------------------------------


def test_replay_builds_keys_and_policy(granted, keys):
    snap = head(granted)
    assert snap.seq == 4 and len(snap.head_hash) == 64
    assert {k.key_class for k in snap.active_keys()} == {"laptop-presence", "display-phone", "presence"}
    assert snap.policy.grants["g1"]["entity"] == "e1"
    assert snap.hash_at(4) == snap.head_hash


def test_genesis_policy_is_the_pinned_constant(monkeypatch):
    """F6: replay never reads live verbs.Policy(); the genesis is a constant of the format."""
    assert dict(auth.genesis_policy().raise_to) == dict(auth._GENESIS_RAISE_TO) == {}
    assert not auth.genesis_policy().grants and not auth.genesis_policy().timeouts
    import levain.cockpit.verbs as verbs
    monkeypatch.setattr(verbs, "Policy", lambda: (_ for _ in ()).throw(AssertionError("read live policy")))
    assert auth.genesis_policy() == auth.AuthorityPolicy()


def test_raise_by_phone_applies(base, keys):
    snap = link(base, {"op": "raise", "verb": "spore_note", "tier": "T3"}, keys["phone"])
    assert snap.policy.apply("spore_note", "C1") == "T3"
    assert snap.policy.apply("other", "C1") == "C1"


# --- who may sign (§3.2) and the genesis -----------------------------------------------------------


def test_genesis_must_enrol_a_laptop_key_signed_by_itself(conn, keys):
    insert(conn, _enrol(keys["phone"], "display-phone"), keys["phone"])
    refused(conn, "first link must enrol a laptop-presence key")


def test_genesis_signed_by_another_key_refused(conn, keys):
    insert(conn, _enrol(keys["laptop"], "laptop-presence"), keys["other"], signer_fp=keys["laptop"].fp)
    refused(conn, "signature does not verify")


def test_enrol_signed_by_phone_refused(base, keys):
    insert(base, _enrol(keys["other"], "presence"), keys["phone"])
    refused(base, "display-phone key cannot sign this enrol")


def test_signer_not_enrolled_refused(base, keys):
    insert(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["other"])
    refused(base, "is not enrolled")


def test_real_floor_refuses_a_software_laptop_key(conn, keys):
    insert(conn, _enrol(keys["laptop"], "laptop-presence"), keys["laptop"])
    refused(conn, "laptop-presence key must be one of sk-ecdsa")


def _sk_public_key() -> str:
    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    blob = s(b"sk-ssh-ed25519@openssh.com") + s(os.urandom(32)) + s(b"ssh:")
    return "sk-ssh-ed25519@openssh.com " + base64.b64encode(blob).decode()


def test_laptop_floor_admits_an_sk_key(base, keys):
    """The floor accepts an sk key line (the real set stays in the widened one); its fingerprint is
    recomputed by ssh-keygen like any other."""
    pk = _sk_public_key()
    from levain.cockpit.keys import fingerprint_of
    fp = fingerprint_of(pk)
    snap = link(base, {"op": "enrol", "fingerprint": fp, "public_key": pk, "class": "laptop-presence"},
                keys["laptop"])
    assert snap.key(fp).key_class == "laptop-presence"
    assert "no-touch-required" not in auth._signers_line("p", auth.NAMESPACE, pk)


# --- the signature check (F1) ---------------------------------------------------------------------


def _ssh_string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _armored_sshsig(public_key: str, sig_type: str, *, flags: int | None) -> str:
    """An SSHSIG (PROTOCOL.sshsig) whose signature field is ``sig_type`` + junk signature bytes, plus,
    for an sk type, the PROTOCOL.u2f flags byte and counter. No FIDO key exists headless, so the
    signature cannot verify: the tests stand in a verifier that says Good, to show the presence check
    refuses on its own."""
    sig = _ssh_string(sig_type.encode()) + _ssh_string(os.urandom(64))
    if flags is not None:
        sig += bytes([flags]) + struct.pack(">I", 7)
    blob = (b"SSHSIG" + struct.pack(">I", 1) + _ssh_string(base64.b64decode(public_key.split()[1]))
            + _ssh_string(auth.NAMESPACE.encode()) + _ssh_string(b"") + _ssh_string(b"sha512")
            + _ssh_string(sig))
    b64 = base64.b64encode(blob).decode()
    body = "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
    return f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n"


FAKE_FP = "SHA256:" + "A" * 43


def _verifier_says_good(monkeypatch) -> None:
    """Stand in an ssh-keygen that accepts anything, reporting FAKE_FP."""
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, b'Good "levain-authority" signature for authority-signer '
                                                    b"with ED25519-SK key " + FAKE_FP.encode() + b"\n", b"")
    monkeypatch.setattr(auth.subprocess, "run", run)


def test_sk_signature_without_user_presence_refused_whatever_the_verifier_says(monkeypatch):
    pk = _sk_public_key()
    _verifier_says_good(monkeypatch)
    no_touch = _armored_sshsig(pk, "sk-ssh-ed25519@openssh.com", flags=0x04)    # UV set, UP clear
    assert not auth.verify_link_sig(b"m", no_touch, public_key=pk, fingerprint=FAKE_FP)
    touched = _armored_sshsig(pk, "sk-ssh-ed25519@openssh.com", flags=0x01)
    assert auth.verify_link_sig(b"m", touched, public_key=pk, fingerprint=FAKE_FP)   # the control


def test_sk_key_with_a_non_sk_signature_refused(monkeypatch):
    pk = _sk_public_key()
    _verifier_says_good(monkeypatch)
    assert not auth.verify_link_sig(b"m", _armored_sshsig(pk, "ssh-ed25519", flags=None), public_key=pk,
                                    fingerprint=FAKE_FP)


def test_malformed_sshsig_refused_before_the_verifier(monkeypatch):
    pk = _sk_public_key()
    _verifier_says_good(monkeypatch)
    good = _armored_sshsig(pk, "sk-ssh-ed25519@openssh.com", flags=0x01)
    trailing = _armored_sshsig(pk, "sk-ssh-ed25519@openssh.com", flags=0x01).replace(
        "-----END", "AAAA\n-----END")
    for bad in (good.replace("SSH SIGNATURE", "SSH SIG"), trailing, good[:60] + "!" + good[61:], "é" + good):
        assert not auth.verify_link_sig(b"m", bad, public_key=pk, fingerprint=FAKE_FP)


def test_signers_and_signature_reach_ssh_keygen_through_pipes_never_disk(monkeypatch, keys):
    """A real verify, with every temp-file maker poisoned: the signers line and the signature travel as
    /dev/fd/N, so a same-uid process has no file to swap."""
    def poisoned(*a, **k):
        raise AssertionError("an authority signature check touched a temp file")
    monkeypatch.setattr(auth.tempfile, "TemporaryDirectory", poisoned)
    monkeypatch.setattr(auth.tempfile, "mkstemp", poisoned)
    monkeypatch.setattr(auth.tempfile, "NamedTemporaryFile", poisoned)
    real, argvs = auth.subprocess.run, []
    monkeypatch.setattr(auth.subprocess, "run", lambda argv, **kw: ("verify" in argv and argvs.append(argv)) or real(argv, **kw))
    k = keys["laptop"]
    assert auth.verify_link_sig(b"msg", k.sign(b"msg"), public_key=k.public_key, fingerprint=k.fp)
    assert not auth.verify_link_sig(b"other", k.sign(b"msg"), public_key=k.public_key, fingerprint=k.fp)
    assert len(argvs) == 2
    for argv in argvs:
        assert argv[argv.index("-f") + 1].startswith("/dev/fd/")
        assert argv[argv.index("-s") + 1].startswith("/dev/fd/")


# --- §3.3a enrol ---------------------------------------------------------------------------------


def test_enrol_non_normal_public_key_refused(base, keys):
    insert(base, {**_enrol(keys["other"], "presence"), "public_key": keys["other"].public_key + " comment"},
           keys["laptop"])
    refused(base, "not in its normal form")


def test_enrol_fingerprint_not_recomputed_refused(base, keys):
    insert(base, {**_enrol(keys["other"], "presence"), "fingerprint": keys["laptop2"].fp}, keys["laptop"])
    refused(base, "is not the key's")


def test_enrol_already_enrolled_refused(base, keys):
    insert(base, _enrol(keys["phone"], "display-phone"), keys["laptop"])
    refused(base, "already enrolled")


def test_enrol_class_change_refused(base, keys):
    insert(base, _enrol(keys["phone"], "presence"), keys["laptop"])
    refused(base, "a key keeps its class")


def test_enrol_after_revoke_refused(base, keys):
    link(base, {"op": "revoke_key", "fingerprint": keys["phone"].fp}, keys["laptop"])
    insert(base, _enrol(keys["phone"], "display-phone"), keys["laptop"])
    refused(base, "a revoked key stays revoked")


# --- §3.3a revoke_key ----------------------------------------------------------------------------


def test_revoke_unknown_refused(base, keys):
    insert(base, {"op": "revoke_key", "fingerprint": keys["other"].fp}, keys["laptop"])
    refused(base, "no enrolled key")


def test_revoke_twice_refused(base, keys):
    link(base, {"op": "revoke_key", "fingerprint": keys["presence"].fp}, keys["laptop"])
    insert(base, {"op": "revoke_key", "fingerprint": keys["presence"].fp}, keys["laptop"])
    refused(base, "already revoked")


def test_revoke_last_laptop_key_needs_compromised_since(base, keys):
    insert(base, {"op": "revoke_key", "fingerprint": keys["laptop"].fp}, keys["laptop"])
    refused(base, "last unrevoked laptop-presence key")


def test_revoke_last_laptop_key_with_compromised_since(base, keys):
    snap = link(base, {"op": "revoke_key", "fingerprint": keys["laptop"].fp, "compromised_since": 2},
                keys["laptop"])
    assert snap.key(keys["laptop"].fp).compromised_since == 2 and not snap.key(keys["laptop"].fp).active


def test_revoke_compromised_since_out_of_range_refused(base, keys):
    insert(base, {"op": "revoke_key", "fingerprint": keys["phone"].fp, "compromised_since": 9}, keys["laptop"])
    refused(base, "compromised_since must be a seq")


def test_revoke_by_lower_class_refused(base, keys):
    insert(base, {"op": "revoke_key", "fingerprint": keys["phone"].fp}, keys["presence"])
    refused(base, "presence key cannot sign this revoke_key")


def test_revoked_key_cannot_sign_after_its_revoke_but_its_earlier_links_stand(base, keys):
    link(base, {"op": "raise", "verb": "v1", "tier": "T3"}, keys["phone"])           # seq 4, by phone
    link(base, {"op": "revoke_key", "fingerprint": keys["phone"].fp}, keys["laptop"])  # seq 5
    snap = head(base)
    assert snap.policy.raise_to["v1"] == "T3"        # the phone's earlier link still verifies
    insert(base, {"op": "raise", "verb": "v2", "tier": "T3"}, keys["phone"])
    refused(base, "was revoked at seq 5")


# --- §3.3a / §4.3 policy ops -----------------------------------------------------------------------


def test_raise_by_presence_key_refused(base, keys):
    insert(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["presence"])
    refused(base, "presence key cannot sign this raise")


def test_raise_that_lowers_is_refused(base, keys):
    link(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["laptop"])
    insert(base, {"op": "raise", "verb": "v", "tier": "T2"}, keys["laptop"])
    refused(base, "a raise link computes to a loosen change")


def test_noop_refused(base, keys):
    link(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])
    insert(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])
    refused(base, "changes nothing")


def test_raise_to_the_lowest_tier_on_a_verb_with_no_floor_is_a_noop(base, keys):
    """F11: no floor already means the lowest tier; the raise changes nothing, so it is refused."""
    assert auth.classify(head(base).policy, auth.AuthorityPolicy.from_dict(
        {"raise_to": {"fresh": "C1"}, "timeouts": {}, "grants": {}})) == auth.NOOP
    insert(base, {"op": "raise", "verb": "fresh", "tier": "C1"}, keys["phone"])
    refused(base, "changes nothing")


def test_loosen_to_the_same_policy_is_a_noop(base, keys):
    link(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])
    insert(base, {"op": "loosen", "policy": head(base).policy.as_dict()}, keys["laptop"])
    refused(base, "changes nothing")


def test_timeouts_to_the_same_values_is_a_noop(base, keys):
    link(base, {"op": "timeouts", "timeouts": {"k": {"on_timeout": "deny"}}}, keys["laptop"])
    insert(base, {"op": "timeouts", "timeouts": {"k": {"on_timeout": "deny"}}}, keys["laptop"])
    refused(base, "changes nothing")


def test_create_grant_cannot_be_a_noop(granted, keys):
    """The identical grant again never computes to NOOP: an existing id is refused outright."""
    insert(granted, {"op": "create_grant", "grant": _grant()}, keys["laptop"])
    refused(granted, "grant 'g1' already exists")


def test_loosen_by_phone_refused(base, keys):
    link(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])
    insert(base, {"op": "loosen", "policy": {"raise_to": {}, "timeouts": {}, "grants": {}}}, keys["phone"])
    refused(base, "display-phone key cannot sign this loosen")


def test_loosen_by_laptop_applies(base, keys):
    link(base, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])
    snap = link(base, {"op": "loosen", "policy": {"raise_to": {}, "timeouts": {}, "grants": {}}}, keys["laptop"])
    assert dict(snap.policy.raise_to) == {}


def test_loosen_that_only_tightens_refused(base, keys):
    insert(base, {"op": "loosen", "policy": {"raise_to": {"v": "T3"}, "timeouts": {}, "grants": {}}},
           keys["laptop"])
    refused(base, "a loosen link computes to a tighten change")


def test_create_grant_by_phone_refused(base, keys):
    insert(base, {"op": "create_grant", "grant": _grant()}, keys["phone"])
    refused(base, "display-phone key cannot sign this create_grant")


def test_grant_with_force_refspec_refused(base, keys):
    insert(base, {"op": "create_grant", "grant": _grant(refspecs=("+refs/heads/x",))}, keys["laptop"])
    refused(base, "refspecs is not a non-empty list")


def test_timeouts_change_is_a_loosen(base, keys):
    insert(base, {"op": "timeouts", "timeouts": {"harness_approve": {"on_timeout": "deny"}}}, keys["phone"])
    refused(base, "display-phone key cannot sign this timeouts")


def test_timeouts_by_laptop_applies(base, keys):
    snap = link(base, {"op": "timeouts", "timeouts": {"k": {"on_timeout": "deny"}}}, keys["laptop"])
    assert snap.policy.timeouts["k"]["on_timeout"] == "deny"


def test_revoke_grant_by_any_enrolled_key(granted, keys):
    snap = link(granted, {"op": "revoke_grant", "grant_id": "g1"}, keys["presence"])
    assert "g1" not in snap.policy.grants


def test_revoke_grant_unknown_refused(base, keys):
    insert(base, {"op": "revoke_grant", "grant_id": "nope"}, keys["presence"])
    refused(base, "no grant 'nope'")


# --- renew ---------------------------------------------------------------------------------------


def _renew(expires, signed=NOW, gid="g1") -> dict:
    return {"op": "renew", "grant_id": gid, "expires_at": _stamp(expires), "signed_at": _stamp(signed)}


def test_renew_by_phone_applies(granted, keys):
    snap = link(granted, _renew(NOW + timedelta(days=30)), keys["phone"])
    assert snap.policy.grants["g1"]["expires_at"] == _stamp(NOW + timedelta(days=30))


def test_renew_with_an_extra_field_refused(granted, keys):
    insert(granted, {**_renew(NOW + timedelta(days=20)), "refspecs": ["refs/heads/*"]}, keys["phone"])
    refused(granted, "a renew record carries exactly")


def test_renew_past_signed_at_plus_30_days_refused(granted, keys):
    insert(granted, _renew(NOW + timedelta(days=30, seconds=1)), keys["phone"])
    refused(granted, "at most 30 days past its signing time")


def test_renew_not_later_refused(granted, keys):
    insert(granted, _renew(NOW + timedelta(days=5)), keys["phone"])
    refused(granted, "must move the grant's expiry later")


def test_renew_by_presence_key_refused(granted, keys):
    insert(granted, _renew(NOW + timedelta(days=20)), keys["presence"])
    refused(granted, "presence key cannot sign this renew")


def test_renew_whose_result_differs_beyond_expiry_refused(granted, keys, monkeypatch):
    """The recompute is the backstop: if applying a renew changed anything but that grant's expiry, the
    classifier sees a loosen and the declared renew is refused."""
    real = auth._apply_policy_op

    def widening(policy, rec):
        new = real(policy, rec)
        if rec["op"] != "renew":
            return new
        data = new.as_dict()
        data["grants"]["g1"]["refspecs"] = sorted(data["grants"]["g1"]["refspecs"] + ["refs/heads/main"])
        return auth.AuthorityPolicy.from_dict(data)

    monkeypatch.setattr(auth, "_apply_policy_op", widening)
    insert(granted, _renew(NOW + timedelta(days=20)), keys["phone"])
    refused(granted, "a renew link computes to a loosen change")


def test_loosen_that_only_extends_an_expiry_refused(granted, keys):
    """An expiry-only change computes to RENEW, so it cannot ride a loosen past the 30-day bound."""
    policy = head(granted).policy.as_dict()
    policy["grants"]["g1"]["expires_at"] = _stamp(NOW + timedelta(days=300))
    insert(granted, {"op": "loosen", "policy": policy}, keys["laptop"])
    refused(granted, "a loosen link computes to a renew change")


def _prepare_then_append(conn, op, signer: Key, *, prepared_at, appended_at):
    cache = ReplayCache()
    ln = auth.prepare(conn, op, signer_fp=signer.fp, now=prepared_at, cache=cache)
    return auth.append(conn, ln, signer.sign(ln.message), now=appended_at, cache=cache, anchor=None)


def test_renew_append_checks(granted, keys):
    with pytest.raises(AuthorityError, match="within 10 minutes"):
        _prepare_then_append(granted, _renew(NOW + timedelta(days=20)), keys["phone"], prepared_at=NOW,
                             appended_at=NOW + timedelta(minutes=11))
    with pytest.raises(AuthorityError, match="30 days from now"):
        _prepare_then_append(granted, _renew(NOW + timedelta(days=30)), keys["phone"], prepared_at=NOW,
                             appended_at=NOW - timedelta(minutes=5))
    later = NOW + timedelta(days=11)
    with pytest.raises(AuthorityError, match="has expired"):
        link(granted, _renew(later + timedelta(days=5), signed=later), keys["phone"], now=later)


def test_renew_signed_at_must_equal_at(granted, keys):
    """F2: a replayed signed_at is not a free input to the 30-day rule; it is the link's own at."""
    insert(granted, _renew(NOW + timedelta(days=40), signed=NOW + timedelta(days=20)), keys["phone"])
    refused(granted, "signed_at must equal its at")


def test_a_link_earlier_than_its_predecessor_refused(granted, keys):
    insert(granted, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"],
           at=_stamp(NOW - timedelta(seconds=1)))
    refused(granted, "earlier than its predecessor")


def test_a_future_dated_link_wedges_later_appends_visibly(granted, keys):
    """F2: a renew inserted with a future at (and signed_at) replays, and every later append is refused
    by the ordering rule rather than extending anything silently. (The grant is still live at that at:
    a renew signed after the expiry is K1's refusal.)"""
    future = NOW + timedelta(days=5)
    insert(granted, _renew(future + timedelta(days=30), signed=future), keys["phone"], at=_stamp(future))
    assert head(granted).policy.grants["g1"]["expires_at"] == _stamp(future + timedelta(days=30))
    with pytest.raises(ReplayError, match="earlier than its predecessor"):
        link(granted, {"op": "raise", "verb": "v", "tier": "T3"}, keys["phone"])


def test_classify():
    p0 = auth.AuthorityPolicy.from_dict({"raise_to": {}, "timeouts": {}, "grants": {"g1": _grant()}})

    def with_grant(**kw):
        return auth.AuthorityPolicy.from_dict({"raise_to": {}, "timeouts": {}, "grants": {"g1": _grant(**kw)}})

    assert auth.classify(p0, p0) == auth.NOOP
    assert auth.classify(p0, with_grant(expires=NOW + timedelta(days=11))) == auth.RENEW
    assert auth.classify(p0, with_grant(expires=NOW + timedelta(days=9))) == auth.TIGHTEN
    assert auth.classify(p0, with_grant(verbs=("fetch", "push"))) == auth.LOOSEN
    assert auth.classify(p0, with_grant(refspecs=("refs/heads/a/*",))) == auth.LOOSEN
    assert auth.classify(p0, with_grant(refspecs=("refs/heads/entity/*",), expires=NOW + timedelta(days=11),
                                        verbs=("fetch", "push"))) == auth.LOOSEN
    wide = with_grant(refspecs=("refs/heads/a", "refs/heads/entity/*"))
    assert auth.classify(wide, p0) == auth.TIGHTEN
    empty = auth.AuthorityPolicy.from_dict({"raise_to": {}, "timeouts": {}, "grants": {}})
    assert auth.classify(p0, empty) == auth.GRANT_REVOKE
    assert auth.classify(empty, p0) == auth.LOOSEN


# --- the chain -----------------------------------------------------------------------------------


def test_truncated_log_refused(granted):
    granted.execute("DELETE FROM authority_log WHERE seq = 2")
    refused(granted, "expected seq 2, found 3")


def test_log_missing_its_genesis_refused(granted):
    granted.execute("DELETE FROM authority_log WHERE seq = 1")
    refused(granted, "expected seq 1, found 2")


def test_reordered_log_refused(granted):
    rows = granted.execute("SELECT seq, prev_hash, record, signer_fp, signature, hash FROM authority_log "
                           "WHERE seq IN (2, 3) ORDER BY seq").fetchall()
    granted.execute("DELETE FROM authority_log WHERE seq IN (2, 3)")
    granted.execute("INSERT INTO authority_log VALUES (2, ?, ?, ?, ?, ?)", rows[1][1:])
    granted.execute("INSERT INTO authority_log VALUES (3, ?, ?, ?, ?, ?)", rows[0][1:])
    with pytest.raises(ReplayError):
        head(granted)


def test_edited_record_refused(granted):
    rec = granted.execute("SELECT record FROM authority_log WHERE seq = 4").fetchone()[0]
    granted.execute("UPDATE authority_log SET record = ? WHERE seq = 4", (rec.replace('"e1"', '"e2"'),))
    refused(granted, "hash does not match")


def test_non_canonical_record_refused(base, keys):
    rec = {**_enrol(keys["other"], "presence"), "format": auth.FORMAT, "store_id": head(base).store_id,
           "seq": 4, "at": "2026-10-12T09:00:00.000000Z"}
    insert(base, {}, keys["laptop"], raw_record=json.dumps(rec, indent=1))
    refused(base, "not a canonical JSON object")


def test_other_store_id_refused(base, keys):
    insert(base, _enrol(keys["other"], "presence"), keys["laptop"], store_id="store-9999")
    refused(base, "names store 'store-9999'")


def test_signature_in_another_namespace_refused(base, keys):
    insert(base, _enrol(keys["other"], "presence"), keys["laptop"], namespace="levain-cockpit")
    refused(base, "signature does not verify")


def test_unknown_op_refused(base, keys):
    insert(base, {"op": "grant_everything"}, keys["laptop"])
    refused(base, "unknown op")


# --- append --------------------------------------------------------------------------------------


def test_append_refuses_a_moved_head(base, keys):
    cache = ReplayCache()
    stale = auth.prepare(base, {"op": "raise", "verb": "a", "tier": "T3"}, signer_fp=keys["phone"].fp,
                         now=NOW, cache=cache)
    link(base, {"op": "raise", "verb": "b", "tier": "T3"}, keys["phone"], cache=cache)
    with pytest.raises(StaleHead):
        auth.append(base, stale, keys["phone"].sign(stale.message), now=NOW, cache=cache, anchor=None)
    assert head(base).seq == 4


def test_append_refuses_a_bad_signature(base, keys):
    cache = ReplayCache()
    ln = auth.prepare(base, {"op": "raise", "verb": "a", "tier": "T3"}, signer_fp=keys["phone"].fp,
                      now=NOW, cache=cache)
    with pytest.raises(ReplayError, match="signature does not verify"):
        auth.append(base, ln, keys["other"].sign(ln.message), now=NOW, cache=cache, anchor=None)
    assert head(base).seq == 3


def test_prepare_refuses_before_any_signature(base, keys):
    with pytest.raises(ReplayError, match="cannot sign this create_grant"):
        auth.prepare(base, {"op": "create_grant", "grant": _grant()}, signer_fp=keys["phone"].fp, now=NOW,
                     cache=ReplayCache())


def test_empty_log_is_not_initialised(conn):
    with pytest.raises(auth.NotInitialised, match="ledger init"):
        auth.snapshot(conn, cache=ReplayCache())


# --- the cache -----------------------------------------------------------------------------------


def test_cache_verifies_only_the_suffix(base, keys, monkeypatch):
    cache = ReplayCache()
    auth.snapshot(base, cache=cache)
    link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], cache=ReplayCache())
    calls = []
    real = auth.verify_link_sig
    monkeypatch.setattr(auth, "verify_link_sig", lambda *a, **k: calls.append(1) or real(*a, **k))
    snap = auth.snapshot(base, cache=cache)
    assert snap.seq == 4 and len(calls) == 1
    assert cache.get(base, snap.store_id, snap.seq, snap.head_hash) is snap
    auth.snapshot(base, cache=cache)
    assert len(calls) == 1


def test_a_stale_reader_is_served_its_own_head(granted, keys, tmp_path):
    """F9: a reader whose transaction began before a concurrent append (same process, same cache) gets
    the state at its own head, from the cache, instead of HeadBelowCache."""
    granted.execute("PRAGMA journal_mode=WAL")                 # as the cockpit store runs: readers never block
    cache = ReplayCache()
    before = auth.snapshot(granted, cache=cache)
    reader = sqlite3.connect(tmp_path / "cockpit.db", isolation_level=None)
    try:
        reader.execute("BEGIN")
        assert auth.snapshot(reader, cache=cache).seq == 4                 # the read snapshot is fixed
        after = link(granted, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], cache=cache)
        assert after.seq == 5 and auth.snapshot(granted, cache=cache).seq == 5
        stale = auth.snapshot(reader, cache=cache)
        assert (stale.seq, stale.head_hash) == (before.seq, before.head_hash) and stale.policy == before.policy
        reader.execute("COMMIT")
        assert auth.snapshot(reader, cache=cache).seq == 5
    finally:
        reader.close()


def test_a_truncated_log_reads_as_its_prefix_and_the_anchor_refuses_it(granted, tmp_path):
    """F9's price: the cache no longer tells a truncation from a stale reader; the anchor does."""
    a = auth.anchor_path(tmp_path)
    cache = ReplayCache()
    auth.advance_anchor(a, auth.snapshot(granted, cache=cache))
    granted.execute("DELETE FROM authority_log WHERE seq = 4")
    snap = auth.snapshot(granted, cache=cache)
    assert snap.seq == 3
    with pytest.raises(AnchorError, match="behind its anchor"):
        auth.check_anchor(snap, auth.read_anchor(a))


def test_cache_refuses_a_rewrite_below_its_head(granted, keys):
    cache = ReplayCache()
    auth.snapshot(granted, cache=cache)
    granted.execute("UPDATE authority_log SET signature = ? WHERE seq = 2", (keys["laptop"].sign(b"x"),))
    with pytest.raises(HeadBelowCache, match="rewritten"):
        auth.snapshot(granted, cache=cache)


# --- the anchor ----------------------------------------------------------------------------------


def test_anchor_advances_after_commit_and_refuses_a_log_behind_it(base, keys, tmp_path):
    a = auth.anchor_path(tmp_path)
    snap = auth.advance_anchor(a, head(base))
    assert snap.seq == 3 and auth.read_anchor(a) == snap
    link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], anchor=a)
    assert auth.read_anchor(a).seq == 4
    base.execute("DELETE FROM authority_log WHERE seq = 4")      # a restore of an older store
    with pytest.raises(AnchorError, match="behind its anchor.*anchor reset"):
        auth.check_anchor(head(base), auth.read_anchor(a))
    with pytest.raises(AnchorError, match="behind its anchor"):
        link(base, {"op": "raise", "verb": "b", "tier": "T3"}, keys["phone"], anchor=a)


def test_anchor_refuses_another_hash_at_its_seq(base, keys, tmp_path):
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    base.execute("DELETE FROM authority_log WHERE seq = 3")
    link(base, _enrol(keys["other"], "presence"), keys["laptop"])
    with pytest.raises(AnchorError, match="behind its anchor"):
        auth.check_anchor(head(base), auth.read_anchor(a))


def test_unreadable_anchor_names_the_reset(tmp_path):
    a = auth.anchor_path(tmp_path)
    a.parent.mkdir()
    a.write_text("{not json")
    with pytest.raises(AnchorError, match="anchor reset"):
        auth.read_anchor(a)


def test_anchor_reset_verifies_against_the_logs_own_keys(base, keys, tmp_path):
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], anchor=a)
    old = auth.read_anchor(a).as_dict()
    base.execute("DELETE FROM authority_log WHERE seq = 4")
    snap = head(base)
    msg = auth.reset_message(snap.store_id, old, snap.seq, snap.head_hash)
    with pytest.raises(AnchorError, match="needs a laptop-presence key"):
        auth.reset_anchor(a, snap, signer_fp=keys["phone"].fp, signature=keys["phone"].sign(msg, auth.RESET_NAMESPACE))
    with pytest.raises(AnchorError, match="does not verify"):
        auth.reset_anchor(a, snap, signer_fp=keys["laptop"].fp, signature=keys["laptop"].sign(msg))
    with pytest.raises(AnchorError, match="not an unrevoked key"):
        auth.reset_anchor(a, snap, signer_fp=keys["other"].fp, signature=keys["other"].sign(msg, auth.RESET_NAMESPACE))
    new = auth.reset_anchor(a, snap, signer_fp=keys["laptop"].fp,
                            signature=keys["laptop"].sign(msg, auth.RESET_NAMESPACE))
    assert new.seq == 3 and auth.read_anchor(a) == new
    auth.check_anchor(head(base), auth.read_anchor(a))


def test_empty_log_beside_an_anchor_refuses_before_commit(base, keys, tmp_path):
    """F3: an emptied log next to an existing anchor must not commit a forked genesis."""
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    base.execute("DELETE FROM authority_log")
    with pytest.raises(AnchorError, match="empty"):
        link(base, _enrol(keys["laptop2"], "laptop-presence"), keys["laptop2"], anchor=a)
    assert base.execute("SELECT count(*) FROM authority_log").fetchone()[0] == 0
    assert auth.read_anchor(a).seq == 3


def test_a_stale_anchor_advance_is_a_noop_never_a_regress(base, keys, tmp_path):
    """F4: an advance from an older snapshot (a slower appender) leaves a later anchor where it is."""
    a = auth.anchor_path(tmp_path)
    older = head(base)
    newer = link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"])
    auth.advance_anchor(a, newer)
    assert auth.advance_anchor(a, older) == auth.read_anchor(a)
    assert auth.read_anchor(a).seq == 4


def test_concurrent_anchor_advances_serialise(base, keys, tmp_path, monkeypatch):
    """F4: two appenders advance at once; the one holding seq 4 reads the anchor (seq 3) and stalls.
    Unserialised, the seq-5 advance lands meanwhile and the stalled one then writes 4 over it."""
    import threading
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    s4 = link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"])
    s5 = link(base, {"op": "raise", "verb": "b", "tier": "T3"}, keys["phone"])
    real, landed = auth.read_anchor, threading.Event()

    def slow_read(path):
        got = real(path)
        if threading.current_thread().name == "slow":
            landed.wait(1.0)
        return got

    monkeypatch.setattr(auth, "read_anchor", slow_read)
    slow = threading.Thread(target=auth.advance_anchor, args=(a, s4), name="slow")
    slow.start()
    time.sleep(0.1)                       # let the slow advance read the anchor first
    auth.advance_anchor(a, s5)
    landed.set()
    slow.join(5)
    assert real(a).seq == 5


def test_a_failed_anchor_write_after_commit_is_a_warning_not_a_failed_append(base, keys, tmp_path, monkeypatch):
    """F5: the link is committed and cached; the anchor lagging is reported, not raised."""
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    cache = ReplayCache()

    def disk_full(path, payload):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(auth, "_write_anchor", disk_full)
    snap = link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], cache=cache, anchor=a)
    assert snap.seq == 4 and "committed at seq 4; the anchor is behind" in snap.anchor_warning
    assert cache.get(base, snap.store_id, 4, snap.head_hash) is not None
    assert auth.read_anchor(a).seq == 3
    assert head(base).anchor_warning is None


def test_append_needs_an_explicit_anchor_and_an_aware_now(base, keys):
    """F10 + F12: anchor is a required keyword (None = unanchored); a naive now is an AuthorityError."""
    cache = ReplayCache()
    ln = auth.prepare(base, {"op": "raise", "verb": "a", "tier": "T3"}, signer_fp=keys["phone"].fp,
                      now=NOW, cache=cache)
    with pytest.raises(TypeError, match="anchor"):
        auth.append(base, ln, keys["phone"].sign(ln.message), now=NOW, cache=cache)  # type: ignore[call-arg]
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(AuthorityError, match="aware"):
        auth.append(base, ln, keys["phone"].sign(ln.message), now=naive, cache=cache, anchor=None)
    with pytest.raises(AuthorityError, match="aware"):
        auth.prepare(base, {"op": "raise", "verb": "a", "tier": "T3"}, signer_fp=keys["phone"].fp,
                     now=naive, cache=cache)


# --- corrupt rows (F7) and non-finite numbers (F8) ------------------------------------------------


@pytest.mark.parametrize("column, sql_value", [
    ("record", "x'7b7d'"),                       # a BLOB, not text
    ("record", "CAST(x'ff7b' AS TEXT)"),         # text that is not UTF-8
    ("signature", "42"),
    ("signature", "'-----BEGIN SSH SIGNATURE-----é'"),
    ("hash", "1"),
    ("signer_fp", "3.5"),
    ("prev_hash", "x'00'"),
])
def test_corrupt_stored_cell_is_a_replay_error(granted, column, sql_value):
    granted.execute(f"UPDATE authority_log SET {column} = {sql_value} WHERE seq = 3")
    with pytest.raises(ReplayError) as exc:
        head(granted)
    assert exc.value.seq == 3


@pytest.mark.parametrize("field, value", [("op", ["enrol"]), ("class", ["presence"]),
                                          ("fingerprint", ["x"]), ("public_key", {"k": 1})])
def test_wrong_typed_record_field_is_a_replay_error(base, keys, field, value):
    op = _enrol(keys["other"], "presence")
    op[field] = value
    insert(base, op, keys["laptop"])
    with pytest.raises(ReplayError) as exc:
        head(base)
    assert exc.value.seq == 4


def test_cache_digest_survives_a_corrupt_cell(granted):
    cache = ReplayCache()
    auth.snapshot(granted, cache=cache)
    granted.execute("UPDATE authority_log SET signature = x'00ff' WHERE seq = 2")
    with pytest.raises(HeadBelowCache, match="rewritten"):
        auth.snapshot(granted, cache=cache)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timeouts_refused(base, keys, bad):
    """F8: _canonical keeps byte parity with team/entry.py (NaN serialises), so replay refuses it."""
    record_op = {"op": "timeouts", "timeouts": {"k": {"after_s": float(bad.replace("Infinity", "inf"))}}}
    assert bad in auth._canonical(record_op)
    insert(base, record_op, keys["laptop"])
    refused(base, "finite JSON")


def test_canonical_parity_with_team_entry():
    from levain.team.entry import canonical
    obj = {"b": [1, 2.5, float("nan")], "a": "é", "hash": "dropped-by-team"}
    assert auth._canonical({k: v for k, v in obj.items() if k != "hash"}) == canonical(obj)


# --- L3 r1 (K1-K6) -------------------------------------------------------------------------------


def test_replay_refuses_a_renew_of_a_lapsed_grant(granted, keys):
    """K1: a display-phone key cannot reactivate a lapsed grant through replay (signed_at is the link's
    own at, which the ordering rule bounds below, so replay can hold the rule)."""
    lapsed = NOW + timedelta(days=11)                       # the grant expired at NOW + 10 days
    insert(granted, _renew(lapsed + timedelta(days=9), signed=lapsed), keys["phone"], at=_stamp(lapsed))
    refused(granted, "has expired")


def test_replay_refuses_a_renew_signed_at_the_expiry_instant(granted, keys):
    expiry = NOW + timedelta(days=10)
    insert(granted, _renew(expiry + timedelta(days=9), signed=expiry), keys["phone"], at=_stamp(expiry))
    refused(granted, "has expired")


def test_sign_and_append_reads_the_clock_after_the_signature(granted, keys):
    """K2: a touch that takes seconds must not let a renew commit after the grant lapsed."""
    expiry = NOW + timedelta(days=10)
    t = [expiry - timedelta(seconds=5)]

    def slow_touch(message: bytes) -> str:
        t[0] = expiry + timedelta(seconds=5)                 # the hardware touch took ten seconds
        return keys["phone"].sign(message)

    with pytest.raises(AuthorityError, match="has expired"):
        auth.sign_and_append(granted, _renew(expiry + timedelta(days=20), signed=t[0]), signer_fp=keys["phone"].fp,
                             sign=slow_touch, clock=lambda: t[0], cache=ReplayCache(), anchor=None)
    assert head(granted).seq == 4


def _reset_setup(base, keys, tmp_path):
    a = auth.anchor_path(tmp_path)
    auth.advance_anchor(a, head(base))
    link(base, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"], anchor=a)
    old = auth.read_anchor(a).as_dict()
    base.execute("DELETE FROM authority_log WHERE seq = 4")
    snap = head(base)
    msg = auth.reset_message(snap.store_id, old, snap.seq, snap.head_hash)
    return a, snap, keys["laptop"].sign(msg, auth.RESET_NAMESPACE)


def test_anchor_reset_refused_while_a_cockpit_server_holds_the_store(base, keys, tmp_path):
    """K3 (a bound): the broker holds <data_root>/cockpit.lock exclusively while active; a reset needs
    it, so no live server can hold the abandoned store across a reset."""
    import fcntl
    from levain.cockpit.broker import LOCK_NAME
    a, snap, sig = _reset_setup(base, keys, tmp_path)
    fd = os.open(tmp_path / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)        # as Broker._attempt takes it
        with pytest.raises(AnchorError, match="cockpit server holds"):
            auth.reset_anchor(a, snap, signer_fp=keys["laptop"].fp, signature=sig)
        assert auth.read_anchor(a).seq == 4
    finally:
        os.close(fd)
    assert auth.reset_anchor(a, snap, signer_fp=keys["laptop"].fp, signature=sig).seq == 3


def test_a_verified_restore_replaces_the_cached_chain(granted, keys):
    """K4: a process that cached seq 4 sees a restored store whose seq 4 differs and verifies: it is
    served, and appended to, instead of HeadBelowCache forever."""
    cache = ReplayCache()
    assert auth.snapshot(granted, cache=cache).seq == 4
    granted.execute("DELETE FROM authority_log WHERE seq = 4")
    link(granted, {"op": "raise", "verb": "a", "tier": "T3"}, keys["phone"])      # another process's cache
    restored = auth.snapshot(granted, cache=cache)
    assert restored.seq == 4 and restored.policy.raise_to == {"a": "T3"} and not restored.policy.grants
    assert link(granted, {"op": "raise", "verb": "b", "tier": "T3"}, keys["phone"], cache=cache).seq == 5
    assert auth.snapshot(granted, cache=cache).seq == 5


FAR = datetime(9999, 12, 15, tzinfo=timezone.utc)


def test_renew_near_year_9999_replays_without_overflow(granted, keys):
    """K5: `signed + 30 days` overflowed past 9999-12-31; replay compares differences."""
    link(granted, {"op": "create_grant", "grant": _grant("g9", expires=FAR + timedelta(days=5))}, keys["laptop"])
    insert(granted, _renew(FAR + timedelta(days=16), signed=FAR, gid="g9"), keys["phone"], at=_stamp(FAR))
    assert head(granted).policy.grants["g9"]["expires_at"] == _stamp(FAR + timedelta(days=16))


def test_renew_near_year_9999_appends_without_overflow(granted, keys):
    """K5: append's `now + 30 days` overflowed the same way."""
    link(granted, {"op": "create_grant", "grant": _grant("g9", expires=FAR + timedelta(days=5))}, keys["laptop"])
    snap = link(granted, _renew(FAR + timedelta(days=16), signed=FAR, gid="g9"), keys["phone"], now=FAR)
    assert snap.policy.grants["g9"]["expires_at"] == _stamp(FAR + timedelta(days=16))


def test_deeply_nested_timeouts_is_a_replay_error(base, keys):
    """K6: a record json.loads accepts but the recursive validators cannot walk is a ReplayError."""
    depth = 3000
    rec = {"op": "timeouts", "timeouts": {"k": "DEEP"}, "format": auth.FORMAT, "store_id": head(base).store_id,
           "seq": 4, "at": "2026-10-12T09:00:00.000000Z"}
    raw = auth._canonical(rec).replace('"DEEP"', "[" * depth + "]" * depth)
    json.loads(raw)                                          # loadable: the failure is past the parse
    insert(base, {"op": "timeouts"}, keys["laptop"], raw_record=raw)
    with pytest.raises(ReplayError) as exc:
        head(base)
    assert exc.value.seq == 4


def test_deeply_nested_anchor_is_an_anchor_error(tmp_path):
    a = auth.anchor_path(tmp_path)
    a.parent.mkdir()
    a.write_text("[" * 100000 + "]" * 100000)
    with pytest.raises(AnchorError, match="unreadable"):
        auth.read_anchor(a)


def test_prepare_with_a_lone_surrogate_is_an_authority_error(base, keys):
    with pytest.raises(AuthorityError, match="no canonical UTF-8 form"):
        auth.prepare(base, {"op": "raise", "verb": "\ud800", "tier": "T3"}, signer_fp=keys["phone"].fp, now=NOW,
                     cache=ReplayCache())


def _unavailable(exc_info) -> None:
    assert type(exc_info.value).__name__ == "VerifierUnavailable"
    assert not isinstance(exc_info.value, ReplayError)        # retry, not tamper


def test_fingerprint_oserror_is_verifier_unavailable_not_a_replay_error(base, monkeypatch):
    # The OSError comes from below fingerprint_of (its ssh-keygen -l), the path a real caller takes:
    # fingerprint_of turns it into VerifierUnavailable itself.
    real = subprocess.run

    def run(argv, *a, **k):
        if "-l" in argv:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AuthorityError) as exc:
        head(base)
    _unavailable(exc)


def test_ssh_keygen_oserror_is_verifier_unavailable_not_a_replay_error(base, monkeypatch):
    real = subprocess.run

    def run(argv, *a, **k):
        if "-Y" in argv:
            raise PermissionError(13, "Permission denied", argv[0])
        return real(argv, *a, **k)

    monkeypatch.setattr(auth.subprocess, "run", run)
    with pytest.raises(AuthorityError) as exc:
        head(base)
    _unavailable(exc)


def test_a_pipe_thread_that_cannot_start_closes_its_fds(keys, monkeypatch):
    """K6: the second _pipe_reading's thread fails to start: its two fds are closed, and the failure is
    VerifierUnavailable."""
    k = keys["laptop"]
    sig = k.sign(b"m")
    pipes: list[tuple[int, int]] = []
    real_pipe, real_thread = os.pipe, auth.threading.Thread

    def recording_pipe():
        p = real_pipe()
        pipes.append(p)
        return p

    class SecondFails(real_thread):
        def start(self):
            if self.name == "levain-authority-pipe" and len(pipes) == 2:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(auth.os, "pipe", recording_pipe)
    monkeypatch.setattr(auth.threading, "Thread", SecondFails)
    with pytest.raises(AuthorityError) as exc:
        auth.verify_link_sig(b"m", sig, public_key=k.public_key, fingerprint=k.fp)
    monkeypatch.undo()
    _unavailable(exc)
    assert len(pipes) == 2                                   # nothing past the failed start ran
    for fd in pipes[1]:
        with pytest.raises(OSError):
            os.fstat(fd)
