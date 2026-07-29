"""levain.session + levain.run.run_task — the K1 keystone: one session, many drivers.

Two tiers, matching `test_run.py`'s convention:
  - PURE tests (no openhands extra): the TurnResult contract, the streaming callback, the
    session lifecycle, and `run_task` driven against a fake session.
  - openhands-gated: nothing here needs the SDK — `EntitySession.open` is exercised through
    its guards in `test_run.py`, and `run_task` is tested by injecting a fake session, which
    is the point of the extraction (a driver testable without a live entity).

THE LOAD-BEARING TEST IN THIS FILE is `test_streamed_activity_line_is_byte_identical_to_the_post_turn_line`.
`levain.session` CLAIMS in its docstring that a streamed line and a post-turn line are
byte-identical. An untested claim of that shape is how a display seam silently forks, so the
claim is pinned here — and it is written to FAIL if the two formatters ever diverge, not merely
to pass today.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.session import (
    EXIT_GATED,
    EXIT_NO_REPLY,
    EXIT_OK,
    EXIT_TURN_FAILED,
    EXIT_USAGE,
    SessionStartError,
    TurnResult,
    _activity_callback,
    turn_tool_activity,
)


# ---------- duck-typed event stand-ins (same shapes as test_run.py) ----------

class _ToolAction:
    def __init__(self, kind, command, path):
        self.kind = kind
        self.command = command
        self.path = path


class _ToolEvent:
    """An agent ActionEvent that is a REAL tool call."""

    def __init__(self, tool_name="terminal", kind="ExecuteBashAction",
                 command="python3 test.py", path=None, source="agent"):
        self.source = source
        self.tool_name = tool_name
        self.llm_message = None
        self.action = _ToolAction(kind, command, path)


class _Content:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, role, texts):
        self.role = role
        self.content = [_Content(t) for t in texts]


class _Event:
    def __init__(self, source, texts):
        self.source = source
        self.llm_message = _Msg(source, texts)


# ---------- the TurnResult contract: harness-observable facts only ----------

def test_turn_result_replied_is_exit_ok():
    r = TurnResult(reply="done", tool_activity=["⚙ terminal: pytest"])
    assert r.ok is True
    assert r.exit_code == EXIT_OK == 0


def test_turn_result_no_reply_is_a_failure_not_a_quiet_success():
    """A completed turn with NO reply is a REAL failure mode (a weak open model can end a turn
    saying nothing usable). Reporting it as success would report a no-op as done."""
    r = TurnResult(reply=None, tool_activity=[])
    assert r.ok is False
    assert r.exit_code == EXIT_NO_REPLY == 1


def test_turn_result_empty_string_reply_counts_as_no_reply():
    # An empty reply is not a reply — guard the truthiness boundary explicitly, since a model
    # returning "" is a stall wearing the shape of an answer.
    r = TurnResult(reply="", tool_activity=[])
    assert r.ok is False
    assert r.exit_code == EXIT_NO_REPLY


def test_turn_result_error_is_exit_turn_failed_even_with_a_reply():
    """error dominates: a turn that raised is EXIT_TURN_FAILED regardless of any partial text,
    because the SDK conversation is left un-resumable."""
    r = TurnResult(reply="partial", tool_activity=[], error="boom")
    assert r.ok is False
    assert r.exit_code == EXIT_TURN_FAILED == 3


def test_exit_codes_are_distinct_and_stable():
    # These are a PROCESS CONTRACT a scheduler/supervisor branches on — pin the values.
    assert (EXIT_OK, EXIT_NO_REPLY, EXIT_USAGE, EXIT_TURN_FAILED, EXIT_GATED) == (0, 1, 2, 3, 4)
    # And they must stay DISTINCT — the whole value of the contract is that a supervisor can
    # tell "waiting for a human" from "stalled" without reading prose.
    assert len({EXIT_OK, EXIT_NO_REPLY, EXIT_USAGE, EXIT_TURN_FAILED, EXIT_GATED}) == 5


def test_turn_result_carries_no_task_success_field():
    """THE HONESTY FLOOR ON RESULTS. TurnResult must never grow a 'succeeded' / 'task_ok' field:
    a confined entity cannot truthfully report on its own environment (spore-373 [5] — it
    reported '25 failed, 1617 passed' for a suite that was 1702/0 outside the sandbox). This
    test fails the moment someone adds one, which is the point.

    The K3 additions (``gated``, ``pending``) are admissible under exactly that rule, and the
    distinction IS the floor: both are things the HARNESS observed — the runtime's own execution
    status, and the tool calls sitting un-executed in its event log — never something the agent
    asserted about its own work. The forbidden list below is what a self-report looks like.
    Widening this set is fine; widening it with a self-report is the failure."""
    fields = set(TurnResult.__dataclass_fields__)
    assert fields == {"reply", "tool_activity", "error", "nudged", "gated", "pending"}
    for forbidden in ("succeeded", "success", "task_ok", "passed", "verdict"):
        assert not hasattr(TurnResult(reply="x"), forbidden)


# ---------- the streaming callback ----------

def test_streamed_activity_line_is_byte_identical_to_the_post_turn_line(tmp_path: Path):
    """levain.session's docstring CLAIMS the streamed line and the post-turn line are
    byte-identical. Pin it: the REPL renders post-turn, the headless driver streams, and if the
    two formatters drift, the same action reads differently depending on which driver ran it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ev = _ToolEvent(tool_name="file_editor", kind="FileEditAction",
                    command=None, path=str(workspace / "notes.md"))

    post_turn = turn_tool_activity([ev], workspace)

    streamed: list[str] = []
    cb = _activity_callback(streamed.append, workspace)
    cb(ev)

    assert streamed == post_turn, "streamed and post-turn activity lines diverged"
    assert len(streamed) == 1
    # And the workspace prefix really is stripped in BOTH (the readability contract).
    assert str(workspace) not in streamed[0]


