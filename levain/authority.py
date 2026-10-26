"""levain.authority: the one signed authority log (trust-tiering DESIGN §3, §4).

Who may sign what, and the standing policy, are not stored anywhere: they are what a VERIFIED REPLAY of
``authority_log`` says. Each link is canonical JSON, hash-chained to its predecessor and signed (SSHSIG,
namespace :data:`NAMESPACE`) by a key the replay has enrolled. Every link is judged against the state at
its predecessor: its chain position, its signature, its signer's class and every op rule. A key revoked at
seq N signs nothing after N, and the links it signed before N stay valid (no retroactive voiding).

A policy link is classified by RECOMPUTE: the declared op is applied to the replayed policy and the
difference is classified on its own (:func:`classify`). A declared op that disagrees with the computed
delta is refused, and so is a link that changes nothing.

A link is written :data:`FORMAT` or, when it needs a rule an older replay does not have, :data:`FORMAT_V2` or
:data:`FORMAT_V3`; each link has one form (:func:`_link_format`), and the replay refuses the others.

The anchor (``anchor/authority.anchor`` in the store's data root, its directory excluded from Time Machine
so a restore of the data root cannot roll it back with the log) is written only after a commit, so it lags
and never leads, and only ever moves up. :func:`append` refuses a log behind it, and an empty log beside one; this
module reads the log without consulting it (:func:`snapshot`), so a caller that opens a store checks it
with :func:`check_anchor`.
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import sqlite3
import struct
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

# the link-signature verifier lives in levain.authority_sig, so root's route-map check verifies without importing this
# store; every name is re-exported here, and this module's own calls go through these module attributes
from levain.authority_sig import (_WEBAUTHN_SIG_TYPE, NAMESPACE, AuthorityError, VerifierUnavailable,
                                  _pipe_reading, _signers_line, _Wire, presence_missing, sshsig_public_key,
                                  verify_link_sig)
from levain.trust_anchor import DEFAULT_SSH_KEYGEN

__all__ = [
    "ANCHOR_DIR", "ANCHOR_NAME", "CONSENT_AGENTS", "CONSENT_GRANT_KIND", "CONSENT_MCP_FIELD_KINDS",
    "CONSENT_MCP_GRANT_KIND", "CONSENT_MCP_READ_ONLY_TOOLS",
    "CONSENT_GRANTABLE_TOOLS", "CONSENT_POSTURES", "CONSENT_SHELL_TOOLS",
    "CUSTODIES", "DISPLAY_PHONE", "EGRESS_KEY_KIND", "FIDO_ATTESTED", "FORMAT", "FORMAT_V2", "FORMAT_V3",
    "GRADUATION_CREATE_RANK",
    "GRADUATION_GRANT_MAX", "GRADUATION_KIND", "GRADUATION_OPT_IN_RANK", "GRADUATION_PHONE_KIND", "PASSKEY",
    "NAMESPACE", "RESET_FORMAT", "RESET_NAMESPACE", "SCHEMA",
    "Anchor", "AnchorError", "ForeignAnchor", "KEY_WORD", "AuthorityError", "AuthorityKey", "AuthorityPolicy", "AuthoritySnapshot",
    "HeadBelowCache", "NotInitialised", "PROCESS_CACHE", "ReplayCache", "ReplayError", "StaleHead", "StandingPolicy",
    "UnsignedLink", "VerifierUnavailable", "append", "advance_anchor", "anchor_data_root", "anchor_path",
    "check_anchor", "classify", "foreign_anchor",
    "consent_grant", "consent_grant_tool_problem", "consent_revocable", "consent_tool_shape_problem",
    "consent_resolved_workspace", "consent_mcp_declaration", "consent_mcp_input_fits", "consent_mcp_tool_problem",
    "consent_root_problem", "consent_tool_problem", "consent_workspace_problem", "create_schema", "custody_mismatch",
    "custody_problem", "egress_key_conflicts", "egress_key_host_problem", "egress_key_incarnation", "egress_key_live",
    "egress_key_record", "egress_key_repo_problem", "graduation_grant", "graduation_incarnation", "graduation_links",
    "graduation_opt_in", "is_passkey_material", "passkey_revocable",
    "genesis_policy", "inert_grant", "conn_data_root", "conn_init_hint", "init_hint", "log_head", "prepare", "read_anchor",
    "reset_anchor", "reset_hint", "reset_lock", "reset_message", "sign_and_append",
    "presence_missing", "quarantined", "signing_laptops", "snapshot", "sshsig_public_key", "UNSTATED",
    "verify_anchor_reset", "verify_link_sig",
]

FORMAT = "levain-authority/1"
# The format of a link that needs a rule a levain-authority/1 replay does not have (14b DESIGN_REV3 §7): a passkey's
# renew or create_grant, and a link that brings in a graduation opt-in. A link that brings in a guarded graduation
# policy is FORMAT_V3, and every other link is FORMAT. This replay takes all three and holds each link to the one form
# :func:`_link_format` gives it; an older replay stops at the first FORMAT_V2 link (its format check), so it never
# reads one as anything else.
FORMAT_V2 = "levain-authority/2"
# The format of a link that brings in a graduation policy carrying a ``guard`` (autogrant DESIGN I7, 1026-20 RULINGS
# R13-12), whoever signs it. A replay that knows only /1 and /2 stops at it (its format check) rather than read a
# policy whose guard it would not see; the guard is fixed with the policy's other terms, so no later link alters it.
FORMAT_V3 = "levain-authority/3"
RESET_FORMAT = "levain-authority-reset/1"
RESET_NAMESPACE = "levain-authority-reset"
ANCHOR_NAME = "authority.anchor"
ANCHOR_DIR = "anchor"
# The sticky Time Machine exclusion `tmutil addexclusion` writes: a binary plist of the string
# "com.apple.backupd", as `xattr -px` reads it back.
_TM_EXCLUDE_ATTR = b"com.apple.metadata:com_apple_backup_excludeItem"
_TM_EXCLUDE_VALUE = bytes.fromhex(
    "62706c69737430305f1011636f6d2e6170706c652e6261636b7570640800000000000001010000000000000001000000"
    "0000000000000000000000001c")
ANCHOR_FORMAT = "levain-authority-anchor/1"

SCHEMA = ("CREATE TABLE IF NOT EXISTS authority_log (seq INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, "
          "record TEXT NOT NULL, signer_fp TEXT NOT NULL, signature TEXT NOT NULL, hash TEXT NOT NULL UNIQUE)")

LAPTOP = "laptop-presence"
# Custody (rev 6 presence floor, DESIGN §3.2 items 1-3): every enrol record states how its key is held. It
# is a DECLARATION, signed by the enrolling key, never a proof: the verifier cannot tell a touch key from a
# no-touch one (a Secure Enclave key echoes the presence flag it is handed), so the touch rests wholly on
# how the key was made. The replay holds the declaration to the key types it can be true of.
UNSTATED = "unstated"
FIDO_ATTESTED = "fido-attested"
# A WebAuthn passkey (Q6b): an iCloud or Google passkey syncs to every device the account holds, so it is weaker
# custody than a key that never leaves one machine, and it is declared as itself. It answers for display-phone only.
PASSKEY = "passkey"
DISPLAY_PHONE = "display-phone"
CUSTODIES = ("touch-id", "passphrase", "tpm", "fido", "windows-hello", PASSKEY, FIDO_ATTESTED, UNSTATED)
_SK_KEY_TYPES = frozenset({"sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"})
# the custody table: the key types each stated custody admits (``unstated`` is not in it)
_CUSTODY_KEY_TYPES: Mapping[str, frozenset[str]] = MappingProxyType({
    "touch-id": _SK_KEY_TYPES, "fido": _SK_KEY_TYPES, "windows-hello": _SK_KEY_TYPES, FIDO_ATTESTED: _SK_KEY_TYPES,
    "tpm": frozenset({"ecdsa-sha2-nistp256"}),
    PASSKEY: frozenset({"sk-ecdsa-sha2-nistp256@openssh.com"}),     # WebAuthn ES256 is the one passkey curve
    "passphrase": frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256"}),
})
# The laptop-presence floor IS the custody table: every stated custody but ``passkey`` is accepted for the class, so
# the types it admits are the table's union (a carried legacy key with no stated custody stays inside it). The
# signers line never carries ``no-touch-required``, so an sk key's presence flag is still required.
_LAPTOP_KEY_TYPES = frozenset().union(*_CUSTODY_KEY_TYPES.values())


def custody_problem(key_class: str, custody: Any, key_type: str, *, public_key: str,
                    legacy: bool = False) -> str | None:
    """Why ``custody`` cannot be declared for a ``key_class`` key of ``key_type`` (None when it can): the
    one rule the replay enforces and the CLI checks before anything is signed. ``legacy``: the key is
    carried from a levain-cockpit/2 store (its record has ``legacy_enrolled_at``), the one laptop-presence
    key that may leave its custody ``unstated``. ``public_key`` (required, so no caller can skip the rule): a key
    whose material is a WebAuthn passkey's (:func:`is_passkey_material`) is declared ``passkey`` or not at all."""
    if not isinstance(custody, str) or custody not in CUSTODIES:
        return f"unknown custody {str(custody)[:40]!r}; one of {', '.join(CUSTODIES)}"
    if custody != PASSKEY and is_passkey_material(public_key):
        return (f"the key is a WebAuthn passkey (an {_PASSKEY_KEY_TYPE} key made on a web origin, not ssh:); "
                f"its custody is {PASSKEY}, never {custody}")
    return _custody_rule_problem(key_class, custody, key_type, legacy)


def _custody_rule_problem(key_class: str, custody: Any, key_type: str, legacy: bool) -> str | None:
    """Every rule of :func:`custody_problem` but the key-material one. Private: its one other caller is the replay's
    quarantine of an enrolment recorded before the door (:func:`_enrolled_key`), which holds every other rule."""
    if not isinstance(custody, str) or custody not in CUSTODIES:
        return f"unknown custody {str(custody)[:40]!r}; one of {', '.join(CUSTODIES)}"
    if custody == FIDO_ATTESTED:
        return (f"custody {FIDO_ATTESTED} is refused until attestation at enrol is built (it comes with the "
                "governed install); an unattested FIDO key is custody fido")
    if custody == PASSKEY and key_class != DISPLAY_PHONE:
        return f"custody {PASSKEY} is for a {DISPLAY_PHONE} key only (a synced passkey never signs T3 or an enrolment)"
    if custody == UNSTATED:
        if key_class != LAPTOP:
            return None
        if not legacy:
            return (f"a {LAPTOP} key must state its custody "
                    f"({', '.join(c for c in CUSTODIES if c not in (UNSTATED, FIDO_ATTESTED, PASSKEY))}); "
                    f"{UNSTATED} is for display-phone and presence keys and a carried legacy key")
        if key_type not in _LAPTOP_KEY_TYPES:
            return f"key type {key_type} is not eligible for {LAPTOP} (one of {', '.join(sorted(_LAPTOP_KEY_TYPES))})"
        return None
    admits = _CUSTODY_KEY_TYPES[custody]
    if key_type not in admits:
        return f"custody {custody} needs a key of type {', '.join(sorted(admits))}, not {key_type}"
    return None


_PASSKEY_KEY_TYPE = "sk-ecdsa-sha2-nistp256@openssh.com"


def is_passkey_material(public_key: object) -> bool:
    """Whether ``public_key``'s own material is a WebAuthn passkey's: an ``sk-ecdsa-sha2-nistp256@openssh.com``
    key whose application is a web rpId (``levain.consent.passkey.public_key_line`` puts it there), where an
    OpenSSH FIDO key's is ``ssh:...``. Anything else, or a key line that cannot be read, is not. Stdlib only."""
    parts = public_key.split() if isinstance(public_key, str) else []
    if len(parts) < 2 or parts[0] != _PASSKEY_KEY_TYPE:
        return False
    try:
        wire = _Wire(base64.b64decode(parts[1], validate=True))
        if wire.string() != _PASSKEY_KEY_TYPE.encode("ascii"):
            return False
        wire.string()                  # curve
        wire.string()                  # point
        application = wire.string()
        wire.end()
    except (ValueError, struct.error):
        return False
    return not application.startswith(b"ssh:")


def custody_mismatch(public_key: str, custody: object) -> str | None:
    """Why a key whose stored custody is ``custody`` may not have its signature checked as that custody, or None:
    the custody its material shows (:func:`is_passkey_material`) must be the stored one, passkey or not. Every
    levain verifier that picks a passkey's checks (user verification, the exact origin, what it may sign) by the
    stored custody runs this first. :func:`custody_problem` refuses such an enrolment at the door; this holds the
    same at every verification, for a log or snapshot from before that door, or one built past it."""
    derived = is_passkey_material(public_key)
    if derived == (custody == PASSKEY):
        return None
    return (f"the signing key's stored custody {str(custody)[:40]!r} is not the custody its key material shows "
            f"({'a passkey' if derived else 'not a passkey'})")


def _quarantinable(key_class: str, custody: Any, key_type: str, legacy: bool, public_key: str) -> bool:
    """Whether an enrolment refused only by the door's key-material rule (WebAuthn material at a custody but
    ``passkey``) and by no other rule is one the replay keeps, QUARANTINED, when it is already in the log (a log
    written before the door). It is never a new link's way in: :func:`_enrolled_key` asks only for history."""
    return (custody != PASSKEY and is_passkey_material(public_key)
            and _custody_rule_problem(key_class, custody, key_type, legacy) is None)


# Delta kinds the recompute classifier returns (§4.3).
NOOP, TIGHTEN, LOOSEN, RENEW, GRANT_REVOKE = "noop", "tighten", "loosen", "renew", "grant_revoke"
# the signer rank each delta needs: T2 = display-phone, T3 = laptop-presence, a revoke any enrolled key
_DELTA_RANK = {TIGHTEN: 2, RENEW: 2, LOOSEN: 3, GRANT_REVOKE: 1}
# what each declared policy op must compute to
_OP_DELTAS = {"raise": {TIGHTEN}, "revoke_grant": {GRANT_REVOKE}, "renew": {RENEW}, "loosen": {LOOSEN},
              "create_grant": {LOOSEN}, "timeouts": {TIGHTEN, LOOSEN}}

_BASE_FIELDS = frozenset({"format", "store_id", "seq", "at", "op"})
# per op: (required fields, optional fields), beyond the base
_OP_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "enrol": (frozenset({"fingerprint", "public_key", "class", "custody"}), frozenset({"legacy_enrolled_at"})),
    "revoke_key": (frozenset({"fingerprint"}), frozenset({"compromised_since"})),
    "raise": (frozenset({"verb", "tier"}), frozenset()),
    "loosen": (frozenset({"policy"}), frozenset()),
    "revoke_grant": (frozenset({"grant_id"}), frozenset()),
    "renew": (frozenset({"grant_id", "expires_at", "signed_at"}), frozenset()),
    "create_grant": (frozenset({"grant"}), frozenset()),
    "timeouts": (frozenset({"timeouts"}), frozenset()),
}
_GRANT_FIELDS = frozenset({"id", "entity", "remote_url", "deploy_key_sha256", "refspecs", "verbs", "expires_at"})
_GRANT_VERBS = frozenset({"push", "fetch"})
# A consent grant (Q4) answers an external agent's permission gate on the consent queue. A grant with no
# ``kind`` is an egress grant (the fields above, unchanged); a consent grant carries exactly these.
CONSENT_GRANT_KIND = "consent"
_CONSENT_GRANT_FIELDS = frozenset({"id", "kind", "agent", "tools", "workspace", "postures", "expires_at"})
# A consent-mcp grant is a consent grant that also DECLARES the input fields of MCP tools it names (``mcp_fields``:
# tool -> field -> kind, :data:`CONSENT_MCP_FIELD_KINDS`), so a declared tool off
# :data:`CONSENT_MCP_READ_ONLY_TOOLS` may be named (ruling #3 (b)). A kind of its own, never an optional field on
# ``consent``: a kind's field set stays closed, and a levain that does not know this kind holds it inert (0.11.5).
CONSENT_MCP_GRANT_KIND = "consent-mcp"
_CONSENT_MCP_GRANT_FIELDS = _CONSENT_GRANT_FIELDS | {"mcp_fields"}
CONSENT_AGENTS = ("claude_code", "codex")
# A setup record (CH-D2, design §3.8.5(o) item 4, "re-attach: the setup record"): the operator names ONCE, under the
# log's rule for a loosen (a laptop-presence key), a host whose requests from a chat launch of ``entity`` may carry a
# credential Levain holds. It names the credential's SOURCE (a keychain item name the service reads at attach time)
# and its ``account`` (the keychain item's account: a service alone does not pick one item, two may share it, so
# the read passes both and a record without an account is refused), never its value, which never enters a link.
# Fields: ``host`` (one plain-DNS name in its canonical form,
# ``levain.chat_egress.parse.canonical_host``; no wildcard, no IP literal), ``placement`` (``{"scheme": "bearer"}`` or
# ``{"scheme": "basic", "user": U}``, R15-9's closed set), ``repos`` (ALL the declared repo paths: the operator's
# own plus the workspace's remotes read once at setup; a launch reads only this list, K-8, item 4) and ``expires_at`` (at most 30 days past its link, the envelope an
# older levain holds an unknown kind to, so a log carrying one still replays there, inert). At most ONE LIVE record
# per (entity, canonical host) (R15-21-4): a second is refused at append (:func:`_egress_key_append_check`), and
# replacing a credential is one ``loosen`` link that removes the old record and adds the new (a supersession, a NEW
# id): a record's id never changes meaning, so a same-id change to anything but ``expires_at`` is refused at append.
# The launch builds its route snapshot from the live records (``levain.chat_egress.keys``), each bound to its
# :func:`egress_key_incarnation`; "live" is :func:`egress_key_live`, defined once. ``revoke_grant`` removes one (any
# enrolled key).
EGRESS_KEY_KIND = "egress-key"
_EGRESS_KEY_FIELDS = frozenset({"id", "kind", "entity", "host", "source", "account", "placement", "repos",
                                "expires_at"})
