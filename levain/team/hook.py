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
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import canon as C
from . import entry as E
from . import index as I
from . import roles as R
from .transport import BRANCH, DIRNAME, GitLedger, Repo, TeamError

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


def _tamper_text(ledger: I.Ledger, team: R.Team) -> str:
    shown = "; ".join(ledger.tamper[:3]) + (f"; and {len(ledger.tamper) - 3} more" if len(ledger.tamper) > 3 else "")
    return (f"{TAG} the team ledger is REFUSED as tampered: {shown}. Nothing in this ledger is trusted until each "
            f"reason is resolved (the team owner is {team.owner}). Do not work around this check.")


def _fail_open(event: str, reason: str) -> None:
    line = f"[team] ledger unavailable: {I.oneline(reason)}"
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
        return gl.base.is_dir()                       # git cannot answer; levain's own state says a ledger is here


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
    # Every repository above the target that holds levain team state judges the edit, not only the one git finds
    # first: a `.git` planted in a subdirectory (a fake nested repository, or an unreadable one) must not take the
    # edit out of the real clone's ledger. Any deny wins.
    roots = _ledger_roots(Path(target))
    answers = []
    for start in roots or [Path(target)]:
        out = _judge_from(start, target, payload, has_ledger=bool(roots))
        if out and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            _out(out)
            return
        if out:
            answers.append(out)
    if answers:
        _out(answers[0])


def _judge_from(start: Path, target: str, payload: dict, *, has_ledger: bool) -> dict | None:
    """The hook's answer for an edit of ``target``, judged in the repository found from ``start``. ``has_ledger``: the
    filesystem shows levain team state there, so anything that stops the judgement is a DENY."""
    try:
        repo = Repo.discover(start)
    except TeamError as exc:
        return _deny(f"the repository could not be read ({exc})") if has_ledger else None
    if repo is None:
        return _deny("the repository holding the team ledger could not be found") if has_ledger else None
    gl = GitLedger(repo)
    try:
        joined = gl.joined()
    except Exception as exc:  # noqa: BLE001 - an invalid state.json (a bad device id) in a clone with a ledger
        return _deny(f"this clone's levain team state could not be read ({type(exc).__name__}: {exc})")
    if not joined:
        if _wired_but_broken(gl):
            return _deny(f"this clone has a {BRANCH} branch but no usable ledger worktree or state (run `levain team "
                         "join`, then `levain team doctor`)")
        return None
    # Fetch first, then read team, ledger and identity together from the branch ref: one consistent snapshot,
    # no lock (the ref only moves when a rebase or commit completes).
    fetch_note = gl.fetch_if_due(_interval(gl), timeout=5.0)
    try:
        return _edit_verdict(gl, repo, target, payload, fetch_note)
    except Exception as exc:  # noqa: BLE001 - THE fail-closed boundary: a joined clone that cannot judge denies
        return _deny(f"the team ledger could not be read ({type(exc).__name__}: {exc})")


def _deny(why: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": I.oneline(
                                       f"{TAG} {why}; every edit is denied until `levain team doctor` is clean.")}}


def _ledger_roots(target: Path) -> list[Path]:
    """Every directory above ``target`` whose repository holds levain team state, nearest first, read from the
    filesystem alone (so it answers when git cannot): a ``.git`` directory, or a linked worktree's ``.git`` file and
    its common dir. A ``.git`` that cannot be read is passed over, never a reason to stop looking."""
    d = Path(os.path.abspath(target))
    out = []
    for p in (d, *d.parents):
        g = p / ".git"
        try:
            if g.is_dir():
                common = g
            elif g.is_file():
                gitdir = Path(g.read_text(encoding="utf-8", errors="replace").partition("gitdir:")[2].strip())
                gitdir = gitdir if gitdir.is_absolute() else p / gitdir
                common = gitdir
                if (gitdir / "commondir").is_file():
                    common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
            else:
                continue
            if (common / DIRNAME).is_dir():
                out.append(p)
        except OSError:
            continue
    return out


