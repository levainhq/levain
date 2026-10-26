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

from tests.test_coordinator_chat_liveness import Probe, _world, cs, kinds, ready, send, store  # noqa: F401
from tests.test_coordinator_chat_liveness_r1 import _crossing

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


# --- L3 r1 (T1-T11): every state the window cannot account for latches ---------------------------------------------
def test_t1_a_crash_after_the_commit_still_leaves_the_control_answered(cs, claude, root, monkeypatch):
    svc, s, ctx = up(cs, claude, root)
    s.inbound("ctl-1", "elicitation", turn_id="t-1")

    def crash(launch_id):
        raise KeyboardInterrupt("the process died after the batch committed")

    monkeypatch.setattr(svc, "_file_latches", crash)
    with pytest.raises(KeyboardInterrupt):
        svc.read_next(claude["launch"])
    svc2 = ChatService(cs, probe=Probe(), sleep=lambda _s: None, tripwires=ctx.context)
    svc2.attach(claude["launch"], s)                  # the restarted service, the same CLI still waiting
    svc2.read_next(claude["launch"])
    assert _replies(s) == [("ctl-1", "levain answers no elicitation in chat")]


def test_t1_a_crash_between_the_reply_and_the_commit_re_finds_it_and_latches(cs, claude, root):
    crashed = []

    def fault(point):
        if point == "controls_answered" and not crashed:
            crashed.append(point)
            raise KeyboardInterrupt("the process died after the reply, before the commit")

    svc, s, ctx = up(cs, claude, root, fault=fault)
    s.inbound("ctl-1", "elicitation", turn_id="t-1")
    with pytest.raises(KeyboardInterrupt):
        svc.read_next(claude["launch"])
    assert crashed and _replies(s) == [("ctl-1", "levain answers no elicitation in chat")]
    svc2 = ChatService(cs, probe=Probe(), sleep=lambda _s: None, tripwires=ctx.context)
    svc2.attach(claude["launch"], s)
    svc2.read_next(claude["launch"])                  # the cursor never moved: the control is read again
    assert _replies(s) == [("ctl-1", "levain answers no elicitation in chat")]    # the driver refused a second
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"] and latched(cs, claude["launch"])


def test_t1_a_failed_transaction_does_not_answer_twice_in_one_process(cs, claude, root, monkeypatch):
    from levain.coordinator import chat as chat_mod
    svc, s, _ = up(cs, claude, root)
    s.inbound("ctl-1", "elicitation", turn_id="t-1")
    real, seen = chat_mod._fingerprint, []

    def moved(conn, launch_id):                       # another writer moved the record once: the batch is refused
        got = real(conn, launch_id)
        seen.append(got)
        return (-1, -1, -1) if len(seen) == 2 else got

    monkeypatch.setattr(chat_mod, "_fingerprint", moved)
    with pytest.raises(chat_mod.CoordError):
        svc.read_next(claude["launch"])
    svc.read_next(claude["launch"])
    assert _replies(s) == [("ctl-1", "levain answers no elicitation in chat")]
    assert trips(cs, claude["launch"]) == []


