#!/usr/bin/env python3
"""Rename levain's role "coordinator" to "desk", everywhere except stored data.

    python3 scripts/rename_desk.py [--apply] [--root DIR] [--report FILE]

Dry-run by default: it computes every change in memory and prints the report. ``--apply`` writes. It is idempotent:
a tree already renamed has nothing left to do, and ``--apply`` on it changes nothing and exits 0.

What it does (the rules are PLAN_1_RENAME.md R1-R10):

* R1 paths: ``levain/coordinator/`` -> ``levain/desk/``, ``levain/cockpit/coordinator_*.py`` / ``coord_effects.py`` ->
  ``desk_*.py``, ``tests/test_*coordinator*.py`` -> ``test_*desk*.py``, ``tests/test_cockpit_coord_drop.py`` ->
  ``test_cockpit_desk_drop.py`` (``git mv`` in a git tree). A name collision refuses the whole run and lists it.
* R2 words: ``Coordinator``/``coordinator``/``COORDINATOR`` -> ``Desk``/``desk``/``DESK`` in code, comments, docstrings,
  help text, templates and docs. ``Coord*`` classes, ``coord_*`` names, ``coordinate`` and ``coordroom`` are never touched (R3).
* R4 stored tokens: a Python string literal that is a stored value (a bare ``"coordinator"``, a quoted SQL literal,
  a stored kind / key / file name) is replaced by a constant from ``levain/desk/legacy.py``, which this codemod
  creates; SQL text becomes an f-string interpolating the constant so the schema text is byte-identical.
* R5 the home directory resolver ``desk_home()``, R6 the ``levain desk`` verb with its ``coord`` / ``coordinator``
  aliases, R7 the ``levain/coordinator/__init__.py`` import shim, R8 the contract owner paths, R9 the docs.

The DONE check (printed at the end) is the plan's: no file under ``levain/`` holds the word except ``legacy.py``
and the shim.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------------------------------------------

OLD = "coordinator"
WORD_RE = re.compile(r"(?<!\d{4}-)coordinator", re.IGNORECASE)   # `1020-coordinator/` is a project-memory directory name
DONE_RE = re.compile(r"(?<![A-Za-z0-9_])coordinator(?![A-Za-z0-9_])", re.IGNORECASE)   # `git grep -w -i`
CLI_PHRASE_RE = re.compile(r"\blevain coord\b(?![-\w])")     # `levain coord status` in prose -> `levain desk status`

# A whole string literal that is a stored/wire value -> its constant in levain/desk/legacy.py.
STORED_WHOLE: dict[str, str] = {
    "coordinator": "ROLE_DESK",
    "coordinator_reply": "KIND_DESK_REPLY",
    "reply_coordinator": "KIND_REPLY_DESK",
    "coordinator-wake": "FACT_DESK_WAKE",
    "coordinator_drop": "VERB_DESK_DROP",
    "coordinator_workspaces": "RECORD_KEY_WORKSPACES",
    "coordinator_workspaces.json": "RECORD_NAME_WORKSPACES",
    "levain-coordinator-stopped": "BAND_KEY_DESK_STOPPED",
    "levain-coordinator.rules": "CODEX_RULE_NAME",
    "levain-coordinator-": "SESSION_PREFIX_DESK",
    "coordinator-draft:": "DRAFT_AGENT_PREFIX",
    "codex coordinator removed": "CAUSE_CODEX_REMOVED",     # a launch's persisted end cause
}
# A stored token inside a longer literal (an f-string interpolation puts the constant back): (text, constant).
# The quoted forms are the SQL / JSON spellings and come first; longest first within the unquoted ones.
STORED_QUOTED_SQL: list[tuple[str, str]] = [
    ("'coordinator_reply'", "SQL_KIND_DESK_REPLY"),
    ("'reply_coordinator'", "SQL_KIND_REPLY_DESK"),
    ("'coordinator'", "SQL_ROLE_DESK"),
]
STORED_JSON: list[tuple[str, str]] = [('"coordinator"', "JSON_ROLE_DESK")]
STORED_SUB: list[tuple[str, str]] = sorted([
    ("levain-coordinator-stopped", "BAND_KEY_DESK_STOPPED"),
    ("levain-coordinator.rules", "CODEX_RULE_NAME"),
    ("levain-coordinator-", "SESSION_PREFIX_DESK"),
    ("(coordinator|worker)", "SESSION_ROLE_ALT"),
    ("coordinator_workspaces.json", "RECORD_NAME_WORKSPACES"),
    ("coordinator_workspaces", "RECORD_KEY_WORKSPACES"),
    ("coordinator_reply", "KIND_DESK_REPLY"),
    ("reply_coordinator", "KIND_REPLY_DESK"),
    ("coordinator-wake", "FACT_DESK_WAKE"),
    ("coordinator-draft:", "DRAFT_AGENT_PREFIX"),
    ("codex coordinator removed", "CAUSE_CODEX_REMOVED"),
    ("coordinator_drop", "VERB_DESK_DROP"),
], key=lambda p: -len(p[0]))
# Trigger messages a schema module passes to a helper that builds the trigger text (so no SQL keyword sits in the
# literal): the stored trigger text holds them, so they keep the word. Found by diffing the two trees' schema dump.
FRAGMENTS_BY_FILE: dict[str, list[str]] = {
    "levain/coordinator/lease.py": ["a coordinator setting"],
    "levain/coordinator/store.py": ["a coordinator reply", "one coordinator reply"],
    "levain/coordinator/ready.py": ["a coordinator writes under another name)"],
}
# A test that matches a stored trigger's message by a short regex: the whole literal is the message text.
TEST_EXACT = ("coordinator mode", "not cleared in coordinator mode")
SQL_KW = re.compile(r"\b(CHECK|WHERE|VALUES|SELECT|INSERT|UPDATE|WHEN|THEN|NOT NULL|DEFAULT|BEGIN|RAISE|ABORT|END|CREATE|TRIGGER|FROM)\b|\bIN\s*\(")
SQL_FORM = re.compile(r"[(,]\s*'coordinator\w*'\s*[,)]|[=!]=?\s*'coordinator\w*'|^\s*'coordinator\w*'\s*[,)]")


def is_sql(body: str) -> bool:
    """SQL text: an upper-case SQL keyword, or the quoted word in a list / comparison position. Schema text a store
    holds is stored data in all of it (a trigger's message too), so such a literal keeps every spelling of the word."""
    return bool(SQL_KW.search(body) or SQL_FORM.search(body))
JSONISH = re.compile(r'"[A-Za-z_]+"\s*:\s*"coordinator"|"coordinator"\s*[,}\]]')

# Files whose content is never rewritten (the history, the frozen fixtures, the codemod itself, the two files that
# are allowed to hold the old word as code).
EXEMPT_PREFIXES = ("tests/fixtures/", "tests/golden/")
EXEMPT_FILES = {"CHANGELOG.md", "scripts/rename_desk.py", "tests/test_rename_desk.py", "levain/desk/legacy.py",
                # the legacy-store fixture's generator and its test name the old spelling on purpose
                "scripts/make_desk_legacy_fixture.py", "tests/test_desk_legacy_store.py"}
GOLDEN_NAMESPACES = "tests/golden/contracts/frozen/namespaces.json"
TEXT_SUFFIXES = {".py", ".md", ".js", ".html", ".json", ".toml", ".txt", ".in", ".cfg", ".yml", ".yaml", ".css", ".sh",
                 ".mjs", ".ini", ".svg", ""}

LEGACY_PATH = "levain/desk/legacy.py"
SHIM_PATH = "levain/coordinator/__init__.py"
OLD_PKG_DIR = "levain/coordinator/"
NEW_PKG_DIR = "levain/desk/"
EXTRA_PATH_MAP = {"levain/cockpit/coord_effects.py": "levain/cockpit/desk_effects.py",
                  "tests/test_cockpit_coord_drop.py": "tests/test_cockpit_desk_drop.py"}
# module names that change with their file, spelled without the word
MODULE_RENAMES = [(re.compile(r"\bcoord_effects\b"), "desk_effects"),
                  (re.compile(r"\btest_cockpit_coord_drop\b"), "test_cockpit_desk_drop")]

CHANGELOG_SENTINEL = "**The coordinator is now the desk, everywhere but stored data.**"
CHANGELOG_ENTRY = (
    "- " + CHANGELOG_SENTINEL + " The package `levain.coordinator` is `levain.desk`, the CLI verb is `levain desk`, and "
    "every help line, label, error message and doc says \"desk\". `levain coord` and `levain coordinator` stay as "
    "aliases of `levain desk` (same parser, same exit codes) for the whole 1.x line, and `import levain.coordinator` stays "
    "as a shim over `levain.desk`. Nothing stored changes: the persisted role value, the table and column names, the "
    "`~/.levain/coordinator` directory (a new install uses `~/.levain/desk`; `desk_home()` picks whichever exists), the "
    "`/coord` routes and the signed-contract namespaces keep their spelling, and `levain/desk/legacy.py` is the one "
    "module that names them. The contract owner paths of three namespaces moved (`Frozen-contract: owner paths only, "
    "namespaces unchanged`).\n")

README_OLD = "The desk is the session that holds the coordinator role"
README_NEW = "The desk is the session that holds the lease on your drops"


# --------------------------------------------------------------------------------------------------------------
# pure functions
# --------------------------------------------------------------------------------------------------------------

def _case(m: re.Match[str]) -> str:
    w = m.group(0)
    if w.isupper():
        return "DESK"
    return "Desk" if w[0].isupper() else "desk"


def fix_articles(text: str) -> str:
    """``an desk`` -> ``a desk`` (the substitution can never produce it from ``a coordinator``; kept as the guard)."""
    return re.sub(r"\b([Aa])n(\s+)(?=[Dd][Ee][Ss][Kk]\b)", lambda m: m.group(1) + m.group(2), text)


_PROTECT_TOKENS = sorted([old for old, _ in STORED_SUB if old != "(coordinator|worker)"], key=lambda p: -len(p))
_PROTECT_RE = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(t) for t in _PROTECT_TOKENS) + r")(?![A-Za-z0-9_])")


