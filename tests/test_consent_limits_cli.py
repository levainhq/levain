"""Q13 session 3: ``levain limits`` (show / set / unset / extend), the cockpit panel and the doctor row."""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
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
from tests._h4 import h4_home


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
    return ConsentQueue(tmp_path / "home" / "consent", authority=authority, home=h4_home())


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
    assert code == 0 and "nothing was resumed" in lines[-1] and "when it next runs" in lines[-1]
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
    assert r.ok and not r.warn and "claude_code: calls 3,000" in r.detail and "[defaults]" in r.detail
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


# --- S3 L1/L2 round 1 ------------------------------------------------------------------------------------------------

def _late_allow(q, key, request_id):
    from levain.consent.record import RULING_NAMESPACE, ruling_message

    payload = q.ruling_payload(request_id, "allow", key.fp)
    return q.answer_late(request_id, payload, key.sign(ruling_message(payload), RULING_NAMESPACE))


def _halt_by_silence(q, cwd, session="s1"):
    for i in (1, 2):
        L.check("claude_code", {"tool_use_id": f"t{i}"}, q, session, "Read", {"p": i}, hold=2.0,
                notify=lambda _t: None, cwd=cwd)
    out = L.check("claude_code", {"tool_use_id": "t3"}, q, session, "Read", {"p": 3}, hold=0.4,
                  notify=lambda _t: None, cwd=cwd,
                  resume={"cwd": cwd, "permission_mode": "default", "invocation": "interactive", "claude_pid": 999999})
    assert out.stop
    return q.undecided_for("claude_code")[0].request.id


def _extend_id(q):
    conn = L._connect(q.directory)
    try:
        return conn.execute("SELECT request_id FROM requests WHERE purpose = 'extend' ORDER BY at DESC").fetchone()[0]
    finally:
        conn.close()


def test_extend_with_no_answer_gets_a_gap_and_a_late_allow_ruling_releases_at_the_next_call(tmp_path, keys,
                                                                                            authority, limited):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory, hold=0.4), lines.append, queue=q,
                         launch=lambda *a: pytest.fail("launched"), listing=lambda m: {})
    assert code == 1
    eid = _extend_id(q)
    assert q.get(eid)[1] == "undecided"
    gap = q.gap(eid)
    assert gap is not None and f"gap #{gap}" in lines[-1] and "levain consent rule" in lines[-1]
    _late_allow(q, keys["phone"], eid)                    # a late RULING is possible on it now
    assert L.snapshot(q.directory)[0]["halt"] is not None
    assert L.check("claude_code", {"tool_use_id": "t9"}, q, "s1", "Read", {"file_path": "/x/9"}, hold=1.0,
                   notify=lambda _t: None, cwd=str(tmp_path)).go
    assert L.snapshot(q.directory)[0]["halt"] is None and L.snapshot(q.directory)[0]["limits"]["calls"] == 4


def test_a_stale_allow_on_the_halting_request_starts_no_second_resume_after_a_cli_extend(tmp_path, keys, authority,
                                                                                          limited, monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    r1 = _halt_by_silence(q, str(work))
    launches: list = []
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude", "--resume", "s1"), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q,
                         launch=lambda a, c, e, lg: launches.append(a) or None, listing=lambda m: {},
                         running=lambda *a, **k: False)
    t.join()
    assert code == 0 and len(launches) == 1, lines
    _late_allow(q, keys["phone"], r1)                     # the old gap card, cleared later with an allow
    got = dv.plan(q, r1)
    assert got.route == "hook" and "already extended" in got.reason
    got = dv.deliver(q, r1, listing=lambda m: {}, running=lambda *a, **k: False,
                     launch=lambda a, c, e, lg: launches.append(a) or None)
    assert len(launches) == 1 and got.route == "hook"
    assert L.snapshot(q.directory)[0]["limits"]["calls"] == 4          # doubled once
    disp = q.get(r1)[0].display
    assert L.ruling_text("allow", q.gap(r1), disp, queue_dir=q.directory, request_id=r1) is None


def test_an_allow_whose_root_is_still_halted_is_planned_as_before(tmp_path, keys, authority, limited):
    q = _q(tmp_path, authority)
    r1 = _halt_by_silence(q, str(tmp_path))
    _late_allow(q, keys["phone"], r1)
    assert L.root_halted(q.directory, "claude_code", "s1") is True
    assert "already extended" not in dv.plan(q, r1).reason
    disp = q.get(r1)[0].display
    assert "EXTENDED" in L.ruling_text("allow", q.gap(r1), disp, queue_dir=q.directory, request_id=r1)


