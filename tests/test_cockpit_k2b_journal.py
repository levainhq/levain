"""K2b-1 lane A: the consent record's canonical bytes and ``cockpit.db``'s transitions (K2B_DESIGN
rev 8 §1, §2, §3.1-§3.5, §4.1.2, §5)."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from levain.cockpit import consent
from levain.cockpit.consent import ConsentError, build_record, encode, utc_stamp
from levain.cockpit.firejournal import (
    DB_NAME,
    CockpitDB,
    CockpitFormatError,
    IdempotencyConflict,
    JournalCorruptError,
    PendingCapExceeded,
    UnitResult,
    new_pending_id,
)

T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PHONE = "token:phone:1"
FP_LAPTOP = "SHA256:laptop"
FP_PHONE = "SHA256:phone"
FP_WEAK = "SHA256:presence"


def effect(i: int, version: int = 3) -> dict:
    return {"store": "spores", "resource_key": f"ok-{i}", "expect": {"version": version},
            "changes": {"text": f"new {i}"}, "projection": {"text": f"new {i}"},
            "writes": ["text"], "binds": []}


def make_record(*, pid: str | None = None, intent: str = "sign", tier: str = "T2",
                n_effects: int = 2, params: dict | None = None, policy_rev: int = 0,
                version: int = 3, ttl: timedelta = timedelta(hours=1)) -> tuple[str, bytes]:
    pid = pid or new_pending_id()
    rec = build_record(
        operation_id=pid, verb="spore_update", intent=intent,  # type: ignore[arg-type]
        target={"kind": "row", "panel_id": "loops", "origin_key": "ok-0", "row_id": "spore-1",
                "expected_version": version},
        params=params if params is not None else {"text": "new 0"},
        effects=[effect(i, version) for i in range(n_effects)], tier=tier,
        fence={"policy_rev": policy_rev}, issued_at=utc_stamp(T0), expires_at=utc_stamp(T0 + ttl))
    return pid, encode(rec)


@pytest.fixture
def db(tmp_path):
    j = CockpitDB(tmp_path)
    with j.write() as conn:
        conn.execute("INSERT INTO tokens (name, generation) VALUES ('phone', 1)")
        for fp, cls in ((FP_LAPTOP, "laptop-presence"), (FP_PHONE, "display-phone"),
                        (FP_WEAK, "presence")):
            conn.execute("INSERT INTO keys (fingerprint, principal, public_key, class, enrolled_at, "
                         "enrolled_via) VALUES (?, ?, 'ssh-ed25519 AAAA', ?, ?, 'laptop cli')",
                         (fp, "k" + fp[-6:], cls, utc_stamp(T0 - timedelta(days=1))))
    return j


def issue(db, *, key: str = "k1", principal: str = PHONE, **kw) -> tuple[str, bytes]:
    cap_exempt = kw.pop("cap_exempt", False)
    pid, raw = make_record(**kw)
    res = db.issue(record_bytes=raw, principal=principal, idempotency_key=key, now=T0,
                   cap_exempt=cap_exempt)
    return res.pending_id, raw


def decided(db, *, key: str = "k1", n_effects: int = 2) -> str:
    pid, raw = issue(db, key=key, n_effects=n_effects)
    out = db.decide(operation_id=pid, decision_kind="ALLOWED", recomputed=raw,
                    now=T0 + timedelta(minutes=1), signer_fp=FP_PHONE, signature="sig",
                    pre_images=[{"text": "old"}] * n_effects)
    assert out.decided
    return pid


def raw_conn(db) -> sqlite3.Connection:
    return sqlite3.connect(db.path, isolation_level=None)


# ---------------------------------------------------------------------------------------------------
# consent: JCS

def test_jcs_sorts_members_by_utf16_code_units():
    # RFC 8785 §3.2.3's sorting example, with U+FB01 for its U+FB33 (a composition exclusion, which
    # NFC decomposes before sorting): a surrogate pair still sorts below a high BMP character
    keys = ["\u20ac", "\r", "\ufb01", "1", "\U0001F600", "\u0080", "\u00f6"]
    out = encode({k: 0 for k in keys}).decode("utf-8")
    order = ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001F600", "\ufb01"]
    assert out == "{" + ",".join(f"{consent.json.dumps(k, ensure_ascii=False)}:0" for k in order) + "}"


def test_jcs_string_escapes_are_ecmascripts():
    assert encode("\u000f\n\"\\\u007f\u00e9") == '"\\u000f\\n\\"\\\\\u007f\u00e9"'.encode("utf-8")
    assert encode([True, False, None, -7, {}]) == b"[true,false,null,-7,{}]"


def test_every_string_and_key_is_nfc():
    assert encode({"e\u0301": "e\u0301"}) == '{"\u00e9":"\u00e9"}'.encode("utf-8")
    with pytest.raises(ConsentError, match="equal under NFC"):
        encode({"e\u0301": 1, "\u00e9": 2})


@pytest.mark.parametrize("bad", [1.0, float("nan"), 2**53, -(2**53), {1: "x"}, {"a": {1, 2}},
                                 "\ud800"])
def test_values_outside_the_domain_are_refused(bad):
    with pytest.raises(ConsentError):
        encode(bad)


def test_build_record_refuses_a_malformed_record():
    _, raw = make_record()
    rec = consent.json.loads(raw)
    with pytest.raises(ConsentError, match="operation_id"):
        build_record(**{**rec, "operation_id": "cpXYZ"})
    with pytest.raises(ConsentError, match="intent"):
        build_record(**{**rec, "intent": "maybe"})
    with pytest.raises(ConsentError, match="policy_rev"):
        build_record(**{**rec, "fence": {"policy_rev": True}})
    with pytest.raises(ConsentError, match="store"):
        build_record(**{**rec, "effects": [{"resource_key": "ok-0"}]})
    with pytest.raises(ConsentError, match="after issued_at"):
        build_record(**{**rec, "expires_at": rec["issued_at"]})
    with pytest.raises(ConsentError, match="timestamp"):
        build_record(**{**rec, "issued_at": "2026-10-10T12:00:00Z"})


def test_utc_stamp_is_fixed_width_utc_and_refuses_naive():
    east = timezone(timedelta(hours=-4))
    assert utc_stamp(datetime(2026, 10, 10, 8, 0, tzinfo=east)) == "2026-10-10T12:00:00.000000Z"
    with pytest.raises(ConsentError):
        utc_stamp(datetime(2026, 10, 10))
    stamps = [utc_stamp(T0 + timedelta(microseconds=m)) for m in (0, 1, 999_999, 10**6, 10**12)]
    assert stamps == sorted(stamps) and len({len(s) for s in stamps}) == 1


# ---------------------------------------------------------------------------------------------------
# the store itself

def test_a_foreign_database_is_refused_not_taken_over(tmp_path):
    conn = sqlite3.connect(tmp_path / DB_NAME)
    conn.execute("CREATE TABLE effects (x)")
    conn.commit()
    conn.close()
    with pytest.raises(CockpitFormatError, match="not a cockpit journal"):
        CockpitDB(tmp_path).meta("format")


def test_a_journal_of_another_format_is_refused(db, tmp_path):
    with db.write() as conn:
        conn.execute("UPDATE meta SET value = 'levain-autonomic/1' WHERE key = 'format'")
    with pytest.raises(CockpitFormatError, match="levain-autonomic/1"):
        CockpitDB(tmp_path).meta("format")


def test_the_journal_runs_the_durable_pragmas(db):
    with db.read() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2          # FULL
        assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1


def test_an_old_sqlite_is_refused_at_open(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.durable_sqlite.sqlite3.sqlite_version", "3.34.1")
    with pytest.raises(RuntimeError, match="RETURNING"):
        CockpitDB(tmp_path).meta("format")


def test_policy_rev_reads_and_bumps(db):
    assert db.policy_rev() == 0
    assert db.bump_policy_rev() == 1 and db.policy_rev() == 1


# ---------------------------------------------------------------------------------------------------
# expiry (§3.1)

def test_expire_due_is_text_ordered_and_inclusive(db):
    a, _ = issue(db, key="a", ttl=timedelta(seconds=10))
    b, _ = issue(db, key="b", ttl=timedelta(seconds=10, microseconds=1))
    c, _ = issue(db, key="c", ttl=timedelta(days=40))       # crosses a month boundary in text
    edge = T0 + timedelta(seconds=10)
    assert not db.has_due(edge - timedelta(microseconds=1))
    assert db.has_due(edge)
    assert db.expire_due(edge) == 1
    assert db.outcome(a).state == "EXPIRED" and db.outcome(b).state == "WAITING"
    assert db.expire_due(T0 + timedelta(days=39, hours=23)) == 1
    assert db.outcome(c).state == "WAITING"
    with db.read() as conn:
        row = conn.execute("SELECT terminal_at, expires_at, reason FROM pendings WHERE pending_id = ?",
                           (a,)).fetchone()
    assert row[0] == row[1] and row[2] == "expired"


# ---------------------------------------------------------------------------------------------------
# issue: idempotency and the cap (§2, §3.1)

def test_issue_records_a_waiting_pending_with_its_exact_bytes(db):
    pid, raw = issue(db)
    view = db.load(pid)
    assert view.state == "WAITING" and view.record_bytes == raw and view.effects == ()
    assert view.principal_base == "token:phone"


def test_a_replay_returns_the_pending_and_a_changed_request_conflicts(db):
    pid, raw = issue(db, key="same")
    _, raw2 = make_record()                                  # a fresh id, same request
    again = db.issue(record_bytes=raw2, principal=PHONE, idempotency_key="same", now=T0)
    assert again.replay and again.pending_id == pid and again.record == raw
    _, other = make_record(params={"text": "different"})
    with pytest.raises(IdempotencyConflict):
        db.issue(record_bytes=other, principal=PHONE, idempotency_key="same", now=T0)
    _, reported = make_record(intent="reported")
    with pytest.raises(IdempotencyConflict):
        db.issue(record_bytes=reported, principal=PHONE, idempotency_key="same", now=T0)


def test_a_replay_across_a_rotation_finds_the_old_pending_and_its_outcome(db):
    pid, _ = issue(db, key="r")
    db.cancel(pid, by="rotation", now=T0)
    _, raw = make_record()
    again = db.issue(record_bytes=raw, principal="token:phone:2", idempotency_key="r", now=T0)
    assert again.replay and again.pending_id == pid and again.outcome.state == "CANCELLED"


def test_the_cap_counts_waiting_pendings_of_this_generation_only(db):
    for i in range(8):
        issue(db, key=f"k{i}")
    with pytest.raises(PendingCapExceeded):
        issue(db, key="k8")
    issue(db, key="rep", intent="reported", cap_exempt=True)      # same request takes REPORTED
    issue(db, key="k9", principal="token:phone:2")                # another generation
    issue(db, key="k10", principal="device:abc")                  # another principal


def test_an_expired_pending_frees_its_cap_slot(db):
    for i in range(8):
        issue(db, key=f"k{i}", ttl=timedelta(seconds=1))
    _, raw = make_record()
    db.issue(record_bytes=raw, principal=PHONE, idempotency_key="late",
             now=T0 + timedelta(seconds=2))


# ---------------------------------------------------------------------------------------------------
# decide (§3.2.2)

def _decide(db, pid, raw, *, fp=FP_PHONE, kind="ALLOWED", at=timedelta(minutes=1), n=2):
    return db.decide(operation_id=pid, decision_kind=kind, recomputed=raw, now=T0 + at,
                     signer_fp=fp if kind == "ALLOWED" else None,
                     signature="sig" if kind == "ALLOWED" else None, pre_images=[None] * n)


def test_decide_writes_decided_and_one_planned_row_per_effect(db):
    pid, raw = issue(db)
    out = db.decide(operation_id=pid, decision_kind="ALLOWED", recomputed=raw,
                    now=T0 + timedelta(minutes=1), signer_fp=FP_PHONE, signature="sig",
                    pre_images=[{"text": "old 0"}, None])
    assert out.decided
    view = db.load(pid)
    assert view.state == "DECIDED" and view.signer_fp == FP_PHONE and view.credential == PHONE
    assert [(e.idx, e.status, e.pre_image) for e in view.effects] == [
        (0, "PLANNED", {"text": "old 0"}), (1, "PLANNED", None)]
    again = _decide(db, pid, raw)
    assert again.state == "DECIDED" and not again.written


def test_decide_on_a_cancelled_pending_returns_cancelled_and_fires_nothing(db):
    pid, raw = issue(db)
    assert db.cancel(pid, by=PHONE, now=T0).state == "CANCELLED"
    out = _decide(db, pid, raw)
    assert (out.state, out.written) == ("CANCELLED", False)
    assert db.load(pid).effects == ()


def test_decide_after_expiry_is_expired(db):
    pid, raw = issue(db, ttl=timedelta(seconds=30))
    out = _decide(db, pid, raw)
    assert out.state == "EXPIRED" and db.outcome(pid).state == "EXPIRED"


@pytest.mark.parametrize("setup, fp, tier, reason", [
    ("UPDATE tokens SET generation = 2", FP_PHONE, "T2", "token rotated"),
    ("UPDATE keys SET revoked_at = '2026-10-10T12:00:00.000000Z' WHERE class = 'display-phone'",
     FP_PHONE, "T2", "key revoked"),
    ("", "SHA256:nobody", "T2", "key not enrolled"),
    ("", FP_WEAK, "T2", "key class presence does not cover T2"),
    ("", FP_PHONE, "T3", "key class display-phone does not cover T3"),
    ("UPDATE meta SET value = '1' WHERE key = 'policy_rev'", FP_PHONE, "T2", "policy changed"),
])
def test_decide_refuses_on_authority_and_policy(db, setup, fp, tier, reason):
    pid, raw = issue(db, tier=tier)
    if setup:
        with db.write() as conn:
            conn.execute(setup)
    out = _decide(db, pid, raw, fp=fp)
    assert (out.state, out.reason) == ("REFUSED", reason)
    assert db.load(pid).effects == ()


def test_laptop_presence_covers_t3(db):
    pid, raw = issue(db, tier="T3")
    assert _decide(db, pid, raw, fp=FP_LAPTOP).decided


def test_decide_compares_the_recompute_byte_for_byte(db):
    pid, raw = issue(db)
    _, moved = make_record(pid=pid, version=4)
    assert (_decide(db, pid, moved).state, db.outcome(pid).reason) == ("STALE", "changed")
    pid2, raw2 = issue(db, key="k2")
    _, raised = make_record(pid=pid2, tier="T3")
    assert _decide(db, pid2, raised).reason == "tier"
    pid3, raw3 = issue(db, key="k3")
    _, renorm = make_record(pid=pid3, params={"text": "NEW 0"})
    out = _decide(db, pid3, renorm)
    assert (out.state, out.reason) == ("REFUSED", "normaliser")


def test_a_faulted_snapshot_leaves_a_signed_pending_unspent_and_refuses_a_reported_one(db):
    pid, _ = issue(db)
    out = db.decide(operation_id=pid, decision_kind="ALLOWED", recomputed=None,
                    now=T0 + timedelta(minutes=1), signer_fp=FP_PHONE, signature="sig")
    assert (out.state, out.written) == ("WAITING", False)
    with db.read() as conn:
        assert conn.execute("SELECT signature FROM pendings").fetchone()[0] is None
    rp, _ = issue(db, key="rep", intent="reported", cap_exempt=True)
    out = db.decide(operation_id=rp, decision_kind="ALLOWED_REPORTED", recomputed=None,
                    now=T0 + timedelta(minutes=1))
    assert (out.state, out.reason) == ("REFUSED", "source unavailable")


def test_a_reported_decision_records_allowed_reported_and_no_signer(db):
    pid, raw = issue(db, key="rep", intent="reported", cap_exempt=True)
    assert _decide(db, pid, raw, kind="ALLOWED_REPORTED").decided
    view = db.load(pid)
    assert (view.decision_kind, view.signer_fp, view.credential) == ("ALLOWED_REPORTED", None, PHONE)
    pid2, raw2 = issue(db, key="sig")
    with pytest.raises(ValueError, match="cannot take"):
        _decide(db, pid2, raw2, kind="ALLOWED_REPORTED")


# ---------------------------------------------------------------------------------------------------
# apply units (§3.3)

def test_record_unit_writes_every_member_and_its_labels(db):
    pid = decided(db)
    out = db.record_unit(pid, [
        UnitResult(0, "APPLIED", native_id="spore-9", labels=[("text", "h0")]),
        UnitResult(1, "UNAPPLIED", reason="changed")], now=T0 + timedelta(minutes=2))
    assert out.recorded
    view = db.load(pid)
    assert [(e.status, e.native_id, e.reason) for e in view.effects] == [
        ("APPLIED", "spore-9", None), ("UNAPPLIED", None, "changed")]
    label = db.label_for("ok-0", "text", "h0")
    assert label is not None and label.fired_at == view.decided_at and label.signer_fp == FP_PHONE


def test_a_unit_a_concurrent_settler_recorded_returns_its_result_and_inserts_no_label(db):
    pid = decided(db)
    other = raw_conn(db)
    other.execute("UPDATE effects SET status = 'APPLIED', "
                  "native_id = CASE idx WHEN 0 THEN 'spore-7' END, "
                  "applied_at = '2026-10-10T12:01:30.000000Z' WHERE operation_id = ?", (pid,))
    other.close()
    out = db.record_unit(pid, [UnitResult(0, "APPLIED", native_id="spore-9", labels=[("text", "h")]),
                               UnitResult(1, "APPLIED", labels=[("text", "h")])],
                         now=T0 + timedelta(minutes=2))
    assert not out.recorded and [e.native_id for e in out.effects] == ["spore-7", None]
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


def test_a_half_recorded_unit_is_corrupt_and_writes_nothing(db):
    pid = decided(db)
    other = raw_conn(db)
    other.execute("UPDATE effects SET status = 'APPLIED', applied_at = "
                  "'2026-10-10T12:01:30.000000Z' WHERE operation_id = ? AND idx = 0", (pid,))
    other.close()
    with pytest.raises(JournalCorruptError, match="1 of 2"):
        db.record_unit(pid, [UnitResult(0, "APPLIED", labels=[("text", "h")]),
                             UnitResult(1, "APPLIED", labels=[("text", "h")])],
                       now=T0 + timedelta(minutes=2))
    assert db.load(pid).effects[1].status == "PLANNED"
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------------
# transient faults (§4.1.2)

def test_faults_back_off_and_the_tenth_ends_store_error_naming_this_fault(db):
    pid = decided(db)
    now = T0 + timedelta(minutes=2)
    waits = []
    for n in range(1, 10):
        out = db.record_fault(pid, [0, 1], f"fault {n}", now)
        assert (out.kind, out.attempts) == ("RETRY", n)
        waits.append((consent.parse_stamp(out.next_attempt_at) - now).total_seconds())
    assert waits == [5, 10, 20, 40, 80, 120, 120, 120, 120] and sum(waits) == 635
    out = db.record_fault(pid, [0, 1], "fault 10", now)
    assert (out.kind, out.attempts) == ("CAPPED", 10)
    view = db.load(pid)
    assert {(e.status, e.reason, e.attempts, e.next_attempt_at, e.last_fault)
            for e in view.effects} == {("UNAPPLIED", "store error: fault 10", 10, None, "fault 10")}
    assert db.record_fault(pid, [0, 1], "late", now).kind == "SETTLED"
    assert db.finish(pid, now).state == "COMMITTED_INCOMPLETE"


def _set_attempts(db, pid, n):
    with db.write() as conn:
        conn.execute("UPDATE effects SET attempts = ? WHERE operation_id = ?", (n, pid))


def test_a_fault_failing_between_members_rolls_the_whole_unit_back(db):
    pid = decided(db)
    _set_attempts(db, pid, 9)
    with db.write() as conn:
        conn.execute("CREATE TRIGGER die BEFORE UPDATE ON effects WHEN NEW.idx = 1 "
                     "BEGIN SELECT RAISE(ABORT, 'killed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="killed"):
        db.record_fault(pid, [0, 1], "fault 10", T0)
    assert {(e.attempts, e.status) for e in db.load(pid).effects} == {(9, "PLANNED")}


_KILL_CHILD = textwrap.dedent("""
    import os, sys
    from datetime import datetime, timezone
    import levain.cockpit.firejournal as fj
    real = fj.open_durable
    def dying(path):
        conn = real(path)
        conn.set_trace_callback(lambda sql: os._exit(137) if sql.strip() == sys.argv[3] else None)
        return conn
    fj.open_durable = dying
    fj.CockpitDB(sys.argv[1]).record_fault(sys.argv[2], [0, 1], "fault 10",
                                           datetime(2026, 10, 10, 12, 5, tzinfo=timezone.utc))
