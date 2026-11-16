"""Root's host-grant verbs (board levain#66 slice S2, plan hgs2_plan_v2): ``egress-grant``, ``egress-grant-revoke``,
``egress-refusals``, ``egress-grants-list``, ``egress-grants-all``. The handlers are thin: they bind the caller to the session, then compose
:mod:`levain.slot.egress_verify` (the signature and host policy), :mod:`levain.slot.egress_store` (the grants file in
the root-owned ``R/grants/slot<n>/``) and :mod:`levain.slot.egress_ledger` (single use, revocation). Imported lazily by
:mod:`levain.slot.runtime_helper`'s wrappers, as ``start`` is.

``egress-grant`` (plan section 2), in this order: bind the caller; parse the signed bytes; freshness; the chosen expiry
is after root's ``now``; the message's session, slot and store are this session's, this slot's and root's; the host is
not already routed; the signature under the install-wide verify lock (taken and RELEASED before any other lock); then
under the session lock: the session is ACTIVE, the slot dir passes its walk, the old file is read (a bad one is an
empty rebuild), expired entries pruned, one entry per host with the LATEST ISSUED winning, the cap checked; the new
file is staged in a unique temp, the ledger row is committed (the commit point of single use) under the global
ledger lock, and only then is the temp renamed to ``grants.json``. Freshness and the chosen expiry are checked again at
the commit with the commit's own time (the signature check and two lock waits ran since the first ``now``).

Failure semantics: before the ledger commit, any failure unlinks only the temp, the old file stays in force and the tap
is NOT burned (``egress-grant-write-failed``); after it and before the rename, the same but the tap IS burned (a new
card); a failure to sync the directory after the rename is ``egress-grant-unsure`` (the new file is in force and may not
survive a power loss; the tap is burned). "Append before rename" orders a PROCESS crash only; power-loss ordering rests
on the full sync of the ledger and its directory completing before the rename is issued.

Root's ``/usr/bin/python3`` (3.9) runs this: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from levain.broker.policy import is_host_name

from levain.contracts import ContractError
from levain.slot import Refused
from levain.slot import egress_grants as eg
from levain.slot import egress_ledger as EL
from levain.slot import egress_store as ES
from levain.slot import egress_verify as EV
from levain.slot import records as R
from levain.slot.ids import SlotId
from levain.slot import runtime_helper as H
from levain.slot.runtime_grammar import OPERATOR, EgressGrantInput, RuntimeRequest

ERR_CLASS = "egress-grant-class"
ERR_KIND = "egress-grant-kind"
GRANTABLE_KINDS = ("interactive", "login")      # an allowlist: an unknown kind fails closed
ERR_SESSION = "egress-grant-session"
ERR_STORE = "egress-grant-store"
ERR_EXPIRED = "egress-grant-expired"
ERR_NOT_ACTIVE = "egress-grant-not-active"
ERR_SUPERSEDED = "egress-grant-superseded"
ERR_WRITE = "egress-grant-write-failed"
ERR_UNSURE = "egress-grant-unsure"
ERR_TOO_MANY = "too-many"
ERR_ALL_UNREAD = "egress-grants-unreadable"
ERR_ALL_MISMATCH = "egress-grants-slot-mismatch"
ERR_LOG = "egress-refusals-log"
ERR_CURSOR = "egress-refusals-cursor"
#: ``egress-refusals``: at most this many hosts and this many log bytes scanned per call (a long log is read over
#: several calls, never one unbounded one).
REFUSALS_HOSTS_MAX = 8
REFUSALS_SCAN_MAX = 256 * 1024
_READ_CHUNK = 64 * 1024
_FRONT_LOG, _FRONT_LOG_ROTATED = "front.log", "front.log.1"


def bind(ctx: H.Ctx, session: str) -> R.Alloc:
    """The alloc of ``session`` for an OPERATOR caller that owns it, on an egress slot; ``no-session`` for every other
    pairing (a caller learns nothing of another's sessions: not a dispatch caller, not another operator's session)."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (Refused, ContractError, ValueError):
        raise Refused("no-session", session) from None
    if a.session != session or ctx.caller != OPERATOR or a.operator_uid != ctx.sudo_uid:
        raise Refused("no-session", session)
    if a.slot.slot_class != "egress":
        raise Refused(ERR_CLASS, session)
    if a.kind not in GRANTABLE_KINDS:
        raise Refused(ERR_KIND, session)
    return a


