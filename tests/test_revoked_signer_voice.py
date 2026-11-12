"""Levain board row 190, the voice-key reach: a voice key whose signing key was revoked stops at the next read.

A ``voice-key`` record is a ``create_grant`` link (or the replacing ``loosen``) signed by a laptop-presence key, and its
terms are fixed (no renew), so the born signer is the only signer. Revoking that key leaves the record in the policy (a
revoke can still name it, a set can still replace it) but ``custody.read_key`` reads the grants in force: it raises
``VoiceKeyUnavailable("not_set", ...)`` in words that say why, without asking the keychain, and the cockpit's presence
read and the live dictation recheck stop treating it as a key. The keychain here is the FAKE ``security`` of
``tests/test_voice_custody.py``; no test reaches a person's keychain.

The current-signer half of the stop rule (a key that last renewed the grant) has no voice-key form, because the kind
has no renew: it is covered by the helper tests in ``tests/test_grant_stop.py``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.chat_egress.key_setup import KeychainItems
from levain.chat_egress.keys import KeychainStore
from levain.cockpit import voice as cockpit_voice
from levain.cockpit.keys import fingerprint_of, revoke
from levain.voice import custody
from levain.voice import meter as vmeter
from levain.voice.custody import Secret, SecurityStdinWriter, VoiceKeyRefused, VoiceKeyUnavailable
from tests.chat_egress.keys_rig import Cockpit
from tests.test_voice_custody import ACCOUNT, OTHER, VALUE, FakeSecurity

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

STOPPED = "the openai voice key stopped: the key that signed it was revoked; set it again"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Rig:
    """A reported cockpit that enrols three laptop-presence keys (A is the genesis signer, B and C are enrolled by A), a
    fake keychain, and helpers that sign as a named key."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.ck = Cockpit(tmp_path)
        self.sec = FakeSecurity(tmp_path)
        self.keys: dict[str, tuple[Path, str]] = {"a": (self.ck.key, self.ck.fp)}
        for name in ("b", "c"):
            path = tmp_path / "keys" / name
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                           capture_output=True, timeout=30)
            pub = " ".join(path.with_name(name + ".pub").read_text().split()[:2])
            fp = fingerprint_of(pub)
            self.ck.link({"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                          "custody": "passphrase"})
            self.keys[name] = (path, fp)

    @property
    def root(self) -> Path:
        return self.ck.root

    def sign_as(self, name: str):
        path = self.keys[name][0]

        def sign(m: bytes) -> str:
            return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(path), "-n", auth.NAMESPACE], input=m,
                                  capture_output=True, check=True, timeout=30).stdout.decode()
        return sign

    def add(self, by: str, *, provider: str = "openai", uses=("stt", "tts"), value: str = VALUE, **kw):
        args = {"provider": provider, "uses": uses, "account": ACCOUNT, "value": Secret(value),
                "confirm": lambda preview: True, "writer": SecurityStdinWriter(self.sec.path),
                "items": KeychainItems(self.sec.path), "store": KeychainStore(self.sec.path)}
        return custody.add_key(self.root, signer_fp=self.keys[by][1], sign=self.sign_as(by), clock=_now,
                               **{**args, **kw})

    def revoke_key(self, target: str, by: str = "a") -> None:
        revoke(self.root, self.keys[target][1], signer_fp=self.keys[by][1], sign=self.sign_as(by), clock=_now)

    def read(self, provider: str = "openai", use: str = "tts") -> str:
        got = custody.read_key(provider, use, authority=self.ck.source(), store=KeychainStore(self.sec.path))
        return got.reveal()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_voice_key_whose_signer_is_active_reads(rig):
    """T14 control. Fails with `grant_stop_reason` returning a reason always."""
    rig.add("b")
    assert rig.read() == VALUE and rig.read(use="stt") == VALUE


