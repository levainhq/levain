"""`levain hands login|status` (levain 15 V1): the hands account's own vendor login. The vendor CLIs are shell shims a
test puts first on PATH; the sudo prefix is the only thing replaced (as in tests/test_ws_git.py), so the hands-side
steps run as the current user. No real login, no user created, no keychain, no live app. Cross-user ownership
(a file owned by the hands uid) is the macOS VM / CI run.

Shim knobs (environment the fake prefix passes): SHIM_HANG (the login leaves a child holding the output open),
SHIM_KEYSTORE (the login is kept in that file, not the credential file: a non-file store), SHIM_LOGOUT_FAIL /
SHIM_LOGOUT_NOOP / SHIM_LOGOUT_HANG / SHIM_LEAVE (logout branches). The sweep is a recorder that also kills the
pids a hanging shim wrote, as the real sweep kills every process of the hands user."""
from __future__ import annotations

import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from levain import hands_live
from levain.cli import build_parser
from levain.firing import confinement, hands_login
from levain.firing.confinement import HANDS_PATH, HandsIdentity
from levain.firing.hands_login import HandsLoginError

#: The module's own refusal, kept before any rig replaces it (None on a build without one).
_REAL_LOGIN_REFUSAL = getattr(hands_login, "login_refusal", None)

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

CLAUDE_SHIM = """#!/bin/sh
echo "$HOME|$CLAUDE_CONFIG_DIR|$PATH|$*" >> "$SHIM_LOG"
CRED="$CLAUDE_CONFIG_DIR/.credentials.json"
case "$1 $2" in
  "auth login")
    echo "Open https://claude.ai/oauth/authorize?code=true&client_id=x to sign in"
    printf "Paste code here if prompted >"
    if [ -n "$SHIM_HANG" ]; then sleep 8 & echo $! > "$SHIM_PIDS"; wait; fi
    umask 077
    mkdir -p "$CLAUDE_CONFIG_DIR"
    if [ -n "$SHIM_KEYSTORE" ]; then echo k > "$SHIM_KEYSTORE"; else echo '{"claudeAiOauth":{}}' > "$CRED"; fi
    exit 0;;
  "auth logout")
    if [ -n "$SHIM_LOGOUT_HANG" ]; then sleep 8 & echo $! > "$SHIM_PIDS"; wait; fi
    if [ -n "$SHIM_LOGOUT_FAIL" ]; then echo "logout failed: keychain locked" >&2; exit 1; fi
    if [ -n "$SHIM_LOGOUT_NOOP" ]; then exit 0; fi
    [ -n "$SHIM_LEAVE" ] || rm -f "$CRED"
    if [ -n "$SHIM_KEYSTORE" ]; then rm -f "$SHIM_KEYSTORE"; fi
    exit 0;;
  "auth status")
    if [ -f "$CRED" ] || { [ -n "$SHIM_KEYSTORE" ] && [ -f "$SHIM_KEYSTORE" ]; }; then
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
  "logout ")
    if [ -n "$SHIM_LOGOUT_FAIL" ]; then echo "logout failed" >&2; exit 1; fi
    [ -n "$SHIM_LEAVE" ] || rm -f "$CODEX_HOME/auth.json"
    exit 0;;
  "login status")
    if [ -f "$CODEX_HOME/auth.json" ]; then echo "Logged in using ChatGPT"; exit 0; fi
    echo "Not logged in"; exit 1;;
esac
exit 2
"""