def test_activity_callback_ignores_non_agent_events(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen: list[str] = []
    cb = _activity_callback(seen.append, workspace)
    cb(_Event("user", ["do the thing"]))
    cb(_ToolEvent(source="environment"))
    assert seen == []


def test_activity_callback_never_raises_into_the_run_loop(tmp_path: Path):
    """A callback that raises would propagate into the SDK's run loop and kill a real turn over
    a DISPLAY concern. Fail-soft is structural here, not politeness."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _explode(_line: str) -> None:
        raise RuntimeError("display blew up")

    cb = _activity_callback(_explode, workspace)
    cb(_ToolEvent())  # must not raise

    class _Hostile:
        @property
        def source(self):
            raise ValueError("hostile event")

    cb(_Hostile())  # must not raise either


# ---------- SessionStartError ----------

def test_session_start_error_carries_message_and_usage_code():
    exc = SessionStartError("not an entity\n  fix: levain init")
    assert exc.code == EXIT_USAGE == 2
    assert str(exc) == exc.message
    assert "fix: levain init" in exc.message


# ---------- closed-session handling (codex L3) ----------

def test_run_turn_on_a_closed_session_returns_a_result_and_does_not_raise():
    """codex L3: run_turn's contract is 'never raises for a failed turn, because a long-lived
    server session must not die on one bad turn' — and a STALE SESSION ID from a reconnecting
    client is exactly that case. Raising would kill the route over a handleable race."""
    from levain.session import _CLOSED_SESSION_ERROR, EntitySession

    sess = EntitySession(
        entity_dir=Path("/tmp/x"), binding=object(), conversation=object(),
        workspace=Path("/tmp/x/workspace"), model_label="m", with_tools=False, bash_ok=False,
    )
    sess.close()
    assert sess.closed is True

    result = sess.run_turn("anything")          # must NOT raise
    assert result.error == _CLOSED_SESSION_ERROR
    assert result.ok is False
    assert result.exit_code == EXIT_TURN_FAILED
    # …and the marker stays detectable, so a driver CAN still treat misuse as a bug.
    assert "closed" in result.error


def test_close_is_idempotent_and_never_raises():
    """A driver's `finally: session.close()` can run after an explicit close; and a teardown
    that raises would mask the real error that triggered it."""
    from levain.session import EntitySession

    class _HostileConversation:
        def close(self):
            raise RuntimeError("teardown exploded")

    sess = EntitySession(
        entity_dir=Path("/tmp/x"), binding=object(), conversation=_HostileConversation(),
        workspace=Path("/tmp/x/workspace"), model_label="m", with_tools=False, bash_ok=False,
    )
    sess.close()   # swallows the hostile teardown
    sess.close()   # idempotent
    assert sess.closed is True


def test_session_context_manager_closes_on_exit():
    from levain.session import EntitySession

    class _Conv:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    conv = _Conv()
    sess = EntitySession(
        entity_dir=Path("/tmp/x"), binding=object(), conversation=conv,
        workspace=Path("/tmp/x/workspace"), model_label="m", with_tools=False, bash_ok=False,
    )
    with sess:
        pass
    assert conv.closed is True and sess.closed is True


# ---------- empty --task is usage, caught BEFORE a session is opened (codex L3) ----------

def test_run_task_rejects_an_empty_task_without_opening_a_session(tmp_path: Path, monkeypatch, capsys):
    """EntitySession.open spawns a sandboxed bash subprocess and mints the confined tool
    bundle. Discovering 'there was no task' after all that is wasteful AND confusing — the
    operator sees a full sovereign banner for a no-op."""
    from levain.run import run_task

    opened: list[str] = []

    def _should_not_open(path, **kwargs):
        opened.append("opened")
        raise AssertionError("a session must not be opened for an empty --task")

    monkeypatch.setattr("levain.run.EntitySession.open", staticmethod(_should_not_open))

    for empty in ("", "   ", "\n\t "):
        rc = run_task(tmp_path, empty)
        assert rc == EXIT_USAGE
    assert opened == []

    err = capsys.readouterr().err
    assert "must not be empty" in err


# ---------- run_task, driven against a fake session ----------

class _FakeBinding:
    def __init__(self, tmp_path: Path) -> None:
        self.episodic_path = tmp_path / ".levain" / "memory.db"
        self.crystal_path = tmp_path / ".levain" / "memory.crystal.json"


class _FakeSession:
    """A stand-in for EntitySession — the whole point of the K1 extraction is that a DRIVER can
    be tested without standing up a live sovereign entity."""

    def __init__(self, tmp_path: Path, result: TurnResult, *, emits: list[str] | None = None):
        self.entity_dir = tmp_path
        self.binding = _FakeBinding(tmp_path)
        self.workspace = tmp_path / "workspace"
        self.model_label = "ollama/glm-5.2:cloud"
        self.with_tools = True
        self.bash_ok = True
        self.ssh_mode = "agent"
        self.deny_standard_creds = False
        self.gate_mode = "ungated"
        self.label = tmp_path.name
        self._result = result
        self._emits = emits or []
        self.turns: list[str] = []
        self.closed_count = 0
        self.opened_with: dict = {}

    def run_turn(self, message: str) -> TurnResult:
        self.turns.append(message)
        # Drive the streaming callback the way a REAL session does — the SDK invokes it during
        # the turn. Without this a "was it streamed?" test can only prove the NEGATIVE (not
        # reprinted), never that the line appeared exactly once (codex L3 test-strength note).
        cb = self.opened_with.get("on_event")
        if cb is not None:
            for line in self._emits:
                cb(line)
        return self._result

    def close(self) -> None:
        self.closed_count += 1

    def wrap_nudge(self):
        return None


def _patch_open(monkeypatch, session_or_exc):
    """Point run_task's session factory at a fake."""
    def _open(path, **kwargs):
        if isinstance(session_or_exc, Exception):
            raise session_or_exc
        session_or_exc.opened_with = kwargs
        return session_or_exc
    monkeypatch.setattr("levain.run.EntitySession.open", staticmethod(_open))


def test_run_task_declares_no_human_is_driving(tmp_path: Path, monkeypatch, capsys):
    """The headless driver must open in a mode with NO human driving — that is what makes the
    default ``efferent_gate: "auto"`` resolve GATED for a scheduler or an unattended seat, which
    is the entire case K3 exists to serve."""
    from levain.firing.drive import human_present
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply="done", tool_activity=[]))
    _patch_open(monkeypatch, sess)
    run_task(tmp_path, "do it")
    assert sess.opened_with.get("mode") == "headless"
    assert human_present(sess.opened_with["mode"]) is False


