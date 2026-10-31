"""levain.cockpit.session_context — how much context a Claude Code or Codex session holds, read from its own transcript.

A DISPLAY, never an authority: nothing here decides anything. Reads are bounded, never follow a symlink at the file itself,
never write.

One reusable token reader: :func:`last_context` (the meter), :func:`iter_usage` (spend totals, read whole) and
:func:`spend_step` (the same totals read incrementally, for Q13's per-session limits in :mod:`levain.consent.limits`). Both read the transcript the agent already writes, so the session needs nothing installed to be measured.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

__all__ = ["CONTEXT_WARN_TOKENS", "Context", "find_transcript", "last_context", "iter_usage", "spend_step",
           "context_chip", "tokens_since", "tokens_since_detail", "recent_rate"]

# The operator's rotation line for a lane, not any model's window: past it a session is dearer per turn than it is worth.
CONTEXT_WARN_TOKENS = 200_000

_START = 1 << 20            # the first window read from the end of the file
_CAP = 8 << 20              # the widest window; a usage record further back than this is "not read"
_SPEND_CAP = 256 << 20      # a file larger than this is not read for spend
_CACHE_MAX = 256
_LINE_CAP = 4 << 20         # a spend-scan line longer than this is skipped, never held whole
_SUBAGENT_FILES = 64        # subagent transcripts iter_usage reads for spend; more are counted as skipped
_MODEL_CAP = 64
_DAY_DIRS = 30              # date directories walked for a Codex rollout, newest first
_cache: dict[tuple[str, str], tuple[tuple[int, ...], Any]] = {}
_rollouts: dict[tuple[str, str], tuple[Path | None, float, int]] = {}    # (home, sid) -> (rollout or None, when, misses)
_MISS_S = 10.0              # how long the first "no rollout yet" is believed; it doubles per repeat
_MISS_MAX_S = 300.0


class Context(NamedTuple):
    tokens: int
    window: int | None       # Codex's own window; None for Claude Code (not knowable from the transcript)
    model: str | None
    compacted: bool = False  # the session was compacted after its newest usable reading; ``tokens`` is then 0


def _open_regular(path: Path | str) -> tuple[int, os.stat_result] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):
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
            model = m["model"][:_MODEL_CAP] if isinstance(m.get("model"), str) else None
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
        n = _num(last.get("input_tokens")) if isinstance(last, dict) else 0
        if n > 0:
            if compacted:
                break
            w = _num(info.get("model_context_window"))
            return Context(n, w or None, None)
    return Context(0, None, None, True) if compacted else None


def last_context(path: Path | str, agent: str = "claude_code") -> tuple[Context | None, str]:
    """(context, "") from the transcript's newest usage record, or (None, why). Cached per file identity and version."""
    opened = _open_regular(path)
    if opened is None:
        return None, "transcript not found"
    fd, st = opened
    try:
        key = (str(path), agent)
        ident = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        hit = _cache.get(key)
        if hit and hit[0] == ident:
            return hit[1]
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
        _cache[key] = (ident, res)
        return res
    except OSError:
        return None, "transcript unreadable"
    finally:
        os.close(fd)


def _claude_dir(env: Mapping[str, str]) -> Path:
    cfg = env.get("CLAUDE_CONFIG_DIR", "")
    return Path(cfg) if cfg and os.path.isabs(cfg) else Path.home() / ".claude"


