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


@pytest.fixture(params=["host", "py"])
def rig(request, tmp_path, monkeypatch):
    # the config-home helper has two programs with one contract (zsh on macOS, python3 on Linux): every test runs the
    # host's, and again the python one (on this interpreter), so a macOS run covers the Linux program too
    if request.param == "py":
        monkeypatch.setattr(hands_login, "_conf_program", lambda: [sys.executable, "-I", "-S", "-c", hands_login._CONF_PY])
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
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None, **k: real(root, datetime.now(timezone.utc) + timedelta(days=days), **k))


# --- refusals first: each leaves every file as it was ----------------------------------------------------------

def test_two_entries_pinning_one_callback_port_refuse_the_render_and_write_nothing(rig, monkeypatch):
    two = [rig.entry("alpha", callback_port=49555), rig.entry("beta", callback_port=49555)]   # the log itself refuses this pair
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None, **k: (two, []))
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
    real = hands_login.conf_stage
    monkeypatch.setattr(hands_login, "conf_stage", lambda *a, **k: wrote.append(a[3]) or real(*a, **k))
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
    rig.codex_file.write_text('model = "gpt-x"\n\n[projects."/work"]\ntrust_level = "trusted"\n')
    prepare(rig)
    doc = codex(rig)
    assert doc["model"] == "gpt-x" and doc["projects"]["/work"] == {"trust_level": "trusted"}
    assert set(doc["mcp_servers"]) == {"linear"}
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
    assert f"CODEX_HOME={rig.hands_home}/.codex" in cargv
    assert cargv[-5:] == ["codex", "--disable", "plugins", "--disable", "remote_plugin"]


def test_the_codex_chat_disables_plugin_loading_after_the_vendor_and_before_any_extra(rig, monkeypatch):
    # L3 r3 G4 (codex HIGH): a plugin under CODEX_HOME can carry an mcp.json the registry does not hold, and the
    # config merge only sees mcp_servers; codex's own `--disable <feature>` turns plugin loading off for the chat
    monkeypatch.setattr(hands_login, "_launch_prefix", _REAL_PREFIX)
    argv = hml.codex_chat_argv(rig.h, "resume")
    at = argv.index("codex")
    assert argv[at:] == ["codex", "--disable", "plugins", "--disable", "remote_plugin", "resume"]
    assert set(hml.CODEX_DISABLED_FEATURES) == {"plugins", "remote_plugin"}


def test_the_verb_parses_and_reports_what_it_rendered(rig, capsys):
    ns = build_parser().parse_args(["hands", "mcp", "prepare", "--path", str(rig.entity)])
    assert ns.hands_mcp_cmd == "prepare" and not hasattr(ns, "name")
    rig.register("linear")
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0
    out = capsys.readouterr().out
    assert "1 server(s) rendered" in out and "linear" in out and "claude:" in out and "(written)" in out
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0
    assert "(unchanged)" in capsys.readouterr().out


# --- L3 r1 (codex, deepseek, complement): each test names the line whose revert turns it red --------------------

def test_a_foreign_codex_server_of_any_name_is_refused_registry_only(rig):
    """merge_codex_config `if foreign:` (was `names & set(foreign)`): Codex has no strict flag, so an unregistered
    `[mcp_servers.evil]` would launch beside the registry's."""
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    body = 'model = "x"\n[mcp_servers.evil]\nurl = "https://evil.example/mcp"\n'
    rig.codex_file.write_text(body)
    with pytest.raises(hml.HandsMcpRefused, match="server named 'evil'.*would run it"):
        prepare(rig)
    assert rig.codex_file.read_text() == body and not rig.claude_file.exists()


@pytest.mark.parametrize("url", ["https://mcp.example/x?session=sk-AAAAAAAAAAAAAAAAAAAAAAAA",
                                 "https://mcp.example/x?s=" + "A" * 44,
                                 "https://mcp.example/s/" + "b" * 44 + "/mcp"])