class Gappy(Session):
    """Reports its next read as a gap (lines the session could not read)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.gap_next = False

    def read(self, cursor):
        got = super().read(cursor)
        if self.gap_next:
            self.gap_next = False
            got = got._replace(gap=True)
        return got


def test_t2_a_gap_with_a_call_open_latches(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Gappy())
    call(s, "toolu_A")
    hooks(s)                                          # A's two hooks; its result is lost in the gap below
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []
    s.gap_next = True
    call(s, "toolu_B")
    host(s, "req-B", "toolu_B")
    result(s, "toolu_B", HOST_ALLOW)                  # B ran no hook: A's would have covered it
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"] and latched(cs, claude["launch"])


def test_t2_a_gap_resets_the_counts(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Gappy())
    hooks(s)                                          # surplus hooks with no call open
    svc.read_next(claude["launch"])
    s.gap_next = True
    s.show("agent_text", None, body="x")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []
    call(s, "toolu_1")
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)                  # no hook of its own: the pre-gap surplus does not count
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]


def test_t3_a_transient_mode_inside_one_batch_trips(cs, claude, root):
    svc, s, _ = up(cs, claude, root)                  # meta reads default before and after the batch
    s.show("error", "mode:1", body="bypassPermissions")
    s.show("error", "mode:2", body="default")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:mode_changed"] and latched(cs, claude["launch"])


def test_t3_a_mode_report_of_default_does_not_trip(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.show("error", "mode:1", body="default")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_t4_no_injected_callback_runs_inside_the_write_transaction(cs, claude, root):
    import sqlite3

    def outside() -> None:
        conn = cs._connect()
        try:
            conn.execute("PRAGMA busy_timeout = 0")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise AssertionError(f"called inside a write transaction: {exc}") from None
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    class Checked(Ctx):
        def expected(self, tool):
            outside()
            return super().expected(tool)

        def gate(self, tool_use_id, tool_input):
            outside()
            return super().gate(tool_use_id, tool_input)

    ctx = Checked(str(root), allowed=("toolu_1",))
    svc, s, _ = up(cs, claude, root, ctx=ctx)
    call(s, "toolu_1")
    hooks(s)
    result(s, "toolu_1", HOOK_ALLOW)
    svc.read_next(claude["launch"])
    assert ctx.asked and trips(cs, claude["launch"]) == []


def test_t5_a_cancelled_host_request_does_not_exempt_a_later_accept(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    s.show(CANCEL_KIND, "cancel-1", cancel_of="req-1")
    result(s, "toolu_1", SANDBOX_ALLOW)               # accepted after the host's request was withdrawn
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


def test_t5_a_cancelled_host_request_then_a_reject_does_not_trip(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    host(s, "req-1", "toolu_1")
    s.show(CANCEL_KIND, "cancel-1", cancel_of="req-1")
    result(s, "toolu_1", HOST_DENY)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_t6_a_result_line_with_a_call_open_latches_and_frees_the_counts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_orphan")
    hooks(s)
    s.show("turn_ended", "t-1", turn_id="t-1")        # a result line with toolu_orphan still open
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:deficit"]
    assert svc._windows[claude["launch"]].calls == {} and svc._windows[claude["launch"]].hooks == {}


def test_t6_a_send_with_a_call_open_still_carries_the_counts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    call(s, "toolu_1")
    hooks(s)
    svc.read_next(claude["launch"])
    send(svc, cs, claude)
    host(s, "req-1", "toolu_1")
    result(s, "toolu_1", HOST_ALLOW)
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_t8_a_launch_cwd_reached_through_a_symlink_keeps_the_read_exemption(cs, claude, root, tmp_path):
    link = tmp_path / "via-link"
    link.symlink_to(root)
    svc, s, _ = up(cs, claude, root, ctx=Ctx(str(link)))
    call(s, "toolu_1", tool="Read", tool_input={"file_path": str(root / "inside.txt")})
    hooks(s, tool="Read")
    result(s, "toolu_1", MODE_READ, tool="Read")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == []


def test_t9_can_use_tool_on_the_error_channel_latches_and_interrupts(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s.show("error", "control:ctl-x", turn_id="t-1", control_subtype="can_use_tool")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:unexpected_control"]
    assert [c.turn_id for c in s.interrupts] == ["t-1"] and _replies(s) == []


def test_t10_a_mode_trip_is_kept_when_the_batch_crosses_a_boundary(cs, claude, root):
    from levain.coordinator.chat import CoordError
    session = Session()
    svc, s, _ = up(cs, claude, root, session=session)
    session.mode = "bypassPermissions"
    with pytest.raises(CoordError, match="crossed a boundary"):
        svc.ingest(claude["launch"], _crossing(100))
    assert trips(cs, claude["launch"]) == ["tripwire:mode_changed"]


def test_t10_a_mode_report_inside_a_crossing_batch_is_kept(cs, claude, root):
    from levain.coordinator.chat import CoordError
    svc, s, _ = up(cs, claude, root)
    b = _crossing(100)
    mode = event("error", "mode:9", "00000100", body="bypassPermissions")
    with pytest.raises(CoordError, match="crossed a boundary"):
        svc.ingest(claude["launch"], b._replace(events=(mode,) + b.events))
    assert trips(cs, claude["launch"]) == ["tripwire:mode_changed"]


def test_t11_the_filed_record_is_per_launch_and_dropped_at_detach(cs, store, claude, root):
    svc, s, _ = up(cs, claude, root, seat_item=Filer(store))
    call(s, "toolu_1")
    result(s, "toolu_1", SANDBOX_ALLOW)
    svc.read_next(claude["launch"])
    assert len(_items(cs)) == 1 and svc._filed.get(claude["launch"])
    svc.detach(claude["launch"])
    assert claude["launch"] not in svc._filed


def test_t1_a_failed_reply_in_a_refused_transaction_still_latches_on_the_re_read(cs, claude, root, monkeypatch):
    from levain.coordinator import chat as chat_mod

    class Broken(Session):
        def reject_control(self, request_id, text):
            raise OSError("broken pipe")

    svc, s, _ = up(cs, claude, root, session=Broken())
    s.inbound("ctl-1", "elicitation", turn_id="t-1")
    real, seen = chat_mod._fingerprint, []

    def moved(conn, launch_id):
        got = real(conn, launch_id)
        seen.append(got)
        return (-1, -1, -1) if len(seen) == 2 else got

    monkeypatch.setattr(chat_mod, "_fingerprint", moved)
    with pytest.raises(chat_mod.CoordError):
        svc.read_next(claude["launch"])
    svc.read_next(claude["launch"])                   # the reply fails again, and this time the latch commits
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"]


# --- L3 verify r2 (1026-3 d1b4g): R1 a crossing batch's controls, R2 a NUL Read path, R3 _filed with no session -------
def test_r1_a_crossing_batch_still_interrupts_its_can_use_tool_on_the_error_channel(cs, claude, root):
    from levain.coordinator.chat import CoordError
    svc, s, _ = up(cs, claude, root)
    b = _crossing(100)
    ctl = event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool")
    with pytest.raises(CoordError, match="crossed a boundary"):
        svc.ingest(claude["launch"], b._replace(events=(ctl,) + b.events))
    assert [c.turn_id for c in s.interrupts] == ["t-1"]
    assert "tripwire:unexpected_control" in trips(cs, claude["launch"])
    assert latched(cs, claude["launch"])


def test_r1_a_crossing_batch_still_answers_its_elicitation_once(cs, claude, root):
    from levain.coordinator.chat import CoordError
    svc, s, _ = up(cs, claude, root)
    s._inbound["ctl-e"] = "elicitation"               # the vendor's pending request, its line in the batch below
    b = _crossing(100)
    ctl = event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation")
    crossing = b._replace(events=(ctl,) + b.events)
    for _ in (1, 2):                                  # the refused batch is read again: no second reply
        with pytest.raises(CoordError, match="crossed a boundary"):
            svc.ingest(claude["launch"], crossing)
    assert _replies(s) == [("ctl-e", "levain answers no elicitation in chat")]
    assert s.interrupts == []


def test_r2_a_read_path_with_an_embedded_nul_trips_and_does_not_raise(cs, claude, root):
    assert _read(cs, claude, root, str(root / "inside.txt") + "\x00x") == ["tripwire:accepted_without_host"]


def test_r2_a_launch_root_with_an_embedded_nul_is_no_root(cs, claude, root):
    svc, s, _ = up(cs, claude, root, ctx=Ctx(str(root) + "\x00x"))
    call(s, "toolu_1", tool="Read", tool_input={"file_path": str(root / "inside.txt")})
    hooks(s, tool="Read")
    result(s, "toolu_1", MODE_READ, tool="Read")
    svc.read_next(claude["launch"])
    assert trips(cs, claude["launch"]) == ["tripwire:accepted_without_host"]


def test_r3_a_never_attached_launch_keeps_no_filed_record(cs, store, claude):
    from tests.test_coordinator_chat_liveness import NONCE, activate, service
    svc = service(cs, seat_item=Filer(store))
    activate(svc, claude)                             # live, then the launcher's failed check: never attached
    svc.activate(claude["launch"], "not_live", "the canary failed", spawn_nonce=NONCE)
    assert len(_items(cs)) == 1                       # the item is filed
    svc.detach(claude["launch"])
    assert claude["launch"] not in svc._filed


# --- L3 verify r3 (1026-3 d1b4h): one handled-control record, keyed by the control event's identity -----------------
class Shaky(Session):
    """Answers each interrupt with the next scripted outcome (the last one repeats)."""

    def __init__(self, *outcomes: str, **kw) -> None:
        super().__init__(**kw)
        self.outcomes = list(outcomes)

    def interrupt(self, command):
        self.interrupts.append(command)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return CommandResult(outcome, outcome)


def _cross_with(ctl):
    b = _crossing(100)
    return b._replace(events=(ctl,) + b.events)


def _refused(svc, launch, batch):
    from levain.coordinator.chat import CoordError
    with pytest.raises(CoordError, match="crossed a boundary"):
        svc.ingest(launch, batch)


def test_h1_a_refused_crossing_batch_re_read_interrupts_its_can_use_tool_once(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    for _ in (1, 2, 3):
        _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == ["t-1"]
    assert "tripwire:unexpected_control" in trips(cs, claude["launch"])


@pytest.mark.parametrize("outcome", ["ambiguous", "rejected"])
def test_h1_an_interrupt_that_was_not_confirmed_is_sent_again(cs, claude, root, outcome):
    svc, s, _ = up(cs, claude, root, session=Shaky(outcome, "confirmed"))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    for _ in (1, 2, 3):
        _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == ["t-1", "t-1"]


def test_h1_an_interrupt_answered_no_such_request_is_not_sent_again(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Shaky("no_such_request"))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    for _ in (1, 2):
        _refused(svc, claude["launch"], crossing)
    assert len(s.interrupts) == 1


def test_h2_a_new_control_event_reusing_a_crossing_batchs_request_id_is_answered(cs, claude, root):
    from tests.test_coordinator_chat_liveness import activate
    svc, s, _ = up(cs, claude, root)
    s._inbound["ctl-e"] = "elicitation"
    _refused(svc, claude["launch"], _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1",
                                                      control_subtype="elicitation")))
    activate(svc, claude)                             # the launch is activated again, its session still attached
    s.inbound("ctl-e", "elicitation", turn_id="t-2")  # a distinct later request that reuses the id
    svc.read_next(claude["launch"])
    assert _replies(s) == [("ctl-e", "levain answers no elicitation in chat")] * 2


@pytest.mark.parametrize("first", ["confirmed", "ambiguous"])
def test_h3_a_stale_crossing_batch_never_interrupts_the_reactivated_launchs_turn(cs, claude, root, first):
    from tests.test_coordinator_chat_liveness import activate
    svc, s, _ = up(cs, claude, root, session=Shaky(first, "confirmed"))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id=None, control_subtype="can_use_tool"))
    _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == [None]
    activate(svc, claude)                             # a new activation: a turn of it may be running now
    _refused(svc, claude["launch"], crossing)         # the stale batch read again
    assert [c.turn_id for c in s.interrupts] == [None]
    assert latched(cs, claude["launch"])


def test_h3_a_stale_control_naming_its_own_turn_is_still_interrupted(cs, claude, root):
    from tests.test_coordinator_chat_liveness import activate
    svc, s, _ = up(cs, claude, root, session=Shaky("ambiguous", "confirmed"))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    _refused(svc, claude["launch"], crossing)
    activate(svc, claude)
    _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == ["t-1", "t-1"]


def test_h4_the_record_is_dropped_at_detach_and_at_attach(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    _refused(svc, claude["launch"], crossing)
    svc.detach(claude["launch"])
    assert claude["launch"] not in svc._handled
    svc.attach(claude["launch"], s)
    _refused(svc, claude["launch"], crossing)         # a new session's record starts empty: it is interrupted
    assert [c.turn_id for c in s.interrupts] == ["t-1", "t-1"]


class Gone(Session):
    """Answers every reply no_such_request."""

    def reject_control(self, request_id, text):
        self.asked = getattr(self, "asked", 0) + 1
        return CommandResult("no_such_request", "no such request")


def test_i1_a_first_reply_answered_no_such_request_latches_and_interrupts(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Gone())
    s.inbound("ctl-1", "elicitation", turn_id="t-1")
    svc.read_next(claude["launch"])
    assert s.asked == 1 and len(s.interrupts) == 1
    assert trips(cs, claude["launch"]) == ["tripwire:unanswerable_control"] and latched(cs, claude["launch"])


def test_i1_a_first_reply_answered_no_such_request_in_a_crossing_batch_latches_and_interrupts(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Gone())
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    _refused(svc, claude["launch"], crossing)
    assert s.asked == 1 and len(s.interrupts) == 1
    assert "tripwire:unanswerable_control" in trips(cs, claude["launch"]) and latched(cs, claude["launch"])


def test_i1_a_confirmed_reply_is_not_sent_again_on_a_re_read(cs, claude, root):
    svc, s, _ = up(cs, claude, root)
    s._inbound["ctl-e"] = "elicitation"
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    for _ in (1, 2, 3):
        _refused(svc, claude["launch"], crossing)
    assert _replies(s) == [("ctl-e", "levain answers no elicitation in chat")] and s.interrupts == []
    assert "tripwire:unanswerable_control" not in trips(cs, claude["launch"])


def test_h5_a_failed_reply_in_a_crossing_batch_is_tried_again(cs, claude, root):
    class Flaky(Session):
        def reject_control(self, request_id, text):
            self.asked = getattr(self, "asked", 0) + 1
            if self.asked == 1:
                raise OSError("broken pipe")
            return super().reject_control(request_id, text)

    svc, s, _ = up(cs, claude, root, session=Flaky())
    s._inbound["ctl-e"] = "elicitation"
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    for _ in (1, 2, 3):
        _refused(svc, claude["launch"], crossing)
    assert s.asked == 2 and _replies(s) == [("ctl-e", "levain answers no elicitation in chat")]
    assert len(s.interrupts) == 1                     # the failed try's interrupt; the confirmed retry sends none
    assert "tripwire:unanswerable_control" in trips(cs, claude["launch"])


def test_h5_a_stale_failed_reply_is_tried_again_but_interrupts_no_new_turn(cs, claude, root):
    from tests.test_coordinator_chat_liveness import activate

    class Broken(Session):
        def reject_control(self, request_id, text):
            raise OSError("broken pipe")

    svc, s, _ = up(cs, claude, root, session=Broken())
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == [None]
    activate(svc, claude)
    _refused(svc, claude["launch"], crossing)
    assert [c.turn_id for c in s.interrupts] == [None]


def test_i2_a_re_enable_makes_an_open_interrupt_naming_no_turn_stale(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Shaky("ambiguous", "confirmed"), probe=Probe(Verdict(True, "ok")))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id=None, control_subtype="can_use_tool"))
    _refused(svc, claude["launch"], crossing)         # its interrupt is ambiguous: the event stays open
    assert [c.turn_id for c in s.interrupts] == [None] and latched(cs, claude["launch"])
    s.show("turn_ended", "t-9", turn_id="t-9")        # a result line: probed live again, with no activation
    svc.read_next(claude["launch"])
    svc.reenable(claude["launch"], "cockpit:tab-a")
    assert not latched(cs, claude["launch"])
    _refused(svc, claude["launch"], crossing)         # the batch from before the re-enable, read again
    assert [c.turn_id for c in s.interrupts] == [None]


# --- L3 verify r4 (1026-3 d1b4j): an interrupted event is never interrupted again; activation under the mutex -------
class Refusing(Shaky):
    """Every reply fails (``raise``: OSError; ``no_such_request``: that answer); interrupts answer as Shaky's do."""

    def __init__(self, how: str, *outcomes: str, **kw) -> None:
        super().__init__(*(outcomes or ("confirmed",)), **kw)
        self.how, self.asked = how, 0

    def reject_control(self, request_id, text):
        self.asked += 1
        if self.how == "raise":
            raise OSError("broken pipe")
        return CommandResult("no_such_request", "no such request")


