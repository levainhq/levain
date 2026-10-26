"""`levain consent away`: away mode set through the CLI is what the adapters' own load_settings reads."""
from __future__ import annotations

import io
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
from tests._h4 import h4_home


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
    assert "claude_code: away mode ON (signed to leave), holds up to 120s" in capsys.readouterr().out
    assert main(["consent", "away", "off", "--agent", "codex", "--signing-key", str(_key(qdir)),
                 "--queue-dir", str(qdir)]) == 0
    assert cx.load_settings(qdir).away_mode is False and cc.load_settings(qdir).away_mode is True


def _key(qdir: Path) -> Path:
    """The laptop key the queue's cockpit enrolled (:func:`_cockpit`)."""
    return Path(json.loads((qdir / cc.SETTINGS_NAME).read_text())["cockpit_data_root"]).parent / "laptop"


def test_off_without_a_signature_leaves_an_away_on_standing(qdir, capsys):
    """AWAY-SIGN run (i), reproduced on the code before it: `away off` after `away on`, unsigned, exited 0 and
    turned away mode off. It is refused now, and away stays on for the hook; a signed off leaves it."""
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert main(["consent", "away", "off", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    assert "leaving it takes a signature" in capsys.readouterr().err
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text())["away_word"] == "on"   # the word was not written
    assert cc.effective_away(away.settings(qdir, "claude_code"))[0] is True
    assert main(["consent", "away", "off", "--agent", "claude_code", "--signing-key", str(_key(qdir)),
                 "--queue-dir", str(qdir)]) == 0
    assert cc.effective_away(away.settings(qdir, "claude_code"))[0] is False


def test_a_hand_rewritten_off_does_not_end_a_marked_away_for_the_hook(qdir, tmp_path):
    """AWAY-SIGN run (ii), reproduced on the code before it: after `away on`, the settings file rewritten by hand
    to away_word off let a gated PreToolUse pass unheld. The hook reads the away mark in the store and holds."""
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    path = qdir / cc.SETTINGS_NAME
    data = json.loads(path.read_text())
    data.update(away_word="off", away_mode=False, away_hold_seconds=0, gate_hold_seconds=0.5)
    path.write_text(json.dumps(data))
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    event = {"hook_event_name": "PreToolUse", "session_id": "S-ii", "cwd": str(ws), "permission_mode": "default",
             "tool_name": "Edit", "tool_use_id": "tu-ii",
             "tool_input": {"file_path": str(ws / ".claude" / "settings.json"), "old_string": "a", "new_string": "b"}}
    out = io.StringIO()
    assert cc.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
    assert "UNDECIDED" in out.getvalue()                       # held to D and marked undecided, never passed
    assert cc.still_away_from(path, cc.load_settings(qdir))()[0] is True




def test_a_hand_rewritten_off_does_not_end_a_marked_away_for_the_codex_hook(qdir, tmp_path):
    """AWAY-SIGN L1 #8, reproduced with the away-mark read taken out of the Codex hook's main: after `away on`, the
    settings file rewritten by hand to away_word off let a gated Codex PreToolUse pass unheld. It holds now."""
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 0
    path = qdir / cx.SETTINGS_NAME
    path.write_text(json.dumps({**json.loads(path.read_text()), "away_word": "off", "away_mode": False,
                                "away_hold_seconds": 0, "gate_hold_seconds": 0.5}))
    ws = tmp_path / "ws"
    (ws / ".codex").mkdir(parents=True)
    event = {"hook_event_name": "PreToolUse", "session_id": "S-cx", "turn_id": "t1", "transcript_path": None,
             "cwd": str(ws), "model": "m", "permission_mode": "default", "tool_name": "apply_patch",
             "tool_input": {"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"},
             "tool_use_id": "call-cx"}
    out = io.StringIO()
    assert cx.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
    assert "UNDECIDED" in out.getvalue()                       # held to D and marked undecided, never passed


def test_a_replaced_mark_row_never_re_latests_a_left_mark(qdir):
    """AWAY-SIGN L1 #1, reproduced on the code before it: after on / signed off / on again and the settings
    file rewritten to off, a raw `INSERT OR REPLACE` of the first (left) mark's id re-ordered it latest, and its stored leave read the agent as
    not away; so did a REPLACE onto the standing mark's rowid with another agent's row. Both are refused now."""
    key = str(_key(qdir))
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert main(["consent", "away", "off", "--agent", "claude_code", "--signing-key", key,
                 "--queue-dir", str(qdir)]) == 0
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    path = qdir / cc.SETTINGS_NAME
    path.write_text(json.dumps({**json.loads(path.read_text()), "away_word": "off", "away_mode": False}))
    conn = sqlite3.connect(qdir / "queue.db", isolation_level=None)
    (first, first_at), (standing,) = (conn.execute("SELECT id, set_at FROM away_marks ORDER BY rowid").fetchone(),
                                      conn.execute("SELECT rowid FROM away_marks ORDER BY rowid DESC").fetchone())
    for stmt, args in (("INSERT OR REPLACE INTO away_marks (id, agent, set_at) VALUES (?, ?, ?)",
                        (first, "claude_code", first_at)),
                       ("INSERT OR REPLACE INTO away_marks (rowid, id, agent, set_at) VALUES (?, ?, ?, ?)",
                        (standing, "am-other", "codex", first_at))):
        try:
            conn.execute(stmt, args)
        except sqlite3.IntegrityError:
            pass
        assert cc.effective_away(away.settings(qdir, "claude_code"))[0] is True
    conn.close()


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
    path.write_text("[" * 20000 + "]" * 20000)  # show: what the hook reads (defaults); on: refused
    assert main(["consent", "away", "--agent", "codex", "--queue-dir", str(qdir)]) == 0
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
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


@pytest.mark.parametrize("action", ["show", "off"])
def test_a_huge_integer_hold_reads_as_malformed_everywhere(qdir, cockpit, action, capsys):
    """L3 r2 (complement + codex): show/off reached the adapters' math.isfinite on a 400-digit int (OverflowError)."""
    (qdir / cc.SETTINGS_NAME).write_text(f'{{"cockpit_data_root": "{cockpit}", "away_mode": true, '
                                         f'"away_hold_seconds": {"9" * 400}}}')
    assert cc.load_settings(qdir).away_hold_seconds == 0.0  # the hook's own reader: malformed -> 0
    assert main(["consent", "away", action, "--queue-dir", str(qdir)]) == 0
    assert "codex: away mode off" in capsys.readouterr().out  # --agent all went on past claude_code


def test_both_hooks_and_the_verb_share_one_reader(qdir):
    (qdir / cx.SETTINGS_NAME).write_text("[" * 20000 + "]" * 20000)
    assert cx.load_settings(qdir) == cc.AdapterSettings()  # deep nesting -> defaults, no RecursionError


def test_a_huge_integer_hold_from_a_library_caller_is_a_value_error(qdir):
    with pytest.raises(ValueError):
        away.set_away(qdir, "claude_code", True, int("9" * 400))


def test_a_verifier_that_cannot_run_is_a_refusal_not_a_traceback(qdir, monkeypatch, capsys):
    """Fix r2 (codex MED), reproduced on 00152acb: with a mark standing, `off --signing-key` while ssh-keygen could
    not fingerprint the key raised VerifierUnavailable out of the CLI. It is the normal refusal now."""
    from levain.authority import VerifierUnavailable
    from levain.cockpit import keys

    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0

    def no_verifier(*_a, **_k):
        raise VerifierUnavailable("ssh-keygen could not fingerprint the key (FileNotFoundError)")
    monkeypatch.setattr(keys, "fingerprint_of", no_verifier)
    assert main(["consent", "away", "off", "--agent", "claude_code", "--signing-key", str(_key(qdir)),
                 "--queue-dir", str(qdir)]) == 1
    assert "away mode not left: ssh-keygen could not fingerprint" in capsys.readouterr().err
    assert cc.effective_away(away.settings(qdir, "claude_code"))[0] is True


@pytest.mark.parametrize("damage", ["drop_leaves", "foreign"])
def test_a_damaged_or_foreign_store_is_unreadable_never_no_mark(qdir, damage, capsys):
    """Fix r2 (codex MED), reproduced on 00152acb: a current-format store that lost away_leaves, and an unrelated
    SQLite file, read as "no mark", so an unsigned `off` exited 0. Both are unreadable now, and say what that costs."""
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    db = qdir / "queue.db"
    if damage == "foreign":
        db.unlink()
    conn = sqlite3.connect(db, isolation_level=None)
    if damage == "foreign":
        conn.execute("CREATE TABLE notes (body TEXT)")
    else:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND sql LIKE "
                                    "'%away_leaves%'").fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE away_leaves")
    conn.close()
    capsys.readouterr()
    assert main(["consent", "away", "off", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    err = capsys.readouterr().err
    assert "consent store unreadable at" in err and "neither hold nor file" in err and "move it aside" in err
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text())["away_word"] == "on"   # nothing written
    assert main(["consent", "away", "show", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    assert "neither hold nor file requests" in capsys.readouterr().out


def _drop(db: Path, *tables: str) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    for table in tables:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                    (table,)).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"DROP TABLE {table}")
    conn.close()


def test_a_store_from_before_away_marks_has_no_mark(qdir, capsys):
    """Fix r3 (complement + codex MED), reproduced on 23a1458e: a current-format store made before the away
    tables (neither one there) made `show` say the store is unreadable. It has no mark, and the queue adds them."""
    assert main(["consent", "away", "auto", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    cc._queue_for(qdir, away.settings(qdir, "claude_code"), home=h4_home()).queue_id   # the store, in its current format
    _drop(qdir / "queue.db", "away_marks", "away_leaves")
    capsys.readouterr()
    assert main(["consent", "away", "show", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert "unreadable" not in capsys.readouterr().out
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert away.settings(qdir, "claude_code").away_marked


def test_the_hook_never_recreates_a_lost_away_table(qdir):
    """Fix r3 (complement + codex MED), reproduced on 23a1458e: a current-format store that lost away_marks read
    as unreadable to `show`, but the hook's own queue recreated the table empty and did not hold under the mark."""
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    _drop(qdir / "queue.db", "away_marks")
    started = cc.settings_from(json.loads((qdir / cc.SETTINGS_NAME).read_text()))
    got = cc.with_away_mark(started, qdir, "claude_code", queue=cc._queue_for(qdir, started, home=h4_home()))
    assert got.away_marked and "consent store unreadable at" in got.away_unreadable
    conn = sqlite3.connect(qdir / "queue.db")
    assert "away_marks" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
