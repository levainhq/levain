"""The seat-root lease and the clone launch (PATHZERO Z1d, ruling D2 a).

A terminal seat works in ONE lane clone plus its RETURN directory. Levain mints exactly those two roots, per lane,
under a signed lease: a ``seat-root-lease`` link in the cockpit's authority log (the kind lives in
``levain.authority``). The lease names the two parents the roots may sit under, pins the launcher key by sha256 and
lasts at most 14 days; its holder re-signs it, and the cockpit Grants tab's passkey revoke removes it. A revoke
refuses every LATER launch; a running seat keeps its roots (its settings were fixed at start, nothing here kills or
touches a minted root). A launch with no live lease refuses; it never falls back to a prompting or bypass launch.

Each mint is one row in ``<key dir>/mints.jsonl``: hash-chained, signed by the launcher key (an ed25519 key made at
the first lease create, private half 0600 in the key directory, which every seat profile denies reading). The clone and
the RETURN directory are created by the mint (an exclusive mkdir under the parent), never adopted from an existing
path, and the row pins the clone's ``.git`` identity (device, inode) so a later Levain git call can refuse a replaced
one; a ``.git`` renamed away, edited and renamed back keeps its identity, so that check never vouches for the
contents (canary P5 measures whether the rename is possible at all). :func:`prepare_launch` BUILDS AND VALIDATES a
launch and executes nothing; :func:`launch_seat` (PATHZERO Z5) starts ``claude`` from a prepared plan and :func:`stop_seat`
stops it, both recorded in the same chain (the ``start`` row).
"""
from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import host_guard as hg
from levain.authority import SEAT_LEASE_KIND, SEAT_LEASE_MAX, AuthoritySnapshot, NotInitialised, _parse_stamp, _thaw
from levain.seat_profile import SeatProfile, SeatRefusal, build_seat_profile

__all__ = ["SEAT_LEASE_KIND", "SeatLaunchRefused", "LaunchPlan", "live_lease", "key_dir_for", "ensure_launcher_key",
           "create_lease", "renew_lease", "mint_lane", "append_mint", "read_mints", "prepare_launch", "validate_launch",
           "launch_seat", "stop_seat", "stop_seats_for_grant", "seed_trust", "SIGN_COMMAND", "StopResult"]

SIGN_COMMAND = "levain seat lease create --lanes-parent <dir> --returns-parent <dir> --signing-key <key>"
_LANE_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_BASE_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_TIMEOUT = 600.0
_MINTS = "mints.jsonl"
_KEY = "launcher.ed25519"


