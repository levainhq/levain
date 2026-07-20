"""levain.firing.encoding — the mojibake shield at the capture boundary (spore-373 blocker [2]).

Two obligations, and the SECOND is the harder one:

  1. Repair fires on the real signature (a UTF-8 character read back as cp1252/latin-1, at one
     or two levels of encoding).
  2. Repair NEVER fires on legitimate text — including legitimate text made of the very same
     byte ranges. Naive mojibake repair mangles real language; an over-eager "fix" writing
     WRONG characters into a memory store is worse than leaving visible damage, because the
     damage at least announces itself.

The mojibake fixtures are CONSTRUCTED by performing the actual corruption
(``correct.encode("utf-8").decode("cp1252")``) rather than typed as literals, so the corpus
cannot drift into testing a typo instead of the defect.
"""
from __future__ import annotations

import pytest

from levain.firing.contract import CaptureRequest
from levain.firing.encoding import RECEIPT_KEY, scan_text


def _misdecode_byte(b: int) -> str:
    """One byte as a mis-decoding layer would render it: cp1252 where it is defined, latin-1
    for the five values cp1252 leaves undefined (0x81/0x8D/0x8F/0x90/0x9D). Whole-string
    ``.decode("cp1252")`` is NOT usable as a fixture generator — it raises on exactly those
    five bytes, so a corpus built that way would silently omit every curly-quote case (U+201D
    ends in 0x9D). Real decoders in this chain do not raise; they emit the latin-1 C1 control."""
    for codec in ("cp1252", "latin-1"):
        try:
            return bytes([b]).decode(codec)
        except UnicodeDecodeError:
            continue
    raise AssertionError(f"unmappable byte {b:#x}")  # unreachable: latin-1 maps all 256


def mojibake(correct: str, levels: int = 1) -> str:
    """The corruption itself: UTF-8 bytes read back through a single-byte codec."""
    out = correct
    for _ in range(levels):
        out = "".join(_misdecode_byte(b) for b in out.encode("utf-8"))
    return out


# --------------------------------------------------------------------------------------
# 1. repair fires on the real signature
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "correct",
    [
        "an em—dash in a sentence",          # THE observed case (U+2014)
        "curly “quotes” and an ellipsis…",
        "café au lait",
        "Größe und Straße",
        "não é possível",
        "a non breaking space",
        "a rocket 🚀 launches",                 # 4-byte astral
        "mixed — “both” — kinds",
    ],
)
def test_single_level_mojibake_is_repaired(correct):
    scan = scan_text(mojibake(correct))
    assert scan.text == correct
    assert scan.repaired
    assert scan.repairs  # the receipt records what was changed


def test_cyrillic_mojibake_is_FLAGGED_rather_than_repaired(correct="Привет мир"):
    """The deliberate cost of the repair-lead gate (see `_REPAIR_LEADS`). Cyrillic/Greek/CJK
    mis-decodes open with 0xD0-0xEF, whose single-byte renderings are also the ordinary
    accented lowercase letters — admitting them as repair leads is exactly what corrupted
    "CAFÉ—hands down". Cyrillic's 0xD0 lead renders as 'Ð', which IS a suspect lead, so this
    damage is surfaced rather than silently fixed or silently kept."""
    scan = scan_text(mojibake(correct))
    assert scan.text != correct     # honest: NOT repaired
    assert not scan.repairs
    assert scan.suspect             # …and not silent either


def test_KNOWN_BLIND_SPOT_three_byte_script_mojibake_passes_through_unflagged():
    """A limitation recorded as a TEST so it cannot be mistaken for coverage.

    A mis-decoded 3-byte character (CJK, Hangul, Devanagari, Greek) renders with a 0xE0-0xEF
    lead — 'à' through 'ï' — which are ordinary accented lowercase letters in living
    orthographies. Flagging on them would fire on a large share of French and Portuguese
    captures, so the shield's flag coverage is Latin-lead-biased and this particular damage is
    invisible to it. Repairing it is out of the question for the same reason. If this test
    ever fails because the span IS flagged, that is an improvement — check the false-flag rate
    against LEGITIMATE first, then update this test rather than the corpus."""
    for correct in ("北京市", "Γειά σου", "한국어"):
        scan = scan_text(mojibake(correct))
        assert not scan.repairs
        assert not scan.suspect
        assert scan.clean


