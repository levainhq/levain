"""levain.firing.hands_login — the hands account's OWN vendor login (Claude Code, Codex), run as that account.

THE RULE (Phill 11-03): the hands account signs in itself. Nothing here reads, copies or points at the
operator's ``~/.claude`` / ``~/.codex`` / ``auth.json`` / keychain: vendor refresh tokens rotate on use, so
two holders of one login break each other, and a copied login is a second holder. Claude and Codex go through
the SAME path; which account exists is decided where it is for everything else, :func:`~levain.firing.confinement.hands_for`
(mode based, no vendor branch).

What runs, always as the hands user through :func:`~levain.firing.confinement.hands_prefix` (so its fixed
``env -i`` environment and PATH from ``_hands_env``), with the config home pinned inside the hands home:

- Claude: ``claude auth login`` (paste-code), ``CLAUDE_CONFIG_DIR=<hands home>/.claude``. With no keychain the
  vendor stores ``.credentials.json`` in that directory (its documented fallback, not a setting we can pin).
- Codex: ``codex login --device-auth``, ``CODEX_HOME=<hands home>/.codex``, whose ``config.toml`` pins
  ``cli_auth_credentials_store = "file"`` (the pin :func:`levain.slot.render.codex_config` writes for a slot).

Then ``claude auth status`` / ``codex login status`` as hands, reduced to one account line.

Login and logout each hold the entity's hands lock for the whole transaction, so neither runs beside a session
or beside the other; ``hands status`` takes it for its read too (L3 r2), so a status that runs out is swept. A
state that could not be read is its own result (``unknown``), never "signed out": a status that cannot be
determined refuses a logout and fails ``hands status`` (L3 r1 of V1/V2, codex HIGH 1).
Every run as the hands account is bounded; one that runs out (or is interrupted) is killed and then the hands
account's processes are swept, because what sudo started runs as another uid and outlives a killed sudo.

Anything that runs as the hands account can use this login, and a logout is local to this machine; the card
says so (:data:`LOGIN_NOTICE`).
"""
from __future__ import annotations

import codecs
import fcntl
import json
import os
import platform
import queue
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from levain import hands_live
from levain.firing.confinement import (
    HANDS_PYTHON, HANDS_ZSH, ConfinementConfig, ConfinementError, HandsIdentity, _hands_env, hands_for, hands_prefix,
    load_confinement_config,
)
from levain.launch import child_env

VENDORS = ("claude", "codex")

#: Per vendor: the config-home variable, its directory under the hands home, the credential file the vendor
#: writes there, the login argv and the status argv (the program is found on the hands PATH).
_VENDOR = {
    "claude": {"env": "CLAUDE_CONFIG_DIR", "dir": ".claude", "cred": ".credentials.json",
               "login": ("claude", "auth", "login"), "status": ("claude", "auth", "status"),
               "logout": ("claude", "auth", "logout"), "page": "https://claude.ai/settings (the account's active sessions)"},
    "codex": {"env": "CODEX_HOME", "dir": ".codex", "cred": "auth.json",
              "login": ("codex", "login", "--device-auth"), "status": ("codex", "login", "status"),
              "logout": ("codex", "logout"), "page": "https://chatgpt.com/ (Settings, Security, the signed-in devices)"},
}

#: The line Codex's hands ``config.toml`` must carry; the same pair ``slot/render.py codex_config`` pins.
CODEX_STORE_KEY = "cli_auth_credentials_store"
CODEX_STORE_LINE = f'{CODEX_STORE_KEY} = "file"\n'

LOGIN_NOTICE = (
    "Anything that runs as the hands account can use this login. A logout here is local to this machine; "
    "to cut the login off at the provider, end the session on the provider's own account page."
)
LOGOUT_NOTICE = (
    "This logout is LOCAL ONLY: it ended the login on this machine, and neither vendor is known to revoke the "
    "token at the provider. Revoke it there: {page}. Anything that ran as the hands account could have used the login."
)
NO_HANDS = "this entity has no hands account; run `sudo levain setup-isolation` first"
#: The stated 1.0 edge (MCP_HANDS_RESEARCH_7 section 3): on Linux the hands uid's egress is loopback-only and no
#: hands CONNECT proxy ships, so a vendor login cannot reach the provider. Status and logout make no call that
#: needs the network and still run.
LINUX_LOGIN_EDGE = ("Linux: the hands account has no egress path for a vendor login yet (a hands proxy is not "
                    "shipped); login on macOS or use the operator account")

LOGIN_TIMEOUT = 900.0     # a person opens a URL and pastes a code
STEP_TIMEOUT = 60.0

#: A status: signed in, signed out, or not determinable.
IN, OUT, UNKNOWN = "in", "out", "unknown"


class HandsLoginError(RuntimeError):
    """A hands login step refused or failed."""


class HandsRefused(HandsLoginError):
    """Refused before anything was changed: the last recorded outcome stands."""


@dataclass
class LoginResult:
    vendor: str
    returncode: int
    urls: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    output: str = ""


@dataclass(frozen=True)
class Status:
    vendor: str
    state: str            # IN, OUT or UNKNOWN
    account: str          # one line, "" when none could be read
    raw: str
    credential: str | None = None     # "<owner> <mode>" of the credential file as hands sees it
    error: str = ""                   # why the state is UNKNOWN

    @property
    def logged_in(self) -> bool:
        return self.state == IN


# --- pure builders ---------------------------------------------------------------------------------------------

