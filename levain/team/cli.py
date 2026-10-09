"""``levain team ...``: the one write path to the ledger, and the read/check tools around it."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import canon as C
from . import entry as E
from . import index as I
from . import pack as P
from . import roles as R
from . import wire as W
from .export import export_stream
from .hook import decide
from .transport import GitLedger, InitIncomplete, JoinIncomplete, Repo, TeamError


def _repo(args) -> Repo:
    repo = Repo.discover(Path(args.repo or os.getcwd()))
    if repo is None:
        raise TeamError(f"{args.repo or os.getcwd()} is not inside a git repository")
    return repo


def _split(values: list[str] | None) -> list[str]:
    out = []
    for v in values or []:
        out += [p.strip() for p in v.split(",") if p.strip()]
    return out


def _repo_paths(repo: Repo, paths: list[str]) -> list[str]:
    """Paths and globs are taken relative to the current directory, like a git pathspec, and stored
    repo-relative. An existing directory governs its whole tree (stored with a trailing "/")."""
    out = []
    top = Path(os.path.realpath(repo.toplevel))
    try:
        here = Path(os.path.realpath(os.getcwd())).relative_to(top)
    except ValueError:
        here = Path(".")  # running outside the repo with --repo: paths are repo-relative as written
    for p in paths:
        if os.path.isabs(p):
            try:
                rel = Path(os.path.realpath(p)).relative_to(top).as_posix()
            except ValueError:
                raise TeamError(f"{p} is outside the repository {top}") from None
        else:
            rel = (here / p).as_posix() if str(here) != "." else p
            while rel.startswith("./"):
                rel = rel[2:]
        if rel in ("", ".", "/"):
            out.append("**")  # the repository root: everything
            continue
        if not rel.endswith("/") and not any(c in rel for c in "*?") and (top / rel).is_dir():
            rel += "/"
        out.append(rel)
    return out


def _members(values: list[str] | None) -> tuple[dict[str, str], dict[str, str]]:
    """``handle=email[=public key line or .pub file]`` -> (handle->email, handle->key line)."""
    out, keys = {}, {}
    for v in values or []:
        if "=" not in v:
            raise TeamError(f"--member takes handle=email[=pubkey], got {v!r}")
        parts = v.split("=", 2)
        out[parts[0].strip()] = parts[1].strip()
        if len(parts) == 3 and parts[2].strip():
            from .cli_tenure import _key_line
            keys[parts[0].strip()] = _key_line(parts[2].strip())
    return out, keys


def cmd_init(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    members, keys = _members(args.member)
    team = R.Team(project=args.project or repo.toplevel.name, owner=args.owner, members=members,
                  client_owners=_split(args.client_owner), mode=args.mode, fetch_interval=args.fetch_interval)
    incomplete = None
    try:
        print(gl.init(team, member_keys=keys, remote=args.remote, push=not args.no_push, signing_key=args.signing_key,
                      replace_legacy=args.replace_legacy))
    except InitIncomplete as exc:
        incomplete = exc        # initialised: the clone's own setup below still runs, then the command fails
    try:
        if args.anneal_db:
            gl.save_state(anneal_db=str(Path(args.anneal_db).expanduser().resolve()))
        if args.pack and incomplete is None:
            written = P.seed(gl, Path(args.pack), push=not args.no_push)
            print(f"seeded {len(written)} pack rule entr{'y' if len(written) == 1 else 'ies'} from {args.pack}")
        if not args.no_install:
            _install_all(repo)
    except Exception as exc:
        if incomplete is not None:
            raise incomplete from exc       # what is still pending is the message a person must read (as join)
        raise
    if incomplete is not None:
        raise incomplete
    return 0


def cmd_join(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    accepted = {}
    for v in args.accept_merge or []:
        sha, _, n = v.partition(":")
        if not n.isdigit():
            raise TeamError(f"--accept-merge takes SHA:PARENT, got {v!r}: name the parent the owner chose (a trust "
                            "decision, never a default)")
        accepted[sha] = int(n)
    incomplete = None
    try:
        print(gl.join(remote=args.remote, new_device=args.new_device, root=args.root, signing_key=args.signing_key,
                      accept_merges=accepted))
    except JoinIncomplete as exc:
        incomplete = exc        # joined: the clone's own setup below still runs, then the command fails
    try:
        if args.anneal_db:
            gl.save_state(anneal_db=str(Path(args.anneal_db).expanduser().resolve()))
        if not args.no_install:
            _install_all(repo)
    except Exception as exc:
        # the pending-steps message is the one a person must read: a setup failure is chained to it, never in its
        # place (code L3 r5 complement 7)
        if incomplete is not None:
            raise incomplete from exc
        raise
    if incomplete is not None:
        raise incomplete
    return 0


def _actor(gl: GitLedger) -> tuple[R.Team, str]:
    team = gl.team()
    handle = gl.handle(team)
    if handle is None:
        raise TeamError(f"this machine's signing key is not a member's key in force in {team.project}: from one of "
                        "your machines whose key is in force, run `levain team key add <your handle> <this machine's "
                        f"public key>`; if you have none yet, {team.owner} proposes your first key (`levain team key add "
                        "<your handle> <your public key>`); then `levain team sync` here")
    return team, handle


def cmd_record(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    gl.require_joined()
    team, handle = _actor(gl)
    words = args.words
    if args.words_file:
        words = Path(args.words_file).read_text(encoding="utf-8").strip()
    owner = args.owner
    if args.kind == "ruling" and not owner:
        raise TeamError("a ruling needs --owner (client:<name>, lead, or a member handle)")
    if owner and not team.owner_ok(owner):
        raise TeamError(f"owner {owner!r} is not allowed by team.toml (members, lead, client:<client_owners>)")
    e = E.build(handle, args.type, kind=args.kind, mode=args.mode, paths=_repo_paths(repo, _split(args.paths)),
                owner=owner, words=words, summary=args.summary, reason=args.reason, recheck=args.recheck,
                supersedes=_split(args.supersedes), refs=_split(args.refs),
                agent=args.agent or os.environ.get("LEVAIN_TEAM_AGENT") or "human",
                session=args.session or os.environ.get("CLAUDE_SESSION_ID"))
    sealed = gl.append(e, push=not args.no_push)
    if args.json:
        print(json.dumps(sealed, ensure_ascii=False))
    else:
        print(f"recorded {sealed['id']}")
        print(I.render(sealed))
    return 0


def cmd_retire(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    gl.require_joined()
    team, handle = _actor(gl)
    if args.owner and not team.owner_ok(args.owner):
        raise TeamError(f"owner {args.owner!r} is not allowed by team.toml (members, lead, client:<client_owners>)")
    e = E.build(handle, "retire", supersedes=_split(args.ids), words=args.words, reason=args.reason,
                owner=args.owner, agent=args.agent or "human")
    sealed = gl.append(e, push=not args.no_push)
    print(f"recorded {sealed['id']} (retires {', '.join(sealed['supersedes'])})")
    return 0


def cmd_sync(args) -> int:
    gl = GitLedger(_repo(args))
    print(gl.sync(push=not args.no_push))
    return 0


def cmd_status(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    gl.require_joined()
    sha, team, ledger = gl.snapshot()
    handle = gl.handle(team)
    state = gl.state_hash(ledger, team)
    canon_text = gl.read_canon(sha)
    if args.path:
        rel = _repo_paths(repo, [args.path])[0]
        d = decide(team, ledger, handle, rel, "", set())
        if args.json:
            print(json.dumps({"path": rel, "deny": bool(d and d.deny), "text": d.text if d else "",
                              "entries": [e["id"] for e in ledger.applying_to(rel)]}))
        else:
            print(d.text if d else f"nothing in the ledger governs {rel}")
            if d:
                print(f"\n(an agent's first edit here would be {'DENIED with this record' if d.deny else 'allowed, with this shown'})")
        return 0
    if args.json:
        print(json.dumps({"project": team.project, "you": handle, "in_force": ledger.in_force,
                          "problems": ledger.problems, "canon": C.staleness(canon_text, state)},
                         ensure_ascii=False))
        return 0
    print(f"{team.project}: owner {team.owner}, you are {handle or 'NOT a member'}, mode {team.mode}")
    from .cli_tenure import status_lines
    for line in status_lines(gl):
        print(line)
    print(C.staleness(canon_text, state))
    if ledger.problems:
        print(f"{len(ledger.problems)} integrity problem(s): run `levain team verify`")
    for e in ledger.in_force:
        print()
        print(I.render(e))
    if not ledger.in_force:
        print("nothing in force yet")
    return 0


def cmd_verify(args) -> int:
    gl = GitLedger(_repo(args))
    gl.require_joined()
    sha, team, ledger = gl.snapshot()
    canon_text = gl.read_canon(sha)
    problems = list(ledger.problems) + gl.team_history_problems(team, sha)
    for e in ledger.entries:
        for s in e.get("supersedes", []) + e.get("refs", []):
            if s not in ledger.by_id:
                problems.append(f"{e['id']}: names {s}, which is not in the ledger")
        if e.get("owner") and not team.owner_ok(e["owner"]):
            problems.append(f"{e['id']}: owner {e['owner']!r} is not allowed by team.toml")
    files = len(ledger.files)
    print(f"{len(ledger.entries)} entries in {files} file(s); {len(ledger.in_force)} in force")
    print(C.staleness(canon_text, gl.state_hash(ledger, team)))
    for p in problems:
        print(f"PROBLEM: {p}")
    print("ledger verified: every chain intact, every line written by the member it is filed under"
          if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def cmd_consolidate(args) -> int:
    gl = GitLedger(_repo(args))
    gl.require_joined()
    team, handle = _actor(gl)
    if handle != team.owner:
        raise TeamError(f"only the canon owner ({team.owner}) consolidates; you are {handle}. "
                        "(This is the team's convention plus a git user.email check, not cryptography.)")
    if gl.remote and not args.no_push:
        gl.sync(push=False)
    ledger = gl.ledger(team)
    text = C.render(team, ledger, tree=gl.state_hash(ledger, team), by=handle, ts=E.now_iso())
    if args.dry_run:
        print(text, end="")
        return 0
    print(gl.write_canon(text, push=not args.no_push))
    print(f"PROJECT.md: {gl.wt / 'PROJECT.md'}")
    return 0


def cmd_export(args) -> int:
    gl = GitLedger(_repo(args))
    gl.require_joined()
    if args.in_force:
        sys.stdout.writelines(export_stream(gl.ledger(), in_force=True))
        return 0
    from .export import export_v3
    sys.stdout.writelines(export_v3(gl))   # contract v3, strict profile (seam doc §3a item 3)
    return 0


def _port(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = -1
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be a number from 0 to 65535, got {text!r}")
    return n


def cmd_view(args) -> int:
    from .view import serve
    gl = GitLedger(_repo(args))
    gl.require_joined()
    return serve(gl, host=args.host, port=args.port, recheck_days=args.recheck_days, ack_flag=args.ack_flag,
                 cockpit_url=args.cockpit_url)


def _install_all(repo: Repo, python: str | None = None) -> None:
    for line in W.install_all(repo, python=python):
        print(line)


def cmd_install(args) -> int:
    _install_all(_repo(args), python=args.python)
    return 0


def cmd_member_add(args) -> int:
    gl = GitLedger(_repo(args))
    gl.require_joined()
    team, handle = _actor(gl)
    if handle != team.owner:
        raise TeamError(f"only the owner ({team.owner}) changes membership")
    from .cli_tenure import _key_line
    line = _key_line(args.key) if args.key else None
    d = gl.derivation()
    if d.retired(args.handle):
        raise TeamError(f"{args.handle} was a member of this ledger and was removed; a handle is never re-used, so "
                        "invite them under a new handle")
    if line and args.handle in d.ever_keyed:
        raise TeamError(f"{args.handle} already has had a key in force, so only {args.handle} proposes their keys "
                        f"(`levain team key add {args.handle} <public key>` from a machine whose key is in force)")

    def add(t, ten) -> None:
        t.members[args.handle] = args.email
        if line:
            ten.pending_keys.setdefault(args.handle, []).append(line)
    print(gl.update_counted(add, f"levain team: add member {args.handle}", push=not args.no_push))
    if not line:
        print(f"{args.handle} has no key yet: their lines are not enforced until `levain team key add {args.handle} "
              "<their public key>` (the owner may propose a member's first key) and their machine confirms it")
    return 0


def cmd_pack_sync(args) -> int:
    gl = GitLedger(_repo(args))
    gl.require_joined()
    written = P.seed(gl, Path(args.pack_dir), push=not args.no_push)
    print(f"{len(written)} pack entr{'y' if len(written) == 1 else 'ies'} written"
          + ("" if written else " (pack rules already current)"))
    return 0


def cmd_doctor(args) -> int:
    repo = _repo(args)
    gl = GitLedger(repo)
    rows: list[tuple[bool, str]] = []
    if not gl.joined():
        print(f"FAIL  this clone has not joined a team ledger ({repo.toplevel})")
        return 1
    rows.append((True, f"ledger worktree {gl.wt} (device {gl.device})"))
    try:
        team = gl.team()
        rows.append((True, f"team.toml valid: {team.project}, owner {team.owner}, mode {team.mode}"))
        handle = gl.handle(team)
        rows.append((handle is not None, f"you are {handle}" if handle else
                     f"git user.email {gl.email() or 'unset'} maps to no member"))
    except R.RolesError as exc:
        rows.append((False, str(exc)))
    ledger = gl.ledger()
    rows.append((not ledger.problems, f"{len(ledger.entries)} entries, chains intact" if not ledger.problems
                 else f"{len(ledger.problems)} integrity problem(s): run `levain team verify`"))
    st = gl.state()
    if gl.remote:
        err = st.get("last_fetch_error")
        rows.append((not err, f"remote {gl.remote}: last fetch "
                     + (I.age(_iso(st.get('last_fetch_ok'))) if st.get("last_fetch_ok") else "never")
                     + (f"; last error: {err}" if err else "")))
        ahead = subprocess.run(["git", "log", "--format=%s", f"refs/remotes/{gl.remote}/{gl.branch}..HEAD"],
                               cwd=gl.wt, capture_output=True, text=True)
        subjects = ahead.stdout.splitlines() if ahead.returncode == 0 else ["?"]
        real = [s for s in subjects if not s.startswith("levain team: ack ")]
        rows.append((not real, f"{len(subjects)} local ledger commit(s) not pushed"
                     + ("" if not subjects else (": run `levain team sync`" if real
                        else " (acknowledgements only; they go out with the next write)"))))
    else:
        rows.append((True, "no remote: ledger is local only"))
    rows += W.check(repo)
    py_ok, py_line = _hook_import_check(repo)
    rows.append((py_ok, py_line))
    rows.append(_anneal_row(gl))
    bad = 0
    for ok, line in rows:
        print(f"{'ok  ' if ok else 'FAIL'}  {line}")
        bad += not ok
    print("All team checks passed." if not bad else f"{bad} check(s) failed.")
    return 0 if not bad else 1


def _iso(ts) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _hook_import_check(repo: Repo) -> tuple[bool, str]:
    import shlex
    try:
        data = json.loads((repo.toplevel / W.SETTINGS_REL).read_text(encoding="utf-8"))
        cmds = [h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"] if W.MARK in h["command"]]
        py = shlex.split(cmds[0])[0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False, "cannot read the wired PreToolUse command"
    cp = subprocess.run([py, "-P", "-c", "import levain.team.hook"], capture_output=True, text=True,
                        cwd=str(repo.toplevel), timeout=30)
    if cp.returncode != 0:
        return False, f"the wired interpreter cannot import levain.team.hook: {cp.stderr.strip()[-200:]}"
    return True, "the wired interpreter imports levain.team.hook"


def _anneal_row(gl: GitLedger) -> tuple[bool, str]:
    import importlib.util
    from .hook import _anneal_db
    try:
        has = importlib.util.find_spec("anneal_memory.team") is not None
    except (ImportError, ValueError):
        has = False
    db = _anneal_db(gl)
    if not has:
        return True, "anneal import: not available in this anneal-memory (ledger reaches agents via hooks + PROJECT.md only)"
    if db is None:
        return True, "anneal import: available, but no store configured (`levain team join --anneal-db PATH`)"
    last = gl.state().get("anneal_last")
    return True, f"anneal import: ON into {db}" + (f" (last run: {last})" if last else " (not run yet)")


def register(subparsers) -> None:
    team_p = subparsers.add_parser(
        "team", help="Share one project's decisions across a team of engineers (git ledger + edit-time hook).",
        description="Team context: an append-only, hash-chained decision ledger on a `levain-team-ledger` branch of "
                    "the project's repo, a Claude Code PreToolUse hook that shows a recorded decision at the "
                    "edit it governs, and an owner-generated PROJECT.md canon.")
    sub = team_p.add_subparsers(dest="team_command", metavar="<team command>", required=True)

    def add(name, func, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("--repo", help="a path inside the project's git repository (default: cwd)")
        p.set_defaults(func=_guarded(func) if func else None)
        return p

    p = add("init", cmd_init, "Create the team ledger in this repository (first engineer; becomes the remote branch).")
    p.add_argument("--project")
    p.add_argument("--owner", required=True, help="handle of the canon owner (must be a --member)")
    p.add_argument("--member", action="append", required=True, metavar="HANDLE=EMAIL[=PUBKEY]",
                   help="repeatable; PUBKEY (a .pub file or key line) is the member's signing key, pending until "
                        "their machine confirms it at join")
    p.add_argument("--signing-key", help="your ssh public key file (in ssh-agent, or without a passphrase)")
    p.add_argument("--replace-legacy", action="store_true",
                   help="create a strict ledger beside a 0.6.x levain-ledger (which is never read or converted)")
    p.add_argument("--client-owner", action="append", metavar="NAME",
                   help="names allowed as client:<NAME> owners (repeatable or comma-separated)")
    p.add_argument("--mode", default="ask-once", choices=E.MODES)
    p.add_argument("--fetch-interval", type=int, default=300, help="seconds between hook fetches (default 300)")
    p.add_argument("--remote")
    p.add_argument("--pack", help="a pack directory whose judgment.toml is seeded into the ledger")
    p.add_argument("--anneal-db", help="your anneal store, for the optional import at session start")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--no-install", action="store_true", help="do not wire the hooks into .claude/settings.local.json")

    p = add("join", cmd_join, "Join this clone to the team ledger already on the remote.")
    p.add_argument("--remote")
    p.add_argument("--root", help="the genesis of the ledger to trust (required when the remote has several)")
    p.add_argument("--signing-key", help="your ssh public key file (in ssh-agent, or without a passphrase)")
    p.add_argument("--accept-merge", action="append", metavar="SHA:PARENT",
                   help="pin through a merge the owner named, following that parent")
    p.add_argument("--new-device", action="store_true",
                   help="give this clone its own device id (after copying a .git directory from another machine)")
    p.add_argument("--anneal-db")
    p.add_argument("--no-install", action="store_true")

    p = add("record", cmd_record, "Record a decision, constraint, finding, question or tension (the one write path).")
    p.add_argument("type", choices=[t for t in E.TYPES if t not in ("ack", "retire")])
    p.add_argument("--kind", choices=E.KINDS, help="ruling (decided by an owner, binds) or practice")
    p.add_argument("--mode", choices=E.MODES, help="per-ruling override of the team mode")
    p.add_argument("--paths", action="append", metavar="GLOB", help="repo-relative globs it governs (repeatable)")
    p.add_argument("--owner", help="client:<name>, lead, or a member handle")
    p.add_argument("--words", help="the decider's own words, verbatim (required for a ruling)")
    p.add_argument("--words-file")
    p.add_argument("--summary")
    p.add_argument("--reason")
    p.add_argument("--recheck", help="how to re-derive or verify the claim")
    p.add_argument("--supersedes", action="append", metavar="ID")
    p.add_argument("--refs", action="append", metavar="ID")
    p.add_argument("--agent", help="what drafted it (default $LEVAIN_TEAM_AGENT or 'human')")
    p.add_argument("--session")
    p.add_argument("--no-push", action="store_true",
                   help="keep it local for now (by default the entry is pushed so the team sees it immediately)")
    p.add_argument("--json", action="store_true")

    p = add("retire", cmd_retire, "Retire entries without replacing them (a ruling needs the decider's words).")
    p.add_argument("ids", nargs="+")
    p.add_argument("--words")
    p.add_argument("--owner")
    p.add_argument("--reason")
    p.add_argument("--agent")
    p.add_argument("--no-push", action="store_true")

    p = add("sync", cmd_sync, "Fetch, rebase and push the ledger.")
    p.add_argument("--no-push", action="store_true")

    p = add("status", cmd_status, "What is in force (or, with --path, what governs one file).")
    p.add_argument("--path")
    p.add_argument("--json", action="store_true")

    add("verify", cmd_verify, "Walk every file's hash chain and every reference; exit 1 on any problem.")

    p = add("consolidate", cmd_consolidate, "Owner only: regenerate PROJECT.md from the in-force entries.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-push", action="store_true")

    p = add("export", cmd_export, "Print the ledger as JSON lines (all entries per file in file order).")
    p.add_argument("--jsonl", action="store_true", help="JSON lines (the only format; the flag is accepted for clarity)")
    p.add_argument("--in-force", action="store_true", help="only in-force entries, time order (not chain-verifiable)")

    p = add("view", cmd_view, "Serve the read-only team view (four panes) on localhost.")
    p.add_argument("--dir", dest="repo", help="same as --repo")
    p.add_argument("--host", default="127.0.0.1", help="loopback only (default 127.0.0.1)")
    p.add_argument("--port", type=_port, default=7450, help="0..65535; 0 picks an ephemeral port (default 7450)")
    p.add_argument("--recheck-days", type=int, default=30, help="a recheck older than this is overdue (default 30)")
    p.add_argument("--cockpit-url", default="http://127.0.0.1:7420/", help="where the nav's Cockpit link points")
    p.add_argument("--ack-flag", type=int, default=3, help="acks on one path before it is flagged for review (default 3)")

    p = add("install", cmd_install, "Wire the team hooks into .claude/settings.local.json (idempotent).")
    p.add_argument("--python", help="interpreter for the hook command (default: this one)")

    add("doctor", cmd_doctor, "Check the ledger, the identity mapping, the remote, the hooks and the anneal seam.")

    p = add("member", None, "Owner only: change team membership.")
    msub = p.add_subparsers(dest="member_command", required=True)
    ma = msub.add_parser("add", help="add a member")
    ma.add_argument("handle")
    ma.add_argument("email")
    ma.add_argument("--key", help="their signing public key (a .pub file or key line); pending until they confirm")
    ma.add_argument("--repo")
    ma.add_argument("--no-push", action="store_true")
    ma.set_defaults(func=_guarded(cmd_member_add))
    from . import cli_tenure
    mr = msub.add_parser("remove", help="remove a member")
    mr.add_argument("handle")
    mr.add_argument("--repo")
    mr.add_argument("--no-push", action="store_true")
    mr.set_defaults(func=_guarded(lambda a: cli_tenure.cmd_member_remove(GitLedger(_repo(a)), a)))
    cli_tenure.register(sub, add)

    p = add("pack-sync", cmd_pack_sync, "Owner only: seed or upgrade a pack's judgment.toml rules into the ledger.")
    p.add_argument("pack_dir")
    p.add_argument("--no-push", action="store_true")


# commands that change no ledger, pin or trust state run outside the op, so they never queue behind a slow sync (code
# L3 r7 complement 2): `view` (a server; its writes take the op), `status`/`verify`/`doctor` (read), `install` (writes
# .claude settings only)
_OUTSIDE_OP = {"view", "status", "verify", "doctor", "install"}


def _guarded(func):
    def call(args) -> int:
        from .transport import WARNINGS
        try:
            if getattr(args, "team_command", None) in _OUTSIDE_OP:
                return func(args)      # read-only, or a server whose writes take the op themselves
            # every other team command is ONE operation on the clone, start to finish (code L3 r6: git's frame)
            with GitLedger(_repo(args)).op():
                return func(args)
        except (TeamError, E.EntryError, R.RolesError, ValueError, OSError) as exc:
            print(f"levain team: {exc}", file=sys.stderr)
            return 2
        finally:
            for w in dict.fromkeys(WARNINGS):
                print(f"levain team: WARNING: {w}", file=sys.stderr)
    return call
