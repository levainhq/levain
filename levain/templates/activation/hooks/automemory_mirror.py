#!/usr/bin/env python3
"""Levain activation — mirror Claude Code's native auto-memory into this install's anneal store.

Claude Code keeps a per-project auto-memory (`<config>/projects/<folder>/memory/*.md`): the
operator-facing layer the seed hands it (stated preferences, shorthand, working rules,
corrections), plus project and reference notes. That store has no consolidation and no immune
system, and it never leaves this machine's Claude Code. This hook copies every write to it, one
way, into THIS install's anneal store as an episode, so the entity's own memory carries the
operator layer too and the wrap decides what graduates. It never writes continuity, and it never
writes back to native memory.

There is deliberately no automatic rebuild of lost state from the store (see LOST STATE).

TWO ENTRY POINTS, ONE SWEEP:
  hook    PostToolUse on Write|Edit. Reads the payload and, only when the edited file is one of
          this install's auto-memory files, spawns a detached `sweep` and returns. It writes
          nothing itself, prints nothing and always exits 0.
  sweep   Spawned by the hook and by session_start.py (which catches edits made by hand or
          through Bash). Under an exclusive lock it compares each auto-memory file's sha256 with
          the one recorded at its last mirror and records:
            new file      -> an episode carrying the file's text;
            changed file  -> an episode that supersedes the current one for that file;
            deleted file  -> a RETRACTED episode that supersedes it;
            recreated     -> a new episode that supersedes the retraction.
          A note whose frontmatter `type` is user or feedback is an OPERATOR RULE (`decision`,
          tag operator-rule); any other note is an AUTO-MEMORY NOTE (`context`).

WHAT IS SUPERSEDED IS READ FROM THE STORE, NOT FROM THE STATE FILE: every write supersedes
every episode the store currently shows (recall-visible) carrying that file's path tag. anneal
accepts a second supersession of an already-superseded episode and the chain forks, so a stale
id held locally (a write that landed unconfirmed, a restored state file) would otherwise leave
an old rule current. The state file only remembers hashes: what was last mirrored, and what was
baselined. anneal refuses a supersession whose text shares too little with the old episode;
only that refusal is retried, once, with the earlier text quoted, which grounds it.
A file's new hash is recorded only after its episode write succeeded. A file that exists but
cannot be read is retried, never retracted.

NO BACKFILL: the first sweep that sees a memory folder records every existing file's hash in it
without writing an episode (a new install, an install upgraded by `levain update`, or an
autoMemoryDirectory set later). A folder that drops out of scope keeps its entries, so it is not
backfilled when it returns, and only a folder that could be listed becomes known. A note modified
at or after the session's start (passed by session_start.py; else the sweep's own start) is the
exception: it is being written now. A folder that is missing or cannot be listed is skipped and
reported; nothing in it is ever retracted.

LOST STATE: when the state file is missing or corrupt while the store already holds mirror
episodes, the sweep STOPS and writes `.levain/automemory_mirror.lost` (`levain doctor` reports
it). Re-baselining then would leave an edited or deleted note current until its next edit.
Recovery is a person's: restore the state file (safe: supersession reads the store) and delete
the marker, or turn the mirror off. Deleting only the marker stops it again.

ON BY DEFAULT. Off when `LEVAIN_AUTOMEMORY_MIRROR` is off/0/false/no (env, per session; it
wins), or `.levain/config.json` has `"automemory_mirror"` false/0 (or one of those strings).

WHICH FOLDER: Claude Code's per-project folder for the install, for the git work tree it sits
in, and for the repository of a linked worktree (Claude Code keys auto-memory by repository),
or `<config>/projects/$CLAUDE_CODE_PROJECT_DIR_NAME`; plus the effective `autoMemoryDirectory`
when it comes from the install's .claude/settings.local.json or .claude/settings.json (absolute
or `~/` only, as Claude Code requires). A user-scope `autoMemoryDirectory` is NOT mirrored: one
folder shared by every project would pull other projects' notes into this entity. Invisible to
any hook: `claude --settings` and managed (policy) settings. NOT MIRRORED: MEMORY.md (the index).

FAIL-OPEN: every entry point catches everything and exits 0.

    <python> automemory_mirror.py hook < payload.json
    <python> automemory_mirror.py sweep [--dry-run] [--trigger PATH] [--started EPOCH]
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_STARTED = time.time()

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    sys.exit(0)

SOURCE = "automemory-mirror"
INDEX_NAME = "MEMORY.md"
META_KEY = "__meta__"
LOCK_WAIT_SECONDS = 60
MAX_BODY_CHARS = 20_000
ANNEAL_TIMEOUT = 60
PAGE = 500
_OFF = {"off", "0", "false", "no"}
_RULE_TYPES = {"user", "feedback"}
_GROUNDING_REFUSAL = "shares too little"     # anneal's supersede refusal (SupersessionError text)


def folder_name(path: Path | str) -> str:
    """The folder name Claude Code gives a project directory: every character outside
    [A-Za-z0-9] becomes '-' (so '/.' becomes '--')."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _levain_dir() -> Path:
    return hook.install_root() / ".levain"