def rename_prose(text: str, *, also: str = "") -> str:
    """:func:`rename_words` with the stored spellings (a file name, a key, a kind) masked: prose that NAMES a stored
    thing keeps its real spelling. A token counts only as a whole word (``coordinator_drop_effect`` is an identifier)."""
    masks: list[str] = []

    def mask(m: re.Match[str]) -> str:
        masks.append(m.group(0))
        return f"\x00{len(masks) - 1}\x00"

    rx = _PROTECT_RE
    if also:
        rx = re.compile(_PROTECT_RE.pattern + "|" + also)
    out = rename_words(rx.sub(mask, text))
    return re.sub(r"\x00(\d+)\x00", lambda m: masks[int(m.group(1))], out)


def rename_words(text: str) -> str:
    """The R2 substitution on prose or identifiers: case-preserving, ``levain coord`` -> ``levain desk``, articles fixed."""
    out = WORD_RE.sub(_case, text)
    out = CLI_PHRASE_RE.sub("levain desk", out)
    for rx, new in MODULE_RENAMES:
        out = rx.sub(new, out)
    return fix_articles(out)


GLITCH_RE = re.compile(r"(?i)\bdesk(?:['’]s)?[ _\-/.]desk\b|\bdesk\b[^\n]{0,3}\(the desk\)|\bdesk (?:or|and) desk\b|\bdesk\(s\)")


