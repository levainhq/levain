"""``levain limits set / unset / show --slots``: the operator's front for root's ``L/policy/limits`` (slot limits S-c).

The real install helper's ``set-limits`` runs in process (its real ``main``, with euid, host, the install lock and the
store root standing in), so the bytes the front sends are the bytes root checks and writes. Head rulings R1 F7: an
unusable file is never merged; ``--replace`` writes a document holding only the change."""
from __future__ import annotations

import argparse
import functools
import io
import json
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from levain.consent import limits as L
from levain.consent import limits_cli as C
from levain.contracts.canon import canonical_bytes
from levain.qbroker.limits import PolicyLimits
from levain.slot import install_helper as IH
from levain.slot import layout
from levain.slot import runtime_helper as RH

ME = os.getuid()


class _NoLock:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "L"
    (root / "policy").mkdir(parents=True)
    os.chmod(root / "policy", 0o755)
    (root / "policy" / "operator").write_text(IH.operator_record_text(pwd.getpwuid(ME).pw_name, ME))
    return root


class Helper:
    """The real ``install_helper.main`` for ``set-limits``, in process; records every document it was handed."""

    def __init__(self, store: Path, *, sudo_uid: int = ME, fsync=None):
        self.store = store
        self.sudo_uid = sudo_uid
        self.fsync = fsync
        self.sent: list[bytes] = []

    def __call__(self, data: bytes) -> tuple[int, str, str]:
        self.sent.append(data)
        limits = functools.partial(IH.run_set_limits, fsync=self.fsync,
                                   store_seams=RH.Seams(host="darwin", store=self.store, trusted=lambda st: True))
        seams = IH.Seams(euid=lambda: 0, host=lambda: "darwin", stdin=lambda n: data[:n], lock=lambda host: _NoLock(),
                         limits=limits)
        out, err = io.StringIO(), io.StringIO()
        rc = IH.main(["set-limits"], env={"SUDO_UID": str(self.sudo_uid)}, seams=seams, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()


def _run(argv: list[str], store: Path, helper=None) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    C.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["limits", *argv])
    out = io.StringIO()
    seams = C.SlotSeams(host="darwin", store=store, owner=ME, helper=helper)
    return C.run(args, out, slots=seams), out.getvalue()


def _doc(store: Path) -> dict:
    return json.loads((store / "policy" / "limits").read_bytes())


def _put(store: Path, doc: dict, mode: int = 0o644) -> bytes:
    data = canonical_bytes(doc)
    path = store / "policy" / "limits"
    path.write_bytes(data)
    os.chmod(path, mode)
    return data


def _reader(store: Path, agent: str) -> dict:
    return PolicyLimits(store / "policy", owner=ME)(agent, "test")


# --- set --slots -----------------------------------------------------------------------------------------------------

def test_set_slots_with_no_file_writes_the_canonical_document_root_reads(store):
    h = Helper(store)
    code, text = _run(["set", "--slots", "default", "calls", "5"], store, h)
    assert code == 0, text
    data = (store / "policy" / "limits").read_bytes()
    assert data == b'{"default":{"calls":5},"format":"levain-limits/1"}'
    assert h.sent == [data]
    assert (os.lstat(store / "policy" / "limits").st_mode & 0o777) == 0o644
    assert _reader(store, "codex") == {"calls": 5.0, "active_hours": 12.0, "repeat": 8.0}
    assert "default calls = 5" in text and "sha256:" in text
    assert "claude_code: calls 5, active hours 12, repeat 8" in text      # what is in force, read back


def test_set_slots_merges_into_the_current_document(store):
    _put(store, {"format": L.LIMITS_FORMAT, "codex": {"active_hours": 2}, "default": {"calls": 50}})
    code, text = _run(["set", "--slots", "claude_code", "repeat", "3"], store, Helper(store))
    assert code == 0, text
    assert _doc(store) == {"format": L.LIMITS_FORMAT, "codex": {"active_hours": 2}, "default": {"calls": 50},
                           "claude_code": {"repeat": 3}}
    code, _ = _run(["set", "--slots", "default", "calls", "60"], store, Helper(store))
    assert code == 0 and _doc(store)["default"] == {"calls": 60}


