"""The words the room reads aloud: the reply a person is to hear, cut into chunks a provider will take.

Pure functions of their input, so chunk ``i`` of a reply is the same on every call: a client that fetches chunk 3 after
chunk 2 must get the chunk that follows the one it already played, and a retry must not shift the boundaries. Nothing
here touches the network, a clock or a key.

Three rules shape the text:

* a fenced code block is not read, because spelling code aloud is noise that costs money; it becomes one fixed spoken
  sentence (:data:`CODE_BLOCK_WORDS`), so the listener knows something was skipped;
* the first chunk is short (about :data:`FIRST_CHUNK`) so the first audio starts early, the rest are longer
  (:data:`CHUNK`), and none is over :data:`API_CAP`, the largest input one provider call accepts;
* a reply past :data:`MAX_CHUNKS` chunks is cut and says so (:data:`CUT_WORDS`), rather than silently reading on.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

CODE_BLOCK_WORDS = "A code block, not read aloud."
CUT_WORDS = "The rest is not read aloud."

FIRST_CHUNK = 200       # about: a sentence boundary under it wins, so the first audio is not a fragment
CHUNK = 1500
API_CAP = 4096          # one speech call's input cap (the provider's own); CHUNK stays well under it
MAX_CHUNKS = 40         # the notice counts as one of them

assert 0 < FIRST_CHUNK <= CHUNK < API_CAP

_FENCE_OPEN = re.compile(r"^ {0,3}(?:(`{3,})[^`]*|(~{3,}).*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?…‽])[\"')\]”’]*\s+|(?<=[。！？])")
_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
_ZWJ = "‍"


def replace_code_fences(text: str) -> str:
    """``text`` with each fenced code block (``` or ~~~, the opener at most three spaces in) replaced by
    :data:`CODE_BLOCK_WORDS`. A block closes on a fence line of the same character at least as long as its opener; a
    block that never closes runs to the end, because a reply cut mid-fence is still code. Inline code is left alone
    (a backtick fence's info string holds no backtick, so a line like ```x``` is inline code, not an opener)."""
    out: list[str] = []
    opener: tuple[str, int] | None = None       # (fence character, length) while inside a block
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if opener is None:
            m = _FENCE_OPEN.match(line)
            if m is not None:
                fence = m.group(1) or m.group(2)
                opener = (fence[0], len(fence))
                out.append(CODE_BLOCK_WORDS)
            else:
                out.append(line)
            continue
        m = _FENCE_CLOSE.match(line)
        if m is not None and m.group(1)[0] == opener[0] and len(m.group(1)) >= opener[1]:
            opener = None
    return "\n".join(out)


def _cut_point(word: str, limit: int) -> int:
    """Where to cut ``word`` (no whitespace, longer than ``limit``): at most ``limit`` in, never between a base
    character and the combining marks or joiner that follow it."""
    cut = limit
    while cut > 1 and (unicodedata.combining(word[cut]) or word[cut] == _ZWJ or word[cut - 1] == _ZWJ):
        cut -= 1
    return cut if cut > 1 else limit    # a run of marks longer than the limit is cut rather than looped on


def _take_words(sentence: str, limit: int) -> tuple[str, str]:
    """``sentence`` as (a head of whole words, at most ``limit`` characters; the rest). A first word over ``limit`` is
    cut (:func:`_cut_point`) and its remainder stays one word at the front of the rest. The rest is empty when the
    sentence fits."""
    words = sentence.split()
    head = ""
    for n, word in enumerate(words):
        if len(word) > limit:
            if head:
                return head, " ".join(words[n:])
            cut = _cut_point(word, limit)
            return word[:cut], " ".join([word[cut:], *words[n + 1:]]).strip()
        if head and len(head) + 1 + len(word) > limit:
            return head, " ".join(words[n:])
        head = f"{head} {word}" if head else word
    return head, ""


def _atoms(paragraph: str, limit: int) -> list[str]:
    """``paragraph`` as sentences, and any sentence over ``limit`` as runs of words."""
    atoms: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        sentence = sentence.strip()
        while len(sentence) > limit:
            head, sentence = _take_words(sentence, limit)
            atoms.append(head)
        if sentence:
            atoms.append(sentence)
    return atoms


def chunk_text(text: str, *, first: int = FIRST_CHUNK, size: int = CHUNK) -> list[str]:
    """``text`` as spoken chunks: the first of about ``first`` characters, the rest up to ``size``, none over
    :data:`API_CAP`. Sentences are packed greedily and never split unless one alone is over the limit (the first chunk
    cuts a long opening sentence at a word boundary so the first audio is early); a paragraph break survives inside a
    chunk as a blank line. Empty or blank input is no chunks. Deterministic. Code fences are NOT replaced here (see
    :func:`speakable`)."""
    if not 0 < first <= size < API_CAP:
        raise ValueError("chunk limits must satisfy 0 < first <= size < API_CAP")
    atoms: list[tuple[str, bool]] = []          # (sentence or run of words, begins a paragraph)
    for paragraph in _PARAGRAPH.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        atoms.extend((a, n == 0) for n, a in enumerate(_atoms(paragraph, size)))
    chunks: list[str] = []
    current = ""
    for atom, new_paragraph in atoms:
        if not chunks and not current and len(atom) > first:
            head, atom = _take_words(atom, first)
            chunks.append(head)
            new_paragraph = False
        sep = "" if not current else ("\n\n" if new_paragraph else " ")
        if current and len(current) + len(sep) + len(atom) > (first if not chunks else size):
            chunks.append(current)
            current, sep = "", ""
        if atom:
            current = f"{current}{sep}{atom}"
    if current:
        chunks.append(current)
    return chunks


def speakable(text: str) -> list[str]:
    """The chunks to speak for a reply's ``text``: code fences replaced, then cut by :func:`chunk_text`. A reply over
    :data:`MAX_CHUNKS` chunks keeps its first ``MAX_CHUNKS - 1`` and ends with :data:`CUT_WORDS`."""
    chunks = chunk_text(replace_code_fences(text))
    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS - 1] + [CUT_WORDS]
    return chunks


def reply_text(items: Iterable[Mapping[str, Any]], reply_gseq: int) -> str | None:
    """The text of the reply that ended at ``reply_gseq``, from a thread's stream items (dicts with ``gseq``, ``kind``,
    ``source`` and ``body``): the ``vendor_agent_text`` bodies after the operator's last ``message`` before it, in
    ``gseq`` order, joined by a blank line. None when ``reply_gseq`` is not a ``vendor_turn_ended`` item (a reply still
    running is never read, and a caller cannot name a ``gseq`` that was not a reply's end). The text is only ever the
    record's: nothing a caller supplies is spoken."""
    ordered = sorted((i for i in items if isinstance(i.get("gseq"), int)), key=lambda i: i["gseq"])
    end = next((i for i in ordered if i["gseq"] == reply_gseq), None)
    if end is None or end.get("kind") != "vendor_turn_ended":
        return None
    before = [i for i in ordered if i["gseq"] < reply_gseq]
    start = max((i["gseq"] for i in before if i.get("kind") == "message" and i.get("source") == "operator"), default=-1)
    parts = [i["body"] for i in before
             if i["gseq"] > start and i.get("kind") == "vendor_agent_text" and isinstance(i.get("body"), str) and i["body"].strip()]
    return "\n\n".join(parts)
