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
import stat
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

__all__ = ["BYTES_PER_TOKEN", "DENY_PREFIX", "HOOK_MATCHER", "HOOK_TIMEOUT_S", "STATE_ENV", "charge", "decide_pre", "main", "outline",
           "price", "settings_hooks", "slice_bytes"]

BYTES_PER_TOKEN = 4
DEFAULT_CALL = 100_000          # token-turns one Read may cost (2.5K tokens ridden for the 40 default turns)
DEFAULT_SESSION = 1_000_000     # token-turns a session's reads may cost in all
DEFAULT_TURNS = 40
READ_DEFAULT_LINES = 2000       # what an unscoped Read returns at most
SCAN_CAP = 8_000_000            # bytes read to count lines; past it the size is stat's and lines are estimated
OUTLINE_MAX = 60
LABEL_MAX = 100                 # a heading or key shown in the outline is cut to this many characters
LINE_PREFIX_BYTES = 8           # ESTIMATE: Read renders each line as a right-aligned 6-wide number plus a separator ("     1" then
#                                 a tab, as seen in a local Read result; an arrow separator is 3 UTF-8 bytes), so 7 to 9 bytes a line
FLOOR_BYTES = 4096              # a Read priced at or under this many bytes ALWAYS passes (a seat is never wedged)
NONTEXT_EXT = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".pdf"))
HOOK_MATCHER = "Read"
HOOK_TIMEOUT_S = 10          # the hook's own bound, seconds (the harness default is 60)
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
    """(the file's lines as bytes, split on \\n only as Read does, its size). Lines are empty past SCAN_CAP."""
    size = os.stat(path).st_size
    if size > SCAN_CAP:
        return [], size
    with open(path, "rb") as fh:
        return fh.read().split(b"\n"), size


def is_nontext(path: str) -> bool:
    """An image or PDF by extension, or any file with a NUL byte in its first 8K: Read returns it as a picture or blob."""
    if os.path.splitext(path)[1].lower() in NONTEXT_EXT:
        return True
    with open(path, "rb") as fh:
        return b"\x00" in fh.read(8192)


def _lines(path: str):
    """Yield (line bytes with its newline, bytes scanned so far); stop after SCAN_CAP bytes. A line is cut at the cap."""
    scanned = 0
    with open(path, "rb") as fh:
        while scanned <= SCAN_CAP:
            ln = fh.readline(SCAN_CAP - scanned + 1)
            if not ln:
                return
            scanned += len(ln)
            yield ln, scanned


def slice_bytes(path: str, offset: int | None, limit: int | None, whole: bool = False) -> tuple[int, int, int]:
    """(file bytes of the lines the Read returns, first line (1-based), lines returned). The line-number prefixes Read
    adds are NOT in these bytes; the callers add LINE_PREFIX_BYTES per line. Unscoped = the first READ_DEFAULT_LINES, or the whole file when ``whole`` (allow_large).
    Streams, so a file over SCAN_CAP prices by its range; past the cap the rest is estimated from the average line."""
    size = os.stat(path).st_size
    start = max(1, offset or 1)
    want = None if (whole and not limit) else (limit or READ_DEFAULT_LINES)
    seen = got_n = got_b = skipped = scanned = 0
    for ln, scanned in _lines(path):
        seen += 1
        if seen < start:
            skipped += len(ln)
        else:
            got_n += 1
            got_b += len(ln)
            if want is not None and got_n >= want:
                return got_b, start, got_n
    if scanned < SCAN_CAP or scanned >= size:           # the whole file was walked
        return got_b, start, got_n
    avg = scanned / max(1, seen)                        # capped: estimate what lies past the cap
    if want is None:
        return max(got_b, size - skipped), start, got_n
    return int(got_b + (want - got_n) * avg), start, want


def _fit(path: str, start: int, max_bytes: int) -> tuple[int, int | None]:
    """(lines from ``start`` whose bytes stay within ``max_bytes``, bytes of the first line at ``start`` or None)."""
    n = used = seen = 0
    first: int | None = None
    for ln, _ in _lines(path):
        seen += 1
        if seen < start:
            continue
        if first is None:
            first = len(ln) + LINE_PREFIX_BYTES
        if used + len(ln) + LINE_PREFIX_BYTES > max_bytes:
            break
        used += len(ln) + LINE_PREFIX_BYTES
        n += 1
    return n, first


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
            heads.append((i, len(m.group(1)), m.group(2)[:LABEL_MAX]))
    out = []
    for k, (start, lvl, title) in enumerate(heads):
        end = next((h[0] - 1 for h in heads[k + 1:] if h[1] <= lvl), len(lines))
        out.append((start, end, f"{'  ' * (lvl - 1)}{'#' * lvl} {title}"))
    return out


