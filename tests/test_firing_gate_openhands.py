"""The EFFERENT GATE mounted on the OpenHands runtime — K3, spore-295.

Gated on the extra (the pure classifier's own tests are base-lane in ``test_firing_gate.py``).
These exercise the half that can only be wrong in contact with the SDK: the risk-channel
mapping, the confirmation policy, and — the one that would ship silently — whether the wiring
actually TOOK.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("openhands.sdk", reason="openhands extra not installed")
pytest.importorskip("openhands.tools.terminal", reason="openhands extra not installed")

from openhands.sdk.event.llm_convertible import ActionEvent  # noqa: E402
from openhands.sdk.llm import MessageToolCall, TextContent  # noqa: E402
from openhands.sdk.security.confirmation_policy import (  # noqa: E402
    AlwaysConfirm,
    NeverConfirm,
)
from openhands.sdk.security.risk import SecurityRisk  # noqa: E402
from openhands.tools.file_editor.definition import FileEditorAction  # noqa: E402
from openhands.tools.terminal.definition import TerminalAction  # noqa: E402

from levain.firing.openhands.gate import (  # noqa: E402
    ConfirmEfferent,
    GateArmingError,
    LevainEfferentAnalyzer,
    arm_efferent_gate,
    awaiting_confirmation,
    disarm_efferent_gate,
    pending_gate_report,
)


def _event(tool_name: str, action, *, model_claimed_risk: str | None = None) -> ActionEvent:
    """A REAL ActionEvent — not a duck-typed stand-in.

    ``model_claimed_risk`` plants a security_risk the MODEL supplied in its own tool call, which
    is exactly the channel the SDK's LLMSecurityAnalyzer trusts and we must not."""
    args: dict = {}
    if model_claimed_risk is not None:
        args["security_risk"] = model_claimed_risk
    return ActionEvent(
        thought=[TextContent(text="...")],
        action=action,
        tool_name=tool_name,
        tool_call_id="call-1",
        tool_call=MessageToolCall(
            id="call-1", name=tool_name, arguments=json.dumps(args), origin="completion"
        ),
        llm_response_id="resp-1",
    )


# ---------- the analyzer reads the TOOL, never the model ----------

def test_a_model_claiming_its_own_action_is_low_risk_is_IGNORED():
    """THE LOAD-BEARING ONE. The SDK's stock path lets a model predict its own ``security_risk``,
    and ships an analyzer that trusts it. A gate an entity can talk its way through is not a
    gate — it is the false-all-clear class one layer below the exit codes, which are built on
    exactly the same rule: the harness reports what it SAW, never what the agent CLAIMED.

    Here the model insists a force-push is LOW. It gates anyway."""
    ev = _event(
        "terminal", TerminalAction(command="git push --force origin main"),
        model_claimed_risk="LOW",
    )
    assert LevainEfferentAnalyzer().security_risk(ev) == SecurityRisk.HIGH


def test_a_model_cannot_downgrade_a_write_by_claiming_it_is_a_read():
    ev = _event(
        "file_editor",
        FileEditorAction(command="create", path="/tmp/x", file_text="pwned"),
        model_claimed_risk="LOW",
    )
    assert LevainEfferentAnalyzer().security_risk(ev) == SecurityRisk.HIGH


@pytest.mark.parametrize(
    ("tool_name", "action", "expected"),
    [
        ("terminal", TerminalAction(command="ls"), SecurityRisk.HIGH),
        ("terminal", TerminalAction(command="rm -rf /"), SecurityRisk.HIGH),
        ("file_editor", FileEditorAction(command="view", path="/tmp/x"), SecurityRisk.LOW),
        ("file_editor", FileEditorAction(command="str_replace", path="/tmp/x",
                                         old_str="a", new_str="b"), SecurityRisk.HIGH),
        ("file_editor", FileEditorAction(command="undo_edit", path="/tmp/x"), SecurityRisk.HIGH),
    ],
)
def test_analyzer_maps_the_classification_onto_the_risk_channel(tool_name, action, expected):
    assert LevainEfferentAnalyzer().security_risk(_event(tool_name, action)) == expected


