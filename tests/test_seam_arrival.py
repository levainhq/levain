"""C15 S4: arrival signals. A verified away OFF opens, a leave by a revoked key opens nothing, two agents open once each,
the silence gap is measured between operator activity records, and the Missing close row shows an unclosed seam.
Every leave here is a REAL signed one (ssh-keygen keys enrolled in a scratch cockpit's authority log), made through
``levain consent away`` exactly as the operator makes it."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cli import main
from levain.cockpit.firejournal import CockpitDB
from levain.coordinator import seam, seam_arrival
from levain.coordinator.store import CoordStore
from tests.test_consent_away import _queue
from tests.test_seam_verbs import _activate, run, sql


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


def _keygen(path: Path) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], check=True, capture_output=True,
                          text=True).stdout.split()[1]


def _sign(key: Path):
    return lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", auth.NAMESPACE], input=m,
                                    capture_output=True, check=True).stdout.decode()


class Cockpit:
    """A scratch cockpit whose authority log enrols two laptop keys, ``one`` first (the genesis) and ``two`` by it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "cockpit"
        self.root.mkdir()
        with CockpitDB(self.root).read():
            pass
        self.root = Path(os.path.realpath(self.root))
        self.keys = {n: tmp_path / n for n in ("one", "two")}
        self.fps = {n: _keygen(p) for n, p in self.keys.items()}
        for n in ("one", "two"):
            pub = " ".join(Path(str(self.keys[n]) + ".pub").read_text().split()[:2])
            self._append({"op": "enrol", "fingerprint": self.fps[n], "public_key": pub, "class": "laptop-presence",
                          "custody": "passphrase"}, signer="one")

    def _append(self, op: dict, *, signer: str) -> None:
        conn = sqlite3.connect(self.root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=self.fps[signer], sign=_sign(self.keys[signer]),
                                 clock=lambda: datetime.now(UTC), cache=ReplayCache(), anchor=None)
        finally:
            conn.close()

    def revoke(self, name: str, *, by: str = "one") -> None:
        self._append({"op": "revoke_key", "fingerprint": self.fps[name]}, signer=by)


@pytest.fixture
def cockpit(tmp_path):
    return Cockpit(tmp_path)


@pytest.fixture
def qdir(tmp_path, cockpit):
    return _queue(tmp_path, cockpit.root)


class Clock:
    def __init__(self) -> None:
        # frozen at the real now when the test starts: the CLI's own store reads the real clock, and the lease the
        # world holds must stay live for it; drops are dated base + offset (a future drop is harmless)
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(qdir, tmp_path, clock):
    s = CoordStore(qdir, clock=clock)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.launch, s.token = _activate(s)
    return s


def away_on(qdir, agent):
    assert main(["consent", "away", "on", "--agent", agent, "--queue-dir", str(qdir)]) == 0


def away_off(cockpit, qdir, agent, key="one"):
    assert main(["consent", "away", "off", "--agent", agent, "--signing-key", str(cockpit.keys[key]),
                 "--queue-dir", str(qdir)]) == 0


def light(qdir, capsys, token, *extra, rc=0):
    return run(qdir, capsys, "open", "--light", "--token", str(token), *extra, rc=rc)


def verified(qdir):
    return seam_arrival.verified_leaves(qdir)


# --- the verified leave -------------------------------------------------------------------------------------------
def test_a_verified_away_off_is_one_leave_and_one_open(world, cockpit, qdir, capsys):
    away_on(qdir, "claude_code")
    leaves, notes = verified(qdir)
    assert leaves == [] and notes == []                       # away ON is not an arrival
    away_off(cockpit, qdir, "claude_code")
    leaves, notes = verified(qdir)
    assert [lv.agent for lv in leaves] == ["claude_code"] and notes == []
    out = light(qdir, capsys, world.token)
    assert out.out.count("~~~ OPEN ") == 1 and "(operator)" in out.out and "coord open --ack" in out.out
    rows = sql(world, "SELECT signal_kind, signal_key, seam_kind FROM open_builds")
    assert rows == [("away_off", f"claude_code:{leaves[0].mark_id}", "operator")]
    assert len(sql(world, "SELECT 1 FROM arrival_signals")) == 1
    again = light(qdir, capsys, world.token)               # the same leave is the same open
    assert len(sql(world, "SELECT 1 FROM open_builds")) == 1 and len(sql(world, "SELECT 1 FROM arrival_signals")) == 1
    assert again.out.split("--\nopen ")[1].split(" (operator)")[0] == out.out.split("--\nopen ")[1].split(" (operator)")[0]