@pytest.mark.parametrize("how", ["raise", "no_such_request"])
def test_j1_a_failed_reply_re_read_five_times_is_interrupted_once(cs, claude, root, how):
    svc, s, _ = up(cs, claude, root, session=Refusing(how))
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    for _ in range(5):
        _refused(svc, claude["launch"], crossing)
    assert s.asked == 5                               # the reply is tried again on every read
    assert len(s.interrupts) == 1                     # its interrupt went through once and is never sent again
    assert "tripwire:unanswerable_control" in trips(cs, claude["launch"]) and latched(cs, claude["launch"])


def test_j1_a_failed_reply_whose_interrupt_failed_is_interrupted_again(cs, claude, root):
    svc, s, _ = up(cs, claude, root, session=Refusing("raise", "ambiguous", "confirmed"))
    crossing = _cross_with(event("error", "control:ctl-e", "00000100", turn_id="t-1", control_subtype="elicitation"))
    for _ in range(4):
        _refused(svc, claude["launch"], crossing)
    assert s.asked == 4 and len(s.interrupts) == 2


def test_j1_a_stale_failed_reply_whose_interrupt_went_through_is_not_interrupted_again(cs, claude, root):
    from tests.test_coordinator_chat_liveness import activate
    svc, s, _ = up(cs, claude, root, session=Refusing("raise"))
    crossing = _cross_with(event("error", "control:ctl-x", "00000100", turn_id="t-1", control_subtype="can_use_tool"))
    elicit = event("error", "control:ctl-e", "00000099", turn_id="t-1", control_subtype="elicitation")
    batch = crossing._replace(events=(elicit,) + crossing.events)
    _refused(svc, claude["launch"], batch)            # one interrupt, of t-1, for both events
    activate(svc, claude)
    _refused(svc, claude["launch"], batch)
    assert [c.turn_id for c in s.interrupts] == ["t-1"] and s.asked == 2


