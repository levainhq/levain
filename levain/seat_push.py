"""The seat's lane-branch egress (PATHZERO Z2a, ruling H2 a): a lane's commits reach ``seat/<lane_key>`` with no dialog,
and a seat has no path to main.

The seat holds no push credential and has no push tool. Its profile (``levain.seat_profile``) carries a ``Stop`` and a
``SessionEnd`` command hook that runs ``python -EP -B -m levain.seat_push <event> ...``; a command hook runs outside the
sandbox bound by vendor design, measured on 2.1.319 to fire under the seat's source arguments and ``--restricted`` (U9,
``project_memory/1106-16/z0d/RETURN_u9.md``: Stop has no 60 s ceiling but takes an explicit ``timeout``; SessionEnd's
default is about 1 s, so it needs one; exit-2 stderr on Stop reaches the model; guard ``stop_hook_active``).

What the hook does, in order, and what each step trusts:

1. the clone and the key directory are ARGUMENTS fixed in the profile's argv, never the hook's cwd or stdin: the seat
   cannot name another repository. The clone's realpath must match a row of the launcher's signed mint chain
   (:func:`levain.seat_lease.read_mints`), whose ``lane_key`` fixes the branch: ``seat/<lane_key>`` and no other name.
2. the tip is read from outside the repository (``git ls-remote`` on the path, which runs none of its hooks). Nothing to
   push (no such branch, still at the base) ends the hook silently; so does a tip already pushed to (or rejected by)
   the remote of the SAME live grant, or tree-refused and told under it (``egress_pushed/<lane>`` remembers tip, grant id, incarnation and remote, so a
   new grant gets the tip again).
3. :func:`levain.consent.push.execute` pushes: it FETCHES the tip into a Levain-owned bare mirror and pushes from the
   mirror, never reading the clone's ``.git/config`` or ``.git/hooks`` as code. Its fresh judgment under the per-repo
   lock is :func:`egress_refusal`: a LIVE ``seat-egress`` grant (``levain.authority.SEAT_EGRESS_KIND``) in a fresh replay
   of the signed authority log, the clone's realpath below the grant's lanes parent, a verified mint row for it, the
   branch equal to ``seat/<lane_key>`` of that row, the remote equal to the grant's pinned one, and the clone's ``.git``
   the same (device, inode) as the mint pinned. A revoked or expired grant pushes nothing and leaves a record.
4. after the fetch and before the push, :func:`tree_refusal` judges the fetched commits in the mirror: the tip must
   descend from the signed mint row's ``base_sha``, and no commit since it may add, modify, delete or rename a path with
   an ``activation`` component. This is what makes a launch-exempt ``activation/`` directory never writable where it
   matters, at what leaves the seat; the in-seat deny rules on it are defence in depth (a rename of an ancestor and
   back, or a hard-link alias, is not a write to the denied spelling, and still changes the committed blob).

The hook never wedges the seat: every failure is recorded in ``<levain home>/push/seat_egress.jsonl`` and ends in exit
0, with two exceptions on a first ``Stop``, each told once (exit 2: stderr reaches the model; the second Stop has
``stop_hook_active`` and ends 0; the tip is remembered, so it is not told again until the seat moves it; never on
``SessionEnd``): a remote's REJECTION (``status == "rejected"``; the model may rebase), and a TREE refusal (the lane
branch changes an activation directory; the model can drop those commits). The tree judge shares one deadline,
inside the hook's own ceiling; past it the push is refused with fixed words and the record is still written. A transport, auth or DNS failure is recorded, ends 0
and is retried at the next Stop. Nothing here forces: git's rejection of a non-fast-forward is reported.

Known edges (one line each): a ``SIGKILL`` between git returning and the pushed-tip file being written re-pushes the
same tip next time (``up-to-date``, harmless); the seat can rename its clone's ``.git`` away and back, which keeps the
(device, inode) pin, and the executor's mirror fetch is what bounds that (the fetch reads objects, runs no repo code);
a seat that never creates ``seat/<lane_key>`` pushes nothing (no tool or hook of its own names another branch).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from levain import host_guard as hg
from levain.authority import SEAT_EGRESS_KIND, SEAT_EGRESS_MAX, AuthoritySnapshot, _parse_stamp, _thaw
from levain.consent import push

__all__ = ["SEAT_EGRESS_KIND", "SeatPushRefused", "SIGN_COMMAND", "STOP_TIMEOUT", "SESSION_END_TIMEOUT",
           "LOG_NAME", "live_egress", "create_egress", "renew_egress", "egress_refusal", "tree_refusal",
           "TREE_REFUSAL_HEAD", "run_hook", "main", "hook_entries"]

SIGN_COMMAND = ("levain seat egress create --lanes-parent <dir> --remote <url> [--allow-domain <host> ...] "
                "--signing-key <key>")
LOG_NAME = "seat_egress.jsonl"
PUSHED_DIR = "egress_pushed"
#: Stop has no vendor ceiling at 60 s but takes an explicit timeout; a SessionEnd without one is cut at about 1 s (U9).
STOP_TIMEOUT = 180
SESSION_END_TIMEOUT = 60
_STDIN_MAX = 1 << 20
#: The fixed opening of the refusal for a lane tip that changes an ``activation`` directory (L3 r1 on the tracked
#: exemption): the seat may read such a directory and never change it, and the push is where that is decided.
TREE_REFUSAL_HEAD = "the lane branch changes an activation directory, which a seat may read and never change"
_TREE_REFUSAL = (TREE_REFUSAL_HEAD + ": {path} ({n} path(s) across the commits since the launch base {base}). Nothing "
                 "is pushed. Drop the commits that touch it; a later commit that restores the files does not clear it.")
#: The names whose any-depth appearance in a changed path refuses the push: the names the launch may exempt read-only
#: (levain.seat_profile.TRACKED_EXEMPT_NAMES), compared case- and normalisation-insensitively (APFS folds both).
_TREE_NAMES = frozenset({"activation"})
_TREE_MAX_COMMITS = 10_000
#: The changed-path caps over both ``diff-tree`` reads together (streamed NUL-delimited, never held whole); over either
#: refuses with fixed words and nothing is pushed (L3 r2 #2).
_TREE_MAX_PATHS = 200_000
_TREE_MAX_BYTES = 64 << 20
_TREE_TOO_MANY = ("the lane branch changes too many paths to judge (over {cap} across the commits since the launch base "
                  "{base}): nothing is pushed")
#: ONE absolute deadline for the whole judge (every git call shares it), well inside the smallest hook ceiling
#: (SessionEnd, :data:`SESSION_END_TIMEOUT`), leaving the rest for the fetch before it and the record after it (L3 r2
#: #3). The hook passes its own event deadline too; the judge stops at whichever comes first.
_JUDGE_BUDGET = 30
_HOOK_RESERVE = 10
_TREE_LATE = "the lane branch could not be judged in time ({budget} s): nothing is pushed"
#: What a first Stop tells the model when the tree check refuses (L3 r2 #1): the same once-only shape as a rejection.
_TREE_TOLD = ("seat-egress: " + TREE_REFUSAL_HEAD + ": {path}. Nothing was pushed. Drop the commits that touch it (a "
              "later commit that restores the files does not clear it); this message appears once per stop.")
_REJECT_TEXT = ("seat-egress: the remote rejected your lane branch ({branch}): {detail}. Nothing was forced. Rebase on "
                "what the remote holds or ask the desk; this message appears once per stop.")


class SeatPushRefused(Exception):
    """A seat-egress grant or hook is refused. ``str()`` is the reason shown to the operator."""


# --- the grant (sign / read) -----------------------------------------------------------------------------------------

def live_egress(state: AuthoritySnapshot, now: datetime) -> Mapping[str, Any] | None:
    """The one live ``seat-egress`` grant of the verified replay ``state`` at ``now`` (its fields plus ``link_seq``), or
    None. More than one live is refused (the log refuses to append a second; a foreign writer could leave two)."""
    live = []
    for gid, grant in state.policy.grants.items():
        if not (isinstance(grant, Mapping) and grant.get("kind") == SEAT_EGRESS_KIND):
            continue
        try:
            if _parse_stamp(grant["expires_at"], "expires_at") > now:
                live.append((gid, grant))
        except Exception:  # noqa: BLE001 - a grant whose expiry cannot be read is not live
            continue
    if len(live) > 1:
        raise SeatPushRefused("the authority log holds more than one live seat-egress grant: revoke all but one with `levain seat egress revoke <id>` (see `levain seat egress list`)")
    if not live:
        return None
    gid, grant = live[0]
    link = state.grant_link(gid)
    return {**_thaw(grant), "link_seq": 0 if link is None else link[0], "incarnation": state.grant_born.get(gid, 0)}


def create_egress(cockpit_root: Path | str, *, lanes_parent: Path | str, push_remote: str,
                  allowed_domains: Sequence[str] = (), signer_fp: str, sign: Callable[[bytes], str],
                  clock: Callable[[], datetime], grant_id: str | None = None) -> Mapping[str, Any]:
    """Sign a 14-day ``seat-egress`` grant (a laptop-presence key). The remote is stored in its canonical spelling and
    the lanes parent as its realpath. The log's own rules judge the rest before anything is signed
    (:func:`levain.authority._seat_egress_check`). A renew is :func:`levain.seat_lease.renew_lease` (kind-blind)."""
    import secrets
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.keys import _append, _capture, _journal, _now, _signing_authority
    try:
        canon = push.canonical_remote(push_remote)
    except ValueError as exc:
        raise SeatPushRefused(f"the remote: {exc}") from None
    from levain.authority import AuthorityError, _seat_egress_check
    now = _now(clock)
    parent = Path(os.path.expanduser(str(lanes_parent)))
    grant = {"id": grant_id or "se-" + secrets.token_hex(6), "kind": SEAT_EGRESS_KIND, "agent": "claude_code",
             "lanes_parent": str(Path(os.path.realpath(parent))), "push_remote": canon,
             "allowed_domains": sorted(set(allowed_domains)), "expires_at": utc_stamp(now + SEAT_EGRESS_MAX)}
    try:
        _seat_egress_check([grant])          # the log's own root rule, BEFORE the mkdir: a refused parent is never made
    except AuthorityError as exc:
        raise SeatPushRefused(str(exc)) from None
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = _journal(cockpit_root)
    _signing_authority(_capture(db, now, DEFAULT_SSH_KEYGEN))
    new = _append(db, {"op": "create_grant", "grant": grant}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=DEFAULT_SSH_KEYGEN)
    return _thaw(new.policy.grants[grant["id"]])


def renew_egress(cockpit_root: Path | str, grant_id: str, *, signer_fp: str, sign: Callable[[bytes], str],
                 clock: Callable[[], datetime]) -> str:
    """Re-sign the seat-egress grant ``grant_id`` for :data:`SEAT_EGRESS_MAX` more; returns the new ``expires_at``.
    Refuses (:class:`SeatPushRefused`) an id the log does not hold as a seat-egress grant, so ``levain seat egress
    renew`` never extends a lease or another kind; the log's own renew rules (live, not lapsed, capped) judge the rest
    and their refusal propagates as itself (``KeyError_``), never as a success line."""
    from levain import seat_lease
    state = seat_lease._replay(cockpit_root)
    grant = None if state is None else state.policy.grants.get(grant_id)
    if not (isinstance(grant, Mapping) and grant.get("kind") == SEAT_EGRESS_KIND):
        raise SeatPushRefused(f"{grant_id!r} is not a seat-egress grant the log holds: nothing renewed")
    return seat_lease.renew_lease(cockpit_root, grant_id, signer_fp=signer_fp, sign=sign, clock=clock,
                                  max_life=SEAT_EGRESS_MAX)


def revoke_egress(cockpit_root: Path | str, grant_id: str, *, signer_fp: str, sign: Callable[[bytes], str],
                  clock: Callable[[], datetime]) -> tuple[int, str | None]:
    """Revoke the seat-egress grant ``grant_id``. Refuses (:class:`SeatPushRefused`) an id the log does not hold as a
    seat-egress grant, so ``levain seat egress revoke`` never revokes a lease or a consent grant; the kind check and
    the revoke are two replays (a same-id re-creation between them is a known edge, an operator-only path)."""
    from levain import seat_lease
    from levain.consent.grants import revoke_grant
    state = seat_lease._replay(cockpit_root)
    grant = None if state is None else state.policy.grants.get(grant_id)
    if not (isinstance(grant, Mapping) and grant.get("kind") == SEAT_EGRESS_KIND):
        raise SeatPushRefused(f"{grant_id!r} is not a seat-egress grant the log holds: nothing revoked")
    return revoke_grant(cockpit_root, grant_id, signer_fp=signer_fp, sign=sign, clock=clock)


# --- the judge -------------------------------------------------------------------------------------------------------

def _mint_for(key_dir: Path, clone_real: str) -> dict[str, Any]:
    """The newest verified mint row whose clone is ``clone_real``. Raises :class:`SeatPushRefused` for a broken chain,
    a missing key or no such row."""
    from levain import seat_lease
    try:
        rows = seat_lease.read_mints(key_dir)
    except (ValueError, OSError, seat_lease.SeatLaunchRefused) as exc:
        raise SeatPushRefused(f"the mint chain cannot be verified ({type(exc).__name__}: {str(exc)[:120]})") from None
    for row in reversed(rows):
        if row.get("clone") == clone_real and isinstance(row.get("lane_key"), str):
            return row
    raise SeatPushRefused(f"no verified mint row names the clone {clone_real!r}")


def egress_refusal(cockpit_root: Path | str, key_dir: Path, *, now: Callable[[], datetime] | None = None,
                   ) -> Callable[..., tuple[str, int] | str]:
    """The ``refusal=`` of :func:`levain.consent.push.execute`: called under the per-repo lock with the clone's realpath,
    the canonical remote and the branch, it re-reads the authority log and the mint chain NOW and returns
    ``(grant id, link seq)`` or the reason it refuses. Pure over its inputs: it pushes nothing."""
    clock = now or (lambda: datetime.now(timezone.utc))

    def refusal(*, path: str, remote: str, branch: str) -> tuple[str, int] | str:
        from levain import seat_lease
        try:
            state = seat_lease._replay(cockpit_root)
            grant = None if state is None else live_egress(state, clock())
        except (SeatPushRefused, seat_lease.SeatLaunchRefused) as exc:
            return str(exc)
        if grant is None:
            return "no live seat-egress grant (revoked, expired or never signed): nothing is pushed"
        try:
            row = _mint_for(key_dir, path)
        except SeatPushRefused as exc:
            return str(exc)
        if branch != f"seat/{row['lane_key']}":
            return f"branch {branch} is not this lane's seat/{row['lane_key']}: a seat pushes its lane branch only"
        if remote != grant["push_remote"]:
            return f"the remote {remote} is not the grant's pinned remote"
        parent = str(grant["lanes_parent"])
        if path == parent or not hg.is_within(path, parent):
            return f"the clone {path} is not below the grant's lanes parent {parent}"
        try:
            st = os.lstat(os.path.join(path, ".git"))
        except OSError:
            return "the clone's .git cannot be read"
        if not stat.S_ISDIR(st.st_mode) or [st.st_dev, st.st_ino] != row.get("git"):
            return "the clone's .git is not the one the mint pinned"
        return grant["id"], int(grant["link_seq"])

    return refusal


# --- the tree check: an activation directory never leaves the seat changed ----------------------------------------------

def _activation_paths(paths: Sequence[bytes]) -> list[str]:
    """The paths (decoded, in order) that have a :data:`_TREE_NAMES` component at any depth."""
    import unicodedata
    out = []
    for raw in paths:
        p = os.fsdecode(raw)
        if any(unicodedata.normalize("NFC", c).casefold() in _TREE_NAMES for c in p.split("/")):
            out.append(p)
    return out


class _Late(Exception):
    """The judge's one deadline passed."""