def test_a_voice_key_whose_signer_was_revoked_stops_with_the_words_and_never_asks_the_keychain(rig):
    """T14. Fails when `read_key` reads the raw policy grants: the stopped record is read and the value comes back."""
    rec, _ = rig.add("b")
    assert rig.read() == VALUE
    rig.revoke_key("b")
    assert rec["id"] in rig.ck.state().policy.grants                    # the policy still holds the record
    calls = len(rig.sec.calls())
    with pytest.raises(VoiceKeyUnavailable) as e:
        rig.read()
    assert e.value.code == "not_set" and str(e.value) == STOPPED
    with pytest.raises(VoiceKeyUnavailable) as e2:
        rig.read(use="stt")
    assert e2.value.code == "not_set" and str(e2.value) == STOPPED
    assert len(rig.sec.calls()) == calls                                # the keychain was not touched
    assert rig.sec.items()                                              # and its item stays: a revoke withdraws authority


def test_a_key_that_signed_nothing_revoked_leaves_the_voice_key_reading(rig):
    """T5 shape: stop on any revoke (or a wrong signer index) would stop this."""
    rig.add("b")
    rig.revoke_key("c")
    assert rig.read() == VALUE


def test_a_revoke_stops_only_the_keys_that_signer_signed(rig):
    rig.add("a", provider="openai")
    rig.add("b", provider="deepgram", uses=("stt",), value=OTHER)
    rig.revoke_key("b")
    assert rig.read("openai") == VALUE
    with pytest.raises(VoiceKeyUnavailable) as e:
        rig.read("deepgram", "stt")
    assert e.value.code == "not_set" and str(e.value) == STOPPED.replace("openai", "deepgram")


def test_a_never_set_provider_keeps_its_own_words(rig):
    rig.add("b")
    with pytest.raises(VoiceKeyUnavailable) as e:
        rig.read("deepgram", "stt")
    assert e.value.code == "not_set" and str(e.value) == "no voice key is set for deepgram"


def test_setting_the_key_again_after_a_stop_works_and_a_plain_set_still_says_a_key_is_there(rig):
    """A write path reads the RAW policy: the stopped record is still the record to replace. Fails when `add_key`
    reads the grants in force (the replace finds nothing to replace)."""
    old, _ = rig.add("b")
    rig.revoke_key("b")
    with pytest.raises(VoiceKeyRefused, match="already has a voice key"):
        rig.add("a", value=OTHER, source="levain-voice-openai-again")
    new, _ = rig.add("a", value=OTHER, replace=True)
    assert new["id"] != old["id"] and old["id"] not in rig.ck.state().policy.grants
    assert rig.read() == OTHER


def test_the_presence_read_does_not_report_a_stopped_key_ready(rig):
    """The cockpit's voice readout and premium dictation setting. Fails when `_grants_for` returns the raw grants."""
    rig.add("b")
    meter = vmeter.Meter(rig.tmp / "voice-ledger.db")
    assert cockpit_voice.premium_stt_state(None, rig.root) == "on"
    row = next(r for r in cockpit_voice.serve_voice(rig.root, meter, last_read={})["providers"] if r["name"] == "openai")
    assert row["key"] == "set"
    rig.revoke_key("b")
    assert cockpit_voice.premium_stt_state(None, rig.root) == "off"
    body = cockpit_voice.serve_voice(rig.root, meter, last_read={})
    row = next(r for r in body["providers"] if r["name"] == "openai")
    assert (row["key"], row["record_id"], row["uses"]) == ("stopped", None, [])
    assert row["stopped_why"] == auth.STOPPED_BORN


def test_a_dictation_stream_ends_when_its_keys_signer_is_revoked(rig):
    """The live recheck: a session opened with a key must end at the next due check once that key stopped. Fails when
    `_stream_revoked` looks the bound record up in the raw policy grants."""
    rec, _ = rig.add("b", provider="deepgram", uses=("stt",))
    bound = (rec["id"], frozenset(rec["uses"]))
    assert cockpit_voice._stream_revoked("deepgram", rig.root, None, bound=bound) is None
    rig.revoke_key("b")
    got = cockpit_voice._stream_revoked("deepgram", rig.root, None, bound=bound)
    assert got is not None and got.status == 409 and json.loads(got.body)["revoked"] is True
