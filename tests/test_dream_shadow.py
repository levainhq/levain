"""levain.dream.shadow (D1-3): the shadow night's reader, seed, episode check and diff, on hand-built fixtures.

The fixture is a SMALL flow ``state/dream`` layout built by hand from the real layout (a ledger ``end`` row, a snapshot
dir with a ``manifest.json`` of hashes, a ``package.json`` made by anneal's own ``prepare_wrap`` on a copy of the
store). Nothing here reads flow's real state or ``~/.anneal-memory``, and nothing composes: the compose step is a stub
the test passes in.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, CrystalStore, Store, sessions
from anneal_memory.continuity import prepare_wrap

from levain import wrap as w
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import ready as rd
from levain.dream import shadow as sh
from levain.dream import snapshot as sn
from levain.dream.cli import add_parsers

DAY = "2026-11-02"
RUN = "2026-11-02-aaaa1111"
FLOW_TEXT = """# flow — Memory (v1)

## State
Mon. The state line.

## Decisions
[decided] ALPHA ruling.
[decided] BETA ruling.

## Patterns
!! one_pattern | 3x
"""
SHADOW_TEXT = """# flow — Memory (v1)

## State
Mon. The state line, reworded.

## Decisions
[decided] ALPHA ruling.

## Patterns
!! one_pattern | 3x
!! another_pattern | 1x
"""
STEPS_OK = "$ prepare  -> 0\nprepare_wrap: READY\n\n$ save --check --text-file x  -> 0\nCLEAN\n"
STEPS_REFUSED = STEPS_OK + "\n$ save --check --text-file x --wrap-token t  -> 1\nREFUSED: the shrink gate held.\n"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Flow:
    """A flow dream tree on disk with one committed dream-day."""

    def __init__(self, tmp: Path, episodes: int = 3) -> None:
        self.root = tmp / "flowdream"
        self.root.mkdir()
        self.src = tmp / "flowstore"
        self.src.mkdir()
        self.db = self.src / "memory.db"
        with Store(str(self.db), section_schema=FLOW_SCHEMA) as s:
            for i in range(episodes):
                s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
        self.snap = self.root / "snapshots" / RUN
        self.run = self.root / RUN
        self.snap.mkdir(parents=True)
        self.run.mkdir()
        self.package_from_a_copy()
        self.snapshot()
        (self.run / sh.COMPOSED).write_text(FLOW_TEXT, encoding="utf-8")
        (self.run / sh.STEPS_LOG).write_text(STEPS_OK, encoding="utf-8")
        self.ledger([self.end(RUN, DAY)])

    def package_from_a_copy(self) -> None:
        scratch = self.src.parent / "probe"
        scratch.mkdir()
        probe = scratch / "memory.db"
        src = sqlite3.connect(str(self.db))
        dst = sqlite3.connect(str(probe))
        src.backup(dst)
        src.close()
        dst.close()
        with Store(str(probe), project_name="probe", section_schema=None) as s:
            res = prepare_wrap(s, crystal_store=CrystalStore(scratch / "memory.crystal.json"), wrap_token="f" * 32,
                               session_id=None)
        (self.run / sh.PACKAGE).write_text(json.dumps(res, default=str), encoding="utf-8")
        shutil.rmtree(scratch)

    def snapshot(self) -> None:
        src = sqlite3.connect(str(self.db))
        dst = sqlite3.connect(str(self.snap / sn.DB_BACKUP))
        src.backup(dst)
        src.close()
        dst.close()
        files: dict[str, dict] = {}
        bodies = {"continuity": FLOW_TEXT, "meta": json.dumps({"saved": "fixture"}),
                  "crystal": json.dumps({"crystals": []})}
        for key, p in sn.text_tier(self.db).items():
            (self.snap / p.name).write_text(bodies[key], encoding="utf-8")
            files[key] = {"name": p.name, "sha256": sha(self.snap / p.name)}
        manifest = {"id": RUN, "anneal_db": str(self.db), "db_backup_sha256": sha(self.snap / sn.DB_BACKUP),
                    "files": files, "taken_at": "2026-11-03T00:13:22-05:00", "reverted_at": None}
        (self.snap / sn.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def end(run: str, day: str, committed: bool = True) -> dict:
        return {"event": "end", "id": run, "day": day, "committed": committed, "outcome": "saved"}

    def ledger(self, rows: list, raw_extra: str = "") -> None:
        (self.root / sh.LEDGER).write_text("".join(json.dumps(r) + "\n" for r in rows) + raw_extra, encoding="utf-8")

    def manifest(self) -> dict:
        return json.loads((self.snap / sn.MANIFEST).read_text())

    def set_manifest(self, m: dict) -> None:
        (self.snap / sn.MANIFEST).write_text(json.dumps(m), encoding="utf-8")


@pytest.fixture
def flow(tmp_path):
    return Flow(tmp_path)


@pytest.fixture
def shadow_db(tmp_path):
    return tmp_path / "shadow" / "memory.db"


# ------------------------------------------------------------------------------------------------------- the reader

def test_a_committed_day_with_a_matching_snapshot_is_ready(flow):
    v = sh.day_ready(flow.root, DAY)
    assert v.ready and v.flow_day.run_id == RUN and v.flow_day.day == DAY
    assert v.flow_day.snap_dir == flow.snap and v.flow_day.run_dir == flow.run


def test_a_day_flow_did_not_commit_is_quiet_not_unreadable(flow):
    assert sh.day_ready(flow.root, "2026-11-05").kind == sh.KIND_NO_FLOW_DREAM
    flow.ledger([Flow.end(RUN, DAY, committed=False)])
    v = sh.day_ready(flow.root, DAY)
    assert (v.verdict, v.kind) == (rd.QUIET, sh.KIND_NO_FLOW_DREAM)
    flow.ledger([{"event": "start", "id": RUN, "day": DAY}])
    assert sh.day_ready(flow.root, DAY).verdict == rd.QUIET


def test_the_newest_committed_end_row_names_the_run(flow):
    flow.ledger([Flow.end("2026-11-02-old00000", DAY), Flow.end(RUN, DAY)])
    assert sh.day_ready(flow.root, DAY).flow_day.run_id == RUN


def test_an_unreadable_ledger_line_blocks_only_when_it_could_be_the_day(flow):
    flow.ledger([], raw_extra="{torn\n")
    v = sh.day_ready(flow.root, DAY)
    assert (v.verdict, v.kind) == (rd.UNREADABLE, sh.KIND_UNREADABLE)       # cannot rule out that it was this day
    flow.ledger([Flow.end(RUN, DAY)], raw_extra="{torn\n")
    assert sh.day_ready(flow.root, DAY).verdict == rd.UNREADABLE               # a torn TAIL is never read past (B3)
    flow.ledger([Flow.end(RUN, DAY)])
    (flow.root / sh.LEDGER).write_text("{torn\n" + (flow.root / sh.LEDGER).read_text(), encoding="utf-8")
    assert sh.day_ready(flow.root, DAY).ready                                  # a torn line mid-file is not the day
    (flow.root / sh.LEDGER).unlink()
    assert sh.day_ready(flow.root, DAY).verdict == rd.UNREADABLE


def test_a_run_id_that_is_a_path_is_refused(flow):
    flow.ledger([Flow.end("../escape", DAY)])
    assert sh.day_ready(flow.root, DAY).verdict == rd.UNREADABLE


@pytest.mark.parametrize("what", ["db", "continuity", "meta", "crystal"])
def test_every_snapshot_file_must_match_its_manifest_hash(flow, what):
    m = flow.manifest()
    target = flow.snap / (sn.DB_BACKUP if what == "db" else m["files"][what]["name"])
    target.write_bytes(target.read_bytes() + b"x")
    v = sh.day_ready(flow.root, DAY)
    assert (v.verdict, v.kind) == (rd.UNREADABLE, sh.KIND_DAMAGED)


def test_a_manifest_missing_or_naming_a_path_is_unreadable_or_damaged(flow):
    m = flow.manifest()
    m["files"]["continuity"]["name"] = "../../etc/passwd"
    flow.set_manifest(m)
    assert sh.day_ready(flow.root, DAY).kind == sh.KIND_DAMAGED
    (flow.snap / sn.MANIFEST).unlink()
    assert sh.day_ready(flow.root, DAY).verdict == rd.UNREADABLE
    flow.snapshot()
    m = flow.manifest()
    del m["db_backup_sha256"]
    flow.set_manifest(m)
    assert sh.day_ready(flow.root, DAY).kind == sh.KIND_DAMAGED


def test_a_file_the_manifest_records_absent_must_be_absent(flow):
    m = flow.manifest()
    m["files"]["crystal"]["sha256"] = None
    flow.set_manifest(m)
    assert sh.day_ready(flow.root, DAY).kind == sh.KIND_DAMAGED
    (flow.snap / m["files"]["crystal"]["name"]).unlink()
    assert sh.day_ready(flow.root, DAY).ready


def test_a_manifest_for_another_run_is_damaged(flow):
    m = flow.manifest()
    m["id"] = "2026-11-02-other000"
    flow.set_manifest(m)
    assert sh.day_ready(flow.root, DAY).kind == sh.KIND_DAMAGED


def test_a_shadow_done_for_the_day_makes_it_quiet_and_another_day_does_not(flow):
    row = {"agent_id": "ag-1", "kind": "scheduled", "state": "done", "window_key": "w:2026-11-02:22:00"}
    v = sh.day_ready(flow.root, DAY, occurrences=lambda: [row], zone="America/New_York", agent_id="ag-1")
    assert (v.verdict, v.kind) == (rd.QUIET, sh.KIND_SHADOWED)
    other = dict(row, window_key="w:2026-11-01:22:00")
    assert sh.day_ready(flow.root, DAY, occurrences=lambda: [other], zone="America/New_York", agent_id="ag-1").ready
    failed = dict(row, state="failed")
    assert sh.day_ready(flow.root, DAY, occurrences=lambda: [failed], zone="America/New_York", agent_id="ag-1").ready
    foreign = dict(row, agent_id="ag-2")
    assert sh.day_ready(flow.root, DAY, occurrences=lambda: [foreign], zone="America/New_York", agent_id="ag-1").ready


def test_an_unreadable_occurrence_table_is_unreadable_never_ready(flow):
    def boom():
        raise OSError("locked")

    v = sh.day_ready(flow.root, DAY, occurrences=boom, zone="America/New_York")
    assert v.verdict == rd.UNREADABLE
    assert sh.day_ready(flow.root, DAY, occurrences=lambda: [], zone=None).verdict == rd.UNREADABLE   # no zone to place a day


def test_the_reader_writes_nothing_under_flow_root(flow):
    before = {p: p.stat().st_mtime_ns for p in flow.root.rglob("*") if p.is_file()}
    sh.day_ready(flow.root, DAY)
    sh.flow_episode_set(sh.read_flow_day(flow.root, DAY))
    after = {p: p.stat().st_mtime_ns for p in flow.root.rglob("*") if p.is_file()}
    assert before == after


# -------------------------------------------------------------------------------------------------------------- seed

def test_seed_copies_the_snapshot_and_claims_the_baton(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    s = sh.seed(fd, shadow_db)
    assert sha(shadow_db) == flow.manifest()["db_backup_sha256"]
    assert (shadow_db.parent / sh.MARKER).is_file() and s.run_id == RUN
    for key, p in sn.text_tier(shadow_db).items():
        assert sha(p) == flow.manifest()["files"][key]["sha256"]
    assert sessions.baton_holder(sn.text_tier(shadow_db)["continuity"]) == rd.SESSION_ID
    assert sha(flow.snap / sn.DB_BACKUP) == flow.manifest()["db_backup_sha256"]      # the source is untouched


def test_seed_replaces_a_consolidated_shadow_store_with_the_snapshot(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    with Store(str(shadow_db), section_schema=None) as s:
        s.record(content="a later episode", episode_type="observation", source="t")
    (shadow_db.parent / "memory.db-wal").write_bytes(b"stale")
    sh.seed(fd, shadow_db)
    assert sha(shadow_db) == flow.manifest()["db_backup_sha256"] and not (shadow_db.parent / "memory.db-wal").exists()


def test_seed_refuses_a_directory_that_holds_something_without_the_marker(flow, tmp_path):
    victim = tmp_path / "real"
    victim.mkdir()
    (victim / "memory.db").write_bytes(b"precious")
    with pytest.raises(sh.ShadowRefused):
        sh.seed(sh.read_flow_day(flow.root, DAY), victim / "memory.db")
    assert (victim / "memory.db").read_bytes() == b"precious"


def test_seed_refuses_flows_own_store(flow):
    with pytest.raises(sh.ShadowRefused) as e:
        sh.seed(sh.read_flow_day(flow.root, DAY), flow.db)
    assert "flow's own store" in e.value.why


def test_seed_refuses_when_the_shadow_store_is_busy(flow, shadow_db):
    shadow_db.parent.mkdir()
    (shadow_db.parent / sh.MARKER).write_text("{}")
    held = w._lock_wrap_file(shadow_db.parent / "wrap.lock")
    try:
        with pytest.raises(sh.ShadowRefused) as e:
            sh.seed(sh.read_flow_day(flow.root, DAY), shadow_db)
        assert "busy" in e.value.why
    finally:
        w._unlock_wrap(held)


def test_seed_verifies_the_hashes_again_under_the_lock(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    (flow.snap / sn.DB_BACKUP).write_bytes((flow.snap / sn.DB_BACKUP).read_bytes() + b"x")     # damaged after the check
    with pytest.raises(sh.ShadowRefused) as e:
        sh.seed(fd, shadow_db)
    assert e.value.kind == sh.KIND_DAMAGED and not shadow_db.exists()


# ------------------------------------------------------------------------------------------------- the episode check

def test_ids_are_read_from_the_package_text_by_regex():
    t = "### Decisions (2)\n- (94cc54e4) text\n- (03cf602d) more (deadbeef) inline\n### Obs\n- (ZZZZZZZZ) no\n"
    assert sh.package_ids(t) == frozenset({"94cc54e4", "03cf602d"})
    assert sh.package_ids(None) == frozenset()


def test_the_real_shape_of_flows_package_yields_count_and_ids(flow):
    es = sh.flow_episode_set(sh.read_flow_day(flow.root, DAY))
    assert es.count == 3 and len(es.ids) == 3


def test_a_shadow_that_prepares_the_same_episodes_is_ready_and_leaves_no_open_wrap(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    v = sh.episode_check(fd, shadow_db)
    assert v.ready, v.reason
    with Store(str(shadow_db), section_schema=None) as s:
        assert not s.get_wrap_started_at()


def test_an_extra_episode_in_the_shadow_ends_the_night_input_diverged(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    with Store(str(shadow_db), section_schema=None) as s:
        s.record(content="not in flow's package", episode_type="observation", source="t")
    v = sh.episode_check(fd, shadow_db)
    assert (v.verdict, v.kind) == (rd.QUIET, sh.KIND_DIVERGED) and "only in the shadow" in v.reason


def test_a_changed_id_with_the_same_count_still_diverges(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    pkg = json.loads((flow.run / sh.PACKAGE).read_text())
    first = sorted(sh.package_ids(pkg["package"]["episodes"]))[0]
    pkg["package"]["episodes"] = pkg["package"]["episodes"].replace(f"- ({first})", "- (ffffffff)", 1)
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    v = sh.episode_check(fd, shadow_db)
    assert v.kind == sh.KIND_DIVERGED and "1 only in flow" in v.reason and pkg["episode_count"] == 3


def test_a_package_without_ids_compares_by_count(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    pkg = json.loads((flow.run / sh.PACKAGE).read_text())
    pkg["package"] = {}
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    assert sh.episode_check(fd, shadow_db).ready
    pkg["episode_count"] = 9
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    v = sh.episode_check(fd, shadow_db)
    assert v.kind == sh.KIND_DIVERGED and "9" in v.reason
    del pkg["episode_count"]
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    assert sh.episode_check(fd, shadow_db).kind == sh.KIND_DIVERGED          # nothing to compare is a mismatch, never a pass


def test_compare_units():
    a = sh.EpisodeSet(2, frozenset({"aaaaaaaa", "bbbbbbbb"}))
    assert sh.compare_episode_sets(a, sh.EpisodeSet(2, frozenset({"aaaaaaaa", "bbbbbbbb"}))) is None
    assert sh.compare_episode_sets(a, sh.EpisodeSet(2, frozenset({"aaaaaaaa", "cccccccc"}))).startswith("the shadow's episode set")
    assert sh.compare_episode_sets(sh.EpisodeSet(2, frozenset()), sh.EpisodeSet(2, frozenset())) is None
    assert sh.compare_episode_sets(sh.EpisodeSet(None, frozenset()), sh.EpisodeSet(2, frozenset())) is not None
    assert sh.compare_episode_sets(sh.EpisodeSet(3, frozenset()), sh.EpisodeSet(0, frozenset())) is not None


def test_a_store_that_will_not_open_is_unreadable_not_diverged(flow, tmp_path):
    fd = sh.read_flow_day(flow.root, DAY)
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "memory.db").write_bytes(b"not a database" * 100)
    v = sh.episode_check(fd, tmp_path / "bare" / "memory.db")
    assert v.verdict == rd.UNREADABLE and v.kind != sh.KIND_DIVERGED


# ------------------------------------------------------------------------------------------------------------- diff

def test_diff_counts_per_section_and_names_the_decisions_the_shadow_lost():
    d = sh.diff_outputs(FLOW_TEXT, SHADOW_TEXT)
    assert d.sections["State"] == {"only_in_flow": 1, "only_in_shadow": 1}
    assert d.sections["Decisions"] == {"only_in_flow": 1, "only_in_shadow": 0}
    assert d.sections["Patterns"] == {"only_in_flow": 0, "only_in_shadow": 1}
    assert d.decisions_missing == ("[decided] BETA ruling.",)
    assert d.flow_chars == len(FLOW_TEXT) and d.shadow_chars == len(SHADOW_TEXT)
    assert "--- flow" in d.text and "+++ shadow" in d.text
    assert any("1 flow line(s) missing from the shadow" in x for x in d.own_lines())


def test_identical_outputs_have_no_section_lines_and_no_missing_decisions():
    d = sh.diff_outputs(FLOW_TEXT, FLOW_TEXT)
    assert d.sections == {} and d.decisions_missing == () and d.text == ""
    assert any("no flow line is missing" in x for x in d.own_lines())


def test_a_decision_that_moved_to_another_section_is_not_missing():
    moved = FLOW_TEXT.replace("[decided] BETA ruling.\n", "").replace("!! one_pattern | 3x", "[decided] BETA ruling.\n!! one_pattern | 3x")
    assert sh.diff_outputs(FLOW_TEXT, moved).decisions_missing == ()


def test_flow_refusals_come_from_failed_save_steps(flow):
    assert sh.flow_refusals(flow.run) == []
    (flow.run / sh.STEPS_LOG).write_text(STEPS_REFUSED, encoding="utf-8")
    r = sh.flow_refusals(flow.run)
    assert len(r) == 1 and "exit 1" in r[0] and "shrink gate" in r[0]
    (flow.run / sh.STEPS_LOG).unlink()
    assert "could not be read" in sh.flow_refusals(flow.run)[0]


def test_the_card_writes_the_diff_counts_both_refusal_lists_and_is_never_authority_relevant(flow, tmp_path):
    (flow.run / sh.STEPS_LOG).write_text(STEPS_REFUSED, encoding="utf-8")
    fd = sh.read_flow_day(flow.root, DAY)
    occ = tmp_path / "occ"
    usage = {"composes": [{"n": 1, "error": "REFUSED: the check gate held on compose one"}], "unit": "usd_list",
             "readable": True, "actual_usd_micros": 1, "reserved_usd_micros": 2, "source": "t"}
    c = sh.shadow_card(fd, SHADOW_TEXT, occurrence="occ-1", occ_dir=occ, usage_doc=usage)
    assert c.authority_relevant is False and c.body["his"] == []
    own = "\n".join(c.body["own"])
    assert "sizes: flow" in own and "State: 1 line(s) only in flow" in own and "Decisions: 1 flow line(s) missing" in own
    assert "refusals: flow 1, shadow 1" in own and "flow refusal" in own and "shadow refusal" in own
    text = (occ / cd.DIFF_FILE).read_text()
    assert text == sh.diff_outputs(FLOW_TEXT, SHADOW_TEXT).text and f"sha256 {hashlib.sha256(text.encode()).hexdigest()[:16]}" in own
    assert c.body["day"] == DAY and "scratch copy" in c.body["remedy"]


def test_the_compose_notes_never_reach_his(flow, tmp_path):
    """A shadow compose's own notes (contradictions) are not read: the comparison must never ask him for anything."""
    fd = sh.read_flow_day(flow.root, DAY)
    c = sh.shadow_card(fd, SHADOW_TEXT, occurrence="occ-1", occ_dir=tmp_path / "occ")
    assert c.authority_relevant is False


