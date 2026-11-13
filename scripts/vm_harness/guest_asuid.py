"""Guest-side: run a command as a given uid/gid with an EXPLICIT supplementary group list (stdlib only, self-contained).

Why it exists (1111-32 derive B, F10 / SO_PEERCRED): the real wrong-uid-but-filesystem-permitted test needs a peer that
holds slot A's GROUP (so the socket's 0750/setgid directory lets it connect) while its UID is slot B's (so the peer
check must drop it). ``sudo -u _levain_sB`` cannot build that: sudo uses the target's own groups. This does.

The sitting copies this one file into the guest tools dir (``/tmp/fire``) and runs it as root through ``guest_cmd``::

    python3 -m scripts.vm_harness.guest_cmd <vm> --profile darwin --sudo --out F -- \\
        /usr/local/bin/python3.13 -I /tmp/fire/guest_asuid.py <b-uid> <b-gid> <a-gid> -- \\
        /usr/local/bin/python3.13 -I /tmp/fire/l3_peerprobe.py <socket>

usage: ``guest_asuid.py UID GID GROUPS -- ABSOLUTE-COMMAND [ARG...]`` where GROUPS is a comma list of gids, or ``-`` for
none. Root only. Order is setgroups, setgid, setuid (the only order that works), then the wrapper PROVES the drop by
trying to get root back (it must fail) and prints the credentials it ended with to stderr before the exec. Nothing is
exec'd unless every step held. The environment is inherited unchanged (no HOME / PATH rewrite): the command is an
absolute path and, for the probe, needs none.

Exit: 64 malformed invocation, 77 not root, 71 an OS error while dropping, 70 the drop did not hold. Never imports the
repo: it runs in the guest with ``-I``.
"""
from __future__ import annotations

import os
import sys

EX_SOFTWARE = 70
EX_OSERR = 71
EX_USAGE = 64
EX_NOPERM = 77


def _bye(err, code: int, msg: str):
    err.write(f"guest_asuid: {msg}\n")
    err.flush()
    raise SystemExit(code)


def drop_and_exec(uid: int, gid: int, groups: list[int], cmd: list[str], *, os_=os, err=sys.stderr) -> None:
    if os_.geteuid() != 0:
        _bye(err, EX_NOPERM, "must run as root (setgroups/setuid need it); nothing was run")
    try:
        os_.setgroups(list(groups))
        os_.setgid(gid)
        os_.setuid(uid)
    except OSError as e:
        _bye(err, EX_OSERR, f"dropping to uid={uid} gid={gid} groups={groups} failed: {e}; nothing was run")
    if (os_.getuid(), os_.geteuid()) != (uid, uid) or (os_.getgid(), os_.getegid()) != (gid, gid):
        _bye(err, EX_SOFTWARE, f"after the drop uid/euid={os_.getuid()}/{os_.geteuid()} gid/egid={os_.getgid()}/{os_.getegid()}, "
                               f"wanted {uid}/{gid}; nothing was run")
    try:
        os_.setuid(0)
    except OSError:
        pass
    else:
        _bye(err, EX_SOFTWARE, "root could be regained after the drop; nothing was run")
    err.write(f"guest_asuid: running as uid={os_.getuid()} euid={os_.geteuid()} gid={os_.getgid()} "
              f"groups={sorted(os_.getgroups())}: {cmd}\n")
    err.flush()
    os_.execv(cmd[0], cmd)


def _num(text: str, what: str, err) -> int:
    if not text.isascii() or not text.isdigit():
        _bye(err, EX_USAGE, f"{what} must be a non-negative integer, got {text!r}")
    return int(text)


def main(argv: list[str], *, os_=os, err=sys.stderr) -> None:
    if len(argv) < 5 or argv[3] != "--":
        _bye(err, EX_USAGE, "usage: guest_asuid.py UID GID GROUPS -- ABSOLUTE-COMMAND [ARG...]   (GROUPS: 1,2,3 or -)")
    uid, gid = _num(argv[0], "UID", err), _num(argv[1], "GID", err)
    if uid == 0:
        _bye(err, EX_USAGE, "UID 0 refused: this wrapper exists to DROP privilege")
    groups = [] if argv[2] == "-" else [_num(g, "a group", err) for g in argv[2].split(",")]
    cmd = argv[4:]
    if not cmd[0].startswith("/"):
        _bye(err, EX_USAGE, f"the command must be an absolute path (no PATH search under root), got {cmd[0]!r}")
    drop_and_exec(uid, gid, groups, cmd, os_=os_, err=err)


if __name__ == "__main__":
    main(sys.argv[1:])
