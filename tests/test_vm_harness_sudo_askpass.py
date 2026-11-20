"""The rig drives ``install-helper`` verbs through ``sudo -A`` with a SUDO_ASKPASS helper the harness stages in the guest, so the
product's PASSWD rule for the helper (authenticate, timestamp_timeout=0) is satisfied, not weakened and not hand-edited around.
The guest transport and the subprocess seam are stubbed: no VM, no sudo, no root."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from scripts.vm_harness import sudo_askpass as SA
from scripts.vm_harness import stage_call as SC
from scripts.vm_harness import wave3_e2e as W
from scripts.vm_harness.host_profile import DARWIN
from tests import test_vm_harness_wave3_e2e as T

PATH = "/tmp/fire/sudo-askpass.sh"
HELPER_ARGV_TAIL = ["routes-trust"]


# ---- host side: the staged helper ---------------------------------------------------------------------------------------

def test_the_askpass_text_prints_the_password_and_is_safe_for_a_hostile_one():
    for pw in ("admin", "it's a \"pw\" $(id) `x` \\"):
        text = SA.askpass_text(pw)
        out = subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True).stdout
        assert out == pw + "\n"


def test_staging_sends_the_password_on_stdin_only_and_makes_the_file_private():
    calls = []
    SA.stage(lambda argv, text, timeout=60: calls.append((list(argv), text)) or "", DARWIN, PATH)
    (argv, text), = calls
    assert text == SA.askpass_text(DARWIN.ssh_password) and DARWIN.ssh_password not in argv
    script = argv[argv.index("-c") + 1]
    assert PATH in argv and "umask 077" in script and "chmod 700" in script
    # an existing inode or symlink is never written through: removed first, then noclobber refuses a path that reappeared
    assert script.index('rm -f -- "$1"') < script.index("set -C") < script.index("umask 077") < script.index("cat >")
    assert "sudo" not in argv          # the file is the operator's own, so sudo -A run by the operator can exec it


def test_the_path_is_one_absolute_word():
    for bad in ("sudo-askpass.sh", "/tmp/a b", "/tmp/x;y", ""):
        with pytest.raises(ValueError):
            SA.stage(lambda *a, **k: "", DARWIN, bad)


def test_remove_deletes_the_file():
    calls = []
    SA.remove(lambda argv, text, timeout=60: calls.append((list(argv), text)) or "", PATH)
    assert calls == [(["/bin/rm", "-f", PATH], "")]


# ---- guest side: the install-helper call is built with -A and the askpass env ------------------------------------------------

def _capture(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=list(argv), **kw)
        return subprocess.CompletedProcess(argv, 0, b'{"ok":true}\n', b"")

    monkeypatch.setattr(SC.subprocess, "run", fake)
    return seen


def test_the_routes_trust_call_is_sudo_dash_a_with_the_askpass_env(monkeypatch, tmp_path):
    ask = tmp_path / "ask.sh"
    ask.write_text("#!/bin/sh\n")
    helper = tmp_path / "install-helper"
    helper.write_text("")
    seen = _capture(monkeypatch)
    rc, out, err = SC.routes_trust_via_askpass(str(ask), helper=str(helper))(b"BODY")
    assert (rc, out) == (0, '{"ok":true}\n')
    assert seen["argv"][:3] == ["/usr/bin/sudo", "-A", str(helper)] and seen["argv"][3:] == HELPER_ARGV_TAIL
    assert "-n" not in seen["argv"]                    # -n would refuse a password the askpass can give
    assert seen["env"]["SUDO_ASKPASS"] == str(ask) and seen["env"]["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert seen["input"] == b"BODY"
    assert set(seen["env"]) == {"PATH", "SUDO_ASKPASS"}   # nothing else of the operator's environment goes to sudo


def test_replace_adds_the_flag(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    helper = tmp_path / "h"
    helper.write_text("")
    SC.routes_trust_via_askpass("/x", helper=str(helper))(b"", replace=True)
    assert seen["argv"][-2:] == ["routes-trust", "--replace"]


def test_a_missing_helper_answers_127_without_running_sudo(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    rc, out, err = SC.routes_trust_via_askpass("/x", helper=str(tmp_path / "nope"))(b"")
    assert rc == 127 and "argv" not in seen


def test_stage_pins_the_route_trust_through_the_askpass_not_the_products_sudo(monkeypatch, tmp_path):
    ask = tmp_path / "ask.sh"
    ask.write_text("#!/bin/sh\n")
    ask.chmod(0o700)
    a = T_args(tmp_path, sudo_askpass=str(ask))
    monkeypatch.setattr("levain.cockpit.keys.signing_public_key", lambda p: "ecdsa-sha2-nistp256 AAAA")
    monkeypatch.setattr("levain.cockpit.keys.fingerprint_of", lambda pub: "SHA256:x")
    from levain.dispatch import cli as dcli
    monkeypatch.setattr(dcli, "ROUTES_TRUST_HELPER", lambda *x, **k: pytest.fail("the product's sudo (no -A) was used"))
    via = []
    monkeypatch.setattr(SC, "routes_trust_via_askpass", lambda path, **k: via.append(path) or (lambda data, **kw: (0, '{"ok":true}\n', "")))

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        if "init" in argv:
            return 0, json.dumps({"store_id": "s1", "route_trust": {"pinned": False}}) + "\n", ""
        raise StopAfterPin

    monkeypatch.setattr(SC, "run", fake_run)
    with pytest.raises(StopAfterPin):
        SC.mode_stage(a)
    assert via == [str(ask)]


class StopAfterPin(Exception):
    pass


def T_args(tmp_path, **kw):
    from tests.test_vm_harness_stage_call import _args
    return _args(tmp_path, **kw)


def test_an_askpass_that_is_not_on_the_guest_stops_the_stage_before_any_store_step(monkeypatch, tmp_path, capsys):
    a = T_args(tmp_path, sudo_askpass=str(tmp_path / "missing.sh"))
    monkeypatch.setattr("levain.cockpit.keys.signing_public_key", lambda p: "ecdsa-sha2-nistp256 AAAA")
    monkeypatch.setattr("levain.cockpit.keys.fingerprint_of", lambda pub: "SHA256:x")
    monkeypatch.setattr(SC, "run", lambda *x, **k: pytest.fail("a store step ran"))
    with pytest.raises(SystemExit):
        SC.mode_stage(a)
    assert "sudo-askpass" in capsys.readouterr().out


def test_the_flag_is_parsed_and_defaults_to_the_old_behaviour(monkeypatch):
    got = {}
    monkeypatch.setattr(SC, "mode_selfcheck", lambda a: got.update(v=a.sudo_askpass) or 0)
    SC.main(["selfcheck"])
    assert got["v"] == ""


# ---- wave3: the sitting stages the helper before the stage call and removes it after --------------------------------------

def test_wave3_stages_the_askpass_before_the_stage_call_names_it_and_removes_it(tmp_path):
    w = T.World()
    order = []
    og, os_ = w.guest, w.stdin_run

    def guest(argv, timeout=60):
        inner = list(argv[4:])
        if inner[:2] == ["stage", "it"]:
            assert inner == ["stage", "it", "--sudo-askpass", PATH]
            order.append("stage-call")
            return "\n".join(["staged gen=4"]) + f"\n{T.RB.RC_MARK} 0\n"
        return og(argv, timeout)

    def stdin_run(argv, text, timeout=60):
        if PATH in argv and "umask 077" in " ".join(argv):
            order.append("askpass-staged")
            assert text == SA.askpass_text(DARWIN.ssh_password)
            return ""
        if list(argv) == ["/bin/rm", "-f", PATH]:
            order.append("askpass-removed")
            return ""
        return os_(argv, text, timeout)

    w.guest, w.stdin_run = guest, stdin_run
    _w, ev, (status, _s, _p) = T.go(tmp_path, w, sudo_askpass=PATH)
    assert order[0] == "askpass-staged" and order.index("stage-call") > 0 and order[-1] == "askpass-removed"
    assert status == "PASS"
    for p in ev.iterdir():
        assert SA.askpass_text(DARWIN.ssh_password) not in p.read_text(), p.name


def test_wave3_without_the_flag_runs_as_before(tmp_path):
    w, _ev, (status, _s, _p) = T.go(tmp_path)
    assert status == "PASS" and not any(PATH in " ".join(a) for a in w.argvs)


# ---- round 1 fixes -----------------------------------------------------------------------------------------------------

def test_staging_does_not_write_through_a_preexisting_file_or_symlink(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "ask.sh"
    link.symlink_to(target)
    done = subprocess.run(list(SA.stage_argv(str(link))), input="secret\n", capture_output=True, text=True)
    assert done.returncode == 0 and target.read_text() == "keep"
    assert not link.is_symlink() and link.read_text() == "secret\n" and (link.stat().st_mode & 0o777) == 0o700


def _askpass_world(order, *, stage_call_raises=False):
    """A World whose askpass stage/removal and stage call are logged in ``order`` (everything else is the World's)."""
    w = T.World()
    og, os_ = w.guest, w.stdin_run

    def guest(argv, timeout=60):
        if list(argv[4:6]) == ["stage", "it"]:
            order.append("stage-call")
            if stage_call_raises:
                raise OSError("ssh dropped")
            return "staged gen=4\n" + f"{T.RB.RC_MARK} 0\n"
        order.append("guest")
        return og(argv, timeout)

    def stdin_run(argv, text, timeout=60):
        if PATH in argv and "umask 077" in " ".join(argv):
            order.append("askpass-staged")
            return ""
        if list(argv) == ["/bin/rm", "-f", PATH]:
            order.append("askpass-removed")
            return ""
        return os_(argv, text, timeout)

    w.guest, w.stdin_run = guest, stdin_run
    return w


def test_a_bad_askpass_path_fails_before_any_key_install(tmp_path):
    w = T.World()
    _w, _ev, (status, _steps, path) = T.go(tmp_path, w, sudo_askpass="/tmp/bad path")
    assert status != "PASS" and path.exists()
    assert w.files == {} and not any(a[:4] == ["sudo", "-n", "/bin/sh", "-c"] and a[4] == T.T3.INSTALL_SCRIPT for a in w.argvs)


def test_the_helper_is_removed_right_after_the_stage_call_not_at_the_end(tmp_path):
    order = []
    T.go(tmp_path, _askpass_world(order), sudo_askpass=PATH)
    assert order.index("askpass-removed") == order.index("stage-call") + 1 and order.count("askpass-removed") == 1


def test_the_helper_is_removed_when_the_stage_call_raises(tmp_path):
    order = []
    T.go(tmp_path, _askpass_world(order, stage_call_raises=True), sudo_askpass=PATH)
    assert order.index("askpass-removed") == order.index("stage-call") + 1


def test_a_raising_askpass_removal_still_runs_the_credential_cleanup(tmp_path, monkeypatch):
    w = _askpass_world([])
    monkeypatch.setattr(W.sudo_askpass, "remove", lambda *a, **k: (_ for _ in ()).throw(ValueError("odd path")))
    _w, _ev, (status, _s, path) = T.go(tmp_path, w, sudo_askpass=PATH)
    assert status != "PASS" and path.exists() and "odd path" in path.read_text()
    assert w.files == {}                      # the credential was removed though the helper's removal raised


# ---- stage_call: the askpass argument and a failed sudo ------------------------------------------------------------------

def _stage_env(monkeypatch):
    monkeypatch.setattr("levain.cockpit.keys.signing_public_key", lambda p: "ecdsa-sha2-nistp256 AAAA")
    monkeypatch.setattr("levain.cockpit.keys.fingerprint_of", lambda pub: "SHA256:x")


def _init_answer(argv, **k):
    return 0, json.dumps({"store_id": "s1", "route_trust": {"pinned": False}}) + "\n", ""


def _executable(tmp_path):
    ask = tmp_path / "ask.sh"
    ask.write_text("#!/bin/sh\n")
    ask.chmod(0o700)
    return ask


def test_a_directory_is_not_an_askpass(monkeypatch, tmp_path, capsys):
    _stage_env(monkeypatch)
    d = tmp_path / "d"
    d.mkdir()
    monkeypatch.setattr(SC, "run", lambda *x, **k: pytest.fail("a store step ran"))
    with pytest.raises(SystemExit):
        SC.mode_stage(T_args(tmp_path, sudo_askpass=str(d)))
    assert "sudo-askpass" in capsys.readouterr().out


def test_a_relative_askpass_is_made_absolute_before_sudo(monkeypatch, tmp_path):
    _stage_env(monkeypatch)
    ask = _executable(tmp_path)
    monkeypatch.chdir(tmp_path)
    via = []
    monkeypatch.setattr(SC, "routes_trust_via_askpass", lambda path, **k: via.append(path) or (lambda data, **kw: (0, '{"ok":true}\n', "")))

    def fake_run(argv, **k):
        if "init" in argv:
            return _init_answer(argv)
        raise StopAfterPin

    monkeypatch.setattr(SC, "run", fake_run)
    with pytest.raises(StopAfterPin):
        SC.mode_stage(T_args(tmp_path, sudo_askpass="ask.sh"))
    assert via == [str(ask)]


@pytest.mark.parametrize("answer", [(1, "", "sudo: 3 incorrect password attempts\n"), (0, "", ""), (0, "not json\n", "")])
def test_a_failed_sudo_is_the_promised_routes_trust_result_not_a_traceback(monkeypatch, tmp_path, capsys, answer):
    _stage_env(monkeypatch)
    ask = _executable(tmp_path)
    monkeypatch.setattr(SC, "routes_trust_via_askpass", lambda path, **k: (lambda data, **kw: answer))
    monkeypatch.setattr(SC, "run", _init_answer)
    with pytest.raises(SystemExit):
        SC.mode_stage(T_args(tmp_path, sudo_askpass=str(ask)))
    assert capsys.readouterr().out.splitlines()[-1].startswith("stage-failed routes-trust: ")
