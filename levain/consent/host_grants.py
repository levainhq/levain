"""The cockpit's side of host grants (board levain#66, slice S5a): list the live host grants of the operator's own
egress sessions, and revoke one or all of a session's, from the phone.

The guarantee is narrow and exact: untrusted HTTP fields can reach only these validated helper invocations, and
nothing else here. Two root verbs, argv built from checked parts (no shell, stdin ``DEVNULL``, ``child_env()``):

* ``egress-grants-all`` (no argument): every live grant of the operator's ACTIVE or BRAKED interactive or login egress
  sessions, one root call for the whole page (never ``status``, which returns every session ever prepared);
* ``egress-grant-revoke <session> <eh-... | all>``: ``session`` must fully match ``SESSION_RE``, the target ``all`` or
  ``eh-`` plus 24 lowercase hex digits. The grammar the root helper applies is the boundary; the check here fails fast,
  before a spawn.

That is input confinement, not process confinement: this process already runs as the operator, whose sudoers rule
covers the whole helper, so code running inside it could call any verb. What S5 adds is the first request-driven path to
a root verb, and the allowlist is :meth:`HostGrantSurface.call`, in this module, not in its callers.

Revoke only narrows: the root helper never adds a grant here (that takes a passkey-signed message root verifies). A
per-id revoke is sent only for a ``(session, id)`` the cockpit's own listing holds; any other is answered ``gone`` with
no revoke spawn, so a credential holder cannot fill root's install-wide revoked-id ledger (``egress-grant-ledger-full``)
through this route. One request is at most one listing spawn and one revoke spawn, never an automatic retry.

Every wait is bounded by one absolute monotonic ``deadline`` the route gives (the route takes it from the request's
start); no spawn starts with under :data:`MIN_SPAWN_S` left. The root helper child is never signalled
(``adopt_view.call_helper``: "the root helper: no signal, no wait"), so a timed-out child keeps the spawn lock until
it exits (a reaper thread waits on it; root's own bounds end it: ``sudo -n`` never prompts and the two verbs read
files or wait the ledger lock for ``LEDGER_LOCK_WAIT_S``; nothing here kills a root process): never two root helpers
at once, and every request in that time answers that a previous root helper is still running. A child whose reaper
could not be started stays held, and the next request that takes the lock checks it has exited before it spawns. A
revoke whose outcome root did not answer clearly (timed out, silent, malformed) after root was started is
``unknown``, never "not revoked": root may have applied it after the CLI stopped waiting. A failure before root was
started is "not revoked".
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from levain.slot import Refused
from levain.slot.grammar import SESSION_RE

_log = logging.getLogger(__name__)

#: The only two verbs this module ever sends. ``status`` is not one of them.
LIST_VERB = "egress-grants-all"
REVOKE_VERB = "egress-grant-revoke"
ALLOWED_VERBS = (LIST_VERB, REVOKE_VERB)

#: One request's total wall (s), listing and revoke together. The page's reads give up at 15 s
#: (``CONSENT_FETCH_TIMEOUT_MS = 15000`` in dashboard_core.js), so this stays under it.
HOSTGRANT_WALL_S = 12.0
#: No helper call starts with less than this left of the wall.
MIN_SPAWN_S = 1.0
#: The listing helper call's own cap (s).
LIST_TIMEOUT_S = 10.0
#: The revoke call's cap beyond root's ledger-lock wait (s).
REVOKE_EXTRA_S = 5.0
#: A listing's answer is reused this long (s); every revoke clears it.
CACHE_S = 5.0
#: Revokes one credential name may send per window.
RATE_LIMIT = 10
RATE_WINDOW_S = 60.0
#: A helper answer over this many bytes is malformed.
ANSWER_MAX = 256 * 1024
#: Requests waiting on the one listing in flight; past it the answer is busy (a thread each, outside the gate).
MAX_WAITERS = 16
#: Revoke requests inside the surface at once (one spawns, the next waits its turn for the spawn lock).
MAX_REVOKES = 2

_GRANT_RE = re.compile(r"eh-[0-9a-f]{24}")
_TARGET_RE = re.compile(r"all|eh-[0-9a-f]{24}")
_STATES = ("ACTIVE", "BRAKED")
_CHOICES = ("s", "d")

#: Helper refusals of a revoke that mean "the grant was not in force by the time root looked": an id root did not hold
#: is recorded (or, with the ledger full, refused); neither can mean a live grant survived.
_GONE_NAMES = ("egress-grant-ledger-full",)
_BUSY_NAMES = ("session-busy", "egress-grant-busy")
#: A revoke's failures where root's answer is not a clear one: it may have applied the revoke. ``unknown`` ONLY when the
#: spawn began (``_began``); the same name before root was started (``host-grants-unreadable`` from building the env)
#: is "not revoked".
_UNKNOWN_NAMES = ("helper-timeout", "helper-silent", "helper-output-malformed", "host-grants-answer-malformed",
                  "host-grants-unreadable")

Spawn = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _note_unreadable(name: str) -> Dict[str, Any]:
    """A failure to read the listing, as the whole answer (never an empty list). No ``slots`` key."""
    if name == "egress-grant-not-ready":
        words = ("host grants are not set up on this machine: the enrol step and the setup re-run have not been "
                 "done, so there is nothing to list yet")
    elif name == "host-grants-helper-running":
        words = ("a previous root helper is still running (the CLI stopped waiting for it and never signals root); "
                 "the list reads again once it exits")
    else:
        words = f"host grants cannot be read now: {name}"
    return {"unreadable": True, "name": name, "note": words}


def _ok_text(x: object, cap: int) -> bool:
    return isinstance(x, str) and 0 < len(x) <= cap and x.isprintable()


def parse_listing(ans: Dict[str, object]) -> Dict[str, Any]:
    """The aggregate verb's answer checked whole and reshaped for the page. Anything off its shape is
    ``Refused("host-grants-answer-malformed")``."""
    bad = Refused("host-grants-answer-malformed", LIST_VERB)
    slots, unread = ans.get("slots"), ans.get("unread")
    if not isinstance(slots, list) or not isinstance(unread, list) or len(slots) > 64 or len(unread) > 64:
        raise bad
    out_slots: List[Dict[str, Any]] = []
    for s in slots:
        if not isinstance(s, dict):
            raise bad
        sess, slot, kind, state, entries = s.get("session"), s.get("slot"), s.get("kind"), s.get("state"), s.get("entries")
        if (not isinstance(sess, str) or SESSION_RE.fullmatch(sess) is None or type(slot) is not int
                or not _ok_text(kind, 32) or state not in _STATES or not isinstance(entries, list) or len(entries) > 64):
            raise bad
        grants: List[Dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                raise bad
            gid, host, exp, choice = e.get("grant"), e.get("host"), e.get("expires"), e.get("choice")
            if (not isinstance(gid, str) or _GRANT_RE.fullmatch(gid) is None or not _ok_text(host, 253)
                    or not _ok_text(exp, 64) or choice not in _CHOICES):
                raise bad
            grants.append({"grant": gid, "host": host, "expires": exp, "choice": choice})
        out_slots.append({"slot": slot, "session": sess, "kind": kind, "state": state, "paused": state == "BRAKED",
                          "grants": grants})
    out_unread: List[Dict[str, Any]] = []
    for u in unread:
        if not isinstance(u, dict) or type(u.get("slot")) is not int or not _ok_text(u.get("name"), 80):
            raise bad
        out_unread.append({"slot": u["slot"], "name": u["name"]})
    return {"slots": out_slots, "unread": out_unread}


class HostGrantSurface:
    """What the cockpit's host-grant routes hold: ``listing(deadline)`` and ``revoke(session, target, credential,
    deadline)``. ``spawn`` is the process seam (the shape of ``adopt_view.Seams.spawn``; a test passes a fake, the
    default is the real one). It never raises into the route."""

    def __init__(self, spawn: "Optional[Spawn]" = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._spawn = spawn
        self._clock = clock
        self._cv = threading.Condition()
        self._spawn_lock = threading.Lock()         # one helper child at a time, listing or revoke, until it EXITS
        self._left: "Optional[subprocess.Popen[bytes]]" = None   # a timed-out child with no reaper yet (under the lock)
        self._reaping: "Optional[subprocess.Popen[bytes]]" = None  # the child a reaper waits on (it holds the lock)
        self._began = False                         # the last call's spawn began (under the lock)
        self._inflight = False
        self._waiters = 0
        self._cache: "Optional[Dict[str, Any]]" = None
        self._cache_until = 0.0
        self._gen = 0
        self._rate: Dict[str, Deque[float]] = {}
        self._rate_lock = threading.Lock()
        self._revokes = threading.BoundedSemaphore(MAX_REVOKES)

    # -- the one function that starts a helper -------------------------------------------------------------------

    def call(self, verb: str, args: Tuple[str, ...], timeout: float) -> Dict[str, object]:
        """The ONE place a root helper is started. The allowlist and the argument checks run here, before any spawn:
        the verb is one of :data:`ALLOWED_VERBS`; the aggregate takes no argument; a revoke takes a session that fully
        matches ``SESSION_RE`` and a target that fully matches ``all|eh-[0-9a-f]{24}``. Raises ``Refused`` (a refusal
        by name, before a spawn for a bad call; root's own for a refused one) or ``ValueError`` for a call this
        module must never make."""
        if verb not in ALLOWED_VERBS:
            raise ValueError(f"host grants never send the verb {verb!r}")
        if verb == LIST_VERB and args:
            raise ValueError(f"{verb} takes no argument")
        if verb == REVOKE_VERB and not (
                len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], str)
                and SESSION_RE.fullmatch(args[0]) is not None and _TARGET_RE.fullmatch(args[1]) is not None):
            raise ValueError(f"{verb}: a session and an id or all, checked")
        from levain.slot import adopt_view

        spawn = self._spawn if self._spawn is not None else adopt_view.Seams().spawn

        self._began = False

        def capped(argv: Any, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
            try:
                p = spawn(argv, **kw)
            except BaseException as e:
                child = getattr(e, "child", None)           # the runner started it and stopped waiting
                if child is not None or isinstance(e, subprocess.TimeoutExpired):
                    self._began = True
                if child is not None:
                    self._left = child                      # may still run: _release_spawn holds the lock until it exits
                raise
            self._began = True
            if len(p.stdout or b"") > ANSWER_MAX:
                raise Refused("host-grants-answer-malformed", f"{verb}: over {ANSWER_MAX} bytes")
            return p

        # shell never: call_helper builds argv as a list and spawns it with stdin DEVNULL and child_env()
        return adopt_view.call_helper(adopt_view.Seams(spawn=capped), verb, *args, err=io.StringIO(), timeout=timeout)

    # -- the listing ----------------------------------------------------------------------------------------------

    def listing(self, deadline: float, release_gate: "Optional[Callable[[], None]]" = None) -> Dict[str, Any]:
        """The page's list: ``{slots, unread}`` or, when it cannot be read, ``{unreadable, name, note}`` with no
        ``slots`` key (an unreadable list is never an empty one). One listing in flight; a request that finds one
        calls ``release_gate`` (it gives back its request-gate slot) and waits for that answer within ``deadline``.
        A fresh answer (:data:`CACHE_S`) is returned at once."""
        with self._cv:
            while True:
                now = self._clock()
                if self._cache is not None and now < self._cache_until:
                    got = self._cache
                    break
                if not self._inflight:
                    self._inflight = True
                    got = None
                    gen = self._gen
                    break
                if release_gate is not None:
                    release_gate()
                    release_gate = None
                left = deadline - now
                if left <= 0 or self._waiters >= MAX_WAITERS:
                    return _note_unreadable("host-grants-busy" if left > 0 else "host-grants-deadline")
                self._waiters += 1
                try:
                    self._cv.wait(left)
                finally:
                    self._waiters -= 1
        if got is not None:
            if release_gate is not None:
                release_gate()
            return got
        cacheable = False
        try:
            result, cacheable = self._fetch(deadline)
        except BaseException:
            with self._cv:
                self._inflight = False
                self._cv.notify_all()
            raise
        with self._cv:
            self._inflight = False
            if cacheable and gen == self._gen:
                self._cache, self._cache_until = result, self._clock() + CACHE_S
            self._cv.notify_all()
        return result

    def _fetch(self, deadline: float) -> "Tuple[Dict[str, Any], bool]":
        """One aggregate call within the wall. (answer, cacheable): a call that never started is not cached."""
        left = deadline - self._clock()
        if left < MIN_SPAWN_S:
            return _note_unreadable("host-grants-deadline"), False
        busy = self._take_spawn(left - MIN_SPAWN_S)
        if busy is not None:
            return _note_unreadable(busy), False
        try:
            left = deadline - self._clock()
            if left < MIN_SPAWN_S:
                return _note_unreadable("host-grants-deadline"), False
            try:
                ans = self.call(LIST_VERB, (), min(left, LIST_TIMEOUT_S))
                return parse_listing(ans), True
            except Refused as e:
                return _note_unreadable(e.name), True
            except Exception as e:  # noqa: BLE001 - a spawn fault, a bad answer: a note, never an empty list
                _log.warning("host grants listing failed: %s: %s", type(e).__name__, e)
                return _note_unreadable("host-grants-unreadable"), True
        finally:
            self._release_spawn()

    def _take_spawn(self, timeout: float) -> "Optional[str]":
        """Take the spawn lock within ``timeout``: None when it is held and no earlier child still runs, else the busy
        name (the lock not held). A child whose reaper could not start (``_left`` set) is checked here, under the lock:
        still running, it goes back to :meth:`_release_spawn` (a reaper again); exited, it is dropped."""
        if not self._spawn_lock.acquire(timeout=max(0.0, timeout)):
            return "host-grants-helper-running" if self._reaping is not None else "host-grants-busy"
        child = self._left
        if child is not None:
            if child.poll() is None:
                self._release_spawn()
                return "host-grants-helper-running"
            self._left = None
        return None

    def _release_spawn(self) -> None:
        """Give the spawn lock back, or, when the call left its child running (a timeout), hand it to a reaper thread
        that gives it back once that child exits. If the reaper cannot start, the child stays in ``_left`` and the lock
        is given back: the next :meth:`_take_spawn` sees the child and spawns nothing beside it while it runs.
        ``child.wait()`` has no bound here: root's own bounds end the helper, and it is never killed from here."""
        child = self._left
        if child is None:
            self._spawn_lock.release()
            return

        def reap() -> None:
            try:
                child.wait()
            finally:
                self._reaping = None
                self._spawn_lock.release()

        self._left, self._reaping = None, child
        try:
            threading.Thread(target=reap, name="host-grants-reap", daemon=True).start()
        except BaseException as e:  # noqa: BLE001 - no thread: the reaper never ran, the lock is still this call's
            self._left, self._reaping = child, None         # the state is consistent first, for any failure
            self._spawn_lock.release()
            if not isinstance(e, Exception):
                raise                                       # an interrupt (KeyboardInterrupt, SystemExit) is not swallowed
            _log.warning("host grants: no reaper for a running root helper: %s: %s", type(e).__name__, e)

    def _invalidate(self) -> None:
        with self._cv:
            self._cache = None
            self._cache_until = 0.0
            self._gen += 1

    # -- the revoke -----------------------------------------------------------------------------------------------

    def _rate_ok(self, credential: str) -> bool:
        now = self._clock()
        with self._rate_lock:
            q = self._rate.setdefault(credential, deque())
            while q and now - q[0] >= RATE_WINDOW_S:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                return False
            q.append(now)
            if len(self._rate) > 256:       # credentials are few; keep the table bounded all the same
                for k in [k for k, v in self._rate.items() if not v or now - v[-1] >= RATE_WINDOW_S]:
                    del self._rate[k]
            return True

    def revoke(self, session: str, target: str, credential: str, deadline: float) -> "Tuple[Dict[str, Any], int]":
        """``(answer, http status)``. ``result`` is ``revoked`` (with ``count``), ``gone`` (not in force: nothing was
        sent to root to revoke, or root says it was not there) or an ``error`` (``rate_limited`` 429, ``bad_request``
        400, ``busy`` 409, ``refused`` 502, ``unknown`` 502, ``unreadable`` 503). ``revoked``, ``gone`` and ``unknown``
        (root was started and did not answer clearly: it may have applied the revoke) carry a fresh ``listing``, which
        may itself be an unreadable note (a refusal changes nothing the page shows; it re-lists on its own). The rate
        limit counts the revoke calls that reach root only. ``credential`` is the credential's NAME (never its token)."""
        if (not isinstance(credential, str) or not isinstance(session, str) or SESSION_RE.fullmatch(session) is None
                or not isinstance(target, str) or _TARGET_RE.fullmatch(target) is None):
            return {"error": "bad_request", "message": "a session and a grant id or all are needed"}, 400
        if not self._revokes.acquire(blocking=False):
            return {"error": "busy", "message": "not revoked; busy, tap again"}, 409
        try:
            return self._revoke(session, target, credential, deadline)
        except Exception as e:  # noqa: BLE001 - never raised into the route
            _log.warning("host grant revoke failed: %s: %s", type(e).__name__, e)
            return {"error": "refused", "name": "host-grants-unreadable",
                    "message": "not revoked: host grants cannot be read now"}, 502
        finally:
            self._revokes.release()

    def _revoke(self, session: str, target: str, credential: str, deadline: float) -> "Tuple[Dict[str, Any], int]":
        held = self.listing(deadline)
        if held.get("unreadable"):
            return {"error": "unreadable", "name": held["name"], "message": "not revoked: " + held["note"],
                    "listing": held}, 503
        rows = [s for s in held["slots"] if s["session"] == session]
        in_force = bool(rows) if target == "all" else any(g["grant"] == target for s in rows for g in s["grants"])
        if not in_force:
            # nothing was sent to root and nothing changed: the listing just read is the fresh one
            _log.info("host grant revoke credential=%r session=%s target=%s answer=gone-no-spawn", credential, session,
                      target)
            return {"result": "gone", "count": 0, "message": "that grant was already gone: expired or revoked",
                    "listing": held}, 200
        left = deadline - self._clock()
        if left < MIN_SPAWN_S:
            return {"error": "busy", "message": "not revoked; busy, tap again"}, 409
        busy = self._take_spawn(left - MIN_SPAWN_S)
        if busy == "host-grants-helper-running":
            return {"error": "busy", "name": busy, "message": "not revoked; a previous root helper is still running, "
                                                              "tap again once it has finished"}, 409
        if busy is not None:
            return {"error": "busy", "message": "not revoked; busy, tap again"}, 409
        try:
            left = deadline - self._clock()
            if left < MIN_SPAWN_S:
                return {"error": "busy", "message": "not revoked; busy, tap again"}, 409
            if not self._rate_ok(credential):
                return {"error": "rate_limited", "message": "too many revokes from this device; wait a minute"}, 429
            from levain.slot import egress_ledger as EL

            refusal = None    # type: Optional[str]
            try:
                ans = self.call(REVOKE_VERB, (session, target), min(left, EL.LEDGER_LOCK_WAIT_S + REVOKE_EXTRA_S))
            except Refused as e:
                refusal, ans = e.name, {}
            except Exception as e:  # noqa: BLE001
                _log.warning("host grant revoke: %s: %s", type(e).__name__, e)
                refusal, ans = "host-grants-unreadable", {}
            began = self._began
        finally:
            self._release_spawn()
        # the fresh listing in every answer below takes the spawn lock itself: never call it while holding it
        if refusal is not None:
            return self._refused(refusal, credential, session, target, deadline, began=began)
        removed = ans.get("removed")
        if (not isinstance(removed, list) or not isinstance(ans.get("recorded"), list)
                or any(not isinstance(x, str) or _GRANT_RE.fullmatch(x) is None for x in removed)):
            return self._refused("host-grants-answer-malformed", credential, session, target, deadline)
        if removed:
            n = len(removed)
            return self._answer({"result": "revoked", "count": n,
                                 "message": (f"revoked the {n} grant{'' if n == 1 else 's'} that "
                                             f"{'was' if n == 1 else 'were'} in force")},
                                credential, session, target, f"removed={n}", deadline)
        return self._answer({"result": "gone", "count": 0,
                             "message": "that grant was already gone: expired or revoked"},
                            credential, session, target, "recorded-gone", deadline)

    def _refused(self, name: str, credential: str, session: str, target: str, deadline: float, *,
                 began: bool = True) -> "Tuple[Dict[str, Any], int]":
        """A revoke root did not answer ``revoked``. ``began`` False: root was never started, so nothing changed."""
        unknown = began and name in _UNKNOWN_NAMES
        _log.info("host grant revoke credential=%r session=%s target=%s answer=%s:%s", credential, session, target,
                  "unknown" if unknown else "refused", name)
        self._invalidate()
        if name in _GONE_NAMES:
            return self._answer({"result": "gone", "count": 0,
                                 "message": "that grant was already gone: expired or revoked"},
                                credential, session, target, None, deadline)
        if name in _BUSY_NAMES:
            return {"error": "busy", "name": name, "message": "not revoked; busy, tap again"}, 409
        if unknown:
            # the attached listing is often a busy note (a timed-out helper still holds the spawn): it does not settle it
            body, _ = self._answer({"error": "unknown", "name": name,
                                    "message": "root did not answer clearly: the grant may or may not be revoked; "
                                               "refresh the list once root's helper has finished"},
                                   credential, session, target, None, deadline)
            return body, 502
        if not began:
            return {"error": "refused", "name": name,
                    "message": f"not revoked: root was not started ({name})"}, 502
        words = ("narrowed, but the change may not survive a power loss" if name == "egress-grant-unsure"
                 else f"not revoked: root refused ({name})")
        return {"error": "refused", "name": name, "message": words}, 502

    def _answer(self, body: Dict[str, Any], credential: str, session: str, target: str, answer: "Optional[str]",
                deadline: float) -> "Tuple[Dict[str, Any], int]":
        """A success body with a fresh listing: the cache is cleared first, so the page never reads its own revoke back
        from before it."""
        if answer is not None:
            _log.info("host grant revoke credential=%r session=%s target=%s answer=%s", credential, session, target,
                      answer)
        self._invalidate()
        body["listing"] = self.listing(deadline)
        return body, 200