def test_a_leave_signed_by_a_revoked_key_opens_nothing(world, cockpit, qdir, capsys):
    away_on(qdir, "codex")
    away_off(cockpit, qdir, "codex", key="two")
    assert [lv.agent for lv in verified(qdir)[0]] == ["codex"]      # it verified while the key stood
    cockpit.revoke("two")
    assert verified(qdir) == ([], [])                               # the stored leave no longer verifies
    out = light(qdir, capsys, world.token)
    assert "no light open needed" in out.out
    assert sql(world, "SELECT 1 FROM open_builds") == [] and sql(world, "SELECT 1 FROM arrival_signals") == []


def test_two_agents_open_once_each(world, cockpit, qdir, capsys):
    for agent in ("claude_code", "codex"):
        away_on(qdir, agent)
        away_off(cockpit, qdir, agent)
    out = light(qdir, capsys, world.token)
    assert out.out.count("~~~ OPEN ") == 2
    keys = sorted(r[0].split(":")[0] for r in sql(world, "SELECT signal_key FROM open_builds"))
    assert keys == ["claude_code", "codex"]
    light(qdir, capsys, world.token)
    light(qdir, capsys, world.token)
    assert len(sql(world, "SELECT 1 FROM open_builds")) == 2 and len(sql(world, "SELECT 1 FROM arrival_signals")) == 2


def test_the_read_never_creates_or_migrates_the_queue_file(world, tmp_path, cockpit, clock):
    bare = tmp_path / "bare"
    bare.mkdir(mode=0o700)
    assert seam_arrival.verified_leaves(bare) == ([], [])
    assert not (bare / "queue.db").exists()
    # and a coordinator store with no queue.db anywhere near it opens a light brief with no queue file made
    s = CoordStore(bare, clock=clock)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.launch, s.token = _activate(s)
    assert seam_arrival.verified_leaves(s.directory) == ([], [])
    assert not (bare / "queue.db").exists()


def test_an_unreadable_queue_opens_nothing_and_says_so(world, qdir):
    (qdir / "queue.db").write_bytes(b"this is not a database")
    leaves, notes = verified(qdir)
    assert leaves == [] and len(notes) == 2 and all("could not be read" in n for n in notes)


def test_an_operator_terminal_prints_what_it_found_and_builds_nothing(world, cockpit, qdir, capsys):
    away_on(qdir, "claude_code")
    away_off(cockpit, qdir, "claude_code")
    out = run(qdir, capsys, "open", "--light")
    assert "away OFF: claude_code" in out.out and "nothing was built" in out.out
    assert sql(world, "SELECT 1 FROM open_builds") == [] and sql(world, "SELECT 1 FROM arrival_signals") == []
    err = run(qdir, capsys, "open", "--light", "--token", str(world.token + 99), rc=1).err
    assert "was refused" in err


# --- the gap rule -------------------------------------------------------------------------------------------------
def drop(store, clock, **adv):
    clock.advance(**adv)
    d, _ = store.accept_drop("words " + str(clock.t), surface="cli")
    return d.drop_id


