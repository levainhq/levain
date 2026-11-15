"""`levain hands mcp register` and `levain hands mcp revoke`: the two signed writes a hands-MCP registry entry needs.

Consent-boundary code. What each verb signs, and what it refuses:

* ``register`` signs ONE link: ``{"op": "create_grant", "grant": <entry>}`` where the entry is exactly what
  :func:`levain.hands_mcp.build_entry` makes from the operator's arguments (name, transport, url or command, identity
  kind, lease expiry, optionally the id). The signature is made by the key ``--signing-key`` names, through the one
  signer the other key verbs use (:func:`levain.cockpit.keys.ssh_signer`): a passphrase-custody key makes ssh-keygen ask
  on the terminal for every signature and nothing caches it; a touch key asks for its touch. This module adds no
  password argument, no environment variable, no agent and no cache. With no terminal the signature fails and nothing is
  written. Refused before anything is signed: a log that cannot be read or is not a reported install's; a name the log
  already holds in ANY state (live, expired or stopped: the log counts a stopped entry until it is revoked, so the
  refusal names the revoke); an id the log already holds; a signer that is not an enrolled laptop-presence key (the
  log's own rule); an entry the log's append rules refuse (a stdio command not in a trusted directory, a secret-shaped
  url or argument, a bad name or id, a lease past the cap).
* ``revoke`` signs ONE link: ``{"op": "revoke_grant", "grant_id": <id>}``, and only for a grant whose kind is
  ``hands-mcp``. Any enrolled key may sign a revoke (the log's rule). Refused before anything is signed: a log that cannot
  be read; a target that names no hands-MCP entry (a consent grant, a seat lease, a key: never revoked here, whatever its
  id); a name that matches more than one entry. Revoke ends the entry at once; the id may be created again by
  ``register --id``.

Neither verb touches the hands account, the vendor, the launch files or the entity's files: they write the authority log
and nothing else."""
from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import hands_mcp as hm

