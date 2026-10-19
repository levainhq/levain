"""Q13 session 3: ``levain limits`` (show / set / unset / extend), the cockpit panel and the doctor row."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

import pytest

from levain import doctor
from levain.consent import delivery as dv
from levain.consent import limits as L
from levain.consent import limits_cli as C
from levain.consent.queue import AnswerRefused, ConsentQueue
from tests.test_consent_queue import authority, keys, signed  # noqa: F401 - the shared fixtures


def _run(argv: list[str], qdir: Path) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    C.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["limits", "--queue-dir", str(qdir), *argv])
    lines: list[str] = []

    class Out:
        def write(self, s):
            lines.append(s)

        def flush(self):
            pass
    code = C.run(args, Out())
    return code, "".join(lines)


def _count(qdir, n, *, session="s1", agent="claude_code", limits=None):
    v = None
    for i in range(n):
        v = L.count_call(qdir, agent, session, {"tool_use_id": f"{session}-{i}"}, tool_name="Read",
                         tool_input={"p": f"/x/{session}/{i}"}, limits=limits)
    return v


# --- set / unset / show ----------------------------------------------------------------------------------------------

def test_set_writes_a_0600_document_and_load_limits_reads_it(tmp_path):
    qdir = tmp_path / "consent"
    assert _run(["set", "calls", "50"], qdir)[0] == 0
    assert _run(["set", "active_hours", "1.5", "--agent", "codex"], qdir)[0] == 0
    path = qdir / L.LIMITS_NAME
    assert (path.stat().st_mode & 0o777) == 0o600
    doc = json.loads(path.read_text())
    assert doc == {"format": L.LIMITS_FORMAT, "default": {"calls": 50}, "codex": {"active_hours": 1.5}}
    assert L.load_limits(qdir, "claude_code")[0]["calls"] == 50
    lim, src = L.load_limits(qdir, "codex")
    assert lim["calls"] == 50 and lim["active_hours"] == 1.5 and src == L.LIMITS_NAME
    assert not list(qdir.glob(".limits-*"))             # no temp file left


@pytest.mark.parametrize("kind,value", [("calls", "0"), ("calls", "-3"), ("calls", "2.5"), ("repeat", "1e400"),
                                        ("calls", "nan"), ("calls", "inf"), ("calls", "abc"), ("active_hours", "0")])
def test_set_refuses_bad_values_and_changes_nothing(tmp_path, kind, value):
    qdir = tmp_path / "consent"
    code, text = _run(["set", kind, value], qdir)
    assert code == 1 and "refused" in text
    assert not (qdir / L.LIMITS_NAME).exists()


def test_set_refuses_a_symlinked_limits_json(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    victim = tmp_path / "victim.json"
    victim.write_text("keep")
    (qdir / L.LIMITS_NAME).symlink_to(victim)
    code, text = _run(["set", "calls", "5"], qdir)
    assert code == 1 and "not a regular file" in text
    assert victim.read_text() == "keep"
    assert _run(["unset", "calls"], qdir)[0] == 1


def test_set_refuses_a_foreign_document_and_keeps_other_keys(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    (qdir / L.LIMITS_NAME).write_text(json.dumps({"format": "other/1"}))
    assert _run(["set", "calls", "5"], qdir)[0] == 1
    (qdir / L.LIMITS_NAME).write_text(json.dumps({"format": L.LIMITS_FORMAT, "codex": {"repeat": 4}}))
    assert _run(["set", "calls", "5"], qdir)[0] == 0
    assert json.loads((qdir / L.LIMITS_NAME).read_text())["codex"] == {"repeat": 4}


def test_unset_removes_the_key_and_an_empty_block(tmp_path):
    qdir = tmp_path / "consent"
    _run(["set", "calls", "5", "--agent", "codex"], qdir)
    assert _run(["unset", "calls", "--agent", "codex"], qdir)[0] == 0
    assert json.loads((qdir / L.LIMITS_NAME).read_text()) == {"format": L.LIMITS_FORMAT}
    code, text = _run(["unset", "calls", "--agent", "codex"], qdir)
    assert code == 0 and "nothing changed" in text


def test_show_text_lists_limits_sources_and_sessions(tmp_path):
    qdir = tmp_path / "consent"
    _run(["set", "calls", "5"], qdir)
    _count(qdir, 3, limits=dict(L.DEFAULTS, calls=2))
    code, text = _run(["show"], qdir)
    assert code == 0
    assert "claude_code: limits.json" in text and "codex: limits.json" in text
    assert "calls              5" in text and "1 counted session(s)" in text
    assert "calls 3/5" in text and "HALTED" not in text


def test_show_without_a_store_says_so_and_names_defaults(tmp_path):
    code, text = _run(["show", "--agent", "codex"], tmp_path / "consent")
    assert code == 0 and "codex: defaults" in text and "no session counted yet" in text


def test_show_json_is_the_panel(tmp_path):
    qdir = tmp_path / "consent"
    _count(qdir, 2)
    code, text = _run(["show", "--json"], qdir)
    assert code == 0 and json.loads(text)["sessions"][0]["session"] == "s1"


# --- panel -----------------------------------------------------------------------------------------------------------

def test_panel_shape_and_status_thresholds(tmp_path):
    qdir = tmp_path / "consent"
    _run(["set", "calls", "10"], qdir)
    lim = None
    _count(qdir, 7, session="ok", limits=lim)
    _count(qdir, 8, session="amber", limits=lim)
    _count(qdir, 11, session="over", limits=lim)
    pan = L.panel(qdir)
    assert set(pan) == {"as_of", "sessions"} and pan["as_of"].endswith("Z")
    by = {s["session"]: s for s in pan["sessions"]}
    assert set(by["ok"]) == {"agent", "session", "last_at", "halted", "counting", "metrics"}
    calls = {k: next(m for m in by[k]["metrics"] if m["key"] == "calls") for k in by}
    assert calls["ok"]["status"] == "ok"
    assert calls["amber"]["status"] == "amber" and calls["amber"]["value"] == 8       # 0.8 * 10, inclusive
    assert calls["over"]["status"] == "over"
    m = by["ok"]["metrics"][0]
    assert set(m) == {"key", "label", "value", "limit", "status"} and m["label"] == "tool calls"
    assert [x["key"] for x in by["ok"]["metrics"]] == list(L.KINDS)
    assert by["ok"]["halted"] is None and by["ok"]["counting"] is True and by["ok"]["last_at"].endswith("Z")


def test_panel_repeat_is_over_at_its_limit_and_flags_not_counting(tmp_path):
    qdir = tmp_path / "consent"
    _run(["set", "repeat", "3"], qdir)
    for i in range(3):
        L.count_call(qdir, "codex", "r1", {"tool_use_id": f"a{i}"}, tool_name="Read", tool_input={"same": 1})
    (s,) = L.panel(qdir)["sessions"]
    assert next(m for m in s["metrics"] if m["key"] == "repeat")["status"] == "over"
    L._fail_write(L._fail_path(qdir, "codex", "r1"), 3, create=False)
    assert L.panel(qdir)["sessions"][0]["counting"] is False


def test_panel_empty_without_a_store(tmp_path):
    assert L.panel(tmp_path / "none")["sessions"] == []


# --- extend ----------------------------------------------------------------------------------------------------------

LIM = dict(L.DEFAULTS, calls=2)


def _q(tmp_path, authority):
    return ConsentQueue(tmp_path / "home" / "consent", authority=authority)


def _halt_by_end(q, key, cwd):
    """Cross the limit on s1 and answer End on the phone: the root is halted (with a recorded resume)."""
    def phone():
        for _ in range(200):
            time.sleep(0.05)
            for r in q.pending():
                if r.tool_name == L.LIMIT_TOOL:
                    payload, sig = signed(q, r, key, "deny")
                    try:
                        q.answer(r.id, payload, sig)
                    except AnswerRefused:
                        pass
                    return
    t = threading.Thread(target=phone, daemon=True)
    for i in (1, 2):
        L.check("claude_code", {"tool_use_id": f"t{i}"}, q, "s1", "Read", {"file_path": f"/x/{i}"}, hold=2.0,
                notify=lambda _t: None, cwd=cwd)
    t.start()
    out = L.check("claude_code", {"tool_use_id": "t3"}, q, "s1", "Read", {"file_path": "/x/3"}, hold=2.0,
                  notify=lambda _t: None, cwd=cwd, resume={"cwd": cwd, "permission_mode": "default",
                                                           "invocation": "interactive"})
    t.join()
    assert not out.go and out.stop


def _args(qdir, session="s1", agent=None, hold=5.0):
    return argparse.Namespace(queue_dir=qdir, session=session, agent=agent, hold=hold)


def _phone_signs(q, key, decision="allow"):
    def run():
        for _ in range(200):
            time.sleep(0.05)
            for r in q.pending():
                if r.tool_name == L.LIMIT_TOOL:
                    payload, sig = signed(q, r, key, decision)
                    try:
                        q.answer(r.id, payload, sig)
                    except AnswerRefused:
                        pass
                    return
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.fixture
def limited(monkeypatch):
    monkeypatch.setattr(L, "load_limits", lambda *_a, **_k: (dict(LIM), "test"))


def test_extend_refuses_a_root_that_is_not_halted(tmp_path, authority, limited):
    q = _q(tmp_path, authority)
    _count(q.directory, 1, limits=LIM)
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q)
    assert code == 1 and "not halted" in lines[0]
    assert q.pending() == []                      # nothing filed
    assert C._cmd_extend(_args(q.directory, session="never-seen"), lines.append, queue=q) == 1


def test_extend_end_to_end_releases_the_halt_and_resumes_with_the_extend_prompt(tmp_path, keys, authority, limited,
                                                                                 monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "work"
    work.mkdir()
    _halt_by_end(q, keys["phone"], str(work))
    assert L.snapshot(q.directory)[0]["halt"]["kind"] == "calls"
    seen: dict = {}

    def route(agent, session, meta, **kw):
        seen["route"] = (agent, session, dict(meta), kw.get("noun"))
        return "resumed", "ended", ("claude", "--resume", "s1", "-p"), str(work)

    def launch(argv, cwd, env, log):
        seen["launch"] = (list(argv), cwd, log)
        return None
    monkeypatch.setattr(dv, "route", route)
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=launch, listing=lambda m: {},
                         running=lambda *a, **k: False)
    t.join()
    text = "\n".join(lines)
    assert code == 0, text
    assert "levain consent answer" in text and "EXTENDED" in text and "resumed with the Extend" in text
    assert L.snapshot(q.directory)[0]["halt"] is None          # released
    assert L.snapshot(q.directory)[0]["limits"]["calls"] == 4  # doubled once
    argv, cwd, log = seen["launch"]
    assert argv[:4] == ["claude", "--resume", "s1", "-p"] and "EXTENDED this session's tool calls limit to 4" in argv[-1]
    assert seen["route"][0] == "claude_code" and seen["route"][3] == "extend" and seen["route"][2]["cwd"] == str(work)
    assert cwd == str(work) and log.parent.name == "resumes"
    # the halt is gone: the next call goes through under the doubled limit
    assert L.check("claude_code", {"tool_use_id": "t4"}, q, "s1", "Read", {"file_path": "/x/4"}, hold=1.0,
                   notify=lambda _t: None, cwd=str(work)).go


def test_extend_deny_changes_nothing(tmp_path, keys, authority, limited):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))
    t = _phone_signs(q, keys["phone"], "deny")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q,
                         launch=lambda *a: pytest.fail("launched"), listing=lambda m: {})
    t.join()
    assert code == 1 and "not extended" in lines[-1]
    assert L.snapshot(q.directory)[0]["halt"] is not None


def test_extend_with_no_answer_changes_nothing(tmp_path, keys, authority, limited):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory, hold=0.4), lines.append, queue=q,
                         launch=lambda *a: pytest.fail("launched"), listing=lambda m: {})
    assert code == 1 and "not extended" in lines[-1]
    assert L.snapshot(q.directory)[0]["halt"] is not None


def test_extend_without_a_resume_record_says_where_it_lands(tmp_path, keys, authority, limited):
    q = _q(tmp_path, authority)
    cwd = str(tmp_path)
    for i in (1, 2):
        L.check("claude_code", {"tool_use_id": f"t{i}"}, q, "s1", "Read", {"p": i}, hold=2.0,
                notify=lambda _t: None, cwd=cwd)
    t = _phone_signs(q, keys["phone"], "deny")
    L.check("claude_code", {"tool_use_id": "t3"}, q, "s1", "Read", {"p": 3}, hold=2.0, notify=lambda _t: None, cwd=cwd)
    t.join()
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q,
                         launch=lambda *a: pytest.fail("launched"), listing=lambda m: {},
                         running=lambda *a, **k: False)
    t.join()
    assert code == 0 and "not resumed" in lines[-1] and "next step" in lines[-1]
    assert L.snapshot(q.directory)[0]["halt"] is None


def test_extend_refuses_before_filing_when_no_key_can_answer(tmp_path, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _count(q.directory, 3, limits=LIM)
    conn = L._connect(q.directory)
    conn.execute("INSERT INTO halts (agent, root, kind, request_id, gap, at) VALUES ('claude_code','s1','calls','r',1,1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(C, "_signable", lambda settings: False)
    lines: list[str] = []
    assert C._cmd_extend(_args(q.directory), lines.append) == 1
    assert "NOT filed" in lines[0] and q.pending() == []


# --- doctor ----------------------------------------------------------------------------------------------------------

def test_doctor_row_reports_limits_sessions_and_halts(tmp_path, monkeypatch):
    qdir = tmp_path / "consent"
    monkeypatch.setattr("levain.install._consent_queue_dir", lambda: qdir)
    (r,) = doctor._check_consent_limits()
    assert r.ok and not r.warn and "claude_code: calls 3000" in r.detail and "[defaults]" in r.detail
    assert "0 session(s) counted, 0 halted" in r.detail
    _run(["set", "calls", "9"], qdir)
    _count(qdir, 3, limits=dict(L.DEFAULTS, calls=2))
    (r,) = doctor._check_consent_limits()
    assert "calls 9" in r.detail and "[limits.json]" in r.detail and "1 session(s) counted" in r.detail
    assert "NOT COUNTING" not in r.detail


def test_doctor_row_says_not_counting_when_a_failure_file_reads_nonzero(tmp_path, monkeypatch):
    qdir = tmp_path / "consent"
    monkeypatch.setattr("levain.install._consent_queue_dir", lambda: qdir)
    _count(qdir, 1)
    L._fail_write(L._fail_path(qdir, "claude_code", "s1"), 2, create=False)
    (r,) = doctor._check_consent_limits()
    assert r.ok and r.warn and "NOT COUNTING: s1" in r.detail and r.hint
