"""levain.consent.default_grants: the default consent grant ``levain init`` offers (Q18, PLAN.md; design
project_memory/1019-q18/DESIGN.md §2).

One grant per agent the consent hook was wired for: that agent's workspace edit tools (and WebFetch for Claude Code),
the install root as workspace, 14 days, id ``cg-default-<agent>-<8 hex>`` so a later init finds its own. It is offered
only when the authority log the hook verifies against is initialised and an enrolled laptop-presence key sits in
levain's key directory (where ``levain cockpit key create`` puts it). At a terminal it prints the exact grant and asks
``[y/N]`` (default No); a yes runs ``levain consent grant create`` itself, with the argv it printed, so the refusals and
the passphrase or Touch ID prompt are that command's. Anywhere else it signs nothing and prints that command. A live
default grant for the same workspace is kept and reported, never offered again. Nothing here creates a key or a log:
key custody stays an explicit act.
"""
from __future__ import annotations

import os
import secrets
import shlex
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["EXPIRES", "MODES", "PACKS", "offer"]

MODES = ("ask", "no")
EXPIRES = "14d"
_EXPIRES_WORDS = "14 days after signing"
# agent -> (tools, postures), each sorted as the log stores them. Every tool must be on
# levain.authority.CONSENT_GRANTABLE_TOOLS for the agent (WebSearch is not, so it is not here: the log would refuse
# the whole grant), every posture on CONSENT_POSTURES.
PACKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "claude_code": (("Edit", "MultiEdit", "NotebookEdit", "WebFetch", "Write"), ("acceptEdits", "default")),
    "codex": (("apply_patch",), ("default",)),
}
_AGENTS = {"claude-code": "claude_code", "codex": "codex"}
_LABELS = {"claude_code": "Claude Code", "codex": "Codex"}


def _grant_argv(agent: str, workspace: str, data_root: Path, key: Path | str, gid: str) -> list[str]:
    tools, postures = PACKS[agent]
    argv = ["consent", "grant", "create", "--data-root", str(data_root), "--agent", agent]
    for t in tools:
        argv += ["--tool", t]
    argv += ["--workspace", workspace]
    for p in postures:
        argv += ["--posture", p]
    return argv + ["--expires", EXPIRES, "--id", gid, "--signing-key", str(key)]


def _hook_root(adapter: str) -> Path | None:
    """The cockpit the installed hook verifies grants against (its adapter settings), or None."""
    from levain.consent.adapters import claude_code, codex
    from levain.install import _consent_queue_dir     # the directory init wrote the settings into

    mod = claude_code if adapter == "claude-code" else codex
    root = mod.load_settings(_consent_queue_dir()).cockpit_data_root
    return Path(root) if root else None


def _resolve_key(rows: list) -> tuple[Path, str] | None:
    """``(key file, fingerprint)`` of an enrolled, live laptop-presence key whose public half is in levain's key
    directory: the private file beside it, else the ``.pub`` (a key an agent holds). None when there is none."""
    from levain.cockpit.custody import default_key_dir
    from levain.cockpit.keys import KeyError_, normalise_public_key

    live = {r.public_key: r.fingerprint for r in rows
            if r.key_class == "laptop-presence" and r.revoked_seq is None and r.compromised_since is None
            and r.public_key}
    d = default_key_dir()
    try:
        pubs = sorted(p for p in d.iterdir() if p.name.endswith(".pub") and p.is_file())
    except OSError:
        return None
    for pub in pubs:
        try:
            normal = normalise_public_key(pub.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, KeyError_):
            continue
        if normal in live:
            private = pub.with_name(pub.name[:-4])
            return (private if private.is_file() else pub), live[normal]
    return None


def offer(install: Path, adapter: str, *, mode: str = "ask", interactive: bool,
          emit: Callable[[str], None] = print, input_fn: Callable[[str], str] = input,
          clock: Callable[[], datetime] | None = None) -> None:
    """Offer ``adapter``'s default grant for ``install`` (see the module docstring). Never raises for a refusal:
    every outcome is one line or the grant-create command's own output."""
    if mode == "no" or adapter not in _AGENTS:
        return
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.keys import KeyError_, keys
    from levain.consent.grants import list_grants

    agent = _AGENTS[adapter]
    label = _LABELS[agent]
    tools, postures = PACKS[agent]
    workspace = os.path.realpath(install)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    covers = (f"{label} using {', '.join(tools)} in {workspace} under {', '.join(postures)}, without asking, for "
              f"14 days")
    root = _hook_root(adapter)
    if root is None:
        emit(f"  default grant: not offered: the consent settings name no cockpit, so no grant could answer the "
             f"hook (it would cover {covers})")
        return
    try:
        rows, _why = keys(root, now=now)
    except KeyError_ as exc:
        if "no cockpit journal" not in str(exc):
            emit(f"  default grant: not offered: the authority log at {root} is unusable ({exc})")
            return
        rows = []
    except Exception as exc:  # noqa: BLE001 - an unreadable log never fails init
        emit(f"  default grant: not offered: the authority log at {root} could not be read "
             f"({type(exc).__name__}: {exc})")
        return
    if not rows:
        emit(f"  default grant: not offered: it would cover {covers}; it needs an authority log first "
             f"(`levain cockpit ledger init`), then `levain init --force` offers it")
        return
    prefix = f"cg-default-{agent}-"
    try:
        grants = list_grants(root, now=now)
    except Exception as exc:  # noqa: BLE001
        emit(f"  default grant: not offered: the authority log at {root} could not be read "
             f"({type(exc).__name__}: {exc})")
        return
    stamp = utc_stamp(now)
    for g in grants:
        if g.id.startswith(prefix) and g.agent == agent and g.workspace == workspace and g.expires_at > stamp:
            emit(f"  default grant: {g.id} kept (expires {g.expires_at})")
            return
    gid = prefix + secrets.token_hex(4)
    found = _resolve_key(rows)
    if found is None:
        from levain.cockpit.custody import default_key_dir

        argv = _grant_argv(agent, workspace, root, "<an enrolled laptop-presence key>", gid)
        emit(f"  default grant: not offered: no enrolled laptop-presence key in {default_key_dir()}; to sign it "
             f"with another: levain {shlex.join(argv)}")
        return
    key, fp = found
    argv = _grant_argv(agent, workspace, root, key, gid)
    command = "levain " + shlex.join(argv)
    if not interactive:
        emit(f"  default grant: not signed (no terminal to ask at); to sign it: {command}")
        return
    emit("")
    emit(f"Default grant for {label}: levain answers these permission prompts for you, without asking:")
    emit(f"  tools:     {', '.join(tools)}")
    emit(f"  workspace: {workspace} (a file outside it still asks)")
    emit(f"  postures:  {', '.join(postures)}")
    emit(f"  expires:   {_EXPIRES_WORDS}")
    emit(f"  id:        {gid}")
    emit(f"  signed by: {key} ({fp})")
    emit("  Shell commands and edits of agent config or hooks always ask you.")
    emit(f"  It runs: {command}")
    try:
        answer = input_fn("Sign this default grant? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
        emit("")
    if answer.strip().lower() not in ("y", "yes"):
        emit(f"  default grant: not signed; to sign it later: {command}")
        return
    import levain.cli as cli

    if cli.main(argv) != 0:
        emit("  default grant: not signed (refused above)")
