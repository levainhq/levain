"""CH-D1b-4: Claude's mid-turn tripwires (rev 15 §4.4) and the named replies to inbound control requests (§8.1),
against scripted events. Each tripwire writes ``not_live`` with source ``tripwire:<name>`` and the launch latches.

The event shapes are the measured ones, cited per test: RBP = ``1023-21/runs72/rbp/logs`` (2.1.309), RBPP =
``1023-26/runs/rbpp/RESULTS_RBPP.md``, rbpp2 = ``1023-26/runs/rbpp2/logs`` (2.1.310). No inbound ``hook_callback``,
``mcp_message``, ``elicitation`` or ``set_permission_mode`` ever arrived in a run (D:6030-6031): those four request
shapes are the design's (§8.1's table), not measured.

Which EVENT_KINDS value a hook or a cancel line rides is CH-D2's driver's to choose; the tripwires read the Event's
``hook`` / ``cancel_of`` field, so these tests put them on :data:`HOOK_KIND` / :data:`CANCEL_KIND`."""
from __future__ import annotations

import os

import pytest

from levain.coordinator import chat_tripwires as tw
from levain.coordinator import review
from levain.coordinator.chat import ChatService, Verdict, liveness
from levain.coordinator.chat_tripwires import TripwireContext, TripwireWindow
from levain.coordinator.driver import CommandResult, ScriptedSession, SessionMeta, event

from tests.test_coordinator_chat_liveness import (Probe, _world, activate, cs, kinds, ready, rows,  # noqa: F401
                                                  send, store)

HOOK_KIND = "agent_text"
CANCEL_KIND = "approval_resolved"
# tool_result_meta[].permission_decision values, each as a measured log line carries it
HOST_ALLOW = {"decision": "accept", "source": "user_temporary"}                       # rbpp2 a12_curl
HOOK_ALLOW = {"decision": "accept", "source": "hook", "reason_type": "hook"}          # RBP b1_perm_allow, RBPP r5_preallow
RULE_ALLOW = {"decision": "accept", "source": "user_permanent", "reason_type": "rule"}   # RBP a1_allowrule (O7)
SANDBOX_ALLOW = {"decision": "accept", "source": "config", "reason_type": "other"}    # RBPP r9_sbx_true
HOST_DENY = {"decision": "reject", "source": "user_reject"}                           # RBPP r9_sbx_false
MODE_READ = dict(tw.MODE_ALLOWED_SHAPE)                                               # rbpp2 a11_inside
PER_CALL = 2      # RBPP (3) r1_deny: 2 hook_response "PreToolUse:Bash" per Bash call (our gate + the managed guard)


class Session(ScriptedSession):
    """The scripted vendor, plus a mode it reports and a record of the interrupts the service sent it."""

    def __init__(self, *, mode: str | None = "default", **kw) -> None:
        super().__init__(**kw)
        self.mode = mode
        self.interrupts: list = []

    def meta(self) -> SessionMeta:
        return SessionMeta("scripted-session", self.mode, None, "scripted-1", None, (), ())

    def interrupt(self, command):
        self.interrupts.append(command)
        return CommandResult("confirmed", "interrupted")


class Ctx:
    def __init__(self, cwd: str, *, per_call: object = PER_CALL, allowed: tuple[str, ...] = ()) -> None:
        self.cwd, self.per_call, self.allowed, self.asked = cwd, per_call, allowed, []

    def context(self, launch_id: str) -> TripwireContext:
        return TripwireContext(self.cwd, self.expected, self.gate)

    def expected(self, tool: str) -> int:
        if isinstance(self.per_call, BaseException):
            raise self.per_call
        return self.per_call

    def gate(self, tool_use_id, tool_input):
        self.asked.append((tool_use_id, tool_input))
        return tool_use_id in self.allowed


@pytest.fixture
def claude(store):
    return _world(store, "claude")


