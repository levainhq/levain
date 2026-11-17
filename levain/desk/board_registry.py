"""levain.desk.board_registry — the registry (``repos.jsonl``): which repos are in play, and each one's mode.

Design 4.1 and 4.2 (``project_memory/1117_two_mode_board/design.md``). The pure fold is :mod:`levain.desk.repos_fold`; this
module is the verbs, one module, every write under :func:`levain.desk.board_store.store_lock`:

* ``register|move|begin-track|track|abandon-track|abandon-done|untrack|pause|resume|ack-torn`` and ``board use --import-focus``;
* the read the CLI prints (:func:`report`), with I3's stale-stub check, the path check and the 72-hour audit;
* the I1 check ``board add`` makes (:func:`check_add`) and the launch hook (:func:`register_for_launch`, I5).

A record is built by the item store's single builder (:func:`levain.desk.board_store.build_record`: author, grade, the key
checks) with the repo name riding in its id slot, then reshaped to 4.2's key order (``v``, ``store``, ``op``, ``repo``, ``at``,
``by``, ``words``, ...). :func:`_reg_append` is the registry's twin of ``board_store._append`` and not a call to it, because
that function reads every line as an ITEM (a valid ``<repo>#<n>`` id, a clean file) and a registry record has none; it
uses the same lock, inode re-check, ``_write_all``, ``chain_prev`` and ``prev`` stamp.

Item facts the verbs need (the keys ``M:<k>``, whether an item reads ``cut``) are read straight from the item records' own
fields, never derived, so this module needs no evidence. The one exception is ``untrack``, which asks the board's derive.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from levain.desk import board_store as bs
from levain.desk import repos_fold as rf
from levain.desk.board_store import BoardError

__all__ = ["REPOS_NAME", "STUB_MARKER", "TRACKING_AUDIT_HOURS", "ack_torn", "abandon_done", "abandon_track", "begin_track",
           "check_add", "import_focus_use", "move", "pause", "read_registry", "register", "register_for_launch", "report",
           "resume", "stale_audit", "track", "untrack"]

REPOS_NAME = "repos.jsonl"
STUB_MARKER = "<!-- levain: tracked stub -->"
TRACKING_AUDIT_HOURS = 72
CHARTER_REF = "refs/remotes/origin/main"
LAUNCH_WORDS = "first seat launched here"
LAUNCH_AUTHOR = {"kind": "agent", "name": "levain launch"}
GIT_TIMEOUT = 30
MIGRATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")


# ------------------------------------------------------------------------------------------------------------------ git


def _git(path: str | Path, *args: str) -> tuple[int, str]:
    from levain.desk.board_evidence import _env
    try:
        cp = subprocess.run(["git", "-C", str(path), *args], capture_output=True, timeout=GIT_TIMEOUT, env=_env())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return 127, f"git: {type(exc).__name__}"
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip() if cp.returncode == 0 else cp.stderr.decode("utf-8", "replace").strip()


def identity(path: str | Path, *, top: bool = True) -> dict[str, Any]:
    """``{path, origin, root}`` of the repository at ``path`` (its realpath; ``top``: the path must be the repository's top
    directory). Refuses a path that is not a repository or has no commit."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    rc, out = _git(real, "rev-parse", "--show-toplevel")
    if rc != 0:
        raise BoardError(f"{real} is not a git repository")
    if top and os.path.realpath(out) != real:
        raise BoardError(f"{real} is inside the repository at {os.path.realpath(out)}; name its top directory")
    real = os.path.realpath(out)
    rc, roots = _git(real, "rev-list", "--max-parents=0", "HEAD")
    if rc != 0 or not roots:
        raise BoardError(f"{real} has no commit yet: nothing identifies the repository")
    rc, origin = _git(real, "config", "--get", "remote.origin.url")
    return {"path": real, "origin": origin if rc == 0 else "", "root": sorted(roots.split())}


def _is_registered_repo(repo: rf.Repo) -> str | None:
    """None when ``repo.path`` still holds the registered repository, else the problem line (design 4.1)."""
    try:
        ident = identity(repo.path)
    except BoardError:
        return f"{repo.name}: {repo.path} is not the registered repository"
    if ident["origin"] != repo.origin or tuple(ident["root"]) != repo.root:
        return f"{repo.name}: {repo.path} is not the registered repository"
    return None


# ----------------------------------------------------------------------------------------------------------------- read


@dataclass(frozen=True)
class View:
    fold: rf.RepoFold
    store_id: str
    raw: bytes
    lines: tuple[str, ...]


def _store_id(store_dir: Path) -> str:
    p = store_dir / bs.STORE_ID_NAME
    if not p.exists():
        raise BoardError(f"{store_dir} holds no {bs.STORE_ID_NAME}; run `levain board use`")
    sid = bs._read_stable(p).decode("utf-8", "replace").strip()
    if not sid:
        raise BoardError(f"{p} is empty; run `levain board use`")
    return sid


