"""levain.desk.board_trail: ``levain board trail export|verify`` (levain#104, design 7.4, P6 part A): the records of both board
files, checked by someone who does not trust the machine that wrote them.

``verify`` re-checks, per file (``items.jsonl`` and ``repos.jsonl`` are two chains, two anchors, two tails, two heads):

* the chain (:func:`levain.desk.board_chain.check_chain`: the cutover over the legacy prefix, an unchained record after it as a
  NAMED problem, a torn line, ``ack-torn`` re-anchoring), reading an acknowledged tear or break as acknowledged and every other
  one as a break;
* every signature, through ``board_sign.verify_record`` (the P2 verifier: ``ssh-keygen -Y verify`` in namespace ``levain-board``
  over the canonical bytes, against the authority log's keys), and that a signed record names THIS store;
* every ``relayed`` citation, through ``board_relay.verify_relayed`` (both digests, the words verbatim in the answer, the row's
  own grade, never promoted) and, for ``begin-track`` and ``track``, ``board_relay.check_manifest`` against the manifest digest
  the ceremony's ``begin-track`` recorded.

It prints one verdict per record in the design's honest vocabulary (``operator, signed with a ceremony`` / ``agent NAME,
relaying np-x (signed answer|unsigned answer)`` / ``asserted by NAME, unsigned`` / ``legacy``), then per file the anchor line
(every record up to the last signed one is committed by the operator's key), the unanchored-tail line (never hidden), and, when
``--head items=H --head repos=H`` is given, whether each kept head is still on its file's chain (a head that is gone is a
truncation or a rewrite). It exits 3 on any break, 2 when it cannot run.

``export`` writes a bundle (one JSON file): the records as written, the public keys the authority log holds, every cited band
answer, and prints ``items=<hash> repos=<hash>``, the two heads to keep. ``verify`` takes that bundle (``verify BUNDLE``) or reads
the live store (the store ``board state`` resolves; the authority log and the band as ``board add --sign`` / ``--source`` read them).

Where the bundle goes. Levain keeps no operator record directory of its own (``record_dir`` is a repo's field, and a repo is
what a bundle must never land in), so the default is ``<levain home>/board-trail/`` and ``--out`` names another file or
directory. A destination inside a git work tree is refused; so is overwriting a file. The bundle carries his words.

Known edges, stated rather than hidden:

* A signature is checked against the authority log as it is NOW: the log carries no time on a key, so "valid at the record's
  time" is not decidable. A key revoked since is reported ("revoked since (seq N)"), never silently valid and never a break by
  itself; a key that was never enrolled, quarantined or of the wrong class is a break.
* A relayed record's band answer is re-read as the band holds it (or as the bundle carried it); the row's grade is the band
  store's claim of a signature. Re-verifying the answer's own signature is ``levain consent receipts verify``'s job.
* A cut's ``cause`` is not re-judged here (head note: it is judged AS OF THE CUT'S POSITION, never against today's fold).
* Per-close author verdicts (P4's tiers) are part B: :func:`close_verdicts` is the seam and returns nothing.
"""
from __future__ import annotations

import argparse
import base64
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

__all__ = ["BUNDLE_FORMAT", "EXIT_BREAK", "EXIT_USAGE", "FileReport", "Trail", "close_verdicts", "register", "run_export",
           "run_verify", "verify_trail"]

BUNDLE_FORMAT = "levain-board-trail/1"
FILES = ("items.jsonl", "repos.jsonl")
SHORT = {"items.jsonl": "items", "repos.jsonl": "repos"}
EXIT_BREAK = 3
EXIT_USAGE = 2
READ_WAIT = 2.0
TRAIL_DIR = "board-trail"
SIGNED_WORDS = "operator, signed with a ceremony"
NO_AUTHORITY_FOR = "cannot be checked: no authority log to verify a signature against ({why})"
_HEAD_RE = re.compile(r"[0-9a-f]{64}")


class TrailError(Exception):
    """The trail cannot be built or read at all (exit 2), as opposed to a break in what it holds (exit 3)."""


# ------------------------------------------------------------------------------------------------------------- the input


