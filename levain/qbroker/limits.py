"""levain.qbroker.limits — the limits a slot session runs under, read from root's ``L/policy/limits``.

Slot limits design (``project_memory/1025-6/limits/DESIGN.md`` §3; head rulings R0 and R1). :class:`PolicyLimits` is
the broker's :data:`levain.qbroker.broker.LimitsSource`: called with (agent, session) on every limited GATE, it returns
exactly :data:`levain.slot.limits_doc.SLOT_KINDS`, or raises :class:`LimitsUnavailable` (any other exception inside it
is wrapped into one). It never returns ``None`` (the broker reads ``None`` from its callable as a fault too, never as
"no limits").

- The file MISSING (the policy directory itself sound): :data:`~levain.slot.limits_doc.DEFAULTS` for the slot kinds
  (Q13 F5b: on by default; R0 ruling 1).
- PRESENT BUT UNUSABLE: raises, so every limited GATE answers ``limits-failed`` (R0 ruling 2): not a regular file, a
  symlink, an owner other than root, writable by group or others, over
  :data:`~levain.slot.limits_doc.SLOT_LIMITS_MAX` bytes, or a document :func:`levain.slot.limits_doc.slot_doc` refuses.
  No last-good fallback: defaults or an older file may be looser than what the operator set.
- The policy directory absent, a symlink, not root's or writable by group or others: raises too (the store is broken).

Each call opens the directory (``O_DIRECTORY | O_NOFOLLOW``) and the file beneath it by that descriptor
(``O_NOFOLLOW | O_NONBLOCK``); every check is made on the opened file's ``fstat``, and the parsed document is kept
under that ``fstat``'s identity, so the bytes checked are the bytes read and an unchanged file is not read again
(R1 F4). An unusable file is logged once per identity. Stdlib only.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any

from levain.slot import limits_doc

__all__ = ["POLICY_LIMITS", "LimitsUnavailable", "PolicyLimits"]

log = logging.getLogger("levain.qbroker")

# the file's name under L/policy/ (root writes it through the install helper's set-limits; this module only reads it)
POLICY_LIMITS = "limits"


class LimitsUnavailable(Exception):
    """The slot's limits cannot be read: ``reason`` says why and what fixes it (the GATE's ``limits-failed``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class PolicyLimits:
    """``PolicyLimits(L / "policy")(agent, session)``: the limits in force for ``agent`` (the session does not choose
    them: one document per host). ``owner``: the uid the directory and the file must belong to (root; a test passes
    its own)."""

    def __init__(self, policy_dir: Path | str, *, owner: int = 0) -> None:
        self._dir = Path(policy_dir)
        self._owner = owner
        self._mutex = threading.Lock()
        # (identity, the checked document or None, the refusal or None), of the last file read
        self._cached: tuple[tuple[int, ...], dict[str, Any] | None, str | None] | None = None
        self._logged: tuple[int, ...] | None = None

    @property
    def path(self) -> Path:
        return self._dir / POLICY_LIMITS

    def __call__(self, agent: str, session: str) -> dict[str, float]:
        """Raises only :class:`LimitsUnavailable`: any other failure here is wrapped into one naming it."""
        try:
            doc = self._document()
            if doc is None:
                return {k: float(limits_doc.DEFAULTS[k]) for k in limits_doc.SLOT_KINDS}
            try:
                got = limits_doc.slot_values(doc, agent)
            except limits_doc.LimitsDocError as exc:   # slot_doc checked the document; an agent with no block
                raise LimitsUnavailable(self._unusable(exc.code)) from None
            return {k: float(got[k]) for k in limits_doc.SLOT_KINDS}
        except LimitsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an fstat, a read or the parser failing is the same fail-closed answer
            raise LimitsUnavailable(self._unusable(f"it could not be read ({type(exc).__name__}: {exc})"[:200])) \
                from None

    def _unusable(self, why: str) -> str:
        return (f"{self.path} is not usable: {why}; the operator rewrites it with levain limits set --slots")

    def _document(self) -> dict[str, Any] | None:
        """The checked document, ``None`` when the file is missing; raises :class:`LimitsUnavailable`."""
        try:
            pfd = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise LimitsUnavailable(f"the policy directory {self._dir} cannot be opened "
                                    f"({errno.errorcode.get(exc.errno, type(exc).__name__)})") from None
        try:
            pst = os.fstat(pfd)
            if not stat.S_ISDIR(pst.st_mode) or pst.st_uid != self._owner or pst.st_mode & 0o022:
                raise LimitsUnavailable(f"the policy directory {self._dir} is not a directory of uid {self._owner} "
                                        "writable by it alone")
            try:
                fd = os.open(POLICY_LIMITS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pfd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                why = "a symlink" if exc.errno == errno.ELOOP else \
                    f"it cannot be opened ({errno.errorcode.get(exc.errno, type(exc).__name__)})"
                raise LimitsUnavailable(self._unusable(why)) from None
        finally:
            os.close(pfd)
        try:
            st = os.fstat(fd)
            ident = _identity(st)
            with self._mutex:
                cached = self._cached
            if cached is not None and cached[0] == ident:
                doc, why = cached[1], cached[2]
            else:
                doc, why = self._check(fd, st)
                with self._mutex:
                    self._cached = (ident, doc, why)
        finally:
            os.close(fd)
        if why is not None:
            reason = self._unusable(why)
            with self._mutex:
                first = self._logged != ident
                self._logged = ident
            if first:
                log.warning("qbroker: %s; every limited GATE fails closed", reason)
            raise LimitsUnavailable(reason)
        return doc

    def _check(self, fd: int, st: os.stat_result) -> tuple[dict[str, Any] | None, str | None]:
        """(the document, None) or (None, why it is refused), from the opened file and its ``fstat``."""
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_uid != self._owner:
            return None, f"owned by uid {st.st_uid}, not {self._owner}"
        if st.st_mode & 0o022:
            return None, f"writable by group or others (mode {stat.S_IMODE(st.st_mode):04o})"
        if st.st_size > limits_doc.SLOT_LIMITS_MAX:
            return None, f"over {limits_doc.SLOT_LIMITS_MAX} bytes"
        try:
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(limits_doc.SLOT_LIMITS_MAX + 1)
        except OSError as exc:
            return None, f"it cannot be read ({errno.errorcode.get(exc.errno, type(exc).__name__)})"
        try:
            return limits_doc.slot_doc(data), None
        except limits_doc.LimitsDocError as exc:
            return None, str(exc)