_EGRESS_KEY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# printable ASCII, no leading or trailing space, at most 255 characters
_EGRESS_KEY_TEXT = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,253}[\x21-\x7e])?")
# a basic-auth user id: printable, no space and no ':' (RFC 7617 section 2: the user-id cannot hold a colon)
_EGRESS_KEY_USER = re.compile(r"[\x21-\x39\x3b-\x7e]{1,255}")
_EGRESS_KEY_REPOS_MAX = 256
# Ruling 1: a shell command is never parsed, so a grant naming a shell tool would allow every command. Both agents
# name their shell tool Bash in a PermissionRequest (Claude Code; Codex 0.162.1's exec_command, measured in Q0 item
# 6, Q3 and 1017-shellnames). Codex 0.162.1 names the input it sends to a running terminal ``write_stdin`` (its
# ``chars`` field, to a terminal launched outside the sandbox: 1017-shellnames, where a grant naming it answered
# ``touch`` typed into /bin/sh). Claude Code's PowerShell tool (named so in the 2.1.302 binary, not run here) and
# the other shell names levain's PostToolUse hooks know (shell, exec_command, shell_command, local_shell) are too.
# A grant naming any of them is refused at append (:func:`consent_tool_problem`) and answers nothing at decision
# (``levain.consent.policy.grant_refusal``). The replay holds only Bash (:data:`_REPLAY_SHELL_TOOLS`), so a log
# that already holds a grant on another of them still replays. Compared case-insensitively, on the whole name and on
# an MCP tool's own name (mcp__shell__exec_command is exec_command: :func:`_consent_shell_name`).
CONSENT_SHELL_TOOLS = frozenset({"bash", "powershell", "write_stdin", "shell", "exec_command", "shell_command",
                                 "local_shell"})
_REPLAY_SHELL_TOOLS = frozenset({"bash"})
# The MCP tools a consent grant may name, by the tool's own name after ``mcp__<server>__``, each with the input its
# schema declares (field -> kind, and the fields it requires): the reference filesystem server's tools that declare
# ``readOnlyHint: true`` (github.com/modelcontextprotocol/servers, src/filesystem/server.ts, read 2026-10-17). MCP
# declares no path fields and its annotations are untrusted (spec 2025-06-18, server/tools), so a grant's workspace
# bound reads only the fields levain knows (``levain.consent.policy.MCP_PATH_FIELDS``): a grant on a tool that writes
# could answer a write anywhere, and one on another server's ``read_file`` taking its path in another field a read
# anywhere. An MCP tool off this list is refused at append and answers nothing at decision unless a consent-mcp
# grant declares its fields (:data:`CONSENT_MCP_GRANT_KIND`), and a request on a listed tool the grant does not
# declare whose input is not one that tool's schema accepts matches no grant: it goes to you. Kinds: "str", "int" (a JSON integer), "strs" (a list of strings), and a
# frozenset of the allowed string values.
_PATH, _COUNT, _STRS = "str", "int", "strs"
CONSENT_MCP_READ_ONLY_TOOLS: Mapping[str, tuple[Mapping[str, Any], frozenset[str]]] = MappingProxyType({
    "read_file": ({"path": _PATH, "head": _COUNT, "tail": _COUNT}, frozenset({"path"})),
    "read_text_file": ({"path": _PATH, "head": _COUNT, "tail": _COUNT}, frozenset({"path"})),
    "read_media_file": ({"path": _PATH}, frozenset({"path"})),
    "read_multiple_files": ({"paths": _STRS}, frozenset({"paths"})),
    "list_directory": ({"path": _PATH}, frozenset({"path"})),
    "list_directory_with_sizes": ({"path": _PATH, "sortBy": frozenset({"name", "size"})}, frozenset({"path"})),
    "directory_tree": ({"path": _PATH, "excludePatterns": _STRS}, frozenset({"path"})),
    "search_files": ({"path": _PATH, "pattern": _PATH, "excludePatterns": _STRS}, frozenset({"path", "pattern"})),
    "get_file_info": ({"path": _PATH}, frozenset({"path"})),
    "list_allowed_directories": ({}, frozenset()),
})


def consent_mcp_input_fits(tool: str, tool_input: Any) -> bool:
    """Whether ``tool_input`` is an input the schema of ``tool``'s entry in :data:`CONSENT_MCP_READ_ONLY_TOOLS`
    accepts: an object with every required field, no other field, and each value of its field's kind. ``False``
    for a tool not on the list."""
    entry = CONSENT_MCP_READ_ONLY_TOOLS.get(tool[len("mcp__"):].partition("__")[2]) \
        if isinstance(tool, str) and tool.startswith("mcp__") else None
    if entry is None or not isinstance(tool_input, Mapping):
        return False
    fields, required = entry
    if not required <= set(tool_input) or not set(tool_input) <= set(fields):
        return False
    for name, value in tool_input.items():
        kind = fields[name]
        if kind == _PATH:
            ok = isinstance(value, str)
        elif kind == _COUNT:
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == _STRS:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, str) and value in kind
        if not ok:
            return False
    return True


# The kinds a consent-mcp grant declares a field of: ``path`` (a string, a target path), ``paths`` (a non-empty list
# of strings, each a target path), ``str``, ``strs`` (a list of strings), ``int`` (a JSON integer, not a boolean),
# ``bool``. A declared tool is answered only for an input of declared fields, each value of its kind, every ``path``
# value and ``paths`` item inside the workspace (``levain.consent.policy.grant_refusal``).
CONSENT_MCP_FIELD_KINDS = frozenset({"path", "paths", "str", "strs", "int", "bool"})
_CONSENT_MCP_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_CONSENT_MCP_FIELDS_MAX = 32


def consent_mcp_declaration(grant: Any, tool: Any) -> Mapping[str, str] | None:
    """The fields a consent-mcp grant declares for the MCP tool ``tool`` (field -> kind), or ``None``: a grant of any
    other kind declares nothing, and neither does one whose declarations do not name ``tool``."""
    if not isinstance(grant, Mapping) or grant.get("kind") != CONSENT_MCP_GRANT_KIND or not isinstance(tool, str):
        return None
    declared = grant.get("mcp_fields")
    fields = declared.get(tool) if isinstance(declared, Mapping) else None
    return fields if isinstance(fields, Mapping) and fields else None


def _validate_mcp_fields(declared: Any, tools: Any, what: str) -> None:
    """``mcp_fields``: a non-empty mapping whose every key is an MCP tool the grant names, each declaring 1 to
    :data:`_CONSENT_MCP_FIELDS_MAX` fields, each named by :data:`_CONSENT_MCP_FIELD` with a kind in
    :data:`CONSENT_MCP_FIELD_KINDS`."""
    if not isinstance(declared, Mapping) or not declared:
        raise AuthorityError(f"{what}.mcp_fields must map the MCP tools it declares to their fields")
    for tool, fields in declared.items():
        if not isinstance(tool, str) or not tool.startswith("mcp__") or not _CONSENT_MCP_TOOL.fullmatch(tool) \
                or tool not in tools:
            raise AuthorityError(f"{what}.mcp_fields declares {str(tool)[:200]!r}, which is not an MCP tool the "
                                 "grant names")
        if not isinstance(fields, Mapping) or not 1 <= len(fields) <= _CONSENT_MCP_FIELDS_MAX:
            raise AuthorityError(f"{what}.mcp_fields[{tool[:200]!r}] must declare 1 to {_CONSENT_MCP_FIELDS_MAX} "
                                 "fields")
        for name, kind in fields.items():
            if not isinstance(name, str) or not _CONSENT_MCP_FIELD.fullmatch(name):
                raise AuthorityError(f"{what}.mcp_fields[{tool[:200]!r}] has the field name {str(name)[:80]!r} "
                                     "(letters, digits, _ . -; at most 64)")
            if not isinstance(kind, str) or kind not in CONSENT_MCP_FIELD_KINDS:
                raise AuthorityError(f"{what}.mcp_fields[{tool[:200]!r}][{name!r}] must be one of "
                                     f"{', '.join(sorted(CONSENT_MCP_FIELD_KINDS))}")


# The posture names each agent's PermissionRequest event carries (``permission_mode``). Claude Code: its documented
# permission modes (Q0 measured ``default``); Codex: the value its event carries (Q0 item 6, Q3: ``default``).
CONSENT_POSTURES: Mapping[str, frozenset[str]] = MappingProxyType({
    "claude_code": frozenset({"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}),
    "codex": frozenset({"default"}),
})
# The built-in tools a consent grant may name, per agent (:func:`consent_tool_problem`): each one either reached
# levain's hook in a measured PermissionRequest or has its targets read by ``levain.consent.policy``, and is not a
# shell. The file and search tools have their target paths checked against the workspace; WebFetch and SendMessage
# name no path and are matched on the working directory alone.
# A name off the list (Claude Code's Task or Agent, which spawns a model with any prompt; a tool an agent
# adds in its next release) is refused at append and answers nothing at decision: its requests go to you.
CONSENT_GRANTABLE_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType({
    "claude_code": frozenset({
        # policy.FILE_TOOLS (target: file_path, else notebook_path); Write measured in a PermissionRequest
        # (1015-consent Q3_RUN/fix1 and Q5_RUN canary_claude_code_record.jsonl)
        "Edit", "Write", "MultiEdit", "NotebookEdit",
        # policy.grant_targets reads their path fields (file_path, path, notebook_path)
        "Read", "Grep", "LS", "NotebookRead",
        "Glob",         # policy.GLOB_TOOLS: its pattern, refused when it can reach outside its root
        "WebFetch",     # names no path: matched on its working directory alone (policy module docstring)
        # names no path: matched on the working directory alone, so a grant answers every send from the workspace,
        # one that resumes a subagent with any message included. Measured in a PermissionRequest
        # (project_memory/1016-land/Q9_OWNCHILD_RUN); CHANGELOG [0.11.x] governed messaging documents a standing
        # grant on it as the way a governed session stops stalling
        "SendMessage",
    }),
    # policy.AGENT_FILE_TOOLS, targets read by the Codex adapter's patch reader; measured in a PermissionRequest
    # (1015-consent Q3_RUN/fix1 and Q5_RUN canary_codex_record.jsonl, 1017-shellnames r1)
    "codex": frozenset({"apply_patch"}),
})
# an exact tool name: no glob, no prefix; an MCP tool is mcp__<server>__<tool>
_CONSENT_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_CONSENT_MCP_TOOL = re.compile(r"mcp__[^_].*?__\S+")
_CONSENT_GRANT_MAX = timedelta(days=30)
# a branch glob only: no force (+), no src:dst, no deletion (empty source)
_REFSPEC = re.compile(r"refs/heads/[A-Za-z0-9._/*-]+")
_STORE_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_FP = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_RENEW_MAX = timedelta(days=30)
_RENEW_SKEW = timedelta(minutes=10)

# A graduation policy (14b DESIGN_REV3 §3): a signed envelope naming actions, each at a risk, the lowest rung an effect
# it covers may run at (``lowest_posture``) and an expiry; what it covers is the coverage reader's (§4). What would
# bind a writer of the levain home is the actions at their risk, the rung and the expiry; ``triggers``, ``families``
# and ``store_id`` narrow what the honest engine reads (writable rows), and ``min_clean_fires`` is advisory (the
# honest engine's walk). Its terms are fixed under its id: the one change is a renew (only a later expiry); anything
# else is a revoke and a new id. It lasts at most GRADUATION_GRANT_MAX past its link, and a renew at most that past
# its own. Created at the laptop (GRADUATION_CREATE_RANK), or by a passkey the live opt-in names
# (GRADUATION_OPT_IN_RANK); renewed by any rank-2 key, a passkey included; revoked by any enrolled key.
# Its ``guard`` (autogrant DESIGN D68, D75, D83) is optional in the record, so a log signed before it replays; this
# levain signs no policy without a complete one (:func:`_graduation_guard_check`), and renews none.
GRADUATION_KIND = "graduation"
_GRADUATION_FIELDS = frozenset({"id", "kind", "actions", "lowest_posture", "expires_at", "triggers", "families",
                                "store_id", "min_clean_fires"})
_GRADUATION_OPTIONAL = frozenset({"guard"})
_GRADUATION_ID = re.compile(r"gp-[0-9a-f]{12}")
# the rungs a policy may lower an effect to: never ABOVE_LOOP (levain.autonomic.posture.Posture names)
_GRADUATION_POSTURES = ("COOLING_OFF", "ON_LOOP")
# the action and trigger names a policy may carry
_GRADUATION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_GRADUATION_FAMILY = re.compile(r"fam-[0-9a-f]{32}")
_GRADUATION_STORE = re.compile(r"store-[0-9a-f]{32}")
# a risk as levain.autonomic.gate._risk_dict renders an ActionRisk; the class ceiling is RiskClass.LOW
_GRADUATION_RISK_FIELDS = frozenset({"cls", "reversible", "external", "financial"})
_GRADUATION_RISK_CEILING = 1
GRADUATION_GRANT_MAX = _CONSENT_GRANT_MAX
GRADUATION_CREATE_RANK = 3
# the rank the live opt-in lends a passkey it names, for a create_grant of one graduation policy
GRADUATION_OPT_IN_RANK = 2
_GRADUATION_FIXED = "a graduation policy's terms are fixed: revoke it and create another"
_GRADUATION_EXPIRY = ("a graduation policy's expiry moves only by its own renew link, never inside another op: "
                      "renew it, or revoke it and create another")
# The opt-in (DESIGN_REV3 §7): signed once at the laptop, naming the passkeys (each an active passkey key when it is
# signed) whose create_grant of one graduation policy the replay takes at GRADUATION_OPT_IN_RANK. No expiry, no
# change, at most one in the policy; a revoke removes it, and a revoked or quarantined passkey signs nothing.
GRADUATION_PHONE_KIND = "graduation-phone"
_GRADUATION_PHONE_FIELDS = frozenset({"id", "kind", "passkeys"})
_GRADUATION_PHONE_ID = re.compile(r"gpo-[0-9a-f]{12}")
_GRADUATION_PHONE_FIXED = "the graduation opt-in has no change: revoke it and create another"


class ReplayError(AuthorityError):
    """A link the replay refuses. ``seq`` is the link's position."""

    def __init__(self, seq: int, why: str) -> None:
        super().__init__(f"authority log link {seq}: {why}")
        self.seq = seq


class NotInitialised(AuthorityError):
    """The log has no links: nothing may be signed until ``levain cockpit ledger init``."""


class StaleHead(AuthorityError):
    """The head moved between preparing a link and appending it: re-prepare and re-sign."""


class HeadBelowCache(AuthorityError):
    """The log differs, at or below a seq both hold, from the chain this process already verified, and
    does not verify in full (a rewrite). One that verifies replaces the cached chain, and a log that is
    merely shorter is a prefix and is served (see :class:`ReplayCache`)."""


class AnchorError(AuthorityError):
    """The anchor refuses this log."""


class ForeignAnchor(AnchorError):
    """The anchor names another store than this log's: an anchor reset refuses it (it is for a rollback of the
    same store, DESIGN §3.8), so the refusal names the file to check or move instead."""


def _ranks() -> Mapping[str, int]:
    # imported at use: the cockpit modules will import this one
    from levain.cockpit.firejournal import _CLASS_RANK
    return _CLASS_RANK


def _tiers() -> tuple[str, ...]:
    from levain.cockpit.verbs import TIERS
    return TIERS


def _stamp(when: datetime) -> str:
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(when)


def _parse_stamp(text: Any, what: str) -> datetime:
    from levain.cockpit.consent import parse_stamp
    if not isinstance(text, str):
        raise AuthorityError(f"{what} is not a timestamp")
    try:
        when = parse_stamp(text)
    except Exception:  # noqa: BLE001 - any parse failure is the same refusal
        raise AuthorityError(f"{what} {text[:40]!r} is not a timestamp") from None
    if _stamp(when) != text:
        raise AuthorityError(f"{what} {text[:40]!r} is not in the one timestamp form")
    return when


def _canonical(obj: Any) -> str:
    # sort_keys + compact separators + ensure_ascii=False: one byte form per record. Byte parity with
    # team/entry.py's canonical, NaN/Infinity included; replay refuses non-finite numbers where they could
    # appear (only `timeouts` carries free-form values), never by changing this form.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _link_hash(prev_hash: str, record: str) -> str:
    return hashlib.sha256((prev_hash + record).encode("utf-8")).hexdigest()


def _message(prev_hash: str, record: str) -> bytes:
    return (prev_hash + "\n" + record).encode("utf-8")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


# ---------------------------------------------------------------------------------------------------
# state


@dataclass(frozen=True)
class AuthorityKey:
    fingerprint: str
    public_key: str
    key_class: str
    enrolled_seq: int
    revoked_seq: int | None = None
    compromised_since: int | None = None
    legacy_enrolled_at: str | None = None
    # the enrol link's ``at``: provenance only, never authority
    enrolled_at: str | None = None
    # the custody its enrol record declares (CUSTODIES): a declaration, never a proof
    custody: str = UNSTATED

    @property
    def principal(self) -> str:
        from levain.cockpit.keys import principal_of
        return principal_of(self.fingerprint)

    @property
    def active(self) -> bool:
        return self.revoked_seq is None


def quarantined(keys: Iterable[AuthorityKey]) -> list[tuple[AuthorityKey, str]]:
    """The QUARANTINED keys among ``keys``, each with why: active, and its stored custody is not the one its key
    material shows (:func:`custody_mismatch`). The one definition: :meth:`AuthoritySnapshot.quarantined_keys`,
    the revoke guard, ``levain doctor``, ``levain cockpit keys`` and the broker's verify all read it."""
    out = []
    for k in keys:
        why = custody_mismatch(k.public_key, k.custody) if k.active else None
        if why is not None:
            out.append((k, why))
    return out


def signing_laptops(keys: Iterable[AuthorityKey]) -> list[AuthorityKey]:
    """The laptop-presence keys among ``keys`` that can sign a new link: active and not :func:`quarantined`. Every
    "is a laptop-presence key left that can sign" count is this one."""
    laptops = [k for k in keys if k.active and k.key_class == LAPTOP]
    held = {k.fingerprint for k, _why in quarantined(laptops)}
    return [k for k in laptops if k.fingerprint not in held]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class StandingPolicy:
    """The standing policy a replay derives: verb tier raises, per-kind timeout defaults (carried, their
    semantics are S4b's) and egress grants (carried, consumed by S5)."""
    raise_to: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    timeouts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    grants: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))

    def apply(self, verb: str, tier: str) -> str:
        """``tier`` raised to this policy's floor for ``verb`` (never lowered)."""
        floor = self.raise_to.get(verb)
        if floor is None:
            return tier
        tiers = _tiers()
        return max(tier, floor, key=tiers.index)

    def as_dict(self) -> dict[str, Any]:
        return {"raise_to": _thaw(self.raise_to), "timeouts": _thaw(self.timeouts), "grants": _thaw(self.grants)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StandingPolicy:
        return cls(_freeze(dict(data["raise_to"])), _freeze(dict(data["timeouts"])),
                   _freeze(dict(data["grants"])))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StandingPolicy) and _canonical(self.as_dict()) == _canonical(other.as_dict())

    def __hash__(self) -> int:
        return hash(_canonical(self.as_dict()))


