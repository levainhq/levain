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

``purge``, ``set-credential``, ``remove-credential`` and ``set-limits`` are not plans: they act under the install lock
(the credential verbs through a forked child that drops to keys before it opens anything below ``L/keys/``,
:func:`run_credential`; ``set-limits`` writes ``L/policy/limits`` in root, :func:`run_set_limits`). Nor are
``enrol-authority-signer`` / ``remove-authority-signer``: root's allowed-signers file, :func:`run_authority_signer`.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import functools
import json
import os
import re
import select
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, ClassVar, ContextManager, Iterable, Iterator, Mapping, Optional, Sequence, TextIO, Tuple

from levain.authority_sig import NAMESPACE as AUTHORITY_NAMESPACE, _Wire
from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HandsSetupError, HostOS, Step, _abs, _kill_all, host_os, run_plan
from levain.launch import child_env
from levain.slot import Refused, bwrap, netns, pf, root_tree, units, write_all
from levain.slot import removal as RM
from levain.slot import generation as gen_mod
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import tickets as T
from levain.slot import runtime_helper as RH
from levain.slot import store_verbs as SV
from levain.slot.root_intent import PhaseFailed
from levain.slot.intent import (INSTALL_LOCK, INTENT_PATH, Intent, install_lock, intent_remove_step, intent_text, intent_write_step,
                                parse_intent, read_intent, write_intent_atomically)
from levain.slot.accounts import (SLOT_MARKER, SLOT_RETIRED_MARKER, Account, Group, account_steps, accounts_of,
                                  membership_pairs, membership_steps, revive_steps,
                                  shared_group_steps, shared_groups_of, member_state, group_state, uid_retires,
                                  account_state, remove_member_steps, remove_group_steps, remove_account_steps)
from levain.slot.grammar import (KEY_TYPE_NAMES, LEASE_EXEMPT, PUBLIC_KEY_RE, ROUTES_REPLACE_INTENT, SESSION_RE, VERBS,
                                 InstallCommand, check_roots, parse_install_argv)
from levain.slot.ids import (RANGES, RETIRED_IDS_PATH, SHARED_GROUPS, SINGLETONS, SLOT_NAME_RE, Allocation,
                             SlotClass, SlotId, allocate, check_capacity, choose_indices, in_own_range,
                             is_levain_account, loopback_alias, read_retired_ids, retired_ids_text, spent_indices)
from levain.slot import limits_doc as LD
from levain.slot import vendor_routes as VR
from levain.broker import policy as broker_policy
from levain.slot.runtime_grammar import VERBS as RUNTIME_VERBS
from levain.slot.runtime_grammar import ok_line, refusal_line
from levain.slot.layout import (STORE, Row, base_routes_steps, base_run_rows, base_store_rows, operator_record_path,
                                parse_pool_config, policy_limits_path, policy_rows, pool_config_row, pool_config_steps,
                                pool_config_undo_steps, reserve_row, reserve_steps, roots_steps, row_steps,
                                run_dir_undo_steps, single_filesystem_steps, slot_rows, tmpfiles_steps,
                                tmpfiles_undo_steps, ungroup_steps, entry_state, remove_entry_steps)
from levain.slot.sudoers import (INSTALL_SUDOERS, RUNTIME_SUDOERS, config_check_step, dropin_state,
                                 remove_dropin_steps, sudoers_paths, sudoers_steps)

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
    #: ``L/policy/install-shape`` parsed (row 33), or None: a host set up before it, or never set up.
    install_shape: gen_mod.InstallShape | None = None
    #: Every user on the host, name -> uid, whatever its label: undo reads whether a recorded account is absent and
    #: whether another account holds its uid (L3 r2 codex HIGH).
    user_ids: Mapping[str, int] = field(default_factory=dict)


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
    generation: str | None = None        # the generation the plan makes current (row 33)
    generation_change: bool = False      # repair: the record is absent or names another generation (§2.3)
    handover: "root_tree.Staged | None" = None   # repair phase A: the staged id, filled by its hash step (§2.2)


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
    """Every host program a step runs: its argv's, its ``skip_if``'s and its validator's, and the ``tools`` a remove
    step (:class:`removal.RemoveStep`, a ``call`` whose argv no field names) declares."""
    return tuple(sorted({x[0] for s in steps for x in (s.argv, s.skip_if, s.validate) if x}
                        | {t for s in steps for t in getattr(s, "tools", ())}))


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
        copies = (bwrap.system_bwrap(),) if verb in ("setup", "replenish", "repair") else ()
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
BUILT_VERBS = ("setup", "undo", "replenish", "purge", "set-credential", "remove-credential", "repair", "set-limits",
               "routes-base", "routes-trust", "routes-egress", "enrol-authority-signer", "remove-authority-signer")


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
               source: Path | None = None, h11: Path | None = None,
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
    txn = journal.txn if journal is not None and journal.txn else gen_mod.new_txn()
    intent = Intent("setup", cmd.operator, dict(alloc.ids), dict(alloc.shared), txn)
    h11_dir = root_tree.h11_source() if h11 is None else h11
    root_tree.check_sources(package, h11_dir)
    target = gen_mod.generation_id(package, h11_dir)
    versions = (gen_mod.read_version_text(package / "__init__.py"), root_tree.h11_floor(h11_dir))
    restricted_aa = host == "linux" and restricted
    desired = desired_managed(host, accounts=alloc.ids, shared=alloc.shared, operator=cmd.operator,
                              services=[s.name for s in services], slots=alloc.slots, apparmor=restricted_aa)
    noted: dict[str, int] = {}
    shape_path = install_shape_path(host)
    # U14 (HEAD 1022-12), option (b): what `current` names, so a re-run whose target is already current drops its
    # partial copy instead of replacing the generation live services import from. A RESUMED setup that changes
    # generation may have started services on the old code, whose unit text need not change: setup starts services
    # only after its flip, so that is when `current` names another generation, or names none while the legacy tree
    # is there (a levain-intent/1 setup ran from it). Then every service this code knows is stopped before the flip
    # (U18's stop: one with no unit is already stopped), and the start steps bring them up on the new generation.
    tree = root_tree.current_path(host).parent
    current = root_tree.read_current(tree)
    ran_old = current != target if current is not None else os.path.isdir(tree / "levain")
    restart = _stop_steps(host, units.services_for(host), {}, time.time) if journal is not None and ran_old else []

    def note() -> tuple[bool, str]:
        now = int(time.time())
        noted.update((s.name, now) for s in services)
        return True, ""

    def record() -> tuple[bool, str]:
        new = gen_mod.InstallShape(target, versions[0], versions[1], txn, dict(noted), _shape_of(desired))
        return gen_mod.write_record_durably(shape_path, gen_mod.shape_text(new))

    vendor_failed: list[str] = []
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
        *base_routes_merge_steps(host),
        *roots_steps(host, roots),
        *pool_config_steps(host, cmd.slots, cmd.verifier_slots),
        *restart,
        *root_tree.tree_steps(host, package, h11_dir=h11_dir, generation=target, current=current),
        # The vendor CLIs a session execs, COPIED into the prefix tree_steps made (head ruling 1026+8). A failed copy
        # does not stop setup here: the report step after the units prints it as skipped (ruling V23).
        *root_tree.vendor_steps(host, cmd.operator, failed=vendor_failed),
        *confinement,
        # Before the first start: each service's started_after threshold (row 33 §2.5).
        Step("note each service's started_after threshold (before the first start)", call=note),
        *units.install_steps(host, services),
        # The quiesce, if the pool lock was not there to take at the live-lease check (a fresh setup).
        quiesce_step(host),
        # Each slot's §3.5 Pool state once everything the slot needs is in place, before the rule that lets
        # prepare run (prepare picks only from these).
        *slot_state_steps(host, alloc.slots),
        # Setup's report is allow_fail (ruling V23): a copy that fails on every run is printed as skipped and never
        # keeps the sudoers rules, the install record or the operator record from being written.
        root_tree.vendor_report_step(vendor_failed, allow_fail=True),
        *sudoers_steps(host, cmd.operator),
        # Row 33 §2.5: the install record before the operator record; setup's completion is still the latter.
        Step(f"write the install record {shape_path} (completed_txn {txn})", call=record),
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
                       intent=intent, generation=target, generation_change=True)


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
    # An UNDO journal with no operator record is an interrupted undo of such a setup (undo removes its journal before
    # the record, so an ordinary undo's journal never outlives the record).
    if facts.operator_record is not None or j is None or j.verb not in ("setup", "undo"):
        return None
    if facts.live_leases > 0:
        raise Refused("live-lease", _live_lease_why("undo", facts))
    if facts.caller_uid == 0 or facts.caller_name != j.operator:
        raise Refused("operator-mismatch", f"the interrupted setup was for {j.operator!r}")
    return j


def _end_processes(host: HostOS, uid: int, name: str, *,
                   end: Callable[[int], "PR.Domain"] = lambda u: PR.end_domain(u, PR.V12K_SECONDS)[0],
                   kill_all: Callable[[int], tuple[bool, str]] = _kill_all,
                   none_left: Callable[[int], bool] = lambda u: PR.UidProcs().no_process(u)) -> tuple[bool, str]:
    """Every process of the uid ended and proven before its account is removed, refused by the account's name.
    macOS: :func:`proc.end_domain` (freeze, kill, the launchd domain booted out, kill again, a quiet window, the
    proof), so no agent is respawned for the uid after the kill (an undo once left a slot's distnoted running past
    rc 0: levain-memory runs/s2-macos/raw_d/998_undo.txt); a bootout launchctl did not accept refuses
    ``domain-remains:<name>``, any other unproven step ``processes-remain:<name>``. Linux: every process killed,
    then the proof (two scans, no process of the uid)."""
    if host == "darwin":
        res = end(uid)
        if res.proven:
            return True, ""
        what = "domain-remains" if res.name.startswith("domain-not-accepted") else "processes-remain"
        return False, f"{what}:{name}: uid {uid}: {res.name} {res.detail}".rstrip()
    ok, why = kill_all(uid)
    if not ok:
        return False, why
    if not none_left(uid):
        return False, f"processes-remain:{name}: a process of uid {uid} is running after the kill"
    return True, ""


def _undo_journal_step(host: HostOS, intent: Intent, live: RM.Live) -> Step:
    """Undo's own journal (FIX1 M2), written whole and durably under :attr:`removal.Live.root` (``/`` on a host)."""
    path, text = live.at(INTENT_PATH[host]), intent_text(intent)
    return Step(f"journal what this undo will remove in {INTENT_PATH[host]} (txn {intent.txn})",
                call=lambda: write_intent_atomically(path, text))


def _check_journal_reads_back(host: HostOS, intent: Intent) -> None:
    """L3 r2 codex MED: undo's journal unions three sets, each within :data:`generation.MAX_MANAGED`, so the union can
    exceed what :func:`read_intent` accepts. It is parsed back by that reader's own rules (its byte cap, then
    :func:`parse_intent`) before step one; one that does not read back as itself refuses ``undo-journal-unreadable``
    with nothing removed, never written to strand the re-run."""
    data = intent_text(intent).encode()
    try:
        if len(data) > gen_mod.MAX_RECORD_BYTES:
            raise Refused("record-too-large", f"{len(data)} bytes, over {gen_mod.MAX_RECORD_BYTES}")
        same = parse_intent(data, host) == intent
    except Refused as e:
        raise Refused("undo-journal-unreadable", f"undo's journal of {len(intent.managed)} records would not read "
                                                 f"back ({e.name}: {e.detail}); undo has removed nothing") from None
    if not same:
        raise Refused("undo-journal-unreadable", "undo's journal does not read back as written; undo has removed "
                                                 "nothing")


def _uid_is_undos(facts: HostFacts, accounts: Mapping[str, int], name: str, uid: int) -> bool:
    """Whether undo retires a recorded account's ``uid``: :func:`accounts.uid_retires`, the rule repair's account step
    also runs (r1b-fix3), over this host's users with Levain's own (``accounts``: live or journal-present; our
    tombstones)."""
    return uid_retires(name, uid, users=facts.user_ids, levain={**facts.levain_tombstones, **accounts})


def _undo_unit_order(host: HostOS, names: Iterable[str]) -> list[str]:
    """Undo stops units in reverse start order: a recorded name this code does not ship first (sorted), then the
    shipped ones in reverse of :data:`units.SERVICES`."""
    shipped = [s.name for s in reversed(units.services_for(host))]
    names = set(names)
    return sorted(names - set(shipped)) + [n for n in shipped if n in names]


def plan_undo(cmd: InstallCommand, facts: HostFacts, *, accounts: Mapping[str, int],
              shared_groups: Mapping[str, int], live: RM.Live | None = None,
              end: Callable[[int, str], tuple[bool, str]] | None = None) -> InstallPlan:
    """``accounts``: every live Levain account the directory service holds, name -> uid (== gid);
    ``shared_groups``: the shared groups present, name -> their live gid.

    Row 33 R1b: undo removes ``record.managed ∪ journal.managed`` plus its own desired set (what this code would manage
    for those accounts and groups, so a host with no record undoes as before; a FINISHED repair journal's records are
    the record's, and undo reads none of them), each record through its kind's per-identity remove step with
    ``retire=False`` (§1.1 g: undo retires no gid). FIX1: first ``visudo -c`` (a config that does not parse refuses
    undo with nothing removed), then undo's OWN journal of that whole set (a re-run plans from it, so what this run
    derived from accounts it then tombstones is not lost). Then, in the order of CONTRACT §23: the
    runtime sudoers rule first (and every other recorded drop-in but the install rule: no new runtime-helper calls),
    every unit stopped (reverse start order) before any unit file is removed, slot state and pool config, the account
    ids retired, each account's processes ended and proven then the account tombstoned, every dir naming a shared
    group given back to root's group, the memberships, the groups deleted, slot confinement (macOS the lo0 aliases,
    the pf anchor and pf's enable reference; Linux the AppArmor profile, the bwrap copy, the netns, then tmpfiles.d
    and R), the install record, ``visudo -c``, the install sudoers rule, the intent journal, the operator record, and
    the ``visudo -c`` after the install rule as the very last step (it can fail without stranding undo). Every remove
    step reads its live object before it acts (ABSENT: nothing run; NOT_OURS: left in place), and the other steps
    are idempotent, so every step is safe to re-run. The store and
    the root import tree stay: ``entry/1`` records are not undo's. ``live``: every live read and act of the remove
    steps; ``end``: the account kind's proven process end.

    With a journal (§25), the accounts and groups it names that are present at their journaled ids are undone with
    the rest. With no operator record and a SETUP journal (the setup died before its record), or the UNDO journal an
    interrupted undo of that setup wrote, undo runs against the journal alone, for its operator, and removes the
    journal last of its removals: there is no record to remove."""
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
        accounts, shared_groups = {}, {}
    check_records(facts, operator)
    host = facts.host
    check_account_ids(host, accounts)
    accounts = dict(accounts)
    groups = {g: i for g, i in shared_groups.items() if g in SHARED_GROUPS}
    if facts.intent is not None:
        # §25: whatever an interrupted run made at its journaled ids goes with the rest, a Linux shared
        # group with no label included. A journaled name present at another id is not ours: left alone.
        ours, _ = _journal_presence(facts)
        for name, i in ours.items():
            if name in facts.intent.accounts:
                accounts.setdefault(name, i)
            elif name in SHARED_GROUPS:
                groups.setdefault(name, i)
        check_account_ids(host, accounts)
    desired = desired_managed(host, accounts=accounts, shared=groups, operator=operator,
                              services=[s.name for s in units.services_for(host)], slots=slots_of(accounts),
                              apparmor=host == "linux", partial_groups=True)
    shape, journal = facts.install_shape, facts.intent
    # A FINISHED repair journal (its txn is the record's completed_txn) is settled in the record: undo reads none of its
    # records, as check_records checks none (an unknown kind there is kept, never read).
    if journal is not None and shape is not None and journal.txn == shape.completed_txn:
        journal = None
    every = (set(shape.managed) if shape is not None else set()) | (set(journal.managed) if journal else set()) | desired
    every = {r for r in every if r[0] != "entry/1"}               # the root import tree stays (§23)
    # M2: what this run derived goes into ITS journal before the first removal, and a re-run plans from it (with the
    # rest): a pre-row-33 install's slot confinement is derived from live accounts, which this run tombstones.
    # Only records within their bound are journaled, so the re-run's check_records cannot refuse its own journal.
    txn = facts.intent.txn if facts.intent is not None and facts.intent.verb == "undo" and facts.intent.txn \
        else gen_mod.new_txn()
    undo_intent = Intent("undo", operator,
                         {n: i for n, i in accounts.items() if in_own_range(host, n, i)},
                         {g: i for g, i in groups.items() if i in RANGES[host].shared}, txn, None,
                         _shape_of(r for r in every if _in_bound(host, r, operator)))
    _check_journal_reads_back(host, undo_intent)
    live = live if live is not None else RM.Live()
    end = end or (lambda uid, name: _end_processes(host, uid, name))

    def of(kind: str) -> list:
        return sorted((r for r in every if r[0] == kind), key=repr)

    def remove(records: Iterable, phase: str) -> list[Step]:
        return [s for k, i in records
                for s in _kind_steps(host, k, i, phase, live, retire=False, held=False, end=end)]

    install_rule = ("sudoers/1", INSTALL_SUDOERS.name)
    grants = sorted((r for r in of("sudoers/1") if r != install_rule), key=lambda r: (r[1] != RUNTIME_SUDOERS.name, r[1]))
    unit_recs = [("unit/1", n) for n in _undo_unit_order(host, (i for _, i in of("unit/1")))]
    uids = {i[0]: i[1] for _, i in of("account/1")}
    retired_after = frozenset(facts.retired) | frozenset(u for n, u in uids.items()
                                                         if _uid_is_undos(facts, accounts, n, u))
    path = RETIRED_IDS_PATH[host]
    mkdir = "/bin/mkdir" if host == "darwin" else _abs("mkdir")
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    steps: list[Step] = [
        # M1: a config that does not parse refuses undo here, with nothing removed (every drop-in's own check after its
        # removal would otherwise stop undo part-way).
        config_check_step(host, "check the sudoers config with visudo -c before undo removes anything", live=live,
                          refusal="the sudoers config does not parse: undo has removed nothing; fix what visudo "
                                  "names, then run undo again", remedy=True),
        _undo_journal_step(host, undo_intent, live),
        *remove(grants, "R5"),                      # the runtime rule first: no new runtime-helper calls
        *remove(unit_recs, "R5"),                   # every unit stopped before anything is removed
        *remove(unit_recs, "R12"),                  # then their files
        # With the services stopped nothing rewrites them: no slot reads as reusable from here on. The store under
        # Live.root, as every other act here (the store itself on a host).
        *slot_state_undo_steps(host, slots_of(uids), store=live.at(STORE[host])),
        *pool_config_undo_steps(host),
        *policy_limits_undo_steps(host, live),
        *authority_signers_undo_steps(host, live),
        Step(f"create {path.parent}", (mkdir, "-p", "-m", "755", str(path.parent))),
        Step("retire every account id so none is reissued", write=(path, retired_ids_text(retired_after), 0o644)),
        *remove(of("account/1"), "R7"),             # per account: its processes ended and proven, then the tombstone
        *ungroup_steps(host, operator),
        *remove(of("member/1"), "R7"),
        *remove(of("group/1"), "R7"),               # retire=False: undo never retires a gid (§1.1 g)
    ]
    # Slot confinement goes only after every slot process is stopped and its account tombstoned; the lo0 aliases
    # before the pf rules guarding them are flushed (§24), and the anchor before pf's enable reference.
    steps += removal_steps(host, [r for r in every if REMOVE_PHASES[r[0]] == ("R8",)], "R8", live=live, retire=False,
                           end=end)
    if host == "linux":
        steps += tmpfiles_undo_steps() + run_dir_undo_steps(host)
    # Row 33 §1.1: the install record goes before the last three acts (the install rule, the journal, the record).
    steps.append(Step("remove the install record", (rm, "-f", str(install_shape_path(host)))))
    # The install rule LAST: until it goes, a failed undo re-runs. M1: so nothing may fail between its removal and the
    # end. The config is checked while the rule still stands (a failure there leaves undo re-runnable through the
    # helper), and the check after its removal runs as the plan's LAST step, after the journal and the operator record
    # are gone: a failure there reports a config broken during undo and strands nothing.
    rm_rule, after_rule = remove([install_rule], "R5")
    steps.append(config_check_step(host, f"check the sudoers config with visudo -c before {INSTALL_SUDOERS} goes",
                                   live=live, refusal=f"the sudoers config does not parse: {INSTALL_SUDOERS} is "
                                                      "still in place, so undo can run again once visudo checks "
                                                      "clean"))
    steps.append(rm_rule)
    # Just before the record (§25): until undo is complete a re-run still reads what the journal names.
    steps.append(intent_remove_step(host))
    if abandoned is None:
        steps.append(Step("remove the operator record (last)", (rm, "-f", str(operator_record_path(host)))))
    steps.append(after_rule)
    check_tools(facts, steps, verb="undo")
    return InstallPlan("undo", host, tuple(steps), retired_after=retired_after, intent=undo_intent)


# --- replenish -------------------------------------------------------------------------------------


def _keys_start_steps(host: HostOS, *, restart: bool) -> list[Step]:
    return units.keys_start_steps(host, restart=restart)


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
    check_records(facts, str(rec.get("name")))
    accounts = dict(accounts or {})
    check_account_ids(host, accounts)
    check_capacity(host, slots, verifier_slots, facts.retired)
    journal = facts.intent
    if journal is not None and journal.verb == "undo":
        raise Refused("install-interrupted", journal.verb)      # undo finishes it: run undo again
    if journal is not None and journal.verb == "repair":
        # Row 33 (§8 N1/N1a): the ONLY proof a repair finished is install-shape's completed_txn == its txn.
        shape = facts.install_shape
        if shape is None or journal.txn != shape.completed_txn:
            # U7 (HEAD 1022-12): setup's shape, the interrupted verb as the detail, so every verb renders it alike.
            raise Refused("install-interrupted", journal.verb)
        journal = None    # finished: this run's journal write replaces it
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
    # R-i: a txn is resumed only from this verb's own journal (a stale setup journal's is setup's, never ours).
    txn = journal.txn if journal is not None and journal.verb == "replenish" and journal.txn else gen_mod.new_txn()
    intent = Intent("replenish", str(rec.get("name")), dict(alloc.ids), {}, txn)
    steps = [
        # FIRST (§25): the slots this run makes, so a run that dies part-way resumes them, never spends more.
        intent_write_step(host, intent),
        *account_steps(host, made),
        *membership_steps(host, membership_pairs(made, None)),
        *row_steps(host, rows),
        *confinement,
        # Only now may prepare take them: a new slot is reusable once its confinement is loaded (§3.5 Pool).
        *slot_state_steps(host, alloc.slots),
        *_extend_shape_steps(host, facts.install_shape, made, alloc.slots),
        # After the pool refill, so a keys failure never stops it. A new gateway account is a restart: keys reads its
        # gateway uids once at startup. With none, a start (a keys unit left failed by exit 78 comes up).
        *_keys_start_steps(host, restart=any(a.name.startswith("_levain_gw") for a in made)),
        intent_remove_step(host),   # LAST, after the confinement steps
    ]
    check_tools(facts, steps, verb="replenish")
    return InstallPlan("replenish", host, tuple(steps), alloc, made, (), tuple(rows), (), facts.retired,
                       intent=intent)


# --- repair (row 33; DESIGN 1022-8/upgrade §2.3) ----------------------------------------------------------------


@dataclass(frozen=True)
class RepairFacts:
    """What a repair reads beyond :class:`HostFacts`, under the install lock (§2.3 R0)."""

    #: Every live Levain account (Levain's live label), name -> uid (== gid).
    accounts: Mapping[str, int] = field(default_factory=dict)
    #: Every group whose name passes ``group/1``'s bound (``levain-*``), name -> gid: the candidates §3 recognises.
    groups: Mapping[str, int] = field(default_factory=dict)
    #: For each of those groups, its members as the directory lists them (§3: read_directory gains membership).
    members: Mapping[str, frozenset] = field(default_factory=dict)
    #: The slots in service, by class (``L/slots/``, retired ones dropped).
    egress: tuple[int, ...] = ()
    verifier: tuple[int, ...] = ()
    #: ``readlink(<tree>/current)``; None on a pre-generation install (or a lost flip).
    current: str | None = None
    #: A pre-generation install's top-level ``<tree>/levain`` is present (R13 removes it).
    legacy: bool = False
    #: ``--source``: the RESOLVED levain package dir and its sibling ``h11`` (§2.1 (e)); None without ``--source``.
    source: Path | None = None
    h11: Path | None = None
    #: The earlier ``L/policy/install-skips`` entries (:func:`parse_install_skips`; R14 re-reads each live).
    skips: tuple = ()
    #: L3 r1: each live account's same-named group, name -> gid, present only at the directory's gid for that name
    #: (macOS: under Levain's label). The plan requires it at the account's uid (§1.1: gid == uid).
    account_groups: Mapping[str, int] = field(default_factory=dict)