@pytest.fixture
def root(tmp_path):
    r = tmp_path.resolve() / "launch"
    r.mkdir()
    (r / "inside.txt").write_text("INSIDE-OK-456")
    out = tmp_path.resolve() / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("secret")
    return r


def up(cs, claude, root, *, ctx=None, session=None, **kw):
    ctx = ctx or Ctx(str(root))
    session = session or Session()
    svc, s = ready(cs, claude, session=session, tripwires=ctx.context, **kw)
    return svc, s, ctx


def hooks(s, tool="Bash", n=PER_CALL):
    for _ in range(n):
        s.show(HOOK_KIND, None, hook={"event": "hook_response", "name": f"PreToolUse:{tool}"})


def call(s, cid, tool="Bash", tool_input=None):
    s.show("tool_call", cid, call_id=cid, tool_name=tool, tool_input=tool_input if tool_input is not None else {})


def host(s, rid, cid, tool="Bash"):
    s.show("approval_request", rid, request_key=rid, host=True, call_id=cid, tool_name=tool,
           control_subtype="can_use_tool")


def result(s, cid, decision, tool="Bash"):
    s.show("tool_result", cid, call_id=cid, tool_name=tool, decision_source=decision)


def trips(cs, launch):
    return [s for k, s in kinds(cs, launch) if s.startswith("tripwire:")]


def latched(cs, launch):
    return cs._read(lambda conn: liveness(conn, launch)).latched


# --- deficit (§4.4) ------------------------------------------------------------------------------------------------
def test_tripwire_deficit_latches(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s, n=PER_CALL - 1)                          # one of the two PreToolUse handlers did not run
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]
    assert latched(cs, claude["launch"])