def test_run_task_unattended_is_a_DISTINCT_mode_from_plain_headless(tmp_path: Path, monkeypatch,
                                                                    capsys):
    """K4a: `--task` typed by a human and a scheduled seat are NOT the same drive.

    The gate rightly treats them alike (neither has anyone to fan an action in to). The crown-
    jewels CRED floor must not: a human running `--task "open a PR"` legitimately needs `gh`,
    while a seat's silent credential read can compound into always-loaded memory with nobody in
    the loop. Collapsing them is the bug this mode exists to prevent — so pin that they differ,
    AND that both still deny a human."""
    from levain.firing.drive import human_present
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply="done", tool_activity=[]))
    _patch_open(monkeypatch, sess)
    run_task(tmp_path, "do it", unattended=True)
    assert sess.opened_with.get("mode") == "unattended"
    assert human_present(sess.opened_with["mode"]) is False


def test_run_entity_declares_that_a_human_IS_driving(tmp_path: Path, monkeypatch, capsys):
    """And the REPL opens with ``human_present=True``: the operator watching activity stream past
    IS the fan-in, so `auto` resolves ungated and no prompt is imposed on them."""
    from levain.run import run_entity

    sess = _FakeSession(tmp_path, TurnResult(reply="hi", tool_activity=[]))
    _patch_open(monkeypatch, sess)
    monkeypatch.setattr("levain.run.TurnReader", lambda: _EOFReader())
    run_entity(tmp_path)
    assert sess.opened_with.get("mode") == "interactive"
    from levain.firing.drive import human_present
    assert human_present(sess.opened_with["mode"]) is True


