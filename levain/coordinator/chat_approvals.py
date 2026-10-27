"""levain.coordinator.chat_approvals — the approval records of a chat launch (CH-D1c-C: CH-D1 rev 15 §2.5, §2.6, §3.4,
§8.1, §8.2, §8.4).

``chat_approvals`` is one vendor approval REQUEST (its durable key is deterministic: the vendor's own key plus a hash of
the call, so a replay finds its row and a different call under the same raw key gets a row of its own);
``chat_approval_events`` is what happened to it, append-only. This module holds the two tables' DDL
(:data:`APPROVAL_SCHEMA`, which :mod:`levain.coordinator.chat` folds into ``CHAT_SCHEMA`` so ``_ensure`` brings a
released store current through its one path), the key builders, the per-harness agent-config classifier, the §8.1
arrival transaction without the core call, the quarantine row, and the four durable-cancellation writers:
:func:`latch_not_live`, :func:`record_abstain`, :func:`record_cancelled` and :func:`cancel_launch_requests`, plus the
derived :func:`ambiguous_stops`.

NOT HERE: ``attempt_answer`` and the restart table (CH-D1c-D). The one narrow deny writer in this module
(:func:`_deny_attempt`, used by :func:`latch_not_live` and by the arrival's ``gate not live``) writes the same two rows
(``decision_observed`` then ``answer_attempted``) that ``attempt_answer`` step 6 writes, so the later door can take the
rows over unchanged. Nothing in this module calls ``DriverSession.answer``: it returns :class:`Attempt` values and the
caller writes the bytes.

This module imports nothing from :mod:`levain.coordinator.chat` at import time (chat imports it for its DDL); the
functions that need chat's liveness fold or its row helpers import it inside the call. Stdlib plus the store helpers.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from levain.coordinator.store import CoordError, _append_only, _in, next_gseq

if TYPE_CHECKING:
    from levain.coordinator.chat import ChatStore

__all__ = [
    "APPROVAL_EVENT_KINDS", "APPROVAL_KINDS", "APPROVAL_SCHEMA", "CANCEL_CAUSES", "Arrival", "AmbiguousStop",
    "ApprovalRow", "Attempt", "CancelCall", "CancelRegistry", "ChatLaunchContext", "agent_config_tagged",
    "ambiguous_stops", "append_event", "approval", "arrive", "cancel_launch_requests", "card_headline", "claude_raw_key",
    "codex_command_call", "codex_file_change_call", "codex_raw_key", "durable_key", "latch_not_live",
    "mark_driver_lost", "pending_requests", "quarantine_raw_key", "record_abstain", "record_cancelled",
    "record_quarantine", "turn_live",
]

APPROVAL_KINDS = ("command", "file_change", "permission", "host", "quarantine")
ROUTES = ("native", "core", "quarantine")
# §2.6 as built: rev 4's core_skipped and rev 5's answer_refused included, plus rev 9's answer_written (R9-4)
APPROVAL_EVENT_KINDS = ("core_started", "core_result", "decision_observed", "answer_attempted", "answer_confirmed",
                        "answer_ambiguous", "answer_redelivered", "resolved_outside", "turn_ended", "decided_after_turn",
                        "driver_lost", "core_skipped", "answer_refused", "answer_written")
EVENT_BYS = ("service", "operator", "outside", "core")
ANSWER_SURFACES = ("local_cockpit", "remote_cockpit", "phone", "cli")
# §8.7 step 1: a request carrying any of these is no longer pending
TERMINAL_KINDS = ("turn_ended", "resolved_outside", "driver_lost", "decided_after_turn", "answer_confirmed",
                  "answer_ambiguous")
# §3.4 R8-2: every cause that raises a cancel on the in-flight core calls of a launch
CANCEL_CAUSES = ("turn_ended", "not_live", "control_cancel_request", "stop", "kill", "stdin closed", "service shutdown")
# the causes whose terminal row is driver_lost; the others are decided_after_turn (§3.4 record_cancelled)
_DRIVER_LOST_CAUSES = ("stop", "kill", "stdin closed", "service shutdown")
# record_cancelled leaves a request carrying any of these alone: a terminal, or one the decision door owns
_CANCEL_SKIP_KINDS = frozenset(TERMINAL_KINDS) | {"answer_attempted", "decision_observed"}
_CAUSE_MAX = 200
_HEX32 = re.compile(r"[0-9a-f]{32}")
_DENY_DECISIONS = ("decline", "cancel", "denied", "abort")      # a Codex row's refusing decisions, best first

_FROZEN_APPROVAL_COLUMNS = ("launch_id", "vendor_request_key", "gseq", "raw_vendor_key", "vendor_turn_id",
                            "vendor_call_id", "approval_id", "kind", "route", "available_decisions", "tool_name",
                            "tool_input_digest", "targets_agent_config", "spawn_nonce", "at", "rowid")

APPROVAL_SCHEMA: tuple[str, ...] = (
    # §2.5: one vendor approval request. The key CHECKs pin the deterministic shape (raw key + ':call:' + 64 lowercase
    # hex), so no writer can store an alias; the quarantine CHECKs are rev 7 (R7-7)
    "CREATE TABLE IF NOT EXISTS chat_approvals (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "vendor_request_key TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, raw_vendor_key TEXT NOT NULL, "
    "transport_key TEXT NOT NULL, "
    "vendor_turn_id TEXT NOT NULL, vendor_call_id TEXT, approval_id TEXT, "
    f"kind TEXT NOT NULL CHECK ({_in('kind', APPROVAL_KINDS)}), "
    f"route TEXT NOT NULL CHECK ({_in('route', ROUTES)}), available_decisions TEXT NOT NULL, "
    "tool_name TEXT NOT NULL, tool_input_digest TEXT NOT NULL, "
    "targets_agent_config INTEGER NOT NULL DEFAULT 0 CHECK (targets_agent_config IN (0, 1)), "
    "spawn_nonce TEXT, at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_request_key), "
    "CHECK (length(raw_vendor_key) > 0 AND length(transport_key) > 0 AND length(tool_input_digest) > 0), "
    "CHECK (spawn_nonce IS NULL OR (length(spawn_nonce) = 32 AND spawn_nonce NOT GLOB '*[^0-9a-f]*')), "
    "CHECK (substr(vendor_request_key, 1, length(raw_vendor_key) + 6) = raw_vendor_key || ':call:' "
    "AND length(vendor_request_key) = length(raw_vendor_key) + 70 "
    "AND substr(vendor_request_key, length(raw_vendor_key) + 7) NOT GLOB '*[^0-9a-f]*'), "
    "CHECK (kind <> 'host' OR spawn_nonce IS NOT NULL), "
    "CHECK ((kind = 'quarantine') = (route = 'quarantine')), "
    "CHECK ((kind = 'quarantine') = (tool_name = '')), "
    "CHECK (kind <> 'quarantine' OR (targets_agent_config = 1 AND spawn_nonce IS NOT NULL "
    "AND raw_vendor_key = 'quarantine:' || spawn_nonce || ':' || tool_input_digest)))",
    # append-only but for the one column a replay refreshes: transport_key (the id a reply is addressed by on the
    # CURRENT connection); every other column is frozen
    "CREATE TRIGGER IF NOT EXISTS chat_approvals_frozen BEFORE UPDATE ON chat_approvals WHEN "
    + " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _FROZEN_APPROVAL_COLUMNS)
    + " BEGIN SELECT RAISE(ABORT, 'a chat approval is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS chat_approvals_kept BEFORE DELETE ON chat_approvals "
    "BEGIN SELECT RAISE(ABORT, 'a chat approval is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS chat_approvals_no_replace BEFORE INSERT ON chat_approvals WHEN EXISTS (SELECT 1 FROM "
    "chat_approvals WHERE (launch_id = NEW.launch_id AND vendor_request_key = NEW.vendor_request_key) "
    "OR gseq = NEW.gseq OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat approval is never replaced'); END",
    # §2.6: what happened to a request, append-only, one seq line per request
    "CREATE TABLE IF NOT EXISTS chat_approval_events (launch_id TEXT NOT NULL, vendor_request_key TEXT NOT NULL, "
    "seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, "
    f"kind TEXT NOT NULL CHECK ({_in('kind', APPROVAL_EVENT_KINDS)}), "
    "decision TEXT, queue_request_id TEXT, via TEXT, reply TEXT, reply_sha256 TEXT, transport_gen INTEGER, "
    f"op_id TEXT, by TEXT NOT NULL CHECK ({_in('by', EVENT_BYS)}), answered_by TEXT, "
    f"answer_surface TEXT CHECK (answer_surface IS NULL OR {_in('answer_surface', ANSWER_SURFACES)}), "
    "owner_epoch INTEGER, cause TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_request_key, seq), "
    "FOREIGN KEY (launch_id, vendor_request_key) REFERENCES chat_approvals(launch_id, vendor_request_key), "
    "CHECK ((reply IS NULL) = (reply_sha256 IS NULL)), "
    "CHECK (kind <> 'answer_attempted' OR (op_id IS NOT NULL AND reply IS NOT NULL)), "
    "CHECK (kind <> 'answer_written' OR op_id IS NOT NULL), "
    "CHECK ((kind = 'answer_redelivered') = (transport_gen IS NOT NULL)))",
    *_append_only("chat_approval_events", "an approval event"),
    "CREATE TRIGGER IF NOT EXISTS chat_approval_events_next BEFORE INSERT ON chat_approval_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM chat_approval_events WHERE launch_id = NEW.launch_id "
    "AND vendor_request_key = NEW.vendor_request_key), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM chat_approval_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'an approval event is the next seq of its request'); END",
    # the CAS: of two racing transitions exactly one commits (§2.6, §8.7 step 6); the fourth index is rev 9's single
    # driver-confirmed write (R9-4)
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_approval_one_decision ON chat_approval_events (launch_id, "
    "vendor_request_key) WHERE kind = 'decision_observed'",
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_approval_one_attempt ON chat_approval_events (launch_id, "
    "vendor_request_key) WHERE kind = 'answer_attempted'",
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_approval_one_redelivery ON chat_approval_events (launch_id, "
    "vendor_request_key, transport_gen) WHERE kind = 'answer_redelivered'",
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_approval_one_written ON chat_approval_events (launch_id, "
    "vendor_request_key) WHERE kind = 'answer_written'",
    # one core result per request: a retried abstain or cancel cannot write a second card row
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_approval_one_core_result ON chat_approval_events (launch_id, "
    "vendor_request_key) WHERE kind = 'core_result'",
)

# §2.8: the view's approval branch, two arms appended to chat.CHAT_SCHEMA's chat_timeline: the request as it was asked
# (its status the request's latest event kind, 'pending' before any) and each event of it. A request's input is never
# shown here, only its tool name; a quarantined line has no usable name
APPROVAL_VIEW_ARMS = (
    "UNION ALL SELECT j.value, a.gseq, 'approval', 'vendor', a.vendor_request_key, a.launch_id, "
    "COALESCE((SELECT v.kind FROM chat_approval_events v WHERE v.launch_id = a.launch_id AND v.vendor_request_key = "
    "a.vendor_request_key ORDER BY v.seq DESC LIMIT 1), 'pending'), "
    "CASE WHEN a.kind = 'quarantine' THEN 'an uncorrelatable request' ELSE a.tool_name END, a.at, NULL "
    "FROM chat_approvals a JOIN launches l ON l.launch_id = a.launch_id JOIN json_each(l.threads) j "
    "UNION ALL SELECT j.value, v.gseq, 'approval_' || v.kind, v.by, v.vendor_request_key, v.launch_id, v.decision, "
    "v.cause, v.at, NULL FROM chat_approval_events v JOIN launches l ON l.launch_id = v.launch_id "
    "JOIN json_each(l.threads) j"
)


class ChatLaunchContext(NamedTuple):
    """§3.4 (Q-C): what the core and the classifier read about a launch, built once by the launcher at spawn and held by
    the service beside the session; never re-read from ``os.environ``. ``environ`` is a frozen copy of the exact env the
    child was spawned with; ``queue_dir`` is the launcher's ``queue_dir_for(environ)``; ``home`` is the operator's home."""
    queue_dir: Path
    cwd: str
    home: Path
    environ: Mapping[str, str]
    spawn_nonce: str
    claude_pid: int


# --- keys ---------------------------------------------------------------------------------------------------------
def durable_key(raw_vendor_key: str, tool_name: str, tool_input_digest: str) -> str:
    """§2.5 (R4): ``raw + ":call:" + sha256(tool_name + "\\0" + tool_input_digest)``. The same call regenerates the same
    key from what the vendor sent; a different call under the same raw key gets another. No store-local allocation."""
    if not isinstance(raw_vendor_key, str) or not raw_vendor_key or not isinstance(tool_name, str) \
            or not isinstance(tool_input_digest, str) or not tool_input_digest:
        raise CoordError("a durable approval key needs a raw key, a tool name and an input digest")
    return raw_vendor_key + ":call:" + hashlib.sha256((tool_name + "\0" + tool_input_digest).encode()).hexdigest()


def claude_raw_key(spawn_nonce: str, tool_use_id: object) -> str:
    """§2.5 (rev 4): ``"can_use_tool:" + spawn_nonce + ":" + tool_use_id``. The transport ``request_id`` never enters it;
    a request with no usable ``tool_use_id`` has no key (it is a quarantine row, :func:`record_quarantine`)."""
    if not isinstance(spawn_nonce, str) or _HEX32.fullmatch(spawn_nonce) is None:
        raise CoordError("a spawn nonce is 32 lowercase hex")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise CoordError("a Claude request without a tool_use_id is uncorrelatable")
    return f"can_use_tool:{spawn_nonce}:{tool_use_id}"


def codex_raw_key(method: str, item_id: str, approval_id: str | None) -> str:
    """§2.5: ``method + ":" + params.itemId + ":" + (params.approvalId or "-")``."""
    if not all(isinstance(v, str) and v for v in (method, item_id)):
        raise CoordError("a Codex request names its method and its item")
    return f"{method}:{item_id}:{approval_id or '-'}"


def quarantine_raw_key(spawn_nonce: str, raw_line: bytes) -> tuple[str, str]:
    """``(raw key, tool_input_digest)`` of an uncorrelatable line (R7-7): the digest is ``sha256`` of the line's bytes as
    read, before JSON decoding; the key is ``"quarantine:" + spawn_nonce + ":" + digest``."""
    if not isinstance(spawn_nonce, str) or _HEX32.fullmatch(spawn_nonce) is None:
        raise CoordError("a spawn nonce is 32 lowercase hex")
    if not isinstance(raw_line, (bytes, bytearray)):
        raise CoordError("a quarantined line is its raw bytes")
    digest = hashlib.sha256(bytes(raw_line)).hexdigest()
    return f"quarantine:{spawn_nonce}:{digest}", digest


def codex_command_call(params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """§2.5: Codex ``item/commandExecution/requestApproval`` is the call ``("commandExecution", {"command", "cwd"})``."""
    return "commandExecution", {"command": params.get("command"), "cwd": params.get("cwd")}


def codex_file_change_call(changes: Sequence[Sequence[str]] | None) -> tuple[str, dict[str, Any] | None]:
    """§2.5: Codex ``item/fileChange/requestApproval`` is ``(apply_patch, {"changes": [[kind, path], ...]})`` from the
    item as the stream recorded it, sorted; ``None`` input when the item was not seen."""
    from levain.consent.adapters.rules import PATCH_TOOL
    if changes is None:
        return PATCH_TOOL, None
    return PATCH_TOOL, {"changes": sorted([str(c[0]), str(c[1])] for c in changes)}


# --- the per-harness agent-config classifier (§2.5, R5) -----------------------------------------------------------
_PATCH_VERBS = {"add": "Add File", "create": "Add File", "update": "Update File", "modify": "Update File",
                "delete": "Delete File", "remove": "Delete File"}


def _patch_text(changes: Sequence[Sequence[str]]) -> str:
    lines = ["*** Begin Patch"]
    for kind, path in changes:
        lines.append(f"*** {_PATCH_VERBS.get(str(kind).lower(), 'Update File')}: {path}")
    lines.append("*** End Patch")
    return "\n".join(lines)


def agent_config_tagged(harness: str, tool_name: str, tool_input: Any, ctx: ChatLaunchContext, *,
                        protected: tuple[Path, ...] = ()) -> bool:
    """Whether a request targets an agent's config, selected BY HARNESS with the launch's context and never
    ``os.environ`` (R5). Claude: the Claude Code classifier. Codex: the Codex classifier with ``CODEX_HOME`` from the
    launch's own environ else ``<home>/.codex`` (never ``codex_home()``, whose fallback expands ``~`` from the process
    environment); a ``fileChange`` call's input is the ``apply_patch`` header text built from its changes, ``changes ==
    None`` gives ``{"command": None}`` (tagged). An unknown harness is tagged (fail closed)."""
    if harness in ("claude", "claude-code"):
        from levain.consent.adapters import claude_code
        return bool(claude_code.targets_agent_config(tool_name, tool_input, ctx.cwd, home=ctx.home,
                                                     protected=protected, environ=ctx.environ))
    if harness == "codex":
        from levain.consent.adapters import codex
        from levain.consent.adapters.rules import PATCH_TOOL
        codex_dir = Path(ctx.environ["CODEX_HOME"]) if ctx.environ.get("CODEX_HOME") else ctx.home / ".codex"
        shown = tool_input
        if tool_name == PATCH_TOOL:
            changes = tool_input.get("changes") if isinstance(tool_input, Mapping) else None
            shown = {"command": None} if changes is None else {"command": _patch_text(changes)}
        return bool(codex.targets_agent_config(tool_name, shown, ctx.cwd, home=ctx.home, codex_dir=codex_dir,
                                               protected=protected, environ=ctx.environ))
    return True


# --- rows ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalRow:
    """One ``chat_approvals`` row."""
    launch_id: str
    vendor_request_key: str
    gseq: int
    raw_vendor_key: str
    vendor_turn_id: str
    vendor_call_id: str | None
    approval_id: str | None
    kind: str
    route: str
    available_decisions: tuple[str, ...]
    tool_name: str
    tool_input_digest: str
    targets_agent_config: bool
    spawn_nonce: str | None
    at: str
    transport_key: str      # the driver's Event.request_key: the id a reply is addressed by (raw_vendor_key is identity)


_COLUMNS = ("launch_id, vendor_request_key, gseq, raw_vendor_key, vendor_turn_id, vendor_call_id, approval_id, kind, "
            "route, available_decisions, tool_name, tool_input_digest, targets_agent_config, spawn_nonce, at, "
            "transport_key")


def _row(values: Sequence[Any]) -> ApprovalRow:
    v = list(values)
    return ApprovalRow(*v[:9], tuple(json.loads(v[9])), v[10], v[11], bool(v[12]), v[13], v[14], v[15])


def approval(conn: sqlite3.Connection, launch_id: str, vendor_request_key: str) -> ApprovalRow | None:
    got = conn.execute(f"SELECT {_COLUMNS} FROM chat_approvals WHERE launch_id = ? AND vendor_request_key = ?",
                       (launch_id, vendor_request_key)).fetchone()
    return None if got is None else _row(got)


@dataclass(frozen=True)
class Attempt:
    """A deny whose two rows (``decision_observed``, ``answer_attempted``) committed: the caller now writes ``reply`` to
    the session (``ApprovalAnswer(op_id, vendor_request_key, raw_vendor_key, reply)``) and records the outcome.
    ``raw_vendor_key`` here is the row's TRANSPORT key (the driver's ``Event.request_key``), the id the session
    addresses a reply by; the row's identity key is ``vendor_request_key``."""
    launch_id: str
    vendor_request_key: str
    raw_vendor_key: str
    op_id: str
    reply: Mapping[str, Any]
    reply_sha256: str


