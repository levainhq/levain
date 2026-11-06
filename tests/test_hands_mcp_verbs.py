"""`levain hands mcp signin|remove|list` (levain 15 M3). The refusals come first: a verb refuses an unregistered, expired
or stdio server, a missing hands account, Linux, the operator's own store and a secret-shaped url; `remove` touches only
what `signin` added. The vendor CLI is a Python shim first on the hands PATH that keeps its config and credential file in
the hands home and completes a login against a local fake OAuth server (a real HTTP round trip); the sudo prefix is the
only thing replaced (tests/test_hands_login.py's Rig), so the hands-side steps run as the current user. No real provider,
no hands user, no keychain, no live app. A real sign-in against a real vendor is the operator's hands."""
from __future__ import annotations

import json
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from levain import hands_mcp as hm
from levain.cli import build_parser
from levain.firing import confinement, hands_login, hands_mcp_login as hml
from levain.firing.confinement import HandsIdentity
from tests.chat_egress.keys_rig import Cockpit
from tests.test_hands_login import _REAL_LOGIN_REFUSAL, Rig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

SHIM = r'''#!%(py)s
import json, os, sys, urllib.request
cfgdir = os.environ["CLAUDE_CONFIG_DIR"]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("%%s|%%s|%%s\n" %% (os.environ["HOME"], cfgdir, " ".join(sys.argv[1:])))
CFG = os.path.join(cfgdir, ".claude.json")
CRED = os.path.join(cfgdir, ".credentials.json")
env = os.environ


def load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return d


def save(p, doc):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(doc).encode())
    os.close(fd)


def missing(name):
    print('No MCP server named "%%s"' %% name, file=sys.stderr)
    sys.exit(1)


a = sys.argv[1:]
if a[:1] != ["mcp"]:
    sys.exit(2)
verb, rest = a[1], a[2:]
servers = load(CFG, {}).get("mcpServers", {})
if verb == "add":
    if env.get("SHIM_ADD_FAIL"):
        print("add failed", file=sys.stderr)
        sys.exit(1)
    name, url = rest[-2], rest[-1]
    port = rest[rest.index("--callback-port") + 1]
    doc = load(CFG, {})
    sv = doc.setdefault("mcpServers", {})
    if name in sv:
        print("already exists", file=sys.stderr)
        sys.exit(1)
    sv[name] = {"type": "http", "url": url, "oauth": {"callbackPort": int(port)}, "headers": {"X": "never-read"}}
    save(CFG, doc)
elif verb == "login":
    name = rest[-1]
    if name not in servers:
        missing(name)
    base = servers[name]["url"].rsplit("/", 1)[0]
    print("Open %%s/authorize?state=s1 to sign in" %% base)
    sys.stdout.write("Paste the redirect URL: ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip() or "pasted"
    if env.get("SHIM_LOGIN_FAIL"):
        sys.exit(3)
    tok = json.load(urllib.request.urlopen(urllib.request.Request(base + "/token", data=code.encode()), timeout=10))["access_token"]
    os.umask(0o077)
    cred = load(CRED, {})
    cred.setdefault("mcpOAuth", {})[name] = {"accessToken": tok}
    save(CRED, cred)
elif verb == "logout":
    name = rest[-1]
    if name not in servers:
        missing(name)
    if env.get("SHIM_LOGOUT_FAIL"):
        print("logout failed: keychain locked", file=sys.stderr)
        sys.exit(1)
    cred = load(CRED, {})
    cred.get("mcpOAuth", {}).pop(name, None)
    save(CRED, cred)
elif verb == "remove":
    name = rest[-1]
    if name not in servers:
        print('No MCP server named "%%s" in user scope' %% name, file=sys.stderr)
        sys.exit(1)
    doc = load(CFG, {})
    del doc["mcpServers"][name]
    save(CFG, doc)
elif verb == "get":
    name = rest[-1]
    if name not in servers:
        missing(name)
    on = name in load(CRED, {}).get("mcpOAuth", {})
    print("%%s:\n  Status: %%s" %% (name, "Connected" if on else "Needs authentication"))
else:
    sys.exit(2)
''' % {"py": sys.executable}


