"""Levain 15 M1: the hands MCP registry, its signed lease, and the one renderer for both vendors.

A real scratch authority log (ssh-keygen, tmp key) carries the entries; the renderer is pure, so its output is asserted
byte for byte. The residue (that claude and codex accept the files) is a run, not a test here."""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
import levain.host_guard as hg
from levain import hands_mcp as hm
from levain.firing import confinement

from .chat_egress.keys_rig import Cockpit

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
EXP = "2026-12-03T12:00:00.000000Z"

# a command every test host holds as root's, outside every home, in no group- or world-writable directory
TRUSTED = "/bin/sh"

STDIO = hm.build_entry(name="files", transport="stdio", identity_kind="dedicated", expires_at=EXP,
                       command=TRUSTED, args=["--root", "/srv/shared"],
                       env={"ZED": "z", "ALPHA": 'a"b'}, entry_id="hm-000000000001")
REMOTE = hm.build_entry(name="linear", transport="remote", identity_kind="operator", expires_at=EXP,
                        url="https://mcp.linear.example/mcp", callback_port=49300, entry_id="hm-000000000002")

CODEX_EXPECTED = '''mcp_oauth_credentials_store = "file"

[mcp_servers.files]
command = "/bin/sh"
args = ["--root", "/srv/shared"]
default_tools_approval_mode = "prompt"
[mcp_servers.files.env]
ALPHA = "a\\"b"
ZED = "z"

[mcp_servers.linear]
url = "https://mcp.linear.example/mcp"
default_tools_approval_mode = "prompt"
[mcp_servers.linear.oauth]
callback_port = 49300
'''

CLAUDE_EXPECTED = '''{
  "mcpServers": {
    "files": {
      "args": [
        "--root",
        "/srv/shared"
      ],
      "command": "/bin/sh",
      "env": {
        "ALPHA": "a\\"b",
        "ZED": "z"
      },
      "type": "stdio"
    },
    "linear": {
      "type": "http",
      "url": "https://mcp.linear.example/mcp"
    }
  }
}
'''


@pytest.fixture
def ck(tmp_path):
    return Cockpit(tmp_path)


def _entry(**kw):
    base = dict(name="files", transport="stdio", identity_kind="dedicated", expires_at=EXP,
                command=TRUSTED, args=[], env={}, entry_id="hm-0000000000aa")
    return hm.build_entry(**{**base, **kw})


# --- one renderer, one entry, both vendors ---------------------------------------------------------------------

def test_the_codex_toml_and_the_claude_json_come_from_the_same_entries_byte_for_byte():
    assert hm.render_codex_toml([REMOTE, STDIO]) == CODEX_EXPECTED
    assert hm.render_claude_json([REMOTE, STDIO]) == CLAUDE_EXPECTED
    for _ in range(3):                                         # no clock, no set order, no randomness in the bytes
        assert hm.render_codex_toml([STDIO, REMOTE]) == CODEX_EXPECTED
        assert hm.render_claude_json([STDIO, REMOTE]) == CLAUDE_EXPECTED


def test_the_rendered_files_parse_and_carry_the_pinned_terms():
    cfg = tomllib.loads(hm.render_codex_toml([STDIO, REMOTE]))
    assert cfg["mcp_oauth_credentials_store"] == "file"
    assert cfg["mcp_servers"]["files"]["env"] == {"ALPHA": 'a"b', "ZED": "z"}
    assert cfg["mcp_servers"]["linear"]["oauth"] == {"callback_port": 49300}
    assert all(s["default_tools_approval_mode"] == "prompt" for s in cfg["mcp_servers"].values())
    assert json.loads(hm.render_claude_json([STDIO, REMOTE]))["mcpServers"]["linear"] == {
        "type": "http", "url": "https://mcp.linear.example/mcp"}


def test_a_control_character_in_a_value_is_escaped_never_emitted_raw():
    table = hm.render_codex_toml([_entry(args=["a\x7fb"])])
    assert "\x7f" not in table and "\\u007f" in table


