"""levain.cockpit.keys (K2b-3c, trust-tiering S3a-2): enrol, revoke and list as signed authority-log
links, and their refusals. The L4 enroll drive (project memory 1010-cockpit/L4_k2b/enroll_drive.py)
drove the /2 unsigned enroll end to end; its signed successor waits on `levain cockpit ledger init`.

A laptop-presence key must be a FIDO key, which cannot be made headless: every test here widens the
class floor to software ed25519 keys (``widen_laptop_floor``); the floor itself is tested in
``test_authority``."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.install import marker_path
from levain.cockpit.keys import KeyError_, enroll, keys, normalise_public_key, principal_of, revoke
from tests.cockpit_authority_rig import RealKey, enrol as genesis_enrol, widen_laptop_floor

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _laptop_floor(monkeypatch):
    widen_laptop_floor(monkeypatch)


def _key(tmp: Path, name: str) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(tmp / name)], check=True)
    return (tmp / f"{name}.pub").read_text()


def _journal(tmp: Path, cls: str = "reported") -> Path:
    root = tmp / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class(cls)
    return root


def _initialised(tmp: Path) -> tuple[Path, RealKey]:
    """A reported journal whose log's genesis enrols ``laptop`` (what `ledger init` will do)."""
    root = _journal(tmp)
    laptop = RealKey(tmp, "laptop")
    genesis_enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    return root, laptop


def _by(k: RealKey) -> dict:
    return {"signer_fp": k.fingerprint, "sign": k.sign_link, "clock": lambda: NOW}


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
    k = RealKey(tmp_path, "k")
    with pytest.raises(KeyError_, match="no cockpit journal"):
        enroll(root, _key(tmp_path, "a"), "laptop-presence", **_by(k), marker_directory=tmp_path / "m")
    assert list(root.iterdir()) == []


def test_a_governed_marker_or_class_refuses(tmp_path: Path) -> None:
    root = _journal(tmp_path)
    m = marker_path(root, tmp_path / "m")
    m.parent.mkdir()
    m.write_text("{}")
    with pytest.raises(KeyError_, match="governed marker"):
        keys(root, now=NOW, marker_directory=tmp_path / "m")
    other = tmp_path / "g"
    other.mkdir()
    gov = _journal(other, cls="governed")
    k = RealKey(tmp_path, "k")
    with pytest.raises(KeyError_, match="install class is 'governed'"):
        enroll(gov, _key(tmp_path, "b"), "laptop-presence", **_by(k), marker_directory=tmp_path / "none")


def test_an_empty_log_refuses_and_names_ledger_init(tmp_path: Path) -> None:
    root = _journal(tmp_path)
    k = RealKey(tmp_path, "k")
    with pytest.raises(KeyError_, match="ledger init"):
        enroll(root, k.public, "laptop-presence", **_by(k), marker_directory=tmp_path / "m")
    rows, note = keys(root, now=NOW, marker_directory=tmp_path / "m")
    assert rows == [] and "ledger init" in str(note)


def test_enroll_revoke_rules(tmp_path: Path) -> None:
    root, laptop = _initialised(tmp_path)
    kw = {**_by(laptop), "marker_directory": tmp_path / "m"}
    with pytest.raises(KeyError_, match="unknown key class"):
        enroll(root, _key(tmp_path, "x"), "operator", **kw)
    phone, created, warning = enroll(root, _key(tmp_path, "p"), "display-phone", **kw)
    assert created and warning is None and phone.principal == principal_of(phone.fingerprint)
    assert (phone.enrolled_seq, phone.key_class) == (2, "display-phone")
    revoke(root, phone.principal, **kw)          # by principal; a phone key is never the last laptop key
    with pytest.raises(KeyError_, match="last unrevoked laptop-presence"):
        revoke(root, laptop.fingerprint, **kw)
    with pytest.raises(KeyError_, match="keeps its class"):
        enroll(root, laptop.public, "display-phone", **kw)
    with pytest.raises(KeyError_, match="stays revoked"):
        enroll(root, (tmp_path / "p.pub").read_text(), "display-phone", **kw)
    with pytest.raises(KeyError_, match="no enrolled key"):
        revoke(root, "SHA256:nope", **kw)
    with pytest.raises(KeyError_, match="an authority seq"):
        revoke(root, laptop.fingerprint, compromised_since=0, **kw)