def _mirror_argv(mirror: Path, args: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    env = {**push.git_env(""), **push.git_isolation_env(), "LC_ALL": "C"}
    return push.git_isolation_args(push.git_bin(), str(mirror)) + ["--no-replace-objects", *args], env


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Late()
    return left


def _mirror_git(mirror: Path, args: Sequence[str], *, deadline: float, stdin: bytes | None = None):
    """git on levain's own mirror: no global or system config, no hooks, no replace objects, no transport. Bytes, for a
    SMALL output only (bounded by its arguments). Raises :class:`_Late` once ``deadline`` (monotonic) has passed."""
    import subprocess
    argv, env = _mirror_argv(mirror, args)
    try:
        return subprocess.run(argv, env=env, cwd="/", input=stdin if stdin is not None else b"",
                              capture_output=True, timeout=_left(deadline))
    except subprocess.TimeoutExpired:
        raise _Late() from None


def _mirror_paths(mirror: Path, args: Sequence[str], *, deadline: float, stdin: bytes, tally: dict[str, Any],
                  ) -> str | None:
    """Stream a ``-z --name-only`` git output from the mirror, counting paths and bytes into ``tally`` (shared across
    calls) and keeping only the activation hits (``tally["hits"]``). Returns None, or the refusal: over a cap
    (:data:`_TREE_MAX_PATHS` paths or :data:`_TREE_MAX_BYTES` bytes, the fixed too-many words) or git failed. Raises
    :class:`_Late` at the deadline. git is killed on every early exit; nothing is held whole."""
    import selectors
    import subprocess
    import tempfile
    argv, env = _mirror_argv(mirror, args)
    with tempfile.TemporaryFile() as inf, tempfile.TemporaryFile() as errf:
        inf.write(stdin)
        inf.seek(0)
        proc = subprocess.Popen(argv, env=env, cwd="/", stdin=inf, stdout=subprocess.PIPE, stderr=errf)
        try:
            fd, carry = proc.stdout.fileno(), b""
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_READ)
                while True:
                    if not sel.select(_left(deadline)):
                        continue
                    chunk = os.read(fd, 65536)
                    tally["bytes"] += len(chunk)
                    if tally["bytes"] > _TREE_MAX_BYTES:
                        return _TREE_TOO_MANY.format(cap=_TREE_MAX_PATHS, base=tally["base"])
                    parts = (carry + chunk).split(b"\0")
                    carry = parts.pop() if chunk else b""
                    parts = [p for p in parts if p]
                    tally["paths"] += len(parts)
                    if tally["paths"] > _TREE_MAX_PATHS:
                        return _TREE_TOO_MANY.format(cap=_TREE_MAX_PATHS, base=tally["base"])
                    tally["hits"].update(_activation_paths(parts))
                    if not chunk:
                        break
            try:
                code = proc.wait(timeout=_left(deadline))
            except subprocess.TimeoutExpired:
                raise _Late() from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if code:
            errf.seek(0)
            err = errf.read(4096).decode(errors="replace").strip()
            return f"the lane branch's changes could not be read ({err[:200]})"
    return None