@dataclass
class Trail:
    """What a verification stands on: each file's lines AS WRITTEN (``bytes``, split on ``\\n`` only), whether a file ends in an
    unterminated line, the store id, the authority snapshot (None: none could be read, ``why_no_authority`` says why) and the
    band rows the relayed records cite (np id -> :class:`~levain.desk.board_relay.BandRow` or a refusal string)."""
    store_id: str
    lines: dict[str, list[bytes]]
    unterminated: dict[str, bool]
    snapshot: Any
    why_no_authority: str = ""
    band: Any = None                                    # a BandSource, built from the live band or from the bundle
    rows: dict[str, Any] = field(default_factory=dict)  # live reading only: filled by :func:`collect_answers`


def _split(raw: bytes) -> tuple[list[bytes], bool]:
    parts = raw.split(b"\n")
    tail = parts[-1] != b""
    if not tail:
        parts.pop()
    return parts, tail


def read_live(target: bs.Target, *, data_root: str | None, band_dir: str | None) -> Trail:
    """The live store under one shared hold (the same bounded wait ``board state`` makes), the authority log replayed now and
    the band opened lazily. A store with no ``repos.jsonl`` has an empty registry file."""
    deadline = time.monotonic() + READ_WAIT
    raws: dict[str, bytes] = {}
    try:
        hold = bs.store_lock(target.store_dir, exclusive=False, deadline=deadline) if target.pointered else None
        if hold is not None:
            hold.__enter__()
        try:
            for name in FILES:
                path = target.items_path if name == bs.ITEMS_NAME else target.store_dir / name
                raws[name] = bs._read_stable(path, fcntl.LOCK_SH, deadline) if os.path.lexists(path) else b""
            sid = bs.read_store_id(target.store_dir) if target.pointered or os.path.lexists(target.store_dir / bs.STORE_ID_NAME) else ""
        finally:
            if hold is not None:
                hold.__exit__(None, None, None)
    except (bs.BoardError, OSError) as exc:
        raise TrailError(f"the store cannot be read: {exc}") from None
    lines, unterminated = {}, {}
    for name in FILES:
        lines[name], unterminated[name] = _split(raws[name])
    snapshot, why = None, ""
    try:
        snapshot = sg.read_authority(sg.resolve_authority(data_root=data_root))
    except bs.BoardError as exc:
        why = str(exc)
    return Trail(sid, lines, unterminated, snapshot, why, _LiveBand(band_dir))


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


class _BundleBand:
    """The cited answers a bundle carries, as a band source."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self._answers = answers

    def row(self, np_id: str) -> rel.BandRow:
        got = self._answers.get(np_id)
        if got is None:
            raise rel.BandRefusal(f"{np_id} is not among the answers this bundle carries")
        if "unreadable" in got:
            raise rel.BandRefusal(f"{np_id} could not be read when the bundle was made: {got['unreadable']}")
        return rel.BandRow(got["question"], got["answer"], got["answer_grade"], got["producer"])


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


def _answer_word(grade: str) -> str:
    if grade.startswith(("signed", "countersigned")):
        return "signed answer"
    if grade.startswith("unsigned"):
        return "unsigned answer"
    return "answer of unknown signing"


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


def close_verdicts(trail: Trail, reports: Sequence[FileReport]) -> list[str]:
    """PART B'S SEAM. One verdict per close, in P4's three tiers (signed commit / asserted by trailer / asserted by git author,
    and "unattributed"), filled from P4's ``closed_by`` once the restack onto P4 lands. Part A prints nothing for closes."""
    return []


def _signed_problem(rec: Mapping[str, Any], trail: Trail) -> tuple[sg.Verdict | None, str | None]:
    """``(verdict, problem)`` for a signed-grade record: the P2 verifier's verdict, or why none could be had."""
    from levain.authority_sig import VerifierUnavailable
    if trail.snapshot is None:
        return None, NO_AUTHORITY_FOR.format(why=trail.why_no_authority or "none was given")
    if trail.store_id and rec.get("store") != trail.store_id:
        return None, f"the signed record names store {rec.get('store')!r}, and this store is {trail.store_id!r}"
    try:
        return sg.verify_record(rec, trail.snapshot), None
    except VerifierUnavailable as exc:
        return None, f"the signature cannot be checked here ({exc})"


def _manifest_sha(rec: Mapping[str, Any], begins: Mapping[tuple[Any, Any], str]) -> str | None:
    if rec.get("op") == "begin-track":
        return rec.get("manifest_sha256")
    return begins.get((rec.get("repo"), rec.get("migration")))


