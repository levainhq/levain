"""The dispatch RUN SERVER (levain 27, slice S10b; design ``1104-27_S10_DESIGN.md`` sections 2, 3.1, 3.3, 3.4, 6): ONE
process as ``_levain_dispatch`` that owns :class:`levain.agents.session_launcher.RealLauncher` and answers the operator
uid's client (:mod:`levain.agents.session_client`, wire ``levain-run/1``) on a unix socket.

* **Peer.** A connection whose kernel peer uid is not the operator's is closed unread and unanswered.
* **LAUNCH** (a holder connection): decode, :func:`verify_point_of_use` (Q1, below), the pre-mint pool check (design port
  item 10: a transient ``no-free-slot`` when ``POOL`` shows no free egress slot, so the fire's next window retries; no
  in-server retry), then ``RealLauncher.spawn`` unchanged, under a holder permit (:data:`MAX_HOLDERS`, below the
  connection cap so USAGE and END always find one; none free is a transient ``no-holder-capacity``). The answer is
  ``{session}``; no ticket, no run id.
* **RELEASE re-runs** :func:`verify_point_of_use` with the LAUNCH's proof and exposure: a link that expired, was revoked
  or barred while the slot was prepared releases nothing, and the child is ABORTed and ENDed.
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
client's verified head, which must be the replica's, else a transient ``ledger-behind``: SYNC and retry) and ``LAUNCH``
carries the agent's ``bar`` (open flag and highest closed bar's revoke seq, checked to lie inside the replica's seq;
``RELEASE`` re-judges with the LAUNCH's bar). A false bar or a withheld row is the power a process running as the operator
already had over the live files; it can forge no signature and cannot roll the replica back (design 1104-28 section 2.4).
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import pwd
import signal
import socket
import sqlite3
import subprocess
import sys
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
from levain.slot.layout import RUN, STORE

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
                        verify_sig: Optional[Callable[..., bool]] = None) -> None:
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

    Replacing this with a stronger per-occurrence check touches this function only."""
    verify_sig = verify_sig or _default_verify_sig()
    try:
        state = replay()
    except Exception as exc:  # noqa: BLE001 - a ledger that does not replay authorises nothing
        raise LauncherRefused("authority-unreadable", f"the authority ledger does not replay ({type(exc).__name__})",
                              reason_class="transient") from None
    grant = state.policy.grants.get(proof.link_id)
    born, current = state.grant_born.get(proof.link_id), state.grant_seqs.get(proof.link_id)
    if not _auth.agent_link(grant) or born is None or current is None or grant["spec_hash"] != proof.spec_hash \
            or grant["store_id"] != proof.store_id:
        raise LauncherRefused("link-not-standing", "the link is not a standing agent link of this spec and store")
    for row, want in ((proof.born, born), (proof.current, current)):
        _row_is_the_ledgers(state, row, want, verify_sig)
    try:
        barred, closed = bar_of(grant["agent_id"])
    except Exception as exc:  # noqa: BLE001
        raise LauncherRefused("bar-unreadable", f"the agent's bar cannot be read ({type(exc).__name__})",
                              reason_class="transient") from None
    if barred:
        raise LauncherRefused("agent-barred", "a revoke is open for this agent")
    if not _auth.agent_link_arms(state, proof.link_id, now=now(), closed_bar_revoke_seq=closed):
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
    this: the head must equal the replica's, the bar is only bounded by the replica's seq."""
    ledger_head: tuple[int, str]
    bar: tuple[bool, int]


def replica_verifier(replica: AuthorityReplica, *,
                     now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                     verify_sig: Optional[Callable[..., bool]] = None
                     ) -> Callable[[C.LinkProof, Optional[ActionExposure], Optional[FrameClaims]], None]:
    """The production ``verify`` of :class:`RunServer`: :func:`verify_point_of_use` over THIS server's replica, with
    the ledger head and the bar taken from the frame. No second authority, no operator-home read."""
    def verify(proof: C.LinkProof, exposure: Optional[ActionExposure], claims: Optional[FrameClaims]) -> None:
        if claims is None:
            raise LauncherRefused("ledger-head-missing", "the frame names no ledger head")
        try:
            head = replica.head()
        except Exception:  # noqa: BLE001 - a replica that cannot be read authorises nothing
            raise LauncherRefused("authority-unreadable", "the authority replica cannot be read",
                                  reason_class="transient") from None
        if head != claims.ledger_head:
            raise LauncherRefused("ledger-behind", "the replica's ledger head is not the claimed head: SYNC, then retry",
                                  reason_class="transient")
        barred, closed = claims.bar
        if not 0 <= closed <= head[0]:
            raise LauncherRefused("bar-invalid", "the claimed closed bar lies outside the replica's ledger")

        def replay() -> Any:
            state = replica.replay()
            if (state.seq, state.head_hash) != head:
                raise LauncherRefused("ledger-behind", "the replica moved during the check", reason_class="transient")
            return state

        verify_point_of_use(proof, exposure, replay=replay, now=now, bar_of=lambda agent: (barred, closed),
                            verify_sig=verify_sig)
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
    * ``tunnel_bytes``: S10d's two-argument ``read_total(path, session)`` over every slot's tunnel log
      ``<store>/gw/slot<n>/front.log``; a session id is unique, so the first slot whose log holds that session's final
      total answers (the session's slot is not known to the server).
    * ``seconds`` [DEFINED HERE, S10d found no source]: whole seconds from the session's index record, ``started_s`` to
      ``ended_s`` rounded UP (both are this launcher's own clock and are written at the start and at the proven END);
      None when the record is absent, not ``ended`` or lacks either stamp."""
    from levain.qbroker import operator as _op
    from levain.slot import dispatch_usage as _du
    from levain.slot.runtime_helper import FRONT_LOG_NAME
    index = SessionIndex(index_dir)
    gw = Path(store) / "gw"

    def run_state(run_id: str) -> Any:
        return _op.run_state(queue_root, run_id)

    def read_total(_dir: Any, session: str) -> Any:
        try:
            slots = sorted(p for p in gw.iterdir() if p.name.startswith("slot"))
        except OSError:
            return None
        for slot in slots:
            got = _du._default_read_total(str(slot / FRONT_LOG_NAME), session)
            if got is not None:
                return got
        return None

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

    return _du.DispatchUsageSources(run_state=run_state, tunnel_log=lambda session: gw, seconds_of=seconds_of,
                                    read_total=read_total)


