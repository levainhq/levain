"""Trust-tiering S3a part 2b-1 (DESIGN rev 5 §3.2 genesis, §3.7, §3.9): `levain cockpit ledger init`
and the doctor's one-laptop-key warning.

Each case here pins what the hand run showed (project memory
`1011-trust-tiering/s3a2/run_2b1_init.txt`: a levain-cockpit/2 store made by the v0.9.1 code, opened by
this code, initialised): a fresh store's genesis; a migrated store's confirmed rows re-enrolled (a /2
revoked row enrolled then revoked, a /2 compromised one with ``compromised_since`` at its own enrol), an
ineligible row skipped, an unconfirmed one dropped; the refusals; a run cut off between links leaving a
store that still refuses every signed act and that a re-run finishes.

A laptop-presence key must be a FIDO key, which cannot be made headless: every test widens the class
floor to software ed25519 keys (``widen_laptop_floor``), as the run did.
"""
from __future__ import annotations

import io
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain import cli
from levain.cockpit.firejournal import UNINITIALISED, CockpitDB
from levain.cockpit.keys import KeyError_, enroll, init_plan, keys, ledger_init
from levain.doctor import _check_cockpit_authority
from tests.cockpit_authority_rig import RealKey, widen_laptop_floor
from tests.test_cockpit_authority_s3a2 import _as_v2, _hold_lock

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
T0 = "2026-10-01T00:00:00.000000Z"


@pytest.fixture(autouse=True)
def _laptop_floor(monkeypatch):
    widen_laptop_floor(monkeypatch)


class EcdsaKey:
    """A software ecdsa-p256 key: not eligible for laptop-presence even with the widened floor."""

    def __init__(self, d: Path, name: str) -> None:
        subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(d / name)],
                       check=True, capture_output=True, timeout=30)
        self.public = " ".join((d / f"{name}.pub").read_text().split()[:2])
        self.fingerprint = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(d / f"{name}.pub")],
                                          check=True, capture_output=True, text=True, timeout=30).stdout.split()[1]


def _fresh(tmp: Path) -> Path:
    root = tmp / "inst" / ".levain" / "cockpit"
    root.mkdir(parents=True)
    CockpitDB(root).set_install_class("reported")
    return root


def _row(k, cls: str, at: str, revoked: str | None = None, compromised: str | None = None) -> tuple:
    principal = getattr(k, "principal", "k" + "0" * 32)
    return (k.fingerprint, principal, k.public, cls, at, revoked, compromised)


@pytest.fixture
def v2(tmp_path: Path):
    """A /2 store as the run's: A laptop (the signer), E laptop ecdsa (ineligible), P phone, R phone
    revoked and compromised, X phone (to drop). Opened once here, by this code, so it is migrated."""
    root = _fresh(tmp_path)
    ks = {n: RealKey(tmp_path, n) for n in "APRX"}
    ks["E"] = EcdsaKey(tmp_path, "E")
    _as_v2(root, [
        _row(ks["A"], "laptop-presence", "2026-10-01T00:00:01.000000Z"),
        _row(ks["E"], "laptop-presence", "2026-10-01T00:00:02.000000Z"),
        _row(ks["P"], "display-phone", "2026-10-01T00:00:03.000000Z"),
        _row(ks["R"], "display-phone", "2026-10-01T00:00:04.000000Z", "2026-10-05T00:00:00.000000Z", T0),
        _row(ks["X"], "display-phone", "2026-10-01T00:00:05.000000Z"),
    ])
    keys(root, now=NOW)                           # the first open by this code migrates it
    return root, ks


def _init(root: Path, signer: RealKey, confirmed, *, drop: bool = False, sign=None):
    return ledger_init(root, signer_public_key=signer.public, signer_fp=signer.fingerprint,
                       sign=sign or signer.sign_link, clock=lambda: NOW, confirmed=confirmed, drop_unlisted=drop)


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "cockpit.db")


def _log(root: Path) -> list[tuple]:
    with _db(root) as c:
        return c.execute("SELECT seq, signer_fp, json_extract(record, '$.op'), json_extract(record, '$.fingerprint'), "
                         "json_extract(record, '$.legacy_enrolled_at'), json_extract(record, '$.compromised_since') "
                         "FROM authority_log ORDER BY seq").fetchall()