def test_double_encoded_mojibake_is_repaired_through_both_layers():
    """The evidence carried doubly-encoded spans ('Ã¢'), so one pass is not enough."""
    correct = "an em—dash"
    scan = scan_text(mojibake(correct, levels=2))
    assert scan.text == correct
    assert len(scan.repairs) >= 2  # each layer peeled is its own auditable entry


def test_repair_leaves_surrounding_ascii_untouched():
    scan = scan_text("before " + mojibake("—") + " after")
    assert scan.text == "before — after"


def test_partially_corrupted_turn_repairs_only_the_damage():
    """The real ratio in the evidence: 4 mangled characters against 49 correct ones."""
    text = "The fix resolves ${CLAUDE_PROJECT_DIR}" + mojibake(" — ") + "in every token."
    scan = scan_text(text)
    assert scan.text == "The fix resolves ${CLAUDE_PROJECT_DIR} — in every token."


# --------------------------------------------------------------------------------------
# 2. repair NEVER fires on legitimate text
# --------------------------------------------------------------------------------------

LEGITIMATE = [
    # Western European — the exact byte range mojibake lives in
    "café au lait",
    "naïve façade",
    "Größe und Straße",                  # ö and ß ADJACENT — a non-ASCII run of length 2
    "não é possível",
    "El Niño",
    "déjà vu, résumé, coöperate",
    "Ærø, Møller, Ångström",
    "Ávila · Óscar · Úrsula",
    "Œuvre and æther",
    "þorn and ð and ñ",
    "«guillemets» and „quotes“",
    "£100 · ¥200 · €300 · ¢5",
    "±5°C, 3 × 4 ÷ 2, ¼ and ½",
    "¡Hola! ¿Qué tal?",
    "Ça ira, l'âme, être, hôtel, coût",
    # outside the single-byte range entirely — must be skipped wholesale
    "日本語のテキスト",
    "Привет, как дела?",
    "emoji 🎉 🚀 ✅ in a line",
    "मानक हिन्दी",
    "한국어 텍스트",
    # correctly-encoded punctuation that mojibake DECODES INTO — must not round-trip back
    "an em—dash and an en–dash",
    "curly “quotes” and ‘singles’ and an ellipsis…",
    "a non breaking space",
    # lone lead-byte-lookalike characters inside real words
    "âme, âge, bâton",
    # code and paths, where a stray byte would be catastrophic to rewrite
    "path = 'C:\\Users\\phill\\Ω' # ohm",
]

# Text that legitimately contains a lead-byte character standing alone — the one shape the
# lone-character suspect rule cannot distinguish from an orphaned lead byte. It must never be
# MODIFIED; it is allowed to be flagged, and the accepted trade is documented in
# `_suspect_spans`. Kept as its own list so the exemption is explicit rather than a silently
# weakened assertion in the main corpus.
AMBIGUOUS_BUT_NEVER_MODIFIED = [
    "Ã is a letter in Portuguese",
    "the â in âme is a circumflex",
]


# THE REGRESSION CORPUS THAT MATTERS MOST (added after an L2 domain review, 2026-07-20).
# An accented character touching a typographic mark forms a two-character non-ASCII run whose
# first char is a valid UTF-8 lead and whose second is a valid continuation — so it passed
# every byte-structural gate and was silently rewritten. 413 of 420 such adjacencies corrupted.
# The corpus sweep that preceded this review found ZERO false positives, because the prose it
# swept puts SPACES around its em-dashes; a clean sweep proves only the absence of failures
# your corpus can express. These are the shapes it could not.
TYPOGRAPHY_ADJACENCIES = [
    "The best CAFÉ—hands down.",
    "JOSÉ—the new hire—starts Monday.",
    "ANDRÉ’s patch landed.",
    "Das Maß—so groß wie nie.",
    "Er sagte „Gruß“ zum Abschied.",
    "PERÚ… y después",
    "le café—“le meilleur”",
    "Ñ—the eñe—is Spanish.",
    "bore Ø±0.05 mm tolerance",
    "Þ—the thorn letter—is Icelandic.",
    "Größe—Straße",
    "FRANÇOIS—a name",
    "MÜNCHEN—die Stadt",
    "naïve—but earnest",
]


