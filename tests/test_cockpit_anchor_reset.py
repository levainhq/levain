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


def _hinted_argv(message: str, key: Path | None = None) -> list[str]:
    """The `levain ...` argv a refusal names; with ``key``, its one placeholder word (the operator's key file)
    filled in."""
    m = _TICK.search(message)
    assert m is not None, message
    argv = m.group(1).split()[1:]
    return argv if key is None else [str(key) if w == auth.KEY_WORD else w for w in argv]


# --- the command, as §3.8 specifies it ------------------------------------------------------------------

def test_the_printed_remedy_runs_and_reopens_a_restored_store(tmp_path: Path) -> None:
    """Fail-first: before this lane `levain cockpit anchor reset` was printed by every anchor refusal and argparse
    refused it (`invalid choice: 'anchor'`). The refusal's own argv, its placeholder filled, now resets."""
    root, laptop, _ = _restored(tmp_path)
    rc, _, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 1 and "behind its anchor" in err
    argv = _hinted_argv(err, laptop.path)
    assert argv == ["cockpit", "anchor", "reset", "--signing-key", str(laptop.path), "--data-root", str(root)]
    rc, out, err = _run(*argv)                                    # as printed, nothing added
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
    init = auth.init_hint(root)
    assert str(anchor_path(root)) in msg and init in msg
    # the read path (RequestAuthority.capture) names the same remedy, and so does the reset command itself
    for argv in (("cockpit", "keys"), ("cockpit", "anchor", "reset", "--signing-key", str(laptop.path))):
        rc, _, err = _run(*argv, "--data-root", str(root))
        assert rc == 1 and str(anchor_path(root)) in err and init in err, err


# --- 1022+20 L3 r1 (FIX1): everything that can refuse is judged before the touch -----------------------------------

class _Recorder:
    """A ``sign`` that records each message it is asked for, then signs as ``key`` (``during`` runs first, inside
    the touch, as whatever else the operator's machine does while they hold the key)."""

    def __init__(self, key: RealKey, during=None) -> None:
        self.key, self.during, self.asked = key, during, []

    def __call__(self, message: bytes) -> str:
        self.asked.append(message)
        if self.during is not None:
            self.during()
        return self.key.sign(message, auth.RESET_NAMESPACE)


def _reset(root: Path, signer_fp: str, sign) -> tuple:
    from levain.cockpit.keys import anchor_reset

    return anchor_reset(root, signer_fp=signer_fp, sign=sign)


def _foreign(root: Path) -> bytes:
    """The anchor replaced by another store's (well-formed, at the seq the log is behind)."""
    anchor_path(root).write_text(json.dumps({"format": auth.ANCHOR_FORMAT, "store_id": "another-store", "seq": 2,
                                             "head_hash": "ab" * 32}))
    return anchor_path(root).read_bytes()


def test_an_anchor_of_another_store_is_refused_before_the_touch_and_names_the_file(tmp_path: Path) -> None:
    """codex HIGH (L3 r1): `check_anchor` refused a store-id mismatch, then the reset took it as a rollback and
    signed over it with this log's own key, so a substituted log adopted the anchor that exists to refuse it. Now
    it is refused by name before anything is signed, and its remedy is the file to check or move (§3.8: a reset
    is for a rollback of the same store)."""
    from levain.cockpit.keys import KeyError_

    root, laptop, _ = _restored(tmp_path)
    before = _foreign(root)
    sign = _Recorder(laptop)
    with pytest.raises(KeyError_) as ei:
        _reset(root, laptop.fingerprint, sign)
    msg = str(ei.value)
    assert sign.asked == [] and anchor_path(root).read_bytes() == before
    assert "another-store" in msg and "another store" in msg and str(anchor_path(root)) in msg, msg
    assert "levain cockpit anchor reset" not in msg and "move" in msg and "nothing signed" in msg, msg
    # the open names the same remedy, not a reset
    rc, _, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 1 and "another store" in err and "levain cockpit anchor reset" not in err, err
    # and the remedy is true: the anchor moved aside, the store opens and its next link writes its own anchor
    anchor_path(root).rename(anchor_path(root).with_name("foreign.anchor"))
    rc, out, err = _run("cockpit", "keys", "--data-root", str(root))
    assert rc == 0 and laptop.fingerprint in out, err


