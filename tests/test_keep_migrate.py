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
        self.intentions = root / "memory.spores.json"
        self.db = root / "memory.db"
        self.mig = root / "migrations"
        from anneal_memory import Store

        with Store(str(self.db)):
            pass                                  # create the episode store

    def plain(self):
        from anneal_memory.spores import IntentionStore

        return IntentionStore(self.intentions, id_prefix="intention")

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
    return km.plan(env.intentions, migrations_dir=env.mig, episodic_db=env.db, **kw)


def test_dry_run_changes_no_byte(env: Env):
    # a keyless legacy row: anneal's own keyed read would backfill + save it, the dry run must not
    doc = json.loads(env.intentions.read_text())
    doc["intentions"][0].pop("origin_key")
    env.intentions.write_text(json.dumps(doc, indent=1))
    before = _sha(env.intentions)
    p = _plan(env)
    km.pending_counts(env.intentions)
    assert _sha(env.intentions) == before
    plan = km.load_plan(p)
    assert p.parent == env.mig and p.name.startswith("keep-")
    texts = [i["text"] for i in plan["items"]]
    assert len(texts) == 6 and "a DATED agenda stays" not in texts and "an ordinary loop" not in texts
    for i in plan["items"]:
        assert i["why"] and i["destination"]["kind"] and "origin_key" in i and isinstance(i["notes"], list)
    first = plan["items"][0]
    assert first["notes"] == [n for n in doc["intentions"][0]["notes"]]


def test_routing_per_frame(env: Env):
    by = {i["text"]: i for i in km.load_plan(_plan(env))["items"]}
    gotcha = by["gotcha: never run `foo --all` on the prod box"]["destination"]
    assert gotcha["episode_type"] == "observation" and "durable-fact" in gotcha["tags"] \
        and "keep-migrated" in gotcha["tags"] and "— cues:" in gotcha["body"] and "bit me twice" in gotcha["body"]
    assert by["decided: the pace floor is 4"]["destination"]["episode_type"] == "decision"
    assert by["remember to renew the domain"]["destination"]["disposition"] == "someday"
    purple = by["purple"]
    assert purple["destination"]["episode_type"] == "observation" and "nothing lost" in purple["why"]
    assert by["pick up the Liz thread"]["destination"]["disposition"] == "someday"
    assert by["an undated agenda"]["destination"]["disposition"] == "someday"


def test_handoff_to_agenda_needs_a_date(env: Env):
    with pytest.raises(km.KeepMigrateError):
        _plan(env, handoff_to="agenda")
    d = {i["text"]: i for i in km.load_plan(_plan(env, handoff_to="agenda", agenda_date="2026-11-30"))["items"]}
    assert d["pick up the Liz thread"]["destination"] == {"kind": "spore", "disposition": "agenda", "next": "2026-11-30"}


def test_counts_and_doctor_line(env: Env):
    assert km.pending_counts(env.intentions) == {"notes": 4, "handoffs": 1, "agendas": 1}
    assert km.doctor_line(env.intentions) == "keep: 4 notes / 1 handoffs await migration (+ 1 undated agendas to someday)"
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    assert km.pending_counts(env.intentions) == {"notes": 0, "handoffs": 0, "agendas": 0}
    assert km.doctor_line(env.intentions) is None
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
                                               if i["text"] == "pick up the Liz thread")["notes"]), "someday") in after
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
        assert km.resolve_destination(i["result"], intentions_json=env.intentions, episodic_db=env.db), i["result"]
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
    seeds = [r for r in env.plain().list_open() if r.get("disposition") == "someday"]
    assert sorted(r["text"] for r in seeds) == sorted(
        ["remember to renew the domain", "pick up the Liz thread", "an undated agenda"])
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
    before = _sha(env.intentions)
    with pytest.raises(km.KeepMigrateError):
        km.apply(p, episodic_db=env.root / "missing.db")
    assert _sha(env.intentions) == before


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
                if r["text"] == "an undated agenda" and r.get("disposition") == "someday"]


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


def test_intention_destinations_carry_the_agenda_fields_and_the_age(env: Env):
    # (6) target / attention / germinated ride over; so do created / seen when the row has no notes
    s = env.plain()
    h = s.add(type="task", text="a targeted handoff", disposition="handoff", target="alice",
              attention="high", germinated={"on": "2026-10-01"})
    doc = json.loads(env.intentions.read_text())
    for r in doc["intentions"]:
        if r["text"] == "an undated agenda":
            r["created"] = "2025-01-02"
    env.intentions.write_text(json.dumps(doc, indent=1))
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    seed = _open_row(env, "a targeted handoff")
    assert seed["disposition"] == "someday" and seed["id"] != h["id"]
    assert seed.get("target") == "alice" and seed.get("attention") == "high" \
        and seed.get("germinated") == {"on": "2026-10-01"}
    assert _open_row(env, "an undated agenda")["created"] == "2025-01-02"