def find_glitches(text: str, rel: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = GLITCH_RE.search(line)
        if m:
            out.append(f"{rel}:{n}: {line.strip()[:150]}")
    return out


def split_literal(token: str) -> tuple[str, str, str] | None:
    """(prefix, quote, body) of a non-f Python string token, or None."""
    m = re.match(r"""^([A-Za-z]*)('''|\"\"\"|'|")(.*)\2$""", token, re.DOTALL)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def classify_literal(body: str, *, quote: str = '"', docstring: bool = False,
                     fragments: tuple[str, ...] = (), exact: tuple[str, ...] = ()) -> tuple[str, str]:
    """What a string literal containing the word is.

    ("prose", why)  -> renamed by the word substitution;
    ("whole", NAME) -> the whole literal is a stored value: replaced by the constant NAME;
    ("interp", why) -> a stored token inside longer text (SQL, a regex, a key prefix): the literal becomes an f-string.
    """
    if docstring:
        return "prose", "docstring"
    if body in STORED_WHOLE:
        return "whole", STORED_WHOLE[body]
    for old, _const in STORED_SUB:
        if old in body:
            return "interp", f"stored token {old}"
    for old, _const in STORED_QUOTED_SQL:
        if old in body and (old != "'coordinator'" or is_sql(body)):
            return "interp", f"quoted SQL literal {old}"
    for frag in fragments:
        if frag in body:
            return "interp", f"stored trigger message ({frag!r})"
    if body in exact:
        return "interp", f"a test matching a stored trigger message ({body!r})"
    if is_sql(body) and re.search("coordinator", body):
        return "interp", "SQL text (stored schema text keeps every spelling)"
    if quote != '"' and JSONISH.search(body):
        return "interp", "JSON text"
    return "prose", "text"


def _interpolate(body: str, *, in_fstring: bool, fragments: tuple[str, ...] = ()) -> tuple[str, set[str]]:
    """Replace every stored token in ``body`` by ``{CONST}``; braces elsewhere in the text are doubled unless the text
    already sits inside an f-string (where they were doubled already)."""
    sql_ok = is_sql(body)
    cands: list[tuple[str, str]] = [(old, "{" + const + "}") for old, const in STORED_QUOTED_SQL[:2]]
    if sql_ok:
        cands.append((STORED_QUOTED_SQL[2][0], "{" + STORED_QUOTED_SQL[2][1] + "}"))
        cands.append(("coordinator", "{ROLE_DESK}"))      # prose inside SQL text (a trigger's message) is stored too
    if JSONISH.search(body):
        cands += [(old, "{" + const + "}") for old, const in STORED_JSON]
    cands += [(old, "{" + const + "}") for old, const in STORED_SUB]
    cands += [(frag, frag.replace("coordinator", "{ROLE_DESK}")) for frag in fragments]
    cands.sort(key=lambda p: -len(p[0]))
    rx = re.compile("|".join(re.escape(old) for old, _ in cands))
    lookup = dict(cands)
    used: set[str] = set()
    out: list[str] = []
    pos = 0

    def plain(seg: str) -> str:
        return seg if in_fstring else seg.replace("{", "{{").replace("}", "}}")

    for m in rx.finditer(body):
        out.append(plain(body[pos:m.start()]))
        tmpl = lookup[m.group(0)]
        used |= set(re.findall(r"\{(\w+)\}", tmpl))
        out.append(tmpl)
        pos = m.end()
    out.append(plain(body[pos:]))
    return "".join(out), used


@dataclass
class PyResult:
    text: str
    stored: list[tuple[int, str, str]] = field(default_factory=list)     # (line, excerpt, reason)
    manual: list[tuple[int, str]] = field(default_factory=list)
    consts: set[str] = field(default_factory=set)
    review: list[tuple[int, str]] = field(default_factory=list)          # prose literals that QUOTE the word


def _lines(text: str) -> list[str]:
    """Lines split on ``\\n`` only, as the tokenizer and ``ast`` count them (``str.splitlines`` also splits on form feeds)."""
    return re.split(r"(?<=\n)", text) if text else []


def _insertion_line(src: str) -> int:
    """The 1-based line after which a new top-level import goes: after the first run of top-level imports, else after
    the module docstring, else 0."""
    tree = ast.parse(src)
    body = list(tree.body)
    last = 0
    i = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        last = body[0].end_lineno or 0
        i = 1
    ran = False
    while i < len(body) and isinstance(body[i], ast.Import | ast.ImportFrom):
        last = body[i].end_lineno or last
        ran = True
        i += 1
    if not ran:
        # imports after a few assignments (e.g. a try/except block) are not chased: the line after the docstring
        pass
    return last


def transform_python(rel: str, src: str) -> PyResult:
    """The code-aware half of the rename for one Python file (stored tokens), then the word substitution."""
    res = PyResult(text=src)
    frags = tuple(FRAGMENTS_BY_FILE.get(rel, ()))
    exact = TEST_EXACT if rel.startswith("tests/") else ()
    frags_all = frags + exact
    if not WORD_RE.search(src):
        res.text = rename_words(src)        # the module-name renames (`coord_effects`) and `levain coord`
        return res
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        res.manual.append((0, f"tokenize failed ({exc}); word substitution only"))
        res.text = rename_words(src)
        return res
    starts = [0]
    for line in _lines(src):
        starts.append(starts[-1] + len(line))

    def off(pos: tuple[int, int]) -> int:
        return starts[pos[0] - 1] + pos[1]

    sig = [i for i, t in enumerate(toks) if t.type not in (tokenize.NL, tokenize.COMMENT)]
    sig_pos = {ti: k for k, ti in enumerate(sig)}
    edits: list[tuple[int, int, str]] = []

    def neighbours(ti: int) -> tuple[tokenize.TokenInfo | None, tokenize.TokenInfo | None]:
        k = sig_pos[ti]
        prev = toks[sig[k - 1]] if k > 0 else None
        nxt = toks[sig[k + 1]] if k + 1 < len(sig) else None
        return prev, nxt

    stmt_starts = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.type == tokenize.STRING and WORD_RE.search(t.string):
            parts = split_literal(t.string)
            prev, nxt = neighbours(i)
            doc = (prev is None or prev.type in stmt_starts) and (nxt is None or nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER))
            if parts is None or "b" in parts[0].lower():
                i += 1
                continue
            prefix, quote, body = parts
            kind, why = classify_literal(body, quote=quote, docstring=doc, fragments=frags, exact=exact)
            adjacent = (prev is not None and prev.type == tokenize.STRING) or (nxt is not None and nxt.type == tokenize.STRING)
            if kind == "whole" and not adjacent:
                edits.append((off(t.start), off(t.end), why))
                res.consts.add(why)
                res.stored.append((t.start[0], t.string[:90], f"whole literal is a stored value -> {why}"))
            elif kind in ("whole", "interp"):
                if nxt is not None and nxt.type == tokenize.OP and nxt.string == "." and sig_pos[i] + 2 < len(sig) \
                        and toks[sig[sig_pos[i] + 2]].string == "format":
                    res.manual.append((t.start[0], "a stored token inside a literal that is .format()ted"))
                else:
                    new_body, used = _interpolate(body, in_fstring=False, fragments=frags_all)
                    if not used and body in STORED_WHOLE:       # a stored value written as one piece of an implicit concatenation
                        new_body, used = "{" + STORED_WHOLE[body] + "}", {STORED_WHOLE[body]}
                    if used:
                        pfx = prefix if "f" in prefix.lower() else prefix + "f"
                        edits.append((off(t.start), off(t.end), f"{pfx}{quote}{new_body}{quote}"))
                        res.consts |= used
                        res.stored.append((t.start[0], t.string[:90], f"stored token inside a literal ({why}) -> {', '.join(sorted(used))}"))
            else:
                if not doc and re.search(r"""\\?['"]coordinator\\?['"]""", body):
                    res.review.append((t.start[0], t.string[:100]))
        elif t.type == tokenize.FSTRING_MIDDLE and WORD_RE.search(t.string):
            # the literal text of an f-string is the gap between the tokens around each run of FSTRING_MIDDLE
            first = i
            last = i
            while last + 1 < n and toks[last + 1].type == tokenize.FSTRING_MIDDLE:
                last += 1
            prev_tok = toks[first - 1]
            next_tok = toks[last + 1]
            gap_s, gap_e = off(prev_tok.end), off(next_tok.start)
            gap = src[gap_s:gap_e]
            if WORD_RE.search(gap):
                kind, why = classify_literal(gap, quote="'", fragments=frags, exact=exact)
                if kind in ("whole", "interp"):
                    new_gap, used = _interpolate(gap, in_fstring=True, fragments=frags_all)
                    if used:
                        edits.append((gap_s, gap_e, new_gap))
                        res.consts |= used
                        res.stored.append((t.start[0], gap[:90], f"stored token inside an f-string ({why}) -> {', '.join(sorted(used))}"))
                elif re.search(r"""['"]coordinator['"]""", gap):
                    res.review.append((t.start[0], gap[:100]))
            i = last
        i += 1

    text = src
    for s, e, new in sorted(edits, key=lambda x: -x[0]):
        text = text[:s] + new + text[e:]
    if res.consts:
        line = _insertion_line(src)
        names = ", ".join(sorted(res.consts))
        stmt = f"from levain.desk.legacy import {names}\n"
        lines = _lines(text)
        if line == 0 or line > len(lines):
            lines.append(stmt)
        else:
            lines.insert(line, stmt)
        text = "".join(lines)
    res.text = rename_prose(text)
    return res


