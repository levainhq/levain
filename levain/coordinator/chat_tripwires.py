"""levain.coordinator.chat_tripwires — Claude's mid-turn tripwires (CH-D1 rev 15 §4.4) and the named replies to an
inbound control request that is not an approval (§8.1).

A :class:`TripwireWindow` watches one Claude session's recorded events and says what latches; it writes nothing and
calls no driver. :class:`~levain.coordinator.chat.ChatService` owns it (one per attached session), writes each
:class:`Trip` as a ``not_live`` row with source ``tripwire:<name>``, sends each :class:`ControlReply` through
``DriverSession.reject_control``, and sends the turn's interrupt when a trip asks for one. A tripwire can only switch a
launch off: nothing here ever reads as live (§4.4, "What O8 limits" 4).

The window holds the counts §4.4 compares and the per-call joins (a call's tool name and input from its ``tool_call``;
whether this child raised a host request for it; whether that request was cancelled). Counts reset at a boundary only
when no call is open (the counting window rule, R9 / Q-R5-4); each join is dropped at its call's ``tool_result``. A
state the window cannot account for latches, never passes: a session the service did not watch from its start has no
counts and no joins, so its next ``tool_result`` trips; a turn boundary (a ``result`` line, a turn start) with a call
still open trips and drops every join and count; a read that reports a gap drops the counts, and trips (dropping the
joins too) when a call was open before or after that read. A line that reports a permission mode is read per event.

The tripwires read the Event's fields (``hook``, ``cancel_of``, ``decision_source``, ``control_subtype``), never which
:data:`~levain.coordinator.chat.EVENT_KINDS` value a driver put a hook or cancel line on. Stdlib only.
"""
from __future__ import annotations


import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

__all__ = [
    "CONTROL_REPLIES", "MODE", "MODE_ALLOWED_SHAPE", "TRIPWIRES", "UNPARSEABLE", "ControlReply", "Trip",
    "TripwireContext", "TripwireWindow", "control_line", "launch_roots", "mode_trip", "read_exempt",
    "read_target_within", "reported_mode_trip",
]

TRIPWIRES = ("deficit", "unexpected_rule", "unexpected_hook", "mode_changed", "accepted_without_host",
             "unexpected_control", "unanswerable_control")

# §8.1: the error text each inbound non-approval subtype gets, and whether it latches (elicitation alone does not: an
# MCP server may legitimately ask, and the worker sees a refusal). Any other subtype: "levain does not answer <it>"
CONTROL_REPLIES: dict[str, tuple[str, bool]] = {
    "hook_callback": ("levain registers no SDK hooks", True),
    "mcp_message": ("levain hosts no SDK MCP server", True),
    "elicitation": ("levain answers no elicitation in chat", False),
    "set_permission_mode": ("levain fixes this launch's mode", True),
}
# the vendor-id prefix of an inbound control_request's event (ScriptedSession's ``error:control:<request_id>``), of the
# event a driver gives a line it could not parse (a CH-D2 driver contract line: ``error:unparseable:<its id>``), and of
# the event a driver gives each line that reports a permissionMode (a CH-D2 driver contract line:
# ``error:mode:<its id>``, the body the reported mode verbatim)
CONTROL = "control:"
UNPARSEABLE = "unparseable:"
MODE = "mode:"
_APPROVAL = "can_use_tool"


class TripwireContext(NamedTuple):
    """What the service knows about one Claude launch that the stream does not say (§4.4).

    ``cwd`` is the launch's working directory (``--restricted`` confines the file tools to it). ``expected_pre_hooks(T)``
    is the PreToolUse handler count for tool ``T``: Levain's gate plus the pinned managed hooks whose matcher matches
    ``T``. ``gate_allowed(tool_use_id, tool_input)`` is exemption 1's record: True only when ``queue.db`` holds Levain's
    gate request for this launch's session and that call, allowed by a verified answer (the launcher binds the agent,
    the session and the posture; D:5881's query). Anything but ``True`` from it, an exception included, is no
    exemption."""
    cwd: str
    expected_pre_hooks: Callable[[str], int]
    gate_allowed: Callable[[str, Any], bool]


