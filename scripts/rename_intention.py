#!/usr/bin/env python3
"""Rename levain's "spore" concept to "intention", everywhere except stored and wire bytes.

    python3 scripts/rename_intention.py [--apply] [--root DIR] [--report FILE]

Dry-run by default: it computes every change in memory and prints the report. ``--apply`` writes (git trees only, on a
clean worktree). It is idempotent: a renamed tree has nothing left to do, and ``--apply`` on it changes nothing and
exits 0. Exit codes: 0 done or nothing to do, 2 refused (the report says why; nothing was written), 3 ``--apply`` on a
dirty worktree, 4 ``--apply`` outside a git tree.

The rules (modelled on rename_desk.py; the spec is the rename map's section 5):

* R1 paths: every T1 file whose path holds the word moves to the renamed path (``git mv``); a name collision refuses the
  whole run. The new paths are derived from the same word rewrite as the text, never listed.
* R2 words: ``spore``/``Spore``/``SPORE`` (and plurals, identifiers, ``spore-NNNN`` ids) become ``intention`` /
  ``Intention`` / ``INTENTION``, with ``a spore`` -> ``an intention``. Only tokens containing ``spore`` are touched; a
  token that also contains ``intent`` is refused, because levain's ``intent`` concept (root intent, provenance) is a
  different thing.
* R3 never touched: the data table ``NEVER_TOUCHED`` (each entry carries its reason), binary and log suffixes, files
  carrying the ``MARKER`` line, and every CHANGELOG section but ``[Unreleased]``.
* R4 stored and wire tokens: the data table ``STORED_TOKEN_RULES`` classifies a stored-looking string (a ``"spore_..."``
  or ``"spore:"`` literal, a bare ``"spores"`` key, a ``.spores.json`` file name, an anneal module import). A rule says
  keep (optionally as a constant of ``levain/intention_legacy.py``, which this codemod writes), rewrite, or refuse. A
  stored-looking string no rule covers is listed as NEEDS-RULE and makes the run refuse.
* R5 the anneal seam: ``from anneal_memory.spores import SporeStore`` keeps its module path but imports the new names,
  and a store construction passes ``id_prefix="intention"``.
* R8 refusals list and never guess: a rewritten identifier that already exists in the tree, a token holding both
  concepts, a file that does not parse before or after, a path outside T1, an unmapped anneal name, an overlapping edit.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------------------------------------------

# `spore` as the start of an identifier part: not followed by a lowercase letter (a longer word), except the plural `s`.
_CORE = r"(?i:spore)(?![a-rt-z])(?!s[a-z])"
# An article directly before the word is carried with it so `a spore` reads `an intention`.
SUB_RE = re.compile(r"(?:(?<![A-Za-z0-9_])([Aa])([ \t]+))?(" + _CORE + ")")
ANY_WORD_RE = re.compile(r"spore", re.IGNORECASE)
INTENT_RE = re.compile(r"intent", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
BARE_TOKENS = {"spore", "spores"}


def _new_word(core: str) -> str | None:
    """The renamed form of a matched ``spore`` keeping its case, or None for a case with no mapping (``sPore``)."""
    if core.isupper():
        return "INTENTION"
    if core[0].isupper() and core[1:].islower():
        return "Intention"
    if core.islower():
        return "intention"
    return None


# --------------------------------------------------------------------------------------------------------------
# data: what is never touched
# --------------------------------------------------------------------------------------------------------------

LEGACY_PATH = "levain/intention_legacy.py"
LEGACY_MODULE = "levain.intention_legacy"
MARKER = "rename-intention: never-touch"      # a file carrying this line in its first 2000 characters is history

# (fnmatch glob over the repo-relative path, where `*` also crosses `/`; the reason). Data, not logic: add an entry
# with its reason, never a special case in the code.
NEVER_TOUCHED: list[tuple[str, str]] = [
    ("project_memory/*", "project memory: dated records of past work, cited by id"),
    ("docs/*/history/*", "docs history: past records"),
    ("docs/history/*", "docs history: past records"),
    ("tests/fixtures/*", "frozen fixtures: stored bytes a test replays"),
    ("tests/golden/*", "frozen goldens: stored bytes a test compares"),
    ("*.jsonl", "append-only logs: never rewritten"),
    ("*.db", "a database: stored bytes"),
    ("*.sqlite", "a database: stored bytes"),
    ("*.sqlite3", "a database: stored bytes"),
    ("*.lock", "a lock file"),
    ("scripts/rename_intention.py", "the codemod names the old word on purpose"),
    ("tests/test_rename_intention.py", "the codemod's test names the old word on purpose"),
    (LEGACY_PATH, "the one module that spells the old word as code"),
]
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".whl",
                   ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pyc", ".so", ".dylib", ".dll", ".exe", ".bin", ".mp3",
                   ".mp4", ".mov", ".wav", ".icns", ".class", ".jar"}
# Files processed only inside one section: rel -> the heading that opens it.
SECTION_ONLY = {"CHANGELOG.md": "## [Unreleased]"}

# --------------------------------------------------------------------------------------------------------------
# data: stored and wire tokens
# --------------------------------------------------------------------------------------------------------------

ACTIONS = ("keep-as-legacy-constant", "rewrite", "refuse")


@dataclass
class Rule:
    """One verdict on a stored or wire spelling.

    ``pattern`` is a regex searched in the candidate's text (a string literal with its quotes, a file name, an import
    statement); a rule with ``always`` is instead applied to every match in the files its ``paths`` name, whether or not a
    detector found a candidate there (the way to rename a token that holds no ``spore``, such as a stored disposition).
    ``paths`` are fnmatch globs over the OLD repo-relative path; empty means every T1 file.

    keep-as-legacy-constant  the matched text keeps its bytes. With ``const``, a plain string literal the pattern matches
                             whole is replaced by that name, imported from ``levain/intention_legacy.py``.
    rewrite                  the word rewrite applies, or ``replacement`` (a regex expansion) replaces the matched text.
    refuse                   the run refuses and lists the site, so a person decides.
    """
    pattern: str
    action: str
    reason: str
    paths: tuple[str, ...] = ()
    replacement: str | None = None
    const: str | None = None
    always: bool = False
    rx: re.Pattern[str] = field(init=False, repr=False)
    # What a rewrite leaves behind. A replacement that still holds the word (an id pattern reading both prefixes) would
    # be rewritten again on the next run, so the text it produces is protected: that is what keeps a second run a no-op.
    settled: re.Pattern[str] | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        if self.replacement is not None and self.action != "rewrite":
            raise ValueError("replacement belongs to a rewrite rule")
        if self.const is not None and self.action != "keep-as-legacy-constant":
            raise ValueError("const belongs to a keep-as-legacy-constant rule")
        if self.always and not self.paths:
            raise ValueError("an always rule must name its paths: it rewrites without a detector")
        self.rx = re.compile(self.pattern)
        if self.replacement is not None and ANY_WORD_RE.search(self.replacement):
            if re.search(r"\\(?:\d|g<)", self.replacement):
                raise ValueError("a replacement that keeps the word cannot use a group reference: its output must be a fixed text")
            self.settled = re.compile(re.escape(self.replacement))

    def applies_to(self, rel: str) -> bool:
        return not self.paths or any(fnmatch.fnmatchcase(rel, g) for g in self.paths)


# The inventory fills this table. First match wins. An entry's reason says what stored or wire byte it protects.
STORED_TOKEN_RULES: list[Rule] = [
    Rule(re.escape('INTENTION_ID = re.compile(r"spore-[0-9A-Za-z_-]{1,56}")'), "rewrite",
         "the id resolver reads both prefixes for good: old ids live in immutable history",
         paths=("levain/consent/questions.py",),
         replacement='INTENTION_ID = re.compile(r"(?:intention|spore)-[0-9A-Za-z_-]{1,56}")', always=True),
]

# --------------------------------------------------------------------------------------------------------------
# the anneal seam
# --------------------------------------------------------------------------------------------------------------

ANNEAL_MODULE = "anneal_memory.spores"      # the module keeps its path; its names gain aliases
ANNEAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])anneal_memory\.spores(?![A-Za-z0-9_])")
ANNEAL_NAME_MAP = {
    "SporeStore": "IntentionStore", "SporeType": "IntentionType", "SporeError": "IntentionError",
    "SporeApply": "IntentionApply", "SporeOp": "IntentionOp",
    "normalize_spore_field": "normalize_intention_field", "spore_version": "intention_version",
}
# A call to one of these names constructs an anneal store, so it passes the id prefix. `_GUARDED` is the one place
# levain builds its guarded subclass.
STORE_CALLEE_RE = re.compile(r"^(?:\w*SporeStore|_GUARDED)$")
ID_PREFIX_KW = 'id_prefix="intention"'

# --------------------------------------------------------------------------------------------------------------
# candidates: what looks stored or wire
# --------------------------------------------------------------------------------------------------------------

WIRE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])spores?[_:]", re.IGNORECASE)     # "spore_touch", "spore:tray", "spore:"
BARE_BODY_RE = re.compile(r"spores?", re.IGNORECASE)                            # a whole literal: a key, a verb, an argv word
STORE_FILE_RE = re.compile(r"[\w.\-]*spores?\.json", re.IGNORECASE)
CODE_SUFFIXES = {".js", ".mjs", ".html", ".json", ".toml", ".yml", ".yaml", ".sh", ".css", ".cfg", ".ini", ".svg"}
QUOTED_RE = re.compile(r"""(?P<q>["'`])(?P<body>(?:\\.|(?!(?P=q))[^\\\n])*)(?P=q)""")


@dataclass
class Op:
    start: int
    end: int
    new: str
    tag: str


@dataclass
class Need:
    rel: str
    line: int
    kind: str
    excerpt: str
    suggest: str


@dataclass
class Lit:
    start: int
    end: int
    line: int
    doc: bool
    fstring: bool
    adjacent: bool
    prefix: str
    quote: str
    body: str


@dataclass
class Cand:
    start: int
    end: int
    kind: str
    text: str
    line: int
    lit: Lit | None = None


@dataclass
class FileOut:
    text: str
    subs: int = 0
    id_swaps: int = 0
    rewritten: dict[str, str] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    needs: list[Need] = field(default_factory=list)
    rule_hits: list[tuple[int, str, int, str]] = field(default_factory=list)     # (rule index, action, line, excerpt)
    seam: list[str] = field(default_factory=list)
    legacy: dict[str, str] = field(default_factory=dict)                          # const name -> literal value
    seam_imports: int = 0
    seam_calls: int = 0


def _lines(text: str) -> list[str]:
    """Lines split on ``\\n`` only, as the tokenizer and ``ast`` count them (``str.splitlines`` also splits on form feeds)."""
    return re.split(r"(?<=\n)", text) if text else []


def file_kind(rel: str) -> str:
    suf = Path(rel).suffix.lower()
    if suf == ".py":
        return "py"
    return "code" if suf in CODE_SUFFIXES else "prose"


def never_touched(rel: str) -> str | None:
    """The reason a path is outside T1, or None."""
    for glob, why in NEVER_TOUCHED:
        if fnmatch.fnmatchcase(rel, glob):
            return why
    if Path(rel).suffix.lower() in BINARY_SUFFIXES:
        return "binary"
    return None


# --------------------------------------------------------------------------------------------------------------
# pure rewriting
# --------------------------------------------------------------------------------------------------------------

def rewrite_string(s: str) -> str:
    """The word rewrite on a string that holds no protected span (a path, an identifier)."""
    out: list[str] = []
    pos = 0
    for m in SUB_RE.finditer(s):
        core = m.group(3)
        new = _new_word(core)
        s3 = m.start(3)
        if new is None or (core[0].islower() and m.group(1) is None and s3 > 0 and s[s3 - 1].isalpha()):
            continue
        out.append(s[pos:m.start()])
        art = m.group(1)
        out.append((("An" if art == "A" else "an") + m.group(2) if art else "") + new)
        pos = m.end()
    out.append(s[pos:])
    return "".join(out)


def rewrite_path(rel: str) -> str:
    return rewrite_string(rel) if ANY_WORD_RE.search(rel) else rel


def _line_of(text: str, off: int) -> int:
    return text.count("\n", 0, off) + 1


def split_literal(token: str) -> tuple[str, str, str] | None:
    """(prefix, quote, body) of a non-f Python string token, or None."""
    m = re.match(r"""^([A-Za-z]*)('''|\"\"\"|'|")(.*)\2$""", token, re.DOTALL)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def py_literals(src: str) -> list[Lit]:
    """The top-level string literals of a Python source (an f-string is one literal, its expressions included)."""
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    starts = [0]
    for line in _lines(src):
        starts.append(starts[-1] + len(line))

    def off(pos: tuple[int, int]) -> int:
        return starts[pos[0] - 1] + pos[1]

    sig = [i for i, t in enumerate(toks) if t.type not in (tokenize.NL, tokenize.COMMENT)]
    sig_pos = {ti: k for k, ti in enumerate(sig)}
    stmt = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    out: list[Lit] = []
    depth = 0
    f_first = 0
    for i, t in enumerate(toks):
        if t.type == tokenize.FSTRING_START:
            if depth == 0:
                f_first = i
            depth += 1
            continue
        if t.type == tokenize.FSTRING_END:
            depth -= 1
            if depth == 0:
                s, e = off(toks[f_first].start), off(t.end)
                prev = toks[sig[sig_pos[f_first] - 1]] if sig_pos[f_first] > 0 else None
                nxt = toks[sig[sig_pos[i] + 1]] if sig_pos[i] + 1 < len(sig) else None
                adj = (prev is not None and prev.type == tokenize.STRING) or (nxt is not None and nxt.type == tokenize.STRING)
                out.append(Lit(s, e, toks[f_first].start[0], False, True, adj, "f", "", src[s:e]))
            continue
        if t.type != tokenize.STRING or depth:
            continue
        k = sig_pos[i]
        prev = toks[sig[k - 1]] if k > 0 else None
        nxt = toks[sig[k + 1]] if k + 1 < len(sig) else None
        doc = (prev is None or prev.type in stmt) and (nxt is None or nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER))
        adj = (prev is not None and prev.type == tokenize.STRING) or (nxt is not None and nxt.type == tokenize.STRING)
        parts = split_literal(t.string)
        prefix, quote, body = parts if parts else ("", "", t.string)
        out.append(Lit(off(t.start), off(t.end), t.start[0], doc, False, adj, prefix, quote, body))
    return out