def _printable(p: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in p)


def tree_refusal(key_dir: Path, *, deadline: float | None = None) -> Callable[..., str | None]:
    """The ``tip_check=`` of :func:`levain.consent.push.execute` for a seat (L3 r1, items 1+2). Under the per-repo
    lock, after the tip is fetched into levain's mirror, it re-reads the clone's verified mint row and judges the lane
    branch from the MIRROR's objects (never the clone's working tree or index): the tip must descend from the row's
    ``base_sha``, and no commit in ``base..tip`` may add, modify, delete or rename a path with an ``activation``
    component, against any parent (merges included; renames are seen as a delete and an add). Returns the refusal, or
    None. A rename of an ancestor and back, or a write through a hard-link alias, changes the committed blob, so it is
    caught here whatever the in-seat deny rules saw.

    Bounded (L3 r2 #2, #3): every git call shares ONE deadline, :data:`_JUDGE_BUDGET` from the check's start or the
    caller's ``deadline`` (monotonic), whichever is first, and past it the check refuses with fixed words; the changed
    paths are streamed under a path and a byte cap. On an activation refusal the returned function's ``hit`` attribute
    is ``(first path, judged tip)``, so the hook can tell the model once; otherwise it is None."""

    def check(*, mirror: Path, new: str, path: str) -> str | None:
        check.hit = None
        end = time.monotonic() + _JUDGE_BUDGET
        if deadline is not None:
            end = min(end, deadline)
        try:
            row = _mint_for(key_dir, path)
        except SeatPushRefused as exc:
            return str(exc)
        base = row.get("base_sha")
        if not (isinstance(base, str) and push._SHA.match(base)):
            return "the mint row carries no launch base: the lane's commits cannot be judged, nothing is pushed"
        if new == base:
            return None
        try:
            return _judge(mirror, new, base, end, check)
        except _Late:
            return _TREE_LATE.format(budget=_JUDGE_BUDGET)

    check.hit = None
    return check