#: The prefix when the operator's sudo rule for the hands user is missing: every run as hands fails like sudo -n.
BROKEN_PREFIX = ["/bin/sh", "-c", 'echo "sudo: a password is required" >&2; exit 1', "sudo"]


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for name, body in (("claude", CLAUDE_SHIM), ("codex", CODEX_SHIM)):
            p = self.bindir / name
            p.write_text(body)
            p.chmod(0o755)
        self.hands_home = tmp_path / "hands-home"
        self.hands_home.mkdir()
        self.operator_home = tmp_path / "operator-home"
        for d in (".claude", ".codex"):
            (self.operator_home / d).mkdir(parents=True)
            (self.operator_home / d / "sentinel").write_text("operator's own")
        monkeypatch.setenv("HOME", str(self.operator_home))
        self.log = tmp_path / "shim.log"
        self.pids = tmp_path / "shim.pids"
        self.entity = tmp_path / "entity"
        (self.entity / ".levain").mkdir(parents=True)
        self.h = HandsIdentity("levain-hands", os.getuid(), str(self.hands_home), tmp_path / "ws")
        self.knobs: dict[str, str] = {}
        self.broken = False
        monkeypatch.setattr(hands_login, "_launch_prefix", self.prefix)
        monkeypatch.setattr(hands_live, "sweep_hands_user", self.sweep)
        monkeypatch.setattr(hands_login, "load_hands_identity", lambda e, cfg=None: self.h)
        monkeypatch.setattr(hands_login, "login_refusal", lambda: None, raising=False)

    def prefix(self, hands, vendor):
        if self.broken:
            return list(BROKEN_PREFIX)
        env = hands_login.vendor_env(hands, vendor)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["SHIM_LOG"] = str(self.log)
        env["SHIM_PIDS"] = str(self.pids)
        env.update(self.knobs)
        return ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items())]

    def sweep(self, user, *, uid, timeout=5.0):
        fd = hands_live.take_exclusive(self.entity)
        held_by_caller = fd is None
        if fd is not None:
            os.close(fd)
        with open(self.log, "a") as f:
            f.write(f"SWEEP {user} {uid} lock-held={held_by_caller}\n")
        if self.pids.exists():
            for pid in self.pids.read_text().split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        return None

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [ln.split("|", 3)[-1] if "|" in ln else ln for ln in self.log.read_text().splitlines()]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def _login(r: Rig, vendor: str, **kw):
    return hands_login.login(r.h, vendor, r.entity, emit=kw.pop("emit", lambda s: None), **kw)


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
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0
    assert res.urls == ["https://claude.ai/oauth/authorize?code=true&client_id=x"]
    assert any("Open https://claude.ai" in s for s in said)
    cred = rig.hands_home / ".claude" / ".credentials.json"
    assert cred.is_file() and _mode(cred) == 0o600 and cred.stat().st_uid == os.getuid()
    assert _mode(rig.hands_home / ".claude") == 0o700
    assert st.logged_in and st.state == "in" and st.account == "hands@example.test / max"
    assert st.credential == f"{_owner()} 600"
    # the child saw the hands home and the fixed PATH tail, never the operator's home
    home, cfg, path, args = rig.log.read_text().splitlines()[0].split("|", 3)
    assert home == str(rig.hands_home) and cfg == str(rig.hands_home / ".claude") and path.endswith(HANDS_PATH)
    assert args == "auth login"
    assert str(rig.operator_home) not in rig.log.read_text()


def _owner() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def test_codex_login_pins_the_file_store_then_signs_in(rig):
    res, st = _login(rig, "codex")
    assert res.returncode == 0 and res.codes == ["ABCD-12345"]
    assert res.urls == ["https://auth.openai.com/codex/device"]
    cfg = rig.hands_home / ".codex" / "config.toml"
    assert cfg.read_text() == hands_login.CODEX_STORE_LINE and _mode(cfg) == 0o600
    auth = rig.hands_home / ".codex" / "auth.json"
    assert auth.is_file() and _mode(auth) == 0o600
    assert st.logged_in and st.account == "Logged in using ChatGPT"
    assert str(rig.operator_home) not in rig.log.read_text()


def _ensure(r: Rig, vendor: str = "codex") -> None:
    hands_login.ensure_config_home(r.h, vendor, entity_dir=r.entity)