def _clip(text: object) -> str:
    flat = " ".join(str(text).split()) or "no reason given"
    return "".join(c if c.isprintable() else "?" for c in flat)[:_CAUSE_MAX]


def append_event(conn: sqlite3.Connection, now: str, *, launch_id: str, key: str, kind: str, by: str, cause: str,
                 decision: str | None = None, queue_request_id: str | None = None, via: str | None = None,
                 reply: str | None = None, transport_gen: int | None = None, op_id: str | None = None,
                 answered_by: str | None = None, answer_surface: str | None = None,
                 owner_epoch: int | None = None) -> int:
    """One ``chat_approval_events`` row in the caller's transaction; its seq. The table's CHECKs, indexes and triggers
    hold the rules for any writer (one decision, one attempt, one write per request; a seq that follows the last)."""
    if kind not in APPROVAL_EVENT_KINDS:
        raise CoordError(f"an approval event is one of {', '.join(APPROVAL_EVENT_KINDS)}")
    if approval(conn, launch_id, key) is None:
        raise CoordError(f"no approval request {str(key)[:60]!r} on launch {launch_id}")
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_approval_events WHERE launch_id = ? AND "
                       "vendor_request_key = ?", (launch_id, key)).fetchone()[0]
    sha = None if reply is None else hashlib.sha256(reply.encode()).hexdigest()
    conn.execute("INSERT INTO chat_approval_events (launch_id, vendor_request_key, seq, gseq, kind, decision, "
                 "queue_request_id, via, reply, reply_sha256, transport_gen, op_id, by, answered_by, answer_surface, "
                 "owner_epoch, cause, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, key, seq, next_gseq(conn, now), kind, decision, queue_request_id, via, reply, sha,
                  transport_gen, op_id, by, answered_by, answer_surface, owner_epoch, _clip(cause), now))
    return seq


