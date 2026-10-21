"""output_compare — a black-box verifier driver: each step's root-captured outputs against the pinned expectations.

Run as ``<python> -I -S output_compare.py <input.json> <run_dir>`` (CONTRACT §3.5, the fixed driver argv). ``-S``
leaves only the standard library importable, so this file imports nothing from levain and nothing else: it is the
driver template a signed ``verifier`` item pins by the SHA-256 of these bytes, and an edit to it is a new digest.

``input.json`` is the entry's ``driver_input``, exactly ``{"steps": [expectation, ...]}``, one expectation per ticket
step in order, each ``{"exit": int}`` plus any of ``"stdout_sha256": "sha256:<64 hex>"`` (of the captured stdout
bytes), ``"stdout_equals": str`` (the captured stdout is exactly its UTF-8 bytes) and ``"stderr_empty": bool`` (true:
nothing on stderr). ``<run_dir>`` holds what root wrote for each step ``k``: ``step<k>.exit`` (``{exit, signal,
wall_s, timed_out, truncated_bytes, lingering_killed}``), ``step<k>.out`` and ``step<k>.err``, where ``k`` is 0-BASED,
the report's numbering: the first ticket step's files are ``step0.exit``, ``step0.out``, ``step0.err``. Each is a
regular file; anything else (a directory, a FIFO, a symlink) is not a capture root wrote. A step passes when it was not timed out or signalled, exited with the pinned code, and every pinned
output check holds; an output check on a capture root truncated fails, since the bytes compared are not all of them.

Stdout carries the ``levain-dispatch-verifier-report/1`` body as canonical JSON (keys sorted, no whitespace, UTF-8,
no trailing newline) and nothing else. Black-box by construction: it reads only root-written files, never the repo.
An input or capture that is not the shape above is the infrastructure's failure, not the code's: the driver writes a
line to stderr and exits 2, so the session ends with no report (``driver-exit:2``) rather than a verdict.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys

REPORT_FORMAT = "levain-dispatch-verifier-report/1"
MAX_STEPS = 16
# mirrors the contract's V25, the driver_input cap
MAX_INPUT_BYTES = 256 * 1024
# mirrors the contract's V21, the cap root writes each capture under; a larger file is not one root wrote
MAX_CAPTURE_BYTES = 1 << 20
# an .exit record is a handful of small integers
MAX_EXIT_BYTES = 4096
EXPECT_KEYS = frozenset({"exit", "stdout_sha256", "stdout_equals", "stderr_empty"})
EXIT_KEYS = frozenset({"exit", "signal", "wall_s", "timed_out", "truncated_bytes", "lingering_killed"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class Infrastructure(Exception):
    """The input or a capture is not the shape this driver reads: no verdict is given."""


def _no_float(text: str) -> None:
    raise Infrastructure("a number that is not an integer")


def _no_constant(text: str) -> None:
    raise Infrastructure(f"the JSON constant {text}")


def _pairs(pairs: list) -> dict:
    out = {}
    for k, v in pairs:
        if k in out:
            raise Infrastructure("a duplicate key")
        out[k] = v
    return out


def _json(raw: bytes, what: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_no_float,
                          parse_constant=_no_constant)
    except Infrastructure as exc:
        raise Infrastructure(f"{what}: {exc}") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Infrastructure(f"{what} is not JSON") from None


def _read(path: str, cap: int, what: str) -> bytes:
    # open, fstat, read and close are ONE guarded unit: any fault is Infrastructure (exit 2), never a traceback.
    # O_NONBLOCK so a FIFO cannot block the open; the fstat refuses anything but a regular file before a read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise Infrastructure(f"{what} cannot be opened ({type(exc).__name__})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Infrastructure(f"{what} is not a regular file")
        chunks, left = [], cap + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise Infrastructure(f"{what} cannot be read ({type(exc).__name__})") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(data) > cap:
        raise Infrastructure(f"{what} is over {cap} bytes")
    return data


def _int(v: object, *, nullable: bool = False) -> bool:
    return (nullable and v is None) or (type(v) is int and v >= 0)


def expectations(raw: bytes) -> list:
    """The pinned expectations ``input.json`` holds, checked whole, each ``stdout_equals`` already encoded to the UTF-8
    bytes :func:`judge` compares; raises :class:`Infrastructure`."""
    if len(raw) > MAX_INPUT_BYTES:
        raise Infrastructure(f"input.json is over {MAX_INPUT_BYTES} bytes")
    obj = _json(raw, "input.json")
    if not isinstance(obj, dict) or set(obj) != {"steps"}:
        raise Infrastructure('input.json is exactly {"steps": [...]}')
    steps = obj["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise Infrastructure(f"input.json's steps are a list of 1 to {MAX_STEPS}")
    for k, e in enumerate(steps):
        if not isinstance(e, dict) or "exit" not in e or not set(e) <= EXPECT_KEYS:
            raise Infrastructure(f"expectation {k} is exit plus any of stdout_sha256, stdout_equals, stderr_empty")
        if type(e["exit"]) is not int or not 0 <= e["exit"] <= 255:
            raise Infrastructure(f"expectation {k}'s exit is an integer from 0 to 255")
        if "stdout_sha256" in e and (not isinstance(e["stdout_sha256"], str)
                                     or _DIGEST.fullmatch(e["stdout_sha256"]) is None):
            raise Infrastructure(f"expectation {k}'s stdout_sha256 is sha256:<64 hex>")
        if "stdout_equals" in e:
            if not isinstance(e["stdout_equals"], str):
                raise Infrastructure(f"expectation {k}'s stdout_equals is text")
            try:
                e["stdout_equals"] = e["stdout_equals"].encode("utf-8")
            except UnicodeEncodeError:
                raise Infrastructure(f"expectation {k}'s stdout_equals is not encodable as UTF-8") from None
        if "stderr_empty" in e and type(e["stderr_empty"]) is not bool:
            raise Infrastructure(f"expectation {k}'s stderr_empty is true or false")
    return steps


def _exit_record(raw: bytes, k: int) -> dict:
    rec = _json(raw, f"step{k}.exit")
    if not (isinstance(rec, dict) and set(rec) == EXIT_KEYS and _int(rec["exit"], nullable=True)
            and _int(rec["signal"], nullable=True) and _int(rec["wall_s"]) and type(rec["timed_out"]) is bool
            and _int(rec["truncated_bytes"]) and _int(rec["lingering_killed"])):
        raise Infrastructure(f"step{k}.exit is not the contract's exit record")
    return rec


def judge(expect: dict, rec: dict, out: bytes, err: bytes) -> tuple[str, str]:
    """``(verdict, note)`` for one step. Notes are fixed text and integers, never captured bytes."""
    if rec["timed_out"]:
        return "fail", f"timed out after {rec['wall_s']} s"
    if rec["signal"] is not None:
        return "fail", f"killed by signal {rec['signal']}"
    if rec["exit"] != expect["exit"]:
        return "fail", f"exit {rec['exit']}, expected {expect['exit']}"
    checks = [c for c in ("stdout_sha256", "stdout_equals") if c in expect]
    if expect.get("stderr_empty") is True:
        checks.append("stderr_empty")
    if checks and rec["truncated_bytes"]:
        return "fail", f"output truncated by {rec['truncated_bytes']} bytes; the pinned output checks cannot hold"
    if "stdout_sha256" in expect and "sha256:" + hashlib.sha256(out).hexdigest() != expect["stdout_sha256"]:
        return "fail", "stdout is not the pinned sha256"
    if "stdout_equals" in expect and out != expect["stdout_equals"]:
        return "fail", "stdout is not the pinned text"
    if "stderr_empty" in checks and err:
        return "fail", f"stderr is not empty ({len(err)} bytes)"
    return "pass", f"exit {rec['exit']} as pinned" + (f"; {', '.join(checks)} held" if checks else "")


def report(input_path: str, run_dir: str) -> dict:
    """The report body for ``run_dir`` against the expectations at ``input_path``; raises :class:`Infrastructure`."""
    steps = expectations(_read(input_path, MAX_INPUT_BYTES, "input.json"))
    verdicts = []
    for k, expect in enumerate(steps):
        base = os.path.join(run_dir, f"step{k}")
        rec = _exit_record(_read(base + ".exit", MAX_EXIT_BYTES, f"step{k}.exit"), k)
        out = _read(base + ".out", MAX_CAPTURE_BYTES, f"step{k}.out")
        err = _read(base + ".err", MAX_CAPTURE_BYTES, f"step{k}.err")
        verdict, note = judge(expect, rec, out, err)
        verdicts.append({"step": k, "verdict": verdict, "note": note})
    result = "pass" if all(v["verdict"] == "pass" for v in verdicts) else "fail"
    return {"format": REPORT_FORMAT, "result": result, "steps": verdicts}


def canonical(body: dict) -> bytes:
    """Levain's canonical JSON for these value types (text, integers, lists, objects), written by hand: ``-S``
    leaves levain unimportable."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def main(argv: list) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: output_compare.py <input.json> <run_dir>\n")
        return 2
    try:
        body = report(argv[1], argv[2])
    except Infrastructure as exc:
        sys.stderr.write(f"output_compare: {exc}\n")
        return 2
    sys.stdout.buffer.write(canonical(body))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