def _plain_file(p: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def _find_rollout(home: Path, sid: str) -> tuple[Path | None, str]:
    """The Codex rollout for a session id: a remembered path (re-checked on use), else the date directories newest
    first, since a session being measured is nearly always a recent one."""
    if "/" in sid or "\\" in sid or "\0" in sid:
        return None, "rollout not found"
    key = (str(home), sid)
    hit = _rollouts.get(key)
    misses = 0
    if hit:
        path, when, misses = hit
        if path is not None:
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    return path, ""
            except OSError:
                pass
            _rollouts.pop(key, None)
            misses = 0
        elif time.monotonic() - when < min(_MISS_S * 2 ** min(misses - 1, 6), _MISS_MAX_S):
            return None, "rollout not found"
    found: Path | None = None
    pat = f"rollout-*-{glob.escape(sid)}.jsonl"
    try:
        root = home / "sessions"
        walked = 0
        for y in sorted(root.glob("[0-9]*"), reverse=True):
            for mo in sorted(y.glob("[0-9]*"), reverse=True):
                for dd in sorted(mo.glob("[0-9]*"), reverse=True):
                    if walked >= _DAY_DIRS:
                        break
                    walked += 1
                    hits = [h for h in sorted(dd.glob(pat)) if _plain_file(h)]
                    if hits:
                        found = hits[-1]
                        break
                if found or walked >= _DAY_DIRS:
                    break
            if found or walked >= _DAY_DIRS:
                break
    except OSError:
        pass
    if len(_rollouts) >= _CACHE_MAX:
        _rollouts.clear()
    _rollouts[key] = (found, time.monotonic(), 0 if found else misses + 1)
    return (found, "") if found else (None, "rollout not found")


def find_transcript(rec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[Path | None, str]:
    """The registry record's recorded transcript when the hook stored one, else the agent's default location under the
    cockpit's own environment (a relocated config directory is invisible without the recorded path)."""
    env = os.environ if env is None else env
    t = rec.get("transcript")
    if isinstance(t, str) and len(t) <= 1024 and "\0" not in t and os.path.isabs(t) and t.endswith(".jsonl"):
        return Path(t), ""
    sid, cwd = rec.get("session_id"), rec.get("cwd")
    if not isinstance(sid, str) or not sid or "\0" in sid:
        return None, "no session id"
    if rec.get("agent") == "codex":
        cfg = env.get("CODEX_HOME", "")
        home = Path(cfg) if cfg and os.path.isabs(cfg) else Path.home() / ".codex"
        return _find_rollout(home, sid)
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None, "no working directory"
    # Claude Code names a project directory by turning every non-alphanumeric character of the cwd into "-"
    return _claude_dir(env) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl", ""


def iter_usage(path: Path | str, include_subagents: bool = False,
               stats: dict[str, int] | None = None) -> Iterator[dict[str, Any]]:
    """Per-message usage of a Claude Code transcript (and, optionally, its ``<sid>/subagents/*.jsonl``), one dict per
    message id: a message written as several records yields once, with the MAX of each usage field. Each file is read
    up to the size it had when opened, and all files together up to ``_SPEND_CAP`` bytes; a file that is oversized,
    unreadable or past a budget is left out, and ``stats["skipped_files"]`` (when given) says how many."""
    p = Path(path)
    files = [p]
    skipped = 0
    if include_subagents:
        sub = p.with_suffix("") / "subagents"
        try:
            found = sorted(sub.glob("*.jsonl"))
        except (OSError, ValueError):
            found = []
        files += found[:_SUBAGENT_FILES]
        skipped += max(0, len(found) - _SUBAGENT_FILES)
    best: dict[str, dict[str, Any]] = {}
    budget = _SPEND_CAP
    for f in files:
        opened = _open_regular(f)
        if opened is None:
            skipped += f is not p
            continue
        fd, st = opened
        try:
            if st.st_size > budget:
                skipped += 1
                continue
            budget -= st.st_size
            with os.fdopen(os.dup(fd), "rb") as fh:
                offset = 0
                while offset < st.st_size:
                    line = fh.readline(min(_LINE_CAP + 1, st.st_size - offset))
                    if not line:
                        break
                    at, offset = offset, offset + len(line)
                    if len(line) > _LINE_CAP and not line.endswith(b"\n"):
                        while offset < st.st_size:     # an over-long line is skipped to its end, never held whole
                            rest = fh.readline(min(_LINE_CAP, st.st_size - offset))
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
            skipped += 1
            continue
        finally:
            os.close(fd)
    if stats is not None:
        stats["skipped_files"] = skipped
    yield from best.values()


_STEP_BYTES = 32 << 20      # bytes read per spend_step call, all files together; the rest is read by later calls
_RECENT_IDS = 128           # message ids whose counted usage is remembered, so a message written as several records adds once
_SPEND_FILES = 256          # subagent transcripts followed for spend; more are counted in state["skipped"]


def _spend_lines(fd: int, size: int, offset: int, budget: int) -> tuple[list[bytes], int]:
    """The whole lines from ``offset`` within ``budget`` bytes, and the offset after the last one. An unterminated
    tail is left for a later call; a single line longer than ``_LINE_CAP`` is skipped to its end, never held whole."""
    want = min(size - offset, budget)
    if want <= 0:
        return [], offset
    raw = os.pread(fd, want, offset)
    end = raw.rfind(b"\n")
    if end >= 0:
        return raw[:end + 1].split(b"\n"), offset + end + 1
    if len(raw) < _LINE_CAP:
        return [], offset                   # an unfinished line: read again once it ends
    at = offset + len(raw)
    while at < size:                        # an over-long line: find its end without keeping it
        chunk = os.pread(fd, min(1 << 20, size - at), at)
        if not chunk:
            break
        nl = chunk.find(b"\n")
        if nl >= 0:
            return [], at + nl + 1
        at += len(chunk)
    return [], offset


def spend_step(path: Path | str, agent: str = "claude_code", state: dict[str, Any] | None = None, *,
               step_bytes: int = _STEP_BYTES) -> dict[str, Any]:
    """Spend so far, read INCREMENTALLY: the caller keeps ``state`` (JSON-safe) between calls and each call reads only
    what was appended since, at most ``step_bytes`` in all. Returns the updated state, whose ``fresh`` and
    ``cache_read`` are the running totals.

    Claude Code: the transcript and its ``<sid>/subagents/*.jsonl``; per message id, each usage field is counted at its
    MAX across the records that carry it (as :func:`iter_usage`); fresh = input + output + cache write. Codex: the
    rollout's newest ``token_count`` ``info.total_token_usage`` (cumulative); cached input is inside ``input_tokens``,
    so fresh = input - cached + output and cache_read = cached. A file replaced or shortened since the last call is
    read again from its start, its earlier usage kept (it may count twice: an over-count, never an under-count)."""
    st_: dict[str, Any] = dict(state) if isinstance(state, dict) else {}
    files = dict(st_.get("files") or {})
    recent = dict(st_.get("recent") or {})
    fresh, cache_read = _num(st_.get("fresh")), _num(st_.get("cache_read"))
    p = Path(path)
    todo = [("", p)]
    if agent != "codex":
        try:
            found = sorted((p.with_suffix("") / "subagents").glob("*.jsonl"))
        except (OSError, ValueError):
            found = []
        todo += [(f.name, f) for f in found[:_SPEND_FILES]]
        st_["skipped"] = max(0, len(found) - _SPEND_FILES)
    budget = max(0, step_bytes)
    best = list(st_.get("codex") or [0, 0, 0])
    behind = False
    for key, f in todo:
        if budget <= 0:
            behind = True
            break
        opened = _open_regular(f)
        if opened is None:
            continue
        fd, st = opened
        try:
            dev, ino, offset = (files.get(key) or [None, None, 0])[:3]
            if (dev, ino) != (st.st_dev, st.st_ino) or not isinstance(offset, int) or offset > st.st_size:
                offset = 0
            lines, new = _spend_lines(fd, st.st_size, offset, budget)
            behind = behind or st.st_size - offset > budget
            budget -= new - offset
            files[key] = [st.st_dev, st.st_ino, new]
        except OSError:
            continue
        finally:
            os.close(fd)
        for line in lines:
            if agent == "codex":
                if b'"token_count"' not in line:
                    continue
                d = _json(line)
                pl = d.get("payload") if d else None
                info = pl.get("info") if isinstance(pl, dict) and pl.get("type") == "token_count" else None
                tot = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(tot, dict):
                    got = [_num(tot.get("input_tokens")), _num(tot.get("cached_input_tokens")),
                           _num(tot.get("output_tokens"))]
                    best = [max(a, b) for a, b in zip(best, got)]
                continue
            if b'"usage"' not in line:
                continue
            d = _json(line)
            m = d.get("message") if d else None
            if not (d and d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict)):
                continue
            u = m["usage"]
            uuid = d.get("uuid")
            mid = m["id"] if isinstance(m.get("id"), str) else f"{key}:{uuid}" if isinstance(uuid, str) else None
            now = [_num(u.get("input_tokens")), _num(u.get("cache_read_input_tokens")),
                   _num(u.get("cache_creation_input_tokens")), _num(u.get("output_tokens"))]
            if mid is None:                 # nothing to tell its records apart: each counts once
                fresh += now[0] + now[2] + now[3]
                cache_read += now[1]
                continue
            old = recent.pop(mid, None) or [0, 0, 0, 0]
            top = [max(a, b) for a, b in zip(old, now)]
            fresh += (top[0] - old[0]) + (top[2] - old[2]) + (top[3] - old[3])
            cache_read += top[1] - old[1]
            recent[mid] = top
            while len(recent) > _RECENT_IDS:
                recent.pop(next(iter(recent)))
    if agent == "codex":
        fresh, cache_read = max(best[0] - best[1], 0) + best[2], best[1]
        st_["codex"] = best
    st_.update(files=files, recent=recent, fresh=fresh, cache_read=cache_read,
               behind=behind)
    return st_


def _stamp(v: Any) -> datetime | None:
    """A transcript record's ISO ``timestamp`` as an aware UTC time, else None."""
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


_day_cache: dict[tuple[str, str, int, int, str], tuple[int | None, str, int]] = {}


def tokens_since(path: Path | str, agent: str, since: datetime) -> tuple[int | None, str]:
    """:func:`tokens_since_detail` without the count of subagent transcripts it could not read."""
    got, why, _skipped = tokens_since_detail(path, agent, since)
    return got, why


def tokens_since_detail(path: Path | str, agent: str, since: datetime) -> tuple[int | None, str, int]:
    """``(tokens, why, skipped)``: ``skipped`` is how many subagent transcripts written since ``since`` were left out
    (the byte budget ran out, or one would not read), so the figure is then a floor. Fresh tokens a transcript spent at or after ``since`` (an aware time), by each record's own ``timestamp``:
    ``(tokens, "")``, or ``(None, why)`` when it cannot be told (unreadable, oversized, or a usage record with no stamp).
    The unit is the one :func:`spend_step` counts: Claude Code input + output + cache write (each message id at the MAX
    across its records, subagent transcripts included); Codex input - cached input + output (its cumulative
    ``total_token_usage`` now, less its value at the last record before ``since``). A transcript last written before
    ``since`` spent nothing since, and is not opened."""
    p = Path(path)
    opened = _open_regular(p)
    if opened is None:
        return None, "unreadable", 0
    fd, st = opened
    os.close(fd)
    key = (str(p), agent, st.st_mtime_ns, st.st_size, since.isoformat())
    if key in _day_cache:
        return _day_cache[key]
    if st.st_mtime < since.timestamp():
        got: tuple[int | None, str, int] = (0, "", 0)
    else:
        try:
            got = (*_codex_since(p, since), 0) if agent == "codex" else _claude_since(p, since)
        except OSError:
            got = (None, "unreadable", 0)
    if len(_day_cache) >= _CACHE_MAX:
        _day_cache.pop(next(iter(_day_cache)))
    _day_cache[key] = got
    return got


def _lines_of(f: Path, budget: int) -> Iterator[bytes]:
    """The lines of one regular file, up to the size it had when opened; an over-long line is skipped."""
    opened = _open_regular(f)
    if opened is None:
        raise OSError("unreadable")
    fd, st = opened
    try:
        if st.st_size > budget:
            raise OSError("oversized")
        with os.fdopen(os.dup(fd), "rb") as fh:
            seen = 0
            while seen < st.st_size:
                line = fh.readline(min(_LINE_CAP + 1, st.st_size - seen))
                if not line:
                    break
                seen += len(line)
                if len(line) > _LINE_CAP and not line.endswith(b"\n"):
                    while seen < st.st_size:
                        rest = fh.readline(min(_LINE_CAP, st.st_size - seen))
                        seen += len(rest)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                yield line
    finally:
        os.close(fd)


def _claude_since(p: Path, since: datetime) -> tuple[int | None, str, int]:
    """Tokens at or after ``since`` over the transcript and its subagent transcripts, and how many of those could not
    be read. Records are appended, so a subagent file last written before ``since`` holds nothing at or after it and is
    not opened; every one written since is read, newest first, until the byte budget runs out, and the rest are
    counted in the third value, never dropped silently."""
    files: list[Path] = []
    try:
        fresh: list[tuple[float, str, Path]] = []
        for f in (p.with_suffix("") / "subagents").glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt >= since.timestamp():
                fresh.append((mt, f.name, f))
        files = [f for _, _, f in sorted(fresh, reverse=True)]
    except (OSError, ValueError):
        pass
    files.insert(0, p)
    best: dict[str, list[Any]] = {}       # message id -> [first stamp, input, cache write, output]
    budget = _SPEND_CAP
    skipped = 0
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            if f is p:
                return None, "unreadable", 0
            skipped += 1
            continue
        if size > budget:
            if f is p:
                return None, "oversized", 0
            skipped += 1
            continue
        budget -= size
        try:
            for line in _lines_of(f, size + (1 << 20)):
                if b'"usage"' not in line:
                    continue
                d = _json(line)
                m = d.get("message") if d else None
                if not (d and d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict)):
                    continue
                at = _stamp(d.get("timestamp"))
                if at is None:
                    return None, "a usage record has no timestamp", 0
                u = m["usage"]
                mid = m["id"] if isinstance(m.get("id"), str) else f"{f.name}:{d.get('uuid') or len(best)}"
                row = [at, _num(u.get("input_tokens")), _num(u.get("cache_creation_input_tokens")), _num(u.get("output_tokens"))]
                old = best.get(mid)
                if old:
                    row = [min(old[0], at)] + [max(a, b) for a, b in zip(old[1:], row[1:])]
                best[mid] = row
        except OSError as exc:
            if f is p:
                return None, str(exc), 0
            skipped += 1                 # a subagent file that cannot be read is left out, and counted
    return sum(r[1] + r[2] + r[3] for r in best.values() if r[0] >= since), "", skipped