def _uninitialised(root: Path) -> bool:
    with _db(root) as c:
        marker = c.execute("SELECT 1 FROM meta WHERE key = ?", (UNINITIALISED,)).fetchone() is not None
        legacy = c.execute("SELECT 1 FROM sqlite_master WHERE name = 'legacy_keys'").fetchone() is not None
    assert marker == legacy                       # the last transaction clears both, or neither
    return marker


def _no_tty(monkeypatch, answers: str = "", tty: bool = False) -> None:
    stdin = io.StringIO(answers)
    stdin.isatty = lambda: tty                    # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)


def _cli(*args: str) -> int:
    return cli.main(["cockpit", "ledger", "init", *args])


# --- a fresh store ---------------------------------------------------------------------------------

def test_a_fresh_store_gets_the_genesis_signed_by_itself(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path)
    a = RealKey(tmp_path, "A")
    _no_tty(monkeypatch)
    assert _cli("--signing-key", str(a.path), "--confirm", a.fingerprint, "--data-root", str(root)) == 0
    out = capsys.readouterr().out
    assert "trust on first use" in out and "seq 1: enrol" in out and "head at seq 1" in out
    assert _log(root) == [(1, a.fingerprint, "enrol", a.fingerprint, None, None)]
    rows, note = keys(root, now=NOW)
    assert [(r.fingerprint, r.key_class) for r in rows] == [(a.fingerprint, "laptop-presence")] and note is None


def test_a_fresh_genesis_is_confirmed_too(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path)
    a = RealKey(tmp_path, "A")
    _no_tty(monkeypatch)
    assert _cli("--signing-key", str(a.path), "--drop-unlisted", "--data-root", str(root)) == 1
    assert "the signing key included" in capsys.readouterr().err
    assert _log(root) == []


# --- a migrated store ------------------------------------------------------------------------------

def test_a_migrated_store_re_enrols_the_confirmed_rows(v2, monkeypatch, capsys):
    root, k = v2
    assert _uninitialised(root)
    _no_tty(monkeypatch)
    rc = _cli("--signing-key", str(k["A"].path), "--data-root", str(root),
              *(x for n in "APR" for x in ("--confirm", k[n].fingerprint)), "--drop-unlisted")
    out = capsys.readouterr().out
    assert rc == 0
    assert f"dropped: {k['X'].fingerprint}" in out and f"skipped: {k['E'].fingerprint}" in out
    assert "SKIPPED: key type ecdsa-sha2-nistp256 is not eligible for laptop-presence" in out
    a = k["A"].fingerprint
    assert _log(root) == [
        (1, a, "enrol", a, "2026-10-01T00:00:01.000000Z", None),          # the genesis, signed by itself
        (2, a, "enrol", k["P"].fingerprint, "2026-10-01T00:00:03.000000Z", None),
        (3, a, "enrol", k["R"].fingerprint, "2026-10-01T00:00:04.000000Z", None),
        (4, a, "revoke_key", k["R"].fingerprint, None, 3),                  # compromised under /2
    ]
    assert not _uninitialised(root)
    rows, note = keys(root, now=NOW)
    assert note is None
    assert {r.fingerprint: (r.revoked_seq, r.compromised_since, r.legacy_enrolled_at) for r in rows} == {
        a: (None, None, "2026-10-01T00:00:01.000000Z"),
        k["P"].fingerprint: (None, None, "2026-10-01T00:00:03.000000Z"),
        k["R"].fingerprint: (4, 3, "2026-10-01T00:00:04.000000Z")}


def test_a_revoked_row_without_a_compromise_is_a_plain_revoke(tmp_path):
    root = _fresh(tmp_path)
    a, r = RealKey(tmp_path, "A"), RealKey(tmp_path, "R")
    _as_v2(root, [_row(a, "laptop-presence", T0), _row(r, "laptop-presence", T0, "2026-10-05T00:00:00.000000Z")])
    _init(root, a, {a.fingerprint, r.fingerprint})
    assert [x[2:] for x in _log(root)][-1] == ("revoke_key", r.fingerprint, None, None)