def transform_text(rel: str, text: str) -> tuple[str, list[str]]:
    """Docs, templates and data files: the word substitution with the stored spellings masked (and listed)."""
    left: list[str] = []
    if rel.endswith(".md") and README_OLD in text:
        text = text.replace(README_OLD, README_NEW)
    out = rename_prose(text, also=r"'coordinator'|\"coordinator\"")
    left_rx = re.compile(_PROTECT_RE.pattern + r"|'coordinator'|\"coordinator\"")
    for n, line in enumerate(text.split("\n"), 1):
        m = left_rx.search(line)
        if m:
            left.append(f"{rel}:{n}: {m.group(0)}")
    return out, left


def path_rename(rel: str) -> str:
    if rel in EXTRA_PATH_MAP:
        return EXTRA_PATH_MAP[rel]
    if rel.startswith(OLD_PKG_DIR):
        return NEW_PKG_DIR + rel[len(OLD_PKG_DIR):]
    if re.search("coordinator", rel, re.I) and rel not in EXEMPT_FILES:
        return WORD_RE.sub(_case, rel)
    return rel


# --------------------------------------------------------------------------------------------------------------
# the hand patches: the few sites the generic rules cannot decide
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Patch:
    name: str
    file: str            # the OLD path
    old: str
    new: str
    imports: tuple[str, ...] = ()


