"""The hands account's own MCP registry (levain 15, M1): the entry, its registration-time refusals, the lease, and ONE
renderer that turns an entry into both vendors' config.

The hands account carries its own MCP list and its own logins; nothing is imported from the operator's servers and no
credential is brokered. An entry is a signed authority-log grant of kind :data:`levain.authority.HANDS_MCP_KIND`
(the shape of the egress-key setup record: fixed terms under an id, a renew only moves the expiry, at most 30 days
past its link). No process is spawned and no file is written; the one read is the stat (and the links) of a stdio
command and of the directories above it (:func:`command_problems`), at registration and again at every render, and the same entry
renders to the same bytes. What launches the servers (M2), signs a server in (M3) and audits drift (M4) are later
slices.

The entry is signed into the authority log in plaintext, so it carries no secret: not in argv, not in a url's query,
and not as the value of an env name that says it holds one. A server's credential lives in the hands account's own
environment or its own login (M3's sign-in).

``identity_kind`` records whose login the server will hold: ``dedicated`` (an identity made for the hands account,
the default and the recommended path) or ``operator`` (the operator's own login; anything that runs as the hands
account can use it, and a card says so plainly). It is recorded here and never acted on.

``server_sha256`` is the digest of the Codex table this entry renders. It is checked when a link is appended and
again inside every renderer, never at replay, so a later change to the renderer cannot make an old link refuse the
log: :func:`live_entries` reports an entry whose digest no longer matches its table and leaves it out, and a renderer
handed one refuses."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

__all__ = ["CLAUDE_TYPE", "DIGEST_REFUSAL", "HandsMcpRefused", "RESERVED_NAMES", "build_entry", "claude_servers",
           "codex_toml", "command_problems", "default_callback_port", "lease_expiry", "live_entries",
           "registration_problems", "render_claude_json", "render_codex_toml", "resolve_command", "secret_in_arg", "server_sha256", "TRUSTED_COMMAND_DIRS"]

# the names the hands account's own servers never take: the two Levain itself writes into a vendor config
RESERVED_NAMES = frozenset({"anneal_memory", "levain_band"})
CALLBACK_PORT_LOW, CALLBACK_PORT_HIGH = 49200, 49999
CLAUDE_TYPE = {"stdio": "stdio", "remote": "http"}
# where a bare command name (``npx``, ``uvx``) is looked up when an entry is BUILT: a fixed list, never the caller's PATH
# or environment. A module constant so a test can point it at a scratch directory.
TRUSTED_COMMAND_DIRS: tuple[str, ...] = ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")
CODEX_STORE_LINE = 'mcp_oauth_credentials_store = "file"'
APPROVAL_MODE = "prompt"
DIGEST_REFUSAL = "server_sha256 is not the digest of the table this entry renders"


class HandsMcpRefused(Exception):
    """An entry the registration-time rules refuse, or a render the registry cannot honour."""


# ---------------------------------------------------------------------------------------------------------------
# registration-time refusals (rev 7's import refusals, made registration-time)

# the words that say a name holds a credential; ``auth`` only as a word of its own (``--author`` is not one)
_SECRET_WORDS = r"(?:token|secret|password|passwd|api[-_]?key|apikey|bearer|credential|auth(?:orization)?(?![a-z]))"
# a flag whose NAME says its value is a credential, `--api-key=V` or `--api-key V`
_SECRET_FLAG = re.compile(r"^-{1,2}[A-Za-z0-9_.-]*" + _SECRET_WORDS + r"[A-Za-z0-9_.-]*$", re.I)
# an env name or a url query name that says its value is a credential: the flag's words, and ``key`` as a word
_SECRET_NAME = re.compile(_SECRET_WORDS + r"|key(?![a-z])", re.I)
# env names that change what code the server's interpreter or loader runs
_LOADER_ENV = re.compile(r"(?:LD_|DYLD_)[A-Z0-9_]*|NODE_OPTIONS|NODE_PATH|PYTHONPATH|PYTHONSTARTUP|PYTHONHOME|"
                         r"PERL5OPT|PERL5LIB|RUBYOPT|RUBYLIB|BASH_ENV|ENV|ZDOTDIR")
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
    if sys.platform == "darwin":          # APFS and HFS+ default to case-insensitive, and realpath keeps the spelling
        real, base = real.casefold(), base.casefold()
    return real == base or real.startswith(base.rstrip("/") + "/")


_MAX_LINKS = 40


def _loose_dir(path: str, uid: int) -> str | None:
    """The first directory from ``path`` up to ``/`` that an account other than root and ``uid`` could change (owned
    by another, or group- or world-writable: a sticky ``/tmp`` included), or None."""
    d = path
    while True:
        st = os.stat(d)
        if st.st_uid not in (0, uid):
            return f"the directory {d[:120]!r} is not owned by root or the operator"
        if st.st_mode & 0o022:
            return f"the directory {d[:120]!r} is writable by group or world"
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_command(cmd: str) -> str:
    """``cmd`` as the absolute path the entry will store. An absolute ``cmd`` is returned as given (judged later by
    :func:`command_problems`); a bare name (no slash) is looked up in :data:`TRUSTED_COMMAND_DIRS` in order, the first
    executable regular file (a link followed) winning, and the caller's PATH and environment are never read. A bare name
    nobody holds there, or a name with a slash that is not absolute, raises :class:`HandsMcpRefused`. What is signed is
    then what runs: the stored path, which :func:`command_problems` judges at registration and at every render."""
    if cmd.startswith("/") or not cmd:
        return cmd
    if "/" in cmd or "\0" in cmd or cmd in (".", ".."):
        raise HandsMcpRefused(f"the command {cmd[:120]!r} must be an absolute path or a bare name with no slash")
    for d in TRUSTED_COMMAND_DIRS:
        cand = os.path.join(d, cmd)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise HandsMcpRefused(f"the command {cmd[:120]!r} is not an executable in {', '.join(TRUSTED_COMMAND_DIRS)}; "
                          "name its absolute path")


def command_problems(cmd: str, *, home: str) -> list[str]:
    """Why a stdio ``cmd`` is not one only root or the operator can replace, empty when it is: an absolute path, not
    under the operator's ``home``, that exists; every symlink on the way is followed and each one's directory is judged
    as well as the target's; the target is a regular executable file owned by root or the operator (this process's
    uid) with no group or world write bit, and no directory above it, nor above any link to it, is writable by group
    or world or owned by another account. The rule that fails is named. Judged at registration and again at render."""
    if not cmd.startswith("/") or ".." in cmd.split("/"):
        return [f"the command {cmd[:120]!r} must be an absolute path (a bare name is resolved "
                f"when the entry is built, :func:`resolve_command`, and the resolved path is what is stored)"]
    if _under(cmd, home):
        return [f"the command {cmd[:120]!r} is under the operator's home: the hands account runs nothing the "
                "operator can rewrite"]
    uid = os.getuid()
    p = os.path.normpath(cmd)
    loose: str | None = None
    try:
        for _ in range(_MAX_LINKS):
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                return [f"the command {p[:120]!r} does not exist: a path nobody holds yet is one anybody may create"]
            loose = loose or _loose_dir(os.path.realpath(os.path.dirname(p)), uid)
            if not stat.S_ISLNK(st.st_mode):
                break
            p = os.path.normpath(os.path.join(os.path.dirname(p), os.readlink(p)))
        else:
            return [f"the command {cmd[:120]!r} follows more than {_MAX_LINKS} links"]
    except (OSError, ValueError) as exc:
        return [f"the command {cmd[:120]!r} cannot be judged: {type(exc).__name__}"]
    # the file's own rules first, so a refusal names the file before a directory above it
    if _under(p, home):
        return [f"the command {cmd[:120]!r} resolves under the operator's home"]
    if not stat.S_ISREG(st.st_mode):
        return [f"the command {cmd[:120]!r} is not a regular file"]
    if not st.st_mode & 0o111:
        return [f"the command {cmd[:120]!r} is not executable"]
    if st.st_uid not in (0, uid):
        return [f"the command {cmd[:120]!r} is not owned by root or the operator"]
    if st.st_mode & 0o022:
        return [f"the command {cmd[:120]!r} is writable by group or world"]
    return [f"the command {cmd[:120]!r}: {loose}"] if loose else []


def secret_in_arg(arg: str) -> bool:
    """Whether one argv element looks like a credential: a secret-named flag carrying a value, a known token
    spelling, URL userinfo, or a bare run of 40+ token characters. A heuristic: it refuses the common shapes, it does
    not prove an argv holds none."""
    if "=" in arg and _SECRET_FLAG.match(arg.split("=", 1)[0]) and arg.split("=", 1)[1]:
        return True
    return bool(_SECRET_VALUE.search(arg) or _USERINFO.search(arg) or _LONG_RUN.search(arg))


_NO_SECRET = "a secret belongs in the hands account's own environment (M3's sign-in), never in the signed entry"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def registration_problems(entry: Mapping[str, Any], *, home: str) -> list[str]:
    """Every registration-time refusal for ``entry`` (a shape-valid entry; ``home`` the operator's home directory),
    empty when it passes. stdio: the command passes :func:`command_problems`; no argument carries a credential, nor
    does one follow a secret-named flag; no env name says it holds a credential while carrying a value, and none
    changes what the server's loader or interpreter runs. remote: a well-formed https url, or a loopback one, with no
    userinfo, no fragment and no secret-named query value. The server's env is the entry's own, never inherited."""
    problems: list[str] = []
    if entry["name"] in RESERVED_NAMES:
        problems.append(f"the name {entry['name']!r} is one Levain writes itself")
    if entry["transport"] == "stdio":
        problems += command_problems(entry["command"], home=home)
        args = list(entry["args"])
        for i, a in enumerate(args):
            if secret_in_arg(a):
                problems.append(f"argument {i} looks like a credential: {_NO_SECRET}")
            elif _SECRET_FLAG.match(a) and "=" not in a and i + 1 < len(args) and not args[i + 1].startswith("-"):
                problems.append(f"argument {i + 1} follows the secret-named flag {a[:60]!r}: {_NO_SECRET}")
        for k, v in sorted(entry["env"].items()):
            if _LOADER_ENV.fullmatch(k):
                problems.append(f"the env name {k!r} changes what the server's loader or interpreter runs")
            elif v and _SECRET_NAME.search(k):
                problems.append(f"the env name {k!r} says it holds a credential, and the entry is signed in "
                                f"plaintext: {_NO_SECRET}")
    else:
        problems += _url_problems(entry["url"])
    return problems


def _url_problems(url: str) -> list[str]:
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return ["a remote url is malformed (whitespace or a control character)"]
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port                                     # noqa: B018 (raises on a port out of range or not a number)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as exc:
        return [f"a remote url is malformed ({str(exc)[:80]})"]
    problems: list[str] = []
    if parts.username is not None or parts.password is not None:
        problems.append("a remote url carries no userinfo")
    if parts.scheme == "https":
        if not host:
            problems.append("a remote url names a host")
    elif parts.scheme != "http" or not _loopback(host):
        problems.append("a remote url is https, or http only on loopback")
    if parts.fragment:
        problems.append("a remote url carries no fragment")
    for k, v in query:
        if v and _SECRET_NAME.search(k):
            problems.append(f"the url query name {k[:60]!r} says it holds a credential: {_NO_SECRET}")
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


def _codex_table(entry: Mapping[str, Any]) -> str:
    """The Codex ``[mcp_servers.<name>]`` table for one entry, newline-terminated, emitted as given: the digest's
    basis, so it checks nothing; only the renderers below hand one out, after :func:`_renderable`. Every tool call asks
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
    """The digest the lease signs: sha256 of the entry's Codex table."""
    return hashlib.sha256(_codex_table(entry).encode("utf-8")).hexdigest()


def _render_problem(entry: Mapping[str, Any]) -> str | None:
    """Why ``entry`` is not rendered now, or None: its digest is not its own table's, its name is one Levain writes,
    or (stdio) its command no longer passes :func:`command_problems` against this process's home."""
    if server_sha256(entry) != entry["server_sha256"]:
        return DIGEST_REFUSAL
    if entry["name"] in RESERVED_NAMES:
        return f"the name {entry['name']!r} is one Levain writes itself"
    if entry["transport"] == "stdio":
        problems = command_problems(entry["command"], home=os.path.expanduser("~"))
        if problems:
            return problems[0]
    return None


def _renderable(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """``entries`` in name order, each judged by :func:`_render_problem` and all unique by name and callback port;
    the first that fails refuses the whole render, so no renderer emits an entry it was handed as given."""
    entries = list(entries)
    for e in entries:
        why = _render_problem(e)
        if why is not None:
            raise HandsMcpRefused(f"hands MCP entry {str(e.get('id'))[:40]!r}: {why}")
    return _unique(entries)


def codex_toml(entries: Iterable[Mapping[str, Any]]) -> str:
    """A whole Codex config body for ``entries``: the file credential store pinned, then each server's table in name
    order. An entry :func:`_renderable` refuses, or two entries of one name, refuse the render, never merged."""
    ordered = _renderable(entries)
    return "\n".join([CODEX_STORE_LINE + "\n"] + [_codex_table(e) for e in ordered])


def _claude_server(entry: Mapping[str, Any]) -> dict[str, Any]:
    if entry["transport"] == "stdio":
        out: dict[str, Any] = {"type": CLAUDE_TYPE["stdio"], "command": entry["command"], "args": list(entry["args"])}
        if entry["env"]:
            out["env"] = dict(sorted(entry["env"].items()))
        return out
    return {"type": CLAUDE_TYPE["remote"], "url": entry["url"]}


def claude_servers(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each entry's Claude ``mcpServers`` value by name, under the same refusals as :func:`codex_toml`."""
    return {e["name"]: _claude_server(e) for e in _renderable(entries)}


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
    kind, unexpired, its ``server_sha256`` the digest of the table this renderer makes of it, and (stdio) its command
    still passing :func:`command_problems`. ``skipped`` is ``(id, why)`` for every hands-mcp entry left out
    (``expired``, ``digest``, ``name`` or ``command``): reported rather than rendered, so one stale entry does not refuse the
    rest. ``now`` is the caller's clock alone (the log's head time is not consulted)."""
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
        elif _render_problem(entry) is not None:
            skipped.append((gid, "name" if entry["name"] in RESERVED_NAMES else "command"))
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
    defaults to :func:`default_callback_port`. A stdio ``command`` that is a bare name is resolved to its absolute path
    first (:func:`resolve_command`, which raises :class:`HandsMcpRefused` when no trusted directory holds it) and the
    digest is of the resolved entry. Otherwise not validated here: the log's validate and append rules judge it."""
    if transport == "stdio":
        command = resolve_command(command)
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
