"""The setup record's operations: what `levain chat key setup | revoke | list` runs (design §3.8.5(o) item 4; CH-D2 S6).

A setup record (`levain.authority.EGRESS_KEY_KIND`) names, for one entity's chat launches, ONE host whose https:443
requests may carry a credential Levain holds, the keychain item the service reads it from (a NAME: the value is
never asked for, passed or stored here), how srt places it (bearer, or basic with a user) and the repos the operator
declares beside the workspace's own remotes. It is a `create_grant` link a laptop-presence key signs. At most one
record per (entity, canonical host) is live (R15-21-4): a second is refused here, before anything is signed, and by
the log itself at append; `replace=True` is the supersession, one `loosen` link that drops the live record and adds
the new one, so exactly one is live after that one write. A record made live, or a replacement, takes effect at the
NEXT launch (the snapshot is per launch, R15-21-6); a revoke stops its key attaching from the next request
(R15-22-1). `revoke_grant` removes a record (any enrolled key)."""
from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import (EGRESS_KEY_KIND, AuthorityError, _thaw, egress_key_live, egress_key_record)
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.chat_egress.parse import canonical_host
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import KeyError_, Signer, _append, _capture, _journal, _now, _signing_authority

__all__ = ["SetupRefused", "list_keys", "revoke_key", "setup_key"]


class SetupRefused(KeyError_):
    """A setup, replace or revoke the record rules refuse, before anything is signed."""


def _host(host: str) -> str:
    try:
        canon = canonical_host(host)
    except ValueError as exc:
        raise SetupRefused(f"{host[:200]!r} is not a host name ({exc})") from None
    return canon


def setup_key(data_root: Path | str, *, entity: str, host: str, source: str, placement: Mapping[str, str],
              repos: Iterable[str], expires: timedelta | datetime, signer_fp: str, sign: Signer,
              clock: Callable[[], datetime], replace: bool = False, record_id: str | None = None,
              ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
              marker_directory: Path | None = None) -> tuple[dict[str, Any], str | None]:
    """Sign a setup record for `entity` and `host` (canonicalised: `Example.COM.` is `example.com`). `source` is the
    keychain item's name. `replace`: supersede the live record for that (entity, host) in the same link; without it
    a live one refuses the setup. Returns `(record, warning)`; `warning` says the link committed but its anchor did not
    advance. The log's rules judge the record before anything is signed."""
    now = _now(clock)
    if isinstance(expires, timedelta):
        if expires <= timedelta(0):
            raise SetupRefused("a setup record's life must be positive")
        expires_at = now + expires
    elif isinstance(expires, datetime) and expires.tzinfo is not None:
        expires_at = expires
    else:
        raise SetupRefused("expires is a duration or an aware time")
    canon = _host(host)
    rid = record_id if record_id is not None else "ek-" + secrets.token_hex(6)
    record = {"id": rid, "kind": EGRESS_KEY_KIND, "entity": entity, "host": canon, "source": source,
              "placement": dict(placement), "repos": sorted(set(repos)), "expires_at": utc_stamp(expires_at)}
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))
    live = sorted(gid for gid, g in state.policy.grants.items()
                  if egress_key_live(g, now) and g["entity"] == entity and g["host"] == canon)
    if rid in state.policy.grants:
        raise SetupRefused(f"a grant {rid!r} already exists")
    if live and not replace:
        raise SetupRefused(f"{entity[:80]!r} already has a live setup record for {canon} ({', '.join(live)}): pass "
                           "--replace to supersede it, or revoke it first")
    if replace and not live:
        raise SetupRefused(f"nothing to replace: {entity[:80]!r} has no live setup record for {canon}")
    if replace:
        policy = _thaw(state.policy.as_dict())
        for gid in live:
            del policy["grants"][gid]
        policy["grants"][rid] = record
        op: dict[str, Any] = {"op": "loosen", "policy": policy}
    else:
        op = {"op": "create_grant", "grant": record}
    try:
        new = _append(db, op, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:
        raise SetupRefused(str(exc)) from None
    return _thaw(new.policy.grants[rid]), new.anchor_warning


def revoke_key(data_root: Path | str, record_id: str, *, signer_fp: str, sign: Signer,
               clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
               marker_directory: Path | None = None) -> tuple[int, str | None]:
    """Sign a `revoke_grant` link removing the setup record `record_id` (any enrolled key). A launch already running
    stops attaching its key from its next request; the host stays keyed for that launch. Returns `(seq, warning)`."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    if not egress_key_record(state.policy.grants.get(record_id)):
        raise SetupRefused(f"no setup record {record_id[:80]!r}")
    try:
        new = _append(db, {"op": "revoke_grant", "grant_id": record_id}, signer_fp=signer_fp, sign=sign,
                      clock=clock, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:
        raise SetupRefused(str(exc)) from None
    return new.seq, new.anchor_warning


def list_keys(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
              marker_directory: Path | None = None) -> list[dict[str, Any]]:
    """Every setup record the replay holds, by id, each with `state` "live" or "expired" (no value: there is none)."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))
    out = []
    for gid, g in sorted(state.policy.grants.items()):
        if egress_key_record(g):
            out.append({**_thaw(g), "state": "live" if egress_key_live(g, now) else "expired",
                        "link_seq": state.grant_seqs.get(gid)})
    return out


# re-exported for the CLI's refusals
_REFUSALS = (SetupRefused, KeyError_, AuthorityError)
