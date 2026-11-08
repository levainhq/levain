"""TS-2: ``levain spores germinate``. Temp dirs only: no ~/.levain, no real questions.db, no live app.
The ping seam is replaced; the band and the coordinator are the real stores in a temp consent dir."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from anneal_memory import SporeStore

from levain import germinate as g
from levain.cli import main
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.coordinator.store import CoordStore
from levain.spores import guarded_spore_store

TODAY = date(2026, 11, 7)
PAST, FUTURE = "2026-11-05", "2026-11-09"


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "home" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def pings(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(band, "_send_ping", lambda agent, q, qdir, extra="", kind="question": sent.append(q.id) or True)
    return sent


def _install(tmp_path, name="a") -> Path:
    root = tmp_path / name
    (root / ".levain").mkdir(parents=True)
    return root


def _spores(root) -> Path:
    return root / ".levain" / "memory.spores.json"


def _add(root, text, nxt=PAST, target=None, disposition="agenda", **kw):
    store = guarded_spore_store(_spores(root))
    extra = {"target": target} if target else {}
    return store.add(type="thought", text=text, disposition=disposition, next=nxt, **extra, **kw)


def _run(root, qdir, **kw):
    return g.germinate(_spores(root), qdir, install=root, today=TODAY, **kw)


def _rows(qdir):
    return QuestionStore(qdir).recent(100)


def _spore(root, sid):
    return SporeStore(_spores(root)).get(sid)


def _open_ids(root):
    return {s["id"] for s in SporeStore(_spores(root)).list_open()}


def _sha(*paths):
    return [hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths]


def _drops(qdir) -> int:
    path = qdir / "dispatch.db"
    with sqlite3.connect(path) as c:
        return c.execute("SELECT count(*) FROM drops").fetchone()[0]


def _make_board(qdir, tmp_path):
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))


# --- band -------------------------------------------------------------------------------------------------------
def test_band_files_one_quiet_row_stamps_and_composts(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "renew the domain\nmore words")
    res = _run(root, qdir)
    (o,) = res["due"]
    assert o["via"] == "band" and o["filed"] and not o["pinged"] and o["done"]
    (q,) = _rows(qdir)
    assert q.id == o["ref"]
    assert q.what.startswith("Due 11-05: renew the domain") and f"({s['id']})" in q.what
    assert [x[0] for x in q.options] == ["done"] and q.rec == "done"
    assert q.agent_session_id == g.KEY_PREFIX + s["origin_key"]
    assert pings == []
    got = _spore(root, s["id"])
    assert got["status"] != "open" and got["germinated"]["ref"] == q.id
    assert got["germinated"] == {"on": "2026-11-07", "target": "band", "ref": q.id}
    assert s["id"] not in _open_ids(root)
    assert _run(root, qdir)["due"] == [] and len(_rows(qdir)) == 1


# --- nudge ------------------------------------------------------------------------------------------------------
def test_nudge_pings_exactly_once_over_two_runs(tmp_path, qdir, pings):
    root = _install(tmp_path)
    _add(root, "call the clinic", target="nudge")
    first = _run(root, qdir)["due"]
    assert first[0]["pinged"] and first[0]["via"] == "nudge" and len(pings) == 1
    _run(root, qdir)
    assert len(pings) == 1 and len(_rows(qdir)) == 1


def test_nudge_after_a_crash_before_compost_does_not_ping_again(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "call the clinic", target="nudge")
    real = type(guarded_spore_store(_spores(root))).descend

    def boom(self, *a, **k):
        raise RuntimeError("crash")

    monkeypatch.setattr(type(guarded_spore_store(_spores(root))), "descend", boom)
    with pytest.raises(RuntimeError):
        _run(root, qdir)
    monkeypatch.setattr(type(guarded_spore_store(_spores(root))), "descend", real)
    assert len(pings) == 1
    res = _run(root, qdir)           # stamped but not composted: finished, nothing filed or pinged again
    assert [o["spore_id"] for o in res["finish"]] == [s["id"]] and res["due"] == []
    assert len(pings) == 1 and len(_rows(qdir)) == 1 and s["id"] not in _open_ids(root)


def test_nudge_crash_between_filing_and_stamp_reruns_into_one_row_and_one_ping(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "call the clinic", target="nudge")
    cls = type(guarded_spore_store(_spores(root)))
    real = cls.update
    monkeypatch.setattr(cls, "update", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _run(root, qdir)
    monkeypatch.setattr(cls, "update", real)
    assert len(pings) == 1 and s["id"] in _open_ids(root)
    (o,) = _run(root, qdir)["due"]         # due again: the held row is returned, filed False, so no second ping
    assert not o["filed"] and not o["pinged"] and o["done"]
    assert len(pings) == 1 and len(_rows(qdir)) == 1


# --- board ------------------------------------------------------------------------------------------------------
def test_board_with_a_coordinator_store_makes_one_drop_over_two_runs(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "write the release notes", target="board:levain")
    _make_board(qdir, tmp_path)
    before = _drops(qdir)
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "board" and o["filed"] and o["done"]
    assert _drops(qdir) == before + 1
    drop = CoordStore(qdir).drop_by_outbox(g.outbox_id_for(s["origin_key"]))
    assert drop is not None and drop.drop_id == o["ref"] and drop.words == "write the release notes"
    assert _rows(qdir) == [] and pings == []
    assert _spore(root, s["id"])["germinated"]["ref"] == drop.drop_id
    _run(root, qdir)
    assert _drops(qdir) == before + 1


def test_board_crash_between_drop_and_compost_gives_one_drop(tmp_path, qdir, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "write the release notes", target="board:levain")
    _make_board(qdir, tmp_path)
    before = _drops(qdir)
    cls = type(guarded_spore_store(_spores(root)))
    real = cls.update
    monkeypatch.setattr(cls, "update", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _run(root, qdir)
    monkeypatch.setattr(cls, "update", real)
    assert _drops(qdir) == before + 1 and s["id"] in _open_ids(root)
    (o,) = _run(root, qdir)["due"]
    assert not o["filed"] and o["done"] and _drops(qdir) == before + 1


def test_board_without_a_coordinator_falls_back_to_the_band(tmp_path, qdir):
    root = _install(tmp_path)
    s = _add(root, "write the release notes", target="board:levain")
    assert not (qdir / "dispatch.db").exists()
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "band" or o["via"] == "band-fallback"
    (q,) = _rows(qdir)
    assert "levain" in q.blocks and "no board" in q.blocks and q.id == o["ref"]
    assert not (qdir / "dispatch.db").exists()
    assert s["id"] not in _open_ids(root)


# --- adoption ---------------------------------------------------------------------------------------------------
def test_an_open_question_named_in_the_text_is_adopted_and_nothing_is_filed(tmp_path, qdir):
    root = _install(tmp_path)
    held, _ = QuestionStore(qdir).file_once(agent="claude_code", key="other", what="held", options=[("a", "A")],
                                            rec="a", blocks="b", cwd=str(tmp_path.resolve()))
    s = _add(root, f"follow up on {held.id} please")
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "adopted" and o["ref"] == held.id and not o["filed"] and o["done"]
    assert len(_rows(qdir)) == 1 and s["id"] not in _open_ids(root)


def test_an_open_question_named_in_a_note_is_adopted(tmp_path, qdir):
    root = _install(tmp_path)
    held, _ = QuestionStore(qdir).file_once(agent="claude_code", key="other", what="held", options=[("a", "A")],
                                            rec="a", blocks="b", cwd=str(tmp_path.resolve()))
    s = _add(root, "follow up")
    guarded_spore_store(_spores(root)).update(s["id"], add_note=f"asked as {held.id}")
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "adopted" and o["ref"] == held.id


def test_a_question_id_that_is_not_open_is_not_adopted(tmp_path, qdir):
    root = _install(tmp_path)
    _add(root, "follow up on q-" + "0" * 24)
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "band" and o["filed"]


# --- the due test -----------------------------------------------------------------------------------------------
def test_an_undated_agenda_a_future_one_and_non_agendas_are_never_due(tmp_path, qdir):
    root = _install(tmp_path)
    store = guarded_spore_store(_spores(root))
    undated = store.add(type="thought", text="someday", disposition="agenda")
    future = _add(root, "later", nxt=FUTURE)
    seed = _add(root, "a seed", disposition="seed")
    loop = guarded_spore_store(_spores(root)).add(type="thought", text="plain loop", next=PAST)
    today = _add(root, "today counts", nxt=TODAY.isoformat())
    res = _run(root, qdir)
    assert [o["spore_id"] for o in res["due"]] == [today["id"]]
    assert {undated["id"], future["id"], seed["id"], loop["id"]} <= _open_ids(root)
    assert len(_rows(qdir)) == 1


# --- crash between filing and compost ---------------------------------------------------------------------------
def test_a_crash_between_filing_and_compost_gives_one_row(tmp_path, qdir, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "renew the domain")
    cls = type(guarded_spore_store(_spores(root)))
    real = cls.update
    monkeypatch.setattr(cls, "update", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _run(root, qdir)
    monkeypatch.setattr(cls, "update", real)
    assert len(_rows(qdir)) == 1 and s["id"] in _open_ids(root)
    (o,) = _run(root, qdir)["due"]
    assert not o["filed"] and o["done"] and len(_rows(qdir)) == 1 and s["id"] not in _open_ids(root)


# --- two installs, one LEVAIN_HOME ------------------------------------------------------------------------------
def test_two_installs_with_the_same_spore_id_both_due_give_two_rows(tmp_path, qdir):
    a, b = _install(tmp_path, "a"), _install(tmp_path, "b")
    sa = _add(a, "install A agenda")
    sb = _add(b, "install B agenda")
    assert sa["id"] == sb["id"] and sa["origin_key"] != sb["origin_key"]
    _run(a, qdir)
    _run(b, qdir)
    rows = _rows(qdir)
    assert len(rows) == 2 and {q.agent_session_id for q in rows} == {g.KEY_PREFIX + sa["origin_key"],
                                                                      g.KEY_PREFIX + sb["origin_key"]}
    assert sa["id"] not in _open_ids(a) and sb["id"] not in _open_ids(b)


# --- dry run ----------------------------------------------------------------------------------------------------
def test_dry_run_json_lists_exactly_the_due_set_and_writes_nothing(tmp_path, qdir, capsys, monkeypatch):
    root = _install(tmp_path)
    due1 = _add(root, "one")
    due2 = _add(root, "two", target="nudge")
    _add(root, "later", nxt=FUTURE)
    _add(root, "undated", nxt=None)
    QuestionStore(qdir).file_once(agent="claude_code", key="seed", what="seed", options=[("a", "A")], rec="a",
                                  blocks="b", cwd=str(tmp_path.resolve()))
    spores, qdb = _spores(root), qdir / "questions.db"
    assert qdb.exists()
    before = _sha(spores, qdb)
    files_before = {p.name for p in qdir.iterdir()}
    rc = main(["spores", "germinate", "--dry-run", "--json", "--path", str(root), "--queue-dir", str(qdir)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {o["spore_id"] for o in out["due"]} == {due1["id"], due2["id"]} and len(out["due"]) == 2
    assert all(not o["done"] and not o["filed"] for o in out["due"])
    assert _sha(spores, qdb) == before
    # a read-only sqlite snapshot may leave its -wal/-shm sidecars; no other file (no dispatch.db) appears
    assert {p.name for p in qdir.iterdir()} - files_before <= {"questions.db-wal", "questions.db-shm"}


def test_the_cli_verb_runs_for_real(tmp_path, qdir, pings, capsys):
    root = _install(tmp_path)
    _add(root, "go")
    rc = main(["spores", "germinate", "--path", str(root), "--queue-dir", str(qdir)])
    assert rc == 0 and "germinated spore-" in capsys.readouterr().out and len(_rows(qdir)) == 1
