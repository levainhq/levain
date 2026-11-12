"""D1-M: ``levain dream start``, the hand-start verb (Phill 11-11, Q5 (a): "build one verb that runs through the same
link, ceiling and controller as a scheduled run"), and three small LOWs of the D1-L1 fix round (L3 r2 D3, D4, N2).

Every store is real: the agent store and the signed link are the Rig of ``tests/test_agents_launch.py`` (ssh-keygen keys,
links verified through the authority replay); the dream's anneal store is a real one with episodes; nothing composes,
no model is called, and the fire is a seam (the one end-to-end fire is the scratch run in the lane's return).

What each verb test pins is a way a hand start could be a second door: it must not claim without a live link, must not
launch past the signed ceiling or the day-ready ceiling, must not skip the admission (a manual one, with check 6 waived
and every other check as at night), and must leave the audit trail a scheduled run leaves.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import cli as levain_cli
from levain.agents import controller as ctl
from levain.agents.store import open_agent_store
from levain.autonomic.journal import RunJournal
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import cli as dcli
from levain.dream import executor as ex
from levain.dream import ready as rd
from levain.dream import start as st
from tests.test_agents_dispatch import _dream_rig
from tests.test_agents_launch import BINDING

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

ZONE = "America/New_York"
MODEL = "claude-opus-5-5"


def _live_rig(tmp_path, *, link=True, **link_over):
    """A dream rig on the wall clock (the dream's input hook reads the wall clock), its install written, a link signed."""
    from anneal_memory import FLOW_SCHEMA, Store

    rig = _dream_rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    db = tmp_path / "st" / "memory.db"
    db.parent.mkdir()
    with Store(str(db), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    cd_ = tmp_path / "cd"
    cd_.mkdir(mode=0o700)
    facts = tmp_path / "facts.json"
    ex.write_first_run_facts(facts, model=MODEL, models_charged=[MODEL], m_usd_micros=ex.one_call_ceiling(MODEL))
    da.write_install(rig.store.directory, store_path=db, cockpit_root=rig.world.root, coord_dir=cd_, zone=ZONE,
                     facts_path=facts)
    if link:
        rig.link(**{"budget_run_usd_micros": 20_000_000, "budget_window_usd_micros": 100_000_000, **link_over})
    rig.db, rig.facts = db, facts
    return rig


def _go(rig, **kw):
    spawned = []
    got = st.start(rig.store, clock=rig.now, authority=rig.authority,
                   spawn=lambda d, oid: spawned.append((Path(d), oid)), **kw)
    return got, spawned


def _rows(rig):
    with rig.store.read() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM agent_occurrences ORDER BY admitted_at, occurrence_id")]


def _hold_a_wrap_open(db: Path) -> None:
    from anneal_memory import Store
    from anneal_memory.continuity import prepare_wrap

    with Store(str(db), section_schema=None) as s:
        assert prepare_wrap(s, wrap_token="c" * 32)["status"] == "ready"


def _end(rig, oid: str, state: str = "failed") -> None:
    """End a launched run by hand the way ``Rig.finish`` does (slot freed, reservation settled), in ``state``."""
    from levain.agents import slots

    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET state = ?, reason = 'r', ended_at = 'x' WHERE occurrence_id = ?",
                  (state, oid))
        slots.release(c, oid)
        c.execute("UPDATE agent_reservations SET state='settled', debited_usd_micros=0 WHERE occurrence_id = ?", (oid,))


def _claim(rig, key: str, *, kind: str = "manual", window_key: str | None = None, launched=False,
           state: str | None = None):
    """Claim (and with ``launched`` launch with no input check; with ``state`` then end) an occurrence."""
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=rig.agent, kind=kind, key=key, spec_hash=rig.spec_hash,
                                 binding_id=BINDING, window_key=window_key or st.manual_window_key(rig.now(), ZONE),
                                 now=rig.now())
    assert outcome == ctl.CLAIMED
    if launched or state:
        res = ctl.launch(rig.store, oid, authority=rig.authority, executor=ex.DreamExecutor(
            agent_store_dir=rig.store.directory, occurrence_id=oid, occ_dir=rig.db.parent / "dream" / oid,
            model=MODEL, facts_path=rig.facts), window=st.window_for(rig.now(), ZONE), now=rig.now(), on_input=None)
        assert res.outcome == "launched", res
    if state:
        _end(rig, oid, state)
    return oid


