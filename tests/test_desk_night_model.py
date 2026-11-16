"""ND-5a: a night model in the desk setting (night desk design 2.5, row ND-5). The setting gains ``night_model``; the
NIGHT launch (``wake(night=True)``) runs it, a day launch runs ``model`` unchanged. The fake ``claude`` and fixtures are
test_desk_launch's: no test here spawns the real one."""
from __future__ import annotations

import sqlite3

import pytest

from levain.desk import launch as cl
from levain.desk import lease
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordStore
from tests.test_desk_launch import (  # noqa: F401  (fixtures)
    _levain_home, _main, fake, live_hook, qdir, tmp_home, workdir)


def _argv_model(argv):
    return argv[argv.index("--model") + 1] if "--model" in argv else None


@pytest.fixture
def store(qdir, workdir):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()), night_model="opus")
    return s


def _night_start(store):
    ln = store.coord_wake(night=True)
    assert ln is not None
    return ln


def test_the_setting_carries_the_night_model_and_keeps_it(store, workdir):
    st = store.coord_setting()
    assert (st.model, st.night_model) == ("haiku", "opus")
    # a later set that does not name it keeps it (as the user layer is kept); the day model moves alone
    got = store.coord_set_agent("claude", model="sonnet", cwd=str(workdir.resolve()))
    assert (got.model, got.night_model) == ("sonnet", "opus")
    # --user-scope alone and a directory move go through the same keep
    assert store.coord_set_user_scope(True).night_model == "opus"
    # agent none holds no night model
    assert store.coord_set_agent("none").night_model is None


def test_an_unsafe_night_model_is_refused_and_nothing_is_written(store, qdir, workdir):
    for bad in ("--dangerously-skip-permissions", "opus; rm", "a b", "x" * 201):
        with pytest.raises(lease.LeaseRefused, match="letters, digits"):
            store.coord_set_agent("claude", cwd=str(workdir.resolve()), night_model=bad)
    with pytest.raises(lease.LeaseRefused, match="no --night-model"):
        store.coord_set_agent("none", night_model="opus")
    assert store.coord_setting().night_model == "opus"


def test_a_store_written_before_night_model_reads_none_and_migrates(store, qdir, workdir):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        c.execute("ALTER TABLE coord_settings DROP COLUMN night_model")     # the store as 0.x left it
        c.commit()
    finally:
        c.close()
    # a reader that never migrates still reads the setting, night model None
    st = lease.setting(sqlite3.connect(qdir / "dispatch.db"))
    assert (st.model, st.night_model) == ("haiku", None)
    # the next writer's opener adds the column (NULL on the old row); a night launch then runs the day's model
    reopened = CoordStore(qdir)
    assert reopened.coord_setting().night_model is None
    assert _night_start(reopened).model == "haiku"
    reopened.coord_set_agent("claude", cwd=str(workdir.resolve()), night_model="opus")
    assert reopened.coord_setting().night_model == "opus"


def test_a_day_launch_passes_the_model_whatever_the_night_model_is(store, fake, live_hook):
    d, _ = store.accept_drop("fix the build", surface="cli")
    day = store.woken_launch(d.drop_id)
    assert store.launch(day).model == "haiku"
    assert cl.launch_session(store, day).started
    (argv,) = fake.spawns()
    assert _argv_model(argv) == "haiku"


def test_the_night_launch_argv_carries_the_night_model(tmp_path, workdir, fake, live_hook):
    q = tmp_path / "nightq"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()), night_model="opus")
    ln = _night_start(s)
    assert ln.model == "opus" and ln.role == ROLE_DESK
    assert cl.launch_session(s, ln.launch_id).started
    (argv,) = fake.spawns()
    assert _argv_model(argv) == "opus"


def test_a_night_launch_with_no_night_model_runs_the_day_model(tmp_path, workdir, fake, live_hook):
    q = tmp_path / "nightq"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    ln = _night_start(s)
    assert ln.model == "haiku"
    assert cl.launch_session(s, ln.launch_id).started
    assert _argv_model(fake.spawns()[0]) == "haiku"


def test_levain_desk_agent_sets_the_night_model_and_refuses_an_unsafe_one(tmp_home, qdir, fake, capsys):
    out = _main(qdir, capsys, "coord", "agent", "claude", "--model", "haiku", "--night-model", "opus")
    assert "night desk model: opus" in out.out
    assert CoordStore(qdir).coord_setting().night_model == "opus"
    bad = _main(qdir, capsys, "coord", "agent", "claude", "--night-model=-bad", rc=1)
    assert "letters, digits" in bad.err + bad.out
    assert CoordStore(qdir).coord_setting().night_model == "opus"
    # the same verb with no --night-model keeps it
    _main(qdir, capsys, "coord", "agent", "claude", "--model", "sonnet")
    assert CoordStore(qdir).coord_setting().night_model == "opus"


# --- L3 r1 fix round -------------------------------------------------------------------------------------------
def test_wake_night_on_a_done_idle_desk_replaces_it_with_the_night_model(store, qdir, fake, live_hook, capsys):
    from datetime import datetime, timedelta, timezone

    from tests.test_desk_launch import _released_and_done, started   # noqa: F401
    t = [datetime.now(timezone.utc)]
    store._clock = lambda: t[0]
    _released_and_done(store, fake, t)
    assert store.coord_status()["wake"] == "idle"
    out = _main(qdir, capsys, "coord", "wake", "--night")
    assert lease.REPLACED in out.out
    assert [_argv_model(a) for a in fake.spawns()] == ["haiku", "opus"]      # the replacement is the night's
    # a plain wake of the same shape stays the day's
    t[0] += timedelta(minutes=3)
    fake.flag("done")
    token = store.coord_claim(store.launches_active()[-1].launch_id, store.launches_active()[-1].session_key)
    store.coord_release(token, by=ROLE_DESK)
    t[0] += timedelta(minutes=3)
    _main(qdir, capsys, "coord", "wake")
    assert _argv_model(fake.spawns()[-1]) == "haiku"


def test_agent_user_scope_json_carries_the_night_model(tmp_home, qdir, fake, capsys, workdir, live_hook):
    import json

    _main(qdir, capsys, "coord", "agent", "claude", "--model", "haiku", "--night-model", "opus",
          "--cwd", str(workdir.resolve()))
    out = _main(qdir, capsys, "coord", "agent", "--user-scope", "off", "--json")
    assert json.loads(out.out)["night_model"] == "opus"


def test_a_night_launch_with_no_day_model_runs_the_night_model_and_none_stays_none(tmp_path, workdir):
    q = tmp_path / "nightq"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_set_agent("claude", cwd=str(workdir.resolve()), night_model="opus")      # no day model
    assert _night_start(s).model == "opus"
    q2 = tmp_path / "nightq2"
    q2.mkdir(mode=0o700)
    s2 = CoordStore(q2)
    s2.coord_set_agent("claude", cwd=str(workdir.resolve()))                         # neither
    assert _night_start(s2).model is None
