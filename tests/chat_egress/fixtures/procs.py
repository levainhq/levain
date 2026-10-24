"""Process and socket plumbing for the chat-egress fixtures (S1 of CH-D2).

Everything here is process/socket level: a child process in its OWN session (so one killpg reaps the whole tree, the
decider's node child included), a READY line it prints, and a stop that is verified. No product API is assumed.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


def free_tcp_ports(n: int) -> list[int]:
    """n distinct free loopback TCP ports (held open together so they are distinct, then released)."""
    socks = []
    try:
        for _ in range(n):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def tcp_listening(port: int) -> bool:
    """True if something accepts a TCP connection on 127.0.0.1:port."""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def udp_bound(port: int) -> bool:
    """True if 127.0.0.1:port (UDP) cannot be bound, i.e. something holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie that nobody waited on still answers kill(0); ps knows
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def lsof_listeners(tcp_ports: list[int], udp_ports: list[int]) -> list[str] | None:
    """Lines of `lsof` naming a listener on any of these ports, or None when lsof is not installed."""
    out: list[str] = []
    try:
        for p in tcp_ports:
            r = subprocess.run(["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=20)
            out += [ln for ln in r.stdout.splitlines()[1:] if ln.strip()]
        for p in udp_ports:
            r = subprocess.run(["lsof", "-nP", f"-iUDP:{p}"], capture_output=True, text=True, timeout=20)
            out += [ln for ln in r.stdout.splitlines()[1:] if ln.strip()]
    except FileNotFoundError:
        return None
    return out


class Managed:
    """A child process in its own session. start() waits for a READY line in its stdout file; stop() kills the group."""

    def __init__(self, argv: list[str], *, workdir: Path, name: str, ready: str, env: dict[str, str] | None = None,
                 ready_timeout: float = 30.0) -> None:
        self.argv, self.workdir, self.name, self.ready = argv, Path(workdir), name, ready
        self.env, self.ready_timeout = env, ready_timeout
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None
        self.ready_line = ""
        self.stdout_path = self.workdir / f"{name}.stdout"
        self.stderr_path = self.workdir / f"{name}.stderr"

    def start(self) -> "Managed":
        self.workdir.mkdir(parents=True, exist_ok=True)
        out, err = self.stdout_path.open("wb"), self.stderr_path.open("wb")
        env = dict(os.environ) if self.env is None else self.env
        self.proc = subprocess.Popen(self.argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                     start_new_session=True, cwd=str(self.workdir))
        self.pgid = self.proc.pid
        out.close()
        err.close()
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            text = self.stdout_path.read_text(errors="replace")
            for line in text.splitlines():
                if line.startswith(self.ready):
                    self.ready_line = line
                    return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        tail = self.stderr_path.read_text(errors="replace")[-800:]
        self.stop()
        raise RuntimeError(f"{self.name}: no {self.ready!r} line (rc={self.proc.returncode if self.proc else None}); stderr tail: {tail}")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.pgid is not None:
                try:
                    os.killpg(self.pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.pgid, signal.SIGKILL)  # type: ignore[arg-type]
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=8)
            # the group's other members (the decider's node) exit on the closed lifeline or the SIGTERM; give them a beat
            # then make sure by SIGKILL
            end = time.time() + 3
            while time.time() < end and self._group_alive():
                time.sleep(0.05)
            if self._group_alive():
                try:
                    os.killpg(self.pgid, signal.SIGKILL)  # type: ignore[arg-type]
                except ProcessLookupError:
                    pass
        finally:
            self.proc = None

    def _group_alive(self) -> bool:
        if self.pgid is None:
            return False
        r = subprocess.run(["ps", "-o", "pid=,stat=", "-g", str(self.pgid)], capture_output=True, text=True)
        return any(ln.split() and not ln.split()[1].startswith("Z") for ln in r.stdout.splitlines() if len(ln.split()) >= 2)

    def group_pids(self) -> list[int]:
        if self.pgid is None:
            return []
        r = subprocess.run(["ps", "-o", "pid=,stat=", "-g", str(self.pgid)], capture_output=True, text=True)
        return [int(ln.split()[0]) for ln in r.stdout.splitlines() if len(ln.split()) >= 2 and not ln.split()[1].startswith("Z")]

    def __enter__(self) -> "Managed":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class Jsonl:
    """A JSONL log another process appends to. mark() = the line count now; since(mark) = parsed lines after it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def lines(self) -> list[dict]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        out = []
        for ln in text.splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass  # a line mid-write; the next read has it
        return out

    def mark(self) -> int:
        return len(self.lines())

    def since(self, mark: int) -> list[dict]:
        return self.lines()[mark:]
