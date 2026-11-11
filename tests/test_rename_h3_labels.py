# rename-intention: never-touch
"""H3 of the spore -> intention rename: a provenance label the release that said "spore" recorded keeps describing its row.

A label is a journal row keyed by the resource it describes, and the journal is append-only: the label the old release
wrote for an edit sits under the old key prefix and cannot be re-keyed (a label row is immutable, and the signed record
beside it is hashed). The label reader therefore reads both prefixes as one resource, and every label the new code
writes uses the new one. ``tests/fixtures/rename_h2`` is the old release's own journal: its op ``a`` fired an update of
the row ``k.a`` to "alpha edited" and left that label.

Whether a RE-KEYED row keeps a label is the migration's question, not the reader's: a value a migration rewrote is a value
no fire signed, so it reads ``unverified`` (the last test pins that).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from levain.cockpit.consent import build_record, encode, utc_stamp, value_sha256
from levain.cockpit.effects import INTENTION_KEY_PREFIX, INTENTIONS
from levain.cockpit.firejournal import CockpitDB, UnitResult, authority_fence, new_pending_id
from levain.cockpit.labels import BrokerLabeller, LabelKey
from levain.intention_legacy import OLD_ROW_PREFIX

FIXTURE = Path(__file__).with_name("fixtures") / "rename_h2"
OPS = json.loads((FIXTURE / "old_journal.json").read_text(encoding="utf-8"))
OLD_OP = OPS["a"]["pending_id"]
OLD_TEXT = "alpha edited"
TOKEN = "token:browser:1"
AFTER_THE_FIXTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[CockpitDB]:
    """The old release's journal as it left it, opened by the new code."""
    conn = sqlite3.connect(tmp_path / "cockpit.db")
    conn.executescript((FIXTURE / "old_journal.sql").read_text(encoding="utf-8"))
    conn.close()
    yield CockpitDB(tmp_path)


def _fire(db: CockpitDB, key: str, text: str, *, origin_key: str = "k.a", at: datetime = AFTER_THE_FIXTURE) -> str:
    """One NEW op through the real journal API: issue, decide, record the unit APPLIED with its label, finish."""
    pid = new_pending_id()
    effect = {"store": INTENTIONS, "resource_key": INTENTION_KEY_PREFIX + origin_key, "expect": {"version": 3},
              "changes": {"text": text}, "projection": {"text": text}, "writes": ["text"], "binds": []}
    rec = build_record(operation_id=pid, verb="intention_update", intent="reported",
                       target={"kind": "row", "panel_id": "tray", "origin_key": origin_key},
                       params={"text": text}, effects=[effect], tier="T2", fence=authority_fence(None),
                       issued_at=utc_stamp(at), expires_at=utc_stamp(at + timedelta(hours=1)))
    raw = encode(rec)
    db.issue(record_bytes=raw, principal=TOKEN, idempotency_key=key, now=at)
    when = at + timedelta(minutes=1)
    assert db.decide(authority=None, operation_id=pid, decision_kind="ALLOWED_REPORTED", recomputed=raw, now=when,
                     pre_images=[{"text": "old"}]).decided
    db.record_unit(pid, [UnitResult(0, "APPLIED", labels=[("text", value_sha256(text))])], when)
    db.finish(pid, when)
    return pid


def _labels(db: CockpitDB):
    with db.read() as conn:
        return BrokerLabeller(db).snapshot(conn, None)


def test_control_the_old_label_is_keyed_by_the_old_prefix(db: CockpitDB) -> None:
    """A fixture rebuilt from the new code would carry the new prefix and make every test below vacuous."""
    with db.read() as conn:
        rows = conn.execute("SELECT resource_key, field, operation_id FROM labels").fetchall()
    assert [tuple(r) for r in rows] == [(OLD_ROW_PREFIX + "k.a", "text", OLD_OP)]


