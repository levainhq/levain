"""The differential test for the cockpit read layer over a FIXED corpus: the same install read by a bare cockpit (the
oracle) and by one attached to the snapshot layer (the subject), at the derive layer and at the request layer. Every
difference is a triple (panel, path, oracle value, subject value) that ``tests/cockpit_diff_approved.json`` either explains
with a reason or that fails the test. The canaries prove the harness SEES a difference; they do not prove the subject right.

Known limits, each owned elsewhere or accepted:
  * Kernel panels on a levain rig only. Both corpora are installs without verbs, labels, error rows or provider-declared
    freshness; more states belong to DIFF-1.
  * The value rule proves "fresh at rest" only. Mutating a row and republishing, to show an etag moves with content (the
    metamorphic half of the etag question), and convergence belong to DIFF-2 (levain#245); this file holds only that
    distinct panels carry distinct etags and that the serve-composed panels, the control, carry equal ones.
  * The k2a install dates its seeds with a fixed calendar date while the clock is the real date, so a date band in that
    corpus can move with the day the suite runs. Both sides read the same install in one run, so it cannot split them."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from levain.cockpit import engine as engine_mod
from levain.cockpit import snapshot_host as sh
from levain.dashboard import SubstrateSource
from levain.desk import board_context, board_store, desk_port
from tests import _cockpit_diff_rig as rig
from tests.test_cockpit_k2a import _install as _k2a_install
from tests.test_dashboard import _make_levain_install

FRESH_KEYS = ("valid_until", "stale", "pending")
# every variable the cockpit's kernel panels read from the environment to find a store outside the install
KERNEL_ENV = (board_store.ITEMS_ENV, board_context.ANNEAL_ENV, board_context.FLOW_ENV, desk_port.DESKCMD_ENV)
MIN_HEALTHY_SHARE = 0.5      # of the derived panels, the share whose ORACLE read is a read and not a failure


def _k2a(tmp_path):
    return _k2a_install(tmp_path)[1]


def _dashboard(tmp_path):
    return SubstrateSource.local(_make_levain_install(tmp_path))


CORPUS = (rig.RigConfig("k2a", _k2a), rig.RigConfig("dashboard", _dashboard))
IDS = [c.name for c in CORPUS]


def _scoped_env(home, mp):
    mp.setenv("LEVAIN_HOME", str(home))
    for name in KERNEL_ENV:
        mp.delenv(name, raising=False)


@pytest.fixture(scope="module")
def scratch_home(tmp_path_factory):
    """The module's fixtures run before any per-test isolation, so they bring their own: the home and every store variable
    point at a scratch dir (or nowhere) for as long as a pair built here can be read."""
    home = tmp_path_factory.mktemp("levain_home")
    with pytest.MonkeyPatch.context() as mp:
        _scoped_env(home, mp)
        yield home


@pytest.fixture(autouse=True)
def _pin_home(scratch_home, monkeypatch):
    """The reads inside a test run under the home the pairs were built under, not a per-test one."""
    _scoped_env(scratch_home, monkeypatch)


@pytest.fixture(scope="module")
def pairs(tmp_path_factory, scratch_home):
    """One pair per corpus case, built once: the reads below change nothing in them."""
    built: dict = {}
    try:
        for c in CORPUS:
            built[c.name] = rig.build_pair(c, tmp_path_factory.mktemp(c.name))
        yield built
    finally:
        for p in built.values():
            p.close()


@pytest.fixture(scope="module")
def triples(pairs):
    return {name: rig.compare(p) for name, p in pairs.items()}


@pytest.fixture(scope="module")
def approved():
    return rig.load_approved()


def _verdict(pairs, triples, approved, name):
    return rig.classify(triples[name], approved, pairs[name].config.now)


# ------------------------------------------------------------------------------------------------------- the approved list


def test_the_approved_list_is_sound(approved):
    assert rig.entry_problems(approved) == []


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
    assert any("registry" in p for p in rig.entry_problems([_entry(kind="extra_key_with_rule", rule="no_such_rule")]))
    assert any("repeated" in p for p in rig.entry_problems([_entry(), _entry()]))


@pytest.mark.parametrize("over,word", [
    ({"scope": "nowhere"}, "scope"),
    ({"kind": "swallow"}, "kind"),
    ({"owner": ""}, "owner"),
    ({"owner": None}, "owner"),
    ({"id": "x-1"}, "id"),
    ({"path": ""}, "path"),
    ({"path": "**"}, "literal"),
    ({"path": "*.*"}, "literal"),
    ({"added": "21-11-2026"}, "added"),
    ({"added": "2026-2-1"}, "added"),
    ({"review_by": "soon"}, "review_by"),
    ({"review_by": "2026-11-21"}, "later than added"),
    ({"review_by": "2026-01-01"}, "later than added"),
])
def test_each_branch_of_the_entry_check_rejects_its_fault(over, word):
    assert any(word in p for p in rig.entry_problems([_entry(**over)])), over


def test_the_entry_check_rejects_a_list_that_is_not_a_list_of_objects():
    assert rig.entry_problems({}) and rig.entry_problems(["x"])


def test_the_review_date_is_data_only_a_past_one_fails_nothing(approved):
    """A date reached with no change to blame must not turn the suite red: ``review_by`` is checked for form only."""
    assert rig.entry_problems([_entry(added="2020-01-01", review_by="2020-02-01")]) == []


def test_every_entry_source_names_symbols_that_resolve(approved):
    for e in approved:
        names = rig.source_symbols(e["source"])
        assert names, e["id"]
        for name in names:
            assert rig.resolve_symbol(name) is not None, (e["id"], name)


def test_a_source_that_does_not_resolve_is_seen():
    assert rig.source_symbols("a.b.C.d (some words), a.b.E.f") == ["a.b.C.d", "a.b.E.f"]
    with pytest.raises((ImportError, AttributeError)):
        rig.resolve_symbol("levain.cockpit.engine.Cockpit._renamed_away")
    with pytest.raises((ImportError, AttributeError)):
        rig.resolve_symbol("levain.no_such_module.Thing")


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


def test_every_pair_was_built_under_the_scratch_home(pairs, scratch_home):
    for name, p in pairs.items():
        assert p.levain_home, f"{name}: built with LEVAIN_HOME unset"
        assert Path(p.levain_home).resolve().is_relative_to(scratch_home.resolve()), (name, p.levain_home)


def test_the_corpus_reads_something_at_both_layers(pairs):
    for name, p in pairs.items():
        composed = [pid for pid in p.attached.panel_ids if p.attached.serve_composed(pid)]
        assert p.derived and composed, name
        assert set(composed) <= engine_mod.SERVE_COMPOSED_IDS, name
        assert p.attached.change_probe() is not None and p.bare.change_probe() is None, name


@pytest.mark.parametrize("name", IDS)
def test_the_panels_that_are_compared_are_mostly_reads_not_failures(pairs, name):
    """An error that appears on both sides compares equal, so it must not be counted as coverage."""
    p = pairs[name]
    for obs in (rig.observe_derive_oracle(p), rig.observe_derive_subject(p)):
        assert not [pid for pid, o in obs.items() if "raised" in o]
    oracle = rig.observe_derive_oracle(p)
    healthy = [pid for pid, o in oracle.items() if o.get("status") in rig.HEALTHY]
    assert len(healthy) >= MIN_HEALTHY_SHARE * len(p.derived), (name, sorted(set(p.derived) - set(healthy)))


@pytest.mark.parametrize("name", IDS)
def test_every_request_the_rig_makes_is_a_read_not_a_raise(pairs, name):
    """The full, the compact and a by-row read of every panel, on each side: a request that raises on both sides compares
    equal, so it is checked here that none does, and that the by-row read was asked for at all."""
    p = pairs[name]
    for side, ck in (("bare", p.bare), ("attached", p.attached)):
        obs = rig.observe_request(ck, p.derived)
        assert rig.raised_paths(obs) == [], (name, side)
        assert set(obs["payload"]) == set(ck.panel_ids), (name, side)
        for pid, per in obs["payload"].items():
            assert {"full", "compact"} <= set(per), (name, side, pid)
        assert obs["_rows_asked"], (name, side)
        assert any(k.startswith("row:") for per in obs["payload"].values() for k in per), (name, side)


def test_a_read_that_raised_is_found_wherever_it_sits():
    assert rig.raised_paths({"a": {"raised": "X: y"}, "b": [1, {"raised": "Z: w"}], "c": {"raised": 1, "d": 2}}) == [
        ("a",), ("b", 1)]
    assert rig.raised_paths({"a": 1}) == []
    assert rig.triple_raised(rig.Triple(("payload", "x", "full"), "value", {"raised": "E: e"}, {"rows": []}))
    assert not rig.triple_raised(rig.Triple(("payload", "x", "full", "rows"), "value", [1], []))


@pytest.mark.parametrize("name", IDS)
@pytest.mark.parametrize("side", ["bare", "attached"])
def test_a_side_read_twice_is_the_same_read(pairs, name, side):
    """A path that differs side-against-itself is an input the harness has not fixed, not a difference to approve."""
    again = rig.self_diff(pairs[name], side)
    assert again == [], "\n".join(rig.render(t) for t in again[:20])


@pytest.mark.parametrize("name", IDS)
def test_every_difference_between_oracle_and_subject_is_approved(pairs, triples, approved, name):
    verdict = _verdict(pairs, triples, approved, name)
    lines = [f"[{t.scope}] panel={t.panel} {rig.render(t)}" for t in verdict.unmatched]
    assert not verdict.unmatched, f"{len(lines)} unapproved difference(s) in corpus {name}:\n" + "\n".join(lines[:40])


def test_no_approved_entry_is_dead(pairs, triples, approved):
    """An entry that explained nothing on the fixed corpus is a difference that stopped existing: delete it."""
    hit: set = set()
    for name in pairs:
        hit.update(_verdict(pairs, triples, approved, name).matched)
    assert sorted(e["id"] for e in approved if e["id"] not in hit) == []


# --------------------------------------------------------------------------------------------------------------- freshness


@pytest.mark.parametrize("name", IDS)
def test_the_subject_carries_each_freshness_key_and_says_fresh_at_rest(pairs, name):
    """Presence and value of what the entries only allow: every panel head the manifest lists (row-backed and
    serve-composed alike) and every panel's own payload has the three keys; the row-backed ones are fresh at rest."""
    p = pairs[name]
    ck, now = p.attached, p.config.now
    manifest = ck.manifest(rig.CRED)
    assert set(ck.panel_ids) <= set(manifest["panels"])
    fresh_map = ck.freshness()
    for pid in ck.panel_ids:
        for where, head in (("manifest", manifest["panels"][pid]), ("payload", ck.panel(pid))):
            for key in FRESH_KEYS:
                assert key in head, (name, pid, where, key)
            assert head["stale"] is False and head["pending"] is False, (name, pid, where)
            assert rig._iso_not_before(head["valid_until"], now), (name, pid, where, head["valid_until"])
    for pid in p.derived:
        assert fresh_map[pid]["stale"] is False, (name, pid)
    assert rig._fresh_block_at_rest(manifest["freshness"]), (name, manifest["freshness"])


