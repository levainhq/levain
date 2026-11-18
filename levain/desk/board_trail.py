"""levain.desk.board_trail: ``levain board trail export|verify`` (levain#104, design 7.4, P6 part A): the records of both board
files, checked by someone who does not trust the machine that wrote them.

THE RULE every line this module prints holds: a verdict is the WEAKEST claim the evidence supports. What the verifier cannot
check is said as unchecked; what is malformed, missing or contradictory is a break (exit 3). Material that CLAIMS a signature
(a signed grade, a ``sig``, a band row graded signed, a sidecar saying ``signed``) is never read as "unsigned" when the
signature is missing or bad: that is a break.

``verify`` re-checks, per file (``items.jsonl`` and ``repos.jsonl`` are two chains, two anchors, two tails, two heads):

* the chain (:func:`levain.desk.board_chain.check_chain`: the cutover over the legacy prefix, an unchained record after it as a
  NAMED problem, a torn line, ``ack-torn`` re-anchoring), reading an acknowledged tear or break as acknowledged and every other
  one as a break;
* every record through its file's OWN fold (``board_state.fold`` for items, ``repos_fold.fold_repos`` for the registry): a
  record the fold rejects is a break, whatever its chain and signature say; after the cutover a record the fold does not apply
  (the state does not allow it) is a break too;
* every signature, through ``board_sign.verify_record`` (the P2 verifier: ``ssh-keygen -Y verify`` in namespace ``levain-board``
  over the canonical bytes, against the authority log's keys), and that a signed record names THIS store (a store with no id
  cannot be compared: a break);
* the writer's policy (``board_sign.POLICY``): after the cutover a decision-class record that is neither signed nor relayed
  (save a cut whose ``cause`` holds) and a record graded ``legacy`` are breaks;
* every ``relayed`` citation, through ``board_relay.verify_relayed`` (both digests, the words verbatim in the answer, the row's
  own grade, never promoted) and, for ``begin-track`` and ``track``, ``board_relay.check_manifest`` against the manifest digest
  the ceremony's ``begin-track`` (signed or relayed, once it verified) recorded.

It prints one verdict line per record in the design's honest vocabulary (``operator, signed with a ceremony`` / ``agent NAME,
relaying np-x (signed answer|unsigned answer)`` / ``asserted by NAME, unsigned`` / ``legacy``), then per file the anchor line
(the span of records the last signed one commits), the unanchored-tail line (never hidden), the relayed lines (signed and
unsigned answers counted apart), and whether each kept head (``--head items=H --head repos=H``) is still on its file's chain (a
head that is gone is a truncation or a rewrite; a file with no head given says truncation was not checked). Every field taken
from a record is printed with its control characters escaped. It exits 3 on any break, 2 when it cannot run.

The trust root is never the bundle. ``verify BUNDLE`` replays the authority log of the verifier's OWN install (``--data-root``,
default the install's) and reads the bundle's keys as claims compared against it (a key the bundle names that the log does not
hold the same way is a break). With no authority log of his own, a signature that verifies under a key the bundle names reads
"signature valid under a key the bundle names; not checked against an authority you hold", and no anchor is claimed. Given the
same trust root, a bundle and the live store give the same verdicts.

KNOWN EDGE (measured, left): each asserted ``move`` in ``repos.jsonl`` is judged on the registry as it stood BEFORE it, by folding
lines ``1..n-1`` again (the writer's own view: a later ack must not change what it judged on), so a registry of M asserted moves
costs O(M^2) folds. Measured on a scratch store of 2,000 asserted path-only moves: ``verify`` takes about 28 s. Carrying one
running fold forward would read the whole file's acks and so judge on a different view than the writer, and would need a fold of
its own; neither is built. A registry that long is not a real store (a move is a rare, deliberate act).

``export`` writes a bundle (one JSON file): the records as written, the public keys the authority log holds, every cited band
answer, and prints ``items=<hash> repos=<hash>``, the two heads to keep.

Where the bundle goes. Levain keeps no operator record directory of its own (``record_dir`` is a repo's field, and a repo is
what a bundle must never land in), so the default is ``<levain home>/board-trail/`` and ``--out`` names another file or
directory. A destination inside a git work tree (judged on the path with every symlink resolved) is refused; so is overwriting
a file, and a failed write removes the file it created.

Known edges, stated rather than hidden:

* A signature is checked against the authority log as it is NOW. Whether it was valid AT THE RECORD'S TIME arrives with part B.
  A key revoked since is reported ("revoked since (seq N)", and the anchor line says so; the tail line and the relayed counts
  never call what it signed anchored), never silently valid and never a break by itself; a key that was never enrolled, quarantined or of the wrong class is a break.
* An answer's own signature (``sig`` and ``message``, as the band store holds them or the bundle carried them) is re-verified
  with the consent code the answer was made with (``levain.consent.terminal_sign``), under a key of the class the band's badge
  trusts (the laptop's terminal key, ``laptop-presence``). "(signed answer)" means the signed message names THIS question
  (``asked`` equals the row's question and the record's ``question_sha256``) and THIS answer (its free text equals the row's
  answer and the record's ``answer_sha256``). An option pick is signed as ``(letter) label`` while the row holds the option's
  key: the verdict says the signature covers the pick and not the words the record quotes. The entry's ``row_version`` is not
  checked (the band row as a relay reads it carries none).
* A ``move`` is decision-class by :func:`levain.desk.board_sign.move_is_decision` on the registry as it stood before it (the
  fold the writer judged it on); recorded asserted after that, it is a break like any other decision-class op.
* A cut's ``cause`` is judged AS OF THE CUT'S OWN POSITION, never against today's fold (see :func:`cause_problem_at`).
* Per-close author verdicts (P4's tiers), part B: see :func:`close_verdicts`. One line per closed item, the author derived by
  ``board_author.annotate`` (the function ``board state`` calls for ``closed_by``; this module holds no attribution rule), in
  P4's words. ``export`` writes each close's ``closed_by`` into the bundle; a bundle ``verify`` RE-DERIVES it when the repo is
  reachable (a close that derives differently is a break) and otherwise says "recorded in the bundle, not re-derived (repo not
  available)", never "verified". A derivation that fails is a note on that close (attribution never fails a read). The
  re-derivation is made with THIS install's authority log (``--data-root``): a key revoked since the export reads as a
  different tier, and that is a break the line explains (the bundle's close and the re-derived one are both printed).
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain.desk import board_chain as bc
from levain.desk import board_relay as rel
from levain.desk import board_sign as sg
from levain.desk import board_store as bs

__all__ = ["BUNDLE_FORMAT", "EXIT_BREAK", "EXIT_USAGE", "FileReport", "Trail", "adopt_trust_root", "close_verdicts", "register",
           "run_export", "run_verify", "verify_trail"]

BUNDLE_FORMAT = "levain-board-trail/1"
FILES = ("items.jsonl", "repos.jsonl")
SHORT = {"items.jsonl": "items", "repos.jsonl": "repos"}
EXIT_BREAK = 3
EXIT_USAGE = 2
READ_WAIT = 2.0
TRAIL_DIR = "board-trail"
SIGNED_WORDS = "operator, signed with a ceremony"
BUNDLE_KEY_WORDS = "signature valid under a key the bundle names; not checked against an authority you hold"
NO_AUTHORITY_FOR = "cannot be checked: no authority log to verify a signature against ({why})"
TRUST_LIVE = "live"
TRUST_BUNDLE = "bundle"
_HEAD_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")   # the separators are named by escape, never written literally (an editor eats them)
_SIGNED_ROW_GRADES = ("signed", "signed-phone", "signed-terminal")
_ANSWER_KEY_CLASS = "laptop-presence"          # levain.authority.LAPTOP: the badge's trust root is the laptop's terminal key


class TrailError(Exception):
    """The trail cannot be built or read at all (exit 2), as opposed to a break in what it holds (exit 3)."""


def safe(text: Any) -> str:
    """``text`` with every control character (C0, DEL, C1, the two Unicode line separators) escaped, so a field a record
    carries can never move the cursor or erase a line of the report."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 0x100 else f"\\u{ord(m.group()):04x}", str(text))


# ------------------------------------------------------------------------------------------------------------- the input


