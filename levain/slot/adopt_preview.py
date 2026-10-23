"""§6.2 content: ``out/preview.txt``, written by the confined verifier (:mod:`levain.slot.verify_run`) after
:func:`adopt_verify.quarantine_verify` passed, from the analysis repo P it left in ``scratch/p`` (S1 session 5 DESIGN
§6.2, V30 §6.3; CONTRACT §10.3 "Display and preview").

What it shows, in order: the A4 header (:data:`adopt_verify.PREVIEW_HEADER`), the base and the tips, the banner when
``merge-base --is-ancestor <base> <head>`` is false (``UNRELATED TO BASE`` for an UNRELATED result, ``NOT ON BASE
(rewound to an ancestor)`` for a DELTA), every new commit's author, committer and message, the ``--raw -z`` change
lists (base..head, head..wip; ``diff-tree -r -t --no-renames``, its tree entries read for the embedded-bare check and
not listed), the config-surface list (CONTRACT §10.3's names on path components, case-insensitive, and every changed
directory holding ``HEAD`` + ``objects/`` + ``refs/``, with one ``safe.bareRepository=explicit`` hint), every symlink
with its target, and every case-fold / NFC collision pair in each tip's tree.

Every untrusted string goes through :func:`escape`, which asks :mod:`levain.unshowable` (the ONE rule for a character
a person cannot see) which characters hide. Each displayed path or field is capped at
:data:`verify_caps.DISPLAY_FIELD_MAX` bytes BEFORE escaping, then shows its byte length and a sha256 prefix. Each
section keeps at most :data:`verify_caps.SECTION_LINES` entries, then ``<dir>/: N more`` groupings by first component
(at most as many groups again, then one ``(other directories)`` line) and its count, so a section's memory is bounded
(the collision check alone holds one key and path per tree entry, inside the byte cap below). Every git output byte
counts against :data:`verify_caps.EXPANDED_OUTPUT_CAP` as it is read (an object read through ``cat-file`` from its
probed size, before its content), every listed path against
:data:`verify_caps.LISTED_PATHS_CAP`, and the file itself against :data:`verify_caps.PREVIEW_OUTPUT_CAP`: past any of
them = ``preview-cap:output``, and no ``preview.txt`` is left. Every cap is read at its check.

``report.json``'s named entries are NOT here: the verifier's only inputs are the bundle and the base bundle (§6.0), so
:func:`report_section` renders them for the CLI, which holds its private copy (S4).
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from levain import unshowable
from levain.slot import Refused, adopt_verify, fsck_reader, importer, special_paths, verify_caps

CAP = "preview-cap:output"
_CHUNK = 1 << 20
_S_IFMT = 0o170000
_S_IFDIR = 0o040000
_S_IFLNK = 0o120000
_META = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]+) ([0-9a-f]+) ([ADMT])")
BARE_HINT = ("hint: an embedded bare repository is listed. A git you run inside it runs its config (core.fsmonitor, "
             "hooks); safe.bareRepository=explicit in your git config stops that.")
NO_SURFACE = "no config surface changed"

# CONTRACT §10.3's config surfaces, matched on each path component after NFC + casefold. "CI files" and
# "Makefile-class" name no list there; these are [judged by adopt5dii-s2, 10-22, against §10.3's two class names].
_DIR_RULES = {".claude": ".claude/", ".codex": ".codex/", ".agents": ".agents/", ".husky": ".husky/",
              ".circleci": ".circleci/", ".buildkite": ".buildkite/"}
_NAME_RULES = {".mcp.json": ".mcp.json", ".envrc": ".envrc", ".pre-commit-config.yaml": ".pre-commit-config.yaml",
               "conftest.py": "conftest.py", "package.json": "package.json (scripts)", "makefile": "Makefile",
               "gnumakefile": "Makefile", "justfile": "Makefile", "rakefile": "Makefile", "jenkinsfile": "CI",
               ".travis.yml": "CI", "azure-pipelines.yml": "CI", "bitbucket-pipelines.yml": "CI", ".drone.yml": "CI",
               "taskfile.yml": "Makefile", "taskfile.yaml": "Makefile"}
_GLOB_RULES = (("claude*.md", "CLAUDE*.md"), ("agents*.md", "AGENTS*.md"), ("*.mk", "Makefile"), (".git*", ".git*"))
BARE_RULE = "embedded bare repository"


def _code(cp: int) -> str:
    if 0xDC80 <= cp <= 0xDCFF:              # surrogateescape's stand-in for a byte that is not UTF-8
        return "\\x%02x" % (cp - 0xDC00)
    return "\\u%04x" % cp if cp <= 0xFFFF else "\\U%08x" % cp


def escape(raw: bytes, *, allow: str = "", selectors: bool = False) -> str:
    """``raw`` for the terminal: a byte that is not UTF-8 = ``\\xNN``, every code point :mod:`levain.unshowable`
    calls unshowable = ``\\uXXXX`` (``\\UXXXXXXXX`` past U+FFFF), and ``\\`` itself = ``\\\\``, so the output is
    unambiguous. ``selectors=False`` is unshowable's path rule, ``True`` its prose rule."""
    text = raw.decode("utf-8", "surrogateescape").replace("\\", "\\\\")
    return unshowable.mark(text, _code, allow=allow, selectors=selectors)


