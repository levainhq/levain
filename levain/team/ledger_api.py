"""The wire-neutral ledger surface: what a ``levain team`` caller may ask of a team ledger, whatever carries it.

``LedgerTransport`` is a structural ``typing.Protocol``. ``GitLedger`` (the git wire, ``transport.py``) satisfies it
without naming it; a self-hosted server wire is meant to be a second implementation behind ``open_ledger``, not a change
to the callers. Members that only make sense on git (branches, refs, commits, the clone's pin as a commit) stay on
``GitLedger`` and are reached through ``require_git_wire``, which refuses on any other wire.

Membership was decided by reading the callers. Private keyword knobs (``update_counted(_check=)``,
``lock(_lower=)``) are deliberately left out. Signatures are copied from ``GitLedger``, including the members it leaves
unannotated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import index as I
from . import roles as R
from .transport import GitLedger, Repo, TeamError


class LedgerTransport(Protocol):
    """The team ledger as a caller sees it: writes, reads, sync, identity and this clone's local state."""

    # ---- local state ------------------------------------------------------------------------------------------
    repo: Repo
    """The project repository this ledger was opened for."""
    base: Path
    """This clone's private ledger directory (state, locks, caches), never pushed."""
    warnings: list[str]
    """Things a person must hear that did not stop the operation, appended as they happen."""

    @property
    def state_path(self) -> Path:
        """Where this clone's local state file lives."""
        ...

    def state(self) -> dict:
        """This clone's local state as last saved; an unreadable file reads as empty."""
        ...

    def save_state(self, _mutate=None, **changes) -> None:
        """Read, change and replace this clone's local state under one lock; ``_mutate`` computes value-dependent changes."""
        ...

    @property
    def device(self) -> str:
        """This clone's device id, the file its own lines are written to."""
        ...

    @property
    def remote(self) -> str | None:
        """The remote this clone publishes to, or None when the ledger is local only."""
        ...

    def session_denied(self, session: str) -> set[str]:
        """Entry ids already denied to this agent session."""
        ...

    def mark_denied(self, session: str, ids: set[str]) -> None:
        """Remember entry ids denied to this agent session, so a retry is answered the same way."""
        ...

    def lock(self, *, exclusive: bool = True, timeout: float = 30.0, name: str = "lock"):
        """A context manager holding one of this clone's named file locks (exclusive or shared)."""
        ...

    def op(self, *, timeout: float = 150.0):
        """A context manager holding this clone for one whole operation, start to finish."""
        ...

    def joined(self) -> bool:
        """Whether this clone has joined (pinned and attached) a team ledger."""
        ...

    def require_joined(self) -> None:
        """Refused unless this clone has joined a team ledger; repairs a moved clone on the way."""
        ...

    def legacy_only(self) -> bool:
        """Whether only a pre-strict ledger is present here, so the strict ledger's refusal applies."""
        ...

    # ---- writes -----------------------------------------------------------------------------------------------
    def append(self, entry: dict, *, push: bool = True, lock_timeout: float = 30.0) -> dict:
        """Seal one entry into this clone's own chain and publish it when asked; returns the entry as written."""
        ...

    def write_canon(self, text: str, *, push: bool = True) -> str:
        """Write the generated canon (PROJECT.md) and publish it when asked; returns a one-line outcome."""
        ...

    def update_counted(self, change, message: str, *, push: bool = True) -> str:
        """Apply ``change(team, tenure)`` to the counted team state, signed, and publish it when asked."""
        ...

    def init(self, team: R.Team, *, member_keys: dict[str, str] | None = None, remote: str | None = None,
             push: bool = True, signing_key: str | None = None, replace_legacy: bool = False,
             anneal_db: str | None = None) -> str:
        """Create a new team ledger with ``team`` as its genesis and join this clone to it."""
        ...

    def join(self, *, remote: str | None = None, new_device: bool = False, root: str | None = None,
             signing_key: str | None = None, accept_merges: dict[str, int] | None = None) -> str:
        """Join this clone to an existing team ledger, pinning the genesis it names or finds."""
        ...

    # ---- reads ------------------------------------------------------------------------------------------------
    def head(self) -> str:
        """The version of the ledger this clone holds now (on git, the tip commit of the ledger branch): the id a
        caller records beside an act it just made. Refused (``TeamError``) when this clone holds no ledger."""
        ...

    def snapshot(self) -> tuple[str, R.Team, I.Ledger]:
        """(version, team, ledger), all read from one consistent version of the ledger."""
        ...

    def ledger(self, team: R.Team | None = None, rev: str | None = None) -> I.Ledger:
        """The ledger as enforced at ``rev`` (default: the newest this clone holds)."""
        ...

    def team(self, rev: str | None = None) -> R.Team:
        """The team in force at ``rev``: the counted team, never an uncounted file."""
        ...

    def read_canon(self, rev: str | None = None, *, d=None) -> str | None:
        """The canon text at ``rev`` (or of the derivation ``d`` the caller holds), or None when there is none."""
        ...

    @staticmethod
    def state_hash(ledger: I.Ledger, team: R.Team) -> str:
        """A content hash of the enforced ledger and team, the canon's staleness key."""
        ...

    def derivation(self, rev: str | None = None):
        """This clone's tenure derivation at ``rev``: who counted, what is in force, and how fully it was judged."""
        ...

    @property
    def pinned_root(self) -> str | None:
        """The genesis this clone pinned when it joined, or None before it has."""
        ...

    def team_history_problems(self, team: R.Team, rev: str | None = None) -> list[str]:
        """Team-history problems reported beside the ledger's own (kept for callers)."""
        ...

    # ---- sync -------------------------------------------------------------------------------------------------
    def sync(self, *, push: bool = True) -> str:
        """Bring this clone up to date with the published ledger and publish its own lines when asked."""
        ...

    def fetch_if_due(self, interval: float, *, timeout: float = 8.0) -> str | None:
        """Fetch when the last fetch is older than ``interval``; returns a note when it could not, else None."""
        ...

    def sync_now(self, *, push: bool = False, op_wait: float = 5.0, timeout: float = 20.0) -> str:
        """Sync now on bounded waits (a push only when ``push``); TeamBusy while another team operation holds it."""
        ...

    # ---- identity ---------------------------------------------------------------------------------------------
    def handle(self, team: R.Team | None = None, *, d=None) -> str | None:
        """The member whose keys in force include this machine's key, or None (from ``d`` when the caller holds one)."""
        ...

    def email(self) -> str:
        """The email this machine identifies as (display and invitations only, never membership)."""
        ...

    def own_fingerprint(self) -> str:
        """The fingerprint of this machine's signing key."""
        ...

    def signing_pubkey(self, key: str | None = None) -> str:
        """The public key line of this machine's signing key (or of ``key``)."""
        ...


    def confirm_own_key(self, d, *, push: bool | None = None) -> bool:
        """If this machine's key is PENDING for a member, move it into force with a commit signed by that key."""
        ...

    def accept_offer(self, d, *, push: bool = True) -> str:
        """Accept the live ownership offer to the member whose key in force is this machine's."""
        ...

    @staticmethod
    def ledger_of(d) -> I.Ledger:
        """The enforced ledger for a derivation (pure)."""
        ...

    def forget_derivation(self) -> None:
        """Drop the cached derivation (this clone's trust state or the tip changed under it)."""
        ...

    def has_local_ledger(self) -> bool:
        """Whether a strict team ledger is held here locally (whether or not this clone pinned it)."""
        ...

    def has_remote_ledger(self) -> bool:
        """Whether a remote-tracking strict ledger branch is here."""
        ...

    @property
    def canon_path(self) -> Path:
        """Where the generated canon (PROJECT.md) lives in this clone's private worktree."""
        ...

    def export_pos(self, d) -> int:
        """Commits on the walk up to this clone's ``anchor`` (the last PUBLISHED tip it derived in full); with no anchor,
        the walk to the remote-tracking tip, or to the local tip when the clone has no remote. 0 when none resolves."""
        ...

    def location_row(self) -> tuple[bool, str]:
        """The doctor's first row: where this clone's private ledger lives and which device it is."""
        ...

    def publish_rows(self) -> list[tuple[bool, str]]:
        """The doctor's publishing rows: the last fetch and what is not pushed, or that the ledger is local only."""
        ...

    def ledger_label(self) -> str:
        """The ``ledger <branch>`` part of the first status line."""
        ...

    def freshness_lines(self) -> list[str]:
        """The status line on how fresh this clone's fetch is (none when the ledger is local only)."""
        ...

    def foreign_ledger_lines(self, d) -> list[str]:
        """Status lines on other ledgers on the remote: a 0.6.x branch, and branches claiming to re-genesis this team."""
        ...


def open_ledger(repo: Repo) -> LedgerTransport:
    """The team ledger for ``repo`` on the wire this clone uses (the git wire, until a second wire exists)."""
    return GitLedger(repo)


def require_git_wire(gl: LedgerTransport) -> GitLedger:
    """``gl`` as a ``GitLedger`` for a command that works on git history; refused on any other wire."""
    if isinstance(gl, GitLedger):
        return gl
    raise TeamError("this command works on the git history of a team ledger (commits, branches, the pinned genesis), "
                    "and this clone's team ledger is not carried by git")