class FakeOAuth:
    """A local token endpoint: every POST /token answers an access token and is recorded."""

    def __init__(self) -> None:
        outer = self
        self.hits: list[tuple[str, bytes]] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):                                  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.hits.append((self.path, body))
                out = json.dumps({"access_token": "tok-" + body.decode()}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):                          # noqa: D401
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class McpRig(Rig):
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        super().__init__(tmp_path, monkeypatch)
        shim = self.bindir / "claude"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        self.ck = Cockpit(self.entity / ".levain")          # the install's own cockpit root: .levain/cockpit
        self.oauth = FakeOAuth()
        self.cfg = self.hands_home / ".claude" / ".claude.json"
        self.cred = self.hands_home / ".claude" / ".credentials.json"

    def entry(self, name="linear", *, url=None, **kw) -> dict:
        now = datetime.now(timezone.utc)
        e = hm.build_entry(**{**dict(name=name, transport="remote", identity_kind="dedicated",
                                     expires_at=hm.lease_expiry(now), url=url or f"http://127.0.0.1:{self.oauth.port}/mcp"), **kw})
        return e

    def register(self, name="linear", **kw) -> dict:
        e = self.entry(name, **kw)
        self.ck.link({"op": "create_grant", "grant": e})
        return e

    def servers(self) -> dict:
        return json.loads(self.cfg.read_text()).get("mcpServers", {}) if self.cfg.exists() else {}

    def mutating(self) -> list[str]:
        return [c for c in self.calls() if c.split(" ")[:2] and c.split(" ")[1:2] and c.split(" ")[1] in
                ("add", "login", "logout", "remove")]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = McpRig(tmp_path, monkeypatch)
    yield r
    r.oauth.close()


def signin(rig, name="linear"):
    return hml.cmd_signin(rig.entity, name, rig.ck.root)


# --- refusals first --------------------------------------------------------------------------------------------

def test_signin_of_an_unregistered_name_refuses_and_changes_nothing(rig, capsys):
    assert signin(rig, "ghost") == 1
    assert "not registered" in capsys.readouterr().out
    assert rig.calls() == [] and not rig.cfg.exists() and hml.read_record(rig.entity) == {}


def test_signin_of_an_expired_entry_refuses_naming_the_lease(rig, capsys, monkeypatch):
    rig.register("old")
    real = hml.load_registry                       # the log refuses a back-dated link, so the clock moves instead
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: real(root, datetime.now(timezone.utc) + timedelta(days=31)))
    assert signin(rig, "old") == 1
    assert "not live (expired)" in capsys.readouterr().out
    assert rig.calls() == [] and hml.read_record(rig.entity) == {}


def test_signin_of_a_stdio_server_refuses_it_has_no_login(rig, capsys):
    rig.ck.link({"op": "create_grant", "grant": hm.build_entry(
        name="files", transport="stdio", identity_kind="dedicated", expires_at=hm.lease_expiry(datetime.now(timezone.utc)),
        command="/bin/sh")})
    assert signin(rig, "files") == 1
    assert "stdio" in capsys.readouterr().out and rig.calls() == []


def test_signin_without_a_hands_account_refuses_with_the_one_wording(rig, capsys, monkeypatch):
    def none(e, cfg=None):
        raise hands_login.HandsRefused(hands_login.NO_HANDS)
    monkeypatch.setattr(hands_login, "load_hands_identity", none)
    rig.register()
    assert signin(rig) == 1
    assert hands_login.NO_HANDS in capsys.readouterr().out and rig.calls() == []


def test_signin_on_linux_refuses_with_the_v1_wording(rig, capsys, monkeypatch):
    assert _REAL_LOGIN_REFUSAL is not None
    monkeypatch.setattr(hands_login, "login_refusal", _REAL_LOGIN_REFUSAL)
    monkeypatch.setattr(hands_login.platform, "system", lambda: "Linux")
    rig.register()
    assert signin(rig) == 1
    assert hands_login.LINUX_LOGIN_EDGE in capsys.readouterr().out
    assert rig.calls() == [] and hml.read_record(rig.entity) == {}