# ------------------------------------------------------------------------------------------------ the verb exists

def test_the_dream_group_has_a_start_verb_that_parses():
    args = levain_cli.build_parser().parse_args(["dream", "start", "--agent-store-dir", "/x", "--json"])
    assert args.func is dcli.run_dream_start and args.json is True
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err):
        assert dcli.run_dream(argparse.Namespace()) == 1
    assert "start" in err.getvalue()


# ------------------------------------------------------------------------------- the link: no link, nothing claimed

def test_with_no_live_link_nothing_is_claimed_and_no_fire_is_spawned(tmp_path):
    rig = _live_rig(tmp_path, link=False)
    got, spawned = _go(rig)
    assert (got.code, got.outcome) == (1, "refused") and "no live signed link" in got.text, got
    assert _rows(rig) == [] and spawned == []


def test_an_expired_link_arms_nothing(tmp_path):
    rig = _live_rig(tmp_path)                                 # a link that lasts 29 days, read 40 days on
    spawned = []
    got = st.start(rig.store, clock=lambda: rig.now(timedelta(days=40)), authority=rig.authority,
                   spawn=lambda *a: spawned.append(a))
    assert (got.outcome, "no live signed link" in got.text) == ("refused", True) and _rows(rig) == [] and spawned == []


def test_a_paused_or_unregistered_dream_is_refused_before_any_write(tmp_path):
    rig = _live_rig(tmp_path)
    with rig.store.write() as c:
        c.execute("UPDATE agents SET operation = 'paused' WHERE agent_id = ?", (rig.agent,))
    got, spawned = _go(rig)
    assert got.outcome == "refused" and "paused" in got.text and "levain agents resume dream" in got.text
    assert _rows(rig) == [] and spawned == []
    empty = open_agent_store(tmp_path / "empty")
    out = st.start(empty, clock=rig.now, authority=rig.authority, spawn=lambda *a: None)
    assert out.outcome == "refused" and "levain dream enable" in out.text


def test_a_store_with_no_install_facts_is_refused_and_says_how_to_write_them(tmp_path):
    rig = _live_rig(tmp_path)
    (rig.store.directory / da.INSTALL_FILE).unlink()
    got, spawned = _go(rig)
    assert got.outcome == "refused" and "levain dream enable --store-dir" in got.text
    assert _rows(rig) == [] and spawned == []


def test_a_dream_that_cannot_state_its_exposure_claims_nothing(tmp_path):
    rig = _live_rig(tmp_path)
    (tmp_path / "facts.json").unlink()                       # the first-run facts: no exposure, no launch
    got, spawned = _go(rig)
    assert got.outcome == "refused" and "exposure" in got.text and _rows(rig) == [] and spawned == []


# --------------------------------------------------------------- a start under a live link: the same trail as a night

def test_a_hand_start_under_a_live_link_launches_one_manual_occurrence_and_spawns_its_fire(tmp_path):
    rig = _live_rig(tmp_path)
    got, spawned = _go(rig)
    assert (got.code, got.outcome) == (0, "launched"), got
    (row,) = _rows(rig)
    assert row["occurrence_id"] == got.occurrence_id and row["kind"] == "manual" and row["state"] == "launched"
    assert row["binding_id"] == BINDING and row["spec_hash"] == rig.spec_hash and row["link_id"]
    assert row["window_key"] == st.manual_window_key(rig.now(), ZONE) and row["launch_epoch"] is not None
    # the same trail a scheduled launch leaves: an open reservation at the executor's stated exposure, a slot, a run
    # admitted in the journal, and ONE fire spawned for this occurrence in this agent store
    taken_slots, taken_resv = rig.taken(row["occurrence_id"])
    assert len(taken_slots) == 1 and taken_resv == [("open",)]
    RunJournal(rig.store.directory).peek(row["run_id"], "\x00admission")            # raises KeyError when not admitted
    assert spawned == [(rig.store.directory, row["occurrence_id"])]
    assert str(rig.db.parent / "dream" / row["occurrence_id"]) in got.text