PATCHES = [
    Patch("R5 the desk directory resolver", "levain/coordinator/launch.py",
          '    home = os.environ.get("LEVAIN_HOME")\n    return (Path(home).expanduser() if home else Path.home() / ".levain") / "coordinator"\n',
          "    return desk_home()\n", ("desk_home",)),
    Patch("R5 the floor names both home directory names", "levain/firing/confinement.py",
          '_ssh_agent.HOST_KEYS_DIR, "coordinator", "chat-node", "observe")',
          '_ssh_agent.HOST_KEYS_DIR, LEGACY_HOME_NAME, DESK_HOME_NAME, "chat-node", "observe")',
          ("LEGACY_HOME_NAME", "DESK_HOME_NAME")),
    Patch("R6 the codex rule keeps naming the long CLI alias", "levain/coordinator/launch.py",
          "[*base, 'coordinator']", "[*base, CLI_ALIAS_LONG]", ("CLI_ALIAS_LONG",)),
    Patch("R6 the desk verb and its aliases", "levain/coordinator/cli.py",
          '        "coord", help="The coordinator lease: who holds the coordinator role, and the sessions launched for it.",\n',
          '        "desk", aliases=[CLI_ALIAS_SHORT, CLI_ALIAS_LONG],\n'
          '        epilog=f"`levain {CLI_ALIAS_SHORT}` and `levain {CLI_ALIAS_LONG}` are aliases of `levain desk`.",\n'
          '        help="The desk lease: who holds the desk role, and the sessions launched for it.",\n',
          ("CLI_ALIAS_SHORT", "CLI_ALIAS_LONG")),
    Patch("R6 the aliases stay out of the verb list", "levain/coordinator/cli.py",
          '    c_sub = c.add_subparsers(dest="coord_cmd", required=True)\n',
          '    for _act in getattr(subparsers, "_choices_actions", []):   # argparse lists the aliases beside the name: list `desk` only\n'
          '        if getattr(_act, "dest", None) == "desk":\n'
          '            _act.metavar = "desk"\n'
          '    c_sub = c.add_subparsers(dest="coord_cmd", required=True)\n'),
]


# --------------------------------------------------------------------------------------------------------------
# the files codemod creates
# --------------------------------------------------------------------------------------------------------------

LEGACY_SOURCE = '''\
"""levain.desk.legacy: the ONE module that spells the desk's old name, "coordinator", as code.

The desk was the "coordinator" until the rename. What was stored or put on a wire under that name keeps its spelling,
because SQLite cannot alter a CHECK constraint in place and a store is append-only: the persisted role value, the CHECK
literals, the quoted SQL text, the stored reply kind, the on-disk file and directory names and the signed broker verb.
Everything that names one of them does it through a constant here, so the old word appears as code in this file and
in the import shim ``levain/coordinator/__init__.py`` and nowhere else. A test greps the package for it.

``desk_home()`` is the one resolver of the desk's home directory: ``<levain home>/desk`` when it exists, else the old
``<levain home>/coordinator`` when that exists, else the new name (a new install).
"""
from __future__ import annotations

import os
from pathlib import Path

ROLE_DESK = "coordinator"                      # the persisted role / by / mode / author_kind value
SQL_ROLE_DESK = "'coordinator'"                # the same, as a quoted SQL literal inside schema text
JSON_ROLE_DESK = \'"coordinator"\'                # the same, as a JSON string inside a JSON text
CLI_ALIAS_LONG = "coordinator"                 # the long alias of `levain desk` (argv)
CLI_ALIAS_SHORT = "coord"                      # the short alias of `levain desk` (argv)
LEGACY_HOME_NAME = "coordinator"               # ~/.levain/coordinator
DESK_HOME_NAME = "desk"                        # ~/.levain/desk, the new install's name
LEGACY_SHADOW_NAME = "coordinator-shadow"      # ~/.levain/coordinator-shadow
KIND_DESK_REPLY = "coordinator_reply"          # replies.kind
KIND_REPLY_DESK = "reply_coordinator"          # the thread view's label key for it
SQL_KIND_DESK_REPLY = "'coordinator_reply'"
SQL_KIND_REPLY_DESK = "'reply_coordinator'"
FACT_DESK_WAKE = "coordinator-wake"            # the turn fact kind of a wake turn (written to the turn record)
VERB_DESK_DROP = "coordinator_drop"            # the signed broker effect verb
RECORD_NAME_WORKSPACES = "coordinator_workspaces.json"   # consent/<file>: the door's workspace record
RECORD_KEY_WORKSPACES = "coordinator_workspaces"
BAND_KEY_DESK_STOPPED = "levain-coordinator-stopped"
CODEX_RULE_NAME = "levain-coordinator.rules"   # <cwd>/.codex/rules/<file>
SESSION_PREFIX_DESK = "levain-coordinator-"    # session_name() of a desk launch: levain-<role>-<id>
SESSION_ROLE_ALT = "(coordinator|worker)"
DRAFT_AGENT_PREFIX = "coordinator-draft:"      # the ledger entry's agent field for a promoted team draft
CAUSE_CODEX_REMOVED = "codex coordinator removed"   # a launch's persisted end cause (lease_events)


def desk_home(levain_home: Path | str | None = None, *, create: bool = False) -> Path:
    """The desk's home directory: the new name if it exists, else the old one if it exists, else the new name.

    ``levain_home`` defaults to ``$LEVAIN_HOME`` else ``~/.levain``. Nothing is created unless ``create`` is true: a
    read-only verb that asks where the desk lives must not make the directory."""
    if levain_home is None:
        env = os.environ.get("LEVAIN_HOME")
        base = Path(env).expanduser() if env else Path.home() / ".levain"
    else:
        base = Path(levain_home).expanduser()
    new, old = base / DESK_HOME_NAME, base / LEGACY_HOME_NAME
    if new.exists():
        return new
    if old.exists():
        return old
    if create:
        new.mkdir(parents=True, exist_ok=True)
    return new
'''

