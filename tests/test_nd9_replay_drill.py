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


@pytest.fixture(scope="module")
def table_runs(drill):
    """Three runs of each desk mode, as the committed table was made (drill.py --runs 3)."""
    return {(mode, i): drill.run_once(mode == "fresh") for mode in ("resident", "fresh") for i in range(3)}


def test_the_committed_table_is_what_the_drill_now_generates_and_every_reason_is_a_live_disagreement(drill, table_runs):
    why = json.loads((DRILL / "why.json").read_text("utf-8"))
    actual = drill.disagreements(table_runs[("resident", 0)])
    assert set(why) == set(actual), "why.json names a disagreement the drill no longer reports, or lacks one it does"
    assert all(why[e].strip() for e in actual)
    assert (drill.render(table_runs, 3, why) + "\n") == (DRILL / "disagreement_table.md").read_text("utf-8")


def test_an_event_is_acted_on_only_when_a_desk_launch_took_it_and_the_desk_acknowledged_it(drill, table_runs):
    """A joiner, a redelivery, a refused or an alerted event is never handed to the scripted desk, so it gets no ask; one
    that was handed over is acknowledged by that desk. The harness's own limits are not a row of the table."""
    for r in table_runs.values():
        for eid in drill.E:
            o = r[eid]
            if not o["wakes_desk"]:
                assert o["ran"] == [] and o["sent"] == [] and o["waha"] == [] and "ack" not in o and o["refusals"] == [], eid
            else:
                assert o["ack"] == "acked", eid


def test_the_order_the_oracle_rejected_is_not_rewritten_and_the_gate_is_put_back(drill, table_runs):
    """E10 (php8.3-fpm) runs against a signed order that names only apache2, not against a renamed copy of the rejected
    8.1 line; the drill opens the closed arming gate for its run and restores it."""
    signing = " ".join(m for v in table_runs[("resident", 0)]["_signing"].values() for m in v)
    assert "signed only for the part of the line the verb accepts, service ['apache2']" in signing
    assert "changed to" not in signing
    from levain.desk import effects
    before = effects.NIGHT_ARMING_FACTS
    effects.NIGHT_ARMING_FACTS = {"closed": "as shipped"}          # a closed gate stands in for the shipped one
    try:
        drill.run_once(False)
        assert effects.NIGHT_ARMING_FACTS == {"closed": "as shipped"}
    finally:
        effects.NIGHT_ARMING_FACTS = before
    assert table_runs[("resident", 0)]["E10"]["refusals"] and table_runs[("resident", 0)]["E10"]["ran"] == []


@pytest.mark.parametrize("n", ["0", "-1"])
def test_runs_must_be_a_positive_number(drill, n, capsys):
    with pytest.raises(SystemExit) as stop:
        drill.main(["--runs", n])
    assert stop.value.code == 2 and "--runs must be 1 or more" in capsys.readouterr().err