class SeatLaunchRefused(Exception):
    """A seat launch is refused before anything ran. ``str()`` is the reason shown to the operator."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --- the lease ---------------------------------------------------------------------------------------------------

def live_lease(state: AuthoritySnapshot, now: datetime) -> Mapping[str, Any] | None:
    """The one live seat-root lease the verified replay ``state`` holds at ``now`` (its fields, plus ``incarnation``:
    the seq of the link that brought its id into the policy), or None. More than one live is refused (the log refuses
    to append a second; a foreign writer could leave two): which one a launch means is not an answer."""
    live = []
    for gid, grant in state.policy.grants.items():
        if not (isinstance(grant, Mapping) and grant.get("kind") == SEAT_LEASE_KIND):
            continue
        try:
            if _parse_stamp(grant["expires_at"], "expires_at") > now:
                live.append((gid, grant))
        except Exception:  # noqa: BLE001 - a lease whose expiry cannot be read is not live
            continue
    if len(live) > 1:
        raise SeatLaunchRefused("the authority log holds more than one live seat-root lease: revoke all but one")
    if not live:
        return None
    gid, grant = live[0]
    return {**_thaw(grant), "incarnation": state.grant_born.get(gid, 0)}


def _replay(cockpit_root: Path | str) -> AuthoritySnapshot | None:
    """The verified, anchored replay, or None when no cockpit journal (or an empty log) exists. Any other failure
    refuses: a log that cannot be read is never read as "no lease" quietly."""
    from levain.agent_authority import AgentAuthority
    from levain.cockpit.firejournal import DB_NAME
    if not (Path(cockpit_root) / DB_NAME).is_file():
        return None
    try:
        return AgentAuthority(cockpit_root).replay()
    except NotInitialised:
        return None
    except Exception as exc:  # noqa: BLE001
        raise SeatLaunchRefused(f"the authority log cannot be read ({type(exc).__name__}: {str(exc)[:160]})") from None


# --- the launcher key ----------------------------------------------------------------------------------------------

def key_dir_for(install: Path | str, roots: tuple[Path, ...]) -> Path:
    """``<host state dir of the install>/seat_launcher``, outside every root a seat can write (created 0700)."""
    d = hg.host_dir(install, "state", roots=roots) / "seat_launcher"
    d.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SeatLaunchRefused(f"the launcher key directory {str(d)!r} is a link, foreign-owned or not private")
    return d


def _load_key(key_dir: Path):
    from cryptography.hazmat.primitives import serialization
    path = key_dir / _KEY
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077 or not stat.S_ISREG(st.st_mode):
            raise SeatLaunchRefused(f"the launcher key {str(path)!r} is not a private file of this user")
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        key = None
    if not isinstance(key, Ed25519PrivateKey):
        raise SeatLaunchRefused(f"the launcher key {str(path)!r} is not an ed25519 key: remove it and sign a new lease")
    return key


def _pub_sha256(key: Any) -> str:
    from cryptography.hazmat.primitives import serialization
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(pub).hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    """os.write may return a short count without raising; loop until every byte is written (a torn key or mint row is worse than a failure)."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


def ensure_launcher_key(key_dir: Path) -> str:
    """Make the launcher ed25519 key if absent; return its public half's sha256 (what a lease pins). The key is written
    whole to a private temp file, fsynced, then published by one ``link`` that fails if a key is already there, so no
    reader ever sees a half-written key and two racing creators agree on the first."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = key_dir / _KEY
    if os.path.lexists(path):
        return _pub_sha256(_load_key(key_dir))
    key = Ed25519PrivateKey.generate()
    tmp = key_dir / f".{_KEY}.{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            _write_all(fd, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _pub_sha256(_load_key(key_dir))
    finally:
        os.unlink(tmp)
    return _pub_sha256(key)


# --- sign / renew a lease (the cockpit key commands' own append) ---------------------------------------------------

def create_lease(cockpit_root: Path | str, *, lanes_parent: Path | str, returns_parent: Path | str, key_dir: Path,
                 signer_fp: str, sign: Callable[[bytes], str], clock: Callable[[], datetime],
                 lease_id: str | None = None) -> Mapping[str, Any]:
    """Sign a 14-day ``seat-root-lease`` (a laptop-presence key). Both parents are made 0700 if absent and stored as
    their realpaths. The log's own rules judge the rest before anything is signed (:func:`levain.authority._seat_lease_check`)."""
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.keys import _append, _capture, _journal, _now, _signing_authority
    now = _now(clock)
    parents = []
    for p in (lanes_parent, returns_parent):
        p = Path(os.path.expanduser(str(p)))
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        parents.append(str(Path(os.path.realpath(p))))
    grant = {"id": lease_id or "sl-" + secrets.token_hex(6), "kind": SEAT_LEASE_KIND, "agent": "claude_code",
             "lanes_parent": parents[0], "returns_parent": parents[1],
             "launcher_key_sha256": ensure_launcher_key(key_dir), "expires_at": utc_stamp(now + SEAT_LEASE_MAX)}
    db = _journal(cockpit_root)
    _signing_authority(_capture(db, now, DEFAULT_SSH_KEYGEN))
    new = _append(db, {"op": "create_grant", "grant": grant}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=DEFAULT_SSH_KEYGEN)
    return _thaw(new.policy.grants[grant["id"]])