@pytest.mark.parametrize("op", ["signin", "remove"])
def test_a_hands_home_that_is_the_operators_own_is_refused_and_the_store_untouched(rig, capsys, monkeypatch, op):
    rig.register()
    monkeypatch.setattr(hml, "_operator_home", lambda: str(rig.hands_home))      # the collision: one home, two roles
    sentinel = rig.operator_home / ".claude" / "sentinel"
    rc = signin(rig) if op == "signin" else hml.cmd_remove(rig.entity, "linear")
    assert rc == 1 and "operator's own home" in capsys.readouterr().out
    assert rig.calls() == [] and sentinel.read_text() == "operator's own"
    assert not rig.cfg.exists()


def test_a_hands_root_account_is_refused(rig):
    h = HandsIdentity("root", 0, str(rig.hands_home), rig.hands_home / "ws")
    assert "root" in (hml.store_problem(h) or "")


@pytest.mark.parametrize("name", ["--help", "-x", "../etc", "a b", "", "x" * 80, ".hidden", "a/b"])
def test_a_server_name_that_could_be_an_option_or_a_path_is_refused(rig, name, capsys):
    assert signin(rig, name) == 1 and hml.cmd_remove(rig.entity, name) == 1
    assert rig.calls() == []


def test_a_secret_shaped_url_is_refused_and_never_echoed(rig, capsys, monkeypatch):
    bad = rig.entry("leaky", url="https://user:sk-live-abcdefghijklmnop0123@mcp.example/mcp")
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: ([bad], []))
    assert signin(rig, "leaky") == 1
    out = capsys.readouterr().out
    assert "registration rules" in out
    assert "sk-live" not in out and "abcdefghijklmnop" not in out and "user:" not in out
    assert rig.calls() == [] and hml.read_record(rig.entity) == {}


def test_a_secret_in_the_query_value_is_refused_without_the_value(rig, capsys, monkeypatch):
    bad = rig.entry("q", url="https://mcp.example/mcp?api_key=SUPERSECRETVALUE1")
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: ([bad], []))
    assert signin(rig, "q") == 1
    assert "SUPERSECRETVALUE1" not in capsys.readouterr().out and rig.calls() == []


def test_an_unreadable_registry_is_a_visible_refusal_not_an_empty_one(rig, capsys):
    rig.register()
    (rig.ck.root / "cockpit.db").write_bytes(b"not a database")
    assert signin(rig) == 1
    assert "authority log cannot be read" in capsys.readouterr().out and rig.calls() == []


def test_a_session_holding_the_hands_lock_refuses_signin(rig, capsys):
    rig.register()
    from levain import hands_live
    fd = hands_live.take_exclusive(rig.entity)
    assert fd is not None
    try:
        assert signin(rig) == 1
    finally:
        import os
        os.close(fd)
    assert "session of this entity is open" in capsys.readouterr().out and rig.mutating() == []


def test_a_server_the_account_already_holds_that_signin_did_not_add_is_not_taken_over(rig, capsys):
    rig.register()
    rig.cfg.parent.mkdir(parents=True)
    rig.cfg.write_text(json.dumps({"mcpServers": {"linear": {"type": "http", "url": "https://elsewhere.example/mcp"}}}))
    before = rig.cfg.read_text()
    assert signin(rig) == 1
    assert "did not add" in capsys.readouterr().out
    assert rig.mutating() == [] and rig.cfg.read_text() == before and hml.read_record(rig.entity) == {}


def test_remove_of_a_name_in_neither_the_record_nor_the_account_changes_nothing(rig, capsys):
    assert hml.cmd_remove(rig.entity, "ghost") == 0
    assert "nothing changed" in capsys.readouterr().out
    assert rig.mutating() == [] and not any(c.startswith("SWEEP") for c in rig.calls())


def test_remove_refuses_a_server_it_did_not_create(rig, capsys):
    rig.cfg.parent.mkdir(parents=True)
    rig.cfg.write_text(json.dumps({"mcpServers": {"hand-made": {"type": "http", "url": "https://x.example/mcp"}}}))
    before = rig.cfg.read_text()
    assert hml.cmd_remove(rig.entity, "hand-made") == 1
    assert "did not add" in capsys.readouterr().out
    assert rig.mutating() == [] and rig.cfg.read_text() == before