def config_home(hands: HandsIdentity, vendor: str) -> str:
    return f"{hands.home}/{_VENDOR[vendor]['dir']}"


def vendor_env(hands: HandsIdentity, vendor: str) -> dict[str, str]:
    """The hands environment for ``vendor`` (``_hands_env``: fixed PATH) plus its config home pinned inside the
    hands home. Nothing else is added and nothing is read from this process."""
    spec = _VENDOR[vendor]
    return {**_hands_env(hands), spec["env"]: config_home(hands, vendor)}


def _launch_prefix(hands: HandsIdentity, vendor: str) -> list[str]:
    """:func:`hands_prefix` with the vendor's config home added as one more ``env -i`` assignment (they sit right
    after the ``-i``); a Linux start program, if any, still follows them."""
    base = hands_prefix(hands)
    i = base.index("-i") + 1
    return [*base[:i], f"{_VENDOR[vendor]['env']}={config_home(hands, vendor)}", *base[i:]]


def login_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    _check_vendor(vendor)
    return [*_launch_prefix(hands, vendor), *_VENDOR[vendor]["login"]]


def status_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    _check_vendor(vendor)
    return [*_launch_prefix(hands, vendor), *_VENDOR[vendor]["status"]]


def credential_path(hands: HandsIdentity, vendor: str) -> str:
    return f"{config_home(hands, vendor)}/{_VENDOR[vendor]['cred']}"


def codex_config_path(hands: HandsIdentity) -> str:
    return f"{config_home(hands, 'codex')}/config.toml"


def _check_vendor(vendor: str) -> None:
    if vendor not in _VENDOR:
        raise HandsLoginError(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")


def login_refusal() -> str | None:
    """Why a vendor login cannot run on this host, or None."""
    return LINUX_LOGIN_EDGE if platform.system() == "Linux" else None


_URL = re.compile(r"https://[^\s\"'<>]+")
_CODE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b")
_OFF = re.compile(r"not\s+logged\s+in|logged\s+out", re.I)

#: Per vendor: the vendor's complete signed-out line and the exit code it comes with. Read from the shipped programs
#: on 11-03 (L3 r2 of V1/V2, codex HIGH): claude 2.1.317 ``auth status`` exits 1 when signed out, printing
#: ``{"loggedIn": false}`` (and, with ``--text``, this line); codex ``login status`` printed exactly this line on
#: stderr and exited 1 against an empty file-store ``CODEX_HOME``. Its errors read ``Error checking login status: ...``.
_SIGNED_OUT = {"claude": ("Not logged in. Run claude auth login to authenticate.", 1),
               "codex": ("Not logged in", 1)}
#: The vendors whose status answers in JSON (``loggedIn``).
_STRUCTURED = frozenset({"claude"})


def surface(text: str) -> tuple[list[str], list[str]]:
    """The sign-in URLs and device codes in a login's output, in order, without duplicates."""
    urls: list[str] = []
    codes: list[str] = []
    for u in _URL.findall(text):
        u = u.rstrip(".,)")
        if u not in urls:
            urls.append(u)
    for c in _CODE.findall(text):
        if c not in codes:
            codes.append(c)
    return urls, codes


def parse_status(vendor: str, returncode: int, text: str) -> tuple[str, str]:
    """(state, one account line) from ``claude auth status`` (JSON, or text) or ``codex login status``.

    OUT only on the vendor's complete signed-out answer with the exit code it uses for it (:data:`_SIGNED_OUT`):
    ``"loggedIn": false`` from a vendor that answers in JSON, or the signed-out line as a whole line. IN only on
    exit 0 with a positive answer. Anything else is UNKNOWN: a nonzero exit with other prose, a JSON reply without
    ``loggedIn``, no output, and any reply that merely mentions being logged in or out (an error such as "could not
    determine whether logged out: keychain locked" says nothing about the login)."""
    _check_vendor(vendor)
    body = text.strip()
    off_line, off_rc = _SIGNED_OUT[vendor]
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if vendor not in _STRUCTURED:
            return UNKNOWN, ""
        on = data.get("loggedIn")
        if on is False and returncode == off_rc:
            return OUT, ""
        if on is True and returncode == 0:
            return IN, " / ".join(str(data[k]) for k in ("email", "orgName", "subscriptionType") if data.get(k))
        return UNKNOWN, ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if off_line in lines:
        return (OUT, "") if returncode == off_rc else (UNKNOWN, "")
    if _OFF.search(body):
        return UNKNOWN, ""
    return (IN, lines[0]) if returncode == 0 and lines else (UNKNOWN, "")


# --- runners ---------------------------------------------------------------------------------------------------

def _text(r: "subprocess.CompletedProcess[bytes]") -> str:
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def _brief(text: str) -> str:
    return " ".join(text.split())[:300]


@contextmanager
def hands_lock(entity_dir: Path | str, what: str) -> Iterator[None]:
    """The entity's hands lock, exclusive, for the whole block, or :class:`HandsRefused` at once while a session
    (or another exclusive holder: a login, a logout, ws-git) has it."""
    (Path(entity_dir) / ".levain").mkdir(exist_ok=True)
    fd = hands_live.take_exclusive(entity_dir)
    if fd is None:
        raise HandsRefused(f"a session of this entity is open; {what} waits until it ends")
    try:
        yield
    finally:
        os.close(fd)


def _sweep_after(hands: HandsIdentity, entity_dir: Path | str, held: bool) -> str | None:
    """Stop what the hands account has left running after a run that did not end on its own. Only under the hands
    lock: the caller's (``held``), or one taken here; while a session holds it, its processes are its own and are
    left alone. None when the sweep found nothing left, else why not. Never raises an ``Exception``."""
    try:
        if held:
            return hands_live.sweep_hands_user(hands.user, uid=hands.uid)
        fd = hands_live.take_exclusive(entity_dir)
        if fd is None:
            return "a session holds the hands lock, so what the hands account left running was not stopped"
        try:
            return hands_live.sweep_hands_user(hands.user, uid=hands.uid)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 - reported, never raised over the failure that led here
        return f"the stop of the hands account's processes failed ({exc})"


def run_as_hands_bounded(hands: HandsIdentity, argv: list[str], *, entity_dir: Path | str, held: bool, what: str,
                         timeout: float | None = None, stdin: bytes | None = None,
                         sweep: bool = True) -> "subprocess.CompletedProcess[bytes]":
    """One run as the hands user, bounded by ``timeout`` (default :data:`STEP_TIMEOUT`). On a timeout, an error or an
    interrupt the child is killed (:func:`hands_live.run_bounded`) and then the hands account's processes are swept
    (:func:`_sweep_after`), since the program sudo started runs as the hands uid and can outlive the killed sudo. An
    ``Exception`` comes back as :class:`HandsLoginError` naming ``what`` and the sweep's result; an interrupt passes
    through after the sweep. ``sweep=False`` is for a read-only caller (the doctor row): the killed child is all that
    is stopped, the hands account's other processes are never touched, and the error says so (L3 r1, levain 23)."""
    t = STEP_TIMEOUT if timeout is None else timeout
    try:
        return hands_live.run_bounded(argv, env=child_env(), timeout=t, stdin=stdin)
    except BaseException as exc:
        left = _sweep_after(hands, entity_dir, held) if sweep else "the hands account's other processes were not touched (a read-only check)"
        if not isinstance(exc, Exception):
            raise
        tail = left or "the hands account's processes were stopped"
        raise HandsLoginError(f"{what} did not finish ({exc}); {tail}") from None


def _as_hands_argv(hands: HandsIdentity, vendor: str, *argv: str) -> list[str]:
    return [*_launch_prefix(hands, vendor), *argv]


def _lstat(hands: HandsIdentity, vendor: str, path: str, *, entity_dir: Path | str,
           held: bool, sweep: bool = True) -> tuple[str, int, str, str] | None:
    """``(type, uid, owner, mode)`` of ``path`` read AS the hands user, not following a symlink; None only when stat
    itself says the path does not exist. Anything else that fails (the sudo rule, the launcher, the program)
    raises :class:`HandsLoginError`: an absent file is never inferred from a failed read."""
    fmt = ("-f", "%HT|%u|%Su|%Lp") if platform.system() == "Darwin" else ("-c", "%F|%u|%U|%a")
    r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/usr/bin/env", "LC_ALL=C", "/usr/bin/stat", *fmt, path),
                             entity_dir=entity_dir, held=held, what=f"stat {path}", sweep=sweep)
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode == 0:
        parts = out.split("|")
        if len(parts) == 4 and parts[1].isdigit():
            return parts[0].lower(), int(parts[1]), parts[2], parts[3]
        raise HandsLoginError(f"could not read {path} as {hands.user}: stat printed {out[:200]!r}")
    if r.returncode == 1 and "No such file or directory" in err and path in err:
        return None
    raise HandsLoginError(f"could not read {path} as {hands.user}: {_brief(err + out) or f'exit {r.returncode}'}")


