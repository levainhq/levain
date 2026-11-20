"""Host side of the rig's ``sudo -A``: a SUDO_ASKPASS helper staged in the guest for the install-helper calls.

The product's sudoers rule for the install helper authenticates every call (``Defaults!install-helper authenticate,
timestamp_timeout=0``), which beats the clone image's admin ``NOPASSWD: ALL`` and cannot be answered with ``sudo -n`` or on
a stdin a verb already uses. The rig answers it the way a person would, with the admin's password, through ``sudo -A``. The
password is the profile's own guest login (``HostProfile.ssh_password``, the credential the harness already hands ssh); it
reaches the guest on stdin only, never in an argv, and the helper file is the operator's own, mode 0700.

The sudoers rule itself is the product's design and is not edited, here or in the guest.
"""
from __future__ import annotations

import re
import shlex
from typing import Callable

#: ``(argv, stdin_text, timeout) -> stdout``: the harness's stdin runner to the guest (``runbook.make_stdin_runner``)
StdinRunner = Callable[..., str]

_PATH_RE = re.compile(r"/[A-Za-z0-9_./-]+")


def default_path(profile) -> str:
    """Where the helper lives in the guest: beside the other staged rig tools."""
    return f"{profile.tools_dir}/sudo-askpass.sh"


def _check_path(path: str) -> str:
    if not _PATH_RE.fullmatch(path or "") or ".." in path.split("/"):
        raise ValueError(f"the askpass path must be one absolute path of plain characters (got {path!r})")
    return path


def askpass_text(password: str) -> str:
    """The helper's body: print the password and a newline, whatever characters it holds."""
    return f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n"


def stage_argv(path: str) -> tuple[str, ...]:
    """Make the helper file private before any byte of it is written (umask), then pin the mode. The path is a positional
    argument, never interpolated into the script."""
    return ("/bin/sh", "-c", 'umask 077 && cat >"$1" && chmod 700 "$1"', "sh", _check_path(path))


def stage(stdin_run: StdinRunner, profile, path: str, timeout: float = 60) -> None:
    """Write the helper into the guest as the operator (the account that will run ``sudo -A``)."""
    stdin_run(stage_argv(path), askpass_text(profile.ssh_password), timeout)


def remove(stdin_run: StdinRunner, path: str, timeout: float = 60) -> None:
    stdin_run(("/bin/rm", "-f", _check_path(path)), "", timeout)