def _insertion_line(src: str) -> int:
    """The 1-based line after which a new top-level import goes: after the first run of imports, else after the docstring."""
    body = list(ast.parse(src).body)
    last = 0
    i = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        last = body[0].end_lineno or 0
        i = 1
    while i < len(body) and isinstance(body[i], ast.Import | ast.ImportFrom):
        last = body[i].end_lineno or last
        i += 1
    return last


class _Offsets:
    """Character offsets for ``ast`` positions, which count UTF-8 bytes."""

    def __init__(self, text: str) -> None:
        self.lines = _lines(text)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))

    def at(self, lineno: int, col_bytes: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode("utf-8")[:col_bytes].decode("utf-8", "ignore"))


# --------------------------------------------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------------------------------------------

def _no_const_spans(tree: ast.AST, offs: _Offsets) -> list[tuple[int, int]]:
    """Where a name may not stand in for a literal: a match pattern and a ``Literal[...]`` subscript."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.match_case):
            target = node.pattern
        elif isinstance(node, ast.Subscript):
            v = node.value
            if (isinstance(v, ast.Name) and v.id == "Literal") or (isinstance(v, ast.Attribute) and v.attr == "Literal"):
                target = node.slice
        if target is not None and getattr(target, "end_lineno", None):
            spans.append((offs.at(target.lineno, target.col_offset), offs.at(target.end_lineno, target.end_col_offset)))
    return spans


def _export_spans(tree: ast.AST, offs: _Offsets) -> list[tuple[int, int]]:
    """The value of every ``__all__ = [...]`` / ``__all__ += [...]`` statement."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is not None and any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) \
                    and getattr(value, "end_lineno", None):
                spans.append((offs.at(value.lineno, value.col_offset), offs.at(value.end_lineno, value.end_col_offset)))
    return spans