def install_shape_path(host: HostOS) -> Path:
    return operator_record_path(host).parent / "install-shape"


def desired_managed(host: HostOS, *, accounts: Mapping[str, int], shared: Mapping[str, int], operator: str,
                    services: Iterable[str], slots: Iterable[SlotId], apparmor: bool = False,
                    partial_groups: bool = False) -> frozenset:
    """§1.1 ``managed``: what an install with these accounts, shared groups, services and slots manages, typed, one
    record per resource, derived from the plan (never typed by hand). ``partial_groups``: undo's alone (a re-run after
    its groups went has no gid to name for them); every other caller passes every group and a missing one raises."""
    accts = accounts_of(Allocation(host, (), (), dict(accounts), {}))
    out: set = {("unit/1", s) for s in services}
    out |= {("sudoers/1", p.name) for p in sudoers_paths()}
    out |= {("group/1", (g, gid)) for g, gid in shared.items()}
    out |= _slot_managed(host, accts, shared, operator, slots, partial_groups=partial_groups)
    out |= {("entry/1", e) for e in root_tree.ENTRY_MODULES}
    if host == "darwin":
        out |= {("pf-anchor/1", (pf.ANCHOR, str(pf.ANCHOR_FILE))), ("pf-enable/1", str(pf.PF_TOKEN))}
    else:
        out |= {("bwrap-copy/1", (str(bwrap.LEVAIN_BWRAP), str(bwrap.BWRAP_DIGEST)))}
        if apparmor:
            out.add(("apparmor/1", bwrap.APPARMOR_PROFILE_NAME))
    return frozenset(out)


def _slot_managed(host: HostOS, accts: Iterable[Account], shared: Mapping[str, int], operator: str | None,
                  slots: Iterable[SlotId], *, partial_groups: bool = False) -> set:
    """The records that come with accounts and slots: each account, each of its memberships (and the operator's, when
    named) at the group's gid, and per slot its lo0 alias (macOS egress) or its netns (Linux). ONE derivation for
    setup's and repair's desired set and for what replenish adds (U16)."""
    accts = tuple(accts)
    out: set = {("account/1", (a.name, a.uid)) for a in accts}
    # partial_groups (undo only): a group with no gid here has none to name (a re-run after the groups went); its
    # pairs went with it. Every other caller passes every group, and a missing one raises KeyError.
    out |= {("member/1", (a, g, shared[g])) for a, g in membership_pairs(accts, operator)
            if not partial_groups or g in shared}
    if host == "darwin":
        out |= {("pf-alias/1", loopback_alias(s)) for s in slots if s.slot_class == "egress"}
    else:
        out |= {("netns/1", s.netns) for s in slots}
    return out


def _extend_shape_steps(host: HostOS, shape: gen_mod.InstallShape | None, made: Sequence[Account],
                        slots: Sequence[SlotId]) -> list[Step]:
    """U16 (HEAD 1022-12): replenish's records added to ``install-shape.managed``, durably, before its journal goes;
    the generation, versions, ``completed_txn`` and ``started_after`` kept, nothing dropped (a union, so a resumed
    replenish writes the same record). No record (a pre-row-33 install): nothing to extend; repair writes it."""
    if shape is None:
        return []
    gids = {i[0]: i[1] for k, i in shape.managed if k == "group/1"}
    lacking = sorted({g for _, g in membership_pairs(made, None)} - set(gids))
    if lacking:
        raise Refused("install-shape-malformed", f"it records no group/1 for {', '.join(lacking)}")
    grown = replace(shape, managed=_shape_of(set(shape.managed) | _slot_managed(host, made, gids, None, slots)))
    path, text = install_shape_path(host), gen_mod.shape_text(grown)
    return [Step(f"extend the install record {path} with what this replenish made (its generation, versions and "
                 "completed_txn kept)", call=lambda: gen_mod.write_record_durably(path, text))]


def check_records(facts: HostFacts, operator: str | None, *, journal_kinds: bool = True) -> None:
    """U15 (HEAD 1022-12), the one preflight every mutating verb (repair, replenish, undo) runs before step one: every
    record ``install-shape`` holds, and every write-ahead record of an UNFINISHED journal (a finished one's txn is the
    record's ``completed_txn``: what it named is settled in the record), is of a kind this code can remove and within
    its bound, else ``downgrade-unsupported:<kind>`` / ``bad-record`` (§1.1 rules c, e). The operator then runs the
    newer code's verb. A record that does not PARSE never reaches here: undo reads it as absent, the rest refuse it.
    ``journal_kinds=False``: repair's phase A [r5: codex HIGH 2], which hands the journal to the STAGED code: its
    records are checked only as parsed (``<kind>/<v>`` tokens, by :func:`parse_intent`), never against this code's
    table; the probe's ``kinds`` and phase B's plan judge them."""
    shape, journal = facts.install_shape, facts.intent
    recs = sorted(shape.managed, key=repr) if shape is not None else []
    if journal_kinds and journal is not None and not (shape is not None and journal.txn == shape.completed_txn):
        recs += sorted(journal.managed, key=repr)
    gen_mod.check_managed(facts.host, recs, operator=operator)


def _in_bound(host: HostOS, record, operator: str | None) -> bool:
    """One record within its kind's bound (:func:`generation.check_managed`'s test, record by record)."""
    kind, ident = record
    return kind in gen_mod.KINDS and not isinstance(ident, gen_mod.Opaque) and gen_mod.KINDS[kind](host, ident, operator)


def _shape_of(records: Iterable) -> tuple:
    return tuple(sorted(records, key=lambda r: (r[0], canon.canonical(gen_mod.managed_json([r])))))


# --- the remove-step table (row 33 R1b; DESIGN §1.1, the per-identity steps in their phases) -----------------------

#: Each kind's phases: its revoke and destroy R-steps (§1.1 table, "revoke @" / "destroy @"). Row order is the
#: table's: within one R-step, kinds go in this order.
REMOVE_PHASES: Mapping[str, tuple[str, ...]] = {
    "unit/1": ("R5", "R12"), "sudoers/1": ("R5",), "member/1": ("R7",), "group/1": ("R7",), "account/1": ("R7",),
    "pf-alias/1": ("R8",), "pf-anchor/1": ("R8",), "pf-enable/1": ("R8",), "apparmor/1": ("R8",),
    "bwrap-copy/1": ("R8",), "netns/1": ("R8",), "entry/1": ("R10",),
}
#: §1.1 rule b2: every kind ever shipped keeps its remove step forever (a test pins this set).
SHIPPED_KINDS = frozenset(REMOVE_PHASES)
if SHIPPED_KINDS != frozenset(gen_mod.KINDS):   # a bound with no remove step, or a remove step with no bound
    raise ImportError(f"remove-step table and kind bounds differ: {sorted(SHIPPED_KINDS ^ frozenset(gen_mod.KINDS))}")


def _kind_steps(host: HostOS, kind: str, ident, phase: str, live: RM.Live, *, retire: bool, held: bool,
                end: Callable[[int, str], tuple[bool, str]]) -> list[Step]:
    """One record's steps for ``phase``: its kind's per-identity builder, given the RECORDED identity only."""
    if kind == "unit/1":
        return (units.stop_unit_by_name(host, ident, live=live) if phase == "R5"
                else units.remove_unit_file_by_name(host, ident, live=live))
    if kind == "sudoers/1":
        return remove_dropin_steps(host, ident, live=live)
    if kind == "member/1":
        return remove_member_steps(host, *ident, live=live)
    if kind == "group/1":
        return remove_group_steps(host, *ident, live=live, retire=retire, record_held=held)
    if kind == "account/1":
        return remove_account_steps(host, *ident, live=live, end=end, retire=retire)
    if kind == "pf-alias/1":
        return pf.remove_alias_steps(ident, live=live)
    if kind == "pf-anchor/1":
        return pf.remove_anchor_steps(*ident, live=live)
    if kind == "pf-enable/1":
        return pf.remove_enable_steps(ident, live=live)
    if kind == "apparmor/1":
        return bwrap.remove_apparmor_profile_steps(ident, live=live)
    if kind == "bwrap-copy/1":
        return bwrap.remove_bwrap_copy_steps(*ident, live=live)
    if kind == "netns/1":
        return netns.remove_netns_steps(ident, live=live)
    if kind == "entry/1":
        return remove_entry_steps(host, ident, live=live)
    raise Refused(f"downgrade-unsupported:{kind}", "this code holds no remove step for that kind")


def kind_state(host: HostOS, kind: str, ident, live: RM.Live) -> str:
    """The live outcome of one record (ABSENT / OURS / NOT_OURS); a failed read raises :class:`removal.ReadFailed`."""
    if kind == "unit/1":
        return units.unit_state(host, ident, live)
    if kind == "sudoers/1":
        return dropin_state(ident, live)
    if kind == "member/1":
        return member_state(host, *ident, live)
    if kind == "group/1":
        return group_state(host, *ident, live)
    if kind == "account/1":
        return account_state(host, *ident, live)
    if kind == "pf-alias/1":
        return pf.alias_state(ident, live)
    if kind == "pf-anchor/1":
        return pf.anchor_state(*ident, live)
    if kind == "pf-enable/1":
        return pf.enable_state(ident, live)
    if kind == "apparmor/1":
        return bwrap.apparmor_state(ident, live)
    if kind == "bwrap-copy/1":
        return bwrap.bwrap_copy_state(*ident, live)
    if kind == "netns/1":
        return netns.netns_state(ident, live)
    if kind == "entry/1":
        return entry_state(host, ident, live)
    raise Refused(f"downgrade-unsupported:{kind}", "this code holds no remove step for that kind")


def removal_steps(host: HostOS, records: Iterable, phase: str, *, live: RM.Live, held: frozenset = frozenset(),
                  retire: bool = True, end: Callable[[int, str], tuple[bool, str]] | None = None) -> list[Step]:
    """Every record in ``records`` whose kind has a phase at ``phase``, in the table's row order (records of one kind
    in a fixed order), each through its kind's per-identity step. ``held``: the records ``install-shape`` holds (the
    gid rule retires an ABSENT group only when the record held it). ``retire=False``: undo (§1.1 g)."""
    end = end or (lambda uid, name: _end_processes(host, uid, name))
    recs = list(records)
    steps: list[Step] = []
    for kind, phases in REMOVE_PHASES.items():
        if phase not in phases:
            continue
        for k, ident in sorted((r for r in recs if r[0] == kind), key=repr):
            steps += _kind_steps(host, k, ident, phase, live, retire=retire, held=(k, ident) in held, end=end)
    return steps


SKIPS_FORMAT = "levain-install-skips/1"


def install_skips_path(host: HostOS) -> Path:
    return operator_record_path(host).parent / "install-skips"


def parse_install_skips(raw: bytes | None) -> tuple:
    """The earlier ``install-skips`` entries, from the bytes R0 read through the store under the install lock (None:
    no file, so none). Bytes that are not a ``levain-install-skips/1`` record holding a well-formed managed list
    refuse ``install-skips-malformed`` (HEAD ruling R1b-6): read as empty, its NOT_OURS entries would be lost, and
    doctor would go green over a foreign object left in place. The per-kind bounds are :func:`check_install_skips`',
    at plan time."""
    if raw is None:
        return ()
    bad = "install-skips-malformed"
    try:
        obj = canon.parse(raw)
    except canon.ContractError as e:
        raise Refused(bad, str(e)) from None
    if not isinstance(obj, dict) or set(obj) != {"format", "skips"} or obj["format"] != SKIPS_FORMAT:
        raise Refused(bad, f"not a {SKIPS_FORMAT} record")
    return gen_mod.parse_managed(obj["skips"], what="install-skips")


def check_install_skips(host: HostOS, skips: Iterable, *, operator: str) -> None:
    """Every earlier entry of a kind this code holds passes that kind's bound (§1.1), else ``install-skips-malformed``
    before any act: R14 re-reads each entry through its kind's live read, which takes only a bounded identity. An
    unknown kind's entry is kept as it is and never read (it names nothing this code can inspect)."""
    for kind, ident in skips:
        if kind in gen_mod.KINDS and (isinstance(ident, gen_mod.Opaque) or not gen_mod.KINDS[kind](host, ident, operator)):
            raise Refused("install-skips-malformed", f"{kind} {canon.canonical(gen_mod._thaw(ident))} breaks its bound")


def _skips_now(host: HostOS, live: RM.Live, earlier: Iterable) -> list:
    """§2.3 R14: this run's NOT_OURS records plus every earlier entry (read at R0, bound-checked at plan time) whose
    object is still present (re-read live; a read that fails keeps it). An entry the operator removed by hand drops
    out."""
    keep = set(live.skips)
    for kind, ident in earlier:
        try:
            if kind not in SHIPPED_KINDS or kind_state(host, kind, ident, live) != RM.ABSENT:
                keep.add((kind, ident))
        except RM.ReadFailed:
            keep.add((kind, ident))
    return gen_mod.managed_json(keep)


def write_install_skips(host: HostOS, live: RM.Live, earlier: Iterable = ()) -> tuple[bool, str]:
    """``L/policy/install-skips`` (root 0644), durably, BEFORE ``install-shape`` (DESIGN X4-M2), so the shape's
    write stays the one completion commit. ``earlier``: :func:`parse_install_skips`' entries (``RepairFacts.skips``)."""
    path = install_skips_path(host)
    text = canon.canonical({"format": SKIPS_FORMAT, "skips": _skips_now(host, live, earlier)})
    return gen_mod.write_record_durably(live.at(path), text)


# --- the fault seam (§5 "The crash seam" [r2: X-10, C-F9], [r3: F7], [r4: X3-H1]) -------------------------------------

#: [r6: L3 s3 r1 codex LOW] A fault's step id, by MEMBERSHIP: the R-steps of §2.3 a plan places points around
#: (:func:`_faulted`'s ``before``/``after``; R4 is the exec, which has none).
FAULT_STEPS = frozenset({"R1", "R2", "R3", "R4b", *(f"R{n}" for n in range(5, 16))})
#: Every point a fault may name, by MEMBERSHIP (never a pattern): an R-step's ``before`` and ``after``, R9's ``a``
#: (after the ``current.new`` symlink) and ``b`` (after the rename), R2's ``mid-copy`` (between the levain and the h11
#: copy), and every remove step's act boundary (:data:`removal.REMOVE_POINTS`).
FAULT_POINTS = frozenset({"before", "after", "a", "b", "mid-copy"}) | RM.REMOVE_POINTS
#: ``(step id, point) -> None``: called at each named point of a plan built with one (a launcher run only).
FaultHook = Callable[[str, str], None]


def _reachable(step: str, point: str) -> bool:
    """[r6: L3 s3 r1 codex LOW] ``(step, point)`` is a pair a plan places: ``before``/``after`` on any of
    :data:`FAULT_STEPS`, ``a``/``b`` on R9 (the flip), ``mid-copy`` on R2, a remove point on its kind's phases
    (:data:`REMOVE_PHASES`)."""
    if step not in FAULT_STEPS or point not in FAULT_POINTS:
        return False
    if point in ("a", "b"):
        return step == "R9"
    if point == "mid-copy":
        return step == "R2"
    if point in RM.REMOVE_POINTS:
        return step in REMOVE_PHASES[point.split(":", 1)[0]]
    return True


def check_fault(fault: object) -> Optional[Tuple[str, str]]:
    """A launcher's fault, or None: a ``(step id, point)`` pair of strings that a plan places (:func:`_reachable`),
    so a harness run cannot finish without the crash it asked for. Else ``bad-token``, before any act. Phase A embeds
    it in phase B's program as a literal only after this check, so nothing but those two shapes of text reaches it."""
    if fault is None:
        return None
    if not (isinstance(fault, tuple) and len(fault) == 2 and all(type(x) is str for x in fault)
            and _reachable(fault[0], fault[1])):
        raise Refused("bad-token", f"fault: {fault!r} (a step in FAULT_STEPS and a point its plan places)")
    return fault[0], fault[1]


def _kill_self() -> None:
    """The launcher's default fault: this process killed where it stands (SIGKILL: no handler, no cleanup)."""
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def fault_hook(fault: Optional[Tuple[str, str]], act: Optional[Callable[[], None]] = None) -> Optional[FaultHook]:
    """The hook a plan calls at its points: ``act`` (default :func:`_kill_self`) at ``fault`` and nothing anywhere
    else; None (no point steps planned at all) without a fault."""
    if fault is None:
        return None
    at = check_fault(fault)
    return lambda step, point: (act or _kill_self)() if (step, point) == at else None


def _faulted(fault: Optional[FaultHook], step: str, steps: Iterable[Step]) -> list[Step]:
    """R-step ``step``'s ``steps`` between its ``before`` and ``after`` points (a call step each, planned only with a
    ``fault``, so a run without one plans exactly what it did before the seam)."""
    if fault is None:
        return list(steps)

    def point(where: str) -> Step:
        return Step(f"fault point {step} {where}", call=lambda: (fault(step, where), (True, ""))[1])
    return [point("before"), *steps, point("after")]


def _journal_step(host: HostOS, why: str, intent: Intent) -> Step:
    path, text = INTENT_PATH[host], intent_text(intent)
    return Step(why, call=lambda: write_intent_atomically(path, text))


def _stop_steps(host: HostOS, services: Sequence[units.Service], noted: dict[str, int],
                clock: Callable[[], float], *, live: RM.Live | None = None) -> list[Step]:
    """§2.3 R5: stop ``services`` in reverse start order, noting ``floor(now)`` for each just BEFORE its stop (R14
    writes it to ``started_after``). The stop body is :func:`units.stop_unit_act`, the ONE body a removed unit's
    revoke runs too (R1b). U18: the verdict is the END STATE, never the stop's exit status, and only a service that
    runs is stopped: macOS boots out only a job launchd holds, then checks it is gone (a ``launchctl print`` exit that
    is neither 0 nor launchd's not-found fails the step, DESIGN X4-M1); Linux reads ``ActiveState`` (``inactive`` or
    ``failed`` is stopped), else stops it and reads it again. Never ``LoadState`` alone: after a daemon-reload a unit
    whose file was deleted reads ``not-found`` while its process still runs."""
    lv = live or RM.Live()

    def note_then(name: str) -> Callable[[], tuple[bool, str]]:
        def go() -> tuple[bool, str]:
            noted[name] = int(clock())
            try:
                return units.stop_unit_act(host, name, lv, disable=False)
            except RM.ReadFailed as e:
                return False, str(e)
        return go

    steps: list[Step] = []
    for s in reversed([x for x in units.services_for(host) if x in services]):
        unit = units.launchd_label(s) if host == "darwin" else units.systemd_name(s)
        steps.append(Step(f"stop {unit}", call=note_then(s.name)))
    return steps


def _recognised_groups(host: HostOS, facts: HostFacts, rf: RepairFacts, journaled: Mapping[str, int]) -> dict[str, int]:
    """§3: the shared groups present that are Levain's: macOS by the live label; any host by ``group/1`` in the record
    at its recorded gid, or journaled at its gid; with NO record (the pre-row-33 hosts, Q3), by name AND gid in the
    shared range. A same-name group anywhere else refuses ``account-exists``, never claimed."""
    shape = facts.install_shape
    recorded = {i for k, i in (shape.managed if shape else ()) if k == "group/1"}
    ours: dict[str, int] = {}
    foreign: list[str] = []
    for g in SHARED_GROUPS:
        gid = rf.groups.get(g)
        if gid is None:
            continue
        if ((host == "darwin" and facts.levain_live.get(g) == gid) or (g, gid) in recorded
                or journaled.get(g) == gid or (shape is None and gid in RANGES[host].shared)):
            ours[g] = gid
        else:
            foreign.append(f"{g} (at {gid})")
    if foreign:
        raise Refused("account-exists", ", ".join(foreign))
    return ours


def _repair_journal(cmd: InstallCommand, facts: HostFacts, rf: RepairFacts,
                    txn: Callable[[], str], *, journal_kinds: bool = True) -> tuple[Intent | None, Intent | None, str, str]:
    """The checks every repair plan (phase A's stage and the whole plan) runs first, before any act: another verb's
    journal refused, setup's stale one dropped, a FINISHED repair journal (``txn == record.completed_txn``) set aside
    to be removed (R1 [F2]), the preflight, the records and skips, a foreign account at a journaled name. Returns
    ``(the unfinished repair journal or None, the finished one or None, the operator, the run's txn)``.
    ``journal_kinds``: :func:`check_records`'s (False in phase A only)."""
    if cmd.verb != "repair":
        raise Refused("wrong-verb", cmd.verb)
    shape = facts.install_shape
    journal = facts.intent
    finished: Intent | None = None
    if journal is not None and journal.verb in ("replenish", "undo"):
        raise Refused("install-interrupted", journal.verb)      # U7: setup's shape (plan_setup above)
    if journal is not None and journal.verb == "setup":
        journal = None    # stale: the operator record proves that setup finished (preflight refuses without one)
    if journal is not None and shape is not None and journal.txn == shape.completed_txn:
        finished, journal = journal, None    # R1 [F2]: that run reached R14; never resume it
    preflight(cmd, facts)
    rec = facts.operator_record
    assert rec is not None
    operator = str(rec["name"])
    check_records(facts, operator, journal_kinds=journal_kinds)        # U15: the preflight every mutating verb runs
    check_install_skips(facts.host, rf.skips, operator=operator)        # R1b-6: before any act
    if journal is not None:
        _, foreign = _journal_presence(facts)
        if foreign:
            raise Refused("account-exists", ", ".join(foreign))
    run_txn = journal.txn if journal is not None and journal.txn else txn()
    return journal, finished, operator, run_txn


def _check_rendered_accounts(rf: RepairFacts, slots: Sequence[SlotId]) -> None:
    """R2 entry (1022+20 Linux VM, `userdel _levain_s7`): desired holds only the LIVE accounts, so a recorded one that
    is gone is removed at R7 while R8 still renders its slot's rows for it. §2.4: repair never creates or removes a
    slot, so it can neither re-create the account nor drop the slot. Every account the rendered plan names (the
    singletons, every in-service slot's pair) must be live, else refused before any act."""
    missing = [n for n in (*SINGLETONS, *(n for s in slots for n in s.accounts)) if n not in rf.accounts]
    if missing:
        raise Refused("account-missing", f"{', '.join(missing)}: no live Levain account by that name, and repair "
                                         "never creates one (it never creates or removes a slot). Run undo, then "
                                         "setup")
    # L3 r1 (codex): R8 renders `chown <owner>:<account>` against each account's same-named primary group, so a
    # group deleted (or moved, or relabelled on macOS) by hand while its user stays is the same gap; refused here.
    ungrouped = [n for n in (*SINGLETONS, *(n for s in slots for n in s.accounts))
                 if rf.account_groups.get(n) != rf.accounts[n]]
    if ungrouped:
        raise Refused("group-missing", f"{', '.join(ungrouped)}: no Levain group of that name at the account's id, "
                                       "and repair never creates one (it never creates or removes a slot). Run undo, "
                                       "then setup")


def _repair_slots(rf: RepairFacts) -> tuple[SlotId, ...]:
    return tuple(SlotId("egress", i) for i in rf.egress) + tuple(SlotId("none", i) for i in rf.verifier)


