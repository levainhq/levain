"""levain.consent.limits — per-session budgets and limits (Q13): Levain's own count of what a session spends.

Ruled 10-19 13:13 (Phill, *"(a) all recs"*; design ``project_memory/1019-q13/DESIGN.md``): F1d, F2a, F3b, F4a, F5b, F6a.

THE COUNTER. Every ``PreToolUse`` of a session is counted here, at the TOP of the agent's gate, before any of the gate's
own early exits (an ungated tool, plan posture, an attended default session, nothing able to decide), so a loop of
reads and a call the agent's sandbox will refuse both count. A count is per SESSION ROOT: the session id with a Claude
Code subagent's ``#agent:<id>`` suffix stripped, so a session and its subagents are one aggregate (F6a). One row per
call ATTEMPT, idempotent by the call's id (``tool_use_id``) among the root's recent ids; a call with no id counts every
time. The count, the threshold check and the halt read are one ``BEGIN IMMEDIATE`` on ``<levain home>/consent/limits.db``.

What is counted (F1d), each against a limit:

- ``calls``: tool-call attempts;
- ``active_hours``: the sum of the gaps between consecutive calls, a gap over :data:`ACTIVE_GAP` not counted (a session
  resumed after a weekend keeps its id; wall clock is shown elsewhere, never enforced);
- ``fresh_tokens`` / ``cache_read_tokens``: read from the agent's own record
  (:func:`levain.cockpit.session_context.spend_step`, incrementally, with its offsets kept here);
- ``repeat``: the loop guard, one call (same tool name and input digest) recurring ``limit`` times among the root's last
  ``window`` calls (a whole-family sequence in the order the gate saw it; catches A,B,A,B too).

Dollars are never counted here: a price table rots with every model change and subscription users are not billed per
token (F1d; the cockpit shows an estimate).

A count past its limit is a CROSSING: :func:`count_call` reports the first crossed kind (in :data:`KINDS` order) and the
gate holds the call (Q13 session 2: the ``limit`` request and its halt). An Extend doubles that kind's limit for the root
(:func:`extend`); for ``repeat`` both the threshold and the window double (8 in 10 -> 16 in 20).

FAILING OPEN, BOUNDED. A brake must not stop all work on a passing disk error: when the store cannot be written the call
proceeds and the verdict says ``not_counting``. A runaway that filled the disk is exactly when the brake is needed, so
after :data:`FAIL_DENY_AFTER` consecutive failed writes in one root every call is denied until a write succeeds. The
failure count lives in a small fixed-size file per root, pre-allocated at the root's first counted call and overwritten
in place, so updating it needs no new disk blocks.

Known edges (one line each):

- token overshoot is the transcript's lag behind the conversation plus the turn in flight; the agent docs do not bound it.
- the agent's own Bash can append to its transcript: an inflated count holds the session (a brake, never a bypass).
- a transcript replaced or truncated mid-session is read again from its start, so its tokens may count twice.
- without S1's sandbox the agent can rewrite ``limits.json`` and ``limits.db``: a brake on a runaway, not a bound on an
  agent that tries (F4).
- the loop guard catches one call recurring, not a loop that varies an argument every time; tokens and calls catch that.
- only the first crossed kind is reported; a second kind already past its limit is held at the next call.
- the recent-id memory is bounded (:data:`RECENT_IDS`): a call id seen again after that many other calls counts again.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.consent.record import tool_input_digest

__all__ = ["ACTIVE_GAP", "DB_NAME", "DEFAULTS", "FAIL_DENY_AFTER", "KINDS", "LIMITS_NAME", "REPEAT_WINDOW", "Verdict",
           "count_call", "extend", "load_limits", "session_root", "snapshot"]

DB_NAME = "limits.db"
LIMITS_NAME = "limits.json"
LIMITS_FORMAT = "levain-limits/1"
STORE_FORMAT = "levain-limits-store/1"
KINDS = ("calls", "active_hours", "fresh_tokens", "cache_read_tokens", "repeat")
# F5b: on by default, 2.5-3x above the largest session measured (design §3, §5 F5); the loop guard 8 against a measured 5
DEFAULTS: dict[str, float] = {"calls": 3000, "active_hours": 12, "fresh_tokens": 15_000_000,
                              "cache_read_tokens": 600_000_000, "repeat": 8}
REPEAT_WINDOW = 10
ACTIVE_GAP = 600.0          # seconds: a longer gap between two calls is not active time
FAIL_DENY_AFTER = 20
RECENT_IDS = 64
FAIL_DIR = "limits-fail"
_FAIL_SIZE = 8
SUBAGENT_MARK = "#agent:"
AGENTS = ("claude_code", "codex")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions ("
    " agent TEXT NOT NULL, root TEXT NOT NULL,"
    " calls INTEGER NOT NULL DEFAULT 0, active_s REAL NOT NULL DEFAULT 0,"
    " first_at REAL, last_at REAL,"
    " fresh INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,"
    " window TEXT NOT NULL DEFAULT '[]', recent_ids TEXT NOT NULL DEFAULT '[]',"
    " spend TEXT NOT NULL DEFAULT '{}', scale TEXT NOT NULL DEFAULT '{}',"
    " transcript TEXT,"
    " PRIMARY KEY (agent, root))",
    # Q13 session 2 writes these; the counter reads them in its own transaction
    "CREATE TABLE IF NOT EXISTS halts ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL,"
    " request_id TEXT, gap INTEGER, at REAL NOT NULL, released_at REAL, released_by TEXT)",
    "CREATE INDEX IF NOT EXISTS halts_open ON halts (agent, root) WHERE released_at IS NULL",
)


@dataclass(frozen=True)
class Verdict:
    """What the counter says about one call.

    ``state``: ``ok`` (counted, under every limit) · ``crossed`` (counted, ``kind`` is past its limit: hold it) ·
    ``halted`` (the root has an unreleased limit halt: deny, nothing counted) · ``not_counting`` (the store failed:
    the call proceeds) · ``fail_deny`` (the store failed :data:`FAIL_DENY_AFTER` times in a row: deny)."""
    state: str
    kind: str | None = None
    count: float | None = None
    limit: float | None = None
    counts: dict[str, float] = field(default_factory=dict)
    limits: dict[str, float] = field(default_factory=dict)
    halt: dict[str, Any] | None = None
    reason: str = ""


def session_root(session: str) -> str:
    """The session a count is kept under: a Claude Code subagent's ``<session>#agent:<id>`` is its parent's."""
    return session.split(SUBAGENT_MARK, 1)[0]


# --- the limits in force (F4a) -------------------------------------------------------------------------------------

def _limit_value(kind: str, v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    if kind != "active_hours" and v != int(v):
        return None
    return float(v)


def load_limits(queue_dir: Path | str, agent: str) -> tuple[dict[str, float], str]:
    """(the limits in force for ``agent``, where they came from): :data:`DEFAULTS`, overlaid by
    ``<queue dir>/limits.json``'s ``default`` block and then its block for the agent. An unreadable or malformed file
    leaves the defaults in force (never off) and says so; a bad value is skipped and said."""
    out = dict(DEFAULTS)
    path = Path(queue_dir) / LIMITS_NAME
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return out, "defaults"
    except OSError as exc:
        return out, f"defaults ({LIMITS_NAME} unreadable: {type(exc).__name__})"
    if not stat.S_ISREG(st.st_mode) or st.st_size > 64 << 10:
        return out, f"defaults ({LIMITS_NAME} is not a small regular file)"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return out, f"defaults ({LIMITS_NAME} unreadable: {type(exc).__name__})"
    if not isinstance(doc, dict) or doc.get("format") != LIMITS_FORMAT:
        return out, f"defaults ({LIMITS_NAME} is not {LIMITS_FORMAT})"
    bad: list[str] = []
    for block in ("default", agent):
        got = doc.get(block)
        if got is None:
            continue
        if not isinstance(got, dict):
            bad.append(block)
            continue
        for kind, v in got.items():
            val = _limit_value(kind, v) if kind in DEFAULTS else None
            if val is None:
                bad.append(f"{block}.{kind}")
            else:
                out[kind] = val
    src = LIMITS_NAME + (f" (skipped: {', '.join(bad)})" if bad else "")
    return out, src


# --- the store -------------------------------------------------------------------------------------------------------

def _connect(queue_dir: Path) -> sqlite3.Connection:
    conn = open_durable(queue_dir / DB_NAME, needs="the limits store")
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    for s in _SCHEMA:
        conn.execute(s)
    got = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    if got is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
    elif got[0] != STORE_FORMAT:
        raise RuntimeError(f"the limits store is {got[0]!r}, not {STORE_FORMAT}")


def _loads(text: Any, default: Any) -> Any:
    try:
        v = json.loads(text) if isinstance(text, str) else default
    except (ValueError, RecursionError):
        return default
    return v if isinstance(v, type(default)) else default


def _scaled(limits: Mapping[str, float], scale: Mapping[str, Any]) -> tuple[dict[str, float], int]:
    out = {}
    for k, v in limits.items():
        s = scale.get(k, 1)
        s = s if isinstance(s, int) and not isinstance(s, bool) and s >= 1 else 1
        out[k] = v * s
    s = scale.get("repeat", 1)
    window = REPEAT_WINDOW * (s if isinstance(s, int) and not isinstance(s, bool) and s >= 1 else 1)
    return out, window


def _transcript(event: Mapping[str, Any]) -> str | None:
    t = event.get("transcript_path")
    if isinstance(t, str) and 0 < len(t) <= 4096 and "\0" not in t and os.path.isabs(t) and t.endswith(".jsonl"):
        return t
    return None


def _fail_path(queue_dir: Path, agent: str, root: str) -> Path:
    h = hashlib.sha256(f"{agent}\0{root}".encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return queue_dir / FAIL_DIR / h


def _fail_read(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return 0
    try:
        raw = os.pread(fd, _FAIL_SIZE, 0)
    except OSError:
        return 0
    finally:
        os.close(fd)
    return int.from_bytes(raw, "big") if len(raw) == _FAIL_SIZE else 0


def _fail_write(path: Path, n: int, *, create: bool) -> bool:
    """Overwrite the root's failure count in place (``create``: make the file first, at a successful count)."""
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | (os.O_CREAT if create else 0)
    try:
        if create:
            path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, flags, 0o600)
    except OSError:
        return False
    try:
        os.pwrite(fd, max(0, n).to_bytes(_FAIL_SIZE, "big"), 0)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _open_halt(conn: sqlite3.Connection, agent: str, root: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT seq, kind, request_id, gap, at FROM halts WHERE agent = ? AND root = ? "
                       "AND released_at IS NULL ORDER BY seq LIMIT 1", (agent, root)).fetchone()
    if row is None:
        return None
    return {"seq": row[0], "kind": row[1], "request_id": row[2], "gap": row[3], "at": row[4]}


