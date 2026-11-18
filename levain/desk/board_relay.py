"""levain.desk.board_relay — relayed provenance (levain#97, P3): a board record whose authority is a band answer.

The desk relays most of the operator's decisions: the answer is in the band, the record is written by the desk. A
``--source <q-id>`` record is therefore ``grade: relayed`` (the writer's, never a caller's), ``author.kind: agent``, and
carries ``source: {np, producer, question_sha256, answer_sha256}``. The one rule the page names: the operator's sentence travels,
never the rule read into it, so the record's ``words`` must be a VERBATIM substring of the answer (an item quotes an
excerpt; the whole answer is not required). The digests are sha256 of the question and answer text as the band store
holds them, so the citation cannot later attach to a different answer.

Three things live here, and nothing else does:

* :class:`BandSource`: the one way a row is read by id. :class:`NativeBandSource` is levain's own (a
  :class:`~levain.consent.questions.QuestionStore`, ``q-`` ids, producer ``native``); it refuses an ``np-`` id (flow's band;
  pinned by a test), so flow's source is T12's work behind this same interface.
* :func:`relay_source`: the WRITE side. Reads the row at write time and refuses, or returns the ``source`` object.
* :func:`verify_relayed` and :func:`check_manifest`: the READ side, pure over their inputs. They report the row's OWN
  grade and never promote it.

Known edges, deliberate: a short excerpt ("yes") passes the verbatim check, since the operator's whole answer is often one word (the
verifier reports the row, so a reader sees the full answer); the band read runs under the board's exclusive lock so the
digests are those of the answer at write time (bounded by the band store's busy timeout); an ``unknown``-graded answer can
be relayed and is reported as exactly that, never promoted; :class:`NativeBandSource` holds a QuestionStore with no explicit
close (one-shot CLI). The band a relayed CAUSE is re-read through is chosen by whoever runs the cut (``--band-dir``): an
asserted cut is a local-trust write, so a band dir fabricated on this machine with the same q-id, question and answer would
pass; the citation binds the row's text, not where it is read from. :class:`LazyBand` is single-threaded (the CLI is one-shot
and its one multi-read caller, ``abandon_track``'s cut loop, is sequential); it holds no lock.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from levain.desk.board_store import BoardError, GRADE_RELAYED, HASH_RE, NP_RE

__all__ = ["BandRefusal", "BandRow", "BandSource", "LazyBand", "NativeBandSource", "ROW_GRADES", "Verdict", "band_source",
           "check_manifest", "digest", "relay_source", "verify_relayed"]

#: the grades a band answer can carry, and how a verdict says them. An unlisted grade is refused, never guessed at.
ROW_GRADES = {
    "signed-phone": "a passkey-signed answer",
    "signed-terminal": "a terminal-signed answer",
    "countersigned": "a countersigned answer",
    "signed": "a signed answer",
    "unsigned": "an unsigned answer",
    "unsigned-phone": "an unsigned answer (phone)",
    "unsigned-terminal": "an unsigned answer (terminal)",
    "unknown": "an answer whose signing the band cannot say",
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


#: a native question id as ``QuestionStore`` mints it (``q-`` and 24 hex); a bare band number is a view-relative handle, never a citation
Q_RE = re.compile(r"q-[0-9a-f]{24}")
#: what relayed words may not carry, on the write side and the read side alike: they are one line of the answer
NOT_ONE_LINE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
NATIVE_PRODUCER = "native"


class NativeBandSource:
    """Levain's own band (``questions.db``). It reads a native ``q-<hex>`` id; an ``np-`` id is flow's (T12's reader), so it
    is not a row this band holds. The row's grade is the STORE'S CLAIM of a signature (``board_producers._grade``, the one
    grade function the board's evidence and this relay share), never a verification. An option answer's text is its option key,
    as stored."""

    def __init__(self, store: Any, producer: str = NATIVE_PRODUCER) -> None:
        self._store = store
        self._producer = producer

    def row(self, np_id: str) -> BandRow:
        if not isinstance(np_id, str) or not Q_RE.fullmatch(np_id):
            raise BandRefusal(f"{np_id} is not a row this band holds")
        try:
            q = self._store.get(np_id)
        except Exception as exc:                            # noqa: BLE001 - any read failure is a refusal, never a crash
            raise BandRefusal(f"the band store could not be read ({type(exc).__name__})") from None
        if q is None:
            path = getattr(self._store, "path", None)
            try:
                missing = path is not None and (not path.exists() or path.stat().st_size == 0)
            except OSError:
                missing = False
            if missing:
                raise BandRefusal(f"no band store at {getattr(self._store, 'directory', path)}")
            raise BandRefusal(f"{np_id} is not a row this band holds")
        if q.answer is None:
            raise BandRefusal(f"{np_id} is unanswered")
        from levain.desk import board_producers
        return BandRow(q.what, q.answer.value, board_producers._grade(self._store, q), self._producer)


def band_source(directory: Path | str | None = None) -> BandSource:
    """The source ``--source`` reads: levain's native band, at ``directory`` or the install's consent directory."""
    from levain.consent.questions import QuestionStore
    return NativeBandSource(QuestionStore(directory))


class LazyBand:
    """The band the CLI hands the writer: :func:`band_source` is built on the first read, inside the guarded path, so a bad
    ``--band-dir`` or a store that cannot be opened is a refusal (:func:`_row_of` maps any raise), never a traceback."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = directory
        self._source: BandSource | None = None

    def row(self, np_id: str) -> BandRow:
        if self._source is None:
            self._source = band_source(self._directory)
        return self._source.row(np_id)


NP_UNREADABLE = ("np- rows are flow's band: their reader arrives with T12; cite a q- row (a native question id), or --sign")


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


def check_source_id(np_id: str) -> None:
    """The one validator of a ``--source`` id: a native q-<24 hex> passes; anything else, an np- id included, is refused.
    Called by :func:`relay_source` and by the store's ``add`` BEFORE an idempotent key hit can return. Raises BoardError."""
    if isinstance(np_id, str) and (NP_RE.fullmatch(np_id) or np_id.startswith("np-")):    # any np- width: flow's band, T12's
        raise BoardError(f"--source {np_id}: {NP_UNREADABLE}")
    if not isinstance(np_id, str) or not Q_RE.fullmatch(np_id):
        raise BoardError(f"--source {np_id!r} is not a native q-<24 hex> question id")


def relay_source(np_id: str, words: str, source: BandSource, manifest_sha256: str | None = None, *,
                 require_manifest: bool = False) -> dict:
    """The WRITE side: read the row now, and refuse unless it is answered and ``words`` is a verbatim, non-blank substring
    of the answer. Returns the record's ``source`` object. With ``require_manifest`` (a ``begin-track`` or ``track``, P3-b)
    the SAME row must also pass :func:`check_manifest` against ``manifest_sha256``: its question carries that digest, so the
    relayed "yes" binds the exact manifest; a missing digest fails closed. The requirement is the flag, never the digest's
    presence: a digest given without it is a caller's mistake (ValueError). Raises BoardError."""
    if manifest_sha256 is not None and not require_manifest:
        raise ValueError("relay_source: a manifest digest was given without require_manifest")
    check_source_id(np_id)
    if not isinstance(words, str) or not words.strip():
        raise BoardError("a relayed record must carry the words it relays: they must be a verbatim excerpt of the answer")
    if NOT_ONE_LINE.search(words):                          # the one-line rule decide already holds (board_store._one_line), on every verb
        raise BoardError("relayed words must be one line with no control characters: quote one line of the answer")
    try:
        row = _row_of(source, np_id)
    except BandRefusal as exc:
        raise BoardError(f"--source {np_id}: {exc}") from None
    if words not in row.answer:
        raise BoardError(f"--source {np_id}: the words are not a verbatim excerpt of the answer: {words!r} (the operator's sentence "
                         f"travels, never a paraphrase; quote exactly what the answer says)")
    cited = {"np": np_id, "producer": row.producer, "question_sha256": digest(row.question),
             "answer_sha256": digest(row.answer)}
    if require_manifest:
        said = check_manifest({"source": cited}, row, manifest_sha256 or "")
        if not said.ok:
            raise BoardError(f"--source {np_id}: {said.text}")
    return cited


def verify_relayed(rec: Mapping[str, Any], band_source: BandSource) -> Verdict:
    """The READ side, pure over its inputs. Re-reads the row, checks both digests and that ``words`` still occur verbatim,
    and says the ROW'S OWN grade (never the record's, never promoted)."""
    if not isinstance(rec, Mapping):
        return Verdict(False, "not a relayed record: it is not a mapping")
    from levain.desk.board_sign import grade_sig_problem
    structural = grade_sig_problem(rec)                     # the rule verify_record holds: relayed or signed, never both
    if structural is not None:
        return Verdict(False, f"not a relayed record: {structural}")
    src = rec.get("source")
    if rec.get("grade") != GRADE_RELAYED or not isinstance(src, Mapping):
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
        return Verdict(False, f"producer mismatch: {np_id} is now served by {row.producer!r}, the record names "
                              f"{src['producer']!r}", row.answer_grade)
    if rec.get("op") == "amend":                            # an amend's words live in fields.words and nowhere else
        fields = rec.get("fields")
        if not isinstance(fields, Mapping) or not isinstance(fields.get("words"), str):
            return Verdict(False, "malformed amend: a relayed amend carries its words in a fields mapping", row.answer_grade)
        words = fields["words"]
    else:
        words = rec.get("words")
    if not isinstance(words, str) or not words.strip() or words not in row.answer:
        return Verdict(False, f"the record's words are not a verbatim excerpt of {np_id}'s answer", row.answer_grade)
    if NOT_ONE_LINE.search(words):                          # the write side's one-line rule, held on the read side too
        return Verdict(False, "the record's words are not one line: relayed words quote one line of the answer", row.answer_grade)
    said = f"relaying {np_id}, {_grade_words(row.answer_grade)}"
    if row.producer == NATIVE_PRODUCER and row.answer_grade.startswith(("signed", "countersigned")):
        said += " (per the band store's sign_state; the signature itself is re-verified by the trail verifier)"
    return Verdict(True, said, row.answer_grade)


def check_manifest(rec: Mapping[str, Any], row: BandRow, manifest_sha256: str | None = None) -> Verdict:
    """P3-b, for ``begin-track`` and ``track``: the question must carry the manifest's sha256, so a signed "yes" binds
    the exact manifest. ``manifest_sha256`` is the digest to hold the question to; None takes ``rec['manifest_sha256']``
    (a ``begin-track`` carries it itself; a ``track`` does not, so the verb that folds the one ``begin-track`` of its
    migration passes that record's digest). Also checks the record's own ``source.question_sha256`` is this question."""
    if not isinstance(rec, Mapping):
        return Verdict(False, "not a relayed record: it is not a mapping")
    want = manifest_sha256 if manifest_sha256 is not None else rec.get("manifest_sha256")
    if not isinstance(want, str) or not HASH_RE.fullmatch(want):
        return Verdict(False, "no manifest sha256 to check the question against")
    src = rec.get("source")
    cited = src.get("question_sha256") if isinstance(src, Mapping) else None
    if not isinstance(cited, str) or not HASH_RE.fullmatch(cited):
        return Verdict(False, "the record must cite this question by a lowercase sha256 in its source", row.answer_grade)
    if cited != digest(row.question):
        return Verdict(False, "digest mismatch: the record cites a different question than the row holds", row.answer_grade)
    if not re.search(rf"(?<![0-9a-f]){want}(?![0-9a-f])", row.question, re.IGNORECASE):
        return Verdict(False, "the question does not carry the manifest's sha256: the answer cannot bind this manifest",
                       row.answer_grade)
    return Verdict(True, "the question carries the manifest's sha256", row.answer_grade)
