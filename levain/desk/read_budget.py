"""levain.desk.read_budget — a position-aware read budget, enforced by hooks (levain row 1686, row 800).

A big read costs what it weighs TIMES the turns it will ride along in context. ``PreToolUse`` prices a Read before it runs:
``price = estimated tokens x remaining turns``. Over the per-call ceiling, or over what the session's ledger has left, it
DENIES with a deterministic outline of the file (each heading or definition with its ``start-end`` lines, the total bytes
and lines) and the exact ``offset``/``limit`` retry that fits. ``PostToolUse`` charges the session ledger with the bytes
that really entered context, so the next big read is priced against what is already spent.

Keyed on bytes, never on a command name or a line-count threshold. ``allow_large`` is a priced read, not an escape. The one
escape is a desk-granted per-path entry in the grants file (the seat cannot grant itself in its own reason text; see the
residue in the row's return: a seat with write access to the state directory could). Reads that set ``offset``/``limit`` are
priced on the slice they ask for and usually pass.

FAIL-OPEN: an unparseable event, a missing field, any exception prints nothing and exits 0; the vendor decides. It never
exits 2. Standard library only and self-contained: ``python -I read_budget.py pre|post`` (the launcher copies the file).

Settings: :func:`settings_hooks` builds the ``hooks`` object (a PreToolUse and a PostToolUse entry, matcher ``Read``).
Wiring it into a seat's settings is the launcher's job and is NOT done here.

Environment (all optional): ``LEVAIN_READ_BUDGET_DIR`` (state directory: the ledgers and ``grants.json``; none = inert),
``LEVAIN_READ_BUDGET_CALL`` (per-call ceiling in token-turns), ``LEVAIN_READ_BUDGET_SESSION`` (per-session total),
``LEVAIN_READ_BUDGET_TURNS`` (the seat's turn cap; remaining = cap minus charged calls, at least 1).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

__all__ = ["BYTES_PER_TOKEN", "DENY_PREFIX", "HOOK_MATCHER", "STATE_ENV", "charge", "decide_pre", "main", "outline",
           "price", "retry_for", "settings_hooks"]

BYTES_PER_TOKEN = 4
DEFAULT_CALL = 100_000          # token-turns one Read may cost (~3.3K tokens ridden for 30 turns)
DEFAULT_SESSION = 1_000_000     # token-turns a session's reads may cost in all
DEFAULT_TURNS = 40
READ_DEFAULT_LINES = 2000       # what an unscoped Read returns at most
SCAN_CAP = 8_000_000            # bytes read to count lines; past it the size is stat's and lines are estimated
OUTLINE_MAX = 60
HOOK_MATCHER = "Read"
STATE_ENV = "LEVAIN_READ_BUDGET_DIR"
DENY_PREFIX = "READ BUDGET: "


# ---- pricing -----------------------------------------------------------------------------------------------------
def _env_int(environ: Mapping[str, str], key: str, default: int) -> int:
    try:
        v = int(environ.get(key, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _scan(path: str) -> tuple[list[bytes], int]:
    """(the file's lines as bytes, its size). Lines are empty past SCAN_CAP (size is then stat's)."""
    size = os.stat(path).st_size
    if size > SCAN_CAP:
        return [], size
    with open(path, "rb") as fh:
        return fh.read().splitlines(keepends=True), size


def slice_bytes(path: str, offset: int | None, limit: int | None) -> tuple[int, int, int]:
    """(bytes the Read would return, first line (1-based), lines returned). Unscoped = the first READ_DEFAULT_LINES."""
    lines, size = _scan(path)
    start = max(1, offset or 1)
    n = limit if limit else READ_DEFAULT_LINES
    if not lines:                                    # too big to scan: bytes are priced as the whole file
        return size, start, n
    chosen = lines[start - 1:start - 1 + n]
    return sum(len(x) for x in chosen), start, len(chosen)


def price(nbytes: int, remaining_turns: int) -> int:
    """Token-turns: estimated tokens (bytes / 4, rounded up) x the turns the result will ride in context."""
    return -(-max(0, nbytes) // BYTES_PER_TOKEN) * max(1, remaining_turns)


# ---- outline -----------------------------------------------------------------------------------------------------
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)")
_JS_OPEN = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+\w+|class\s+\w+|"
                      r"(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)")
_SH_OPEN = re.compile(r"^(?:function\s+)?\w+\s*\(\)\s*\{|^function\s+\w+")


def _py_outline(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []

    def walk(body: list[ast.stmt], depth: int) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(n, ast.ClassDef) else "def"
                out.append((n.lineno, n.end_lineno or n.lineno, f"{'  ' * depth}{kind} {n.name}"))
                if isinstance(n, ast.ClassDef):
                    walk(n.body, depth + 1)

    walk(ast.parse(text).body, 0)
    return out


def _md_outline(lines: list[str]) -> list[tuple[int, int, str]]:
    heads: list[tuple[int, int, str]] = []
    fence = False
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else _MD_HEAD.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for k, (start, lvl, title) in enumerate(heads):
        end = next((h[0] - 1 for h in heads[k + 1:] if h[1] <= lvl), len(lines))
        out.append((start, end, f"{'  ' * (lvl - 1)}{'#' * lvl} {title}"))
    return out


def _opener_outline(lines: list[str], rx: re.Pattern[str]) -> list[tuple[int, int, str]]:
    starts = [i for i, ln in enumerate(lines, 1) if rx.match(ln)]
    return [(s, (starts[k + 1] - 1 if k + 1 < len(starts) else len(lines)), lines[s - 1].strip()[:80])
            for k, s in enumerate(starts)]


def outline(path: str) -> dict[str, Any]:
    """The deterministic outline: ``{"bytes", "lines", "kind", "entries": [(start, end, label)], "approx": bool}``.
    Python by ``ast``, Markdown by headings, JS/TS/shell by openers (approximate), JSON by top-level keys, else the
    first 20 lines. Never raises on content it cannot parse: it falls back to the generic head."""
    raw, size = _scan(path)
    text = b"".join(raw).decode("utf-8", "replace")
    lines = text.splitlines()
    ext = os.path.splitext(path)[1].lower()
    entries: list[tuple[int, int, str]] = []
    kind, approx = "head", False
    try:
        if ext == ".py":
            entries, kind = _py_outline(text), "python"
        elif ext in (".md", ".markdown"):
            entries, kind = _md_outline(lines), "markdown"
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            entries, kind, approx = _opener_outline(lines, _JS_OPEN), "js", True
        elif ext in (".sh", ".bash", ".zsh"):
            entries, kind, approx = _opener_outline(lines, _SH_OPEN), "shell", True
        elif ext == ".json":
            top = json.loads(text)
            if isinstance(top, dict):
                kind, entries = "json", [(1, len(lines), f"key {k!r}") for k in list(top)[:OUTLINE_MAX]]
    except Exception:  # noqa: BLE001  (a file that will not parse gets the head)
        entries, kind = [], "head"
    if not entries:
        kind = "head"
        entries = [(i, i, ln[:100]) for i, ln in enumerate(lines[:20], 1)]
    return {"bytes": size, "lines": len(lines), "kind": kind, "entries": entries, "approx": approx}


def retry_for(path: str, start: int, bytes_per_line: float, call_budget: int, remaining: int) -> tuple[int, int]:
    """(offset, limit) of the largest slice from ``start`` that prices within the per-call ceiling (at least 1 line)."""
    fits_bytes = (call_budget // max(1, remaining)) * BYTES_PER_TOKEN
    return max(1, start), max(1, int(fits_bytes // max(1.0, bytes_per_line)))


def _deny_text(path: str, nbytes: int, cost: int, call_budget: int, why: str, remaining: int) -> str:
    o = outline(path)
    bpl = o["bytes"] / max(1, o["lines"])
    offset, limit = retry_for(path, 1, bpl, call_budget, remaining)
    shown = o["entries"][:OUTLINE_MAX]
    body = "\n".join(f"  {s}-{e}  {label}" for s, e, label in shown)
    more = f"\n  ... {len(o['entries']) - OUTLINE_MAX} more" if len(o["entries"]) > OUTLINE_MAX else ""
    note = " (starts approximate)" if o["approx"] else ""
    return (f"{DENY_PREFIX}this read would cost ~{cost} token-turns ({nbytes} bytes x {remaining} turns left), "
            f"{why}. {path}: {o['bytes']} bytes, {o['lines']} lines; outline by {o['kind']}{note}:\n{body}{more}\n"
            f"Retry with a range, for example Read file_path={path!r} offset={offset} limit={limit}, "
            f"or pick a section above and read its start-end lines. A desk-granted path reads whole.")


# ---- ledger and grants ---------------------------------------------------------------------------------------------
def _ledger_path(state: str, session_id: str) -> str:
    return os.path.join(state, "ledger-" + hashlib.sha256(session_id.encode()).hexdigest()[:24] + ".jsonl")


def _rows(state: str, session_id: str) -> list[dict[str, Any]]:
    try:
        with open(_ledger_path(state, session_id), encoding="utf-8") as fh:
            return [r for r in (json.loads(x) for x in fh if x.strip()) if isinstance(r, dict)]
    except (OSError, ValueError):
        return []


def charge(state: str, session_id: str, *, tool: str, path: str, nbytes: int, remaining: int) -> int:
    """Append one row to the session's ledger and return its token-turn price."""
    cost = price(nbytes, remaining)
    os.makedirs(state, mode=0o700, exist_ok=True)
    with open(_ledger_path(state, session_id), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": int(time.time()), "tool": tool, "path": path, "bytes": nbytes, "price": cost}) + "\n")
    return cost


def _granted(state: str, real: str, now: float) -> bool:
    """``grants.json``: ``{"paths": {"<abs realpath>": <expires unix seconds or null>}}``, the desk's to write."""
    try:
        with open(os.path.join(state, "grants.json"), encoding="utf-8") as fh:
            paths = json.load(fh)["paths"]
        exp = paths[real]
        return exp is None or (isinstance(exp, (int, float)) and exp > now)
    except (OSError, ValueError, KeyError, TypeError):
        return False


# ---- the two hooks -----------------------------------------------------------------------------------------------
def _target(event: Any) -> tuple[str, Mapping[str, Any], str, str] | None:
    if not isinstance(event, Mapping) or event.get("tool_name") != "Read":
        return None
    ti, sid, cwd = event.get("tool_input"), event.get("session_id"), event.get("cwd")
    if not isinstance(ti, Mapping) or not isinstance(sid, str) or not sid:
        return None
    given = ti.get("file_path")
    if not isinstance(given, str) or not given.strip() or "\x00" in given:
        return None
    p = os.path.expanduser(given)
    if not os.path.isabs(p):
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        p = os.path.join(cwd, p)
    return os.path.realpath(p), ti, sid, p


def _remaining(environ: Mapping[str, str], rows: list[dict[str, Any]]) -> int:
    return max(1, _env_int(environ, "LEVAIN_READ_BUDGET_TURNS", DEFAULT_TURNS) - len(rows))


def decide_pre(event: Any, environ: Mapping[str, str], now: float | None = None) -> str | None:
    """The deny text for a Read priced over budget, else None (allow, silently). ``allow_large`` changes nothing here."""
    try:
        state = environ.get(STATE_ENV)
        t = _target(event)
        if not state or not os.path.isabs(state) or t is None:
            return None
        real, ti, sid, _ = t
        if not os.path.isfile(real) or _granted(state, real, time.time() if now is None else now):
            return None
        offset, limit = _int(ti.get("offset")), _int(ti.get("limit"))
        nbytes, _start, _n = slice_bytes(real, offset, limit)
        rows = _rows(state, sid)
        remaining = _remaining(environ, rows)
        cost = price(nbytes, remaining)
        call_budget = _env_int(environ, "LEVAIN_READ_BUDGET_CALL", DEFAULT_CALL)
        left = _env_int(environ, "LEVAIN_READ_BUDGET_SESSION", DEFAULT_SESSION) - sum(
            r["price"] for r in rows if isinstance(r.get("price"), int))
        if cost > call_budget:
            why = f"over the per-call ceiling of {call_budget}"
        elif cost > left:
            why = f"over the {max(0, left)} the session has left"
            call_budget = max(1, min(call_budget, left))
        else:
            return None
        return _deny_text(real, nbytes, cost, call_budget, why, remaining)
    except Exception:  # noqa: BLE001  (any doubt: the vendor decides)
        return None


def _response_bytes(resp: Any) -> int:
    if isinstance(resp, str):
        return len(resp.encode("utf-8", "replace"))
    return len(json.dumps(resp, ensure_ascii=False, default=str).encode("utf-8", "replace")) if resp is not None else 0


def decide_post(event: Any, environ: Mapping[str, str]) -> str | None:
    """Charge the ledger with what entered context; the note for the model when the charge was heavy, else None."""
    try:
        state = environ.get(STATE_ENV)
        t = _target(event)
        if not state or not os.path.isabs(state) or t is None:
            return None
        real, _ti, sid, _ = t
        remaining = _remaining(environ, _rows(state, sid))
        nbytes = _response_bytes(event.get("tool_response"))
        cost = charge(state, sid, tool="Read", path=real, nbytes=nbytes, remaining=remaining)
        if cost > _env_int(environ, "LEVAIN_READ_BUDGET_CALL", DEFAULT_CALL):
            return f"{DENY_PREFIX}that read cost ~{cost} token-turns ({nbytes} bytes x {remaining} turns left)."
    except Exception:  # noqa: BLE001
        pass
    return None


def settings_hooks(python: str, script: str) -> dict[str, Any]:
    """The ``hooks`` object for a seat's settings: PreToolUse and PostToolUse on :data:`HOOK_MATCHER`."""
    if not os.path.isabs(python) or not os.path.isabs(script):
        raise ValueError("the hook's python and script must be absolute paths")
    base = f"{shlex.quote(python)} -I {shlex.quote(script)}"
    return {ev: [{"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": f"{base} {arg}"}]}]
            for ev, arg in (("PreToolUse", "pre"), ("PostToolUse", "post"))}


def main(argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None,
         environ: Mapping[str, str] | None = None) -> int:
    """``pre`` or ``post``: read one event, print the decision or note, return 0 in every case."""
    try:
        args = sys.argv[1:] if argv is None else argv
        env = os.environ if environ is None else environ
        event = json.loads((stdin or sys.stdin).read())
        out = stdout or sys.stdout
        if args[:1] == ["pre"]:
            reason = decide_pre(event, env)
            if reason is not None:
                out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                             "permissionDecision": "deny",
                                                             "permissionDecisionReason": reason}}))
        elif args[:1] == ["post"]:
            note = decide_post(event, env)
            if note is not None:
                out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                             "additionalContext": note}}))
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
