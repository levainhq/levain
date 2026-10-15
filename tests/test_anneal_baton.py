"""anneal 1.0 (KL-19): a store `init` creates requires the consolidate baton. Levain's side of it:
`_init_store` claims the baton, the MCP launcher serves as that session, `levain wrap` wraps as it."""
from __future__ import annotations

import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, Store, sessions

import levain.install as inst
from levain import wrap as wrapmod
from levain.doctor import _check_baton, _check_mcp_command
from levain.manifest import ANNEAL_SESSION_ID
from levain.wrap import wrap_entity
from tests.test_wrap import _VALID_NEOCORTEX, _openhands_entity


def _baton_store(entity: Path) -> Path:
    """The store `anneal-memory init` makes: partnership schema, baton policy, baton claimed for levain."""
    db = entity / ".levain" / "memory.db"
    store = Store(str(db), section_schema=FLOW_SCHEMA, consolidate_requires_baton_on_create=True)
    store.record(content="Turn 0: I helped the operator with the wrap command.",
                 episode_type="observation", source="test-session")
    store.close()
    sessions.claim_baton(entity / ".levain" / "memory.continuity.md", ANNEAL_SESSION_ID)
    return db


def test_wrap_runs_as_the_baton_session_on_a_baton_store(tmp_path, capsys, monkeypatch):
    """Reproduced against rc2: `prepare_wrap` with no session on a baton store returns "downgraded", so
    `levain wrap` on every fresh install printed "anneal declined to start a wrap"."""
    ent = _openhands_entity(tmp_path)
    db = _baton_store(ent)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent) == 0
    assert "consolidated." in capsys.readouterr().out
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_init_store_claims_the_baton_on_a_fresh_store(tmp_path, monkeypatch):
    # THIS interpreter's anneal, not whichever `anneal-memory` a PATH lookup finds (a stale 0.9 would
    # make a store without the baton policy, and doctor would resolve the same one).
    beside = Path(sys.executable).parent / "anneal-memory"
    anneal = str(beside) if beside.exists() else shutil.which("anneal-memory") or "anneal-memory"
    monkeypatch.setattr("levain.manifest.resolve_anneal_bin", lambda: anneal)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)

    assert inst._init_store(store, anneal) is True
    baton = tmp_path / ".levain" / "memory.continuity.md"
    assert sessions.baton_holder(baton) == ANNEAL_SESSION_ID
    assert [r.ok for r in _check_baton(tmp_path)] == [True]

    sessions.release_baton(baton, ANNEAL_SESSION_ID)          # doctor names the unclaimed baton
    (row,) = _check_baton(tmp_path)
    assert not row.ok and "baton claim levain" in row.hint


def test_init_store_fails_loud_when_the_claim_fails(tmp_path, monkeypatch):
    store = tmp_path / "memory.db"
    calls: list[list[str]] = []

    def fake(_store, _anneal, sub_args):
        calls.append(sub_args)
        return (sub_args[0] != "baton", "", ["boom"])

    monkeypatch.setattr(inst, "_run_anneal_cmd", fake)
    lines: list[str] = []
    assert inst._init_store(store, "anneal-memory", emit=lines.append) is False
    assert calls[-1] == ["baton", "claim", ANNEAL_SESSION_ID]
    assert any("cannot consolidate" in ln for ln in lines)


def test_launcher_shape_agrees_across_template_doctor_and_reinit(tmp_path):
    root = Path(inst.__file__).parent / "templates" / "adapters"
    fill = lambda t: t.replace("{{PYTHON}}", sys.executable).replace("{{INSTALL_DIR}}", str(tmp_path))  # noqa: E731
    cc = json.loads(fill((root / "claude-code" / "mcp.template.json").read_text()))
    cx = tomllib.loads(fill((root / "codex" / "mcp.template.toml").read_text()))
    cc_args = cc["mcpServers"]["anneal_memory"]["args"]
    cx_args = cx["mcp_servers"]["anneal_memory"]["args"]
    assert cc_args == cx_args and cc_args[-2:] == ["--session-id", ANNEAL_SESSION_ID]

    assert _check_mcp_command("m", sys.executable, cc_args, tmp_path).ok is True
    assert _check_mcp_command("m", sys.executable, cc_args[:6], tmp_path).ok is True      # pre-KL-19 init
    assert _check_mcp_command("m", sys.executable, cc_args[:7], tmp_path).ok is False     # flag, no id
    assert not _check_mcp_command("m", sys.executable, cc_args[:6] + ["--session-id", ""], tmp_path).ok

    store = cc_args[4]
    for args in (cc_args, cc_args[:6]):                      # re-init reads both as levain's own
        assert inst._is_levain_launcher({"mcp_servers": {"anneal_memory": {
            "command": sys.executable, "args": args}}}, store)