# -------------------------------------------------------------------------------------------------------- the night

HIST = dict(occurrences=lambda: [], zone="America/New_York")


def stub_compose(calls: list, text: str = SHADOW_TEXT):
    def compose(fd, db, occ):
        calls.append((fd.run_id, db, occ))
        return text, None
    return compose


def test_a_night_runs_the_pieces_in_order_and_builds_the_card(flow, shadow_db, tmp_path):
    calls: list = []
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="occ-1", compose=stub_compose(calls), **HIST)
    assert r.state == "composed" and r.card is not None and r.card.authority_relevant is False
    assert len(calls) == 1 and calls[0][0] == RUN
    assert (tmp_path / "occ" / cd.DIFF_FILE).is_file()


def test_a_diverged_input_never_reaches_the_compose(flow, shadow_db, tmp_path):
    fd = sh.read_flow_day(flow.root, DAY)
    pkg = json.loads((flow.run / sh.PACKAGE).read_text())
    pkg["episode_count"] = 99
    pkg["package"] = {}
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    calls: list = []
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="occ-1", compose=stub_compose(calls), **HIST)
    assert r.state == "skipped-no-input" and r.reason.startswith("quiet/input-diverged:") and calls == []
    assert fd.run_id == RUN


def test_a_quiet_or_damaged_day_never_reaches_the_compose(flow, shadow_db, tmp_path):
    calls: list = []
    r = sh.run_night(flow.root, "2026-11-09", shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls), **HIST)
    assert r.state == "skipped-no-input" and r.reason.startswith("quiet/no-flow-dream") and calls == []
    (flow.snap / sn.DB_BACKUP).write_bytes(b"x")
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls), **HIST)
    assert r.reason.startswith("unreadable/snapshot-damaged") and calls == [] and not shadow_db.exists()


