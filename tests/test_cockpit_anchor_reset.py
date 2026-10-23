"""Custody Q2 (1022+19 custody_fix RETURN, head ruling "FIX in a later lane"): a named remedy is a command the
operator can run. The anchor errors named `levain cockpit anchor reset` (trust-tiering DESIGN §3.8) and no such
command existed, so the remedy printed at the one moment the operator needs it was a dead end. These rows hold
the command to §3.8 (a laptop-presence signature over ``{store_id, old_anchor, new_head}``, verified against the
log's own keys as of its head, stored in the anchor file; refused while a cockpit server holds the store) and
hold every printed ``levain ...`` remedy to the argparse tree that has to accept it."""
from __future__ import annotations

import ast
import contextlib
import fcntl
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import levain
from levain import authority as auth
from levain.authority import anchor_path
from levain.cockpit.firejournal import CockpitDB
from tests.cockpit_authority_rig import RealKey, enrol

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
LEVAIN = Path(levain.__file__).parent


@pytest.fixture(autouse=True)
def _no_governed_marker(tmp_path: Path, monkeypatch) -> None:
    from levain.cockpit import install

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")   # no real governed marker dir


def _restored(tmp_path: Path) -> tuple[Path, RealKey, RealKey]:
    """A reported journal whose log holds a laptop genesis and a phone enrolment, the anchor at seq 2, then the
    phone's link rolled back as a restore of an older copy would: the log sits behind its anchor."""
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop, phone = RealKey(tmp_path, "laptop"), RealKey(tmp_path, "phone")
    db = CockpitDB(root)
    enrol(db, laptop, "laptop-presence", now=NOW)
    enrol(db, phone, "display-phone", by=laptop, now=NOW)
    assert auth.read_anchor(anchor_path(root)).seq == 2
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    conn.execute("DELETE FROM authority_log WHERE seq = 2")
    conn.close()
    return root, laptop, phone


def _run(*argv: str) -> tuple[int, str, str]:
    from levain import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


_TICK = re.compile(r"`(levain [^`]+)`")


def _hinted_argv(message: str) -> list[str]:
    """The `levain ...` argv a refusal names, its placeholder words left in."""
    m = _TICK.search(message)
    assert m is not None, message
    return m.group(1).split()[1:]


# --- the command, as §3.8 specifies it ------------------------------------------------------------------

def test_the_printed_remedy_runs_and_reopens_a_restored_store(tmp_path: Path) -> None:
    """Fail-first: before this lane `levain cockpit anchor reset` was printed by every anchor refusal and argparse
    refused it (`invalid choice: 'anchor'`). The refusal's own argv, its placeholder filled, now resets."""
    root, laptop, _ = _restored(tmp_path)
    rc, _, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 1 and "behind its anchor" in err
    argv = _hinted_argv(re.sub(r"<[^>]*>", str(laptop.path), err))
    assert argv[:3] == ["cockpit", "anchor", "reset"] and "--signing-key" in argv
    rc, out, err = _run(*argv, "--data-root", str(root))
    assert rc == 0, err
    assert "anchor reset" in out and "seq 1" in out
    stored = json.loads(anchor_path(root).read_text())
    assert stored["seq"] == 1 and stored["reset"]["signer_fp"] == laptop.fingerprint
    assert stored["reset"]["old_anchor"]["seq"] == 2              # the proof names what it replaced
    rc, out, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 0 and laptop.fingerprint in out, err


def test_a_reset_is_signed_only_by_an_unrevoked_laptop_key_of_the_log(tmp_path: Path) -> None:
    root, laptop, phone = _restored(tmp_path)
    before = anchor_path(root).read_bytes()
    # the phone's enrolment is the link the restore rolled back: the log as it stands does not hold it
    rc, _, err = _run("cockpit", "anchor", "reset", "--signing-key", str(phone.path), "--data-root", str(root))
    assert rc == 1 and "not an unrevoked key of this log" in err
    stranger = RealKey(tmp_path, "stranger")
    rc, _, err = _run("cockpit", "anchor", "reset", "--signing-key", str(stranger.path), "--data-root", str(root))
    assert rc == 1 and "not an unrevoked key of this log" in err
    assert anchor_path(root).read_bytes() == before


def test_a_reset_refuses_while_a_cockpit_server_holds_the_store(tmp_path: Path) -> None:
    from levain.cockpit.broker import LOCK_NAME

    root, laptop, _ = _restored(tmp_path)
    before = anchor_path(root).read_bytes()
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)            # as the broker holds it
        rc, _, err = _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path), "--data-root", str(root))
    finally:
        os.close(fd)
    assert rc == 1 and "cockpit server holds" in err and "stop it" in err
    assert anchor_path(root).read_bytes() == before


def test_an_unreadable_anchor_is_reset_and_its_bytes_are_named(tmp_path: Path) -> None:
    root, laptop, _ = _restored(tmp_path)
    anchor_path(root).write_bytes(b"not an anchor")
    rc, _, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 1 and "unreadable" in err and "levain cockpit anchor reset" in err
    rc, out, err = _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path), "--data-root", str(root))
    assert rc == 0, err
    assert "sha256" in json.loads(anchor_path(root).read_text())["reset"]["old_anchor"]