def _kinds(conn: sqlite3.Connection, launch_id: str, key: str) -> set[str]:
    return {k for (k,) in conn.execute("SELECT kind FROM chat_approval_events WHERE launch_id = ? AND "
                                       "vendor_request_key = ?", (launch_id, key))}


def pending_requests(conn: sqlite3.Connection, launch_id: str, *, unattempted: bool = False) -> list[str]:
    """The launch's request keys with none of the terminal kinds of §8.7 step 1, in arrival order. ``unattempted`` also
    leaves out a request that already carries ``answer_attempted`` or ``decision_observed`` (the latch's list: a decided
    request is the door's, and a second ``decision_observed`` would break its unique index)."""
    blocked = TERMINAL_KINDS + (("answer_attempted", "decision_observed") if unattempted else ())
    return [k for (k,) in conn.execute(
        "SELECT a.vendor_request_key FROM chat_approvals a WHERE a.launch_id = ? AND NOT EXISTS (SELECT 1 FROM "
        "chat_approval_events e WHERE e.launch_id = a.launch_id AND e.vendor_request_key = a.vendor_request_key AND "
        f"{_in('e.kind', blocked)}) ORDER BY a.gseq", (launch_id,))]


def _ended_turn(conn: sqlite3.Connection, row: ApprovalRow) -> bool:
    if row.spawn_nonce is not None:
        return conn.execute("SELECT 1 FROM chat_events WHERE launch_id = ? AND kind = 'turn_ended' AND gseq > ? LIMIT 1",
                            (row.launch_id, row.gseq)).fetchone() is not None
    return conn.execute("SELECT 1 FROM chat_events WHERE launch_id = ? AND kind = 'turn_ended' AND vendor_turn_id = ? "
                        "LIMIT 1", (row.launch_id, row.vendor_turn_id)).fetchone() is not None


