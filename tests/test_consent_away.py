"""`levain consent away`: away mode set through the CLI is what the adapters' own load_settings reads."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cli import main
from levain.cockpit.firejournal import CockpitDB
from levain.consent import away
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx


def _cockpit(tmp_path: Path, enrol: bool = True) -> Path:
    """A real cockpit data root; with ``enrol``, one laptop key enrolled in its signed authority log."""
    root = tmp_path / "cockpit"
    root.mkdir()
    with CockpitDB(root).read():
        pass
    if enrol:
        key = tmp_path / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        pub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
        fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], check=True, capture_output=True,
                            text=True).stdout.split()[1]
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        auth.sign_and_append(conn, {"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                                    "custody": "passphrase"}, signer_fp=fp,
                             sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n",
                                                            auth.NAMESPACE], input=m, capture_output=True,
                                                           check=True).stdout.decode(),
                             clock=lambda: datetime.now(UTC), cache=ReplayCache(), anchor=None)
        conn.close()
    return Path(os.path.realpath(root))


def _queue(tmp_path: Path, root: object) -> Path:
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    os.chmod(q, 0o700)
    for name in (cc.SETTINGS_NAME, cx.SETTINGS_NAME):
        (q / name).write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                          "away_hold_seconds": 0, "other": 1}))
    return q


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory):
    return _cockpit(tmp_path_factory.mktemp("ck"))


@pytest.fixture
def qdir(tmp_path, cockpit):
    return _queue(tmp_path, cockpit)


def test_on_then_off_is_what_both_adapters_read(qdir, capsys):
    assert main(["consent", "away", "on", "--hold", "120", "--queue-dir", str(qdir)]) == 0
    for mod in (cc, cx):
        s = mod.load_settings(qdir)
        assert s.away_mode is True and s.away_hold_seconds == 120.0
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text())["other"] == 1  # other keys kept
    assert oct((qdir / cc.SETTINGS_NAME).stat().st_mode & 0o777) == "0o600"
    assert "claude_code: away mode ON, holds up to 120s" in capsys.readouterr().out
    assert main(["consent", "away", "off", "--agent", "codex", "--queue-dir", str(qdir)]) == 0
    assert cx.load_settings(qdir).away_mode is False and cc.load_settings(qdir).away_mode is True


def test_on_without_hold_uses_the_default_and_show_writes_nothing(qdir, capsys):
    before = (qdir / cc.SETTINGS_NAME).read_bytes()
    assert main(["consent", "away", "--queue-dir", str(qdir)]) == 0
    assert (qdir / cc.SETTINGS_NAME).read_bytes() == before
    assert "away mode off" in capsys.readouterr().out
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert cc.load_settings(qdir).away_hold_seconds == away.DEFAULT_HOLDS["claude_code"]
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 0
    assert cx.load_settings(qdir).away_hold_seconds == away.DEFAULT_HOLDS["codex"]


@pytest.mark.parametrize("hold", ["0", "-1", "841", "900", "nan", "inf"])
def test_a_hold_outside_the_hooks_deadline_is_refused(qdir, hold):
    before = (qdir / cc.SETTINGS_NAME).read_bytes()
    assert main(["consent", "away", "on", "--agent", "claude_code", "--hold", hold, "--queue-dir", str(qdir)]) == 1
    assert (qdir / cc.SETTINGS_NAME).read_bytes() == before


def test_a_missing_file_is_never_created(qdir):
    (qdir / cx.SETTINGS_NAME).unlink()
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    assert not (qdir / cx.SETTINGS_NAME).exists()
    assert main(["consent", "away", "on", "--queue-dir", str(qdir)]) == 0  # all: the agent with a file
    assert not (qdir / cx.SETTINGS_NAME).exists() and cc.load_settings(qdir).away_mode is True


def test_a_symlinked_file_or_an_open_queue_dir_is_refused(qdir, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (qdir / cx.SETTINGS_NAME).unlink()
    (qdir / cx.SETTINGS_NAME).symlink_to(target)
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    assert target.read_text() == "{}"
    os.chmod(qdir, 0o755)
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    assert cc.load_settings(qdir).away_mode is False


def test_no_queue_directory_is_an_error(tmp_path):
    assert main(["consent", "away", "on", "--queue-dir", str(tmp_path / "nope")]) == 1
    assert not (tmp_path / "nope").exists()


def test_the_longest_hold_is_the_longest_the_hook_waits(qdir, capsys):
    """L1/L2 on 22fc206: 900 was accepted while the hook stops waiting CLEANUP_RESERVE before its 900 s deadline."""
    assert away.MAX_HOLD == cc.DEFAULT_MAX_HOLD - cc.CLEANUP_RESERVE
    assert main(["consent", "away", "on", "--agent", "claude_code", "--hold", "840", "--queue-dir", str(qdir)]) == 0
    assert "holds up to 840s" in capsys.readouterr().out


@pytest.mark.parametrize("root", ["", "relative/ck", "missing"])
def test_on_refuses_when_no_enrolled_key_could_sign(tmp_path, root):
    """L2 on 22fc206: away mode with nothing that can sign only stalls the agent (Codex shows no dialog)."""
    if root == "missing":
        root = str(tmp_path / "nowhere")
    q = _queue(tmp_path, root)
    assert main(["consent", "away", "on", "--queue-dir", str(q)]) == 1
    assert cc.load_settings(q).away_mode is False and cx.load_settings(q).away_mode is False


def test_on_refuses_a_cockpit_with_no_enrolled_key(tmp_path, capsys):
    q = _queue(tmp_path, _cockpit(tmp_path, enrol=False))
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(q)]) == 1
    assert "away mode not turned on" in capsys.readouterr().err


def test_show_reports_what_the_hook_reads_and_names_a_skipped_agent(tmp_path, capsys):
    """L1 on 22fc206: show printed the raw file (a relative root as if it verified); --agent all hid a skip."""
    q = _queue(tmp_path, "relative/ck")
    (q / cx.SETTINGS_NAME).unlink()
    assert main(["consent", "away", "--queue-dir", str(q)]) == 0
    out = capsys.readouterr().out
    assert "claude_code: away mode off; WARN: the settings name no cockpit_data_root" in out
    assert "codex: skipped, no settings file" in out


def test_a_fifo_or_oversized_or_deep_file_is_refused_without_blocking(qdir):
    """L3 r1 codex: read_bytes() before the size check, a FIFO blocked, deep nesting raised RecursionError."""
    path = qdir / cx.SETTINGS_NAME
    path.unlink()
    os.mkfifo(path)
    assert main(["consent", "away", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    path.unlink()
    path.write_text("[" * 20000 + "]" * 20000)
    assert main(["consent", "away", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    path.write_text(json.dumps({"x": "a" * (70 * 1024)}))
    assert main(["consent", "away", "--agent", "codex", "--queue-dir", str(qdir)]) == 1


def test_a_huge_integer_hold_falls_back_to_the_default(qdir, cockpit):
    (qdir / cc.SETTINGS_NAME).write_text(f'{{"cockpit_data_root": "{cockpit}", "away_hold_seconds": {"9" * 400}}}')
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert cc.load_settings(qdir).away_hold_seconds == away.DEFAULT_HOLDS["claude_code"]


def test_a_symlinked_or_open_queue_dir_is_refused_for_show_too(qdir, tmp_path):
    link = tmp_path / "qlink"
    link.symlink_to(qdir)
    assert main(["consent", "away", "--queue-dir", str(link)]) == 1
    assert main(["consent", "away", "on", "--queue-dir", str(link)]) == 1
    os.chmod(qdir, 0o755)
    assert main(["consent", "away", "--queue-dir", str(qdir)]) == 1


def test_a_file_changed_mid_write_is_not_overwritten(qdir, monkeypatch):
    """L3 r1 complement + codex: read-modify-replace could write a stale snapshot over a newer file."""
    real = away.why_unanswerable

    def swap(s):
        (qdir / cc.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": "/newer", "x": 2}))
        return real(s)
    monkeypatch.setattr(away, "why_unanswerable", swap)
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text()) == {"cockpit_data_root": "/newer", "x": 2}
