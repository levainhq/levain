"""The setup record's operations: what `levain chat key setup | revoke | list` runs (design §3.8.5(o) item 4; CH-D2 S6).

A setup record (`levain.authority.EGRESS_KEY_KIND`) names, for one entity's chat launches, ONE host whose https:443
requests may carry a credential Levain holds, the keychain item the service reads it from (its service AND account:
NAMES, the value is never asked for, passed or stored here), how srt places it (bearer, or basic with a user) and its
declared repos: the operator's `--repo`s plus, when a workspace is named, its https remotes on that host. A launch
reads only that signed list, never the workspace's `.git/config` (K-8: the sandbox can write it); adding a repo is a
new setup (`replace=True`). It is a `create_grant` link a laptop-presence key signs.

The ceremony (K-8 obs 1 ruling, F6): the workspace's remotes are read ONCE; the record built from that one read is
SHOWN (with every remote on that host it left out, and why) and `confirm` is asked before anything is signed; what is
signed is exactly the record shown, never a re-read. A `replace` signs a full policy built from the snapshot it was
judged on, and is refused if the log moved since (F7). At most one record per (entity, canonical host) is live
(R15-21-4): a second is refused here, before anything is signed, and by the log itself at append; `replace=True` is
the supersession, one `loosen` link that drops the live record and adds the new one, so exactly one is live after
that one write. A record made live, or a replacement, takes effect at the NEXT launch (the snapshot is per launch,
R15-21-6); a revoke stops its key attaching from the next request (R15-22-1). `revoke_grant` removes a record (any
enrolled key)."""
from __future__ import annotations

import re
import secrets
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import (EGRESS_KEY_KIND, AuthorityError, _thaw, anchor_path, append, egress_key_live,
                              egress_key_record, prepare)
from levain.authority_sig import VerifierUnavailable
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.chat_egress.keys import _remote_repo, workspace_remote_urls
from levain.chat_egress.parse import canonical_host, canonical_path
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import KeyError_, Signer, _append, _capture, _journal, _now, _signing_authority

__all__ = ["Excluded", "KeychainItems", "SetupDeclined", "SetupPreview", "SetupRefused", "list_keys", "revoke_key",
           "setup_key"]


class SetupRefused(KeyError_):
    """A setup, replace or revoke the record rules refuse, before anything is signed."""


class SetupDeclined(SetupRefused):
    """The operator did not confirm the record shown: nothing was signed."""


@dataclass(frozen=True)
class Excluded:
    """A workspace remote on the setup's host that declares no repo, and why. `url` carries no userinfo (a token in
    a remote is never shown); both fields are sandbox-written text, shown only through `visible`."""
    url: str
    why: str


@dataclass(frozen=True)
class SetupPreview:
    """What `confirm` is shown: the record exactly as it will be signed, which of its repos came from the workspace's
    one read, the remotes on that host that read left out, and the live record a replace supersedes."""
    record: Mapping[str, Any]
    workspace: Path | None
    from_workspace: tuple[str, ...]
    excluded: tuple[Excluded, ...]
    replaces: tuple[str, ...]


class KeychainItems:
    """How many generic-password items match a service AND account across the user's keychain search list
    (`security list-keychains -d user`, then `security find-generic-password -s S -a A <keychain>` per keychain).
    Attributes only: no `-w`/`-g`, so no value is ever read. `find-generic-password` answers found or not found per
    keychain, never how many items in that keychain match, so the count is of keychains holding a match."""

    def __init__(self, security: str = "/usr/bin/security", timeout_s: float = 5.0) -> None:
        self._security = security
        self._timeout = timeout_s

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([self._security, *argv], capture_output=True, timeout=self._timeout, check=False,
                              env={"PATH": "/usr/bin:/bin"})

    def count(self, service: str, account: str) -> int:
        try:
            listed = self._run("list-keychains", "-d", "user")
            if listed.returncode != 0:
                raise SetupRefused("the keychain search list is unreadable (`security list-keychains` failed)")
            chains = re.findall(r'"([^"]+)"', listed.stdout.decode("utf-8", "replace"))
            if not chains:
                raise SetupRefused("the keychain search list is empty")
            hits = 0
            for chain in chains:
                out = self._run("find-generic-password", "-s", service, "-a", account, chain)
                hits += out.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            raise SetupRefused("the keychain did not answer") from None
        return hits