def _latest_nonce(conn: sqlite3.Connection, launch_id: str) -> str | None:
    got = conn.execute("SELECT spawn_nonce FROM chat_launch_checks WHERE launch_id = ? AND source = 'activation' AND "
                       "spawn_nonce IS NOT NULL ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()
    return None if got is None else got[0]


def turn_live(conn: sqlite3.Connection, row: ApprovalRow) -> str | None:
    """§8.7 step 2: ``None`` while the request's turn is live, else the reason. Codex: no ``turn_ended`` event for the
    request's turn. Claude: the request's nonce is the latest activation's, and no ``turn_ended`` event of the launch
    has a ``gseq`` above the request's."""
    if _ended_turn(conn, row):
        return "turn ended"
    if row.spawn_nonce is not None and row.spawn_nonce != _latest_nonce(conn, row.launch_id):
        return "activation replaced"
    return None


# --- the arrival transaction (§8.1) minus the core call ----------------------------------------------------------
def _reply_text(reply: Mapping[str, Any]) -> str:
    return json.dumps(reply, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deny_reply(row: ApprovalRow, cause: str) -> tuple[Mapping[str, Any], str]:
    """The refusal a row's harness understands, always buildable: Claude's ``{"behavior": "deny", "message": ...}``;
    Codex's, by the request's method: ``item/permissions/requestApproval`` (kind ``permission``) is denied with
    ``{"permissions": {}}`` and carries no decision list; a command or file-change approval uses the first refusing
    decision among its ``available_decisions`` (the list is optional on the wire), else the pinned default
    ``{"decision": "decline"}``. (``codex_wire.SERVER_REQUESTS`` holds these replies; the literals are unconfirmed
    against a live Codex.)"""
    if row.spawn_nonce is not None or row.kind == "quarantine":
        return {"behavior": "deny", "message": f"Levain: the gate is not live ({cause})"}, "deny"
    if row.kind == "permission":
        return {"permissions": {}}, "deny"
    for want in _DENY_DECISIONS:
        if want in row.available_decisions:
            return {"decision": want}, want
    return {"decision": "decline"}, "decline"


def _deny_attempt(conn: sqlite3.Connection, now: str, row: ApprovalRow, *, cause: str) -> Attempt:
    """``decision_observed`` (by service) then ``answer_attempted`` with the deny literal, in the caller's
    transaction: the two rows ``attempt_answer`` step 6 writes. The unique indexes make a second one raise."""
    reply, decision = _deny_reply(row, cause)
    text = _reply_text(reply)
    op_id = "op-" + secrets.token_hex(16)
    append_event(conn, now, launch_id=row.launch_id, key=row.vendor_request_key, kind="decision_observed",
                 by="service", cause=cause, decision=decision)
    append_event(conn, now, launch_id=row.launch_id, key=row.vendor_request_key, kind="answer_attempted",
                 by="service", cause=cause, decision=decision, reply=text, op_id=op_id)
    return Attempt(row.launch_id, row.vendor_request_key, row.transport_key, op_id, reply,
                   hashlib.sha256(text.encode()).hexdigest())


@dataclass(frozen=True)
class Arrival:
    """What the arrival transaction did with one request. ``replay``: the same call came again, nothing written.
    ``denied``: the gate was not live, the deny is in ``attempt``. ``probing``: wait for the verdict (§4.2).
    ``routed``: ``row.route`` says where it goes. ``quarantined``: a quarantine row, its one answer is a deny through
    the one door (CH-D1c-D)."""
    disposition: Literal["replay", "denied", "probing", "routed", "quarantined"]
    row: ApprovalRow
    attempt: Attempt | None = None


def _launch_harness(conn: sqlite3.Connection, launch_id: str) -> str:
    from levain.coordinator import chat
    chat._worker_launch(conn, launch_id)
    return conn.execute("SELECT harness FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()[0]


def _insert(conn: sqlite3.Connection, now: str, **f: Any) -> ApprovalRow:
    conn.execute(f"INSERT INTO chat_approvals ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (f["launch_id"], f["key"], next_gseq(conn, now), f["raw"], f["turn"], f["call"], f["approval_id"],
                  f["kind"], f["route"], json.dumps(list(f["decisions"])), f["tool_name"], f["digest"],
                  int(f["tagged"]), f["nonce"], now, f["transport"]))
    got = approval(conn, f["launch_id"], f["key"])
    assert got is not None
    return got


def _refresh_transport(conn: sqlite3.Connection, held: ApprovalRow, transport_key: str) -> ApprovalRow:
    """A replay (same durable key) carries the CURRENT connection's transport id: record it, the one column the table
    lets change, so a later deny is addressed to the live id."""
    if held.transport_key == transport_key:
        return held
    conn.execute("UPDATE chat_approvals SET transport_key = ? WHERE launch_id = ? AND vendor_request_key = ?",
                 (transport_key, held.launch_id, held.vendor_request_key))
    got = approval(conn, held.launch_id, held.vendor_request_key)
    assert got is not None
    return got


def arrive(conn: sqlite3.Connection, now: str, *, launch_id: str, raw_vendor_key: str, transport_key: str,
           vendor_turn_id: str, vendor_call_id: str | None, approval_id: str | None, kind: str,
           available_decisions: Sequence[str] | None,
           tool_name: str, tool_input: Any, ctx: ChatLaunchContext, protected: tuple[Path, ...] = (),
           spawn_nonce: str | None = None) -> Arrival:
    """§8.1 in the caller's ``BEGIN IMMEDIATE``, minus the core call: compute the durable key from the raw key and the
    call, look it up (a hit is a replay: nothing is written), else write the ``chat_approvals`` row with the harness's
    own agent-config tag; then read the liveness fold. Neither enabled nor ``probing``: the ``gate not live`` deny is
    committed here as an :class:`Attempt` (a request that arrives after a latch meets this). ``probing``: the caller
    waits for the verdict. Otherwise the row's ``route``: ``core`` for a Claude host request, else ``native``. A call the
    key cannot be built from (an input that cannot be digested, a tool name that is not a non-empty string) raises
    :class:`CoordError`: the caller writes a quarantine row instead. ``transport_key`` is the driver's
    ``Event.request_key`` (what a reply is addressed by); ``available_decisions`` is optional (``None`` stores ``()``)."""
    from levain.coordinator import chat
    from levain.consent.record import tool_input_digest
    harness = _launch_harness(conn, launch_id)
    if kind not in APPROVAL_KINDS or kind == "quarantine":
        raise CoordError("an arriving request is a command, file_change, permission or host request")
    if not isinstance(tool_name, str) or not tool_name:
        raise CoordError("uncorrelatable: no usable tool name")
    if kind == "host" and spawn_nonce is None:
        raise CoordError("a host request names the child it came from")
    if not isinstance(transport_key, str) or not transport_key:
        raise CoordError("a request names the transport key its reply is addressed by")
    try:
        digest = tool_input_digest(tool_input)
    except (TypeError, ValueError):
        raise CoordError("uncorrelatable: the tool input cannot be digested") from None
    key = durable_key(raw_vendor_key, tool_name, digest)
    held = approval(conn, launch_id, key)
    if held is not None:
        return Arrival("replay", _refresh_transport(conn, held, transport_key))
    row = _insert(conn, now, launch_id=launch_id, key=key, raw=raw_vendor_key, transport=transport_key,
                  turn=vendor_turn_id, call=vendor_call_id,
                  approval_id=approval_id, kind=kind, route="core" if kind == "host" else "native",
                  decisions=tuple(available_decisions or ()), tool_name=tool_name, digest=digest, nonce=spawn_nonce,
                  tagged=agent_config_tagged(harness, tool_name, tool_input, ctx, protected=protected))
    fold = chat.liveness(conn, launch_id)
    if not fold.enabled and not (fold.latest == "probing" and not fold.latched):
        return Arrival("denied", row, _deny_attempt(conn, now, row, cause="gate not live"))
    if not fold.enabled:
        return Arrival("probing", row)
    return Arrival("routed", row)


def record_quarantine(conn: sqlite3.Connection, now: str, *, launch_id: str, spawn_nonce: str, raw_line: bytes,
                      transport_key: str, vendor_turn_id: str = "-") -> Arrival:
    """The rowless-deny row (R7-7): a Claude ``can_use_tool`` the service cannot key as a host row is written
    ``kind = 'quarantine'``, ``tool_name = ''``, ``targets_agent_config = 1``, its digest the ``sha256`` of the raw
    line's bytes. The same bytes replay to the same row. Its one answer is a deny through the one door; it never reaches
    the core and never becomes a card. ``transport_key`` is the line's own ``request_id`` (the driver's
    ``Event.request_key``): the deny is addressed by it."""
    _launch_harness(conn, launch_id)
    if not isinstance(transport_key, str) or not transport_key:
        raise CoordError("a quarantined request names the transport key its deny is addressed by")
    raw, digest = quarantine_raw_key(spawn_nonce, raw_line)
    key = durable_key(raw, "", digest)
    held = approval(conn, launch_id, key)
    if held is not None:
        return Arrival("replay", _refresh_transport(conn, held, transport_key))
    row = _insert(conn, now, launch_id=launch_id, key=key, raw=raw, transport=transport_key, turn=vendor_turn_id,
                  call=None, approval_id=None,
                  kind="quarantine", route="quarantine", decisions=("deny",), tool_name="", digest=digest, tagged=True,
                  nonce=spawn_nonce)
    return Arrival("quarantined", row)


# --- cancellation: one durable state machine (§3.4 R8-2, R9-3, R10-6) --------------------------------------------
class CancelCall:
    """The per-request wake-up of an in-flight core call. The Event is never the fact: every producer commits its
    durable record first (:func:`cancel_launch_requests`), then sets it."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.cause: str | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self, cause: str) -> None:
        if cause not in CANCEL_CAUSES:
            raise CoordError(f"a cancel cause is one of {', '.join(CANCEL_CAUSES)}")
        if not self._event.is_set():
            self.cause = cause
            self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class CancelRegistry:
    """The in-flight core calls by ``(launch_id, vendor_request_key)``: a call registers before it runs and drops its
    entry when it returns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], CancelCall] = {}

    def register(self, launch_id: str, key: str) -> CancelCall:
        with self._lock:
            return self._calls.setdefault((launch_id, key), CancelCall())

    def drop(self, launch_id: str, key: str) -> None:
        with self._lock:
            self._calls.pop((launch_id, key), None)

    def calls(self, launch_id: str) -> list[CancelCall]:
        with self._lock:
            return [c for (ln, _k), c in self._calls.items() if ln == launch_id]


