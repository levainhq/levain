"""A session's transcript (Q8 slice B): the bounded log reader and its token-gated route, a display never an authority."""
from __future__ import annotations

import json
import shlex
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit import session_log
from levain.cockpit.session_log import EVENTS_SHOWN, LOG_TAIL, read_session
from levain.consent.listing import read_listing
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_sessions import _put
from tests._h4 import h4_home

SID, SID_OTHER = "zq-q8-session-cc-7701", "zq-q8-session-cc-7702"
CWD = "/zq/q8 full/it's-here"
PROMPT = "zq-q8-prompt-marker please list the files"


def _stem(p: Path) -> str:
    return p.name[:-len(".json")]


def _line(kind: str, text: str, cut: int = 0, at: str = "2026-10-15T23:50:00Z") -> str:
    return json.dumps({"v": 1, "at": at, "kind": kind, "text": text, "cut": cut}) + "\n"


def _log(rec: Path, body: str | bytes) -> Path:
    p = rec.with_name(_stem(rec) + ".log.jsonl")
    p.write_bytes(body.encode() if isinstance(body, str) else body)
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _queue(tmp_path: Path, monkeypatch) -> ConsentQueue:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    return ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())


# ---- the reader -------------------------------------------------------------------------------------------------

def test_events_in_order_with_the_record_fields_and_a_quoted_resume(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    _log(rec, _line("prompt", PROMPT) + _line("tool", "Bash: ls -la") + _line("reply", "done", cut=12))
    body = read_session(root, _stem(rec), _now(), None)
    assert [e["kind"] for e in body["events"]] == ["prompt", "tool", "reply"]
    assert body["events"][2] == {"at": "2026-10-15T23:50:00Z", "kind": "reply", "text": "done", "cut": 12}
    assert body["agent"] == "claude_code" and body["workspace"] == CWD and body["skipped"] == 0
    assert set(body) == {"agent", "workspace", "first_seen", "last_seen", "resume", "events", "consent", "skipped", "context"}
    assert body["resume"] == f"cd -- {shlex.quote(CWD)} && claude --resume {shlex.quote(SID)}"
    assert shlex.split(body["resume"])[:3] == ["cd", "--", CWD]          # the space and the quote survive a shell
    cx = _put(root, "codex", SID, CWD, timedelta(minutes=2))
    assert read_session(root, _stem(cx), _now(), None)["resume"].endswith(f"&& codex resume {SID}")


def test_malformed_lines_are_skipped_and_counted(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    bad = [
        "{not json\n",
        json.dumps({"v": 2, "at": "2026-10-15T23:50:00Z", "kind": "prompt", "text": "x", "cut": 0}) + "\n",
        json.dumps({"v": 1, "at": "2026-10-15T23:50:00Z", "kind": "consent", "text": "x", "cut": 0}) + "\n",
        json.dumps({"v": 1, "at": "2026-10-15T23:50:00+00:00", "kind": "prompt", "text": "x", "cut": 0}) + "\n",
        json.dumps({"v": 1, "at": "2026-10-15T23:50:00Z", "kind": "prompt", "text": 7, "cut": 0}) + "\n",
        json.dumps({"v": 1, "at": "2026-10-15T23:50:00Z", "kind": "prompt", "text": "x", "cut": True}) + "\n",
        json.dumps({"v": 1, "at": "2026-10-15T23:50:00Z", "kind": "prompt", "text": "x", "cut": -1}) + "\n",
        "[1, 2]\n",
        json.dumps({"v": True, "at": "2026-10-15T23:50:00Z", "kind": "prompt", "text": "x", "cut": 0}) + "\n",
    ]
    _log(rec, _line("prompt", "kept") + "".join(bad) + "\n" + _line("reply", "kept too"))
    body = read_session(root, _stem(rec), _now(), None)
    assert [e["text"] for e in body["events"]] == ["kept", "kept too"]
    assert body["skipped"] == len(bad)                              # a blank line is not counted


def test_the_tail_cut_drops_the_split_first_line_uncounted_and_the_newest_300_are_returned(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    lines = [_line("tool", f"Bash: echo {i:05d} " + "y" * 900) for i in range(400)]   # ~370 KiB: past the tail
    log = _log(rec, "".join(lines))
    assert log.stat().st_size > LOG_TAIL
    body = read_session(root, _stem(rec), _now(), None)
    assert body["skipped"] == 0                                     # the split first line is not a malformed one
    assert len(body["events"]) <= EVENTS_SHOWN
    assert body["events"][-1]["text"].startswith("Bash: echo 00399")       # newest last
    nums = [int(e["text"][11:16]) for e in body["events"]]
    assert nums == sorted(nums) and nums == list(range(nums[0], 400))      # oldest first, contiguous, nothing torn
    # the cap: 350 small lines, all inside the tail, give the newest 300
    _log(rec, "".join(_line("prompt", f"p{i}") for i in range(350)))
    body = read_session(root, _stem(rec), _now(), None)
    assert len(body["events"]) == EVENTS_SHOWN and body["events"][0]["text"] == "p50"


def test_a_cut_landing_exactly_on_a_line_boundary_keeps_that_line(tmp_path, monkeypatch):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    first, second = _line("prompt", "first"), _line("prompt", "second")
    _log(rec, first + second)
    monkeypatch.setattr(session_log, "LOG_TAIL", len(second))      # the window starts exactly at "second"
    body = read_session(root, _stem(rec), _now(), None)
    assert [e["text"] for e in body["events"]] == ["second"] and body["skipped"] == 0


def test_an_unterminated_last_line_is_an_append_in_flight_not_a_malformed_line(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    _log(rec, _line("prompt", "whole") + _line("reply", "half written")[:20])
    body = read_session(root, _stem(rec), _now(), None)
    assert [e["text"] for e in body["events"]] == ["whole"] and body["skipped"] == 0


def test_a_missing_log_is_no_events_and_a_missing_or_foreign_record_is_none(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    body = read_session(root, _stem(rec), _now(), None)
    assert body["events"] == [] and body["skipped"] == 0
    assert read_session(root, "0" * 24, _now(), None) is None                 # no such record
    assert read_session(root, "../" + _stem(rec)[3:], _now(), None) is None   # not a stem
    assert read_session(root, _stem(rec).upper(), _now(), None) is None
    foreign = rec.with_name("a" * 24 + ".json")                                # a record under another session's name
    foreign.write_bytes(rec.read_bytes())
    assert read_session(root, "a" * 24, _now(), None) is None
    ahead = _put(root, "codex", SID_OTHER, CWD, timedelta(minutes=-30))       # last_seen past the skew: not ours
    assert read_session(root, _stem(ahead), _now(), None) is None


def test_a_symlinked_log_is_not_followed(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(_line("prompt", "should not be read"))
    rec.with_name(_stem(rec) + ".log.jsonl").symlink_to(target)
    assert read_session(root, _stem(rec), _now(), None)["events"] == []


def test_consent_joins_this_sessions_requests_pending_and_decided_and_not_another_sessions(tmp_path, monkeypatch):
    q = _queue(tmp_path, monkeypatch)
    q.file(agent="claude_code", agent_session_id=SID, tool_name="Bash", tool_input={"command": "ls"}, cwd=CWD,
           hold=timedelta(minutes=5), event_id="e1")
    done = q.file(agent="claude_code", agent_session_id=SID, tool_name="Write", tool_input={"file_path": "/x"},
                  cwd=CWD, hold=timedelta(minutes=5), event_id="e2")
    q.withdraw(done.id)
    q.file(agent="claude_code", agent_session_id=SID_OTHER, tool_name="Edit", tool_input={"file_path": "/y"},
           cwd=CWD, hold=timedelta(minutes=5), event_id="e3")
    q.file(agent="codex", agent_session_id=SID, tool_name="Read", tool_input={"file_path": "/z"},      # same id, other agent
           cwd=CWD, hold=timedelta(minutes=5), event_id="e4")
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    rows = read_session(root, _stem(rec), _now(), lambda now: read_listing(now=now))["consent"]
    assert sorted((r["tool"], r["state"]) for r in rows) == [("Bash", "pending"), ("Write", "withdrawn")]
    assert all(set(r) == {"at", "tool", "state", "via", "decided_at"} for r in rows)
    assert {r["state"]: r["decided_at"] is None for r in rows} == {"pending": True, "withdrawn": False}
    assert [r["at"] for r in rows] == sorted(r["at"] for r in rows)


def test_an_unreadable_queue_is_said_not_drawn_as_no_requests(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))

    def broken(now):
        raise OSError("queue gone")
    body = read_session(root, _stem(rec), _now(), broken)
    assert body["consent"] is None and body["events"] == []


# ---- the route --------------------------------------------------------------------------------------------------

def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"X-Levain-Write-Token": token} if token is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_route_needs_the_token_on_loopback_and_404s_a_bad_stem(tmp_path, monkeypatch):
    _queue(tmp_path, monkeypatch)
    root, src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    _log(rec, _line("prompt", PROMPT))
    route = f"/cockpit/session/{_stem(rec)}.json"
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        status, blob = _get(url + route)
        assert status == 403 and "token" in json.loads(blob)["message"] and PROMPT.encode() not in blob
        assert _get(url + route, "wrong")[0] == 403
        assert _get(url + "/cockpit/session/zz.json")[0] == 403          # no probing of stems without the token
        status, blob = _get(url + route, "T")
        assert status == 200
        body = json.loads(blob)
        assert body["events"][0]["text"] == PROMPT and body["workspace"] == CWD and body["consent"] == []
        for bad in ("zz", "0" * 24, _stem(rec).upper(), "..%2F" + _stem(rec)):
            assert _get(url + f"/cockpit/session/{bad}.json", "T")[0] == 404, bad


def test_no_token_free_route_carries_the_transcript_or_the_session_id(tmp_path, monkeypatch):
    _queue(tmp_path, monkeypatch)
    root, src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2))
    _log(rec, _line("prompt", PROMPT) + _line("reply", "zq-q8-reply-marker"))
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        blobs = {r: _get(url + r) for r in ("/cockpit/panel/sessions.json", "/cockpit/manifest.json")}
    assert all(status == 200 for status, _b in blobs.values())
    assert b"session:" in blobs["/cockpit/panel/sessions.json"][1]       # the row is there; its words are not
    for _status, blob in blobs.values():
        for marker in (PROMPT, "zq-q8-reply-marker", SID, CWD, "q8 full"):
            assert marker.encode() not in blob, marker


def test_a_nul_in_a_record_never_takes_the_session_view_down(tmp_path):
    root, _src, _ids = _install(tmp_path)
    rec = _put(root, "claude_code", SID, CWD, timedelta(minutes=2), transcript="/t/a\0b.jsonl")
    body = read_session(root, _stem(rec), _now(), None)           # the bad transcript is dropped, the record stands
    assert body is not None and body["context"]["tokens"] is None and body["context"]["compacted"] is False
    rec = _put(root, "claude_code", SID_OTHER, CWD, timedelta(minutes=2), session_id=SID_OTHER + "\0")
    assert read_session(root, _stem(rec), _now(), None) is None   # a NUL id is not a record of ours
