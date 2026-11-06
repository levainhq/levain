"""The terminal seat's record-only hooks (D1: Levain records and bounds).

``record_main`` is the PreToolUse hook: it appends one row per tool call (tool, sha256 of the input, cwd) to a
JSONL file in a host-owned directory the seat's sandbox cannot write, prints nothing and never decides. It exists
because the seat loads none of the operator's own settings, so the observe logger there is blind on these seats.
``bound_main`` is the PostToolUse / PostToolUseFailure hook: it files the finished call in the consent ledger,
where the BOUND classifier reads what the vendor's sandbox refused inside it.

Both exit 0 whatever happens: a record is never a gate. A seat whose rows are zero after tool calls the transcript
shows is BLIND, never quiet (:func:`blind`).

Run as ``python -I -B -m levain.seat_record pre|post <record dir>``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

__all__ = ["RECORD_FILE", "record_main", "bound_main", "read_rows", "blind"]

RECORD_FILE = "calls.jsonl"
_MAX_ROW = 4096


def _digest(tool_input: Any) -> str:
    try:
        blob = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        blob = repr(tool_input)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _str(v: Any) -> str | None:
    return v[:512] if isinstance(v, str) else None


def _append(record_dir: Path, row: dict[str, Any]) -> None:
    line = (json.dumps(row, sort_keys=True) + "\n").encode()
    if len(line) > _MAX_ROW:
        return
    record_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(record_dir / RECORD_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line)   # one write: concurrent hooks interleave whole rows
    finally:
        os.close(fd)


def record_main(argv: Sequence[str] | None = None, stdin: Any = None) -> int:
    """PreToolUse: ``argv[0]`` is the record directory. Prints nothing, decides nothing, returns 0."""
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        payload = json.loads((stdin if stdin is not None else sys.stdin).read())
        if not args or not isinstance(payload, dict):
            return 0
        _append(Path(args[0]), {
            "t": round(time.time(), 3), "event": "pre", "tool": _str(payload.get("tool_name")),
            "input_sha256": _digest(payload.get("tool_input")), "cwd": _str(payload.get("cwd")),
            "session": _str(payload.get("session_id")), "tool_use_id": _str(payload.get("tool_use_id"))})
    except BaseException:  # noqa: BLE001 - the contract is that the harness never notices this hook
        pass
    return 0


def bound_main(stdin: Any = None) -> int:
    """PostToolUse / PostToolUseFailure: file the call in the consent ledger (its BOUND record). Returns 0."""
    try:
        payload = json.loads((stdin if stdin is not None else sys.stdin).read())
        from levain.consent import ledger
        ledger.hook_record("claude_code", payload)
        ledger.hook_fill("claude_code", payload)
    except BaseException:  # noqa: BLE001
        pass
    return 0


def read_rows(record_dir: Path) -> list[dict[str, Any]]:
    """The rows written so far; an unreadable line is skipped."""
    out: list[dict[str, Any]] = []
    try:
        text = (Path(record_dir) / RECORD_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def blind(record_dir: Path, tool_calls_seen: int) -> bool:
    """True when the transcript shows tool calls (``tool_calls_seen`` > 0) and the record holds none."""
    return tool_calls_seen > 0 and not read_rows(record_dir)


def main(argv: Sequence[str]) -> int:
    if argv and argv[0] == "pre":
        return record_main(argv[1:])
    if argv and argv[0] == "post":
        return bound_main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