def _edit_verdict(gl: GitLedger, repo: Repo, target: str, payload: dict, fetch_note: str | None) -> dict | None:
    """The hook's answer for one edit in a joined clone. Any exception here is a DENY (``pretooluse``)."""
    _, team, ledger = gl.snapshot()
    rel = Path(os.path.realpath(target)).relative_to(os.path.realpath(repo.toplevel)).as_posix() \
        if _within(target, repo.toplevel) else None
    if rel is None:
        return None
    if ledger.tamper:
        # Fail closed: nothing in a refused ledger can be trusted to be the whole record.
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": I.oneline(_tamper_text(ledger, team))}}
    # Claude Code always sends session_id; the transcript path is a stable stand-in if a build ever does not.
    session = str(payload.get("session_id") or "")
    if not session and payload.get("transcript_path"):
        # a schema-safe token (entries allow [A-Za-z0-9._:@-]{1,128}), unique per transcript
        session = "t-" + hashlib.sha256(str(payload["transcript_path"]).encode("utf-8")).hexdigest()[:32]
    handle = gl.handle(team)
    d = decide(team, ledger, handle, rel, session, gl.session_denied(session) if session else set())
    from .transport import WARNINGS
    notes = [f"[team] {w}" for w in dict.fromkeys(WARNINGS)]
    if fetch_note:
        notes.append(f"[team] ledger not refreshed: {fetch_note} (showing the last fetched copy)")
    if ledger.problems:
        notes.append(f"[team] ledger integrity: {len(ledger.problems)} problem(s); run `levain team verify`")
    notes = [I.oneline(n) for n in notes]   # a warning or a fetch error can carry git or ledger text
    if d is None:
        if notes:
            return {"systemMessage": notes[0],
                    "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(notes)}}
        return None
    text = d.text + ("\n\n" + "\n".join(notes) if notes else "")
    if d.deny:
        if session and d.newly_denied:
            try:
                gl.mark_denied(session, d.newly_denied)
            except OSError as exc:
                # Not remembering the deny means the next attempt is denied again: never a reason to allow.
                text += f"\n\n[team] could not record this denial ({I.oneline(str(exc))}); a retry will be denied again"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": text}}
    if d.ack and handle:
        try:
            gl.append(E.build(handle, "ack", refs=d.ack, session=session, agent="claude-code",
                              summary=f"proceeded with an edit of {rel} after the ruling was shown"),
                      push=False, lock_timeout=3.0)
        except (TeamError, E.EntryError) as exc:
            text += f"\n\n[team] acknowledgement not recorded: {I.oneline(str(exc))}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def _anneal_db(gl: GitLedger) -> Path | None:
    configured = gl.state().get("anneal_db")
    if isinstance(configured, str) and configured:
        p = Path(configured).expanduser()
        return p if p.exists() else None
    p = gl.repo.toplevel / ".levain" / "memory.db"
    return p if p.exists() else None


def anneal_import(gl: GitLedger, ledger: I.Ledger, tree: str, owner: str) -> str | None:  # tree: state hash
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
    from .export import export_stream

    body = "".join(export_stream(ledger))
    try:
        # --link-authority: only the owner's supersedes cross authors, the same rule levain's in-force view applies
        cp = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db", str(db), "team-import",
                             "--link-authority", owner, "-"],
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


_SESSIONSTART_BUDGET = 20.0   # seconds, the session start's whole network allowance (fetch plus the ack flush)


def sessionstart(payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    roots = _ledger_roots(Path(cwd))
    try:
        repo = Repo.discover(roots[0] if roots else Path(cwd))
    except TeamError as exc:
        if roots:                                     # silent where no ledger lives; said where one does
            _fail_open("SessionStart", str(exc))
        return
    if repo is None:
        return
    gl = GitLedger(repo)
    try:
        joined = gl.joined()
    except Exception as exc:  # noqa: BLE001 - an invalid state.json: said, and every edit is denied by PreToolUse
        _fail_open("SessionStart", f"this clone's levain team state could not be read ({type(exc).__name__}: {exc}); "
                                   "every edit is denied until `levain team doctor` is clean")
        return
    if not joined:
        if _wired_but_broken(gl):
            _fail_open("SessionStart", f"this clone has a {BRANCH} branch but no usable ledger worktree "
                                       "(run `levain team join`, then `levain team doctor`)")
        return
    started = time.monotonic()
    fetch_note = gl.fetch_if_due(0, timeout=10.0)
    try:
        sha, team, ledger = gl.snapshot()
        if ledger.tamper:
            # Only the refusal: nothing of a refused ledger (rulings, counts, words) reaches the agent or its memory.
            _out({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                  I.oneline(_tamper_text(ledger, team)) + " Every edit in this clone is denied until then."}})
            return
        tree = gl.state_hash(ledger, team)
        canon_text = gl.read_canon(sha)
    except (R.RolesError, TeamError) as exc:
        _fail_open("SessionStart", str(exc))
        return
    # Acknowledgements are committed without a push; a session start sends them, within a bound, so an ack with no
    # later ledger write still reaches the team. What cannot be sent is said.
    # One budget for the whole hook: the flush gets what the fetch left of _SESSIONSTART_BUDGET, and none when the
    # fetch already failed (the remote is not answering).
    left = _SESSIONSTART_BUDGET - (time.monotonic() - started)
    push_note = gl.flush_unpushed(timeout=left / 3) if left > 3 and not fetch_note else (fetch_note or None)
    pending = 0
    if push_note:
        try:
            pending = gl.unpushed() or 0
        except TeamError:
            pending = 0
    live = ledger.in_force
    rulings = [e for e in live if e.get("kind") == "ruling"]
    newest = max((e.get("ts", "") for e in ledger.entries), default="")
    handle = gl.handle(team)
    lines = [f"[team] {I.oneline(team.project)}: {len(rulings)} ruling(s) and {len(live) - len(rulings)} other entr"
             f"{'y' if len(live) - len(rulings) == 1 else 'ies'} in force; newest entry "
             f"{I.age(newest) if newest else 'none'}; you are {handle or 'NOT a member (git user.email unmapped)'}."]
    lines.append(f"[team] {C.staleness(canon_text, tree)}. Canon: {gl.wt / 'PROJECT.md'} (or `levain team status`)")
    from .transport import WARNINGS
    # A fallback to an older team.toml (or any other read warning) must reach the session, not only the edit hook.
    lines += [f"[team] {w}" for w in dict.fromkeys(WARNINGS)]
    if pending:
        lines.append(f"[team] {pending} local ledger commit(s), acknowledgements included, not pushed yet "
                     f"({I.oneline(push_note)}); run `levain team sync`")
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
    except Exception as exc:  # noqa: BLE001 - fail open, visibly
        _fail_open(name, f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
