"""The install lock and the intent journal (CONTRACT §25, c7.7).

The lock serialises install-helper runs: every verb takes it before it gathers the host's facts and
holds it through its last step, so two runs never plan against the same state or interleave their
acts. The journal records the exact allocation a ``setup`` or ``replenish`` is about to create,
written before its first act and removed by its last. A Linux shared group carries no label, so the
journal is what tells a group an interrupted setup made from someone else's; and a replenish that
dies part-way resumes the indices it journaled instead of spending new ones.

The lock is the apply layer's (a context manager it enters before reading the host); the journal's
write and removal are plan Steps.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HostOS, Step, _abs
from levain.slot import Refused, write_all
from levain.slot.generation import GEN_RE, TXN_RE, Record, managed_json, parse_managed
from levain.slot.ids import RANGES, RETIRED_IDS_PATH, in_own_range, is_levain_account

INTENT_FORMAT = "levain-intent/2"
#: Still read (a journal an older generation wrote): no ``txn``, no ``generation``, no ``managed``.
INTENT_FORMAT_V1 = "levain-intent/1"
#: The verbs that journal; ``undo`` reads a journal but never writes one.
INTENT_VERBS = ("setup", "replenish", "repair")

STATE_DIR: Mapping[str, Path] = {h: p.parent for h, p in RETIRED_IDS_PATH.items()}
INSTALL_LOCK: Mapping[str, Path] = {h: d / "install.lock" for h, d in STATE_DIR.items()}
INTENT_PATH: Mapping[str, Path] = {h: d / "install-intent" for h, d in STATE_DIR.items()}


def _root_owned(st: os.stat_result) -> bool:
    return st.st_uid == 0


# --- the lock ----------------------------------------------------------------------------------------


@contextmanager
def install_lock(host: HostOS | None = None, *, path: Path | None = None,
                 owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> Iterator[int]:
    """Hold the install lock for the body of the ``with``. ``owner_ok``: whether a stat belongs to the
    owner the lock must have (root; a test passes its own uid).

    The state dir is made if absent; it must then be a real directory with the right owner. The lock
    file is opened without following a link and created 0600 if absent; anything but a regular file
    with the right owner is ``bad-root``. Another holder is ``install-busy``: the lock is never waited
    for, so a second run refuses by name instead of hanging behind the first."""
    if path is None:
        if host not in INSTALL_LOCK:
            raise Refused("unsupported-host", repr(host))
        path = INSTALL_LOCK[host]
    path = Path(path)
    try:
        os.mkdir(path.parent, 0o755)
    except FileExistsError:
        pass
    pst = os.lstat(path.parent)
    if not stat.S_ISDIR(pst.st_mode) or not owner_ok(pst):
        raise Refused("bad-root", f"{path.parent} is not a directory with the expected owner")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("bad-root", f"{path} is a link") from None
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("install-busy", "another install-helper run holds the lock") from None
        yield fd
    finally:
        os.close(fd)   # closing the only descriptor releases the flock


# --- the journal record ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """What a ``setup``, ``replenish`` or ``repair`` is about to create: its accounts (name -> uid == gid) and its
    shared groups (name -> gid). ``levain-intent/2`` (row 33) adds the run's ``txn`` (a fresh 128-bit id; repair's
    completion proof is ``install-shape``'s ``completed_txn == txn``), the ``generation`` a repair is moving to (None
    until its staged copy is hashed, §2.3 R3) and the write-ahead ``managed`` (R4b). A ``/1`` journal reads with
    ``txn`` None."""

    verb: str
    operator: str
    accounts: Mapping[str, int] = field(default_factory=dict)
    groups: Mapping[str, int] = field(default_factory=dict)
    txn: str | None = None
    generation: str | None = None
    managed: tuple[Record, ...] = ()

    @property
    def names(self) -> dict[str, int]:
        return {**self.accounts, **self.groups}


def intent_record(intent: Intent) -> dict[str, object]:
    if intent.txn is None or not TXN_RE.fullmatch(intent.txn):
        raise Refused("intent-malformed", "a levain-intent/2 journal carries a txn")
    return {"format": INTENT_FORMAT, "verb": intent.verb, "operator": intent.operator, "txn": intent.txn,
            "generation": intent.generation, "accounts": dict(intent.accounts), "groups": dict(intent.groups),
            "managed": managed_json(intent.managed)}


def intent_text(intent: Intent) -> str:
    return canon.canonical(intent_record(intent))


#: §1.1 rule f: a shared group's name by the bound (``group/1``'s), never by membership of ``SHARED_GROUPS``.
GROUP_RE = re.compile(r"levain-[a-z0-9-]{1,24}")


def _is_id(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def parse_intent(data: bytes, host: HostOS) -> Intent:
    """Strict: exactly the record's keys, a journaling verb, an operator name, Levain account names at
    ids in their §1.1 range, shared-group names at ids in the shared range, no id twice. A group is judged by
    the BOUND (a ``levain-`` name at a gid in the shared range), never by this code's own ``SHARED_GROUPS``, so a
    package can read a journal naming a group it does not know (DESIGN 1022-8/upgrade §1.1 rule f)."""
    try:
        obj = canon.parse(data)
    except canon.ContractError as e:
        raise Refused("intent-malformed", str(e)) from None
    if host not in RANGES:
        raise Refused("unsupported-host", repr(host))
    bad = Refused("intent-malformed", f"not a {INTENT_FORMAT} or {INTENT_FORMAT_V1} record")
    v1 = {"format", "verb", "operator", "accounts", "groups"}
    v2 = v1 | {"txn", "generation", "managed"}
    if not isinstance(obj, dict) or (set(obj), obj.get("format")) not in ((v1, INTENT_FORMAT_V1), (v2, INTENT_FORMAT)):
        raise bad
    if obj["format"] == INTENT_FORMAT_V1 and obj["verb"] == "repair":
        raise bad
    if obj["format"] == INTENT_FORMAT and (
            not isinstance(obj["txn"], str) or not TXN_RE.fullmatch(obj["txn"])
            or not (obj["generation"] is None or (isinstance(obj["generation"], str) and GEN_RE.fullmatch(obj["generation"])))
            or (obj["verb"] != "repair" and (obj["generation"] is not None or obj["managed"]))):
        raise bad
    if (obj["verb"] not in INTENT_VERBS
            or not isinstance(obj["operator"], str) or not OPERATOR_RE.fullmatch(obj["operator"])
            or not isinstance(obj["accounts"], dict) or not isinstance(obj["groups"], dict)):
        raise bad
    accounts, groups = obj["accounts"], obj["groups"]
    for name, uid in accounts.items():
        if not is_levain_account(name) or not in_own_range(host, name, uid):
            raise bad
    for name, gid in groups.items():
        if not GROUP_RE.fullmatch(name) or not _is_id(gid) or gid not in RANGES[host].shared:
            raise bad
    if obj["verb"] == "replenish" and groups:
        raise bad
    ids = [*accounts.values(), *groups.values()]
    if len(set(ids)) != len(ids):
        raise bad
    managed = parse_managed(obj["managed"], what="intent") if obj["format"] == INTENT_FORMAT else ()
    return Intent(obj["verb"], obj["operator"], dict(accounts), dict(groups), obj.get("txn"), obj.get("generation"),
                  managed)


def read_intent(host: HostOS, *, path: Path | None = None) -> Intent | None:
    """The apply layer's read, under the lock: ``None`` when there is no journal. A link or a file
    that is not regular is refused, as for the lock."""
    path = Path(path or INTENT_PATH[host])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Refused("bad-root", f"{path} is not a regular file")
        return parse_intent(fh.read(), host)


# --- the journal steps -------------------------------------------------------------------------------


def write_intent_atomically(path: Path, text: str) -> tuple[bool, str]:
    """Write ``text`` to ``path`` 0600 so a reader sees the old journal or the whole new one, never part
    of one, and the new one survives a crash once this returns ok: a fresh temp file written whole,
    fsynced, renamed over the path, the directory fsynced. A write that cannot finish is a failed step,
    with the temp file removed and the path untouched."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.mkdir(path.parent, 0o755)
    except FileExistsError:
        pass
    try:
        os.unlink(tmp)          # a temp file a dead run left
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        write_all(fd, text.encode("utf-8"))
        os.fsync(fd)
    except OSError as e:
        # Nothing is renamed: a journal that is not whole is never put where a reader would trust it.
        os.close(fd)
        tmp.unlink(missing_ok=True)
        return False, f"could not write the intent journal {tmp}: {e.strerror or e}"
    os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return True, ""


def intent_write_step(host: HostOS, intent: Intent, *, path: Path | None = None) -> Step:
    target = Path(path or INTENT_PATH[host])
    text = intent_text(intent)
    return Step(f"journal what this {intent.verb} will create in {target} (txn {intent.txn})",
                call=lambda: write_intent_atomically(target, text))


def intent_remove_step(host: HostOS, *, path: Path | None = None) -> Step:
    target = Path(path or INTENT_PATH[host])
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    return Step(f"remove the intent journal {target}", (rm, "-f", str(target)))


__all__ = ["GROUP_RE", "INSTALL_LOCK", "INTENT_FORMAT", "INTENT_FORMAT_V1", "INTENT_PATH", "INTENT_VERBS", "Intent", "STATE_DIR",
           "install_lock", "intent_record", "intent_remove_step", "intent_text", "intent_write_step",
           "parse_intent", "read_intent", "write_intent_atomically"]
