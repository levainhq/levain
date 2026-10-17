"""levain.consent.grants — standing consent grants, as signed links in the cockpit's authority log.

A consent grant lets the consent queue answer an agent's permission gate without asking: this agent, these exact
tools, inside this workspace, under these postures, until it expires (at most 30 days). It is a ``create_grant``
link a laptop-presence key signs (a loosen of the standing policy); ``revoke_grant`` removes it (any enrolled key).
The queue never trusts a grant it was handed: it finds it in a fresh replay of the log at decision time and again
at delivery (:mod:`levain.consent.queue`). These are the operations ``levain consent grant`` runs; the data root,
signing keys and refusals are the cockpit key commands' own (:mod:`levain.cockpit.keys`).
"""
from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import CONSENT_GRANT_KIND, consent_grant, consent_workspace_problem, inert_grant
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import KeyError_, Signer, _append, _capture, _journal, _now, _signing_authority

__all__ = ["ConsentGrant", "create_grant", "grant_listing", "list_grants", "list_inert_grants", "revoke_grant"]


@dataclass(frozen=True)
class ConsentGrant:
    """A consent grant as the replay holds it, with the authority link that created or last renewed it."""
    id: str
    agent: str
    tools: tuple[str, ...]
    workspace: str
    postures: tuple[str, ...]
    expires_at: str
    seq: int | None
    link_hash: str | None

    @classmethod
    def of(cls, grant: Mapping[str, Any], link: tuple[int, str] | None) -> ConsentGrant:
        return cls(grant["id"], grant["agent"], tuple(grant["tools"]), grant["workspace"], tuple(grant["postures"]),
                   grant["expires_at"], None if link is None else link[0], None if link is None else link[1])


def _workspace(path: Path | str) -> str:
    """``path`` resolved (``~`` expanded, symlinks followed), an existing directory: what the grant stores is
    exactly what the criteria compare against."""
    resolved = os.path.realpath(Path(path).expanduser())
    if not os.path.isdir(resolved):
        raise KeyError_(f"the workspace {resolved} is not a directory")
    problem = consent_workspace_problem(resolved)
    if problem is not None:
        raise KeyError_(problem)
    return resolved


def create_grant(data_root: Path | str, *, agent: str, tools: Iterable[str], workspace: Path | str,
                 postures: Iterable[str], expires: timedelta | datetime, signer_fp: str, sign: Signer,
                 clock: Callable[[], datetime], grant_id: str | None = None,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None) -> tuple[ConsentGrant, str | None]:
    """Sign a ``create_grant`` link for a consent grant (``expires``: a duration from now, or an aware time).
    The log's own rules judge it before anything is signed: exact tool names, no shell tool, the agent's
    posture names, a workspace that is not ``/`` or the home directory, a life of at most 30 days, a
    laptop-presence signer. Returns ``(grant, warning)``; ``warning`` says the link committed but its anchor
    did not advance."""
    now = _now(clock)
    if isinstance(expires, timedelta):
        if expires <= timedelta(0):
            raise KeyError_("a grant's life must be positive")
        expires_at = now + expires
    elif isinstance(expires, datetime) and expires.tzinfo is not None:
        expires_at = expires
    else:
        raise KeyError_("expires is a duration or an aware time")
    gid = grant_id if grant_id is not None else "cg-" + secrets.token_hex(6)
    grant = {"id": gid, "kind": CONSENT_GRANT_KIND, "agent": agent, "tools": sorted(set(tools)),
             "workspace": _workspace(workspace), "postures": sorted(set(postures)),
             "expires_at": utc_stamp(expires_at)}
    db = _journal(data_root, marker_directory=marker_directory)
    _signing_authority(_capture(db, now, ssh_keygen))     # refused before anything is signed
    new = _append(db, {"op": "create_grant", "grant": grant}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=ssh_keygen)
    return ConsentGrant.of(new.policy.grants[gid], new.grant_link(gid)), new.anchor_warning


def revoke_grant(data_root: Path | str, grant_id: str, *, signer_fp: str, sign: Signer,
                 clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None) -> tuple[int, str | None]:
    """Sign a ``revoke_grant`` link removing the consent grant ``grant_id``, or one of a kind this levain does not
    know (:func:`list_inert_grants`); any enrolled key may. From the next decision on it answers nothing, and an
    answer it already gave is no longer delivered. Returns ``(seq, warning)``."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    grant = state.policy.grants.get(grant_id)
    if not consent_grant(grant) and not inert_grant(grant):
        raise KeyError_(f"no consent grant {grant_id!r}")
    new = _append(db, {"op": "revoke_grant", "grant_id": grant_id}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=ssh_keygen)
    return new.seq, new.anchor_warning


def list_grants(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                marker_directory: Path | None = None) -> list[ConsentGrant]:
    """Every consent grant the authority log's replay holds now, expired ones included, by id."""
    return grant_listing(data_root, now=now, ssh_keygen=ssh_keygen, marker_directory=marker_directory)[0]


def list_inert_grants(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                      marker_directory: Path | None = None) -> list[tuple[str, str, str, int | None]]:
    """``(id, kind, expires_at, link seq)`` of every grant the replay holds of a kind this levain does not know
    (one a newer levain wrote): held inert, it answers nothing here, and ``revoke_grant`` removes it."""
    return grant_listing(data_root, now=now, ssh_keygen=ssh_keygen, marker_directory=marker_directory)[1]


def grant_listing(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                  marker_directory: Path | None = None
                  ) -> tuple[list[ConsentGrant], list[tuple[str, str, str, int | None]]]:
    """``(consent grants, inert grants)`` from ONE capture and one snapshot, so a grant is in exactly one of them
    even while a link lands (two separate captures could show it twice or not at all)."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))
    consent: list[ConsentGrant] = []
    inert: list[tuple[str, str, str, int | None]] = []
    for gid, g in sorted(state.policy.grants.items()):
        link = state.grant_link(gid)
        if consent_grant(g):
            consent.append(ConsentGrant.of(g, link))
        elif inert_grant(g):
            inert.append((gid, str(g["kind"]), str(g["expires_at"]), None if link is None else link[0]))
    return consent, inert
