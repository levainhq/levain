"""`levain hands login|status` (levain 15 V1): the hands account's own vendor login. The vendor CLIs are shell shims a
test puts first on PATH; the sudo prefix is the only thing replaced (as in tests/test_ws_git.py), so the hands-side
steps run as the current user. No real login, no user created, no keychain, no live app. Cross-user ownership
(a file owned by the hands uid) is the macOS VM / CI run."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from levain.cli import build_parser
from levain.firing import confinement, hands_login
from levain.firing.confinement import HANDS_PATH, HandsIdentity
from levain.firing.hands_login import HandsLoginError

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

CLAUDE_SHIM = """#!/bin/sh
echo "$HOME|$CLAUDE_CONFIG_DIR|$PATH|$*" >> "$SHIM_LOG"
case "$1 $2" in
  "auth login")
    echo "Open https://claude.ai/oauth/authorize?code=true&client_id=x to sign in"
    echo "Paste code here if prompted >"
    umask 077
    mkdir -p "$CLAUDE_CONFIG_DIR"
    echo '{"claudeAiOauth":{}}' > "$CLAUDE_CONFIG_DIR/.credentials.json"
    exit 0;;
  "auth status")
    if [ -f "$CLAUDE_CONFIG_DIR/.credentials.json" ]; then
      echo '{"loggedIn": true, "email": "hands@example.test", "subscriptionType": "max"}'; exit 0
    fi
    echo '{"loggedIn": false}'; exit 1;;
esac
exit 2
"""

CODEX_SHIM = """#!/bin/sh
echo "$HOME|$CODEX_HOME|$PATH|$*" >> "$SHIM_LOG"
case "$1 $2" in
  "login --device-auth")
    echo "Visit https://auth.openai.com/codex/device and enter ABCD-12345"
    grep -q '^cli_auth_credentials_store = "file"$' "$CODEX_HOME/config.toml" || { echo "no pin" >&2; exit 3; }
    umask 077
    echo '{"tokens":{}}' > "$CODEX_HOME/auth.json"
    exit 0;;
  "login status")
    if [ -f "$CODEX_HOME/auth.json" ]; then echo "Logged in using ChatGPT"; exit 0; fi
    echo "Not logged in"; exit 1;;
