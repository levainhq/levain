"""The planner capability record: one 0600 file per filing session under the dispatch directory, written whole under a
lock, read back by the same agent and session only, refused when it or its directory is anything but the operator's
alone, and never written for a session that cannot be told apart."""
from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from levain.dispatch import capability as caps

JOB1, JOB2 = "dj-" + "1" * 32, "dj-" + "2" * 32
CAP1, CAP2 = "A" * 43, "b-_" * 14 + "c"


@pytest.fixture
def ddir(tmp_path):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


def test_round_trip_and_the_record_shape(ddir):
    path = caps.keep(ddir, "claude_code", "sess-1", JOB1, CAP1)
    want = hashlib.sha256(b"claude_code\0sess-1").hexdigest()
    assert path == ddir / "capabilities" / f"{want}.json"
    assert caps.recall(ddir, "claude_code", "sess-1", JOB1) == CAP1
    assert os.stat(path).st_mode & 0o777 == 0o600 and os.stat(path.parent).st_mode & 0o777 == 0o700
    data = json.loads(path.read_text())
    assert data == {"format": caps.FORMAT, "agent": "claude_code", "session_id": "sess-1", "jobs": {JOB1: CAP1}}


def test_a_second_job_joins_the_record_and_sessions_and_agents_stay_apart(ddir):
    caps.keep(ddir, "claude_code", "sess-1", JOB1, CAP1)
    caps.keep(ddir, "claude_code", "sess-1", JOB2, CAP2)
    assert caps.recall(ddir, "claude_code", "sess-1", JOB1) == CAP1
    assert caps.recall(ddir, "claude_code", "sess-1", JOB2) == CAP2
    assert caps.recall(ddir, "claude_code", "sess-2", JOB1) is None
    assert caps.recall(ddir, "codex", "sess-1", JOB1) is None


def test_concurrent_filings_from_one_session_lose_nothing(ddir):
    ids = [f"dj-{i:032x}" for i in range(24)]
    threads = [threading.Thread(target=caps.keep, args=(ddir, "codex", "t-1", j, "C" * 43)) for j in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(caps.recall(ddir, "codex", "t-1", j) == "C" * 43 for j in ids)


@pytest.mark.parametrize("session", [None, "", caps.NO_SESSION, "levain-ask", "a\nb", "x" * 257,
                                     "main#agent:sub-1", "tab\there"])
def test_no_record_for_a_session_that_cannot_be_told_apart(ddir, session):
    assert caps.session_problem(session) is not None
    assert caps.keep(ddir, "claude_code", session, JOB1, CAP1) is None
    assert caps.recall(ddir, "claude_code", session, JOB1) is None
    assert not (ddir / "capabilities").exists()


def test_a_directory_others_can_reach_is_refused(ddir):
    caps.keep(ddir, "claude_code", "s", JOB1, CAP1)
    os.chmod(ddir / "capabilities", 0o755)
    with pytest.raises(caps.CapabilityRecordError, match="0700"):
        caps.recall(ddir, "claude_code", "s", JOB1)
    with pytest.raises(caps.CapabilityRecordError, match="0700"):
        caps.keep(ddir, "claude_code", "s", JOB2, CAP2)


def test_a_record_others_can_read_is_refused(ddir):
    path = caps.keep(ddir, "claude_code", "s", JOB1, CAP1)
    os.chmod(path, 0o644)
    with pytest.raises(caps.CapabilityRecordError, match="0600"):
        caps.recall(ddir, "claude_code", "s", JOB1)


def test_a_symlinked_record_or_directory_is_refused(ddir, tmp_path):
    path = caps.keep(ddir, "claude_code", "s", JOB1, CAP1)
    elsewhere = tmp_path / "elsewhere.json"
    os.replace(path, elsewhere)
    os.symlink(elsewhere, path)
    with pytest.raises(caps.CapabilityRecordError, match="symlink"):
        caps.recall(ddir, "claude_code", "s", JOB1)
    other = tmp_path / "d2"
    other.mkdir(mode=0o700)
    real = tmp_path / "real-caps"
    real.mkdir(mode=0o700)
    os.symlink(real, other / "capabilities")
    with pytest.raises(caps.CapabilityRecordError, match="symlink"):
        caps.keep(other, "claude_code", "s", JOB1, CAP1)


def test_a_record_naming_another_session_or_malformed_is_refused(ddir):
    path = caps.keep(ddir, "claude_code", "s", JOB1, CAP1)
    data = json.loads(path.read_text())
    path.write_text(json.dumps(dict(data, session_id="other")))
    with pytest.raises(caps.CapabilityRecordError, match="another session"):
        caps.recall(ddir, "claude_code", "s", JOB1)
    path.write_text(json.dumps(dict(data, jobs={JOB1: "not a capability!"})))
    with pytest.raises(caps.CapabilityRecordError, match="not a"):
        caps.recall(ddir, "claude_code", "s", JOB1)
    path.write_text("{")
    with pytest.raises(caps.CapabilityRecordError, match="not a record"):
        caps.recall(ddir, "claude_code", "s", JOB1)


def test_no_dispatch_directory_is_refused_on_keep_and_empty_on_recall(tmp_path):
    with pytest.raises(caps.CapabilityRecordError, match="does not exist"):
        caps.keep(tmp_path / "absent", "claude_code", "s", JOB1, CAP1)
    assert caps.recall(tmp_path / "absent", "claude_code", "s", JOB1) is None


def test_bad_inputs_are_refused(ddir):
    with pytest.raises(caps.CapabilityRecordError, match="agent"):
        caps.keep(ddir, "cursor", "s", JOB1, CAP1)
    with pytest.raises(caps.CapabilityRecordError, match="job id"):
        caps.keep(ddir, "codex", "s", "nope", CAP1)
    with pytest.raises(caps.CapabilityRecordError, match="capability"):
        caps.keep(ddir, "codex", "s", JOB1, "short")
