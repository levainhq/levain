"""§5.6 (session 2f, S14 piece A2a): a verifier session's ``start`` as root, from the clone to the last step's evidence.

ROOT-SIDE. It runs in the helper as root and is a trust and confinement boundary: python 3.9-valid, stdlib plus
``levain.slot.*`` and ``levain.contracts.canon`` only, never ``levain.dispatch``, and it fails closed (a stream or
an exit root could not record is a refusal, never a default). :mod:`levain.slot.start` imports it at the top, so it
joins the root closure on its own.

What this module does, for each ticket step ``k`` (0-based, the report's numbering), in a FRESH kill domain:

1. under the session END lock, re-read the record and refuse unless it is ACTIVE; create the domain (Linux: the
   leaf cgroup ``step<k>``); spawn the ``step`` target (the child uid, cwd ``<S>/repo``) with stdin ``/dev/null`` and
   stdout and stderr on pipes root holds; release the lock (D2: the lock is held per spawn, never for the wait);
2. drain the two pipes into ``<S>/run/step<k>.out`` and ``.err`` (root : ``_levain_v<n>`` 0640) up to
   :data:`STREAM_CAP` bytes each, counting the rest as ``truncated_bytes`` (D4: the cap is the shipped driver's own
   ``MAX_CAPTURE_BYTES``, 1 MiB, so a capture root wrote is one the driver accepts), until the step process exits or
   its ``wall_s`` is spent;
3. under the lock again: kill the WHOLE domain (a step that daemonizes is ended before step ``k + 1`` can run),
   prove it empty within V12 (else ``step-unkillable``), then write ``step<k>.exit`` with the contract's six keys,
   fsynced, and only then go to step ``k + 1``.

Then (A2b), once every step's evidence is durable, :func:`finish_after_steps`: the driver in its own leaf, its end
classified (``driver-timeout``, ``driver-signal:<n>``, ``driver-exit:<n>``, ``report-too-large``,
``report-not-canonical``, ``report-format``), root's ENVELOPE written canonical to the export dir, and the
field-only CAS of ``verifier_report_digest``. The driver's body is carried opaque.
"""
from __future__ import annotations

import hashlib
import os
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from math import ceil
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import levain
from levain.contracts import canon
from levain.slot import Refused, write_all
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import runtime_helper as H
from levain.slot.root_tree import ROOT_PYTHON

#: D4: root's per-stream capture cap, equal to ``verifier_drivers.output_compare.MAX_CAPTURE_BYTES`` (a test pins
#: the two). The driver refuses a longer capture as "not one root wrote", so root never writes one.
STREAM_CAP = 1 << 20

#: the final reads of a pipe after its domain is proven empty: nothing can write any more, so this reads to EOF, and
#: the bound is only a backstop against a misbehaving fd.
_FINAL_READ_LIMIT = 1 << 14
_REAP_GRACE_S = 5.0
_READ = 65536

RUN_DIR = "run"
#: the report fields; a start that finds either set refuses ``already-started``
REPORT_FIELDS = ("verifier_report_digest", "verifier_report_refused")

_O_FILE_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

#: The reap of the step's own child. A module attribute so the fake process table of a test can stand in for the host;
#: production is :func:`os.waitpid`.
WAITPID: Callable[[int, int], Tuple[int, int]] = os.waitpid


@dataclass(frozen=True)
class Hooks:
    """What :func:`run` takes from :mod:`levain.slot.start` (passed in, so this module never imports it back).

    ``state()``: the session's record now (None = none). ``clone()``: the guards, the fresh repo, the clone and its
    (a) check, the clone marker; called under the lock; raises ``Refused`` (``clone-failed`` ...). ``launch(target,
    argv, leaf)``: ``(launch argv, spec json, cgroup)`` for ``target`` (``step`` or, for A2b, ``driver``) run in the
    cgroup leaf ``leaf`` (Linux; macOS None)."""

    state: Callable[[], Optional[R.SessionState]]
    clone: Callable[[], None]
    launch: Callable[[str, Sequence[str], Optional[str]], Tuple[Tuple[str, ...], bytes, Optional[str]]]
    wake: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class StepsResult:
    """Every step's ``.exit`` record, in order, all durable under ``<S>/run/``."""

    exits: Tuple[Dict[str, object], ...]