def renew_lease(cockpit_root: Path | str, lease_id: str, *, signer_fp: str, sign: Callable[[bytes], str],
                clock: Callable[[], datetime], max_life: timedelta = SEAT_LEASE_MAX) -> str:
    """Re-sign a live lease for 14 more days (its terms stay; ``max_life`` is the kind's own cap when another fixed-term
    kind reuses this, :func:`levain.seat_push.renew_egress`). Returns the new ``expires_at``.

    A renew's ``signed_at`` must equal its link's ``at``, so the link is prepared at the SAME clock reading the op
    carries, and appended at a fresh reading taken after the signature (a lease that lapsed during the touch is
    refused), the shape of ``levain.cli._signed_append``."""
    from levain.authority import AuthorityError, VerifierUnavailable, anchor_path, append, prepare
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.keys import KeyError_, _journal, _now
    now = _now(clock)
    op = {"op": "renew", "grant_id": lease_id, "expires_at": utc_stamp(now + max_life), "signed_at": utc_stamp(now)}
    db = _journal(cockpit_root)
    try:
        with db.autocommit() as conn:
            link = prepare(conn, op, signer_fp=signer_fp, now=now, ssh_keygen=DEFAULT_SSH_KEYGEN)
        signature = sign(link.message)
        with db.autocommit() as conn:
            append(conn, link, signature, now=_now(clock), anchor=anchor_path(db.data_root),
                   ssh_keygen=DEFAULT_SSH_KEYGEN)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None
    return op["expires_at"]


# --- the mint ------------------------------------------------------------------------------------------------------

def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "LC_ALL": "C"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env, text=True,
                          capture_output=True, timeout=_GIT_TIMEOUT, check=False)


def _real_dir(path: str, what: str) -> Path:
    p = Path(path)
    st = os.lstat(p) if os.path.lexists(p) else None
    if st is None or not stat.S_ISDIR(st.st_mode) or Path(os.path.realpath(p)) != p:
        raise SeatLaunchRefused(f"the lease's {what} {path!r} is not a real directory")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        # another user who can write the parent could swap a root between its mkdir and the clone (L3 z1 r1 codex 8)
        raise SeatLaunchRefused(f"the lease's {what} {path!r} is not this user's, or others can write it")
    return p


def _mkdir_exclusive(parent: Path, name: str) -> Path:
    path = parent / name
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        raise SeatLaunchRefused(f"{str(path)!r} already exists: a mint creates its roots, never adopts one") from None
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise SeatLaunchRefused(f"{str(path)!r} is a link")
    return path