def test_a_new_signing_key_is_the_genesis_beside_the_legacy_rows(v2):
    # a /2 laptop key that its class no longer accepts cannot sign: the operator brings a new one
    root, k = v2
    n = RealKey(root.parent, "new")
    res = _init(root, n, {n.fingerprint, k["P"].fingerprint}, drop=True)
    assert [x[2:4] for x in _log(root)] == [("enrol", n.fingerprint), ("enrol", k["P"].fingerprint)]
    assert _log(root)[0][4] is None                                         # no legacy row, no legacy time
    assert set(res.dropped) == {k["A"].fingerprint, k["R"].fingerprint, k["X"].fingerprint}
    assert not _uninitialised(root)


@pytest.mark.parametrize("case, match", [
    ("neither", "not confirmed: .*--drop-unlisted"),
    ("ineligible", "cannot be re-enrolled: key type ecdsa"),
    ("unknown", "no legacy key SHA256:nope"),
    ("signer", "the signing key .* is not confirmed"),
])
def test_what_init_refuses_appends_nothing(v2, case, match):
    root, k = v2
    base = {k["A"].fingerprint, k["P"].fingerprint, k["R"].fingerprint}
    confirmed, drop = {
        "neither": (base, False),
        "ineligible": (base | {k["E"].fingerprint}, True),
        "unknown": (base | {"SHA256:nope"}, True),
        "signer": (base - {k["A"].fingerprint}, True),
    }[case]
    with pytest.raises(KeyError_, match=match):
        _init(root, k["A"], confirmed, drop=drop)
    assert _log(root) == [] and _uninitialised(root)


def test_a_signing_key_the_legacy_rows_disallow_is_refused(tmp_path):
    root = _fresh(tmp_path)
    a, p = RealKey(tmp_path, "A"), RealKey(tmp_path, "P")
    _as_v2(root, [_row(a, "laptop-presence", T0, "2026-10-05T00:00:00.000000Z"), _row(p, "display-phone", T0)])
    with pytest.raises(KeyError_, match="revoked before the migration"):
        _init(root, a, {a.fingerprint}, drop=True)
    with pytest.raises(KeyError_, match="legacy display-phone key"):
        _init(root, p, {p.fingerprint}, drop=True)
    assert _log(root) == []


def test_on_no_terminal_with_neither_flag_init_refuses_naming_both(v2, monkeypatch, capsys):
    root, k = v2
    _no_tty(monkeypatch)
    assert _cli("--signing-key", str(k["A"].path), "--data-root", str(root)) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "--confirm <fingerprint>" in err and "--drop-unlisted" in err
    assert _log(root) == [] and _uninitialised(root)


def test_at_a_terminal_each_key_is_asked(v2, monkeypatch, capsys):
    root, k = v2
    # asked in enrol order, the signing key first: A (y), P (y), R (n), X (y); E is never asked
    _no_tty(monkeypatch, "y\ny\nn\ny\n", tty=True)
    assert _cli("--signing-key", str(k["A"].path), "--data-root", str(root)) == 0
    out = capsys.readouterr().out
    assert out.count("? [y/N]") == 4 and k["E"].fingerprint + "?" not in out
    assert [x[3] for x in _log(root)] == [k[n].fingerprint for n in "APX"]
    assert f"dropped: {k['R'].fingerprint}" in out


def test_init_twice_is_refused(v2, tmp_path):
    root, k = v2
    _init(root, k["A"], {k["A"].fingerprint}, drop=True)
    with pytest.raises(KeyError_, match="already initialised"):
        _init(root, k["A"], {k["A"].fingerprint}, drop=True)
    with pytest.raises(KeyError_, match="already initialised"):
        init_plan(root, now=NOW)
    fresh = _fresh(tmp_path / "f")
    a = RealKey(tmp_path, "fa")
    _init(fresh, a, {a.fingerprint})
    with pytest.raises(KeyError_, match="already initialised"):
        _init(fresh, a, {a.fingerprint})