def cancel_launch_requests(store: "ChatStore", *, launch_id: str, cause: str,
                           commit: Callable[[sqlite3.Connection, str], None], registry: CancelRegistry) -> None:
    """The ONLY way a cancel is raised: ``commit(conn, now)`` in one write transaction (the producer's durable record,
    §3.4's table), THEN set every in-flight call of the launch with ``cause``. A failed commit raises and sets
    nothing, so the Event never runs ahead of the fact. (The design's signature takes ``now``: a ChatStore write reads
    its clock under the lock, so ``commit`` gets it instead.)"""
    if cause not in CANCEL_CAUSES:
        raise CoordError(f"a cancel cause is one of {', '.join(CANCEL_CAUSES)}")
    with store._write() as (conn, now):
        commit(conn, now)
    for call in registry.calls(launch_id):
        call.set(cause)


def mark_driver_lost(conn: sqlite3.Connection, now: str, launch_id: str, cause: str) -> list[str]:
    """``driver_lost`` for every request of the launch still pending, in the caller's transaction (the record behind
    ``stop`` / ``kill`` / ``stdin closed`` / ``service shutdown``). The keys written."""
    keys = pending_requests(conn, launch_id)
    for key in keys:
        append_event(conn, now, launch_id=launch_id, key=key, kind="driver_lost", by="service", cause=cause)
    return keys