def test_a_compose_that_raises_ends_the_night_failed(flow, shadow_db, tmp_path):
    def boom(fd, db, occ):
        raise RuntimeError("the mind died")

    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=boom, **HIST)
    assert r.state == "failed" and "the mind died" in r.reason and r.card is None


def test_the_shadow_input_check_speaks_the_dreams_verdict_and_the_controller_bool(flow, shadow_db):
    row = {"occurrence_id": "occ-9", "agent_id": "ag-1", "kind": "scheduled", "window_key": "w:2026-11-02:22:00"}
    v = sh.shadow_input(flow.root, shadow_db, row, zone="America/New_York", occurrences=lambda: [], agent_id="ag-1")
    assert v.ok and v.state == rd.READY
    assert da.bool_on_input(lambda r: v)(row) is True
    pkg = json.loads((flow.run / sh.PACKAGE).read_text())
    pkg["episode_count"], pkg["package"] = 77, {}
    (flow.run / sh.PACKAGE).write_text(json.dumps(pkg))
    v2 = sh.shadow_input(flow.root, shadow_db, row, zone="America/New_York", occurrences=lambda: [], agent_id="ag-1")
    assert (v2.ok, v2.state, v2.kind) == (False, rd.QUIET, sh.KIND_DIVERGED)
    assert da.parse_recorded(v2.recorded())[:2] == (rd.QUIET, sh.KIND_DIVERGED)
    assert da.bool_on_input(lambda r: v2)(row) is False
    bad = sh.shadow_input(flow.root, shadow_db, {"kind": "scheduled", "window_key": "garbage"}, zone="America/New_York",
                          occurrences=lambda: [])
    assert (bad.ok, bad.state) == (False, rd.UNREADABLE)


