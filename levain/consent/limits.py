"""levain.consent.limits — per-session budgets and limits (Q13): Levain's own count of what a session spends.

Ruled 10-19 13:13 (Phill, *"(a) all recs"*; design ``project_memory/1019-q13/DESIGN.md``): F1d, F2a, F3b, F4a, F5b, F6a.

THE COUNTER. Every ``PreToolUse`` of a session is counted here, at the top of the agent's gate (on Codex, right after its
queue halt is read), before any of the gate's own early exits (an ungated tool, plan posture, an attended default session, nothing able to decide), so a loop of
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
- a root followed through more than :data:`SPEND_PATHS` transcript paths re-reads a returning one from its start
  (an over-count, never an under-count).
- past 256 subagent transcripts in one session, the later ones' tokens are not read; their calls and repeats still count.
- an event with no absolute ``cwd`` is not counted (Claude Code and Codex always send one).

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

__all__ = ["ACTIVE_GAP", "DB_NAME", "DEFAULTS", "FAIL_DENY_AFTER", "GO", "KINDS", "LIMIT_TIER", "LIMIT_TOOL", "LIMITS_NAME",
           "Outcome", "REPEAT_WINDOW", "Verdict", "check", "file_extend", "release_if_extended", "retry_text", "stop_text",
           "verified_allow", "root_halted", "extended_by",
           "count_call", "extend", "load_limits", "session_root", "snapshot"]

DB_NAME = "limits.db"
LIMITS_NAME = "limits.json"
LIMITS_FORMAT = "levain-limits/1"
LIMITS_MAX = 64 << 10       # bytes: a larger limits.json is not read (the defaults stand) and is never written
STORE_FORMAT = "levain-limits-store/1"
KINDS = ("calls", "active_hours", "fresh_tokens", "cache_read_tokens", "repeat")
# F5b, ruled 10-19: on by default, set above the operator's measured sessions (design §3 holds the measurement and its command)
DEFAULTS: dict[str, float] = {"calls": 3000, "active_hours": 12, "fresh_tokens": 15_000_000,
                              "cache_read_tokens": 600_000_000, "repeat": 8}
REPEAT_WINDOW = 10
ACTIVE_GAP = 600.0          # seconds: a longer gap between two calls is not active time
FAIL_DENY_AFTER = 20
RECENT_IDS = 64
SPEND_PATHS = 8           # transcript paths followed per root; an older one keeps its totals and is read no more
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
    "CREATE UNIQUE INDEX IF NOT EXISTS halts_request ON halts (request_id) WHERE request_id IS NOT NULL",
    # every limit request filed (by a crossing hook, or by `levain limits extend`), the one open hold per (root, kind)
    "CREATE TABLE IF NOT EXISTS requests ("
    " request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL,"
    " purpose TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS holds ("
    " agent TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL, request_id TEXT, claimed_at REAL NOT NULL,"
    " PRIMARY KEY (agent, root, kind))",
    # one Extend applied per verified allow: the doubling and any halt release happen once, whoever sees it first
    "CREATE TABLE IF NOT EXISTS extensions ("
    " request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL, scale INTEGER NOT NULL,"
    " at REAL NOT NULL)",
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
    # (newest extensions rowid, newest halts seq) of the root, read in the counting transaction: an Extend or halt past
    # this mark settled the very crossing this call joined (see :func:`_settled_since`)
    mark: tuple[int, int] | None = None


def session_root(session: str) -> str:
    """The session a count is kept under: a Claude Code subagent's ``<session>#agent:<id>`` is its parent's."""
    return session.split(SUBAGENT_MARK, 1)[0]


# --- the limits in force (F4a) -------------------------------------------------------------------------------------

def _limit_value(kind: str, v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:                            # an integer past float range raises here, and is a bad value like any other
        if not math.isfinite(v) or v <= 0 or (kind != "active_hours" and v != int(v)):
            return None
        return float(v)
    except (OverflowError, ValueError):
        return None


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
    if not stat.S_ISREG(st.st_mode) or st.st_size > LIMITS_MAX:
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
    """The store beside the queue, under the queue directory's own rule (created 0700; a symlink, another owner or
    access by group or others refused), so it is the operator's alone as ``queue.db`` is."""
    if not queue_dir.exists():
        queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(queue_dir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f"the consent directory {queue_dir} is not a 0700 directory of uid {os.getuid()}")
    return open_durable(queue_dir / DB_NAME, needs="the limits store")


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
        in_force = dict(limits) if limits is not None else _limits_or_defaults(qdir, agent)
        verdict = _count(qdir, agent, root, event, tool_name, tool_input,
                         time.time() if now is None else now, in_force)
    except Exception as exc:  # noqa: BLE001 - a brake never stops work on its own failure, until it is a runaway's
        halt = _halt_read_only(qdir, agent, root)
        if halt is not None:            # a halt is never lifted by the store failing to take the count
            return Verdict("halted", kind=halt["kind"], halt=halt, reason=f"{type(exc).__name__}")
        n = _fail_read(fail) + 1
        _fail_write(fail, n, create=True)    # made here too: a store that never worked is still capped
        why = f"{type(exc).__name__}: {exc}"[:300]
        if n >= FAIL_DENY_AFTER:
            return Verdict("fail_deny", reason=f"the limits store {qdir / DB_NAME} failed {n} times in a row ({why}); "
                                               "the operator fixes or moves that file aside (a full disk, a store of "
                                               "another levain version, a corrupt file) and the next call is counted")
        return Verdict("not_counting", reason=f"the limits store failed ({why})")
    try:
        if _fail_read(fail) or not os.path.lexists(fail):
            _fail_write(fail, 0, create=True)
    except Exception:  # noqa: BLE001 - the count stands; the failure file is best effort
        pass
    return verdict


