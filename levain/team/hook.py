"""Claude Code hooks for the team layer: ``python -P -m levain.team.hook pretooluse|sessionstart``.

PreToolUse on Edit|Write|MultiEdit|NotebookEdit matches the target path against every in-force entry:
- a ``ruling`` owned by someone else: ``ask-once`` (default) denies the first touch per session with the
  record as the reason, and lets the retry through (recording an ``ack``); ``block`` denies until a
  superseding ruling exists; ``surface`` never denies;
- a ``tension`` on the path always denies ("ask the owners");
- practices, findings, questions and the actor's own rulings are surfaced as context, never denied;
- any edit inside the ledger worktree is denied: ``levain team record`` is the only write path.

Two rulings that disagree are NOT detected semantically: every in-force ruling on the path is shown
together, and an explicit ``tension`` entry is how a team marks a disagreement.

Every failure is fail-OPEN with one visible line (``[team] ledger unavailable: <reason>``): a broken ledger
never blocks work silently and never lets work through silently. Bash writes are not covered (path
extraction from shell is unreliable); that gap is documented, not hidden.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import canon as C
from . import entry as E
from . import index as I
from . import roles as R
from .transport import BRANCH, DIRNAME, LEGACY_MESSAGE, GitLedger, Repo, TeamError

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
TAG = "[levain team]"


@dataclass
class Decision:
    deny: bool = False
    text: str = ""
    newly_denied: set[str] = field(default_factory=set)
    ack: list[str] = field(default_factory=list)


def _mode(e: dict, team: R.Team) -> str:
    return e.get("mode") or team.mode


def decide(team: R.Team, ledger: I.Ledger, handle: str | None, rel: str, session: str,
           denied_before: set[str]) -> Decision | None:
    """What the hook does for one edit of ``rel``. Pure: no I/O. None = nothing applies."""
    matched = ledger.applying_to(rel)
    if not matched:
        return None
    tensions = [e for e in matched if e.get("type") == "tension"]
    foreign = [e for e in matched if e.get("kind") == "ruling" and not team.owns(handle, e.get("owner", ""))]
    always = tensions + [e for e in foreign if _mode(e, team) == "block"]
    once = [e for e in foreign if _mode(e, team) == "ask-once"]
    acked = ledger.acked(session, handle) if session else set()
    seen = denied_before | acked
    pending = [e for e in once if e["id"] not in seen]
    owners = sorted({I.oneline(e["owner"]) for e in tensions + foreign if e.get("owner")})
    blocks = "\n\n".join(I.render(e) for e in matched)
    head = (f"{TAG} {rel} is governed by {len(matched)} recorded entr{'y' if len(matched) == 1 else 'ies'} in "
            f"the {I.oneline(team.project)} team ledger.")
    if always or pending:
        how = []
        if pending:
            retry = ("If your change stays within the ruling(s) above, retry the same edit: the retry is allowed and "
                     "recorded as your acknowledgement. " if not always else "")
            how.append(f"{retry}If your change would contradict a ruling, STOP and ask the owner of the call "
                       f"({', '.join(owners) or 'the owner'}) before changing anything; do not route around this "
                       "check (no Bash, sed, heredoc or copy edits of this path). If the owner changes the call, "
                       "record their words as a new ruling (`levain team record decision --kind ruling --owner ... "
                       f"--words ... --refs <old id>`); the team owner ({I.oneline(team.owner)}) or the ruling's author then "
                       "supersedes the old one.")
        if [e for e in foreign if _mode(e, team) == "block" and e in always]:
            how.append("A ruling above is in BLOCK mode: no edit to this path until a superseding ruling is "
                       "recorded with the owner's words.")
        if tensions:
            how.append(f"CONFLICT: an open tension is recorded on this path. Ask {', '.join(owners)}; edits stay "
                       "blocked until the tension is superseded by a decision.")
        if handle is None:
            how.append("(This clone's git user.email maps to no member of the team, so every ruling here counts "
                       "as someone else's: `levain team doctor` shows the mapping.)")
        text = (f"{I.oneline(head)} Read it before changing this file.\n\n{blocks}\n\nWhat to do: "
                + " ".join(I.oneline(h) for h in how))
        return Decision(deny=True, text=text, newly_denied={e["id"] for e in pending})
    acks = [e["id"] for e in once if e["id"] in denied_before and e["id"] not in acked]
    lead = "Proceeding is allowed; stay within these." if foreign else "For your information:"
    return Decision(deny=False, text=f"{I.oneline(head)} {lead}\n\n{blocks}", ack=acks)


# ---- I/O -----------------------------------------------------------------------------------------------------

def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _fail_open(event: str, reason: str) -> None:
    line = f"[team] ledger unavailable: {I.oneline(reason)}"
    _out({"systemMessage": line,
          "hookSpecificOutput": {"hookEventName": event, "additionalContext": line}})


def _deny_unjudged(reason: str) -> None:
    """The gate HALTS when it cannot judge (tenure_design_1005.md §3f): an empty verdict must never read as "nothing
    governs this path". The person can override for a session with LEVAIN_TEAM_UNJUDGED=allow."""
    if os.environ.get("LEVAIN_TEAM_UNJUDGED") == "allow":
        _fail_open("PreToolUse", reason + " (LEVAIN_TEAM_UNJUDGED=allow: allowed for this session)")
        return
    _out({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": (f"{TAG} levain cannot judge this team ledger on this clone, so it cannot tell "
                                     f"whether a ruling governs this file: {I.oneline(reason)}. Run `levain team "
                                     "doctor`; to edit anyway for this session, set LEVAIN_TEAM_UNJUDGED=allow.")}})


