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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from levain import authority as _auth
from levain.cockpit.firejournal import DB_NAME

__all__ = ["OPEN_BAR_REFUSAL", "PEEK_BUSY_TIMEOUT", "AgentAuthority", "AgentAuthorityError", "AgentCoverMoved",
           "RevokeStep", "agent_fence", "open_bar_for", "revoke_agent_begin", "revoke_agent_finish",
           "revoke_agent_sign"]

_log = logging.getLogger("levain.agent_authority")

# The peek's head read waits at most this long on a lock: a timeout is a head it cannot read.
PEEK_BUSY_TIMEOUT = 0.05
# The refresh's replay runs outside any store transaction and may wait longer.
_REFRESH_BUSY_TIMEOUT = 2.0

_LAUNCHED = "launched"
# the columns the claim wrote (agent_occurrences_identity) or the launch wrote once (..._launch_immutable); the fence from
# the caller's copy, fence_now from the stored row, so the two agree only when the copy is the row's launch
_LAUNCH_COLUMNS = ("agent_id", "link_id", "link_incarnation", "spec_hash", "binding_id", "launch_epoch")


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
        # L3 r1, left: the replay's own error (sqlite3, AnchorError, ...) propagates unwrapped, a raise either way;
        # concurrent refreshes may publish out of order or refuse peeks mid-replay, which fails closed (UNCLASSIFIED).
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
            _auth.check_anchor(state, anchor, path=path)   # an anchor past the held replay raises here
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
        state = self._state     # read once: a concurrent refresh clears it
        if state is None:
            raise AgentCoverMoved("no verified replay is held")
        return _auth.agent_link_incarnation(state, link_id)

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


# L3 r1, left (each fails closed, none reachable to a pass): state is pinned to "launched" on purpose (launched goes
# only to the terminal done/failed/stopped, and no effect fires after); an absent anchor is check_anchor's first-use
# contract, shared with AuthorityCoverage; a close() that raises in replay/_head can only replace one raise with
# another; a malformed grant or key in peek, or an unserialisable digest field, raises out of fence_now (a refusal).
def agent_fence(occurrence: Mapping[str, object], *, authority: AgentAuthority, store_id: str,
                now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                ) -> tuple[str, Callable[[sqlite3.Connection], str]]:
    """``(fence, fence_now)`` for a launched ``occurrence`` (a mapping carrying ``occurrence_id``, ``agent_id``,
    ``link_id``, ``link_incarnation``, ``spec_hash``, ``binding_id`` and ``launch_epoch``).

    ``fence`` is built from those launch columns alone, with state ``launched``, no bar and a live link.
    ``fence_now(conn)`` reads the occurrence's launch columns, epoch and state and the agent's bar on ``conn`` (the
    effect's own transaction) and liveness by :meth:`AgentAuthority.peek` of the stored link, so the check runs on the
    row and a mapping that is not that row's launch never matches. It raises :class:`AgentCoverMoved` when the peek
    does, and :class:`AgentAuthorityError` for an occurrence or bar it cannot read, so a check that cannot complete
    refuses."""
    try:
        occurrence_id = occurrence["occurrence_id"]
        launch = {column: occurrence[column] for column in _LAUNCH_COLUMNS}
    except KeyError as exc:
        raise AgentAuthorityError(f"the occurrence lacks {exc.args[0]!r}: it is not a launched one") from exc
    if type(occurrence_id) is not str or type(launch["launch_epoch"]) is not int \
            or any(type(launch[c]) is not str or not launch[c] for c in _LAUNCH_COLUMNS if c != "launch_epoch"):
        raise AgentAuthorityError("the occurrence carries no launch epoch or link: it was never launched")
    fence = _digest({**launch, "live_incarnation": launch["link_incarnation"], "epoch": launch["launch_epoch"],
                     "state": _LAUNCHED, "barred": False, "live": True})

    def fence_now(conn: sqlite3.Connection) -> str:
        try:
            row = conn.execute(f"SELECT {', '.join(_LAUNCH_COLUMNS)}, epoch, state FROM agent_occurrences"
                               " WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if row is None:
                raise AgentAuthorityError("the occurrence is not in the store")
            stored = dict(zip(_LAUNCH_COLUMNS, row))
            barred = conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                                  (stored["agent_id"],)).fetchone() is not None
            closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                                  (stored["agent_id"],)).fetchone()[0]
        except sqlite3.Error as exc:
            raise AgentAuthorityError(f"the occurrence or its bar cannot be read: {type(exc).__name__}") from exc
        # the bar's revoke_seq goes to agent_link_arms as stored (no int()): a value that is no integer at or
        # above zero is its False, never a truncation that passes. An unlaunched row (NULL link) peeks False.
        live, current = authority.peek(stored["link_id"], agent_id=stored["agent_id"], store_id=store_id, now=now(),
                                       closed_bar_revoke_seq=0 if closed is None else closed)
        return _digest({**stored, "live_incarnation": current or "", "epoch": row[len(_LAUNCH_COLUMNS)],
                        "state": row[len(_LAUNCH_COLUMNS) + 1], "barred": barred, "live": live})

    return fence, fence_now