def test_two_entries_of_one_name_or_one_callback_port_are_refused_not_merged():
    with pytest.raises(hm.HandsMcpRefused, match="one name"):
        hm.render_codex_toml([STDIO, {**STDIO, "id": "hm-0000000000bb"}])
    other = hm.build_entry(name="other", transport="remote", identity_kind="dedicated", expires_at=EXP,
                           url="https://other.example/mcp", callback_port=REMOTE["callback_port"])
    with pytest.raises(hm.HandsMcpRefused, match="callback port"):
        hm.render_claude_json([REMOTE, other])


def test_the_claude_path_beside_cc_mcp_config_adds_the_registry_and_leaves_the_canary_check_alone(tmp_path):
    ent = tmp_path / "ent"
    ent.mkdir()
    base = hg.cc_mcp_config(ent, "/usr/bin/python3")
    assert set(base["mcpServers"]) == {"anneal_memory"}        # the one-server check still stands
    both = hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [STDIO, REMOTE])
    assert both["mcpServers"]["anneal_memory"] == base["mcpServers"]["anneal_memory"]
    assert both["mcpServers"]["files"] == json.loads(hm.render_claude_json([STDIO]))["mcpServers"]["files"]
    with pytest.raises(hg.HostRefusal, match="anneal_memory"):
        hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [_entry(name="anneal_memory")])


def test_hands_for_is_mode_based_with_no_vendor_branch():
    src = inspect.getsource(confinement.hands_for)
    assert "claude" not in src.lower() and "codex" not in src.lower() and "vendor" not in src.lower()
    assert list(inspect.signature(confinement.hands_for).parameters) == ["cfg", "mode", "system"]
    assert "confinement" not in inspect.getsource(hm)


# --- registration-time refusals ----------------------------------------------------------------------------------

HOME = "/Users/operator"


def test_a_clean_entry_passes_the_registration_rules():
    assert hm.registration_problems(STDIO, home=HOME) == []
    assert hm.registration_problems(REMOTE, home=HOME) == []
    assert hm.registration_problems(_entry(args=["-y", "some-server@1.2.3", "--author", "bob"]), home=HOME) == []


@pytest.mark.parametrize("kw, said", [
    (dict(command="/Users/operator/bin/mcp"), "under the operator's home"),
    (dict(command="/Users/operator"), "under the operator's home"),
    (dict(command="/Users/operator/../operator/x"), "absolute path"),
    (dict(args=["--token=abc123"]), "looks like a credential"),
    (dict(args=["--api-key", "abc123"]), "follows the secret-named flag"),
    (dict(args=["--password", "hunter2"]), "follows the secret-named flag"),
    (dict(args=["ghp_" + "a" * 30]), "looks like a credential"),
    (dict(args=["sk-" + "b" * 24]), "looks like a credential"),
    (dict(args=["Authorization: Bearer abcdefghijkl"]), "looks like a credential"),
    (dict(args=["--url=https://user:pw@host/x"]), "looks like a credential"),
    (dict(args=["A" * 44]), "looks like a credential"),
    (dict(name="anneal_memory"), "Levain writes itself"),
])
def test_a_stdio_entry_the_rules_refuse(kw, said):
    problems = hm.registration_problems(_entry(**kw), home=HOME)
    assert any(said in p for p in problems), problems


@pytest.mark.parametrize("cmd", ["~/bin/mcp", "./mcp"])
def test_a_relative_command_in_a_foreign_signed_entry_is_refused(cmd):
    assert _said(hm.registration_problems(_bare(cmd), home=HOME), "absolute path")