def plan_repair_stage(cmd: InstallCommand, facts: HostFacts, rf: RepairFacts, *,
                      txn: Callable[[], str] = gen_mod.new_txn, fault: Optional[FaultHook] = None) -> InstallPlan:
    """``repair --source PATH``'s PHASE A (§2.2), in the installed generation's code: every check a plan runs before
    any act, then R1 (the journal FIRST, generation unset unless an unfinished journal fixed it), R2 (the copy and its
    checks, then the id hashed over the STAGED copy and the same hash over the source), R3 (the generation placed and
    made durable, THEN ``generation:<id>`` journalled, same ``txn``). The id is the staged copy's, so the plan carries
    it in ``handover`` (:class:`root_tree.Staged`), filled when the hash step runs. :func:`run` then probes the staged
    generation (R3a) and execs into it (R4); everything from R4b on is planned by THAT code (:func:`repair_continue`).
    """
    host = facts.host
    # [r5: codex HIGH 2] the incumbent judges the journal's kinds generically only: the staged code is the judge
    journal, finished, operator, run_txn = _repair_journal(cmd, facts, rf, txn, journal_kinds=False)
    if rf.source is None or rf.h11 is None:
        raise Refused("bad-source", "no h11 beside the levain package" if rf.source is not None else "no --source")
    root_tree.check_sources(rf.source, rf.h11)
    check_account_ids(host, rf.accounts)
    _check_rendered_accounts(rf, _repair_slots(rf))
    pinned = journal.generation if journal is not None else None
    first = Intent("repair", operator, {}, dict(journal.groups) if journal else {}, run_txn, pinned,
                   tuple(journal.managed) if journal else ())
    cell = root_tree.Staged()

    def journal_generation() -> tuple[bool, str]:
        if cell.target is None:
            return False, "no staged id to journal"
        return write_intent_atomically(INTENT_PATH[host], intent_text(replace(first, generation=cell.target)))

    steps: list[Step] = []
    if finished is not None:
        steps.append(Step(f"remove the finished repair journal {INTENT_PATH[host]} (its txn is install-shape's "
                          "completed_txn)", (root_tree._tools(host)["rm"], "-f", str(INTENT_PATH[host]))))
    steps += _faulted(fault, "R1", [intent_write_step(host, first)])                              # R1
    staging = root_tree.stage_hashing_steps(host, rf.source, rf.h11, cell, current=rf.current, pinned=pinned,
                                            fault=(lambda pt: fault("R2", pt)) if fault else None)
    steps += _faulted(fault, "R2", staging[:-1])                                                  # R2: copy, hash
    steps += _faulted(fault, "R3", [staging[-1], Step(f"journal the staged generation id (same txn {run_txn})",
                                                      call=journal_generation)])                  # R3
    check_tools(facts, steps, verb="repair", python=True)
    return InstallPlan("repair", host, tuple(steps), intent=first, handover=cell)


def _resumes_staged(facts: HostFacts, rf: RepairFacts) -> bool:
    """A bare repair meets an UNFINISHED repair journal (its txn is not the record's ``completed_txn``) whose
    journalled generation is not ``current``: a pre-flip repair, which resumes into its own generation (§2.2 r4)."""
    j, shape = facts.intent, facts.install_shape
    return (j is not None and j.verb == "repair" and j.generation is not None
            and not (shape is not None and j.txn == shape.completed_txn) and j.generation != rf.current)


def plan_repair_resume(cmd: InstallCommand, facts: HostFacts, rf: RepairFacts, *,
                       tree: Path | None = None) -> InstallPlan:
    """§2.2 [r4: X3-M1, C3-F5], a bare ``repair`` in phase A (:func:`_resumes_staged`): the journal names
    ``generation:<id>`` (written at R3, after the staged copy was made durable) and ``current`` still names the old
    one, so the code that journalled the run's kinds must finish it. Every check a phase-A plan runs first, then
    ``<tree>/gen-<id>`` re-hashed with the §1.1 id function. Equal: that generation is the target, already placed
    (``Staged.existed``, never ``placed``: a restore keeps it); the plan has no step (R1-R3 ran; there is no source to
    read), and :func:`repair_handover` probes it and execs into it exactly as after a fresh stage. Unequal (power loss
    tore it): one step removes it and, only once that step succeeded, the run refuses ``source-required:<id>``
    (``Staged.then_refuse``; a failed removal is ``step-failed`` naming it); the journal stays as found, so the next
    ``repair --source`` re-stages it. Absent: ``source-required:<id>``, no act."""
    host = facts.host
    journal, _finished, _operator, _txn = _repair_journal(cmd, facts, rf, gen_mod.new_txn, journal_kinds=False)
    assert journal is not None and journal.generation is not None
    check_account_ids(host, rf.accounts)
    _check_rendered_accounts(rf, _repair_slots(rf))
    target = journal.generation
    gen_dir = (tree if tree is not None else root_tree.current_path(host).parent) / target
    if not os.path.isdir(gen_dir) or os.path.islink(gen_dir):
        raise Refused(f"source-required:{target}", f"the interrupted repair is moving to {target}, which is not in "
                                                    "the import tree: repair --source <that levain package>")
    try:
        got = gen_mod.generation_id(gen_dir / "levain", gen_dir / "h11")
    except Refused as e:
        got = f"unreadable ({e.name})"
    if got == target:
        return InstallPlan("repair", host, (), intent=journal, handover=root_tree.Staged(target=target, existed=True))
    cell = root_tree.Staged(then_refuse=Refused(f"source-required:{target}", f"{gen_dir} hashes to {got}, not "
                                                                             f"{target} (torn), and was removed: "
                                                                             "repair --source <that levain package>"))
    steps = [Step(f"remove the torn generation {gen_dir} (it hashes to {got})",
                  (root_tree._tools(host)["rm"], "-rf", str(gen_dir)))]
    check_tools(facts, steps, verb="repair", python=True)
    return InstallPlan("repair", host, tuple(steps), intent=journal, handover=cell)


def plan_repair(cmd: InstallCommand, facts: HostFacts, rf: RepairFacts, *,
                txn: Callable[[], str] = gen_mod.new_txn, clock: Callable[[], float] = time.time,
                read_sysctl: Callable[[str], str] | None = None,
                program_exists: Callable[[Path], bool] = Path.is_file, live: RM.Live | None = None,
                end: Callable[[int, str], tuple[bool, str]] | None = None,
                continuing: str | None = None, fault: Optional[FaultHook] = None) -> InstallPlan:
    """``repair [--source PATH]`` (§2.1, §2.3), the ADDITION path: setup's own plan re-applied against what is
    installed, every step idempotent, the levain + h11 pair staged as ONE generation behind one flip of ``current``.
    Order: the journal (a finished repair journal, ``txn == record.completed_txn``, is removed first and a fresh
    ``txn`` minted; an unfinished one is resumed at its ``txn``, gids and write-ahead ``managed``); the stage (R2-R3);
    the gids and write-ahead ``managed`` journaled (R4b); every affected service STOPPED (R5); new shared groups (R6);
    the missing memberships (R7); rows and confinement (R8); THE FLIP (R9); entries (R10); sudoers (R11); units and
    every start (R12); GC (R13); ``install-shape`` (R14, the completion commit); the journal removed (R15).

    The removal path (R1b): ``remove = (record.managed ∪ journal.managed ∪ desired) − desired``, each record through
    its kind's per-identity step in its phases (:data:`REMOVE_PHASES`: R5 unit stops and sudoers, R7 members, groups
    and accounts, R8 the confinement kinds BEFORE the re-render, R10 entries, R12 unit files), with the three outcomes
    (``live``: every live read and act; ``end``: the account kind's proven process end). ``install-skips`` is written
    durably before ``install-shape`` (R14).

    The whole plan in one process is the plan layer's view (its tests, ``--plan-only``). A run with ``--source``
    applies :func:`plan_repair_stage` (phase A) and then this plan with ``continuing`` = the staged generation, in
    that generation's own code (phase B, :func:`repair_continue`): the journalled generation is the target although it
    is not ``current`` yet, and R1-R3 (phase A's) are not planned again. ``fault``: the launcher's hook (§5 [F7]):
    every R-step gets its ``before``/``after`` points, R9 its ``a``/``b``, R2 its ``mid-copy`` and each remove step its
    act boundaries (the remove phase is the step id)."""
    host = facts.host
    shape = facts.install_shape
    journal, finished, operator, run_txn = _repair_journal(cmd, facts, rf, txn)
    previous = frozenset(shape.managed) if shape is not None else frozenset()
    journaled = frozenset(journal.managed) if journal is not None else frozenset()

    # The target generation (R2). U13: an UNFINISHED repair's target is its journal's. Once journaled (R3) it is fixed:
    # a --source must hash to it. Before R3 only a --source may establish it, never the record or current (that would
    # finish the interrupted txn on the generation it was moving away from). With no journal: the --source package's
    # id, else the record's generation, which must be current.
    pinned = journal.generation if journal is not None else None
    if rf.source is not None:
        if rf.h11 is None:
            raise Refused("bad-source", "no h11 beside the levain package")
        root_tree.check_sources(rf.source, rf.h11)
        target = gen_mod.generation_id(rf.source, rf.h11)
        if pinned is not None and target != pinned:
            raise Refused(f"source-mismatch:{pinned}", f"the interrupted repair is moving to {pinned}; --source "
                                                      f"{rf.source} is {target}. Re-run with that package")
        versions = (gen_mod.read_version_text(rf.source / "__init__.py"), root_tree.h11_floor(rf.h11))
        package = rf.source
    else:
        if journal is not None:
            target = pinned
        else:
            target = shape.generation if shape is not None else None
        if continuing is not None and target != continuing:
            raise Refused("handover-mismatch", f"phase B was handed {continuing}; the journal names {target}")
        if target is None or (target != rf.current and target != continuing):
            raise Refused(f"source-required:{target or 'none'}", "this host needs repair --source <levain package>")
        # U17: the record's versions are the TARGET generation's own, read as text from its tree (never an older
        # record's: after a crash past the flip the record may still describe the generation it replaced).
        gen_dir = root_tree.current_path(host).parent / target
        versions = (gen_mod.read_version_text(gen_dir / "levain" / "__init__.py"), root_tree.h11_floor(gen_dir / "h11"))
        package = gen_dir / "levain"
    gen_change = shape is None or shape.generation != target or rf.current != target

    # R4b: the shared groups, kept at the gids they hold (or were journaled at); only the missing ones allocated.
    jgroups = dict(journal.groups) if journal is not None else {}
    present = _recognised_groups(host, facts, rf, jgroups)
    keep_shared = {**{g: i for g, i in jgroups.items() if g in SHARED_GROUPS}, **present}
    # R1b: a recorded or journaled group this code no longer names is on its way out, and its gid is retired at R7
    # (§1.1 g); R6 runs first, so that gid must never be handed to a new group here, even when no group holds it now.
    leaving = {i[1] for k, i in previous | journaled if k == "group/1" and i[0] not in SHARED_GROUPS}
    alloc = allocate(host, egress=(), verifier=(), used=facts.used_ids | leaving, retired=facts.retired, shared=True,
                     keep_shared=keep_shared)
    check_account_ids(host, rf.accounts)
    slots = _repair_slots(rf)
    _check_rendered_accounts(rf, slots)
    services, skipped = units.built_services(host, package, exists=program_exists)
    restricted = False
    if host == "linux":
        restricted = bwrap.userns_restricted() if read_sysctl is None else bwrap.userns_restricted(read_sysctl)
    desired = desired_managed(host, accounts=rf.accounts, shared=alloc.shared, operator=operator,
                              services=[s.name for s in services], slots=slots, apparmor=restricted)
    reconcile = previous | journaled | desired
    remove = reconcile - desired
    live = live if live is not None else RM.Live()

    def removals(phase: str) -> list[Step]:
        # one Live per phase only for its fault hook: ``skips`` is the same set, so R14 still writes every skip
        at = replace(live, fault=lambda pt: fault(phase, pt)) if fault is not None else live
        return removal_steps(host, remove, phase, live=at, held=previous, end=end)
    accts = accounts_of(Allocation(host, (), (), dict(rf.accounts), {}))
    missing_groups = [Group(g, gid, ()) for g, gid in alloc.shared.items() if g not in present]
    missing_pairs = [(a, g) for a, g in membership_pairs(accts, operator) if a not in rf.members.get(g, frozenset())]
    changed = {a for a, _ in missing_pairs}
    stop = list(services) if gen_change else [s for s in services if s.user in changed]

    first = Intent("repair", operator, {}, jgroups, run_txn, journal.generation if journal else None,
                   tuple(journal.managed) if journal else ())
    staged = replace(first, generation=target)
    final = replace(staged, groups=dict(alloc.shared), managed=_shape_of(reconcile))
    noted: dict[str, int] = {}
    carried = dict(shape.started_after) if shape is not None else {}
    keep = {target} | ({shape.generation} if shape is not None else set())
    shape_path = install_shape_path(host)

    def record() -> tuple[bool, str]:
        new = gen_mod.InstallShape(target, versions[0], versions[1], run_txn, {**carried, **noted},
                                   _shape_of(desired))
        ok, why = write_install_skips(host, live, rf.skips)    # X4-M2: before the shape, the one completion commit
        if not ok:
            return ok, why
        return gen_mod.write_record_durably(shape_path, gen_mod.shape_text(new))

    rows = base_store_rows(host, operator) + base_run_rows(host) + slot_rows(host, slots)
    ids = {a.name: a.uid for a in accts}
    if host == "darwin":
        confinement = [*pf.alias_steps(slots), *pf.load_steps(slots, ids)]
    else:
        confinement = [*bwrap.bwrap_steps(apparmor_userns_restricted=restricted), *netns.netns_steps(slots),
                       *tmpfiles_steps(host, slots)]
    steps: list[Step] = []
    if finished is not None:
        steps.append(Step(f"remove the finished repair journal {INTENT_PATH[host]} (its txn is install-shape's "
                          "completed_txn)", (root_tree._tools(host)["rm"], "-f", str(INTENT_PATH[host]))))
    vendor_failed: list[str] = []
    F = functools.partial(_faulted, fault)
    if continuing is None:                          # phase B starts at R4b: phase A journalled R1-R3
        steps += F("R1", [intent_write_step(host, first)])                                        # R1
        if rf.source is not None:
            steps += F("R2", root_tree.stage_steps(host, rf.source, rf.h11, target, current=rf.current,  # type: ignore[arg-type]
                                                   fault=(lambda pt: fault("R2", pt)) if fault else None))
        steps += F("R3", [_journal_step(host, f"journal the generation {target} (same txn)", staged)])  # R3
    steps += F("R4b", [_journal_step(host, "journal the shared-group gids and the write-ahead managed set (same "
                                           "txn)", final)])                                       # R4b
    steps += F("R5", [*_stop_steps(host, stop, noted, clock, live=live),                          # R5
                      *removals("R5")])             # R1b: recorded units stopped by name, then sudoers (grants first)
    steps += F("R6", shared_group_steps(host, missing_groups))                                    # R6
    steps += F("R7", [*membership_steps(host, missing_pairs),                                     # R7
                      *removals("R7")])             # R1b: members, then groups (gid retired first), then accounts
    steps += F("R8", [*removals("R8"),              # R1b: the confinement kinds' removals first, then the re-render
                      *row_steps(host, rows), *reserve_steps(host), *base_routes_steps(host),     # R8
                      *base_routes_merge_steps(host),
                      *single_filesystem_steps(host, slots), *confinement])
    if rf.current != target:
        steps += F("R9", [root_tree.flip_step(host, target,                                       # R9
                                              fault=(lambda pt: fault("R9", pt)) if fault else None)])
    steps += F("R10", [*root_tree.entry_steps(host),                                              # R10
                       *removals("R10"),            # R1b: entries after the flip (their sudoers rule went at R5)
                       # the vendor CLIs re-copied, so a vendor updated since the last repair reaches the slot;
                       # a failed copy does not stop R11/R12: the report step after them fails the run (ruling V6)
                       *root_tree.vendor_steps(host, operator, failed=vendor_failed),
                       # the versions the copies kept, removed only with no session live (ruling V18)
                       *vendor_prune_steps(host, failed=vendor_failed)])
    steps += F("R11", sudoers_steps(host, operator))                                              # R11
    steps += F("R12", [*removals("R12"),            # R1b: the removed units' files
                       *units.install_steps(host, services),                                     # R12
                       root_tree.vendor_report_step(vendor_failed)])
    if rf.legacy or gen_change:
        steps += F("R13", [root_tree.gc_step(host, keep)])                                        # R13
    steps += F("R14", [Step(f"write the install record {shape_path} (completed_txn {run_txn}, last host act)",
                            call=record)])                                                        # R14
    steps += F("R15", [intent_remove_step(host)])                                                 # R15
    check_tools(facts, steps, verb="repair", python=True)
    return InstallPlan("repair", host, tuple(steps), alloc, accts, tuple(missing_groups), tuple(rows),
                       retired_after=facts.retired, pending=helper_pending() + skipped,
                       services=tuple(s.name for s in services), intent=final, generation=target,
                       generation_change=gen_change)


def _act(step: Step, *, run: Callable[..., object] = subprocess.run) -> bool:
    """Whether ``step`` would act on this host, read without acting (the staged-tree launcher's ``--plan-only``):
    a ``skip_if`` probe that exits 0 means no; a ``chown``/``chmod`` whose path already has that owner/mode means no;
    a ``write`` whose file already holds that text at that mode means no; anything else would act."""
    import grp
    import pwd

    if step.skip_if:
        try:
            if run(list(step.skip_if), capture_output=True, cwd="/", env=child_env()).returncode == 0:   # type: ignore[attr-defined]
                return False
        except OSError:
            return True
    if step.write is not None:
        path, text, mode = step.write
        try:
            st = os.lstat(path)
            with open(path, "rb") as fh:
                return not (stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == mode
                            and fh.read() == text.encode("utf-8"))
        except OSError:
            return True
    argv = step.argv
    if len(argv) == 3 and argv[0].endswith(("/chown", "/chmod")):
        try:
            st = os.lstat(argv[2])
        except OSError:
            return True
        if argv[0].endswith("/chmod"):
            return stat.S_IMODE(st.st_mode) != int(argv[1], 8)
        owner, _, group = argv[1].partition(":")
        try:
            return (pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name) != (owner, group)
        except KeyError:
            return True
    return True


def plan_only_report(plan: InstallPlan, err: TextIO, *, act: Callable[[Step], bool] = _act) -> str | None:
    """The staged-tree launcher's ``--plan-only`` apply (a launcher seam, never a grammar flag): each step printed
    ``ACT`` or ``----`` (already so) and NOTHING applied. Every read it makes is a probe (``skip_if``) or an lstat."""
    for i, step in enumerate(plan.steps, 1):
        err.write(f"{'ACT ' if act(step) else '----'} [{i}/{len(plan.steps)}] {step.why}\n")
    return None


# --- the apply layer: ``__main__`` (CONTRACT §5 preamble, §5.2, §25) --------------------------------------


@dataclass(frozen=True)
class Directory:
    """The host's directory service, read once under the install lock: name -> id, and each name's label (macOS
    ``RealName``; Linux a user's GECOS field, a group has none)."""

    users: Mapping[str, int]
    groups: Mapping[str, int]
    user_labels: Mapping[str, str] = field(default_factory=dict)
    group_labels: Mapping[str, str] = field(default_factory=dict)
    #: Each group's listed members (macOS ``GroupMembership``; Linux ``getent group`` field 4). Row 33 §3.
    group_members: Mapping[str, frozenset] = field(default_factory=dict)
    #: Each user's primary gid (Linux ``getent passwd`` field 4; macOS :func:`read_primary_groups`, read only for a
    #: ``repair --source``): a group's members include every user whose primary group it is, whom no member list
    #: names (§2.1, the source writer check).
    user_gids: Mapping[str, int] = field(default_factory=dict)
    #: macOS: each group that nests others (``NestedGroups``), whose members no list here enumerates. Linux: none.
    nested: frozenset = frozenset()


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
    gmem: dict[str, frozenset] = {}
    ugid: dict[str, int] = {}
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
        for ln in _lines(run, (dscl, ".", "-list", "/Groups", "GroupMembership")):
            p = ln.split()
            if p:
                gmem[p[0]] = frozenset(p[1:])
    elif host == "linux":
        getent = _abs("getent")
        for ln in _lines(run, (getent, "passwd")):
            if not ln:
                continue
            p = ln.split(":")
            if len(p) != 7:
                raise Refused("host-read-failed", f"getent passwd: {ln[:120]!r}")
            users[p[0]] = _id(p[2], ln)
            ugid[p[0]] = _id(p[3], ln)
            ulab[p[0]] = p[4]
        for ln in _lines(run, (getent, "group")):
            if not ln:
                continue
            p = ln.split(":")
            if len(p) != 4:
                raise Refused("host-read-failed", f"getent group: {ln[:120]!r}")
            groups[p[0]] = _id(p[2], ln)
            gmem[p[0]] = frozenset(m for m in p[3].split(",") if m)
    else:
        raise Refused("unsupported-host", repr(host))
    if users.get("root") != 0:
        raise Refused("host-read-failed", "the directory service lists no root at uid 0")
    return Directory(users, groups, ulab, glab, gmem, ugid)


def read_primary_groups(d: Directory, *, run: Callable[..., object] = subprocess.run) -> Directory:
    """macOS: ``d`` with each user's primary gid and the groups that nest others, the two reads a group's COMPLETE
    membership needs beyond ``GroupMembership`` (L3 r1, the ``--source`` writer check). A failed read refuses
    ``host-read-failed``; a user with no ``PrimaryGroupID`` is in no group by it."""
    dscl = "/usr/bin/dscl"
    ugid: dict[str, int] = {}
    for ln in _lines(run, (dscl, ".", "-list", "/Users", "PrimaryGroupID")):
        p = ln.split()
        if len(p) > 2:
            raise Refused("host-read-failed", f"dscl /Users PrimaryGroupID: {ln[:120]!r}")
        if len(p) == 2:
            ugid[p[0]] = _id(p[1], ln)
    nested = frozenset(p[0] for p in (ln.split() for ln in _lines(run, (dscl, ".", "-list", "/Groups", "NestedGroups")))
                       if len(p) > 1)
    return replace(d, user_gids=ugid, nested=nested)


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


def vendor_prune_live(store: Path) -> tuple[str, ...]:
    """The live-session evidence :func:`levain.slot.root_tree.prune_vendor` reads (head ruling V18): this run's
    :meth:`Quiesce.live` when it holds ``L/slots/pool.lock`` (the same count preflight's ``live-lease`` refusal took,
    under the hold that keeps a ``prepare`` from leasing beneath the prune). With no ``pool.lock`` no ``prepare`` can
    lease, so the session records' ``lease_live`` (:func:`live_lease_sessions`) are the evidence. A ``pool.lock``
    this process does not hold counts as live: nothing keeps a lease from starting beneath the prune."""
    L = Path(store)
    q = _QUIESCING.get(_store_key(L))
    if q is not None and q.held:
        return q.live()
    if os.path.lexists(L / "slots" / "pool.lock"):
        return (f"{L / 'slots' / 'pool.lock'} not held by this run",)
    return live_lease_sessions(L / "sessions")


def vendor_prune_steps(host: HostOS, *, store: Path | None = None, failed: list | None = None) -> list[Step]:
    """One step per versioned vendor package: :func:`levain.slot.root_tree.prune_vendor` with
    :func:`vendor_prune_live`, read when the step runs. ``allow_fail``: a prune never stops the plan. ``failed`` is
    the sink :func:`levain.slot.root_tree.vendor_steps` appends a failed or indeterminate copy's line to (each line
    starts ``<name>:``): a unit with a line there is not pruned, because the previous version may still be the one a
    crash restores (head ruling V25)."""
    L = Path(store) if store is not None else STORE[host]
    sink = failed if failed is not None else []

    def prune(name: str) -> Callable[[], tuple[bool, str]]:
        def run() -> tuple[bool, str]:
            if any(str(line).startswith(f"{name}:") for line in sink):
                return True, (f"{name}: every version kept (this run's copy of {name} did not finish durably, so "
                              "the previous version may be the one a crash restores)")
            return root_tree.prune_vendor(host, name, live=lambda: vendor_prune_live(L))
        return run
    return [Step(f"remove the old versions of root's {name} copy in {root_tree.VENDOR_PREFIX[host]} (only with no "
                 "session live)", call=prune(name), allow_fail=True) for name in root_tree.VENDOR_UNITS]


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
    """The configured slots (the runtime helper's own reading of ``L/slots/``) whose state record
    :func:`levain.slot.records.in_service` keeps (no state file: in service)."""
    out: dict[str, tuple[int, ...]] = {}
    for cls, indices in RH.configured_slots(store).items():
        keep = []
        for i in sorted(indices):
            view = RH.read_view(store, SlotId(cls, i))   # type: ignore[arg-type]
            if R.in_service(view.state):
                keep.append(i)
        out[cls] = tuple(keep)
    return out