def _opener_outline(lines: list[str], rx: re.Pattern[str]) -> list[tuple[int, int, str]]:
    starts = [i for i, ln in enumerate(lines, 1) if rx.match(ln)]
    return [(s, (starts[k + 1] - 1 if k + 1 < len(starts) else len(lines)), lines[s - 1].strip()[:LABEL_MAX])
            for k, s in enumerate(starts)]


def outline(path: str) -> dict[str, Any]:  # noqa: C901
    """The deterministic outline: ``{"bytes", "lines", "kind", "entries": [(start, end, label)], "approx": bool}``.
    Python by ``ast``, Markdown by headings, JS/TS/shell by openers (approximate), JSON by top-level keys, else the
    first 20 lines. Never raises on content it cannot parse: it falls back to the generic head."""
    lines_total = 0
    raw, size = _scan(path)
    est = not raw and size > 0                          # over SCAN_CAP: only the head is read
    if est:
        with open(path, "rb") as fh:
            raw = fh.read(65536).split(b"\n")
    text = b"\n".join(raw).decode("utf-8", "replace")
    lines = text.split("\n")                            # \\n only, as Read counts
    if lines and lines[-1] == "":
        lines.pop()
    if est:
        lines_total = int(size / max(1.0, len(text) / max(1, len(lines))))
    ext = os.path.splitext(path)[1].lower() if not est else ""
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
                kind, entries = "json", [(1, len(lines), f"key {k!r}"[:LABEL_MAX]) for k in list(top)[:OUTLINE_MAX]]
    except Exception:  # noqa: BLE001  (a file that will not parse gets the head)
        entries, kind = [], "head"
    if not entries:
        kind = "head"
        entries = [(i, i, ln[:LABEL_MAX]) for i, ln in enumerate(lines[:20], 1)]
    return {"bytes": size, "lines": lines_total if est else len(lines), "kind": kind, "entries": entries,
            "approx": approx, "est": est}


def _deny_text(path: str, nbytes: int, cost: int, max_bytes: int, why: str, remaining: int, offset: int | None,
               state: str) -> str:
    """``max_bytes`` is what one read may carry now (at least FLOOR_BYTES); the retry is the longest range that fits it.
    ``state`` is the budget state directory: the text names its ``grants.json`` as the operator's hand-edited escape."""
    grants = os.path.join(state, "grants.json")
    o = outline(path)
    start = offset or (o["entries"][0][0] if o["entries"] else 1)
    limit, first = _fit(path, start, max_bytes)
    if first is None:                                    # the asked start is past the end: begin at the top
        start = 1
        limit, first = _fit(path, 1, max_bytes)
    shown = o["entries"][:OUTLINE_MAX]
    body = "\n".join(f"  {s}-{e}  {label}" for s, e, label in shown)
    more = f"\n  ... {len(o['entries']) - OUTLINE_MAX} more" if len(o["entries"]) > OUTLINE_MAX else ""
    note = " (starts approximate)" if o["approx"] else ""
    nlines = f"about {o['lines']}" if o["est"] else str(o["lines"])
    head = (f"{DENY_PREFIX}this read would cost ~{cost} token-turns ({nbytes} bytes x {remaining} turns left), "
            f"{why}. {path}: {o['bytes']} bytes, {nlines} lines; outline by {o['kind']}{note}:\n{body}{more}\n")
    if first is None:                                    # nothing to read at all: an empty file or a range past its end
        return head + "The file has no lines to return (it is empty), so there is no range to retry."
    if limit < 1:
        return (head + f"Line {start} alone is {first} bytes, over the {max_bytes} one read may carry now, and Read cannot "
                f"return part of a line: no offset/limit can pass. To read it whole the operator adds this path to {grants} by hand "
                f"(no Levain command writes that file).")
    return (head + f"Retry with a range that fits, for example Read file_path={path!r} offset={start} limit={limit}, "
            f"or pick a section above and read part of its start-end lines (at most {max_bytes} bytes). "
            f"Only a path the operator has added to {grants} by hand reads whole (no Levain command writes that file).")


# ---- ledger and grants ---------------------------------------------------------------------------------------------
def _ledger_path(state: str, session_id: str) -> str:
    return os.path.join(state, "ledger-" + hashlib.sha256(session_id.encode()).hexdigest()[:24] + ".jsonl")


