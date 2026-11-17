"""levain.desk.board_relay — relayed provenance (levain#97, P3): a board record whose authority is a band answer.

The desk relays most of the operator's decisions: the answer is in the band, the record is written by the desk. A
``--source <np-id>`` record is therefore ``grade: relayed`` (the writer's, never a caller's), ``author.kind: agent``, and
carries ``source: {np, producer, question_sha256, answer_sha256}``. The one rule the page names: his sentence travels,
never the rule read into it, so the record's ``words`` must be a VERBATIM substring of the answer (an item quotes an
excerpt; the whole answer is not required). The digests are sha256 of the question and answer text as the band store
holds them, so the citation cannot later attach to a different answer.

Three things live here, and nothing else does:

* :class:`BandSource`: the one way a row is read by id. :class:`NativeBandSource` is levain's own (a
  :class:`~levain.consent.questions.QuestionStore`); the native source refuses an ``np-`` id (flow's band; pinned by a test), so
  its source is flow's work to supply behind this same interface.
* :func:`relay_source`: the WRITE side. Reads the row at write time and refuses, or returns the ``source`` object.
* :func:`verify_relayed` and :func:`check_manifest`: the READ side, pure over their inputs. They report the row's OWN
  grade and never promote it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from levain.desk.board_store import BoardError, HASH_RE, NP_RE

__all__ = ["BandRefusal", "BandRow", "BandSource", "NativeBandSource", "ROW_GRADES", "Verdict", "band_source",
           "check_manifest", "digest", "relay_source", "verify_relayed"]

#: the grades a band answer can carry, and how a verdict says them. An unlisted grade is refused, never guessed at.
ROW_GRADES = {
    "signed-phone": "a passkey-signed answer",
    "signed-terminal": "a terminal-signed answer",
    "countersigned": "a countersigned answer",
    "signed": "a signed answer",
    "unsigned": "an unsigned answer",
}
SOURCE_KEYS = ("np", "producer", "question_sha256", "answer_sha256")


class BandRefusal(Exception):
    """A row that cannot be read, with the reason (unknown, unanswered, not this source's id form)."""


@dataclass(frozen=True)
class BandRow:
    """One band row as a relay needs it: the question text, the answer text, the answer's own grade, the producer."""

    question: str
    answer: str
    answer_grade: str
    producer: str


class BandSource(Protocol):
    def row(self, np_id: str) -> BandRow:
        """The row, or raise :class:`BandRefusal` naming why not."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    text: str
    grade: str | None = None        # the ROW's own grade when it was read; None when it could not be

    def __str__(self) -> str:
        return self.text


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NativeBandSource:
    """Levain's own band (``questions.db``). It reads a ``q-…`` id or a number. An ``np-`` id is flow's, so it is unknown
    here. The native store records no signature (``Answer.signed`` is None unless a host sets it), which reads as unsigned:
    a grade the store cannot vouch for is never promoted. An option answer's text is its option key, as stored."""

    def __init__(self, store: Any, producer: str = "levain") -> None:
        self._store = store
        self._producer = producer

    def row(self, np_id: str) -> BandRow:
        try:
            q = self._store.get(np_id)
        except Exception as exc:                            # noqa: BLE001 - any read failure is a refusal, never a crash
            raise BandRefusal(f"the band store could not be read ({type(exc).__name__})") from None
        if q is None:
            raise BandRefusal(f"{np_id} is not a row this band holds")
        if q.answer is None:
            raise BandRefusal(f"{np_id} is unanswered")
        signed = getattr(q.answer, "signed", None)
        return BandRow(q.what, q.answer.value, "signed" if signed else "unsigned", self._producer)


def band_source(directory: Path | str | None = None) -> BandSource:
    """The source ``--source`` reads: levain's native band, at ``directory`` or the install's consent directory."""
    from levain.consent.questions import QuestionStore
    return NativeBandSource(QuestionStore(directory))


def _grade_words(grade: str) -> str:
    if grade not in ROW_GRADES:
        raise BandRefusal(f"the row carries a grade this reader does not know ({grade!r})")
    return ROW_GRADES[grade]


def _row_of(source: BandSource, np_id: str) -> BandRow:
    try:
        row = source.row(np_id)
    except BandRefusal:
        raise
    except Exception as exc:                                # noqa: BLE001 - a source that raises is an unreadable row
        raise BandRefusal(f"the band source failed ({type(exc).__name__})") from None
    if not isinstance(row, BandRow) or not isinstance(row.question, str) or not isinstance(row.answer, str) \
            or not isinstance(row.answer_grade, str) or not isinstance(row.producer, str) or not row.producer:
        raise BandRefusal(f"{np_id} was read as something that is not a band row")
    _grade_words(row.answer_grade)
    return row


def relay_source(np_id: str, words: str, source: BandSource) -> dict:
    """The WRITE side: read the row now, and refuse unless it is answered and ``words`` is a verbatim, non-blank substring
    of the answer. Returns the record's ``source`` object. Raises BoardError."""
    if not isinstance(np_id, str) or not NP_RE.fullmatch(np_id):
        raise BoardError(f"--source {np_id!r} is not np-xxxxxxxx")
    if not isinstance(words, str) or not words.strip():
        raise BoardError("a relayed record must carry the words it relays: they must be a verbatim excerpt of the answer")
    try:
        row = _row_of(source, np_id)
    except BandRefusal as exc:
        raise BoardError(f"--source {np_id}: {exc}") from None
    if words not in row.answer:
        raise BoardError(f"--source {np_id}: the words are not a verbatim excerpt of the answer: {words!r} (his sentence "
                         f"travels, never a paraphrase; quote exactly what the answer says)")
    return {"np": np_id, "producer": row.producer, "question_sha256": digest(row.question),
            "answer_sha256": digest(row.answer)}


def verify_relayed(rec: Mapping[str, Any], band_source: BandSource) -> Verdict:
    """The READ side, pure over its inputs. Re-reads the row, checks both digests and that ``words`` still occur verbatim,
    and says the ROW'S OWN grade (never the record's, never promoted)."""
    src = rec.get("source") if isinstance(rec, Mapping) else None
    if rec.get("grade") != "relayed" or not isinstance(src, Mapping):
        return Verdict(False, "not a relayed record: grade is not relayed or there is no source")
    if set(src) != set(SOURCE_KEYS) or not all(isinstance(src[k], str) for k in SOURCE_KEYS):
        return Verdict(False, f"malformed source: it must carry exactly {', '.join(SOURCE_KEYS)}, each a string")
    author = rec.get("author")
    if not isinstance(author, Mapping) or author.get("kind") != "agent":
        return Verdict(False, "a relayed record's author must be an agent: a relay is the desk's act")
    np_id = src["np"]
    try:
        row = _row_of(band_source, np_id)
    except BandRefusal as exc:
        return Verdict(False, f"{np_id} unreadable: {exc}")
    if digest(row.answer) != src["answer_sha256"] or digest(row.question) != src["question_sha256"]:
        return Verdict(False, "digest mismatch: the answer changed or the citation points elsewhere", row.answer_grade)
    if row.producer != src["producer"]:
        return Verdict(False, f"digest mismatch: {np_id} is now served by {row.producer!r}, the record names "
                              f"{src['producer']!r}", row.answer_grade)
    fields = rec.get("fields")
    words = fields.get("words") if rec.get("op") == "amend" and isinstance(fields, Mapping) else rec.get("words")
    if not isinstance(words, str) or not words.strip() or words not in row.answer:
        return Verdict(False, f"the record's words are not a verbatim excerpt of {np_id}'s answer", row.answer_grade)
    return Verdict(True, f"relaying {np_id}, {_grade_words(row.answer_grade)}", row.answer_grade)


def check_manifest(rec: Mapping[str, Any], row: BandRow, manifest_sha256: str | None = None) -> Verdict:
    """P3-b, for ``begin-track`` and ``track``: the question must carry the manifest's sha256, so a signed "yes" binds
    the exact manifest. ``manifest_sha256`` is the digest to hold the question to; None takes ``rec['manifest_sha256']``
    (a ``begin-track`` carries it itself; a ``track`` does not, so the verb that folds the one ``begin-track`` of its
    migration passes that record's digest). Also checks the record's own ``source.question_sha256`` is this question."""
    want = manifest_sha256 if manifest_sha256 is not None else rec.get("manifest_sha256")
    if not isinstance(want, str) or not HASH_RE.fullmatch(want):
        return Verdict(False, "no manifest sha256 to check the question against")
    src = rec.get("source")
    if isinstance(src, Mapping) and src.get("question_sha256") != digest(row.question):
        return Verdict(False, "digest mismatch: the record cites a different question than the row holds", row.answer_grade)
    if not re.search(rf"(?<![0-9a-f]){want}(?![0-9a-f])", row.question):
        return Verdict(False, "the question does not carry the manifest's sha256: the answer cannot bind this manifest",
                       row.answer_grade)
    return Verdict(True, "the question carries the manifest's sha256", row.answer_grade)