def _unreadable_store(tmp_path, how):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    if how == "corrupt":
        (qdir / L.DB_NAME).write_bytes(b"this is not a sqlite database at all " * 200)
    else:
        _count(qdir, 1)
        os.chmod(qdir, 0o755)
    return qdir


@pytest.mark.parametrize("how", ["corrupt", "mode"])
@pytest.mark.parametrize("argv", [["show"], ["show", "--json"], ["extend", "s1"]])
def test_an_unreadable_store_is_one_line_and_exit_1(tmp_path, how, argv):
    qdir = _unreadable_store(tmp_path, how)
    try:
        code, text = _run(argv, qdir)
    finally:
        os.chmod(qdir, 0o700)
    assert code == 1 and text.count("\n") == 1 and "levain limits" in text and "Traceback" not in text


def test_ruling_text_names_a_gap_only_when_there_is_one():
    disp = "levain_limit " + json.dumps(L.limit_call("claude_code", "s1", "calls", 0, 2, purpose="extend"))
    for ruling in ("allow", "deny"):
        text = L.ruling_text(ruling, None, disp)
        assert text and "gap" not in text and "None" not in text
        assert "(gap #3)" in L.ruling_text(ruling, 3, disp)


def test_extend_after_an_end_resumes_with_no_gap_none(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    _halt_by_end(q, keys["phone"], str(work))
    launches: list = []
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude",), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))
    t = _phone_signs(q, keys["phone"], "allow")
    C._cmd_extend(_args(q.directory), lambda s: None, queue=q, launch=lambda a, c, e, lg: launches.append(a) or None,
                  listing=lambda m: {}, running=lambda *a, **k: False)
    t.join()
    assert len(launches) == 1 and "None" not in launches[0][-1] and "EXTENDED" in launches[0][-1]


def test_extend_resumes_only_when_its_own_allow_lifted_the_halt(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    r1 = _halt_by_silence(q, str(work))
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude",), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))

    def phone():   # the old gap card is ruled allow first, then the extend request is allowed too
        for _ in range(200):
            time.sleep(0.05)
            for r in q.pending():
                if r.tool_name == L.LIMIT_TOOL:
                    _late_allow(q, keys["phone"], r1)
                    payload, sig = signed(q, r, keys["phone"], "allow")
                    q.answer(r.id, payload, sig)
                    return
    t = threading.Thread(target=phone, daemon=True)
    t.start()
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"),
                         listing=lambda m: {}, running=lambda *a, **k: False)
    t.join()
    text = "\n".join(lines)
    assert code == 0 and "another" in text, text
    assert L.snapshot(q.directory)[0]["halt"] is None and L.snapshot(q.directory)[0]["limits"]["calls"] == 4


def test_a_resume_that_raises_after_the_halt_is_lifted_says_extended_resume_failed(tmp_path, keys, authority, limited,
                                                                                   monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("route blew up")
    monkeypatch.setattr(dv, "route", boom)
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"),
                         listing=lambda m: {}, running=lambda *a, **k: False)
    t.join()
    assert code == 0 and "extended; resume failed: RuntimeError: route blew up" in lines[-1]
    assert L.snapshot(q.directory)[0]["halt"] is None


def test_a_launch_that_raises_is_reported_unknown_with_exit_4(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    _halt_by_end(q, keys["phone"], str(work))
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude",), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))

    def boom(*a):
        raise OSError("close failed after Popen")
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=boom, listing=lambda m: {},
                         running=lambda *a, **k: False)
    t.join()
    assert code == 4 and "may have started (unknown" in lines[-1] and "check before resuming by hand" in lines[-1]


def test_extend_resume_waits_on_the_band_session_lock(tmp_path, keys, authority, limited, monkeypatch):
    from levain.consent import band

    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    _halt_by_end(q, keys["phone"], str(work))
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude",), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))
    monkeypatch.setattr(band, "SESSION_LOCK_WAIT", 0.3)
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    with band._session_lock(q, "claude_code", "s1") as held:
        assert held
        code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"),
                             listing=lambda m: {}, running=lambda *a, **k: False)
    t.join()
    assert code == 0 and "held its lock" in lines[-1] and "nothing was resumed" in lines[-1]


