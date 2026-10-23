"""Every "authority not initialised" refusal prints a complete command: `levain cockpit ledger init` with its required
--signing-key (the one placeholder word) and this journal's --data-root, shlex-quoted. Each refusal's printed remedy,
with only the key file filled in, goes through levain.cli's parser to the command it names, on this data root
(the pattern of test_cockpit_anchor_reset's anchor-remedy test)."""
from __future__ import annotations

import sqlite3
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain import authority
from levain.authority import anchor_path
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.firejournal import UNINITIALISED, CockpitDB, _key_problem
from tests.cockpit_authority_rig import RealKey, enrol
from tests.test_cockpit_anchor_reset import _parsed

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    authority.PROCESS_CACHE.clear()


def _argv(message: str, key: Path) -> list[str]:
    """The backticked `levain ...` remedy split as a shell splits it (the root is quoted), the placeholder word filled."""
    m = re.search(r"`(levain [^`]+)`", message)
    assert m is not None, message
    return [str(key) if w == authority.KEY_WORD else w for w in shlex.split(m.group(1))[1:]]


def _capture_error(root: Path, anchor: bool) -> str:
    with CockpitDB(root).read() as conn:
        got = RequestAuthority.capture(conn, now=NOW, anchor=anchor_path(root) if anchor else None, source="t")
    assert got.authority_error is not None
    return got.authority_error


def test_every_authority_not_initialised_remedy_is_a_complete_command_the_parser_takes(tmp_path: Path,
                                                                                       monkeypatch) -> None:
    root = tmp_path / "a cockpit"                        # a space: the remedy must quote it
    root.mkdir()
    db = CockpitDB(root)
    db.set_install_class("reported")
    printed = {}
    # 1. an empty log
    printed["capture, empty log"] = _capture_error(root, anchor=False)
    # 2. a migrated store: the marker is set over a populated log
    laptop = RealKey(tmp_path, "laptop")
    enrol(db, laptop, "laptop-presence", now=NOW)
    authority.PROCESS_CACHE.clear()
    with db.write() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, 'authority migrated')", (UNINITIALISED,))
    printed["capture, marker set"] = _capture_error(root, anchor=True)
    # 3. a decide / sign-client refusal with no authority captured
    with db.read() as conn:
        refusal, _ = _key_problem(conn, None, laptop.fingerprint, "T2")
    printed["key problem"] = refusal
    for case, err in printed.items():
        assert err.startswith("authority not initialised"), (case, err)
        ns = _parsed(_argv(err, laptop.path), monkeypatch)
        assert ns.func.__name__ == "_cmd_cockpit_ledger_init", (case, err)
        assert (ns.data_root, ns.signing_key) == (root, laptop.path), (case, err)


def _argv_of(span: str, key: Path) -> list[str]:
    """One backticked `levain ...` span, split as a shell splits it, the placeholder word filled."""
    return [str(key) if w == authority.KEY_WORD else w for w in shlex.split(span)[1:]]


def _spans(message: str) -> list[str]:
    return re.findall(r"`(levain [^`]+)`", message)


def _key_create(tmp_path: Path, monkeypatch, capsys, key: Path) -> str:
    from types import SimpleNamespace

    from levain import cli
    from levain.cockpit import custody
    from levain.cockpit import keys as keys_mod

    made = SimpleNamespace(path=key, public_path=Path(str(key) + ".pub"), public_key="pk", custody="passphrase",
                           ctk_hash=None, label="made")
    monkeypatch.setattr(custody, "create_passphrase", lambda d, label: made)
    monkeypatch.setattr(keys_mod, "fingerprint_of", lambda pk: "SHA256:x")
    assert cli.main(["cockpit", "key", "create", "--custody", "passphrase", "--dir", str(tmp_path)]) == 0
    return next(line for line in capsys.readouterr().out.splitlines() if line.startswith("next"))


def test_key_create_names_ledger_init_and_enroll_only_for_a_journal_that_exists(tmp_path: Path, monkeypatch,
                                                                                capsys) -> None:
    inst = tmp_path / "inst dir"
    inst.mkdir()
    monkeypatch.chdir(inst)
    key = tmp_path / "k ey" / "made"
    root = inst.resolve() / ".levain" / "cockpit"
    # the install has no cockpit yet: the command that creates it, then the init, and no init pasted before it
    nxt = _key_create(tmp_path, monkeypatch, capsys, key)
    first, second = _spans(nxt)
    ns = _parsed(_argv_of(first, key), monkeypatch)
    assert ns.func.__name__ == "_cmd_serve" and ns.write and ns.install_class == "reported", nxt
    assert not (root / "cockpit.db").exists() and "<" not in nxt, nxt
    assert (_parsed(_argv_of(second, key), monkeypatch).data_root, ) == (root,), nxt
    # the journal exists: the init for a new log, or the enrol into an existing one (this key's public half,
    # the one placeholder for the signing key, the same data root), every span quoted through a shell split
    root.mkdir(parents=True)
    CockpitDB(root).set_install_class("reported")
    nxt = _key_create(tmp_path, monkeypatch, capsys, key)
    init, enrol_ = _spans(nxt)
    ns = _parsed(_argv_of(init, key), monkeypatch)
    assert (ns.func.__name__, ns.data_root, ns.signing_key) == ("_cmd_cockpit_ledger_init", root, key), nxt
    ns = _parsed(_argv_of(enrol_, key), monkeypatch)
    assert (ns.func.__name__, ns.data_root, ns.signing_key, ns.key) == (
        "_cmd_cockpit_enroll", root, key, Path(str(key) + ".pub")), nxt
    assert "<" not in nxt, nxt


