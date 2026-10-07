"""The in-force view of a ledger, derived from its lines every time (never stored as truth).

In force = every anchorable entry that no honoured link supersedes. An ``ack`` never supersedes; a
``retire`` only supersedes. Globs are repo-relative: ``billing.py`` names the file at the repo root,
``**/billing.py`` names it anywhere, ``src/`` the whole tree. Matching ignores case and Unicode
normalisation form, so ``SRC/Billing.py`` on a case-insensitive disk cannot slip past a rule.
"""
from __future__ import annotations

import functools
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import entry as E

ANCHORABLE = ("decision", "constraint", "finding", "question", "tension")


def _norm(s: str, fold: bool) -> str:
    s = unicodedata.normalize("NFC", s)
    return s.casefold() if fold else s


def _segments(glob: str) -> tuple[str, ...]:
    g = glob.strip()
    while g.startswith("./"):
        g = g[2:]
    if g in ("", "."):
        return ("**",)
    if g.endswith("/"):
        g += "**"
    segs: list[str] = []
    for s in g.split("/"):
        if s == "**" and segs and segs[-1] == "**":
            continue  # "**/**" is "**"
        if s:
            segs.append(s)
    return tuple(segs) or ("**",)


def _seg_match(pat: str, text: str) -> bool:
    """One path segment: ``*`` any run, ``?`` one character, everything else literal. Linear DP, no regex,
    so no pattern a member records can make a hook spin (a hook that times out lets the edit through)."""
    prev = [True] + [False] * len(text)
    for pc in pat:
        cur = [False] * (len(text) + 1)
        if pc == "*":
            cur[0] = prev[0]
            for j in range(1, len(text) + 1):
                cur[j] = cur[j - 1] or prev[j]
        else:
            for j in range(1, len(text) + 1):
                cur[j] = prev[j - 1] and (pc == "?" or pc == text[j - 1])
        prev = cur
    return prev[len(text)]


_MAX_INNER = 6  # segments like "**.py" or "a**b" expand to two alternatives each; bound the product


def _expand(segs: tuple[str, ...]) -> list[tuple[str, ...]]:
    """``**`` inside a segment ("src/**.py", "a**b") crosses directories, as it always has: it means the same
    as the segment with ``*`` in its place OR ``pre*/**/*post``. Expanded to whole-segment ``**`` patterns."""
    out: list[tuple[str, ...]] = [()]
    inner = 0
    for s in segs:
        if s != "**" and "**" in s:
            inner += 1
            if inner > _MAX_INNER:
                return []   # too many to expand: matches nothing (validate refuses such globs at write)
            pre, post = s.split("**", 1)
            post = post.replace("**", "*")
            alts = [(pre + "*" + post,), (pre + "*", "**", "*" + post)]
            out = [o + a for o in out for a in alts]
        else:
            out = [o + (s,) for o in out]
    return out


def matches(glob: str, relpath: str, *, fold: bool = True) -> bool:
    parts = tuple(p for p in _norm(relpath, fold).split("/") if p)
    return any(_match(pats, parts) for pats in _expand(_segments(_norm(glob, fold))))


