"""levain.firing.encoding — the mojibake SHIELD at the capture boundary.

**This is a shield, not a cure. Levain does not own the defect it guards against.**

The defect (verified on a real entity 'ember', 2026-07-19, in the stored ``memory.db``
BYTES — not a terminal rendering artifact): an em-dash arrived in a captured episode as
``b'\\xc3\\xa2'`` (the UTF-8 encoding of ``'\\xe2'`` — i.e. only the FIRST byte of the
em-dash's three, re-encoded), alongside doubly-encoded spans (``'\\xc3\\x83\\xc2\\xa2'``).
4 mangled characters against 49 correct ones, in 2 of 11 episodes — INTERMITTENT, which is
the signature of a multi-byte character split across two streaming chunks and decoded
independently, then latin-1/cp1252-repaired by some layer that should have buffered instead.

Levain owns ZERO bytes of that decode path. ``levain run`` hands an ``LLM`` to the OpenHands
SDK (``run.py``) and reads back ALREADY-DECODED ``MessageEvent`` text
(``agent_reply.message_event_text``); the mangling happens upstream in the
Ollama ``/v1`` -> litellm -> OpenHands streaming chain. So there is no bad decode of OURS to
fix. What levain owns is whether it STORES the damage — and for a product whose pitch is
"a memory you can trust because you can see what's in it", silently persisting mojibake is
disproportionate damage.

Hence the posture, and the two halves are deliberately unequal:

  - **REPAIR only what is PROVABLE.** A span is repaired iff it OPENS with one of four
    lead-byte renderings (:data:`_REPAIR_LEADS` — read that constant before touching any of
    this) AND re-encoding it (cp1252, with a latin-1 fallback for the five bytes cp1252 leaves
    undefined) yields bytes that are STRICTLY valid UTF-8 decoding to control-free,
    replacement-free text. Whole-run-or-nothing: no sub-span search, no scoring heuristic, no
    "most plausible" guess. Naive mojibake repair mangles legitimate text — ftfy carries a
    large cost model precisely because the general problem is ambiguous, and an over-eager
    repair writing WRONG characters into a memory store is a worse failure than leaving
    visible damage. The lead gate is what makes that sentence true rather than aspirational;
    the byte-structural checks alone do NOT, as the measurements in `_REPAIR_LEADS` show.
  - **FLAG everything else.** A span that looks like mojibake but cannot be proven (the
    ``b'\\xc3\\xa2'`` case above is exactly this — one orphaned lead byte is not decodable
    back into an em-dash, the other two bytes are simply GONE) is left BYTE-FOR-BYTE
    UNTOUCHED and reported. The caller records the report as episode metadata and logs it,
    so the damage is visible in the store rather than laundered by a guess.

The scan runs at ``CaptureRequest`` construction (``levain.firing.contract``) — the one
value object every firing-adapter passes through on the way to a store write — so no capture
path, present or future, can write unscanned text. ``structural_invariants_beat_discipline``.

Pure stdlib, no ``anneal``/``openhands`` imports: it stays in the SDK-free test tier and can
be exercised directly against a byte corpus (``tests/test_firing_encoding.py``).

The full defect record — including what has NOT been done (no upstream issue is filed yet,
because the fault has only been observed end-to-end and has not been isolated to a specific
layer of the chain) — is ``docs/upstream-defects.md``, UD-1.

CALIBRATION, and a warning about how it was nearly misread (2026-07-20). A corpus sweep over
223,192 lines of this repo plus flow's project notes — 38,226 of them carrying non-ASCII
characters — modified only 5 lines, every one of them a line that literally DISCUSSES
mojibake. Zero false positives. **That number was reassuring and insufficient**, and it is
recorded here as a caution rather than a credential: the corpus is written in a house style
that puts SPACES around its em-dashes, so it contained almost none of the one shape that
actually breaks this module (an accented character touching a typographic mark). A
domain-lens review that enumerated the byte space instead of sampling real text found 413
corrupting adjacencies in minutes. A clean sweep over text you happen to have proves the
absence of the failures your corpus can express, and nothing more — so the regression corpus
in ``tests/test_firing_encoding.py`` now carries the enumerated adjacencies explicitly.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["EncodingScan", "scan_text", "RECEIPT_KEY", "DETECTOR_ID"]

# The metadata key the receipt lands under on a captured episode.
RECEIPT_KEY = "levain_encoding"
# Bumped when the detector's RULES change, so an old receipt in a store stays interpretable.
DETECTOR_ID = "levain.firing.encoding/1"

# Maximal runs of non-ASCII characters. A complete mojibake sequence is ALWAYS one such run:
# every byte of a UTF-8 multi-byte character is >= 0x80 (leads 0xC2-0xF4, continuations
# 0x80-0xBF), and BOTH latin-1 and cp1252 map every one of those to a non-ASCII character.
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")

# Characters that, at the START of an unrepairable multi-character non-ASCII run, are strong
# evidence of a broken decode. Deliberately TINY: these are the cp1252/latin-1 renderings of
# UTF-8 LEAD bytes (0xC2/0xC3 -> 'Â'/'Ã', 0xE2 -> 'â', 0xD0 -> 'Ð', 0xF0 -> 'ð'). Accented
# letters that merely LOOK similar are excluded on purpose — German "Größe" (ö followed by ß)
# and Portuguese "não" must never be flagged, so no letter that opens a real word in a living
# orthography goes in this set.
_SUSPECT_LEADS = frozenset("ÃÂâÐð")

# The narrower set for the LONE-character rule, where the evidence is weaker (one character,
# no run to corroborate it) and the false-flag risk is correspondingly higher. 'Ð'/'ð' are
# excluded here on measured grounds: a standalone 'ð' is ordinary in text ABOUT language
# (Icelandic eth, phonetics), while a standalone 'Ã'/'Â'/'â' is the observed orphaned-lead
# shape and is not a word in any orthography.
_LONE_SUSPECT_LEADS = frozenset("ÃÂâ")

# THE REPAIR GATE — the single most important constant in this module.
#
# A run is a repair candidate ONLY if its FIRST character is one of these four. They are the
# single-byte renderings of the UTF-8 lead bytes 0xC2, 0xC3, 0xE2 and 0xF0, which between them
# cover the whole of what actually gets mangled in practice: Latin-1 accented letters
# (0xC2/0xC3 leads), General Punctuation — em/en dashes, curly quotes, the ellipsis, bullets,
# arrows (0xE2), and emoji/astral characters (0xF0).
#
# WHY THIS GATE EXISTS — measured, not theorized. Without it, the gates below (strict UTF-8 +
# category checks) admit ordinary typography, because an accented CAPITAL is itself a valid
# UTF-8 lead byte and typographic punctuation is a valid continuation byte. Verified against
# this module before the gate was added:
#
#     "The best CAFÉ—hands down."      ->  "The best CAFɗhands down."
#     "ANDRÉ’s patch landed."          ->  "ANDRɒs patch landed."
#     "Das Maß—so groß wie nie."       ->  "Das Maߗso groß wie nie."
#     "PERÚ… y después"                ->  "PERڅ y después"
#     "bore Ø±0.05 mm tolerance"       ->  "bore ر0.05 mm tolerance"
#
# Of the 420 realistic (accented-capital, typographic-mark) adjacencies, 413 silently corrupted.
# An unspaced em-dash and a curly apostrophe are not exotic — they are house style in most
# published prose, and 'ß' is lowercase and word-final in ordinary German. The receipt made it
# worse rather than better: the corruption was recorded as a "repair", so the audit trail
# asserted the damage was a fix.
#
# The gate ALSO fixes the multi-pass overshoot, in the same stroke and for the same reason: the
# correct output of pass 1 ("CAFÉ—hands down") starts with 'É', which is not a candidate lead,
# so pass 2 cannot touch it. Before the gate, the module's own primary success path — repairing
# a doubly-encoded span — landed on the corruption above at pass 2.
#
# THE COST, stated plainly: mis-decoded Cyrillic, Greek and CJK are no longer REPAIRED (their
# leads are 0xD0-0xDF and 0xE0-0xEF, which are also the ordinary accented lowercase letters
# 'à' through 'ï' — admitting them re-opens the hole above). They are FLAGGED where the run
# carries other evidence, and otherwise pass through untouched. That is the correct trade for a
# memory store: unrepaired damage is visible, a wrong character is not.
_REPAIR_LEADS = frozenset("ÂÃâð")

# U+FFFD: some upstream decoder ALREADY gave up on these bytes. The original is unrecoverable
# by definition, so this is pure evidence, never repairable.
_REPLACEMENT_CHAR = "�"

# Bounded receipt — a capture receipt is provenance, not a second copy of the episode.
_MAX_SAMPLES = 8
_MAX_SAMPLE_CHARS = 40
# A repair pass may reveal another layer beneath it (the doubly-encoded spans in the evidence),
# so re-scan — but a bounded number of times: an unbounded loop on adversarial input is a hang.
_MAX_PASSES = 3


@dataclass(frozen=True)
class EncodingScan:
    """The result of scanning one capture's text.

    ``text`` is the (possibly repaired) content to store. ``repairs`` are the
    ``(before, after)`` pairs actually applied — the receipt that makes a repair AUDITABLE
    rather than an invisible rewrite. ``suspect`` are spans left BYTE-FOR-BYTE UNTOUCHED that
    the detector believes are damaged but cannot prove.
    """

    text: str
    repairs: tuple[tuple[str, str], ...] = ()
    suspect: tuple[str, ...] = ()

    @property
    def repaired(self) -> bool:
        return bool(self.repairs)

    @property
    def clean(self) -> bool:
        """True iff nothing was repaired and nothing looks damaged — the normal capture."""
        return not self.repairs and not self.suspect

    def receipt(self) -> dict:
        """The provenance record for the episode's metadata. Bounded in size; the samples are
        truncated so a receipt can never become a second copy of the turn."""
        return {
            "detector": DETECTOR_ID,
            "repaired": len(self.repairs),
            "repairs": [
                [_clip(before), _clip(after)] for before, after in self.repairs[:_MAX_SAMPLES]
            ],
            "suspect": [_clip(s) for s in self.suspect[:_MAX_SAMPLES]],
            "note": (
                "Upstream (Ollama /v1 -> litellm -> OpenHands) delivered mis-decoded text to "
                "levain's capture boundary; levain repairs only provably double-encoded spans "
                "and leaves the rest untouched and flagged. Shield, not cure."
            ),
        }


def _clip(s: str) -> str:
    """Bound a sample AND make it safely serializable.

    A suspect span can contain a lone surrogate (upstream damage is not required to be
    well-formed), which `json.dumps(..., ensure_ascii=False).encode("utf-8")` refuses — so a
    receipt describing corruption could itself break the store write it was meant to annotate.
    The surrogate is replaced in the SAMPLE only; the episode content is untouched."""
    clipped = s if len(s) <= _MAX_SAMPLE_CHARS else s[:_MAX_SAMPLE_CHARS] + "…"
    return clipped.encode("utf-8", "replace").decode("utf-8")


def _to_bytes(run: str) -> bytes | None:
    """Re-encode a run through the single-byte codec that most likely produced it.

    cp1252 FIRST (it is what Windows-lineage and many "latin-1" decoders actually implement,
    and it is the only one that maps 0x80-0x9F to the printable characters seen in real
    mojibake — '€', '"', '–'), with a per-character latin-1 fallback for the five byte values
    cp1252 leaves undefined (0x81/0x8D/0x8F/0x90/0x9D), which a true latin-1 decoder would
    have rendered as C1 controls. A character encodable by NEITHER (anything above U+00FF that
    is not a cp1252 special — CJK, emoji, most of Unicode) makes the whole run non-mojibake by
    construction: it could not have come from a single-byte misread. Returns ``None`` then."""
    out = bytearray()
    for ch in run:
        for codec in ("cp1252", "latin-1"):
            try:
                out += ch.encode(codec)
                break
            except UnicodeEncodeError:
                continue
        else:
            return None
    return bytes(out)


def _repair_run(run: str) -> str | None:
    """The provable-only repair of ONE non-ASCII run, or ``None`` to leave it alone.

    Every gate here exists to make a FALSE repair impossible on legitimate text; the cost is
    that genuine damage adjacent to a legitimate accented character goes unrepaired (and is
    flagged instead). That trade is deliberate — a wrong character silently written into a
    memory store is the failure this module exists to prevent."""
    if len(run) < 2:
        # A single non-ASCII character cannot be a double-encoded multi-byte sequence: UTF-8
        # needs >= 2 bytes, which is >= 2 characters after a single-byte misread. This is what
        # keeps "café", "naïve", "não" untouched.
        return None
    if run[0] not in _REPAIR_LEADS:
        # THE gate — see _REPAIR_LEADS. Everything below is a structural check on the BYTES;
        # this is the one check on whether the run is plausibly mojibake AT ALL, and without it
        # ordinary typography (an accented capital touching an em-dash) passes every other gate.
        return None
    raw = _to_bytes(run)
    if raw is None:
        return None
    try:
        decoded = raw.decode("utf-8")  # STRICT — any invalid byte sequence disqualifies the run
    except UnicodeDecodeError:
        return None
    if not decoded or decoded == run:
        return None
    if len(decoded) >= len(run):
        # STRUCTURALLY ALWAYS TRUE, kept as an assertion rather than a safety gate — and
        # labelled as such because it was previously documented as one. The run is entirely
        # non-ASCII, so every character encodes to exactly one byte >= 0x80, and any valid
        # UTF-8 sequence over such bytes consumes at least two bytes per character; the result
        # can never be longer. Measured: 0 hits across all 16384 two-character runs and 35041
        # sampled longer runs. It guards a future change to the run definition, nothing more.
        return None
    if _REPLACEMENT_CHAR in decoded:
        return None
    for ch in decoded:
        # Decoding into control characters, unassigned code points, isolated combining marks or
        # private-use characters means the bytes were not a mis-decoded text span — refuse
        # rather than write junk into memory. ("Cs" is unreachable, since a strict UTF-8 decode
        # cannot yield a surrogate; kept for the same defensive reason as the length check.
        # Whitespace is not reachable either: it is ASCII, and this run is entirely non-ASCII.)
        if unicodedata.category(ch) in {"Cc", "Cn", "Cs", "Co", "Mn"}:
            return None
    return decoded


def _one_pass(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Repair every provable run in ``text`` once; return the new text + the repairs applied."""
    repairs: list[tuple[str, str]] = []

    def _sub(match: re.Match[str]) -> str:
        run = match.group(0)
        fixed = _repair_run(run)
        if fixed is None:
            return run
        repairs.append((run, fixed))
        return fixed

    return _NON_ASCII_RUN.sub(_sub, text), repairs


