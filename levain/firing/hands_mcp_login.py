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

The doctor rows read the vendor config's ``mcpServers`` names and urls as the hands user (never a token: the
credential file is stat'ed for owner and mode only) and compare them with the record and the registry.

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

from levain.firing import hands_login as hl
from levain.firing.confinement import HandsIdentity
from levain.hands_mcp import registration_problems

VENDOR = "claude"
RECORD_FILE = "hands-mcp.json"
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
    :class:`HandsMcpError`: an unreadable record is not an empty one."""
    p = _record_path(entity_dir)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise HandsMcpError(f"{p} cannot be read ({type(exc).__name__}); nothing was changed") from None
    if not isinstance(data, dict):
        raise HandsMcpError(f"{p} is not a record; nothing was changed")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_record(entity_dir: Path | str, name: str, value: dict[str, Any] | None) -> None:
    """Set (or, with None, drop) one name, under a sidecar lock, by a rename. Raises :class:`HandsMcpError`."""
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
            tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True))
            os.replace(tmp, p)
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


def vendor_servers(hands: HandsIdentity, entity_dir: Path | str, *, held: bool = False,
                   sweep: bool = True) -> dict[str, dict[str, str]]:
    """The hands account's Claude ``mcpServers`` as ``{name: {"type", "url"}}``, read as the hands user. Only those
    two fields of each server are kept: a stdio server's env or an http server's headers are never held here. An
    absent config is ``{}``; one that cannot be read or parsed raises :class:`hl.HandsLoginError`."""
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
        return {str(k): {"type": str(v.get("type", "")), "url": str(v.get("url", ""))}
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
    if home == op or home.startswith(op.rstrip("/") + "/") or op.startswith(home.rstrip("/") + "/"):
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
            if name in held and (held[name]["url"] != entry["url"] or (ours or {}).get("url") != entry["url"]):
                raise HandsMcpRefused(f"the hands account's {name!r} points elsewhere than its registered entry; "
                                      f"run `levain hands mcp remove {name}` first. Nothing was changed")
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
            res = hl._run_login(hands, VENDOR, entity_dir, emit or hl._write_out,
                                hl.LOGIN_TIMEOUT if timeout is None else timeout, argv=login_argv(hands, name))
            if res.returncode != 0:
                _write_record(entity_dir, name, {"entry_id": entry["id"], "url": entry["url"], "at": _stamp(),
                                                 "outcome": "failed"})
                raise HandsMcpError(f"the login exited {res.returncode}")
            g = hl.run_as_hands_bounded(hands, get_argv(hands, name), entity_dir=entity_dir, held=True,
                                        what=f"{VENDOR} mcp get")
            view = hl._brief(hl._text(g)) or f"exit {g.returncode}"
            _write_record(entity_dir, name, {"entry_id": entry["id"], "url": entry["url"], "at": _stamp(),
                                             "outcome": "login-exited-0"})
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

def status_rows(entity_dir: Path | str, cockpit_root: Path | str | None = None,
                *, sweep: bool = True) -> tuple[list[tuple[bool, str]], int]:
    """One ``(ok, line)`` per finding and the exit code. Every read that fails is its own line, never silence."""
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
        held: dict[str, dict[str, str]] | None = vendor_servers(hands, entity_dir, sweep=sweep)
    except (hl.HandsLoginError, OSError, subprocess.SubprocessError) as exc:
        held = None
        rows.append((False, f"hands mcp config: unknown (the hands account's server list could not be read: {exc})"))
    live_names = {e["name"]: e for e in live}
    for gid, why in skipped:
        rows.append((True, f"hands mcp entry {gid}: not live ({why})"))
    if held is not None:
        for name in sorted(set(record) | set(held) | set(live_names)):
            rec, vend, reg = record.get(name), held.get(name), live_names.get(name)
            if vend is not None and rec is None:
                rows.append((False, f"hands mcp {name}: DRIFT, held by the hands account but not added by sign-in"))
            elif rec is not None and vend is None:
                rows.append((False, f"hands mcp {name}: DRIFT, recorded as added but missing from the hands account's config"))
            elif rec is not None and reg is None:
                rows.append((False, f"hands mcp {name}: DRIFT, signed in but no longer registered or live; "
                                    f"`levain hands mcp remove {name}`"))
            elif rec is not None and vend is not None and vend["url"] != rec.get("url"):
                rows.append((False, f"hands mcp {name}: DRIFT, the hands account's url is not the one signed in"))
            elif rec is not None:
                rows.append((True, f"hands mcp {name}: in step with the registry; last: {rec.get('outcome', '?')} {rec.get('at', '')}".rstrip()))
            else:
                rows.append((True, f"hands mcp {name}: registered, not signed in"))
    return rows, 0 if all(ok for ok, _ in rows) else 1


def doctor_rows(install: Path) -> list[tuple[str, bool, str, str | None, bool]]:
    """``(name, ok, detail, hint, warn)`` for the doctor. Silent without a hands account. With one: the registry,
    record and config comparison (a fault reading any is a failing row), and the credential file's owner and mode
    (read by ``stat`` as the hands user; wider than 0600 fails)."""
    try:
        hands = hl.load_hands_identity(install)
    except Exception:  # noqa: BLE001 - no hands account (the confinement rows report a broken config)
        return []
    out: list[tuple[str, bool, str, str | None, bool]] = []
    rows, _ = status_rows(install, sweep=False)
    for ok, line in rows:
        out.append(("hands mcp", ok, line, None if ok else "`levain hands mcp list` shows the same read",
                    ok and "not live" in line))
    if not rows:
        out.append(("hands mcp", True, "no hands MCP server is registered or signed in", None, False))
    try:
        cred = hl.credential_state(hands, VENDOR, entity_dir=install, sweep=False)
    except Exception as exc:  # noqa: BLE001
        out.append(("hands mcp tokens", True, f"unknown: the credential file could not be read ({exc})",
                    "`levain hands status claude` asks the vendor", True))
        return out
    if cred is None:
        out.append(("hands mcp tokens", True, "no credential file (no MCP login is held)", None, False))
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
