"""The question band's way back to an ENDED session (Q17 Q-1b): what ``levain ask`` records for a resume, and
``deliver_answer`` routing through the delivery module's shared ``route`` (resumed with the answer as its prompt,
settled by the resume's outcome; any other session left to its next hook)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

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


def test_ask_resume_meta_claude_code_from_env_and_transcript(tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    start = "/work/my.proj"
    tdir = config / "projects" / "-work-my-proj"
    tdir.mkdir(parents=True)
    (tdir / f"{SESSION}.jsonl").write_text(json.dumps({"cwd": start, "permissionMode": "plan"}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    meta = band.ask_resume_meta("claude_code", SESSION)
    assert (meta["invocation"], meta["permission_mode"], meta["cwd"], meta["config_dir"], meta["claude_pid"]) == \
        ("-p", "plan", start, str(config), os.getpid())
    assert meta["claude_started"] and meta["claude_flags"]
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


def test_ask_resume_meta_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(band, "_codex_ancestor", lambda: (4242, True))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert band.ask_resume_meta("codex", SESSION) == {"invocation": "-p", "codex_pid": 4242,
                                                       "cwd": str(tmp_path.resolve()), "config_dir": str(tmp_path)}


def test_levain_ask_records_the_resume_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(band, "ask_resume_meta", lambda agent, session: {"invocation": "-p", "cwd": "/w"})
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
