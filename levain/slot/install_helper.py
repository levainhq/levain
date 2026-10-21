"""The install helper's plans (CONTRACT §5.2, §23): ``plan_setup``, ``plan_undo``, ``plan_replenish``.

Each takes the parsed command (:mod:`levain.slot.grammar`) and the host's facts as data, and returns
an :class:`InstallPlan` whose ``steps`` are :class:`~levain.firing.hands.Step` s. Nothing here acts on
the OS; every refusal (live lease, second operator, capacity, index space, a missing tool) is raised
while planning, before step 1.

Re-runs (§23): setup writes the operator record LAST, so ``already-set-up`` means a complete install,
and a setup that failed part-way re-runs: every step is idempotent (a ``skip_if`` where the act is
not), and what the interrupted run made is kept at the ids it holds (:attr:`HostFacts.levain_live`).
Undo removes the runtime rule first and the install rule last, before the record, so a failed undo
re-runs through the install helper.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Iterable, Iterator, Mapping, Optional, Sequence, TextIO

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HandsSetupError, HostOS, Step, _abs, _kill_all, host_os, run_plan
from levain.launch import child_env
from levain.slot import Refused, bwrap, netns, pf, root_tree, units
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import runtime_helper as RH
from levain.slot.intent import INTENT_PATH, Intent, install_lock, intent_remove_step, intent_write_step, read_intent
from levain.slot.accounts import (SLOT_MARKER, SLOT_RETIRED_MARKER, Account, Group, account_steps, accounts_of,
                                  delete_group_steps, membership_pairs, membership_steps, revive_steps,
                                  shared_group_steps, shared_groups_of, tombstone_steps)
from levain.slot.grammar import LEASE_EXEMPT, SESSION_RE, VERBS, InstallCommand, check_roots, parse_install_argv
from levain.slot.ids import (RANGES, RETIRED_IDS_PATH, SHARED_GROUPS, SINGLETONS, SLOT_NAME_RE, Allocation,
                             SlotClass, SlotId, allocate, check_capacity, choose_indices, in_own_range,
                             is_levain_account, read_retired_ids, retired_ids_text, spent_indices)
from levain.slot.runtime_grammar import VERBS as RUNTIME_VERBS
from levain.slot.runtime_grammar import ok_line, refusal_line
from levain.slot.layout import (STORE, Row, base_routes_steps, base_run_rows, base_store_rows, operator_record_path,
                                parse_pool_config, policy_rows, pool_config_row, pool_config_steps,
                                pool_config_undo_steps, reserve_row, reserve_steps, roots_steps, row_steps,
                                run_dir_undo_steps, single_filesystem_steps, slot_rows, tmpfiles_steps,
                                tmpfiles_undo_steps, ungroup_steps)
from levain.slot.sudoers import remove_install_sudoers_steps, remove_runtime_sudoers_steps, sudoers_steps

OPERATOR_FORMAT = "levain-operator/1"


def _tool_present(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _xcode_ready() -> bool:
    """macOS: ``/usr/bin/python3`` is a shim that runs only once the developer tools are installed."""
    try:
        return subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True,
                              env=child_env()).returncode == 0
    except OSError:
        return False


@dataclass(frozen=True)
class HostFacts:
    """What the apply layer reads before planning; the plan reads nothing else."""

    host: HostOS
    caller_uid: int                                   # SUDO_UID
    caller_name: str | None = None                    # the name SUDO_UID resolves to
    operator_uid: int | None = None                   # setup: the uid ``--operator`` resolves to
    operator_record: Mapping[str, object] | None = None   # parsed ``L/policy/operator``, or None
    live_leases: int = 0
    used_ids: frozenset[int] = frozenset()            # every uid and gid in use on the host
    existing_names: frozenset[str] = frozenset()      # every user and group name on the host
    retired: frozenset[int] = frozenset()             # the retired-ids file
    #: Accounts whose label is Levain's retired marker (``accounts.SLOT_RETIRED_MARKER``), name -> uid.
    levain_tombstones: Mapping[str, int] = field(default_factory=dict)
    #: Accounts and shared groups whose label is Levain's live marker (``accounts.SLOT_MARKER``),
    #: name -> id: what an interrupted setup made (§23).
    levain_live: Mapping[str, int] = field(default_factory=dict)
    #: Whether a host tool (an absolute path) is present and executable (§23 preflight).
    tool_present: Callable[[str], bool] = _tool_present
    #: macOS: ``xcode-select -p`` succeeds (§23 preflight).
    xcode_ready: Callable[[], bool] = _xcode_ready
    #: The intent journal (§25), read under the install lock; None when there is none.
    intent: Intent | None = None
    #: For each name the journal lists that is present on the host, the id it holds there (the uid of
    #: an account, the gid of a group): a journaled name at its journaled id is Levain's (§25).
    host_ids: Mapping[str, int] = field(default_factory=dict)
    #: What ``live_leases`` counts (the refusal names them); last, so positional construction keeps its meaning.
    live_sessions: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallPlan:
    verb: str
    host: HostOS
    steps: tuple[Step, ...]
    allocation: Allocation | None = None
    accounts: tuple[Account, ...] = ()
    groups: tuple[Group, ...] = ()
    rows: tuple[Row, ...] = ()
    roots: tuple[str, ...] = ()
    retired_after: frozenset[int] = field(default_factory=frozenset)
    revived: tuple[str, ...] = ()        # singletons brought back at their recorded id (§20)
    kept: tuple[str, ...] = ()           # names an interrupted setup made, kept at their ids (§23)
    pending: tuple[str, ...] = ()        # programs the plan points at that are not built yet
    services: tuple[str, ...] = ()       # the §1.3 services whose units this plan installs
    intent: Intent | None = None         # what the plan's first step journals (§25)


# --- the operator record ---------------------------------------------------------------------------


def operator_record(name: str, uid: int) -> dict[str, object]:
    return {"format": OPERATOR_FORMAT, "name": name, "uid": uid}


def operator_record_text(name: str, uid: int) -> str:
    return canon.canonical(operator_record(name, uid))


def parse_operator_record(data: bytes) -> dict[str, object]:
    try:
        obj = canon.parse(data)
    except canon.ContractError as e:
        raise Refused("operator-record-malformed", str(e)) from None
    if (not isinstance(obj, dict) or set(obj) != {"format", "name", "uid"} or obj["format"] != OPERATOR_FORMAT
            or not isinstance(obj["name"], str) or not OPERATOR_RE.fullmatch(obj["name"])
            or not isinstance(obj["uid"], int) or isinstance(obj["uid"], bool) or obj["uid"] <= 0):
        raise Refused("operator-record-malformed", "not a levain-operator/1 record")
    return obj


# --- preflight: the refusals every verb shares ----------------------------------------------------


def preflight(cmd: InstallCommand, facts: HostFacts) -> None:
    """Live lease first, then the one-operator rule (CONTRACT header, §5.2)."""
    if cmd.verb not in LEASE_EXEMPT and facts.live_leases > 0:
        raise Refused("live-lease", _live_lease_why(cmd.verb, facts))
    rec = facts.operator_record
    if cmd.verb == "setup":
        if facts.operator_uid is None:
            raise Refused("no-such-operator", repr(cmd.operator))
        if facts.operator_uid == 0:
            raise Refused("bad-operator", "the operator cannot be uid 0")
        if rec is not None:
            if rec.get("name") == cmd.operator and rec.get("uid") == facts.operator_uid:
                raise Refused("already-set-up", "use repair")
            raise Refused("second-operator", f"configured for {rec.get('name')!r}, not {cmd.operator!r}")
        if facts.caller_uid != facts.operator_uid:
            raise Refused("caller-not-operator", f"uid {facts.caller_uid} is not {cmd.operator!r}")
        return
    if rec is None:
        raise Refused("not-set-up", cmd.verb)
    if facts.caller_uid != rec.get("uid") or facts.caller_name != rec.get("name"):
        raise Refused("operator-mismatch", f"configured for {rec.get('name')!r}")


def plan_tools(steps: Sequence[Step]) -> tuple[str, ...]:
    """Every host program a step runs: its argv's, its ``skip_if``'s and its validator's."""
    return tuple(sorted({x[0] for s in steps for x in (s.argv, s.skip_if, s.validate) if x}))


