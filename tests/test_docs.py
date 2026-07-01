"""Tests for levain.docs — the base ∪ pack chapter composition + install copy.

Compose logic (title extraction, HTML-comment stripping, base-first-then-pack
ordering, base-only fallback, the corrupt-wheel honesty floor) plus the
`install._copy_pack_docs` persistence step that makes `levain docs` self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain import docs
from levain.install import _copy_pack_docs
from levain.packs import PackError, load_pack_manifest


def _write_pack(root: Path, name: str, order: int, *, docs_files: dict[str, str] | None = None) -> Path:
    """A minimal on-disk pack: pack.toml + seed/ + optional docs/."""
    pack = root / name
    (pack / "seed").mkdir(parents=True)
    (pack / "seed" / "extra.md").write_text("# extra\n", encoding="utf-8")
    (pack / "pack.toml").write_text(
        f'name = "{name}"\norder = {order}\n', encoding="utf-8"
    )
    if docs_files:
        (pack / "docs").mkdir()
        for fn, body in docs_files.items():
            (pack / "docs" / fn).write_text(body, encoding="utf-8")
    return pack


def _pairs(*pack_dirs: Path) -> list[tuple]:
    """(manifest, dir) pairs — the pre-validated snapshot run_init threads in."""
    return [(load_pack_manifest(p), p) for p in pack_dirs]


# --------------------------------------------------------------------------
# base chapters (from the wheel/editable package)
# --------------------------------------------------------------------------

class TestBase:
    def test_base_manual_present(self) -> None:
        chapters = docs.base_chapters()
        assert chapters, "the base operator manual must ship in the package"
        assert all(c.source == "base" for c in chapters)
        # The shipped manual's title is its first H1.
        assert any("Driving Your Partner" in c.title for c in chapters)

    def test_base_missing_is_corrupt_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _EmptyRoot:
            def iterdir(self):
                return iter(())

        monkeypatch.setattr(docs, "_base_docs_root", lambda: _EmptyRoot())
        with pytest.raises(docs.DocsError, match="corrupt"):
            docs.base_chapters()


# --------------------------------------------------------------------------
# title extraction + comment stripping (pure)
# --------------------------------------------------------------------------

class TestChapterParse:
    def test_title_from_first_h1(self) -> None:
        c = docs._chapter("intro\n# The Title\n## sub\n", "fallback", "base")
        assert c.title == "The Title"

    def test_title_fallback_when_no_h1(self) -> None:
        c = docs._chapter("no heading here\n## only h2\n", "my-file", "base")
        assert c.title == "my-file"

    def test_html_comments_stripped(self) -> None:
        c = docs._chapter("<!-- provenance note -->\n# Title\nbody\n", "f", "base")
        assert "provenance" not in c.markdown
        assert "# Title" in c.markdown

    def test_multiline_html_comment_stripped(self) -> None:
        raw = "<!--\n  do not commit\n  secret\n-->\n# T\n"
        c = docs._chapter(raw, "f", "base")
        assert "secret" not in c.markdown and "do not commit" not in c.markdown

    def test_html_comment_in_code_fence_preserved(self) -> None:
        # codex/L1: a comment shown INSIDE a code sample is literal content, not a
        # markdown comment — it must survive the strip.
        raw = "# API\n\n```html\n<!-- required sentinel -->\n<div>ok</div>\n```\n"
        c = docs._chapter(raw, "f", "base")
        assert "<!-- required sentinel -->" in c.markdown

    def test_title_ignores_heading_in_code_fence(self) -> None:
        # codex: a `# heading` inside a fence is a code sample, not the chapter title.
        raw = "```md\n# Not The Title\n```\n\n# Actual Title\n"
        c = docs._chapter(raw, "fallback", "base")
        assert c.title == "Actual Title"

    def test_title_strips_space_preceded_closing_hashes(self) -> None:
        assert docs._chapter("# Title #\n", "f", "base").title == "Title"
        # a non-space-preceded '#' is part of the text (e.g. "C#")
        assert docs._chapter("# C#\n", "f", "base").title == "C#"

    def test_fence_mask_tracks_open_and_close(self) -> None:
        lines = ["text", "```", "code # not heading", "```", "more"]
        assert docs._fence_mask(lines) == [False, True, True, True, False]

    def test_malformed_fence_opener_does_not_hide_title(self) -> None:
        # codex fix-verify LOW: a backtick in the info string means it is NOT a valid
        # fence opener (matching markdown.js), so the following # heading is the title.
        raw = "```foo`bar\n# Actual Title\n"
        assert docs._chapter(raw, "fallback", "base").title == "Actual Title"

    def test_valid_fence_with_info_string_masks(self) -> None:
        raw = "```python\n# not the title\n```\n\n# Real Title\n"
        assert docs._chapter(raw, "f", "base").title == "Real Title"


# --------------------------------------------------------------------------
# pack composition + ordering
# --------------------------------------------------------------------------

class TestCompose:
    def test_empty_install_is_base_only(self, tmp_path: Path) -> None:
        payload = docs.chapters_payload(tmp_path)
        assert payload["chapters"], "base chapters always present"
        assert {c["source"] for c in payload["chapters"]} == {"base"}

    def test_pack_chapters_after_base(self, tmp_path: Path) -> None:
        # Simulate a persisted pack-docs layer (what _copy_pack_docs lays down).
        layer = tmp_path / ".levain" / "docs" / "001-acme"
        layer.mkdir(parents=True)
        (layer / "workday.md").write_text("# Acme Work Day\nstuff\n", encoding="utf-8")
        chapters = docs.discover_chapters(tmp_path)
        assert chapters[0].source == "base"
        assert chapters[-1].source == "acme"
        assert chapters[-1].title == "Acme Work Day"

    def test_layers_ordered_by_prefix(self, tmp_path: Path) -> None:
        root = tmp_path / ".levain" / "docs"
        for dirname, title in (("002-beta", "Beta"), ("001-alpha", "Alpha")):
            layer = root / dirname
            layer.mkdir(parents=True)
            (layer / "c.md").write_text(f"# {title}\n", encoding="utf-8")
        packs = [c for c in docs.discover_chapters(tmp_path) if c.source != "base"]
        assert [c.source for c in packs] == ["alpha", "beta"]  # 001 before 002

    def test_pack_docs_optional(self, tmp_path: Path) -> None:
        # A .levain with no docs/ subdir composes cleanly to base-only.
        (tmp_path / ".levain").mkdir()
        assert docs.pack_chapters(tmp_path) == []


# --------------------------------------------------------------------------
# install._copy_pack_docs — the persistence step
# --------------------------------------------------------------------------

class TestCopyPackDocs:
    def test_copies_and_composes(self, tmp_path: Path) -> None:
        install = tmp_path / "install"
        install.mkdir()
        pack = _write_pack(tmp_path, "acme", 5, docs_files={"day.md": "# Day\n"})
        copied = _copy_pack_docs(install, _pairs(pack))
        assert copied == ["acme/day.md"]
        # Numbered by composition RANK (a single pack is rank 0), not the order value.
        assert (install / ".levain" / "docs" / "000-acme" / "day.md").is_file()
        packs = [c for c in docs.discover_chapters(install) if c.source != "base"]
        assert [c.title for c in packs] == ["Day"]

    def test_layers_in_composition_order_by_order_value(self, tmp_path: Path) -> None:
        # Passed in input order [beta(5), alpha(1)] — composition sorts by `order`,
        # so alpha (order 1) composes BEFORE beta (order 5).
        beta = _write_pack(tmp_path, "beta", 5, docs_files={"b.md": "# B\n"})
        alpha = _write_pack(tmp_path, "alpha", 1, docs_files={"a.md": "# A\n"})
        install = tmp_path / "install"
        install.mkdir()
        _copy_pack_docs(install, _pairs(beta, alpha))
        packs = [c for c in docs.discover_chapters(install) if c.source != "base"]
        assert [c.source for c in packs] == ["alpha", "beta"]

    def test_same_order_preserves_input_order_no_collision(self, tmp_path: Path) -> None:
        # codex L3 MED: equal `order` must preserve INPUT order (compose_roster's
        # stable sort), not fall back to alphabetical dir-name sort — and two packs
        # never share a layer dir.
        beta = _write_pack(tmp_path, "beta", 1, docs_files={"b.md": "# B\n"})
        alpha = _write_pack(tmp_path, "alpha", 1, docs_files={"a.md": "# A\n"})
        install = tmp_path / "install"
        install.mkdir()
        _copy_pack_docs(install, _pairs(beta, alpha))  # input order beta, alpha
        packs = [c for c in docs.discover_chapters(install) if c.source != "base"]
        assert [c.source for c in packs] == ["beta", "alpha"]  # NOT alphabetical
        layer_dirs = sorted(d.name for d in (install / ".levain" / "docs").iterdir())
        assert layer_dirs == ["000-beta", "001-alpha"]  # distinct dirs, no merge

    def test_pack_without_docs_is_skipped(self, tmp_path: Path) -> None:
        install = tmp_path / "install"
        install.mkdir()
        pack = _write_pack(tmp_path, "nodocs", 1)  # no docs/
        assert _copy_pack_docs(install, _pairs(pack)) == []
        assert not (install / ".levain" / "docs").exists()

    def test_empty_pack_list_clears_stale_docs(self, tmp_path: Path) -> None:
        # The property the run_init unconditional call relies on (complement L3
        # CRITICAL): a reinstall that DROPS all packs must CLEAR the previously
        # copied pack docs, never leave a removed pack's chapters serving.
        install = tmp_path / "install"
        install.mkdir()
        stale = install / ".levain" / "docs" / "000-acme"
        stale.mkdir(parents=True)
        (stale / "old.md").write_text("# Old\n", encoding="utf-8")
        assert _copy_pack_docs(install, []) == []
        assert not (install / ".levain" / "docs").exists()
        # And it composes to base-only afterwards.
        assert {c.source for c in docs.discover_chapters(install)} == {"base"}

    def test_force_reinstall_rebuilds_no_stale(self, tmp_path: Path) -> None:
        install = tmp_path / "install"
        install.mkdir()
        p1 = _write_pack(tmp_path, "acme", 1, docs_files={"old.md": "# Old\n"})
        _copy_pack_docs(install, _pairs(p1))
        # A second install with a DIFFERENT pack set must not leave "old.md" behind.
        p2 = _write_pack(tmp_path, "beta", 1, docs_files={"new.md": "# New\n"})
        _copy_pack_docs(install, _pairs(p2))
        docs_root = install / ".levain" / "docs"
        remaining = sorted(f.name for f in docs_root.rglob("*.md"))
        assert remaining == ["new.md"]

    def test_path_traversal_name_refused_at_copy(self, tmp_path: Path) -> None:
        # Defense-in-depth (codex L3 HIGH): even if a bad name reached the copy with
        # the parse gate bypassed, the containment check refuses a layer that would
        # escape .levain/docs, and nothing is written outside.
        from levain.install import InitError
        from levain.packs import PackManifest

        pack = _write_pack(tmp_path, "acme", 1, docs_files={"d.md": "# D\n"})
        evil = PackManifest(name="x/../../../seed", order=1)
        install = tmp_path / "install"
        install.mkdir()
        with pytest.raises(InitError, match="escapes"):
            _copy_pack_docs(install, [(evil, pack)])
        assert not (install / "seed").exists()  # nothing written outside the docs tree

    def test_partial_copy_failure_cleans_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # codex L3 LOW: a mid-copy failure must remove the whole derived tree so the
        # caller's "base only" fallback is TRUE, not a half-copied manual.
        import levain.install as install_mod

        pack = _write_pack(tmp_path, "acme", 1, docs_files={"a.md": "# A\n", "b.md": "# B\n"})
        install = tmp_path / "install"
        install.mkdir()
        real_copy = install_mod.shutil.copy2
        calls = {"n": 0}

        def _boom(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_copy(src, dst)

        monkeypatch.setattr(install_mod.shutil, "copy2", _boom)
        with pytest.raises(OSError, match="disk full"):
            _copy_pack_docs(install, _pairs(pack))
        assert not (install / ".levain" / "docs").exists()  # no partial tree left


# --------------------------------------------------------------------------
# read-error honesty floor (L1 #2)
# --------------------------------------------------------------------------

class TestReadErrors:
    def test_pack_doc_bad_utf8_raises_docs_error(self, tmp_path: Path) -> None:
        # An unreadable/corrupt pack doc must surface as a clear DocsError, not a
        # raw UnicodeDecodeError that run_docs_web mis-maps.
        layer = tmp_path / ".levain" / "docs" / "000-acme"
        layer.mkdir(parents=True)
        (layer / "bad.md").write_bytes(b"\xff\xfe not valid utf-8 \x80")
        with pytest.raises(docs.DocsError, match="pack doc chapter"):
            docs.pack_chapters(tmp_path)

    def test_base_docs_dir_missing_is_corrupt_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The FileNotFoundError branch (distinct from the empty-dir branch above).
        class _NoRoot:
            def iterdir(self):
                raise FileNotFoundError("gone")

        monkeypatch.setattr(docs, "_base_docs_root", lambda: _NoRoot())
        with pytest.raises(docs.DocsError, match="corrupt"):
            docs.base_chapters()

    def test_pack_docs_dir_unreadable_is_docs_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # codex fix-verify MED: an unreadable .levain/docs dir must be a DocsError,
        # not a raw OSError that run_docs_web mis-maps to a bind error.
        class _BadRoot:
            def is_dir(self):
                return True

            def iterdir(self):
                raise PermissionError("locked")

        monkeypatch.setattr(docs, "_pack_docs_root", lambda install: _BadRoot())
        with pytest.raises(docs.DocsError, match="pack docs dir"):
            docs.pack_chapters(tmp_path)


# --------------------------------------------------------------------------
# pack name path-safety (codex L3 HIGH — the parse-gate structural fix)
# --------------------------------------------------------------------------

class TestPackNameValidation:
    def _make(self, tmp_path: Path, name: str) -> Path:
        pack = tmp_path / "p"
        (pack / "seed").mkdir(parents=True)
        (pack / "seed" / "x.md").write_text("# x\n", encoding="utf-8")
        (pack / "pack.toml").write_text(f'name = "{name}"\norder = 1\n', encoding="utf-8")
        return pack

    def test_name_with_forward_slash_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PackError, match="path separators"):
            load_pack_manifest(self._make(tmp_path, "x/../../seed"))

    def test_name_with_backslash_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PackError, match="path separators"):
            load_pack_manifest(self._make(tmp_path, "a\\\\b"))

    def test_plain_name_accepted(self, tmp_path: Path) -> None:
        assert load_pack_manifest(self._make(tmp_path, "pressable-solutions")).name == (
            "pressable-solutions"
        )


# --------------------------------------------------------------------------
# Python <-> JS renderer agreement (complement L3 root-cause structural lock)
# --------------------------------------------------------------------------

class TestPythonJsParity:
    """docs.py's fence + heading regexes must agree with markdown.js's, or the
    composed title/comment logic diverges from what the browser renders. No live JS
    here — this pins the Python side to the RENDERER's documented behavior on a
    tricky fixture set, the same way test_markdown_parity locks the JS<->JS core."""

    @pytest.mark.parametrize(
        "line,is_fence",
        [
            ("```", True),
            ("```python", True),  # valid info string
            ("   ```", True),  # indented (markdown.js allows [ \t]*)
            ("       ```bash", True),  # 4+ spaces — markdown.js allows it
            ("```foo`bar", False),  # backtick in info string — renderer rejects
            ("```has ~tilde", False),  # tilde in info string — renderer rejects
            ("~~~", True),
            ("~~~ruby", True),
            ("not a fence", False),
            ("``", False),  # only 2 — not a fence
        ],
    )
    def test_fence_open_matches_renderer(self, line: str, is_fence: bool) -> None:
        assert bool(docs._FENCE_RE.match(line)) is is_fence

    @pytest.mark.parametrize(
        "line,title",
        [
            ("# Title", "Title"),
            ("# Title #", "Title"),  # space-preceded closing hashes dropped
            ("# C#", "C#"),  # non-space-preceded # kept
            ("  # Indented", None),  # renderer heading needs col-0 '#' → not a title
            ("## Section", None),  # h2, not h1
            ("#NoSpace", None),  # needs a space after '#'
        ],
    )
    def test_h1_matches_renderer(self, line: str, title: str | None) -> None:
        m = docs._H1_RE.match(line)
        assert (m.group(1).strip() if m else None) == title
