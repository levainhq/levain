"""levain.cockpit.session_context — how much context a Claude Code or Codex session holds, read from its own transcript.

A DISPLAY, never an authority: nothing here decides anything. Reads are bounded, never follow a symlink, never write.

One reusable token reader: :func:`last_context` (the meter) and :func:`iter_usage` (spend totals, for a later per-session
budget). Both read the transcript the agent already writes, so the session needs nothing installed to be measured.

Claude Code. Every assistant record carries ``message.usage``; the live context is
``input_tokens + cache_read_input_tokens + cache_creation_input_tokens`` of the LAST one (the prompt that call sent).
No window is derived from the model name: the transcript's model string reads the same for a 1M and a 200K session, so
a name-to-window table would be a guess dressed as a measurement. The model is reported verbatim and the only wall shown
is a MEASURED one, an auto compaction boundary's ``compactMetadata.preTokens``.

Codex. The newest ``token_count`` event's ``info.last_token_usage.input_tokens`` is the last request's whole prompt:
Codex counts cached input INSIDE ``input_tokens`` (``cached_input_tokens`` is a subset of it, and
``total_tokens == input_tokens + output_tokens`` on the same record), so adding the cached field would double-count.
``info.model_context_window`` is Codex's own number, so "of N" is allowed there.

The meter shows the main transcript's last record only: a subagent's transcript is a separate context and does not add to
the session's live context. Spend is different (every call is paid for), hence ``include_subagents`` on the iterator.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

__all__ = ["CONTEXT_WARN_TOKENS", "Context", "find_transcript", "last_context", "iter_usage", "context_chip"]

# The operator's rotation line for a lane, not any model's window: past it a session is dearer per turn than it is worth.
CONTEXT_WARN_TOKENS = 200_000

_START = 1 << 20            # the first window read from the end of the file
_CAP = 8 << 20              # the widest window; a usage record further back than this is "not read"
_SPEND_CAP = 256 << 20      # a file larger than this is not read for spend
_CACHE_MAX = 256
_cache: dict[tuple[str, str], tuple[int, int, Any]] = {}


class Context(NamedTuple):
    tokens: int
    window: int | None       # Codex's own window; None for Claude Code (not knowable from the transcript)
    model: str | None
    wall: int | None         # a measured auto-compaction trigger, Claude Code only


def _open_regular(path: Path | str) -> tuple[int, os.stat_result] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None
    return fd, st


def _tail_lines(fd: int, size: int, width: int) -> list[bytes]:
    """The whole lines in the last ``width`` bytes (a split first line and an unterminated last line dropped)."""
    start = max(0, size - width)
    raw = os.pread(fd, size - start + (1 if start else 0), start - 1 if start else 0)
    if start:
        nl = raw.find(b"\n")
        raw = b"" if nl < 0 else raw[nl + 1:]
    end = raw.rfind(b"\n")
    return raw[:end + 1].split(b"\n") if end >= 0 else []


def _json(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        d = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _num(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _claude_prompt(usage: Mapping[str, Any]) -> int:
    return _num(usage.get("input_tokens")) + _num(usage.get("cache_read_input_tokens")) \
        + _num(usage.get("cache_creation_input_tokens"))


def _scan_claude(lines: list[bytes]) -> Context | None:
    last_msg: dict[str, Any] | None = None
    wall: int | None = None
    for line in reversed(lines):
        d = _json(line)
        if d is None:
            continue
        md = d.get("compactMetadata")
        if wall is None and isinstance(md, dict) and md.get("trigger") == "auto" and _num(md.get("preTokens")):
            wall = md["preTokens"]
        m = d.get("message")
        if last_msg is None and d.get("type") == "assistant" and isinstance(m, dict) \
                and isinstance(m.get("usage"), dict) and not d.get("isSidechain"):
            last_msg = m
        if last_msg is not None and wall is not None:
            break
    if last_msg is None:
        return None
    # one message can be written as several records; the usage of the same id is the max, and the last record is that
    model = last_msg.get("model") if isinstance(last_msg.get("model"), str) else None
    return Context(_claude_prompt(last_msg["usage"]), None, model, wall)


def _scan_codex(lines: list[bytes]) -> Context | None:
    for line in reversed(lines):
        if b'"token_count"' not in line:
            continue
        d = _json(line)
        p = d.get("payload") if d else None
        info = p.get("info") if isinstance(p, dict) and p.get("type") == "token_count" else None
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if isinstance(last, dict) and isinstance(last.get("input_tokens"), int):
            w = info.get("model_context_window")
            return Context(_num(last["input_tokens"]), w if isinstance(w, int) and w > 0 else None, None, None)
    return None


def last_context(path: Path | str, agent: str = "claude_code") -> tuple[Context | None, str]:
    """(context, "") from the transcript's newest usage record, or (None, why). Cached per (path, size, mtime)."""
    opened = _open_regular(path)
    if opened is None:
        return None, "transcript not found"
    fd, st = opened
    try:
        key = (str(path), agent)
        hit = _cache.get(key)
        if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
            return hit[2]
        scan = _scan_codex if agent == "codex" else _scan_claude
        width, out = _START, None
        while out is None:
            out = scan(_tail_lines(fd, st.st_size, width))
            if width >= min(_CAP, st.st_size):
                break
            width *= 2
        res = (out, "") if out else (None, "no usage record in the transcript's tail")
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (st.st_size, st.st_mtime_ns, res)
        return res
    except OSError:
        return None, "transcript unreadable"
    finally:
        os.close(fd)


