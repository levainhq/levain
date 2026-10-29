"""The question band's way back to an ENDED session (Q17 Q-1b): what ``levain ask`` records for a resume, and
``deliver_answer`` routing through the delivery module's shared ``route`` (resumed with the answer as its prompt,
settled by the resume's outcome; any other session left to its next hook)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from levain.cli import main as cli_main
from levain.consent import band
from levain.consent import delivery as dv
from levain.consent.questions import QuestionStore

SESSION = "ceedbaa9-2e6d-433f-b374-0ad96dd5f62d"


def _answered(tmp_path, *, agent="claude_code", session=SESSION, resume=None, option="a"):
    store = QuestionStore(tmp_path / "consent")
    q = store.file(agent=agent, agent_session_id=session, what="merge?", options=[("a", "yes"), ("b", "no")], rec="a",
                   blocks="the merge", cwd="/work/proj", resume=resume)
    store.answer(q.id, option=option, channel="cli")
    return store, q


def test_transcript_facts_reads_the_first_cwd_and_the_last_mode(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [{"type": "summary"}, {"cwd": "/work/proj", "permissionMode": "default"}, {"cwd": "/work/proj/sub"},
             "not json", {"permissionMode": "acceptEdits"}, {"other": 1}]
    t.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    assert band.transcript_facts(t) == ("/work/proj", "acceptEdits")
    assert band.transcript_facts(tmp_path / "missing.jsonl") == (None, None)


@pytest.fixture(autouse=True)
def _no_process_tree(monkeypatch):
    """No test reads this run's own process tree (it runs under an agent)."""
    monkeypatch.setattr(band, "_ancestors", lambda: iter(()))


def _cc_transcript(tmp_path, start="/work/my.proj", mode="plan"):
    config = tmp_path / "cfg"
    tdir = config / "projects" / "-work-my-proj"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{SESSION}.jsonl").write_text(json.dumps({"cwd": start, "permissionMode": mode}) + "\n")
    return config, tdir


def test_ask_resume_meta_claude_code_from_env_and_transcript(tmp_path, monkeypatch):
    config, tdir = _cc_transcript(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(1111, ["zsh", "-c", "levain ask"]),
                                                          (os.getpid(), ["claude", "-p", "--model", "x"])]))
    meta = band.ask_resume_meta("claude_code", SESSION)
    assert (meta["invocation"], meta["permission_mode"], meta["cwd"], meta["config_dir"], meta["claude_pid"]) == \
        ("-p", "plan", "/work/my.proj", str(config), os.getpid())
    assert meta["claude_started"] and meta["claude_flags"]
    # the pid is the nearest claude ANCESTOR, whatever CLAUDE_PID says (a nested claude inherits its starter's)
    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert band.ask_resume_meta("claude_code", SESSION)["claude_pid"] == os.getpid()
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(1111, ["zsh"])]))
    assert band.ask_resume_meta("claude_code", SESSION)["claude_pid"] is None
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(os.getpid(), ["claude", "-p"])]))
    # a start directory that does not name the transcript's directory is not trusted
    (tdir / f"{SESSION}.jsonl").write_text(json.dumps({"cwd": "/elsewhere", "permissionMode": "plan"}) + "\n")
    assert band.ask_resume_meta("claude_code", SESSION)["cwd"] is None
    # two transcripts for one id: neither is read
    other = config / "projects" / "-x"
    other.mkdir()
    (other / f"{SESSION}.jsonl").write_text(json.dumps({"cwd": "/x", "permissionMode": "plan"}) + "\n")
    meta = band.ask_resume_meta("claude_code", SESSION)
    assert meta["cwd"] is None and meta["permission_mode"] is None
    assert band.ask_resume_meta("claude_code", "not-a-uuid") is None
    assert band.ask_resume_meta("claude_code", f"{SESSION}#agent:x") is None


def test_ask_resume_meta_is_only_for_the_callers_own_session(monkeypatch):
    other = "aaaaaaaa-2e6d-433f-b374-0ad96dd5f62d"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION)
    assert band.ask_resume_meta("claude_code", other) is None
    assert band.ask_resume_meta("codex", other) is None


