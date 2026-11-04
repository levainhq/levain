"""levain.agents.session_launcher: the real :class:`levain.agents.session.SessionLauncher` for a subscription session on
an S1 dispatch slot (BUILD_PLAN P7-1 slice S4; P7-A section 6; head ruling 11-04: option (a), an own launcher on session
kind ``dispatch``, no ``JobStore``, no wait on dispatch wave 3).

:class:`RealLauncher` owns the order of one launch and nothing else; every act on the outside world goes through one of
three seams, so the whole of it runs in pytest against fakes:

* :class:`ControlPort`: ``call(op, **fields)``, the shape of :class:`levain.slot.control_client.ControlClient`. The
  launcher mints through it (``RUN-TICKET-MINT``), and undoes a mint through it (``RUN-TICKET-STATUS`` /
  ``RUN-TICKET-CANCEL``). This module contains the one place that mints (:meth:`RealLauncher._mint`).
* :class:`SlotRuntime`: the credential slot's start (lease, prepare, render, start under the ticket, the agent fence's
  hooks) answering the gated child, and S1 END (``end(session)``: True only when the helper's teardown answer proves
  the slot uid's group gone, read by :func:`teardown_proves`).
* :class:`UsageSources`: the three figures a subscription run settles on (P7-A section 3.3 item 4): the broker's call
  count by ``run_id``, the front's byte total and the session's seconds by slot session.

Order of :meth:`RealLauncher.spawn`: the launch is refused by name before anything is allocated (the API-key branch is
always ``brokered-route-not-built``: it needs dispatch wave 3's per-run keys and dollar caps, 3c-10); the ticket facts
are checked by CONTROL's own shape function (:func:`levain.slot.tickets.check_facts`) and the model by render's grammar;
a ``pending`` record is CREATED (exclusively: that create is the key's existence check, so a second spawn of one key is
refused before it mints and no refusal ever replaces a record) in the :class:`SessionIndex`; the run ticket is minted
with ``attempt_id`` == the session key; the slot is started; the record is completed with the slot session.

What a failing ``spawn`` leaves (the :class:`~levain.agents.session.SessionLauncher` contract: one that raises has left
nothing of its own live). A start that raises is undone: an unbound ticket cancelled (``undone``, usage zero), or the
session CONTROL's ``RUN-TICKET-STATUS`` names ENDed (``undone``). When STATUS names a session whose END is not proven,
``spawn`` does NOT raise: it returns a :class:`HeldChild` that owns that session, so the run keeps its slot and the
run's own stop and abort reach it. It raises with the record left ``minted`` (usage reads the caps) only when no
session is known; that raise claims only that nothing this launcher can name is live.

The child a ``spawn`` returns runs S1 END behind the gate's own exit (:class:`_Launched`): a normal exit answers its
status only once END proved the group gone, and :data:`levain.agents.controller.GATE_UNPROVEN` until then, so a clean
exit is never ``done`` without END's proof. A proven END is recorded (``ended``), and only an ``ended`` record's usage
reads the sources.

Usage is keyed by the RECORDED session key and by nothing else (:meth:`RealLauncher.usage`): the key reaches a durable
:class:`SessionIndex` record (run id, slot session), so a fresh launcher in another process, after a crash, reads the
same figures. The ticket itself is a bearer capability and is never written down. CONTROL's ``RUN-TICKET-STATUS``
answers ``{ticket, state, session?}`` and never the ``run_id``, so the index is the only record of it.

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
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from levain.agents import controller as _ctl
from levain.agents.accounting import StopResult, Usage
from levain.agents.session import S1_DISPATCH, SessionLaunch
from levain.slot import Refused
from levain.slot import tickets as T
from levain.slot.records import RETAINED
from levain.slot.runtime_grammar import _MODEL_RE as _RENDER_MODEL_RE

__all__ = ["VENDOR", "HOOKS", "LauncherRefused", "ControlPort", "SlotStart", "SlotHandle", "SlotRuntime",
           "UsageSources", "SessionIndex", "RealLauncher", "HeldChild", "teardown_proves"]

_log = logging.getLogger("levain.agents.session_launcher")

VENDOR = "claude"
#: the agent fence: the per-call gate hook and the queue hook the slot's render writes (``runtime_grammar.HOOKS``)
HOOKS = ("levain_gate", "levain_queue")
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")      # the attempt-id grammar; a session key is used as the attempt id
_INDEX_FORMAT = "levain-session-index/1"
_INDEX_STATES = ("pending", "minted", "started", "ended", "refused", "undone")
_ZERO_STATES = ("refused", "undone")                  # nothing ran: usage reads zero
#: CONTROL refusals a later attempt may not meet (a slot frees, a lock drops, the journal comes back). Every other
#: name is STANDING: the operator's configuration or the request itself, which a retry meets again.
_TRANSIENT = frozenset({"no-free-slot", "locked", "internal-error", "journal-unavailable", "control-down"})


def _reason_class(name: str) -> str:
    return "transient" if name.split(":", 1)[0] in _TRANSIENT else "standing"


def teardown_proves(answer: object) -> bool:
    """How a :class:`SlotRuntime`'s ``end`` reads the runtime helper's ``teardown`` answer (``levain.slot.start.teardown``
    answers ``{"state": ...}`` and, when the slot stays leased to the session, ``"released": false``):

    * ``RETAINED`` -> True: END proved the slot uid's group gone. ``released: false`` does not change that; it says the
      slot is still leased to the session, which the reaper's SCRUB releases.
    * ``HELD``, any other state, a ``released`` that is not a bool, anything not a dict -> False.
    * a refusal (the helper answering ``ok: false``, raised as :class:`levain.slot.Refused`) is the caller's to read as
      False. A second teardown after SCRUB need not answer RETAINED again (L2 review F9 read it as refusing), so a
      launcher answers a later stop from its own ``ended`` record, never from a second END."""
    return type(answer) is dict and answer.get("state") == RETAINED and type(answer.get("released", False)) is bool


class LauncherRefused(RuntimeError):
    """The launcher refused to start a session. ``name`` is the stable reason; ``reason_class`` is ``transient`` (a
    retry may succeed) or ``standing`` (it will not, until something changes)."""

    def __init__(self, name: str, detail: str = "", *, reason_class: str = "standing") -> None:
        self.name = name
        self.detail = detail
        self.reason_class = reason_class
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
    raises has left no S1 session live. ``end(session)`` answers True only when the helper's teardown answer proves the
    slot uid's group gone (:func:`teardown_proves`); anything else, a refusal or an error included, is False."""

    def start(self, req: SlotStart) -> SlotHandle: ...

    def end(self, session: str) -> bool: ...


