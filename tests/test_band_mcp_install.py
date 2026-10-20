"""QMCP-2: the question band's MCP server wired into `init` / `update` / `doctor` (DESIGN_QMCP R-q20 .. R-q23).

Claude Code: the WORKSPACE .mcp.json gains a ``levain_band`` server from its own fragment and the settings allow
``mcp__levain_band__ask``; the hosted template (host_guard.cc_mcp_config: exactly anneal_memory) is unchanged. Codex: a
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
    assert set(servers) == {"anneal_memory", "levain_band", "levain_dispatch"}
    b = servers["levain_band"]
    assert b["type"] == "stdio" and b["args"] == [*BAND_ARGS, "claude_code"]
    assert inst._PYTHON_NAME.fullmatch(Path(b["command"]).name)
    assert servers["anneal_memory"]["command"] == b["command"]   # one interpreter for both
    settings = json.loads((install / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert {"mcp__anneal_memory", "mcp__levain_band__ask"} <= set(settings["permissions"]["allow"])


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
    assert row.ok and row.warn and ".levain/pending/.mcp.json" in row.hint, row   # update keeps it: the hint names what helps
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
    assert row.ok and row.warn and "delete the [mcp_servers.levain_band] table" in row.hint, row
    _edit_band(_config(tmp_path), "")
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.ok and row.warn and "missing" in row.detail, row


def test_the_codex_band_block_carries_this_levain_home_and_approves_its_tool(make_install, tmp_path):
    """R-q20 amended: Codex starts an MCP server with no LEVAIN_HOME of the shell's, and `codex exec` refuses an
    unapproved MCP call (both measured, CODEX_MCP_RUN/REPORT.md), so the block carries both."""
    make_install("codex")
    text = _config(tmp_path).read_text(encoding="utf-8")
    assert "{{" not in text                                        # every slot filled
    server = tomllib.loads(text)["mcp_servers"]["levain_band"]
    assert server["env"] == {"LEVAIN_HOME": str((tmp_path / "levain-home").resolve()),
                             "CODEX_HOME": str((tmp_path / "codex-home").resolve())}
    assert server["default_tools_approval_mode"] == "approve"
    assert set(server) == {"command", "args", "env", "default_tools_approval_mode"}


def test_an_older_band_render_is_refreshed_and_an_operator_approval_mode_is_kept(make_install, tmp_path):
    install = make_install("codex")
    config = _config(tmp_path)
    want = config.read_text(encoding="utf-8")
    block = inst._codex_band_block(want)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"].pop(inst.CODEX_BAND_KEY)                          # no record: judged by shape
    receipt.write_text(json.dumps(data), encoding="utf-8")
    older = "".join(line + "\n" for line in block.splitlines()
                    if not line.startswith(("env = ", "default_tools_approval_mode")))
    _edit_band(config, older)
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.warn and "older levain render" in row.detail and row.hint == "levain update", row
    r, _out = _refresh(install)
    assert any("levain_band" in x for x in r.refreshed), r
    assert config.read_text(encoding="utf-8") == want
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"].pop(inst.CODEX_BAND_KEY)
    receipt.write_text(json.dumps(data), encoding="utf-8")
    _edit_band(config, block.replace('"approve"', '"prompt"'))
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.warn and "'prompt'" in row.detail and "delete the [mcp_servers.levain_band]" in row.hint, row
    r, _out = _refresh(install)
    assert not any("levain_band" in x for x in r.refreshed), r      # the operator's mode is kept
    assert '"prompt"' in config.read_text(encoding="utf-8")


def test_doctor_warns_when_the_band_files_under_another_levain_home(make_install, tmp_path, monkeypatch):
    install = make_install("codex")
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "elsewhere"))
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.warn and "LEVAIN_HOME" in row.detail and "cockpit here" in row.detail, row
    assert row.hint == "levain update", row


def test_a_reinit_after_the_levain_home_moved_rewrites_levains_own_band_block(make_install, tmp_path, monkeypatch,
                                                                              capsys):
    """L3 r1 (complement MED): levain's own block written under another home was judged the operator's and kept, and
    the doctor hint `levain update` looped."""
    install = make_install("codex")
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "moved-home"))
    assert run_init(install, "codex", force=True, answers_file=tmp_path / "entity-answers.json") == 0
    capsys.readouterr()
    server = tomllib.loads(_config(tmp_path).read_text(encoding="utf-8"))["mcp_servers"]["levain_band"]
    assert server["env"]["LEVAIN_HOME"] == str((tmp_path / "moved-home").resolve())
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.ok and not row.warn, row


def test_doctor_reports_an_unresolvable_home_instead_of_crashing(make_install, tmp_path):
    """L3 r1 (codex MED): a NUL in a TOML string reached Path.resolve and aborted doctor."""
    install = make_install("codex")
    config = _config(tmp_path)
    block = inst._codex_band_block(config.read_text(encoding="utf-8"))
    home_line = next(line for line in block.splitlines() if line.startswith("env = "))
    _edit_band(config, block.replace(home_line, 'env = { LEVAIN_HOME = "/x\\u0000y", CODEX_HOME = "/c" }'))
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.warn and "LEVAIN_HOME" in row.detail, row


def test_doctor_warns_when_the_ask_tool_is_not_allowed(make_install):
    """L3 r1 (codex MED): with the server registered but no allow rule, a headless session is refused `ask` while
    doctor read green. The rule names the one tool, never the whole server."""
    install = make_install()
    settings_path = install / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "mcp__levain_band__ask" in settings["permissions"]["allow"]
    assert "mcp__levain_band" not in settings["permissions"]["allow"]
    (row,) = [r for r in doctor._check_claude_code(install) if r.name == doctor.BAND_ALLOW_ROW]
    assert row.ok and not row.warn, row
    settings["permissions"]["allow"].remove("mcp__levain_band__ask")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    (row,) = [r for r in doctor._check_claude_code(install) if r.name == doctor.BAND_ALLOW_ROW]
    assert row.warn and "headless" in row.detail, row


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


# ---- L1 + L2 fix lane (1018+51) ----------------------------------------------------------------------------------

CODEX_ROOT = Path(inst.__file__).parent / "templates" / "adapters" / "codex"


def _pair(home: Path, notes: list[str], hooks_text: str = '{"hooks": {}}\n') -> None:
    inst._write_codex_pair(home, inst._codex_fragment(CODEX_ROOT, sys.executable, home / "inst"), hooks_text,
                           notes.append, band_fragment=inst._codex_band_fragment(CODEX_ROOT, sys.executable))


@pytest.mark.parametrize("hooks_already_there", [False, True])
def test_a_band_merge_failure_leaves_config_toml_as_it_was_and_names_the_band(tmp_path, monkeypatch,
                                                                               hooks_already_there):
    """L2 #1: the band merge failing after the anneal merge wrote config.toml put nothing back (the undo could not
    prove the file levain's) and blamed hooks.json. Now config.toml is byte-for-byte as before, whatever hooks.json
    holds, and the note names the band block."""
    home = tmp_path / "codex-home"
    home.mkdir()
    orig = '[mcp_servers.other]\ncommand = "x"\n'
    (home / "config.toml").write_text(orig, encoding="utf-8")
    hooks_text = '{"hooks": {}}\n'
    if hooks_already_there:
        (home / "hooks.json").write_text(hooks_text, encoding="utf-8")

    def boom(*_a, **_k):
        raise inst.InitError("the band merge failed")

    monkeypatch.setattr(inst, "_merge_codex_band", boom)
    notes: list[str] = []
    with pytest.raises(inst.InitError, match="the band merge failed"):
        _pair(home, notes, hooks_text)
    assert (home / "config.toml").read_text(encoding="utf-8") == orig
    assert sorted(p.name for p in home.iterdir()) == (["config.toml", "hooks.json"] if hooks_already_there
                                                       else ["config.toml"])
    text = "\n".join(notes)
    assert "its levain_band block could not be added to" in text and "was undone" in text, text
    assert "hooks.json could not be written" not in text


def test_a_quoted_band_header_is_kept_never_a_duplicate_table(tmp_path):
    """L2 #4c: `[mcp_servers."levain_band"]` is the same table to TOML; levain neither appends a second one (a parse
    refusal, then the L2 #1 undo) nor edits it."""
    home = tmp_path / "codex-home"
    home.mkdir()
    orig = '[mcp_servers."levain_band"]\ncommand = "x"\nargs = []\n'
    (home / "config.toml").write_text(orig, encoding="utf-8")
    notes: list[str] = []
    _pair(home, notes)
    after = (home / "config.toml").read_text(encoding="utf-8")
    servers = tomllib.loads(after)["mcp_servers"]
    assert servers["levain_band"] == {"command": "x", "args": []} and "anneal_memory" in servers
    assert after.count("levain_band") == 1
    assert any("form levain does not edit" in n for n in notes), notes


