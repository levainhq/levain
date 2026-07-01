"""levain.docs — compose the operator manual from base ∪ installed-pack chapters.

`levain docs` renders a browser view of the operator manual. The BASE manual ships
in the wheel (`templates/docs/*.md`, the public domain-neutral "Driving Your
Partner"). A pack that carried its own `docs/` at `levain init --pack` time had
those chapters copied INTO the install (`<install>/.levain/docs/<order>-<pack>/`,
by `install._copy_pack_docs`) so the composed view is SELF-CONTAINED — this module
reads the install, never the original `--pack` directory (which may be long gone).

Ordering mirrors the seed roster (`packs.compose_roster`): base chapters first,
then pack layers by their `pack.toml` ``order`` (zero-padded into the copied dir
name so a lexical sort is the order sort); within a layer, chapters sort by
filename. The multi-root LAYERING is the same move the seed composition makes,
applied to docs.

Honesty floor (mirrors `packs.py`): the base docs MISSING is a corrupt wheel — an
ERROR, never a silent empty (a blank manual would look like a healthy no-op). Pack
docs are OPTIONAL: a pack may ship none, and an install with no `.levain/docs/`
composes to base-only, which is correct.

Safe by construction: HTML comments are stripped here (they are markdown-invisible
by spec, and the browser renderer paints raw text via ``textContent`` — an
un-stripped ``<!-- provenance -->`` would otherwise render as literal visible
text). The composed markdown is handed to the SAME reviewed `renderMarkdown`
(`templates/web/markdown.js`) the dashboard uses; no HTML is ever interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The subdir a pack ships its manual chapters in (peer of the pack's `seed/`).
PACK_DOCS_DIRNAME = "docs"
# Where `install._copy_pack_docs` persists a pack's chapters, so the composed view
# is independent of the (possibly vanished) original `--pack` dir.
INSTALL_DOCS_SUBPATH = (".levain", "docs")

# First ATX H1 = the chapter title. Shape-matched to markdown.js's heading regex
# (`^(#{1,6})[ \t]+…`), which requires the `#` at column 0 (NO leading spaces), so
# the extracted title can't disagree with the heading the browser actually renders
# (complement L3: a leading-space `#` renders as a paragraph, not a title). A
# space-preceded trailing `#` run is a closing sequence and is dropped, so
# "# Title #" → "Title" while "# C#" keeps "C#".
_H1_RE = re.compile(r"^#\s+(.+?)(?:\s+#+)?\s*$")
# HTML comments are markdown-invisible; strip so a provenance comment never paints
# as literal text. Applied ONLY to non-fenced regions (see _fence_mask) so an HTML
# comment shown INSIDE a fenced code sample is preserved (codex/L1).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# A fenced-code OPENER — shape-matched to markdown.js's renderer
# (`^([ \t]*)(```+|~~~+)[ \t]*([^`~]*)$`) so Python's fence detection masks exactly
# the lines the browser treats as fenced. The info string may not contain a
# backtick/tilde, so a MALFORMED opener (e.g. ```` ```foo`bar ````) is NOT a fence
# in either place — a heading after it stays a heading (codex fix-verify LOW).
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*[^`~]*$")


class DocsError(Exception):
    """The base docs are missing or unreadable — a corrupt wheel.

    Carries a ready-to-surface ``.args[0]`` message (mirrors ``packs.PackError`` /
    ``install.InitError``) so the CLI/server can print it directly."""


@dataclass(frozen=True)
class DocChapter:
    """One composed chapter: its title, its (comment-stripped) markdown body, and
    which layer it came from (``"base"`` or the pack's name)."""

    title: str
    markdown: str
    source: str


def _fence_mask(lines: list[str]) -> list[bool]:
    """Mark each line True if it is inside (or delimits) a fenced code block.

    A fence opens on a ``` / ~~~ run (≥3, ≤3 leading spaces) and closes on a line
    that is ONLY the same fence char, repeated at least as long (CommonMark). Used
    so comment-stripping and title-extraction never touch code samples."""
    mask = [False] * len(lines)
    fence = ""  # the active opening fence run, or "" when not in a fenced block
    for i, line in enumerate(lines):
        if not fence:
            m = _FENCE_RE.match(line)
            if m:
                fence = m.group(1)
                mask[i] = True
        else:
            mask[i] = True  # content + the closing fence line
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
    return mask


def _strip_html_comments(md: str) -> str:
    """Strip HTML comments from NON-fenced regions only (a comment inside a code
    fence is literal sample content and is preserved). Each maximal run of
    non-fenced lines is stripped as a unit, so a multi-line comment is removed but
    can never reach across a fence boundary."""
    lines = md.split("\n")
    mask = _fence_mask(lines)
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if mask[i]:
            out.append(lines[i])
            i += 1
        else:
            run: list[str] = []
            while i < n and not mask[i]:
                run.append(lines[i])
                i += 1
            out.append(_HTML_COMMENT_RE.sub("", "\n".join(run)))
    return "\n".join(out)


def _extract_title(md: str, fallback: str) -> str:
    """The first ATX H1's text (outside any code fence), or ``fallback`` (the
    filename stem) if none. Fence-aware so a ``# heading`` inside a code sample is
    not mistaken for the chapter title."""
    lines = md.split("\n")
    mask = _fence_mask(lines)
    for i, line in enumerate(lines):
        if mask[i]:
            continue
        m = _H1_RE.match(line)
        if m:
            return m.group(1).strip()
    return fallback


def _chapter(raw: str, fallback_title: str, source: str) -> DocChapter:
    md = _strip_html_comments(raw)
    return DocChapter(title=_extract_title(md, fallback_title), markdown=md, source=source)


def _base_docs_root():
    """The packaged base-docs dir as an ``importlib.resources`` Traversable — the
    same access path `load_web_asset` uses, so it resolves from a wheel, an
    editable install, or a zip alike."""
    from importlib.resources import files

    return files("levain") / "templates" / PACK_DOCS_DIRNAME


def base_chapters() -> list[DocChapter]:
    """The base manual chapters shipped in the wheel, sorted by filename.

    Raises :class:`DocsError` if the base docs dir is missing/unreadable or empty —
    a corrupt wheel, never a silent empty manual (the honesty floor)."""
    root = _base_docs_root()
    try:
        entries = sorted(
            (e for e in root.iterdir() if e.name.endswith(".md") and e.is_file()),
            key=lambda e: e.name,
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as e:
        raise DocsError(
            "base operator-manual docs not found in the installed package "
            "(templates/docs/). The wheel may be corrupt; reinstall with "
            "`pip install --force-reinstall levain`."
        ) from e
    if not entries:
        raise DocsError(
            "base operator-manual docs are empty (templates/docs/ has no .md files). "
            "The wheel may be corrupt; reinstall with `pip install --force-reinstall levain`."
        )
    chapters: list[DocChapter] = []
    for entry in entries:
        try:
            raw = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise DocsError(f"could not read base doc chapter {entry.name}: {err}") from err
        chapters.append(_chapter(raw, entry.name[:-3], "base"))
    return chapters


def _pack_docs_root(install: Path) -> Path:
    return install.joinpath(*INSTALL_DOCS_SUBPATH)


def pack_chapters(install: Path) -> list[DocChapter]:
    """Chapters a pack contributed, copied into the install at init time.

    OPTIONAL: an install with no persisted pack docs (`<install>/.levain/docs/`
    absent) composes to base-only — returns ``[]``. Pack layers are ordered by
    their copied dir name (``<order>-<pack>``, order zero-padded → lexical sort =
    order sort); within a layer, chapters sort by filename."""
    root = _pack_docs_root(install)
    if not root.is_dir():
        return []
    # Wrap the directory SCANS too, not just the file reads — an unreadable
    # .levain/docs must surface as a clear DocsError, never a raw OSError that
    # run_docs_web mis-maps to a bind error (codex fix-verify MED).
    try:
        layer_dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError as err:
        raise DocsError(f"could not read pack docs dir {root}: {err}") from err
    chapters: list[DocChapter] = []
    for layer in layer_dirs:
        # The copied dir is "<seq:03d>-<packname>"; recover the pack name for the
        # chapter source label (fallback to the whole dir name if unprefixed).
        pack_name = layer.name.split("-", 1)[1] if "-" in layer.name else layer.name
        try:
            md_files = sorted((f for f in layer.glob("*.md") if f.is_file()), key=lambda f: f.name)
        except OSError as err:
            raise DocsError(f"could not read pack docs layer {layer.name}: {err}") from err
        for f in md_files:
            # Wrap the read like base_chapters does — a corrupt/unreadable pack doc
            # must surface as a clear DocsError, not a raw OSError/UnicodeDecodeError
            # that run_docs_web mis-maps to "port in use" / "wheel corrupt" (L1).
            try:
                raw = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                raise DocsError(f"could not read pack doc chapter {f.name}: {err}") from err
            chapters.append(_chapter(raw, f.stem, pack_name))
    return chapters


def discover_chapters(install: Path) -> list[DocChapter]:
    """The full composed manual: base chapters, then installed-pack chapters."""
    return base_chapters() + pack_chapters(install)


def chapters_payload(install: Path) -> dict[str, object]:
    """The JSON projection the docs server serves at ``/docs.json``."""
    chapters = discover_chapters(install)
    return {
        "chapters": [
            {"title": c.title, "markdown": c.markdown, "source": c.source}
            for c in chapters
        ]
    }