@dataclass
class Trail:
    """What a verification stands on: each file's lines AS WRITTEN (``bytes``, split on ``\\n`` only), whether a file ends in an
    unterminated line, the store id, the authority snapshot (None: none could be read, ``why_no_authority`` says why), the band
    the relayed records cite, and the trust root: ``trust`` is :data:`TRUST_LIVE` (the snapshot is an authority log this
    install replayed) or :data:`TRUST_BUNDLE` (the snapshot is the keys a bundle names, checked against nothing); ``trust_line``
    says which, and ``breaks`` holds what is broken in the trail as a whole (a bundle key the trust root contradicts)."""
    store_id: str
    lines: dict[str, list[bytes]]
    unterminated: dict[str, bool]
    snapshot: Any
    why_no_authority: str = ""
    band: Any = None                                    # a BandSource, built from the live band or from the bundle
    trust: str = TRUST_LIVE
    trust_line: str = ""
    breaks: list[str] = field(default_factory=list)
    data_root: str | None = None                        # the authority a close's signing key is judged against (P4's resolver)
    from_bundle: bool = False                           # read from a bundle (whatever its trust root became)
    closes: Any = None                                  # a bundle's recorded closes ({item: closed_by}); None: it records none
    closes_unavailable: str = ""                        # why an export could not derive them


def _split(raw: bytes) -> tuple[list[bytes], bool]:
    parts = raw.split(b"\n")
    tail = parts[-1] != b""
    if not tail:
        parts.pop()
    return parts, tail


def _authority_line(snapshot: Any, why: str, where: str) -> str:
    if snapshot is None:
        return f"authority: none could be read ({why}): no signature can be checked"
    return f"authority: {where}, replayed now (seq {snapshot.seq}, head {str(snapshot.head_hash)[:12]})"


def _read_authority(data_root: str | None) -> tuple[Any, str]:
    try:
        return sg.read_authority(sg.resolve_authority(data_root=data_root)), ""
    except bs.BoardError as exc:
        return None, str(exc)
    except Exception as exc:                            # noqa: BLE001 - an authority log that cannot be read is none to trust
        return None, f"the authority log cannot be read ({type(exc).__name__}: {exc})"


def read_live(target: bs.Target, *, data_root: str | None, band_dir: str | None) -> Trail:
    """The live store under one shared hold (the same bounded wait ``board state`` makes; taken for a pointered store and for any
    store directory a writer ever locked, i.e. one holding ``store.lock``), the authority log replayed now and the band opened
    lazily. A store with no ``repos.jsonl`` has an empty registry file."""
    deadline = time.monotonic() + READ_WAIT
    raws: dict[str, bytes] = {}
    try:
        with contextlib.ExitStack() as stack:
            if target.pointered or os.path.lexists(target.store_dir / bs.LOCK_NAME):
                stack.enter_context(bs.store_lock(target.store_dir, exclusive=False, deadline=deadline))
            for name in FILES:
                path = target.items_path if name == bs.ITEMS_NAME else target.store_dir / name
                raws[name] = bs._read_stable(path, fcntl.LOCK_SH, deadline) if os.path.lexists(path) else b""
            sid = bs.read_store_id(target.store_dir) if target.pointered or os.path.lexists(target.store_dir / bs.STORE_ID_NAME) else ""
    except (bs.BoardError, OSError) as exc:
        raise TrailError(f"the store cannot be read: {exc}") from None
    lines, unterminated = {}, {}
    for name in FILES:
        lines[name], unterminated[name] = _split(raws[name])
    snapshot, why = _read_authority(data_root)
    return Trail(sid, lines, unterminated, snapshot, why, _LiveBand(band_dir), TRUST_LIVE,
                 _authority_line(snapshot, why, "the log this install holds"), data_root=data_root)


class _LiveBand:
    """The band a live ``verify`` reads, opened on the first row so a missing band is a per-record refusal, not a crash."""

    def __init__(self, directory: str | None) -> None:
        self._dir, self._band = directory, None

    def row(self, np_id: str) -> rel.BandRow:
        if self._band is None:
            try:
                self._band = rel.band_source(self._dir)
            except Exception as exc:                    # noqa: BLE001 - an unopenable band is a refusal naming it
                raise rel.BandRefusal(f"the band could not be opened ({type(exc).__name__})") from None
        return self._band.row(np_id)

    def sign(self, np_id: str) -> Any:
        """The answer's own signing record as the band store holds it, or None."""
        self.row(np_id)                                 # opens the band (a refusal here is the row's)
        own = getattr(self._band, "sign", None)
        if own is not None:
            return own(np_id)
        store = getattr(self._band, "_store", None)
        if store is None:
            return None
        from levain import band_view
        return band_view.answer_sign_of(store, np_id)