def test_an_unreadable_action_gates_rather_than_raising():
    """Fail-closed AND fail-soft: our own analyzer must reach the gate decision itself, not lean
    on the SDK coercing an analyzer exception to HIGH — relying on someone else's error handling
    for our invariant is how the invariant quietly stops holding."""

    class _Hostile:
        tool_name = "file_editor"

        @property
        def action(self):
            raise RuntimeError("unreadable")

    assert LevainEfferentAnalyzer().security_risk(_Hostile()) == SecurityRisk.HIGH


# ---------- the confirmation policy ----------

@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (SecurityRisk.LOW, False),
        (SecurityRisk.MEDIUM, True),
        (SecurityRisk.HIGH, True),
        (SecurityRisk.UNKNOWN, True),
    ],
)
def test_confirm_efferent_only_lets_LOW_run_free(risk, expected):
    """LOW is the single value we assign to perception. Everything else confirms — including
    UNKNOWN, which is what arrives if our analyzer is ever absent: the failure lands on
    over-gating (loud, safe) instead of on running ungoverned (silent, not)."""
    assert ConfirmEfferent().should_confirm(risk) is expected


def test_unknown_confirms_so_a_missing_analyzer_over_gates():
    """The explicit statement of the safe failure direction, pinned on its own so a future
    'optimisation' of the UNKNOWN branch has to argue with a named test."""
    assert ConfirmEfferent().should_confirm(SecurityRisk.UNKNOWN) is True


# ---------- arming: the wiring is PROVEN, not assumed ----------

class _FakeState:
    def __init__(self):
        self.security_analyzer = None
        self.confirmation_policy = NeverConfirm()
        self.events: list = []
        self.execution_status = "idle"


class _FakeConversation:
    """A conversation that honours the setters — the healthy case."""

    def __init__(self):
        self.state = _FakeState()

    def set_security_analyzer(self, analyzer):
        self.state.security_analyzer = analyzer

    def set_confirmation_policy(self, policy):
        self.state.confirmation_policy = policy


def test_arming_sets_both_halves_and_reads_them_back():
    conv = _FakeConversation()
    arm_efferent_gate(conv)
    assert isinstance(conv.state.security_analyzer, LevainEfferentAnalyzer)
    assert isinstance(conv.state.confirmation_policy, ConfirmEfferent)


def test_arming_REFUSES_when_the_policy_did_not_take():
    """THE FAILURE THIS EXISTS FOR. Analyzer set, policy silently ignored → the SDK's default
    NeverConfirm waves every efferent action through, and the session reports itself GATED while
    executing everything. That is strictly worse than an honestly ungated session, so arming
    must refuse rather than continue."""

    class _SwallowsPolicy(_FakeConversation):
        def set_confirmation_policy(self, policy):
            pass  # silently ignored — exactly what `**_: object` kwargs would have done

    with pytest.raises(GateArmingError) as exc:
        arm_efferent_gate(_SwallowsPolicy())
    assert "confirmation policy" in str(exc.value)


def test_arming_REFUSES_when_the_analyzer_did_not_take():
    class _SwallowsAnalyzer(_FakeConversation):
        def set_security_analyzer(self, analyzer):
            pass

    with pytest.raises(GateArmingError):
        arm_efferent_gate(_SwallowsAnalyzer())


def test_arming_REFUSES_when_a_foreign_policy_is_left_in_place():
    """A policy that is not OURS — even a stricter one — means our wiring did not land. Accepting
    AlwaysConfirm here would make the readback a formality that passes on the wrong object."""

    class _ForcesAlwaysConfirm(_FakeConversation):
        def set_confirmation_policy(self, policy):
            self.state.confirmation_policy = AlwaysConfirm()

    with pytest.raises(GateArmingError):
        arm_efferent_gate(_ForcesAlwaysConfirm())