def shown(raw: bytes, *, allow: str = "", selectors: bool = False) -> str:
    """:func:`escape` of at most :data:`verify_caps.DISPLAY_FIELD_MAX` bytes; a longer value then shows its byte length
    and a sha256 prefix."""
    cap = verify_caps.DISPLAY_FIELD_MAX
    if len(raw) <= cap:
        return escape(raw, allow=allow, selectors=selectors)
    return (f"{escape(raw[:cap], allow=allow, selectors=selectors)} "
            f"[{len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()[:16]}]")


def _fold(name: bytes) -> str:
    s = unicodedata.normalize("NFC", name.decode("utf-8", "surrogateescape"))
    return unicodedata.normalize("NFC", s.casefold())


def surface_rule(path: bytes) -> Optional[str]:
    """The config-surface rule a path matches on any of its components (the first match), or None."""
    parts = [_fold(c) for c in path.split(b"/")]
    for i, c in enumerate(parts):
        if c in _DIR_RULES:
            return _DIR_RULES[c]
        if c == ".vscode" and i + 1 < len(parts) and parts[i + 1] == "tasks.json":
            return ".vscode/tasks.json"
        if c in _NAME_RULES:
            return _NAME_RULES[c]
        for pat, label in _GLOB_RULES:
            if fnmatch.fnmatchcase(c, pat):
                return label
    return None


class _Budget:
    """The git bytes read and the paths listed, each against its V30 cap (read at each check)."""

    def __init__(self) -> None:
        self.git_bytes = 0
        self.paths = 0

    def take(self, n: int) -> None:
        self.git_bytes += n
        cap = verify_caps.EXPANDED_OUTPUT_CAP
        if self.git_bytes > cap:
            raise Refused(CAP, f"the preview's git output passes {cap} bytes")

    def path(self) -> None:
        self.paths += 1
        cap = verify_caps.LISTED_PATHS_CAP
        if self.paths > cap:
            raise Refused(CAP, f"the preview lists more than {cap} paths")


class _Section:
    """One section: at most :data:`verify_caps.SECTION_LINES` entries kept, the rest counted by group."""

    def __init__(self, title: str, empty: str = "none") -> None:
        self.title = title
        self.empty = empty
        self.n = 0
        self.lines: List[str] = []
        self.groups: Dict[str, int] = {}
        self.other = 0

    def add(self, lines: Callable[[], List[str]], group: Callable[[], str]) -> None:
        self.n += 1
        keep = verify_caps.SECTION_LINES
        if self.n <= keep:
            self.lines.extend(lines())
            return
        g = group()
        if g in self.groups:
            self.groups[g] += 1
        elif len(self.groups) < keep:
            self.groups[g] = 1
        else:
            self.other += 1

    def render(self) -> List[str]:
        out = [f"== {self.title} ({self.n}) =="]
        if not self.n:
            out.append(self.empty)
        out += self.lines
        out += [f"{g}: {c} more" for g, c in self.groups.items()]
        if self.other:
            out.append(f"(other directories): {self.other} more")
        return out


