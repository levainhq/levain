"""levain.cockpit.session_context — how much context a Claude Code or Codex session holds, read from its own transcript.

A DISPLAY, never an authority: nothing here decides anything. Reads are bounded, never follow a symlink at the file itself,
never write.

One reusable token reader: :func:`last_context` (the meter) and :func:`iter_usage` (spend totals, for a later per-session
budget). Both read the transcript the agent already writes, so the session needs nothing installed to be measured.

Claude Code. Every assistant record carries ``message.usage``; the live context is
``input_tokens + cache_read_input_tokens + cache_creation_input_tokens`` of the LAST one (the prompt that call sent).
No window is derived from the model name: the transcript's model string reads the same for a 1M and a 200K session, so
a name-to-window table would be a guess dressed as a measurement. The model is reported verbatim and no wall is shown.
A record that is not a real prompt (the ``<synthetic>`` model, an API-error record, a usage summing to 0) is skipped, and
a ``compact_boundary`` newer than the newest usable record reads "just compacted" with no number: until the next reply
the newest usable record is the pre-compaction prompt, and the boundary's own ``postTokens`` is not the next reading.

Codex. The newest ``token_count`` event's ``info.last_token_usage.input_tokens`` is the last request's whole prompt:
Codex counts cached input INSIDE ``input_tokens`` (``cached_input_tokens`` is a subset of it, and
``total_tokens == input_tokens + output_tokens`` on the same record), so adding the cached field would double-count.
``info.model_context_window`` is Codex's own number, so "of N" is allowed there. A ``token_count`` right after a
compaction reports 0 input tokens; it is skipped, and a ``compacted`` record newer than the newest nonzero reading reads
"just compacted".

The meter shows the main transcript's last record only: a subagent's transcript is a separate context and does not add to
the session's live context. Spend is different (every call is paid for), hence ``include_subagents`` on the iterator.
"""
from __future__ import annotations

import glob
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

__all__ = ["CONTEXT_WARN_TOKENS", "Context", "find_transcript", "last_context", "iter_usage", "context_chip"]

# The operator's rotation line for a lane, not any model's window: past it a session is dearer per turn than it is worth.
CONTEXT_WARN_TOKENS = 200_000

_START = 1 << 20            # the first window read from the end of the file
_CAP = 8 << 20              # the widest window; a usage record further back than this is "not read"
_SPEND_CAP = 256 << 20      # a file larger than this is not read for spend
_CACHE_MAX = 256
_LINE_CAP = 4 << 20         # a spend-scan line longer than this is skipped, never held whole
_cache: dict[tuple[str, str], tuple[int, int, Any]] = {}
_rollouts: dict[tuple[str, str], tuple[Path | None, float]] = {}    # (codex home, sid) -> (rollout or None, when looked)
_MISS_S = 10.0              # how long "no rollout yet" is believed


class Context(NamedTuple):
    tokens: int
    window: int | None       # Codex's own window; None for Claude Code (not knowable from the transcript)
    model: str | None
    compacted: bool = False  # the session was compacted after its newest usable reading; ``tokens`` is then 0


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


def _usable_claude(d: Mapping[str, Any]) -> dict[str, Any] | None:
    """The message of a real, main-thread assistant reply, else None: ``<synthetic>`` and API-error records carry a
    zeroed usage that is not a prompt."""
    m = d.get("message")
    if d.get("type") != "assistant" or d.get("isSidechain") or d.get("isApiErrorMessage") or not isinstance(m, dict):
        return None
    if m.get("model") == "<synthetic>" or not isinstance(m.get("usage"), dict) or _claude_prompt(m["usage"]) <= 0:
        return None
    return m


def _scan_claude(lines: list[bytes]) -> Context | None:
    compacted = False
    for line in reversed(lines):
        if b'"usage"' not in line and b"compact_boundary" not in line:
            continue
        d = _json(line)
        if d is None:
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            compacted = True
            continue
        m = _usable_claude(d)
        if m is not None:
            if compacted:
                break
            model = m.get("model") if isinstance(m.get("model"), str) else None
            return Context(_claude_prompt(m["usage"]), None, model)
    return Context(0, None, None, True) if compacted else None


def _scan_codex(lines: list[bytes]) -> Context | None:
    compacted = False
    for line in reversed(lines):
        if b'"token_count"' not in line and b'"compacted"' not in line:
            continue
        d = _json(line)
        if d is None:
            continue
        if d.get("type") == "compacted":
            compacted = True
            continue
        p = d.get("payload")
        info = p.get("info") if isinstance(p, dict) and p.get("type") == "token_count" else None
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if isinstance(last, dict) and isinstance(last.get("input_tokens"), int) and last["input_tokens"] > 0:
            if compacted:
                break
            w = info.get("model_context_window")
            return Context(last["input_tokens"], w if isinstance(w, int) and w > 0 else None, None)
    return Context(0, None, None, True) if compacted else None


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


def _find_rollout(home: Path, sid: str) -> tuple[Path | None, str]:
    """The Codex rollout for a session id: a remembered path (re-checked on use), else the date directories newest
    first, since a session being measured is nearly always a recent one."""
    if "/" in sid or "\\" in sid:
        return None, "rollout not found"
    key = (str(home), sid)
    hit = _rollouts.get(key)
    if hit:
        path, when = hit
        if path is not None:
            try:
                if stat.S_ISREG(os.stat(path).st_mode):
                    return path, ""
            except OSError:
                pass
        elif time.monotonic() - when < _MISS_S:
            return None, "rollout not found"
    found: Path | None = None
    pat = f"rollout-*-{glob.escape(sid)}.jsonl"
    try:
        root = home / "sessions"
        for y in sorted(root.glob("[0-9]*"), reverse=True):
            for mo in sorted(y.glob("[0-9]*"), reverse=True):
                for dd in sorted(mo.glob("[0-9]*"), reverse=True):
                    hits = sorted(dd.glob(pat))
                    if hits:
                        found = hits[-1]
                        break
                if found:
                    break
            if found:
                break
    except OSError:
        pass
    if len(_rollouts) >= _CACHE_MAX:
        _rollouts.clear()
    _rollouts[key] = (found, time.monotonic())
    return (found, "") if found else (None, "rollout not found")


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
        return _find_rollout(home, sid)
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
                offset = 0
                while True:
                    line = fh.readline(_LINE_CAP + 1)
                    if not line:
                        break
                    at, offset = offset, offset + len(line)
                    if len(line) > _LINE_CAP and not line.endswith(b"\n"):
                        while True:       # an over-long line is skipped to its end, never held whole
                            rest = fh.readline(_LINE_CAP)
                            offset += len(rest)
                            if not rest or rest.endswith(b"\n"):
                                break
                        continue
                    if b'"usage"' not in line:
                        continue
                    d = _json(line)
                    m = d.get("message") if d else None
                    if not (d and d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict)):
                        continue
                    u = m["usage"]
                    mid = m.get("id") if isinstance(m.get("id"), str) else f"{f.name}:{d.get('uuid') or at}"
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
    if ctx.compacted:
        return "context: just compacted", ctx
    text = f"context {_k(ctx.tokens)}" + (f" of {_k(ctx.window)}" if ctx.window else "")
    if ctx.tokens >= CONTEXT_WARN_TOKENS:
        text += f", past the {_k(CONTEXT_WARN_TOKENS)} warn line"
    return text, ctx
