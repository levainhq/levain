"""ND-9 (board levain#18): the replay drill's harness, not its verdict. The drill (project_memory/1116-26_nd9_replay/)
replays 21 synthetic events against the draft orders and writes a disagreement table for him to read. What is pinned here
is only that the harness runs offline, covers the oracle's events and the five classes, gives the same observation under
a fresh and a resident desk, and has a written reason for each disagreement it reports. Whether the code and the oracle
agree is never asserted: a disagreement is the table's content, and the table is his to read."""
from __future__ import annotations

import importlib.util
import json
import shutil
import socket
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

ROOT = Path(__file__).resolve().parent.parent
DRILL = ROOT / "project_memory" / "1116-26_nd9_replay"
pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.skipif(not (DRILL / "drill.py").exists(), reason="the drill is not in this checkout")]


@pytest.fixture(scope="module")
def drill():
    spec = importlib.util.spec_from_file_location("nd9_drill", DRILL / "drill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_corpus_and_the_oracle_name_the_same_events_across_the_classes(drill):
    oracle = json.loads((DRILL / "oracle.json").read_text("utf-8"))["events"]
    assert set(oracle) == set(drill.E) and len(oracle) >= 20
    assert {o["class"] for o in oracle.values()} >= {"forged", "real", "ambiguous", "hostile_body", "hostile_reader_field"}


def test_the_drill_runs_offline_and_a_fresh_and_a_resident_desk_observe_the_same(drill, monkeypatch):
    def no_network(*a, **kw):
        raise AssertionError("the drill opened a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    resident, fresh = drill.run_once(False), drill.run_once(True)
    assert set(resident) - {"_signing"} == set(drill.E)
    for eid in drill.E:
        assert {k: resident[eid].get(k) for k in drill.KEYS} == {k: fresh[eid].get(k) for k in drill.KEYS}, eid


def test_every_disagreement_the_drill_reports_has_a_written_reason(drill):
    oracle = json.loads((DRILL / "oracle.json").read_text("utf-8"))["events"]
    why = json.loads((DRILL / "why.json").read_text("utf-8"))
    got = drill.run_once(False)
    unexplained = [e for e in drill.E if drill.compare(oracle[e]["expect"], got[e]) and not why.get(e)]
    assert unexplained == []
