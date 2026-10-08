"""``levain team`` tenure commands: hand-off, removal, keys, revocation, vetoes, and this clone's own trust acts.

Team changes go through ``GitLedger.update_counted`` (signed, written from the COUNTED state, restore-first). The
clone-local acts (``distrust``, ``repin``, ``accept-merge``) change only this clone's trust state and are never pushed:
they are the operator's own judgement, taken on the owner's word out of band (tenure_design_1005.md §3a, §3g).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import roles as R
from . import signing as S
from . import tenure as T
from .transport import BRANCH, LEGACY_BRANCH, GitLedger, TeamError, git


def _key_line(value: str) -> str:
    """A public key given as a line or a path to a ``.pub`` file, refused unless levain accepts its type."""
    p = Path(value).expanduser()
    line = p.read_text(encoding="utf-8").strip().splitlines()[0] if p.exists() else value.strip()
    try:
        S.fingerprint(line)
    except S.SigningError as exc:
        raise TeamError(f"that key cannot be used: {exc}") from None
    return line


def _me(gl: GitLedger) -> str:
    h = gl.handle()
    if h is None:
        raise TeamError("this machine's key is not a member's key in this ledger")
    return h


def _require_owner(gl: GitLedger) -> T.Derivation:
    d = gl.derivation()
    if gl.own_fingerprint() not in T.key_fps(d.tenure, d.team.owner):
        raise TeamError(f"only the owner in force ({d.team.owner}) can do this, signing with her key")
    return d


LOST = ("a member who lost every key is invited again under a new handle (a removed handle is never re-used); "
        "a stolen key is also revoked with `levain team revoke`")


def cmd_owner(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = _require_owner(gl)
    if args.cancel:
        if not d.tenure.offer:
            print("no pending offer")
            return 0
        print(gl.update_counted(lambda t, n: setattr(n, "offer", None), "levain team: cancel the ownership offer",
                                push=not args.no_push))
        return 0
    if not args.handle:
        raise TeamError("levain team owner <handle> [--email E]   (or --cancel)")
    if args.handle not in d.team.members:
        raise TeamError(f"{args.handle} is not a member")
    if not d.tenure.keys.get(args.handle):
        raise TeamError(f"{args.handle} has no key in force yet, so they could not sign the accept: their pending "
                        "key comes into force when they run `levain team key confirm` (or `join`) on its machine")
    email = args.email or d.team.members[args.handle]

    def offer(team: R.Team, ten: T.Tenure) -> None:
        ten.offer = {"handle": args.handle, "email": email}
    print(gl.update_counted(offer, f"levain team: offer ownership to {args.handle}", push=not args.no_push))
    print(f"offered ownership to {args.handle}; it takes effect when they run `levain team accept` with a key already "
          "in force for them")
    return 0


def cmd_accept(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = gl.derivation()
    off = d.tenure.offer
    fp = gl.own_fingerprint()
    if not off or fp not in T.key_fps(d.tenure, off["handle"]):
        raise TeamError("there is no live ownership offer to a member whose key in force is this machine's")

    def accept(team: R.Team, ten: T.Tenure) -> None:
        team.owner = off["handle"]
        team.members[off["handle"]] = off.get("email") or team.members[off["handle"]]
        ten.offer = None
    print(gl.update_counted(accept, f"levain team: {off['handle']} accepts ownership", push=not args.no_push))
    return 0


def cmd_member_remove(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = _require_owner(gl)
    if args.handle == d.team.owner:
        raise TeamError("the owner cannot be removed; hand off first (`levain team owner`)")
    if args.handle not in d.team.members:
        raise TeamError(f"{args.handle} is not a member")

    def remove(team: R.Team, ten: T.Tenure) -> None:
        team.members.pop(args.handle, None)
        ten.keys.pop(args.handle, None)
        ten.pending_keys.pop(args.handle, None)
        if ten.offer and ten.offer.get("handle") == args.handle:
            ten.offer = None
    print(gl.update_counted(remove, f"levain team: remove member {args.handle}", push=not args.no_push))
    return 0


def cmd_key_add(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = gl.derivation()
    me = gl.handle()
    owner_first_key = (me == d.team.owner and args.handle in d.team.members and args.handle not in d.ever_keyed
                       and not d.tenure.pending_keys.get(args.handle))
    if me != args.handle and not owner_first_key:
        # the owner proposes a member's key only until that member's first key is in force: after that, a key she
        # holds confirmed under their handle would sign as them (owner-impersonation fix)
        raise TeamError(f"only {args.handle}, signing with a key already in force for them, may propose "
                        f"{args.handle}'s keys (the owner proposes only a member's first key, while none is pending: `levain team key remove` a wrong "
                        f"pending one first); {LOST}")
    line = _key_line(args.key)
    fp = S.fingerprint(line)
    for h, lines in list(d.tenure.keys.items()) + list(d.tenure.pending_keys.items()):
        if fp in {T._fp_or_none(x) for x in lines}:
            raise TeamError(f"that key is already {h}'s (in force or pending); the owner or {h} removes it first")

    def add(team: R.Team, ten: T.Tenure) -> None:
        ten.pending_keys.setdefault(args.handle, []).append(line)
    print(gl.update_counted(add, f"levain team: propose a key for {args.handle}", push=not args.no_push))
    print(f"pending: the key comes into force when {args.handle} runs `levain team key confirm` (or `join`) on the machine "
          "that holds it")
    return 0


def cmd_key_confirm(gl: GitLedger, args) -> int:
    gl.require_joined()
    print("confirmed" if gl._confirm_own_key(gl.derivation()) else "this machine's key is not pending for anyone")
    return 0


def cmd_key_remove(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = gl.derivation()
    if gl.handle() not in (d.team.owner, args.handle):
        raise TeamError(f"only the owner ({d.team.owner}) or {args.handle} may remove {args.handle}'s keys")

    def remove(team: R.Team, ten: T.Tenure) -> None:
        for table in (ten.keys, ten.pending_keys):
            table[args.handle] = [x for x in table.get(args.handle, []) if T._fp_or_none(x) != args.fingerprint]
    print(gl.update_counted(remove, f"levain team: remove a key of {args.handle}", push=not args.no_push))
    return 0


def cmd_revoke(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = _require_owner(gl)
    if args.fingerprint in T.key_fps(d.tenure, d.team.owner):
        # an owner key is never revoked in the ledger: a thief holding it could revoke the owner's other keys the same
        # way. A stolen owner key is recovered by re-genesis (signing doc §7)
        raise TeamError("that is an owner key, which the ledger never revokes (whoever stole it could revoke yours "
                        "the same way); a stolen owner key is recovered with `levain team regenesis`")
    after = git(["rev-parse", "--verify", args.after + "^{commit}"], gl.repo.toplevel).stdout.strip()
    spell = next((s_ for s_ in d.spells if s_.role == "owner" and s_.end is None), None)
    if spell is not None and (after not in d.walk or d.walk.index(after) < spell.start):
        # the derivation counts a revoke only from inside the current owner's spell (tenure._apply); say so instead of
        # the generic "would not count" (docs L3 r1 complement + anansi)
        raise TeamError(f"--after must name a commit on this ledger at or after {spell.start_sha[:10]}, where "
                        f"{d.team.owner}'s spell as owner began; a revoke voids only what the key signed after it. For "
                        "lines a key signed in an earlier spell, `levain team veto` withdraws that spell's authority")

    def revoke(team: R.Team, ten: T.Tenure) -> None:
        ten.revokes.append({"key": args.fingerprint, "after": after})
        for table in (ten.keys, ten.pending_keys):
            for h in list(table):
                table[h] = [x for x in table[h] if T._fp_or_none(x) != args.fingerprint]
    print(gl.update_counted(revoke, f"levain team: revoke key {args.fingerprint} after {after[:10]}",
                            push=not args.no_push))
    return 0


def cmd_veto(gl: GitLedger, args) -> int:
    gl.require_joined()
    d = _require_owner(gl)
    since = git(["rev-parse", "--verify", args.since + "^{commit}"], gl.repo.toplevel).stdout.strip()
    # the derivation matches a veto to a spell by the commit that BEGAN it; any other commit was counted and did nothing
    # (docs L3 r2 anansi, RUN)
    if args.role == "owner":
        starts = [first for _s, _e, first in T._owner_runs(d.spells, args.handle)]
    else:
        starts = [sp.start_sha for sp in d.spells if sp.role == "member" and sp.handle == args.handle]
    if since not in starts:
        raise TeamError(f"{since[:10]} began no {args.role} spell of {args.handle}, so this veto would change nothing; "
                        f"--since takes the commit that began the spell: "
                        + (", ".join(x[:10] for x in starts) or f"{args.handle} has held no {args.role} spell"))

    def veto(team: R.Team, ten: T.Tenure) -> None:
        ten.vetoes.append({"handle": args.handle, "role": args.role, "since": since, "links": False})
    print(gl.update_counted(veto, f"levain team: veto {args.handle}'s {args.role} spell from {since[:10]}",
                            push=not args.no_push))
    return 0


# ---- this clone's own trust acts (never pushed) ----------------------------------------------------------------


def cmd_distrust(gl: GitLedger, args) -> int:
    st = gl.state()
    cur = set(st.get("distrust") or [])
    if args.list:
        for c in sorted(cur):
            print(c)
        return 0
    if args.clear:
        gl.save_state(distrust=[])
        print("distrust list cleared on this clone")
        return 0
    sha = git(["rev-parse", "--verify", args.commit + "^{commit}"], gl.repo.toplevel).stdout.strip()
    gl.save_state(_mutate=lambda st: st.__setitem__("distrust", sorted(set(st.get("distrust") or []) | {sha})))
    print(f"{sha[:10]} distrusted on this clone (its changes do not count here); this is never pushed")
    return 0


def cmd_repin(gl: GitLedger, args) -> int:
    if args.root:
        sha = git(["rev-parse", "--verify", args.root + "^{commit}"], gl.repo.toplevel).stdout.strip()
        if git(["rev-list", "--parents", "-n", "1", sha], gl.repo.toplevel).stdout.split()[1:]:
            raise TeamError("a genesis has no parents; that commit does")
        gl.save_state(pinned_root=sha, anchor=None, repin_n=int(gl.state().get("repin_n") or 0) + 1)
        left = gl.other_genesis_items()
        if left:
            gl.warnings.append(left)
    if args.anchor:
        sha = git(["rev-parse", "--verify", args.anchor + "^{commit}"], gl.repo.toplevel).stdout.strip()
        if git(["merge-base", "--is-ancestor", sha, gl.head()], gl.repo.toplevel, check=False).returncode != 0:
            raise TeamError("the anchor must be on this ledger's history")
        root = gl.pinned_root or ""
        merges = git(["rev-list", "--first-parent", "--merges", sha, f"^{root}"], gl.repo.toplevel,
                     check=False).stdout.split()
        unaccepted = [m for m in merges if m not in (gl.state().get("accepted") or {})]
        if unaccepted:
            raise TeamError(f"the anchor is past merge {unaccepted[0][:10]}, which this clone has not accepted")
        # the repair point (the commit the owner named) feeds the export epoch, so honest clones that apply the same
        # repair agree; repin_n makes this clone's next export win over its own stored high-water mark (seam §3a)
        gl.save_state(anchor=sha, repair_point=sha, repin_n=int(gl.state().get("repin_n") or 0) + 1)
    gl._dcache = None
    d = gl.derivation()
    print(f"re-pinned; judged {d.judged}, owner in force {d.team.owner}")
    return 0


def cmd_accept_merge(gl: GitLedger, args) -> int:
    """This OPERATOR's act on the owner's out-of-band word: follow parent N of one merge. Lists what it leaves out."""
    gl.require_joined()
    sha = git(["rev-parse", "--verify", args.merge + "^{commit}"], gl.repo.toplevel).stdout.strip()
    parents = git(["rev-list", "--parents", "-n", "1", sha], gl.repo.toplevel).stdout.split()[1:]
    if len(parents) < 2:
        raise TeamError("that commit is not a merge")
    n = args.parent
    if not 1 <= n <= len(parents):
        raise TeamError(f"--parent must be 1..{len(parents)}")
    anchor = gl.state().get("anchor")
    follow = parents[n - 1]
    if anchor and git(["merge-base", "--is-ancestor", anchor, follow], gl.repo.toplevel,
                      check=False).returncode != 0:
        raise TeamError(f"this clone's anchor {anchor[:10]} is not on parent {n}'s chain: this clone never derived "
                        "that side, so it will not follow it (ask the owner which parent is the team's line)")
    others = [p for i, p in enumerate(parents, 1) if i != n]
    base = git(["merge-base", follow, *others], gl.repo.toplevel, check=False).stdout.strip()
    left = git(["rev-list", "--reverse", *others, f"^{follow}"], gl.repo.toplevel).stdout.split()
    touched_team = [c for c in left if set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c],
                                               gl.repo.toplevel).stdout.split()) & {T.TEAM_FILE, T.TENURE_FILE}]
    # list the side's team DECISIONS, judged on the side's own chain (this clone's anchor is not on it): a commit that
    # never counted even there is not a decision being left out (T40, RUN). A commit that cannot be judged is listed.
    import dataclasses
    side_clone = dataclasses.replace(gl.clone(), anchor=None)
    cache = S.SigCache(gl.base / "sigcache.json")

    def counted_on_its_side(c: str) -> bool:
        try:
            dc = T.derive(gl.repo.toplevel, c, side_clone, cache)
        except (T.Unjudgeable, TeamError):
            return True     # cannot be judged: listed, never a crash after the merge is accepted (code L3 r3)
        return dc.judged != "full" or dc.counted_head == c
    team_side = [c for c in touched_team if counted_on_its_side(c)]
    # the whole listing is built BEFORE the merge is accepted: a failure while building it leaves nothing accepted
    # (code L3 r3 complement 6 + glm MED 2)
    listing = [f"accepted merge {sha[:10]} on THIS clone, following parent {n}. Commits on the other side are NOT read:",
               f"  {len(left)} commit(s) since the merge-base {base[:10]}; team decisions among them NOT in force:"]
    listing += ["    " + git(["log", "-1", "--format=%h %ae %s", c], gl.repo.toplevel).stdout.strip() for c in team_side]
    if len(touched_team) > len(team_side):
        listing.append(f"  ({len(touched_team) - len(team_side)} other commit(s) there changed team.toml/tenure.toml "
                       "and never counted, even on that side)")
    # judged on a PROSPECTIVE clone that already follows this parent, and saved only if that judges: an acceptance
    # whose walk cannot be judged was saved, then failed (code L3 r4 codex MED, RUN: a local-only ledger, an orphan
    # merged in, `--parent 2`)
    c0 = gl.clone()
    try:
        d = T.derive(gl.repo.toplevel, gl.head(), dataclasses.replace(c0, accepted={**c0.accepted, sha: n}), cache)
    except T.Unjudgeable as exc:
        raise TeamError(f"following parent {n} of {sha[:10]} leaves a history this clone cannot judge ({exc}); "
                        "nothing was accepted") from None
    gl.save_state(_mutate=lambda st: st.__setitem__("accepted", {**dict(st.get("accepted") or {}), sha: n}))
    gl._dcache = None
    print("\n".join(listing))
    print(f"Every member runs exactly: levain team accept-merge {sha} --parent {n}")
    print(f"now judged {d.judged}; owner in force {d.team.owner}")
    return 0


def cmd_regenesis(gl: GitLedger, args) -> int:
    """Option 2: a FRESH strict genesis on a NEW branch, carrying the old ledger as a read-only record under prior/."""
    gl.require_joined()
    old_tip = git(["rev-parse", "--verify", args.from_ + "^{commit}"], gl.repo.toplevel).stdout.strip()
    # the ledger re-genesised is the one this clone is pinned to: a --from on another ledger was derived against this
    # pin by anchor fallback, and the identity guard below read the WRONG roster (code L3 r3 codex 1, RUN: ben, pinned
    # to A, filed his key as alice's owner key from re-genesis B's tip)
    # and on the history this clone itself holds: an ancestor of its head or of its anchor, never a commit it never
    # judged (code L3 r4 complement LOW 5: any descendant of the genesis, host-forged or unsynced, became the record)
    old_root = gl.pinned_root or ""
    ours = [r for r in (gl.head(), gl.state().get("anchor")) if r]
    if git(["merge-base", "--is-ancestor", old_root, old_tip], gl.repo.toplevel, check=False).returncode != 0 or \
            not any(git(["merge-base", "--is-ancestor", old_tip, r], gl.repo.toplevel, check=False).returncode == 0
                    for r in ours):
        raise TeamError(f"--from {args.from_} is not on the ledger this clone is pinned to (genesis {old_root[:12]}) "
                        "as this clone holds it (an ancestor of its head or its last judged commit); a re-genesis "
                        "starts from the pinned ledger. To re-genesis another, join it first")
    members: dict[str, str] = {}
    keys: dict[str, str] = {}
    for v in args.member or []:
        parts = v.split("=", 2)
        if len(parts) < 2:
            raise TeamError(f"--member takes handle=email[=pubkey], got {v!r}")
        members[parts[0]] = parts[1]
        if len(parts) == 3 and parts[2]:
            keys[parts[0]] = _key_line(parts[2])
    team = R.Team(project=args.project or "", owner=args.owner, members=members)
    R.validate_team(team)
    # the new genesis lists the RUNNER's key as the owner's (it signs it): naming someone else as owner filed the
    # runner's key under their handle, and an owner pubkey given here was dropped without a word (docs L3 r2 anansi)
    if args.owner in keys:
        raise TeamError(f"--member {args.owner}=...=<key>: the owner's key in a re-genesis is the key of whoever runs "
                        "it; run it on the owner's machine, without a key for the owner")
    # by identity, not by "who am I": the runner may not name a handle the old ledger knows unless the runner's key is
    # in force for it there (residue run 1008, RUN: with a rotated key not yet in force, handle() was None and ben
    # filed his key as ana's). A new handle is fine: a member who lost every key comes back under a new handle.
    # Judged on this clone's CURRENT derivation of its pinned ledger (frozen: its last full one), never on --from's: an
    # older --from would not know a handle added since
    d_old = gl.derivation()
    known = set(d_old.team.members) | {s_.handle for s_ in d_old.spells}
    if args.owner in known and gl.own_fingerprint() not in T.key_fps(d_old.tenure, args.owner):
        raise TeamError(f"{args.owner} is a handle on this ledger and this machine's key is not in force for it, so a "
                        "re-genesis with --owner " + args.owner + " would file your key under their name. Name your own "
                        "handle (one your key is in force for) or a new one, then offer ownership; or let "
                        f"{args.owner} run it")
    own = gl.signing_pubkey()
    ten = T.Tenure(keys={team.owner: [own]}, pending_keys={h: [k] for h, k in keys.items() if h != team.owner},
                   prior={"root": old_root, "tip": old_tip})
    files = {T.TEAM_FILE: R.dump_team(team), T.TENURE_FILE: T.dump_tenure(ten)}
    for row in git(["ls-tree", "-r", "--name-only", old_tip, "ledger/"], gl.repo.toplevel).stdout.splitlines():
        files[f"prior/{old_root[:12]}/{row}"] = git(["show", f"{old_tip}:{row}"], gl.repo.toplevel).stdout
    commit = _nested_genesis(gl, files, f"levain team: re-genesis of {old_root[:12]} (prior tip {old_tip[:12]})")
    name = f"{BRANCH}-{commit[:12]}"
    git(["update-ref", f"refs/heads/{name}", commit, ""], gl.repo.toplevel)
    if gl.remote and not args.no_push:
        git(["push", "-q", "--no-verify", gl.remote, f"refs/heads/{name}:refs/heads/{name}"], gl.repo.toplevel,
            timeout=120)
    print(f"re-genesis {commit} on branch {name}; the old ledger is untouched and its history is under "
          f"prior/{old_root[:12]}/ as a read-only record (nothing there is enforced).")
    print(f"Each member moves with: levain team join --root {commit[:12]}")
    print("Re-record the rulings the team still wants; the old in-force entries were:")
    try:
        d = gl.derivation(old_tip)
        for e in gl._ledger_of(d).in_force:
            print(f"   {e['id']}: {e.get('words') or e.get('summary') or ''}"[:160])
    except TeamError as exc:
        print(f"   (cannot judge the old ledger here: {exc})")
    return 0


def cmd_retire_legacy(gl: GitLedger, args) -> int:
    """Delete the remote 0.6.x ``levain-ledger`` (never read or converted by this levain), once a strict ledger exists.
    Only the 0.6.x ledger's owner may (0.6.x has no signatures: the user's email must equal that owner's, T r22-2
    complement L2), or anyone with --force."""
    from .transport import LEGACY_BRANCH
    gl.require_joined()
    remote = gl.remote
    if not remote:
        raise TeamError("no remote")
    row = git(["ls-remote", "--heads", remote, f"refs/heads/{LEGACY_BRANCH}"], gl.repo.toplevel, timeout=60).stdout
    if not row.strip():
        print(f"{remote} has no {LEGACY_BRANCH}")
        return 0
    tip = row.split()[0]
    git(["fetch", "-q", remote, f"+refs/heads/{LEGACY_BRANCH}:refs/remotes/{remote}/{LEGACY_BRANCH}"],
        gl.repo.toplevel, timeout=120)
    try:
        old = R.parse_team(git(["show", f"{tip}:team.toml"], gl.repo.toplevel).stdout, "legacy team.toml")
        owner_mail = old.members.get(old.owner, "")
    except (R.RolesError, TeamError):
        owner_mail = ""
    if not args.force and owner_mail.strip().lower() != gl.email().strip().lower():
        raise TeamError(f"only the 0.6.x ledger's owner ({owner_mail or 'unknown'}) retires it; --force to do it anyway")
    git(["push", "-q", "--no-verify", remote, f"--force-with-lease=refs/heads/{LEGACY_BRANCH}:{tip}",
         f":refs/heads/{LEGACY_BRANCH}"], gl.repo.toplevel, timeout=120)
    print(f"deleted {remote}/{LEGACY_BRANCH} (was {tip[:12]}); 0.6.x clones keep their local copy until upgraded")
    return 0


def _nested_genesis(gl: GitLedger, files: dict[str, str], message: str) -> str:
    """A signed parentless commit whose tree holds ``files`` (paths may nest)."""
    top = gl.repo.toplevel
    idx = gl.base / "regenesis.index"
    env_idx = {"GIT_INDEX_FILE": str(idx)}
    import os
    import subprocess
    from .transport import _NO_HOOKS, _SCRUB_ENV
    env = {**{k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}, "LC_ALL": "C", **env_idx}
    try:
        idx.unlink()
    except FileNotFoundError:
        pass
    for path, text in sorted(files.items()):
        blob = git(["hash-object", "-w", "--stdin"], top, input_text=text).stdout.strip()
        subprocess.run(["git", *_NO_HOOKS, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                       cwd=str(top), env=env, check=True, capture_output=True)
    tree = subprocess.run(["git", *_NO_HOOKS, "write-tree"], cwd=str(top), env=env, check=True,
                          capture_output=True, text=True).stdout.strip()
    idx.unlink(missing_ok=True)
    cp = subprocess.run(["git", *_NO_HOOKS, *gl._sign_cfg(), "commit-tree", "-S", tree, "-m", message],
                        cwd=str(top), capture_output=True, text=True, env={**env, **gl._sign_env()},
                        stdin=subprocess.DEVNULL, timeout=60)
    if cp.returncode != 0:
        raise TeamError(f"could not sign the re-genesis: {(cp.stderr or cp.stdout).strip()[-200:]}")
    return cp.stdout.strip()


def register(sub, add) -> None:
    """Add the tenure commands to ``levain team``. ``add`` is cli.register's helper (it wires --repo and guarding)."""
    def wrap(fn, fetch: bool = True):
        def run(args):
            from .cli import _repo
            g = GitLedger(_repo(args))
            if fetch and g.joined() and g.remote:
                g.sync(push=False)     # act on the team as it is now, not as this clone last saw it
                g._dcache = None
            return fn(g, args)
        return run

    p = add("owner", wrap(cmd_owner), "Owner only: offer ownership to a member (they accept with a key already "
            "in force for them).")
    p.add_argument("handle", nargs="?")
    p.add_argument("--email")
    p.add_argument("--cancel", action="store_true", help="withdraw the pending offer")
    p.add_argument("--no-push", action="store_true")

    p = add("accept", wrap(cmd_accept), "Accept a pending ownership offer made to the member whose key this "
            "machine holds.")
    p.add_argument("--no-push", action="store_true")

    p = add("key", None, "Signing keys: add (pending until proven), confirm, remove.")
    ksub = p.add_subparsers(dest="key_command", required=True)
    for name, fn, help_ in (("add", cmd_key_add, "propose a key: the member's own, or the owner's for a member with no key yet"),
                            ("confirm", cmd_key_confirm, "prove this machine's pending key"),
                            ("remove", cmd_key_remove, "remove one of a member's keys by fingerprint")):
        kp = ksub.add_parser(name, help=help_)
        kp.add_argument("--repo")
        kp.add_argument("--no-push", action="store_true")
        if name in ("add", "remove"):
            kp.add_argument("handle")
        if name == "add":
            kp.add_argument("key")
        if name == "remove":
            kp.add_argument("fingerprint")
        from .cli import _guarded
        kp.set_defaults(func=_guarded(wrap(fn)))

    p = add("revoke", wrap(cmd_revoke), "Owner only: void every commit a stolen key signed after a commit.")
    p.add_argument("fingerprint")
    p.add_argument("--after", required=True)
    p.add_argument("--no-push", action="store_true")

    p = add("veto", wrap(cmd_veto), "Owner only: revoke a past spell's authority (owner links, or member lines).")
    p.add_argument("handle")
    p.add_argument("--role", choices=("owner", "member"), required=True)
    p.add_argument("--since", required=True, help="the counted commit that began the spell")
    p.add_argument("--no-push", action="store_true")

    p = add("distrust", wrap(cmd_distrust, fetch=False), "This clone only: stop counting one commit (an emergency act).")
    p.add_argument("commit", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--clear", action="store_true")

    p = add("repin", wrap(cmd_repin, fetch=False), "This clone only: re-pin the genesis or the anchor the owner names.")
    p.add_argument("--root")
    p.add_argument("--anchor")

    p = add("accept-merge", wrap(cmd_accept_merge, fetch=False), "This clone only, on the owner's word: follow one parent of a "
                                                    "merge on the ledger; lists what it leaves out.")
    p.add_argument("merge")
    p.add_argument("--parent", type=int, required=True,   # a trust decision, never a default (docs L3 r1)
                   help="the merge parent the owner named (1 = the published line)")

    p = add("retire-legacy", wrap(cmd_retire_legacy), "Delete the remote 0.6.x levain-ledger (its owner only, or "
                                                      "--force); it is never read or converted.")
    p.add_argument("--force", action="store_true")

    p = add("regenesis", wrap(cmd_regenesis), "After a permanently lost owner: a fresh strict genesis on a new "
                                              "branch, carrying the old ledger as a read-only record.")
    p.add_argument("--from", dest="from_", required=True,
                   help="the old ledger tip to carry (a commit of the pinned ledger this clone holds)")
    p.add_argument("--owner", required=True)
    p.add_argument("--member", action="append", required=True, metavar="HANDLE=EMAIL[=PUBKEY]")
    p.add_argument("--project")
    p.add_argument("--no-push", action="store_true")


def status_lines(gl: GitLedger) -> list[str]:
    """What every member must see (§3f, signing doc §6): the pinned genesis and branch, a frozen state, role changes
    from the last 14 days, a pending offer, and any re-genesis whose ``prior`` names this ledger."""
    from datetime import datetime, timedelta, timezone
    out: list[str] = []
    try:
        d = gl.derivation()
    except TeamError as exc:
        return [f"cannot judge the team ledger on this clone: {exc}"]
    out.append(f"ledger {gl.branch}, genesis {d.walk[0][:12]}, owner in force {d.team.owner}")
    if d.judged != "full":
        fix = ("`levain team accept-merge <merge> --parent <n>`, the parent the owner names" if "merge" in d.frozen_why
               else "`levain team repin --anchor <commit>`, the commit the owner names, or the host restores the branch")
        out.append(f"FROZEN at {str(d.frozen_at)[:10]}: {d.frozen_why}; {d.waiting} line(s) after it are waiting "
                   f"(not enforced). Resolve with the owner: {fix}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for sha, date, text in d.role_changes:
        try:
            when = datetime.fromisoformat(date)
        except ValueError:
            continue
        if when >= cutoff:
            out.append(f"role change {when.date()}: {text} ({sha[:10]})")
    last = gl.state().get("last_fetch_ok")
    if gl.remote:
        import time
        age = (time.time() - float(last)) if last else None
        newest = git(["log", "-1", "--format=%cr", gl.ref], gl.repo.toplevel, check=False).stdout.strip()
        out.append(f"last fetched {('%d min ago' % (age // 60)) if age is not None else 'never'}; newest ledger "
                   f"commit {newest or 'unknown'}. A host can withhold newer commits without any signal: compare "
                   "the tip id with a teammate if anything looks stale")
    if d.tenure.offer:
        out.append(f"pending ownership offer to {d.tenure.offer['handle']} (`levain team owner --cancel` withdraws it)")
    for h, lines in sorted(d.tenure.pending_keys.items()):
        if lines:
            out.append(f"{h} has {len(lines)} key(s) pending confirmation")
    if gl.remote:
        refs = git(["for-each-ref", "--format=%(refname)", f"refs/remotes/{gl.remote}/"], gl.repo.toplevel,
                   check=False).stdout.split()
        if f"refs/remotes/{gl.remote}/{LEGACY_BRANCH}" in refs:
            # T46, RUN: a 0.6.x ledger beside the strict one was never mentioned
            out.append(f"a 0.6.x {LEGACY_BRANCH} branch was on {gl.remote} at this clone's last fetch: this levain never "
                       "reads it (nothing in it is enforced); the owner deletes it with `levain team retire-legacy`")
        for ref in refs:
            name = ref.rsplit("/", 1)[-1]
            if not name.startswith(BRANCH + "-") or name == gl.branch:
                continue
            roots = git(["rev-list", "--max-parents=0", ref], gl.repo.toplevel, check=False).stdout.split()
            ten = gl._show(T.TENURE_FILE, roots[0]) if roots else None
            try:
                rt = T.parse_tenure(ten or "", "x")
                rteam = R.parse_team(gl._show(T.TEAM_FILE, roots[0]) or "", "x")
                prior, rowner = rt.prior, rteam.owner
                rfps = ", ".join(sorted(T.key_fps(rt, rowner))) or "none"
            except Exception:  # noqa: BLE001 - a display line; a foreign ledger's bad file is not this clone's problem
                prior = None
            if prior and prior.get("root") == d.walk[0]:
                # anyone who can push can make one, so the line names its owner and keys and claims nothing for it
                # (docs L3 r1 anansi: an unqualified "to move" line advertised whatever branch was pushed)
                out.append(f"a branch {name} claims to RE-GENESIS this team, owner {rowner} (keys {rfps}); UNVERIFIED: "
                           f"move only if your owner confirms those keys out of band "
                           f"(`levain team join --root {name.rsplit('-', 1)[-1]}`)")
    return out
