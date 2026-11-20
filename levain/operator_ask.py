"""A reply that puts a question to the operator in prose.

What an agent needs from the operator (a decision, a pick, a price, a confirmation) is a band row, so the operator
sees it on every surface and the row is the record. :func:`detect` reads a reply's text for the shapes such an ask takes
in prose; :func:`refusal` is the words that send the agent back to file the row. Deterministic (no model), pure text
in and out, stdlib only, and it imports nothing from levain so any caller can use it. A shape needs the form of an ask
to the operator: a question sentence addressed to him, or an imperative that asks him for a word or a pick.

The caller decides whether a row was filed in the turn, and what to do with a verdict. ``drop_lines`` are lines the
reply is known to carry that are not the agent's own words (the band block every reply reprints): they are taken out
before anything is read.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NamedTuple

SHAPES = ("lettered_options", "numbered_options", "say_and_ill", "which_do_you", "confirm_pick", "q_to_you")
TAIL_CHARS = 12000
WHAT_MAX = 1000
EXCERPT_MAX = 300


class Verdict(NamedTuple):
    """What :func:`detect` found: the ``shapes`` that matched (names in :data:`SHAPES`), who the ask is for
    (``addressee``: ``agent`` only on an explicit cue naming another agent, ``operator`` when the asking text offers
    options, invites a reply or speaks to the operator, else ``unclear``), the paragraph it sits in (``excerpt``), and
    the question as one line (``what_hint``, at most :data:`WHAT_MAX` characters)."""
    shapes: tuple[str, ...]
    addressee: str
    excerpt: str
    what_hint: str


def _norm(line: str) -> str:
    """A line as compared with ``drop_lines``: a markdown quote or list lead and spacing ignored, nothing else."""
    return re.sub(r"\s+", " ", re.sub(r"^[ \t>*-]+", "", line)).strip()


_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")

# a row the reply points at: the band header, "row 4", "#4", or a row id
_CITE_BAND = re.compile(r"\bband@\d+")
_CITE_ROW = re.compile(r"\b(?:rows?|items?|questions?)\s+#?(\d+)\b|(?<![\w&])#(\d+)\b", re.I)
_CITE_ID = re.compile(r"\b(?:q|np)-[0-9a-f]{6,}\b")
_ITEM_NUMBER = re.compile(r"^(\d+)\. ")

# a sentence that opens a message to another agent: it and the sentences after it that do not name the operator
_SEAT = r"(?:levain|flow|argus|desk|fan-?in|heads?|lanes?|seats?|codex|claude|nemo|cognitive)"
_MESSAGE_CUES = (
    re.compile(r"(?m)^\W*ROUTE\b"),
    re.compile(r"\bROUTE to \S"),
    re.compile(r"\bto\s+" + _SEAT + r"\b(?:\s+[\w-]{1,12})?\s*:", re.I),
)
# a cue that holds only for its own sentence
_ASIDE_CUES = (
    re.compile(r"(?<![\w.@/])@" + _SEAT + r"\b", re.I),
    re.compile(r"\bSendMessage\b"),
)
_OPERATOR_NAME = re.compile(r"\b(?:phill|phillip|operator)\b", re.I)

_YOU = re.compile(r"\b(?:you|your|you're|you'd|you'll|yours|phill|phillip|operator)\b", re.I)
_ADDRESS = re.compile(r"\b(?:you|your|yours|you're|you'd|you'll)\b", re.I)
# a first-person permission form or a confirmation question, in a question sentence
_PERMISSION = re.compile(r"\b(?:can|may|shall|should|will|would|could) (?:I|we)\b|\bwant me to\b|\bok(?:ay)? to\b|\bdo you\b",
                         re.I)
_CONFIRM_FORM = re.compile(r"\bis that (?:ok|okay|fine|right)\b|\bany (?:objection|preference)s?\b", re.I)
_PICK_Q = re.compile(r"\b(?:your call|up to you|you(?:'d| would)? (?:decide|pick|choose))\b", re.I)
_AMOUNT = re.compile(r"[$£€¥]\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:k\b|usd\b|eur\b|gbp\b|dollars?\b|bucks\b|"
                     r"/\s?(?:hr|hour|mo|month|yr|year)\b|per (?:hour|month|year)\b)", re.I)
_CONFIRM_Q = re.compile(r"\b(?:confirm|pick|approve|sign off|go ahead|proceed|ship it|merge)\b", re.I)
_CONFIRM_ANY = re.compile(r"\bplease (?:confirm|pick|choose|approve)\b", re.I)

# an imperative that asks him for a word or a pick, at the start of a clause or a line
_LEAD = r"(?:^|[.:;!?\-—(]\s*|\n[ \t]*)"
_IMPERATIVE = re.compile(
    _LEAD + r"(?:say\b(?!\s+(?:nothing|that|this|what)\b)|tell me\b|let me know\b|pick (?:one|a|the)\b|"
    r"give me (?:a|the) (?:go|word|price|number)\b|name (?:the|a) (?:price|number|time|day)\b|"
    r"(?:(?:it|that)(?:['’]s| is) )?(?:your (?:call|pick)|up to you)\b|"
    r"(?:reply|answer|respond)\s+(?:with\s+)?(?:a|b|yes|no|go|ok|okay|one of|the word)\b)", re.I)

_LETTER_PAREN = re.compile(r"(?:^|[\s:;,—-])\(([a-h])\)\s+\S", re.I | re.M)
_LETTER_LINE = re.compile(r"^[ \t>*•-]*([a-h])\)\s+\S", re.I | re.M)
_LETTER_OPTION = re.compile(r"\boption\s+([a-h])\b", re.I)
_NUMBERED = re.compile(r"^[ \t>*•-]*\(?(\d{1,2})[.)]\s+\S", re.M)

# a sentence ends at . ! ? and a space; a list line starts a sentence of its own
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n(?=[ \t]*(?:[-*•]|\(?\d{1,2}[.)]|\(?[a-h]\))\s)", re.I)
_MARKER = re.compile(r"\s*(?:[-*•]|\(?\d{1,2}[.)]|\(?[a-h]\))")

_TRAIL = " \t)\"'*_]”’"


def _spans(text: str) -> list[tuple[int, int]]:
    """The sentences of ``text`` as ``(start, end)``: a wrapped line stays inside its sentence, a bare list marker
    ("1.") stays with its item."""
    out: list[tuple[int, int]] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        if _MARKER.fullmatch(text[start:m.start()]):
            continue
        if m.start() > start:
            out.append((start, m.start()))
        start = m.end()
    if start < len(text):
        out.append((start, len(text)))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _is_question(sentence: str) -> bool:
    return sentence.rstrip(_TRAIL).endswith("?")


def _cited(sentence: str, numbers: set[int]) -> bool:
    """Whether the sentence points at a row that already exists: its id, a band header, or a row number the band
    shown (a number the reply was shown, so ``numbers`` empty cites nothing by number)."""
    if _CITE_ID.search(sentence):
        return True
    if numbers and _CITE_BAND.search(sentence):
        return True
    for m in _CITE_ROW.finditer(sentence):
        n = m.group(1) or m.group(2)
        if n is not None and int(n) in numbers:
            return True
    return False


def _blank(segment: str) -> str:
    return re.sub(r"[^\n]", " ", segment)


def _split(paragraph: str, numbers: set[int]) -> tuple[str, list[str], str, list[str]]:
    """``paragraph`` as (operator text, operator sentences, aside text, aside sentences). The texts keep the
    paragraph's lines with the other side's sentences blanked; a sentence that cites a row is on neither side."""
    spans = _spans(paragraph)
    op_text: list[str] = []
    side_text: list[str] = []
    op_sents: list[str] = []
    side_sents: list[str] = []
    cursor = 0
    in_message = False
    for a, b in spans:
        gap, sent = paragraph[cursor:a], paragraph[a:b]
        cursor = b
        op_text.append(gap)
        side_text.append(gap)
        if _cited(sent, numbers):
            op_text.append(_blank(sent))
            side_text.append(_blank(sent))
            continue
        if any(rx.search(sent) for rx in _MESSAGE_CUES):
            in_message, aside = True, True
        elif any(rx.search(sent) for rx in _ASIDE_CUES):
            in_message, aside = False, True
        elif in_message and not _OPERATOR_NAME.search(sent):
            aside = True
        else:
            in_message, aside = False, False
        if aside:
            op_text.append(_blank(sent))
            side_text.append(sent)
            side_sents.append(sent)
        else:
            op_text.append(sent)
            side_text.append(_blank(sent))
            op_sents.append(sent)
    tail = paragraph[cursor:]
    op_text.append(tail)
    side_text.append(tail)
    return "".join(op_text), op_sents, "".join(side_text), side_sents


def _shapes_of(text: str, sentences: list[str]) -> tuple[str, ...]:
    """The shapes of :data:`SHAPES` that ``text`` (its lines) and its ``sentences`` show, in that order."""
    found: list[str] = []
    questions = [s for s in sentences if _is_question(s)]
    to_him = [q for q in questions if _ADDRESS.search(q) or _PERMISSION.search(q) or _CONFIRM_FORM.search(q)]
    told = any(_IMPERATIVE.search(s) for s in sentences)
    letters = {m.group(1).lower() for rx in (_LETTER_PAREN, _LETTER_LINE, _LETTER_OPTION) for m in rx.finditer(text)}
    if {"a", "b"} <= letters and (questions or told):
        found.append("lettered_options")
    numbers = {m.group(1) for m in _NUMBERED.finditer(text)}
    if len(numbers) >= 2 and (to_him or told):
        found.append("numbered_options")
    if told:
        found.append("say_and_ill")
    if any(_PERMISSION.search(q) or _CONFIRM_FORM.search(q) or _PICK_Q.search(q) for q in questions):
        found.append("which_do_you")
    if _CONFIRM_ANY.search(text) or any(_AMOUNT.search(q) or (q in to_him and _CONFIRM_Q.search(q)) for q in questions):
        found.append("confirm_pick")
    if any(_ADDRESS.search(q) for q in questions):
        found.append("q_to_you")
    return tuple(found)


def _line(text: str, limit: int) -> str:
    one = re.sub(r"\s+", " ", re.sub(r"(?m)^[ \t>*#-]+", "", text)).strip()
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


def _verdict(shapes: tuple[str, ...], text: str, sentences: list[str], addressee: str) -> Verdict:
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    asked = [s for s in sentences if _is_question(s)]
    return Verdict(shapes, addressee, _line(paragraphs[-1] if paragraphs else text, EXCERPT_MAX),
                   _line(asked[-1] if asked else (paragraphs[0] if paragraphs else text), WHAT_MAX))


def _read(text: str, dropped: set[str]) -> str:
    """The prose of ``text`` that is the agent's own: fenced code, dropped lines, quoted lines, inline code and URLs
    taken out (the dropped lines are compared as they stand in the reply), and the last :data:`TAIL_CHARS`."""
    text = _FENCE.sub("\n\n", text)
    lines = ["" if _norm(ln) in dropped or ln.lstrip().startswith(">") else ln for ln in text.splitlines()]
    text = _URL.sub(" ", _INLINE.sub(" ", "\n".join(lines)))
    return text[-TAIL_CHARS:]


def detect(text: str, *, drop_lines: Iterable[str] = ()) -> Verdict | None:
    """The :class:`Verdict` for a reply that asks the operator something in prose, or ``None`` (no ask, ``text`` not a
    string, or anything unreadable: this never raises). ``drop_lines``: lines of the reply that are not the agent's
    words, compared by :func:`_norm` and taken out first. A sentence that points at an existing row (its id, the band
    header, a row number the dropped lines carry) is a restatement and is not read. A sentence that opens a message
    to another agent (an explicit cue) is read apart: a verdict only from those is addressed to the ``agent``."""
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        dropped = {_norm(ln) for ln in drop_lines if isinstance(ln, str)}
        dropped.discard("")
        numbers = {int(m.group(1)) for ln in dropped for m in [_ITEM_NUMBER.match(ln)] if m}
        op_texts: list[str] = []
        op_sents: list[str] = []
        side_texts: list[str] = []
        side_sents: list[str] = []
        for para in re.split(r"\n\s*\n", _read(text, dropped)):
            if not para.strip():
                continue
            o_text, o_sents, s_text, s_sents = _split(para, numbers)
            op_texts.append(o_text)
            op_sents += o_sents
            side_texts.append(s_text)
            side_sents += s_sents
        body = "\n\n".join(op_texts)
        shapes = _shapes_of(body, op_sents)
        if shapes:
            asks = bool(set(shapes) - {"confirm_pick", "q_to_you"}) or bool(_YOU.search(" ".join(op_sents)))
            return _verdict(shapes, body, op_sents, "operator" if asks else "unclear")
        side = "\n\n".join(side_texts)
        shapes = _shapes_of(side, side_sents)
        return _verdict(shapes, side, side_sents, "agent") if shapes else None
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