_BLOCK = {"worker": "live", "heartbeat_age_s": 0.5, "deriving": [], "stream": {"state": "up"}}


@pytest.mark.parametrize("path,kind,value,ok", [
    (("manifest", "panels", "x", "stale"), "extra_key_in_subject", False, True),
    (("manifest", "panels", "x", "stale"), "extra_key_in_subject", True, False),
    (("manifest", "panels", "x", "stale"), "extra_key_in_subject", 0, False),
    (("manifest", "panels", "x", "pending"), "extra_key_in_subject", False, True),
    (("manifest", "panels", "x", "pending"), "extra_key_in_subject", True, False),
    (("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", None, True),
    (("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", "2999-01-01T00:00:00+00:00", True),
    (("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", "2000-01-01T00:00:00+00:00", False),
    (("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", "soon", False),
    (("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", 5, False),
    (("payload", "x", "full", "stale"), "extra_key_in_subject", True, False),
    (("payload", "x", "compact", "pending"), "extra_key_in_subject", True, False),
    (("freshness", "x", "stale"), "extra_key_in_subject", True, False),
    (("freshness", "x", "stale"), "extra_key_in_subject", False, True),
    (("manifest", "freshness"), "extra_key_in_subject", _BLOCK, True),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, worker="down"), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, deriving=["x"]), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, unexpected=1), False),
    (("manifest", "freshness"), "extra_key_in_subject", {k: v for k, v in _BLOCK.items() if k != "stream"}, False),
    (("manifest", "freshness"), "extra_key_in_subject", {k: v for k, v in _BLOCK.items() if k != "heartbeat_age_s"}, False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, heartbeat_age_s="garbage"), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, heartbeat_age_s=-1), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, heartbeat_age_s=True), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, heartbeat_age_s=None), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, stream="up"), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, stream={}), False),
    (("manifest", "freshness"), "extra_key_in_subject", dict(_BLOCK, stream={"state": 1}), False),
    (("manifest", "freshness"), "extra_key_in_subject", None, False),
    # a freshness key whose value DIFFERS (both sides had it) is a value triple, never explained by an extra-key entry
    (("manifest", "panels", "x", "stale"), "value", False, False),
])
def test_the_value_rule_accepts_fresh_at_rest_and_rejects_anything_else(approved, path, kind, value, ok):
    t = rig.Triple(path, kind, None, value)
    now = rig.fixed_clock()
    assert (rig.classify([t], approved, now).unmatched == []) is ok