def test_a_credential_in_a_url_query_value_or_path_is_refused_at_registration_and_at_render(url):
    """hands_mcp._url_problems `_secret_value(v)` and the path-segment loop (registration), and `_render_problem`'s
    `registration_problems` call (render): an entry signed before the rule existed is never written."""
    assert hm.registration_problems({"name": "x", "transport": "remote", "url": url}, home="/nonexistent")
    e = hm.build_entry(name="leaky", transport="remote", identity_kind="dedicated",
                       expires_at=hm.lease_expiry(datetime.now(timezone.utc)), url=url)
    with pytest.raises(hml.HandsMcpRefused, match="shaped like a credential"):
        hml.render_launch([e])
    ok, skipped = hm.live_entries({"g1": e}, datetime.now(timezone.utc))
    assert ok == [] and skipped == [("g1", "rules")]


def test_an_innocent_query_value_still_registers():
    assert hm.registration_problems({"name": "x", "transport": "remote", "url": "https://mcp.example/x?team=eng&v=2"},
                                    home="/nonexistent") == []


def test_a_planted_temp_link_is_never_written_through(rig):
    """hands_login._CONF_ZSH / _CONF_PY `stage` (O_CREAT|O_EXCL|O_NOFOLLOW at an unpredictable name, leftovers
    removed first): the old fixed name `config.toml.levain-tmp` linked at a credential file truncated it, and a
    leftover of the new pattern linked at one is removed, never followed."""
    rig.register("linear")
    d = rig.codex_file.parent
    d.mkdir(parents=True, mode=0o700)
    victim = rig.hands_home / "victim.json"
    victim.write_text("credential")
    (d / "config.toml.levain-tmp").symlink_to(victim)
    (d / ".config.toml.levain-1-planted").symlink_to(victim)
    prepare(rig)
    assert victim.read_text() == "credential"
    assert not rig.codex_file.is_symlink() and "linear" in codex(rig)["mcp_servers"]
    assert not (d / ".config.toml.levain-1-planted").exists() and not (d / ".config.toml.levain-1-planted").is_symlink()
    assert not [p for p in d.iterdir() if p.name.startswith(".config.toml.levain-")]


def test_a_launch_file_that_is_a_symlink_is_refused_and_not_read(rig):
    rig.register("linear")
    rig.claude_file.parent.mkdir(parents=True)
    other = rig.operator_home / ".claude" / "sentinel"
    rig.claude_file.symlink_to(other)
    with pytest.raises(hml.HandsMcpRefused, match="symlink, not a file"):
        prepare(rig)
    assert other.read_text() == "operator's own" and rig.claude_file.is_symlink()


def test_the_registry_is_read_under_the_hands_lock(rig, monkeypatch):
    """prepare_launch: `load_registry` inside `work()` (it ran before the lock was taken)."""
    rig.register("linear")
    real, seen = hml.load_registry, []

    def spy(root, now=None, **k):
        fd = hands_live.take_exclusive(rig.entity)          # None while someone (this call) holds the lock
        seen.append(fd is None)
        if fd is not None:
            os.close(fd)
        return real(root, now, **k)
    monkeypatch.setattr(hml, "load_registry", spy)
    prepare(rig)
    assert seen == [True]


def test_identical_bytes_at_a_loose_mode_are_repaired(rig):
    """prepare_launch `old != ("600", body)` (was a bytes-only compare): a 644 file of the right bytes was "unchanged"."""
    rig.register("linear")
    prepare(rig)
    rig.claude_file.chmod(0o644)
    res = prepare(rig)
    assert res.changed == {"claude": True, "codex": False} and (rig.claude_file.stat().st_mode & 0o777) == 0o600


def test_a_launch_file_of_another_owner_is_refused(rig, monkeypatch):
    """_read_launch `if uid != hands.uid` (no chown without root, so the helper's answer is replaced)."""
    rig.register("linear")
    prepare(rig)
    real = hands_login.conf_read
    monkeypatch.setattr(hands_login, "conf_read", lambda h, v, d, n, **k: (lambda g: (g[0] + 1, *g[1:]) if g else g)(real(h, v, d, n, **k)))
    with pytest.raises(hml.HandsMcpRefused, match="belongs to another account"):
        prepare(rig)


