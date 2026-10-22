"""The reaper's pass logic (CONTRACT §7.7, reaper DESIGN §4): the boot pass, the PASS WORKER's serial pass and the
REVOKER's front kills. The program that runs them (a supervising main process, the worker process, the revoker process,
each single-threaded) is :mod:`levain.slot.services.reaper`.

The reaper is root and acts through the helpers' own functions, never a second copy of them: END is
:func:`end.end_session` / :func:`end.reconcile_session` on the facts and seams :mod:`start` builds, SCRUB is
:func:`start.scrub_and_release`, the front kill is :func:`start.revoke_front`, every slot-uid kill is
:func:`proc.end_domain` (reaper DESIGN I1). What it reads of CONTROL's records is a HINT
(§7.1): a REVOKE or CAS it sends is checked by CONTROL.

The pass (§4.3), in this fixed order; each step try-locks and skips what is busy, and one item that fails is logged
and named in the heartbeat, never the end of the pass:
0 the ``*.new`` sweep · 1 dead-holder REVOKE · 2 journal reconciliation · 3 END, the HELD retry, and SCRUB again for a
RETAINED session whose slot is still leased to it (RH5) · 3a the orphan ``leased`` revert · 3b/3c, 5 seams · 4 BRAKE
below V5 · 6 reserve refill · 7 pool health · 8 the heartbeat. With CONTROL down only the local steps run (0, 6, 8).
Lock order: pool -> slot -> session END lock; the reaper never takes the install lock.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import end as E
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot.grammar import SESSION_RE
from levain.slot.ids import SlotId
from levain.slot.layout import RESERVE_BYTES, RUN, Row, base_run_rows, slot_run_rows
from levain.slot.root_intent import fsync_dir, pending_sessions

#: CONTRACT §11 V11: the pass worker's tick, and the revoker's when no front kill is pending.
V11_S = 60.0
#: §11 V18: wakes are coalesced to passes at least this far apart (start to start).
V18_S = 1.0
#: §11 V22: the cap on a HELD session's retry backoff.
V22_S = 3600.0
#: DECISION (reaper DESIGN §4.3 step 3): the HELD backoff's first step, doubling per attempt to V22.
HELD_BACKOFF_S = 60.0
#: §11 V23: how long a SLOW CONTROL answer is retried before the revoker treats CONTROL as down (§4.5).
V23_S = 10.0
#: DECISION: the revoker's tick while a front kill is pending (a ``front-busy`` retried inside the revoker's bound).
REVOKER_RETRY_S = 0.25
#: DECISION (§4.2 step 4): the worker's boot waits this long for CONTROL to answer, then passes run anyway (a pass
#: with CONTROL down runs the local steps only).
CONTROL_BOOT_WAIT_S = 30.0
#: DECISION: the bound on one boot-pass ``ip netns`` command.
NETNS_STEP_S = 30.0

#: ``create_at``'s temp below ``L/alloc/``: ``<session>.json.<16 hex>.new``.
_TEMP_TAIL = len(".json.") + 16 + len(".new")
#: The mark force-retire (b) writes (reaper DESIGN §4.6 (b), seat ruling S3-1): a slot that may never be reissued.
SPENT = "spent"
HEARTBEAT = "pass.json"


def _log_to_stderr(msg: str) -> None:
    try:
        sys.stderr.write(f"levain reaper[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001 - a log line never changes what the reaper does
        pass


@dataclass(frozen=True)
class Tuning:
    """The sizes a host test shrinks; production keeps the contract's values."""

    reserve_bytes: int = RESERVE_BYTES
    floor_bytes: int = E.V5_FLOOR_BYTES


def temp_session(name: str) -> Optional[str]:
    """The session a ``create_at`` temp of its alloc names, or None for any other name."""
    if not name.endswith(".new") or len(name) <= _TEMP_TAIL:
        return None
    session, rest = name[:-_TEMP_TAIL], name[-_TEMP_TAIL:]
    hexpart = rest[len(".json."):-len(".new")]
    if not rest.startswith(".json.") or len(hexpart) != 16 or any(c not in "0123456789abcdef" for c in hexpart):
        return None
    return session if SESSION_RE.fullmatch(session) else None


def _slots(store: H.Store) -> List[SlotId]:
    conf = H.configured_slots(store)
    return [SlotId(cls, i) for cls in ("egress", "none") for i in sorted(conf[cls])]


def _slot_state(store: H.Store, slot: SlotId) -> Optional[R.SlotState]:
    raw = store.read(("slots", slot.tag), "state")
    return H.parse_slot_state_of(raw, slot) if raw is not None else None


def _exists(store: H.Store, parts: Sequence[str], name: str, *, trusted: bool = True) -> bool:
    try:
        with store.dir(*parts, trusted=trusted) as fd:
            return _lexists_at(fd, name)
    except Refused as e:
        if e.name == "store-missing":
            return False
        raise