def test_a_rule_that_compares_with_the_clock_fails_closed_without_one(approved):
    t = rig.Triple(("manifest", "panels", "x", "valid_until"), "extra_key_in_subject", None, "2999-01-01T00:00:00+00:00")
    assert rig.classify([t], approved).unmatched == [t]


# ------------------------------------------------------------------------------------------------------------------ etags


@pytest.mark.parametrize("value", [None, "", "error", "ABC", "a" * 63, "a" * 65, "g" * 64, 5])
def test_an_etag_is_a_lowercase_sha256_hex_and_nothing_else(value):
    assert not rig._is_etag("a" * 64, value) and not rig._is_etag(value, "a" * 64)
    assert rig._is_etag("0123456789abcdef" * 4, "f" * 64)


@pytest.mark.parametrize("name", IDS)
def test_the_serve_composed_panels_carry_equal_etags_on_both_sides(pairs, name):
    """The control: a panel composed at request time is read the same way by both cockpits, so the etag entries do not
    explain a difference for it; it must be none."""
    p = pairs[name]
    composed = [pid for pid in p.attached.panel_ids if p.attached.serve_composed(pid)]
    assert composed
    mb, ma = p.bare.manifest(rig.CRED)["panels"], p.attached.manifest(rig.CRED)["panels"]
    for pid in composed:
        assert mb[pid]["etag"] == ma[pid]["etag"], (name, pid, "manifest")
        assert p.bare.panel(pid)["etag"] == p.attached.panel(pid)["etag"], (name, pid, "payload")


