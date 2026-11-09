"""Trust-tiering S3a part 2a (DESIGN rev 5.2 §3.3, §3.4, §3.7, S3a-2 MUST): the cockpit reads who may
sign only from the verified replay of the signed authority log, captured once per request.

Each refusal here FAILS on the /2 shape (a ``keys`` table and ``meta.policy_rev`` read directly): a
planted key row, a direct meta write and a truncated log grant nothing, a /2 store migrates and refuses
every signed act until ``ledger init``, and no code builds a snapshot outside ``RequestAuthority.capture``
and ``levain.authority.append``.
"""
from __future__ import annotations

import ast
import functools
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import levain
import levain.authority as authority
from levain.authority import VerifierUnavailable, anchor_path
from levain.cockpit.firejournal import CockpitDB, STORE_FORMAT
from levain.cockpit.keys import KeyError_, fingerprint_of
from tests.cockpit_authority_rig import RealKey, enrol, enrol_any, revoke, revoke_any, root_of
from tests.test_cockpit_k2b_broker import (  # noqa: F401 - fixtures
    BROWSER,
    Key,
    _broker,
    _move,
    _refused,
    _rows,
    _state,
    home,
    keydir,
    spores,
)
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture

LEVAIN = Path(levain.__file__).parent


@pytest.fixture
def broker(home: Path, spores: Path):
    b = _broker(home, spores)
    try:
        yield b
    finally:
        b.stop()


def _sign_pending(b, spores, key: Key, origin: str = "k.seed") -> tuple[str, str]:
    pid = _move(b, spores, origin)["pending_id"]
    return pid, key.sign(b.outcome(pid)["record"].encode())


def _raw(b) -> sqlite3.Connection:
    return sqlite3.connect(b.data_root / "cockpit.db", isolation_level=None)


# --- a planted key row is ignored -----------------------------------------------------------------------

def test_a_planted_keys_row_grants_nothing(broker, spores, keydir):
    Key(keydir, "phone").enroll(broker, "display-phone")
    planted = Key(keydir, "planted")
    conn = _raw(broker)
    # the /2 projection, recreated and written as a same-uid agent would: nothing reads it
    conn.execute("CREATE TABLE keys (fingerprint TEXT PRIMARY KEY, principal TEXT NOT NULL UNIQUE, "
                 "public_key TEXT NOT NULL, class TEXT NOT NULL, enrolled_at TEXT NOT NULL, "
                 "enrolled_via TEXT NOT NULL, revoked_at TEXT, compromised_since TEXT)")
    conn.execute("INSERT INTO keys VALUES (?, ?, ?, 'laptop-presence', '2026-10-10T00:00:00.000000Z', "
                 "'laptop cli', NULL, NULL)", (planted.fingerprint, planted.principal, planted.public))
    conn.close()
    pid, sig = _sign_pending(broker, spores, planted)
    _refused("bad_signature", broker.submit, pid, sig)
    assert _state(broker, pid) == "WAITING"


def test_a_directly_inserted_unsigned_link_refuses_the_whole_log(broker, spores, keydir):
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    planted = Key(keydir, "planted")
    pid, _ = _sign_pending(broker, spores, planted)
    conn = _raw(broker)
    head = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
    conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                 "VALUES (?, ?, '{}', ?, 'forged', 'h')", (head[0] + 1, head[1], phone.fingerprint))
    conn.close()
    auth = broker.capture()
    assert auth.authority is None and "authority log is refused" in str(auth.authority_error)
    # nothing signs and nothing issues against a refused log, a token-reported write included
    _refused("authority_unavailable", broker.submit, pid, phone.sign(broker.outcome(pid)["record"].encode()))
    _refused("authority_unavailable", _move, broker, spores, "k.seed2", intent="reported")


# --- a direct meta write grants nothing ---------------------------------------------------------------

def test_a_direct_meta_write_grants_nothing(broker, spores, keydir):
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    pid, sig = _sign_pending(broker, spores, phone)
    conn = _raw(broker)
    # the retired integer fence, and a meta row naming the head: neither moves anything
    conn.execute("INSERT INTO meta (key, value) VALUES ('policy_rev', '0')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('authority_seq', '99')")
    conn.close()
    head = broker.capture().authority
    assert broker.capture().fence == {"authority_store_id": head.store_id, "authority_seq": 2,
                                      "authority_head_hash": head.head_hash}
    assert broker.submit(pid, sig)["state"] == "COMMITTED"


def test_deleting_the_uninitialised_marker_grants_nothing_on_an_empty_log(broker, spores, keydir):
    phone = Key(keydir, "phone")
    conn = _raw(broker)
    conn.execute("INSERT INTO meta (key, value) VALUES ('authority_uninitialised', 'authority migrated')")
    conn.execute("DELETE FROM meta WHERE key = 'authority_uninitialised'")
    conn.close()
    pid, sig = _sign_pending(broker, spores, phone)
    err = _refused("not_initialised", broker.submit, pid, sig)
    assert "ledger init" in str(err)


# --- a truncated log is refused at open ---------------------------------------------------------------

def test_a_truncated_log_is_refused_by_its_anchor(home, spores, keydir):
    b = _broker(home, spores)
    try:
        Key(keydir, "phone").enroll(b, "display-phone")
        assert len(_rows(b, "SELECT seq FROM authority_log")) == 2
    finally:
        b.stop()
    conn = sqlite3.connect(home / "data" / "cockpit.db", isolation_level=None)
    conn.execute("DELETE FROM authority_log WHERE seq = 2")          # the phone's enrolment rolled back
    conn.close()
    b = _broker(home, spores)                                         # open again
    try:
        auth = b.capture()
        assert auth.authority is None and "behind its anchor" in str(auth.authority_error)
        assert "anchor reset" in str(auth.authority_error)
        _refused("authority_unavailable", _move, b, spores, intent="reported")
    finally:
        b.stop()


# --- a whole-folder restore cannot carry the anchor back (L2 1012+80 A) -----------------------------------

def _phone_after(root: Path) -> tuple[Any, Any]:
    from levain.cockpit.authority import RequestAuthority

    authority.PROCESS_CACHE.clear()
    with CockpitDB(root).read() as conn:
        auth = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=anchor_path(root), source="t")
    return auth.authority, auth.authority_error


def test_a_restore_without_the_excluded_anchor_dir_cannot_unrevoke_a_key(tmp_path, monkeypatch):
    """The anchor lives in its own directory, which Time Machine never backs up, so a restore of the
    data root brings back the old log beside the CURRENT anchor, and the capture refuses."""
    root, keys = tmp_path / "data", tmp_path / "keys"
    root.mkdir()
    keys.mkdir()
    db = CockpitDB(root)
    db.meta("format")
    lap, phone = RealKey(keys, "lap"), RealKey(keys, "phone")
    enrol(db, lap, "laptop-presence")
    enrol(db, phone, "display-phone", by=lap)
    excluded = anchor_path(root).parent.name
    backup = tmp_path / "backup"
    shutil.copytree(root, backup, ignore=shutil.ignore_patterns(excluded))   # what Time Machine copies
    revoke(db, phone, by=lap, compromised_since=2)
    snap, err = _phone_after(root)
    assert err is None and not snap.key(phone.fingerprint).active
    for p in root.iterdir():                                                   # the restore
        if p.name != excluded:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(backup, root, dirs_exist_ok=True)
    snap, err = _phone_after(root)
    assert snap is None and "behind its anchor" in str(err) and "anchor reset" in str(err)


