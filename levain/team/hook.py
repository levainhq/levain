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
from .transport import BRANCH, DIRNAME, GitLedger, Repo, TeamBusy, TeamError

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


FOLD_CASE = sys.platform == "darwin"


def decide(team: R.Team, ledger: I.Ledger, handle: str | None, rel: str, session: str,
           denied_before: set[str], *, fold: bool = FOLD_CASE) -> Decision | None:
    """What the hook does for one edit of ``rel``. Pure: no I/O. None = nothing applies."""
    matched = ledger.applying_to(rel, fold=fold)
    if not matched:
        return None
    tensions = [e for e in matched if e.get("type") == "tension"]
    foreign = [e for e in matched if e.get("kind") == "ruling" and not team.owns(handle, e.get("owner", ""))]
    always = tensions + [e for e in foreign if _mode(e, team) == "block"]
    once = [e for e in foreign if _mode(e, team) == "ask-once"]
    acked = ledger.acked(session) if session else set()
    seen = denied_before | acked
    pending = [e for e in once if e["id"] not in seen]
    owners = sorted({e["owner"] for e in tensions + foreign if e.get("owner")})
    blocks = "\n\n".join(I.render(e) for e in matched)
    head = (f"{TAG} {rel} is governed by {len(matched)} recorded entr{'y' if len(matched) == 1 else 'ies'} in "
            f"the {team.project} team ledger.")
    if always or pending:
        how = []
        if pending:
            retry = ("If your change stays within the ruling(s) above, retry the same edit: the retry is allowed and "
                     "recorded as your acknowledgement. " if not always else "")
            how.append(f"{retry}If your change would contradict a ruling, STOP and ask the owner of the call "
                       f"({', '.join(owners) or 'the owner'}) before changing anything; do not route around this "
                       "check (no Bash, sed, heredoc or copy edits of this path). If the owner changes the call, "
                       "record their words as a new ruling (`levain team record decision --kind ruling --owner ... "
                       f"--words ... --refs <old id>`); the team owner ({team.owner}) or the ruling's author then "
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
        text = f"{head} Read it before changing this file.\n\n{blocks}\n\nWhat to do: " + " ".join(how)
        return Decision(deny=True, text=text, newly_denied={e["id"] for e in pending})
    acks = [e["id"] for e in once if e["id"] in denied_before and e["id"] not in acked]
    lead = "Proceeding is allowed; stay within these." if foreign else "For your information:"
    return Decision(deny=False, text=f"{head} {lead}\n\n{blocks}", ack=acks)


# ---- I/O -----------------------------------------------------------------------------------------------------

def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _fail_open(event: str, reason: str) -> None:
    line = f"[team] ledger unavailable: {reason}"
    _out({"systemMessage": line,
          "hookSpecificOutput": {"hookEventName": event, "additionalContext": line}})


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
        if parent.name == DIRNAME and (parent.parent / "HEAD").is_file() and (parent.parent / "objects").is_dir():
            return True
    return False


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
            "permissionDecisionReason": (f"{TAG} {target} is inside the team ledger's private machinery. The "
                                         "ledger is written only through `levain team record` (it validates, "
                                         "hash-chains and attributes every entry); do not edit it directly.")}})
        return
    repo = Repo.discover(Path(target))
    if repo is None:
        return
    gl = GitLedger(repo)
    if not gl.joined():
        if _wired_but_broken(gl):
            _fail_open("PreToolUse", f"this clone has a {BRANCH} branch but no usable ledger worktree "
                                     "(run `levain team join`, then `levain team doctor`)")
        return
    try:
        team = gl.team()
    except R.RolesError as exc:
        _fail_open("PreToolUse", str(exc))
        return
    fetch_note = gl.fetch_if_due(team.fetch_interval, timeout=5.0)
    busy_note = None
    try:
        with gl.lock(exclusive=False, timeout=10.0):
            ledger = gl.ledger()
    except TeamBusy:
        # The worktree lock covers only local commits and rebases, so this is rare; reading unlocked can at worst
        # see a file mid-rewrite, which verification reports. Enforcing from that beats enforcing nothing.
        ledger = gl.ledger()
        busy_note = "[team] read the ledger without its lock (another operation held it for 10 s)"
    rel = Path(os.path.realpath(target)).relative_to(os.path.realpath(repo.toplevel)).as_posix() \
        if _within(target, repo.toplevel) else None
    if rel is None:
        return
    session = str(payload.get("session_id") or "")
    handle = gl.handle(team)
    d = decide(team, ledger, handle, rel, session, gl.session_denied(session) if session else set())
    from .transport import WARNINGS
    notes = [f"[team] {w}" for w in WARNINGS] + ([busy_note] if busy_note else [])
    if fetch_note:
        notes.append(f"[team] ledger not refreshed: {fetch_note} (showing the last fetched copy)")
    if ledger.problems:
        notes.append(f"[team] ledger integrity: {len(ledger.problems)} problem(s); run `levain team verify`")
    if d is None:
        if notes:
            _out({"systemMessage": notes[0],
                  "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(notes)}})
        return
    text = d.text + ("\n\n" + "\n".join(notes) if notes else "")
    if d.deny:
        if session and d.newly_denied:
            gl.mark_denied(session, d.newly_denied)
        _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": text}})
        return
    if d.ack and handle:
        try:
            gl.append(E.build(handle, "ack", refs=d.ack, session=session, agent="claude-code",
                              summary=f"proceeded with an edit of {rel} after the ruling was shown"),
                      push=False, lock_timeout=3.0)
        except (TeamError, E.EntryError) as exc:
            text += f"\n\n[team] acknowledgement not recorded: {exc}"
    _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})