# --- the server ----------------------------------------------------------------------------------------------------------

_log = logging.getLogger("levain.slot.services.dispatch_run")


class _Holder:
    """One session this server is responsible for, registered in ``_live`` BEFORE its spawn: its session key, the
    launched child (None until the spawn answers; ``ready`` is set when it has, or when it failed), the proof and
    exposure it was launched under (re-judged before RELEASE), whether its ABORT already ran, and its END job once its
    cleanup started (``job``, set once under ``lock``: the cleanup runs once whichever of EOF, a refused RELEASE or
    shutdown reaches it first). It leaves ``_live`` only when an END for its key is PROVEN."""

    def __init__(self, key: str, proof: C.LinkProof, exposure: Optional[ActionExposure],
                 claims: Optional[FrameClaims] = None) -> None:
        self.key, self.proof, self.exposure, self.claims = key, proof, exposure, claims
        self.child: Any = None
        self.ready = threading.Event()
        self.aborted = False
        self.lock = threading.Lock()
        self.job: Optional[_EndJob] = None


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
                 max_holders: int = MAX_HOLDERS, replica: Optional[AuthorityReplica] = None) -> None:
        if not 0 < max_holders < max_connections:
            raise ValueError("holders must be capped below the connection cap (USAGE and END need a permit)")
        self.launcher, self._verify, self._pool_free, self._peer_ok = launcher, verify, pool_free, peer_ok
        self._replica = replica
        self._idle = holder_idle_s
        self._mu = threading.Condition(threading.Lock())
        self._jobs: dict[str, _EndJob] = {}
        self._slots = threading.BoundedSemaphore(max_connections)
        self._holder_slots = threading.BoundedSemaphore(max_holders)
        self._stripes = tuple(threading.RLock() for _ in range(_END_STRIPES))
        self._live: dict[str, _Holder] = {}
        self._closing = False

    # -- connection ------------------------------------------------------------------------------------------------------

    def handle(self, sock: socket.socket) -> None:
        """Serve one connection to its end. Never raises."""
        if not self._slots.acquire(blocking=False):
            sock.close()
            return
        mine: Optional[_Holder] = None
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
                    ans, mine = self._op(op, req, rid, mine)
                except LauncherRefused as exc:
                    ans = C.refused_answer(rid, exc.name, exc.detail, exc.reason_class)
                except C.WireError as exc:
                    self._send(sock, C.refused_answer(rid, "bad-request", str(exc)))
                    return
                except Exception as exc:  # noqa: BLE001
                    _log.warning("op %s failed (%s)", op, type(exc).__name__)
                    ans = C.refused_answer(rid, "internal-error", reason_class="transient")
                if not self._send(sock, ans):
                    return
        finally:
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

    def _op(self, op: str, req: dict, rid: int, mine: Any) -> tuple[dict, Any]:
        if op == "LAUNCH":
            if mine is not None:
                raise C.WireError("one launch per connection")
            claims = self._claims(req, bar=True)
            launch, proof = C.launch_from_wire({k: v for k, v in req.items() if k not in ("ledger_head", "bar")})
            self._verify(proof, launch.exposure, claims)
            self._pool()
            holder = self._spawn(launch, proof, claims)
            try:
                return C.ok_answer(rid, session=holder.child.session), holder
            except BaseException:
                self._finish(holder, 0.0)
                raise
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
            claims = self._release_claims(req, mine)
            # the point of use is here too: a link that expired, was revoked or barred while the slot was prepared
            # starts nothing; the child is aborted and ENDed (codex r1 :339)
            try:
                self._verify(mine.proof, mine.exposure, claims)
            except LauncherRefused:
                self._finish(mine, HOLDER_END_WAIT_S)
                raise
            with mine.lock:
                if mine.job is not None:
                    raise LauncherRefused("session-ended", "this session's END already started; nothing is released")
                mine.child.release()
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

    def _release_claims(self, req: Mapping[str, object], holder: "_Holder") -> Optional[FrameClaims]:
        """RELEASE: its own ``ledger_head`` (a fresh SYNC may have moved it), the LAUNCH's bar."""
        if self._replica is None:
            self._fields(req)
            return None
        self._fields(req, "ledger_head")
        head = self._claims(req, bar=False)
        assert head is not None
        return FrameClaims(head.ledger_head, holder.claims.bar if holder.claims is not None else (False, 0))

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
        return C.ok_answer(rid, head={"seq": seq, "hash": digest})

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
        periodic END (S11/S11b) is the retry. There is no retry loop here on purpose."""
        try:
            with h.lock:
                if h.job is None:
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
        only finished unproven ones are dropped."""
        with self._mu:
            job = self._jobs.get(key)
            if job is None or (job.done.is_set() and job.result.terminated is not True):
                job = self._jobs[key] = _EndJob()
                threading.Thread(target=self._run_end, args=(key, job), daemon=True).start()
            if len(self._jobs) > _JOBS_KEEP:
                for k in [k for k, j in self._jobs.items()
                          if k != key and j.done.is_set() and j.result.terminated is not True]:
                    del self._jobs[k]
        return job

    def _end(self, key: str, deadline_s: float) -> StopResult:
        """Answer within ``deadline_s`` with the key's END job's result, else that it is in progress."""
        job = self._start_end(key)
        job.done.wait(max(0.0, deadline_s))
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
    launcher = RealLauncher(DispatchControlPort(control_sock),
                            HelperSlotRuntime(host=host, control_socket=control_sock, store=store),
                            bound_usage_sources(queue_root=store / "queue", store=store, index_dir=index_dir),
                            SessionIndex(index_dir))
    pool = DispatchControlPort(control_sock, ops=frozenset({"POOL"}))

    def pool_free() -> bool:
        ans = pool.call("POOL")
        return type(ans.get("egress_free")) is int and ans["egress_free"] > 0

    return RunServer(launcher, verify=replica_verifier(replica), pool_free=pool_free,
                     peer_ok=lambda s: peer_uid(s) == operator_uid, replica=replica)


def service_row():
    """The unit row this program runs under: ``Service("dispatch_run", ...)`` as ``_levain_dispatch``, always restarted,
    after CONTROL and gw-keys: the same row as ``levain.slot.units.by_name("dispatch_run")`` (registered in S10b slice
    R, with its run dir ``R/dispatch``). A golden for the plist this row renders is in
    ``tests/test_slot_dispatch_run_server.py``."""
    from levain.slot.units import Service
    return Service(NAME, "dispatch run server", ACCOUNT, ("control", "keys"), "always")


__all__ = ["ACCOUNT", "NAME", "FrameClaims", "RunServer", "replica_verifier", "service_row", "agent_store_bar", "bound_usage_sources", "build_server", "main", "serve",
           "verify_point_of_use"]


if __name__ == "__main__":
    sys.exit(main())
