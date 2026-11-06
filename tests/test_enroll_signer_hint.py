"""The desk signer hint after a passkey enrol is printed only where a desk signer file exists (1105-36).

The hint names flow's ``scripts/needs_phill_signers`` and a principal and namespaces that are flow's desk's. A
Levain-only install has no such list, so the line would send a public user to a file they do not have. The gate is the
file the hint names: found in the working directory or one of its parents, the hint prints; otherwise nothing prints
(the enrol's own success line still does). Output text only."""

from __future__ import annotations

from pathlib import Path

from levain import cli
from levain.cockpit import install
from tests.test_cockpit_keyfacts import Passkey, _cockpit

SENTENCE = "add the line below to the signer list your desk verifies against"
SIGNERS = Path("scripts") / "needs_phill_signers"


def _passkey_enroll(tmp_path: Path, monkeypatch, capsys) -> tuple[str, str]:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    key_file = tmp_path / "pk.pub"
    key_file.write_text(pk.line + "\n")
    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    assert cli.main(["cockpit", "enroll", str(key_file), "--signing-key", str(laptop.path), "--data-root", str(root),
                     "--class", "display-phone", "--custody", "passkey"]) == 0
    return capsys.readouterr().out, pk.line


def test_a_levain_only_install_prints_no_desk_hint(tmp_path: Path, capsys, monkeypatch) -> None:
    here = tmp_path / "plain"
    here.mkdir()
    monkeypatch.chdir(here)                                   # no scripts/needs_phill_signers here or above
    out, line = _passkey_enroll(tmp_path, monkeypatch, capsys)
    assert "enrolled:" in out                                 # the enrol itself still reports
    assert SENTENCE not in out and "needs_phill_signers" not in out
    assert "namespaces=" not in out and line not in out and "phill namespaces" not in out


def test_an_install_with_a_desk_signer_file_prints_the_hint(tmp_path: Path, capsys, monkeypatch) -> None:
    desk = tmp_path / "desk"
    (desk / SIGNERS).parent.mkdir(parents=True)
    (desk / SIGNERS).write_text("")
    monkeypatch.chdir(desk)
    out, line = _passkey_enroll(tmp_path, monkeypatch, capsys)
    assert SENTENCE in out and "needs_phill_signers" in out
    assert f'phill namespaces="flow-needs-phill,flow-cockpit-message,flow-i3b-resolve" {line}' in out.splitlines()


def test_a_subdirectory_of_a_desk_checkout_still_finds_the_file(tmp_path: Path, capsys, monkeypatch) -> None:
    desk = tmp_path / "desk"
    (desk / SIGNERS).parent.mkdir(parents=True)
    (desk / SIGNERS).write_text("")
    sub = desk / "projects" / "x"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    out, _ = _passkey_enroll(tmp_path, monkeypatch, capsys)
    assert SENTENCE in out


def test_a_directory_named_like_the_file_is_not_a_signer_file(tmp_path: Path, capsys, monkeypatch) -> None:
    desk = tmp_path / "desk"
    (desk / SIGNERS).mkdir(parents=True)                      # a directory, not a file
    monkeypatch.chdir(desk)
    out, _ = _passkey_enroll(tmp_path, monkeypatch, capsys)
    assert SENTENCE not in out
