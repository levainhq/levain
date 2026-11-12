"""levain.firing.hands_mcp_login: `levain hands mcp signin|remove|list` and the doctor rows (levain 15, M3 + M4).

The hands account's OWN MCP logins. A server is registered by a signed authority-log entry (:mod:`levain.hands_mcp`,
M1); this module signs a REGISTERED, unexpired, remote server in as the hands account and takes it out again. The
rule is the vendor-login rule (:mod:`levain.firing.hands_login`): the vendor's own command runs AS the hands user
through the same ``env -i`` prefix, with the config home pinned inside the hands home; nothing here reads, copies or
points at the operator's ``~/.claude``, and no token is ever read into this process. Claude's half only: Codex
servers are named-agent work.

What runs as the hands user, under the entity's hands lock (so never beside a session):

- sign-in: ``claude mcp add --scope user --transport http --callback-port <port> <name> <url>`` (the port the
  entry pinned; ``add`` takes it, ``login`` does not), then ``claude mcp login --no-browser <name>``, whose output
  (the authorization URL) is shown and whose stdin (the pasted redirect) is the caller's.
- remove: ``claude mcp logout <name>``, then ``claude mcp remove --scope user <name>``, then a read of what is left.

Levain keeps a record of the servers IT added (``<entity>/.levain/hands-mcp.json``). ``remove`` touches only a
server in that record: a server the hands account holds that sign-in did not add is refused, never removed, and a
name in neither the record nor the vendor config changes nothing. A server whose entry has since expired or been
revoked can still be removed (taking a login away needs no lease).

The doctor rows read the vendor config's ``mcpServers`` names, urls and callback ports as the hands user (never a
token: the credential file is stat'ed for owner and mode only) and compare them with the record and the registry
in one function (:func:`server_status`), which ``list`` reads too. The record sits in ``.levain``, which the entity's
hands can write: it is never followed through a symlink nor written through a predictable temp.

Anything that runs as the hands account can use a server's login, and a remove is local: the card says so
(:data:`REMOVE_NOTICE`)."""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain import pathid
from levain.authority import STOPPED_BORN, STOPPED_CURRENT
from levain.firing import hands_login as hl
from levain.firing.confinement import HandsIdentity
from levain.hands_mcp import registration_problems

VENDOR = "claude"
RECORD_FILE = "hands-mcp.json"
#: the record's outcome once the vendor login exited 0; every other outcome is not signed in
SIGNED_IN = "login-exited-0"
#: a server name as a command-line argument: never an option, never a path
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

SIGNIN_NOTICE = ("Anything that runs as the hands account can use this server's login. "
                 "Sign in with an account made for the hands account where the provider allows a second one.")
REMOVE_NOTICE = ("This remove is LOCAL ONLY: it cleared the login on this machine and neither vendor is known to "
                 "revoke the token at the provider. Revoke it there, on the provider's connected-apps page for {host}. "
                 "Anything that ran as the hands account could have used the login.")
LINUX_EDGE = hl.LINUX_LOGIN_EDGE
#: the skipped whys that mean a revoked key stopped the entry (born or last renewer); no renew brings one back
STOP_REASONS = (STOPPED_BORN, STOPPED_CURRENT)


class HandsMcpError(RuntimeError):
    """A hands MCP step failed after something may have changed, or a read could not be told."""


class HandsMcpRefused(HandsMcpError):
    """Refused before anything was changed."""


# --- the registry (the verified authority log) -----------------------------------------------------------------

def cockpit_root_of(entity_dir: Path | str) -> Path:
    return Path(entity_dir) / ".levain" / "cockpit"