def _codex_since(p: Path, since: datetime) -> tuple[int | None, str]:
    before = latest = 0
    stamped = False
    for line in _lines_of(p, _SPEND_CAP):
        if b'"token_count"' not in line:
            continue
        d = _json(line)
        pl = d.get("payload") if d else None
        info = pl.get("info") if isinstance(pl, dict) and pl.get("type") == "token_count" else None
        tot = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(tot, dict):
            continue
        at = _stamp(d.get("timestamp"))
        if at is None:
            return None, "a token record has no timestamp"
        stamped = True
        v = max(_num(tot.get("input_tokens")) - _num(tot.get("cached_input_tokens")), 0) + _num(tot.get("output_tokens"))
        latest = max(latest, v)
        if at < since:
            before = max(before, v)
    return (max(latest - before, 0), "") if stamped else (0, "")


def recent_rate(path: Path | str, agent: str, now: datetime, window_s: float) -> float | None:
    """Messages per minute a transcript wrote in the ``window_s`` seconds before ``now``, from its records' own
    ``timestamp`` (the tail only: the window is recent). Claude Code counts user and assistant records, Codex its
    response items. None when the transcript cannot be read; a readable one with no message in the window is 0.0."""
    opened = _open_regular(path)
    if opened is None:
        return None
    fd, st = opened
    try:
        lines = _tail_lines(fd, st.st_size, 512 * 1024)
    except OSError:
        return None
    finally:
        os.close(fd)
    kinds = (b'"response_item"',) if agent == "codex" else (b'"user"', b'"assistant"')
    n = 0
    for line in lines:
        if not any(k in line for k in kinds):
            continue
        d = _json(line)
        if not d or d.get("type") not in (("response_item",) if agent == "codex" else ("user", "assistant")):
            continue
        at = _stamp(d.get("timestamp"))
        if at is not None and 0 <= (now - at).total_seconds() <= window_s:
            n += 1
    return round(n / (window_s / 60), 2)


def _k(n: int) -> str:
    return f"{n // 1000}K" if n >= 1000 else str(n)


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
