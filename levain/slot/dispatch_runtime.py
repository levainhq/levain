"""levain.slot.dispatch_runtime: the production :class:`levain.agents.session_launcher.SlotRuntime` and
:class:`~levain.agents.session_launcher.ControlPort` for a subscription session on an S1 dispatch slot (levain 27, slice
S10c; design ``1104-27_S10_DESIGN.md`` sections 3.1, 3.3, 3.4). Both run as ``_levain_dispatch`` inside the run server
(S10b); this module builds neither the server nor the operator-side client.

* :class:`DispatchControlPort` is the mint, cancel and status entry of ``RealLauncher``: ONE
  :class:`~levain.slot.control_client.ControlClient` per call, closed after it (a client never reconnects, L2 F5). It
  refuses every other op: the lease is never this port's.
* :class:`HelperSlotRuntime` is ``start`` and ``end``. ``start`` runs, in this order, ``LEASE-OPEN{ticket}`` (the
  connection becomes the lease and is held for the session's life), ``prepare``, the brief (and the workspace, when a
  stager is given), ``render``, and the GATED ``start``; there is no ``probe`` step (see below). A step that fails after
  the session is known ENDs it once, and the lease is closed only on a proven END: closing an unproven one would hand
  the session to the root reaper and make a later teardown's answer ambiguous (design F5, F6). A failure before the
  session is known closes the lease (CONTROL revokes on EOF; the root reaper ENDs whatever ``prepare`` allocated).
* ``end`` is :func:`~levain.agents.session_launcher.end_by_teardown` over the helper's ``teardown`` and nothing else.

Finding C (the lease opener sends no ticket): ``ControlClient.call("LEASE-OPEN", ticket=...)`` carries the ticket (the
frame has no reserved ``ticket`` key), so ``control_client.py`` is untouched. The nonce is read from that answer.

Finding D (``probe`` is not a dispatch verb): V13 does not bind a dispatch session at launch, by two independent
refusals in code: ``runtime_grammar.DISPATCH_FORBIDDEN`` lists ``probe`` ("forbidden to dispatch until a dispatch
session launches"), and ``probe.PROBE_KINDS`` is ``("interactive", "login")``, so a ``dispatch`` kind session answers
``not-probeable:kind``. So the order has no probe, and ``sudoers.DISPATCH_VERBS`` needs no change. What this leaves open
is a policy question, not a code one: a dispatch session has no launch-time firewall tripwire (the interactive
supervisor does), so the wall's only check is the VM's.

Obligations of the SlotRuntime protocol honoured here: (1) the brief is staged by ``stage_brief_for`` after ``prepare``
and before ``render``; (2) the gated child's ``returncode`` is the helper's process status through sudo; (3) the
child's stdout is drained and discarded in bounded memory by :class:`StreamDrain`, never parsed; (4) the ticket is written
nowhere: it goes to CONTROL in the LEASE-OPEN frame and to the helper's ``prepare`` stdin (the dispatch grammar requires
it there, ``bind_stdin``: ``ticket-required``), and into no log line, refusal detail, argv or file.
"""
from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from levain.agents import controller as _ctl
from levain.agents.session_launcher import (HOOKS, LauncherRefused, SlotHandle, SlotStart, end_by_teardown,
                                            stage_brief_for)
from levain.contracts import canon
from levain.slot import Refused, layout
from levain.slot.control_client import ControlClient
from levain.slot.grammar import SESSION_RE
from levain.slot.ids import SlotId
from levain.slot.runtime_grammar import RENDER_FORMAT, REPORT_CAP, parse_stdin
from levain.slot.supervisor import (HELPER_DEADLINE_DEFAULT_S, HELPER_DEADLINE_S, KILL_GRACE_S, SUDO, _host,
                                    parse_helper_line, pick_routes, prepare_answer, read_base_map, spawn)

__all__ = ["PORT_OPS", "DispatchControlPort", "HelperSlotRuntime", "HelperVerifierRuntime", "StreamDrain"]

_log = logging.getLogger(__name__)