def _ensure_dir(hands: HandsIdentity, vendor: str, d: str, entity_dir: Path | str) -> None:
    st = _lstat(hands, vendor, d, entity_dir=entity_dir, held=True)
    if st is None:
        r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/bin/mkdir", "-m", "700", d),
                                 entity_dir=entity_dir, held=True, what=f"mkdir {d}")
        if r.returncode != 0:
            raise HandsLoginError(f"could not make {d} as {hands.user}: {_brief(_text(r))}")
        st = _lstat(hands, vendor, d, entity_dir=entity_dir, held=True)
        if st is None:
            raise HandsLoginError(f"{d} is not there after it was made")
    kind, uid, owner, mode = st
    if kind != "directory":
        raise HandsLoginError(f"{d} is a {kind}, not a directory; nothing was written (remove it by hand)")
    if uid != hands.uid:
        raise HandsLoginError(f"{d} belongs to {owner}, not {hands.user}; nothing was written")
    if mode != "700":
        r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/bin/chmod", "700", d),
                                 entity_dir=entity_dir, held=True, what=f"chmod {d}")
        if r.returncode != 0:
            raise HandsLoginError(f"could not set {d} to mode 700 as {hands.user}: {_brief(_text(r))}")


# --- the config-home file helper (hands-MCP M2 L3 r1) ----------------------------------------------------------
#
# Every read and replacement of a file in a vendor config home goes through ONE fixed program run as the hands user,
# so it is made with that user's rights and follows no link it did not expect. The directory must be a real directory
# (not a symlink) owned by the hands uid, and the program ENTERS it and checks that the directory it is in is the one
# it judged; everything after is relative to it. Operations (exit 0 done, 2 the directory or file is absent, 3 refused
# with the reason on stderr, anything else a failure):
#   read    <name>              the regular file opened O_NOFOLLOW|O_NONBLOCK, "uid mode\n" then its bytes (at most
#                               :data:`_CONF_READ_LIMIT`); a symlink, directory, FIFO or device is refused, never read
#   stage   <name> <mode>       stdin into a NEW file of an unpredictable name beside it (O_CREAT|O_EXCL|O_NOFOLLOW,
#                               mode 600, then ``mode``), printing that name; leftovers of an earlier stage of the same
#                               name are removed first (a link is removed, never followed)
#   commit  <name> <staged>     the staged file renamed over ``name`` (a link at ``name`` is replaced; a directory there
#                               is refused)
#   drop    <name> <staged>     the staged file removed
# Two programs with one contract, because no one interpreter is on every host: zsh ships with every macOS (its
# zsh/system module opens with O_NOFOLLOW, which no stock utility can), and on Linux every hands process already
# starts through python3 (:data:`HANDS_PYTHON`), while a Linux runner need not have zsh. The tests run both.
_CONF_READ_LIMIT = 1024 * 1024
_CONF_ZSH = r"""emulate -R zsh
zmodload zsh/system zsh/stat 2>/dev/null || { print -ru2 -- 'zsh modules are unavailable'; exit 70 }
refuse() { print -rnu2 -- "$1"; exit 3 }
op=$1 d=$2 uid=$3 n=$4 x=$5
[[ $d == /* ]] || refuse "not an absolute path: $d"
[[ -n $n && $n != */* && $n != . && $n != .. ]] || refuse "not a file name: $n"
[[ -L $d ]] && refuse "$d is a symlink, not a directory; change it by hand"
[[ -e $d ]] || exit 2
zstat -L -H st -- $d 2>/dev/null || refuse "could not read $d"
(( (st[mode] & 8#170000) == 8#040000 )) || refuse "$d is not a directory; change it by hand"
[[ $st[uid] == $uid ]] || refuse "$d belongs to another account; change it by hand"
id="$st[device]:$st[inode]"
builtin cd -q -- $d 2>/dev/null || refuse "could not read $d: permission denied"
zstat -L -H st -- . 2>/dev/null && [[ "$st[device]:$st[inode]" == $id ]] || refuse "$d changed while it was entered"
staged() { [[ $x == .$n.levain-* && $x != */* ]] || refuse "not a staged name: $x" }
case $op in
  read)
    if ! sysopen -r -o nofollow,nonblock -u 5 -- ./$n 2>/dev/null; then
      [[ -e ./$n || -L ./$n ]] || exit 2
      [[ -L ./$n ]] && refuse "$d/$n is a symlink, not a file; change it by hand"
      refuse "could not read $d/$n: permission denied"
    fi
    zstat -f 5 -H st 2>/dev/null || refuse "could not read $d/$n"
    (( (st[mode] & 8#170000) == 8#100000 )) || refuse "$d/$n is not a file; change it by hand"
    (( st[size] <= LIM )) || refuse "$d/$n is larger than LIM bytes; change it by hand"
    print -rn -- "$st[uid] $(( [##8] st[mode] & 8#7777 ))"$'\n'
    /bin/cat <&5
    ;;
  stage)
    [[ $x == [0-7][0-7][0-7] ]] || refuse "not a mode: $x"
    for o in ./.$n.levain-*(N); do /bin/rm -f -- $o 2>/dev/null; done
    t=.$n.levain-$$-$RANDOM$RANDOM$RANDOM
    sysopen -w -o create,excl,nofollow -m 600 -u 6 -- ./$t 2>/dev/null || refuse "could not create a new file in $d"
    if ! /bin/cat >&6; then exec 6>&-; /bin/rm -f -- ./$t; refuse "writing in $d failed"; fi
    exec 6>&-
    /bin/chmod $x ./$t || { /bin/rm -f -- ./$t; refuse "could not set the mode of a new file in $d" }
    print -rn -- $t
    ;;
  commit)
    staged
    [[ -f ./$x && ! -L ./$x ]] || refuse "the staged $d/$x is gone or not a file"
    [[ -d ./$n ]] && refuse "$d/$n is a directory; change it by hand"
    /bin/mv -f -- ./$x ./$n || refuse "could not replace $d/$n"
    ;;
  drop) staged; /bin/rm -f -- ./$x ;;
  *) refuse "unknown operation $op" ;;
esac
""".replace("LIM", str(_CONF_READ_LIMIT))
_CONF_PY = r"""import os, secrets, stat, sys
def refuse(m):
    sys.stderr.write(m); sys.exit(3)
op, d, uid, n, x = (sys.argv[1:] + [""] * 5)[:5]
if not d.startswith("/"): refuse("not an absolute path: " + d)
if not n or "/" in n or n in (".", ".."): refuse("not a file name: " + n)
if os.path.islink(d): refuse(d + " is a symlink, not a directory; change it by hand")
try:
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
except FileNotFoundError:
    sys.exit(2)
except OSError as e:
    refuse(d + " is not a directory; change it by hand" if os.path.lexists(d) and not os.path.isdir(d) else "could not read %s: %s" % (d, e.strerror))
st = os.fstat(dfd)
if not stat.S_ISDIR(st.st_mode): refuse(d + " is not a directory; change it by hand")
if str(st.st_uid) != uid: refuse(d + " belongs to another account; change it by hand")
def staged():
    if not x.startswith("." + n + ".levain-") or "/" in x: refuse("not a staged name: " + x)
p = d + "/" + n
if op == "read":
    try:
        fd = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        sys.exit(2)
    except OSError as e:
        try:
            link = stat.S_ISLNK(os.lstat(n, dir_fd=dfd).st_mode)
        except OSError:
            link = False
        refuse(p + " is a symlink, not a file; change it by hand" if link else "could not read %s: %s" % (p, e.strerror))
    fs = os.fstat(fd)
    if not stat.S_ISREG(fs.st_mode): refuse(p + " is not a file; change it by hand")
    if fs.st_size > LIM: refuse(p + " is larger than LIM bytes; change it by hand")
    out = sys.stdout.buffer
    out.write(("%d %o\n" % (fs.st_uid, stat.S_IMODE(fs.st_mode))).encode())
    while True:
        b = os.read(fd, 65536)
        if not b: break
        out.write(b)
    out.flush()
elif op == "stage":
    if len(x) != 3 or any(c not in "01234567" for c in x): refuse("not a mode: " + x)
    for e in os.listdir(dfd):
        if e.startswith("." + n + ".levain-"):
            try:
                os.unlink(e, dir_fd=dfd)
            except OSError:
                pass
    t = ".%s.levain-%d-%s" % (n, os.getpid(), secrets.token_hex(8))
    try:
        fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    except OSError:
        refuse("could not create a new file in " + d)
    try:
        data = sys.stdin.buffer.read()
        while data:
            data = data[os.write(fd, data):]
        os.fchmod(fd, int(x, 8)); os.fsync(fd); os.close(fd)
    except OSError:
        os.unlink(t, dir_fd=dfd); refuse("writing in %s failed" % d)
    sys.stdout.write(t)
elif op == "commit":
    staged()
    try:
        ok = stat.S_ISREG(os.lstat(x, dir_fd=dfd).st_mode)
    except OSError:
        ok = False
    if not ok: refuse("the staged %s/%s is gone or not a file" % (d, x))
    try:
        if stat.S_ISDIR(os.stat(n, dir_fd=dfd).st_mode): refuse(p + " is a directory; change it by hand")
    except FileNotFoundError:
        pass
    try:
        os.rename(x, n, src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError:
        refuse("could not replace " + p)
elif op == "drop":
    staged()
    try:
        os.unlink(x, dir_fd=dfd)
    except FileNotFoundError:
        pass
else:
    refuse("unknown operation " + op)
""".replace("LIM", str(_CONF_READ_LIMIT))