class _EOFReader:
    """A TurnReader stand-in that ends the session immediately."""

    cancelled_lines = 0

    def read_turn(self, prompt):
        return None


def test_run_task_gated_exits_4_and_says_nothing_ran(tmp_path: Path, monkeypatch, capsys):
    """The headless half of the gate. Exit 4 (a decision, not a failure), the report on STDERR,
    and — the part a supervisor's log depends on — an explicit statement that NOTHING WAS
    EXECUTED, so a silent stdout cannot be read as "it did the work and said nothing"."""
    from levain.firing.gate import PendingEfferent
    from levain.run import run_task

    held = PendingEfferent(
        tool_name="terminal", detail="git push --force origin main",
        reason="bash always fans in", recognized=True,
    )
    sess = _FakeSession(
        tmp_path, TurnResult(reply=None, tool_activity=[], gated=True, pending=(held,))
    )
    _patch_open(monkeypatch, sess)

    # quiet=True is the PIPELINE case: stdout carries the reply payload and nothing else, so it
    # is where "did anything get emitted as if it were a result?" can actually be asserted.
    rc = run_task(tmp_path, "push the branch", quiet=True)
    out = capsys.readouterr()

    assert rc == EXIT_GATED == 4
    assert "nothing was executed" in out.err.lower()
    assert "git push --force origin main" in out.err, "the operator must see the actual command"
    assert out.out.strip() == "", "stdout is the reply payload, and there is no reply yet"
    assert sess.closed_count == 1


def test_run_task_gated_does_not_report_the_stall_message(tmp_path: Path, monkeypatch, capsys):
    """A halt must not be described as "completed the turn without a reply". Same words, opposite
    meaning: one needs a human, the other needs a restart."""
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply=None, tool_activity=[], gated=True))
    _patch_open(monkeypatch, sess)

    run_task(tmp_path, "push")
    err = capsys.readouterr().err

    assert "without a reply" not in err


def test_run_task_replies_and_exits_zero(tmp_path: Path, monkeypatch, capsys):
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply="I fixed the bug.", tool_activity=["⚙ terminal: pytest"]))
    _patch_open(monkeypatch, sess)

    rc = run_task(tmp_path, "fix the bug")
    out = capsys.readouterr()

    assert rc == EXIT_OK
    assert sess.turns == ["fix the bug"]
    assert "I fixed the bug." in out.out
    assert sess.closed_count == 1, "the session must be closed even on the happy path"


