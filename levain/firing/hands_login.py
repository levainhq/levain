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

Anything that runs as the hands account can use this login, and a logout is local to this machine; the card
says so (:data:`LOGIN_NOTICE`).
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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
               "login": ("claude", "auth", "login"), "status": ("claude", "auth", "status")},
    "codex": {"env": "CODEX_HOME", "dir": ".codex", "cred": "auth.json",
              "login": ("codex", "login", "--device-auth"), "status": ("codex", "login", "status")},
}

#: The line Codex's hands ``config.toml`` must carry; the same pair ``slot/render.py codex_config`` pins.
CODEX_STORE_KEY = "cli_auth_credentials_store"
CODEX_STORE_LINE = f'{CODEX_STORE_KEY} = "file"\n'

LOGIN_NOTICE = (
    "Anything that runs as the hands account can use this login. A logout here is local to this machine; "
    "to cut the login off at the provider, end the session on the provider's own account page."
)
NO_HANDS = "this entity has no hands account; run `sudo levain setup-isolation` first"

LOGIN_TIMEOUT = 900.0     # a person opens a URL and pastes a code
STEP_TIMEOUT = 60.0


class HandsLoginError(RuntimeError):
    """A hands login step refused or failed."""


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
    logged_in: bool
    account: str          # one line, "" when none could be read
    raw: str
    credential: str | None = None     # "<owner> <mode>" of the credential file as hands sees it


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


_URL = re.compile(r"https://[^\s\"'<>]+")
_CODE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b")


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


def parse_status(vendor: str, returncode: int, text: str) -> tuple[bool, str]:
    """(logged in, one account line) from ``claude auth status`` (JSON, or text) or ``codex login status``."""
    _check_vendor(vendor)
    body = text.strip()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        on = data.get("loggedIn")
        if on is None:
            on = returncode == 0
        bits = [str(data[k]) for k in ("email", "orgName", "subscriptionType") if data.get(k)]
        return bool(on) and returncode == 0, " / ".join(bits)
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    off = re.search(r"not\s+logged\s+in|logged\s+out", body, re.I) is not None
    return returncode == 0 and not off and bool(first), ("" if off else first)


# --- runners ---------------------------------------------------------------------------------------------------

def _run(argv: list[str], *, stdin: bytes | None = None, timeout: float = STEP_TIMEOUT) -> tuple[int, str]:
    r = hands_live.run_bounded(argv, env=child_env(), timeout=timeout, stdin=stdin)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _as_hands_argv(hands: HandsIdentity, vendor: str, *argv: str) -> list[str]:
    return [*_launch_prefix(hands, vendor), *argv]


def ensure_config_home(hands: HandsIdentity, vendor: str) -> None:
    """The vendor's config home exists, owned by the hands user (made BY it), and Codex's ``config.toml`` pins the
    file store. An existing ``config.toml`` keeps its content: the pin is added in front when absent, and a
    different value for the same key is refused rather than overwritten."""
    d = config_home(hands, vendor)
    rc, out = _run(_as_hands_argv(hands, vendor, "/bin/mkdir", "-p", "-m", "700", d))
    if rc != 0:
        raise HandsLoginError(f"could not make {d} as {hands.user}: {out.strip()[:200]}")
    if vendor != "codex":
        return
    cfg = codex_config_path(hands)
    rc, _ = _run(_as_hands_argv(hands, vendor, "/bin/test", "-e", cfg))
    existing = ""
    if rc == 0:
        rc, existing = _run(_as_hands_argv(hands, vendor, "/bin/cat", cfg))
        if rc != 0:
            raise HandsLoginError(f"could not read {cfg} as {hands.user}")
    keys = [ln for ln in existing.splitlines() if re.match(rf"\s*{CODEX_STORE_KEY}\s*=", ln)]
    if keys:
        if any(ln.split("=", 1)[1].strip() != '"file"' for ln in keys):
            raise HandsLoginError(f'{cfg} sets {CODEX_STORE_KEY} to something other than "file"; '
                                  "change it by hand, a login would not be kept in a file")
        return
    rc, out = _run(_as_hands_argv(hands, vendor, "/usr/bin/tee", cfg), stdin=(CODEX_STORE_LINE + existing).encode())
    if rc != 0:
        raise HandsLoginError(f"could not write {cfg} as {hands.user}: {out.strip()[:200]}")