def _conf_program() -> list[str]:
    """The config-home helper for this host: zsh on macOS, the hands python3 elsewhere (one contract, above)."""
    if platform.system() == "Darwin":
        return [HANDS_ZSH, "-f", "-c", _CONF_ZSH, "zsh"]
    return [HANDS_PYTHON, "-I", "-S", "-c", _CONF_PY]


def _conf_run(hands: HandsIdentity, vendor: str, op: str, d: str, name: str, extra: str = "", *,
              entity_dir: Path | str, stdin: bytes | None = None) -> "subprocess.CompletedProcess[bytes] | None":
    """One helper run as the hands user (under the caller's hands lock); None for exit 2 (absent), the run for exit
    0, :class:`HandsLoginError` with the helper's reason for a refusal or a failure."""
    r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, *_conf_program(), op, d, str(hands.uid), name, extra),
                             entity_dir=entity_dir, held=True, what=f"{op} {d}/{name}", stdin=stdin)
    if r.returncode == 0:
        return r
    if r.returncode == 2 and op == "read":
        return None
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode == 3 and err:
        raise HandsLoginError(err)
    raise HandsLoginError(f"could not {op} {d}/{name} as {hands.user}: {_brief(err) or f'exit {r.returncode}'}")


def conf_read(hands: HandsIdentity, vendor: str, d: str, name: str, *,
              entity_dir: Path | str) -> tuple[int, str, str] | None:
    """``(uid, mode, text)`` of ``d/name`` read as the hands user without following a link, None when the directory
    or the file is absent; anything that is not a plain UTF-8 file in a real hands-owned directory raises."""
    r = _conf_run(hands, vendor, "read", d, name, entity_dir=entity_dir)
    if r is None:
        return None
    head, sep, body = r.stdout.partition(b"\n")
    parts = head.decode("ascii", "replace").split(" ")
    if not sep or len(parts) != 2 or not parts[0].isdigit():
        raise HandsLoginError(f"could not read {d}/{name} as {hands.user}: the helper printed {head[:80]!r}")
    try:
        return int(parts[0]), parts[1], body.decode("utf-8")
    except UnicodeDecodeError:
        raise HandsLoginError(f"{d}/{name} is not UTF-8 text; change it by hand") from None