def _judge(mirror: Path, new: str, base: str, end: float, check) -> str | None:
    if _mirror_git(mirror, ["cat-file", "-e", f"{base}^{{commit}}"], deadline=end).returncode:
        return f"the lane branch does not descend from the launch base {base[:12]}: nothing is pushed"
    anc = _mirror_git(mirror, ["merge-base", "--is-ancestor", base, new], deadline=end)
    if anc.returncode == 1:
        return f"the lane branch does not descend from the launch base {base[:12]}: nothing is pushed"
    if anc.returncode:
        return f"the lane branch's history could not be read ({anc.stderr.decode(errors='replace').strip()[:200]})"
    revs = _mirror_git(mirror, ["rev-list", f"--max-count={_TREE_MAX_COMMITS + 1}", f"{base}..{new}"], deadline=end)
    if revs.returncode:
        return f"the lane branch's commits could not be listed ({revs.stderr.decode(errors='replace').strip()[:200]})"
    if len(revs.stdout.split()) > _TREE_MAX_COMMITS:
        return f"the lane branch holds more than {_TREE_MAX_COMMITS} commits since the launch base: nothing is pushed"
    tally: dict[str, Any] = {"paths": 0, "bytes": 0, "hits": set(), "base": base[:12]}
    for args, stdin in (
            (["diff-tree", "--stdin", "-r", "-z", "-m", "--root", "--no-renames", "--name-only", "--no-commit-id",
              "--no-ext-diff", "--ignore-submodules=none"], revs.stdout),
            (["diff-tree", "-r", "-z", "--no-renames", "--name-only", "--no-ext-diff", "--ignore-submodules=none",
              base, new], b"")):
        if why := _mirror_paths(mirror, args, deadline=end, stdin=stdin, tally=tally):
            return why
    hits = sorted(tally["hits"])
    if hits:
        check.hit = (_printable(hits[0]), new)
        return _TREE_REFUSAL.format(path=_printable(hits[0]), n=len(hits), base=base[:12])
    return None


