"""The A10 tree reader: §6.1 step 2a of S1 session 5 DESIGN, run by :func:`adopt_verify.quarantine_verify` inside the
confined verifier, after Q's pack passed ``index-pack --fsck-objects`` and the commit-count gate, before P and step 4.

In ONE pass over the graph the session's importer proposes it does two things:

- **The below-ERROR ids** (S4): every tree Q serves is checked for :func:`fsck_ids.reader_ids` (the list DERIVED by
  §4.2's one rule, never recalled here) by git's own definition of each, ported from ``fsck.c``'s ``fsck_tree`` at
  v2.50.1 (the same rules at v2.39.4 except ``largePathname``, which the floor git lacks): ``nullSha1`` an entry oid of
  all zeros; ``zeroPaddedFilemode`` a mode whose text starts with ``0``; ``fullPathname`` a ``/`` in a name;
  ``badFilemode`` a mode outside 100755, 100644, 120000, 040000, 160000 (git's ``strict`` reading: 100664 is bad too,
  and the whole parsed mode is compared where git compares it truncated to 16 bits, so Levain is the stricter);
  ``gitattributesSymlink`` a symlink at a ``.gitattributes`` name (HFS or NTFS spelling); ``largePathname`` an
  entry NAME longer than 4096 bytes (a per-entry length, not a depth); ``hasDotgit`` a ``.git`` name by
  :func:`special_paths.is_hfs_dotgit` / :func:`special_paths.is_ntfs_dotgit`, and every segment after a backslash by
  the NTFS one; ``hasDotdot`` a name ``..``. The first id of the derived list that a tree carries is refused
  ``fsck:<id>``. A tree B serves (his history) is read for its height ONLY.
- **Frame (a)'s depth bound**: every tree reachable from every commit in Q (each commit's ``tree`` line, read by the
  same bounded reader), plus every tree object in Q. Each tree is looked up in Q, then in B (Q wins an oid in both);
  a commit's root tree or a directory entry whose oid neither store has, or whose object is not a tree, is
  ``preview-graph-unresolved``. A directory is an entry whose mode is ``S_ISDIR`` (git's ``canon_mode`` test on the
  whole parsed mode); every other entry kind (a gitlink too) has height 1 and no traversal. height(tree) = the max
  over its entries of (1 + height(entry) for a directory, else 1); the empty tree is 0. Heights are memoised per oid,
  computed by an explicit stack whose frames hold only each tree's REMAINING subtree oids (deduped per tree), the
  memo checked at push AND at pop, so each unique tree is parsed once. END levels add
  :func:`walkframe.end_level_offset`. ``preview-cap:tree-depth`` as soon as the stack's depth in edges, or any tree's
  height, in END levels, passes :data:`walkframe.MAX_DEPTH`: the stack never holds more than ``MAX_DEPTH`` + 1 frames.

Each store is read through ONE ``git cat-file --batch-command`` process, strictly one request, one response (no
``--buffer``: git flushes after each command): ``info <oid>`` is the type probe (``--batch-check``'s answer, no
content), and ``contents <oid>`` is sent only after the probe answered ``tree`` (or ``commit`` for a commit), so a
huge blob named as a directory is refused from its probe and never streamed [judged by adopt-5dii, 10-22, against
§6.1 2a's "``--batch-check`` then ``--batch``": the same two answers from one process per store, git >= 2.36, the floor
is 2.39.4]. A commit is parsed only up to its ``tree`` line; the rest of its one response is drained unparsed; a
commit whose probed size passes :data:`verify_caps.COMMIT_OBJECT_MAX` is refused ``preview-cap:commits`` (detail
``object-size``) from its probe. Every tree ``contents`` counts against V30's expanded-output cap
(:data:`verify_caps.EXPANDED_OUTPUT_CAP`, read at each check): before each ``contents`` request for a tree, the reader
refuses ``preview-cap:output`` if that tree's whole response (``<oid> tree <size>\n<payload>\n``, from its probe), added
to every tree response it already read, would pass the cap, so no tree past it is ever streamed (L3 r1, codex +
complement MED). There is no work cap: V30's time and memory caps bound a hostile fan (§6.1 2a "Cost").
"""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from levain.slot import Refused, fsck_ids, importer, special_paths, verify_caps, walkframe

#: git's ``max_tree_entry_len`` default (``fsck.c`` v2.50.1): ``largePathname`` is a name longer than this.
LARGE_NAME = 4096
_S_IFMT = 0o170000
_S_IFDIR = 0o040000
_S_IFLNK = 0o120000
#: ``fsck_tree``'s standard modes, ``strict`` (100664 is not among them).
_GOOD_MODES = frozenset({0o100755, 0o100644, _S_IFLNK, _S_IFDIR, 0o160000})
_CHUNK = 1 << 20
DEPTH = "preview-cap:tree-depth"
UNRESOLVED = "preview-graph-unresolved"