def conf_stage(hands: HandsIdentity, vendor: str, d: str, name: str, text: str, *, entity_dir: Path | str,
               mode: str = "600") -> str:
    """``text`` written as the hands user into a new file of an unpredictable name in ``d``; returns that name."""
    r = _conf_run(hands, vendor, "stage", d, name, mode, entity_dir=entity_dir, stdin=text.encode("utf-8"))
    assert r is not None
    staged = r.stdout.decode("utf-8", "replace")
    if not staged.startswith(f".{name}.levain-") or "/" in staged:
        raise HandsLoginError(f"could not write {d}/{name} as {hands.user}: the helper printed {staged[:80]!r}")
    return staged


def conf_commit(hands: HandsIdentity, vendor: str, d: str, name: str, staged: str, *, entity_dir: Path | str) -> None:
    _conf_run(hands, vendor, "commit", d, name, staged, entity_dir=entity_dir)


def conf_drop(hands: HandsIdentity, vendor: str, d: str, name: str, staged: str, *, entity_dir: Path | str) -> None:
    """Remove a staged file; never raises (it runs on a failure path, after the failure that is reported)."""
    try:
        _conf_run(hands, vendor, "drop", d, name, staged, entity_dir=entity_dir)
    except HandsLoginError:
        pass


def ensure_config_home(hands: HandsIdentity, vendor: str, *, entity_dir: Path | str) -> None:
    """Under the hands lock (the caller's): the vendor's config home is a real directory owned by the hands user,
    mode 700 (made BY it when absent, its mode repaired, a symlink refused), and Codex's ``config.toml`` pins the
    file store at the top level. An existing ``config.toml`` keeps its content: the pin is added in front when the
    top-level key is absent, a different value for it is refused rather than overwritten, and the new file replaces
    the old one by a rename as hands (never a truncating write)."""
    d = config_home(hands, vendor)
    _ensure_dir(hands, vendor, d, entity_dir)
    if vendor != "codex":
        return
    cfg = codex_config_path(hands)
    # read and replaced through the config-home helper (hands-MCP M2 L3 r1): never a followed link, never a fixed
    # temp name a hands session could have planted as a link to a file of its own
    got = conf_read(hands, vendor, d, "config.toml", entity_dir=entity_dir)
    existing, mode = ("", "600") if got is None else (got[2], got[1][-3:].rjust(3, "0"))
    try:
        doc = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise HandsLoginError(f"{cfg} is not valid TOML ({exc}); change it by hand") from None
    if CODEX_STORE_KEY in doc:
        if doc[CODEX_STORE_KEY] != "file":
            raise HandsLoginError(f'{cfg} sets {CODEX_STORE_KEY} to something other than "file"; '
                                  "change it by hand, a login would not be kept in a file")
        return
    staged = conf_stage(hands, vendor, d, "config.toml", CODEX_STORE_LINE + existing, entity_dir=entity_dir, mode=mode)
    try:
        conf_commit(hands, vendor, d, "config.toml", staged, entity_dir=entity_dir)
    except HandsLoginError:
        conf_drop(hands, vendor, d, "config.toml", staged, entity_dir=entity_dir)
        raise


