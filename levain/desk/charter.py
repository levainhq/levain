"""levain.desk.charter -- ``levain charter check`` (does a repository's CHARTER.md hold the prescribed shape?) and
``levain charter init`` (write the skeleton).

The charter is one human-written ``CHARTER.md`` at a repository's root: ``# Charter: <name>``, ``Charter-Version: 1``,
``Export: repo | memory | off``, then exactly four ``##`` sections in order (Purpose, Criteria, Constraints, Plan). It
carries intent only: purpose, checkable criteria, constraints, and the plan as board item ids. The validator checks
SHAPE, never vocabulary, so an ordinary English word is never refused.

:func:`check_text` is pure over the file text (no git, no network, no model). In Levain 1.0 the verb WARNS: it prints the
refusal text behind a ``levain: warning (charter not yet enforced):`` prefix and exits 0 whatever it found. Only a wrong-repo
refusal (``--repo-name`` of a registered repo, run against another repository; a worktree of it is the same repo) and an
unusable command line (argparse) exits 2.

``init`` writes a skeleton whose every blank is a ``<fill: what goes here>`` token (the Plan is prefilled from the store).
:func:`check_text` refuses exactly those tokens (an empty ``<fill:>`` too) and nothing else: ``<br>``, ``Vec<T>`` and
``a<b`` are ordinary prose and pass.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = ["DEFAULT_REF", "MAX_LINES", "Problem", "WARN_PREFIX", "check_text", "invalid_text", "missing_text", "register"]

DEFAULT_REF = "refs/remotes/origin/main"
MAX_LINES = 200
PURPOSE_MAX = 800
MAX_PRINTED = 50          # problems and warnings printed as text; --json carries them all
SECTIONS = ("Purpose", "Criteria", "Constraints", "Plan")
EXPORTS = ("repo", "memory", "off")
WARN_PREFIX = "levain: warning (charter not yet enforced): "
#: Decisions that take an item out of the plan-in-step warning.
OUT_OF_PLAN = ("cut", "parked", "out-of-1.0")

_TITLE = re.compile(r"^# Charter: (\S+)$")
_CRITERION = re.compile(r"^- (C\d+): (.+)$")
_CONSTRAINT = re.compile(r"^- (K\d+): (.+)$")
_PLAN_ID = re.compile(r"^- ([a-z0-9][a-z0-9._-]*)#(\d+)$")
_TAG = re.compile(r"\[(check: [^\]]+|item: [a-z0-9][a-z0-9._-]*#\d+|judged: [^\]]+)\]\s*$")
_TAGS_ANY = re.compile(r"\[(?:check|item|judged):")
_ITEM_TAG = re.compile(r"\[item: ([a-z0-9][a-z0-9._-]*#\d+)\]\s*$")
_PLAN_ID_ANY = re.compile(r"[a-z0-9][a-z0-9._-]*#\d+")
_ITEM_TAG_ANY = re.compile(r"\[item: [a-z0-9][a-z0-9._-]*#\d+\]")
_JUDGED = re.compile(r"\[judged: [^\]]*\]")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_GLYPH = re.compile(r"[✅☑✔]|\[[xX ]\]")
_PLACEHOLDER = re.compile(r"<fill:[^<>]*>")      # the ONE token `init` writes; any other angle-bracketed text is prose
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")


@dataclass(frozen=True)
class Problem:
    line: int
    rule: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "rule": self.rule, "text": self.text}


_STUBS_KEY = re.compile(r"^[^:\s]+:stubs:([a-z0-9][a-z0-9._-]*)$")


def _is_stubs_key(key: Any, repo: str) -> bool:
    """True for the key ``M:stubs:<repo>`` of an item of ``repo`` (design 5(e), T2-i)."""
    m = _STUBS_KEY.match(key) if isinstance(key, str) else None
    return m is not None and m.group(1) == repo


def check_text(text: str, *, repo_name: str | None = None,
               items: Mapping[str, tuple] | None = None) -> tuple[list[Problem], list[str]]:
    """``(problems, warnings)`` for the charter ``text``. ``items`` maps an item id to ``(repo, decision or None)`` when
    the store is at hand (an optional third element is the item's idempotency key); without it the store checks are skipped,
    never guessed."""
    problems: list[Problem] = []
    warnings: list[str] = []

    def bad(line: int, rule: str, msg: str) -> None:
        problems.append(Problem(line, rule, msg))

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        bad(MAX_LINES + 1, "size", f"the charter is {len(lines)} lines; the limit is {MAX_LINES}")

    title = _TITLE.match(lines[0]) if lines else None
    name = title.group(1) if title else None
    if title is None:
        bad(1, "title", "the first line must be `# Charter: <repo name>`")
    elif repo_name is not None and name != repo_name:
        bad(1, "title", f"the title names {name!r}, not this repository's name {repo_name!r}")

    headings = [(i + 1, ln[3:].strip()) for i, ln in enumerate(lines) if ln.startswith("## ")]
    first_section = headings[0][0] if headings else len(lines) + 1
    header = {}
    for i in range(1, first_section - 1):
        ln = lines[i]
        if not ln.strip():
            continue
        key, sep, value = ln.partition(": ")
        if not sep or key not in ("Charter-Version", "Export") or key in header:
            bad(i + 1, "header", f"unexpected line before the first section: {ln.strip()[:60]!r}")
            continue
        header[key] = (i + 1, value.strip())
    if "Charter-Version" not in header:
        bad(2, "version", "`Charter-Version: 1` is required")
    elif header["Charter-Version"][1] != "1":
        bad(header["Charter-Version"][0], "version",
            f"Charter-Version is {header['Charter-Version'][1]!r}; this Levain reads only version 1")
    if "Export" not in header:
        bad(3, "export", "`Export: repo | memory | off` is required and has no default")
    elif header["Export"][1] not in EXPORTS:
        bad(header["Export"][0], "export", f"Export is {header['Export'][1]!r}; it must be one of {', '.join(EXPORTS)}")

    got = tuple(h for _, h in headings)
    if got != SECTIONS:
        where = headings[0][0] if headings else 1
        bad(where, "sections", f"the `##` sections must be exactly {', '.join(SECTIONS)}, in that order; found "
                               f"{', '.join(got) if got else 'none'}")

    # Section bodies, keyed by their heading when it is one of the four (an unknown heading ends the previous body).
    body: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            h = ln[3:].strip()
            current = h if h in SECTIONS else None
            if current is not None:
                body.setdefault(current, [])
            continue
        if current is not None and i + 1 > first_section - 1:
            body[current].append((i + 1, ln))

    purpose = " ".join(ln.strip() for _, ln in body.get("Purpose", []) if ln.strip())
    if "Purpose" in body and not 1 <= len(purpose) <= PURPOSE_MAX:
        bad(headings[0][0] if headings else 1, "purpose",
            f"Purpose is {len(purpose)} characters; it must be 1 to {PURPOSE_MAX}")

    seen: dict[str, int] = {}
    criteria_items: list[tuple[int, str]] = []
    for n, ln in body.get("Criteria", []):
        if not ln.strip():
            continue
        m = _CRITERION.match(ln)
        if m is None:
            bad(n, "criteria", f"a Criteria line is `- C<n>: <statement> [check: ... | item: ... | judged: ...]`: {ln.strip()[:60]!r}")
            continue
        if m.group(1) in seen:
            bad(n, "criteria", f"{m.group(1)} repeats line {seen[m.group(1)]}")
        seen[m.group(1)] = n
        tags = _TAGS_ANY.findall(ln)
        if not _TAG.sub("", m.group(2)).strip():
            bad(n, "criteria", f"{m.group(1)} has no statement, only a tag")
        if _TAG.search(ln) is None or len(tags) != 1:
            bad(n, "criteria", f"{m.group(1)} needs exactly one [check: ...], [item: ...] or [judged: <who>] at the end "
                               "(an unenforced criterion is advice)")
        else:
            it = _ITEM_TAG.search(ln)
            if it:
                criteria_items.append((n, it.group(1)))
    if "Criteria" in body and not seen and not any(p.rule == "criteria" for p in problems):
        bad(headings[1][0] if len(headings) > 1 else 1, "criteria", "Criteria needs at least one `- C<n>:` line")

    kseen: dict[str, int] = {}
    for n, ln in body.get("Constraints", []):
        if not ln.strip():
            continue
        m = _CONSTRAINT.match(ln)
        if m is None:
            bad(n, "constraints", f"a Constraints line is `- K<n>: <constraint>`: {ln.strip()[:60]!r}")
            continue
        if m.group(1) in kseen:
            bad(n, "constraints", f"{m.group(1)} repeats line {kseen[m.group(1)]}")
        kseen[m.group(1)] = n

    plan_ids: list[tuple[int, str, str]] = []
    for n, ln in body.get("Plan", []):
        if not ln.strip() or ln.startswith("### "):
            continue
        m = _PLAN_ID.match(ln)
        if m is None:
            bad(n, "plan", f"a Plan line is a board item id and nothing else (`- <repo>#<n>`): {ln.strip()[:60]!r}")
            continue
        pid = f"{m.group(1)}#{m.group(2)}"
        if any(pid == q for _, q, _ in plan_ids):
            warnings.append(f"line {n}: {pid} is listed in the Plan more than once")
        plan_ids.append((n, pid, m.group(1)))
    if "Plan" in body and not plan_ids and not any(p.rule == "plan" for p in problems):
        bad(headings[-1][0] if headings else 1, "plan", "Plan needs at least one `- <repo>#<n>` line")

    if items is not None:
        for n, iid, prefix in plan_ids:
            if iid not in items:
                bad(n, "store", f"{iid} is not in the board store")
            elif name is not None and prefix != name:
                bad(n, "store", f"{iid} belongs to another repo; list only {name}#<n> ids in the Plan")
        for n, iid in criteria_items:
            if iid not in items:
                bad(n, "store", f"{iid} is not in the board store")
        planned = {iid for _, iid, _ in plan_ids}
        for iid, info in sorted(items.items()):
            repo, decision = info[0], info[1]
            key = info[2] if len(info) > 2 else None
            if _is_stubs_key(key, repo):        # a stubs item of another repo's ceremony (`M:stubs:<repo>`) is never in this plan
                continue
            if repo == name and iid not in planned and decision not in OUT_OF_PLAN:
                warnings.append(f"{iid} is in the store and in no plan group")

    # No status, by structure: shapes a machine can check without refusing an ordinary word. The scan does not see the
    # charter's own NAME (the title) or an item id (`NAME#N`, a Plan row, an `[item: ...]` tag): a repo named `2026-11-18` or
    # `cafe123` is date- or sha-shaped by name, and its charter must still be able to pass. Prose is scanned as ever.
    own_id = re.compile(r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9]\.)" + re.escape(name) + r"#\d+(?![A-Za-z0-9_-]|\.[A-Za-z0-9])") if name else None
    for i, ln in enumerate(lines):
        n = i + 1
        if i == 0 and title is not None:
            ln = ""
        else:
            ln = _PLAN_ID_ANY.sub("", _ITEM_TAG_ANY.sub("", ln)) if (_PLAN_ID.match(ln) or _ITEM_TAG.search(ln)) else ln
            if own_id is not None:
                ln = own_id.sub("", ln)
        if _SHA.search(ln):
            bad(n, "status", "a sha-shaped token (a mutable fact: it names a state of the code)")
        if _GLYPH.search(ln):
            bad(n, "status", "a check-mark glyph or task box (status does not live in the charter)")
        if _DATE.search(_JUDGED.sub("", ln)):
            bad(n, "status", "a date outside a [judged: ...] tag (a dated line is a status line)")
    # A skeleton's `<fill: ...>` token is a blank not yet filled; a charter holding one is not a charter.
    for i, ln in enumerate(lines):
        for m in _PLACEHOLDER.finditer(ln):
            bad(i + 1, "placeholder", f"placeholder: {m.group(0)} on line {i + 1} is not filled")
    problems.sort(key=lambda p: (p.line, p.rule))
    return problems, warnings


def missing_text(repo: str, where: str) -> str:
    return (f"{WARN_PREFIX}{repo} has no charter. Levain works on a repository only when {where} carries a CHARTER.md\n"
            "(purpose, criteria, constraints, and the plan as board item ids). Commit one and push it; "
            "`levain charter check` says what a valid one needs.")


def invalid_text(repo: str, where: str, problems: list[Problem], repo_arg: str) -> str:
    first = problems[0]
    return (f"{WARN_PREFIX}{repo}'s CHARTER.md on {where} is not a valid charter ({len(problems)} problems; "
            f"the first: line {first.line}: {first.text}).\n"
            f"Run `levain charter check --repo {repo_arg}` for every problem, fix them, commit, and push.")


#: What git says when the charter is simply not at the ref: the path is not in it, or the ref itself is not there.
_GIT_ABSENT = ("does not exist in", "exists on disk, but not in", "invalid object name", "unknown revision", "bad revision",
               "not a valid object name")
MAX_BYTES = 1 << 20       # a charter is at most 200 lines; a file past this is refused before it is decoded


def _decode(raw: bytes) -> tuple[str | None, str]:
    """UTF-8 (a BOM stripped) with CRLF and CR read as LF, the same on every path."""
    try:
        return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n"), ""
    except UnicodeDecodeError:
        return None, "unreadable (not UTF-8)"


def _read_ref(repo: Path, ref: str) -> tuple[str | None, str]:
    """``(text, why)``: the file at ``ref``, or None and why: ``absent`` (the ref has no such file) or ``unreadable (..)``.
    A ref that starts with ``-`` is refused, so it can never reach git as an option."""
    if ref.startswith("-"):
        return None, "unreadable (a ref cannot start with '-')"
    from levain.desk.board_evidence import _env
    try:
        cp = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:CHARTER.md"], capture_output=True, timeout=30, env=_env())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:   # git missing from PATH, or it hung: a warning, never a traceback
        return None, f"unreadable (git: {type(exc).__name__})"
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace")
        if any(k in err for k in _GIT_ABSENT):
            return None, "absent"
        first = (err.strip().splitlines() or ["no message"])[0][:120]
        return None, f"unreadable (git exit {cp.returncode}: {first})"
    if len(cp.stdout) > MAX_BYTES:
        return None, f"unreadable (larger than {MAX_BYTES} bytes)"
    return _decode(cp.stdout)


def _read_path(path: Path) -> tuple[str | None, str]:
    try:
        with path.expanduser().open("rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError, RuntimeError) as exc:      # RuntimeError: `~nouser` and a symlink loop
        return None, f"unreadable ({type(exc).__name__})"
    if len(raw) > MAX_BYTES:
        return None, f"unreadable (larger than {MAX_BYTES} bytes)"
    return _decode(raw)


def _store(path: Path) -> tuple[dict[str, tuple] | None, str]:
    """``(items, why)``: the store folded, or None and why the store checks are skipped. A torn store or one with
    problems is not complete, so it is skipped like an unreadable one, never trusted."""
    from levain.desk import board_store
    from levain.desk.board_state import fold
    try:
        lines = board_store.read_item_lines(board_store.resolve_store(path))
        folded = fold(lines)
    except board_store.BoardError as exc:
        return None, f"was refused: {exc}"
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        return None, f"cannot be read ({type(exc).__name__})"
    if folded.torn or folded.problems:
        return None, f"is not a whole store ({'torn: ' + folded.torn_why if folded.torn else folded.problems[0]})"
    keys = folded.keys                  # the fold returns each add's key: nothing here re-parses a raw line
    return {i.id: (i.repo, i.decision.decision if i.decision else None, keys.get(i.id)) for i in folded.items.values()}, ""


def run_check(args: Any) -> int:
    repo = Path(args.repo)
    repo_name = args.repo_name
    if repo_name is None:
        from levain.desk.board_evidence import repo_name_of
        try:
            repo_name = repo_name_of(repo.expanduser())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            repo_name = None
        repo_name = repo_name or os.path.basename(os.path.abspath(str(repo))) or "repo"
    try:
        repo = repo.expanduser()
    except RuntimeError:                                    # `~nouser`: left as typed, git will say it is not a repository
        pass
    if args.repo_name is not None and args.file is None:    # a NAME given outright: it must be read from the repo it names (--file reads only that file)
        wrong = _wrong_repo("check", args.repo_name, _toplevel(repo), args.items)
        if wrong is not None:
            print(f"levain charter check: {wrong}", file=sys.stderr)
            if args.json:       # one document on stdout on every path: the refusal is its reason
                print(json.dumps({"repo": repo_name, "source": "", "present": False, "readable": False, "why": f"refused: {wrong}",
                                  "valid": False, "store_checked": False, "store_note": "", "problems": [], "warnings": []},
                                 indent=2))
            return 2
    if args.file is not None:
        where, label = str(args.file), "the file"
        text, why = _read_path(Path(args.file))
    elif args.worktree:
        where, label = "the worktree", "the worktree"
        text, why = _read_path(repo / "CHARTER.md")
    else:
        ref = args.ref or DEFAULT_REF
        where = "origin/main" if ref == DEFAULT_REF else ref
        label = where
        text, why = _read_ref(repo, ref)
    items, store_note = None, ""
    if args.items is not None:
        items, store_note = _store(args.items)
        if items is None:
            print(f"levain charter check: the items file {args.items} {store_note}; the store checks are skipped.",
                  file=sys.stderr)
    problems: list[Problem] = []
    warnings: list[str] = []
    if text is not None:
        problems, warnings = check_text(text, repo_name=repo_name, items=items)
    if args.json:       # exactly one document on stdout, the same keys on every path
        print(json.dumps({"repo": repo_name, "source": where, "present": text is not None or why != "absent",
                          "readable": text is not None, "why": why, "valid": text is not None and not problems,
                          "store_checked": items is not None, "store_note": store_note,
                          "problems": [p.to_dict() for p in problems], "warnings": warnings}, indent=2))
        return 0
    if text is None:
        if why == "absent":
            print(missing_text(repo_name, label))
        else:
            print(f"{WARN_PREFIX}{repo_name}'s CHARTER.md ({label}) could not be read: {why}. "
                  "Fix that and run `levain charter check` again.")
        return 0
    if problems:
        print(invalid_text(repo_name, where, problems, shlex.quote(str(args.repo))))
        for p in problems[:MAX_PRINTED]:
            print(f"  line {p.line}: [{p.rule}] {p.text}")
        if len(problems) > MAX_PRINTED:
            print(f"  and {len(problems) - MAX_PRINTED} more (--json lists every one)")
    else:
        print(f"levain charter check: {repo_name}'s CHARTER.md ({where}) is a valid charter.")
    for w in warnings[:MAX_PRINTED]:
        print(f"  warning: {w}")
    if len(warnings) > MAX_PRINTED:
        print(f"  and {len(warnings) - MAX_PRINTED} more warnings (--json lists every one)")
    return 0     # warn mode: a charter's content never refuses in 1.0; only the wrong-repo guard above exits 2


_SKELETON_BODIES = {
    "Purpose": ["<fill: one paragraph, why this repo exists and for whom>"],
    "Criteria": ["- C1: <fill: a checkable statement of done-for-the-product> [judged: <fill: who>]"],
    "Constraints": ["- K1: <fill: an opinion or limit the work must hold>"],
}


def skeleton_text(name: str, export: str, plan_ids: list[str] | None) -> str:
    """The charter skeleton: every body a ``<fill: ...>`` placeholder, except a Plan prefilled with ``plan_ids`` (None or
    empty: the Plan holds one placeholder line)."""
    out = [f"# Charter: {name}", "Charter-Version: 1", f"Export: {export}", ""]
    for sec in SECTIONS:
        out.append(f"## {sec}")
        if sec == "Plan":
            out.extend([f"- {i}" for i in plan_ids] if plan_ids else ["- <fill: a board item id, NAME#n>"])
        else:
            out.extend(_SKELETON_BODIES[sec])
        out.append("")
    return "\n".join(out)


def _plan_ids(items: Mapping[str, tuple], name: str) -> list[str]:
    """NAME's items not decided out of the plan, in id order. Each value is ``(repo, decision, ...)``, as :func:`_store`
    builds it (it carries the item's key third)."""
    return sorted((i for i, (repo, dec, *_rest) in items.items() if repo == name and dec not in OUT_OF_PLAN),
                  key=lambda iid: int(iid.rpartition("#")[2]))


def _toplevel(path: Path) -> Path | None:
    from levain.desk.board_evidence import _env
    try:
        cp = subprocess.run(["git", "-C", str(path.expanduser()), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=30, env=_env())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    out = cp.stdout.removesuffix("\n")
    return Path(out) if cp.returncode == 0 and out else None


def _common_dir(path: Path) -> Path | None:
    """The resolved ``git rev-parse --git-common-dir`` of PATH (every worktree of one repository shares it), or None when
    PATH cannot be read as a git repository."""
    try:
        from levain.desk.board_evidence import _env
        cp = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, timeout=30, env=_env())
        out = cp.stdout.removesuffix("\n")
        if cp.returncode != 0 or not out:
            return None
        d = Path(out)
        return (d if d.is_absolute() else Path(path) / d).resolve()      # relative output is relative to the dir git ran in
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


def _wrong_repo(verb: str, name: str, top: Path | None, items: Path | None) -> str | None:
    """The refusal text when NAME is registered in the board registry and ``top`` (the git top-level the verb is about to act
    on) belongs to a different repository than the registered path; None otherwise. The comparison is of the REPOSITORY
    (the git common dir), so any worktree of the registered repo is allowed. Open (None, the verb behaves as it always did)
    in exactly three cases: there is no store anywhere (the lookup ``resolve_store`` does: no pointer, and the ``--items``,
    ``$LEVAIN_BOARD_ITEMS`` or home-default path it lands on holds no ``store.id``, which is ``is_store``), NAME is not
    registered, and the registered path is the same repository as ``top`` (or ``top`` is not a repository at all). Every
    other reading of the world is a refusal text: a store that IS found (by the pointer, ``--items``, the environment or the
    home) whose registry or pointer cannot be read, and a
    registered path or target that cannot be resolved or read as a git repository.

    Known edges, judged and not built: a ``board move`` landing between this read and the write (an operator running both
    at once) is not guarded; an operator racing their own command can still slip past it: a pointer created or changed
    between the guard's two reads (the pointer read and the store lookup) is not re-read, and a store directory that becomes
    unsearchable mid-command (``is_store`` then reads absent, so the guard opens) is not noticed. A name like ``a..b`` or ``a.`` is accepted, the one rule ``register`` already applies."""
    if top is None:
        return None
    from levain.desk import board_registry as br, board_store as bs
    cannot = f"cannot tell where {name} is registered: "
    try:
        pointer = bs.read_pointer()
        try:
            target = bs.resolve_store(items)
        except bs.BoardError:
            if pointer is None and not ((items is not None and str(items) != "") or os.environ.get(bs.ITEMS_ENV)):
                return None                 # no pointer, nothing explicit and no levain home: `resolve_store` found no store
            raise
        if pointer is None and not bs.is_store(target):
            return None                     # reached by --items, the environment or the home with no store.id beside it: no registry
        if not target.pointered:            # a real store reached without a pointer: `read_registry` reads only a pointered one; this read is read-only
            target = dataclasses.replace(target, pointered=True)
        reg = br.read_registry(target, allow_inactive=True).fold.repos.get(name)
    except (bs.BoardError, OSError, ValueError, RuntimeError) as exc:   # a broken pointer, no store at the pointer, two stores, a torn registry
        return f"{cannot}{exc}. Fix the store, then run this again."
    if reg is None:
        return None
    try:
        registered = Path(reg.path).expanduser().resolve()
        here = top.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:      # an unreadable parent, a symlink loop: a path that cannot be told is no match
        return (f"{cannot}{name}'s registered path {reg.path} or {top} cannot be resolved ({type(exc).__name__}: {exc}). "
                f"Fix the path, `levain board move` updates the registered one, or pass --repo to name the registered checkout.")
    if registered == here:
        return None
    reg_common = _common_dir(registered)
    if reg_common is None:
        return (f"{name} is registered at {registered}, which cannot be read as a git repository, so this cannot tell whether "
                f"{here} is the same one. If the checkout moved, `levain board move` updates the registered path; or pass "
                f"--repo {registered} to name the registered checkout.")
    if reg_common == _common_dir(here):
        return None
    return (f"{name} is registered at {registered}, but this would write CHARTER.md in {here}. "
            f"Pass --repo {registered}.") if verb == "init" else (
            f"{name} is registered at {registered}, but this would read {here}. Pass --repo {registered}.")


def run_init(args: Any) -> int:
    def refuse(msg: str, code: int = 1) -> int:
        print(f"levain charter init: {msg}", file=sys.stderr)
        return code

    name = args.name
    if not _NAME.fullmatch(name):
        return refuse(f"the name {name!r} must be lowercase letters, digits, '.', '_' or '-', starting with a letter or digit (the "
                      "name `levain board register` takes, and the prefix of the repo's item ids).")
    root: Path | None = None
    if not args.stdout or args.repo is not None:     # --stdout alone needs no repository
        where = args.repo if args.repo is not None else Path(".")
        root = _toplevel(where)
        if root is None:
            return refuse(f"{where} is not inside a git work tree.")
        wrong = None if args.stdout else _wrong_repo("init", name, root, args.items)     # --stdout writes nothing: nothing to guard
        if wrong is not None:
            return refuse(wrong, 2)
    plan_ids: list[str] | None = None
    if args.items is None:
        print("levain charter init: no --items store was read; the Plan holds one placeholder line.", file=sys.stderr)
    else:
        items, why = _store(args.items)
        if items is None:
            return refuse(f"the items file {args.items} {why}; nothing was written.")
        plan_ids = _plan_ids(items, name)
        if not plan_ids:
            print(f"levain charter init: no live items of {name} in the store", file=sys.stderr)
    text = skeleton_text(name, args.export, plan_ids)
    n_lines, n_bytes = len(text.split("\n")) - 1, len(text.encode("utf-8"))
    if n_lines > MAX_LINES or n_bytes > MAX_BYTES:
        return refuse(f"the skeleton would be {n_lines} lines and {n_bytes} bytes; the limits are {MAX_LINES} lines and "
                      f"{MAX_BYTES} bytes; nothing was written.")
    if args.stdout:
        sys.stdout.write(text)
        return 0
    assert root is not None
    target = root / "CHARTER.md"
    tmp: str | None = None
    try:
        try:
            # A random name made with O_CREAT|O_EXCL: it never follows a planted symlink, never reopens a stale file (a hard
            # link left by a killed run would otherwise be truncated through "w"), and two inits cannot share it.
            fd, tmp = tempfile.mkstemp(dir=root, prefix=".CHARTER.md.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o644)     # mkstemp makes 0600; the charter is an ordinary tracked file
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.link(tmp, target)        # fails if the target exists, even against a race: never an overwrite, never partial
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except FileExistsError:
        return refuse(f"{target} already exists; it is never overwritten.")
    except OSError as exc:
        return refuse(f"cannot write {target}: {type(exc).__name__}.")
    print(f"levain charter init: wrote {target}; replace every <fill: ...> placeholder, then run `levain charter check --worktree`.")
    return 0


def register(subparsers: Any) -> None:
    # Hidden, as ``levain board`` is (no ``help=``: ``levain --help`` and the README's "All commands" table leave it out).
    charter = subparsers.add_parser("charter", description="The repository charter: purpose, criteria, constraints, plan.")
    sub = charter.add_subparsers(dest="charter_cmd", required=True)
    p = sub.add_parser(
        "check", help="Check a repository's CHARTER.md (warn mode: exits 0; 2 on another repository's name, an unreadable registry, or a bad command line).",
        description=("Check that CHARTER.md holds the prescribed shape. Reads it from origin/main by default. In this "
                     "release it only WARNS: it prints what a valid charter needs and exits 0 either way. It exits "
                     "non-zero in these cases, all exit 2 and it reads nothing then, but only when the guard runs (NAME given "
                     "with --repo-name; not under --file, where the guard does not run and a bad --items gives 0 or 1): "
                     "--repo-name names a repo registered in "
                     "the board registry and --repo is a different repository from the registered one (a worktree of it is "
                     "fine); the registry, the store pointer or the registered checkout cannot be read, so that cannot be "
                     "told; --items names something that is neither a store directory nor a file called items.jsonl; "
                     "--items names one store while the pointer names another (two stores). An unusable command line exits 2 always."))
    p.add_argument("--repo", type=Path, default=Path("."), metavar="PATH", help="The repository to read (default: here).")
    p.add_argument("--repo-name", dest="repo_name", metavar="NAME",
                   help="The repo's name in item ids (default: the directory that holds the repository's .git).")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--ref", metavar="REF", help=f"Read CHARTER.md at this ref (default {DEFAULT_REF}).")
    src.add_argument("--worktree", action="store_true", help="Read the CHARTER.md in the working tree (a draft, before committing).")
    src.add_argument("--file", type=Path, metavar="PATH", help="Read this file as the charter.")
    p.add_argument("--items", type=Path, metavar="PATH", help="The board items store; when given, plan and criterion ids are checked against it.")
    p.add_argument("--json", action="store_true", help="Print the result as JSON.")
    p.set_defaults(func=run_check)
    from levain.desk.door import door_ok_author

    door_ok_author(p)     # the desk's door opens check and init, each where it is defined
    q = sub.add_parser(
        "init", help="Write a CHARTER.md skeleton for a repository.",
        description=("Write CHARTER.md at the repository root: the name, Export and Plan filled in, every other body a "
                     "<fill: ...> placeholder that `check` reports until it is replaced. Never overwrites. Exits 2, and writes "
                     "nothing, only when the guard runs (init writing into a repository; not under --stdout, where it does not "
                     "run and a bad --items gives 0 or 1), when NAME is registered in the board registry at a different repository than the one it would "
                     "write to (a worktree of the registered repo is fine); when the registry, the store pointer or the "
                     "registered checkout cannot be read, so that cannot be told; when --items names something that is neither a store "
                     "directory nor a file called items.jsonl; when --items names one store while the pointer names another "
                     "(two stores). An unusable command line exits 2 always."))
    q.add_argument("name", metavar="NAME", help="The repo's name in item ids (lowercase).")
    q.add_argument("--repo", type=Path, default=None, metavar="PATH", help="The repository (default: the git top-level of here).")
    q.add_argument("--export", choices=EXPORTS, default="memory", help="The Export value (default memory).")
    q.add_argument("--items", type=Path, metavar="PATH",
                   help="The board items store; the Plan is prefilled from NAME's items not cut, parked or out-of-1.0.")
    q.add_argument("--stdout", action="store_true", help="Print the skeleton instead of writing it.")
    q.set_defaults(func=run_init)
    door_ok_author(q)
