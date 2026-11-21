"""The differential test for the cockpit read layer over a FIXED corpus (no randomness): the same install read by a bare
cockpit (the oracle) and by one attached to the snapshot layer (the subject), at the derive layer and at the request layer.
Every difference is a triple (panel, path, oracle value, subject value) that ``tests/cockpit_diff_approved.json`` either
explains with a reason or that fails the test. The canaries prove the harness SEES a difference; they do not prove the
subject right. Design: project_memory/1121-12_lanes/differential_plan.md (DIFF-0)."""
from __future__ import annotations

import copy
from datetime import date

import pytest

from levain.cockpit import engine as engine_mod
from levain.cockpit import snapshot_host as sh
from levain.dashboard import SubstrateSource
from tests import _cockpit_diff_rig as rig
from tests.test_cockpit_k2a import _install as _k2a_install
from tests.test_dashboard import _make_levain_install


def _k2a(tmp_path):
    return _k2a_install(tmp_path)[1]


def _dashboard(tmp_path):
    return SubstrateSource.local(_make_levain_install(tmp_path))


CORPUS = (rig.RigConfig("k2a", _k2a), rig.RigConfig("dashboard", _dashboard))
IDS = [c.name for c in CORPUS]


@pytest.fixture(scope="module")
def pairs(tmp_path_factory):
    """One pair per corpus case, built once: the reads below change nothing in them."""
    built = {c.name: rig.build_pair(c, tmp_path_factory.mktemp(c.name)) for c in CORPUS}
    yield built
    for p in built.values():
        p.close()


@pytest.fixture(scope="module")
def triples(pairs):
    return {name: rig.compare(p) for name, p in pairs.items()}


@pytest.fixture(scope="module")
def approved():
    return rig.load_approved()


# ------------------------------------------------------------------------------------------------------- the approved list


def test_the_approved_list_is_sound(approved):
    assert rig.entry_problems(approved) == []


def test_every_entry_review_date_is_ahead(approved):
    stale = [e["id"] for e in approved if e["review_by"] < date.today().isoformat()]
    assert stale == [], f"approved differences past their review_by date: {stale}"


def _entry(**over):
    e = {"id": "AD-x", "scope": "manifest", "path": "panels.*.stale", "kind": "extra_key_in_subject",
         "reason": "a reason long enough to say which code produces the difference and why it is meant",
         "source": "levain.cockpit.engine.Cockpit._head", "owner": "levain-cockpit", "added": "2026-11-21",
         "review_by": "2027-02-21"}
    e.update(over)
    return e


def test_a_sound_entry_passes_the_entry_check():
    assert rig.entry_problems([_entry()]) == []


@pytest.mark.parametrize("reason", ["", "   ", None, "short", "TODO", "a" * 39])
def test_an_entry_with_an_empty_or_thin_reason_fails_the_entry_check(reason):
    assert any("reason" in p for p in rig.entry_problems([_entry(reason=reason)]))


def test_an_entry_with_no_reason_key_fails_the_entry_check():
    e = _entry()
    del e["reason"]
    assert any("reason" in p for p in rig.entry_problems([e]))


def test_an_entry_with_no_source_a_bad_rule_or_a_repeated_id_fails_the_entry_check():
    e = _entry()
    del e["source"]
    assert any("source" in p for p in rig.entry_problems([e]))
    assert any("registry" in p for p in rig.entry_problems([_entry(kind="value_rule", rule="no_such_rule")]))
    assert any("repeated" in p for p in rig.entry_problems([_entry(), _entry()]))


def test_a_path_glob_matches_one_segment_with_a_star_and_a_run_with_two():
    assert rig.glob_match("panels.*.stale", ("panels", "x", "stale"))
    assert not rig.glob_match("panels.*.stale", ("panels", "x", "y", "stale"))
    assert rig.glob_match("panels.**.stale", ("panels", "x", "y", "stale"))
    assert rig.glob_match("panels.*.stale|pending", ("panels", "x", "pending"))
    assert not rig.glob_match("panels.*.stale", ("panels", "x", "pending"))


def test_the_diff_reads_dicts_by_key_and_lists_by_position():
    assert rig.diff({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}) == []
    got = rig.diff({"a": 1, "b": [1, 2]}, {"a": 1, "b": [2, 1], "c": 0})
    assert [(t.dotted(), t.kind) for t in got] == [("b.0", "value"), ("b.1", "value"), ("c", "extra_key_in_subject")]
    assert [t.kind for t in rig.diff({"a": 1}, {})] == ["missing_key_in_subject"]
    assert [t.kind for t in rig.diff({"a": [1]}, {"a": [1, 2]})] == ["value"]


