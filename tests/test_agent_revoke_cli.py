"""``levain agents revoke`` and ``revoke --abandon`` (BUILD_PLAN P5c-2c): the CLI verbs over P5c-2b's steps.

The log is real (ssh-keygen keys, links verified through the replay) and the autonomic store is the real agent store
opened through ``open_agent_store`` on a scratch directory, so the verb reads the store id the way an operator's does.
The signer is the World's laptop key behind the same ``ssh_signer`` seam ``levain agents sign`` uses; no real key is
asked for.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3

import pytest

import levain.authority as auth
from levain import agent_authority as aa
import levain.cockpit.keys as keys
from levain.agents import bars, cli
from levain.agents.store import open_agent_store
from levain.cockpit.firejournal import CockpitDB

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, World, create  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


class CliRig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.world = World(tmp_path)
        CockpitDB(self.world.root).set_install_class("reported")
        self.store_dir = tmp_path / "home" / "autonomic"
        self.agent_store = open_agent_store(self.store_dir)
        self.conn = sqlite3.connect(self.agent_store.path, isolation_level=None)
        self.store_id = self.conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
        self.fp, self.sign = self.world.keys["laptop"]
        self.gestures = 0
        monkeypatch.setattr(cli, "_utcnow", lambda: self.world.clock.now)     # the log's times never go back
        monkeypatch.setattr(keys, "ssh_signer", lambda path, **kw: (self.fp, self._counted))

    def _counted(self, message: bytes) -> str:
        self.gestures += 1
        return self.sign(message)

    def args(self, agent: str = AGENT, *, abandon: bool = False, signed: bool = True) -> argparse.Namespace:
        return argparse.Namespace(agent=agent, abandon=abandon, store_dir=self.store_dir,
                                  data_root=self.world.root if signed else None,
                                  signing_key=self.tmp / "keys" / "laptop" if signed else None)

    def revoke(self, agent: str = AGENT) -> int:
        return cli.run_revoke(self.args(agent))

    def abandon(self, agent: str = AGENT) -> int:
        return cli.run_revoke(self.args(agent, abandon=True, signed=False))

    def bars(self) -> list[bars.Bar]:
        return [bars._bar(r) for r in self.conn.execute(f"{bars._SELECT} ORDER BY rowid")]

    def revokes(self) -> int:
        return self.revokes_of(AGENT)

    def revokes_of(self, agent: str) -> int:
        return auth.agent_last_revoke(self.world.state(), agent, self.store_id)


@pytest.fixture
def rig(tmp_path, monkeypatch) -> CliRig:
    r = CliRig(tmp_path, monkeypatch)
    create(r.world, 1, store=r.store_id)
    return r


def test_revoke_signs_once_appends_and_closes_the_bar_signed(rig, capsys):
    assert rig.revoke() == 0
    out = capsys.readouterr().out
    (bar,) = rig.bars()
    assert (bar.reason, bar.closed_at is not None, bar.revoke_seq) == ("signed", True, rig.revokes()) and bar.revoke_seq
    assert rig.gestures == 1 and f"revoke landed at seq {bar.revoke_seq}" in out
    print(out)


def test_the_verb_is_on_the_parser_and_a_revoke_needs_a_key_and_a_root(rig, capsys):
    from levain.cli import main

    assert main(["agents", "--store-dir", str(rig.store_dir), "revoke", AGENT]) == 1
    assert "needs --data-root and --signing-key" in capsys.readouterr().err
    assert rig.bars() == []


def test_abandon_closes_the_open_bar_abandoned_with_a_null_seq_and_an_attention_line(rig, capsys):
    step = aa.revoke_agent_begin(rig.conn, sqlite3.connect(rig.world.db, isolation_level=None), AGENT,
                                     store_id=rig.store_id, signer_fp=rig.fp, clock=lambda: rig.world.clock.now)
    assert rig.abandon() == 0
    cap = capsys.readouterr()
    (bar,) = rig.bars()
    assert bar.bar_id == step.bar_id and (bar.reason, bar.revoke_seq, bar.closed_at is not None) == ("abandoned", None, True)
    assert f"attention [agent-abandon:{bar.bar_id}]" in cap.err and "nothing was appended" in cap.out
    with pytest.raises(sqlite3.DatabaseError):      # a bar is never deleted
        rig.conn.execute("DELETE FROM agent_bars")
    assert rig.revokes() == 0


def test_abandon_with_nothing_open_says_so_even_when_an_old_revoke_landed(rig, capsys):
    assert rig.abandon() == 1 and "no open revoke" in capsys.readouterr().err
    assert rig.revoke() == 0
    capsys.readouterr()
    assert rig.abandon() == 1          # no open bar was ever seen by this call: a stale landed bar is not "lost the race"
    assert "no open revoke" in capsys.readouterr().err
    assert len(rig.bars()) == 1


def test_abandon_that_loses_the_close_to_a_landing_revoke_reports_its_seq(rig, capsys, monkeypatch):
    aa.revoke_agent_begin(rig.conn, sqlite3.connect(rig.world.db, isolation_level=None), AGENT,
                          store_id=rig.store_id, signer_fp=rig.fp, clock=lambda: rig.world.clock.now)
    real = bars.close_bar

    def lose(conn, bar_id, *, revoke_seq, reason, now):
        real(conn, bar_id, revoke_seq=7, reason="signed", now=now)      # the revoke's finish got there first
        return False

    monkeypatch.setattr(bars, "close_bar", lose)
    assert rig.abandon() == 0
    assert "the revoke had already landed at seq 7; nothing to abandon" in capsys.readouterr().out


def test_a_finish_that_loses_to_a_concurrent_landing_reports_it_and_exits_0(rig, capsys, monkeypatch):
    real = aa.revoke_agent_finish

    def lands_then_refuses(autonomic, cockpit, bar_id, **kw):
        real(autonomic, cockpit, bar_id, **kw)       # the other process's revoke landed; ours cannot prepare
        raise aa.AgentAuthorityError("the revoke cannot be prepared: no agent-link to remove")

    monkeypatch.setattr(aa, "revoke_agent_finish", lands_then_refuses)
    assert rig.revoke() == 0
    cap = capsys.readouterr()
    (bar,) = rig.bars()
    assert f"already landed at seq {bar.revoke_seq}" in cap.out and bar.revoke_seq


def test_a_failure_after_begin_says_the_agent_stays_barred(rig, capsys, monkeypatch):
    def refuse(*a, **k):
        raise aa.AgentAuthorityError("the revoke cannot be prepared")

    monkeypatch.setattr(aa, "_prepare_onto", refuse)
    assert rig.revoke() == 1
    err = capsys.readouterr().err
    assert "the agent stays barred; run revoke again or --abandon" in err
    (bar,) = rig.bars()
    assert bar.closed_at is None


def test_an_abandoned_result_says_not_revoked_and_exits_0(rig, capsys, monkeypatch):
    monkeypatch.setattr(aa, "revoke_agent_sign", lambda autonomic, bar_id, sign, **kw: aa.RevokeStep(
        "abandoned", bar_id, None, "the revoke was abandoned; nothing was appended"))
    assert rig.revoke() == 0
    assert "NOT revoked" in capsys.readouterr().out


def test_a_store_error_is_a_one_line_failure_not_a_traceback(rig, capsys, monkeypatch):
    from levain.autonomic.db import StoreFormatError

    for exc in (sqlite3.OperationalError("database is locked"), StoreFormatError("bad store")):
        def boom(*a, _e=exc, **k):
            raise _e

        monkeypatch.setattr(aa, "revoke_agent_begin", boom)
        assert rig.revoke() == 1
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and str(exc) in err and "Traceback" not in err


def test_an_unknown_terminal_state_is_a_failure_not_success(rig, capsys, monkeypatch):
    monkeypatch.setattr(aa, "revoke_agent_finish", lambda autonomic, cockpit, bar_id, **kw: aa.RevokeStep(
        "refused", bar_id, None, "policy refused"))
    assert rig.revoke() == 1
    assert "unexpected revoke state 'refused'" in capsys.readouterr().err
