"""§6.2 content: ``out/preview.txt``, written by the confined verifier (:mod:`levain.slot.verify_run`) after
:func:`adopt_verify.quarantine_verify` passed, from the analysis repo P it left in ``scratch/p`` (S1 session 5 DESIGN
§6.2, V30 §6.3; CONTRACT §10.3 "Display and preview").

The rule (head 1022+21, S2 L3 r1): an untrusted agent shapes its own commits, so the preview shows every surface the
take writes, and every cap fails closed by name.

What it shows, in order: the A4 header (:data:`adopt_verify.PREVIEW_HEADER`), the base and the tips, the banners
(``merge-base --is-ancestor``: base against the first tip, ``UNRELATED TO BASE`` for an UNRELATED result, ``NOT ON
BASE`` for a DELTA; head against wip, ``NOT ON HEAD``), every new commit's author, committer and message (past
:data:`verify_caps.SECTION_LINES` commits, each is still read and its author and committer counted), the ``--raw -z``
change lists (base..head, head..wip; ``diff-tree -r -t --no-renames``, its tree entries read for the embedded-bare
check and not listed), the config-surface list (CONTRACT §10.3's names on path components, case-insensitive; a path
whose non-ASCII component LOOKS LIKE one, by :mod:`levain.slot.confusables_ascii`; every changed directory and each
tip's root tree holding ``HEAD`` + ``objects/`` + ``refs/``, with one ``safe.bareRepository=explicit`` hint), every
symlink with its target, every case-fold / NFC collision pair in each tip's tree with at least one side changed from
base, and last, the full listing: every path of every change list, uncut. The config-surface, symlink and bare checks
run on the net lists AND on every new commit against its parents (``diff-tree --stdin --root -m``), so a surface a
commit adds and a later one deletes is listed with the commit that touched it (``commit <oid>``), unless the same
(status, path, mode, oid) is already listed from a net list.

Every untrusted string goes through :func:`escape`, which asks :mod:`levain.unshowable` (the ONE rule for a character
a person cannot see) which characters hide, and is shown by :func:`field`: capped at
:data:`verify_caps.DISPLAY_FIELD_MAX` bytes BEFORE escaping (then its byte length and a sha256 prefix), and wrapped in
FSI .. PDI (U+2068 / U+2069, the only characters of their kind in the file, ours to emit; the escaper turns any in the
input into ``\\u2068`` / ``\\u2069``), so a bidi run inside one field cannot reorder the line around it. A field
holding a bidi R / AL / AN code point is also flagged ``[right-to-left text]``.

Each section keeps its first :data:`verify_caps.SECTION_LINES` entries; after that, a FLAGGED entry (an outside link,
a surface hit, a bare repository, an over-long path, a right-to-left field, a collision) is still listed, and the
others are counted in ``<dir>/: N more not listed, M flagged listed above`` groupings by first component (at most as
many groups again, then one ``(other directories)`` line). Every git output byte counts against
:data:`verify_caps.EXPANDED_OUTPUT_CAP` as it is read (each ``cat-file`` response whole: the probe's line when it
arrives, then the content's header, payload and newline before it is asked for), every listed path against
:data:`verify_caps.LISTED_PATHS_CAP`, every entry of each tip-tree walk against
:data:`verify_caps.TREE_WALK_ENTRIES_CAP`, and against :data:`verify_caps.PREVIEW_OUTPUT_CAP` every entry line a
section or the full listing keeps (as it is kept, so memory stays bounded) and then the whole file (before a byte of
it is written): past any of them = ``preview-cap:output`` (its detail naming the cap), and no
``preview.txt`` is left. A commit past :data:`verify_caps.COMMIT_OBJECT_MAX` is ``preview-cap:commits`` (detail
``object-size``), the verifier's own name for it (:mod:`levain.slot.fsck_reader`), never shown cut. Every cap is read at
its check.

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
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from levain import unshowable
from levain.slot import Refused, adopt_verify, confusables_ascii, fsck_reader, importer, special_paths, verify_caps

CAP = "preview-cap:output"
_CHUNK = 1 << 20
_S_IFMT = 0o170000
_S_IFDIR = 0o040000
_S_IFLNK = 0o120000
_META = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]+) ([0-9a-f]+) ([ADMT])")
FSI = "⁨"
PDI = "⁩"
RTL = "  [right-to-left text]"
BARE_HINT = ("hint: an embedded bare repository is listed. A git you run inside it runs its config (core.fsmonitor, "
             "hooks); safe.bareRepository=explicit in your git config stops that.")
NO_SURFACE = "no config surface changed"
#: macOS's PATH_MAX, the operator's side of the checkout (the verifier's Linux host says 4096): the warning fires at
#: the lower of this and the host's (complement LOW 4).
PATH_MAX_FLOOR = 1024
#: NAME_MAX on macOS and Linux: one path component past it fails at checkout.
NAME_MAX = 255

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
_RTL_CLASSES = frozenset({"R", "AL", "AN"})


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


def field(raw: bytes, *, selectors: bool = False) -> str:
    """One untrusted field as the file shows it: :func:`shown`, isolated in FSI .. PDI."""
    return FSI + shown(raw, selectors=selectors) + PDI


def rtl(raw: bytes) -> bool:
    """Whether ``raw`` holds a code point of bidi class R, AL or AN (it can reorder what a viewer draws)."""
    text = raw.decode("utf-8", "surrogateescape")
    return not text.isascii() and any(unicodedata.bidirectional(ch) in _RTL_CLASSES for ch in text)


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


def surface_lookalike(path: bytes) -> Optional[str]:
    """The rule a NON-ASCII path matches once each component is NFKC + casefolded and every look-alike code point
    (:data:`confusables_ascii.LOOKALIKE`) replaced by its ASCII prototype (``.cl\\u0430ude`` -> ``.claude/``), when
    the path itself matches none; else None."""
    text = path.decode("utf-8", "surrogateescape")
    if text.isascii() or surface_rule(path) is not None:
        return None
    look = confusables_ascii.LOOKALIKE
    skel = "/".join("".join(look.get(ch, ch) for ch in unicodedata.normalize("NFKC", comp).casefold())
                    for comp in text.split("/"))
    return surface_rule(skel.encode("utf-8", "surrogateescape"))


class _Budget:
    """The git bytes read, the paths listed and the lines kept, each against its V30 cap (read at each check)."""

    def __init__(self) -> None:
        self.git_bytes = 0
        self.paths = 0
        self.out_bytes = 0

    def take(self, n: int) -> None:
        self.git_bytes += n
        cap = verify_caps.EXPANDED_OUTPUT_CAP
        if self.git_bytes > cap:
            raise Refused(CAP, f"the preview's git output passes EXPANDED_OUTPUT_CAP ({cap} bytes)")

    def path(self) -> None:
        self.paths += 1
        cap = verify_caps.LISTED_PATHS_CAP
        if self.paths > cap:
            raise Refused(CAP, f"the preview lists more than LISTED_PATHS_CAP ({cap}) paths")

    def out(self, line: str) -> None:
        self.out_bytes += len(line.encode("utf-8")) + 1
        cap = verify_caps.PREVIEW_OUTPUT_CAP
        if self.out_bytes > cap:
            raise Refused(CAP, f"preview.txt passes PREVIEW_OUTPUT_CAP ({cap} bytes)")


class _Section:
    """One section: its first :data:`verify_caps.SECTION_LINES` entries kept, then every flagged entry kept and the
    rest counted by group. ``budget`` (when given) holds every kept line to the file's cap as it is kept."""

    def __init__(self, title: str, budget: Optional[_Budget] = None, empty: str = "none", flags: bool = True) -> None:
        self.title = title
        self.budget = budget
        self.empty = empty
        self.flags = flags
        self.n = 0
        self.lines: List[str] = []
        self.groups: Dict[str, List[int]] = {}      # group -> [not listed, flagged and listed past the first entries]
        self.other = [0, 0]
        self.tail: List[str] = []                   # rendered after the overflow lines (the commits' summary)

    def _keep(self, lines: List[str]) -> None:
        for ln in lines:
            if self.budget is not None:
                self.budget.out(ln)
            self.lines.append(ln)

    def add(self, lines: Callable[[], List[str]], group: Callable[[], str], flagged: bool = False) -> None:
        self.n += 1
        keep = verify_caps.SECTION_LINES
        if self.n <= keep:
            self._keep(lines())
            return
        if flagged:
            self._keep(lines())
        g = group()
        slot = self.groups.get(g)
        if slot is None:
            if len(self.groups) < keep:
                slot = self.groups[g] = [0, 0]
            else:
                slot = self.other
        slot[1 if flagged else 0] += 1

    def _more(self, g: str, slot: List[int]) -> str:
        if not self.flags:
            return f"{g}: {slot[0]} more not listed"
        return f"{g}: {slot[0]} more not listed, {slot[1]} flagged listed above"

    def render(self) -> List[str]:
        out = [f"== {self.title} ({self.n}) =="]
        if not self.n:
            out.append(self.empty)
        out += self.lines
        out += [self._more(g, s) for g, s in self.groups.items() if s[0]]
        if self.other[0]:
            out.append(self._more("(other directories)", self.other))
        return out + self.tail


