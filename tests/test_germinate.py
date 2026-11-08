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
    assert "crash" in _run(root, qdir)["errors"][0]["error"]      # isolated: recorded, never raised
    monkeypatch.setattr(type(guarded_spore_store(_spores(root))), "descend", real)
    assert len(pings) == 1
    res = _run(root, qdir)           # stamped but not composted: finished, nothing filed or pinged again
    assert [o["spore_id"] for o in res["finish"]] == [s["id"]] and res["due"] == []
    assert len(pings) == 1 and len(_rows(qdir)) == 1 and s["id"] not in _open_ids(root)


def test_nudge_crash_between_filing_and_stamp_reruns_into_one_row_and_pings_again(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "call the clinic", target="nudge")
    cls = type(guarded_spore_store(_spores(root)))
    real = cls.update
    monkeypatch.setattr(cls, "update", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    assert "crash" in _run(root, qdir)["errors"][0]["error"]      # isolated: recorded, never raised
    monkeypatch.setattr(cls, "update", real)
    assert len(pings) == 1 and s["id"] in _open_ids(root)
    (o,) = _run(root, qdir)["due"]         # due again: the held row is returned (filed False); the ping is at-least-once
    assert not o["filed"] and o["pinged"] and o["done"]
    assert len(pings) == 2 and len(_rows(qdir)) == 1


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
    assert "crash" in _run(root, qdir)["errors"][0]["error"]      # isolated: recorded, never raised
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
    assert "crash" in _run(root, qdir)["errors"][0]["error"]      # isolated: recorded, never raised
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


# --- TS-2 L3 round: the findings that clear the reachability line ---------------------------------------------------
from levain.dispatch.jobs import JobStore  # noqa: E402


def _store_cls(root):
    return type(guarded_spore_store(_spores(root)))


def test_a_failed_ping_is_surfaced_and_the_spore_stays_open_and_a_rerun_retries(tmp_path, qdir, monkeypatch, capsys):
    root = _install(tmp_path)
    s = _add(root, "call the clinic", target="nudge")
    sent: list[str] = []
    ok = {"v": False}
    monkeypatch.setattr(band, "_send_ping", lambda agent, q, qdir, extra="", kind="question": sent.append(q.id) or ok["v"])
    rc = main(["spores", "germinate", "--json", "--path", str(root), "--queue-dir", str(qdir)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["due"] == []
    (e,) = out["errors"]
    assert e["ping_failed"] is True and e["spore_id"] == s["id"] and e["ref"]
    assert s["id"] in _open_ids(root) and len(_rows(qdir)) == 1 and not _spore(root, s["id"]).get("germinated")
    ok["v"] = True                                   # the channel is back: the same row, pinged now, then composted
    rc = main(["spores", "germinate", "--path", str(root), "--queue-dir", str(qdir)])
    assert rc == 0 and len(sent) == 2 and len(_rows(qdir)) == 1 and s["id"] not in _open_ids(root)
    main(["spores", "germinate", "--path", str(root), "--queue-dir", str(qdir)])
    assert len(sent) == 2


def test_a_ping_that_raises_is_surfaced_the_same_way(tmp_path, qdir, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "call the clinic", target="nudge")

    def boom(*a, **k):
        raise OSError("push backend off")

    res = _run(root, qdir, ping=boom)
    (e,) = res["errors"]
    assert e["ping_failed"] and "push backend off" in e["error"] and s["id"] in _open_ids(root)


def test_one_bad_spore_does_not_stop_the_others_and_the_cli_prints_what_completed(tmp_path, qdir, pings, monkeypatch,
                                                                                    capsys):
    root = _install(tmp_path)
    bad = _add(root, "bad one")
    good = _add(root, "good one")
    real = QuestionStore.file_once

    def picky(self, **kw):
        if kw["key"].endswith(bad["origin_key"]):
            raise RuntimeError("store exploded")
        return real(self, **kw)

    monkeypatch.setattr(QuestionStore, "file_once", picky)
    rc = main(["spores", "germinate", "--path", str(root), "--queue-dir", str(qdir)])
    cap = capsys.readouterr()
    assert rc == 1 and f"germinated {good['id']}" in cap.out and "store exploded" in cap.err and bad["id"] in cap.err
    assert bad["id"] in _open_ids(root) and good["id"] not in _open_ids(root)


def test_a_spore_with_no_origin_key_is_reported_not_fatal(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    good = _add(root, "good one")
    keyless = {"id": "spore-099", "text": "no key", "disposition": "agenda", "next": PAST, "status": "open"}
    real = _store_cls(root).list_open
    monkeypatch.setattr(_store_cls(root), "list_open", lambda self, **k: [keyless, *real(self, **k)])
    res = _run(root, qdir)
    assert [o["spore_id"] for o in res["due"]] == [good["id"]]
    assert res["errors"][0]["spore_id"] == "spore-099" and "origin_key" in res["errors"][0]["error"]


def test_a_missing_install_is_refused_with_a_reason_not_nothing_due(tmp_path, qdir, capsys):
    with pytest.raises(g.GerminateRefused):
        g.germinate(tmp_path / "nope" / ".levain" / "memory.spores.json", qdir, install=tmp_path, today=TODAY)
    rc = main(["spores", "germinate", "--path", str(tmp_path / "nope"), "--queue-dir", str(qdir)])
    cap = capsys.readouterr()
    assert rc == 2 and "refused" in cap.err and "memory.spores.json" in cap.err and "no agenda is due" not in cap.out


def test_a_dispatch_only_database_is_not_a_board(tmp_path, qdir):
    root = _install(tmp_path)
    s = _add(root, "write the release notes", target="board:levain")
    JobStore(qdir).event_cursor()
    assert (qdir / "dispatch.db").exists() and not CoordStore(qdir).coordinator_present()
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "band-fallback" and o["ref"].startswith("q-") and s["id"] not in _open_ids(root)
    assert not _drops_table_exists(qdir)


def _drops_table_exists(qdir) -> bool:
    with sqlite3.connect(qdir / "dispatch.db") as c:
        return c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'drops'").fetchone()[0] > 0


def test_a_spore_edited_while_it_germinates_is_skipped_not_composted_on_the_old_read(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "write the notes")
    real = QuestionStore.file_once

    def edit_after_filing(self, **kw):
        out = real(self, **kw)
        guarded_spore_store(_spores(root)).update(s["id"], text="publish the hotfix instead")
        return out

    monkeypatch.setattr(QuestionStore, "file_once", edit_after_filing)
    res = _run(root, qdir)
    assert res["due"] == [] and res["errors"] == []
    (sk,) = res["skipped"]
    assert sk["spore_id"] == s["id"] and "left open" in sk["reason"]
    got = _spore(root, s["id"])
    assert got["status"] == "open" and got["text"] == "publish the hotfix instead" and not got.get("germinated")


def test_a_board_spore_edited_after_its_drop_is_never_stamped_with_the_old_drop(tmp_path, qdir, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "write the release notes", target="board:levain")
    _make_board(qdir, tmp_path)
    cls = _store_cls(root)
    real = cls.update
    monkeypatch.setattr(cls, "update", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    _run(root, qdir)
    monkeypatch.setattr(cls, "update", real)
    guarded_spore_store(_spores(root)).update(s["id"], text="publish the hotfix")
    res = _run(root, qdir)
    assert res["due"] == [] and "DropConflict" in res["errors"][0]["error"]
    got = _spore(root, s["id"])
    assert got["status"] == "open" and not got.get("germinated") and _drops(qdir) == 1


def test_a_dry_run_on_a_machine_with_no_band_creates_no_store(tmp_path, qdir):
    root = _install(tmp_path)
    due = _add(root, "one")
    res = _run(root, qdir, dry_run=True)
    assert [o["spore_id"] for o in res["due"]] == [due["id"]]
    assert not (qdir / "questions.db").exists() and not (qdir / "dispatch.db").exists()


def test_a_leading_blank_line_does_not_empty_the_title_and_the_target_word_ignores_case(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "\n\nrenew the domain", target="Nudge")
    (o,) = _run(root, qdir)["due"]
    assert o["via"] == "nudge" and len(pings) == 1
    (q,) = _rows(qdir)
    assert q.what == f"Due 11-05: renew the domain ({s['id']})"