SHIM_SOURCE = '''\
"""levain.coordinator: the old import path of :mod:`levain.desk`, kept for the 1.x line.

``import levain.coordinator`` and ``from levain.coordinator.room_host import HostCommands`` keep working: this
package re-exports ``levain.desk``, and a meta-path finder resolves ``levain.coordinator.<anything>`` to the very same
module object as ``levain.desk.<anything>`` (so ``levain.coordinator.x.Y is levain.desk.x.Y``). One of the two files
allowed to hold the old word as code (the other is ``levain/desk/legacy.py``).
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

import levain.desk as _desk

_OLD = __name__ + "."
_NEW = "levain.desk."


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real: str) -> None:
        self._real = real

    def create_module(self, spec):
        return importlib.import_module(self._real)

    def exec_module(self, module) -> None:        # the real module is already executed
        return None


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD):
            return None
        real = _NEW + fullname[len(_OLD):]
        try:
            if importlib.util.find_spec(real) is None:
                return None
        except (ImportError, ValueError):
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name: str):
    return getattr(_desk, name)
'''


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    root: Path
    moves: list[tuple[str, str]] = field(default_factory=list)
    writes: dict[str, str] = field(default_factory=dict)       # NEW path -> new text
    old_of: dict[str, str] = field(default_factory=dict)       # NEW path -> OLD path
    creates: dict[str, str] = field(default_factory=dict)
    collisions: list[str] = field(default_factory=list)
    stored: list[tuple[str, int, str, str]] = field(default_factory=list)
    manual: list[tuple[str, int, str]] = field(default_factory=list)
    review: list[tuple[str, int, str]] = field(default_factory=list)
    left_wire: list[str] = field(default_factory=list)
    glitches: list[str] = field(default_factory=list)
    patch_misses: list[str] = field(default_factory=list)
    patched: list[str] = field(default_factory=list)
    word_hits_by_rule: dict[str, int] = field(default_factory=dict)
    files_by_rule: dict[str, list[str]] = field(default_factory=dict)
    changelog: str | None = None
    unhandled: list[str] = field(default_factory=list)


def list_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return [p for p in out.decode("utf-8").split("\0") if p and (root / p).is_file()]
    files: list[str] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for nm in names:
            files.append(str(Path(dirpath, nm).relative_to(root)))
    return sorted(files)


def _read(root: Path, rel: str) -> str | None:
    p = root / rel
    if p.suffix not in TEXT_SUFFIXES:
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _add_import(text: str, names: tuple[str, ...]) -> str:
    """Add ``from levain.desk.legacy import names`` after the import block, merging into an existing legacy import."""
    have = re.search(r"^from levain\.desk\.legacy import ([^\n(]+)$", text, re.M)
    if have:
        cur = {s.strip() for s in have.group(1).split(",")}
        merged = ", ".join(sorted(cur | set(names)))
        return text[:have.start()] + f"from levain.desk.legacy import {merged}" + text[have.end():]
    line = _insertion_line(text)
    stmt = f"from levain.desk.legacy import {', '.join(sorted(names))}\n"
    lines = _lines(text)
    if line == 0 or line > len(lines):
        lines.append(stmt)
    else:
        lines.insert(line, stmt)
    return "".join(lines)