def _suspect_spans(text: str) -> list[str]:
    """Spans that look damaged but were NOT repaired — reported, never modified.

    Three narrow signals:

      - **U+FFFD** anywhere: an upstream decoder already surrendered on those bytes.
      - **A multi-character non-ASCII run opening with a lead-byte rendering**
        (:data:`_SUSPECT_LEADS`) that failed the provable-repair gate — damaged, but missing
        the bytes needed to prove what it was.
      - **A lone lead-byte character standing as its own word**
        (:data:`_LONE_SUSPECT_LEADS`). This is the EXACT ember signature: the store held
        ``b'\\xc3\\xa2'`` — the UTF-8 encoding of ``'â'``, i.e. the orphaned FIRST byte of an
        em-dash whose other two bytes never arrived — sitting between spaces where the dash
        belonged. Unrepairable by construction (the information is gone), and a run of length
        one, so both rules above miss it. Requiring that NEITHER neighbour is alphanumeric is
        what keeps genuine words safe: French "âme" and "l'âme" have a letter on one side,
        "one â two" does not.

    The known, accepted cost of the third rule: text ABOUT a character ("Ã is a letter in
    Portuguese") flags. That is the right direction to be wrong in — a flag ANNOTATES, it never
    edits, so a capture carrying a spurious note is strictly better than one whose corruption
    went unrecorded.
    """
    spans: list[str] = []
    for match in _NON_ASCII_RUN.finditer(text):
        run = match.group(0)
        if _REPLACEMENT_CHAR in run:
            spans.append(run)
        elif len(run) >= 2 and any(ch in _SUSPECT_LEADS for ch in run):
            # ANYWHERE in the run, not just at index 0. Runs are ASCII-delimited, so damage
            # that happens to touch a legitimate accented letter is absorbed into that letter's
            # run and no longer STARTS with a lead: "café" + a mangled em-dash forms the single
            # run "éâ€”", which an index-0 test misses entirely — it was neither repaired
            # nor flagged, and `clean` was True. That is the silent store write this module
            # exists to prevent, so the docstring's promise is now kept by the code.
            spans.append(run)
        elif len(run) >= 2 and any(_is_c1_control(ch) for ch in run):
            # A C1 control (U+0080-U+009F) never occurs in legitimate text. It appears when a
            # decoder read a UTF-8 continuation byte as latin-1, so it is direct evidence of a
            # mis-decode even when no Latin lead survived — the one signal that reaches
            # mangled CJK/Greek/Cyrillic, whose own leads are too ambiguous to flag on.
            spans.append(run)
        elif len(run) == 1 and run in _LONE_SUSPECT_LEADS and _stands_alone(text, match):
            spans.append(run)
    return spans


