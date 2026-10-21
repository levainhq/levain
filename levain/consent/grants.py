"""levain.consent.grants — standing consent grants, as signed links in the cockpit's authority log.

A consent grant lets the consent queue answer an agent's permission gate without asking: this agent, these exact
tools, inside this workspace, under these postures, until it expires (at most 30 days). It is a ``create_grant``
link a laptop-presence key signs (a loosen of the standing policy); ``revoke_grant`` removes it (any enrolled key).
The queue never trusts a grant it was handed: it finds it in a fresh replay of the log at decision time and again
at delivery (:mod:`levain.consent.queue`). These are the operations ``levain consent grant`` runs; the data root,
signing keys and refusals are the cockpit key commands' own (:mod:`levain.cockpit.keys`). A revoke can also be
signed by a passkey on the phone (:mod:`levain.consent.grant_phone`), the one authority link a passkey may sign: the
link is built here (:func:`prepare_revoke`), signed there, and appended here (:func:`append_revoke`).
:func:`grant_view` is what the cockpit page and the phone show: each grant, what it answered, and its revoke line.
"""
from __future__ import annotations

import secrets
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import (CONSENT_GRANT_KIND, CONSENT_MCP_GRANT_KIND, AuthorityError, StaleHead, UnsignedLink,
                              VerifierUnavailable, anchor_path, append, consent_grant, consent_grant_tool_problem,
                              consent_resolved_workspace, inert_grant, prepare)
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import KeyError_, Signer, _append, _capture, _journal, _now, _signing_authority

__all__ = ["INERT_NOTE", "ConsentGrant", "append_revoke", "create_grant", "grant_listing", "grant_view", "list_grants",
           "list_inert_grants", "prepare_revoke", "revoke_command", "revoke_grant"]


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
    kind: str = CONSENT_GRANT_KIND
    # a consent-mcp grant's declarations: tool -> field -> kind (empty for a consent grant)
    mcp_fields: dict[str, dict[str, str]] = field(default_factory=dict, hash=False)

    @classmethod
    def of(cls, grant: Mapping[str, Any], link: tuple[int, str] | None) -> ConsentGrant:
        declared = grant.get("mcp_fields") if grant.get("kind") == CONSENT_MCP_GRANT_KIND else None
        return cls(grant["id"], grant["agent"], tuple(grant["tools"]), grant["workspace"], tuple(grant["postures"]),
                   grant["expires_at"], None if link is None else link[0], None if link is None else link[1],
                   str(grant["kind"]), {str(t): {str(k): str(v) for k, v in f.items()}
                                        for t, f in (declared or {}).items()})

    def tool_problem(self, tool: str) -> str | None:
        """Why this grant answers nothing for ``tool`` (:func:`levain.authority.consent_grant_tool_problem`)."""
        return consent_grant_tool_problem({"kind": self.kind, "agent": self.agent, "mcp_fields": self.mcp_fields},
                                          tool)


def _workspace(path: Path | str) -> str:
    """``path`` resolved (``~`` expanded, symlinks followed), an existing directory: what the grant stores is
    exactly what the criteria compare against."""
    resolved, problem = consent_resolved_workspace(path)
    if resolved is None:
        raise KeyError_(problem)
    return resolved


def create_grant(data_root: Path | str, *, agent: str, tools: Iterable[str], workspace: Path | str,
                 postures: Iterable[str], expires: timedelta | datetime, signer_fp: str, sign: Signer,
                 clock: Callable[[], datetime], grant_id: str | None = None,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None,
                 mcp_fields: Mapping[str, Mapping[str, str]] | None = None) -> tuple[ConsentGrant, str | None]:
    """Sign a ``create_grant`` link for a consent grant (``expires``: a duration from now, or an aware time).
    ``mcp_fields`` declares, for MCP tools among ``tools``, their input fields and each one's kind
    (:data:`levain.authority.CONSENT_MCP_FIELD_KINDS`): with a declaration the grant is of the ``consent-mcp`` kind,
    without one of the ``consent`` kind, exactly as before.
    The log's own rules judge it before anything is signed: exact tool names, no shell tool, no MCP tool off the
    filesystem server's read tools that the grant does not declare, the agent's posture names, a workspace that is
    not ``/`` or the home directory, a life of at most 30 days, a laptop-presence signer. Returns
    ``(grant, warning)``; ``warning`` says the link committed but its anchor did not advance."""
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
    tools = sorted(set(tools))
    grant = {"id": gid, "kind": CONSENT_GRANT_KIND, "agent": agent, "tools": tools,
             "workspace": _workspace(workspace), "postures": sorted(set(postures)),
             "expires_at": utc_stamp(expires_at)}
    if mcp_fields:
        stray = sorted(t for t in mcp_fields if t not in tools)
        if stray:
            raise KeyError_(f"fields are declared for {stray[0][:200]!r}, which the grant does not name (--tool)")
        grant["kind"] = CONSENT_MCP_GRANT_KIND
        grant["mcp_fields"] = {t: dict(f) for t, f in mcp_fields.items()}
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
    db = _revocable(data_root, grant_id, clock, ssh_keygen, marker_directory)
    new = _append(db, {"op": "revoke_grant", "grant_id": grant_id}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=ssh_keygen)
    return new.seq, new.anchor_warning