def _anneal_db(gl: GitLedger) -> Path | None:
    configured = gl.state().get("anneal_db")
    if isinstance(configured, str) and configured:
        p = Path(configured).expanduser()
        return p if p.exists() else None
    p = gl.repo.toplevel / ".levain" / "memory.db"
    return p if p.exists() else None


def anneal_import(gl: GitLedger, ledger: I.Ledger, tree: str, owner: str) -> str | None:
    """Optional seam: feed every ledger entry to anneal's team import, once per ledger tree.

    Runs only when the installed anneal-memory ships ``anneal_memory.team`` and a store is known.
    Returns a one-line note when it ran or failed, None when it did not apply.
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
    if db is None or gl.state().get("anneal_imported_tree") == tree:
        return None
    from .export import export_lines

    body = "".join(export_lines(ledger))
    try:
        # --link-authority: only the owner's supersedes cross authors, the same rule levain's in-force view applies
        cp = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "team-import", "--db", str(db), "-",
                             "--link-authority", owner],
                            input=body, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[team] anneal import did not run: {exc}"
    tail = ((cp.stderr or cp.stdout).strip().splitlines() or ["exit " + str(cp.returncode)])[-1]
    if cp.returncode == 3:
        # anneal imported what verified and refused the rest; not a crash, but a person should see it
        gl.save_state(anneal_imported_tree=tree, anneal_last=f"partial: {tail}")
        return f"[team] anneal import refused some entries: {tail}"
    if cp.returncode != 0:
        gl.save_state(anneal_last=f"failed: {tail}")
        return f"[team] anneal import failed: {tail}"
    gl.save_state(anneal_imported_tree=tree, anneal_last="ok")
    return f"[team] ledger imported into your memory store ({db.name})"


def sessionstart(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    repo = Repo.discover(Path(cwd))
    if repo is None:
        return
    gl = GitLedger(repo)
    if not gl.joined():
        if _wired_but_broken(gl):
            _fail_open("SessionStart", f"this clone has a {BRANCH} branch but no usable ledger worktree "
                                       "(run `levain team join`, then `levain team doctor`)")
        return
    try:
        team = gl.team()
    except R.RolesError as exc:
        _fail_open("SessionStart", str(exc))
        return
    fetch_note = gl.fetch_if_due(0, timeout=10.0)
    try:
        with gl.lock(exclusive=False, timeout=10.0):
            ledger = gl.ledger()
            tree = gl.ledger_tree()
            canon_text = gl.read_canon()
    except TeamError as exc:
        _fail_open("SessionStart", str(exc))
        return
    live = ledger.in_force
    rulings = [e for e in live if e.get("kind") == "ruling"]
    newest = max((e.get("ts", "") for e in ledger.entries), default="")
    handle = gl.handle(team)
    lines = [f"[team] {team.project}: {len(rulings)} ruling(s) and {len(live) - len(rulings)} other entr"
             f"{'y' if len(live) - len(rulings) == 1 else 'ies'} in force; newest entry "
             f"{I.age(newest) if newest else 'none'}; you are {handle or 'NOT a member (git user.email unmapped)'}."]
    lines.append(f"[team] {C.staleness(canon_text, ledger, tree)}. Canon: {gl.wt / 'PROJECT.md'}")
    lines.append("[team] Edits to governed paths show the recorded decision first. When a person decides "
                 "something about this codebase, record it with their words: `levain team record --help`.")
    # LEVAIN_TEAM_SESSIONSTART_RULINGS=off: count + canon pointer only, so enforcement rests on the edit-time hook.
    show = os.environ.get("LEVAIN_TEAM_SESSIONSTART_RULINGS", "").lower() not in ("0", "off", "false", "no")
    for e in (rulings[:15] if show else []):
        where = ", ".join(e.get("paths") or ["(project-wide)"])
        words = e.get("words", "")
        lines.append(f"  - {where}: owner {e.get('owner')}: \"{words[:160]}{'...' if len(words) > 160 else ''}\"")
    if show and len(rulings) > 15:
        lines.append(f"  - ...and {len(rulings) - 15} more in PROJECT.md / `levain team status`")
    if fetch_note:
        lines.append(f"[team] ledger not refreshed: {fetch_note} (showing the last fetched copy)")
    if ledger.problems:
        lines.append(f"[team] ledger integrity: {len(ledger.problems)} problem(s); run `levain team verify`")
    note = anneal_import(gl, ledger, tree, team.owner)
    if note:
        lines.append(note)
    text = "\n".join(lines)
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
    except Exception as exc:  # noqa: BLE001 - fail open, visibly
        _fail_open(name, f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