""")


@pytest.mark.parametrize("kill_at", ["COMMIT", "NEVER"])
def test_a_kill_at_the_tenth_fault_never_leaves_ten_attempts_planned(db, tmp_path, kill_at):
    pid = decided(db)
    _set_attempts(db, pid, 9)
    proc = subprocess.run([sys.executable, "-c", _KILL_CHILD, str(tmp_path), pid, kill_at],
                          capture_output=True, text=True)
    assert proc.returncode == (137 if kill_at == "COMMIT" else 0), proc.stderr
    pairs = {(e.attempts, e.status) for e in db.load(pid).effects}
    assert pairs == ({(9, "PLANNED")} if kill_at == "COMMIT" else {(10, "UNAPPLIED")})
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM effects WHERE attempts >= 10 "
                            "AND status = 'PLANNED'").fetchone()[0] == 0


def test_a_fault_on_a_half_planned_unit_is_corrupt(db):
    pid = decided(db)
    other = raw_conn(db)
    other.execute("UPDATE effects SET status = 'APPLIED', applied_at = "
                  "'2026-10-10T12:01:30.000000Z' WHERE operation_id = ? AND idx = 0", (pid,))
    other.close()
    with pytest.raises(JournalCorruptError, match="1 of 2"):
        db.record_fault(pid, [0, 1], "f", T0)
    assert db.load(pid).effects[1].attempts == 0


# ---------------------------------------------------------------------------------------------------
# finish (§3.4), cancel (§3.5), scheduling (§4.2)

def test_finish_waits_for_every_unit_then_commits(db):
    pid = decided(db)
    now = T0 + timedelta(minutes=2)
    db.record_unit(pid, [UnitResult(0, "APPLIED")], now)
    assert db.finish(pid, now).state == "applying"
    db.record_unit(pid, [UnitResult(1, "APPLIED")], now)
    assert db.finish(pid, now).state == "COMMITTED"
    assert db.finish(pid, now).state == "COMMITTED"           # the row count is 0, the outcome kept


def test_finish_names_each_unapplied_resource_and_reason(db):
    pid = decided(db)
    db.record_unit(pid, [UnitResult(0, "UNAPPLIED", reason="changed")], T0)
    db.record_unit(pid, [UnitResult(1, "UNAPPLIED", reason="store gone")], T0)
    out = db.finish(pid, T0)
    assert (out.state, out.reason) == ("COMMITTED_INCOMPLETE", "ok-0: changed; ok-1: store gone")


def test_cancel_only_takes_a_waiting_pending(db):
    pid = decided(db)
    assert db.cancel(pid, by=PHONE, now=T0).state == "applying"
    p2, _ = issue(db, key="k2")
    out = db.cancel(p2, by="token:other:1", now=T0)
    assert out.state == "CANCELLED"
    with db.read() as conn:
        assert conn.execute("SELECT cancelled_by FROM pendings WHERE pending_id = ?",
                            (p2,)).fetchone()[0] == "token:other:1"


def test_earliest_due_never_hides_a_never_faulted_unit(db):
    a = decided(db, key="a")                      # decided at T0+1m, no fault: due at T0+1m5s
    b = decided(db, key="b")
    db.record_unit(a, [UnitResult(0, "APPLIED")], T0)
    db.record_fault(b, [0], "f", T0 + timedelta(minutes=1))      # b/0 due at T0+1m5s too
    db.record_fault(b, [1], "f", T0 + timedelta(minutes=1))
    db.record_fault(b, [1], "f", T0 + timedelta(minutes=1))      # b/1 due at T0+1m10s
    assert db.earliest_due() == T0 + timedelta(minutes=1, seconds=5)
    assert db.due_ops(T0 + timedelta(minutes=1, seconds=4)) == []
    assert db.due_ops(T0 + timedelta(minutes=1, seconds=5)) == [a, b]


# ---------------------------------------------------------------------------------------------------
# integrity framing (§2)

@pytest.mark.parametrize("tamper", [
    "UPDATE pendings SET record = CAST('{}' AS BLOB)",
    "UPDATE effects SET store = 'inbox' WHERE idx = 1",
    "UPDATE effects SET resource_key = 'ok-9' WHERE idx = 0",
    "UPDATE effects SET next_attempt_at = 'soon' WHERE idx = 0",
    "UPDATE effects SET pre_image = '{nope' WHERE idx = 0",
    "DELETE FROM effects WHERE idx = 1",
])
def test_a_tampered_op_fails_closed_for_every_reader(db, tamper):
    pid = decided(db)
    other = raw_conn(db)
    other.execute(tamper)
    other.close()
    with pytest.raises(JournalCorruptError):
        db.load(pid)
    with pytest.raises(JournalCorruptError):
        db.record_unit(pid, [UnitResult(0, "APPLIED")], T0)
    with pytest.raises(JournalCorruptError):
        db.finish(pid, T0)


def test_a_record_rehashed_but_not_canonical_is_corrupt(db):
    pid, raw = issue(db)
    loose = raw.replace(b",", b", ", 1)
    other = raw_conn(db)
    other.execute("UPDATE pendings SET record = ?, record_sha256 = ?",
                  (loose, consent.record_sha256(loose)))
    other.close()
    with pytest.raises(JournalCorruptError, match="canonical"):
        _decide(db, pid, raw)
    c = raw_conn(db)
    assert c.execute("SELECT state FROM pendings").fetchone()[0] == "WAITING"
    c.close()


# ---------------------------------------------------------------------------------------------------
# labels (§5)

def test_label_for_takes_the_latest_fire_in_the_current_epoch_ties_to_insertion(db):
    a = decided(db, key="a")
    b = decided(db, key="b")                                   # same decided_at as a
    db.record_unit(a, [UnitResult(0, "APPLIED", labels=[("text", "h")])], T0)
    db.record_unit(b, [UnitResult(0, "APPLIED", labels=[("text", "h")])], T0)
    assert db.label_for("ok-0", "text", "h").operation_id == b
    assert db.label_for("ok-0", "text", "other") is None
    with db.write() as conn:
        conn.execute("UPDATE pendings SET decided_at = '2026-10-10T13:00:00.000000Z' "
                     "WHERE pending_id = ?", (a,))
        conn.execute("UPDATE labels SET fired_at = '2026-10-10T13:00:00.000000Z' "
                     "WHERE operation_id = ?", (a,))
    assert db.label_for("ok-0", "text", "h").operation_id == a
    with db.write() as conn:
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'epoch'")
    assert db.label_for("ok-0", "text", "h") is None