@pytest.mark.skipif(sys.platform != "darwin", reason="the Time Machine exclusion is macOS's")
def test_the_anchor_directory_is_excluded_from_time_machine(tmp_path, monkeypatch):
    root, keys = tmp_path / "data", tmp_path / "keys"
    root.mkdir()
    keys.mkdir()
    db = CockpitDB(root)
    db.meta("format")
    enrol(db, RealKey(keys, "lap"), "laptop-presence")
    anchor = anchor_path(root)
    assert anchor.is_file() and anchor.parent != root
    attr = "com.apple.metadata:com_apple_backup_excludeItem"

    def value(p: Path) -> str:
        return subprocess.run(["xattr", "-px", attr, str(p)], capture_output=True, text=True, check=True).stdout
    # the oracle is what tmutil itself writes (`tmutil isexcluded` is vacuous here: the test's temp
    # directory sits under a path Time Machine already skips)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run(["tmutil", "addexclusion", str(scratch)], capture_output=True, check=True)
    assert value(anchor.parent) == value(scratch)
    names = subprocess.run(["xattr", str(anchor)], capture_output=True, text=True, check=True).stdout.split()
    assert attr not in names                         # on the directory, never the file a rename replaces


# --- a panel etag moves when the authority flips (L1 1012+80 B) -----------------------------------------

def test_a_panel_etag_moves_when_the_authority_is_refused_and_again_when_it_is_restored(rig_broker, keydir):
    from tests.test_cockpit_k2b_section import _edit, _pv

    rig, b, _db = rig_broker
    rig.ck.attach_authority(b.capture)                                  # as `levain serve` wires it
    laptop = Key(keydir, "laptop").enroll(b, "laptop-presence")
    out = _edit(rig, b, _pv(rig), "signed State body", "sec-etag", intent=None)
    assert b.submit(out["pending_id"], laptop.sign(out["record"].encode()))["state"] == "COMMITTED"
    signed = rig.ck.panel("section:state")
    assert signed["value"]["provenance"]["label"] == "signed · reported"
    anchor = anchor_path(b.data_root)
    good = anchor.read_bytes()
    behind = authority.read_anchor(anchor)
    anchor.write_text(json.dumps({"format": authority.ANCHOR_FORMAT, "store_id": behind.store_id,
                                  "seq": behind.seq + 1, "head_hash": "0" * 64}))   # the log is now behind it
    refused = rig.ck.panel("section:state")
    assert refused["value"]["provenance"]["label"] == "unverified"
    assert refused["etag"] != signed["etag"]
    anchor.write_bytes(good)
    restored = rig.ck.panel("section:state")
    assert restored["etag"] != refused["etag"]
    assert restored["value"]["provenance"]["label"] == "signed · reported"


# --- a replayed reported pending on a refused log is a 503, never STALE (L1 1012+80 C) ------------------

def _refuse_the_log(data_root: Path) -> bytes:
    """Move the anchor one link past the log, so every capture refuses it; returns the bytes to restore."""
    anchor = anchor_path(data_root)
    good = anchor.read_bytes()
    at = authority.read_anchor(anchor)
    anchor.write_text(json.dumps({"format": authority.ANCHOR_FORMAT, "store_id": at.store_id,
                                  "seq": at.seq + 1, "head_hash": "0" * 64}))
    return good


def test_a_replayed_reported_pending_on_a_refused_log_is_unavailable_and_stays_waiting(broker, spores, keydir):
    from datetime import timedelta

    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.firejournal import new_pending_id
    from tests.test_cockpit_k2b_broker import _target

    Key(keydir, "phone").enroll(broker, "display-phone")
    tgt = _target(spores, "k.seed")
    dead = new_pending_id()
    with broker._admitted() as db:                 # the request that died after `issued`
        built = broker._build(db, "spore_set_disposition", {"disposition": "agenda"}, tgt, dead, at_issue=True)
        n = broker._now()
        raw = broker._record(pid=dead, verb="spore_set_disposition", intent="reported", built=built,
                             params={"disposition": "agenda"}, tier="T2", fence=broker.capture().fence,
                             issued_at=utc_stamp(n), expires_at=utc_stamp(n + timedelta(minutes=15)))
        db.issue(record_bytes=raw, principal="token:browser:1", idempotency_key="died", now=n, cap_exempt=True)
    good = _refuse_the_log(broker.data_root)

    def replay() -> dict[str, Any]:
        return broker.issue(verb="spore_set_disposition", params={"disposition": "agenda"}, target=tgt, tier="T2",
                            credential=BROWSER, intent="reported", idempotency_key="died")
    err = _refused("authority_unavailable", replay)
    assert err.status == 503
    assert _state(broker, dead) == "WAITING"
    anchor_path(broker.data_root).write_bytes(good)
    out = replay()
    assert (out["pending_id"], out["state"], out["replay"]) == (dead, "COMMITTED", True), out


# --- a /2 store migrates and refuses signed acts until init -------------------------------------------

_V2_KEYS = ("CREATE TABLE keys (fingerprint TEXT PRIMARY KEY, principal TEXT NOT NULL UNIQUE, "
            "public_key TEXT NOT NULL, class TEXT NOT NULL, enrolled_at TEXT NOT NULL, enrolled_via TEXT NOT NULL, "
            "revoked_at TEXT, compromised_since TEXT)")


def _as_v2(data: Path, rows: list[tuple[Any, ...]]) -> None:
    """Turn a /3 journal back into the /2 shape: no authority log, the keys and policy tables, the
    integer fence and its trigger set."""
    conn = sqlite3.connect(data / "cockpit.db", isolation_level=None)
    conn.execute("DROP TABLE authority_log")
    conn.execute(_V2_KEYS)
    conn.execute("CREATE TABLE policy (key TEXT PRIMARY KEY, value TEXT NOT NULL, rev INTEGER NOT NULL)")
    bump = "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'label_rev';"
    conn.execute(f"CREATE TRIGGER label_rev_on_key_insert AFTER INSERT ON keys BEGIN {bump} END")
    conn.executemany("INSERT INTO keys VALUES (?, ?, ?, ?, ?, 'laptop cli', ?, ?)", rows)
    conn.execute("UPDATE meta SET value = 'levain-cockpit/2' WHERE key = 'format'")
    conn.execute("INSERT INTO meta (key, value) VALUES ('policy_rev', '0')")
    conn.close()


