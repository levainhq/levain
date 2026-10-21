"""levain.qbroker.store — the broker's rows under ``L/queue/`` (SCHEMAS §4; dirs and owners CONTRACT c3 §2.1).

``requests/<request_id>.json`` is written by the broker only: a temp file in the same directory, fsynced, mode
0640, then hard-linked to its name (never replacing an existing row: a second writer finds the first one's row)
and the directory fsynced. ``requests/grant-<request_id>.json`` is the broker's own allow for a request the
signed dispatch contract's typed grant covers (:data:`GRANT_ROW_FORMAT`, W2C_DESIGN §4), written the same way beside
the request row: in the directory only the broker writes, never in ``answers/``, which any member of
``levain-queue`` (dispatch, the suspect principal, included) can create files in. ``requests/queue-id`` holds the queue's random id, made once. ``answers/<id>.json`` is
written by the operator's cockpit / phone server; any member of ``levain-queue`` can create a file there, so the
broker reads it as untrusted bytes (no symlink, a regular file, bounded) and trusts only its signature.
Stdlib only.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Any

from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse

__all__ = ["ANSWER_ROW_FORMAT", "GRANT_ROW_FORMAT", "REQUEST_ROW_FORMAT", "ROW_MAX", "QueueStore", "fsync_dir",
           "replace_file", "valid_request_id"]

REQUEST_ROW_FORMAT = "levain-queue-request/1"
ANSWER_ROW_FORMAT = "levain-queue-answer/1"
# the broker's allow under a signed contract's typed grant: never an answer row, never a consent grant answer
GRANT_ROW_FORMAT = "levain-queue-grant-allow/1"
_GRANT_PREFIX = "grant-"
# a row holds one request (its display is bounded by levain.consent.record.DISPLAY_MAX) or one signed answer
ROW_MAX = 64 * 1024
ROW_MODE = 0o640
_QUEUE_ID = "queue-id"
# levain.autonomic.pending.seal_pending_id's shape: pend-<received_at stamp>-<16 hex>
_REQUEST_ID = re.compile(r"pend-[0-9A-Za-z:.+-]{1,40}-[0-9a-f]{16}")


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


def _read_untrusted(path: Path) -> bytes | None:
    """The bytes of a regular file of at most :data:`ROW_MAX`, or ``None`` when there is no file; ``ValueError``
    for anything else at that name (a symlink, a FIFO, an oversize file)."""
    got = _read_untrusted_stamped(path)
    return None if got is None else got[0]


def _read_untrusted_stamped(path: Path) -> tuple[bytes, tuple[int, ...]] | None:
    """:func:`_read_untrusted`, with the file's identity (dev, ino, size, mtime, ctime) from the fd it was read on."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path.name} is a symlink") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > ROW_MAX:
            raise ValueError(f"{path.name} is not a regular file of at most {ROW_MAX} bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(ROW_MAX + 1)
        if len(data) > ROW_MAX:
            raise ValueError(f"{path.name} grew past {ROW_MAX} bytes")
        return data, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    finally:
        os.close(fd)


def _parsed(data: bytes) -> dict[str, Any]:
    try:
        obj = parse(data)
    except ContractError as exc:
        raise ValueError(str(exc)) from None
    if not isinstance(obj, dict):
        raise ValueError("a row is a JSON object")
    return obj


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


_fsync_dir = fsync_dir


def _write_tmp(directory: Path, data: bytes, mode: int) -> Path:
    """A new temp file in ``directory`` holding ``data``, fsynced, mode ``mode`` (never through a symlink)."""
    tmp = directory / f".tmp-{os.getpid()}-{os.urandom(8).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def replace_file(path: Path, data: bytes, mode: int = ROW_MODE) -> None:
    """``data`` at ``path``, whole or not at all: temp + fsync + rename over whatever is there + directory fsync."""
    tmp = _write_tmp(path.parent, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    fsync_dir(path.parent)


class QueueStore:
    """``L/queue``: both directories must exist (setup makes them); nothing here creates them."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.requests = self.root / "requests"
        self.answers = self.root / "answers"
        for d in (self.requests, self.answers):
            if not d.is_dir() or d.is_symlink():
                raise FileNotFoundError(f"{d} is not a directory (setup makes it)")
        self.queue_id = self._queue_id()

    def _queue_id(self) -> str:
        path = self.requests / _QUEUE_ID
        data = _read_untrusted(path)
        if data is None:
            fresh = "qb-" + os.urandom(16).hex()
            try:
                self._link_new(path, fresh.encode("ascii"))
            except FileExistsError:
                pass
            data = _read_untrusted(path)
        text = (data or b"").decode("ascii", "replace")
        if not re.fullmatch(r"qb-[0-9a-f]{32}", text):
            raise ValueError(f"{path} does not hold a queue id")
        return text

    def _link_new(self, path: Path, data: bytes) -> None:
        """Write ``data`` at ``path`` only if nothing is there: temp + fsync + link (FileExistsError if taken)."""
        tmp = _write_tmp(path.parent, data, ROW_MODE)
        try:
            os.link(tmp, path, follow_symlinks=False)
        finally:
            os.unlink(tmp)
        _fsync_dir(path.parent)

    def request_path(self, request_id: str) -> Path:
        if not valid_request_id(request_id):
            raise ValueError("not a request id")
        return self.requests / f"{request_id}.json"

    def read_request(self, request_id: str) -> dict[str, Any] | None:
        data = _read_untrusted(self.request_path(request_id))
        if data is None:
            return None
        row = _parsed(data)
        if row.get("format") != REQUEST_ROW_FORMAT:
            raise ValueError("not a levain-queue-request/1 row")
        return row

    def write_request(self, row: dict[str, Any]) -> bool:
        """True when this call wrote the row; False when a row of that id was already there."""
        try:
            self._link_new(self.request_path(row["request"]["id"]), canonical_bytes(row))
        except FileExistsError:
            return False
        return True

    def grant_path(self, request_id: str) -> Path:
        if not valid_request_id(request_id):
            raise ValueError("not a request id")
        return self.requests / f"{_GRANT_PREFIX}{request_id}.json"

    def write_grant(self, row: dict[str, Any]) -> bool:
        """The broker's grant-allow row for ``row["request_id"]``; True when this call wrote it, False when one was
        already there (never replaced)."""
        if row.get("format") != GRANT_ROW_FORMAT:
            raise ValueError(f"not a {GRANT_ROW_FORMAT} row")
        try:
            self._link_new(self.grant_path(row["request_id"]), canonical_bytes(row))
        except FileExistsError:
            return False
        return True

    def read_grant(self, request_id: str) -> dict[str, Any] | None:
        """The grant-allow row for ``request_id``, ``None`` when there is none; ``ValueError`` for a file at that
        name that is not one (another format included: a row is read only as its own format)."""
        data = _read_untrusted(self.grant_path(request_id))
        if data is None:
            return None
        row = _parsed(data)
        if row.get("format") != GRANT_ROW_FORMAT:
            raise ValueError(f"not a {GRANT_ROW_FORMAT} row")
        return row

    def has_grant(self, request_id: str) -> bool:
        try:
            os.lstat(self.grant_path(request_id))
        except FileNotFoundError:
            return False
        return True

    def read_answer(self, request_id: str) -> dict[str, Any] | None:
        """The answer row as parsed JSON, ``None`` when there is none; ``ValueError`` for a file that is not one."""
        got = self.read_answer_stamped(request_id)
        return None if got is None else got[0]

    def read_answer_stamped(self, request_id: str) -> tuple[dict[str, Any], tuple[int, ...]] | None:
        """:meth:`read_answer` with the identity of the file it parsed, taken from the fd it read."""
        if not valid_request_id(request_id):
            raise ValueError("not a request id")
        got = _read_untrusted_stamped(self.answers / f"{request_id}.json")
        return None if got is None else (_parsed(got[0]), got[1])

    def has_answer(self, request_id: str) -> bool:
        """Whether anything is at the answer row's name (whatever it holds)."""
        try:
            os.lstat(self.answers / f"{request_id}.json")
        except FileNotFoundError:
            return False
        return True

    def rows(self) -> list[dict[str, Any]]:
        """Every readable request row (the quota's count at start); an unreadable one is skipped."""
        out = []
        for path in self.requests.glob("pend-*.json"):
            try:
                row = self.read_request(path.name[:-len(".json")])
            except (ValueError, OSError):
                continue
            if row is not None:
                out.append(row)
        return out