def _target(payload: dict) -> str | None:
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return None
    p = ti.get("file_path") or ti.get("notebook_path")
    if not isinstance(p, str) or not p:
        return None
    if not os.path.isabs(p):
        cwd = payload.get("cwd") or os.getcwd()
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def _within(path: str, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def _in_ledger_machinery(path: str) -> bool:
    """Is ``path`` under some repository's ``<git dir>/levain-team/``? Purely by shape, no git needed."""
    p = Path(os.path.realpath(path))
    for parent in [p, *p.parents]:
        if parent.name.casefold() == DIRNAME and (parent.parent / "HEAD").is_file() \
                and (parent.parent / "objects").is_dir():
            return True
    return False


def _interval(gl: GitLedger) -> float:
    try:
        return float(gl.team().fetch_interval)
    except (R.RolesError, TeamError):
        return 300.0


def _wired_but_broken(gl: GitLedger) -> bool:
    try:
        return bool(gl._local_branch_exists())
    except TeamError:
        return False


def pretooluse(payload: dict) -> None:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return
    target = _target(payload)
    if not target:
        return
    # A path inside the ledger machinery is refused whether or not this clone has joined. It is checked BEFORE
    # repository discovery, which finds no working tree from inside .git (state.json, locks, sessions).
    if _in_ledger_machinery(target):
        _out({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": (f"{TAG} {I.oneline(target)} is inside the team ledger's private machinery. The "
                                         "ledger is written only through `levain team record` (it validates, "
                                         "hash-chains and attributes every entry); do not edit it directly.")}})
        return
    repo = Repo.discover(Path(target))
    if repo is None:
        return
    gl = GitLedger(repo)
    if not gl.joined():
        if gl.legacy_only():
            _fail_open("PreToolUse", LEGACY_MESSAGE)      # B-1: reported, nothing from it enforced
            return
        # a clone that pinned a ledger and lost its worktree or refs still has a ledger it cannot judge (code L3 r2
        # codex MED: it fell through and allowed the edit)
        if gl.pinned_root or _wired_but_broken(gl) or (gl.remote and gl._has(f"refs/remotes/{gl.remote}/{BRANCH}")):
            # a strict ledger exists here and this clone never pinned it (UNPINNED): judge nothing from it, halt
            _deny_unjudged(f"this clone has a {BRANCH} ledger it has not pinned (run `levain team join`)")
        return
    # Fetch first, then read team, ledger and identity together from the branch ref: one consistent snapshot,
    # no lock (the ref only moves when a rebase or commit completes).
    fetch_note = gl.fetch_if_due(_interval(gl), timeout=5.0)
    try:
        _, team, ledger = gl.snapshot()
    except Exception as exc:  # noqa: BLE001 - any failure to JUDGE a joined ledger halts the gate (§3f), never allows
        _deny_unjudged(str(exc) if isinstance(exc, (R.RolesError, TeamError)) else f"{type(exc).__name__}: {exc}")
        return
    rel = Path(os.path.realpath(target)).relative_to(os.path.realpath(repo.toplevel)).as_posix() \
        if _within(target, repo.toplevel) else None
    if rel is None:
        return
    # Claude Code always sends session_id; the transcript path is a stable stand-in if a build ever does not.
    session = str(payload.get("session_id") or "")
    if not session and payload.get("transcript_path"):
        # a schema-safe token (entries allow [A-Za-z0-9._:@-]{1,128}), unique per transcript
        session = "t-" + hashlib.sha256(str(payload["transcript_path"]).encode("utf-8")).hexdigest()[:32]
    handle = gl.handle(team)
    d = decide(team, ledger, handle, rel, session, gl.session_denied(session) if session else set())
    from .transport import WARNINGS
    notes = [f"[team] {w}" for w in WARNINGS]
    if fetch_note:
        notes.append(f"[team] ledger not refreshed: {fetch_note} (showing the last fetched copy)")
    if ledger.problems:
        notes.append(f"[team] ledger integrity: {len(ledger.problems)} problem(s); run `levain team verify`")
    notes = [I.oneline(n) for n in notes]   # a warning or a fetch error can carry git or ledger text
    if d is None:
        if notes:
            _out({"systemMessage": notes[0],
                  "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(notes)}})
        return
    text = d.text + ("\n\n" + "\n".join(notes) if notes else "")
    if d.deny:
        if session and d.newly_denied:
            try:
                gl.mark_denied(session, d.newly_denied)
            except OSError as exc:
                # Not remembering the deny means the next attempt is denied again: never a reason to allow.
                text += f"\n\n[team] could not record this denial ({I.oneline(str(exc))}); a retry will be denied again"
        _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": text}})
        return
    if d.ack and handle:
        try:
            gl.append(E.build(handle, "ack", refs=d.ack, session=session, agent="claude-code",
                              summary=f"proceeded with an edit of {rel} after the ruling was shown"),
                      push=False, lock_timeout=3.0)
        except (TeamError, E.EntryError) as exc:
            text += f"\n\n[team] acknowledgement not recorded: {I.oneline(str(exc))}"
    _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})


