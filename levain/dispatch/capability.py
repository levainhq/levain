"""levain.dispatch.capability — the planner's capability record: how a filing session gets its jobs' capabilities back.

The store hands a job's capability to the filing planner once and keeps only its hash (:mod:`levain.dispatch.jobs`).
A planner whose session restarts, or whose context lost the filing response, would otherwise be left unable to
recommend or cancel its own job. So the filing door keeps the capability in that session's own record (design §3.2:
"a restarted planner session regains it from its own session record"): one file per filing session,
``<dispatch_dir>/capabilities/<sha256(agent + "\\0" + session_id)>.json``, holding ``{format, agent, session_id,
jobs: {job_id: capability}}``. It is written whole (a temporary file, then a rename) under a per-record lock, so a
reader sees the old record or the new one and two filings from one session never lose each other's job. The file is
0600 and its directory 0700, both the operator's, never through a symlink; a record or directory that is anything
else is refused (:class:`CapabilityRecordError`), as the job store refuses its own directory.

A session that cannot be told apart from others gets NO record: an absent session (the doors fall back to
:data:`NO_SESSION`, which every such caller would share), a malformed one (empty, too long, holding a control
character) or a subagent's (whose id carries ``#agent:``, the question band's rule). For those the filing response
says the capability is shown once and is not kept.

Known edge: until S1 places the control principal (design §8), any process of the operator's uid can read this record,
the same edge the job store already states; the capability separates sessions by intent, not by the OS, until then.
Stdlib only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from levain.dispatch import DispatchError

__all__ = ["AGENTS", "DIR_NAME", "FORMAT", "NO_SESSION", "CapabilityRecordError", "keep", "preflight", "recall",
           "record_path", "session_problem"]

FORMAT = "levain-dispatch-capabilities/1"
DIR_NAME = "capabilities"
AGENTS = ("claude_code", "codex")
# the filer session a door names when the caller's session is unknown: shared by every such caller, so never a record
NO_SESSION = "levain-dispatch"
# the question band's names for the same two cases, refused here too so a session from either door means one thing
_BAND_NO_SESSION = "levain-ask"
_SUBAGENT = "#agent:"
_SESSION_MAX = 256
# a record is a few hundred bytes per job; this bound only stops a planted file from being read whole
_RECORD_MAX = 1024 * 1024
_ACCESS_BY_OTHERS = 0o077
_JOB_ID = re.compile(r"dj-[0-9a-f]{32}")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{16,128}")


class CapabilityRecordError(DispatchError):
    """A capability record or its directory that cannot be trusted: reachable by others, not the operator's, a
    symlink, or not a record. A fault on this machine, never a verdict on the caller's request."""


def session_problem(session: Any) -> str | None:
    """Why ``session`` cannot key a record (absent, malformed, shared or a subagent's), or ``None``."""
    if not isinstance(session, str) or not session:
        return "no session was named"
    if session in (NO_SESSION, _BAND_NO_SESSION):
        return "no session was named"
    if len(session) > _SESSION_MAX:
        return f"the session id is over {_SESSION_MAX} characters"
    if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp") for c in session):
        return "the session id holds a control character"
    if _SUBAGENT in session:
        return "the session is a subagent's"
    return None


def _check_agent(agent: Any) -> str:
    if agent not in AGENTS:
        raise CapabilityRecordError(f"agent must be one of {AGENTS}")
    return agent


def record_path(directory: Path | str, agent: str, session: str) -> Path:
    """Where ``agent``'s session ``session`` keeps its record under the dispatch directory ``directory``."""
    name = hashlib.sha256(f"{_check_agent(agent)}\0{session}".encode("utf-8", "surrogatepass")).hexdigest()
    return Path(directory) / DIR_NAME / f"{name}.json"


def _private_dir(path: Path, *, create: bool) -> bool:
    """True when ``path`` is a directory that is the operator's alone; False when it is absent and ``create`` is
    off. Anything else is refused."""
    if create and not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)          # the umask can only narrow it
        except FileExistsError:
            pass
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        raise CapabilityRecordError(f"{path} is not a directory (a symlink is refused)")
    if st.st_uid != os.getuid():
        raise CapabilityRecordError(f"{path} is not owned by uid {os.getuid()}")
    if st.st_mode & _ACCESS_BY_OTHERS:
        raise CapabilityRecordError(f"{path} is reachable by group or others (mode {st.st_mode & 0o777:o}); it must "
                                    "be 0700")
    return True


