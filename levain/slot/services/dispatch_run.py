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
  refuses ``session_refused:workspace`` on any mismatch, with nothing staged (and a zero-usage ``refused`` record only
  for a standing refusal of this connection's own upload for the key); the slot runtime later writes the verified bytes
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
from levain.slot import workspace as WS
from levain.slot.layout import RUN, STORE, dispatch_store_dir

NAME = "dispatch_run"
ACCOUNT = "_levain_dispatch"
EX_USAGE = 64
EX_TEMPFAIL = 75
EX_CONFIG = 78
EX_WRONG_USER = 77
#: [judged] a holder connection that sends nothing for this long is read as gone (EOF): the session is ENDed.
HOLDER_IDLE_S = 1800.0
#: [judged] one WAIT is held at most this long whatever the client asked.
WAIT_MAX_S = 30.0
MAX_CONNECTIONS = 64
#: [judged] holder connections are capped BELOW the connection cap, so USAGE and END always find a permit
MAX_HOLDERS = 48
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
                        verify_sig: Optional[Callable[..., bool]] = None, rejudge: bool = False) -> None:
    """The run server's whole authority check for one fire. Raises :class:`LauncherRefused` (standing, or transient for
    a ledger or store that cannot be read); returns None when the fire may go on.

    The ruling (Q1 = (a), Phill 11-04): the operator's enrolled signature over the agent's LINK, which names the spec
    hash, is what authorises a fire. There is no per-occurrence signature (none exists when he signs). So, at the point
    of use and against this server's OWN replay of the authority ledger (``replay()``: a verified, anchored replay; it
    raises when the ledger cannot be read):

    1. the link is a standing agent link of the proof's spec hash and store, and the two proof rows ARE the link's born
       and current rows of that replay (same seq, same chain, same record, same signer);
    2. each row's SSHSIG verifies under the enrolled public key of its signer, and that signer is still active;
    3. the link is unexpired, was born after its agent's last revoke and after the last closed bar's revoke
       (:func:`levain.authority.agent_link_arms`), and the agent has no open bar (``bar_of``);
    4. the requested exposure (calls and tunnel bytes) is inside the link's per-run budgets, on a subscription link.

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
    billing, calls, tunnel = _auth.agent_link_terms(grant)
    if billing != "subscription":
        raise LauncherRefused("link-not-subscription", "the link is not a subscription link")
    if exposure is None or exposure.tunnel_bytes is None or exposure.actions > calls or exposure.tunnel_bytes > tunnel:
        raise LauncherRefused("exposure-over-link", "the requested exposure is not inside the link's per-run budgets")


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
                     verify_sig: Optional[Callable[..., bool]] = None
                     ) -> Callable[[C.LinkProof, Optional[ActionExposure], Optional[FrameClaims]], None]:
    """The production ``verify`` of :class:`RunServer`: :func:`verify_point_of_use` over THIS server's replica, with
    the ledger head and the bar taken from the frame. No second authority, no operator-home read."""
    def verify(proof: C.LinkProof, exposure: Optional[ActionExposure], claims: Optional[FrameClaims]) -> None:
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
        verify_point_of_use(proof, exposure, replay=lambda: state, now=now, bar_of=lambda agent: (barred, closed),
                            verify_sig=verify_sig, rejudge=claims.rejudge)
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
                 claims: Optional[FrameClaims] = None) -> None:
        self.key, self.proof, self.exposure, self.claims = key, proof, exposure, claims
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


#: the longest refusal detail a cause line or an `internal-error` reply carries
_REFUSAL_DETAIL_MAX = 120


def _refusal_cause(exc: BaseException) -> str:
    """``name: detail`` of a :class:`levain.slot.Refused` (the supervisor's refusal BY NAME, CONTRACT §5), else ``""``.
    The name is the stable reason and the detail is its one human line; both are repr'd, so a control character in either
    cannot start a second log line, and the detail is capped. Any other exception contributes nothing: ``str(exc)`` of an
    arbitrary error can carry a path, a key or a ticket from the request (VM v2 finding 2: the name was dropped, and the
    operator read ``Refused at supervisor.py:407``)."""
    if not isinstance(exc, Refused):
        return ""
    name = repr(str(exc.name)[:64])
    detail = str(exc.detail)[:_REFUSAL_DETAIL_MAX]
    return f"{name}: {detail!r}" if detail else name


def _report_failure(op: str, exc: BaseException) -> None:
    """ONE stderr line for an unexpected failure that is about to answer `internal-error`: the op, the exception's type
    and the file:line of its last frame, and for a supervisor :class:`Refused` its name and one-line detail
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


class RunServer:
    """The protocol and the per-key END jobs; the socket loop is :func:`serve`. ``verify(proof, exposure)`` raises
    :class:`LauncherRefused` (production: :func:`verify_point_of_use` bound to this server's readers);
    ``pool_free()`` is whether a free egress slot exists; ``peer_ok(sock)`` is the peer-uid check. With a ``replica``
    (production) ``SYNC`` feeds it and LAUNCH/RELEASE must carry ``ledger_head`` (LAUNCH also ``bar``), passed to
    ``verify`` as :class:`FrameClaims`; without one the frames carry neither and ``verify`` gets ``None``."""

    def __init__(self, launcher: RealLauncher, *,
                 verify: Callable[[C.LinkProof, Optional[ActionExposure], Optional[FrameClaims]], None],
                 pool_free: Callable[[], bool], peer_ok: Callable[[socket.socket], bool],
                 holder_idle_s: float = HOLDER_IDLE_S, max_connections: int = MAX_CONNECTIONS,
                 max_holders: int = MAX_HOLDERS, replica: Optional[AuthorityReplica] = None,
                 uploads: Optional[WS.WorkspaceUploads] = None) -> None:
        if not 0 < max_holders < max_connections:
            raise ValueError("holders must be capped below the connection cap (USAGE and END need a permit)")
        self.launcher, self._verify, self._pool_free, self._peer_ok = launcher, verify, pool_free, peer_ok
        self._replica = replica
        self._uploads = uploads           # None: this server stages no workspace (a LAUNCH naming one is refused)
        self._idle = holder_idle_s
        self._mu = threading.Condition(threading.Lock())
        self._jobs: dict[str, _EndJob] = {}
        self._slots = threading.BoundedSemaphore(max_connections)
        self._holder_slots = threading.BoundedSemaphore(max_holders)
        self._stripes = tuple(threading.RLock() for _ in range(_END_STRIPES))
        self._live: dict[str, _Holder] = {}
        self._closing = False
        self._rejudge_running = False       # one SYNC re-judge worker at a time; a SYNC during a pass asks for one more
        self._rejudge_again = False

    # -- connection ------------------------------------------------------------------------------------------------------

    def handle(self, sock: socket.socket) -> None:
        """Serve one connection to its end. Never raises."""
        if not self._slots.acquire(blocking=False):
            sock.close()
            return
        mine: Optional[_Holder] = None
        owner = WS.UploadOwner()          # the workspace uploads this connection began (dropped if no LAUNCH took them)
        try:
            try:
                if not self._peer_ok(sock):
                    return
            except Exception:  # noqa: BLE001 - a peer that cannot be identified is refused
                return
            sock.settimeout(self._idle)
            while True:
                try:
                    req = C.read_run_frame(sock)
                except (C.WireError, OSError):
                    return
                if req is None:
                    return
                try:
                    rid, op = C.check_run_request(req)
                except C.WireError as exc:
                    self._send(sock, C.refused_answer(None, "protocol-mismatch" if str(exc) == "protocol-mismatch"
                                                       else "bad-request", str(exc)))
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
                if not self._send(sock, ans):
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
            self._slots.release()

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
                claims = self._claims(req, bar=True)
                launch, proof = C.launch_from_wire({k: v for k, v in req.items() if k not in ("ledger_head", "bar")})
                self._verify(proof, launch.exposure, claims)
                launch = self._workspace_of(launch, owner)
                self._pool()
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

    def _workspace_of(self, launch: Any, owner: Optional[WS.UploadOwner]) -> Any:
        """The LAUNCH's workspace judgment, after the authority check and before the pool check or any mint: the launch
        itself when it names none and no upload exists; the launch with its member's ``manifest_digest`` filled by the
        server's own derivation when the upload verifies; else ``session_refused:workspace``. Only a standing refusal of
        the upload THIS connection holds for the key (``exc.tombstone``) is recorded as the zero-usage ``refused``
        tombstone (:meth:`RealLauncher.refuse_key`), so the fire's usage read settles at nothing; a refusal for no
        upload here, a foreign one, or a transient host failure writes nothing, as a pool refusal writes nothing, so
        it never poisons another connection's upload or a retry (L3 w1srv r1, gemini HIGH 1, codex MED 2 and 4)."""
        try:
            if self._uploads is None:
                if launch.workspace is None:
                    return launch
                raise WS.WorkspaceRefused("unavailable", "this server stages no workspace")
            ws = self._uploads.verify(launch.session_key, owner if owner is not None else WS.UploadOwner(),
                                      launch.workspace)
        except WS.WorkspaceRefused as exc:
            _log.warning("workspace refused for %s: %s", launch.session_key, exc.cause)
            refuse = getattr(self.launcher, "refuse_key", None)
            if exc.tombstone and refuse is not None:
                refuse(launch.session_key, WS.WORKSPACE_REFUSAL, occurrence_id=launch.occurrence_id)
            raise
        return launch if ws is None else dataclasses.replace(launch, workspace=ws)

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
        pass; a pending flag waiting for a next SYNC that may never come was the alternative, and it can lose one."""
        with self._mu:
            if self._closing:
                return
            self._rejudge_again = True
            if self._rejudge_running:
                return
            self._rejudge_running = True
        try:
            threading.Thread(target=self._rejudge_loop, daemon=True).start()
        except Exception:  # noqa: BLE001
            _log.warning("a SYNC re-judge worker could not start; the pass runs on the SYNC's thread")
            self._rejudge_loop()

    def _rejudge_loop(self) -> None:
        """Passes until no SYNC asked for another. Every exit clears ``_rejudge_running``, and the exception exit
        re-checks ``_rejudge_again`` (a SYNC that landed during the pass) and asks for a fresh worker, so a raise loses no
        queued pass (L3 r1). Each holder is judged in its own ``try``: one holder that raises skips no other."""
        try:
            while True:
                with self._mu:
                    if not self._rejudge_again or self._closing:
                        self._rejudge_again = False
                        self._rejudge_running = False
                        return
                    self._rejudge_again = False
                    holders = list(self._live.values())
                for h in holders:
                    try:
                        self._rejudge(h)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("SYNC re-judge of %s raised (%s)", h.key, type(exc).__name__)
        except BaseException:  # noqa: BLE001 - the flag must come back, whatever raised
            with self._mu:
                self._rejudge_running = False
                again = self._rejudge_again and not self._closing
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
            raise LauncherRefused("no-free-slot", "no dispatch slot is free", reason_class="transient")

    # -- holders -----------------------------------------------------------------------------------------------------

    def _spawn(self, launch: Any, proof: C.LinkProof, claims: Optional[FrameClaims] = None) -> _Holder:
        """``RealLauncher.spawn`` under a holder permit (capped below the connection cap). The holder is registered in
        ``_live`` BEFORE the blocking spawn, in the same critical section that reads ``_closing``, so a shutdown's
        snapshot holds every spawn that can still produce a session; refused transient while the server is stopping."""
        key = launch.session_key
        with self._mu:
            if self._closing:
                raise LauncherRefused("server-stopping", "the run server is stopping", reason_class="transient")
            if key in self._live:
                raise LauncherRefused("session-key-already-used", "this server already holds a session under this key")
            if not self._holder_slots.acquire(blocking=False):
                raise LauncherRefused("no-holder-capacity", "every holder slot is taken", reason_class="transient")
            holder = self._live[key] = _Holder(key, proof, launch.exposure, claims)
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
                    self._holder_slots.release()      # the connection's permit; the _live entry outlives it
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
                holders = list(self._live.values())
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
        never reads as in flight for good and the next END (or SYNC re-judge) starts a fresh one (L3 r1)."""
        with self._mu:
            job = self._jobs.get(key)
            if job is None or (job.done.is_set() and job.result.terminated is not True):
                job = self._jobs[key] = _EndJob()
                try:
                    threading.Thread(target=self._run_end, args=(key, job), daemon=True).start()
                except Exception:  # noqa: BLE001
                    job.result = StopResult(False, "END could not start; nothing assumed ended")
                    job.done.set()
            if len(self._jobs) > _JOBS_KEEP:
                for k in [k for k, j in self._jobs.items()
                          if k != key and j.done.is_set() and j.result.terminated is not True]:
                    del self._jobs[k]
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

def serve(server: RunServer, listener: socket.socket, stop: threading.Event) -> None:
    listener.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
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
        server = build_server(host, Path(a.store_dir), Path(a.control), op_uid)
        listener = _bind(Path(a.socket))
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
    try:
        serve(server, listener, stop)
    finally:
        server.shutdown(SHUTDOWN_BUDGET_S)       # END every live session first: daemon handlers never clean up at exit
        listener.close()
        close = getattr(server.launcher.runtime, "close", None)
        if close is not None:
            close()
    return 0


def build_server(host: str, store: Path, control_sock: Path, operator_uid: int) -> RunServer:
    """The production wiring: the real ports behind :class:`RealLauncher`, and the authority REPLICA at
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
                                              workspace=uploads.stage),
                            bound_usage_sources(queue_root=store / "queue", store=store, index_dir=index_dir),
                            SessionIndex(index_dir))
    pool = DispatchControlPort(control_sock, ops=frozenset({"POOL"}))

    def pool_free() -> bool:
        ans = pool.call("POOL")
        return type(ans.get("egress_free")) is int and ans["egress_free"] > 0

    return RunServer(launcher, verify=replica_verifier(replica), pool_free=pool_free,
                     peer_ok=lambda s: peer_uid(s) == operator_uid, replica=replica, uploads=uploads)


def service_row():
    """The unit row this program runs under: ``Service("dispatch_run", ...)`` as ``_levain_dispatch``, always restarted,
    after CONTROL and gw-keys, its stdout and stderr kept in the launchd log: the same row as
    ``levain.slot.units.by_name("dispatch_run")`` (registered in S10b slice R, with its run dir ``R/dispatch``; a test
    pins the two equal). A golden for the plist this row renders is in ``tests/test_slot_service_dispatch.py``."""
    from levain.slot.units import Service
    return Service(NAME, "dispatch run server", ACCOUNT, ("control", "keys"), "always", logs=True)


__all__ = ["ACCOUNT", "NAME", "FrameClaims", "RunServer", "replica_verifier", "service_row", "agent_store_bar", "bound_usage_sources", "build_server", "main", "serve",
           "verify_point_of_use"]


if __name__ == "__main__":
    sys.exit(main())