def test_a_symlink_into_the_home_is_still_under_the_home(tmp_path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (tmp_path / "ln").symlink_to(home / "bin")
    problems = hm.registration_problems(_entry(command=str(tmp_path / "ln" / "mcp")), home=str(home))
    assert any("under the operator's home" in p for p in problems)


@pytest.mark.parametrize("url, ok", [
    ("https://mcp.example.com/mcp", True), ("http://127.0.0.1:8080/mcp", True), ("http://localhost/mcp", True),
    ("http://[::1]:9/mcp", True), ("http://example.com/mcp", False), ("ftp://example.com/x", False),
    ("https://user:pw@example.com/mcp", False), ("https://token@example.com/mcp", False),
    ("https:///nohost", False), ("https://example.com/mcp#frag", False),
])
def test_a_remote_entry_needs_an_https_or_loopback_url(url, ok):
    assert (hm.registration_problems(_entry(name="r", transport="remote", command="", url=url, callback_port=49300),
                                     home=HOME) == []) is ok


# --- the signed lease --------------------------------------------------------------------------------------------

def _fresh(entry, **kw):
    now = datetime.now(timezone.utc)
    return hm.build_entry(**{**{"name": entry["name"], "transport": entry["transport"], "command": entry["command"],
                                "args": entry["args"], "env": entry["env"], "url": entry["url"],
                                "identity_kind": entry["identity_kind"], "callback_port": entry["callback_port"],
                                "entry_id": entry["id"], "expires_at": hm.lease_expiry(now)}, **kw})


def test_an_entry_is_signed_into_the_log_and_replays_as_its_kind(ck):
    e = _fresh(STDIO)
    ck.link({"op": "create_grant", "grant": e})
    g = ck.state().policy.grants[e["id"]]
    assert g["kind"] == auth.HANDS_MCP_KIND and g["identity_kind"] == "dedicated"
    assert not auth.consent_grant(g) and not auth.inert_grant(g)
    live, skipped = hm.live_entries(ck.state().policy.grants, datetime.now(timezone.utc))
    assert skipped == [] and [x["id"] for x in live] == [e["id"]]
    assert hm.render_codex_toml(live) == hm.render_codex_toml([e])           # the log round-trips the render


def test_the_lease_is_30_days_and_an_expired_entry_is_not_rendered(ck):
    with pytest.raises(auth.AuthorityError, match="30 days"):
        ck.link({"op": "create_grant", "grant": _fresh(STDIO, expires_at=hm.lease_expiry(
            datetime.now(timezone.utc), timedelta(days=31)))})
    e = _fresh(STDIO)
    ck.link({"op": "create_grant", "grant": e})
    grants = ck.state().policy.grants
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert hm.live_entries(grants, later) == ([], [(e["id"], "expired")])


def test_a_digest_that_no_longer_matches_the_table_is_reported_and_not_rendered():
    stale = {**STDIO, "server_sha256": "f" * 64}
    assert hm.live_entries({stale["id"]: stale}, NOW) == ([], [(stale["id"], "digest")])


def test_the_log_refuses_a_wrong_digest_and_each_registration_rule_at_append(ck, monkeypatch, tmp_path):
    with pytest.raises(auth.AuthorityError, match="digest of the table"):
        ck.link({"op": "create_grant", "grant": {**_fresh(STDIO), "server_sha256": "0" * 64}})
    home = tmp_path / "opshome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(auth.AuthorityError, match="under the operator's home"):
        ck.link({"op": "create_grant", "grant": _fresh(STDIO, command=str(home / "mcp"))})
    with pytest.raises(auth.AuthorityError, match="looks like a credential"):
        ck.link({"op": "create_grant", "grant": _fresh(STDIO, args=["--token=abc"])})
    with pytest.raises(auth.AuthorityError, match="https, or http only on loopback"):
        ck.link({"op": "create_grant", "grant": _fresh(REMOTE, url="http://example.com/mcp")})


@pytest.mark.parametrize("patch, said", [
    ({"identity_kind": "shared"}, "identity_kind"), ({"transport": "ws"}, "transport"),
    ({"name": "Bad Name"}, "name"), ({"callback_port": 80}, "remote entry"),
    ({"args": ["x"]}, "remote entry"), ({"url": ""}, "remote entry"), ({"server_sha256": "xyz"}, "server_sha256"),
])
def test_the_log_refuses_a_malformed_remote_entry(ck, patch, said):
    with pytest.raises(auth.AuthorityError, match=said):
        ck.link({"op": "create_grant", "grant": {**_fresh(REMOTE), **patch}})


def test_a_stdio_entry_with_a_url_or_a_port_is_refused(ck):
    with pytest.raises(auth.AuthorityError, match="stdio entry"):
        ck.link({"op": "create_grant", "grant": {**_fresh(STDIO), "url": "https://x.example/"}})
    with pytest.raises(auth.AuthorityError, match="stdio entry"):
        ck.link({"op": "create_grant", "grant": {**_fresh(STDIO), "callback_port": 49300}})


def test_an_entrys_terms_are_fixed_under_its_id_and_only_a_renew_moves_the_expiry(ck):
    e = _fresh(STDIO)
    ck.link({"op": "create_grant", "grant": e})
    now = datetime.now(timezone.utc)
    changed = _fresh(STDIO, args=["--root", "/elsewhere"])
    full = json.loads(json.dumps(ck.state().policy.as_dict()))
    full["grants"][e["id"]] = changed
    with pytest.raises(auth.AuthorityError, match="terms are fixed"):
        ck.link({"op": "loosen", "policy": full})
    ck.link({"op": "renew", "grant_id": e["id"], "signed_at": auth._stamp(now),
             "expires_at": auth._stamp(now + timedelta(days=30))}, at=now)
    renewed = ck.state().policy.grants[e["id"]]
    assert renewed["expires_at"] > e["expires_at"] and renewed["server_sha256"] == e["server_sha256"]


def test_an_enrolled_key_revokes_an_entry_and_it_is_no_longer_rendered(ck):
    e = _fresh(STDIO)
    ck.link({"op": "create_grant", "grant": e})
    ck.link({"op": "revoke_grant", "grant_id": e["id"]})
    assert hm.live_entries(ck.state().policy.grants, datetime.now(timezone.utc)) == ([], [])


# --- L3 r1: the command is one nobody but root or the operator can replace, at registration and at render ---------

def _exe(path, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return str(path)


def _said(problems, what):
    return any(what in p for p in problems)


def test_a_command_that_does_not_exist_yet_is_refused():
    # /tmp/late-mcp: outside the operator's home, absent today, creatable tomorrow by any local account
    late = f"/tmp/late-mcp-{os.getpid()}"
    assert not os.path.lexists(late)
    assert _said(hm.registration_problems(_entry(command=late), home=HOME), "does not exist")


def _bare(cmd):
    """An entry as a foreign writer would sign it: a bare command, never resolved, its own digest."""
    e = dict(STDIO, command=cmd)
    e["server_sha256"] = hm.server_sha256(e)
    return e


def test_a_bare_name_in_a_signed_entry_is_refused_the_stored_command_is_always_absolute():
    assert _said(hm.registration_problems(_bare("npx"), home=HOME), "absolute path")


def _trusted(tmp_path, monkeypatch, name="npx", mode=0o755):
    d = tmp_path / "trusted"
    exe = _exe(d / name, mode)
    monkeypatch.setattr(hm, "TRUSTED_COMMAND_DIRS", (str(tmp_path / "nowhere"), str(d)))
    return d, exe


def test_a_bare_name_resolves_through_the_trusted_dirs_and_the_signed_entry_holds_the_absolute_path(tmp_path, monkeypatch):
    _, exe = _trusted(tmp_path, monkeypatch)
    e = _entry(command="npx")
    assert e["command"] == exe and e["server_sha256"] == hm.server_sha256(e)
    assert hm.registration_problems(e, home=HOME) == []
    assert f'command = "{exe}"' in hm.render_codex_toml([e])
    assert json.loads(hm.render_claude_json([e]))["mcpServers"]["files"]["command"] == exe


def test_the_callers_path_is_never_read(tmp_path, monkeypatch):
    other = tmp_path / "onpath"
    _exe(other / "npx")
    monkeypatch.setenv("PATH", str(other))
    monkeypatch.setattr(hm, "TRUSTED_COMMAND_DIRS", (str(tmp_path / "empty"),))
    with pytest.raises(hm.HandsMcpRefused, match="not an executable in"):
        _entry(command="npx")


def test_an_unresolved_name_is_refused_naming_the_searched_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "TRUSTED_COMMAND_DIRS", (str(tmp_path / "a"), str(tmp_path / "b")))
    with pytest.raises(hm.HandsMcpRefused) as exc:
        _entry(command="uvx")
    assert str(tmp_path / "a") in str(exc.value) and str(tmp_path / "b") in str(exc.value)


@pytest.mark.parametrize("cmd", ["./npx", "bin/npx", "~/npx", "../npx", ".."])
def test_a_name_with_a_slash_that_is_not_absolute_stays_refused(tmp_path, monkeypatch, cmd):
    _trusted(tmp_path, monkeypatch)
    with pytest.raises(hm.HandsMcpRefused, match="absolute path or a bare name"):
        _entry(command=cmd)


def test_the_default_trusted_dirs_are_the_three_fixed_ones():
    assert hm.TRUSTED_COMMAND_DIRS == ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")


def test_a_bare_name_that_resolves_to_a_group_writable_file_is_refused_by_the_existing_checks(tmp_path, monkeypatch):
    _trusted(tmp_path, monkeypatch, mode=0o775)
    assert _said(hm.registration_problems(_entry(command="npx"), home=HOME), "writable by group or world")


def test_a_bare_name_that_resolves_to_a_foreign_owned_file_is_refused_by_the_existing_checks(tmp_path, monkeypatch):
    _trusted(tmp_path, monkeypatch)
    e = _entry(command="npx")
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    assert _said(hm.registration_problems(e, home=HOME), "owned by root or the operator")


def test_a_bare_name_resolving_to_a_link_into_a_loose_tree_is_judged_by_its_target(tmp_path, monkeypatch):
    target = _exe(tmp_path / "loose" / "npx-cli")
    (tmp_path / "loose").chmod(0o777)
    d = tmp_path / "trusted"
    d.mkdir()
    (d / "npx").symlink_to(target)
    monkeypatch.setattr(hm, "TRUSTED_COMMAND_DIRS", (str(d),))
    e = _entry(command="npx")
    assert e["command"] == str(d / "npx")                       # the link is stored, as the file the name found
    assert _said(hm.registration_problems(e, home=HOME), "writable by group or world")


def test_a_command_in_a_sticky_world_writable_directory_is_refused_whole():
    fd, path = tempfile.mkstemp(dir="/tmp", prefix="levain-hm-")
    os.close(fd)
    try:
        os.chmod(path, 0o755)
        assert _said(hm.registration_problems(_entry(command=path), home=HOME), "writable by group or world")
    finally:
        os.unlink(path)


@pytest.mark.parametrize("mode, said", [(0o775, "writable by group or world"), (0o757, "writable by group or world"),
                                        (0o644, "not executable")])
def test_a_command_file_another_account_can_rewrite_or_cannot_run_is_refused(tmp_path, mode, said):
    cmd = _exe(tmp_path / "bin" / "mcp", mode)
    assert _said(hm.registration_problems(_entry(command=cmd), home=HOME), said)


def test_a_command_under_a_group_writable_directory_is_refused(tmp_path):
    cmd = _exe(tmp_path / "shared" / "bin" / "mcp")
    (tmp_path / "shared").chmod(0o775)
    assert _said(hm.registration_problems(_entry(command=cmd), home=HOME), "writable by group or world")


def test_a_command_that_is_a_directory_is_refused(tmp_path):
    (tmp_path / "d").mkdir()
    assert _said(hm.registration_problems(_entry(command=str(tmp_path / "d")), home=HOME), "not a regular file")


def test_a_symlink_to_a_rewritable_target_is_judged_by_its_target(tmp_path):
    target = _exe(tmp_path / "loose" / "mcp")
    (tmp_path / "loose").chmod(0o777)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "mcp").symlink_to(target)
    assert _said(hm.registration_problems(_entry(command=str(tmp_path / "bin" / "mcp")), home=HOME),
                 "writable by group or world")