def build_plan(root: Path) -> Plan:
    plan = Plan(root=root)
    files = list_files(root)
    fileset = set(files)
    renamed_already = (root / "levain/desk/__init__.py").exists()

    # R1: moves, with collisions
    targets: dict[str, str] = {}
    for rel in files:
        if rel == SHIM_PATH and renamed_already:
            continue
        new = path_rename(rel)
        if new == rel:
            continue
        if new in fileset or new in targets:
            plan.collisions.append(f"{rel} -> {new} (the target exists)")
            continue
        targets[new] = rel
        plan.moves.append((rel, new))
    move_of = dict(plan.moves)

    def patches_for(rel: str) -> list[Patch]:
        return [p for p in PATCHES if p.file == rel]

    # R2/R4/R9: content
    counts: dict[str, int] = {}
    by_rule: dict[str, list[str]] = {}
    for rel in files:
        if rel in EXEMPT_FILES or any(rel.startswith(p) for p in EXEMPT_PREFIXES) and rel != GOLDEN_NAMESPACES:
            continue
        if rel == SHIM_PATH and renamed_already:
            continue
        text = _read(root, rel)
        if text is None:
            continue
        new_rel = move_of.get(rel, rel)
        if rel == GOLDEN_NAMESPACES:
            new_text = text.replace("levain/coordinator/", "levain/desk/")
            if new_text != text:
                plan.writes[new_rel] = new_text
                plan.old_of[new_rel] = rel
                by_rule.setdefault("R8 contract owner paths", []).append(rel)
            continue
        if not WORD_RE.search(text) and not any(rx.search(text) for rx, _ in MODULE_RENAMES) and not patches_for(rel) \
                and not CLI_PHRASE_RE.search(text):
            continue
        extra_imports: set[str] = set()
        for p in patches_for(rel):
            if text.count(p.old) != 1:
                plan.patch_misses.append(f"{p.name}: anchor in {rel} found {text.count(p.old)} times (wanted 1)")
                continue
            text = text.replace(p.old, p.new)
            extra_imports |= set(p.imports)
            plan.patched.append(f"{p.name} ({rel})")
        before_hits = len(WORD_RE.findall(text))
        if rel.endswith(".py"):
            res = transform_python(rel, text)
            new_text = res.text
            for ln, ex, why in res.stored:
                plan.stored.append((rel, ln, ex, why))
            for ln, why in res.manual:
                plan.manual.append((rel, ln, why))
            for ln, ex in res.review:
                plan.review.append((rel, ln, ex))
            if extra_imports:
                new_text = _add_import(new_text, tuple(sorted(extra_imports)))
        else:
            new_text, left = transform_text(rel, text)
            plan.left_wire += left
        if rel == "README.md" or rel == "docs/contracts.md" or rel.startswith("docs/") or rel.endswith(".md"):
            rule = "R9 docs"
        elif rel.startswith("levain/templates/"):
            rule = "R9 templates"
        elif rel.startswith("tests/"):
            rule = "R2 tests"
        else:
            rule = "R2 code and prose"
        if new_text != text or new_rel != rel:
            counts[rule] = counts.get(rule, 0) + before_hits
            by_rule.setdefault(rule, []).append(rel)
            plan.writes[new_rel] = new_text
            plan.old_of[new_rel] = rel
            plan.glitches += find_glitches(new_text, new_rel)
        elif new_rel != rel:
            plan.writes[new_rel] = new_text
            plan.old_of[new_rel] = rel
    plan.word_hits_by_rule = counts
    plan.files_by_rule = by_rule
    plan.files_by_rule["R1 moves"] = [f"{a} -> {b}" for a, b in plan.moves]

    # files that move but had no content change are still carried by the move itself.
    # created files
    if not (root / LEGACY_PATH).exists():
        plan.creates[LEGACY_PATH] = LEGACY_SOURCE
    if not (root / SHIM_PATH).exists() or not renamed_already:
        plan.creates[SHIM_PATH] = SHIM_SOURCE
    # the changelog
    cl = root / "CHANGELOG.md"
    if cl.exists():
        text = cl.read_text(encoding="utf-8")
        if CHANGELOG_SENTINEL not in text:
            m = re.search(r"^## \[Unreleased\]\n+### Changed\n", text, re.M)
            if m:
                plan.changelog = text[:m.end()] + CHANGELOG_ENTRY + text[m.end():]
            else:
                plan.unhandled.append("CHANGELOG.md has no `## [Unreleased]` / `### Changed` heading to add the entry under")
    return plan


def apply_plan(plan: Plan) -> None:
    root = plan.root
    git = (root / ".git").exists()
    for old, new in plan.moves:
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        if git:
            r = subprocess.run(["git", "-C", str(root), "mv", old, new], capture_output=True, text=True)
            if r.returncode == 0:
                continue
        os.replace(root / old, root / new)
    for new, text in plan.writes.items():
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        (root / new).write_text(text, encoding="utf-8")
    for rel, text in plan.creates.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    if plan.changelog is not None:
        (root / "CHANGELOG.md").write_text(plan.changelog, encoding="utf-8")


