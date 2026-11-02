"""C15 S4: a verified away leave opens the operator's return, ``coord open --light`` and its gap rule, and the Missing
close row. Every expectation was first read from a real run on a scratch store and a scratch cockpit (an active
coordinator launch, a signed `consent away on` / `off`), printed in full by the lane's run script."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cli import main
from levain.coordinator import seam_arrival, seam_open
from levain.coordinator.store import CoordStore
from tests.test_consent_away import _cockpit, _queue
from tests.test_seam_verbs import _activate, _body, body_file


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


class World:
    """A coordinator store beside its consent queue and a real cockpit with one enrolled laptop key."""

    def __init__(self, tmp_path, capsys):
        (tmp_path / "ck").mkdir()
        self.q = _queue(tmp_path, _cockpit(tmp_path / "ck"))
        self.key = tmp_path / "ck" / "laptop"
        self.capsys = capsys
        self.store = CoordStore(self.q)
        self.store.coord_set_agent("claude", cwd=str(tmp_path))
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