def _rows(state: str, session_id: str) -> list[dict[str, Any]]:
    try:
        with open(_ledger_path(state, session_id), encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for x in lines:                                      # a torn or garbled line is skipped alone, every good row stays
        try:
            r = json.loads(x) if x.strip() else None
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def charge(state: str, session_id: str, *, tool: str, path: str, nbytes: int, remaining: int) -> int:
    """Append one row to the session's ledger and return its token-turn price."""
    cost = price(nbytes, remaining)
    os.makedirs(state, mode=0o700, exist_ok=True)
    with open(_ledger_path(state, session_id), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": int(time.time()), "tool": tool, "path": path, "bytes": nbytes, "price": cost}) + "\n")
    return cost


def _denials_path(state: str, session_id: str) -> str:
    return os.path.join(state, "denials-" + hashlib.sha256(session_id.encode()).hexdigest()[:24] + ".jsonl")


def _log_denial(state: str, session_id: str, path: str, nbytes: int, cost: int, remaining: int,
                now: float | None = None) -> None:
    """Append one row per deny to the session's ``denials-<session>.jsonl``; fail-open (never changes the decision).
    One ``os.write`` of the whole line on an O_APPEND fd (0600, non-blocking, no symlink); a non-regular file is refused."""
    try:
        row = {"path": path, "bytes": nbytes, "price": cost, "remaining": remaining, "session": session_id,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        line = (json.dumps(row) + "\n").encode("utf-8")
        os.makedirs(state, mode=0o700, exist_ok=True)
        fd = os.open(_denials_path(state, session_id),
                     os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


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
        if is_nontext(real):                             # an image, a PDF or a blob: no outline to give, not priced by bytes
            return None
        offset, limit = _int(ti.get("offset")), _int(ti.get("limit"))
        nbytes, _start, _n = slice_bytes(real, offset, limit, whole=ti.get("allow_large") is True)
        nbytes += _n * LINE_PREFIX_BYTES                 # the prefixes Read adds enter context too
        if nbytes <= FLOOR_BYTES:                        # a read this small always passes: no ledger can wedge a seat
            return None
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
            call_budget = min(call_budget, max(0, left))
        else:
            return None
        max_bytes = max(FLOOR_BYTES, (call_budget // max(1, remaining)) * BYTES_PER_TOKEN)
        text = _deny_text(real, nbytes, cost, max_bytes, why, remaining, offset, state)
        _log_denial(state, sid, real, nbytes, cost, remaining, now)   # only a deny that is certain leaves a row
        return text
    except Exception:  # noqa: BLE001  (any doubt: the vendor decides)
        return None


def _response_bytes(resp: Any) -> int:
    """Bytes that entered context. Relies on ONE field of the Read ``tool_response`` (seen in a local transcript as
    ``{"type": "text", "file": {"filePath": ..., "content": ...}}``): ``file.content`` when it is a string. Any other
    shape is charged as its whole serialization (the PostToolUse schema for Read is not verified against the docs)."""
    if isinstance(resp, Mapping) and isinstance(resp.get("file"), Mapping) and isinstance(resp["file"].get("content"), str):
        return len(resp["file"]["content"].encode("utf-8", "replace"))
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
        try:
            nontext = is_nontext(real)
        except Exception:  # noqa: BLE001  (the target vanished or is unreadable since the pre hook: treat as text)
            nontext = False
        nbytes = FLOOR_BYTES if nontext else _response_bytes(event.get("tool_response"))
        cost = charge(state, sid, tool="Read", path=real, nbytes=nbytes, remaining=remaining)
        if cost > _env_int(environ, "LEVAIN_READ_BUDGET_CALL", DEFAULT_CALL):
            return f"{DENY_PREFIX}that read cost ~{cost} token-turns ({nbytes} bytes x {remaining} turns left)."
    except Exception:  # noqa: BLE001
        pass
    return None


def settings_hooks(python: str, script: str) -> dict[str, Any]:
    """The ``hooks`` object for a seat's settings: PreToolUse and PostToolUse on :data:`HOOK_MATCHER`.

    The command is fail-open: ``/bin/sh -c 'PYTHON -I SCRIPT pre|post; c=$?; [ "$c" -eq 2 ] && exit 1; exit "$c"'``.
    Exit 2 is a BLOCKING hook error (every Read refused) and is what ``python -I`` returns when the script is missing or
    unreadable, whenever that happens (no test-then-exec window); it becomes 1, which does not block. :func:`main`
    never returns 2, so the script's own result and stdout pass through unchanged. A missing python (127) and a crash
    (1) are non-blocking already."""
    if not os.path.isabs(python) or not os.path.isabs(script):
        raise ValueError("the hook's python and script must be absolute paths")
    py, sc = shlex.quote(python), shlex.quote(script)

    def command(arg: str) -> str:
        return "/bin/sh -c " + shlex.quote(f'{py} -I {sc} {arg}; c=$?; [ "$c" -eq 2 ] && exit 1; exit "$c"')

    return {ev: [{"matcher": HOOK_MATCHER,
                  "hooks": [{"type": "command", "command": command(arg), "timeout": HOOK_TIMEOUT_S}]}]
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
