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


def _the_sign_child_waits(bound: float, seen: list[float]) -> bool:
    """The one sign child is handed the bound less the verify reserve (levain#125 fix 2: the verify runs inside the same
    deadline), less only the real time between the deadline being set and the child starting."""
    (t,) = seen
    want = bound - min(keys._VERIFY_RESERVE_S, bound / 2)
    return want - 0.5 < t <= want


def test_the_default_timeout_is_the_modules(tmp_path, monkeypatch):
    assert _the_sign_child_waits(keys._SIGN_TIMEOUT, _sign_seen(tmp_path, monkeypatch))


def test_a_per_signer_timeout_is_used_and_leaves_the_global_alone(tmp_path, monkeypatch):
    before = keys._SIGN_TIMEOUT
    assert _the_sign_child_waits(20.0, _sign_seen(tmp_path, monkeypatch, timeout=20.0))
    assert keys._SIGN_TIMEOUT == before