# the S3 name, kept so code written against it still imports; one type, two names
AuthorityPolicy = StandingPolicy


# The policy before any policy link, pinned for this FORMAT: replay is a pure function of the log, so
# it never reads live code (a log replays the same whatever the code ships). These are the values the
# retired ``verbs.Policy()`` shipped when the format was born: no raises. Every cockpit tier reads
# this through ``RequestAuthority.policy`` and nowhere else (S4a).
_GENESIS_RAISE_TO: Mapping[str, str] = MappingProxyType({})


def genesis_policy() -> StandingPolicy:
    """The policy before any policy link: the constant pinned for :data:`FORMAT`."""
    return StandingPolicy(raise_to=_freeze(dict(_GENESIS_RAISE_TO)))


@dataclass(frozen=True)
class AuthoritySnapshot:
    """The replayed state at ``seq``/``head_hash``. ``keys`` holds every key ever enrolled, revoked ones
    included (``revoked_seq`` set), because a revoked key may never be enrolled again."""
    store_id: str
    seq: int
    head_hash: str
    keys: Mapping[str, AuthorityKey]
    policy: StandingPolicy
    _hashes: tuple[str, ...] = field(repr=False, default=())
    # the head link's ``at``: the next link's ``at`` may not be earlier (an ordering rule, never a grant)
    head_at: datetime | None = None
    # set only on what append returns: the link committed but the anchor could not be advanced
    anchor_warning: str | None = field(default=None, compare=False)
    # per standing grant, the seq of the link that last created or changed it (a create, a renew, a loosen)
    grant_seqs: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), compare=False, repr=False)
    # per standing grant, the seq of the link that brought its id into the policy (a create, or a loosen adding it):
    # a renew or a change keeps it; a revoke and a later create of the same id gives a new one (an incarnation)
    grant_born: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), compare=False, repr=False)
    # the signer fingerprint of every link, by seq - 1 (as ``_hashes``): :func:`graduation_links` reads it
    link_signers: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def key(self, fingerprint: str) -> AuthorityKey | None:
        return self.keys.get(fingerprint)

    def grant_link(self, grant_id: str) -> tuple[int, str] | None:
        """``(seq, hash)`` of the link that created or last changed the standing grant ``grant_id``; None when
        the policy holds no such grant."""
        seq = self.grant_seqs.get(grant_id)
        link = None if seq is None else self.hash_at(seq)
        return None if seq is None or link is None or grant_id not in self.policy.grants else (seq, link)

    def active_keys(self) -> list[AuthorityKey]:
        return [k for k in self.keys.values() if k.active]

    def quarantined_keys(self) -> list[tuple[AuthorityKey, str]]:
        """The active keys whose stored custody is not the one their key material shows (:func:`custody_mismatch`),
        each with why: derived from the stored fields alone, never a field of its own. Two shapes: WebAuthn material
        at a custody but ``passkey`` (only from a log written before the enrol door, :func:`_quarantinable`), and
        material that is not a passkey's stored at custody ``passkey`` (an OpenSSH sk-ecdsa key; the door does not
        refuse that enrolment). Every verify site refuses either one's new signatures. A revoke ends it;
        re-enrolling the same material is refused (a revoked key stays revoked). :func:`quarantined` of its keys."""
        return quarantined(self.keys.values())

    def signing_laptop_keys(self) -> list[AuthorityKey]:
        """The laptop-presence keys that can sign a new link (:func:`signing_laptops`): none QUARANTINED."""
        return signing_laptops(self.keys.values())

    def hash_at(self, seq: int) -> str | None:
        return self._hashes[seq - 1] if 1 <= seq <= len(self._hashes) else None


_TEXT_COLUMNS = ("prev_hash", "record", "signer_fp", "signature", "hash")


@dataclass(frozen=True)
class _Cell:
    """A stored value that is not UTF-8 text: its SQLite type and raw bytes, kept so the row still has
    a digest; the replay refuses it."""
    kind: str
    raw: bytes | None


@dataclass(frozen=True)
class _Row:
    seq: Any
    prev_hash: Any
    record: Any
    signer_fp: Any
    signature: Any
    hash: Any

    def digest(self) -> str:
        cells = [self.seq, self.prev_hash, self.record, self.signer_fp, self.signature, self.hash]
        return hashlib.sha256(json.dumps(cells, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                                         default=repr).encode("ascii")).hexdigest()


def _text_cell(kind: str, raw: bytes | None) -> str | _Cell:
    if kind == "text" and raw is not None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return _Cell(kind, raw)


def _rows(conn: sqlite3.Connection) -> list[_Row]:
    # each text column as (typeof, bytes), decoded here: a corrupt cell becomes a _Cell the replay refuses
    # at its seq, never an exception out of the driver's own decode
    cols = ", ".join(f"typeof({c}), CAST({c} AS BLOB)" for c in _TEXT_COLUMNS)
    cur = conn.execute(f"SELECT seq, {cols} FROM authority_log ORDER BY seq")
    return [_Row(r[0], *(_text_cell(k, raw) for k, raw in zip(r[1::2], r[2::2]))) for r in cur.fetchall()]


# ---------------------------------------------------------------------------------------------------
# policy ops and the recompute classifier


def consent_workspace_problem(workspace: Any) -> str | None:
    """Why ``workspace`` cannot be a consent grant's workspace (None when it can): an absolute path in its
    normal form (no ``..``, no ``.``, no doubled or trailing slash), never ``/``. Pure, so the replay holds it;
    the home directory (and its ancestors) is refused at append (:func:`_append_only_checks`), which knows it."""
    if not isinstance(workspace, str) or not workspace or "\x00" in workspace:
        return "the workspace must be a non-empty path"
    if not workspace.startswith("/") or workspace.startswith("//"):
        return f"the workspace {workspace[:80]!r} must be an absolute path"
    if os.path.normpath(workspace) != workspace:
        return f"the workspace {workspace[:80]!r} is not in its normal form (no .., no trailing slash)"
    if workspace == "/":
        return "the workspace may not be / (that grants the whole disk)"
    return None


def consent_tool_shape_problem(tool: Any) -> str | None:
    """Why ``tool`` is not a name a consent grant may carry (None when it is): the exact-name grammar, and for an MCP
    tool the ``mcp__<server>__<tool>`` shape. The one judgement of a tool name's SHAPE, used by the grant validator and
    by the cockpit's proposals; whether the tool is grantable at all is :func:`consent_tool_problem`."""
    if not isinstance(tool, str) or not _CONSENT_TOOL.fullmatch(tool):
        return "a grant's tool must be an exact tool name (a letter, then letters, digits, _ . -, at most 128)"
    if tool.startswith("mcp__") and not _CONSENT_MCP_TOOL.fullmatch(tool):
        return "an MCP tool is named mcp__<server>__<tool>"
    return None


def consent_resolved_workspace(path: Any) -> tuple[str | None, str | None]:
    """``(resolved, None)`` when ``path`` (``~`` expanded, symlinks followed) is an existing directory a consent
    grant may name as its workspace, else ``(None, why)``. Pure of the home test, which :func:`consent_root_problem`
    makes on the result. What ``levain consent grant create`` stores is exactly this."""
    if not isinstance(path, (str, os.PathLike)) or not str(path) or "\x00" in str(path):
        return None, "the workspace must be a non-empty path"
    resolved = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        return None, f"the workspace {resolved} is not a directory"
    problem = consent_workspace_problem(resolved)
    return (None, problem) if problem is not None else (resolved, None)