def _top(path: bytes) -> str:
    head, sep, _rest = path.partition(b"/")
    return f"{field(head)}/" if sep else "./"


def _stream(p: importer.GitRunner, budget: _Budget, sep: bytes, on_record: Callable[[bytes], None],
            *args: str, stdin: Optional[bytes] = None) -> None:
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
        rc, _out, err = adopt_verify._g(p, *args, stdout_fd=wfd, stdin=stdin)
    finally:
        os.close(wfd)
        t.join()
    if failed:
        raise failed[0]
    if rc != 0:
        last = (err.strip().splitlines() or [""])[-1]
        raise importer.Fatal(None, f"git {args[0]} rc {rc}: {last[:300]}")


class _Reader:
    """P's objects through one ``cat-file --batch-command`` (:class:`fsck_reader._Store`). Every byte of each response
    counts against the budget: the probe's line as it arrives, then the content's ``<oid> <type> <size>\\n``, payload
    and trailing newline BEFORE the content is asked for (codex MED: one header was counted, not both)."""

    def __init__(self, p: importer.GitRunner, budget: _Budget, hexlen: int) -> None:
        self.store = fsck_reader._Store(p, "P", hexlen)
        self.budget = budget

    def read(self, oid: str, kind: str) -> bytes:
        got = self.store.info(oid)
        self.budget.take(len(f"{oid} missing\n" if got is None else f"{oid} {got[0]} {got[1]}\n"))
        if got is None or got[0] != kind:
            raise importer.Fatal(None, f"{kind} {oid} in P: probe {got!r}")
        size = got[1]
        cap = verify_caps.COMMIT_OBJECT_MAX
        if kind == "commit" and size > cap:
            raise Refused("preview-cap:commits", f"object-size: commit {oid} is {size} bytes > {cap}")
        self.budget.take(len(f"{oid} {kind} {size}\n") + size + 1)
        return self.store.head(oid, kind, size, size)

    def close(self) -> None:
        self.store.close()


