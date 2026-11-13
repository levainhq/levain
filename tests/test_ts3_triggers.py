"""TS-3: the session-start germinate trigger (both adapters) and the doctor's overdue-agenda row."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from anneal_memory import IntentionStore

from levain import doctor
from levain.intentions import guarded_intention_store

_T = Path(__file__).resolve().parents[1] / "levain" / "templates"
_HOOK_DIRS = {"claude_code": _T / "activation" / "hooks", "codex": _T / "adapters" / "codex" / "activation" / "hooks"}
TODAY = date(2026, 11, 7)
_PAST = (date.today() - timedelta(days=30)).isoformat()   # relative to now: never time-bombed


def _load(agent: str):
    d = _HOOK_DIRS[agent]
    sys.modules.pop("_levain_hook", None)
    sys.modules.pop("automemory_mirror", None)
    sys.path.insert(0, str(d))
    try:
        spec = importlib.util.spec_from_file_location(f"_ss_{agent}", d / "session_start.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(d))
    return mod


def _run(mod, monkeypatch, capsys, intentions, tmp_path, rc=None):
    h = mod.hook
    spawned: list[list[str]] = []
    kw_seen: list[dict] = []
    _run.kw = kw_seen
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: (spawned.append(argv), kw_seen.append(kw), _Child(rc))[2])
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    monkeypatch.setattr(h, "should_fire", lambda: True)
    monkeypatch.setattr(h, "open_agendas", lambda *a, **k: intentions)
    monkeypatch.setattr(h, "wrap_state", lambda timeout=None: (0, False), raising=False)
    for opt in ("open_intentions", "due_dormant_intentions"):
        monkeypatch.setattr(h, opt, lambda *a, **k: [], raising=False)
    for opt in ("compat_drift", "pack_drift"):
        monkeypatch.setattr(h, opt, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(mod, "_T0", 0.0, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "startup"})))
    capsys.readouterr()
    assert mod.main() == 0
    out = capsys.readouterr().out.strip()
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
    return [a for a in spawned if "germinate" in a], ctx


def _agenda(nxt, **kw):
    return {"id": "intention-001", "disposition": "agenda", "next": nxt, "text": "x", **kw}


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_one_due_agenda_spawns_once_and_prints_the_line(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys, [_agenda(_PAST), _agenda("2999-01-01"), _agenda(None)], tmp_path)
    assert len(spawned) == 1 and spawned[0][-2:] == ["--agent", agent] and "--path" in spawned[0]
    assert "[germinate] 1 agenda came due" in ctx


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_none_due_spawns_nothing_and_prints_nothing(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys,
                        [_agenda("2999-01-01"), _agenda("2026-01-01", germinated={"on": "2026-01-02"}),
                         _agenda("2999-01-01T09:00"), _agenda("garbage"), {"disposition": "seed", "next": _PAST}],
                        tmp_path)
    assert spawned == [] and "[germinate]" not in ctx


def _install(tmp_path) -> Path:
    (tmp_path / ".levain").mkdir()
    return tmp_path


def _add(root, nxt, disposition="agenda"):
    path = root / ".levain" / "memory.spores.json"
    day = nxt[:10]
    s = guarded_intention_store(path).add(type="thought", text="call him", disposition=disposition, next=day)
    if nxt != day:      # anneal's write path takes a date only; a timed next is stored by hand (as a hand edit would)
        doc = json.loads(path.read_text())
        for row in (doc["intentions"] if isinstance(doc, dict) else doc):
            if row["id"] == s["id"]:
                row["next"] = nxt
        path.write_text(json.dumps(doc))
    return s


def test_overdue_agenda_warns_on_a_past_ungerminated_agenda(tmp_path):
    root = _install(tmp_path)
    s = _add(root, "2026-11-05")
    rows = doctor._check_overdue_agenda(root, today=TODAY)
    assert len(rows) == 1 and rows[0].ok and rows[0].warn and s["id"] in rows[0].detail


def test_overdue_agenda_silent_when_today_future_or_not_an_agenda(tmp_path):
    root = _install(tmp_path)
    _add(root, "2026-11-07")
    _add(root, "2026-11-07T00:01")
    _add(root, "2026-11-20")
    _add(root, "2026-11-01", disposition="someday")
    assert doctor._check_overdue_agenda(root, today=TODAY) == []


def test_overdue_agenda_silent_with_no_intentions_file(tmp_path):
    assert doctor._check_overdue_agenda(tmp_path, today=TODAY) == []


def test_overdue_agenda_a_timed_agenda_from_yesterday_is_overdue(tmp_path):
    root = _install(tmp_path)
    _add(root, "2026-11-06T09:30")
    assert len(doctor._check_overdue_agenda(root, today=TODAY)) == 1


class _Child:
    """A stand-in Popen: rc None = still running after the grace (wait times out), an int = exited with it."""
    def __init__(self, rc):
        self.rc = rc

    def wait(self, timeout=None):
        import subprocess
        if self.rc is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.rc


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_child_that_dies_at_once_prints_a_failure_line_not_a_success(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path, rc=1)
    assert len(spawned) == 1 and "did not start" in ctx and "germination started" not in ctx


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_live_child_says_started_and_runs_from_the_install_root(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path)
    assert "germination started" in ctx and _run.kw[0]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_two_starts_within_a_minute_spawn_once(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    s1, c1 = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path)
    s2, c2 = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path)
    assert len(s1) == 1 and s2 == []
    assert "already started" in c2 and "did not start" not in c2


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_popen_raising_prints_a_failure_line_and_leaves_no_marker(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)

    def boom(*a, **k):
        raise OSError("no exec")
    spawned, ctx = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path)   # wires everything, one ok spawn
    (tmp_path / ".levain" / ".germinate-spawn").unlink()
    monkeypatch.setattr(mod.hook.subprocess, "Popen", boom)
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "startup"})))
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "did not start" in out and "germination started" not in out
    assert not (tmp_path / ".levain" / ".germinate-spawn").exists()


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_failed_start_does_not_poison_the_next_one(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    s1, c1 = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path, rc=1)
    assert "did not start" in c1 and not (tmp_path / ".levain" / ".germinate-spawn").exists()
    s2, c2 = _run(mod, monkeypatch, capsys, [_agenda(_PAST)], tmp_path)
    assert len(s2) == 1 and "germination started" in c2   # retried, not busy


# hook and doctor agree on what a valid next/agenda is: same table through both
_PARITY = [
    ("agenda", "2026-11-05"), ("Agenda ", "2026-11-05"), ("agenda", "2026-11-05T09:30"),
    ("agenda", "2026-11-05T09:30:00"), ("agenda", "2026-11-05 09:30"), ("agenda", "2026-11-05T09:30Z"),
    ("agenda", "2026-13-01"), ("agenda", "2026-11-05 note"), ("agenda", " 2026-11-05"), ("agenda", None),
    ("seed", "2026-11-05"),
]


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_hook_and_doctor_agree_on_which_agendas_are_due(agent, tmp_path):
    h = _load(agent).hook
    root = _install(tmp_path)
    rows = [{"id": f"intention-{i:03d}", "disposition": d, "next": n, "text": "x", "type": "thought", "status": "open"}
            for i, (d, n) in enumerate(_PARITY)]
    path = root / ".levain" / "memory.spores.json"
    path.write_text(json.dumps({"schema_version": 2, "intentions": rows}))
    hook_due = {s["id"] for s in h.due_agendas(rows, now=datetime(2026, 11, 7, 12, 0))}
    rep = doctor._check_overdue_agenda(root, today=TODAY)
    doc_due = {r["id"] for r in rows if rep and r["id"] in rep[0].detail}
    assert hook_due == doc_due and hook_due       # every past valid one is due in both; every malformed one in neither


def test_one_malformed_intention_is_skipped_not_the_row(tmp_path, monkeypatch):
    root = _install(tmp_path)
    (root / ".levain" / "memory.spores.json").write_text("{}")
    good = {"id": "intention-001", "disposition": "agenda", "next": "2026-11-05"}

    monkeypatch.setattr("levain.intentions.list_open_readonly", lambda p, today=None: [object(), good])
    rows = doctor._check_overdue_agenda(root, today=TODAY)
    assert len(rows) == 1 and rows[0].ok and "intention-001" in rows[0].detail


def test_a_datetime_today_does_not_crash_the_row(tmp_path):
    root = _install(tmp_path)
    _add(root, "2026-11-05")
    assert len(doctor._check_overdue_agenda(root, today=datetime(2026, 11, 7, 3, 0))) == 1


# --- an agenda that waits on an event (a split's second part): the hook and doctor count it as due too ---------------
def _event_rig(tmp_path):
    """One install, a real store: open and finished first parts, and second parts in every state the due rule reads."""
    root = _install(tmp_path)
    st = guarded_intention_store(root / ".levain" / "memory.spores.json")
    kw = dict(type="task", disposition="agenda", target="band")
    open_first = st.add(text="open first", **kw)["id"]
    done_first = st.add(text="done first", **kw)["id"]
    st.descend(done_first, kind="done")
    ev = {}
    ev["waiting"] = st.add(text="waits", on_event=f"done:{open_first}", check_by="2026-11-20", **kw)["id"]
    ev["check_by_passed"] = st.add(text="ring", on_event=f"done:{open_first}", check_by="2026-11-06", **kw)["id"]
    ev["first_done"] = st.add(text="woke", on_event=f"done:{done_first}", check_by="2026-11-20", **kw)["id"]
    ev["stamped"] = st.add(text="stamped", on_event=f"done:{done_first}", check_by="2026-11-06",
                           germinated={"on": "2026-11-06"}, **kw)["id"]
    ev["no_check_by_old"] = st.add(text="old", on_event=f"done:{open_first}", today=date(2026, 10, 20), **kw)["id"]
    ev["no_check_by_new"] = st.add(text="new", on_event=f"done:{open_first}", today=date(2026, 11, 3), **kw)["id"]
    ev["other_key_check_by_passed"] = st.add(text="landed", on_event="landed:np-1", check_by="2026-11-06", **kw)["id"]
    ev["dated_and_event_is_not_dated"] = st.add(text="both", on_event=f"done:{open_first}", check_by="2026-11-20", next="2026-11-01", **kw)["id"]
    return root, st, ev


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_hook_and_doctor_agree_on_which_event_agendas_are_due(agent, tmp_path):
    import re

    h = _load(agent).hook
    root, st, ev = _event_rig(tmp_path)
    rows = st.list_open(today=TODAY)
    hook_due = {s["id"] for s in h.due_agendas(rows, now=datetime(2026, 11, 7, 12, 0))}
    rep = doctor._check_overdue_agenda(root, today=TODAY)
    doc_due = set(re.findall(r"(intention-\d+) \(", rep[0].detail)) if rep else set()
    expected = {ev["check_by_passed"], ev["first_done"], ev["no_check_by_old"], ev["other_key_check_by_passed"]}
    assert hook_due == doc_due == expected and rep[0].warn


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_first_part_that_is_gone_is_worth_a_run_to_the_hook_and_not_to_doctor(agent, tmp_path):
    """The hook sees open intentions only, so it cannot tell a composted first part from a finished one: it asks for a run (the
    run decides, and files nothing for a compost). Doctor reads the store and warns only for a finish."""
    h = _load(agent).hook
    root, st, ev = _event_rig(tmp_path)
    composted = st.add(type="task", text="c", disposition="agenda", target="band")["id"]
    st.descend(composted, kind="composted")
    follower = st.add(type="task", text="after c", disposition="agenda", target="band", on_event=f"done:{composted}", check_by="2026-11-20")["id"]
    assert follower in {s["id"] for s in h.due_agendas(st.list_open(today=TODAY), now=datetime(2026, 11, 7, 12, 0))}
    rep = doctor._check_overdue_agenda(root, today=TODAY)
    assert follower not in rep[0].detail


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_finished_first_part_makes_the_session_start_hook_spawn_germinate(agent, monkeypatch, capsys, tmp_path):
    """The reproduced failure: a part whose first part was finished by any path waited for an unrelated dated agenda."""
    mod = _load(agent)
    root = _install(tmp_path)
    st = guarded_intention_store(root / ".levain" / "memory.spores.json")
    first = st.add(type="task", text="call Jim", disposition="agenda", target="band")["id"]
    st.add(type="task", text="send the invoice", disposition="agenda", target="band", on_event=f"done:{first}",
           check_by=(date.today() + timedelta(days=5)).isoformat())
    spawned, ctx = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert spawned == [] and "[germinate]" not in ctx          # the first part is open: nothing to run
    st.descend(first, kind="done")
    spawned, ctx = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(spawned) == 1 and "[germinate] 1 agenda came due" in ctx


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_hooks_event_pattern_is_levains(agent):
    from levain import intentions

    assert _load(agent).hook._EVENT_DONE_RE.pattern == intentions._EVENT_DONE.pattern


# --- a part past its check-by whose first part is still open: one run a day, not one per session start -------------------
def _ring_rig(tmp_path):
    """An open first part and a second part waiting on it whose check-by is past: the run files the band question and leaves the
    follower unstamped (the shape the session-start hook used to spawn for at every start)."""
    import os
    import time

    root = _install(tmp_path)
    st = guarded_intention_store(root / ".levain" / "memory.spores.json")
    kw = dict(type="task", disposition="agenda", target="band")
    anchor = st.add(text="call Jim", **kw)["id"]
    follower = st.add(text="send the invoice", on_event=f"done:{anchor}", check_by=_PAST, **kw)["id"]

    def an_hour_later():     # the busy marker spares only starts within a minute: the next start is past it
        marker = root / ".levain" / ".germinate-spawn"
        if marker.exists():
            os.utime(marker, (time.time() - 3600, time.time() - 3600))
    return root, st, anchor, follower, an_hour_later


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_check_by_only_part_is_spawned_for_once_a_day(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    s1, c1 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s1) == 1 and "[germinate] 1 agenda came due; germination started" in c1
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert s2 == [] and "[germinate]" not in c2          # the run for it already ran today; it filed the one question


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_next_day_asks_for_one_more_run(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    rec = root / ".levain" / ".germinate-ringed"
    doc = json.loads(rec.read_text())
    assert doc["ids"] == [follower] and doc["day"] == date.today().isoformat()
    rec.write_text(json.dumps({**doc, "day": (date.today() - timedelta(days=1)).isoformat()}))
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s2) == 1 and "[germinate] 1 agenda came due" in c2     # a question still open tomorrow is a pending answer, asked once


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_finished_first_part_is_never_held_back_by_the_days_record(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    st.descend(anchor, kind="done")
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s2) == 1 and "[germinate] 1 agenda came due" in c2     # a genuinely due part: the wake must happen today


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_start_that_failed_records_nothing_and_is_retried(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    s1, c1 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path, rc=1)
    assert len(s1) == 1 and "did not start" in c1 and not (root / ".levain" / ".germinate-ringed").exists()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s2) == 1 and "germination started" in c2


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_an_unreadable_record_asks_for_the_run_as_before(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    (root / ".levain" / ".germinate-ringed").write_text("{not json")
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s2) == 1


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_dated_agenda_still_spawns_beside_a_held_back_part(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    dated = st.add(type="task", text="renew the domain", disposition="agenda", target="band", next=_PAST)["id"]
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert len(s2) == 1 and "[germinate] 1 agenda came due" in c2       # the dated one only: the held-back part is not counted


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_due_agendas_default_still_counts_every_due_part(agent, tmp_path):
    h = _load(agent).hook
    root, st, anchor, follower, _ = _ring_rig(tmp_path)
    rows = st.list_open()
    now = datetime.now()
    assert {s["id"] for s in h.due_agendas(rows, now=now)} == {follower}
    assert h.due_agendas(rows, now=now, ringed={follower}) == []
    assert h.ring_only_ids(h.due_agendas(rows, now=now), rows) == [follower]
    st.descend(anchor, kind="done")
    dated = st.add(type="task", text="renew the domain", disposition="agenda", target="band", next=_PAST)["id"]
    rows = st.list_open()
    due = h.due_agendas(rows, now=now)
    assert {s["id"] for s in due} == {follower, dated}
    assert h.ring_only_ids(due, rows) == []          # a finished first part and a dated agenda are woken by a run, never held back


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_run_that_finished_at_once_cleanly_is_recorded_too(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    root, st, anchor, follower, an_hour_later = _ring_rig(tmp_path)
    s1, c1 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path, rc=0)       # the child exited 0 inside the grace
    assert len(s1) == 1 and "germination started" in c1
    an_hour_later()
    s2, c2 = _run(mod, monkeypatch, capsys, st.list_open(), tmp_path)
    assert s2 == [] and "[germinate]" not in c2


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_ids_recorded_are_the_ones_the_spawn_was_handed(agent, monkeypatch, tmp_path):
    """`start_germinate` records what it is given, not what some later `due_agendas` call left behind."""
    h = _load(agent).hook
    root, st, anchor, follower, _ = _ring_rig(tmp_path)
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: _Child(None))
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    rows = st.list_open()
    ids = h.ring_only_ids(h.due_agendas(rows), rows)
    assert ids == [follower]
    h.due_agendas([])                                  # a second call between the two: it must change nothing recorded
    assert h.start_germinate(agent, ids) == "started"
    assert json.loads((root / ".levain" / ".germinate-ringed").read_text())["ids"] == [follower]


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_spawn_handed_no_ids_records_nothing(agent, monkeypatch, tmp_path):
    h = _load(agent).hook
    root, st, anchor, follower, _ = _ring_rig(tmp_path)
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: _Child(None))
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    h.due_agendas(st.list_open())                      # a call that found a ring-only part, then a spawn that was not told
    assert h.start_germinate(agent) == "started"
    assert not (root / ".levain" / ".germinate-ringed").exists()


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_day_record_is_written_by_a_replace_and_leaves_no_temp_file(agent, monkeypatch, tmp_path):
    h = _load(agent).hook
    (tmp_path / ".levain").mkdir()
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    real, seen = h.os.replace, []

    def spy(src, dst):
        seen.append((Path(src), Path(dst)))
        assert not Path(dst).exists() or json.loads(Path(dst).read_text())     # the target is never a torn file at the swap
        return real(src, dst)

    monkeypatch.setattr(h.os, "replace", spy)
    h.note_germinate_ringed(["intention-1"])
    h.note_germinate_ringed(["intention-2"])
    rec = tmp_path / ".levain" / ".germinate-ringed"
    assert [d for _, d in seen] == [rec, rec] and all(s.parent == rec.parent and s != rec for s, _ in seen)
    assert json.loads(rec.read_text())["ids"] == ["intention-1", "intention-2"]
    assert sorted(p.name for p in rec.parent.iterdir()) == [".germinate-ringed"]


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_failed_swap_keeps_the_old_record_and_leaves_no_temp_file(agent, monkeypatch, tmp_path):
    h = _load(agent).hook
    (tmp_path / ".levain").mkdir()
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    h.note_germinate_ringed(["intention-1"])
    rec = tmp_path / ".levain" / ".germinate-ringed"
    before = rec.read_text()

    def refuse(src, dst):
        raise OSError("no")

    monkeypatch.setattr(h.os, "replace", refuse)
    h.note_germinate_ringed(["intention-2"])           # best effort: it neither raises nor damages the record
    assert rec.read_text() == before and sorted(p.name for p in rec.parent.iterdir()) == [".germinate-ringed"]
