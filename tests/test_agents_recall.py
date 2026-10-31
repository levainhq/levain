"""P4: episode identity stamped at capture, and per-agent recall read back from a shared store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.agents import recall as recall_mod
from levain.agents.recall import recall_agent_episodes_at
from levain.firing import CaptureRequest
from levain.firing.anneal import AnnealFiring, episode_identity

A = "ag-" + "a" * 32
B = "ag-" + "b" * 32
ROLE = "c" * 64


def _cap(f: AnnealFiring, text: str, **meta) -> None:
    assert f.capture(CaptureRequest(content=text, episode_type="observation", source="t",
                                    session_id=None, metadata=meta or None))


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


def test_identity_is_stamped_and_overrides_a_forged_key(tmp_path):
    db = _store_path(tmp_path)
    _cap(AnnealFiring(episodic_path=db, identity={"agent": A, "run": "occ-1", "role": ROLE}),
         "mine", agent=B, run="forged")
    ep = recall_agent_episodes_at(db, A).episodes[0]
    assert ep.metadata["agent"] == A and ep.metadata["run"] == "occ-1" and ep.metadata["role"] == ROLE
    assert recall_agent_episodes_at(db, B).episodes == ()


def test_spawn_namespace_and_bad_stamps():
    assert episode_identity("@spawn/reviewer", "launch-9") == {"agent": "@spawn/reviewer", "run": "launch-9"}
    for bad in ("ag-short", "@spawn/Upper", "", "someone"):
        with pytest.raises(ValueError):
            episode_identity(bad, "r")
    with pytest.raises(ValueError):
        episode_identity(A, "has space")
    with pytest.raises(ValueError):
        episode_identity(A, "r", role="nothex")
    with pytest.raises(ValueError):
        AnnealFiring(identity={"agent": A})  # no run


def test_no_cross_agent_leak(tmp_path):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    fb = AnnealFiring(episodic_path=db, identity={"agent": B, "run": "r2"})
    for i in range(5):
        _cap(fa, f"a{i}")
        _cap(fb, f"b{i}")
    got = recall_agent_episodes_at(db, A).episodes
    assert [e.content for e in got] == [f"a{i}" for i in (4, 3, 2, 1, 0)]
    assert all(e.metadata["agent"] == A for e in got)


def test_quiet_agent_survives_a_busy_store(tmp_path):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    fb = AnnealFiring(episodic_path=db, identity={"agent": B, "run": "r2"})
    fn = AnnealFiring(episodic_path=db)
    _cap(fa, "quiet first")
    for i in range(300):
        _cap(fb, f"busy {i}")
    for i in range(300):
        _cap(fn, f"plain {i}")
    _cap(fa, "quiet last")
    r = recall_agent_episodes_at(db, A)
    assert [e.content for e in r.episodes] == ["quiet last", "quiet first"]
    assert r.scanned == 602 and not r.truncated  # more than one page of the time read


def test_cap_applies_after_the_filter(tmp_path):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    for i in range(6):
        _cap(fa, f"a{i}")
    assert [e.content for e in recall_agent_episodes_at(db, A, limit=2).episodes] == ["a5", "a4"]


def test_window_and_truncation_are_reported(tmp_path):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    fb = AnnealFiring(episodic_path=db, identity={"agent": B, "run": "r2"})
    _cap(fa, "old")
    for i in range(20):
        _cap(fb, f"busy {i}")
    r = recall_agent_episodes_at(db, A, max_scan=10)
    assert r.truncated and r.scanned == 10 and r.episodes == ()  # absence is flagged, not claimed
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(days=45)
    assert recall_agent_episodes_at(db, A, now=future).episodes == ()  # outside 30 days


def test_missing_store_is_an_empty_memory(tmp_path):
    r = recall_agent_episodes_at(tmp_path / "nope.db", A)
    assert r.episodes == () and r.scanned == 0 and not r.truncated


# --- P4 FIX1 (L1/L2 + L3 r1) ---------------------------------------------------------------------------


def _raw_store(db):
    from anneal_memory import Store
    return Store(db)


def test_k1_identity_survives_the_real_serialize_rebuild_path(tmp_path):
    """The condenser fork/reload path: dump -> validate rebuilds the firing from serialized data."""
    pytest.importorskip("openhands.sdk", reason="openhands extra not installed")
    from openhands.sdk.context.condenser import NoOpCondenser
    from levain.firing.contract import build_firing
    from levain.firing.openhands.condenser import VagusCondenser

    db = _store_path(tmp_path)
    ident = {"agent": A, "run": "occ-7", "role": ROLE}
    orig = VagusCondenser(inner=NoOpCondenser(), firing_kind="anneal", identity=ident)
    restored = VagusCondenser.model_validate(orig.model_dump())
    assert restored.identity == ident
    fired = restored._firing
    fired._episodic_path = db  # a scratch store, never the host's
    _cap(fired, "after fork", agent=B)
    ep = recall_agent_episodes_at(db, A).episodes[0]
    assert ep.content == "after fork" and ep.metadata["run"] == "occ-7" and ep.metadata["role"] == ROLE
    assert recall_agent_episodes_at(db, B).episodes == ()
    # build_firing is the rebuild path itself
    f2 = build_firing("anneal", identity=ident)
    f2._episodic_path = db
    _cap(f2, "second")
    assert [e.content for e in recall_agent_episodes_at(db, A).episodes][0] == "second"


def test_k1_a_kind_that_cannot_carry_identity_refuses_it():
    from levain.firing.contract import build_firing
    with pytest.raises(ValueError):
        build_firing("stub", identity={"agent": A, "run": "r"})


def test_k2_unstamped_firing_strips_reserved_keys(tmp_path):
    db = _store_path(tmp_path)
    _cap(AnnealFiring(episodic_path=db), "forged", agent=B, run="x", role=ROLE, keep="yes")
    assert recall_agent_episodes_at(db, B).episodes == ()
    with _raw_store(db) as st:
        meta = st.recall().episodes[0].metadata
    assert "agent" not in meta and "run" not in meta and "role" not in meta and meta["keep"] == "yes"


def _insert_between_pages(monkeypatch, db):
    """Make any second query to the store see one new episode recorded after the first query."""
    from anneal_memory import Store
    real = Store.recall
    state = {"n": 0}

    def wrapped(self, *a, **kw):
        out = real(self, *a, **kw)
        state["n"] += 1
        if state["n"] == 1:
            w = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "late"})
            _cap(w, "recorded mid-scan")
        return out

    monkeypatch.setattr(Store, "recall", wrapped)
    return state


def test_k3_a_writer_between_reads_never_duplicates(tmp_path, monkeypatch):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    for i in range(12):
        _cap(fa, f"a{i}")
    monkeypatch.setattr(recall_mod, "_PAGE", 5, raising=False)  # a paged read would page here; the one-snapshot read ignores it
    calls = _insert_between_pages(monkeypatch, db)
    got = recall_agent_episodes_at(db, A, limit=50).episodes
    ids = [e.id for e in got]
    assert len(ids) == len(set(ids))
    assert [e.content for e in got] == [f"a{i}" for i in range(11, -1, -1)]
    assert calls["n"] == 1                                  # one snapshot: a paged read would query again
    assert "recorded mid-scan" not in [e.content for e in got]


def test_r2_bad_arguments_refused_on_both_entry_points_and_deep_metadata_is_unreadable(tmp_path):
    missing = tmp_path / "nowhere.db"
    for bad in ({"limit": 0}, {"limit": 2.5}, {"max_scan": True}, {"window_days": "3"}, {"window_days": 10**9}):
        with pytest.raises(ValueError):
            recall_agent_episodes_at(missing, A, **bad)
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    _cap(fa, "kept")
    deep = json.dumps("[" * 5000 + "]" * 5000)          # a double-encoded string nested past the parser's limit
    with _raw_store(db) as st:
        st.record("deep", "observation", source="t", metadata=deep)
    got = recall_agent_episodes_at(db, A)
    assert [e.content for e in got.episodes] == ["kept"] and got.unreadable >= 1


def test_k4_window_is_bounded_on_both_ends(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    _cap(fa, "now-ish")
    hist = datetime.now(timezone.utc) - timedelta(days=5)
    # a historical now: the episode recorded after it is outside the window
    assert recall_agent_episodes_at(db, A, now=hist).episodes == ()
    # a future-dated row is outside the window of the real now
    with _raw_store(db) as st:
        st.record("future", "observation", source="t", metadata={"agent": A, "run": "r"},
                  )
        fut = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        st._conn.execute("UPDATE episodes SET timestamp = ? WHERE content = 'future'", (fut,))
        st._conn.commit()
    got = [e.content for e in recall_agent_episodes_at(db, A).episodes]
    assert got == ["now-ish"]


def test_k5_naive_now_refused_and_aware_now_converted(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    _cap(fa, "x")
    with pytest.raises(ValueError):
        recall_agent_episodes_at(db, A, now=datetime.now())
    tz = timezone(timedelta(hours=14))
    aware = datetime.now(timezone.utc).astimezone(tz)
    assert [e.content for e in recall_agent_episodes_at(db, A, now=aware + timedelta(seconds=5)).episodes] == ["x"]
    # same instant, 14h-ahead zone: the window must not shift by 14 hours (a pre-fix until would sit 14h ahead)
    behind = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-14))) - timedelta(hours=1)
    assert recall_agent_episodes_at(db, A, now=behind).episodes == ()


def test_k6_string_metadata_is_parsed_and_unreadable_is_counted(tmp_path):
    import json
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    _cap(fa, "good")
    with _raw_store(db) as st:
        st.record("double", "observation", source="t",
                  metadata=json.dumps({"agent": A, "run": "r"}))  # stored as a JSON string of JSON
        st.record("scalar", "observation", source="t", metadata=[1, 2])  # not a mapping
    r = recall_agent_episodes_at(db, A)
    assert sorted(e.content for e in r.episodes) == ["double", "good"]
    assert r.unreadable == 1


def test_k7_truncated_only_when_the_answer_could_change(tmp_path):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    for i in range(6):
        _cap(fa, f"a{i}")
    full = recall_agent_episodes_at(db, A, limit=2, max_scan=5)
    assert [e.content for e in full.episodes] == ["a5", "a4"] and not full.truncated
    short = recall_agent_episodes_at(db, A, limit=10, max_scan=5)
    assert short.truncated


def test_k8_shapes_come_from_their_owners():
    from levain.agents.role import ROLE_NAME, spawn_namespace
    from levain.agents.spec import AGENT_ID
    from levain.firing import anneal
    assert anneal._IDENT_AGENT_ID is AGENT_ID and anneal._IDENT_SPAWN_NAME is ROLE_NAME
    assert episode_identity(spawn_namespace("reviewer"), "r")["agent"] == "@spawn/reviewer"