def credential_state(hands: HandsIdentity, vendor: str, *, entity_dir: Path | str, held: bool = False,
                     sweep: bool = True) -> str | None:
    """``"<owner> <mode>"`` of the credential file, read AS the hands user, or None when stat says it is absent.
    Raises :class:`HandsLoginError` when that cannot be told, or when the path is not a plain file. ``sweep=False``
    (the doctor row) never stops the hands account's processes when the read times out."""
    path = credential_path(hands, vendor)
    st = _lstat(hands, vendor, path, entity_dir=entity_dir, held=held, sweep=sweep)
    if st is None:
        return None
    kind, _, owner, mode = st
    if not kind.startswith("regular"):
        raise HandsLoginError(f"{path} is a {kind}, not a file")
    return f"{owner} {mode}"


def _write_out(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _run_login(hands: HandsIdentity, vendor: str, entity_dir: Path | str, emit: Callable[[str], None],
               timeout: float, argv: list[str] | None = None) -> LoginResult:
    """The vendor login as the hands user, under the caller's hands lock. The caller's stdin is inherited (Claude's
    paste-code prompt reads the code there); output goes to ``emit`` as it arrives, a prompt without a newline
    included. Reading and waiting share one deadline; on it, or on any exception or interrupt, sudo is killed and
    the hands account swept, then a :class:`HandsLoginError` is raised (an interrupt passes through)."""
    proc = subprocess.Popen(login_argv(hands, vendor) if argv is None else argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd="/", env=child_env())
    chunks: "queue.Queue[bytes | None]" = queue.Queue()

    def pump() -> None:
        try:
            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            while True:
                b = os.read(fd, 4096)
                if not b:
                    break
                chunks.put(b)
        except (OSError, ValueError):
            pass
        finally:
            chunks.put(None)

    threading.Thread(target=pump, name="hands-login-output", daemon=True).start()
    deadline = time.monotonic() + timeout
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    said: list[str] = []
    done = False
    note: str | None = None
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                b = chunks.get(timeout=left)
            except queue.Empty:
                break
            if b is None:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                done = True
                break
            s = dec.decode(b)
            if s:
                said.append(s)
                emit(s)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if not done:
            hands_live.kill_quietly(proc)
            note = _sweep_after(hands, entity_dir, True)
        hands_live.reap_quietly(proc, 0.5 if not done else 0.0)
    if not done:
        tail = note or "the hands account's processes were stopped"
        raise HandsLoginError(f"the {vendor} login did not finish within {timeout:g} s; {tail}")
    text = "".join(said) + dec.decode(b"", final=True)
    urls, codes = surface(text)
    return LoginResult(vendor, proc.returncode, urls, codes, text)


def run_status(hands: HandsIdentity, vendor: str, *, entity_dir: Path | str, held: bool = False) -> Status:
    """The vendor's own status and the credential file's owner/mode, both as hands. Never raises for a status
    that cannot be read: that is ``state == UNKNOWN`` with the reason in ``error``.

    Always under the hands lock: the caller's (``held``), or one taken here for the whole read, so a vendor status
    that runs out is swept like every other run as hands. While a session (or a login, a logout, ws-git) holds it,
    nothing runs and the state is UNKNOWN: an unlocked status that ran out could leave a descendant no sweep was
    allowed to stop (L3 r2 of V1/V2, codex MED)."""
    _check_vendor(vendor)
    if not held:
        try:
            with hands_lock(entity_dir, "hands status"):
                return run_status(hands, vendor, entity_dir=entity_dir, held=True)
        except HandsRefused as exc:
            return Status(vendor, UNKNOWN, "", "", None, str(exc))
        except OSError as exc:
            return Status(vendor, UNKNOWN, "", "", None, f"the hands lock could not be taken ({exc})")
    try:
        r = run_as_hands_bounded(hands, status_argv(hands, vendor), entity_dir=entity_dir, held=held,
                                 what=f"{vendor} status")
    except HandsLoginError as exc:
        return Status(vendor, UNKNOWN, "", "", None, str(exc))
    text = _text(r)
    state, account = parse_status(vendor, r.returncode, text)
    error = "" if state != UNKNOWN else f"the status exited {r.returncode}: {_brief(text) or 'no output'}"
    try:
        cred = credential_state(hands, vendor, entity_dir=entity_dir, held=held)
    except HandsLoginError as exc:
        return Status(vendor, UNKNOWN, account, text.strip(), None, str(exc))
    return Status(vendor, state, account, text.strip(), cred, error)


def login(hands: HandsIdentity, vendor: str, entity_dir: Path | str, *, emit: Callable[[str], None] | None = None,
          timeout: float | None = None) -> tuple[LoginResult, Status | None]:
    """The whole login: refused on a host where it cannot reach the provider (:func:`login_refusal`); then, under
    the hands lock, the config home, the vendor login, and (on exit 0) the status. The status is None when the
    login exited nonzero."""
    _check_vendor(vendor)
    refusal = login_refusal()
    if refusal:
        raise HandsRefused(refusal)
    with hands_lock(entity_dir, "a login"):
        try:
            ensure_config_home(hands, vendor, entity_dir=entity_dir)
        except HandsRefused:
            raise
        except HandsLoginError as exc:
            # Before the vendor login launches nothing about the login has changed, so a failure here is a
            # refusal and the last recorded outcome stands (L3 r2 of V1/V2, complement + codex MED).
            raise HandsRefused(f"{exc}; the login was not started") from None
        res = _run_login(hands, vendor, entity_dir, emit or _write_out, LOGIN_TIMEOUT if timeout is None else timeout)
        st = run_status(hands, vendor, entity_dir=entity_dir, held=True) if res.returncode == 0 else None
    return res, st


# --- CLI -------------------------------------------------------------------------------------------------------

def load_hands_identity(entity_dir: Path | str, cfg: ConfinementConfig | None = None) -> HandsIdentity:
    """The hands account, or a refusal that reads the same for every vendor."""
    c = cfg if cfg is not None else load_confinement_config(Path(entity_dir).expanduser())
    try:
        hands = hands_for(c, "headless")
    except ConfinementError as exc:
        raise HandsRefused(str(exc)) from None
    if hands is None:
        raise HandsRefused(NO_HANDS)
    return hands


def _line(st: Status) -> str:
    if st.state == UNKNOWN:
        return f"hands {st.vendor}: unknown ({st.error})"
    cred = f" (credential file: {st.credential})" if st.credential else ""
    if st.state == IN:
        return f"hands {st.vendor}: {st.account or 'signed in'}{cred}"
    return f"hands {st.vendor}: not signed in{cred}"


def cmd_hands_login(entity_dir: Path | str, vendor: str) -> int:
    try:
        hands = load_hands_identity(entity_dir)
        refusal = login_refusal()
        if refusal:
            raise HandsRefused(refusal)
        print(f"Signing the hands account ({hands.user}) in to {vendor}. {LOGIN_NOTICE}")
        res, st = login(hands, vendor, entity_dir)
    except HandsRefused as exc:
        print(f"hands login: {exc}")
        return 1
    except (HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        record_outcome(entity_dir, vendor, "failed")
        print(f"hands login: {exc}")
        return 1
    if st is None:
        record_outcome(entity_dir, vendor, "failed")
        print(f"hands login {vendor}: the login exited {res.returncode}")
        return 1
    record_outcome(entity_dir, vendor, "logged-in" if st.logged_in else "failed")
    print(_line(st))
    if st.logged_in and st.credential is None:
        print(f"warning: no credential file at {credential_path(hands, vendor)}; the vendor kept this login "
              "somewhere else, which `levain hands logout` may not reach")
    if st.credential and st.credential.split()[-1] != "600":
        print(f"warning: the credential file's mode is {st.credential.split()[-1]}, not 600")
    return 0 if st.logged_in else 1


def cmd_hands_status(entity_dir: Path | str, vendor: str | None) -> int:
    try:
        hands = load_hands_identity(entity_dir)
        rows = [run_status(hands, v, entity_dir=entity_dir) for v in ((vendor,) if vendor else VENDORS)]
    except (HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands status: {exc}")
        return 1
    for st in rows:
        print(_line(st))
    print(LOGIN_NOTICE)
    if any(r.state == UNKNOWN for r in rows):
        return 1
    return 0 if vendor is None or rows[0].logged_in else 1


# --- logout (V2) -----------------------------------------------------------------------------------------------

#: The operator-side record of the last login/logout outcome per vendor (a doctor row reads it).
OUTCOME_FILE = "hands-login.json"


def logout_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    _check_vendor(vendor)
    return [*_launch_prefix(hands, vendor), *_VENDOR[vendor]["logout"]]


def remove_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    """Remove exactly the one credential file, as the hands user: the fallback when the vendor's logout leaves it."""
    _check_vendor(vendor)
    return _as_hands_argv(hands, vendor, "/bin/rm", "-f", "--", credential_path(hands, vendor))


class RecordFileRefused(OSError):
    """An operator-side record under ``<entity>/.levain/`` is a symlink or not a regular file: refused, never
    followed. ``.levain`` is writable by the entity's hands, so whatever stands at a record's path may be planted."""


def read_record_json(path: Path) -> object | None:
    """The JSON at ``path``, or None when nothing is there. Opened with ``O_NOFOLLOW`` (and ``O_NONBLOCK``, so a
    planted FIFO cannot hang the read) and checked by ``fstat`` on the open descriptor: a symlink or a non-regular
    file raises :class:`RecordFileRefused`; other faults raise ``OSError`` or ``ValueError``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if os.path.islink(path):
            raise RecordFileRefused(f"{path} is a symlink, not a record; it was not followed") from None
        raise exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecordFileRefused(f"{path} is not a regular file; it was not read")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
    finally:
        os.close(fd)
    return json.loads(b"".join(chunks).decode("utf-8"))


def write_record_json(path: Path, data: object) -> None:
    """Replace ``path`` with ``data`` as JSON: a fresh temp of a random name beside it (``O_CREAT | O_EXCL |
    O_NOFOLLOW``, mode 0600, so nothing planted under any name is opened or truncated), written in full, fsync'd,
    then renamed over ``path`` (a rename replaces a planted symlink itself, never its target). The temp is removed
    on any failure. Raises ``OSError``."""
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        try:
            view = memoryview(json.dumps(data, sort_keys=True).encode("utf-8"))
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _outcome_path(entity_dir: Path | str) -> Path:
    return Path(entity_dir) / ".levain" / OUTCOME_FILE


def read_outcomes(entity_dir: Path | str) -> dict[str, dict[str, str]]:
    try:
        data = read_record_json(_outcome_path(entity_dir))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def record_outcome(entity_dir: Path | str, vendor: str, outcome: str) -> None:
    """Best effort: the record is a convenience for the doctor row, never a gate. The read-modify-write holds a
    sidecar lock, so two verbs for different vendors do not drop each other's record; the file is replaced by a
    rename, never rewritten in place."""
    try:
        path = _outcome_path(entity_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        lk = os.open(path.with_name(path.name + ".lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = read_outcomes(entity_dir)
            data[vendor] = {"outcome": outcome, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            write_record_json(path, data)
        finally:
            os.close(lk)
    except OSError:
        pass


def run_logout(hands: HandsIdentity, vendor: str, entity_dir: Path | str) -> str:
    """End the hands user's login for ``vendor``, all under the hands lock (a live session or a login refuses): read
    the state; stop every process of the hands user; run the vendor's logout as it; remove that one credential file
    as the hands user if it is still there; then ask the vendor again. Returns ``nothing`` (confirmed signed out and
    no file: nothing run), ``logged-out`` or ``file-removed`` only when the vendor then says signed out and the file
    is gone. Raises :class:`HandsRefused` when nothing was changed (the state could not be read, the lock is held,
    processes were left) and :class:`HandsLoginError` when the logout ran but could not be confirmed."""
    _check_vendor(vendor)
    with hands_lock(entity_dir, "logout"):
        st = run_status(hands, vendor, entity_dir=entity_dir, held=True)
        if st.state == UNKNOWN:
            raise HandsRefused(f"cannot tell whether hands {vendor} is signed in, so nothing was changed: {st.error}")
        if st.state == OUT and st.credential is None:
            return "nothing"
        left = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
        if left:
            raise HandsRefused(f"the hands account's processes were not stopped, so nothing was logged out: {left}")
        r = run_as_hands_bounded(hands, logout_argv(hands, vendor), entity_dir=entity_dir, held=True,
                                 what=f"{vendor} logout")
        said = _brief(_text(r)) or f"exit {r.returncode}"
        how = "logged-out"
        if credential_state(hands, vendor, entity_dir=entity_dir, held=True) is not None:
            r2 = run_as_hands_bounded(hands, remove_argv(hands, vendor), entity_dir=entity_dir, held=True,
                                      what=f"rm {credential_path(hands, vendor)}")
            if r2.returncode != 0 or credential_state(hands, vendor, entity_dir=entity_dir, held=True) is not None:
                raise HandsLoginError(f"the credential file {credential_path(hands, vendor)} is still there "
                                      f"({_brief(_text(r2)) or said})")
            how = "file-removed"
        after = run_status(hands, vendor, entity_dir=entity_dir, held=True)
        if after.state != OUT or after.credential is not None:
            why = after.error if after.state == UNKNOWN else f"it says {after.state}{': ' + after.account if after.account else ''}"
            raise HandsLoginError(f"hands {vendor} is not confirmed signed out after the logout ({why}); "
                                  f"the vendor logout said: {said}")
        return how


def cmd_hands_logout(entity_dir: Path | str, vendor: str) -> int:
    try:
        hands = load_hands_identity(entity_dir)
        how = run_logout(hands, vendor, entity_dir)
    except HandsRefused as exc:
        print(f"hands logout: {exc}")
        return 1
    except (HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        record_outcome(entity_dir, vendor, "failed")
        print(f"hands logout: {exc}")
        return 1
    if how == "nothing":
        print(f"hands {vendor}: nothing signed in, nothing to log out")
        return 0
    record_outcome(entity_dir, vendor, how)
    print(f"hands {vendor}: signed out ({how})")
    print(LOGOUT_NOTICE.format(page=_VENDOR[vendor]["page"]))
    return 0
