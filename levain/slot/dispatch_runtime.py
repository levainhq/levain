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

import collections
import logging
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
from levain.slot.runtime_grammar import RENDER_FORMAT, parse_stdin
from levain.slot.supervisor import (HELPER_DEADLINE_DEFAULT_S, HELPER_DEADLINE_S, KILL_GRACE_S, SUDO, _host,
                                    parse_helper_line, pick_routes, prepare_answer, read_base_map, spawn)

__all__ = ["PORT_OPS", "DispatchControlPort", "HelperSlotRuntime", "StreamDrain"]

_log = logging.getLogger(__name__)

#: the ops ``RealLauncher`` uses through its ControlPort. Nothing else crosses (the lease is the runtime's own).
PORT_OPS = frozenset({"RUN-TICKET-MINT", "RUN-TICKET-CANCEL", "RUN-TICKET-STATUS"})

_HELPER_OUT_MAX = 1 << 20     # a helper answer past this is no single line (the supervisor's bound)
_ERR_TAIL = 1 << 14           # DECISION: the helper's stderr and the vendor's stderr are kept to their last 16 KiB
_ENDED_KEEP = 256             # DECISION: sessions this runtime proved ended, remembered so a repeat END is not a 2nd teardown


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
    the base map's egress routes plus the one Claude passthrough route). ``workspace`` is S10w's seam: called with the
    request and the staging dir after the brief and before ``render``."""

    def __init__(self, *, host: "str | None" = None, helper: "Sequence[str] | None" = None,
                 control_socket: "Path | None" = None, store: "Path | None" = None,
                 routes: "Sequence[str] | Callable[[SlotStart], Sequence[str]] | None" = None,
                 workspace: "Callable[[SlotStart, Path], None] | None" = None,
                 client_factory: Callable[..., Any] = ControlClient, env: "Mapping[str, str] | None" = None,
                 deadlines: "Mapping[str, float] | None" = None, kill_grace_s: float = KILL_GRACE_S,
                 git: str = "git") -> None:
        self.host = _host() if host is None else host
        self._helper = tuple((SUDO, "-n", str(layout.runtime_helper_path(self.host))) if helper is None else helper)
        self._socket = layout.RUN[self.host] / "control" / "control.sock" if control_socket is None else control_socket
        self._store = layout.STORE[self.host] if store is None else Path(store)
        self._routes, self._workspace, self._factory = routes, workspace, client_factory
        self._env, self._kill_grace = env, kill_grace_s
        self._deadlines = dict(HELPER_DEADLINE_S if deadlines is None else deadlines)
        self._git = git
        self._mu = threading.Lock()
        self._leases: "dict[str, Any]" = {}
        self._ended: "collections.OrderedDict[str, bool]" = collections.OrderedDict()
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

    # -- the SlotRuntime ---------------------------------------------------------------------------------------------

    def _render_blob(self, req: SlotStart) -> bytes:
        """The §5.3 blob, checked by the helper's own grammar BEFORE anything is allocated. It must name every hook of
        :data:`HOOKS` (port item 4): a launcher that asks for fewer is refused here, not left to the server."""
        if set(req.hooks) != set(HOOKS):
            raise LauncherRefused("hooks-incomplete", ",".join(sorted(set(req.hooks) ^ set(HOOKS))))
        if self._routes is None:
            routes = pick_routes(read_base_map(layout.base_routes_path(self.host)), req.harness, "passthrough")
        else:
            routes = self._routes(req) if callable(self._routes) else self._routes
        data = canon.canonical_bytes({"format": RENDER_FORMAT, "vendor": req.harness, "routes": list(routes),
                                      "model": req.model, "hooks": list(req.hooks)})
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
            prep = self._run("prepare", "egress", "dispatch", req.harness, "passthrough",
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
            proven = end_by_teardown(self._teardown, session)
            if proven:
                with self._mu:
                    lease = self._leases.pop(session, None)
                    self._drains.pop(session, None)
                    self._ended[session] = True
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