def credential_state(hands: HandsIdentity, vendor: str) -> str | None:
    """``"<owner> <mode>"`` of the credential file, read AS the hands user, or None when it is absent."""
    stat = ["/usr/bin/stat", "-f", "%Su %Lp"] if platform.system() == "Darwin" else ["/usr/bin/stat", "-c", "%U %a"]
    rc, out = _run(_as_hands_argv(hands, vendor, *stat, credential_path(hands, vendor)))
    return out.strip() if rc == 0 and out.strip() else None


def run_login(hands: HandsIdentity, vendor: str, *, emit: Callable[[str], None] = print,
              timeout: float = LOGIN_TIMEOUT) -> LoginResult:
    """Run the vendor's login as the hands user. The caller's stdin is inherited (Claude's paste-code prompt reads
    the code there); every output line goes to ``emit`` as it comes, and the URLs and device codes in it come back
    in the result."""
    _check_vendor(vendor)
    ensure_config_home(hands, vendor)
    proc = subprocess.Popen(login_argv(hands, vendor), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd="/", env=child_env())
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            lines.append(line)
            emit(line)
        rc = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    text = "\n".join(lines)
    urls, codes = surface(text)
    return LoginResult(vendor, rc, urls, codes, text)


def run_status(hands: HandsIdentity, vendor: str) -> Status:
    _check_vendor(vendor)
    rc, out = _run(status_argv(hands, vendor))
    on, account = parse_status(vendor, rc, out)
    return Status(vendor, on, account, out.strip(), credential_state(hands, vendor))


# --- CLI -------------------------------------------------------------------------------------------------------

def load_hands_identity(entity_dir: Path | str, cfg: ConfinementConfig | None = None) -> HandsIdentity:
    """The hands account, or a refusal that reads the same for every vendor."""
    c = cfg if cfg is not None else load_confinement_config(Path(entity_dir).expanduser())
    try:
        hands = hands_for(c, "headless")
    except ConfinementError as exc:
        raise HandsLoginError(str(exc)) from None
    if hands is None:
        raise HandsLoginError(NO_HANDS)
    return hands


def _line(st: Status) -> str:
    shown = st.account or ("signed in" if st.logged_in else "not signed in")
    cred = f" (credential file: {st.credential})" if st.credential else ""
    return f"hands {st.vendor}: {shown}{cred}" if st.logged_in else f"hands {st.vendor}: not signed in{cred}"


def cmd_hands_login(entity_dir: Path | str, vendor: str) -> int:
    try:
        hands = load_hands_identity(entity_dir)
        print(f"Signing the hands account ({hands.user}) in to {vendor}. {LOGIN_NOTICE}")
        res = run_login(hands, vendor)
        if res.returncode != 0:
            print(f"hands login {vendor}: the login exited {res.returncode}")
            return 1
        st = run_status(hands, vendor)
    except (HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands login: {exc}")
        return 1
    print(_line(st))
    if st.credential and st.credential.split()[-1] != "600":
        print(f"warning: the credential file's mode is {st.credential.split()[-1]}, not 600")
    return 0 if st.logged_in else 1


def cmd_hands_status(entity_dir: Path | str, vendor: str | None) -> int:
    try:
        hands = load_hands_identity(entity_dir)
        rows = [run_status(hands, v) for v in ((vendor,) if vendor else VENDORS)]
    except (HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands status: {exc}")
        return 1
    for st in rows:
        print(_line(st))
    print(LOGIN_NOTICE)
    return 0 if all(r.logged_in for r in rows) or vendor is None else 1