def test_remove_with_an_unreadable_record_changes_nothing(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    hml._record_path(rig.entity).write_text("{not json")
    rig.log.write_text("")
    assert hml.cmd_remove(rig.entity, "linear") == 1
    assert "cannot be read" in capsys.readouterr().out
    assert rig.mutating() == [] and "linear" in rig.servers()


def test_remove_that_cannot_read_the_accounts_config_changes_nothing(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    rig.cfg.write_text("{broken")
    rig.log.write_text("")
    assert hml.cmd_remove(rig.entity, "linear") == 1
    assert "nothing was changed" in capsys.readouterr().out and rig.mutating() == []


# --- the sign-in against the fake OAuth server -------------------------------------------------------------------

def test_signin_adds_pins_the_port_logs_in_as_hands_and_keeps_the_token_in_a_0600_file(rig, capsys):
    e = rig.register()
    assert signin(rig) == 0
    out = capsys.readouterr().out
    assert hands_login.LOGIN_NOTICE.split(".")[0] in out or hml.SIGNIN_NOTICE in out
    assert f"http://127.0.0.1:{rig.oauth.port}/authorize" in out           # the vendor's URL reached the operator
    assert "Needs authentication" not in out and "Connected" in out
    assert rig.servers()["linear"]["oauth"]["callbackPort"] == e["callback_port"]
    calls = rig.calls()
    assert calls[0] == f"mcp add --scope user --transport http --callback-port {e['callback_port']} linear {e['url']}"
    assert calls[1] == "mcp login --no-browser linear" and calls[2] == "mcp get linear"
    assert len(rig.oauth.hits) == 1 and rig.oauth.hits[0][0] == "/token"
    assert stat.S_IMODE(rig.cred.stat().st_mode) == 0o600
    assert json.loads(rig.cred.read_text())["mcpOAuth"]["linear"]["accessToken"] == "tok-pasted"
    # the operator side holds no token, the hands home is where everything ran, the operator's own home is never named
    assert "tok-pasted" not in out and "tok-pasted" not in hml._record_path(rig.entity).read_text()
    assert all(ln.startswith(str(rig.hands_home) + "|") for ln in rig.log.read_text().splitlines())
    assert str(rig.operator_home) not in rig.log.read_text()
    rec = hml.read_record(rig.entity)["linear"]
    assert rec["outcome"] == "login-exited-0" and rec["entry_id"] == e["id"] and rec["url"] == e["url"]


def test_a_second_signin_does_not_add_the_server_again(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    rig.log.write_text("")
    assert signin(rig) == 0
    assert not any(c.startswith("mcp add") for c in rig.calls())


def test_a_signin_whose_entry_url_moved_asks_for_a_remove_first(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    rig.cfg.write_text(json.dumps({"mcpServers": {"linear": {"type": "http", "url": "https://moved.example/mcp"}}}))
    rig.log.write_text("")
    assert signin(rig) == 1
    assert "hands mcp remove linear" in capsys.readouterr().out and rig.mutating() == []


def test_a_failed_add_leaves_no_record_and_no_server(rig, capsys):
    rig.register()
    rig.knobs["SHIM_ADD_FAIL"] = "1"
    assert signin(rig) == 1
    assert "mcp add exited 1" in capsys.readouterr().out
    assert hml.read_record(rig.entity) == {} and rig.servers() == {}


def test_a_failed_login_is_recorded_failed_and_can_be_removed(rig, capsys):
    rig.register()
    rig.knobs["SHIM_LOGIN_FAIL"] = "1"
    assert signin(rig) == 1
    assert hml.read_record(rig.entity)["linear"]["outcome"] == "failed" and not rig.cred.exists()
    del rig.knobs["SHIM_LOGIN_FAIL"]
    assert hml.cmd_remove(rig.entity, "linear") == 0
    assert rig.servers() == {} and hml.read_record(rig.entity) == {}


# --- remove ------------------------------------------------------------------------------------------------------

def test_remove_sweeps_then_logs_out_then_removes_and_says_the_revoke_is_the_providers(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    rig.log.write_text("")
    assert hml.cmd_remove(rig.entity, "linear") == 0
    out = capsys.readouterr().out
    calls = rig.calls()
    sweep = next(i for i, c in enumerate(calls) if c.startswith("SWEEP levain-hands"))
    assert sweep < calls.index("mcp logout linear") < calls.index("mcp remove --scope user linear")
    assert rig.servers() == {} and "linear" not in json.loads(rig.cred.read_text()).get("mcpOAuth", {})
    assert hml.read_record(rig.entity) == {}
    assert "LOCAL ONLY" in out and "127.0.0.1" in out and "tok-" not in out


def test_remove_needs_no_live_entry(rig, capsys, monkeypatch):
    rig.register()
    assert signin(rig) == 0
    monkeypatch.setattr(hml, "load_registry", lambda *a, **k: (_ for _ in ()).throw(AssertionError("registry read")))
    assert hml.cmd_remove(rig.entity, "linear") == 0 and rig.servers() == {}


def test_a_failed_logout_leaves_the_server_and_the_record_so_it_can_be_retried(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    rig.knobs["SHIM_LOGOUT_FAIL"] = "1"
    assert hml.cmd_remove(rig.entity, "linear") == 1
    assert "left in place" in capsys.readouterr().out
    assert "linear" in rig.servers() and "linear" in hml.read_record(rig.entity)
    del rig.knobs["SHIM_LOGOUT_FAIL"]
    assert hml.cmd_remove(rig.entity, "linear") == 0 and rig.servers() == {}


def test_a_second_remove_changes_nothing(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    assert hml.cmd_remove(rig.entity, "linear") == 0
    capsys.readouterr()
    rig.log.write_text("")
    assert hml.cmd_remove(rig.entity, "linear") == 0
    assert "nothing changed" in capsys.readouterr().out and rig.mutating() == []


# --- the argv, through the production prefix --------------------------------------------------------------------

@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_the_vendor_argv_runs_as_hands_with_the_pinned_home_and_the_name_last(monkeypatch, system):
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    h = HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))
    e = {"name": "linear", "url": "https://mcp.linear.example/mcp", "callback_port": 49300}
    for argv in (hml.add_argv(h, e), hml.login_argv(h, "linear"), hml.logout_argv(h, "linear"),
                 hml.remove_argv(h, "linear"), hml.get_argv(h, "linear")):
        assert argv[:6] == [confinement.SUDO, "-n", "-u", "levain-hands", "/usr/bin/env", "-i"]
        assert "CLAUDE_CONFIG_DIR=/Users/levain-hands/.claude" in argv and "HOME=/Users/levain-hands" in argv
        assert "claude" in argv and argv[argv.index("claude") + 1] == "mcp"
        assert all("/Users/phillipclapham" not in a for a in argv)
    assert hml.add_argv(h, e)[-9:] == ["add", "--scope", "user", "--transport", "http", "--callback-port", "49300",
                                       "linear", "https://mcp.linear.example/mcp"]
    assert hml.login_argv(h, "linear")[-3:] == ["login", "--no-browser", "linear"]


def test_the_parser_names_the_verbs_and_remove_takes_no_data_root():
    p = build_parser()
    a = p.parse_args(["hands", "mcp", "signin", "linear", "--path", "/e", "--data-root", "/d"])
    assert (a.hands_mcp_cmd, a.name, a.data_root) == ("signin", "linear", Path("/d"))
    assert p.parse_args(["hands", "mcp", "list"]).hands_mcp_cmd == "list"
    with pytest.raises(SystemExit):
        p.parse_args(["hands", "mcp", "remove", "linear", "--data-root", "/d"])


# --- the record is in a directory the entity's hands can write (L3 r1 F1) ------------------------------------------

def _victim(rig, text='{"operator": "own"}'):
    v = rig.operator_home / "victim.json"
    v.write_text(text)
    return v


@pytest.mark.parametrize("record", [hml._record_path, lambda e: hands_login._outcome_path(e)])
def test_a_symlink_planted_at_the_old_predictable_temp_is_never_opened(rig, record, capsys):
    """The temp was ``<record>.<pid>.tmp`` written by write_text: a planted symlink there truncated its target."""
    import os
    victim = _victim(rig)
    path = record(rig.entity)
    os.symlink(victim, path.with_name(path.name + f".{os.getpid()}.tmp"))
    if record is hml._record_path:
        rig.register()
        assert signin(rig) == 0
    else:
        hands_login.record_outcome(rig.entity, "claude", "logged-in")
        assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "logged-in"
    assert victim.read_text() == '{"operator": "own"}'
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]


def test_a_guessed_temp_name_is_refused_by_the_exclusive_open_never_followed(rig, monkeypatch):
    import os
    victim = _victim(rig)
    monkeypatch.setattr(hands_login.secrets, "token_hex", lambda n: "guessed")
    path = hml._record_path(rig.entity)
    os.symlink(victim, path.with_name(path.name + ".guessed.tmp"))
    with pytest.raises(hml.HandsMcpError, match="could not be written"):
        hml._write_record(rig.entity, "linear", {"url": "u"})
    assert victim.read_text() == '{"operator": "own"}' and not path.exists()
    assert path.with_name(path.name + ".guessed.tmp").is_symlink()      # the plant is not ours to remove


def test_the_record_temp_is_0600_and_the_record_is_replaced_by_a_rename(rig, monkeypatch):
    import os
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, stat.S_IMODE(os.lstat(src).st_mode)))
        return real(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    old = os.umask(0)                              # the mode comes from the open, not the umask
    try:
        hml._write_record(rig.entity, "linear", {"url": "u"})
    finally:
        os.umask(old)
    (name, mode), = seen
    assert name.startswith("hands-mcp.json.") and name.endswith(".tmp") and str(os.getpid()) not in name
    assert mode == 0o600 and hml.read_record(rig.entity) == {"linear": {"url": "u"}}


def test_a_symlinked_record_is_refused_by_name_and_never_followed(rig, capsys):
    import os
    victim = _victim(rig, "{}")
    os.symlink(victim, hml._record_path(rig.entity))
    rig.register()
    assert signin(rig) == 1
    out = capsys.readouterr().out
    assert "symlink" in out and "not followed" in out
    assert rig.mutating() == [] and victim.read_text() == "{}"
    assert hml._record_path(rig.entity).is_symlink()


def test_a_fifo_at_the_record_is_refused_as_not_a_regular_file_and_never_hangs(rig):
    import os
    os.mkfifo(hml._record_path(rig.entity))
    with pytest.raises(hml.HandsMcpError, match="not a regular file"):
        hml.read_record(rig.entity)


def test_the_record_is_not_a_crown_jewel_and_neither_is_its_sibling():
    """Decided, not missed: the own-store write-deny is anneal's memory files only; the operator-side records
    (hands-login.json, hands-mcp.json) are refused at read and never followed at write instead."""
    assert hml.RECORD_FILE not in confinement._OWN_MEMORY_FILENAMES
    assert hands_login.OUTCOME_FILE not in confinement._OWN_MEMORY_FILENAMES


# --- a read-only verb never stops the hands account (L3 r1 F9) -----------------------------------------------------

def test_list_whose_read_runs_out_never_sweeps_the_hands_account(rig, capsys, monkeypatch):
    import subprocess
    rig.register()
    assert signin(rig) == 0
    rig.log.write_text("")

    def runs_out(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(hands_login.hands_live, "run_bounded", runs_out)
    assert hml.cmd_list(rig.entity, rig.ck.root) == 1
    assert "config: unknown" in capsys.readouterr().out
    assert not any(c.startswith("SWEEP") for c in rig.calls())


# --- a sign-in that raises leaves "failed", never "added" (L3 r1 F4) -----------------------------------------------

@pytest.mark.parametrize("exc", [KeyboardInterrupt, hands_login.HandsLoginError])
def test_a_login_that_raises_is_recorded_failed(rig, monkeypatch, exc):
    e = rig.register()

    def raises(*a, **k):
        raise exc("ran out")
    monkeypatch.setattr(hands_login, "_run_login", raises)
    with pytest.raises(exc):
        hml.signin(rig.h, e, rig.entity)
    assert hml.read_record(rig.entity)["linear"]["outcome"] == "failed"


def test_a_signin_whose_held_callback_port_moved_asks_for_a_remove_first(rig, capsys):
    rig.register()
    assert signin(rig) == 0
    capsys.readouterr()
    doc = json.loads(rig.cfg.read_text())
    doc["mcpServers"]["linear"]["oauth"]["callbackPort"] = 1
    rig.cfg.write_text(json.dumps(doc))
    rig.log.write_text("")
    assert signin(rig) == 1
    assert "callback port" in capsys.readouterr().out and rig.mutating() == []


# --- 1106-1 miscdeny: the hands home is the operator's home, spelled in another case (casesweep row 12) ---------------

def _case_alias(p: Path) -> Path:
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same directory (a
    case-insensitive one: macOS APFS by default); else the test is skipped."""
    import os
    alias = Path(str(p.resolve()).swapcase())
    if str(alias) == str(p.resolve()) or not alias.exists() or not os.path.samefile(alias, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return alias


def _hands_at(home) -> HandsIdentity:
    import os
    return HandsIdentity("levain-hands", os.getuid() or 1, str(home), Path(str(home)) / "ws")


@pytest.mark.parametrize("which", ["same", "inside", "above"])
def test_a_hands_home_that_is_a_case_alias_of_the_operators_is_refused(tmp_path, monkeypatch, which):
    """casesweep row 12: `os.path.realpath` keeps the spelling it is given, so a hands home written `/USERS/<me>` compared
    unequal to the operator's `/Users/<me>` and `signin`/`remove` would have run against the operator's own store.
    DENY direction (`pathid.maybe_inside` both ways: the same, below, or above). Config-only, so not reachable by a
    plausible caller; the fix is one call. Needs a case-insensitive volume (macOS APFS): on a case-sensitive one this
    skips, so a Linux CI run never reds it; the injected-fold test below runs there."""
    op = tmp_path.resolve() / "operator"
    (op / "sub").mkdir(parents=True)
    monkeypatch.setattr(hml, "_operator_home", lambda: str(op))
    hands_home = {"same": _case_alias(op), "inside": _case_alias(op / "sub"), "above": _case_alias(op.parent)}[which]
    assert "operator's own home" in (hml.store_problem(_hands_at(hands_home)) or "")


def test_a_hands_home_beside_the_operators_is_accepted_whatever_its_spelling(tmp_path, monkeypatch):
    """Control: a real second home, and its alias, are not the operator's (and the vendor config home inside it holds)."""
    op = tmp_path.resolve() / "operator"
    hh = tmp_path.resolve() / "hands"
    op.mkdir()
    hh.mkdir()
    monkeypatch.setattr(hml, "_operator_home", lambda: str(op))
    assert hml.store_problem(_hands_at(hh)) is None
    assert hml.store_problem(_hands_at(_case_alias(hh))) is None
    (tmp_path / "hands-camp").mkdir()                                       # a sibling whose name starts with the hands'
    assert hml.store_problem(_hands_at(tmp_path / "operatorcamp")) is None


def test_the_hands_home_check_with_an_injected_case_fold(tmp_path, monkeypatch):
    """The same logic where no alias exists (a Linux CI run): `pathid._fs_spelling` is replaced by a case-insensitive
    walk over the real tree, standing in for a case-insensitive volume."""
    from levain import pathid
    from tests.test_pathid import _case_insensitive_spelling
    op = tmp_path.resolve() / "operator"
    hh = tmp_path.resolve() / "hands"
    (op / "sub").mkdir(parents=True)
    hh.mkdir()
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    monkeypatch.setattr(hml, "_operator_home", lambda: str(op))
    for spelled in (op.with_name("OPERATOR"), op.with_name("OPERATOR") / "sub", op.parent.with_name(op.parent.name.upper())):
        assert "operator's own home" in (hml.store_problem(_hands_at(spelled)) or ""), spelled
    assert "operator's own home" in (hml.store_problem(_hands_at(op)) or "")                    # canonical: as before
    assert hml.store_problem(_hands_at(hh)) is None and hml.store_problem(_hands_at(hh.with_name("HANDS"))) is None