def test_a_codex_home_that_is_a_symlink_refuses_before_claude_is_made(rig, tmp_path):
    """prepare_launch: `_ensure_dir` only after every judgement (it made `.claude` before `.codex` refused, under a
    message saying nothing was changed); the linked directory's config.toml is never read."""
    rig.register("linear")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.toml").write_text('[mcp_servers.x]\nurl = "https://x.example"\n')
    (rig.hands_home / ".codex").symlink_to(elsewhere)
    with pytest.raises(hml.HandsMcpRefused, match="not a directory; change it by hand"):
        prepare(rig)
    assert not (rig.hands_home / ".claude").exists()


def test_a_codex_stage_failure_changes_neither_file(rig, monkeypatch):
    """prepare_launch: both files staged before either rename (the Claude file was renamed in before Codex was written)."""
    rig.register("linear")
    rig.claude_file.parent.mkdir(parents=True)
    rig.claude_file.write_text("{}\n")
    rig.claude_file.chmod(0o600)
    real = hands_login.conf_stage

    def stage(h, v, d, n, text, **k):
        if v == "codex":
            raise hands_login.HandsLoginError("disk full")
        return real(h, v, d, n, text, **k)
    monkeypatch.setattr(hands_login, "conf_stage", stage)
    with pytest.raises(hml.HandsMcpError, match="disk full; neither launch file was changed"):
        prepare(rig)
    assert rig.claude_file.read_text() == "{}\n" and not rig.codex_file.exists()
    assert not [p for p in rig.claude_file.parent.iterdir() if ".levain-" in p.name]


def test_a_failed_second_rename_names_the_file_already_replaced(rig, monkeypatch):
    rig.register("linear")
    real = hands_login.conf_commit

    def commit(h, v, d, n, t, **k):
        if v == "codex":
            raise hands_login.HandsLoginError("could not replace")
        return real(h, v, d, n, t, **k)
    monkeypatch.setattr(hands_login, "conf_commit", commit)
    with pytest.raises(hml.HandsMcpError, match=f"{hml.CLAUDE_LAUNCH_FILE} was already replaced, the other was not"):
        prepare(rig)
    assert rig.claude_file.exists() and not rig.codex_file.exists()
    assert not [p for p in rig.codex_file.parent.iterdir() if ".levain-" in p.name]
    monkeypatch.setattr(hands_login, "conf_commit", real)
    assert prepare(rig).changed == {"claude": False, "codex": True}      # the next call converges


def test_markers_inside_a_multiline_string_refuse_instead_of_deleting_its_content(rig):
    """merge_codex_config: the parse-with-and-without-the-marker-lines comparison (codex reproduced the silent delete)."""
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    body = f'note = """before\n{hml.CODEX_BEGIN}\nSECRET\n{hml.CODEX_END}\nafter"""\n'
    rig.codex_file.write_text(body)
    with pytest.raises(hml.HandsMcpRefused, match="inside a TOML string"):
        prepare(rig)
    assert rig.codex_file.read_text() == body


def test_reversed_markers_refuse(rig):
    """merge_codex_config `begins[0] > ends[0]`: END above BEGIN passed the counts and wedged the file."""
    rig.register("linear")
    rig.codex_file.parent.mkdir(parents=True)
    body = f"{hml.CODEX_END}\n{hml.CODEX_BEGIN}\n"
    rig.codex_file.write_text(body)
    with pytest.raises(hml.HandsMcpRefused, match="unbalanced or repeated"):
        prepare(rig)


def test_marker_text_inside_a_one_line_string_is_not_a_marker():
    """merge_codex_config: a marker is a whole line (a substring count refused this valid file)."""
    out = hml.merge_codex_config(f'title = "{hml.CODEX_BEGIN}"\n', "", set())
    assert tomllib.loads(out)["title"] == hml.CODEX_BEGIN


def test_an_oserror_after_the_judgement_is_a_launch_error_not_a_raw_oserror(rig, monkeypatch):
    """prepare_launch: `OSError` in the stage `except` (a launcher keyed on the two error types missed it)."""
    rig.register("linear")

    def boom(*a, **k):
        raise OSError("EMLINK")
    monkeypatch.setattr(hands_login, "_ensure_dir", boom)
    with pytest.raises(hml.HandsMcpError, match="EMLINK; neither launch file was changed"):
        prepare(rig, held=True)
    assert not rig.claude_file.exists()
