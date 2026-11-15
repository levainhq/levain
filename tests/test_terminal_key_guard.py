"""The keyguard: a pytest run cannot reach the operator's real terminal key. Nothing here signs: every case asserts a
refusal, a path or a "no key", and uses a fake signer that fails the test if it is ever called. The guard is an
ALLOWLIST (a key is reachable only inside the scratch root the conftest names), so a refusal is any path outside it:
the cases use the real account's own key directory, which they never create or touch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.cockpit import keys as K
from levain.consent import terminal_sign as ts


def _never(*a, **kw):
    raise AssertionError("a signer was reached")


def _never_factory(path, timeout=None):
    raise AssertionError("a signer factory was reached")


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
    with pytest.raises(K.KeyError_, match="scratch root"):
        ts.find_key()
    with pytest.raises(K.KeyError_, match="scratch root"):
        ts.find_key(real)
    assert ts.status().state == "warning"


def test_guard_covers_sign_factories_create_and_ssh_signer(fixture_bypassed):
    key_path = K.pwd_key_dir() / ts.KEY_LABEL
    with pytest.raises(K.KeyError_, match="scratch root"):
        ts.default_signer_factory(key_path)
    with pytest.raises(K.KeyError_, match="scratch root"):
        ts.default_desk_signer_factory(key_path)
    with pytest.raises(K.KeyError_, match="scratch root"):
        K.ssh_signer(key_path)
    out = ts.sign(b"x", key=ts.TerminalKey(key_path, "ssh-ed25519 AAAA"), signer_factory=_never_factory)
    assert out.state == "unsigned" and "scratch root" in out.detail
    rep = ts.create(creator=_never, in_agent_session=lambda: False)
    assert not rep.created and "scratch root" in rep.text


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
    assert out.state == "unsigned" and out.reason == "signer-error" and "scratch root" in out.detail
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
    with pytest.raises(K.KeyError_, match="scratch root"):
        custody._key_dir(K.pwd_key_dir())


def test_guard_resolves_links(tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(K.pwd_key_dir().parent)
    with pytest.raises(K.RealKeyReached):
        K.refuse_real_key_in_tests(link / "keys" / ts.KEY_LABEL)
    other = tmp_path / "other"
    other.mkdir()
    ok = tmp_path / "alias2"
    ok.symlink_to(other)
    K.refuse_real_key_in_tests(ok / "keys" / ts.KEY_LABEL)          # a link to a temp dir is allowed


@pytest.mark.parametrize("where", ["/", "/usr", "", ".ssh", ".levain/keys", ".LEVAIN/KEYS"])
def test_a_path_outside_the_scratch_root_is_refused_whatever_its_spelling(where):
    # the denylist needed a cut for each spelling of the real directory (case, Unicode form, a firmlink); an allowlist
    # has none: nothing outside the scratch root is a key a test may use. ``where`` is relative to the ACCOUNT's home
    # (the password database's, never $HOME, which a test run points at scratch), or absolute
    account = K.pwd_key_dir().parent.parent
    path = Path(where) if where.startswith("/") else account / where
    with pytest.raises(K.RealKeyReached, match="scratch root"):
        K.refuse_real_key_in_tests(path)


def test_guard_fails_closed_when_no_scratch_root_is_named(monkeypatch, tmp_path):
    from levain import home_guard
    monkeypatch.delenv(home_guard.TEST_SCRATCH_ROOT_ENV, raising=False)
    with pytest.raises(K.RealKeyReached, match="scratch root"):
        K.refuse_real_key_in_tests(tmp_path / "keys")


def test_guard_fails_closed_when_the_temp_root_holds_the_account_home(monkeypatch, tmp_path):
    from levain import home_guard
    monkeypatch.setattr(home_guard, "_account_home", lambda: tmp_path / "home")
    with pytest.raises(K.RealKeyReached, match="scratch root"):
        K.refuse_real_key_in_tests(tmp_path / "keys")


@pytest.mark.parametrize("exc", [OSError(13, "Permission denied"), RuntimeError("Symlink loop")])
def test_guard_fails_closed_when_a_path_cannot_be_resolved(monkeypatch, tmp_path, exc):
    def broken(self, *a, **k):
        raise exc
    monkeypatch.setattr(Path, "resolve", broken)
    with pytest.raises(K.RealKeyReached, match="cannot be resolved"):
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
    with pytest.raises(AssertionError, match="scratch root"):
        ts.default_signer_factory(K.pwd_key_dir() / ts.KEY_LABEL, 1.0)