def _anneal_db(gl: GitLedger) -> Path | None:
    configured = gl.state().get("anneal_db")
    if isinstance(configured, str) and configured:
        p = Path(configured).expanduser()
        return p if p.exists() else None
    p = gl.repo.toplevel / ".levain" / "memory.db"
    return p if p.exists() else None


# The anneal-memory release that first reads team stream v3. Not released when this was written: set it to that
# version when levain's floor and KNOWN_GOOD move to it (seam §3a item 4).
ANNEAL_V3_FLOOR = "the first anneal-memory release that reads team stream v3"

# Run as ``python -P -c`` (never the cwd on sys.path): which team stream versions the INSTALLED anneal reads, and the
# exact v3 key sets it checks, so a reader of another v3 shape (the 10-05 header without prev_root) is never sent one.
_PROBE = (
    "import json\n"
    "try:\n"
    "    import anneal_memory as a, anneal_memory.team as t\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'error': type(exc).__name__}))\n"
    "else:\n"
    "    print(json.dumps({'version': str(getattr(a, '__version__', '?')),\n"
    "                      'versions': [v for v in (getattr(t, 'STREAM_VERSION', None),\n"
    "                                               getattr(t, 'SNAPSHOT_STREAM_VERSION', None)) if type(v) is int],\n"
    "                      'header': sorted(getattr(t, '_V3_HEADER', ()) or ()),\n"
    "                      'envelope': sorted(getattr(t, '_V3_ENVELOPE', ()) or ())}))\n"
)