# --- the agent revoke (A0 section 2.2; BUILD_PLAN P5c-2b) -------------------------------------------------------------
#
# Three steps over the bar rows of ``levain.agents.bars``: begin (the bar, then the prepared link), sign (the gesture,
# recorded on the bar before anything is appended) and finish (the append, then the close). A step reports what
# happened as a :class:`RevokeStep` and returns; the outcomes worth a raise are a link the log refuses and a verifier
# that cannot run (``VerifierUnavailable``, raised as itself: a retry, never a refusal).
# Every write to a bar is guarded ``closed_at IS NULL``: a write that touches no row means an abandon closed the bar
# meanwhile, and the step then says so and returns, because the closed bar is frozen by a trigger and an unguarded
# UPDATE would abort after a revoke had landed. The signature's write and the stale-head re-prepare are also guarded on
# the exact pending link the step read (L3 r1), so a link prepared again in between is reported, never overwritten.
# The cockpit connection is autocommit with no transaction open (``levain.authority.append`` owns its own).

# The refusal a signing surface gives while the agent's revoke is open; the one text for ``sign`` and ``renew``.
OPEN_BAR_REFUSAL = "a revoke is open for this agent: finish or abandon it"

_ABANDONED = "the revoke was abandoned; nothing was appended"
_REPREPARED = "the revoke link was prepared again meanwhile: nothing was recorded; sign it again"


@dataclass(frozen=True)
class RevokeStep:
    """What one revoke step did. ``state`` is one of ``prepared`` (begin: the link waits for a signature),
    ``signed`` (sign: the signature is on the bar), ``landed`` (finish: appended, the bar closed signed),
    ``landed_bar_abandoned`` (appended; an abandon had closed the bar, so R5 records it ``from_log`` at the next tick;
    the revoke stands), ``abandoned`` (the bar was closed before this step's write; nothing was appended),
    ``reprepared`` (another step prepared the bar's link again before this step's write; nothing was recorded or
    appended, and the bar's current link wants a signature) and ``stale_head`` (the head moved: the bar stays open,
    re-prepared, and wants one more signature). ``seq`` is the revoke link's seq where one is known."""
    state: str
    bar_id: str
    seq: int | None
    text: str

    @property
    def stands(self) -> bool:
        """Whether the revoke is in the log."""
        return self.state in ("landed", "landed_bar_abandoned")


def open_bar_for(agent_id: str, store_dir: Path | str | None = None):
    """The agent's open ``bars.Bar`` in the autonomic store at ``store_dir`` (default: the store every ``levain agents``
    verb opens), or None. A read only: it opens the database read-only and never creates the store or its tables, so a
    store with no agent schema, or none at all, has no bar. Anything else that stops the read raises
    :class:`AgentAuthorityError`, because a bar that cannot be read is never taken for "no bar"."""
    from levain.agents import bars
    from levain.autonomic.db import DB_NAME as _STORE_DB, default_store_dir

    path = (default_store_dir() if store_dir is None else Path(store_dir)) / _STORE_DB
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2.0)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_bars'").fetchone() is None:
                return None
            return bars.open_bar_of(conn, agent_id)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AgentAuthorityError(f"the agent's bar cannot be read: {type(exc).__name__}") from exc