def test_the_library_reset_refuses_another_stores_anchor_whatever_the_signature(tmp_path: Path) -> None:
    """The backstop under the CLI: ``reset_anchor`` re-reads the anchor under the lock and refuses a foreign one,
    a signature over exactly that anchor included."""
    from levain.cockpit.authority import RequestAuthority

    root, laptop, _ = _restored(tmp_path)
    before = _foreign(root)
    with CockpitDB(root).read() as conn:
        snap = RequestAuthority.capture(conn, now=NOW, anchor=None, source="t", cache=auth.ReplayCache()).authority
    old = auth._old_anchor_view(anchor_path(root))
    sig = laptop.sign(auth.reset_message(snap.store_id, old, snap.seq, snap.head_hash), auth.RESET_NAMESPACE)
    with pytest.raises(auth.ForeignAnchor, match="another store"):
        auth.reset_anchor(anchor_path(root), snap, signer_fp=laptop.fingerprint, signature=sig)
    assert anchor_path(root).read_bytes() == before


def test_a_quarantined_laptop_key_is_refused_before_it_is_asked_for_a_touch(tmp_path: Path) -> None:
    """codex MED + complement LOW (L3 r1): an unrevoked laptop-presence key that is QUARANTINED (WebAuthn material
    at custody fido, from a log written before the enrol door) was asked to sign, then refused by the verify.
    The pre-sign gate is now ``snap.signing_laptop_keys()``, the one usable-key predicate."""
    from types import SimpleNamespace

    from levain.cockpit.keys import KeyError_
    from tests.test_passkey_custody_derived import WebAuthnKey, pre_door

    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop, pk = RealKey(tmp_path, "laptop"), WebAuthnKey()
    enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    with pre_door():                              # written by a levain from before the enrol door
        enrol(CockpitDB(root), SimpleNamespace(fingerprint=pk.fp, public=pk.public_key), "laptop-presence",
              by=laptop, now=NOW, custody="fido")
    enrol(CockpitDB(root), RealKey(tmp_path, "phone"), "display-phone", by=laptop, now=NOW)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    conn.execute("DELETE FROM authority_log WHERE seq = 3")      # the log (laptop, pk) sits behind the anchor
    conn.close()
    before = anchor_path(root).read_bytes()
    asked = []
    with pytest.raises(KeyError_) as ei:
        _reset(root, pk.fp, lambda m: asked.append(m) or pk.sign(m, auth.RESET_NAMESPACE))
    assert asked == [] and anchor_path(root).read_bytes() == before
    assert "quarantined" in str(ei.value) and "nothing signed" in str(ei.value), ei.value
    # the control: the laptop key that can sign resets the same store
    assert _reset(root, laptop.fingerprint, _Recorder(laptop))[1].seq == 2


def test_the_store_lock_is_held_through_the_touch(tmp_path: Path) -> None:
    """codex LOW (L3 r1): the live-server check released ``cockpit.lock`` before the signature, so a server that
    started during the touch made the touch wasted. The reset lock is now held from the old anchor's read through
    the write: inside the touch neither the store lock nor the anchor's lock can be taken."""
    from levain.cockpit.broker import LOCK_NAME

    root, laptop, _ = _restored(tmp_path)
    tried = {}

    def contend() -> None:
        for name, lock in (("store", root / LOCK_NAME), ("anchor", anchor_path(root).with_name(auth.ANCHOR_NAME + ".lock"))):
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                tried[name] = "taken"
            except BlockingIOError:
                tried[name] = "held"
            finally:
                os.close(fd)

    _, new = _reset(root, laptop.fingerprint, _Recorder(laptop, during=contend))
    assert tried == {"store": "held", "anchor": "held"} and new.seq == 1