def test_codex_config_keeps_existing_content_and_refuses_a_conflicting_store(rig):
    d = rig.hands_home / ".codex"
    d.mkdir(mode=0o700)
    (d / "config.toml").write_text('model = "x"\n[projects."/a"]\ntrust_level = "untrusted"\n')
    _ensure(rig)
    text = (d / "config.toml").read_text()
    assert text.startswith(hands_login.CODEX_STORE_LINE) and text.endswith('trust_level = "untrusted"\n')
    _ensure(rig)                                                     # idempotent
    assert (d / "config.toml").read_text() == text
    (d / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    with pytest.raises(HandsLoginError, match="other than"):
        _ensure(rig)
    assert (d / "config.toml").read_text() == 'cli_auth_credentials_store = "keyring"\n'


def test_status_before_login_is_not_signed_in(rig):
    for v in ("claude", "codex"):
        st = hands_login.run_status(rig.h, v, entity_dir=rig.entity)
        assert st.state == "out" and not st.logged_in and st.credential is None


def test_nothing_of_the_operators_is_read_or_changed(rig):
    before = {p: p.read_text() for p in rig.operator_home.rglob("sentinel")}
    for v in ("claude", "codex"):
        _login(rig, v)
    assert {p: p.read_text() for p in rig.operator_home.rglob("sentinel")} == before
    assert sorted(p.name for p in rig.operator_home.rglob("*") if p.is_file()) == ["sentinel", "sentinel"]


def test_a_refused_codex_config_is_reported(rig, capsys):
    (rig.hands_home / ".codex").mkdir(mode=0o700)
    (rig.hands_home / ".codex" / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    assert hands_login.cmd_hands_login(rig.entity, "codex") == 1
    assert "other than" in capsys.readouterr().out


def test_a_nonzero_login_exit_is_reported_and_recorded(rig, capsys, monkeypatch):
    monkeypatch.setitem(hands_login._VENDOR["claude"], "login", ("claude", "nope"))
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    assert "the login exited 2" in capsys.readouterr().out
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "failed"


def test_login_verb_end_to_end(rig, capsys):
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 0
    out = capsys.readouterr().out
    assert "hands claude: hands@example.test / max" in out
    assert "Anything that runs as the hands account can use this login" in out
    assert "local to this machine" in out
    assert hands_login.cmd_hands_status(rig.entity, None) == 0
    out = capsys.readouterr().out
    assert "hands codex: not signed in" in out and "hands claude: hands@example.test" in out


# --- L3 r1 reproductions (each fails on V1/V2 as landed in eda2f593) -------------------------------------------

def test_linux_login_is_refused_up_front_and_launches_nothing(rig, capsys, monkeypatch):
    """V1 codex HIGH 1: the stated 1.0 edge, refused before any launch."""
    if _REAL_LOGIN_REFUSAL is not None:
        monkeypatch.setattr(hands_login, "login_refusal", _REAL_LOGIN_REFUSAL)
    monkeypatch.setattr(hands_login.platform, "system", lambda: "Linux")
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "Linux: the hands account has no egress path for a vendor login yet" in out
    assert rig.calls() == [] and not (rig.hands_home / ".claude").exists()
    assert "claude" not in hands_login.read_outcomes(rig.entity)       # a refusal, not an outcome


def test_a_login_that_outlives_its_deadline_is_killed_and_swept(rig, monkeypatch):
    """V1 codex HIGH 2 / complement MED 1: the vendor's descendant holds the output open past a killed sudo; the
    read is bounded by the deadline and the sweep runs, under the lock. Also complement MED 2: a prompt with no
    newline reaches the person before the login ends."""
    monkeypatch.setattr(hands_login, "LOGIN_TIMEOUT", 1.0)
    rig.knobs["SHIM_HANG"] = "1"
    said: list[str] = []
    monkeypatch.setattr(hands_login, "_write_out", said.append, raising=False)
    t0 = time.monotonic()
    rc = hands_login.cmd_hands_login(rig.entity, "claude")
    took = time.monotonic() - t0
    assert rc == 1 and took < 5.0, took
    assert any("Paste code here if prompted >" in s for s in said)
    sweeps = [c for c in rig.calls() if c.startswith("SWEEP")]
    assert sweeps == ["SWEEP levain-hands %d lock-held=True" % os.getuid()]
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "failed"
    pid = int(rig.pids.read_text().split()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_takes_the_hands_lock(rig, capsys):
    """V1 codex MED 3 / V2 codex HIGH 3: a login while a session holds the lock is refused and runs nothing."""
    fd = hands_live.take_exclusive(rig.entity)
    try:
        assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    finally:
        os.close(fd)
    assert "a session of this entity is open" in capsys.readouterr().out
    assert rig.calls() == []


def test_a_key_inside_a_table_is_not_the_top_level_pin(rig):
    """V1 codex MED 4 / complement LOW 4: the key under a table counts as absent; the pin is added at the top."""
    d = rig.hands_home / ".codex"
    d.mkdir(mode=0o700)
    body = '[profiles.x]\ncli_auth_credentials_store = "file"\n'
    (d / "config.toml").write_text(body)
    _ensure(rig)
    assert (d / "config.toml").read_text() == hands_login.CODEX_STORE_LINE + body


@pytest.mark.parametrize("body", ['"cli_auth_credentials_store" = "file"\n',
                                  "'cli_auth_credentials_store' = 'file'  # pinned\n"])
def test_a_quoted_key_or_commented_value_is_the_pin(rig, body):
    """V1 codex MED 4: a quoted key is the same key (a second, unquoted one would make Codex reject the file)."""
    d = rig.hands_home / ".codex"
    d.mkdir(mode=0o700)
    (d / "config.toml").write_text(body)
    _ensure(rig)
    assert (d / "config.toml").read_text() == body


def test_config_toml_is_replaced_by_rename_keeping_its_mode(rig):
    """V1 codex MED 5 / complement LOW 3: never a truncating write in place."""
    d = rig.hands_home / ".codex"
    d.mkdir(mode=0o700)
    cfg = d / "config.toml"
    cfg.write_text('model = "x"\n')
    cfg.chmod(0o640)
    ino = cfg.stat().st_ino
    _ensure(rig)
    assert cfg.read_text() == hands_login.CODEX_STORE_LINE + 'model = "x"\n'
    assert cfg.stat().st_ino != ino and _mode(cfg) == 0o640
    assert sorted(p.name for p in d.iterdir()) == ["config.toml"]


def test_an_unreadable_config_fails_the_login_and_keeps_the_file(rig, capsys):
    """V1 codex MED 5: a failed read is never treated as an empty file."""
    d = rig.hands_home / ".codex"
    d.mkdir(mode=0o700)
    cfg = d / "config.toml"
    cfg.write_text('model = "x"\n')
    cfg.chmod(0o000)
    try:
        assert hands_login.cmd_hands_login(rig.entity, "codex") == 1
        assert "could not read" in capsys.readouterr().out
    finally:
        cfg.chmod(0o600)
    assert cfg.read_text() == 'model = "x"\n'


def test_an_existing_config_home_has_its_mode_repaired(rig):
    """V1 codex MED 7: mkdir -p never repaired a 755 directory."""
    d = rig.hands_home / ".codex"
    d.mkdir()
    d.chmod(0o755)
    _login(rig, "codex")
    assert _mode(d) == 0o700


def test_a_symlinked_config_home_is_refused(rig, tmp_path, capsys):
    """V1 codex MED 7: the config home may not resolve outside the hands home."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (rig.hands_home / ".claude").symlink_to(elsewhere)
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    assert "not a directory" in capsys.readouterr().out
    assert list(elsewhere.iterdir()) == [] and not any("auth login" in c for c in rig.calls())


def test_status_that_cannot_run_is_unknown_and_fails_the_verb(rig, capsys):
    """V1 codex MED 6: a missing sudo rule is not "not signed in", and unqualified status exits nonzero."""
    rig.broken = True
    assert hands_login.cmd_hands_status(rig.entity, None) == 1
    out = capsys.readouterr().out
    assert "hands claude: unknown" in out and "hands codex: unknown" in out
    assert "a password is required" in out and "not signed in" not in out


def test_a_session_open_during_status_is_left_alone_after_a_status_timeout(rig, monkeypatch):
    """The sweep after a status that ran out takes the hands lock first; while a session holds it, nothing is
    stopped and the row says so."""
    monkeypatch.setattr(hands_login, "STEP_TIMEOUT", 1.0)
    rig.knobs["SHIM_LOGOUT_HANG"] = "1"
    monkeypatch.setitem(hands_login._VENDOR["claude"], "status", ("claude", "auth", "logout"))
    fd = hands_live.take_exclusive(rig.entity)
    try:
        st = hands_login.run_status(rig.h, "claude", entity_dir=rig.entity)
    finally:
        os.close(fd)
    assert st.state == "unknown" and "a session holds the hands lock" in st.error
    assert not any(c.startswith("SWEEP") for c in rig.calls())
    for pid in rig.pids.read_text().split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


# --- refusal, parsing, the verb ------------------------------------------------------------------------------

def test_no_hands_account_is_one_refusal_for_both_vendors(tmp_path, capsys):
    msgs = []
    for v in ("claude", "codex"):
        assert hands_login.cmd_hands_login(tmp_path, v) == 1
        msgs.append(capsys.readouterr().out)
    assert msgs[0] == msgs[1] == f"hands login: {hands_login.NO_HANDS}\n"
    assert hands_login.cmd_hands_status(tmp_path, None) == 1
    assert hands_login.NO_HANDS in capsys.readouterr().out
    assert not (tmp_path / ".levain").exists()


def test_hands_for_is_untouched_and_vendor_blind():
    import inspect
    src = inspect.getsource(confinement.hands_for)
    assert "claude" not in src and "codex" not in src and "vendor" not in src


@pytest.mark.parametrize("vendor,rc,text,want", [
    ("claude", 0, '{"loggedIn": true, "email": "a@b.c", "orgName": "Org"}', ("in", "a@b.c / Org")),
    ("claude", 1, '{"loggedIn": false}', ("out", "")),
    ("claude", 1, "Not logged in", ("out", "")),
    ("claude", 0, "Logged in as a@b.c", ("in", "Logged in as a@b.c")),
    ("claude", 0, '{"email": "a@b.c"}', ("unknown", "")),             # no loggedIn: not a yes
    ("claude", 1, '{"loggedIn": true}', ("unknown", "")),            # a yes with a failed exit
    ("claude", 126, "sudo: unable to execute /usr/bin/env: Permission denied", ("unknown", "")),
    ("codex", 0, "Logged in using ChatGPT\n", ("in", "Logged in using ChatGPT")),
    ("codex", 1, "Not logged in", ("out", "")),
    ("codex", 1, "", ("unknown", "")),
    ("codex", 0, "", ("unknown", "")),
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