def _anneal_probe() -> dict:
    """``{"version", "v3": bool}`` for the installed anneal-memory; ``v3`` False when it cannot be told."""
    from . import export as X
    try:
        cp = subprocess.run([sys.executable, "-P", "-c", _PROBE], capture_output=True, text=True, timeout=15)
        got = json.loads(cp.stdout.strip().splitlines()[-1]) if cp.returncode == 0 and cp.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        got = {}
    got = got if isinstance(got, dict) else {}
    v3 = (X.SNAPSHOT_VERSION in (got.get("versions") or []) and got.get("header") == sorted(X.V3_HEADER)
          and got.get("envelope") == sorted(X.V3_ENVELOPE))
    return {"version": str(got.get("version") or "?"), "v3": v3}


def _store_view(db: Path, key: str) -> list | None:
    """The store's record of this key, ``[repin_n, pos, seq, active]``, read through ``python -P``; None when the store
    holds no record of it or could not be read. A store reset or restored from backup therefore changes the import
    key, and so does a takeover (``active``)."""
    try:
        cp = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "team-status", "--json"],
                            capture_output=True, text=True, timeout=20)
        data = json.loads(cp.stdout) if cp.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    for k in (data.get("keys") or []) if isinstance(data, dict) else []:
        if isinstance(k, dict) and k.get("key") == key:
            return [k.get("repin_n"), k.get("pos"), k.get("seq"), bool(k.get("active"))]
    return None


def _v3_import_key(snap, db: Path, store: list | None, anneal_version: str) -> str:
    """seam §3a item 5: the verdict (state hash, honoured pairs, enforced lines, judged), the stream version, this
    clone's trust state (root, epoch, repin_n), the installed anneal, and the STORE's identity (its path and its record
    of this key), so a restored, reset or repointed store, a takeover, an upgrade or any verdict change imports once.
    ``prev_root`` and ``seq`` are left out: neither is a change to mirror."""
    from . import export as X
    h = snap.head
    blob = ["levain-anneal-import-v3", X.SNAPSHOT_VERSION, snap.state_hash, snap.honoured, sorted(snap.enforced),
            h["judged"], h["root"], h["epoch"], h["repin_n"], anneal_version, os.path.realpath(db), store]
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode("utf-8")).hexdigest()


