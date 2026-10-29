"""levain.graduation_policy — the gate's coverage source: graduation policies replayed from the cockpit's authority log
and anchored in root's allowed-signers file (14b DESIGN_REV3 §3, §5.3; DESIGN_REV3_1 D2, D3).

:class:`AuthorityCoverage` implements :class:`levain.autonomic.coverage.CoverageSource` over three things it reads and
never writes: the cockpit journal's ``authority_log`` (replayed and verified by :func:`levain.authority.snapshot`,
checked against the authority anchor), root's allowed-signers file, and the id of the autonomic store the gate runs in.

- :meth:`AuthorityCoverage.refresh` reads the root file's bytes once, then replays the log in one read transaction,
  and builds a :class:`~levain.autonomic.coverage.CoverageSnapshot` from that replay's own ``(seq, head_hash)``, those
  bytes and the store id. Each graduation policy carries the public keys of the signers of its born link and of its
  current link (:func:`levain.authority.graduation_links`), so the snapshot's ``live`` test is membership of both in
  root's file. Any failure caches :class:`~levain.autonomic.coverage.Uncovered` at the token a peek reads.
- :meth:`AuthorityCoverage.peek` reads the root file's digest, then (for a covered snapshot) the authority anchor, which
  must still admit the replay the snapshot was built from, then the log head (one SELECT at a 50 ms busy timeout),
  then (for a covered snapshot) the anchor again, which must be the anchor it read first; then it compares in pure
  code: the cached cover value, or ``cover:moved``. The anchor reads are what see a ``cockpit.db`` restored to the
  head a snapshot was built at after a later link advanced the anchor: the first sees a restore before it, the second
  one that lands between the first and the head read.
- :meth:`AuthorityCoverage.peek_cover` is that same read, answering the policy whose cover value it is beside the
  value (:class:`~levain.autonomic.coverage.EffectiveCover`); :meth:`~AuthorityCoverage.peek` is its value.

A read that fails gives a fixed sentinel in its half of the token (:data:`UNREADABLE_DIGEST`, :data:`UNREADABLE_HEAD`),
the same in a failed refresh and in a peek, so a peek after a failed refresh matches it and answers ``none``
(CONFIRM), and a peek of a covered snapshot that cannot read answers ``cover:moved``.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from levain import authority as _auth
from levain.autonomic import confirm as _confirm
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.autonomic.coverage import (COVER_MOVED, CoverageSnapshot, CoverageView, Effect, EffectiveCover,
                                       GraduationPolicy, Uncovered, peek_cover_snapshot, signers_digest)
from levain.cockpit.firejournal import DB_NAME
from levain.trust_anchor import trust_anchor_problem

__all__ = ["PEEK_BUSY_TIMEOUT", "RENEW_NEEDS_ANCHOR", "UNREADABLE_DIGEST", "UNREADABLE_HEAD", "AuthorityCoverage",
           "TermedGraduationPolicy", "graduation_policies"]

_log = logging.getLogger("levain.graduation_policy")

# H1 (HEAD_RULINGS_R1 R6): a renew counts only if its signer is root-listed too. False is rev 2's fallback: only the
# born link's signer is anchored, and the current link's signer is taken to be the born one's.
RENEW_NEEDS_ANCHOR = True

# The peek's head read waits at most this long on a lock (DESIGN_REV3 §5.3): a timeout is a head it cannot read.
PEEK_BUSY_TIMEOUT = 0.05
# The refresh's replay may wait longer: it runs outside any store transaction.
_REFRESH_BUSY_TIMEOUT = 2.0

# The token halves of a read that failed: the same in a failed refresh's Uncovered and in a peek.
UNREADABLE_DIGEST = "unreadable"
UNREADABLE_HEAD: tuple[int, str] = (-1, "unreadable")

_MOVED = EffectiveCover(COVER_MOVED, None, None)

# the graduation grants _graduation_policy has warned have no grant_born entry (one warning per grant per process)
_UNBORN_WARNED: set[str] = set()


def _connect(db: Path, timeout: float) -> sqlite3.Connection:
    # read-only: the source never writes the cockpit journal (and never creates one)
    return sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, isolation_level=None, timeout=timeout)


@dataclass(frozen=True)
class TermedGraduationPolicy(GraduationPolicy):
    """A replayed graduation policy with its signed ``min_clean_fires``: the clean fires the honest engine waits for
    before it graduates a binding under this policy (``levain.autonomic.graduation.graduate_due``). Advisory against
    a writer of the levain home; the gate never reads it.

    The term has one source, the signed terms (1026-21 RULINGS G1-5, 1026-22 RULINGS G1): construction reads it from
    them and takes no value from a caller; terms that carry none are refused (the authority log requires the term on
    every graduation policy, so only a policy built outside it can lack one)."""

    min_clean_fires: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if "min_clean_fires" not in self._terms:
            raise ValueError("the policy's terms carry no signed min_clean_fires")
        signed = self._terms["min_clean_fires"]
        if type(signed) is not int or signed < 0:
            raise ValueError("min_clean_fires is a non-negative integer")
        object.__setattr__(self, "min_clean_fires", signed)

    @classmethod
    def of(cls, policy: GraduationPolicy) -> "TermedGraduationPolicy":
        """``policy`` with its signed ``min_clean_fires``, read from its terms."""
        return cls(**{f.name: getattr(policy, f.name) for f in fields(GraduationPolicy) if f.init})


def graduation_policies(state: _auth.AuthoritySnapshot) -> tuple[GraduationPolicy, ...]:
    """Every graduation policy ``state`` holds, each with its incarnation and the public keys of the signers of its
    born and current links (with :data:`RENEW_NEEDS_ANCHOR` false, the born signer's for both). A policy whose links or
    keys ``state`` cannot name, either of whose signers the log has since revoked, or whose record
    :meth:`GraduationPolicy.from_record` refuses, is left out: it covers nothing. Each is a
    :class:`TermedGraduationPolicy`, carrying its signed ``min_clean_fires``."""
    out: list[GraduationPolicy] = []
    for gid, grant in sorted(state.policy.grants.items()):
        try:
            policy = _graduation_policy(state, gid, grant)
        except Exception as exc:  # noqa: BLE001 - one policy that cannot be read covers nothing, alone
            _log.warning("graduation policy %r covers nothing: %s: %s", gid, type(exc).__name__, exc)
            continue
        if policy is not None:
            out.append(policy)
    return tuple(out)


def _graduation_policy(state: _auth.AuthoritySnapshot, gid: str, grant: object) -> GraduationPolicy | None:
    """The policy grant ``gid`` is (:func:`graduation_policies`), None when it is not a graduation policy or covers
    nothing; it raises for a record :meth:`GraduationPolicy.from_record` or :class:`TermedGraduationPolicy` refuses."""
    if not _auth.graduation_grant(grant):
        return None
    incarnation = _auth.graduation_incarnation(state, gid)
    links = _auth.graduation_links(state, gid)
    born_seq = state.grant_born.get(gid)
    if born_seq is None and gid not in _UNBORN_WARNED:
        # a graduation grant the replay holds with no born link is a replay defect, not a revoke: say it, once
        _UNBORN_WARNED.add(gid)
        _log.warning("graduation policy %r covers nothing: the replay has no grant_born entry for it", gid)
    if incarnation is None or links is None or born_seq is None:
        return None
    born_key, current_key = (state.keys.get(fp) for fp in links)
    # RULINGS_S1 item 2: a signer the log has revoked anchors nothing, whatever root's file lists
    if born_key is None or current_key is None or not (born_key.active and current_key.active):
        return None
    born = born_key.public_key
    current = current_key.public_key if RENEW_NEEDS_ANCHOR else born
    record = _auth._thaw(grant)
    # the log requires min_clean_fires on every graduation policy; the engine walks it, read from the signed terms
    return TermedGraduationPolicy.of(
        GraduationPolicy.from_record(record, incarnation=incarnation, born=born_seq, born_signer=born,
                                     current_signer=current))


class AuthorityCoverage:
    """The production :class:`~levain.autonomic.coverage.CoverageSource` (DESIGN_REV3 §5.3).

    ``cockpit_root`` holds the cockpit journal and the authority anchor (``levain.authority.anchor_path``).
    ``allowed_signers`` is root's allowed-signers file: an absolute path that must pass ``trust_anchor_problem`` for
    the owners :mod:`levain.autonomic.confirm` trusts, or nothing is covered. ``store_id`` is the autonomic store's
    own id (its ``meta`` row): only a policy signed for it covers. :meth:`refresh` and :meth:`peek` never raise."""

    def __init__(self, cockpit_root: Path | str, *, allowed_signers: Path | str, store_id: str,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, cache: _auth.ReplayCache | None = None,
                 on_refresh_read: Callable[[], None] | None = None) -> None:
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id is the autonomic store's id")
        self._root = Path(cockpit_root)
        self._signers = Path(allowed_signers)
        self._store_id = store_id
        self._ssh_keygen = ssh_keygen
        self._cache = cache
        # the cached view and, for a CoverageSnapshot, the verified replay it was built from (one tuple, so one
        # assignment replaces both)
        self._cached: tuple[CoverageView, _auth.AuthoritySnapshot | None] | None = None
        # test seam: runs after refresh has read its inputs and before it stores the snapshot (DESIGN_REV3_1 D2's
        # barrier tests); production passes nothing
        self._on_refresh_read = on_refresh_read

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def cockpit_root(self) -> Path:
        """The cockpit data root whose authority log this source replays."""
        return self._root

    def _root_bytes(self) -> bytes | None:
        """The root file's bytes, or None when it is missing, unreadable or not a trust anchor (never raises)."""
        try:
            problem, path = trust_anchor_problem(self._signers, what="allowed-signers file",
                                                 owners=_confirm._SIGNERS_FILE_OWNERS)
            if problem is not None or path is None:
                _log.warning("graduation coverage: %s: nothing is covered", problem)
                return None
            return Path(path).read_bytes()
        except Exception as exc:  # noqa: BLE001 - a root file that cannot be checked or read covers nothing
            _log.warning("graduation coverage: allowed-signers file unreadable (%s): %s: nothing is covered",
                         type(exc).__name__, exc)
            return None

    def _digest(self) -> str:
        """The root file's digest, :data:`UNREADABLE_DIGEST` when it cannot be read (never raises)."""
        try:
            raw = self._root_bytes()
            return UNREADABLE_DIGEST if raw is None else signers_digest(raw)
        except Exception:  # noqa: BLE001 - the sentinel is the digest of a file that cannot be read
            return UNREADABLE_DIGEST

    def _head(self) -> tuple[int, str]:
        """``(seq, hash)`` of the log's last stored link (one indexed SELECT), :data:`UNREADABLE_HEAD` when it cannot
        be read, ``(0, "")`` for an empty log (never raises)."""
        try:
            conn = _connect(self._root / DB_NAME, PEEK_BUSY_TIMEOUT)
            try:
                head = _auth.log_head(conn)
            finally:
                conn.close()
            return (0, "") if head is None else (int(head[0]), str(head[1]))
        except Exception as exc:  # noqa: BLE001 - a head that cannot be read or converted is the sentinel
            _log.warning("graduation coverage: authority head unreadable (%s): %s", type(exc).__name__, exc)
            return UNREADABLE_HEAD

    def replay(self, *, check_anchor: bool = True) -> _auth.AuthoritySnapshot:
        """The log's verified replay, in one read-only transaction: the read ``refresh`` makes, for a caller that
        reports on the log (``levain doctor``) rather than decides coverage. ``check_anchor=False`` leaves the
        anchor comparison to the caller, who then holds the replay even where the anchor refuses it."""
        return self._replay(check_anchor=check_anchor)

    def _replay(self, *, check_anchor: bool = True) -> _auth.AuthoritySnapshot:
        """The verified, anchored replay of the log, in one read transaction."""
        pinned = _auth.read_anchor(_auth.anchor_path(self._root))   # before the transaction's first SELECT
        conn = _connect(self._root / DB_NAME, _REFRESH_BUSY_TIMEOUT)
        try:
            conn.execute("BEGIN")
            try:
                state = _auth.snapshot(conn, cache=self._cache, ssh_keygen=self._ssh_keygen)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass        # a ROLLBACK that fails (the transaction already ended) never masks the replay's error
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        if check_anchor:
            _auth.check_anchor(state, pinned, path=_auth.anchor_path(self._root))
        return state

    def _anchor_admits(self, state: _auth.AuthoritySnapshot) -> tuple[bool, _auth.Anchor | None]:
        """``(admits, anchor)``: the authority anchor, read now, and whether it still admits ``state``
        (:func:`levain.authority.check_anchor`); an anchor that cannot be read admits nothing, and no anchor file
        admits it (as at refresh). An anchor of this store past the replay's seq is a link since the refresh, logged
        at DEBUG: from here it cannot be told from a restore behind it, and the next refresh's replay judges which.
        Every other refusal (an unreadable anchor, another store's, another hash at a seq the replay holds) is a
        WARNING."""
        path = _auth.anchor_path(self._root)
        try:
            anchor = _auth.read_anchor(path)
            if anchor is not None and anchor.store_id == state.store_id and state.seq < anchor.seq:
                _log.debug("graduation coverage: the anchor is past the cached replay (seq %d < %d): re-decide",
                           state.seq, anchor.seq)
                return False, None
            _auth.check_anchor(state, anchor, path=path)
        except Exception as exc:  # noqa: BLE001 - an anchor that refuses or cannot be read forces a re-decide
            _log.warning("graduation coverage: the anchor refuses the cached replay (%s): %s", type(exc).__name__, exc)
            return False, None
        return True, anchor

    def _anchor_unchanged(self, before: _auth.Anchor | None) -> bool:
        """Whether the authority anchor, read again now, is ``before`` (an anchor that cannot be read is not)."""
        try:
            if _auth.read_anchor(_auth.anchor_path(self._root)) == before:
                return True
            _log.debug("graduation coverage: the anchor moved while the head was read: re-decide")
            return False
        except Exception as exc:  # noqa: BLE001 - an anchor that cannot be read forces a re-decide
            _log.warning("graduation coverage: the anchor cannot be read again (%s): %s", type(exc).__name__, exc)
            return False

    def refresh(self) -> CoverageView:
        """Replay, parse and cache (DESIGN_REV3_1 D2): the root file's bytes once, then the log, and a snapshot
        whose token is built from that replay's head and those bytes. A failure of any kind caches
        :class:`Uncovered` at the token a peek reads (digest first, head last)."""
        view: CoverageView
        state: _auth.AuthoritySnapshot | None = None
        try:
            raw = self._root_bytes()
            if raw is None:
                raise _auth.AuthorityError("no trusted allowed-signers file")
            state = self._replay()
            if self._on_refresh_read is not None:
                self._on_refresh_read()
            view = CoverageSnapshot((state.seq, state.head_hash), raw, graduation_policies(state), self._store_id)
        except Exception as exc:  # noqa: BLE001 - a refresh that cannot finish covers nothing
            _log.warning("graduation coverage: refresh failed (%s): %s: nothing is covered", type(exc).__name__, exc)
            state = None
            try:
                digest = self._digest()
                view = Uncovered(self._head(), digest, self._store_id)
            except Exception:  # noqa: BLE001 - the fallback token is the two sentinels
                view = Uncovered(UNREADABLE_HEAD, UNREADABLE_DIGEST, self._store_id)
        self._cached = (view, state)
        return view

    def peek(self, effect: Effect, now: datetime) -> str:
        """The cover value the cached snapshot gives ``effect`` at ``now`` if the root file and the log head are
        what it was built from and the anchor still admits its replay, else ``cover:moved``. For a covered snapshot
        the anchor is read before the head SELECT and again after it, and the two reads must agree: an anchor that
        advanced while the head was read (a link, then a restore of ``cockpit.db`` behind it) is ``cover:moved``.
        A refused first read answers ``cover:moved`` without reading the head. The value :meth:`peek_cover` reads."""
        return self.peek_cover(effect, now).value

    def peek_cover(self, effect: Effect, now: datetime) -> EffectiveCover:
        """:meth:`peek`'s read, answering the value with the rung and the policy that set it, all from the one cached
        view this call read (the cache is read once, before any other read, and nothing here refreshes it)."""
        cached = self._cached
        try:
            if cached is None:
                return _MOVED
            view, state = cached
            digest = self._digest()
            if state is None:
                return peek_cover_snapshot(view, (self._head(), digest, self._store_id), effect, now)
            admits, anchor = self._anchor_admits(state)
            if not admits:
                return _MOVED
            head = self._head()
            if not self._anchor_unchanged(anchor):
                return _MOVED
            return peek_cover_snapshot(view, (head, digest, self._store_id), effect, now)
        except Exception as exc:  # noqa: BLE001 - forces a re-decide
            _log.warning("graduation coverage: peek failed (%s): %s", type(exc).__name__, exc)
            return _MOVED
