"""The dispatch MCP door: a stdio MCP server for a planner that reaches tools more readily than its shell. Its tools
file a contract, read jobs, recommend and cancel exactly as ``levain dispatch`` does (:mod:`levain.dispatch.cli`),
for the PLANNER only: an MCP door never signs, so it has no sign tool and no human cancel; those are the operator's,
on their own device. Stdlib only: JSON-RPC 2.0, one JSON message per line. Run as
``<python> -P -m levain.dispatch.mcp --agent <claude_code|codex>``.

The session is read per call, as the question band's door reads it (:mod:`levain.consent.band_mcp`): the
``tools/call`` request's ``params._meta`` (``threadId``, then ``sessionId``) when it names a well-formed main-session
id, else the server's own environment (``CLAUDE_CODE_SESSION_ID`` / ``CODEX_THREAD_ID``), else
:data:`levain.dispatch.capability.NO_SESSION`, which keeps no capability record. The session decides which record a
filing's capability is kept in and which record ``dispatch_recommend`` / ``dispatch_cancel`` read when no capability
is passed. A refusal or a machine fault comes back as the tool's own error, its text leading with which it was."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from levain.authority import VerifierUnavailable
from levain.consent.band_mcp import _meta_session
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import capability as caps
from levain.dispatch.cli import NO_REGISTRY, file_job, job_view, planner_capability, render
from levain.dispatch.contract import GitUnavailable
from levain.dispatch.jobs import (
    TERMINAL, CapabilityRefused, JobStore, JobStoreError, SignatureRefused, TransitionRefused,
)

__all__ = ["TOOLS", "handle", "main", "serve"]

_PROTOCOL = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
_NO_AUTHORITY = ("Filing creates a contract the operator must sign on their own device before anything runs; a recommend "
                 "confers no authority.")
_JOB = {"type": "string", "description": "The job id (dj-...)."}
_CAPABILITY = {"type": "string", "description": "The job's capability, as filing returned it (default: the one this "
                                                "session's record kept)."}
TOOLS: list[dict[str, Any]] = [
    {"name": "dispatch_file",
     "description": "File a task contract for cheaper executors to run. " + _NO_AUTHORITY + " Returns the job id, its "
                    "digest, its lease end and, once, its capability (kept in this session's record when the "
                    "session is known). " + NO_REGISTRY.capitalize() + " until a registry exists, so filing refuses.",
     "inputSchema": {"type": "object",
                     "properties": {"contract": {"type": "object", "description": "The contract: template, goal, "
                                                 "read, write, base, budget, grant, expires_at, repo."},
                                    "idem_key": {"type": "string", "description": "A replay with the same key and "
                                                 "contract returns the same job (and no capability)."}},
                     "required": ["contract"], "additionalProperties": False}},
    {"name": "dispatch_show",
     "description": "One job resolved as its signer sees it: the goal in full, the read and write lists, the template, "
                    "verifier, models, budget, expiry, and every transition with its cause.",
     "inputSchema": {"type": "object", "properties": {"job": _JOB}, "required": ["job"],
                     "additionalProperties": False}},
    {"name": "dispatch_list",
     "description": "The live jobs (all: every job), oldest first.",
     "inputSchema": {"type": "object",
                     "properties": {"repo": {"type": "string", "description": "Only this repository (absolute)."},
                                    "all": {"type": "boolean", "description": "Include adopted, failed, cancelled."}},
                     "additionalProperties": False}},
    {"name": "dispatch_recommend",
     "description": "Mark a VERIFIED job you filed as recommended. Advisory: a recommend confers no authority; only the "
                    "operator's signature authorises a result.",
     "inputSchema": {"type": "object", "properties": {"job": _JOB, "capability": _CAPABILITY}, "required": ["job"],
                     "additionalProperties": False}},
    {"name": "dispatch_cancel",
     "description": "Cancel a job you filed, at any state before the operator authorises it (it only removes "
                    "authority). The planner's cancel only: after authorisation, only the operator can cancel.",
     "inputSchema": {"type": "object", "properties": {"job": _JOB, "capability": _CAPABILITY}, "required": ["job"],
                     "additionalProperties": False}},
]
_NAMES = {t["name"]: t for t in TOOLS}
_FAULTS = (GitUnavailable, JobStoreError, caps.CapabilityRecordError, VerifierUnavailable, sqlite3.Error, OSError)
_REFUSALS = (ContractRefused, TransitionRefused, SignatureRefused, CapabilityRefused, DispatchError, ValueError)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _arguments(name: str, raw: Any) -> dict[str, Any]:
    """The tool's arguments, shape-checked against its schema (a wrong shape is the caller's protocol error; whether
    the contract is acceptable is the store's call, and its refusal comes back as the tool's own error)."""
    if not isinstance(raw, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    schema = _NAMES[name]["inputSchema"]
    props = schema["properties"]
    extra = sorted(set(raw) - set(props))
    if extra:
        raise _RpcError(INVALID_PARAMS, f"unknown arguments: {', '.join(extra)}")
    missing = [k for k in schema.get("required", ()) if k not in raw]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"missing arguments: {', '.join(missing)}")
    kinds = {"string": str, "object": dict, "boolean": bool}
    for k, v in raw.items():
        if not isinstance(v, kinds[props[k]["type"]]):
            raise _RpcError(INVALID_PARAMS, f"{k} must be a {props[k]['type']}")
    return raw


def _session(agent: str, params: dict[str, Any]) -> str:
    session = _meta_session(params)
    if session is None:
        env = os.environ
        session = (env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")) or ""
    return session if caps.session_problem(session) is None else caps.NO_SESSION


def _text(text: str, data: Any, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "structuredContent": {"result": data}, "isError": error}


def _call(agent: str, directory: Path | None, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in _NAMES:
        raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
    args = _arguments(name, params.get("arguments", {}))
    session = _session(agent, params)
    store = JobStore(directory)
    try:
        if name == "dispatch_file":
            job, capability, kept = file_job(store, args["contract"], agent=agent, session=session,
                                             idem_key=args.get("idem_key"))
            data = {"id": job.id, "digest": job.digest, "state": job.state, "lease_end": job.lease_end,
                    "capability": capability, "capability_kept": kept}
            return _text(f"Filed job {job.id} ({job.state}), digest {job.digest}, lease ends {job.lease_end}. "
                         f"Capability: {capability if capability is not None else '(not shown again)'}; {kept}. "
                         + _NO_AUTHORITY, data)
        if name == "dispatch_show":
            found = store.get(args["job"])
            if found is None:
                raise TransitionRefused(f"no dispatch job {args['job'][:40]!r}")
            return _text(render(found), job_view(found))
        if name == "dispatch_list":
            jobs = [j for j in store.list(repo=args.get("repo")) if args.get("all") or j.state not in TERMINAL]
            rows = [{"id": j.id, "state": j.state, "repo": j.repo, "lease_end": j.lease_end,
                     "template": j.contract.template.template_id} for j in jobs]
            return _text(json.dumps(rows, ensure_ascii=False) if rows else "No jobs.", rows)
        cap = planner_capability(store.directory, agent, session, args["job"], args.get("capability"))
        if cap is None:
            raise CapabilityRefused(f"this session's record holds no capability for {args['job'][:40]!r}; pass "
                                    "capability as filing returned it")
        if name == "dispatch_recommend":
            job = store.recommend(args["job"], cap)
            return _text(f"Job {job.id} is recommended. It authorises nothing: the operator still decides.",
                         {"id": job.id, "state": job.state})
        job = store.cancel_by_planner(args["job"], cap)
        return _text(f"Cancelled job {job.id} as its planner.", {"id": job.id, "state": job.state})
    except _FAULTS as exc:
        return _text(f"This machine could not do it ({type(exc).__name__}): {exc}", None, error=True)
    except _REFUSALS as exc:
        return _text(f"Refused, nothing changed: {exc}", None, error=True)


def handle(agent: str, msg: Any, *, directory: Path | None = None) -> dict[str, Any] | None:
    """The reply to one decoded message, or ``None`` for a notification (a message with no ``id``). ``directory`` is
    the dispatch directory (default: :func:`levain.dispatch.jobs.default_dispatch_dir`)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "id" not in msg:
        return None
    rid, method, params = msg["id"], msg["method"], msg.get("params")
    if not isinstance(rid, (str, int)) or isinstance(rid, bool):
        return _error(None, INVALID_REQUEST, "id must be a string or an integer")
    try:
        if method == "ping":
            return _ok(rid, {})
        if params is not None and not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        params = params or {}
        if method == "initialize":
            version = params.get("protocolVersion")
            return _ok(rid, {"protocolVersion": version if isinstance(version, str) else _PROTOCOL,
                             "capabilities": {"tools": {}}, "serverInfo": {"name": "levain_dispatch", "version": "1"}})
        if method == "tools/list":
            return _ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            return _ok(rid, _call(agent, directory, params))
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
    except _RpcError as exc:
        return _error(rid, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - a server that dies mid-conversation loses the agent's tools for the session
        return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(agent: str, stdin: Any = None, stdout: Any = None, *, directory: Path | None = None) -> None:
    """Answer one message per line until stdin ends."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):  # a decoder overflow is a parse error too, never the end of the server
            reply: dict[str, Any] | None = _error(None, PARSE_ERROR, "parse error")
        else:
            reply = handle(agent, msg, directory=directory)
        if reply is not None:
            # ASCII-escaped: a raw U+2028 / U+2029 / U+0085 would split the line for a client that splits on every
            # line break, and one reply would become two broken ones
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="levain.dispatch.mcp", description="The dispatch planner's MCP server.")
    parser.add_argument("--agent", required=True, choices=caps.AGENTS)
    parser.add_argument("--dispatch-dir", dest="dispatch_dir", type=Path, default=None,
                        help="The dispatch directory (default: the consent directory).")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        serve(args.agent, directory=args.dispatch_dir)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