@pytest.mark.parametrize("writer", ["activate", "reenable"])
def test_j3_an_activation_or_re_enable_racing_a_read_waits_for_its_interrupt(cs, claude, root, writer):
    import threading
    from levain.coordinator.chat import CoordError
    from tests.test_coordinator_chat_liveness import activate
    order, racers, armed = [], [], []

    class Racing(Session):
        def reject_control(self, request_id, text):
            if not armed:
                return super().reject_control(request_id, text)

            def write():
                try:
                    if writer == "activate":
                        activate(svc, claude)
                    else:
                        svc.reenable(claude["launch"], "cockpit:tab-a")
                    order.append(writer)
                except CoordError:                    # a re-enable after the read's own latch is refused
                    order.append(f"{writer} refused")
            t = threading.Thread(target=write)
            racers.append(t)
            t.start()
            t.join(0.5)                               # the writer lands now unless it waits on the launch's mutex
            raise OSError("broken pipe")

        def interrupt(self, command):
            order.append(("interrupt", command.turn_id))
            return super().interrupt(command)

    svc, s, _ = up(cs, claude, root, session=Racing(), probe=Probe(Verdict(True, "ok")))
    if writer == "reenable":                          # a latch whose latest probe read live, so a re-enable is allowed
        s.inbound("ctl-0", "hook_callback", turn_id="t-0")
        svc.read_next(claude["launch"])
        s.show("turn_ended", "t-9", turn_id="t-9")
        svc.read_next(claude["launch"])
        assert latched(cs, claude["launch"])
    armed.append(True)
    s.inbound("ctl-1", "elicitation", turn_id="t-1")
    svc.read_next(claude["launch"])
    for t in racers:
        t.join(5)
    # the interrupt the read asked for goes out before the write, which waited for the read to end
    assert order[0] == ("interrupt", None) and order[1] in (writer, f"{writer} refused") and len(order) == 2