def record_cancelled(store: "ChatStore", *, launch_id: str, vendor_request_key: str, cause: str, tag: str) -> None:
    """§3.4 R7-8: a cancelled core call, in one transaction: ``core_result`` (``decision`` the union's ``tag``, cause
    ``cancelled: <cause>``) then the named terminal row: ``driver_lost`` for ``stop`` / ``kill`` / ``stdin closed`` /
    ``service shutdown``, else ``decided_after_turn``. Idempotent: a request already carrying a terminal kind,
    ``answer_attempted`` or ``decision_observed`` gets nothing written (the decision door owns it); one with a
    ``core_result`` (a card) but no terminal gets the terminal only.
    Never a card, never forwarded. (``result=out`` of the design is its ``tag``: this module does not
    import the consent core's union.)"""
    if cause not in CANCEL_CAUSES:
        raise CoordError(f"a cancel cause is one of {', '.join(CANCEL_CAUSES)}")
    with store._write() as (conn, now):
        if approval(conn, launch_id, vendor_request_key) is None:
            raise CoordError(f"no approval request {vendor_request_key[:60]!r} on launch {launch_id}")
        have = _kinds(conn, launch_id, vendor_request_key)
        if have & _CANCEL_SKIP_KINDS:
            return                    # idempotent: a retry, a settled request, or one the decision door owns
        if "core_result" not in have:  # a card (core_result none) withdrawn later still gets its terminal, no 2nd result
            append_event(conn, now, launch_id=launch_id, key=vendor_request_key, kind="core_result", by="core",
                         decision=tag, cause=f"cancelled: {cause}")
        terminal = "driver_lost" if cause in _DRIVER_LOST_CAUSES else "decided_after_turn"
        append_event(conn, now, launch_id=launch_id, key=vendor_request_key, kind=terminal, by="service",
                     cause=f"cancelled: {cause}")


