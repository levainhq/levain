"""`levain hands login|status` (levain 15 V1): the hands account's own vendor login. The vendor CLIs are shell shims a
test puts first on PATH; the sudo prefix is the only thing replaced (as in tests/test_ws_git.py), so the hands-side
steps run as the current user. No real login, no user created, no keychain, no live app. Cross-user ownership
(a file owned by the hands uid) is the macOS VM / CI run.

Shim knobs (environment the fake prefix passes): SHIM_HANG (the login leaves a child holding the output open),
SHIM_KEYSTORE (the login is kept in that file, not the credential file: a non-file store), SHIM_LOGOUT_FAIL /
SHIM_LOGOUT_NOOP / SHIM_LOGOUT_HANG / SHIM_LEAVE (logout branches), SHIM_STATUS_SAYS / SHIM_STATUS_RC (the status
prints that line on stderr and exits that code, default 1: a failed status). The sweep is a recorder that also kills the
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
_REAL_VENDOR_MISSING = hands_login.vendor_missing
_REAL_KEYCHAIN_NEEDED = hands_login.keychain_needed
_REAL_SECURITY = hands_login.SECURITY

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
    if [ -n "$SHIM_STATUS_SAYS" ]; then echo "$SHIM_STATUS_SAYS" >&2; exit "${SHIM_STATUS_RC:-1}"; fi
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
    if [ -n "$SHIM_KEYSTORE" ]; then echo k > "$SHIM_KEYSTORE"; else echo '{"tokens":{}}' > "$CODEX_HOME/auth.json"; fi
    exit 0;;
  "logout ")
    if [ -n "$SHIM_LOGOUT_FAIL" ]; then echo "logout failed" >&2; exit 1; fi
    [ -n "$SHIM_LEAVE" ] || rm -f "$CODEX_HOME/auth.json"
    if [ -n "$SHIM_KEYSTORE" ]; then rm -f "$SHIM_KEYSTORE"; fi
    exit 0;;
  "login status")
    if [ -n "$SHIM_STATUS_SAYS" ]; then echo "$SHIM_STATUS_SAYS" >&2; exit "${SHIM_STATUS_RC:-1}"; fi
    if [ -f "$CODEX_HOME/auth.json" ] || { [ -n "$SHIM_KEYSTORE" ] && [ -f "$SHIM_KEYSTORE" ]; }; then
      echo "Logged in using ChatGPT"; exit 0
    fi
    echo "Not logged in" >&2; exit 1;;
esac
exit 2
"""

#: levain#139: the stand-in for /usr/bin/security (the rig points ``hands_login.SECURITY`` at it, so no test ever runs
#: the real one or touches a real keychain). The "keychain" is files under $HOME (the hands home): the default
#: keychain's path in .fake-security-default, the search list in .fake-security-list, and the keychain file itself
#: (create-keychain writes a marker, never the password). The real CLI's forms are the ``-d user`` ones (measured 11-18:
#: ``/usr/bin/security default-keychain -d user`` exits 0 and prints the default). Each call logs HOME, LC_ALL and its
#: arguments. Knobs: SEC_READ_FAIL (every default read fails with that text), SEC_NONE_TEXT (the wording of "no
#: default"), SEC_SET_NOOP (setting the default is accepted but changes nothing), SEC_SET_FAIL (setting the default
#: fails), SEC_LIST_FAIL (setting the search list fails), SEC_CREATE_FAIL (create-keychain fails, echoing its
#: arguments, the password included), SEC_CREATE_HANG (create-keychain hangs, its sleeper's pid in SHIM_PIDS).
SECURITY_SHIM = """#!/bin/sh
echo "$HOME|$LC_ALL|$*" >> "$SHIM_SECLOG"
D="$HOME/.fake-security-default"
L="$HOME/.fake-security-list"
case "$1" in
  default-keychain)
    if [ "$4" = "-s" ]; then
      if [ -n "$SEC_SET_FAIL" ]; then echo "security: SecKeychainSetDomainDefault: $SEC_SET_FAIL" >&2; exit 1; fi
      [ -n "$SEC_SET_NOOP" ] || [ ! -d "$HOME/Library/Preferences" ] || printf '%s' "$5" > "$D"; exit 0   # macOS: rc 0 and no write without ~/Library/Preferences (levain#139, l139_prefsdir_probe*.txt)
    fi
    if [ -n "$SEC_READ_FAIL" ]; then echo "security: SecKeychainCopyDomainDefault user: $SEC_READ_FAIL" >&2; exit 1; fi
    if [ -f "$D" ]; then printf '    "%s"\\n' "$(cat "$D")"; exit 0; fi
    echo "security: ${SEC_NONE_PREFIX:-SecKeychainCopyDomainDefault user:} ${SEC_NONE_TEXT:-A default keychain could not be found.}" >&2; exit 1;;
  list-keychains)
    if [ "$4" = "-s" ]; then
      if [ -n "$SEC_LIST_FAIL" ]; then echo "security: SecKeychainSetDomainSearchList: $SEC_LIST_FAIL" >&2; exit 1; fi
      [ -d "$HOME/Library/Preferences" ] && printf '%s\\n' "$5" > "$L"; exit 0
    fi
    if [ -f "$L" ]; then while read -r k; do printf '    "%s"\\n' "$k"; done < "$L"; fi
    exit 0;;
  create-keychain)
    if [ -n "$SEC_CREATE_HANG" ]; then sleep 8 & echo $! > "$SHIM_PIDS"; wait; fi
    if [ -n "$SEC_CREATE_FAIL" ]; then echo "security: SecKeychainCreate $*: $SEC_CREATE_FAIL" >&2; exit 1; fi
    if [ -e "$4" ]; then echo "security: SecKeychainCreate $4: A keychain with the same name already exists." >&2; exit 48; fi
    umask 077; echo "fake keychain" > "$4"; exit 0;;
esac
exit 2
"""