@dataclass(frozen=True)
class Gathered:
    """What :func:`gather_facts` read under the install lock: the facts every plan takes, plus each verb's own
    inputs (undo: every live Levain account and the shared groups present, name -> live gid; replenish: the slots in
    service, by class, and their accounts)."""

    facts: HostFacts
    accounts: Mapping[str, int] = field(default_factory=dict)
    shared_groups: Mapping[str, int] = field(default_factory=dict)
    live: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    #: The pool-lock hold taken before the live-lease count, released by :func:`run` after the last act.
    quiesce: Quiesce | None = None
    #: replenish: ``L/policy/pool``'s ``(slots, verifier_slots)``, or None when it is absent.
    pool: tuple[int, int] | None = None
    #: replenish: every index ``L/slots/`` holds, by class, retired ones included.
    configured: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    #: repair: what :func:`plan_repair` reads beyond the facts (row 33).
    repair: RepairFacts | None = None


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
    if host == "darwin" and cmd.verb == "repair" and cmd.source is not None:
        d = read_primary_groups(d, run=run)
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
    orec, in_service, pool, configured, shape, skips = None, None, None, {}, None, ()
    if os.path.lexists(L):
        # Read as the runtime helper reads the store: one dirfd, every component O_NOFOLLOW, root's dirs only.
        store = RH.Store(rs)
        try:
            orec = _operator_of(store)
            try:
                raw_shape = (store.read(("policy",), "install-shape", cap=gen_mod.MAX_RECORD_BYTES)
                             if orec is not None else None)
                shape = gen_mod.parse_shape(raw_shape) if raw_shape is not None else None
            except Refused as e:
                # U15: undo is the recovery verb. A record whose CONTENT it cannot read (torn, oversized, a newer
                # format) is read as absent, so undo plans what it plans on a pre-record host and removes the file;
                # every other verb refuses. A link or non-file there is a trust fault (bad-root) and refuses undo too.
                if cmd.verb != "undo" or e.name not in ("install-shape-malformed", "record-too-large"):
                    raise
                shape = None
            if cmd.verb in ("replenish", "repair") and orec is not None:
                in_service = slots_in_service(store)
                configured = {cls: tuple(sorted(i)) for cls, i in RH.configured_slots(store).items()}
                raw = store.read(("policy",), "pool")
                pool = parse_pool_config(raw) if raw is not None else None
            if cmd.verb == "repair" and orec is not None:     # R1b-6: a malformed one refuses here, before any act
                skips = parse_install_skips(store.read(("policy",), "install-skips", cap=gen_mod.MAX_RECORD_BYTES))
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
        levain_tombstones=tombs, levain_live=live, intent=journal, host_ids=host_ids, install_shape=shape,
        user_ids=dict(d.users))
    if cmd.verb == "repair":
        repair = _repair_facts(cmd, host, d, accounts, in_service or {}, skips=skips,
                               operator_uid=int(orec["uid"]) if orec is not None else None)
        return Gathered(facts, accounts, quiesce=q, repair=repair)
    if cmd.verb == "undo":
        return Gathered(facts, accounts, {g: d.groups[g] for g in shared}, quiesce=q)
    if in_service is not None:
        wanted = {n for cls, idx in in_service.items() for i in idx for n in SlotId(cls, i).accounts}  # type: ignore[arg-type]
        return Gathered(facts, {n: u for n, u in accounts.items() if n in wanted}, live=in_service, quiesce=q,
                        pool=pool, configured=configured)
    return Gathered(facts, quiesce=q)


def _repair_facts(cmd: InstallCommand, host: HostOS, d: Directory, accounts: Mapping[str, int],
                  in_service: Mapping[str, tuple[int, ...]], *, skips: tuple = (),
                  operator_uid: int | None = None) -> RepairFacts:
    """Row 33 §2.3 R0: the directory's ``levain-*`` groups and their members, the slots in service, what
    ``<tree>/current`` names, whether the legacy tree is there, and ``--source`` RESOLVED (§2.1 (e): every later read
    names the resolved parent), its names checked and then who may write it (§2.1, :func:`root_tree.
    check_source_writers`) before any other root read of it. ``operator_uid`` None (no operator record) with a
    ``--source``: ``not-set-up`` here, before the resolve (L3 r1: the first root read of the source).

    ``account_groups``: each live account's same-named group at its gid (macOS: under Levain's label), for the
    plan's ``group-missing`` refusal (L3 r1)."""
    from levain.slot.intent import GROUP_RE

    if cmd.source is not None and operator_uid is None:
        raise Refused("not-set-up", cmd.verb)
    groups = {g: i for g, i in d.groups.items() if GROUP_RE.fullmatch(g)}
    own = {n: d.groups[n] for n in accounts
           if n in d.groups and (host != "darwin" or d.group_labels.get(n) == SLOT_MARKER)}
    tree = root_tree.current_path(host).parent
    current = root_tree.read_current(tree)
    source = h11 = None
    if cmd.source is not None:
        parent = Path(os.path.realpath(os.path.dirname(cmd.source)))
        source, h11 = parent / os.path.basename(cmd.source), parent / "h11"
        assert operator_uid is not None
        root_tree.check_sources(source, h11)
        root_tree.check_source_writers(parent, operator_uid=operator_uid,
                                       operator_gids=operator_only_gids(d, operator_uid),
                                       group_names={i: g for g, i in sorted(d.groups.items(), reverse=True)})
    return RepairFacts(accounts=dict(accounts), groups=groups, members={g: d.group_members.get(g, frozenset())
                                                                        for g in groups},
                       egress=tuple(in_service.get("egress", ())), verifier=tuple(in_service.get("none", ())),
                       current=current, legacy=os.path.isdir(tree / "levain"), source=source, h11=h11,
                       skips=skips, account_groups=own)


def operator_only_gids(d: Directory, operator_uid: int) -> frozenset:
    """L3 r1 (§2.1, the source writer check): the gids whose COMPLETE membership is the operator alone: every group
    holding that gid lists no one but the operator and nests no group (macOS), no other user holds it as primary
    group, and the operator IS in it (listed or by primary gid). A listed name the directory does not resolve counts
    as another account. Ubuntu's user-private group passes; macOS ``staff`` (gid 20, every local user's primary group,
    listing ``root``) never does. The operator must be found in it because an EMPTY membership proves nothing on
    macOS: its computed groups (``everyone``, ``localaccounts``, ``authedusers``...) list no one and hold everyone
    [measured by r2repair-fix1, 10-22, `dscl . -list /Groups GroupMembership` on Phill's Mac]."""
    by_gid: dict[int, list[str]] = {}
    for g, i in d.groups.items():
        by_gid.setdefault(i, []).append(g)
    out = set()
    for i, gs in by_gid.items():
        if any(g in d.nested for g in gs):
            continue
        names = {n for g in gs for n in d.group_members.get(g, ())} | {n for n, pg in d.user_gids.items() if pg == i}
        if names and all(d.users.get(n) == operator_uid for n in names):
            out.add(i)
    return frozenset(out)


def plan_for(cmd: InstallCommand, g: Gathered, *, fault: Optional[FaultHook] = None, **setup_kw) -> InstallPlan:
    """The verb's plan from what was gathered. ``setup_kw`` reaches :func:`plan_setup` (tests: its host seams).
    Replenish fills to ``L/policy/pool`` (head ruling 8) and refuses ``pool-config-missing`` without it. ``fault``:
    the launcher's hook, given to repair's plans (:func:`run` passes it only when :class:`Seams` carries a fault)."""
    if cmd.verb == "setup":
        return plan_setup(cmd, g.facts, **setup_kw)
    if cmd.verb == "undo":
        return plan_undo(cmd, g.facts, accounts=g.accounts, shared_groups=g.shared_groups)
    if cmd.verb == "replenish":
        slots, verifier_slots = g.pool if g.pool is not None else (None, None)
        return plan_replenish(cmd, g.facts, slots=slots, verifier_slots=verifier_slots, live=g.live,
                              accounts=g.accounts, configured=g.configured)
    if cmd.verb == "repair" and g.repair is not None and cmd.source is not None:
        return plan_repair_stage(cmd, g.facts, g.repair, fault=fault)   # phase A; phase B plans in the staged code
    if cmd.verb == "repair" and g.repair is not None and _resumes_staged(g.facts, g.repair):
        return plan_repair_resume(cmd, g.facts, g.repair)         # phase A, no source: into the journalled generation
    if cmd.verb == "repair" and g.repair is not None:
        return plan_repair(cmd, g.facts, g.repair, fault=fault)   # bare repair: in-process, no exec, no probe
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


def run_purge(session: str, caller_uid: int, host: HostOS, *, store_seams: RH.Seams | None = None,
              verb_seams: Callable[[SV.Cas], SV.StoreSeams] | None = None) -> dict[str, object]:
    """``purge <session>`` (CONTRACT §5.2, §7.4; DESIGN §4.6): the operator's forced purge of a TRASH session, whatever
    its age. The alloc is read as the runtime helper reads the store; a caller who is not its ``operator_uid`` learns
    nothing (``no-session``). The verb is :func:`levain.slot.store_verbs.purge` with ``forced``; its CAS goes to
    CONTROL. A purge that stops part-way keeps its journal and is refused ``purge-incomplete:<phase>``; the next
    ``purge <session>`` finishes that journal first and answers PURGED. ``verb_seams``: a test's store-verb seams around the CAS (default: real ones)."""
    rs = store_seams or RH.Seams(host=host)
    store = RH.Store(rs)
    ctx = None
    try:
        data = store.read(("alloc",), f"{session}.json")
        if data is None:
            raise Refused("no-session", session)
        a = R.parse_alloc(data)
        if a.session != session or a.operator_uid != caller_uid:
            raise Refused("no-session", session)
        ctx = RH.Ctx(rs, store, caller_uid, RH.OPERATOR, a.operator_uid, {})
        cas = SV.control_cas(ctx, session)
        seams = verb_seams(cas) if verb_seams is not None else SV.StoreSeams(cas=cas)
        try:
            state = SV.purge(SV.facts_from_alloc(a, rs.L), seams, forced=True)
        except PhaseFailed as e:
            raise Refused(f"purge-incomplete:{e.phase}", str(e.cause)) from None
    finally:
        if ctx is not None:
            ctx.close()
        store.close()
    return {"verb": "purge", "session": session, "state": state}


# --- set-credential / remove-credential (design 1.8 Bootstrap) ----------------------------------------------------

#: The account the credential child drops to: keys' (CONTRACT §1.3), which owns ``L/keys/`` (0700).
KEYS_ACCOUNT = "_levain_keys"
CREDENTIAL_VERBS = ("set-credential", "remove-credential")
_CHILD_MAX = 256                     # the child's one status line, read to EOF up to this
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
#: what the child may answer, PER VERB, and nothing else is believed: a dropped child's bytes are not root's, and a
#: `set-credential` child's `removed` (or a remove child's `committed`) is not an answer to the verb asked.
_CHILD_OK = {"set-credential": re.compile(r"committed [0-9a-f]{32} (?:durable|not-durable)"),
             "remove-credential": re.compile(r"(?:removed|absent) (?:durable|not-durable)")}
_CHILD_FAILED = re.compile(r"failed (credential-commit-failed|credential-lock-timeout|credential-drop-failed"
                           r"|credential-dir-failed|internal) ([A-Za-z]{1,40})")
#: root's ONE deadline over the child's whole life, the pipe drain AND the reap, is the child's own bounded lock wait
#: (creds.LOCK_TIMEOUT_S, passed to it) plus this margin for its fsyncs. On expiry root kills and reaps the child
#: BEFORE it lets the install lock go and refuses `credential-child-timeout` (HEAD_1022-12 H18).
CHILD_MARGIN_S = 20.0
_REAP_POLL_S = 0.01


def load_creds():   # -> the levain.broker.creds module
    """The credential layer, or the refusal ``credential-layer-unavailable`` naming the missing module: it imports h11
    (through upstream), an optional extra a root Python may lack. Called before stdin is read and before any fork,
    so its absence is a named refusal, never a generic internal from inside the child."""
    try:
        from levain.broker import creds as C
    except ImportError as e:
        raise Refused("credential-layer-unavailable", getattr(e, "name", None) or "import") from None
    return C


def _field_slug(field: str) -> str:
    """A field name as a refusal qualifier (``refresh.token_url host`` -> ``refresh-token-url-host``)."""
    return re.sub(r"[^a-z0-9]+", "-", field.lower())[:40].strip("-") or "field"


def _read_stdin(n: int) -> bytes:
    return sys.stdin.buffer.read(n)


def read_credential(name: str, read: Callable[[int], bytes]) -> dict:
    """``set-credential``'s stdin: one ``levain-keys-credential/1`` object WITHOUT ``rev``, at most the file cap,
    parsed with duplicate keys refused and validated in root by the credential layer's own render + parse (schema,
    size caps, ``hosts``, ``token_url``'s host in ``hosts``, ``name`` = the argv's). Refused ``bad-credential:<field>``
    naming the field, never a value. Nothing is forked or opened before this passes."""
    C = load_creds()   # here, not at import: undo and purge never need the gateway's h11

    raw = read(C.FILE_CAP + 1)
    if len(raw) > C.FILE_CAP:
        raise Refused("bad-credential:size", f"more than {C.FILE_CAP} bytes")
    try:
        fields = json.loads(raw.decode("utf-8"), object_pairs_hook=C._no_dup)
    except (ValueError, RecursionError, UnicodeError):
        raise Refused("bad-credential:json", "not one JSON object") from None
    if not isinstance(fields, dict):
        raise Refused("bad-credential:format", "not a JSON object")
    try:
        C.render(fields, name)          # the bytes a writer would write, parsed; its fresh rev is thrown away
    except C.Unparseable as e:
        raise Refused(f"bad-credential:{_field_slug(str(e))}", str(e)) from None
    return fields


def drop_to_keys(uid: int, gid: int) -> None:
    """The child's drop (HEAD_1022-12 H5): wall's one drop, the groups EXACTLY ``{gid}`` (wall refuses an empty list,
    s1-1d D12), no ``RLIMIT_NPROC`` (the child never forks)."""
    from levain.slot import wall

    wall.drop_and_assert(uid, gid, (gid,), nproc=None)


def _credential_child(verb: str, name: str, fields: Optional[dict], keys_dir: str, uid: int, gid: int, w: int,
                      drop: Callable[[int, int], None], lock_timeout_s: float) -> None:
    """The forked child. Never returns: it writes one status line on ``w`` and ``os._exit``s (a return would run the
    parent's stack a second time and write a second stdout line). Order (design 1.8, §8 known edge): close every
    inherited fd but 0-2 and ``w``, stdin and stdout onto /dev/null (``w`` is the only result channel; stderr stays,
    for the credential layer's warnings), cwd ``/``, umask 077, an empty environment, THEN the drop, and only then
    open ``L/keys`` and ``cred`` below it. Each open is ``O_NOFOLLOW`` on its LAST component only: ``L`` and its
    ancestors are root-owned, which is what makes the path safe. The credential lock is taken bounded
    (``lock_timeout_s``, creds' constant): a stopped or wedged holder is ``credential-lock-timeout``. ``set`` makes
    ``cred/`` when absent and fsyncs ``L/keys`` after (a failure there is ``not-durable``); ``remove`` creates
    nothing: no ``cred/`` is ``absent``."""
    code, msg = 70, "failed internal Unknown"
    try:
        from levain.broker import creds as C
        from levain.slot.stage import _close_fds_except

        _close_fds_except((w,))
        nul = os.open(os.devnull, os.O_RDWR)
        os.dup2(nul, 0)
        os.dup2(nul, 1)
        if nul > 2:
            os.close(nul)
        os.chdir("/")
        os.umask(0o077)
        os.environ.clear()
        try:
            drop(uid, gid)
        except Refused:
            msg = "failed credential-drop-failed Refused"
            raise
        keys_durable = True
        try:
            keys_fd = os.open(keys_dir, _O_DIR)
            if verb == "set-credential":
                try:
                    os.mkdir("cred", 0o700, dir_fd=keys_fd)
                except FileExistsError:
                    pass
                try:
                    C.dir_fsync(keys_fd)              # every set, so a retry discharges an earlier failed sync of the `cred` entry
                except OSError:
                    keys_durable = False
            try:
                cred_fd = os.open("cred", _O_DIR, dir_fd=keys_fd)
            except FileNotFoundError:
                if verb == "set-credential":
                    raise
                cred_fd = None                         # remove on a host that never had a credential
        except OSError as e:
            msg = f"failed credential-dir-failed {type(e).__name__}"
            raise
        try:
            if verb == "set-credential":
                out = C.commit(cred_fd, name, fields or {}, wait=False,   # the operator's write: no rev compare
                               lock_timeout_s=lock_timeout_s)
                msg = f"committed {out.rev} {'durable' if out.durable and keys_durable else 'not-durable'}"
            elif cred_fd is None:
                msg = "absent durable"
            else:
                out = C.remove(cred_fd, name, wait=False, lock_timeout_s=lock_timeout_s)
                msg = f"{out.result} {'durable' if out.durable else 'not-durable'}"
        except C.CommitFailed as e:
            msg = f"failed {e.name} CommitFailed"
            raise
        code = 0
    except BaseException as e:   # noqa: BLE001 - the child's only way out is os._exit below
        if msg == "failed internal Unknown":
            msg = f"failed internal {type(e).__name__[:40]}"
    finally:
        try:
            os.write(w, msg.encode("ascii") + b"\n")
        finally:
            os._exit(code)


def _check_operator(verb: str, caller_uid: int, rs: RH.Seams) -> None:
    store = RH.Store(rs)
    try:
        rec = _operator_of(store)
    finally:
        store.close()
    if rec is None:
        raise Refused("not-set-up", verb)
    if caller_uid != rec.get("uid") or _user_name(caller_uid) != rec.get("name"):
        raise Refused("operator-mismatch", f"configured for {rec.get('name')!r}")


def _wait_child(pid: int, r: int, deadline: float) -> tuple[bytes, int]:
    """The child's status line (read to EOF, at most _CHILD_MAX + 1 bytes) and its wait status, both within ONE
    `deadline` (monotonic). On expiry, or on any exception, the child is SIGKILLed and reaped before this returns or
    raises; expiry is the refusal ``credential-child-timeout``."""
    chunks, got, status, reaped = [], 0, 0, False
    try:
        while got <= _CHILD_MAX:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Refused("credential-child-timeout", "the child did not answer in time")
            if not select.select([r], [], [], left)[0]:
                continue
            b = os.read(r, _CHILD_MAX + 1 - got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                break
            if time.monotonic() >= deadline:
                raise Refused("credential-child-timeout", "the child did not exit in time")
            time.sleep(_REAP_POLL_S)
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)    # SIGKILL ends a stopped child too
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
    return b"".join(chunks), status


def run_credential(verb: str, name: str, caller_uid: int, host: HostOS, *, read: Callable[[int], bytes],
                   lock: Callable[[], ContextManager[object]], store_seams: RH.Seams | None = None,
                   drop: Callable[[int, int], None] = drop_to_keys, fork: Callable[[], int] = os.fork,
                   err: TextIO | None = None, lock_timeout_s: float | None = None,
                   deadline_s: float | None = None) -> dict[str, object]:
    """``set-credential`` / ``remove-credential``. Order: the operator check (as every verb's, from
    ``L/policy/operator``; a non-operator never parks root on a stdin read), the credential layer's import
    (:func:`load_creds`), ``set``'s object from stdin validated in root (:func:`read_credential`), all before the
    install lock; then UNDER the lock: the operator check again, FORK, and root waits on the child within ONE deadline
    (``lock_timeout_s``, creds' bound the child's flock uses, + CHILD_MARGIN_S), killing and reaping it before the lock
    is let go. The child (:func:`_credential_child`) drops to keys and writes. Root opens, creates, writes, renames and
    chowns nothing below ``L/keys/`` (design 1.8, r1-14). No pool hold and no live-lease count: keys reads credentials
    on demand. ``drop`` / ``fork`` / ``deadline_s``: tests run as the lane uid with a no-op drop (the real drop is
    proven with real uids, 3b-8a)."""
    rs = store_seams or RH.Seams(host=host)
    _check_operator(verb, caller_uid, rs)
    C = load_creds()
    fields = read_credential(name, read) if verb == "set-credential" else None
    bound = C.LOCK_TIMEOUT_S if lock_timeout_s is None else lock_timeout_s
    with lock():
        _check_operator(verb, caller_uid, rs)      # under the lock: an undo between the two checks is seen
        uid, gid = rs.account_uid(KEYS_ACCOUNT), rs.group_gid(KEYS_ACCOUNT)
        keys_dir = str(rs.L / "keys")
        r, w = os.pipe()
        try:
            pid = fork()
        except BaseException:
            os.close(r)
            os.close(w)
            raise
        if pid == 0:   # pragma: no cover - the child; covered by the tests' real fork, measured by its effects
            os.close(r)
            _credential_child(verb, name, fields, keys_dir, uid, gid, w, drop, bound)
        os.close(w)
        try:
            raw, status = _wait_child(pid, r, time.monotonic() + (bound + CHILD_MARGIN_S if deadline_s is None
                                                                    else deadline_s))
        finally:
            os.close(r)
    line = raw.decode("ascii", "replace").rstrip("\n")
    exited = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    if exited == 0 and _CHILD_OK[verb].fullmatch(line):
        words = line.split(" ")
        if words[-1] == "not-durable":
            what = {"committed": "committed but not durable (the directory fsync failed); it is in use",
                    "removed": "removed but not durable (the directory fsync failed); a crash may bring it back",
                    "absent": "was absent, but not durable (the directory fsync failed); an earlier removal may "
                              "come back after a crash",
                    }[words[0]]
            (err or sys.stderr).write(f"levain install-helper: warning: credential {name} {what}\n")
        if words[0] == "committed":
            return {"verb": verb, "credential": name, "rev": words[1]}
        return {"verb": verb, "credential": name, "removed": words[0] == "removed"}
    m = _CHILD_FAILED.fullmatch(line)
    if exited == 70 and m and m.group(1) != "internal":
        raise Refused(m.group(1), m.group(2))
    raise RuntimeError(f"the credential child: status {status:#x}, {line[:80]!r}")


# --- set-limits (slot limits DESIGN §3, 1025-6/limits; head rulings R0, R1 F5-F7) --------------------------------

def read_limits(read: Callable[[int], bytes]) -> bytes:
    """``set-limits``' stdin: one ``levain-limits/1`` document, at most the slot cap, checked WHOLE in root by the one
    slot parser (:func:`levain.slot.limits_doc.slot_doc`, the queue broker's) with non-canonical bytes accepted, and
    returned as its canonical bytes, re-read through the broker's own check: root never writes the caller's bytes,
    and never bytes the broker would refuse. A refusal is the parser's code (``limits-oversize``,
    ``limits-malformed``, ``limits-key:<key>``, ``limits-not-counted:<kind>``, ``limits-value:<block>[.<kind>]``) with
    its qualifier in the refusal wire's spelling (:func:`_limits_refusal`: ``active_hours`` -> ``active-hours``)."""
    raw = read(LD.SLOT_LIMITS_MAX + 1)
    try:
        data = canon.canonical_bytes(LD.slot_doc(raw, canonical=False))
        LD.slot_doc(data)
    except LD.LimitsDocError as e:
        raise _limits_refusal(e.code, e.detail) from None
    return data


def _limits_refusal(code: str, detail: str) -> Refused:
    """The parser's code as a refusal the wire carries: the refusal line keeps only a lowercase name and a qualifier of
    letters and digits joined by ``-`` / ``.`` (any other name goes out as ``internal``), so each other run in the
    qualifier becomes one ``-``. The exact code stays in the detail (stderr)."""
    name, _, qual = code.partition(":")
    if not qual:
        return Refused(name, detail)
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", qual)
    slug = re.sub(r"[-.]*([-.])[-.]*", r"\1", slug).strip("-.")[:40].strip("-.") or "key"
    return Refused(f"{name}:{slug}", f"{code}: {detail}" if detail else code)


