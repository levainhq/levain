"""levain.cockpit.keys (K2b-3c): the refusals the L4 enroll drive does not reach. The drive
(project memory L4_k2b/enroll_drive.py) runs enroll -> real broker signature -> revoke end to end."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.install import marker_path
from levain.cockpit.keys import KeyError_, enroll, keys, normalise_public_key, principal_of, revoke

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _key(tmp: Path, name: str) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(tmp / name)], check=True)
    return (tmp / f"{name}.pub").read_text()


def _journal(tmp: Path, cls: str = "reported") -> Path:
    root = tmp / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class(cls)
    return root


def test_a_key_line_is_stored_as_type_and_base64_only(tmp_path: Path) -> None:
    pub = _key(tmp_path, "a")
    assert normalise_public_key(pub) == " ".join(pub.split()[:2])
    for bad, why in ((f"cert-authority {pub}", "carries options"), (pub + pub, "exactly one"),
                     ("ssh-ed25519 !!!", "not a valid"), ("", "exactly one"),
                     ("-----BEGIN OPENSSH PRIVATE KEY-----", "carries options")):
        with pytest.raises(KeyError_, match=why):
            normalise_public_key(bad)


def test_a_blob_whose_embedded_type_is_not_the_lines_is_refused(tmp_path: Path) -> None:
    blob = _key(tmp_path, "a").split()[1]
    with pytest.raises(KeyError_, match="not 'ssh-rsa'"):
        normalise_public_key(f"ssh-rsa {blob}")


def test_no_journal_is_refused_and_none_is_created(tmp_path: Path) -> None:
    root = tmp_path / "cockpit"
    root.mkdir()
    with pytest.raises(KeyError_, match="no cockpit journal"):
        enroll(root, _key(tmp_path, "a"), "laptop-presence", now=NOW, marker_directory=tmp_path / "m")
    assert list(root.iterdir()) == []


def test_a_governed_marker_or_class_refuses(tmp_path: Path) -> None:
    root = _journal(tmp_path)
    m = marker_path(root, tmp_path / "m")
    m.parent.mkdir()
    m.write_text("{}")
    with pytest.raises(KeyError_, match="governed marker"):
        keys(root, marker_directory=tmp_path / "m")
    other = tmp_path / "g"
    other.mkdir()
    gov = _journal(other, cls="governed")
    with pytest.raises(KeyError_, match="install class is 'governed'"):
        enroll(gov, _key(tmp_path, "b"), "laptop-presence", now=NOW, marker_directory=tmp_path / "none")


def test_enroll_revoke_rules(tmp_path: Path) -> None:
    root = _journal(tmp_path)
    kw = {"now": NOW, "marker_directory": tmp_path / "m"}
    with pytest.raises(KeyError_, match="unknown key class"):
        enroll(root, _key(tmp_path, "x"), "operator", **kw)
    a, created = enroll(root, _key(tmp_path, "a"), "laptop-presence", **kw)
    assert created and a.principal == principal_of(a.fingerprint) and a.enrolled_via == "laptop cli"
    phone, _ = enroll(root, _key(tmp_path, "p"), "display-phone", **kw)
    revoke(root, phone.principal, **kw)          # by principal; a phone key is never the last laptop key
    with pytest.raises(KeyError_, match="last unrevoked laptop-presence"):
        revoke(root, a.fingerprint, **kw)
    with pytest.raises(KeyError_, match="in the future"):
        revoke(root, a.fingerprint, compromised_since=datetime(2027, 1, 1, tzinfo=timezone.utc), **kw)
    with pytest.raises(KeyError_, match="stays revoked"):
        enroll(root, (tmp_path / "p.pub").read_text(), "display-phone", **kw)
    with pytest.raises(KeyError_, match="no enrolled key"):
        revoke(root, "SHA256:nope", **kw)


def test_the_cli_helps_tier_claims_hold() -> None:
    # `levain cockpit --help`: "class laptop-presence signs T2 and T3, display-phone T2"
    from levain.cockpit.firejournal import _CLASS_RANK, _TIER_NEEDS

    def signs(cls: str) -> set[str]:
        return {t for t, need in _TIER_NEEDS.items() if _CLASS_RANK[cls] >= need}
    assert signs("laptop-presence") == {"T2", "T3"}
    assert signs("display-phone") == {"T2"}
    assert signs("presence") == set()