@pytest.mark.parametrize("header", ['[mcp_servers."levain_band"]', '  [mcp_servers.levain_band]'])
def test_update_stages_a_band_table_levain_cannot_edit(make_install, tmp_path, header):
    install = make_install("codex")
    config = _config(tmp_path)
    _edit_band(config, f'{header}\ncommand = "/opt/wrap/levain-band"\nargs = []\n')
    before = config.read_text(encoding="utf-8")
    r, _out = _refresh(install)                                    # the package has not moved: kept quietly
    assert config.read_text(encoding="utf-8") == before and not r.review
    assert not any("levain_band" in x for x in r.refreshed)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)               # the package moved: kept, staged, said once
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"][inst.CODEX_BAND_KEY] = "0" * 64
    receipt.write_text(json.dumps(data), encoding="utf-8")
    r, out = _refresh(install)
    assert config.read_text(encoding="utf-8") == before
    assert inst.CODEX_BAND_KEY in r.review and "yours is kept" in out
    assert not any("levain_band" in x for x in r.refreshed)
    data["files"].pop(inst.CODEX_BAND_KEY)                          # no record at all: the same
    receipt.write_text(json.dumps(data), encoding="utf-8")
    r, out = _refresh(install)
    assert config.read_text(encoding="utf-8") == before and inst.CODEX_BAND_KEY in r.review