def test_the_input_check_does_not_count_its_own_admitted_row(flow, shadow_db):
    row = {"occurrence_id": "occ-9", "agent_id": "ag-1", "kind": "scheduled", "window_key": "w:2026-11-02:22:00"}
    done_self = dict(row, state="done")
    v = sh.shadow_input(flow.root, shadow_db, row, zone="America/New_York", occurrences=lambda: [done_self],
                        agent_id="ag-1")
    assert v.ok


# ---------------------------------------------------------------------------------------------------------- the verb

def run_verb(*argv: str):
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["dream", "shadow", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    return code, buf.getvalue()


def test_the_verb_reads_only_without_a_shadow_dir(flow):
    code, out = run_verb("--flow-root", str(flow.root), "--day", DAY, "--json")
    doc = json.loads(out)
    assert code == 0 and doc["day_ready"]["verdict"] == "ready" and "seed" not in doc


def test_the_verb_seeds_a_scratch_dir_and_diffs_a_given_output(flow, tmp_path):
    composed = tmp_path / "composed.md"
    composed.write_text(SHADOW_TEXT, encoding="utf-8")
    code, out = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(tmp_path / "scratch"),
                         "--composed", str(composed), "--occurrence-dir", str(tmp_path / "occ"), "--json")
    doc = json.loads(out)
    assert code == 0 and doc["episodes"]["verdict"] == "ready"
    assert doc["card"]["authority_relevant"] is False and (tmp_path / "occ" / cd.DIFF_FILE).is_file()
    assert (tmp_path / "scratch" / "memory.db").is_file()