def transform_file(rel: str, text: str, rules: list[Rule]) -> FileOut:
    """Every edit one T1 file needs: seam, rules, then the word rewrite. Nothing is written here."""
    out = FileOut(text=text)
    kind = file_kind(rel)
    active = [(i, r) for i, r in enumerate(rules) if r.applies_to(rel)]
    if not ANY_WORD_RE.search(text) and not any(r.always for _, r in active):
        return out

    ops: list[Op] = []
    prot: list[tuple[int, int]] = []

    def clash(s: int, e: int) -> bool:
        if s == e:
            return any(a < s < b for a, b in prot)
        return any(a < e and s < b for a, b in prot)

    def add(s: int, e: int, new: str, tag: str) -> bool:
        if clash(s, e):
            out.refusals.append(f"{rel}:{_line_of(text, s)}: overlapping edits ({tag})")
            return False
        ops.append(Op(s, e, new, tag))
        if s != e:
            prot.append((s, e))
        return True

    tree: ast.AST | None = None
    offs = _Offsets(text)
    lits: list[Lit] = []
    if kind == "py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            out.refusals.append(f"{rel}: does not parse before the rewrite ({exc})")
            return out
        try:
            lits = py_literals(text)
        except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
            out.refusals.append(f"{rel}: tokenize failed ({exc})")
            return out
    json_ok_before = False
    if rel.endswith(".json"):
        try:
            json.loads(text)
            json_ok_before = True
        except ValueError:
            json_ok_before = False

    # the module path stays: anneal 1.0.7 keeps `anneal_memory/spores.py`
    for m in ANNEAL_PATH_RE.finditer(text):
        prot.append(m.span())

    no_const = _no_const_spans(tree, offs) if tree is not None else []
    # a name in `__all__` is an identifier spelled as a string: it follows its definition, it is not a stored value
    exports = _export_spans(tree, offs) if tree is not None else []
    legacy_names: set[str] = set()

    def hit(idx: int, action: str, s: int, excerpt: str) -> None:
        out.rule_hits.append((idx, action, _line_of(text, s), excerpt[:90]))

    def apply_rule(idx: int, rule: Rule, s: int, e: int, m: re.Match[str], lit: Lit | None) -> None:
        if rule.action == "refuse":
            hit(idx, "refuse", s, text[s:e])
            out.refusals.append(f"{rel}:{_line_of(text, s)}: rule {idx} refuses ({rule.reason}): {text[s:e][:80]!r}")
        elif rule.action == "rewrite":
            hit(idx, "rewrite", s, text[s:e])
            if rule.replacement is not None:
                add(s, e, m.expand(rule.replacement), f"rule {idx}")
        else:
            hit(idx, "keep", s, text[s:e])
            whole = (rule.const is not None and kind == "py" and lit is not None and not lit.fstring and not lit.adjacent
                     and not lit.doc and "b" not in lit.prefix.lower() and lit.quote
                     and rule.rx.fullmatch(lit.body) is not None
                     and not any(a <= lit.start and lit.end <= b for a, b in no_const))
            if whole:
                assert rule.const is not None and lit is not None
                value = ast.literal_eval(text[lit.start:lit.end])
                if out.legacy.get(rule.const, value) != value:
                    out.refusals.append(f"{rel}:{lit.line}: constant {rule.const} would hold two values")
                elif add(lit.start, lit.end, rule.const, f"rule {idx} const"):
                    out.legacy[rule.const] = value
                    legacy_names.add(rule.const)
            else:
                prot.append((s, e))

    for _idx, rule in active:
        if rule.settled is not None:
            prot.extend(m.span() for m in rule.settled.finditer(text))

    # always-rules: a pattern applied wherever it matches in the files it names
    covered: list[tuple[int, int]] = []
    for idx, rule in active:
        if rule.always:
            for m in rule.rx.finditer(text):
                apply_rule(idx, rule, m.start(), m.end(), m, None)
                covered.append(m.span())

    # candidates
    cands: list[Cand] = []
    for m in STORE_FILE_RE.finditer(text):
        cands.append(Cand(m.start(), m.end(), "store-file", m.group(0), _line_of(text, m.start())))
    if kind == "py":
        for lit in lits:
            if lit.doc or not ANY_WORD_RE.search(text[lit.start:lit.end]) \
                    or any(a <= lit.start and lit.end <= b for a, b in exports):
                continue
            body_text = text[lit.start:lit.end]
            if not lit.fstring and BARE_BODY_RE.fullmatch(lit.body):
                cands.append(Cand(lit.start, lit.end, "bare-word", body_text, lit.line, lit))
            elif WIRE_TOKEN_RE.search(body_text):
                cands.append(Cand(lit.start, lit.end, "wire-token", body_text, lit.line, lit))
            elif ANNEAL_PATH_RE.search(body_text):
                cands.append(Cand(lit.start, lit.end, "anneal-module", body_text, lit.line, lit))
        assert tree is not None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name == ANNEAL_MODULE or a.name.startswith(ANNEAL_MODULE + ".") for a in node.names):
                    s, e = offs.at(node.lineno, node.col_offset), offs.at(node.end_lineno, node.end_col_offset)
                    cands.append(Cand(s, e, "anneal-module-import", text[s:e], node.lineno))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "anneal_memory" \
                    and any(a.name == "spores" for a in node.names):
                s, e = offs.at(node.lineno, node.col_offset), offs.at(node.end_lineno, node.end_col_offset)
                cands.append(Cand(s, e, "anneal-module-import", text[s:e], node.lineno))
    elif kind == "code":
        for m in QUOTED_RE.finditer(text):
            body = m.group("body")
            if not ANY_WORD_RE.search(body):
                continue
            if BARE_BODY_RE.fullmatch(body):
                cands.append(Cand(m.start(), m.end(), "bare-word", m.group(0), _line_of(text, m.start())))
            elif WIRE_TOKEN_RE.search(body):
                cands.append(Cand(m.start(), m.end(), "wire-token", m.group(0), _line_of(text, m.start())))
            elif ANNEAL_PATH_RE.search(body):
                cands.append(Cand(m.start(), m.end(), "anneal-module", m.group(0), _line_of(text, m.start())))

    for c in cands:
        if any(a <= c.start and c.end <= b for a, b in covered):
            continue
        settled = False
        for idx, rule in active:
            if rule.always:
                continue
            ms = list(rule.rx.finditer(c.text))
            if not ms:
                continue
            for m in ms:
                apply_rule(idx, rule, c.start + m.start(), c.start + m.end(), m, c.lit)
            settled = True
            break
        if not settled:
            out.needs.append(Need(rel, c.line, c.kind, c.text[:100], re.escape(c.text[:60])))

    # the anneal seam
    if kind == "py" and tree is not None:
        assert tree is not None
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == ANNEAL_MODULE:
                for alias in node.names:
                    if not ANY_WORD_RE.search(alias.name):
                        continue
                    new = ANNEAL_NAME_MAP.get(alias.name)
                    if new is None:
                        out.refusals.append(f"{rel}:{alias.lineno}: anneal name {alias.name} has no mapped alias")
                        continue
                    s = offs.at(alias.lineno, alias.col_offset)
                    if add(s, s + len(alias.name), new, "seam import"):
                        out.seam_imports += 1
                        out.seam.append(f"{rel}:{alias.lineno}: import {alias.name} -> {new}")
            elif isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name is None or not STORE_CALLEE_RE.match(name):
                    continue
                if any(k.arg == "id_prefix" for k in node.keywords):
                    continue
                ends = [(a.end_lineno, a.end_col_offset) for a in [*node.args, *node.keywords]]
                if ends:
                    el, ec = max(ends)
                    pos = offs.at(el, ec)
                    ins = ", " + ID_PREFIX_KW
                else:
                    pos = offs.at(node.end_lineno, node.end_col_offset) - 1
                    ins = ID_PREFIX_KW
                    if text[pos:pos + 1] != ")":
                        out.refusals.append(f"{rel}:{node.lineno}: cannot place id_prefix on this call")
                        continue
                if add(pos, pos, ins, "seam call"):
                    out.seam_calls += 1
                    out.seam.append(f"{rel}:{node.lineno}: {name}(...) passes {ID_PREFIX_KW}")

    # the legacy import
    if legacy_names:
        line = _insertion_line(text)
        stmt = f"from {LEGACY_MODULE} import {', '.join(sorted(legacy_names))}\n"
        pos = offs.starts[line] if line < len(offs.lines) else len(text)
        if line >= len(offs.lines) and text and not text.endswith("\n"):
            stmt = "\n" + stmt
        ops.append(Op(pos, pos, stmt, "legacy import"))

    # the word rewrite
    for m in SUB_RE.finditer(text):
        s, e = m.span()
        s3 = m.start(3)
        core = m.group(3)
        if clash(s, e) or any(a <= s3 < b for a, b in prot):
            continue
        run_s, run_e = s3, m.end(3)
        while run_s > 0 and (text[run_s - 1].isalnum() or text[run_s - 1] == "_"):
            run_s -= 1
        while run_e < len(text) and (text[run_e].isalnum() or text[run_e] == "_"):
            run_e += 1
        run = text[run_s:run_e]
        new = _new_word(core)
        if new is None or (core[0].islower() and m.group(1) is None and s3 > 0 and text[s3 - 1].isalpha()):
            out.left.append(f"{rel}:{_line_of(text, s3)}: {run}")
            continue
        if INTENT_RE.search(run):
            out.refusals.append(f"{rel}:{_line_of(text, s3)}: token {run!r} holds both spore and intent")
            continue
        art = m.group(1)
        rep = (("An" if art == "A" else "an") + m.group(2) if art else "") + new
        ops.append(Op(s, e, rep, "word"))
        out.subs += 1
        if text[m.end(3):m.end(3) + 1] == "-" and text[m.end(3) + 1:m.end(3) + 2].isdigit():
            out.id_swaps += 1
        if run.lower() not in BARE_TOKENS:
            out.rewritten[run] = rewrite_string(run)

    # assemble
    result = text
    for op in sorted(ops, key=lambda o: (-o.start, -o.end)):
        result = result[:op.start] + op.new + result[op.end:]
    out.text = result
    if kind == "py" and result != text:
        try:
            ast.parse(result)
        except SyntaxError as exc:
            out.refusals.append(f"{rel}: does not parse after the rewrite ({exc})")
    if json_ok_before and result != text:
        try:
            json.loads(result)
        except ValueError as exc:
            out.refusals.append(f"{rel}: no longer valid JSON after the rewrite ({exc})")
    return out


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    root: Path
    git: bool = False
    t1: int = 0
    never: dict[str, int] = field(default_factory=dict)                  # reason -> files
    never_with_word: dict[str, int] = field(default_factory=dict)        # reason -> files holding the word
    moves: list[tuple[str, str]] = field(default_factory=list)
    writes: dict[str, str] = field(default_factory=dict)                 # NEW path -> new text
    creates: dict[str, str] = field(default_factory=dict)
    refusals: list[tuple[str, str]] = field(default_factory=list)        # (category, message)
    needs: list[Need] = field(default_factory=list)
    subs_by_group: Counter = field(default_factory=Counter)
    files_by_group: Counter = field(default_factory=Counter)
    id_swaps: int = 0
    rule_hits: list[tuple[str, int, str, int, str]] = field(default_factory=list)   # (rel, rule, action, line, excerpt)
    seam: list[str] = field(default_factory=list)
    seam_imports: int = 0
    seam_calls: int = 0
    left: list[str] = field(default_factory=list)
    changelog_touched: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.refusals or self.needs)

    @property
    def empty(self) -> bool:
        return not (self.moves or self.writes or self.creates)