def _unsigned(bar) -> _auth.UnsignedLink | None:
    """The bar's pending link as an ``UnsignedLink`` when its four fields are well formed (else None)."""
    rec, prev, seq, fp = bar.pending_record, bar.pending_prev, bar.pending_seq, bar.pending_signer_fp
    if type(rec) is not str or type(prev) is not str or type(seq) is not int or seq < 1 or type(fp) is not str:
        return None
    return _auth.UnsignedLink(seq, prev, rec, fp)


_PENDING = ("pending_record", "pending_prev", "pending_seq", "pending_signer_fp")


def _write(autonomic: sqlite3.Connection, set_sql: str, params: tuple, bar_id: str, *,
           holding: _auth.UnsignedLink | None = None) -> bool:
    """One bar write guarded ``closed_at IS NULL`` and, with ``holding``, on the bar still holding that exact pending
    link (a compare-and-swap: a re-prepare between the read and the write makes it touch no row)."""
    where, extra = "", ()
    if holding is not None:
        where = "".join(f" AND {c} = ?" for c in _PENDING)
        extra = (holding.record, holding.prev_hash, holding.seq, holding.signer_fp)
    return autonomic.execute(f"UPDATE agent_bars SET {set_sql} WHERE bar_id = ? AND closed_at IS NULL{where}",
                             (*params, bar_id, *extra)).rowcount == 1


def _lost(autonomic: sqlite3.Connection, bar_id: str, seq: int | None) -> RevokeStep:
    """The step for a guarded write that touched no row: the bar closed (an abandon) or was prepared again."""
    from levain.agents import bars

    now = bars.read_bar(autonomic, bar_id)
    if now is None or now.closed_at is not None:
        return RevokeStep("abandoned", bar_id, seq, _ABANDONED)
    return RevokeStep("reprepared", bar_id, now.pending_seq, _REPREPARED)


def _prepare_onto(autonomic: sqlite3.Connection, cockpit: sqlite3.Connection, bar_id: str, agent_id: str, *,
                  store_id: str, signer_fp: str, now: datetime, ssh_keygen: Path | str | None,
                  last_error: str | None = None, holding: _auth.UnsignedLink | None = None) -> bool:
    """Prepare the ``revoke_agent`` link on the current head and record it on the bar in ONE guarded UPDATE: the four
    ``pending_*`` fields, the stored signature and its time nulled together (a stale signature never pairs with a new
    record), ``attempts`` up by one and ``last_error`` as given. False: the bar is closed, or (with ``holding``) no
    longer holds that link. The log's own refusal (no agent-link to remove, a signer it does not know) raises
    :class:`AgentAuthorityError`; a verifier that cannot run raises as itself."""
    kw = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        link = _auth.prepare(cockpit, {"op": "revoke_agent", "agent_id": agent_id, "agent_store_id": store_id},
                             signer_fp=signer_fp, now=now, **kw)
    except _auth.VerifierUnavailable:
        raise
    except _auth.AuthorityError as exc:
        raise AgentAuthorityError(f"the revoke cannot be prepared: {exc}") from exc
    return _write(autonomic, "pending_record = ?, pending_prev = ?, pending_seq = ?, pending_signer_fp = ?,"
                             " pending_signature = NULL, pending_signed_at = NULL, attempts = attempts + 1,"
                             " last_error = ?",
                  (link.record, link.prev_hash, link.seq, link.signer_fp, last_error), bar_id, holding=holding)


