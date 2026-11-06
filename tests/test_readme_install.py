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