def test_a_signed_enrol_refuses_before_init_naming_it_and_lands_after(v2):
    root, k = v2
    b = RealKey(root.parent, "B")
    by = {"signer_fp": k["A"].fingerprint, "sign": k["A"].sign_link, "clock": lambda: NOW}
    with pytest.raises(KeyError_, match="levain cockpit ledger init"):
        enroll(root, b.public, "laptop-presence", **by)
    _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    row, created, _ = enroll(root, b.public, "laptop-presence", **by)
    assert created and row.enrolled_seq == 3


# --- a run cut off between links -------------------------------------------------------------------

def test_a_run_cut_off_between_links_still_refuses_and_a_rerun_finishes_it(v2):
    root, k = v2
    calls = []

    def cut_after_two(message: bytes) -> str:
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt                     # the operator walks away mid-touch
        return k["A"].sign_link(message)

    confirmed = {k["A"].fingerprint, k["P"].fingerprint, k["R"].fingerprint, k["X"].fingerprint}
    with pytest.raises(KeyboardInterrupt):
        _init(root, k["A"], confirmed, sign=cut_after_two)
    assert [x[3] for x in _log(root)] == [k["A"].fingerprint, k["P"].fingerprint]
    assert _uninitialised(root)
    # an interrupted store refuses every signed act, naming init
    with pytest.raises(KeyError_, match="ledger init"):
        enroll(root, RealKey(root.parent, "B").public, "display-phone",
               signer_fp=k["A"].fingerprint, sign=k["A"].sign_link, clock=lambda: NOW)
    plan = init_plan(root, now=NOW)
    assert plan.seq == 2 and plan.genesis == k["A"].fingerprint
    assert {r.fingerprint for r in plan.rows if r.in_log is not None} == {k["A"].fingerprint, k["P"].fingerprint}
    # the re-run drops P, which the first run had enrolled: it is revoked, never left standing, compromised
    # from its own enrol so its seq-less /2-era fires read REVOKED, not "signed · reported" (F1)
    res = _init(root, k["A"], confirmed - {k["P"].fingerprint}, drop=True)
    assert _log(root)[2][2:] == ("revoke_key", k["P"].fingerprint, None, 2)
    assert [x[2:4] for x in _log(root)] == [
        ("enrol", k["A"].fingerprint), ("enrol", k["P"].fingerprint), ("revoke_key", k["P"].fingerprint),
        ("enrol", k["R"].fingerprint), ("revoke_key", k["R"].fingerprint), ("enrol", k["X"].fingerprint)]
    assert res.dropped == (k["P"].fingerprint,) and not _uninitialised(root)


def test_a_rerun_cannot_keep_a_key_the_cut_off_run_revoked(v2):
    root, k = v2
    calls = []

    def cut_at(n: int):
        calls.clear()

        def sign(message: bytes) -> str:
            calls.append(1)
            if len(calls) == n:
                raise KeyboardInterrupt
            return k["A"].sign_link(message)
        return sign

    a, p, x = k["A"].fingerprint, k["P"].fingerprint, k["X"].fingerprint
    with pytest.raises(KeyboardInterrupt):
        _init(root, k["A"], {a, p, x}, sign=cut_at(3), drop=True)       # A, P, then X's enrol is cut
    with pytest.raises(KeyboardInterrupt):
        _init(root, k["A"], {a, x}, sign=cut_at(2), drop=True)          # P revoked, then X's enrol is cut
    assert [x_[2:4] for x_ in _log(root)] == [("enrol", a), ("enrol", p), ("revoke_key", p)]
    with pytest.raises(KeyError_, match="revoked by an earlier, interrupted init"):
        _init(root, k["A"], {a, p, x}, drop=True)
    assert _uninitialised(root)
    _init(root, k["A"], {a, x}, drop=True)
    assert not _uninitialised(root)