def test_a_command_owned_by_another_account_is_refused(tmp_path, monkeypatch):
    cmd = _exe(tmp_path / "bin" / "mcp")
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)            # the file's owner is now not the operator
    assert _said(hm.registration_problems(_entry(command=cmd), home=HOME), "owned by root or the operator")


def test_a_case_variant_of_the_home_is_still_the_home_on_darwin(tmp_path, monkeypatch):
    home = tmp_path / "Home"
    home.mkdir()
    monkeypatch.setattr(hm.sys, "platform", "darwin")
    variant = str(tmp_path / "HOME" / "bin" / "mcp")
    assert _said(hm.registration_problems(_entry(command=variant), home=str(home)), "under the operator's home")


def test_every_renderer_refuses_a_command_that_no_longer_passes_and_live_entries_skips_it():
    gone = _entry(command=f"/tmp/late-mcp-{os.getpid()}")          # its own digest: only the file is wrong
    for render in (hm.render_codex_toml, hm.render_claude_json, hm.codex_toml, hm.claude_servers):
        with pytest.raises(hm.HandsMcpRefused, match="does not exist"):
            render([gone])
    assert hm.live_entries({gone["id"]: gone}, NOW) == ([], [(gone["id"], "command")])


# --- L3 r1: the renderers take only an entry whose digest is its own ----------------------------------------------