def done_check(root: Path) -> list[str]:
    """The plan's DONE check 1: files under levain/ that still hold the word, bar the two allowed."""
    bad = []
    for dirpath, dirs, names in os.walk(root / "levain"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for nm in names:
            rel = str(Path(dirpath, nm).relative_to(root))
            if rel in (LEGACY_PATH, SHIM_PATH):
                continue
            text = _read(root, rel)
            if text is not None and DONE_RE.search(text):
                bad.append(rel)
    return sorted(bad)


R3_PATTERNS = {
    "Coord* class names": re.compile(r"\bCoord[A-Z][A-Za-z0-9]*"),
    "coord_* / _coord_* identifiers": re.compile(r"\b_?coord_[a-z0-9_]+|\b_?COORD_[A-Z0-9_]+"),
    "coordinate(s)/coordinated": re.compile(r"\bcoordinat(?:e|es|ed|ing)\b", re.I),
    "coordroom": re.compile(r"coordroom", re.I),
}


def r3_counts(root: Path) -> dict[str, tuple[int, int, list[str]]]:
    """(occurrences, files, distinct names) of what the vocabulary leaves alone."""
    out: dict[str, list] = {k: [0, set(), set()] for k in R3_PATTERNS}
    for rel in list_files(root):
        if not rel.startswith(("levain/", "tests/")) or rel.startswith(("tests/fixtures", "tests/golden")):
            continue
        text = _read(root, rel)
        if text is None:
            continue
        for k, rx in R3_PATTERNS.items():
            found = rx.findall(text)
            if found:
                out[k][0] += len(found)
                out[k][1].add(rel)
                out[k][2].update(found)
    return {k: (v[0], len(v[1]), sorted(v[2])) for k, v in out.items()}


def scan_sites(root: Path) -> list[str]:
    """Where the cockpit `/coord/` routes and the `"coord"` argv spelling live (the result tree's own lines)."""
    rx = re.compile(r"""/coord/|/coord['"]|['"]coord['"]""")
    out = []
    for dirpath, dirs, names in os.walk(root / "levain"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for nm in sorted(names):
            if not nm.endswith((".py", ".js", ".html")):
                continue
            rel = str(Path(dirpath, nm).relative_to(root))
            text = _read(root, rel) or ""
            for n, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    out.append(f"{rel}:{n}: {line.strip()[:110]}")
    return sorted(out)


def render_report(plan: Plan, *, applied: bool, root: Path) -> str:
    L: list[str] = []
    w = L.append
    w(f"# rename_desk report ({'APPLIED' if applied else 'DRY RUN'}), root {root}")
    w("")
    if plan.collisions:
        w("## REFUSED: name collisions (nothing was changed)")
        L.extend(f"- {c}" for c in plan.collisions)
        w("")
    w("## per rule")
    w(f"- R1 moves: {len(plan.moves)} files")
    for rule, n in sorted(plan.word_hits_by_rule.items()):
        w(f"- {rule}: {n} word hits in {len(plan.files_by_rule.get(rule, []))} files")
    w(f"- R8 contract: {len(plan.files_by_rule.get('R8 contract owner paths', []))} golden file; the namespace strings never change")
    w(f"- created: {', '.join(plan.creates) or 'nothing (already present)'}")
    w(f"- CHANGELOG entry: {'added' if plan.changelog is not None else 'present or not added'}")
    w("")
    w("## hand patches")
    L.extend(f"- {p}" for p in plan.patched)
    L.extend(f"- MISS {p}" for p in plan.patch_misses)
    w("")
    w(f"## R4: string literals classified STORED ({len(plan.stored)}): file:line, text, reason")
    for rel, ln, ex, why in plan.stored:
        w(f"- {rel}:{ln}  {ex!r}  -- {why}")
    w("")
    w(f"## stored spellings left in templates and docs (no import there) ({len(plan.left_wire)})")
    L.extend(f"- {x}" for x in plan.left_wire)
    w("")
    w(f"## REVIEW: prose literals that quote the word ({len(plan.review)}); renamed, check each is not stored")
    for rel, ln, ex in plan.review:
        w(f"- {rel}:{ln}  {ex!r}")
    w("")
    w(f"## MANUAL: needs a hand ({len(plan.manual)})")
    for rel, ln, why in plan.manual:
        w(f"- {rel}:{ln}: {why}")
    L.extend(f"- {x}" for x in plan.unhandled)
    w("")
    w(f"## grammar glitches the substitution made ({len(plan.glitches)}); list, never silently left")
    L.extend(f"- {g}" for g in plan.glitches)
    w("")
    w("## R5: wire and persisted names routed through levain/desk/legacy.py (besides the role value and its SQL spelling)")
    other = [(rel, ln, ex, why) for rel, ln, ex, why in plan.stored if not re.search(r"-> (ROLE_DESK|SQL_ROLE_DESK)$", why)]
    L.extend(f"- {rel}:{ln}  {ex!r}  -- {why}" for rel, ln, ex, why in other)
    w("")
    w("## route table and argv sites that STAY (R5; `/desk/...` aliases are not in this codemod)")
    L.extend(f"- {x}" for x in scan_sites(root))
    w("")
    w("## R3: what the vocabulary left (identifier count, files)")
    for k, (n, f, names) in r3_counts(root).items():
        w(f"- {k}: {n} occurrences in {f} files, {len(names)} distinct")
        w("  " + ", ".join(names[:70]) + (" ..." if len(names) > 70 else ""))
    w("")
    w("## regenerate the frozen contract golden")
    w("- tests/golden/contracts/frozen/namespaces_gen.py does not exist in the tree (the golden names it as its generator); the golden's three")
    w("  `owner` strings are rewritten by this codemod, and `tests/test_contracts_namespaces.py::test_the_registry_equals_the_frozen_golden` is the proof.")
    w("")
    bad = done_check(root) if applied else []
    w("## DONE check (applied trees only): `git grep -l -w -i coordinator -- levain | grep -v -E 'levain/desk/legacy.py|levain/coordinator/__init__.py'`")
    if applied:
        L.extend(f"- {b}" for b in bad) if bad else w("- prints nothing")
    else:
        w("- (dry run: not evaluated)")
    return "\n".join(L) + "\n"


def default_root() -> Path:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
        return Path(top)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--root", type=Path, default=None, help="the tree to rename (default: the git top level)")
    ap.add_argument("--report", type=Path, default=None, help="also write the report to this file")
    args = ap.parse_args(argv)
    root = (args.root or default_root()).resolve()
    plan = build_plan(root)
    if plan.collisions:
        report = render_report(plan, applied=False, root=root)
        sys.stdout.write(report)
        if args.report:
            args.report.write_text(report, encoding="utf-8")
        return 2
    if args.apply:
        apply_plan(plan)
    report = render_report(plan, applied=args.apply, root=root)
    sys.stdout.write(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