esac
exit 2
"""


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("claude", CLAUDE_SHIM), ("codex", CODEX_SHIM)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    hands_home = tmp_path / "hands-home"
    hands_home.mkdir()
    operator_home = tmp_path / "operator-home"
    for d in (".claude", ".codex"):
        (operator_home / d).mkdir(parents=True)
        (operator_home / d / "sentinel").write_text("operator's own")
    monkeypatch.setenv("HOME", str(operator_home))
    log = tmp_path / "shim.log"
    h = HandsIdentity("levain-hands", os.getuid(), str(hands_home), tmp_path / "ws")

    def fake_prefix(hands, vendor):
        env = hands_login.vendor_env(hands, vendor)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["SHIM_LOG"] = str(log)
        return ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items())]

    monkeypatch.setattr(hands_login, "_launch_prefix", fake_prefix)
    return h, hands_home, operator_home, log


# --- argv / env (the production prefix, not the shim) ---------------------------------------------------------

@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("vendor,var,sub", [("claude", "CLAUDE_CONFIG_DIR", ".claude"), ("codex", "CODEX_HOME", ".codex")])
def test_login_argv_runs_as_hands_with_pinned_home_and_fixed_path(monkeypatch, system, vendor, var, sub):
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    h = HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))
    argv = hands_login.login_argv(h, vendor)
    assert argv[:6] == [confinement.SUDO, "-n", "-u", "levain-hands", "/usr/bin/env", "-i"]
    assigned = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv[6:] if "=" in a and a.split("=", 1)[0].isupper()}
    assert assigned[var] == f"/Users/levain-hands/{sub}"
    assert assigned["HOME"] == "/Users/levain-hands"
    assert assigned["PATH"] == HANDS_PATH
    assert set(assigned) == {"HOME", "PATH", "LANG", "TERM", "USER", "LOGNAME", var}
    assert argv[-len(hands_login._VENDOR[vendor]["login"]):] == list(hands_login._VENDOR[vendor]["login"])
    # one login command each, never the operator's home
    assert "--device-auth" in argv if vendor == "codex" else argv[-3:] == ["claude", "auth", "login"]
    assert all(os.path.expanduser("~") not in a for a in argv)
    # the same shape for status, with the same pinned home
    sargv = hands_login.status_argv(h, vendor)
    assert f"{var}=/Users/levain-hands/{sub}" in sargv and sargv[:6] == argv[:6]


def test_unknown_vendor_is_refused():
    h = HandsIdentity("u", 1, "/h", Path("/w"))
    with pytest.raises(HandsLoginError):
        hands_login.login_argv(h, "gemini")


def test_the_codex_pin_is_the_pair_a_slot_writes():
    from levain.slot import render
    text = Path(render.__file__).read_text()
    assert f'"{hands_login.CODEX_STORE_KEY}": "file"' in text
    assert hands_login.CODEX_STORE_LINE == 'cli_auth_credentials_store = "file"\n'


# --- the runner against shims ---------------------------------------------------------------------------------

def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_claude_login_writes_a_0600_file_in_the_hands_home_and_status_parses(rig):
    h, hands_home, operator_home, log = rig
    said: list[str] = []
    res = hands_login.run_login(h, "claude", emit=said.append)
    assert res.returncode == 0
    assert res.urls == ["https://claude.ai/oauth/authorize?code=true&client_id=x"]
    assert any("Open https://claude.ai" in s for s in said)
    cred = hands_home / ".claude" / ".credentials.json"
    assert cred.is_file() and _mode(cred) == 0o600 and cred.stat().st_uid == os.getuid()
    assert _mode(hands_home / ".claude") == 0o700
    st = hands_login.run_status(h, "claude")
    assert st.logged_in and st.account == "hands@example.test / max"
    assert st.credential == f"{os.environ.get('USER') or _owner()} 600" or st.credential.endswith(" 600")
    # the child saw the hands home and the fixed PATH tail, never the operator's home
    home, cfg, path, args = log.read_text().splitlines()[0].split("|", 3)
    assert home == str(hands_home) and cfg == str(hands_home / ".claude") and path.endswith(HANDS_PATH)
    assert args == "auth login"
    assert str(operator_home) not in log.read_text()


def _owner() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def test_codex_login_pins_the_file_store_then_signs_in(rig):
    h, hands_home, operator_home, log = rig
    res = hands_login.run_login(h, "codex", emit=lambda s: None)
    assert res.returncode == 0 and res.codes == ["ABCD-12345"]
    assert res.urls == ["https://auth.openai.com/codex/device"]
    cfg = hands_home / ".codex" / "config.toml"
    assert cfg.read_text() == hands_login.CODEX_STORE_LINE
    auth = hands_home / ".codex" / "auth.json"
    assert auth.is_file() and _mode(auth) == 0o600
    st = hands_login.run_status(h, "codex")
    assert st.logged_in and st.account == "Logged in using ChatGPT"
    assert str(operator_home) not in log.read_text()


def test_codex_config_keeps_existing_content_and_refuses_a_conflicting_store(rig):
    h, hands_home, _, _ = rig
    d = hands_home / ".codex"
    d.mkdir()
    (d / "config.toml").write_text('model = "x"\n[projects."/a"]\ntrust_level = "untrusted"\n')
    hands_login.ensure_config_home(h, "codex")
    text = (d / "config.toml").read_text()
    assert text.startswith(hands_login.CODEX_STORE_LINE) and text.endswith('trust_level = "untrusted"\n')
    hands_login.ensure_config_home(h, "codex")                      # idempotent
    assert (d / "config.toml").read_text() == text
    (d / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    with pytest.raises(HandsLoginError, match="other than"):
        hands_login.ensure_config_home(h, "codex")
    assert (d / "config.toml").read_text() == 'cli_auth_credentials_store = "keyring"\n'


def test_status_before_login_is_not_signed_in(rig):
    h, *_ = rig
    for v in ("claude", "codex"):
        st = hands_login.run_status(h, v)
        assert not st.logged_in and st.credential is None


def test_nothing_of_the_operators_is_read_or_changed(rig):
    h, hands_home, operator_home, _ = rig
    before = {p: p.read_text() for p in operator_home.rglob("sentinel")}
    for v in ("claude", "codex"):
        hands_login.run_login(h, v, emit=lambda s: None)
    assert {p: p.read_text() for p in operator_home.rglob("sentinel")} == before
    assert sorted(p.name for p in operator_home.rglob("*") if p.is_file()) == ["sentinel", "sentinel"]


def test_a_failed_login_exit_is_reported(rig, capsys, monkeypatch):
    h, hands_home, *_ = rig
    monkeypatch.setattr(hands_login, "load_hands_identity", lambda e, cfg=None: h)
    (hands_home / ".codex").mkdir()
    (hands_home / ".codex" / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    assert hands_login.cmd_hands_login("/x", "codex") == 1
    assert "other than" in capsys.readouterr().out


def test_login_verb_end_to_end(rig, capsys, monkeypatch):
    h, *_ = rig
    monkeypatch.setattr(hands_login, "load_hands_identity", lambda e, cfg=None: h)
    assert hands_login.cmd_hands_login("/x", "claude") == 0
    out = capsys.readouterr().out
    assert "hands claude: hands@example.test / max" in out
    assert "Anything that runs as the hands account can use this login" in out
    assert "local to this machine" in out
    assert hands_login.cmd_hands_status("/x", None) == 0
    out = capsys.readouterr().out
    assert "hands codex: not signed in" in out and "hands claude: hands@example.test" in out


# --- refusal, parsing, the verb ------------------------------------------------------------------------------

def test_no_hands_account_is_one_refusal_for_both_vendors(tmp_path, capsys):
    msgs = []
    for v in ("claude", "codex"):
        assert hands_login.cmd_hands_login(tmp_path, v) == 1
        msgs.append(capsys.readouterr().out)
    assert msgs[0] == msgs[1] == f"hands login: {hands_login.NO_HANDS}\n"
    assert hands_login.cmd_hands_status(tmp_path, None) == 1
    assert hands_login.NO_HANDS in capsys.readouterr().out


def test_hands_for_is_untouched_and_vendor_blind():
    import inspect
    src = inspect.getsource(confinement.hands_for)
    assert "claude" not in src and "codex" not in src and "vendor" not in src


@pytest.mark.parametrize("vendor,rc,text,want", [
    ("claude", 0, '{"loggedIn": true, "email": "a@b.c", "orgName": "Org"}', (True, "a@b.c / Org")),
    ("claude", 1, '{"loggedIn": false}', (False, "")),
    ("claude", 1, "Not logged in", (False, "")),
    ("claude", 0, "Logged in as a@b.c", (True, "Logged in as a@b.c")),
    ("codex", 0, "Logged in using ChatGPT\n", (True, "Logged in using ChatGPT")),
    ("codex", 1, "Not logged in", (False, "")),
    ("codex", 1, "", (False, "")),
])
def test_parse_status(vendor, rc, text, want):
    assert hands_login.parse_status(vendor, rc, text) == want


def test_surface_dedups_urls_and_codes():
    urls, codes = hands_login.surface("go https://a.test/x, then https://a.test/x. code WXYZ-1234 / WXYZ-1234")
    assert urls == ["https://a.test/x"] and codes == ["WXYZ-1234"]


def test_cli_registers_login_and_status():
    p = build_parser()
    a = p.parse_args(["hands", "login", "codex", "--path", "/e"])
    assert (a.hands_cmd, a.vendor, str(a.path)) == ("login", "codex", "/e")
    b = p.parse_args(["hands", "status"])
    assert (b.hands_cmd, b.vendor) == ("status", None)
    with pytest.raises(SystemExit):
        p.parse_args(["hands", "login", "gemini"])