def record_abstain(store: "ChatStore", *, launch_id: str, vendor_request_key: str,
                   precursor: Mapping[str, Any]) -> Literal["card", "decided_after_turn", "no_card"]:
    """The only writer of a native card (R7-8, R8-2, R9-3). One ``BEGIN IMMEDIATE`` that checks FIVE durable facts and
    never the Event; the card is the ``core_result none`` row (``precursor`` gives its ``cause``, e.g.
    ``abstained:hold_expired``). The first failure decides:

    1. still pending (no terminal kind): else nothing more is written and the answer is ``decided_after_turn``;
       a request that already carries ``answer_attempted`` is ``no_card`` (its deny exists);
    2. its turn is live: else ``decided_after_turn`` is written;
    3. the launch's fold is enabled or ``probing``, not ``not_live``: else ``no_card`` and NOTHING terminal (the latch
       committed the deny, or the request was never pending);
    4. the launch is running (its latest launch event is ``active``): else ``decided_after_turn`` is written;
    5. the latest activation is the child the request came from: else ``decided_after_turn`` is written."""
    from levain.coordinator import chat
    cause = _clip(precursor.get("cause", "abstained")) if isinstance(precursor, Mapping) else "abstained"
    with store._write() as (conn, now):
        row = approval(conn, launch_id, vendor_request_key)
        if row is None:
            raise CoordError(f"no approval request {vendor_request_key[:60]!r} on launch {launch_id}")

        def refuse(why: str) -> str:
            append_event(conn, now, launch_id=launch_id, key=vendor_request_key, kind="decided_after_turn",
                         by="service", cause=why)
            return "decided_after_turn"

        if row.kind == "quarantine":
            return "no_card"                      # R7-7: a quarantined line never reaches the core and never becomes a card
        have = _kinds(conn, launch_id, vendor_request_key)
        if "answer_attempted" in have:
            return "no_card"
        if have & set(TERMINAL_KINDS):
            return "decided_after_turn"
        if "core_result" in have:
            return "no_card"                      # idempotent: the card (or the cancel's result) is already written
        if _ended_turn(conn, row):
            return refuse("abstain after the turn ended")
        fold = chat.liveness(conn, launch_id)
        if fold.latched or fold.latest not in ("live", "probing"):
            return "no_card"
        if chat._launch_state(conn, launch_id)[1] != "active":
            return refuse("abstain after the launch stopped")
        if row.spawn_nonce is not None and row.spawn_nonce != _latest_nonce(conn, launch_id):
            return refuse("abstain after the activation was replaced")
        append_event(conn, now, launch_id=launch_id, key=vendor_request_key, kind="core_result", by="core",
                     decision="none", cause=cause)
        return "card"