def test_the_input_hook_runs_the_admission_as_a_manual_run(tmp_path, monkeypatch):
    rig = _live_rig(tmp_path)
    seen = []
    real = rd.evaluate
    monkeypatch.setattr(rd, "evaluate", lambda *a, **k: seen.append(k.get("manual")) or real(*a, **k))
    got, _ = _go(rig)
    assert got.outcome == "launched" and seen == [True]


def test_an_open_wrap_still_refuses_a_hand_start_as_it_refuses_a_night(tmp_path):
    rig = _live_rig(tmp_path)
    _hold_a_wrap_open(rig.db)
    got, spawned = _go(rig)
    assert (got.code, got.outcome) == (1, "skipped-no-input") and "wrap-open" in got.text, got
    (row,) = _rows(rig)
    assert row["state"] == "skipped-no-input" and row["launched_at"] is None and spawned == []
    assert rig.taken(row["occurrence_id"]) == ([], [])


def test_check_six_is_waived_for_a_hand_start_the_catch_up_after_a_spent_attempt(tmp_path):
    # a scheduled run of the window that failed spends the window's one attempt; a night would refuse, a hand start runs
    rig = _live_rig(tmp_path)
    now = rig.now()
    key = rd.window_key_for(rd.manual_day_at(now, ZONE))
    _claim(rig, "2026-11-01T03:00:00Z", kind="scheduled", window_key=key, state="failed")
    assert rd.evaluate(rd.sources_for(rig.db, agent_dir=rig.store.directory, coord_dir=tmp_path / "cd", now=now)[0],
                       day=rd.manual_day_at(now, ZONE), now=now, zone=ZONE, agent_id=rig.agent).kind == "attempt-spent"
    got, spawned = _go(rig)
    assert got.outcome == "launched", got.text
    assert len(spawned) == 1


# ------------------------------------------------------------------------------------------------------ the ceilings

def test_the_signed_run_ceiling_stops_a_hand_start_and_takes_nothing(tmp_path):
    rig = _live_rig(tmp_path, run_ceiling=1)
    first, spawned = _go(rig)
    assert first.outcome == "launched"
    _end(rig, first.occurrence_id)                            # that run ended (not done: done would be a dreamed day)
    second, spawned2 = _go(rig)
    assert (second.code, second.outcome) == (1, "skipped-budget") and "ceiling" in second.text, second
    row = next(r for r in _rows(rig) if r["occurrence_id"] == second.occurrence_id)
    assert row["state"] == "skipped-budget" and row["reason"] == "ceiling" and row["launched_at"] is None
    assert rig.taken(second.occurrence_id) == ([], []) and spawned2 == []


def test_the_day_ready_ceiling_of_two_stops_a_third_hand_start(tmp_path):
    rig = _live_rig(tmp_path, run_ceiling=9)
    for n in range(rd.RUN_CEILING):                          # two manual runs launched today that failed
        _claim(rig, f"earlier-{n}", state="failed")
    got, spawned = _go(rig)
    assert (got.code, got.outcome) == (1, "skipped-no-input"), got
    assert f"the ceiling is {rd.RUN_CEILING}" in got.text and spawned == []


def test_a_start_is_refused_while_another_occurrence_is_in_flight(tmp_path):
    rig = _live_rig(tmp_path)
    busy = _claim(rig, "busy", launched=True)
    got, spawned = _go(rig)
    assert got.outcome == "refused" and busy in got.text and spawned == []
    assert [r["occurrence_id"] for r in _rows(rig)] == [busy]


# ---------------------------------------------------------------------------- a claimed start that could not launch

