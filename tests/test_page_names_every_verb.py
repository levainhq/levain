"""The page names every verb id the cockpit's kernel registry exposes (row 182, the Keep's page half).

The Keep's backend (``intention_keep``) landed before its page did, and nothing noticed the gap: a verb the registry offers that no
page file mentions cannot be pressed from any page. This is the cheap whole-set check: every id in ``KERNEL_VERBS`` appears, as a
whole word, somewhere in the page files under ``levain/templates/web``. It does not prove a button is drawn or wired (the browser
tests do that, ``tests/test_list_page_keep_agenda.py`` for the Keep); it proves the id is not absent from the page entirely.

A verb that is deliberately not a page verb (a kernel-only or CLI-only id) goes in ``NOT_A_PAGE_VERB`` with its reason, never
silently out of the check; an empty reason is refused. Verb ids must be Python identifiers: the whole-word match uses ``\\b``, which
is only defined at a word character, and a test asserts the assumption so a non-identifier id fails loudly instead of matching wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from levain.cockpit.verbs import KERNEL_VERBS

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

# The file types the dashboard ships under WEB (derived from `git ls-files levain/templates/web`); a new type fails
# test_every_file_under_web_is_a_known_page_type instead of being skipped by the walk.
PAGE_SUFFIXES = {".js", ".html", ".css"}

NOT_A_PAGE_VERB: dict[str, str] = {}


def _page_text(root: Path = WEB) -> str:
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in PAGE_SUFFIXES)
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def test_the_page_files_exist() -> None:
    assert (WEB / "dashboard_core.js").is_file()


def test_every_file_under_web_is_a_known_page_type() -> None:
    odd = sorted(str(p.relative_to(WEB)) for p in WEB.rglob("*") if p.is_file() and p.suffix not in PAGE_SUFFIXES)
    assert not odd, f"files under levain/templates/web with a suffix _page_text() does not read: {odd}"


def test_the_page_text_walk_reaches_subdirectories_and_survives_a_directory_named_like_a_page(tmp_path: Path) -> None:
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "sub" / "deep" / "x.html").write_text("deep_verb", encoding="utf-8")
    (tmp_path / "a.css").write_text("css_verb", encoding="utf-8")
    (tmp_path / "pkg.js").mkdir()
    (tmp_path / "ignored.txt").write_text("txt_verb", encoding="utf-8")
    text = _page_text(tmp_path)
    assert "deep_verb" in text and "css_verb" in text
    assert "txt_verb" not in text


def _missing(ids: list[str], text: str) -> list[str]:
    return [i for i in ids if i not in NOT_A_PAGE_VERB and not re.search(rf"\b{re.escape(i)}\b", text)]


def test_the_check_can_fail() -> None:
    """A probe that cannot fail is not evidence: an id the text lacks is reported, and neither a suffix nor a prefix of a named
    word is a name (both word edges are exercised)."""
    assert _missing(["intention_keep"], "x intention_keep_not y") == ["intention_keep"]
    assert _missing(["intention_keep"], "x xintention_keep y") == ["intention_keep"]
    assert _missing(["run"], "rerun") == ["run"]
    assert _missing(["run"], "run_all") == ["run"]
    assert _missing(["intention_keep"], "x 'intention_keep' y") == []
    assert _missing(["run"], "do run now") == []


def test_every_kernel_verb_id_is_an_identifier() -> None:
    bad = [v.name for v in KERNEL_VERBS if not v.name.isidentifier()]
    assert not bad, f"the \\b whole-word match assumes identifier ids; these are not: {bad}"


def test_every_kernel_verb_id_is_named_in_a_page_file() -> None:
    ids = [v.name for v in KERNEL_VERBS]
    assert ids and len(set(ids)) == len(ids)
    missing = _missing(ids, _page_text())
    assert not missing, f"no page file under levain/templates/web names these verb ids: {missing}"


def test_the_keep_is_named_by_the_list_page_itself() -> None:
    core = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    assert any(v.name == "intention_keep" for v in KERNEL_VERBS)
    assert re.search(r"\bintention_keep\b", core)


def test_an_exempt_verb_is_a_real_verb() -> None:
    ids = {v.name for v in KERNEL_VERBS}
    assert set(NOT_A_PAGE_VERB) <= ids


def test_every_exemption_carries_a_reason() -> None:
    assert all(isinstance(r, str) and r.strip() for r in NOT_A_PAGE_VERB.values())


def test_the_exemption_checks_can_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason check and the real-verb check refuse a bad exemption table (run against a patched table, not the real one)."""
    with monkeypatch.context() as m:
        m.setitem(NOT_A_PAGE_VERB, "section_edit", "")
        with pytest.raises(AssertionError):
            test_every_exemption_carries_a_reason()
    with monkeypatch.context() as m:
        m.setitem(NOT_A_PAGE_VERB, "no_such_verb_id", "typo")
        with pytest.raises(AssertionError):
            test_an_exempt_verb_is_a_real_verb()
