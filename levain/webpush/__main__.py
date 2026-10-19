"""``python -m levain.webpush send``: the detached child :func:`levain.webpush.sender.spawn` starts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from levain.webpush import sender


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m levain.webpush")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--queue-dir", required=True, type=Path)
    s.add_argument("--kind", required=True)
    s.add_argument("--ref", default=None)
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--url", default="/")
    a = p.parse_args(argv)
    sender.send(a.queue_dir, kind=a.kind, ref=a.ref, title=a.title, body=a.body, url=a.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
