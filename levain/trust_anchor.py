"""levain.trust_anchor — may this file be trusted to verify a signature?

The one trust-anchor rule (:func:`trust_anchor_problem`) and the verifier's fixed path and environment, for every
levain signature check: the autonomic gate's (``levain.autonomic.confirm``), the authority links' and the route
maps' (``levain.authority_sig``), and the cockpit's. Stdlib only and 3.9-valid, importing no other ``levain``
module: root's own processes run it (a route map's signature is verified in CONTROL and keys), and
``levain.autonomic`` imports it as its one module from outside the package (``tests/test_autonomic_leaf.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DEFAULT_SSH_KEYGEN", "trust_anchor_fault", "trust_anchor_problem"]

# the verifier: an absolute path, never a PATH lookup (a binary planted earlier on PATH could print the
# "Good signature" line for any signer). Overridable only by an explicit gate argument.
DEFAULT_SSH_KEYGEN = Path("/usr/bin/ssh-keygen")
# the verifier's whole environment: fixed, nothing copied from this process
_VERIFY_ENV = {"LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never"}
# st_mode bits (the values of stat.S_IFMT / S_IFREG / S_IFDIR / S_IWGRP / S_IWOTH, which are fixed by POSIX; the
# autonomic package's standard-library allowlist does not include ``stat``)
_S_IFMT, _S_IFREG, _S_IFDIR, _S_IWGRP, _S_IWOTH = 0o170000, 0o100000, 0o040000, 0o000020, 0o000002
# who may own a trust anchor and every directory above it: root alone
_ROOT: frozenset[int] = frozenset({0})


def trust_anchor_problem(path: Path | str, *, what: str, owners: frozenset[int] = _ROOT,
                         group_write_exempt: tuple[str, int] | None = None) -> tuple[str | None, str | None]:
    """``(why, None)`` if the file at ``path`` may not be trusted to verify a signature, else ``(None, the
    resolved path)``, which is the path to use. A trust anchor (the verifier binary, the allowed-signers
    file) must be an absolute path that resolves to a regular file owned by one of ``owners`` (root),
    writable by neither its group nor anyone else nor, through an ACL, this process's uid, in directories that
    are all the same, up to ``/``: the requester's uid can then neither edit it nor swap it between this
    check and its use (the rule of sshd's StrictModes and sudo's sudoers, with root as the only owner).

    ``group_write_exempt``, an exact absolute path and a gid, relaxes the rule for that ONE component of the
    resolved chain: it passes the mode test when it is owned by root with that gid, group-writable and not
    world-writable, and the uid-write test is not run on it (the write it would find is that group's). Every
    other component is held to the whole rule. It exists for the automation interpreter's rule, ``/Applications``
    and the ``admin`` group (autogrant r5.3)."""
    fault = _fault(path, what, owners, group_write_exempt)
    return (fault[2], None) if fault[0] is not None else (None, fault[3])


def trust_anchor_fault(path: Path | str, *, what: str, owners: frozenset[int] = _ROOT,
                       group_write_exempt: tuple[str, int] | None = None) -> tuple[str | None, str | None]:
    """:func:`trust_anchor_problem`'s rule, answered as a short code instead of a sentence: ``("<code>:<path>",
    None)`` or ``(None, the resolved path)``. The codes: ``not_absolute``, ``unreadable``, ``not_regular``,
    ``not_owned``, ``group_writable``, ``uid_mismatch``, ``writable``, ``not_directory``; the path is the
    component that failed."""
    fault = _fault(path, what, owners, group_write_exempt)
    return (f"{fault[0]}:{fault[1]}", None) if fault[0] is not None else (None, fault[3])


def _fault(path: Path | str, what: str, owners: frozenset[int],
           exempt: tuple[str, int] | None) -> tuple[str | None, str, str | None, str | None]:
    """``(code, component, sentence, None)`` for the first failure, else ``(None, "", None, resolved)``."""
    p = Path(path)
    if not p.is_absolute():
        return "not_absolute", str(p), f"{what} path {p} is not absolute", None
    real = os.path.realpath(p)

    def exempted(at: str, st: os.stat_result) -> bool:
        return (exempt is not None and at == exempt[0] and st.st_uid == 0 and st.st_gid == exempt[1]
                and bool(st.st_mode & _S_IWGRP) and not st.st_mode & _S_IWOTH)

    try:
        st = os.lstat(real)
    except OSError as e:
        return "unreadable", real, f"{what} {p} cannot be read ({type(e).__name__})", None
    if st.st_mode & _S_IFMT != _S_IFREG:
        return "not_regular", real, f"{what} {p} is not a regular file", None
    if st.st_uid not in owners:
        return "not_owned", real, f"{what} {p} is not owned by root (uid {st.st_uid})", None
    file_exempt = exempted(real, st)
    if st.st_mode & (_S_IWGRP | _S_IWOTH) and not file_exempt:
        return "group_writable", real, f"{what} {p} is group- or world-writable", None
    # the mode bits do not show an ACL: ask the kernel whether THIS uid may write (access(2) applies ACLs,
    # and tests the REAL uid, so a process whose effective uid differs is refused rather than guessed about)
    if os.getuid() != os.geteuid():
        return ("uid_mismatch", real,
                f"{what} {p} cannot be checked: real uid {os.getuid()} is not effective uid {os.geteuid()}", None)
    check_access = os.getuid() not in owners
    if check_access and not file_exempt and os.access(real, os.W_OK):
        return "writable", real, f"{what} {p} is writable by uid {os.getuid()}", None
    parent = os.path.dirname(real)
    while True:
        try:
            dst = os.lstat(parent)
        except OSError as e:
            return "unreadable", parent, f"{what} directory {parent} cannot be read ({type(e).__name__})", None
        if dst.st_mode & _S_IFMT != _S_IFDIR:
            return "not_directory", parent, f"{what} directory {parent} is not a directory", None
        if dst.st_uid not in owners:
            return "not_owned", parent, f"{what} directory {parent} is not owned by root (uid {dst.st_uid})", None
        dir_exempt = exempted(parent, dst)
        if dst.st_mode & (_S_IWGRP | _S_IWOTH) and not dir_exempt:
            return "group_writable", parent, f"{what} directory {parent} is group- or world-writable", None
        if check_access and not dir_exempt and os.access(parent, os.W_OK):
            return "writable", parent, f"{what} directory {parent} is writable by uid {os.getuid()}", None
        if parent == os.path.dirname(parent):
            return None, "", None, real
        parent = os.path.dirname(parent)