def mint_lane(lease: Mapping[str, Any], lane_key: str, remote_url: str, base_sha: str, *, now: datetime,
              ) -> tuple[Path, Path, dict[str, Any]]:
    """Create the lane's two roots under the lease's parents and return ``(clone, return_dir, mint row)``: the clone
    (``git clone --no-hardlinks`` with no hooks or templates, then a detached checkout of ``base_sha``) at
    ``<lanes_parent>/<lane_key>`` and an empty RETURN directory at ``<returns_parent>/<lane_key>``, both made here with
    an exclusive mkdir. The row (before the profile digest, which the caller adds) pins the lease id and incarnation,
    the clone's ``.git`` (device, inode) and the source. On any failure the roots this call made are removed."""
    if not _LANE_KEY.fullmatch(lane_key or ""):
        raise SeatLaunchRefused("the lane key must be 1 to 64 characters of a-z, 0-9, '.', '_', '-', not starting with a symbol")
    if not _BASE_SHA.fullmatch(base_sha or ""):
        raise SeatLaunchRefused("the base sha must be a full lowercase hex commit id")
    if not remote_url or remote_url.startswith("-") or "\0" in remote_url:
        raise SeatLaunchRefused("the source remote is not a usable url or path")
    lanes = _real_dir(lease["lanes_parent"], "lanes_parent")
    returns = _real_dir(lease["returns_parent"], "returns_parent")
    made: list[Path] = []
    try:
        clone = _mkdir_exclusive(lanes, lane_key)
        made.append(clone)
        return_dir = _mkdir_exclusive(returns, lane_key)
        made.append(return_dir)
        for step in (("clone", "--quiet", "--no-hardlinks", "--no-checkout", "--template=", "--", remote_url, str(clone)),):
            r = _git(*step)
            if r.returncode:
                raise SeatLaunchRefused(f"git clone failed: {r.stderr.strip()[:300]}")
        r = _git("checkout", "--quiet", "--detach", base_sha, cwd=clone)
        if r.returncode:
            raise SeatLaunchRefused(f"base {base_sha[:12]} is not in the source: {r.stderr.strip()[:300]}")
        gst = os.lstat(clone / ".git")
        if not stat.S_ISDIR(gst.st_mode):
            raise SeatLaunchRefused("the clone's .git is not a real directory")
        row = {"v": 1, "lease_id": lease["id"], "incarnation": lease.get("incarnation", 0), "lane_key": lane_key,
               "clone": str(clone), "git": [gst.st_dev, gst.st_ino], "return_dir": str(return_dir),
               "remote": remote_url, "base_sha": base_sha,
               "at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        return clone, return_dir, row
    except BaseException:
        for p in reversed(made):
            shutil.rmtree(p, ignore_errors=True)
        raise


def append_mint(key_dir: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    """Append ``row`` to the launcher's mint chain: ``prev`` the last row's hash, ``hash`` over the row, ``sig`` the
    launcher key's ed25519 signature of that hash. Serialised by a lock on the file. The whole chain is verified under
    the lock first (a torn or forged row refuses the launch), and a write that fails is truncated back to where it
    started, so a failed append never leaves a partial row."""
    key = _load_key(key_dir)
    path = key_dir / _MINTS
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read()
        try:
            rows = _verify_chain(data, key.public_key())
        except ValueError as exc:
            raise SeatLaunchRefused(f"{exc}: {str(path)!r} needs a look before another launch") from None
        body = {**row, "prev": rows[-1]["hash"] if rows else ""}
        digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
        out = {**body, "hash": digest, "sig": key.sign(bytes.fromhex(digest)).hex()}
        try:
            _write_all(fd, (_canonical(out) + "\n").encode())
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, len(data))
            raise
        return out
    finally:
        os.close(fd)


def _verify_chain(data: bytes, pub: Any) -> list[dict[str, Any]]:
    """The rows of a mint file, each one's ``prev``, ``hash`` and signature checked; any fault raises ``ValueError``."""
    from cryptography.exceptions import InvalidSignature
    rows, prev = [], ""
    for ln in data.splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
            body = {k: v for k, v in row.items() if k not in ("hash", "sig")}
            digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
            if body.get("prev") != prev or digest != row.get("hash"):
                raise ValueError
            pub.verify(bytes.fromhex(row["sig"]), bytes.fromhex(digest))
        except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature):
            raise ValueError("the mint chain is broken") from None
        rows.append(row)
        prev = digest
    return rows


def read_mints(key_dir: Path) -> list[dict[str, Any]]:
    """Every mint row, with the chain and each signature verified; a broken chain raises ``ValueError``."""
    key = _load_key(key_dir)
    path = key_dir / _MINTS
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    try:
        # the same file lock :func:`append_mint` holds EXCLUSIVE across its verify-write-fsync: a reader never sees a
        # half-written row of a launch in flight (L3 zc r1 codex 4 / deepseek)
        fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read()
    finally:
        os.close(fd)
    return _verify_chain(data, key.public_key())


# --- the launch (build and validate only) --------------------------------------------------------------------------

class LaunchPlan:
    """What a validated launch would run: the profile, its digest and the mint row already recorded."""

    def __init__(self, profile: SeatProfile, mint: Mapping[str, Any], key_dir: Path | None = None) -> None:
        self.profile, self.mint, self.key_dir = profile, mint, key_dir

    @property
    def digest(self) -> str:
        return str(self.mint["profile_digest"])

    @property
    def egress_grant(self) -> str | None:
        """The seat-egress grant id the profile was built under (None: no grant; a row from before Z5 has none)."""
        return self.mint.get("egress_grant")


