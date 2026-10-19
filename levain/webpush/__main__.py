"""``python -m levain.webpush send``: the detached child :func:`levain.webpush.sender.spawn` starts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from levain.webpush import sender


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
    sender.send(a.queue_dir, kind=a.kind, ref=ref, title=title, body=body, url=url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
