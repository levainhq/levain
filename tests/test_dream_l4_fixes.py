"""The D1-L1 L4 findings, each pinned by the failure that was REPRODUCED in a scratch store (never a prescription).

F1  an orphan wrap keeps the dream quiet every night; the text said "a stale orphan is healed inside the effect" and the
    card told the operator to run ``levain agents run-now dream``, which is not a verb.
F2  ``levain dream rollback <occurrence>`` as printed on cards needs ``--store-dir``; a ``failed`` card carried the same
    rollback remedy and the verb refuses it.
F3  the ready module said "the card can say who was live"; the card has no seat field.
F4  ``levain dream --help`` said "Nothing schedules a dream"; the README row was stale.
F5  the desk brief said dreaming is not built, and nothing said a running desk keeps the memory it loaded.

A printed remedy is held to one rule here: every ``levain ...`` command in it parses with the real parser, and every
anneal command in it is RUN against the scratch store.
"""
from __future__ import annotations

import argparse
import io
import re
import shlex
import sqlite3
import subprocess
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

from levain import cli as levain_cli
from levain.desk import review
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import cli as dcli
from levain.dream import ready as rd
from levain.dream import recover as rc
from tests.test_dream_agent import FEED, hold_a_wrap_open, rs, row  # noqa: F401
from tests.test_dream_ready import Entity, ent  # noqa: F401
from tests.test_dream_snapshot import _VALID, add_episode, dream, occ, scratch  # noqa: F401

REPO = Path(__file__).resolve().parents[1]


def commands(text: str, start: str) -> list[str]:
    """Every backticked command in ``text`` that starts with ``start``."""
    return [c for c in re.findall(r"`([^`]+)`", text) if c.startswith(start)]


def assert_levain_commands_parse(text: str) -> list[str]:
    cmds = commands(text, "levain ")
    for c in cmds:
        argv = shlex.split(c)[1:]
        try:
            levain_cli.build_parser().parse_args(argv)
        except SystemExit as exc:
            pytest.fail(f"`{c}` does not parse (exit {exc.code}): the verb or a required flag is not there")
    return cmds


def age_the_open_wrap(db: Path, days: int = 3) -> None:
    """A dead seat's orphan: the same wrap, started ``days`` ago (what the L4 run did to its scratch store)."""
    conn = sqlite3.connect(str(db))
    conn.create_function("anneal_writer_schema", -1, lambda *a: 2)
    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert conn.execute("update metadata set value=? where key='wrap_started_at'", (old,)).rowcount == 1
    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------------------------------------- F1

def test_f1_an_aged_orphan_keeps_the_dream_quiet_and_the_text_says_nothing_here_heals_it(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db)
    for manual in (False, True):                       # a manual run is no different: check 2 has no waiver
        v = ent.evaluate(manual=manual)
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
        assert "healed" not in v.reason and "inside the effect" not in v.reason, v.reason
        assert "wrap-cancel" in v.reason and "nothing" in v.reason, v.reason


def test_f1_the_default_remedy_of_every_class_names_only_verbs_that_exist():
    for cls in ("wrap-open", "baton", "failed", "laptop-slept", "unknown", "unreadable"):
        text = da.window_remedy(cls, "/tmp/some/store/memory.db")
        assert "run-now" not in text, cls
        assert_levain_commands_parse(text)


