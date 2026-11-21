"""The snapshot contract doc points at code; every pointer must still land.

docs/cockpit_snapshot_contract.md backticks only code symbols and repo paths (its own convention paragraph says so).
This reads the doc and resolves each one: a symbol by importing the module and looking it up (``Class.method`` too),
a path by checking the file exists. A doc that names a symbol the code no longer defines fails here, which is the only
thing that keeps a pointer from rotting silently.
"""
from __future__ import annotations

import re
from pathlib import Path

from levain.cockpit import derive, engine, snapshot_host, snapshots

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "cockpit_snapshot_contract.md"
MODULES = (snapshots, snapshot_host, engine, derive)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def _resolves(token: str) -> bool:
    head, _, tail = token.partition(".")
    for mod in MODULES:
        obj = getattr(mod, head, None)
        if obj is None:
            continue
        if not tail or hasattr(obj, tail):
            return True
    return False


def unresolved(text: str, root: Path = ROOT) -> list[str]:
    """Every backticked token of `text` that is neither a defined code symbol nor an existing repo path."""
    bad = []
    for token in re.findall(r"`([^`\n]+)`", text):
        if "/" in token:
            if not (root / token).is_file():
                bad.append(token)
        elif not (IDENT.fullmatch(token) and _resolves(token)):
            bad.append(token)
    return bad


def test_every_symbol_and_path_the_doc_names_exists():
    assert unresolved(DOC.read_text(encoding="utf-8")) == []


def test_the_doc_still_points_at_the_reader_code_for_the_field_rules():
    names = set(re.findall(r"`([^`\n]+)`", DOC.read_text(encoding="utf-8")))
    for symbol in ("BODY_FIELDS", "decode_snap", "PSEUDO_PREFIX", "DISCOVER_ID", "ENTITY_ID",
                   "Cockpit._fresh_of", "Cockpit._worker_health"):
        assert symbol in names, f"the doc no longer points at {symbol}"


def test_the_check_goes_red_on_a_pointer_that_rotted(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("x", encoding="utf-8")
    scratch = (
        "Names `BODY_FIELDS` and `Cockpit._fresh_of` (live), `docs/real.md` (a file that exists),\n"
        "then `BODY_FIELDS_GONE`, `Cockpit._no_such_method`, `NoSuchClass.attr`, `docs/gone.md`, `not a symbol`.\n"
    )
    assert unresolved(scratch, root=tmp_path) == [
        "BODY_FIELDS_GONE", "Cockpit._no_such_method", "NoSuchClass.attr", "docs/gone.md", "not a symbol",
    ]
    assert unresolved("Only `BODY_FIELDS` and `decode_snap`.", root=tmp_path) == []
