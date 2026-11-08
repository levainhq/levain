"""TS-4: the Keep and legacy migration (``levain.keep_migrate``). Temp dirs only; no real store."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from levain import keep_migrate as km


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.spores = root / "memory.spores.json"
        self.db = root / "memory.db"
        self.mig = root / "migrations"
        from anneal_memory import Store

        with Store(str(self.db)):
            pass                                  # create the episode store

    def plain(self):
        from anneal_memory.spores import SporeStore

        return SporeStore(self.spores)

    def seed_legacy(self) -> None:
        """A store holding every shape the migration meets (retired dispositions via anneal's plain store,
        as a pre-TS-1 install wrote them)."""
        s = self.plain()
        a = s.add(type="thought", text="gotcha: never run `foo --all` on the prod box", disposition="note")
        s.update(a["id"], add_note="bit me twice")
        b = s.add(type="thought", text="decided: the pace floor is 4", disposition="note")
        s.update(b["id"], add_note="said on a Tuesday")
        s.add(type="task", text="remember to renew the domain", disposition="note")
        s.add(type="thought", text="purple", disposition="note")
        c = s.add(type="task", text="pick up the Liz thread", disposition="handoff")
        s.update(c["id"], add_note="she answered")
        s.add(type="thought", text="an undated agenda", disposition="agenda")
        s.add(type="thought", text="a DATED agenda stays", disposition="agenda", next="2030-01-01")
        s.add(type="task", text="an ordinary loop")
        s.add(type="thought", text="a live seed", disposition="seed")

    def open_set(self) -> list[tuple]:
        return sorted(
            (r["text"], tuple(r.get("notes") or ()), r.get("disposition"))
            for r in self.plain().list_open())

    def episodes(self) -> list:
        from anneal_memory import Store

        with Store(str(self.db)) as st:
            return [e for e in st.recall(source=km.EPISODE_SOURCE, limit=500).episodes]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.seed_legacy()
    return e


def _plan(env: Env, **kw) -> Path:
    return km.plan(env.spores, migrations_dir=env.mig, episodic_db=env.db, **kw)


def test_dry_run_changes_no_byte(env: Env):
    # a keyless legacy row: anneal's own keyed read would backfill + save it, the dry run must not
    doc = json.loads(env.spores.read_text())
    doc["spores"][0].pop("origin_key")
    env.spores.write_text(json.dumps(doc, indent=1))
    before = _sha(env.spores)
    p = _plan(env)
    km.pending_counts(env.spores)
    assert _sha(env.spores) == before
    plan = km.load_plan(p)
    assert p.parent == env.mig and p.name.startswith("keep-")
    texts = [i["text"] for i in plan["items"]]
    assert len(texts) == 6 and "a DATED agenda stays" not in texts and "an ordinary loop" not in texts
    for i in plan["items"]:
        assert i["why"] and i["destination"]["kind"] and "origin_key" in i and isinstance(i["notes"], list)
    first = plan["items"][0]
    assert first["notes"] == [n for n in doc["spores"][0]["notes"]]


def test_routing_per_frame(env: Env):
    by = {i["text"]: i for i in km.load_plan(_plan(env))["items"]}
    gotcha = by["gotcha: never run `foo --all` on the prod box"]["destination"]
    assert gotcha["episode_type"] == "observation" and "durable-fact" in gotcha["tags"] \
        and "keep-migrated" in gotcha["tags"] and "— cues:" in gotcha["body"] and "bit me twice" in gotcha["body"]
    assert by["decided: the pace floor is 4"]["destination"]["episode_type"] == "decision"
    assert by["remember to renew the domain"]["destination"]["disposition"] == "seed"
    purple = by["purple"]
    assert purple["destination"]["episode_type"] == "observation" and "nothing lost" in purple["why"]
    assert by["pick up the Liz thread"]["destination"]["disposition"] == "seed"
    assert by["an undated agenda"]["destination"]["disposition"] == "seed"


def test_handoff_to_agenda_needs_a_date(env: Env):
    with pytest.raises(km.KeepMigrateError):
        _plan(env, handoff_to="agenda")
    d = {i["text"]: i for i in km.load_plan(_plan(env, handoff_to="agenda", agenda_date="2026-11-30"))["items"]}
    assert d["pick up the Liz thread"]["destination"] == {"kind": "spore", "disposition": "agenda", "next": "2026-11-30"}


def test_counts_and_doctor_line(env: Env):
    assert km.pending_counts(env.spores) == {"notes": 4, "handoffs": 1, "agendas": 1}
    assert km.doctor_line(env.spores) == "keep: 4 notes / 1 handoffs await migration (+ 1 undated agendas to seeds)"
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    assert km.pending_counts(env.spores) == {"notes": 0, "handoffs": 0, "agendas": 0}
    assert km.doctor_line(env.spores) is None
    assert km.pending_counts(env.root / "nope.json") == {"notes": 0, "handoffs": 0, "agendas": 0}


def test_apply_then_revert_round_trips_the_open_set(env: Env):
    original = env.open_set()
    p = _plan(env)
    rep = km.apply(p, episodic_db=env.db)
    assert len(rep.applied) == 6 and not rep.skipped
    after = env.open_set()
    assert after != original
    assert not [r for r in after if r[2] in ("note", "handoff")]       # every legacy row left the open set
    assert ("an ordinary loop", (), None) in after and ("a DATED agenda stays", (), "agenda") in after
    # the migrated seeds carry the original's notes verbatim
    assert ("pick up the Liz thread", tuple(next(i for i in km.load_plan(p)["items"]
                                               if i["text"] == "pick up the Liz thread")["notes"]), "seed") in after
    rv = km.revert(p, episodic_db=env.db)
    assert len(rv.reverted) == 6
    assert env.open_set() == original                                   # (text, notes, disposition), notes verbatim
    assert env.episodes() == []                                         # tombstoned
    again = km.revert(p, episodic_db=env.db)                            # idempotent
    assert not again.reverted and env.open_set() == original


def test_every_applied_destination_resolves(env: Env):
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    plan = km.load_plan(p)
    kinds = set()
    for i in plan["items"]:
        assert i["state"] == km.STATE_APPLIED
        assert km.resolve_destination(i["result"], spores_json=env.spores, episodic_db=env.db), i["result"]
        kinds.add(i["result"]["kind"])
    assert kinds == {"episode", "spore"}
    # the originals are composted with the plan path in a note
    resolved = env.plain()._load()["resolved"]
    assert len(resolved) == 6
    assert all(any(str(p) in n for n in r["notes"]) and r["resolution"]["kind"] == "composted" for r in resolved)


@pytest.mark.parametrize("stage", ["after_destination", "after_record"])
def test_crash_between_destination_and_compost_loses_nothing(env: Env, stage: str):
    original = env.open_set()
    p = _plan(env)

    class Boom(RuntimeError):
        pass

    def fault(s, item):
        if s == stage and item["index"] == 1:                 # the decision episode
            raise Boom

    with pytest.raises(Boom):
        km.apply(p, episodic_db=env.db, fault=fault)
    # item 0 fully applied, item 1 crashed mid-flight: its ORIGINAL is still open (not lost)
    live = env.open_set()
    assert any(r[0] == "decided: the pace floor is 4" and r[2] == "note" for r in env.open_set())
    # re-run: resumes, finishes, duplicates nothing
    rep = km.apply(p, episodic_db=env.db)
    assert 1 in rep.applied
    eps = env.episodes()
    assert len(eps) == 3                                      # gotcha, decision, the unplaced note: none doubled
    assert len({e.id for e in eps}) == len(eps)
    seeds = [r for r in env.plain().list_open() if r.get("disposition") == "seed"]
    assert sorted(r["text"] for r in seeds) == sorted(
        ["a live seed", "remember to renew the domain", "pick up the Liz thread", "an undated agenda"])
    rv = km.revert(p, episodic_db=env.db)
    assert env.open_set() == original and env.episodes() == []


def test_a_second_apply_duplicates_nothing(env: Env):
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    n_eps, n_open = len(env.episodes()), len(env.plain().list_open())
    rep = km.apply(p, episodic_db=env.db)
    assert not rep.applied and len(env.episodes()) == n_eps and len(env.plain().list_open()) == n_open


def test_a_row_edited_after_the_plan_is_left_alone(env: Env):
    p = _plan(env)
    s = env.plain()
    row = next(r for r in s.list_open() if r["text"] == "purple")
    s.update(row["id"], text="purple, edited")
    rep = km.apply(p, episodic_db=env.db)
    assert any("edited since" in why for _, why in rep.skipped)
    assert ("purple, edited", (), "note") in env.open_set()           # still there, untouched
    assert len(rep.applied) == 5


def test_apply_without_the_episode_store_changes_nothing(env: Env):
    p = _plan(env)
    before = _sha(env.spores)
    with pytest.raises(km.KeepMigrateError):
        km.apply(p, episodic_db=env.root / "missing.db")
    assert _sha(env.spores) == before


class Boom(RuntimeError):
    pass


def _index_of(plan_path: Path, text: str) -> int:
    return next(i["index"] for i in km.load_plan(plan_path)["items"] if i["text"] == text)


def _open_row(env: Env, text: str) -> dict:
    return next(r for r in env.plain().list_open() if r["text"] == text)


def test_revert_crash_after_restore_restores_once(env: Env):
    # (1) a crash between the restore and the state flip; the re-run must not re-add the original
    original = env.open_set()
    p = _plan(env)
    km.apply(p, episodic_db=env.db)

    def fault(s, item):
        if s == "after_restore" and item["index"] == 0:
            raise Boom

    with pytest.raises(Boom):
        km.revert(p, episodic_db=env.db, fault=fault)
    text0 = km.load_plan(p)["items"][0]["text"]
    env.plain().update(_open_row(env, text0)["id"], add_note="touched after the crash")
    km.revert(p, episodic_db=env.db)
    rows = [r for r in env.plain().list_open() if r["text"] == text0]
    assert len(rows) == 1                                               # no doubled original
    assert any("touched after the crash" in n for n in rows[0]["notes"])   # the restore is not re-run over it
    assert env.episodes() == []
    assert len(env.open_set()) == len(original)


@pytest.mark.parametrize("stage", ["after_destination", "after_record"])
def test_a_destination_written_before_an_edit_is_removed_by_revert(env: Env, stage: str):
    # (2) crash with the destination written, the original edited, re-apply: no orphan destination
    p = _plan(env)
    idx = _index_of(p, "decided: the pace floor is 4")

    def fault(s, item):
        if s == stage and item["index"] == idx:
            raise Boom

    with pytest.raises(Boom):
        km.apply(p, episodic_db=env.db, fault=fault)
    s = env.plain()
    s.update(_open_row(env, "decided: the pace floor is 4")["id"], text="decided: the pace floor is 5")
    rep = km.apply(p, episodic_db=env.db)
    assert idx in [i for i, _ in rep.skipped]
    item = km.load_plan(p)["items"][idx]
    assert item["result"] and item["result"]["kind"] == "episode"      # the destination stays on record
    km.revert(p, episodic_db=env.db)
    assert env.episodes() == []                                         # nothing orphaned
    floors = [r for r in env.open_set() if r[0].startswith("decided: the pace floor")]
    assert len(floors) == 1 and floors[0][0] == "decided: the pace floor is 5" and floors[0][2] == "note"


def test_scheduling_an_undated_agenda_after_the_plan_makes_it_stale(env: Env):
    # (3) every field the destination depends on is compared, `next` included
    p = _plan(env)
    s = env.plain()
    s.update(_open_row(env, "an undated agenda")["id"], next="2030-06-01")
    rep = km.apply(p, episodic_db=env.db)
    assert any("edited since" in why for _, why in rep.skipped)
    assert _open_row(env, "an undated agenda")["disposition"] == "agenda"
    assert _open_row(env, "an undated agenda")["next"] == "2030-06-01"
    assert not [r for r in env.plain().list_open()
                if r["text"] == "an undated agenda" and r.get("disposition") == "seed"]


def test_an_edit_between_the_compare_and_the_compost_is_not_composted(env: Env):
    # (3) the compare-then-compost is a compare-and-set: an edit landing after the compare survives
    p = _plan(env)
    idx = _index_of(p, "purple")

    def fault(s, item):
        if s == "after_record" and item["index"] == idx:
            row = _open_row(env, "purple")
            env.plain().update(row["id"], text="purple, edited mid-apply")

    rep = km.apply(p, episodic_db=env.db, fault=fault)
    assert idx not in rep.applied
    assert any(r[0] == "purple, edited mid-apply" and r[2] == "note" for r in env.open_set())
    km.revert(p, episodic_db=env.db)
    assert not [e for e in env.episodes() if "purple" in e.content]


def test_a_second_apply_while_one_runs_is_refused(env: Env):
    # (4) a plan-level lock: two applies cannot interleave
    p = _plan(env)
    inner: list = []

    def fault(s, item):
        if s == "after_record" and item["index"] == 0 and not inner:
            try:
                km.apply(p, episodic_db=env.db)
                inner.append("ran")
            except km.KeepMigrateError as exc:
                inner.append(exc)

    km.apply(p, episodic_db=env.db, fault=fault)
    assert isinstance(inner[0], km.KeepMigrateError)
    assert all(i["state"] == km.STATE_APPLIED for i in km.load_plan(p)["items"])


def test_plan_name_allocation_never_overwrites(env: Env, monkeypatch):
    # (4) the name is reserved atomically: a racing planner that saw the name free cannot clobber it
    from datetime import datetime, timezone

    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    first = _plan(env, now=now)
    first_bytes = first.read_bytes()
    monkeypatch.setattr(km.Path, "exists", lambda self: False)          # both planners saw it free
    second = _plan(env, now=now)
    assert second != first and first.read_bytes() == first_bytes


def test_an_episode_override_without_a_type_is_a_clean_error(env: Env):
    # (5) KeepMigrateError, not KeyError
    row = next(r for r in env.plain().list_open() if r["text"] == "purple")
    with pytest.raises(km.KeepMigrateError):
        _plan(env, overrides={row["origin_key"]: {"kind": "episode"}})


def test_spore_destinations_carry_the_agenda_fields_and_the_age(env: Env):
    # (6) target / attention / germinated ride over; so do created / seen when the row has no notes
    s = env.plain()
    h = s.add(type="task", text="a targeted handoff", disposition="handoff", target="alice",
              attention="high", germinated={"on": "2026-10-01"})
    doc = json.loads(env.spores.read_text())
    for r in doc["spores"]:
        if r["text"] == "an undated agenda":
            r["created"] = "2025-01-02"
    env.spores.write_text(json.dumps(doc, indent=1))
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    seed = _open_row(env, "a targeted handoff")
    assert seed["disposition"] == "seed" and seed["id"] != h["id"]
    assert seed.get("target") == "alice" and seed.get("attention") == "high" \
        and seed.get("germinated") == {"on": "2026-10-01"}
    assert _open_row(env, "an undated agenda")["created"] == "2025-01-02"


@pytest.mark.parametrize("stage", ["after_destination", "after_record"])
def test_crash_on_a_spore_destination_retries_once(env: Env, stage: str):
    # (7) the spore-destination crash-retry path: each seed lands exactly once, notes once
    original = env.open_set()
    p = _plan(env)
    idx = _index_of(p, "pick up the Liz thread")

    def fault(s, item):
        if s == stage and item["index"] == idx:
            raise Boom

    with pytest.raises(Boom):
        km.apply(p, episodic_db=env.db, fault=fault)
    rep = km.apply(p, episodic_db=env.db)
    assert idx in rep.applied
    liz = [r for r in env.plain().list_open() if r["text"] == "pick up the Liz thread"]
    assert len(liz) == 1 and liz[0]["disposition"] == "seed"
    assert liz[0]["notes"] == km.load_plan(p)["items"][idx]["notes"]
    seeds = [r for r in env.plain().list_open() if r.get("disposition") == "seed"]
    assert sorted(r["text"] for r in seeds) == sorted(
        ["a live seed", "remember to renew the domain", "pick up the Liz thread", "an undated agenda"])
    km.revert(p, episodic_db=env.db)
    assert env.open_set() == original


def test_the_guard_still_refuses_a_new_retired_write(env: Env):
    from levain.spores import RetiredDisposition, guarded_spore_store

    with pytest.raises(RetiredDisposition):
        guarded_spore_store(env.spores).add(type="thought", text="x", disposition="note")