class _BundleBand:
    """The cited answers a bundle carries, as a band source (their shape is checked by :func:`load_bundle`)."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self._answers = answers

    def row(self, np_id: str) -> rel.BandRow:
        got = self._answers.get(np_id)
        if got is None:
            raise rel.BandRefusal(f"{np_id} is not among the answers this bundle carries")
        if "unreadable" in got:
            raise rel.BandRefusal(f"{np_id} could not be read when the bundle was made: {got['unreadable']}")
        return rel.BandRow(got["question"], got["answer"], got["answer_grade"], got["producer"])

    def sign(self, np_id: str) -> Any:
        got = self._answers.get(np_id)
        return got.get("sign") if isinstance(got, Mapping) else None


class _ReadOnce:
    """One verification's view of the band: each row and each signing record is read ONCE and every check (the citation, the
    answer's signature, the manifest, a cut's cause) judges that same reading. A read that fails is a refusal, never a crash."""

    def __init__(self, band: Any) -> None:
        self._band = band
        self._rows: dict[str, tuple[Any, Exception | None]] = {}
        self._signs: dict[str, tuple[Any, Exception | None]] = {}

    def row(self, np_id: str) -> rel.BandRow:
        if np_id not in self._rows:
            try:
                self._rows[np_id] = (rel._row_of(self._band, np_id), None)
            except rel.BandRefusal as exc:
                self._rows[np_id] = (None, exc)
        row, exc = self._rows[np_id]
        if exc is not None:
            raise rel.BandRefusal(str(exc))
        return row

    def sign(self, np_id: str) -> Any:
        if np_id not in self._signs:
            reader = getattr(self._band, "sign", None)
            try:
                self._signs[np_id] = (reader(np_id) if reader is not None else None, None)
            except Exception as exc:                    # noqa: BLE001 - re-raised to the one caller that words it
                self._signs[np_id] = (None, exc)
        got, exc = self._signs[np_id]
        if exc is not None:
            raise exc
        return got


# ------------------------------------------------------------------------------------------------------------ the verdicts


@dataclass
class RecordLine:
    n: int
    text: str                    # "decide levain#1" (what the record is)
    verdict: str
    ok: bool = True


@dataclass
class FileReport:
    name: str
    lines: int
    head: str
    records: list[RecordLine] = field(default_factory=list)
    breaks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # acknowledged tears and breaks, revoked keys: listed, not failures
    anchor: str = ""
    tail: str = ""
    relayed: str = ""
    truncation: str = ""


def line_head(lines: Sequence[bytes]) -> str:
    """A file's head: the sha256 of its last line AS WRITTEN (what the next record's ``prev`` would be); of empty bytes for an
    empty file (what a new file's cutover commits)."""
    return bc.line_hash(lines[-1]) if lines else hashlib.sha256(b"").hexdigest()


def _author_of(rec: Mapping[str, Any]) -> tuple[str, str]:
    a = rec.get("author")
    if isinstance(a, Mapping):
        return str(a.get("kind", "?")), str(a.get("name", "?"))
    return "?", "?"


def _what(rec: Mapping[str, Any]) -> str:
    ident = rec.get("id") if rec.get("id") is not None else rec.get("repo")
    return " ".join(str(x) for x in (rec.get("op", "?"), ident) if x is not None)


def _acks(lines: Sequence[bytes], chain: bc.Chain) -> dict[int, tuple[int, bc.Ack]]:
    """line number -> (the acking record's line, the ack), by the writer's own rule (:func:`board_chain.acked_lines`), keeping
    who acknowledged: only a line that itself parses and chains can ack."""
    breaks = chain.by_line()
    out: dict[int, tuple[int, bc.Ack]] = {}
    for m, line in enumerate(lines, 1):
        if m in breaks:
            continue
        try:
            ack = bc.parse_ack(bc.record_of(line))
        except bc.AckError:
            continue
        if ack is not None and ack.line <= len(lines) and bc.honours(ack, bc.line_hash(lines[ack.line - 1]), breaks.get(ack.line)):
            out.setdefault(ack.line, (m, ack))
    return out


@dataclass(frozen=True)
class _FoldSaid:
    """What a file's own fold says of each line: ``rejected`` (line -> why: the fold will not read it as a record),
    ``dropped`` (line -> the line of the ack the fold dropped it by), ``unapplied`` (line -> why: a well-formed record the state
    does not allow), ``bogus_acks`` (an ack's line -> the line it names, which is not torn: the fold reports it)."""
    rejected: Mapping[int, str]
    dropped: Mapping[int, int]
    unapplied: Mapping[int, str]
    bogus_acks: Mapping[int, int] = field(default_factory=dict)


def _problem_of(problems: Sequence[str], n: int) -> str:
    prefix = f"line {n}: "
    return next((p[len(prefix):] for p in problems if p.startswith(prefix)), "rejected")


def _fold_said(name: str, trail: Trail) -> _FoldSaid:
    """Each record judged by its file's OWN fold (no second schema here): the item fold for ``items.jsonl``, the registry fold
    (with this store's id) for ``repos.jsonl``."""
    lines = trail.lines[name]
    if name == "items.jsonl":
        from levain.desk.board_state import fold
        texts: list[Any] = []
        for x in lines:
            try:
                texts.append(x.decode("utf-8"))
            except UnicodeDecodeError:
                texts.append(x)                         # not text: the fold says so
        got = fold(texts)
        applied_or_ack = None
    else:
        from levain.desk.repos_fold import fold_repos
        got = fold_repos(lines, store_id=trail.store_id or None, unterminated_tail=bool(trail.unterminated.get(name)))
        applied_or_ack = set(got.applied)
    dropped = {n: m for n, m in got.dropped}
    rejected = {n: _problem_of(got.problems, n) for n in got.malformed if n not in dropped}
    unapplied: dict[int, str] = {}
    named_clean: set[int] = set()
    for p in got.problems:
        m = re.match(r"line (\d+): (.*)", p)
        if not m:
            continue
        n, why = int(m.group(1)), m.group(2)
        if why.startswith("ack-torn names this line"):
            named_clean.add(n)                          # the fold's own problem: an ack of a line that is not torn
            continue
        if n in rejected or n in dropped or why.startswith("torn"):
            continue
        if applied_or_ack is not None and n in applied_or_ack:
            continue
        unapplied.setdefault(n, why)                    # what is left is a soft rejection: the record did not apply
    return _FoldSaid(rejected, dropped, unapplied, _acks_naming(lines, named_clean))


def _acks_naming(lines: Sequence[bytes], targets: set[int]) -> dict[int, int]:
    """ack line -> the line it names, for every ack (parsed and chained, as the folds collect them) naming one of ``targets``."""
    if not targets:
        return {}
    breaks = bc.check_chain(lines).by_line()
    out: dict[int, int] = {}
    for m, line in enumerate(lines, 1):
        if m in breaks:
            continue
        try:
            ack = bc.parse_ack(bc.record_of(line))
        except bc.AckError:
            continue
        if ack is not None and ack.line in targets:
            out[m] = ack.line
    return out


CLOSES_NONE = "closes: the bundle records none (a bundle made before per-close authors were exported)"


def _materialise(trail: Trail, root: Path) -> Path:
    """The trail's two files (and its id) as a store directory under ``root``, so ``board state``'s own read can fold and derive
    from exactly the lines this verification judged. Never the live store: nothing is written there."""
    for name in FILES:
        body = b"\n".join(trail.lines[name])
        (root / name).write_bytes(body if not trail.lines[name] or trail.unterminated[name] else body + b"\n")
    if trail.store_id:
        (root / bs.STORE_ID_NAME).write_text(trail.store_id + "\n", encoding="utf-8")
    return root


def derive_closes(trail: Trail) -> tuple[dict[str, dict[str, Any]], dict[str, str], set[str], str]:
    """``(closed_by by item, the done items with no git close, the repos that were read, why)`` for the store the trail holds,
    derived by ``board state``'s own function (:func:`levain.desk.board_cli.state_doc`, which calls ``board_author.annotate``),
    the way ``board state --json`` reads it: every repo the registry tracks, read from its registered path. ``why`` is non-empty
    when nothing could be derived (no repo reachable, a read refused); the closes are then empty. Nothing is written to the
    live store."""
    import tempfile
    from levain.desk import board_cli
    parser = argparse.ArgumentParser()
    board_cli.register(parser.add_subparsers())
    with tempfile.TemporaryDirectory(prefix="levain-trail-") as tmp:
        root = _materialise(trail, Path(tmp))
        argv = ["board", "state", "--json", "--items", str(root)]
        if trail.data_root:
            argv += ["--data-root", str(trail.data_root)]
        args = parser.parse_args(argv)
        said: list[str] = []
        try:
            # the copy is handed over as a target: ``--items`` would be compared with the pointer's store (a bundle is not it)
            rc, doc = board_cli.state_doc(args, say=said.append, strict_store=True,
                                          target=bs.Target(store_dir=root, items_path=root / bs.ITEMS_NAME, pointered=False))
        except Exception as exc:                        # noqa: BLE001 - attribution never fails a read
            return {}, {}, set(), f"the closes could not be derived ({type(exc).__name__}: {exc})"
    if doc is None:
        return {}, {}, set(), "; ".join(said) or f"board state refused (exit {rc})"
    closes = {s["id"]: s["closed_by"] for s in doc.get("items", []) if s.get("state") == "done" and isinstance(s.get("closed_by"), dict)}
    bands = {s["id"]: "no git close to read an author from" for s in doc.get("items", [])
             if s.get("state") == "done" and not isinstance(s.get("closed_by"), dict)}
    reader = doc.get("reader") or {}
    read = {str(r.get("repo")) for r in reader.get("repos", [])} if reader.get("repos") else (
        {str(reader["repo"])} if reader.get("repo") else set())
    return closes, bands, read, ""


def _same_close(recorded: Mapping[str, Any], derived: Mapping[str, Any]) -> bool:
    """Whether a recorded ``closed_by`` and a re-derived one are the same claim: every field alike but the free-text note."""
    return {k: v for k, v in recorded.items() if k != "note"} == {k: v for k, v in derived.items() if k != "note"}


def close_verdicts(trail: Trail, reports: Sequence[FileReport]) -> tuple[list[str], int]:
    """``(lines, breaks)``: one verdict per closed item, in P4's words (``board_author.describe``), the weakest claim the evidence
    supports. A live store's closes are derived now. A bundle that records closes has each RE-DERIVED when its repo is reachable:
    the same claim is "re-derived from the repo, matches the bundle's record", a different one is a break; a repo not reachable
    prints the bundle's close as recorded, "not re-derived (repo not available)", never as verified. A close the repo shows that
    the bundle does not record is said, not a break. A derivation that fails is one note line, never a crash and never a break."""
    from levain.desk import board_author as ba
    closes, bands, read, why = derive_closes(trail)
    lines: list[str] = []
    breaks = 0
    recorded = trail.closes if isinstance(trail.closes, dict) else None
    if not trail.from_bundle or recorded is None:
        if why:
            lines.append(f"closes: not derived ({why})")
        if trail.from_bundle:
            lines.append(f"closes: the export could not derive them ({trail.closes_unavailable})" if trail.closes_unavailable
                         else CLOSES_NONE)
        for item_id, by in sorted(closes.items()):
            lines.append(f"close {item_id}: {ba.describe(by)}")
        for item_id, note in sorted(bands.items()):
            lines.append(f"close {item_id}: {note}")
        return lines, breaks
    if why:
        lines.append(f"closes: not re-derived ({why})")
    if trail.closes_unavailable:
        lines.append(f"closes: the export could not derive them ({trail.closes_unavailable})")
    for item_id, rec in sorted(recorded.items()):
        repo = item_id.rpartition("#")[0]
        if item_id in closes:
            now = closes[item_id]
            if _same_close(rec, now):
                lines.append(f"close {item_id}: {ba.describe(now)} (re-derived from the repo; matches the bundle)")
            else:
                breaks += 1
                lines.append(f"BREAK close {item_id}: the bundle records [{ba.describe(rec)}] and the repo derives [{ba.describe(now)}]")
        elif repo in read:
            breaks += 1
            lines.append(f"BREAK close {item_id}: the bundle records [{ba.describe(rec)}] and the repo does not show the item closed")
        else:
            lines.append(f"close {item_id}: {ba.describe(rec)} (recorded in the bundle, not re-derived (repo not available))")
    for item_id, now in sorted(closes.items()):
        if item_id not in recorded:
            lines.append(f"close {item_id}: {ba.describe(now)} (derived from the repo; the bundle records no close for it)")
    return lines, breaks


def _signed_problem(rec: Mapping[str, Any], trail: Trail) -> tuple[sg.Verdict | None, str | None]:
    """``(verdict, problem)`` for a signed-grade record: the P2 verifier's verdict, or why none could be had. A store with no id
    cannot be compared with the store a signed record names: that is a break, never a skipped check."""
    from levain.authority_sig import VerifierUnavailable
    if trail.snapshot is None:
        return None, NO_AUTHORITY_FOR.format(why=trail.why_no_authority or "none was given")
    if not trail.store_id:
        return None, (f"the signed record names store {rec.get('store')!r}, and this store has no id: the store it was signed for "
                      f"cannot be compared")
    if rec.get("store") != trail.store_id:
        return None, f"the signed record names store {rec.get('store')!r}, and this store is {trail.store_id!r}"
    try:
        return sg.verify_record(rec, trail.snapshot), None
    except VerifierUnavailable as exc:
        return None, f"the signature cannot be checked here ({exc})"


def cause_problem_at(rec: Mapping[str, Any], n: int, trail: Trail) -> str | None:
    """Why the ``cause`` of the cut at items line ``n`` did not hold when it was written, or None when it did. The predicate is
    ``board_sign.cut_cause_problem``, the one the writer asks, handed the items file as it stood BEFORE the cut (lines
    ``1..n-1``) and the registry up to and including the cited abandon: an abandon the fold applied, of a repo aborting that
    migration, located through the item fold, signed or relayed. Never today's fold: after ``abandon-done`` every old cut would
    fail "currently aborting". A cause that names no line of the registry is judged against the whole registry (the predicate says
    so)."""
    from levain.authority_sig import VerifierUnavailable
    if not (rec.get("op") == "decide" and rec.get("decision") == "cut"):
        return "cause belongs to a decide of cut only"
    repos = trail.lines["repos.jsonl"]
    cause = rec.get("cause")
    at = next((i for i, ln in enumerate(repos) if hashlib.sha256(ln).hexdigest() == cause), None)
    upto = repos if at is None else repos[:at + 1]
    tail_open = bool(trail.unterminated.get("repos.jsonl")) and len(upto) == len(repos)
    store = bs.StoreLines(tuple(trail.lines["items.jsonl"][:n - 1]), tuple(upto), trail.store_id, tail_open, False)
    try:
        return sg.cut_cause_problem(cause, rec.get("id"), store, trail.snapshot, trail.band)
    except VerifierUnavailable as exc:
        return f"the cause cannot be checked here ({exc})"


@dataclass(frozen=True)
class AnswerSig:
    """The verdict on a cited answer's OWN signature. ``anchored``: a signature that verifies under a key of the trust root and
    binds this question and these words, under a key not revoked since; ``word``: what the record's verdict says of the answer;
    ``problem``: a break; ``held``: it verifies under a key of the trust root and binds these words, revoked or not;
    ``revoked_seq``: the seq its signer was revoked at, when it was."""
    anchored: bool = False
    word: str = "unsigned answer"
    problem: str = ""
    held: bool = False
    revoked_seq: int | None = None


def _material(sign: Any) -> AnswerSig | tuple[str, str, str]:
    """The signing record's material ``(sig, message, signer_fp)``, or the verdict when there is none to check: absent, or a
    record that states unsigned and carries nothing, is unsigned; a record that says ``signed`` without complete material, or
    carries signature material (sig, message or signer_fp) under another state, is a break (it claims what it cannot show)."""
    if sign is None:
        return AnswerSig()
    if not isinstance(sign, Mapping):
        return AnswerSig(problem="the answer's signature record is malformed (not a mapping)")
    state = sign.get("sign_state")
    sig, message, fp = sign.get("sig"), sign.get("message"), sign.get("signer_fp")
    if state == "signed":
        if not (isinstance(sig, str) and sig and isinstance(message, str) and message and isinstance(fp, str) and fp):
            return AnswerSig(problem="the answer's signature record says signed and does not carry a complete signature "
                                     "(sig, message and signer_fp)")
        return sig, message, fp
    if sig or message or fp:
        return AnswerSig(problem=f"the answer's signature record carries signature material under sign_state {state!r}")
    return AnswerSig()


def answer_signature(np_id: str, trail: Trail, row: rel.BandRow | None = None, src: Mapping[str, Any] | None = None) -> AnswerSig:
    """The band answer ``np_id``'s OWN signature, judged against ``row`` (the row as this verification read it) and ``src`` (the
    relayed record's citation). See the module docstring for what "(signed answer)" binds."""
    from levain.authority import quarantined
    from levain.authority_sig import VerifierUnavailable
    from levain.consent import terminal_sign as ts
    reader = getattr(trail.band, "sign", None)
    try:
        sign = reader(np_id) if reader is not None else None
    except Exception as exc:                            # noqa: BLE001 - a sidecar that cannot be read is no signature to check
        return AnswerSig(problem=f"the answer's signature record cannot be read ({type(exc).__name__})")
    got = _material(sign)
    if isinstance(got, AnswerSig):
        if not got.problem and row is not None and row.answer_grade in _SIGNED_ROW_GRADES:
            return AnswerSig(problem=f"the band row says the answer is {row.answer_grade}, and no signature is there to check")
        return got
    sig, message, fp = got
    if trail.snapshot is None:
        return AnswerSig(problem=NO_AUTHORITY_FOR.format(why=trail.why_no_authority or "none was given"))
    key = trail.snapshot.keys.get(fp)
    if key is None:
        return AnswerSig(problem=f"the answer's signer {fp} is not a key in the authority log")
    if key.compromised_since is not None or quarantined([key]):
        return AnswerSig(problem=f"the answer's signer {fp} is {'compromised' if key.compromised_since is not None else 'quarantined'}")
    if key.key_class != _ANSWER_KEY_CLASS:
        return AnswerSig(problem=f"the answer's signer {fp} is a {key.key_class} key; an answer is signed with the laptop's "
                                 f"terminal key ({_ANSWER_KEY_CLASS}), the only key the band's badge trusts")
    desk = message.startswith(ts.DESK_FORMAT + "\n")
    try:
        raw = message.encode("utf-8")
        parsed = (ts.parse_desk_verified if desk else ts.parse_verified)(raw)
    except (ts.MessageError, ValueError) as exc:
        return AnswerSig(problem=f"the answer's signed message cannot be read ({exc})")
    mine = [e for e in parsed.entries if e.item == np_id]
    if not mine:
        return AnswerSig(problem=f"the answer's signed message does not name {np_id}")
    if len(mine) > 1:
        return AnswerSig(problem=f"the answer's signed message names {np_id} more than once")
    try:
        res = ts.verify(raw, sig, f'phill namespaces="{ts.SIGNERS_NAMESPACES}" {key.public_key}\n', "phill",
                        namespace=ts.DESK_NAMESPACE if desk else ts.NAMESPACE, ctk=lambda _fp: (None, ""))
    except VerifierUnavailable as exc:
        return AnswerSig(problem=f"the answer's signature cannot be checked here ({exc})")
    if not res.ok or res.fingerprint != fp:
        return AnswerSig(problem=f"the answer's signature does not verify ({res.problem or 'it names another key'})")
    entry = mine[0]
    if row is not None:
        if entry.asked != row.question or (src is not None and rel.digest(entry.asked) != src.get("question_sha256")):
            return AnswerSig(problem=f"the answer's signature is over another question (it asked {entry.asked!r}): it does not "
                                     f"bind this question")
        if entry.text is not None and (entry.text != row.answer
                                       or (src is not None and rel.digest(entry.text) != src.get("answer_sha256"))):
            return AnswerSig(problem="the answer's signature is over other words than the row's answer: it does not bind this answer")
    # Every qualifier is said in every trust mode, and none returns early: what the signature covers (a pick, or the words), whose
    # key it is (the trust root's, or only one the bundle names) and whether that key is revoked since. Anchored only with none.
    revoked = f", by a key revoked since (seq {key.revoked_seq})" if key.revoked_seq is not None else ""
    bundle = trail.trust != TRUST_LIVE
    under = " under a key the bundle names" if bundle else ""
    unchecked = "; not checked against an authority you hold" if bundle else ""
    if entry.text is None:
        word = (f"answer signed as the pick {entry.answer!r}{under}{revoked}{unchecked}; the signature does not cover the option "
                f"key {row.answer if row is not None else '?'!r} the row holds, which the record's words quote")
    elif bundle:
        word = f"answer signature valid{under}{revoked}{unchecked}"
    else:
        word = f"signed answer{revoked}"
    held = not bundle and entry.text is not None
    return AnswerSig(held and key.revoked_seq is None, word, "", held, key.revoked_seq)


def _manifest_sha(rec: Mapping[str, Any], begins: Mapping[tuple[Any, Any], str]) -> str | None:
    if rec.get("op") == "begin-track":
        return rec.get("manifest_sha256")
    return begins.get((rec.get("repo"), rec.get("migration")))


def _relayed(rec: Mapping[str, Any], trail: Trail, begins: Mapping[tuple[Any, Any], str]) -> tuple[AnswerSig | None, str]:
    """``(sig, text)`` for a relayed record: the citation, the answer's own signature bound to this row, the manifest. ``sig`` is
    the answer's verdict when the citation holds, None when it does not (``text`` then says why)."""
    _, name = _author_of(rec)
    got = rel.verify_relayed(rec, trail.band)
    if not got.ok:
        return None, f"relayed citation does not hold: {got.text}"
    src = rec["source"]                                 # verify_relayed held its shape: a mapping of four strings
    try:
        row = rel._row_of(trail.band, src["np"])
    except rel.BandRefusal as exc:
        return None, f"relayed citation does not hold: {src['np']} unreadable: {exc}"
    sig = answer_signature(src["np"], trail, row, src)
    if sig.problem:
        return None, f"relayed citation does not hold: {sig.problem}"
    if rec.get("op") in ("begin-track", "track"):
        manifest = _manifest_sha(rec, begins)
        if manifest is None:
            return None, "relayed citation does not hold: no begin-track of this migration records a manifest digest to bind"
        said = rel.check_manifest(rec, row, manifest)
        if not said.ok:
            return None, f"relayed citation does not hold: {said.text}"
    return sig, f"agent {name}, relaying {src['np']} ({sig.word})"


@dataclass(frozen=True)
class _Anchor:
    """The ONE judgement of what the last signature-valid record commits; the anchor line and the tail line are both read from
    it, so neither can claim more than the other. ``trusted``: its key is the trust root's (not one only a bundle names), not
    revoked since, and the file has no break at or before it; else ``why_not`` says which of those fails."""
    n: int
    at: str
    trusted: bool
    why_not: str


def _anchor_of(n: int, rec: Mapping[str, Any], verdict: sg.Verdict | None, trail: Trail, broken: list[int]) -> _Anchor:
    if trail.trust != TRUST_LIVE:
        why = "its signature was checked only against a key the bundle names"
    elif broken:
        why = f"the file breaks at line {broken[0]}, at or before it"
    elif verdict is not None and verdict.state == "revoked":
        why = f"its signer's key is revoked since (seq {verdict.revoked_seq})"
    else:
        why = ""
    return _Anchor(n, str(rec.get("at", "?")), not why, why)


def _anchor_line(name: str, anchor: _Anchor, verdict: sg.Verdict | None, trail: Trail, broken: list[int], cuts: list[int],
                 acked_by: Any) -> str:
    n, at = anchor.n, anchor.at
    if trail.trust != TRUST_LIVE:
        return (f"{name}: NOT ANCHORED: the signatures were checked only against keys the bundle itself names "
                f"({trail.why_no_authority or 'no authority log of yours was read'}): nothing here is committed by a key you hold")
    if broken:
        return (f"{name}: NOT ANCHORED: the file breaks at line {broken[0]}, at or before the last signed record (line {n}); "
                f"nothing up to it can be called committed by the operator's key")
    # what is left: a live trust root and no break up to line n; the key is either good (anchored) or revoked since (NOT
    # ANCHORED by a key you can still trust, the span it commits said all the same)
    lead = f"{name}: anchored" if anchor.trusted else f"{name}: NOT ANCHORED by a key you can still trust: the signature at line {n}"
    by = ("the operator's key" if anchor.trusted else
          f"a key revoked since (seq {verdict.revoked_seq if verdict is not None else '?'}): a forgery signed with it after the "
          f"revocation would read the same")
    if cuts:
        t = max(cuts)
        before = (f"lines 1-{t - 1} are committed by NOTHING but the acknowledgement of line {t} by {acked_by(t)}" if t > 1
                  else "nothing precedes it")
        span = (f" from line {t} through line {n} only (signed at {at}): the signature commits" if anchor.trusted else
                f" (signed at {at}) commits, from line {t} through line {n} only,")
        return f"{lead}{span} the chain back to the acknowledged line {t}, by {by}; {before}"
    span = (f" through line {n} (signed at {at}): every record up to it is committed by" if anchor.trusted else
            f" (signed at {at}) commits every record up to it, by")
    return f"{lead}{span} {by}"


def _tail_line(name: str, anchor: _Anchor, after: int) -> str:
    if after:
        return (f"{name}: {after} records after the last signed record (at {anchor.at}) are unanchored: anyone who can write this "
                f"file could have rewritten them")
    if anchor.trusted:
        return f"{name}: 0 records after the last signed record (at {anchor.at}): the tail is anchored"
    return (f"{name}: 0 records after the last signature-valid record (line {anchor.n}, at {anchor.at}); no anchor you can trust "
            f"covers it: {anchor.why_not}")


def _move_decision(rec: Mapping[str, Any], n: int, trail: Trail) -> str | None:
    """Why the ``move`` at registry line ``n`` is decision-class, judged by the writer's own rule
    (:func:`board_sign.move_is_decision`) on the registry as it stood BEFORE the move (lines ``1..n-1``, the fold the writer
    judged it on), or None when it is not (or its shape is the fold's to reject)."""
    from levain.desk.repos_fold import fold_repos
    repo, origin, root = rec.get("repo"), rec.get("origin"), rec.get("root")
    if not isinstance(repo, str) or not isinstance(origin, str) or not isinstance(root, list) \
            or not all(isinstance(r, str) for r in root):
        return None
    cur = fold_repos(trail.lines["repos.jsonl"][:n - 1], store_id=trail.store_id or None).repos.get(repo)
    if cur is None or not sg.move_is_decision(cur.mode, cur.origin, cur.root, origin, root):
        return None
    return f"the repo is {cur.mode} and the move changes its recorded origin or root set"


def verify_file(name: str, trail: Trail, heads: Mapping[str, str]) -> FileReport:
    lines = trail.lines[name]
    rep = FileReport(name, len(lines), line_head(lines))
    chain = bc.check_chain(lines)
    breaks = chain.by_line()
    acks = _acks(lines, chain)
    folded = _fold_said(name, trail)
    begins: dict[tuple[Any, Any], str] = {}
    last_signed: tuple[int, Mapping[str, Any], sg.Verdict | None] | None = None
    relays = {"anchored": 0, "revoked": 0, "unanchored": 0}
    claimed_signed = False
    signed_broken: int | None = None                    # the last record whose signature verified but which carries a break
    unterminated_at = len(lines) if trail.unterminated.get(name) else 0
    broken_lines: list[int] = []                        # lines that carry a break (the anchor reads them)
    cuts: list[int] = []                                # acknowledged tears and chain breaks: where a signed record's reach stops

    def acked_by(n: int) -> str | None:
        """Who acknowledged line ``n``: the ack the chain honours, else the ack the fold dropped it by; None when no ack does."""
        m = acks[n][0] if n in acks else folded.dropped.get(n)
        if m is None:
            return None
        who = bc.record_of(lines[m - 1]) or {}
        _, aname = _author_of(who)
        return f"{aname} ({who.get('grade', '?')}, line {m})"

    def brk(n: int, text: str) -> None:
        rep.breaks.append(f"line {n}: {text}")
        broken_lines.append(n)

    for n, line in enumerate(lines, 1):
        rec = bc.record_of(line)
        if rec is None:
            if n == unterminated_at:
                brk(n, "no terminating newline (a write in flight, or a torn tail)")
            ackd = acked_by(n)
            if ackd is not None:
                rep.notes.append(f"line {n} torn, acknowledged by {ackd}")
                cuts.append(n)
            else:
                brk(n, f"torn (not a JSON object); `levain board ack-torn --file {name} --line {n}` after repairing or accepting it")
            continue
        what = _what(rec)
        if n in folded.dropped:                         # a record the fold rejects, acknowledged as a torn line: dropped
            ackd = acked_by(n)                          # the fold names the ack it dropped the line by: never None here
            rep.records.append(RecordLine(n, what, f"not read: torn for itself and acknowledged by {ackd}"))
            rep.notes.append(f"line {n} torn for itself, acknowledged by {ackd}")
            cuts.append(n)
            continue
        problems: list[str] = []
        if n == unterminated_at:
            problems.append("no terminating newline (a write in flight, or a torn tail)")
        if n in breaks:
            if n in acks and acks[n][1].chain:
                rep.notes.append(f"line {n} chain break, acknowledged by {acked_by(n)}: {breaks[n].problem}")
                cuts.append(n)
            else:
                problems.append(breaks[n].problem)
        if n in folded.rejected and n != unterminated_at:
            problems.append(f"the fold rejects this record ({folded.rejected[n]})")
        if n in folded.bogus_acks:
            t = folded.bogus_acks[n]
            problems.append(f"it acknowledges line {t} as torn, and line {t} is not torn (the file's own fold reports this ack)")
        grade = rec.get("grade")
        pre_cutover = chain.cutover is None or n < chain.cutover
        if n in folded.unapplied:
            if pre_cutover:
                rep.notes.append(f"line {n}: the fold does not apply it ({folded.unapplied[n]})")
            else:
                problems.append(f"the fold does not apply it ({folded.unapplied[n]}): the writer refuses such a record")
        cause_holds = False
        if "cause" in rec and name == "items.jsonl":
            why_cause = cause_problem_at(rec, n, trail)
            if why_cause is not None:
                problems.append(f"the cause does not hold as of this cut: {why_cause}")
            else:
                cause_holds = True
        kind, who = _author_of(rec)
        verdict_text: str | None
        signed_here: sg.Verdict | None = None           # applied below only when the record has no problem at all
        relay_sig: AnswerSig | None = None
        if pre_cutover and "grade" not in rec:
            verdict_text = "legacy"
        elif grade == sg.GRADE_LEGACY:
            verdict_text = "legacy"
            if not pre_cutover:
                problems.append("graded legacy after the cutover: no writer writes a legacy record once the file is chained")
        elif grade in sg.SIGNED_GRADES or "sig" in rec:
            claimed_signed = True
            verdict, problem = _signed_problem(rec, trail)
            if problem is not None or verdict is None or verdict.state == "bad":
                verdict_text = None
                problems.insert(0, problem if problem is not None else (verdict.text if verdict else "signature does not verify"))
            else:
                base = SIGNED_WORDS if trail.trust == TRUST_LIVE else BUNDLE_KEY_WORDS
                if verdict.state == "revoked":
                    verdict_text = f"{base}, by a key revoked since (seq {verdict.revoked_seq})"
                    rep.notes.append(f"line {n}: signed by {verdict.fingerprint}, revoked at seq {verdict.revoked_seq}; the record's "
                                     f"own time is the signer's claim")
                else:
                    verdict_text = base
                signed_here = verdict
        elif grade == sg.GRADE_RELAYED:
            relay_sig, text = _relayed(rec, trail, begins)
            if relay_sig is not None:
                verdict_text = text
                if relay_sig.revoked_seq is not None:
                    rep.notes.append(f"line {n}: the answer {rec['source']['np']} is signed by a key revoked at seq "
                                     f"{relay_sig.revoked_seq}: a forgery signed with it after the revocation would read the same")
            else:
                verdict_text = None
                problems.insert(0, text)
        elif grade == bs.GRADE_ASSERTED:
            if kind == "?":
                verdict_text = None
                problems.insert(0, "asserted, but the record names no author")
            else:
                verdict_text = f"asserted by {who}, unsigned"
                if pre_cutover:                         # a grandfathered record is judged by the pre-cutover rules
                    pass
                elif sg.POLICY.get(rec.get("op")) == sg.DECISION_CLASS and not cause_holds:
                    problems.append(f"a decision-class {rec.get('op')} recorded asserted: the writer refuses this (it needs a "
                                    f"signature or a relayed answer)")
                elif rec.get("op") == "move" and name == "repos.jsonl":
                    why_move = _move_decision(rec, n, trail)
                    if why_move is not None:
                        problems.append(f"a decision-class move ({why_move}) recorded asserted: the writer refuses this (it "
                                        f"needs a signature or a relayed answer)")
        else:
            verdict_text = None
            problems.insert(0, f"{grade!r} is not a grade" if "grade" in rec else "a record after the cutover carries no grade")
        if problems:                                    # a broken record anchors nothing, records no manifest and is no relay
            if signed_here is not None:
                signed_broken = n
            text = "; ".join(problems) if verdict_text is None else f"{verdict_text}, but: {'; '.join(problems)}"
            rep.records.append(RecordLine(n, what, text, False))
            brk(n, f"{what}: {'; '.join(problems)}")
            continue
        rep.records.append(RecordLine(n, what, verdict_text or "?"))
        if signed_here is not None:
            last_signed = (n, rec, signed_here)
        if relay_sig is not None:
            relays["anchored" if relay_sig.anchored else "revoked" if relay_sig.held else "unanchored"] += 1
        if (signed_here is not None or relay_sig is not None) and rec.get("op") == "begin-track" \
                and isinstance(rec.get("manifest_sha256"), str):
            begins[(rec.get("repo"), rec.get("migration"))] = rec["manifest_sha256"]
    # the anchor, the tail: both read from ONE judgement of the last signature-valid record
    records_after = lambda n0: sum(1 for k in range(n0, len(lines)) if bc.record_of(lines[k]) is not None)   # noqa: E731
    if last_signed is None:
        rep.anchor = (f"{name}: NOT ANCHORED: the signed record at line {signed_broken} carries a break, and no signed record "
                      f"without one precedes it: nothing in this file is anchored by the operator's key" if signed_broken else
                      f"{name}: no signed record verifies: nothing in this file is anchored by the operator's key" if claimed_signed
                      else f"{name}: no signed record: nothing in this file is anchored by the operator's key")
        rep.tail = (f"{name}: {records_after(0)} records, none after a signed record that stands: all are unanchored: "
                    "anyone who can write this file could have rewritten them")
    else:
        n, rec, verdict = last_signed
        broken_upto = sorted(m for m in broken_lines if m <= n)
        anchor = _anchor_of(n, rec, verdict, trail, broken_upto)
        rep.anchor = _anchor_line(name, anchor, verdict, trail, broken_upto, [t for t in cuts if t <= n], acked_by)
        rep.tail = _tail_line(name, anchor, records_after(n))
    said = []
    if relays["anchored"]:
        said.append(f"{name}: {relays['anchored']} relayed record(s) rest on a signed answer: his words are anchored by the "
                    f"answer's own signature, which does not cover the chain")
    if relays["revoked"]:
        said.append(f"{name}: {relays['revoked']} relayed record(s) rest on an answer signed by a key revoked since: a forgery "
                    f"signed with it after the revocation would read the same, so nothing you can trust anchors his words")
    if relays["unanchored"]:
        said.append(f"{name}: {relays['unanchored']} relayed record(s) rest on an answer no signature of a key you hold binds: "
                    f"nothing anchors his words")
    rep.relayed = "\n".join(said)
    if name in heads:
        h = heads[name]
        hashes = [bc.line_hash(x) for x in lines]
        empty = hashlib.sha256(b"").hexdigest()
        if h in hashes:
            j = len(hashes) - 1 - hashes[::-1].index(h)
            later = len(lines) - 1 - j
            rep.truncation = (f"{name}: kept head {h[:12]} is the current head: nothing was removed" if later == 0 else
                              f"{name}: kept head {h[:12]} is line {j + 1}; {later} lines were written after it (the file grew)")
        elif h == empty:
            rep.truncation = (f"{name}: kept head {h[:12]} is the empty file's, and the file is empty" if not lines else
                              f"{name}: kept head {h[:12]} is the empty file's; {len(lines)} lines were written after it (the file "
                              f"grew: an empty file had nothing to remove)")
        else:
            rep.truncation = (f"{name}: TRUNCATION OR REWRITE: kept head {h[:12]} is not on this chain (the file now ends at "
                              f"{rep.head[:12]}): records were removed or rewritten")
            rep.breaks.append(rep.truncation)
    else:
        rep.truncation = (f"{name}: no kept head given: a truncation of this file is not checked "
                          f"(`--head {SHORT[name]}=<hash>` from an earlier export)")
    return rep


def verify_trail(trail: Trail, heads: Mapping[str, str] | None = None) -> list[FileReport]:
    once = dataclasses.replace(trail, band=_ReadOnce(trail.band))
    return [verify_file(name, once, heads or {}) for name in FILES]


# ----------------------------------------------------------------------------------------------------------------- bundle


def _line_json(line: bytes) -> Any:
    try:
        text = line.decode("utf-8")
        if text.encode("utf-8") == line:
            return text
    except UnicodeDecodeError:
        pass
    return {"b64": base64.b64encode(line).decode("ascii")}


def _line_back(x: Any) -> bytes:
    if isinstance(x, str):
        return x.encode("utf-8")
    if isinstance(x, dict) and set(x) == {"b64"} and isinstance(x["b64"], str):
        try:
            return base64.b64decode(x["b64"], validate=True)
        except ValueError:
            pass
    raise TrailError("the bundle holds a line that is neither text nor {b64}")


def collect_answers(trail: Trail) -> dict[str, Any]:
    """Every band answer a relayed record cites, as the band holds it now (or why it, or its signing record, could not be read:
    the bundle then carries the reason, and verifying it is a break for that record)."""
    out: dict[str, Any] = {}
    for name in FILES:
        for line in trail.lines[name]:
            rec = bc.record_of(line)
            src = rec.get("source") if rec else None
            if not isinstance(src, Mapping) or not isinstance(src.get("np"), str) or src["np"] in out:
                continue
            try:
                row = rel._row_of(trail.band, src["np"])
                got: dict[str, Any] = {"producer": row.producer, "question": row.question, "answer": row.answer,
                                       "answer_grade": row.answer_grade}
                try:
                    sign = trail.band.sign(src["np"]) if hasattr(trail.band, "sign") else None
                except Exception as exc:                # noqa: BLE001 - a sidecar that cannot be read is carried as unreadable
                    raise rel.BandRefusal(f"its signing record could not be read ({type(exc).__name__})") from None
                if isinstance(sign, Mapping):
                    got["sign"] = {k: sign.get(k) for k in ("sig", "message", "signer_fp", "sign_state", "sign_reason")}
                out[src["np"]] = got
            except rel.BandRefusal as exc:
                out[src["np"]] = {"unreadable": str(exc)}
    return out


def _key_json(key: Any) -> dict:
    return {f.name: getattr(key, f.name) for f in dataclasses.fields(key)}


def _closes_json(trail: Trail) -> dict:
    """The bundle's ``closes``: each closed item's derived ``closed_by`` as ``board state --json`` emits it (the author of the
    close, by P4's tiers). Derived with this install's authority log; a derivation that fails is carried as ``closes_unavailable``."""
    closes, _bands, _read, why = derive_closes(trail)
    return {"closes": closes} if not why else {"closes": {}, "closes_unavailable": why}


def bundle_of(trail: Trail) -> dict:
    keys = sorted((_key_json(k) for k in trail.snapshot.keys.values()), key=lambda k: k["fingerprint"]) if trail.snapshot else []
    return {
        "format": BUNDLE_FORMAT, "store": trail.store_id,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {name: {"lines": [_line_json(x) for x in trail.lines[name]], "unterminated": trail.unterminated[name]} for name in FILES},
        "heads": {SHORT[name]: line_head(trail.lines[name]) for name in FILES},
        "authority": ({"store_id": trail.snapshot.store_id, "seq": trail.snapshot.seq, "head_hash": trail.snapshot.head_hash,
                       "keys": keys} if trail.snapshot else {"unavailable": trail.why_no_authority}),
        "answers": collect_answers(trail),
        **_closes_json(trail),
    }


_ANSWER_FIELDS = ("question", "answer", "answer_grade", "producer")


def _check_answer(np_id: Any, got: Any) -> None:
    if not isinstance(np_id, str) or not isinstance(got, dict):
        raise TypeError(f"answer {np_id!r} is not an object")
    if "unreadable" in got:
        if not isinstance(got["unreadable"], str):
            raise TypeError(f"answer {np_id}: unreadable is not text")
        return
    if not all(isinstance(got.get(k), str) for k in _ANSWER_FIELDS):
        raise TypeError(f"answer {np_id}: {', '.join(_ANSWER_FIELDS)} must each be text")
    if got.get("sign") is not None and not isinstance(got["sign"], dict):
        raise TypeError(f"answer {np_id}: sign is not an object")


def load_bundle(path: Path) -> Trail:
    """A bundle as a :class:`Trail` whose snapshot is the keys the bundle NAMES (``trust`` :data:`TRUST_BUNDLE`): claims, until
    :func:`adopt_trust_root` compares them with an authority log the verifier holds."""
    from levain.authority import AuthorityKey, AuthoritySnapshot, StandingPolicy
    try:
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError) as exc:
        raise TrailError(f"{path} cannot be read as a trail bundle ({type(exc).__name__})") from None
    if not isinstance(doc, dict) or doc.get("format") != BUNDLE_FORMAT:
        raise TrailError(f"{path} is not a {BUNDLE_FORMAT} bundle")
    try:
        files = doc["files"]
        for name in FILES:
            if not isinstance(files[name], dict) or not isinstance(files[name]["lines"], list):
                raise TypeError(f"{name} lines is not a list")
        lines = {name: [_line_back(x) for x in files[name]["lines"]] for name in FILES}
        unterminated = {name: bool(files[name].get("unterminated")) for name in FILES}
        store = doc["store"]
        answers = doc["answers"]
        auth = doc["authority"]
        if not isinstance(store, str) or not isinstance(answers, dict) or not isinstance(auth, dict):
            raise KeyError("shape")
        for np_id, got in answers.items():
            _check_answer(np_id, got)
        closes = doc.get("closes")
        if closes is not None:
            if not isinstance(closes, dict) or not all(isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("tier"), str)
                                                       and isinstance(v.get("how"), str) for k, v in closes.items()):
                raise TypeError("closes must map an item id to a closed_by object (tier and how, each text)")
        unavailable = doc.get("closes_unavailable", "")
        if not isinstance(unavailable, str):
            raise TypeError("closes_unavailable is not text")
        snapshot, why = None, ""
        if "unavailable" in auth:
            why = f"the bundle carries no keys: {auth['unavailable']}"
        else:
            known = {f.name for f in dataclasses.fields(AuthorityKey)}
            keys = {}
            for k in auth["keys"]:
                key = AuthorityKey(**{a: b for a, b in k.items() if a in known})
                if not all(isinstance(v, str) and v for v in (key.fingerprint, key.public_key, key.key_class)):
                    raise TypeError("a key's fingerprint, public_key and key_class must each be non-empty text")
                if any(v is not None and (isinstance(v, bool) or not isinstance(v, int))
                       for v in (key.revoked_seq, key.compromised_since)):
                    raise TypeError("a key's revoked_seq and compromised_since must each be an integer or null")
                keys[key.fingerprint] = key
            snapshot = AuthoritySnapshot(store_id=auth["store_id"], seq=auth["seq"], head_hash=auth["head_hash"], keys=keys,
                                         policy=StandingPolicy())
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise TrailError(f"{path} is a damaged bundle ({type(exc).__name__}: {exc})") from None
    return Trail(store, lines, unterminated, snapshot, why, _BundleBand(answers), TRUST_BUNDLE, "", closes=closes,
                 closes_unavailable=unavailable, from_bundle=True)


