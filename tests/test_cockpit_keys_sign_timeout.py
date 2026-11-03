"""levain.cockpit.keys.ssh_signer's optional per-signer ``timeout`` (a caller wanting a shorter bound for its own
signature no longer narrows the module's global). The default stays the module's ``_SIGN_TIMEOUT``."""

from __future__ import annotations

import subprocess
from pathlib import Path

from levain.cockpit import keys


def _key(tmp: Path) -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(tmp / "k")], check=True)
    return tmp / "k"


class _Seen:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def run(self, argv, **kw):
        self.timeouts.append(kw["timeout"])
        raise subprocess.TimeoutExpired(argv, kw["timeout"])


def _sign_seen(tmp_path: Path, monkeypatch, **signer_kw) -> list[float]:
    seen = _Seen()
    _fp, sign = keys.ssh_signer(_key(tmp_path), **signer_kw)
    monkeypatch.setattr(keys.subprocess, "run", seen.run)
    try:
        sign(b"x")
    except keys.KeyError_:
        pass
    return seen.timeouts


def test_the_default_timeout_is_the_modules(tmp_path, monkeypatch):
    assert _sign_seen(tmp_path, monkeypatch) == [keys._SIGN_TIMEOUT]


def test_a_per_signer_timeout_is_used_and_leaves_the_global_alone(tmp_path, monkeypatch):
    before = keys._SIGN_TIMEOUT
    assert _sign_seen(tmp_path, monkeypatch, timeout=20.0) == [20.0]
    assert keys._SIGN_TIMEOUT == before