DEFAULT_LEASE_DAYS = 30
MAX_LEASE_DAYS = 30             # the log's cap on a grant's life past its link (authority _CONSENT_GRANT_MAX)
_ID_PREFIX = "hm-"
_ID_SHAPE = re.compile(r"hm-[0-9a-f]{12}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log_entries(db: Any, ssh_keygen: Any, clock: Any = _utc_now) -> tuple[Any, dict[str, dict[str, Any]]]:
    """``(snapshot, {id: thawed hands-mcp grant})`` from the journal's verified replay, or a KeyError_ refusal."""
    from levain.authority import HANDS_MCP_KIND, _thaw
    from levain.cockpit.keys import _capture, _signing_authority
    state = _signing_authority(_capture(db, clock(), ssh_keygen))
    held = {gid: _thaw(g) for gid, g in state.policy.grants.items()
            if isinstance(g, Mapping) and g.get("kind") == HANDS_MCP_KIND}
    return state, held


def register(data_root: Path | str, *, name: str, transport: str, identity_kind: str, url: str = "", command: str = "",
             args: tuple[str, ...] = (), entry_id: str | None = None, days: int = DEFAULT_LEASE_DAYS,
             signer_fp: str, sign: Any, clock: Any = _utc_now,
             ssh_keygen: Path | str | None = None, marker_directory: Path | None = None) -> tuple[dict[str, Any], int, str | None]:
    """Sign and append the ``create_grant`` link of a new hands-MCP entry. Returns ``(entry, seq, anchor warning)``.
    Raises ``KeyError_`` (or the log's own refusals) before anything is signed when the entry is not acceptable."""
    from levain.authority import AuthorityError
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.keys import KeyError_, _append, _journal
    ssh_keygen = DEFAULT_SSH_KEYGEN if ssh_keygen is None else ssh_keygen
    # checked before any timedelta is built: a huge --days overflowed into a traceback (leg 6 L3 r1, gemini/codex LOW)
    if not isinstance(days, int) or not 1 <= days <= MAX_LEASE_DAYS:
        raise KeyError_(f"--days must be a whole number from 1 to {MAX_LEASE_DAYS} (the log's lease cap), "
                        f"not {str(days)[:20]}")
    if _ID_SHAPE.fullmatch(name):
        # resolve_target matches an id before a name, so such a name could make a revoke end another entry
        raise KeyError_(f"the name {name!r} looks like an entry id ({_ID_PREFIX}<12 hex digits>); choose another name")
    if entry_id is not None and not entry_id.startswith(_ID_PREFIX):
        raise KeyError_(f"an entry id looks like {_ID_PREFIX}<12 hex digits>, not {entry_id[:40]!r}")
    try:
        entry = hm.build_entry(name=name, transport=transport, identity_kind=identity_kind, url=url, command=command,
                               args=args, entry_id=entry_id,
                               expires_at=hm.lease_expiry(clock(), timedelta(days=days)))
    except hm.HandsMcpRefused as exc:
        raise KeyError_(str(exc)) from None
    db = _journal(data_root, marker_directory=marker_directory)
    _state, held = _log_entries(db, ssh_keygen, clock)
    for gid, g in sorted(held.items()):
        if gid == entry["id"]:
            raise KeyError_(f"the log already holds an entry with id {gid}: revoke it first "
                            f"(`levain hands mcp revoke {gid}`), then register again with --id {gid}")
        if g.get("name") == name:
            raise KeyError_(f"the log already holds a hands-MCP entry named {name!r} ({gid}), live, expired or "
                            f"stopped: one entry per name, and a stopped one still counts until it is revoked "
                            f"(`levain hands mcp revoke {gid}`)")
    try:
        new = _append(db, {"op": "create_grant", "grant": entry}, signer_fp=signer_fp, sign=sign, clock=clock,
                      ssh_keygen=ssh_keygen)
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None
    return entry, new.seq, new.anchor_warning


def resolve_target(held: Mapping[str, Mapping[str, Any]], target: str) -> str:
    """The id of the one hands-MCP entry ``target`` names (an id, or a name held by exactly one entry), or a refusal."""
    from levain.cockpit.keys import KeyError_
    if target in held:
        return target
    by_name = sorted(gid for gid, g in held.items() if g.get("name") == target)
    if len(by_name) == 1:
        return by_name[0]
    if by_name:
        raise KeyError_(f"{target!r} names {len(by_name)} hands-MCP entries ({', '.join(by_name)}): revoke by id")
    raise KeyError_(f"no hands-MCP entry {target[:80]!r} in the log (this verb revokes hands-MCP entries only, "
                    "never a consent grant, a lease or a key)")


def revoke(data_root: Path | str, target: str, *, signer_fp: str, sign: Any, clock: Any = _utc_now,
           ssh_keygen: Path | str | None = None, marker_directory: Path | None = None) -> tuple[str, int, str | None]:
    """Sign and append the ``revoke_grant`` link of one hands-MCP entry (``target``: its id or its name). Returns
    ``(id, seq, anchor warning)``."""
    from levain.authority import AuthorityError
    from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
    from levain.cockpit.keys import KeyError_, _append_at, _journal
    ssh_keygen = DEFAULT_SSH_KEYGEN if ssh_keygen is None else ssh_keygen
    db = _journal(data_root, marker_directory=marker_directory)
    state, held = _log_entries(db, ssh_keygen, clock)
    gid = resolve_target(held, target)
    try:
        # bound to the head the target was resolved on (leg 6 L3 r1, codex HIGH): an id revoked and made again
        # (as any kind) after that check is never the grant revoked here
        new = _append_at(db, {"op": "revoke_grant", "grant_id": gid}, state, signer_fp=signer_fp, sign=sign,
                         clock=clock, ssh_keygen=ssh_keygen, what=f"`levain hands mcp revoke {target[:80]}`")
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None
    return gid, new.seq, new.anchor_warning


def _root(path: Path, data_root: Path | None) -> Path:
    from levain.firing.hands_mcp_login import cockpit_root_of
    return Path(data_root) if data_root is not None else cockpit_root_of(path)


def _refusals() -> tuple[type[BaseException], ...]:
    import sqlite3

    from levain.authority import AuthorityError
    from levain.cockpit.firejournal import CockpitFormatError
    from levain.cockpit.keys import KeyError_
    return (KeyError_, AuthorityError, OSError, UnicodeDecodeError, sqlite3.Error, CockpitFormatError)


def cmd_register(args: Any) -> int:
    from levain.cockpit.keys import ssh_signer
    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        entry, seq, warning = register(
            _root(args.path, args.data_root), name=args.name, transport=args.transport,
            identity_kind=args.identity_kind, url=args.url or "", command=args.command or "",
            args=tuple(args.arg), entry_id=args.entry_id, days=args.days, signer_fp=signer_fp, sign=sign)
    except _refusals() as exc:
        print(f"register refused: {exc}", file=sys.stderr)
        return 1
    print(f"registered: {entry['id']} ({entry['name']}, {entry['transport']}, {entry['identity_kind']}) at seq {seq}, "
          f"until {entry['expires_at']}")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_revoke(args: Any) -> int:
    from levain.cockpit.keys import ssh_signer
    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        gid, seq, warning = revoke(_root(args.path, args.data_root), args.target, signer_fp=signer_fp, sign=sign)
    except _refusals() as exc:
        print(f"revoke refused: {exc}", file=sys.stderr)
        return 1
    print(f"revoked: {gid} at seq {seq}")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0