def _top(path: bytes) -> str:
    head, sep, _rest = path.partition(b"/")
    return f"{shown(head)}/" if sep else "./"


def _stream(p: importer.GitRunner, budget: _Budget, sep: bytes, on_record: Callable[[bytes], None],
            *args: str) -> None:
    """A git's stdout through a pipe, every byte counted by ``budget``, split at ``sep`` into ``on_record`` (on the
    pump thread). A refusal stops the pump, which closes the pipe (git dies on it); the refusal wins over git's
    exit."""
    rfd, wfd = os.pipe()
    failed: List[BaseException] = []

    def pump() -> None:
        buf = bytearray()
        try:
            while True:
                b = os.read(rfd, _CHUNK)
                if not b:
                    break
                budget.take(len(b))
                buf += b
                start = 0
                while True:
                    i = buf.find(sep, start)
                    if i < 0:
                        break
                    on_record(bytes(buf[start:i]))
                    start = i + 1
                del buf[:start]
            if buf:
                raise importer.Fatal(None, f"git {args[0]}: its output ended inside a record")
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            failed.append(e)
        finally:
            os.close(rfd)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        rc, _out, err = adopt_verify._g(p, *args, stdout_fd=wfd)
    finally:
        os.close(wfd)
        t.join()
    if failed:
        raise failed[0]
    if rc != 0:
        last = (err.strip().splitlines() or [""])[-1]
        raise importer.Fatal(None, f"git {args[0]} rc {rc}: {last[:300]}")


class _Reader:
    """P's objects through one ``cat-file --batch-command`` (:class:`fsck_reader._Store`), each response counted by the
    budget from its probe, before its content is read."""

    def __init__(self, p: importer.GitRunner, budget: _Budget, hexlen: int) -> None:
        self.store = fsck_reader._Store(p, "P", hexlen)
        self.budget = budget

    def head(self, oid: str, kind: str, keep: int) -> Tuple[bytes, int]:
        got = self.store.info(oid)
        if got is None or got[0] != kind:
            raise importer.Fatal(None, f"{kind} {oid} in P: probe {got!r}")
        size = got[1]
        self.budget.take(len(oid) + len(kind) + len(str(size)) + 3 + size)
        return self.store.head(oid, kind, size, keep), size

    def close(self) -> None:
        self.store.close()


@dataclass(frozen=True)
class _Change:
    status: str
    src_mode: int
    dst_mode: int
    dst_oid: str
    path: bytes


def _mode_text(c: _Change) -> str:
    if c.status == "A":
        return "%06o" % c.dst_mode
    if c.status == "D" or c.src_mode == c.dst_mode:
        return "%06o" % (c.src_mode if c.status == "D" else c.dst_mode)
    return "%06o->%06o" % (c.src_mode, c.dst_mode)


def _is_bare(payload: bytes, hexlen: int) -> bool:
    """A tree holding a non-directory ``HEAD`` and directories ``objects`` and ``refs`` (names after casefold: a
    case-insensitive checkout opens them alike) [judged by adopt5dii-s2, 10-22, against §6.2's "a directory holding
    ``HEAD`` + ``objects/`` + ``refs/``"]."""
    head = objects = refs = False
    for mode_s, name, _oid in special_paths.tree_entries_raw(payload, hexlen):
        is_dir = int(mode_s, 8) & _S_IFMT == _S_IFDIR
        n = _fold(name)
        if n == "head" and not is_dir:
            head = True
        elif n == "objects" and is_dir:
            objects = True
        elif n == "refs" and is_dir:
            refs = True
    return head and objects and refs


