"""The keyguard: a pytest run cannot reach the operator's real terminal key. Nothing here signs: every case asserts a
refusal, a path or a "no key", and uses a fake signer that fails the test if it is ever called."""

from __future__ import annotations

import pytest

from levain.cockpit import keys as K
from levain.consent import terminal_sign as ts

REAL = K.pwd_key_dir()


def _never(*a, **kw):
    raise AssertionError("a signer was reached")


def _never_factory(path, timeout=None):
    raise AssertionError("a signer factory was reached")


def test_fixture_default_call_sees_no_key(tmp_path):
    assert ts._real_key_dir() == tmp_path / "nokeys"
    assert ts.find_key() is None
    assert ts.sign(b"x", signer_factory=_never_factory).reason == "no-key"
    assert ts.status().state == "not enrolled"


@pytest.fixture
def fixture_bypassed(monkeypatch):
    monkeypatch.setattr(ts, "_real_key_dir", K.pwd_key_dir)


def test_guard_refuses_real_dir_when_fixture_bypassed(fixture_bypassed):
    assert ts._real_key_dir() == REAL
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.find_key()
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        ts.find_key(REAL)
    assert ts.status().state == "warning"


def test_guard_covers_sign_factories_create_and_ssh_signer(fixture_bypassed):
    key_path = REAL / ts.KEY_LABEL
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


def test_guard_covers_custody_key_dir(fixture_bypassed):
    from levain.cockpit import custody
    with pytest.raises(K.KeyError_, match="real operator key directory"):
        custody._key_dir(REAL)


def test_guard_resolves_links(tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(REAL.parent if REAL.parent.exists() else tmp_path)
    if REAL.parent.exists():
        with pytest.raises(K.KeyError_):
            K.refuse_real_key_in_tests(link / "keys" / ts.KEY_LABEL)


def test_temp_key_dir_is_untouched(tmp_path):
    K.refuse_real_key_in_tests(tmp_path / "keys")
    K.refuse_real_key_in_tests(None)
    assert ts.find_key(tmp_path / "nowhere") is None