def test_a_link_landing_before_the_last_transaction_leaves_the_store_uninitialised(v2, monkeypatch):
    # e.g. a second `ledger init` racing this one: the last transaction finds the head moved and refuses
    from contextlib import contextmanager

    from tests.cockpit_authority_rig import enrol as rig_enrol
    root, k = v2
    real_write = CockpitDB.write

    @contextmanager
    def racing_write(self):
        rig_enrol(self, k["X"], "display-phone", by=k["A"], now=NOW)
        with real_write(self) as conn:
            yield conn
    monkeypatch.setattr(CockpitDB, "write", racing_write)
    with pytest.raises(KeyError_, match="moved past seq 2 while init ran"):
        _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    assert _uninitialised(root) and len(_log(root)) == 3


# --- refusals at the CLI are one line, never a traceback -------------------------------------------

def test_a_v2_store_under_a_live_server_is_a_one_line_refusal(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path)
    a = RealKey(tmp_path, "A")
    _as_v2(root, [_row(a, "laptop-presence", T0)])
    holder = _hold_lock(root)
    try:
        _no_tty(monkeypatch)
        assert _cli("--signing-key", str(a.path), "--confirm", a.fingerprint, "--data-root", str(root)) == 1
    finally:
        holder.kill()
        holder.wait()
    err = capsys.readouterr().err
    assert err.startswith("ledger init refused:") and "levain serve" in err and "Traceback" not in err


def test_an_unwritable_data_root_is_a_one_line_refusal(tmp_path, monkeypatch, capsys):
    root = _fresh(tmp_path)
    a = RealKey(tmp_path, "A")
    _as_v2(root, [_row(a, "laptop-presence", T0)])
    (root / "cockpit.lock").unlink(missing_ok=True)
    root.chmod(0o500)                               # cockpit.lock cannot be created: an OSError
    try:
        _no_tty(monkeypatch)
        assert _cli("--signing-key", str(a.path), "--confirm", a.fingerprint, "--data-root", str(root)) == 1
    finally:
        root.chmod(0o700)
    err = capsys.readouterr().err
    assert err.startswith("ledger init refused:") and err.count("\n") == 1 and "Traceback" not in err


# --- doctor (§3.9) ---------------------------------------------------------------------------------

def test_doctor_warns_at_exactly_one_laptop_presence_key(tmp_path):
    root = _fresh(tmp_path)
    install = tmp_path / "inst"
    [r] = _check_cockpit_authority(install)
    assert r.ok and not r.warn and "ledger init" in r.detail                   # 0 keys: the init message
    a = RealKey(tmp_path, "A")
    _init(root, a, {a.fingerprint})
    [r] = _check_cockpit_authority(install)
    assert r.ok and r.warn and a.fingerprint in r.detail
    assert "levain cockpit enroll" in r.hint and "not in this build" in r.hint
    p = RealKey(tmp_path, "P")
    enroll(root, p.public, "display-phone", signer_fp=a.fingerprint, sign=a.sign_link, clock=lambda: NOW)
    [r] = _check_cockpit_authority(install)
    assert r.warn                                   # a phone key is not a second laptop-presence key
    b = RealKey(tmp_path, "B")
    enroll(root, b.public, "laptop-presence", signer_fp=a.fingerprint, sign=a.sign_link, clock=lambda: NOW)
    [r] = _check_cockpit_authority(install)
    assert r.ok and not r.warn and r.detail.startswith("2 laptop-presence keys")


def test_doctor_reads_a_migrated_store_without_warning_and_writes_nothing(v2, tmp_path):
    root, _ = v2
    before = (root / "cockpit.db").read_bytes()
    [r] = _check_cockpit_authority(root.parent.parent)
    assert r.ok and not r.warn and "ledger init" in r.detail
    assert (root / "cockpit.db").read_bytes() == before
    assert _check_cockpit_authority(tmp_path / "nothing-here") == []


def test_doctor_leaves_a_v2_journal_unmigrated(tmp_path):
    root = _fresh(tmp_path)
    _as_v2(root, [])
    [r] = _check_cockpit_authority(tmp_path / "inst")
    assert r.ok and "levain-cockpit/2" in r.detail
    with _db(root) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-cockpit/2"


# --- the 2b-1 fix round (L1 + L2 findings, s3a2/L12_2b1/FINDINGS.md) ---------------------------------

