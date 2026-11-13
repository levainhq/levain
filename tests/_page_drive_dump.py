"""``chrome --dump-dom`` for the tests that read a page's serialized DOM, with a bounded retry and a diagnosable failure.

Why it exists. ``--dump-dom --virtual-time-budget`` hands Chrome a page and waits for it to settle: the tab fires its load event, the
virtual-time budget runs out, the DOM is printed and Chrome quits. If the tab never gets there Chrome never exits and the caller's
timeout is the only thing that ends the run (Linux CI run 43373428452: ``test_reversed_order_groups_and_an_error_panel_are_drawn``,
one hang in 758 browser tests, Chrome's stderr only dbus noise). The page registers a service worker and opens its change stream at
load, the same two things that were running in the window of the sibling hang (a reload sent as the stream starts,
``test_the_lists_later_group_tag_is_one_line_on_a_phone``), so this is probably the same family, but nothing measured here shows the mechanism.

What it does, all test-side:
  * each attempt is ``_page_drive_run.run``: its own process group, killed whole on a timeout, its scratch directory removed;
  * each attempt has a fresh ``--user-data-dir`` (no profile, cache or service-worker registration is shared between attempts or
    with an earlier hung Chrome) that is removed after;
  * ONE retry of the whole dump when an attempt times out. A retry hides a hang only if the hang is deterministic, and then both
    attempts hang and the test FAILS: the assertion message carries each attempt's wall time, stderr tail, the box load and the size of
    ``/dev/shm``, so a repeat says what the box looked like (``[prescription: reasoned]``; see the lane return for what was measured).

Flags: ``CHROME_SAFE_FLAGS`` (tests/chrome_flags.json) come first, as at every Chrome launch site.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence

from tests._chrome_flags import CHROME_SAFE_FLAGS
from tests._page_drive_run import run as _run

ATTEMPTS = 2           # one try and ONE retry; a third would turn a deterministic hang into minutes of wait
STDERR_TAIL = 700      # bytes of each attempt's stderr kept for the failure message


class DumpDomHung(AssertionError):
    """Every attempt timed out. An AssertionError so the test FAILS (a deterministic hang is not retried away)."""


def _tail(text: object) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "")[-STDERR_TAIL:] if isinstance(text, str) else ""


def _box() -> str:
    """The box facts that decide whether a hang was the box: load average and the size of /dev/shm."""
    try:
        load = ", ".join(f"{x:.1f}" for x in os.getloadavg())
    except OSError:
        load = "?"
    try:
        shm = shutil.disk_usage("/dev/shm")
        shm_s = f"{shm.total // (1024 * 1024)} MB total, {shm.free // (1024 * 1024)} MB free"
    except OSError:
        shm_s = "none"
    return f"load {load}; /dev/shm {shm_s}"


def dump_dom(chrome: str, url: str, *, timeout: float = 60, extra: Sequence[str] = (),
             runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> str:
    """The page's serialized DOM. ``timeout`` bounds EACH attempt. ``runner`` is ``_page_drive_run.run`` unless a test names another."""
    runner = runner or _run
    attempts: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        profile = tempfile.mkdtemp(prefix="levain-dump-profile-")
        started = time.monotonic()
        try:
            argv = [chrome, *CHROME_SAFE_FLAGS, f"--user-data-dir={profile}", "--dump-dom", "--virtual-time-budget=8000",
                    "--window-size=1440,1000", *extra, url]
            return runner(argv, timeout=timeout).stdout
        except subprocess.TimeoutExpired as e:
            attempts.append(f"attempt {n}: no exit within {timeout:g}s (wall {time.monotonic() - started:.1f}s); "
                            f"stdout {len(e.stdout or '')} bytes; stderr tail: {_tail(e.stderr)!r}")
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    raise DumpDomHung("chrome --dump-dom hung on every attempt (" + _box() + "), so the retry did not hide it:\n  " + "\n  ".join(attempts))
