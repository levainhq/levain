"""Trust-tiering S3a part 2a (DESIGN rev 5.2 §3.3, §3.4, §3.7, S3a-2 MUST): the cockpit reads who may
sign only from the verified replay of the signed authority log, captured once per request.

Each refusal here FAILS on the /2 shape (a ``keys`` table and ``meta.policy_rev`` read directly): a
planted key row, a direct meta write and a truncated log grant nothing, a /2 store migrates and refuses
every signed act until ``ledger init``, and no code builds a snapshot outside ``RequestAuthority.capture``
and ``levain.authority.append``.
"""
from __future__ import annotations

import ast
import json
import os
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
from tests.cockpit_authority_rig import RealKey, enrol, enrol_any, revoke, revoke_any, root_of, widen_laptop_floor
from tests.test_cockpit_k2b_broker import (  # noqa: F401 - fixtures
    BROWSER,
    Key,
    _broker,
    _laptop_floor,
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
    assert broker.capture().fence == {"authority_store_id": broker.capture().authority.store_id,
                                      "authority_seq": 2}
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
        phone = Key(keydir, "phone").enroll(b, "display-phone")
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
        assert phone.fingerprint  # the phone's key is not in any snapshot this store serves
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
    widen_laptop_floor(monkeypatch)
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
    widen_laptop_floor(monkeypatch)
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
    key = LabelKey("spore:k.seed", {"disposition": "note"})
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
    other = Key(keydir, "other").enroll(broker, "presence")            # the head moves past the fire
    assert other.fingerprint
    revoke_any(broker.db, phone, compromised_since=broker.capture().authority.seq)
    assert broker.capture().labels.label(LabelKey("spore:k.seed", {"disposition": "note"})) == "signed · reported"


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


# --- structural fences ----------------------------------------------------------------------------

def _calls(tree: ast.AST, module_aliases: set[str], name_aliases: set[str], attr: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in name_aliases:
                out.append(node)
            elif isinstance(f, ast.Attribute) and f.attr == attr and isinstance(f.value, ast.Name) \
                    and f.value.id in module_aliases:
                out.append(node)
    return out


def _uses(path: Path, names: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """Calls in ``path`` of any of ``names`` from ``levain.authority``, as (name, enclosing function,
    line). A module alias is ``import levain.authority as X`` / ``from levain import authority as X``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "levain.authority" and a.asname:
                    modules.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "levain":
                modules.update(a.asname or a.name for a in node.names if a.name == "authority")
            elif node.module == "levain.authority":
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


def test_no_snapshot_is_built_outside_capture_and_append():
    """DESIGN §3.4: ``RequestAuthority.capture`` is the only snapshot builder (``levain.authority.append``
    replays inside itself), so no reader can fall back to a second head."""
    offenders = []
    for path in sorted(LEVAIN.rglob("*.py")):
        if path == LEVAIN / "authority.py":
            continue
        for name, fn, line in _uses(path, ("snapshot", "_replay", "_replay_entry")):
            if (path, fn) != (LEVAIN / "cockpit" / "authority.py", "capture"):
                offenders.append(f"{path.relative_to(LEVAIN.parent)}:{line} {name}() in {fn}")
    assert offenders == []


def test_the_fence_sees_a_planted_snapshot_call(tmp_path):
    # the scan above is not vacuous: a reader that builds its own snapshot is found
    probe = tmp_path / "probe.py"
    probe.write_text("from levain import authority as a\nfrom levain.authority import snapshot as s\n"
                     "def reader(conn):\n    return a.snapshot(conn), s(conn)\n")
    assert sorted(_uses(probe, ("snapshot",))) == [("snapshot", "reader", 4), ("snapshot", "reader", 4)]


def test_every_appender_is_the_short_lived_cli_path():
    """S3a-2 MUST (a): a long-lived appender would have to go through the broker or hold cockpit.lock
    (an anchor reset refuses only while a live broker holds it). The only appenders are the key acts in
    levain.cockpit.keys, and their only callers are the CLI's commands."""
    appenders = []
    for path in sorted(LEVAIN.rglob("*.py")):
        if path == LEVAIN / "authority.py":
            continue
        for name, fn, _line in _uses(path, ("append", "sign_and_append", "prepare")):
            appenders.append((str(path.relative_to(LEVAIN)), fn, name))
    assert appenders == [("cockpit/keys.py", "_append", "sign_and_append")]
    callers = []
    for path in sorted(LEVAIN.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "levain.cockpit.keys":
                if {a.name for a in node.names} & {"enroll", "revoke"}:
                    callers.append(str(path.relative_to(LEVAIN)))
    assert sorted(set(callers)) == ["cli.py"]


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
    assert os.path.exists(slow)


def test_the_root_is_reachable(broker, keydir):
    # the rig's root key is the genesis this store's other keys hang from
    Key(keydir, "phone").enroll(broker, "display-phone")
    assert root_of(broker.db) is not None
