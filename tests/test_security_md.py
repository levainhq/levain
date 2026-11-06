"""SECURITY.md is a final file (1105-36 b1fix): a reporter who follows it must reach a real channel.

It was first drafted with a DRAFT banner and a SLOT for the reporting channel; those must never reach `main`. The
channel is GitHub private vulnerability reporting, so the advisory URL must be present. Two claims it makes about other
files are pinned here, because nothing else would fail when those files change."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
ADVISORY_URL = "https://github.com/levainhq/levain/security/advisories/new"


def test_no_draft_marker_remains():
    assert re.findall(r"\b(DRAFT|SLOT|TODO)\b", SECURITY) == []


def test_the_private_advisory_url_is_the_channel():
    assert ADVISORY_URL in SECURITY


def test_the_section_it_points_the_signature_namespaces_at_exists():
    assert "§F11" in SECURITY
    contracts = (ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
    assert re.search(r"^### F11\. SSHSIG namespace registry$", contracts, re.M)


def test_the_gate_switch_it_names_is_one_the_readme_names():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 'efferent_gate: "ungated"' in SECURITY and 'efferent_gate: "ungated"' in readme
    assert "Boundaries, kept honest" in SECURITY and "## Boundaries, kept honest" in readme
