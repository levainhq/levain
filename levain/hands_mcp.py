"""The hands account's own MCP registry (levain 15, M1): the entry, its registration-time refusals, the lease, and ONE
renderer that turns an entry into both vendors' config.

The hands account carries its own MCP list and its own logins; nothing is imported from the operator's servers and no
credential is brokered. An entry is a signed authority-log grant of kind :data:`levain.authority.HANDS_MCP_KIND`
(the shape of the egress-key setup record: fixed terms under an id, a renew only moves the expiry, at most 30 days
past its link). Everything here is a pure function: no process is spawned, no file is read or written, and the same
entry renders to the same bytes. What launches the servers (M2), signs a server in (M3) and audits drift (M4) are
later slices.

``identity_kind`` records whose login the server will hold: ``dedicated`` (an identity made for the hands account,
the default and the recommended path) or ``operator`` (the operator's own login; anything that runs as the hands
account can use it, and a card says so plainly). It is recorded here and never acted on.

``server_sha256`` is the digest of the Codex table this entry renders. It is checked when a link is appended and
again when an entry is rendered, never at replay, so a later change to the renderer cannot make an old link refuse the
log: an entry whose digest no longer matches its table is reported and not rendered."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

__all__ = ["CLAUDE_TYPE", "HandsMcpRefused", "RESERVED_NAMES", "build_entry", "claude_server", "claude_servers",
           "codex_server_table", "codex_toml", "default_callback_port", "lease_expiry", "live_entries",
           "registration_problems", "render_claude_json", "render_codex_toml", "secret_in_arg", "server_sha256"]

# the names the hands account's own servers never take: the two Levain itself writes into a vendor config
RESERVED_NAMES = frozenset({"anneal_memory", "levain_band"})
CALLBACK_PORT_LOW, CALLBACK_PORT_HIGH = 49200, 49999
CLAUDE_TYPE = {"stdio": "stdio", "remote": "http"}
CODEX_STORE_LINE = 'mcp_oauth_credentials_store = "file"'
APPROVAL_MODE = "prompt"


class HandsMcpRefused(Exception):
    """An entry the registration-time rules refuse, or a render the registry cannot honour."""


# ---------------------------------------------------------------------------------------------------------------
# registration-time refusals (rev 7's import refusals, made registration-time)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# a flag whose NAME says its value is a credential, `--api-key=V` or `--api-key V`
_SECRET_FLAG = re.compile(r"^-{1,2}[A-Za-z0-9_.-]*(?:token|secret|password|passwd|api[-_]?key|apikey|bearer|"
                          r"credential|auth(?:orization)?)[A-Za-z0-9_.-]*$", re.I)
# well-known credential spellings and a bare high-entropy run
_SECRET_VALUE = re.compile(r"(?:^|[=:\s])(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                           r"gho_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
                           r"lin_api_[A-Za-z0-9]{20,}|(?:bearer|basic)\s+\S{8,})", re.I)
_LONG_RUN = re.compile(r"(?:^|[=:/])[A-Za-z0-9+/_-]{40,}={0,2}$")
_USERINFO = re.compile(r"://[^/\s@]*:[^/\s@]*@")


def _under(path: str, home: str) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(home)
    except (OSError, ValueError):
        return True                       # unresolvable: refuse
    return real == base or real.startswith(base.rstrip("/") + "/")


def secret_in_arg(arg: str) -> bool:
    """Whether one argv element looks like a credential: a secret-named flag carrying a value, a known token
    spelling, URL userinfo, or a bare run of 40+ token characters. A heuristic: it refuses the common shapes, it does
    not prove an argv holds none."""
    if "=" in arg and _SECRET_FLAG.match(arg.split("=", 1)[0]) and arg.split("=", 1)[1]:
        return True
    return bool(_SECRET_VALUE.search(arg) or _USERINFO.search(arg) or _LONG_RUN.search(arg))


def registration_problems(entry: Mapping[str, Any], *, home: str) -> list[str]:
    """Every registration-time refusal for ``entry`` (a shape-valid entry; ``home`` the operator's home directory),
    empty when it passes. stdio: the command is a bare name (the hands PATH finds it) or an absolute path outside
    the operator's home, and no argument carries a credential, nor does one follow a secret-named flag; remote: an
    https url, or a loopback url, with no userinfo. The server's env is the entry's own and is never inherited."""
    problems: list[str] = []
    if entry["name"] in RESERVED_NAMES:
        problems.append(f"the name {entry['name']!r} is one Levain writes itself")
    if entry["transport"] == "stdio":
        cmd = entry["command"]
        if cmd.startswith("~") or ("/" in cmd and not cmd.startswith("/")) or ".." in cmd.split("/"):
            problems.append(f"the command {cmd[:120]!r} must be a bare name or an absolute path")
        elif cmd.startswith("/") and _under(cmd, home):
            problems.append(f"the command {cmd[:120]!r} is under the operator's home: the hands account runs "
                            "nothing the operator can rewrite")
        args = list(entry["args"])
        for i, a in enumerate(args):
            if secret_in_arg(a):
                problems.append(f"argument {i} looks like a credential: a secret goes in the entry's env, never argv")
            elif _SECRET_FLAG.match(a) and "=" not in a and i + 1 < len(args) and not args[i + 1].startswith("-"):
                problems.append(f"argument {i + 1} follows the secret-named flag {a[:60]!r}: a secret goes in the "
                                "entry's env, never argv")
    else:
        parts = urlsplit(entry["url"])
        host = (parts.hostname or "").lower()
        if parts.username is not None or parts.password is not None:
            problems.append("a remote url carries no userinfo")
        if parts.scheme == "https":
            if not host:
                problems.append("a remote url names a host")
        elif parts.scheme == "http":
            if host not in _LOOPBACK:
                problems.append("a remote url is https, or http only on loopback")
        else:
            problems.append("a remote url is https, or http only on loopback")
        if parts.fragment:
            problems.append("a remote url carries no fragment")
    return problems


# ---------------------------------------------------------------------------------------------------------------
# the renderer: one entry -> both vendors

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_str(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else _toml_str(k)


def codex_server_table(entry: Mapping[str, Any]) -> str:
    """The Codex ``[mcp_servers.<name>]`` table for one entry, newline-terminated. Every tool call asks
    (``default_tools_approval_mode = "prompt"``); a remote entry pins its OAuth callback port in its own
    ``[mcp_servers.<name>.oauth]`` table; a stdio entry's env is its own table and nothing is inherited from it."""
    name = entry["name"]
    lines = [f"[mcp_servers.{name}]"]
    if entry["transport"] == "stdio":
        lines.append(f"command = {_toml_str(entry['command'])}")
        lines.append("args = [" + ", ".join(_toml_str(a) for a in entry["args"]) + "]")
    else:
        lines.append(f"url = {_toml_str(entry['url'])}")
    lines.append(f'default_tools_approval_mode = "{APPROVAL_MODE}"')
    if entry["transport"] == "stdio" and entry["env"]:
        lines.append(f"[mcp_servers.{name}.env]")
        lines += [f"{_toml_key(k)} = {_toml_str(v)}" for k, v in sorted(entry["env"].items())]
    if entry["transport"] == "remote":
        lines.append(f"[mcp_servers.{name}.oauth]")
        lines.append(f"callback_port = {entry['callback_port']}")
    return "\n".join(lines) + "\n"


def server_sha256(entry: Mapping[str, Any]) -> str:
    """The digest the lease signs: sha256 of the entry's Codex table (:func:`codex_server_table`)."""
    return hashlib.sha256(codex_server_table(entry).encode("utf-8")).hexdigest()


def codex_toml(entries: Iterable[Mapping[str, Any]]) -> str:
    """A whole Codex config body for ``entries``: the file credential store pinned, then each server's table in name
    order. Two entries of one name are refused here, never merged."""
    ordered = _unique(entries)
    return "\n".join([CODEX_STORE_LINE + "\n"] + [codex_server_table(e) for e in ordered])


def claude_server(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One server's Claude ``mcpServers`` value."""
    if entry["transport"] == "stdio":
        out: dict[str, Any] = {"type": CLAUDE_TYPE["stdio"], "command": entry["command"], "args": list(entry["args"])}
        if entry["env"]:
            out["env"] = dict(sorted(entry["env"].items()))
        return out
    return {"type": CLAUDE_TYPE["remote"], "url": entry["url"]}


def claude_servers(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["name"]: claude_server(e) for e in _unique(entries)}


def render_claude_json(entries: Iterable[Mapping[str, Any]]) -> str:
    """The Claude ``--mcp-config`` file body for ``entries``, canonical JSON (sorted keys, newline-terminated). The
    OAuth callback port is a login-time flag in Claude (``--callback-port``, M3's), never a config key."""
    return json.dumps({"mcpServers": claude_servers(entries)}, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_codex_toml(entries: Iterable[Mapping[str, Any]]) -> str:
    return codex_toml(entries)


def _unique(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    ordered = sorted(entries, key=lambda e: e["name"])
    names = [e["name"] for e in ordered]
    if len(set(names)) != len(names):
        raise HandsMcpRefused("two entries carry one name: the registry renders one server per name")
    ports = [e["callback_port"] for e in ordered if e["transport"] == "remote"]
    if len(set(ports)) != len(ports):
        raise HandsMcpRefused("two remote entries pin one callback port")
    return ordered


# ---------------------------------------------------------------------------------------------------------------
# the lease

def live_entries(grants: Mapping[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """``(renderable, skipped)`` from an authority policy's grants at ``now``. A renderable entry is of the hands-mcp
    kind, unexpired, and its ``server_sha256`` is the digest of the table this renderer makes of it. ``skipped`` is
    ``(id, why)`` for every hands-mcp entry left out (``expired`` or ``digest``): an expired lease is not rendered,
    and a digest that no longer matches is reported rather than rendered."""
    from levain.authority import HANDS_MCP_KIND, _parse_stamp, _thaw
    ok: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    for gid, g in sorted(grants.items()):
        if not (isinstance(g, Mapping) and g.get("kind") == HANDS_MCP_KIND):
            continue
        entry = _thaw(g)
        if _parse_stamp(entry["expires_at"], "expires_at") <= now:
            skipped.append((gid, "expired"))
        elif server_sha256(entry) != entry["server_sha256"]:
            skipped.append((gid, "digest"))
        else:
            ok.append(entry)
    return ok, skipped


def default_callback_port(name: str) -> int:
    """A stable port in 49200-49999 derived from the server's name: pinned once at registration and then carried in
    the entry, so a later change to this function moves no signed entry."""
    n = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big")
    return CALLBACK_PORT_LOW + n % (CALLBACK_PORT_HIGH - CALLBACK_PORT_LOW + 1)


def build_entry(*, name: str, transport: str, identity_kind: str, expires_at: str, command: str = "",
                args: Sequence[str] = (), env: Mapping[str, str] | None = None, url: str = "",
                callback_port: int | None = None, entry_id: str | None = None) -> dict[str, Any]:
    """An entry in the signed shape, its digest filled. A stdio entry's ``callback_port`` is 0 and a remote one's
    defaults to :func:`default_callback_port`. Not validated here: the log's validate and append rules judge it."""
    if callback_port is None:
        callback_port = default_callback_port(name) if transport == "remote" else 0
    entry: dict[str, Any] = {
        "id": entry_id if entry_id is not None else "hm-" + secrets.token_hex(6), "kind": "hands-mcp", "name": name,
        "transport": transport, "command": command, "args": list(args), "env": dict(env or {}), "url": url,
        "identity_kind": identity_kind, "callback_port": callback_port, "server_sha256": "0" * 64,
        "expires_at": expires_at}
    entry["server_sha256"] = server_sha256(entry)
    return entry


def lease_expiry(now: datetime, life: timedelta = timedelta(days=30)) -> str:
    """The 30-day default expiry stamp (the form every expiry in the log takes)."""
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(now + life)
