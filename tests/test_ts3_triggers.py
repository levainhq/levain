"""TS-3: the session-start germinate trigger (both adapters) and the doctor's overdue-agenda row."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from anneal_memory import SporeStore

from levain import doctor
from levain.spores import guarded_spore_store

_T = Path(__file__).resolve().parents[1] / "levain" / "templates"
_HOOK_DIRS = {"claude_code": _T / "activation" / "hooks", "codex": _T / "adapters" / "codex" / "activation" / "hooks"}
TODAY = date(2026, 11, 7)


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


def _run(mod, monkeypatch, capsys, spores, tmp_path):
    h = mod.hook
    spawned: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(h, "install_root", lambda: tmp_path)
    monkeypatch.setattr(h, "should_fire", lambda: True)
    monkeypatch.setattr(h, "open_agendas", lambda *a, **k: spores)
    monkeypatch.setattr(h, "wrap_state", lambda timeout=None: (0, False), raising=False)
    for opt in ("open_spores", "due_dormant_spores"):
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
    return {"id": "spore-001", "disposition": "agenda", "next": nxt, "text": "x", **kw}


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_one_due_agenda_spawns_once_and_prints_the_line(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys, [_agenda("2026-01-01"), _agenda("2999-01-01"), _agenda(None)], tmp_path)
    assert len(spawned) == 1 and spawned[0][-2:] == ["--agent", agent] and "--path" in spawned[0]
    assert "[germinate] 1 agenda came due" in ctx


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_none_due_spawns_nothing_and_prints_nothing(agent, monkeypatch, capsys, tmp_path):
    mod = _load(agent)
    spawned, ctx = _run(mod, monkeypatch, capsys,
                        [_agenda("2999-01-01"), _agenda("2026-01-01", germinated={"on": "2026-01-02"}),
                         _agenda("2999-01-01T09:00"), _agenda("garbage"), {"disposition": "seed", "next": "2026-01-01"}],
                        tmp_path)
    assert spawned == [] and "[germinate]" not in ctx


def _install(tmp_path) -> Path:
    (tmp_path / ".levain").mkdir()
    return tmp_path


def _add(root, nxt, disposition="agenda"):
    path = root / ".levain" / "memory.spores.json"
    day = nxt[:10]
    s = guarded_spore_store(path).add(type="thought", text="call him", disposition=disposition, next=day)
    if nxt != day:      # anneal's write path takes a date only; a timed next is stored by hand (as a hand edit would)
        doc = json.loads(path.read_text())
        for row in (doc["spores"] if isinstance(doc, dict) else doc):
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
    _add(root, "2026-11-01", disposition="seed")
    assert doctor._check_overdue_agenda(root, today=TODAY) == []


def test_overdue_agenda_silent_with_no_spores_file(tmp_path):
    assert doctor._check_overdue_agenda(tmp_path, today=TODAY) == []


def test_overdue_agenda_a_timed_agenda_from_yesterday_is_overdue(tmp_path):
    root = _install(tmp_path)
    _add(root, "2026-11-06T09:30")
    assert len(doctor._check_overdue_agenda(root, today=TODAY)) == 1