def test_an_anchor_that_agrees_with_the_log_signs_nothing(tmp_path: Path) -> None:
    root, laptop, _ = _restored(tmp_path)
    assert _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path), "--data-root", str(root))[0] == 0
    after = anchor_path(root).read_bytes()
    stranger = RealKey(tmp_path, "stranger")      # a key that could not sign: nothing is asked of it
    rc, out, _ = _run("cockpit", "anchor", "reset", "--signing-key", str(stranger.path), "--data-root", str(root))
    assert rc == 0 and "nothing to reset" in out
    assert anchor_path(root).read_bytes() == after


def test_an_unopenable_store_lock_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    """S3a-2 MUST (DESIGN §S3a-2): an OSError opening ``cockpit.lock`` surfaces as AnchorError."""
    from levain.cockpit.broker import LOCK_NAME

    root, laptop, _ = _restored(tmp_path)
    (root / LOCK_NAME).mkdir()                    # a directory cannot be opened O_RDWR
    with pytest.raises(auth.AnchorError, match="cannot be opened"):
        with auth._no_live_cockpit(anchor_path(root)):
            pass


def test_an_empty_log_beside_an_anchor_names_a_remedy_that_runs_today(tmp_path: Path) -> None:
    """An empty log has no keys, so no reset can verify against it: the remedy is the anchor moved aside and
    `levain cockpit ledger init`, both of which exist."""
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp_path, "laptop")
    enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    conn.execute("DELETE FROM authority_log")
    conn.close()
    with pytest.raises(auth.AnchorError) as exc:
        enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    msg = str(exc.value)
    assert "anchor reset" not in msg
    assert str(anchor_path(root)) in msg and "`levain cockpit ledger init`" in msg
    # the read path (RequestAuthority.capture) names the same remedy, and so does the reset command itself
    for argv in (("cockpit", "keys"), ("cockpit", "anchor", "reset", "--signing-key", str(laptop.path))):
        rc, _, err = _run(*argv, "--data-root", str(root))
        assert rc == 1 and str(anchor_path(root)) in err and "`levain cockpit ledger init`" in err, err


# --- every printed remedy is an argv the CLI accepts --------------------------------------------------------

def _strings(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            yield "".join(str(v.value) if isinstance(v, ast.Constant) else "{x}" for v in node.values)


def _help(words: tuple[str, ...], cache: dict) -> tuple[int, str]:
    from levain import cli

    if words not in cache:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main([*words, "--help"])
                code = 0
            except SystemExit as e:
                code = e.code or 0
        cache[words] = (code, out.getvalue() + err.getvalue())
    return cache[words]


def _verdict(cmd: str, cache: dict) -> str | None:
    """None when ``cmd`` (a `levain ...` remedy) parses: its words are a command path (a trailing word may be a
    listed choice or a positional), and every --flag after it is in that command's help."""
    toks = cmd.split()[1:]
    if toks and toks[0].endswith(":"):
        return None                               # a message's own prefix (`levain run: ...`), not a command
    words: list[str] = []
    for t in toks:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", t):
            break
        words.append(t)
    depth = len(words)
    while depth and _help(tuple(words[:depth]), cache)[0] != 0:
        depth -= 1
    if words and not depth:
        return f"`levain {words[0]}` is no command"
    text = _help(tuple(words[:depth]), cache)[1]
    if depth < len(words):
        usage = text.split("\n\n")[0]
        choices = {c for grp in re.findall(r"\{([^}]*)\}", usage) for c in grp.split(",")}
        if choices and words[depth] not in choices:
            return f"`{words[depth]}` is not a command of `levain {' '.join(words[:depth])}`"
    for t in toks[len(words):]:
        if t.startswith("--") and t.split("=")[0].rstrip(",;)") not in text:
            return f"`levain {' '.join(words[:depth])}` has no {t}"
    return None


# a future command named AS ABSENT is not a remedy offered to run: it may stay only beside these words
_NAMED_AS_ABSENT = {"levain cockpit ledger re-genesis": "is not in this build"}


def test_every_printed_levain_remedy_names_an_argv_the_cli_accepts() -> None:
    """Fail-first: before this lane the sweep found `levain cockpit anchor reset` (authority.py, every anchor
    refusal) and `levain dispatch --dispatch-dir` (the phone command's --queue-dir help; the flag is
    `levain dispatch file`'s) NOT-REAL. Derive the full table:
    ``python project_memory/1022-20/anchorq2/sweep.py <worktree>``."""
    files = [LEVAIN / "authority.py", LEVAIN / "doctor.py", LEVAIN / "cli.py", *sorted((LEVAIN / "cockpit").glob("*.py"))]
    cache: dict = {}
    bad = []
    for f in files:
        for s in _strings(f):
            for m in _TICK.finditer(s):
                cmd = m.group(1).strip()
                if cmd in _NAMED_AS_ABSENT:
                    if _NAMED_AS_ABSENT[cmd] not in s:
                        bad.append(f"{f.name}: `{cmd}` named without '{_NAMED_AS_ABSENT[cmd]}'")
                    continue
                why = _verdict(cmd, cache)
                if why is not None:
                    bad.append(f"{f.name}: `{cmd}`: {why}")
    assert not bad, "\n".join(bad)


def test_the_absent_command_is_still_absent() -> None:
    """When `ledger re-genesis` lands, its doctor rows must name it as a command to run, not as missing."""
    assert _verdict("levain cockpit ledger re-genesis", {}) is not None
