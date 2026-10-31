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
from levain.cockpit.firejournal import PairingError, _clean_label


def _unassigned() -> str:
    for cp in range(0x0370, 0xD800):
        if unicodedata.category(chr(cp)) == "Cn":
            return chr(cp)
    raise AssertionError("no unassigned BMP code point found")


CN = _unassigned()

# (case id, text, indices hidden with no allowance under the prose rule[, under the identifier/path rule when they
# differ: selectors=False, where no presentation selector ever shows])
TABLE = [
    ("RLO", "a‮b", [1]),
    ("ZWSP", "a​b", [1]),
    ("ZWJ", "a‍b", [1]),
    ("SHY", "a­b", [1]),
    ("VS1", "a︀b", [1]),
    ("VS16 after a letter", "a️b", [1]),
    ("VS15 after a letter", "a︎b", [1]),
    ("VS16 run of 3", "a️️️b", [1, 2, 3]),
    ("VS16 run of 3 after a symbol", "⚠️️️b", [2, 3], [1, 2, 3]),
    ("keycap digit + VS16", "1️", [], [1]),
    ("double exclamation + VS16", "‼️", [], [1]),
    ("left-right arrow + VS15: its default (text), so it changes nothing", "\u2194\ufe0e", [1]),
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
    ("warning sign + VS16", "⚠️", [], [1]),
    ("plain text", "fix the bug now", []),
    ("newline", "a\nb", [1]),
    ("tab", "a\tb", [1]),
    # presentation selectors (prose): only after a base listed in emoji-variation-sequences.txt, and only the
    # one that changes its default presentation (emoji-data.txt); in identifiers and paths, never
    ("information source + VS16", "ℹ️", [], [1]),
    ("heart + VS16", "❤️", [], [1]),
    ("copyright + VS16", "©️", [], [1]),
    ("number sign + VS16", "#️", [], [1]),
    ("asterisk + VS16", "*️", [], [1]),
    ("keycap one", "1️⃣", [], [1, 2]),
    ("left-right arrow + VS16: changes text to emoji", "\u2194\ufe0f", [], [1]),
    ("watch + VS16: its default (emoji), so it changes nothing", "\u231a\ufe0f", [1]),
    ("watch + VS15: changes emoji to text", "\u231a\ufe0e", [], [1]),
    ("look-alike path, a selector restating the default", "src/\u2194\ufe0e/x", [5]),
    ("look-alike path, a selector that changes the glyph", "src/\u2194\ufe0f/x", [], [5]),
    ("DEL", "a\x7fb", [1]),
    ("equals + VS16", "=️", [1]),
    ("plus + VS15", "+︎", [1]),
    ("rightwards arrow (not a listed base) + VS16", "→️", [1]),
    ("covert bits after Sm", "=︎=️", [1, 3]),
    ("look-alike path", "src/c+️+/x", [6]),
    # combining marks: need a visible base, at most four on one
    ("mark after a space", "rm ̸-rf", [3]),
    ("mark at start", "̀abc", [0]),
    ("thirty marks on one base", "a" + "́" * 30, list(range(5, 31))),
    ("four marks on one base", "á̂̃̄", []),
    ("mark after newline", "a\ń", [1, 2]),
    ("e + acute", "é", []),
    ("Vietnamese decomposed", "ệ", []),
    ("Devanagari", "क्षि", []),
    ("Thai", "กี่", []),
    ("KHITAN FILLER U+16FE4", "a\U00016fe4b", [1]),
    ("NULL NOTEHEAD U+1D159", "a\U0001d159b", [1]),
    ("VS16 after newline", "a\n️b", [1, 2]),
]
IDS = [t[0] for t in TABLE]
TABLE = [(c, t, prose, rest[0] if rest else prose) for c, t, prose, *rest in TABLE]


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


# refusal sites: (name, refuses(text) -> bool, allowance, prose): prose=False is an identifier or a path
REFUSAL = [
    ("dispatch.contract._text_problem", lambda t: contract._text_problem(t, "goal") is not None, "\n\t", True),
    ("dispatch.policy.path_text_problem", lambda t: policy.path_text_problem(t) is not None, "", False),
    ("dispatch.capability.session_problem", lambda t: capability.session_problem(t) is not None, "", False),
    ("dispatch.sign.reason_problem", lambda t: dispatch_sign.reason_problem(t) is not None, "", True),
    ("dispatch.jobs._label", _raises(DispatchError, lambda t: jobs._label("label", t, 1000)), "", False),
    ("dispatch.outcomes._one_line", _raises(DispatchError, lambda t: outcomes._one_line("route", t)), "", False),
    ("consent.sign._text_problem", lambda t: consent_sign._text_problem(t, "message") is not None, "\n\t", True),
    ("consent.push_gate._showable", _raises(ValueError, lambda t: push_gate._showable("branch", t)), "", False),
    ("consent.publish._showable", _raises(publish.PublishRefused, lambda t: publish._showable("version", t)), "",
     False),
    ("consent.questions._has_control (question text)", questions._has_control, "", True),
    ("consent.questions._has_control (an id or the cwd)", lambda t: questions._has_control(t, selectors=False), "",
     False),
    ("slot.runtime_grammar._display", _raises(Refused, lambda t: runtime_grammar._display(t, "repo_label")), "",
     False),
]


