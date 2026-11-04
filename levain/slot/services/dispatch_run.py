"""The dispatch RUN SERVER (levain 27, slice S10b; design ``1104-27_S10_DESIGN.md`` sections 2, 3.1, 3.3, 3.4, 6): ONE
process as ``_levain_dispatch`` that owns :class:`levain.agents.session_launcher.RealLauncher` and answers the operator
uid's client (:mod:`levain.agents.session_client`, wire ``levain-run/1``) on a unix socket.

* **Peer.** A connection whose kernel peer uid is not the operator's is closed unread and unanswered.
* **LAUNCH** (a holder connection): decode, :func:`verify_point_of_use` (Q1, below), the pre-mint pool check (design port
  item 10: a transient ``no-free-slot`` when ``POOL`` shows no free egress slot, so the fire's next window retries; no
  in-server retry), then ``RealLauncher.spawn`` unchanged. The answer is ``{session}``; no ticket, no run id.
* **The holder is the lifeline** (design 3.4): EOF before RELEASE is ABORT (nothing ran) then END, EOF after RELEASE is
  END, each once; the server keeps no session whose holder is gone. RELEASE, WAIT, TERM, ABORT map onto the launched
  child.
* **USAGE / END** (short connections). USAGE is ``RealLauncher.usage`` by the recorded key. END is asynchronous and
  idempotent (T7): the first END for a key starts one ``RealLauncher.stop`` on a worker and answers within the
  caller's deadline with its result, else ``StopResult(False, "END in progress")``; a later END joins the same job, and a
  proven result is never run twice. [Deviation from design section 2: a thread per connection, not one select loop,
  because WAIT is a long poll and END runs on a worker either way; the server forks nothing (RH2 holds).]

**Q1, final (Phill 11-04, np-ed47d151, "8a").** The server verifies a valid enrolled signature over the agent's LINK
that names the spec hash (his existing signature; no per-occurrence signature exists), and re-checks the link's expiry,
revocation, the agent's bar and both signers' standing against ITS OWN replay of the authority ledger, plus the
requested exposure inside the link's budgets, at the point of use: every fire. All of that is the one function
:func:`verify_point_of_use`.

**Where the readers come from** (``main``): the operator's uid is the operator record's; the operator's home is that
account's; the agent store is ``<home>/.levain/autonomic`` (the default every ``levain agents`` verb opens), and the
cockpit root is the store's ``session_install.json``. The unit has fixed argv, so there is no flag for them in an
install; the dispatch uid must be GIVEN read access to both (install step, UNVERIFIED here).
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
from typing import Any, Optional, Sequence
from urllib.parse import quote

from levain import authority as _auth
from levain.agents import controller as _ctl
from levain.agents import session_client as C
from levain.agents.accounting import ActionExposure, StopResult
from levain.agents.session_launcher import LauncherRefused, RealLauncher, SessionIndex
from levain.slot import Refused
from levain.slot.layout import RUN, STORE

NAME = "dispatch_run"
ACCOUNT = "_levain_dispatch"
EX_USAGE = 64
EX_CONFIG = 78
EX_WRONG_USER = 77
#: [judged] a holder connection that sends nothing for this long is read as gone (EOF): the session is ENDed.
HOLDER_IDLE_S = 1800.0
#: [judged] one WAIT is held at most this long whatever the client asked.
WAIT_MAX_S = 30.0
MAX_CONNECTIONS = 64
#: finished END jobs remembered per key (a proven one answers a repeat END without a second teardown)
_JOBS_KEEP = 512


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

def _end_child(child: Any) -> None:
    """One END attempt on a launched or held child, never raising."""
    try:
        (getattr(child, "end_once", None) or child.try_end)()
    except Exception:  # noqa: BLE001
        _log.warning("a holder's END raised")


_log = logging.getLogger("levain.slot.services.dispatch_run")


class _Holder:
    """The launched child one holder connection owns, and whether its ABORT already ran."""

    def __init__(self, child: Any) -> None:
        self.child, self.aborted = child, False


class _EndJob:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = StopResult(False, "END in progress")


class RunServer:
    """The protocol and the per-key END jobs; the socket loop is :func:`serve`. ``verify(proof, exposure)`` raises
    :class:`LauncherRefused` (production: :func:`verify_point_of_use` bound to this server's readers);
    ``pool_free()`` is whether a free egress slot exists; ``peer_ok(sock)`` is the peer-uid check."""

    def __init__(self, launcher: RealLauncher, *, verify: Callable[[C.LinkProof, Optional[ActionExposure]], None],
                 pool_free: Callable[[], bool], peer_ok: Callable[[socket.socket], bool],
                 holder_idle_s: float = HOLDER_IDLE_S) -> None:
        self.launcher, self._verify, self._pool_free, self._peer_ok = launcher, verify, pool_free, peer_ok
        self._idle = holder_idle_s
        self._mu = threading.Lock()
        self._jobs: dict[str, _EndJob] = {}
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    # -- connection ------------------------------------------------------------------------------------------------------

    def handle(self, sock: socket.socket) -> None:
        """Serve one connection to its end. Never raises."""
        if not self._slots.acquire(blocking=False):
            sock.close()
            return
        mine = None
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
                try:
                    if not mine.aborted and not getattr(mine.child, "released", False):
                        mine.child.abort()
                    _end_child(mine.child)
                except Exception:  # noqa: BLE001
                    _log.warning("a holder's cleanup raised")
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
            launch, proof = C.launch_from_wire(req)
            self._verify(proof, launch.exposure)
            self._pool()
            child = self.launcher.spawn(launch)
            return C.ok_answer(rid, session=child.session), _Holder(child)
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
            self._fields(req)
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

    def _pool(self) -> None:
        try:
            free = self._pool_free()
        except Exception:  # noqa: BLE001
            raise LauncherRefused("control-down", "CONTROL's pool cannot be read", reason_class="transient") from None
        if not free:
            raise LauncherRefused("no-free-slot", "no dispatch slot is free", reason_class="transient")

    def _end(self, key: str, deadline_s: float) -> StopResult:
        """T7: one in-flight teardown per key, on a worker; answer within ``deadline_s`` or say it is in progress. A
        finished PROVEN job is the answer to a repeat END; a finished unproven one may be retried (never concurrently)."""
        with self._mu:
            job = self._jobs.get(key)
            if job is None or (job.done.is_set() and job.result.terminated is not True):
                job = self._jobs[key] = _EndJob()
                threading.Thread(target=self._run_end, args=(key, job), daemon=True).start()
            if len(self._jobs) > _JOBS_KEEP:
                for k in [k for k, j in self._jobs.items() if j.done.is_set() and k != key][:_JOBS_KEEP // 4]:
                    del self._jobs[k]
        job.done.wait(max(0.0, deadline_s))
        return job.result

    def _run_end(self, key: str, job: _EndJob) -> None:
        try:
            got = self.launcher.stop(key, 0.0)
        except Exception:  # noqa: BLE001
            got = StopResult(False, "END could not complete; nothing assumed ended")
        job.result = got
        job.done.set()


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
    ap.add_argument("--agent-store-dir", default=None, help="the operator's agent store dir (default <home>/.levain/autonomic)")
    ap.add_argument("--cockpit-root", default=None, help="default: the agent store's session_install.json")
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
        store_dir = Path(a.agent_store_dir) if a.agent_store_dir else Path(pwd.getpwuid(op_uid).pw_dir) / ".levain" / "autonomic"
        cockpit = Path(a.cockpit_root) if a.cockpit_root else _cockpit_root(store_dir)
        server = build_server(host, Path(a.store_dir), Path(a.control), op_uid, store_dir, cockpit)
        listener = _bind(Path(a.socket))
    except (Refused, SystemExit, KeyError, OSError, ValueError) as exc:
        print(f"levain dispatch-run: refused: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EX_CONFIG
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        serve(server, listener, stop)
    finally:
        listener.close()
        close = getattr(server.launcher.runtime, "close", None)
        if close is not None:
            close()
    return 0


def _cockpit_root(store_dir: Path) -> Path:
    from levain.agents.session_wire import read_install
    return Path(read_install(store_dir).cockpit_root)


def build_server(host: str, store: Path, control_sock: Path, operator_uid: int, agent_store_dir: Path,
                 cockpit_root: Path) -> RunServer:
    """The production wiring: the real ports behind :class:`RealLauncher`."""
    from levain.agent_authority import AgentAuthority
    from levain.autonomic.db import DB_NAME
    from levain.qbroker.peercred import peer_uid
    from levain.slot.dispatch_runtime import DispatchControlPort, HelperSlotRuntime
    index_dir = store / "dispatch" / "sessions"
    authority = AgentAuthority(cockpit_root)
    bar_of = agent_store_bar(agent_store_dir / DB_NAME)
    launcher = RealLauncher(DispatchControlPort(control_sock),
                            HelperSlotRuntime(host=host, control_socket=control_sock, store=store),
                            bound_usage_sources(queue_root=store / "queue", store=store, index_dir=index_dir),
                            SessionIndex(index_dir))
    pool = DispatchControlPort(control_sock, ops=frozenset({"POOL"}))

    def verify(proof: C.LinkProof, exposure: Optional[ActionExposure]) -> None:
        verify_point_of_use(proof, exposure, replay=authority.replay, now=lambda: datetime.now(timezone.utc),
                            bar_of=bar_of)

    def pool_free() -> bool:
        ans = pool.call("POOL")
        return type(ans.get("egress_free")) is int and ans["egress_free"] > 0

    return RunServer(launcher, verify=verify, pool_free=pool_free, peer_ok=lambda s: peer_uid(s) == operator_uid)


def service_row():
    """The unit row this program runs under: ``Service("dispatch_run", ...)`` as ``_levain_dispatch``, always restarted,
    after CONTROL and gw-keys. NOT yet in ``levain.slot.units.SERVICES``: adding it changes CONTRACT section 1.3 (the
    service table), section 2.2 (a run dir for ``run.sock``, root-made, ``_levain_dispatch``-owned, others traverse)
    and the pinned tables in four test files, which is the head's call (RETURN, open questions). A golden for the plist
    this row renders is in ``tests/test_slot_dispatch_run_server.py``."""
    from levain.slot.units import Service
    return Service(NAME, "dispatch run server", ACCOUNT, ("control", "keys"), "always")


__all__ = ["ACCOUNT", "NAME", "RunServer", "service_row", "agent_store_bar", "bound_usage_sources", "build_server", "main", "serve",
           "verify_point_of_use"]


if __name__ == "__main__":
    sys.exit(main())