@pytest.mark.parametrize("name", IDS)
def test_each_side_agrees_with_itself_and_distinct_panels_have_distinct_etags(pairs, name):
    """A side's manifest etag is the etag its payload carries and a second read gives the same tag; panels of different
    content never share one on a side (a constant tag would let a 304 hide any change)."""
    p = pairs[name]
    for ck in (p.bare, p.attached):
        manifest = ck.manifest(rig.CRED)["panels"]
        for pid in p.derived:
            assert ck.panel(pid)["etag"] == manifest[pid]["etag"] == ck.manifest(rig.CRED)["panels"][pid]["etag"], (name, pid)
        tags = {pid: h["etag"] for pid, h in manifest.items()}
        assert len(set(tags.values())) == len(tags), (name, sorted(t for t in tags if list(tags.values()).count(tags[t]) > 1))
    assert any(p.bare.panel(pid)["etag"] != p.attached.panel(pid)["etag"] for pid in p.derived), name


# --------------------------------------------------------------------------------------------------------------- canaries
# Each builds its own pair: the shared one holds heads decoded before the patch, and a patched decode would never run.


@pytest.fixture()
def fresh(tmp_path, scratch_home):
    p = rig.build_pair(CORPUS[0], tmp_path)
    yield p
    p.close()


def _break_decode(monkeypatch, mutate):
    real = sh.decode_snap

    ran: list = []      # one entry per decode the mutation completed on: a patch that raises (the engine turns that into an error panel, not a raise) leaves it empty

    def broken(*args, **kwargs):
        snap = real(*args, **kwargs)
        mutate(snap)
        ran.append(1)
        return snap

    monkeypatch.setattr(sh, "decode_snap", broken)
    monkeypatch.setattr(engine_mod, "decode_snap", broken)
    return ran


def _unmatched_on(pair, approved):
    return rig.classify(rig.compare(pair), approved, pair.config.now).unmatched


def _request_layer_sees(unmatched):
    return {"payload", "manifest"} <= {t.scope for t in unmatched}


def _sees_a_value_difference_at(got, field):
    """The canary's difference is the one it made: no unmatched triple is a read that raised (a patch that cannot run
    would raise on both sides' reads and look like a catch), and the field it changed is on a path."""
    assert not [t for t in got if rig.triple_raised(t)], [rig.render(t) for t in got if rig.triple_raised(t)][:5]
    assert any(field in t.path for t in got), field


