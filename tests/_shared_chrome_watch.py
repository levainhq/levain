"""Parent-death watchdog for the session Chrome (stdlib only; started by ``tests/_shared_chrome.py`` in its own session).

``python _shared_chrome_watch.py <owner pid> <chrome pgid> <profile dir>``: once the owner (the pytest process) is gone, kill the
Chrome's process group and remove the profile, then exit; if the Chrome's group disappears first, remove the profile and exit.
A SIGKILLed pytest runs no cleanup of its own, and an orphan headless Chrome keeps its profile and gets new-window requests
routed to it.
"""
import os
import shutil
import signal
import sys
import time


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str]) -> int:
    owner, pgid, profile = int(argv[0]), int(argv[1]), argv[2]
    while _alive(owner) and _group_alive(pgid):
        time.sleep(0.25)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(50):
        if not _group_alive(pgid):
            break
        time.sleep(0.1)
    for _ in range(5):   # a dying Chrome can still write into it for a moment
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile):
            break
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
