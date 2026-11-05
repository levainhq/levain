"""The keyguard: a pytest run cannot reach the operator's real terminal key. Nothing here signs: every case asserts a
refusal, a path or a "no key", and uses a fake signer that fails the test if it is ever called. Cases that need a
"real" directory to exist point ``pwd_key_dir`` at one under ``tmp_path``, so they run on any host, CI included."""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

from levain.cockpit import keys as K
from levain.consent import terminal_sign as ts


def _never(*a, **kw):
    raise AssertionError("a signer was reached")


def _never_factory(path, timeout=None):
    raise AssertionError("a signer factory was reached")


@pytest.fixture
def fake_real(monkeypatch, tmp_path):
    """The guard's notion of the real key directory, pointed at an existing directory under ``tmp_path``."""
    real = tmp_path / "Home" / ".levain" / "keys"
    real.mkdir(parents=True)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    return real


def test_fixture_default_call_sees_no_key(tmp_path):
    assert ts._real_key_dir() == tmp_path / "nohome" / ".levain" / "keys"
    assert not ts._real_key_dir().exists()
    assert ts.find_key() is None
    assert ts.sign(b"x", signer_factory=_never_factory).reason == "no-key"
    assert ts.status().state == "not enrolled"


def test_fixture_survives_monkeypatch_undo(monkeypatch, tmp_path):
    # codex MED (L3 r1): the redirect sat on the test's own monkeypatch, so a test's undo() dropped it
    monkeypatch.undo()
    assert ts._real_key_dir() == tmp_path / "nohome" / ".levain" / "keys"
    assert ts.find_key() is None


def test_pwd_key_dir_ignores_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "x"))
    real = K.pwd_key_dir()
    assert real != tmp_path / ".levain" / "keys" and real.parts[-2:] == (".levain", "keys")


@pytest.fixture
def fixture_bypassed(monkeypatch):
    monkeypatch.setattr(ts, "_real_key_dir", K.pwd_key_dir)


def test_guard_refuses_real_dir_when_fixture_bypassed(fixture_bypassed):
    real = K.pwd_key_dir()
    assert ts._real_key_dir() == real
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.find_key()
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.find_key(real)
    assert ts.status().state == "warning"


def test_guard_covers_sign_factories_create_and_ssh_signer(fixture_bypassed):
    key_path = K.pwd_key_dir() / ts.KEY_LABEL
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.default_signer_factory(key_path)
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.default_desk_signer_factory(key_path)
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        K.ssh_signer(key_path)
    out = ts.sign(b"x", key=ts.TerminalKey(key_path, "ssh-ed25519 AAAA"), signer_factory=_never_factory)
    assert out.state == "unsigned" and "real operator key directory" in out.detail
    rep = ts.create(creator=_never, in_agent_session=lambda: False)
    assert not rep.created and "real operator key directory" in rep.text


def test_a_handed_in_real_key_is_refused_even_with_a_fake_factory():
    # deepseek MED (L3 r1), decided: a TerminalKey naming the real key is refused before ANY factory, a fake one
    # included. The fake would not reach Touch ID, but the path is the real key's, and a test that wants sign()'s
    # plumbing passes a tmp_path key. This holds with the autouse fixture ON (no bypass): the handed-in key never
    # goes through _real_key_dir.
    called = []

    def fake_factory(path, timeout=None):
        called.append(path)
        return "SHA256:fake", lambda m: "sig"

    out = ts.sign(b"x", key=ts.TerminalKey(K.pwd_key_dir() / ts.KEY_LABEL, "ssh-ed25519 AAAA"),
                  signer_factory=fake_factory)
    assert out.state == "unsigned" and out.reason == "signer-error" and "real operator key directory" in out.detail
    assert called == []
    # the same fake factory with a tmp key signs: the refusal is the path, not the factory
    tmp_key = ts.TerminalKey(Path(os.environ["HOME"]) / "k" / ts.KEY_LABEL, "ssh-ed25519 AAAA")
    assert ts.sign(b"x", key=tmp_key, signer_factory=fake_factory).state == "signed"


def test_custody_default_key_dir_is_neutralised_by_the_fixtures(monkeypatch):
    # deepseek MED (L3 r1), decided: custody's default (``$LEVAIN_HOME/keys``, else ``~/.levain/keys``) is already
    # per-test under conftest's _isolate_levain_home and the session/per-test HOME, so _key_dir(None) lands in a
    # temp directory and is not refused. Without $LEVAIN_HOME and after undo() it falls back to the SESSION scratch
    # home, never the operator's.
    from levain.cockpit import custody
    real = K.pwd_key_dir()
    d = custody._key_dir(None)
    assert d == custody.default_key_dir().absolute() and d.is_dir()
    assert not str(d).casefold().startswith(str(real).casefold())
    monkeypatch.undo()
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    d2 = custody._key_dir(None)
    assert not str(d2).casefold().startswith(str(real).casefold()) and d2.is_dir()