def test_arming_surfaces_a_runtime_that_rejects_the_call_outright():
    class _Hostile:
        state = _FakeState()

        def set_security_analyzer(self, analyzer):
            raise RuntimeError("nope")

        def set_confirmation_policy(self, policy):
            raise RuntimeError("nope")

    with pytest.raises(GateArmingError):
        arm_efferent_gate(_Hostile())


def test_disarming_declares_the_ungated_posture_explicitly():
    """It is a no-op against today's SDK default, and that is why it is written down: an ungated
    REPL that works only because an upstream default happens to match our intent would break the
    day that default changes, with no line of ours to point at."""
    conv = _FakeConversation()
    arm_efferent_gate(conv)
    disarm_efferent_gate(conv)
    assert isinstance(conv.state.confirmation_policy, NeverConfirm)
    assert conv.state.security_analyzer is None


def test_disarming_never_raises_on_a_hostile_runtime():
    class _Hostile:
        def set_confirmation_policy(self, policy):
            raise RuntimeError("nope")

    disarm_efferent_gate(_Hostile())  # must not raise — ungated is already the SDK default


# ---------- the operator-facing report ----------

def test_the_report_shows_the_COMMAND_not_the_action_kind():
    """An operator judging ``git push --force`` needs the command. A report that said
    ``TerminalAction`` would name the tool while withholding the only content they can weigh."""
    conv = _FakeConversation()
    conv.state.events = [_event("terminal", TerminalAction(command="git push --force origin main"))]
    report = pending_gate_report(conv)
    assert len(report) == 1
    assert report[0].detail == "git push --force origin main"
    assert report[0].tool_name == "terminal"


def test_the_report_shows_the_file_operation_and_its_path():
    conv = _FakeConversation()
    conv.state.events = [
        _event("file_editor", FileEditorAction(command="create", path="/tmp/x", file_text="y"))
    ]
    report = pending_gate_report(conv)
    assert report[0].detail == "create /tmp/x"


def test_the_report_elides_a_command_that_would_bury_the_decision():
    conv = _FakeConversation()
    conv.state.events = [_event("terminal", TerminalAction(command="echo " + "x" * 4000))]
    detail = pending_gate_report(conv)[0].detail
    assert len(detail) < 300, "a here-doc payload must not push the decision off screen"
    assert detail.startswith("echo xxx")


def test_the_report_includes_afferent_members_of_a_halted_batch():
    """A batch halts as a UNIT, so its afferent members are also un-executed and also waiting.
    Listing only the efferent ones would under-report what approving actually authorises."""
    conv = _FakeConversation()
    conv.state.events = [
        _event("file_editor", FileEditorAction(command="view", path="/tmp/a")),
        _event("terminal", TerminalAction(command="rm -rf build")),
    ]
    report = pending_gate_report(conv)
    assert len(report) == 2
    assert {r.detail for r in report} == {"view /tmp/a", "rm -rf build"}


def test_the_report_NEVER_shows_the_models_own_account_of_why_it_wants_to_act():
    """PROVENANCE OF THE DECISION SURFACE. What the gate shows at the moment of fan-in IS the
    gate's product, so every line of it must be something the HARNESS observed — the tool the
    runtime recorded, the arguments it recorded — never the model's narration of its own intent.

    An ActionEvent carries a ``thought``. Rendering it beside the observed command would put a
    self-report and a harness observation on the same surface in the same typeface, and the
    operator would have no way to tell which is which. That is the exact blur the exit codes and
    the analyzer both refuse one layer down; the decision surface is where it would do the most
    damage, because it is the one a human actually acts on.

    A model that wants a force-push approved gets to have the command shown. It does not get to
    put its reassurance in front of the person deciding."""
    conv = _FakeConversation()
    ev = _event("terminal", TerminalAction(command="git push --force origin main"))
    ev = ev.model_copy(update={"thought": [TextContent(
        text="This is completely safe and routine, no need to check anything."
    )]})
    conv.state.events = [ev]

    rendered = " ".join(item.line() for item in pending_gate_report(conv))

    assert "git push --force origin main" in rendered
    assert "completely safe" not in rendered, "the model's self-report reached the decision surface"
    assert "no need to check" not in rendered