def test_the_band_block_ends_at_its_last_key_line():
    text = ('[mcp_servers.levain_band]\n# about the band\ncommand = "x"\n\nargs = [\n  "-P",\n]\n'
            '\n# my precious server\n[mcp_servers.mine]\ncommand = "y"\n')
    m = inst._CODEX_BAND_BLOCK_RE.search(text)
    assert m.group(0) == '[mcp_servers.levain_band]\n# about the band\ncommand = "x"\n\nargs = [\n  "-P",\n]\n'
    tail = '[mcp_servers.levain_band]\ncommand = "x"\nargs = []\n\n# note\n  [mcp_servers.mine]\ncommand = "y"\n'
    assert inst._CODEX_BAND_BLOCK_RE.search(tail).group(0) == '[mcp_servers.levain_band]\ncommand = "x"\nargs = []\n'
    assert inst._CODEX_BAND_BLOCK_RE.search('[mcp_servers.levain_band]\ncommand = "x"').group(0) == \
        '[mcp_servers.levain_band]\ncommand = "x"'


def test_a_replace_keeps_the_operators_comment_above_the_next_table(make_install, tmp_path):
    """L1 #1 / L2 #4a: the band block's regex ran to the next header, so a replace deleted the operator's comment
    above it, and the comment then made the block read as the operator's forever."""
    install = make_install("codex")
    config = _config(tmp_path)
    levains = inst._codex_band_block(config.read_text(encoding="utf-8"))
    tail = "\n# my precious server\n[mcp_servers.mine]\ncommand = \"y\"\n"
    _edit_band(config, '[mcp_servers.levain_band]\ncommand = "/old/venv/bin/python3.11"\n'
                       'args = ["-P", "-m", "levain.consent.band_mcp", "--agent", "codex"]\n' + tail)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"].pop(inst.CODEX_BAND_KEY)                          # no record: judged by shape
    receipt.write_text(json.dumps(data), encoding="utf-8")
    r, _out = _refresh(install)
    assert any("levain_band" in x for x in r.refreshed), r
    text = config.read_text(encoding="utf-8")
    assert levains + tail in text
    r2, out2 = _refresh(install)                                     # levain's own now: nothing staged
    assert inst.CODEX_BAND_KEY not in r2.review and "levain_band" not in out2