def prepare_launch(lane_key: str, remote_url: str, base_sha: str, *, claude_bin: str, python: str,
                   cockpit_root: Path | str, install: Path | str, now: datetime | None = None,
                   **profile_kw: Any) -> LaunchPlan:
    """Check the lease, mint the roots, build the profile through the one builder and record the mint. Raises
    :class:`SeatLaunchRefused` (no live lease, a lease whose key is not the launcher's, a mint problem) or
    :class:`levain.seat_profile.SeatRefusal` (the profile); on either, the roots this call made are removed and no
    mint row is written. Nothing is executed.

    The clone can run for minutes, so the lease is read again after it, at a fresh clock (``now``, when a test passes
    one): a lease revoked, expired or replaced meanwhile refuses and the roots are removed. A seat is "running" only
    once the plan is returned; nothing here outlives a revoke that landed before that."""
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    cockpit_root = Path(os.path.abspath(cockpit_root))   # the hooks run from the clone: a relative root would replay nothing there
    state = _replay(cockpit_root)
    lease = None if state is None else live_lease(state, clock())
    if lease is None:
        raise SeatLaunchRefused(f"no live seat-root lease: a launch needs one, and never falls back to a prompt. "
                                f"Sign one: {SIGN_COMMAND}")
    lanes, returns = Path(lease["lanes_parent"]), Path(lease["returns_parent"])
    key_dir = key_dir_for(install, (lanes, returns))
    try:
        pinned = _pub_sha256(_load_key(key_dir))
    except FileNotFoundError:
        raise SeatLaunchRefused("the launcher key is missing: the lease cannot be used; sign a new one") from None
    if pinned != lease["launcher_key_sha256"]:
        raise SeatLaunchRefused("the launcher key is not the one the lease pins: sign a new lease")
    clone, return_dir, row = mint_lane(lease, lane_key, remote_url, base_sha, now=clock())
    try:
        state = _replay(cockpit_root)    # the post-clone replay: the egress below is read from THIS one too
        again = None if state is None else live_lease(state, clock())
        if again is None or (again["id"], again["incarnation"]) != (lease["id"], lease["incarnation"]):
            raise SeatLaunchRefused("the seat-root lease is no longer live (revoked, expired or replaced while the "
                                    "clone ran): nothing was launched")
        egress = None                    # Z2a: the live seat-egress grant, if one is signed; none = today's profile
        if state is not None:
            from levain import seat_push
            try:
                egress = seat_push.live_egress(state, clock())
            except seat_push.SeatPushRefused as exc:
                raise SeatLaunchRefused(str(exc)) from None
        if egress is not None:
            profile_kw = {"cockpit_root": cockpit_root, "levain_home": Path(os.path.abspath(_levain_home())), **profile_kw}
        profile = build_seat_profile(clone, return_dir, claude_bin=claude_bin, python=python, lease=lease,
                                     key_dir=key_dir, egress=egress, **profile_kw)
        row = {**row, "profile_digest": profile.digest(), "claude": _binary_ident(claude_bin),
               "egress_grant": None if egress is None else egress["id"]}
        mint = append_mint(key_dir, row)
    except BaseException:
        for p in (return_dir, clone):
            shutil.rmtree(p, ignore_errors=True)
        raise
    return LaunchPlan(profile, mint, key_dir)


def _levain_home() -> Path:
    from levain.consent.queue import default_queue_dir
    return default_queue_dir().parent


def _binary_ident(claude_bin: str) -> list[int]:
    try:
        st = os.stat(os.path.realpath(claude_bin))
        return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
    except OSError:
        return []




def validate_launch(lane_key: str, remote_url: str, base_sha: str, *, claude_bin: str, python: str,
                    cockpit_root: Path | str, install: Path | str, out=None, err=None, **kw: Any) -> int:
    """:func:`prepare_launch`, then print the argv and the digest. Starts nothing (``levain seat launch --dry-run``; the
    roots it mints stay, with their mint row). Returns 0, or 1 with the refusal on ``err``."""
    out, err = out or sys.stdout, err or sys.stderr
    try:
        plan = prepare_launch(lane_key, remote_url, base_sha, claude_bin=claude_bin, python=python,
                              cockpit_root=cockpit_root, install=install, **kw)
    except (SeatLaunchRefused, SeatRefusal) as exc:
        print(f"seat launch refused: {exc}", file=err)
        return 1
    print(f"seat launch validated (not started): lane {lane_key}", file=out)
    print(f"  clone   {plan.profile.clone}\n  return  {plan.profile.return_dir}\n  digest  {plan.digest}", file=out)
    print("  argv    " + json.dumps(plan.profile.argv()), file=out)
    return 0