def test_a_link_appended_during_the_touch_refuses_the_reset(tmp_path: Path) -> None:
    """complement LOW (L3 r1): the snapshot was taken before the lock, so an append between it and the write left
    the anchor reset to an older head. Under the lock the head is read again; a moved head refuses, nothing
    written."""
    from levain.cockpit.keys import KeyError_

    root, laptop, _ = _restored(tmp_path)
    before = anchor_path(root).read_bytes()
    extra = RealKey(tmp_path, "extra")

    def append_unanchored() -> None:              # a writer the store lock does not keep out
        db = CockpitDB(root)
        with db.autocommit() as conn:
            auth.sign_and_append(conn, {"op": "enrol", "fingerprint": extra.fingerprint, "public_key": extra.public,
                                        "class": "display-phone", "custody": "unstated"},
                                 signer_fp=laptop.fingerprint, sign=laptop.sign_link, clock=lambda: NOW, anchor=None)

    with pytest.raises(KeyError_, match="moved while the reset was signed"):
        _reset(root, laptop.fingerprint, _Recorder(laptop, during=append_unanchored))
    assert anchor_path(root).read_bytes() == before


def test_an_empty_log_with_no_anchor_names_only_ledger_init(tmp_path: Path) -> None:
    """complement LOW (L3 r1): with no anchor file the remedy said to move one aside."""
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp_path, "laptop")
    rc, _, err = _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path), "--data-root", str(root))
    assert rc == 1 and auth.init_hint(root) in err and "aside" not in err, err


def _parsed(argv: list[str], monkeypatch) -> "argparse.Namespace":
    """``argv`` through levain.cli's own parser (``main``'s), stopped before the command runs."""
    import argparse

    from levain import cli

    class Parsed(Exception):
        pass

    real = argparse.ArgumentParser.parse_args

    def spy(self, args=None, namespace=None):
        raise Parsed(real(self, args, namespace))

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(argv)
    except Parsed as done:
        return done.args[0]
    except SystemExit:
        raise AssertionError(f"levain's parser refused {argv}: {err.getvalue()}") from None
    finally:
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", real)
    raise AssertionError("never parsed")


def test_every_printed_anchor_remedy_is_a_complete_command_the_parser_takes(tmp_path: Path, monkeypatch) -> None:
    """codex LOW + complement LOW (L3 r1): the hint named `--path or --data-root` in prose and `ledger init` without
    its required `--signing-key`. Each refusal's printed remedy, with only the key file filled in, now parses to the
    command it names, on this data root."""
    from levain.cockpit.broker import LOCK_NAME

    root, laptop, _ = _restored(tmp_path)
    printed = {}
    printed["behind"] = _run("cockpit", "keys", "--data-root", str(root))[2]
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        printed["server"] = _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path),
                                 "--data-root", str(root))[2]
    finally:
        os.close(fd)
    good = anchor_path(root).read_bytes()
    anchor_path(root).write_bytes(b"not an anchor")
    printed["unreadable"] = _run("cockpit", "keys", "--data-root", str(root))[2]
    anchor_path(root).write_bytes(good)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    conn.execute("DELETE FROM authority_log")
    conn.close()
    printed["empty, anchored"] = _run("cockpit", "keys", "--data-root", str(root))[2]
    anchor_path(root).unlink()
    printed["empty"] = _run("cockpit", "anchor", "reset", "--signing-key", str(laptop.path),
                            "--data-root", str(root))[2]
    want = {"behind": "_cmd_cockpit_anchor_reset", "server": "_cmd_cockpit_anchor_reset",
            "unreadable": "_cmd_cockpit_anchor_reset", "empty, anchored": "_cmd_cockpit_ledger_init",
            "empty": "_cmd_cockpit_ledger_init"}
    for case, err in printed.items():
        ns = _parsed(_hinted_argv(err, laptop.path), monkeypatch)
        assert ns.func.__name__ == want[case], (case, err)
        assert (ns.data_root, ns.signing_key) == (root, laptop.path), (case, err)


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
