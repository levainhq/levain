"""levain.unshowable: one table of characters, the predicate's verdict on each, and every display and refusal site
agreeing with it (a display site never passes a hidden character through raw; a refusal site refuses exactly when
the predicate finds a hidden character outside the site's own allowance)."""
from __future__ import annotations

import unicodedata

import pytest

from levain import cli as levain_cli
from levain import unshowable
from levain.cockpit import display_safe
from levain.cockpit import text as cockpit_text
from levain.consent import band, publish, push_gate, questions
from levain.consent import sign as consent_sign
from levain.dispatch import DispatchError
from levain.dispatch import capability, contract, jobs, outcomes, policy
from levain.dispatch import cli as dispatch_cli
from levain.dispatch import sign as dispatch_sign
from levain.slot import Refused
from levain.slot import runtime_grammar


def _unassigned() -> str:
    for cp in range(0x0370, 0xD800):
        if unicodedata.category(chr(cp)) == "Cn":
            return chr(cp)
    raise AssertionError("no unassigned BMP code point found")


CN = _unassigned()

# (case id, text, indices the predicate calls hidden with no allowance)
TABLE = [
    ("RLO", "a‮b", [1]),
    ("ZWSP", "a​b", [1]),
    ("ZWJ", "a‍b", [1]),
    ("SHY", "a­b", [1]),
    ("VS1", "a︀b", [1]),
    ("VS16 after a letter", "a️b", []),
    ("VS15 after a letter", "a︎b", []),
    ("VS16 run of 3", "a️️️b", [2, 3]),
    ("VS16 at start", "️ab", [0]),
    ("VS16 after a hidden char", "a​️b", [1, 2]),
    ("VS17 U+E0100", "a\U000e0100b", [1]),
    ("KHMER U+17B4", "a឴b", [1]),
    ("HANGUL FILLER U+3164", "aㅤb", [1]),
    ("CGJ U+034F", "a͏b", [1]),
    ("BRAILLE BLANK", "a⠀b", [1]),
    ("NBSP", "a b", [1]),
    ("IDEOGRAPHIC SPACE", "a　b", [1]),
    ("PRIVATE USE U+E000", "ab", [1]),
    ("UNASSIGNED", f"a{CN}b", [1]),
    ("TAG U+E0041", "a\U000e0041b", [1]),
    ("warning sign + VS16", "⚠️", []),
    ("plain text", "fix the bug now", []),
    ("newline", "a\nb", [1]),
    ("tab", "a\tb", [1]),
    ("VS16 after newline", "a\n️b", [1, 2]),
]
IDS = [t[0] for t in TABLE]


def _expected(text: str, base: list[int], allow: str) -> list[int]:
    return [i for i in base if text[i] not in allow]


# display sites: (name, function, allowance)
DISPLAY = [
    ("cockpit.display_safe.visible", display_safe.visible, "\n\t"),
    ("cockpit.text.visible", cockpit_text.visible, ""),
    ("cli._term_safe", levain_cli._term_safe, ""),
    ("dispatch.cli._safe", dispatch_cli._safe, ""),
    ("consent.band._escaped", band._escaped, ""),
    ("consent.publish._clean", publish._clean, "\n\t"),
]


def _raises(exc: type[BaseException], fn):  # type: ignore[no-untyped-def]
    def check(text: str) -> bool:
        try:
            fn(text)
        except exc:
            return True
        return False
    return check


# refusal sites: (name, refuses(text) -> bool, allowance)
REFUSAL = [
    ("dispatch.contract._text_problem", lambda t: contract._text_problem(t, "goal") is not None, "\n\t"),
    ("dispatch.policy.path_text_problem", lambda t: policy.path_text_problem(t) is not None, ""),
    ("dispatch.capability.session_problem", lambda t: capability.session_problem(t) is not None, ""),
    ("dispatch.sign.reason_problem", lambda t: dispatch_sign.reason_problem(t) is not None, ""),
    ("dispatch.jobs._label", _raises(DispatchError, lambda t: jobs._label("label", t, 1000)), ""),
    ("dispatch.outcomes._one_line", _raises(DispatchError, lambda t: outcomes._one_line("route", t)), ""),
    ("consent.sign._text_problem", lambda t: consent_sign._text_problem(t, "message") is not None, "\n\t"),
    ("consent.push_gate._showable", _raises(ValueError, lambda t: push_gate._showable("branch", t)), ""),
    ("consent.publish._showable", _raises(publish.PublishRefused, lambda t: publish._showable("version", t)), ""),
    ("consent.questions._has_control", questions._has_control, ""),
    ("slot.runtime_grammar._display", _raises(Refused, lambda t: runtime_grammar._display(t, "repo_label")), ""),
]


@pytest.mark.parametrize(("case", "text", "base"), TABLE, ids=IDS)
def test_every_site_agrees_with_the_predicate(case: str, text: str, base: list[int]) -> None:
    assert unshowable.hidden(text) == base, case
    assert (unshowable.first_hidden(text) is None) == (not base)

    for name, fn, allow in DISPLAY:
        want = _expected(text, base, allow)
        out = fn(text)
        for ch in set(text):
            if ch.isascii() and ch.isalnum():
                continue        # an escape or a marker spells hex digits and letters itself
            raw_kept = sum(1 for j, c in enumerate(text) if c == ch and j not in want)
            if ch == "\n" and name == "consent.band._escaped":
                continue        # JSON escapes a newline itself, whatever the predicate says
            assert out.count(ch) == raw_kept, (case, name, f"U+{ord(ch):04X}", out)

    for name, refuses, allow in REFUSAL:
        assert refuses(text) == bool(_expected(text, base, allow)), (case, name)


def test_mark_formats_each_hidden_character() -> None:
    assert unshowable.mark("a‮b\nc") == "a⟨U+202E⟩b\nc"
    assert unshowable.mark("a️️", "<{:X}>", allow="") == "a️<FE0F>"
    assert unshowable.mark("a​b", "") == "ab"