@pytest.mark.parametrize("text", TYPOGRAPHY_ADJACENCIES)
def test_accented_character_touching_typography_is_never_rewritten(text):
    scan = scan_text(text)
    assert scan.text == text, f"MANGLED legitimate typography: {text!r} -> {scan.text!r}"
    assert not scan.repairs


def test_multi_pass_cannot_overshoot_a_correct_repair():
    """The gate's second job. Repairing a doubly-encoded span used to produce the RIGHT answer
    at pass 1 and then corrupt it at pass 2, because the correct output was itself a valid
    two-character adjacency — the module's own success path walking onto its own landmine."""
    correct = "CAFÉ—hands down"
    assert scan_text(mojibake(correct)).text == correct
    # Tightened after L3 pointed out the disjunction below used to accept the un-repaired
    # string as a pass — an assertion that would have survived the bug it was guarding.
    assert scan_text(mojibake(correct, levels=2)).text == correct


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_text_is_never_repaired(text):
    scan = scan_text(text)
    assert scan.text == text, f"MANGLED legitimate text: {text!r} -> {scan.text!r}"
    assert not scan.repairs


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_text_is_not_even_flagged(text):
    """A false SUSPECT flag is cheap compared to a false repair, but it still cries wolf on
    every capture containing an accent — so the flag has to be quiet on real language too."""
    assert not scan_text(text).suspect, f"false suspect flag on {text!r}"


@pytest.mark.parametrize("text", AMBIGUOUS_BUT_NEVER_MODIFIED)
def test_ambiguous_text_may_be_flagged_but_is_never_modified(text):
    scan = scan_text(text)
    assert scan.text == text
    assert not scan.repairs


def test_ACCEPTED_LIMITATION_text_about_mojibake_is_repaired_like_mojibake():
    """The irreducible ambiguity, pinned so it is a known trade rather than a surprise.

    Text that CONTAINS mojibake and text that DISCUSSES mojibake are byte-identical, so a
    forensic note quoting a corrupt spelling gets "repaired" like the real thing. Narrowing
    around code fences or log quoting would be a fragile heuristic guarding a rare case, and
    the rewrite is recorded in the receipt rather than silent. Worth knowing when reading this
    project's own defect notes back out of a store."""
    scan = scan_text("log shows `Ã¢` in memory.db")
    assert scan.text == "log shows `â` in memory.db"
    assert scan.repairs  # not silent — the receipt says exactly what changed


def test_pure_ascii_is_returned_untouched():
    text = "a perfectly ordinary turn with no accents at all"
    scan = scan_text(text)
    assert scan.text is text  # fast path — identical object, no scanning cost
    assert scan.clean


def test_repair_is_idempotent():
    once = scan_text(mojibake("an em—dash and “quotes”")).text
    assert scan_text(once).text == once


# --------------------------------------------------------------------------------------
# 3. unprovable damage is FLAGGED, never guessed at
# --------------------------------------------------------------------------------------

def test_damage_touching_an_accented_letter_is_still_flagged():
    """A run is ASCII-delimited, so mojibake that touches a legitimate accent is absorbed into
    that accent's run and no longer STARTS with a lead byte. An index-0-only suspect test
    missed it entirely: not repaired, not flagged, `clean` True — a silent store write, which
    is the one outcome this module exists to prevent."""
    scan = scan_text("café" + mojibake("—") + "bar")
    assert not scan.repairs      # unprovable — the run opens with a legitimate 'é'
    assert scan.suspect          # but NOT silent
    assert not scan.clean