def _revocable(data_root: Path | str, grant_id: str, clock: Callable[[], datetime], ssh_keygen: Path | str,
               marker_directory: Path | None) -> Any:
    """The journal at ``data_root``, once its replay holds ``grant_id`` as a consent grant or an inert one."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    grant = state.policy.grants.get(grant_id)
    if not consent_grant(grant) and not inert_grant(grant):
        raise KeyError_(f"no consent grant {grant_id!r}")
    return db


def prepare_revoke(data_root: Path | str, grant_id: str, *, signer_fp: str, clock: Callable[[], datetime],
                   ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                   marker_directory: Path | None = None) -> UnsignedLink:
    """The unsigned ``revoke_grant`` link for ``grant_id`` (what :func:`revoke_grant` would sign), for a signer that
    signs elsewhere: the phone's passkey. Judged now by every rule but the signature, so a link the log would refuse
    (an unknown grant, a signer that may not sign it) is refused before anyone is asked to sign."""
    db = _revocable(data_root, grant_id, clock, ssh_keygen, marker_directory)
    try:
        with db.autocommit() as conn:
            return prepare(conn, {"op": "revoke_grant", "grant_id": grant_id}, signer_fp=signer_fp, now=_now(clock),
                           ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None


def append_revoke(data_root: Path | str, link: UnsignedLink, signature: str, *, clock: Callable[[], datetime],
                  ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                  marker_directory: Path | None = None) -> tuple[int, str | None]:
    """Append ``link`` (from :func:`prepare_revoke`) with ``signature`` over its message. The log verifies both as a
    replay would; a head that moved since the link was prepared raises :class:`levain.authority.StaleHead` (the link
    is then prepared and signed again), every other refusal :class:`levain.cockpit.keys.KeyError_`. Returns
    ``(seq, warning)`` as :func:`revoke_grant` does."""
    import json

    try:
        record = json.loads(link.record)
    except ValueError:
        record = None
    if not isinstance(record, dict) or record.get("op") != "revoke_grant":
        raise KeyError_("only a revoke_grant link is appended here")
    db = _journal(data_root, marker_directory=marker_directory)
    try:
        with db.autocommit() as conn:
            new = append(conn, link, signature, now=_now(clock), anchor=anchor_path(db.data_root),
                         ssh_keygen=ssh_keygen)
    except (VerifierUnavailable, StaleHead):
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None
    return new.seq, new.anchor_warning


def revoke_command(grant_id: str, data_root: Path | str) -> str:
    """The terminal line that revokes ``grant_id`` in the cockpit at ``data_root``, every value shell-quoted and the
    id after ``--`` (an id is the creator's choice: one that starts with a dash is still the id, never an option).
    ``<key>`` is the operator's own signing key, left for them to fill in."""
    return " ".join(["levain consent grant revoke --data-root", shlex.quote(str(data_root)),
                     "--signing-key <key> --", shlex.quote(grant_id)])


INERT_NOTE = "held inert: a newer levain's kind, it answers nothing here; revoke removes it"


def grant_view(queue_dir: Path | str, *, now: datetime, recent: int = 5,
               ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> dict[str, Any]:
    """Every grant the cockpit the consent hooks of ``queue_dir`` name holds (expired and inert ones included), each
    with what it answered and the line that revokes it: ``{"root", "note", "grants", "inert_note"}``. ``root`` is the
    ONE cockpit those hooks verify against (the log the queue finds a grant in at decision time); none or more than
    one is a ``note`` and no rows. What a grant answered is read from the queue (the requests it allowed) and the
    ledger (whether each such call then ran): ``answered`` is ``{"count", "last_at", "recent"}``, ``recent`` the
    newest ``recent`` calls, each ``{"request_id", "at", "tool", "cwd", "outcome"}``. Read-only: nothing is created.
    Every string a grant's creator or an agent wrote is shown through ``visible``; ``id`` is also given as is, the
    name a revoke route takes."""
    from levain.cockpit.display_safe import visible
    from levain.consent.answering import hook_roots
    from levain.consent.listing import read_grant_evidence

    roots = hook_roots(Path(queue_dir))
    if len(roots) != 1:
        return {"root": "", "grants": [], "inert_note": INERT_NOTE,
                "note": ("no consent hook names a cockpit, so no grant is read here" if not roots else
                         "the consent hooks name more than one cockpit, so which grants answer is not one answer")}
    root = roots[0]
    try:
        consent, inert = grant_listing(root, now=now, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:        # no journal there, a governed root, a log that does not replay: said, not raised
        return {"root": visible(str(root)), "grants": [], "inert_note": INERT_NOTE, "note": visible(str(exc))}
    ids = [g.id for g in consent] + [i[0] for i in inert]
    evidence = read_grant_evidence(queue_dir, ids, recent=recent)
    stamp = utc_stamp(now)
    rows: list[dict[str, Any]] = []

    def answered(gid: str) -> dict[str, Any]:
        got = evidence.get(gid) or {"count": 0, "last_at": "", "recent": []}
        return {**got, "recent": [{**c, "tool": visible(c["tool"]), "cwd": visible(c["cwd"])} for c in got["recent"]]}

    for g in consent:
        row: dict[str, Any] = {
            "id": g.id, "id_shown": visible(g.id), "kind": g.kind,
            "state": "live" if g.expires_at > stamp else "expired", "agent": g.agent,
            "tools": [visible(t) for t in g.tools], "workspace": visible(g.workspace), "postures": list(g.postures),
            "expires_at": g.expires_at, "link_seq": g.seq, "answered": answered(g.id),
            "revoke": visible(revoke_command(g.id, root))}
        if g.kind == CONSENT_MCP_GRANT_KIND:
            row["mcp_fields"] = {visible(t): {visible(k): v for k, v in f.items()} for t, f in g.mcp_fields.items()}
        rows.append(row)
    for gid, kind, expires_at, seq in inert:
        rows.append({"id": gid, "id_shown": visible(gid), "kind": visible(kind), "state": "inert",
                     "expires_at": expires_at, "link_seq": seq, "answered": answered(gid),
                     "revoke": visible(revoke_command(gid, root))})
    return {"root": visible(str(root)), "note": "", "grants": rows, "inert_note": INERT_NOTE}


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