# --- the hook ---------------------------------------------------------------------------------------------------------

def _log(home: Path, row: dict[str, Any]) -> None:
    """One line in ``<home>/push/seat_egress.jsonl``; a record that cannot be written is dropped, never raised."""
    try:
        push._private_dir(home / "push")
        push.append_line(home / "push" / LOG_NAME, {**row, "at": time.time()})
    except Exception:  # noqa: BLE001 - a hook never wedges the seat
        pass


def _tip(real: str, branch: str, timeout: int = 60) -> str | None:
    """The commit ``refs/heads/<branch>`` names in the repository at ``real``, read from outside it; None: no branch."""
    r = push._git(["git", "ls-remote", "--", real, f"refs/heads/{branch}"], timeout=timeout, protocols="file")
    if r.returncode:
        raise SeatPushRefused(f"could not read branch {branch} of {real}: {r.stderr.strip()[-200:]}")
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}" and push._SHA.match(sha):
            return sha
    return None


def _pushed_mark(sha: str, grant: Mapping[str, Any]) -> dict[str, str]:
    """What ``egress_pushed/<lane>`` remembers: the tip AND the grant incarnation and remote it went to, so a tip
    already pushed under one grant is pushed again under another (revoke A, sign B for another remote: B gets it). The
    incarnation (the seq of the link that brought the id in) survives a renew and changes on a revoke and re-create."""
    return {"sha": sha, "grant": str(grant["id"]), "incarnation": str(grant.get("incarnation")),
            "remote": str(grant["push_remote"])}