def test_the_verb_exit_codes_quiet_1_unreadable_3(flow, tmp_path):
    assert run_verb("--flow-root", str(flow.root), "--day", "2026-11-08")[0] == 1
    (flow.snap / sn.DB_BACKUP).write_bytes(b"x")
    assert run_verb("--flow-root", str(flow.root), "--day", DAY)[0] == 3


def test_the_verb_refuses_a_live_looking_directory(flow, tmp_path):
    d = tmp_path / "live"
    d.mkdir()
    (d / "memory.db").write_bytes(b"x")
    code, out = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(d))
    assert code == 3
    assert (d / "memory.db").read_bytes() == b"x"


# --- D1 L3 r2 fixes, commit B (B1-B10) ---------------------------------------------------------------------------------

def test_a_seed_that_dies_midway_leaves_a_dir_the_retry_can_reuse(flow, shadow_db, monkeypatch):
    # B1: the marker was written last, so a crash after the db copy bricked the dir for every retry
    fd = sh.read_flow_day(flow.root, DAY)
    real, n = sh._put, {"n": 0}

    def flaky(src, dest):
        n["n"] += 1
        if n["n"] == 2:
            raise OSError("disk went away")
        real(src, dest)

    monkeypatch.setattr(sh, "_put", flaky)
    with pytest.raises(OSError):
        sh.seed(fd, shadow_db)
    monkeypatch.setattr(sh, "_put", real)
    sh.seed(fd, shadow_db)
    assert sha(shadow_db) == flow.manifest()["db_backup_sha256"]