def test_the_gap_is_between_operator_records_not_since_an_open(tmp_path, clock):
    """Measured on a store with no lease (a drop dated hours ahead would lapse a held one): the answer is a function of
    the two newest operator records alone."""
    s = CoordStore(tmp_path / "gap", clock=clock)
    assert "no operator activity record yet" in seam_arrival.silence(s).why
    drop(s, clock, minutes=0)
    assert "only operator activity record" in seam_arrival.silence(s).why
    drop(s, clock, hours=1)
    near = seam_arrival.silence(s)
    assert not near.fired and near.gap_hours == pytest.approx(1.0) and "previous activity 1.0 h ago" in near.why
    far = drop(s, clock, hours=5)
    fired = seam_arrival.silence(s)
    assert fired.fired and fired.key == f"drop:{far}" and fired.gap_hours == pytest.approx(5.0)
    drop(s, clock, minutes=10)                  # the next record is only 10 minutes after `far`: the gap is to IT
    again = seam_arrival.silence(s)
    assert not again.fired and again.gap_hours == pytest.approx(10 / 60)
    assert seam_arrival.silence(s, gap_hours=0.1).fired                     # the threshold is the caller's
    with pytest.raises(Exception, match="zero or more"):
        seam_arrival.silence(s, gap_hours=-1)


def test_light_fires_on_the_gap_and_a_repeat_is_the_same_open(world, qdir, clock, capsys):
    out = light(qdir, capsys, world.token)          # the activation drop is the only record
    assert "only operator activity record" in out.out and sql(world, "SELECT 1 FROM open_builds") == []
    newest = world.accept_drop("one more", surface="cli")[0].drop_id
    out = light(qdir, capsys, world.token)
    assert "no light open needed: previous activity " in out.out and sql(world, "SELECT 1 FROM open_builds") == []
    out = light(qdir, capsys, world.token, "--gap-hours", "0")
    assert out.out.count("~~~ OPEN ") == 1 and "(light)" in out.out
    assert sql(world, "SELECT signal_kind, signal_key, seam_kind FROM open_builds") == [
        ("silence", f"drop:{newest}", "light")]
    light(qdir, capsys, world.token, "--gap-hours", "0")
    assert len(sql(world, "SELECT 1 FROM open_builds")) == 1
    err = run(qdir, capsys, "open", "--light", "--token", str(world.token), "--gap-hours", "-1", rc=1).err
    assert "zero or more" in err


# --- the Missing close row ----------------------------------------------------------------------------------------
def _brief(world, qdir, clock):
    return seam.open_brief(world, qdir, "light", clock()).text


def test_the_missing_close_row_shows_when_a_seam_has_no_close(world, qdir, clock, tmp_path, capsys):
    assert "Missing close" not in _brief(world, qdir, clock)         # no mark: folded into the Empty line
    away_on(qdir, "claude_code")
    text = _brief(world, qdir, clock)
    assert "Missing close: operator seam began " in text and "no operator close recorded" in text
    body = tmp_path / "b.json"
    body.write_text(json.dumps({"state_line": "heading to bed", "in_flight": [], "next": "", "contingencies": [],
                                "notes": "", "attested_operator_present": True, "attested_walked": False}))
    run(qdir, capsys, "close", "--kind", "operator", "--body-file", str(body), "--request-id", "c1", "--token",
        str(world.token))
    closed = _brief(world, qdir, clock)
    assert "Missing close: operator seam" not in closed and "missing close" in closed.lower()
    time.sleep(1.1)                                                  # stamps are whole seconds
    away_on_again = ["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]
    assert main(away_on_again) == 0                                  # a LATER mark than the close: missing again
    assert "the last operator close was" in _brief(world, qdir, clock)


def test_a_rotation_close_does_not_close_an_operator_seam(world, qdir, clock, tmp_path, capsys):
    away_on(qdir, "claude_code")
    body = tmp_path / "b.json"
    body.write_text(json.dumps({"state_line": "rotating", "in_flight": [], "next": "", "contingencies": [],
                                "notes": "", "attested_operator_present": False, "attested_walked": False}))
    run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(body), "--request-id", "c2", "--token",
        str(world.token))
    assert "Missing close: operator seam began " in _brief(world, qdir, clock)


def test_an_unreadable_queue_is_cannot_derive_for_missing_close_not_zero(world, qdir, clock):
    (qdir / "queue.db").write_bytes(b"this is not a database")
    text = _brief(world, qdir, clock)
    assert "Missing close: cannot-derive" in text