def extra_tools(host: HostOS, verb: str) -> tuple[str, ...]:
    """The host programs a plan runs that no step's argv names first: the system bwrap (Linux) is only
    the copy's SOURCE argument, and pkill, pgrep and dseditgroup run inside step callbacks. A fixed
    list per host and verb, checked with every plan's own tools, so a missing one refuses before step 1
    instead of failing part-way. Only setup and replenish copy bwrap; undo removes the copy and never
    reads the system one, so a host whose bwrap is gone can still undo. (``/usr/bin/python3`` is
    covered by the ``xcode-select`` check.)"""
    if host == "darwin":
        return ("/usr/sbin/dseditgroup", "/usr/bin/pkill", "/usr/bin/pgrep")
    if host == "linux":
        copies = (bwrap.system_bwrap(),) if verb in ("setup", "replenish") else ()
        return (*copies, _abs("pkill"), _abs("pgrep"))
    raise Refused("unsupported-host", repr(host))


def check_tools(facts: HostFacts, steps: Sequence[Step], *, verb: str, python: bool = False) -> None:
    """CONTRACT §23: every tool a step names, and every one of :func:`extra_tools`, is an absolute path
    that is present, refused by name before step 1 (a bare name is a tool the plan could not resolve on
    this host). ``python``: the plan installs root-run Python, so on macOS ``xcode-select -p`` must
    succeed too."""
    wanted = sorted(set(plan_tools(steps)) | set(extra_tools(facts.host, verb)))
    missing = [t for t in wanted if not t.startswith("/") or not facts.tool_present(t)]
    if missing:
        raise Refused("tool-missing", ", ".join(missing))
    if python and facts.host == "darwin" and not facts.xcode_ready():
        raise Refused("tool-missing", f"{root_tree.ROOT_PYTHON['darwin']} (xcode-select -p fails: no developer tools)")


#: The verbs :func:`main` applies; every other verb of the §5.2 grammar answers ``verb-unbuilt:<verb>``.
BUILT_VERBS = ("setup", "undo", "replenish")


def helper_pending() -> tuple[str, ...]:
    """A plan's ``pending`` line for each helper with verbs it does not apply yet, derived from the helpers
    themselves (this module's :data:`BUILT_VERBS`, the runtime helper's registered handlers), so it never goes
    stale as lanes land."""
    install = [v for v in VERBS if v not in BUILT_VERBS]
    runtime = [v for v in RUNTIME_VERBS if v not in RH.VERB_HANDLERS]
    out = []
    if install:
        out.append(f"levain.slot.install_helper answers verb-unbuilt for: {', '.join(install)}")
    if runtime:
        out.append(f"levain.slot.runtime_helper answers verb-unbuilt for: {', '.join(runtime)}")
    return tuple(out)


# --- the slot state files (CONTRACT §3.5 Pool, §2.1) -----------------------------------------------


def _store_key(store: Path) -> str:
    return os.path.abspath(os.fspath(store))


#: The :class:`Quiesce` of the install run in progress, by store: at most one per store (the install lock).
_QUIESCING: dict[str, "Quiesce"] = {}