def _now(ctx: H.Ctx) -> datetime:
    """Root's clock as an aware UTC datetime (never the message's)."""
    return datetime.fromtimestamp(float(ctx.seams.clock()), tz=timezone.utc)


def _active(ctx: H.Ctx, session: str) -> bool:
    """CONTROL's state record, read as a hint (the caller holds the session lock END also takes): ACTIVE and no other
    state admits a grant (a BRAKED session is paused: no tap lands while it is)."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    if data is None:
        return False
    try:
        return R.parse_state(data).state == R.ACTIVE
    except (Refused, ContractError, ValueError):
        return False


def _entry_dict(e: eg.Entry) -> Dict[str, object]:
    return {"grant": e.grant, "host": e.host, "expires": eg.stamp(e.expires), "choice": e.choice}


def egress_grant(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    if not isinstance(blob, EgressGrantInput):
        raise Refused("missing-stdin", "egress-grant")
    session = str(req.session)
    a = bind(ctx, session)
    now = _now(ctx)
    # A. before any lock, no write
    try:
        signed = eg.signed_bytes(blob.message, blob.choice)
        grant = eg.parse_signed(signed, writable_at_root=True)
        eg.check_fresh(grant, now)
    except eg.GrantRefused as e:
        raise EV.refusal_of(e) from None
    if grant.expires <= now:
        raise Refused(ERR_EXPIRED, "the chosen grant is already over")
    if grant.session != session or grant.slot != a.slot.index:
        raise Refused(ERR_SESSION, "the message is for another session or slot")
    with EL.ledger_dir(ctx) as dfd:
        if grant.store != EL.read_store_id(ctx, dfd):
            raise Refused(ERR_STORE, "the message names another store")
        EV.host_class(ctx, grant.host)
        with EL.verify_lock(ctx, dfd):
            EV.check_signature(ctx, signed, blob.sig, blob.signer_fp)
        # B. under the session lock
        with H.session_lock(ctx, session):
            if not _active(ctx, session):
                raise Refused(ERR_NOT_ACTIVE, "the session is not ACTIVE")
            with ES.open_slot_dir(ctx, a.slot) as sd:
                _publish(ctx, a, session, grant, sd, dfd, now)
                ES.sweep_temps(sd)
    return {"grant": grant.grant, "host": grant.host, "expires": eg.stamp(grant.expires), "choice": grant.choice}


def _publish(ctx: H.Ctx, a: R.Alloc, session: str, grant: eg.HostGrant, sd: ES.SlotDir, dfd: int,
             now: datetime) -> None:
    at = ctx.seams.at
    live = [e for e in ES.read_current(ctx, a.slot, session, sd) if e.expires > now]
    used = EL.read_used(ctx, dfd)
    # An id already used or revoked is named so before the replace-by-host rule can call it superseded; the commit under
    # the ledger lock (EL.record_used) is the authoritative check, this one only chooses the clearer refusal.
    if EL.used_for(used, grant.grant) is not None:
        raise Refused("egress-grant-used", "that grant was already published")
    if any(r.grant == grant.grant and r.keep_until > now for r in EL.read_revoked(ctx, dfd)):
        raise Refused("egress-grant-revoked", "that grant was revoked before it arrived")
    same = [e for e in live if e.host == grant.host]
    if same:
        row = EL.used_for(used, same[0].grant)
        if row is None:
            raise Refused(EL.ERR_LEDGER, "a live entry has no used row")
        if grant.issued <= row.issued:
            raise Refused(ERR_SUPERSEDED, "a grant issued later already holds that host")
        live.remove(same[0])
    live.append(eg.entry_from_grant(grant))
    if len(live) > eg.GRANTS_MAX:
        raise Refused(ERR_TOO_MANY, "a session holds no more host grants than this")
    try:
        raw = eg.dump_grants(session, a.slot.index, live)
    except eg.GrantRefused:
        raise Refused(ERR_WRITE, "the grants file cannot be built") from None
    tmp = None  # type: Optional[str]
    spent = False
    try:
        tmp = ES.stage(ctx, sd, raw, at)
        at("egress-grant:ledger")
        with EL.ledger_lock(ctx, dfd):
            at_commit = _now(ctx)
            try:
                eg.check_fresh(grant, at_commit)
            except eg.GrantRefused as e:
                raise EV.refusal_of(e) from None
            if grant.expires <= at_commit:
                raise Refused(ERR_EXPIRED, "the chosen grant ran out while the tap waited")
            spent = True                    # from here a failure may leave the used row in force
            EL.record_used(ctx, dfd, grant, at_commit)
        at("egress-grant:committed")
        ES.commit(ctx, sd, tmp, at)
    except ES.RenamedNotDurable:
        raise Refused(ERR_UNSURE, "the new file is in force and may not survive a power loss") from None
    except Refused:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise
    except OSError as e:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise Refused(ERR_WRITE, "the grant could not be written; the old file is in force%s (%s)"
                      % ("; the tap may be spent" if spent else "", e.strerror or type(e).__name__)) from None
    except BaseException:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise


def _removal(ctx: H.Ctx, a: R.Alloc, session: str, target: str, sd: Optional[ES.SlotDir]):
    """``(remaining entries, removed ids, unknown ids)`` for revoking ``target`` (``all`` or one id) from the file."""
    current = ES.read_current(ctx, a.slot, session, sd) if sd is not None else []
    if target == "all":
        return [], [e.grant for e in current], []
    if any(e.grant == target for e in current):
        return [e for e in current if e.grant != target], [target], []
    return current, [], [target]


def egress_grant_revoke(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    session, target = str(req.session), str(req.target)
    a = bind(ctx, session)
    now = _now(ctx)
    with EL.ledger_dir(ctx) as dfd:
        with H.session_lock(ctx, session):
            try:
                sd = ES.open_slot_dir(ctx, a.slot)    # type: Optional[ES.SlotDir]
            except Refused as e:
                if e.name != ES.NOT_READY:
                    raise
                sd = None                             # nothing was ever published here; an unknown id is still recorded
            try:
                remaining, removed, unknown = _removal(ctx, a, session, target, sd)
                if removed and sd is not None:
                    try:
                        raw = eg.dump_grants(session, a.slot.index, remaining)
                        ES.publish(ctx, sd, raw, ctx.seams.at)
                    except ES.RenamedNotDurable:
                        raise Refused(ERR_UNSURE, "the narrowed file is in force and may not survive a power loss") \
                            from None
                    except (eg.GrantRefused, OSError):
                        raise Refused(ERR_WRITE, "the narrowed file could not be written; the old one is in force") \
                            from None
                if unknown:
                    with EL.ledger_lock(ctx, dfd):
                        try:
                            EL.record_revoked(ctx, dfd, unknown, now)
                        except EL.LedgerWriteFailed:
                            raise Refused(ERR_WRITE, "the revocation may not be recorded: revoke it again") from None
            finally:
                if sd is not None:
                    sd.close()
    return {"removed": removed, "recorded": unknown}


class _Log:
    """One open front log file that passed the trust check, with its inode and size at open."""

    def __init__(self, fd: int, st: os.stat_result) -> None:
        self.fd, self.ino, self.size = fd, st.st_ino, st.st_size


def _open_log(dfd: int, name: str, owners: Tuple[int, ...]) -> Optional[_Log]:
    """``name`` below the judged front log dir: absent is ``None``; a link, a FIFO or other non-regular file, a file with
    more than one link, or one owned by neither the front account nor root is ``egress-refusals-log`` (the trust
    :func:`levain.slot.runtime_helper.read_session_tunnel_total` holds the log to)."""
    try:
        fd = os.open(name, H._O_READ, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(ERR_LOG, "the front log cannot be opened") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in owners:
        os.close(fd)
        raise Refused(ERR_LOG, "the front log is not the front's regular file")
    return _Log(fd, st)


def _refused_host(raw: bytes, session: str) -> Optional[str]:
    """The host of one log line when it is ``session``'s ``refused``/``host-not-allowed``/``off-list`` record with a
    valid lowercase host name (the front logs only a host that passed its own grammar, so a record that does not is
    forged or damaged); else None."""
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or rec.get("session") != session or rec.get("event") != "refused":
        return None
    if rec.get("refused") != "host-not-allowed" or rec.get("detail") != "off-list" or rec.get("host_invalid"):
        return None
    host = rec.get("host")
    if type(host) is not str or len(host) > 253 or host != host.lower() or not is_host_name(host):
        return None
    return host


def _scan(log: _Log, offset: int, budget: int, session: str, hosts: List[str]) -> Tuple[int, int, bool]:
    """Read ``log`` from ``offset`` for at most ``budget`` bytes, adding ``session``'s off-list host refusals to
    ``hosts`` (deduped, at most :data:`REFUSALS_HOSTS_MAX`; it stops right after the line that makes the last one).
    Returns ``(offset after the last whole line consumed, bytes read, reached the end of the file)``. A partial last
    line is left for the next call; one that fills a WHOLE per-call budget (:data:`REFUSALS_SCAN_MAX`) with no newline
    in it is junk and skipped, so a cursor cannot wedge on it. A line cut short only because an earlier file of this
    call used up most of the budget is left for the next call, never skipped."""
    pos, scanned, buf, eof = offset, 0, b"", False
    while len(hosts) < REFUSALS_HOSTS_MAX:
        chunk = b""
        if scanned < budget:
            chunk = os.pread(log.fd, min(_READ_CHUNK, budget - scanned), pos + len(buf))
            scanned += len(chunk)
            eof = not chunk
        buf += chunk
        while len(hosts) < REFUSALS_HOSTS_MAX:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            host = _refused_host(buf[:nl], session)
            if host is not None and host not in hosts:
                hosts.append(host)
            buf = buf[nl + 1:]
            pos += nl + 1
        if not chunk:
            break
    if pos == offset and len(buf) >= REFUSALS_SCAN_MAX:
        pos += len(buf)
    return pos, scanned, eof


def _parse_cursor(cursor: str) -> Optional[Tuple[int, int]]:
    if cursor == "0":
        return None
    ino, _, off = cursor.partition("-")
    return int(ino), int(off)


def egress_refusals(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    """``{hosts, cursor}``: the hosts this session's front refused as off-list since ``cursor`` (``0``, or
    ``<inode>-<offset>`` of the last whole line read). The log is the slot's ``L/gw/slot<n>/front.log`` through the
    trusted walk root's END uses, so a slot-owned or swapped log is never read. The cursor names the log's INODE, so a
    rotation is followed: an offset into the inode now called ``front.log.1`` finishes that file, then ``front.log``
    from 0; an inode that is neither restarts at 0 of ``front.log``; an offset past the end of the file it names is
    refused ``egress-refusals-cursor``. Each call reads at most :data:`REFUSALS_SCAN_MAX` bytes and answers at most
    :data:`REFUSALS_HOSTS_MAX` hosts (the next call goes on from the cursor)."""
    a = bind(ctx, str(req.session))
    session = str(req.session)
    want = _parse_cursor(str(req.cursor))
    try:
        front_uid = H.slot_ids(ctx, a.slot)[a.slot.accounts[1]]
        dfd = H._open_front_log_dir(ctx.store, a.slot, front_uid)
    except Refused:
        raise Refused(ERR_LOG, "the front log dir is not the front's") from None
    if dfd is None:
        raise Refused(ERR_LOG, "no front account")
    owners = (ctx.seams.chowned_uid(front_uid), 0)
    opened = []  # type: List[_Log]
    try:
        cur = _open_log(dfd, _FRONT_LOG, owners)
        if cur is not None:
            opened.append(cur)              # closed below even when the rotated file's open refuses
        rot = _open_log(dfd, _FRONT_LOG_ROTATED, owners)
        if rot is not None:
            opened.append(rot)
        plan = []  # type: List[Tuple[_Log, int]]
        if want is not None and cur is not None and want[0] == cur.ino:
            plan = [(cur, want[1])]
        elif want is not None and rot is not None and want[0] == rot.ino:
            plan = [(rot, want[1])] + ([(cur, 0)] if cur is not None else [])
        elif cur is not None:
            plan = [(cur, 0)]
        for log, off in plan:
            if off > log.size:
                raise Refused(ERR_CURSOR, "the cursor is past the end of the log it names")
        hosts = []  # type: List[str]
        budget = REFUSALS_SCAN_MAX
        cursor = "0"
        for log, off in plan:
            pos, scanned, eof = _scan(log, off, budget, session, hosts)
            budget -= scanned
            cursor = "%d-%d" % (log.ino, pos)
            if log is rot and eof and len(hosts) < REFUSALS_HOSTS_MAX:
                continue                    # the rotated file is final: finish it, then the current one from 0
            break
        return {"hosts": hosts, "cursor": cursor}
    finally:
        for x in opened:
            os.close(x.fd)
        os.close(dfd)


def egress_grants_list(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    """``{store, entries}``: root's grant-store id (what a card's ``store`` must be) and the session's live entries."""
    session = str(req.session)
    a = bind(ctx, session)
    now = _now(ctx)
    with EL.ledger_dir(ctx) as dfd:
        store_id = EL.read_store_id(ctx, dfd)
    try:
        sd = ES.open_slot_dir(ctx, a.slot)   # type: Optional[ES.SlotDir]
    except Refused as e:
        if e.name != ES.NOT_READY:
            raise
        sd = None
    try:
        live = [e for e in (ES.read_current(ctx, a.slot, session, sd) if sd is not None else []) if e.expires > now]
    finally:
        if sd is not None:
            sd.close()
    return {"store": store_id, "entries": [_entry_dict(e) for e in live]}


