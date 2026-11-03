"""``python -m levain.qbroker.fence_server``: the agent fence, run by the OPERATOR's uid for the queue broker (P7-2
wiring, shape C: ``project_memory/1102-7/P72_WIRING_DESIGN.md``).

The operator owns every store the fence reads (``autonomic.db`` and the cockpit's authority log and anchor), and no
other uid can read them. So this process, run as the operator, holds one :class:`levain.qbroker.fence.AgentFences`
(unchanged) and answers questions about one session at a time on ``R/fence/fence.sock``:

- ``FENCE {session}``, only from the uid of ``_levain_qbroker``: ``{"current": true}``, ``{"fenced": <reason>}``, or
  the refusal ``fence-unavailable`` with a ``why`` (the broker answers GATE ``limits-failed`` with it). ``session`` must
  be an S1 session id (``s-`` and 16 lower-case hex). A session no agent row names is ``current``, as AgentFences
  answers it.
- ``STATUS``, only from the operator's own uid (``levain doctor``): the store and cockpit root it reads, the authority
  log's head seq, and what it could not read.

Any other peer uid is closed unread and unanswered. A request is one frame of at most
:data:`levain.qbroker.fence.REQUEST_MAX` bytes, whole within :data:`REQUEST_READ_S`; one request per connection.

``R/fence/`` is the operator's, group ``_levain_qbroker``, mode 0750 (setgid on Linux): the server refuses to start
unless it finds that shape, and refuses to serve unless the socket it binds is in that group (mode 0660), so no slot
uid can reach the socket and the broker's peer check refuses any other process bound there. Never runs as root.
"""
from __future__ import annotations

import argparse
import errno
import grp
import logging
import os
import pwd
import signal
import socket
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain import authority as _auth
from levain.agent_authority import AgentAuthority
from levain.autonomic.db import DB_NAME as AGENT_DB_NAME
from levain.cockpit.firejournal import DB_NAME as COCKPIT_DB_NAME
from levain.qbroker import wire
from levain.qbroker.broker import FenceUnavailable, Resolved
from levain.qbroker.fence import (FENCE_DB_TIMEOUT, REQUEST_MAX, SESSION_RE, TEXT_MAX, AgentFences,
                                  fence_socket_path)
from levain.qbroker.peercred import peer_uid as _peer_uid

__all__ = ["BROKER_ACCOUNT", "EX_CONFIG", "EX_NOPERM", "MAX_CONNECTIONS", "REQUEST_READ_S", "FenceServer",
           "dir_problem", "main"]

log = logging.getLogger("levain.qbroker.fence_server")

BROKER_ACCOUNT = "_levain_qbroker"
# the broker asks once per dispatch GATE, from a thread per hook connection; one past this is closed at accept (the
# broker reads the close as "cannot decide", limits-failed)
MAX_CONNECTIONS = 16
# a request must arrive whole this long after the connection is served (the broker sends it right after connect)
REQUEST_READ_S = 2.0
# how long an answer may wait on a peer that does not read it
ANSWER_WRITE_S = 2.0
# after an accept() error that is not the listener's own close, the pause before the next
ACCEPT_BACKOFF = 0.1
EX_NOPERM = 77          # run as root, or a socket dir of the wrong shape
EX_CONFIG = 78          # no broker account
_DIR_MODES = (0o750, 0o2750)
_JSON_INT_MAX = 2 ** 53 - 1
_DEFAULT_RUN = "/private/var/run/levain" if sys.platform == "darwin" else "/run/levain"


def _text(s: str) -> str:
    """``s`` as the wire's bounded printable text (:data:`levain.qbroker.fence.TEXT_RE`)."""
    out = "".join(c if " " <= c <= "~" else "?" for c in s)[:TEXT_MAX]
    return out or "an unnamed reason"


def dir_problem(st: os.stat_result, *, owner: int, gid: int) -> str | None:
    """Why ``R/fence/`` (lstat'd) is not the operator's 0750 directory in the broker's group, or None."""
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != owner:
        return f"is owned by uid {st.st_uid}, not the operator's {owner}"
    if st.st_gid != gid:
        return f"is in group {st.st_gid}, not {BROKER_ACCOUNT}'s {gid}"
    if stat.S_IMODE(st.st_mode) not in _DIR_MODES:
        return f"has mode {stat.S_IMODE(st.st_mode):04o}, not 0750"
    return None