@dataclass(frozen=True)
class _Change:
    status: str
    src_mode: int
    dst_mode: int
    dst_oid: str
    path: bytes

    def key(self) -> Tuple[str, bytes, int, str]:
        return self.status, self.path, self.dst_mode, self.dst_oid


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
    try:
        for mode_s, name, _oid in special_paths.tree_entries_raw(payload, hexlen):
            is_dir = int(mode_s, 8) & _S_IFMT == _S_IFDIR
            n = _fold(name)
            if n == "head" and not is_dir:
                head = True
            elif n == "objects" and is_dir:
                objects = True
            elif n == "refs" and is_dir:
                refs = True
    except ValueError as e:
        raise Refused("fsck:other", f"a tree Levain's reader refuses: {e}"[:300]) from None
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


@dataclass
class _Ctx:
    """What one preview's passes share."""
    p: importer.GitRunner
    reader: _Reader
    budget: _Budget
    hexlen: int
    pmax: int
    config: _Section
    links: _Section
    full: List[str] = _dc_field(default_factory=list)
    bare: bool = False
    listed: Set[Tuple[str, bytes, int, str]] = _dc_field(default_factory=set)
    bare_oids: Dict[str, bool] = _dc_field(default_factory=dict)

    def keep_full(self, line: str) -> None:
        self.budget.out(line)
        self.full.append(line)

    def is_bare(self, oid: str) -> bool:
        got = self.bare_oids.get(oid)
        if got is None:
            got = self.bare_oids[oid] = _is_bare(self.reader.read(oid, "tree"), self.hexlen)
        return got