def test_run_task_no_reply_exits_one_and_says_why_on_stderr(tmp_path: Path, monkeypatch, capsys):
    """The stall case. stdout stays an empty payload; the REASON goes to stderr so a pipeline
    gets both a clean payload and a visible failure."""
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply=None, tool_activity=[], nudged=True))
    _patch_open(monkeypatch, sess)

    rc = run_task(tmp_path, "do it", quiet=True)
    out = capsys.readouterr()

    assert rc == EXIT_NO_REPLY
    assert out.out.strip() == "", "quiet mode must emit no payload when there is no reply"
    assert "without a reply" in out.err
    assert "act-first nudge" in out.err, "the nudge fact is diagnostic — surface it"


def test_run_task_turn_failure_exits_three(tmp_path: Path, monkeypatch, capsys):
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply=None, tool_activity=[], error="model exploded"))
    _patch_open(monkeypatch, sess)

    rc = run_task(tmp_path, "do it")
    out = capsys.readouterr()

    assert rc == EXIT_TURN_FAILED
    assert "model exploded" in out.err
    assert sess.closed_count == 1


def test_run_task_startup_error_exits_two_before_any_turn(tmp_path: Path, monkeypatch, capsys):
    from levain.run import run_task

    _patch_open(monkeypatch, SessionStartError("not an initialized Levain entity"))

    rc = run_task(tmp_path, "do it")
    out = capsys.readouterr()

    assert rc == EXIT_USAGE
    assert "not an initialized Levain entity" in out.err
    assert out.out.strip() == "", "a startup error must not pollute the stdout payload"


def test_run_task_quiet_prints_only_the_reply(tmp_path: Path, monkeypatch, capsys):
    """--quiet is the pipeline contract: stdout carries the reply and NOTHING else — no banner,
    no activity, no entity label."""
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply="42", tool_activity=["⚙ terminal: compute"]))
    _patch_open(monkeypatch, sess)

    rc = run_task(tmp_path, "answer", quiet=True)
    out = capsys.readouterr()

    assert rc == EXIT_OK
    assert out.out == "42\n"
    assert "levain run —" not in out.out
    assert "⚙" not in out.out


def test_run_task_streams_each_activity_line_exactly_once(tmp_path: Path, monkeypatch, capsys):
    """Activity must appear EXACTLY ONCE — streamed live, never also reprinted post-turn.

    The fake session drives on_event the way the real SDK does, so this pins BOTH halves:
    the line IS emitted (a driver that forgot to wire on_event fails), and it is emitted only
    once (a driver that also printed result.tool_activity fails). Proving only the negative was
    the weakness codex flagged in the first cut of this test."""
    from levain.run import run_task

    line = "⚙ terminal: pytest -q"
    sess = _FakeSession(
        tmp_path,
        TurnResult(reply="ok", tool_activity=[line]),   # post-turn copy the driver must NOT print
        emits=[line],                                    # what the SDK streams during the turn
    )
    _patch_open(monkeypatch, sess)

    run_task(tmp_path, "run the tests")
    out = capsys.readouterr().out

    assert out.count(line) == 1, (
        f"activity must stream exactly once, got {out.count(line)} occurrences — "
        "0 means on_event was never wired, 2 means result.tool_activity was also reprinted"
    )


def test_run_task_quiet_suppresses_the_activity_stream_entirely(tmp_path: Path, monkeypatch, capsys):
    from levain.run import run_task

    line = "⚙ terminal: pytest -q"
    sess = _FakeSession(tmp_path, TurnResult(reply="ok", tool_activity=[line]), emits=[line])
    _patch_open(monkeypatch, sess)

    run_task(tmp_path, "run the tests", quiet=True)
    out = capsys.readouterr()

    assert out.out == "ok\n"
    assert line not in out.out and line not in out.err


