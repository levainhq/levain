"""levain.desk.repos_fold — the registry's pure fold: the lines of ``repos.jsonl`` -> each repo's mode and attention.

Stdlib only and no levain import beyond :mod:`levain.desk.board_chain` (also stdlib only), like :mod:`levain.desk.board_state`:
it reads nothing, writes nothing, and judges only the lines it is handed. The writer, the verbs and the locking are
:mod:`levain.desk.board_registry`.

A record is ``{"v": 1, "store": <store.id>, "op": ..., "repo": <name>, "at": ..., "by": ..., "words": ..., ...}`` (design
4.2); ``author`` and ``grade`` ride on it and this fold ignores them. ``prev`` is the chain's: a break in it is a torn line.

Two kinds of trouble, as in the item store's fold:

* a TORN line (not JSON, not an object, an unknown version or op, a missing or ill-shaped required field, another store's
  id, an unterminated tail, or a line where the hash chain breaks: :func:`levain.desk.board_chain.check_chain`) makes the
  whole registry torn: nothing can be trusted past it. ``torn`` and ``torn_why``
  say so, and every reader refuses until an ``ack-torn`` record names exactly that line and the sha256 of its bytes (a chain break
  also names its ``want`` and ``have``, and the line is then KEPT: see :mod:`levain.desk.board_chain`).
* a well-formed record that no state allows (a ``track`` on an untracked repo, a ``register`` over another path) is a
  PROBLEM line: skipped, listed, never fatal.

``ack-torn`` is ``{"op": "ack-torn", "line": N, "sha256": <of the torn line's bytes>, "why": ...}``. Lines are numbered from 1
in the order given, blank lines counted, so the caller splits on ``\\n`` only. The hash is of the line as given, newline
excluded, encoded UTF-8. An ack that names a line that is not torn, or whose hash differs, is a problem and skips nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from levain.desk import board_chain

__all__ = ["ACK", "MODES", "OPS", "Repo", "RepoFold", "check_manifest", "fold_repos", "line_sha256"]

SCHEMA = 1
ACK = "ack-torn"
OPS = ("register", "begin-track", "track", "abandon-track", "abandon-done", "untrack", "pause", "resume", "move")
MODES = ("untracked", "tracking", "aborting", "tracked")
_REPO_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*#[1-9][0-9]*")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Repo:
    """One registry entry, derived. ``since`` is the ``at`` of the record that put it in its present mode."""

    name: str
    path: str
    origin: str
    root: tuple[str, ...]
    mode: str = "untracked"
    attention: str = "active"
    since: str = ""
    words: str = ""
    migration: str | None = None            # M of the ceremony in progress or last run
    owner: str | None = None
    charter_before: str | None = None
    manifest: Mapping[str, Any] | None = None
    manifest_sha256: str | None = None
    begin_at: str | None = None
    track_at: str | None = None
    charter_blob: str | None = None
    retire: tuple[Mapping[str, str], ...] = ()
    record_dir: str | None = None
    begins: int = 0                          # how many begin-track records this repo has had (a new ceremony's M differs)
    via: str | None = None                   # the ``via`` marker of the register record that created the repo (``import-focus``: the import wrote it)
    history: int = 0                         # records the fold accepted for this repo after its creating ``register``

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "mode": self.mode, "attention": self.attention, "since": self.since,
                "words": self.words, "migration": self.migration, "owner": self.owner, "origin": self.origin,
                "root": list(self.root), "charter_before": self.charter_before, "charter_blob": self.charter_blob,
                "retire": [dict(r) for r in self.retire], "record_dir": self.record_dir, "manifest": self.manifest,
                "manifest_sha256": self.manifest_sha256, "begin_at": self.begin_at, "track_at": self.track_at}


@dataclass(frozen=True)
class RepoFold:
    repos: Mapping[str, Repo]
    problems: tuple[str, ...] = ()
    torn: bool = False
    torn_why: str = ""
    acked: tuple[int, ...] = ()
    torn_lines: tuple[tuple[int, str], ...] = field(default=())     # (line, sha256) of each torn line, acked or not
    dropped: tuple[tuple[int, int], ...] = ()                       # (line, the line of the ack that dropped it): torn lines skipped
    kept: tuple[tuple[int, int], ...] = ()                          # (line, the line of the ack): chain-broken lines KEPT and applied
    malformed: tuple[int, ...] = ()                                 # lines torn by themselves (shape), chain break or not
    records: tuple[tuple[int, Mapping[str, Any]], ...] = ()         # (line, record) of every record the fold accepted, acks excluded
    applied: tuple[int, ...] = ()                                   # lines of the records the fold APPLIED: accepted and allowed by the state (a soft rejection is not)


_NOT_JSON = object()


class _Bad(Exception):
    """A torn line."""


class _Soft(Exception):
    """A well-formed record the state does not allow."""


def line_sha256(line: str | bytes) -> str:
    """sha256 of a line: a ``bytes`` line is hashed as it is on disk, a ``str`` line as its UTF-8."""
    return hashlib.sha256(line if isinstance(line, bytes) else line.encode("utf-8")).hexdigest()


def _str(rec: Mapping[str, Any], key: str, *, empty_ok: bool = False) -> str:
    v = rec.get(key)
    if not isinstance(v, str) or (not empty_ok and not v.strip()):
        raise _Bad(f"{key} must be {'a string' if empty_ok else 'a non-empty string'}")
    return v


def _repo_name(rec: Mapping[str, Any]) -> str:
    name = _str(rec, "repo")
    if not _REPO_RE.fullmatch(name):
        raise _Bad(f"repo {name!r} is not a registry name")
    return name


def _retire(value: Any) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list):
        raise _Bad("retire must be a list of {repo, path}")
    out = []
    for e in value:
        if not isinstance(e, dict) or set(e) != {"repo", "path"} or not isinstance(e["repo"], str) \
                or not _REPO_RE.fullmatch(e["repo"]) or not isinstance(e["path"], str) or not e["path"].strip():
            raise _Bad("a retire entry is exactly {repo, path}")
        if e["path"].startswith("/") or ".." in e["path"].split("/"):
            raise _Bad("a retire path is relative to its repo's root (no leading / and no ..)")
        out.append({"repo": e["repo"], "path": e["path"]})
    return tuple(out)


def _manifest(value: Any) -> dict:
    """``plan_items`` are the KEYS ``k`` of X's own items (each is added as ``M:<k>``; the ids exist only after the add, so the
    manifest cannot name them); ``contract_items`` are the repos that get a stubs item; ``retire`` and ``record_dir`` are optional."""
    if not isinstance(value, dict):
        raise _Bad("manifest must be an object")
    plan, contract = value.get("plan_items"), value.get("contract_items")
    if not isinstance(plan, list) or len(set(plan)) != len(plan) or not all(isinstance(k, str) and k and not re.search(r"\s", k) for k in plan):
        raise _Bad("manifest plan_items must be a list of distinct item keys (no whitespace)")
    if not isinstance(contract, list) or not all(isinstance(i, str) and _REPO_RE.fullmatch(i) for i in contract):
        raise _Bad("manifest contract_items must be a list of repo names")
    if "retire" in value:
        _retire(value["retire"])
    if "record_dir" in value and not (isinstance(value["record_dir"], str) and value["record_dir"]):
        raise _Bad("manifest record_dir must be a non-empty string")
    return value


def check_manifest(value: Any) -> str | None:
    """Why ``value`` is not a ceremony manifest, or None."""
    try:
        _manifest(value)
    except _Bad as e:
        return str(e)
    return None


def _validate(rec: Mapping[str, Any], store_id: str | None) -> None:
    """Shape only: raises :class:`_Bad` on a record that cannot be trusted."""
    if rec.get("v") != SCHEMA:
        raise _Bad(f"unknown record version {rec.get('v')!r}")
    op = rec.get("op")
    if op != ACK and op not in OPS:
        raise _Bad(f"unknown op {op!r}")
    _str(rec, "at")
    _str(rec, "by")
    if store_id is not None and rec.get("store") != store_id:
        raise _Bad(f"evidence from another store ({rec.get('store')!r}, this store is {store_id!r})")
    if op == ACK:
        try:
            board_chain.parse_ack(rec)              # the one judge of an ack's shape
        except board_chain.AckError as exc:
            raise _Bad(str(exc)) from None
        return
    _repo_name(rec)
    _str(rec, "words", empty_ok=True)
    if op in ("register", "move"):
        path = _str(rec, "path")
        if not path.startswith("/"):
            raise _Bad("path must be absolute")
        _str(rec, "origin", empty_ok=True)
        root = rec.get("root")
        if not isinstance(root, list) or not root or not all(isinstance(r, str) and r for r in root):
            raise _Bad("root must be a non-empty list of commit ids")
    elif op == "begin-track":
        _str(rec, "migration")
        _manifest(rec.get("manifest"))
        if not isinstance(rec.get("manifest_sha256"), str) or not _HASH_RE.fullmatch(rec["manifest_sha256"]):
            raise _Bad("manifest_sha256 must be 64 lowercase hex")
        if rec.get("owner") is not None:
            _str(rec, "owner")
        _str(rec, "charter_before")
    elif op == "track":
        _str(rec, "migration")
        _str(rec, "charter_blob")
        _retire(rec.get("retire"))
    elif op == "abandon-track":
        _str(rec, "migration")
    elif op == "abandon-done" and rec.get("migration") is not None:
        _str(rec, "migration")


def _apply(rec: Mapping[str, Any], repos: dict[str, Repo]) -> None:
    """Apply one record the fold accepts. ``history`` counts every record after the one that created the repo, and only those
    the fold ACCEPTED (a torn line never reaches here; a record that is a problem raises before the count)."""
    from dataclasses import replace

    existed = rec["repo"] in repos
    _apply_one(rec, repos)
    if existed:
        repos[rec["repo"]] = replace(repos[rec["repo"]], history=repos[rec["repo"]].history + 1)


def _apply_one(rec: Mapping[str, Any], repos: dict[str, Repo]) -> None:
    from dataclasses import replace

    op, name, at = rec["op"], rec["repo"], rec["at"]
    cur = repos.get(name)
    if op == "register":
        if cur is None:
            repos[name] = Repo(name, rec["path"], rec["origin"], tuple(sorted(rec["root"])), since=at, words=rec["words"],
                               via=rec["via"] if isinstance(rec.get("via"), str) else None)
        elif cur.path != rec["path"]:
            raise _Soft(f"register {name}: already registered at {cur.path}, not {rec['path']}; use move")
        return
    if cur is None:
        raise _Soft(f"{op} {name}: the repo was never registered")
    if op == "move":
        repos[name] = replace(cur, path=rec["path"], origin=rec["origin"], root=tuple(sorted(rec["root"])))
    elif op == "begin-track":
        if cur.mode == "tracking" and cur.migration == rec["migration"]:
            return                                                  # a retry of the same ceremony
        if cur.mode != "untracked":
            raise _Soft(f"begin-track {name}: the repo is {cur.mode}, not untracked")
        repos[name] = replace(cur, mode="tracking", since=at, words=rec["words"], migration=rec["migration"],
                              owner=rec.get("owner"), charter_before=rec["charter_before"], manifest=rec["manifest"],
                              manifest_sha256=rec["manifest_sha256"], begin_at=at, track_at=None, charter_blob=None,
                              retire=(), record_dir=rec["manifest"].get("record_dir"), begins=cur.begins + 1)
    elif op == "track":
        if cur.mode == "tracked" and cur.migration == rec["migration"]:
            return
        if cur.mode != "tracking" or cur.migration != rec["migration"]:
            raise _Soft(f"track {name}: the repo is {cur.mode} (migration {cur.migration}), not tracking {rec['migration']}")
        repos[name] = replace(cur, mode="tracked", since=at, words=rec["words"], track_at=at,
                              charter_blob=rec["charter_blob"], retire=_retire(rec["retire"]))
    elif op == "abandon-track":
        if cur.mode == "aborting" and cur.migration == rec["migration"]:
            return
        if cur.mode != "tracking" or cur.migration != rec["migration"]:
            raise _Soft(f"abandon-track {name}: the repo is {cur.mode} (migration {cur.migration}), not tracking {rec['migration']}")
        repos[name] = replace(cur, mode="aborting", since=at, words=rec["words"])
    elif op == "abandon-done":
        if cur.mode == "untracked":
            return
        if cur.mode != "aborting":
            raise _Soft(f"abandon-done {name}: the repo is {cur.mode}, not aborting")
        if rec.get("migration") != cur.migration:
            raise _Soft(f"abandon-done {name}: it names migration {rec.get('migration')!r}, the aborting ceremony is {cur.migration!r}")
        repos[name] = replace(cur, mode="untracked", since=at, words=rec["words"])
    elif op == "untrack":
        if cur.mode != "tracked":
            raise _Soft(f"untrack {name}: the repo is {cur.mode}, not tracked")
        repos[name] = replace(cur, mode="untracked", since=at, words=rec["words"])
    elif op in ("pause", "resume"):
        want = "paused" if op == "pause" else "active"
        if cur.attention != want:
            repos[name] = replace(cur, attention=want)


def fold_repos(lines: Iterable[str], *, store_id: str | None = None, unterminated_tail: bool = False) -> RepoFold:
    """The registry from its record lines. ``store_id`` (the store's ``store.id``) makes a record that names another store
    torn; ``unterminated_tail`` says the last line has no newline (an append in flight, or a crash), and it is torn."""
    lines = list(lines)
    breaks = board_chain.check_chain(lines).by_line()
    acks: dict[int, list[tuple[int, board_chain.Ack]]] = {}  # (the ack's own line, ack); ANY ack that honours the line accepts it: a corrected re-ack repairs
    parsed: list[Any] = []
    texts: list[str | None] = []                 # None: the line is not text (bytes that are not UTF-8, or not a str at all)
    shas: list[str] = []                         # of the line's RAW bytes: a bytes line is hashed as given, never as decoded text
    for n, raw in enumerate(lines, 1):
        rec: Any = _NOT_JSON
        text: str | None = None
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        elif isinstance(raw, str):
            text = raw
        texts.append(text)
        shas.append(line_sha256(raw) if isinstance(raw, (str, bytes)) else "")
        if text is not None:
            try:
                rec = json.loads(text)
            except (ValueError, RecursionError):
                rec = _NOT_JSON
        parsed.append(rec)
        if isinstance(rec, dict) and rec.get("op") == ACK and n not in breaks:      # only from a line that itself parses and chains
            try:
                _validate(rec, store_id)
                ack = board_chain.parse_ack(rec)
            except (_Bad, board_chain.AckError):
                continue
            if ack is not None:
                acks.setdefault(ack.line, []).append((n, ack))
    repos: dict[str, Repo] = {}
    problems: list[str] = []
    torn_why = ""
    acked: list[int] = []
    torn_lines: list[tuple[int, str]] = []
    dropped: list[tuple[int, int]] = []
    kept: list[tuple[int, int]] = []
    malformed: list[int] = []
    records: list[tuple[int, Mapping[str, Any]]] = []
    applied: list[int] = []
    last = len(lines)
    for n, raw in enumerate(lines, 1):
        sha = shas[n - 1]
        brk = breaks.get(n)
        mine = acks.get(n, ())
        bad: _Bad | None = None
        try:
            if texts[n - 1] is None:
                raise _Bad("not valid UTF-8" if isinstance(raw, bytes) else f"not text ({type(raw).__name__})")
            if not texts[n - 1].strip():
                continue
            rec = parsed[n - 1]
            if unterminated_tail and n == last:
                raise _Bad("no newline at the end (an append in flight, or a crash)")
            if rec is _NOT_JSON:
                raise _Bad("not JSON")
            if not isinstance(rec, dict):
                raise _Bad("a record must be a JSON object")
            _validate(rec, store_id)
            if n in breaks and board_chain.prev_ill_formed(rec):
                raise _Bad(board_chain.PREV_BAD)
        except _Bad as e:
            bad = e
        if bad is not None:
            malformed.append(n)
            torn_lines.append((n, sha))
            # a malformed line is dropped by a TORN-style ack of its bytes; a chain ack clears only the chain break (never this)
            hit = next((pos for pos, a in mine if a.sha256 == sha and not a.chain), None)
            if hit is not None:
                acked.append(n)                 # skipped exactly; listed in ``acked``, not a problem
                dropped.append((n, hit))
                continue
            if brk is not None and any(board_chain.honours(a, sha, brk) for _, a in mine):
                note = "; its chain break is acknowledged, but the line is torn for itself: acknowledge it as a torn line"
            elif any(a.chain and a.sha256 == sha for _, a in mine):
                note = "; a chain-style ack names it, but the line is torn for itself (not a chain break): acknowledge it as a torn line"
            elif n in acks:
                note = "; an ack-torn names it with a different sha256" if brk is None else "; an ack-torn names it, but not as it stands now"
            else:
                note = ""
            problems.append(f"line {n}: torn ({bad}){note}" if note.startswith("; an ack-torn names it with") else f"line {n}: torn: {bad}{note}")
            torn_why = torn_why or f"line {n}: {bad}" + (f" ({brk.problem})" if brk is not None else "")
            continue
        if brk is not None:
            hit = next((pos for pos, a in mine if board_chain.honours(a, sha, brk)), None)
            if hit is None:
                changed = "; an ack-torn names it, but not this break as it stands now" if n in acks else ""
                problems.append(f"line {n}: torn: {brk.problem}{changed}")
                torn_why = torn_why or f"line {n}: {brk.problem}{changed}"
                torn_lines.append((n, sha))
                continue
            torn_lines.append((n, sha))
            acked.append(n)                     # a chain break acknowledged: the record is KEPT, and applied below
            kept.append((n, hit))
        if n in acks and brk is None:
            problems.append(f"line {n}: ack-torn names this line, which is not torn")
        if rec["op"] != ACK:
            records.append((n, rec))
            try:
                _apply(rec, repos)
            except _Soft as e:
                problems.append(f"line {n}: {e}")
            else:
                applied.append(n)
    return RepoFold(repos, tuple(problems), bool(torn_why), torn_why, tuple(acked), tuple(torn_lines), tuple(dropped), tuple(kept),
                    tuple(malformed), tuple(records), tuple(applied))
