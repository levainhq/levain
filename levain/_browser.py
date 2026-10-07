"""Open a URL in the operator's browser, from a child process levain starts with an allowlisted
environment (:func:`levain.launch.open_browser`). ``webbrowser`` starts the browser with this
process's whole environment, which in levain itself holds every variable carried across the launch
re-exec; run here, it holds only what :func:`levain.launch.child_env` gave it. The URL arrives on
stdin, never on a command line.
"""
from __future__ import annotations

import json
import sys


def open_url(url: str, unlocked: str | None = None) -> None:
    """Open the cockpit. ``unlocked`` (the URL with the chat token in its fragment) goes ONLY to macOS's osascript
    controller, which hands the URL over on osascript's stdin and then as an Apple Event, never on a command line.
    Every other controller (``open``, xdg-open, a browser binary, ``$BROWSER``) puts the URL in argv, which other OS
    users can read from the process table: the very callers the chat token exists to keep out. So the osascript
    controller is called directly, never through ``webbrowser.open``, which on a failure would hand the same URL to
    the next registered controller; if it is not the default or fails, the plain URL opens through the usual chain
    and the token field asks."""
    import webbrowser

    if unlocked is not None:
        try:
            ctl = webbrowser.get()
            if isinstance(ctl, webbrowser.MacOSXOSAScript) and ctl.open(unlocked):
                return
        except Exception:  # noqa: BLE001 — no usable controller, or no MacOSXOSAScript on this platform
            pass
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a headless box without a browser is fine
        pass


def main() -> int:
    data = json.loads(sys.stdin.read())
    open_url(data["url"], data.get("unlocked"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
