"""The page names every verb id the cockpit's kernel registry exposes (row 182, the Keep's page half).

The Keep's backend (``intention_keep``) landed before its page did, and nothing noticed the gap: a verb the registry offers that no
page file mentions cannot be pressed from any page. This is the cheap whole-set check: every id in ``KERNEL_VERBS`` appears, as a
whole word, somewhere in the page files under ``levain/templates/web``. It does not prove a button is drawn or wired (the browser
tests do that, ``tests/test_list_page_keep_agenda.py`` for the Keep); it proves the id is not absent from the page entirely.

A verb that is deliberately not a page verb (a kernel-only or CLI-only id) goes in ``NOT_A_PAGE_VERB`` with its reason, never
silently out of the check. The set is empty today.
"""

from __future__ import annotations

import re
from pathlib import Path

from levain.cockpit.verbs import KERNEL_VERBS

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

NOT_A_PAGE_VERB: dict[str, str] = {}


def _page_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(WEB.iterdir()) if p.suffix in {".js", ".html"})


def test_the_page_files_exist() -> None:
    assert (WEB / "dashboard_core.js").is_file()


def _missing(ids: list[str], text: str) -> list[str]:
    return [i for i in ids if i not in NOT_A_PAGE_VERB and not re.search(rf"\b{re.escape(i)}\b", text)]


def test_the_check_can_fail() -> None:
    """A probe that cannot fail is not evidence: an id the text lacks is reported, a prefix of a named id is not a name."""
    assert _missing(["intention_keep"], "x intention_keep_not y") == ["intention_keep"]
    assert _missing(["intention_keep"], "x 'intention_keep' y") == []


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
