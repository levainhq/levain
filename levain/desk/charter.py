"""levain.desk.charter -- ``levain charter check``: does a repository's CHARTER.md hold the prescribed shape?

The charter is one human-written ``CHARTER.md`` at a repository's root: ``# Charter: <name>``, ``Charter-Version: 1``,
``Export: repo | memory | off``, then exactly four ``##`` sections in order (Purpose, Criteria, Constraints, Plan). It
carries intent only: purpose, checkable criteria, constraints, and the plan as board item ids. The validator checks
SHAPE, never vocabulary, so an ordinary English word is never refused.

:func:`check_text` is pure over the file text (no git, no network, no model). In Levain 1.0 the verb WARNS: it prints the
refusal text behind a ``levain: warning (charter not yet enforced):`` prefix and exits 0 whatever it found. Only an
unusable command line (argparse) exits 2.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = ["DEFAULT_REF", "MAX_LINES", "Problem", "WARN_PREFIX", "check_text", "invalid_text", "missing_text", "register"]

DEFAULT_REF = "refs/remotes/origin/main"
MAX_LINES = 200
PURPOSE_MAX = 800
SECTIONS = ("Purpose", "Criteria", "Constraints", "Plan")
EXPORTS = ("repo", "memory", "off")
WARN_PREFIX = "levain: warning (charter not yet enforced): "
#: Decisions that take an item out of the plan-in-step warning.
OUT_OF_PLAN = ("cut", "parked", "out-of-1.0")

_TITLE = re.compile(r"^# Charter: (\S+)$")
_CRITERION = re.compile(r"^- (C\d+): (.+)$")
_CONSTRAINT = re.compile(r"^- (K\d+): (.+)$")
_PLAN_ID = re.compile(r"^- ([a-z][a-z0-9_-]*)#(\d+)$")
_TAG = re.compile(r"\[(check: [^\]]+|item: [a-z][a-z0-9_-]*#\d+|judged: [^\]]+)\]\s*$")
_TAGS_ANY = re.compile(r"\[(?:check|item|judged):")
_ITEM_TAG = re.compile(r"\[item: ([a-z][a-z0-9_-]*#\d+)\]\s*$")
_JUDGED = re.compile(r"\[judged: [^\]]*\]")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_GLYPH = re.compile(r"[✅☑✔]|\[[xX ]\]")


@dataclass(frozen=True)
class Problem:
    line: int
    rule: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "rule": self.rule, "text": self.text}


def check_text(text: str, *, repo_name: str | None = None,
               items: Mapping[str, tuple[str, str | None]] | None = None) -> tuple[list[Problem], list[str]]:
    """``(problems, warnings)`` for the charter ``text``. ``items`` maps an item id to ``(repo, decision or None)`` when
    the store is at hand; without it the store checks are skipped, never guessed."""
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
        plan_ids.append((n, f"{m.group(1)}#{m.group(2)}", m.group(1)))
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
        for iid, (repo, decision) in sorted(items.items()):
            if repo == name and iid not in planned and decision not in OUT_OF_PLAN:
                warnings.append(f"{iid} is in the store and in no plan group")

    # No status, by structure: shapes a machine can check without refusing an ordinary word.
    for i, ln in enumerate(lines):
        n = i + 1
        if _SHA.search(ln):
            bad(n, "status", "a sha-shaped token (a mutable fact: it names a state of the code)")
        if _GLYPH.search(ln):
            bad(n, "status", "a check-mark glyph or task box (status does not live in the charter)")
        if _DATE.search(_JUDGED.sub("", ln)):
            bad(n, "status", "a date outside a [judged: ...] tag (a dated line is a status line)")
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


def _read_ref(repo: Path, ref: str) -> tuple[str | None, str]:
    """``(text, why)``: the file at ``ref``, or None and why it could not be read."""
    from levain.desk.board_evidence import _env
    cp = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:CHARTER.md"], capture_output=True, timeout=30, env=_env())
    if cp.returncode != 0:
        return None, "absent"
    try:
        return cp.stdout.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "not UTF-8"


def _store(path: Path) -> dict[str, tuple[str, str | None]]:
    from levain.desk.board_state import fold
    folded = fold(path.expanduser().read_text("utf-8").splitlines())
    return {i.id: (i.repo, i.decision.decision if i.decision else None) for i in folded.items.values()}


def run_check(args: Any) -> int:
    repo = Path(args.repo).expanduser()
    repo_name = args.repo_name
    if repo_name is None:
        from levain.desk.board_evidence import repo_name_of
        repo_name = repo_name_of(repo) or repo.resolve().name
    if args.file is not None:
        where, label = str(args.file), "the file"
        try:
            text, why = Path(args.file).expanduser().read_text("utf-8"), ""
        except (OSError, UnicodeDecodeError) as exc:
            text, why = None, f"unreadable ({type(exc).__name__})"
    elif args.worktree:
        where, label = "the worktree", "the worktree"
        path = repo / "CHARTER.md"
        try:
            text, why = path.read_text("utf-8"), ""
        except (OSError, UnicodeDecodeError) as exc:
            text, why = None, f"unreadable ({type(exc).__name__})"
    else:
        ref = args.ref or DEFAULT_REF
        where = "origin/main" if ref == DEFAULT_REF else ref
        label = where
        text, why = _read_ref(repo, ref)
    items = None
    if args.items is not None:
        try:
            items = _store(args.items)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"levain charter check: the items file {args.items} cannot be read ({type(exc).__name__}); "
                  "the store checks are skipped.", file=sys.stderr)
    if text is None:
        print(missing_text(repo_name, label) + (f" (the file is {why})" if why not in ("", "absent") else ""))
        if args.json:
            print(json.dumps({"repo": repo_name, "source": where, "present": False, "valid": False, "problems": [], "warnings": []}))
        return 0
    problems, warnings = check_text(text, repo_name=repo_name, items=items)
    if args.json:
        print(json.dumps({"repo": repo_name, "source": where, "present": True, "valid": not problems,
                          "problems": [p.to_dict() for p in problems], "warnings": warnings}, indent=2))
        return 0
    if problems:
        print(invalid_text(repo_name, where, problems, str(args.repo)))
        for p in problems:
            print(f"  line {p.line}: [{p.rule}] {p.text}")
    else:
        print(f"levain charter check: {repo_name}'s CHARTER.md ({where}) is a valid charter.")
    for w in warnings:
        print(f"  warning: {w}")
    return 0     # warn mode: the check never refuses in 1.0


def register(subparsers: Any) -> None:
    # Hidden, as ``levain board`` is (no ``help=``: ``levain --help`` and the README's "All commands" table leave it out).
    charter = subparsers.add_parser("charter", description="The repository charter: purpose, criteria, constraints, plan.")
    sub = charter.add_subparsers(dest="charter_cmd", required=True)
    p = sub.add_parser(
        "check", help="Check a repository's CHARTER.md (warn mode: always exits 0).",
        description=("Check that CHARTER.md holds the prescribed shape. Reads it from origin/main by default. In this "
                     "release it only WARNS: it prints what a valid charter needs and exits 0 either way."))
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
