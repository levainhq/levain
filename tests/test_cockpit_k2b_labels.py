"""K2b-3b lane A: ``meta.label_rev`` and the immutability triggers, the journal's edits and decisions
readers, and the broker labeller (K2B3B_DESIGN rev 5 §0.3, §0.6, §2, §3, §8d)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cockpit.consent import build_record, encode, utc_stamp, value_sha256
from levain.cockpit.firejournal import CockpitDB, UnitResult, new_pending_id

T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PHONE = "token:phone:1"
FP_PHONE = "SHA256:phone"


def effect(i: int, text: str) -> dict:
    return {"store": "spores", "resource_key": f"ok-{i}", "expect": {"version": 3},
            "changes": {"text": text}, "projection": {"text": text}, "writes": ["text"], "binds": []}


def make_record(*, intent: str = "reported", text: str = "new", n_effects: int = 1,
                ttl: timedelta = timedelta(hours=1), effects: list | None = None) -> tuple[str, bytes]:
    pid = new_pending_id()
    rec = build_record(
        operation_id=pid, verb="spore_update", intent=intent,  # type: ignore[arg-type]
        target={"kind": "row", "panel_id": "loops", "origin_key": "ok-0", "row_id": "spore-1",
                "expected_version": 3},
        params={"text": text}, effects=effects or [effect(i, text) for i in range(n_effects)],
        tier="T2", fence={"policy_rev": 0}, issued_at=utc_stamp(T0), expires_at=utc_stamp(T0 + ttl))
    return pid, encode(rec)


@pytest.fixture
def db(tmp_path):
    j = CockpitDB(tmp_path)
    with j.write() as conn:
        conn.execute("INSERT INTO tokens (name, generation) VALUES ('phone', 1)")
    return j


def enrol(db, fp: str = FP_PHONE, cls: str = "display-phone") -> None:
    with db.write() as conn:
        conn.execute("INSERT INTO keys (fingerprint, principal, public_key, class, enrolled_at, "
                     "enrolled_via) VALUES (?, ?, 'ssh-ed25519 AAAA', ?, ?, 'laptop cli')",
                     (fp, "k" + fp[-6:], cls, utc_stamp(T0 - timedelta(days=1))))


def fire(db, key: str, *, text: str = "new", intent: str = "reported", at: datetime | None = None,
         label: bool = True, effects: list | None = None) -> str:
    """One op through the real journal API: issue, decide, record every unit APPLIED, finish."""
    pid, raw = make_record(intent=intent, text=text, effects=effects)
    db.issue(record_bytes=raw, principal=PHONE, idempotency_key=key, now=T0)
    when = at or T0 + timedelta(minutes=1)
    kw = {"signer_fp": FP_PHONE, "signature": "sig"} if intent == "sign" else {}
    n = len(effects) if effects else 1
    out = db.decide(operation_id=pid, decision_kind="ALLOWED" if intent == "sign" else "ALLOWED_REPORTED",
                    recomputed=raw, now=when, pre_images=[{"text": "old"}] * n, **kw)
    assert out.decided
    for i in range(n):
        labels = [("text", value_sha256(text))] if label else []
        db.record_unit(pid, [UnitResult(i, "APPLIED", labels=labels)], when)
    db.finish(pid, when)
    return pid


def label_rev(db) -> str:
    return db.meta("label_rev")


# ---------------------------------------------------------------------------------------------------
# label_rev triggers (§8d row 2)

def test_ten_labelled_fires_in_a_row_all_succeed_and_label_rev_reads_ten(db):
    assert label_rev(db) == "0"
    for i in range(10):
        fire(db, f"k{i}", text=f"v{i}")
    assert label_rev(db) == "10"


@pytest.mark.parametrize("value", ["3", "07", "x", "-1"])
def test_a_lowering_or_non_canonical_label_rev_update_aborts(db, value):
    for i in range(5):
        fire(db, f"k{i}", text=f"v{i}")
    with pytest.raises(sqlite3.IntegrityError, match="label_rev only rises"):
        with db.write() as conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'label_rev'", (value,))
    assert label_rev(db) == "5"


# ---------------------------------------------------------------------------------------------------
# the labeller (§2, §8d row 3)

from levain.cockpit.labels import BrokerLabeller, LabelKey  # noqa: E402


def test_provenance_labels_the_fired_value_and_not_a_changed_one(db):
    pid = fire(db, "k0", text="fired")
    snap = BrokerLabeller(db).snapshot()
    assert snap.error is None and snap.revision == ("", 1, 1)
    prov = snap.provenance(LabelKey("ok-0", {"text": "fired"}))
    assert prov == {"label": "claims cockpit token", "signer": None, "credential": PHONE,
                    "fired_at": utc_stamp(T0 + timedelta(minutes=1)), "operation_id": pid,
                    "fields": {"text": {"label": "claims cockpit token", "signer": None,
                                        "fired_at": utc_stamp(T0 + timedelta(minutes=1)),
                                        "operation_id": pid}}}
    changed = snap.provenance(LabelKey("ok-0", {"text": "edited by hand"}))
    assert changed["label"] == "unverified" and changed["operation_id"] is None
    assert changed["fields"]["text"] == {"label": "unverified", "signer": None, "fired_at": None,
                                         "operation_id": None}


def test_a_corrupt_label_row_degrades_only_its_resource(db):
    fire(db, "a", effects=[effect(0, "a")], text="a")
    fire(db, "b", effects=[effect(1, "b")], text="b")
    conn = sqlite3.connect(db.path, isolation_level=None)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)   # a tamper past the triggers
    # a copy that claims a stronger decision than its pending took (r1 codex)
    conn.execute("UPDATE labels SET decision_kind = 'ALLOWED' WHERE resource_key = 'ok-0'")
    conn.close()
    snap = BrokerLabeller(db).snapshot()
    assert snap.error is None and snap.degraded == 1
    assert snap.label(LabelKey("ok-0", {"text": "a"})) == "unverified"
    assert snap.label(LabelKey("ok-1", {"text": "b"})) == "claims cockpit token"
