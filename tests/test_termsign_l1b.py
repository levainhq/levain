"""TERMSIGN L1b: the `levain band terminal-key {create,status}` verbs, `terminal_sign.create` and the doctor row.
Nothing here reaches sc_auth, the Secure Enclave or a Touch ID sheet: every creator is injected."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from levain import band_view, doctor
from levain.cockpit.custody import CreatedKey
from levain.cockpit.keys import KeyError_
from levain.consent import terminal_sign as ts

PUB = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBExample levain-terminal"


def _creator(calls, exc=None):
    def make(key_dir, label):
        calls.append((key_dir, label))
        if exc is not None:
            raise exc
        return CreatedKey(Path(key_dir) / label, Path(key_dir) / (label + ".pub"), PUB, "touch-id", label, "ab" * 20)
    return make


@pytest.fixture
def outside(monkeypatch):
    """This test process runs as no agent session (the band's one detector, where create() reads it)."""
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: False)


def _cli(*argv):
    root = argparse.ArgumentParser()
    band_view.add_parser(root.add_subparsers(dest="cmd", required=True))
    args = root.parse_args(["band", *argv])
    return args.func(args)


def test_create_calls_the_one_door_with_the_fixed_label(tmp_path, outside):
    calls = []
    rep = ts.create(tmp_path / "keys", creator=_creator(calls))
    assert calls == [(tmp_path / "keys", "levain-terminal")]
    assert rep.created and rep.public_key == PUB
    assert 'phill namespaces="levain-terminal-answer" ecdsa-sha2-nistp256 AAAAE2Vj' in rep.text
    assert "scripts/needs_phill_signers" in rep.text and "levain cockpit enroll" in rep.text
    assert "levain-terminal\n" not in rep.text.split("namespaces=")[1].split("\n")[0]    # the comment is dropped


def test_create_refuses_when_enrolled(tmp_path, monkeypatch, outside):
    monkeypatch.setattr(ts, "find_key", lambda d=None: ts.TerminalKey(Path("k"), PUB))
    calls = []
    rep = ts.create(tmp_path, creator=_creator(calls))
    assert not rep.created and "already enrolled" in rep.text and calls == []


def test_create_unreadable_dir_is_a_refusal_not_a_creation(tmp_path, monkeypatch, outside):
    def boom(d=None):
        raise KeyError_("not a private directory")
    monkeypatch.setattr(ts, "find_key", boom)
    calls = []
    rep = ts.create(tmp_path, creator=_creator(calls))
    assert not rep.created and "private directory" in rep.text and calls == []


@pytest.mark.parametrize("exc", [KeyError_("sc_auth could not create the identity\nmore"), RuntimeError("boom")])
def test_create_failure_is_a_report(tmp_path, exc, outside):
    rep = ts.create(tmp_path / "keys", creator=_creator([], exc))
    assert not rep.created and rep.text.startswith("not created:") and "\n" not in rep.text


def test_cli_create_refused_inside_an_agent_session(monkeypatch, capsys):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: True)
    monkeypatch.setattr(ts, "create", lambda *a, **k: pytest.fail("created inside an agent session"))
    assert _cli("terminal-key", "create") != 0
    assert "refused" in capsys.readouterr().err


def test_cli_create_prints_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: False)
    monkeypatch.setattr(ts, "create", lambda *a, **k: ts.CreateReport(True, "made", PUB))
    assert _cli("terminal-key", "create") == 0
    assert capsys.readouterr().out.strip() == "made"


def test_cli_status_not_enrolled_on_this_mac(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "none")
    assert _cli("terminal-key", "status") == 0
    assert capsys.readouterr().out.startswith("not enrolled")


def test_cli_status_warning_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(ts, "status", lambda *a, **k: ts.StatusReport("warning", "WARNING: x"))
    assert _cli("terminal-key", "status") == 1
    assert "WARNING: x" in capsys.readouterr().err


def test_cli_requires_a_verb():
    with pytest.raises(SystemExit):
        _cli("terminal-key")


@pytest.mark.parametrize("state,text,ok,warn,neutral", [
    ("enrolled", "terminal key: enrolled (SHA256:x), CTK identity bio", True, False, False),
    ("not enrolled", "not enrolled: answers at the terminal are recorded unsigned", True, False, True),
    ("warning", "WARNING: terminal key (SHA256:x): CTK identity missing", True, True, False),
])
def test_doctor_row(monkeypatch, state, text, ok, warn, neutral):
    monkeypatch.setattr(ts, "status", lambda *a, **k: ts.StatusReport(state, text))
    r = doctor._check_terminal_key()
    assert (r.name, r.ok, r.warn, r.neutral) == ("terminal key", ok, warn, neutral)
    assert not r.detail.startswith(("WARNING", "terminal key:"))


def test_doctor_row_is_wired_into_the_run():
    import inspect
    assert "_check_terminal_key" in inspect.getsource(doctor._run_doctor)


