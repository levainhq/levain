"""Levain 15 M1: the hands MCP registry, its signed lease, and the one renderer for both vendors.

A real scratch authority log (ssh-keygen, tmp key) carries the entries; the renderer is pure, so its output is asserted
byte for byte. The residue (that claude and codex accept the files) is a run, not a test here."""
from __future__ import annotations

import inspect
import json
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

STDIO = hm.build_entry(name="files", transport="stdio", identity_kind="dedicated", expires_at=EXP,
                       command="/usr/local/bin/files-mcp", args=["--root", "/srv/shared"],
                       env={"ZED": "z", "ALPHA": 'a"b'}, entry_id="hm-000000000001")
REMOTE = hm.build_entry(name="linear", transport="remote", identity_kind="operator", expires_at=EXP,
                        url="https://mcp.linear.example/mcp", callback_port=49300, entry_id="hm-000000000002")

CODEX_EXPECTED = '''mcp_oauth_credentials_store = "file"

[mcp_servers.files]
command = "/usr/local/bin/files-mcp"
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
      "command": "/usr/local/bin/files-mcp",
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
                command="/usr/local/bin/files-mcp", args=[], env={}, entry_id="hm-0000000000aa")
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
    table = hm.codex_server_table({**STDIO, "args": ["a\x7fb"]})
    assert "\x7f" not in table and "\\u007f" in table


def test_two_entries_of_one_name_or_one_callback_port_are_refused_not_merged():
    with pytest.raises(hm.HandsMcpRefused, match="one name"):
        hm.render_codex_toml([STDIO, {**STDIO, "id": "hm-0000000000bb"}])
    with pytest.raises(hm.HandsMcpRefused, match="callback port"):
        hm.render_claude_json([REMOTE, {**REMOTE, "name": "other"}])


def test_the_claude_path_beside_cc_mcp_config_adds_the_registry_and_leaves_the_canary_check_alone(tmp_path):
    ent = tmp_path / "ent"
    ent.mkdir()
    base = hg.cc_mcp_config(ent, "/usr/bin/python3")
    assert set(base["mcpServers"]) == {"anneal_memory"}        # the one-server check still stands
    both = hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [STDIO, REMOTE])
    assert both["mcpServers"]["anneal_memory"] == base["mcpServers"]["anneal_memory"]
    assert both["mcpServers"]["files"] == hm.claude_server(STDIO)
    with pytest.raises(hg.HostRefusal, match="already carries"):
        hg.cc_mcp_registry_config(ent, "/usr/bin/python3", [{**STDIO, "name": "anneal_memory"}])


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
    assert hm.registration_problems(_entry(command="npx", args=["-y", "some-server@1.2.3"]), home=HOME) == []


@pytest.mark.parametrize("kw, said", [
    (dict(command="/Users/operator/bin/mcp"), "under the operator's home"),
    (dict(command="/Users/operator"), "under the operator's home"),
    (dict(command="/Users/operator/../operator/x"), "bare name or an absolute path"),
    (dict(command="~/bin/mcp"), "bare name or an absolute path"),
    (dict(command="./mcp"), "bare name or an absolute path"),
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
