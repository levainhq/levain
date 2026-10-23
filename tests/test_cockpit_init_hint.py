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
