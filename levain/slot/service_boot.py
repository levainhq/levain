"""The shared service bootstrap (CONTRACT §22, §23): what every service unit runs, through the service
entry script, before its own program.

``service-entry <name>``: ``<name>`` must be a §1.3 service of this host. A non-root service then
checks its ``R/`` dir (:func:`check_run_dir`) and EXITS non-zero when the dir is absent or not the
one root made with the §2.2 owner, group and mode: launchd has no start ordering, so after a reboot a
service can start before the reaper's boot pass recreated ``R/``, and the restart (``KeepAlive`` /
``Restart=always``) brings it back once the dir is there (§22). Then the program
``levain/slot/services/<name>.py`` runs as ``__main__``.
"""
from __future__ import annotations

import grp
import os
import pwd
import runpy
import stat
import sys
from typing import Callable, Mapping, Sequence

from levain.firing.hands import HostOS
from levain.slot.layout import RUN, Row, base_run_rows

#: The §2.2 dir each non-root service binds in (its run dir); root services have none to wait for.
RUN_DIR_OF: Mapping[str, str] = {"control": "control", "keys": "keys", "qbroker": "qb", "dispatch": "planner"}

EXIT_NO_RUN_DIR = 75   # EX_TEMPFAIL: try again later
EXIT_USAGE = 64        # EX_USAGE


def host_os() -> HostOS:
    return "darwin" if sys.platform == "darwin" else "linux"


def run_dir_row(host: HostOS, service: str) -> Row:
    want = RUN[host] / RUN_DIR_OF[service]
    return next(r for r in base_run_rows(host) if r.path == want)


def _is_root_dir(st: os.stat_result, mode: int) -> bool:
    return stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == mode


def check_run_dir(host: HostOS, service: str, *,
                  lstat: Callable[[str], os.stat_result] = os.lstat,
                  uid_of: Callable[[str], int] = lambda n: pwd.getpwnam(n).pw_uid,
                  gid_of: Callable[[str], int] = lambda n: grp.getgrnam(n).gr_gid) -> tuple[bool, str]:
    """``R/`` is root's, with §2.2's mode (so only root made what is in it) and the service's dir under
    it is a directory (never a link) with exactly the §2.2 row's owner, group and mode."""
    R = RUN[host]
    row = run_dir_row(host, service)
    try:
        top = lstat(str(R))
        st = lstat(str(row.path))
    except OSError as e:
        return False, f"{e.filename or row.path} is absent ({e.strerror or e})"
    if not _is_root_dir(top, 0o755):
        return False, f"{R} is not root's 0755 directory"
    try:
        owner = 0 if row.owner == "root" else uid_of(row.owner)
        group = 0 if row.group in ("root", "wheel") else gid_of(row.group)
    except KeyError as e:
        return False, f"no account {e.args[0]!r}"
    if not stat.S_ISDIR(st.st_mode):
        return False, f"{row.path} is not a directory"
    if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (owner, group, row.mode):
        return False, (f"{row.path} is {st.st_uid}:{st.st_gid} {stat.S_IMODE(st.st_mode):04o}, "
                       f"not {row.owner}:{row.group} {row.mode:04o}")
    return True, ""


def main(argv: Sequence[str], *, host: HostOS | None = None,
         check: Callable[[HostOS, str], tuple[bool, str]] = check_run_dir) -> int:
    from levain.slot.units import services_for

    host = host or host_os()
    names = {s.name: s for s in services_for(host)}
    if len(argv) != 1 or argv[0] not in names:
        print(f"levain service-entry: usage: service-entry <{'|'.join(names)}>", file=sys.stderr)
        return EXIT_USAGE
    name = argv[0]
    if names[name].user != "root":
        ok, why = check(host, name)
        if not ok:
            print(f"levain {name}: run dir not ready: {why}", file=sys.stderr)
            return EXIT_NO_RUN_DIR
    runpy.run_module(f"levain.slot.services.{name}", run_name="__main__", alter_sys=True)
    return 0


__all__ = ["EXIT_NO_RUN_DIR", "EXIT_USAGE", "RUN_DIR_OF", "check_run_dir", "host_os", "main", "run_dir_row"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

