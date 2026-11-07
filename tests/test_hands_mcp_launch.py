"""`levain hands mcp prepare` and the launch files (levain 15 M2): the registry's LIVE servers rendered by the one renderer
into the hands account's Claude launch file and Codex ``config.toml``, written by the hands user. The refusals come first
and each asserts that nothing was written; the rig is the M3 one (a shim for sudo, the hands steps run as the current
user, a real scratch authority log). No real claude or codex reads the files: that the vendors accept them is a run."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone

import pytest

from levain import hands_live, hands_mcp as hm
from levain.cli import build_parser
from levain.firing import hands_login, hands_mcp_login as hml
from tests.test_hands_mcp_verbs import McpRig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

_REAL_PREFIX = hands_login._launch_prefix                    # captured before any rig replaces it


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = McpRig(tmp_path, monkeypatch)
    r.claude_file = r.hands_home / ".claude" / hml.CLAUDE_LAUNCH_FILE
    r.codex_file = r.hands_home / ".codex" / "config.toml"
    yield r
    r.oauth.close()


def prepare(rig, **kw):
    return hml.prepare_launch(rig.h, rig.entity, rig.ck.root, **kw)


def codex(rig) -> dict:
    return tomllib.loads(rig.codex_file.read_text())


def later(monkeypatch, days=31):
    real = hml.load_registry                                  # the log refuses a back-dated link, so the clock moves instead
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: real(root, datetime.now(timezone.utc) + timedelta(days=days)))


# --- refusals first: each leaves every file as it was ----------------------------------------------------------

def test_two_entries_pinning_one_callback_port_refuse_the_render_and_write_nothing(rig, monkeypatch):
    two = [rig.entry("alpha", callback_port=49555), rig.entry("beta", callback_port=49555)]   # the log itself refuses this pair
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: (two, []))
    with pytest.raises(hml.HandsMcpRefused, match="one callback port"):
        prepare(rig)
    assert not rig.claude_file.exists() and not rig.codex_file.exists()


def test_a_foreign_codex_server_of_a_rendered_name_is_refused_and_the_claude_file_is_not_written_either(rig):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text('cli_auth_credentials_store = "file"\n[mcp_servers.linear]\nurl = "https://elsewhere.example/mcp"\n')
    before = rig.codex_file.read_text()
    with pytest.raises(hml.HandsMcpRefused, match="already holds a server named 'linear'"):
        prepare(rig)
    assert rig.codex_file.read_text() == before and not rig.claude_file.exists()


@pytest.mark.parametrize("line", ['cli_auth_credentials_store = "keyring"', 'mcp_oauth_credentials_store = "keyring"'])
def test_a_store_key_set_to_anything_but_file_is_refused(rig, line):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text(line + "\n")
    with pytest.raises(hml.HandsMcpRefused, match='other than "file"'):
        prepare(rig)
    assert rig.codex_file.read_text() == line + "\n" and not rig.claude_file.exists()


@pytest.mark.parametrize("text", [f"{hml.CODEX_BEGIN}\n", f"{hml.CODEX_END}\n",
                                  f"{hml.CODEX_BEGIN}\n{hml.CODEX_END}\n{hml.CODEX_BEGIN}\n{hml.CODEX_END}\n"])
def test_an_unbalanced_or_repeated_marker_is_refused(rig, text):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text(text)
    with pytest.raises(hml.HandsMcpRefused, match="unbalanced or repeated"):
        prepare(rig)
    assert rig.codex_file.read_text() == text and not rig.claude_file.exists()


def test_a_config_that_is_not_toml_is_refused_and_left_alone(rig):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text("[[[ nope")
    with pytest.raises(hml.HandsMcpRefused, match="not valid TOML"):
        prepare(rig)
    assert rig.codex_file.read_text() == "[[[ nope" and not rig.claude_file.exists()


def test_a_launch_file_that_is_a_directory_is_refused(rig):
    rig.register("linear")
    rig.claude_file.mkdir(parents=True)
    with pytest.raises(hml.HandsMcpRefused, match="not a file"):
        prepare(rig)


def test_an_unreadable_registry_refuses_and_leaves_the_files_it_found(rig):
    rig.register("linear")
    prepare(rig)
    before = (rig.claude_file.read_text(), rig.codex_file.read_text())
    (rig.ck.root / "cockpit.db").write_bytes(b"not a database")
    with pytest.raises(hml.HandsMcpRefused, match="authority log cannot be read"):
        prepare(rig)
    assert (rig.claude_file.read_text(), rig.codex_file.read_text()) == before


def test_a_hands_home_that_is_the_operators_own_is_refused_and_the_operators_store_untouched(rig, monkeypatch):
    rig.register("linear")
    monkeypatch.setattr(hml, "_operator_home", lambda: str(rig.hands_home))
    with pytest.raises(hml.HandsMcpRefused, match="operator's own home"):
        prepare(rig)
    assert (rig.operator_home / ".claude" / "sentinel").read_text() == "operator's own"
    assert not rig.claude_file.exists() and not rig.codex_file.exists() and rig.calls() == []


def test_a_session_holding_the_hands_lock_refuses_unless_the_launcher_says_it_holds_it(rig):
    rig.register("linear")
    fd = hands_live.take_exclusive(rig.entity)
    assert fd is not None
    try:
        with pytest.raises(hml.HandsMcpRefused, match="session of this entity is open"):
            prepare(rig)
        assert not rig.claude_file.exists()
        assert prepare(rig, held=True).names == ["linear"]       # the launcher that holds the lock passes held
    finally:
        os.close(fd)
    assert rig.claude_file.exists()


def test_without_a_hands_account_the_verb_refuses_with_the_one_wording(rig, capsys, monkeypatch):
    def none(e, cfg=None):
        raise hands_login.HandsRefused(hands_login.NO_HANDS)
    monkeypatch.setattr(hands_login, "load_hands_identity", none)
    rig.register("linear")
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 1
    assert hands_login.NO_HANDS in capsys.readouterr().out and not rig.claude_file.exists()


# --- what is written -------------------------------------------------------------------------------------------

def test_both_files_hold_exactly_the_renderers_output_for_the_live_entries(rig):
    e = rig.register("linear")
    res = prepare(rig)
    assert res.names == ["linear"] and res.changed == {"claude": True, "codex": True}
    assert rig.claude_file.read_text() == hm.render_claude_json([e])
    doc = codex(rig)
    assert doc["cli_auth_credentials_store"] == "file" and doc["mcp_oauth_credentials_store"] == "file"
    assert doc["mcp_servers"] == tomllib.loads(hm.render_codex_toml([e]))["mcp_servers"]
    assert doc["mcp_servers"]["linear"]["oauth"] == {"callback_port": e["callback_port"]}
    assert doc["mcp_servers"]["linear"]["default_tools_approval_mode"] == "prompt"


def test_the_files_are_the_hands_users_and_mode_600(rig):
    rig.register("linear")
    prepare(rig)
    for f in (rig.claude_file, rig.codex_file):
        st = f.stat()
        assert st.st_uid == rig.h.uid and (st.st_mode & 0o777) == 0o600
    assert not list(rig.hands_home.rglob("*.levain-tmp"))


def test_a_stdio_entry_renders_into_both_files_too(rig):
    e = hm.build_entry(name="files", transport="stdio", identity_kind="dedicated",
                       expires_at=hm.lease_expiry(datetime.now(timezone.utc)), command="/bin/sh", args=["--root", "/srv/x"])
    rig.ck.link({"op": "create_grant", "grant": e})
    prepare(rig)
    assert json.loads(rig.claude_file.read_text())["mcpServers"]["files"]["command"] == "/bin/sh"
    assert codex(rig)["mcp_servers"]["files"]["args"] == ["--root", "/srv/x"]


def test_a_second_call_changes_nothing_and_writes_nothing(rig, monkeypatch):
    rig.register("linear")
    prepare(rig)
    ino = (rig.claude_file.stat().st_ino, rig.codex_file.stat().st_ino)
    wrote = []
    real = hml._replace_as_hands
    monkeypatch.setattr(hml, "_replace_as_hands", lambda *a, **k: wrote.append(a[2]) or real(*a, **k))
    res = prepare(rig)
    assert res.changed == {"claude": False, "codex": False} and wrote == []
    assert (rig.claude_file.stat().st_ino, rig.codex_file.stat().st_ino) == ino


def test_the_operators_own_store_is_never_touched(rig):
    rig.register("linear")
    prepare(rig)
    assert sorted(p.name for p in (rig.operator_home / ".claude").iterdir()) == ["sentinel"]
    assert sorted(p.name for p in (rig.operator_home / ".codex").iterdir()) == ["sentinel"]


def test_the_codex_content_the_hands_account_already_had_is_kept(rig):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text('model = "gpt-x"\n\n[projects."/work"]\ntrust_level = "trusted"\n\n'
                              '[mcp_servers.own]\nurl = "https://own.example/mcp"\n')
    prepare(rig)
    doc = codex(rig)
    assert doc["model"] == "gpt-x" and doc["projects"]["/work"] == {"trust_level": "trusted"}
    assert set(doc["mcp_servers"]) == {"own", "linear"}
    assert doc["cli_auth_credentials_store"] == "file" and doc["mcp_oauth_credentials_store"] == "file"


# --- the lease: what the registry no longer renders leaves the files -------------------------------------------

def test_an_expired_entry_leaves_both_files_and_the_rest_stays(rig, monkeypatch):
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    rig.codex_file.write_text('model = "gpt-x"\n')
    prepare(rig)
    assert "linear" in codex(rig)["mcp_servers"] and "linear" in json.loads(rig.claude_file.read_text())["mcpServers"]
    later(monkeypatch)
    res = prepare(rig)
    assert res.names == [] and [why for _, why in res.skipped] == ["expired"] and res.changed == {"claude": True, "codex": True}
    assert json.loads(rig.claude_file.read_text()) == {"mcpServers": {}}
    doc = codex(rig)
    assert "mcp_servers" not in doc and doc["model"] == "gpt-x" and doc["cli_auth_credentials_store"] == "file"
    assert hml.CODEX_BEGIN not in rig.codex_file.read_text()


def test_one_expired_entry_leaves_and_the_longer_lease_stays(rig, monkeypatch):
    now = datetime.now(timezone.utc)
    rig.ck.link({"op": "create_grant", "grant": rig.entry("alpha", expires_at=hm.lease_expiry(now, timedelta(days=10)))})
    rig.register("gamma")
    assert prepare(rig).names == ["alpha", "gamma"]
    later(monkeypatch, 20)
    res = prepare(rig)
    assert res.names == ["gamma"] and [why for _, why in res.skipped] == ["expired"]
    assert set(json.loads(rig.claude_file.read_text())["mcpServers"]) == {"gamma"}
    assert set(codex(rig)["mcp_servers"]) == {"gamma"}


def test_a_hand_edit_inside_the_block_is_overwritten_at_the_next_call(rig):
    rig.register("linear")
    prepare(rig)
    rig.codex_file.write_text(rig.codex_file.read_text().replace("prompt", "never"))
    assert prepare(rig).changed["codex"] is True
    assert codex(rig)["mcp_servers"]["linear"]["default_tools_approval_mode"] == "prompt"


# --- pure pieces -----------------------------------------------------------------------------------------------

def test_merge_keeps_the_pins_at_the_top_level_before_any_table():
    e = hm.build_entry(name="a", transport="remote", identity_kind="dedicated", expires_at=hm.lease_expiry(datetime.now(timezone.utc)),
                       url="https://a.example/mcp")
    _, tables = hml.render_launch([e])
    out = hml.merge_codex_config('[projects."/w"]\ntrust_level = "trusted"\n', tables, {"a"})
    doc = tomllib.loads(out)
    assert doc["mcp_oauth_credentials_store"] == "file" and doc["cli_auth_credentials_store"] == "file"
    assert "mcp_oauth_credentials_store" not in doc["projects"]["/w"]
    assert out.index("mcp_oauth_credentials_store") < out.index("[projects")


def test_merge_with_no_tables_on_an_empty_file_is_the_one_pin():
    assert hml.merge_codex_config("", "", set()) == hands_login.CODEX_STORE_LINE


def test_render_launch_with_no_entry_is_an_empty_server_table():
    body, tables = hml.render_launch([])
    assert json.loads(body) == {"mcpServers": {}} and tables == ""


# --- the launch argv and the verb ------------------------------------------------------------------------------

def test_the_claude_chat_argv_reads_only_the_launch_file_under_the_hands_prefix(rig, monkeypatch):
    monkeypatch.setattr(hands_login, "_launch_prefix", _REAL_PREFIX)
    argv = hml.claude_chat_argv(rig.h, "-p")
    assert argv[-5:] == ["claude", "--strict-mcp-config", "--mcp-config", str(rig.claude_file), "-p"] or \
        argv[-5:] == ["claude", "--strict-mcp-config", "--mcp-config", f"{rig.hands_home}/.claude/{hml.CLAUDE_LAUNCH_FILE}", "-p"]
    assert f"CLAUDE_CONFIG_DIR={rig.hands_home}/.claude" in argv
    cargv = hml.codex_chat_argv(rig.h)
    assert f"CODEX_HOME={rig.hands_home}/.codex" in cargv and cargv[-1] == "codex"


def test_the_verb_parses_and_reports_what_it_rendered(rig, capsys):
    ns = build_parser().parse_args(["hands", "mcp", "prepare", "--path", str(rig.entity)])
    assert ns.hands_mcp_cmd == "prepare" and not hasattr(ns, "name")
    rig.register("linear")
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0
    out = capsys.readouterr().out
    assert "1 server(s) rendered" in out and "linear" in out and "claude:" in out and "(written)" in out
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0
    assert "(unchanged)" in capsys.readouterr().out