def test_full_hook_count_with_host_answer_does_not_trip(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    call(s, "toolu_2")
    hooks(s)
    host(s, "req-2", "toolu_2")
    result(s, "toolu_2", HOST_DENY)                   # a deny is never refused
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []
    assert cs._read(lambda conn: liveness(conn, claude["launch"])).enabled


def test_deficit_counts_per_tool_over_parallel_calls(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    call(s, "toolu_2")
    hooks(s, n=2 * PER_CALL - 1)                      # two Bash calls, one handler run missing (O8 limit 1)
    host(s, "req-1", "toolu_1")
    host(s, "req-2", "toolu_2")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []          # 3 seen >= 2 x 1 result so far
    result(s, "toolu_2", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


@pytest.mark.parametrize("per_call", [0, "2", None, ValueError("no pin")])
def test_unknown_expected_count_trips_deficit(cs, claude, root, per_call):
    svc, s, _ = up(cs, claude, root, ctx=Ctx(str(root), per_call=per_call))
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


def test_no_tripwire_context_trips_at_the_first_tool_result(cs, claude):
    svc, s = ready(cs, claude, session=Session(), tripwires=lambda _l: None)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


def test_a_session_watched_from_mid_call_trips(cs, claude, root):
    """A new session (a reattach) starts with no counts and no joins: a result whose hooks it never saw trips."""
    svc, s, ctx = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    svc.read_next(claude["launch"])
    svc.detach(claude["launch"])
    s2 = Session()
    s2._n = s._n                                      # the vendor's order goes on: its next line is not history
    svc.attach(claude["launch"], s2)
    result(s2, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


# --- unexpected_rule, unexpected_hook, mode_changed ----------------------------------------------------------------
def test_tripwire_unexpected_rule_latches(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", RULE_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_rule"]


def test_tripwire_unexpected_hook_latches(cs, claude, root):
    # RBP b1_perm_allow: can_use_tool, then {"type":"control_cancel_request","request_id":...}, then the tool_result
    # with source hook (a PermissionRequest hook decided first, O4)
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    s.show(CANCEL_KIND, "cancel-req-1", cancel_of="req-1")
    result(s, "toolu_1", HOOK_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_hook"]


def test_unexpected_hook_latches_for_a_hook_deny_too(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    s.show(CANCEL_KIND, "cancel-req-1", cancel_of="req-1")
    result(s, "toolu_1", {"decision": "reject", "source": "hook", "reason_type": "hook"})   # RBP b2_perm_deny
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_hook"]


@pytest.mark.parametrize("mode", ["bypassPermissions", "acceptEdits", "plan", ""])
def test_tripwire_mode_change_latches(cs, claude, root, mode):
    # O7 (RBP a0_bypass_mode): init reads permissionMode bypassPermissions without --permission-mode default
    session = Session()
    svc, s, _ = up(cs, claude, root, session=session)
    assert trips(cs, claude["launch"]) == []
    session.mode = mode
    s.show("agent_text", None, body="hello")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:mode_changed"]


def test_mode_default_or_unreported_does_not_trip(cs, claude, root):
    session = Session(mode=None)
    svc, s, _ = up(cs, claude, root, session=session)
    s.show("agent_text", None, body="hello")
    svc.read_next(claude["launch"])
    session.mode = "default"
    s.show("agent_text", None, body="again")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_codex_launch_has_no_claude_tripwires(cs, store, root):
    codex = _world(store, "codex")
    session = Session(mode="bypassPermissions")
    svc, s = ready(cs, codex, session=session, tripwires=Ctx(str(root)).context)
    call(s, "c1")
    result(s, "c1", RULE_ALLOW)
    svc.read_next(codex["launch"])
    assert trips(cs, codex["launch"]) == []


# --- accepted_without_host (H2, H10, arm 9) ------------------------------------------------------------------------
def test_tripwire_accepted_without_host_latches_for_hook(cs, claude, root):
    # RBPP r5_preallow: a PreToolUse allow suppresses the host request: {accept, hook, hook}, no can_use_tool
    svc, s, ctx = up(cs, claude, root)
    call(s, "toolu_1", tool_input={"command": "ls"})
    hooks(s)
    result(s, "toolu_1", HOOK_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]
    assert ctx.asked == [("toolu_1", {"command": "ls"})]       # exemption 1 was asked, with the call's own input


def test_tripwire_accepted_without_host_latches_for_config(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


@pytest.mark.parametrize("decision", [
    {"decision": "accept", "source": "some_new_source", "reason_type": "other"},
    {"decision": "accept"},
    {"decision": "allow_once", "source": "user_temporary"},
    None,
])
def test_tripwire_accepted_without_host_latches_for_unknown_source(cs, claude, root, decision):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", decision)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


def test_gate_allowed_call_does_not_trip(cs, claude, root):
    """Exemption 1 against an injected record (the real gate row join is D1c-E's)."""
    svc, s, ctx = up(cs, claude, root, ctx=Ctx(str(root), allowed=("toolu_1",)))
    call(s, "toolu_1", tool_input={"command": "ls"})
    hooks(s)
    result(s, "toolu_1", HOOK_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


@pytest.mark.parametrize("gate", [
    lambda cid, _i: cid == "toolu_other",              # the record is another call's
    lambda _c, _i: 1,                                  # truthy, not True
    lambda _c, _i: (_ for _ in ()).throw(OSError("queue.db locked")),
])
def test_hook_source_without_gate_record_trips(cs, claude, root, gate):
    ctx = Ctx(str(root))
    ctx.gate = gate
    svc, s, _ = up(cs, claude, root, ctx=ctx)
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", HOOK_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


def test_gate_record_never_exempts_a_config_source(cs, claude, root):
    svc, s, _ = up(cs, claude, root, ctx=Ctx(str(root), allowed=("toolu_1",)))
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


# --- the Read exemption (R7-4) -------------------------------------------------------------------------------------
def _read(cs, claude, root, path, *, tool="Read", decision=None):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_r", tool=tool, tool_input={"file_path": path})
    hooks(s, tool=tool)
    result(s, "toolu_r", decision or MODE_READ, tool=tool)
    svc.read_next(claude["launch"])
    return trips(cs, claude["launch"])


def test_read_inside_with_measured_shape_does_not_trip(cs, claude, root):
    assert _read(cs, claude, root, str(root / "inside.txt")) == []


def test_read_outside_with_same_shape_trips(cs, claude, root):
    assert _read(cs, claude, root, str(root.parent / "outside" / "secret.txt")) == ["tripwire:accepted_without_host"]


def test_read_missing_path_with_same_shape_trips(cs, claude, root):
    assert _read(cs, claude, root, str(root / "gone.txt")) == ["tripwire:accepted_without_host"]


def test_read_symlink_escape_with_same_shape_trips(cs, claude, root):
    os.symlink(root.parent / "outside" / "secret.txt", root / "link.txt")
    assert _read(cs, claude, root, str(root / "link.txt")) == ["tripwire:accepted_without_host"]


def test_read_sibling_prefix_dir_trips(cs, claude, root):
    sib = root.parent / (root.name + "2")
    sib.mkdir()
    (sib / "x.txt").write_text("x")
    assert _read(cs, claude, root, str(sib / "x.txt")) == ["tripwire:accepted_without_host"]


def test_read_relative_path_trips(cs, claude, root):
    assert _read(cs, claude, root, "inside.txt") == ["tripwire:accepted_without_host"]


def test_non_read_tool_with_mode_shape_trips(cs, claude, root):
    assert _read(cs, claude, root, str(root / "inside.txt"), tool="Glob") == ["tripwire:accepted_without_host"]


def test_read_inside_with_other_shape_trips(cs, claude, root):
    assert _read(cs, claude, root, str(root / "inside.txt"), decision=SANDBOX_ALLOW) == [
        "tripwire:accepted_without_host"]


# --- the counting window (R9, Q-R5-4) ------------------------------------------------------------------------------
def test_deficit_window_resets_at_result_line(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s, n=3 * PER_CALL)                          # more handler runs than this turn's one call needed
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    s.show("turn_ended", "t-1", turn_id="t-1")        # the result line: a boundary, no call open
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []
    call(s, "toolu_2")                                # no hook ran for this one; the last turn's surplus cannot cover it
    host(s, "req-2", "toolu_2")
    result(s, "toolu_2", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


def test_send_with_open_call_does_not_reset_counts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    svc.read_next(claude["launch"])
    send(svc, cs, claude)                             # a queued send lands mid-turn with toolu_1 open: a boundary
    assert ("probing", "send") in kinds(cs, claude["launch"])
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_send_with_no_open_call_resets_counts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s, n=2 * PER_CALL)
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    send(svc, cs, claude)                             # no call open: the counts reset
    call(s, "toolu_2")
    host(s, "req-2", "toolu_2")
    result(s, "toolu_2", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


def test_a_trip_before_a_result_line_leaves_that_boundary_its_verdict(cs, claude, root):
    svc, s, _ = up(cs, claude, root, probe=Probe(Verdict(True, "ok")))
    call(s, "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)                  # deficit, caught before the result line
    s.show("turn_ended", "t-1", turn_id="t-1")
    svc.read_next(claude["launch"])
    assert kinds(cs, claude["launch"])[-3:] == [("not_live", "tripwire:deficit"), ("probing", "result"),
                                               ("live", "result")]
    assert svc.composer(claude["launch"], "cockpit:tab-a").reenable     # your re-enable is offered


def test_window_resets_only_when_the_batch_commits():
    w = TripwireWindow(None)
    w.hooks["PreToolUse:Bash"] = 2
    c = w.copy()
    c.boundary()
    assert w.hooks == {"PreToolUse:Bash": 2} and c.hooks == {}


# --- inbound control requests (§8.1) -------------------------------------------------------------------------------
def _replies(s):
    return [(w["response"]["request_id"], w["response"]["error"]) for w in s.written
            if w.get("type") == "control_response" and w["response"].get("subtype") == "error"]


@pytest.mark.parametrize("subtype,text,latches", [
    ("hook_callback", "levain registers no SDK hooks", True),
    ("mcp_message", "levain hosts no SDK MCP server", True),
    ("elicitation", "levain answers no elicitation in chat", False),
    ("set_permission_mode", "levain fixes this launch's mode", True),
    ("rewind_files", "levain does not answer rewind_files", True),
])
def test_each_inbound_subtype_gets_its_named_reply(cs, claude, root, subtype, text, latches):
    svc, s, _ = up(cs, claude, root)
    s.inbound("ctl-1", subtype, turn_id="t-1")
    svc.read_next(claude["launch"])
    assert _replies(s) == [("ctl-1", text)]
    assert s._inbound == {}                           # answered: the request is forgotten
    assert trips(cs, claude["launch"]) == (["tripwire:unexpected_control"] if latches else [])
    assert s.interrupts == []


def test_unknown_control_subtype_errors_and_latches(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.inbound("ctl-9", "some_future_subtype")
    svc.read_next(claude["launch"])
    assert _replies(s) == [("ctl-9", "levain does not answer some_future_subtype")]
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_control"]
    assert latched(cs, claude["launch"])


def test_hook_callback_latches(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.inbound("ctl-2", "hook_callback")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_control"]
    assert latched(cs, claude["launch"])


def test_unparseable_control_line_latches_and_interrupts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.show("error", "unparseable:1", turn_id="t-1", body="{not json")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"]
    assert [c.turn_id for c in s.interrupts] == ["t-1"]
    assert _replies(s) == []


def test_control_request_with_no_request_id_latches_and_interrupts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.show("error", "control:", turn_id="t-1", control_subtype="hook_callback")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"]
    assert len(s.interrupts) == 1 and _replies(s) == []


def test_a_reply_that_cannot_be_written_latches_and_interrupts(cs, claude, root):
    class Broken(Session):
        def reject_control(self, request_id, text):
            raise OSError("broken pipe")

    svc, s, _ = up(cs, claude, root, session=Broken())
    s.inbound("ctl-3", "elicitation", turn_id="t-1")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"]
    assert len(s.interrupts) == 1


def test_a_replayed_control_event_gets_no_second_reply(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.inbound("ctl-4", "elicitation")
    svc.read_next(claude["launch"])
    svc.ingest(claude["launch"], s.read(None))        # the history again: nothing new, no reply
    assert _replies(s) == [("ctl-4", "levain answers no elicitation in chat")]
    assert trips(cs, claude["launch"]) == [] and s.interrupts == []    # no second reply was even tried


# --- the latch row: one per tripwire while latched; the §4.3 seat review item ----------------------------------------
def test_a_tripwire_writes_once_while_latched(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    for n in (1, 2):
        call(s, f"toolu_{n}")
        result(s, f"toolu_{n}", HOST_ALLOW)
        svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


class Filer:
    """The launcher's seat-item filer: the review queue's own writer, once per key. ``fail`` raises that many times."""

    def __init__(self, store, fail: int = 0) -> None:
        self.store, self.fail, self.calls = store, fail, 0

    def __call__(self, key, body):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise OSError("dispatch.db is locked")
        return review.file_item(self.store, "seat", body, dedupe_key=key)


def _items(cs):
    with cs._write() as (conn, _now):
        return conn.execute("SELECT feed, dedupe_key, body FROM review_items ORDER BY rowid").fetchall()


def test_not_live_after_live_files_a_seat_item(cs, store, claude, root):
    svc, s, _ = up(cs, claude, root, seat_item=Filer(store))
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    items = _items(cs)
    assert [(f, k.split(":")[0]) for f, k, _b in items] == [("seat", "chat-not-live")]
    assert "tripwire:accepted_without_host" in items[0][2] and claude["launch"] in items[0][2]
    assert "'config'" in items[0][2]                  # names the source


def test_a_turn_probe_not_live_after_live_files_a_seat_item(cs, store, claude, root):
    svc, s, _ = up(cs, claude, root, probe=Probe(Verdict(False, "hook gone")), seat_item=Filer(store))
    s.show("turn_started", "t-2", turn_id="t-2")
    svc.read_next(claude["launch"])
    assert kinds(cs, claude["launch"])[-1] == ("not_live", "turn_start")
    assert [f for f, _k, _b in _items(cs)] == ["seat"]


def test_not_live_with_no_prior_live_files_none(cs, store, claude):
    svc = ChatService(cs, probe=Probe(), sleep=lambda _s: None, seat_item=Filer(store))
    svc.activate(claude["launch"], "not_live", "canary failed", spawn_nonce="0123456789abcdef0123456789abcdef")
    assert latched(cs, claude["launch"]) and _items(cs) == []


def test_a_second_not_live_while_latched_files_no_second_item(cs, store, claude, root):
    svc, s, _ = up(cs, claude, root, seat_item=Filer(store))
    call(s, "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)                  # deficit
    s.inbound("ctl-1", "hook_callback")               # unexpected_control, same latch
    svc.read_next(claude["launch"])
    assert sorted(trips(cs, claude["launch"])) == ["tripwire:deficit", "tripwire:unexpected_control"]
    assert len(_items(cs)) == 1


def test_a_reenabled_launch_that_latches_again_files_again(cs, store, claude, root):
    filer = Filer(store)
    svc, s, _ = up(cs, claude, root, probe=Probe(Verdict(True, "ok")), seat_item=filer)
    call(s, "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)                  # deficit: latched
    s.show("turn_ended", "t-1", turn_id="t-1")        # a result line: probed live again
    svc.read_next(claude["launch"])
    svc.reenable(claude["launch"], "cockpit:tab-a")
    call(s, "toolu_2")
    result(s, "toolu_2", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit", "tripwire:deficit"]
    assert len(_items(cs)) == 2


def test_a_failing_filer_never_costs_the_latch_and_the_next_sweep_files_it(cs, store, claude, root):
    filer = Filer(store, fail=1)
    svc, s, _ = up(cs, claude, root, seat_item=filer)
    call(s, "toolu_1")
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    assert latched(cs, claude["launch"]) and _items(cs) == []
    svc.read_next(claude["launch"])                   # the next pump step: owed, not filed, filed now
    assert len(_items(cs)) == 1
    calls = filer.calls
    svc.read_next(claude["launch"])
    assert filer.calls == calls                       # filed once by this process: not asked again


def test_a_restarted_service_files_what_the_crash_left_owed(cs, store, claude, root):
    svc, s, _ = up(cs, claude, root)                  # no filer: the latch commits, its item is owed
    call(s, "toolu_1")
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    assert latched(cs, claude["launch"]) and _items(cs) == []
    svc2 = ChatService(cs, probe=Probe(), sleep=lambda _s: None, seat_item=Filer(store))
    svc2.attach(claude["launch"], Session())
    svc2.read_next(claude["launch"])
    assert len(_items(cs)) == 1


# --- the module's pure parts -----------------------------------------------------------------------------------------
def test_mode_allowed_shape_is_the_rbpp2_a11_inside_shape():
    assert tw.MODE_ALLOWED_SHAPE == {"decision": "accept", "source": "config", "reason_type": "mode"}


def test_tripwire_sources_fit_the_released_check():
    import re
    for name in tw.TRIPWIRES:
        assert re.fullmatch(r"[a-z_]+", name)


def test_observe_reads_fields_not_kinds():
    w = TripwireWindow(None)
    for kind in ("agent_text", "error", "approval_resolved"):
        w.observe(event(kind, f"h-{kind}", "1", hook={"event": "hook_response", "name": "PreToolUse:Bash"}))
    assert w.hooks == {"PreToolUse:Bash": 3}