def build(p: importer.GitRunner, v: adopt_verify.Verified, *, base_oid: str, hexlen: int,
          path_max: Optional[int] = None) -> Tuple[List[str], Stats]:
    """The preview's lines (each without its newline) from P, or :class:`Refused` (``preview-cap:output``,
    ``preview-cap:commits``, ``fsck:other``) / :class:`importer.Fatal`. ``path_max``: the PATH_MAX the warning uses
    (default: the lower of :data:`PATH_MAX_FLOOR` and this host's)."""
    budget = _Budget()
    if path_max is None:
        host = _path_max()
        path_max = PATH_MAX_FLOOR if host is None else min(PATH_MAX_FLOOR, host)
    head, wip = v.refs.get("head"), v.refs.get("wip")
    lines = [adopt_verify.PREVIEW_HEADER, f"base {base_oid}"]
    lines += [f"{k} {o}" for k, o in (("head", head), ("wip", wip)) if o is not None]
    lines.append(f"result {v.result}")
    first = head if head is not None else wip
    assert first is not None
    if not _ancestor(p, base_oid, first):
        what = "UNRELATED TO BASE" if v.result == "unrelated" else "NOT ON BASE"
        lines.append(f"!!! {what}: {'head' if head else 'wip'} {first} does not descend from base {base_oid}; "
                     f"every new commit is listed below !!!")
    if head is not None and wip is not None and not _ancestor(p, head, wip):
        lines.append(f"!!! NOT ON HEAD: wip {wip} does not descend from head {head}; every new commit is listed "
                     f"below !!!")
    tips = [o for o in (head, wip) if o is not None]
    reader = _Reader(p, budget, hexlen)
    try:
        ctx = _Ctx(p, reader, budget, hexlen, path_max, _Section("config surface", budget, NO_SURFACE),
                   _Section("symlinks", budget))
        commits, oids = _commits(p, reader, budget, tips, base_oid if v.result == "delta" else None)
        lists: List[Tuple[str, str, str]] = []
        if head is not None:
            lists.append(("base..head", base_oid, head))
            if wip is not None:
                lists.append(("head..wip", head, wip))
        elif wip is not None:
            lists.append(("base..wip", base_oid, wip))
        changed: Dict[str, Set[bytes]] = {}
        changes = []
        for label, a, b in lists:
            sec, paths = _changes(ctx, label, a, b)
            changes.append(sec)
            changed[label] = paths
        for label, tip in (("head", head), ("wip", wip)):
            if tip is not None:
                _root_bare(ctx, label, tip)
        _per_commit(ctx, oids)
        collisions = _Section("collisions", budget)
        if head is not None:
            _collisions(p, budget, "head", head, changed["base..head"], collisions)
            if wip is not None:
                _collisions(p, budget, "wip", wip, changed["base..head"] | changed["head..wip"], collisions)
        elif wip is not None:
            _collisions(p, budget, "wip", wip, changed["base..wip"], collisions)
    finally:
        reader.close()
    lines += commits.render()
    for c in changes:
        lines += c.render()
    lines += ctx.config.render()
    if ctx.bare:
        lines.append(BARE_HINT)
    lines += ctx.links.render()
    lines += collisions.render()
    lines.append(f"== every path the take writes ({len(ctx.full)}) ==")
    lines += ctx.full or ["none"]
    return lines, Stats(budget.git_bytes, budget.paths, len(oids))