def test_label_for_finds_an_old_label_by_the_new_key(db: CockpitDB) -> None:
    row = db.label_for(INTENTION_KEY_PREFIX + "k.a", "text", value_sha256(OLD_TEXT))
    assert row is not None and row.operation_id == OLD_OP and row.decision_kind == "ALLOWED_REPORTED"
    assert row.resource_key == INTENTION_KEY_PREFIX + "k.a"                  # read in the new words
    assert db.label_for(INTENTION_KEY_PREFIX + "k.a", "text", value_sha256("not what was fired")) is None


def test_the_old_key_keeps_its_label(db: CockpitDB) -> None:
    """The row's label under the key the old release wrote: asked by either spelling it is the same resource's label."""
    for key in (INTENTION_KEY_PREFIX + "k.a", OLD_ROW_PREFIX + "k.a"):
        prov = _labels(db).provenance(LabelKey(key, {"text": OLD_TEXT}))
        assert prov["label"] == "claims cockpit token", key
        assert prov["operation_id"] == OLD_OP and prov["credential"] == TOKEN


def test_an_old_label_and_a_new_one_for_one_resource_both_resolve_and_the_latest_wins(db: CockpitDB) -> None:
    new_op = _fire(db, "n1", "alpha again")
    snap = _labels(db)
    key = INTENTION_KEY_PREFIX + "k.a"
    assert snap.provenance(LabelKey(key, {"text": OLD_TEXT}))["operation_id"] == OLD_OP
    assert snap.provenance(LabelKey(key, {"text": "alpha again"}))["operation_id"] == new_op
    # the same value fired again under the new prefix: the later fire answers, across both spellings
    again = _fire(db, "n2", OLD_TEXT, at=AFTER_THE_FIXTURE + timedelta(days=1))
    assert db.label_for(key, "text", value_sha256(OLD_TEXT)).operation_id == again
    assert _labels(db).provenance(LabelKey(key, {"text": OLD_TEXT}))["operation_id"] == again


def test_a_new_label_is_written_under_the_new_prefix(db: CockpitDB) -> None:
    _fire(db, "n1", "alpha again")
    with db.read() as conn:
        keys = {r[0] for r in conn.execute("SELECT resource_key FROM labels WHERE operation_id != ?", (OLD_OP,))}
    assert keys == {INTENTION_KEY_PREFIX + "k.a"}


def test_a_corrupt_old_label_degrades_its_resource_whichever_spelling_a_label_uses(db: CockpitDB) -> None:
    """The existing rule (one corrupt label row degrades only its own resource), across the rename: the corrupt row sits
    under the old prefix, so the resource it degrades is the intention's, and a label the new code wrote for the same
    resource reads ``unverified`` with it, while another resource's is untouched."""
    _fire(db, "n1", "alpha again")
    other = _fire(db, "n2", "zeta again", origin_key="k.z")
    conn = sqlite3.connect(db.path, isolation_level=None)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)             # a tamper past the triggers
    conn.execute("UPDATE labels SET decision_kind = 'ALLOWED' WHERE resource_key = ?", (OLD_ROW_PREFIX + "k.a",))
    conn.close()
    snap = _labels(db)
    assert snap.error is None and snap.degraded == 1
    assert snap.label(LabelKey(INTENTION_KEY_PREFIX + "k.a", {"text": OLD_TEXT})) == "unverified"
    assert snap.label(LabelKey(INTENTION_KEY_PREFIX + "k.a", {"text": "alpha again"})) == "unverified"
    assert snap.provenance(LabelKey(INTENTION_KEY_PREFIX + "k.z", {"text": "zeta again"}))["operation_id"] == other


def test_a_value_a_migration_rewrote_reads_unverified_not_the_old_label(db: CockpitDB) -> None:
    """The label is a claim about the value a fire signed. A value some other write left (a migration's rewrite) was
    signed by nobody: it is ``unverified``, and the old label still describes the value it was fired for."""
    snap = _labels(db)
    key = INTENTION_KEY_PREFIX + "k.a"
    rewritten = snap.provenance(LabelKey(key, {"text": "alpha, rewritten by a migration"}))
    assert rewritten["label"] == "unverified" and rewritten["operation_id"] is None
    assert snap.provenance(LabelKey(key, {"text": OLD_TEXT}))["operation_id"] == OLD_OP