def _check_ids(mode_s: bytes, mode: int, name: bytes, oid: str) -> Iterator[str]:
    """The reader ids one entry carries, by ``fsck_tree``'s definitions (module docstring)."""
    if oid.strip("0") == "":
        yield "nullSha1"
    if mode_s[:1] == b"0":
        yield "zeroPaddedFilemode"
    if b"/" in name:
        yield "fullPathname"
    if mode not in _GOOD_MODES:
        yield "badFilemode"
    if mode & _S_IFMT == _S_IFLNK and special_paths.is_dotgitattributes(name):
        yield "gitattributesSymlink"
    if len(name) > LARGE_NAME:
        yield "largePathname"
    if name == b"..":
        yield "hasDotdot"
    if special_paths.is_hfs_dotgit(name) or special_paths.is_ntfs_dotgit(name):
        yield "hasDotgit"
    else:
        bs = name.find(b"\\")
        while bs >= 0:
            if special_paths.is_ntfs_dotgit(name, bs + 1):
                yield "hasDotgit"
                break
            bs = name.find(b"\\", bs + 1)


#: Every id :func:`_check_ids` can yield; the derived list must be a subset (checked at each read, never assumed).
CHECKED_IDS = frozenset({"nullSha1", "zeroPaddedFilemode", "fullPathname", "badFilemode", "gitattributesSymlink",
                         "largePathname", "hasDotdot", "hasDotgit"})


@dataclass
class Stats:
    """The reader's counts (logged as one ``reader:`` line; §9's rows read them)."""

    cq: int = 0                 # commits in Q (each read for its tree line)
    q_trees: int = 0            # tree objects in Q's inventory
    uq: int = 0                 # unique trees parsed from Q
    ub: int = 0                 # unique trees parsed from B
    lookups: int = 0            # type probes (``info``), commits' included
    parses: int = 0             # commit header reads + tree parses
    reparses: int = 0           # a tree parsed a second time (always 0: the memo)
    memo_hits: int = 0
    bytes_parsed: int = 0       # tree payloads + commit header lines
    bytes_out: int = 0          # tree responses counted against the expanded-output cap
    bytes_drained: int = 0      # the rest of each commit's response, read unparsed
    max_frames: int = 0
    max_pending: int = 0        # the peak pending frontier: subtree oids waiting in every frame
    max_height: int = 0         # the tallest tree's height (entry depth, not END levels)
    stop: str = ""              # "", "stack" or "height": which bound refused
    stop_end: int = 0           # the END level that bound saw when it refused (the refused frame's, for "stack")

    def as_dict(self) -> Dict[str, object]:
        return dict(self.__dict__)


