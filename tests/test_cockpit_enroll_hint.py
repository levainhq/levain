"""`levain cockpit enroll` reminds the operator of the second trust list after a passkey enrol (1105-7): the line a desk's
signer list needs for this key, and one sentence saying where it goes. Output text only."""

from __future__ import annotations

from pathlib import Path

from levain import cli
from levain.cockpit import install
from tests.cockpit_authority_rig import RealKey
from tests.test_cockpit_keyfacts import Passkey, _cockpit

SENTENCE = "add the line below to the signer list your desk verifies against"


def _enroll(tmp_path: Path, monkeypatch, key_file: Path, root: Path, laptop, *extra: str) -> int:
    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    return cli.main(["cockpit", "enroll", str(key_file), "--signing-key", str(laptop.path), "--data-root", str(root),
                     *extra])


def test_a_passkey_enrol_prints_its_signer_line_and_the_sentence(tmp_path: Path, capsys, monkeypatch) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    key_file = tmp_path / "pk.pub"
    key_file.write_text(pk.line + "\n")
    assert _enroll(tmp_path, monkeypatch, key_file, root, laptop, "--class", "display-phone", "--custody",
                   "passkey") == 0
    out = capsys.readouterr().out
    assert SENTENCE in out and "needs_phill_signers" in out and "verifies signatures elsewhere" in out
    line = f'phill namespaces="flow-needs-phill,flow-cockpit-message,flow-i3b-resolve" {pk.line}'
    assert line in out.splitlines()                 # the whole line, alone on its own, ready to paste
    # the rerun of an enrolled passkey is a success too, and repeats the reminder
    assert _enroll(tmp_path, monkeypatch, key_file, root, laptop, "--class", "display-phone", "--custody",
                   "passkey") == 0
    again = capsys.readouterr().out
    assert "already enrolled" in again and line in again.splitlines() and SENTENCE in again


def test_a_failed_enrol_prints_neither(tmp_path: Path, capsys, monkeypatch) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    key_file = tmp_path / "pk.pub"
    key_file.write_text(pk.line + "\n")
    # a passkey is a display-phone key only: enrolling it as laptop-presence is refused before anything is signed
    assert _enroll(tmp_path, monkeypatch, key_file, root, laptop, "--class", "laptop-presence", "--custody",
                   "passkey") == 1
    cap = capsys.readouterr()
    assert "enroll refused" in cap.err
    assert SENTENCE not in cap.out + cap.err and pk.line not in cap.out + cap.err
    # an unreadable key file is a refusal as well
    assert _enroll(tmp_path, monkeypatch, tmp_path / "nope.pub", root, laptop, "--class", "display-phone") == 1
    cap = capsys.readouterr()
    assert SENTENCE not in cap.out + cap.err


def test_a_key_that_is_not_a_passkey_prints_no_reminder(tmp_path: Path, capsys, monkeypatch) -> None:
    # a terminal key signs in other namespaces than a passkey: a passkey line for it would be wrong
    root, laptop = _cockpit(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    k = RealKey(other, "second")
    assert _enroll(tmp_path, monkeypatch, Path(str(k.path) + ".pub"), root, laptop, "--class", "display-phone") == 0
    out = capsys.readouterr().out
    assert "enrolled:" in out and SENTENCE not in out and "namespaces=" not in out