#: how long a spawn waits for the session END lock (a try-lock: held = ``session-busy``) before it refuses. END holds it
#: for a kill-and-prove, never for a wait, so a held lock is a brief one; the record is re-read at every try.
LOCK_WAIT_S = 30.0


@contextmanager
def _locked(ctx: H.Ctx, a: R.Alloc) -> Iterator[None]:
    """The session END lock for one spawn, one kill-and-prove or one envelope write (D2), waited for within
    :data:`LOCK_WAIT_S` (``session-busy`` after)."""
    seams = ctx.seams
    end = seams.monotonic() + LOCK_WAIT_S
    stack = ExitStack()
    while True:
        try:
            stack.enter_context(H.session_lock(ctx, a.session))
            break
        except Refused as e:
            if e.name != "session-busy" or seams.monotonic() >= end:
                raise
            seams.sleep(0.05)
    with stack:
        yield


# --- the record --------------------------------------------------------------------------------------------------------

def _state_name(rec: Optional[R.SessionState]) -> str:
    return rec.state if rec is not None else "none"


def _require_active(hooks: Hooks, a: R.Alloc) -> R.SessionState:
    """Under the lock: the record is ACTIVE, else no process starts (a REVOKE between two steps starts no step ``k+1``)."""
    rec = hooks.state()
    if rec is None or rec.state != R.ACTIVE:
        raise Refused("not-active", f"{a.session}: {_state_name(rec)}")
    return rec


def refuse_field(ctx: H.Ctx, a: R.Alloc, name: str, rec: Optional[R.SessionState]) -> None:
    """A field-only CAS of ``verifier_report_refused: name`` on the record's own state, best effort: a CONTROL that
    refuses it (the edge is not one A0 allows, or the record moved) is noted and never replaces the refusal the caller
    raises. END's own fallback records ``no-report:<state>`` when this one did not land."""
    if rec is None:
        return
    try:
        ctx.call("CAS", session=a.session, generation=rec.generation, **{"from": rec.state, "to": rec.state},
                 fields={"verifier_report_refused": name})
    except Exception as e:   # noqa: BLE001 - the caller's refusal is the answer
        text = e.name if isinstance(e, Refused) else type(e).__name__
        _note(f"{a.session}: verifier_report_refused:{name} not recorded: {text}")


def _note(msg: str) -> None:
    try:
        sys.stderr.write(f"levain runtime-helper: {msg}\n")
    except Exception:   # noqa: BLE001 - a note never changes the verb's answer
        pass


# --- files under <S>/run -------------------------------------------------------------------------------------------------

def _make_run_dir(ctx: H.Ctx, a: R.Alloc) -> None:
    """``<S>/run/`` fresh, root : ``_levain_v<n>`` 0750: the driver reads the evidence, the child (the step's uid) can
    neither list nor write it. An existing name of any kind is ``session-exists`` (nothing is written through one)."""
    rel = f"live/{a.session}/{RUN_DIR}"
    with ctx.store.dir("live", a.session) as sfd:
        ctx.store.mkdir(sfd, RUN_DIR, 0, a.slot_gid, 0o750, rel)
        ctx.seams.fsync(sfd)


def _create(ctx: H.Ctx, a: R.Alloc, rfd: int, name: str) -> int:
    rel = f"live/{a.session}/{RUN_DIR}/{name}"
    try:
        fd = os.open(name, _O_FILE_NEW, 0o600, dir_fd=rfd)
    except FileExistsError:
        raise Refused("render-dest-exists", rel) from None
    try:
        ctx.seams.chown(fd, 0, a.slot_gid, rel)
        os.fchmod(fd, 0o640)
    except BaseException:
        os.close(fd)
        raise
    return fd


# --- the drain -------------------------------------------------------------------------------------------------------------

