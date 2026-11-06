"""The terminal seat's record-only hooks (D1)."""
from __future__ import annotations

import io
import json
import subprocess
import sys

from levain import seat_record as sr


def run(tmp_path, payload, argv=None):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = sr.record_main(argv if argv is not None else [str(tmp_path / "rec")], io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_row_has_tool_digest_cwd_and_prints_nothing(tmp_path):
    rc, out = run(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/w", "session_id": "s"})
    assert (rc, out) == (0, "")
    rows = sr.read_rows(tmp_path / "rec")
    assert len(rows) == 1 and rows[0]["tool"] == "Bash" and rows[0]["cwd"] == "/w"
    assert len(rows[0]["input_sha256"]) == 64 and "ls" not in json.dumps(rows[0])
    assert oct((tmp_path / "rec" / sr.RECORD_FILE).stat().st_mode & 0o777) == "0o600"


def test_same_input_same_digest_and_append(tmp_path):
    for _ in range(2):
        run(tmp_path, {"tool_name": "Read", "tool_input": {"a": 1, "b": 2}, "cwd": "/w"})
    a, b = sr.read_rows(tmp_path / "rec")
    assert a["input_sha256"] == b["input_sha256"]


def test_never_raises_never_decides(tmp_path):
    assert run(tmp_path, "not json") == (0, "")
    assert run(tmp_path, [1, 2]) == (0, "")
    assert run(tmp_path, {"tool_name": "x"}, argv=[]) == (0, "")
    assert sr.read_rows(tmp_path / "rec") == []


def test_blind(tmp_path):
    assert sr.blind(tmp_path / "rec", 3) is True
    assert sr.blind(tmp_path / "rec", 0) is False
    run(tmp_path, {"tool_name": "Bash", "tool_input": {}, "cwd": "/w"})
    assert sr.blind(tmp_path / "rec", 3) is False


def test_script_entry_pre_via_subprocess(tmp_path):
    r = subprocess.run([sys.executable, "-B", sr.__file__, "pre", str(tmp_path / "r2")],
                       input=json.dumps({"tool_name": "Write", "tool_input": {}, "cwd": "/c"}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == ""
    assert sr.read_rows(tmp_path / "r2")[0]["tool"] == "Write"


def test_bound_main_never_raises(tmp_path):
    assert sr.bound_main(io.StringIO("garbage")) == 0