# --- the start and the stop (PATHZERO Z5) ---------------------------------------------------------------------------

#: The seat starts as ``sh``, which stops ITSELF before it execs ``claude``: the launcher records the process in the mint
#: chain while it is stopped, then continues it. ``exec`` keeps the pid and the process start time, so the recorded
#: identity is the one ``claude`` runs under. A launcher that dies in that window leaves a stopped ``sh`` that has run
#: nothing; a seat is never live and unrecorded.
_WRAP = 'kill -STOP $$; exec "$@"'
_STOP_WAIT = 15.0
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC", "PATH": "/bin:/usr/bin"}


def _proc_start_ns(pid: int) -> int | None:
    """The process's OWN start time (epoch ns, ``ps -o lstart=``: one-second resolution), or None when no such process.
    Never the clock at spawn: a reused pid has a different start time, which is what makes a recorded pid safe to signal."""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], env=_PS_ENV, text=True, capture_output=True,
                       timeout=10, check=False)
    text = r.stdout.strip()
    if not text:
        return None
    try:
        return calendar.timegm(time.strptime(text, "%a %b %d %H:%M:%S %Y")) * 1_000_000_000
    except ValueError:
        raise SeatLaunchRefused(f"the start time of process {pid} cannot be read ({text[:60]!r})") from None