class _Stream:
    """One pipe drained into one file: at most :data:`STREAM_CAP` bytes written, the rest read and counted. A failed
    write stops that stream's writes (the rest counted as dropped) and keeps draining, so the child never blocks on a
    full pipe."""

    def __init__(self, rfd: int, ffd: int, cap: Optional[int] = None, keep: bool = False) -> None:
        self.r, self.f = rfd, ffd
        self.cap = cap                  # None: STREAM_CAP, read at each take (a test moves it)
        self.keep = keep                # True: the kept bytes stay in ``buf`` (the driver's stdout), not in a file
        self.buf = bytearray()
        self.written = 0
        self.dropped = 0
        self.live = True
        self.broken = False
        os.set_blocking(rfd, False)

    def take(self) -> bool:
        """One read; False at EOF or when nothing is ready."""
        if not self.live:
            return False
        try:
            b = os.read(self.r, _READ)
        except BlockingIOError:
            return False
        if not b:
            self.live = False
            return False
        cap = STREAM_CAP if self.cap is None else self.cap
        room = 0 if self.broken else max(0, min(len(b), cap - self.written))
        if room:
            try:
                if self.keep:
                    self.buf += b[:room]
                else:
                    write_all(self.f, b[:room])
                self.written += room
            except OSError:
                self.broken = True
                room = 0
        self.dropped += len(b) - room
        return True

    def finish(self) -> None:
        """The domain is proven empty: read what is left to EOF (bounded)."""
        for _ in range(_FINAL_READ_LIMIT):
            if not self.take():
                break


def _status(status: int) -> Tuple[Optional[int], Optional[int]]:
    if os.WIFSIGNALED(status):
        return None, os.WTERMSIG(status)
    return os.WEXITSTATUS(status), None


# --- the kill domain -------------------------------------------------------------------------------------------------------

def _proc_seams(ctx: H.Ctx) -> PR.ProcSeams:
    return ctx.seams.proc_seams or PR.default_seams(ctx.seams.host)   # type: ignore[return-value]


def _lingering(ctx: H.Ctx, a: R.Alloc, pid: int) -> int:
    """The processes of the step's uid other than the step's own main process, alive now: what the step left behind."""
    assert a.child_uid is not None
    seams = _proc_seams(ctx)
    return sum(1 for p in seams.listing()
               if p.pid != pid and a.child_uid in p.uids and p.state != PR.ZOMBIE)


def _kill_domain(ctx: H.Ctx, a: R.Alloc, cgroup: Optional[str], pid: Optional[int], uid: Optional[int] = None) -> bool:
    """The step's WHOLE domain ended and proven empty within V12. Linux: ``cgroup.kill`` on the step's leaf, proven by
    ``populated 0`` and the child uid's sweep (:class:`levain.slot.proc.CgroupProcs`). macOS: the child uid's
    :func:`levain.slot.proc.end_domain` (``pid``, when it is still the helper's unreaped child, is its reaped child).
    ``uid`` is the domain's account: the child's for a step (the default), the slot's for the driver. False = not
    proven."""
    uid = a.child_uid if uid is None else uid
    assert uid is not None
    seams = ctx.seams
    try:
        if seams.host == "linux":
            assert cgroup is not None
            _, kill, _ = PR.end_seams("linux", cgroup, seams.proc_seams)   # type: ignore[arg-type]
            return bool(kill(uid, PR.V12_SECONDS))
        res, _ = PR.end_domain(uid, PR.V12K_SECONDS, () if pid is None else (pid,), seams=_proc_seams(ctx))
        return res.proven
    except Refused as e:
        _note(f"{a.session}: step domain kill: {e.name}")
        return False


