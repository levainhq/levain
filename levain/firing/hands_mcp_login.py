"""levain.firing.hands_mcp_login: `levain hands mcp signin|remove|list` and the doctor rows (levain 15, M3 + M4).

The hands account's OWN MCP logins. A server is registered by a signed authority-log entry (:mod:`levain.hands_mcp`,
M1); this module signs a REGISTERED, unexpired, remote server in as the hands account and takes it out again. The
rule is the vendor-login rule (:mod:`levain.firing.hands_login`): the vendor's own command runs AS the hands user
through the same ``env -i`` prefix, with the config home pinned inside the hands home; nothing here reads, copies or
points at the operator's ``~/.claude``, and no token is ever read into this process. Claude's half only for sign-in: Codex
servers are named-agent work. The launch files (M2, :func:`prepare_launch`) are rendered for both vendors.

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


class HandsMcpError(RuntimeError):
    """A hands MCP step failed after something may have changed, or a read could not be told."""


class HandsMcpRefused(HandsMcpError):
    """Refused before anything was changed."""


# --- the registry (the verified authority log) -----------------------------------------------------------------

def cockpit_root_of(entity_dir: Path | str) -> Path:
    return Path(entity_dir) / ".levain" / "cockpit"


def load_registry(cockpit_root: Path | str, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """``(live entries, skipped (id, why))`` from the cockpit's verified, anchored replay, or :class:`HandsMcpError`.
    A log that cannot be read is an error, never an empty registry."""
    from levain.agent_authority import AgentAuthority
    from levain.authority import NotInitialised
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
    return live_entries(state.policy.grants, now or datetime.now(timezone.utc))


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
        why = sorted({w for gid, w in skipped if any(k["id"] == gid for k in known)}) or ["not live"]
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


# --- the launch files (M2) -------------------------------------------------------------------------------------
#
# What a hands chat launch reads. The hands account's launch starts from the LIVE registry (the verified authority
# log, :func:`load_registry`), never from what an earlier launch wrote: an entry whose lease ran out or whose digest or
# command no longer holds is not rendered, so the file it was in loses it at the next call. Both renderers of
# :mod:`levain.hands_mcp` are called here and nowhere else, and everything is rendered BEFORE anything is written, so
# a refusal of the renderer (a digest, a reserved name, a repeated name or port, a command no longer trusted) leaves
# every file as it was.
#
# Claude: ``<hands home>/.claude/levain-mcp.json``, the whole file Levain's, written fresh at every call; the launch
# passes it as ``--strict-mcp-config --mcp-config <path>`` (:func:`claude_chat_argv`). Codex: the registry's tables go
# into ``<hands home>/.codex/config.toml`` between two marker lines, with the two file-store pins at the top level; any
# other content of that file is kept. Codex has no strict flag, so a server the file declares outside the block would
# launch beside the registry's: any such server refuses the call (L3 r1, codex HIGH), never merged and never deleted
# (deleting would destroy what the account holds; refusing tells the operator, who registers it or removes it).
# Every read and write is made BY the hands user through the config-home helper (:func:`hl.conf_read`,
# :func:`hl.conf_stage`, :func:`hl.conf_commit`): no link is followed, a new file has an unpredictable name and is
# created exclusively, and both files are staged before either is renamed in. The registry is read UNDER the hands
# lock, so a revocation lands either before the read or after the write, never between.
#
# The launcher is :func:`levain.firing.hands_chat.launch_hands_chat` (``levain hands launch``): it takes the entity's
# session and one-session locks, calls ``prepare_launch(..., held=True)`` and starts the argv only when it returned; no
# launch follows a failed or partial prepare. The ``prepare`` verb is the other caller.

CLAUDE_LAUNCH_FILE = "levain-mcp.json"
CODEX_BEGIN = "# >>> levain hands mcp (written at every launch; edit the registry, not this block) >>>"
CODEX_END = "# <<< levain hands mcp <<<"
_OAUTH_STORE_KEY = "mcp_oauth_credentials_store"


class LaunchFiles:
    """What :func:`prepare_launch` did: the two paths, the rendered server names, the entries left out and why, and
    whether each file's bytes changed."""

    def __init__(self, claude_path: str, codex_path: str, names: list[str], skipped: list[tuple[str, str]],
                 changed: dict[str, bool]) -> None:
        self.claude_path, self.codex_path, self.names, self.skipped, self.changed = (
            claude_path, codex_path, names, skipped, changed)