def adopt_trust_root(trail: Trail, data_root: str | None) -> None:
    """Make the verifier's OWN authority log the trust root of a bundle ``trail``: replay it (``data_root``, default the
    install's), compare every key the bundle names against it (a key it does not hold, or holds as another key or class, is a
    break), and verify against it. With no log to replay, the bundle's keys stay claims: ``trust`` stays :data:`TRUST_BUNDLE`
    and every verdict that rests on them says so."""
    claimed = trail.snapshot
    trail.data_root = data_root
    live, why = _read_authority(data_root)
    if live is None:
        trail.why_no_authority = (f"no authority log of yours could be read ({why})" if claimed is not None else
                                  f"{trail.why_no_authority}; no authority log of yours could be read ({why})")
        trail.trust_line = (f"authority: the keys the bundle names (seq {claimed.seq}, head {str(claimed.head_hash)[:12]}), NOT "
                            f"checked against an authority you hold: {why}" if claimed is not None else
                            _authority_line(None, trail.why_no_authority, ""))
        return
    for fp, k in sorted((claimed.keys if claimed is not None else {}).items()):
        mine = live.keys.get(fp)
        if mine is None:
            trail.breaks.append(f"the bundle names key {fp} ({k.key_class}), which the authority log you hold does not")
        elif mine.public_key != k.public_key or mine.key_class != k.key_class:
            trail.breaks.append(f"the bundle names key {fp} as a {k.key_class} key {k.public_key[:24]}..., and the authority log "
                                f"you hold has it as a {mine.key_class} key {mine.public_key[:24]}...")
    trail.snapshot, trail.why_no_authority, trail.trust = live, "", TRUST_LIVE
    trail.trust_line = (_authority_line(live, "", "the log this install holds")
                        + (f"; the bundle's {len(claimed.keys)} key(s) were compared to it" if claimed is not None else
                           "; the bundle carried no keys"))


