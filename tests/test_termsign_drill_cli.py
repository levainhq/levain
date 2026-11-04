"""scripts/termsign_drill_cli.py: the recovery-drill wrapper that lets the two ssh-askpass names through the CLI's
child environment for one call, and refuses to run unless it is confined to a scratch directory."""
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


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    """A scratch dir S with HOME=S/home, standing in for the system temp dir's mktemp -d."""
    mod = _load()
    s = tmp_path / "S"
    (s / "home").mkdir(parents=True)
    s.chmod(0o700)
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("HOME", str(s / "home"))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    return mod, s


def test_script_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_script_has_one_fixed_message_and_no_passphrase_handling():
    text = SCRIPT.read_text()
    assert text.count("print(") == 1                              # the fixed refusal line, never an env value
    assert "logging" not in text and "stderr.write" not in text and "stdout.write" not in text


def test_refuses_an_unconfined_home(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("HOME", raising=False)
    assert mod.main(["cockpit", "keys"]) == 2
    for bad in ("/", str(tmp_path), str(tmp_path / "S"), str(tmp_path.parent / "elsewhere" / "home")):
        monkeypatch.setenv("HOME", bad)
        assert mod._refusal(["cockpit", "keys"]) is not None, bad


def test_refuses_levain_home_outside_scratch(scratch, monkeypatch, tmp_path):
    mod, s = scratch
    assert mod._refusal(["cockpit", "keys"]) is None
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "real" / ".levain"))
    assert "LEVAIN_HOME" in mod._refusal(["cockpit", "keys"])
    monkeypatch.setenv("LEVAIN_HOME", str(s / "home" / ".levain"))
    assert mod._refusal(["cockpit", "keys"]) is None


def test_every_path_form_must_be_inside_scratch(scratch, monkeypatch, tmp_path):
    mod, s = scratch
    outside = str(tmp_path / "real" / ".ssh" / "id_x")
    inside = str(s / "keys" / "k")
    assert mod._refusal(["cockpit", "enroll", inside + ".pub", "--signing-key", inside, "--data-root", str(s / "d")]) is None
    assert mod._refusal(["cockpit", "keys", "--data-root", outside]) is not None
    assert mod._refusal(["cockpit", "keys", f"--data-root={outside}"]) is not None           # the = form
    assert mod._refusal(["cockpit", "keys", "--signing-key", "~/.ssh/id_x"]) is None         # ~ is the scratch HOME
    monkeypatch.chdir(tmp_path)                                                              # cwd is irrelevant: names resolve in S
    assert mod._refusal(["cockpit", "keys", "--signing-key", "id_x", "--data-root", "data"]) is None   # bare names land in S
    assert mod._refusal(["cockpit", "keys", "--signing-key", "../real/.ssh/id_x"]) is not None
    assert mod._refusal(["cockpit", "keys", "-k/real/.ssh/id_x"]) is not None                   # short option, attached value
    assert mod._refusal(["cockpit", "keys", "--data-root", "d"]) is None
    assert mod._refusal(["cockpit", "revoke", "SHA256:ab/cd+ef", "--signing-key", inside]) is None  # fingerprint holds "/"


def test_only_drill_commands(scratch):
    mod, _ = scratch
    for argv in (["chat"], ["cockpit", "key", "create"], [], ["doctor"]):
        assert mod._refusal(argv) is not None, argv
    assert mod._refusal(["cockpit", "ledger", "init"]) is None


def test_passes_only_the_askpass_names_through_for_one_call(scratch, monkeypatch):
    from levain import cli, launch

    mod, _ = scratch
    monkeypatch.setenv("SSH_ASKPASS", "/x/askpass.sh")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/x/agent.sock")
    monkeypatch.setenv("SOME_OTHER_SECRETISH", "nope")
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        seen["env"] = launch.child_env()
        return 7

    monkeypatch.setattr(cli, "main", fake_main)
    before = launch.allowed_env
    assert launch.child_env().get("SSH_ASKPASS") is None             # the real list strips it
    assert mod.main(["cockpit", "keys"]) == 7
    assert seen["argv"] == ["cockpit", "keys"]
    assert seen["env"]["SSH_ASKPASS"] == "/x/askpass.sh"
    assert seen["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert "SOME_OTHER_SECRETISH" not in seen["env"]
    assert launch.allowed_env is before                              # restored once the call ends
    assert launch.child_env().get("SSH_ASKPASS") is None


def test_a_shared_scratch_dir_is_refused(scratch):
    mod, s = scratch
    s.chmod(0o755)
    assert "private" in mod._refusal(["cockpit", "keys"])


def test_main_runs_the_cli_inside_scratch_and_restores_cwd_and_agent(scratch, monkeypatch, tmp_path):
    from levain import cli

    mod, s = scratch
    monkeypatch.setenv("SSH_AUTH_SOCK", "/x/agent.sock")
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(cli, "main", lambda argv: seen.update(cwd=os.getcwd(), sock=os.environ.get("SSH_AUTH_SOCK")) or 0)
    assert mod.main(["cockpit", "keys"]) == 0
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(s) and seen["sock"] is None
    assert os.getcwd() == os.path.realpath(tmp_path) or os.getcwd() == str(tmp_path)
    assert os.environ["SSH_AUTH_SOCK"] == "/x/agent.sock"


def test_relative_or_missing_scratch_is_refused_not_a_traceback(scratch, monkeypatch, tmp_path):
    mod, s = scratch
    monkeypatch.setenv("HOME", "../S/home")
    assert "absolute" in mod._refusal(["cockpit", "keys"])
    monkeypatch.setenv("HOME", str(s / "home"))
    monkeypatch.setenv("LEVAIN_HOME", "rel")
    assert "absolute" in mod._refusal(["cockpit", "keys"])
    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "gone" / "home"))
    assert mod._refusal(["cockpit", "keys"]) is not None


def test_a_relative_askpass_is_made_absolute_before_the_chdir(scratch, monkeypatch, tmp_path):
    from levain import cli

    mod, s = scratch
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SSH_ASKPASS", "askpass.sh")
    seen = {}
    monkeypatch.setattr(cli, "main", lambda argv: seen.update(a=os.environ["SSH_ASKPASS"]) or 0)
    assert mod.main(["cockpit", "keys"]) == 0
    assert os.path.isabs(seen["a"]) and seen["a"].endswith("/askpass.sh") and not seen["a"].startswith(str(s))
    assert os.environ["SSH_ASKPASS"] == "askpass.sh"