def claude_launch_path(hands: HandsIdentity) -> str:
    return f"{hl.config_home(hands, VENDOR)}/{CLAUDE_LAUNCH_FILE}"


def claude_chat_argv(hands: HandsIdentity, *extra: str) -> list[str]:
    """The hands account's Claude chat: the hands prefix with its config home, then ``claude`` reading ONLY the
    registry's servers from the launch file (``--strict-mcp-config``)."""
    return [*hl._launch_prefix(hands, VENDOR), "claude", "--strict-mcp-config", "--mcp-config",
            claude_launch_path(hands), *extra]


def codex_chat_argv(hands: HandsIdentity, *extra: str) -> list[str]:
    """The hands account's Codex chat: the hands prefix with ``CODEX_HOME``, whose ``config.toml`` carries the tables."""
    return [*hl._launch_prefix(hands, "codex"), "codex", *extra]


def render_launch(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """``(claude json body, codex tables)`` for ``entries``, both from the one renderer; the tables are empty when
    there is no entry. :class:`HandsMcpRefused` on any refusal of the renderer, before anything is written."""
    from levain import hands_mcp as hm
    try:
        body = hm.render_claude_json(entries)
        toml_text = hm.render_codex_toml(entries)
    except hm.HandsMcpRefused as exc:
        raise HandsMcpRefused(f"{exc}; nothing was changed") from None
    if not entries:
        return body, ""
    if not toml_text.startswith(hm.CODEX_STORE_LINE):                  # the renderer's own shape changed under us
        raise HandsMcpRefused("the Codex renderer no longer opens with its store pin; nothing was changed")
    return body, toml_text[len(hm.CODEX_STORE_LINE):].lstrip("\n")


def merge_codex_config(existing: str, tables: str, names: set[str]) -> str:
    """``existing`` (the hands account's ``config.toml``) with the managed block replaced by ``tables`` (dropped when
    empty), the file-store pins at the top level, everything else kept as it was. :class:`HandsMcpRefused` for: text
    that is not TOML, an unbalanced, repeated or reversed marker, a marker line inside a TOML string, a store key set
    to anything but ``"file"``, and ANY server the file declares outside the block (Codex would launch it beside the
    registry's). The result is parsed again and must hold exactly the rendered servers.

    A marker is a whole line equal to :data:`CODEX_BEGIN` or :data:`CODEX_END` (L3 r1: never a substring count), and
    it must be a comment: deleting the two marker lines alone must leave the parsed document as it was, which fails
    when they sit inside a multi-line string (whose content between them would otherwise be deleted)."""
    import tomllib
    from levain import hands_mcp as hm

    def parse(text: str) -> dict[str, Any]:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise HandsMcpRefused(f"the hands account's Codex config.toml is not valid TOML ({exc}); change it by "
                                  "hand. Nothing was changed") from None

    lines = existing.splitlines(keepends=True)
    begins = [i for i, ln in enumerate(lines) if ln.rstrip() == CODEX_BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.rstrip() == CODEX_END]
    if len(begins) != len(ends) or len(begins) > 1 or (begins and begins[0] > ends[0]):
        raise HandsMcpRefused("the hands account's Codex config.toml has an unbalanced or repeated Levain block; "
                              "change it by hand. Nothing was changed")
    rest = existing
    if begins:
        b, e = begins[0], ends[0]
        if parse(existing) != parse("".join(ln for i, ln in enumerate(lines) if i not in (b, e))):
            raise HandsMcpRefused("a Levain marker line of the hands account's Codex config.toml sits inside a TOML "
                                  "string; change it by hand. Nothing was changed")
        rest = "".join(lines[:b] + lines[e + 1:])
    doc = parse(rest)
    for key in (hl.CODEX_STORE_KEY, _OAUTH_STORE_KEY):
        if key in doc and doc[key] != "file":
            raise HandsMcpRefused(f'the hands account\'s Codex config.toml sets {key} to something other than "file"; '
                                  "change it by hand. Nothing was changed")
    foreign = doc.get("mcp_servers", {})
    if not isinstance(foreign, dict):
        raise HandsMcpRefused("the hands account's Codex config.toml has an mcp_servers that is not a table; change it by hand")
    if foreign:                       # registry-only (L3 r1): any server outside the block, not only a rendered name
        raise HandsMcpRefused(f"the hands account's Codex config.toml already holds a server named {sorted(foreign)[0]!r} "
                              "that the registry did not put there, and a hands Codex launch would run it; register it "
                              "or remove it by hand first. Nothing was changed")
    head = ""
    if hl.CODEX_STORE_KEY not in doc:
        head += hl.CODEX_STORE_LINE
    if tables and _OAUTH_STORE_KEY not in doc:
        head += hm.CODEX_STORE_LINE + "\n"
    out = head + rest
    if tables:
        out = out.rstrip("\n") + ("\n\n" if out.strip() else "") + f"{CODEX_BEGIN}\n{tables.rstrip(chr(10))}\n{CODEX_END}\n"
    try:
        after = tomllib.loads(out)
    except tomllib.TOMLDecodeError as exc:                              # not reachable from valid inputs; a refusal, not a write
        raise HandsMcpRefused(f"the merged Codex config.toml does not parse ({exc}); nothing was changed") from None
    if set(after.get("mcp_servers", {})) != names:
        raise HandsMcpRefused("the merged Codex config.toml does not hold exactly the registry's servers; "
                              "nothing was changed")
    return out


def _preflight_dir(hands: HandsIdentity, vendor: str, d: str, entity_dir: Path | str) -> None:
    """``d`` absent, or a real directory of the hands uid; judged WITHOUT a change (L3 r1: both config homes are
    judged before either is made or chmodded, and before any file in them is read)."""
    st = hl._lstat(hands, vendor, d, entity_dir=entity_dir, held=True)
    if st is None:
        return
    kind, uid, owner, _ = st
    if kind != "directory":
        raise hl.HandsLoginError(f"{d} is a {kind}, not a directory; change it by hand")
    if uid != hands.uid:
        raise hl.HandsLoginError(f"{d} belongs to {owner}, not {hands.user}; change it by hand")


def _read_launch(hands: HandsIdentity, vendor: str, d: str, name: str, entity_dir: Path | str) -> tuple[str, str] | None:
    """``(mode, text)`` of the launch file ``d/name`` read as the hands user through the config-home helper (no link
    followed, the directory judged first), None when it is absent. A file of another owner refuses (L3 r1: an
    identical-bytes file of another account is not "unchanged"); a mode other than 600 is repaired by the caller."""
    got = hl.conf_read(hands, vendor, d, name, entity_dir=entity_dir)
    if got is None:
        return None
    uid, mode, text = got
    if uid != hands.uid:
        raise hl.HandsLoginError(f"{d}/{name} belongs to another account, not {hands.user}; change it by hand")
    return mode, text


def prepare_launch(hands: HandsIdentity, entity_dir: Path | str, cockpit_root: Path | str | None = None, *,
                   now: datetime | None = None, held: bool = False) -> LaunchFiles:
    """Write the hands account's two launch files from the live registry. ``held`` is for a launcher that already
    holds the entity's hands lock (a session open) and has not read the registry itself; otherwise the exclusive
    lock is taken here and a session in progress refuses it. Either way the registry is read under the lock.

    Raises :class:`HandsMcpRefused` before anything changed (the store, the lock, the registry, a renderer refusal,
    a config home or launch file that is not a plain one of the hands account, the merge) and :class:`HandsMcpError`
    once a change began; its message says which launch file, if any, was already replaced. Both files are staged
    before either is renamed in, so a failure while staging changes neither; a failure (or a death) between the two
    renames leaves the first new and the second as it was, which the next call rewrites from the registry. No launch
    follows a call that did not return."""
    root = cockpit_root if cockpit_root is not None else cockpit_root_of(entity_dir)
    bad = store_problem(hands)
    if bad:
        raise HandsMcpRefused(f"{bad}; nothing was changed")
    claude_dir, codex_dir = hl.config_home(hands, "claude"), hl.config_home(hands, "codex")
    claude_path, codex_path = claude_launch_path(hands), hl.codex_config_path(hands)
    changed = {"claude": False, "codex": False}
    out: dict[str, Any] = {}

    def work() -> None:
        # the registry under the lock (L3 r1): a revocation lands before this read or after the write, never between
        try:
            live, skipped = load_registry(root, now)
        except (HandsMcpError, OSError) as exc:        # an unreadable log is no registry: nothing is rendered over it
            raise HandsMcpRefused(f"{exc}; nothing was changed") from None
        claude_body, tables = render_launch(live)
        names = {e["name"] for e in live}
        out.update(names=sorted(names), skipped=skipped)
        try:
            # judge everything first, changing nothing: both config homes, then both files through the helper (which
            # follows no link), then the merge; only then is a directory made or a file staged
            for vendor, d in (("claude", claude_dir), ("codex", codex_dir)):
                _preflight_dir(hands, vendor, d, entity_dir)
            old_claude = _read_launch(hands, "claude", claude_dir, CLAUDE_LAUNCH_FILE, entity_dir)
            old_codex = _read_launch(hands, "codex", codex_dir, "config.toml", entity_dir)
        except (hl.HandsLoginError, OSError) as exc:
            raise HandsMcpRefused(f"{exc}; nothing was changed") from None
        new_codex = merge_codex_config(old_codex[1] if old_codex else "", tables, names)
        todo = [(vendor, d, name, body) for vendor, d, name, old, body in (
            ("claude", claude_dir, CLAUDE_LAUNCH_FILE, old_claude, claude_body),
            ("codex", codex_dir, "config.toml", old_codex, new_codex))
            if old is None or old != ("600", body)]            # new bytes, or a mode to repair
        staged: list[tuple[str, str, str, str]] = []

        def drop(rest: list[tuple[str, str, str, str]]) -> None:
            for vendor, d, name, t in rest:
                hl.conf_drop(hands, vendor, d, name, t, entity_dir=entity_dir)

        try:
            for vendor, d, name, body in todo:
                hl._ensure_dir(hands, vendor, d, entity_dir)
                staged.append((vendor, d, name, hl.conf_stage(hands, vendor, d, name, body, entity_dir=entity_dir)))
        except (hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
            drop(staged)
            raise HandsMcpError(f"{exc}; neither launch file was changed") from None
        for i, (vendor, d, name, t) in enumerate(staged):
            try:
                hl.conf_commit(hands, vendor, d, name, t, entity_dir=entity_dir)
            except (hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
                drop(staged[i:])
                done = [f"{dd}/{nn}" for _, dd, nn, _ in staged[:i]]
                raise HandsMcpError(f"{exc}; " + (f"{done[0]} was already replaced, the other was not (the next "
                                                  "prepare writes both from the registry)" if done
                                                  else "neither launch file was changed")) from None
            changed[vendor] = True

    try:
        if held:
            work()
        else:
            with hl.hands_lock(entity_dir, "writing the MCP launch files"):
                work()
    except hl.HandsRefused as exc:
        raise HandsMcpRefused(str(exc)) from None
    except OSError as exc:                              # the lock itself (work() reports its own)
        raise HandsMcpRefused(f"the hands lock could not be taken ({exc}); nothing was changed") from None
    return LaunchFiles(claude_path, codex_path, out["names"], out["skipped"], changed)


def cmd_prepare(entity_dir: Path | str, cockpit_root: Path | str | None = None) -> int:
    try:
        hands = _hands_or_refuse(entity_dir)
        res = prepare_launch(hands, entity_dir, cockpit_root)
    except HandsMcpRefused as exc:
        print(f"hands mcp prepare: {exc}")
        return 1
    except (HandsMcpError, hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        print(f"hands mcp prepare: {exc}")
        return 1
    for gid, why in res.skipped:
        print(f"hands mcp entry {gid}: not rendered ({why})")
    print(f"hands mcp: {len(res.names)} server(s) rendered for {hands.user}"
          + (f" ({', '.join(res.names)})" if res.names else ""))
    print(f"  claude: {res.claude_path} ({'written' if res.changed['claude'] else 'unchanged'})")
    print(f"  codex:  {res.codex_path} ({'written' if res.changed['codex'] else 'unchanged'})")
    return 0
