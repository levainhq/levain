"""The dispatch RUN SERVER (levain 27, slice S10b; design ``1104-27_S10_DESIGN.md`` sections 2, 3.1, 3.3, 3.4, 6): ONE
process as ``_levain_dispatch`` that owns :class:`levain.agents.session_launcher.RealLauncher` and answers the operator
uid's client (:mod:`levain.agents.session_client`, wire ``levain-run/1``) on a unix socket.

* **Peer.** A connection whose kernel peer uid is not the operator's is closed unread and unanswered.
* **LAUNCH** (a holder connection): decode, :func:`verify_point_of_use` (Q1, below), the pre-mint pool check (design port
  item 10: a transient ``no-free-slot`` when ``POOL`` shows no free egress slot, so the fire's next window retries; no
  in-server retry), then ``RealLauncher.spawn`` unchanged, under a holder permit (:data:`MAX_HOLDERS`, below the
  connection cap so USAGE and END always find one; none free is a transient ``no-holder-capacity``). The answer is
  ``{session}``; no ticket, no run id.
* **A workspace upload** (levain 34, W1; :mod:`levain.slot.workspace`). A LAUNCH whose ``launch.workspace`` member is set
  is preceded, on the same connection, by ``WSBEGIN`` (the two parts' sizes and sha256 digests) and ``WSDATA`` chunks
  (bounded, in order, base64) of the canonical snapshot manifest and the git bundle. After the authority check and
  before the pool check or any mint, the LAUNCH verifies the upload (hash identity: the server re-derives the commit and
  the object count from the manifest, ingests the bundle in a scratch clone, binds the repo-layer loosening check) and
  refuses ``session_refused:workspace`` on any mismatch, with nothing staged and a zero-usage ``refused`` record (standing
  for a standing refusal of this connection's own upload for the key, transient for any other); the slot runtime later writes the verified bytes
  to ``staging/repo.bundle``. One upload per connection (a WSBEGIN replaces the earlier one); the LAUNCH, whatever its
  outcome, and the connection ending drop whatever the stager did not take.
* **RELEASE re-runs** :func:`verify_point_of_use` with the LAUNCH's proof and exposure: a link that expired, was revoked
  or barred while the slot was prepared releases nothing, and the child is ABORTed and ENDed.
* **SYNC re-judges the live holders** (levain 34, row 16; design ``1105-3/DESIGN_dispatch_lease_revoke.md`` D2). A
  session has no point of use after its RELEASE, so a revoke SYNCed into the replica while it ran was enforced only by
  the operator-uid agent tick reading the operator's own ledger. After a SYNC that landed, a worker thread (the SYNC's
  answer is not delayed) runs the SAME ``verify`` over every holder that has a child, with its LAUNCH proof, exposure
  and claims: a STANDING refusal ENDs the session through the key's one END job (``ending`` is marked, so a pending
  RELEASE refuses); a TRANSIENT one ends nothing and logs, and the next SYNC (an empty one included) judges again. A
  session whose END job finished UNPROVEN is judged again on every SYNC, and a standing refusal retries its END; one
  whose job is in flight or proven is skipped. Expiry is not judged here (``FrameClaims.rejudge``): the design's stop reasons are revoke_agent,
  revoke_grant, revoke_key, bar and raise, and an expiry never stopped a released session; nor does a renewal of the
  session's own link, whose new row the LAUNCH proof cannot name (levain 35, L3 lease r2). No new
  wire op and no new authority: the replica never moves backward, so a revoke judged once stays judged whatever a
  restored operator ledger later offers. The bar half is not seen here (bars travel only in frames; the held claims
  carry the LAUNCH's), so a bar stays the tick's. A ``raise`` that tightens a link's budgets below a running session's
  exposure refuses ``exposure-over-link`` and so ENDs that session: the verifier was reused unchanged on purpose, that
  was the recommended reading (design section 5), and it stands as the default until Phill answers the question put to
  him (skipping the exposure check in the re-judge is the cheap alternative). Known edges (L3 r1 LOWs, accepted): a
  transient refusal in the last pass before the SYNCs stop waits for the operator tick, since nothing re-runs a pass on a
  timer. A re-judge END of a never-released child does not ABORT it first; RELEASE's refusal or the holder's EOF cleanup
  ABORTs it later, and that ABORT on a child the END already stopped is caught and harmless. Known edge (L3 lease r3,
  accepted by the head): the re-judge's range check was written to take ANY authentic, active-signed ledger row in
  [born, current) as the proof's current row, not only this link's own, because the stored LAUNCH proof was verified
  exact at LAUNCH and so was meant to name this link's row; only a reconstructed proof could name an unrelated row
  there, and a test pins that, so a later tightening changes it on purpose.
* **The holder is the lifeline** (design 3.4): EOF before RELEASE is ABORT (nothing ran) then END, EOF after RELEASE is
  END, once; a session whose holder is gone stays registered only while its END is unproven. The holder's END is the
  key's END job (below), so a reaper's END on the same key joins it; every END attempt the child makes itself takes the
  same per-key lock. WAIT, TERM, ABORT map onto the launched child. On SIGTERM/SIGINT (:meth:`RunServer.shutdown`) new
  LAUNCHes are refused and every registered session (spawns in flight included) is ENDed on its own thread, returning
  by the absolute :data:`SHUTDOWN_BUDGET_S` deadline; one not proven by then is left to the root reaper by its record.
* **USAGE / END** (short connections). USAGE is ``RealLauncher.usage`` by the recorded key. END is asynchronous and
  idempotent (T7): the first END for a key starts one ``RealLauncher.stop`` on a worker and answers within the
  caller's deadline with its result, else ``StopResult(False, "END in progress")``; a later END joins the same job, and a
  proven result is kept for the process's life and never run twice. [Deviation from design section 2: a thread per
  connection, not one select loop, because WAIT is a long poll and END runs on a worker either way; the server forks
  nothing (RH2 holds).]

**Q1, final (Phill 11-04, np-ed47d151, "8a").** The server verifies a valid enrolled signature over the agent's LINK
that names the spec hash (his existing signature; no per-occurrence signature exists), and re-checks the link's expiry,
revocation, the agent's bar and both signers' standing against ITS OWN replay of the authority ledger, plus the
requested exposure inside the link's budgets, at the point of use: every fire, at LAUNCH and again at RELEASE. All of
that is the one function :func:`verify_point_of_use`. What it does NOT bind: the occurrence id (a process running as
the operator may replay a valid proof for any occurrence of an already-signed spec; accepted by the ruling, bounded by
what he signed) and the launch's brief, harness and model, which travel in the request (an open design question, the
S10b fix-round RETURN).

**Where the readers come from** (``build_server``): the operator's uid is the operator record's (0644). The server reads
NOTHING from the operator's home. The authority ledger is a dispatch-owned REPLICA (``L/dispatch/authority``,
:class:`levain.agents.authority_replica.AuthorityReplica`) that the fire feeds over the run socket with ``SYNC`` (rows
that strictly continue the replica's head); the server verifies every row itself (chain plus SSHSIG), commits only a
verified continuation and never moves backward (its own anchor). ``LAUNCH`` and ``RELEASE`` carry ``ledger_head`` (the
client's verified head, which the replica must HOLD: its row at that seq has that hash. The replica may be AHEAD of it,
because another fire's SYNC can land between this fire's SYNC and its frame; a head past the replica is a transient
``ledger-behind`` (SYNC and retry), a held seq with another hash a standing ``ledger-fork``) and the agent's ``bar``
(open flag and highest closed bar's revoke seq, checked to lie inside the claimed head's seq), each read afresh by the
client for that frame, so ``RELEASE`` judges the bar as it stands at RELEASE. The point-of-use check always judges
the replica's WHOLE current replay, never the claimed prefix, so a revoke another fire SYNCed past the claimed head still refuses. A false bar or a withheld row is the power a process running as the operator
already had over the live files; it can forge no signature and cannot roll the replica back (design 1104-28 section 2.4).

**Known edges** (LOW, kept by intent, none built against). An END for a key whose LAUNCH has not yet registered marks
nothing and answers unproven by name ("no session is recorded under this key"), never "ended", so the LAUNCH that follows
is not refused and the caller's retry (the reaper's periodic END) is what reaches the session once it exists; a revoke
that lands in that window is caught by ``start_run``'s release transaction, which aborts the child unreleased
(``_end``, pinned by ``test_an_end_before_the_holder_registers_answers_a_named_unproven_inside_its_deadline``).
"""
from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import logging
import os
import pwd
import signal
import socket
import sqlite3
import subprocess
import stat
import sys
import traceback
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional, Sequence
from urllib.parse import quote

from levain import authority as _auth
from levain.agents import controller as _ctl
from levain.agents import session_client as C
from levain.agents.authority_replica import AuthorityReplica, ReplicaRefused, ReplicaRow
from levain.agents.accounting import ActionExposure, StopResult
from levain.agents.session_launcher import LauncherRefused, RealLauncher, SessionIndex
from levain.slot import Refused
from levain.agents.signed_role import SignedLink, SignedRoleRefused, signed_withhold
from levain.slot import workspace as WS
from levain.slot.layout import RUN, STORE, dispatch_store_dir

NAME = "dispatch_run"
ACCOUNT = "_levain_dispatch"
EX_USAGE = 64
EX_TEMPFAIL = 75
#: What a `no-free-slot` tells the operator. The gate is CONTROL's `POOL` count (`egress_free`, tickets.pool_free): the
#: credential-free egress slots the mirror shows, less those other open dispatches already hold, less the EGRESS_RESERVE
#: the operator keeps; it must leave at least one. A slot holding a credential or baseline (the marked one included) is
#: not credential-free, so it never counts. The marked slot itself is a separate check (`no-dispatch-credential`).
_NO_FREE_SLOT_DETAIL = ("no dispatch slot is free: after the slot kept for the operator and the slots other dispatches "
                        "are using or have asked for, no spare slot is left that holds no credential and no baseline "
                        "(a slot with either never counts as spare). Wait for a running dispatch to finish, or add a spare slot")