def _reap(ctx: H.Ctx, pid: int) -> int:
    """The step's own child reaped, polled (never blocking past :data:`_REAP_GRACE_S`): its wait status. A child that is
    not reapable is a refusal, never a guess."""
    seams = ctx.seams
    end = seams.monotonic() + _REAP_GRACE_S
    while True:
        try:
            done, status = WAITPID(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            raise Refused("step-unreaped", f"pid {pid} is not the helper's child") from None
        if done:
            return status
        if seams.monotonic() >= end:
            raise Refused("step-unreaped", f"pid {pid} was not reaped within {_REAP_GRACE_S:g}s")
        seams.sleep(0.01)


# --- one step ----------------------------------------------------------------------------------------------------------------

def run_step(ctx: H.Ctx, a: R.Alloc, hooks: Hooks, k: int, argv: Sequence[str], wall_s: int) -> Dict[str, object]:
    """Step ``k``: spawn, drain within ``wall_s``, kill and prove the whole domain, write ``step<k>.exit``; returns the
    record. Raises ``Refused`` (``not-active``, ``step-unkillable``, ...) with no record written for a step whose domain
    was not proven empty."""
    seams = ctx.seams
    leaf = f"step{k}" if seams.host == "linux" else None
    out_r = out_w = err_r = err_w = -1
    out_f = err_f = -1
    pid: Optional[int] = None
    cgroup: Optional[str] = None
    try:
        with ctx.store.dir("live", a.session, RUN_DIR) as rfd:
            with _locked(ctx, a):
                _require_active(hooks, a)       # before any file: a REVOKED session leaves nothing of step k
                out_f = _create(ctx, a, rfd, f"step{k}.out")
                err_f = _create(ctx, a, rfd, f"step{k}.err")
                out_r, out_w = os.pipe()
                err_r, err_w = os.pipe()
                seams.at("step")
                largv, spec_json, cgroup = hooks.launch("step", argv, leaf)
                if cgroup is not None:
                    seams.make_cgroup(cgroup)
                start = seams.monotonic()
                try:
                    pid = seams.spawn(largv, spec_json, cgroup, None, False, stdin_null=True,
                                      stdout_w=out_w, stderr_w=err_w)
                finally:
                    os.close(out_w)
                    os.close(err_w)
                    out_w = err_w = -1
            streams = [_Stream(out_r, out_f), _Stream(err_r, err_f)]
            timed_out, status = _wait(ctx, pid, streams, start + wall_s)
            with _locked(ctx, a):
                lingering = _lingering(ctx, a, pid) if not timed_out else 0
                proven = _kill_domain(ctx, a, cgroup, pid if timed_out and seams.host != "linux" else None)
                if not proven:
                    rec = hooks.state()
                    if rec is not None and rec.state == R.ACTIVE:
                        refuse_field(ctx, a, "step-unkillable", rec)
                    raise Refused("step-unkillable", f"step {k}: the domain was not proven empty")
                if status is None:
                    # the main process was reaped elsewhere: the domain is proven empty, but no exit status is root's
                    # to record, so no .exit is written and the step is refused by name (never a raw exception)
                    rec = hooks.state()
                    if rec is not None and rec.state == R.ACTIVE:
                        refuse_field(ctx, a, "step-unreaped", rec)
                    raise Refused("step-unreaped", f"step {k}: pid {pid} is not the helper's child")
                if timed_out:
                    # the main process died of the domain's kill; it is the helper's child still, reaped now
                    status = _reap(ctx, pid)
                    pid = None
                for s in streams:
                    s.finish()
                code, sig = _status(status)
                rec_exit: Dict[str, object] = {
                    "exit": code, "signal": sig, "wall_s": max(0, int(ceil(seams.monotonic() - start))),
                    "timed_out": timed_out, "truncated_bytes": streams[0].dropped + streams[1].dropped,
                    "lingering_killed": lingering}
                for fd in (out_f, err_f):
                    seams.fsync(fd)
                efd = _create(ctx, a, rfd, f"step{k}.exit")
                try:
                    write_all(efd, canon.canonical_bytes(rec_exit))
                    seams.fsync(efd)
                finally:
                    os.close(efd)
                seams.fsync(rfd)
                return rec_exit
    finally:
        for fd in (out_r, out_w, err_r, err_w, out_f, err_f):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _wait(ctx: H.Ctx, pid: int, streams: List[_Stream], deadline: float) -> Tuple[bool, Optional[int]]:
    """Drain until the step's main process exits (``(False, wait status)``) or ``deadline`` passes (``(True, 0)``: the
    caller kills the domain and reaps). Never until the pipes' EOF: a process the step left behind can hold a write end
    open. ``(False, None)``: ``waitpid`` answered ``ChildProcessError`` (the process is not, or no longer, this helper's
    child: reaped elsewhere, or SIGCHLD ignored). It is treated as already reaped: the caller still kills and proves the
    whole domain, then refuses ``<target>-unreaped`` with no status to record (S14 fix 6)."""
    seams = ctx.seams
    nap = 0.001
    while True:
        try:
            done, status = WAITPID(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return False, None
        if done:
            return False, status
        if seams.monotonic() >= deadline:
            return True, 0
        if any([s.take() for s in streams]):
            nap = 0.001
            continue
        seams.sleep(nap)
        nap = min(nap * 2, 0.05)


# --- the verifier start ------------------------------------------------------------------------------------------------------

#: V26 (CONTRACT §11): the driver's own wall, seconds.
DRIVER_WALL_S = 600
#: V21: the report's cap (the driver's stdout); one byte more is read, so a longer one is seen and refused.
REPORT_CAP = 1 << 20
#: the entry's keys the ENVELOPE carries (CONTRACT §3.5 VERIFIER IDENTITY): alloc's resolved entry also carries
#: ``verifier_generation``, which the contract's envelope does not. Root keeps its own tuple; a test pins it equal to
#: ``levain.dispatch.verifier_report.VERIFIER_KEYS``.
ENVELOPE_VERIFIER_KEYS = ("declared_kind", "driver_input_digest", "driver_template", "driver_template_digest",
                          "steps_digest", "verifier_artifact_digest", "verifier_id")
EXPORT_GROUP = "levain-dispatch-export"
REPORT_NAME = "verifier-report.json"
DRIVER_DIR = "driver"
INPUT_NAME = "input.json"


def _driver_path(template: str) -> str:
    """``<import tree>/levain/verifier_drivers/<template>.py``: the tree this interpreter imports ``levain`` from (the
    file render digest-checked, and :func:`finish_after_steps` checks again just before the spawn; the name was
    grammar-checked at mint and again by render)."""
    return os.path.join(os.path.dirname(os.path.abspath(levain.__file__)), "verifier_drivers", template + ".py")


def run_driver(ctx: H.Ctx, a: R.Alloc, hooks: Hooks, argv: Sequence[str], wall_s: int
               ) -> Tuple[bool, Optional[int], Optional[int], bytes, int]:
    """The driver in its own leaf: spawn (under the lock, ACTIVE only), drain within ``wall_s`` (stdout kept up to
    :data:`REPORT_CAP` + 1 bytes, stderr into ``<S>/run/driver.err`` up to :data:`STREAM_CAP`), kill and prove its
    domain empty, reap. Returns ``(timed_out, exit, signal, stdout, stdout_len_seen)``. A domain not proven empty is
    ``driver-unkillable``."""
    seams = ctx.seams
    leaf = DRIVER_DIR if seams.host == "linux" else None
    out_r = out_w = err_r = err_w = err_f = -1
    pid: Optional[int] = None
    cgroup: Optional[str] = None
    try:
        with ctx.store.dir("live", a.session, RUN_DIR) as rfd:
            with _locked(ctx, a):
                _require_active(hooks, a)
                err_f = _create(ctx, a, rfd, "driver.err")
                out_r, out_w = os.pipe()
                err_r, err_w = os.pipe()
                seams.at("driver")
                largv, spec_json, cgroup = hooks.launch("driver", argv, leaf)
                if cgroup is not None:
                    seams.make_cgroup(cgroup)
                start = seams.monotonic()
                try:
                    pid = seams.spawn(largv, spec_json, cgroup, None, False, stdin_null=True,
                                      stdout_w=out_w, stderr_w=err_w)
                finally:
                    os.close(out_w)
                    os.close(err_w)
                    out_w = err_w = -1
            streams = [_Stream(out_r, -1, REPORT_CAP + 1, keep=True), _Stream(err_r, err_f)]
            timed_out, status = _wait(ctx, pid, streams, start + wall_s)
            with _locked(ctx, a):
                if not _kill_domain(ctx, a, cgroup, pid if timed_out and seams.host != "linux" else None,
                                    a.slot_uid):
                    rec = hooks.state()
                    if rec is not None and rec.state == R.ACTIVE:
                        refuse_field(ctx, a, "driver-unkillable", rec)
                    raise Refused("driver-unkillable", "the driver's domain was not proven empty")
                if status is None:
                    # reaped elsewhere (see _wait): proven empty, but no exit status to classify
                    rec = hooks.state()
                    if rec is not None and rec.state == R.ACTIVE:
                        refuse_field(ctx, a, "driver-unreaped", rec)
                    raise Refused("driver-unreaped", f"pid {pid} is not the helper's child")
                if timed_out:
                    status = _reap(ctx, pid)
                    pid = None
                for st in streams:
                    st.finish()
                seams.fsync(err_f)
                seams.fsync(rfd)
                code, sig = (None, None) if timed_out else _status(status)
                return timed_out, code, sig, bytes(streams[0].buf), streams[0].written + streams[0].dropped
    finally:
        for fd in (out_r, out_w, err_r, err_w, err_f):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def classify(timed_out: bool, code: Optional[int], sig: Optional[int], out: bytes) -> Tuple[Optional[str], object]:
    """The driver's end, in the contract's order: ``(refusal name, None)``, or ``(None, the parsed body)``. The body is
    carried OPAQUE: root checks only that it is one canonical JSON object within the cap, never what it says."""
    if timed_out:
        return "driver-timeout", None
    if sig is not None:
        return f"driver-signal:{sig}", None
    if code != 0:
        return f"driver-exit:{code}", None
    if len(out) > REPORT_CAP:
        return "report-too-large", None
    try:
        body = canon.parse(out)
        again = canon.canonical_bytes(body)
    except Exception:   # noqa: BLE001 - whatever the bytes are, they are not a report root can carry
        return "report-not-canonical", None
    if again != out:
        return "report-not-canonical", None
    if not isinstance(body, dict):
        return "report-format", None
    return None, body


def _refuse(ctx: H.Ctx, a: R.Alloc, hooks: Hooks, name: str, detail: str = "") -> None:
    """The field-only CAS of ``verifier_report_refused: name`` under the lock, then the refusal."""
    try:
        with _locked(ctx, a):
            refuse_field(ctx, a, name, hooks.state())
    except Refused as e:
        _note(f"{a.session}: verifier_report_refused:{name} not recorded: {e.name}")
    raise Refused(name, detail or a.session)


def _write_envelope(ctx: H.Ctx, a: R.Alloc, raw: bytes) -> str:
    """``L/export/dispatch/<session>/verifier-report.json``: the dir root : ``levain-dispatch-export`` 0750 if absent;
    the file a ``O_EXCL`` temp (0640), fsynced, renamed, the dir fsynced; never over an existing report. Returns the
    sha256 of the file's bytes."""
    seams = ctx.seams
    gid = seams.group_gid(EXPORT_GROUP)
    rel = f"export/dispatch/{a.session}"
    with ctx.store.dir("export", "dispatch") as pfd:
        try:
            ctx.store.mkdir(pfd, a.session, 0, gid, 0o750, rel)
        except Refused as e:
            if e.name != "session-exists":
                raise
        seams.fsync(pfd)
    with ctx.store.dir("export", "dispatch", a.session) as dfd:
        try:
            os.stat(REPORT_NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused("report-exists", f"{rel}/{REPORT_NAME}")
        tmp = f".{REPORT_NAME}.{os.getpid()}"
        try:
            os.unlink(tmp, dir_fd=dfd)          # a leftover of a crashed attempt of this pid
        except FileNotFoundError:
            pass
        fd = os.open(tmp, _O_FILE_NEW, 0o600, dir_fd=dfd)
        try:
            seams.chown(fd, 0, gid, f"{rel}/{REPORT_NAME}")
            os.fchmod(fd, 0o640)
            write_all(fd, raw)
            seams.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, REPORT_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        seams.fsync(dfd)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def finish_after_steps(ctx: H.Ctx, a: R.Alloc, hooks: Hooks, res: StepsResult) -> Dict[str, object]:
    """Every step's evidence is durable under ``<S>/run/``: the driver (leaf ``driver``, the ``driver`` target, wall
    :data:`DRIVER_WALL_S`), its end classified, root's ENVELOPE written canonical to the export dir, and the field-only
    CAS (ACTIVE) of ``verifier_report_digest``. Any refusal is CASed as ``verifier_report_refused`` and raised; the
    answer is ``{"exit": 0}`` (the driver's own exit)."""
    facts = a.ticket_facts or {}
    entry = facts.get("verifier")
    if not isinstance(entry, dict) or any(k not in entry for k in ENVELOPE_VERIFIER_KEYS) \
            or "driver_input_digest" not in facts or "input_bundle_digest" not in facts or a.ticket is None:
        raise Refused("not-rendered", f"{a.session}: the alloc carries no resolved verifier entry")
    template = entry["driver_template"]
    if type(template) is not str:
        raise Refused("not-rendered", f"{a.session}: the entry's driver_template")
    store = str(ctx.store.path)
    argv = (ROOT_PYTHON[ctx.seams.host], "-I", "-S", _driver_path(template),
            os.path.join(store, "live", a.session, DRIVER_DIR, INPUT_NAME),
            os.path.join(store, "live", a.session, RUN_DIR))
    # S14 fix 6 (L2 L2): render checked the template's digest, but the steps ran for up to their walls since; an
    # upgrade of the import tree in that window would run a driver the envelope does not name, so it is checked again
    # immediately before the spawn
    try:
        RD.check_driver_template(entry, ctx.seams.driver_template)
    except Refused as e:
        _refuse(ctx, a, hooks, e.name)
    timed_out, code, sig, out, _seen = run_driver(ctx, a, hooks, argv, DRIVER_WALL_S)
    name, body = classify(timed_out, code, sig, out)
    if name is not None:
        _refuse(ctx, a, hooks, name)
    envelope = {"session": a.session, "ticket": a.ticket,
                "verifier": {k: entry[k] for k in ENVELOPE_VERIFIER_KEYS},
                "driver_input_digest": facts["driver_input_digest"],
                "input_bundle_digest": facts["input_bundle_digest"],
                "steps": [dict(e) for e in res.exits], "body": body}
    raw = canon.canonical_bytes(envelope)
    ctx.seams.at("envelope")
    with _locked(ctx, a):
        rec = _require_active(hooks, a)
        digest = _write_envelope(ctx, a, raw)
        ctx.call("CAS", session=a.session, generation=rec.generation, **{"from": R.ACTIVE, "to": R.ACTIVE},
                 fields={"verifier_report_digest": digest})
    return {"exit": 0}


def run(ctx: H.Ctx, a: R.Alloc, hooks: Hooks) -> Dict[str, object]:
    """``start`` for kind verifier (§5.6): the guards, the clone, RENDERED -> ACTIVE, then each step in order. The
    refusals of the clone and of an unkillable step leave a ``verifier_report_refused`` field where CONTROL accepts one;
    the runtime ENDs the session (no retry for a verifier)."""
    steps = a.policy.verifier_steps if a.policy is not None else None
    if not steps:
        raise Refused("not-rendered", f"{a.session}: a verifier session rendered with no steps")
    seams = ctx.seams
    seams.at("repo")
    try:
        with _locked(ctx, a):
            if H.read_alloc(ctx, a.session) != a:
                raise Refused("not-rendered", f"{a.session}: the alloc changed")
            rec = hooks.state()
            if rec is not None and any(f in rec.fields for f in REPORT_FIELDS):
                raise Refused("already-started", a.session)
            try:
                hooks.clone()           # raises already-started / not-rendered / clone-failed
            except Refused as e:
                if e.name == "clone-failed":
                    refuse_field(ctx, a, "clone-failed", hooks.state())
                raise
            _make_run_dir(ctx, a)
            seams.at("active")
            ctx.call("CAS", session=a.session, generation=H.state_generation(ctx, a.session),
                     **{"from": R.RENDERED, "to": R.ACTIVE}, fields={})
    finally:
        hooks.wake()
    exits: List[Dict[str, object]] = []
    try:
        for k, step in enumerate(steps):
            exits.append(run_step(ctx, a, hooks, k, step.argv, step.wall_s))
    finally:
        hooks.wake()
    seams.at("steps-done")
    return finish_after_steps(ctx, a, hooks, StepsResult(tuple(exits)))


__all__ = ["DRIVER_WALL_S", "ENVELOPE_VERIFIER_KEYS", "REPORT_CAP", "Hooks", "REPORT_FIELDS", "RUN_DIR", "STREAM_CAP", "StepsResult", "WAITPID", "finish_after_steps",
           "classify", "refuse_field", "run", "run_driver", "run_step"]
