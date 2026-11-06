"""README's install front door (1105-36 B1; the reproduced failure is in the CHANGELOG entry).

On a stock Mac the system `python3` is older than levain's `requires-python`, and a bare `pip install levain` there
resolves to an early placeholder release and reports success. The README must therefore (a) lead with an isolated
installer, (b) say how to check the Python, (c) state its platform and Node requirements, and (d) never recommend a
bare `pip install levain` on a line of its own. The pitch words around the install block are not pinned here."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)


def _section(heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## )", README, re.M | re.S)
    assert m, heading
    return m.group(1)


def test_the_top_install_block_is_an_isolated_installer():
    top = _fenced_blocks(README.split("\n## ", 1)[0])[0]
    assert "pipx install levain" in top and "uv tool install levain" in top, top


def test_set_it_up_checks_the_python_before_it_installs():
    block = _fenced_blocks(_section("Set it up"))[0]
    assert "python3 --version" in block and "3.12" in block, block
    assert block.index("python3 --version") < block.index("install levain"), block
    assert "pipx install levain" in block and "uv tool install levain" in block, block
    assert "levain --version" in block, block


def test_the_requirements_line_names_platform_python_and_node():
    line = next(l for l in README.splitlines() if l.startswith("Apache 2.0"))
    for needed in ("Python 3.12", "macOS and Linux", "Windows planned", "Node 26", "levain chat setup"):
        assert needed in line, (needed, line)


def test_no_line_recommends_bare_pip_install_levain():
    bare = [l for b in _fenced_blocks(README) for l in b.splitlines() if re.match(r"\s*pip3?\s+install\s+levain\s*(#.*)?$", l)]
    assert bare == [], bare


def test_the_project_urls_still_hold_the_homepage_and_repository():
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    assert {"Homepage", "Repository"} <= set(urls), urls


# --- docs fix round (1105-36 b1fix): findings of the docs L3 on this branch -------------------------------------------

CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
CONTRIBUTING = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")


def _set_it_up_prose() -> str:
    return "\n".join(l for l in _section("Set it up").split("\n\n") if l.startswith("Install it with"))


def test_the_node_requirement_is_the_major_the_launch_check_accepts():
    from levain.chat_egress.launch import ACCEPTED_NODE_MAJORS

    line = next(l for l in README.splitlines() if l.startswith("Apache 2.0"))
    assert len(ACCEPTED_NODE_MAJORS) == 1, "the README line names one major; update it with the tuple"
    assert f"Node {ACCEPTED_NODE_MAJORS[0]} (other majors are refused)" in line, line
    assert "+" not in line.split("Node", 1)[1].split(" ", 2)[1], line
    assert "every chat launch" in line, line
    unreleased = CHANGELOG.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    entry = next(e for e in unreleased.split("\n- **") if e.startswith("The README's install block"))
    assert "26+" not in entry, entry


def test_a_missing_command_names_path_before_the_old_python():
    prose = _set_it_up_prose()
    assert "pipx ensurepath" in prose, prose
    assert prose.index("pipx ensurepath") < prose.index("older than 3.12"), prose


def test_pipx_is_told_which_python_and_uv_is_said_to_fetch_its_own():
    prose = _set_it_up_prose()
    assert "pipx install --python python3.12 levain" in prose, prose
    assert "uv tool install" in prose and "fetches" in prose, prose
    assert "install pipx or uv first" in prose.lower(), prose


def test_the_readme_makes_no_claim_about_the_package_index_releases():
    assert "placeholder" not in README.lower()


def test_contributing_gives_the_literal_ci_install_line_and_no_security_file_link():
    ci_lines = [l.strip().removeprefix("run: ") for l in WORKFLOW.splitlines() if "pip install -e" in l]
    literal = next(l for l in ci_lines if "openhands" in l)
    assert literal in CONTRIBUTING, (literal, CONTRIBUTING)
    assert "test extras" not in CONTRIBUTING
    assert "SECURITY.md" not in CONTRIBUTING
    assert "private vulnerability reporting" in CONTRIBUTING and "Security tab" in CONTRIBUTING


def test_the_changelog_structure_contributing_describes_exists():
    assert "## [Unreleased]" in CONTRIBUTING and "\n## [Unreleased]\n" in CHANGELOG
    heads = re.findall(r"^## .*$", CHANGELOG, re.M)
    assert heads[-1].startswith("## Versioning"), heads[-1]
    assert "*Versioning*" in CONTRIBUTING