class _Store:
    """One ``cat-file --batch-command`` process over one store (Q or B), one request, one response."""

    def __init__(self, runner: importer.GitRunner, label: str, hexlen: int) -> None:
        from levain.slot import adopt_verify as AV
        argv = importer.git_argv(runner.git, runner.work, *AV.GIT_C, "cat-file", "--batch-command")
        runner.started.append(argv)
        if runner.log is not None:
            runner.log.write((" ".join(argv) + "\n").encode("utf-8", "replace"))
            runner.log.flush()
        self.label = label
        self.hexlen = hexlen
        self._head = re.compile(rb"([0-9a-f]{%d}) (commit|tree|blob|tag) (0|[1-9][0-9]{0,19})\n" % hexlen)
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=importer.git_env(runner.work), cwd=str(runner.work), close_fds=True)
        self._err: List[bytes] = []
        self._drain = threading.Thread(target=importer._drain, args=(self.p.stderr, self._err), daemon=True)
        self._drain.start()

    def _fatal(self, detail: str) -> importer.Fatal:
        """A failure with cat-file's last stderr line (``GitRunner``'s bounded stderr reader, the ``_must`` tail): the
        process is reaped first, so its stderr is whole."""
        self.close()
        err = b"".join(self._err).decode("utf-8", "replace")
        last = (err.strip().splitlines() or [""])[-1][:300]
        return importer.Fatal(None, f"{detail}; stderr: {last}" if last else detail)

    def _ask(self, cmd: str, oid: str) -> Optional[Tuple[str, int]]:
        assert self.p.stdin is not None and self.p.stdout is not None
        try:
            self.p.stdin.write(f"{cmd} {oid}\n".encode("ascii"))
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise self._fatal(f"cat-file ({self.label}) is gone") from None
        line = self.p.stdout.readline(self.hexlen + 32)
        if line == f"{oid} missing\n".encode("ascii"):
            return None
        m = self._head.fullmatch(line)
        if m is None or m.group(1).decode("ascii") != oid:
            raise self._fatal(f"cat-file ({self.label}) answered {line[:80]!r}")
        return m.group(2).decode("ascii"), int(m.group(3))

    def info(self, oid: str) -> Optional[Tuple[str, int]]:
        return self._ask("info", oid)

    def _open(self, oid: str, kind: str, size: int) -> None:
        got = self._ask("contents", oid)
        if got != (kind, size):
            raise self._fatal(f"cat-file ({self.label}) contents {got!r} after probe {(kind, size)!r}")

    def _read(self, n: int) -> bytes:
        assert self.p.stdout is not None
        out = self.p.stdout.read(n)
        if len(out) != n:
            raise self._fatal(f"cat-file ({self.label}) ended inside an object")
        return out

    def _end(self) -> None:
        if self._read(1) != b"\n":
            raise self._fatal(f"cat-file ({self.label}) object not followed by a newline")

    def tree(self, oid: str, size: int) -> bytes:
        self._open(oid, "tree", size)
        payload = self._read(size)
        self._end()
        return payload

    def head(self, oid: str, kind: str, size: int, keep: int) -> bytes:
        """The first ``keep`` bytes of a ``kind`` object the probe sized at ``size``; the rest of its one response
        drained unparsed (the preview's reads, :mod:`levain.slot.adopt_preview`)."""
        self._open(oid, kind, size)
        out = self._read(min(keep, size))
        left = size - len(out)
        while left > 0:
            left -= len(self._read(min(_CHUNK, left)))
        self._end()
        return out

    def commit_tree(self, oid: str, size: int, stats: Stats) -> str:
        """The commit's ``tree`` oid from its first line; the rest of the response drained unparsed."""
        self._open(oid, "commit", size)
        want = 5 + self.hexlen + 1
        head = self._read(min(want, size))
        stats.bytes_parsed += len(head)
        left = size - len(head)
        while left > 0:
            n = len(self._read(min(_CHUNK, left)))
            stats.bytes_drained += n
            left -= n
        self._end()
        if len(head) != want or not head.startswith(b"tree ") or head[-1:] != b"\n" \
                or not re.fullmatch(rb"[0-9a-f]+", head[5:-1]):
            raise Refused("fsck:other", f"commit {oid}: no tree line first")   # index-pack's fsck refuses it first
        return head[5:-1].decode("ascii")

    def close(self) -> None:
        for f in (self.p.stdin, self.p.stdout):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self._drain.join()


@dataclass
class _Frame:
    oid: str
    pending: List[str]
    best: int


def _inventory(path: Path, hexlen: int) -> Tuple[List[str], List[str]]:
    """Q's commits and tree objects from 2a's inventory (``%(objectname) %(objecttype)``)."""
    line_re = re.compile(rb"([0-9a-f]{%d}) (commit|tree|blob|tag)" % hexlen)
    commits: List[str] = []
    trees: List[str] = []
    with importer._read_own(path) as fh:
        for line in importer._lines(fh, "the inventory"):
            m = line_re.fullmatch(line)
            if m is None:
                raise importer.Fatal(None, "the inventory: a line that is not '<oid> <type>'")
            if m.group(2) == b"commit":
                commits.append(m.group(1).decode("ascii"))
            elif m.group(2) == b"tree":
                trees.append(m.group(1).decode("ascii"))
    return commits, trees


def read_trees(q: importer.GitRunner, b: importer.GitRunner, inventory: Path, hexlen: int, *,
               ids: Optional[Sequence[str]] = None, max_depth: int = walkframe.MAX_DEPTH,
               offset: Optional[int] = None, log=None) -> Stats:
    """2a over Q (and B for lookups): returns the counts, or raises :class:`Refused` (``fsck:<id>``,
    ``fsck:other``, ``preview-cap:tree-depth``, ``preview-cap:commits``, ``preview-cap:output``,
    ``preview-graph-unresolved``)."""
    ids = tuple(fsck_ids.reader_ids() if ids is None else ids)
    unknown = set(ids) - CHECKED_IDS
    if unknown:
        raise importer.Fatal(None, f"the derived reader ids hold {sorted(unknown)}, which this reader cannot check")
    off = walkframe.end_level_offset() if offset is None else offset
    stats = Stats()
    commits, q_tree_objs = _inventory(inventory, hexlen)
    stats.cq, stats.q_trees = len(commits), len(q_tree_objs)
    memo: Dict[str, int] = {}
    parsed: set = set()
    qs = _Store(q, "Q", hexlen)
    try:
        bs = _Store(b, "B", hexlen)
        try:
            _walk(qs, bs, commits, q_tree_objs, ids, max_depth, off, stats, memo, parsed)
        finally:
            bs.close()
    finally:
        qs.close()
        if log is not None:                 # the log never changes the verdict (L3 r1: ENOSPC here replaced it)
            from levain.contracts import canon
            try:
                log.write(b"reader: " + canon.canonical_bytes(stats.as_dict()) + b"\n")
                log.flush()
            except OSError:
                pass
    return stats


