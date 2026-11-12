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