def count_call(queue_dir: Path | str, agent: str, session: str, event: Mapping[str, Any], *,
               tool_name: str, tool_input: Any, now: float | None = None,
               limits: Mapping[str, float] | None = None) -> Verdict:
    """Count one ``PreToolUse`` call of ``session`` (Claude Code's subagent key included) and say whether it may go on.
    Never raises: a failure is ``not_counting`` (proceed) or, past :data:`FAIL_DENY_AFTER`, ``fail_deny``."""
    qdir = Path(queue_dir)
    root = session_root(session)
    fail = _fail_path(qdir, agent, root)
    try:
        if agent not in AGENTS:
            raise ValueError(f"unknown agent {agent!r}")
        in_force = dict(limits) if limits is not None else load_limits(qdir, agent)[0]
        verdict = _count(qdir, agent, root, event, tool_name, tool_input,
                         time.time() if now is None else now, in_force)
    except Exception as exc:  # noqa: BLE001 - a brake never stops work on its own failure, until it is a runaway's
        n = _fail_read(fail) + 1
        _fail_write(fail, n, create=False)
        why = f"{type(exc).__name__}: {exc}"[:300]
        if n >= FAIL_DENY_AFTER:
            return Verdict("fail_deny", reason=f"the limits store failed {n} times in a row ({why})")
        return Verdict("not_counting", reason=f"the limits store failed ({why})")
    if _fail_read(fail) or not fail.exists():
        _fail_write(fail, 0, create=True)
    return verdict