def test_run_task_passes_streaming_and_bounds_through_to_the_session(tmp_path: Path, monkeypatch):
    """The driver must actually WIRE the streaming callback and the iteration bound — otherwise
    a long headless turn is silent and unbounded, which is the failure K1 exists to remove."""
    from levain.run import run_task

    sess = _FakeSession(tmp_path, TurnResult(reply="ok"))
    _patch_open(monkeypatch, sess)

    run_task(tmp_path, "go", max_iterations=7)
    assert sess.opened_with["max_iterations"] == 7
    assert callable(sess.opened_with["on_event"]), "non-quiet must stream"

    sess2 = _FakeSession(tmp_path, TurnResult(reply="ok"))
    _patch_open(monkeypatch, sess2)
    run_task(tmp_path, "go", quiet=True)
    assert sess2.opened_with["on_event"] is None, "quiet must NOT stream"


def test_run_task_interrupt_exits_130_not_the_usage_code(tmp_path: Path, monkeypatch, capsys):
    """glm L3 (HIGH): a Ctrl-C is NOT a configuration error. Returning EXIT_USAGE(2) sends a
    supervisor hunting for a bad --model when a human simply interrupted — actively misleading
    in exactly the unattended-supervision case this keystone exists to enable. POSIX: 128+2."""
    from levain.run import run_task
    from levain.session import EXIT_INTERRUPTED

    class _Interrupting(_FakeSession):
        def run_turn(self, message: str) -> TurnResult:
            raise KeyboardInterrupt

    sess = _Interrupting(tmp_path, TurnResult(reply=None))
    _patch_open(monkeypatch, sess)

    rc = run_task(tmp_path, "long job", quiet=True)

    assert rc == EXIT_INTERRUPTED == 130
    assert rc != EXIT_USAGE, "an interrupt must be distinguishable from a usage error"
    assert "interrupted" in capsys.readouterr().err
    assert sess.closed_count == 1, "an interrupted run must still close the session"


def test_repl_closes_the_session_even_if_the_banner_raises(tmp_path: Path, monkeypatch):
    """glm L3 (MED): the banner used to print in the GAP between open() and the loop's
    try/finally, so a BrokenPipeError on a closed stdout jumped past `session.close()` and
    leaked the session — and with it the OS-sandboxed bash subprocess it holds."""
    from levain.run import run_entity

    sess = _FakeSession(tmp_path, TurnResult(reply="ok"))
    _patch_open(monkeypatch, sess)

    def _explode(_session, **_kw):
        raise BrokenPipeError("stdout closed")

    monkeypatch.setattr("levain.run._banner_for", _explode)

    with pytest.raises(BrokenPipeError):
        run_entity(tmp_path)

    assert sess.closed_count == 1, "the session must be closed even when the banner blows up"


def test_run_task_closes_the_session_on_every_path(tmp_path: Path, monkeypatch):
    """A driver that leaks a session leaks a sandboxed bash subprocess with it."""
    from levain.run import run_task

    for result in (
        TurnResult(reply="ok"),
        TurnResult(reply=None),
        TurnResult(reply=None, error="boom"),
    ):
        sess = _FakeSession(tmp_path, result)
        _patch_open(monkeypatch, sess)
        run_task(tmp_path, "go", quiet=True)
        assert sess.closed_count == 1


# ---------- CLI routing ----------

def test_cli_task_routes_to_run_task_and_bare_run_routes_to_the_repl(monkeypatch, tmp_path: Path):
    """`--task` selects the headless driver; its absence selects the REPL. One flag, two
    drivers, one session — the K1 shape.

    Both halves matter: routing to run_task is the new capability, and routing to the REPL
    WITHOUT --task is the no-regression half (the extraction must not steal the bare `run`)."""
    from levain.cli import main

    calls: dict[str, dict] = {}

    def _fake_task(**kw):
        calls["task"] = kw
        return 0

    def _fake_repl(**kw):
        calls["repl"] = kw
        return 0

    # `_cmd_run` imports these INSIDE the function, so patching the module attribute is what a
    # real dispatch will see.
    monkeypatch.setattr("levain.run.run_task", _fake_task)
    monkeypatch.setattr("levain.run.run_entity", _fake_repl)

    rc = main(["run", str(tmp_path), "--task", "fix the bug", "--max-iterations", "5"])
    assert rc == 0
    assert "task" in calls and "repl" not in calls
    assert calls["task"]["task"] == "fix the bug"
    assert calls["task"]["max_iterations"] == 5
    assert calls["task"]["quiet"] is False

    calls.clear()
    rc = main(["run", str(tmp_path)])
    assert rc == 0
    assert "repl" in calls and "task" not in calls, "a bare `levain run` must still be the REPL"


