"""levain.agent_authority — the agent's authority source and the invariant 1 predicate (A0 section 2.3; BUILD_PLAN P5c-1).

:class:`AgentAuthority` reads the cockpit's authority log and never writes it. It is modelled on
``levain.graduation_policy.AuthorityCoverage``: a verified, anchored replay (:meth:`~AgentAuthority.refresh` raises, so
a log that cannot be read is never taken for "nothing live"), then a cheap check (:meth:`~AgentAuthority.peek`) that
reads the anchor, then the log head at a short busy timeout, then the anchor again, and compares them to the replay it
holds. A head or anchor that moved, or cannot be read, raises :class:`AgentCoverMoved`.

:func:`agent_fence` returns ``(fence, fence_now)`` for one occurrence, the pair ``RunJournal.effect`` compares inside
its admission transaction. The expected side is built only from the columns the launch wrote and never changes; the
current side reads the mutable epoch, state and bar in the effect's own transaction, and liveness by peek. The two are
equal exactly when nothing that matters has moved since the launch, so every change only narrows (a STALE) and a read
that cannot finish is an UNCLASSIFIED.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from levain import authority as _auth
from levain.cockpit.firejournal import DB_NAME

__all__ = ["PEEK_BUSY_TIMEOUT", "AgentAuthority", "AgentAuthorityError", "AgentCoverMoved", "agent_fence"]

_log = logging.getLogger("levain.agent_authority")

# The peek's head read waits at most this long on a lock: a timeout is a head it cannot read.
PEEK_BUSY_TIMEOUT = 0.05
# The refresh's replay runs outside any store transaction and may wait longer.
_REFRESH_BUSY_TIMEOUT = 2.0

_LAUNCHED = "launched"


class AgentAuthorityError(Exception):
    """The authority could not be read or does not hold what the caller needs (never read as "nothing live")."""


class AgentCoverMoved(AgentAuthorityError):
    """The cached replay no longer matches the log or its anchor, or the log cannot be read now: re-refresh."""


def _connect(db: Path, timeout: float) -> sqlite3.Connection:
    # read-only: the source never writes the cockpit journal (and never creates one)
    return sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, isolation_level=None, timeout=timeout)


class AgentAuthority:
    """The verified view of the cockpit authority log an agent's liveness is judged from.

    ``cockpit_root`` holds the cockpit journal and the authority anchor. ``ssh_keygen`` and ``cache`` are passed to the
    replay as given. :meth:`refresh` raises on any failure and leaves no cached view; :meth:`peek` raises
    :class:`AgentCoverMoved` unless the cached view still matches the log."""

    def __init__(self, cockpit_root: Path | str, *, ssh_keygen: Path | str | None = None,
                 cache: _auth.ReplayCache | None = None) -> None:
        self._root = Path(cockpit_root)
        self._ssh_keygen = ssh_keygen
        self._cache = cache
        self._state: _auth.AuthoritySnapshot | None = None

    @property
    def cockpit_root(self) -> Path:
        return self._root

    def replay(self) -> _auth.AuthoritySnapshot:
        """The log's verified replay, in one read transaction, checked against the anchor. Raises on any failure."""
        path = _auth.anchor_path(self._root)
        pinned = _auth.read_anchor(path)   # before the transaction's first SELECT
        conn = _connect(self._root / DB_NAME, _REFRESH_BUSY_TIMEOUT)
        try:
            conn.execute("BEGIN")
            try:
                kw = {} if self._ssh_keygen is None else {"ssh_keygen": self._ssh_keygen}
                state = _auth.snapshot(conn, cache=self._cache, **kw)
            finally:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass        # a ROLLBACK that fails never masks the replay's own error
        finally:
            conn.close()
        _auth.check_anchor(state, pinned, path=path)
        return state

    def refresh(self) -> _auth.AuthoritySnapshot:
        """Replay and cache. A failed replay raises and leaves nothing cached, so the next peek refuses."""
        self._state = None
        state = self.replay()
        self._state = state
        return state

    def _head(self) -> tuple[int, str]:
        conn = _connect(self._root / DB_NAME, PEEK_BUSY_TIMEOUT)
        try:
            head = _auth.log_head(conn)
        finally:
            conn.close()
        return (0, "") if head is None else (int(head[0]), str(head[1]))

    def _unmoved(self) -> _auth.AuthoritySnapshot:
        """The cached replay, once the anchor (before), the log head and the anchor (after) agree with it."""
        state = self._state
        if state is None:
            raise AgentCoverMoved("no verified replay is held")
        path = _auth.anchor_path(self._root)
        try:
            anchor = _auth.read_anchor(path)
            if anchor is not None and anchor.store_id == state.store_id and state.seq < anchor.seq:
                raise AgentCoverMoved("the anchor is past the held replay")
            _auth.check_anchor(state, anchor, path=path)
            head = self._head()
            if head != (state.seq, state.head_hash):
                raise AgentCoverMoved("the log head moved")
            if _auth.read_anchor(path) != anchor:
                raise AgentCoverMoved("the anchor moved while the head was read")
        except AgentCoverMoved:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever cannot be read now is a cover that moved
            _log.warning("agent authority: peek failed (%s): %s", type(exc).__name__, exc)
            raise AgentCoverMoved(f"the authority cannot be read: {type(exc).__name__}") from exc
        return state

    def incarnation(self, link_id: str) -> str | None:
        """The held replay's incarnation of ``link_id`` (None: no such link there). Raises if nothing is held."""
        if self._state is None:
            raise AgentCoverMoved("no verified replay is held")
        return _auth.agent_link_incarnation(self._state, link_id)

    def peek(self, link_id: str, *, agent_id: str, store_id: str, now: datetime,
             closed_bar_revoke_seq: int = 0) -> tuple[bool, str | None]:
        """``(live, incarnation)`` of agent link ``link_id`` at ``now``, from the held replay, after the log and the
        anchor are confirmed unmoved (:class:`AgentCoverMoved` otherwise). ``live`` is: the link is unexpired and was
        born after its agent's last revoke and after ``closed_bar_revoke_seq``
        (:func:`levain.authority.agent_link_arms`), it names ``agent_id`` and ``store_id``, and the signers of its born
        and current links are active keys of the replay."""
        state = self._unmoved()
        incarnation = _auth.agent_link_incarnation(state, link_id)
        grant = state.policy.grants.get(link_id)
        if incarnation is None or not _auth.agent_link(grant) \
                or grant["agent_id"] != agent_id or grant["store_id"] != store_id:
            return False, incarnation
        if not _auth.agent_link_arms(state, link_id, now=now, closed_bar_revoke_seq=closed_bar_revoke_seq):
            return False, incarnation
        born, current = state.grant_born.get(link_id), state.grant_seqs.get(link_id)
        signers = state.link_signers
        if born is None or current is None or len(signers) != state.seq \
                or not (1 <= born <= state.seq and 1 <= current <= state.seq):
            return False, incarnation
        for fp in (signers[born - 1], signers[current - 1]):
            key = state.keys.get(fp)
            if key is None or not key.active:
                return False, incarnation
        return True, incarnation