def test_guard_covers_custody_key_dir(fixture_bypassed):
    from levain.cockpit import custody
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        custody._key_dir(K.pwd_key_dir())


def test_guard_resolves_links(fake_real, tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(fake_real.parent)
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(link / "keys" / ts.KEY_LABEL)
    other = tmp_path / "other"
    other.mkdir()
    ok = tmp_path / "alias2"
    ok.symlink_to(other)
    K.refuse_real_key_in_tests(ok / "keys" / ts.KEY_LABEL)          # a link to a temp dir is allowed


def test_guard_ignores_case(fake_real, tmp_path):
    # codex HIGH (L3 r1): macOS is case-insensitive by default, and resolve() keeps the spelling it is given
    variant = Path(str(fake_real).replace("Home", "hOME").replace(".levain/keys", ".LEVAIN/KEYS"))
    assert variant != fake_real
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(variant / ts.KEY_LABEL)
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(variant)


def test_guard_ignores_unicode_normalisation(monkeypatch, tmp_path):
    real = tmp_path / unicodedata.normalize("NFC", "café") / ".levain" / "keys"
    real.mkdir(parents=True)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    nfd = tmp_path / unicodedata.normalize("NFD", "café") / ".levain" / "keys" / ts.KEY_LABEL
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(nfd)


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/System/Volumes/Data").is_dir(),
                    reason="needs the macOS data-volume firmlink")
def test_guard_compares_identity_not_spelling(fake_real):
    # a second spelling of the same directory that neither resolve() nor case folding maps: the data-volume firmlink
    alias = Path("/System/Volumes/Data" + str(fake_real.resolve()))
    assert alias.is_dir() and os.path.samefile(alias, fake_real)
    assert alias.resolve() != fake_real.resolve()
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(alias / ts.KEY_LABEL)


def test_guard_fails_closed_when_the_real_dir_cannot_be_named(monkeypatch, tmp_path):
    # complement + codex + deepseek (L3 r1): a pwd lookup failure returned silently
    def no_entry():
        raise KeyError("getpwuid(): uid not found")
    monkeypatch.setattr(K, "pwd_key_dir", no_entry)
    with pytest.raises(K.RealKeyReached, match="cannot tell"):
        K.refuse_real_key_in_tests(tmp_path / "keys")


@pytest.mark.parametrize("exc", [OSError(13, "Permission denied"), RuntimeError("Symlink loop")])
def test_guard_fails_closed_when_a_path_cannot_be_resolved(monkeypatch, tmp_path, exc):
    def broken(p):
        raise exc
    monkeypatch.setattr(K, "_resolved", broken)
    with pytest.raises(K.RealKeyReached, match="cannot tell"):
        K.refuse_real_key_in_tests(tmp_path / "keys")


def test_guard_returns_the_path_it_judged(tmp_path):
    # codex MED (L3 r1): ssh_signer used the original, mutable spelling after the check; the guard now hands back the
    # resolved path it judged, and callers use that
    link = tmp_path / "ln"
    (tmp_path / "t").mkdir()
    link.symlink_to(tmp_path / "t")
    got = K.refuse_real_key_in_tests(link / "k")
    assert got == (tmp_path / "t" / "k").resolve()


def test_ssh_signer_uses_the_judged_path(monkeypatch, tmp_path):
    (tmp_path / "t").mkdir()
    link = tmp_path / "ln"
    link.symlink_to(tmp_path / "t")
    seen = []

    def spy(p):
        seen.append(Path(p))
        raise K.KeyError_("stop here")
    monkeypatch.setattr(K, "signing_public_key", spy)
    with pytest.raises(K.KeyError_, match="stop here"):
        K.ssh_signer(link / "k")
    assert seen == [(tmp_path / "t" / "k").resolve()]


def test_temp_key_dir_is_untouched(tmp_path):
    K.refuse_real_key_in_tests(tmp_path / "keys")
    assert K.refuse_real_key_in_tests(None) is None
    assert ts.find_key(tmp_path / "nowhere") is None


def test_refusal_is_a_key_error_and_an_assertion_error(fixture_bypassed):
    with pytest.raises(AssertionError, match="real terminal key"):
        ts.default_signer_factory(K.pwd_key_dir() / ts.KEY_LABEL, 1.0)