@pytest.mark.parametrize(("case", "text", "base", "ident"), TABLE, ids=IDS)
def test_every_site_agrees_with_the_predicate(case: str, text: str, base: list[int], ident: list[int]) -> None:
    assert unshowable.hidden(text) == base, case
    assert unshowable.hidden(text, selectors=False) == ident, case
    assert (unshowable.first_hidden(text) is None) == (not base)
    assert (unshowable.first_hidden(text, selectors=False) is None) == (not ident)

    for name, fn, allow in DISPLAY:
        want = _expected(text, base, allow)
        out = fn(text)
        for ch in set(text):
            if (ch.isascii() and ch.isalnum()) or ch == "+":
                continue        # an escape or a marker spells hex digits, letters and the "+" of "U+" itself
            raw_kept = sum(1 for j, c in enumerate(text) if c == ch and j not in want)
            if ch == "\n" and name == "consent.band._escaped":
                continue        # JSON escapes a newline itself, whatever the predicate says
            assert out.count(ch) == raw_kept, (case, name, f"U+{ord(ch):04X}", out)

    for name, refuses, allow, prose in REFUSAL:
        assert refuses(text) == bool(_expected(text, base if prose else ident, allow)), (case, name)


def test_mark_formats_each_hidden_character() -> None:
    assert unshowable.mark("a‮b\nc") == "a⟨U+202E⟩b\nc"
    assert unshowable.mark("⚠️️", "<{:X}>", allow="") == "⚠️<FE0F>"
    assert unshowable.mark("a️", "<{:X}>", allow="") == "a<FE0F>"
    assert unshowable.mark("a​b", "") == "ab"


def test_strip_sites_keep_words_apart_and_strip_the_rest() -> None:
    assert publish._clean("a b　c​d") == "a b cd"         # a space becomes a space, not nothing
    # a device label (the cockpit's pairing journal) REFUSES a hidden character; the phone port's registry stripped it
    with pytest.raises(PairingError):
        _clean_label("ph​one‮")


def test_the_cockpit_row_carries_the_servers_hidden_flag() -> None:
    """The page's edit lock reads the server's flag (levain.unshowable), never its own render escape: a leading
    combining mark, which the page's escape keeps as it is, is flagged here; a newline in a body is text."""
    from levain.cockpit import Cockpit, ProviderSpec, Read, RowIn

    rows = (RowIn("r1", "t", {"at": "1"}, body="̀x", stored={"id": "r1"}),
            RowIn("r2", "t", {"at": "2"}, body="one\ntwo ⚠️", stored={"id": "r2"}),
            RowIn("r3", "a️b", {"at": "3"}, stored={"id": "r3"}))
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="triage-list", title="p", priority="feed", read=lambda ctx: Read(rows=rows),
                             order="time.desc", facets=frozenset({"at"}), version_fields=("id",)))
    full = {r["id"]: r["text_hidden"] for r in ck.panel("p")["rows"]}
    assert full == {"r1": True, "r2": False, "r3": True}
    compact = ck.panel("p", profile="compact", q="r")["rows"]      # a search sends compact rows
    assert all(r["body"] is None for r in compact)
    assert {r["id"]: r["text_hidden"] for r in compact} == full      # dropping the body keeps the flag


def test_team_project_and_entry_owner_follow_the_predicate() -> None:
    """team.toml's project name is folded on read and refused on write by levain.unshowable; a ledger owner is
    refused on write (a name: no selector) but an entry recorded before the rule still validates on read."""
    from levain.team import entry as team_entry
    from levain.team import roles

    toml = 'project = "webㅤapp ̀x"\nowner = "lead"\n[members]\nlead = "l@x.org"\n'
    assert roles.parse_team(toml).project == "web app x"        # the filler and the baseless mark fold to spaces
    with pytest.raises(roles.RolesError):
        roles.validate_team(roles.Team(project="webㅤapp", owner="lead", members={"lead": "l@x.org"}))
    roles.validate_team(roles.Team(project="web ⚠️ app", owner="lead", members={"lead": "l@x.org"}))

    e = team_entry.build("lead", "finding", owner="client:acme⚠️", words="w", summary="s")
    with pytest.raises(team_entry.EntryError, match="shows as itself"):
        team_entry.validate(e)
    team_entry.validate(e, scan=False)                                 # read: a recorded ruling stays enforced