def seed_trust(home: Path | str, clone: Path | str) -> Path:
    """Make ``clone`` a trusted workspace for the seat whose HOME is ``home``: claude refuses ``--bg`` and an interactive
    start in an untrusted path ("Workspace not trusted"; 1106-16/z1e), and a minted clone is a new path every time. Merges
    exactly ``projects[<clone>].hasTrustDialogAccepted = true`` into ``<home>/.claude.json`` (and the first-run
    ``hasCompletedOnboarding`` / ``theme`` only if the file lacks them), which is what the vendor writes when a person
    accepts the dialog. Everything else in the file is kept; a file that cannot be read as a JSON object is refused,
    never overwritten; written whole to a private temp and renamed, 0600. It touches that one file and nothing else."""
    home = Path(home)
    if not home.is_absolute() or not home.is_dir() or home.is_symlink():
        raise SeatLaunchRefused(f"the seat HOME {str(home)!r} is not an absolute, real directory")
    path = home / ".claude.json"
    data: Any = {}
    if os.path.lexists(path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                data = json.loads(os.read(fd, 16 * 1024 * 1024) or b"{}")
            finally:
                os.close(fd)
        except (OSError, ValueError):
            raise SeatLaunchRefused(f"{str(path)!r} cannot be read as JSON (or is a link): the trust was not seeded, "
                                    "and the file was not touched") from None
    projects = data.get("projects", {}) if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise SeatLaunchRefused(f"{str(path)!r} is not a JSON object with an object 'projects': not touched")
    data.setdefault("hasCompletedOnboarding", True)
    data.setdefault("theme", "dark")
    entry = projects.get(str(clone))
    projects[str(clone)] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
    data["projects"] = projects
    tmp = home / f".claude.json.levain-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            _write_all(fd, json.dumps(data, indent=2).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def launch_seat(plan: LaunchPlan, *, prompt: str | None = None, model: str | None = None, headless: bool = False,
                stop_wait: float = _STOP_WAIT) -> dict[str, Any]:
    """Start the seat ``plan`` describes (``prepare_launch`` already minted its roots and recorded the mint) and return
    ``{lane, pid, pgid, started_at_ns, clone, return_dir, log, row}``.

    ``claude`` runs with ``plan.profile.argv(...)`` and ``plan.profile.env`` in the clone, in its own session and process
    group, stdin closed, stdout and stderr to ``<RETURN>/seat-launch.log``. The sequence: seed the trust; spawn the
    stopped wrapper (:data:`_WRAP`); read its own start time; append the ``start`` row (pid, pgid, start time, the egress
    grant id, the claude identity) to the chain; only then SIGCONT. A failure at any step before the continue kills the
    group and removes the roots this launch minted (the mint row stays: the audit trail of a launch that did not run).
    The caller must treat the returned seat as running."""
    if plan.key_dir is None:
        raise SeatLaunchRefused("this plan carries no launcher key directory: it was not made by prepare_launch")
    profile, lane = plan.profile, str(plan.mint["lane_key"])
    clone, return_dir = profile.clone, profile.return_dir
    if prompt is not None and (prompt.startswith("-") or "\0" in prompt):
        raise SeatLaunchRefused("the prompt must not start with '-' (it would read as a flag) or hold a NUL")
    pgid = 0
    log = return_dir / "seat-launch.log"
    try:
        home = profile.env.get("HOME", "")
        seed_trust(home, clone)
        argv = [*profile.argv(headless=headless, model=model), *([prompt] if prompt else [])]
        logfd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", _WRAP, "sh", *argv], cwd=clone, env=dict(profile.env),
                                    stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd, start_new_session=True)
        finally:
            os.close(logfd)
        pid = pgid = proc.pid
        deadline = time.monotonic() + stop_wait
        while True:
            done, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
            if done and os.WIFSTOPPED(status):
                break
            if done:
                pgid = 0
                raise SeatLaunchRefused(f"the seat process exited before it started (status {status}): see {str(log)!r}")
            if time.monotonic() > deadline:
                raise SeatLaunchRefused("the seat process did not reach its start gate in time")
            time.sleep(0.01)
        started = _proc_start_ns(pid)
        if started is None or os.getpgid(pid) != pgid:
            raise SeatLaunchRefused("the stopped seat process is not the group leader the row would record")
        row = append_mint(plan.key_dir, {
            "v": 1, "ev": "start", "lane_key": lane, "lease_id": plan.mint["lease_id"], "mint_hash": plan.mint["hash"],
            "clone_path": str(clone), "pid": pid, "pgid": pgid, "started_at_ns": started, "log": str(log),
            "profile_digest": plan.digest, "claude": plan.mint.get("claude", []),
            "egress_grant": plan.mint.get("egress_grant"), "headless": bool(headless),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
        os.kill(pid, signal.SIGCONT)
    except BaseException:
        if pgid > 1:
            _kill_group(pgid, signal.SIGKILL)
            _reap(pgid)
        for p in (return_dir, clone):
            shutil.rmtree(p, ignore_errors=True)
        raise
    return {"lane": lane, "pid": pid, "pgid": pgid, "started_at_ns": started, "clone": str(clone),
            "return_dir": str(return_dir), "log": str(log), "row": row}


class StopResult:
    """What :func:`stop_seat` did: ``status`` is ``stopped`` (a signal ended it) or ``gone`` (nothing to signal)."""

    def __init__(self, lane: str, status: str, detail: str, pgid: int | None = None) -> None:
        self.lane, self.status, self.detail, self.pgid = lane, status, detail, pgid

    def __str__(self) -> str:
        return f"seat {self.lane}: {self.status}: {self.detail}"


def _start_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The NEWEST ``start`` row of each lane."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("ev") == "start" and isinstance(r.get("lane_key"), str):
            out[r["lane_key"]] = r
    return out


def _group_alive(pgid: int, *, signalled: bool = False) -> bool:
    """Whether process group ``pgid`` has a member. EPERM before we signalled it means a group that is not this user's
    (refused); after our own SIGTERM it is macOS answering for a group of zombies, which is dead for our purpose."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        if signalled:
            return False
        raise SeatLaunchRefused(f"process group {pgid} is not this user's: nothing signalled") from None
    return True


def _stop_row(row: Mapping[str, Any], *, grace: float, poll: float = 0.05) -> StopResult:
    lane, pid, pgid = str(row["lane_key"]), row.get("pid"), row.get("pgid")
    if not (isinstance(pid, int) and isinstance(pgid, int) and pid > 1 and pgid > 1 and isinstance(row.get("started_at_ns"), int)):
        raise SeatLaunchRefused(f"the start row of lane {lane!r} carries no usable pid, pgid and start time: nothing signalled")
    now = _proc_start_ns(pid)
    if now is not None and now != row["started_at_ns"]:
        raise SeatLaunchRefused(f"pid {pid} now has a different start time than lane {lane!r} recorded (the pid was "
                                "reused): nothing signalled")
    if now is not None:
        try:
            if os.getpgid(pid) != pgid:
                raise SeatLaunchRefused(f"pid {pid} is no longer in the recorded process group {pgid}: nothing signalled")
        except ProcessLookupError:
            pass
    _reap(pid)
    # a pid cannot be reused while a process group of that id has members, so a group that is still alive with the
    # leader gone holds only the seat's own descendants
    if not _group_alive(pgid):
        return StopResult(lane, "gone", f"process group {pgid} had already exited; nothing to signal", pgid)
    _kill_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap(pid)
        if not _group_alive(pgid, signalled=True):
            return StopResult(lane, "stopped", f"SIGTERM to process group {pgid}; it exited", pgid)
        time.sleep(poll)
    _kill_group(pgid, signal.SIGKILL)
    for _ in range(100):
        _reap(pid)
        if not _group_alive(pgid, signalled=True):
            return StopResult(lane, "stopped", f"SIGTERM to process group {pgid} was not enough in {grace:g}s; SIGKILL ended it", pgid)
        time.sleep(poll)
    raise SeatLaunchRefused(f"process group {pgid} of lane {lane!r} survived SIGKILL")


def stop_seat(key_dir: Path, lane_key: str, *, grace: float = 10.0) -> StopResult:
    """Stop the lane's seat: the NEWEST ``start`` row for ``lane_key`` in the verified mint chain names the pid, the
    process group and the process's own start time. The group is signalled (SIGTERM, ``grace`` seconds, then SIGKILL)
    only if the pid still has that start time (a reused pid is NEVER signalled: refused). A seat already gone is a
    success (``gone``). Raises :class:`SeatLaunchRefused` for no such row, a reused pid, or a group that survives."""
    try:
        rows = read_mints(key_dir)
    except (ValueError, OSError) as exc:
        raise SeatLaunchRefused(f"the mint chain cannot be verified ({type(exc).__name__}: {str(exc)[:120]})") from None
    row = _start_rows(rows).get(lane_key)
    if row is None:
        raise SeatLaunchRefused(f"no launched seat is recorded for lane {lane_key!r}: nothing signalled")
    return _stop_row(row, grace=grace)


def stop_seats_for_grant(key_dir: Path, grant_id: str, *, grace: float = 10.0) -> list[StopResult]:
    """Stop every seat whose newest ``start`` row records egress grant ``grant_id`` (the post-step of ``levain seat egress
    revoke``: the grant's ``allowedDomains`` are frozen in the launch settings, so the revoke cuts the push, not the
    seat's network). A seat launched under no grant, or another, is untouched. One seat that cannot be stopped is
    reported as a ``refused`` result and the rest are still tried."""
    if not os.path.lexists(key_dir / _KEY):
        return []                       # no launcher key, so no seat was ever launched from this install
    try:
        rows = read_mints(key_dir)
    except (ValueError, OSError) as exc:
        raise SeatLaunchRefused(f"the mint chain cannot be verified ({type(exc).__name__}: {str(exc)[:120]})") from None
    out = []
    for lane, row in _start_rows(rows).items():
        if row.get("egress_grant") != grant_id:
            continue
        try:
            out.append(_stop_row(row, grace=grace))
        except SeatLaunchRefused as exc:
            out.append(StopResult(lane, "refused", str(exc)))
    return out