def revoke_agent_begin(autonomic: sqlite3.Connection, cockpit: sqlite3.Connection, agent_id: str, *, store_id: str,
                       signer_fp: str, clock: Callable[[], datetime],
                       ssh_keygen: Path | str | None = None) -> RevokeStep:
    """Steps 1 and 2. ``store_id`` must be the autonomic store's own (its ``meta`` row, where there is one). The bar is
    written first, with the epoch of every live occurrence of the agent raised, in one ``BEGIN IMMEDIATE`` (a second
    revoke reuses the open bar, and its re-prepare drops any signature already recorded on it); then the link is
    prepared on the current head and its four fields recorded on the bar. The bar stays when the prepare is refused:
    the agent is barred, and recovery (R3, R4) decides what the bar becomes. ``autonomic`` and ``cockpit`` are
    autocommit connections with no transaction open."""
    from levain.agents import bars

    sid, why = bars._store_id(autonomic, store_id)
    if sid is None:
        raise AgentAuthorityError(f"no revoke begun: {why}")
    head = _auth.log_head(cockpit)
    bar = bars.open_bar(autonomic, agent_id, head_seq=0 if head is None else int(head[0]), now=clock())
    if not _prepare_onto(autonomic, cockpit, bar.bar_id, agent_id, store_id=sid, signer_fp=signer_fp, now=clock(),
                         ssh_keygen=ssh_keygen):
        return RevokeStep("abandoned", bar.bar_id, None, _ABANDONED)
    return RevokeStep("prepared", bar.bar_id, bars.read_bar(autonomic, bar.bar_id).pending_seq,
                      "the agent is barred; the revoke link waits for a signature")


def revoke_agent_sign(autonomic: sqlite3.Connection, bar_id: str, sign: Callable[[bytes], str], *,
                      clock: Callable[[], datetime]) -> RevokeStep:
    """Step 3. The link is rebuilt from the bar's own row (so a crash after the gesture loses nothing), signed outside
    any transaction, and the signature and the time it was recorded go to the bar in one UPDATE, before anything is
    appended. That UPDATE finds the bar still holding the link that was signed, or touches no row: a bar prepared
    again during the gesture reports ``reprepared`` and keeps no signature."""
    from levain.agents import bars
    from levain.cockpit.consent import utc_stamp

    bar = bars.read_bar(autonomic, bar_id)
    if bar is None or bar.closed_at is not None:
        return RevokeStep("abandoned", bar_id, None, _ABANDONED)
    link = _unsigned(bar)
    if link is None:
        raise AgentAuthorityError("the bar has no prepared revoke link to sign")
    signature = sign(link.message)
    if not _write(autonomic, "pending_signature = ?, pending_signed_at = ?", (signature, utc_stamp(clock())), bar_id,
                  holding=link):
        return _lost(autonomic, bar_id, link.seq)
    return RevokeStep("signed", bar_id, link.seq, "the signature is recorded on the bar")


def _store_of(link: _auth.UnsignedLink) -> str:
    """The ``agent_store_id`` the bar's pending record names: the store this revoke was begun for."""
    try:
        sid = json.loads(link.record).get("agent_store_id")
    except (ValueError, AttributeError):
        sid = None
    if type(sid) is not str:
        raise AgentAuthorityError("the bar's pending record names no agent store")
    return sid


def _landed(autonomic: sqlite3.Connection, bar_id: str, seq: int, *, now: datetime, warning: str | None) -> RevokeStep:
    """Close the bar signed at ``seq`` after its link is in the log. A close that touches no row reads the frozen row:
    closed signed at ``seq`` (recovery's R1 got there first) is ``landed``; anything else is an abandon (R5's)."""
    from levain.agents import bars

    tail = "" if warning is None else f"; warning: {warning}"
    if not bars.close_bar(autonomic, bar_id, revoke_seq=seq, reason="signed", now=now):
        row = bars.read_bar(autonomic, bar_id)
        if row is None or (row.reason, row.revoke_seq) != ("signed", seq):
            return RevokeStep("landed_bar_abandoned", bar_id, seq,
                              f"revoke landed at seq {seq}; the bar had been abandoned; it is recorded from_log at the "
                              f"next tick (R5){tail}")
    return RevokeStep("landed", bar_id, seq, f"revoke landed at seq {seq}; the bar is closed{tail}")


