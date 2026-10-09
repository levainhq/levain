#!/usr/bin/env python3
"""Measure what a phone fetches from a served cockpit: the manifest plus every panel in the compact
profile, as bytes on the wire (K1 residue: "compact profile of flow's live store measured and
reported, target 40 kB: a number, not a pass").

    cockpit_measure.py http://127.0.0.1:PORT [--token T]

GET only. Prints the manifest size, the largest compact panels and the total."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

TARGET = 40_000


def get(url: str, token: str | None) -> bytes:
    req = urllib.request.Request(url, headers={"X-Levain-Write-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - operator-named URL, GET only
        return r.read()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    base = argv[0].rstrip("/")
    token = argv[argv.index("--token") + 1] if "--token" in argv else None
    manifest_raw = get(f"{base}/cockpit/manifest.json", token)
    manifest = json.loads(manifest_raw)
    sizes = {"manifest": len(manifest_raw)}
    for pid in manifest["panels"]:
        sizes[pid] = len(get(f"{base}/cockpit/panel/{urllib.parse.quote(pid)}.json?profile=compact", token))
    total = sum(sizes.values())
    for name, n in sorted(sizes.items(), key=lambda kv: -kv[1])[:10]:
        print(f"{name:28s} {n:8d} B")
    print(f"TOTAL compact (manifest + {len(sizes) - 1} panels): {total} B = {total / 1000:.1f} kB; "
          f"target {TARGET // 1000} kB -> {'over' if total > TARGET else 'under'} by {abs(total - TARGET)} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
