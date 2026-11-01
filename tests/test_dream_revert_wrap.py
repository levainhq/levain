"""levain.dream.snapshot rollback with anneal's ``revert_wrap`` (D1 revert wiring).

The installed anneal may be 1.0.2 (no ``revert_wrap``): the text-only path then runs for real (the existing
test_dream_snapshot tests). Here a fake store object exposing ``revert_wrap`` stands in for anneal main, on a real
format-2 scratch store, so the ORDER of calls and the record in rollback.json are asserted. The real anneal main run is
the live proof (project_memory/1031-38/revert_live_proof.txt)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.dream import cli as dcli
from levain.dream import snapshot as sn
from tests.test_dream_snapshot import add_episode, dream, occ, scratch, store_state, text_n  # noqa: F401


class WrapRevertRefused(Exception):
    def __init__(self, code, detail="d"):
        self.code, self.detail = code, detail
        super().__init__(f"revert_wrap refused ({code}): {detail}")


class LeaseFencedError(Exception):
    pass


def receipt(**kw):
    base = dict(wrap_id=2, revert_id=1, episodes_reoffered=1, window_after=1, last_wrap_id_after=1,
                already_reverted=False, identity_version=7, pattern_state_exact=True)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeAnneal:
    """Stands in for ``Store``: records the call and the live text hashes at the moment of it."""

    def __init__(self, db, result, project_from=None, log=None):
        self.db, self.result, self.project_from, self.calls, self.log = db, result, project_from, [], log

    def revert_wrap(self, wrap_id, **kw):
        self.calls.append((wrap_id, kw))
        self.hashes_at_call = sn.tier_hashes(self.db)
        if self.log is not None:
            self.log.append("revert")
        if isinstance(self.result, BaseException):
            raise self.result
        if self.project_from is not None and not self.result.already_reverted:   # anneal re-projects the identity
            for p in sn.text_tier(self.db).values():
                if (self.project_from / p.name).exists():
                    shutil.copy2(self.project_from / p.name, p)
        return self.result

    def close(self):
        pass


@pytest.fixture
def two(scratch, monkeypatch):
    assert dream(scratch, "o1", text_n(1)).outcome == "saved"
    add_episode(scratch, "two")
    assert dream(scratch, "o2", text_n(2)).outcome == "saved"
    log: list[str] = []
    real = sn._atomic_copy
    monkeypatch.setattr(sn, "_atomic_copy", lambda s, d: (log.append("text"), real(s, d))[1])

    def install(result, project=False):
        fake = FakeAnneal(scratch, result, scratch.parent / "dream/o2/snap" if project else None, log)
        monkeypatch.setattr(sn, "_revert_store", lambda db: fake)
        return fake
    return SimpleNamespace(db=scratch, log=log, install=install, o2=occ(scratch, "o2"))


def _rec(t):
    return json.loads((t.o2 / "rollback.json").read_text())


def test_identity_version_set_means_anneal_restored_and_levain_writes_no_text(two):
    fake = two.install(receipt(identity_version=7, episodes_reoffered=3), project=True)
    res = sn.rollback(two.db, two.o2, who="me", lease_token="tok")
    assert res.ok and res.outcome == "rolled-back"
    assert two.log == ["revert"]                                   # no Levain text write, before or after
    wrap_id, kw = fake.calls[0]
    assert wrap_id == 2 and kw["reason"] == "levain dream rollback o2" and kw["actor"] == "levain-dream:me"
    assert kw["lease_token"] == "tok" and kw["allow_missing"] is False
    # the live files were still the dream's at the call, and its text was kept before it
    assert fake.hashes_at_call == json.loads((two.o2 / "snap/manifest.json").read_text())["post_files"]
    assert "Day 2." in (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text()
    assert "Day 1." in (two.db.parent / "memory.continuity.md").read_text()
    rec = _rec(two)["revert_wrap"]
    assert rec["receipt"]["identity_version"] == 7 and rec["receipt"]["episodes_reoffered"] == 3
    assert set(rec["receipt"]) == {"wrap_id", "revert_id", "episodes_reoffered", "window_after", "last_wrap_id_after",
                                   "already_reverted", "identity_version", "pattern_state_exact"}
    assert "called_at" in rec and "reverted_at" in rec
    assert "the day's 3 episodes will be offered to the next dream" in res.message
    assert res.card["revert_wrap"]["episodes_reoffered"] == 3
    assert json.loads((two.o2 / "snap/manifest.json").read_text())["reverted_at"]


def test_identity_version_none_means_levain_restores_the_text_after_the_revert(two):
    fake = two.install(receipt(identity_version=None))
    res = sn.rollback(two.db, two.o2)
    assert res.ok
    assert two.log[0] == "revert" and "text" in two.log[1:]        # the text step comes AFTER the call
    assert fake.hashes_at_call == json.loads((two.o2 / "snap/manifest.json").read_text())["post_files"]
    assert "Day 1." in (two.db.parent / "memory.continuity.md").read_text()
    assert _rec(two)["revert_wrap"]["receipt"]["identity_version"] is None


def test_a_rerun_after_a_revert_that_committed_finishes_the_owed_text_step(two, monkeypatch):
    two.install(receipt(identity_version=None))
    monkeypatch.setattr(sn, "_restore_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        sn.rollback(two.db, two.o2)
    mid = _rec(two)["revert_wrap"]
    assert mid["receipt"] and "reverted_at" in mid                  # progress marked right after the call
    assert "Day 2." in (two.db.parent / "memory.continuity.md").read_text()     # the text step is still owed
    monkeypatch.undo()
    real = sn._atomic_copy
    two.log.clear()
    monkeypatch.setattr(sn, "_atomic_copy", lambda s, d: (two.log.append("text"), real(s, d))[1])
    fake = two.install(receipt(already_reverted=True, identity_version=None, episodes_reoffered=0))
    res = sn.rollback(two.db, two.o2)
    assert res.ok and res.outcome in ("finished", "rolled-back")
    assert "Day 1." in (two.db.parent / "memory.continuity.md").read_text()
    assert two.log == ["revert", "text"] or two.log[-1] == "text"
    assert (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text().count("Day 2.") == 1   # first copy kept
    assert len(fake.calls) == 1


def test_already_reverted_with_an_identity_version_is_success_and_writes_no_text(two):
    two.install(receipt(already_reverted=True, identity_version=7, episodes_reoffered=0), project=True)
    res = sn.rollback(two.db, two.o2)
    assert res.ok and two.log == ["revert"]
    assert _rec(two)["revert_wrap"]["receipt"]["already_reverted"] is True


@pytest.mark.parametrize("code", ["not_latest", "unknown_wrap", "wrap_open", "identity_moved", "no_prior_identity",
                                  "episodes_gone", "no_identity_version"])
def test_every_refusal_restores_nothing_names_the_code_and_a_remedy(two, code):
    before = (sn.tier_hashes(two.db), (two.o2 / "snap/manifest.json").read_text())
    two.install(WrapRevertRefused(code, "because"))
    res = sn.rollback(two.db, two.o2)
    assert not res.ok and res.code == code and code not in ("", None)
    assert sn.REVERT_REMEDY[code] in res.message and "because" in res.message
    assert two.log == ["revert"]                                    # no text written
    assert sn.tier_hashes(two.db) == before[0]
    m = json.loads(before[1])
    assert json.loads((two.o2 / "snap/manifest.json").read_text())["revert_started_at"] is None
    assert not (two.o2 / "rollback.json").exists() and not (two.o2 / sn.UNDO_DIR).exists()
    assert m["reverted_at"] is None


def test_episodes_gone_offers_allow_missing_and_passes_it_through(two, capsys):
    two.install(WrapRevertRefused("episodes_gone"))
    assert dcli.run_dream_rollback(SimpleNamespace(occurrence="o2", store_dir=str(two.db.parent), who="x", json=False,
                                                   allow_missing=False, lease_token=None)) == 1
    assert "--allow-missing" in capsys.readouterr().out
    fake = two.install(receipt(identity_version=None))
    assert dcli.run_dream_rollback(SimpleNamespace(occurrence="o2", store_dir=str(two.db.parent), who="x", json=False,
                                                   allow_missing=True, lease_token="L")) == 0
    out = capsys.readouterr().out
    assert fake.calls[0][1]["allow_missing"] is True and fake.calls[0][1]["lease_token"] == "L"
    assert "anneal receipt" in out and "will be offered to the next dream" in out


def test_a_lease_fence_is_a_refusal_not_a_traceback(two):
    two.install(LeaseFencedError("fenced"))
    res = sn.rollback(two.db, two.o2)
    assert not res.ok and res.code == "lease_fenced" and "lease" in res.message
    assert not (two.o2 / "rollback.json").exists() and "Day 2." in (two.db.parent / "memory.continuity.md").read_text()


def test_a_refusal_after_an_interrupted_attempt_keeps_that_attempts_marks(two, monkeypatch):
    two.install(receipt(identity_version=None))
    monkeypatch.setattr(sn, "_restore_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        sn.rollback(two.db, two.o2)
    monkeypatch.undo()
    two.install(WrapRevertRefused("not_latest"))
    assert sn.rollback(two.db, two.o2).code == "not_latest"
    assert json.loads((two.o2 / "snap/manifest.json").read_text())["revert_started_at"]
    assert (two.o2 / sn.UNDO_DIR).is_dir()


def test_without_revert_wrap_the_text_restore_runs_and_says_why(two, monkeypatch):
    monkeypatch.setattr(sn, "_revert_store", lambda db: None)
    res = sn.rollback(two.db, two.o2)
    assert res.ok and "has no revert_wrap" in res.message and "NOT re-offered" in res.message
    assert "Day 1." in (two.db.parent / "memory.continuity.md").read_text()
    assert "revert_wrap" not in (_rec(two))


def test_capability_is_the_public_surface_only(monkeypatch):
    import anneal_memory
    monkeypatch.setattr(anneal_memory.Store, "revert_wrap", None, raising=False)
    assert sn._revert_store(Path("/nonexistent/x.db")) is None
    monkeypatch.undo()


def test_the_dream_subparser_names_every_verb():
    import argparse
    ap = argparse.ArgumentParser()
    dcli.add_parsers(ap.add_subparsers())
    text = ap.format_help() + ap._subparsers._group_actions[0].choices["dream"].format_help()
    assert "only `run`" not in text
    for verb in ("run", "rollback", "recover", "drill"):
        assert verb in text
