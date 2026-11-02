"""levain.agents.gauges — read-only gauges over the agent tables and an agent's input and output files (BUILD_PLAN P8b-i).

A gauge reports a value and its own staleness, and nothing else: it never writes, never fires anything and holds no
gate semantics (flow's GATE/GAUGE/FEED ruling: a wrong gauge is a wrong number, so it must say how old the number is).
The hooks that fill these in are registered by a later hunk in ``supervise.py``, not here.

Absence is never health. A gauge with nothing to look at reports :data:`UNKNOWN`, not :data:`OK`; a quiet input and a
dead input are different values (quiet means ``skipped-no-input``, dead means failure).

Every function takes the evidence's location and an injected ``now``; the store connections are the caller's (read
connections from ``AgentStore.read()`` or ``AgentStoreView.read()``), and the only statements issued are SELECTs.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["OK", "QUIET", "DEAD", "UNKNOWN", "NOT_MEASURED", "STUB_MAX_BYTES", "DEFAULT_STALE_AFTER", "Gauge",
           "input_liveness", "output_arrival", "fate_of_output", "run_health"]

OK = "ok"
QUIET = "quiet"            # nothing real to read: the fire is skipped-no-input, which is not a failure
DEAD = "dead"              # the source is gone or unreadable: a failure
UNKNOWN = "unknown"        # no evidence either way
NOT_MEASURED = "not-measured"

# An error body written where input should be (an HTTP error page, a traceback, an {"error": ...} reply) is small;
# a real input of that size would not start with an error marker, so both tests must hold.
STUB_MAX_BYTES = 1024
_ERROR_MARKERS = (b"error", b"traceback", b"exception", b"failed", b"unauthorized", b"forbidden", b"not found",
                  b"timed out", b"timeout", b"rate limit", b"denied")
_JSON_ERROR_HEAD = (b'{"error"', b'{"errors"', b'{"message"', b"<html", b"<!doctype")

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
    low = head.lstrip().lower()
    return low.startswith(_JSON_ERROR_HEAD) or any(m in low[:200] for m in _ERROR_MARKERS)


def input_liveness(source: str | os.PathLike | None, now: datetime, *, stale_after: int = DEFAULT_STALE_AFTER) -> Gauge:
    """Whether the input at ``source`` (a file path) is real. A missing source is DEAD, an empty file or an error
    stub (small and error-shaped) is QUIET with no real input, anything else is OK. A source that is not a path
    this host can stat is UNKNOWN: this gauge makes no network call to find out."""
    name = "input_liveness"
    if source is None or not str(source).strip():
        return _gauge(name, UNKNOWN, "the spec names no input source", None, now, stale_after)
    path = Path(str(source)).expanduser()
    try:
        st = path.stat()
    except FileNotFoundError:
        return _gauge(name, DEAD, "the input source is gone", None, now, stale_after)
    except OSError as e:
        return _gauge(name, UNKNOWN, f"the input source cannot be examined ({type(e).__name__})", None, now, stale_after)
    if not path.is_file():
        return _gauge(name, UNKNOWN, "the input source is not a file", None, now, stale_after)
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if st.st_size == 0:
        return _gauge(name, QUIET, "no real input: the file is empty", mtime, now, stale_after)
    try:
        with open(path, "rb") as f:
            head = f.read(STUB_MAX_BYTES + 1)
    except OSError as e:
        return _gauge(name, DEAD, f"the input source cannot be read ({type(e).__name__})", mtime, now, stale_after)
    if len(head) <= STUB_MAX_BYTES and _looks_like_error(head):
        return _gauge(name, QUIET, f"no real input: a {st.st_size}-byte error stub", mtime, now, stale_after)
    return _gauge(name, OK, f"{st.st_size} bytes of input", mtime, now, stale_after)


def _latest(conn: sqlite3.Connection, agent_id: str, state: str | None = None) -> tuple | None:
    sql = ("SELECT state, admitted_at, launched_at, ended_at, reason FROM agent_occurrences WHERE agent_id = ?")
    args: list = [agent_id]
    if state is not None:
        sql += " AND state = ?"
        args.append(state)
    sql += " ORDER BY admitted_at DESC, occurrence_id DESC LIMIT 1"
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
    if destination is None or not str(destination).strip():
        return _gauge(name, UNKNOWN, "the spec names no output destination", ended, now, stale_after)
    try:
        mtime = datetime.fromtimestamp(Path(str(destination)).expanduser().stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return _gauge(name, DEAD, "the last run is done and its destination is missing", ended, now, stale_after)
    except OSError as e:
        return _gauge(name, UNKNOWN, f"the destination cannot be examined ({type(e).__name__})", ended, now, stale_after)
    if began is not None and mtime < began:
        return _gauge(name, DEAD, "the destination is older than the last run", mtime, now, stale_after)
    return _gauge(name, OK, "the destination was written by the last run", mtime, now, stale_after)


def fate_of_output(conn: sqlite3.Connection, agent_id: str, output: dict | None, now: datetime) -> Gauge:
    """What became of the output. A corpus output is not measured. A recovery output means "a drill ran within its
    cadence": OK when a run finished inside ``drill_cadence`` seconds, DEAD when the latest finished run is older,
    UNKNOWN with none. A push output has no fate measure here (its arrival is :func:`output_arrival`)."""
    name = "fate_of_output"
    cls = (output or {}).get("class")
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
    state, admitted, _launched, ended, _reason = row
    when = _parse_stamp(ended) or _parse_stamp(admitted)
    value = {"done": OK, "failed": DEAD, "skipped-no-input": QUIET}.get(state, UNKNOWN)
    return _gauge(name, value, f"the latest run is {state}", when, now, stale_after)