def _host(host: str) -> str:
    try:
        canon = canonical_host(host)
    except ValueError as exc:
        raise SetupRefused(f"{host[:200]!r} is not a host name ({exc})") from None
    return canon


# a remote URL's parts, for NAMING an exclusion only (the include decision is `keys._remote_repo`'s); no
# urllib.parse in this package (R15-12-B3: parse.py is the one parser)
_ANY_URL = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/?#]*@)?(?P<host>[^:/?#]*)(?::(?P<port>[^/?#]*))?"
                      r"(?P<path>[^?#]*)(?P<rest>[?#].*)?", re.DOTALL)
_SCP_LIKE = re.compile(r"(?:[^@/:]+@)?(?P<host>[^/:\[\]]+):(?!//)")


def _on_host(url: str, host: str) -> bool:
    """Whether a remote URL names canonical `host` at all (any scheme, any port, scp-like `user@host:path`)."""
    m = _ANY_URL.fullmatch(url) if "://" in url else _SCP_LIKE.match(url)
    name = m.group("host") if m else None
    if not name:
        return False
    try:
        return canonical_host(name) == host
    except ValueError:
        return False


def _without_userinfo(url: str) -> str:
    """An on-host remote as the preview shows it: its userinfo (`user:token@`, scp-like `token@`) replaced by `***@`,
    so a token a remote carries is never echoed; the host onward is kept as the same parse that put it on the host
    read it."""
    if "://" in url:
        m = _ANY_URL.fullmatch(url)
        prefix_end = m.end("scheme") + 3 if m else 0
    else:
        m = _SCP_LIKE.match(url)
        prefix_end = 0
    if m is None:
        return "(a remote that does not parse)"
    start = m.start("host")
    return url if start == prefix_end else url[:prefix_end] + "***@" + url[start:]


def _why_excluded(url: str) -> str:
    """Why a remote on the host declares no repo (`keys._remote_repo` made the decision; this only names it)."""
    m = _ANY_URL.fullmatch(url)
    if m is None:
        return "an scp-style (ssh) remote: a key rides https on port 443 only"
    if m.group("scheme").lower() != "https":
        return f"a {m.group('scheme')} remote: a key rides https on port 443 only"
    port = m.group("port")
    if port is not None and port != "443":
        return f"port {port or '(empty)'}: a key rides https on port 443 only"
    if m.group("rest"):
        return "it carries a query or fragment"
    path = m.group("path")
    path = path[:-1] if path.endswith("/") else path
    if not path or path == "/":
        return "it names no repo path"
    canon, why = canonical_path(path, "GET")
    if canon is None:
        return f"its path walls ({why})"
    return f"its path is not in canonical form ({canon!r})"


def _read_workspace(workspace: Path | str, host: str) -> tuple[tuple[str, ...], tuple[Excluded, ...]]:
    """ONE read of the workspace's remotes: the repo paths its https:443 remotes on `host` declare, and every other
    remote on that host with why it declares nothing."""
    declared: set[str] = set()
    excluded: list[Excluded] = []
    for url in workspace_remote_urls(workspace):
        repo = _remote_repo(url, host)
        if repo is not None:
            declared.add(repo)
        elif _on_host(url, host):
            excluded.append(Excluded(_without_userinfo(url), _why_excluded(url)))
    return tuple(sorted(declared)), tuple(excluded)