def test_cli_task_quiet_flag_reaches_the_driver(monkeypatch, tmp_path: Path):
    from levain.cli import main

    calls: dict[str, dict] = {}
    monkeypatch.setattr("levain.run.run_task", lambda **kw: (calls.update(kw), 0)[1])
    main(["run", str(tmp_path), "--task", "go", "--quiet"])
    assert calls["quiet"] is True


def test_cli_run_help_documents_the_exit_code_contract(capsys):
    """The exit codes are a PROCESS CONTRACT — a scheduler branches on them, so they must be
    discoverable from `--help`, not only from the source. And the help must carry the HONESTY
    caveat: an operator who reads exit 0 as 'the task succeeded' has misread it."""
    from levain.cli import main

    with pytest.raises(SystemExit):
        main(["run", "--help"])
    # argparse HARD-WRAPS help text, so assert against whitespace-normalized output: the
    # wrapping is a display artifact, and matching raw would make this test fail on terminal
    # width rather than on missing content.
    run_help = " ".join(capsys.readouterr().out.split())

    assert "--task" in run_help
    for token in ("0 replied", "1 completed with no reply", "2 startup", "3 the turn raised"):
        assert token in run_help, f"exit-code contract missing from --help: {token!r}"
    assert "never what the agent claimed" in run_help
    assert "verify that against the world" in run_help


# --- the drive policy step (extracted from EntitySession.open so it is TESTABLE) ---------------
#
# NOTE worth keeping: no test in this suite runs the real `EntitySession.open` — it is stubbed
# everywhere — so its body had NO unit coverage, and two mutations against the cred-floor wiring
# survived a full mutation pass because of it. The policy decision is extracted precisely so the
# security-relevant half can be pinned without a live session.

class _Cfg:
    def __init__(self, deny):
        self.deny_standard_creds = deny


def test_apply_drive_policy_binds_the_fork_safe_channel(monkeypatch):
    """Bind WITHOUT resolve and bash gets the wrong floor; resolve WITHOUT bind and the FILE
    EDITOR does — and the file editor is the `view` path. Pin that the bind actually happens."""
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV, current_drive_mode
    from levain.session import _apply_drive_policy

    monkeypatch.delenv(LEVAIN_DRIVE_MODE_ENV, raising=False)
    _apply_drive_policy(_Cfg(None), "interactive")
    assert current_drive_mode() == "interactive"
    _apply_drive_policy(_Cfg(None), "unattended")
    assert current_drive_mode() == "unattended"


def test_apply_drive_policy_resolves_the_floor_from_the_DRIVE_not_the_raw_value(monkeypatch):
    """`deny_standard_creds` is a TRI-STATE whose None means "derive". Reading it raw treats an
    undeclared entity as opted-OUT — the exact hole this closes."""
    from levain.session import _apply_drive_policy

    import os

    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    def fresh(cfg, mode):
        # Each assertion is a SEPARATE process in reality; bind_drive_mode now refuses a rebind
        # that would widen the floor, so clear the binding between them rather than weakening the
        # guard to suit the test.
        os.environ.pop(LEVAIN_DRIVE_MODE_ENV, None)
        return _apply_drive_policy(cfg, mode)

    assert fresh(_Cfg(None), "unattended") is True     # absent + seat → DENY
    assert fresh(_Cfg(None), "headless") is False      # absent + human → allow
    assert fresh(_Cfg(False), "unattended") is False   # explicit opt-IN survives
    assert fresh(_Cfg(True), "interactive") is True    # explicit pin survives


def test_apply_drive_policy_handles_a_toolless_session(monkeypatch):
    """`cfg` is None when the session runs with --no-tools. It must still bind the mode (nothing
    else will) and must not crash resolving a floor for hands that do not exist."""
    from levain.firing.drive import current_drive_mode
    from levain.session import _apply_drive_policy

    assert _apply_drive_policy(None, "unattended") is True
    assert current_drive_mode() == "unattended"