@pytest.mark.parametrize("patch", [{"command": "/tmp/replaced"}, {"command": "/bin/echo"}, {"args": ["--other"]},
                                   {"name": "renamed"}])
def test_every_renderer_refuses_an_entry_changed_under_its_old_digest(tmp_path, patch):
    changed = {**STDIO, **patch}
    assert changed["server_sha256"] == STDIO["server_sha256"]
    for render in (hm.render_codex_toml, hm.render_claude_json, hm.codex_toml, hm.claude_servers):
        with pytest.raises(hm.HandsMcpRefused, match="digest"):
            render([changed])
    ent = tmp_path / "ent"
    ent.mkdir()
    with pytest.raises(hg.HostRefusal, match="digest"):
        hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [changed])


def test_a_reserved_name_is_refused_at_render_and_a_registry_refusal_is_a_host_refusal(tmp_path):
    with pytest.raises(hm.HandsMcpRefused, match="levain_band"):
        hm.claude_servers([_entry(name="levain_band")])
    ent = tmp_path / "ent"
    ent.mkdir()
    with pytest.raises(hg.HostRefusal, match="one name"):
        hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [STDIO, {**STDIO, "id": "hm-0000000000bb"}])


# --- L3 r1: malformed urls refuse, never raise ---------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://[::1", "https://example.com:bad/mcp", "https://exa mple.com/mcp",
                                 "https://example.com:99999/mcp"])