def test_init_keeping_an_operator_band_block_records_no_hash_for_it(make_install, tmp_path):
    """L1 #9: init recorded levain's block hash although the operator's was kept. Unrecorded, update judges it by
    shape: kept, levain's staged, listed once."""
    install = make_install("codex")
    config = _config(tmp_path)
    mine = '[mcp_servers.levain_band]\ncommand = "/opt/wrap/levain-band"\nargs = ["--agent", "codex"]\n'
    _edit_band(config, mine)
    assert run_init(install, "codex", force=True, answers_file=tmp_path / "entity-answers.json") == 0
    receipt = json.loads(install.joinpath(*ADAPTER_RECEIPT_REL).read_text(encoding="utf-8"))
    assert inst.CODEX_BAND_KEY not in receipt["files"]
    r, out = _refresh(install)
    assert inst._codex_band_block(config.read_text(encoding="utf-8")) == mine
    assert inst.CODEX_BAND_KEY in r.review and "yours is kept" in out
    r2, out2 = _refresh(install)
    assert inst.CODEX_BAND_KEY not in r2.review and "levain_band" not in out2
    (row,) = _band_rows(doctor._check_codex(install))
    assert row.warn and "levain update` keeps a block it did not write" in row.hint, row


def test_init_writing_levains_band_block_records_it(make_install, tmp_path):
    install = make_install("codex")
    receipt = json.loads(install.joinpath(*ADAPTER_RECEIPT_REL).read_text(encoding="utf-8"))
    block = inst._codex_band_block(_config(tmp_path).read_text(encoding="utf-8"))
    assert receipt["files"][inst.CODEX_BAND_KEY] == inst._sha256_text(block)


def test_doctor_band_row_warns_on_a_lookalike_interpreter_and_on_extra_keys(make_install, tmp_path):
    """L2 #3: OK only for THIS levain's interpreter with exactly levain's keys."""
    install = make_install()
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["levain_band"]
    data["mcpServers"]["levain_band"] = {**entry, "command": "/tmp/evil/python3"}
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _band_rows(doctor._check_claude_code(install))
    assert row.warn and "not this levain's Python" in row.detail, row
    data["mcpServers"]["levain_band"] = {**entry, "env": {"PYTHONPATH": "/tmp/evil"}}
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _band_rows(doctor._check_claude_code(install))
    assert row.warn and "carries env" in row.detail, row

    cx = make_install("codex", name="cx")
    config = _config(tmp_path)
    block = inst._codex_band_block(config.read_text(encoding="utf-8"))
    home_line = next(line for line in block.splitlines() if line.startswith("env = "))
    _edit_band(config, block.replace(home_line, home_line[:-1] + ', PYTHONPATH = "/tmp/evil" }'))
    (row,) = _band_rows(doctor._check_codex(cx))
    assert row.warn and "carries env" in row.detail, row
    _edit_band(config, block.replace(home_line + "\n", ""))
    config.write_text(config.read_text(encoding="utf-8") + '\n[mcp_servers.levain_band.env]\nPYTHONPATH = "/x"\n',
                      encoding="utf-8")
    (row,) = _band_rows(doctor._check_codex(cx))
    assert row.warn and "carries env" in row.detail, row


# ---- the dispatch door (levain_dispatch, BUILD_wire item 3) ------------------------------------------------------

DISPATCH_ARGS = ["-P", "-m", "levain.dispatch.mcp", "--agent"]


