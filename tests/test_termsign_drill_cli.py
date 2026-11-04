"""scripts/termsign_drill_cli.py: the recovery-drill wrapper that lets the two ssh-askpass names through the CLI's
child environment, and refuses to run against the account's real home."""
from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "termsign_drill_cli.py"


def _load():
    spec = importlib.util.spec_from_file_location("termsign_drill_cli", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_script_never_handles_a_passphrase():
    text = SCRIPT.read_text()
    assert "print(" in text and "passphrase" in text          # the docstring and the refusal exist
    # no code path prints or logs the askpass values: the only print is the fixed HOME refusal
    assert text.count("print(") == 1
    assert "os.environ[" not in text and "getenv" not in text


def test_refuses_the_real_home(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setenv("HOME", mod._real_home())
    assert mod.main(["cockpit", "keys"]) == 2
    assert "real home" in capsys.readouterr().err
    monkeypatch.delenv("HOME")
    assert mod.main(["cockpit", "keys"]) == 2


def test_passes_only_the_askpass_names_through(monkeypatch, tmp_path):
    from levain import cli, launch

    mod = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SSH_ASKPASS", "/x/askpass.sh")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("SOME_OTHER_SECRETISH", "nope")
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        seen["env"] = launch.child_env()
        return 7

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setattr(launch, "allowed_env", launch.allowed_env)      # restored by monkeypatch after the wrapper patches it
    assert launch.child_env().get("SSH_ASKPASS") is None             # the real list strips it
    assert mod.main(["cockpit", "keys"]) == 7
    assert seen["argv"] == ["cockpit", "keys"]
    assert seen["env"]["SSH_ASKPASS"] == "/x/askpass.sh"
    assert seen["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert "SOME_OTHER_SECRETISH" not in seen["env"]


def test_refuses_what_is_not_confined_to_scratch(monkeypatch, tmp_path, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "_real_home", lambda: str(tmp_path / "realhome"))
    scratch = tmp_path / "scratch" / "home"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path / "scratch"))
    monkeypatch.setenv("HOME", str(scratch))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    assert mod._refusal(["cockpit", "keys"]) is None
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "realhome" / ".levain"))      # points outside the scratch HOME
    assert "LEVAIN_HOME" in mod._refusal(["cockpit", "keys"])
    monkeypatch.delenv("LEVAIN_HOME")
    assert "real home" in mod._refusal(["--signing-key", str(tmp_path / "realhome" / ".ssh" / "id_x")])
    monkeypatch.setenv("HOME", "/")                                                    # not under the temp dir
    assert mod._refusal([]) is not None
    monkeypatch.setenv("HOME", str(tmp_path / "scratch"))                              # the temp dir itself
    assert mod._refusal([]) is not None