def load_registry(cockpit_root: Path | str, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """``(live entries, skipped (id, why))`` from the cockpit's verified, anchored replay, or :class:`HandsMcpError`.
    A log that cannot be read is an error, never an empty registry. An entry stopped because a key that signed it was
    revoked (:func:`levain.authority.grants_in_force`) is not live and is skipped with its stop reason as the why
    (:func:`levain.authority.grant_stop_reason`: the creator's words or the last renewer's, one of :data:`STOP_REASONS`),
    whatever else is true of it (an expired one included: no renew brings it back). It stays in the log until revoked."""
    from levain.agent_authority import AgentAuthority
    from levain.authority import HANDS_MCP_KIND, NotInitialised, grant_stop_reason, grants_in_force
    from levain.cockpit.firejournal import DB_NAME
    from levain.hands_mcp import live_entries
    if not (Path(cockpit_root) / DB_NAME).is_file():
        return [], []                                   # no journal, so nothing was ever registered
    try:
        state = AgentAuthority(cockpit_root).replay()
    except NotInitialised:
        return [], []                                   # a journal with an empty log registered nothing
    except Exception as exc:  # noqa: BLE001 - a log that cannot be replayed is a fault, never "nothing registered"
        raise HandsMcpError(f"the authority log cannot be read ({type(exc).__name__}: {str(exc)[:160]})") from None
    in_force = grants_in_force(state)
    live, skipped = live_entries(in_force, now or datetime.now(timezone.utc))
    stopped = [(gid, grant_stop_reason(state, gid) or STOPPED_BORN) for gid, g in state.policy.grants.items()
               if gid not in in_force and isinstance(g, Mapping) and g.get("kind") == HANDS_MCP_KIND]
    return live, sorted(skipped + stopped)


def _revoke_cmd(gid: str) -> str:
    """The verb that clears a stopped entry from the log, every required argument shown (no hands verb revokes one; the
    kind-blind consent revoke does)."""
    from levain.authority import _HANDS_MCP_REVOKE
    return _HANDS_MCP_REVOKE.format(gid=gid)


def _grants_named(cockpit_root: Path | str, name: str) -> list[dict[str, Any]]:
    """Every hands-mcp entry of ``name`` in the log, live or not (to say WHY a name cannot sign in)."""
    from levain.agent_authority import AgentAuthority
    from levain.authority import HANDS_MCP_KIND, NotInitialised, _thaw
    from levain.cockpit.firejournal import DB_NAME
    if not (Path(cockpit_root) / DB_NAME).is_file():
        return []
    try:
        state = AgentAuthority(cockpit_root).replay()
    except NotInitialised:
        return []
    return [_thaw(g) for g in state.policy.grants.values()
            if isinstance(g, Mapping) and g.get("kind") == HANDS_MCP_KIND and g.get("name") == name]


# --- the record of what Levain added ---------------------------------------------------------------------------

def _record_path(entity_dir: Path | str) -> Path:
    return Path(entity_dir) / ".levain" / RECORD_FILE


def read_record(entity_dir: Path | str) -> dict[str, dict[str, Any]]:
    """The record, or ``{}`` when there is none. A record that exists and cannot be read raises
    :class:`HandsMcpError`: an unreadable record is not an empty one. ``.levain`` is writable by the entity's hands,
    so the record is read without following a symlink and only as a regular file (:func:`hl.read_record_json`); a
    symlink or anything else at its path is refused by name, never followed."""
    p = _record_path(entity_dir)
    try:
        data = hl.read_record_json(p)
    except hl.RecordFileRefused as exc:
        raise HandsMcpError(f"{exc}; nothing was changed (remove it by hand)") from None
    except (OSError, ValueError) as exc:
        raise HandsMcpError(f"{p} cannot be read ({type(exc).__name__}); nothing was changed") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HandsMcpError(f"{p} is not a record; nothing was changed")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_record(entity_dir: Path | str, name: str, value: dict[str, Any] | None) -> None:
    """Set (or, with None, drop) one name, under a sidecar lock, by :func:`hl.write_record_json` (a random 0600 temp
    opened ``O_EXCL | O_NOFOLLOW``, fsync'd, renamed over the record). Raises :class:`HandsMcpError`; a record that
    is a symlink or not a regular file is refused before anything is written."""
    p = _record_path(entity_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lk = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = read_record(entity_dir)
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            hl.write_record_json(p, data)
        finally:
            os.close(lk)
    except OSError as exc:
        raise HandsMcpError(f"the record {p} could not be written ({type(exc).__name__})") from None


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --- the vendor, as the hands user -----------------------------------------------------------------------------

def _argv(hands: HandsIdentity, *sub: str) -> list[str]:
    return [*hl._launch_prefix(hands, VENDOR), "claude", "mcp", *sub]


def add_argv(hands: HandsIdentity, entry: dict[str, Any]) -> list[str]:
    return _argv(hands, "add", "--scope", "user", "--transport", "http", "--callback-port", str(entry["callback_port"]),
                 entry["name"], entry["url"])


def login_argv(hands: HandsIdentity, name: str) -> list[str]:
    return _argv(hands, "login", "--no-browser", name)


def logout_argv(hands: HandsIdentity, name: str) -> list[str]:
    return _argv(hands, "logout", name)


def remove_argv(hands: HandsIdentity, name: str) -> list[str]:
    return _argv(hands, "remove", "--scope", "user", name)


def get_argv(hands: HandsIdentity, name: str) -> list[str]:
    return _argv(hands, "get", name)


def vendor_config_path(hands: HandsIdentity) -> str:
    return f"{hl.config_home(hands, VENDOR)}/.claude.json"


def _port_of(server: dict[str, Any]) -> str:
    oauth = server.get("oauth")
    port = oauth.get("callbackPort") if isinstance(oauth, dict) else None
    return str(port) if isinstance(port, int) and not isinstance(port, bool) else ""


def vendor_servers(hands: HandsIdentity, entity_dir: Path | str, *, held: bool = False,
                   sweep: bool = True) -> dict[str, dict[str, str]]:
    """The hands account's Claude ``mcpServers`` as ``{name: {"type", "url", "port"}}``, read as the hands user
    (``port`` is ``oauth.callbackPort`` as text, ``""`` when absent). Only those fields of each server are kept: a
    stdio server's env or an http server's headers are never held here. An absent config is ``{}``; one that cannot
    be read or parsed raises :class:`hl.HandsLoginError`."""
    path = vendor_config_path(hands)
    st = hl._lstat(hands, VENDOR, path, entity_dir=entity_dir, held=held, sweep=sweep)
    if st is None:
        return {}
    if not st[0].startswith("regular"):
        raise hl.HandsLoginError(f"{path} is a {st[0]}, not a file")
    r = hl.run_as_hands_bounded(hands, hl._as_hands_argv(hands, VENDOR, "/bin/cat", path), entity_dir=entity_dir,
                                held=held, what=f"cat {path}", sweep=sweep)
    if r.returncode != 0:
        raise hl.HandsLoginError(f"could not read {path} as {hands.user}: {hl._brief(hl._text(r))}")
    try:
        doc = json.loads(r.stdout.decode("utf-8"))
        servers = doc.get("mcpServers", {}) if isinstance(doc, dict) else None
        if not isinstance(servers, dict):
            raise ValueError("no mcpServers table")
        return {str(k): {"type": str(v.get("type", "")), "url": str(v.get("url", "")), "port": _port_of(v)}
                for k, v in servers.items() if isinstance(v, dict)}
    except (ValueError, UnicodeDecodeError) as exc:
        raise hl.HandsLoginError(f"{path} is not readable as the vendor's config ({type(exc).__name__})") from None


def _operator_home() -> str:
    return pwd.getpwuid(os.getuid()).pw_dir          # the account's database entry, never $HOME


def store_problem(hands: HandsIdentity) -> str | None:
    """Why the hands account's store is not one this module may act on, or None: the hands home must be a real home
    of another account than the operator's, with the vendor config home strictly inside it. A hands home that is
    the operator's own (or above or below it) would sign in to, or remove from, the operator's store."""
    try:
        op = os.path.realpath(_operator_home())
        home = os.path.realpath(hands.home)
        cfg = os.path.realpath(hl.config_home(hands, VENDOR))
    except (OSError, KeyError, ValueError):
        return "the operator's home or the hands home could not be resolved"
    if hands.uid == 0 or hands.user in ("", "root"):
        return "the hands account is root"
    # one home, two roles, in any spelling (casesweep row 12, key 1106-1): ``realpath`` keeps the letter case it is given.
    # DENY direction: ``pathid.maybe_inside`` both ways is "the same, below or above", and an unanswered stat refuses.
    if pathid.maybe_inside(op, home) or pathid.maybe_inside(home, op):
        return "the hands home is the operator's own home (or holds, or sits in, it): the operator's store is not touched"
    if not cfg.startswith(home.rstrip("/") + "/"):
        return "the vendor config home is not inside the hands home"
    return None


def _check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise HandsMcpRefused("a server name is letters, digits, hyphen and underscore, starting with a letter or digit")


# --- sign-in ---------------------------------------------------------------------------------------------------

def entry_for_signin(entity_dir: Path | str, cockpit_root: Path | str, name: str,
                     now: datetime | None = None) -> dict[str, Any]:
    """The live, remote, clean entry of ``name``, or :class:`HandsMcpRefused` saying which of those it is not. The
    entry's own text is never repeated in a refusal beyond the rule that failed."""
    _check_name(name)
    live, skipped = load_registry(cockpit_root, now)
    for e in live:
        if e["name"] == name:
            break
    else:
        known = _grants_named(cockpit_root, name)
        if not known:
            raise HandsMcpRefused(f"{name!r} is not registered for the hands account; nothing was changed")
        mine = [(gid, w) for gid, w in skipped if any(k["id"] == gid for k in known)]
        why = sorted({w for _gid, w in mine}) or ["not live"]
        if all(w in STOP_REASONS for w in why):   # a renew does not bring a stopped entry back, and no verb registers one
            cmds = ", ".join(f"`{_revoke_cmd(gid)}`" for gid, _w in mine)
            raise HandsMcpRefused(f"{name!r} is registered but stopped: {'; '.join(why)}; revoke it to clear it "
                                  f"({cmds}); no levain verb registers a hands entry yet. Nothing was changed")
        raise HandsMcpRefused(f"{name!r} is registered but not live ({', '.join(why)}); renew or re-register it. "
                              "Nothing was changed")
    if e["transport"] != "remote":
        raise HandsMcpRefused(f"{name!r} is a stdio server: it has no sign-in (its secrets live in the hands "
                              "account's environment). Nothing was changed")
    problems = registration_problems(e, home=_operator_home())
    if problems:
        raise HandsMcpRefused(f"{name!r} no longer passes the registration rules ({problems[0][:200]}); "
                              "nothing was changed")
    return e


def signin(hands: HandsIdentity, entry: dict[str, Any], entity_dir: Path | str, *, emit=None,
           timeout: float | None = None) -> str:
    """Sign ``entry`` in as the hands user. Returns the vendor's one-line view of the server afterwards. Raises
    :class:`HandsMcpRefused` before anything changed, :class:`HandsMcpError` (or :class:`hl.HandsLoginError`) after."""
    name = entry["name"]
    bad = store_problem(hands)
    if bad:
        raise HandsMcpRefused(f"{bad}; nothing was changed")
    refusal = hl.login_refusal()
    if refusal:
        raise HandsMcpRefused(refusal)
    try:
        with hl.hands_lock(entity_dir, "an MCP sign-in"):
            try:
                hl.ensure_config_home(hands, VENDOR, entity_dir=entity_dir)
                held = vendor_servers(hands, entity_dir, held=True)
            except hl.HandsLoginError as exc:
                raise HandsMcpRefused(f"{exc}; the sign-in was not started") from None
            ours = read_record(entity_dir).get(name)
            if name in held and ours is None:
                raise HandsMcpRefused(f"the hands account already holds a server named {name!r} that sign-in did not "
                                      "add; remove it by hand first. Nothing was changed")
            if name in held and (held[name]["url"] != entry["url"] or (ours or {}).get("url") != entry["url"]
                                 or held[name].get("port") != str(entry["callback_port"])):
                raise HandsMcpRefused(f"the hands account's {name!r} points elsewhere than its registered entry (url "
                                      f"or callback port); run `levain hands mcp remove {name}` first. Nothing was changed")
            if name not in held:
                _write_record(entity_dir, name, {"entry_id": entry["id"], "url": entry["url"], "at": _stamp(),
                                                 "outcome": "adding"})
                r = hl.run_as_hands_bounded(hands, add_argv(hands, entry), entity_dir=entity_dir, held=True,
                                            what=f"{VENDOR} mcp add")
                if r.returncode != 0:
                    _write_record(entity_dir, name, None)
                    raise HandsMcpError(f"claude mcp add exited {r.returncode}: {hl._brief(hl._text(r))}")
            _write_record(entity_dir, name, {"entry_id": entry["id"], "url": entry["url"], "at": _stamp(),
                                             "outcome": "added"})
            failed = {"entry_id": entry["id"], "url": entry["url"], "outcome": "failed"}
            try:
                res = hl._run_login(hands, VENDOR, entity_dir, emit or hl._write_out,
                                    hl.LOGIN_TIMEOUT if timeout is None else timeout, argv=login_argv(hands, name))
            except BaseException:                      # a timeout, a fault or an interrupt: the record says failed
                try:
                    _write_record(entity_dir, name, {**failed, "at": _stamp()})
                except HandsMcpError:
                    pass                               # the login's own failure is the one raised
                raise
            if res.returncode != 0:
                _write_record(entity_dir, name, {**failed, "at": _stamp()})
                raise HandsMcpError(f"the login exited {res.returncode}")
            g = hl.run_as_hands_bounded(hands, get_argv(hands, name), entity_dir=entity_dir, held=True,
                                        what=f"{VENDOR} mcp get")
            view = hl._brief(hl._text(g)) or f"exit {g.returncode}"
            _write_record(entity_dir, name, {"entry_id": entry["id"], "url": entry["url"], "at": _stamp(),
                                             "outcome": SIGNED_IN})
            return view
    except hl.HandsRefused as exc:
        raise HandsMcpRefused(str(exc)) from None


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname or "the provider"
    except ValueError:
        return "the provider"


# --- remove ----------------------------------------------------------------------------------------------------

_NOT_FOUND = re.compile(r'No MCP server named', re.I)


def remove(hands: HandsIdentity, name: str, entity_dir: Path | str) -> tuple[str, str]:
    """Take ``name`` out of the hands account: its login (``claude mcp logout``), then the server, then confirm it is
    gone. Only a server in the record is touched. Returns ``("removed", url)`` or ``("nothing", "")``."""
    _check_name(name)
    bad = store_problem(hands)
    if bad:
        raise HandsMcpRefused(f"{bad}; nothing was changed")
    try:
        with hl.hands_lock(entity_dir, "an MCP remove"):
            ours = read_record(entity_dir).get(name)
            try:
                held = vendor_servers(hands, entity_dir, held=True)
            except hl.HandsLoginError as exc:
                raise HandsMcpRefused(f"cannot read what the hands account holds, so nothing was changed: {exc}") from None
            if ours is None:
                if name in held:
                    raise HandsMcpRefused(f"the hands account holds a server named {name!r} that `levain hands mcp "
                                          "signin` did not add; it is not removed here. Nothing was changed")
                return "nothing", ""
            left = hl.hands_live.sweep_hands_user(hands.user, uid=hands.uid)
            if left:
                raise HandsMcpRefused(f"the hands account's processes were not stopped, so nothing was removed: {left}")
            r = hl.run_as_hands_bounded(hands, logout_argv(hands, name), entity_dir=entity_dir, held=True,
                                        what=f"{VENDOR} mcp logout")
            if r.returncode != 0 and not _NOT_FOUND.search(hl._text(r)):
                raise HandsMcpError(f"claude mcp logout exited {r.returncode}: {hl._brief(hl._text(r))}; the server "
                                    "was left in place so its login can be cleared again")
            if name in held:
                r2 = hl.run_as_hands_bounded(hands, remove_argv(hands, name), entity_dir=entity_dir, held=True,
                                             what=f"{VENDOR} mcp remove")
                if r2.returncode != 0 and not _NOT_FOUND.search(hl._text(r2)):
                    raise HandsMcpError(f"claude mcp remove exited {r2.returncode}: {hl._brief(hl._text(r2))}")
            after = vendor_servers(hands, entity_dir, held=True)
            if name in after:
                raise HandsMcpError(f"{name!r} is still in the hands account's config after the remove")
            _write_record(entity_dir, name, None)
            return "removed", str(ours.get("url", ""))
    except hl.HandsRefused as exc:
        raise HandsMcpRefused(str(exc)) from None


# --- CLI -------------------------------------------------------------------------------------------------------

def _hands_or_refuse(entity_dir: Path | str) -> HandsIdentity:
    try:
        return hl.load_hands_identity(entity_dir)
    except hl.HandsRefused as exc:
        raise HandsMcpRefused(str(exc)) from None


def cmd_signin(entity_dir: Path | str, name: str, cockpit_root: Path | str | None = None) -> int:
    root = cockpit_root if cockpit_root is not None else cockpit_root_of(entity_dir)
    try:
        refusal = hl.login_refusal()
        if refusal:
            raise HandsMcpRefused(refusal)
        hands = _hands_or_refuse(entity_dir)
        entry = entry_for_signin(entity_dir, root, name)
        print(f"Signing the hands account ({hands.user}) in to {name}. {SIGNIN_NOTICE}")
        view = signin(hands, entry, entity_dir)
    except HandsMcpRefused as exc:
        print(f"hands mcp signin: {exc}")
        return 1
    except (HandsMcpError, hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands mcp signin: {exc}")
        return 1
    print(f"hands mcp {name}: the login exited 0; the vendor's own view: {view}")
    return 0


def cmd_remove(entity_dir: Path | str, name: str) -> int:
    try:
        hands = _hands_or_refuse(entity_dir)
        how, url = remove(hands, name, entity_dir)
    except HandsMcpRefused as exc:
        print(f"hands mcp remove: {exc}")
        return 1
    except (HandsMcpError, hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands mcp remove: {exc}")
        return 1
    if how == "nothing":
        print(f"hands mcp {name}: not in the hands account, nothing changed")
        return 0
    print(f"hands mcp {name}: removed")
    print(REMOVE_NOTICE.format(host=_host(url)))
    return 0


def cmd_list(entity_dir: Path | str, cockpit_root: Path | str | None = None) -> int:
    rows, rc = status_rows(entity_dir, cockpit_root)
    for _ok, line in rows:
        print(line)
    print(SIGNIN_NOTICE)
    return rc


# --- status (CLI list and the doctor rows share it) -------------------------------------------------------------

def status_rows(entity_dir: Path | str, cockpit_root: Path | str | None = None) -> tuple[list[tuple[bool, str]], int]:
    """One ``(ok, line)`` per finding and the exit code. Every read that fails is its own line, never silence.
    Read-only, for both callers (``list`` and the doctor): its reads run with ``sweep=False``, so one that runs out
    kills only its own child and never the hands account's other processes (there is no sweeping variant)."""
    from levain.authority import HANDS_MCP_KIND
    from levain.consent.grants import stopped_line

    root = cockpit_root if cockpit_root is not None else cockpit_root_of(entity_dir)
    rows: list[tuple[bool, str]] = []
    try:
        hands = hl.load_hands_identity(entity_dir)
    except hl.HandsRefused as exc:
        return [(False, f"hands mcp: {exc}")], 1
    try:
        live, skipped = load_registry(root)
    except HandsMcpError as exc:
        live, skipped = [], []
        rows.append((False, f"hands mcp registry: unknown ({exc})"))
    try:
        record = read_record(entity_dir)
    except HandsMcpError as exc:
        record = {}
        rows.append((False, f"hands mcp record: unknown ({exc})"))
    try:
        held: dict[str, dict[str, str]] | None = vendor_servers(hands, entity_dir, sweep=False)
    except (hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        held = None
        rows.append((False, f"hands mcp config: unknown (the hands account's server list could not be read: {exc})"))
    live_names = {e["name"]: e for e in live}
    for gid, why in skipped:
        # a stopped entry is a failing row: it answers nothing and no renew brings it back, so it is not health
        if why in STOP_REASONS:
            rows.append((False, f"hands mcp entry {gid}: {stopped_line(why, HANDS_MCP_KIND)}: `{_revoke_cmd(gid)}`"))
        else:
            rows.append((True, f"hands mcp entry {gid}: not live ({why})"))
    if held is not None:
        for name in sorted(set(record) | set(held) | set(live_names)):
            rows.append(server_status(name, record.get(name), held.get(name), live_names.get(name)))
    return rows, 0 if all(ok for ok, _ in rows) else 1


def server_status(name: str, rec: dict[str, Any] | None, vend: dict[str, str] | None,
                  reg: dict[str, Any] | None) -> tuple[bool, str]:
    """The ONE comparison of a server's record (what sign-in did), the hands account's config (what the vendor
    holds) and its live registration: ``(ok, line)``. In step means all three agree on the entry (the record's
    ``entry_id`` is the live entry's id, so a revoked-then-re-registered name is not in step), the url and the
    callback port, AND the last sign-in's login exited 0; any other outcome (``adding``, ``added``, ``failed``) is
    a failing row. ``list`` and the doctor both read this, through :func:`status_rows`."""
    remove = f"`levain hands mcp remove {name}`"
    if rec is None and vend is not None:
        return False, f"hands mcp {name}: DRIFT, held by the hands account but not added by sign-in"
    if rec is None:
        return True, f"hands mcp {name}: registered, not signed in"
    if vend is None:
        return False, f"hands mcp {name}: DRIFT, recorded as added but missing from the hands account's config"
    if reg is None:
        return False, f"hands mcp {name}: DRIFT, signed in but no longer registered or live; {remove}"
    if rec.get("entry_id") != reg["id"]:
        return False, (f"hands mcp {name}: DRIFT, signed in under entry {rec.get('entry_id')} but the live registration "
                       f"is entry {reg['id']}; {remove}, then sign in again")
    if not (vend["url"] == rec.get("url") == reg["url"]):
        return False, f"hands mcp {name}: DRIFT, the hands account's url is not the one signed in or registered; {remove}"
    if vend.get("port") != str(reg["callback_port"]):
        return False, (f"hands mcp {name}: DRIFT, the hands account's callback port is not the registered "
                       f"{reg['callback_port']}; {remove}, then sign in again")
    last = f"{rec.get('outcome', '?')} {rec.get('at', '')}".rstrip()
    if rec.get("outcome") != SIGNED_IN:
        return False, f"hands mcp {name}: NOT SIGNED IN, the last sign-in ended {last}; sign in again or {remove}"
    return True, f"hands mcp {name}: in step with the registry; last: {last}"


def doctor_rows(install: Path) -> list[tuple[str, bool, str, str | None, bool]]:
    """``(name, ok, detail, hint, warn)`` for the doctor. Silent without a hands account. With one: the registry,
    record and config comparison (a fault reading any is a failing row), and the credential file's owner and mode
    (read by ``stat`` as the hands user; wider than 0600 fails, and so does a stat that cannot be told). No file
    says only that no login is held IN THAT FILE: a Keychain-held login is not checked, and the row warns when the
    record says a server signed in."""
    try:
        hands = hl.load_hands_identity(install)
    except Exception:  # noqa: BLE001 - no hands account (the confinement rows report a broken config)
        return []
    out: list[tuple[str, bool, str, str | None, bool]] = []
    rows, _ = status_rows(install)
    for ok, line in rows:
        out.append(("hands mcp", ok, line, None if ok else "`levain hands mcp list` shows the same read",
                    ok and "not live" in line))
    if not rows:
        out.append(("hands mcp", True, "no hands MCP server is registered or signed in", None, False))
    try:
        cred = hl.credential_state(hands, VENDOR, entity_dir=install, sweep=False)
    except Exception as exc:  # noqa: BLE001 - a state that cannot be told fails: an unchecked mode is not a 0600 one
        out.append(("hands mcp tokens", False, f"unknown: the credential file's owner and mode could not be read ({exc})",
                    "`levain hands status claude` asks the vendor", False))
        return out
    if cred is None:
        try:
            signed = sorted(n for n, r in read_record(install).items() if r.get("outcome") == SIGNED_IN)
        except HandsMcpError:
            signed = []                                # the record's own row already fails
        where = hl.credential_path(hands, VENDOR)
        if signed:
            out.append(("hands mcp tokens", True,
                        f"no credential file at {where}, but {', '.join(signed)} signed in: the login is not held in "
                        "that file; a login the vendor keeps in the macOS Keychain is not checked here",
                        "`levain hands mcp list`, or the vendor's own `claude mcp get <name>` as the hands account", True))
        else:
            out.append(("hands mcp tokens", True,
                        f"no credential file at {where}: no MCP login is held in a file; a login the vendor keeps in "
                        "the macOS Keychain is not checked here", None, False))
        return out
    owner, _, mode = cred.partition(" ")
    wide = False
    try:
        wide = bool(int(mode, 8) & 0o077)
    except ValueError:
        wide = True
    if owner != hands.user or wide:
        out.append(("hands mcp tokens", False, f"credential file owner {owner}, mode {mode}: wider than 600, or not the hands account's",
                    f"expected {hands.user} 600; `levain hands mcp remove <name>` and sign in again", False))
    else:
        out.append(("hands mcp tokens", True, f"credential file owner {owner}, mode {mode}", None, False))
    return out