def test_canary_a_decode_that_drops_a_row_is_seen(fresh, pairs, approved, triples, monkeypatch):
    base = len(_verdict(pairs, triples, approved, "k2a").unmatched)

    def drop(snap):
        if snap.rows:
            snap.rows = snap.rows[1:]

    ran = _break_decode(monkeypatch, drop)
    got = _unmatched_on(fresh, approved)
    assert ran, "the patched decode never completed"
    assert len(got) > base and _request_layer_sees(got)
    _sees_a_value_difference_at(got, "rows")


def test_canary_a_decode_that_changes_a_status_is_seen(fresh, pairs, approved, triples, monkeypatch):
    base = len(_verdict(pairs, triples, approved, "k2a").unmatched)
    ran = _break_decode(monkeypatch, lambda snap: setattr(snap, "status", "error"))
    got = _unmatched_on(fresh, approved)
    assert ran, "the patched decode never completed"
    assert len(got) > base and _request_layer_sees(got)
    _sees_a_value_difference_at(got, "status")


def test_canary_a_subject_that_reads_stale_pending_or_a_dead_worker_is_seen(fresh, approved):
    """The value rule bites on the wire: the subject's own answers are patched, and the stale flag, the pending flag, a
    past bound, a worker that is not live and a deriving panel each stand unmatched with the scope they were set in."""
    ck = fresh.attached
    real_manifest, real_panel, real_fresh = ck.manifest, ck.panel, ck.freshness

    def manifest(*a, **kw):
        out = copy.deepcopy(real_manifest(*a, **kw))
        for h in out["panels"].values():
            h.update(stale=True, pending=True, valid_until="2000-01-01T00:00:00+00:00")
        out["freshness"].update(worker="down", deriving=["x"])
        return out

    def panel(*a, **kw):
        out = dict(real_panel(*a, **kw))
        out.update(stale=True)
        return out

    def freshness():
        return {pid: dict(v, stale=True) if "stale" in v else v for pid, v in real_fresh().items()}

    ck.manifest, ck.panel, ck.freshness = manifest, panel, freshness
    seen = {(t.scope, str(t.path[-1])) for t in _unmatched_on(fresh, approved)}
    assert {("manifest", "stale"), ("manifest", "pending"), ("manifest", "valid_until"), ("manifest", "freshness"),
            ("payload", "stale"), ("freshness", "stale")} <= seen


def test_canary_a_subject_that_reads_fresh_is_not_flagged(fresh, approved):
    assert _unmatched_on(fresh, approved) == []


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


def test_canary_a_subject_that_changes_between_two_reads_is_seen(fresh):
    calls = []
    real = fresh.attached.panel

    def drifting(*a, **kw):
        out = dict(real(*a, **kw))
        out["drift"] = len(calls)
        calls.append(1)
        return out

    fresh.attached.panel = drifting
    assert rig.self_diff(fresh, "attached")


def test_canary_an_approved_entry_with_an_empty_reason_fails_the_entry_check(approved):
    broken = copy.deepcopy(approved)
    broken[0]["reason"] = ""
    assert rig.entry_problems(broken)


@pytest.mark.parametrize("path", [("manifest", "panels", "x", "surprise"), ("manifest", "surprise"),
                                  ("payload", "x", "full", "surprise"), ("freshness", "x", "surprise"),
                                  ("probe", "x", "surprise"), ("derive", "x", "surprise")])
def test_canary_an_unapproved_extra_key_stays_unmatched_in_every_scope(approved, path):
    t = rig.Triple(path, "extra_key_in_subject", None, 1)
    assert rig.classify([t], approved, rig.fixed_clock()).unmatched == [t]


@pytest.mark.parametrize("scope", rig.SCOPES)
@pytest.mark.parametrize("kind", ["extra_key_in_subject", "missing_key_in_subject"])
def test_canary_a_catch_all_entry_is_rejected_in_every_scope(scope, kind):
    """The check is what keeps a ``**`` entry out of the list: with it in, a real extra key in the scope is swallowed."""
    entry = _entry(scope=scope, path="**", kind=kind)
    assert any("literal" in p for p in rig.entry_problems([entry]))
    t = rig.Triple((scope, "x", "surprise"), kind, None, 1)
    assert rig.classify([t], [entry]).unmatched == []