#: ``egress-grants-all`` lists a session in these CONTROL states only (a dead front's leftover file is never shown live).
_LISTED_STATES = (R.ACTIVE, R.BRAKED)


def _session_state(ctx: H.Ctx, session: str) -> Optional[str]:
    """CONTROL's state name for ``session`` (``None`` when there is no record: prepare's window between its alloc and
    ATTACH, a session never ACTIVE). A record that cannot be read or parsed raises, and one that names another session
    is ``egress-grants-slot-mismatch`` (the caller names the slot ``unread``, never an absent one)."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    if data is None:
        return None
    rec = R.parse_state(data)
    if rec.session != session:
        raise Refused(ERR_ALL_MISMATCH, "the CONTROL record names another session")
    return rec.state


def _slot_alloc(ctx: H.Ctx, slot: SlotId, ss: R.SlotState) -> Optional[R.Alloc]:
    """The alloc of the session slot state ``ss`` leases ``slot`` to, or ``None`` for a session this verb never shows:
    the reaper's orphan (no alloc and no CONTROL record), another operator's, or an unattended kind. Not :func:`bind`,
    which folds a corrupt alloc into ``no-session``: here an alloc that is gone under a record or does not parse is
    ``egress-grants-unreadable`` and one that is not this slot's lease is ``egress-grants-slot-mismatch``."""
    session = str(ss.session)
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        if ctx.store.read(("sessions",), f"{session}.json", trusted=False) is None:
            return None                     # only a clean prepare failure unlinks an alloc, while no record exists
        raise Refused(ERR_ALL_UNREAD, "the alloc is gone under a CONTROL record")
    try:
        a = R.parse_alloc(data)
    except (Refused, ContractError, ValueError):
        raise Refused(ERR_ALL_UNREAD, "the alloc does not parse") from None
    if a.session != session:
        raise Refused(ERR_ALL_MISMATCH, "the alloc names another session")
    if a.operator_uid != ctx.sudo_uid:
        return None                         # another operator's session: invisible by design, never named
    if a.slot != slot or a.lease_seq != ss.lease_seq:
        raise Refused(ERR_ALL_MISMATCH, "the alloc is not this slot's lease")
    return a if a.kind in GRANTABLE_KINDS else None    # an unattended session: invisible by design