@pytest.mark.parametrize("kind,value,says", [
    ("fresh_tokens", "5", "counts no tokens"),
    ("cache_read_tokens", "5", "counts no tokens"),
    ("active_hours", "1.5", "never rounded"),
    ("calls", "0", "from 1 to"),
    ("calls", "-3", "from 1 to"),
    ("calls", "1e3", "whole number"),
    ("calls", " 5", "whole number"),
    ("calls", "1_000", "whole number"),
    ("repeat", "1", "from 2 to 10"),
    ("repeat", "11", "from 2 to 10"),
    ("calls", str(10 ** 9 + 1), "from 1 to"),
])
def test_set_slots_refuses_a_bad_value_before_the_password(store, kind, value, says):
    h = Helper(store)
    code, text = _run(["set", "--slots", "default", kind, value], store, h)
    assert code == 1 and "refused, nothing changed" in text and says in text, text
    assert h.sent == []                      # no sudo, so no password asked for a change root would refuse
    assert not (store / "policy" / "limits").exists()


@pytest.mark.parametrize("make,why", [
    (lambda p: (p.write_bytes(b"garbage"), os.chmod(p, 0o644)), "limits-malformed"),
    (lambda p: (p.write_bytes(b'{"default":{"calls":5},"format":"levain-limits/1"}'), os.chmod(p, 0o666)),
     "writable by group or others"),
    (lambda p: (p.write_bytes(b'{"default":{"fresh_tokens":5},"format":"levain-limits/1"}'), os.chmod(p, 0o644)),
     "limits-not-counted:fresh_tokens"),
])
def test_set_slots_never_merges_an_unusable_file_and_names_replace(store, make, why):
    path = store / "policy" / "limits"
    make(path)
    before = path.read_bytes()
    h = Helper(store)
    code, text = _run(["set", "--slots", "codex", "calls", "7"], store, h)
    assert code == 1 and why in text and "is not usable" in text, text
    assert "levain limits set --slots codex calls 7 --replace" in text
    assert h.sent == [] and path.read_bytes() == before
    code, text = _run(["set", "--slots", "codex", "calls", "7", "--replace"], store, h)
    assert code == 0, text
    assert _doc(store) == {"format": L.LIMITS_FORMAT, "codex": {"calls": 7}}
    assert (os.lstat(path).st_mode & 0o777) == 0o644
    assert _reader(store, "codex")["calls"] == 7.0


def test_set_slots_refuses_a_symlink_and_never_reads_through_it(store, tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b'{"default":{"calls":5},"format":"levain-limits/1"}')
    (store / "policy" / "limits").symlink_to(victim)
    h = Helper(store)
    code, text = _run(["set", "--slots", "default", "calls", "9"], store, h)
    assert code == 1 and "a symlink" in text and "--replace" in text and h.sent == []


def test_replace_on_a_usable_file_drops_every_other_limit(store):
    _put(store, {"format": L.LIMITS_FORMAT, "codex": {"active_hours": 2}, "default": {"calls": 50}})
    code, text = _run(["set", "--slots", "default", "repeat", "4", "--replace"], store, Helper(store))
    assert code == 0, text
    assert _doc(store) == {"format": L.LIMITS_FORMAT, "default": {"repeat": 4}}


def test_the_helpers_refusal_is_printed_by_name_with_its_detail(store):
    h = Helper(store, sudo_uid=ME + 4242)            # not the operator the record names
    code, text = _run(["set", "--slots", "default", "calls", "5"], store, h)
    assert code == 1, text
    assert "refused by the install helper" in text and "nothing changed" in text
    rc, out, err = h(canonical_bytes({"format": L.LIMITS_FORMAT}))
    name = json.loads(out)["refused"]
    assert name in text and err.strip() in text
    assert not (store / "policy" / "limits").exists()