def _split(raw: bytes) -> tuple[list[str], bool]:
    """The lines of ``raw`` split on ``\\n`` only, and whether the last one has no newline."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")                  # the replacement char makes the line torn (not JSON) or changes its hash: both visible
    parts = text.split("\n")
    tail = parts[-1] != ""
    if not tail:
        parts.pop()
    return parts, tail


def _load(store_dir: Path, *, flock: bool = True) -> View:
    """The registry as it is on disk. The caller holds ``store.lock``. A missing file with a store that holds items reads as
    unreadable, never empty; a missing file in a store with none is an empty registry."""
    sid = _store_id(store_dir)
    path = store_dir / REPOS_NAME
    if not path.exists():
        items = store_dir / bs.ITEMS_NAME
        if items.exists() and items.stat().st_size > 0:
            raise BoardError(f"the registry is unreadable: {REPOS_NAME} is missing from a store that holds items "
                             f"(register a repo to start it, or restore the file)")
        return View(rf.fold_repos([], store_id=sid), sid, b"", ())
    op = fcntl.LOCK_SH if flock else None
    raw = bs._read_stable(path, op)
    lines, tail = _split(raw)
    if tail:                                                    # an append in flight? read once more before judging it
        raw = bs._read_stable(path, op)
        lines, tail = _split(raw)
    return View(rf.fold_repos(lines, store_id=sid, unterminated_tail=tail), sid, raw, tuple(lines))


def read_registry(target: bs.Target) -> View:
    """The registry under the shared lock. Refuses with no pointer (the registry is not active) and on a torn file."""
    bs._require_pointered(target)
    with bs.store_lock(target.store_dir, exclusive=False):
        view = _load(target.store_dir)
    if view.fold.torn:
        raise BoardError(_torn_text(view))
    return view


def _torn_text(view: View) -> str:
    n = next((ln for ln, _ in view.fold.torn_lines if ln not in view.fold.acked), None)
    hint = f"; after repairing or accepting it: levain board ack-torn --file {REPOS_NAME} --line {n} --why \"...\"" if n else ""
    return f"the registry is unreadable: {view.fold.torn_why}{hint}"


def _items_facts(store_dir: Path) -> tuple[dict[str, dict], dict[str, str | None]]:
    """``(adds, decision)`` from the item records' own fields: every ``add`` by id, and each id's current decision (the
    last ``decide``; ``reopen`` clears it). Lines this cannot read are skipped (the item fold is the one that judges them)."""
    path = store_dir / bs.ITEMS_NAME
    adds: dict[str, dict] = {}
    decision: dict[str, str | None] = {}
    if not path.exists():
        return adds, decision
    raw = bs._read_stable(path, fcntl.LOCK_SH)
    for line in raw.decode("utf-8", "replace").split("\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
            continue
        if rec.get("op") == "add":
            adds.setdefault(rec["id"], rec)
        elif rec.get("op") == "decide":
            decision[rec["id"]] = None if rec.get("decision") == "reopen" else rec.get("decision")
    return adds, decision


def _migration_items(store_dir: Path, name: str, migration: str) -> list[str]:
    """Ids of X's items keyed to ``migration`` (``M:<k>``), in store order. The stubs items (``M:stubs:<repo>``) are not X's
    ceremony items."""
    adds, _ = _items_facts(store_dir)
    stubs = f"{migration}:stubs:"
    return [i for i, r in adds.items() if r.get("repo") == name and isinstance(r.get("key"), str)
            and r["key"].startswith(migration + ":") and not r["key"].startswith(stubs)]


# ---------------------------------------------------------------------------------------------------------------- write


def _words(value: Any, *, required: bool = True) -> str:
    return bs._one_line(value, "words", required=required, verbatim=True)


def _record(op: str, repo: str | None, by: str, store_id: str, body: Mapping[str, Any], author: Mapping[str, Any],
            now: str | None = None) -> dict:
    """The record, built by the store's one builder and reshaped to 4.2's key order. ``repo`` rides in the builder's id slot."""
    base = bs.build_record(op, repo or "-", by, dict(body), now, author=author)
    out: dict[str, Any] = {"v": base.pop("v"), "store": store_id, "op": base.pop("op")}
    rid = base.pop("id")
    if repo is not None:
        out["repo"] = rid
    out.update(base)
    return out


def _reg_append(target: bs.Target, build: Callable[[rf.RepoFold, str, bytes], tuple[dict | None, bool]], *,
                filename: str = REPOS_NAME, allow_torn: bool = False) -> tuple[dict | None, bool]:
    """Under the exclusive lock, hand ``build(fold, store_id, raw)`` the registry read in this same locked read; it returns
    ``(record, write)`` and may raise BoardError. A written record gets ``prev`` (chain_prev of the bytes just read), is
    appended as one line, and fsynced. The same replaced-file and exact-bytes retry rules as the item store's ``_append``."""
    bs._require_pointered(target)
    path = target.store_dir / filename
    with bs.store_lock(target.store_dir, exclusive=True) as held:
        sid = _store_id(target.store_dir)
        line = None
        rec: dict | None = None
        for _ in range(bs.OPEN_RETRIES):
            fd = held.open(filename, os.O_RDWR | os.O_CREAT | os.O_APPEND, fcntl.LOCK_EX)
            try:
                empty = os.fstat(fd).st_size == 0
                raw = bs._read_fd(fd)
                if line is not None and line in raw.split(b"\n"):
                    return rec, True
                lines, tail = _split(raw)
                folded = rf.fold_repos(lines, store_id=sid, unterminated_tail=tail)
                if folded.torn and not allow_torn:
                    view = View(folded, sid, raw, tuple(lines))
                    raise BoardError(_torn_text(view))
                rec, write = build(folded, sid, raw)
                if write and rec is not None:
                    rec = bs._with_prev(rec, bs.chain_prev(raw))
                    line = bs.canonical_bytes(rec)
                    bs._write_all(fd, (b"\n" if raw and not raw.endswith(b"\n") else b"") + line + b"\n")
                if bs._same_file(fd, path):
                    if write and empty:
                        bs._fsync_dir_or_warn(target.store_dir, filename)
                    return rec, bool(write)
            finally:
                os.close(fd)
        raise BoardError(f"{path} was replaced while a write was in flight (a git operation?) {bs.OPEN_RETRIES} times; nothing was kept, retry")


def _name(value: Any) -> str:
    name = bs._one_line(value, "repo")
    if not bs.REPO_RE.fullmatch(name):
        raise BoardError(f"repo {name!r} must match {bs.REPO_RE.pattern}")
    return name


def _need_repo(fold: rf.RepoFold, name: str) -> rf.Repo:
    cur = fold.repos.get(name)
    if cur is None:
        raise BoardError(f"{name} is not registered; run `levain board register {name} --path <dir> --words \"...\"`")
    return cur


# ------------------------------------------------------------------------------------------------------------- verbs


