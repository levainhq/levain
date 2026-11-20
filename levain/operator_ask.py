"""A reply that puts a question to the operator in prose.

What an agent needs from the operator (a decision, a pick, a price, a confirmation) is a band row, so the operator
sees it on every surface and the row is the record. :func:`detect` reads a reply's text for the shapes such an ask takes
in prose; :func:`refusal` is the words that send the agent back to file the row. Deterministic (no model), pure text
in and out, stdlib only, and it imports nothing from levain so any caller can use it. It leans wide: a reply that
only looks like an ask costs one extra row, a missed ask costs the operator's trust.

The caller decides whether a row was filed in the turn, and what to do with a verdict. ``drop_lines`` are lines the
reply is known to carry that are not the agent's own words (the band block every reply reprints): they are taken out
before anything is read.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NamedTuple

SHAPES = ("lettered_options", "numbered_options", "say_and_ill", "which_do_you", "confirm_pick", "q_to_you")
ADDRESSEES = ("operator", "agent", "unclear")
TAIL_CHARS = 12000
WHAT_MAX = 1000
EXCERPT_MAX = 300


class Verdict(NamedTuple):
    """What :func:`detect` found: the ``shapes`` that matched (names in :data:`SHAPES`), who the ask is for
    (``addressee``: ``agent`` only on an explicit cue naming another agent, ``operator`` when the asking text offers
    options, invites a reply or speaks to the operator, else ``unclear``), the paragraph it sits in (``excerpt``), and the question as one line
    (``what_hint``, at most :data:`WHAT_MAX` characters)."""
    shapes: tuple[str, ...]
    addressee: str
    excerpt: str
    what_hint: str


def _norm(line: str) -> str:
    """A line as compared with ``drop_lines``: a markdown quote or list lead and spacing ignored, nothing else."""
    return re.sub(r"\s+", " ", re.sub(r"^[ \t>*-]+", "", line)).strip()


_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")

# a row the reply points at: the band header, "row 4", "#4", or a row id
_CITE_BAND = re.compile(r"\bband@\d+")
_CITE_ROW = re.compile(r"\b(?:rows?|items?|questions?)\s+#?(\d+)\b|(?<![\w&])#(\d+)\b", re.I)
_CITE_ID = re.compile(r"\b(?:q|np)-[0-9a-f]{6,}\b")
_ITEM_NUMBER = re.compile(r"^(\d+)\. ")

# an explicit cue that a paragraph is a message to another agent
_AGENT_CUES = (
    re.compile(r"(?m)^\W*ROUTE\b"),
    re.compile(r"\bROUTE to \S"),
    re.compile(r"(?<![\w.@/])@[A-Za-z][\w-]*"),
    re.compile(r"\bto\s+(?:levain|flow|argus|desk|fan-?in|lane|head|seat|codex)\b[^\n:]{0,20}:", re.I),
    re.compile(r"\bSendMessage\b"),
)

_YOU = re.compile(r"\b(?:you|your|you're|you'd|you'll|yours|phill|phillip|operator)\b", re.I)
_DECIDE = re.compile(
    r"\b(?:which (?:one|option|of|do|would|should|path|way|approach|first)|pick\b(?! up)|choose|prefer|your call|"
    r"up to you|you decide|go with|approve|confirm\b|let me know|tell me|reply|want me to|do you want|would you|"
    r"should (?:i|we)|shall (?:i|we)|ok to|okay to|sign off|decide\b)", re.I)

_LETTER_PAREN = re.compile(r"(?:^|[\s:;,—-])\(([a-h])\)\s+\S", re.I | re.M)
_LETTER_LINE = re.compile(r"^[ \t>*•-]*([a-h])\)\s+\S", re.I | re.M)
_LETTER_OPTION = re.compile(r"\boption\s+([a-h])\b", re.I)
_NUMBERED = re.compile(r"^[ \t>*•-]*\(?(\d{1,2})[.)]\s+\S", re.M)

_SAY_AND_ILL = (
    re.compile(r"\b(?:say|type|reply|answer|respond|send)\b[^.\n?]{0,60}?\band i(?:['’]?ll| will)\b", re.I),
    re.compile(r"\breply(?: with|:)", re.I),
    re.compile(r"\bjust say\b|\bsay (?:the word|go|yes|no|ok|okay|ship)\b", re.I),
    re.compile(r"\b(?:tell me|let me know)\s+(?:which|what|if|whether|when|how|where|who|and|your|to)\b", re.I),
    re.compile(r"\b(?:waiting (?:on|for) you|needs? (?:your|you to)|need a (?:decision|call|pick|answer|ruling) from "
               r"you)\b", re.I),
    re.compile(r"\byour (?:ok|okay|go-?ahead|approval|sign-?off|decision|answer|pick|input|ruling)\b", re.I),
)
_WHICH_DO_YOU_Q = re.compile(
    r"\b(?:which\b[^?]{0,60}\b(?:do|would|should|shall|can|could|want|prefer|like)\b|what do you|do you "
    r"(?:want|prefer|need)|would you (?:like|prefer|rather)|want me to|should (?:i|we)|shall (?:i|we)|"
    r"can (?:i|we) (?:go|proceed|ship|merge|push|run|start)|(?:ok|okay) to|is that (?:ok|okay|fine|right)|"
    r"are you (?:ok|okay|happy|fine)|how (?:do|would) you|any (?:objection|preference))\b", re.I)
_WHICH_DO_YOU_ANY = re.compile(r"\byour call\b|\bup to you\b|\byou(?:'d| would)? (?:decide|pick|choose)\b", re.I)
_AMOUNT = re.compile(r"[$£€¥]\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:k\b|usd\b|eur\b|gbp\b|dollars?\b|bucks\b|"
                     r"/\s?(?:hr|hour|mo|month|yr|year)\b|per (?:hour|month|year)\b)", re.I)
_CONFIRM_Q = re.compile(r"\b(?:confirm|pick|approve|sign off|go ahead|proceed|ship it|merge)\b", re.I)
_CONFIRM_ANY = re.compile(r"\bplease (?:confirm|pick|choose|approve)\b", re.I)

_TRAIL = " \t)\"'*_]”’"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _is_question(sentence: str) -> bool:
    return sentence.rstrip(_TRAIL).endswith("?")


def _questions(text: str) -> list[str]:
    return [s for s in _sentences(text) if _is_question(s)]


def _cited(paragraph: str, numbers: set[int]) -> bool:
    """Whether the paragraph points at a row that already exists: its id, a band header, or a row number the band
    shown (a number the reply was shown, so ``numbers`` empty cites nothing by number)."""
    if _CITE_ID.search(paragraph):
        return True
    if numbers and _CITE_BAND.search(paragraph):
        return True
    for m in _CITE_ROW.finditer(paragraph):
        n = m.group(1) or m.group(2)
        if n is not None and int(n) in numbers:
            return True
    return False


def _shapes_of(text: str) -> tuple[str, ...]:
    """The shapes of :data:`SHAPES` that ``text`` shows, in that order."""
    found: list[str] = []
    questions = _questions(text)
    cue = bool(questions) or bool(_DECIDE.search(text))
    letters = {m.group(1).lower() for rx in (_LETTER_PAREN, _LETTER_LINE, _LETTER_OPTION) for m in rx.finditer(text)}
    if {"a", "b"} <= letters and cue:
        found.append("lettered_options")
    numbers = {m.group(1) for m in _NUMBERED.finditer(text)}
    if len(numbers) >= 2 and _DECIDE.search(text):
        found.append("numbered_options")
    if any(rx.search(text) for rx in _SAY_AND_ILL):
        found.append("say_and_ill")
    if _WHICH_DO_YOU_ANY.search(text) or any(_WHICH_DO_YOU_Q.search(q) for q in questions):
        found.append("which_do_you")
    if _CONFIRM_ANY.search(text) or any(_AMOUNT.search(q) or _CONFIRM_Q.search(q) for q in questions):
        found.append("confirm_pick")
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if paragraphs:
        last = paragraphs[-1]
        last_line = [ln for ln in last.splitlines() if ln.strip()][-1]
        asked = _questions(last)
        if asked and (_is_question(last_line.strip()) or any(_YOU.search(q) for q in asked)):
            found.append("q_to_you")
    return tuple(found)


def _line(text: str, limit: int) -> str:
    one = re.sub(r"\s+", " ", re.sub(r"(?m)^[ \t>*#-]+", "", text)).strip()
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


def _verdict(shapes: tuple[str, ...], text: str, addressee: str) -> Verdict:
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    asked = _questions(text)
    return Verdict(shapes, addressee, _line(paragraphs[-1] if paragraphs else text, EXCERPT_MAX),
                   _line(asked[-1] if asked else (paragraphs[0] if paragraphs else text), WHAT_MAX))


def detect(text: str, *, drop_lines: Iterable[str] = ()) -> Verdict | None:
    """The :class:`Verdict` for a reply that asks the operator something in prose, or ``None`` (no ask, ``text`` not a
    string, or anything unreadable: this never raises). ``drop_lines``: lines of the reply that are not the agent's
    words, compared by :func:`_norm` and taken out first. A paragraph that points at an existing row (its id, the band
    header, a row number the dropped lines carry) is a restatement and is not read. A paragraph that is a message to
    another agent (an explicit cue) is read apart: a verdict only from those is addressed to the ``agent``."""
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        dropped = {_norm(ln) for ln in drop_lines if isinstance(ln, str)}
        dropped.discard("")
        numbers = {int(m.group(1)) for ln in dropped for m in [_ITEM_NUMBER.match(ln)] if m}
        text = text[-TAIL_CHARS:]
        text = _URL.sub(" ", _INLINE.sub(" ", _FENCE.sub("\n\n", text)))
        lines = ["" if _norm(ln) in dropped else ln for ln in text.splitlines()]
        operator: list[str] = []
        aside: list[str] = []
        for para in re.split(r"\n\s*\n", "\n".join(lines)):
            if not para.strip() or _cited(para, numbers):
                continue
            (aside if any(rx.search(para) for rx in _AGENT_CUES) else operator).append(para)
        body = "\n\n".join(operator)
        shapes = _shapes_of(body) if body else ()
        if shapes:
            asks = bool(set(shapes) - {"confirm_pick", "q_to_you"}) or bool(_YOU.search(body))
            return _verdict(shapes, body, "operator" if asks else "unclear")
        side = "\n\n".join(aside)
        shapes = _shapes_of(side) if side else ()
        return _verdict(shapes, side, "agent") if shapes else None
    except Exception:
        return None


def refusal(filer: str, verdict: Verdict | None = None) -> str:
    """The words that end the turn's refusal: file the ask as a row (``filer`` is the command that files one, with its
    own flags), options and recommendation in the row, and the reply may restate it. ``verdict``: when given, the
    paragraph it was read from is quoted back."""
    read = f' It read this as an ask: "{verdict.excerpt}".' if verdict is not None and verdict.excerpt else ""
    return ("[Levain NEEDS YOU band] Your reply asks the operator for something in prose (a decision, a pick between "
            "options, a price, a confirmation, an answer), and no band row was filed for it in this turn." + read +
            " What the operator must answer is a row, so it reaches him on every surface and not only this terminal: "
            f"run `{filer}`, with your options and your recommendation in the row itself, not in prose. You may "
            "restate it in your reply; the row is the record. Then end your turn. If this reply asked the operator "
            "nothing, end your turn again as it is.")