@runtime_checkable
class UsageSources(Protocol):
    """The readers a subscription run settles on. Each answers None when it cannot read (never a quiet zero), and
    ``tunnel_bytes`` answers None until the front's FINAL byte total for the session exists. The launcher asks them
    only once S1 END proved the session's group gone."""

    def calls(self, run_id: str) -> int | None: ...

    def tunnel_bytes(self, session: str) -> int | None: ...

    def seconds(self, session: str) -> int | None: ...


class SessionIndex:
    """The durable record of each session key: what usage and stop need to find the run again. One file per key under
    ``directory`` (mode 0600). :meth:`create` is exclusive, :meth:`put` replaces; both write whole (temp, then a link or
    a rename) and fsync the file and the directory. It never holds the ticket."""

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

    def _temp(self, key: str, state: str, fields: Mapping[str, Any]) -> tuple[Path, Path]:
        if state not in _INDEX_STATES:
            raise ValueError(f"unknown index state {state!r}")
        path = self._path(key)
        self.dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps({**fields, "format": _INDEX_FORMAT, "key": key, "state": state}, sort_keys=True).encode()
        tmp = path.with_name(f".{key}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path, tmp

    def _sync_dir(self) -> None:
        fd = os.open(self.dir, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def create(self, key: str, state: str, **fields: Any) -> bool:
        """Write the record under ``key`` only if there is none: True when written, False when a record (any record,
        readable or not) is already there, which is left as it is."""
        path, tmp = self._temp(key, state, fields)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)
        self._sync_dir()
        return True

    def put(self, key: str, state: str, **fields: Any) -> None:
        """Write the record under ``key`` whole (replacing), durably. Only the spawn that CREATED a key's record, and
        the END that proves its session gone, replace it."""
        path, tmp = self._temp(key, state, fields)
        try:
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        self._sync_dir()


def _count(v: object) -> int | None:
    return v if type(v) is int and v >= 0 else None


class _Proc:
    """A stand-in gate for a session this process could not prove ENDed: ``wait`` and ``terminate`` make one END
    attempt each, and the exit is :data:`levain.agents.controller.GATE_UNPROVEN` (the exit that proves nothing) until
    one is proven."""

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
    """The gated child of a launch whose S1 session could not be proven ENDed. It runs nothing (``release`` does
    nothing), and each ``terminate``, ``wait`` and ``abort`` makes one END attempt (no retry loop), so the run's own
    stop and abort reach the session. Until END is proven its exit is :data:`~levain.agents.controller.GATE_UNPROVEN`:
    the run is never ``done`` and keeps its slot for the reaper. ``on_end`` runs once, when END is proven."""

    def __init__(self, runtime: SlotRuntime, session: str, on_end: Callable[[], None] | None = None) -> None:
        self._runtime, self.session, self._on_end = runtime, session, on_end
        self.process = _Proc(self)
        self.released = False

    def try_end(self) -> bool:
        try:
            ok = bool(self._runtime.end(self.session))
        except Exception:  # noqa: BLE001 - an END that cannot complete proved nothing
            return False
        if ok and self._on_end is not None:
            self._on_end()
        return ok

    def release(self) -> None:
        """Nothing to release: this child holds no program."""

    def abort(self, timeout: float = 5.0) -> int:
        self.process._try()
        return self.process.returncode if self.process.returncode is not None else _ctl.GATE_UNPROVEN


class _SlotProc:
    """The inner gate's process with S1 END behind its exit: ``wait`` and ``poll`` answer the gate's status only once
    END proved the slot uid's group gone (one END attempt per call while it is not), and
    :data:`levain.agents.controller.GATE_UNPROVEN` until then. ``terminate`` is the gate's."""

    def __init__(self, inner: Any, launched: "_Launched") -> None:
        self._p, self._l = inner, launched

    def _settle(self, rc: int | None) -> int | None:
        if rc is None:
            return None
        return rc if self._l.end_once() else _ctl.GATE_UNPROVEN

    @property
    def returncode(self) -> int | None:
        rc = getattr(self._p, "returncode", None)
        return None if rc is None else (rc if self._l.proven else _ctl.GATE_UNPROVEN)

    def poll(self) -> int | None:
        return self._settle(self._p.poll())

    def wait(self, timeout: float | None = None) -> int | None:
        return self._settle(self._p.wait(timeout))

    def terminate(self) -> None:
        self._p.terminate()

    def __getattr__(self, name: str) -> Any:
        if name in ("_p", "_l"):
            raise AttributeError(name)
        return getattr(self._p, name)


class _Launched:
    """The child :meth:`RealLauncher.spawn` answers: the runtime's gated child, with S1 END behind the gate's exit
    (:class:`_SlotProc`) and behind ``abort`` (the run's own stop path), so a session is over only when the slot's END
    proved it, not when its local gate closed."""

    def __init__(self, inner: Any, runtime: SlotRuntime, session: str, on_end: Callable[[], None]) -> None:
        self._inner, self._runtime, self.session, self._on_end = inner, runtime, session, on_end
        self.proven = False
        self.process = _SlotProc(inner.process, self)

    def end_once(self) -> bool:
        """One S1 END attempt, unless one was already proven. Never raises."""
        if self.proven:
            return True
        try:
            ok = bool(self._runtime.end(self.session))
        except Exception:  # noqa: BLE001 - an END that cannot complete proved nothing
            _log.warning("S1 END for session %s failed", self.session)
            ok = False
        if ok:
            self.proven = True
            self._on_end()
        return ok

    @property
    def released(self) -> bool:
        return bool(getattr(self._inner, "released", False))

    def release(self) -> None:
        self._inner.release()

    def abort(self, timeout: float = 5.0) -> int:
        try:
            rc = self._inner.abort(timeout)
        finally:
            self.end_once()
        return rc if self.proven else _ctl.GATE_UNPROVEN


class RealLauncher:
    """A subscription session on an S1 dispatch slot. ``auth`` is the ticket's auth axis this launcher mints with
    (``passthrough``; ``brokered`` is accepted so its refusal is named, not mistaken for a wiring gap: no setting makes
    a brokered launcher mint). ``clock`` returns seconds since the epoch."""

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
        # the record exists before the mint (a crash from here on leaves a key usage reads as unreadable, the caps,
        # never as nothing), and creating it is the existence check: a key already held is refused before any mint
        if not self.index.create(key, "pending", occurrence_id=launch.occurrence_id, created_s=self.clock()):
            raise LauncherRefused("session-key-already-used", f"the index already holds state {self._existing(key)!r}")
        # AUTHORITY: whoever builds a launcher can mint for any occurrence_id and caps; run_session is the guard,
        # with CONTROL's ceiling behind it
        ticket, run_id = self._mint(key, facts)
        try:
            self.index.put(key, "minted", occurrence_id=launch.occurrence_id, run_id=run_id, created_s=self.clock())
            handle = self.runtime.start(SlotStart(ticket, run_id, key, launch.harness, launch.model, launch.brief, HOOKS))
        except Exception as exc:
            held = self._undo(key, ticket, run_id, launch.occurrence_id)
            if held is None:
                raise
            _log.warning("the slot start for %s raised %s: %s; its session is held", key, type(exc).__name__, exc)
            return held
        except BaseException:
            # an interrupt propagates (run_session does not read it as a known spawn failure); undone best effort
            self._undo(key, ticket, run_id, launch.occurrence_id)
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
        return _Launched(handle.child, self.runtime, handle.session, lambda: self._mark_ended(key))

    def _refuse_before_allocating(self, launch: SessionLaunch) -> None:
        why = None
        if self.auth == "brokered":
            why = ("brokered-route-not-built", "the API-key route needs dispatch wave 3's per-run keys and dollar "
                   "caps (3c-10); only a subscription session is launchable")
        elif self.auth != "passthrough":
            why = ("auth-not-launchable", f"a session launcher mints passthrough tickets only, not {self.auth!r}")
        elif launch.auth != self.auth:
            why = ("launch-auth-not-the-launchers", f"launch {launch.auth!r}, launcher {self.auth!r}")
        elif launch.harness != VENDOR:
            why = ("harness-not-claude", str(launch.harness))
        elif type(launch.model) is not str or _RENDER_MODEL_RE.fullmatch(launch.model) is None:
            why = ("model-outside-render-grammar", "the slot's render would refuse this model")
        elif launch.exposure is None:
            why = ("no-action-exposure", "a passthrough ticket carries three per-run caps")
        elif launch.exposure.unit != "tool_calls":
            why = ("exposure-unit-not-tool-calls", str(launch.exposure.unit))
        elif launch.exposure.tunnel_bytes is None:
            why = ("no-tunnel-byte-cap", "a passthrough ticket carries a byte cap")
        if why is None:
            return
        self._tombstone(launch.session_key, "refused", why[0], new=True)
        raise LauncherRefused(*why)

    def _tombstone(self, key: str, state: str, reason: str, *, new: bool = False, **extra: Any) -> None:
        """A zero-usage record. ``new``: only where the key holds no record (a refusal of a key some other spawn
        holds leaves that spawn's record as it is); else it replaces the record this spawn created."""
        try:
            if new:
                self.index.create(key, state, reason=reason, created_s=self.clock(), **extra)
            else:
                self.index.put(key, state, reason=reason, created_s=self.clock(), **extra)
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
            "reserved_microdollars": 0, "auth": self.auth, "vendor": VENDOR, "model_requested": launch.model,
            "max_calls": exp.actions, "max_tunnel_bytes": exp.tunnel_bytes, "deadline_s": exp.seconds}
        try:
            T.check_attempt_id(fields["attempt_id"])
            T.check_facts(fields)
        except Refused as exc:
            self._tombstone(launch.session_key, "refused", exc.name, new=True)
            raise LauncherRefused(f"mint-facts:{exc.name}", exc.detail) from None
        return fields

    def _mint(self, key: str, fields: Mapping[str, object]) -> tuple[str, str]:
        """THE mint: ``RUN-TICKET-MINT`` through the control port (the only call of it in the repo's launchers)."""
        try:
            ans = self.control.call("RUN-TICKET-MINT", **fields)
        except Refused as exc:
            # a refused mint minted nothing; a control-down one may have (the journal is idempotent per attempt, and the
            # index says pending: usage reads the caps until a stop proves otherwise)
            cls = _reason_class(exc.name)
            if exc.name != "control-down":
                self._tombstone(key, "refused", f"mint:{exc.name}", reason_class=cls)
            raise LauncherRefused(f"mint-refused:{exc.name}", exc.detail, reason_class=cls) from None
        except Exception as exc:  # noqa: BLE001 - not a CONTROL answer: it may have minted, so the record stays pending
            raise LauncherRefused("mint-failed", f"{type(exc).__name__}; the record is left pending",
                                  reason_class="transient") from None
        ticket = ans.get("ticket") if type(ans) is dict else None
        run_id = ans.get("run_id") if type(ans) is dict else None
        if type(ticket) is not str or not ticket or type(run_id) is not str or T.RUN_ID_RE.fullmatch(run_id) is None:
            raise LauncherRefused("mint-answer-malformed", self._settle_malformed(key))
        return ticket, run_id

    def _settle_malformed(self, key: str) -> str:
        """A mint answer that names no ticket and run id: ask CONTROL by attempt id what was minted, cancel it while
        it is unbound, and say what was left."""
        try:
            st = self.control.call("RUN-TICKET-STATUS", attempt_id=key)
        except Refused as exc:
            if exc.name == "attempt-unknown":
                self._tombstone(key, "refused", "mint-answer-malformed")
                return "no ticket or run id in the answer; CONTROL minted nothing under this attempt"
            return f"no ticket or run id in the answer; left pending (STATUS refused {exc.name})"
        except Exception as exc:  # noqa: BLE001
            return f"no ticket or run id in the answer; left pending (STATUS failed: {type(exc).__name__})"
        state = st.get("state") if type(st) is dict else None
        tk = st.get("ticket") if type(st) is dict else None
        if state != T.UNBOUND or type(tk) is not str or not tk:
            return f"no ticket or run id in the answer; left pending (STATUS state {state!r})"
        try:
            self.control.call("RUN-TICKET-CANCEL", ticket=tk)
        except Exception as exc:  # noqa: BLE001
            name = exc.name if isinstance(exc, Refused) else type(exc).__name__
            return f"no ticket or run id in the answer; left pending (cancel refused {name})"
        self._tombstone(key, "undone", "mint-answer-malformed")
        return "no ticket or run id in the answer; the minted ticket was cancelled"

    def _undo(self, key: str, ticket: str, run_id: str, occurrence_id: str) -> "HeldChild | None":
        """Undo a mint (and whatever the slot started) after a failed start. None: undone, or nothing this launcher
        can name is live (the record then stays ``minted`` and usage reads the caps); the caller raises. A
        :class:`HeldChild`: a session is bound to the ticket and its END is unproven; the caller returns it."""
        try:
            self.control.call("RUN-TICKET-CANCEL", ticket=ticket)
        except Exception as exc:  # noqa: BLE001 - any failed cancel asks CONTROL what the ticket is bound to
            name = exc.name if isinstance(exc, Refused) else type(exc).__name__
            session, state = self._status(key)
            if session is None:
                if state in (T.CANCELLED, T.EXPIRED):
                    self._tombstone(key, "undone", "spawn-failed")       # closed before any binding: nothing ran
                else:
                    _log.warning("the ticket for %s could not be cancelled (%s) and names no session", key, name)
                return None
            if self.runtime_end(session):
                self._tombstone(key, "undone", "spawn-failed")
                return None
            try:
                self.index.put(key, "started", occurrence_id=occurrence_id, run_id=run_id, session=session,
                               started_s=self.clock())
            except Exception:  # noqa: BLE001 - the record stays minted: stop() still finds the session by STATUS
                _log.warning("the held session of %s could not be recorded", key)
            return HeldChild(self.runtime, session, on_end=lambda: self._mark_ended(key))
        self._tombstone(key, "undone", "spawn-failed")
        return None

    def _status(self, key: str) -> tuple[str | None, str | None]:
        """``RUN-TICKET-STATUS`` by attempt id: (the bound session, the state), each None when it does not read."""
        try:
            ans = self.control.call("RUN-TICKET-STATUS", attempt_id=key)
        except Exception:  # noqa: BLE001
            return None, None
        if type(ans) is not dict:
            return None, None
        s, state = ans.get("session"), ans.get("state")
        return (s if type(s) is str and s else None), (state if type(state) is str else None)

    def _mark_ended(self, key: str) -> None:
        """S1 END proved the session's group gone: record it, so usage reads the sources and a later stop needs no
        second END. A record that cannot be written stays as it was (usage reads the caps, the safe side)."""
        try:
            rec = self.index.get(key)
            if rec is None or rec["state"] not in ("minted", "started"):
                return
            fields = {k: v for k, v in rec.items() if k not in ("format", "key", "state")}
            self.index.put(key, "ended", **{**fields, "ended_s": self.clock()})
        except Exception:  # noqa: BLE001
            _log.warning("the END of session %s could not be recorded", key)

    def runtime_end(self, session: str) -> bool:
        try:
            return bool(self.runtime.end(session))
        except Exception:  # noqa: BLE001
            return False

    # -- usage and stop, keyed by the recorded session key ------------------------------------------------------------

    def usage(self, session_key: str) -> Usage:
        """What the run spent, read under the RECORDED ``session_key`` and nothing else. Never raises. A launch that
        provably started nothing (``refused``, ``undone``) reads zero. The sources are read only for an ``ended``
        record (S1 END proved the group gone); every other case (an unknown key, a record that does not read, a
        session not proven ended, a figure the source cannot read, a brokered launcher) is None, the caps."""
        try:
            rec = self.index.get(session_key)
        except Exception:  # noqa: BLE001
            return Usage(None, None, None, "session:index-unreadable")
        if rec is None:
            return Usage(None, None, None, "session:unindexed")
        if rec["state"] in _ZERO_STATES:
            return Usage(0, 0, 0, "session:not-started", actions=0, tunnel_bytes=0, seconds=0)
        if self.auth != "passthrough":
            return Usage(None, None, None, f"session:{self.auth}-usage-unbuilt")
        if rec["state"] != "ended":
            return Usage(None, None, None, "session:end-unproven")
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
        """Stop the session recorded under ``session_key`` by S1 END: terminated only on END's proof (recorded, so a
        later stop answers from the record, not from a second END). Never raises. This is the stop a reaper (S3)
        calls for a session it did not spawn; ``deadline_s`` is the caller's, END is bounded by its own."""
        try:
            rec = self.index.get(session_key)
        except Exception:  # noqa: BLE001
            return StopResult(False, "the session index does not read; nothing assumed ended")
        if rec is None:
            return StopResult(False, "no session is recorded under this key; nothing observed")
        if rec["state"] in _ZERO_STATES:
            return StopResult(True, "no session was started")
        if rec["state"] == "ended":
            return StopResult(True, "S1 END already proved the session's uid group gone")
        session = rec.get("session")
        if type(session) is not str:
            session, _ = self._status(session_key)
        if session is None:
            return StopResult(False, "the record names no slot session; nothing observed")
        if self.runtime_end(session):
            self._mark_ended(session_key)
            return StopResult(True, "S1 END proved the session's uid group gone")
        return StopResult(False, "S1 END did not prove the session's uid group gone")
