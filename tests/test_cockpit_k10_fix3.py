"""K10 head 1020+24 (fix3): the L3 findings on the replay server half (S8a) and the S7 provider. One test per item, named
for it; each failed on ck-k10 69f294a0 unless its docstring says it is a verification of a finding that did not hold."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from levain.cockpit import engine as eng
from levain.cockpit import providers as prov
from levain.cockpit import routes
from levain.cockpit import topology as topo
from levain.cockpit.engine import QueryError, check_replay_at
from tests.test_cockpit_s8a_replay import (NOW, B, _at, _kinds, _Listing, _m, _node, _record, _rig, _stem, _z, _events,
                                           _Entry)

NOCRED = {"class": "none", "device_class": None}


def _get(ck, query: str):
    status, body, _h = routes.handle_get(ck, f"/cockpit/panel/topology.json{query}", if_none_match=None, credential=NOCRED)
    return status, body


# --- F1: the horizon filter comes before the cap ------------------------------------------------------------------

def test_f1_a_session_live_at_t_is_not_pushed_out_by_newer_records(tmp_path, monkeypatch):
    ck, root, _cdir, work = _rig(tmp_path, monkeypatch)
    _record(root, "s-old", str(work / "a"), _m(0), _m(100))          # started 10:00, last seen 11:40: live at 11:30
    old = root / ".levain" / "sessions" / f"{_stem('claude_code', 's-old')}.json"
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    for i in range(prov.SESSIONS_SHOWN + 5):                         # newer files, all started after T
        _record(root, f"s-new-{i}", str(work / "late"), _m(110), NOW - timedelta(minutes=1))
    v = _at(ck, _m(90))
    assert _node("claude_code", "s-old") in {n["id"] for n in v["nodes"]}


def test_f1_a_scan_that_stopped_early_names_session_records_unreadable(tmp_path, monkeypatch):
    ck, root, _cdir, work = _rig(tmp_path, monkeypatch)
    for i in range(6):
        _record(root, f"s-x{i}", str(work / "a"), _m(0), _m(100))
    monkeypatch.setattr(prov, "SESSIONS_SCAN", 3)
    v = _at(ck, _m(90))
    assert "session_records" in v["unreadable"]
    assert any(e.startswith("session_records: not kept for the past") for e in v["errors"])
    live = ck.panel("topology", credential_class="none")["value"]            # control: a live read says nothing of it
    assert "session_records" not in live["unreadable"]


# --- F2: replay has its own bounded pool and a short cache ----------------------------------------------------------

def test_f2_a_hung_replay_takes_no_slot_from_the_write_pool(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["topology"]
    release = threading.Event()
    real = spec.read_at
    spec.read_at = lambda ctx, iso: (release.wait(10), real(ctx, iso))[1]
    spec.timeout_s = 0.15
    try:
        p = ck.panel("topology", credential_class="none", at=_z(_m(90)))
        assert p["status"] == "error"
        assert ck._one_live == []
    finally:
        release.set()


def test_f2_over_its_own_cap_a_replay_is_refused_clearly(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["topology"]
    release = threading.Event()
    spec.read_at = lambda ctx, iso: (release.wait(10), None)[1]
    spec.timeout_s = 0.1
    try:
        outs = [ck.panel("topology", credential_class="none", at=_z(_m(60 + i))) for i in range(eng.REPLAY_READ_CAP + 1)]
        assert all(o["status"] == "error" for o in outs)
        assert "replay" in outs[-1]["error"] and "refused" in outs[-1]["error"]
    finally:
        release.set()


def test_f2_one_minute_read_twice_reads_the_source_once(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["topology"]
    real, calls = spec.read_at, []
    spec.read_at = lambda ctx, iso: (calls.append(iso), real(ctx, iso))[1]
    a = ck.panel("topology", credential_class="none", at=_z(_m(90)))
    b = ck.panel("topology", credential_class="none", at=_z(_m(90)))
    c = ck.panel("topology", credential_class="none", at=_z(_m(91)))
    assert len(calls) == 2 and a["value"] == b["value"] and a["etag"] == b["etag"] and c["etag"] != a["etag"]


def test_f2_a_failed_replay_is_never_cached(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["topology"]
    real, calls = spec.read_at, []

    def flaky(ctx, iso):
        calls.append(iso)
        return eng.Fault("boom") if len(calls) == 1 else real(ctx, iso)
    spec.read_at = flaky
    assert ck.panel("topology", credential_class="none", at=_z(_m(90)))["status"] == "error"
    assert ck.panel("topology", credential_class="none", at=_z(_m(90)))["status"] in ("ok", "empty")


# --- F3: normalisation happens inside the bounded call ------------------------------------------------------------

def test_f3_a_slow_normalisation_is_bounded_like_the_read(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["topology"]
    spec.timeout_s = 0.2
    real = ck._normalize
    ck._normalize = lambda s, c, r: (time.sleep(1.0), real(s, c, r))[1]
    t0 = time.monotonic()
    p = ck.panel("topology", credential_class="none", at=_z(_m(90)))
    assert time.monotonic() - t0 < 0.7 and p["status"] == "error"


# --- F4: an instant that overflows the UTC conversion is a 400 / an omitted line ----------------------------------------

@pytest.mark.parametrize("at", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:00-23:59"])
def test_f4_an_overflowing_instant_is_a_query_error(at):
    with pytest.raises(QueryError):
        check_replay_at(at, NOW)


def test_f4_a_route_answers_400_not_500(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    assert _get(ck, "?at=0001-01-01T00:00:00%2B23:59")[0] == 400


def test_f4_a_wrap_time_that_overflows_is_unknown_time():
    assert prov._utc_at("0001-01-01T00:00:00+23:59") == ""
    assert prov._overnight(SimpleNamespace(wraps=[SimpleNamespace(wrapped_at="0001-01-01T00:00:00+23:59")],
                                           health=SimpleNamespace(episodes_since_wrap=3), errors={})) is None


# --- F5: an incomplete consent listing fails closed -----------------------------------------------------------------

@pytest.mark.parametrize("flag", [{"overflow": True}, {"skipped": 2}])
def test_f5_a_listing_that_overflowed_or_skipped_cannot_answer_for_t(flag):
    listing = _Listing(recent=(_Entry(_m(80), _m(85)),))
    for k, v in flag.items():
        setattr(listing, k, v)
    with pytest.raises(LookupError, match="incomplete"):
        topo._consent_waiting(listing, _m(90), NOW)
    # control: the same listing, complete, answers; and a live read is not affected
    ok = _Listing(recent=(_Entry(_m(80), _m(85)),))
    ok.overflow, ok.skipped = False, 0
    assert topo._consent_waiting(ok, _m(90), NOW) == []
    live = _Listing()
    for k, v in flag.items():
        setattr(live, k, v)
    assert topo._consent_waiting(live, None) == []


# --- F6: the query is read as sent --------------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["?at=", "?at=&profile=full", f"?at=2026-10-20T11:30:00Z&q=", f"?at=2026-10-20T11:30:00Z&row=x",
                                   f"?at=2026-10-20T11:30:00Z&limit=3"])
def test_f6_a_blank_at_or_a_param_beside_at_is_a_400(tmp_path, monkeypatch, query):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    assert _get(ck, query)[0] == 400


def test_f6_blank_params_without_at_still_read_as_absent(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    assert _get(ck, "?q=&row=&limit=&profile=")[0] == 200


# --- F7: a node not drawn is not counted unsure -------------------------------------------------------------------

def test_f7_a_rotated_log_on_an_undrawn_node_is_not_counted(tmp_path, monkeypatch):
    ck, root, _cdir, work = _rig(tmp_path, monkeypatch)
    t = NOW - timedelta(hours=1)
    # started 26 h before T, record last seen now (so after T): its only stamp at T is its first, quiet past 24 h
    _record(root, "s-quiet", str(work / "a"), t - timedelta(hours=26), NOW - timedelta(minutes=1))
    _events(root, "s-quiet", [("reply", NOW - timedelta(minutes=30), "x")], pad=50)     # rotated, oldest kept is after T
    v = _at(ck, t)
    assert _node("claude_code", "s-quiet") not in {n["id"] for n in v["nodes"]}
    assert "session_logs" not in v["unreadable"]


# --- F8: the head promises a replay only for a panel the engine will serve ---------------------------------------------

def test_f8_a_row_engine_panel_with_a_read_at_carries_no_replay_flag(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    spec = ck._specs["tray"]
    assert spec.kind in eng.ROW_ENGINE_KINDS
    spec.read_at = lambda ctx, iso: None
    assert "replay" not in ck.panel("tray", credential_class="none")
    with pytest.raises(QueryError, match="no past read"):
        ck.panel("tray", credential_class="none", at=_z(_m(90)))
    assert ck.panel("topology", credential_class="none")["replay"] is True


# --- F9: a replay's day is T's day ------------------------------------------------------------------------------------

def test_f9_the_replay_context_today_is_ts_local_day(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    fixed = datetime.now(timezone.utc).astimezone().replace(hour=1, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    ck._clock = lambda: fixed
    t = fixed - timedelta(hours=3)                 # the previous local day
    p = ck.panel("topology", credential_class="none", at=_z(t))
    assert p["today"] == t.astimezone().date().isoformat() != fixed.astimezone().date().isoformat()
    assert ck.panel("topology", credential_class="none")["today"] == fixed.astimezone().date().isoformat()


# --- V1 / V2: findings to verify -----------------------------------------------------------------------------------

def test_v1_a_question_filed_before_t_and_never_answered_is_waiting_at_t(tmp_path, monkeypatch):
    """glm HIGH: ``store.recent`` is said to list only answered questions. It lists the newest questions answered or not
    (questions.py ``recent``: no join filter), so this passes on ck-k10 69f294a0: the finding is refuted."""
    from levain.consent.questions import QuestionStore

    ck, root, cdir, work = _rig(tmp_path, monkeypatch)
    QuestionStore(cdir, clock=lambda: _m(60)).file(
        agent="claude_code", agent_session_id="s-a", what="never answered?", options=[("a", "A"), ("b", "B")], rec="a",
        blocks="x", cwd=str(work / "a"))
    still_open = QuestionStore(cdir).open_questions()
    assert len(still_open) == 1                                              # it is open now
    v = _at(ck, _m(90))
    waiting = {t["id"] for t in _kinds(v, "question")}
    assert f"question:{still_open[0].n}" in waiting
    assert len(waiting) == 2                                                 # and the rig's own (answered at 12:00)
    assert not [t for t in _kinds(_at(ck, _m(59)), "question")]              # control: not yet filed at 10:59


def test_v2_the_overnight_line_takes_the_newest_wrap(tmp_path, monkeypatch):
    """glm MED: ``wraps[0]`` is the newest only if the view sorts newest first. ``build_substrate_view`` sorts by
    ``wrapped_at`` descending (dashboard.py), so this passes: the finding is refuted."""
    import anneal_memory

    from levain.dashboard import AnnealPaths, build_substrate_view

    db = tmp_path / "memory.db"
    with anneal_memory.Store(db):
        pass

    def rec(at):
        return SimpleNamespace(wrapped_at=at, episodes_compressed=1, continuity_chars=1, graduations_validated=0,
                               graduations_demoted=0, associations_formed=0, associations_strengthened=0,
                               associations_decayed=0)
    monkeypatch.setattr(anneal_memory.Store, "get_wrap_history",
                        lambda self: [rec("2026-10-01T10:00:00+00:00"), rec("2026-10-19T10:00:00+00:00"),
                                      rec("2026-10-10T10:00:00+00:00")])
    v = build_substrate_view(AnnealPaths.from_db(db))
    assert [w.wrapped_at[:10] for w in v.wraps] == ["2026-10-19", "2026-10-10", "2026-10-01"]
    v.errors.pop("wraps", None), v.errors.pop("health", None)
    v.health = SimpleNamespace(episodes_since_wrap=4)
    assert prov._overnight(v)["since"].startswith("2026-10-19")