def _validate_consent_grant(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A consent grant's shape (Q4). ``at``: the link's own time, when the grant arrives in a link; its life
    ends at most 30 days after it (the bound a renew has), so no link can carry a longer one."""
    if set(grant) != _CONSENT_GRANT_FIELDS:
        raise AuthorityError(f"{what} (a consent grant) must carry exactly {', '.join(sorted(_CONSENT_GRANT_FIELDS))}")
    if grant["kind"] != CONSENT_GRANT_KIND:
        raise AuthorityError(f"{what}.kind must be {CONSENT_GRANT_KIND!r}")
    return _consent_grant_body(grant, what, at)


def _validate_consent_mcp_grant(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A consent-mcp grant's shape: exactly a consent grant's fields and ``mcp_fields``, the consent fields under the
    consent grant's own rules, and the declarations under :func:`_validate_mcp_fields`."""
    if set(grant) != _CONSENT_MCP_GRANT_FIELDS:
        raise AuthorityError(f"{what} (a consent-mcp grant) must carry exactly "
                             f"{', '.join(sorted(_CONSENT_MCP_GRANT_FIELDS))}")
    if grant["kind"] != CONSENT_MCP_GRANT_KIND:
        raise AuthorityError(f"{what}.kind must be {CONSENT_MCP_GRANT_KIND!r}")
    out = _consent_grant_body(grant, what, at)
    _validate_mcp_fields(grant["mcp_fields"], grant["tools"], what)
    return out


def _consent_grant_body(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """The rules a consent grant and a consent-mcp grant share, past their own field set and kind."""
    if not isinstance(grant["id"], str) or not grant["id"]:
        raise AuthorityError(f"{what}.id must be a non-empty string")
    agent = grant["agent"]
    if not isinstance(agent, str) or agent not in CONSENT_AGENTS:
        raise AuthorityError(f"{what}.agent must be one of {', '.join(CONSENT_AGENTS)}")
    for name, ok in (("tools", lambda t: consent_tool_shape_problem(t) is None),
                     ("postures", CONSENT_POSTURES[agent].__contains__)):
        items = grant[name]
        if not isinstance(items, (list, tuple)) or not items or not all(isinstance(i, str) and ok(i) for i in items):
            raise AuthorityError(f"{what}.{name} is not a non-empty list of allowed values" + (
                " (exact tool names, an MCP tool as mcp__<server>__<tool>)" if name == "tools" else
                f" (for {agent}: {', '.join(sorted(CONSENT_POSTURES[agent]))})"))
        if list(items) != sorted(set(items)):
            raise AuthorityError(f"{what}.{name} must be sorted and unique")
    shell = [t for t in grant["tools"] if t.lower() in _REPLAY_SHELL_TOOLS]
    if shell:
        raise AuthorityError(_shell_refusal(what, shell[0]))
    problem = consent_workspace_problem(grant["workspace"])
    if problem is not None:
        raise AuthorityError(f"{what}: {problem}")
    expires = _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    # a difference, never `at + 30 days`, which overflows near year 9999
    if at is not None and expires - at > _CONSENT_GRANT_MAX:
        raise AuthorityError(f"{what} may last at most 30 days past its link's time")
    return _thaw(grant)


def _validate_inert_grant(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A grant of a kind this levain does not know (one a newer levain wrote), held INERT: only its envelope is read
    (``id`` a non-empty string, ``kind`` a :data:`_GRANT_KIND_NAME`, ``expires_at`` a stamp at most 30 days past its
    link's time, every leaf finite JSON). It answers nothing (:func:`consent_grant` is false for it), and a change
    to it classifies as nothing narrower than a renew (:func:`_inert_grant_change`)."""
    if not isinstance(grant["kind"], str) or not _GRANT_KIND_NAME.fullmatch(grant["kind"]):
        raise AuthorityError(f"{what}.kind must be a grant kind name ({_GRANT_KIND_NAME.pattern})")
    if not isinstance(grant.get("id"), str) or not grant["id"]:
        raise AuthorityError(f"{what}.id must be a non-empty string")
    expires = _parse_stamp(grant.get("expires_at"), f"{what}.expires_at")
    if at is not None and expires - at > _CONSENT_GRANT_MAX:
        raise AuthorityError(f"{what} may last at most 30 days past its link's time")
    if not _finite_json(grant):
        raise AuthorityError(f"{what} must be finite JSON (no NaN or Infinity)")
    return _thaw(grant)


def _graduation_risk(risk: Any, where: str) -> None:
    """A signed action risk: well-formed, and one a graduation policy may cover (class LOW or below, reversible,
    not external, not financial). A replay rule as well as an append rule: it reads the link alone."""
    if not isinstance(risk, Mapping) or set(risk) != _GRADUATION_RISK_FIELDS or type(risk["cls"]) is not int \
            or not all(type(risk[k]) is bool for k in ("reversible", "external", "financial")):
        raise AuthorityError(f"{where} must be a risk dict: exactly cls (an integer) and reversible, external and "
                             "financial (booleans)")
    if not 0 <= risk["cls"] <= _GRADUATION_RISK_CEILING:
        raise AuthorityError(f"{where}.cls is {risk['cls']}: a graduation policy covers an action of risk class "
                             f"LOW ({_GRADUATION_RISK_CEILING}) or below only")
    for name, refused in (("reversible", False), ("external", True), ("financial", True)):
        if risk[name] is refused:
            raise AuthorityError(f"{where}.{name} is {str(refused).lower()}: a graduation policy covers a reversible, "
                                 "internal, non-financial action only")


def _graduation_names(items: Any, pattern: re.Pattern[str], where: str) -> None:
    """``None``, or a non-empty sorted list of unique names ``pattern`` matches (equal sets are equal bytes)."""
    if items is None:
        return
    if not isinstance(items, (list, tuple)) or not items \
            or not all(isinstance(i, str) and pattern.fullmatch(i) for i in items):
        raise AuthorityError(f"{where} is null or a non-empty list of names ({pattern.pattern})")
    if list(items) != sorted(set(items)):
        raise AuthorityError(f"{where} must be sorted and unique")


def _validate_graduation(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A graduation policy's shape (:data:`GRADUATION_KIND`). ``at``: the link's own time; the policy lasts at most
    :data:`GRADUATION_GRANT_MAX` past it. A ``guard`` is held here only to be a list of objects: what its entries
    must say is an append rule (:func:`_graduation_guard_check`), so a replay never refuses the log over a guard's
    contents, and a replayed policy whose guard is not one the gate can evaluate covers nothing
    (``levain.autonomic.coverage``)."""
    if not _GRADUATION_FIELDS <= set(grant) or set(grant) - _GRADUATION_FIELDS - _GRADUATION_OPTIONAL:
        raise AuthorityError(f"{what} (a graduation policy) must carry exactly {', '.join(sorted(_GRADUATION_FIELDS))}"
                             f" (optionally {', '.join(sorted(_GRADUATION_OPTIONAL))})")
    if "guard" in grant:
        guard = grant["guard"]
        if not isinstance(guard, (list, tuple)) or not all(isinstance(g, Mapping) for g in guard):
            raise AuthorityError(f"{what}.guard must be a list of objects")
    if grant["kind"] != GRADUATION_KIND:
        raise AuthorityError(f"{what}.kind must be {GRADUATION_KIND!r}")
    if not isinstance(grant["id"], str) or not _GRADUATION_ID.fullmatch(grant["id"]):
        raise AuthorityError(f"{what}.id must be gp- and 12 lowercase hex digits")
    actions = grant["actions"]
    if not isinstance(actions, Mapping) or not actions:
        raise AuthorityError(f"{what}.actions must map at least one action name to its risk")
    for name, risk in actions.items():
        if not isinstance(name, str) or not _GRADUATION_NAME.fullmatch(name):
            raise AuthorityError(f"{what}.actions names {str(name)[:80]!r}, not an action name "
                                 f"({_GRADUATION_NAME.pattern})")
        _graduation_risk(risk, f"{what}.actions[{name!r}]")
    if grant["lowest_posture"] not in _GRADUATION_POSTURES:
        raise AuthorityError(f"{what}.lowest_posture must be one of {', '.join(_GRADUATION_POSTURES)}")
    expires = _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    if at is not None and expires - at > GRADUATION_GRANT_MAX:
        raise AuthorityError(f"{what} may last at most {GRADUATION_GRANT_MAX.days} days past its link's time")
    _graduation_names(grant["triggers"], _GRADUATION_NAME, f"{what}.triggers")
    _graduation_names(grant["families"], _GRADUATION_FAMILY, f"{what}.families")
    if not isinstance(grant["store_id"], str) or not _GRADUATION_STORE.fullmatch(grant["store_id"]):
        raise AuthorityError(f"{what}.store_id must be store- and 32 lowercase hex digits")
    fires = grant["min_clean_fires"]
    if type(fires) is not int or fires < 0:
        raise AuthorityError(f"{what}.min_clean_fires must be a non-negative integer")
    return _thaw(grant)


def _validate_graduation_phone(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """The opt-in's shape (:data:`GRADUATION_PHONE_KIND`): an id and a non-empty sorted list of unique key
    fingerprints. That each names an active passkey key is judged against the state (:func:`_graduation_step_checks`)."""
    if set(grant) != _GRADUATION_PHONE_FIELDS:
        raise AuthorityError(f"{what} (the graduation opt-in) must carry exactly "
                             f"{', '.join(sorted(_GRADUATION_PHONE_FIELDS))}")
    if grant["kind"] != GRADUATION_PHONE_KIND:
        raise AuthorityError(f"{what}.kind must be {GRADUATION_PHONE_KIND!r}")
    if not isinstance(grant["id"], str) or not _GRADUATION_PHONE_ID.fullmatch(grant["id"]):
        raise AuthorityError(f"{what}.id must be gpo- and 12 lowercase hex digits")
    keys = grant["passkeys"]
    if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) and _FP.fullmatch(k) for k in keys):
        raise AuthorityError(f"{what}.passkeys must be a non-empty list of key fingerprints")
    if list(keys) != sorted(set(keys)):
        raise AuthorityError(f"{what}.passkeys must be sorted and unique")
    return _thaw(grant)


def egress_key_host_problem(host: Any) -> str | None:
    """Why ``host`` cannot be a setup record's host, or None: it must be a DNS name (never an IP literal, never a
    wildcard) already in its canonical form, so the record, the one-live rule and the route lookup compare one
    spelling (R15-21-6)."""
    from levain.chat_egress.parse import canonical_host

    if not isinstance(host, str) or not host or not host.isascii():
        return "is not a host in its canonical form (an ASCII DNS name)"
    try:
        canon = canonical_host(host)
    except ValueError as exc:
        return f"is not a host in its canonical form ({exc})"
    if canon != host:
        return f"is not in its canonical form (that is {canon!r})"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return "must be a DNS name, not an IP literal"


def egress_key_repo_problem(repo: Any) -> str | None:
    """Why ``repo`` cannot be a declared repo path, or None: an origin-form path that starts with ``/``, holds more
    than ``/``, does not end with ``/``, carries no query, is already in R-2's canonical form (the decider
    compares a request's prefix to it as a string), and holds none of the escapes the key rule makes keyless
    (``advert.KEYLESS_ESCAPES``: such a repo would be signed and then never attach)."""
    from levain.chat_egress.advert import KEYLESS_ESCAPES
    from levain.chat_egress.parse import canonical_path

    if not isinstance(repo, str) or not repo.startswith("/") or repo == "/" or repo.endswith("/") or "?" in repo:
        return "a declared repo is a path like /owner/repo.git (a leading /, no trailing /, no query)"
    canon, why = canonical_path(repo, "GET")
    if canon is None:
        return f"a declared repo path walls ({why})"
    if canon != repo:
        return f"a declared repo path must be in its canonical form ({canon!r})"
    held = [esc for esc in KEYLESS_ESCAPES if esc in canon]
    if held:
        return f"a declared repo path holding {', '.join(held)} never carries the key (such a path is keyless)"
    return None


def _validate_egress_key(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A setup record's shape (:data:`EGRESS_KEY_KIND`)."""
    if set(grant) != _EGRESS_KEY_FIELDS:
        raise AuthorityError(f"{what} (a setup record) must carry exactly {', '.join(sorted(_EGRESS_KEY_FIELDS))}")
    if grant["kind"] != EGRESS_KEY_KIND:
        raise AuthorityError(f"{what}.kind must be {EGRESS_KEY_KIND!r}")
    if not isinstance(grant["id"], str) or not _EGRESS_KEY_ID.fullmatch(grant["id"]):
        raise AuthorityError(f"{what}.id must match {_EGRESS_KEY_ID.pattern}")
    for name in ("entity", "source", "account"):
        if not isinstance(grant[name], str) or not _EGRESS_KEY_TEXT.fullmatch(grant[name]):
            raise AuthorityError(f"{what}.{name} must be 1 .. 255 printable ASCII characters (the {name}'s name)")
    problem = egress_key_host_problem(grant["host"])
    if problem is not None:
        raise AuthorityError(f"{what}.host {problem}")
    placement = grant["placement"]
    ok = isinstance(placement, Mapping) and (
        dict(placement) == {"scheme": "bearer"}
        or (set(placement) == {"scheme", "user"} and placement["scheme"] == "basic"
            and isinstance(placement["user"], str) and bool(_EGRESS_KEY_USER.fullmatch(placement["user"]))))
    if not ok:
        raise AuthorityError(f"{what}.placement must be {{'scheme': 'bearer'}} or {{'scheme': 'basic', 'user': U}} "
                             "(U printable, no space, no ':')")
    repos = grant["repos"]
    if not isinstance(repos, (list, tuple)) or len(repos) > _EGRESS_KEY_REPOS_MAX:
        raise AuthorityError(f"{what}.repos must be a list of at most {_EGRESS_KEY_REPOS_MAX} declared repo paths")
    for repo in repos:
        problem = egress_key_repo_problem(repo)
        if problem is not None:
            raise AuthorityError(f"{what}.repos: {problem}")
    if list(repos) != sorted(set(repos)):
        raise AuthorityError(f"{what}.repos must be sorted and unique")
    expires = _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    if at is not None and expires - at > _CONSENT_GRANT_MAX:
        raise AuthorityError(f"{what} may last at most 30 days past its link's time")
    return _thaw(grant)


def _egress_key_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A setup record's change: ``renew`` when only its expiry moved later, anything else ``loose`` (a new host, a
    new source, account or placement, or a repo added or dropped is a new credential route, never narrower). The
    replay's classification only: a new link that changes a record's id in place but its expiry is refused at
    append (:func:`_egress_key_append_check`)."""
    rest = lambda g: _canonical({k: v for k, v in g.items() if k != "expires_at"})   # noqa: E731
    return "renew" if rest(o) == rest(n) and n_exp > o_exp else "loose"


def egress_key_record(grant: Any) -> bool:
    """Whether ``grant`` is a setup record this levain knows (:data:`EGRESS_KEY_KIND`)."""
    return isinstance(grant, Mapping) and grant.get("kind") == EGRESS_KEY_KIND \
        and _grant_kind(grant) is _GRANT_KINDS[EGRESS_KEY_KIND]


def egress_key_live(grant: Any, now: datetime) -> bool:
    """THE definition of a LIVE setup record (R15-21-4, R15-22-1): a setup record the replay holds whose
    ``expires_at`` is after ``now``. A revoked or superseded one is no longer in the policy, so it is not live."""
    if not egress_key_record(grant):
        return False
    try:
        return _parse_stamp(grant["expires_at"], "expires_at") > now
    except AuthorityError:
        return False


def egress_key_conflicts(grants: Mapping[str, Any], now: datetime) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(entity, host, record ids)`` for every (entity, canonical host) holding more than one live setup record:
    what a launch refuses, naming both (R15-21-4: no merge, no pick)."""
    groups: dict[tuple[str, str], list[str]] = {}
    for gid, g in grants.items():
        if egress_key_live(g, now):
            groups.setdefault((g["entity"], g["host"]), []).append(gid)
    return [(e, h, tuple(sorted(ids))) for (e, h), ids in sorted(groups.items()) if len(ids) > 1]


def _egress_key_meaning(grant: Any) -> str | None:
    """A setup record's meaning: everything it says but its expiry, canonical (None: not a mapping)."""
    if not isinstance(grant, Mapping):
        return None
    return _canonical(_thaw({k: v for k, v in grant.items() if k != "expires_at"}))


def egress_key_incarnation(state: AuthoritySnapshot, record_id: str) -> str | None:
    """The incarnation of setup record ``record_id`` in ``state``, or None when ``state`` holds no such record: the
    seq of the link that brought the id into the policy, and a sha256 of its meaning (:func:`_egress_key_meaning`).
    A launch's route carries it and every per-request liveness check compares it (``levain.chat_egress.keys``), so a
    revoked id created again, or the same id holding another host, source, account, placement or repo list, is not
    the record the launch read. A renew (only the expiry later) keeps it."""
    grant = state.policy.grants.get(record_id)
    born = state.grant_born.get(record_id)
    if not egress_key_record(grant) or born is None:
        return None
    meaning = _egress_key_meaning(grant)
    return f"{born}:{hashlib.sha256(meaning.encode()).hexdigest()}" if meaning is not None else None


def graduation_grant(grant: Any) -> bool:
    """Whether ``grant`` is a graduation policy this levain knows (:data:`GRADUATION_KIND`)."""
    return isinstance(grant, Mapping) and grant.get("kind") == GRADUATION_KIND


def graduation_opt_in(policy: StandingPolicy) -> Mapping[str, Any] | None:
    """The graduation opt-in ``policy`` holds (the replay admits at most one), or None."""
    found = [g for g in policy.grants.values() if isinstance(g, Mapping) and g.get("kind") == GRADUATION_PHONE_KIND]
    return found[0] if len(found) == 1 else None


def graduation_incarnation(state: AuthoritySnapshot, grant_id: str) -> str | None:
    """The incarnation of graduation policy ``grant_id`` in ``state``, or None when ``state`` holds no such policy: the
    seq of the link that brought the id into the policy, and a sha256 of everything it says but its expiry (the shape
    of :func:`egress_key_incarnation`). A renew keeps it; a revoke and a create of the same id gives another."""
    grant = state.policy.grants.get(grant_id)
    born = state.grant_born.get(grant_id)
    if not graduation_grant(grant) or born is None:
        return None
    meaning = _canonical(_thaw({k: v for k, v in grant.items() if k != "expires_at"}))
    return f"{born}:{hashlib.sha256(meaning.encode()).hexdigest()}"


def graduation_links(state: AuthoritySnapshot, grant_id: str) -> tuple[str, str] | None:
    """``(born_signer_fp, current_signer_fp)`` of graduation policy ``grant_id`` in ``state``: the signer of the link
    that brought it into the policy (``grant_born``) and of the link that last created or changed it (``grant_seqs``,
    which a renew moves). With its terms fixed, those two links are every link that defines what it means now. None
    when ``state`` holds no such policy, or does not carry one signer per link (``link_signers``, by seq)."""
    if not graduation_grant(state.policy.grants.get(grant_id)):
        return None
    born, current = state.grant_born.get(grant_id), state.grant_seqs.get(grant_id)
    signers = state.link_signers
    if born is None or current is None or len(signers) != state.seq \
            or not (1 <= born <= state.seq and 1 <= current <= state.seq):
        return None
    return signers[born - 1], signers[current - 1]


def _egress_key_append_check(state: AuthoritySnapshot, rec: Mapping[str, Any], now: datetime) -> None:
    """The one-live rule at append (R15-21-4): a create or a loosen that brings in or changes a setup record may not
    leave two live records for that record's (entity, host). A supersession is the loosen that drops the old one in
    the same link. And a record's id keeps its meaning: a loosen that keeps an id but changes anything of a setup
    record there but its expiry (its host, source, account, placement, repos, entity or kind) is refused; that is a
    supersession under a new id (a running launch's route is bound to the record it read). Judged with the
    appender's clock (liveness reads expiry), so it is an append rule, not a replay rule: a store a foreign writer left holding two is refused at LAUNCH, naming both (``levain.chat_egress.keys``)."""
    if rec["op"] == "create_grant":
        grant = rec["grant"]
        after = {**state.policy.grants, grant["id"]: grant}
        brought = [grant]
    elif rec["op"] == "loosen":
        after = rec["policy"]["grants"]
        old = state.policy.grants
        brought = [g for gid, g in after.items()
                   if gid not in old or _canonical(_thaw(old[gid])) != _canonical(_thaw(g))]
        for gid, g in after.items():
            if gid in old and (egress_key_record(old[gid]) or egress_key_record(g)) \
                    and _egress_key_meaning(old[gid]) != _egress_key_meaning(g):
                raise AuthorityError(f"setup record {gid[:80]!r} keeps its meaning: a change to anything but its "
                                     "expiry is a new record under a new id (a supersession), never the same id")
    else:
        return
    touched = {(g["entity"], g["host"]) for g in brought if egress_key_live(g, now)}
    for entity, host, ids in egress_key_conflicts(after, now):
        if (entity, host) in touched:
            raise AuthorityError(f"one live setup record per entity and host: {entity[:80]!r} already has a live "
                                 f"setup record for {host} ({', '.join(ids)}); replace it (a supersession) or "
                                 "revoke it first")


def _validate_grant(grant: Any, what: str, *, at: datetime | None = None) -> dict[str, Any]:
    if not isinstance(grant, Mapping):
        raise AuthorityError(f"{what} must carry exactly {', '.join(sorted(_GRANT_FIELDS))}")
    return _grant_kind(grant).validate(grant, what, at)


def _validate_egress_grant(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    if set(grant) != _GRANT_FIELDS:
        raise AuthorityError(f"{what} must carry exactly {', '.join(sorted(_GRANT_FIELDS))}")
    for name in ("id", "entity", "remote_url", "deploy_key_sha256"):
        if not isinstance(grant[name], str) or not grant[name]:
            raise AuthorityError(f"{what}.{name} must be a non-empty string")
    for name, ok in (("refspecs", _REFSPEC.fullmatch), ("verbs", _GRANT_VERBS.__contains__)):
        items = grant[name]
        if not isinstance(items, (list, tuple)) or not items or not all(isinstance(i, str) and ok(i) for i in items):
            raise AuthorityError(f"{what}.{name} is not a non-empty list of allowed values")
        # sorted and unique, so equal sets are equal bytes
        if list(items) != sorted(set(items)):
            raise AuthorityError(f"{what}.{name} must be sorted and unique")
    _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    return _thaw(grant)


def _validate_policy(data: Any, *, at: datetime | None = None) -> StandingPolicy:
    if not isinstance(data, Mapping) or set(data) != {"raise_to", "timeouts", "grants"}:
        raise AuthorityError("a policy must carry exactly raise_to, timeouts and grants")
    raise_to, timeouts, grants = data["raise_to"], data["timeouts"], data["grants"]
    tiers = _tiers()
    if not isinstance(raise_to, Mapping) or not all(isinstance(v, str) and v and t in tiers
                                                    for v, t in raise_to.items()):
        raise AuthorityError("policy.raise_to must map verbs to tiers")
    _validate_timeouts(timeouts)
    if not isinstance(grants, Mapping):
        raise AuthorityError("policy.grants must be a mapping")
    for gid, grant in grants.items():
        g = _validate_grant(grant, f"policy.grants[{gid!r}]", at=at)
        if g["id"] != gid:
            raise AuthorityError(f"policy.grants[{gid!r}] carries id {g['id']!r}")
    return StandingPolicy.from_dict(data)


def _finite_json(value: Any) -> bool:
    """Every leaf a finite JSON scalar (NaN and Infinity are not JSON)."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _finite_json(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_json(v) for v in value)
    return False


def _validate_timeouts(timeouts: Any) -> None:
    if not isinstance(timeouts, Mapping) or not all(isinstance(k, str) and k for k in timeouts):
        raise AuthorityError("timeouts must map pending kinds to values")
    if not _finite_json(timeouts):
        raise AuthorityError("timeouts values must be finite JSON (no NaN or Infinity)")


def _renewable_kind(grant: Mapping[str, Any], gid: Any) -> _GrantKind:
    """The kind of ``grant`` (id ``gid``), which a renew is about to extend; refused for a kind that does not expire
    (the opt-in). The one rule for "never renewed", called by the replay (:func:`_apply_policy_op`) and by the
    append-only checks (:func:`_append_only_checks`) before either reads ``expires_at``."""
    kind = _grant_kind(grant)
    if not kind.expires:
        raise AuthorityError(f"grant {str(gid)[:80]!r} has no expiry, so it is never renewed")
    return kind


def _apply_policy_op(policy: StandingPolicy, rec: Mapping[str, Any]) -> StandingPolicy:
    """The policy after the declared op, refusing a malformed or impossible one. Does NOT judge the
    delta's kind: that is :func:`classify`, on the result alone."""
    op = rec["op"]
    data = policy.as_dict()
    if op == "raise":
        verb, tier = rec["verb"], rec["tier"]
        if not isinstance(verb, str) or not verb or tier not in _tiers():
            raise AuthorityError("raise needs a verb and a tier")
        data["raise_to"][verb] = tier
    elif op == "loosen":
        return _validate_policy(rec["policy"], at=_parse_stamp(rec["at"], "at"))
    elif op == "timeouts":
        _validate_timeouts(rec["timeouts"])
        data["timeouts"] = _thaw(rec["timeouts"])
    elif op == "create_grant":
        at = _parse_stamp(rec["at"], "at")
        grant = _validate_grant(rec["grant"], "grant", at=at)
        if grant["id"] in data["grants"]:
            raise AuthorityError(f"grant {grant['id']!r} already exists")
        kind = _grant_kind(grant)
        if kind is not _EGRESS_KIND and kind.expires and _parse_stamp(grant["expires_at"], "expires_at") <= at:
            raise AuthorityError(f"a {str(grant['kind'])[:40]} grant must expire after the link that creates it")
        data["grants"][grant["id"]] = grant
    elif op in ("revoke_grant", "renew"):
        gid = rec["grant_id"]
        if not isinstance(gid, str) or gid not in data["grants"]:
            raise AuthorityError(f"no grant {gid!r}")
        if op == "revoke_grant":
            del data["grants"][gid]
        else:
            kind = _renewable_kind(data["grants"][gid], gid)
            old = _parse_stamp(data["grants"][gid]["expires_at"], "the grant's expires_at")
            new = _parse_stamp(rec["expires_at"], "expires_at")
            signed = _parse_stamp(rec["signed_at"], "signed_at")
            # signed_at IS the link's at, which the ordering rule bounds below; otherwise a replayed
            # signed_at would be an unbounded input to the 30-day rule
            if rec["signed_at"] != rec["at"]:
                raise AuthorityError("a renew's signed_at must equal its at")
            if old <= signed:
                raise AuthorityError(f"grant {gid!r} has expired ({data['grants'][gid]['expires_at']}) by this "
                                     "renew's signing time; a lapsed grant is never renewed, only created "
                                     "again (at the laptop, or a graduation policy by a passkey the opt-in names)")
            if new <= old:
                raise AuthorityError("a renew must move the grant's expiry later")
            # a difference, never `signed + 30 days`, which overflows near year 9999
            if new - signed > _RENEW_MAX:
                raise AuthorityError("a renew may extend a grant at most 30 days past its signing time")
            # a kind with its own cap holds every renew to it as well (R7: a graduation policy's renew)
            if kind.max_life is not None and new - signed > kind.max_life:
                raise AuthorityError(f"a renew may extend this grant at most {kind.max_life.days} days past its "
                                     "signing time")
            data["grants"][gid]["expires_at"] = rec["expires_at"]
    else:
        raise AuthorityError(f"{op!r} is not a policy op")
    return StandingPolicy.from_dict(data)


def _grant_change(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """``same``, ``renew`` (only a later expiry), ``tight`` (provably narrower) or ``loose``."""
    o, n = _thaw(old), _thaw(new)
    if _canonical(o) == _canonical(n):
        return "same"
    if o.get("kind") != n.get("kind"):
        # a kind whose terms are fixed under its id is never turned into another kind, nor another into it
        for g in (o, n):
            if _grant_kind(g).fixed is not None:
                raise AuthorityError(_grant_kind(g).fixed)
        return "loose"
    # the opt-in has no expiry (its kind's ``expires`` is False); every other kind's validate requires one
    o_exp = _parse_stamp(o["expires_at"], "expires_at") if "expires_at" in o else None
    n_exp = _parse_stamp(n["expires_at"], "expires_at") if "expires_at" in n else None
    kind = _grant_kind(n)
    if kind.expires and (o_exp is None or n_exp is None):
        raise AuthorityError(f"grant {str(n.get('id'))[:80]!r} carries no expires_at, which its kind requires")
    return kind.change(o, n, o_exp, n_exp)


def _egress_grant_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    rest = [k for k in _GRANT_FIELDS if k not in ("refspecs", "verbs", "expires_at")]
    if any(o[k] != n[k] for k in rest):
        return "loose"
    narrower_sets = set(n["refspecs"]) <= set(o["refspecs"]) and set(n["verbs"]) <= set(o["verbs"])
    sets_equal = n["refspecs"] == o["refspecs"] and n["verbs"] == o["verbs"]
    if sets_equal and n_exp > o_exp:
        return "renew"
    if narrower_sets and n_exp <= o_exp:
        return "tight"
    return "loose"


def _path_within(path: str, root: str) -> bool:
    """``path`` is ``root`` or under it (both absolute normal forms; a string test, so ``/a/bc`` is not in ``/a/b``)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _consent_grant_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A consent grant's change: ``renew`` when only the expiry moved later; ``tight`` when the id and agent
    are the same, tools and postures a subset, the workspace the same or under the old one, and the expiry no
    later; anything else ``loose``."""
    if o["id"] != n["id"] or o["agent"] != n["agent"]:
        return "loose"
    if n["tools"] == o["tools"] and n["postures"] == o["postures"] and n["workspace"] == o["workspace"] \
            and n_exp > o_exp:
        return "renew"
    narrower = set(n["tools"]) <= set(o["tools"]) and set(n["postures"]) <= set(o["postures"]) \
        and isinstance(n["workspace"], str) and isinstance(o["workspace"], str) \
        and _path_within(n["workspace"], o["workspace"])
    return "tight" if narrower and n_exp <= o_exp else "loose"


def _consent_mcp_grant_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A consent-mcp grant's change: a consent grant's (:func:`_consent_grant_change`), except that every tool the new
    grant names must carry the very declaration it had (any change to one, a field added, dropped or of another
    kind, or a listed read tool's declaration dropped so it falls back to levain's own schema, is ``loose``)."""
    if any(consent_mcp_declaration(n, t) != consent_mcp_declaration(o, t) for t in n["tools"]):
        return "loose"
    return _consent_grant_change(o, n, o_exp, n_exp)


def _inert_grant_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A change to a grant of a kind this levain does not know: ``renew`` when only its expiry moved later, else
    ``loose`` (never ``tight``: what would be narrower is the newer levain's to say). Fail-closed on purpose, and the
    rule a newer writer is held to: it changes a grant of a kind an older reader does not know only by create/loosen,
    renew or revoke; any new op, or a change that declares TIGHTEN, is a log FORMAT bump. An older reader replays a
    tighten-declared change to such a grant as an op/delta mismatch and stops the log at that seq."""
    rest = lambda g: _canonical({k: v for k, v in g.items() if k != "expires_at"})   # noqa: E731
    return "renew" if rest(o) == rest(n) and n_exp > o_exp else "loose"


def _graduation_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A graduation policy's change: ``renew`` when only its expiry moved later; anything else is refused (its terms
    are fixed under its id, at append and at replay)."""
    rest = lambda g: _canonical({k: v for k, v in g.items() if k != "expires_at"})   # noqa: E731
    if rest(o) == rest(n) and n_exp > o_exp:
        return "renew"
    raise AuthorityError(_GRADUATION_FIXED)


def _graduation_guard_problem(grant: Mapping[str, Any]) -> str | None:
    # the gate's own rule for a signed guard, imported at use (only an append reaches it, never a replay)
    from levain.autonomic.coverage import guard_problem
    if "guard" not in grant:
        return "it carries no guard (every graduation policy is signed with one: its kill and its drill)"
    return guard_problem(_thaw(grant["guard"]))


def _graduation_guard_check(grants: list[Any]) -> None:
    """At append (DESIGN D83, 1026-20 RULINGS R13-11): a graduation policy a create or a loosen brings in carries a
    complete guard, whatever rung it names (the rung an effect runs at is the binding's and the policy's together, so
    only the fire knows whether it is confirm-class), every entry within the sign-time caps. Never at replay: a planted
    policy without one replays and covers nothing."""
    for grant in grants:
        why = _graduation_guard_problem(grant)
        if why is not None:
            raise AuthorityError(f"graduation policy {str(grant.get('id'))[:80]!r} is refused at sign: {why}")


def _graduation_renew_check(grants: list[Any], after: str) -> None:
    """At append: a renew extends only a policy signed with a complete guard. One signed before the guard covers
    nothing (``levain.autonomic.coverage.live``), and a renew cannot add one (its terms are fixed)."""
    for grant in grants:
        why = _graduation_guard_problem(grant)
        if why is not None:
            raise AuthorityError(f"graduation policy {str(grant.get('id'))[:80]!r} is not renewed: {why}; it covers "
                                 "nothing, so revoke it and create another with a guard")


def _graduation_phone_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime | None,
                             n_exp: datetime | None) -> str:
    """The opt-in has no change: every one is refused."""
    raise AuthorityError(_GRADUATION_PHONE_FIXED)


def _refuse_unknown_kind(grants: list[Any]) -> None:
    """At append: this levain writes no grant of a kind it does not know (it holds one a newer levain wrote, inert)."""
    for grant in grants:
        kind = grant.get("kind") if isinstance(grant, Mapping) else None
        raise AuthorityError(f"this levain does not know the grant kind {str(kind)[:40]!r}: a grant of it is "
                             "replayed, renewed and revoked here, never created or otherwise changed; use the "
                             "levain that wrote it")


@dataclass(frozen=True)
class _GrantKind:
    """How one grant kind is read, compared and appended. ``consent``: the consent queue answers from a grant of it
    (:func:`consent_grant`). ``append_checks`` judge each grant of the kind a create or a loosen brings in or changes;
    ``renew_checks`` the grant a renew extends (given the refusal's tail naming the grant). ``passkey_revocable``: a
    passkey may revoke a grant of it (:func:`passkey_revocable`). ``expires``: a grant of it carries ``expires_at``
    (the opt-in does not, so it is never renewed). ``max_life``: the most a renew may set its expiry past the renew's
    own time, beside the generic renew bound. ``fixed``: the refusal for any change to a grant of it but a renew
    (its kind included), raised from ``change`` and from :func:`_grant_change`."""
    validate: Callable[[Mapping[str, Any], str, datetime | None], dict[str, Any]]
    change: Callable[[Mapping[str, Any], Mapping[str, Any], datetime, datetime], str]
    append_checks: tuple[Callable[[list[Any]], None], ...] = ()
    renew_checks: tuple[Callable[[list[Any], str], None], ...] = ()
    consent: bool = False
    passkey_revocable: bool = False
    expires: bool = True
    max_life: timedelta | None = None
    fixed: str | None = None


# Every kind this levain knows, by the grant's ``kind`` (``None``: an egress grant, which carries none); any other
# kind is :data:`_INERT_KIND`. Every per-kind rule dispatches through here, so no check keyed on one kind's name can
# skip another kind (the consent checks are wrapped in lambdas because they are defined further down).
_GRANT_KIND_NAME = re.compile(r"[a-z][a-z0-9.-]{0,31}")
_EGRESS_KIND = _GrantKind(validate=_validate_egress_grant, change=_egress_grant_change)
_INERT_KIND = _GrantKind(validate=_validate_inert_grant, change=_inert_grant_change,
                         append_checks=(_refuse_unknown_kind,), passkey_revocable=True)
_GRANT_KINDS: Mapping[str | None, _GrantKind] = MappingProxyType({
    None: _EGRESS_KIND,
    CONSENT_GRANT_KIND: _GrantKind(validate=_validate_consent_grant, change=_consent_grant_change,
                                   append_checks=(lambda g: _consent_home_check(g), lambda g: _consent_tool_check(g)),
                                   renew_checks=(lambda g, after: _consent_tool_check(g, after),), consent=True,
                                   passkey_revocable=True),
    CONSENT_MCP_GRANT_KIND: _GrantKind(validate=_validate_consent_mcp_grant, change=_consent_mcp_grant_change,
                                       append_checks=(lambda g: _consent_home_check(g),
                                                      lambda g: _consent_mcp_tool_check(g)),
                                       renew_checks=(lambda g, after: _consent_mcp_tool_check(g, after),),
                                       consent=True, passkey_revocable=True),
    EGRESS_KEY_KIND: _GrantKind(validate=_validate_egress_key, change=_egress_key_change),
    GRADUATION_KIND: _GrantKind(validate=_validate_graduation, change=_graduation_change,
                                append_checks=(_graduation_guard_check,), renew_checks=(_graduation_renew_check,),
                                passkey_revocable=True, max_life=GRADUATION_GRANT_MAX, fixed=_GRADUATION_FIXED),
    GRADUATION_PHONE_KIND: _GrantKind(validate=_validate_graduation_phone, change=_graduation_phone_change,
                                      passkey_revocable=True, expires=False, fixed=_GRADUATION_PHONE_FIXED),
})


def _grant_kind(grant: Mapping[str, Any]) -> _GrantKind:
    if "kind" not in grant:
        return _EGRESS_KIND
    kind = grant["kind"]
    return _GRANT_KINDS.get(kind, _INERT_KIND) if isinstance(kind, str) else _INERT_KIND


def consent_grant(grant: Any) -> bool:
    """Whether the consent queue answers from ``grant``: a grant of a consent kind this levain knows. A grant of a
    kind it does not know is held inert and answers nothing."""
    return isinstance(grant, Mapping) and _grant_kind(grant).consent


def inert_grant(grant: Any) -> bool:
    """Whether ``grant`` is of a kind this levain does not know (held inert: replayed, revocable, answers nothing)."""
    return isinstance(grant, Mapping) and _grant_kind(grant) is _INERT_KIND


def consent_revocable(grant: Any) -> bool:
    """Whether ``grant`` is a consent grant or an inert one: what a consent revoke removes. An egress grant is
    neither. What a passkey may revoke is :func:`passkey_revocable`."""
    return consent_grant(grant) or inert_grant(grant)


def passkey_revocable(grant: Any) -> bool:
    """Whether a passkey may sign the revoke of ``grant``: a consent or consent-mcp grant, an inert one, a graduation
    policy or the graduation opt-in (each kind's ``passkey_revocable``). Never an egress grant or a setup record."""
    return isinstance(grant, Mapping) and _grant_kind(grant).passkey_revocable


def classify(old: StandingPolicy, new: StandingPolicy) -> str:
    """The kind of the difference from ``old`` to ``new``, computed from the two policies alone (§4.3).
    :data:`NOOP` when nothing changes in effect (a missing floor and a floor at the lowest tier are the
    same). Anything else not PROVABLY narrower is :data:`LOOSEN`; a grant extended and nothing
    else changed is :data:`RENEW`; one grant removed and nothing else is :data:`GRANT_REVOKE`."""
    if old == new:
        return NOOP
    changes: list[str] = []
    tiers = _tiers()
    for verb in set(old.raise_to) | set(new.raise_to):
        # no floor IS the lowest tier (apply never lowers), so a raise to it, or dropping it, changes nothing
        rank_o = tiers.index(old.raise_to.get(verb, tiers[0]))
        rank_n = tiers.index(new.raise_to.get(verb, tiers[0]))
        if rank_n != rank_o:
            changes.append("tight" if rank_n > rank_o else "loose")
    # timeout semantics are S4b's: until then no change is provably stricter
    if _canonical(_thaw(old.timeouts)) != _canonical(_thaw(new.timeouts)):
        changes.append("loose")
    for gid in set(old.grants) | set(new.grants):
        if gid not in new.grants:
            changes.append("revoke")
        elif gid not in old.grants:
            changes.append("loose")
        else:
            kind = _grant_change(old.grants[gid], new.grants[gid])
            if kind != "same":
                changes.append(kind)
    if "loose" in changes:
        return LOOSEN
    if "renew" in changes:
        return RENEW if len(changes) == 1 else LOOSEN
    if changes == ["revoke"]:
        return GRANT_REVOKE
    return TIGHTEN if changes else NOOP


# ---------------------------------------------------------------------------------------------------
# replay


def _parse_record(row: _Row) -> dict[str, Any]:
    seq = row.seq
    if not isinstance(row.record, str):
        raise ReplayError(seq, "the record is not text")
    try:
        rec = json.loads(row.record)
    except (ValueError, RecursionError):
        raise ReplayError(seq, "the record is not JSON") from None
    try:
        canonical = isinstance(rec, dict) and _canonical(rec) == row.record
    except RecursionError:
        raise ReplayError(seq, "the record is nested too deeply") from None
    if not canonical:
        raise ReplayError(seq, "the record is not a canonical JSON object")
    if rec.get("format") not in (FORMAT, FORMAT_V2, FORMAT_V3):
        raise ReplayError(seq, f"the record's format is not {FORMAT}, {FORMAT_V2} or {FORMAT_V3}")
    op = rec.get("op")
    if not isinstance(op, str) or op not in _OP_FIELDS:
        raise ReplayError(seq, f"unknown op {op!r}")
    required, optional = _OP_FIELDS[op]
    have = set(rec) - _BASE_FIELDS
    if not _BASE_FIELDS <= set(rec) or not required <= have or not have <= required | optional:
        raise ReplayError(seq, f"a {op} record carries exactly {', '.join(sorted(_BASE_FIELDS | required))}"
                               + (f" (optionally {', '.join(sorted(optional))})" if optional else ""))
    if rec["seq"] != seq or type(rec["seq"]) is not int:
        raise ReplayError(seq, f"the record names seq {rec['seq']!r}")
    return rec


def _enrolled_key(rec: Mapping[str, Any], seq: int, keys: Mapping[str, AuthorityKey],
                  ssh_keygen: Path | str, *, new_link: bool) -> AuthorityKey:
    """The key an ``enrol`` record adds. ``new_link``: the record is being prepared or appended now (the door binds
    whole); otherwise it is a link already in the log, and an enrolment refused only by the door's key-material rule
    is kept as a QUARANTINED key (:func:`_quarantinable`; :meth:`AuthoritySnapshot.quarantined_keys`)."""
    from levain.cockpit.keys import KeyError_, fingerprint_of, normalise_public_key
    fp, public_key, cls = rec["fingerprint"], rec["public_key"], rec["class"]
    if not isinstance(cls, str) or cls not in _ranks():
        raise AuthorityError(f"unknown key class {cls!r}")
    if not isinstance(public_key, str) or not isinstance(fp, str):
        raise AuthorityError("enrol needs a fingerprint and a public key")
    try:
        normal = normalise_public_key(public_key)
        if normal != public_key:
            raise AuthorityError("the public key is not in its normal form (<type> <base64>)")
        computed = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:
        raise AuthorityError(str(exc)) from None
    if computed != fp:
        raise AuthorityError(f"the record's fingerprint {fp} is not the key's ({computed})")
    legacy = rec.get("legacy_enrolled_at")
    if legacy is not None and not isinstance(legacy, str):
        raise AuthorityError("legacy_enrolled_at must be text")
    custody = rec["custody"]
    key_type = public_key.split()[0]
    problem = custody_problem(cls, custody, key_type, legacy=legacy is not None, public_key=public_key)
    if problem is not None and (new_link or not _quarantinable(cls, custody, key_type, legacy is not None,
                                                                public_key)):
        raise AuthorityError(problem)
    old = keys.get(fp)
    if old is not None:
        if not old.active:
            raise AuthorityError(f"{fp} was revoked at seq {old.revoked_seq}; a revoked key stays revoked")
        if old.key_class != cls:
            raise AuthorityError(f"{fp} is enrolled as {old.key_class!r}; a key keeps its class")
        raise AuthorityError(f"{fp} is already enrolled")
    return AuthorityKey(fp, public_key, cls, seq, legacy_enrolled_at=legacy, enrolled_at=rec["at"], custody=custody)


def _passkey_act(rec: Mapping[str, Any], policy: StandingPolicy, fingerprint: str) -> None:
    """Refuse a link a passkey signs unless it is one act of the allow-list (DESIGN_REV3 §7). A passkey signs consent
    answers (its own namespace, the consent queue's check), since K8 a cockpit pending of T2 at most in the phone
    namespace (the broker's check), and, in this log, exactly one of: the revoke of a grant :func:`passkey_revocable`
    takes (which only narrows); the renew of a graduation policy (Phill's Q2 ruling: the phone keeps a policy alive);
    the create of one graduation policy while the live opt-in names it. Every other op is refused, whatever its
    class's rank would allow, and a ``loosen`` with it, so no passkey link carries more than one act. The rule is the
    replay's, so a link built past the phone's ceremonies is held to it."""
    op = rec["op"]
    if op in ("revoke_grant", "renew"):
        gid = rec["grant_id"]
        target = policy.grants.get(gid) if isinstance(gid, str) else None
        if target is None:
            raise AuthorityError(f"a {PASSKEY} key {'revokes' if op == 'revoke_grant' else 'renews'} a grant the "
                                 f"policy holds: no grant {str(gid)[:80]!r}")
        if op == "revoke_grant" and not passkey_revocable(target):
            raise AuthorityError(f"a {PASSKEY} key revokes a consent grant or an inert one only, never grant "
                                 f"{gid[:80]!r}, beyond a graduation policy and the graduation opt-in")
        if op == "renew" and not graduation_grant(target):
            raise AuthorityError(f"a {PASSKEY} key renews a graduation policy only, never grant {gid[:80]!r}")
        return
    refusal = (f"a {PASSKEY} key signs consent answers and grant revokes only, never another authority link, but for "
               "one graduation act per link: a graduation policy's renew, or, named by the live opt-in, one graduation "
               "policy's create")
    if op == "create_grant":
        grant = rec["grant"]
        if not (isinstance(grant, Mapping) and grant.get("kind") == GRADUATION_KIND):
            raise AuthorityError(refusal)
        opt = graduation_opt_in(policy)
        if opt is None or fingerprint not in opt["passkeys"]:
            raise AuthorityError(f"a {PASSKEY} key creates a graduation policy only when the live graduation opt-in "
                                 f"names it; {fingerprint} is not named")
        return
    raise AuthorityError(refusal)


def _graduation_step_checks(old: StandingPolicy, new: StandingPolicy, keys: Mapping[str, AuthorityKey],
                            op: str) -> None:
    """The graduation rules a policy link (op ``op``) is judged by against the state: a graduation policy's expiry
    changes only in a ``renew`` link, which alone holds the lapse rule and the renew caps (inside a loosen that also
    adds a grant, an extension computes to LOOSEN and would bring a lapsed policy back under its old incarnation);
    at most one opt-in in the policy; and an opt-in a link brings in names only active passkey keys whose material
    is a passkey's (none quarantined)."""
    if op != "renew":
        for gid, g in old.grants.items():
            n = new.grants.get(gid)
            if graduation_grant(g) and n is not None and n.get("expires_at") != g.get("expires_at"):
                raise AuthorityError(_GRADUATION_EXPIRY)
    opt_ins = [g for g in new.grants.values() if g.get("kind") == GRADUATION_PHONE_KIND]
    if len(opt_ins) > 1:
        raise AuthorityError("at most one graduation opt-in: revoke the live one first")
    for g in opt_ins:
        if g["id"] in old.grants:
            continue                    # carried: a change to it is refused by the kind (_graduation_phone_change)
        for fp in g["passkeys"]:
            key = keys.get(fp)
            if key is None or not key.active or key.custody != PASSKEY \
                    or custody_mismatch(key.public_key, key.custody) is not None:
                raise AuthorityError(f"the graduation opt-in names {fp}, which is not an active passkey key")


def _link_format(policy: StandingPolicy, rec: Mapping[str, Any], signer_custody: str) -> str:
    """The one format a link carrying ``rec`` (its op fields), signed by a key of ``signer_custody``, is written in
    after ``policy``: :data:`FORMAT_V3` for a link that brings in a graduation policy carrying a ``guard`` (by
    create_grant or loosen, whoever signs it); else :data:`FORMAT_V2` for a passkey's renew or create_grant, and for a
    link that brings in a graduation opt-in; :data:`FORMAT` for every other. :func:`prepare` writes it and the replay
    holds every link to it. A guard enters the log only by a ``/3`` link and never changes after it (a graduation
    policy's terms are fixed), so a replay that stops at ``/3`` never reads a link that carries or alters a guard.
    Known edge, kept: a hand-made ``/1`` loosen that changes a graduation policy's
    terms is refused here and replays on a levain from before ``/2`` as an inert change; that levain never fires on a
    graduation policy (a kind it does not know is inert there), so taking the link changes nothing it enforces."""
    op = rec.get("op")
    if op == "create_grant":
        brought = [rec.get("grant")]
    elif op == "loosen":
        grants = rec.get("policy", {}).get("grants") if isinstance(rec.get("policy"), Mapping) else None
        brought = [g for gid, g in grants.items() if gid not in policy.grants] if isinstance(grants, Mapping) else []
    else:
        brought = []
    if any(isinstance(g, Mapping) and g.get("kind") == GRADUATION_KIND and "guard" in g for g in brought):
        return FORMAT_V3
    if signer_custody == PASSKEY and op in ("renew", "create_grant"):
        return FORMAT_V2
    if any(isinstance(g, Mapping) and g.get("kind") == GRADUATION_PHONE_KIND for g in brought):
        return FORMAT_V2
    return FORMAT


def _step(state: AuthoritySnapshot | None, row: _Row, *, ssh_keygen: Path | str,
          check_signature: bool = True, new_link: bool = False) -> AuthoritySnapshot:
    """The state after ``row``, judged against ``state`` (its predecessor), or :class:`ReplayError`. ``new_link``:
    ``row`` is being prepared or appended now, never a link already in the log; a QUARANTINED key (one a log from
    before the enrol door holds, :func:`_quarantinable`) is then neither enrolled nor taken as a signer."""
    expected = 1 if state is None else state.seq + 1
    seq = row.seq
    if type(seq) is not int or seq != expected:
        raise ReplayError(expected, f"expected seq {expected}, found {seq!r} (a link is missing or out of order)")
    for name in _TEXT_COLUMNS:
        if not isinstance(getattr(row, name), str):
            raise ReplayError(seq, f"{name} is not UTF-8 text")
    prev = "" if state is None else state.head_hash
    if row.prev_hash != prev:
        raise ReplayError(seq, "prev_hash is not the predecessor's hash (the chain is broken)")
    if not isinstance(row.record, str) or row.hash != _link_hash(prev, row.record):
        raise ReplayError(seq, "hash does not match prev_hash + record")
    rec = _parse_record(row)
    op = rec["op"]
    # `at` is an ORDERING constraint, never a grant of authority (amends DESIGN §3.1): a link is never
    # earlier than its predecessor, so a future-dated inserted link wedges every later append visibly
    # (an append is stamped now) instead of silently widening a clock-bounded rule such as renew's
    try:
        at = _parse_stamp(rec["at"], "at")
    except AuthorityError as exc:
        raise ReplayError(seq, str(exc)) from None
    if state is not None and state.head_at is not None and at < state.head_at:
        raise ReplayError(seq, f"at {rec['at']} is earlier than its predecessor's "
                               f"{_stamp(state.head_at)}; the log's times never go back")
    if state is None:
        if not isinstance(rec["store_id"], str) or not _STORE_ID.fullmatch(rec["store_id"]):
            raise ReplayError(seq, "the genesis store_id is malformed")
    elif rec["store_id"] != state.store_id:
        raise ReplayError(seq, f"the record names store {rec['store_id']!r}, not {state.store_id!r}")
    if not isinstance(row.signer_fp, str) or not _FP.fullmatch(row.signer_fp):
        raise ReplayError(seq, "signer_fp is malformed")

    ranks = _ranks()
    keys = {} if state is None else dict(state.keys)
    policy = genesis_policy() if state is None else state.policy
    grant_seqs = {} if state is None else dict(state.grant_seqs)
    grant_born = {} if state is None else dict(state.grant_born)
    quarantined_history = False
    try:
        if state is None:
            # TOFU genesis (§3.2): the first link enrols a laptop-presence key, signed by that key
            if op != "enrol" or rec["class"] != LAPTOP:
                raise AuthorityError(f"the first link must enrol a {LAPTOP} key")
            if row.signer_fp != rec["fingerprint"]:
                raise AuthorityError("the first link must be signed by the key it enrols")
            if rec["format"] != FORMAT:
                raise AuthorityError(f"the first link carries {rec['format']}; it is written {FORMAT}")
            signer = _enrolled_key(rec, seq, keys, ssh_keygen, new_link=new_link)
            keys[signer.fingerprint] = signer
            # a quarantined genesis key (only ever history: _enrolled_key refuses it on a new link) signed this
            # link: re-checked below as the passkey its material shows
            quarantined_history = custody_mismatch(signer.public_key, signer.custody) is not None
        else:
            found = keys.get(row.signer_fp)
            if found is None:
                raise AuthorityError(f"signer {row.signer_fp} is not enrolled")
            signer = found
            if not signer.active:
                raise AuthorityError(f"signer {row.signer_fp} was revoked at seq {signer.revoked_seq}")
            # the custody that picks the rule below and the passkey checks after the signature is the stored one,
            # held equal to the one the signer's key material shows (an enrolment from before the door at
            # custody_problem could hold a passkey at custody fido). The one exception is a QUARANTINED key's link
            # already in the log: WebAuthn material at another custody, so the link is re-checked below as the
            # passkey its material shows (user verification, the origin rule below). A new link it signs is refused
            why = custody_mismatch(signer.public_key, signer.custody)
            if why is not None and (new_link or not is_passkey_material(signer.public_key)):
                raise AuthorityError(why + ("; the key is quarantined: it signs nothing new" if new_link and
                                            is_passkey_material(signer.public_key) else ""))
            quarantined_history = why is not None
            # known edge, kept: a quarantined key's links already in the log meet UV + origin only, not the op rule
            # below (stored custody fido/unstated), since holding them to it refuses existing logs (Phill's (a))
            if signer.custody == PASSKEY:
                _passkey_act(rec, policy, signer.fingerprint)
            need = _apply_op(rec, seq, state, keys, ssh_keygen, new_link=new_link)
            if isinstance(need, StandingPolicy):
                delta = classify(policy, need)
                if delta == NOOP:
                    raise AuthorityError(f"this {op} changes nothing")
                if delta not in _OP_DELTAS[op]:
                    raise AuthorityError(f"a {op} link computes to a {delta} change")
                rank = _DELTA_RANK[delta]
                grant_seqs = {gid: (grant_seqs.get(gid, seq) if gid in policy.grants and _canonical(
                    _thaw(policy.grants[gid])) == _canonical(_thaw(g)) else seq) for gid, g in need.grants.items()}
                grant_born = {gid: (grant_born.get(gid, seq) if gid in policy.grants else seq) for gid in need.grants}
                if signer.custody == PASSKEY and op == "create_grant":
                    rank = GRADUATION_OPT_IN_RANK       # _passkey_act let it through only as the opt-in names it
                policy = need
            else:
                rank = need
            if ranks[signer.key_class] < rank:
                raise AuthorityError(f"a {signer.key_class} key cannot sign this {op}")
            if isinstance(need, StandingPolicy):
                _graduation_step_checks(state.policy, need, keys, op)
            want = _link_format(state.policy, rec, signer.custody)
            if rec["format"] != want:
                raise AuthorityError(f"this {op} link carries {rec['format']}; a link like it is written {want}")
    except AuthorityError as exc:
        if isinstance(exc, (ReplayError, VerifierUnavailable)):
            raise
        raise ReplayError(seq, str(exc)) from None
    except (TypeError, ValueError, KeyError, UnicodeError, OverflowError, RecursionError) as exc:
        # a field of a shape no rule above anticipated: still a refusal at this seq, never an escape
        detail = f"missing key {exc.args[0]!r}" if isinstance(exc, KeyError) and exc.args else type(exc).__name__
        raise ReplayError(seq, f"a malformed field ({detail})") from None

    if check_signature:
        ok = verify_link_sig(_message(prev, row.record), row.signature, public_key=signer.public_key,
                             fingerprint=signer.fingerprint, ssh_keygen=ssh_keygen)
        if not ok:
            raise ReplayError(seq, f"the signature does not verify as {signer.fingerprint}")
        if signer.custody == PASSKEY or quarantined_history:
            # the checks ssh-keygen does not make on a WebAuthn signature (user verification, an origin that is
            # the key's own https name, not cross-origin), run at every replay, and on a quarantined key's
            # link as on any passkey's: the stored signature carries its flags and origin. A revoke_grant may come
            # from that name on any port (a cockpit behind a proxy, Phill 10-24 "2a"); every other link, exactly
            from levain.consent.passkey import authority_signature_problem

            problem = authority_signature_problem(row.signature, signer.public_key, revoke=op == "revoke_grant")
            if problem is not None:
                raise ReplayError(seq, f"the passkey signature is refused: {problem}"
                                       + (f" (signed by {signer.fingerprint}, quarantined: WebAuthn material stored "
                                          f"at custody {signer.custody})" if quarantined_history else ""))
    hashes = (() if state is None else state._hashes) + (row.hash,)
    signers = (() if state is None else state.link_signers) + (row.signer_fp,)
    return AuthoritySnapshot(rec["store_id"], seq, row.hash, MappingProxyType(keys), policy, hashes, head_at=at,
                             grant_seqs=MappingProxyType(grant_seqs), grant_born=MappingProxyType(grant_born),
                             link_signers=signers)


def _apply_op(rec: Mapping[str, Any], seq: int, state: AuthoritySnapshot, keys: dict[str, AuthorityKey],
              ssh_keygen: Path | str, *, new_link: bool) -> int | StandingPolicy:
    """Applies a key op to ``keys`` and returns the signer rank it needs, or returns the new policy for
    a policy op (whose rank follows from its computed delta)."""
    op = rec["op"]
    ranks = _ranks()
    if op == "enrol":
        key = _enrolled_key(rec, seq, keys, ssh_keygen, new_link=new_link)
        keys[key.fingerprint] = key
        return ranks[LAPTOP]
    if op == "revoke_key":
        fp = rec["fingerprint"]
        target = keys.get(fp) if isinstance(fp, str) else None
        if target is None:
            raise AuthorityError(f"no enrolled key {fp!r}")
        if not target.active:
            raise AuthorityError(f"{fp} is already revoked (at seq {target.revoked_seq})")
        since = rec.get("compromised_since")
        if since is not None and (type(since) is not int or not target.enrolled_seq <= since <= state.seq):
            raise AuthorityError(f"compromised_since must be a seq between {target.enrolled_seq} and {state.seq}")
        others = [k for k in keys.values() if k.fingerprint != fp]
        # A new link counts the laptop-presence keys that can sign (signing_laptops: a QUARANTINED one signs nothing
        # new, so it is no survivor). A link already in the log keeps the count the replay that took it made (any
        # unrevoked laptop-presence key): a log from before the enrol door still loads, and doctor names its dead end
        if target.key_class == LAPTOP and since is None and not (
                signing_laptops(others) if new_link else [k for k in others if k.active and k.key_class == LAPTOP]):
            held = ", ".join(k.fingerprint for k, _why in quarantined(others) if k.key_class == LAPTOP)
            raise AuthorityError(f"{fp} is the last unrevoked {LAPTOP} key"
                                 + (f" that can sign (quarantined, signing nothing new: {held})" if held else "")
                                 + "; enrol another first, or revoke it with compromised_since if it is lost or stolen")
        keys[fp] = replace(target, revoked_seq=seq, compromised_since=since)
        return ranks[target.key_class]
    return _apply_policy_op(state.policy, rec)


def _db_identity(conn: sqlite3.Connection) -> str:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return os.path.realpath(path) if path else f"memory:{id(conn)}"
    return f"memory:{id(conn)}"


@dataclass(frozen=True)
class _Entry:
    """A verified chain: the state after every link, and the digest of every row."""
    states: tuple[AuthoritySnapshot, ...]
    digests: tuple[str, ...]

    @property
    def snapshot(self) -> AuthoritySnapshot:
        return self.states[-1]


class ReplayCache:
    """Per-process verified chains, keyed by store: the state after each link and the digest of each
    row. A log whose rows match the cached chain verifies only its suffix. A log that is a
    digest-matching PREFIX of the cached chain is served its own head from the cache: that is a reader
    whose transaction began before a concurrent append (a busy cockpit), and its view is a state this
    process already verified. A log that differs from the chain at or below a seq both hold is replayed
    in full: if it verifies it replaces the cached chain (a restore, then an anchor reset), and if it
    does not it is refused (:class:`HeadBelowCache`). A log truncated to a prefix reads as that prefix;
    the anchor, not this cache, is what refuses a rollback (:func:`check_anchor`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}

    def get(self, conn: sqlite3.Connection, store_id: str, seq: int, head_hash: str) -> AuthoritySnapshot | None:
        """The cached snapshot for exactly this head, or None."""
        with self._lock:
            entry = self._entries.get((_db_identity(conn), store_id))
        if entry is not None and (entry.snapshot.seq, entry.snapshot.head_hash) == (seq, head_hash):
            return entry.snapshot
        return None

    def _load(self, key: tuple[str, str]) -> _Entry | None:
        with self._lock:
            return self._entries.get(key)

    def _store(self, key: tuple[str, str], entry: _Entry) -> None:
        """Keep the longer of two agreeing chains; a verified chain that disagrees with the cached one
        below the shorter head replaces it (the last verified chain stands, see :func:`_replay_entry`)."""
        with self._lock:
            old = self._entries.get(key)
            both = 0 if old is None else min(len(old.digests), len(entry.digests))
            if old is None or entry.snapshot.seq > old.snapshot.seq \
                    or entry.digests[:both] != old.digests[:both]:
                self._entries[key] = entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


PROCESS_CACHE = ReplayCache()


def _genesis_store_id(rows: list[_Row]) -> str:
    first = rows[0]
    try:
        rec = json.loads(first.record) if isinstance(first.record, str) else None
    except ValueError:
        rec = None
    store_id = rec.get("store_id") if isinstance(rec, dict) else None
    return store_id if isinstance(store_id, str) else ""


def _replay_entry(conn: sqlite3.Connection, cache: ReplayCache, ssh_keygen: Path | str) -> _Entry | None:
    rows = _rows(conn)
    if not rows:
        return None
    key = (_db_identity(conn), _genesis_store_id(rows))
    entry = cache._load(key)
    digests = tuple(r.digest() for r in rows)
    states: tuple[AuthoritySnapshot, ...] = ()
    diverged: str | None = None
    if entry is not None:
        n = len(entry.digests)
        both = min(n, len(rows))
        if digests[:both] != entry.digests[:both]:
            # nothing cached is reusable: replay from genesis, and the result replaces the cached chain
            diverged = f"the chain this process verified (to seq {n}) at or below seq {both}"
        elif len(rows) <= n:
            return _Entry(entry.states[:len(rows)], digests)
        else:
            states = entry.states
    state = states[-1] if states else None
    grown = list(states)
    try:
        for row in rows[len(states):]:
            state = _step(state, row, ssh_keygen=ssh_keygen)
            grown.append(state)
    except ReplayError as exc:
        if diverged is None:
            raise
        raise HeadBelowCache(f"the authority log differs from {diverged}; it was rewritten ({exc})") from None
    out = _Entry(tuple(grown), digests)
    cache._store(key, out)
    return out


def _replay(conn: sqlite3.Connection, cache: ReplayCache, ssh_keygen: Path | str) -> AuthoritySnapshot | None:
    entry = _replay_entry(conn, cache, ssh_keygen)
    return None if entry is None else entry.snapshot


def snapshot(conn: sqlite3.Connection, *, cache: ReplayCache | None = None,
             ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """The authority as a verified replay of ``authority_log`` in ``conn``'s current snapshot (run it
    inside the caller's read transaction so it and the caller's other reads see one head)."""
    state = _replay(conn, PROCESS_CACHE if cache is None else cache, ssh_keygen)
    if state is None:
        raise NotInitialised(f"the authority log is empty: {conn_init_hint(conn)}")
    return state


def log_head(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """``(seq, hash)`` of the log's last stored link, unverified (one indexed read, no replay); None when the log is
    empty. Equal to a verified snapshot's ``(seq, head_hash)`` exactly when no link was appended at, or replaced in,
    the head since that replay: a caller that verified a snapshot outside a lock compares only this inside it. A row
    rewritten below an unchanged head row is not seen here; the next full replay and the anchor refuse it."""
    row = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
    return None if row is None else (row[0], row[1])


# ---------------------------------------------------------------------------------------------------
# append


@dataclass(frozen=True)
class UnsignedLink:
    """A link ready to sign: ``message`` is what the signer signs (namespace :data:`NAMESPACE`)."""
    seq: int
    prev_hash: str
    record: str
    signer_fp: str

    @property
    def message(self) -> bytes:
        return _message(self.prev_hash, self.record)


def _require_aware(now: Any) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AuthorityError("`now` must be an aware datetime; a naive one has no zone")


def consent_root_problem(workspace: str, *, home: str | os.PathLike[str]) -> str | None:
    """Why the directory ``workspace`` names, on THIS machine, cannot be a consent grant's root (None when it can):
    it is ``/``, or ``home`` (this machine's home directory) or above it. Read at append and again at every
    decision, since a log written elsewhere names another home."""
    home = os.fspath(home)
    if workspace == "/" or any(_path_within(h, workspace) for h in {os.path.normpath(home), os.path.realpath(home)}):
        return f"the workspace {workspace[:200]} is / or the home directory or above it; name a project directory"
    return None


def consent_mcp_tool_problem(tool: str) -> str | None:
    """Why a consent grant may not name ``tool``, when it is an MCP tool (``mcp__<server>__<tool>``, the server name
    ending at its first ``__``) whose own name is not in :data:`CONSENT_MCP_READ_ONLY_TOOLS`, or any other name with
    a ``__`` in it (an agent that spelled its MCP tools otherwise would fail closed); ``None`` otherwise."""
    if not isinstance(tool, str) or "__" not in tool:
        return None     # a built-in tool (Claude Code's and Codex's names carry no "__")
    if tool.startswith("mcp__") and tool[len("mcp__"):].partition("__")[2] in CONSENT_MCP_READ_ONLY_TOOLS:
        return None
    return (f"{tool[:200]!r} is an MCP tool levain does not know to be read-only: MCP does not say which of a "
            "tool's fields are paths, so a grant could not keep its writes inside the workspace; its requests go "
            "to you (a grant may name the filesystem server's read tools, such as mcp__<server>__read_text_file)")


def _shell_refusal(what: str, tool: str) -> str:
    return (f"{what} names the shell tool {tool[:200]!r}: a shell command is never parsed, so a grant for it would "
            "allow every command; shell requests go to you")


def _consent_shell_name(tool: str) -> bool:
    """Whether ``tool`` is a shell by name: it, or the name after any ``__`` in it (an MCP tool's own name after
    ``mcp__<server>__``, whatever ``__`` the server name holds), is in :data:`CONSENT_SHELL_TOOLS`, any case. The
    single judgement of a shell name for a grant, at append and at decision; a tool's fields are never read."""
    name = tool.lower()
    return name in CONSENT_SHELL_TOOLS or any(
        name[i + 2:] in CONSENT_SHELL_TOOLS for i in range(len(name) - 1) if name.startswith("__", i))


def consent_tool_problem(agent: str, tool: str) -> str | None:
    """Why a consent grant for ``agent`` may not name ``tool`` (``None`` when it may): a shell tool
    (:func:`_consent_shell_name`: a name in :data:`CONSENT_SHELL_TOOLS`, any case, bare or as an MCP tool's own
    name), an MCP tool :func:`consent_mcp_tool_problem` refuses, or any other
    name that is not in :data:`CONSENT_GRANTABLE_TOOLS` for ``agent``. Read at append and at every decision."""
    if not isinstance(tool, str):
        return "a grant's tool must be a name"
    if _consent_shell_name(tool):
        return _shell_refusal("grant", tool)
    if "__" in tool:
        return consent_mcp_tool_problem(tool)
    known = CONSENT_GRANTABLE_TOOLS.get(agent) if isinstance(agent, str) else None
    if known is not None and tool in known:
        return None
    if known is None:
        return f"{tool[:200]!r}: {str(agent)[:40]} is not a consent agent; its requests go to you"
    return (f"{tool[:200]!r} is not a tool levain knows for {str(agent)[:40]}: a grant may name only a tool whose "
            f"request levain bounds to the workspace ({', '.join(sorted(known))}, or an MCP read tool); its "
            "requests go to you")


def _consent_tool_check(grants: list[Any], after: str = "") -> None:
    """At append, every tool a consent grant a link brings in or extends names is one :func:`consent_tool_problem`
    allows for the grant's agent (run on the grants of a consent kind, :data:`_GRANT_KINDS`); ``after`` is added to
    the refusal."""
    for grant in grants:
        if not isinstance(grant, Mapping):
            continue
        tools = grant.get("tools")
        for tool in tools if isinstance(tools, (list, tuple)) else ():
            problem = consent_tool_problem(grant.get("agent"), tool)
            if problem is not None:
                raise AuthorityError(problem + after)


def consent_grant_tool_problem(grant: Any, tool: Any) -> str | None:
    """Why ``grant`` may not name ``tool`` (``None`` when it may): :func:`consent_tool_problem` for the grant's agent,
    except that an MCP tool a consent-mcp grant declares (:func:`consent_mcp_declaration`) may be named whether or not
    it is on :data:`CONSENT_MCP_READ_ONLY_TOOLS`. A shell name (:func:`_consent_shell_name`, an MCP tool's own name
    included) is refused whatever is declared. Read at append (for a consent-mcp grant) and at every decision."""
    if isinstance(tool, str) and not _consent_shell_name(tool) \
            and consent_mcp_declaration(grant, tool) is not None:
        return None
    return consent_tool_problem(grant.get("agent") if isinstance(grant, Mapping) else None, tool)


def _consent_mcp_tool_check(grants: list[Any], after: str = "") -> None:
    """At append, every tool a consent-mcp grant a link brings in or extends names is one
    :func:`consent_grant_tool_problem` allows; ``after`` is added to the refusal."""
    for grant in grants:
        if not isinstance(grant, Mapping):
            continue
        tools = grant.get("tools")
        for tool in tools if isinstance(tools, (list, tuple)) else ():
            problem = consent_grant_tool_problem(grant, tool)
            if problem is not None:
                raise AuthorityError(problem + after)


def _consent_home_check(grants: list[Any]) -> None:
    """At append, every consent grant's workspace is an existing directory whose realpath is exactly the signed
    string (no symlink anywhere on it, so what was signed is what the criteria resolve), and not a refused root
    (run on the grants of a consent kind, :data:`_GRANT_KINDS`)."""
    for grant in grants:
        if not isinstance(grant, Mapping):
            continue
        ws = grant.get("workspace")
        if not isinstance(ws, str):
            continue                                    # the replay's own shape check refuses it
        try:
            canonical = os.path.isdir(ws) and os.path.realpath(ws) == ws
        except (OSError, ValueError):
            canonical = False
        if not canonical:
            raise AuthorityError(f"the workspace {ws[:200]} must be an existing directory named by its real path "
                                 "(no symlink on it)")
        problem = consent_root_problem(ws, home=os.path.expanduser("~"))   # append time, off the core path
        if problem is not None:
            raise AuthorityError(problem)


def _grant_append_checks(grants: list[Any]) -> None:
    for grant in grants:
        if isinstance(grant, Mapping):
            for check in _grant_kind(grant).append_checks:
                check([grant])


def _append_only_checks(state: AuthoritySnapshot, rec: Mapping[str, Any], now: datetime) -> None:
    """The rules a replay cannot hold because they need the appender's clock (§3.3a, §6), or its home
    directory: a consent grant's workspace is never the home directory or above it (that grants every
    workspace under it), judged for every consent grant a create or a loosen brings in or changes; and every
    other per-kind append rule (:data:`_GRANT_KINDS`), a kind this levain does not know refused; and the setup
    record's one-live rule (:func:`_egress_key_append_check`)."""
    _egress_key_append_check(state, rec, now)
    if rec["op"] == "create_grant":
        _grant_append_checks([rec["grant"]])
    elif rec["op"] == "loosen":
        policy = rec["policy"]
        grants = policy.get("grants", {}) if isinstance(policy, Mapping) else {}
        old = state.policy.grants
        _grant_append_checks([g for gid, g in grants.items()
                              if gid not in old or _canonical(_thaw(old[gid])) != _canonical(_thaw(g))])
    if rec["op"] != "renew":
        return
    grant = state.policy.grants.get(rec["grant_id"])
    if grant is None:
        raise AuthorityError(f"no grant {rec['grant_id']!r}")
    kind = _renewable_kind(grant, rec["grant_id"])
    after = (f"; grant {str(rec['grant_id'])[:80]!r} was signed before this rule: revoke it "
             "(levain consent grant revoke) and create a new one")
    for check in kind.renew_checks:
        check([grant], after)       # a grant signed before a kind's rule is not extended past it
    signed = _parse_stamp(rec["signed_at"], "signed_at")
    if abs(now - signed) > _RENEW_SKEW:
        raise AuthorityError("a renew's signed_at must be within 10 minutes of now")
    if _parse_stamp(grant["expires_at"], "the grant's expires_at") <= now:
        raise AuthorityError(f"grant {rec['grant_id']!r} has expired; a lapsed grant is never renewed, only "
                             "created again")
    if _parse_stamp(rec["expires_at"], "expires_at") - now > _RENEW_MAX:      # never now + 30 days: overflow
        raise AuthorityError("a renew may extend a grant at most 30 days from now")
    cap = kind.max_life
    if cap is not None and _parse_stamp(rec["expires_at"], "expires_at") - now > cap:
        raise AuthorityError(f"a renew may extend this grant at most {cap.days} days from now")


def prepare(conn: sqlite3.Connection, op: Mapping[str, Any], *, signer_fp: str, now: datetime,
            store_id: str | None = None, cache: ReplayCache | None = None,
            ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> UnsignedLink:
    """The next link carrying ``op`` (its op fields, ``op`` included), checked against the current head
    by every rule but the signature, so a doomed link is refused before anyone touches a key.
    ``store_id`` is for the genesis link only (a fresh one is drawn when it is None)."""
    _require_aware(now)
    if not isinstance(op, Mapping):
        raise AuthorityError("an op is a mapping of its fields, `op` among them")
    if "op" not in op or set(op) & (_BASE_FIELDS - {"op"}):
        raise AuthorityError("an op names `op` and its own fields, never format/store_id/seq/at")
    state = _replay(conn, PROCESS_CACHE if cache is None else cache, ssh_keygen)
    if state is None:
        sid = store_id if store_id is not None else secrets.token_hex(16)
    else:
        if store_id is not None and store_id != state.store_id:
            raise AuthorityError(f"this log's store is {state.store_id!r}")
        sid = state.store_id
    seq = 1 if state is None else state.seq + 1
    prev = "" if state is None else state.head_hash
    signer = None if state is None else state.key(signer_fp)
    # an unknown signer is refused by _step below; the format it would carry does not matter
    fmt = FORMAT if state is None or signer is None else _link_format(state.policy, op, signer.custody)
    try:
        record = _canonical({**op, "format": fmt, "store_id": sid, "seq": seq, "at": _stamp(now)})
        row = _Row(seq, prev, record, signer_fp, "", _link_hash(prev, record))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        # not JSON, a lone surrogate (no UTF-8 form), or nested too deeply
        raise AuthorityError(f"the op has no canonical UTF-8 form ({type(exc).__name__})") from None
    _step(state, row, ssh_keygen=ssh_keygen, check_signature=False, new_link=True)
    if state is not None:
        _append_only_checks(state, json.loads(record), now)
    return UnsignedLink(seq, prev, record, signer_fp)


def append(conn: sqlite3.Connection, link: UnsignedLink, signature: str, *, now: datetime,
           anchor: Path | None, cache: ReplayCache | None = None,
           ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """Append a signed link in one ``BEGIN IMMEDIATE``: replay to the head, refuse a moved head
    (:class:`StaleHead`), verify the link against that state as a replay would, apply the append-only
    clock checks, insert. ``conn`` must be in autocommit mode with no open transaction.

    ``anchor`` is required: the anchor file's path, or an explicit None for an unanchored store. With
    one, a log behind it refuses (an empty log beside an existing anchor included), and after the commit
    the anchor advances. Once committed the link stands: if the anchor cannot then be advanced, the
    snapshot comes back with ``anchor_warning`` set rather than as a failed append."""
    _require_aware(now)
    if conn.in_transaction:
        raise AuthorityError("append owns its transaction; the connection has one open")
    cache = PROCESS_CACHE if cache is None else cache
    conn.execute("BEGIN IMMEDIATE")
    try:
        entry = _replay_entry(conn, cache, ssh_keygen)
        state = None if entry is None else entry.snapshot
        if anchor is not None:
            current = read_anchor(anchor)
            if state is None and current is not None:
                # not a reset: an empty log holds no key a reset could verify against (verify_anchor_reset)
                raise AnchorError(f"the authority log is empty but its anchor names store {current.store_id} at "
                                  f"seq {current.seq}; a new genesis here would fork it. If this store was "
                                  f"emptied on purpose, move {anchor} aside, then run "
                                  f"{init_hint(anchor_data_root(anchor))}")
            if state is not None:
                check_anchor(state, current, path=anchor)
        head = (0, "") if state is None else (state.seq, state.head_hash)
        if head != (link.seq - 1, link.prev_hash):
            raise StaleHead(f"the authority log's head moved to seq {head[0]} since this link was prepared")
        row = _Row(link.seq, link.prev_hash, link.record, link.signer_fp, signature,
                   _link_hash(link.prev_hash, link.record))
        new = _step(state, row, ssh_keygen=ssh_keygen, new_link=True)
        if state is not None:
            _append_only_checks(state, json.loads(link.record), now)
        conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (row.seq, row.prev_hash, row.record, row.signer_fp, row.signature, row.hash))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    prior = _Entry((), ()) if entry is None else entry
    cache._store((_db_identity(conn), new.store_id),
                 _Entry(prior.states + (new,), prior.digests + (row.digest(),)))
    if anchor is not None:
        try:
            advance_anchor(anchor, new)
        except (AnchorError, OSError) as exc:
            return replace(new, anchor_warning=f"committed at seq {new.seq}; the anchor is behind ({exc})")
    return new


def sign_and_append(conn: sqlite3.Connection, op: Mapping[str, Any], *, signer_fp: str,
                    sign: Callable[[bytes], str], clock: Callable[[], datetime], anchor: Path | None,
                    store_id: str | None = None, cache: ReplayCache | None = None,
                    ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """:func:`prepare`, then ``sign(message)`` OUTSIDE any transaction (a touch can take seconds),
    then :func:`append` (``anchor`` as there: required, None for an unanchored store). ``clock`` is read
    once for prepare and again AFTER the signature, so append's clock checks (a renew of a grant that
    lapsed during the touch) judge the time of the commit, not the time the touch began."""
    link = prepare(conn, op, signer_fp=signer_fp, now=clock(), store_id=store_id, cache=cache,
                   ssh_keygen=ssh_keygen)
    signature = sign(link.message)
    return append(conn, link, signature, now=clock(), cache=cache, ssh_keygen=ssh_keygen, anchor=anchor)


# ---------------------------------------------------------------------------------------------------
# the anchor (§3.8)


@dataclass(frozen=True)
class Anchor:
    store_id: str
    seq: int
    head_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "seq": self.seq, "head_hash": self.head_hash}


# the one word of a printed remedy the operator fills in: which of their laptop-presence key files signs (levain.doctor
# prints the same word)
KEY_WORD = "YOUR_LAPTOP_PRESENCE_KEY"


def reset_hint(data_root: Path | str) -> str:
    """The remedy an anchor refusal of a same-store rollback prints: the whole `levain cockpit anchor reset` argv,
    this data root filled in, so the operator runs it as printed with their key file in place of :data:`KEY_WORD`
    (tests/test_cockpit_anchor_reset.py runs each one through levain.cli's parser)."""
    return f"`levain cockpit anchor reset --signing-key {KEY_WORD} --data-root {shlex.quote(str(data_root))}`"


def init_hint(data_root: Path | str, signing_key: Path | str = KEY_WORD) -> str:
    """The remedy for an empty log: the whole `levain cockpit ledger init` argv, this data root filled in (and the
    signing key, where the caller holds one: `levain cockpit key create` has just made it)."""
    argv = ["levain", "cockpit", "ledger", "init", "--signing-key", str(signing_key), "--data-root", str(data_root)]
    return f"`{shlex.join(argv)}`"


def conn_data_root(conn: sqlite3.Connection) -> Path | None:
    """The data root whose ``cockpit.db`` is ``conn``'s main database, or None for a database with no file
    (``:memory:``, a temp database): there the code cannot name a cockpit."""
    file = conn.execute("PRAGMA database_list").fetchone()[2]     # (seq, name, file): the main database first
    return Path(file).parent if file else None


def conn_init_hint(conn: sqlite3.Connection) -> str:
    """:func:`init_hint` for ``conn``'s own data root. A fileless database has no root to name, so it says in prose
    what the command needs and pastes nothing."""
    root = conn_data_root(conn)
    if root is None:
        return ("levain cockpit ledger init, with its --signing-key (a laptop-presence key) and --data-root "
                "(the cockpit directory)")
    return init_hint(root)


def anchor_path(data_root: Path | str) -> Path:
    """``<data_root>/anchor/authority.anchor``: its own directory, so a backup can leave it out while it
    takes the store (:func:`_anchor_dir`)."""
    return Path(data_root) / ANCHOR_DIR / ANCHOR_NAME


def anchor_data_root(path: Path | str) -> Path:
    """The data root whose anchor is ``path`` (:func:`anchor_path` inverted)."""
    return Path(path).parent.parent


def _exclude_from_time_machine(directory: Path) -> None:
    """Set the sticky Time Machine exclusion on ``directory``, best effort: off macOS, or when the
    attribute cannot be set, the anchor works as before and nothing is recorded."""
    if sys.platform != "darwin":
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.setxattr
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                       ctypes.c_int]
        fn(os.fsencode(directory), _TM_EXCLUDE_ATTR, _TM_EXCLUDE_VALUE, len(_TM_EXCLUDE_VALUE), 0, 0)
    except (OSError, AttributeError):
        pass


def _anchor_dir(path: Path) -> None:
    """Make the anchor's directory and exclude it from Time Machine. The exclusion is on the directory,
    never the file: :func:`_write_anchor`'s rename replaces the file's inode on every advance."""
    directory = Path(path).parent
    directory.mkdir(mode=0o700, exist_ok=True)
    _exclude_from_time_machine(directory)


def read_anchor(path: Path) -> Anchor | None:
    """The anchor at ``path``; None when there is no file. Anything unreadable refuses."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AnchorError(f"the authority anchor {path} cannot be read ({type(exc).__name__}); "
                          f"after checking the log, run {reset_hint(anchor_data_root(path))}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("format") != ANCHOR_FORMAT:
            raise ValueError
        store_id, seq, head = data["store_id"], data["seq"], data["head_hash"]
        if not isinstance(store_id, str) or type(seq) is not int or seq < 1 or not isinstance(head, str) \
                or not _SHA256_HEX.fullmatch(head):
            raise ValueError
    except (ValueError, KeyError, AttributeError, UnicodeDecodeError, RecursionError):
        raise AnchorError(f"the authority anchor {path} is unreadable; after checking the log, run "
                          f"{reset_hint(anchor_data_root(path))}") from None
    return Anchor(store_id, seq, head)


def foreign_anchor(snap: AuthoritySnapshot, anchor: Mapping[str, Any] | Anchor | None,
                   path: Path | str) -> ForeignAnchor | None:
    """The refusal of an anchor at ``path`` that names another store than ``snap``'s, or None (no anchor, an
    unreadable one, or this store's). Its remedy is the file to check or move, never a reset: a reset is for a
    rollback of the same store (DESIGN §3.8), and signing over another store's anchor with this log's keys would
    let a substituted log adopt the anchor that exists to refuse it."""
    store = anchor.store_id if isinstance(anchor, Anchor) else None if anchor is None else anchor.get("store_id")
    if store is None or store == snap.store_id:
        return None
    return ForeignAnchor(f"the authority anchor {path} names store {store}, but this log is store {snap.store_id}: "
                         f"the anchor belongs to another store, and an anchor reset refuses it. Check that "
                         f"{anchor_data_root(path)} holds the store you meant; if you replaced that store on purpose, "
                         f"move {path} aside (the next signed link writes this store's anchor)")


def check_anchor(snap: AuthoritySnapshot, anchor: Anchor | None, *, path: Path | str) -> None:
    """Refuse a log behind ``anchor`` (the anchor file at ``path``): another store (:class:`ForeignAnchor`),
    fewer links, or another hash at the anchor's seq."""
    if anchor is None:
        return
    foreign = foreign_anchor(snap, anchor, path)
    if foreign is not None:
        raise foreign
    here = f"log store {snap.store_id} at seq {snap.seq} ({snap.head_hash[:12]})"
    there = f"anchor store {anchor.store_id} at seq {anchor.seq} ({anchor.head_hash[:12]})"
    if snap.seq < anchor.seq or snap.hash_at(anchor.seq) != anchor.head_hash:
        raise AnchorError(f"the authority log is behind its anchor: {here}, {there}. If this store was "
                          f"restored on purpose, run {reset_hint(anchor_data_root(path))}")


def _write_anchor(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".authority-anchor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_canonical(payload) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@contextmanager
def _anchor_lock(path: Path) -> Iterator[None]:
    """An exclusive flock on ``<anchor>.lock``: every read-compare-write of the anchor runs under it, so
    two appenders (the cockpit server and the CLI) serialise their advances."""
    path = Path(path)
    _anchor_dir(path)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def advance_anchor(path: Path, snap: AuthoritySnapshot) -> Anchor:
    """Check the anchor at ``path`` against ``snap`` and move it up to ``snap``'s head; call only after
    the commit that produced ``snap``, so the anchor never leads the log. An anchor of this store already
    past ``snap`` (a later append's advance landed first) is left where it is: the anchor never moves
    down, and a stale advance is a no-op that returns the anchor as it stands."""
    with _anchor_lock(path):
        current = read_anchor(path)
        if current is not None and current.store_id == snap.store_id and current.seq > snap.seq:
            return current
        check_anchor(snap, current, path=path)
        new = Anchor(snap.store_id, snap.seq, snap.head_hash)
        if current != new:
            _write_anchor(path, {"format": ANCHOR_FORMAT, **new.as_dict()})
        return new


def reset_message(store_id: str, old_anchor: Mapping[str, Any] | None, new_seq: int, new_head: str) -> bytes:
    """What the operator signs (namespace :data:`RESET_NAMESPACE`) to move an anchor to ``new_head``."""
    return _canonical({"format": RESET_FORMAT, "store_id": store_id, "old_anchor": old_anchor,
                       "new_head": {"seq": new_seq, "head_hash": new_head}}).encode("utf-8")


def _old_anchor_view(path: Path) -> Mapping[str, Any] | None:
    """The anchor a reset replaces: its fields when readable, else the digest of its bytes."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        current = read_anchor(path)
    except AnchorError:
        return {"sha256": hashlib.sha256(raw).hexdigest()}
    return None if current is None else current.as_dict()


def verify_anchor_reset(snap: AuthoritySnapshot, old_anchor: Mapping[str, Any] | None, *, signer_fp: str,
                        signature: str, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> None:
    """Refuse unless ``signature`` is a laptop-presence signature over the reset of ``old_anchor`` to
    ``snap``'s head, by a key the log itself holds unrevoked as of that head."""
    key = snap.key(signer_fp)
    if key is None or not key.active:
        raise AnchorError(f"{signer_fp} is not an unrevoked key of this log at seq {snap.seq}")
    if key.key_class != LAPTOP:
        raise AnchorError(f"an anchor reset needs a {LAPTOP} key, not {key.key_class}")
    # the one custody rule, as at every verify site: a snapshot from a replay cache warmed before the enrol door can
    # hold WebAuthn material at a laptop custody, whose signature ssh-keygen takes without user verification
    why = custody_mismatch(key.public_key, key.custody)
    if why is not None:
        raise AnchorError(f"the anchor reset signer {signer_fp}: {why}")
    message = reset_message(snap.store_id, old_anchor, snap.seq, snap.head_hash)
    try:
        ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                             namespace=RESET_NAMESPACE, ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise AnchorError(str(exc)) from None
    if not ok:
        raise AnchorError(f"the anchor reset signature does not verify as {signer_fp}")


@contextmanager
def _no_live_cockpit(path: Path) -> Iterator[None]:
    """Hold the store's exclusive lock: the non-blocking ``flock`` on ``<data_root>/cockpit.lock`` that
    a live cockpit broker holds for its whole active life (``levain.cockpit.broker.LOCK_NAME``). The
    anchor sits in a directory of that data root (:func:`anchor_path`). Held, a reset is refused rather than waited
    for: a server still holding the abandoned store could advance the anchor back onto it."""
    from levain.cockpit.broker import LOCK_NAME
    lock = Path(path).parent.parent / LOCK_NAME
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:            # a read-only or missing data root: a refusal, not a traceback (S3a-2 MUST)
        raise AnchorError(f"the store lock {lock} cannot be opened ({type(exc).__name__}); a reset needs it") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnchorError(f"a cockpit server holds {lock}: stop it, then run "
                              f"{reset_hint(anchor_data_root(path))}") from None
        yield
    finally:
        os.close(fd)


class ResetLock:
    """Both locks a reset holds (:func:`reset_lock`), while its ``with`` block runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.live = True


@contextmanager
def reset_lock(path: Path | str) -> Iterator[ResetLock]:
    """The store lock (:func:`_no_live_cockpit`, refused while a cockpit server holds it) and the anchor's lock,
    held together: a caller that reads the old anchor, has the reset signed and writes it holds this across all
    three (``levain.cockpit.keys.anchor_reset``), so no server starts and no advance lands between them."""
    held = ResetLock(Path(path))
    try:
        with _no_live_cockpit(held.path), _anchor_lock(held.path):
            yield held
    finally:
        held.live = False


def reset_anchor(path: Path, snap: AuthoritySnapshot, *, signer_fp: str, signature: str,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, held: ResetLock | None = None) -> Anchor:
    """Move the anchor at ``path`` to ``snap``'s head on a verified reset signature, storing the proof
    in the anchor file (the log refuses to open, so it cannot be a log link). Runs under :func:`reset_lock`:
    ``held``, the caller's, or else taken here (refused while a cockpit server is live on the store). An anchor
    of another store is refused (:func:`foreign_anchor`), whatever the signature."""
    if held is None:
        with reset_lock(path) as mine:
            return reset_anchor(path, snap, signer_fp=signer_fp, signature=signature, ssh_keygen=ssh_keygen,
                                held=mine)
    if not held.live or held.path != Path(path):
        raise AnchorError(f"reset_anchor needs the reset lock of {path}, held")
    old = _old_anchor_view(path)
    foreign = foreign_anchor(snap, old, path)
    if foreign is not None:
        raise foreign
    verify_anchor_reset(snap, old, signer_fp=signer_fp, signature=signature, ssh_keygen=ssh_keygen)
    new = Anchor(snap.store_id, snap.seq, snap.head_hash)
    _write_anchor(path, {"format": ANCHOR_FORMAT, **new.as_dict(),
                         "reset": {"old_anchor": old, "signer_fp": signer_fp, "signature": signature}})
    return new
