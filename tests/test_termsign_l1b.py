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


def _cli(*argv):
    root = argparse.ArgumentParser()
    band_view.add_parser(root.add_subparsers(dest="cmd", required=True))
    args = root.parse_args(["band", *argv])
    return args.func(args)


def test_create_calls_the_one_door_with_the_fixed_label(tmp_path):
    calls = []
    rep = ts.create(tmp_path / "keys", creator=_creator(calls))
    assert calls == [(tmp_path / "keys", "levain-terminal")]
    assert rep.created and rep.public_key == PUB
    assert 'phill namespaces="levain-terminal-answer" ecdsa-sha2-nistp256 AAAAE2Vj' in rep.text
    assert "scripts/needs_phill_signers" in rep.text and "levain cockpit enroll" in rep.text
    assert "levain-terminal\n" not in rep.text.split("namespaces=")[1].split("\n")[0]    # the comment is dropped


def test_create_refuses_when_enrolled(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "find_key", lambda d=None: ts.TerminalKey(Path("k"), PUB))
    calls = []
    rep = ts.create(tmp_path, creator=_creator(calls))
    assert not rep.created and "already enrolled" in rep.text and calls == []


def test_create_unreadable_dir_is_a_refusal_not_a_creation(tmp_path, monkeypatch):
    def boom(d=None):
        raise KeyError_("not a private directory")
    monkeypatch.setattr(ts, "find_key", boom)
    calls = []
    rep = ts.create(tmp_path, creator=_creator(calls))
    assert not rep.created and "private directory" in rep.text and calls == []


@pytest.mark.parametrize("exc", [KeyError_("sc_auth could not create the identity\nmore"), RuntimeError("boom")])
def test_create_failure_is_a_report(tmp_path, exc):
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