def test_orphaned_lead_byte_is_flagged_not_invented():
    """THE ember signature: the store held b'\\xc3\\xa2' — the UTF-8 encoding of 'â', the
    orphaned FIRST byte of an em-dash whose other two bytes never arrived. The character it
    was is UNRECOVERABLE, so the shield must leave the bytes alone and say so."""
    text = "the fix works â in every token"
    scan = scan_text(text)
    assert scan.text == text          # byte-for-byte untouched — no guess
    assert not scan.repairs
    assert scan.suspect == ("â",)     # …but not silent


def test_replacement_character_is_flagged():
    scan = scan_text("an upstream decoder gave up � here")
    assert scan.suspect
    assert not scan.repairs


def test_truncated_multibyte_character_is_flagged_not_completed():
    """The chunk-split failure in its other shape: TWO of an em-dash's three bytes arrived.
    The detector must not invent the missing third byte to "finish" the character."""
    truncated = mojibake("—")[:2]  # 0xE2 0x80 — a 3-byte sequence one byte short
    scan = scan_text(f"broken {truncated} span")
    assert scan.text == f"broken {truncated} span"
    assert not scan.repairs
    assert scan.suspect == (truncated,)


def test_a_valid_two_byte_sequence_is_still_repaired_even_when_it_looks_odd():
    """Guard against over-correcting the rule above: 'Ã¿' really IS the mojibake of 'ÿ'
    (0xC3 0xBF is valid UTF-8), so it must repair, not flag."""
    scan = scan_text("broken Ã¿ span")
    assert scan.text == "broken ÿ span"


def test_receipt_is_bounded_and_describes_the_upstream_origin():
    scan = scan_text(mojibake("an em—dash " * 40))
    receipt = scan.receipt()
    assert receipt["repaired"] == len(scan.repairs)
    assert len(receipt["repairs"]) <= 8
    assert all(len(before) <= 41 for before, _after in receipt["repairs"])
    assert "shield, not cure" in receipt["note"].lower()


# --------------------------------------------------------------------------------------
# 4. the structural guarantee: the shield cannot be bypassed at the capture boundary
# --------------------------------------------------------------------------------------

def test_capture_request_repairs_and_attaches_a_receipt():
    req = CaptureRequest(content="[assistant] " + mojibake("an em—dash"))
    assert req.content == "[assistant] an em—dash"
    assert req.metadata is not None
    assert req.metadata[RECEIPT_KEY]["repaired"] == 1


def test_capture_request_flags_unprovable_damage_without_editing_it():
    req = CaptureRequest(content="the fix works â in every token")
    assert req.content == "the fix works â in every token"
    assert req.metadata[RECEIPT_KEY]["suspect"] == ["â"]


def test_capture_request_leaves_clean_content_and_metadata_alone():
    req = CaptureRequest(content="[user] a normal turn", metadata={"mine": 1})
    assert req.content == "[user] a normal turn"
    assert req.metadata == {"mine": 1}  # no receipt noise on the common path


def test_dataclasses_replace_does_not_carry_a_stale_receipt():
    """`replace()` re-runs __post_init__ but COPIES the old metadata, so a clean replacement
    would inherit a receipt describing content it no longer holds — an audit trail asserting a
    repair that is not in the text. Found by L3; no caller does this, but a value object whose
    invariant holds only while nobody uses the stdlib on it does not have an invariant."""
    import dataclasses

    dirty = CaptureRequest(content=mojibake("an em—dash"))
    assert RECEIPT_KEY in dirty.metadata
    clean = dataclasses.replace(dirty, content="a perfectly ordinary turn")
    assert clean.content == "a perfectly ordinary turn"
    assert not (clean.metadata or {}).get(RECEIPT_KEY)


def test_capture_request_preserves_caller_metadata_alongside_the_receipt():
    req = CaptureRequest(content=mojibake("café"), metadata={"mine": 1})
    assert req.metadata["mine"] == 1
    assert RECEIPT_KEY in req.metadata
