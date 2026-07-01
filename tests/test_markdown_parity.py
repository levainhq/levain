"""Parity lock for the shared markdown renderer.

`templates/web/markdown.js` (used by `levain docs`) and `templates/web/
dashboard_core.js` (used by `levain serve`) each carry the markdown→DOM renderer
core. This is SECURITY-sensitive code (the textContent-only + scheme-allowlist
contract that keeps store/manual content from becoming markup). To stop the two
copies from silently DIVERGING, both delimit the identical core between
``[MD-EXTRACT-START]`` / ``[MD-EXTRACT-END]`` markers; this test slices both and
asserts byte-identity. A drift is a CI FAILURE (structural_invariants_beat_
discipline) — edit the renderer in BOTH files, or repoint the dashboard to load
markdown.js.
"""

from __future__ import annotations

from importlib.resources import files

START = "[MD-EXTRACT-START"
END = "  // [MD-EXTRACT-END]"


def _web_asset(name: str) -> str:
    return (files("levain") / "templates" / "web" / name).read_text(encoding="utf-8")


def _between_markers(text: str) -> str:
    start = text.index(START)
    line_end = text.index("\n", start) + 1  # first char AFTER the START marker line
    end = text.index(END)
    return text[line_end:end]


def test_renderer_core_is_byte_identical() -> None:
    dash = _web_asset("dashboard_core.js")
    md = _web_asset("markdown.js")
    dash_core = _between_markers(dash)
    md_core = _between_markers(md)
    assert dash_core, "dashboard_core.js must contain the MD-EXTRACT block"
    assert md_core, "markdown.js must contain the MD-EXTRACT block"
    assert md_core == dash_core, (
        "markdown.js and dashboard_core.js renderer cores have DIVERGED. Edit the "
        "renderer in BOTH files (between the [MD-EXTRACT] markers), or repoint "
        "dashboard_core.js to load markdown.js. This is a security-sensitive control."
    )


def test_renderer_el_dependency_is_identical() -> None:
    # The renderer core (between the markers) depends on el(), which lives OUTSIDE
    # the markers in both files. Lock it too, so the "cannot diverge" claim actually
    # covers the core's one dependency (codex/L1 noted the marker-only lock is
    # narrower than the comment implied). Whitespace-normalized so indentation
    # differences don't false-fail — only a real logic change trips it.
    import re

    el_re = re.compile(r"const el = \(tag, cls, text\) => \{.*?return n;\s*\};", re.S)
    dash = el_re.search(_web_asset("dashboard_core.js"))
    md = el_re.search(_web_asset("markdown.js"))
    assert dash and md, "el() helper not found in one of the files"
    norm = lambda m: re.sub(r"\s+", " ", m.group(0))  # noqa: E731
    assert norm(md) == norm(dash), "markdown.js el() diverged from dashboard_core.js"


def test_markdown_js_exports_renderer() -> None:
    md = _web_asset("markdown.js")
    assert "window.LevainMD" in md
    assert "renderMarkdown" in md
    assert "mdSafeHref" in md


def test_docs_frontend_is_textcontent_only() -> None:
    """The textContent-only contract (structural_invariants_beat_discipline),
    extended to the docs surface: markdown.js (the shared renderer) and docs.js
    (its thin client) must never reach for innerHTML/outerHTML/insertAdjacentHTML/
    document.write in CODE (comments/strings excluded). Mirrors the dashboard's
    `test_frontend_is_textcontent_only`."""
    import re

    block_re = re.compile(r"/\*.*?\*/", re.S)
    str_re = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'" r"|`(?:[^`\\]|\\.)*`")
    banned = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    for name in ("markdown.js", "docs.js"):
        js = _web_asset(name)
        js = block_re.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), js)
        for lineno, line in enumerate(js.splitlines(), 1):
            code = str_re.sub('""', line)
            ci = code.find("//")
            if ci >= 0:
                code = code[:ci]
            for tok in banned:
                assert tok not in code, (
                    f"{name}:{lineno} uses {tok} in code (not a comment/string) — "
                    "breaks the textContent-only render contract"
                )