def _digest(fields: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(fields), sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def agent_fence(occurrence: Mapping[str, object], *, authority: AgentAuthority, store_id: str,
                now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                ) -> tuple[str, Callable[[sqlite3.Connection], str]]:
    """``(fence, fence_now)`` for a launched ``occurrence`` (a mapping carrying ``occurrence_id``, ``agent_id``,
    ``link_id``, ``link_incarnation``, ``spec_hash``, ``binding_id`` and ``launch_epoch``).

    ``fence`` is built from those immutable launch columns alone, with state ``launched``, no bar and a live link.
    ``fence_now(conn)`` reads the occurrence's epoch and state and the agent's bar on ``conn`` (the effect's own
    transaction) and liveness by :meth:`AgentAuthority.peek`; it raises :class:`AgentCoverMoved` when the peek does,
    and :class:`AgentAuthorityError` for an occurrence or bar it cannot read, so a check that cannot complete refuses."""
    try:
        occurrence_id = str(occurrence["occurrence_id"])
        agent_id, link_id = str(occurrence["agent_id"]), str(occurrence["link_id"])
        incarnation = str(occurrence["link_incarnation"])
        spec_hash, binding_id = str(occurrence["spec_hash"]), str(occurrence["binding_id"])
        launch_epoch = occurrence["launch_epoch"]
    except KeyError as exc:
        raise AgentAuthorityError(f"the occurrence lacks {exc.args[0]!r}: it is not a launched one") from exc
    if type(launch_epoch) is not int or not link_id or not incarnation:
        raise AgentAuthorityError("the occurrence carries no launch epoch or link: it was never launched")
    fixed = {"agent_id": agent_id, "link_id": link_id, "link_incarnation": incarnation, "spec_hash": spec_hash,
             "binding_id": binding_id}
    fence = _digest({**fixed, "epoch": launch_epoch, "state": _LAUNCHED, "barred": False, "live": True})

    def fence_now(conn: sqlite3.Connection) -> str:
        try:
            row = conn.execute("SELECT epoch, state FROM agent_occurrences WHERE occurrence_id = ?",
                               (occurrence_id,)).fetchone()
            barred = conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                                  (agent_id,)).fetchone() is not None
            closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                                  (agent_id,)).fetchone()[0]
        except sqlite3.Error as exc:
            raise AgentAuthorityError(f"the occurrence or its bar cannot be read: {type(exc).__name__}") from exc
        if row is None:
            raise AgentAuthorityError("the occurrence is not in the store")
        live, current = authority.peek(link_id, agent_id=agent_id, store_id=store_id, now=now(),
                                       closed_bar_revoke_seq=0 if closed is None else int(closed))
        return _digest({**fixed, "link_incarnation": current or "", "epoch": row[0], "state": row[1],
                        "barred": barred, "live": live})

    return fence, fence_now