def test_ask_resume_meta_codex_from_its_rollout(monkeypatch, tmp_path):
    chome = tmp_path / "codex"
    day = chome / "sessions" / "2026" / "10" / "18"
    day.mkdir(parents=True)
    (day / f"rollout-2026-10-18T22-00-00-{SESSION}.jsonl").write_text(json.dumps(
        {"type": "session_meta", "payload": {"id": SESSION, "cwd": "/work/start"}}) + "\n{}\n")
    monkeypatch.setenv("CODEX_HOME", str(chome))
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION)
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(10, ["bash", "-lc", "x"]), (4242, ["codex", "exec", "go"])]))
    assert band.ask_resume_meta("codex", SESSION) == {"invocation": "-p", "codex_pid": 4242, "cwd": "/work/start",
                                                       "config_dir": str(chome), "codex_flags": "codex"}
    # an app-server outlives its threads: no pid, so the thread is never judged ended by it
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(4242, ["codex", "app-server"])]))
    assert band.ask_resume_meta("codex", SESSION)["codex_pid"] is None
    # a rollout whose record names another thread is not read
    assert band.codex_rollout_cwd(chome, "bbbbbbbb-2e6d-433f-b374-0ad96dd5f62d") is None


def test_the_nearest_agent_ancestor_names_the_agent(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CODEX_THREAD_ID", "t-1")
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(5, ["bash"]), (6, ["/opt/bin/codex", "exec"]),
                                                          (7, ["claude"])]))
    assert band._agent_and_session(None, None) == ("codex", "t-1")