def test_a_not_durable_write_is_ok_and_its_warning_is_printed(store):
    def fsync(fd):
        if _is_dir(fd):
            raise OSError(5, "Input/output error")
        os.fsync(fd)

    code, text = _run(["set", "--slots", "default", "calls", "5"], store, Helper(store, fsync=fsync))
    assert code == 0, text
    assert "not durable" in text and "run it again" in text
    assert _reader(store, "codex")["calls"] == 5.0


def _is_dir(fd: int) -> bool:
    import stat as st
    return st.S_ISDIR(os.fstat(fd).st_mode)


def test_no_answer_from_sudo_says_so_and_relays_its_words(store):
    def helper(data):
        return 1, "", "Sorry, try again.\nsudo: 3 incorrect password attempts\n"

    code, text = _run(["set", "--slots", "default", "calls", "5"], store, helper)
    assert code == 1
    assert "no answer from the install helper (exit 1)" in text and "3 incorrect password attempts" in text
    assert "levain limits show --slots" in text


def test_a_store_with_no_policy_directory_is_refused_before_sudo(tmp_path):
    h = Helper(tmp_path / "L")
    code, text = _run(["set", "--slots", "default", "calls", "5"], tmp_path / "L", h)
    assert code == 1 and "not set up" in text and h.sent == []


def test_agent_and_slots_together_are_refused(store):
    h = Helper(store)
    code, text = _run(["set", "--slots", "default", "calls", "5", "--agent", "codex"], store, h)
    assert code == 2 and "--slots" in text and h.sent == []


def test_replace_without_slots_is_refused_and_the_local_file_untouched(store, tmp_path):
    qdir = tmp_path / "consent"
    code, text = _run(["--queue-dir", str(qdir), "set", "calls", "5", "--replace"], store)
    assert code == 2 and "--replace" in text
    assert not (qdir / L.LIMITS_NAME).exists()


# --- unset --slots ---------------------------------------------------------------------------------------------------

def test_unset_slots_removes_the_kind_and_an_emptied_block(store):
    _put(store, {"format": L.LIMITS_FORMAT, "codex": {"active_hours": 2}, "default": {"calls": 50, "repeat": 3}})
    code, text = _run(["unset", "--slots", "codex", "active_hours"], store, Helper(store))
    assert code == 0, text
    assert _doc(store) == {"format": L.LIMITS_FORMAT, "default": {"calls": 50, "repeat": 3}}
    code, text = _run(["unset", "--slots", "default", "calls"], store, Helper(store))
    assert code == 0 and _doc(store) == {"format": L.LIMITS_FORMAT, "default": {"repeat": 3}}


def test_unset_slots_of_a_kind_not_set_changes_nothing_and_asks_no_password(store):
    data = _put(store, {"format": L.LIMITS_FORMAT, "default": {"calls": 50}})
    h = Helper(store)
    for argv in (["unset", "--slots", "codex", "calls"], ["unset", "--slots", "default", "repeat"]):
        code, text = _run(argv, store, h)
        assert code == 0 and "nothing changed" in text
    assert h.sent == [] and (store / "policy" / "limits").read_bytes() == data
    code, text = _run(["unset", "--slots", "default", "calls"], store.parent / "none", h)
    assert code == 1 and "not set up" in text and h.sent == []


def test_unset_slots_on_an_unusable_file_needs_replace(store):
    path = store / "policy" / "limits"
    path.write_bytes(b"garbage")
    os.chmod(path, 0o644)
    h = Helper(store)
    code, text = _run(["unset", "--slots", "default", "calls"], store, h)
    assert code == 1 and "is not usable" in text and "levain limits unset --slots default calls --replace" in text
    assert h.sent == []
    code, text = _run(["unset", "--slots", "default", "calls", "--replace"], store, h)
    assert code == 0, text
    assert _doc(store) == {"format": L.LIMITS_FORMAT}
    assert _reader(store, "claude_code") == {"calls": 3000.0, "active_hours": 12.0, "repeat": 8.0}