def list_files(root: Path) -> list[str]:
    """The tracked files of a git tree; a plain directory (a ``git archive`` export) lists every file."""
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return sorted(p for p in out.decode("utf-8").split("\0") if p and (root / p).is_file())
    files: list[str] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for nm in names:
            files.append(str(Path(dirpath, nm).relative_to(root)))
    return sorted(files)


def _read_text(root: Path, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _group(rel: str) -> str:
    if rel.startswith("tests/"):
        return "tests"
    if rel.startswith("levain/templates/"):
        return "templates"
    if rel.endswith((".md", ".rst", ".txt")) or rel.startswith("docs/"):
        return "docs"
    if rel.startswith("levain/"):
        return "code"
    return "other"


def _split_section(text: str, heading: str) -> tuple[str, str, str]:
    """(head, section, tail): the part of a changelog from ``heading`` to the next ``## `` heading."""
    m = re.search(r"^" + re.escape(heading) + r"[^\n]*\n", text, re.M)
    if not m:
        return text, "", ""
    nxt = re.search(r"^## ", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[:m.start()], text[m.start():end], text[end:]


def build_plan(root: Path, rules: list[Rule] | None = None) -> Plan:
    rules = STORED_TOKEN_RULES if rules is None else rules
    plan = Plan(root=root, git=(root / ".git").exists())
    files = list_files(root)
    fileset = set(files)

    texts: dict[str, str] = {}
    never_count: Counter = Counter()
    never_word: Counter = Counter()
    for rel in files:
        why = never_touched(rel)
        text = None if why == "binary" else _read_text(root, rel)
        if why is None and text is not None and MARKER in text[:2000]:
            why = "marked historical"
        if why is not None:
            never_count[why] += 1
            if text is not None and ANY_WORD_RE.search(text):
                never_word[why] += 1
            continue
        if text is None:
            never_count["not text"] += 1
            continue
        texts[rel] = text
    plan.never = dict(never_count)
    plan.never_with_word = dict(never_word)
    plan.t1 = len(texts)

    tokens: set[str] = set()
    for text in texts.values():
        tokens.update(TOKEN_RE.findall(text))

    # R1: moves
    targets: dict[str, str] = {}
    move_of: dict[str, str] = {}
    for rel in sorted(texts):
        new = rewrite_path(rel)
        if new == rel:
            continue
        if never_touched(new):
            plan.refusals.append(("path outside T1", f"{rel} -> {new} (the target is not a T1 path: {never_touched(new)})"))
            continue
        if new in fileset or new in targets or (root / new).exists():
            plan.refusals.append(("move collision", f"{rel} -> {new} (the target exists)"))
            continue
        if new.endswith(".py") and any(f.startswith(new[:-3] + "/") for f in fileset):
            plan.refusals.append(("move collision", f"{rel} -> {new} (a package of that name exists)"))
            continue
        targets[new] = rel
        move_of[rel] = new
        plan.moves.append((rel, new))

    # R2/R4/R5/R8: content
    rewritten_in: dict[str, set[str]] = defaultdict(set)
    legacy_values: dict[str, str] = {}
    for rel, text in sorted(texts.items()):
        section = SECTION_ONLY.get(rel)
        if section:
            head, sec, tail = _split_section(text, section)
            out = transform_file(rel, sec, rules)
            out.text = head + out.text + tail
            if out.text != text:
                plan.changelog_touched = True
        else:
            out = transform_file(rel, text, rules)
        new_rel = move_of.get(rel, rel)
        for msg in out.refusals:
            cat = "parse" if "parse" in msg or "tokenize" in msg or "JSON" in msg else \
                "mixed token" if "holds both" in msg else "rule or edit"
            plan.refusals.append((cat, msg))
        plan.needs += out.needs
        plan.left += out.left
        plan.seam += out.seam
        plan.seam_imports += out.seam_imports
        plan.seam_calls += out.seam_calls
        plan.id_swaps += out.id_swaps
        for idx, action, line, ex in out.rule_hits:
            plan.rule_hits.append((rel, idx, action, line, ex))
        for old, new in out.rewritten.items():
            rewritten_in[old].add(rel)
        for name, value in out.legacy.items():
            if legacy_values.get(name, value) != value:
                plan.refusals.append(("legacy constant", f"{name} would hold two values"))
            legacy_values[name] = value
        if out.subs:
            plan.subs_by_group[_group(rel)] += out.subs
            plan.files_by_group[_group(rel)] += 1
        if out.text != text:
            plan.writes[new_rel] = out.text
        if out.rewritten:
            for old, new in out.rewritten.items():
                if new in tokens:
                    plan.refusals.append(("collision", f"{old} -> {new}: the new name already exists in the tree "
                                                       f"(first seen renaming in {rel})"))
    # collisions are reported once per identifier
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for cat, msg in plan.refusals:
        key = msg.split(":", 1)[0] if cat == "collision" else msg
        if cat == "collision":
            if key in seen:
                continue
            seen.add(key)
        uniq.append((cat, msg))
    plan.refusals = uniq

    # created files
    if legacy_values:
        existing = _read_text(root, LEGACY_PATH) if (root / LEGACY_PATH).exists() else None
        if existing is None:
            plan.creates[LEGACY_PATH] = render_legacy(legacy_values, rules)
        else:
            for name, value in legacy_values.items():
                if not re.search(rf"^{re.escape(name)} = {re.escape(repr(value))}\s*(#.*)?$", existing, re.M):
                    plan.refusals.append(("legacy constant", f"{LEGACY_PATH} exists without {name} = {value!r}"))
    return plan


def render_legacy(values: dict[str, str], rules: list[Rule]) -> str:
    reasons = {r.const: r.reason for r in rules if r.const}
    lines = [
        '"""levain.intention_legacy: the ONE module that spells the old name, "spore", as code.',
        "",
        "What an older release stored or put on a wire under that name keeps its spelling, because a store is append-only and",
        "a reader must still recognise it. Everything that names one of those bytes does it through a constant here.",
        '"""',
        "from __future__ import annotations",
        "",
    ]
    for name in sorted(values):
        lines.append(f"{name} = {values[name]!r}" + (f"   # {reasons[name]}" if name in reasons else ""))
    return "\n".join(lines) + "\n"


def apply_plan(plan: Plan) -> None:
    root = plan.root
    for old, new in plan.moves:
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-C", str(root), "mv", old, new], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git mv {old} {new} failed: {r.stderr.strip()}")
    for new, text in plan.writes.items():
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        (root / new).write_bytes(text.encode("utf-8"))
    for rel, text in plan.creates.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))