def _in_log(cockpit: sqlite3.Connection, link: _auth.UnsignedLink, ssh_keygen: Path | str | None) -> bool:
    """Whether the verified, anchored replay of the cockpit's log (:meth:`AgentAuthority.replay` on the connection's
    own file) holds ``link`` itself at its seq: same prev and record. A replay that cannot finish raises
    :class:`AgentAuthorityError` (a verifier that cannot run raises as itself), never a False."""
    try:
        row = next((r for r in cockpit.execute("PRAGMA database_list") if r[1] == "main"), None)
        db = Path(row[2]) if row is not None and row[2] else None
        if db is None or db.name != DB_NAME:
            raise AgentAuthorityError("the cockpit connection is not a cockpit journal on disk")
        state = AgentAuthority(db.parent, ssh_keygen=ssh_keygen).replay()
    except (_auth.VerifierUnavailable, AgentAuthorityError):
        raise
    except Exception as exc:  # noqa: BLE001 - a log that cannot be replayed is never read as "not landed"
        raise AgentAuthorityError(f"the log cannot be replayed to look for this revoke: {type(exc).__name__}") from exc
    return state.hash_at(link.seq) == _auth._link_hash(link.prev_hash, link.record)


def revoke_agent_finish(autonomic: sqlite3.Connection, cockpit: sqlite3.Connection, bar_id: str, *,
                        clock: Callable[[], datetime], anchor: Path | None,
                        ssh_keygen: Path | str | None = None) -> RevokeStep:
    """Steps 4 and 5. The recorded signature is appended; on success the bar closes ``signed`` at the link's seq. A head
    that moved is first checked for the bar's own link (an append that landed before a crash, or recovery's): if the
    verified log holds it, the bar closes ``landed``. Otherwise the bar stays open, records ``stale_head`` and
    re-prepares on the new head for the store its own record names, which drops the stale signature: the caller signs
    once more (the agent stays barred meanwhile). A bar an abandon closed, or another step prepared again, meanwhile
    ends the step with the true outcome, never a raise. A verifier that cannot run raises as itself, with
    ``last_error`` ``verifier_unavailable``: a retry, never a refusal."""
    from levain.agents import bars

    bar = bars.read_bar(autonomic, bar_id)
    if bar is None or bar.closed_at is not None:
        return RevokeStep("abandoned", bar_id, None, _ABANDONED)
    link = _unsigned(bar)
    if link is None or bar.pending_signature is None:
        raise AgentAuthorityError("the bar has no signed revoke link to append")
    store_id = _store_of(link)
    kw = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        try:
            new = _auth.append(cockpit, link, bar.pending_signature, now=clock(), anchor=anchor, **kw)
        except _auth.StaleHead:
            if _in_log(cockpit, link, ssh_keygen):
                return _landed(autonomic, bar_id, link.seq, now=clock(), warning=None)
            try:
                prepared = _prepare_onto(autonomic, cockpit, bar_id, bar.agent_id, store_id=store_id,
                                         signer_fp=link.signer_fp, now=clock(), ssh_keygen=ssh_keygen,
                                         last_error="stale_head", holding=link)
            except AgentAuthorityError:
                bars._guarded(autonomic, "last_error = ?", ("stale_head",), bar_id)    # the bar stays open and says why
                raise
            if not prepared:
                return _lost(autonomic, bar_id, link.seq)
            return RevokeStep("stale_head", bar_id, bars.read_bar(autonomic, bar_id).pending_seq,
                              "the authority log moved: nothing was appended; the revoke link was prepared again and "
                              "needs one more signature")
    except _auth.VerifierUnavailable:
        bars._guarded(autonomic, "last_error = ?", ("verifier_unavailable",), bar_id)
        raise
    except _auth.AuthorityError as exc:
        bars._guarded(autonomic, "last_error = ?", (f"append_refused:{type(exc).__name__}",), bar_id)
        raise AgentAuthorityError(f"the log refused the revoke: {exc}") from exc
    return _landed(autonomic, bar_id, link.seq, now=clock(), warning=new.anchor_warning)
