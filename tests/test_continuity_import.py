"""A Claude Code install loads its living memory at session start.

MEASURED by 1003+21 on its demo transcripts (2026-10-03): the SessionStart hook injected only
posture, and the carrier did not import ``.levain/memory.continuity.md``, so the entity read its
memory on 4-5 of 8 days and missed the intent day in 2 of 3 runs. RUN on this branch: a fresh
``levain init --adapter claude-code`` plus a headless ``claude -p`` with every tool disallowed
answered a codeword held only in the continuity file; the same install without the import line
answered NONE.
"""

from __future__ import annotations

import json
from pathlib import Path

from levain.dashboard import LEVAIN_CONTINUITY_REL
from levain.doctor import CONTINUITY_IMPORT, _check_carrier_freshness, _check_context_surface
from levain.install import ADAPTER_RECEIPT_REL
from tests.test_update_refresh import _refresh, _sha, make_install  # noqa: F401


def _without_import(install: Path, *, edited: bool = False) -> Path:
    """Make the install look as a pre-0.5.5 levain left it: the carrier without the import,
    with that copy's hash in the receipt; ``edited`` then adds an operator's own line."""
    carrier = install / "CLAUDE.md"
    old = "".join(ln for ln in carrier.read_text().splitlines(keepends=True)
                  if ln.strip() != CONTINUITY_IMPORT)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"]["CLAUDE.md"] = _sha(old.encode())
    receipt.write_text(json.dumps(data))
    carrier.write_text(old + ("\nMy own note.\n" if edited else ""))
    return carrier


def test_a_fresh_install_imports_the_store_the_memory_server_serves(make_install):
    install = make_install()
    lines = (install / "CLAUDE.md").read_text().splitlines()
    assert CONTINUITY_IMPORT in lines
    # Claude Code resolves the import against the carrier's directory: it must name the
    # continuity file beside the store levain registers with the memory server.
    assert install / CONTINUITY_IMPORT[1:] == install.joinpath(*LEVAIN_CONTINUITY_REL)
    mcp = json.loads((install / ".mcp.json").read_text())
    db = Path(mcp["mcpServers"]["anneal_memory"]["args"][4])
    assert db.with_name("memory.continuity.md") == install.joinpath(*LEVAIN_CONTINUITY_REL)


def test_update_adds_the_import_to_an_unedited_old_carrier_once(make_install):
    install = make_install()
    carrier = _without_import(install)
    r, _out = _refresh(install)
    assert "CLAUDE.md" in r.refreshed and CONTINUITY_IMPORT in carrier.read_text().splitlines()
    after = carrier.read_bytes()
    r2, _out = _refresh(install)
    assert not r2.refreshed and not r2.review and carrier.read_bytes() == after


def test_update_stages_rather_than_overwrites_an_edited_old_carrier(make_install):
    install = make_install()
    carrier = _without_import(install, edited=True)
    before = carrier.read_bytes()
    r, _out = _refresh(install)
    assert carrier.read_bytes() == before and "CLAUDE.md" in r.review
    staged = list((install / ".levain" / "pending").rglob("CLAUDE.md*"))
    assert staged and any(CONTINUITY_IMPORT in p.read_text().splitlines() for p in staged)


def test_doctor_flags_a_carrier_without_the_import_as_a_pending_upgrade(make_install):
    install = make_install()
    [ok] = _check_carrier_freshness(install, install / "CLAUDE.md")
    assert ok.ok
    _without_import(install)
    [stale] = _check_carrier_freshness(install, install / "CLAUDE.md")
    assert not stale.ok and stale.upgrade_pending and "levain update" in (stale.hint or "")


def test_the_context_surface_counts_the_living_memory(make_install):
    import re

    install = make_install()
    total = lambda d: int(re.match(r"([\d,]+) B", d).group(1).replace(",", ""))  # noqa: E731
    [before] = _check_context_surface(install, install / "CLAUDE.md")
    install.joinpath(*LEVAIN_CONTINUITY_REL).write_text("x" * 5000)
    [row] = _check_context_surface(install, install / "CLAUDE.md")
    assert "living memory 5,000B" in row.detail
    assert total(row.detail) == total(before.detail) + 5000


def test_an_import_doctor_cannot_see_claude_code_evaluate_does_not_count(tmp_path):
    from levain.doctor import _imports_continuity

    assert _imports_continuity("@.levain/memory.continuity.md\n")
    assert _imports_continuity("  @./.levain/memory.continuity.md  \n")
    assert not _imports_continuity("```\n@.levain/memory.continuity.md\n```\n")
    assert not _imports_continuity("<!--\n@.levain/memory.continuity.md\n-->\n")
    assert not _imports_continuity("<!-- @.levain/memory.continuity.md -->\n")
    assert not _imports_continuity("    @.levain/memory.continuity.md\n")   # indented code
    assert not _imports_continuity("- @.levain/memory.continuity.md\n")
    # gemini, the 0.5.5 L3, each RUN against Claude Code (`claude -p`, codeword in the
    # continuity): Claude Code answered NONE for all three while doctor counted the import.
    assert not _imports_continuity("\t@.levain/memory.continuity.md\n")
    assert not _imports_continuity("````\n```\n@.levain/memory.continuity.md\n````\n")
    assert not _imports_continuity("text <!-- open\n@.levain/memory.continuity.md\n-->\n")
    # Claude Code read this one (same RUN), and doctor counts it.
    assert _imports_continuity("<!-- closed -->\n@.levain/memory.continuity.md\n")
    assert _imports_continuity("@.levain/memory.continuity.md\r\n")
    for sep in ("\v", "\f", "\x85", "\u2028"):
        assert not _imports_continuity(f"x{sep}@.levain/memory.continuity.md\n")
        assert not _imports_continuity(f"@.levain/memory.continuity.md{sep}x\n")
    assert not _imports_continuity("\u00a0@.levain/memory.continuity.md\n")
    assert not _imports_continuity("    ```\n@.levain/memory.continuity.md\n")
    # A closed fence before the import: Claude Code may read it, doctor cannot confirm it.
    assert not _imports_continuity("```\nx\n```\n@.levain/memory.continuity.md\n")