def _inside_git(path: Path) -> Path | None:
    for p in (path, *path.parents):
        if os.path.lexists(p / ".git"):
            return p
    return None


def default_dir() -> Path:
    from levain.desk import work_attach
    return work_attach.live_consent_dir().parent / TRAIL_DIR


def destination(out: str | None, store_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"trail-{re.sub(r'[^0-9A-Za-z_-]', '_', (store_id or 'store')[:8])}-{stamp}.json"
    if out is None:
        try:
            base = default_dir()
        except Exception as exc:                        # noqa: BLE001 - no levain home to name: --out is required
            raise TrailError(f"no default directory for the bundle ({exc}): name one with --out") from None
        dest = base / fname
    else:
        given = Path(out).expanduser()
        dest = given / fname if (given.is_dir() or str(out).endswith(os.sep)) else given
    dest = Path(os.path.abspath(dest))
    repo = _inside_git(dest.parent) or _inside_git(Path(os.path.realpath(dest.parent)))
    if repo is not None:
        raise TrailError(f"{dest} is inside a git work tree ({repo}): the bundle carries his words and never goes into a repo; "
                         f"name a place outside one with --out")
    return dest


def _write_new(dest: Path, data: bytes) -> None:
    """Write ``data`` to a NEW file at ``dest`` (0600, O_EXCL). Any failure is a :class:`TrailError`; a failure after the file
    was created removes it (this call made it, and it was never published)."""
    try:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise TrailError(f"{dest.parent} cannot be made ({exc})") from None
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TrailError(f"{dest} exists: a bundle is never overwritten") from None
    except OSError as exc:
        raise TrailError(f"{dest} cannot be written ({exc})") from None
    try:
        with os.fdopen(fd, "wb") as f:
            view = memoryview(data)
            while view:
                wrote = f.write(view)
                if not wrote:
                    raise OSError("a write made no progress")
                view = view[wrote:]
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise TrailError(f"{dest} cannot be written ({exc}); the partial file was removed") from None


# --------------------------------------------------------------------------------------------------------------------- CLI


def _print_report(reports: Sequence[FileReport], trail: Trail) -> int:
    broken = 0
    if trail.trust_line:
        print(safe(trail.trust_line))
    for b in trail.breaks:
        print(f"BREAK {safe(b)}")
    broken += len(trail.breaks)
    for rep in reports:
        print(f"{rep.name} ({rep.lines} lines, head {rep.head})")
        for r in rep.records:
            print(safe(f"  {rep.name}:{r.n} {r.text}: {r.verdict}" + ("" if r.ok else "  [BREAK]")))
        for note in rep.notes:
            print(safe(f"  note: {note}"))
        print(safe(rep.anchor))
        print(safe(rep.tail))
        for line in rep.relayed.splitlines():
            print(safe(line))
        if rep.truncation:
            print(safe(rep.truncation))
        for b in rep.breaks:
            print(safe(f"BREAK {rep.name}: {b}"))
        broken += len(rep.breaks)
    close_lines, close_breaks = close_verdicts(trail, reports)
    for line in close_lines:
        print(safe(line))
    return broken + close_breaks


def _parse_heads(raw: Sequence[str]) -> dict[str, str]:
    long = {v: k for k, v in SHORT.items()}
    out: dict[str, str] = {}
    for item in raw:
        key, _, val = item.partition("=")
        if key not in long or not _HEAD_RE.fullmatch(val):
            raise TrailError(f"--head {item!r}: use items=<64 hex> or repos=<64 hex>")
        if long[key] in out:
            raise TrailError(f"--head {key}= is given twice: give each file's kept head once")
        out[long[key]] = val
    return out


def _live(args: argparse.Namespace) -> Trail:
    try:
        target = bs.resolve_store(args.items)
    except bs.BoardError as exc:
        raise TrailError(str(exc)) from None
    return read_live(target, data_root=args.data_root, band_dir=args.band_dir)


def run_export(args: argparse.Namespace) -> int:
    try:
        trail = _live(args)
        bundle = bundle_of(trail)
        data = (json.dumps(bundle, indent=1, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
        dest = destination(args.out, trail.store_id)
        _write_new(dest, data)
    except TrailError as exc:
        print(safe(f"levain board trail export: refused: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    print(safe(f"bundle: {dest}"))
    print(f"items={bundle['heads']['items']} repos={bundle['heads']['repos']}")
    broke = sum(len(r.breaks) for r in verify_trail(trail)) + len(trail.breaks)
    if broke:
        print(f"levain board trail export: note: this store does not verify ({broke} breaks); the bundle holds it as it is: "
              f"run `levain board trail verify`", file=sys.stderr)
    if trail.snapshot is None:
        print(safe(f"levain board trail export: note: no authority log was read ({trail.why_no_authority}); the bundle carries no "
                   f"keys, so its signatures can be checked only against an authority log the verifier holds"), file=sys.stderr)
    return 0


def run_verify(args: argparse.Namespace) -> int:
    try:
        heads = _parse_heads(args.head)
        if args.bundle:
            trail = load_bundle(Path(args.bundle))
            adopt_trust_root(trail, args.data_root)
        else:
            trail = _live(args)
    except TrailError as exc:
        print(safe(f"levain board trail verify: refused: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    broken = _print_report(verify_trail(trail, heads), trail)
    if broken:
        print(f"levain board trail verify: {broken} breaks", file=sys.stderr)
        return EXIT_BREAK
    print("levain board trail verify: no breaks")
    return 0


def register(sub: Any) -> None:
    """Add ``trail`` under ``levain board`` (called from :func:`levain.desk.board_cli.register`)."""
    trail = sub.add_parser("trail", description="The board's records as a trail someone who does not trust this machine can check.")
    tsub = trail.add_subparsers(dest="trail_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--items", type=str, metavar="PATH",
                       help=f"The store directory or its items.jsonl (default: the pointer, else ${bs.ITEMS_ENV}).")
        p.add_argument("--data-root", dest="data_root", default=None, metavar="DIR",
                       help=("The cockpit data root whose authority log holds the operator's keys (default: the one the consent "
                             "hooks name). With a BUNDLE it is the trust root the bundle's keys are checked against."))
        p.add_argument("--band-dir", dest="band_dir", default=None, metavar="DIR",
                       help="The band store the relayed records cite (default: this install's consent directory).")

    e = tsub.add_parser(
        "export", help="Write the records, signatures, public keys and cited answers as one bundle; print the two heads.",
        description=("Writes one JSON bundle: both files' records as written, the public keys the authority log holds, and every "
                     "cited band answer. Prints `items=<hash> repos=<hash>`: keep them, so a later `verify --head` can tell a "
                     "truncated tail. The bundle goes to <levain home>/board-trail/ unless --out names a file or directory; it "
                     "carries his words, so a place inside a git work tree is refused."))
    common(e)
    e.add_argument("--out", metavar="PATH", default=None, help="A file, or a directory the bundle is named inside.")
    e.set_defaults(func=run_export)
    v = tsub.add_parser(
        "verify", help="Re-check the chain, every signature and every relayed citation; one verdict per record.",
        description=("Reads a bundle (BUNDLE) or the live store. Prints one verdict per record, then per file the anchor line, the "
                     "unanchored-tail line and whether the kept head is still on the chain. A bundle's keys are claims: they are "
                     "checked against the authority log of THIS install (--data-root); with none, no anchor is claimed. Exits 0 "
                     "with no break, 3 on any break (a chain break, a record its fold rejects, a signature that does not verify, a "
                     "relayed citation that does not hold, a head that is gone), 2 when it cannot run."))
    v.add_argument("bundle", nargs="?", default=None, metavar="BUNDLE")
    common(v)
    v.add_argument("--head", action="append", default=[], metavar="FILE=HASH",
                   help="A head `export` printed (items=<hash> or repos=<hash>), each file at most once: checked to still be on "
                        "that file's chain.")
    v.set_defaults(func=run_verify)