def _cut_at(n: int, key: RealKey):
    """A signer that signs with ``key`` and is cut off (KeyboardInterrupt) at its ``n``-th link."""
    calls: list[int] = []

    def sign(message: bytes) -> str:
        calls.append(1)
        if len(calls) == n:
            raise KeyboardInterrupt
        return key.sign_link(message)
    return sign


def test_f1_a_dropped_key_the_cut_off_run_enrolled_is_revoked_compromised_at_its_enrol(v2):
    # run 1 enrols the /2-compromised R and is cut before R's revoke link; run 2 drops R. R's /2-era fires
    # carry no authority seq, so only a compromised_since reads them REVOKED (O3), never "signed · reported"
    root, k = v2
    a, p, r, x = (k[n].fingerprint for n in "APRX")
    with pytest.raises(KeyboardInterrupt):
        _init(root, k["A"], {a, p, r, x}, sign=_cut_at(4, k["A"]))       # A, P, R enrolled; R's revoke cut
    assert [x_[2:4] for x_ in _log(root)] == [("enrol", a), ("enrol", p), ("enrol", r)]
    res = _init(root, k["A"], {a, p, x}, drop=True)
    assert _log(root)[3][2:] == ("revoke_key", r, None, 3)                # compromised_since = R's enrolled_seq
    assert res.dropped == (r,) and not _uninitialised(root)
    rows, _ = keys(root, now=NOW)
    assert {row.fingerprint: row.compromised_since for row in rows}[r] == 3


def _foreign_genesis(tmp_path: Path):
    """N (no legacy row) wrote the genesis and enrolled legacy A, then stopped before legacy P's enrol."""
    root = _fresh(tmp_path)
    a, p, n = RealKey(tmp_path, "A"), RealKey(tmp_path, "P"), RealKey(tmp_path, "N")
    _as_v2(root, [_row(a, "laptop-presence", "2026-10-01T00:00:01.000000Z"),
                  _row(p, "display-phone", "2026-10-01T00:00:02.000000Z")])
    with pytest.raises(KeyboardInterrupt):
        _init(root, n, {n.fingerprint, a.fingerprint, p.fingerprint}, sign=_cut_at(3, n))
    assert [x[3] for x in _log(root)] == [n.fingerprint, a.fingerprint]
    return root, a, p, n


def test_f2_a_resume_that_does_not_confirm_the_log_genesis_refuses_naming_it(tmp_path):
    root, a, p, n = _foreign_genesis(tmp_path)
    with pytest.raises(KeyError_, match=f"genesis {re.escape(n.fingerprint)}.*not confirmed"):
        _init(root, a, {a.fingerprint, p.fingerprint}, drop=True)
    assert len(_log(root)) == 2 and _uninitialised(root)
    # confirmed, it stays (the operator saw it listed and kept it)
    _init(root, a, {a.fingerprint, p.fingerprint, n.fingerprint}, drop=True)
    assert not _uninitialised(root)
    rows, _ = keys(root, now=NOW)
    assert {r.fingerprint for r in rows if r.revoked_seq is None} == {n.fingerprint, a.fingerprint, p.fingerprint}


def test_f2_a_resume_lists_every_log_key_genesis_first(tmp_path, monkeypatch, capsys):
    root, a, p, n = _foreign_genesis(tmp_path)
    _no_tty(monkeypatch)
    assert _cli("--signing-key", str(a.path), "--confirm", a.fingerprint, "--confirm", p.fingerprint,
                "--drop-unlisted", "--data-root", str(root)) == 1
    out, err = capsys.readouterr()
    lines = out.splitlines()
    held = [i for i, line in enumerate(lines) if line.startswith("  seq ")]
    assert [lines[i].split()[2] for i in held] == [n.fingerprint, a.fingerprint]
    assert "laptop-presence" in lines[held[0]] and "the genesis" in lines[held[0]]
    assert n.fingerprint in err and "genesis" in err
    # at a terminal the genesis is asked too
    _no_tty(monkeypatch, "y\ny\ny\n", tty=True)
    assert _cli("--signing-key", str(a.path), "--data-root", str(root)) == 0
    out = capsys.readouterr().out
    assert f"keep {n.fingerprint}" in out and out.count("? [y/N]") == 3
    assert not _uninitialised(root)


