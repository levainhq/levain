"""levain.agents.session_launcher: the real :class:`levain.agents.session.SessionLauncher` for a subscription session on
an S1 dispatch slot (BUILD_PLAN P7-1 slice S4; P7-A section 6; head ruling 11-04: option (a), an own launcher on session
kind ``dispatch``, no ``JobStore``, no wait on dispatch wave 3).

:class:`RealLauncher` owns the order of one launch and nothing else; every act on the outside world goes through one of
three seams, so the whole of it runs in pytest against fakes:

* :class:`ControlPort`: ``call(op, **fields)``, the shape of :class:`levain.slot.control_client.ControlClient`. The
  launcher mints through it (``RUN-TICKET-MINT``), and undoes a mint through it (``RUN-TICKET-STATUS`` /
  ``RUN-TICKET-CANCEL``). This module contains the one place that mints (:meth:`RealLauncher._mint`).
* :class:`SlotRuntime`: the credential slot's start (lease, prepare, render, start under the ticket, the agent fence's
  hooks) answering the gated child, and S1 END (``end(session)`` answers True only on ``Domain.proven``).
* :class:`UsageSources`: the three figures a subscription run settles on (P7-A section 3.3 item 4): the broker's call
  count by ``run_id``, the front's byte total and the session's seconds by slot session.

Order of :meth:`RealLauncher.spawn` (FAILURE-ATOMIC, the :class:`~levain.agents.session.SessionLauncher` contract):
the route is refused by name before anything is allocated (:data:`BROKERED_ROUTE_BUILT` is False until 3c-10, so the
API-key branch is ``brokered-route-not-built``); the ticket facts are checked by CONTROL's own shape function
(:func:`levain.slot.tickets.check_facts`); a ``pending`` record is written to the :class:`SessionIndex` under the
recorded session key; the run ticket is minted with ``attempt_id`` == the session key (a replay of one key answers the
same ticket, so there is one derivation); the slot is started; the record is completed with the slot session. A
``spawn`` that raises has undone what it allocated (an unbound ticket cancelled, a bound session ENDed), and says so in
the index (a ``refused`` / ``undone`` record reads zero usage). One it cannot prove undone does NOT raise: it returns a
:class:`HeldChild` that owns the leftover session, so the run's own stop and abort reach it.

Usage is keyed by the RECORDED session key and by nothing else (:meth:`RealLauncher.usage`): the key reaches a durable
:class:`SessionIndex` record (run id, slot session, start time), so a fresh launcher in another process, after a crash,
reads the same figures. The ticket itself is a bearer capability and is never written down. CONTROL's
``RUN-TICKET-STATUS`` does not answer the ``run_id`` once a ticket is bound, so the index is the only record of it.

What no test here holds (the residue, UNVERIFIED until the VM rows): a real credential slot started under a real
passthrough ticket, the unmodified ``claude`` binary running as the slot uid under the agent fence, a real front byte
total, a real broker call count, and a real S1 END proving a live uid group gone. Also unverified: that the fire's uid
may mint at all. CONTROL answers the ``dispatch`` verbs to ``_levain_dispatch``'s uid only (``Control.peer_kind``), so
the production :class:`ControlPort` must be a connection made as that uid; no such adapter is in this module.

Nothing imports this module from an entry point, and :class:`~levain.agents.session.UnwiredLauncher` stays the default.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from levain.agents import controller as _ctl
from levain.agents.accounting import StopResult, Usage
from levain.agents.session import S1_DISPATCH, SessionLaunch
from levain.slot import Refused
from levain.slot import tickets as T

__all__ = ["BROKERED_ROUTE_BUILT", "VENDOR", "HOOKS", "LauncherRefused", "ControlPort", "SlotStart", "SlotHandle",
           "SlotRuntime", "UsageSources", "SessionIndex", "RealLauncher", "HeldChild"]

_log = logging.getLogger("levain.agents.session_launcher")

#: the API-key (``brokered``) route needs dispatch wave 3's per-run keys and dollar caps, and so 3c-10's OpenRouter
#: credential. Until that is built the launcher refuses it by name. This is the one place the fact is stated.
BROKERED_ROUTE_BUILT = False
VENDOR = "claude"
#: the agent fence: the per-call gate hook and the queue hook the slot's render writes (``runtime_grammar.HOOKS``)
HOOKS = ("levain_gate", "levain_queue")
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")      # the attempt-id grammar; a session key is used as the attempt id
_INDEX_FORMAT = "levain-session-index/1"
_INDEX_STATES = ("pending", "minted", "started", "refused", "undone")
_ZERO_STATES = ("refused", "undone")                  # nothing ran: usage reads zero


class LauncherRefused(RuntimeError):
    """The launcher refused to start a session. ``name`` is the stable reason; nothing was allocated."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