@functools.lru_cache(maxsize=4096)
def _match(pats: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    # reach[j]: the pattern so far can consume exactly parts[:j]. O(len(pats) * len(parts)) segment checks.
    reach = [True] + [False] * len(parts)
    for p in pats:
        nxt = [False] * (len(parts) + 1)
        if p == "**":
            seen = False
            for j in range(len(parts) + 1):
                seen = seen or reach[j]
                nxt[j] = seen
        else:
            for j in range(1, len(parts) + 1):
                nxt[j] = reach[j - 1] and _seg_match(p, parts[j - 1])
        reach = nxt
    return reach[len(parts)]


@dataclass
class LedgerFile:
    rel: str                       # path under ledger/, e.g. "ana/3f2a9c1d0e4b5a6c.jsonl"
    raw: list[dict]                # every line that parsed as a JSON object, in order
    entries: list[dict]            # the lines that verified
    last_hash: str


def may_link(linker: dict, target: dict, owner: str | None) -> str | None:
    """None if ``linker`` may supersede or retire ``target``, else the reason it may not.

    The same rules the write path checks, enforced again here, at read: a line pushed by hand must not
    get further than one written through ``levain team record``.
    """
    if target.get("type") in ("ack", "retire"):
        return f"{target['id']} is an {target['type']}; it cannot be superseded"
    la, ta = linker.get("author", ""), target.get("author", "")
    if la != ta and not (owner is not None and la == owner):
        return f"only {ta} or the owner ({owner}) may supersede {target['id']}"
    if target.get("kind") == "ruling":
        if not (linker.get("words") or "").strip():
            return f"replacing or retiring the ruling {target['id']} needs the decider's words"
        if linker.get("type") != "retire" and linker.get("kind") != "ruling":
            return f"only a ruling or a retire may supersede the ruling {target['id']}"
    return None


@dataclass
class Ledger:
    entries: list[dict]            # every verified entry, sorted by (ts, id)
    file_problems: list[str]       # chain breaks, bad lines, misfiled or stranger-written entries
    files: list[LedgerFile] = field(default_factory=list)
    owner: str | None = None       # team.toml owner: the one author whose links cross authors
    # Paths under ledger/ levain never writes. Non-empty means the ledger is refused as a whole by every reader.
    tamper: list[str] = field(default_factory=list)

    @functools.cached_property
    def by_id(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.entries}

    @functools.cached_property
    def _links(self) -> tuple[set[str], list[str]]:
        honoured: set[str] = set()
        refused = Capped()
        for e in self.entries:
            if e.get("type") == "ack":
                continue
            for s in e.get("supersedes", []):
                target = self.by_id.get(s)
                if target is None:
                    refused.append(f"{e['id']} supersedes {s}, which is not in the ledger (ignored)")
                    continue
                why = may_link(e, target, self.owner)
                if why is None:
                    honoured.add(s)
                else:
                    refused.append(f"{e['id']} by {e.get('author')}: {why}; ignored, {s} stays in force")
        return honoured, refused.done("refused links")

    @property
    def superseded(self) -> set[str]:
        return self._links[0]

    @property
    def problems(self) -> list[str]:
        return [f"TAMPER: {t}" for t in self.tamper] + self.file_problems + self._links[1]

    @functools.cached_property
    def in_force(self) -> list[dict]:
        return [e for e in self.entries if e.get("type") in ANCHORABLE and e["id"] not in self.superseded]

    def acked(self, session: str, handle: str | None) -> set[str]:
        """Ruling ids this member acknowledged in this session. An ack counts only for its own author."""
        out: set[str] = set()
        for e in self.entries:
            if e.get("type") == "ack" and e.get("session") == session and e.get("author") == handle:
                out.update(e.get("refs", []))
        return out

    def applying_to(self, relpath: str, *, fold: bool = True) -> list[dict]:
        return [e for e in self.in_force if any(matches(g, relpath, fold=fold) for g in e.get("paths", []))]

    def retired_by_others(self, pack: str) -> dict[str, dict]:
        """rule_id -> the pack's last entry for it, where someone other than the pack retired or replaced it."""
        author = f"pack:{pack}"
        last: dict[str, dict] = {}
        for e in self.entries:
            if e.get("author") == author and e.get("rule_id") and e.get("type") != "retire":
                last[e["rule_id"]] = e
        out = {}
        for rid, e in last.items():
            if e["id"] in self.superseded and any(
                    e["id"] in o.get("supersedes", []) and o.get("author") != author for o in self.entries):
                out[rid] = e
        return out

    def pack_rules(self, pack: str) -> dict[str, dict]:
        """rule_id -> the in-force entry a pack seeded for it."""
        return {e["rule_id"]: e for e in self.in_force
                if str(e.get("pack", "")).split("@")[0] == pack and e.get("rule_id")}


MAX_PROBLEMS = 1000


class Capped(list):
    """A list of messages that keeps at most ``limit`` and only COUNTS the rest, so a hostile ledger can make many
    problems but never hold many in memory. ``done(noun)`` is the list with one "and N more" line."""

    def __init__(self, items=(), limit: int | None = None):
        super().__init__()
        self.limit = MAX_PROBLEMS if limit is None else limit
        self.more = 0
        self.extend(items)

    def append(self, item) -> None:
        if len(self) < self.limit:
            super().append(item)
        else:
            self.more += 1

    def extend(self, items) -> None:
        for item in items:
            self.append(item)

    def __iadd__(self, items):
        self.extend(items)
        return self

    def done(self, noun: str = "problems") -> list[str]:
        return list(self) + ([f"and {self.more} more {noun}"] if self.more else [])



def build(files: list[tuple[str, list[str]]], owner: str | None = None,
          problems: list[str] | None = None, *, tamper: list[str] | None = None) -> Ledger:
    """A ledger from (rel path under ledger/, lines) pairs. Callers split on "\\n" only:
    ``str.splitlines()`` also splits on U+2028/U+2029/U+0085, which JSON written with ensure_ascii=False
    carries unescaped inside a string."""
    entries: list[dict] = []
    problems = Capped(problems or [])
    out_files: list[LedgerFile] = []
    for rel, lines in sorted(files):
        got, probs, last = E.verify_lines(lines)
        owner_dir = rel.split("/", 1)[0]
        misfiled = [e for e in got if E.safe_handle(e.get("author", "")) != owner_dir]
        probs += [E._printable(f"{e['id']}: author {e.get('author')!r} is filed under {owner_dir}/ (not enforced)")
                  for e in misfiled]
        bad = {id(e) for e in misfiled}  # identity, not dict equality: a list scan here is quadratic in a hostile file
        got = [e for e in got if id(e) not in bad]
        raw = []
        for line in lines:
            try:
                obj = E.parse_line(line)
            except E.LineError:
                continue
            if isinstance(obj, dict):
                raw.append(obj)
        out_files.append(LedgerFile(rel, raw, got, last))
        problems += (f"{rel}: {p}" for p in probs)
        entries += got
    seen, uniq = set(), []
    for e in entries:
        if e["id"] in seen:
            problems.append(f"duplicate id {e['id']} (kept the first)")
            continue
        seen.add(e["id"])
        uniq.append(e)
    uniq.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")))
    # every reader shows, caches and prints these: kept bounded, the rest counted
    return Ledger(uniq, problems.done(), out_files, owner, Capped(tamper or []).done("reasons"))


def load_dir(ledger_dir: Path, owner: str | None = None) -> Ledger:
    """A ledger from files on disk (tests and tools). The hook and the CLI read git history instead."""
    files, problems = [], []
    if ledger_dir.is_dir():
        for f in sorted(ledger_dir.rglob("*.jsonl")):
            rel = f.relative_to(ledger_dir).as_posix()
            try:
                files.append((rel, f.read_text(encoding="utf-8").split("\n")))
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(f"{rel}: unreadable ({exc})")
    return build(files, owner, problems)


def age(ts: str, now: datetime | None = None) -> str:
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "age unknown"
    d = (now or datetime.now(timezone.utc)) - then
    if d.days >= 1:
        return f"{d.days}d ago"
    if d.total_seconds() < 60:
        return "just now"
    h = d.seconds // 3600
    return f"{h}h ago" if h else f"{max(1, d.seconds // 60)}m ago"


_BREAKS = ("\r\n", "\n", "\r", " ", " ", "\x85", "\x0b", "\x0c",
           "\x1c", "\x1d", "\x1e")  # FS/GS/RS: str.splitlines() breaks on these too


def oneline(text: str) -> str:
    """Free text on one line, so recorded words can never draw a fake entry or heading where they are shown."""
    for b in _BREAKS:
        text = text.replace(b, " ⏎ ")
    return text


def render(e: dict) -> str:
    """One entry as an agent or a person reads it: the decider's words first, then who owns the call."""
    label = f"{e.get('type')} · {e['kind']}" if e.get("kind") else str(e.get("type"))
    bits = [f"[{label}] {e['id']}"]
    if e.get("words"):
        bits.append(f'  words: "{oneline(e["words"])}"')
    if e.get("owner"):
        bits.append(f"  owner of the call: {oneline(e['owner'])}")
    src = f"pack {oneline(e['pack'])}" if e.get("pack") else (e.get("agent") or "human")
    bits.append(f"  recorded by {oneline(str(e.get('author')))} ({src}), {age(e.get('ts', ''))}")
    if e.get("summary"):
        bits.append(f"  summary by {oneline(str(e.get('author')))}: {oneline(e['summary'])}")
    if e.get("reason"):
        bits.append(f"  why: {oneline(e['reason'])}")
    if e.get("paths"):
        bits.append(f"  governs: {oneline(', '.join(e['paths']))}")
    if e.get("recheck"):
        bits.append(f"  re-check: {oneline(e['recheck'])}")
    # One fold over every line this emits, whatever field fed it: per-field folding missed a new field each review
    # round (spore-813), and a line break in any of them draws a forged line in an agent's context.
    return "\n".join(oneline(b) for b in bits)