def run_set_limits(caller_uid: int, host: HostOS, *, read: Callable[[int], bytes],
                   lock: Callable[[], ContextManager[object]], store_seams: RH.Seams | None = None,
                   err: TextIO | None = None, fsync: Callable[[int], None] | None = None) -> dict[str, object]:
    """``set-limits``: the whole ``L/policy/limits`` replaced by stdin's document (one change, one password). Order
    (as the credential verbs'): the operator check, stdin read and checked in root (:func:`read_limits`), both before
    the install lock; UNDER the lock the operator check again (an undo between the two is seen), then the write:
    ``limits.tmp`` (``O_EXCL | O_NOFOLLOW``, 0644, fsynced; macOS ``F_FULLFSYNC``), renamed over ``limits``, then
    ``L/policy`` fsynced (:func:`levain.slot.records.write_durably`). The new entry is root's (the helper's euid): a
    file of another owner or mode is replaced, never edited. A failure before the rename refuses
    ``limits-write-failed`` with the old file in force; a failure of the directory fsync after it answers ok and warns
    on ``err`` (in force now, it may not survive a crash). No pool hold and no live-lease count: the broker re-reads the
    file on its next GATE. ``fsync``: a test's seam (default :func:`levain.slot.generation._full_fsync`)."""
    import hashlib

    rs = store_seams or RH.Seams(host=host)
    _check_operator("set-limits", caller_uid, rs)
    data = read_limits(read)
    path = rs.L / policy_limits_path(host).relative_to(STORE[host])     # STORE on a host, a test's scratch L otherwise
    with lock():
        _check_operator("set-limits", caller_uid, rs)
        try:
            R.write_durably(path, data, mode=0o644, fsync=fsync or gen_mod._full_fsync)
        except R.NotDurable as e:
            (err or sys.stderr).write(f"levain install-helper: warning: {path} written but not durable ({e.strerror}): "
                                      "it is in force now and may not survive a crash; run it again\n")
        except OSError as e:
            raise Refused("limits-write-failed", f"{path}: {e.strerror or e}; the earlier file stands") from None
    return {"verb": "set-limits", "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


def policy_limits_state(host: HostOS, live: RM.Live) -> str:
    """``L/policy/limits`` for undo: OURS by path and owner alone (a regular file of root's), WHATEVER its bytes, so an
    unusable file goes too (R1 F7); a link or another owner's entry there is NOT_OURS and left."""
    return RM.root_file(live, policy_limits_path(host))


def policy_limits_undo_steps(host: HostOS, live: RM.Live) -> list[Step]:
    """Undo's removal of ``L/policy/limits``: set under the operator record undo removes, so a later setup for
    another operator does not inherit its limits. The live read first (ABSENT runs nothing)."""
    path = policy_limits_path(host)
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    return [RM.remove_step(f"remove the slot limits {path}", ("policy-limits", str(path)), live,
                           lambda: policy_limits_state(host, live),
                           lambda: live.act((rm, "-f", RM.at(live, path))), tools=(rm,))]


def authority_signers_undo_steps(host: HostOS, live: RM.Live) -> list[Step]:
    """Undo's removal of root's allowed-signers file (:func:`levain.slot.root_tree.authority_signers_path`), so a
    later setup for another operator does not inherit the keys root listed for this one. OURS by path and owner alone
    (:func:`levain.slot.removal.root_file`); a link or another owner's entry there is left. The live read first."""
    path = root_tree.authority_signers_path(host)
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    return [RM.remove_step(f"remove root's authority signers {path}", ("authority-signers", str(path)), live,
                           lambda: RM.root_file(live, path),
                           lambda: live.act((rm, "-f", RM.at(live, path))), tools=(rm,))]


# --- routes-base and routes-trust (3c design §2.3, Q15 r3; 3c-9) -------------------------------------------------

#: Q15 r3: the stdin of ``routes-trust``, exactly these keys, canonical bytes.
ROUTES_TRUST_REQUEST = "levain-routes-trust-request/1"
ROUTES_TRUST_REQUEST_KEYS = frozenset({"format", "store_id", "fingerprint", "public_key"})
ROUTES_TRUST_REQUEST_MAX = 16 * 1024
#: the key types a configuration trust root may be: the laptop custody table's (``levain.authority``'s
#: ``_LAPTOP_KEY_TYPES``; a test holds the two equal). Written here so root imports no authority code.
TRUST_KEY_TYPES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "sk-ssh-ed25519@openssh.com",
                             "sk-ecdsa-sha2-nistp256@openssh.com"})
SSH_KEYGEN = "/usr/bin/ssh-keygen"
_KEY_LINE_RE = PUBLIC_KEY_RE
_STORE_ID_RE = re.compile(r"[0-9a-f]{32}")
_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_KEYGEN_LINE_RE = re.compile(r"[0-9]+ (SHA256:[A-Za-z0-9+/]{43}) ")


def _routes_dir(rs: RH.Seams, host: HostOS) -> Path:
    """``L/routes`` under the store the seams name (STORE on a host, a test's scratch L otherwise)."""
    return rs.L / "routes"


def merge_base_routes(routes: Path, *, fsync: Callable[[int], None] | None = None,
                      warn: Callable[[str], None] | None = None) -> tuple[list, bool]:
    """``routes-base``'s act: ``routes/base.json`` read (absent = the empty map), the shipped routes (the vendor
    routes and ``egress-default``) merged in by route id (:func:`levain.slot.vendor_routes.merge`, checked by keys'
    own parser), written durably root 0644 when
    the bytes, the owner or the mode differ. Returns ``(the shipped route ids the map holds, written)``; a map that cannot be merged
    is ``Refused`` by the merge's code and left as it is, and a failure before the rename is the ``OSError``. Told to
    ``warn`` (default: stderr): each host left off ``egress-default`` because a route of the operator's admits it (on
    every run, written or not), each field of a shipped route's entry the merge reverted
    (:func:`levain.slot.vendor_routes.reverted_fields`), and a rename whose directory fsync failed (the map is in
    force, ``written`` is True)."""
    warn = warn or (lambda line: sys.stderr.write(f"levain install-helper: warning: {line}\n"))
    path = Path(routes) / "base.json"
    raw, doc = _read_base_map(path)
    try:
        out, ids, notes = VR.merge(doc)
    except VR.MergeRefused as e:
        raise Refused(e.code, f"{path}: {e.detail}; it is left as it is") from None
    written = _write_base_map(path, raw, out, verb="routes-base", fsync=fsync, warn=warn)
    for note in notes:
        warn(f"{note} ({path})")
    if not written:
        return ids, False
    for rid, fields in sorted(VR.reverted_fields(doc, out).items()):
        warn(f"route {rid} in {path} replaced by the shipped entry; it had changed {', '.join(fields)}, now the "
             "shipped values")
    return ids, True


def _read_base_map(path: Path) -> tuple[bytes | None, object]:
    """``(the bytes on disk or None when absent, their parse)``; bytes that are not canonical JSON are
    ``base-map-malformed`` and left as they are."""
    raw = R.read_record(path, max_bytes=1 << 20)
    try:
        return raw, (None if raw is None else canon.parse(raw))
    except canon.ContractError:
        raise Refused("base-map-malformed", f"{path} is not canonical JSON; it is left as it is") from None


def _write_base_map(path: Path, raw: bytes | None, doc: dict, *, verb: str,
                    fsync: Callable[[int], None] | None, warn: Callable[[str], None]) -> bool:
    """``doc`` written to ``path`` durably, root 0644, unless ``raw`` (the bytes read) already holds it with root's
    owner and that mode: then False. A failure before the rename is the ``OSError`` (the earlier map stands); a
    failed directory fsync after it is told to ``warn`` (the map is in force) and answers True."""
    data = canon.canonical_bytes(doc)
    if raw == data:
        st = os.lstat(path)
        if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o644:
            return False
    try:
        R.write_durably(path, data, mode=0o644, fsync=fsync or gen_mod._full_fsync)
    except R.NotDurable as e:
        warn(f"{path} written but not durable ({e.strerror}): it is in force now and may not survive a crash; run "
             f"`sudo install-helper {verb}` again")
    return True


def base_routes_merge_steps(host: HostOS, *, store: Path | None = None) -> list[Step]:
    """setup's and repair's ``routes-base``: after :func:`levain.slot.layout.base_routes_steps` (the empty map when
    absent), so an upgraded store holding the empty map ends with the vendor routes. Best-effort: a map that cannot be
    merged is the operator's own and is named, never rewritten, and never stops the install."""
    routes = (Path(store) if store is not None else STORE[host]) / "routes"

    def act() -> tuple[bool, str]:
        warnings: list[str] = []
        try:
            ids, written = merge_base_routes(routes, warn=warnings.append)
        except Refused as e:
            return False, f"{e.name}: {e.detail} (`sudo install-helper routes-base` merges it once it parses)"
        except OSError as e:
            return False, (f"base-map-write-failed: {e.strerror or e}; the earlier map stands (`sudo install-helper "
                           "routes-base` merges it once it can be written)")
        line = f"{'merged' if written else 'already holds'} the shipped routes {', '.join(ids)}"
        return True, "; ".join([line, *(f"warning: {w}" for w in warnings)])

    return [Step(f"merge the shipped routes (vendor, egress-default) into {routes / 'base.json'} (by route id)",
                 call=act,
                 allow_fail=True)]


def run_routes_base(caller_uid: int, host: HostOS, *, lock: Callable[[], ContextManager[object]],
                    store_seams: RH.Seams | None = None, err: TextIO | None = None,
                    fsync: Callable[[int], None] | None = None) -> dict[str, object]:
    """``routes-base``: the vendor routes merged into ``L/routes/base.json`` (:func:`merge_base_routes`), the operator
    checked before the install lock and again under it. A failure before the rename refuses
    ``base-map-write-failed`` with the old map in force; a failed directory fsync after it answers ok (the merged ids
    named) and warns on ``err``, as does each reverted field of a shipped route."""
    rs = store_seams or RH.Seams(host=host)
    _check_operator("routes-base", caller_uid, rs)

    def warn(line: str) -> None:
        (err or sys.stderr).write(f"levain install-helper: warning: {line}\n")
    with lock():
        _check_operator("routes-base", caller_uid, rs)
        try:
            ids, written = merge_base_routes(_routes_dir(rs, host), fsync=fsync, warn=warn)
        except OSError as e:
            raise Refused("base-map-write-failed", f"{e.strerror or e}; the earlier map stands") from None
    return {"verb": "routes-base", "routes": ids, "written": written}


#: WF-1b: the stdin of ``routes-egress``, exactly ``{format, hosts}``, canonical bytes.
ROUTES_EGRESS_REQUEST = "levain-routes-egress-request/1"
ROUTES_EGRESS_REQUEST_MAX = 16 * 1024
#: [judged by lane wf1b, 10-26, against F3's four shipped hosts and the request cap (64 names of the longest legal
#: length do not fit 16 KiB, so the cap binds first for long names): an operator list is a handful of hosts]
ROUTES_EGRESS_MAX_HOSTS = 64