def latch_not_live(store: "ChatStore", *, launch_id: str, source: str, cause: str) -> list[Attempt]:
    """R9-3: ONE ``BEGIN IMMEDIATE``: the ``not_live`` verdict row, then for EVERY request of the launch that is pending
    and has no ``answer_attempted``, ``decision_observed`` (by service) + ``answer_attempted`` with the refusal its
    harness understands. One :class:`Attempt` per request; the caller writes each one's bytes after the commit, THEN
    sets the cancel. A failed commit raises and writes nothing: no bytes, no cancel. A request that had an attempt or a
    decision before the latch is left to it (the one-call window of R10-7). A request whose deny cannot be written is
    rolled back under its own savepoint and recorded as ``answer_refused``; the verdict row commits regardless.
    A request already carded (``core_result none``) is denied too, by intent: the latch fails closed (pinned by
    ``test_latch_denies_a_carded_request``)."""
    from levain.coordinator import chat
    with store._write() as (conn, now):
        chat.record_check(conn, now, launch_id, "not_live", source, chat._cause_text(cause))
        out = []
        for n, key in enumerate(pending_requests(conn, launch_id, unattempted=True)):
            row = approval(conn, launch_id, key)
            assert row is not None
            point = f"latch_{n}"
            conn.execute(f"SAVEPOINT {point}")
            try:
                out.append(_deny_attempt(conn, now, row, cause=f"not_live: {_clip(cause)}"))
            except (sqlite3.DatabaseError, CoordError) as exc:
                # one request whose deny cannot be written never undoes the verdict or the other denies: its own
                # savepoint rolls back and the failure is a row (answer_refused) on that request
                conn.execute(f"ROLLBACK TO {point}")
                append_event(conn, now, launch_id=launch_id, key=key, kind="answer_refused", by="service",
                             cause=f"not_live: deny could not be built: {_clip(exc)}")
            conn.execute(f"RELEASE {point}")
        return out


def card_headline(cause: str) -> str:
    """§8.2 (R7-9): the headline of the card a ``core_result none`` row stands for. ``abstained:no_decider:<why>`` says
    Levain has no rules loaded and why; ``abstained:hold_expired`` (any other cause) says its rules left the call to you."""
    prefix = "abstained:no_decider:"
    if isinstance(cause, str) and cause.startswith(prefix):
        return f"Levain has no rules loaded for this launch: {cause[len(prefix):] or 'no reason given'}"
    return "Levain's rules left this to you"


# --- derived reads ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AmbiguousStop:
    """R11-7: an answer that may not have reached a LIVE child and is never resent. Derived, never stored."""
    launch_id: str
    request_id: str
    since: str


def ambiguous_stops(conn: sqlite3.Connection, launch_id: str) -> list[AmbiguousStop]:
    """Every request of a live launch (its latest launch event is ``active``) whose LAST approval event is
    ``answer_ambiguous`` and whose turn is live (:func:`turn_live`: not ended, and its child is the latest activation's);
    one per request, empty when the launch ended or a later event
    settled the request. A store that has no approval tables yet (an older file opened read-only) has none."""
    have = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "chat_approvals" not in have or "chat_approval_events" not in have:
        return []
    from levain.coordinator import chat
    if chat._launch_state(conn, launch_id)[1] != "active":
        return []
    out = []
    for key, at in conn.execute(
            "SELECT a.vendor_request_key, e.at FROM chat_approvals a JOIN chat_approval_events e ON e.launch_id = "
            "a.launch_id AND e.vendor_request_key = a.vendor_request_key AND e.seq = (SELECT MAX(x.seq) FROM "
            "chat_approval_events x WHERE x.launch_id = a.launch_id AND x.vendor_request_key = a.vendor_request_key) "
            "WHERE a.launch_id = ? AND e.kind = 'answer_ambiguous' ORDER BY a.gseq", (launch_id,)).fetchall():
        row = approval(conn, launch_id, key)
        if row is not None and turn_live(conn, row) is None:
            out.append(AmbiguousStop(launch_id, key, at))
    return out