def _halt_read_only(qdir: Path, agent: str, root: str) -> dict[str, Any] | None:
    """The open halt, read without the write lock (``mode=ro``, a short busy wait), or ``None`` when none can be read."""
    try:
        uri = (qdir / DB_NAME).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception:  # noqa: BLE001
        return None
    try:
        return _open_halt(conn, agent, root)
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _halt_released(qdir: Path, request_id: str) -> bool:
    """True only when the halt on ``request_id`` is READ as released; no row, or any failure to read, is False."""
    try:
        conn = sqlite3.connect((qdir / DB_NAME).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = conn.execute("SELECT released_at FROM halts WHERE request_id = ?", (request_id,)).fetchone()
        return row is not None and row[0] is not None
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def _halt_released_by_other(qdir: Path, request_id: str) -> bool:
    """True only when the halt on ``request_id`` READS as released by ANOTHER request's Extend; no row, a release by
    this request itself, or any failure to read, is False."""
    try:
        conn = sqlite3.connect((qdir / DB_NAME).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = conn.execute("SELECT released_at, released_by FROM halts WHERE request_id = ?", (request_id,)).fetchone()
        return row is not None and row[0] is not None and row[1] != request_id
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def root_halted(queue_dir: Path | str, agent: str, root: str) -> bool | None:
    """Whether ``root`` has an open limit halt, read without the write lock; ``None`` when it cannot be read."""
    try:
        conn = sqlite3.connect((Path(queue_dir) / DB_NAME).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except Exception:  # noqa: BLE001
        return None
    try:
        return _open_halt(conn, agent, session_root(root)) is not None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def extended_by(queue_dir: Path | str, request_id: str) -> bool:
    """True when ``request_id``'s own Extend was applied AND it is what released the root's halt (``released_by``):
    the one allow whose holder may resume the session. Read-only (``mode=ro``): a read never creates or migrates the
    store; a failure to read raises."""
    conn = sqlite3.connect((Path(queue_dir) / DB_NAME).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        return (conn.execute("SELECT 1 FROM extensions WHERE request_id = ?", (request_id,)).fetchone() is not None
                and conn.execute("SELECT 1 FROM halts WHERE released_by = ?", (request_id,)).fetchone() is not None)
    finally:
        conn.close()


def _limits_or_defaults(qdir: Path, agent: str) -> dict[str, float]:
    """The limits in force, or :data:`DEFAULTS` when they cannot be read at all: a bad limits file never turns the
    brake off."""
    try:
        return load_limits(qdir, agent)[0]
    except Exception:  # noqa: BLE001
        return dict(DEFAULTS)


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
        mark = (conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM extensions WHERE agent = ? AND root = ?",
                             (agent, root)).fetchone()[0],
                conn.execute("SELECT COALESCE(MAX(seq), 0) FROM halts WHERE agent = ? AND root = ?",
                             (agent, root)).fetchone()[0])
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
            # one reader state PER transcript path, summed: a root can show more than one (a resumed session's new
            # file), and a state shared between paths would re-read each file whole at every switch
            by = spend.get("by") if isinstance(spend.get("by"), dict) else {}
            by[transcript] = spend_step(transcript, agent, by.pop(transcript, None))   # re-inserted: newest last
            while len(by) > SPEND_PATHS:
                old = by.pop(next(iter(by)))
                spend["gone_fresh"] = _num0(spend.get("gone_fresh")) + _num0(old.get("fresh"))
                spend["gone_cache_read"] = _num0(spend.get("gone_cache_read")) + _num0(old.get("cache_read"))
            spend["by"] = by
            fresh = _num0(spend.get("gone_fresh")) + sum(_num0(s.get("fresh")) for s in by.values())
            cache_read = _num0(spend.get("gone_cache_read")) + sum(_num0(s.get("cache_read")) for s in by.values())
            stored = transcript
        conn.execute("UPDATE sessions SET calls = ?, active_s = ?, first_at = ?, last_at = ?, fresh = ?, "
                     "cache_read = ?, window = ?, recent_ids = ?, spend = ?, transcript = ? "
                     "WHERE agent = ? AND root = ?",
                     (calls, active_s, first_at, last_at, fresh, cache_read, json.dumps(window), json.dumps(recent),
                      json.dumps(spend, separators=(",", ":")), stored, agent, root))
    counts = _counts(calls, active_s, fresh, cache_read, window, win, digest if counted else None)
    for kind in KINDS:
        if counts[kind] > lim[kind] or (kind == "repeat" and counts[kind] >= lim[kind]):
            return Verdict("crossed", kind=kind, count=counts[kind], limit=lim[kind], counts=counts, limits=lim,
                           mark=mark)
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


def _apply_extension(conn: sqlite3.Connection, request_id: str, agent: str, root: str, kind: str, now: float
                     ) -> bool:
    """Inside the caller's write transaction: the Extend a verified allow on ``request_id`` grants, applied ONCE
    (``extensions`` keyed by the request): ``kind``'s scale doubles and every open halt of the root is released.
    False when this allow was already applied."""
    if conn.execute("SELECT 1 FROM extensions WHERE request_id = ?", (request_id,)).fetchone():
        return False
    row = conn.execute("SELECT scale FROM sessions WHERE agent = ? AND root = ?", (agent, root)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sessions (agent, root) VALUES (?, ?)", (agent, root))
        scale: dict[str, Any] = {}
    else:
        scale = _loads(row[0], {})
    cur = scale.get(kind, 1)
    cur = cur if isinstance(cur, int) and not isinstance(cur, bool) and cur >= 1 else 1
    scale[kind] = cur * 2
    conn.execute("UPDATE sessions SET scale = ? WHERE agent = ? AND root = ?", (json.dumps(scale), agent, root))
    conn.execute("INSERT INTO extensions (request_id, agent, root, kind, scale, at) VALUES (?, ?, ?, ?, ?, ?)",
                 (request_id, agent, root, kind, scale[kind], now))
    conn.execute("UPDATE halts SET released_at = ?, released_by = ? WHERE agent = ? AND root = ? "
                 "AND released_at IS NULL", (now, request_id, agent, root))
    conn.execute("DELETE FROM holds WHERE agent = ? AND root = ? AND kind = ? AND request_id = ?",
                 (agent, root, kind, request_id))
    return True


def extend(queue_dir: Path | str, agent: str, session: str, kind: str, request_id: str) -> int:
    """Apply the Extend of ``request_id`` (once; see :func:`_apply_extension`) and return ``kind``'s multiplier now.
    The CALLER verifies the signed allow first: this writes the scale, it authorises nothing."""
    if kind not in KINDS:
        raise ValueError(f"unknown limit {kind!r}")
    qdir, root = Path(queue_dir), session_root(session)
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        _apply_extension(conn, request_id, agent, root, kind, time.time())
        row = conn.execute("SELECT scale FROM sessions WHERE agent = ? AND root = ?", (agent, root)).fetchone()
    return _loads(row[0], {}).get(kind, 1)


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
            lim, win = _scaled(_limits_or_defaults(qdir, a), _loads(scale, {}))
            halt = _open_halt(conn, a, root)
            out.append({"agent": a, "root": root, "first_at": first_at, "last_at": last_at, "halt": halt,
                        "counts": _counts(calls, active_s, fresh, cache_read, _loads(window, []), win),
                        "limits": lim})
        return out
    finally:
        conn.close()


# --- the hold at a limit (F3b) ---------------------------------------------------------------------------------------

LIMIT_TOOL = "levain_limit"
# T2, as a push: a phone passkey (a key the operator's uid cannot hold) can Extend while he is away
LIMIT_TIER = "T2"
JOIN_WAIT = 5.0             # how long a parallel crossing waits for the first one's request id
HOLD_STALE = 60.0           # a claimed hold with no request id after this is a crashed filer's; reclaimed
LABELS = {"calls": "tool calls", "active_hours": "active hours", "fresh_tokens": "fresh tokens",
          "cache_read_tokens": "cache-read tokens", "repeat": "repeats of one call"}


@dataclass(frozen=True)
class Outcome:
    """The hook's answer from the limits layer: ``go`` (the call goes on to the gate) or a deny, ``stop`` saying
    whether the session's turn ends (Claude Code's ``continue: false``; Codex has no stop, its halt denies)."""
    go: bool
    text: str = ""
    stop: bool = False
    extended: str | None = None     # the kind an Extend just doubled while this call was held


GO = Outcome(True)


def retry_text(kind: str | None) -> str:
    """The deny for a GATED call held at a limit and then extended: the gate's own hold could not also fit inside the
    hook's timeout, so the call is asked for again in a fresh hook."""
    return (f"levain: the operator extended this session's {LABELS.get(kind or '', 'budget')} limit. The action was "
            "NOT run yet: run the same call again now.")


def _num_text(kind: str, v: float) -> str:
    if kind == "active_hours":
        return f"{v:.1f}"
    return f"{int(v):,}"


def limit_call(agent: str, root: str, kind: str, count: float, limit: float, *, purpose: str = "crossed"
               ) -> dict[str, Any]:
    """The tool input a limit request is filed with: what the operator decides, in fields the display shows whole."""
    call: dict[str, Any] = {"agent": agent, "session": root, "limit": kind,
                            "now": _num_text(kind, limit), "extend_to": _num_text(kind, limit * 2)}
    if purpose == "crossed":
        call["count"] = _num_text(kind, count)
    else:
        call["purpose"] = purpose
    return call


def limit_what(call: Mapping[str, Any]) -> str:
    """The limit request in the words the operator is pinged with."""
    kind = call.get("limit")
    label = LABELS.get(kind, str(kind))
    have = f"{call['count']} of {call['now']}" if "count" in call else f"limit {call['now']}"
    return (f"{call.get('agent')} session {str(call.get('session'))[:12]} reached its {label} limit ({have}). "
            f"Extend to {call['extend_to']} or End")


def _filed_for(queue: Any, request_id: str, got: Any, *, root: str | None = None, kind: str | None = None) -> bool:
    """``got`` decides a :data:`LIMIT_TOOL` request at :data:`LIMIT_TIER` whose sealed display reads back whole and,
    when given, names this ``root`` and ``kind``: the operator was shown the limit he extended, for this session."""
    from levain.consent.listing import filed_input

    found = queue.get(request_id)
    if found is None or getattr(got, "request_id", None) != request_id:
        return False
    request = found[0]
    call = filed_input(request)
    return (request.tool_name == LIMIT_TOOL and request.tier == LIMIT_TIER
            and getattr(got, "request_digest", None) == request.digest and isinstance(call, dict)
            and (root is None or call.get("session") == root) and (kind is None or call.get("limit") == kind))


def verified_allow(queue: Any, request_id: str, *, root: str | None = None, kind: str | None = None) -> bool:
    """A signed allow (a timely answer, or a late ruling on its gap) that verifies NOW for this limit request."""
    from levain.consent.queue import Decision, Ruling

    try:
        got = queue.decision(request_id)
    except Exception:  # noqa: BLE001 - nothing that fails is an allow
        return False
    if isinstance(got, Decision):
        ok = got.decision == "allow"
    elif isinstance(got, Ruling):
        ok = got.ruling == "allow"
    else:
        return False
    return ok and _filed_for(queue, request_id, got, root=root, kind=kind)


def _verified_deny(queue: Any, request_id: str) -> bool:
    from levain.consent.queue import Decision

    try:
        got = queue.decision(request_id)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(got, Decision) and got.decision == "deny" and _filed_for(queue, request_id, got)


def release_if_extended(queue: Any, agent: str, session: str, *, now: float | None = None) -> bool:
    """Lift the root's open halt when a limit request filed for it at or after the halt carries a verified allow
    (the halting request's late ruling, or a ``levain limits extend`` request). An End, a late deny or another
    request's ruling releases nothing. True when the root is no longer halted."""
    qdir, root = Path(queue.directory), session_root(session)
    now = time.time() if now is None else now
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        halt = _open_halt(conn, agent, root)
        if halt is None:
            return True
        cands = conn.execute("SELECT r.request_id, r.kind FROM requests r WHERE r.agent = ? AND r.root = ? AND r.at >= "
                             "(SELECT COALESCE((SELECT at FROM requests WHERE request_id = ?), ?)) AND NOT EXISTS "
                             "(SELECT 1 FROM extensions e WHERE e.request_id = r.request_id) ORDER BY r.at",
                             (agent, root, halt["request_id"], halt["at"])).fetchall()
    for request_id, kind in cands:    # the queue is read outside the limits transaction (never one lock in another)
        if verified_allow(queue, request_id, root=root, kind=kind):
            with write_transaction(lambda: _connect(qdir)) as conn:
                _ensure(conn)
                if _open_halt(conn, agent, root) is not None:   # another hook released it first: no second doubling
                    _apply_extension(conn, request_id, agent, root, kind, now)
            return True
    return False


def _settled_since(conn: sqlite3.Connection, agent: str, root: str, kind: str,
                   mark: tuple[int, int] | None) -> str | None:
    """The request whose Extend or halt of (root, kind) landed after ``mark`` (what the call's count saw), or None.
    The one hold of (root, kind) is gone once settled, so a crossing that finds no hold must ask this first: the
    hold it would have joined may have been filed, answered and cleared between its count and its look."""
    if mark is None:
        return None
    row = conn.execute("SELECT request_id FROM extensions WHERE agent = ? AND root = ? AND kind = ? AND rowid > ? "
                       "ORDER BY rowid LIMIT 1", (agent, root, kind, mark[0])).fetchone()
    if row is None:
        row = conn.execute("SELECT request_id FROM halts WHERE agent = ? AND root = ? AND kind = ? AND seq > ? "
                           "AND request_id IS NOT NULL ORDER BY seq LIMIT 1", (agent, root, kind, mark[1])).fetchone()
    return row[0] if row else None


def _claim(qdir: Path, agent: str, root: str, kind: str, now: float,
           mark: tuple[int, int] | None = None) -> tuple[bool, str | None]:
    """(this hook files the request, the open request id when another hook already filed it, or settled it since the
    call's count saw ``mark``)."""
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        row = conn.execute("SELECT request_id, claimed_at FROM holds WHERE agent = ? AND root = ? AND kind = ?",
                           (agent, root, kind)).fetchone()
        if row is not None and (row[0] is not None or now - row[1] < HOLD_STALE):
            return False, row[0]
        settled = _settled_since(conn, agent, root, kind, mark) if row is None else None
        if settled is not None:
            return False, settled
        conn.execute("INSERT OR REPLACE INTO holds (agent, root, kind, request_id, claimed_at) VALUES (?, ?, ?, NULL, ?)",
                     (agent, root, kind, now))
        return True, None


def _record_filed(qdir: Path, agent: str, root: str, kind: str, request_id: str | None, purpose: str,
                  now: float) -> None:
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        if request_id is None:      # the filing failed: free the claim so the next call can try
            conn.execute("DELETE FROM holds WHERE agent = ? AND root = ? AND kind = ? AND request_id IS NULL",
                         (agent, root, kind))
            return
        conn.execute("INSERT OR IGNORE INTO requests (request_id, agent, root, kind, purpose, at) VALUES "
                     "(?, ?, ?, ?, ?, ?)", (request_id, agent, root, kind, purpose, now))
        if purpose == "crossed":
            conn.execute("UPDATE holds SET request_id = ? WHERE agent = ? AND root = ? AND kind = ?",
                         (request_id, agent, root, kind))


def _halt_on(qdir: Path, agent: str, root: str, kind: str, request_id: str, gap: int | None, now: float) -> None:
    with write_transaction(lambda: _connect(qdir)) as conn:
        _ensure(conn)
        if conn.execute("SELECT 1 FROM extensions WHERE request_id = ?", (request_id,)).fetchone():
            return                  # an allow on this very request was applied first: no halt
        conn.execute("INSERT OR IGNORE INTO halts (agent, root, kind, request_id, gap, at) VALUES (?, ?, ?, ?, ?, ?)",
                     (agent, root, kind, request_id, gap, now))
        conn.execute("DELETE FROM holds WHERE agent = ? AND root = ? AND kind = ? AND request_id = ?",
                     (agent, root, kind, request_id))


def stop_text(kind: str | None, gap: int | None = None, *, ended: bool = False) -> str:
    """What the agent is told when a limit stops it: a budget the operator set, not a fault."""
    label = LABELS.get(kind or "", "a")
    how = "The operator chose End." if ended else (
        f"The operator did not answer in time (gap #{gap})." if gap is not None else "")
    return (f"levain: STOPPED by a budget the operator set: this session reached its {label} limit. {how} This is "
            "not a fault and not a denial of the work. Do not try to continue it another way; the session goes on "
            "only when the operator extends the limit (levain limits extend).").replace("  ", " ")


def check(agent: str, event: Mapping[str, Any], queue: Any, session: str, tool_name: str, tool_input: Any, *,
          hold: float, notify: Any, resume: Mapping[str, Any] | None = None, cwd: str,
          sleep: Any = time.sleep, now: Any = time.time) -> Outcome:
    """The limits layer at the top of the agent's ``PreToolUse`` gate: count the call (:func:`count_call`), and at a
    crossing hold it on ONE ``levain_limit`` request per (root, kind) (a parallel crossing joins the open one), pinging
    the operator (``notify(text)``) once. A verified allow = Extend: the limit doubles and the call goes on to the gate.
    A verified deny = End, or no answer by the hold's end = undecided (a gap number): both HALT the root, and every
    later call of the root (its subagents too) is denied with the stop until a verified allow lifts it
    (:func:`release_if_extended`). Never raises: anything that fails while holding is a deny, never a pass."""
    qdir = Path(queue.directory)
    v = count_call(qdir, agent, session, event, tool_name=tool_name, tool_input=tool_input)
    if v.state == "halted":
        try:
            if release_if_extended(queue, agent, session):
                v = count_call(qdir, agent, session, event, tool_name=tool_name, tool_input=tool_input)
        except Exception:  # noqa: BLE001 - the halt stands
            pass
    if v.state in ("ok", "not_counting"):
        return GO
    if v.state == "fail_deny":
        return Outcome(False, f"levain: the action was NOT taken: {v.reason}. The operator's budget cannot be counted, "
                              "so nothing runs until it can.")
    if v.state == "halted":
        return Outcome(False, stop_text(v.kind, (v.halt or {}).get("gap")), stop=True)
    root, kind = session_root(session), v.kind or "calls"
    try:
        return _hold(agent, queue, qdir, root, kind, v, hold=hold, notify=notify, resume=resume, cwd=cwd,
                     sleep=sleep, now=now)
    except Exception as exc:  # noqa: BLE001 - a failed hold never passes the call
        return Outcome(False, f"levain: the limits hold failed ({type(exc).__name__}), so the action was NOT taken. "
                              "Do not retry it another way.", stop=True)


def _hold(agent: str, queue: Any, qdir: Path, root: str, kind: str, v: Verdict, *, hold: float, notify: Any,
          resume: Mapping[str, Any] | None, cwd: str, sleep: Any, now: Any) -> Outcome:
    """File (or join) the one open request for (root, kind), wait for it, and RESOLVE it. Every participant runs the
    same resolution (:func:`_resolve`), so a filer that died mid-hold leaves nothing the next call cannot finish: a
    request that is undecided, allowed or denied is resolved by whoever reads it, and only an ended one (expired,
    withdrawn) is filed again."""
    from datetime import timedelta

    filer, request_id = _claim(qdir, agent, root, kind, now(), v.mark)
    if not filer and request_id is not None:
        held = queue.get(request_id)
        if held is None or held[1] in ("expired", "withdrawn"):
            with write_transaction(lambda: _connect(qdir)) as conn:
                _ensure(conn)
                conn.execute("DELETE FROM holds WHERE agent = ? AND root = ? AND kind = ? AND request_id = ?",
                             (agent, root, kind, request_id))
            filer, request_id = _claim(qdir, agent, root, kind, now(), v.mark)
    if filer:
        call = limit_call(agent, root, kind, v.count or 0, v.limit or 0)
        try:
            request = queue.file(agent=agent, agent_session_id=root, tool_name=LIMIT_TOOL, tool_input=call, cwd=cwd,
                                 hold=timedelta(seconds=hold), tier=LIMIT_TIER, resume=resume)
        except BaseException:
            _record_filed(qdir, agent, root, kind, None, "crossed", now())
            raise
        request_id = request.id
        _record_filed(qdir, agent, root, kind, request_id, "crossed", now())
        try:
            notify(f"levain: {limit_what(call)}? (request {request_id}, held up to {hold:g} s)")
        except Exception:  # noqa: BLE001 - a ping never fails the hold
            pass
    else:
        waited = 0.0
        while request_id is None and waited < JOIN_WAIT:
            sleep(0.2)
            waited += 0.2
            request_id = _hold_id(qdir, agent, root, kind, v.mark)
        if request_id is None:
            return Outcome(False, f"levain: this session reached its {LABELS.get(kind, kind)} limit and the operator is "
                                  "being asked; the action was NOT taken. Try it again shortly.")
    held = queue.get(request_id)
    if held is not None and held[1] == "pending":
        queue.await_decision(request_id, hold + 5.0, poll=0.25)
    out = _resolve(agent, queue, qdir, root, kind, request_id, sleep=sleep, now=now)
    if out.go:      # an Extend releases every open halt, so a halt open now is a NEWER End: the root's stop wins
        with write_transaction(lambda: _connect(qdir)) as conn:
            _ensure(conn)
            halt = _open_halt(conn, agent, root)
        if halt is not None:
            return Outcome(False, stop_text(halt["kind"], halt["gap"]), stop=True)
    return out


def _resolve(agent: str, queue: Any, qdir: Path, root: str, kind: str, request_id: str, *, sleep: Any,
             now: Any) -> Outcome:
    """What became of the limit request, applied once whoever reads it: a verified allow = Extend (the call goes on);
    a verified deny = End, or undecided at the end of its hold (a gap) = HALT; anything else is a plain deny with no
    halt (the next call asks again)."""
    from datetime import datetime, timezone

    from levain.cockpit.consent import parse_stamp
    from levain.consent.queue import Decision, Ruling

    held = queue.get(request_id)
    if held is not None and held[1] == "pending":
        left = (parse_stamp(held[0].expires_at) - datetime.now(timezone.utc)).total_seconds()
        if left > 0:      # capped: a clock stepped back must not carry the wait past the hook's timeout
            sleep(min(left + 0.05, 6.0))
    marked = queue.mark_undecided(request_id)       # idempotent: a retry keeps the gap; a decided one says what decided it
    got = marked.decision
    if isinstance(got, Ruling):        # ruled late after the hold ended (a filer that died between the mark and the halt)
        if got.ruling == "allow" and verified_allow(queue, request_id, root=root, kind=kind):
            with write_transaction(lambda: _connect(qdir)) as conn:
                _ensure(conn)
                _apply_extension(conn, request_id, agent, root, kind, now())
            return Outcome(True, extended=kind)
        _halt_on(qdir, agent, root, kind, request_id, marked.gap if marked.gap is not None else queue.gap(request_id),
                 now())
        return Outcome(False, stop_text(kind, marked.gap), stop=True)
    if isinstance(got, Decision) and got.decision == "allow" and _filed_for(queue, request_id, got, root=root,
                                                                             kind=kind):
        with write_transaction(lambda: _connect(qdir)) as conn:
            _ensure(conn)
            _apply_extension(conn, request_id, agent, root, kind, now())
        return Outcome(True, extended=kind)
    if _extended(qdir, request_id):                 # another participant applied this very allow
        return Outcome(True, extended=kind)
    if isinstance(got, Decision) and got.decision == "deny" and _filed_for(queue, request_id, got, root=root,
                                                                            kind=kind):
        _halt_on(qdir, agent, root, kind, request_id, None, now())
        return Outcome(False, stop_text(kind, ended=True), stop=True)
    if marked.state == "undecided" and marked.gap is not None:
        _halt_on(qdir, agent, root, kind, request_id, marked.gap, now())
        return Outcome(False, stop_text(kind, marked.gap), stop=True)
    if marked.state == "pending":     # not yet at its end (a clock step): still the open hold, asked again shortly
        return Outcome(False, f"levain: this session reached its {LABELS.get(kind, kind)} limit and the operator is "
                              "being asked; the action was NOT taken. Try it again shortly.")
    with write_transaction(lambda: _connect(qdir)) as conn:   # ended without a decision: the next call files anew
        _ensure(conn)
        conn.execute("DELETE FROM holds WHERE agent = ? AND root = ? AND kind = ? AND request_id = ?",
                     (agent, root, kind, request_id))
    return Outcome(False, f"levain: this session's {LABELS.get(kind, kind)} limit request ended without a decision "
                          f"({marked.reason or (held[1] if held else 'gone')}); the action was NOT taken. Run it "
                          "again to ask again.")


def _hold_id(qdir: Path, agent: str, root: str, kind: str, mark: tuple[int, int] | None = None) -> str | None:
    """The open hold's request id; once the hold is gone, the request that settled it since ``mark``."""
    conn = _connect(qdir)
    try:
        row = conn.execute("SELECT request_id FROM holds WHERE agent = ? AND root = ? AND kind = ?",
                           (agent, root, kind)).fetchone()
        if row is None:
            return _settled_since(conn, agent, root, kind, mark)
    finally:
        conn.close()
    return row[0]


def _extended(qdir: Path, request_id: str) -> bool:
    conn = _connect(qdir)
    try:
        return conn.execute("SELECT 1 FROM extensions WHERE request_id = ?", (request_id,)).fetchone() is not None
    finally:
        conn.close()


def file_extend(queue: Any, agent: str, session: str, *, hold: float, cwd: str,
                resume: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """File the Extend request ``levain limits extend <session>`` asks the operator to sign, for the limit the root is
    halted on. Returns (request id, its call). The operator's verified allow on it lifts the halt
    (:func:`release_if_extended`). ``resume`` is the halting request's resume record, so a late allow RULED on this
    request resumes the session through delivery as a ruling on the halting request would."""
    from datetime import timedelta

    qdir, root = Path(queue.directory), session_root(session)
    conn = _connect(qdir)
    try:
        _ensure(conn)
        halt = _open_halt(conn, agent, root)
        row = conn.execute("SELECT scale FROM sessions WHERE agent = ? AND root = ?", (agent, root)).fetchone()
    finally:
        conn.close()
    if halt is None:                # an allow is applied only to a halted root (release_if_extended): refuse a dead letter
        raise ValueError("the session is not halted by a limit: there is nothing to extend")
    kind = halt["kind"]
    lim, _w = _scaled(_limits_or_defaults(qdir, agent), _loads(row[0], {}) if row else {})
    call = limit_call(agent, root, kind, 0, lim[kind], purpose="extend")
    request = queue.file(agent=agent, agent_session_id=root, tool_name=LIMIT_TOOL, tool_input=call, cwd=cwd,
                         hold=timedelta(seconds=hold), tier=LIMIT_TIER, resume=resume)
    _record_filed(qdir, agent, root, kind, request.id, "extend", time.time())
    return request.id, call


def ruling_text(ruling: str, gap: int | None, display: str | None, *, queue_dir: Path | str | None = None,
                request_id: str | None = None) -> str | None:
    """What a session is told of a late ruling on its LIMIT request (never the gated-action text: there is no action
    to do): an allow is an Extend, so it goes on; a deny is an End, so it stays stopped. ``None`` for an unknown
    ruling, and for an End whose own halt (given ``queue_dir`` and ``request_id``) READS as released: an Extend lifted it
    since, so a stale End is never told to a session that was extended. Likewise ``None`` for an allow whose own halt
    reads as released by ANOTHER request: the session was extended and told so already. Anything that cannot be read
    tells the ruling. The gap is named only when there is one (an End-halted session has none)."""
    from types import SimpleNamespace

    from levain.consent.listing import filed_input

    call = filed_input(SimpleNamespace(tool_name=LIMIT_TOOL, display=display or ""))
    call = call if isinstance(call, dict) else {}
    label = LABELS.get(call.get("limit"), "budget")
    if ruling not in ("allow", "deny"):
        return None
    if queue_dir is not None and request_id is not None:
        released = _halt_released if ruling == "deny" else _halt_released_by_other
        if released(Path(queue_dir), request_id):
            return None
    on = f" (gap #{gap})" if isinstance(gap, int) and not isinstance(gap, bool) else ""
    if ruling == "allow":
        n = call.get("extend_to")
        to = f" to {n}" if isinstance(n, str) and n and len(n) <= 24 and set(n) <= set("0123456789,.") else ""
        return (f"levain: the operator EXTENDED this session's {label} limit{to}{on}. Go on with the work "
                "where it stopped.")
    return (f"levain: the operator ENDED this session at its {label} limit{on}. Stop here; do not continue "
            "the work another way.")


def _iso(t: float | None) -> str | None:
    from datetime import datetime, timezone

    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def panel(queue_dir: Path | str) -> dict[str, Any]:
    """The cockpit's limits panel and ``levain limits show --json``: every counted session with its metrics against
    its limits. ``status`` is ``over`` past the limit (``repeat``: at it), ``amber`` from 80 % of it, else ``ok``;
    ``counting`` is False while the root's failure file reads nonzero (the store is failing to take its calls)."""
    from datetime import datetime, timezone

    qdir = Path(queue_dir)
    sessions = []
    for r in snapshot(qdir):
        halt = r["halt"]
        metrics = []
        for k in KINDS:
            value, limit = r["counts"][k], r["limits"][k]
            over = value > limit or (k == "repeat" and value >= limit)
            metrics.append({"key": k, "label": LABELS[k], "value": value, "limit": limit,
                            "status": "over" if over else ("amber" if value >= 0.8 * limit else "ok")})
        sessions.append({"agent": r["agent"], "session": r["root"], "last_at": _iso(r["last_at"]),
                         "halted": ({"kind": halt["kind"], "gap": halt["gap"], "request_id": halt["request_id"]}
                                    if halt else None),
                         "counting": _fail_read(_fail_path(qdir, r["agent"], r["root"])) == 0,
                         "metrics": metrics})
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sessions": sessions}
