"""levain.agents.signing: the one entry that signs an agent link (A0 §3.2; BUILD_PLAN P5a-2).

:func:`sign_agent_link` creates an ``agent-link`` and :func:`renew_agent_link` moves one's expiry. They are the only
functions that prepare an agent-link append or renew: ``levain agents sign|renew``, the cockpit signing card and the
renew verb call these and nothing lower, so the open-bar check made here has one call site every surface passes.
``tests/test_authority_agent_signing.py`` walks ``levain/`` with ``ast`` to keep it so.

The log's own rules judge every link (the kind's validation, the laptop-only renew, the rank and the format); nothing
here repeats them. What is here is the shape of the gesture: judge on the head the replay just read, sign outside any
transaction (a touch can take seconds), append only on that same head, at the clock read after the signature.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.agent_authority import OPEN_BAR_REFUSAL, AgentAuthorityError, open_bar_for
from levain.authority import (AGENT_LINK_KIND, AGENT_LINK_MAX, AuthorityError, AuthoritySnapshot, StaleHead,
                              VerifierUnavailable, agent_link, anchor_path, append, prepare)
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import KeyError_, _capture, _journal, _now, _signing_authority

__all__ = ["renew_agent_link", "sign_agent_link"]

Signer = Callable[[bytes], str]


def _refuse_open_bar(agent_id: object, store_dir: Path | str | None) -> None:
    """A link is not signed while its agent's revoke is open (A0 section 2.2): the open bar is read from the autonomic
    store, and a bar that cannot be read refuses too. The append writes the cockpit's log, so a bar committed after this
    read can still be passed: A0 section 6.10's known edge."""
    if type(agent_id) is not str:
        return      # the log's own rules refuse a link with no agent
    try:
        bar = open_bar_for(agent_id, store_dir)
    except AgentAuthorityError as exc:
        raise KeyError_(f"{exc}: nothing was signed") from None
    if bar is not None:
        raise KeyError_(OPEN_BAR_REFUSAL)


def _sign_op(data_root: Path | str, op: Mapping[str, Any], *, signer_fp: str, sign: Signer,
             clock: Callable[[], datetime], ssh_keygen: Path | str, marker_directory: Path | None,
             now: datetime, check: Callable[[AuthoritySnapshot], None]) -> AuthoritySnapshot:
    """One link: judged at ``now`` on the head the replay read (``check`` sees that head first), signed outside any
    transaction, appended only on that head at the clock after the signature."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))        # refused before anything is signed
    check(state)
    try:
        with db.autocommit() as conn:
            link = prepare(conn, op, signer_fp=signer_fp, now=now, ssh_keygen=ssh_keygen)
        if (link.seq - 1, link.prev_hash) != (state.seq, state.head_hash):
            raise StaleHead("the authority log moved while this was prepared: look at it again")
        signature = sign(link.message)
        with db.autocommit() as conn:
            return append(conn, link, signature, now=_now(clock), anchor=anchor_path(db.data_root),
                          ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except StaleHead as exc:
        raise KeyError_(f"{exc} (the authority log moved: nothing was appended)") from None
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None


def sign_agent_link(data_root: Path | str, grant: Mapping[str, Any], *, signer_fp: str, sign: Signer,
                    clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                    marker_directory: Path | None = None, store_dir: Path | str | None = None) -> AuthoritySnapshot:
    """Create ``grant``, an ``agent-link`` record, by a link ``signer_fp`` signs. The log refuses it unless the signer
    is a laptop-presence key and the record is a valid link. Refused while the agent's revoke is open, judged from the
    autonomic store at ``store_dir`` (default: the store every ``levain agents`` verb opens)."""
    if not isinstance(grant, Mapping) or grant.get("kind") != AGENT_LINK_KIND:
        raise KeyError_(f"only an {AGENT_LINK_KIND} is signed here")
    return _sign_op(data_root, {"op": "create_grant", "grant": dict(grant)}, signer_fp=signer_fp, sign=sign,
                    clock=clock, ssh_keygen=ssh_keygen, marker_directory=marker_directory, now=_now(clock),
                    check=lambda state: _refuse_open_bar(grant.get("agent_id"), store_dir))


def renew_agent_link(data_root: Path | str, link_id: str, *, life: timedelta, signer_fp: str, sign: Signer,
                     clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                     marker_directory: Path | None = None,
                     store_dir: Path | str | None = None) -> tuple[AuthoritySnapshot, str]:
    """Move the expiry of the live agent link ``link_id`` to ``life`` after now (its terms and incarnation stay).
    Returns the snapshot and the new ``expires_at``. The log refuses a signer that is not a laptop-presence key, and
    this refuses while the link's agent has an open revoke (``store_dir`` as in :func:`sign_agent_link`)."""
    if not timedelta(0) < life <= AGENT_LINK_MAX:
        raise KeyError_(f"a renew is for more than nothing and at most {AGENT_LINK_MAX.days} days")
    now = _now(clock)

    def check(state: AuthoritySnapshot) -> None:
        grant = state.policy.grants.get(link_id)
        if not agent_link(grant):
            raise KeyError_(f"no agent link {str(link_id)[:80]!r}")
        _refuse_open_bar(grant.get("agent_id"), store_dir)

    expires_at = utc_stamp(now + life)
    op = {"op": "renew", "grant_id": link_id, "expires_at": expires_at, "signed_at": utc_stamp(now)}
    new = _sign_op(data_root, op, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen,
                   marker_directory=marker_directory, now=now, check=check)
    return new, expires_at