def _claude_dir(env: Mapping[str, str]) -> Path:
    cfg = env.get("CLAUDE_CONFIG_DIR", "")
    return Path(cfg) if cfg and os.path.isabs(cfg) else Path.home() / ".claude"


def find_transcript(rec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[Path | None, str]:
    """The registry record's recorded transcript when the hook stored one, else the agent's default location under the
    cockpit's own environment (a relocated config directory is invisible without the recorded path)."""
    env = os.environ if env is None else env
    t = rec.get("transcript")
    if isinstance(t, str) and os.path.isabs(t) and t.endswith(".jsonl"):
        return Path(t), ""
    sid, cwd = rec.get("session_id"), rec.get("cwd")
    if not isinstance(sid, str) or not sid:
        return None, "no session id"
    if rec.get("agent") == "codex":
        cfg = env.get("CODEX_HOME", "")
        home = Path(cfg) if cfg and os.path.isabs(cfg) else Path.home() / ".codex"
        try:
            found = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{sid}.jsonl"))
        except OSError:
            return None, "rollout not found"
        return (found[-1], "") if found else (None, "rollout not found")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None, "no working directory"
    # Claude Code names a project directory by turning every non-alphanumeric character of the cwd into "-"
    return _claude_dir(env) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl", ""


def iter_usage(path: Path | str, include_subagents: bool = False) -> Iterator[dict[str, Any]]:
    """Per-message usage of a Claude Code transcript (and, optionally, its ``<sid>/subagents/*.jsonl``), one dict per
    message id: a message written as several records yields once, with the MAX of each usage field."""
    p = Path(path)
    files = [p]
    if include_subagents:
        sub = p.with_suffix("") / "subagents"
        try:
            files += sorted(sub.glob("*.jsonl"))
        except OSError:
            pass
    best: dict[str, dict[str, Any]] = {}
    for f in files:
        opened = _open_regular(f)
        if opened is None:
            continue
        fd, st = opened
        try:
            if st.st_size > _SPEND_CAP:
                continue
            with os.fdopen(os.dup(fd), "rb") as fh:
                for line in fh:
                    d = _json(line)
                    m = d.get("message") if d else None
                    if not (d and d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict)):
                        continue
                    u = m["usage"]
                    mid = m.get("id") if isinstance(m.get("id"), str) else f"{f.name}:{d.get('uuid')}"
                    row = {"id": mid, "model": m.get("model"), "file": f.name,
                           "input": _num(u.get("input_tokens")), "cache_read": _num(u.get("cache_read_input_tokens")),
                           "cache_creation": _num(u.get("cache_creation_input_tokens")),
                           "output": _num(u.get("output_tokens"))}
                    old = best.get(mid)
                    if old:
                        for k in ("input", "cache_read", "cache_creation", "output"):
                            row[k] = max(row[k], old[k])
                    best[mid] = row
        except OSError:
            continue
        finally:
            os.close(fd)
    yield from best.values()


def _k(n: int) -> str:
    return f"{round(n / 1000)}K" if n >= 1000 else str(n)


def context_chip(rec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[str, Context | None]:
    """The Sessions chip text and the reading behind it."""
    path, why = find_transcript(rec, env)
    ctx = None
    if path is not None:
        ctx, why = last_context(path, "codex" if rec.get("agent") == "codex" else "claude_code")
    if ctx is None:
        return f"context not read: {why}", None
    text = f"context {_k(ctx.tokens)}" + (f" of {_k(ctx.window)}" if ctx.window else "")
    if ctx.tokens >= CONTEXT_WARN_TOKENS:
        text += f", past the {_k(CONTEXT_WARN_TOKENS)} warn line"
    return text, ctx
