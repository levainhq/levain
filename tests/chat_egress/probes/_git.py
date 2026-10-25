"""Helpers shared by the round-4 git probes."""

from __future__ import annotations

PK = b"0014command=ls-refs\n0000"          # the spike's pk.bin: a small upload-pack request body
GH = ["-H", "Content-Type: application/x-git-upload-pack-request", "-H", "Accept: application/x-git-upload-pack-result",
      "-H", "Git-Protocol: version=2"]
ADV_Q = "info/refs?service=git-upload-pack"


def creds(entry: dict) -> list[str]:
    """Every credential-bearing header line in an origin request's raw head (git_origin logs the raw head)."""
    import re
    raw = entry.get("raw_head", "")
    return [m.group(0).strip("'\\ ") for m in re.finditer(r"(?i)(authorization|cookie|x[-_.]api[-_.]key|api-key|x-auth-token)\s*:[^\\]*", raw)]


def origin_reqs(seen) -> list[dict]:
    return [d for d in seen.origin if d.get("method")]