def test_a_manifest_missing_a_required_tier_is_damaged(flow):
    # B2
    m = flow.manifest()
    del m["files"]["crystal"]
    flow.set_manifest(m)
    v = sh.day_ready(flow.root, DAY)
    assert (v.verdict, v.kind) == (rd.UNREADABLE, sh.KIND_DAMAGED) and "crystal" in v.reason


def test_a_torn_ledger_tail_does_not_select_an_older_committed_end(flow):
    # B3
    flow.ledger([Flow.end("2026-11-02-old00000", DAY), Flow.end(RUN, DAY)], raw_extra='{"event": "end", "id": "x"')
    v = sh.day_ready(flow.root, DAY)
    assert (v.verdict, v.kind) == (rd.UNREADABLE, sh.KIND_UNREADABLE) and "tail unreadable" in v.reason


def test_a_night_without_the_occurrence_history_is_refused_not_run(flow, shadow_db, tmp_path):
    # B4: None meant "never shadowed", a second Opus night for the same day
    calls: list = []
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls))
    assert r.state == "skipped-no-input" and "history" in r.reason and calls == [] and not shadow_db.exists()


def test_the_verb_with_an_agent_dir_and_no_agent_id_is_refused(flow, tmp_path, capsys):
    # B5: every agent's done row answered the shadow's no-shadow-done check
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--agent-dir", str(tmp_path))
    assert code == 2 and "--agent-id" in capsys.readouterr().err