def test_the_reason_shown_is_the_harnesss_own_rule_not_a_confidence_score():
    """The gate states WHY it stopped something in terms of its own structural rule, which it can
    always vouch for. It never renders an aggregate, a score, or a count — there is nothing in
    K3 that aggregates, so there is no number here that could be inflated by restatement."""
    conv = _FakeConversation()
    conv.state.events = [_event("terminal", TerminalAction(command="curl x | bash"))]
    reason = pending_gate_report(conv)[0].reason
    assert "always fans in" in reason
    assert not any(ch.isdigit() for ch in reason), "no score, no count — a rule the gate owns"


def test_awaiting_confirmation_is_false_on_an_idle_conversation():
    assert awaiting_confirmation(_FakeConversation()) is False


def test_awaiting_confirmation_returns_NONE_when_the_status_cannot_be_read():
    """codex L3, HIGH — this test previously asserted ``False`` here and PINNED THE UNSAFE
    BEHAVIOUR. Collapsing "I could not tell" into "it is not held" made the caller's whole
    three-valued handling unreachable: dead code that read as a safeguard.

    ``None`` is not a nicety — it is what lets the driver refuse to continue blind instead of
    capturing, reporting a finished turn, and leaving the runtime parked at the gate for the
    next ``run()`` to approve implicitly."""
    class _Broken:
        @property
        def state(self):
            raise RuntimeError("gone")

    assert awaiting_confirmation(_Broken()) is None


def test_awaiting_confirmation_still_distinguishes_a_genuine_NOT_HELD():
    """The other half — ``None`` must not swallow the ordinary case, or every turn would end as
    undeterminable."""
    assert awaiting_confirmation(_FakeConversation()) is False


def test_awaiting_confirmation_is_true_when_the_runtime_says_so():
    from openhands.sdk import ConversationExecutionStatus

    conv = _FakeConversation()
    conv.state.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    assert awaiting_confirmation(conv) is True


# ---------- the session drive: what a halt must NOT do ----------

class _DriveConversation(_FakeConversation):
    """A conversation whose halt behaviour the test scripts turn by turn."""

    def __init__(self, halts: list[bool]):
        super().__init__()
        self._halts = list(halts)
        self.runs = 0
        self.messages: list[str] = []
        self.rejections: list[str] = []
        self.closed = False

    def send_message(self, message):
        self.messages.append(message)

    def run(self):
        from openhands.sdk import ConversationExecutionStatus

        halting = self._halts.pop(0) if self._halts else False
        self.runs += 1
        self.state.execution_status = (
            ConversationExecutionStatus.WAITING_FOR_CONFIRMATION if halting else "idle"
        )
        if halting:
            self.state.events = [_event("terminal", TerminalAction(command="git push"))]
        else:
            self.state.events = []

    def reject_pending_actions(self, reason="rejected"):
        from openhands.sdk import ConversationExecutionStatus

        self.rejections.append(reason)
        self.state.execution_status = ConversationExecutionStatus.IDLE
        self.state.events = []

    def close(self):
        self.closed = True


class _CountingBinding:
    def __init__(self, tmp_path):
        self.entity_dir = tmp_path
        self.episodic_path = tmp_path / "episodic.db"
        self.crystal_path = tmp_path / "crystal.json"
        self.captures = 0

    def capture_turn(self, conversation):
        self.captures += 1

    def wrap_nudge(self, **kwargs):
        return None


def _session(tmp_path, conv, *, gate_mode="gated"):
    from levain.session import EntitySession

    return EntitySession(
        entity_dir=tmp_path,
        binding=_CountingBinding(tmp_path),
        conversation=conv,
        workspace=tmp_path / "workspace",
        model_label="ollama/glm-5.2:cloud",
        with_tools=True,
        bash_ok=True,
        gate_mode=gate_mode,
    )