@runtime_checkable
class ControlPort(Protocol):
    """The CONTROL client as far as the launcher uses it (``ControlClient.call``): raises
    :class:`levain.slot.Refused` on a refusal and ``Refused("control-down")`` on a broken connection."""

    def call(self, op: str, **fields: object) -> dict: ...


@dataclass(frozen=True)
class SlotStart:
    """What the slot runtime is asked to start: the minted ``ticket`` (a bearer capability, handed over here and kept
    nowhere else), the run it belongs to, the recorded session key, the harness, model and brief, and the fence hooks."""
    ticket: str
    run_id: str
    session_key: str
    harness: str
    model: str
    brief: str
    hooks: tuple[str, ...]


@dataclass(frozen=True)
class SlotHandle:
    """A started slot: its S1 ``session`` id and the GATED child (:class:`levain.agents.controller.GatedChild`'s shape)
    that has run nothing until ``release``."""
    session: str
    child: Any


@runtime_checkable
class SlotRuntime(Protocol):
    """The credential slot's start and S1 END. ``start`` is failure-atomic like the launcher's ``spawn``: one that
    raises has left no S1 session live. ``end(session)`` is idempotent and answers True only when END proved the slot
    uid's group gone (``Domain.proven``); anything else, an error included, is False."""

    def start(self, req: SlotStart) -> SlotHandle: ...

    def end(self, session: str) -> bool: ...


@runtime_checkable
class UsageSources(Protocol):
    """The readers a subscription run settles on. Each answers None when it cannot read (never a quiet zero)."""

    def calls(self, run_id: str) -> int | None: ...

    def tunnel_bytes(self, session: str) -> int | None: ...

    def seconds(self, session: str) -> int | None: ...