def _count(qdir: Path, agent: str, root: str, event: Mapping[str, Any], tool_name: str, tool_input: Any,
           now: float, in_force: Mapping[str, float]) -> Verdict:
    from levain.cockpit.session_context import spend_step

    digest = hashlib.sha256(f"{tool_name}\0{tool_input_digest(tool_input)}".encode()).hexdigest()[:16]
    tid = event.get("tool_use_id")
    tid = tid if isinstance(tid, str) and 0 < len(tid) <= 256 else None
    transcript = _transcript(event)
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        halt = _open_halt(conn, agent, root)
        row = conn.execute("SELECT calls, active_s, first_at, last_at, fresh, cache_read, window, recent_ids, spend, "
                           "scale, transcript FROM sessions WHERE agent = ? AND root = ?", (agent, root)).fetchone()
        if row is None:
            conn.execute("INSERT INTO sessions (agent, root) VALUES (?, ?)", (agent, root))
            row = (0, 0.0, None, None, 0, 0, "[]", "[]", "{}", "{}", None)
        calls, active_s, first_at, last_at, fresh, cache_read = row[:6]
        window, recent, spend, scale = (_loads(row[6], []), _loads(row[7], []), _loads(row[8], {}),
                                        _loads(row[9], {}))
        lim, win = _scaled(in_force, scale)
        if halt is not None:   # a halted root: deny, count nothing (the counts resume where they stopped on Extend)
            return Verdict("halted", kind=halt["kind"], halt=halt,
                           counts=_counts(calls, active_s, fresh, cache_read, window, win), limits=lim)
        if tid is not None and tid in recent:
            counted = False
        else:
            counted = True
            calls += 1
            if last_at is not None and 0 <= now - last_at <= ACTIVE_GAP:
                active_s += now - last_at
            last_at = now if last_at is None else max(last_at, now)
            first_at = first_at if first_at is not None else now
            window = (window + [digest])[-max(win, REPEAT_WINDOW) * 2:]
            if tid is not None:
                recent = (recent + [tid])[-RECENT_IDS:]
        stored = row[10]
        if transcript is not None:
            if stored is not None and stored != transcript:
                spend = {}   # another transcript under the same root (a resumed session's new file): totals kept below
                spend_base = (fresh, cache_read)
            else:
                spend_base = (_num0(spend.get("base_fresh")), _num0(spend.get("base_cache_read")))
            spend = spend_step(transcript, agent, spend)
            spend["base_fresh"], spend["base_cache_read"] = spend_base
            fresh = spend_base[0] + spend["fresh"]
            cache_read = spend_base[1] + spend["cache_read"]
            stored = transcript
        conn.execute("UPDATE sessions SET calls = ?, active_s = ?, first_at = ?, last_at = ?, fresh = ?, "
                     "cache_read = ?, window = ?, recent_ids = ?, spend = ?, transcript = ? "
                     "WHERE agent = ? AND root = ?",
                     (calls, active_s, first_at, last_at, fresh, cache_read, json.dumps(window), json.dumps(recent),
                      json.dumps(spend, separators=(",", ":")), stored, agent, root))
    counts = _counts(calls, active_s, fresh, cache_read, window, win, digest if counted else None)
    for kind in KINDS:
        if counts[kind] > lim[kind] or (kind == "repeat" and counts[kind] >= lim[kind]):
            return Verdict("crossed", kind=kind, count=counts[kind], limit=lim[kind], counts=counts, limits=lim)
    return Verdict("ok", counts=counts, limits=lim)


