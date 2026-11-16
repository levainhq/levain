"""levain.desk.night_ssh: the night seat's laptop-side ssh config for the TAOSERV night key (ND-8 step 5, levain#47).

The LAPTOP half of ``docs/night_key_runbook.md`` (the server half is :mod:`levain.desk.night_key`). It renders the one
``Host`` block of the runbook into ``<home>/.levain/night/ssh_config``, the file ``effects._night_argv`` hands ssh with
``-F`` (so no ``~/.ssh/config`` setting, an ``IdentityAgent`` included, applies to the night key's ssh). It writes ONLY
that file, under an injectable ``home``; it never generates or reads a key and never runs ssh. The key
(``taoserv_night``) and ``known_hosts`` are placed by a person.
"""
from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from pathlib import Path

from levain.desk.effects import NIGHT_ALIAS

__all__ = ["config_path", "render_config", "write_config"]

#: a plain DNS name or IPv4 address: labels of letters, digits, inner hyphens, dots between; no space, newline, slash,
#: leading hyphen or non-ASCII byte, so the value cannot add a config line or an ssh option
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(_LABEL + r"(?:\." + _LABEL + r")*", re.ASCII)
_HOSTNAME_MAX = 253
_PLACEHOLDER = "REPLACE-WITH-TAOSERV-ADDRESS"          # the runbook's marker: never a real address


#: characters a home path may not hold: ``%`` (an ssh token), ``$`` (``${VAR}`` expansion), ``"`` and ``\`` (they would
#: end or escape the quoted value); control characters are refused by category below
_HOME_FORBIDDEN = frozenset('%$"\\')


def _checked_home(home: Path | str) -> Path:
    """``home`` as an absolute path whose text can sit inside one double-quoted ssh-config value unchanged; else
    ValueError."""
    raw = os.fspath(home) if isinstance(home, (str, os.PathLike)) else None
    if type(raw) is not str or not os.path.isabs(raw):
        raise ValueError("home must be an absolute path")
    if any(c in _HOME_FORBIDDEN or unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in raw):
        raise ValueError("home holds a character an ssh config value cannot carry")
    return Path(raw)


def _night_dir(home: Path | str) -> Path:
    return _checked_home(home) / ".levain" / "night"


def config_path(home: Path | str) -> Path:
    return _night_dir(home) / "ssh_config"


def render_config(hostname: str, home: Path | str) -> str:
    """The runbook's ``taoserv-night`` block with absolute, quoted key and known-hosts paths under ``home``, and the
    compiled-in global known-hosts files turned off (only the pinned file can vouch for the server)."""
    if (type(hostname) is not str or len(hostname) > _HOSTNAME_MAX or hostname.upper() == _PLACEHOLDER
            or _HOSTNAME.fullmatch(hostname) is None):
        raise ValueError("hostname must be a plain DNS name or IPv4 address")
    nd = _night_dir(home)
    return (f"Host {NIGHT_ALIAS['taoserv']}\n"
            f"    HostName {hostname}\n"
            "    User nightkey\n"
            f'    IdentityFile "{nd}/taoserv_night"\n'
            "    IdentitiesOnly yes\n"
            "    IdentityAgent none\n"
            f'    UserKnownHostsFile "{nd}/known_hosts"\n'
            "    GlobalKnownHostsFile none\n"
            "    StrictHostKeyChecking yes\n"
            "    RequestTTY no\n"
            "    ForwardAgent no\n")


def write_config(hostname: str, home: Path | str) -> Path:
    """Write the config (mode 0600, its directory 0700) through a unique temp file in the same directory and replace;
    return its path. A symlink at ``.levain``, ``.levain/night`` or the target is refused, not followed; a failed write
    removes only its own temp file."""
    text = render_config(hostname, home)
    target = config_path(home)
    levain_dir, nd = target.parent.parent, target.parent
    for d in (levain_dir, nd):
        if d.is_symlink():
            raise ValueError("refusing to write through a symlink")
        d.mkdir(parents=True, exist_ok=True)
        if d.is_symlink():
            raise ValueError("refusing to write through a symlink")
    os.chmod(nd, 0o700)
    if target.is_symlink():
        raise ValueError("refusing to write through a symlink")
    fd, tmp_name = tempfile.mkstemp(prefix=".ssh_config.", suffix=".tmp", dir=nd)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