def test_a_malformed_url_is_a_refusal_not_a_raw_error(url):
    problems = hm.registration_problems(_entry(name="r", transport="remote", command="", url=url,
                                               callback_port=49300), home=HOME)
    assert _said(problems, "malformed"), problems


@pytest.mark.parametrize("url, ok", [("http://127.0.0.2:8080/mcp", True),
                                     ("https://mcp.example.com/mcp?api_key=abcdef", False),
                                     ("https://mcp.example.com/mcp?team=core", True)])
def test_loopback_is_the_whole_block_and_a_secret_query_is_refused(url, ok):
    assert (hm.registration_problems(_entry(name="r", transport="remote", command="", url=url, callback_port=49300),
                                     home=HOME) == []) is ok


# --- L3 r1 / head: env is signed in plaintext, so it carries no secret ----------------------------------------------

@pytest.mark.parametrize("key", ["GITHUB_TOKEN", "API_KEY", "OPENAI_APIKEY", "DB_PASSWORD", "CLIENT_SECRET",
                                 "AWS_CREDENTIALS", "MY_AUTH", "SSH_KEY"])
def test_a_literal_value_for_a_secret_named_env_key_is_refused(key):
    problems = hm.registration_problems(_entry(env={key: "x"}), home=HOME)
    assert _said(problems, "hands account's own environment"), problems


