"""levain#79: the Desk view's context reading held equal to flow's scripts/seat_context.py, where flow's checkout is present.

These three tests live apart from tests/test_desk_context_1116.py on purpose. That file has real browser tests, so the CI browser job
selects it, and the browser job fails on ANY skip; flow's checkout exists only on the operator's machine, so these skip on CI. In
a file of their own the browser job never selects them and a normal job tolerates their skip. The fixtures (rec, transcript) are the
other file's; nothing here is a browser test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pwd
import shutil
import sys
from pathlib import Path

import pytest

from levain.desk.desk_context import DEEP_HARD, HARD, SOFT, read_context
from tests.test_desk_context_1116 import rec, transcript

# the account's real home, not $HOME (the test rigs repoint HOME)
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
SEAT_CONTEXT = Path(os.environ.get("FLOW_SEAT_CONTEXT") or REAL_HOME / "Briefcase" / "flow" / "scripts" / "seat_context.py")


def flow_scan():
    if not SEAT_CONTEXT.is_file():
        pytest.skip("flow's scripts/seat_context.py is not on this machine")
    spec = importlib.util.spec_from_file_location("seat_context_ref", SEAT_CONTEXT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SEAT_CONTEXT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SEAT_CONTEXT.parent))
    return mod


def test_the_desk_lines_are_flows_rotation_lines():
    sc = flow_scan()
    assert sc.LIMITS["desk"] == (SOFT, HARD) and sc.DEEP_HARD == DEEP_HARD and sc.limits_for("desk", None)[:2] == (SOFT, HARD)
    assert sc.limits_for("desk", {"granted": "x"})[:2] == (None, DEEP_HARD)


def test_the_reading_matches_seat_contexts_for_the_same_transcript(tmp_path):
    sc = flow_scan()
    p = transcript(tmp_path, [rec(5, 100_000, 2_000), '{"type":"user"}', rec(0, 0, 0), rec(3, 212_000, 400, model="claude-opus-5-5"), "garbage", '{"type":"user"}'])
    ctx, _samples, model = sc.scan(str(p))
    got = read_context(p)
    assert got.tokens == ctx == 212_403 and got.model == model


def test_the_reading_matches_seat_contexts_on_a_real_session_transcript(tmp_path):
    sc = flow_scan()
    root = REAL_HOME / ".claude" / "projects"
    live = sorted((x for x in root.glob("*/*.jsonl") if 50_000 < x.stat().st_size < 60_000_000), key=lambda x: x.stat().st_mtime) if root.is_dir() else []
    if not live:
        pytest.skip("no real session transcript here")
    copy = tmp_path / "real.jsonl"
    shutil.copyfile(live[-1], copy)          # a copy: a live file grows between the two reads
    ctx, _samples, _model = sc.scan(str(copy))
    got = read_context(copy)
    assert (got.tokens if got else 0) == ctx, (got, ctx)
