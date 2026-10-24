"""Shared formatting for the ported probes."""

from __future__ import annotations

from ..fixtures.clients import Curl
from ..fixtures.stack import Seen


def facts(seen: Seen) -> str:
    """The side-channel observations a verdict is stated in: decider frames, DNS lookups, origin bytes, glue events."""
    ev = ",".join(sorted({e["t"] for e in seen.events if e["t"] not in ("req",)})) or "-"
    return (f"frames={len(seen.req_frames())}req/{len(seen.verdicts())}verdict dns={len(seen.dns)} "
            f"origin={seen.origin_bytes()}B events={ev}")


def client(res: Curl) -> str:
    return f"curl rc={res.rc} status={res.status} {res.first_line!r} deny={res.deny_reason}"