def _is_c1_control(ch: str) -> bool:
    """A C1 control (U+0080-U+009F). Written as code points, never as literals — a
    module about mis-encoded text must not carry unprintable characters in its source."""
    return 0x80 <= ord(ch) <= 0x9F


def _stands_alone(text: str, match: re.Match[str]) -> bool:
    """True iff the matched single character has no alphanumeric neighbour on either side —
    it is a word unto itself, which no lead-byte rendering ever legitimately is."""
    before = text[match.start() - 1] if match.start() > 0 else ""
    after = text[match.end()] if match.end() < len(text) else ""
    return not (before.isalnum() or after.isalnum())


def scan_text(text: str) -> EncodingScan:
    """Scan ``text`` for upstream mis-decoding: repair the provable, flag the rest, touch
    nothing else.

    Fast-path: text with no non-ASCII characters at all (the overwhelmingly common capture)
    returns immediately with the string unchanged and identical by identity."""
    if not text or text.isascii():
        return EncodingScan(text=text)

    current = text
    repairs: list[tuple[str, str]] = []
    for _ in range(_MAX_PASSES):
        # Re-scan after a successful pass: the evidence carries DOUBLY-encoded spans, where one
        # provable repair exposes another provable layer underneath it.
        current, pass_repairs = _one_pass(current)
        if not pass_repairs:
            break
        repairs.extend(pass_repairs)

    return EncodingScan(
        text=current,
        repairs=tuple(repairs),
        suspect=tuple(_suspect_spans(current)),
    )