def test_levain_ask_records_the_resume_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(band, "ask_resume_meta", lambda agent, session, own=None: {"invocation": "-p", "cwd": "/w"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert cli_main(["ask", "--what", "q?", "--option", "a=x", "--option", "b=y", "--rec", "a", "--blocks", "z",
                     "--queue-dir", str(tmp_path / "consent")]) == 0
    assert QuestionStore(tmp_path / "consent").resume_meta(1) == {"invocation": "-p", "cwd": "/w"}


def test_a_broken_resume_reader_never_refuses_the_question(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("ps gone")

    monkeypatch.setattr(band, "ask_resume_meta", boom)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert cli_main(["ask", "--what", "q?", "--option", "a=x", "--option", "b=y", "--rec", "a", "--blocks", "z",
                     "--queue-dir", str(tmp_path / "consent")]) == 0
    assert QuestionStore(tmp_path / "consent").resume_meta(1) is None


def test_route_names_its_text(tmp_path):
    busy = lambda meta: {SESSION: {"status": "busy", "kind": "interactive"}}  # noqa: E731
    assert dv.route("claude_code", SESSION, {}, listing=busy)[1] == "the session is working; the ruling rides its next step"
    assert dv.route("claude_code", SESSION, {}, noun="answer", listing=busy)[1] == \
        "the session is working; the answer rides its next step"
    assert dv.route("codex", SESSION, {}, noun="answer")[:2] == \
        ("hook", "no resume record was kept for this session; the answer lands at its next step")


def test_deliver_answer_resumes_an_ended_session_with_the_answer_as_its_prompt(tmp_path, monkeypatch):
    store, q = _answered(tmp_path)
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/claude", "-p", f"--resume={SESSION}"),
                                                      str(tmp_path)))
    seen = []
    got = band.deliver_answer(store, q.id, launch=lambda argv, cwd, env, log: seen.append((argv, cwd)) or None)
    assert got.route == "resumed" and got.rulings == (q.id,)
    prompt = seen[0][0][-1]
    assert prompt.splitlines() == [band.answer_line(store.get(q.id)), band.ANSWER_TAIL, band.QUOTED_NOTE]
    assert store.delivery_state(q.id)[0] == "delivered"
    assert band.deliver_answer(store, q.id).route == "delivered"
    assert band.question_context(store.directory, "claude_code", SESSION) is None      # never twice


def test_a_resume_that_fails_leaves_the_answer_for_the_next_hook(tmp_path, monkeypatch):
    store, q = _answered(tmp_path)
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/claude",), str(tmp_path)))
    got = band.deliver_answer(store, q.id, launch=lambda *a: "it exited 1")
    assert got.route == "failed" and "stays deliverable" in got.reason
    assert store.delivery_state(q.id)[0] == "failed"
    assert "question #1" in band.question_context(store.directory, "claude_code", SESSION)
    assert store.delivery_state(q.id)[0] == "delivered"


def test_a_launch_that_raises_leaves_it_unconfirmed(tmp_path, monkeypatch):
    store, q = _answered(tmp_path)
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/claude",), str(tmp_path)))

    def raises(*a):
        raise RuntimeError("after the spawn")

    try:
        band.deliver_answer(store, q.id, launch=raises)
    except RuntimeError:
        pass
    assert store.delivery_state(q.id)[0] == "unconfirmed"
    assert band.deliver_answer(store, q.id).route == "unconfirmed"
    assert band.question_context(store.directory, "claude_code", SESSION) is None


def test_deliver_answer_leaves_a_live_or_unknown_session_to_its_hook(tmp_path, monkeypatch):
    store, q = _answered(tmp_path)
    got = band.deliver_answer(store, q.id, listing=lambda meta: {SESSION: {"status": "idle", "kind": "interactive"}})
    assert (got.route, got.reason) == ("hook", "the session is open in a terminal; the answer lands at its next step")
    assert store.delivery_state(q.id)[0] == "undelivered"
    assert band.deliver_answer(store, "#9").route == "none"


def test_deliver_answer_none_for_no_answer_or_no_session(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = store.file(agent="codex", agent_session_id=band.NO_SESSION, what="q?", options=[("a", "x"), ("b", "y")],
                   rec="a", blocks="z", cwd="/w")
    assert band.deliver_answer(store, q.id).route == "none"
    store.answer(q.id, option="a", channel="cli")
    assert "reaches no agent" in band.deliver_answer(store, q.id).reason


def test_deliver_answers_and_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: False)   # a human terminal, whatever runs this suite
    store, q = _answered(tmp_path, agent="codex")
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert cli_main(["question", "deliver", "--queue-dir", str(store.directory)]) == 0
    assert f"{q.id}: hook: no resume record was kept" in out.getvalue()
    q2 = store.file(agent="codex", agent_session_id=SESSION, what="q2?", options=[("a", "x"), ("b", "y")], rec="a",
                    blocks="z", cwd="/w")
    out.truncate(0)
    assert cli_main(["question", "answer", str(q2.n), "--option", "b", "--no-deliver", "--queue-dir",
                     str(store.directory)]) == 0
    assert "delivery" not in out.getvalue()


def test_a_resume_needs_the_transcript_to_still_show_the_recorded_mode(tmp_path, monkeypatch):
    config, tdir = _cc_transcript(tmp_path, mode="bypassPermissions")
    meta = {"invocation": "-p", "cwd": "/work/my.proj", "permission_mode": "bypassPermissions",
            "config_dir": str(config)}
    store, q = _answered(tmp_path, resume=meta)
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/claude",), str(tmp_path)))
    (tdir / f"{SESSION}.jsonl").write_text(
        json.dumps({"cwd": "/work/my.proj", "permissionMode": "bypassPermissions"}) + "\n"
        + json.dumps({"permissionMode": "default"}) + "\n")                # he dropped the mode before it ended
    got = band.deliver_answer(store, q.id, launch=lambda *a: None)
    assert got.route == "hook" and "no longer shows" in got.reason
    assert store.delivery_state(q.id)[0] == "undelivered"


def test_the_real_route_refuses_untrusted_meta_for_an_answer(tmp_path):
    """No seam patched: an ended Claude Code session whose recorded start directory does not exist is not resumed."""
    store, q = _answered(tmp_path, resume={"invocation": "-p", "cwd": str(tmp_path / "gone"), "claude_pid": 999999,
                                           "permission_mode": "default", "claude_flags": "claude"})
    got = band.deliver_answer(store, q.id, listing=lambda meta: {}, running=lambda pid, started=None: False)
    assert got.route == "hook" and "is not an existing absolute directory" in got.reason
    assert got.reason.endswith("the answer lands at its next step")


def test_deliver_answers_tries_one_resume_per_session(tmp_path, monkeypatch):
    store, q = _answered(tmp_path)
    q2 = store.file(agent="claude_code", agent_session_id=SESSION, what="q2?", options=[("a", "x"), ("b", "y")],
                    rec="a", blocks="z", cwd="/w")
    store.answer(q2.id, option="a", channel="cli")
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/claude",), str(tmp_path)))
    launches = []
    got = band.deliver_answers(store, launch=lambda *a: launches.append(a) or "it exited 1")
    assert len(launches) == 1 and [d.route for d in got] == ["failed"]


def test_the_cli_says_the_answer_stands_when_delivery_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: False)   # a human terminal, whatever runs this suite
    store = QuestionStore(tmp_path / "consent")
    store.file(agent="codex", agent_session_id=SESSION, what="q?", options=[("a", "x"), ("b", "y")], rec="a",
               blocks="z", cwd="/w")

    def boom(*a, **k):
        raise OSError("listing died")

    monkeypatch.setattr(band, "deliver_answer", boom)
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert cli_main(["question", "answer", "1", "--option", "a", "--queue-dir", str(store.directory)]) == 0
    assert "the answer is recorded; its delivery raised OSError" in out.getvalue()
    assert store.get(1).answer is not None