#: The prefix when the operator's sudo rule for the hands user is missing: every run as hands fails like sudo -n.
BROKEN_PREFIX = ["/bin/sh", "-c", 'echo "sudo: a password is required" >&2; exit 1', "sudo"]


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for name, body in (("claude", CLAUDE_SHIM), ("codex", CODEX_SHIM), ("security", SECURITY_SHIM)):
            p = self.bindir / name
            p.write_text(body)
            p.chmod(0o755)
        self.seclog = tmp_path / "security.log"
        # levain#139: never the real /usr/bin/security; the keychain step runs on every host so the rig is the same
        monkeypatch.setattr(hands_login, "SECURITY", str(self.bindir / "security"))
        monkeypatch.setattr(hands_login, "keychain_needed", lambda vendor: vendor == "claude")
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
        monkeypatch.setattr(hands_login, "vendor_missing", lambda vendor: None)       # a test host has no vendor on HANDS_PATH

    def prefix(self, hands, vendor):
        if self.broken:
            return list(BROKEN_PREFIX)
        env = hands_login.vendor_env(hands, vendor)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["SHIM_LOG"] = str(self.log)
        env["SHIM_PIDS"] = str(self.pids)
        env["SHIM_SECLOG"] = str(self.seclog)
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
    assert set(assigned) == {"HOME", "PATH", "LANG", "TERM", "USER", "LOGNAME", "ENABLE_CLAUDEAI_MCP_SERVERS", var}
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


