"""PAIR P1, the journal half: per-device pairing inside ``cockpit.db`` (levain project_memory/1024-18/PAIR_DESIGN.md,
rev 6). Each test names the ruling whose path it holds."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import (
    OFFER_TTL,
    PAIRED_PREFIX,
    CockpitDB,
    DeviceRevoked,
    PairingError,
    _SCHEMA,
)

NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path: Path) -> CockpitDB:
    root = tmp_path / "cockpit"
    root.mkdir()
    return CockpitDB(root)


def _pair(db: CockpitDB, minted_by: str = "browser", label: str = "ipad") -> tuple[str, str]:
    code = db.mint_offer(label=label, minted_by=minted_by, now=NOW)
    out = db.redeem_offer(code, now=NOW)
    assert out is not None
    return out


def _gen(db: CockpitDB, device: str) -> int | None:
    return db.token_generation(PAIRED_PREFIX + device)


def _waiting(db: CockpitDB, principal: str, n: int = 2) -> list[str]:
    """File ``n`` WAITING pendings under ``principal`` straight into the journal (the broker's own insert path)."""
    from levain.cockpit.consent import encode, record_sha256  # noqa: F401 - the record shape issue() parses
    from levain.cockpit.firejournal import new_pending_id

    ids = []
    for i in range(n):
        pid = new_pending_id()
        rec = encode({"operation_id": pid, "verb": "spore_set_disposition", "tier": "T2", "intent": "sign",
                      "issued_at": utc_stamp(NOW), "expires_at": utc_stamp(NOW + timedelta(hours=1)),
                      "params": {"i": i}, "target": {"row_id": f"r{i}"}, "effects": []})
        db.issue(record_bytes=rec, principal=principal, idempotency_key=f"k{i}-{pid}", now=NOW)
        ids.append(pid)
    return ids


def _states(db: CockpitDB, ids: list[str]) -> list[str]:
    with db.read() as conn:
        return [conn.execute("SELECT state FROM pendings WHERE pending_id = ?", (i,)).fetchone()[0] for i in ids]


def test_rp10_3_two_concurrent_claims_of_one_code_yield_one_device(db: CockpitDB) -> None:
    """RP-10 (3) / RQ-2: the spend and the insert are one BEGIN IMMEDIATE: two claims racing one code, one device."""
    code = db.mint_offer(label="ipad", minted_by="browser", now=NOW)
    db.list_devices()      # the schema exists before the race
    barrier = threading.Barrier(8)
    got: list = []

    def claim() -> None:
        barrier.wait()
        got.append(CockpitDB(db.data_root).redeem_offer(code, now=NOW))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    winners = [g for g in got if g is not None]
    assert len(got) == 8 and len(winners) == 1, got
    assert [d["id"] for d in db.list_devices()] == [winners[0][0]]
    assert db.list_devices()[0]["paired_by"] == "browser" and db.list_devices()[0]["label"] == "ipad"


def test_a_code_is_single_use_expires_and_only_three_live(db: CockpitDB) -> None:
    """The offer's own semantics (RQ-1's constants): single use, OFFER_TTL, OFFERS_LIVE (the oldest voided)."""
    code = db.mint_offer(label=None, minted_by="browser", now=NOW)
    assert db.redeem_offer(code, now=NOW + OFFER_TTL) is None            # expired at its TTL
    codes = [db.mint_offer(label="x", minted_by="browser", now=NOW + timedelta(seconds=i)) for i in range(4)]
    assert db.redeem_offer(codes[0], now=NOW + timedelta(seconds=5)) is None       # voided by the 4th
    assert db.redeem_offer(codes[3], now=NOW + timedelta(seconds=5)) is not None
    assert db.redeem_offer(codes[3], now=NOW + timedelta(seconds=5)) is None       # used
    assert db.redeem_offer("nope", now=NOW) is None
    with pytest.raises(PairingError):
        db.mint_offer(label="\x1b[2J", minted_by="browser", now=NOW)


def test_rq8_an_offer_from_a_revoked_device_is_refused_and_a_second_revoke_bumps_nothing(db: CockpitDB) -> None:
    """RQ-8 (5): device A offers, A is revoked, the code is refused; revoke twice: generation bumped once, A's
    WAITING pendings CANCELLED."""
    a, _tok = _pair(db)
    principal = f"token:{PAIRED_PREFIX}{a}:1"
    assert db.ensure_token(PAIRED_PREFIX + a) == 1
    pend = _waiting(db, principal)
    code = db.mint_offer(label="child", minted_by=PAIRED_PREFIX + a, now=NOW)
    assert db.revoke_devices([a], by="browser", now=NOW) == [a]
    assert db.redeem_offer(code, now=NOW) is None
    assert _gen(db, a) == 2 and _states(db, pend) == ["CANCELLED", "CANCELLED"]
    assert db.revoke_devices([a], by="browser", now=NOW) == []
    assert _gen(db, a) == 2


def test_rs3_revoking_a_device_that_never_wrote_creates_and_bumps_its_generation(db: CockpitDB) -> None:
    """RS-3: a member with no ``tokens`` row (it never made a broker write) is still rotated, in the one transaction."""
    a, _ = _pair(db)
    assert _gen(db, a) is None
    assert db.revoke_devices([a], by="browser", now=NOW) == [a]
    assert _gen(db, a) == 2


def test_rp10_4_revoke_cascades_to_the_family_only(db: CockpitDB) -> None:
    """RP-10 (4) journal half / RQ-2 / RS-5: the closure over paired_by = 'paired-' || id takes the child and
    grandchild, never a sibling minted by a surface token or the box."""
    a, _ = _pair(db)
    b, _ = _pair(db, minted_by=PAIRED_PREFIX + a)
    c, _ = _pair(db, minted_by=PAIRED_PREFIX + b)
    sib, _ = _pair(db, minted_by="browser")
    boxed, _ = _pair(db, minted_by="box")
    pending_offer = db.mint_offer(label="later", minted_by=PAIRED_PREFIX + c, now=NOW)
    assert sorted(db.revoke_devices([a], by="browser", now=NOW)) == sorted([a, b, c])
    live = {d["id"]: d["revoked_at"] is None for d in db.list_devices()}
    assert live == {a: False, b: False, c: False, sib: True, boxed: True}
    assert db.redeem_offer(pending_offer, now=NOW) is None
    assert all(_gen(db, m) == 2 for m in (a, b, c)) and _gen(db, sib) is None
    with pytest.raises(PairingError):
        db.revoke_devices(["d-000000000000"], by="browser", now=NOW)


def test_rs4_a_credential_authenticated_before_the_revoke_mints_and_files_nothing_after_it(db: CockpitDB) -> None:
    """RS-4: offer from A, revoke A, A's pre-revoke credential offers again (refused) and its earlier code is refused;
    and RQ-3 as amended: issue() refuses the revoked principal inside its own write(), beside the cap."""
    a, _ = _pair(db)
    early = db.mint_offer(label="x", minted_by=PAIRED_PREFIX + a, now=NOW)
    assert db.ensure_token(PAIRED_PREFIX + a) == 1
    db.revoke_devices([a], by="browser", now=NOW)
    with pytest.raises(DeviceRevoked):
        db.mint_offer(label="x", minted_by=PAIRED_PREFIX + a, now=NOW)
    assert db.redeem_offer(early, now=NOW) is None
    for gen in (1, 2):      # the pre-revoke generation and the rotated one alike
        with pytest.raises(DeviceRevoked):
            _waiting(db, f"token:{PAIRED_PREFIX}{a}:{gen}", n=1)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pendings").fetchone()[0] == 0


def test_verify_device_hash_only_and_throttled_last_seen(db: CockpitDB) -> None:
    """RQ-5's surviving parts (in RQ-1's tables): the token is never stored, the lookup is by its hash, last_seen
    moves at most once a minute."""
    a, tok = _pair(db)
    with db.read() as conn:
        dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM paired_devices"))
    assert tok not in dump
    assert db.verify_device(tok, now=NOW) == a
    first = db.list_devices()[0]["last_seen"]
    assert db.verify_device(tok, now=NOW + timedelta(seconds=30)) == a
    assert db.list_devices()[0]["last_seen"] == first
    assert db.verify_device(tok, now=NOW + timedelta(seconds=61)) == a
    assert db.list_devices()[0]["last_seen"] != first
    assert db.verify_device(tok + "x", now=NOW) is None
    db.revoke_devices([a], by="browser", now=NOW)
    assert db.verify_device(tok, now=NOW) is None and db.device_live(a) is False


def test_rs5_an_existing_3_journal_gains_the_two_tables_and_nothing_else_moves(tmp_path: Path) -> None:
    """RS-5 upgrade in place: a /3 journal from before PAIR (built here from the schema minus the two tables) opens
    with them added; every pre-existing object's SQL and every meta row are unchanged."""
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).meta("format")          # a current journal
    with sqlite3.connect(root / "cockpit.db") as conn:
        conn.execute("DROP TABLE paired_devices")
        conn.execute("DROP TABLE pair_offers")
    with sqlite3.connect(root / "cockpit.db") as conn:
        before = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=str)
        meta_before = sorted(conn.execute("SELECT key, value FROM meta").fetchall())
    assert not any(n in ("paired_devices", "pair_offers") for _t, n, _s in before)
    reopened = CockpitDB(root)
    assert reopened.meta("format") == "levain-cockpit/3"
    assert reopened.list_devices() == []
    with sqlite3.connect(root / "cockpit.db") as conn:
        after = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=str)
        meta_after = sorted(conn.execute("SELECT key, value FROM meta").fetchall())
    added = sorted({n for _t, n, _s in after} - {n for _t, n, _s in before})
    assert added == ["pair_offers", "paired_devices", "sqlite_autoindex_pair_offers_1",
                     "sqlite_autoindex_paired_devices_1", "sqlite_autoindex_paired_devices_2"]
    assert [r for r in after if r[1] not in added] == before
    assert meta_after == meta_before
    assert sum("paired_devices" in s or "pair_offers" in s for s in _SCHEMA) == 2