def setup_key(data_root: Path | str, *, entity: str, host: str, source: str, account: str,
              placement: Mapping[str, str], repos: Iterable[str], expires: timedelta | datetime, signer_fp: str,
              sign: Signer, clock: Callable[[], datetime], confirm: Callable[[SetupPreview], bool],
              replace: bool = False, record_id: str | None = None, workspace: Path | str | None = None,
              keychain: KeychainItems | None = None, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
              marker_directory: Path | None = None) -> tuple[dict[str, Any], str | None]:
    """Sign a setup record for `entity` and `host` (canonicalised: `Example.COM.` is `example.com`). `source` and
    `account` are the keychain item's service and account: refused unless exactly one item matches. `repos` plus,
    when `workspace` is given, its https remotes on that host (read ONCE, here) are the record's declared repos; with
    no `workspace` only `repos` are declared and no remote is read. `replace`: supersede the live record for that
    (entity, host) in the same link; without it a live one refuses the setup. The record is judged by the log's rules
    (prepared, unsigned), then `confirm(preview)` is asked; False signs nothing (:class:`SetupDeclined`); True signs
    exactly the previewed record. Returns `(record, warning)`; `warning` says the link committed but its anchor did
    not advance."""

    now = _now(clock)
    if isinstance(expires, timedelta):
        if expires <= timedelta(0):
            raise SetupRefused("a setup record's life must be positive")
        expires_at = now + expires
    elif isinstance(expires, datetime) and expires.tzinfo is not None:
        expires_at = expires
    else:
        raise SetupRefused("expires is a duration or an aware time")
    if expires_at <= now:
        raise SetupRefused(f"expires {utc_stamp(expires_at)} is not after now ({utc_stamp(now)})")
    canon = _host(host)
    if not isinstance(account, str) or not account:
        raise SetupRefused("the keychain item's account is required (--account)")
    matches = (keychain if keychain is not None else KeychainItems()).count(source, account)
    if matches == 0:
        raise SetupRefused(f"no keychain item with service {source[:80]!r} and account {account[:80]!r}")
    if matches > 1:
        raise SetupRefused(f"{matches} keychain items have service {source[:80]!r} and account {account[:80]!r}: "
                           "which one a read gets is the search list's order; leave one")
    rid = record_id if record_id is not None else "ek-" + secrets.token_hex(6)
    explicit = set(repos)
    if workspace is not None:
        from_ws, excluded = _read_workspace(workspace, canon)
    else:
        from_ws, excluded = (), ()
    record = {"id": rid, "kind": EGRESS_KEY_KIND, "entity": entity, "host": canon, "source": source,
              "account": account, "placement": dict(placement), "repos": sorted(explicit | set(from_ws)),
              "expires_at": utc_stamp(expires_at)}
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
    preview = SetupPreview(_thaw(record), None if workspace is None else Path(workspace),
                           tuple(r for r in from_ws if r not in explicit), excluded, tuple(live))
    try:
        with db.autocommit() as conn:
            link = prepare(conn, op, signer_fp=signer_fp, now=_now(clock), ssh_keygen=ssh_keygen)
            # F7: a replace's policy is the WHOLE policy as judged on `state`; prepare chains onto whatever the head
            # is now, so a link appended in between (a grant created, revoked or renewed) would be undone by it.
            if replace and (link.seq, link.prev_hash) != (state.seq + 1, state.head_hash):
                raise SetupRefused(f"the authority log moved (seq {state.seq} -> {link.seq - 1}) since this replace "
                                   "was built: run it again")
            if not confirm(preview):
                raise SetupDeclined("not confirmed: nothing was signed")
            # the confirm and the sign (a touch, a passphrase) can each wait on a person. After the confirm, a record
            # already expired never prompts the signer; after the sign, the expiry is judged again at the `now` the
            # append runs with
            if expires_at <= _now(clock):
                raise SetupRefused(f"expires {utc_stamp(expires_at)} passed while the record was being confirmed: "
                                   "nothing was signed")
            signature = sign(link.message)
            at = _now(clock)
            if expires_at <= at:
                raise SetupRefused(f"expires {utc_stamp(expires_at)} passed while the record was being signed: "
                                   "nothing was appended")
            new = append(conn, link, signature, now=at, ssh_keygen=ssh_keygen, anchor=anchor_path(db.data_root))
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
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