EX_CONFIG = 78
EX_WRONG_USER = 77
#: [judged] a holder connection that sends nothing for this long is read as gone (EOF): the session is ENDed.
HOLDER_IDLE_S = 1800.0
#: [judged] one WAIT is held at most this long whatever the client asked.
WAIT_MAX_S = 30.0
MAX_CONNECTIONS = 64
#: [judged] holder connections are capped BELOW the connection cap, so USAGE and END always find a permit
MAX_HOLDERS = 48
#: [judged] connections past the general cap that may each carry ONE END and nothing else: an END is the way a session
#: stops, so USAGE or idle callers filling the general pool must never leave it unservable
MAX_END_RESERVE = 8
#: [judged] the absolute time an overflow connection has to deliver its whole first frame, counted from its permit
#: and not per read or per byte: past it the connection is closed and the reserve permit freed
END_RESERVE_FIRST_FRAME_S = 1.0
#: [judged] how long a holder's own cleanup waits for its END job before its thread lets go (the job runs on)
HOLDER_END_WAIT_S = 10.0
#: [judged] how long a RELEASE refused at the point of use waits for its END job before it answers: under the client's
#: RELEASE socket timeout (``session_client._CONNECT_S``, 10 s), so the refusal arrives by name and not as "unreachable".
#: The job runs on; the holder's EOF cleanup joins it. The cleanup's own ABORT settle is not bounded by this.
RELEASE_REFUSED_WAIT_S = 2.0
#: [judged] on SIGTERM/SIGINT, the whole budget for ENDing every live session before exit (under launchd's 20 s)
SHUTDOWN_BUDGET_S = 15.0
#: past this many END jobs, the finished UNPROVEN ones are dropped (a retry re-runs them anyway); a PROVEN one is never
#: dropped: it exists only for a key the index recorded, so they are bounded by the sessions this process served
_JOBS_KEEP = 512
#: END attempts on one key are serialized by one of these locks (by key hash; a fixed set, never one per key)
_END_STRIPES = 64


# --- Q1: the point-of-use check ---------------------------------------------------------------------------------------

def verify_point_of_use(proof: C.LinkProof, exposure: Optional[ActionExposure], *,
                        replay: Callable[[], Any], now: Callable[[], datetime],
                        bar_of: Callable[[str], tuple[bool, int]],
                        verify_sig: Optional[Callable[..., bool]] = None, rejudge: bool = False,
                        billing: str) -> Any:
    """The run server's whole authority check for one fire. Raises :class:`LauncherRefused` (standing, or transient for
    a ledger or store that cannot be read); returns the link's grant, as THIS replay holds it, when the fire may go on
    (a LAUNCH that stages a workspace derives the signed role's withhold against its ``agent_id`` and ``role_hash``:
    :meth:`RunServer._signed_withhold`; a brokered LAUNCH reads its dollar share from the grant's signed
    ``budget_run_usd_micros``: :func:`signed_share`).

    The ruling (Q1 = (a), Phill 11-04): the operator's enrolled signature over the agent's LINK, which names the spec
    hash, is what authorises a fire. There is no per-occurrence signature (none exists when he signs). So, at the point
    of use and against this server's OWN replay of the authority ledger (``replay()``: a verified, anchored replay; it
    raises when the ledger cannot be read):

    1. the link is a standing agent link of the proof's spec hash and store, and the two proof rows ARE the link's born
       and current rows of that replay (same seq, same chain, same record, same signer);
    2. each row's SSHSIG verifies under the enrolled public key of its signer, and that signer is still active;
    3. the link is unexpired, was born after its agent's last revoke and after the last closed bar's revoke
       (:func:`levain.authority.agent_link_arms`), and the agent has no open bar (``bar_of``);
    4. the link's billing is the launcher's (``billing``: ``subscription`` for the passthrough launcher, ``api_key`` for the
       brokered one). A subscription link: the requested exposure (calls and tunnel bytes) is inside its per-run budgets.
       An api_key link: no action exposure, and the per-run dollar figure he signed is above 0 (the share itself is that
       figure, read from the link, never from the fire).

    ``rejudge=True`` (the SYNC re-judge of a session that is already running, never a LAUNCH or a RELEASE; the server
    sets it, a frame never does) changes two things, and only these:
    * it drops the time half of 3, expiry and the clock-behind-the-log check
      (:func:`levain.authority.agent_link_unrevoked`): an expiry was never a reason to stop a running session;
    * in 1, the proof's CURRENT row may be an earlier row than the link's current one, when the link was renewed while
      the session ran (L3 lease r2 MED, codex: a renew moves ``grant_seqs``, and the exact check ENDed the healthy
      session of the operator's own renewal). The proof's two rows must still be authentic rows of the ledger with
      active signers, the born row exactly the link's born row (the same incarnation) and the current row at or after
      it and before the link's current row; and the link's current row's signer must be active too. What can lie
      between them is a renew and nothing else: an agent link's terms are fixed under its born row, and a replay
      refuses any change to one but a ``renew``, which moves only its expiry
      (:func:`levain.authority._agent_link_step_checks`). So the budgets judged in 4 are the very ones the session
      launched under. The proof re-judged is the one this server's LAUNCH and RELEASE judged exactly; a LAUNCH or a
      RELEASE with a proof the ledger has since moved past still refuses ``link-proof-mismatch``.

    Replacing this with a stronger per-occurrence check touches this function only."""
    verify_sig = verify_sig or _default_verify_sig()
    try:
        state = replay()
    except LauncherRefused:
        raise                 # the replay's own named refusal (``ledger-behind``) keeps its name
    except Exception as exc:  # noqa: BLE001 - a ledger that does not replay authorises nothing
        raise LauncherRefused("authority-unreadable", f"the authority ledger does not replay ({type(exc).__name__})",
                              reason_class="transient") from None
    grant = state.policy.grants.get(proof.link_id)
    born, current = state.grant_born.get(proof.link_id), state.grant_seqs.get(proof.link_id)
    if not _auth.agent_link(grant) or born is None or current is None or grant["spec_hash"] != proof.spec_hash \
            or grant["store_id"] != proof.store_id:
        raise LauncherRefused("link-not-standing", "the link is not a standing agent link of this spec and store")
    if rejudge and proof.current.seq != current:
        # a renewal while the session ran (docstring): the proof's current row is an earlier row of this incarnation
        if not born <= proof.current.seq < current:
            raise LauncherRefused("link-proof-mismatch", "the proof's row is not the link's row in the ledger")
        _row_is_the_ledgers(state, proof.born, born, verify_sig)
        _row_is_the_ledgers(state, proof.current, proof.current.seq, verify_sig)
        renewed_by = state.keys.get(state.link_signers[current - 1])     # its signature: the replay verified it
        if renewed_by is None or not renewed_by.active:
            raise LauncherRefused("signer-not-active", "the link's signer is not an active enrolled key")
    else:
        for row, want in ((proof.born, born), (proof.current, current)):
            _row_is_the_ledgers(state, row, want, verify_sig)
    try:
        barred, closed = bar_of(grant["agent_id"])
    except Exception as exc:  # noqa: BLE001
        raise LauncherRefused("bar-unreadable", f"the agent's bar cannot be read ({type(exc).__name__})",
                              reason_class="transient") from None
    if barred:
        raise LauncherRefused("agent-barred", "a revoke is open for this agent")
    live = (_auth.agent_link_unrevoked(state, proof.link_id, closed_bar_revoke_seq=closed) if rejudge
            else _auth.agent_link_arms(state, proof.link_id, now=now(), closed_bar_revoke_seq=closed))
    if not live:
        raise LauncherRefused("link-not-live", "the link is expired or was revoked")
    link_billing, calls, tunnel = _auth.agent_link_terms(grant)
    if link_billing != billing:
        raise LauncherRefused("link-billing-not-the-launchers", f"the link is a {link_billing} link, this launcher's is {billing}")
    if billing == "api_key":
        # no action caps on an api_key link: the share is the dollar figure he signed, read from his verified link here
        # and never priced by the fire (its reserved dollars are only compared: RunServer._priced). A link signing 0
        # can fund no run.
        if exposure is not None:
            raise LauncherRefused("exposure-over-link", "an api_key run carries no action exposure")
        share = grant["budget_run_usd_micros"]
        if type(share) is not int or not share > 0:
            raise LauncherRefused("link-run-budget-zero", "the link signs no dollars for a run")
        return grant
    if exposure is None or exposure.tunnel_bytes is None or exposure.actions > calls or exposure.tunnel_bytes > tunnel:
        raise LauncherRefused("exposure-over-link", "the requested exposure is not inside the link's per-run budgets")
    return grant


def signed_share(grant: Any) -> Optional[int]:
    """The per-run dollar share, in micro-dollars, of a grant :func:`verify_point_of_use` returned: the figure he signed
    on an ``api_key`` link, read from his verified link and never from the fire. None for a subscription link, a grant
    that is no mapping, or a figure that is not a positive int (the brokered launcher then refuses ``no-dollar-share``)."""
    if not isinstance(grant, Mapping):
        return None
    try:
        billing = _auth.agent_link_terms(grant)[0]
    except (KeyError, TypeError):
        return None
    share = grant.get("budget_run_usd_micros")
    if billing != "api_key" or type(share) is not int or not share > 0:
        return None
    return share


def _row_is_the_ledgers(state: Any, row: C.LinkRow, want_seq: int, verify_sig: Callable[..., bool]) -> None:
    if row.seq != want_seq or not 1 <= row.seq <= state.seq or len(state.link_signers) != state.seq:
        raise LauncherRefused("link-proof-mismatch", "the proof's row is not the link's row in the ledger")
    prev = "" if row.seq == 1 else state.hash_at(row.seq - 1)
    ours = hashlib.sha256((row.prev_hash + row.record).encode("utf-8")).hexdigest()
    if prev is None or row.prev_hash != prev or state.hash_at(row.seq) != ours \
            or state.link_signers[row.seq - 1] != row.signer_fp:
        raise LauncherRefused("link-proof-mismatch", "the proof's row is not the link's row in the ledger")
    key = state.keys.get(row.signer_fp)
    if key is None or not key.active:
        raise LauncherRefused("signer-not-active", "the link's signer is not an active enrolled key")
    message = (row.prev_hash + "\n" + row.record).encode("utf-8")       # the signed message (levain.authority._message)
    try:
        good = verify_sig(message, row.signature, public_key=key.public_key, fingerprint=key.fingerprint)
    except Exception as exc:  # noqa: BLE001 - a verifier that cannot run is never a verdict
        raise LauncherRefused("verifier-unavailable", f"the signature verifier could not run ({type(exc).__name__})",
                              reason_class="transient") from None
    if good is not True:
        raise LauncherRefused("link-signature-invalid", "the link's signature does not verify")


def _default_verify_sig() -> Callable[..., bool]:
    from levain.authority_sig import verify_link_sig
    return verify_link_sig