def _last_pushed(key_dir: Path, lane_key: str) -> dict[str, Any] | None:
    """The remembered mark for ``lane_key``, or None (none yet, unreadable, or the pre-fix bare-sha form: a re-push of
    a tip the remote already holds is ``up-to-date``, harmless)."""
    try:
        mark = json.loads((key_dir / PUSHED_DIR / lane_key).read_text())
    except (OSError, ValueError):
        return None
    return mark if isinstance(mark, dict) else None


def _remember(key_dir: Path, lane_key: str, sha: str, grant: Mapping[str, Any]) -> None:
    d = key_dir / PUSHED_DIR
    d.mkdir(mode=0o700, exist_ok=True)
    tmp = d / f".{lane_key}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(_pushed_mark(sha, grant), sort_keys=True) + "\n")
    os.replace(tmp, d / lane_key)


def run_hook(event: str, stdin_text: str, *, clone: Path | str, key_dir: Path | str, cockpit_root: Path | str,
             home: Path | str, err=None, now: Callable[[], datetime] | None = None) -> int:
    """One hook run. Returns the hook's exit code: 0, or 2 once on a first ``Stop`` for a remote's rejection or a tree
    refusal (the lane branch changes an activation directory)."""
    err = err or sys.stderr
    home, key_dir = Path(home), Path(key_dir)
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    active = isinstance(payload, dict) and payload.get("stop_hook_active") is True
    clone_real = os.path.realpath(clone)
    base: dict[str, Any] = {"event": event, "clone": clone_real}
    ceiling = SESSION_END_TIMEOUT if event == "SessionEnd" else STOP_TIMEOUT
    hook_deadline = time.monotonic() + ceiling - _HOOK_RESERVE       # the judge never runs into the record's time
    tc = tree_refusal(key_dir, deadline=hook_deadline)
    lane = grant = None
    try:
        row = _mint_for(key_dir, clone_real)
        lane = row["lane_key"]
        branch = f"seat/{lane}"
        base["branch"] = branch
        tip = _tip(clone_real, branch, timeout=max(1, min(60, int(hook_deadline - time.monotonic()))))
        if tip is None or tip == row.get("base_sha"):
            return 0                                           # nothing on the lane branch yet: silent
        base["tip"] = tip
        from levain import seat_lease
        clock = now or (lambda: datetime.now(timezone.utc))
        state = seat_lease._replay(cockpit_root)
        grant = None if state is None else live_egress(state, clock())
        if grant is None:
            _log(home, {**base, "outcome": "refused", "detail": "no live seat-egress grant: commits stay local"})
            return 0
        if _last_pushed(key_dir, lane) == _pushed_mark(tip, grant):
            return 0                                           # this tip already went to THIS grant's remote: silent
        refusal = egress_refusal(cockpit_root, key_dir, now=clock)

        def receipt(rec: dict[str, Any]) -> None:
            _log(home, {**base, "outcome": "pushed" if rec.get("ok") else "not-pushed", "via": "seat-egress", **rec})

        try:
            result = push.execute({"id": grant["id"]}, repo_path=clone_real, remote=grant["push_remote"],
                                  branch=branch, levain_home=home, refusal=refusal, receipt=receipt,
                                  tip_check=tc)
        except push.PushRecordIncomplete as exc:              # git reported; only levain's own record is short
            _log(home, {**base, "outcome": "record-incomplete", "detail": str(exc)[:400]})
            result = exc.result
        return _settle(result, event=event, active=active, key_dir=key_dir, lane=lane, grant=grant, home=home,
                       base=base, err=err)
    except push.PushRefused as exc:
        _log(home, {**base, "outcome": "refused", "detail": str(exc)[:400]})
        if tc.hit is not None and lane is not None and grant is not None:
            return _tell_tree(tc.hit, event=event, active=active, key_dir=key_dir, lane=lane, grant=grant,
                              home=home, base=base, err=err)
    except push.PushOutcomeUnknown as exc:
        _log(home, {**base, "outcome": "unknown", "detail": str(exc)[:400]})
    except Exception as exc:  # noqa: BLE001 - fail closed on the push, open on the seat
        _log(home, {**base, "outcome": "error", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return 0


def _tell_tree(hit: tuple[str, str], *, event: str, active: bool, key_dir: Path, lane: str,
               grant: Mapping[str, Any], home: Path, base: dict[str, Any], err) -> int:
    """A tree refusal (L3 r2 #1), already recorded: the same once-only shape as a remote's rejection. Only a first
    ``Stop`` (not ``stop_hook_active``, never ``SessionEnd``) exits 2 with :data:`_TREE_TOLD`, so the model can drop the
    commits; the judged tip is then remembered against this grant, so it is not judged or told again until the seat
    moves the branch (or another grant is signed). A refusal nobody heard is judged again at the next hook."""
    path, judged = hit
    if event != "Stop" or active:
        return 0
    try:
        _remember(key_dir, lane, judged, grant)
    except (OSError, TypeError, ValueError) as exc:
        _log(home, {**base, "outcome": "error", "detail": f"the refused-tip mark was not written: "
                                                          f"{type(exc).__name__}: {str(exc)[:200]}"})
    print(_TREE_TOLD.format(path=path), file=err)
    return 2


def _settle(result: push.PushResult, *, event: str, active: bool, key_dir: Path, lane: str, grant: Mapping[str, Any],
            home: Path, base: dict[str, Any], err) -> int:
    """The exit code for a push git reported on. Pushed, or rejected AND the model told: the tip is remembered against this grant (a
    told rejection is not retried or re-told until the seat moves it, or another grant is signed; a rejection nobody heard,
    on SessionEnd or under stop_hook_active, is retried at the next hook). Only a REJECTION
    (``status == "rejected"``: a non-fast-forward or the remote's own rule) on a first ``Stop`` exits 2 with the
    rebase text; a transport, auth or DNS failure (``"error"``) is the receipt's record, is retried at the next Stop
    and ends 0. A remember that fails never costs the model its one rejection message."""
    told = result.status == "rejected" and event == "Stop" and not active     # the model hears a rejection only here
    if result.ok or told:
        try:
            _remember(key_dir, lane, result.new_sha, grant)
        except (OSError, TypeError, ValueError) as exc:
            _log(home, {**base, "outcome": "error", "detail": f"the pushed-tip mark was not written: "
                                                              f"{type(exc).__name__}: {str(exc)[:200]}"})
    if result.ok:
        return 0
    if told:
        print(_REJECT_TEXT.format(branch=base.get("branch"), detail=str(result.detail)[:300]), file=err)
        return 2
    return 0


def hook_entries(python: str, *, clone: Path, key_dir: Path, cockpit_root: Path | str, home: Path | str,
                 ) -> dict[str, list[dict[str, Any]]]:
    """The profile's ``hooks`` entries for the egress: ``Stop`` and ``SessionEnd``, each with an explicit timeout."""
    def entry(event: str, timeout: int) -> list[dict[str, Any]]:
        cmd = " ".join([shlex.quote(python), "-EP", "-B", "-m", "levain.seat_push", event,
                        "--clone", shlex.quote(str(clone)), "--key-dir", shlex.quote(str(key_dir)),
                        "--cockpit-root", shlex.quote(str(cockpit_root)), "--home", shlex.quote(str(home))])
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]
    return {"Stop": entry("Stop", STOP_TIMEOUT), "SessionEnd": entry("SessionEnd", SESSION_END_TIMEOUT)}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="levain.seat_push")
    ap.add_argument("event", choices=("Stop", "SessionEnd"))
    for name in ("clone", "key-dir", "cockpit-root", "home"):
        ap.add_argument(f"--{name}", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 0                                               # a malformed command line never blocks the seat
    try:
        text = sys.stdin.read(_STDIN_MAX) if not sys.stdin.isatty() else ""
    except Exception:  # noqa: BLE001
        text = ""
    return run_hook(a.event, text, clone=a.clone, key_dir=a.key_dir, cockpit_root=a.cockpit_root, home=a.home)


if __name__ == "__main__":
    sys.exit(main())