def _outside(path: bytes, target: bytes) -> bool:
    if target.startswith(b"/"):
        return True
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return joined == b".." or joined.startswith(b"../")


def _path_max() -> Optional[int]:
    try:
        return os.pathconf("/", "PC_PATH_MAX")
    except (OSError, ValueError):
        return None


@dataclass
class Stats:
    git_bytes: int = 0
    paths: int = 0
    commits: int = 0
    bytes_written: int = 0


def build(p: importer.GitRunner, v: adopt_verify.Verified, *, base_oid: str, hexlen: int,
          path_max: Optional[int] = None) -> Tuple[List[str], Stats]:
    """The preview's lines (each without its newline) from P, or :class:`Refused` (``preview-cap:output``,
    ``preview-cap:commits``) / :class:`importer.Fatal`."""
    budget = _Budget()
    pmax = _path_max() if path_max is None else path_max
    head, wip = v.refs.get("head"), v.refs.get("wip")
    lines = [adopt_verify.PREVIEW_HEADER, f"base {base_oid}"]
    lines += [f"{k} {o}" for k, o in (("head", head), ("wip", wip)) if o is not None]
    lines.append(f"result {v.result}")
    first = head if head is not None else wip
    assert first is not None
    rc, _o, err = adopt_verify._g(p, "merge-base", "--is-ancestor", base_oid, first)
    if rc == 1:
        what = "UNRELATED TO BASE" if v.result == "unrelated" else "NOT ON BASE (rewound to an ancestor)"
        lines.append(f"!!! {what}: {'head' if head else 'wip'} {first} does not descend from base {base_oid}; "
                     f"every new commit is listed below !!!")
    elif rc != 0:
        raise importer.Fatal(None, f"git merge-base rc {rc}: {(err.strip().splitlines() or [''])[-1][:300]}")
    tips = [o for o in (head, wip) if o is not None]
    reader = _Reader(p, budget, hexlen)
    try:
        commits = _commits(p, reader, budget, tips, base_oid if v.result == "delta" else None)
        lists: List[Tuple[str, str, str]] = []
        if head is not None:
            lists.append(("base..head", base_oid, head))
            if wip is not None:
                lists.append(("head..wip", head, wip))
        elif wip is not None:
            lists.append(("base..wip", base_oid, wip))
        config = _Section("config surface", NO_SURFACE)
        links = _Section("symlinks")
        bare = [False]
        changes = [_changes(p, reader, budget, label, a, b, hexlen, pmax, config, links, bare)
                   for label, a, b in lists]
        collisions = _Section("collisions")
        for label, tip in (("head", head), ("wip", wip)):
            if tip is not None:
                _collisions(p, budget, label, tip, collisions)
    finally:
        reader.close()
    lines += commits.render()
    for c in changes:
        lines += c.render()
    lines += config.render()
    if bare[0]:
        lines.append(BARE_HINT)
    lines += links.render()
    lines += collisions.render()
    return lines, Stats(budget.git_bytes, budget.paths, commits.n)


def _commits(p: importer.GitRunner, reader: _Reader, budget: _Budget, tips: Sequence[str],
             base_oid: Optional[str]) -> _Section:
    sec = _Section("commits")
    hexlen = reader.store.hexlen

    def one(rec: bytes) -> None:
        oid = rec.decode("ascii", "replace")
        if len(rec) != hexlen or not re.fullmatch(rb"[0-9a-f]+", rec):
            raise importer.Fatal(None, "rev-list printed a line that is not an oid")
        if sec.n >= adopt_verify.MAX_COMMITS:
            raise Refused("preview-cap:commits", f"more than {adopt_verify.MAX_COMMITS} new commits")
        if sec.n >= verify_caps.SECTION_LINES:          # counted, not read: the gate already bounded each commit
            sec.add(lambda: [], lambda: "(more commits)")
            return
        raw, _size = reader.head(oid, "commit", verify_caps.COMMIT_OBJECT_MAX)
        sec.add(lambda: _commit_lines(oid, raw), lambda: "(more commits)")

    extra = ("--not", base_oid) if base_oid is not None else ()
    _stream(p, budget, b"\n", one, "rev-list", *tips, *extra)
    return sec