def test_a_manual_occurrence_left_admitted_is_the_one_a_second_start_retries(tmp_path):
    rig = _live_rig(tmp_path)
    left = _claim(rig, "left-admitted")
    got, spawned = _go(rig)
    assert (got.outcome, got.resumed, got.occurrence_id) == ("launched", True, left), got
    assert len(_rows(rig)) == 1 and spawned == [(rig.store.directory, left)]


def test_a_fire_that_cannot_be_spawned_is_reported_with_the_row_left_launched(tmp_path):
    rig = _live_rig(tmp_path)

    def boom(directory, oid):
        raise OSError("no fork")

    got = st.start(rig.store, clock=rig.now, authority=rig.authority, spawn=boom)
    assert (got.code, got.outcome) == (1, "launched") and "no fork" in got.text and "lapses to takeover" in got.text
    assert _rows(rig)[0]["state"] == "launched"


# ------------------------------------------------------------------------------------------------- the CLI front end

def test_the_cli_prints_the_refusal_and_exits_one_when_there_is_nothing_to_start(tmp_path, capsys):
    d = tmp_path / "ag"
    open_agent_store(d)
    assert levain_cli.main(["dream", "start", "--agent-store-dir", str(d)]) == 1
    cap = capsys.readouterr()
    assert "levain dream start:" in cap.err and "levain dream enable" in cap.err
    assert levain_cli.main(["dream", "start", "--agent-store-dir", str(d), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "refused"


# ------------------------------------------------------------------- the cards name the verb only where it applies

_STORE = "/tmp/some/store/memory.db"


def _cmds(text: str) -> list[str]:
    return [c for c in re.findall(r"`([^`]+)`", text) if c.startswith("levain ")]


def _parses(text: str) -> None:
    for c in _cmds(text):
        levain_cli.build_parser().parse_args(shlex.split(c)[1:])


@pytest.mark.parametrize("cls", ["failed", "laptop-slept", "attempt-spent", "wrap-open", "baton"])
def test_a_did_not_consolidate_remedy_names_the_hand_start_where_it_can_run(cls):
    text = da.window_remedy(cls, _STORE)
    assert "`levain dream start`" in text and "next window" in text, text
    _parses(text)


@pytest.mark.parametrize("cls", ["ceiling", "in-flight", "unreadable", "unknown", "live-seat", "consolidated-today"])
def test_a_remedy_where_a_hand_start_would_be_refused_does_not_name_it(cls):
    assert "levain dream start" not in da.window_remedy(cls, _STORE)


def test_the_hand_start_after_a_wrap_or_baton_is_named_as_the_step_after_clearing_it():
    for cls in ("wrap-open", "baton"):
        text = da.window_remedy(cls, _STORE)
        assert text.index("levain dream start") > text.index("wrap-cancel" if cls == "wrap-open" else "baton status"), text


def test_a_failed_run_card_names_the_hand_start_and_a_consolidated_card_does_not(tmp_path):
    failed = cd.remedy_for("failed", "o2", store_dir=tmp_path)
    assert "`levain dream start`" in failed
    _parses(failed)
    assert "levain dream start" not in cd.remedy_for("consolidated", "o1", store_dir=tmp_path)
    assert "levain dream start" not in cd.remedy_for("rolled-back", "o1", store_dir=tmp_path)


def test_the_generic_did_not_consolidate_card_names_the_hand_start(tmp_path):
    text = cd.remedy_for("did-not-consolidate", "window w:x", store_dir=tmp_path)
    assert "`levain dream start`" in text
    _parses(text)


def test_the_window_remedy_docstring_no_longer_says_no_verb_starts_a_dream_by_hand():
    assert "There is NO verb that starts a dream by hand" not in " ".join(da.window_remedy.__doc__.split())


# ------------------------------------------------------------------------------- LOW D3: enable reads the install

def _enable_args(tmp_path, *extra):
    from levain.agents import store as agent_store

    d = tmp_path / "ag"
    agent_store.set_default_zone(agent_store.open_agent_store(d), ZONE)
    p = argparse.ArgumentParser()
    dcli.add_parsers(p.add_subparsers())
    return p.parse_args(["dream", "enable", "--agent-store-dir", str(d), "--json", *extra])


def test_d3_enable_survives_an_install_file_whose_read_raises_something_other_than_install_refused(tmp_path, capsys):
    # reproduced: a hand-edited facts_path "~no_such_user/..." makes Path.expanduser() raise RuntimeError inside
    # read_install, which run_dream_enable caught only as InstallRefused: a traceback AFTER the dream was registered
    from tests.test_dream_ready import Entity

    e = Entity(tmp_path / "ent")
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir()
    args = _enable_args(tmp_path, "--store-dir", str(e.store_dir), "--cockpit-root", str(cockpit), "--coord-dir",
                        str(e.coord_dir))
    assert args.func(args) == 0
    capsys.readouterr()
    facts = tmp_path / "ag" / da.INSTALL_FILE
    doc = json.loads(facts.read_text())
    doc["facts_path"] = "~no_such_user_xyz/f.json"
    facts.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError):
        da.read_install(tmp_path / "ag")                       # the premise: this read raises a RuntimeError
    again = _enable_args(tmp_path)                             # no --store-dir: the card is built from the file as it stands
    assert again.func(again) == 0
    out = json.loads(capsys.readouterr().out)
    # D1-N's rule (the signing card never prints a stand-in for a path) governs what the card says when the facts are unreadable
    assert "`levain dream rollback` (each morning card names the exact command, with this install's store in it)" in out["signing_card"]
    assert "<the store's directory>" not in out["signing_card"]


def test_d3_the_shadow_branch_falls_back_on_the_same_broader_errors(tmp_path, capsys, monkeypatch):
    args = _enable_args(tmp_path)
    monkeypatch.setattr(da, "install_kind", lambda d: "shadow")
    monkeypatch.setattr(da, "read_install", lambda d: (_ for _ in ()).throw(OSError("unreadable")))
    assert args.func(args) == 0
    assert "facts cannot be read" in json.loads(capsys.readouterr().out)["signing_card"]


# ----------------------------------------------------------------------------------- LOW D4: the cadence the help says

def test_d4_the_dream_group_help_states_the_retry_rule_the_code_has():
    parser = levain_cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    text = " ".join(sub.choices["dream"].description.split())
    assert "once a night" not in text
    assert f"another every {da.RETRY_MINUTES} minutes until {da.WINDOW['end']}" in text, text
    assert "tried again at the next one" in text


# --------------------------------------------------------------------------- LOW N2: no placeholder command in a card

def test_n2_a_consolidated_or_failed_card_cannot_be_built_without_the_store(tmp_path):
    for kind in ("consolidated", "failed"):
        with pytest.raises(ValueError, match="store_dir"):
            cd.build_card(kind, occurrence="o1")
        with pytest.raises(ValueError, match="store_dir"):
            cd.remedy_for(kind, "o1")
    # an explicit remedy carries its own commands; a kind that prints no store command needs none
    assert cd.build_card("consolidated", occurrence="o1", remedy="see the desk").body["remedy"] == "see the desk"
    assert cd.build_card("rolled-back", occurrence="o1").body["remedy"]
    assert "<the store's directory>" not in cd.build_card("consolidated", occurrence="o1", store_dir=tmp_path).body["remedy"]


def test_n2_from_spec_without_a_store_or_a_db_refuses_for_those_kinds(tmp_path):
    for kind in ("consolidated", "failed"):
        with pytest.raises(ValueError, match="store_dir"):
            cd.from_spec({"kind": kind, "occurrence": "o1"}, occ_dir=tmp_path / "dream" / "o1")
    ok = cd.from_spec({"kind": "failed", "occurrence": "o1", "reason": "x"}, occ_dir=tmp_path / "dream" / "o1",
                      db_path=tmp_path / "memory.db")
    assert str(tmp_path) in ok.body["remedy"]