def _relayed(rec: Mapping[str, Any], trail: Trail, begins: Mapping[tuple[Any, Any], str]) -> tuple[bool, str]:
    kind, name = _author_of(rec)
    got = rel.verify_relayed(rec, trail.band)
    if not got.ok:
        return False, f"relayed citation does not hold: {got.text}"
    src = rec["source"]
    if rec.get("op") in ("begin-track", "track"):
        manifest = _manifest_sha(rec, begins)
        if manifest is None:
            return False, "relayed citation does not hold: no begin-track of this migration records a manifest digest to bind"
        row = rel._row_of(trail.band, src["np"])
        said = rel.check_manifest(rec, row, manifest)
        if not said.ok:
            return False, f"relayed citation does not hold: {said.text}"
    return True, f"agent {name}, relaying {src['np']} ({_answer_word(got.grade or '')})"


def verify_file(name: str, trail: Trail, heads: Mapping[str, str]) -> FileReport:
    lines = trail.lines[name]
    rep = FileReport(name, len(lines), line_head(lines))
    chain = bc.check_chain(lines)
    breaks = chain.by_line()
    acks = _acks(lines, chain)
    begins: dict[tuple[Any, Any], str] = {}
    last_signed: tuple[int, Mapping[str, Any]] | None = None
    relayed = 0
    unterminated_at = len(lines) if trail.unterminated.get(name) else 0

    def acked_by(n: int) -> str:
        m, _ = acks[n]
        who = bc.record_of(lines[m - 1]) or {}
        kind, aname = _author_of(who)
        return f"{aname} ({who.get('grade', '?')}, line {m})"

    for n, line in enumerate(lines, 1):
        rec = bc.record_of(line)
        if n == unterminated_at:
            rep.breaks.append(f"line {n}: no terminating newline (a write in flight, or a torn tail)")
        if rec is None:
            if n in acks:
                rep.notes.append(f"line {n} torn, acknowledged by {acked_by(n)}")
            else:
                rep.breaks.append(f"line {n}: torn (not a JSON object); `levain board ack-torn --file {name} --line {n}` after repairing or accepting it")
            continue
        if n in breaks:
            brk = breaks[n]
            if n in acks and acks[n][1].chain:
                rep.notes.append(f"line {n} chain break, acknowledged by {acked_by(n)}: {brk.problem}")
            else:
                rep.breaks.append(f"line {n}: {brk.problem}")
        grade = rec.get("grade")
        pre_cutover = chain.cutover is None or n < chain.cutover
        what = _what(rec)
        if pre_cutover and "grade" not in rec:
            rep.records.append(RecordLine(n, what, "legacy"))
            continue
        if grade == sg.GRADE_LEGACY:
            rep.records.append(RecordLine(n, what, "legacy"))
            continue
        kind, who = _author_of(rec)
        if grade in sg.SIGNED_GRADES or "sig" in rec:
            verdict, problem = _signed_problem(rec, trail)
            if problem is not None or verdict is None or verdict.state == "bad":
                why = problem if problem is not None else (verdict.text if verdict else "signature does not verify")
                rep.records.append(RecordLine(n, what, why, False))
                rep.breaks.append(f"line {n}: {what}: {why}")
                continue
            if verdict.state == "revoked":
                rep.records.append(RecordLine(n, what, f"{SIGNED_WORDS}, by a key revoked since (seq {verdict.revoked_seq})"))
                rep.notes.append(f"line {n}: signed by {verdict.fingerprint}, revoked at seq {verdict.revoked_seq}; the record's own time is the signer's claim")
            else:
                rep.records.append(RecordLine(n, what, SIGNED_WORDS))
            last_signed = (n, rec)
            continue
        if grade == sg.GRADE_RELAYED:
            ok, text = _relayed(rec, trail, begins)
            rep.records.append(RecordLine(n, what, text, ok))
            if ok:
                relayed += 1
            else:
                rep.breaks.append(f"line {n}: {what}: {text}")
        elif grade == bs.GRADE_ASSERTED:
            if kind == "?":
                text = "asserted, but the record names no author"
                rep.records.append(RecordLine(n, what, text, False))
                rep.breaks.append(f"line {n}: {what}: {text}")
            else:
                rep.records.append(RecordLine(n, what, f"asserted by {who}, unsigned"))
        else:
            text = f"{grade!r} is not a grade" if "grade" in rec else "a record after the cutover carries no grade"
            rep.records.append(RecordLine(n, what, text, False))
            rep.breaks.append(f"line {n}: {what}: {text}")
        if rec.get("op") == "begin-track" and isinstance(rec.get("manifest_sha256"), str):
            begins[(rec.get("repo"), rec.get("migration"))] = rec["manifest_sha256"]
    # the anchor, the tail
    records_after = lambda n0: sum(1 for k in range(n0, len(lines)) if bc.record_of(lines[k]) is not None)   # noqa: E731
    if last_signed is None:
        rep.anchor = f"{name}: no signed record: nothing in this file is anchored by the operator's key"
        rep.tail = (f"{name}: {records_after(0)} records, none after a signed record: all are unanchored: "
                    "anyone who can write this file could have rewritten them")
    else:
        n, rec = last_signed
        at_or_before = sorted(int(m.group(1)) for m in (re.match(r"line (\d+)[:\s]", b) for b in rep.breaks) if m and int(m.group(1)) <= n)
        rep.anchor = (f"{name}: NOT ANCHORED: the file breaks at line {at_or_before[0]}, at or before the last signed record (line {n}); "
                      f"nothing up to it can be called committed by the operator's key" if at_or_before else
                      f"{name}: anchored through line {n} (signed at {rec.get('at', '?')}): every record up to it is committed by the "
                      f"operator's key")
        after = records_after(n)
        rep.tail = (f"{name}: {after} records after the last signed record (at {rec.get('at', '?')}) are unanchored: anyone who can "
                    f"write this file could have rewritten them" if after else
                    f"{name}: 0 records after the last signed record (at {rec.get('at', '?')}): the tail is anchored")
    rep.relayed = (f"{name}: {relayed} relayed record(s) are anchored to his words by the band row's signature, which does not cover "
                   f"the chain" if relayed else "")
    if name in heads:
        h = heads[name]
        hashes = [bc.line_hash(x) for x in lines]
        empty = hashlib.sha256(b"").hexdigest()
        if h in hashes:
            j = len(hashes) - 1 - hashes[::-1].index(h)
            later = len(lines) - 1 - j
            rep.truncation = (f"{name}: kept head {h[:12]} is the current head: nothing was removed" if later == 0 else
                              f"{name}: kept head {h[:12]} is line {j + 1}; {later} lines were written after it (the file grew)")
        elif h == empty and not lines:
            rep.truncation = f"{name}: kept head {h[:12]} is the empty file's, and the file is empty"
        else:
            rep.truncation = (f"{name}: TRUNCATION OR REWRITE: kept head {h[:12]} is not on this chain (the file now ends at "
                              f"{rep.head[:12]}): records were removed or rewritten")
            rep.breaks.append(rep.truncation)
    return rep