class Trip(NamedTuple):
    name: str                         # one of TRIPWIRES
    detail: str
    interrupt: bool = False           # unanswerable_control: the service also sends the turn's interrupt
    turn_id: str | None = None


class ControlReply(NamedTuple):
    request_id: str
    text: str


# ---- §4.4 exemption 2, from rev 15 (D:5851-5869); verbatim but for resolving the roots (L3 r1 T8) and ValueError --
MODE_ALLOWED_SHAPE = {"decision": "accept", "source": "config", "reason_type": "mode"}   # rbpp2 a11_inside, 2.1.310

def read_exempt(tool_name: str, tool_input: Any, decision: Mapping[str, Any], ctx: TripwireContext) -> bool:
    # the target is compared resolved, so the roots are too (a cwd under macOS /tmp or /var is a symlink path); a
    # relative or unusable root is no root, and one that cannot be resolved (an embedded NUL) is no exemption
    try:
        roots = tuple(os.path.realpath(r) for r in launch_roots(ctx) if isinstance(r, str) and os.path.isabs(r))
    except (OSError, RuntimeError, ValueError):
        return False
    return (tool_name == "Read"                                   # exact
            and {k: decision.get(k) for k in MODE_ALLOWED_SHAPE} == MODE_ALLOWED_SHAPE
            and read_target_within(tool_input, roots))

def launch_roots(ctx) -> tuple[str, ...]: return (ctx.cwd,)       # --restricted's working directory; no --add-dir
def read_target_within(tool_input: Any, roots: tuple[str, ...]) -> bool:
    fp = tool_input.get("file_path") if isinstance(tool_input, Mapping) else None
    if not isinstance(fp, str) or not fp or not os.path.isabs(fp):
        return False                                               # missing, malformed or relative: latch
    try:
        real = str(Path(fp).resolve(strict=True))                  # a missing target raises: latch
    except (OSError, RuntimeError, ValueError):
        return False                                               # a symlink loop or an embedded NUL too
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots)   # symlink escape: latch
# ---- end verbatim --------------------------------------------------------------------------------------------------


def mode_trip(permission_mode: Any) -> Trip | None:
    """``mode_changed``: the session's mode (its init, or a later line that reported a change) is not ``default``.
    None means no line has reported one yet."""
    if permission_mode is None or permission_mode == "default":
        return None
    return Trip("mode_changed", f"the session reports permission mode {str(permission_mode)[:60]!r}, not 'default'")


def reported_mode_trip(e: Any) -> Trip | None:
    """``mode_changed`` from one event: a line that reports a permissionMode (``error:mode:<id>``, its body the mode)
    other than ``default``, read per event so a mode that changes and changes back inside one read still trips. A
    report whose mode cannot be read trips too."""
    if e.kind != "error" or not e.vendor_id.startswith(MODE):
        return None
    if not isinstance(e.body, str) or not e.body:
        return Trip("mode_changed", "a line reported a permission mode that cannot be read")
    return mode_trip(e.body)


def control_line(e: Any) -> tuple[Trip | None, ControlReply | None] | None:
    """The trip and the reply an inbound control line (or a line the session could not parse) gives; None for any
    other event. It reads no window state: the CLI waits on the request whether or not the batch that carries it is
    recorded, so a batch that crosses a boundary is answered by it too."""
    if e.hook is not None or e.cancel_of is not None or e.kind != "error":
        return None
    vid = e.vendor_id
    if vid.startswith(UNPARSEABLE):
        return Trip("unanswerable_control", "the session read a line it could not parse", True, e.turn_id), None
    if vid.startswith(CONTROL):
        return _control(vid[len(CONTROL):], e.control_subtype, e.turn_id)
    return None


