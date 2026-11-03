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
    ConfinementConfig, ConfinementError, HandsIdentity, _hands_env, hands_for, hands_prefix,
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
                         timeout: float | None = None, stdin: bytes | None = None) -> "subprocess.CompletedProcess[bytes]":
    """One run as the hands user, bounded by ``timeout`` (default :data:`STEP_TIMEOUT`). On a timeout, an error or an
    interrupt the child is killed (:func:`hands_live.run_bounded`) and then the hands account's processes are swept
    (:func:`_sweep_after`), since the program sudo started runs as the hands uid and can outlive the killed sudo. An
    ``Exception`` comes back as :class:`HandsLoginError` naming ``what`` and the sweep's result; an interrupt passes
    through after the sweep."""
    t = STEP_TIMEOUT if timeout is None else timeout
    try:
        return hands_live.run_bounded(argv, env=child_env(), timeout=t, stdin=stdin)
    except BaseException as exc:
        left = _sweep_after(hands, entity_dir, held)
        if not isinstance(exc, Exception):
            raise
        tail = left or "the hands account's processes were stopped"
        raise HandsLoginError(f"{what} did not finish ({exc}); {tail}") from None


def _as_hands_argv(hands: HandsIdentity, vendor: str, *argv: str) -> list[str]:
    return [*_launch_prefix(hands, vendor), *argv]


def _lstat(hands: HandsIdentity, vendor: str, path: str, *, entity_dir: Path | str,
           held: bool) -> tuple[str, int, str, str] | None:
    """``(type, uid, owner, mode)`` of ``path`` read AS the hands user, not following a symlink; None only when stat
    itself says the path does not exist. Anything else that fails (the sudo rule, the launcher, the program)
    raises :class:`HandsLoginError`: an absent file is never inferred from a failed read."""
    fmt = ("-f", "%HT|%u|%Su|%Lp") if platform.system() == "Darwin" else ("-c", "%F|%u|%U|%a")
    r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/usr/bin/env", "LC_ALL=C", "/usr/bin/stat", *fmt, path),
                             entity_dir=entity_dir, held=held, what=f"stat {path}")
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
    st = _lstat(hands, vendor, cfg, entity_dir=entity_dir, held=True)
    existing, mode = "", "600"
    if st is not None:
        kind, _, _, mode = st
        if not kind.startswith("regular"):
            raise HandsLoginError(f"{cfg} is a {kind}, not a file; change it by hand")
        r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/bin/cat", cfg),
                                 entity_dir=entity_dir, held=True, what=f"cat {cfg}")
        if r.returncode != 0:
            raise HandsLoginError(f"could not read {cfg} as {hands.user}: {_brief(_text(r))}")
        try:
            existing = r.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise HandsLoginError(f"{cfg} is not UTF-8 text; change it by hand") from None
    try:
        doc = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise HandsLoginError(f"{cfg} is not valid TOML ({exc}); change it by hand") from None
    if CODEX_STORE_KEY in doc:
        if doc[CODEX_STORE_KEY] != "file":
            raise HandsLoginError(f'{cfg} sets {CODEX_STORE_KEY} to something other than "file"; '
                                  "change it by hand, a login would not be kept in a file")
        return
    tmp = f"{cfg}.levain-tmp"
    steps = ((("/usr/bin/tee", tmp), (CODEX_STORE_LINE + existing).encode("utf-8")),
             (("/bin/chmod", mode, tmp), None),
             (("/bin/mv", "-f", tmp, cfg), None))
    for argv, data in steps:
        r = run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, *argv), entity_dir=entity_dir, held=True,
                                 what=f"{argv[0]} {cfg}", stdin=data)
        if r.returncode != 0:
            try:
                run_as_hands_bounded(hands, _as_hands_argv(hands, vendor, "/bin/rm", "-f", "--", tmp),
                                     entity_dir=entity_dir, held=True, what=f"rm {tmp}")
            except HandsLoginError:
                pass
            raise HandsLoginError(f"could not write {cfg} as {hands.user}: {_brief(_text(r))}")


def credential_state(hands: HandsIdentity, vendor: str, *, entity_dir: Path | str, held: bool = False) -> str | None:
    """``"<owner> <mode>"`` of the credential file, read AS the hands user, or None when stat says it is absent.
    Raises :class:`HandsLoginError` when that cannot be told, or when the path is not a plain file."""
    path = credential_path(hands, vendor)
    st = _lstat(hands, vendor, path, entity_dir=entity_dir, held=held)
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
               timeout: float) -> LoginResult:
    """The vendor login as the hands user, under the caller's hands lock. The caller's stdin is inherited (Claude's
    paste-code prompt reads the code there); output goes to ``emit`` as it arrives, a prompt without a newline
    included. Reading and waiting share one deadline; on it, or on any exception or interrupt, sudo is killed and
    the hands account swept, then a :class:`HandsLoginError` is raised (an interrupt passes through)."""
    proc = subprocess.Popen(login_argv(hands, vendor), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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


def _outcome_path(entity_dir: Path | str) -> Path:
    return Path(entity_dir) / ".levain" / OUTCOME_FILE


def read_outcomes(entity_dir: Path | str) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_outcome_path(entity_dir).read_text())
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
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True))
            os.replace(tmp, path)
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