@pytest.mark.parametrize("stage", ["after_destination", "after_record"])
def test_crash_on_a_intention_destination_retries_once(env: Env, stage: str):
    # (7) the intention-destination crash-retry path: each seed lands exactly once, notes once
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
    assert len(liz) == 1 and liz[0]["disposition"] == "someday"
    assert liz[0]["notes"] == km.load_plan(p)["items"][idx]["notes"]
    seeds = [r for r in env.plain().list_open() if r.get("disposition") == "someday"]
    assert sorted(r["text"] for r in seeds) == sorted(
        ["remember to renew the domain", "pick up the Liz thread", "an undated agenda"])
    km.revert(p, episodic_db=env.db)
    assert env.open_set() == original


def test_a_note_added_to_a_crashed_destination_survives_the_retry(env: Env):
    # (A1, L3 1107-63 r2) a retry must never re-write the verbatim fields over a destination row
    # someone edited after the crash
    p = _plan(env)
    idx = _index_of(p, "pick up the Liz thread")

    def fault(s, item):
        if s == "after_destination" and item["index"] == idx:
            raise Boom

    with pytest.raises(Boom):
        km.apply(p, episodic_db=env.db, fault=fault)
    seed = next(r for r in env.plain().list_open()
                if r["text"] == "pick up the Liz thread" and r.get("disposition") == "someday")
    env.plain().update(seed["id"], add_note="added to the seed after the crash")
    rep = km.apply(p, episodic_db=env.db)
    assert idx in rep.applied
    liz = _open_row(env, "pick up the Liz thread")
    assert liz["id"] == seed["id"]
    assert any("added to the seed after the crash" in n for n in liz["notes"])
    assert any(i == idx for i, _ in rep.kept)                          # reported, not silent


def _crash_on_the_save_after(monkeypatch, prefix: str) -> None:
    """Raise Boom at the first intention-store save AFTER the save that first committed a row keyed
    under ``prefix`` (a process dying right after anneal's keyed add commits)."""
    from anneal_memory.spores import IntentionStore

    real = IntentionStore._save
    st = {"armed": False, "fired": False}

    def save(self, data):
        if st["armed"] and not st["fired"]:
            st["fired"] = True
            raise Boom
        real(self, data)
        if any(str(r.get("origin_key") or "").startswith(prefix) for r in data.get("intentions", [])):
            st["armed"] = True

    monkeypatch.setattr(IntentionStore, "_save", save)


def test_a_crash_right_after_the_restore_add_loses_no_notes(env: Env, monkeypatch):
    # (A2, L3 1107-63 r2) the restored original never exists without its verbatim notes / age
    original = env.open_set()
    p = _plan(env)
    km.apply(p, episodic_db=env.db)
    with monkeypatch.context() as m:
        _crash_on_the_save_after(m, km._REVERT_PREFIX)
        with pytest.raises(Boom):
            km.revert(p, episodic_db=env.db)
    km.revert(p, episodic_db=env.db)
    assert env.open_set() == original                                   # notes verbatim, none doubled


def _edit_destination_on_read(monkeypatch, env: Env, text: str) -> list:
    """The guarded store's ``get`` of the destination seed returns it, then an edit lands (once)."""
    import levain.intentions as ls

    real = ls.guarded_intention_store
    hits: list = []

    def wrapped(path):
        store = real(path)
        get = store.get

        def get_then_edit(sid):
            row = get(sid)
            if row is not None and row.get("text") == text and row.get("disposition") == "someday" \
                    and row.get("status") == "open" and not hits:
                hits.append(sid)
                env.plain().update(sid, add_note="edited mid-revert")
            return row

        store.get = get_then_edit
        return store

    monkeypatch.setattr(ls, "guarded_intention_store", wrapped)
    return hits