def _control(request_id: str, subtype: Any, turn_id: str | None) -> tuple[Trip | None, ControlReply | None]:
    if not request_id:
        return Trip("unanswerable_control", "a control_request with no usable request_id", True, turn_id), None
    if subtype == _APPROVAL:
        # never answered outside the one door (reject_control refuses it), so the CLI would wait on it: latch, and
        # the turn's interrupt aborts its pending requests (§8.1, O6)
        return Trip("unexpected_control", "a can_use_tool request arrived as a non-approval control request", True,
                    turn_id), None
    if isinstance(subtype, str) and subtype in CONTROL_REPLIES:
        text, latches = CONTROL_REPLIES[subtype]
    else:
        named = subtype if isinstance(subtype, str) and subtype else "this request"
        text, latches = f"levain does not answer {named[:80]}", True
    trip = Trip("unexpected_control", f"inbound control_request {str(subtype)[:60]!r}: {text}") if latches else None
    return trip, ControlReply(request_id, text)


class _Call(NamedTuple):
    tool_name: str | None
    tool_input: Any


class TripwireWindow:
    """One Claude session's tripwire state. :meth:`observe` takes the session's newly recorded, non-history events in
    stream order; :meth:`boundary` is a §4.2 boundary (a service send, a ``result`` line, a turn start). The service
    works on a :meth:`copy` and keeps it only once the transaction that wrote the batch committed."""

    def __init__(self, ctx: TripwireContext | None) -> None:
        self.ctx = ctx
        self.hooks: dict[str, int] = {}           # "PreToolUse:<T>" -> hook_response events in the window
        self.results: dict[str, int] = {}         # T -> tool_results in the window
        self.calls: dict[str, _Call] = {}         # tool_use_id -> its tool_call's name and input, until its result
        self.host: set[str] = set()               # tool_use_ids this child raised a can_use_tool for, until the result
        self.pending: dict[str, str] = {}         # a host request's key -> its tool_use_id, until a cancel or result
        self.cancelled: set[str] = set()          # tool_use_ids whose host request the CLI cancelled, until the result

    def copy(self) -> TripwireWindow:
        """The window's own state copied; the context is the launcher's and is shared, never cloned."""
        w = TripwireWindow(self.ctx)
        w.hooks, w.results, w.calls = dict(self.hooks), dict(self.results), dict(self.calls)
        w.host, w.pending, w.cancelled = set(self.host), dict(self.pending), set(self.cancelled)
        return w

    def boundary(self, turn: bool = False) -> Trip | None:
        """The counts reset only when no call is open (every ``tool_call`` seen has its ``tool_result``): a send's
        boundary never splits a PreToolUse ``hook_response`` from its call's ``tool_result``. A ``turn`` boundary (a
        ``result`` line, a turn start) has no call open in a whole stream; one that does trips ``deficit``, and the
        orphans' joins and every count are dropped, so an orphan never pins the counts."""
        if turn and self.calls:
            orphans = len(self.calls)
            self._forget()
            return Trip("deficit", f"a turn boundary with {orphans} tool call(s) that never got a tool_result")
        if not self.calls:
            self.hooks.clear()
            self.results.clear()
        return None

    def gap(self, was_open: bool) -> Trip | None:
        """A read that reports a gap (lines the session could not read). The counts are dropped (a hook counted before
        the gap never covers a call after it); a call open before or after the read (``was_open``, or one open now)
        may have lost its hooks or its result, so that trips ``deficit`` and drops every join too."""
        if was_open or self.calls:
            self._forget()
            return Trip("deficit", "the read reported a gap with a tool call open: its hooks or result may be lost")
        self.hooks.clear()
        self.results.clear()
        return None

    def _forget(self) -> None:
        self.hooks.clear()
        self.results.clear()
        self.calls.clear()
        self.host.clear()
        self.pending.clear()
        self.cancelled.clear()

    def observe(self, e: Any) -> tuple[Trip | None, ControlReply | None]:
        """The trip (if any) and the control reply (if any) one event gives."""
        if e.hook is not None:
            hook = e.hook if isinstance(e.hook, Mapping) else {}
            if hook.get("event") == "hook_response" and isinstance(hook.get("name"), str):
                self.hooks[hook["name"]] = self.hooks.get(hook["name"], 0) + 1
            return None, None
        if e.cancel_of is not None:
            call = self.pending.pop(e.cancel_of, None)
            if call is not None:
                self.cancelled.add(call)
            return None, None
        vid = e.vendor_id
        mode = reported_mode_trip(e)
        if mode is not None or e.kind == "error" and vid.startswith(MODE):
            return mode, None
        control = control_line(e)
        if control is not None:
            return control
        if e.kind == "tool_call":
            if isinstance(e.call_id, str) and e.call_id:
                self.calls[e.call_id] = _Call(e.tool_name, e.tool_input)
            return None, None
        if e.kind == "approval_request" and e.host:
            if isinstance(e.call_id, str) and e.call_id:
                self.host.add(e.call_id)
                if isinstance(e.request_key, str):
                    self.pending[e.request_key] = e.call_id
            return None, None
        if e.kind == "tool_result":
            return self._result(e), None
        return None, None

    def _result(self, e: Any) -> Trip | None:
        call_id = e.call_id if isinstance(e.call_id, str) and e.call_id else None
        joined = self.calls.pop(call_id, None) if call_id else None
        tool = e.tool_name if isinstance(e.tool_name, str) and e.tool_name else (joined.tool_name if joined else None)
        tool_input = joined.tool_input if joined else None
        host = call_id in self.host if call_id else False
        cancelled = call_id in self.cancelled if call_id else False
        if call_id:
            self.host.discard(call_id)
            self.cancelled.discard(call_id)
            for key in [k for k, v in self.pending.items() if v == call_id]:
                del self.pending[key]
        if not isinstance(tool, str) or not tool:
            return Trip("deficit", "a tool_result whose tool cannot be named: its PreToolUse hooks cannot be counted")
        self.results[tool] = self.results.get(tool, 0) + 1
        trip = self._deficit(tool)
        if trip is not None:
            return trip
        d = e.decision_source
        if not isinstance(d, Mapping):
            return Trip("accepted_without_host", f"a {tool[:60]} tool_result with no permission_decision")
        decision, source, reason = d.get("decision"), d.get("source"), d.get("reason_type")
        if cancelled and source == "hook":
            return Trip("unexpected_hook", f"a hook decided the {tool[:60]} call after its host request was cancelled")
        if cancelled:
            host = False                                  # a withdrawn request is no host answer: no exemption
        if decision == "reject":
            return None                                   # a deny is never refused
        if decision != "accept":
            return Trip("accepted_without_host", f"a {tool[:60]} tool_result with decision {str(decision)[:40]!r}")
        if reason == "rule":
            return Trip("unexpected_rule", f"an allow rule from source {str(source)[:40]!r} answered a {tool[:60]} "
                                           "call; Levain rendered no allow rule")
        if host:
            return None                                   # the host's own answer
        if source == "hook" and call_id is not None and self._gate_allowed(call_id, tool_input):
            return None                                   # exemption 1: Levain's gate allowed this call
        if self.ctx is not None and read_exempt(tool, tool_input, d, self.ctx):
            return None                                   # exemption 2: a Read the mode allows, inside the launch
        return Trip("accepted_without_host", f"a {tool[:60]} call accepted with no host request, source "
                                             f"{str(source)[:40]!r}, reason {str(reason)[:40]!r}")

    def _deficit(self, tool: str) -> Trip | None:
        if self.ctx is None:
            return Trip("deficit", "no tripwire context for this launch: the expected PreToolUse count is unknown")
        try:
            per_call = self.ctx.expected_pre_hooks(tool)
        except Exception as exc:  # noqa: BLE001 - an unknown count never reads as enough
            return Trip("deficit", f"the expected PreToolUse count for {tool[:60]} is unknown: {type(exc).__name__}")
        if type(per_call) is not int or per_call < 1:
            return Trip("deficit", f"the expected PreToolUse count for {tool[:60]} is not a positive int")
        seen, want = self.hooks.get(f"PreToolUse:{tool}", 0), per_call * self.results[tool]
        if seen < want:
            return Trip("deficit", f"{seen} PreToolUse:{tool[:60]} hook responses for {self.results[tool]} "
                                   f"{tool[:60]} results, {want} expected")
        return None

    def _gate_allowed(self, call_id: str, tool_input: Any) -> bool:
        if self.ctx is None:
            return False
        try:
            return self.ctx.gate_allowed(call_id, tool_input) is True
        except Exception:  # noqa: BLE001 - no record read is no exemption
            return False
