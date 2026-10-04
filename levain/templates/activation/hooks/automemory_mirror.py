#!/usr/bin/env python3
"""Levain activation — mirror Claude Code's native auto-memory into this install's anneal store.

Claude Code keeps a per-project auto-memory (`<config>/projects/<folder>/memory/*.md`): the
operator-facing layer the seed hands it (stated preferences, shorthand, working rules,
corrections). That store has no consolidation and no immune system, and it never leaves this
machine's Claude Code. This hook copies every write to it, one way, into THIS install's anneal
store as a decision episode, so the entity's own memory carries the operator layer too and the
wrap decides what graduates. It never writes continuity, and it never writes back to native
memory.

There is deliberately no automatic rebuild of lost state from the store (see LOST STATE).

TWO ENTRY POINTS, ONE SWEEP:
  hook    PostToolUse on Write|Edit. Reads the payload and, only when the edited file is one of
          this install's auto-memory files, spawns a detached `sweep` and returns. It writes
          nothing itself, prints nothing and always exits 0, so it can never block a Write or an
          Edit.
  sweep   Spawned by the hook and by session_start.py (which catches edits made by hand). Under
          an exclusive lock it compares each auto-memory file's sha256 with the one recorded at
          its last mirror and records:
            new file      -> a decision episode carrying the file's text;
            changed file  -> a decision episode that supersedes the previous one;
            deleted file  -> a decision episode that supersedes it as RETRACTED;
            recreated     -> a new-rule episode that supersedes the retraction.
          Supersession is anneal's own (`record --supersedes`), so recall hides the replaced
          episode. anneal refuses a supersession whose text shares too little with the old
          episode; the write is then retried once with the earlier text quoted, which grounds it.
          A file's new hash is recorded only after its episode write succeeded, so a failed write
          is retried at the next sweep; a write that landed unconfirmed costs a duplicate, never
          a loss. A file that exists but cannot be read is retried, never retracted.

NO BACKFILL: the first sweep that sees a memory folder records every existing file's hash in it
without writing an episode (a new install, an install upgraded by `levain update`, or an
autoMemoryDirectory set later), so only rules written or changed after that are mirrored. The
edit that triggered the sweep is the exception: it is the rule being written now.

LOST STATE: when the state file is missing or corrupt while the store already holds mirror
episodes, the sweep STOPS and writes `.levain/automemory_mirror.lost` (`levain doctor` reports
it). Re-baselining then would leave an edited or deleted rule current forever. Recovery is a
person's: restore the state file, or accept the loss, then delete the marker.

ON BY DEFAULT. Off when `LEVAIN_AUTOMEMORY_MIRROR` is off/0/false/no (env, per session; it
wins), or `.levain/config.json` has `"automemory_mirror": false` (or one of those strings).

WHICH FOLDER: Claude Code's per-project folder for the install (and for the git work tree it
sits in), plus any `autoMemoryDirectory` set in the install's .claude/settings.local.json or
.claude/settings.json or in the user's settings.json. NOT MIRRORED: MEMORY.md (the index); any
other folder.

FAIL-OPEN: every entry point catches everything and exits 0.

    <python> automemory_mirror.py hook < payload.json
    <python> automemory_mirror.py sweep [--dry-run] [--trigger PATH]
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
_OFF = {"off", "0", "false", "no"}


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


def mirror_enabled() -> bool:
    env = os.environ.get("LEVAIN_AUTOMEMORY_MIRROR", "").strip().lower()
    if env:
        return env not in _OFF
    try:
        cfg = json.loads((_levain_dir() / "config.json").read_text(encoding="utf-8"))
        value = cfg.get("automemory_mirror", True) if isinstance(cfg, dict) else True
    except Exception:
        return True
    if value is False:
        return False
    return not (isinstance(value, str) and value.strip().lower() in _OFF)


def _config_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(base).expanduser() if base else Path.home() / ".claude"


def _projects_dir() -> Path:
    return _config_dir() / "projects"


def _configured_dirs() -> list[Path]:
    """Every `autoMemoryDirectory` set in a settings file this hook can read: the install's
    .claude/settings.local.json and .claude/settings.json, and the user's settings.json.
    A directory given only by `claude --settings` on the command line is invisible here."""
    root = hook.install_root()
    out = []
    for f in (root / ".claude" / "settings.local.json", root / ".claude" / "settings.json",
              _config_dir() / "settings.json"):
        try:
            value = json.loads(f.read_text(encoding="utf-8")).get("autoMemoryDirectory")
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            d = Path(value.strip()).expanduser()
            out.append(d if d.is_absolute() else root / d)
    return out


def _project_roots() -> list[Path]:
    """The directories Claude Code may key this install's auto-memory folder by: the install
    itself, and the git work tree it sits in when that differs."""
    root = hook.install_root()
    roots = [root]
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=2)
        top = r.stdout.strip()
        if r.returncode == 0 and top:
            top_path = Path(top).resolve()
            if top_path != root:
                roots.append(top_path)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return roots


def memory_dirs() -> list[Path]:
    proj = _projects_dir()
    dirs = [proj / folder_name(r) / "memory" for r in _project_roots()] + _configured_dirs()
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
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


def memory_files(dirs: list[Path]) -> tuple[dict, set]:
    """({path: sha256} readable now, {paths that exist but could not be read})."""
    out, unreadable = {}, set()
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == INDEX_NAME:
                continue
            try:
                out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                unreadable.add(str(f))
    return out, unreadable


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
    bin_path = getattr(hook, "_INSTALL_ANNEAL_BIN", "{{")
    if "{{" not in bin_path:
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


def _record(body: str, tags: list[str], supersedes: str | None) -> str:
    args = ["--json", "record", "-", "--type", "decision", "--source", SOURCE,
            "--tags", ",".join(tags)]
    if supersedes:
        args += ["--supersedes", supersedes]
    r = _anneal(args, stdin=body)
    if r is None or r[0] != 0:
        return ""
    try:
        ep = json.loads(r[1]).get("id")
    except (ValueError, AttributeError):
        return ""
    return ep if isinstance(ep, str) and ep else ""


def _episode_text(ep_id: str) -> str | None:
    """The content of an earlier episode, '' when anneal says it no longer exists, None when
    it cannot be read."""
    r = _anneal(["--json", "get", ep_id])
    if r is None:
        return None
    if r[0] != 0:
        return "" if "not found" in (r[1] + r[2]).lower() else None
    try:
        content = json.loads(r[1]).get("content")
    except (ValueError, AttributeError):
        return None
    return content if isinstance(content, str) else None


def write_episode(body: str, tags: list[str], prev_episode: str | None) -> str:
    """Record one mirror episode; return its id, or '' on failure (retried next sweep)."""
    if not prev_episode:
        return _record(body, tags, None)
    ep = _record(body, tags, prev_episode)
    if ep:
        return ep
    earlier = _episode_text(prev_episode)
    if earlier is None:
        return ""
    if earlier == "":
        return _record(body, tags, None)        # the old episode is gone: nothing to supersede
    quoted = earlier if len(earlier) <= MAX_BODY_CHARS else earlier[:MAX_BODY_CHARS] + " […]"
    return _record(f"{body}\n\nIt replaces this earlier text:\n\n{quoted}", tags, prev_episode)


def path_tag(path: str) -> str:
    return "amem-path-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _tags(path: str) -> list[str]:
    return ["operator-rule", "automemory", "mirror", path_tag(path)]


def episode_body(path: str, sha: str, prev: dict | None, text: str | None) -> str:
    """`text` None means the file was deleted."""
    where = f"memory/{Path(path).name}"
    if text is None:
        return (f"RETRACTED OPERATOR RULE (auto-memory mirror): {where} was deleted. The rule "
                f"recorded in {prev.get('episode')} no longer holds; do not graduate or recall it "
                f"as current.")
    head = "OPERATOR RULE (auto-memory mirror)"
    if prev:
        head = f"OPERATOR RULE, REVISED (auto-memory mirror; replaces {prev.get('episode')})"
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n[… cut at {MAX_BODY_CHARS} chars; full text in {where}]"
    return f"{head}: {where} (sha256 {sha[:12]})\n\n{text}"


def sweep(dry_run: bool = False, trigger: str = "", writer=None) -> dict:
    """Mirror every change since the last sweep, holding the lock throughout. Returns counts."""
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
        now, unreadable = memory_files(dirs)
        first = META_KEY not in state
        if first:
            has = store_has_mirror_episodes()
            if has is None:
                counts["store_unreadable"] = 1      # cannot tell lost from first: do nothing
                return counts
            if has:
                counts["state_lost"] = 1
                if not dry_run:
                    lost_marker().write_text(
                        "auto-memory mirror stopped: .levain/automemory_mirror.json is missing "
                        "or unreadable while this store already holds mirror episodes. Restore "
                        "the state file, or accept that edits made since are not mirrored, then "
                        "delete this marker.\n", encoding="utf-8")
                return counts
        trigger = os.path.realpath(os.path.expanduser(trigger)) if trigger else ""
        # No backfill, per folder: a memory folder the state has not seen yet (the first sweep,
        # or an autoMemoryDirectory set later) has its existing files baselined, not mirrored.
        meta = state.get(META_KEY) if isinstance(state.get(META_KEY), dict) else {}
        known = set() if first else set(meta.get("dirs", []))
        fresh = {str(d) for d in dirs} - known
        if fresh:
            snap = dict(state)
            for path in sorted(set(now) | unreadable):
                if str(Path(path).parent) not in fresh or path in snap \
                        or (trigger and os.path.realpath(path) == trigger):
                    continue
                snap[path] = {"sha": now.get(path), "episode": None, "baseline": True,
                              "mirrored_at": time.time()}
                counts["baselined"] = counts.get("baselined", 0) + 1
            snap[META_KEY] = {**meta, "baseline_at": meta.get("baseline_at", time.time()),
                              "dirs": sorted(known | fresh)}
            if dry_run:
                return counts
            save_state(snap)
            state = snap
            if first and not trigger:
                return counts
        for path in sorted((set(now) | set(state)) - {META_KEY}):
            prev = state.get(path)
            sha = now.get(path)
            if sha is None and path not in unreadable and os.path.lexists(path) \
                    and not is_memory_file(path, dirs):
                state.pop(path, None)              # left the mirror's scope: forget it
                save_state(state)
                continue
            if sha is None and (path in unreadable or os.path.lexists(path)):
                counts["failed"] += 1              # present but unreadable: retry, never retract
                continue
            if sha is not None and prev and prev.get("baseline") and not prev.get("episode") \
                    and prev.get("sha") is None:
                prev["sha"] = sha                  # unreadable at the baseline: baseline it now
                save_state(state)
                continue
            if sha is not None and prev and not prev.get("deleted") and prev.get("sha") == sha:
                counts["unchanged"] += 1
                continue
            if sha is None and (not prev or prev.get("deleted") or not prev.get("episode")):
                if prev and not prev.get("deleted") and not prev.get("episode"):
                    state.pop(path, None)          # a baselined file deleted: nothing to retract
                    save_state(state)
                continue
            live = prev if prev and not prev.get("deleted") and prev.get("episode") else None
            kind = "deleted" if sha is None else ("changed" if live else "new")
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
            # A recreated file supersedes its retraction, so the retraction stops being current.
            replaces = prev.get("episode") if prev and prev.get("episode") else None
            ep = writer(episode_body(path, sha or "", live, text), _tags(path), replaces)
            if not ep:
                counts["failed"] += 1
                continue
            state[path] = ({"sha": sha, "episode": ep, "mirrored_at": time.time()}
                           if sha is not None else
                           {"sha": prev.get("sha"), "episode": ep, "mirrored_at": time.time(),
                            "deleted": True})
            save_state(state)        # per file: a later failure never loses an earlier success
            counts[kind] += 1
    return counts


def spawn_sweep(trigger: str = "") -> None:
    """Start `sweep` detached (no wait, no inherited stdio)."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "sweep"]
                     + (["--trigger", trigger] if trigger else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def start_sweep_if_enabled() -> None:
    """session_start.py's entry: a detached sweep for hand edits. Never raises."""
    try:
        if hook.should_fire() and mirror_enabled():
            spawn_sweep()
    except Exception:
        pass


def run_hook() -> int:
    """PostToolUse entry. Always 0, never prints."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        ti = payload.get("tool_input") if isinstance(payload, dict) else None
        path = ti.get("file_path") if isinstance(ti, dict) else None
        if isinstance(path, str) and path.endswith(".md") and hook.should_fire() \
                and mirror_enabled() and is_memory_file(path):
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
            print(json.dumps(sweep(dry_run="--dry-run" in argv, trigger=trig)))
            return 0
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