def _read(path: Path, agent: str, session: str) -> dict[str, str] | None:
    """The record's ``jobs``, or ``None`` when there is no record."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CapabilityRecordError(f"capability record {path} cannot be opened ({exc.strerror or exc}); a "
                                    "symlink is refused") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CapabilityRecordError(f"capability record {path} is not a regular file")
        if st.st_uid != os.getuid():
            raise CapabilityRecordError(f"capability record {path} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise CapabilityRecordError(f"capability record {path} is reachable by group or others (mode "
                                        f"{st.st_mode & 0o777:o}); it must be 0600")
        raw = fh.read(_RECORD_MAX + 1)
    if len(raw) > _RECORD_MAX:
        raise CapabilityRecordError(f"capability record {path} is larger than a record")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CapabilityRecordError(f"capability record {path} is not a record") from None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != {"format", "agent", "session_id", "jobs"}
            or data["format"] != FORMAT or not isinstance(jobs, dict)
            or not all(isinstance(k, str) and _JOB_ID.fullmatch(k) and isinstance(v, str) and _CAPABILITY.fullmatch(v)
                       for k, v in jobs.items())):
        raise CapabilityRecordError(f"capability record {path} is not a {FORMAT} record")
    if data["agent"] != agent or data["session_id"] != session:
        # the file name is the hash of these two: a record naming another session was put here, not written here
        raise CapabilityRecordError(f"capability record {path} names another session")
    return dict(jobs)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                        # closing the descriptor releases the lock


def keep(directory: Path | str, agent: str, session: Any, job_id: str, capability: str) -> Path | None:
    """Keep ``capability`` for ``job_id`` in ``agent``'s session ``session``'s record under the dispatch directory
    ``directory``; returns the record's path, or ``None`` when the session gets no record
    (:func:`session_problem`)."""
    if session_problem(session) is not None:
        return None
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise CapabilityRecordError(f"{str(job_id)[:40]!r} is not a dispatch job id")
    if not isinstance(capability, str) or _CAPABILITY.fullmatch(capability) is None:
        raise CapabilityRecordError("that is not a job capability")
    path = record_path(directory, agent, session)
    if not _private_dir(path.parent.parent, create=False):
        raise CapabilityRecordError(f"the dispatch directory {path.parent.parent} does not exist")
    _private_dir(path.parent, create=True)
    with _locked(path):
        jobs = _read(path, agent, session) or {}
        jobs[job_id] = capability
        body = json.dumps({"format": FORMAT, "agent": agent, "session_id": session, "jobs": jobs},
                          sort_keys=True, ensure_ascii=True).encode("ascii")
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return path


def preflight(directory: Path | str, agent: str, session: Any) -> None:
    """Refuse (:class:`CapabilityRecordError`) when :func:`keep` would refuse ``agent``'s session ``session``'s record
    for a reason already on disk: a directory that is not the operator's private one or that cannot be written, or a
    record that is not one. Run before filing, so a record that cannot be kept stops the filing, not the keeping. A
    session that gets no record (:func:`session_problem`) and a directory not made yet pass."""
    if session_problem(session) is not None:
        return
    path = record_path(directory, agent, session)
    for d in (path.parent.parent, path.parent):
        if not _private_dir(d, create=False):
            return
        if not os.access(d, os.W_OK | os.X_OK):
            raise CapabilityRecordError(f"{d} cannot be written by uid {os.getuid()}")
    # the lock keep() takes, taken here too: a lock path keep() could not open stops the filing, not the keeping
    try:
        with _locked(path):
            _read(path, agent, session)
    except OSError as exc:
        raise CapabilityRecordError(f"capability record {path} cannot be locked: {exc}") from None


def recall(directory: Path | str, agent: str, session: Any, job_id: str) -> str | None:
    """The capability ``agent``'s session ``session`` kept for ``job_id``, or ``None`` (no usable session, no
    record, or no entry for that job)."""
    if session_problem(session) is not None:
        return None
    path = record_path(directory, agent, session)
    if not _private_dir(path.parent, create=False):
        return None
    # no lock: a record is only ever replaced whole by a rename, so a read sees the old one or the new one
    jobs = _read(path, agent, session)
    return None if jobs is None else jobs.get(job_id)