def _commit_lines(oid: str, raw: bytes) -> List[str]:
    hdr, _sep, msg = raw.partition(b"\n\n")
    fields: Dict[bytes, bytes] = {}
    for line in hdr.split(b"\n"):
        if line[:1] == b" ":
            continue                                    # a continuation line (gpgsig, mergetag)
        k, _s, val = line.partition(b" ")
        fields.setdefault(k, val)
    out = [f"commit {oid}", f"  author {shown(fields.get(b'author', b''), selectors=True)}",
           f"  committer {shown(fields.get(b'committer', b''), selectors=True)}", "  message"]
    cap = verify_caps.DISPLAY_FIELD_MAX
    body = msg[:cap]
    if body.endswith(b"\n"):
        body = body[:-1]
    out += ["    " + ln for ln in escape(body, allow="\n", selectors=True).split("\n")]
    if len(msg) > cap:
        out.append(f"    [{len(msg)} bytes, sha256 {hashlib.sha256(msg).hexdigest()[:16]}]")
    return out


def _changes(p: importer.GitRunner, reader: _Reader, budget: _Budget, label: str, a: str, b: str, hexlen: int,
             pmax: Optional[int], config: _Section, links: _Section, bare: List[bool]) -> _Section:
    sec = _Section(f"changes {label}")
    meta: List[bytes] = []

    def one(rec: bytes) -> None:
        if not meta:
            if _META.fullmatch(rec) is None:
                raise importer.Fatal(None, f"diff-tree printed {rec[:80]!r} where a raw line was due")
            meta.append(rec)
            return
        m = _META.fullmatch(meta.pop())
        assert m is not None
        c = _Change(m.group(5).decode("ascii"), int(m.group(1), 8), int(m.group(2), 8), m.group(4).decode("ascii"),
                    rec)
        budget.path()
        if c.dst_mode & _S_IFMT == _S_IFDIR and c.status in "AMT":
            payload, _ = reader.head(c.dst_oid, "tree", 1 << 62)
            try:
                found = _is_bare(payload, hexlen)
            except ValueError as e:
                raise Refused("fsck:other", f"a tree Levain's reader refuses: {e}"[:300]) from None
            if found:
                bare[0] = True
                config.add(lambda: [f"{label} {c.status} {_mode_text(c)} {shown(c.path)}/  [{BARE_RULE}]"],
                           lambda: _top(c.path))
            return
        if c.dst_mode & _S_IFMT == _S_IFDIR or (c.status == "D" and c.src_mode & _S_IFMT == _S_IFDIR):
            return                                      # a tree entry of -t: read above, not listed
        sec.add(lambda: _change_lines(c, pmax), lambda: _top(c.path))
        rule = surface_rule(c.path)
        if rule is not None:
            config.add(lambda: [f"{label} {c.status} {_mode_text(c)} {shown(c.path)}  [{rule}]"],
                       lambda: _top(c.path))
        if c.dst_mode & _S_IFMT == _S_IFLNK and c.status in "AMT":
            target, _size = reader.head(c.dst_oid, "blob", 1 << 62)     # whole: shown() gives its length + sha256
            out = "  [points outside the repository]" if _outside(c.path, target) else ""
            links.add(lambda: [f"{label} {c.status} {shown(c.path)} -> {shown(target)}{out}"], lambda: _top(c.path))

    _stream(p, budget, b"\0", one, "diff-tree", "-r", "-t", "--raw", "-z", "--no-renames", "--no-ext-diff",
            "--no-textconv", a, b)
    if meta:
        raise importer.Fatal(None, "diff-tree: a raw line without its path")
    return sec