def test_a_vendor_that_is_not_on_the_hands_path_is_refused_before_anything_runs(rig, monkeypatch, tmp_path, capsys):
    """levain#139 (leg 6): ``claude`` only in the operator's ~/.local/bin ended in a bare ``env: claude: No such file or
    directory`` (exit 127) after the locks and the keychain: now a refusal naming the PATH, before any call."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setattr(hands_login, "vendor_missing", _REAL_VENDOR_MISSING)
    monkeypatch.setattr(confinement, "HANDS_PATH", str(empty))
    with pytest.raises(hands_login.HandsRefused, match=r"claude is not on the hands account's PATH .*the login was not started"):
        _login(rig, "claude")
    assert rig.calls() == [] and not (rig.hands_home / "Library").exists()
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    assert "the login was not started" in capsys.readouterr().out


def test_a_vendor_on_the_hands_path_is_not_refused(rig, monkeypatch):
    monkeypatch.setattr(hands_login, "vendor_missing", _REAL_VENDOR_MISSING)
    monkeypatch.setattr(confinement, "HANDS_PATH", f"{rig.bindir}:/usr/bin:/bin")    # the rig's bindir holds the claude shim
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in


def test_the_hands_keychain_step_makes_library_preferences_without_which_security_persists_nothing(rig):
    """levain#139 (leg 6): on a bare hands home ``default-keychain -s`` and ``list-keychains -s`` answered rc 0 and wrote
    nothing; the rig's fake ``security`` now does the same, so this fails the product's own after-set check without the dir."""
    prefs = rig.hands_home / "Library" / "Preferences"
    assert not prefs.exists()
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert prefs.is_dir() and _mode(prefs) == 0o700
    assert (rig.hands_home / ".fake-security-default").read_text() == str(_kc(rig))


def test_a_default_of_ours_with_library_preferences_since_removed_is_healed_not_refused_for_ever(rig):
    """L3 r2 (complement LOW, deepseek MED): the directory is made on every path that can reach a set, not only when no
    default exists: with ours the default, the directory gone and the search list lost, the set is a silent no-op
    without it and every later login would refuse the same way."""
    import shutil
    _login(rig, "claude")
    shutil.rmtree(rig.hands_home / "Library" / "Preferences")
    (rig.hands_home / ".fake-security-list").unlink()                 # the search list drifted: a set is owed
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert (rig.hands_home / "Library" / "Preferences").is_dir()
    assert str(_kc(rig)) in (rig.hands_home / ".fake-security-list").read_text()


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
    deadline = time.monotonic() + 3.0          # the killed child is reaped by init, not by this process (CI py3.13 read it as a zombie)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
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
    assert "codex" not in hands_login.read_outcomes(rig.entity)       # L3 r2: a pre-flight failure records nothing


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


def test_status_runs_nothing_while_a_session_holds_the_hands_lock(rig, capsys, monkeypatch):
    """L3 r2 of V1/V2 (codex MED, item 4 residual): an unlocked status that ran out while a session held the lock
    was killed and its descendant left with no sweep allowed. The status now takes the lock for its whole read;
    while it is held nothing runs, the state is unknown and the verb exits nonzero."""
    monkeypatch.setattr(hands_login, "STEP_TIMEOUT", 1.0)
    rig.knobs["SHIM_LOGOUT_HANG"] = "1"
    monkeypatch.setitem(hands_login._VENDOR["claude"], "status", ("claude", "auth", "logout"))
    fd = hands_live.take_exclusive(rig.entity)
    try:
        st = hands_login.run_status(rig.h, "claude", entity_dir=rig.entity)
        assert hands_login.cmd_hands_status(rig.entity, None) == 1
    finally:
        os.close(fd)
    try:
        assert st.state == "unknown" and "a session of this entity is open" in st.error
        assert rig.calls() == [] and not rig.pids.exists()                 # nothing ran as hands
        out = capsys.readouterr().out
        assert "hands claude: unknown" in out and "hands codex: unknown" in out
    finally:
        for pid in (rig.pids.read_text().split() if rig.pids.exists() else []):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_status_that_runs_out_is_swept_under_the_lock_it_took(rig, monkeypatch):
    """A guard for the same fix (this half held before it too): with no session, the status holds the lock it took,
    so the sweep after it ran out runs under it and the descendant is gone."""
    monkeypatch.setattr(hands_login, "STEP_TIMEOUT", 1.0)
    rig.knobs["SHIM_LOGOUT_HANG"] = "1"
    monkeypatch.setitem(hands_login._VENDOR["claude"], "status", ("claude", "auth", "logout"))
    st = hands_login.run_status(rig.h, "claude", entity_dir=rig.entity)
    assert st.state == "unknown" and "did not finish" in st.error
    assert [c for c in rig.calls() if c.startswith("SWEEP")] == ["SWEEP levain-hands %d lock-held=True" % os.getuid()]
    pid = int(rig.pids.read_text().split()[0])
    deadline = time.monotonic() + 3.0          # the killed child is reaped by init, not by this process
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_read_only_credential_read_that_runs_out_sweeps_nothing(rig, monkeypatch):
    """L3 r1 of the bundle4 merge (complement MED): the doctor row reads the credential file with ``sweep=False``. A read
    that runs out then stops only its own killed child and never the hands account's other processes; the same read
    with the default does sweep (the explicit verbs keep that)."""
    swept = []
    monkeypatch.setattr(hands_login, "_sweep_after", lambda hands, entity_dir, held: swept.append(held))

    def hang(argv, **kw):
        raise TimeoutError("stat ran out")
    monkeypatch.setattr(hands_live, "run_bounded", hang)
    with pytest.raises(hands_login.HandsLoginError, match="read-only check"):
        hands_login.credential_state(rig.h, "claude", entity_dir=rig.entity, sweep=False)
    assert swept == []
    with pytest.raises(hands_login.HandsLoginError):
        hands_login.credential_state(rig.h, "claude", entity_dir=rig.entity)
    assert swept == [False]


def test_a_config_refusal_does_not_overwrite_the_last_outcome(rig, capsys):
    """L3 r2 of V1/V2 (complement + codex MED, item 7 residual): a pre-flight refusal in the config home (here a
    keyring pin) changed nothing about the login, so it records nothing."""
    assert hands_login.cmd_hands_login(rig.entity, "codex") == 0
    capsys.readouterr()
    cfg = rig.hands_home / ".codex" / "config.toml"
    cfg.write_text('cli_auth_credentials_store = "keyring"\n')
    assert hands_login.cmd_hands_login(rig.entity, "codex") == 1
    out = capsys.readouterr().out
    assert "other than" in out and "the login was not started" in out
    assert hands_login.read_outcomes(rig.entity)["codex"]["outcome"] == "logged-in"
    assert sum(c == "login --device-auth" for c in rig.calls()) == 1      # only the first login launched


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
    ("claude", 1, "Not logged in. Run claude auth login to authenticate.", ("out", "")),
    ("claude", 0, "Logged in as a@b.c", ("in", "Logged in as a@b.c")),
    # L3 r2 (codex HIGH): only the vendor's complete signed-out answer, with its exit code, is OUT
    ("claude", 1, "could not determine whether logged out: keychain locked", ("unknown", "")),
    ("claude", 1, "Error: not logged in to the keychain", ("unknown", "")),
    ("claude", 1, "Not logged in", ("unknown", "")),                 # not claude's line
    ("claude", 0, '{"loggedIn": false}', ("unknown", "")),           # signed out, but not with its exit code
    ("claude", 2, '{"loggedIn": false}', ("unknown", "")),
    ("claude", 0, "Not logged in. Run claude auth login to authenticate.", ("unknown", "")),
    ("claude", 0, "Session ok; previously logged out", ("unknown", "")),   # a mention is never an IN either
    ("codex", 1, "Error checking login status: could not determine whether logged out: keychain locked",
     ("unknown", "")),
    ("codex", 1, "Error checking login status: Not logged in to keyring", ("unknown", "")),
    ("codex", 2, "Not logged in", ("unknown", "")),
    ("codex", 0, "Not logged in", ("unknown", "")),
    ("codex", 1, '{"loggedIn": false}', ("unknown", "")),            # codex does not answer in JSON
    ("codex", 1, "WARNING: a deprecated key in config.toml\nNot logged in\n", ("out", "")),
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


# --- levain#139: the hands account's own sealed keychain (the `security` program is the rig's shim) -------------

def _sec_lines(r: Rig) -> list[list[str]]:
    return [ln.split("|", 2) for ln in r.seclog.read_text().splitlines()] if r.seclog.exists() else []


def _sec_calls(r: Rig) -> list[str]:
    return [parts[2] for parts in _sec_lines(r)]


def _kc(r: Rig) -> Path:
    return r.hands_home / "Library" / "Keychains" / hands_login.HANDS_KEYCHAIN


def test_keychain_needed_is_claude_on_macos_only(monkeypatch):
    monkeypatch.setattr(hands_login, "keychain_needed", _REAL_KEYCHAIN_NEEDED)
    for system, vendor, want in (("Darwin", "claude", True), ("Darwin", "codex", False),
                                 ("Linux", "claude", False), ("Linux", "codex", False)):
        monkeypatch.setattr(hands_login.platform, "system", lambda s=system: s)
        assert hands_login.keychain_needed(vendor) is want


#: Every ``security`` verb the product may run; anything else (unlock-keychain, lock-keychain, find-generic-password,
#: delete-keychain, set-keychain-settings ...) failing this set is a change of the custody design, not a refactor.
_ALLOWED_VERBS = {"default-keychain", "list-keychains", "create-keychain"}


def test_the_rig_never_runs_the_real_program_and_the_product_never_unlocks(rig):
    assert hands_login.SECURITY == str(rig.bindir / "security")
    assert _REAL_SECURITY == "/usr/bin/security"
    # behaviour, not source text: every path that runs `security` (make, re-run, repair, refusals) uses only the
    # read/set/create verbs, and every call is LC_ALL=C with HOME = the hands home
    _login(rig, "claude")
    _login(rig, "claude")
    (rig.hands_home / ".fake-security-list").unlink()
    _login(rig, "claude")
    _kc(rig).unlink()
    _login(rig, "claude")
    rig.knobs["SEC_SET_FAIL"] = "x"
    (rig.hands_home / ".fake-security-default").unlink()
    with pytest.raises(hands_login.HandsRefused):
        _login(rig, "claude")
    lines = _sec_lines(rig)
    assert len(lines) > 10
    assert {parts[2].split(" ")[0] for parts in lines} <= _ALLOWED_VERBS
    assert {(parts[0], parts[1]) for parts in lines} == {(str(rig.hands_home), "C")}


def test_claude_login_first_makes_the_hands_keychain_then_the_login_lands_in_the_file(rig):
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in
    kc = _kc(rig)
    calls = _sec_calls(rig)
    assert calls == ["default-keychain -d user", calls[1], f"default-keychain -d user -s {kc}", "list-keychains -d user",
                     f"list-keychains -d user -s {kc}", "list-keychains -d user", "default-keychain -d user"]
    create = calls[1].split(" ")
    assert create[0] == "create-keychain" and create[1] == "-p" and len(create[2]) >= 40 and create[3] == str(kc)
    # the keychain is the hands account's, inside its home, in a 700 directory; the password is kept nowhere
    assert kc.is_file() and _mode(kc.parent) == 0o700 and _mode(rig.hands_home / "Library") == 0o700
    pw = create[2]
    for p in rig.hands_home.rglob("*"):
        if p.is_file():
            assert pw not in p.read_text(errors="replace"), p
    for p in rig.entity.rglob("*"):
        if p.is_file():
            assert pw not in p.read_text(errors="replace"), p
    assert not any(pw in s for s in said)
    # every call ran with HOME = the hands home, never the operator's
    homes = {parts[0] for parts in _sec_lines(rig)}
    assert homes == {str(rig.hands_home)}
    # the note says it was made, and the login itself is the 0600 file as before
    assert any(hands_login.KEYCHAIN_MADE_NOTE.format(path=kc) in s for s in said)
    assert (rig.hands_home / ".claude" / ".credentials.json").is_file()
    # the note comes before the vendor's own output
    joined = "".join(said)
    assert joined.index("Made the hands account's own keychain") < joined.index("Open https://claude.ai")


def test_a_second_login_finds_the_keychain_and_makes_nothing(rig):
    _login(rig, "claude")
    rig.seclog.unlink()
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in
    assert _sec_calls(rig) == ["default-keychain -d user", "list-keychains -d user"]
    assert not any("Made the hands account" in s or "again" in s for s in said)


def test_codex_login_makes_no_keychain(rig):
    res, st = _login(rig, "codex")
    assert res.returncode == 0 and st.logged_in
    assert _sec_calls(rig) == [] and not (rig.hands_home / "Library").exists()


def test_a_foreign_default_keychain_is_refused_and_never_replaced(rig):
    other = rig.hands_home / "Library" / "Keychains" / "login.keychain-db"
    other.parent.mkdir(parents=True)
    other.write_text("someone's keychain")
    (rig.hands_home / ".fake-security-default").write_text(str(other))
    with pytest.raises(hands_login.HandsRefused, match="not adopted or replaced.*the login was not started"):
        _login(rig, "claude")
    assert _sec_calls(rig) == ["default-keychain -d user"]
    assert (rig.hands_home / ".fake-security-default").read_text() == str(other)
    assert rig.calls() == []                               # the vendor login never started


def test_an_unreadable_keychain_state_is_refused_never_taken_for_absent(rig):
    rig.knobs["SEC_READ_FAIL"] = "The user name or passphrase you entered is not correct."
    with pytest.raises(hands_login.HandsRefused, match="could not read .*default keychain"):
        _login(rig, "claude")
    assert [c.split(" ")[0] for c in _sec_calls(rig)] == ["default-keychain"]
    assert not _kc(rig).exists() and rig.calls() == []


def test_a_failed_create_is_refused_and_the_login_not_started(rig):
    rig.knobs["SEC_CREATE_FAIL"] = "disk full"
    with pytest.raises(hands_login.HandsRefused, match="could not make .*disk full"):
        _login(rig, "claude")
    assert rig.calls() == [] and not (rig.hands_home / ".fake-security-default").exists()


def test_a_default_that_does_not_stick_is_refused(rig):
    rig.knobs["SEC_SET_NOOP"] = "1"
    with pytest.raises(hands_login.HandsRefused, match="is none after it was set"):
        _login(rig, "claude")
    assert rig.calls() == []


def test_a_keychain_file_left_by_an_earlier_run_is_reused_not_recreated(rig):
    kc = _kc(rig)
    kc.parent.mkdir(parents=True)
    kc.write_text("earlier")
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert "create-keychain" not in " ".join(_sec_calls(rig))
    assert kc.read_text() == "earlier"


def test_a_keychain_path_that_is_not_a_file_is_refused(rig):
    kc = _kc(rig)
    kc.mkdir(parents=True)
    with pytest.raises(hands_login.HandsRefused, match="not a keychain file"):
        _login(rig, "claude")
    assert rig.calls() == []


def test_the_cli_login_reports_a_keychain_refusal_without_recording_a_failure(rig, capsys):
    other = rig.hands_home.parent / "elsewhere-login.keychain-db"
    other.write_text("someone's keychain")
    (rig.hands_home / ".fake-security-default").write_text(str(other))
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "not adopted or replaced" in out and "the login was not started" in out
    assert hands_login.read_outcomes(rig.entity).get("claude") is None


# --- levain#139 review fixes: the "no default" probe, idempotence, the password in errors, the home guard ---------

def test_a_printed_default_that_does_not_exist_counts_as_none(rig):
    # a fresh account may print a login.keychain-db that was never made: that is no default, not a foreign one
    ghost = rig.hands_home / "Library" / "Keychains" / "login.keychain-db"
    (rig.hands_home / ".fake-security-default").write_text(str(ghost))
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in and not ghost.exists()
    assert (rig.hands_home / ".fake-security-default").read_text() == str(_kc(rig))
    assert "create-keychain" in " ".join(_sec_calls(rig))
    assert any("Made the hands account's own keychain" in s for s in said)


@pytest.mark.parametrize("text", ["A default keychain could not be found.", "Unknown error: (-25307)"])
def test_the_no_default_answer_is_matched_on_its_structure(rig, text):
    rig.knobs["SEC_NONE_TEXT"] = text
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in and _kc(rig).is_file()


@pytest.mark.parametrize("line", [
    # the line measured on a fresh macOS 15.7.7 hands account (levain#139, l139_defkc.txt), verbatim
    "security: SecKeychainCopyDomainDefault user: A default keychain could not be found.",
    "security: SecKeychainCopyDomainDefault user: Unknown error: (-25307)",
    "security: SecKeychainCopyDefault: A default keychain could not be found.",       # the form the code first assumed
])
def test_the_no_default_line_matches_the_measured_shape_and_its_older_form(line):
    assert hands_login._NO_DEFAULT.fullmatch(line)


@pytest.mark.parametrize("line", [
    "security: SecKeychainCopyDomainDefault bogus: A default keychain could not be found.",
    "security: SecKeychainCopyDomainDefault user : A default keychain could not be found.",
    "security: SecKeychainSetDomainDefault user: A default keychain could not be found.",
    # a domain this module never queries (-d user): another domain's "no default" is not an absent user default
    "security: SecKeychainCopyDomainDefault system: A default keychain could not be found.",
    "security: SecKeychainCopyDomainDefault common: A default keychain could not be found.",
    "security: SecKeychainCopyDomainDefault dynamic: A default keychain could not be found.",
    # a domain word belongs to the Domain call only
    "security: SecKeychainCopyDefault user: A default keychain could not be found.",
])
def test_only_the_queried_domain_on_the_domain_call_is_no_default(line):
    assert not hands_login._NO_DEFAULT.fullmatch(line)


@pytest.mark.parametrize("prefix", ["SecKeychainCopyDefault:", "SecKeychainCopyDomainDefault user:"])
def test_both_printed_forms_of_no_default_make_the_keychain_through_the_rig(rig, prefix):
    rig.knobs["SEC_NONE_PREFIX"] = prefix
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in and _kc(rig).is_file()


def test_a_system_domain_no_default_line_is_an_error_not_an_absent_default(rig):
    rig.knobs["SEC_NONE_PREFIX"] = "SecKeychainCopyDomainDefault system:"
    with pytest.raises(hands_login.HandsRefused, match="could not read .*default keychain"):
        _login(rig, "claude")
    assert not _kc(rig).exists() and rig.calls() == []


@pytest.mark.parametrize("text", ["The specified item could not be found in the keychain.",
                                  "A default keychain could not be found. (and more)"])
def test_another_could_not_be_found_is_not_taken_for_no_default(rig, text):
    rig.knobs["SEC_NONE_TEXT"] = text
    with pytest.raises(hands_login.HandsRefused, match="could not read .*default keychain"):
        _login(rig, "claude")
    assert not _kc(rig).exists() and rig.calls() == []


def test_our_default_with_its_file_gone_is_made_again_not_taken_as_present(rig):
    _login(rig, "claude")
    _kc(rig).unlink()                                     # deleted by hand, or a restore without it
    rig.seclog.unlink()
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in and _kc(rig).is_file()
    assert "create-keychain" in " ".join(_sec_calls(rig))
    assert any("Made the hands account's own keychain" in s for s in said)


def test_a_failed_search_list_refuses_then_the_next_login_finishes_it(rig):
    rig.knobs["SEC_LIST_FAIL"] = "write failed"
    with pytest.raises(hands_login.HandsRefused, match="list-keychains.*write failed.*the next login sets the search "
                                                       "list again.*the login was not started"):
        _login(rig, "claude")
    assert rig.calls() == [] and not (rig.hands_home / ".fake-security-list").exists()
    assert (rig.hands_home / ".fake-security-default").read_text() == str(_kc(rig))      # the half-made state
    del rig.knobs["SEC_LIST_FAIL"]
    rig.seclog.unlink()
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in
    assert _sec_calls(rig) == ["default-keychain -d user", "list-keychains -d user", f"list-keychains -d user -s {_kc(rig)}",
                               "list-keychains -d user", "default-keychain -d user"]
    assert (rig.hands_home / ".fake-security-list").read_text().strip() == str(_kc(rig))
    assert any(hands_login.KEYCHAIN_REPAIRED_NOTE.format(path=_kc(rig)) in s for s in said)


def test_a_search_list_without_ours_is_set_again(rig):
    _login(rig, "claude")
    (rig.hands_home / ".fake-security-list").write_text("/elsewhere/other.keychain-db\n")
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert (rig.hands_home / ".fake-security-list").read_text().strip() == str(_kc(rig))


def test_a_search_list_with_a_foreign_keychain_beside_ours_is_reset_to_ours_alone(rig):
    # l139 round 3: macOS adds a newly made keychain to the list, so ours can be listed beside a foreign one
    _login(rig, "claude")
    (rig.hands_home / ".fake-security-list").write_text(f'"/elsewhere/other.keychain-db"\n"{_kc(rig)}"\n')
    rig.seclog.unlink()
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert f"list-keychains -d user -s {_kc(rig)}" in _sec_calls(rig)
    assert (rig.hands_home / ".fake-security-list").read_text().strip().strip('"') == str(_kc(rig))


def test_a_search_list_that_is_not_ours_alone_after_it_was_set_is_refused(rig, monkeypatch):
    real = hands_login._search_list
    seen = []

    def stuck(hands, vendor, entity_dir):
        seen.append(1)
        return real(hands, vendor, entity_dir) + (["/elsewhere/other.keychain-db"] if len(seen) > 1 else [])
    monkeypatch.setattr(hands_login, "_search_list", stuck)
    with pytest.raises(hands_login.HandsRefused, match="search list is .*other.keychain-db.* after it was set"):
        _login(rig, "claude")


def test_a_failed_default_set_refuses_then_the_next_login_finishes_it(rig):
    rig.knobs["SEC_SET_FAIL"] = "denied"
    with pytest.raises(hands_login.HandsRefused, match="default-keychain.*denied.*is kept and the next login sets it"):
        _login(rig, "claude")
    assert _kc(rig).is_file() and not (rig.hands_home / ".fake-security-default").exists() and rig.calls() == []
    del rig.knobs["SEC_SET_FAIL"]
    rig.seclog.unlink()
    said: list[str] = []
    res, st = _login(rig, "claude", emit=said.append)
    assert res.returncode == 0 and st.logged_in
    assert "create-keychain" not in " ".join(_sec_calls(rig))
    assert any("again" in s for s in said) and not any("Made the hands" in s for s in said)


def test_a_failed_search_list_read_refuses(rig, monkeypatch):
    real = hands_login._security

    def broken(hands, vendor, *args, **kw):
        if args == ("list-keychains", "-d", "user"):
            return hands_login.subprocess.CompletedProcess(args, 1, b"", b"security: no list\n")
        return real(hands, vendor, *args, **kw)
    monkeypatch.setattr(hands_login, "_security", broken)
    with pytest.raises(hands_login.HandsRefused, match="could not read .*search list.*no list"):
        _login(rig, "claude")
    assert rig.calls() == []


def test_our_keychain_spelled_another_way_is_still_ours(rig):
    # a trailing slash on the home, and `security` printing the path with a redundant component, read as ours
    _login(rig, "claude")
    rig.h = HandsIdentity(rig.h.user, rig.h.uid, str(rig.hands_home) + "/", rig.h.workspace)
    (rig.hands_home / ".fake-security-default").write_text(f"{rig.hands_home}/Library/./Keychains//{hands_login.HANDS_KEYCHAIN}")
    rig.seclog.unlink()
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and st.logged_in
    assert _sec_calls(rig) == ["default-keychain -d user", "list-keychains -d user"]
    assert hands_login.keychain_path(rig.h) == str(_kc(rig))


def test_our_keychain_through_a_symlinked_home_is_still_ours(rig, tmp_path):
    link = tmp_path / "hands-link"
    link.symlink_to(rig.hands_home)
    h = HandsIdentity(rig.h.user, rig.h.uid, str(link), rig.h.workspace)
    assert hands_login._is_ours(os.path.realpath(_kc(rig)), h) and hands_login._is_ours(f"{link}/Library/Keychains/"
                                                                          f"{hands_login.HANDS_KEYCHAIN}", h)
    assert not hands_login._is_ours(f"{link}/Library/Keychains/login.keychain-db", h)


@pytest.mark.parametrize("home", ["relative/home", "/", ""])
def test_a_hands_home_that_is_not_absolute_is_refused(rig, home):
    rig.h = HandsIdentity(rig.h.user, rig.h.uid, home, rig.h.workspace)
    with pytest.raises(HandsLoginError, match="not an absolute directory"):
        hands_login.ensure_hands_keychain(rig.h, "claude", entity_dir=rig.entity)
    assert _sec_calls(rig) == []


def test_the_operators_home_as_the_hands_home_is_refused(rig):
    rig.h = HandsIdentity(rig.h.user, rig.h.uid, str(rig.operator_home), rig.h.workspace)
    with pytest.raises(HandsLoginError, match="operator's own home"):
        hands_login.ensure_hands_keychain(rig.h, "claude", entity_dir=rig.entity)
    assert _sec_calls(rig) == [] and not (rig.operator_home / "Library").exists()


def test_a_create_that_times_out_never_shows_the_password(rig, monkeypatch, capsys):
    monkeypatch.setattr(hands_login.secrets, "token_urlsafe", lambda n: "PW-SENTINEL-not-for-any-message")
    monkeypatch.setattr(hands_login, "STEP_TIMEOUT", 1.0)
    rig.knobs["SEC_CREATE_HANG"] = "1"
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "security create-keychain did not finish (timed out after 1 seconds)" in out
    assert "PW-SENTINEL" not in out and "Command '" not in out and "the login was not started" in out
    assert rig.calls() == ["SWEEP levain-hands %d lock-held=True" % os.getuid()]
    # the function-level error carries no argv either
    with pytest.raises(hands_login.HandsRefused) as ei:
        _login(rig, "claude")
    assert "PW-SENTINEL" not in str(ei.value) and "timed out after" in str(ei.value)


def test_a_failed_create_that_echoes_its_arguments_never_shows_the_password(rig, monkeypatch, capsys):
    monkeypatch.setattr(hands_login.secrets, "token_urlsafe", lambda n: "PW-SENTINEL-not-for-any-message")
    rig.knobs["SEC_CREATE_FAIL"] = "disk full"
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "could not make" in out and "disk full" in out and "<password>" in out and "PW-SENTINEL" not in out


def test_an_existing_library_is_tightened_to_700(rig):
    lib = rig.hands_home / "Library"
    lib.mkdir(mode=0o755)
    lib.chmod(0o755)
    res, st = _login(rig, "claude")
    assert res.returncode == 0 and _mode(lib) == 0o700 and _kc(rig).is_file()


def test_a_symlinked_keychains_directory_is_refused(rig, tmp_path):
    (rig.hands_home / "Library").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (rig.hands_home / "Library" / "Keychains").symlink_to(elsewhere)
    with pytest.raises(hands_login.HandsRefused, match="not a directory"):
        _login(rig, "claude")
    assert list(elsewhere.iterdir()) == [] and rig.calls() == []