# ------------------------------------------------------------------------------------------------------------- the corpus


def test_the_corpus_reads_something_at_both_layers(pairs):
    for name, p in pairs.items():
        composed = [pid for pid in p.attached.panel_ids if p.attached.serve_composed(pid)]
        assert len(p.derived) > 5 and "edits" in composed, name
        assert p.attached.change_probe() is not None and p.bare.change_probe() is None, name


@pytest.mark.parametrize("name", IDS)
def test_the_oracle_read_twice_is_the_same_read(pairs, name):
    """A path that differs old-against-old is an input the harness has not fixed, not a difference to approve."""
    again = rig.self_diff(pairs[name])
    assert again == [], "\n".join(rig.render(t) for t in again[:20])


@pytest.mark.parametrize("name", IDS)
def test_every_difference_between_oracle_and_subject_is_approved(triples, approved, name):
    verdict = rig.classify(triples[name], approved)
    lines = [f"[{t.scope}] panel={t.panel} {rig.render(t)}" for t in verdict.unmatched]
    assert not verdict.unmatched, f"{len(lines)} unapproved difference(s) in corpus {name}:\n" + "\n".join(lines[:40])


def test_no_approved_entry_is_dead(triples, approved):
    """An entry that explained nothing on the fixed corpus is a difference that stopped existing: delete it."""
    hit: set = set()
    for t in triples.values():
        hit.update(rig.classify(t, approved).matched)
    assert sorted(e["id"] for e in approved if e["id"] not in hit) == []


def test_the_etags_differ_by_construction_but_each_side_agrees_with_itself(pairs):
    """The metamorphic half of the etag entries: a side's manifest etag is the etag its payload carries, and a second read
    gives the same tag; the bare one and the attached one are different tags for the same panel."""
    for name, p in pairs.items():
        for ck in (p.bare, p.attached):
            manifest = ck.manifest(rig.CRED)["panels"]
            for pid in p.derived:
                assert ck.panel(pid)["etag"] == manifest[pid]["etag"] == ck.manifest(rig.CRED)["panels"][pid]["etag"], (name, pid)
        assert any(p.bare.panel(pid)["etag"] != p.attached.panel(pid)["etag"] for pid in p.derived), name


# --------------------------------------------------------------------------------------------------------------- canaries
# Each builds its own pair: the shared one holds heads decoded before the patch, and a patched decode would never run.


@pytest.fixture()
def fresh(tmp_path):
    p = rig.build_pair(CORPUS[0], tmp_path)
    yield p
    p.close()


def _break_decode(monkeypatch, mutate):
    real = sh.decode_snap

    def broken(status, body, *, as_of, error=None):
        snap = real(status, body, as_of=as_of, error=error)
        mutate(snap)
        return snap

    monkeypatch.setattr(sh, "decode_snap", broken)
    monkeypatch.setattr(engine_mod, "decode_snap", broken)


def _unmatched_on(pair, approved):
    return rig.classify(rig.compare(pair), approved).unmatched


def test_canary_a_decode_that_drops_a_row_is_seen(fresh, approved, triples, monkeypatch):
    base = len(rig.classify(triples["k2a"], approved).unmatched)

    def drop(snap):
        if snap.rows:
            snap.rows = snap.rows[1:]

    _break_decode(monkeypatch, drop)
    assert len(_unmatched_on(fresh, approved)) > base


def test_canary_a_decode_that_changes_a_status_is_seen(fresh, approved, triples, monkeypatch):
    base = len(rig.classify(triples["k2a"], approved).unmatched)
    _break_decode(monkeypatch, lambda snap: setattr(snap, "status", "error"))
    assert len(_unmatched_on(fresh, approved)) > base


def test_canary_an_oracle_that_changes_between_two_reads_is_seen(fresh):
    calls = []
    real = fresh.bare.manifest

    def drifting(*a, **kw):
        out = dict(real(*a, **kw))
        out["drift"] = len(calls)
        calls.append(1)
        return out

    fresh.bare.manifest = drifting
    assert [t.dotted() for t in rig.self_diff(fresh)] == ["manifest.drift"]


def test_canary_an_approved_entry_with_an_empty_reason_fails_the_entry_check(approved):
    broken = copy.deepcopy(approved)
    broken[0]["reason"] = ""
    assert rig.entry_problems(broken)


def test_canary_an_unapproved_extra_key_stays_unmatched(approved):
    t = rig.Triple(("manifest", "panels", "x", "surprise"), "extra_key_in_subject", None, 1)
    assert rig.classify([t], approved).unmatched == [t]
