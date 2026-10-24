"""Held keys: the launch's route snapshot, the per-request liveness check and R-6's attachment (design §3.8.5(o)
item 4, item 2 *Per request*, R-6, R-10's `attach` field; CH-D2 S6).

At LAUNCH, :func:`build_route_set` reads the authority log's replay ONCE and builds one immutable
:class:`RouteSnapshot` per keyed host from the entity's LIVE setup records (`levain.authority.egress_key_live`): the
record id, the canonical host, the credential SOURCE (a keychain item name; never the value), the declared repo paths
(the record's own plus the workspace's https remotes on that host) and attach yes/no. A store holding two live records
for one canonical host refuses the launch, naming both (:class:`LaunchRefused`; R15-21-4: no merge, no pick).

PER REQUEST the composer does ONE :func:`lookup` by canonical host (R15-18-L1) and hands that one result to every
reader: classify's keyed-route rules (S3a: "keyed" = in the snapshot, R15-23-4) and :func:`decide_key` here. The
snapshot is immutable for what it GRANTS and live for what REVOKES (R15-22-1): `decide_key` checks, once and bounded,
that the snapshot's record id is still live (:class:`AuthorityLiveness`); a revoked or superseded record attaches
nothing from the next request and the host stays keyed; a record made live, or a replacement, mid-launch attaches
nothing until the next launch (R15-21-6, R15-25-4). The value is read from the credential store only when a key is
chosen, in the service (it never enters the sandbox, an argv, an env, a record or a log), and it is never in a repr.

The key attaches only on (https, host, 443) (R15-10-A6, R15-12-B1 / B12): never on port 80, never on 8443. On an
upload-pack exchange (the discovery `GET <prefix>/info/refs?service=git-upload-pack` and `POST
<prefix>/git-upload-pack`) only for a prefix in the declared repo set (R15-17-1): an undeclared repo, a redirected one
included, goes keyless. `attach` takes the first value that applies (R15-28-3): `not-evaluated` (no audit value:
denied before the lookup) -> `liveness-unreadable` -> `revoked` -> `none` -> `attached` ("chosen at decision", R15-26-2).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from levain.chat_egress import wire
from levain.chat_egress.parse import _canonical_path, canonical_host

__all__ = ["ATTACH_VALUES", "AuthorityLiveness", "CredentialStore", "CredentialUnreadable", "KeyDecision",
           "KeychainStore", "LaunchRefused", "LivenessUnreadable", "Placement", "RouteSet", "RouteSnapshot",
           "build_route_set", "credential_placements", "decide_key", "lookup", "upload_pack_prefix",
           "workspace_remote_urls"]

NOT_EVALUATED, LIVENESS_UNREADABLE, REVOKED, NONE, ATTACHED = (
    "not-evaluated", "liveness-unreadable", "revoked", "none", "attached")
#: R-10's `attach` enum, in R15-28-3's precedence order (the first that applies)
ATTACH_VALUES = (NOT_EVALUATED, LIVENESS_UNREADABLE, REVOKED, NONE, ATTACHED)

_UPLOAD_PACK = "git-upload-pack"
_INFO_REFS = "/info/refs"
_POST_SUFFIX = "/" + _UPLOAD_PACK
# a workspace remote URL (git config, never a request: no request target is parsed outside parse.py, R15-12-B3):
# https, optional userinfo, a DNS host, optional port, a path, no query or fragment
_REMOTE_URL = re.compile(r"(?i:https)://(?:[^@/?#]*@)?(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?"
                         r"(?P<path>/[^?#]*)?")


@dataclass(frozen=True, slots=True)
class RouteSnapshot:
    """One keyed host's route for one launch (R15-21-4), fixed by the build plan (S3a imports it):
    record_id          the setup record the launch read (its liveness is checked per request)
    canonical_host     `parse.canonical_host` of the record's host
    credential_source  the keychain item name the service reads the value from at attach time (never the value)
    declared_repos     the repo path prefixes an upload-pack exchange may carry the key for
    attach             whether this route may attach at all (False: keyed for classification, never attaches)"""
    record_id: str
    canonical_host: str
    credential_source: str
    declared_repos: frozenset[str]
    attach: bool


@dataclass(frozen=True, slots=True)
class Placement:
    """Where srt puts a record's value (R15-9's closed set): `Authorization: Bearer <v>`, or Basic with a user."""
    scheme: Literal["bearer", "basic"]
    user: str | None = None


@dataclass(frozen=True)
class RouteSet:
    """A launch's route snapshot: one :class:`RouteSnapshot` per keyed canonical host, and each record's placement
    (srt's `credentialPlacements`, set once at launch). Immutable; built by :func:`build_route_set`."""
    routes: Mapping[str, RouteSnapshot]
    placements: Mapping[str, Placement] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def of(cls, routes: Iterable[RouteSnapshot],
           placements: Mapping[str, Placement] | None = None) -> RouteSet:
        by_host: dict[str, RouteSnapshot] = {}
        for r in routes:
            if r.canonical_host in by_host:
                raise LaunchRefused(f"two routes for {r.canonical_host}: {by_host[r.canonical_host].record_id}, "
                                    f"{r.record_id}")
            by_host[r.canonical_host] = r
        pl = dict(placements or {})
        for r in by_host.values():
            pl.setdefault(r.record_id, Placement("bearer"))
        return cls(MappingProxyType(by_host), MappingProxyType(pl))


class LaunchRefused(Exception):
    """The launch cannot build its route snapshot (two live records for one canonical host): fail closed."""


class LivenessUnreadable(Exception):
    """The per-request liveness check got no answer (an error, a timeout, an exception alike: R15-24-1)."""


class CredentialUnreadable(Exception):
    """The credential store gave no value for a source."""


def lookup(snapshot_set: RouteSet, canonical_host: str | None) -> RouteSnapshot | None:
    """THE keyed-route lookup: once per request, by canonical host, into the launch's snapshot (R15-18-L1)."""
    if canonical_host is None:
        return None
    return snapshot_set.routes.get(canonical_host)


# --- launch -----------------------------------------------------------------------------------------------------

def _remote_repo(url: str, host: str) -> str | None:
    """The declared-repo path a workspace remote URL contributes for `host`: an https URL on that host and port 443
    (the only route a key rides), its path exactly as git spells it with one trailing `/` dropped (git requests
    `<url>/info/refs` either way), in R-2's canonical form; else None."""
    m = _REMOTE_URL.fullmatch(url)
    if m is None or m.group("port") not in (None, "443"):
        return None
    try:
        if canonical_host(m.group("host")) != host:
            return None
    except ValueError:
        return None
    path = m.group("path") or ""
    path = path[:-1] if path.endswith("/") else path
    if not path or path == "/":
        return None
    canon, _why = _canonical_path(path, "GET")
    return canon if canon == path else None


def build_route_set(authority: Any, *, entity: str, workspace_remotes: Iterable[str] = (),
                    now: datetime) -> RouteSet:
    """The launch's route snapshot from one verified replay (`levain.authority.AuthoritySnapshot`): the live setup
    records of `entity` (expired, revoked and superseded ones are not live), one route each. Two live records for one
    canonical host (any entity: a store a foreign writer left so) refuses the launch naming both."""
    from levain.authority import egress_key_conflicts, egress_key_live

    grants = authority.policy.grants
    conflicts = [c for c in egress_key_conflicts(grants, now) if c[0] == entity]
    if conflicts:
        named = "; ".join(f"{host}: {', '.join(ids)}" for _e, host, ids in conflicts)
        raise LaunchRefused(f"two live setup records for one host, so this launch is refused (no merge, no pick): "
                            f"{named}. Revoke one of them (levain chat key revoke)")
    remotes = tuple(workspace_remotes)
    routes: list[RouteSnapshot] = []
    placements: dict[str, Placement] = {}
    for gid, g in sorted(grants.items()):
        if not egress_key_live(g, now) or g["entity"] != entity:
            continue
        host = g["host"]
        repos = set(g["repos"])
        repos.update(r for r in (_remote_repo(u, host) for u in remotes) if r is not None)
        routes.append(RouteSnapshot(record_id=gid, canonical_host=host, credential_source=g["source"],
                                    declared_repos=frozenset(repos), attach=True))
        p = g["placement"]
        placements[gid] = Placement("basic", p["user"]) if p["scheme"] == "basic" else Placement("bearer")
    return RouteSet.of(routes, placements)


def workspace_remote_urls(workspace: Path | str) -> tuple[str, ...]:
    """The workspace repo's own `remote.<name>.url` values (its local config only), or () when it is no repo. Read
    once at launch: a remote added mid-launch is the next launch's (R15-21-6)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "HOME": os.devnull, "LC_ALL": "C"}
    try:
        out = subprocess.run(["git", "-C", str(workspace), "config", "--local", "--null", "--get-regexp",
                              r"^remote\..*\.url$"], capture_output=True, env=env, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if out.returncode != 0:
        return ()
    urls = []
    for entry in out.stdout.split(b"\0"):
        if b"\n" in entry:
            urls.append(entry.split(b"\n", 1)[1].decode("utf-8", "replace"))
    return tuple(urls)


def credential_placements(snapshot_set: RouteSet) -> list[dict]:
    """srt's `credentialPlacements` for the launch: one rule per route, its class the record id (the `cred` an allow
    names) and its host the canonical host, so a class can only ever place on its own host."""
    out = []
    for r in snapshot_set.routes.values():
        p = snapshot_set.placements[r.record_id]
        rule: dict = {"class": r.record_id, "host": r.canonical_host, "scheme": p.scheme}
        if p.scheme == "basic":
            rule["user"] = p.user
        out.append(rule)
    return out


# --- per request ------------------------------------------------------------------------------------------------

class Liveness(Protocol):
    def check(self, record_id: str) -> bool: ...


class CredentialStore(Protocol):
    def read(self, source: str) -> str: ...


class AuthorityLiveness:
    """The per-request liveness check (R15-22-1): is the snapshot's record id still a live setup record in a fresh
    verified replay? `source` returns that replay (production: `levain.consent.queue.authority_source(...)`). One read
    per check, no retry, bounded by `timeout_s`: an error, a timeout or an exception raises
    :class:`LivenessUnreadable` (R15-24-1). A read that outlives its bound finishes on a daemon thread and its answer
    is dropped."""

    def __init__(self, source: Callable[[], Any], *, clock: Callable[[], datetime], timeout_s: float = 2.0) -> None:
        self._source = source
        self._clock = clock
        self._timeout = timeout_s

    def check(self, record_id: str) -> bool:
        from levain.authority import egress_key_live

        box: dict[str, Any] = {}

        def read() -> None:
            try:
                box["snap"] = self._source()
            except BaseException as exc:     # noqa: BLE001: any failure is "no answer"
                box["err"] = exc

        t = threading.Thread(target=read, name="levain-key-liveness", daemon=True)
        t.start()
        t.join(self._timeout)
        if t.is_alive():
            raise LivenessUnreadable("the setup-record store did not answer in time")
        if "err" in box:
            raise LivenessUnreadable(f"the setup-record store could not be read ({type(box['err']).__name__})")
        try:
            return egress_key_live(box["snap"].policy.grants.get(record_id), self._clock())
        except Exception as exc:             # noqa: BLE001: a malformed answer is no answer
            raise LivenessUnreadable(f"the setup-record store gave no usable answer ({type(exc).__name__})") from None


class KeychainStore:
    """The macOS login keychain: a generic password whose service is the source name (`security
    find-generic-password -s <source> -w`). Read in the service, at attach time; the value is never logged. Tests
    never construct this (a real item is a person's, S10)."""

    def __init__(self, security: str = "/usr/bin/security", timeout_s: float = 5.0) -> None:
        self._security = security
        self._timeout = timeout_s

    def read(self, source: str) -> str:
        try:
            out = subprocess.run([self._security, "find-generic-password", "-s", source, "-w"], capture_output=True,
                                 timeout=self._timeout, check=False, env={"PATH": "/usr/bin:/bin"})
        except (OSError, subprocess.TimeoutExpired):
            raise CredentialUnreadable("the keychain did not answer") from None
        if out.returncode != 0:
            raise CredentialUnreadable(f"no keychain item {source[:80]!r}")
        return out.stdout.decode("utf-8", "replace").rstrip("\n")


@dataclass(frozen=True)
class KeyDecision:
    """R-6 for one request.
    route       the one lookup's result (None: an unkeyed host, or not evaluated)
    attach      R-10's enum value (:data:`ATTACH_VALUES`)
    cred        the allow's `cred` (the record id, srt's placement class) iff a key was chosen
    credential  the value iff a key was chosen; never in repr or str
    problem     why a key was chosen but none could be attached: "credential-unreadable" (the store gave no value)
                or "credential-unusable" (a value srt's frame refuses), with `attach` "none"; else None"""
    route: RouteSnapshot | None
    attach: str
    cred: str | None = None
    credential: str | None = field(default=None, repr=False)
    problem: str | None = None

    @property
    def keyed(self) -> bool:
        """Snapshot membership (R15-23-4): what classify reads as "keyed", whatever the liveness."""
        return self.route is not None

    def __str__(self) -> str:
        return repr(self)


def upload_pack_prefix(parsed: Any) -> str | None:
    """The repo prefix of an upload-pack exchange, or None: `GET`/`HEAD <prefix>/info/refs` whose `service` (R-1's
    folded query parse) is exactly `git-upload-pack`, or `POST <prefix>/git-upload-pack`; on R-2's canonical path."""
    path = parsed.canonical_path
    if not path:
        return None
    if parsed.method in ("GET", "HEAD") and path.endswith(_INFO_REFS) \
            and parsed.query_values("service") == (_UPLOAD_PACK,):
        prefix = path[:-len(_INFO_REFS)]
    elif parsed.method == "POST" and path.endswith(_POST_SUFFIX):
        prefix = path[:-len(_POST_SUFFIX)]
    else:
        return None
    return prefix or None


def decide_key(route: RouteSnapshot | None, parsed: Any, *, liveness: Liveness, store: CredentialStore) -> KeyDecision:
    """R-6 for one request, given the request's ONE lookup result `route` (R15-18-L1). The first value that applies
    (R15-28-3): no audit value -> `not-evaluated` (no liveness read); an unkeyed host -> `none`; then ONE liveness read:
    no answer -> `liveness-unreadable`, not live -> `revoked`; then off (https, 443), a route that never attaches, or
    an upload-pack exchange for an undeclared prefix -> `none`; else the value is read and `attached`."""
    if parsed.request_id is None:
        return KeyDecision(None, NOT_EVALUATED)
    if route is None:
        return KeyDecision(None, NONE)
    try:
        live = liveness.check(route.record_id)
    except LivenessUnreadable:
        return KeyDecision(route, LIVENESS_UNREADABLE)
    except Exception:                        # noqa: BLE001: a check that raises anything is unreadable
        return KeyDecision(route, LIVENESS_UNREADABLE)
    if not live:
        return KeyDecision(route, REVOKED)
    if not route.attach or parsed.scheme != "https" or parsed.port != 443:
        return KeyDecision(route, NONE)
    prefix = upload_pack_prefix(parsed)
    if prefix is not None and prefix not in route.declared_repos:
        return KeyDecision(route, NONE)
    try:
        value = store.read(route.credential_source)
    except Exception:                        # noqa: BLE001: no value is no key, whatever the store raised
        return KeyDecision(route, NONE, problem="credential-unreadable")
    if not isinstance(value, str) or not wire._CREDENTIAL.fullmatch(value):
        return KeyDecision(route, NONE, problem="credential-unusable")
    return KeyDecision(route, ATTACHED, cred=route.record_id, credential=value)