def test_f1_a_night_quiet_on_an_orphan_files_a_card_whose_anneal_commands_run_and_clear_it(ent, rs):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db)
    rows = [row("skipped-no-input", key=f"2026-10-31T0{i}:00:00Z",
                reason=f"quiet/wrap-open: {ent.evaluate().reason}") for i in range(3)]
    r = da.close_window(agent_id=da_id(), day=date(2026, 10, 30), now=datetime(2026, 10, 31, 9, 0,
                        tzinfo=ZoneInfo("America/New_York")), zone="America/New_York", rows=rows, review_store=rs,
                        feed=FEED, store_path=ent.db)
    assert r.action == "filed" and r.cls == "wrap-open", (r.action, r.cls, r.why)
    remedy = review.list_items(rs, feed=FEED)[0].body["remedy"]
    assert "run-now" not in remedy
    assert_levain_commands_parse(remedy)
    status, cancel = [shlex.split(c) for c in commands(remedy, "/") if " wrap-" in c]
    assert str(ent.db) in status and str(ent.db) in cancel
    out = subprocess.run(status, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "wrap in progress" in out.stdout, out.stdout + out.stderr
    token = re.search(r"token:\s+([0-9a-f]{32})", out.stdout).group(1)
    assert cancel[-1] == "<token>"                                    # the one placeholder: the token status printed
    cancel = cancel[:-1] + [token]
    out = subprocess.run(cancel, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "wrap cancelled" in out.stdout, out.stdout + out.stderr
    assert ent.evaluate().verdict == rd.READY                         # the way to proceed: the dream is admitted again


def da_id() -> str:
    from tests.test_dream_agent import AID

    return AID


# ------------------------------------------------------------------------------------------------------------- F2

def test_f2_the_rollback_remedy_on_a_consolidated_card_runs_as_printed(scratch, rs, capsys):
    r = dream(scratch, "o1", _VALID)
    assert r.outcome == "saved"
    got = da._file_run_card("saved", {}, "o1", occ(scratch, "o1"), scratch, rs, "2026-11-02", False, None, FEED)
    assert got == "filed"
    remedy = review.list_items(rs, feed=FEED)[0].body["remedy"]
    assert remedy.startswith("levain dream rollback o1 --store-dir "), remedy
    assert levain_cli.main(shlex.split(remedy)[1:]) == 0               # the printed command, run through the real CLI
    assert "rolled back o1" in capsys.readouterr().out


def test_f2_a_failed_card_names_no_rollback_and_the_verb_would_have_refused_it(scratch, rs, capsys):
    dream(scratch, "o1", _VALID)                                       # a saved dream: rollback has something to refuse
    add_episode(scratch, "a seat's capture after the dream")
    hold_a_wrap_open_on(scratch)                                      # the seat-wrap race: a wrap opens, the run fails
    got = da._file_run_card("precondition", {"detail": "wrap in progress: a wrap is already in progress"}, "o2",
                            occ(scratch, "o2"), scratch, rs, "2026-11-02", False, None, FEED)
    assert got == "filed"
    body = review.list_items(rs, feed=FEED)[0].body
    assert body["kind"] == "failed"
    assert "levain dream rollback" not in body["remedy"] and "nothing to roll back" in body["remedy"], body["remedy"]
    assert_levain_commands_parse(body["remedy"])
    # the remedy it used to print, run for real: refused ("Nothing was changed")
    assert levain_cli.main(["dream", "rollback", "o1", "--store-dir", str(scratch.parent)]) == 1
    assert "REFUSED (wrap-open)" in capsys.readouterr().out


def hold_a_wrap_open_on(db: Path) -> None:
    from anneal_memory import Store
    from anneal_memory.continuity import prepare_wrap

    with Store(str(db), section_schema=None) as s:
        assert prepare_wrap(s, wrap_token="b" * 32)["status"] == "ready"


def test_f2_recovery_cards_carry_the_store_and_a_failed_one_names_no_rollback(scratch):
    spec = rc._card("failed", rc.UNKNOWN_COMMIT, "o2", his=["x"])
    assert "remedy" not in spec                                       # the store is not known to the spec
    c = cd.from_spec(spec, occ_dir=occ(scratch, "o2"), db_path=scratch.parent / "memory.db")
    assert "levain dream rollback" not in c.body["remedy"]
    assert str(scratch.parent) in c.body["remedy"]
    ok = cd.build_card("consolidated", occurrence="o1", store_dir=scratch.parent)
    assert f"--store-dir {shlex.quote(str(scratch.parent))}" in ok.body["remedy"]
    done = cd.build_card("rolled-back", occurrence="o1", store_dir=scratch.parent)
    assert "levain dream rollback" not in done.body["remedy"]          # rolling back twice is "already-reverted"


def test_f2_the_signing_card_undo_command_carries_the_store_dir():
    kw = dict(per_run_usd=20.0, night_usd=40.0, expires="x")
    known = da.signing_card_text("America/New_York", store_dir="/data/levain store", **kw)
    assert "levain dream rollback <occurrence> --store-dir '/data/levain store'" in known
    unknown = da.signing_card_text("America/New_York", **kw)
    assert "--store-dir <the store's directory>" in unknown            # never a bare command that cannot run




def test_f2_enable_prints_the_undo_command_with_the_installs_store(tmp_path, capsys):
    import json

    from tests.test_dream_agent import _enable

    e = Entity(tmp_path / "ent")
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir()
    code, out, err, _ = _enable(tmp_path, capsys, "--store-dir", str(e.store_dir), "--cockpit-root", str(cockpit),
                                "--coord-dir", str(e.coord_dir))
    assert code == 0, err
    card = json.loads(out)["signing_card"]
    assert f"`levain dream rollback <occurrence> --store-dir {e.store_dir.resolve()}`" in card