@dataclass(frozen=True)
class FrameClaims:
    """What a LAUNCH or RELEASE frame says about the operator's side: ``ledger_head`` is the client's verified
    ``(seq, hash)``; ``bar`` is ``(open bar?, highest closed bar's revoke_seq)`` of the agent. Neither is trusted beyond
    this: the replica must hold the head (it may be ahead of it), the bar is only bounded by the head's seq.
    ``rejudge`` is never read from a frame: the server sets it for its own SYNC re-judge, which judges no expiry and
    keeps a session whose link was renewed while it ran (:func:`verify_point_of_use`)."""
    ledger_head: tuple[int, str]
    bar: tuple[bool, int]
    rejudge: bool = False


def replica_verifier(replica: AuthorityReplica, *,
                     now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                     verify_sig: Optional[Callable[..., bool]] = None, billing: str = "subscription"
                     ) -> Callable[[C.LinkProof, Optional[ActionExposure], Optional[FrameClaims]], Any]:
    """The production ``verify`` of :class:`RunServer`: :func:`verify_point_of_use` over THIS server's replica, with
    the ledger head and the bar taken from the frame. No second authority, no operator-home read. ``billing`` is the
    launcher's (``subscription`` for the passthrough launcher, ``api_key`` for the brokered one)."""
    def verify(proof: C.LinkProof, exposure: Optional[ActionExposure], claims: Optional[FrameClaims]) -> Any:
        if claims is None:
            raise LauncherRefused("ledger-head-missing", "the frame names no ledger head")
        # The claimed head must be HELD by the replica (its row at that seq, same hash), not be its exact head: another
        # fire's SYNC may append rows between this fire's SYNC and its frame (b2rel). The judgment below is always the
        # replica's WHOLE current replay, so rows past the claimed head (a revoke of this agent included) still refuse.
        seq, digest = claims.ledger_head
        try:
            head = replica.head()
        except ReplicaRefused as exc:     # the same mapping as the replay's below: head() runs first (L3 r2 #5)
            raise LauncherRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None
        except Exception:  # noqa: BLE001 - a replica that cannot be read authorises nothing
            raise LauncherRefused("authority-unreadable", "the authority replica cannot be read",
                                  reason_class="transient") from None
        if not 1 <= seq <= head[0]:
            raise LauncherRefused("ledger-behind", "the replica does not hold the claimed head: SYNC, then retry",
                                  reason_class="transient")
        barred, closed = claims.bar
        if not 0 <= closed <= seq:          # the bar was read with the claimed head: it lies inside that ledger
            raise LauncherRefused("bar-invalid", "the claimed closed bar lies outside the claimed ledger")
        try:
            state = replica.replay()
        except ReplicaRefused as exc:       # keeps its own name and class (a standing replica-not-ours stays standing)
            raise LauncherRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None
        except Exception as exc:  # noqa: BLE001 - a ledger that does not replay authorises nothing
            raise LauncherRefused("authority-unreadable", f"the authority ledger does not replay ({type(exc).__name__})",
                                  reason_class="transient") from None
        held = state.hash_at(seq)
        if held is None:                    # the replay is not the head just read: never a verdict on the claim
            raise LauncherRefused("ledger-behind", "the replica moved during the check", reason_class="transient")
        if held != digest:                  # the replica holds that seq (append-only, verified): another chain
            raise LauncherRefused("ledger-fork", "the claimed head is not on the replica's chain")
        return verify_point_of_use(proof, exposure, replay=lambda: state, now=now, bar_of=lambda agent: (barred, closed),
                                   verify_sig=verify_sig, rejudge=claims.rejudge, billing=billing)
    return verify


def agent_store_bar(db_path: "Path | str") -> Callable[[str], tuple[bool, int]]:
    """``bar_of`` over the agent store at ``db_path``, read-only: ``(has an open bar, highest closed bar's revoke_seq or
    0)``. A store that does not read raises (the caller refuses transient), never reads as "no bar"."""
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"

    def bar_of(agent_id: str) -> tuple[bool, int]:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            opened = conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                                  (agent_id,)).fetchone() is not None
            closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                                  (agent_id,)).fetchone()[0]
        finally:
            conn.close()
        return opened, 0 if closed is None else closed
    return bar_of


# --- usage readers (S10d's three, bound) ----------------------------------------------------------------------------------