def verify_trail(trail: Trail, heads: Mapping[str, str] | None = None) -> list[FileReport]:
    return [verify_file(name, trail, heads or {}) for name in FILES]


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
    """Every band answer a relayed record cites, as the band holds it now (or why it could not be read)."""
    out: dict[str, Any] = {}
    for name in FILES:
        for line in trail.lines[name]:
            rec = bc.record_of(line)
            src = rec.get("source") if rec else None
            if not isinstance(src, Mapping) or not isinstance(src.get("np"), str) or src["np"] in out:
                continue
            try:
                row = rel._row_of(trail.band, src["np"])
                out[src["np"]] = {"producer": row.producer, "question": row.question, "answer": row.answer,
                                  "answer_grade": row.answer_grade}
            except rel.BandRefusal as exc:
                out[src["np"]] = {"unreadable": str(exc)}
    return out


def _key_json(key: Any) -> dict:
    return {f.name: getattr(key, f.name) for f in dataclasses.fields(key)}


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
    }


def load_bundle(path: Path) -> Trail:
    from levain.authority import AuthorityKey, AuthoritySnapshot, StandingPolicy
    try:
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise TrailError(f"{path} cannot be read as a trail bundle ({type(exc).__name__})") from None
    if not isinstance(doc, dict) or doc.get("format") != BUNDLE_FORMAT:
        raise TrailError(f"{path} is not a {BUNDLE_FORMAT} bundle")
    try:
        files = doc["files"]
        lines = {name: [_line_back(x) for x in files[name]["lines"]] for name in FILES}
        unterminated = {name: bool(files[name].get("unterminated")) for name in FILES}
        store = doc["store"]
        answers = doc["answers"]
        auth = doc["authority"]
        if not isinstance(store, str) or not isinstance(answers, dict) or not isinstance(auth, dict):
            raise KeyError("shape")
        snapshot, why = None, ""
        if "unavailable" in auth:
            why = str(auth["unavailable"])
        else:
            known = {f.name for f in dataclasses.fields(AuthorityKey)}
            keys = {}
            for k in auth["keys"]:
                key = AuthorityKey(**{a: b for a, b in k.items() if a in known})
                keys[key.fingerprint] = key
            snapshot = AuthoritySnapshot(store_id=auth["store_id"], seq=auth["seq"], head_hash=auth["head_hash"], keys=keys,
                                         policy=StandingPolicy())
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise TrailError(f"{path} is a damaged bundle ({type(exc).__name__}: {exc})") from None
    return Trail(store, lines, unterminated, snapshot, why, _BundleBand(answers))


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
    fname = f"trail-{(store_id or 'store')[:8]}-{stamp}.json"
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
    repo = _inside_git(dest.parent)
    if repo is not None:
        raise TrailError(f"{dest} is inside a git work tree ({repo}): the bundle carries his words and never goes into a repo; "
                         f"name a place outside one with --out")
    return dest