def test_l3r2_codex_subcommand_after_a_top_level_option(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(77, ["codex", "-m", "o3", "exec", "go"])]))
    meta = band.ask_resume_meta("codex", SESSION)
    assert (meta["invocation"], meta["codex_pid"]) == ("-p", 77)
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(77, ["codex", "-c", "k=v", "mcp-server"])]))
    assert band.ask_resume_meta("codex", SESSION)["codex_pid"] is None


def test_l3r2_a_long_first_rollout_line_still_gives_its_cwd(tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "18"
    day.mkdir(parents=True)
    (day / f"rollout-x-{SESSION}.jsonl").write_text(json.dumps(
        {"type": "session_meta", "payload": {"id": SESSION, "cwd": "/w", "base_instructions": "x" * 200_000}}) + "\n")
    assert band.codex_rollout_cwd(tmp_path, SESSION) == "/w"


def test_l3r2_a_process_naming_the_session_blocks_a_resume(tmp_path, monkeypatch):
    meta = {"invocation": "-p", "cwd": str(tmp_path), "codex_pid": 999999, "codex_flags": "codex"}
    monkeypatch.setattr(dv, "_resume_argv", lambda *a: ["/bin/codex", "exec", "resume", SESSION])   # CI has no codex
    monkeypatch.setattr(dv, "codex_sandbox_args", lambda meta, thread: ["-s", "read-only"])
    got = dv.route("codex", SESSION, meta, noun="answer", running=lambda pid, started=None: False,
                   named=lambda s: True)
    assert got[0] == "hook" and "a process resuming this session may be running" in got[1]
    got = dv.route("codex", SESSION, meta, noun="answer", running=lambda pid, started=None: False,
                   named=lambda s: None)
    assert got[0] == "hook"                                   # cannot tell: no resume
    got = dv.route("codex", SESSION, meta, noun="answer", running=lambda pid, started=None: False,
                   named=lambda s: False)
    assert got[0] == "resumed"
    assert dv.session_named_running("not-a-uuid") is None
    # a False is only true of the list it read (a dev seat's own claude -c reads None): read a list with one
    # unrelated process
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, *a, **k: dv.subprocess.CompletedProcess(
        argv, 0, "1 /bin/zsh -l\n", ""))
    assert dv.session_named_running(SESSION) is False


def test_l3r2_an_answer_without_meta_borrows_the_sessions_newest(tmp_path, monkeypatch):
    store = QuestionStore(tmp_path / "consent")
    for resume in (None, {"invocation": "-p", "cwd": "/w2"}):
        q = store.file(agent="codex", agent_session_id=SESSION, what="q?", options=[("a", "x"), ("b", "y")], rec="a",
                       blocks="z", cwd="/w", resume=resume)
    seen = {}
    monkeypatch.setattr(dv, "route", lambda agent, session, meta, **k: seen.setdefault("meta", meta) and
                        ("hook", "x", (), None))
    store.answer(1, option="a", channel="cli")
    band.deliver_answer(store, 1)
    assert seen["meta"] == {"invocation": "-p", "cwd": "/w2"}