def _read_capped(read: Callable[[int], bytes], cap: int) -> bytes:
    """``read`` called until it returns no bytes or one past ``cap`` has been read (a pipe may hand stdin over in
    pieces)."""
    parts: list[bytes] = []
    got = 0
    while got <= cap:
        chunk = read(cap + 1 - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def read_egress_request(read: Callable[[int], bytes]) -> list[str]:
    """``routes-egress``'s stdin: one canonical ``levain-routes-egress-request/1`` object, exactly ``format`` and
    ``hosts``; ``hosts`` a list of at most :data:`ROUTES_EGRESS_MAX_HOSTS` distinct names, each passing the policy's one
    host grammar (:func:`levain.broker.policy.is_host_name`: a lowercase A-label name, no address literal, no
    wildcard). Returned sorted. Refused ``routes-egress-request:<size|json|keys|hosts>``."""
    raw = _read_capped(read, ROUTES_EGRESS_REQUEST_MAX)
    if len(raw) > ROUTES_EGRESS_REQUEST_MAX:
        raise Refused("routes-egress-request:size", f"over {ROUTES_EGRESS_REQUEST_MAX} bytes")
    try:
        obj = canon.parse(raw)
        if canon.canonical_bytes(obj) != raw:
            raise canon.ContractError("not canonical")
    except canon.ContractError:
        raise Refused("routes-egress-request:json", "not canonical JSON") from None
    if type(obj) is not dict or set(obj) != {"format", "hosts"} or obj["format"] != ROUTES_EGRESS_REQUEST:
        raise Refused("routes-egress-request:keys", f"exactly format ({ROUTES_EGRESS_REQUEST}) and hosts")
    hosts = obj["hosts"]
    if type(hosts) is not list or len(hosts) > ROUTES_EGRESS_MAX_HOSTS:
        raise Refused("routes-egress-request:hosts", f"a list of at most {ROUTES_EGRESS_MAX_HOSTS} host names")
    for h in hosts:
        if not broker_policy.is_host_name(h):
            raise Refused("routes-egress-request:hosts", f"{str(h)[:80]!r} is not a lowercase A-label host name")
    if len(set(hosts)) != len(hosts):
        raise Refused("routes-egress-request:hosts", "a host is named twice")
    return sorted(hosts)


def run_routes_egress(caller_uid: int, host: HostOS, *, read: Callable[[int], bytes],
                      lock: Callable[[], ContextManager[object]], store_seams: RH.Seams | None = None,
                      err: TextIO | None = None, fsync: Callable[[int], None] | None = None) -> dict[str, object]:
    """``routes-egress`` (WF-1b): the operator's egress allow-list, the ``egress-operator`` route of
    ``L/routes/base.json``, set to the request's hosts (replaced whole; an empty list removes the route). Order, as
    ``routes-trust``'s: the operator checked, stdin read and checked in root (:func:`read_egress_request`), both before
    the install lock; under it the operator checked again, the map read, the route set
    (:func:`levain.slot.vendor_routes.set_operator_hosts`: keys' parser, and no egress host on another route), and the
    map written durably root 0644 unless it already holds it. Refused by name with the map left as it is:
    ``routes-egress-request:*``, ``routes-egress-host-on-route``, ``base-map-missing``, ``base-map-malformed``;
    ``base-map-write-failed`` for a failure before the rename (the earlier map stands); a failed directory fsync after
    it answers ok and warns on ``err``. The list reaches the sessions rendered after it [judged by lane wf1b, 10-26,
    against ``runtime_helper.render_facts`` reading ``base.json`` at render and ``front.load_policy`` parsing the
    session file render writes]."""
    rs = store_seams or RH.Seams(host=host)
    _check_operator("routes-egress", caller_uid, rs)
    hosts = read_egress_request(read)

    def warn(line: str) -> None:
        (err or sys.stderr).write(f"levain install-helper: warning: {line}\n")
    with lock():
        _check_operator("routes-egress", caller_uid, rs)
        path = _routes_dir(rs, host) / "base.json"
        raw, doc = _read_base_map(path)
        try:
            out = VR.set_operator_hosts(doc, hosts)
        except VR.MergeRefused as e:
            raise Refused(e.code, f"{path}: {e.detail}" + ("" if doc is None else "; it is left as it is")) from None
        try:
            written = _write_base_map(path, raw, out, verb="routes-egress", fsync=fsync, warn=warn)
        except OSError as e:
            raise Refused("base-map-write-failed", f"{e.strerror or e}; the earlier map stands") from None
    return {"verb": "routes-egress", "hosts": hosts, "written": written}


def read_trust_request(read: Callable[[int], bytes]) -> dict:
    """``routes-trust``'s stdin (Q15 r3): one canonical ``levain-routes-trust-request/1`` object, exactly its four
    keys, ``store_id`` 32 lowercase hex, ``fingerprint`` ``SHA256:<43>``, ``public_key`` ONE ``<type> <base64>`` line
    of a :data:`TRUST_KEY_TYPES` type (no options, no comment). Refused ``routes-trust-request:<field>``. ``read`` is
    called until it returns no bytes or one past the cap has been read (a pipe may hand the request over in pieces)."""
    parts: list[bytes] = []
    got = 0
    while got <= ROUTES_TRUST_REQUEST_MAX:
        chunk = read(ROUTES_TRUST_REQUEST_MAX + 1 - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    raw = b"".join(parts)
    if len(raw) > ROUTES_TRUST_REQUEST_MAX:
        raise Refused("routes-trust-request:size", f"over {ROUTES_TRUST_REQUEST_MAX} bytes")
    try:
        obj = canon.parse(raw)
        if canon.canonical_bytes(obj) != raw:
            raise canon.ContractError("not canonical")
    except canon.ContractError:
        raise Refused("routes-trust-request:json", "not canonical JSON") from None
    if type(obj) is not dict or set(obj) != ROUTES_TRUST_REQUEST_KEYS or obj["format"] != ROUTES_TRUST_REQUEST:
        raise Refused("routes-trust-request:keys", f"exactly {sorted(ROUTES_TRUST_REQUEST_KEYS)}, format "
                                                   f"{ROUTES_TRUST_REQUEST}")
    if type(obj["store_id"]) is not str or not _STORE_ID_RE.fullmatch(obj["store_id"]):
        raise Refused("routes-trust-request:store-id", "32 lowercase hex")
    if type(obj["fingerprint"]) is not str or not _FINGERPRINT_RE.fullmatch(obj["fingerprint"]):
        raise Refused("routes-trust-request:fingerprint", "SHA256:<43 base64>")
    m = _KEY_LINE_RE.fullmatch(obj["public_key"]) if type(obj["public_key"]) is str else None
    if m is None or m.group(1) not in TRUST_KEY_TYPES:
        raise Refused("routes-trust-request:public-key", f"one '<type> <base64>' line of {sorted(TRUST_KEY_TYPES)}")
    return obj


def key_fingerprint(public_key: str, *, ssh_keygen: str = SSH_KEYGEN, timeout: float = 10.0,
                    refusal: str = "routes-trust-verifier") -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -l -E sha256`` prints for ``public_key``, the key handed to it on a
    pipe (``/dev/fd/N``, written whole by :func:`levain.slot.write_all` before ssh-keygen runs: the longest key line
    ``_KEY_LINE_RE`` admits, 8,260 bytes with its newline, is under the 65,536-byte pipe buffer of both hosts
    [measured on macOS 10-25 by lane-3c9-fix; Linux's default]). ``routes-trust-verifier`` when ssh-keygen cannot run
    or does not name a fingerprint (``refusal``: another verb's name for that)."""
    r, w = os.pipe()
    try:
        try:
            write_all(w, (public_key + "\n").encode("ascii"))
        finally:
            os.close(w)
        p = subprocess.run([ssh_keygen, "-l", "-E", "sha256", "-f", f"/dev/fd/{r}"], capture_output=True, text=True,
                           timeout=timeout, check=False, pass_fds=(r,), cwd="/", env=child_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(refusal, f"ssh-keygen could not run ({type(e).__name__})") from None
    finally:
        os.close(r)
    m = _KEYGEN_LINE_RE.match(p.stdout)
    if p.returncode != 0 or m is None:
        raise Refused(refusal, f"ssh-keygen read no key: {(p.stderr or p.stdout).strip()[:120]}")
    return m.group(1)


def _trust_on_disk(routes: Path) -> dict | None:
    """``trust.json`` as root wrote it, or None when absent; one that does not parse is ``routes-trust-exists`` (only
    ``--replace``, Q16, may set aside what is there)."""
    raw = R.read_record(routes / "trust.json", max_bytes=64 * 1024)
    if raw is None:
        return None
    try:
        obj = canon.parse(raw)
    except canon.ContractError:
        obj = None
    if type(obj) is not dict or set(obj) != {"format", "store_id", "fingerprint"}:
        raise Refused("routes-trust-exists", f"{routes / 'trust.json'} is there and does not parse")
    return obj


def _trust_file_holds(path: Path, data: bytes, *, max_bytes: int = 64 * 1024) -> bool:
    """``path`` holds ``data``, owned by the helper's euid at 0644: what root writes there, readable by CONTROL
    (``merge_base_routes``' test; the group is not read, at 0644 it grants what every account has)."""
    if R.read_record(path, max_bytes=max_bytes) != data:
        return False
    st = os.lstat(path)
    return st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o644


#: Q16: what ``routes-trust --replace`` sets aside with the old pin, each a path under ``L`` and whether it is a
#: directory, which is made again empty with the old one's owner, group and mode: CONTROL's route-map generations and
#: head, its verifier generations and head (signed under the same trust root, 3c-8), and keys' head.
REPLACE_ASIDE = ((("routes", "dispatch"), True), (("routes", "verifiers"), True), (("keys", "routemap.head"), False))
#: each entry by the name it has in the aside dir (and in the intent marker's ``moved``)
_ASIDE_BY_NAME = {parts[-1]: (parts, is_dir) for parts, is_dir in REPLACE_ASIDE}
#: DECISION: the most session records a replace reads to find the live ones (far above any pool's sessions).
REPLACE_SESSIONS_MAX = 65536
#: L3 r1 Q1: the intent marker's format (:data:`levain.slot.grammar.ROUTES_REPLACE_INTENT`): the aside dir's name, the
#: entries found there to move, the old pin's bytes (base64; null for a file that was not there), and the sha256 of each
#: file of the new pair, so a rerun can undo the replace from the marker alone, or finish one whose new pair is in force.
REPLACE_INTENT_FORMAT = "levain-routes-replace/1"
_INTENT_KEYS = frozenset({"format", "aside", "moved", "old", "new"})
_ASIDE_NAME = re.compile(r"\.replaced-[0-9]{8}T[0-9]{6}Z(-[0-9]{1,6})?\Z")
_PAIR_NAMES = ("trust.pub", "trust.json")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
#: the old pin's two files, base64, with the marker's own keys (each file is read at most 64 KiB)
_INTENT_MAX = 256 * 1024
#: the states :func:`_session_live` knows to be past live; any other state, a new one included, counts as live
_NOT_LIVE = frozenset(R.STATES) - R.LIVE_STATES


def _session_live(data: bytes, session: str) -> bool:
    """One ``L/sessions/<session>.json``: live unless its state is one this helper knows to be past live
    (:data:`_NOT_LIVE`; a state it does not know counts as live). A record that does not parse, or names another
    session, raises: the replace then cannot rule the session out."""
    rec = R.parse_state(data)
    if rec.session != session:
        raise ValueError(f"{session}.json names {rec.session}")
    return rec.state not in _NOT_LIVE


def _control_references(sessions: Path) -> tuple[frozenset, frozenset]:
    """The route-map and verifier generations CONTROL's records still reference, read from disk as CONTROL's own start
    reads them: every session record (``s-*.json``), and the ticket journal rebuilt by
    :class:`levain.slot.tickets.TicketStore`'s own reader, never compacted or written. The route-map generations are
    :meth:`TicketStore.referenced_generations` of the live sessions (an OPEN ticket's, and a ticket's bound to a live
    session, R4 §5), the verifier ones :meth:`TicketStore.referenced_verifier_generations`. No ``sessions`` dir is no
    references (L3 r1 Q7). Anything else that cannot be read whole is ``routes-trust-in-use``, naming the file: a
    replace never rules a reference out by not reading it."""
    try:
        names = sorted(n for n in os.listdir(sessions) if n.startswith("s-") and n.endswith(".json"))
    except FileNotFoundError:
        return frozenset(), frozenset()
    except OSError as e:
        raise Refused("routes-trust-in-use", f"{sessions} cannot be listed ({e.strerror or e}); nothing was "
                                             "replaced") from None
    if len(names) > REPLACE_SESSIONS_MAX:
        raise Refused("routes-trust-in-use", f"over {REPLACE_SESSIONS_MAX} session records in {sessions}; nothing "
                                             "was replaced")
    live = set()
    for name in names:
        try:
            data = R.read_record(sessions / name)
            if data is not None and _session_live(data, name[:-len(".json")]):
                live.add(name[:-len(".json")])
        except (Refused, OSError, ValueError, KeyError, TypeError) as e:
            raise Refused("routes-trust-in-use", f"session record {sessions / name} cannot be read whole "
                                                 f"({_why(e)}): fix or remove it; nothing was replaced") from None
    try:
        store = T.TicketStore(sessions / T.JOURNAL, sessions / "unused-ceiling")
        store._by_attempt, store._by_ticket = store._rebuild(store._read())
        return store.referenced_generations(live), store.referenced_verifier_generations()
    except (Refused, OSError, ValueError, KeyError, TypeError) as e:
        raise Refused("routes-trust-in-use", f"the ticket journal {sessions / T.JOURNAL} cannot be read whole "
                                             f"({_why(e)}); nothing was replaced") from None


def _why(e: BaseException) -> str:
    if isinstance(e, Refused):
        return f"{e.name} {e.detail}".strip()
    return f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"


@contextmanager
def _flock_held(path: Path, refusal: str, running: str, *, create_as: os.stat_result) -> Iterator[None]:
    """An exclusive ``flock`` on a service's single-instance lock file, taken non-blocking and held through the block:
    held elsewhere is ``refusal`` (``running`` says what to do), and a service starting while root holds it refuses its
    own start. A lock file that is not there is made 0600, owned as ``create_as`` says (the directory it is in), so
    the lock exists whether or not the service ever ran (verify r2 R1); the service opens it as its own after. A
    file the service makes between the look and the make (``EEXIST``) is opened and locked as one found. A file this
    makes is locked before it is owned; one that cannot be owned as ``create_as`` is removed, still locked, before
    the refusal (verify r2 R5). A made file is never removed once owned: a service that opened it before a removal
    and locked it after this closed would hold its lock on a file no longer in the directory."""
    made = False
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                made = True
            except FileExistsError:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise Refused("routes-trust-in-use", f"the lock {path}: {e.strerror or e}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(refusal, running) from None
        except OSError as e:
            raise Refused("routes-trust-in-use", f"the lock {path}: {e.strerror or e}") from None
        if made:
            try:
                have = os.fstat(fd)
                if (have.st_uid, have.st_gid) != (create_as.st_uid, create_as.st_gid):
                    os.fchown(fd, create_as.st_uid, create_as.st_gid)
            except OSError as e:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise Refused("routes-trust-in-use", f"the lock {path} cannot be owned as its directory is "
                                                     f"({e.strerror or e}); it is removed") from None
        yield
    finally:
        os.close(fd)                          # closing the only descriptor releases the flock


@contextmanager
def _services_held(L: Path) -> Iterator[None]:
    """CONTROL and keys held stopped through the block, in that order (L3 r1 Q2): CONTROL's single-CONTROL lock
    (:meth:`levain.slot.tickets.TicketStore.acquire`'s ``flock`` on ``L/sessions/tickets.jsonl.lock``;
    ``control-running``), then keys' ledger lock (:meth:`levain.broker.ledger.Ledger.open`'s ``flock`` on
    ``L/keys/ledger.jsonl``; ``keys-running``), so neither mints, reloads or commits a head against the old store
    between the reference check and the last write. A missing lock file is made as its service would make it, owned
    as its directory is (:func:`_flock_held`): the ledger made so is an empty file, which ``Ledger.open`` takes as a
    ledger with no lines and writes its header to. A missing ``L/sessions`` (L3 r1 Q7) is made root 0700 and no lock
    file is made in it: no CONTROL can hold or take a lock in a directory only root can enter, and setup's chown of
    the directory would not reach a root-owned 0600 lock file left in it [judged by lane-r3c9f, 10-26, against
    ``row_steps``' per-row chown; not run]. A missing ``L/keys`` takes no keys lock: keys' start opens its ledger
    before it makes any directory, and the open fails there
    (``tests/test_slot_routes_trust_replace.py::test_keys_refuses_to_start_with_no_keys_dir``)."""
    sessions = L / "sessions"
    made = False
    try:
        st = os.lstat(sessions)
    except FileNotFoundError:
        try:
            os.mkdir(sessions, 0o700)
            os.chmod(sessions, 0o700)
            st = os.lstat(sessions)
        except OSError as e:
            raise Refused("routes-trust-in-use", f"{sessions} is not there and cannot be made ({e.strerror or e})"
                          ) from None
        made = True
    except OSError as e:
        raise Refused("routes-trust-in-use", f"{sessions}: {e.strerror or e}") from None
    keys_dir = L / "keys"
    try:
        kst: Optional[os.stat_result] = os.lstat(keys_dir)
    except FileNotFoundError:
        kst = None
    except OSError as e:
        raise Refused("routes-trust-in-use", f"{keys_dir}: {e.strerror or e}") from None
    control = (contextlib.nullcontext() if made else
               _flock_held(sessions / (T.JOURNAL + ".lock"), "control-running", "CONTROL holds its ticket journal: "
                           "stop the control service, then run the replace again", create_as=st))
    keys = (contextlib.nullcontext() if kst is None else
            _flock_held(keys_dir / "ledger.jsonl", "keys-running", "keys holds its ledger: stop the keys service, "
                        "then run the replace again", create_as=kst))
    with control, keys:
        yield


def _aside_entries(L: Path) -> list[tuple[Path, Optional[os.stat_result]]]:
    """:data:`REPLACE_ASIDE` as ``(path, lstat or None when absent)``, checked before anything moves: an entry meant to
    be a directory that is another kind (a link included) is ``routes-trust-replace-layout``."""
    out = []
    for parts, is_dir in REPLACE_ASIDE:
        p = L.joinpath(*parts)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            out.append((p, None))
            continue
        if stat.S_ISDIR(st.st_mode) != is_dir or stat.S_ISLNK(st.st_mode):
            raise Refused("routes-trust-replace-layout", f"{p} is not the {'directory' if is_dir else 'file'} the "
                                                         "install made; nothing was replaced")
        out.append((p, st))
    return out


def _aside_path(routes: Path) -> Path:
    """A fresh ``L/routes/.replaced-<UTC time>[-n]`` name (not made)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside = routes / f".replaced-{stamp}"
    n = 1
    while os.path.lexists(aside):
        n += 1
        aside = routes / f".replaced-{stamp}-{n}"
    return aside


def _intent_of(aside: Path, entries: Sequence[tuple[Path, Optional[os.stat_result]]],
               old_pair: Mapping[str, Optional[bytes]], pair: Sequence[tuple[str, bytes]]) -> dict:
    import hashlib
    return {"format": REPLACE_INTENT_FORMAT, "aside": aside.name,
            "moved": [p.name for p, st in entries if st is not None],
            "old": {n: None if old_pair[n] is None else base64.b64encode(old_pair[n]).decode("ascii")
                    for n in _PAIR_NAMES},
            "new": {n: hashlib.sha256(d).hexdigest() for n, d in pair}}


def _read_intent(routes: Path) -> Optional[dict]:
    """The intent marker of a replace that did not finish, checked whole, or None when there is none. One that cannot
    be read or does not parse is ``routes-trust-replace-unfinished``: it is left for the operator, never guessed at."""
    marker = routes / ROUTES_REPLACE_INTENT
    try:
        raw = R.read_record(marker, max_bytes=_INTENT_MAX)
        if raw is None:
            return None
        obj = canon.parse(raw)
        ok = (type(obj) is dict and set(obj) == _INTENT_KEYS and obj["format"] == REPLACE_INTENT_FORMAT
              and type(obj["aside"]) is str and _ASIDE_NAME.fullmatch(obj["aside"]) is not None
              and type(obj["moved"]) is list and len(set(obj["moved"])) == len(obj["moved"])
              and all(type(m) is str and m in _ASIDE_BY_NAME for m in obj["moved"])
              and type(obj["old"]) is dict and set(obj["old"]) == set(_PAIR_NAMES)
              and type(obj["new"]) is dict and set(obj["new"]) == set(_PAIR_NAMES)
              and all(type(v) is str and _SHA256_HEX.fullmatch(v) for v in obj["new"].values())
              and type(obj["old"]["trust.json"]) is str
              and all(v is None or type(v) is str for v in obj["old"].values()))
        if ok:
            obj["old"] = {n: None if v is None else base64.b64decode(v, validate=True) for n, v in obj["old"].items()}
            return obj
        why = "it does not hold a replace's intent"
    except (Refused, OSError, ValueError, canon.ContractError) as e:
        why = _why(e)
    raise Refused("routes-trust-replace-unfinished", f"{marker} marks a replace that did not finish and cannot be read "
                                                     f"({why}): put L/routes and keys' head right by hand (any "
                                                     "L/routes/.replaced-* dir holds what was moved), then remove it")


def _set_aside(L: Path, routes: Path, aside: Path, entries: Sequence[tuple[Path, Optional[os.stat_result]]],
               old_pair: Mapping[str, Optional[bytes]]) -> None:
    """Q16's move, run with the intent marker already durable: ``aside`` made root 0700, a copy of the old pin's two
    files in it, then each :data:`REPLACE_ASIDE` entry renamed into it (a directory made again empty, with the old
    one's owner, group and mode), every directory touched fsynced. A failure part way is undone from the marker
    (:func:`_roll_back`)."""
    os.mkdir(aside, 0o700)
    os.chmod(aside, 0o700)
    for name, data in old_pair.items():
        if data is not None:
            R.write_durably(aside / name, data, mode=0o600, fsync=gen_mod._full_fsync)
    for p, st in entries:
        if st is None:
            continue
        os.rename(p, aside / p.name)
        if stat.S_ISDIR(st.st_mode):
            os.mkdir(p, 0o700)
            made = os.lstat(p)
            if (made.st_uid, made.st_gid) != (st.st_uid, st.st_gid):
                os.lchown(p, st.st_uid, st.st_gid)
            os.chmod(p, stat.S_IMODE(st.st_mode))
    for d in {aside, routes, *(p.parent for p, st in entries if st is not None)}:
        problem = _fsync_dir(d)
        if problem is not None:
            raise OSError(errno.EIO, problem)


def _roll_back(L: Path, routes: Path, intent: Mapping) -> tuple[list[str], Optional[Path]]:
    """Undo a replace from its intent marker alone, whatever point it reached (each step tests what is there, so it
    may run again after a crash of its own): the old pin's bytes put back, each moved entry renamed back (the empty
    directory the replace made in its place removed first), then every parent fsynced. Returns the steps that failed,
    and the aside dir when it is kept. Only when every step succeeded are the pin copies removed from the aside and
    the aside removed, and then only if empty (anything else in it keeps it, named); with any failure it is kept whole.
    The caller removes the marker only when nothing failed."""
    problems: list[str] = []
    aside = routes / intent["aside"]
    for name in _PAIR_NAMES:
        try:
            if R.read_record(routes / name, max_bytes=64 * 1024) != intent["old"][name]:
                _put_back(routes / name, intent["old"][name])
        except Exception as e:                                       # fail closed: every failure is named
            problems.append(f"put back {name}: {_why(e)}")
    for name in intent["moved"]:
        parts, is_dir = _ASIDE_BY_NAME[name]
        p, src = L.joinpath(*parts), aside / name
        try:
            if not os.path.lexists(src):
                if not os.path.lexists(p):
                    problems.append(f"{p} is neither in place nor in {aside}")
                continue                                             # never moved
            if os.path.lexists(p):
                if not is_dir:
                    problems.append(f"{p} and {src} are both there")
                    continue
                os.rmdir(p)                                          # the empty one the replace made
            os.rename(src, p)
        except Exception as e:
            problems.append(f"move {name} back: {_why(e)}")
    for d in sorted({routes, L / "keys"} | ({aside} if os.path.isdir(aside) else set())):
        if os.path.isdir(d):
            try:
                problem = _fsync_dir(d)
            except Exception as e:
                problem = f"{d} not synced ({_why(e)})"
            if problem is not None:
                problems.append(problem)
    if problems or not os.path.lexists(aside):
        return problems, aside if os.path.lexists(aside) else None
    for name in _PAIR_NAMES:
        copy = aside / name
        try:
            if R.read_record(copy, max_bytes=64 * 1024) == intent["old"][name]:
                os.unlink(copy)
            os.unlink(aside / (name + ".tmp"))                       # a copy's temp a kill left
        except FileNotFoundError:
            pass
        except Exception:
            pass                                                     # left in the aside, which is then kept
    try:
        os.rmdir(aside)
    except OSError:
        return problems, aside
    problem = _fsync_dir(routes)
    if problem is not None:
        problems.append(problem)
    return problems, None


def _replace_complete(L: Path, routes: Path, intent: Mapping) -> bool:
    """The replace the marker names reached its end: every entry it moved is in the aside with its place made again,
    and both files of the new pair hold the marker's digests, root's and 0644."""
    import hashlib
    aside = routes / intent["aside"]
    for name in intent["moved"]:
        parts, is_dir = _ASIDE_BY_NAME[name]
        if not os.path.lexists(aside / name):
            return False
        if is_dir:
            try:
                st = os.lstat(L.joinpath(*parts))
            except OSError:
                return False
            if not stat.S_ISDIR(st.st_mode):
                return False
    for name in _PAIR_NAMES:
        try:
            data = R.read_record(routes / name, max_bytes=64 * 1024)
        except (Refused, OSError):
            return False
        if data is None or hashlib.sha256(data).hexdigest() != intent["new"][name] \
                or not _trust_file_holds(routes / name, data):
            return False
    return True


def _finish_intent(routes: Path, warn: Callable[[str], object]) -> None:
    """The marker removed: the replace (or its undo) is whole. Its directory fsync failing only warns: a marker that
    comes back after a crash names a replace a rerun finds complete (or undone) and removes again."""
    os.unlink(routes / ROUTES_REPLACE_INTENT)
    problem = _fsync_dir(routes)
    if problem is not None:
        warn(f"levain install-helper: warning: {problem}\n")


def _recover(L: Path, routes: Path, intent: Mapping, warn: Callable[[str], object]) -> tuple[str, Optional[Path]]:
    """A rerun's first act when a replace did not finish (L3 r1 Q1): ``completed`` when it reached its end
    (:func:`_replace_complete`: only the marker is removed), else ``rolled-back`` (:func:`_roll_back`). Returns that
    and the aside dir still on disk (verify r2 R4): the completed replace's (it holds the old store), or a rolled-back
    one's that held more than it moved; None when the undo removed it. An undo with a failed step keeps the marker and
    refuses ``routes-trust-replace-unfinished``, naming the steps and the aside; so does a marker that cannot be
    removed after (verify r2 R4), naming it."""
    def finish(what: str, aside: Optional[Path]) -> tuple[str, Optional[Path]]:
        try:
            _finish_intent(routes, warn)
        except OSError as e:
            raise Refused("routes-trust-replace-unfinished", f"the replace that did not finish is {what}, but "
                                                             f"{routes / ROUTES_REPLACE_INTENT} could not be removed "
                                                             f"({e.strerror or e}): run the replace again"
                                                             + (f"; the old store is in {aside}" if aside else "")
                          ) from None
        return ("completed" if what == "finished" else "rolled-back"), aside

    if _replace_complete(L, routes, intent):
        return finish("finished", routes / intent["aside"])
    problems, kept = _roll_back(L, routes, intent)
    if problems:
        raise Refused("routes-trust-replace-unfinished", f"undoing the replace that did not finish FAILED "
                                                         f"({'; '.join(problems)}): {routes / ROUTES_REPLACE_INTENT} "
                                                         f"stays; what was moved is in {kept}")
    if kept is not None:
        warn(f"levain install-helper: the replace that did not finish is undone; {kept} held more than it moved and "
             "is kept\n")
    return finish("undone", kept)


def _replace_store(L: Path, routes: Path, pair: Sequence[tuple[str, bytes]], *, fsync: Callable[[int], None] | None,
                   warn: Callable[[str], object]) -> Path:
    """Q16's rotation, with CONTROL and keys held stopped (:func:`_services_held`): the reference check
    (``routes-trust-in-use``, nothing moved); the layout check (``routes-trust-replace-layout``); the intent marker
    written durably; the old store set aside (:func:`_set_aside`); the new pair written and read back; the marker
    removed. Any exception between the marker and the read-back is undone from the marker (:func:`_roll_back`) and,
    unless it is ``KeyboardInterrupt`` or ``SystemExit`` (re-raised after the undo), refused
    ``routes-trust-write-failed``; an undo with a failed step keeps the marker and the aside and names both. Returns the
    aside dir."""
    gens, vgens = _control_references(L / "sessions")
    if gens or vgens:
        held = [f"generation {g}" for g in sorted(gens)] + [f"verifier generation {g}" for g in sorted(vgens)]
        raise Refused("routes-trust-in-use", f"{', '.join(held)} of the pinned store is referenced by an open ticket "
                                             "or a live session; nothing was replaced (let them end or cancel the "
                                             "tickets, then replace)")
    old_pair = {name: R.read_record(routes / name, max_bytes=64 * 1024) for name in _PAIR_NAMES}
    entries = _aside_entries(L)
    aside = _aside_path(routes)
    intent = _intent_of(aside, entries, old_pair, pair)
    marker = routes / ROUTES_REPLACE_INTENT
    try:
        R.write_durably(marker, canon.canonical_bytes(intent), mode=0o600, fsync=gen_mod._full_fsync)
    except R.NotDurable:
        pass                                  # in place, perhaps not durable: undone below like any later failure
    except OSError as e:
        raise Refused("routes-trust-write-failed", f"the intent marker {marker}: {e.strerror or e}; nothing was "
                                                   "replaced") from None
    try:
        if R.read_record(marker, max_bytes=_INTENT_MAX) != canon.canonical_bytes(intent):
            raise OSError(errno.EIO, f"{marker} does not hold the intent when read back")
        problem = _fsync_dir(routes)                  # again: a NotDurable above is durable once this succeeds
        if problem is not None:
            raise OSError(errno.EIO, problem)
        _set_aside(L, routes, aside, entries, old_pair)
        _write_trust_pair(routes, pair, fsync=fsync, warn=warn)
    except BaseException as e:
        problems, kept = _roll_back(L, routes, dict(intent, old=old_pair))   # the old bytes, not their base64
        if not problems:
            try:
                _finish_intent(routes, warn)
            except Exception as u:
                problems.append(f"remove {marker}: {_why(u)}")
        what = e.detail if isinstance(e, Refused) else _why(e)
        if problems:
            raise Refused("routes-trust-write-failed", f"{what}; putting the old store back FAILED "
                                                       f"({'; '.join(problems)}): the old store is in {kept or aside}; "
                                                       f"{marker} stays, so CONTROL and keys refuse to start until a "
                                                       "replace finishes or undoes it") from None
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        raise Refused("routes-trust-write-failed", f"{what}; the old pin and store are back in place"
                                                   + (f" ({kept} is kept: it held more than was moved)" if kept
                                                      else "")) from None
    try:
        _finish_intent(routes, warn)
    except OSError as e:
        raise Refused("routes-trust-write-failed", f"the new pin is in force, but {marker} could not be removed "
                                                   f"({e.strerror or e}): run the replace again to finish") from None
    return aside


def run_routes_trust(caller_uid: int, host: HostOS, *, read: Callable[[int], bytes],
                     lock: Callable[[], ContextManager[object]], store_seams: RH.Seams | None = None,
                     err: TextIO | None = None, fsync: Callable[[int], None] | None = None,
                     fingerprint: Callable[[str], str] = key_fingerprint, replace: bool = False) -> dict[str, object]:
    """``routes-trust`` (Q15 r3): pin the dispatch trust root in ``L/routes``. Before the install lock: the operator
    check, the request (:func:`read_trust_request`) and ``ssh-keygen -l`` of its key naming exactly its fingerprint
    (``routes-trust-fingerprint`` otherwise). Root reads no authority log: the request comes from the operator's own
    ``levain dispatch config init``, which ran the may-sign-configuration test against it, and the password proves the
    operator. UNDER the lock: the operator check again; an existing ``trust.json`` naming another store or key is
    ``routes-trust-exists`` unless ``replace``; then ``trust.pub`` and LAST ``trust.json``
    (:func:`levain.slot.records.write_durably`, root 0644), so a reader finding both finds a matched pair. A file is
    left only when its bytes, owner and mode are already root's (:func:`_trust_file_holds`); any other is written
    again. A failure before ``trust.json``'s rename refuses ``routes-trust-write-failed`` (``trust.pub`` renamed but
    not durable included: ``trust.json`` is then not written); ``trust.json`` renamed but not durable answers ok and
    warns. Before the answer both files are read back and must hold the pair (``routes-trust-write-failed``).

    ``replace`` (Q16, ``routes-trust --replace``) rotates a pin to another store or key; with the pinned pair, or no
    pin, it is the plain verb. A rotation runs with CONTROL and keys held stopped (:func:`_services_held`;
    ``control-running`` / ``keys-running``) and is :func:`_replace_store`. A replace that did not finish leaves its
    intent marker (:data:`levain.slot.grammar.ROUTES_REPLACE_INTENT`): the plain verb then refuses
    ``routes-trust-replace-unfinished``, and ``replace`` first finishes or undoes it (:func:`_recover`, under the same
    holds), then goes on as above. The answer adds ``replaced`` (the old ``{store_id, fingerprint}``, None for each the
    old ``trust.json`` did not parse to; None when nothing was replaced), ``aside`` (the set-aside dir, or None) and
    ``recovered`` (``completed`` / ``rolled-back`` for a replace that did not finish, else None) and
    ``recovered_aside`` (the aside dir that replace left on disk, :func:`_recover`; else None)."""
    import hashlib

    rs = store_seams or RH.Seams(host=host)
    _check_operator("routes-trust", caller_uid, rs)
    req = read_trust_request(read)
    if fingerprint(req["public_key"]) != req["fingerprint"]:
        raise Refused("routes-trust-fingerprint", "the key does not have the request's fingerprint")
    trust = {"format": "levain-routes-trust/1", "store_id": req["store_id"], "fingerprint": req["fingerprint"]}
    routes = _routes_dir(rs, host)
    pub = (req["public_key"] + "\n").encode("ascii")
    pair = (("trust.pub", pub), ("trust.json", canon.canonical_bytes(trust)))
    warn = (err or sys.stderr).write
    replaced: Optional[dict] = None
    aside: Optional[Path] = None
    recovered: Optional[str] = None
    recovered_aside: Optional[Path] = None

    def pinned() -> Optional[dict]:
        try:
            have = _trust_on_disk(routes)
        except Refused:
            if not replace:
                raise
            return {"store_id": None, "fingerprint": None}         # does not parse: set aside like any other pin
        if have is not None and have != trust and not replace:
            raise Refused("routes-trust-exists", f"pinned to store {str(have.get('store_id'))[:32]} and key "
                                                 f"{str(have.get('fingerprint'))[:51]}; rotation is --replace's")
        return have

    with lock():
        _check_operator("routes-trust", caller_uid, rs)
        intent = _read_intent(routes)
        if intent is not None and not replace:
            raise Refused("routes-trust-replace-unfinished", f"{routes / ROUTES_REPLACE_INTENT} marks a replace that "
                                                             "did not finish: run routes-trust --replace")
        have = pinned() if intent is None else None
        if intent is None and (have is None or have == trust):
            _write_trust_pair(routes, pair, fsync=fsync, warn=warn)
        else:
            with _services_held(rs.L):
                if intent is not None:
                    recovered, recovered_aside = _recover(rs.L, routes, intent, warn)
                    have = pinned()
                if have is None or have == trust:
                    _write_trust_pair(routes, pair, fsync=fsync, warn=warn)
                else:
                    replaced = {"store_id": have.get("store_id"), "fingerprint": have.get("fingerprint")}
                    aside = _replace_store(rs.L, routes, pair, fsync=fsync, warn=warn)
    out: dict[str, object] = {"verb": "routes-trust", "store_id": req["store_id"], "fingerprint": req["fingerprint"],
                              "digest": "sha256:" + hashlib.sha256(pub).hexdigest()}
    if replace:
        out.update(replaced=replaced, aside=None if aside is None else str(aside), recovered=recovered,
                   recovered_aside=None if recovered_aside is None else str(recovered_aside))
    return out


def _write_trust_pair(routes: Path, pair: Sequence[tuple[str, bytes]], *, fsync: Callable[[int], None] | None,
                      warn: Callable[[str], object]) -> None:
    """:func:`run_routes_trust`'s write: each of ``pair`` in order, unless it already holds (:func:`_trust_file_holds`),
    then both read back. A ``trust.json`` renamed but not durable is in force, so it warns and answers."""
    for name, data in pair:
        if _trust_file_holds(routes / name, data):
            continue
        try:
            R.write_durably(routes / name, data, mode=0o644, fsync=fsync or gen_mod._full_fsync)
        except R.NotDurable as e:
            if name == "trust.pub":
                raise Refused("routes-trust-write-failed", f"trust.pub is in place but not durable "
                                                           f"({e.strerror}); trust.json was not written") from None
            warn(f"levain install-helper: warning: trust.json written but not durable ({e.strerror}): it is in "
                 "force now and may not survive a crash; run it again\n")
        except OSError as e:
            raise Refused("routes-trust-write-failed", f"{name}: {e.strerror or e}") from None
    for name, data in pair:
        if not _trust_file_holds(routes / name, data):
            raise Refused("routes-trust-write-failed", f"{name} does not hold the requested pair when read back "
                                                       "(bytes, root's owner, 0644)")


def _put_back(path: Path, data: Optional[bytes]) -> None:
    """A replace's put-back of one pin file: its old bytes, root 0644, or no file when there was none (the caller
    fsyncs the directory)."""
    if data is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    R.write_durably(path, data, mode=0o644, fsync=gen_mod._full_fsync)


# --- enrol-authority-signer / remove-authority-signer (14b DESIGN_REV2 S5, DESIGN_REV3 §7) --------------------------

SIGNER_VERBS = ("enrol-authority-signer", "remove-authority-signer")
#: The confirm gate's namespace (``levain.autonomic.confirm.NAMESPACE``; a test holds the two equal). Root imports
#: neither ``levain.authority`` nor ``levain.autonomic`` (``tests/test_ci_root_closure.py``), so the custody rule below
#: is written here too, held equal to ``levain.authority.custody_problem`` by the tests.
CONFIRM_NAMESPACE = "levain-confirm"
#: The one key type a WebAuthn passkey is (``levain.authority``'s custody table entry for ``passkey``).
PASSKEY_KEY_TYPE = "sk-ecdsa-sha2-nistp256@openssh.com"
#: DECISION: the most root's allowed-signers file may hold for these verbs to read it.
AUTHORITY_SIGNERS_MAX = 256 * 1024
_BLOB_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def is_passkey_material(public_key: str) -> bool:
    """``levain.authority.is_passkey_material``, written here (root imports no authority store; a test holds the two
    equal): a :data:`PASSKEY_KEY_TYPE` key whose application is a web rpId, where an OpenSSH FIDO key's is ``ssh:``."""
    import base64
    import struct

    parts = public_key.split()
    if len(parts) < 2 or parts[0] != PASSKEY_KEY_TYPE:
        return False
    try:
        wire = _Wire(base64.b64decode(parts[1], validate=True))
        if wire.string() != PASSKEY_KEY_TYPE.encode("ascii"):
            return False
        wire.string()                  # curve
        wire.string()                  # point
        application = wire.string()
        wire.end()
    except (ValueError, IndexError, struct.error):
        return False
    return not application.startswith(b"ssh:")


def authority_key_problem(public_key: str, *, passkey: bool) -> str | None:
    """Why ``public_key`` may not be enrolled with root, or None: the authority log's own custody rule
    (``levain.authority.custody_problem``; the tests hold this equal to it), so root lists no key the log would refuse
    to enrol. The laptop key (``passkey`` False): a type some stated laptop-presence custody admits
    (:data:`TRUST_KEY_TYPES`, the custody table's union) and not a passkey's material. A ``--passkey``: a
    :data:`PASSKEY_KEY_TYPE` key whose material is a passkey's."""
    key_type = public_key.split()[0]
    if passkey:
        if not is_passkey_material(public_key):
            return f"not a WebAuthn passkey (a {PASSKEY_KEY_TYPE} key made on a web origin)"
        return None
    if is_passkey_material(public_key):
        return "the key is a WebAuthn passkey: name it with --passkey, never as the laptop key"
    if key_type not in TRUST_KEY_TYPES:
        return f"no laptop-presence custody takes a {key_type} key (one of {', '.join(sorted(TRUST_KEY_TYPES))})"
    return None


def blob_fingerprint(public_key: str) -> str | None:
    """OpenSSH's ``SHA256:`` fingerprint of a ``<type> <base64>`` key: the unpadded base64 of the sha256 of the key
    blob (what ``ssh-keygen -l -E sha256`` prints; :func:`run_authority_signer` refuses an enrolment where the two
    differ). None when the base64 does not decode. Used to find a key's lines in root's file without running
    ssh-keygen on lines root may have written by hand."""
    import base64
    import hashlib

    try:
        blob = base64.b64decode(public_key.split()[1], validate=True)
    except (ValueError, IndexError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _quoted_split(text: str, sep: str | None) -> list[str] | None:
    """``text`` split outside double quotes, on whitespace runs (``sep`` None) or on ``sep`` (empty entries kept);
    None for an unterminated quote."""
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif not quoted and (ch.isspace() if sep is None else ch == sep):
            if cur or sep is not None:
                out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if quoted:
        return None
    if cur or sep is not None:
        out.append("".join(cur))
    return out


def _signer_fields(raw: bytes) -> tuple[str, str | None, str] | None:
    """A line of root's file in the allowed-signers structure: ``(principals, options or None, "<type> <base64>")``,
    the principals unquoted; None for a blank or ``#`` line, or one that does not parse. Only the key field is the
    key: a word after it is the line's comment and is never read, so a key named in a comment is no line's key.
    ``raw`` is UTF-8 (:func:`_read_signers` refuses a file that is not)."""
    text = raw.decode("utf-8").strip()
    if not text or text.startswith("#"):
        return None
    parts = _quoted_split(text, None)
    if parts is None or len(parts) < 3:
        return None
    principals, rest = parts[0], parts[1:]
    if len(principals) >= 2 and principals[0] == principals[-1] == '"':
        principals = principals[1:-1]
    options = None
    if rest[0] not in KEY_TYPE_NAMES:
        options, rest = rest[0], rest[1:]
    if len(rest) < 2 or rest[0] not in KEY_TYPE_NAMES or not _BLOB_RE.fullmatch(rest[1]):
        return None
    return principals, options, f"{rest[0]} {rest[1]}"


def _line_keys(raw: bytes) -> tuple[str, ...]:
    """The key a line of root's file names (:func:`_signer_fields`), or none. A line with options the coverage reader
    refuses (one ``ssh-keygen -Y verify`` may still honour) names its key too."""
    fields = _signer_fields(raw)
    return () if fields is None else (fields[2],)


def _line_namespaces(options: str | None) -> frozenset[str] | None:
    """The namespaces a line's options restrict it to, when they are exactly one ``namespaces="a,b"``; None for no
    options (every namespace) or any other option."""
    opts = None if options is None else _quoted_split(options, ",")
    if opts is None or len(opts) != 1:
        return None
    name, sep, value = opts[0].partition("=")
    if not sep or name.lower() != "namespaces" or len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return None
    entries = value[1:-1].split(",")
    return frozenset(entries) if all(entries) and '"' not in value[1:-1] else None


def _signers_owner() -> int:
    """Who root's file and its directory must be owned by: the helper's euid (root on a host)."""
    return os.geteuid()


def _read_signers(path: Path, *, owner: int | None = None) -> bytes | None:
    """Root's allowed-signers file, None when absent; read only when root wrote it and only whole. The descriptor
    read is the one checked: a regular file (``authority-signers-unreadable`` otherwise), owned by
    :func:`_signers_owner` and writable by neither group nor others, in a directory of the same
    (``authority-signers-unsafe``: a line another account could have written is never re-written as root's); at most
    :data:`AUTHORITY_SIGNERS_MAX` bytes and UTF-8 throughout (``authority-signers-unreadable``: never taken as empty,
    and ``remove`` never misses a line it cannot read). A link there is ``bad-root``. ``owner`` is who the file and its
    directory must be owned by (default :func:`_signers_owner`); a caller that is not root (``levain doctor``) names the
    uid its own trust-anchor check has already accepted."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("bad-root", f"{path} is a link") from None
        raise Refused("authority-signers-unreadable", f"{path}: {e.strerror or e}; nothing written") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("authority-signers-unreadable", f"{path} is not a regular file; nothing written")
        owner = _signers_owner() if owner is None else owner
        if st.st_uid != owner or st.st_mode & 0o022:
            raise Refused("authority-signers-unsafe", f"{path} is owned by uid {st.st_uid} at "
                                                      f"{stat.S_IMODE(st.st_mode):04o}: not root's alone; nothing "
                                                      "written, its lines are not re-written as root's")
        try:
            dst = os.lstat(path.parent)
        except OSError as e:
            raise Refused("authority-signers-unreadable", f"{path.parent}: {e.strerror or e}") from None
        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != owner or dst.st_mode & 0o022:
            raise Refused("authority-signers-unsafe", f"{path.parent} is not a directory root alone may write; "
                                                      "nothing written")
        if st.st_size > AUTHORITY_SIGNERS_MAX:
            raise Refused("authority-signers-unreadable", f"{path} is past {AUTHORITY_SIGNERS_MAX} bytes; nothing "
                                                          "written")
        raw = b""
        try:
            while len(raw) <= AUTHORITY_SIGNERS_MAX:
                chunk = os.read(fd, AUTHORITY_SIGNERS_MAX + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError as e:
            raise Refused("authority-signers-unreadable", f"{path}: {e.strerror or e}; nothing written") from None
    finally:
        os.close(fd)
    if len(raw) > AUTHORITY_SIGNERS_MAX:
        raise Refused("authority-signers-unreadable", f"{path} is past {AUTHORITY_SIGNERS_MAX} bytes; nothing written")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("authority-signers-unreadable", f"{path} has a line that is not UTF-8; nothing written") from None
    return raw


def _signer_lines(raw: bytes | None) -> list[bytes]:
    if not raw:
        return []
    lines = raw.split(b"\n")
    return lines[:-1] if lines[-1] == b"" else lines


def _signers_bytes(lines: Sequence[bytes]) -> bytes:
    return b"".join(line + b"\n" for line in lines)


def _signer_text(principal: str, namespaces: Sequence[str], key: str) -> bytes:
    return f'{principal} namespaces="{",".join(namespaces)}" {key}'.encode("ascii")


def _signers_path(rs: RH.Seams, host: HostOS) -> Path:
    """Root's allowed-signers file under the store the seams name (STORE on a host, a test's scratch L otherwise)."""
    return rs.L / root_tree.authority_signers_path(host).relative_to(STORE[host])


def _write_signers(path: Path, data: bytes, err: TextIO | None, fsync: Callable[[int], None] | None) -> bool:
    """``data`` at ``path``, root 0644, unless it already holds it (:func:`_trust_file_holds`); True when written. A
    failure before the rename refuses ``authority-signers-write-failed`` with the earlier file in force; a rename
    whose directory fsync failed answers (True) and warns on ``err``. Read back before the answer."""
    if _trust_file_holds(path, data, max_bytes=AUTHORITY_SIGNERS_MAX):
        return False
    try:
        R.write_durably(path, data, mode=0o644, fsync=fsync or gen_mod._full_fsync)
    except R.NotDurable as e:
        (err or sys.stderr).write(f"levain install-helper: warning: {path} written but not durable ({e.strerror}): "
                                  "it is in force now and may not survive a crash; run it again\n")
    except OSError as e:
        raise Refused("authority-signers-write-failed", f"{path}: {e.strerror or e}; the earlier file stands") from None
    if not _trust_file_holds(path, data, max_bytes=AUTHORITY_SIGNERS_MAX):
        raise Refused("authority-signers-write-failed", f"{path} does not hold the new lines when read back "
                                                        "(bytes, root's owner, 0644)")
    return True


def run_authority_signer(cmd: InstallCommand, caller_uid: int, host: HostOS, *,
                         lock: Callable[[], ContextManager[object]], store_seams: RH.Seams | None = None,
                         err: TextIO | None = None, fsync: Callable[[int], None] | None = None,
                         fingerprint: Callable[[str], str] = functools.partial(
                             key_fingerprint, refusal="authority-signer-verifier")) -> dict[str, object]:
    """``enrol-authority-signer`` / ``remove-authority-signer`` (14b DESIGN_REV2 S5, DESIGN_REV3 §7): root's
    allowed-signers file (:func:`levain.slot.root_tree.authority_signers_path`): a line per key in namespace
    ``levain-authority`` (a graduation policy covers only when its signers are listed there), the laptop key's line
    naming ``levain-confirm`` too with ``--confirm`` (the namespace of a person's signed approval).

    Root reads no operator-uid file: every key comes from argv, typed at the operator's terminal under one ``sudo``.
    Before the install lock: the operator check, each key held to the authority log's custody rule
    (:func:`authority_key_problem`, ``authority-signer-key``) and fingerprinted by ``ssh-keygen -l``
    (``authority-signer-verifier`` when it cannot read the key, or prints another fingerprint than
    :func:`blob_fingerprint`'s, the one ``remove`` finds lines by). UNDER the lock: the operator check again (an undo
    between the two is seen), then one read-modify-write of the file, read only when it is root's alone, whole and
    UTF-8 (:func:`_read_signers`). A line's key is its key field alone (:func:`_signer_fields`). ``enrol`` writes one
    line per key: a key's earlier line is replaced where it stood and any further line naming that key dropped, a key
    with no line is appended in argv order (the laptop key's first), and every other line is kept as root wrote it.
    A line for one of the keys whose principal is not ``--principal``, or whose options are anything but a
    ``namespaces=`` this enrolment names all of, refuses ``authority-signer-changed`` (remove it first); a file that
    would pass :data:`AUTHORITY_SIGNERS_MAX` refuses ``authority-signers-too-large`` before any write. ``remove``
    drops every line naming the key whose fingerprint ``--key`` gives (a narrowing; the file stays, listing what is
    left; no file stays none, and its ``digest`` is None). Nothing is written when the bytes, root's owner and 0644
    already hold (:func:`_write_signers`); ``digest`` is then the file's own. The answer names each fingerprint, also
    told on ``err`` to compare with ``levain cockpit keys``."""
    import hashlib

    errw = err or sys.stderr
    rs = store_seams or RH.Seams(host=host)
    _check_operator(cmd.verb, caller_uid, rs)
    path = _signers_path(rs, host)
    if cmd.verb == "remove-authority-signer":
        target = cmd.key_fingerprint
        with lock():
            _check_operator(cmd.verb, caller_uid, rs)
            old = _read_signers(path)
            lines = _signer_lines(old)
            kept = [line for line in lines if not any(blob_fingerprint(k) == target for k in _line_keys(line))]
            written = old is not None and len(kept) != len(lines) and _write_signers(path, _signers_bytes(kept),
                                                                                       err, fsync)
        removed = len(lines) - len(kept)
        errw.write(f"levain install-helper: {target}: {removed} line(s) removed from {path}\n")
        return {"verb": cmd.verb, "fingerprint": target, "removed": removed, "written": bool(written),
                "digest": None if old is None else
                "sha256:" + hashlib.sha256(_signers_bytes(kept) if written else old).hexdigest()}

    if cmd.public_key is None or cmd.principal is None:
        raise Refused("missing-argv", f"{cmd.verb} takes --principal and --public-key")
    wanted: list[tuple[str, tuple[str, ...], str]] = []      # (key, namespaces, fingerprint), the laptop key first
    for key, passkey in ((cmd.public_key, False), *((k, True) for k in cmd.passkeys)):
        problem = authority_key_problem(key, passkey=passkey)
        if problem is not None:
            raise Refused("authority-signer-key", f"{'--passkey' if passkey else '--public-key'}: {problem}")
        fp = fingerprint(key)
        if fp != blob_fingerprint(key):
            raise Refused("authority-signer-verifier", f"ssh-keygen names {fp} for a key whose blob hashes to "
                                                       f"{blob_fingerprint(key)}")
        ns = (AUTHORITY_NAMESPACE, CONFIRM_NAMESPACE) if cmd.confirm and not passkey else (AUTHORITY_NAMESPACE,)
        wanted.append((key, ns, fp))
    new = {key: _signer_text(cmd.principal, ns, key) for key, ns, _ in wanted}
    namespaces = {key: ns for key, ns, _ in wanted}
    with lock():
        _check_operator(cmd.verb, caller_uid, rs)
        out: list[bytes] = []
        placed: set[str] = set()
        for n, line in enumerate(_signer_lines(_read_signers(path)), 1):
            fields = _signer_fields(line)
            if fields is None or fields[2] not in new:
                out.append(line)
                continue
            principals, options, k = fields
            had = _line_namespaces(options)
            if principals != cmd.principal or had is None or not had <= set(namespaces[k]):
                raise Refused("authority-signer-changed",
                              f"line {n} lists {blob_fingerprint(k)} for {principals[:128]} with "
                              f"{(options or 'every namespace')[:256]}; this enrolment would not keep that: "
                              "remove-authority-signer it first, then enrol")
            if k not in placed:
                out.append(new[k])
                placed.add(k)
        out.extend(new[k] for k, _, _ in wanted if k not in placed)
        data = _signers_bytes(out)
        if len(data) > AUTHORITY_SIGNERS_MAX:
            raise Refused("authority-signers-too-large", f"{path} would be past {AUTHORITY_SIGNERS_MAX} bytes; the "
                                                         "earlier file stands")
        written = _write_signers(path, data, err, fsync)
    for _, ns, fp in wanted:
        errw.write(f"levain install-helper: {fp} enrolled in {path} for {','.join(ns)}; compare it with "
                   "`levain cockpit keys`\n")
    return {"verb": cmd.verb, "signers": [{"fingerprint": fp, "namespaces": ",".join(ns)} for _, ns, fp in wanted],
            "written": written, "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


# --- repair's handover: the probe (R3a), the exec (R4), phase B (R4a on) (row 33 §2.2) ---------------------------

#: R3a's bounds [r3: X2-M5]: the probe child's wall clock and the most it may print.
PROBE_TIMEOUT_S = 60.0
PROBE_OUT_CAP = 64 * 1024
#: The probe program's own exits (phase A writes that program, so they mean the same in every generation): the
#: staged code did not import, or it imported but lacks the handover (any package from before row 33), or its
#: ``repair_probe`` could not read the host's records.
PROBE_EXIT_NO_IMPORT = 3
PROBE_EXIT_UNSUPPORTED = 4
#: [r5: L3 r2 codex] the staged ``repair_continue`` cannot bind the call :func:`phase_b_text` makes (phase A's own
#: program checks it, so a self-reported ABI is never the only evidence).
PROBE_EXIT_HANDOVER = 5
#: [r5: L3 r2 codex MED] the staged code's own bounds refuse a record or a journal identity (``bad-record``).
PROBE_EXIT_BAD_RECORD = 6
#: The handover's ABI [r5: codex MED 3]: what phase A's exec passes to ``repair_continue`` (the call shape of
#: :func:`phase_b_text`) and what phase B adopts. The probe answers it; phase A execs only into its own.
#: [r6: L3 s3 r1 codex HIGH, bounded] It also freezes the probe's wire: the request's keys (:func:`run_probe`'s
#: ``journal``, ``operator``, ``shape``) and the reply's (:func:`repair_probe`'s ``handover``, ``kinds``, ``ok``). ANY
#: change to either key set bumps this number, so a probe across versions refuses ``downgrade-unsupported:handover``
#: (the designed path) and never needs a second schema; ``test_the_probe_wire_is_frozen_at_handover_abi_1`` pins both.
HANDOVER_ABI = 1


def _handover_args(lock_fd: int, pool_fd: int, gen: str, caller_uid: int,
                   fault: Optional[Tuple[str, str]] = None) -> str:
    """The argument list of phase A's call into ``repair_continue``: the exec's (:func:`phase_b_text`) and the probe's
    ``inspect.signature(...).bind`` of it (:func:`probe_text`) are this one string. ``fault`` [F7]: the launcher's,
    as a literal (checked by :func:`check_fault` first); without one the text names no fault at all."""
    at = check_fault(fault)
    return f"{lock_fd}, {pool_fd}, {gen!r}, caller_uid={caller_uid}" + (f", fault={at!r}" if at else "")


def probe_text(host: HostOS, gen: str, *, tree: Path | None = None, fault: Optional[Tuple[str, str]] = None) -> str:
    """The R3a probe program for ``gen``: its code on ``sys.path`` (:func:`root_tree.generation_boot_code`), then
    :func:`repair_probe` if the staged package has the handover and its ``repair_continue`` binds the exact call
    phase B's program makes (:func:`_handover_args`). Written by phase A, so every exit code but ``repair_probe``'s
    own is phase A's."""
    return (f"{root_tree.generation_boot_code(host, gen, tree=tree)}\n"
            "try:\n"
            "    from levain.slot import install_helper as M\n"
            "except BaseException:\n"
            f"    raise SystemExit({PROBE_EXIT_NO_IMPORT})\n"
            "if not (callable(getattr(M, 'repair_probe', None)) and callable(getattr(M, 'repair_continue', None))):\n"
            f"    raise SystemExit({PROBE_EXIT_UNSUPPORTED})\n"
            "import inspect\n"
            "try:\n"
            f"    inspect.signature(M.repair_continue).bind({_handover_args(0, 0, gen, 0, fault)})\n"
            "except (TypeError, ValueError):\n"
            f"    raise SystemExit({PROBE_EXIT_HANDOVER})\n"
            "raise SystemExit(M.repair_probe())\n")


def repair_probe(*, stdin=None, out: TextIO | None = None, host: HostOS | None = None) -> int:
    """R3a, run by the STAGED generation in a child (§2.2 [r3: X2-M5, X2-H1]): stdin holds the host's
    ``install-shape`` and journal texts and the operator's name (``{"journal": <text|null>, "operator": <name|null>,
    "shape": <text|null>}``, canonical). Exactly one canonical ``{"handover":<HANDOVER_ABI>,"kinds":[...],"ok":true}``
    line on ``out`` (the kinds this code can REMOVE, so the caller can check every recorded and journalled one, and
    the handover ABI it takes) and 0; :data:`PROBE_EXIT_UNSUPPORTED` with nothing printed when either record does
    not parse here; [r5: L3 r2 codex MED] :data:`PROBE_EXIT_BAD_RECORD` when a record of a kind this code holds fails
    its bound (:func:`generation.check_managed`, the check phase B's plan runs first). A kind it lacks is left to the
    caller's ``kinds`` check, which names it."""
    host = host or _host()
    raw = (stdin if stdin is not None else sys.stdin.buffer).read(4 * gen_mod.MAX_RECORD_BYTES + 1)
    try:
        records = canon.parse(raw)
        if not isinstance(records, dict) or set(records) != {"journal", "operator", "shape"}:
            return PROBE_EXIT_UNSUPPORTED
        operator = records["operator"]
        if operator is not None and not (isinstance(operator, str) and OPERATOR_RE.fullmatch(operator)):
            return PROBE_EXIT_UNSUPPORTED
        shape = gen_mod.parse_shape(str(records["shape"]).encode()) if records["shape"] is not None else None
        journal = parse_intent(str(records["journal"]).encode(), host) if records["journal"] is not None else None
    except (Refused, canon.ContractError, TypeError, ValueError):
        return PROBE_EXIT_UNSUPPORTED
    held = [r for r in (*(shape.managed if shape else ()), *(journal.managed if journal else ())) if r[0] in gen_mod.KINDS]
    try:
        gen_mod.check_managed(host, held, operator=operator)
    except Refused:
        return PROBE_EXIT_BAD_RECORD
    o = out or sys.stdout
    o.write(canon.canonical({"ok": True, "kinds": sorted(gen_mod.KINDS), "handover": HANDOVER_ABI}) + "\n")
    o.flush()
    return 0


def _bounded_child(argv: Sequence[str], data: bytes, *, timeout: float, cap: int,
                   clock: Callable[[], float] = time.monotonic) -> tuple[int | None, bytes]:
    """``argv`` with ``data`` on stdin (from an unlinked temp file, so a child that never reads cannot wedge the
    write), stdout read to at most ``cap`` + 1 bytes before ``timeout`` (stderr discarded, ``close_fds``, its own
    process group, cwd ``/``). ``(None, out)`` when it ran out of time or printed past the cap. The group is killed
    on EVERY exit [r5: L3 r1 complement LOW 7], so nothing the child left in it outlives the call (a process that made
    its own session escapes that; the probe is the staged code phase A is about to exec into anyway)."""
    import tempfile

    with tempfile.TemporaryFile() as fin:
        fin.write(data)
        fin.flush()
        fin.seek(0)
        p = subprocess.Popen(list(argv), stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             close_fds=True, cwd="/", env=child_env(), start_new_session=True)
    assert p.stdout is not None
    out, deadline, over = b"", clock() + timeout, False
    fd = p.stdout.fileno()
    sel = None
    try:
        # [r6: L3 s3 r1 complement LOW] made inside the try, so a register that raises (EMFILE) still kills the group
        sel = selectors.DefaultSelector()        # [r5: L3 r2 OBS] select.select refuses an fd >= FD_SETSIZE
        sel.register(fd, selectors.EVENT_READ)
        while True:
            left = deadline - clock()
            if left <= 0:
                over = True
                break
            if not sel.select(left):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            out += chunk
            if len(out) > cap:
                over = True
                break
    except BaseException:
        over = True                              # the group killed now, not after the wait's grace
        raise
    finally:
        if sel is not None:
            sel.close()
        p.stdout.close()
        if over:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            rc = p.wait(timeout=max(0.0, deadline - clock()) + 5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            rc, over = p.wait(), True
        # After the leader is reaped its pid cannot be reused while any member keeps the group alive.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return (None, out) if over else (rc, out)


def run_probe(host: HostOS, gen: str, shape: gen_mod.InstallShape | None, journal: Intent | None, *,
              python: str | None = None, tree: Path | None = None, timeout: float = PROBE_TIMEOUT_S,
              cap: int = PROBE_OUT_CAP, fault: Optional[Tuple[str, str]] = None) -> tuple[str, ...]:
    """R3a in phase A: the staged ``gen`` asked, in a bounded child (:func:`_bounded_child`), whether it can take this
    host over. Returns its ``kinds``. A timeout, an exit other than 0, or anything but exactly one canonical
    ``{"handover":<n>,"kinds":[...],"ok":true}`` line (``handover`` may be absent), or a probe that cannot be
    started, refuses ``source-broken``; a package with no handover (from before row 33) or one that cannot read the
    records refuses ``downgrade-unsupported``; an ABI other than :data:`HANDOVER_ABI` (or none), or a
    ``repair_continue`` that cannot bind the exec's call, refuses ``downgrade-unsupported:handover``; a record the
    staged code's bounds refuse, ``bad-record``; a kind in the record's ``managed`` or the journal's write-ahead ``managed``
    that it cannot remove refuses ``downgrade-unsupported:<kind>``."""
    python = python or root_tree.ROOT_PYTHON[host]
    data = canon.canonical({"shape": gen_mod.shape_text(shape) if shape is not None else None,
                            "journal": intent_text(journal) if journal is not None else None,
                            "operator": journal.operator if journal is not None else None}).encode()
    try:
        rc, out = _bounded_child([python, root_tree.PY_FLAGS, "-c", probe_text(host, gen, tree=tree, fault=fault)], data,
                                 timeout=timeout, cap=cap)
    except OSError as e:                  # [r5: both seats MED] e.g. the interpreter gone, EMFILE
        raise Refused("source-broken", f"the probe of {gen} could not start ({e.strerror or e})") from None
    if rc == PROBE_EXIT_UNSUPPORTED:
        raise Refused("downgrade-unsupported", f"{gen} cannot take this host over (it has no repair handover, or "
                                               "it cannot read the install record or the journal)")
    if rc == PROBE_EXIT_HANDOVER:
        raise Refused("downgrade-unsupported:handover", f"{gen}'s repair_continue does not take the call this "
                                                        "code's exec makes")
    if rc == PROBE_EXIT_BAD_RECORD:
        raise Refused("bad-record", f"{gen} finds a record of the install record or the journal outside its "
                                    "kind's bound")
    if rc is None:
        raise Refused("source-broken", f"the probe of {gen} ran past {timeout:g} s or printed past {cap} bytes")
    if rc != 0:
        raise Refused("source-broken", f"the probe of {gen} exited {rc} (its code does not import)")
    try:
        rec = canon.parse(out[:-1]) if out.endswith(b"\n") else None
    except canon.ContractError:
        rec = None
    shaped = isinstance(rec, dict) and set(rec) - {"handover"} == {"ok", "kinds"} and rec["ok"] is True
    kinds = rec.get("kinds") if shaped else None
    if (not isinstance(kinds, list) or not all(isinstance(k, str) and gen_mod.KIND_RE.fullmatch(k) for k in kinds)
            or out != (canon.canonical(rec) + "\n").encode()):
        raise Refused("source-broken", f"the probe of {gen} answered {out[:120]!r}, not one canonical record")
    abi = rec.get("handover")
    if type(abi) is not int or abi != HANDOVER_ABI:
        raise Refused("downgrade-unsupported:handover", f"{gen} takes handover ABI {abi!r}; this code execs "
                                                        f"{HANDOVER_ABI}")
    held = set(kinds)
    for kind in sorted({k for k, _ in (*(shape.managed if shape else ()), *(journal.managed if journal else ()))}):
        if kind not in held:
            raise Refused(f"downgrade-unsupported:{kind}", f"{gen} holds no remove step for {kind}, which this host "
                                                           "records")
    return tuple(kinds)


def phase_b_text(host: HostOS, gen: str, lock_fd: int, pool_fd: int, caller_uid: int, *, tree: Path | None = None,
                 entry: str | None = None, fault: Optional[Tuple[str, str]] = None) -> str:
    """The R4 program: ``gen``'s code on ``sys.path``, then :func:`repair_continue` with the two inherited lock
    descriptors (and the launcher's ``fault`` as a literal [F7], only when it set one). Every value in it is phase A's
    own (ints, a ``GEN_RE`` id, a checked fault): nothing the sudoers rule passes reaches it, so the grammar gains no
    hidden verb. ``entry``: a test's own callable instead of ``M.repair_continue``, called with the same arguments
    (it still runs in ``gen``'s code)."""
    if not all(type(x) is int and x >= 0 for x in (lock_fd, pool_fd, caller_uid)):
        raise Refused("bad-token", "phase B's descriptors and caller")
    return (f"{root_tree.generation_boot_code(host, gen, tree=tree)}\n"
            "from levain.slot import install_helper as M\n"
            f"raise SystemExit({entry or 'M.repair_continue'}({_handover_args(lock_fd, pool_fd, gen, caller_uid, fault)}))\n")


def exec_phase_b(host: HostOS, gen: str, lock_fd: int, pool_fd: int, caller_uid: int, *, python: str | None = None,
                 tree: Path | None = None, entry: str | None = None, fault: Optional[Tuple[str, str]] = None) -> None:
    """R4 [r2: X-4]: both lock descriptors made inheritable (a flock belongs to the open file description, so it
    survives the exec), then ``execve`` into ``gen`` with :func:`levain.launch.child_env` (never this process's whole
    environment): same PID, so sudo still waits on it. Returns only through a test's patched ``os.execve``."""
    python = python or root_tree.ROOT_PYTHON[host]
    text = phase_b_text(host, gen, lock_fd, pool_fd, caller_uid, tree=tree, entry=entry, fault=fault)
    os.set_inheritable(lock_fd, True)
    os.set_inheritable(pool_fd, True)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(python, [python, root_tree.PY_FLAGS, "-c", text], child_env())
    except BaseException:
        os.set_inheritable(lock_fd, False)    # the exec did not happen: nothing may inherit them now
        os.set_inheritable(pool_fd, False)
        raise


def _fsync_dir(path: Path) -> str | None:
    """``path`` (a directory) fsynced; the failure as text, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        return f"{path} not synced ({e.strerror or e})"
    return None


def _restore_phase_a(host: HostOS, cell: root_tree.Staged, prior: Intent | None, current: str | None,
                     *, intent_path: Path | None = None, tree: Path | None = None) -> list[str]:
    """[r5: both seats MED] The host put back as phase A found it, after ANY phase-A failure or exception before the
    exec: ``staging.partial`` and the staged generation removed (never ``current``, never a generation that was there
    before :func:`root_tree._place` replaced it, ``cell.existed``), the import tree fsynced, and the journal back to
    what phase A read (``prior`` rewritten, a finished one included; none -> the run's own removed, the state dir
    fsynced). [r5: L3 r2 codex MED] The target is removed only when THIS run placed it (``cell.placed``) and it was
    not there before (``cell.existed``, recorded by the hash step before anything can raise). Every op is checked:
    returns what could NOT be put back (empty when all of it was)."""
    import shutil

    tree = tree if tree is not None else root_tree.current_path(host).parent
    undone: list[str] = []
    removed = False
    names = [root_tree.STAGING] + ([cell.target] if cell.target and cell.placed and not cell.existed else [])
    for name in names:
        p = tree / name
        if name == current or not os.path.lexists(p):
            continue
        try:
            shutil.rmtree(p)
            removed = True
        except OSError as e:
            undone.append(f"{p} not removed ({e.strerror or e})")
    if cell.target and cell.existed and not os.path.isdir(tree / cell.target):
        undone.append(f"{tree / cell.target} was there before this run and is gone")
    if removed:
        undone += [w for w in (_fsync_dir(tree),) if w]
    path = Path(intent_path or INTENT_PATH[host])
    if prior is None:
        try:
            os.unlink(path)
            undone += [w for w in (_fsync_dir(path.parent),) if w]
        except FileNotFoundError:
            pass
        except OSError as e:
            undone.append(f"the run's journal {path} not removed ({e.strerror or e})")
    else:
        try:
            ok, why = write_intent_atomically(path, intent_text(prior))
        except OSError as e:
            ok, why = False, f"could not write the intent journal {path}: {e.strerror or e}"
        if not ok:
            undone.append(f"the earlier journal not put back ({why})")
    return undone


def repair_handover(plan: InstallPlan, g: Gathered, lock_fd: int, caller_uid: int, err: TextIO, *,
                    apply: Callable[[InstallPlan, TextIO], Optional[str]] = apply_steps,
                    probe: Callable[..., tuple[str, ...]] = run_probe,
                    python: str | None = None,
                    tree: Path | None = None, intent_path: Path | None = None, entry: str | None = None,
                    fault: Optional[Tuple[str, str]] = None) -> None:
    """``repair --source``'s PHASE A, from its first act to the exec (§2.2): the pool lock proven held BEFORE any act
    (so that refusal has nothing to undo), then phase A's steps (``apply``), the staged generation probed (R3a), and
    the exec into phase B (R4) with the install lock and the pool lock carried across it. [r5: both seats MED] ANY
    failure or exception in between (a failed step, the hash's refusal, the probe's, an OSError) restores the host
    (:func:`_restore_phase_a`) and is raised; a restore that could not finish appends ``rollback-incomplete: <what>``
    to the refusal's detail. ``fault``: the launcher's, carried into the probe's bind and phase B's program as a literal
    [F7]. A resume (:func:`plan_repair_resume`) arrives here with its target already placed, or with a refusal raised
    only after its one step removed a torn generation (``Staged.then_refuse``, after ``failed``). ``python``, ``tree``, ``intent_path``, ``entry``: a test's
    interpreter, import tree, journal and phase B entry."""
    host, cell = plan.host, plan.handover
    assert cell is not None
    q = g.quiesce
    if q is None or not q.held:
        raise Refused("bad-root", "no pool lock held to hand over")      # [r5: codex 5] before any act
    prior = g.facts.intent               # as phase A found it (a finished journal too: phase A's R1 removes it)
    current = g.repair.current if g.repair is not None else None
    try:
        failed = apply(plan, err)
        if cell.refused is not None:
            raise cell.refused
        if failed is not None:
            raise Refused("step-failed", failed)
        if cell.then_refuse is not None:
            raise cell.then_refuse
        if cell.target is None:
            raise Refused("internal", "phase A staged no generation")
        journal = replace(plan.intent, generation=cell.target) if plan.intent is not None else None
        probe(host, cell.target, g.facts.install_shape, journal, fault=fault)
        err.write(f"levain install-helper: {cell.target} staged and probed; handing over to it\n")
        exec_phase_b(host, cell.target, lock_fd, q.fd, caller_uid, python=python, tree=tree, entry=entry, fault=fault)
    except BaseException as e:
        undone = _restore_phase_a(host, cell, prior, current, intent_path=intent_path, tree=tree)
        if not undone:
            raise
        note = "rollback-incomplete: " + "; ".join(undone)
        err.write(f"levain install-helper: {note}\n")
        if isinstance(e, Refused):
            raise Refused(e.name, f"{e.detail}; {note}" if e.detail else note) from None
        raise


@dataclass
class ContinueSeams:
    """Every input :func:`repair_continue` reads that a non-root test must replace, with its real default."""

    host: Optional[HostOS] = None
    lock_path: Optional[Path] = None
    owner_ok: Callable[[os.stat_result], bool] = lambda st: st.st_uid == 0
    store_seams: Optional[RH.Seams] = None
    state_dir: Optional[Path] = None
    #: None: :func:`read_directory`'s own default, which passes ``env=`` (a held ``subprocess.run`` here is one the
    #: spawn scan cannot follow).
    run: Optional[Callable[..., object]] = None
    user_name: Callable[[int], Optional[str]] = _user_name
    #: Called with the re-gather's result before the plan (a test observes the adopted locks there).
    after_gather: Optional[Callable[["Gathered"], None]] = None
    plan: Optional[Callable[[InstallCommand, "Gathered"], InstallPlan]] = None
    apply: Optional[Callable[[InstallPlan, TextIO], Optional[str]]] = None


def adopt_lock(fd: int, path: Path, *, owner_ok: Callable[[os.stat_result], bool]) -> None:
    """R4a [r2: X-4]: ``fd`` (inherited across the exec) must be the lock file at ``path``: ``path`` opened
    ``O_NOFOLLOW`` and both ``fstat``-ed to the same ``st_dev``/``st_ino``, a regular file with the expected owner and
    mode 0600, and the flock HELD by ``fd`` itself [r5: L3 r1 complement LOW 5, L3 r2 codex]: a ``LOCK_EX|LOCK_NB`` on
    that second description must meet ``BlockingIOError`` (someone holds it), and then the same on ``fd`` must succeed
    (a description that holds the lock re-takes it; one that does not meets the other holder). Else ``bad-root`` (a
    descriptor that did not survive the exec, names another file, or holds no lock)."""
    try:
        held = os.fstat(fd)
    except OSError as e:
        raise Refused("bad-root", f"descriptor {fd} for {path} did not survive the exec ({e.strerror})") from None
    try:
        check = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    try:
        st = os.fstat(check)
        if ((held.st_dev, held.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(st.st_mode) or not owner_ok(st)
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise Refused("bad-root", f"descriptor {fd} is not the lock {path} (regular, root's, 0600)")
        try:
            fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass                     # held by some description: ``fd``'s, or another run's (proved next)
        except OSError as e:
            raise Refused("bad-root", f"{path}: {e.strerror}") from None
        else:
            raise Refused("bad-root", f"descriptor {fd} for {path} holds no lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)    # a no-op on the description that holds it
        except BlockingIOError:
            raise Refused("bad-root", f"descriptor {fd} for {path} holds no lock (another description holds "
                                      "it)") from None
        except OSError as e:
            raise Refused("bad-root", f"{path}: {e.strerror}") from None
    finally:
        os.close(check)              # a second description: its close never drops the flock on ``fd``'s


def _continue(lock_fd: int, pool_fd: int, gen: str, caller_uid: int, cs: ContinueSeams, err: TextIO,
              fault: Optional[Tuple[str, str]] = None) -> dict:
    host = cs.host or _host()
    if not gen_mod.GEN_RE.fullmatch(gen):
        raise Refused("bad-token", f"generation: {gen!r}")
    hook = fault_hook(check_fault(fault))       # [F7] the launcher's literal: a kill at that point, nothing else
    rs = cs.store_seams or RH.Seams(host=host)
    L = rs.L
    adopt_lock(lock_fd, Path(cs.lock_path or INSTALL_LOCK[host]), owner_ok=cs.owner_ok)
    adopt_lock(pool_fd, L / "slots" / "pool.lock", owner_ok=cs.owner_ok)
    os.set_inheritable(lock_fd, False)    # children never inherit either (subprocess closes fds by default anyway)
    os.set_inheritable(pool_fd, False)
    q = Quiesce(L, waits=rs)
    q.fd = pool_fd                        # ADOPTED: never opened again, never waited on (that would wait on itself)
    _QUIESCING[_store_key(L)] = q
    cmd = parse_install_argv(["repair"])
    try:
        d = read_directory(host) if cs.run is None else read_directory(host, run=cs.run)
        g = _gather_held(cmd, host, caller_uid, d, rs, L, q, state_dir=cs.state_dir, user_name=cs.user_name)
        if cs.after_gather is not None:
            cs.after_gather(g)
        j = g.facts.intent
        if j is None or j.verb != "repair" or j.generation != gen:
            raise Refused("handover-mismatch", f"phase B was handed {gen}; the journal names "
                                               f"{j.generation if j is not None else 'nothing'}")
        plan = (cs.plan or (lambda c, gg, **kw: plan_repair(c, gg.facts, gg.repair, continuing=gen, **kw)))(  # type: ignore[arg-type]
            cmd, g, **({"fault": hook} if hook else {}))
        for line in plan.pending:
            err.write(f"levain install-helper: pending: {line}\n")
        failed = (cs.apply or apply_steps)(plan, err)
    finally:
        q.release()                       # the pool lock, then the install lock (§2.3's last row)
        try:
            os.close(lock_fd)
        except OSError:
            pass
    if failed is not None:
        raise Refused("step-failed", failed)
    return {"verb": "repair", "steps": len(plan.steps), "generation": gen}


def repair_continue(lock_fd: int, pool_fd: int, gen: str, *, caller_uid: int, fault: Optional[Tuple[str, str]] = None,
                    seams: ContinueSeams | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """PHASE B (§2.2, §2.3 R4a on), entered only through :func:`exec_phase_b`'s ``-c`` program, in the staged
    generation's code: ADOPT the two inherited locks (:func:`adopt_lock`; non-inheritable; the pool lock's
    :class:`Quiesce` built on the descriptor and registered), re-gather under them with :func:`_gather_held` (never
    :func:`gather_facts`, which would wait on its own pool lock, never :func:`install_lock`, which would refuse
    ``install-busy`` against itself), then plan from R4b on and apply. One canonical line on ``out``, as :func:`main`.
    ``fault``: the launcher's literal from phase A's program [F7] (SIGKILL at that point); never set by :func:`main`."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        line = ok_line(_continue(lock_fd, pool_fd, gen, caller_uid, seams or ContinueSeams(), err, fault))
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


@dataclass
class Seams:
    """Every act :func:`main` takes that a unit test cannot take as a non-root user, with its real default."""

    euid: Callable[[], int] = os.geteuid
    host: Callable[[], HostOS] = _host
    lock: Callable[[HostOS], ContextManager[object]] = install_lock
    gather: Callable[[InstallCommand, HostOS, int], Gathered] = gather_facts
    plan: Callable[[InstallCommand, Gathered], InstallPlan] = plan_for
    apply: Callable[[InstallPlan, TextIO], Optional[str]] = apply_steps
    purge: Callable[[str, int, HostOS], dict] = run_purge
    stdin: Callable[[int], bytes] = _read_stdin
    credential: Callable[..., dict] = run_credential
    #: set-limits (R1 F5): its own branch in :func:`run`, never the install plan's generic path.
    limits: Callable[..., dict] = run_set_limits
    #: routes-base / routes-trust (3c-9): their own branches in :func:`run`, as set-limits'.
    routes_base: Callable[..., dict] = run_routes_base
    routes_trust: Callable[..., dict] = run_routes_trust
    #: routes-egress (WF-1b): its own branch in :func:`run`, as routes-trust's.
    routes_egress: Callable[..., dict] = run_routes_egress
    #: enrol-authority-signer / remove-authority-signer (14b S5): their own branch in :func:`run`, as routes-trust's.
    authority_signer: Callable[..., dict] = run_authority_signer
    #: repair --source's phase A: its steps (with ``apply``), the probe and the exec into phase B (:func:`repair_handover`).
    handover: Callable[..., None] = repair_handover
    #: [F7] The staged-tree launcher's fault, ``(step id, point)``: set by a test harness, never by :func:`main`'s
    #: defaults, never read from the environment, argv, the grammar or a file. :func:`run` checks it before any act.
    fault_at: Optional[Tuple[str, str]] = None
    #: What happens at ``fault_at`` in THIS process (default: SIGKILL). A harness action (R2's mid-copy source edit)
    #: runs only in phase A; across the exec only the kill crosses, as the literal.
    fault_act: Optional[Callable[[], None]] = None
    #: The points a fault may name (membership, :data:`FAULT_POINTS`).
    POINTS: ClassVar[frozenset] = FAULT_POINTS

    def fault(self, step_id: str, point: str) -> None:
        """The hook every planned point calls: ``fault_act`` at ``fault_at``, nothing anywhere else."""
        if self.fault_at is not None and (step_id, point) == tuple(self.fault_at):
            (self.fault_act or _kill_self)()


def run(argv: Sequence[str], *, env: Mapping[str, str], seams: Seams, err: TextIO) -> dict[str, object]:
    """The verb's ok fields, or :class:`Refused`. The order is §25's: the install lock is taken BEFORE the host's
    facts are gathered, and held through the plan, its first step (the journal), every act, the record and the
    journal's removal, so no second run plans against this one's half. Nothing before the lock reads the host, except
    the credential verbs' and ``set-limits``' operator record and their stdin (:func:`run_credential` and
    :func:`run_set_limits` take the lock after them and check the operator again under it).
    Inside it, the gather's :class:`Quiesce` (the pool lock) is held through the last act too."""
    if seams.euid() != 0:
        raise Refused("not-root", "the install helper runs as root")
    fault = check_fault(seams.fault_at)          # [F7] a launcher's point outside Seams.POINTS: refused before any act
    cmd = parse_install_argv(argv)
    if cmd.verb not in BUILT_VERBS:
        raise Refused(f"verb-unbuilt:{cmd.verb}", cmd.verb)
    caller_uid = RH._sudo_uid(env)
    host = seams.host()
    if cmd.verb == "purge":
        # A store verb, not a host plan: no account read, no pool hold (a live lease does not refuse it, §5.2); the
        # install lock still keeps an undo of the store out from under it.
        with seams.lock(host):
            return seams.purge(cmd.session, caller_uid, host)   # type: ignore[arg-type]
    if cmd.verb in CREDENTIAL_VERBS:
        # Neither a host plan nor a pool hold (a live lease does not refuse them, §5.2); the object is refused in root
        # before the lock and before any fork; the install lock keeps an undo of L/keys out from under the child.
        # run_credential takes the lock itself: the operator check and set's stdin come before it (H18, B7).
        return seams.credential(cmd.verb, cmd.credential, caller_uid, host, read=seams.stdin,
                                lock=lambda: seams.lock(host), err=err)
    if cmd.verb == "set-limits":
        # As the credential verbs: no host plan, no pool hold, no live-lease count (a held slot session waits on this
        # very raise); the operator check and the document's check come before the lock, which run_set_limits takes.
        return seams.limits(caller_uid, host, read=seams.stdin, lock=lambda: seams.lock(host), err=err)
    if cmd.verb == "routes-base":
        # As set-limits: one root file, no host plan, no pool hold (keys re-reads base.json at every Q).
        return seams.routes_base(caller_uid, host, lock=lambda: seams.lock(host), err=err)
    if cmd.verb == "routes-trust":
        # As set-limits: the request and its fingerprint are checked before the lock, the write under it.
        # --replace (Q16) is passed only when given, so a seam written for the plain verb still takes it.
        return seams.routes_trust(caller_uid, host, read=seams.stdin, lock=lambda: seams.lock(host), err=err,
                                  **({"replace": True} if cmd.replace else {}))
    if cmd.verb == "routes-egress":
        # As routes-trust: the request is checked before the lock, the map written under it.
        return seams.routes_egress(caller_uid, host, read=seams.stdin, lock=lambda: seams.lock(host), err=err)
    if cmd.verb in SIGNER_VERBS:
        # As routes-trust: argv's keys are checked and fingerprinted before the lock, the file written under it.
        return seams.authority_signer(cmd, caller_uid, host, lock=lambda: seams.lock(host), err=err)
    with seams.lock(host) as lock_fd:
        gathered = seams.gather(cmd, host, caller_uid)
        try:
            plan = seams.plan(cmd, gathered, **({"fault": seams.fault} if fault else {}))
            for line in plan.pending:
                err.write(f"levain install-helper: pending: {line}\n")
            if plan.handover is not None:
                # repair --source: phase A (its steps, the probe, the exec into the staged code; never returns). Any
                # failure in it puts the host back and is raised [r5: both seats MED].
                seams.handover(plan, gathered, lock_fd, caller_uid, err, apply=seams.apply, fault=fault)
                failed = None
            else:
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


__all__ = ["ContinueSeams", "PROBE_OUT_CAP", "PROBE_TIMEOUT_S", "adopt_lock", "exec_phase_b", "phase_b_text",
           "plan_repair_stage", "plan_repair_resume", "check_fault", "fault_hook", "FAULT_POINTS", "FAULT_STEPS", "probe_text", "repair_continue", "repair_handover", "HANDOVER_ABI", "repair_probe", "run_probe",
           "RepairFacts", "desired_managed", "install_shape_path", "plan_only_report", "plan_repair", "BUILT_VERBS", "CHILD_MARGIN_S", "CREDENTIAL_VERBS", "INSTALL_ENTRY_ENV", "KEYS_ACCOUNT", "Directory", "Gathered", "HostFacts", "InstallPlan", "OPERATOR_FORMAT", "Seams",
           "apply_steps", "check_account_ids", "drop_to_keys", "load_creds", "read_credential", "run_credential", "read_limits", "run_set_limits", "run_authority_signer", "authority_key_problem", "blob_fingerprint", "SIGNER_VERBS", "policy_limits_state", "policy_limits_undo_steps", "authority_signers_undo_steps", "run_purge", "check_tools", "count_live_leases", "live_lease_sessions", "extra_tools", "gather_facts",
           "helper_pending", "main", "operator_record", "operator_record_text", "parse_operator_record",
           "plan_for", "plan_replenish", "plan_setup", "plan_tools", "plan_undo", "pool_lock", "preflight", "Quiesce",
           "quiesce_step",
           "read_directory", "remove_slot_state", "run", "slot_state_steps", "slot_state_undo_steps",
           "slots_in_service", "write_initial_slot_state"]


if __name__ == "__main__":   # pragma: no cover - the root entry (root_tree.ENTRY_MODULES)
    raise SystemExit(main(sys.argv[1:], env=RH.entry_env(os.environ, INSTALL_ENTRY_ENV)))