def test_a_missing_composed_text_is_refused_before_any_compose(flow, shadow_db, tmp_path):
    # B6
    (flow.run / sh.COMPOSED).unlink()
    calls: list = []
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls), **HIST)
    assert r.state == "skipped-no-input" and "composed text" in r.reason and calls == [] and not shadow_db.exists()


def test_an_unwritable_shadow_parent_is_a_typed_skip_not_a_traceback(flow, tmp_path):
    # B7
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    calls: list = []
    r = sh.run_night(flow.root, DAY, blocker / "sub" / "memory.db", tmp_path / "occ", occurrence="o",
                     compose=stub_compose(calls), **HIST)
    assert r.state == "skipped-no-input" and r.reason.startswith("unreadable/") and calls == []
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(blocker / "sub"))
    assert code == 3


def test_composed_without_a_shadow_dir_is_refused_by_name(flow, tmp_path, capsys):
    # B8
    composed = tmp_path / "c.md"
    composed.write_text(SHADOW_TEXT)
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--composed", str(composed))
    assert code == 2 and "--composed needs --shadow-dir" in capsys.readouterr().err


def test_composed_without_an_occurrence_dir_is_refused_by_name(flow, tmp_path, capsys):
    composed = tmp_path / "c.md"
    composed.write_text(SHADOW_TEXT)
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(tmp_path / "s"),
                       "--composed", str(composed))
    assert code == 2 and "--occurrence-dir" in capsys.readouterr().err