def _walk(qs: _Store, bs: _Store, commits: List[str], q_tree_objs: List[str], ids: Sequence[str], max_depth: int,
          off: int, stats: Stats, memo: Dict[str, int], parsed: set) -> None:
    hexlen = qs.hexlen

    def lookup(oid: str, want: str) -> Tuple[_Store, int]:
        stats.lookups += 1
        got = qs.info(oid)
        store = qs
        if got is None:
            stats.lookups += 1
            got = bs.info(oid)
            store = bs
        if got is None:
            raise Refused(UNRESOLVED, f"{want} {oid}: in neither the bundle nor the base")
        if got[0] != want:
            raise Refused(UNRESOLVED, f"{want} {oid}: a {got[0]}")
        return store, got[1]

    def frame(oid: str) -> _Frame:
        store, size = lookup(oid, "tree")
        if oid in parsed:
            stats.reparses += 1
        parsed.add(oid)
        cost = len(oid) + len(" tree ") + len(str(size)) + 1 + size + 1
        cap = verify_caps.EXPANDED_OUTPUT_CAP
        if stats.bytes_out + cost > cap:
            raise Refused("preview-cap:output", f"tree {oid}: the reader's tree output would reach "
                                                f"{stats.bytes_out + cost} bytes > {cap}")
        stats.bytes_out += cost
        payload = store.tree(oid, size)
        stats.parses += 1
        stats.bytes_parsed += len(payload)
        strict = store is qs
        if strict:
            stats.uq += 1
        else:
            stats.ub += 1
        best = 0
        pending: Dict[str, None] = {}
        hit: set = set()
        try:
            for mode_s, name, child in special_paths.tree_entries_raw(payload, hexlen):
                mode = int(mode_s, 8)
                if strict:
                    hit.update(_check_ids(mode_s, mode, name, child))
                if mode & _S_IFMT == _S_IFDIR:
                    h = memo.get(child)
                    if h is not None:
                        stats.memo_hits += 1
                        best = max(best, 1 + h)
                    else:
                        pending[child] = None
                else:
                    best = max(best, 1)
        except ValueError as e:
            raise Refused("fsck:other", f"a tree Levain's reader refuses ({'Q' if strict else 'B'}): {e}"[:300]) \
                from None
        for i in ids:
            if i in hit:
                raise Refused(f"fsck:{i}", f"tree {oid} (Levain's reader)")
        return _Frame(oid, list(reversed(pending)), best)

    def climb(root: str) -> None:
        if root in memo:
            stats.memo_hits += 1
            return
        stack = [frame(root)]
        pending_total = len(stack[0].pending)
        stats.max_frames = max(stats.max_frames, 1)
        stats.max_pending = max(stats.max_pending, pending_total)
        while stack:
            top = stack[-1]
            if top.pending:
                child = top.pending.pop()
                pending_total -= 1
                h = memo.get(child)
                if h is not None:                       # memoised between its push and its pop
                    stats.memo_hits += 1
                    top.best = max(top.best, 1 + h)
                    continue
                if len(stack) + off > max_depth:        # the new frame's edge, in END levels
                    stats.stop, stats.stop_end = "stack", len(stack) + off
                    raise Refused(DEPTH, f"stack past END level {max_depth} under tree {stack[0].oid}")
                f = frame(child)
                stack.append(f)
                pending_total += len(f.pending)
                stats.max_frames = max(stats.max_frames, len(stack))
                stats.max_pending = max(stats.max_pending, pending_total)
            else:
                stack.pop()
                stats.max_height = max(stats.max_height, top.best)
                if top.best + off > max_depth:
                    stats.stop, stats.stop_end = "height", top.best + off
                    raise Refused(DEPTH, f"tree {top.oid} reaches END level {top.best + off} > {max_depth}")
                memo[top.oid] = top.best
                if stack:
                    stack[-1].best = max(stack[-1].best, 1 + top.best)

    for c in commits:
        stats.lookups += 1
        got = qs.info(c)
        if got is None or got[0] != "commit":
            raise importer.Fatal(None, f"commit {c} from Q's inventory: probe {got!r}")
        if got[1] > verify_caps.COMMIT_OBJECT_MAX:
            raise Refused("preview-cap:commits", f"object-size: commit {c} is {got[1]} bytes > "
                                                 f"{verify_caps.COMMIT_OBJECT_MAX}")
        root = qs.commit_tree(c, got[1], stats)
        stats.parses += 1
        climb(root)
    for t in q_tree_objs:
        climb(t)


__all__ = ["CHECKED_IDS", "DEPTH", "LARGE_NAME", "Stats", "UNRESOLVED", "read_trees"]