def test_key_create_outside_an_install_pastes_no_command(tmp_path: Path, monkeypatch, capsys) -> None:
    from levain import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_cockpit_root", lambda args: (_ for _ in ()).throw(SystemExit("no writable install")))
    nxt = _key_create(tmp_path, monkeypatch, capsys, tmp_path / "k" / "made")
    assert _spans(nxt) == [] and "<" not in nxt and ">" not in nxt, nxt


def _grant_install(tmp_path: Path, monkeypatch, root: Path | None = None) -> tuple[Path, Path]:
    from levain.consent import default_grants

    inst = tmp_path / "an install"
    inst.mkdir()
    root = root or (inst.resolve() / ".levain" / "cockpit")
    monkeypatch.setattr(default_grants, "_hook_root", lambda adapter: root)
    return inst, root


def _offer(inst: Path) -> list[str]:
    from levain.consent import default_grants

    said: list[str] = []
    default_grants._offer(inst, "claude-code", interactive=False, emit=said.append, input_fn=input,
                          clock=lambda: NOW, state={})
    return said


def test_default_grant_with_no_journal_starts_the_cockpit_first_and_pastes_no_ledger_init(tmp_path: Path,
                                                                                         monkeypatch) -> None:
    """codex MED: `ledger init` refuses a root with no cockpit.db, so an absent journal is not an empty log."""
    inst, root = _grant_install(tmp_path, monkeypatch)
    (line,) = _offer(inst)
    spans = _spans(line)
    serve, init, force = spans
    assert _parsed(_argv_of(serve, tmp_path / "k"), monkeypatch).func.__name__ == "_cmd_serve", line
    ns = _parsed(_argv_of(force, tmp_path / "k"), monkeypatch)
    assert (ns.func.__name__, ns.path, ns.adapter, ns.force) == ("_cmd_init", inst.resolve(), "claude-code", True), line
    # the printed serve target is the install whose ledger's cockpit/ is the root that was reported
    from levain.cli import _cockpit_root
    import argparse
    assert _cockpit_root(argparse.Namespace(path=_parsed(_argv_of(serve, tmp_path / "k"), monkeypatch).path,
                                            data_root=None)) == root


def test_default_grant_for_an_embedders_root_pastes_no_serve_command(tmp_path: Path, monkeypatch) -> None:
    inst, _ = _grant_install(tmp_path, monkeypatch, root=tmp_path / "elsewhere" / "cockpit")
    (line,) = _offer(inst)
    assert not any(" serve " in span for span in _spans(line)), line
    assert "<" not in line and len(_spans(line)) == 1, line      # only the complete --force re-offer


def test_the_printed_ledger_init_runs_once_the_journal_exists_and_the_force_remedy_keeps_the_install(
        tmp_path: Path, monkeypatch) -> None:
    """The execution row: the command the offer prints, with the placeholder filled, the sidecar's --custody (this key
    has none) and a --confirm (a pipe is not a terminal), initialises the log; the `--force` remedy parses back to this install and adapter."""
    import levain.cockpit.custody as custody_mod
    from levain import cli

    # TEST ONLY: the key is unencrypted (ssh-keygen cannot ask a passphrase here); the check is test_cockpit_key_custody's
    monkeypatch.setattr(custody_mod, "passphrase_problem", lambda *a, **kw: None)
    inst, root = _grant_install(tmp_path, monkeypatch)
    root.mkdir(parents=True)
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp_path, "laptop")
    init, force = _spans(_offer(inst)[0])
    ns = _parsed(_argv_of(force, laptop.path), monkeypatch)
    assert (ns.path, ns.adapter, ns.force) == (inst.resolve(), "claude-code", True)
    assert cli.main(_argv_of(init, laptop.path) + ["--custody", "passphrase", "--confirm", laptop.fingerprint]) == 0
    authority.PROCESS_CACHE.clear()
    with CockpitDB(root).read() as conn:
        assert authority.snapshot(conn).key(laptop.fingerprint) is not None


def test_an_empty_log_refusal_from_authority_snapshot_is_a_complete_command(tmp_path: Path, monkeypatch) -> None:
    """complement MED: dispatch/jobs and dispatch/control print NotInitialised's text verbatim."""
    root = tmp_path / "a cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    with CockpitDB(root).read() as conn, pytest.raises(authority.NotInitialised) as exc:
        authority.snapshot(conn)
    (span,) = _spans(str(exc.value))
    ns = _parsed(_argv_of(span, tmp_path / "k"), monkeypatch)
    assert (ns.func.__name__, ns.data_root) == ("_cmd_cockpit_ledger_init", root), str(exc.value)


def test_a_fileless_journal_names_no_data_root(tmp_path: Path) -> None:
    """complement LOW 3: `:memory:` has no file, so no root: prose, never `--data-root .`."""
    conn = sqlite3.connect(":memory:")
    assert authority.conn_data_root(conn) is None
    hint = authority.conn_init_hint(conn)
    assert "--data-root ." not in hint and "`" not in hint and "<" not in hint, hint
    assert _spans(hint) == [], hint