# --- L3 r1 (TSL1B2) ------------------------------------------------------------------------------------------

# a touch-id-eligible key line (the golden sk-ecdsa vector, application "ssh:")
SK_PUB = ("sk-ecdsa-sha2-nistp256@openssh.com AAAAInNrLWVjZHNhLXNoYTItbmlzdHAyNTZAb3BlbnNzaC5jb20AAAAIbmlzdHAyNTYAAABB"
          "BOzb70UQZqwNgYD05Q8SZzl5SAQAc+SS0XJhZSutH5Kw3G/+Sc0zbjhT5W3rjaNsQ0UHKuobTNismdkZs02nVaoAAAAEc3NoOg== "
          "levain-terminal")


def _no_raise(call):
    try:
        return call()
    except BaseException as exc:  # noqa: BLE001 - a KeyboardInterrupt here would end the pytest run
        pytest.fail(f"raised {type(exc).__name__}: {exc}")


def test_fix1_custody_import_failure_is_a_report(tmp_path, monkeypatch, outside):
    monkeypatch.delattr("levain.cockpit.custody.create_touch_id")
    rep = _no_raise(lambda: ts.create(tmp_path / "keys"))
    assert not rep.created and rep.text.startswith("not created:") and "ImportError" in rep.text


def test_fix1_key_dir_failure_is_a_report(monkeypatch, outside):
    def gone():
        raise KeyError("getpwuid(): uid not found: 501")
    monkeypatch.setattr(ts, "_real_key_dir", gone)
    calls = []
    rep = _no_raise(lambda: ts.create(creator=_creator(calls)))
    assert not rep.created and "uid not found" in rep.text and calls == []


def test_fix1_ctrl_c_during_creation_is_cancelled(tmp_path, outside):
    rep = _no_raise(lambda: ts.create(tmp_path / "keys", creator=_creator([], KeyboardInterrupt())))
    assert rep == ts.CreateReport(False, "not created: cancelled")


def test_fix1_cli_ctrl_c_prints_no_traceback(tmp_path, monkeypatch, capsys, outside):
    from levain.consent.band import EXIT_REFUSED
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "keys")
    monkeypatch.setattr("levain.cockpit.custody.create_touch_id", _creator([], KeyboardInterrupt()))
    assert _no_raise(lambda: _cli("terminal-key", "create")) == EXIT_REFUSED
    assert capsys.readouterr().err.strip() == "not created: cancelled"


def test_fix2_create_refuses_inside_an_agent_session_before_the_key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: True)    # the default detector
    monkeypatch.setattr(ts, "find_key", lambda d=None: pytest.fail("the key directory was read"))
    calls = []
    rep = ts.create(tmp_path / "keys", creator=_creator(calls))
    assert not rep.created and calls == [] and not (tmp_path / "keys").exists()
    assert rep.text == f"{ts.session_refusal()} (nothing was created)"


def test_fix2_detector_is_injectable_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: pytest.fail("the default was used"))
    calls = []
    assert not ts.create(tmp_path, creator=_creator(calls), in_agent_session=lambda: True).created

    def unreadable():
        raise OSError("ps: operation not permitted")
    rep = ts.create(tmp_path, creator=_creator(calls), in_agent_session=unreadable)
    assert not rep.created and rep.text.startswith("refused:") and calls == []
    assert ts.create(tmp_path / "keys", creator=_creator(calls), in_agent_session=lambda: False).created


def test_fix2_cli_and_create_refuse_in_the_same_words(monkeypatch, capsys):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: True)
    assert _cli("terminal-key", "create") != 0
    assert capsys.readouterr().err.strip() == f"levain band terminal-key: {ts.session_refusal()}"


def test_fix3_printed_enroll_command_parses_and_validates(tmp_path, outside):
    import shlex

    from levain import cli
    from levain.authority import KEY_WORD
    from levain.cockpit.custody import declared_custody

    d = tmp_path / "my keys"            # a space: the path must come through quoted
    d.mkdir(mode=0o700)

    def make(key_dir, label):
        pub = Path(key_dir) / (label + ".pub")
        pub.write_text(SK_PUB + "\n")
        return CreatedKey(Path(key_dir) / label, pub, SK_PUB, "touch-id", label, "ab" * 20)

    rep = ts.create(d, creator=make)
    assert rep.created
    [line] = [x.strip() for x in rep.text.splitlines() if x.strip().startswith("levain cockpit enroll")]
    argv = shlex.split(line)
    assert argv[:3] == ["levain", "cockpit", "enroll"]
    args = cli.build_parser().parse_args(argv[1:])
    assert args.func is cli._cmd_cockpit_enroll
    assert args.key == d / "levain-terminal.pub" and args.signing_key == Path(KEY_WORD)
    raw = args.key.expanduser().read_bytes().decode("utf-8")     # what the verb reads first
    # the verb's own check before anything is signed (linux: no sc_auth is asked)
    assert declared_custody(args.key, args.custody, args.key_class, raw, platform="linux") == "touch-id"