def _slot_row(ctx: H.Ctx, slot: SlotId, now: datetime) -> Optional[Dict[str, object]]:
    """One configured egress slot's row for :func:`egress_grants_all`, or ``None`` when it is not to be listed (not
    leased, another operator's session, an unattended kind, a state that is not ACTIVE or BRAKED). Raises ``Refused``,
    ``OSError`` or a parse error for a slot root could not read: the caller names it."""
    raw = ctx.store.read(("slots", slot.tag), "state")
    if raw is None:
        return None
    ss = H.parse_slot_state_of(raw, slot)
    if ss.state != "leased" or ss.session is None:
        return None
    a = _slot_alloc(ctx, slot, ss)
    if a is None:
        return None
    state = _session_state(ctx, ss.session)
    if state not in _LISTED_STATES:
        return None
    try:
        sd = ES.open_slot_dir(ctx, slot)    # type: Optional[ES.SlotDir]
    except Refused as e:
        if e.name != ES.NOT_READY:
            raise
        sd = None                           # nothing was ever published here
    try:
        live = [e for e in (ES.read_current(ctx, slot, ss.session, sd) if sd is not None else []) if e.expires > now]
    finally:
        if sd is not None:
            sd.close()
    return {"slot": slot.index, "session": ss.session, "kind": a.kind, "state": state,
            "entries": [_entry_dict(e) for e in live]}