def _anneal_import_v3(gl: GitLedger, db: Path, anneal_version: str) -> str | None:
    """Send this clone's complete verdict (contract v3) and mark it imported only when anneal REPLACED with it."""
    from . import export as X
    try:
        d = gl.derivation()
        snap = X.snapshot(gl, d)
    except (R.RolesError, TeamError, OSError) as exc:
        return f"[team] anneal import did not run: {exc}"
    if gl.state().get("anneal_v3_imported") == _v3_import_key(snap, db, _store_view(db, str(snap.head["key"])),
                                                               anneal_version):
        return None
    body = "".join(snap.lines(X.next_seq(gl)))
    try:
        cp = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "team-import", "--json",
                             "-"], input=body, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[team] anneal import did not run: {exc}"
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("framing") != "v3" or "snapshot" not in data:
        tail = ((cp.stderr or cp.stdout).strip().splitlines() or ["exit " + str(cp.returncode)])[-1]
        gl.save_state(anneal_last=f"failed: {tail}")
        return f"[team] anneal import failed: {tail}"
    outcome = str(data["snapshot"])
    added = len(data.get("links_added") or []) + len(data.get("links_added_legacy") or [])
    removed = len(data.get("links_removed") or [])
    unmappable = len(data.get("unmappable") or [])
    if outcome == "replaced":
        # the marker is computed from the store's state AFTER the import, so the next session's key matches it
        X.record_exported(gl, snap)
        mark = _v3_import_key(snap, db, _store_view(db, str(snap.head["key"])), anneal_version)
        gl.save_state(anneal_v3_imported=mark, anneal_last="ok" if cp.returncode == 0 else "replaced, with findings")
        text = (f"[team] team links in your memory store ({db.name}) now match the ledger: {added} added, {removed} "
                "removed")
        if unmappable:
            text += f"; {unmappable} enforced entr{'y' if unmappable == 1 else 'ies'} could not be imported " \
                    "(`anneal-memory team-status`)"
        return text
    reasons = [str(x) for x in (data.get("chain_problems") or []) + (data.get("snapshot_notes") or [])]
    if outcome == "partial_stream" and any("copied or reused state file" in r for r in reasons):
        why = ("this clone's export key is held by the store for another ledger (a copied or reused levain state "
               f"file); remove `anneal_key` from {gl.state_path} so this clone makes a key of its own")
    elif outcome == "partial_stream" and snap.head["judged"] != "full":
        why = (f"levain judges this ledger only in part on this clone ({d.frozen_why or 'see `levain team doctor`'}), "
               "so the store keeps its last view")
    elif outcome == "stale_stream":
        why = reasons[-1] if reasons else "the store already holds a later view of this ledger"
    else:
        why = reasons[-1] if reasons else f"exit {cp.returncode}"
    gl.save_state(anneal_last=f"{outcome}: {why}")
    return f"[team] team links in your memory store were NOT updated ({outcome}): {why}"


def anneal_import(gl: GitLedger, ledger: I.Ledger, tree: str, owner: str) -> str | None:  # tree: state hash
    """Optional seam: mirror levain's verdict into anneal's store.

    Runs only when the installed anneal-memory ships ``anneal_memory.team`` and a store is known. An anneal that
    reads team stream v3 gets this clone's complete verdict (seam §3a items 4-7) and replaces its team links with it,
    once per change; an older one gets the v2 stream, once per ledger tree, exactly as before, and the line says that
    it keeps links as first imported. Returns a one-line note when it ran, failed or needs saying, else None.
    """
    import importlib.util

    if os.environ.get("LEVAIN_TEAM_ANNEAL_IMPORT", "").lower() in ("0", "off", "false", "no"):
        return None
    try:
        if importlib.util.find_spec("anneal_memory.team") is None:
            return None
    except (ImportError, ValueError):
        return None
    db = _anneal_db(gl)
    if db is None:
        return None
    probe = _anneal_probe()
    if probe["v3"]:
        return _anneal_import_v3(gl, db, probe["version"])
    upgrade = (f"[team] anneal-memory {probe['version']} keeps links as first imported; upgrade to "
               f"{ANNEAL_V3_FLOOR}")
    if gl.state().get("anneal_imported_tree") == tree:
        return upgrade
    from .export import export_stream

    body = "".join(export_stream(ledger))
    try:
        # --link-authority: only the owner's supersedes cross authors, the same rule levain's in-force view applies
        cp = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "team-import",
                             "--link-authority", owner, "-"],
                            input=body, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[team] anneal import did not run: {exc}; {upgrade[7:]}"
    tail = ((cp.stderr or cp.stdout).strip().splitlines() or ["exit " + str(cp.returncode)])[-1]
    if cp.returncode == 3:
        # anneal imported what verified and refused the rest; not a crash, but a person should see it
        gl.save_state(anneal_imported_tree=tree, anneal_last=f"partial: {tail}")
        return f"[team] anneal import refused some entries: {tail}; {upgrade[7:]}"
    if cp.returncode != 0:
        gl.save_state(anneal_last=f"failed: {tail}")
        return f"[team] anneal import failed: {tail}; {upgrade[7:]}"
    gl.save_state(anneal_imported_tree=tree, anneal_last="ok")
    return f"[team] ledger imported into your memory store ({db.name}); {upgrade[7:]}"


