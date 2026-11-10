"""C15 S4: a verified away leave opens the operator's return, ``coord open --light`` and its gap rule, and the Missing
close row. Every expectation was first read from a real run on a scratch store and a scratch cockpit (an active
desk launch, a signed `consent away on` / `off`), printed in full by the lane's run script."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cli import main
from levain.desk import seam, seam_arrival, seam_build, seam_open
from levain.desk.store import CoordStore
from tests.test_consent_away import _cockpit, _queue
from tests.test_seam_verbs import _activate, _body, body_file


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


class World:
    """A desk store beside its consent queue and a real cockpit with one enrolled laptop key."""

    def __init__(self, tmp_path, capsys, starter=False):
        (tmp_path / "ck").mkdir()
        self.q = _queue(tmp_path, _cockpit(tmp_path / "ck"))
        self.key = tmp_path / "ck" / "laptop"
        self.capsys = capsys
        self.store = CoordStore(self.q)
        self.store.coord_set_agent("claude", cwd=str(tmp_path))
        if starter:          # as a real launch runs: the cold start builds its open before the launch activates
            d, _ = self.store.accept_drop("words", surface="cli")
            lid = self.store.woken_launch(d.drop_id)
            self.store.launch_claim_spawn(lid, os.getpid())
            seam_build.starter_build(self.store, lid, pid=os.getpid(), kind="light", queue_dir=self.q)
            self.store.launch_activate(lid, harness_session_id="h-" + lid)
            self.token = self.store.coord_status()["lease"]["token"]
        else:
            _, self.token = _activate(self.store)

    def run(self, *argv, rc=0):
        got = main(list(argv))
        out = self.capsys.readouterr()
        assert got == rc, (argv, out.out, out.err)
        return out.out

    def coord(self, *argv):
        return self.run("coord", *argv, "--token", str(self.token), "--queue-dir", str(self.q))

    def away(self, agent, on=True, key=None):
        extra = [] if on else ["--signing-key", str(key or self.key)]
        return self.run("consent", "away", "on" if on else "off", "--agent", agent, *extra, "--queue-dir", str(self.q))

    def builds(self):
        from contextlib import closing

        with closing(self.store._connect()) as c:
            if not seam_open.open_tables_present(c):
                return []
            return c.execute("SELECT open_id, seam_kind, signal_kind, signal_key FROM open_builds WHERE signal_kind = "
                             "'away_off' ORDER BY open_id").fetchall()


@pytest.fixture
def world(tmp_path, capsys):
    return World(tmp_path, capsys)


def test_a_verified_leave_opens_once_and_a_missing_close_is_named(world):
    world.run("coord", "close", "--kind", "operator", "--body-file", str(body_file(world.q.parent, _body())),
              "--request-id", "r1", "--token", str(world.token), "--queue-dir", str(world.q))
    world.away("claude_code")
    before = world.coord("open")
    assert "a signed leave" not in before and world.builds() == []        # away on: nothing to consume
    assert re.search(r"Missing close: operator seam began \S+ \(claude_code away\); no operator close after it \(", before)
    world.away("claude_code", on=False)
    first = world.coord("open")
    ((open_id, seam, signal, key),) = world.builds()
    assert (seam, signal) == ("operator", "away_off") and key.startswith("claude_code:")
    assert f"open {open_id} (operator)" in first and "arrival_signals: 1" in first
    again = world.coord("open")
    fence = lambda t: re.sub(r"(~~~ (?:END )?OPEN) [0-9a-f]{8}", r"\1 F", t)
    assert fence(again) == fence(first) and world.builds() == [(open_id, seam, signal, key)]   # a repeat builds nothing
    world.coord("open", "--ack", str(open_id))
    assert world.builds() == [(open_id, seam, signal, key)]


def test_a_revoked_key_leave_opens_nothing(world, tmp_path):
    second = tmp_path / "second"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(second)], check=True)
    pub = " ".join((tmp_path / "second.pub").read_text().split()[:2])
    fp = lambda p: subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(p)], check=True, capture_output=True,
                                  text=True).stdout.split()[1]

    def link(op):
        import sqlite3

        conn = sqlite3.connect(world.q.parent / "ck" / "cockpit" / "cockpit.db", isolation_level=None)
        auth.sign_and_append(conn, op, signer_fp=fp(world.key),
                             sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(world.key), "-n",
                                                            auth.NAMESPACE], input=m, capture_output=True,
                                                           check=True).stdout.decode(),
                             clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
        conn.close()

    link({"op": "enrol", "fingerprint": fp(second), "public_key": pub, "class": "laptop-presence",
          "custody": "passphrase"})
    world.away("claude_code")
    world.away("claude_code", on=False, key=second)
    link({"op": "revoke_key", "fingerprint": fp(second)})
    out = world.coord("open")
    assert world.builds() == [] and "arrival_signals" not in out          # the mark stands: no leave, no open
    assert seam_arrival.verified_leaves(world.q) == ([], [])


def test_two_agents_open_once_each(world):
    for agent in ("claude_code", "codex"):
        world.away(agent)
        world.away(agent, on=False)
    first = world.coord("open")
    assert "1 more leave open(s) wait" in first and len(world.builds()) == 2
    keys = {k for _, _, _, k in world.builds()}
    assert {k.split(":")[0] for k in keys} == {"claude_code", "codex"}
    world.coord("open", "--ack", str(world.builds()[0][0]))
    second = world.coord("open")
    assert f"open {world.builds()[1][0]} (operator)" in second and "more leave" not in second
    world.coord("open", "--ack", str(world.builds()[1][0]))
    assert len(world.builds()) == 2                                       # nothing is opened a third time
    world.coord("open")
    assert len(world.builds()) == 2


def test_the_light_gap_is_between_operator_activity_not_since_an_open(world, tmp_path):
    world.store.accept_drop("one", surface="cli")
    world.store.accept_drop("two", surface="cli")
    quiet = world.coord("open", "--light")
    assert re.fullmatch(r"no light open needed: previous activity \d+\.\d h ago\n", quiet)
    assert world.coord("open", "--light") == quiet                        # the same gap however many opens ran between
    got = world.coord("open", "--light", "--gap-hours", "0.000000001")
    assert "SEAM OPEN light" in got and "(light)" in got.split("--\n")[-1]
    # the same rule with an hours-long gap, read from a store whose clock the test sets
    now = [datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)]
    os.makedirs(tmp_path / "gapq", mode=0o700)
    s = CoordStore(tmp_path / "gapq", clock=lambda: now[0])
    s.accept_drop("a", surface="cli")
    now[0] += timedelta(hours=5)
    s.accept_drop("b", surface="cli")
    needed, why, latest = seam_arrival.light_gap(s, 4.0)
    assert needed and "5.0 h" in why and latest.ref.startswith("drop:")
    now[0] += timedelta(hours=1)
    s.accept_drop("c", surface="cli")
    assert seam_arrival.light_gap(s, 4.0)[:2] == (False, "no light open needed: previous activity 1.0 h ago")


def test_light_passes_an_acknowledged_open(tmp_path, capsys):
    w = World(tmp_path, capsys, starter=True)
    starter = re.search(r"--ack (\d+)", w.coord("open")).group(1)
    w.coord("open", "--ack", starter)
    w.store.accept_drop("later", surface="cli")
    got = w.coord("open", "--light", "--gap-hours", "0.000000001")
    light = re.search(r"open (\d+) \(light\)", got).group(1)
    assert light != starter                                               # the acknowledged starter is not reprinted
    w.coord("open", "--ack", light)
    assert w.coord("open", "--light", "--gap-hours", "0.000000001") == (
        f"no light open needed: the light open for this activity (open {light}) is acknowledged\n")


def test_gap_hours_a_duration_cannot_hold_is_refused(world):
    world.store.accept_drop("two", surface="cli")
    assert main(["coord", "open", "--light", "--gap-hours", "1e308", "--token", str(world.token), "--queue-dir",
                 str(world.q)]) == 1
    assert "a duration can hold" in world.capsys.readouterr().err


def test_ack_refuses_the_light_flags(world):
    assert main(["coord", "open", "--ack", "1", "--light", "--gap-hours", "-1", "--token", str(world.token),
                 "--queue-dir", str(world.q)]) == 1
    assert "takes no --light or --gap-hours" in world.capsys.readouterr().err


@pytest.mark.parametrize("band", [lambda *a, **k: 1 / 0, lambda *a, **k: []])
def test_missing_close_is_read_without_the_band(world, monkeypatch, band):
    world.away("claude_code")
    monkeypatch.setattr(seam, "_band", band)
    assert "Missing close: operator seam began" in world.run("coord", "open", "--queue-dir", str(world.q))


def test_leaves_open_oldest_mark_first(world):
    for agent in ("codex", "claude_code"):
        world.away(agent)
    for agent in ("claude_code", "codex"):
        world.away(agent, on=False)
    leaves, notes = seam_arrival.verified_leaves(world.q)
    assert [lv.agent for lv in leaves] == ["codex", "claude_code"] and notes == []


def test_a_build_that_returns_nothing_is_a_note(world, monkeypatch):
    world.away("claude_code")
    world.away("claude_code", on=False)
    monkeypatch.setattr(seam_arrival, "build", lambda *a, **k: None)
    builds, notes = seam_arrival.ingest_leaves(world.store, world.q, world.token)
    assert builds == [] and notes == [notes[0]] and "no build was returned" in notes[0]


def test_a_stale_lookup_records_the_leave_once(world, monkeypatch):
    world.away("claude_code")
    world.away("claude_code", on=False)
    (first,), _ = seam_arrival.ingest_leaves(world.store, world.q, world.token)
    real = seam_arrival.stored_build
    # the racing reader's view outside the fence misses the build; the look under the fence sees it
    monkeypatch.setattr(seam_arrival, "stored_build", lambda conn, *a: real(conn, *a) if conn.in_transaction else None)
    monkeypatch.setattr(seam_arrival, "record_arrival", lambda *a, **k: pytest.fail("recorded a consumed leave"))
    (again,), notes = seam_arrival.ingest_leaves(world.store, world.q, world.token)
    assert again.open_id == first.open_id and notes == []
