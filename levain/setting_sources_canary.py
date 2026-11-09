"""The ``--setting-sources ""`` tripwire's probe. The empty source list is undocumented in Claude Code's own help, so
what it does is a measured fact about one version. This probe re-measures it with no model call and nothing off the loopback: a
scratch project plants a SessionStart hook that touches a marker, ``claude -p`` runs once WITHOUT the flag (the control:
the hook must fire) and once WITH ``--setting-sources ""`` (the hook must not), each with a fake key, its own scratch HOME
and config dir (never the operator's), and its own local listener as the API address.

The evidence is positive, never an absence. The listener never answers; it reads each connection's request line. A
connection alone proves nothing: the client sends a preflight before it runs any hook. The anchor is the first model
request, a ``POST`` to ``/v1/messages``, which the client sends only after its SessionStart hooks. The control validates
that order on this binary: its marker must already exist when its model request arrives. Only then does the empty-list
run's model request without a marker read ``held``. A rejected flag, a crash, a run that never sends its model request,
or a timeout is ``inconclusive``; the empty-list marker appearing at any moment is ``leaked``. Each run is killed as a
whole process group."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from levain.host_guard import SEAT_SOURCE_ARGS

__all__ = ["ProbeResult", "run_probe"]

_STDERR_CAP = 300
_LINE_CAP = 4096
_MODEL_PATH = "/v1/messages"


def _is_model_request(head: bytes) -> bool:
    """True when ``head`` holds a complete request line that POSTs to the model endpoint (any query string)."""
    end = head.find(b"\r\n")
    if end < 0:
        return False
    parts = head[:end].decode("latin-1").split(" ")
    if len(parts) != 3 or parts[0] != "POST":
        return False
    return parts[1].split("?", 1)[0] == _MODEL_PATH


@dataclass(frozen=True)
class ProbeResult:
    verdict: str  # "held" | "leaked" | "inconclusive"
    detail: str


class _Run:
    """One ``claude -p`` run: its project, marker, listener, stderr file and process group."""

    def __init__(self, root: Path, name: str, binary: str, extra: tuple[str, ...]) -> None:
        self.name = name
        self.proj = root / name
        (self.proj / ".claude").mkdir(parents=True)
        self.marker = root / f"{name}.marker"
        hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                       "command": f"touch {shlex.quote(str(self.marker))}"}]}]}}
        (self.proj / ".claude" / "settings.json").write_text(json.dumps(hook))
        home = root / f"{name}-home"
        (home / "cfg").mkdir(parents=True)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.setblocking(False)
        self.conns: list[socket.socket] = []
        self.heads: list[bytes] = []
        self.sent_model_request = False
        self.err_path = root / f"{name}.stderr"
        port = self.listener.getsockname()[1]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "cfg"),
               "ANTHROPIC_API_KEY": "sk-ant-levain-probe-not-a-key", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
               "DISABLE_AUTOUPDATER": "1"}
        try:
            with open(self.err_path, "wb") as err:
                self.proc = subprocess.Popen([binary, "-p", "levain probe", *extra], cwd=self.proj, env=env,
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                             start_new_session=True)
        except BaseException:
            self.listener.close()
            raise

    def reached_model(self) -> bool:
        """Accept new connections, read what each has sent so far, and report whether any of them carries the model
        request. Never answers, so the client cannot reuse a connection for a second request."""
        if self.sent_model_request:
            return True
        while True:
            try:
                conn, _addr = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                break
            conn.setblocking(False)
            self.conns.append(conn)
            self.heads.append(b"")
        for i, conn in enumerate(self.conns):
            if len(self.heads[i]) >= _LINE_CAP:
                continue
            try:
                data = conn.recv(_LINE_CAP - len(self.heads[i]))
            except OSError:
                continue
            self.heads[i] += data
            if _is_model_request(self.heads[i]):
                self.sent_model_request = True
        return self.sent_model_request

    def exited(self) -> int | None:
        return self.proc.poll()

    def stderr_tail(self) -> str:
        try:
            raw = self.err_path.read_bytes()[:_STDERR_CAP]
        except OSError:
            return ""
        return raw.decode("utf-8", "replace").strip()

    def why_exited(self) -> str:
        err = self.stderr_tail()
        return f"exited {self.proc.returncode}" + (f": {err}" if err else " with no stderr")

    def stop(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        for s in (*self.conns, self.listener):
            try:
                s.close()
            except OSError:
                pass


def run_probe(binary: str, *, timeout: float = 20.0) -> ProbeResult:
    """``held``: the control fired its hook before sending its model request, and the empty-list run sent its model
    request with no hook. ``leaked``: the empty-list run's hook fired, at any moment. ``inconclusive``: anything else, including a
    rejected flag, a crash, an early control exit and the overall ``timeout``."""
    root: Path | None = None
    runs: list[_Run] = []
    try:
        root = Path(tempfile.mkdtemp(prefix="levain-ssprobe-"))
        ctl = _Run(root, "ctl", binary, ())
        runs.append(ctl)
        empty = _Run(root, "empty", binary, SEAT_SOURCE_ARGS)
        runs.append(empty)
        t0 = time.monotonic()
        ctl_ok = False
        while True:
            if empty.marker.exists():
                return ProbeResult("leaked", '--setting-sources "" still loaded the project settings (its hook fired)')
            if not ctl_ok and ctl.reached_model():
                if not ctl.marker.exists():
                    return ProbeResult("inconclusive", "the control sent its model request before its project hook "
                                       "fired, so the order this probe relies on does not hold on this version")
                ctl_ok = True
            if not ctl_ok and ctl.exited() is not None:
                return ProbeResult("inconclusive", f"the control run {ctl.why_exited()}"[:400])
            if empty.reached_model():
                if ctl_ok:
                    empty.stop()
                    if empty.marker.exists():
                        return ProbeResult("leaked", '--setting-sources "" still loaded the project settings (its hook '
                                           "fired)")
                    return ProbeResult("held", f"the control fired its project hook before its POST {_MODEL_PATH}; "
                                       f"the empty-list run sent its POST {_MODEL_PATH} with no hook, in "
                                       f"{time.monotonic() - t0:.1f}s")
            elif empty.exited() is not None:
                empty.stop()
                if empty.marker.exists():
                    return ProbeResult("leaked", '--setting-sources "" still loaded the project settings (its hook '
                                       "fired)")
                return ProbeResult("inconclusive", (f'the --setting-sources "" run {empty.why_exited()} before its '
                                                    "model request")[:400])
            if time.monotonic() - t0 > timeout:
                empty.stop()
                if empty.marker.exists():
                    return ProbeResult("leaked", '--setting-sources "" still loaded the project settings (its hook '
                                       "fired)")
                which = "the empty-list run never sent its model request" if ctl_ok else \
                    "the control run never fired its project hook and sent its model request"
                return ProbeResult("inconclusive", f"{which} within {timeout:.0f}s, so the probe proves nothing")
            time.sleep(0.05)
    except OSError as exc:
        return ProbeResult("inconclusive", f"the probe could not run: {exc}"[:200])
    finally:
        for r in runs:
            r.stop()
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