def test_f3_doctor_names_the_act_that_exists_and_says_re_genesis_is_not_in_this_build(tmp_path):
    root = _fresh(tmp_path)
    install = tmp_path / "inst"
    a = RealKey(tmp_path, "A")
    _init(root, a, {a.fingerprint})
    [r] = _check_cockpit_authority(install)
    assert r.warn and "levain cockpit enroll" in r.hint
    assert "`levain cockpit ledger re-genesis`" in r.hint and "not in this build" in r.hint
    from levain.cockpit.keys import revoke
    revoke(root, a.fingerprint, signer_fp=a.fingerprint, sign=a.sign_link, clock=lambda: NOW,
           compromised_since=1)
    [r] = _check_cockpit_authority(install)
    assert r.warn and "not in this build" in r.hint


def test_l1a_a_marker_without_a_legacy_table_still_initialises(v2):
    root, k = v2
    with _db(root) as c:
        c.execute("DROP TABLE legacy_keys")
    _init(root, k["A"], {k["A"].fingerprint}, drop=True)
    with _db(root) as c:
        assert c.execute("SELECT 1 FROM meta WHERE key = ?", (UNINITIALISED,)).fetchone() is None


def test_l1b_a_refusal_after_links_landed_says_where_the_log_is(v2, monkeypatch, capsys):
    import levain.cockpit.keys as keys_mod
    root, k = v2
    calls: list[int] = []

    def bad_third(message: bytes) -> str:
        calls.append(1)
        return "not a signature" if len(calls) == 3 else k["A"].sign_link(message)
    monkeypatch.setattr(keys_mod, "ssh_signer", lambda path, **kw: (k["A"].fingerprint, bad_third))
    _no_tty(monkeypatch)
    rc = _cli("--signing-key", str(k["A"].path), "--data-root", str(root),
              *(x for n in "APR" for x in ("--confirm", k[n].fingerprint)), "--drop-unlisted")
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("ledger init refused:") and err.count("\n") == 1
    assert "the log is at seq 2; the store stays uninitialised; re-run to resume" in err
    assert len(_log(root)) == 2 and _uninitialised(root)


def test_l1c_no_link_can_land_between_the_last_transactions_head_check_and_its_drop(v2, monkeypatch):
    # the competing append is tried INSIDE the last transaction, after its head check and before the drop:
    # it must not land (the write lock is held), and the head check must be on that same transaction
    from contextlib import contextmanager

    from levain.authority import anchor_path, sign_and_append
    root, k = v2
    real_write = CockpitDB.write
    seen: dict[str, object] = {"head_checked": False, "raced": None}

    class Proxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if sql.startswith("DROP TABLE"):
                seen["head_before_drop"] = seen["head_checked"]
                race = sqlite3.connect(root / "cockpit.db", timeout=0.2, isolation_level=None)
                try:
                    sign_and_append(race, {"op": "enrol", "fingerprint": k["X"].fingerprint,
                                           "public_key": k["X"].public, "class": "display-phone"},
                                    signer_fp=k["A"].fingerprint, sign=k["A"].sign_link, clock=lambda: NOW,
                                    anchor=anchor_path(root))
                    seen["raced"] = "landed"
                except sqlite3.OperationalError as exc:
                    seen["raced"] = str(exc)
                finally:
                    race.close()
            if "FROM authority_log" in sql:
                seen["head_checked"] = True
            return self._conn.execute(sql, *args)

    @contextmanager
    def racing_write(self):
        with real_write(self) as conn:
            yield Proxy(conn)
    monkeypatch.setattr(CockpitDB, "write", racing_write)
    _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    assert seen["head_before_drop"] is True and seen["raced"] is not None and "locked" in str(seen["raced"])
    assert len(_log(root)) == 2 and not _uninitialised(root)


# --- the 2b-1 L3 r1 fix round (s3a2/L3_r1/findings_r1.md) ------------------------------------------