class SessionIndex:
    """The durable record of each session key: what usage and stop need to find the run again. One file per key under
    ``directory`` (mode 0600, atomic replace). It never holds the ticket."""

    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        if type(key) is not str or _KEY_RE.fullmatch(key) is None:
            raise ValueError("a session key is outside the attempt-id grammar")
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """The record under ``key``, None when there is none. A record that is there and unreadable raises (the caller
        reads that as unreadable, never as absent)."""
        path = self._path(key)
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb") as fh:
                fd = None
                data = fh.read(65537)
        finally:
            if fd is not None:
                os.close(fd)
        if len(data) > 65536:
            raise ValueError("an index record past its size cap")
        rec = json.loads(data)
        if type(rec) is not dict or rec.get("format") != _INDEX_FORMAT or rec.get("key") != key \
                or rec.get("state") not in _INDEX_STATES:
            raise ValueError("not an index record")
        return rec

    def put(self, key: str, state: str, **fields: Any) -> None:
        """Write the record under ``key`` whole (replacing), durably."""
        if state not in _INDEX_STATES:
            raise ValueError(f"unknown index state {state!r}")
        path = self._path(key)
        self.dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"format": _INDEX_FORMAT, "key": key, "state": state, **fields}, sort_keys=True).encode()
        tmp = path.with_name(f".{key}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)


def _count(v: object) -> int | None:
    return v if type(v) is int and v >= 0 else None


class _Proc:
    """A stand-in gate for a session this process could not prove ENDed: ``wait`` and ``poll`` retry the END, and answer
    :data:`levain.agents.controller.GATE_UNPROVEN` (the exit that proves nothing) until it is proven."""

    def __init__(self, held: "HeldChild") -> None:
        self._held = held
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._try()

    def wait(self, timeout: float | None = None) -> int:
        self._try()
        return self.returncode if self.returncode is not None else _ctl.GATE_UNPROVEN

    def _try(self) -> None:
        if self.returncode is None and self._held.try_end():
            self.returncode = _ctl.GATE_REFUSED     # nothing ran under it; a non-zero exit that proves the group gone


class HeldChild:
    """The gated child of a launch whose leftover S1 session could not be proven ENDed. It runs nothing (``release``
    does nothing), and it keeps trying the END from ``terminate``, ``wait`` and ``abort``, so the run's own stop and
    abort reach the session. Until END is proven its exit is :data:`~levain.agents.controller.GATE_UNPROVEN`: the run is
    never ``done`` and keeps its slot for the reaper."""

    def __init__(self, runtime: SlotRuntime, session: str) -> None:
        self._runtime, self.session = runtime, session
        self.process = _Proc(self)
        self.released = False

    def try_end(self) -> bool:
        try:
            return bool(self._runtime.end(self.session))
        except Exception:  # noqa: BLE001 - an END that cannot complete proved nothing
            return False

    def release(self) -> None:
        """Nothing to release: this child holds no program."""

    def abort(self, timeout: float = 5.0) -> int:
        self.process._try()
        return self.process.wait()


class _Launched:
    """The child :meth:`RealLauncher.spawn` answers: the runtime's gated child, with S1 END behind ``abort`` (the run's
    own stop path), so an aborted session is ENDed by the slot, not only its local gate closed."""

    def __init__(self, inner: Any, runtime: SlotRuntime, session: str) -> None:
        self._inner, self._runtime, self.session = inner, runtime, session

    @property
    def process(self) -> Any:
        return self._inner.process

    @property
    def released(self) -> bool:
        return bool(getattr(self._inner, "released", False))

    def release(self) -> None:
        self._inner.release()

    def abort(self, timeout: float = 5.0) -> int:
        try:
            return self._inner.abort(timeout)
        finally:
            try:
                self._runtime.end(self.session)
            except Exception:  # noqa: BLE001 - the run's own error is the one that matters; END is retried by stop()
                _log.warning("S1 END for session %s failed after an abort", self.session)


class RealLauncher:
    """A subscription (or, later, API-key) session on an S1 dispatch slot. ``auth`` is the ticket's auth axis this
    launcher mints with (``passthrough``; ``brokered`` is accepted so the refusal is named, not mistaken for a wiring
    gap). ``clock`` returns seconds since the epoch."""

    route = S1_DISPATCH

    def __init__(self, control: ControlPort, runtime: SlotRuntime, sources: UsageSources, index: SessionIndex, *,
                 auth: str = "passthrough", clock: Callable[[], float] = time.time) -> None:
        if auth not in T.AUTHS:
            raise ValueError(f"auth is one of {T.AUTHS}")
        self.control, self.runtime, self.sources, self.index = control, runtime, sources, index
        self.auth = auth
        self.clock = clock

    # -- spawn -------------------------------------------------------------------------------------------------------

    def spawn(self, launch: SessionLaunch) -> Any:
        key = launch.session_key
        self._refuse_before_allocating(launch)
        facts = self._facts(launch)
        # the record exists before the mint: a crash from here on leaves a key usage reads as unreadable (the caps),
        # never as nothing
        existing = self._existing(key)
        if existing is not None:
            raise LauncherRefused("session-key-already-used", f"the index already holds state {existing!r}")
        self.index.put(key, "pending", occurrence_id=launch.occurrence_id, created_s=self.clock())
        ticket, run_id = self._mint(key, facts)
        try:
            self.index.put(key, "minted", occurrence_id=launch.occurrence_id, run_id=run_id, created_s=self.clock())
            handle = self.runtime.start(SlotStart(ticket, run_id, key, launch.harness, launch.model, launch.brief, HOOKS))
        except BaseException:
            self._undo(key, ticket, run_id)
            raise
        try:
            self.index.put(key, "started", occurrence_id=launch.occurrence_id, run_id=run_id, session=handle.session,
                           started_s=self.clock())
        except BaseException:
            # the session is live and the index cannot say so: end it, or hold it
            try:
                handle.child.abort()
            except Exception:  # noqa: BLE001 - END below is the proof; the local gate is closed best effort
                pass
            if self.runtime_end(handle.session):
                self._tombstone(key, "undone", "index-write-failed")
                raise
            return HeldChild(self.runtime, handle.session)
        return _Launched(handle.child, self.runtime, handle.session)

    def _refuse_before_allocating(self, launch: SessionLaunch) -> None:
        why = None
        if self.auth == "brokered" and not BROKERED_ROUTE_BUILT:
            why = ("brokered-route-not-built", "the API-key route needs dispatch wave 3's per-run keys and dollar "
                   "caps (3c-10); only a subscription session is launchable")
        elif launch.auth != self.auth:
            why = ("launch-auth-not-the-launchers", f"launch {launch.auth!r}, launcher {self.auth!r}")
        elif launch.harness != VENDOR:
            why = ("harness-not-claude", str(launch.harness))
        elif launch.exposure is None:
            why = ("no-action-exposure", "a passthrough ticket carries three per-run caps")
        elif launch.exposure.unit != "tool_calls":
            why = ("exposure-unit-not-tool-calls", str(launch.exposure.unit))
        elif launch.exposure.tunnel_bytes is None:
            why = ("no-tunnel-byte-cap", "a passthrough ticket carries a byte cap")
        if why is None:
            return
        self._tombstone(launch.session_key, "refused", why[0])
        raise LauncherRefused(*why)

    def _tombstone(self, key: str, state: str, reason: str) -> None:
        try:
            self.index.put(key, state, reason=reason, created_s=self.clock())
        except Exception:  # noqa: BLE001 - an unwritable tombstone reads as unreadable usage, the safe side
            _log.warning("no %s record could be written for session %s", state, key)

    def _existing(self, key: str) -> str | None:
        try:
            rec = self.index.get(key)
        except ValueError as exc:
            raise LauncherRefused("session-index-unreadable", str(exc)) from None
        return None if rec is None else str(rec["state"])

    def _facts(self, launch: SessionLaunch) -> dict[str, object]:
        """The mint request, checked by CONTROL's own shape function before anything is sent."""
        exp = launch.exposure
        assert exp is not None
        digest = hashlib.sha256(json.dumps({"occurrence_id": launch.occurrence_id, "brief": launch.brief},
                                           sort_keys=True).encode("utf-8")).hexdigest()
        fields: dict[str, object] = {
            "attempt_id": launch.session_key, "job_id": launch.occurrence_id, "run_index": 0,
            "contract_digest": f"sha256:{digest}", "kind": "executor", "egress_routes": [],
            "reserved_microdollars": 0, "auth": "passthrough", "vendor": VENDOR, "model_requested": launch.model,
            "max_calls": exp.actions, "max_tunnel_bytes": exp.tunnel_bytes, "deadline_s": exp.seconds}
        try:
            T.check_attempt_id(fields["attempt_id"])
            T.check_facts(fields)
        except Refused as exc:
            self._tombstone(launch.session_key, "refused", exc.name)
            raise LauncherRefused(f"mint-facts:{exc.name}", exc.detail) from None
        return fields

    def _mint(self, key: str, fields: Mapping[str, object]) -> tuple[str, str]:
        """THE mint: ``RUN-TICKET-MINT`` through the control port (the only call of it in the repo's launchers)."""
        try:
            ans = self.control.call("RUN-TICKET-MINT", **fields)
        except Refused as exc:
            # a refused mint minted nothing; a control-down one may have (the journal is idempotent per attempt, and the
            # index says pending: usage reads the caps until a stop proves otherwise)
            if exc.name != "control-down":
                self._tombstone(key, "refused", f"mint:{exc.name}")
            raise LauncherRefused(f"mint-refused:{exc.name}", exc.detail) from None
        ticket, run_id = ans.get("ticket"), ans.get("run_id")
        if type(ticket) is not str or not ticket or type(run_id) is not str or T.RUN_ID_RE.fullmatch(run_id) is None:
            raise LauncherRefused("mint-answer-malformed", "no ticket or run id in the answer")
        return ticket, run_id

    def _undo(self, key: str, ticket: str, run_id: str) -> None:
        """Undo a mint (and whatever the slot started) after a failure, so the raising ``spawn`` has left nothing of its
        own live. When it cannot be shown undone the index stays ``minted`` (usage reads the caps)."""
        try:
            self.control.call("RUN-TICKET-CANCEL", ticket=ticket)
        except Refused as exc:
            if exc.name != "ticket-bound":
                _log.warning("the ticket for %s could not be cancelled: %s", key, exc.name)
                return
            session = self._bound_session(key)
            if session is None or not self.runtime_end(session):
                _log.warning("the session bound to %s could not be shown ended", key)
                return
        except Exception:  # noqa: BLE001
            _log.warning("the ticket for %s could not be cancelled", key)
            return
        self._tombstone(key, "undone", "spawn-failed")

    def _bound_session(self, key: str) -> str | None:
        try:
            ans = self.control.call("RUN-TICKET-STATUS", attempt_id=key)
        except Exception:  # noqa: BLE001
            return None
        s = ans.get("session")
        return s if type(s) is str and s else None

    def runtime_end(self, session: str) -> bool:
        try:
            return bool(self.runtime.end(session))
        except Exception:  # noqa: BLE001
            return False

    # -- usage and stop, keyed by the recorded session key ------------------------------------------------------------

    def usage(self, session_key: str) -> Usage:
        """What the run spent, read under the RECORDED ``session_key`` and nothing else. Never raises: an unknown key, a
        record that does not read, or a figure the source cannot read is None (the caps), never a zero. A launch that
        provably started nothing (``refused``, ``undone``) reads zero."""
        try:
            rec = self.index.get(session_key)
        except Exception:  # noqa: BLE001
            return Usage(None, None, None, "session:index-unreadable")
        if rec is None:
            return Usage(None, None, None, "session:unindexed")
        if rec["state"] in _ZERO_STATES:
            return Usage(0, 0, 0, "session:not-started", actions=0, tunnel_bytes=0, seconds=0)
        run_id, session = rec.get("run_id"), rec.get("session")
        return Usage(0, None, None, "session:passthrough",
                     actions=self._read(self.sources.calls, run_id), tunnel_bytes=self._read(self.sources.tunnel_bytes, session),
                     seconds=self._read(self.sources.seconds, session))

    @staticmethod
    def _read(fn: Callable[[str], Any], arg: object) -> int | None:
        if type(arg) is not str or not arg:
            return None
        try:
            return _count(fn(arg))
        except Exception:  # noqa: BLE001
            return None

    def stop(self, session_key: str, deadline_s: float = 0.0) -> StopResult:
        """Stop the session recorded under ``session_key`` by S1 END: terminated only on END's proof. Never raises. This
        is the stop a reaper (S3) calls for a session it did not spawn; ``deadline_s`` is the caller's, END is bounded by
        its own."""
        try:
            rec = self.index.get(session_key)
        except Exception:  # noqa: BLE001
            return StopResult(False, "the session index does not read; nothing assumed ended")
        if rec is None:
            return StopResult(False, "no session is recorded under this key; nothing observed")
        if rec["state"] in _ZERO_STATES:
            return StopResult(True, "no session was started")
        session = rec.get("session")
        if type(session) is not str:
            session = self._bound_session(session_key)
        if session is None:
            return StopResult(False, "the record names no slot session; nothing observed")
        if self.runtime_end(session):
            return StopResult(True, "S1 END proved the session's uid group gone")
        return StopResult(False, "S1 END did not prove the session's uid group gone")