def register(target: bs.Target, name: str, path: str, words: str, by: str, *, author: Mapping[str, Any],
             now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """Put ``name`` in play, untracked. Records the path's realpath, origin and root set. A known name at the same path is a
    no-op; at another path it refuses and names ``move``. Returns ``(repo as folded, written)``."""
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    ident = identity(path)

    def build(fold, sid, raw):
        cur = fold.repos.get(name)
        if cur is not None:
            if cur.path == ident["path"]:
                return None, False
            raise BoardError(f"{name} is already registered at {cur.path}, not {ident['path']}: use `levain board move {name} --path ...`")
        other = next((r.name for r in fold.repos.values() if r.path == ident["path"]), None)
        if other is not None:
            raise BoardError(f"{ident['path']} is already registered as {other}")
        return _record("register", name, by, sid, {"words": words, **ident}, author, now), True

    rec, written = _reg_append(target, build)
    return _fold_one(target, name), written


def _fold_one(target: bs.Target, name: str) -> rf.Repo | None:
    return read_registry(target).fold.repos.get(name)


def move(target: bs.Target, name: str, path: str, words: str, by: str, *, author: Mapping[str, Any],
         now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """Re-base a registered repo onto ``path``. The new path must hold the same repository (the same root commits)."""
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    ident = identity(path)

    def build(fold, sid, raw):
        cur = _need_repo(fold, name)
        if cur.path == ident["path"]:
            return None, False
        if tuple(ident["root"]) != cur.root:
            raise BoardError(f"{ident['path']} is not the repository {name} is registered as (its root commits differ)")
        other = next((r.name for r in fold.repos.values() if r.path == ident["path"] and r.name != name), None)
        if other is not None:
            raise BoardError(f"{ident['path']} is already registered as {other}")
        return _record("move", name, by, sid, {"words": words, **ident}, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def _set_attention(op: str, target: bs.Target, name: str, words: str, by: str, author, now) -> tuple[rf.Repo | None, bool]:
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    want = "paused" if op == "pause" else "active"

    def build(fold, sid, raw):
        cur = _need_repo(fold, name)
        if cur.attention == want:
            return None, False
        return _record(op, name, by, sid, {"words": words}, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def pause(target, name, words, by, *, author, now=None):
    return _set_attention("pause", target, name, words, by, author, now)


def resume(target, name, words, by, *, author, now=None):
    return _set_attention("resume", target, name, words, by, author, now)


def _blob(path: str, ref: str = CHARTER_REF) -> str:
    """The blob sha of CHARTER.md at ``ref``, or ``none`` when the ref has no such file. A ref that cannot be read refuses."""
    rc, _ = _git(path, "rev-parse", "--verify", "--quiet", ref)
    if rc != 0:
        raise BoardError(f"{ref} cannot be read in {path} (is there an origin/main?)")
    rc, out = _git(path, "rev-parse", "--verify", "--quiet", f"{ref}:CHARTER.md")
    return out if rc == 0 and out else "none"


def _derive_migration(name: str, manifest_sha: str, words: str, begins: int) -> str:
    return "m" + hashlib.sha256(f"{name}\0{manifest_sha}\0{words}\0{begins}".encode()).hexdigest()[:10]


def begin_track(target: bs.Target, name: str, manifest_file: str | Path, words: str, by: str, *, author: Mapping[str, Any],
                owner: str | None = None, migration: str | None = None, now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """X becomes ``tracking``. Refuses unless X is registered and untracked. The migration id is ``migration`` or derived from
    (name, manifest bytes, words, how many ceremonies X has had): a repeat with the same manifest and words after a lost
    response derives the same id and is a no-op (T2-a). Returns ``(repo, written)``."""
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    owner = bs._one_line(owner, "owner") if owner is not None else None
    try:
        data = Path(manifest_file).expanduser().read_bytes()
        manifest = json.loads(data)
    except (OSError, ValueError) as exc:
        raise BoardError(f"--manifest {manifest_file} cannot be read as JSON ({type(exc).__name__})") from None
    why = rf.check_manifest(manifest)
    if why:
        raise BoardError(f"--manifest {manifest_file}: {why}")
    sha = hashlib.sha256(data).hexdigest()
    if migration is not None and not MIGRATION_RE.fullmatch(migration):
        raise BoardError(f"migration id {migration!r} must match {MIGRATION_RE.pattern}")
    view = read_registry(target)
    cur = _need_repo(view.fold, name)
    if cur.mode == "untracked":                                 # git is read outside the lock
        problem = _is_registered_repo(cur)
        if problem:
            raise BoardError(problem)
        before = _blob(cur.path)
    else:
        before = cur.charter_before or "none"

    def build(fold, sid, raw):
        cur = _need_repo(fold, name)
        if cur.mode == "tracking":
            k = cur.begins - 1
            same = (migration or _derive_migration(name, sha, words, k)) == cur.migration
            if same and cur.manifest_sha256 == sha and cur.words == words and cur.owner == owner:
                return None, False                              # the retry of a begin that already landed
            raise BoardError(f"{name} is already tracking (migration {cur.migration}): finish it with `track` or `abandon-track`")
        if cur.mode != "untracked":
            raise BoardError(f"{name} is {cur.mode}, not untracked: begin-track needs an untracked repo")
        m = migration or _derive_migration(name, sha, words, cur.begins)
        body = {"words": words, "migration": m, "manifest": manifest, "manifest_sha256": sha, "owner": owner,
                "charter_before": before}
        return _record("begin-track", name, by, sid, body, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def _plan_ids(text: str) -> list[str]:
    """The ids on the charter's Plan lines (``- <repo>#<n>``)."""
    from levain.desk import charter
    out, in_plan = [], False
    for ln in text.split("\n"):
        if ln.startswith("## "):
            in_plan = ln[3:].strip() == "Plan"
            continue
        m = charter._PLAN_ID.match(ln) if in_plan else None
        if m:
            out.append(f"{m.group(1)}#{m.group(2)}")
    return out


def _charter_state(path: str, name: str, items: Mapping | None) -> tuple[str | None, str, list, list[str]]:
    """``(text, why, problems, warnings)`` of CHARTER.md on origin/main."""
    from levain.desk import charter
    text, why = charter._read_ref(Path(path), charter.DEFAULT_REF)
    if text is None:
        return None, why, [], []
    problems, warnings = charter.check_text(text, repo_name=name, items=items)
    return text, "", problems, warnings


def track(target: bs.Target, name: str, words: str, by: str, *, author: Mapping[str, Any], migration: str | None = None,
          now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """The one switchover record (design 5(d)). Refuses unless X is ``tracking``, its charter passes ``charter check`` on
    origin/main (I2), and the manifest's plan items (less any cut) and the charter's Plan ids are the same set, both ways.
    It does not wait for the stubs. A repeat on a ``tracked`` repo with the same migration and words is a no-op (T2-a)."""
    from levain.desk import charter
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    view = read_registry(target)
    cur = _need_repo(view.fold, name)
    if cur.mode == "tracked" and migration in (None, cur.migration) and cur.words == words:
        return cur, False
    if cur.mode != "tracking":
        raise BoardError(f"{name} is {cur.mode}, not tracking: track needs a begun ceremony")
    if migration is not None and migration != cur.migration:
        raise BoardError(f"{name} is tracking migration {cur.migration}, not {migration}")
    problem = _is_registered_repo(cur)
    if problem:
        raise BoardError(problem)
    items, note = charter._store(target.items_path)
    if items is None:
        raise BoardError(f"the items store {note}: cannot check the charter's Plan against it")
    text, why, problems, _ = _charter_state(cur.path, name, items)
    if text is None:
        raise BoardError(f"{name} has no readable CHARTER.md on origin/main ({why}): land one, then track")
    if problems:
        first = problems[0]
        raise BoardError(f"{name}'s CHARTER.md on origin/main is not valid ({len(problems)} problems; first: line {first.line}: "
                         f"{first.text}); run `levain charter check --repo {cur.path}`")
    rc, blob = _git(cur.path, "rev-parse", f"{CHARTER_REF}:CHARTER.md")
    if rc != 0 or not blob:
        raise BoardError(f"{name}'s CHARTER.md blob on origin/main cannot be read")
    plan = set(_plan_ids(text))
    manifest = cur.manifest or {}

    def build(fold, sid, raw):
        now_cur = _need_repo(fold, name)
        if now_cur.mode == "tracked" and now_cur.migration == cur.migration:
            return None, False
        if now_cur.mode != "tracking" or now_cur.migration != cur.migration:
            raise BoardError(f"{name} changed under the check ({now_cur.mode}); run it again")
        adds, decision = _items_facts(target.store_dir)
        by_key = {r["key"]: i for i, r in adds.items() if isinstance(r.get("key"), str) and r.get("repo") == name}
        expected, missing = set(), []
        for k in manifest.get("plan_items", []):
            iid = by_key.get(f"{cur.migration}:{k}")
            if iid is None:
                missing.append(k)
            elif decision.get(iid) != "cut":
                expected.add(iid)
        if missing:
            raise BoardError(f"the manifest's plan items {', '.join(missing)} were never added (levain board add --key {cur.migration}:<k> ...)")
        if expected != plan:
            gone = sorted(plan - expected)
            absent = sorted(expected - plan)
            parts = []
            if absent:
                parts.append(f"the manifest's items not in the charter's Plan: {', '.join(absent)}")
            if gone:
                parts.append(f"the charter's Plan ids not in the manifest: {', '.join(gone)}")
            raise BoardError("the charter's Plan and the manifest's plan items are not the same set (" + "; ".join(parts) + ")")
        body = {"words": words, "migration": cur.migration, "charter_blob": blob, "retire": list(manifest.get("retire", []))}
        return _record("track", name, by, sid, body, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def _abandon_record_hash(view: View, name: str, migration: str) -> str | None:
    """sha256 of the abandon-track record of ``migration`` as written (the cause the cuts carry)."""
    for line in reversed(view.lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("op") == "abandon-track" and rec.get("repo") == name and rec.get("migration") == migration:
            return hashlib.sha256(line.encode("utf-8")).hexdigest()
    return None


def abandon_track(target: bs.Target, name: str, words: str, by: str, *, author: Mapping[str, Any],
                  now: str | None = None) -> tuple[rf.Repo | None, list[str]]:
    """``tracking`` becomes ``aborting``; every item keyed to the ceremony's M is decided ``cut`` with the abandon's words and
    ``cause``, the hash of the abandon-track record. The registry record and the cuts are two files, so a crash can land the
    first without all the second: run again on an ``aborting`` repo and only the missing cuts are written (T2-d, T2-f).
    Returns ``(repo, ids cut by this call)``."""
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    view = read_registry(target)
    cur = _need_repo(view.fold, name)
    if cur.mode not in ("tracking", "aborting"):
        raise BoardError(f"{name} is {cur.mode}: abandon-track needs a tracking repo (after track, leave with `untrack`)")

    def build(fold, sid, raw):
        c = _need_repo(fold, name)
        if c.mode == "aborting" and c.migration == cur.migration:
            return None, False
        if c.mode != "tracking" or c.migration != cur.migration:
            raise BoardError(f"{name} changed under the abandon ({c.mode}); run it again")
        return _record("abandon-track", name, by, sid, {"words": words, "migration": c.migration}, author, now), True

    _reg_append(target, build)
    view = read_registry(target)
    m = cur.migration or ""
    cause = _abandon_record_hash(view, name, m)
    if cause is None:
        raise BoardError(f"no abandon-track record for {name} ({m}) was found after the write")
    cut = []
    with bs.store_lock(target.store_dir, exclusive=False):
        ids = _migration_items(target.store_dir, name, m)
        _, decision = _items_facts(target.store_dir)
    abandon_words = view.fold.repos[name].words if view.fold.repos[name].mode == "aborting" else words
    for iid in ids:
        if decision.get(iid) != "cut":
            bs.decide(target, iid, "cut", abandon_words, by, author=author, cause=cause)
            cut.append(iid)
    return view.fold.repos.get(name), cut


def _compensation_missing(cur: rf.Repo) -> str | None:
    """The charter compensation of an abort that is not yet in effect (design 5), or None."""
    if (cur.charter_before or "none") == "none":
        try:
            blob = _blob(cur.path)
        except BoardError as exc:
            return f"origin/main cannot be read ({exc})"
        if blob != "none":
            return "origin/main still has CHARTER.md (the repo had none before the ceremony): revert the landing"
    return None


def abandon_done(target: bs.Target, name: str, words: str, by: str, *, author: Mapping[str, Any],
                 now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """``aborting`` becomes ``untracked`` and the lock lifts. Refuses unless every item keyed to M reads cut AND the charter
    compensation holds. On an ``untracked`` repo it is a no-op."""
    name, words, by = _name(name), _words(words, required=False), bs._one_line(by, "by")
    view = read_registry(target)
    cur = _need_repo(view.fold, name)
    if cur.mode == "untracked":
        return cur, False
    if cur.mode != "aborting":
        raise BoardError(f"{name} is {cur.mode}: abandon-done needs an aborting repo")
    problems = []
    with bs.store_lock(target.store_dir, exclusive=False):
        ids = _migration_items(target.store_dir, name, cur.migration or "")
        _, decision = _items_facts(target.store_dir)
    uncut = [i for i in ids if decision.get(i) != "cut"]
    if uncut:
        problems.append(f"items not cut: {', '.join(uncut)} (run `levain board abandon-track {name}` again)")
    comp = _compensation_missing(cur)
    if comp:
        problems.append(f"charter compensation missing: {comp}")
    if problems:
        raise BoardError(f"abandon-done {name} refused: " + "; ".join(problems))

    def build(fold, sid, raw):
        c = _need_repo(fold, name)
        if c.mode == "untracked":
            return None, False
        if c.mode != "aborting" or c.migration != cur.migration:
            raise BoardError(f"{name} changed under the abandon-done ({c.mode}); run it again")
        return _record("abandon-done", name, by, sid, {"words": words, "migration": c.migration}, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def _unsettled_items(target: bs.Target, cur: rf.Repo) -> list[str]:
    """The ids of the repo's items that are neither done nor decided (cut, parked, out of 1.0). The board's own derive is
    asked, with the repo's git evidence; an item it cannot read counts as unsettled."""
    from levain.desk import board_evidence as be
    from levain.desk.board_state import Evidence, derive_all, fold

    lines = bs.read_item_lines(target)
    folded = fold(lines)
    mine = {i: it for i, it in folded.items.items() if it.repo == cur.name}
    if not mine:
        return []
    if folded.torn:
        return sorted(mine)
    ev, summary = be.read_git_evidence(Path(cur.path), repo_name=cur.name)
    ev["documents"] = be.read_documents(mine, Path(cur.path))
    board = derive_all(folded, Evidence.from_dict(ev), now=datetime.now(timezone.utc),
                       repo_evidence={cur.name: Evidence.from_dict(ev)}, primary_repo=cur.name)
    return sorted(s.id for s in board.items if s.repo == cur.name and s.state not in ("done", "cut", "parked", "out_of_1_0"))


def untrack(target: bs.Target, name: str, words: str, by: str, *, author: Mapping[str, Any],
            now: str | None = None) -> tuple[rf.Repo | None, bool]:
    """Leave tracked mode (a forward act). Refuses while the repo has an item that is not done, cut or parked."""
    name, words, by = _name(name), _words(words), bs._one_line(by, "by")
    view = read_registry(target)
    cur = _need_repo(view.fold, name)
    if cur.mode == "untracked":
        return cur, False
    if cur.mode != "tracked":
        raise BoardError(f"{name} is {cur.mode}: untrack needs a tracked repo")
    open_ids = _unsettled_items(target, cur)
    if open_ids:
        raise BoardError(f"untrack {name} refused: items not done, cut or parked: {', '.join(open_ids[:20])}"
                         f"{' ...' if len(open_ids) > 20 else ''}; decide each one first")

    def build(fold, sid, raw):
        c = _need_repo(fold, name)
        if c.mode == "untracked":
            return None, False
        if c.mode != "tracked":
            raise BoardError(f"{name} is {c.mode}, not tracked")
        return _record("untrack", name, by, sid, {"words": words}, author, now), True

    _, written = _reg_append(target, build)
    return _fold_one(target, name), written


def ack_torn(target: bs.Target, file: str, line: int, why: str, by: str, *, author: Mapping[str, Any],
             now: str | None = None) -> bool:
    """Append ``{"op": "ack-torn", "line": N, "sha256": <of that line's bytes>, "why": ...}`` to ``repos.jsonl`` or
    ``items.jsonl``: a reader then skips exactly that line. The hash is computed here from the bytes on disk. For the registry
    the line must be torn by the fold's rules; for the items file it must not be a clean record (the item fold, not this
    module, is the one that judges it). Returns False when the line is already acknowledged."""
    if file not in (REPOS_NAME, bs.ITEMS_NAME):
        raise BoardError(f"--file must be {REPOS_NAME} or {bs.ITEMS_NAME}")
    if not isinstance(line, int) or line < 1:
        raise BoardError("--line must be a positive line number")
    why, by = bs._one_line(why, "why"), bs._one_line(by, "by")

    def build(fold, sid, raw):
        parts = raw.split(b"\n")
        if parts and parts[-1] == b"":
            parts.pop()
        if line > len(parts):
            raise BoardError(f"{file} has {len(parts)} lines; there is no line {line}")
        sha = hashlib.sha256(parts[line - 1]).hexdigest()
        if file == REPOS_NAME:
            if line in fold.acked:
                return None, False
            if line not in {n for n, _ in fold.torn_lines}:
                raise BoardError(f"line {line} of {REPOS_NAME} is not torn")
        else:
            for rec_line in (parts[line - 1],):
                try:
                    rec = json.loads(rec_line)
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and isinstance(rec.get("id"), str) and bs.ID_RE.fullmatch(rec["id"]):
                    raise BoardError(f"line {line} of {file} is a clean record, not a torn one")
            for later in parts[line:]:
                try:
                    r2 = json.loads(later)
                except ValueError:
                    continue
                if isinstance(r2, dict) and r2.get("op") == rf.ACK and r2.get("line") == line and r2.get("sha256") == sha:
                    return None, False
        rec = _record(rf.ACK, None, by, sid, {"line": line, "sha256": sha, "why": why}, author, now)
        return rec, True

    if file == REPOS_NAME:
        _, written = _reg_append(target, build, allow_torn=True)
    else:
        _, written = _reg_append(target, build, filename=bs.ITEMS_NAME, allow_torn=True)
    return written


# --------------------------------------------------------------------------------------------------------------- I1, I5


def check_add(store_dir: Path, repo: str, key: str | None) -> None:
    """``board add``'s I1 check, called under the store's exclusive lock: the repo must be ``tracking`` (the add keyed to its
    migration) or ``tracked``. An ``aborting`` repo, an untracked or unknown one, and an unreadable registry refuse."""
    try:
        view = _load(store_dir)
    except BoardError as exc:
        raise BoardError(f"the registry cannot be read, so no item can be added: {exc}") from None
    if view.fold.torn:
        raise BoardError(f"no item can be added: {_torn_text(view)}")
    cur = view.fold.repos.get(repo)
    if cur is None:
        raise BoardError(f"{repo} is not registered: `levain board register {repo} --path <dir> --words \"...\"`, then track it")
    if cur.mode == "tracked":
        return
    if cur.mode == "tracking":
        if not (isinstance(key, str) and key.startswith((cur.migration or "") + ":")):
            raise BoardError(f"{repo} is tracking (migration {cur.migration}): an add for it must carry --key {cur.migration}:<k>")
        return
    raise BoardError(f"{repo} is {cur.mode}: the board holds items only for a tracking or tracked repo")


def register_for_launch(cwd: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> bool:
    """I5: once the registry is active (the pointer exists), Levain's own seat launch registers the repo it is about to work
    in, if the registry does not know it, BEFORE the launch is written (words: ``first seat launched here``). A directory
    that is not in a git repository registers nothing. Raises BoardError when the register fails: that refuses the launch.
    Returns True when a record was written."""
    store_dir = bs.read_pointer(environ)
    if store_dir is None:
        return False
    rc, top = _git(os.path.expanduser(str(cwd)), "rev-parse", "--show-toplevel")
    if rc != 0:
        return False
    ident = identity(top)
    target = bs.Target(store_dir, store_dir / bs.ITEMS_NAME, True)
    view = read_registry(target)
    for r in view.fold.repos.values():                  # a worktree of a known repo is that repo
        if r.path == ident["path"] or (tuple(ident["root"]) == r.root and ident["origin"] == r.origin):
            return False
    name = re.sub(r"[^a-z0-9._-]", "-", os.path.basename(ident["path"]).lower()).strip("-.")
    if not bs.REPO_RE.fullmatch(name or ""):
        raise BoardError(f"{ident['path']}: no registry name can be made from its directory name")
    _, written = register(target, name, ident["path"], LAUNCH_WORDS, LAUNCH_AUTHOR["name"], author=LAUNCH_AUTHOR)
    return written


# ----------------------------------------------------------------------------------------------------- report and audit


def _hours_since(at: str, now: datetime) -> float | None:
    try:
        t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - t).total_seconds() / 3600


def _next_step(target: bs.Target, cur: rf.Repo) -> str:
    """The first step of the ceremony whose effect is missing (design 5(a), abort)."""
    from levain.desk import charter
    m = cur.migration or ""
    if cur.mode == "aborting":
        with bs.store_lock(target.store_dir, exclusive=False):
            ids = _migration_items(target.store_dir, cur.name, m)
            _, decision = _items_facts(target.store_dir)
        uncut = [i for i in ids if decision.get(i) != "cut"]
        if uncut:
            return f"cut {', '.join(uncut)} (levain board abandon-track {cur.name} again)"
        comp = _compensation_missing(cur)
        return comp or f"levain board abandon-done {cur.name}"
    with bs.store_lock(target.store_dir, exclusive=False):
        adds, decision = _items_facts(target.store_dir)
    keys = {r["key"]: i for i, r in adds.items() if isinstance(r.get("key"), str) and r.get("repo") == cur.name}
    missing = [k for k in (cur.manifest or {}).get("plan_items", []) if f"{m}:{k}" not in keys]
    if missing:
        return f"add the plan items {', '.join(missing)} (levain board add --key {m}:<k>)"
    items, _ = charter._store(target.items_path)
    text, why, problems, _ = _charter_state(cur.path, cur.name, items)
    if text is None:
        return f"land CHARTER.md on origin/main ({why})"
    if problems:
        return f"fix CHARTER.md on origin/main ({len(problems)} problems: levain charter check --repo {cur.path})"
    expected = {keys[f"{m}:{k}"] for k in (cur.manifest or {}).get("plan_items", []) if decision.get(keys[f"{m}:{k}"]) != "cut"}
    if expected != set(_plan_ids(text)):
        return "make the charter's Plan match the manifest's plan items"
    return f"levain board track {cur.name}"


def _stale_stubs(view: View, cur: rf.Repo) -> list[str]:
    """I3: each retired path of a tracked repo that does not begin with the stub marker. A path is read in the repo it names,
    through that repo's registered path. A file that is gone holds nothing and is not stale."""
    out = []
    for e in cur.retire:
        owner = view.fold.repos.get(e["repo"])
        if owner is None:
            out.append(f"{e['repo']}/{e['path']}: its repo is not registered, so the stub cannot be checked")
            continue
        p = Path(owner.path) / e["path"]
        try:
            head = p.open("rb").readline().decode("utf-8", "replace").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            out.append(f"{p}: unreadable ({type(exc).__name__})")
            continue
        if head != STUB_MARKER:
            out.append(f"stale: {p} still holds pre-track content; the board is the source since {cur.track_at}")
    return out


def stale_audit(view_fold: rf.RepoFold, now: datetime) -> list[str]:
    """T2-b: a ``tracking`` or ``aborting`` repo older than 72 hours is an audit entry until it is finished or abandoned."""
    out = []
    for r in sorted(view_fold.repos.values(), key=lambda r: r.name):
        if r.mode in ("tracking", "aborting"):
            h = _hours_since(r.since, now)
            if h is None or h > TRACKING_AUDIT_HOURS:
                out.append(f"registry: {r.name} has been {r.mode} ({r.migration}) since {r.since or 'an unreadable time'}, "
                           f"over {TRACKING_AUDIT_HOURS} hours: finish it (`track`) or `abandon-track` it")
    return out


def audit_for_state(target: bs.Target, now: datetime) -> list[str]:
    """The registry's audit lines for ``board state`` (the 72-hour rule, and a torn registry). A registry that is absent or
    not active adds nothing here: ``levain board repos`` is what names that."""
    if not target.pointered:
        return []
    try:
        with bs.store_lock(target.store_dir, exclusive=False):
            view = _load(target.store_dir)
    except BoardError:
        return []
    out = [f"registry: {_torn_text(view)}"] if view.fold.torn else []
    return out + stale_audit(view.fold, now)


def report(target: bs.Target, now: datetime | None = None, *, check_paths: bool = True) -> dict[str, Any]:
    """The registry's derived entries and every problem, for ``levain board repos``. Reads git for each repo (the path
    check, the charter warnings of a tracked one, the next step of a ceremony); a failure of that is a problem line, never a
    crash. ``audit`` carries the 72-hour entries (T2-b)."""
    now = now or datetime.now(timezone.utc)
    bs._require_pointered(target)
    with bs.store_lock(target.store_dir, exclusive=False):
        view = _load(target.store_dir)
    problems = list(view.fold.problems)
    if view.fold.torn:
        raise BoardError(_torn_text(view))
    entries = []
    for r in sorted(view.fold.repos.values(), key=lambda r: r.name):
        d = r.to_dict()
        d["problems"], d["warnings"], d["next"] = [], [], None
        if check_paths:
            bad = _is_registered_repo(r)
            if bad:
                d["problems"].append(bad)
            elif r.mode in ("tracking", "aborting"):
                try:
                    d["next"] = _next_step(target, r)
                except (BoardError, OSError) as exc:
                    d["next"] = f"unreadable ({exc})"
            elif r.mode == "tracked":
                from levain.desk import charter
                text, why, probs, _ = _charter_state(r.path, r.name, None)
                if text is None:
                    d["warnings"].append(f"charter: {r.name} has no readable CHARTER.md on origin/main ({why})")
                elif probs:
                    d["warnings"].append(f"charter: {r.name}'s CHARTER.md on origin/main is not valid ({len(probs)} problems; "
                                         f"first: line {probs[0].line}: {probs[0].text})")
        if r.mode == "tracked":
            d["problems"] += _stale_stubs(view, r)
        entries.append(d)
    return {"store": view.store_id, "repos": entries, "problems": problems, "audit": stale_audit(view.fold, now)}


def format_report(doc: Mapping[str, Any]) -> list[str]:
    out = [f"registry of store {doc['store']}: {len(doc['repos'])} repos, {len(doc['audit'])} audit entries"]
    for d in doc["repos"]:
        head = f"  {d['name']}: {d['mode']}"
        if d["mode"] in ("tracking", "aborting"):
            head += f" ({d['migration']}), owner {d['owner'] or '-'}, since {d['since']}; next: {d['next']}"
        else:
            head += f", {d['attention']}, since {d['since']}"
        if d["attention"] == "paused" and d["mode"] in ("tracking", "aborting"):
            head += " [paused]"
        out.append(head + f"  [{d['path']}]")
        out += [f"    problem: {p}" for p in d["problems"]]
        out += [f"    warning: {w}" for w in d["warnings"]]
    out += [f"  problem: {p}" for p in doc["problems"]]
    out += [f"  audit: {a}" for a in doc["audit"]]
    return out


# --------------------------------------------------------------------------------------------- use --import-focus


def import_focus_use(directory: str | Path, focus_file: str | Path, *, by: str, author: Mapping[str, Any],
                     briefcase: str | Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[Path, bool, str]:
    """``levain board use DIR --import-focus FILE`` in design 4.2's order: import each entry as ``register`` (plus ``pause``),
    then replace ``FILE`` by the tombstone (atomic rename), then write the pointer LAST. A tombstone whose ``moved_to`` is this
    store's ``repos.jsonl`` is the proof that the import finished: the import is skipped and the pointer written. Returns
    ``(store dir, pointer changed, note)``. The records are written through the one privileged path ``use`` has before the
    pointer exists; every other writer refuses without it."""
    focus = Path(focus_file).expanduser()
    store_dir = Path(os.path.realpath(Path(directory).expanduser()))
    moved_to = str(store_dir / REPOS_NAME)
    try:
        data = focus.read_bytes()
        doc = json.loads(data)
    except (OSError, ValueError) as exc:
        raise BoardError(f"--import-focus {focus} cannot be read as JSON ({type(exc).__name__})") from None
    if not isinstance(doc, dict):
        raise BoardError(f"--import-focus {focus} must hold a JSON object")
    if "moved_to" in doc:
        if doc["moved_to"] != moved_to:
            raise BoardError(f"{focus} is a tombstone for {doc['moved_to']}, not this store ({moved_to})")
        dest, changed = bs.use(store_dir, environ)
        return dest, changed, "already imported; pointer written"
    base = Path(briefcase).expanduser() if briefcase else focus.resolve().parent.parent.parent
    entries = []
    for key, ent in sorted(doc.items()):
        name = str(key).lower()
        if not isinstance(ent, dict) or ent.get("state") not in ("active", "paused") or not isinstance(ent.get("words"), str):
            raise BoardError(f"--import-focus: entry {key!r} is not {{state: active|paused, words}}")
        if not bs.REPO_RE.fullmatch(name):
            raise BoardError(f"--import-focus: entry {key!r} is not a registry name")
        entries.append((name, str(base / key), ent["state"], ent["words"]))
    if len({n for n, *_ in entries}) != len(entries):
        raise BoardError("--import-focus: two entries lower-case to the same name")
    refused = []
    for name, path, state, words in entries:                          # nothing is written until every entry can be
        try:
            identity(path)
            _words(words)
        except BoardError as exc:
            refused.append(f"{name}: {exc}")
    if refused:
        raise BoardError("--import-focus refused, nothing written:\n  " + "\n  ".join(refused))
    try:                                                                # the store, without the pointer
        store_dir.mkdir(parents=True, exist_ok=True)
        with bs.store_lock(store_dir, exclusive=True, setup=True) as held:
            bs._write_store_id_once(held)
    except OSError as exc:
        raise BoardError(f"cannot set up the store at {store_dir}: {exc}") from None
    target = bs.Target(store_dir, store_dir / bs.ITEMS_NAME, True)      # privileged: open until the pointer is written
    for name, path, state, words in entries:
        register(target, name, path, words, by, author=author)
        if state == "paused":
            pause(target, name, words, by, author=author)
    if focus.read_bytes() != data:
        raise BoardError(f"{focus} changed while it was imported; nothing replaced it (run again)")
    tmp = focus.with_name(f".{focus.name}.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps({"moved_to": moved_to}) + "\n", encoding="utf-8")
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if focus.read_bytes() != data:
            raise BoardError(f"{focus} changed while it was imported; nothing replaced it (run again)")
        os.replace(tmp, focus)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BoardError(f"cannot write the tombstone over {focus}: {exc}") from None
    dest, changed = bs.use(store_dir, environ)
    return dest, changed, f"imported {len(entries)} repos; {focus} is now a tombstone"


# -------------------------------------------------------------------------------------------------------------- CLI


def register_cli(sub: Any, author_args: Callable[[Any], None], author_of: Callable[[Any], dict], items_help: str) -> None:
    """Add the registry's verbs under ``levain board``."""
    def common(p: Any, *, words: bool | str = True) -> None:
        p.add_argument("name", metavar="NAME")
        if words:
            p.add_argument("--words", required=words is True, default="", help="his words, verbatim")
        p.add_argument("--by", required=True)
        author_args(p)
        p.add_argument("--items", type=str, metavar="PATH", help=items_help)

    def run(fn: Callable[[Any, bs.Target], Any]) -> Callable[[Any], int]:
        def inner(args: Any) -> int:
            try:
                target = bs.resolve_store(args.items)
                fn(args, target)
            except BoardError as exc:
                print(f"levain board: refused: {exc}", file=sys.stderr)
                return 2
            return 0
        return inner

    def said(args, repo, written, verb, again):
        print(f"{verb if written else again} {args.name}" + (f" (migration {repo.migration})" if repo is not None and repo.migration and repo.mode in ("tracking", "aborting", "tracked") else ""))

    p = sub.add_parser("repos", description="The registry: every repo in play, its mode and attention, and what is wrong. A read.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--items", type=str, metavar="PATH", help=items_help)
    p.add_argument("--now", metavar="ISO", help="Judge the 72-hour audit at this instant (UTC, YYYY-MM-DDTHH:MM:SSZ).")

    def run_repos(args: Any) -> int:
        try:
            target = bs.resolve_store(args.items)
            now = datetime.now(timezone.utc)
            if args.now:
                try:
                    now = datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    raise BoardError(f"--now {args.now!r} must be YYYY-MM-DDTHH:MM:SSZ") from None
            doc = report(target, now)
        except BoardError as exc:
            print(f"levain board: refused: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            print("\n".join(format_report(doc)))
        return 3 if doc["audit"] or doc["problems"] or any(d["problems"] for d in doc["repos"]) else 0
    p.set_defaults(func=run_repos)

    p = sub.add_parser("register", description="Put a repo in play (untracked). Idempotent for the same path.")
    common(p)
    p.add_argument("--path", required=True)
    p.set_defaults(func=run(lambda a, t: said(a, *register(t, a.name, a.path, a.words, a.by, author=author_of(a)), "registered", "already registered")))

    p = sub.add_parser("move", description="Re-base a registered repo onto a new path (the same repository).")
    common(p)
    p.add_argument("--path", required=True)
    p.set_defaults(func=run(lambda a, t: said(a, *move(t, a.name, a.path, a.words, a.by, author=author_of(a)), "moved", "already at that path")))

    p = sub.add_parser("begin-track", description="Start the tracking ceremony for a registered, untracked repo.")
    common(p)
    p.add_argument("--manifest", required=True, metavar="FILE", help="JSON: plan_items (item keys), contract_items (repos), optional retire and record_dir.")
    p.add_argument("--owner", default=None, metavar="DESK", help="The desk that owns the ceremony.")
    p.add_argument("--migration", default=None, metavar="M", help="The migration id (default: derived, so a retry derives the same one).")
    p.set_defaults(func=run(lambda a, t: said(a, *begin_track(t, a.name, a.manifest, a.words, a.by, author=author_of(a), owner=a.owner, migration=a.migration), "tracking", "already tracking")))

    p = sub.add_parser("track", description="The switchover record: the board becomes the repo's source of truth.")
    common(p)
    p.add_argument("--migration", default=None, metavar="M")
    p.set_defaults(func=run(lambda a, t: said(a, *track(t, a.name, a.words, a.by, author=author_of(a), migration=a.migration), "tracked", "already tracked")))

    def run_abandon(a: Any, t: bs.Target) -> None:
        repo, cut = abandon_track(t, a.name, a.words, a.by, author=author_of(a))
        print(f"aborting {a.name}; cut {len(cut)} items" + (f": {', '.join(cut)}" if cut else ""))
    p = sub.add_parser("abandon-track", description="Abort a ceremony before track: the repo becomes aborting and its items are cut. Run again to finish the cuts.")
    common(p)
    p.set_defaults(func=run(run_abandon))

    p = sub.add_parser("abandon-done", description="Finish an abort: aborting becomes untracked once every item is cut and the compensation holds.")
    common(p, words=False)
    p.set_defaults(func=run(lambda a, t: said(a, *abandon_done(t, a.name, "", a.by, author=author_of(a)), "untracked", "already untracked")))

    p = sub.add_parser("untrack", description="Leave tracked mode. Refuses while an item is not done, cut or parked.")
    common(p)
    p.set_defaults(func=run(lambda a, t: said(a, *untrack(t, a.name, a.words, a.by, author=author_of(a)), "untracked", "already untracked")))

    for verb, fn in (("pause", pause), ("resume", resume)):
        p = sub.add_parser(verb, description=f"{verb.capitalize()} a repo's attention. Repeating it is a no-op.")
        common(p)
        p.set_defaults(func=run((lambda fn, verb: lambda a, t: said(a, *fn(t, a.name, a.words, a.by, author=author_of(a)), verb + "d", "already " + verb + "d"))(fn, verb)))

    p = sub.add_parser("ack-torn", description="Acknowledge one torn line of repos.jsonl or items.jsonl: readers then skip exactly that line.")
    p.add_argument("--file", required=True, choices=(REPOS_NAME, bs.ITEMS_NAME))
    p.add_argument("--line", required=True, type=int)
    p.add_argument("--why", required=True)
    p.add_argument("--by", required=True)
    author_args(p)
    p.add_argument("--items", type=str, metavar="PATH", help=items_help)

    def run_ack(a: Any, t: bs.Target) -> None:
        written = ack_torn(t, a.file, a.line, a.why, a.by, author=author_of(a))
        print(f"acknowledged {a.file} line {a.line}" if written else f"{a.file} line {a.line} is already acknowledged")
    p.set_defaults(func=run(run_ack))