def test_a_phone_key_cannot_sign_an_enrolment_and_an_unenrolled_key_signs_nothing(tmp_path: Path) -> None:
    root, laptop = _initialised(tmp_path)
    phone = RealKey(tmp_path, "phone")
    enroll(root, phone.public, "display-phone", **_by(laptop), marker_directory=tmp_path / "m")
    with pytest.raises(KeyError_, match="display-phone key cannot sign this enrol"):
        enroll(root, _key(tmp_path, "x"), "presence", **_by(phone), marker_directory=tmp_path / "m")
    stranger = RealKey(tmp_path, "stranger")
    with pytest.raises(KeyError_, match="not enrolled"):
        enroll(root, _key(tmp_path, "y"), "presence", **_by(stranger), marker_directory=tmp_path / "m")
    # a presence key may not revoke a display-phone key (class >= the revoked key's)
    weak = RealKey(tmp_path, "weak")
    enroll(root, weak.public, "presence", **_by(laptop), marker_directory=tmp_path / "m")
    with pytest.raises(KeyError_, match="presence key cannot sign this revoke_key"):
        revoke(root, phone.fingerprint, **_by(weak), marker_directory=tmp_path / "m")


def test_the_cli_helps_tier_claims_hold() -> None:
    # `levain cockpit --help`: "class laptop-presence signs T2 and T3, display-phone T2"
    from levain.cockpit.firejournal import _CLASS_RANK, _TIER_NEEDS

    def signs(cls: str) -> set[str]:
        return {t for t, need in _TIER_NEEDS.items() if _CLASS_RANK[cls] >= need}
    assert signs("laptop-presence") == {"T2", "T3"}
    assert signs("display-phone") == {"T2"}
    assert signs("presence") == set()


def test_revoke_lands_when_another_laptop_key_exists_and_records_the_compromise(tmp_path: Path) -> None:
    root, a = _initialised(tmp_path)
    kw = {**_by(a), "marker_directory": tmp_path / "m"}
    again, created, _ = enroll(root, a.public, "laptop-presence", **kw)
    assert not created and again.fingerprint == a.fingerprint and again.enrolled_seq == 1
    c = RealKey(tmp_path, "c")
    enroll(root, c.public, "laptop-presence", **kw)
    r, _ = revoke(root, a.fingerprint, compromised_since=1, **{**kw, **_by(c)})
    assert (r.revoked_seq, r.compromised_since) == (3, 1)
    rows, note = keys(root, now=NOW, marker_directory=tmp_path / "m")
    assert note is None and [k.fingerprint for k in rows if k.revoked_seq is None] == [c.fingerprint]
    # a revoked key signs nothing after its revoke
    with pytest.raises(KeyError_, match="revoked at seq 3"):
        enroll(root, _key(tmp_path, "z"), "presence", **kw)
    # the last laptop key: a plain revoke refuses, a compromised one lands
    ckw = {**_by(c), "marker_directory": tmp_path / "m"}
    with pytest.raises(KeyError_, match="compromised_since if it is lost or stolen"):
        revoke(root, c.fingerprint, **ckw)
    assert revoke(root, c.fingerprint, compromised_since=2, **ckw)[0].revoked_seq == 4
    with pytest.raises(KeyError_, match="aware"):
        enroll(root, _key(tmp_path, "n"), "presence", signer_fp=c.fingerprint, sign=c.sign_link,
               clock=lambda: datetime(2026, 10, 11), marker_directory=tmp_path / "m")


