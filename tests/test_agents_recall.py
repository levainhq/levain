"""P4: episode identity stamped at capture, and per-agent recall read back from a shared store."""
from __future__ import annotations

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


def test_window_and_truncation_are_reported(tmp_path, monkeypatch):
    db = _store_path(tmp_path)
    fa = AnnealFiring(episodic_path=db, identity={"agent": A, "run": "r1"})
    fb = AnnealFiring(episodic_path=db, identity={"agent": B, "run": "r2"})
    _cap(fa, "old")
    for i in range(20):
        _cap(fb, f"busy {i}")
    monkeypatch.setattr(recall_mod, "_PAGE", 5)
    r = recall_agent_episodes_at(db, A, max_scan=10)
    assert r.truncated and r.scanned == 10 and r.episodes == ()  # absence is flagged, not claimed
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(days=45)
    assert recall_agent_episodes_at(db, A, now=future).episodes == ()  # outside 30 days


def test_missing_store_is_an_empty_memory(tmp_path):
    r = recall_agent_episodes_at(tmp_path / "nope.db", A)
    assert r.episodes == () and r.scanned == 0 and not r.truncated