def test_the_workspace_mcp_json_registers_the_dispatch_door_beside_the_band(make_install, tmp_path):
    install = make_install()
    servers = json.loads((install / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    d = servers["levain_dispatch"]
    assert d == {"type": "stdio", "command": servers["levain_band"]["command"], "args": [*DISPATCH_ARGS, "claude_code"]}
    # every write stayed in the scratch homes
    assert str(tmp_path) in str(install) and not (tmp_path / "codex-home" / "config.toml").exists()


def test_a_pre_dispatch_render_with_no_record_is_refreshed_to_carry_the_door(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    package = mcp.read_text(encoding="utf-8")
    old = json.loads(package)
    del old["mcpServers"]["levain_dispatch"]                    # this release's render before the door
    mcp.write_text(json.dumps(old, indent=2), encoding="utf-8")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert ".mcp.json" in r.refreshed and mcp.read_text(encoding="utf-8") == package


def test_an_operator_edited_dispatch_entry_is_kept_and_reported(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    data["mcpServers"]["levain_dispatch"]["command"] = "/opt/wrap/levain-dispatch"
    mcp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    before = mcp.read_text(encoding="utf-8")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, out = _refresh(install)
    assert r.review == [".mcp.json"] and "yours is kept" in out
    assert mcp.read_text(encoding="utf-8") == before


def test_is_levain_launcher_reads_the_dispatch_shape():
    def t(command, args):
        return {"mcp_servers": {"levain_dispatch": {"command": command, "args": args}}}

    ok = inst._is_levain_launcher
    assert ok(t("/v/bin/python3", [*DISPATCH_ARGS, "claude_code"]), None, server="levain_dispatch",
              agent="claude_code")
    assert not ok(t("/v/bin/python3", [*DISPATCH_ARGS, "claude_code"]), None, server="levain_dispatch",
                  agent="codex")
    assert not ok(t("/v/bin/python3", [*BAND_ARGS, "claude_code"]), None, server="levain_dispatch")
    assert not ok(t("/usr/bin/wrapper", [*DISPATCH_ARGS, "claude_code"]), None, server="levain_dispatch")
    assert not ok(t("/v/bin/python3", [*DISPATCH_ARGS, "claude_code", "--dispatch-dir", "/x"]), None,
                  server="levain_dispatch")


def test_the_settings_template_allows_no_dispatch_tool():
    """The dispatch door needs the operator's approval on every call: no allow rule levain writes names it."""
    template = Path(inst.__file__).parent / "templates" / "adapters" / "claude-code" / "settings.template.json"
    allow = json.loads(template.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow and not [a for a in allow if a.startswith("mcp__levain_dispatch")]


DISPATCH_ROW = "levain_dispatch MCP registered"


def _dispatch_rows(results) -> list:
    return [r for r in results if r.name == DISPATCH_ROW]


def test_doctor_cc_dispatch_row_ok_then_warn(make_install):
    install = make_install()
    (row,) = _dispatch_rows(doctor._check_claude_code(install))
    assert row.ok and not row.warn and "levain.dispatch.mcp" in row.detail, row
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    data["mcpServers"]["levain_dispatch"]["command"] = "/opt/wrap/levain-dispatch"
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _dispatch_rows(doctor._check_claude_code(install))
    assert row.ok and row.warn and ".levain/pending/.mcp.json" in row.hint, row
    data["mcpServers"]["levain_dispatch"]["command"] = sys.executable
    data["mcpServers"]["levain_dispatch"]["env"] = {"PYTHONPATH": "/x"}
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _dispatch_rows(doctor._check_claude_code(install))
    assert row.ok and row.warn and "carries env" in row.detail, row


def test_doctor_cc_dispatch_row_absent_names_update_or_the_hand_merge(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    del data["mcpServers"]["levain_dispatch"]
    mcp.write_text(json.dumps(data), encoding="utf-8")
    (row,) = _dispatch_rows(doctor._check_claude_code(install))
    assert row.ok and row.warn and "missing" in row.detail and "levain dispatch file" in row.detail, row
    assert row.hint == "levain update", row
    # a .mcp.json that diverged from levain's render: update keeps it whole and stages its own render
    data["mcpServers"]["github"] = {"type": "stdio", "command": "gh-mcp", "args": []}
    mcp.write_text(json.dumps(data), encoding="utf-8")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    _refresh(install)
    assert "levain_dispatch" not in json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
    (row,) = _dispatch_rows(doctor._check_claude_code(install))
    assert row.warn and "by hand" in row.hint and ".levain/pending/.mcp.json" in row.hint, row