@pytest.mark.parametrize("key", ["DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH"])
def test_a_loader_env_key_is_refused(key):
    assert _said(hm.registration_problems(_entry(env={key: "/x"}), home=HOME), "loader")


def test_a_non_secret_env_value_stays_allowed():
    assert hm.registration_problems(_entry(env={"LOG_LEVEL": "debug", "AUTHOR": "bob", "KEYBOARD": "us"}),
                                    home=HOME) == []


# --- L3 r1: two live entries never share a name or a callback port --------------------------------------------------

def test_a_second_live_entry_of_one_name_or_one_callback_port_is_refused_at_append(ck):
    a = _fresh(STDIO)
    ck.link({"op": "create_grant", "grant": a})
    with pytest.raises(auth.AuthorityError, match="per name"):
        ck.link({"op": "create_grant", "grant": _fresh(STDIO, entry_id="hm-0000000000bb")})
    r = _fresh(REMOTE)
    ck.link({"op": "create_grant", "grant": r})
    with pytest.raises(auth.AuthorityError, match="callback port"):
        ck.link({"op": "create_grant", "grant": _fresh(REMOTE, name="other", entry_id="hm-0000000000cc")})
    full = json.loads(json.dumps(ck.state().policy.as_dict()))
    full["grants"]["hm-0000000000dd"] = _fresh(STDIO, entry_id="hm-0000000000dd")
    with pytest.raises(auth.AuthorityError, match="per name"):
        ck.link({"op": "loosen", "policy": full})
    live, skipped = hm.live_entries(ck.state().policy.grants, datetime.now(timezone.utc))
    assert skipped == [] and hm.render_codex_toml(live)                    # the list stays renderable


def test_an_expired_entry_does_not_hold_its_name(ck):
    now = datetime.now(timezone.utc)
    a = _fresh(STDIO, expires_at=hm.lease_expiry(now, timedelta(days=1)))
    ck.link({"op": "create_grant", "grant": a}, at=now)
    later = now + timedelta(days=2)
    b = _fresh(STDIO, entry_id="hm-0000000000bb", expires_at=hm.lease_expiry(later))
    ck.link({"op": "create_grant", "grant": b}, at=later)
    assert [e["id"] for e in hm.live_entries(ck.state().policy.grants, later)[0]] == [b["id"]]


# --- L3 r1: a revoke only shrinks authority, so the phone passkey revokes an entry ----------------------------------

def test_a_passkey_revokes_an_entry_and_a_key_never_enrolled_does_not(tmp_path):
    pytest.importorskip("cryptography")
    from tests.test_authority_graduation_kind import T0, World
    from tests.test_passkey_custody_derived import WebAuthnKey
    world = World(tmp_path)
    e = hm.build_entry(name="linear", transport="remote", identity_kind="operator", url="https://mcp.example.com/mcp",
                       expires_at=hm.lease_expiry(T0, timedelta(days=20)), entry_id="hm-0000000000ee")
    world.link({"op": "create_grant", "grant": e})
    stranger = WebAuthnKey()
    world.keys["stranger"] = (stranger.fp, lambda m: stranger.sign(m, auth.NAMESPACE))
    world.refused({"op": "revoke_grant", "grant_id": e["id"]}, "stranger")
    assert e["id"] in world.state().policy.grants
    world.link({"op": "revoke_grant", "grant_id": e["id"]}, "pk")
    assert e["id"] not in world.state().policy.grants
