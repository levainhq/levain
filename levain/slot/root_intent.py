"""Root's per-session intent journal (CONTRACT §7.4, ``levain-root-intent/1``).

A multi-object transaction (END's ``retain``, the credential ``park``; session 5 adds ``discard``,
``restore`` and ``purge``) is driven by ONE root-owned file, ``L/alloc/<session>.intent.json``
(root 0600), never by the CONTROL-written state record: a forged state record must not be able to
steer root into a purge (CONTRACT §7.4's c3 rebuild). The file names the transaction and the phase
it is in; each phase is written BEFORE the step it names, and every step is idempotent, so a crash
anywhere is finished by re-running the recorded phase's step and the rest.

Reconciliation (helper start, the reaper's boot pass and every pass, §7.7 step 2) reads ONLY this
file and the filesystem. After the last step it calls the injected ``cas(target_state, fields)``
seam, then deletes the file. The seam is CONTROL's CAS: one whose target is already the record's
state counts as done (CONTRACT §7.4: a crash between the CAS and the delete must not refuse on a
stale ``from``), so a finish that is redone after that crash still completes.
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from levain.contracts import ContractError, canon
from levain.slot import Refused, write_all
from levain.slot.grammar import SESSION_RE

ROOT_INTENT_FORMAT = "levain-root-intent/1"

# CONTRACT §7.4's table. discard / restore / purge are listed so a journal session 5 writes parses
# by name here; their steps are session 5's, so reconciling one refuses `intent-unbuilt`.
INTENT_PHASES: Mapping[str, tuple[str, ...]] = {
    "retain": ("init", "manifest", "meta", "object", "tree", "commit"),
    "park": ("move", "commit"),
    "discard": ("rename", "stamp", "commit"),
    "restore": ("mark", "rename", "unstamp", "commit"),
    "purge": ("check", "delete_export", "delete", "commit"),
}
#: The state each intent CASes after its last step (CONTRACT §7.3, §7.4).
TARGET_STATE: Mapping[str, str] = {"retain": "RETAINED", "park": "CREDENTIAL_PARKED", "discard": "TRASH",
                                   "restore": "RETAINED", "purge": "PURGED"}
BUILT_INTENTS = ("retain", "park")

TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")   # CONTRACT header
# DECISION: a journal is four short fields; a file past this is not one, refused before it is parsed.
MAX_INTENT_BYTES = 4096

Cas = Callable[[str, Mapping[str, object]], None]
Fault = Callable[[str], None]
Retry = Callable[[Callable[[], None]], None]


def _no_fault(point: str) -> None:
    return None


def _direct(fn: Callable[[], None]) -> None:
    fn()


def _root_owned(st: os.stat_result) -> bool:
    return st.st_uid == 0


# --- durable writes --------------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def temp_path(path: Path) -> Path:
    """A temp name beside ``path`` that is unique per writer (``.<name>.<16 hex>.tmp``): two writers of the
    same path never unlink or rename each other's temp file (L2 M7). A dead writer's temp is swept by
    :func:`sweep_temps` under the lock that makes its caller the only writer."""
    path = Path(path)
    return path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")


def is_temp_of(name: str, base: str) -> bool:
    """Whether ``name`` is a :func:`temp_path` name of a path whose name starts with ``base``."""
    return name.startswith(f".{base}") and name.endswith(".tmp") and len(name) >= len(base) + 22


def sweep_temps(directory: Path, base: str, *,
                owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> list[str]:
    """Unlink the regular files in ``directory`` that are :func:`temp_path` names of paths starting with
    ``base`` and owned as expected (a dead writer's leftovers). The caller holds the lock that makes it the
    only writer, so no live writer's temp is among them. Returns the names removed."""
    gone = []
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return gone
    for name in names:
        if not is_temp_of(name, base):
            continue
        p = Path(directory) / name
        st = os.lstat(p)
        if stat.S_ISREG(st.st_mode) and owner_ok(st):
            os.unlink(p)
            gone.append(name)
    if gone:
        fsync_dir(Path(directory))
    return gone


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """``data`` at ``path`` so a reader sees the old file or the whole new one: a fresh temp file
    beside it (:func:`temp_path`, unique per writer) written whole and fsynced, renamed over the path,
    the directory fsynced. Raises ``OSError`` (ENOSPC included, which the reserve's retry acts on). A
    failure before the rename removes the temp file and leaves the path untouched; a failure of the
    directory fsync after it leaves the new file in place, not yet durable (a retry rewrites it)."""
    path = Path(path)
    tmp = temp_path(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(fd, mode)
        write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def unlink_durable(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    fsync_dir(Path(path).parent)


# --- the record ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RootIntent:
    intent: str
    phase: str
    started_at: str
    forced: bool | None = None

    def at(self, phase: str) -> "RootIntent":
        return RootIntent(self.intent, phase, self.started_at, self.forced)


def intent_path(alloc_dir: Path, session: str) -> Path:
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise Refused("bad-token", f"session {session!r}")
    return Path(alloc_dir) / f"{session}.intent.json"


def intent_bytes(ri: RootIntent) -> bytes:
    obj: dict[str, object] = {"format": ROOT_INTENT_FORMAT, "intent": ri.intent, "phase": ri.phase,
                              "started_at": ri.started_at}
    if ri.forced is not None:
        obj["forced"] = ri.forced
    return canon.canonical_bytes(obj)


def parse_root_intent(data: bytes) -> RootIntent:
    """Strict, never a guess: canonical bytes, exactly the record's keys, a known intent at one of its
    phases, a contract timestamp; ``forced`` only as ``true`` on a ``purge`` (the install verb's)."""
    bad = Refused("intent-malformed", f"not a {ROOT_INTENT_FORMAT} record")
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise Refused("intent-malformed", str(e)) from None
    if not isinstance(obj, dict) or canon.canonical_bytes(obj) != data:
        raise bad
    keys = set(obj)
    if not {"format", "intent", "phase", "started_at"} <= keys <= {"format", "intent", "phase", "started_at",
                                                                     "forced"}:
        raise bad
    intent, phase, started = obj["intent"], obj["phase"], obj["started_at"]
    if (obj["format"] != ROOT_INTENT_FORMAT or intent not in INTENT_PHASES
            or phase not in INTENT_PHASES[intent]
            or not isinstance(started, str) or not TIMESTAMP_RE.fullmatch(started)):
        raise bad
    forced = obj.get("forced")
    if "forced" in obj and (forced is not True or intent != "purge"):
        raise bad
    return RootIntent(intent, phase, started, forced)


def read_root_intent(path: Path, *,
                     owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> RootIntent | None:
    """``None`` when there is no journal. A link, a non-regular file or one root does not own is
    ``bad-root``; anything that does not parse is ``intent-malformed``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("bad-root", f"{path} is a link") from None
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or not owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        data = fh.read(MAX_INTENT_BYTES + 1)
    if len(data) > MAX_INTENT_BYTES:
        raise Refused("intent-malformed", f"{path} is over {MAX_INTENT_BYTES} bytes")
    return parse_root_intent(data)


def write_root_intent(path: Path, ri: RootIntent) -> None:
    atomic_write(path, intent_bytes(ri), 0o600)


def delete_root_intent(path: Path) -> None:
    unlink_durable(path)


def pending_sessions(alloc_dir: Path) -> list[str]:
    """The sessions with a journal in ``alloc_dir``, by file name only (the reaper's step 2 list)."""
    out = []
    for name in sorted(os.listdir(alloc_dir)):
        if name.endswith(".intent.json") and SESSION_RE.fullmatch(name[: -len(".intent.json")]):
            out.append(name[: -len(".intent.json")])
    return out


# --- the phases and the reconciler -----------------------------------------------------------------


@dataclass(frozen=True)
class Phase:
    """One phase: its name and its idempotent step (raises on failure; running it again after it
    succeeded, or after a crash part-way through it, is safe)."""

    name: str
    step: Callable[[], None]


@dataclass(frozen=True)
class Plan:
    """An intent's phases in §7.4 order, and the CAS fields its finish carries (computed from durable
    files only, so a redone finish sends the same ones)."""

    intent: str
    phases: tuple[Phase, ...]
    fields: Callable[[], Mapping[str, object]] = field(default=lambda: {})

    def __post_init__(self) -> None:
        if tuple(p.name for p in self.phases) != INTENT_PHASES.get(self.intent):
            raise ValueError(f"{self.intent}: phases are not CONTRACT §7.4's")

    @property
    def target(self) -> str:
        return TARGET_STATE[self.intent]


class PhaseFailed(Exception):
    """A phase's step (or the finishing CAS, reported as the last phase) did not succeed. The journal
    stays at that phase, so the next reconciliation redoes it."""

    def __init__(self, phase: str, cause: BaseException) -> None:
        self.phase = phase
        self.cause = cause
        super().__init__(f"{phase}: {cause}")


def run_from(path: Path, plan: Plan, ri: RootIntent, *, cas: Cas, fault: Fault = _no_fault,
             retry: Retry = _direct) -> None:
    """Redo ``ri.phase``'s step, then for each later phase: write it, then run its step; then CAS the
    target and delete the journal. Fault points (a test's crash seam): ``<intent>:before:<phase>``
    and ``<intent>:after:<phase>`` around each phase write, ``<intent>:before:cas`` /
    ``<intent>:after:cas`` around the CAS."""
    names = [p.name for p in plan.phases]
    if ri.intent != plan.intent or ri.phase not in names:
        raise Refused("intent-malformed", f"{ri.intent}/{ri.phase} is not a phase of {plan.intent}")
    for phase in plan.phases[names.index(ri.phase):]:
        if phase.name != ri.phase:
            fault(f"{plan.intent}:before:{phase.name}")
            try:
                retry(lambda: write_root_intent(path, ri.at(phase.name)))
            except OSError as e:
                raise PhaseFailed(phase.name, e) from e
            fault(f"{plan.intent}:after:{phase.name}")
        try:
            retry(phase.step)
        except Exception as e:   # noqa: BLE001 - any failure of a step leaves the journal at it
            raise PhaseFailed(phase.name, e) from e
    fault(f"{plan.intent}:before:cas")
    try:
        cas(plan.target, dict(plan.fields()))
    except Exception as e:   # noqa: BLE001
        raise PhaseFailed(names[-1], e) from e
    fault(f"{plan.intent}:after:cas")
    delete_root_intent(path)


def begin(path: Path, plan: Plan, *, started_at: str, cas: Cas, forced: bool | None = None,
          fault: Fault = _no_fault, retry: Retry = _direct) -> None:
    """Start ``plan``: its first phase is journaled BEFORE its first filesystem step. A journal already
    present is refused (``intent-pending``): it is reconciled, never overwritten."""
    if os.path.lexists(path):
        raise Refused("intent-pending", f"{path} exists")
    if not TIMESTAMP_RE.fullmatch(started_at):
        raise ValueError(f"started_at {started_at!r}")
    ri = RootIntent(plan.intent, plan.phases[0].name, started_at, forced)
    fault(f"{plan.intent}:before:{ri.phase}")
    try:
        retry(lambda: write_root_intent(path, ri))
    except OSError as e:
        raise PhaseFailed(ri.phase, e) from e
    fault(f"{plan.intent}:after:{ri.phase}")
    run_from(path, plan, ri, cas=cas, fault=fault, retry=retry)


def reconcile(path: Path, plan_for: Callable[[RootIntent], Plan], *, cas: Cas, fault: Fault = _no_fault,
              retry: Retry = _direct,
              owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> RootIntent | None:
    """Finish the journal at ``path`` FORWARD from its recorded phase; ``None`` when there is none.
    ``plan_for`` builds the intent's plan from alloc-derived facts (CONTRACT §7.1: every path root
    uses derives from alloc); an intent whose steps are not built here refuses ``intent-unbuilt``
    and nothing is touched. Returns the journal it finished."""
    ri = read_root_intent(path, owner_ok=owner_ok)
    if ri is None:
        return None
    if ri.intent not in BUILT_INTENTS:
        raise Refused("intent-unbuilt", f"{ri.intent} is session 5's")
    run_from(path, plan_for(ri), ri, cas=cas, fault=fault, retry=retry)
    return ri


__all__ = ["BUILT_INTENTS", "Cas", "INTENT_PHASES", "MAX_INTENT_BYTES", "Phase", "PhaseFailed", "Plan",
           "ROOT_INTENT_FORMAT", "RootIntent", "TARGET_STATE", "TIMESTAMP_RE", "atomic_write", "begin",
           "delete_root_intent", "fsync_dir", "intent_bytes", "intent_path", "parse_root_intent",
           "is_temp_of", "pending_sessions", "read_root_intent", "reconcile", "run_from", "sweep_temps",
           "temp_path", "unlink_durable", "write_root_intent"]
