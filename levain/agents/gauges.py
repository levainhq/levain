"""levain.agents.gauges — read-only gauges over the agent tables and an agent's input and output files (BUILD_PLAN P8b-i).

A gauge reports a value and its own staleness, and nothing else: it never writes, never fires anything and holds no
gate semantics (flow's GATE/GAUGE/FEED ruling: a wrong gauge is a wrong number, so it must say how old the number is).
The win verifier at the end of this module is the ``verify`` hook's default; ``supervise.py`` builds it for its store.

Absence is never health. A gauge with nothing to look at reports :data:`UNKNOWN`, not :data:`OK`; a quiet input and a
dead input are different values (quiet means ``skipped-no-input``, dead means failure).

Every function takes the evidence's location and an injected ``now``; the store connections are the caller's (read
connections from ``AgentStore.read()`` or ``AgentStoreView.read()``), and the only statements issued are SELECTs.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["OK", "QUIET", "DEAD", "UNKNOWN", "NOT_MEASURED", "STUB_MAX_BYTES", "DEFAULT_STALE_AFTER", "Gauge",
           "input_liveness", "output_arrival", "fate_of_output", "run_health", "WinContext", "WIN_UNVERIFIED", "WIN_FAILED",
           "register_check", "win_verifier"]

OK = "ok"
QUIET = "quiet"            # nothing real to read: the fire is skipped-no-input, which is not a failure
DEAD = "dead"              # the source is gone or unreadable: a failure
UNKNOWN = "unknown"        # no evidence either way
NOT_MEASURED = "not-measured"

# An error body written where input should be (an HTTP error page, a traceback, an {"error": ...} reply) is small and
# shaped like an envelope. A bare word such as "error" inside real data is not a stub, so only a leading shape counts.
STUB_MAX_BYTES = 1024
_ENVELOPE_KEYS = frozenset({"error", "errors", "message", "status", "statuscode", "code", "detail", "details", "type",
                            "request_id", "requestid"})
_JSON_ERROR_LEAD = re.compile(r'\{\s*"errors?"\s*:')
_TEXT_ERROR_HEAD =re.compile(r"(?:traceback \(most recent call last\)|(?:error|fatal|exception)\s*[:\[]"
                              r"|http/\d(?:\.\d)?\s+[45]\d\d\b)", re.IGNORECASE)

DEFAULT_STALE_AFTER = 2 * 24 * 3600    # seconds; a reading older than this says so


@dataclass(frozen=True)
class Gauge:
    """One reading. ``observed_at`` is when the evidence was made (None when there is none), ``age_seconds`` its age
    at ``now``, ``stale`` True when it is older than ``stale_after`` or when there is no evidence to date at all."""
    name: str
    value: str
    detail: str
    observed_at: str | None
    age_seconds: int | None
    stale: bool
    stale_after: int


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _parse_stamp(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _gauge(name: str, value: str, detail: str, observed: datetime | None, now: datetime,
           stale_after: int) -> Gauge:
    if observed is None:
        return Gauge(name, value, detail, None, None, True, stale_after)
    age = max(0, int((_utc(now) - _utc(observed)).total_seconds()))
    return Gauge(name, value, detail, _utc(observed).strftime("%Y-%m-%dT%H:%M:%SZ"), age, age > stale_after, stale_after)


def _looks_like_error(head: bytes) -> bool:
    text = head.decode("utf-8", "replace").lstrip()
    low = text.lower()
    if low.startswith(("<html", "<!doctype html")):
        return True
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except ValueError:    # a cut-off reply still leads with its error key
            return _JSON_ERROR_LEAD.match(text) is not None
        if not isinstance(body, dict):
            return False
        lowered = {str(k).lower(): v for k, v in body.items()}
        keys = set(lowered)
        return (bool(keys & {"error", "errors"}) and keys <= _ENVELOPE_KEYS
                and bool(lowered.get("error") or lowered.get("errors")))
    return _TEXT_ERROR_HEAD.match(text) is not None


def _as_path(source: object) -> Path | None:
    """The path ``source`` names, or None when it cannot be made into one on this host."""
    try:
        raw = os.fspath(source)    # type: ignore[arg-type]
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        return Path(raw).expanduser() if raw.strip() else None
    except (TypeError, ValueError, RuntimeError, OSError):
        return None


def input_liveness(source: str | os.PathLike | None, now: datetime, *, stale_after: int = DEFAULT_STALE_AFTER) -> Gauge:
    """Whether the input at ``source`` (a file path) is real. A missing source is DEAD, an empty file or an error
    stub (small and envelope-shaped) is QUIET with no real input, anything else is OK. A source that is not a path
    this host can open is UNKNOWN: this gauge makes no network call to find out. The size and the content come from
    one descriptor, so a file replaced mid-read cannot pair one file's size with another's bytes."""
    name = "input_liveness"
    if source is None:
        return _gauge(name, UNKNOWN, "the spec names no input source", None, now, stale_after)
    path = _as_path(source)
    if path is None:
        return _gauge(name, UNKNOWN, "the input source is not a path this host can use", None, now, stale_after)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return _gauge(name, DEAD, "the input source is gone", None, now, stale_after)
    except PermissionError:
        return _gauge(name, DEAD, "the input source is unreadable (permission denied)", None, now, stale_after)
    except (OSError, ValueError) as e:
        return _gauge(name, UNKNOWN, f"the input source cannot be examined ({type(e).__name__})", None, now, stale_after)
    try:
        try:
            st = os.fstat(fd)
        except OSError as e:
            return _gauge(name, UNKNOWN, f"the input source cannot be examined ({type(e).__name__})", None, now, stale_after)
        if not stat.S_ISREG(st.st_mode):
            return _gauge(name, UNKNOWN, "the input source is not a file", None, now, stale_after)
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        try:
            head = os.read(fd, STUB_MAX_BYTES + 1)
        except OSError as e:
            return _gauge(name, DEAD, f"the input source cannot be read ({type(e).__name__})", mtime, now, stale_after)
    finally:
        os.close(fd)
    if not head:
        return _gauge(name, QUIET, "no real input: the file is empty", mtime, now, stale_after)
    if len(head) <= STUB_MAX_BYTES and _looks_like_error(head):
        return _gauge(name, QUIET, f"no real input: a {len(head)}-byte error stub", mtime, now, stale_after)
    return _gauge(name, OK, f"{st.st_size} bytes of input", mtime, now, stale_after)


def _latest(conn: sqlite3.Connection, agent_id: str, state: str | None = None) -> tuple | None:
    """The newest occurrence of the agent's CURRENT spec: a run of an earlier revision says nothing of this one, and an
    agent with no current spec has no evidence. Ties on the second-resolution stamp break by insertion order."""
    sql = ("SELECT o.state, o.admitted_at, o.launched_at, o.ended_at, o.reason FROM agent_occurrences o"
           " JOIN agents a ON a.agent_id = o.agent_id"
           " JOIN agent_specs s ON s.agent_id = a.agent_id AND s.version = a.current_version AND s.spec_hash = o.spec_hash"
           " WHERE o.agent_id = ?")
    args: list = [agent_id]
    if state is not None:
        sql += " AND o.state = ?"
        args.append(state)
    sql += " ORDER BY o.admitted_at DESC, o.rowid DESC LIMIT 1"
    return conn.execute(sql, args).fetchone()


def output_arrival(conn: sqlite3.Connection, agent_id: str, destination: str | os.PathLike | None, now: datetime, *,
                   stale_after: int = DEFAULT_STALE_AFTER) -> Gauge:
    """Whether the output of the agent's latest finished run reached its destination file: OK when the file was
    written at or after that run launched, DEAD when the run is done and the file is missing or older. No finished
    run is UNKNOWN."""
    name = "output_arrival"
    row = _latest(conn, agent_id, "done")
    if row is None:
        return _gauge(name, UNKNOWN, "no finished run to look for output from", None, now, stale_after)
    ended = _parse_stamp(row[3])
    began = _parse_stamp(row[2]) or _parse_stamp(row[1])
    path = _as_path(destination) if destination is not None else None
    if path is None:
        return _gauge(name, UNKNOWN, "the spec names no usable output destination", ended, now, stale_after)
    try:
        st = path.stat()
    except FileNotFoundError:
        return _gauge(name, DEAD, "the last run is done and its destination is missing", ended, now, stale_after)
    except (OSError, ValueError) as e:
        return _gauge(name, UNKNOWN, f"the destination cannot be examined ({type(e).__name__})", ended, now, stale_after)
    if not stat.S_ISREG(st.st_mode):
        return _gauge(name, UNKNOWN, "the destination is not a file", ended, now, stale_after)
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if began is None:
        return _gauge(name, UNKNOWN, "the last run has no start stamp to compare the destination with", mtime, now,
                      stale_after)
    if mtime < began:
        return _gauge(name, DEAD, "the destination is older than the last run", mtime, now, stale_after)
    return _gauge(name, OK, "the destination was written by the last run", mtime, now, stale_after)


def fate_of_output(conn: sqlite3.Connection, agent_id: str, output: dict | None, now: datetime) -> Gauge:
    """What became of the output. A corpus output is not measured. A recovery output means "a drill ran within its
    cadence": OK when a run finished inside ``drill_cadence`` seconds, DEAD when the latest finished run is older,
    UNKNOWN with none. A push output has no fate measure here (its arrival is :func:`output_arrival`)."""
    name = "fate_of_output"
    output = output if isinstance(output, dict) else {}
    cls = output.get("class")
    if cls == "corpus":
        return _gauge(name, NOT_MEASURED, "a corpus output is not measured", None, now, DEFAULT_STALE_AFTER)
    if cls != "recovery":
        return _gauge(name, NOT_MEASURED if cls == "push" else UNKNOWN,
                      "a push output's arrival is measured by output_arrival" if cls == "push"
                      else "the spec names no output class", None, now, DEFAULT_STALE_AFTER)
    cadence = output.get("drill_cadence")
    if not isinstance(cadence, int) or cadence <= 0:
        return _gauge(name, UNKNOWN, "a recovery output names no drill cadence", None, now, DEFAULT_STALE_AFTER)
    row = _latest(conn, agent_id, "done")
    ended = _parse_stamp(row[3]) if row is not None else None
    if ended is None:
        return _gauge(name, UNKNOWN, "no drill has finished", None, now, cadence)
    age = (_utc(now) - ended).total_seconds()
    if age > cadence:
        return _gauge(name, DEAD, f"the last drill finished {int(age)} s ago, past its {cadence} s cadence",
                      ended, now, cadence)
    return _gauge(name, OK, "a drill ran within its cadence", ended, now, cadence)


def run_health(conn: sqlite3.Connection, agent_id: str, now: datetime, *, stale_after: int = DEFAULT_STALE_AFTER) -> Gauge:
    """The agent's latest run read as health. An agent that has never run is UNKNOWN, never OK. The latest run
    ``done`` is OK, ``failed`` is DEAD, a skipped-no-input run is QUIET; anything else (in flight, stopped, missed,
    skipped for overlap or budget) is UNKNOWN with the state named."""
    name = "run_health"
    row = _latest(conn, agent_id)
    if row is None:
        return _gauge(name, UNKNOWN, "the agent has no runs", None, now, stale_after)
    state, admitted, launched, ended, _reason = row
    when = _parse_stamp(ended) or _parse_stamp(launched) or _parse_stamp(admitted)
    value = {"done": OK, "failed": DEAD, "skipped-no-input": QUIET}.get(state, UNKNOWN)
    return _gauge(name, value, f"the latest run is {state}", when, now, stale_after)


# --- the win verifier (BUILD_PLAN P8b-ii) ------------------------------------------------------------------------------
#
# ``Supervisor.finish`` calls the verify hook inside the fire, before the end is written, so the verifier runs in the
# fire's process under the occurrence's grant. It judges only a ``done`` the run asked for. ``win.kind`` decides what
# counts: ``human`` and ``inherited`` name no machine check (a named human verdict surface; the downstream's win) and
# are accepted here; ``verifier`` and ``verified-restore`` name a check by ``win.check``, which must be a name
# registered with :func:`register_check`. A run whose win names no registered check was not verified, so it fails
# (:data:`WIN_UNVERIFIED`): a done that nothing checked is not a win. A check that raises fails the run as
# ``verify_error`` through the hook's own rule.

WIN_UNVERIFIED = "win_unverified"
WIN_FAILED = "win_failed"
_MACHINE_KINDS = frozenset({"verifier", "verified-restore"})
_CHECKS: dict[str, Callable[["WinContext"], str | None]] = {}


@dataclass(frozen=True)
class WinContext:
    """What a registered check is given: the occurrence, its spec as stored, and the end it is judging. Read-only."""
    store: object
    occurrence_id: str
    agent_id: str
    spec: dict
    launched_at: str | None
    end: object
    now: datetime


def register_check(name: str, check: Callable[[WinContext], str | None]) -> None:
    """Register ``check`` under ``name`` (the ``win.check`` text of a spec). It returns None for a win or a reason."""
    if not isinstance(name, str) or not name.strip() or not callable(check):
        raise ValueError("a check needs a non-empty name and a callable")
    if name in _CHECKS and _CHECKS[name] is not check:
        raise ValueError(f"a check named {name!r} is already registered")
    _CHECKS[name] = check


def _stored_spec(store, occurrence_id: str) -> tuple[str, dict | None, str | None] | None:
    with store.read() as conn:
        row = conn.execute("SELECT o.agent_id, s.spec, o.launched_at FROM agent_occurrences o"
                           " LEFT JOIN agent_specs s ON s.agent_id = o.agent_id AND s.spec_hash = o.spec_hash"
                           " WHERE o.occurrence_id = ?", (occurrence_id,)).fetchone()
    if row is None:
        return None
    try:
        spec = json.loads(row[1]) if row[1] else None
    except ValueError:
        spec = None
    return row[0], spec if isinstance(spec, dict) else None, row[2]


def win_verifier(store, *, clock: Callable[[], datetime] | None = None) -> Callable[[object], str | None]:
    """The ``verify`` hook for ``store``: None accepts the end, a reason fails a ``done``. Only a ``done`` is judged."""
    now_of = clock or (lambda: datetime.now(timezone.utc))

    def verify(end) -> str | None:
        if getattr(end, "state", None) != "done":
            return None
        found = _stored_spec(store, end.occurrence_id)
        if found is None:
            return WIN_UNVERIFIED
        agent_id, spec, launched = found
        if spec is None:
            return WIN_UNVERIFIED
        win = spec.get("win")
        if not isinstance(win, dict) or win.get("kind") in ("human", "inherited"):
            return None      # no machine check is named: registration (refuse.py) is what requires a win
        if win.get("kind") not in _MACHINE_KINDS:
            return WIN_UNVERIFIED
        check = _CHECKS.get(win.get("check"))
        if check is None:
            return WIN_UNVERIFIED
        verdict = check(WinContext(store, end.occurrence_id, agent_id, spec, launched, end, now_of()))
        if verdict is None:
            return None
        return verdict if type(verdict) is str and verdict.strip() and verdict != "completed" else WIN_FAILED

    return verify