def _change_lines(c: _Change, pmax: Optional[int]) -> List[str]:
    out = [f"{c.status} {_mode_text(c)} {shown(c.path)}"]
    if pmax is not None and len(c.path) > pmax:
        out.append(f"  warning: {len(c.path)} bytes is longer than this OS's PATH_MAX ({pmax}): a checkout may fail")
    return out


def _collisions(p: importer.GitRunner, budget: _Budget, label: str, tip: str, sec: _Section) -> None:
    """Every sibling pair in ``tip``'s tree whose names agree after NFC (``NFC``) or after NFC + casefold
    (``case-fold``), in tree order [judged by adopt5dii-s2, 10-22, against §6.2: Python's full casefold, not HFS+'s
    or NTFS's own tables]. Memory: one key and one path per entry, bounded by the expanded-output cap."""
    seen: Dict[bytes, bytes] = {}

    def one(rec: bytes) -> None:
        _meta, tab, path = rec.partition(b"\t")
        if not tab:
            raise importer.Fatal(None, "ls-tree printed a line without its tab")
        budget.path()
        parent, _s, name = path.rpartition(b"/")
        key = hashlib.sha256(parent + b"\0" + _fold(name).encode("utf-8", "surrogatepass")).digest()
        prev = seen.get(key)
        if prev is None:
            seen[key] = path
            return
        pn = prev.rpartition(b"/")[2]
        nfc = unicodedata.normalize("NFC", pn.decode("utf-8", "surrogateescape")) == \
            unicodedata.normalize("NFC", name.decode("utf-8", "surrogateescape"))
        kind = "NFC" if nfc else "case-fold"
        tail = f" ({len(pn)} vs {len(name)} bytes)" if nfc else ""
        sec.add(lambda: [f"{label} {kind} {shown(prev)} <-> {shown(path)}{tail}"], lambda: _top(path))

    _stream(p, budget, b"\0", one, "ls-tree", "-r", "-t", "-z", "--full-tree", tip)


def write_preview(path: Path, p: importer.GitRunner, v: adopt_verify.Verified, *, base_oid: str, hexlen: int,
                  path_max: Optional[int] = None) -> Stats:
    """:func:`build`, then the file (``O_EXCL``, 0600, fsynced), refused ``preview-cap:output`` past
    :data:`verify_caps.PREVIEW_OUTPUT_CAP` before a byte of it is written."""
    lines, stats = build(p, v, base_oid=base_oid, hexlen=hexlen, path_max=path_max)
    data = "".join(ln + "\n" for ln in lines).encode("utf-8")
    cap = verify_caps.PREVIEW_OUTPUT_CAP
    if len(data) > cap:
        raise Refused(CAP, f"preview.txt would be {len(data)} bytes > {cap}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    stats.bytes_written = len(data)
    return stats


def report_section(report: Mapping[str, object]) -> List[str]:
    """``report.json``'s named entries (DESIGN §4.4 ``named: [{path_b64, type}]``) as the importer's report, a
    PROPOSAL, never a fact about the tree: for the CLI, which holds the report (the verifier does not). ``report``
    is the parsed, canonical JSON; a malformed entry raises ValueError."""
    import base64
    sec = _Section("the importer's report: named entries (a proposal)")
    named = report.get("named")
    if not isinstance(named, list):
        raise ValueError("named is not a list")
    for e in named:
        if not isinstance(e, dict) or not isinstance(e.get("path_b64"), str) or not isinstance(e.get("type"), str):
            raise ValueError("a named entry without path_b64 / type")
        raw = base64.b64decode(e["path_b64"], validate=True)
        kind = escape(e["type"].encode("utf-8"))
        sec.add(lambda: [f"{kind} {shown(raw)}"], lambda: _top(raw))
    out = sec.render()
    if report.get("truncated") is True:
        out.append("the importer's report says its list was truncated")
    return out


__all__ = ["BARE_HINT", "BARE_RULE", "CAP", "NO_SURFACE", "Stats", "build", "escape", "report_section", "shown",
           "surface_rule", "write_preview"]