def _num0(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _counts(calls: int, active_s: float, fresh: int, cache_read: int, window: list[Any], win: int,
            digest: str | None = None) -> dict[str, float]:
    last = window[-win:]
    if digest is not None:
        rep = last.count(digest)
    else:
        rep = max((last.count(d) for d in set(last)), default=0)
    return {"calls": calls, "active_hours": active_s / 3600.0, "fresh_tokens": fresh, "cache_read_tokens": cache_read,
            "repeat": rep}


def extend(queue_dir: Path | str, agent: str, session: str, kind: str) -> int:
    """Double ``kind``'s limit for the session's root (an Extend; for ``repeat`` the window doubles with it). Returns
    the new multiplier. The CALLER verifies the signed Extend first (Q13 session 2); this only writes the scale."""
    if kind not in KINDS:
        raise ValueError(f"unknown limit {kind!r}")
    qdir, root = Path(queue_dir), session_root(session)
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        row = conn.execute("SELECT scale FROM sessions WHERE agent = ? AND root = ?", (agent, root)).fetchone()
        if row is None:
            conn.execute("INSERT INTO sessions (agent, root) VALUES (?, ?)", (agent, root))
            scale: dict[str, Any] = {}
        else:
            scale = _loads(row[0], {})
        cur = scale.get(kind, 1)
        cur = cur if isinstance(cur, int) and not isinstance(cur, bool) and cur >= 1 else 1
        scale[kind] = cur * 2
        conn.execute("UPDATE sessions SET scale = ? WHERE agent = ? AND root = ?",
                     (json.dumps(scale), agent, root))
    return scale[kind]


def snapshot(queue_dir: Path | str, agent: str | None = None) -> list[dict[str, Any]]:
    """Every counted root's counts against its limits, for the cockpit row and ``levain limits show`` (a read; an
    absent store is an empty list)."""
    qdir = Path(queue_dir)
    if not (qdir / DB_NAME).exists():
        return []
    conn = _connect(qdir)
    try:
        rows = conn.execute("SELECT agent, root, calls, active_s, first_at, last_at, fresh, cache_read, window, scale "
                            "FROM sessions" + (" WHERE agent = ?" if agent else "") + " ORDER BY last_at DESC",
                            (agent,) if agent else ()).fetchall()
        out = []
        for a, root, calls, active_s, first_at, last_at, fresh, cache_read, window, scale in rows:
            lim, win = _scaled(load_limits(qdir, a)[0], _loads(scale, {}))
            halt = _open_halt(conn, a, root)
            out.append({"agent": a, "root": root, "first_at": first_at, "last_at": last_at, "halt": halt,
                        "counts": _counts(calls, active_s, fresh, cache_read, _loads(window, []), win),
                        "limits": lim})
        return out
    finally:
        conn.close()