def _ancestor(p: importer.GitRunner, a: str, b: str) -> bool:
    rc, _o, err = adopt_verify._g(p, "merge-base", "--is-ancestor", a, b)
    if rc not in (0, 1):
        raise importer.Fatal(None, f"git merge-base rc {rc}: {(err.strip().splitlines() or [''])[-1][:300]}")
    return rc == 0


_IDENT = re.compile(rb"(.*) [0-9]+ [+-][0-9]{4}", re.DOTALL)


def _commit_fields(raw: bytes) -> Tuple[Dict[bytes, bytes], bytes]:
    hdr, _sep, msg = raw.partition(b"\n\n")
    fields: Dict[bytes, bytes] = {}
    for line in hdr.split(b"\n"):
        if line[:1] == b" ":
            continue                                    # a continuation line (gpgsig, mergetag)
        k, _s, val = line.partition(b" ")
        fields.setdefault(k, val)
    return fields, msg


def _commits(p: importer.GitRunner, reader: _Reader, budget: _Budget, tips: Sequence[str],
             base_oid: Optional[str]) -> Tuple[_Section, List[str]]:
    """The commits section and every new commit's oid. Past :data:`verify_caps.SECTION_LINES`, each commit is still
    read (whole, size-checked) and its author and committer identities counted (at most as many identities again
    per role, then one ``(other identities)`` count)."""
    sec = _Section("commits", budget, flags=False)
    hexlen = reader.store.hexlen
    oids: List[str] = []
    who: Dict[bytes, Dict[bytes, int]] = {b"author": {}, b"committer": {}}
    other = {b"author": 0, b"committer": 0}

    def one(rec: bytes) -> None:
        oid = rec.decode("ascii", "replace")
        if len(rec) != hexlen or not re.fullmatch(rb"[0-9a-f]+", rec):
            raise importer.Fatal(None, "rev-list printed a line that is not an oid")
        if len(oids) >= adopt_verify.MAX_COMMITS:
            raise Refused("preview-cap:commits", f"more than {adopt_verify.MAX_COMMITS} new commits")
        oids.append(oid)
        raw = reader.read(oid, "commit")
        if sec.n < verify_caps.SECTION_LINES:
            sec.add(lambda: _commit_lines(oid, raw), lambda: "(more commits)")
            return
        sec.add(lambda: [], lambda: "(more commits)")
        fields, _msg = _commit_fields(raw)
        for role in (b"author", b"committer"):
            val = fields.get(role, b"")
            m = _IDENT.fullmatch(val)
            ident = m.group(1) if m else val
            counts = who[role]
            if ident in counts:
                counts[ident] += 1
            elif len(counts) < verify_caps.SECTION_LINES:
                counts[ident] = 1
            else:
                other[role] += 1

    extra = ("--not", base_oid) if base_oid is not None else ()
    _stream(p, budget, b"\n", one, "rev-list", *tips, *extra)
    for role in (b"author", b"committer"):
        for ident, n in who[role].items():
            ln = f"  {role.decode()} {field(ident, selectors=True)}: {n}{RTL if rtl(ident) else ''}"
            budget.out(ln)
            sec.tail.append(ln)
        if other[role]:
            sec.tail.append(f"  (other identities): {other[role]}")
    return sec, oids