def test_a_second_night_on_a_held_scratch_dir_does_not_interleave(flow, shadow_db, tmp_path):
    # B9: while one night holds the night lock a second is refused as busy and never reaches seed or compose
    shadow_db.parent.mkdir()
    held = w._lock_wrap_file(shadow_db.parent / sh.NIGHT_LOCK)
    try:
        calls: list = []
        r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls), **HIST)
        assert r.state == "skipped-no-input" and "busy" in r.reason and calls == [] and not shadow_db.exists()
    finally:
        w._unlock_wrap(held)
    r = sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=stub_compose(calls), **HIST)
    assert r.state == "composed"                                  # the lock file alone does not trip the marker guard


def test_the_compose_runs_under_the_night_lock(flow, shadow_db, tmp_path):
    seen: list = []

    def compose(fd, db, occ):
        probe = w._lock_wrap_file(db.parent / sh.NIGHT_LOCK)
        seen.append(probe is w._ANOTHER_WRAP_RUNNING)
        w._unlock_wrap(probe)
        return SHADOW_TEXT, None

    assert sh.run_night(flow.root, DAY, shadow_db, tmp_path / "occ", occurrence="o", compose=compose, **HIST).state == "composed"
    assert seen == [True]


def test_episode_check_takes_the_wrap_lock(flow, shadow_db):
    fd = sh.read_flow_day(flow.root, DAY)
    sh.seed(fd, shadow_db)
    held = w._lock_wrap_file(shadow_db.parent / "wrap.lock")
    try:
        v = sh.episode_check(fd, shadow_db)
        assert not v.ready and "busy" in v.reason
    finally:
        w._unlock_wrap(held)
    assert sh.episode_check(fd, shadow_db).ready


def test_the_own_store_check_fails_closed_when_it_cannot_resolve(flow, shadow_db, monkeypatch):
    # B10
    fd = sh.read_flow_day(flow.root, DAY)
    real = Path.resolve

    def boom(self, *a, **k):
        if str(self) == str(flow.db):
            raise OSError("loop")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", boom)
    with pytest.raises(sh.ShadowRefused, match="cannot tell"):
        sh.seed(fd, shadow_db)


# --- D1 L3 r3 fixes (C3, C4) -------------------------------------------------------------------------------------------

def test_a_second_verb_on_a_held_scratch_dir_is_busy_and_does_not_seed(flow, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    held = sh._lock(scratch, sh.NIGHT_LOCK)
    try:
        code, out = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(scratch), "--json")
    finally:
        sh._unlock(held)
    doc = json.loads(out)
    assert code == 3 and "busy" in doc["seed"]["why"] and not (scratch / "memory.db").exists()


def test_shadow_input_on_a_held_scratch_dir_answers_busy_not_ready(flow, shadow_db):
    row = {"occurrence_id": "occ-9", "agent_id": "ag-1", "kind": "scheduled", "window_key": "w:2026-11-02:22:00"}
    shadow_db.parent.mkdir(parents=True, exist_ok=True)
    held = sh._lock(shadow_db.parent, sh.NIGHT_LOCK)
    try:
        v = sh.shadow_input(flow.root, shadow_db, row, zone="America/New_York", occurrences=lambda: [], agent_id="ag-1")
    finally:
        sh._unlock(held)
    assert not v.ok and "busy" in v.reason and not shadow_db.exists()


def test_shadow_input_without_the_history_is_refused_typed(flow, shadow_db):
    row = {"occurrence_id": "occ-9", "agent_id": "ag-1", "kind": "scheduled", "window_key": "w:2026-11-02:22:00"}
    v = sh.shadow_input(flow.root, shadow_db, row, zone="America/New_York", agent_id="ag-1")
    assert (v.ok, v.state, v.kind) == (False, rd.UNREADABLE, sh.KIND_UNREADABLE) and not shadow_db.exists()


def test_composed_without_an_occurrence_dir_exits_2_before_any_seed(flow, tmp_path):
    composed = tmp_path / "c.md"
    composed.write_text(SHADOW_TEXT)
    scratch = tmp_path / "s"
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(scratch),
                       "--composed", str(composed))
    assert code == 2 and not scratch.exists()


def test_an_undecodable_composed_file_is_a_typed_exit_3_not_a_traceback(flow, tmp_path, capsys):
    composed = tmp_path / "c.md"
    composed.write_bytes(b"\xff\xfe\x00bad")
    code, _ = run_verb("--flow-root", str(flow.root), "--day", DAY, "--shadow-dir", str(tmp_path / "s"),
                       "--composed", str(composed), "--occurrence-dir", str(tmp_path / "occ"))
    assert code == 3 and "levain dream shadow:" in capsys.readouterr().err