def test_a_destination_edited_mid_revert_is_retried_not_marked_reverted(env: Env, monkeypatch):
    # (B1, L3 1107-63 r2) the CAS refusal leaves the item retryable, never `reverted`
    p = _plan(env)
    idx = _index_of(p, "pick up the Liz thread")
    km.apply(p, episodic_db=env.db)
    with monkeypatch.context() as m:
        hits = _edit_destination_on_read(m, env, "pick up the Liz thread")
        rep = km.revert(p, episodic_db=env.db)
    assert hits
    assert idx not in rep.reverted and any(i == idx for i, _ in rep.skipped)
    assert km.load_plan(p)["items"][idx]["state"] == km.STATE_REVERT_BLOCKED
    assert [r for r in env.plain().list_open() if r["text"] == "pick up the Liz thread"
            and r.get("disposition") == "someday"]                        # the destination is still open
    again = km.revert(p, episodic_db=env.db)                            # a later revert retries it
    assert idx in again.reverted
    liz = [r for r in env.plain().list_open() if r["text"] == "pick up the Liz thread"]
    assert len(liz) == 1 and liz[0]["disposition"] == "handoff"        # one open copy: the original
    assert km.load_plan(p)["items"][idx]["state"] == km.STATE_REVERTED


def test_an_original_deleted_outside_the_plan_is_not_resurrected(env: Env):
    # (B2, L3 1107-63 r2) a missing original is never silently "ours": not re-added, destination kept
    p = _plan(env)
    idx = _index_of(p, "pick up the Liz thread")

    def fault(s, item):
        if s == "after_record" and item["index"] == idx:
            raise Boom

    with pytest.raises(Boom):
        km.apply(p, episodic_db=env.db, fault=fault)
    s = env.plain()
    from anneal_memory.spores import intention_version

    orig = next(r for r in s.list_open() if r["text"] == "pick up the Liz thread" and r["disposition"] == "handoff")
    s.delete(orig["id"], expected_version=intention_version(orig))
    rep = km.revert(p, episodic_db=env.db)
    assert idx not in rep.reverted and any(i == idx for i, _ in rep.skipped)
    liz = [r for r in env.plain().list_open() if r["text"] == "pick up the Liz thread"]
    assert len(liz) == 1 and liz[0]["disposition"] == "someday"           # not resurrected; the copy is kept
    assert km.load_plan(p)["items"][idx]["state"] == km.STATE_REVERT_BLOCKED


def test_the_guard_still_refuses_a_new_retired_write(env: Env):
    from levain.intentions import RetiredDisposition, guarded_intention_store

    with pytest.raises(RetiredDisposition):
        guarded_intention_store(env.intentions).add(type="thought", text="x", disposition="note")


# --- the private-anneal pin: _add_verbatim leans on three private names; this is the one place that says so.

_VERBATIM_ONLY = {"notes", "created", "seen"}


def test_add_verbatim_stores_the_same_shape_as_anneals_public_add(tmp_path: Path):
    """If an anneal release changes what `add` stores (a new key, a different value shape), the
    verbatim copy of it must change too. This fails then, instead of the migration writing rows the
    rest of the stack reads differently. Apart from notes / created / seen (set verbatim on purpose)
    the two rows must carry the same keys with the same value types."""
    from anneal_memory.spores import IntentionStore

    kw = dict(type="thought", domain="ops", tier="warm", salience=2, pointer="p/q.md",
              disposition="seed", target=None, attention=None, germinated=None)
    pub = IntentionStore(tmp_path / "pub.json", id_prefix="intention").add(text="the same words", next="2030-01-01",
                                                origin_key="pt-a1b2c3d4e5f6", **kw)
    row = {**kw, "notes": ["n1"], "created": "2020-01-02", "seen": "2020-01-03"}
    ver, created = km._add_verbatim(
        IntentionStore(tmp_path / "ver.json", id_prefix="intention"), origin_key="pt-a1b2c3d4e5f6", row=row, text="the same words",
        disposition="seed", next_="2030-01-01", guard=False)
    assert created
    assert ver["notes"] == ["n1"] and ver["created"] == "2020-01-02" and ver["seen"] == "2020-01-03"
    shape = lambda d: {k: type(v).__name__ for k, v in d.items() if k not in _VERBATIM_ONLY}  # noqa: E731
    assert shape(ver) == shape(pub), (
        "anneal's add now stores a different row shape than _add_verbatim writes. Update "
        "levain.keep_migrate._add_verbatim, or ask anneal for a public add taking notes/created/seen.")
    assert set(ver) - _VERBATIM_ONLY == set(pub) - _VERBATIM_ONLY
    assert _VERBATIM_ONLY <= set(pub)             # the three fields exist on a public row too


@pytest.mark.parametrize("name", ["_prepare_add", "_find_by_origin_key", "_add_locked", "_transaction"])
def test_the_private_anneal_names_add_verbatim_uses_still_exist(name: str):
    from anneal_memory.spores import IntentionStore

    assert hasattr(IntentionStore, name), (
        f"anneal's IntentionStore no longer has {name}, which levain.keep_migrate._add_verbatim calls. "
        "Update _add_verbatim, or ask anneal for a public add that takes notes/created/seen.")