def test_l3r2_non_bmp_controls_round_trip(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = store.file(agent="codex", agent_session_id=SESSION, what="q?", options=[("a", "x"), ("b", "y")], rec="a",
                   blocks="z", cwd="/w")
    words = "flag \U000e0067\U000e0062 end \U0010fffd"
    store.answer(q.id, text=words, channel="cli")
    line = band.answer_line(store.get(q.id))
    assert json.loads(line.split("in words: ", 1)[1]) == words
    assert "\U000e0067" not in line


def test_l3r2_nineteen_digit_numbers_and_the_agent_names():
    from levain.consent.questions import _parse_ref

    assert _parse_ref("9223372036854775807") == ("n", 2 ** 63 - 1)
    assert _parse_ref("9223372036854775808") is None
    assert set(band._AGENT_NAMES) == set(band._agents())


def test_l3r3_two_deliveries_of_one_session_start_one_resume(tmp_path, monkeypatch):
    """codex r3 #2: answer B arriving between A's lease and A's launch passed the route too (A's resume not started
    yet) and leased itself: two resumes. The per-session lock makes B route after A's resume started."""
    import threading
    import time

    store = QuestionStore(tmp_path / "consent")
    for n in (1, 2):
        store.file(agent="codex", agent_session_id=SESSION, what=f"q{n}?", options=[("a", "x"), ("b", "y")],
                   rec="a", blocks="z", cwd="/w")
    store.answer(1, option="a", channel="phone")
    started = threading.Event()
    leased_a = threading.Event()

    def route(*a, **k):
        if started.is_set():                     # A's resume is running: its process names the session
            return "hook", "a process naming this session may be running", (), None
        return "resumed", "ended", ("/bin/codex", "exec", "resume", SESSION), str(tmp_path)

    monkeypatch.setattr(dv, "route", route)
    launches = []

    def launch(*a):
        leased_a.set()                           # A has leased; B's answer lands now, before A's launch returns
        time.sleep(0.5)
        launches.append(a)
        started.set()

    results = []
    first = threading.Thread(target=lambda: results.append(band.deliver_answer(store, 1, launch=launch)))
    first.start()
    assert leased_a.wait(5)
    store.answer(2, option="a", channel="phone")
    second = threading.Thread(target=lambda: results.append(band.deliver_answer(store, 2, launch=launch)))
    second.start()
    first.join(10)
    second.join(10)
    assert len(launches) == 1
    assert sorted(r.route for r in results) == ["hook", "resumed"]


def test_l3r4_a_hung_lock_holder_never_blocks_a_delivery(tmp_path, monkeypatch):
    """codex r4: flock(LOCK_EX) waited without a bound, so a hung holder blocked every later delivery of its session.
    Past SESSION_LOCK_WAIT the delivery starts nothing and routes hook; the answer stays deliverable."""
    import fcntl
    import hashlib
    import time

    store = QuestionStore(tmp_path / "consent")
    store.file(agent="codex", agent_session_id=SESSION, what="q?", options=[("a", "x"), ("b", "y")], rec="a",
               blocks="z", cwd="/w")
    store.answer(1, option="a", channel="phone")
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("/bin/codex", "exec", "resume", SESSION),
                                                      str(tmp_path)))
    monkeypatch.setattr(band, "SESSION_LOCK_WAIT", 0.3)
    folder = tmp_path / "consent" / "resumes"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"codex\0{SESSION}".encode()).hexdigest()[:32]
    holder = os.open(folder / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)                              # the hung holder
    launches = []
    try:
        t0 = time.monotonic()
        got = band.deliver_answer(store, 1, launch=lambda *a: launches.append(a))
        assert time.monotonic() - t0 < 5
        assert got.route == "hook" and "held the lock" in got.reason
        assert launches == [] and store.delivery_state(store.get(1).id)[0] not in ("delivered", "unconfirmed")
    finally:
        os.close(holder)
    got = band.deliver_answer(store, 1, launch=lambda *a: launches.append(a))   # freed: it delivers
    assert got.route == "resumed" and len(launches) == 1


def test_a_session_the_caller_knows_as_its_own_records_resume_meta_though_its_environment_names_none(monkeypatch):
    """The MCP door's per-call _meta (QMCP R-q19 amended): Codex gives an MCP child no CODEX_THREAD_ID, so the thread
    the call names is passed as ``own``; with neither, a named session is another's and nothing is recorded."""
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(41, ["/opt/bin/codex", "exec"])]))
    assert band.ask_resume_meta("codex", SESSION) is None
    meta = band.ask_resume_meta("codex", SESSION, own=SESSION)
    assert meta is not None and meta["invocation"] == "-p" and meta["codex_pid"] == 41
    assert band.ask_resume_meta("codex", SESSION, own="another-thread") is None