def _commit_lines(oid: str, raw: bytes) -> List[str]:
    fields, msg = _commit_fields(raw)
    out = [f"commit {oid}"]
    for role in (b"author", b"committer"):
        val = fields.get(role, b"")
        out.append(f"  {role.decode()} {field(val, selectors=True)}{RTL if rtl(val) else ''}")
    out.append("  message")
    cap = verify_caps.DISPLAY_FIELD_MAX
    body = msg[:cap]
    if body.endswith(b"\n"):
        body = body[:-1]
    for raw_ln, ln in zip(body.split(b"\n"), escape(body, allow="\n", selectors=True).split("\n")):
        out.append(f"    {FSI}{ln}{PDI}{RTL if rtl(raw_ln) else ''}")
    if len(msg) > cap:
        out.append(f"    [{len(msg)} bytes, sha256 {hashlib.sha256(msg).hexdigest()[:16]}]")
    return out


def _raw_records(on_change: Callable[[_Change], None], on_header: Optional[Callable[[str], None]],
                 hexlen: int) -> Callable[[bytes], None]:
    """A ``diff-tree --raw -z`` record splitter: a meta record, then its path; with ``on_header`` (``--stdin``), a
    bare oid record starts the next commit."""
    meta: List[bytes] = []

    def one(rec: bytes) -> None:
        if meta:
            m = _META.fullmatch(meta.pop())
            assert m is not None
            on_change(_Change(m.group(5).decode("ascii"), int(m.group(1), 8), int(m.group(2), 8),
                              m.group(4).decode("ascii"), rec))
            return
        if _META.fullmatch(rec) is not None:
            meta.append(rec)
            return
        if on_header is not None and len(rec) == hexlen and re.fullmatch(rb"[0-9a-f]+", rec):
            on_header(rec.decode("ascii"))
            return
        raise importer.Fatal(None, f"diff-tree printed {rec[:80]!r} where a raw line was due")

    one.pending = meta                                  # type: ignore[attr-defined]
    return one