# --- show --slots ----------------------------------------------------------------------------------------------------

def test_show_slots_defaults_file_and_unusable(store):
    code, text = _run(["show", "--slots"], store)
    assert code == 0 and "defaults (no file)" in text and "claude_code: calls 3,000, active hours 12, repeat 8" in text
    _put(store, {"format": L.LIMITS_FORMAT, "codex": {"calls": 7}})
    code, text = _run(["show", "--slots", "--agent", "codex"], store)
    assert code == 0 and "codex: calls 7, active hours 12, repeat 8" in text and "claude_code" not in text
    assert str(store / "policy" / "limits") in text
    code, text = _run(["show", "--slots", "--json"], store)
    assert code == 0
    assert json.loads(text) == {"source": str(store / "policy" / "limits"),
                                "limits": {"claude_code": {"calls": 3000, "active_hours": 12, "repeat": 8},
                                           "codex": {"calls": 7, "active_hours": 12, "repeat": 8}}}
    os.chmod(store / "policy" / "limits", 0o666)
    code, text = _run(["show", "--slots"], store)
    assert code == 1 and "is not usable" in text and "every slot tool call" in text


def test_an_unusable_file_is_said_once_by_the_command_never_logged_as_the_broker(store, caplog):
    import logging

    (store / "policy" / "limits").write_bytes(b"garbage")
    os.chmod(store / "policy" / "limits", 0o644)
    caplog.set_level(logging.DEBUG)
    for argv in (["show", "--slots"], ["set", "--slots", "default", "calls", "5"]):
        code, text = _run(argv, store, Helper(store))
        assert code == 1 and text.count("is not usable") == 1
    assert not [r for r in caplog.records if r.name.startswith("levain.qbroker")]
    assert logging.getLogger("levain.qbroker").disabled is False        # restored


# --- the real root call, and the command text the messages print --------------------------------------------------

def test_the_default_helper_runs_the_install_helper_through_sudo_by_absolute_path(monkeypatch, tmp_path):
    host = "linux" if os.uname().sysname != "Darwin" else "darwin"
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, b'{"ok":true}\n', b"")

    monkeypatch.setattr(C.subprocess, "run", fake_run)
    monkeypatch.setattr(C.os.path, "isfile", lambda p: True)
    assert C._sudo_set_limits(host, b"{}") == (0, '{"ok":true}\n', "")
    assert seen["argv"] == ["/usr/bin/sudo", str(layout.install_helper_path(host)), "set-limits"]
    assert seen["kw"]["input"] == b"{}" and seen["kw"].get("shell") is not True
    monkeypatch.setattr(C.os.path, "isfile", lambda p: False)
    rc, out, err = C._sudo_set_limits(host, b"{}")
    assert rc != 0 and out == "" and str(layout.install_helper_path(host)) in err


def test_the_command_the_broker_and_doctor_name_parses():
    """The agent text, the broker's reason and doctor's hint print ``levain limits set --slots <agent|default>
    <kind> <value>``: that exact shape parses."""
    parser = argparse.ArgumentParser()
    C.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["limits", "set", "--slots", "default", "calls", "5"])
    assert (args.slots, args.kind, args.value) == ("default", "calls", "5")
    for agent in L.AGENTS:
        assert parser.parse_args(["limits", "unset", "--slots", agent, "repeat"]).slots == agent
    from levain import doctor
    from levain.consent import broker_client
    from levain.qbroker import limits as QL
    for text in (Path(doctor.__file__).read_text(), Path(broker_client.__file__).read_text(),
                 Path(QL.__file__).read_text()):
        assert "levain limits set --slots" in text