def test_a_v2_store_migrates_and_refuses_signed_acts_until_init(home, spores, keydir):
    b = _broker(home, spores)
    try:
        waiting = _move(b, spores)["pending_id"]                      # a WAITING pending from before
    finally:
        b.stop()
    phone, gone = RealKey(keydir, "phone"), RealKey(keydir, "gone")
    _as_v2(home / "data", [
        (phone.fingerprint, phone.principal, phone.public, "display-phone", "2026-10-01T00:00:00.000000Z", None, None),
        (gone.fingerprint, gone.principal, gone.public, "display-phone", "2026-10-01T00:00:00.000000Z",
         "2026-10-05T00:00:00.000000Z", None)])
    label_rev = sqlite3.connect(home / "data" / "cockpit.db").execute(
        "SELECT value FROM meta WHERE key = 'label_rev'").fetchone()[0]

    b = _broker(home, spores)                                         # the new code's first open
    try:
        tables = {r[0] for r in _rows(b, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"authority_log", "legacy_keys"} <= tables and not {"keys", "policy"} & tables
        meta = dict(_rows(b, "SELECT key, value FROM meta"))
        assert meta["format"] == STORE_FORMAT == "levain-cockpit/3"
        assert "policy_rev" not in meta and meta["authority_uninitialised"] == "authority migrated"
        assert int(meta["label_rev"]) == int(label_rev) + 1
        assert sorted(_rows(b, "SELECT fingerprint, revoked_at IS NOT NULL FROM legacy_keys")) == sorted(
            [(phone.fingerprint, 0), (gone.fingerprint, 1)])
        assert _rows(b, "SELECT state, reason FROM pendings WHERE pending_id = ?", waiting) == [
            ("STALE", "authority migrated")]
        # the migrated keys authorise nothing: a legacy key's signature is refused, naming init
        pid, sig = _sign_pending(b, spores, phone, "k.seed2")
        assert "ledger init" in str(_refused("not_initialised", b.submit, pid, sig))
        # an initialisation in progress (links appended, the marker still set) still refuses
        laptop = RealKey(keydir, "laptop")
        enrol_any(b.db, laptop, "laptop-presence")
        enrol_any(b.db, phone, "display-phone")
        pid2, sig2 = _sign_pending(b, spores, phone, "k.loop")
        assert "ledger init" in str(_refused("not_initialised", b.submit, pid2, sig2))
    finally:
        b.stop()
    # a 0.9 binary refuses the upgraded store (its format marker is /3)
    assert sqlite3.connect(home / "data" / "cockpit.db").execute(
        "SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-cockpit/3"


def test_a_store_of_an_unknown_format_is_still_refused(tmp_path):
    from levain.cockpit.firejournal import CockpitFormatError
    CockpitDB(tmp_path).meta("format")
    conn = sqlite3.connect(tmp_path / "cockpit.db", isolation_level=None)
    conn.execute("UPDATE meta SET value = 'levain-cockpit/1' WHERE key = 'format'")
    conn.close()
    with pytest.raises(CockpitFormatError, match="levain-cockpit/1"):
        CockpitDB(tmp_path).meta("format")


# --- signers, labels and label_rev ride the snapshot ----------------------------------------------------

def test_enrol_and_revoke_each_move_label_rev(broker, keydir):
    def rev() -> int:
        return int(_rows(broker, "SELECT value FROM meta WHERE key = 'label_rev'")[0][0])
    before = rev()
    phone = Key(keydir, "phone").enroll(broker, "display-phone")       # genesis + phone: two links
    assert rev() == before + 2
    revoke_any(broker.db, phone)
    assert rev() == before + 3


def test_a_revoked_key_signs_nothing_and_its_compromised_fires_read_revoked(broker, spores, keydir):
    from levain.cockpit.authority import LabelKey

    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    pid, sig = _sign_pending(broker, spores, phone)
    assert broker.submit(pid, sig)["label"] == "signed · reported"
    key = LabelKey("spore:k.seed", {"disposition": "agenda"})
    assert broker.capture().labels.label(key) == "signed · reported"
    fired_at = broker.capture().authority.seq
    revoke_any(broker.db, phone, compromised_since=fired_at)
    assert broker.capture().labels.label(key) == "signed by a revoked key"
    pid2, sig2 = _sign_pending(broker, spores, phone, "k.seed2")
    _refused("bad_signature", broker.submit, pid2, sig2)


def test_a_compromise_after_the_fire_leaves_its_label(broker, spores, keydir):
    from levain.cockpit.authority import LabelKey

    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    pid, sig = _sign_pending(broker, spores, phone)
    broker.submit(pid, sig)
    Key(keydir, "other").enroll(broker, "presence")                    # the head moves past the fire
    revoke_any(broker.db, phone, compromised_since=broker.capture().authority.seq)
    assert broker.capture().labels.label(LabelKey("spore:k.seed", {"disposition": "agenda"})) == "signed · reported"


def test_an_append_stales_a_waiting_signed_pending(broker, spores, keydir):
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    pid, sig = _sign_pending(broker, spores, phone)
    Key(keydir, "later").enroll(broker, "presence")
    out = broker.submit(pid, sig)
    assert (out["state"], out["reason"]) == ("STALE", "authority changed")


def test_one_request_captures_once(broker, spores, keydir, monkeypatch):
    from levain.cockpit.authority import RequestAuthority

    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    pid, sig = _sign_pending(broker, spores, phone)
    calls = []
    real = RequestAuthority.capture.__func__

    def counting(cls, *a, **kw):
        calls.append(1)
        return real(cls, *a, **kw)
    monkeypatch.setattr(RequestAuthority, "capture", classmethod(counting))
    assert broker.submit(pid, sig)["state"] == "COMMITTED"
    assert len(calls) == 1


# --- L3 r1 (1012+80): the fence's head hash, the migration's lock, the anchor read's order ---------------

def test_a_pending_issued_on_a_replaced_suffix_ends_stale(broker, spores, keydir):
    """F1: a fork after a failed anchor advance puts a second validly signed link at the same seq. A
    pending issued on the first is decided under the second, so its fence must name the head hash."""
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    root = root_of(broker.db)
    a, b = RealKey(keydir, "a"), RealKey(keydir, "b")
    with broker.db.autocommit() as conn:                              # suffix A; its anchor advance failed
        authority.sign_and_append(conn, {"op": "enrol", "fingerprint": a.fingerprint, "public_key": a.public,
                                         "class": "presence", "custody": "unstated"}, signer_fp=root.fingerprint, sign=root.sign_link,
                                  clock=lambda: datetime.now(timezone.utc), anchor=None)
    issued_on = broker.capture().authority
    pid, sig = _sign_pending(broker, spores, phone)
    raw = _raw(broker)
    raw.execute("DELETE FROM authority_log WHERE seq = ?", (issued_on.seq,))
    raw.close()
    replaced = enrol(broker.db, b, "presence", by=root)               # suffix B at the same seq
    assert replaced.seq == issued_on.seq and replaced.head_hash != issued_on.head_hash
    assert broker.capture().authority_error is None
    out = broker.submit(pid, sig)
    assert (out["state"], out["reason"]) == ("STALE", "authority changed")


def _hold_lock(data: Path) -> subprocess.Popen:
    """Another process holding ``cockpit.lock`` exclusively, as a live ``levain serve`` does."""
    code = ("import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(data / "cockpit.lock")], stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


def _a_v2_store(home: Path, spores: Path, keydir: Path) -> str:
    """A /2 journal (started reported) holding one WAITING pending; its id."""
    b = _broker(home, spores)
    try:
        waiting = _move(b, spores)["pending_id"]
    finally:
        b.stop()
    phone = RealKey(keydir, "phone")
    _as_v2(home / "data", [(phone.fingerprint, phone.principal, phone.public, "display-phone",
                            "2026-10-01T00:00:00.000000Z", None, None)])
    return waiting


def _still_v2(data: Path, waiting: str) -> None:
    conn = sqlite3.connect(data / "cockpit.db")
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-cockpit/2"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "keys" in tables and "legacy_keys" not in tables
        assert conn.execute("SELECT state FROM pendings WHERE pending_id = ?", (waiting,)).fetchone()[0] == "WAITING"
    finally:
        conn.close()


def test_a_cli_open_of_a_v2_store_under_a_live_server_refuses_and_migrates_nothing(home, spores, keydir, capsys):
    """F2 (a): the one-way migration runs only under ``cockpit.lock``; a server holding it (an old
    ``levain serve`` still running) leaves the store /2 and the command names what to do."""
    from levain import cli

    waiting = _a_v2_store(home, spores, keydir)
    data = home / "data"
    holder = _hold_lock(data)
    try:
        assert cli.main(["cockpit", "keys", "--data-root", str(data)]) == 1
        assert "levain serve" in capsys.readouterr().err
        _still_v2(data, waiting)
    finally:
        holder.kill()
        holder.wait()
    assert cli.main(["cockpit", "keys", "--data-root", str(data)]) == 0     # the lock free, it migrates
    assert sqlite3.connect(data / "cockpit.db").execute(
        "SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == STORE_FORMAT


def test_keys_on_a_governed_v2_store_refuses_before_migrating(home, spores, keydir, capsys):
    """F2 (b): the install-class check comes before any mutation: a governed /2 journal stays /2."""
    from levain import cli

    waiting = _a_v2_store(home, spores, keydir)
    data = home / "data"
    conn = sqlite3.connect(data / "cockpit.db", isolation_level=None)
    conn.execute("UPDATE meta SET value = 'governed' WHERE key = 'install_class'")
    conn.close()
    assert cli.main(["cockpit", "keys", "--data-root", str(data)]) == 1
    assert "governed" in capsys.readouterr().err
    _still_v2(data, waiting)


@pytest.mark.parametrize("via", ["broker", "keys"])
def test_the_anchor_is_read_before_the_snapshot_is_pinned(broker, keydir, monkeypatch, via):
    """F3: an append that commits and advances the anchor after the read transaction pinned its
    snapshot leaves the snapshot behind the anchor; reading the anchor first, it is not refused. The
    broker's capture (its start's ``_advance_anchor`` included) pins at the replay's first SELECT;
    ``levain cockpit keys`` would pin at its install-class check if that ran first."""
    from levain.cockpit import keys as cockpit_keys

    Key(keydir, "phone").enroll(broker, "display-phone")
    root = root_of(broker.db)
    late = RealKey(keydir, "late")
    fired: list[int] = []

    def advance() -> None:
        if not fired:
            fired.append(1)
            enrol(broker.db, late, "presence", by=root)              # link N+1 commits, the anchor moves
    if via == "broker":
        real = authority.snapshot

        def pinned_then_advanced(conn, **kw):
            snap = real(conn, **kw)                                   # the snapshot is pinned here
            advance()
            return snap
        monkeypatch.setattr(authority, "snapshot", pinned_then_advanced)
        auth = broker.capture()
        assert fired and auth.authority_error is None, auth.authority_error
        seen = auth.authority.seq
    else:
        real_check = cockpit_keys._check_reported

        def checked_then_advanced(conn):
            real_check(conn)                                          # a SELECT: pinned here if it runs first
            advance()
        monkeypatch.setattr(cockpit_keys, "_check_reported", checked_then_advanced)
        rows, note = cockpit_keys.keys(broker.data_root, now=datetime.now(timezone.utc))
        assert fired and note is None, note
        seen = max(r.enrolled_seq for r in rows)
    assert authority.read_anchor(anchor_path(broker.data_root)).seq == seen + 1


# --- structural fences ----------------------------------------------------------------------------

_PARSED: dict[str, ast.AST] = {}


def _parsed(source: str) -> ast.AST:
    """``ast.parse`` of a source text, remembered by that text. The guards below only read a tree, never change one,
    so every scan of the same text shares one parse; a planted copy's edited file has other text and parses afresh."""
    if source not in _PARSED:
        _PARSED[source] = ast.parse(source)
    return _PARSED[source]


def _by_tree(scan):
    """Remember a scan of one parsed tree by the tree and its other arguments. A scan only reads the tree, and the
    parse above hands the same tree to every scan of the same text, so a file that has not changed is not walked
    again; a list result is handed out as a copy so no caller can change what is remembered."""
    cached = functools.lru_cache(maxsize=None)(scan)

    @functools.wraps(scan)
    def run(*args):
        result = cached(*args)
        return list(result) if isinstance(result, list) else result
    return run


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _calls(tree: ast.AST, module_aliases: set[str], name_aliases: set[str], attr: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in name_aliases:
                out.append(node)
            elif isinstance(f, ast.Attribute) and f.attr == attr and _dotted(f.value) in module_aliases:
                out.append(node)
    return out


def _module_aliases(tree: ast.AST, module: str) -> set[str]:
    """Every dotted name ``module`` is reachable as: ``import a.b.c [as X]``, ``import a.b as X``
    (``X.c``), a bare ``import a...`` (``a.b.c``), ``from a.b import c [as X]``, ``from a import b``
    (``b.c``)."""
    out: set[str] = set()

    def bind(full: str, local: str) -> None:
        if module == full or module.startswith(full + "."):
            out.add(local + module[len(full):])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    bind(a.name, a.asname)
                elif a.name.split(".")[0] == module.split(".")[0]:
                    out.add(module)            # a bare import binds the top package, and the dotted path
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                bind(f"{node.module}.{a.name}", a.asname or a.name)
    return out


def _uses(path: Path, names: tuple[str, ...], module: str = "levain.authority") -> list[tuple[str, str, int]]:
    """Calls in ``path`` of any of ``names`` from ``module``, as (name, enclosing function, line),
    through a from-import of the name or any alias of the module (:func:`_module_aliases`)."""
    return _uses_in(_parsed(path.read_text(encoding="utf-8")), names, module)


@_by_tree
def _uses_in(tree: ast.AST, names: tuple[str, ...], module: str) -> list[tuple[str, str, int]]:
    modules = _module_aliases(tree, module)
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module and not node.level:
            imported.update({a.asname or a.name: a.name for a in node.names if a.name in names})
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    found = []
    for name in names:
        aliases = {k for k, v in imported.items() if v == name}
        for call in _calls(tree, modules, aliases, name):
            fn, node = "<module>", call
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = node.name
                    break
            found.append((name, fn, call.lineno))
    return found


@_by_tree
def _key_act_callers(tree: ast.AST) -> bool:
    """Whether ``tree`` reaches ``levain.cockpit.keys``' enroll, revoke or ledger_init: a from-import of
    any, or a call through any alias of the module."""
    acts = ("enroll", "revoke", "ledger_init")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "levain.cockpit.keys" \
                and {a.name for a in node.names} & set(acts):
            return True
    return bool(_uses_in(tree, acts, "levain.cockpit.keys"))


# The call sites allowed to build a snapshot, by name. Each entry carries its reason; the scan is never loosened.
SNAPSHOT_BUILDERS = {
    (LEVAIN / "cockpit" / "authority.py", "capture"),
    # DESIGN_REV3 §5.3's coverage replay: opened mode=ro, anchored by read_anchor before its read transaction, and it
    # appends nothing; its decision is re-checked at use (graduation's precondition on the binding record). Routing
    # it through RequestAuthority.capture would pull the cockpit request path into the autonomic engine for no
    # freshness gain (both snapshot at one instant).
    (LEVAIN / "graduation_policy.py", "_replay"),
    # AgentAuthority.replay is the autonomic engine's read-only anchored replay: the anchor is read before its
    # transaction, it appends nothing, and it was written to be the same kind of reader as graduation_policy._replay.
    (LEVAIN / "agent_authority.py", "replay"),
    # AuthorityReplica._verify (S10b-B1, 1104-28_S10B_INSTALL_DESIGN §2: "runs authority.snapshot(conn)") is the WRITE
    # path's verify, the same role as levain.authority.append's in-transaction replay: it runs inside the replica's
    # BEGIN IMMEDIATE over the dispatch-owned replica (never the cockpit's store), decides COMMIT or ROLLBACK and feeds
    # advance_anchor; append hands back only the (seq, hash) head, never a policy. The replica's READ path is
    # replay() -> AgentAuthority.replay above. So no request reads policy from it. Held by
    # test_the_replica_verify_is_only_the_append_transactions_check.
    (LEVAIN / "agents" / "authority_replica.py", "_verify"),
}


def test_no_snapshot_is_built_outside_capture_and_append():
    """DESIGN §3.4: ``RequestAuthority.capture`` is the only snapshot builder (``levain.authority.append``
    replays inside itself), so no reader can fall back to a second head."""
    offenders = []
    for path in sorted(LEVAIN.rglob("*.py")):
        if path == LEVAIN / "authority.py":
            continue
        for name, fn, line in _uses(path, ("snapshot", "_replay", "_replay_entry")):
            if (path, fn) not in SNAPSHOT_BUILDERS:
                offenders.append(f"{path.relative_to(LEVAIN.parent)}:{line} {name}() in {fn}")
    assert offenders == []


def test_the_fence_sees_a_planted_snapshot_call(tmp_path):
    # the scan above is not vacuous: a reader that builds its own snapshot is found
    probe = tmp_path / "probe.py"
    probe.write_text("from levain import authority as a\nfrom levain.authority import snapshot as s\n"
                     "def reader(conn):\n    return a.snapshot(conn), s(conn)\n")
    assert sorted(_uses(probe, ("snapshot",))) == [("snapshot", "reader", 4), ("snapshot", "reader", 4)]


def test_the_replica_verify_is_only_the_append_transactions_check():
    """SNAPSHOT_BUILDERS' ``authority_replica._verify`` entry, held to its reason: ``_verify`` is called only from
    ``_txn``, after that function's ``BEGIN IMMEDIATE`` and before its ``COMMIT``, and ``append`` returns a ``Head``
    (``(seq, hash)``), so the snapshot it builds decides the write and reaches no reader."""
    tree = _parsed((LEVAIN / "agents" / "authority_replica.py").read_text(encoding="utf-8"))
    callers = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) == "self._verify"]
    txn = _function(tree, "_txn")
    assert len(callers) == 1 and _within(callers[0], txn)
    sql = {n.args[0].value: n.lineno for n in ast.walk(txn) if isinstance(n, ast.Call) and _dotted(n.func)
           == "conn.execute" and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value in
           ("BEGIN IMMEDIATE", "COMMIT")}
    assert sql["BEGIN IMMEDIATE"] < callers[0].lineno < sql["COMMIT"], sql
    assert ast.unparse(_function(tree, "append").returns) == "Head"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The one function (or method) named ``name`` in ``tree``."""
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _within(node: ast.AST, scope: ast.AST) -> bool:
    return any(n is node for n in ast.walk(scope))


# the statements graduation_policy._replay may execute itself: transaction control, never a write
REPLAY_STATEMENTS = ("BEGIN", "COMMIT", "ROLLBACK")


def _check_the_coverage_replay(path: Path) -> None:
    """SNAPSHOT_BUILDERS' ``graduation_policy._replay`` entry, held to its reason: the module's only
    ``sqlite3.connect`` is in ``_connect`` and opens a ``file:...?mode=ro`` URI with ``uri=True``; ``_replay``
    opens its connection through ``_connect`` and nothing else; every statement it executes itself is a literal
    in :data:`REPLAY_STATEMENTS`."""
    tree = _parsed(path.read_text(encoding="utf-8"))
    connect_fn = _function(tree, "_connect")
    connects = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) == "sqlite3.connect"]
    assert connects and all(_within(c, connect_fn) for c in connects), "a sqlite3.connect outside _connect"
    for call in connects:
        url = call.args[0] if call.args else None
        parts = url.values if isinstance(url, ast.JoinedStr) else [url]
        literal = "".join(v.value for v in parts if isinstance(v, ast.Constant) and isinstance(v.value, str))
        assert literal.startswith("file:") and literal.endswith("?mode=ro") and literal.count("mode=") == 1, literal
        assert isinstance(parts[-1], ast.Constant), "the URI's mode is not its literal tail"
        assert any(k.arg == "uri" and isinstance(k.value, ast.Constant) and k.value.value is True
                   for k in call.keywords), "the URI is not opened as a URI"
    replay = _function(tree, "_replay")
    openers = [_dotted(n.func) for n in ast.walk(replay) if isinstance(n, ast.Call)
               and (_dotted(n.func) or "").split(".")[-1] in ("_connect", "connect", "CockpitDB", "_journal",
                                                                "autocommit", "write")]
    assert openers == ["_connect"], openers
    for n in ast.walk(replay):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("execute", "executemany", "executescript"):
            arg = n.args[0] if n.args else None
            assert n.func.attr == "execute" and isinstance(arg, ast.Constant) and arg.value in REPLAY_STATEMENTS, \
                f"_replay executes {ast.unparse(n)}"


def test_the_coverage_replay_opens_read_only_and_writes_nothing():
    _check_the_coverage_replay(LEVAIN / "graduation_policy.py")


@pytest.mark.parametrize("old, new, why", [
    ("?mode=ro", "?mode=rw", "mode=rw"),
    ('conn.execute("COMMIT")', 'conn.execute("DELETE FROM authority_log")', "_replay executes"),
    ("conn = _connect(self._root / DB_NAME, _REFRESH_BUSY_TIMEOUT)\n",
     "conn = sqlite3.connect(self._root / DB_NAME)\n", "outside _connect"),
])
def test_the_coverage_replay_check_sees_a_planted_write_path(tmp_path, old, new, why):
    # the check is not vacuous: a read-write open, a write statement, or a second connect fails it
    src = (LEVAIN / "graduation_policy.py").read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    planted = tmp_path / "graduation_policy.py"
    planted.write_text(src.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError, match=why):
        _check_the_coverage_replay(planted)


@_by_tree
def _key_setup_callers(tree: ast.AST) -> bool:
    """Whether ``tree`` reaches ``levain.chat_egress.key_setup``'s setup_key or revoke_key: a from-import of
    either, or a call through any alias of the module."""
    acts = ("setup_key", "revoke_key")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "levain.chat_egress.key_setup" \
                and {a.name for a in node.names} & set(acts):
            return True
    return bool(_uses_in(tree, acts, "levain.chat_egress.key_setup"))


# The appenders allowlisted for their per-act shape, with the append calls each makes (_check_per_act_append).
PER_ACT_APPENDERS = {
    ("cockpit/keys.py", "_append"): ("sign_and_append",),
    ("cli.py", "_signed_append"): ("append", "prepare"),
    # agents.signing._sign_op (P5a-2a): prepare and append in two separate autocommit blocks of one journal it opens
    # per act, the signature taken between them OUTSIDE any transaction (_check_the_signing_module holds that)
    ("agents/signing.py", "_sign_op"): ("append", "prepare"),
    # voice.custody.add_key (V1-1): a voice key's create or replacing loosen, prepared and appended inside one
    # autocommit block of a journal it opens per act, like key_setup.setup_key
    ("voice/custody.py", "add_key"): ("append", "prepare"),
    # seat_lease.renew_lease (Z1d): a seat-root lease's renew, prepared and appended in two separate autocommit blocks of a
    # journal it opens per act, the signature taken between them outside any transaction, the shape of cli._signed_append
    ("seat_lease.py", "renew_lease"): ("append", "prepare"),
}


def _check_per_act_append(fn: ast.FunctionDef | ast.AsyncFunctionDef, names: tuple[str, ...]) -> None:
    """``fn`` appends per act: every call of ``names`` in it passes, as its first argument, the connection an
    enclosing ``with <db>.autocommit() as <conn>:`` opened, where ``<db>`` is a parameter of ``fn`` or a local
    ``fn`` assigns only from ``_journal(...)``/``CockpitDB(...)``; and ``fn`` declares no global or nonlocal."""
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal))], f"{fn.name}: global state"
    params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
    built: dict[str, list[ast.AST]] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    built.setdefault(t.id, []).append(n.value)
    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id in names)
                  or (isinstance(n.func, ast.Attribute) and n.func.attr in names))]
    assert calls, f"{fn.name}: no {names} call"
    for call in calls:
        first = call.args[0] if call.args else None
        node, holder = call, None
        while node in parents and holder is None:
            node = parents[node]
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    ctx, var = item.context_expr, item.optional_vars
                    if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) \
                            and ctx.func.attr == "autocommit" and isinstance(ctx.func.value, ast.Name) \
                            and isinstance(var, ast.Name) and isinstance(first, ast.Name) and first.id == var.id:
                        holder = ctx.func.value.id
        assert holder is not None, f"{fn.name}:{call.lineno} {ast.unparse(call.func)} outside its own autocommit()"
        sources = built.get(holder, [])
        assert holder in params and not sources or holder not in params and sources and all(
            isinstance(v, ast.Call) and (_dotted(v.func) or "").split(".")[-1] in ("_journal", "CockpitDB")
            for v in sources), f"{fn.name}: {holder} is not a journal this act opened or was handed"


# The appenders that are HANDED their cockpit connection by a caller and open none: (file, function, the parameter).
HANDED_APPENDERS = {
    ("agents/bars.py", "_r2"): "cockpit",
}


# The revoke steps of agent_authority.py: handed the cockpit connection by their caller, they open none. (file, function):
# (the parameter, the levain.authority acts the function may make). The caller side is the revoke verbs' (P5c-2c).
HANDED_STEPS = {
    ("agent_authority.py", "_prepare_onto"): ("cockpit", ("prepare",)),
    ("agent_authority.py", "revoke_agent_finish"): ("cockpit", ("append",)),
}


def _check_handed_step(fn: ast.FunctionDef, param: str, acts: tuple[str, ...]) -> None:
    """``fn`` makes each of ``acts`` through ``param`` alone: the bare parameter is the first argument of every
    ``_auth.<act>`` call, ``fn`` never rebinds it, builds no connection and declares no global state."""
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal))], f"{fn.name}: global state"
    assert param in {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}, f"{fn.name}: no {param}"
    assert _rebinds(fn, param) == [], f"{fn.name}: {param} is rebound"
    for act in acts:
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _dotted(n.func) == f"_auth.{act}"]
        assert calls, f"{fn.name}: no _auth.{act} call"
        for call in calls:
            first = call.args[0] if call.args else None
            assert isinstance(first, ast.Name) and first.id == param, \
                f"{fn.name}:{call.lineno} {act} off the handed {param}"
    builders = ("_journal", "CockpitDB", "autocommit", "connect", "Connection")
    built = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and _dotted(n.func) is not None
             and _dotted(n.func).split(".")[-1] in builders]
    assert built == [], f"{fn.name}: builds a connection itself at {built}"


def test_the_handed_step_guard_sees_a_step_off_its_handed_connection(tmp_path):
    # the guard above is not vacuous: an append off another connection, a rebound parameter, or a connection the
    # step builds fails it
    root = tmp_path / "levain"
    shutil.copytree(LEVAIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    path = root / "agent_authority.py"
    src = path.read_text(encoding="utf-8")
    for old, new, why in (
            ("_auth.append(cockpit, link,", "_auth.append(autonomic, link,", "append off the handed cockpit"),
            ("    from levain.agents import bars\n\n    bar = bars.read_bar(autonomic, bar_id)\n    if bar is None or bar.closed_at is not None:\n        return RevokeStep(\"abandoned\", bar_id, None, _ABANDONED)\n    link = _unsigned(bar)\n    if link is None or bar.pending_signature is None:",
             "    from levain.agents import bars\n\n    cockpit = autonomic\n    bar = bars.read_bar(autonomic, bar_id)\n    link = _unsigned(bar)\n    if link is None or bar.pending_signature is None:",
             "cockpit is rebound"),
            ("    kw = {} if ssh_keygen is None else {\"ssh_keygen\": ssh_keygen}\n    try:\n        try:\n            new = _auth.append(",
             "    cockpit = sqlite3.connect(':memory:')\n    try:\n        try:\n            new = _auth.append(",
             "cockpit is rebound")):
        assert src.count(old) == 1, old
        path.write_text(src.replace(old, new), encoding="utf-8")
        with pytest.raises(AssertionError, match=why):
            _check_the_appenders(root)
    path.write_text(src, encoding="utf-8")
    _check_the_appenders(root)


def _rebinds(fn: ast.AST, param: str) -> list[int]:
    """Every way ``fn`` (nested scopes included) can bind or unbind ``param`` after its parameter list."""
    hits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == param
            and isinstance(n.ctx, (ast.Store, ast.Del))]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler) and n.name == param]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, ast.alias) and (n.asname or n.name) == param]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and n is not fn and n.name == param]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == param]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, ast.MatchMapping) and n.rest == param]
    hits += [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal)) and param in n.names]
    return hits


def _check_handed_append(tree: ast.Module, name: str, param: str) -> None:
    """``name`` appends through ``param`` alone: every append call passes that bare parameter as its first
    argument, ``name`` neither rebinds it nor declares global state, the module builds no journal of its own at
    any depth (so it cannot hold one), and ``name`` is referenced only by one direct call inside ``recover``, which
    passes ``recover``'s own, never rebound ``cockpit`` in exactly the slot ``name`` binds ``param`` to, so the
    connection is the caller's per-act one and not the module's."""
    fn = _function(tree, name)
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal))], f"{name}: global state"
    positional = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    assert param in {*positional, *(a.arg for a in fn.args.kwonlyargs)}, f"{name}: no {param}"
    assert _rebinds(fn, param) == [], f"{name}: {param} is rebound"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "append" and _dotted(n.func) == "_auth.append"]
    assert calls, f"{name}: no _auth.append call"
    for call in calls:
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name) and first.id == param, f"{name}:{call.lineno} append off the handed {param}"
    builders = ("_journal", "CockpitDB", "autocommit", "connect", "Connection")
    built = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) is not None
             and _dotted(n.func).split(".")[-1] in builders]
    built += [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for a in n.names if a.name in builders]
    built += [n.lineno for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Load)
              and (n.id if isinstance(n, ast.Name) else n.attr) in builders[:-1]]   # a Connection annotation is no opener
    assert built == [], f"{name}: the module builds a connection itself at {sorted(set(built))}"
    recover = _function(tree, "recover")
    assert param in {a.arg for a in (*recover.args.args, *recover.args.kwonlyargs)}, f"{name}: recover has no {param}"
    assert _rebinds(recover, param) == [], f"{name}: recover rebinds its {param}"
    # every mention of the appender, in any context, is the func of one direct call inside recover
    direct = [c for c in ast.walk(recover) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == name]
    mentions = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == name
                or isinstance(n, ast.Attribute) and n.attr == name
                or isinstance(n, ast.alias) and name in (n.name, n.asname)]
    assert len(direct) == 1 and len(mentions) == 1 and mentions[0] is direct[0].func, \
        f"{name}: not called exactly once, directly, from recover ({len(direct)} direct, {len(mentions)} mentions)"
    call = direct[0]
    assert not any(isinstance(a, ast.Starred) for a in call.args) and all(k.arg is not None for k in call.keywords), \
        f"{name}: recover's call splats its arguments"
    at = positional.index(param) if param in positional else None
    keyed = [k.value for k in call.keywords if k.arg == param]
    slot = ([call.args[at]] if at is not None and at < len(call.args) else []) + keyed
    assert len(slot) == 1 and isinstance(slot[0], ast.Name) and slot[0].id == param, \
        f"{name}: not recover's {param} in the {param} slot"


def _check_the_signing_module(tree: ast.Module) -> None:
    """agents/signing.py is a NAMED appender: only ``_sign_op`` prepares or appends, only ``sign_agent_link`` and
    ``renew_agent_link`` call it (one direct call each), and ``_sign_op`` takes the signature (``sign(...)``) outside
    every ``with ...autocommit()`` block, so a touch never holds a transaction."""
    op = _function(tree, "_sign_op")
    calls = [n for n in ast.walk(op) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "sign"]
    assert len(calls) == 1, "_sign_op: not exactly one sign() call"
    inside = [n for w in ast.walk(op) if isinstance(w, ast.With) for n in ast.walk(w) if n is calls[0]]
    assert inside == [], "_sign_op: sign() inside a with block (a transaction held across the touch)"
    callers = sorted(f.name for f in tree.body if isinstance(f, ast.FunctionDef) and f.name != "_sign_op"
                     for n in ast.walk(f) if isinstance(n, ast.Name) and n.id == "_sign_op")
    assert callers == ["renew_agent_link", "sign_agent_link"], f"_sign_op callers: {callers}"


def _check_the_appenders(root: Path) -> None:
    """The S3a-2 MUST (a) guard over the package at ``root`` (the installed one, or a planted copy)."""
    appenders = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "authority.py":
            continue
        for name, fn, _line in _uses(path, ("append", "sign_and_append", "prepare")):
            appenders.append((str(path.relative_to(root)), fn, name))
    # cli._signed_append (append + prepare): the `levain binding policy create|renew|revoke` verbs, a short-lived CLI
    # act that opens its journal per act, the same shape as keys._append; PER_ACT_APPENDERS holds both to it below.
    # agents.bars._r2 appends a bar's stored signature through the cockpit connection recover hands it; it opens none
    # and is held to that by HANDED_APPENDERS below.
    # agent_authority._prepare_onto and revoke_agent_finish prepare and append the agent revoke through the cockpit
    # connection their caller hands them (HANDED_STEPS below); they open none.
    # agents.signing._sign_op signs and appends an agent link: sign_agent_link and renew_agent_link are its only
    # callers, it signs outside any transaction, and PER_ACT_APPENDERS holds its connections to the per-act shape.
    assert sorted(appenders) == [("agent_authority.py", "_prepare_onto", "prepare"),
                                 ("agent_authority.py", "revoke_agent_finish", "append"),
                                 ("agents/bars.py", "_r2", "append"),
                                 ("agents/signing.py", "_sign_op", "append"),
                                 ("agents/signing.py", "_sign_op", "prepare"),
                                 ("chat_egress/key_setup.py", "setup_key", "append"),
                                 ("chat_egress/key_setup.py", "setup_key", "prepare"),
                                 ("cli.py", "_signed_append", "append"),
                                 ("cli.py", "_signed_append", "prepare"),
                                 ("cockpit/keys.py", "_append", "sign_and_append"),
                                 ("consent/grants.py", "append_revoke", "append"),
                                 ("consent/grants.py", "prepare_revoke", "prepare"),
                                 ("seat_lease.py", "renew_lease", "append"),
                                 ("seat_lease.py", "renew_lease", "prepare"),
                                 ("voice/custody.py", "add_key", "append"),
                                 ("voice/custody.py", "add_key", "prepare")]
    for (rel, name), calls in PER_ACT_APPENDERS.items():
        _check_per_act_append(_function(_parsed((root / rel).read_text(encoding="utf-8")), name), calls)
    _check_the_signing_module(_parsed((root / "agents" / "signing.py").read_text(encoding="utf-8")))
    for (rel, name), (param, acts) in HANDED_STEPS.items():
        _check_handed_step(_function(_parsed((root / rel).read_text(encoding="utf-8")), name), param, acts)
    for (rel, name), param in HANDED_APPENDERS.items():
        _check_handed_append(_parsed((root / rel).read_text(encoding="utf-8")), name, param)
    callers = [str(path.relative_to(root)) for path in sorted(root.rglob("*.py"))
               if _key_act_callers(_parsed(path.read_text(encoding="utf-8")))]
    assert callers == ["cli.py"]
    # the egress key acts are reached only from the CLI's commands, like the cockpit key acts
    setup_callers = [str(path.relative_to(root)) for path in sorted(root.rglob("*.py"))
                     if _key_setup_callers(_parsed(path.read_text(encoding="utf-8")))]
    assert setup_callers == ["cli.py"]

    holders = ("_journal", "CockpitDB")

    def builds_journal(node: ast.AST) -> bool:
        return any(isinstance(n, ast.Call) and _dotted(n.func) is not None
                   and _dotted(n.func).split(".")[-1] in holders for n in ast.walk(node))
    # append_revoke's only caller in the package is the phone grant surface
    revoke_callers = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py") if p != root / "consent" / "grants.py"
        and any(isinstance(n, ast.Name) and n.id == "append_revoke"
                or isinstance(n, ast.Attribute) and n.attr == "append_revoke"
                or isinstance(n, ast.alias) and n.name == "append_revoke"
                for n in ast.walk(_parsed(p.read_text(encoding="utf-8")))))
    assert revoke_callers == ["consent/grant_phone.py"]
    # the journal is built inside append_revoke's body, and grants.py builds none at module level
    grants = _parsed((root / "consent" / "grants.py").read_text(encoding="utf-8"))
    fn = next(n for n in grants.body if isinstance(n, ast.FunctionDef) and n.name == "append_revoke")
    assert any(isinstance(n, ast.Call) and _dotted(n.func) == "_journal" for n in ast.walk(fn))
    module_level = [n for n in grants.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and builds_journal(n)]
    assert module_level == []
    # key_setup builds its journal inside each act's body, and none at module level
    setup = _parsed((root / "chat_egress" / "key_setup.py").read_text(encoding="utf-8"))
    for act in ("setup_key", "revoke_key"):
        fn = next(n for n in setup.body if isinstance(n, ast.FunctionDef) and n.name == act)
        assert any(isinstance(n, ast.Call) and _dotted(n.func) == "_journal" for n in ast.walk(fn)), act
    assert [n for n in setup.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and builds_journal(n)] == []
    # and the phone surface keeps no journal on itself between requests
    phone = _parsed((root / "consent" / "grant_phone.py").read_text(encoding="utf-8"))
    kept = [n.lineno for n in ast.walk(phone) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and builds_journal(n.value if n.value is not None else n)
            and any(isinstance(t, ast.Attribute) for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    assert kept == []


def test_every_appender_is_the_short_lived_cli_path():
    """S3a-2 MUST (a): a long-lived process may append only by opening the journal by path for that one
    act, holding nothing across calls; any other long-lived appender goes through the broker or holds
    cockpit.lock (an anchor reset refuses only while a live broker holds it). The appenders are the key
    acts in levain.cockpit.keys and the egress key acts in levain.chat_egress.key_setup (callers of each:
    the CLI's commands; key_setup opens its journal per act), and the phone revoke, which builds its
    journal inside the function and whose one caller keeps no journal."""
    _check_the_appenders(LEVAIN)


def test_the_appender_guard_sees_a_planted_second_caller_of_setup_key(tmp_path):
    # the guard is not vacuous: a second caller of key_setup's acts (a long-lived surface, say) fails it
    root = tmp_path / "levain"
    shutil.copytree(LEVAIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    _check_the_appenders(root)
    for i, source in enumerate((
            "from levain.chat_egress.key_setup import setup_key\ndef serve(r):\n    return setup_key(r)\n",
            "from levain.chat_egress import key_setup as ks\ndef serve(r):\n    return ks.revoke_key(r, 'x')\n")):
        planted = root / "cockpit" / f"planted_{i}.py"
        planted.write_text(source, encoding="utf-8")
        with pytest.raises(AssertionError, match=f"planted_{i}"):
            _check_the_appenders(root)
        planted.unlink()


@pytest.mark.parametrize("rel, old, new, why", [
    ("cli.py", "            return append(conn, link,", "            return append(HELD, link,", "_signed_append:.* outside"),
    ("cli.py", "    db = _journal(_cockpit_root(args))\n", "    db = _JOURNAL\n", "_signed_append: db is not a journal"),
    ("cockpit/keys.py", "        with db.autocommit() as conn:\n            return sign_and_append(",
     "        with _HELD as conn:\n            return sign_and_append(", "_append:.* outside"),
])
def test_the_appender_guard_sees_an_append_off_its_per_act_connection(tmp_path, rel, old, new, why):
    # the per-act shape is not vacuous: another connection, a held journal, or a with that is not autocommit fails it
    root = tmp_path / "levain"
    shutil.copytree(LEVAIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    src = (root / rel).read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    (root / rel).write_text(src.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError, match=why):
        _check_the_appenders(root)


def test_the_appender_guard_sees_a_second_appender_in_signing_or_a_new_module(tmp_path):
    # the signing entry is a NAMED allowance, not a general one: another function in signing.py that appends, or a
    # new module that does, fails the inventory; a second caller of _sign_op or a sign() inside a with fails its scope
    root = tmp_path / "levain"
    shutil.copytree(LEVAIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    _check_the_appenders(root)
    signing = root / "agents" / "signing.py"
    src = signing.read_text(encoding="utf-8")
    signing.write_text(src + "\n\ndef sneaky(conn, link, sig, now):\n    return append(conn, link, sig, now=now)\n",
                       encoding="utf-8")
    with pytest.raises(AssertionError, match="sneaky"):
        _check_the_appenders(root)
    signing.write_text(src + "\n\ndef sneaky(db, op):\n    return _sign_op(db, op)\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="_sign_op callers"):
        _check_the_appenders(root)
    old = "        signature = sign(link.message)\n"
    assert src.count(old) == 1
    signing.write_text(src.replace(old, "        with db.autocommit():\n            signature = sign(link.message)\n"),
                       encoding="utf-8")
    with pytest.raises(AssertionError, match="sign\\(\\) inside a with"):
        _check_the_appenders(root)
    signing.write_text(src, encoding="utf-8")
    planted = root / "agents" / "planted_agent.py"
    planted.write_text("from levain.authority import append\ndef go(conn, link, sig, now):\n"
                       "    return append(conn, link, sig, now=now)\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="planted_agent"):
        _check_the_appenders(root)


@pytest.mark.parametrize("old, new, why", [
    ("_auth.append(cockpit, link,", "_auth.append(_HELD, link,", "_r2:.* append off the handed cockpit"),
    ("    link = _auth.UnsignedLink(seq, prev, rec, fp)\n", "    link = _auth.UnsignedLink(seq, prev, rec, fp)\n    cockpit = conn\n",
     "_r2: cockpit is rebound"),
    ("    pending = _pending(bar)\n    if pending is None or bar.pending_signature",
     "    pending = _pending(bar)\n    _HELD = CockpitDB('x')\n    if pending is None or bar.pending_signature",
     "the module builds a connection itself"),
    ("or _r2(conn, cockpit, state,", "or _r2(conn, _HELD, state,", "_r2: not recover's cockpit"),
    # the slot the handed argument sits in, not just its presence among the arguments
    ("or _r2(conn, cockpit, state,", "or _r2(cockpit, conn, state,", "_r2: not recover's cockpit in the cockpit slot"),
    ("or _r2(conn, cockpit, state, bar, now=now, anchor=anchor,", "or _r2(conn, conn, state, bar, now=now, anchor=cockpit,",
     "_r2: not recover's cockpit in the cockpit slot"),
    ("or _r2(conn, cockpit, state, bar,", "or _r2(*(conn, cockpit, state), bar,", "_r2: recover's call splats"),
    # recover's own cockpit stays the one it was handed
    ("        open_bars = [_bar(r)", "        cockpit = conn\n        open_bars = [_bar(r)", "_r2: recover rebinds its cockpit"),
    ("    link = _auth.UnsignedLink(seq, prev, rec, fp)\n",
     "    link = _auth.UnsignedLink(seq, prev, rec, fp)\n    try:\n        pass\n    except Exception as cockpit:\n        pass\n",
     "_r2: cockpit is rebound"),
    ("    link = _auth.UnsignedLink(seq, prev, rec, fp)\n", "    link = _auth.UnsignedLink(seq, prev, rec, fp)\n    del cockpit\n",
     "_r2: cockpit is rebound"),
    # no other reference to the appender: a module-level call, an alias, a callback
    ("def recover(conn: sqlite3.Connection, authority",
     "_r2(None, None, None, None, now=None, anchor=None, ssh_keygen=None)\n\n\ndef recover(conn: sqlite3.Connection, authority",
     "_r2: not called exactly once, directly, from recover"),
    ("def recover(conn: sqlite3.Connection, authority", "_ALIAS = _r2\n\n\ndef recover(conn: sqlite3.Connection, authority",
     "_r2: not called exactly once, directly, from recover"),
    # an opener the module reaches under another name
    ("from levain.agent_authority import AgentAuthority\n",
     "from levain.agent_authority import AgentAuthority\nfrom levain.cockpit.db import CockpitDB as DB\n",
     "the module builds a connection itself"),
    ("from levain.agent_authority import AgentAuthority\n",
     "from levain.agent_authority import AgentAuthority\nfrom sqlite3 import connect as open_it\n",
     "the module builds a connection itself"),
    ("from levain.agent_authority import AgentAuthority\n",
     "from levain.agent_authority import AgentAuthority\nimport sqlite3 as _s\n_OPEN = _s.connect\n",
     "the module builds a connection itself"),
])
def test_the_handed_append_guard_sees_an_append_off_the_handed_connection(tmp_path, old, new, why):
    # the handed-connection shape is not vacuous: another connection, a rebound parameter, a journal the module
    # builds, or a caller that passes something other than recover's own cockpit fails it
    root = tmp_path / "levain"
    shutil.copytree(LEVAIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    src = (root / "agents" / "bars.py").read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    (root / "agents" / "bars.py").write_text(src.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError, match=why):
        _check_the_appenders(root)


def test_the_fences_see_a_nested_module_call_and_a_module_import_of_keys():
    # each source is one the first scan (an asname-only module alias, a from-import of the act) missed
    nested = ast.parse("import levain.authority\ndef reader(conn):\n    return levain.authority.snapshot(conn)\n")
    assert _uses_in(nested, ("snapshot",), "levain.authority") == [("snapshot", "reader", 3)]
    package = ast.parse("import levain.cockpit\ndef r(c):\n    return levain.authority._replay(c)\n")
    assert _uses_in(package, ("_replay",), "levain.authority") == [("_replay", "r", 3)]
    via_module = ast.parse("from levain.cockpit import keys\ndef act(root):\n    keys.enroll(root, 'k')\n")
    assert _key_act_callers(via_module)
    via_alias = ast.parse("import levain.cockpit.keys as k\nk.revoke('r', 'x')\n")
    assert _key_act_callers(via_alias)
    assert _key_act_callers(ast.parse("from levain.cockpit.keys import ledger_init\n"))
    assert not _key_act_callers(ast.parse("from levain.cockpit import keys\nkeys.keys('r')\n"))


# --- MUST (b): fingerprint_of over an fd, a timeout is VerifierUnavailable ------------------------------

def test_fingerprint_of_reads_the_key_over_an_fd_and_writes_no_file(keydir, monkeypatch):
    import tempfile

    k = RealKey(keydir, "fd")

    def no_files(*a, **kw):
        raise AssertionError("fingerprint_of wrote a file")
    monkeypatch.setattr(tempfile, "mkstemp", no_files)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_files)
    assert fingerprint_of(k.public) == k.fingerprint


def test_fingerprint_of_a_hung_verifier_is_unavailable_not_a_key_refusal(home):
    slow = home / "ssh-keygen-slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    with pytest.raises(VerifierUnavailable, match="TimeoutExpired"):
        fingerprint_of("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                       ssh_keygen=slow, timeout=0.5)
    missing = home / "no-such-keygen"
    with pytest.raises(VerifierUnavailable):
        fingerprint_of("ssh-ed25519 AAAA", ssh_keygen=missing)
    with pytest.raises(KeyError_, match="could not read the key"):
        fingerprint_of("ssh-ed25519 AAAA")                              # a refusal stays a refusal


def test_the_root_is_reachable(broker, keydir):
    # the rig's root key is the genesis this store's other keys hang from
    Key(keydir, "phone").enroll(broker, "display-phone")
    assert root_of(broker.db) is not None