def egress_grants_all(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    """``{store, slots, unread}``: for the operator caller, every configured egress slot that is leased to one of the
    caller's interactive or login sessions in CONTROL state ACTIVE or BRAKED, with that session's live entries
    (``slots``: ``{slot, session, kind, state, entries}``). It walks the configured slots (at most 64, each 16 grants),
    never the alloc records of every session ever prepared, so its answer is bounded. A slot whose directory or file
    root cannot read is a named ``unread`` row (``{slot, name}``), never an omission; every other slot still lists.
    No session lock (as ``egress-grants-list``): the state is a hint, the file is root's whole rename. A missing grants
    store is the refusal ``egress-grants-list`` gives."""
    if ctx.caller != OPERATOR:              # the owner check would skip every slot: an empty answer is not a refusal
        raise Refused("verb-not-allowed-for-caller", "egress-grants-all")
    now = _now(ctx)
    with EL.ledger_dir(ctx) as dfd:
        store_id = EL.read_store_id(ctx, dfd)
    slots, unread = [], []   # type: List[Dict[str, object]], List[Dict[str, object]]
    for idx in sorted(H.configured_slots(ctx.store)["egress"]):
        slot = SlotId("egress", idx)
        try:
            row = _slot_row(ctx, slot, now)
        except Refused as e:
            unread.append({"slot": idx, "name": e.name})
            continue
        except (OSError, ContractError, ValueError):
            unread.append({"slot": idx, "name": ERR_ALL_UNREAD})
            continue
        if row is not None:
            slots.append(row)
    return {"store": store_id, "slots": slots, "unread": unread}


__all__ = ["bind", "egress_grant", "egress_grant_revoke", "egress_grants_all", "egress_grants_list", "egress_refusals"]