def test_the_cli_enrolls_revokes_and_refuses_with_exit_codes(tmp_path: Path, capsys, monkeypatch) -> None:
    from levain import cli
    from levain.cockpit import install

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")   # no real governed marker dir
    root, laptop = _initialised(tmp_path)
    _key(tmp_path, "c")
    run = lambda *a: cli.main(["cockpit", *a, "--data-root", str(root)])   # noqa: E731
    assert run("enroll", str(tmp_path / "c.pub"), "--class", "display-phone", "--signing-key", str(laptop.path)) == 0
    assert "at seq 2" in capsys.readouterr().out
    assert run("enroll", str(tmp_path / "nope.pub"), "--signing-key", str(laptop.path)) == 1
    assert run("enroll", str(tmp_path / "c.pub"), "--signing-key", str(tmp_path / "nokey")) == 1
    assert "cannot read the signing key" in capsys.readouterr().err
    c_fp = keys(root, now=NOW, marker_directory=tmp_path / "m")[0][1].fingerprint
    with pytest.raises(SystemExit):
        run("revoke", c_fp, "--signing-key", str(laptop.path), "--compromised-since", "garbage")
    assert run("revoke", laptop.fingerprint, "--signing-key", str(laptop.path)) == 1   # the last laptop key
    assert "last unrevoked laptop-presence" in capsys.readouterr().err
    assert run("revoke", c_fp, "--signing-key", str(laptop.path), "--compromised-since", "2") == 0
    assert "compromised since seq 2" in capsys.readouterr().out
    assert run("keys") == 0
    out = capsys.readouterr().out
    assert "revoked at seq 3, compromised since seq 2" in out and "enrolled at seq 1" in out


def test_keys_prints_the_enrol_time_only_when_it_is_set(tmp_path: Path, capsys, monkeypatch) -> None:
    """L3 r1 F4 (1012+80): a key with no enrol time lists no "(None)"."""
    from levain import cli
    from levain.cockpit import keys as cockpit_keys
    from levain.cockpit.keys import KeyRow

    rows = [KeyRow("SHA256:untimed", "kuntimed", "presence", 1, None, None, None),
            KeyRow("SHA256:timed", "ktimed", "presence", 2, "2026-10-11T12:00:00.000000Z", None, None)]
    monkeypatch.setattr(cockpit_keys, "keys", lambda *a, **kw: (rows, None))
    assert cli.main(["cockpit", "keys", "--data-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("enrolled at seq 1") and "None" not in out[0]
    assert out[1].endswith("enrolled at seq 2 (2026-10-11T12:00:00.000000Z)")


def test_a_busy_journal_is_a_refusal_not_a_traceback(tmp_path: Path, capsys) -> None:
    import sqlite3

    from levain import cli

    root, laptop = _initialised(tmp_path)
    _key(tmp_path, "a")
    hold = sqlite3.connect(root / "cockpit.db", timeout=0)
    hold.execute("BEGIN EXCLUSIVE")
    try:
        rc = cli.main(["cockpit", "enroll", str(tmp_path / "a.pub"), "--signing-key", str(laptop.path),
                       "--data-root", str(root)])
    finally:
        hold.rollback()
        hold.close()
    assert rc == 1 and "enroll refused" in capsys.readouterr().err


def test_an_oversized_key_file_is_refused(tmp_path: Path, capsys) -> None:
    from levain import cli

    root, laptop = _initialised(tmp_path)
    big = tmp_path / "big.pub"
    big.write_bytes(b"ssh-ed25519 " + b"A" * 20000)
    assert cli.main(["cockpit", "enroll", str(big), "--signing-key", str(laptop.path), "--data-root", str(root)]) == 1
    assert "larger than a public key file" in capsys.readouterr().err


def test_an_unreadable_marker_path_refuses_and_is_never_read_as_absent(tmp_path: Path, monkeypatch) -> None:
    # L3 r1 codex + codex-pack HIGH: os.path.lexists read an EACCES on the marker as "no marker", so a
    # governed root whose marker the operator cannot lstat took reported keys. The EACCES is injected
    # (a chmod does not deny root), for the marker path only.
    import errno
    import os

    from levain.cockpit.install import decide_install

    root = _journal(tmp_path)
    mdir = tmp_path / "m"
    mdir.mkdir()
    marker = marker_path(root, mdir)
    real_lstat = os.lstat

    def lstat(path, *a, **kw):
        if os.fspath(path) == os.fspath(marker):
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(KeyError_, match="cannot read the governed marker"):
        keys(root, now=NOW, marker_directory=mdir)
    decision = decide_install("reported", root, marker_directory=mdir)
    assert decision.install_class is None and "cannot read the governed marker" in str(decision.reason)
    monkeypatch.undo()
    assert decide_install("reported", root, marker_directory=tmp_path / "absent").install_class == "reported"
