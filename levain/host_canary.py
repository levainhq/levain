"""The per-session canary for a hosted Claude Code primary (chat S2).

`levain.host_guard` builds the hosted profile; this module proves by behaviour that the profile
holds on THIS machine, with THIS claude binary, before the first hosted turn. It exists because
configuration is not proof: on CC 2.1.292 a sandbox block with one wrongly-typed value was
dropped whole, with no error and no warning (the 10-06 probes, 1f).

One canary turn spawns the session's own argv (plus no persistence and a cheap model), asks for
exactly one Bash call and four Write calls, answers every permission prompt itself, and watches
a listener it owns. Every check must pass. A model that refuses, or calls something else, makes
the verdict inconclusive, and inconclusive refuses: the canary fails closed. The plan, with
the reason for each check: flow projects/levain/reference/chat_s2_plan_1007.md, section 2.2.

It never builds a sandbox. Every attempt below is an ordinary connect or write that the
harness's own sandbox must refuse.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from levain.host_guard import (CC_HOSTED_TOOLS, CC_VERSION_FLOOR, CCHostProfile, HostRefusal,
                               parse_cc_version)

__all__ = ["Check", "CanaryVerdict", "run_cc_canary", "check_init_event", "probe_command"]

Status = Literal["pass", "fail", "inconclusive"]

# Claude Code's built-in plugins report this path in the init event; anything else is a plugin
# loaded from somewhere, which --restricted should never do.
_BUILTIN_PLUGIN_PATH = "builtin"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    evidence: str


@dataclass(frozen=True)
class CanaryVerdict:
    ok: bool
    checks: tuple[Check, ...]
    claude_version: str | None
    claude_bin: str
    profile_digest: str

    def reason(self) -> str:
        bad = [c for c in self.checks if c.status != "pass"]
        return "; ".join(f"{c.name} {c.status}: {c.evidence}" for c in bad) or "all checks passed"


# ---------------------------------------------------------------------------- init event


def check_init_event(init: dict[str, Any], *, expected_tools: tuple[str, ...] = CC_HOSTED_TOOLS,
                     expected_mcp: frozenset[str] = frozenset({"anneal_memory"})) -> Check:
    """C1, run on the canary's init event AND on every hosted turn's init event (S3 calls it per
    turn; it costs nothing): the tool set, the MCP set, plugins, the permission mode and the CC
    version are exactly what the profile asked for."""
    problems = []
    tools = init.get("tools")
    if not isinstance(tools, list):
        problems.append("tools missing from the init event")
    else:
        builtins = sorted(t for t in tools if not str(t).startswith("mcp__"))
        if builtins != sorted(expected_tools):
            problems.append(f"built-in tools {builtins!r} != {sorted(expected_tools)!r}")
        foreign = [t for t in tools if str(t).startswith("mcp__")
                   and not any(str(t).startswith(f"mcp__{n}__") for n in expected_mcp)]
        if foreign:
            problems.append(f"MCP tools from unexpected servers: {foreign!r}")
    servers = init.get("mcp_servers")
    server_list: list[Any] = servers if isinstance(servers, list) else []
    names = {s.get("name") for s in server_list} if isinstance(servers, list) else None
    if names != set(expected_mcp):
        problems.append(f"mcp_servers {names!r} != {sorted(expected_mcp)!r}")
    elif any(s.get("status") != "connected" for s in server_list):
        problems.append(f"an MCP server is not connected: {servers!r}")
    plugins = init.get("plugins")
    if not isinstance(plugins, list):
        problems.append("plugins missing from the init event")
    else:
        foreign = [p.get("name") for p in plugins if p.get("path") != _BUILTIN_PLUGIN_PATH]
        if foreign:
            problems.append(f"non-builtin plugins loaded: {foreign!r}")
    if init.get("permissionMode") != "default":
        problems.append(f"permissionMode {init.get('permissionMode')!r} != 'default'")
    version = parse_cc_version(str(init.get("claude_code_version", "")))
    if version is None or version < CC_VERSION_FLOOR:
        problems.append(f"claude_code_version {init.get('claude_code_version')!r} is below "
                        f"{'.'.join(map(str, CC_VERSION_FLOOR))}")
    return Check("C1 profile loaded", "fail" if problems else "pass",
                 "; ".join(problems) or f"tools, MCP, plugins, mode and version match "
                                         f"({init.get('claude_code_version')})")


# ---------------------------------------------------------------------------- the probe


_PROBE = """\
import errno, socket
N = {nonce!r}
def cls(e):
    n = getattr(e, "errno", None)
    return "BLOCKED" if n in (errno.EPERM, errno.EACCES) else "INCONCLUSIVE errno=%s" % n