def bound_usage_sources(*, queue_root: "Path | str", store: "Path | str", index_dir: "Path | str"):
    """:class:`levain.slot.dispatch_usage.DispatchUsageSources` bound to the real readers.

    * ``calls``: :func:`levain.qbroker.operator.run_state` over ``queue_root`` (``L/queue``).
    * ``tunnel_bytes``: the session's final total ROOT published at its END (CONTRACT §5.1, ``teardown``'s
      ``tunnel_bytes``), stamped on the session's ended index record by the launcher. The server no longer walks
      ``L/gw/`` (``_levain_gw<n>`` owns it, group root, no other access). None when the record is absent, not ``ended``
      or carries no int: the settle then debits the reservation (a reaper-ENDed session lands here, over-debiting
      and never under-debiting).
    * ``seconds`` [DEFINED HERE, S10d found no source]: whole seconds from the session's index record, ``started_s`` to
      ``ended_s`` rounded UP (both are this launcher's own clock and are written at the start and at the proven END);
      None when the record is absent, not ``ended`` or lacks either stamp."""
    from levain.qbroker import operator as _op
    from levain.slot import dispatch_usage as _du
    index = SessionIndex(index_dir)

    def run_state(run_id: str) -> Any:
        return _op.run_state(queue_root, run_id)

    def ended_record(session: str) -> Any:
        try:
            names = sorted(os.listdir(index.dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                rec = index.get(name[:-5])
            except Exception:  # noqa: BLE001
                continue
            if rec is not None and rec.get("session") == session:
                return rec
        return None

    def read_total(_dir: Any, session: str) -> Any:
        rec = ended_record(session)
        v = None if rec is None or rec.get("state") != "ended" else rec.get("tunnel_bytes")
        return v if type(v) is int and 0 <= v <= 2**63 - 1 else None

    def seconds_of(session: str) -> Optional[int]:
        try:
            names = sorted(os.listdir(index.dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                rec = index.get(name[:-5])
            except Exception:  # noqa: BLE001
                continue
            if rec is None or rec.get("session") != session:
                continue
            a, b = rec.get("started_s"), rec.get("ended_s")
            if rec.get("state") != "ended" or type(a) not in (int, float) or type(b) not in (int, float) or b < a:
                return None
            return int(-(-(b - a) // 1))
        return None

    return _du.DispatchUsageSources(run_state=run_state, tunnel_log=lambda session: None, seconds_of=seconds_of,
                                    read_total=read_total)


# --- the server ----------------------------------------------------------------------------------------------------------

_log = logging.getLogger("levain.slot.services.dispatch_run")


class _Holder:
    """One session this server is responsible for, registered in ``_live`` BEFORE its spawn: its session key, the
    launched child (None until the spawn answers; ``ready`` is set when it has, or when it failed), the proof and
    exposure it was launched under (re-judged before RELEASE), whether its ABORT already ran, and its END job once its
    cleanup started (``job``, set once under ``lock``: the cleanup runs once whichever of EOF, a refused RELEASE or
    shutdown reaches it first). ``ending`` is THE one "an END began for this session" fact: set before any END job for
    the key starts (the holder's own cleanup, under ``lock``; a short-connection END while the holder is registered,
    under ``lock`` unless its deadline ran out waiting for it, see :meth:`RunServer._end`), and RELEASE reads it under
    ``lock``, so a RELEASE either ran before every END or refuses and runs the cleanup itself.
    It leaves ``_live`` only when an END for its key is PROVEN."""

    def __init__(self, key: str, proof: C.LinkProof, exposure: Optional[ActionExposure],
                 claims: Optional[FrameClaims] = None, owner: "Optional[RunServer]" = None) -> None:
        self.key, self.proof, self.exposure, self.claims = key, proof, exposure, claims
        self.owner = owner              # the server whose LAUNCH spawned it: its verify re-judges it, its permit is held
        self.child: Any = None
        self.ready = threading.Event()
        self.aborted = False
        self.ending = False
        self.lock = threading.Lock()
        self.job: Optional[_EndJob] = None


#: [judged] the size past which the launchd log (the inherited stderr) is emptied before a line is written to it: a few
#: MiB holds thousands of one-line causes, and the file under KeepAlive=always has no other bound.
LOG_MAX_BYTES = 4 << 20
#: serializes the size check, the truncate AND the failure line's write: the handler threads report concurrently, and a
#: truncate between another thread's write and its flush would erase a line just written
_LOG_LOCK = threading.Lock()


def _expected_log() -> Path:
    """The file launchd opens as this unit's stderr (``units.launchd_log_path`` for ``dispatch_run``; a test pins the two
    equal). Only a descriptor that IS this file is ever trimmed."""
    return dispatch_store_dir(_host()) / f"com.levain.{NAME}.log"


def _trim_if_over(fd: int, expected: Optional[Path] = None) -> None:
    """The size check and the truncate, with :data:`_LOG_LOCK` HELD by the caller. A regular file past
    :data:`LOG_MAX_BYTES` that is the unit's own log (same device and inode as :func:`_expected_log`) is emptied; a
    terminal, a pipe, a closed descriptor, or a file redirected over fd 2 from a shell is left alone. A file of exactly
    :data:`LOG_MAX_BYTES` is left alone too (the test is "past"), so the cap is a bound plus the lines written between
    checks, not a quota."""
    try:
        st = os.fstat(fd)
        if not (stat.S_ISREG(st.st_mode) and st.st_size > LOG_MAX_BYTES):
            return
        mine = os.stat(_expected_log() if expected is None else expected)
        if (st.st_dev, st.st_ino) != (mine.st_dev, mine.st_ino):
            return
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)      # the descriptor is O_APPEND (launchd), but no stale offset is left to surprise a reader
    except OSError:
        pass


def _bound_log(fd: int = 2, expected: Optional[Path] = None) -> None:
    """Empty ``fd``'s file when it is the unit's log and past :data:`LOG_MAX_BYTES`. launchd opens the log for
    appending and never trims it, and the account cannot replace it, so the program trims the descriptor it inherited.
    (``pf.bound_log`` has this shape but no lock and a 1 MiB cap, and importing the pf program here would pull in the
    install helper.)"""
    with _LOG_LOCK:
        _trim_if_over(fd, expected)


def _refusal_cause(exc: BaseException) -> str:
    """``refused '<name>'`` for a :class:`levain.slot.Refused` (the supervisor's refusal BY NAME, CONTRACT §5), else ``""``.
    The NAME only: its ``detail`` can embed git's stderr and repo, settings and base-map paths, so it reaches neither the
    log nor the reply. The name is repr'd, so a control character in it cannot start a second log line. Any other
    exception contributes nothing: ``str(exc)`` of an arbitrary error can carry a path, a key or a ticket from the
    request (VM v2 finding 2: the name was dropped, and the operator read ``Refused at supervisor.py:407``)."""
    if not isinstance(exc, Refused):
        return ""
    return f"refused {str(exc.name)[:64]!r}"


def _report_failure(op: str, exc: BaseException) -> None:
    """ONE stderr line for an unexpected failure that is about to answer `internal-error`: the op, the exception's type
    and the file:line of its last frame, and for a supervisor :class:`Refused` its name
    (:func:`_refusal_cause`). Never ``str(exc)``: it can carry a path, a key or a ticket from the request.
    Stderr is the unit's log (journald on Linux); it was `_log.warning` before, which reaches no stream unless a
    handler is configured, so the cause appeared nowhere on the box (VM finding 5). The bound and the write share ONE
    :data:`_LOG_LOCK` span, so no other thread's truncate lands between this line's write and its flush."""
    try:
        tb = traceback.extract_tb(exc.__traceback__)
        where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"
        with _LOG_LOCK:
            _trim_if_over(2)
            cause = _refusal_cause(exc)
            tail = f" ({cause})" if cause else ""
            print(f"levain dispatch-run: op {str(op)[:32]!r} failed: {type(exc).__name__} at {where}{tail}",
                  file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - reporting must never turn a refusal into a crash
        pass


class _EndJob:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = StopResult(False, "END in progress")


class HolderRegistry:
    """The per-key state the run servers of ONE process share (PRE-W L2 HIGH-1, MED-2): the live holders, the END jobs,
    the END stripes and the SYNC re-judge flags, all guarded by ``mu``. The passthrough and brokered servers share one
    session index and one replica, so a key is one session whichever socket it was launched on: a SYNC on either socket
    re-judges every live holder (each by its own server's verify), an END on either finds the real holder and marks it
    ``ending``, and a key's teardowns serialise on one stripe. Lock order: a holder's ``lock``, then a stripe, then the
    index's per-key lock; ``mu`` is a leaf (nothing that blocks is taken under it). Each server keeps its own connection
    and holder permits and its own ``_closing``, which is read and written only under ``mu``."""

    def __init__(self) -> None:
        self.mu = threading.Condition(threading.Lock())
        self.live: dict[str, _Holder] = {}
        self.jobs: dict[str, _EndJob] = {}
        self.stripes = tuple(threading.RLock() for _ in range(_END_STRIPES))
        self.rejudge_running = False        # one SYNC re-judge worker at a time; a SYNC during a pass asks for one more
        self.rejudge_again = False
        self.servers: list["RunServer"] = []

    def all_closing(self) -> bool:
        """Every server on this registry has begun its shutdown (call under ``mu``)."""
        return all(s._closing for s in self.servers)


class _DeadlineSock:
    """A read-only view of a socket whose every recv is bounded by what is left of ONE absolute deadline, so a peer that
    drips bytes cannot stretch the time its first frame may take."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock, self._deadline = sock, deadline

    def recv(self, n: int) -> bytes:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("first-frame deadline")
        self._sock.settimeout(left)
        return self._sock.recv(n)


class RunServer:
    """The protocol and the per-key END jobs; the socket loop is :func:`serve`. ``verify(proof, exposure)`` raises
    :class:`LauncherRefused` (production: :func:`verify_point_of_use` bound to this server's readers);
    ``pool_free()`` is whether a free egress slot exists; ``peer_ok(sock)`` is the peer-uid check. With a ``replica``
    (production) ``SYNC`` feeds it and LAUNCH/RELEASE must carry ``ledger_head`` (LAUNCH also ``bar``), passed to
    ``verify`` as :class:`FrameClaims`; without one the frames carry neither and ``verify`` gets ``None``. Servers that
    share a session index share a :class:`HolderRegistry` (``registry``); without one a server makes its own."""

    #: the brokered server :func:`build_server` built beside this passthrough one (setting not opted out), else None
    brokered: "Optional[RunServer]" = None

    def __init__(self, launcher: RealLauncher, *,
                 verify: Callable[[C.LinkProof, Optional[ActionExposure], Optional[FrameClaims]], Any],
                 pool_free: Callable[[], bool], peer_ok: Callable[[socket.socket], bool],
                 holder_idle_s: float = HOLDER_IDLE_S, max_connections: int = MAX_CONNECTIONS,
                 max_holders: int = MAX_HOLDERS, max_end_reserve: int = MAX_END_RESERVE, replica: Optional[AuthorityReplica] = None,
                 uploads: Optional[WS.WorkspaceUploads] = None, registry: Optional[HolderRegistry] = None) -> None:
        if not 0 < max_holders < max_connections:
            raise ValueError("holders must be capped below the connection cap (USAGE and END need a permit)")
        self.launcher, self._verify, self._pool_free, self._peer_ok = launcher, verify, pool_free, peer_ok
        self._replica = replica
        self._uploads = uploads           # None: this server stages no workspace (a LAUNCH naming one is refused)
        self._idle = holder_idle_s
        self._reg = registry if registry is not None else HolderRegistry()
        self._mu = self._reg.mu
        self._jobs = self._reg.jobs
        self._stripes = self._reg.stripes
        self._live = self._reg.live
        self._slots = threading.BoundedSemaphore(max_connections)
        self._holder_slots = threading.BoundedSemaphore(max_holders)
        self._end_slots = threading.BoundedSemaphore(max(1, max_end_reserve))
        self._closing = False
        with self._mu:
            self._reg.servers.append(self)

    @property
    def _rejudge_running(self) -> bool:
        return self._reg.rejudge_running

    @property
    def _rejudge_again(self) -> bool:
        return self._reg.rejudge_again

    # -- connection ------------------------------------------------------------------------------------------------------

    def handle(self, sock: socket.socket) -> None:
        """Serve one connection to its end. Never raises."""
        # The verb is in the first frame, so a connection that finds the general pool full cannot be judged by its
        # verb at accept. It takes a permit from the small END reserve instead, is read under a short timeout, and is
        # served only if that frame is an END (one frame, then closed); any other first frame is refused as busy.
        end_only = False
        if not self._slots.acquire(blocking=False):
            if not self._end_slots.acquire(blocking=False):
                sock.close()
                return
            end_only = True
        mine: Optional[_Holder] = None
        owner = WS.UploadOwner()          # the workspace uploads this connection began (dropped if no LAUNCH took them)
        try:
            try:
                if not self._peer_ok(sock):
                    return
            except Exception:  # noqa: BLE001 - a peer that cannot be identified is refused
                return
            sock.settimeout(self._idle)
            first_frame_by = time.monotonic() + END_RESERVE_FIRST_FRAME_S if end_only else None
            while True:
                try:
                    req = C.read_run_frame(_DeadlineSock(sock, first_frame_by) if first_frame_by else sock)
                except (C.WireError, OSError):
                    return
                if req is None:
                    return
                sock.settimeout(self._idle)          # the reserve deadline bounded the read, not the answer
                try:
                    rid, op = C.check_run_request(req)
                except C.WireError as exc:
                    self._send(sock, C.refused_answer(None, "protocol-mismatch" if str(exc) == "protocol-mismatch"
                                                       else "bad-request", str(exc)))
                    return
                if end_only and op != "END":
                    self._send(sock, C.refused_answer(rid, "busy", "the connection pool is full", "transient"))
                    return
                try:
                    ans, mine = self._op(op, req, rid, mine, owner)
                except LauncherRefused as exc:
                    ans = C.refused_answer(rid, exc.name, exc.detail, exc.reason_class)
                except C.WireError as exc:
                    self._send(sock, C.refused_answer(rid, "bad-request", str(exc)))
                    return
                except Exception as exc:  # noqa: BLE001
                    _report_failure(op, exc)
                    ans = C.refused_answer(rid, "internal-error", _refusal_cause(exc), reason_class="transient")
                if not self._send(sock, ans) or end_only:
                    return
        finally:
            if self._uploads is not None:
                self._uploads.drop_owner(owner)
            if mine is not None:
                self._finish(mine, HOLDER_END_WAIT_S)
            try:
                sock.close()
            except OSError:
                pass
            (self._end_slots if end_only else self._slots).release()

    @staticmethod
    def _send(sock: socket.socket, ans: dict) -> bool:
        try:
            sock.sendall(C.encode_run_frame(ans))
            return True
        except (OSError, C.WireError):
            return False

    # -- ops -------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _fields(req: Mapping[str, object], *names: str) -> dict:
        extra = set(req) - {"p", "op", "id", *names}
        missing = set(names) - set(req)
        if extra or missing:
            raise C.WireError(f"unexpected {sorted(extra)} missing {sorted(missing)}")
        return {n: req[n] for n in names}

    def _op(self, op: str, req: dict, rid: int, mine: Any, owner: Optional[WS.UploadOwner] = None
            ) -> tuple[dict, Any]:
        if op == "LAUNCH":
            if mine is not None:
                raise C.WireError("one launch per connection")
            try:
                # the launch first (it reads neither claim), so every refusal after it names the key it refuses
                launch, proof = C.launch_from_wire({k: v for k, v in req.items() if k not in ("ledger_head", "bar")})
                try:
                    claims = self._claims(req, bar=True)
                    grant = self._verify(proof, launch.exposure, claims)
                    launch = self._priced(launch, grant)
                    launch = self._workspace_of(launch, owner, proof, grant)
                    self._pool()
                except WS.WorkspaceRefused as exc:
                    # standing only for a refusal of this connection's own upload (``exc.tombstone``); any other is
                    # recorded transient, so the same occurrence retakes the key (L3 w3client r2 A2, deepseek)
                    self._refuse_unspawned(launch, exc, reason_class=None if exc.tombstone else "transient")
                    raise
                except LauncherRefused as exc:
                    self._refuse_unspawned(launch, exc)
                    raise
                except C.WireError as exc:      # the frame's claims off the wire: answered ``bad-request`` below
                    self._refuse_unspawned(launch, LauncherRefused("bad-request", str(exc)))
                    raise
                except Exception as exc:  # noqa: BLE001 - answered ``internal-error``: nothing was spawned either
                    self._refuse_unspawned(launch, LauncherRefused("internal-error", type(exc).__name__,
                                                                   reason_class="transient"))
                    raise
                holder = self._spawn(launch, proof, claims)
            finally:
                # every LAUNCH ends this connection's upload BEFORE its answer is sent: a held-for-spawn upload was
                # consumed by the stager inside the spawn; any other outcome (a transient refusal included) must not
                # pin it or turn a retry's WSBEGIN into upload-exists (L3 w1srv r1, gemini HIGH 2, codex MED 1)
                if self._uploads is not None and owner is not None:
                    self._uploads.drop_owner(owner)
            try:
                return C.ok_answer(rid, session=holder.child.session), holder
            except BaseException:
                self._finish(holder, 0.0)
                raise
        if op in ("WSBEGIN", "WSDATA"):
            if mine is not None or owner is None:
                raise C.WireError("a workspace upload precedes the LAUNCH on its connection")
            return self._upload_op(op, req, rid, owner), mine
        if op == "SYNC":
            return self._sync(rid, self._fields(req, "rows")["rows"]), mine
        if op == "USAGE":
            key = self._key(self._fields(req, "session_key")["session_key"])
            return C.ok_answer(rid, usage=C.usage_to_wire(self.launcher.usage(key))), mine
        if op == "END":
            f = self._fields(req, "session_key", "deadline_ms")
            got = self._end(self._key(f["session_key"]), C._int(f["deadline_ms"], "deadline_ms") / 1000.0)
            return C.ok_answer(rid, terminated=got.terminated is True, detail=got.detail[:400]), mine
        if mine is None:
            raise C.WireError(f"{op} before LAUNCH")
        if op == "RELEASE":
            claims = self._release_claims(req)
            # the point of use is here too: a link that expired, was revoked or barred while the slot was prepared
            # starts nothing; the child is aborted and ENDed (codex r1 :339)
            try:
                self._verify(mine.proof, mine.exposure, claims)
            except LauncherRefused:
                self._finish(mine, RELEASE_REFUSED_WAIT_S)      # inside the client's RELEASE timeout (fixrel2)
                raise
            with mine.lock:
                ended = mine.ending
                if not ended:
                    mine.child.release()
            if ended:
                # the holder's cleanup runs here, not at EOF: an END that began before the child existed (during the
                # spawn) finished unproven and aborted nothing (L3 fixrel r1). No wait on the job: an END is already
                # under way, and the refusal must answer inside the client's RELEASE timeout.
                self._finish(mine, 0.0)
                raise LauncherRefused("session-ended", "this session's END already started; nothing is released")
            return C.ok_answer(rid), mine
        if op == "TERM":
            self._fields(req)
            mine.child.process.terminate()
            return C.ok_answer(rid), mine
        if op == "ABORT":
            ms = C._int(self._fields(req, "timeout_ms")["timeout_ms"], "timeout_ms")
            rc = mine.child.abort(ms / 1000.0)
            mine.aborted = True
            return C.ok_answer(rid, returncode=rc), mine
        if op == "WAIT":
            ms = C._int(self._fields(req, "timeout_ms")["timeout_ms"], "timeout_ms")
            try:
                rc = mine.child.process.wait(min(ms / 1000.0, WAIT_MAX_S))
            except subprocess.TimeoutExpired:
                return C.ok_answer(rid, exited=False, returncode=None), mine
            return C.ok_answer(rid, exited=True, returncode=rc), mine
        raise C.WireError(f"unknown op {op!r}")

    # -- the workspace upload (levain 34, W1) ------------------------------------------------------------------------------

    def _upload_op(self, op: str, req: dict, rid: int, owner: WS.UploadOwner) -> dict:
        """WSBEGIN / WSDATA: the frames that carry the snapshot manifest and bundle ahead of a LAUNCH. A frame off the
        shape drops the connection (``WireError``); a refusal by the upload (over cap, out of order, no capacity)
        answers by name and, for a refused chunk, has already dropped the upload."""
        if self._uploads is None:
            raise WS.WorkspaceRefused("unavailable", "this server stages no workspace")
        if op == "WSBEGIN":
            b = C.ws_begin_from_wire(req)
            self._uploads.begin(self._key(b.session_key), owner, b.sizes, b.digests)
            return C.ok_answer(rid)
        key, part, offset, raw = C.ws_chunk_from_wire(req)
        return C.ok_answer(rid, received=self._uploads.chunk(self._key(key), owner, part, offset, raw))

    def _priced(self, launch: Any, grant: Any) -> Any:
        """The launch with a BROKERED launcher's dollar share stamped on it: the verified link's signed figure
        (:func:`signed_share`), which the ticket is minted with. The fire's own ``usd_micros`` (the dollars its
        reservation took from the window) never prices the run: it must EQUAL that figure, or the launch is refused
        ``dollars-not-the-links-share`` before anything is minted, so a keys cap is never above what the window was
        debited (S-B1 admits an exposure below the figure: wave-3 RETURN finding 1). A grant that holds no figure and a fire
        that sent none stamp None, so the launcher refuses ``no-dollar-share``; a fire's figure against a grant that
        holds none is ``dollars-not-the-links-share``. A passthrough launch carries no dollars
        (``dollars-on-a-passthrough-launch``) and is otherwise returned as it came."""
        if getattr(self.launcher, "auth", None) != "brokered":
            if launch.usd_micros is not None:
                raise LauncherRefused("dollars-on-a-passthrough-launch", "a passthrough run reserves no dollars")
            return launch
        share = signed_share(grant)
        # a fire's figure against a grant that signs none is refused by THIS name too (L3 r1 deepseek LOW 1)
        if launch.usd_micros != share:
            raise LauncherRefused("dollars-not-the-links-share",
                                  "the fire's reserved dollars are not the link's signed per-run figure")
        return dataclasses.replace(launch, usd_micros=share)

    def _workspace_of(self, launch: Any, owner: Optional[WS.UploadOwner], proof: Any = None, grant: Any = None) -> Any:
        """The LAUNCH's workspace judgment, after the authority check and before the pool check or any mint: the launch
        itself when it names none and no upload exists; the launch with its member's ``manifest_digest`` filled by the
        server's own derivation when the upload verifies; else ``session_refused:workspace``. The LAUNCH records every
        such refusal as the zero-usage ``refused`` tombstone, so the fire's usage read settles at nothing. Only a
        standing refusal of the upload THIS connection holds for the key (``exc.tombstone``) is recorded standing; a
        refusal for no upload here, a foreign one, or a transient host failure is recorded transient, which the same
        occurrence's retry retakes, so it never poisons another connection's upload or a retry (L3 w1srv r1, gemini
        HIGH 1, codex MED 2 and 4; L3 w3client r2 A2, deepseek)."""
        try:
            if self._uploads is None:
                if launch.workspace is None:
                    return launch
                raise WS.WorkspaceRefused("unavailable", "this server stages no workspace")
            ws = self._uploads.verify(launch.session_key, owner if owner is not None else WS.UploadOwner(),
                                      launch.workspace, signed_withhold=lambda: self._signed_withhold(proof, grant))
        except WS.WorkspaceRefused as exc:
            _log.warning("workspace refused for %s: %s", launch.session_key, exc.cause)
            raise
        return launch if ws is None else dataclasses.replace(launch, workspace=ws)

    @staticmethod
    def _signed_withhold(proof: Any, grant: Any) -> tuple:
        """The withhold the SIGNED role declares (levain 1107, W2), for a launch that names a workspace: re-derived from
        the proof's role evidence against the link's ``agent_id``, ``spec_hash`` and ``role_hash`` as ``grant`` (the
        grant the point-of-use check just judged, from THIS server's replay) names them
        (:func:`levain.agents.signed_role.signed_withhold`); a spec that pins another role than the link names derives
        nothing (L3 W2 r1 HIGH, codex). Called by :meth:`WorkspaceUploads.verify` inside its judgment, only for a
        member with this connection's upload: a proof that carries no evidence, or evidence that does not derive the
        signed role, is ``session_refused:workspace``, standing (the bytes will not become the signed role's by waiting),
        and tombstones the key like every other standing refusal of the upload (L3 W2 r1 LOW, complement)."""
        signed = getattr(proof, "signed", None)
        if signed is None:
            raise WS.WorkspaceRefused("signed-role-missing", "the proof carries no role evidence for the workspace")
        try:
            return signed_withhold(signed, SignedLink.of_grant(grant))
        except SignedRoleRefused as exc:
            raise WS.WorkspaceRefused(f"signed-role-{exc.name}", exc.detail) from None

    @staticmethod
    def _key(v: object) -> str:
        if type(v) is not str or _ctl.SESSION_KEY_RE.fullmatch(v) is None:
            raise C.WireError("session_key is off the grammar")
        return v

    def _claims(self, req: Mapping[str, object], *, bar: bool) -> Optional[FrameClaims]:
        """The frame's ``ledger_head`` (and ``bar``), decoded; None on a server without a replica."""
        if self._replica is None:
            return None
        head = C._exact(req.get("ledger_head"), ("seq", "hash"), "ledger_head")
        seq, digest = C._int(head["seq"], "ledger_head.seq"), C._str(head["hash"], "ledger_head.hash", max_len=128)
        if bar:
            b = C._exact(req.get("bar"), ("open", "closed_revoke_seq"), "bar")
            if type(b["open"]) is not bool:
                raise C.WireError("bar.open is not a boolean")
            return FrameClaims((seq, digest), (b["open"], C._int(b["closed_revoke_seq"], "bar.closed_revoke_seq")))
        return FrameClaims((seq, digest), (False, 0))

    def _release_claims(self, req: Mapping[str, object]) -> Optional[FrameClaims]:
        """RELEASE: its own ``ledger_head`` (a fresh SYNC may have moved it) and its own ``bar``, read at RELEASE, so the
        point of use judges the bar as it is now (L3 r1: the LAUNCH's bar missed a bar opened in between)."""
        if self._replica is None:
            self._fields(req)
            return None
        self._fields(req, "ledger_head", "bar")
        return self._claims(req, bar=True)

    def _sync(self, rid: int, rows: object) -> dict:
        """``SYNC``: append the rows that continue the replica's head (none: just read it); answer the new head."""
        if self._replica is None:
            raise LauncherRefused("sync-unavailable", "this server holds no authority replica")
        if type(rows) is not list:
            raise C.WireError("rows is not a list")
        keys = ("seq", "prev_hash", "record", "signer_fp", "signature", "hash")
        decoded = []
        for i, o in enumerate(rows):
            o = C._exact(o, keys, f"rows[{i}]")
            decoded.append(ReplicaRow(C._int(o["seq"], "seq", lo=1), C._str(o["prev_hash"], "prev_hash", max_len=128),
                                      C._str(o["record"], "record", max_len=64 * 1024),
                                      C._str(o["signer_fp"], "signer_fp", max_len=256),
                                      C._str(o["signature"], "signature", max_len=16 * 1024),
                                      C._str(o["hash"], "hash", max_len=128)))
        try:
            seq, digest = self._replica.append(decoded)
        except ReplicaRefused as exc:
            raise LauncherRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None
        except Exception as exc:  # noqa: BLE001
            _log.warning("SYNC failed (%s)", type(exc).__name__)
            raise LauncherRefused("replica-unreadable", "the authority replica could not be updated",
                                  reason_class="transient") from None
        self._rejudge_soon()
        return C.ok_answer(rid, head={"seq": seq, "hash": digest})

    def _rejudge_soon(self) -> None:
        """After a SYNC that landed: have a worker re-judge every live holder (module docstring, "SYNC re-judges"). One
        worker at a time; a SYNC that lands during a pass sets ``_rejudge_again`` so that pass runs once more and sees
        its rows. Never raises, never waits: the SYNC's answer is not delayed, except in one case. When the worker
        thread cannot be started (thread exhaustion), the pass runs HERE, on the SYNC's own thread, before it answers
        (L3 r1: a swallowed start lost a committed revoke on a quiet box). A delayed answer is the price of never losing a
        pass; a pending flag waiting for a next SYNC that may never come was the alternative, and it can lose one.
        The worker and its flags are the registry's: one pass covers the live holders of every server sharing it."""
        reg = self._reg
        with self._mu:
            if reg.all_closing():
                return
            reg.rejudge_again = True
            if reg.rejudge_running:
                return
            reg.rejudge_running = True
        try:
            threading.Thread(target=self._rejudge_loop, daemon=True).start()
        except Exception:  # noqa: BLE001
            _log.warning("a SYNC re-judge worker could not start; the pass runs on the SYNC's thread")
            self._rejudge_loop()

    def _rejudge_loop(self) -> None:
        """Passes until no SYNC asked for another. Every exit clears ``_rejudge_running``, and the exception exit
        re-checks ``_rejudge_again`` (a SYNC that landed during the pass) and asks for a fresh worker, so a raise loses no
        queued pass (L3 r1). Each holder is judged in its own ``try``: one holder that raises skips no other. Each holder
        is judged by the server that spawned it (``owner``); a holder whose server is shutting down is left to that
        shutdown."""
        reg = self._reg
        try:
            while True:
                with self._mu:
                    if not reg.rejudge_again or reg.all_closing():
                        reg.rejudge_again = False
                        reg.rejudge_running = False
                        return
                    reg.rejudge_again = False
                    holders = [h for h in self._live.values() if not (h.owner or self)._closing]
                for h in holders:
                    try:
                        (h.owner or self)._rejudge(h)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("SYNC re-judge of %s raised (%s)", h.key, type(exc).__name__)
        except BaseException:  # noqa: BLE001 - the flag must come back, whatever raised
            with self._mu:
                reg.rejudge_running = False
                again = reg.rejudge_again and not reg.all_closing()
            _log.warning("a SYNC re-judge pass raised")
            if again:
                self._rejudge_soon()

    def _rejudge(self, h: _Holder) -> None:
        """One holder, judged by the very ``verify`` LAUNCH and RELEASE call, with its LAUNCH proof, exposure and claims
        (marked ``rejudge``, so expiry is not judged: :func:`verify_point_of_use`). A holder with no child yet is skipped
        (its RELEASE judges it), and so is one whose key's END job is in flight or PROVEN. One whose END began but whose
        last job finished UNPROVEN is judged again, and a standing refusal starts a fresh job (:meth:`_start_end`): a
        revoked session whose first END failed is retried on every SYNC, never skipped for good (codex HIGH, L3 r1).
        STANDING refusal: ``_end(key, 0.0)`` (the END job, ``ending`` marked, no wait); TRANSIENT or unexpected: nothing
        ends, one log line. The verify and the END are each caught here."""
        if h.child is None:
            return
        if h.ending or h.job is not None:
            with self._mu:
                job = self._jobs.get(h.key)
            if job is not None and (not job.done.is_set() or job.result.terminated is True):
                return
        if h.claims is None:
            # Unreachable by construction (L3 lease r2, deepseek MED): a re-judge runs only after a SYNC, a SYNC only
            # on a server with a replica, and there every LAUNCH decodes its claims or is refused before a holder
            # exists (``_claims``). Were it reached, the replica verifier would refuse ``ledger-head-missing``, a
            # STANDING refusal, and this pass would END a session for a hole in its own record. A session with no
            # claims has nothing the replica can judge it by, so it is skipped, by name; the operator tick still judges it.
            _log.warning("SYNC re-judge of %s: the holder carries no frame claims; not judged, nothing ended", h.key)
            return
        claims = FrameClaims(h.claims.ledger_head, h.claims.bar, rejudge=True)
        try:
            self._verify(h.proof, h.exposure, claims)
            return
        except LauncherRefused as exc:
            if exc.reason_class != "standing":
                _log.warning("SYNC re-judge of %s could not decide (%s); nothing ended", h.key, exc.name)
                return
            _log.warning("SYNC re-judge of %s: its authority no longer holds (%s); ENDing it", h.key, exc.name)
        except Exception as exc:  # noqa: BLE001 - a verifier that broke is not a revoke
            _log.warning("SYNC re-judge of %s raised (%s); nothing ended", h.key, type(exc).__name__)
            return
        try:
            self._end(h.key, 0.0)
        except Exception as exc:  # noqa: BLE001 - the next SYNC judges it again (its job is not in flight or proven)
            _log.warning("SYNC re-judge END of %s raised (%s); the next SYNC retries", h.key, type(exc).__name__)

    def _pool(self) -> None:
        try:
            free = self._pool_free()
        except Exception:  # noqa: BLE001
            raise LauncherRefused("control-down", "CONTROL's pool cannot be read", reason_class="transient") from None
        if not free:
            raise LauncherRefused("no-free-slot", _NO_FREE_SLOT_DETAIL, reason_class="transient")

    def _refuse_unspawned(self, launch: Any, exc: LauncherRefused, *, reason_class: Optional[str] = None) -> None:
        """A LAUNCH refused before the launcher was asked to spawn started nothing: record its key as the zero-usage
        ``refused`` tombstone (:meth:`RealLauncher.refuse_key`), so the fire's usage read settles at nothing instead
        of at the caps (L3 w3client r1, codex HIGH 1). Not for a key a live holder holds (its spawn writes the record),
        and only where the key holds no record. A transient refusal stays retakable by the same occurrence;
        ``reason_class`` records the refusal under another class than its own (a workspace refusal that is not this
        connection's to make standing)."""
        refuse = getattr(self.launcher, "refuse_key", None)
        if refuse is None:
            return
        with self._mu:
            if launch.session_key in self._live:
                return
        refuse(launch.session_key, exc.name, occurrence_id=launch.occurrence_id,
               reason_class=reason_class or exc.reason_class)

    # -- holders -----------------------------------------------------------------------------------------------------

    def _spawn(self, launch: Any, proof: C.LinkProof, claims: Optional[FrameClaims] = None) -> _Holder:
        """``RealLauncher.spawn`` under a holder permit (capped below the connection cap). The holder is registered in
        ``_live`` BEFORE the blocking spawn, in the same critical section that reads ``_closing``, so a shutdown's
        snapshot holds every spawn that can still produce a session; refused transient while the server is stopping."""
        key = launch.session_key
        unspawned: Optional[LauncherRefused] = None
        with self._mu:
            if key in self._live:       # the registry's: a key held on the sibling socket is held here too
                # never recorded as refused: the record is the holder's spawn's to write
                raise LauncherRefused("session-key-already-used", "this server already holds a session under this key")
            if self._closing:
                unspawned = LauncherRefused("server-stopping", "the run server is stopping", reason_class="transient")
            elif not self._holder_slots.acquire(blocking=False):
                unspawned = LauncherRefused("no-holder-capacity", "every holder slot is taken", reason_class="transient")
            else:
                holder = self._live[key] = _Holder(key, proof, launch.exposure, claims, owner=self)
        if unspawned is not None:
            self._refuse_unspawned(launch, unspawned)       # outside ``mu``: it writes the index
            raise unspawned
        try:
            child = self.launcher.spawn(launch)
        except BaseException:
            with self._mu:
                if self._live.get(key) is holder:
                    del self._live[key]
            self._holder_slots.release()
            holder.ready.set()
            raise
        try:
            self._serialize_ends(key, child)    # before the child is visible, so every teardown of it takes the lock
        except BaseException:
            holder.child = child
            holder.ready.set()
            self._finish(holder, 0.0)
            raise
        holder.child = child
        holder.ready.set()
        with self._mu:
            closing = self._closing
        if closing:                     # a shutdown began during the spawn: END it now (shutdown also may, once)
            self._finish(holder, 0.0)
            raise LauncherRefused("server-stopping", "the run server is stopping", reason_class="transient")
        return holder

    def _finish(self, h: _Holder, wait_s: float) -> None:
        """The holder's cleanup, once (whichever of EOF, a refused RELEASE or shutdown comes first): ABORT if it was
        never released, then END through the key's ONE END job (:meth:`_start_end`), so a reaper's END for the same
        key joins it instead of racing it; wait up to ``wait_s`` for the job. Never raises.

        The holder leaves ``_live`` only once an END for its key is PROVEN (here, or in :meth:`_run_end`). An END that
        does not prove (abort's and the job's both failed) keeps it registered as UNPROVEN: a shutdown re-attempts it,
        a later END for the key starts a fresh job, and the index record stays not-``ended``, so the root reaper's
        periodic END (S11/S11b) is the retry, and so is a SYNC re-judge that finds the session's authority gone. There
        is no retry loop here on purpose."""
        try:
            with h.lock:
                if h.job is None:
                    h.ending = True
                    try:
                        if not h.aborted and not getattr(h.child, "released", False):
                            h.child.abort()
                            h.aborted = True
                    except Exception:  # noqa: BLE001 - the END below is the proof
                        _log.warning("a holder's ABORT raised")
                    h.job = self._start_end(h.key)
                    (h.owner or self)._holder_slots.release()   # the spawning connection's permit; _live outlives it
                    self._forget_if_proven(h.key, h.job)
                job = h.job
            job.done.wait(max(0.0, wait_s))
        except Exception:  # noqa: BLE001
            _log.warning("a holder's cleanup raised")

    def _forget_if_proven(self, key: str, job: "_EndJob") -> None:
        """Drop ``key``'s holder from ``_live`` when ``job`` proved its END and the holder's cleanup has started."""
        with self._mu:
            h = self._live.get(key)
            if h is not None and h.job is not None and job.done.is_set() and job.result.terminated is True:
                del self._live[key]

    def _stripe(self, key: str) -> "threading.RLock":
        return self._stripes[int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % len(self._stripes)]

    def _serialize_ends(self, key: str, child: Any) -> None:
        """Every END attempt the launched child makes itself (WAIT's settle, ABORT, a poll: all go through its
        ``_EndProof.attempt``, the one the launcher's ``_Launched`` and ``HeldChild`` share) takes the same key lock
        the END job holds, so two teardowns of one session never run at once; the later one reads the ``ended``
        record the first wrote."""
        proof = getattr(child, "proof", None)
        attempt = getattr(proof, "attempt", None)
        if attempt is None:
            return
        lock = self._stripe(key)

        def locked(*a: Any, **kw: Any) -> Any:
            with lock:
                return attempt(*a, **kw)
        proof.attempt = locked

    def shutdown(self, budget_s: float = SHUTDOWN_BUDGET_S) -> None:
        """On SIGTERM/SIGINT, before exit (handler threads are daemons: their own cleanup never runs at exit): refuse
        new LAUNCHes and END every registered session, in-flight spawns and UNPROVEN ones included, each on its own
        thread, returning by the ABSOLUTE deadline ``budget_s`` from now whatever blocks (a spawn, a holder lock, an
        ABORT, a stripe). A session not proven by then keeps its index record not-``ended``: the root reaper's retry
        owns it. Never raises."""
        end_at = time.monotonic() + max(0.0, budget_s)
        try:
            with self._mu:
                self._closing = True                 # no holder registers after this, so the snapshot is complete
                # this server's own holders: a sibling sharing the registry ENDs its own in its shutdown
                holders = [h for h in self._live.values() if (h.owner or self) is self]
            threads = [threading.Thread(target=self._shutdown_one, args=(h, end_at), daemon=True) for h in holders]
            for t in threads:
                t.start()
            for t in threads:
                t.join(max(0.0, end_at - time.monotonic()))
            with self._mu:
                left = [h.key for h in holders if self._live.get(h.key) is h]
            if left:
                _log.warning("shutdown: %d session(s) not proven ENDed; their records stay for the reaper", len(left))
        except Exception:  # noqa: BLE001
            _log.warning("the shutdown END pass raised")

    def _shutdown_one(self, h: _Holder, end_at: float) -> None:
        try:
            if not h.ready.wait(max(0.0, end_at - time.monotonic())) or h.child is None:
                return                               # still spawning (or the spawn failed): its record is the reaper's
            self._finish(h, 0.0)
            self._start_end(h.key).done.wait(max(0.0, end_at - time.monotonic()))   # a fresh attempt if unproven
        except Exception:  # noqa: BLE001
            _log.warning("a shutdown END raised")

    # -- END -----------------------------------------------------------------------------------------------------------

    def _start_end(self, key: str) -> _EndJob:
        """T7: the key's one END job, started if there is none or the last one finished UNPROVEN (a retry, never
        concurrent with another). A PROVEN job is kept for good and answers every repeat; past :data:`_JOBS_KEEP` jobs
        only finished unproven ones are dropped. A job whose thread cannot start is finished UNPROVEN on the spot, so it
        never reads as in flight for good and the next END (or SYNC re-judge) starts a fresh one (L3 r1).

        The job is published under the shared ``mu`` and its thread started after ``mu`` is released: ``Thread.start``
        can block, and ``mu`` is shared by both servers' END, SYNC re-judge and shutdown (PRE-W L3 r1, codex MED). A
        caller that joins the published job before its thread runs waits on ``done`` like any other joiner; a start
        failure finishes the published job under ``mu``, so every waiter on the key wakes with the failure."""
        fresh: Optional[_EndJob] = None
        with self._mu:
            job = self._jobs.get(key)
            if job is None or (job.done.is_set() and job.result.terminated is not True):
                job = fresh = self._jobs[key] = _EndJob()
            if len(self._jobs) > _JOBS_KEEP:
                for k in [k for k, j in self._jobs.items()
                          if k != key and j.done.is_set() and j.result.terminated is not True]:
                    del self._jobs[k]
        if fresh is not None:
            try:
                threading.Thread(target=self._run_end, args=(key, fresh), daemon=True).start()
            except Exception:  # noqa: BLE001
                with self._mu:
                    fresh.result = StopResult(False, "END could not start; nothing assumed ended")
                    fresh.done.set()
        return job

    def _end(self, key: str, deadline_s: float) -> StopResult:
        """Answer within ``deadline_s`` with the key's END job's result, else that it is in progress. A holder registered
        under the key (its spawn in flight included) is marked ``ending`` under its lock BEFORE the job starts, so its
        RELEASE refuses from here on (codex HIGH, bundle L3 :471). The lock is waited on only inside ``deadline_s``: the
        holder's cleanup holds it across an ABORT that may be slow (L3 fixrel r1). On timeout the flag is set without
        it (a plain bool write); the holder is then either in its cleanup (which sets ``ending`` itself) or in a RELEASE
        that already took the lock, i.e. one that ran before this END, the case the END then stops.

        An END strictly before the key's LAUNCH registers (no holder, no index record) answers unproven by name ("no
        session is recorded under this key; nothing observed") within the deadline and marks nothing: it is never a
        false "ended", and a LAUNCH of that key afterwards is not refused. Documented edge (C1, 1104-31): the caller's
        retry (the reaper's periodic END) reaches the session once it is registered, and a revoke that moved the row
        is fenced by ``start_run``'s release transaction, which aborts the child unreleased."""
        end_at = time.monotonic() + max(0.0, deadline_s)
        with self._mu:
            h = self._live.get(key)
        if h is not None:
            if h.lock.acquire(timeout=min(max(0.0, deadline_s), threading.TIMEOUT_MAX)):
                try:
                    h.ending = True
                finally:
                    h.lock.release()
            else:
                h.ending = True
        job = self._start_end(key)
        job.done.wait(max(0.0, end_at - time.monotonic()))
        return job.result

    def _run_end(self, key: str, job: _EndJob) -> None:
        try:
            with self._stripe(key):
                got = self.launcher.stop(key, 0.0)
        except Exception:  # noqa: BLE001
            got = StopResult(False, "END could not complete; nothing assumed ended")
        job.result = got
        job.done.set()
        self._forget_if_proven(key, job)


# --- the process --------------------------------------------------------------------------------------------------------

#: accept errors a listener lives through (out of descriptors, a peer that went before its accept, a signal): the
#: loop waits this long and accepts again
_ACCEPT_RETRY = frozenset({errno.EMFILE, errno.ENFILE, errno.ECONNABORTED, errno.EINTR, errno.ENOBUFS, errno.ENOMEM})
_ACCEPT_BACKOFF_S = 0.1
#: a run of retried accept errors logs its first and then every this many (L3 w3client r2 L1, complement)
_ACCEPT_LOG_EVERY = 100


def serve(server: RunServer, listener: socket.socket, stop: threading.Event) -> None:
    """Accept until ``stop``. A recoverable accept error is waited out; any other sets ``stop``, so the process (both
    listeners) exits and the service manager starts it again, never one socket dead beside a live one (L3 w3client
    r1, codex MED 3)."""
    listener.settimeout(0.5)
    retried = 0                         # retried accept errors since the last accept that returned a connection
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError as exc:
            if exc.errno in _ACCEPT_RETRY:
                retried += 1
                if retried % _ACCEPT_LOG_EVERY == 1 or _ACCEPT_LOG_EVERY == 1:
                    print(f"levain dispatch-run: accept failed ({type(exc).__name__}: {exc}); retrying "
                          f"(retry {retried} of this run)", file=sys.stderr, flush=True)
                stop.wait(_ACCEPT_BACKOFF_S)
                continue
            print(f"levain dispatch-run: accept failed ({type(exc).__name__}: {exc}); stopping for a restart",
                  file=sys.stderr, flush=True)
            stop.set()
            return
        retried = 0
        threading.Thread(target=server.handle, args=(conn,), daemon=True).start()


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def _bind(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = os.lstat(path)
        import stat as _stat
        if not _stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
            raise Refused("socket-path-not-ours", str(path))
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o666)       # the peer-uid check, not the mode, is the gate; the directory's traverse bit scopes who can try
    sock.listen(16)
    return sock


def _parser(host: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="levain dispatch run server", description="the dispatch run server (levain-run/1)")
    ap.add_argument("--socket", default=str(C.default_run_socket()))
    ap.add_argument("--control", default=str(RUN[host] / "control" / "control.sock"))
    ap.add_argument("--store-dir", default=str(STORE[host]), help="L: the store root (gw logs, queue, dispatch)")
    ap.add_argument("--operator-record", default=None)
    ap.add_argument("--operator-uid", type=int, default=None)
    from levain.slot.layout import policy_dispatch_auth_path
    ap.add_argument("--auth-setting", default=str(policy_dispatch_auth_path(host)),
                    help="root's brokered setting (absent: ON, the brokered server serves beside the passthrough one; 'passthrough' opts out)")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.geteuid() == 0:
        print(f"levain dispatch-run: refusing to run as root (it runs as {ACCOUNT})", file=sys.stderr)
        return EX_CONFIG
    try:
        me = pwd.getpwnam(ACCOUNT).pw_uid
    except KeyError:
        me = None
    if me is None or os.geteuid() != me:
        print(f"levain dispatch-run: must run as {ACCOUNT} (the service unit runs it)", file=sys.stderr)
        return EX_WRONG_USER
    _bound_log()      # a restart under KeepAlive starts from a bounded file
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [NAME]:
        args = args[1:]
    host = _host()
    try:
        a = _parser(host).parse_args(args)
        from levain.slot.layout import operator_record_path
        from levain.slot.services.control import _operator_uid
        op_uid = a.operator_uid if a.operator_uid is not None else _operator_uid(Path(a.operator_record or operator_record_path(host)))
        # the brokered server is ON unless root's setting opts out; it serves on its own socket beside run.sock
        auth, why = read_dispatch_auth(a.auth_setting)
        if why is not None:
            print(f"levain dispatch-run: brokered launcher off: {why}", file=sys.stderr, flush=True)
        server = build_server(host, Path(a.store_dir), Path(a.control), op_uid, brokered=auth == "brokered")
        listener = _bind(Path(a.socket))
        brokered = None
        if server.brokered is not None:
            try:
                brokered = (server.brokered, _bind(Path(a.socket).parent / BROKERED_SOCKET_NAME))
            except Refused:
                # `socket-path-not-ours`: something not ours (not a socket, or another uid's) sits at the brokered
                # path inside the run server's own directory. That is an unsafe install, not a busy port, so the whole
                # service refuses (fail closed, EX_CONFIG) rather than serve run.sock beside it; ON taking OFF down is
                # accepted on this one path (PRE-W L3 r1)
                raise
            except OSError as exc:
                # ON never takes OFF's server down (PRE-W L2): the passthrough socket serves alone, as with the
                # setting OFF; the unserved brokered server is marked stopping so nothing waits on it
                print(f"levain dispatch-run: brokered socket not bound ({type(exc).__name__}: {exc}); serving the "
                      "passthrough socket only", file=sys.stderr, flush=True)
                server.brokered.shutdown(0.0)
    except Refused as exc:
        # Setup starts this unit before it writes the operator record, its LAST act (§23): on a fresh host the first
        # start finds none. EX_TEMPFAIL is restarted (Restart=always / KeepAlive); EX_CONFIG would leave it down.
        print(f"levain dispatch-run: refused: Refused: {exc}", file=sys.stderr, flush=True)
        return EX_TEMPFAIL if exc.name == "no-operator" else EX_CONFIG
    except (SystemExit, KeyError, OSError, ValueError) as exc:
        print(f"levain dispatch-run: refused: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EX_CONFIG
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    side = None
    if brokered is not None:
        side = threading.Thread(target=serve, args=(brokered[0], brokered[1], stop), name="brokered", daemon=True)
        side.start()
    try:
        serve(server, listener, stop)
    finally:
        stop.set()
        # END every live session first: daemon handlers never clean up at exit. Both servers shut down AT ONCE against
        # one absolute deadline, so the pair returns within one SHUTDOWN_BUDGET_S (a unit's stop timeout is sized for
        # one service's shutdown, not two back to back; PRE-W L3 r1)
        end_at = time.monotonic() + SHUTDOWN_BUDGET_S
        down = None
        if brokered is not None:
            side_server = brokered[0]
            down = threading.Thread(target=lambda: side_server.shutdown(max(0.0, end_at - time.monotonic())),
                                    name="brokered-down", daemon=True)
            try:
                down.start()
            except RuntimeError:
                down = None                      # no thread to spare: it runs after, in what is left of the budget
        server.shutdown(max(0.0, end_at - time.monotonic()))
        if down is not None:
            down.join(max(0.0, end_at - time.monotonic()))
        elif brokered is not None:
            brokered[0].shutdown(max(0.0, end_at - time.monotonic()))
        listener.close()
        if brokered is not None:
            brokered[1].close()
            if side is not None:
                side.join(1.0)
        close = getattr(server.launcher.runtime, "close", None)
        if close is not None:
            close()
    return 0


def build_server(host: str, store: Path, control_sock: Path, operator_uid: int, *, brokered: bool = False) -> RunServer:
    """The PASSTHROUGH server (L3 wave3 r1, codex HIGH 1). With ``brokered`` (:func:`read_dispatch_auth`: ON unless
    root's setting opts out; the parameter itself defaults False, so a caller that names none builds the one server) it
    also builds, as the returned server's ``brokered``, a BROKERED server: a
    separately constructed ``RealLauncher(auth="brokered")`` whose usage is ``spend=lineage_spend_reader(L/lineage/
    lineage.jsonl)``, which waits (bounded) for keys' run-final row and settles on it alone (a run with no final settles
    at its WHOLE share, the fail-closed rule), behind its own ``replica_verifier(replica, billing="api_key")``, served by
    :func:`main` on its own socket (:data:`BROKERED_SOCKET_NAME`). The two share the runtime, the usage sources, the
    session index (one record directory, keyed by attempt id), the ONE replica (two replica objects on one directory
    would contend on its append lock), the ONE uploads (a second sweep would delete the first's staging) and the ONE
    :class:`HolderRegistry` (a key is one session whichever socket launched it, so SYNC and END reach it from either). Both
    servers come from the one ``RunServer(`` call below with ``replica=replica`` (the claims=None ruling's pin). The
    client's ``build_launcher`` sends an ``api_key`` occurrence to the brokered socket and a subscription one to
    ``run.sock``; only an END of a brokered session whose socket is gone is sent to ``run.sock``. Never mutate a shared launcher's ``auth`` per request: concurrent launches and later
    USAGE reads would race on it.

    The production wiring: the real ports behind :class:`RealLauncher`, and the authority REPLICA at
    ``<store>/dispatch/authority`` behind :func:`replica_verifier`. Nothing of the operator's home is read or named: the operator's
    authority reaches the replica only through SYNC."""
    from levain.qbroker.peercred import peer_uid
    from levain.slot.dispatch_runtime import DispatchControlPort, HelperSlotRuntime
    index_dir = store / "dispatch" / "sessions"
    replica = AuthorityReplica(store / "dispatch" / "authority")
    uploads = WS.WorkspaceUploads(store / "dispatch" / "workspace")     # the verify's scratch; the runtime stages from it
    uploads.sweep()
    launcher = RealLauncher(DispatchControlPort(control_sock),
                            HelperSlotRuntime(host=host, control_socket=control_sock, store=store,
                                              workspace=uploads.stage, repo_settings=uploads.repo_settings),
                            bound_usage_sources(queue_root=store / "queue", store=store, index_dir=index_dir),
                            SessionIndex(index_dir))
    pool = DispatchControlPort(control_sock, ops=frozenset({"POOL"}))

    def pool_free() -> bool:
        ans = pool.call("POOL")
        return type(ans.get("egress_free")) is int and ans["egress_free"] > 0

    launchers = [(launcher, replica_verifier(replica))]
    if brokered:
        from levain.slot.dispatch_usage import lineage_spend_reader
        from levain.slot.lineage import SINK
        launchers.append((RealLauncher(DispatchControlPort(control_sock), launcher.runtime, launcher.sources,
                                       launcher.index, auth="brokered",
                                       spend=lineage_spend_reader(store / "lineage" / SINK)),
                          replica_verifier(replica, billing="api_key")))
    registry = HolderRegistry()         # one session index, so one set of holders, END jobs, stripes and re-judge
    servers = [RunServer(ln, verify=verify, pool_free=pool_free, peer_ok=lambda s: peer_uid(s) == operator_uid,
                         replica=replica, uploads=uploads, registry=registry) for ln, verify in launchers]
    if brokered:
        servers[0].brokered = servers[1]
    return servers[0]


#: the whole content of ``L/policy/dispatch_auth`` that turns the brokered launcher ON (an install that opted in
#: before the default flipped keeps it)
DISPATCH_AUTH_ON = b"brokered\n"
#: the whole content of ``L/policy/dispatch_auth`` that opts OUT: the passthrough server serves alone
DISPATCH_AUTH_OFF = b"passthrough\n"
#: the brokered server's socket, beside the passthrough one (``run.sock``): the billing decision is which
#: socket a fire reached, never a per-request change of a shared launcher. The client names it too, so it lives there.
BROKERED_SOCKET_NAME = C.BROKERED_SOCKET_NAME


def read_dispatch_auth(path: "Path | str", owner: int = 0) -> tuple[str, Optional[str]]:
    """``(auth, why)`` of the run server's brokered setting. The brokered server is ON by default: ``("brokered",
    None)`` when ``path`` is absent, or is a regular file (not a link) owned by ``owner`` (root), writable by nobody
    else, whose whole content is :data:`DISPATCH_AUTH_ON`. ``("passthrough", None)`` is the explicit opt-out: the same
    file, whose whole content is :data:`DISPATCH_AUTH_OFF`. Anything else (unreadable, another owner or mode, other
    content) is ``("passthrough", why)``, OFF with the reason (the doctor shows it): a setting someone tried to write
    and got wrong is not read as the default. Never raises."""
    import stat as _stat
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "brokered", None
    except OSError as exc:
        return "passthrough", f"the setting cannot be read ({exc.strerror or type(exc).__name__})"
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return "passthrough", "the setting is not a regular file"
        if st.st_uid != owner or st.st_mode & 0o022:
            return "passthrough", "the setting is not root's alone (another account owns it or can write it)"
        data = os.read(fd, max(len(DISPATCH_AUTH_ON), len(DISPATCH_AUTH_OFF)) + 1)
    except OSError as exc:
        return "passthrough", f"the setting cannot be read ({exc.strerror or type(exc).__name__})"
    finally:
        os.close(fd)
    if data == DISPATCH_AUTH_ON:
        return "brokered", None
    if data == DISPATCH_AUTH_OFF:
        return "passthrough", None
    return "passthrough", "the setting's content is neither exactly 'brokered' nor exactly 'passthrough'"


def service_row():
    """The unit row this program runs under: ``Service("dispatch_run", ...)`` as ``_levain_dispatch``, always restarted,
    after CONTROL and gw-keys, its stdout and stderr kept in the launchd log: the same row as
    ``levain.slot.units.by_name("dispatch_run")`` (registered in S10b slice R, with its run dir ``R/dispatch``; a test
    pins the two equal). A golden for the plist this row renders is in ``tests/test_slot_service_dispatch.py``."""
    from levain.slot.units import Service
    return Service(NAME, "dispatch run server", ACCOUNT, ("control", "keys"), "always", logs=True)


__all__ = ["ACCOUNT", "NAME", "FrameClaims", "RunServer", "replica_verifier", "service_row", "agent_store_bar", "bound_usage_sources", "build_server", "main", "serve",
           "verify_point_of_use", "BROKERED_SOCKET_NAME", "DISPATCH_AUTH_ON", "DISPATCH_AUTH_OFF", "read_dispatch_auth"]


if __name__ == "__main__":
    sys.exit(main())
