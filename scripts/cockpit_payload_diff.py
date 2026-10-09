#!/usr/bin/env python3
"""Diff live/saved payload keys against design §6's field map (K1 DONE: "the §6 diff script against
fresh payloads reports zero unmapped keys").

    cockpit_payload_diff.py board=URL_or_FILE substrate=... header=... sky=... fleet=...

Each argument is ``<payload>=<source>`` where ``<payload>`` is one of board, substrate, header, sky,
fleet and ``<source>`` is a file path or an http(s) URL (fetched with GET only). Exits 0 when every
key is covered by the table, 1 when any key is unmapped. Entries the table marks ``literal=False``
(row-level consequences §6 does not spell out) are listed separately: they are proposed amendments
to §6, not text the design already contains."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from levain.cockpit.payload_map import TABLE, classify  # noqa: E402


def key_paths(obj, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            # a key may itself contain dots (a repo name); keep them from reading as a path separator
            k = str(k).replace(".", "\u2024")
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            out |= key_paths(v, p)
    elif isinstance(obj, list):
        for v in obj:
            out |= key_paths(v, prefix + "[]")
    return out


def load(src: str):
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=30) as r:  # noqa: S310 - GET of an operator-named URL
            return json.load(r)
    return json.loads(Path(src).read_text())


def diff(payload: str, data) -> dict:
    unmapped, nonliteral, dropped = [], {}, {}
    seen = set()
    for path in sorted(key_paths(data)):
        hit = classify(payload, path)
        if hit is None:
            unmapped.append(path)
            continue
        disposition, target, literal = hit
        seen.add(path)
        if disposition == "unmapped":
            unmapped.append(f"{path}  (named unmapped: {target})")
        elif not literal:
            nonliteral[path] = target
        elif disposition == "drop":
            dropped[path] = target
    return {"payload": payload, "keys": len(seen) + len(unmapped), "unmapped": unmapped,
            "not_literal_in_section_6": nonliteral, "dropped": dropped}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    bad = 0
    for arg in argv:
        name, _, src = arg.partition("=")
        if name not in {r[0] for r in TABLE} or not src:
            print(f"bad argument {arg!r}")
            return 2
        res = diff(name, load(src))
        print(f"== {name}: {res['keys']} distinct key paths, {len(res['unmapped'])} unmapped, "
              f"{len(res['not_literal_in_section_6'])} mapped only by an entry §6 does not spell out")
        for p in res["unmapped"]:
            print(f"   UNMAPPED  {p}")
        for p, t in res["not_literal_in_section_6"].items():
            print(f"   amend-§6  {p}  ->  {t}")
        bad += len(res["unmapped"])
    print("RESULT:", "zero unmapped keys" if not bad else f"{bad} unmapped key path(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