def _lexists_at(dirfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


class _SlotLock:
    """A slot's lock TRIED (``held`` False = busy: skip it this pass); released on exit."""

    def __init__(self, store: H.Store, slot: SlotId) -> None:
        with store.dir("slots", slot.tag) as sfd:
            self.fd = H._open_lock(sfd, "lock", f"slots/{slot.tag}/lock")
        try:
            self.held = H.try_flock(self.fd)
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self) -> "_SlotLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        os.close(self.fd)


class _PoolLock(_SlotLock):
    def __init__(self, store: H.Store) -> None:      # noqa: D107 - the same TRY, on slots/pool.lock
        with store.dir("slots") as fd:
            self.fd = H._open_lock(fd, "pool.lock", "slots/pool.lock")
        try:
            self.held = H.try_flock(self.fd)
        except BaseException:
            os.close(self.fd)
            raise


@contextmanager
def _reserve_lock(path: Path) -> Iterator[bool]:
    """The reserve's own lock, TRIED (False = busy: skip the refill this pass): ``flock`` on ``L/reserve`` itself, so
    a release serializes with one ``flock`` on the file it already opens, and no module is shared.
    Held across the refill's fstat, write and fsync. A reserve not yet there is made here, its name made durable,
    so the file the lock names is the one the refill writes."""
    created = not os.path.lexists(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if created:
            fsync_dir(path.parent)
        yield H.try_flock(fd)
    finally:
        os.close(fd)


def _name(e: BaseException) -> str:
    return e.name if isinstance(e, Refused) else type(e).__name__


class _Item:
    """One per-item loop body (the module's rule): an exception is logged and named in the report's ``skipped`` as
    ``<what>:<name>``, and the loop goes on to the next item."""

    def __init__(self, log: Callable[[str], None], rep: Dict[str, object], what: str) -> None:
        self.log, self.rep, self.what = log, rep, what

    def __enter__(self) -> "_Item":
        return self

    def __exit__(self, et: object, e: Optional[BaseException], _tb: object) -> bool:
        if not isinstance(e, Exception):
            return False
        self.log(f"{self.what}: {type(e).__name__}: {e}")
        self.rep["skipped"].append(f"{self.what}:{_name(e)}")          # type: ignore[union-attr]
        return True


def _new_report(boot_id: str, seq: int, started: float) -> Dict[str, object]:
    return {"boot_id": boot_id, "pass_seq": seq, "started_at": R.timestamp(started), "control": "up",
            "swept": [], "revoked": [], "reconciled": [], "ended": [], "scrubbed": [], "reverted": [], "braked": [],
            "held": [], "skipped": []}


class Reaper:
    """The pass worker's state across passes: the HELD backoff clock (in memory, seeded from ``held_attempts`` at
    service start: a restart retries every HELD once at once, then backs off; DESIGN Q6) and the pass counter."""

    def __init__(self, seams: H.Seams, *, tuning: Tuning = Tuning(),
                 log: Callable[[str], None] = _log_to_stderr,
                 netns: Optional[Callable[[Sequence[SlotId]], None]] = None) -> None:
        self.seams = seams
        self.tuning = tuning
        self.log = log
        self.netns = netns or ensure_netns
        self.pass_seq = 0
        self.next_try: Dict[str, float] = {}

    # -- context ------------------------------------------------------------------------------------------------------

    def ctx(self) -> H.Ctx:
        """A fresh root context per pass (its own store fd and CONTROL connection, both closed after)."""
        return H.Ctx(seams=self.seams, store=H.Store(self.seams), sudo_uid=0, caller="reaper", operator_uid=-1,
                     env={})

    def item(self, rep: Dict[str, object], what: str) -> _Item:
        return _Item(self.log, rep, what)

    def control_up(self, ctx: H.Ctx) -> bool:
        """CONTROL down = connect refused or the socket absent (§4.1). Any other refusal or OS error from the connect
        is logged and taken as down too: the pass runs its local steps, never nothing."""
        try:
            ctx.client
            return True
        except (Refused, OSError) as e:
            if not (isinstance(e, Refused) and e.name == "control-down"):
                self.log(f"CONTROL connect: {type(e).__name__}: {e}; taken as down")
            return False

    # -- boot (§4.2) --------------------------------------------------------------------------------------------------

    def boot_steps(self) -> None:
        """§4.2 steps 2-4, in the worker before its first pass: Linux namespaces, the reserve, then CONTROL with
        retry. Step 1 (``R/`` and ``wake.sock``) is the main process's (:func:`boot_dirs`)."""
        store = H.Store(self.seams)
        try:
            if self.seams.host == "linux":
                try:
                    self.netns(_slots(store))
                except Exception as e:   # noqa: BLE001 - start refuses netns-missing; the pass goes on
                    self.log(f"boot: netns: {type(e).__name__}: {e}")
            self._refill(store)
        finally:
            store.close()
        deadline = self.seams.monotonic() + CONTROL_BOOT_WAIT_S
        while True:
            ctx = self.ctx()
            try:
                if self.control_up(ctx):
                    return
            finally:
                ctx.close()
                ctx.store.close()
            if self.seams.monotonic() >= deadline:
                self.log("boot: CONTROL did not answer; passes run the local steps until it does")
                return
            self.seams.sleep(0.5)

    # -- the pass (§4.3) ----------------------------------------------------------------------------------------------

    def run_pass(self) -> Dict[str, object]:
        PR.reap_leftovers()
        self.pass_seq += 1
        seams = self.seams
        started = seams.clock()
        ctx = self.ctx()
        rep = _new_report(seams.boot_id(), self.pass_seq, started)
        try:
            up = self.control_up(ctx)
            rep["control"] = "up" if up else "down"
            steps: List[Tuple[str, Callable[[H.Ctx, Dict[str, object]], None]]] = [("0", self.sweep_temps)]
            if up:
                steps += [("1", self.revoke_dead_holders), ("2", self.reconcile_journals),
                          ("3", self.end_and_scrub), ("3a", self.revert_orphans)]
            for name in ("3b", "3c", "5"):
                rep["skipped"].append(f"step{name}:seam-unbuilt")     # type: ignore[union-attr]
            if up:
                steps += [("4", self.brake_below_floor)]
            steps += [("6", self.refill_reserve)]
            if up:
                steps += [("7", self.pool_health)]
            else:
                rep["skipped"].append("control-down")                 # type: ignore[union-attr]
            for name, step in steps:
                try:
                    step(ctx, rep)
                except Exception as e:   # noqa: BLE001 - one step's fault is logged; the pass goes on
                    self.log(f"pass {self.pass_seq} step {name}: {type(e).__name__}: {e}")
                    rep["skipped"].append(f"step{name}:{type(e).__name__}")   # type: ignore[union-attr]
        finally:
            try:
                ctx.close()
            finally:
                try:
                    rep["ended_at"] = R.timestamp(seams.clock())
                    self.write_heartbeat(rep)
                except Exception as e:   # noqa: BLE001 - doctor reads the heartbeat's age; a miss shows there
                    self.log(f"heartbeat: {type(e).__name__}: {e}")
                finally:
                    ctx.store.close()
        return rep

    # -- reads ---------------------------------------------------------------------------------------------------------

    def _allocs(self, ctx: H.Ctx) -> Dict[str, R.Alloc]:
        out: Dict[str, R.Alloc] = {}
        with ctx.store.dir("alloc") as fd:
            names = sorted(os.listdir(fd))
        for n in names:
            if not n.endswith(".json") or not SESSION_RE.fullmatch(n[:-5]):
                continue
            try:
                raw = ctx.store.read(("alloc",), n)
                if raw is not None:
                    a = R.parse_alloc(raw)
                    if a.session == n[:-5]:
                        out[a.session] = a
            except Exception as e:   # noqa: BLE001 - a malformed alloc is named, never acted on
                self.log(f"alloc {n}: unreadable: {type(e).__name__}: {e}")
        return out

    def _records(self, ctx: H.Ctx) -> Dict[str, R.SessionState]:
        """CONTROL's records, read as HINTS (§7.1): the dir is CONTROL's, so it is opened untrusted."""
        out: Dict[str, R.SessionState] = {}
        try:
            with ctx.store.dir("sessions", trusted=False) as fd:
                names = sorted(os.listdir(fd))
        except Refused as e:
            if e.name == "store-missing":
                return out
            raise
        for n in names:
            if not n.endswith(".json") or not SESSION_RE.fullmatch(n[:-5]):
                continue
            try:
                raw = ctx.store.read(("sessions",), n, trusted=False)
                if raw is not None:
                    rec = R.parse_state(raw)
                    if rec.session == n[:-5]:
                        out[rec.session] = rec
            except Exception as e:   # noqa: BLE001
                self.log(f"record {n}: unreadable: {type(e).__name__}: {e}")
        return out

    def _leased_to(self, ctx: H.Ctx, session: str) -> Optional[SlotId]:
        for slot in _slots(ctx.store):
            st = _slot_state(ctx.store, slot)
            if st is not None and st.state == "leased" and st.session == session:
                return slot
        return None

    # -- step 0: the *.new sweep (DESIGN Q4) ---------------------------------------------------------------------------

    def sweep_temps(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        """A dead writer's alloc temp, removed only when its writer is provably dead: no slot is ``leased`` to its
        session, or that slot's lock TRY-locks free (``prepare`` holds the slot lock from the ``leased`` write until
        ATTACH answers, §3.5 ORDER). The temp is listed FIRST and the slot state read SECOND: a listed temp implies
        its lease write already happened."""
        with ctx.store.dir("alloc") as fd:
            temps = [(n, s) for n in sorted(os.listdir(fd)) for s in (temp_session(n),) if s is not None]
        for name, session in temps:
            with self.item(rep, f"swept:{name}"):
                slot = self._leased_to(ctx, session)
                if slot is None:
                    self._unlink_temp(ctx, name, rep)
                    continue
                with _SlotLock(ctx.store, slot) as lk:
                    if not lk.held:
                        rep["skipped"].append(f"swept:{name}:slot-busy")   # type: ignore[union-attr]
                        continue
                    self._unlink_temp(ctx, name, rep)

    def _unlink_temp(self, ctx: H.Ctx, name: str, rep: Dict[str, object]) -> None:
        with ctx.store.dir("alloc") as fd:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(st.st_mode):
                rep["skipped"].append(f"swept:{name}:not-a-file")      # type: ignore[union-attr]
                return
            os.unlink(name, dir_fd=fd)
            self.seams.fsync(fd)
        rep["swept"].append(name)                                       # type: ignore[union-attr]

    # -- step 1: dead-holder REVOKE (§7.7 1) ---------------------------------------------------------------------------

    def revoke_dead_holders(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        """``REVOKE {s}`` when alloc's ``boot_id`` is not this boot's; or the record is live with ``lease_live`` false;
        or there is an alloc and no record (CONTROL creates it at REVOKED). REVOKE only ends a session and keeps its
        work, so a forged hint can only end a session early. DECISION (seat): the no-record case is sent only while
        the session's slot lock TRY-locks free, since a live ``prepare`` holds that lock between its alloc and ATTACH."""
        boot = self.seams.boot_id()
        records = self._records(ctx)
        for s, a in self._allocs(ctx).items():
            with self.item(rep, f"revoke:{s}"):
                rec = records.get(s)
                if rec is None:
                    with _SlotLock(ctx.store, a.slot) as lk:
                        if not lk.held:
                            rep["skipped"].append(f"revoke:{s}:slot-busy")   # type: ignore[union-attr]
                            continue
                        if not _exists(ctx.store, ("sessions",), f"{s}.json", trusted=False):
                            self._revoke(ctx, s, rep)
                    continue
                if rec.state in R.LIVE_STATES and (a.boot_id != boot or not rec.lease_live):
                    self._revoke(ctx, s, rep)

    def _revoke(self, ctx: H.Ctx, s: str, rep: Dict[str, object]) -> None:
        try:
            ctx.call("REVOKE", session=s)
            rep["revoked"].append(s)                                    # type: ignore[union-attr]
        except Refused as e:
            rep["skipped"].append(f"revoke:{s}:{e.name}")              # type: ignore[union-attr]

    # -- step 2: journal reconciliation (§7.4, §7.7 2) -----------------------------------------------------------------

    def reconcile_journals(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        allocs = self._allocs(ctx)
        records = self._records(ctx)
        for s in pending_sessions(ctx.store.path / "alloc"):
            a = allocs.get(s)
            if a is None:
                rep["skipped"].append(f"reconcile:{s}:no-alloc")       # type: ignore[union-attr]
                continue
            rec = records.get(s)
            try:
                with self.item(rep, f"reconcile:{s}"):   # the journal stays at its phase for the next pass
                    E.reconcile_session(SS.end_facts(ctx, a), SS.end_seams(ctx, a),
                                        held_attempts=rec.fields.get("held_attempts") if rec else None)  # type: ignore
                    rep["reconciled"].append(s)                         # type: ignore[union-attr]
            finally:
                with self.item(rep, f"reconcile:{s}:after"):
                    self._held_by_reconcile(ctx, s, rec)

    def _held_by_reconcile(self, ctx: H.Ctx, s: str, before: Optional[R.SessionState]) -> None:
        """A reconcile whose proof failed CASed the record HELD: its backoff starts now, as END's does, so step 3
        of the same pass does not run END on it again."""
        after = SS.read_record(ctx, s)
        if after is not None and after.state == R.HELD and (before is None or after.generation != before.generation):
            self._back_off(s, after.fields.get("held_attempts"))

    # -- step 3: END, the HELD retry, SCRUB again (§7.3, §7.7 3, RH5) --------------------------------------------------

    def _held_due(self, s: str) -> bool:
        return self.next_try.get(s, 0.0) <= self.seams.monotonic()

    def _back_off(self, s: str, attempts: object) -> None:
        n = attempts if isinstance(attempts, int) and attempts >= 1 else 1
        delay = min(HELD_BACKOFF_S * (2 ** min(n - 1, 32)), V22_S)
        self.next_try[s] = self.seams.monotonic() + delay

    def end_and_scrub(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        """Records in REVOKED, FROZEN, DEAD or CREDENTIAL_PARKED run END; HELD runs END from its ``failed_step`` when
        its backoff is due (a HELD ``kill`` re-runs END from ``kill``). A
        RETAINED END goes on to SCRUB. The pass rule (RH5): a RETAINED record whose alloc is present and whose slot is
        still leased to it is SCRUBbed again, every pass, no backoff (SCRUB re-proves and is idempotent). An
        ``end-busy`` (or ``slot-locked``) refusal is not an attempt: no record change, no backoff (DESIGN r5 F5)."""
        allocs = self._allocs(ctx)
        live: set = set()
        held: Dict[str, None] = {}      # the sessions still HELD when step 3 ends, in pass order
        for s, rec in self._records(ctx).items():
            with self.item(rep, f"end:{s}"):
                self._end_one(ctx, s, rec, allocs.get(s), live, held, rep)
        for s in [s for s in self.next_try if s not in live]:
            del self.next_try[s]
        rep["held"].extend(held)                                        # type: ignore[union-attr]

    def _end_one(self, ctx: H.Ctx, s: str, rec: R.SessionState, a: Optional[R.Alloc], live: set,
                 held: Dict[str, None], rep: Dict[str, object]) -> None:
        if rec.state == R.RETAINED:
            if a is not None and SS._names_lease(_slot_state(ctx.store, a.slot), a):
                self._scrub(ctx, a, rep)
            return
        if rec.state not in R.END_FROM_STATES and rec.state != R.HELD:
            return
        if rec.state == R.HELD:
            live.add(s)
            held[s] = None
            if not self._held_due(s):
                return
        if a is None:
            rep["skipped"].append(f"end:{s}:no-alloc")                  # type: ignore[union-attr]
            return
        try:
            out = E.end_session(SS.end_facts(ctx, a), SS.end_seams(ctx, a), record_state=rec.state,
                                failed_step=rec.fields.get("failed_step"),              # type: ignore[arg-type]
                                held_attempts=rec.fields.get("held_attempts"))          # type: ignore[arg-type]
        except Refused as e:
            rep["skipped"].append(f"end:{s}:{e.name}")                  # type: ignore[union-attr]
            if e.name not in ("end-busy", "slot-locked") and rec.state == R.HELD:
                self._back_off(s, rec.fields.get("held_attempts"))
            return
        except Exception as e:   # noqa: BLE001 - CasNotSent and the like: the next pass tries again
            self.log(f"END {s}: {type(e).__name__}: {e}")
            rep["skipped"].append(f"end:{s}:{type(e).__name__}")        # type: ignore[union-attr]
            return
        rep["ended"].append(f"{s}:{out}")                               # type: ignore[union-attr]
        if out == R.RETAINED:
            self.next_try.pop(s, None)
            held.pop(s, None)
            self._scrub(ctx, a, rep)
        elif out == R.HELD:
            live.add(s)
            held[s] = None
            after = SS.read_record(ctx, s)
            self._back_off(s, after.fields.get("held_attempts") if after is not None else None)

    def _scrub(self, ctx: H.Ctx, a: R.Alloc, rep: Dict[str, object]) -> None:
        try:
            done = SS.scrub_and_release(ctx, a)
        except Exception as e:   # noqa: BLE001 - an Unproven sweep or an OS error: the next pass scrubs again
            if not isinstance(e, Refused):
                self.log(f"SCRUB {a.session}: {type(e).__name__}: {e}")
            rep["skipped"].append(f"scrub:{a.session}:{_name(e)}")      # type: ignore[union-attr]
            return
        if done:
            rep["scrubbed"].append(a.session)                           # type: ignore[union-attr]
        elif done is False:
            rep["skipped"].append(f"scrub:{a.session}:pool-busy")       # type: ignore[union-attr]

    # -- step 3a: the orphan lease revert (§3.5 Pool, DESIGN Q3, I1, R-d) ---------------------------------------------

    def revert_orphans(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        """A slot ``leased`` to a session with no alloc and no record (a ``prepare`` that died before ATTACH). Two
        phases: a snapshot under the pool lock; then, per slot, the slot lock TRIED, the state still the snapshot's,
        alloc and record still absent, every uid of the slot ended and proven
        (I1), the pool lock TRIED, and the state written ``reusable`` (DESIGN Q3:
        ``scrubbed`` is a format value only) with ``rev + 1`` iff the rev is unchanged. A slot carrying the ``spent``
        mark is written ``retired`` instead (R-d), and no account is removed (DESIGN Q-G1).
        The gateway uid is swept without ``front.lock``: no render of a session with no alloc can be in flight."""
        with _PoolLock(ctx.store) as pool:
            if not pool.held:
                rep["skipped"].append("orphan:pool-busy")               # type: ignore[union-attr]
                return
            snap: Dict[SlotId, Optional[R.SlotState]] = {}
            for slot in _slots(ctx.store):
                with self.item(rep, f"orphan:{slot.tag}"):
                    snap[slot] = _slot_state(ctx.store, slot)
        for slot, st in snap.items():
            if st is None or st.state != "leased" or st.session is None:
                continue
            with self.item(rep, f"orphan:{slot.tag}"):
                self._revert_one(ctx, slot, st, rep)

    def _revert_one(self, ctx: H.Ctx, slot: SlotId, st: R.SlotState, rep: Dict[str, object]) -> None:
        s = st.session
        if _exists(ctx.store, ("alloc",), f"{s}.json") or _exists(ctx.store, ("sessions",), f"{s}.json",
                                                                     trusted=False):
            return
        with _SlotLock(ctx.store, slot) as lk:
            if not lk.held:
                rep["skipped"].append(f"orphan:{slot.tag}:slot-busy")        # type: ignore[union-attr]
                return
            if _slot_state(ctx.store, slot) != st or _exists(ctx.store, ("alloc",), f"{s}.json") \
                    or _exists(ctx.store, ("sessions",), f"{s}.json", trusted=False):
                return
            ids = H.slot_ids(ctx, slot)
            first, second = slot.accounts
            PR.scrub(ids[first], ids[second], seams=self.seams.proc_seams)   # type: ignore[arg-type]
            with _PoolLock(ctx.store) as pool:
                if not pool.held:
                    rep["skipped"].append(f"orphan:{slot.tag}:pool-busy")    # type: ignore[union-attr]
                    return
                if _slot_state(ctx.store, slot) != st:
                    return
                to = "retired" if _exists(ctx.store, ("slots", slot.tag), SPENT) else "reusable"
                new = replace(st, rev=st.rev + 1, state=to, session=None, changed_at=R.timestamp(self.seams.clock()))
                ctx.store.replace(("slots", slot.tag), "state", R.slot_state_bytes(new), mode=0o644)
        rep["reverted"].append(f"{slot.tag}:{s}:{to}")                     # type: ignore[union-attr]

    # -- step 4: BRAKE below V5 (§7.7 4) -------------------------------------------------------------------------------

    def brake_below_floor(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        if self.seams.free_bytes(ctx.store.fd) >= self.tuning.floor_bytes:
            return
        for s, rec in self._records(ctx).items():
            if rec.state != R.ACTIVE:
                continue
            with self.item(rep, f"brake:{s}"):
                ctx.call("CAS", session=s, generation=rec.generation, **{"from": R.ACTIVE, "to": R.BRAKED},
                         fields={"brake_reason": "below-floor"})
                rep["braked"].append(s)                                 # type: ignore[union-attr]

    # -- step 6: reserve refill (§7.5) ---------------------------------------------------------------------------------

    def refill_reserve(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        full = self._refill(ctx.store)
        if full is None:
            rep["skipped"].append("reserve:busy")                       # type: ignore[union-attr]
        elif not full:
            rep["skipped"].append("reserve:below-floor")                # type: ignore[union-attr]

    def _refill(self, store: H.Store) -> Optional[bool]:
        """Whether the reserve is full, or None when its lock is busy (a release is truncating it)."""
        reserve = E.Reserve(store.path / "reserve", self.tuning.reserve_bytes)
        with _reserve_lock(reserve.path) as held:
            if not held:
                return None
            return reserve.refill(lambda: self.seams.free_bytes(store.fd), floor=self.tuning.floor_bytes)

    # -- step 7: pool health (§3.5) ------------------------------------------------------------------------------------

    def pool_health(self, ctx: H.Ctx, rep: Dict[str, object]) -> None:
        """``dispatch_eligible`` recomputed (false for a slot whose ``keep/`` holds a credential or that has a
        baseline), written only where it changed, under the pool lock and the slot lock (both TRIED). ``SLOT-SYNC`` is
        a seam until CONTROL serves it (session 3c)."""
        from levain.slot.install_helper import _operator_held

        rep["skipped"].append("slot-sync:seam-unbuilt")                 # type: ignore[union-attr]
        with _PoolLock(ctx.store) as pool:
            if not pool.held:
                rep["skipped"].append("pool-health:pool-busy")          # type: ignore[union-attr]
                return
            for slot in _slots(ctx.store):
                with self.item(rep, f"pool-health:{slot.tag}"), _SlotLock(ctx.store, slot) as lk:
                    if not lk.held:
                        continue
                    st = _slot_state(ctx.store, slot)
                    if st is None:
                        continue
                    want = not _operator_held(slot.slot_dir(ctx.store.path))
                    if st.dispatch_eligible != want:
                        new = replace(st, rev=st.rev + 1, dispatch_eligible=want,
                                      changed_at=R.timestamp(self.seams.clock()))
                        ctx.store.replace(("slots", slot.tag), "state", R.slot_state_bytes(new), mode=0o644)

    # -- step 8: the heartbeat (DESIGN Q5) -----------------------------------------------------------------------------

    def write_heartbeat(self, rep: Mapping[str, object]) -> None:
        """``R/reaper/pass.json``, root 0644, temp + rename: for doctor (pass age, control up/down, HELD list); a source
        of truth for nothing."""
        keep = ("boot_id", "pass_seq", "started_at", "ended_at", "control", "held", "skipped")
        data = canon.canonical_bytes({k: rep[k] for k in keep if k in rep})
        d = os.open(str(self.seams.R / "reaper"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            H.replace_at(d, HEARTBEAT, data, mode=0o644, fsync=self.seams.fsync)
        finally:
            os.close(d)


# --- the revoker (§4.7, CONTRACT §4.5) ----------------------------------------------------------------------------------

class Revoker:
    """The REVOKER process's state: which leases' fronts it has dealt with, when it first saw each, and since when
    CONTROL has been slow. It takes no lock the pass worker takes (DESIGN §4.7: ``front.lock`` alone). Each wake (a
    datagram or a tick): reap leftover signalling children; ask CONTROL for ``REVOKED-SET``; for every session in it
    whose alloc names an egress slot, :func:`start.revoke_front`. CONTROL DOWN (connect refused or the socket absent; or
    an answer slow past V23): every egress alloc's front is ended, since no session can be confirmed live (CONTRACT
    §4.5). Only an outcome that proves this lease's front cannot run is final: ``front-ended``, and
    ``front-lease-moved`` (the slot no longer names this lease: its uids are SCRUB's, not this lease's). Anything
    else (an error, ``front-unproven:*``, ``front-busy``) stays pending and is retried on every wake while the lease
    is a target. A REVOKED-SET answer of the wrong shape is a protocol failure: CONTROL is down. While CONTROL is slow (under
    V23 since the first slow attempt began), the leases already confirmed and still pending are retried and nothing
    is forgotten."""

    #: The outcomes that prove a lease's front cannot run (the only ones kept in ``done``).
    FINAL = frozenset({"front-ended", "front-lease-moved"})

    def __init__(self, seams: H.Seams, *, log: Callable[[str], None] = _log_to_stderr) -> None:
        self.seams = seams
        self.log = log
        self.done: Dict[Tuple[str, int], str] = {}
        self.first_seen: Dict[Tuple[str, int], float] = {}
        self.slow_since: Optional[float] = None
        self.unread: frozenset = frozenset()   # the targets whose alloc the last wake could not read

    def pending(self) -> bool:
        return bool(self.unread) or any(k not in self.done for k in self.first_seen)

    def wake(self) -> Dict[str, object]:
        PR.reap_leftovers()
        seams = self.seams
        ctx = H.Ctx(seams=seams, store=H.Store(seams), sudo_uid=0, caller="reaper", operator_uid=-1, env={})
        out: Dict[str, object] = {"control": "up", "ended": [], "outcomes": {}}
        try:
            targets = self._targets(ctx, out)
            slow = targets is None
            if slow:   # only the leases CONTROL already confirmed, still pending
                targets = sorted({k[0] for k in self.first_seen if k not in self.done})
            allocs = {}
            unread = set()
            for s in targets:   # type: ignore[union-attr]
                try:
                    raw = ctx.store.read(("alloc",), f"{s}.json")
                    if raw is None:
                        continue
                    a = R.parse_alloc(raw)
                except Exception as e:   # noqa: BLE001 - this alloc is named and kept pending; the other targets go on
                    self.log(f"revoker: alloc {s}: {type(e).__name__}: {e}")
                    out["outcomes"][s] = f"alloc:{_name(e)}"            # type: ignore[index]
                    unread.add(s)
                    continue
                key = (s, a.lease_seq)
                if a.session == s and a.slot.slot_class == "egress" and (not slow or key in self.first_seen):
                    allocs[key] = a
            now = seams.monotonic()
            for key, a in allocs.items():
                if key in self.done:
                    continue
                self.first_seen.setdefault(key, now)
                try:
                    res = SS.revoke_front(ctx, a)
                except Exception as e:   # noqa: BLE001 - not proven; retried on the next wake
                    res = e.name if isinstance(e, Refused) else f"error:{type(e).__name__}"
                    if not isinstance(e, Refused):
                        self.log(f"revoker: {a.session}: {type(e).__name__}: {e}")
                out["outcomes"][a.session] = res                        # type: ignore[index]
                if res == "front-ended":
                    out["ended"].append(a.session)                      # type: ignore[union-attr]
                if res in self.FINAL:
                    self.done[key] = res
                    if res != "front-ended":
                        self.log(f"revoker: {a.session} lease {a.lease_seq}: {res}")
                elif not (res == "front-busy" and now - self.first_seen[key] < SS.REVOKE_FRONT_S):
                    self.log(f"revoker: {a.session} lease {a.lease_seq}: {res}; retried on the next wake")
            self.unread = frozenset(unread)
            if not slow:   # a lease whose alloc could not be read is not known gone: it is kept
                for key in [k for k in self.first_seen if k not in allocs and k[0] not in unread]:
                    self.first_seen.pop(key, None)
                    self.done.pop(key, None)
        finally:
            try:
                ctx.close()
            finally:
                ctx.store.close()
        return out

    def _targets(self, ctx: H.Ctx, out: Dict[str, object]) -> Optional[List[str]]:
        """The sessions whose fronts are ended this wake: REVOKED-SET's, every egress alloc's with CONTROL down, or
        None while CONTROL is slow. The V23 window opens when the first slow attempt STARTS and closes on any other
        outcome."""
        try:
            ctx.client
        except (Refused, OSError) as e:
            if not (isinstance(e, Refused) and e.name == "control-down"):
                self.log(f"revoker: CONTROL connect: {type(e).__name__}: {e}; taken as down")
            return self._down(ctx, out)
        t0 = self.seams.monotonic()
        try:
            ans = ctx.call("REVOKED-SET")
        except (Refused, OSError) as e:
            if not (isinstance(e, Refused) and e.name == "control-down"):
                self.log(f"revoker: REVOKED-SET: {type(e).__name__}: {e}; taken as down")
                return self._down(ctx, out)
            if self.slow_since is None:
                self.slow_since = t0
            if self.seams.monotonic() - self.slow_since < V23_S:
                out["control"] = "slow"
                return None
            out["control"] = "down"
            return self._all_egress(ctx)
        sessions = ans.get("sessions") if isinstance(ans, dict) else None
        if not isinstance(sessions, list) or not all(isinstance(s, str) and SESSION_RE.fullmatch(s) for s in sessions):
            self.log(f"revoker: REVOKED-SET answered off its shape: {ans!r:.200}; taken as down")
            return self._down(ctx, out)
        self.slow_since = None
        return list(sessions)

    def _down(self, ctx: H.Ctx, out: Dict[str, object]) -> List[str]:
        self.slow_since = None
        out["control"] = "down"
        return self._all_egress(ctx)

    @staticmethod
    def _all_egress(ctx: H.Ctx) -> List[str]:
        with ctx.store.dir("alloc") as fd:
            return [n[:-5] for n in sorted(os.listdir(fd)) if n.endswith(".json") and SESSION_RE.fullmatch(n[:-5])]


# --- §4.2 step 1: R/ and the wake socket (the main process) --------------------------------------------------------

def run_rows(seams: H.Seams) -> List[Row]:
    """§2.2's run-dir rows for this host and every configured slot, rooted at ``seams.R``."""
    host = seams.host
    store = H.Store(seams)
    try:
        slots = _slots(store)
    finally:
        store.close()
    rows = base_run_rows(host) + [r for s in slots for r in slot_run_rows(host, s)]   # type: ignore[arg-type]
    base = RUN[host]                                                                   # type: ignore[index]
    return [replace(r, path=seams.R / r.path.relative_to(base)) for r in rows]


def boot_dirs(seams: H.Seams, *, log: Callable[[str], None] = _log_to_stderr) -> List[str]:
    """Every run dir made when absent and set to its row's owner, group and mode (macOS: the reaper is the only
    creator, §22; Linux: tmpfiles made them and this re-checks). Never through a link: a row whose path is not a
    directory is named and left as it is. Returns the problems."""
    problems: List[str] = []
    for row in run_rows(seams):
        p = str(row.path)
        try:
            try:
                os.mkdir(p, 0o700)
            except FileExistsError:
                pass
            fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            problems.append(f"{p}: {e.strerror or e}")
            continue
        try:
            uid = 0 if row.owner == "root" else seams.account_uid(row.owner)
            gid = 0 if row.group in ("root", "wheel") else seams.group_gid(row.group)
            seams.chown(fd, uid, gid, p)
            os.fchmod(fd, row.mode)
        except (OSError, Refused, KeyError) as e:
            problems.append(f"{p}: {e}")
        finally:
            os.close(fd)
    for msg in problems:
        log(f"boot: run dir: {msg}")
    return problems


def ensure_netns(slots: Sequence[SlotId]) -> None:
    """§4.2 step 2 (Linux): ``levain-{e,v}<n>`` made when absent, by the same Steps setup runs."""
    from levain.slot.netns import netns_steps

    for step in netns_steps(slots):
        if step.skip_if and subprocess.run(step.skip_if, timeout=NETNS_STEP_S).returncode == 0:
            continue
        if step.argv:
            rc = subprocess.run(step.argv, timeout=NETNS_STEP_S).returncode
            if rc != 0 and not step.allow_fail:
                raise RuntimeError(f"{step.why}: exit {rc}")


__all__ = ["CONTROL_BOOT_WAIT_S", "HEARTBEAT", "HELD_BACKOFF_S", "REVOKER_RETRY_S", "Reaper", "Revoker", "SPENT",
           "Tuning", "V11_S", "V18_S", "V22_S", "V23_S", "boot_dirs", "ensure_netns", "run_rows", "temp_session"]
