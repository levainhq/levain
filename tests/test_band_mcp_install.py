"""QMCP-2: the question band's MCP server wired into `init` / `update` / `doctor` (DESIGN_QMCP R-q20 .. R-q23).

Claude Code: the WORKSPACE .mcp.json gains a ``levain_band`` server from its own fragment and the settings allow
``mcp__levain_band``; the hosted template (host_guard.cc_mcp_config: exactly anneal_memory) is unchanged. Codex: a
``[mcp_servers.levain_band]`` block beside anneal's, kept when the operator edited it. Every install here lives
under tmp_path (HOME, CODEX_HOME, LEVAIN_HOME)."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain import install as inst
from levain.consent import band
from levain.install import ADAPTER_RECEIPT_REL, refresh_adapter, run_init

BAND_ARGS = ["-P", "-m", "levain.consent.band_mcp", "--agent"]
ROW = "levain_band MCP registered"


@pytest.fixture
def make_install(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])

    def _make(adapter: str = "claude-code", name: str = "entity") -> Path:
        from tests.test_init_answers import _filled

        af = tmp_path / f"{name}-answers.json"
        af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
        install = tmp_path / name
        assert run_init(install, adapter, force=False, answers_file=af) == 0
        capsys.readouterr()
        return install

    return _make


def _refresh(install: Path):
    lines: list[str] = []
    return refresh_adapter(install, apply=True, emit=lines.append), "\n".join(lines)


def _band_rows(results) -> list:
    return [r for r in results if r.name == ROW]


def _config(tmp_path: Path) -> Path:
    return tmp_path / "codex-home" / "config.toml"


# ---- Claude Code ------------------------------------------------------------------------------------------------

def test_the_workspace_mcp_json_registers_the_band_with_its_argv(make_install):
    install = make_install()
    servers = json.loads((install / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"anneal_memory", "levain_band"}
    b = servers["levain_band"]
    assert b["type"] == "stdio" and b["args"] == [*BAND_ARGS, "claude_code"]
    assert inst._PYTHON_NAME.fullmatch(Path(b["command"]).name)
    assert servers["anneal_memory"]["command"] == b["command"]   # one interpreter for both
    settings = json.loads((install / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert {"mcp__anneal_memory", "mcp__levain_band"} <= set(settings["permissions"]["allow"])


def test_the_hosted_template_still_names_exactly_anneal_memory(tmp_path):
    tpl = Path(inst.__file__).parent / "templates" / "adapters" / "claude-code" / "mcp.template.json"
    assert set(json.loads(tpl.read_text(encoding="utf-8"))["mcpServers"]) == {"anneal_memory"}
    ent = tmp_path / "ent"
    ent.mkdir()
    cfg = hg.cc_mcp_config(ent, sys.executable)            # raises HostRefusal on any other server set
    assert set(cfg["mcpServers"]) == {"anneal_memory"}


def test_a_pre_band_render_with_no_record_is_refreshed_to_carry_the_band(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    package = mcp.read_text(encoding="utf-8")
    old = json.loads(package)
    del old["mcpServers"]["levain_band"]
    mcp.write_text(json.dumps(old, indent=2), encoding="utf-8")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert ".mcp.json" in r.refreshed and mcp.read_text(encoding="utf-8") == package


def test_an_operator_edited_band_entry_is_kept_and_reported(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    data["mcpServers"]["levain_band"]["command"] = "/opt/wrap/levain-band"   # the operator's own launcher
    mcp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    before = mcp.read_text(encoding="utf-8")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()                           # no record: judged by shape
    r, out = _refresh(install)
    assert r.review == [".mcp.json"] and "yours is kept" in out
    assert mcp.read_text(encoding="utf-8") == before


def test_render_variant_sets_aside_both_levain_launchers(tmp_path):
    install = tmp_path / "i"
    store = str(install / ".levain" / "memory.db")

    def render(py: str, band_args=None, band_extra=None) -> str:
        servers = {"anneal_memory": {"type": "stdio", "command": py, "args": [
            "-P", "-m", "anneal_memory", "--db", store, "serve", "--session-id", "levain"]}}
        if band_args is not False:
            servers["levain_band"] = {"type": "stdio", "command": py,
                                      "args": band_args or [*BAND_ARGS, "claude_code"], **(band_extra or {})}
        return json.dumps({"mcpServers": servers})

    want, variant = render("/new/venv/bin/python3"), render(inst._VARIABLE)
    assert inst._levain_render_variant(render("/old/venv/bin/python3.12"), want, variant, install=install, mcp=True)
    assert inst._levain_render_variant(render("/old/bin/python", band_args=False), want, variant,
                                       install=install, mcp=True)
    assert not inst._levain_render_variant(render("/old/bin/python", band_args=[*BAND_ARGS, "claude_code", "-v"]),
                                           want, variant, install=install, mcp=True)
    assert not inst._levain_render_variant(render("/old/bin/python", band_extra={"env": {"X": "1"}}),
                                           want, variant, install=install, mcp=True)


def test_is_levain_launcher_reads_the_band_shape():
    def t(command, args):
        return {"mcp_servers": {"levain_band": {"command": command, "args": args}}}

    ok = inst._is_levain_launcher
    assert ok(t("/v/bin/python3", [*BAND_ARGS, "codex"]), None, server="levain_band", agent="codex")
    assert not ok(t("/v/bin/python3", [*BAND_ARGS, "codex"]), None, server="levain_band", agent="claude_code")
    assert not ok(t("/v/bin/python3", [*BAND_ARGS, "nobody"]), None, server="levain_band")
    assert not ok(t("/usr/bin/wrapper", [*BAND_ARGS, "codex"]), None, server="levain_band", agent="codex")
    assert not ok(t("/v/bin/python3", ["-m", "levain.consent.band_mcp", "--agent", "codex"]), None,
                  server="levain_band", agent="codex")


def test_doctor_cc_band_row_ok_then_warn(make_install):
    install = make_install()
    (row,) = _band_rows(doctor._check_claude_code(install))
    assert row.ok and not row.warn, row
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    data["mcpServers"]["levain_band"]["command"] = "/opt/wrap/levain-band"
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _band_rows(doctor._check_claude_code(install))
    assert row.ok and row.warn and row.hint == "levain update", row
    del data["mcpServers"]["levain_band"]
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _band_rows(doctor._check_claude_code(install))
    assert row.ok and row.warn and "missing" in row.detail and row.hint == "levain update", row


# ---- Codex ------------------------------------------------------------------------------------------------------

def test_codex_config_gets_the_band_block_beside_anneal(make_install, tmp_path):
    make_install("codex")
    servers = tomllib.loads(_config(tmp_path).read_text(encoding="utf-8"))["mcp_servers"]
    assert set(servers) == {"anneal_memory", "levain_band"}
    assert servers["levain_band"]["args"] == [*BAND_ARGS, "codex"]
    assert servers["levain_band"]["command"] == servers["anneal_memory"]["command"]


def test_codex_reinit_is_idempotent(make_install, tmp_path):
    install = make_install("codex")
    before = _config(tmp_path).read_text(encoding="utf-8")
    assert run_init(install, "codex", force=True, answers_file=tmp_path / "entity-answers.json") == 0
    after = _config(tmp_path).read_text(encoding="utf-8")
    assert after == before and after.count("[mcp_servers.levain_band]") == 1


def _edit_band(config: Path, new_block: str) -> None:
    text = config.read_text(encoding="utf-8")
    m = inst._CODEX_BAND_BLOCK_RE.search(text)
    assert m is not None
    config.write_text(text[:m.start()] + new_block + text[m.end():], encoding="utf-8")


def test_an_operator_edited_codex_band_block_is_kept_and_reported_on_update(make_install, tmp_path):
    install = make_install("codex")
    config = _config(tmp_path)
    mine = ('[mcp_servers.levain_band]\ncommand = "/opt/wrap/levain-band"\n'
            'args = ["--agent", "codex"]\nstartup_timeout_ms = 9000\n')
    _edit_band(config, mine)
    before = config.read_text(encoding="utf-8")
    # the package has moved since: the receipt says an older levain wrote a different block (an edit the package
    # has NOT moved is kept quietly, the receipt's rule)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"][inst.CODEX_BAND_KEY] = "0" * 64
    receipt.write_text(json.dumps(data), encoding="utf-8")
    r, out = _refresh(install)
    assert config.read_text(encoding="utf-8") == before
    assert inst.CODEX_BAND_KEY in r.review and "[mcp_servers.levain_band]: yours is kept" in out
    staged = install / ".levain" / "pending" / "codex-home" / "config.toml.levain_band"
    assert "levain.consent.band_mcp" in staged.read_text(encoding="utf-8")
    r2, out2 = _refresh(install)                                   # listed once
    assert inst.CODEX_BAND_KEY not in r2.review and "levain_band" not in out2


def test_a_levain_launcher_codex_band_block_is_replaced_on_update(make_install, tmp_path):
    install = make_install("codex")
    config = _config(tmp_path)
    want = config.read_text(encoding="utf-8")
    _edit_band(config, '[mcp_servers.levain_band]\ncommand = "/old/venv/bin/python3.11"\n'
                       'args = ["-P", "-m", "levain.consent.band_mcp", "--agent", "codex"]\n')
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"].pop(inst.CODEX_BAND_KEY)                          # no record: judged by shape
    receipt.write_text(json.dumps(data), encoding="utf-8")
    r, _out = _refresh(install)
    assert any("levain_band" in x for x in r.refreshed), r
    assert config.read_text(encoding="utf-8") == want


def test_update_adds_the_band_block_to_a_pre_band_codex_config(make_install, tmp_path):
    install = make_install("codex")
    config = _config(tmp_path)
    want = config.read_text(encoding="utf-8")
    _edit_band(config, "")
    assert "levain_band" not in config.read_text(encoding="utf-8")
    r, _out = _refresh(install)
    assert any("levain_band" in x for x in r.refreshed), r
    servers = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]
    assert servers["levain_band"]["args"] == [*BAND_ARGS, "codex"]
    assert tomllib.loads(want) == tomllib.loads(config.read_text(encoding="utf-8"))


def test_update_leaves_the_band_alone_when_codex_registers_another_install(make_install, tmp_path):
    a = make_install("codex", name="a")
    make_install("codex", name="b")                                # b now owns codex's registration
    config = _config(tmp_path)
    _edit_band(config, "")
    before = config.read_text(encoding="utf-8")
    _refresh(a)
    assert config.read_text(encoding="utf-8") == before


def test_doctor_codex_band_row_ok_then_warn(make_install, tmp_path):
    install = make_install("codex")
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.ok and not row.warn, row
    _edit_band(_config(tmp_path), '[mcp_servers.levain_band]\ncommand = "/opt/wrap/levain-band"\nargs = []\n')
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.ok and row.warn and row.hint == "levain update", row
    _edit_band(_config(tmp_path), "")
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.ok and row.warn and "missing" in row.detail, row


# ---- the agent line ---------------------------------------------------------------------------------------------

def test_the_agent_line_names_the_mcp_tool():
    assert "levain_band `ask` tool" in band.AGENT_LINE and "levain ask" in band.AGENT_LINE


def test_codex_reinit_keeps_an_operator_band_block_and_says_so(make_install, tmp_path, capsys):
    install = make_install("codex")
    config = _config(tmp_path)
    mine = '[mcp_servers.levain_band]\ncommand = "/opt/wrap/levain-band"\nargs = ["--agent", "codex"]\n'
    _edit_band(config, mine)
    assert run_init(install, "codex", force=True, answers_file=tmp_path / "entity-answers.json") == 0
    assert inst._codex_band_block(config.read_text(encoding="utf-8")) == mine
    assert "[mcp_servers.levain_band]: yours is kept" in capsys.readouterr().out