#: the ops ``RealLauncher`` uses through its ControlPort. Nothing else crosses (the lease is the runtime's own).
PORT_OPS = frozenset({"RUN-TICKET-MINT", "RUN-TICKET-CANCEL", "RUN-TICKET-STATUS", "ROUTE-RESOLVE"})

_HELPER_OUT_MAX = 1 << 20     # a helper answer past this is no single line (the supervisor's bound)
_ERR_TAIL = 1 << 14           # DECISION: the helper's stderr and the vendor's stderr are kept to their last 16 KiB
_ENDED_KEEP = 256            # DECISION: sessions this runtime proved ended, remembered so a repeat END is not a 2nd teardown


class DispatchControlPort:
    """``ControlPort`` over CONTROL's socket as the dispatch uid: a fresh connection for every call, closed after it."""

    def __init__(self, socket_path: "Path | str", *, ops: "frozenset[str]" = PORT_OPS, timeout: float = 10.0,
                 client_factory: Callable[..., Any] = ControlClient) -> None:
        self._path, self._ops, self._timeout, self._factory = socket_path, frozenset(ops), timeout, client_factory

    def call(self, op: str, **fields: object) -> dict:
        if op not in self._ops:
            raise Refused("op-not-allowed", op[:64])
        client = self._factory(self._path, timeout=self._timeout)
        try:
            return client.call(op, **fields)
        finally:
            client.close()


class StreamDrain:
    """Reads a byte stream to EOF and keeps nothing but a count and the LAST ``keep`` bytes (``keep=0``: not even
    that). Memory is bounded by one read chunk plus ``keep`` whatever the stream writes. :meth:`start` runs it on a
    daemon thread; :meth:`run` is the loop itself."""

    def __init__(self, stream: Any, *, keep: int = 0, chunk: int = 65536, close: bool = False) -> None:
        self._close = close
        self._stream, self._keep, self._chunk = stream, max(0, keep), chunk
        self._tail = bytearray()
        self.total = 0
        self.peak_tail = 0
        self.thread: "threading.Thread | None" = None

    def run(self) -> None:
        try:
            self._loop()
        finally:
            if self._close:                       # the reader closes its own end: a close from another thread can block
                try:
                    self._stream.close()
                except (OSError, ValueError):
                    pass

    def _loop(self) -> None:
        read = getattr(self._stream, "read1", None) or self._stream.read
        while True:
            try:
                data = read(self._chunk)
            except (OSError, ValueError):
                return
            if not data:
                return
            self.total += len(data)
            if self._keep:
                self._tail += data
                if len(self._tail) > self._keep:
                    del self._tail[:-self._keep]
                self.peak_tail = max(self.peak_tail, len(self._tail))

    def start(self) -> "StreamDrain":
        self.thread = threading.Thread(target=self.run, name="levain-slot-drain", daemon=True)
        self.thread.start()
        return self

    @property
    def tail(self) -> bytes:
        return bytes(self._tail)


def _safe(data: bytes, limit: int = 400) -> str:
    return data[-limit:].decode("utf-8", "replace").encode("ascii", "backslashreplace").decode("ascii")


