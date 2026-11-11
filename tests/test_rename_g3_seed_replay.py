# rename-intention: never-touch
"""G3: a signed op an OLD release left DECIDED on the retired disposition ``seed`` completes as ``someday`` under the new
broker (replay translation: the signed record is history and is never touched), while a NEW op naming ``seed`` is refused
with the pointer. The journal is what the old release wrote (``tests/fixtures/rename_g3/make_old_seed_journal.py`` drives
that release's own broker, killed at ``decided``)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import IntentionStore

from levain.cockpit.broker import Broker, Lifecycle
from levain.cockpit.effects import INTENTIONS, Effect, IntentionEffectStore, canonical_effect
from tests.autonomic_confirm_keys import anchor_scratch

FIXTURE = Path(__file__).with_name("fixtures") / "rename_g3"
OPS: dict[str, dict[str, str]] = json.loads((FIXTURE / "old_journal.json").read_text(encoding="utf-8"))


@pytest.fixture
def old() -> Iterator[tuple[Path, Path]]:
    home = anchor_scratch("g3-replay-")
    try:
        data = home / "data"
        data.mkdir()
        conn = sqlite3.connect(data / "cockpit.db")
        conn.executescript((FIXTURE / "old_journal.sql").read_text(encoding="utf-8"))
        conn.close()
        store = home / "intentions.json"
        shutil.copyfile(FIXTURE / "old_store.json", store)
        yield data, store
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _sql(data: Path, sql: str, *args: Any) -> list[tuple]:
    conn = sqlite3.connect(data / "cockpit.db")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _record(data: Path, name: str) -> dict[str, Any]:
    (rec,) = _sql(data, "SELECT record FROM pendings WHERE pending_id = ?", OPS[name]["pending_id"])[0]
    return json.loads(rec)


def _states(data: Path) -> dict[str, str]:
    by_id = {v["pending_id"]: k for k, v in OPS.items()}
    return {by_id[pid]: state for pid, state in _sql(data, "SELECT pending_id, state FROM pendings")}


def _settle(data: Path, store: Path) -> dict[str, str]:
    b = Broker(data, stores={INTENTIONS: IntentionEffectStore(store)}, store_paths=[store], install_class="reported")
    b.start()
    try:
        assert b.state is Lifecycle.ACTIVE
        end = time.monotonic() + 15
        while time.monotonic() < end and "DECIDED" in _states(data).values():
            time.sleep(0.05)
    finally:
        b.stop()
    return _states(data)


def test_the_fixture_is_ops_signed_on_the_retired_word(old):
    data, _ = old
    assert _states(data) == {"h": "DECIDED", "i": "DECIDED"}
    for name in "hi":
        eff = _record(data, name)["effects"][0]
        assert eff["changes"]["disposition"] == "seed" and eff["projection"]["disposition"] == "seed", eff


def test_ops_signed_on_seed_complete_as_someday_not_unapplied_refused(old):
    data, store = old
    got = _settle(data, store)
    assert got == {"h": "COMMITTED", "i": "COMMITTED"}, (got, _sql(data, "SELECT status, reason FROM effects"))
    raw = json.loads(store.read_text())
    rows = [r for r in raw["spores"] if r["origin_key"] == "k.i" or r["text"] == "captured as a seed"]
    assert sorted(r["disposition"] for r in rows) == ["someday", "someday"]
    assert all(e == ("APPLIED", None) for e in _sql(data, "SELECT status, reason FROM effects"))


def test_the_label_of_an_op_signed_on_seed_is_the_signers_over_someday(old):
    """A fire writes a label row per authority field it applied, as ``sha256(value)`` keyed to the op that signed it. The
    op signed ``seed`` and the store now holds ``someday``, so the row carries the digest of ``someday`` and the row
    renders under that signature (the same disposition under its new name), not as ``unverified``."""
    from levain.cockpit.consent import value_sha256

    data, store = old
    _settle(data, store)
    rows = _sql(data, "SELECT field, operation_id, value_sha256 FROM labels WHERE field = ?", "disposition")
    assert rows, "the settled ops wrote no disposition label"
    assert {r[2] for r in rows} == {value_sha256("someday")}, rows
    assert all(r[2] != value_sha256("seed") for r in rows)


def test_the_signed_record_is_never_rewritten(old):
    data, store = old
    before = [_sql(data, "SELECT record, record_sha256 FROM pendings WHERE pending_id = ?", OPS[n]["pending_id"])
              for n in "hi"]
    _settle(data, store)
    after = [_sql(data, "SELECT record, record_sha256 FROM pendings WHERE pending_id = ?", OPS[n]["pending_id"])
             for n in "hi"]
    assert after == before
    assert _record(data, "h")["effects"][0]["changes"]["disposition"] == "seed"


def test_replay_reads_the_retired_word_as_someday_and_nothing_else():
    rec = {"resource_key": "intention:k.x", "store": INTENTIONS, "expect": {"absent": True},
           "changes": {"type": "thought", "text": "t", "disposition": "seed"},
           "projection": {"disposition": "seed", "text": "t"}, "writes": ["disposition"], "binds": []}
    read = Effect.from_record(rec)
    assert read.changes["disposition"] == "someday" and read.projection["disposition"] == "someday"
    assert Effect.from_record({**rec, "changes": {**rec["changes"], "disposition": "agenda"}}).changes["disposition"] == "agenda"
    assert rec["changes"]["disposition"] == "seed"                            # the input mapping is not mutated


def test_a_new_op_naming_seed_is_refused_with_the_pointer():
    for verb, params, pre in (
        ("intention_seed", {"text": "x", "type": "thought", "disposition": "seed"}, None),
        ("intention_set_disposition", {"disposition": "seed"},
         {"id": "intention-1", "status": "open", "origin_key": "k", "disposition": "agenda", "next": "2030-01-01",
          "type": "thought", "text": "x"}),
    ):
        with pytest.raises(ValueError, match="someday"):
            canonical_effect(verb, params, pre, origin_key="k.new" if pre is None else None, version_of=lambda r: "v")
