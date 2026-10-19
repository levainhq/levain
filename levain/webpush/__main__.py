"""``python -m levain.webpush send``: the detached child :func:`levain.webpush.sender.spawn` starts."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from levain.webpush import sender


_LOG_LINES = 200


def _log_error(queue_dir: Path, exc: Exception) -> None:
    try:
        log = queue_dir / "webpush" / "send-errors.log"
        old = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {type(exc).__name__}: {exc}".replace("\n", " ")
        log.write_text("\n".join(old[-(_LOG_LINES - 1):] + [line]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - a log that cannot be written is still not a reason to hang the child
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m levain.webpush")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--queue-dir", required=True, type=Path)
    s.add_argument("--kind", required=True)
    a = p.parse_args(argv)
    # the text arrives as JSON on stdin (see sender.spawn): never argv, so it is not an option and not in `ps`
    try:
        f = json.loads(sys.stdin.buffer.read(65536).decode("utf-8"))
        title, body, ref, url = f["title"], f["body"], f.get("ref"), f.get("url") or "/"
        if not all(isinstance(x, str) for x in (title, body, url)) or not (ref is None or isinstance(ref, str)):
            raise ValueError("fields")
    except (ValueError, KeyError, TypeError, AttributeError):
        print("levain.webpush send: expected a JSON object {title, body, ref, url} on stdin", file=sys.stderr)
        return 2
    try:
        sender.send(a.queue_dir, kind=a.kind, ref=ref, title=title, body=body, url=url)
    except Exception as exc:  # noqa: BLE001 - the child's stderr is /dev/null: the failure is said in a bounded log
        _log_error(a.queue_dir, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