class HelperSlotRuntime:
    """The :class:`SlotRuntime`. ``helper`` is the argv prefix the verbs follow (default ``sudo -n <runtime helper>``;
    tests run a fake script there). ``routes`` is the render blob's route list, or a callable that answers it (default:
    the base map's egress routes plus the one Claude passthrough route); a BROKERED start ignores it and names its
    ticket's minted routes (``SlotStart.routes``). ``workspace`` is S10w's seam: called with the
    request and the staging dir after the brief and before ``render``. ``repo_settings`` answers the request's frozen
    repo settings layers (the verified snapshot's, None for none), which the render blob carries as its
    ``repo_settings``."""

    def __init__(self, *, host: "str | None" = None, helper: "Sequence[str] | None" = None,
                 control_socket: "Path | None" = None, store: "Path | None" = None,
                 routes: "Sequence[str] | Callable[[SlotStart], Sequence[str]] | None" = None,
                 workspace: "Callable[[SlotStart, Path], None] | None" = None,
                 repo_settings: "Callable[[SlotStart], Mapping[str, Any] | None] | None" = None,
                 client_factory: Callable[..., Any] = ControlClient, env: "Mapping[str, str] | None" = None,
                 deadlines: "Mapping[str, float] | None" = None, kill_grace_s: float = KILL_GRACE_S,
                 git: str = "git") -> None:
        self.host = _host() if host is None else host
        self._helper = tuple((SUDO, "-n", str(layout.runtime_helper_path(self.host))) if helper is None else helper)
        self._socket = layout.RUN[self.host] / "control" / "control.sock" if control_socket is None else control_socket
        self._store = layout.STORE[self.host] if store is None else Path(store)
        self._routes, self._workspace, self._factory = routes, workspace, client_factory
        self._repo_settings = repo_settings
        self._env, self._kill_grace = env, kill_grace_s
        self._deadlines = dict(HELPER_DEADLINE_S if deadlines is None else deadlines)
        self._git = git
        self._mu = threading.Lock()
        self._leases: "dict[str, Any]" = {}
        # session -> the tunnel_bytes its proven END published (None: none). The proof and its measurement are ONE
        # entry, so they share one lifetime and one eviction: no proof outlives its value or the reverse.
        self._ended: "collections.OrderedDict[str, int | None]" = collections.OrderedDict()
        self._drains: "dict[str, tuple[StreamDrain, StreamDrain]]" = {}
        self._end_locks: "dict[str, threading.Lock]" = {}
        self._closed = False

    # -- the helper's verbs ----------------------------------------------------------------------------------------

    def _run(self, verb: str, *args: str, stdin: "bytes | None" = None, lease: Any = None) -> "dict[str, object]":
        """One verb off the helper: stdin (None: /dev/null) and stdout/stderr pipes, a wall per verb (TERM, then KILL),
        the answer checked as exactly one canonical JSON line. An OS failure to run it is ``helper-unrunnable``."""
        argv = [*self._helper, verb, *args]
        fds = (lease.fileno(),) if lease is not None and hasattr(lease, "fileno") else ()
        try:
            p = spawn(argv, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, lease_fds=fds, env=self._env)
        except OSError as e:
            raise Refused("helper-unrunnable", f"{verb}: {e.strerror or e}") from None
        wall = self._deadlines.get(verb, HELPER_DEADLINE_DEFAULT_S)
        out_d = StreamDrain(p.stdout, keep=_HELPER_OUT_MAX, close=True).start()
        err_d = StreamDrain(p.stderr, keep=_ERR_TAIL, close=True).start()
        try:
            try:
                if stdin is not None:
                    p.stdin.write(stdin)          # type: ignore[union-attr]
                    p.stdin.close()               # type: ignore[union-attr]
            except OSError:
                pass                              # it stopped reading: its answer says why
            try:
                p.wait(timeout=wall)
            except subprocess.TimeoutExpired:
                raise Refused("helper-timeout", f"{verb} did not answer in {wall}s") from None
            for d in (out_d, err_d):
                d.thread.join(min(wall, 5.0))               # type: ignore[union-attr]
        finally:
            if p.poll() is None:
                self._end_helper(p)
            if p.stdin is not None:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        if err_d.tail.strip():
            _log.info("helper %s stderr: %s", verb, _safe(err_d.tail))
        if out_d.total > _HELPER_OUT_MAX:
            raise Refused("helper-output-malformed", f"{verb}: over {_HELPER_OUT_MAX} bytes")
        out = out_d.tail
        ans = parse_helper_line(out, verb)
        if p.returncode != 0:                     # an ok line from a helper that then failed is no success
            raise Refused("helper-status", f"{verb}: exit {p.returncode}")
        return ans

    def _end_helper(self, p: "subprocess.Popen[bytes]") -> None:
        for send in (p.terminate, p.kill):
            try:
                send()
            except OSError:
                return
            try:
                p.wait(timeout=self._kill_grace)
                return
            except subprocess.TimeoutExpired:
                continue
        _log.warning("the runtime helper (pid %s) did not exit after SIGKILL", p.pid)

    def _teardown(self, session: str) -> "dict[str, object]":
        return self._run("teardown", session)

    def read_report(self, session: str) -> "bytes | None":
        """The session's report file as root read it from the retained tree (``read-report``), or None when the session
        left none. The bytes are UNTRUSTED DATA: this checks only the wire (one base64 field, the byte cap), never what
        the text says. A refusal of the helper (``report-unreadable``, ``report-oversize``, ``report-malformed``, a state
        gate) is raised as :class:`~levain.slot.Refused` under its own name."""
        if type(session) is not str or SESSION_RE.fullmatch(session) is None:
            raise Refused("report-session-malformed", "the session id is off the grammar")
        ans = self._run("read-report", session)
        present = ans.get("present")
        if present is False and set(ans) == {"present"}:
            return None
        b64 = ans.get("b64")
        if present is not True or type(b64) is not str or set(ans) != {"present", "b64"}:
            raise Refused("helper-output-malformed", "read-report: the answer is off its shape")
        if len(b64) > 4 * ((REPORT_CAP + 2) // 3):
            raise Refused("report-oversize", "read-report: the answer is over the report cap")
        try:
            data = base64.b64decode(b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError):
            raise Refused("helper-output-malformed", "read-report: the bytes are not base64") from None
        if len(data) > REPORT_CAP:
            raise Refused("report-oversize", "read-report: the answer is over the report cap")
        return data

    def final_tunnel_bytes(self, session: str) -> "int | None":
        """The final tunnel total root's END published for ``session`` (CONTRACT §5.1 ``tunnel_bytes``), kept only
        from the answer that PROVED the END (:func:`end_by_teardown`) and only as an int in 0..2**63-1. None: not
        ended by this runtime, END did not publish one, it was malformed, or the entry was evicted with its proof
        (more than :data:`_ENDED_KEEP` later ENDs before the launcher's ended record was written). Every None leaves
        the key off the record and usage settles at the reservation: an over-debit, never an under-debit."""
        with self._mu:
            return self._ended.get(session)

    # -- the SlotRuntime ---------------------------------------------------------------------------------------------

    def _render_blob(self, req: SlotStart) -> bytes:
        """The §5.3 blob, checked by the helper's own grammar BEFORE anything is allocated. It must name every hook of
        :data:`HOOKS` (port item 4): a launcher that asks for fewer is refused here, not left to the server. It carries
        the request's frozen repo settings layers (``repo_settings``) when there are any."""
        if set(req.hooks) != set(HOOKS):
            raise LauncherRefused("hooks-incomplete", ",".join(sorted(set(req.hooks) ^ set(HOOKS))))
        if req.auth == "brokered":
            # the ticket's own routes, exactly as minted (L3 wave3 r1, codex HIGH 2): the base map's brokered route is
            # not the dispatch-map route CONTROL resolved, and render refuses any blob whose routes are not the ticket's
            if not req.routes:
                raise LauncherRefused("brokered-start-without-route", "a brokered start names its ticket's routes")
            routes = req.routes
        elif self._routes is None:
            routes = pick_routes(read_base_map(layout.base_routes_path(self.host)), req.harness, req.auth,
                                 egress=False)   # the ticket lists none by default (11-02); render refuses extras
        else:
            routes = self._routes(req) if callable(self._routes) else self._routes
        blob = {"format": RENDER_FORMAT, "vendor": req.harness, "routes": list(routes), "model": req.model,
                "hooks": list(req.hooks)}
        settings = self._repo_settings(req) if self._repo_settings is not None else None
        if settings:
            blob["repo_settings"] = dict(settings)
        data = canon.canonical_bytes(blob)
        parse_stdin("render", data)
        return data

    def start(self, req: SlotStart) -> SlotHandle:
        blob = self._render_blob(req)
        lease = self._factory(self._socket)
        try:
            ans = lease.call("LEASE-OPEN", ticket=req.ticket)
            nonce = ans.get("lease_nonce")
            if type(nonce) is not str or not nonce:
                raise Refused("lease-nonce-missing", "LEASE-OPEN")
            prep = self._run("prepare", "egress", "dispatch", req.harness, req.auth,
                             stdin=canon.canonical_bytes({"lease_nonce": nonce, "ticket": req.ticket}), lease=lease)
            session, _slot = prepare_answer(prep, host=self.host, store=self._store)   # type: ignore[arg-type]
        except BaseException:
            try:
                lease.close()
            except Exception:  # noqa: BLE001 - never mask the refusal that is being raised
                _log.warning("closing the lease failed")
            # the session is not known: CONTROL revokes on EOF, the root reaper ENDs
            raise
        with self._mu:
            shut = self._closed
            if not shut:
                self._leases[session] = lease
        if shut:                                  # close() ran meanwhile: END it, refuse; the lease's EOF is the backstop
            try:
                self.end(session)
                lease.close()
            except Exception:  # noqa: BLE001 - never mask the refusal below
                _log.warning("ending a session refused at shutdown failed")
            raise LauncherRefused("runtime-closed", "the dispatch runtime is shutting down", reason_class="transient")
        child = None
        try:
            staging = self._store / "staging" / session
            stage_brief_for(req, staging)
            if self._workspace is not None:
                self._workspace(req, staging)
            self._run("render", session, stdin=blob, lease=lease)
            child = _ctl.GatedChild([*self._helper, "start", session], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=self._env)
            drains = (StreamDrain(child.process.stdout).start(), StreamDrain(child.process.stderr, keep=_ERR_TAIL).start())
            with self._mu:
                self._drains[session] = drains
            return SlotHandle(session, child)
        except BaseException:
            if child is not None:
                try:
                    child.abort()
                except Exception:  # noqa: BLE001 - END below is the proof; the gate is closed best effort
                    pass
                finally:
                    self.end(session)
                raise
            self.end(session)
            raise

    def end(self, session: str) -> bool:
        with self._mu:
            if session in self._ended:
                return True
            gate = self._end_locks.setdefault(session, threading.Lock())
        with gate:                                # one teardown in flight per session; a second caller shares its result
            with self._mu:
                if session in self._ended:
                    return True
            answers: "list[object]" = []

            def teardown(s: str) -> "dict[str, object]":
                ans = self._teardown(s)
                answers.append(ans)
                return ans

            proven = end_by_teardown(teardown, session)
            if proven:
                # the measurement is taken from the very answer that proved the END, never from an unproven one
                v = answers[-1].get("tunnel_bytes") if answers and type(answers[-1]) is dict else None
                tunnel = v if type(v) is int and 0 <= v <= 2**63 - 1 else None
                with self._mu:
                    lease = self._leases.pop(session, None)
                    self._drains.pop(session, None)
                    self._ended[session] = tunnel
                    self._end_locks.pop(session, None)
                    while len(self._ended) > _ENDED_KEEP:
                        self._ended.popitem(last=False)
                if lease is not None:
                    try:
                        lease.close()
                    except Exception:  # noqa: BLE001 - END is proven; a failing close leaves nothing to retry
                        _log.warning("closing the lease of ended session %s failed", session)
            return proven

    def stderr_tail(self, session: str) -> bytes:
        """The vendor stderr's last bytes (for a log line), b"" when none is kept."""
        with self._mu:
            d = self._drains.get(session)
        return d[1].tail if d else b""

    def close(self) -> None:
        """Close every lease still held: each EOF revokes its session and the root reaper ENDs it (the shutdown backstop)."""
        with self._mu:
            self._closed = True
            leases, self._leases = list(self._leases.values()), {}
        for lease in leases:
            try:
                lease.close()
            except Exception:  # noqa: BLE001 - one failing close must not leave the rest open
                _log.warning("closing a lease failed at shutdown")


# -- the verifier session (S14 A4, design A D1 a and D3 a) -------------------------------------------------------------

#: the driver's own wall (V26), the clone's and the slack the waiter adds. DECISION: dispatch cannot import root's
#: ``verifier_session`` (the closure runs the other way), so the V26 figure is written here and a test pins it equal.
DRIVER_WALL_S = 600
CLONE_WALL_S = 300
WAIT_SLACK_S = 120
#: root's per-spawn costs beside a wall (S14 fix 5, L2 M2), written here for the same reason and each pinned equal by a
#: test: the session END lock waited for up to ``verifier_session.LOCK_WAIT_S`` before a spawn and again before its
#: kill-and-prove, the proof itself up to ``proc.V12_SECONDS`` (Linux) / ``proc.V12K_SECONDS`` (macOS), and the reap
#: of a timed-out child up to ``verifier_session._REAP_GRACE_S``
ROOT_LOCK_WAIT_S = 30
ROOT_KILL_PROOF_S = 10
ROOT_REAP_GRACE_S = 5
#: one spawn's worst case beside its wall: two lock waits, a kill proof and a reap
SPAWN_OVERHEAD_S = 2 * ROOT_LOCK_WAIT_S + ROOT_KILL_PROOF_S + ROOT_REAP_GRACE_S
_COPY_CHUNK = 1 << 20
_BUNDLE_NAME = "repo.bundle"
_DRIVER_INPUT_NAME = "driver_input.json"
#: ``runtime_helper.PREPARE_RESULT_FORMAT``, written here so this module takes no import of the helper (a test pins them equal)
_PREPARE_RESULT_FORMAT = "levain-prepare-result/1"
_PREPARE_NONE_KEYS = frozenset({"format", "session", "slot", "staging"})


def verifier_wait_bound(steps: "Sequence[Mapping[str, Any]]") -> float:
    """How long the waiter gives the helper's ``start`` for a verifier session: every step's wall, the driver's, the
    clone's, root's worst case beside each wall for the k steps and the driver (:data:`SPAWN_OVERHEAD_S` each: two
    lock waits, a kill proof, a reap), one lock wait each for the clone and the envelope write, and a slack. Past it the
    helper is ended and the session ENDed anyway, so a bound short of root's real worst case would end a long ticket
    early as ``no-report:ACTIVE``."""
    walls = sum(int(st["wall_s"]) for st in steps)
    return float(walls + DRIVER_WALL_S + CLONE_WALL_S + (len(steps) + 1) * SPAWN_OVERHEAD_S + 2 * ROOT_LOCK_WAIT_S
                 + WAIT_SLACK_S)


def _prepare_none_answer(prep: "Mapping[str, object]", *, store: Path) -> str:
    """``prepare none verifier``'s answer checked whole: a none-class slot has no gateway, so the answer holds no
    ``gwport`` (``supervisor.prepare_answer`` is the egress shape and refuses it). Returns the session id."""
    def bad(what: str) -> Refused:
        return Refused("prepare-answer-malformed", what)

    if set(prep) != _PREPARE_NONE_KEYS:
        raise bad("keys: " + ",".join(sorted(set(prep) ^ _PREPARE_NONE_KEYS))[:128])
    if prep["format"] != _PREPARE_RESULT_FORMAT:
        raise bad("format")
    session = prep["session"]
    if type(session) is not str or not SESSION_RE.fullmatch(session):
        raise bad("session")
    sl = prep["slot"]
    if type(sl) is not dict or set(sl) != {"class", "index"} or sl["class"] != "none" or type(sl["index"]) is not int:
        raise bad("slot")
    try:
        SlotId("none", sl["index"])
    except Refused:
        raise bad("slot") from None
    if prep["staging"] != str(Path(store) / "staging" / session):
        raise bad("staging")
    return session


def _write_new(dfd: int, name: str, data: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    except FileExistsError:
        raise Refused("staging-dest-exists", name) from None
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_verifier_inputs(staging: "Path | str", bundle_path: "Path | str", bundle_digest: str, driver_input: bytes) -> None:
    """``L/staging/<session>/repo.bundle`` (copied from ``bundle_path``, hashed as it is copied) and
    ``driver_input.json``. Both are fresh files (``O_EXCL|O_NOFOLLOW``, 0600, the lease peer's own, which root's
    reader opens as the staging uid). A copy whose digest is not ``bundle_digest`` is removed and refused
    ``verifier-bundle-mismatch``; a source that cannot be read as a regular file is ``verifier-bundle-unreadable``.
    Both are refused BEFORE render, so root never sees a bundle the ticket does not name."""
    dfd = os.open(os.fspath(staging), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            src = os.open(os.fspath(bundle_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            raise Refused("verifier-bundle-unreadable", f"open: {e.strerror or e}") from None
        try:
            import stat as _stat
            if not _stat.S_ISREG(os.fstat(src).st_mode):
                raise Refused("verifier-bundle-unreadable", "not a regular file")
            try:
                dst = os.open(_BUNDLE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                              dir_fd=dfd)
            except FileExistsError:
                raise Refused("staging-dest-exists", _BUNDLE_NAME) from None
            h = hashlib.sha256()
            try:
                os.fchmod(dst, 0o600)
                while True:
                    try:
                        chunk = os.read(src, _COPY_CHUNK)
                    except OSError as e:
                        raise Refused("verifier-bundle-unreadable", f"read: {e.strerror or e}") from None
                    if not chunk:
                        break
                    h.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view):]
                os.fsync(dst)
            except BaseException:
                os.close(dst)
                dst = -1
                try:
                    os.unlink(_BUNDLE_NAME, dir_fd=dfd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                if dst >= 0:
                    os.close(dst)
        finally:
            os.close(src)
        if "sha256:" + h.hexdigest() != bundle_digest:
            os.unlink(_BUNDLE_NAME, dir_fd=dfd)
            raise Refused("verifier-bundle-mismatch", "the bundle copied is not the one the ticket was minted against")
        _write_new(dfd, _DRIVER_INPUT_NAME, driver_input)
        os.fsync(dfd)
    finally:
        os.close(dfd)


class HelperVerifierRuntime:
    """The production ``VerifierRuntime`` (S14 A4): a verifier session's start through the runtime helper, in the order
    ``LEASE-OPEN{ticket}``, ``prepare none verifier claude none`` (stdin ``{lease_nonce, ticket}``; the vendor token
    is the grammar's and no vendor binary runs), the bundle copied into staging (hashed as it copies) and
    ``driver_input.json`` written, ``render`` (blob ``{format, vendor, routes: [], verifier_steps, driver}``, checked
    by the helper's own grammar first), then the helper's ``start`` holding the lease, and a return.

    ``slot`` is the executor's :class:`HelperSlotRuntime`: its helper, socket, store, client factory, lease table and
    ``end`` are this runtime's, so ``slot.close()`` closes verifier leases too, and one END proof per session.

    Design A D3 (a): a verifier session is ENDed by THIS runtime. One waiter thread per session waits for the helper's
    ``start`` to exit (bounded by :func:`verifier_wait_bound`; past it the helper is ended), then calls ``slot.end``
    and closes the lease only on a proven END (the executor's rule). An unproven END leaves the lease open and logs
    it: the lease's EOF at shutdown (CONTROL revokes) and the root reaper are the backstop. A start that fails after
    the session is known ENDs it once, as the executor's does. The ticket is written nowhere: it goes to CONTROL in
    the LEASE-OPEN frame and to ``prepare``'s stdin, never into an argv, a log line, a refusal detail or a file."""

    def __init__(self, slot: HelperSlotRuntime) -> None:
        self._slot = slot
        self._mu = threading.Lock()
        self._waiters: "list[threading.Thread]" = []

    # -- the render blob ---------------------------------------------------------------------------------------------

    @staticmethod
    def _render_blob(steps: "Sequence[Mapping[str, Any]]", driver_template: str) -> bytes:
        blob = {"format": RENDER_FORMAT, "vendor": "claude", "routes": [], "verifier_steps": [dict(s) for s in steps],
                "driver": driver_template}
        data = canon.canonical_bytes(blob)
        parse_stdin("render", data)               # the helper's own grammar, before anything is allocated
        return data

    # -- start -------------------------------------------------------------------------------------------------------

    def start(self, ticket: str, inputs: Any) -> None:
        slot = self._slot
        blob = self._render_blob(inputs.steps, inputs.driver_template)
        lease = slot._factory(slot._socket)
        try:
            ans = lease.call("LEASE-OPEN", ticket=ticket)
            nonce = ans.get("lease_nonce")
            if type(nonce) is not str or not nonce:
                raise Refused("lease-nonce-missing", "LEASE-OPEN")
            prep = slot._run("prepare", "none", "verifier", "claude", "none",
                             stdin=canon.canonical_bytes({"lease_nonce": nonce, "ticket": ticket}), lease=lease)
            session = _prepare_none_answer(prep, store=slot._store)
        except BaseException:
            try:
                lease.close()
            except Exception:  # noqa: BLE001 - never mask the refusal that is being raised
                _log.warning("closing the lease failed")
            raise                                 # the session is not known: CONTROL revokes on EOF, the reaper ENDs
        with slot._mu:
            shut = slot._closed
            if not shut:
                slot._leases[session] = lease
        if shut:
            try:
                slot.end(session)
                lease.close()
            except Exception:  # noqa: BLE001 - never mask the refusal below
                _log.warning("ending a session refused at shutdown failed")
            raise LauncherRefused("runtime-closed", "the dispatch runtime is shutting down", reason_class="transient")
        child = None
        try:
            stage_verifier_inputs(slot._store / "staging" / session, inputs.bundle_path, inputs.bundle_digest,
                                  inputs.driver_input)
            slot._run("render", session, stdin=blob, lease=lease)
            child = _ctl.GatedChild([*slot._helper, "start", session], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=slot._env)
            drains = (StreamDrain(child.process.stdout).start(), StreamDrain(child.process.stderr, keep=_ERR_TAIL).start())
            with slot._mu:
                slot._drains[session] = drains
            child.release()
            waiter = threading.Thread(target=self._wait, args=(session, child, verifier_wait_bound(inputs.steps)),
                                      name="levain-verifier-wait", daemon=True)
            with self._mu:
                self._waiters = [t for t in self._waiters if t.is_alive()] + [waiter]
            waiter.start()
        except BaseException:
            if child is not None:
                try:
                    child.abort()
                except Exception:  # noqa: BLE001 - END below is the proof; the gate is closed best effort
                    pass
            slot.end(session)
            raise

    # -- the waiter (D3 a) ---------------------------------------------------------------------------------------------

    def _wait(self, session: str, child: Any, bound: float) -> None:
        """Wait for the helper's ``start`` to answer, reap it, then END the session. Never raises."""
        slot = self._slot
        try:
            try:
                child.process.wait(timeout=bound)
            except subprocess.TimeoutExpired:
                _log.warning("the verifier start of session %s ran past %ss; ending it", session, bound)
            try:
                child.abort()
            except Exception:  # noqa: BLE001 - END below is the proof
                _log.warning("reaping the verifier start of session %s failed", session)
            with slot._mu:
                drains = slot._drains.get(session)
            if drains is not None and drains[1].tail.strip():
                _log.info("verifier start %s stderr: %s", session, _safe(drains[1].tail))
            if not slot.end(session):
                _log.warning("END of verifier session %s is not proven; its lease stays open (shutdown revokes it, the "
                             "root reaper ENDs it)", session)
        except Exception as exc:  # noqa: BLE001 - a waiter that dies leaves the lease and the reaper to END
            _log.warning("the waiter of verifier session %s failed: %s", session, type(exc).__name__)

    def join_waiters(self, timeout: float = 10.0) -> bool:
        """Wait for every waiter to finish (a test's and a shutdown's seam). True when none is left running."""
        with self._mu:
            threads = list(self._waiters)
        for t in threads:
            t.join(timeout)
        return not any(t.is_alive() for t in threads)

    def close(self) -> None:
        """The shared lease table's close (:meth:`HelperSlotRuntime.close`): every lease still held is closed."""
        self._slot.close()