def tcp():
    s = socket.create_connection(("127.0.0.1", {port}), timeout=3); s.close()
def uds():
    s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect({sock!r}); s.close()
def fsout():
    open({outside!r}, "x").close()
def fshook():
    open({hook!r}, "x").close()
for name, fn, ok in (("TCP", tcp, "CONNECTED"), ("UDS", uds, "CONNECTED"),
                     ("FSOUT", fsout, "ALLOWED"), ("FSHOOK", fshook, "ALLOWED")):
    try:
        fn(); print(N, name, ok)
    except OSError as e:
        print(N, name, cls(e))
"""


def probe_script(*, nonce: str, port: int, sock: str, outside: str, hook: str) -> str:
    return _PROBE.format(nonce=nonce, port=port, sock=sock, outside=outside, hook=hook)


def probe_command(script: str | os.PathLike[str], python: str = "/usr/bin/python3") -> str:
    """The exact Bash command the canary asks for. The tool_use input must equal it byte for byte,
    so a model talked into printing the expected text instead of running this is caught. Every
    attempt is classified by errno: BLOCKED only on EPERM/EACCES (what the sandbox raised in the
    10-06 probes); a refused connection or a missing directory is INCONCLUSIVE, never a pass."""
    return f"{shlex.quote(python)} {shlex.quote(os.fspath(script))}"


# ---------------------------------------------------------------------------- the listener


class _Listener:
    """A TCP and an AF_UNIX listener that count connections server-side. The canary connects to
    each itself before and after the turn: zero foreign hits then means something, because the
    listener is shown to have been able to see one."""

    def __init__(self, sock_path: Path) -> None:
        self.hits = 0
        self.self_hits = 0
        self._lock = threading.Lock()
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(8)
        self.port = self.tcp.getsockname()[1]
        self.sock_path = sock_path
        self.uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.uds.bind(str(sock_path))
        self.uds.listen(8)
        self._stop = False
        self._threads = [threading.Thread(target=self._accept, args=(s,), daemon=True)
                         for s in (self.tcp, self.uds)]
        for t in self._threads:
            t.start()

    def _accept(self, s: socket.socket) -> None:
        s.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = s.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self.hits += 1
            conn.close()

    def self_connect(self) -> bool:
        ok = True
        for fam, addr in ((socket.AF_INET, ("127.0.0.1", self.port)), (socket.AF_UNIX, str(self.sock_path))):
            before = self.hits
            try:
                c = socket.socket(fam, socket.SOCK_STREAM)
                c.settimeout(2)
                c.connect(addr)
                c.close()
            except OSError:
                ok = False
                continue
            deadline = time.monotonic() + 2
            while self.hits == before and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.hits == before:
                ok = False
            else:
                with self._lock:
                    self.self_hits += 1
        return ok

    def foreign_hits(self) -> int:
        return self.hits - self.self_hits

    def close(self) -> None:
        self._stop = True
        for t in self._threads:
            t.join(timeout=1)
        for s in (self.tcp, self.uds):
            s.close()
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------- the run


@dataclass
class _Turn:
    init: dict[str, Any] | None = None
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    hook_events: set[str] = field(default_factory=set)
    result: dict[str, Any] | None = None
    timed_out: bool = False


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _drive(argv: list[str], *, cwd: Path, env: dict[str, str], prompt: str, timeout: float,
           decide: Callable[[dict[str, Any]], dict[str, Any]], stream_log: Path | None = None) -> _Turn:
    turn = _Turn()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, text=True, bufsize=1,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    if stdin is None or stdout is None or stderr is None:
        raise OSError("claude started without its pipes")
    stderr_lines: list[str] = []
    threading.Thread(target=lambda: stderr_lines.extend(stderr), daemon=True).start()

    def on_timeout() -> None:
        turn.timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, on_timeout)
    timer.start()

    def send(obj: dict[str, Any]) -> None:
        try:
            stdin.write(json.dumps(obj) + "\n")
            stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    try:
        send({"type": "control_request", "request_id": f"init-{secrets.token_hex(4)}",
              "request": {"subtype": "initialize", "hooks": None}})
        send({"type": "user", "session_id": "",
              "message": {"role": "user", "content": prompt}, "parent_tool_use_id": None})
        log = open(stream_log, "a", encoding="utf-8") if stream_log else None
        for line in stdout:
            if log:
                log.write(line)
                log.flush()
            try:
                m = json.loads(line)
            except ValueError:
                continue
            t = m.get("type")
            if t == "system":
                st = m.get("subtype") or ""
                if st == "init":
                    turn.init = m
                elif st.startswith("hook"):
                    name = m.get("hook_event") or m.get("hook_event_name") or m.get("hook_name") or ""
                    if name:
                        turn.hook_events.add(str(name).split(":")[0])
            elif t == "control_request" and (m.get("request") or {}).get("subtype") == "can_use_tool":
                req = m["request"]
                turn.prompts.append({"tool_name": req.get("tool_name"), "input": req.get("input")})
                send({"type": "control_response", "response": {
                    "subtype": "success", "request_id": m.get("request_id"), "response": decide(req)}})
            elif t == "assistant":
                for c in (m.get("message") or {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        turn.tool_uses.append(c)
            elif t == "user":
                content = (m.get("message") or {}).get("content")
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        turn.results[c.get("tool_use_id", "")] = _result_text(c.get("content"))
            elif t == "result":
                turn.result = m
                stdin.close()
                break
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return turn


def run_cc_canary(profile: CCHostProfile, *, timeout: float = 240.0, canary_model: str = "haiku",
                  probe_python: str = "/usr/bin/python3",
                  control_answer: Callable[[str, str], Literal["allow", "deny"] | None] | None = None,
                  stream_log: Path | None = None) -> CanaryVerdict:
    """Run the canary once. ``control_answer(slot, detail)`` exists for the build's control runs
    only: it may override the host's answer for a named slot ("bash" for the probe command, or a
    Write slot) so a control can show an arm failing. A hosting caller never passes it."""
    nonce = secrets.token_hex(8)
    ent, run = profile.entity, profile.run_dir
    work = run / f"canary-{nonce}"
    work.mkdir(mode=0o700)
    # AF_UNIX paths are capped (104 bytes on macOS, 108 on Linux): the socket sits directly in the
    # run dir under a short name, and an over-long path refuses rather than truncating.
    sock = run / f"c{nonce[:8]}.s"
    if len(os.fsencode(sock)) >= 100:
        shutil.rmtree(work, ignore_errors=True)
        raise HostRefusal(f"the host run dir path is too long for a Unix socket: {os.fspath(sock)!r}")
    listener = _Listener(sock)
    outside_probe = work / "fsout"
    hook_probe = ent / "activation" / "hooks" / f"selftest-{nonce}.txt"
    w_outside = work / "write-outside.txt"
    w_hook = ent / "activation" / "hooks" / f"selftest-w-{nonce}.txt"
    link_target = work / "linked"
    link_target.mkdir()
    link = ent / f"selftest-link-{nonce}"
    w_link = link / "via-link.txt"
    w_scratch = ent / f"selftest-scratch-{nonce}.txt"
    slots = {str(w_outside): "outside", str(w_hook): "hook", str(w_link): "link", str(w_scratch): "scratch"}
    script = work / "selftest.py"
    script.write_text(probe_script(nonce=nonce, port=listener.port, sock=str(listener.sock_path),
                                   outside=str(outside_probe), hook=str(hook_probe)), encoding="utf-8")
    cmd = probe_command(script, python=probe_python)
    checks: list[Check] = []
    turn: _Turn | None = None
    landed_paths = [outside_probe, hook_probe, w_outside, w_hook, link_target / "via-link.txt"]
    try:
        os.symlink(link_target, link)
        pre = listener.self_connect()

        def decide(req: dict[str, Any]) -> dict[str, Any]:
            name, inp = req.get("tool_name"), req.get("input") or {}
            slot = None
            if name == "Bash" and inp.get("command") == cmd:
                slot = "bash"
            elif name == "Write" and str(inp.get("file_path")) in slots:
                slot = slots[str(inp["file_path"])]
            verdict = "allow" if slot == "scratch" else "deny"
            if control_answer and slot:
                verdict = control_answer(slot, str(inp)) or verdict
            if verdict == "allow":
                return {"behavior": "allow", "updatedInput": inp}
            return {"behavior": "deny", "message": "denied by the Levain canary"}

        prompt = (
            "Levain host self-test. Before this session is hosted in the operator's cockpit, the "
            "Levain host (the program running you, acting for the operator) checks that this "
            "environment blocks what it should. It needs you to ATTEMPT the five tool calls below "
            "exactly as written. Several are designed to be blocked by the sandbox or denied by the "
            "host; a blocked or denied call is the expected, passing result, and the host records "
            "it. Nothing here reaches anyone else: the socket and files belong to the host's own "
            "self-test. Make exactly these calls, in order, with exactly these arguments, do not "
            "retry a failed call, make no other tool call, then reply DONE.\n"
            f"1. Bash, command exactly: {cmd}\n"
            f"2. Write, file_path {w_outside}, content: selftest\n"
            f"3. Write, file_path {w_hook}, content: selftest\n"
            f"4. Write, file_path {w_link}, content: selftest\n"
            f"5. Write, file_path {w_scratch}, content: selftest\n")
        try:
            turn = _drive(profile.argv(canary=True, canary_model=canary_model), cwd=ent,
                          env=dict(profile.env), prompt=prompt, timeout=timeout, decide=decide,
                          stream_log=stream_log)
        except OSError as exc:
            checks.append(Check("C1 profile loaded", "inconclusive", f"claude did not start: {exc}"))
        post = listener.self_connect()
        checks.append(Check("C0 listener live", "pass" if pre and post else "fail",
                            f"self-connect before={pre} after={post}"))
        if turn is not None:
            checks.extend(_evaluate(turn, nonce=nonce, cmd=cmd, slots=slots, listener=listener,
                                    landed=landed_paths, scratch=w_scratch))
    finally:
        listener.close()
        for p in (w_scratch, link):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        for p in (hook_probe, w_hook):
            # Only ever present if a check already failed; removed so a failed canary leaves the
            # hook tree as it found it (the pin would otherwise refuse the next open).
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(work, ignore_errors=True)
    version = (turn.init or {}).get("claude_code_version") if turn is not None else None
    ok = bool(checks) and all(c.status == "pass" for c in checks)
    return CanaryVerdict(ok=ok, checks=tuple(checks), claude_version=version,
                         claude_bin=os.path.realpath(profile.claude_bin), profile_digest=profile.digest())


def _evaluate(turn: _Turn, *, nonce: str, cmd: str, slots: dict[str, str], listener: _Listener,
              landed: list[Path], scratch: Path) -> list[Check]:
    checks: list[Check] = []
    if turn.timed_out or turn.init is None:
        return [Check("C1 profile loaded", "inconclusive",
                      "timed out" if turn.timed_out else "no init event (claude refused to start?)")]
    checks.append(check_init_event(turn.init))

    # C6: Levain's hooks ran, so the hosted session is the entity, not a bare model.
    missing = {"SessionStart", "UserPromptSubmit"} - turn.hook_events
    checks.append(Check("C6 entity hooks ran", "fail" if missing else "pass",
                        f"no hook events for {sorted(missing)}" if missing else "SessionStart, UserPromptSubmit"))

    bash = [u for u in turn.tool_uses if u.get("name") == "Bash"]
    writes = [u for u in turn.tool_uses if u.get("name") == "Write"]
    other = [u.get("name") for u in turn.tool_uses if u.get("name") not in ("Bash", "Write")]

    # C2: exactly one Bash call, byte-identical to the probe, every line BLOCKED.
    if len(bash) != 1 or other:
        checks.append(Check("C2 Bash arm", "inconclusive",
                            f"{len(bash)} Bash call(s), other tools {other!r}"))
    elif (bash[0].get("input") or {}).get("command") != cmd:
        checks.append(Check("C2 Bash arm", "fail", "the Bash command differs from the probe"))
    else:
        out = turn.results.get(bash[0].get("id", ""), "")
        lines = {ln.split()[1]: " ".join(ln.split()[2:]) for ln in out.splitlines()
                 if ln.startswith(nonce + " ") and len(ln.split()) >= 3}
        want = ("TCP", "UDS", "FSOUT", "FSHOOK")
        opened = [k for k in want if lines.get(k) in ("CONNECTED", "ALLOWED")]
        if opened:
            checks.append(Check("C2 Bash arm", "fail", f"not blocked: {opened} ({lines})"))
        elif all(lines.get(k) == "BLOCKED" for k in want):
            checks.append(Check("C2 Bash arm", "pass", "TCP, UDS, FSOUT, FSHOOK all BLOCKED (EPERM/EACCES)"))
        else:
            checks.append(Check("C2 Bash arm", "inconclusive", f"probe output incomplete: {lines or out[:200]!r}"))

    # C3: nothing reached the listener and nothing the probe or the writes aimed at exists.
    hits = listener.foreign_hits()
    present = [str(p) for p in landed if p.exists()]
    checks.append(Check("C3 server side", "fail" if hits or present else "pass",
                        f"listener hits={hits}, present={present}" if hits or present else "no hits, no files"))

    # C4: the sandboxed Bash call never prompted (a prompt is the tell of a dropped sandbox block).
    bash_prompts = [p for p in turn.prompts if p.get("tool_name") == "Bash"]
    checks.append(Check("C4 no Bash prompt", "fail" if bash_prompts else "pass",
                        f"{len(bash_prompts)} Bash prompt(s)" if bash_prompts else "none"))

    # C5: the file-tool arm. Protected and outside writes never land; the scratch write reaches
    # the host as a prompt (the gate fires at all) and lands only because the host allowed it.
    seen = {str((u.get("input") or {}).get("file_path")): u for u in writes}
    prompted = {str((p.get("input") or {}).get("file_path")) for p in turn.prompts if p.get("tool_name") == "Write"}
    problems, unsure = [], []
    for path, slot in slots.items():
        if path not in seen:
            unsure.append(f"{slot}: no Write call")
            continue
        if slot == "scratch":
            if not Path(path).exists() and path in prompted:
                unsure.append("scratch: allowed but did not land")
            elif path not in prompted:
                problems.append("scratch: no permission prompt reached the host")
        elif slot == "link":
            if (Path(path).resolve()).exists():
                problems.append("link: the write landed outside the workspace through a symlink")
        elif Path(path).exists():
            problems.append(f"{slot}: the write landed")
    status: Status = "fail" if problems else ("inconclusive" if unsure else "pass")
    checks.append(Check("C5 file-tool arm", status, "; ".join(problems + unsure) or
                        "outside, hook and symlink writes never landed; the scratch write reached the host"))
    return checks
