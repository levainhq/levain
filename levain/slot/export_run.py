"""The sealed run's root side, piece 5c-ii-a: the ONE classifier and the publish transaction (S1 session 5 DESIGN §1
steps 10-11, §6.0; CONTRACT_EDITS E12, E20; cut: ``project_memory/1022-18/adopt/CUT.md``).

Root classifies a run exactly ONCE (:func:`classify`), from its own observations (:class:`RunObservation`) and the
result line read from fd 4 as DATA, in the total order of DESIGN §1 step 10: (1) memory (an unreadable Linux
``memory.events.local`` takes that slot as ``export-image-unavailable`` and ranks first), (2) the host volume short,
(3) the strict-boolean ``enospc: true`` ONLY on a plain non-zero exit, (4) ``<role>-failed:<reason>``. SUCCESS is exit
0, no root failure observation and a fully valid line with ``enospc`` false; every other outcome is a DENIAL that
publishes nothing. :func:`publish_if_success` is the one call site that reads the answer: only SUCCESS reaches
:func:`check_outputs` and :func:`transact`, so an importer's ``out/`` pair left by a failed, killed or line-less run is
never published (the 5b2 carry in CONTRACT_EDITS, head 1022+8).

The transaction (r11 S14-1, r12 S15-1/S15-3, r13 S16-6): copy each output into a fresh ``<s>.new/`` while hashing (the
bundle's header parsed by :mod:`levain.slot.bundlefmt` as it streams), fsync each file, fsync ``<s>.new``, then COMMIT
by the no-replace rename where no ``<s>`` exists or by the atomic EXCHANGE where one does, fsync the class dir, and only
then delete the older tree (now at ``<s>.new``). The exchange is the only commit form: a filesystem that refuses it (or
the no-replace call) fails CLOSED as ``<role>-failed:publish`` with detail ``exchange-unsupported``; there is no
plain-rename fallback. Any OSError before the commit removes ``<s>.new`` and leaves ``<s>`` as it was.

Root code: runs under ``/usr/bin/python3 -IS`` (3.9). Seams are injected parameters (:class:`PublishSeams`), never
env flags. The image, the leaf, the watchdog and the domain proof are piece 5c-ii-b; the wiring into adopt-open is
5c-ii-c.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.slot import Refused, write_all
from levain.slot import bundlefmt as BF
#: The importer's cap on its one line, the newline included: ONE constant, the writer's.
from levain.slot.importer import RESULT_CAP

# --- the two commit calls ---------------------------------------------------------------------------------------

if sys.platform == "darwin":
    #: ``renameatx_np`` flags (<stdio.h>): RENAME_SWAP, RENAME_EXCL.
    EXCHANGE, NOREPLACE, _SYMBOL = 0x2, 0x4, "renameatx_np"
else:
    #: ``renameat2`` flags (<linux/fs.h>): RENAME_EXCHANGE, RENAME_NOREPLACE.
    EXCHANGE, NOREPLACE, _SYMBOL = 0x2, 0x1, "renameat2"

#: The errnos that mean "this filesystem does not do it" (DESIGN §1 step 11, r12 S15-1).
UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


class ExchangeUnsupported(Exception):
    """The exchange or the no-replace rename is refused by the filesystem, or the libc symbol is missing."""


_FN: list = []


def _rename_fn():
    """The libc commit call, or None when this libc has no such symbol (a missing symbol is ``exchange-unsupported``,
    never a reason to reach for another form)."""
    if not _FN:
        fn = None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fn = getattr(libc, _SYMBOL)
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
        except (OSError, AttributeError, TypeError):
            fn = None
        _FN.append(fn)
    return _FN[0]


def rename2(fd1: int, name1: str, fd2: int, name2: str, flag: int) -> None:
    """One ``renameatx_np`` / ``renameat2`` call: an unsupported refusal raises :class:`ExchangeUnsupported`, any
    other errno ``OSError``."""
    fn = _rename_fn()
    if fn is None:
        raise ExchangeUnsupported(f"no {_SYMBOL} in this libc")
    if fn(fd1, os.fsencode(name1), fd2, os.fsencode(name2), flag) != 0:
        e = ctypes.get_errno()
        if e in UNSUPPORTED_ERRNOS:
            raise ExchangeUnsupported(errno.errorcode.get(e, str(e)))
        raise OSError(e, os.strerror(e), name1)


# --- roles ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Role:
    """The names one sealed run answers with (CONTRACT_EDITS E20): the importer's under adopt-open, the verifier's
    under adopt-verify. ``missing`` is the reason a line that is valid but incomplete gets (the importer's terminal
    frame, the verifier's outputs)."""

    failed: str
    memory: str
    space: str
    work_cap: str
    missing: str
    staged_help: str


IMPORTER = Role("importer-failed", "adopt-cap:memory", "adopt-space-exhausted", "adopt-work-cap", "terminal-missing",
                "L/export/<class>/<s>.new")
VERIFIER = Role("verifier-failed", "preview-cap:memory", "preview-space-exhausted", "preview-space-exhausted",
                "output-missing", "<s>/verify.new")

# --- the result line --------------------------------------------------------------------------------------------

LINE_KEYS = frozenset({"v", "terminal", "enospc", "refused", "result", "detail", "walk", "ignored_git_paths"})
RESULTS = frozenset({"delta", "unrelated", "adopt-nothing-new"})
#: An importer refusal name root surfaces verbatim as ``importer-failed:<name>`` (5b1 carry 1): printable ASCII, no
#: space, so a hostile importer cannot put a control byte into the operator's terminal through it.
_NAME = re.compile(r"[a-z][a-z0-9-]*(?::[\x21-\x7e]{1,512})?")


class LineBad(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason      # "result-oversize" | "result-malformed"
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class Line:
    terminal: bool
    enospc: bool
    refused: Optional[str]
    result: Optional[str]
    walk: Tuple[int, int]


def _nonneg_int(x: object) -> bool:
    return type(x) is int and x >= 0


def _no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def parse_importer_line(raw: Optional[bytes]) -> Line:
    """The importer's one line (CONTRACT_EDITS, appended 10-22 by head 1022+8) as data, or :class:`LineBad`. Strict:
    exactly one newline-terminated line within :data:`RESULT_CAP`, byte-equal to its own canonical JSON, the exact
    key set, strict booleans, non-bool integers. Consistency with the exit status is :func:`classify`'s."""
    if raw is None or raw == b"":
        raise LineBad("result-malformed", "no result line")
    if len(raw) > RESULT_CAP:
        raise LineBad("result-oversize", f"more than {RESULT_CAP} bytes")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise LineBad("result-malformed", "not one newline-terminated line")
    try:
        obj = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_no_dup, parse_constant=_no_constant)
        if not isinstance(obj, dict) or canon.canonical_bytes(obj) + b"\n" != raw:
            raise ValueError("not canonical JSON")
    except (ValueError, UnicodeDecodeError, ContractError, TypeError) as e:
        raise LineBad("result-malformed", f"{type(e).__name__}: {e}"[:200]) from None
    if set(obj) != LINE_KEYS:
        raise LineBad("result-malformed", "the key set")
    walk = obj["walk"]
    ok = (type(obj["v"]) is int and obj["v"] == 1
          and type(obj["terminal"]) is bool and type(obj["enospc"]) is bool
          and (obj["refused"] is None or (isinstance(obj["refused"], str) and _NAME.fullmatch(obj["refused"])))
          and (obj["result"] is None or obj["result"] in RESULTS)
          and isinstance(obj["detail"], str)
          and isinstance(walk, dict) and set(walk) == {"entries", "bytes"}
          and _nonneg_int(walk["entries"]) and _nonneg_int(walk["bytes"])
          and (obj["ignored_git_paths"] is None or _nonneg_int(obj["ignored_git_paths"])))
    if not ok:
        raise LineBad("result-malformed", "a field's type or value")
    return Line(obj["terminal"], obj["enospc"], obj["refused"], obj["result"], (walk["entries"], walk["bytes"]))


# --- the one classifier -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunObservation:
    """Everything root observed of one run, read at seal-out once the domain is proven empty (Linux: after the leaf
    reads ``populated 0``; macOS: after the watchdog stopped), so a late kill is in it (r11 S14-5).

    ``exit``: ``"exit"`` (a plain exit, ``code`` its status), ``"signal"`` or ``"time"`` (root's own observations,
    never overridden by the line). ``memory_killed``: the leaf's ``oom_kill > 0`` or the watchdog's kill flag.
    ``memory_unreadable``: the leaf's ``memory.events.local`` could not be read. ``host_short``: root's host-side
    reading (``f_bavail x f_frsize`` below the image's unallocated remainder). ``line``: the bytes read from fd 4,
    at most :data:`RESULT_CAP` + 1 (None = none)."""

    exit: str
    code: Optional[int]
    memory_killed: bool = False
    memory_unreadable: bool = False
    host_short: bool = False
    line: Optional[bytes] = None


@dataclass(frozen=True)
class Outcome:
    success: bool
    name: Optional[str]           # the denial's refusal name; None on SUCCESS
    detail: str
    line: Optional[Line]


def classify(obs: RunObservation, role: Role = IMPORTER,
             parse: Callable[[Optional[bytes]], Line] = parse_importer_line) -> Outcome:
    """The run's ONE classification (DESIGN §1 step 10, r10 S13-1). Order: (1) ``export-image-unavailable`` for an
    unreadable leaf, then ``role.memory``; SUCCESS or not; (2) ``role.space`` on a short host reading; (3)
    ``role.work_cap`` only on a plain non-zero exit with a valid line carrying ``enospc: true``; (4)
    ``role.failed:<reason>``, reason ``signal``/``time`` > ``result-oversize`` > ``result-malformed`` >
    ``role.missing`` > the importer's own refusal name > ``exit``."""
    if obs.memory_unreadable:
        return Outcome(False, "export-image-unavailable", "the run leaf's memory.events.local could not be read", None)
    if obs.memory_killed:
        return Outcome(False, role.memory, "", None)
    plain = obs.exit == "exit" and type(obs.code) is int
    line: Optional[Line] = None
    bad: Optional[LineBad] = None
    try:
        line = parse(obs.line)
    except LineBad as e:
        bad = e
    if line is not None and plain:
        if obs.code == 0 and not (line.result is not None and line.refused is None and not line.enospc
                                  and line.terminal):
            bad = LineBad("result-malformed", "an exit 0 line must carry a result, no refusal, enospc false")
        elif obs.code != 0 and line.result is not None:
            bad = LineBad("result-malformed", "a result on a non-zero exit")
        if bad is not None:
            line = None
    if plain and obs.code == 0 and line is not None:
        return Outcome(True, None, "", line)
    if obs.host_short:
        return Outcome(False, role.space, "", line)
    if plain and obs.code != 0 and line is not None and line.enospc:
        return Outcome(False, role.work_cap, line.refused or "", line)
    if not plain:
        reason, detail = (obs.exit if obs.exit in ("signal", "time") else "exit"), ""
    elif bad is not None:
        reason, detail = bad.reason, bad.detail
    elif line is not None and not line.terminal:
        reason, detail = role.missing, line.refused or ""
    elif line is not None and line.refused:
        reason, detail = line.refused, ""
    else:
        reason, detail = "exit", f"status {obs.code}"
    return Outcome(False, f"{role.failed}:{reason}", detail, line)


# --- seams ------------------------------------------------------------------------------------------------------

def _no_fault(point: str) -> None:
    return None


def _no_log(event: str) -> None:
    return None


def _store_delete(dfd: int, name: str) -> bool:
    from levain.slot import store_verbs as SV

    def _no_cas(*_a, **_k):
        raise AssertionError("delete_at writes no record")

    return SV.delete_at(dfd, name, SV.StoreSeams(cas=_no_cas), root_tree=True)


@dataclass(frozen=True)
class PublishSeams:
    """``rename2``: the commit call (:func:`rename2`). ``fsync``. ``delete(dfd, name)``: purge's delete (flags cleared,
    by dirfd ``unlinkat``, :func:`levain.slot.store_verbs.delete_at`). ``fault(point)``: may raise ``OSError`` at a
    named point (``copy:<name>``, ``fsync:<name>``, ``fsync:staged``, ``commit``, ``fsync:class``, ``delete-older``).
    ``log(event)``: the order log the DONE rows read."""

    rename2: Callable[[int, str, int, str, int], None] = rename2
    fsync: Callable[[int], None] = os.fsync
    delete: Callable[[int, str], bool] = _store_delete
    fault: Callable[[str], None] = _no_fault
    log: Callable[[str], None] = _no_log


_DIR_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
PROBE_NAMES = (".xchg-probe.a", ".xchg-probe.b")
STAGING_SUFFIX = ".new"
_CHUNK = 1 << 20


def _errname(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, str(e.errno))


def _publish_refusal(role: Role, detail: str) -> Refused:
    return Refused(f"{role.failed}:publish", detail)


# --- seal-in's two publish checks -------------------------------------------------------------------------------

def clear_staging(class_dfd: int, staged: str, role: Role, seams: PublishSeams) -> None:
    """DESIGN §1 step 6 (r12 S15-2, r13 S16-4): delete a stale ``<s>.new`` (or ``verify.new``) BEFORE the run, flags
    cleared first; one that cannot be deleted refuses here, never after the import."""
    try:
        if seams.delete(class_dfd, staged):
            seams.log(f"stale-staging deleted {staged}")
    except (OSError, Refused) as e:
        raise _publish_refusal(role, "stale-staging") from Refused(
            "stale-staging", f"{role.staged_help} could not be deleted ({e}); purge the session, or clear the fault "
            f"at that path as root")


def probe_exchange(class_dfd: int, role: Role, seams: PublishSeams) -> None:
    """DESIGN §1 step 6, the early exchange probe (r13 S16-3): run by the caller only when an older tree exists. Two
    scratch dirs whose names no session id can be, a crash's pair deleted first, the SAME exchange, both removed
    whatever the result. A refusal = ``<role>-failed:publish`` / ``exchange-unsupported`` before the import."""
    a, b = PROBE_NAMES
    for n in PROBE_NAMES:
        if seams.delete(class_dfd, n):
            seams.log(f"probe residue deleted {n}")
    try:
        os.mkdir(a, 0o700, dir_fd=class_dfd)
        os.mkdir(b, 0o700, dir_fd=class_dfd)
        seams.rename2(class_dfd, a, class_dfd, b, EXCHANGE)
        seams.log("probe exchange ok")
    except ExchangeUnsupported as e:
        raise _publish_refusal(role, "exchange-unsupported") from Refused(
            "exchange-unsupported", f"the export volume does not support atomic exchange ({e})")
    except OSError as e:
        raise _publish_refusal(role, _errname(e)) from e
    finally:
        for n in PROBE_NAMES:
            try:
                seams.delete(class_dfd, n)
            except (OSError, Refused):
                pass


# --- the outputs ------------------------------------------------------------------------------------------------

def check_outputs(work_dfd: int, names: Sequence[str], absent_ok: Sequence[str], *, owner_uid: int,
                  caps: Mapping[str, int]) -> Mapping[str, int]:
    """DESIGN §1 step 11: open ``out`` under ``work_dfd`` and each named output ``O_NOFOLLOW|O_NONBLOCK``; each must be
    regular, owned by ``owner_uid``, ``nlink == 1`` and within its cap (``export-output-shape:<check>``, closed:
    ``symlink``, ``nlink``, ``owner``, ``type``, ``size``). ``absent_ok``: the names that must be ABSENT (the bundle
    on ``adopt-nothing-new``); one present, or a required one absent, = ``export-result-mismatch``. Returns name ->
    an open fd (the caller closes them)."""
    fds: dict = {}

    def shape(check: str, what: str) -> Refused:
        return Refused(f"export-output-shape:{check}", what)    # the handler below closes every fd, once

    try:
        out = os.open("out", _DIR_OPEN, dir_fd=work_dfd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise Refused("export-result-mismatch", "no out/") from None
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            # a symlink under O_DIRECTORY|O_NOFOLLOW: ENOTDIR measured on darwin (test_output_shapes); ELOOP per
            # open(2)'s O_NOFOLLOW text elsewhere [judged by adopt, 10-22, against open(2)]: lstat names either
            st = os.lstat("out", dir_fd=work_dfd)
            raise shape("symlink" if stat.S_ISLNK(st.st_mode) else "type", "out") from None
        raise
    try:
        for name in tuple(names) + tuple(absent_ok):
            try:
                fd = os.open(name, _LEAF_OPEN, dir_fd=out)
            except OSError as e:
                if e.errno == errno.ENOENT and name in absent_ok:
                    continue
                if e.errno == errno.ENOENT:
                    raise Refused("export-result-mismatch", f"out/{name} missing") from None
                if e.errno == errno.ELOOP:
                    raise shape("symlink", name) from None
                raise
            fds[name] = fd
            if name in absent_ok:
                raise Refused("export-result-mismatch", f"out/{name} present") from None
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise shape("type", name)
            if st.st_nlink != 1:
                raise shape("nlink", name)
            if st.st_uid != owner_uid:
                raise shape("owner", name)
            if st.st_size > caps[name]:
                raise shape("size", name)
    except Refused:
        for fd in fds.values():
            os.close(fd)
        fds.clear()
        raise
    finally:
        os.close(out)
    return fds


def importer_bundle_check(session: str, object_format: str) -> Callable[[BF.BundleHeader], None]:
    """Step 11's header check (§4.12): the signature of ``alloc.base.object_format``; refs only
    ``refs/levain/<s>/{head,wip}``, each once, at least one (``bundle-header-mismatch``). Prerequisites are already
    well-formed oids of the format by the parser."""
    allowed = {f"refs/levain/{session}/head".encode(), f"refs/levain/{session}/wip".encode()}

    def check(h: BF.BundleHeader) -> None:
        if h.object_format != object_format:
            raise Refused(BF.MISMATCH, f"object format {h.object_format}, alloc.base says {object_format}")
        names = [n for _oid, n in h.refs]
        if not names or len(set(names)) != len(names) or not set(names) <= allowed:
            raise Refused(BF.MISMATCH, "refs other than refs/levain/<s>/{head,wip}, each once, at least one")
    return check


# --- the transaction --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Published:
    digests: Mapping[str, str]          # name -> sha256 hex of the bytes root copied
    header: Optional[BF.BundleHeader]   # the bundle's, when one was copied
    exchanged: bool                     # True = an older tree was replaced


def transact(class_dfd: int, session: str, sources: Sequence[Tuple[str, int]], role: Role, seams: PublishSeams, *,
             gid: Optional[int] = None, bundle_check: Optional[Callable[[BF.BundleHeader], None]] = None,
             before_commit: Optional[Callable[[], None]] = None) -> Published:
    """The PUBLISH TRANSACTION, entered only after SUCCESS (DESIGN §1 step 11, r11 S14-1): the copy into a fresh
    ``<s>.new`` (root, ``gid``, files 0640 ``O_CREAT|O_EXCL``), the fsyncs, the commit. ENOSPC or EDQUOT before the
    commit = ``role.space``; any other errno = ``role.failed:publish`` (detail: the errno name; ``exists`` for the
    no-replace call's EEXIST; ``exchange-unsupported`` for a refused commit call); a bundle header off the check =
    ``bundle-header-mismatch``. In each, ``<s>.new`` is removed and ``<s>`` is as it was.

    Order (r12 S15-3), asserted by the log: ``fsync <name>`` per file, ``fsync staged``, ``exchange`` or
    ``noreplace``, ``fsync class``, ``delete older``. ``before_commit`` runs between ``fsync staged`` and the commit
    (5c-ii-b's detach); the caller makes it never raise. After the commit call returns, the run is COMMITTED: a
    failed class-dir fsync or older-tree delete is logged and does not withhold the answer (an older tree left at
    ``<s>.new`` is the next seal-in's stale delete)."""
    staged = session + STAGING_SUFFIX
    stage = "staging"
    made = False
    new_dfd = -1
    digests: dict = {}
    header: Optional[BF.BundleHeader] = None
    exchanged = False
    try:
        os.mkdir(staged, 0o750, dir_fd=class_dfd)
        made = True
        new_dfd = os.open(staged, _DIR_OPEN, dir_fd=class_dfd)
        if gid is not None:
            os.fchown(new_dfd, 0, gid)
        for name, src in sources:
            seams.fault(f"copy:{name}")
            dst = os.open(name, _NEW_FILE, 0o640, dir_fd=new_dfd)
            try:
                if gid is not None:
                    os.fchown(dst, 0, gid)
                h = hashlib.sha256()
                obs = BF.HeaderObserver(bundle_check) if name == "bundle" else None
                while True:
                    chunk = os.read(src, _CHUNK)
                    if obs is not None:
                        obs.feed(chunk)
                    if not chunk:
                        break
                    h.update(chunk)
                    write_all(dst, chunk)
                if obs is not None:
                    header = obs.finish()
                seams.fault(f"fsync:{name}")
                seams.fsync(dst)
                seams.log(f"fsync {name}")
            finally:
                os.close(dst)
            digests[name] = h.hexdigest()
        seams.fault("fsync:staged")
        seams.fsync(new_dfd)
        seams.log("fsync staged")
        if before_commit is not None:
            before_commit()
        stage = "commit"
        seams.fault("commit")
        try:
            older = os.lstat(session, dir_fd=class_dfd)
        except FileNotFoundError:
            older = None
        if older is None:
            seams.rename2(class_dfd, staged, class_dfd, session, NOREPLACE)
            seams.log("noreplace")
        else:
            seams.rename2(class_dfd, staged, class_dfd, session, EXCHANGE)
            exchanged = True
            seams.log("exchange")
        stage = "committed"
    except ExchangeUnsupported as e:
        _remove_staged(class_dfd, staged, made, seams)
        raise _publish_refusal(role, "exchange-unsupported") from Refused(
            "exchange-unsupported", f"the export volume does not support atomic exchange ({e})")
    except Refused:
        _remove_staged(class_dfd, staged, made, seams)
        raise
    except OSError as e:
        _remove_staged(class_dfd, staged, made, seams)
        if stage == "commit" and e.errno == errno.EEXIST:
            raise _publish_refusal(role, "exists") from e
        if e.errno in SPACE_ERRNOS:
            raise Refused(role.space, _errname(e)) from e
        raise _publish_refusal(role, _errname(e)) from e
    finally:
        if new_dfd >= 0:
            os.close(new_dfd)
    try:
        seams.fault("fsync:class")
        seams.fsync(class_dfd)
        seams.log("fsync class")
    except OSError as e:
        seams.log(f"fsync class failed {_errname(e)}")
    if exchanged:
        try:
            seams.fault("delete-older")
            seams.delete(class_dfd, staged)
            seams.log("delete older")
        except (OSError, Refused) as e:
            seams.log(f"delete older failed {e}")
    return Published(digests, header, exchanged)


def _remove_staged(class_dfd: int, staged: str, made: bool, seams: PublishSeams) -> None:
    if not made:
        return
    try:
        seams.delete(class_dfd, staged)
        seams.log("staged removed")
    except (OSError, Refused) as e:
        seams.log(f"staged left {e}")


def publish_if_success(outcome: Outcome, work_dfd: int, class_dfd: int, session: str, *, object_format: str,
                       owner_uid: int, caps: Mapping[str, int], seams: PublishSeams, gid: Optional[int] = None,
                       before_commit: Optional[Callable[[], None]] = None) -> Tuple[str, Published]:
    """Seal-out's ONE gate into the publish (DESIGN §1 step 11, r10 S13-1): a DENIAL raises its name and touches
    nothing in the class dir; SUCCESS runs the output checks and the transaction. Returns ``(result, published)``,
    ``result`` derived from the copied header (prerequisites = ``delta``, none = ``unrelated``; root never
    evaluates ancestry) or ``adopt-nothing-new``."""
    if not outcome.success or outcome.line is None:
        raise Refused(outcome.name or f"{IMPORTER.failed}:exit", outcome.detail)
    nothing_new = outcome.line.result == "adopt-nothing-new"
    names = ("report.json",) if nothing_new else ("bundle", "report.json")
    fds = check_outputs(work_dfd, names, ("bundle",) if nothing_new else (), owner_uid=owner_uid, caps=caps)
    try:
        pub = transact(class_dfd, session, [(n, fds[n]) for n in names], IMPORTER, seams, gid=gid,
                       bundle_check=importer_bundle_check(session, object_format), before_commit=before_commit)
    finally:
        for fd in fds.values():
            os.close(fd)
    if nothing_new:
        return "adopt-nothing-new", pub
    assert pub.header is not None
    return ("delta" if pub.header.prerequisites else "unrelated"), pub