def test_a_gated_halt_DOES_NOT_CAPTURE_THE_TURN(tmp_path):
    """THE MEMORY DEFECT THIS ORDERING EXISTS TO PREVENT. ``vagus_run`` keys idempotency on the
    user-turn id, so capturing at the halt would make the post-approval capture a NO-OP on the
    unchanged id — memory would keep the moment the entity was STOPPED while the world got the
    work it was later allowed to do. Exactly the defect the nudge ordering already paid for
    once: a store perfectly grounded in a perfectly recorded misunderstanding."""
    conv = _DriveConversation([True])
    sess = _session(tmp_path, conv)

    result = sess.run_turn("push the branch")

    assert result.gated is True
    assert sess.binding.captures == 0, "a halted turn must not be written to memory"


def test_a_gated_halt_does_not_fire_the_act_first_nudge(tmp_path):
    """A halted turn looks exactly like a stalled one from outside: no tool ran, no reply. But
    nudging an entity that DID act — and was stopped by a human gate — to 'stop planning and
    act' is both false and useless. The gate check must come FIRST."""
    conv = _DriveConversation([True])
    sess = _session(tmp_path, conv)

    result = sess.run_turn("push the branch")

    assert result.nudged is False
    assert conv.runs == 1, "the nudge would have driven a second run()"
    assert len(conv.messages) == 1, "the nudge would have sent a second message"


def test_the_turn_is_captured_once_the_operator_approves(tmp_path):
    """The other half: approving continues ONE turn, and the completed turn captures exactly
    once — so the episode contains the whole arc including the halt, not a fragment."""
    conv = _DriveConversation([True, False])
    sess = _session(tmp_path, conv)

    first = sess.run_turn("push the branch")
    assert first.gated is True
    assert sess.binding.captures == 0

    second = sess.resume_turn()
    assert second.gated is False
    assert sess.binding.captures == 1, "the completed turn captures exactly once"


def test_rejecting_tells_the_entity_why_and_continues_the_turn(tmp_path):
    """A refusal is a MOVE in the conversation, not a dead end — the reason reaches the model so
    it can adapt instead of narrating success over a world it never touched."""
    conv = _DriveConversation([True, False])
    sess = _session(tmp_path, conv)
    sess.run_turn("push the branch")

    result = sess.reject_turn("we don't force-push main")

    assert conv.rejections == ["we don't force-push main"]
    assert result.gated is False


def test_A_FAILED_REFUSAL_DOES_NOT_BECOME_AN_APPROVAL(tmp_path):
    """glm L3, HIGH — and the test whose ABSENCE was the real defect.

    ``_drive()`` calls ``run()``, and ``run()`` on a still-halted conversation is the SDK's
    APPROVAL path. So if ``reject_pending_actions`` fails and the code drives on anyway, the
    operator's explicit "no" silently executes the action they refused — the gate's one
    invariant, broken by its own error handling.

    The sibling test above (rejection succeeds → turn continues) STIPULATED that rejection
    works. A harness that stipulates what it tests proves nothing about the direction that
    matters: what happens when the refusal does NOT land."""
    from openhands.sdk import ConversationExecutionStatus

    class _RejectionFails(_DriveConversation):
        def reject_pending_actions(self, reason="rejected"):
            raise RuntimeError("transient runtime failure")

    conv = _RejectionFails([True])
    sess = _session(tmp_path, conv)
    sess.run_turn("push the branch")
    runs_before = conv.runs

    result = sess.reject_turn("absolutely not")

    assert conv.runs == runs_before, (
        "run() was called after a failed refusal — that is the SDK's APPROVAL path, so the "
        "refused action would have executed"
    )
    assert result.error is not None, "a refusal that did not land must be reported, not hidden"
    assert result.gated is True, "the actions are still held"
    assert sess.binding.captures == 0
    assert (
        conv.state.execution_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
    ), "the conversation is still halted — nothing was executed"