def sessionstart(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    repo = Repo.discover(Path(cwd))
    if repo is None:
        return
    gl = GitLedger(repo)
    if not gl.joined():
        if gl.legacy_only():
            _fail_open("SessionStart", LEGACY_MESSAGE)
        elif _wired_but_broken(gl):
            _fail_open("SessionStart", f"this clone has a {BRANCH} branch it has not pinned "
                                       "(run `levain team join`, then `levain team doctor`)")
        return
    fetch_note = gl.fetch_if_due(0, timeout=10.0)
    try:
        sha, team, ledger = gl.snapshot()
        tree = gl.state_hash(ledger, team)
        canon_text = gl.read_canon(sha)
    except (R.RolesError, TeamError) as exc:
        _fail_open("SessionStart", str(exc))
        return
    live = ledger.in_force
    rulings = [e for e in live if e.get("kind") == "ruling"]
    newest = max((e.get("ts", "") for e in ledger.entries), default="")
    handle = gl.handle(team)
    lines = [f"[team] {I.oneline(team.project)}: {len(rulings)} ruling(s) and {len(live) - len(rulings)} other entr"
             f"{'y' if len(live) - len(rulings) == 1 else 'ies'} in force; newest entry "
             f"{I.age(newest) if newest else 'none'}; you are {handle or 'NOT a member (this machine key is not in force)'}."]
    from .cli_tenure import status_lines
    lines += [f"[team] {x}" for x in status_lines(gl)]
    lines.append(f"[team] {C.staleness(canon_text, tree)}. Canon: {gl.wt / 'PROJECT.md'} (or `levain team status`)")
    from .transport import WARNINGS
    # A fallback to an older team.toml (or any other read warning) must reach the session, not only the edit hook.
    lines += [f"[team] {w}" for w in WARNINGS]
    lines.append("[team] Edits to governed paths show the recorded decision first. When a person decides "
                 "something about this codebase, record it with their words: `levain team record --help`.")
    # LEVAIN_TEAM_SESSIONSTART_RULINGS=off: count + canon pointer only, so enforcement rests on the edit-time hook.
    show = os.environ.get("LEVAIN_TEAM_SESSIONSTART_RULINGS", "").lower() not in ("0", "off", "false", "no")
    for e in (rulings[:15] if show else []):
        # Every ledger-supplied string goes through oneline: a member writes these, and a line break in
        # them would draw a forged "[team]" line into every teammate's session context.
        where = I.oneline(", ".join(e.get("paths") or ["(project-wide)"]))
        words = I.oneline(e.get("words", ""))
        owner = I.oneline(str(e.get("owner")))
        lines.append(f"  - {where}: owner {owner}: \"{words[:160]}{'...' if len(words) > 160 else ''}\"")
    if show and len(rulings) > 15:
        lines.append(f"  - ...and {len(rulings) - 15} more in PROJECT.md / `levain team status`")
    if fetch_note:
        lines.append(f"[team] ledger not refreshed: {fetch_note} (showing the last fetched copy)")
    if ledger.problems:
        lines.append(f"[team] ledger integrity: {len(ledger.problems)} problem(s); run `levain team verify`")
    note = anneal_import(gl, ledger, tree, team.owner)
    if note:
        lines.append(note)
    text = "\n".join(I.oneline(line) for line in lines)   # every assembled line, whatever fed it
    _out({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    name = {"pretooluse": "PreToolUse", "sessionstart": "SessionStart"}.get(event)
    if name is None:
        print("usage: python -m levain.team.hook pretooluse|sessionstart", file=sys.stderr)
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
        (pretooluse if event == "pretooluse" else sessionstart)(payload)
    except Exception as exc:  # noqa: BLE001 - SessionStart fails open, visibly; PreToolUse is the GATE and halts
        if event == "pretooluse":
            _deny_unjudged(f"{type(exc).__name__}: {exc}")    # code L3 r1 codex MED: a crash must not allow
        else:
            _fail_open(name, f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
