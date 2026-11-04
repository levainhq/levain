"""levain.agents.session_wire — P7-1 S2: the production factory for :func:`levain.agents.session.make_run`.

``fire_factory(store, occurrence_id, launch_epoch)`` builds :func:`levain.agents.session.run_session`'s keyword
arguments for one session occurrence, from the store and ONE install-fact source: ``<store dir>/session_install.json``
(:func:`read_install`, :func:`write_install`). Nothing else supplies a launcher.

* ``sup``: a ``Supervisor`` over the install's authority log (``cockpit_root``) and the agent ``RunJournal``; the occurrence's run
  was admitted by the launch, so the factory admits nothing.
* ``role``: the role the occurrence's signed spec pins (:func:`levain.agents.spec.occurrence_spec`), read from the
  store by hash. A refused spec or a pinned role that is not in the store raises (the run is not built;
  :func:`levain.agents.session.make_run` reads that as a run that did not start).
* ``exposure`` and ``max_exposure_usd_micros``: the open reservation's own terms (:func:`session.reserved_terms`), so
  no billing figure here is invented; ``launch_refusal`` still checks them against the role and the launcher.
* ``recall``: P4's recall of the agent's episodes from the install's ``episodic_path``, only when the SIGNED spec's
  ``memory`` is ``stateful``; None for a stateless agent (the default) or when the install names no path.
* ``values``: the install's ``values`` (default none).
* ``launcher``: the install's ``launcher`` NAME resolved through :data:`LAUNCHERS`, a registry only
  :func:`register_launcher` writes; no name is :class:`levain.agents.session.UnwiredLauncher`, so a store that has
  not been given a launcher launches nothing and the run settles at zero as ``session_refused:launcher-not-wired``.
  A name that is not registered, a builder that does not return a :class:`SessionLauncher` on the S1 dispatch route,
  or the unwired launcher named explicitly as if it were wired, raises :class:`InstallRefused`: a launcher that did
  not come from the install source is refused, never replaced by the default.

The install file is read when the run is built, in the fire process, so it must be there and valid; a missing or
malformed one raises (the occurrence stays ``launched`` for the reaper and lapse recovery, as a dream does).
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.agents import session as _session
from levain.agents.store import AgentStore

__all__ = ["INSTALL_FILE", "INSTALL_SCHEMA", "InstallRefused", "SessionInstall", "LAUNCHERS", "register_launcher",
           "read_install", "write_install", "resolve_launcher", "fire_factory"]

INSTALL_FILE = "session_install.json"
INSTALL_SCHEMA = 1


class InstallRefused(ValueError):
    """The session install facts are absent, unusable, or name a launcher that cannot be used."""


# name -> a builder ``(store) -> SessionLauncher``. Written only by register_launcher: S4's real launcher registers
# itself here; a test registers its stub. A name in the install file that is not here is refused.
LAUNCHERS: dict[str, Callable[[AgentStore], Any]] = {}


def register_launcher(name: str, builder: Callable[[AgentStore], Any]) -> None:
    if not isinstance(name, str) or not name.strip() or not callable(builder):
        raise InstallRefused("a launcher is registered under a non-empty name with a callable builder")
    if name == "unwired":
        raise InstallRefused("'unwired' is the default and cannot be registered")
    LAUNCHERS[name] = builder


@dataclass(frozen=True)
class SessionInstall:
    cockpit_root: Path
    launcher: str | None
    episodic_path: Path | None
    values: Mapping[str, Any]


def _abs(doc: Mapping[str, Any], key: str, path: Path, *, kind: str) -> Path:
    v = doc.get(key)
    if not isinstance(v, str) or not v.strip() or not Path(v).expanduser().is_absolute():
        raise InstallRefused(f"{path}: {key} {v!r} is not an absolute path")
    p = Path(v).expanduser()
    if kind == "dir" and not p.is_dir():
        raise InstallRefused(f"{path}: {key} {str(p)!r} is not a directory")
    if kind == "file" and p.is_dir():       # a missing file is allowed (recall reads it as an empty memory)
        raise InstallRefused(f"{path}: {key} {str(p)!r} is a directory, not a file")
    return p


def read_install(agent_dir: Path | str) -> SessionInstall:
    """The install facts from ``<agent_dir>/session_install.json``. Absent, unreadable, another schema, an unknown key,
    a relative path or a missing cockpit directory raises :class:`InstallRefused`."""
    path = Path(agent_dir) / INSTALL_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise InstallRefused(f"the session install facts cannot be read from {path} ({type(exc).__name__})") from exc
    return _parse_install(doc, path)


def _parse_install(doc: Any, path: Path) -> SessionInstall:
    """The one check of an install document, for :func:`read_install` and, before it replaces anything,
    :func:`write_install`."""
    if not isinstance(doc, dict):
        raise InstallRefused(f"{path} is not a JSON object")
    if doc.get("schema") != INSTALL_SCHEMA:
        raise InstallRefused(f"{path} has schema {doc.get('schema')!r}, not {INSTALL_SCHEMA}")
    unknown = set(doc) - {"schema", "cockpit_root", "launcher", "episodic_path", "values"}
    if unknown:
        raise InstallRefused(f"{path}: unknown key {sorted(unknown)[0]!r}")
    root = _abs(doc, "cockpit_root", path, kind="dir")
    launcher = doc.get("launcher")
    if launcher is not None and (not isinstance(launcher, str) or not launcher.strip()):
        raise InstallRefused(f"{path}: launcher {launcher!r} is not a name")
    episodic = None if doc.get("episodic_path") is None else _abs(doc, "episodic_path", path, kind="file")
    values = doc.get("values", {})
    if not isinstance(values, dict):
        raise InstallRefused(f"{path}: values is not an object")
    return SessionInstall(root, launcher, episodic, values)


def write_install(agent_dir: Path | str, *, cockpit_root: Path | str, launcher: str | None = None,
                  episodic_path: Path | str | None = None, values: Mapping[str, Any] | None = None) -> Path:
    """Write the install file atomically: the new document is checked FIRST (as :func:`read_install` would read it
    back), then written to a temp in the directory (fsync, mode 0600) and renamed over the old one. A document that
    fails the check raises :class:`InstallRefused` and leaves the working install as it was."""
    d = Path(agent_dir)
    doc: dict[str, Any] = {"schema": INSTALL_SCHEMA, "cockpit_root": str(Path(cockpit_root).expanduser())}
    if launcher is not None:
        doc["launcher"] = launcher
    if episodic_path is not None:
        doc["episodic_path"] = str(Path(episodic_path).expanduser())
    if values:
        doc["values"] = dict(values)
    text = json.dumps(doc, sort_keys=True)
    _parse_install(json.loads(text), d / INSTALL_FILE)     # the round trip: what a read will see, before any write
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".session_install.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, d / INSTALL_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return d / INSTALL_FILE


def resolve_launcher(store: AgentStore, install: SessionInstall) -> Any:
    """The launcher the install names, or the unwired default when it names none. Refuses (raises) one that is not
    registered or does not satisfy the seam."""
    name = install.launcher
    if name is None:
        return _session.UnwiredLauncher()
    builder = LAUNCHERS.get(name)
    if builder is None:
        raise InstallRefused(f"the install names launcher {name!r}, which is not registered")
    launcher = builder(store)
    if isinstance(launcher, _session.UnwiredLauncher) or not isinstance(launcher, _session.SessionLauncher):
        raise InstallRefused(f"launcher {name!r} did not build a session launcher")
    if getattr(launcher, "route", None) != _session.S1_DISPATCH:
        raise InstallRefused(f"launcher {name!r} is not on the S1 dispatch route")
    return launcher


def _pinned_role(store: AgentStore, role_hash: str | None) -> Mapping[str, Any]:
    from levain.agents import role as _role
    found = _role.role_by_hash(store, role_hash) if isinstance(role_hash, str) else None
    if found is None:
        raise InstallRefused("the role the occurrence's signed spec pins is not in the store")
    return found


def fire_factory(store: AgentStore, occurrence_id: str, launch_epoch: int, *,
                 clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """The keyword arguments of :func:`levain.agents.session.run_session` for a session occurrence (see the module
    docstring). Nothing here starts a session. A failure to build raises; ``make_run`` reads that as a run that did
    not start. ``clock`` is a test seam (the supervisor's and the run's time)."""
    from levain.agent_authority import AgentAuthority
    from levain.agents import recall as _recall, spec as _spec, supervise
    from levain.autonomic.journal import RunJournal

    install = read_install(store.directory)
    launcher = resolve_launcher(store, install)
    reserved = _session.reserved_terms(store, occurrence_id)
    try:
        signed = _spec.occurrence_spec(store, occurrence_id)
    except _spec.SpecError as exc:
        raise InstallRefused(f"occurrence {occurrence_id}: its signed spec is refused ({exc})") from exc
    role = _pinned_role(store, signed["role"]["role_hash"])
    with store.read() as conn:
        row = conn.execute("SELECT agent_id FROM agent_occurrences"
                           " WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
    # memory is the SIGNED spec's: a stateless agent (the default) gets no retained episodes whatever the install names
    recall = None
    if signed["memory"] == "stateful" and install.episodic_path is not None:
        recall = _recall.recall_agent_episodes_at(install.episodic_path, row[0], now=datetime.now(timezone.utc))
    journal = RunJournal(store.directory)   # the run was admitted at the launch (controller._admit_run_in), not here
    sup = supervise.Supervisor(store, authority=AgentAuthority(install.cockpit_root), clock=clock, journal=journal)
    extra = {} if clock is None else {"clock": clock}   # a test seam: production passes none and both read real time
    return {**extra, "sup": sup, "role": role, "values": dict(install.values), "recall": recall, "launcher": launcher,
            "exposure": None if reserved is None else reserved.exposure,
            "max_exposure_usd_micros": 0 if reserved is None or reserved.usd_micros is None else reserved.usd_micros}
