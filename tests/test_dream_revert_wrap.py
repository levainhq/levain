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

    closed = False

    def close(self):
        self.closed = True


def gen3(monkeypatch, fake):
    """Make this process look like an anneal WITH ``revert_wrap`` on a format-3 store (the scratch store is format 2
    on PyPI anneal; on anneal main it is already 3): the capability, the open and the format read are the three seams."""
    real = sn.format_version
    monkeypatch.setattr(sn, "_has_revert_wrap", lambda: True)
    monkeypatch.setattr(sn, "format_version", lambda db: sn.REVERT_FORMAT if real(db) == sn.SUPPORTED_FORMAT else real(db))
    monkeypatch.setattr(sn, "_revert_store", lambda db: fake)
    return fake


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
        return gen3(monkeypatch, fake)
    return SimpleNamespace(db=scratch, log=log, install=install, o2=occ(scratch, "o2"), mp=monkeypatch)


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
    assert res.ok and res.outcome == "finished", res.message
    assert "Day 1." in (two.db.parent / "memory.continuity.md").read_text()
    assert two.log and set(two.log) == {"text"}                     # only text writes: anneal is never asked again
    assert fake.calls == []                                        # the receipt was recorded, so S2 finishes alone
    assert (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text().count("Day 2.") == 1   # first copy kept
    assert _rec(two)["revert_wrap"]["receipt"]["identity_version"] is None


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


def test_a_refusal_after_an_interrupted_attempt_keeps_that_attempts_marks(two):
    two.install(RuntimeError("died in the call"))                  # a crash, not a refusal: S1 (called_at, no receipt)
    with pytest.raises(RuntimeError):
        sn.rollback(two.db, two.o2)
    assert "called_at" in _rec(two)["revert_wrap"]
    two.install(WrapRevertRefused("not_latest"))
    res = sn.rollback(two.db, two.o2)
    assert res.code == "later-dream" and not res.ok
    assert json.loads((two.o2 / "snap/manifest.json").read_text())["revert_started_at"]
    assert (two.o2 / sn.UNDO_DIR).is_dir() and "called_at" in _rec(two)["revert_wrap"]


def test_without_revert_wrap_the_text_restore_runs_and_says_why(two, monkeypatch):
    monkeypatch.setattr(sn, "_has_revert_wrap", lambda: False)
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


# ------------------------------------------------------------------------- D1 revert fix2: the gen-3 state machine

class Committing(FakeAnneal):
    """A revert that COMMITS: it projects the snapshot's text (anneal re-projects from its identity), may crash after
    the commit, and answers ``already_reverted`` to every later call."""

    def __init__(self, db, result, snap, crash_after_commit=False):
        super().__init__(db, result, project_from=snap)
        self.crash_after_commit, self.committed = crash_after_commit, False

    def revert_wrap(self, wrap_id, **kw):
        self.calls.append((wrap_id, kw))
        if self.committed:
            return receipt(already_reverted=True, identity_version=7, episodes_reoffered=0)
        for p in sn.text_tier(self.db).values():
            if (self.project_from / p.name).exists():
                shutil.copy2(self.project_from / p.name, p)
        self.committed = True
        if self.crash_after_commit:
            raise RuntimeError("crash after anneal committed the revert")
        return self.result


def _live(t, name="memory.continuity.md"):
    return (t.db.parent / name).read_text()


def _manifest(t):
    return json.loads((t.o2 / "snap/manifest.json").read_text())


def _crash_in_the_call(t):
    """Leave the occurrence in S1: the record holds ``called_at`` and no receipt."""
    t.install(RuntimeError("died in the call"))
    with pytest.raises(RuntimeError, match="died in the call"):
        sn.rollback(t.db, t.o2)
    rw = _rec(t)["revert_wrap"]
    assert "called_at" in rw and "receipt" not in rw
    return rw


def test_s0_live_equal_to_pre_still_asks_anneal_there_is_no_file_hash_noop(two):
    pre = {k: e["sha256"] for k, e in _manifest(two)["files"].items()}
    for k, p in sn.text_tier(two.db).items():                      # a legitimate edit back to the snapshot's text
        if pre[k] is None:
            p.unlink(missing_ok=True)
        else:
            shutil.copy2(two.o2 / "snap" / p.name, p)
    assert sn.tier_hashes(two.db) == pre
    fake = two.install(receipt(identity_version=7), project=True)
    res = sn.rollback(two.db, two.o2)
    assert res.ok and res.outcome == "rolled-back" and len(fake.calls) == 1, res.message


def _setup_foreign(t):
    (t.db.parent / "memory.continuity.md").write_text("a stranger's text\n")


def _setup_wrap_open(t):
    t.mp.setattr(sn, "_wrap_open_ro", lambda db: True)


def _setup_damaged(t):
    e = next(e for e in _manifest(t)["files"].values() if e["sha256"] is not None)
    (t.o2 / "snap" / e["name"]).write_text("damaged")


def _setup_no_post(t):
    m = _manifest(t)
    m["post_files"] = None
    (t.o2 / "snap/manifest.json").write_text(json.dumps(m))


@pytest.mark.parametrize("code,setup", [("foreign-write", _setup_foreign), ("wrap-open", _setup_wrap_open),
                                        ("snapshot-damaged", _setup_damaged), ("no-post", _setup_no_post),
                                        ("later-dream", None)])
def test_no_write_capable_open_happens_until_every_check_has_passed(two, code, setup):
    fake = two.install(receipt())
    opened = []
    two.mp.setattr(sn, "_revert_store", lambda db: opened.append("open") or fake)
    target = two.o2
    if setup:
        setup(two)
    else:
        target = occ(two.db, "o1")                                  # the older dream while a newer one exists
    before = (sn.tier_hashes(two.db), (target / "snap/manifest.json").read_text())
    res = sn.rollback(two.db, target)
    assert res.code == code and not res.ok, res.message
    assert opened == [] and fake.calls == []
    assert sn.tier_hashes(two.db) == before[0] and (target / "snap/manifest.json").read_text() == before[1]
    assert not (target / "rollback.json").exists() and not (target / sn.UNDO_DIR).exists()


def test_the_undo_copy_is_taken_before_the_first_write_capable_open(two):
    seen = {}
    fake = two.install(receipt(identity_version=7), project=True)

    def opener(db):
        seen["undo"] = (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text()
        seen["called_at"] = _rec(two)["revert_wrap"]["called_at"]    # and the mark is durable before the open
        return fake
    two.mp.setattr(sn, "_revert_store", opener)
    assert sn.rollback(two.db, two.o2).ok
    assert "Day 2." in seen["undo"] and seen["called_at"]


def test_an_anneal_refusal_in_s0_restores_the_record_exactly_and_the_next_run_is_s0(two):
    rec0 = {"note": "an earlier record, no call in it"}
    (two.o2 / "rollback.json").write_text(json.dumps(rec0))
    before = _manifest(two)
    two.install(WrapRevertRefused("identity_moved"))
    res = sn.rollback(two.db, two.o2)
    assert res.code == "identity_moved"
    assert json.loads((two.o2 / "rollback.json").read_text()) == rec0
    assert _manifest(two) == before and not (two.o2 / sn.UNDO_DIR).exists()
    fake = two.install(receipt(identity_version=7), project=True)
    res = sn.rollback(two.db, two.o2)                               # S0 again: a first attempt, not a finished one
    assert res.ok and res.outcome == "rolled-back" and len(fake.calls) == 1, res.message


def test_the_store_is_closed_after_a_successful_revert(two):
    fake = two.install(receipt(identity_version=7), project=True)
    assert sn.rollback(two.db, two.o2).ok and fake.closed is True


def test_the_store_is_closed_after_an_anneal_refusal(two):
    fake = two.install(WrapRevertRefused("wrap_open"))
    assert not sn.rollback(two.db, two.o2).ok and fake.closed is True


def test_s1_crash_after_the_commit_then_a_later_dream_saves_then_the_rerun_reconciles(two):
    fake = gen3(two.mp, Committing(two.db, receipt(identity_version=7, episodes_reoffered=3), two.o2 / "snap",
                                   crash_after_commit=True))
    with pytest.raises(RuntimeError, match="crash after anneal"):
        sn.rollback(two.db, two.o2)
    assert "receipt" not in _rec(two)["revert_wrap"] and "Day 1." in _live(two)
    add_episode(two.db, "three")
    assert dream(two.db, "o3", text_n(3)).outcome == "saved"        # a later dream saves over the reverted text
    later = _live(two)
    assert "Day 3." in later
    res = sn.rollback(two.db, two.o2)                               # not refused as foreign, not as a later dream
    assert res.ok and res.outcome == "finished", res.message
    assert len(fake.calls) == 2                                     # S1 asked anneal first; it said already_reverted
    assert _live(two) == later and res.restored == []              # the later dream's text is untouched, not "restored"
    rw = _rec(two)["revert_wrap"]
    assert rw["receipt"]["already_reverted"] is True and _manifest(two)["reverted_at"]
    assert "Day 2." in (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text()


def test_s1_with_a_hand_edit_refuses_foreign_write_after_revert_until_allowed(two):
    _crash_in_the_call(two)
    cont = two.db.parent / "memory.continuity.md"
    cont.write_text("my hand edit\n")
    fake = two.install(receipt(identity_version=7), project=True)
    res = sn.rollback(two.db, two.o2)
    assert res.code == "foreign-write-after-revert" and not res.ok
    assert "was started" in res.message and "Nothing was overwritten" in res.message and "--allow-foreign" in res.message
    assert cont.read_text() == "my hand edit\n" and fake.calls == []
    assert _manifest(two)["revert_started_at"] and "called_at" in _rec(two)["revert_wrap"]
    res = sn.rollback(two.db, two.o2, allow_foreign=True)
    assert res.ok and res.outcome == "finished" and len(fake.calls) == 1, res.message
    assert "Day 1." in cont.read_text()


@pytest.mark.parametrize("code", ["identity_moved", "wrap_open", "episodes_gone", "lease"])
def test_s1_other_refusals_keep_the_marks_and_say_the_revert_may_have_committed(two, code):
    _crash_in_the_call(two)
    exc = LeaseFencedError("fenced") if code == "lease" else WrapRevertRefused(code, "because")
    two.install(exc)
    res = sn.rollback(two.db, two.o2)
    assert not res.ok and res.code == ("lease_fenced" if code == "lease" else code)
    assert "may already have committed" in res.message and "anneal-memory --db" in res.message and "history" in res.message
    assert _manifest(two)["revert_started_at"] and (two.o2 / sn.UNDO_DIR).is_dir()
    assert "called_at" in _rec(two)["revert_wrap"] and not _manifest(two).get("reverted_at")


def test_s1_not_latest_is_a_later_dream_block_and_keeps_the_marks(two):
    _crash_in_the_call(two)
    two.install(WrapRevertRefused("not_latest", "a newer wrap exists"))
    res = sn.rollback(two.db, two.o2)
    assert res.code == "later-dream" and "a newer wrap exists" in res.message and "marks are kept" in res.message
    assert _manifest(two)["revert_started_at"] and "called_at" in _rec(two)["revert_wrap"]


def test_s1_a_store_that_will_not_open_says_a_revert_was_started(two):
    _crash_in_the_call(two)

    def boom(db):
        raise sn.RevertStoreUnavailable("OSError: database is locked")
    two.mp.setattr(sn, "_revert_store", boom)
    res = sn.rollback(two.db, two.o2)
    assert res.code == "revert-store" and "was started" in res.message and "database is locked" in res.message
    assert "history" in res.message and _manifest(two)["revert_started_at"]


def test_s1_a_wrap_open_refusal_also_says_a_revert_was_started(two):
    _crash_in_the_call(two)
    two.mp.setattr(sn, "_wrap_open_ro", lambda db: True)
    res = sn.rollback(two.db, two.o2)
    assert res.code == "wrap-open" and "was started" in res.message and "history" in res.message


def test_a_keyboard_interrupt_is_not_swallowed_as_a_refusal_and_leaves_s1(two):
    two.install(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        sn.rollback(two.db, two.o2)
    assert "called_at" in _rec(two)["revert_wrap"] and _manifest(two)["revert_started_at"]


def test_s2_death_before_finish_finishes_without_asking_anneal_and_keeps_the_first_receipt(two, monkeypatch):
    fake = gen3(monkeypatch, Committing(two.db, receipt(identity_version=7, episodes_reoffered=3), two.o2 / "snap"))
    real_finish = sn._finish
    monkeypatch.setattr(sn, "_finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died before finish")))
    with pytest.raises(RuntimeError, match="died before finish"):
        sn.rollback(two.db, two.o2)
    assert _rec(two)["revert_wrap"]["receipt"]["episodes_reoffered"] == 3
    monkeypatch.setattr(sn, "_finish", real_finish)
    res = sn.rollback(two.db, two.o2)
    assert res.ok and res.outcome == "finished", res.message
    assert len(fake.calls) == 1                                     # S2: anneal is never asked again
    rw = _rec(two)["revert_wrap"]
    assert rw["receipt"]["episodes_reoffered"] == 3 and rw["receipt"]["already_reverted"] is False
    assert "receipt_rerun" not in rw and "receipt_reruns" not in rw
    assert res.card["revert_wrap"]["episodes_reoffered"] == 3
    assert "the day's 3 episodes will be offered" in res.message and "nothing was reopened" not in res.message
    # `restored` is the dream's post text against the post-revert files, so the rerun's card is accurate
    assert "memory.continuity.md" in res.restored and "nothing further" not in res.message
    assert "Day 2." in (two.o2 / sn.UNDO_DIR / "memory.continuity.md").read_text()


def test_s2_the_owed_text_step_never_overwrites_a_strangers_file_unless_allowed(two, monkeypatch):
    two.install(receipt(identity_version=None))
    real = sn._restore_text
    monkeypatch.setattr(sn, "_restore_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        sn.rollback(two.db, two.o2)
    monkeypatch.setattr(sn, "_restore_text", real)
    cont = two.db.parent / "memory.continuity.md"
    cont.write_text("a stranger's text\n")
    fake = two.install(receipt(identity_version=None))
    res = sn.rollback(two.db, two.o2)
    assert res.code == "foreign-write-after-revert" and cont.read_text() == "a stranger's text\n"
    assert not _manifest(two).get("reverted_at")
    res = sn.rollback(two.db, two.o2, allow_foreign=True)
    assert res.ok and "Day 1." in cont.read_text() and fake.calls == []


def test_an_unknown_reoffer_count_reads_as_an_unknown_number():
    assert sn._reoffer_text({"episodes_reoffered": None, "already_reverted": False}) == (
        "an unknown number of the day's episodes will be offered to the next dream.")
    assert sn._reoffer_text({"episodes_reoffered": 0, "already_reverted": False}) == (
        "the day's 0 episodes will be offered to the next dream.")


def test_a_receipt_with_no_count_renders_in_the_message(two):
    two.install(receipt(identity_version=7, episodes_reoffered=None), project=True)
    res = sn.rollback(two.db, two.o2)
    assert res.ok and "an unknown number of the day's episodes" in res.message and "None" not in res.message


def test_a_store_that_will_not_open_refuses_with_the_error_and_a_remedy_and_stays_s0(two, monkeypatch):
    import anneal_memory
    monkeypatch.setattr(sn, "_has_revert_wrap", lambda: True)
    real = sn.format_version
    monkeypatch.setattr(sn, "format_version", lambda db: sn.REVERT_FORMAT if real(db) == sn.SUPPORTED_FORMAT else real(db))

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("database is locked")
    monkeypatch.setattr(anneal_memory, "Store", Boom)
    before = (sn.tier_hashes(two.db), _manifest(two))
    res = sn.rollback(two.db, two.o2)
    assert not res.ok and res.code == "revert-store"
    assert "database is locked" in res.message and "Remedy" in res.message and "has no revert_wrap" not in res.message
    assert sn.tier_hashes(two.db) == before[0] and _manifest(two) == before[1]
    assert not (two.o2 / "rollback.json").exists() and not (two.o2 / sn.UNDO_DIR).exists()


def test_a_format_2_store_is_refused_before_any_open_when_the_anneal_would_migrate_it(two, monkeypatch):
    if sn.format_version(two.db) != sn.SUPPORTED_FORMAT:
        pytest.skip("scratch store is not format 2 (anneal main creates format 3)")
    monkeypatch.setattr(sn, "_has_revert_wrap", lambda: True)
    opened = []
    monkeypatch.setattr(sn, "_wrap_open", lambda db: opened.append("wrap_open") or False)
    monkeypatch.setattr(sn, "_wrap_open_ro", lambda db: opened.append("wrap_open_ro") or False)
    monkeypatch.setattr(sn, "_revert_store", lambda db: opened.append("revert_store"))
    db_bytes = two.db.read_bytes()
    res = sn.rollback(two.db, two.o2)
    assert not res.ok and res.code == "format" and "migrate it" in res.message and "Remedy" in res.message
    assert opened == [] and two.db.read_bytes() == db_bytes             # no Store() open, the db untouched


def test_the_rollback_verb_takes_allow_foreign_and_passes_it_on(two, monkeypatch):
    import argparse
    ap = argparse.ArgumentParser()
    dcli.add_parsers(ap.add_subparsers())
    dream_p = ap._subparsers._group_actions[0].choices["dream"]
    assert "--allow-foreign" in dream_p._subparsers._group_actions[0].choices["rollback"].format_help()
    seen = {}
    monkeypatch.setattr(sn, "rollback", lambda *a, **k: (seen.update(k), sn.RollbackResult("refused", "x", code="x"))[1])
    dcli.run_dream_rollback(SimpleNamespace(occurrence="o2", store_dir=str(two.db.parent), who="x", json=False,
                                            allow_missing=False, lease_token=None, allow_foreign=True))
    assert seen["allow_foreign"] is True