class FenceServer:
    """``fences`` answers FENCE (an :class:`AgentFences`); ``path`` is the socket; ``broker_uid`` may ask FENCE and
    ``operator_uid`` (this process's) may ask STATUS; the socket must come out of ``bind`` in group ``expected_gid``.
    ``store`` and ``cockpit_root`` are what STATUS names and probes."""

    def __init__(self, fences: Callable[[Resolved], "str | None"], path: Path | str, *, broker_uid: int,
                 operator_uid: int, expected_gid: int, store: Path | str, cockpit_root: Path | str,
                 peer_uid: Callable[[socket.socket], int] = _peer_uid, max_connections: int = MAX_CONNECTIONS,
                 read_timeout: float = REQUEST_READ_S) -> None:
        if broker_uid == operator_uid:
            raise ValueError("the broker's uid and the operator's are one uid: FENCE and STATUS cannot be told apart")
        self.fences, self.path = fences, Path(path)
        self.broker_uid, self.operator_uid, self.expected_gid = broker_uid, operator_uid, expected_gid
        self.store, self.cockpit_root = Path(store), Path(cockpit_root)
        self._peer_uid, self._read_timeout = peer_uid, read_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._conns: set = set()
        self._conns_lock = threading.Lock()
        self._stopped = False
        self.refused_peers = 0
        self.last_error: str | None = None

    # --- the listener -------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Refuses (:class:`PermissionError`) unless the socket's directory has :func:`dir_problem`'s shape, owned by
        this process's uid (the operator's, as :func:`main` runs it), and the bound socket is in ``expected_gid``; a
        stale socket at ``path`` is replaced, anything else there refuses."""
        st = os.lstat(self.path.parent)
        why = dir_problem(st, owner=os.geteuid(), gid=self.expected_gid)
        if why is not None:
            raise PermissionError(f"{self.path.parent} {why}: refusing to serve")
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise FileExistsError(f"{self.path} exists and is not a socket")
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            sock.bind(str(self.path))
            bound = True
            os.chmod(self.path, 0o660)
            st = os.lstat(self.path)
            if not stat.S_ISSOCK(st.st_mode) or st.st_gid != self.expected_gid:
                raise PermissionError(f"{self.path} is in group {st.st_gid}, not {BROKER_ACCOUNT}'s "
                                      f"{self.expected_gid}: refusing to serve")
            sock.listen(MAX_CONNECTIONS)
        except BaseException:
            sock.close()
            if bound:
                self._unlink()
            raise
        self._sock = sock
        thread = threading.Thread(target=self._accept_loop, name="fence-accept", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._sock = None
            sock.close()
            self._unlink()
            raise
        self._thread = thread

    def _unlink(self) -> None:
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    def stop(self) -> None:
        with self._conns_lock:
            self._stopped = True
            conns, self._conns = list(self._conns), set()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            self._unlink()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while True:
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except OSError as exc:
                if self._sock is None or exc.errno == errno.EBADF:
                    return
                log.warning("fence: accept failed: %s", exc)
                time.sleep(ACCEPT_BACKOFF)
                continue
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            with self._conns_lock:
                if self._stopped:
                    self._slots.release()
                    conn.close()
                    return
                self._conns.add(conn)
            try:
                threading.Thread(target=self._serve, args=(conn,), name="fence-conn", daemon=True).start()
            except BaseException:
                self._forget(conn)
                self._slots.release()
                conn.close()

    def _forget(self, conn: socket.socket) -> None:
        with self._conns_lock:
            self._conns.discard(conn)

    def _serve(self, conn: socket.socket) -> None:
        try:
            try:
                uid: int | None = self._peer_uid(conn)
            except OSError:
                uid = None
            role = "broker" if uid == self.broker_uid else "operator" if uid == self.operator_uid else None
            if role is None:
                self.refused_peers += 1
                log.warning("fence: refused a peer of uid %s", uid)
                return
            try:
                message = wire.read_frame(conn, timeout=self._read_timeout, max_frame=REQUEST_MAX)
            except wire.FrameError as exc:
                conn.settimeout(ANSWER_WRITE_S)
                wire.write_frame(conn, wire.refusal(None, exc.name))
                return
            if message is None or self._stopped:
                return
            answer = self.handle(role, message)
            conn.settimeout(ANSWER_WRITE_S)
            wire.write_frame(conn, answer)
        except OSError:         # a peer gone, or a request that never came (TimeoutError is one)
            return
        finally:
            self._forget(conn)
            conn.close()
            self._slots.release()

    # --- the verbs ----------------------------------------------------------------------------------------------

    def handle(self, role: str, message: Any) -> dict:
        """One request from a peer whose role (``broker`` or ``operator``) the kernel's uid gave."""
        rid = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message, dict) or type(message.get("v")) is not int or message["v"] != wire.VERSION \
                or type(rid) is not int or not 0 <= rid <= _JSON_INT_MAX or type(message.get("op")) is not str:
            return wire.refusal(None, "bad-request")
        op = message["op"]
        if (role, op) == ("broker", "FENCE"):
            if set(message) != {"v", "op", "id", "session"}:
                return wire.refusal(rid, "bad-request")
            session = message["session"]
            if type(session) is not str or SESSION_RE.fullmatch(session) is None:
                return wire.refusal(rid, "bad-session")
            return self._fence(rid, session)
        if (role, op) == ("operator", "STATUS"):
            if set(message) != {"v", "op", "id"}:
                return wire.refusal(rid, "bad-request")
            return wire.answer(rid, **self.status())
        return wire.refusal(rid, "not-permitted")

    def _fence(self, rid: int, session: str) -> dict:
        try:
            # AgentFences reads only the session and whether there is a run; the broker asks only about runs
            reason = self.fences(Resolved(session=session, kind="dispatch", run={}))
        except FenceUnavailable as exc:
            return self._unavailable(rid, str(exc))
        except Exception as exc:  # noqa: BLE001 - a fence that cannot be read decides nothing
            return self._unavailable(rid, f"the agent fence could not be read ({type(exc).__name__})")
        if reason is None:
            return wire.answer(rid, current=True)
        if type(reason) is not str or not reason:
            return self._unavailable(rid, "the agent fence answered neither current nor a reason")
        return wire.answer(rid, fenced=_text(reason))

    def _unavailable(self, rid: int, why: str) -> dict:
        self.last_error = _text(why)
        log.warning("fence: cannot decide: %s", self.last_error)
        return {**wire.refusal(rid, "fence-unavailable"), "why": self.last_error}

    def status(self) -> dict:
        seq, authority_error = self._probe_authority()
        return {"store": str(self.store), "cockpit_root": str(self.cockpit_root), "seq": seq,
                "store_error": self._probe_store(), "authority_error": authority_error,
                "last_error": self.last_error}

    def _probe_store(self) -> str | None:
        try:
            conn = sqlite3.connect(f"file:{quote(str(self.store / AGENT_DB_NAME))}?mode=ro", uri=True,
                                   isolation_level=None, timeout=FENCE_DB_TIMEOUT)
            try:
                conn.execute("SELECT 1 FROM agent_occurrences LIMIT 1").fetchall()
                if conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone() is None:
                    return "it holds no store id"
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - STATUS reports, never raises
            return _text(f"{type(exc).__name__}: {exc}")
        return None

    def _probe_authority(self) -> tuple[int | None, str | None]:
        try:
            if _auth.read_anchor(_auth.anchor_path(self.cockpit_root)) is None:
                return None, "no authority anchor"
            conn = sqlite3.connect(f"file:{quote(str(self.cockpit_root / COCKPIT_DB_NAME))}?mode=ro", uri=True,
                                   isolation_level=None, timeout=FENCE_DB_TIMEOUT)
            try:
                head = _auth.log_head(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - STATUS reports, never raises
            return None, _text(f"{type(exc).__name__}: {exc}")
        if head is None:
            return None, "the authority log is empty"
        return int(head[0]), None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m levain.qbroker.fence_server")
    ap.add_argument("--agent-store", required=True, help="the operator's autonomic store directory (its autonomic.db)")
    ap.add_argument("--cockpit-root", required=True, help="the operator's cockpit directory (the authority log)")
    ap.add_argument("--run", default=_DEFAULT_RUN, help="R, the run directory (the socket is R/fence/fence.sock)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    me = os.geteuid()
    if me == 0:
        print("levain fence: must run as the operator, never root (the operator's user agent runs it)",
              file=sys.stderr)
        return EX_NOPERM
    try:
        broker_uid, broker_gid = pwd.getpwnam(BROKER_ACCOUNT).pw_uid, grp.getgrnam(BROKER_ACCOUNT).gr_gid
    except KeyError:
        print(f"levain fence: no {BROKER_ACCOUNT} account or group (the isolation install makes them)",
              file=sys.stderr)
        return EX_CONFIG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store, root = Path(args.agent_store), Path(args.cockpit_root)
    server = FenceServer(AgentFences(store / AGENT_DB_NAME, AgentAuthority(root)), fence_socket_path(args.run),
                         broker_uid=broker_uid, operator_uid=me, expected_gid=broker_gid, store=store,
                         cockpit_root=root)
    try:
        server.start()
    except (PermissionError, FileExistsError, FileNotFoundError) as exc:
        print(f"levain fence: {exc}", file=sys.stderr)
        return EX_NOPERM
    log.info("serving %s for uid %d (operator uid %d)", server.path, broker_uid, me)
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