def is_dirty(root: Path) -> bool:
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, check=True)
    return bool(r.stdout.strip())


# --------------------------------------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------------------------------------

def render_report(plan: Plan, *, applied: bool, root: Path, rules: list[Rule] | None = None) -> str:
    rules = STORED_TOKEN_RULES if rules is None else rules
    L: list[str] = []
    w = L.append
    w(f"# rename_intention report ({'APPLIED' if applied else 'DRY RUN'}), root {root}")
    w("")
    if plan.blocked:
        w(f"## REFUSED: {len(plan.refusals)} refusals and {len(plan.needs)} NEEDS-RULE items (nothing was changed)")
        w("")
    w("## scope")
    w(f"- T1 files (rewritten set): {plan.t1}")
    for why, n in sorted(plan.never.items()):
        w(f"- never touched, {why}: {n} files ({plan.never_with_word.get(why, 0)} hold the word)")
    w(f"- CHANGELOG [Unreleased] section touched: {'yes' if plan.changelog_touched else 'no'}")
    w("")
    w("## per rule")
    w(f"- R1 moves: {len(plan.moves)} files")
    total = sum(plan.subs_by_group.values())
    w(f"- R2 word rewrites: {total} in {sum(plan.files_by_group.values())} files "
      f"({', '.join(f'{g} {n}' for g, n in sorted(plan.subs_by_group.items())) or 'none'}); {plan.id_swaps} of them spore-NNNN ids")
    by_rule: dict[int, Counter] = defaultdict(Counter)
    for _rel, idx, action, _line, _ex in plan.rule_hits:
        by_rule[idx][action] += 1
    w(f"- R4 stored-token rules fired: {sum(sum(c.values()) for c in by_rule.values())}")
    for idx, c in sorted(by_rule.items()):
        w(f"  - rule {idx} ({rules[idx].action}; {rules[idx].reason}): {', '.join(f'{a} {n}' for a, n in sorted(c.items()))}")
    w(f"- R5 anneal seam: {plan.seam_imports} imported names, {plan.seam_calls} store constructions get id_prefix")
    w(f"- R8 refusals: {len(plan.refusals)}; NEEDS-RULE: {len(plan.needs)}")
    w(f"- created: {', '.join(plan.creates) or 'nothing'}")
    w("")
    w(f"## R1 paths moved ({len(plan.moves)})")
    L.extend(f"- {a} -> {b}" for a, b in plan.moves)
    w("")
    w(f"## R8 refusals ({len(plan.refusals)}); each stops --apply")
    by_cat: dict[str, list[str]] = defaultdict(list)
    for cat, msg in plan.refusals:
        by_cat[cat].append(msg)
    for cat in sorted(by_cat):
        w(f"### {cat} ({len(by_cat[cat])})")
        L.extend(f"- {m}" for m in by_cat[cat])
    w("")
    w(f"## NEEDS-RULE ({len(plan.needs)}): a stored- or wire-looking spelling no STORED_TOKEN_RULES entry covers")
    if plan.needs:
        w("--apply refuses until every item below is covered by an entry (keep-as-legacy-constant, rewrite or refuse).")
    kinds = Counter(n.kind for n in plan.needs)
    w("by kind: " + (", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "none"))
    for n in plan.needs:
        w(f"- {n.rel}:{n.line} [{n.kind}] {n.excerpt!r}   suggest pattern: {n.suggest}")
    w("")
    w(f"## R4 rule hits ({len(plan.rule_hits)})")
    L.extend(f"- {rel}:{line} rule {idx} {action}: {ex!r}" for rel, idx, action, line, ex in plan.rule_hits)
    w("")
    w(f"## R5 anneal seam lines ({len(plan.seam)})")
    L.extend(f"- {s}" for s in plan.seam)
    w("")
    w(f"## spelled `spore` and left alone (a longer word or an odd case) ({len(plan.left)})")
    L.extend(f"- {s}" for s in plan.left)
    w("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------------------------------------------

def default_root() -> Path:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
        return Path(top)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def _emit(report: str, path: Path | None) -> None:
    if path:
        path.write_text(report, encoding="utf-8")
    sys.stdout.write(report)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--root", type=Path, default=None, help="the tree to rename (default: the git top level)")
    ap.add_argument("--report", type=Path, default=None, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = (args.root or default_root()).resolve()
    plan = build_plan(root)
    if plan.blocked:
        _emit(render_report(plan, applied=False, root=root), args.report)
        return 2
    if args.apply and not plan.empty:
        if not plan.git:
            _emit(render_report(plan, applied=False, root=root), args.report)
            sys.stderr.write("refused: --apply needs a git tree (the moves are git mv)\n")
            return 4
        if is_dirty(root):
            _emit(render_report(plan, applied=False, root=root), args.report)
            sys.stderr.write("refused: --apply on a dirty worktree; commit or clean it first\n")
            return 3
        apply_plan(plan)
        again = build_plan(root)
        if not again.empty or again.blocked:
            _emit(render_report(plan, applied=True, root=root), args.report)
            sys.stderr.write("the tree is not stable after --apply: a second pass still has work\n")
            return 1
    _emit(render_report(plan, applied=args.apply, root=root), args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