_DIFF = ("diff-tree", "-r", "-t", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv")


def _surfaces(ctx: _Ctx, label: str, c: _Change, net: bool) -> None:
    """The config-surface, bare and symlink checks of one change (a net list's, or a commit's: a commit's entry
    already listed from a net list is not listed again)."""
    is_dir = c.dst_mode & _S_IFMT == _S_IFDIR
    if not net and c.key() in ctx.listed:
        return
    mark = RTL if rtl(c.path) else ""
    if is_dir:
        if c.status in "AMT" and ctx.is_bare(c.dst_oid):
            ctx.bare = True
            if net:
                ctx.listed.add(c.key())
            ctx.config.add(lambda: [f"{label} {c.status} {_mode_text(c)} {field(c.path)}/  [{BARE_RULE}]{mark}"],
                           lambda: _top(c.path), flagged=True)
        return
    if c.status == "D" and c.src_mode & _S_IFMT == _S_IFDIR:
        return
    rule = surface_rule(c.path)
    tag = rule if rule is not None else surface_lookalike(c.path)
    if tag is not None:
        if net:
            ctx.listed.add(c.key())
        text = f"[{tag}]" if rule is not None else f"[looks like {tag}]"
        ctx.config.add(lambda: [f"{label} {c.status} {_mode_text(c)} {field(c.path)}  {text}{mark}"],
                       lambda: _top(c.path), flagged=True)
    if c.dst_mode & _S_IFMT == _S_IFLNK and c.status in "AMT":
        if net:
            ctx.listed.add(c.key())
        target = ctx.reader.read(c.dst_oid, "blob")     # whole: field() gives its length + sha256
        out = "  [points outside the repository]" if _outside(c.path, target) else ""
        mark = RTL if (rtl(c.path) or rtl(target)) else ""
        ctx.links.add(lambda: [f"{label} {c.status} {field(c.path)} -> {field(target)}{out}{mark}"],
                      lambda: _top(c.path), flagged=bool(out or mark))


def _changes(ctx: _Ctx, label: str, a: str, b: str) -> Tuple[_Section, Set[bytes]]:
    """One net change list: its section, the full listing's lines, the surface checks, and the paths (tree entries
    too) it adds or changes, for the collision check."""
    sec = _Section(f"changes {label}", ctx.budget)
    paths: Set[bytes] = set()

    def change(c: _Change) -> None:
        ctx.budget.path()
        if c.status != "D":
            paths.add(c.path)
        _surfaces(ctx, label, c, True)
        if c.dst_mode & _S_IFMT == _S_IFDIR or (c.status == "D" and c.src_mode & _S_IFMT == _S_IFDIR):
            return                                      # a tree entry of -t: read above, not listed
        lines = _change_lines(c, ctx.pmax)
        sec.add(lambda: lines, lambda: _top(c.path), flagged=len(lines) > 1 or rtl(c.path))
        ctx.keep_full(f"{label} {c.status} {_mode_text(c)} {field(c.path)}{RTL if rtl(c.path) else ''}")

    one = _raw_records(change, None, ctx.hexlen)
    _stream(ctx.p, ctx.budget, b"\0", one, *_DIFF, a, b)
    if one.pending:                                     # type: ignore[attr-defined]
        raise importer.Fatal(None, "diff-tree: a raw line without its path")
    return sec, paths


def _per_commit(ctx: _Ctx, oids: Sequence[str]) -> None:
    """Every new commit against each of its parents (a root commit against nothing), its surface, bare and symlink
    hits listed under ``commit <oid>`` (complement MED 1: a surface added in one commit and deleted in the next is in
    no net list). Bounded by :data:`adopt_verify.MAX_COMMITS` and :data:`verify_caps.LISTED_PATHS_CAP`."""
    if not oids:
        return
    cur: List[str] = []

    def header(oid: str) -> None:
        cur[:] = [oid]

    def change(c: _Change) -> None:
        if not cur:
            raise importer.Fatal(None, "diff-tree --stdin printed a change before its commit")
        ctx.budget.path()
        _surfaces(ctx, f"commit {cur[0]}", c, False)

    one = _raw_records(change, header, ctx.hexlen)
    _stream(ctx.p, ctx.budget, b"\0", one, *_DIFF, "--stdin", "--root", "-m",
            stdin="".join(o + "\n" for o in oids).encode("ascii"))
    if one.pending:                                     # type: ignore[attr-defined]
        raise importer.Fatal(None, "diff-tree: a raw line without its path")


def _root_bare(ctx: _Ctx, label: str, tip: str) -> None:
    """A tip's ROOT tree holding the embedded-bare pattern (``diff-tree -t`` never lists the root; complement LOW 5)."""
    raw = ctx.reader.read(tip, "commit")
    first = raw.split(b"\n", 1)[0]
    if not first.startswith(b"tree ") or not re.fullmatch(rb"[0-9a-f]{%d}" % ctx.hexlen, first[5:]):
        raise Refused("fsck:other", f"commit {tip}: no tree line first")    # index-pack's fsck refuses it first
    if ctx.is_bare(first[5:].decode("ascii")):
        ctx.bare = True
        ctx.config.add(lambda: [f"{label} root tree  [{BARE_RULE}]"], lambda: "./", flagged=True)


def _change_lines(c: _Change, pmax: int) -> List[str]:
    out = [f"{c.status} {_mode_text(c)} {field(c.path)}{RTL if rtl(c.path) else ''}"]
    if len(c.path) >= pmax:
        out.append(f"  warning: {len(c.path)} bytes reaches PATH_MAX ({pmax}) before your checkout's own path: a "
                   f"checkout may fail")
    longest = max(len(part) for part in c.path.split(b"/"))
    if longest > NAME_MAX:
        out.append(f"  warning: a {longest}-byte component is past NAME_MAX ({NAME_MAX}): a checkout may fail")
    return out


def _collisions(p: importer.GitRunner, budget: _Budget, label: str, tip: str, changed: Set[bytes],
                sec: _Section) -> None:
    """Every sibling pair in ``tip``'s tree whose names agree after NFC (``NFC``) or after NFC + casefold
    (``case-fold``), in tree order, at least one side in ``changed`` (a pair the base already held is not new)
    [judged by adopt5dii-s2, 10-22, against §6.2: Python's full casefold, not HFS+'s or NTFS's own tables]. Every
    pair is listed (each a flagged line, held to the file's cap as it is kept), so a fan of k alike names costs at
    most the cap. The walk's entries count against :data:`verify_caps.TREE_WALK_ENTRIES_CAP`, not the listed paths.
    Memory: one key and one path per entry, bounded by the expanded-output cap."""
    seen: Dict[bytes, Tuple[List[bytes], List[bytes]]] = {}       # key -> (every path, the changed ones)
    walked = [0]

    def one(rec: bytes) -> None:
        _meta, tab, path = rec.partition(b"\t")
        if not tab:
            raise importer.Fatal(None, "ls-tree printed a line without its tab")
        walked[0] += 1
        cap = verify_caps.TREE_WALK_ENTRIES_CAP
        if walked[0] > cap:
            raise Refused(CAP, f"the {label} tree walk passes TREE_WALK_ENTRIES_CAP ({cap} entries)")
        parent, _s, name = path.rpartition(b"/")
        key = hashlib.sha256(parent + b"\0" + _fold(name).encode("utf-8", "surrogatepass")).digest()
        every, new = seen.setdefault(key, ([], []))
        is_new = path in changed
        for prev in (every if is_new else new):
            _pair(label, prev, path, sec)
        every.append(path)
        if is_new:
            new.append(path)

    _stream(p, budget, b"\0", one, "ls-tree", "-r", "-t", "-z", "--full-tree", tip)


def _pair(label: str, prev: bytes, path: bytes, sec: _Section) -> None:
    pn, name = prev.rpartition(b"/")[2], path.rpartition(b"/")[2]
    nfc = unicodedata.normalize("NFC", pn.decode("utf-8", "surrogateescape")) == \
        unicodedata.normalize("NFC", name.decode("utf-8", "surrogateescape"))
    kind = "NFC" if nfc else "case-fold"
    tail = f" ({len(pn)} vs {len(name)} bytes)" if nfc else ""
    mark = RTL if (rtl(prev) or rtl(path)) else ""
    sec.add(lambda: [f"{label} {kind} {field(prev)} <-> {field(path)}{tail}{mark}"], lambda: _top(path), flagged=True)


def write_preview(path: Path, p: importer.GitRunner, v: adopt_verify.Verified, *, base_oid: str, hexlen: int,
                  path_max: Optional[int] = None) -> Stats:
    """:func:`build`, then the file (``O_EXCL``, 0600, fsynced), refused ``preview-cap:output`` past
    :data:`verify_caps.PREVIEW_OUTPUT_CAP` before a byte of it is written."""
    lines, stats = build(p, v, base_oid=base_oid, hexlen=hexlen, path_max=path_max)
    data = "".join(ln + "\n" for ln in lines).encode("utf-8")
    cap = verify_caps.PREVIEW_OUTPUT_CAP
    if len(data) > cap:
        raise Refused(CAP, f"preview.txt would be {len(data)} bytes > PREVIEW_OUTPUT_CAP ({cap})")
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
        kind = field(e["type"].encode("utf-8"))
        sec.add(lambda: [f"{kind} {field(raw)}{RTL if rtl(raw) else ''}"], lambda: _top(raw), flagged=rtl(raw))
    out = sec.render()
    if report.get("truncated") is True:
        out.append("the importer's report says its list was truncated")
    return out


__all__ = ["BARE_HINT", "BARE_RULE", "CAP", "FSI", "NAME_MAX", "NO_SURFACE", "PATH_MAX_FLOOR", "PDI", "RTL", "Stats",
           "build", "escape", "field", "report_section", "rtl", "shown", "surface_lookalike", "surface_rule",
           "write_preview"]