def _plain_revoked_in_log(tmp_path: Path):
    """A laptop, R phone /2-revoked without a compromise, Q phone. Run 1 confirms all three and is cut at
    Q's enrol, after R's enrol and plain revoke landed (seq 1-3), so the store is still uninitialised."""
    root = _fresh(tmp_path)
    a, r, q = RealKey(tmp_path, "A"), RealKey(tmp_path, "R"), RealKey(tmp_path, "Q")
    _as_v2(root, [_row(a, "laptop-presence", "2026-10-01T00:00:01.000000Z"),
                  _row(r, "display-phone", "2026-10-01T00:00:02.000000Z", "2026-10-05T00:00:00.000000Z"),
                  _row(q, "display-phone", "2026-10-01T00:00:03.000000Z")])
    with pytest.raises(KeyboardInterrupt):
        _init(root, a, {a.fingerprint, r.fingerprint, q.fingerprint}, sign=_cut_at(4, a))
    assert [x[2:] for x in _log(root)][1:] == [("enrol", r.fingerprint, "2026-10-01T00:00:02.000000Z", None),
                                               ("revoke_key", r.fingerprint, None, None)]
    return root, a, r, q


def test_c1_a_resume_cannot_drop_a_key_the_cut_off_run_left_plainly_revoked(tmp_path):
    # the log holds R as a plain revocation: "dropped" would be a report the log does not back (its /2-era
    # fires read signed by a revoked key, not unverified), and no op can turn that revocation compromised
    root, a, r, q = _plain_revoked_in_log(tmp_path)
    with pytest.raises(KeyError_, match=f"{re.escape(r.fingerprint)} is already in this log, revoked by the "
                                        "interrupted run; confirm it to keep that record"):
        _init(root, a, {a.fingerprint, q.fingerprint}, drop=True)
    assert len(_log(root)) == 3 and _uninitialised(root)              # refused before any link
    res = _init(root, a, {a.fingerprint, r.fingerprint, q.fingerprint})
    assert r.fingerprint in res.kept and not res.dropped and not _uninitialised(root)


def _anchor_stuck_after_genesis(monkeypatch):
    """Make every anchor advance past seq 1 fail; returns the patch, for a ``monkeypatch.context``."""
    import levain.authority as authority_mod
    real_advance = authority_mod.advance_anchor

    def stuck(path, snap):
        if snap.seq > 1:
            raise OSError("the anchor's disk is full")
        return real_advance(path, snap)
    return lambda m: m.setattr(authority_mod, "advance_anchor", stuck)


def _cut_before_the_last_txn(root: Path, k, monkeypatch) -> None:
    """Run 1 lands A's genesis and P's enrol with the anchor stuck at seq 1, then is cut before its last
    transaction."""
    from levain.authority import anchor_path, read_anchor
    stuck = _anchor_stuck_after_genesis(monkeypatch)

    def cut(self):
        raise KeyboardInterrupt
    with monkeypatch.context() as m:
        stuck(m)
        m.setattr(CockpitDB, "write", cut)
        with pytest.raises(KeyboardInterrupt):
            _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    assert len(_log(root)) == 2 and _uninitialised(root) and read_anchor(anchor_path(root)).seq == 1


def test_c2_a_resume_that_appends_nothing_reports_an_anchor_it_cannot_advance(v2, monkeypatch):
    from levain.authority import anchor_path, read_anchor
    root, k = v2
    _cut_before_the_last_txn(root, k, monkeypatch)
    with monkeypatch.context() as m:
        _anchor_stuck_after_genesis(monkeypatch)(m)
        res = _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    assert res.links == () and not _uninitialised(root)
    assert res.warning is not None and "anchor is behind" in res.warning and "disk is full" in res.warning
    assert read_anchor(anchor_path(root)).seq == 1


def test_c2_a_resume_that_appends_nothing_advances_the_anchor_to_the_log_head(v2, monkeypatch):
    from levain.authority import anchor_path, read_anchor
    root, k = v2
    _cut_before_the_last_txn(root, k, monkeypatch)
    res = _init(root, k["A"], {k["A"].fingerprint, k["P"].fingerprint}, drop=True)
    assert res.links == () and res.warning is None and not _uninitialised(root)
    assert read_anchor(anchor_path(root)).seq == 2