def state_path() -> Path:
    return _levain_dir() / "automemory_mirror.json"


def lost_marker() -> Path:
    return _levain_dir() / "automemory_mirror.lost"


def pending_path() -> Path:
    """Holds the earliest time a first sweep could not read the store. Until a first sweep
    succeeds, nothing modified after it is baselined, so a note written then is mirrored late,
    never lost. `levain doctor` reports the file while it exists."""
    return _levain_dir() / "automemory_mirror.pending"


def _read_pending() -> float | None:
    """The recorded time; a file that exists but does not parse counts from its own mtime."""
    try:
        return float(pending_path().read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:
            return pending_path().stat().st_mtime
        except OSError:
            return None


def mirror_enabled() -> bool:
    env = os.environ.get("LEVAIN_AUTOMEMORY_MIRROR", "").strip().lower()
    if env:
        return env not in _OFF
    try:
        cfg = json.loads((_levain_dir() / "config.json").read_text(encoding="utf-8"))
        value = cfg.get("automemory_mirror", True) if isinstance(cfg, dict) else True
    except Exception:
        return True
    if value is False or (type(value) is int and value == 0):
        return False
    return not (isinstance(value, str) and value.strip().lower() in _OFF)


def _config_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(base).expanduser() if base else Path.home() / ".claude"


def _projects_dir() -> Path:
    return _config_dir() / "projects"


def _setting_dir(f: Path) -> tuple[bool, Path | None]:
    """(file sets the key, the directory if it is one Claude Code accepts)."""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return False, None
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return False, None
    value = value.strip()
    if value.startswith("~/"):
        return True, Path.home() / value[2:]
    return True, (Path(value) if Path(value).is_absolute() else None)


def _configured_dir() -> Path | None:
    """The effective `autoMemoryDirectory` (local beats project beats user), when it comes from
    the install's own settings. A user-scope value is shared by every project: not mirrored."""
    root = hook.install_root()
    for f in (root / ".claude" / "settings.local.json", root / ".claude" / "settings.json"):
        found, d = _setting_dir(f)
        if found:
            return d
    return None


def _git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", *args],
                           capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _project_roots() -> list[Path]:
    """The directories Claude Code may key this install's auto-memory folder by: the install,
    the git work tree it sits in, and the main repository of a linked worktree."""
    root = hook.install_root()
    roots = [root]
    top = _git(root, "--show-toplevel")
    if top:
        roots.append(Path(top).resolve())
    common = _git(root, "--path-format=absolute", "--git-common-dir")
    if common:
        c = Path(common).resolve()
        roots.append(c.parent if c.name == ".git" else c)
    return roots


def memory_dirs(with_git: bool = True) -> list[Path]:
    proj = _projects_dir()
    roots = _project_roots() if with_git else [hook.install_root()]
    dirs = [proj / folder_name(r) / "memory" for r in roots]
    named = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME", "").strip()
    if named and "/" not in named and named not in (".", ".."):
        dirs.append(proj / named / "memory")
    configured = _configured_dir()
    if configured is not None:
        dirs.append(configured)
    out: list[Path] = []
    for d in dirs:
        c = Path(os.path.realpath(d))     # one identity per folder: aliases must not split a chain
        if c not in out:
            out.append(c)
    return out


def is_memory_file(path: str, dirs: list[Path] | None = None) -> bool:
    """True for a direct `*.md` child (not MEMORY.md) of one of this install's memory dirs."""
    try:
        p = Path(path).expanduser()
        if p.name == INDEX_NAME or not p.name.endswith(".md"):
            return False
        parent = p.parent.resolve()
        return any(parent == d.resolve() for d in (dirs if dirs is not None else memory_dirs()))
    except (OSError, RuntimeError, ValueError):
        return False


def memory_files(dirs: list[Path]) -> tuple[dict, set, set]:
    """({path: sha256} readable now, {paths that exist but could not be read}, {folders that
    were listed}). A folder that is missing or cannot be listed is absent from the third set,
    and nothing inside it is ever taken as deleted."""
    out, unreadable, listed = {}, set(), set()
    for d in dirs:
        try:
            names = sorted(e.name for e in os.scandir(d))
        except OSError:
            continue
        listed.add(str(d))
        for name in names:
            f = d / name
            if name == INDEX_NAME or not name.endswith(".md"):
                continue
            try:
                if not f.is_file():
                    continue
                out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                unreadable.add(str(f))
    return out, unreadable, listed


class StateUnreadable(Exception):
    pass


def load_state() -> dict:
    """{path: {...}}; {} when missing or corrupt (corrupt counts as lost). Raises
    StateUnreadable when the file exists but cannot be read just now."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}
    except OSError as exc:
        raise StateUnreadable(str(exc)) from exc


def save_state(state: dict) -> None:
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(f"{sp.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, sp)


def _anneal(args: list[str], stdin: str | None = None) -> tuple[int, str, str] | None:
    """Run one anneal-memory command against THIS install's store; (rc, stdout, stderr), or
    None when no candidate could be started or it timed out. The next candidate is tried only
    when one cannot be STARTED, never after one ran: a record that ran and failed must not be
    re-run by a second entry point (that is how a duplicate would be written)."""
    db = str(hook.store_path())
    candidates = []
    unfilled = "{{" + "ANNEAL_MEMORY}}"   # split, so the install fill cannot reach it
    bin_path = getattr(hook, "_INSTALL_ANNEAL_BIN", unfilled)
    if bin_path != unfilled:
        candidates.append([bin_path, "--db", db, *args])
    candidates.append([sys.executable, "-P", "-m", "anneal_memory", "--db", db, *args])
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                               errors="replace", timeout=ANNEAL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        return r.returncode, r.stdout or "", r.stderr or ""
    return None


def store_has_mirror_episodes() -> bool | None:
    """Whether this install's store holds any mirror episode; None when it cannot be told."""
    if not hook.store_path().exists():
        return False
    r = _anneal(["--json", "episodes", "--source", SOURCE, "--include-superseded",
                 "--limit", "1"])
    if r is None or r[0] != 0:
        return None
    try:
        total = json.loads(r[1]).get("total_matching")
    except (ValueError, AttributeError):
        return None
    return total > 0 if isinstance(total, int) else None


def current_heads() -> dict[str, list[dict]] | None:
    """{path tag: [visible mirror episodes carrying it]} read from the store; None when it
    cannot be read (then nothing is written: superseding blind could fork a chain)."""
    if not hook.store_path().exists():
        return {}
    heads: dict[str, list[dict]] = {}
    offset = 0
    while True:
        r = _anneal(["--json", "episodes", "--source", SOURCE, "--limit", str(PAGE),
                     "--offset", str(offset)])
        if r is None or r[0] != 0:
            return None
        try:
            data = json.loads(r[1])
            eps = data["episodes"]
            total = data.get("total_matching", 0)
        except (ValueError, KeyError, TypeError):
            return None
        for ep in eps:
            tags = (ep.get("metadata") or {}).get("tags") or []
            for t in tags:
                if isinstance(t, str) and t.startswith("amem-path-"):
                    heads.setdefault(t, []).append(ep)
        offset += len(eps)
        if not eps or offset >= total:
            return heads


def _record(body: str, etype: str, tags: list[str], supersedes: list[str]) -> tuple[str, str]:
    """(episode id or '', anneal's stderr)."""
    args = ["--json", "record", "-", "--type", etype, "--source", SOURCE,
            "--tags", ",".join(tags)]
    for s in supersedes:
        args += ["--supersedes", s]
    r = _anneal(args, stdin=body)
    if r is None:
        return "", ""
    if r[0] != 0:
        return "", r[2] + r[1]
    try:
        ep = json.loads(r[1]).get("id")
    except (ValueError, AttributeError):
        return "", ""
    return (ep if isinstance(ep, str) and ep else ""), ""


def write_episode(body: str, etype: str, tags: list[str], heads: list[dict]) -> str:
    """Record one mirror episode superseding `heads`; its id, or '' (retried next sweep).
    Only anneal's grounding refusal is retried here, with the earlier text quoted: a timeout or
    any other failure may have committed, and re-recording then would fork the chain."""
    ids = [h["id"] for h in heads if isinstance(h.get("id"), str)]
    ep, err = _record(body, etype, tags, ids)
    if ep or not ids or _GROUNDING_REFUSAL not in err:
        return ep
    earlier = "\n\n".join(h.get("content") or "" for h in heads)
    if len(earlier) > MAX_BODY_CHARS:
        earlier = earlier[:MAX_BODY_CHARS] + " […]"
    ep, _ = _record(f"{body}\n\nIt replaces this earlier text:\n\n{earlier}", etype, tags, ids)
    return ep


def path_tag(path: str) -> str:
    return "amem-path-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def note_type(text: str) -> str:
    """The `type:` field of a note's YAML frontmatter, lowercased; '' when there is none."""
    m = re.match(r"\A---\s*\n(.*?)\n---", text.lstrip("\ufeff"), re.S)
    if not m:
        return ""
    # top level, or nested (Claude Code writes `metadata:` then an indented `type:`)
    t = re.search(r"^[ \t]*type:\s*['\"]?([A-Za-z_-]+)", m.group(1), re.M)
    return t.group(1).lower() if t else ""


def episode_body(path: str, sha: str, rule: bool, revised: bool, text: str | None) -> str:
    """`text` None means the file was deleted."""
    where = f"memory/{Path(path).name}"
    label = "OPERATOR RULE" if rule else "AUTO-MEMORY NOTE"
    if text is None:
        return (f"RETRACTED {label} (auto-memory mirror): {where} was deleted. What it said no "
                f"longer holds; do not graduate or recall it as current.")
    head = f"{label} (auto-memory mirror)"
    if revised:
        head = f"{label}, REVISED (auto-memory mirror)"
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n[… cut at {MAX_BODY_CHARS} chars; full text in {where}]"
    tail = ("\n\n(A copy of a native memory note. If you also recorded this yourself, the two are "
            "one piece of evidence, not two.)")
    return f"{head}: {where} (sha256 {sha[:12]})\n\n{text}{tail}"


def _tags(path: str, rule: bool) -> list[str]:
    return (["operator-rule"] if rule else []) + ["automemory", "mirror", path_tag(path)]


def _finish(state: dict, counts: dict, dry_run: bool) -> dict:
    """Record this sweep's outcome in the state for `levain doctor`, then return the counts."""
    if not dry_run and META_KEY in state:
        state[META_KEY] = {**state[META_KEY], "last_sweep": {"at": time.time(), **counts}}
        try:
            save_state(state)
        except OSError:
            pass
    return counts


def sweep(dry_run: bool = False, trigger: str = "", writer=None,
          started: float | None = None) -> dict:
    """Mirror every change since the last sweep, holding the lock throughout. Returns counts.
    `started` is when the spawning session began (session_start.py passes it): a note modified
    at or after it is being written now and is never baselined, whichever sweep runs first."""
    writer = writer or write_episode
    counts = {"new": 0, "changed": 0, "deleted": 0, "failed": 0, "unchanged": 0}
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    with open(sp.with_name(sp.name + ".lock"), "w") as lock:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    counts["locked_out"] = 1
                    return counts
                time.sleep(0.2)
        if lost_marker().exists():
            counts["state_lost"] = 1               # stopped until a person clears the marker
            return counts
        try:
            state = load_state()
        except StateUnreadable:
            counts["state_unreadable"] = 1         # transient: next sweep
            return counts
        dirs = memory_dirs()
        dir_set = {str(d) for d in dirs}
        now, unreadable, listed = memory_files(dirs)
        first = META_KEY not in state
        if first:
            has = store_has_mirror_episodes()
            if has is None:
                counts["store_unreadable"] = 1      # cannot tell lost from first: wait
                if not dry_run:
                    since = [t for t in (started, _STARTED, _read_pending()) if t]
                    if trigger:
                        try:
                            since.append(os.path.getmtime(trigger))
                        except OSError:
                            pass
                    try:
                        tmp = pending_path().with_name(f"{pending_path().name}.{os.getpid()}")
                        tmp.write_text(repr(min(since)), encoding="utf-8")
                        os.replace(tmp, pending_path())
                    except OSError:
                        pass
                return counts
            if has:
                counts["state_lost"] = 1
                if not dry_run:
                    lost_marker().write_text(
                        "auto-memory mirror stopped: .levain/automemory_mirror.json is missing "
                        "or unreadable while this store already holds mirror episodes. Restore "
                        "the state file from a backup (an older copy is safe) and then delete "
                        "this marker; or turn the mirror off (\"automemory_mirror\": false in "
                        ".levain/config.json). Deleting only this marker stops it again.\n",
                        encoding="utf-8")
                return counts
        trigger = os.path.realpath(os.path.expanduser(trigger)) if trigger else ""

        meta = state.get(META_KEY) if isinstance(state.get(META_KEY), dict) else {}
        # A note modified at or after the cutoff (the session's start when session_start.py
        # passed it, else this sweep's own) is being written now and is never baselined.
        cutoff = min(started, _STARTED) if started else _STARTED
        if not first and not dry_run and pending_path().exists():
            try:
                pending_path().unlink()            # left by a crash after the first save
            except OSError:
                pass
        pending = _read_pending() if first else None
        if pending is not None:
            cutoff = min(cutoff, pending)          # a first sweep that waited on the store

        def written_now(path: str) -> bool:
            if trigger and os.path.realpath(path) == trigger:
                return True
            try:
                return os.path.getmtime(path) >= cutoff
            except OSError:
                return False

        # No backfill, per folder: a folder the state has not listed before has its existing
        # files baselined, not mirrored. Only a folder that could be listed becomes known.
        known = set() if first else set(meta.get("dirs", []))
        fresh = (listed & dir_set) - known
        if fresh or first:
            snap = dict(state)
            for path in sorted(set(now) | unreadable):
                if str(Path(path).parent) not in fresh or path in snap or written_now(path):
                    continue
                snap[path] = {"sha": now.get(path), "baseline": True, "mirrored_at": time.time()}
                counts["baselined"] = counts.get("baselined", 0) + 1
            snap[META_KEY] = {**meta, "baseline_at": meta.get("baseline_at", time.time()),
                              "dirs": sorted(known | fresh)}
            if not dry_run:
                save_state(snap)
                if first:
                    try:
                        pending_path().unlink()
                    except OSError:
                        pass
            state = snap
        heads = None                               # read from the store on the first write
        for path in sorted((set(now) | set(state)) - {META_KEY}):
            if str(Path(path).parent) not in dir_set:
                continue                           # out of scope now: dormant, never forgotten
            prev = state.get(path)
            sha = now.get(path)
            if sha is None and str(Path(path).parent) not in listed:
                if prev and prev.get("episode") and not prev.get("deleted"):
                    counts["unavailable"] = counts.get("unavailable", 0) + 1
                continue                           # its folder cannot be listed: never retract
            if sha is None and (path in unreadable or os.path.lexists(path)):
                counts["failed"] += 1              # present but unreadable: retry, never retract
                continue
            if sha is not None and prev and prev.get("baseline") and prev.get("sha") is None:
                prev["sha"] = sha                  # unreadable at the baseline: baseline it now
                if not dry_run:
                    save_state(state)
                continue
            if sha is not None and prev and not prev.get("deleted") and prev.get("sha") == sha:
                counts["unchanged"] += 1
                continue
            if sha is None and (not prev or prev.get("deleted")):
                continue
            if sha is None and prev.get("baseline"):
                if not dry_run:
                    state.pop(path, None)          # a baselined file deleted: nothing to retract
                    save_state(state)
                continue
            kind = ("deleted" if sha is None else
                    "changed" if prev and not prev.get("deleted") and not prev.get("baseline")
                    else "new")
            if dry_run:
                counts[kind] += 1
                continue
            text = None
            if sha is not None:
                try:
                    raw = Path(path).read_bytes()
                except OSError:
                    counts["failed"] += 1
                    continue
                if hashlib.sha256(raw).hexdigest() != sha:   # edited mid-sweep: next sweep
                    counts["failed"] += 1
                    continue
                text = raw.decode("utf-8", errors="replace")
            if heads is None:
                heads = current_heads()
                if heads is None:
                    counts["failed"] += 1
                    counts["store_unreadable"] = 1
                    break
            tag = path_tag(path)
            current = heads.get(tag, [])
            rule = (note_type(text) in _RULE_TYPES if text is not None
                    else bool(prev and prev.get("rule")))
            etype = "decision" if rule else "context"
            ep = writer(episode_body(path, sha or "", rule, bool(current) and text is not None,
                                     text), etype, _tags(path, rule), current)
            if not ep:
                counts["failed"] += 1
                continue
            heads[tag] = [{"id": ep}]
            state[path] = ({"sha": sha, "episode": ep, "rule": rule, "mirrored_at": time.time()}
                           if sha is not None else
                           {"sha": prev.get("sha"), "episode": ep, "rule": rule,
                            "mirrored_at": time.time(), "deleted": True})
            save_state(state)        # per file: a later failure never loses an earlier success
            counts[kind] += 1
        return _finish(state, counts, dry_run)


def spawn_sweep(trigger: str = "", started: float | None = None) -> None:
    """Start `sweep` detached (no wait, no inherited stdio)."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "sweep"]
                     + (["--trigger", trigger] if trigger else [])
                     + (["--started", repr(started)] if started else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def start_sweep_if_enabled(started: float | None = None) -> None:
    """session_start.py's entry: a detached sweep for hand edits. Never raises."""
    try:
        if hook.should_fire() and mirror_enabled():
            spawn_sweep(started=started)
    except Exception:
        pass


def run_hook() -> int:
    """PostToolUse entry. Always 0, never prints. It asks git (2 s cap) only for a write under
    Claude Code's projects folder that the git-free folders do not already cover."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        ti = payload.get("tool_input") if isinstance(payload, dict) else None
        path = ti.get("file_path") if isinstance(ti, dict) else None
        if not (isinstance(path, str) and path.endswith(".md") and hook.should_fire()
                and mirror_enabled()):
            return 0
        if is_memory_file(path, memory_dirs(with_git=False)):
            spawn_sweep(path)
            return 0
        try:
            Path(path).resolve().relative_to(_projects_dir().resolve())
        except (ValueError, OSError, RuntimeError):
            return 0
        if is_memory_file(path):
            spawn_sweep(path)
    except Exception:
        pass
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["hook"]:
            return run_hook()
        if argv[:1] == ["sweep"]:
            if not (mirror_enabled() or "--dry-run" in argv):
                print(json.dumps({"disabled": "LEVAIN_AUTOMEMORY_MIRROR or config turns it off"}))
                return 0
            trig = argv[argv.index("--trigger") + 1] if "--trigger" in argv[:-1] else ""
            started = None
            if "--started" in argv[:-1]:
                try:
                    started = float(argv[argv.index("--started") + 1])
                except ValueError:
                    started = None
            print(json.dumps(sweep(dry_run="--dry-run" in argv, trigger=trig, started=started)))
            return 0
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
