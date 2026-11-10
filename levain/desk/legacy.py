"""levain.desk.legacy: the ONE module that spells the desk's old name, "coordinator", as code.

The desk was the "coordinator" until the rename. What was stored or put on a wire under that name keeps its spelling,
because SQLite cannot alter a CHECK constraint in place and a store is append-only: the persisted role value, the CHECK
literals, the quoted SQL text, the stored reply kind, the on-disk file and directory names and the signed broker verb.
Everything that names one of them does it through a constant here, so the old word appears as code in this file and
in the import shim ``levain/coordinator/__init__.py`` and nowhere else. A test greps the package for it.

``desk_home()`` is the one resolver of the desk's home directory: ``<levain home>/desk`` when it exists, else the old
``<levain home>/coordinator`` when that exists, else the new name (a new install).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROLE_DESK = "coordinator"                      # the persisted role / by / mode / author_kind value
SQL_ROLE_DESK = "'coordinator'"                # the same, as a quoted SQL literal inside schema text
JSON_ROLE_DESK = '"coordinator"'                # the same, as a JSON string inside a JSON text
CLI_ALIAS_LONG = "coordinator"                 # the long alias of `levain desk` (argv)
CLI_ALIAS_SHORT = "coord"                      # the short alias of `levain desk` (argv)
LEGACY_HOME_NAME = "coordinator"               # ~/.levain/coordinator
DESK_HOME_NAME = "desk"                        # ~/.levain/desk, the new install's name
LEGACY_SHADOW_NAME = "coordinator-shadow"      # ~/.levain/coordinator-shadow
KIND_DESK_REPLY = "coordinator_reply"          # replies.kind
KIND_REPLY_DESK = "reply_coordinator"          # the thread view's label key for it
SQL_KIND_DESK_REPLY = "'coordinator_reply'"
SQL_KIND_REPLY_DESK = "'reply_coordinator'"
FACT_DESK_WAKE = "coordinator-wake"            # the turn fact kind of a wake turn (written to the turn record)
VERB_DESK_DROP = "coordinator_drop"            # the signed broker effect verb
RECORD_NAME_WORKSPACES = "coordinator_workspaces.json"   # consent/<file>: the door's workspace record
RECORD_KEY_WORKSPACES = "coordinator_workspaces"
BAND_KEY_DESK_STOPPED = "levain-coordinator-stopped"
CODEX_RULE_NAME = "levain-coordinator.rules"   # <cwd>/.codex/rules/<file>
SESSION_PREFIX_DESK = "levain-coordinator-"    # the name an OLD desk launch's session carries (read, never written)
SESSION_ROLE_ALT = "(desk|coordinator|worker)"   # a session name is read under either word, written as "desk"
DRAFT_AGENT_PREFIX = "coordinator-draft:"      # the ledger entry's agent field for a promoted team draft
CAUSE_CODEX_REMOVED = "codex coordinator removed"   # a launch's persisted end cause (lease_events)


# the text the REMOVED Codex-rule writer put into ``<cwd>/.codex/rules/levain-coordinator.rules``: a retired launch removes a
# file only when it holds exactly this, so these two lines keep the old word byte for byte
CODEX_RULE_HEADER = "# Written by Levain for its coordinator (np-6843ca07 (a)). Codex runs exactly these commands without asking:\n"
CODEX_RULE_JUSTIFICATION = "Levain coordinator verbs: they grant nothing (a signed envelope still does)"


def by_word(word: str) -> str:
    """argparse ``type=`` for ``--by``: the desk's new name is accepted and mapped to the stored value; the old one passes."""
    return ROLE_DESK if word == "desk" else word


def role_word(role: str) -> str:
    """The word a message shows for a stored role value: the desk's stored spelling reads "desk"."""
    return "desk" if role == ROLE_DESK else role


def cause_word(cause: str) -> str:
    """A persisted end cause as a message shows it: the old word in a stored cause (``CAUSE_CODEX_REMOVED``) reads "desk".
    The stored text is never changed; only what is printed."""
    return re.sub(rf"\b{LEGACY_HOME_NAME}\b", "desk", cause)


def desk_home(levain_home: Path | str | None = None, *, create: bool = False) -> Path:
    """The desk's home directory: the new name if it exists, else the old one if it exists, else the new name.

    ``levain_home`` defaults to ``$LEVAIN_HOME`` else ``~/.levain``. Nothing is created unless ``create`` is true: a
    read-only verb that asks where the desk lives must not make the directory."""
    if levain_home is None:
        env = os.environ.get("LEVAIN_HOME")
        base = Path(env).expanduser() if env else Path.home() / ".levain"
    else:
        base = Path(levain_home).expanduser()
    new, old = base / DESK_HOME_NAME, base / LEGACY_HOME_NAME
    if new.exists():
        return new
    if old.exists():
        return old
    if create:
        new.mkdir(parents=True, exist_ok=True)
    return new