def _write_new(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TrailError(f"{dest} exists: a bundle is never overwritten") from None
    except OSError as exc:
        raise TrailError(f"{dest} cannot be written ({exc})") from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise TrailError(f"{dest} cannot be written ({exc})") from None


# --------------------------------------------------------------------------------------------------------------------- CLI


def _print_report(reports: Sequence[FileReport], trail: Trail) -> int:
    broken = 0
    for rep in reports:
        print(f"{rep.name} ({rep.lines} lines, head {rep.head})")
        for r in rep.records:
            print(f"  {rep.name}:{r.n} {r.text}: {r.verdict}" + ("" if r.ok else "  [BREAK]"))
        for note in rep.notes:
            print(f"  note: {note}")
        print(rep.anchor)
        print(rep.tail)
        if rep.relayed:
            print(rep.relayed)
        if rep.truncation:
            print(rep.truncation)
        for b in rep.breaks:
            print(f"BREAK {rep.name}: {b}")
        broken += len(rep.breaks)
    for line in close_verdicts(trail, reports):
        print(line)
    return broken


def _parse_heads(raw: Sequence[str]) -> dict[str, str]:
    long = {v: k for k, v in SHORT.items()}
    out: dict[str, str] = {}
    for item in raw:
        key, _, val = item.partition("=")
        if key not in long or not _HEAD_RE.fullmatch(val):
            raise TrailError(f"--head {item!r}: use items=<64 hex> or repos=<64 hex>")
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
        print(f"levain board trail export: refused: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"bundle: {dest}")
    print(f"items={bundle['heads']['items']} repos={bundle['heads']['repos']}")
    broke = sum(len(r.breaks) for r in verify_trail(trail))
    if broke:
        print(f"levain board trail export: note: this store does not verify ({broke} breaks); the bundle holds it as it is: "
              f"run `levain board trail verify`", file=sys.stderr)
    if trail.snapshot is None:
        print(f"levain board trail export: note: no authority log was read ({trail.why_no_authority}); the bundle carries no keys, "
              f"so its signatures cannot be checked from it", file=sys.stderr)
    return 0


def run_verify(args: argparse.Namespace) -> int:
    try:
        heads = _parse_heads(args.head)
        trail = load_bundle(Path(args.bundle)) if args.bundle else _live(args)
    except TrailError as exc:
        print(f"levain board trail verify: refused: {exc}", file=sys.stderr)
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
                       help="The cockpit data root whose authority log holds the operator's keys (default: the one the consent hooks name).")
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
                     "unanchored-tail line and, with --head, whether the kept head is still on the chain. Exits 0 with no break, "
                     "3 on any break (a chain break, a signature that does not verify, a relayed citation that does not hold, a "
                     "head that is gone), 2 when it cannot run."))
    v.add_argument("bundle", nargs="?", default=None, metavar="BUNDLE")
    common(v)
    v.add_argument("--head", action="append", default=[], metavar="FILE=HASH",
                   help="A head `export` printed (items=<hash> or repos=<hash>), repeatable: checked to still be on that file's chain.")
    v.set_defaults(func=run_verify)