def test_ctrl_c_during_the_hold_leaves_the_request_pending_exit_130(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(q, "await_decision", interrupted)
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"))
    eid = _extend_id(q)
    assert code == 130 and f"interrupted; request {eid} stays pending until its hold ends" in lines[-1]
    assert q.get(eid)[1] == "pending"


def test_set_refuses_an_oversized_limits_json_and_a_document_that_would_be_one(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    big = {"format": L.LIMITS_FORMAT, "default": {}, "pad": "x" * (L.LIMITS_MAX + 1)}
    (qdir / L.LIMITS_NAME).write_text(json.dumps(big))
    code, text = _run(["set", "calls", "5"], qdir)
    assert code == 1 and "KiB" in text
    with pytest.raises(ValueError, match="KiB"):
        C._write_doc(tmp_path / "other" / L.LIMITS_NAME, big)
    assert not (tmp_path / "other" / L.LIMITS_NAME).exists()


def test_set_caps_values_and_stores_the_typed_whole_number_exactly(tmp_path):
    qdir = tmp_path / "consent"
    assert _run(["set", "fresh_tokens", "999999999999"], qdir)[0] == 0
    assert json.loads((qdir / L.LIMITS_NAME).read_text())["default"]["fresh_tokens"] == 999999999999
    assert _run(["set", "fresh_tokens", str(10 ** 12)], qdir)[0] == 0
    for kind, value in [("fresh_tokens", str(10 ** 12 + 1)), ("calls", "1e3"), ("active_hours", "10001"),
                        ("repeat", "9" * 30)]:
        code, text = _run(["set", kind, value], qdir)
        assert code == 1 and "at most" in text, (kind, value)
    assert _run(["set", "active_hours", "10000"], qdir)[0] == 0


def test_no_subcommand_names_where_queue_dir_goes(tmp_path):
    code, text = _run([], tmp_path / "consent")
    assert code == 2 and "levain limits --queue-dir D <cmd>" in text


def test_an_unreadable_consent_queue_before_filing_is_one_line_and_files_nothing(tmp_path, keys, authority, limited,
                                                                                monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))
    before = len(q.pending())

    def broken(*_a, **_k):
        raise sqlite3.DatabaseError("file is not a database")
    monkeypatch.setattr(q, "resume_meta", broken)
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"))
    assert code == 1 and "refused, nothing was filed: DatabaseError" in lines[-1]
    assert len(q.pending()) == before


def test_a_queue_failure_while_waiting_says_the_request_was_filed(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(q, "await_decision", broken)
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"))
    eid = _extend_id(q)
    assert code == 1 and f"request {eid} was filed, but its answer could not be read" in lines[-1]


def test_ctrl_c_after_the_answer_is_exit_130_not_a_traceback(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(L, "verified_allow", interrupted)
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"))
    t.join()
    assert code == 130 and "interrupted after the answer" in lines[-1]


def test_ctrl_c_during_the_launch_is_reported_unknown_with_exit_4(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    work = tmp_path / "w"
    work.mkdir()
    _halt_by_end(q, keys["phone"], str(work))
    monkeypatch.setattr(dv, "route", lambda *a, **k: ("resumed", "ended", ("claude",), str(work)))
    monkeypatch.setattr("levain.consent.band._claude_facts", lambda *_a: (str(work), "default"))

    def interrupted(*a):
        raise KeyboardInterrupt
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=interrupted, listing=lambda m: {},
                         running=lambda *a, **k: False)
    t.join()
    assert code == 4 and "may have started (unknown: KeyboardInterrupt" in lines[-1]


def test_extended_by_never_creates_the_store(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    with pytest.raises(sqlite3.Error):
        L.extended_by(qdir, "r1")
    assert not (qdir / L.DB_NAME).exists()


def test_ctrl_c_during_the_ping_leaves_the_request_pending_exit_130(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(C.claude_code, "notify", interrupted)
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q, launch=lambda *a: pytest.fail("launched"))
    eid = _extend_id(q)
    assert code == 130 and f"interrupted; request {eid} stays pending" in lines[-1]


def test_ctrl_c_before_the_resume_starts_says_extended_and_exit_130(tmp_path, keys, authority, limited, monkeypatch):
    q = _q(tmp_path, authority)
    _halt_by_end(q, keys["phone"], str(tmp_path))

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(C, "_resume", interrupted)
    t = _phone_signs(q, keys["phone"], "allow")
    lines: list[str] = []
    code = C._cmd_extend(_args(q.directory), lines.append, queue=q)
    t.join()
    assert code == 130 and "extended; the resume was interrupted: check whether" in lines[-1]
    assert L.snapshot(q.directory)[0]["halt"] is None


def test_ctrl_c_anywhere_in_extend_is_exit_130_never_a_traceback(tmp_path, monkeypatch):
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(C, "_find_halt", interrupted)
    out = io.StringIO()
    code = C.run(argparse.Namespace(limits_cmd="extend", queue_dir=tmp_path, session="s1", agent=None, hold=1.0), out)
    assert code == 130 and "`levain consent pending` whether a request was filed" in out.getvalue()