def test_an_UNREADABLE_gate_status_ends_the_turn_instead_of_cascading(tmp_path):
    """glm L3, LOW — the deferred form of the HIGH.

    If the status read raises and we call that "not halted", the driver captures and reports an
    ordinary finished turn. But the SDK may still have the conversation parked at the gate, so
    the NEXT turn's ``run()`` would execute the previous turn's held actions with nobody having
    approved them — the same invariant broken, one turn later.

    So undeterminable ends the turn as a FAILURE: not captured, non-zero, and no "next turn"
    for the cascade to use."""
    from levain.session import EXIT_TURN_FAILED

    # The conversation RUNS FINE; only the STATUS READ fails, and it starts failing only AFTER
    # the run. The earlier version of this test raised from `state` itself, so `run()` blew up
    # and the error came from the generic exception handler — it passed while proving nothing
    # about the gate path at all (codex L3). A test green for the wrong reason is worse than
    # none, so this one pins that run() actually succeeded first.
    conv = _StatusUnreadableAfterRun()
    sess = _session(tmp_path, conv)

    result = sess.run_turn("do the thing")

    assert conv.runs == 1, "run() must have succeeded — the failure is the STATUS READ alone"
    assert result.gated is False, "we do not claim a halt we could not read"
    assert result.error is not None, "nor do we claim an ordinary completed turn"
    assert result.exit_code == EXIT_TURN_FAILED
    assert sess.binding.captures == 0, "an undeterminable turn must not be written to memory"


def test_an_unreadable_gate_status_refuses_the_turn_BEFORE_it_starts(tmp_path):
    """The pre-flight half. If the gate's state is unreadable at entry, the turn never begins —
    `run_turn` must not send a message into a conversation whose hold-state it cannot determine,
    because that run() would be the implicit-approval path."""
    conv = _StatusUnreadableAfterRun(fail_from=0)
    sess = _session(tmp_path, conv)

    result = sess.run_turn("do the thing")

    assert conv.runs == 0, "the turn must not start"
    assert conv.messages == [], "and the message must not be sent"
    assert result.error is not None
    assert sess.binding.captures == 0


class _StatusUnreadableAfterRun:
    """Runs normally; its execution_status READ starts raising after ``fail_from`` reads.

    Writes always succeed — only reads fail — so ``run()`` behaves like an ordinary successful
    run and the failure is isolated to the status query the gate depends on."""

    def __init__(self, fail_from: int = 1):
        self.runs = 0
        self.messages: list[str] = []
        self.state = _RaisingState(fail_from)

    def send_message(self, message):
        self.messages.append(message)

    def run(self):
        self.runs += 1

    def close(self):
        pass


class _RaisingState:
    def __init__(self, fail_from: int):
        self.events: list = []
        self.security_analyzer = None
        self.confirmation_policy = NeverConfirm()
        self._reads = 0
        self._fail_from = fail_from

    @property
    def execution_status(self):
        self._reads += 1
        if self._reads > self._fail_from:
            raise RuntimeError("status unreadable")
        return "idle"

    @execution_status.setter
    def execution_status(self, value):
        pass


def test_TALKING_TO_A_HELD_SESSION_IS_NOT_AN_APPROVAL(tmp_path):
    """codex L3, HIGH. ``send_message`` does not clear ``WAITING_FOR_CONFIRMATION``, but the
    ``run()`` after it DOES — the SDK clears it as *the user approved*, and ``Agent.step()``
    executes the unmatched pending actions before sampling anything new.

    So a driver that answers a halt by talking again — "wait, don't push that" — would execute
    the very push it is objecting to. The REPL and ``--task`` never do this, but ``EntitySession``
    is the shared long-lived API that a ``/turn`` route is built on next."""
    conv = _DriveConversation([True, False])
    sess = _session(tmp_path, conv)
    sess.run_turn("push the branch")
    runs_before, msgs_before = conv.runs, len(conv.messages)

    result = sess.run_turn("wait, don't push that")

    assert conv.runs == runs_before, "run() after a held gate is the SDK's APPROVAL path"
    assert len(conv.messages) == msgs_before, "the message must not even be sent"
    assert result.gated is True
    assert result.error is not None and "resume_turn" in result.error, (
        "the caller must be told what to do instead, or they will just try again"
    )
    assert sess.binding.captures == 0