class Quiesce:
    """CONTRACT §5.2 ``live-lease`` made a gate, not a snapshot (codex r1 g2 5): an install verb that refuses a
    live lease holds ``L/slots/pool.lock`` from its live-lease check through its last act, so no ``prepare`` can
    lease beneath it (``lease_slot`` waits for the same lock, then refuses ``pool-locked``). Lock order: the install
    lock, then this. The live-lease count is taken under it, and counts :meth:`busy_slots` too: ``prepare`` holds its
    slot lock from the lease until ``ATTACH`` has made the record that counts it live, so a slot lock held under the
    pool lock is a lease the session records do not show yet.

    While held, this process's own :func:`pool_lock` bodies (the slot state steps) run under it instead of
    taking the lock again (a second ``flock`` on a new descriptor would wait on this one)."""

    def __init__(self, store: Path, *, waits: RH.Seams | None = None) -> None:
        self.store = Path(store)
        self.waits = waits
        self.fd = -1

    @property
    def held(self) -> bool:
        return self.fd >= 0

    def hold(self) -> bool:
        """Take the lock. False when there is no lock file: no ``prepare`` can lease without it (``lease_slot``
        refuses ``store-missing``), and a fresh ``setup`` takes it with :func:`quiesce_step` once its rows have made
        the file. Raises ``pool-locked`` past the wait."""
        # DECISION: the newest hold of a store is the registered one (one install run per process; a gather
        # called without run(), as a test does, leaves its hold for the caller to release).
        _QUIESCING[_store_key(self.store)] = self
        if self.held:
            return True
        path = self.store / "slots" / "pool.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Refused("bad-root", f"{path} is not a regular file")
            RH.wait_flock(fd, self.waits or RH.Seams(), "pool-locked")
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def busy_slots(self) -> list[str]:
        """Under the hold: the slots whose lock is held (a ``prepare`` before its ``ATTACH`` answered, or an END).
        Each is TRIED, never waited on: the runtime's own rule, the pool lock never waits on a slot lock."""
        assert self.held
        busy = []
        slots = self.store / "slots"
        for name in sorted(os.listdir(slots)):
            try:
                lock = os.open(slots / name / "lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            except (FileNotFoundError, NotADirectoryError):
                continue
            try:
                if not RH.try_flock(lock):
                    busy.append(name)
            finally:
                os.close(lock)             # closing the only descriptor drops the flock it took
        return busy

    def live(self) -> tuple[str, ...]:
        """What the live-lease count counts: the sessions whose record is ``lease_live``; when held, also the session
        of every slot whose state is ``leased`` and ``slot <name>`` per :meth:`busy_slots` not already named. Under the
        hold a slot's state cannot change (prepare leases and SCRUB releases under ``pool.lock``, runtime_helper
        "state=\"leased\"" and start "state=\"reusable\""), so ``leased`` covers every lease from prepare to its
        release, including the two moments neither a record nor a slot lock shows it (L3 r2, codex)."""
        sessions = list(live_lease_sessions(self.store / "sessions"))
        if not self.held:
            return tuple(sessions)
        leased = self.leased_slots()
        for sess in leased.values():
            if sess not in sessions:
                sessions.append(sess)
        return tuple(sessions) + tuple(f"slot {b}" for b in self.busy_slots() if leased.get(b) not in sessions)

    def leased_slots(self) -> dict[str, str]:
        """Under the hold: slot name -> what holds it, for every slot whose state file says ``leased`` (its session),
        or cannot be read or parsed (``slot <name>``: counted live, so an install verb refuses rather than act)."""
        assert self.held
        out: dict[str, str] = {}
        slots = self.store / "slots"
        for name in sorted(os.listdir(slots)):
            try:
                fd = os.open(slots / name / "state", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                out[name] = f"slot {name}"
                continue
            try:
                with os.fdopen(fd, "rb") as fh:
                    st = R.parse_slot_state(fh.read(1 << 16))
            except (OSError, Refused):
                out[name] = f"slot {name}"
                continue
            if st.state == "leased" and st.session is not None:
                out[name] = st.session
        return out

    def live_leases(self) -> int:
        return len(self.live())

    def release(self) -> None:
        if _QUIESCING.get(_store_key(self.store)) is self:
            del _QUIESCING[_store_key(self.store)]
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def quiesce_step(host: HostOS, *, store: Path | None = None) -> Step:
    """A fresh ``setup``'s quiesce, once its rows have made ``L/slots/pool.lock`` and before any slot reads as
    reusable: the run's :class:`Quiesce` takes the lock here and holds it to the run's end. DECISION: with no run in
    progress (a plan applied outside :func:`run`) the step has nothing to hold and says so."""
    L = Path(store) if store is not None else STORE[host]

    def take() -> tuple[bool, str]:
        q = _QUIESCING.get(_store_key(L))
        if q is None:
            return True, "no install run is quiescing this store"
        if not q.hold():
            return False, f"{L / 'slots' / 'pool.lock'} is missing after the rows that make it"
        leased = q.live()
        if leased:   # nothing can lease before the runtime rule exists; a lease here is one this setup cannot see past
            return False, f"live-lease: {_lease_list(leased)}"
        return True, ""

    return Step(f"hold {L / 'slots' / 'pool.lock'} to the end of this run (no prepare leases beneath it)", call=take)


@contextmanager
def pool_lock(store: Path, *, missing_ok: bool = False, waits: RH.Seams | None = None) -> Iterator[None]:
    """``L/slots/pool.lock`` held for the body: every writer of a slot state file holds it (§3.5). Install only
    TRIES a slot lock, under this (:meth:`Quiesce.busy_slots`), so the pool-then-slot order holds. Waited for as the runtime helper waits
    (:func:`runtime_helper.wait_flock`; ``waits`` carries the clock and the bound), refused ``pool-locked`` past
    it. ``missing_ok``: with no lock file (a store emptied by hand) there is no writer to exclude and the body runs
    unlocked; undo only, since setup and replenish made the file in an earlier step. Under this process's held
    :class:`Quiesce` the body runs under that hold."""
    q = _QUIESCING.get(_store_key(store))
    if q is not None and q.held:
        yield
        return
    path = Path(store) / "slots" / "pool.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        if not missing_ok:
            raise
        yield
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("bad-root", f"{path} is not a regular file")
        RH.wait_flock(fd, waits or RH.Seams(), "pool-locked")
        yield
    finally:
        os.close(fd)   # closing the only descriptor releases the flock


def _operator_held(slot_dir: Path) -> bool:
    """§3.5 ``dispatch_eligible`` is false for a slot whose ``keep/`` holds a vendor credential or that has a
    ``baseline``; both are looked at by name only (§2.1: keep files are moved, never opened)."""
    return (any(os.path.lexists(slot_dir / "keep" / n) for n in RH.CREDENTIAL_NAME.values())
            or os.path.lexists(slot_dir / "baseline"))


def write_initial_slot_state(store: Path, slot: SlotId, *, clock: Callable[[], float] = time.time,
                             waits: RH.Seams | None = None) -> tuple[bool, str]:
    """A slot an install makes starts ``reusable`` with ``rev`` 0 and ``lease_seq`` 0 (DECISION: §3.5 fixes only
    that both count up from here and are never reset), root 0644 (§2.1), written whole and durably under the pool
    lock in the runtime helper's own encoding (:func:`runtime_helper.slot_state_bytes`, the one definition).

    Written only when absent: a re-run must never put a slot back to ``rev`` 0 over a newer state. A present
    file is parsed instead, and one that does not parse fails the step."""
    path = slot.slot_dir(Path(store)) / "state"
    with pool_lock(store, waits=waits):
        raw = R.read_record(path)
        if raw is not None:
            have = RH.parse_slot_state_of(raw, slot)
            return True, f"{slot.tag} already has its state ({have.state}, rev {have.rev})"
        state = R.SlotState(slot, rev=0, lease_seq=0, state="reusable",
                             dispatch_eligible=not _operator_held(slot.slot_dir(Path(store))),
                             changed_at=R.timestamp(clock()))
        R.write_durably(path, R.slot_state_bytes(state), mode=0o644)
    return True, ""


def remove_slot_state(store: Path, slot: SlotId, *, waits: RH.Seams | None = None) -> tuple[bool, str]:
    """Undo: the slot's state file goes, under the pool lock, so no ``prepare`` reads the slot as reusable."""
    path = slot.slot_dir(Path(store)) / "state"
    with pool_lock(store, missing_ok=True, waits=waits):
        try:
            os.unlink(path)
        except FileNotFoundError:
            return True, "already gone"
    return True, ""


def slot_state_steps(host: HostOS, slots: Iterable[SlotId], *, store: Path | None = None) -> list[Step]:
    L = Path(store) if store is not None else STORE[host]
    return [Step(f"mark {s.tag} reusable in {s.slot_dir(L) / 'state'} (levain-slot-state/1, under the pool lock)",
                 call=lambda s=s: write_initial_slot_state(L, s))
            for s in sorted(set(slots))]


def slot_state_undo_steps(host: HostOS, slots: Iterable[SlotId], *, store: Path | None = None) -> list[Step]:
    L = Path(store) if store is not None else STORE[host]
    return [Step(f"remove {s.slot_dir(L) / 'state'} (under the pool lock)", call=lambda s=s: remove_slot_state(L, s))
            for s in sorted(set(slots))]


# --- setup -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reuse:
    revive: dict[str, int]              # singleton tombstones, at their recorded id (§20)
    keep: dict[str, int]                # accounts an interrupted setup made (§23)
    keep_shared: dict[str, int]         # shared groups an interrupted setup made (§23)
    kept_slots: tuple[SlotId, ...]      # the slots whose accounts those are


def _reuse(facts: HostFacts, slots: int, verifier_slots: int) -> _Reuse:
    """CONTRACT §20 + §23: what setup brings back or keeps, and what it refuses as ``account-exists``.

    A singleton carrying Levain's live marker at an id in its range is kept (an interrupted setup made
    it); one carrying the retired marker at such an id is revived; any other present singleton or
    shared-group name is refused. Slot accounts carrying the live marker are kept with their slot."""
    r = RANGES[facts.host]
    live = dict(facts.levain_live)
    revive: dict[str, int] = {}
    keep: dict[str, int] = {}
    keep_shared: dict[str, int] = {}
    taken: list[str] = []
    if facts.intent is not None and facts.intent.verb == "setup":
        # §25: what the journal names, present at its journaled id, is ours with or without a marker. A
        # singleton tombstone stays on the revive path below: until it is relabelled it is not live.
        ours, foreign = _journal_presence(facts)
        taken += foreign
        for name, i in ours.items():
            if name not in facts.levain_tombstones:
                live.setdefault(name, i)
    for name in SINGLETONS:
        cur, tomb = live.get(name), facts.levain_tombstones.get(name)
        if cur is not None and in_own_range(facts.host, name, cur):
            keep[name] = cur
        elif cur is not None:
            taken.append(name)
        elif tomb is not None and tomb in r.singletons:
            revive[name] = tomb
        elif tomb is not None or name in facts.existing_names:
            taken.append(name)
    for g in SHARED_GROUPS:
        cur = live.get(g)
        if cur is not None and isinstance(cur, int) and cur in r.shared:
            keep_shared[g] = cur
        elif cur is not None or g in facts.existing_names:
            taken.append(g)
    kept: set[SlotId] = set()
    for name, uid in sorted(live.items()):
        m = SLOT_NAME_RE.fullmatch(name)
        if m is None or not is_levain_account(name):
            continue
        if not in_own_range(facts.host, name, uid):
            taken.append(name)
            continue
        keep[name] = uid
        kept.add(SlotId("egress" if m.group(1) in ("s", "gw") else "none", int(m.group(2))))
    if taken:
        raise Refused("account-exists", ", ".join(taken))
    for cls, want in (("egress", slots), ("none", verifier_slots)):
        have = sum(1 for s in kept if s.slot_class == cls)
        if have > want:
            raise Refused("account-exists", f"an interrupted setup made {have} {cls} slots; re-run it with "
                                            f"at least that many")
    return _Reuse(revive, keep, keep_shared, tuple(sorted(kept)))


def _journal_presence(facts: HostFacts) -> tuple[dict[str, int], list[str]]:
    """CONTRACT §25: (the journal's names present on the host at their journaled id, which are Levain's;
    the names that refuse as ``account-exists``: present at another id, or absent while their id is in
    use by something else)."""
    assert facts.intent is not None
    ours: dict[str, int] = {}
    foreign: list[str] = []
    for name, i in sorted(facts.intent.names.items()):
        have = facts.host_ids.get(name)
        if have == i:
            ours[name] = i
        elif have is not None:
            foreign.append(f"{name} (journaled at {i}, present at {have})")
        elif i in facts.used_ids:
            foreign.append(f"{name} (journaled id {i} is in use)")
    return ours, foreign


def _journal_slots(intent: Intent) -> dict[SlotClass, tuple[SlotId, ...]]:
    """The slots a journal allocated, by class. Every account of each is journaled and nothing else
    but singletons, or the journal is not one a plan wrote."""
    slots = slots_of(intent.accounts)
    names = {n for s in slots for n in s.accounts}
    odd = (set(intent.accounts) ^ names) - set(SINGLETONS)
    if odd:
        raise Refused("intent-malformed", f"the journal's accounts are not whole slots: {', '.join(sorted(odd))}")
    return {cls: tuple(s for s in slots if s.slot_class == cls) for cls in ("egress", "none")}  # type: ignore[misc]


def _levain_package() -> Path:
    import levain

    return Path(levain.__file__).resolve().parent


def plan_setup(cmd: InstallCommand, facts: HostFacts, *,
               realpath: Callable[[str], str] = os.path.realpath,
               lstat: Callable[[str], os.stat_result] = os.lstat,
               read_sysctl: Callable[[str], str] | None = None,
               source: Path | None = None,
               program_exists: Callable[[Path], bool] = Path.is_file) -> InstallPlan:
    """``read_sysctl``: reads a ``/proc/sys`` path (Linux, the AppArmor userns check); ``source``: the
    ``levain`` package copied into the root import tree (default: the one running);
    ``program_exists``: whether a service's program file is in ``source`` (§23)."""
    if cmd.verb != "setup" or cmd.slots is None or cmd.verifier_slots is None or cmd.operator is None:
        raise Refused("wrong-verb", cmd.verb)
    journal = facts.intent
    if journal is not None and journal.verb != "setup":
        # Another verb was interrupted: that verb finishes it, never a setup planned over its half.
        raise Refused("install-interrupted", journal.verb)
    preflight(cmd, facts)
    host = facts.host
    assert facts.operator_uid is not None
    roots = check_roots(cmd.roots, facts.operator_uid, realpath=realpath, lstat=lstat)
    reuse = _reuse(facts, cmd.slots, cmd.verifier_slots)
    check_capacity(host, cmd.slots, cmd.verifier_slots, facts.retired)
    chosen: dict[SlotClass, tuple[SlotId, ...]] = {}
    keep, keep_shared = dict(reuse.keep), dict(reuse.keep_shared)
    if journal is not None:
        # §25: an interrupted setup is re-planned at the journal's allocation, the same names at the same
        # ids, never allocated afresh; so it is re-run as it was first asked for.
        chosen = _journal_slots(journal)
        asked = (cmd.operator, cmd.slots, cmd.verifier_slots)
        was = (journal.operator, len(chosen["egress"]), len(chosen["none"]))
        if asked != was:
            raise Refused("install-interrupted", f"setup: re-run it as setup --slots {was[1]} "
                                                 f"--verifier-slots {was[2]} --operator {was[0]}")
        if not set(SINGLETONS) <= set(journal.accounts) or set(journal.groups) != set(SHARED_GROUPS):
            raise Refused("intent-malformed", "a setup journal names every singleton and shared group")
        unjournaled = sorted((set(keep) - set(journal.accounts)) | (set(keep_shared) - set(journal.groups)))
        if unjournaled:
            raise Refused("account-exists", ", ".join(unjournaled))
        keep.update((n, i) for n, i in journal.accounts.items() if n not in reuse.revive)
        keep_shared.update(journal.groups)
    else:
        # Slot, front and verifier-pair tombstones are never revived: their indices are spent (§20).
        # Slots an interrupted setup made come first, at their own indices (§23).
        for cls, want in (("egress", cmd.slots), ("none", cmd.verifier_slots)):
            mine = tuple(s for s in reuse.kept_slots if s.slot_class == cls)
            spent = spent_indices(cls, facts.existing_names) | {s.index for s in mine}
            chosen[cls] = mine + choose_indices(cls, want - len(mine), spent)
    alloc = allocate(host, egress=chosen["egress"], verifier=chosen["none"], used=facts.used_ids,
                     retired=facts.retired, singletons=True, shared=True, revive=reuse.revive,
                     keep=keep, keep_shared=keep_shared)
    accounts = accounts_of(alloc)
    fresh = tuple(a for a in accounts if a.name not in reuse.revive)
    revived = tuple(a for a in accounts if a.name in reuse.revive)
    groups = shared_groups_of(alloc, cmd.operator)
    early = policy_rows(host)
    rows = ([r for r in base_store_rows(host, cmd.operator) if r not in early] + base_run_rows(host)
            + slot_rows(host, alloc.slots))
    package = source or _levain_package()
    services, skipped = units.built_services(host, package, exists=program_exists)
    if host == "linux":
        restricted = (bwrap.userns_restricted() if read_sysctl is None
                      else bwrap.userns_restricted(read_sysctl))
        confinement = [*bwrap.bwrap_steps(apparmor_userns_restricted=restricted), *netns.netns_steps(alloc.slots)]
        boot_dirs = tmpfiles_steps(host, alloc.slots)
    else:
        # Each egress slot's lo0 alias (§24) before the rules that name it.
        confinement = [*pf.alias_steps(alloc.slots), *pf.load_steps(alloc.slots, alloc.ids)]
        boot_dirs = []
    intent = Intent("setup", cmd.operator, dict(alloc.ids), dict(alloc.shared))
    steps: list[Step] = [
        # FIRST (§25): the exact allocation, before anything is made, so an interrupted run re-plans it.
        intent_write_step(host, intent),
        *row_steps(host, early),
        *shared_group_steps(host, groups),
        *account_steps(host, fresh),
        *revive_steps(host, revived),
        *membership_steps(host, membership_pairs(accounts, cmd.operator)),
        *row_steps(host, rows),
        *boot_dirs,
        *reserve_steps(host),
        *single_filesystem_steps(host, alloc.slots),
        *base_routes_steps(host),
        *roots_steps(host, roots),
        *pool_config_steps(host, cmd.slots, cmd.verifier_slots),
        *root_tree.tree_steps(host, package),
        *confinement,
        *units.install_steps(host, services),
        # The quiesce, if the pool lock was not there to take at the live-lease check (a fresh setup).
        quiesce_step(host),
        # Each slot's §3.5 Pool state once everything the slot needs is in place, before the rule that lets
        # prepare run (prepare picks only from these).
        *slot_state_steps(host, alloc.slots),
        *sudoers_steps(host, cmd.operator),
        # LAST (§23): only a complete install is `already-set-up`; until this, setup re-runs.
        Step(f"record {cmd.operator} as this machine's one operator (last)",
             write=(operator_record_path(host), operator_record_text(cmd.operator, facts.operator_uid), 0o644)),
        # After the record: a run dying between the two leaves a finished install with a stale setup
        # journal, which replenish overwrites and undo removes.
        intent_remove_step(host),
    ]
    check_tools(facts, steps, verb="setup", python=True)
    kept = tuple(sorted(reuse.keep)) + tuple(sorted(reuse.keep_shared))
    return InstallPlan("setup", host, tuple(steps), alloc, accounts, groups,
                       tuple(early + rows + [reserve_row(host), pool_config_row(host)]), roots, facts.retired,
                       revived=tuple(a.name for a in revived), kept=kept,
                       pending=helper_pending() + skipped, services=tuple(s.name for s in services),
                       intent=intent)


# --- undo ------------------------------------------------------------------------------------------


def slots_of(names: Iterable[str]) -> tuple[SlotId, ...]:
    """The slots whose primary accounts (``_levain_s<n>``, ``_levain_v<n>``) are among ``names``."""
    out = set()
    for name in names:
        m = SLOT_NAME_RE.fullmatch(name)
        if m and m.group(1) in ("s", "v") and is_levain_account(name):
            out.add(SlotId("egress" if m.group(1) == "s" else "none", int(m.group(2))))
    return tuple(sorted(out))


def check_account_ids(host: HostOS, accounts: Mapping[str, int]) -> None:
    """CONTRACT §23: every name a Levain account name, every uid in §1.1's range for its class, so no
    caller-supplied id can aim a kill or a retire at an account that is not one of ours."""
    for name, uid in accounts.items():
        if not is_levain_account(name):
            raise Refused("not-levain-name", repr(name))
        if not in_own_range(host, name, uid):
            raise Refused("bad-token", f"{name}: uid {uid!r} is not in its §1.1 range")


def _abandoned_setup(facts: HostFacts) -> Intent | None:
    """CONTRACT §25 (c7.7a): the setup journal undo runs against when there is no operator record (the
    setup died before writing it), or None when undo takes its ordinary path. With no record, only the
    journal's operator may run it, by the name the caller's uid resolves to; a live lease still refuses."""
    j = facts.intent
    if facts.operator_record is not None or j is None or j.verb != "setup":
        return None
    if facts.live_leases > 0:
        raise Refused("live-lease", _live_lease_why("undo", facts))
    if facts.caller_uid == 0 or facts.caller_name != j.operator:
        raise Refused("operator-mismatch", f"the interrupted setup was for {j.operator!r}")
    return j


def _end_processes(host: HostOS, uid: int, name: str, *, bootout: Callable[..., bool] = PR.darwin_bootout,
                   kill_all: Callable[[int], tuple[bool, str]] = _kill_all,
                   none_left: Callable[[int], bool] = lambda u: PR.UidProcs().no_process(u)) -> tuple[bool, str]:
    """macOS first ends the uid's launchd domain, so no agent is respawned for it after the kill (an undo once
    left a slot's distnoted running past rc 0: levain-memory runs/s2-macos/raw_d/998_undo.txt); then every
    process is killed, and the proof (two scans, no process of the uid) refuses by the account's name. A bootout
    launchctl did not report done refuses ``domain-remains:<name>`` (the account is never removed under it)."""
    if host == "darwin" and not bootout(uid):
        return False, f"domain-remains:{name}: launchctl did not report uid {uid}'s domain booted out"
    ok, why = kill_all(uid)
    if not ok:
        return False, why
    if not none_left(uid):
        return False, f"processes-remain:{name}: a process of uid {uid} is running after the kill"
    return True, ""


def plan_undo(cmd: InstallCommand, facts: HostFacts, *, accounts: Mapping[str, int],
              shared_groups: Iterable[str]) -> InstallPlan:
    """``accounts``: every live Levain account the directory service holds, name -> uid (== gid);
    ``shared_groups``: the shared groups present. Order (§23): the runtime sudoers rule first (no new
    runtime-helper calls), every service stopped and its unit removed, ids retired, processes stopped,
    accounts tombstoned, every dir naming a shared group given back to root's group, the shared groups
    deleted, slot confinement removed (Linux netns + the slots' bwrap + tmpfiles.d; macOS the slots'
    lo0 aliases, the pf rules and pf's enable reference), the install sudoers rule, the intent journal,
    the operator record LAST. Every step is safe to re-run. The store and the root import tree stay.
    With a journal (§25), the accounts and groups it names that are present at their journaled ids are
    tombstoned and deleted with the rest. With no operator record and a SETUP journal (the setup died
    before its record), undo runs against the journal alone, for its operator, and ends by removing the
    journal: there is no record to remove."""
    if cmd.verb != "undo":
        raise Refused("wrong-verb", cmd.verb)
    abandoned = _abandoned_setup(facts)
    if abandoned is None:
        preflight(cmd, facts)
        rec = facts.operator_record
        assert rec is not None and isinstance(rec.get("name"), str)
        operator = str(rec["name"])
    else:
        # §25 (c7.7a): only the journal speaks for what that setup made, so the directory service's
        # reports are set aside and the journal's own names, at their journaled ids, are undone.
        operator = abandoned.operator
        accounts, shared_groups = {}, ()
    host = facts.host
    check_account_ids(host, accounts)
    accounts = dict(accounts)
    journaled_groups: list[str] = []
    if facts.intent is not None:
        # §25: whatever an interrupted run made at its journaled ids goes with the rest, a Linux shared
        # group with no label included. A journaled name present at another id is not ours: left alone.
        ours, _ = _journal_presence(facts)
        for name, i in ours.items():
            if name in facts.intent.accounts:
                accounts.setdefault(name, i)
            else:
                journaled_groups.append(name)
        check_account_ids(host, accounts)
    groups = list(dict.fromkeys(g for g in (*shared_groups, *journaled_groups) if g in SHARED_GROUPS))
    retired_after = frozenset(facts.retired) | frozenset(accounts.values())
    path = RETIRED_IDS_PATH[host]
    mkdir = "/bin/mkdir" if host == "darwin" else _abs("mkdir")
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    steps: list[Step] = [
        *remove_runtime_sudoers_steps(host),
        # Every service stops before anything is removed; their unit files go with them.
        *units.uninstall_steps(host),
        # With the services stopped nothing rewrites them: no slot reads as reusable from here on.
        *slot_state_undo_steps(host, slots_of(accounts)),
        *pool_config_undo_steps(host),
        Step(f"create {path.parent}", (mkdir, "-p", "-m", "755", str(path.parent))),
        Step("retire every account id so none is reissued", write=(path, retired_ids_text(retired_after), 0o644)),
    ]
    for name, uid in sorted(accounts.items()):
        steps.append(Step(f"stop every process of {name}, and check they are gone",
                          call=lambda u=uid, n=name: _end_processes(host, u, n)))
    for name in sorted(accounts):
        steps += tombstone_steps(host, name)
    steps += ungroup_steps(host, operator)
    steps += delete_group_steps(host, groups)
    # Slot confinement goes only after every slot process is stopped and its account tombstoned.
    if host == "linux":
        steps += (netns.netns_undo_steps(slots_of(accounts)) + bwrap.bwrap_undo_steps() + tmpfiles_undo_steps()
                  + run_dir_undo_steps(host))
    else:
        # The slots' lo0 aliases go before the rules guarding them are flushed (§24).
        steps += pf.alias_undo_steps(slots_of(accounts)) + pf.unload_steps()
    steps += remove_install_sudoers_steps(host)
    # Just before the record (§25): until undo is complete a re-run still reads what the journal names.
    steps.append(intent_remove_step(host))
    if abandoned is None:
        steps.append(Step("remove the operator record (last)", (rm, "-f", str(operator_record_path(host)))))
    check_tools(facts, steps, verb="undo")
    return InstallPlan("undo", host, tuple(steps), retired_after=retired_after)


# --- replenish -------------------------------------------------------------------------------------


def plan_replenish(cmd: InstallCommand, facts: HostFacts, *, slots: int | None, verifier_slots: int | None,
                   live: Mapping[SlotClass, Iterable[int]],
                   accounts: Mapping[str, int] | None = None,
                   configured: Mapping[SlotClass, Iterable[int]] | None = None) -> InstallPlan:
    """Bring each class back to its configured count (``slots``, ``verifier_slots``: ``L/policy/pool``) with NEW
    indices (a retired index is never reissued); None = no ``L/policy/pool``, refused ``pool-config-missing``
    after the shared refusals. ``live``: the indices of each class still in service;
    ``configured``: every index ``L/slots/`` holds, retired ones included, none of which is chosen again;
    ``accounts``: the in-service slots' accounts, name -> uid (macOS: the pf rules name every slot in service by
    uid, §23). With a replenish journal (§25) the plan is that journal's slots, resumed, and nothing more."""
    if cmd.verb != "replenish":
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    if slots is None or verifier_slots is None:
        raise Refused("pool-config-missing", "L/policy/pool: no configured counts to replenish to")
    host = facts.host
    rec = facts.operator_record
    assert rec is not None
    accounts = dict(accounts or {})
    check_account_ids(host, accounts)
    check_capacity(host, slots, verifier_slots, facts.retired)
    journal = facts.intent
    new: dict[SlotClass, tuple[SlotId, ...]] = {}
    if journal is not None and journal.verb == "replenish":
        # §25: an interrupted replenish is resumed at the slots, indices and ids it journaled, and no new
        # index is chosen until its journal is gone; the next run fills to the configured counts.
        if journal.operator != rec.get("name"):
            raise Refused("install-interrupted", f"replenish: journaled for {journal.operator!r}")
        _, foreign = _journal_presence(facts)
        if foreign:
            raise Refused("account-exists", ", ".join(foreign))
        new = _journal_slots(journal)
        alloc = allocate(host, egress=new["egress"], verifier=new["none"], used=facts.used_ids,
                         retired=facts.retired, keep=journal.accounts)
    else:
        # A setup journal here is stale (the operator record, written before its removal, proves that
        # setup finished); this run's journal write replaces it.
        for cls, want in (("egress", slots), ("none", verifier_slots)):
            have = set(live.get(cls, ()))
            spent = spent_indices(cls, facts.existing_names) | have | set((configured or {}).get(cls, ()))
            new[cls] = choose_indices(cls, max(0, want - len(have)), spent)
        alloc = allocate(host, egress=new["egress"], verifier=new["none"], used=facts.used_ids,
                         retired=facts.retired)
    made = accounts_of(alloc)
    rows = slot_rows(host, alloc.slots)
    resumed = set(alloc.slots)
    in_service = tuple(s for s in (SlotId(cls, i) for cls in ("egress", "none") for i in live.get(cls, ()))  # type: ignore[arg-type]
                       if s not in resumed)
    if host == "linux":
        # Every slot in service keeps its boot-time run dirs; the slots' bwrap re-copied if the system's changed.
        confinement = [*netns.netns_steps(alloc.slots), *tmpfiles_steps(host, in_service + alloc.slots),
                       *bwrap.copy_steps()]
    else:
        # The rules re-rendered for every slot in service, the new ones with it, so no slot runs
        # without its pf rules; every egress slot's alias (§24) added where lo0 lacks it.
        confinement = [*pf.alias_steps(in_service + alloc.slots),
                       *pf.load_steps(in_service + alloc.slots, {**accounts, **alloc.ids})]
    intent = Intent("replenish", str(rec.get("name")), dict(alloc.ids))
    steps = [
        # FIRST (§25): the slots this run makes, so a run that dies part-way resumes them, never spends more.
        intent_write_step(host, intent),
        *account_steps(host, made),
        *membership_steps(host, membership_pairs(made, None)),
        *row_steps(host, rows),
        *confinement,
        # Only now may prepare take them: a new slot is reusable once its confinement is loaded (§3.5 Pool).
        *slot_state_steps(host, alloc.slots),
        intent_remove_step(host),   # LAST, after the confinement steps
    ]
    check_tools(facts, steps, verb="replenish")
    return InstallPlan("replenish", host, tuple(steps), alloc, made, (), tuple(rows), (), facts.retired,
                       intent=intent)


# --- the apply layer: ``__main__`` (CONTRACT §5 preamble, §5.2, §25) --------------------------------------


@dataclass(frozen=True)
class Directory:
    """The host's directory service, read once under the install lock: name -> id, and each name's label (macOS
    ``RealName``; Linux a user's GECOS field, a group has none)."""

    users: Mapping[str, int]
    groups: Mapping[str, int]
    user_labels: Mapping[str, str] = field(default_factory=dict)
    group_labels: Mapping[str, str] = field(default_factory=dict)


def _lines(run: Callable[..., object], argv: Sequence[str]) -> list[str]:
    r = run(list(argv), capture_output=True, text=True, cwd="/", env=child_env())
    if r.returncode != 0:
        raise Refused("host-read-failed", f"{' '.join(argv)}: {(r.stderr or '').strip()[:200]}")
    return r.stdout.splitlines()


def _id(token: str, line: str) -> int:
    try:
        return int(token)
    except ValueError:
        raise Refused("host-read-failed", f"no id in {line[:120]!r}") from None


def read_directory(host: HostOS, *, run: Callable[..., object] = subprocess.run) -> Directory:
    """Every user and group with its id and label. A read that fails, or a line that does not parse, refuses
    ``host-read-failed``: an id the planner never saw is an id it could hand out again."""
    users: dict[str, int] = {}
    groups: dict[str, int] = {}
    ulab: dict[str, str] = {}
    glab: dict[str, str] = {}
    if host == "darwin":
        dscl = "/usr/bin/dscl"
        for kind, key, into in (("/Users", "UniqueID", users), ("/Groups", "PrimaryGroupID", groups)):
            for ln in _lines(run, (dscl, ".", "-list", kind, key)):
                p = ln.split()
                if not p:
                    continue
                if len(p) != 2:
                    raise Refused("host-read-failed", f"dscl {kind} {key}: {ln[:120]!r}")
                into[p[0]] = _id(p[1], ln)
        for kind, into in (("/Users", ulab), ("/Groups", glab)):
            for ln in _lines(run, (dscl, ".", "-list", kind, "RealName")):
                p = ln.split(None, 1)
                if p:
                    into[p[0]] = " ".join(p[1].split()) if len(p) > 1 else ""
    elif host == "linux":
        getent = _abs("getent")
        for ln in _lines(run, (getent, "passwd")):
            if not ln:
                continue
            p = ln.split(":")
            if len(p) != 7:
                raise Refused("host-read-failed", f"getent passwd: {ln[:120]!r}")
            users[p[0]] = _id(p[2], ln)
            ulab[p[0]] = p[4]
        for ln in _lines(run, (getent, "group")):
            if not ln:
                continue
            p = ln.split(":")
            if len(p) != 4:
                raise Refused("host-read-failed", f"getent group: {ln[:120]!r}")
            groups[p[0]] = _id(p[2], ln)
    else:
        raise Refused("unsupported-host", repr(host))
    if users.get("root") != 0:
        raise Refused("host-read-failed", "the directory service lists no root at uid 0")
    return Directory(users, groups, ulab, glab)


def live_lease_sessions(sessions: Path) -> tuple[str, ...]:
    """Root's read of §3.1: the sessions whose state record CONTROL marks ``lease_live``. DECISION: a record that
    does not parse counts as live, so an install verb refuses rather than act beside a session it cannot read."""
    try:
        names = os.listdir(sessions)
    except FileNotFoundError:
        return ()
    live = []
    for name in sorted(names):
        if not name.endswith(".json") or not SESSION_RE.fullmatch(name[:-len(".json")]):
            continue
        try:
            raw = R.read_record(Path(sessions) / name)
            if raw is not None and R.parse_state(raw).lease_live:
                live.append(name[:-len(".json")])
        except (Refused, OSError):
            live.append(name[:-len(".json")])
    return tuple(live)


def count_live_leases(sessions: Path) -> int:
    """How many leases :func:`live_lease_sessions` finds."""
    return len(live_lease_sessions(sessions))


def _live_lease_why(verb: str, facts: HostFacts) -> str:
    """The refusal names the sessions (VM-Y Y7a: an operator met ``live-lease`` with no session named; a lease
    stays live until that session's END, even when its holder has died)."""
    return f"{verb} refuses while {facts.live_leases} lease(s) are live" + (
        f": {_lease_list(facts.live_sessions)}" if facts.live_sessions else "")


def _lease_list(leased: Sequence[str]) -> str:
    """Sessions are named for the operator to end; a ``slot <name>`` is a prepare or END in flight, to wait out."""
    sessions = [x for x in leased if not x.startswith("slot ")]
    slots = [x for x in leased if x.startswith("slot ")]
    parts = ([f"session(s) {', '.join(sessions)} (end each first)"] if sessions else []) + (
        [f"{', '.join(slots)} busy (a prepare or END in flight; retry)"] if slots else [])
    return "; ".join(parts)


def slots_in_service(store: RH.Store) -> dict[str, tuple[int, ...]]:
    """The configured slots (the runtime helper's own reading of ``L/slots/``) whose state file is not
    ``retired``. DECISION: a slot with no state file is in service (one an install made before state files)."""
    out: dict[str, tuple[int, ...]] = {}
    for cls, indices in RH.configured_slots(store).items():
        keep = []
        for i in sorted(indices):
            view = RH.read_view(store, SlotId(cls, i))   # type: ignore[arg-type]
            if view.state is None or view.state.state != "retired":
                keep.append(i)
        out[cls] = tuple(keep)
    return out


@dataclass(frozen=True)
class Gathered:
    """What :func:`gather_facts` read under the install lock: the facts every plan takes, plus each verb's own
    inputs (undo: every live Levain account and the shared groups present; replenish: the slots in service, by
    class, and their accounts)."""

    facts: HostFacts
    accounts: Mapping[str, int] = field(default_factory=dict)
    shared_groups: tuple[str, ...] = ()
    live: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    #: The pool-lock hold taken before the live-lease count, released by :func:`run` after the last act.
    quiesce: Quiesce | None = None
    #: replenish: ``L/policy/pool``'s ``(slots, verifier_slots)``, or None when it is absent.
    pool: tuple[int, int] | None = None
    #: replenish: every index ``L/slots/`` holds, by class, retired ones included.
    configured: Mapping[str, tuple[int, ...]] = field(default_factory=dict)


def _operator_of(store: RH.Store) -> Mapping[str, object] | None:
    """``L/policy/operator`` parsed, or None when it (or ``L/policy/``) is absent: not set up."""
    try:
        raw = store.read(("policy",), "operator")
    except Refused as e:
        if e.name != "store-missing":
            raise
        return None
    return parse_operator_record(raw) if raw is not None else None


def _user_name(uid: int) -> str | None:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def gather_facts(cmd: InstallCommand, host: HostOS, caller_uid: int, *,
                 run: Callable[..., object] = subprocess.run, store_seams: RH.Seams | None = None,
                 state_dir: Path | None = None,
                 user_name: Callable[[int], str | None] = _user_name) -> Gathered:
    """CONTRACT §25: everything a plan reads, read while the caller holds the install lock. ``store_seams``: the
    runtime helper's store access (``L`` and the trust check on its dirs); ``state_dir``: the dir holding the
    retired ids and the journal (default beside :data:`~levain.slot.ids.RETIRED_IDS_PATH`).

    Accounts and groups are Levain's by label (§23) or, with a journal, by name AND journaled id (§25: a Linux
    group carries no label)."""
    d = read_directory(host, run=run)
    rs = store_seams or RH.Seams(host=host)
    L = rs.L
    q = Quiesce(L, waits=rs) if cmd.verb not in LEASE_EXEMPT else None
    try:
        g = _gather_held(cmd, host, caller_uid, d, rs, L, q, state_dir=state_dir, user_name=user_name)
    except BaseException:
        if q is not None:
            q.release()
        raise
    return g


def _gather_held(cmd: InstallCommand, host: HostOS, caller_uid: int, d: Directory, rs: RH.Seams, L: Path,
                 q: Quiesce | None, *, state_dir: Path | None, user_name: Callable[[int], str | None]) -> Gathered:
    if q is not None and os.path.lexists(L):
        q.hold()        # before the live-lease count below, so the count is one no prepare can change
    retired_path = Path(state_dir) / RETIRED_IDS_PATH[host].name if state_dir else RETIRED_IDS_PATH[host]
    intent_path = Path(state_dir) / INTENT_PATH[host].name if state_dir else INTENT_PATH[host]
    orec, in_service, pool, configured = None, None, None, {}
    if os.path.lexists(L):
        # Read as the runtime helper reads the store: one dirfd, every component O_NOFOLLOW, root's dirs only.
        store = RH.Store(rs)
        try:
            orec = _operator_of(store)
            if cmd.verb == "replenish" and orec is not None:
                in_service = slots_in_service(store)
                configured = {cls: tuple(sorted(i)) for cls, i in RH.configured_slots(store).items()}
                raw = store.read(("policy",), "pool")
                pool = parse_pool_config(raw) if raw is not None else None
        finally:
            store.close()
    journal = read_intent(host, path=intent_path)
    live: dict[str, int] = {}
    tombs: dict[str, int] = {}
    accounts: dict[str, int] = {}
    for name, uid in d.users.items():
        if not name.startswith("_levain_"):
            continue
        label = d.user_labels.get(name, "")
        if label == SLOT_MARKER:
            live[name] = accounts[name] = uid
        elif label == SLOT_RETIRED_MARKER[host]:
            tombs[name] = uid
    shared = tuple(sorted(g for g in d.groups if g in SHARED_GROUPS))
    if host == "darwin":
        live.update((g, d.groups[g]) for g in shared if d.group_labels.get(g) == SLOT_MARKER)
    host_ids: dict[str, int] = {}
    if journal is not None:
        host_ids.update((n, d.users[n]) for n in journal.accounts if n in d.users)
        host_ids.update((g, d.groups[g]) for g in journal.groups if g in d.groups)
    leased = q.live() if q is not None else live_lease_sessions(L / "sessions")
    facts = HostFacts(
        host=host, caller_uid=caller_uid, caller_name=user_name(caller_uid),
        operator_uid=d.users.get(cmd.operator) if cmd.operator else None, operator_record=orec,
        live_leases=len(leased), live_sessions=leased,
        used_ids=frozenset(d.users.values()) | frozenset(d.groups.values()),
        existing_names=frozenset(d.users) | frozenset(d.groups), retired=read_retired_ids(retired_path),
        levain_tombstones=tombs, levain_live=live, intent=journal, host_ids=host_ids)
    if cmd.verb == "undo":
        return Gathered(facts, accounts, shared, quiesce=q)
    if in_service is not None:
        wanted = {n for cls, idx in in_service.items() for i in idx for n in SlotId(cls, i).accounts}  # type: ignore[arg-type]
        return Gathered(facts, {n: u for n, u in accounts.items() if n in wanted}, live=in_service, quiesce=q,
                        pool=pool, configured=configured)
    return Gathered(facts, quiesce=q)


def plan_for(cmd: InstallCommand, g: Gathered, **setup_kw) -> InstallPlan:
    """The verb's plan from what was gathered. ``setup_kw`` reaches :func:`plan_setup` (tests: its host seams).
    Replenish fills to ``L/policy/pool`` (head ruling 8) and refuses ``pool-config-missing`` without it."""
    if cmd.verb == "setup":
        return plan_setup(cmd, g.facts, **setup_kw)
    if cmd.verb == "undo":
        return plan_undo(cmd, g.facts, accounts=g.accounts, shared_groups=g.shared_groups)
    if cmd.verb == "replenish":
        slots, verifier_slots = g.pool if g.pool is not None else (None, None)
        return plan_replenish(cmd, g.facts, slots=slots, verifier_slots=verifier_slots, live=g.live,
                              accounts=g.accounts, configured=g.configured)
    raise Refused(f"verb-unbuilt:{cmd.verb}", cmd.verb)


def apply_steps(plan: InstallPlan, err: TextIO) -> str | None:
    """Run the plan's steps in order with :func:`levain.firing.hands.run_plan` (it reads only ``steps``), progress
    on ``err``. None when every step ran or was skipped; else the failing step's line and why."""
    seen = {"step": "", "why": ""}

    def emit(text: str) -> None:
        err.write(text + "\n")
        if text.startswith("["):
            seen["step"] = text.split("\n", 1)[0]
        elif text.lstrip().startswith("FAILED:"):
            seen["why"] = text.strip()

    if run_plan(plan, dry_run=False, emit=emit) == 0:   # type: ignore[arg-type]
        return None
    return f"{seen['step']} {seen['why']}".strip()


def _host() -> HostOS:
    try:
        return host_os()
    except HandsSetupError as e:
        raise Refused("unsupported-host", str(e)) from None


@dataclass
class Seams:
    """Every act :func:`main` takes that a unit test cannot take as a non-root user, with its real default."""

    euid: Callable[[], int] = os.geteuid
    host: Callable[[], HostOS] = _host
    lock: Callable[[HostOS], ContextManager[object]] = install_lock
    gather: Callable[[InstallCommand, HostOS, int], Gathered] = gather_facts
    plan: Callable[[InstallCommand, Gathered], InstallPlan] = plan_for
    apply: Callable[[InstallPlan, TextIO], Optional[str]] = apply_steps


def run(argv: Sequence[str], *, env: Mapping[str, str], seams: Seams, err: TextIO) -> dict[str, object]:
    """The verb's ok fields, or :class:`Refused`. The order is §25's: the install lock is taken BEFORE the host's
    facts are gathered, and held through the plan, its first step (the journal), every act, the record and the
    journal's removal, so no second run plans against this one's half. Nothing before the lock reads the host.
    Inside it, the gather's :class:`Quiesce` (the pool lock) is held through the last act too."""
    if seams.euid() != 0:
        raise Refused("not-root", "the install helper runs as root")
    cmd = parse_install_argv(argv)
    if cmd.verb not in BUILT_VERBS:
        raise Refused(f"verb-unbuilt:{cmd.verb}", cmd.verb)
    caller_uid = RH._sudo_uid(env)
    host = seams.host()
    with seams.lock(host):
        gathered = seams.gather(cmd, host, caller_uid)
        try:
            plan = seams.plan(cmd, gathered)
            for line in plan.pending:
                err.write(f"levain install-helper: pending: {line}\n")
            failed = seams.apply(plan, err)
        finally:
            if gathered.quiesce is not None:
                gathered.quiesce.release()     # the pool lock, after the last act and before the install lock
        if failed is not None:
            raise Refused("step-failed", failed)
    fields: dict[str, object] = {"verb": plan.verb, "steps": len(plan.steps)}
    if plan.allocation is not None:
        fields["slots"] = [s.tag for s in plan.allocation.slots]
    return fields


def main(argv: Sequence[str], *, env: Mapping[str, str], seams: Seams | None = None,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Exactly one canonical JSON line on ``out`` (``ok`` or ``refused`` by name); progress and every refusal's
    detail on ``err``. Exits as the runtime helper does: 0 ok, 1 refused, 70 internal."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        line = ok_line(run(argv, env=env, seams=seams or Seams(), err=err))
    except Refused as e:
        err.write(f"levain install-helper: refused {e.name}: {e.detail!r}\n")
        out.write(refusal_line(e))
        out.flush()
        return RH.EXIT_REFUSED
    except Exception as e:   # noqa: BLE001 - one line on stdout, whatever went wrong
        err.write(f"levain install-helper: internal error: {type(e).__name__}: {e}\n")
        out.write(refusal_line(Refused("internal")))
        out.flush()
        return RH.EXIT_INTERNAL
    out.write(line)
    out.flush()
    return RH.EXIT_OK



#: The only name :func:`main` reads from its environment (the caller, :func:`levain.slot.runtime_helper._sudo_uid`).
INSTALL_ENTRY_ENV = ("SUDO_UID",)


__all__ = ["BUILT_VERBS", "INSTALL_ENTRY_ENV", "Directory", "Gathered", "HostFacts", "InstallPlan", "OPERATOR_FORMAT", "Seams",
           "apply_steps", "check_account_ids", "check_tools", "count_live_leases", "live_lease_sessions", "extra_tools", "gather_facts",
           "helper_pending", "main", "operator_record", "operator_record_text", "parse_operator_record",
           "plan_for", "plan_replenish", "plan_setup", "plan_tools", "plan_undo", "pool_lock", "preflight", "Quiesce",
           "quiesce_step",
           "read_directory", "remove_slot_state", "run", "slot_state_steps", "slot_state_undo_steps",
           "slots_in_service", "write_initial_slot_state"]


if __name__ == "__main__":   # pragma: no cover - the root entry (root_tree.ENTRY_MODULES)
    raise SystemExit(main(sys.argv[1:], env=RH.entry_env(os.environ, INSTALL_ENTRY_ENV)))