def test_a_held_action_is_not_reported_as_completed_work(tmp_path):
    """codex L3, LOW — and it was visible in the live run without being noticed.

    The runtime emits an ``ActionEvent`` BEFORE the confirmation decision and then skips
    execution, so an unfiltered activity list prints ``⚙ terminal: git push`` — the standard
    "here is what it DID" line — on the same screen where the gate says nothing was executed.
    Two true-looking statements that contradict each other, with the operator left to guess."""
    # A FILE EDITOR action deliberately, not bash: `turn_tool_activity` renders a terminal
    # action as its KIND ("⚙ terminal: TerminalAction") but a file edit as "⚙ file_editor:
    # create x.txt" — the real, readable "here is what it DID" line. An assertion written
    # against the bash rendering is VACUOUS (it can never contain the command), which is how
    # the first version of this test passed against the unfiltered implementation.
    class _HeldFileWrite(_DriveConversation):
        def run(self):
            from openhands.sdk import ConversationExecutionStatus

            self.runs += 1
            self.state.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            self.state.events = [
                _event("file_editor", FileEditorAction(
                    command="create", path=str(tmp_path / "workspace" / "x.txt"), file_text="y",
                ))
            ]

    conv = _HeldFileWrite([True])
    sess = _session(tmp_path, conv)

    result = sess.run_turn("write the file")

    assert result.gated is True
    assert result.tool_activity == [], (
        f"a HELD action was reported as completed work: {result.tool_activity}"
    )
    # It is still visible where it belongs — as a decision, not as work done.
    assert any("create" in p.detail for p in result.pending)


def test_an_ungated_session_never_halts_and_pays_nothing_for_the_gate(tmp_path):
    """The default REPL path must be untouched by K3. Even with the runtime reporting a halt, an
    UNGATED session short-circuits before consulting it — so an operator who never opted in
    cannot acquire a gate by accident."""
    conv = _DriveConversation([True])
    sess = _session(tmp_path, conv, gate_mode="ungated")

    result = sess.run_turn("push the branch")

    assert result.gated is False
    assert sess.binding.captures == 1, "an ungated turn captures normally"


def test_pending_efferent_is_empty_when_nothing_is_held(tmp_path):
    """AUTHORITY before DESCRIPTION: an un-halted session must not be handed the
    'contents unknown' placeholder that a real-but-undescribable halt produces."""
    conv = _DriveConversation([False])
    sess = _session(tmp_path, conv)
    sess.run_turn("what does the readme say")
    assert sess.pending_efferent() == ()


def test_a_halt_the_report_cannot_describe_still_reports_as_gated(tmp_path):
    """AUTHORITY ≠ DESCRIPTION, end to end. The runtime says it halted but the event log yields
    nothing describable — the turn must still be gated (actions did not run), and the operator
    must get a loud placeholder rather than an EMPTY decision, because 'held, contents unknown'
    and 'nothing held' look identical to a reader and mean opposite things."""
    from openhands.sdk import ConversationExecutionStatus

    class _OpaqueHalt(_DriveConversation):
        def run(self):
            self.runs += 1
            self.state.execution_status = (
                ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            )
            self.state.events = []  # nothing the report can render

    conv = _OpaqueHalt([True])
    sess = _session(tmp_path, conv)

    result = sess.run_turn("do the thing")

    assert result.gated is True
    assert sess.binding.captures == 0
    assert len(result.pending) == 1
    assert result.pending[0].recognized is False
    assert "could not be read" in result.pending[0].detail
