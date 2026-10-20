"""levain.dispatch.grant — the typed contract grant: a file write inside the signed write list, decided (F4).

A dispatch contract may ``grant`` the executor typed file tools (``Write``, ``Edit``, ``apply_patch``; never Bash).
:func:`grant_decision` decides one such call: :class:`Allow` when every path it touches is a file of the contract's
signed ``write`` list and the disk under the worktree root holds no symlink or hard link on the way to it, else
:class:`Queue` with the reason, and the call goes to the consent queue as any ungranted call does. The authority is
the operator's signature over the contract, which the caller has verified; no answer, model or planner authorises.

The paths come from a TOTAL parse (:func:`grant_paths`): Claude Code's ``Write``/``Edit`` name one absolute
``file_path`` (:func:`levain.consent.policy.targets`); a Codex ``apply_patch`` names its header paths
(:func:`levain.consent.adapters.codex.patch_targets`), each joined to the worktree root, ``Move to`` giving both.
A patch is read only when no line Codex's own parser could take for a header can be read otherwise here: a
character Python splits lines on but Codex does not (``\\r`` outside ``\\r\\n``, ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``,
``\\x85``, U+2028, U+2029), or one Python strips as whitespace but Codex does not (``\\x1c``-``\\x1f``), makes it
unreadable. Anything not read whole is ``None``, never a partial list: what it touches cannot be told.

:func:`path_bound_digest` is the ``pb1:`` request digest: the tool, those paths and the sha256 of the input's JCS
bytes, under one canonical encoding, so a request filed with other paths than its input names has another digest.

Each path is checked at approval time (:func:`no_follow_check`, Phill's ruling 10-20): from a directory fd on the
root, each intermediate component is opened ``O_DIRECTORY|O_NOFOLLOW`` relative to the last, and the final one is
``lstat``ed: absent only when the entry is NEW or the tool is ``Write``, else a regular file with one link.

Known edge (the same ruling): a symlink swapped in after the check and before the tool's write is not caught here;
Bash already runs free inside the sandbox, so the race buys nothing Bash cannot do, and the floor (the final diff
inside the write list) and the verifier judge the artifact. Stdlib only.
"""
from __future__ import annotations

import errno
import hashlib
import os
import posixpath
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from levain.cockpit.consent import ConsentError, value_sha256
from levain.consent.adapters.codex import PATCH_TOOL, patch_targets
from levain.consent.policy import AGENT_FILE_TOOLS, targets
from levain.contracts.canon import canonical_bytes
from levain.dispatch.contract import GRANTABLE, NEW, PathEntry, ResolvedContract

__all__ = ["Allow", "DIGEST_PREFIX", "Queue", "grant_decision", "grant_paths", "no_follow_check", "path_bound_digest",
           "write_list_match"]

DIGEST_PREFIX = "pb1:"
# characters on which Python's str.splitlines breaks a line and Codex's parser (Rust str::lines, '\n' only) does
# not, and the ones Python's str.strip and re's \s take for whitespace and Rust's trim does not; '\r' is read apart
_PATCH_UNREADABLE = frozenset("\v\f\x1c\x1d\x1e\x1f\x85  ")
# the input keys a grantable call may carry; any other key is a field whose meaning this parse cannot tell
_INPUT_KEYS = {"Write": frozenset({"file_path", "content"}),
               "Edit": frozenset({"file_path", "old_string", "new_string", "replace_all"}),
               PATCH_TOOL: frozenset({"command"})}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class Allow:
    """The grant holds: every path (absolute, sorted, each once) is a write-list file the walk found clean."""
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Queue:
    """The grant does not decide this call; it goes to the consent queue. ``reason`` says why."""
    reason: str


def path_bound_digest(tool_name: str, paths: Sequence[str], tool_input: Any) -> str:
    """``pb1:`` + sha256 of the canonical bytes of ``{"tool", "paths", "input"}``, ``input`` being
    :func:`levain.cockpit.consent.value_sha256` of ``tool_input``. ``paths`` must be sorted strings, each once (one
    meaning, one digest): ValueError otherwise; ConsentError when ``tool_input`` is outside JCS (a float)."""
    paths = list(paths)
    if not all(isinstance(p, str) for p in paths) or paths != sorted(set(paths)):
        raise ValueError("paths must be sorted strings, each once")
    body = {"tool": tool_name, "paths": paths, "input": value_sha256(tool_input)}
    return DIGEST_PREFIX + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _patch_readable(patch: str) -> bool:
    return not any(c in _PATCH_UNREADABLE for c in patch) and "\r" not in patch.replace("\r\n", "")


def grant_paths(agent: str, tool_name: str, tool_input: Any, root: str) -> tuple[str, ...] | None:
    """The absolute paths a grantable call of ``agent`` touches, sorted, each once; ``None`` when the call is not a
    grantable file tool of that agent or its input is not read whole (an unknown input key, a relative
    ``file_path``, a patch :func:`patch_targets` cannot read or holds a character the two parsers split or strip
    differently, an input outside JCS). ``root`` is the worktree root, absolute, that patch paths join to."""
    if tool_name not in GRANTABLE or tool_name not in AGENT_FILE_TOOLS.get(agent, frozenset()):
        return None
    if not isinstance(tool_input, Mapping) or not set(tool_input) <= _INPUT_KEYS[tool_name]:
        return None
    try:
        value_sha256(tool_input)
    except ConsentError:
        return None
    if tool_name == PATCH_TOOL:
        patch = tool_input.get("command")
        if not isinstance(patch, str) or not _patch_readable(patch):
            return None
        headers = patch_targets(patch)
        if headers is None or not isinstance(root, str) or not posixpath.isabs(root):
            return None
        found = [posixpath.join(root, h) for h in headers]
    else:
        found = targets(tool_name, tool_input) or []
        if len(found) != 1 or not isinstance(found[0], str) or not posixpath.isabs(found[0]):
            return None
    return tuple(sorted(set(found)))


def write_list_match(rel: str, write_entries: Iterable[PathEntry]) -> PathEntry | None:
    """The one entry of ``write_entries`` whose path equals ``rel`` in NFC, compared without case (contract.py's
    rule: the target disk is case-insensitive), or ``None``. A whole entry only: an entry is never a prefix."""
    key = unicodedata.normalize("NFC", rel).casefold()
    hits = [e for e in write_entries if unicodedata.normalize("NFC", e.path).casefold() == key]
    return hits[0] if len(hits) == 1 else None


def no_follow_check(root: str, rel: str, *, new_ok: bool) -> str | None:
    """``None`` when ``rel`` under ``root`` is reached with no symlink: ``root`` and each intermediate component
    opened ``O_DIRECTORY|O_NOFOLLOW`` from the last, the final component ``lstat``ed and either absent (only when
    ``new_ok``; an absent intermediate directory counts as absent) or a regular file with ``st_nlink == 1``. Else the
    refusal line. Every fd opened is closed."""
    parts = rel.split("/")
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        return f"the worktree root cannot be opened without following a link ({errno.errorcode.get(exc.errno, exc)})"
    try:
        for i, part in enumerate(parts[:-1]):
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None if new_ok else f"{'/'.join(parts[:i + 1])} is absent and {rel} is not a new file"
            except OSError as exc:
                return (f"{'/'.join(parts[:i + 1])} is not a plain directory (a symlink or a file: "
                        f"{errno.errorcode.get(exc.errno, exc)})")
            os.close(fd)
            fd = nxt
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None if new_ok else f"{rel} is absent and is not a new file"
        except OSError as exc:
            return f"{rel} cannot be read without following a link ({errno.errorcode.get(exc.errno, exc)})"
        if stat.S_ISLNK(st.st_mode):
            return f"{rel} is a symlink"
        if not stat.S_ISREG(st.st_mode):
            return f"{rel} is not a regular file"
        if st.st_nlink != 1:
            return f"{rel} has {st.st_nlink} hard links, so a write would reach another name's file"
        return None
    finally:
        os.close(fd)


def grant_decision(contract: ResolvedContract, agent: str, tool_name: str, tool_input: Any, *,
                   root: str) -> Allow | Queue:
    """:class:`Allow` when ``tool_name`` is in the contract's ``grant`` and every path of :func:`grant_paths` is,
    in order: under ``root`` with a normalised relative part; spelled exactly as one entry of the signed ``write``
    list (a case or Unicode-form variant of an entry queues: on a case-sensitive disk it is another file); and
    clean under :func:`no_follow_check`. Else :class:`Queue` naming the first path that fails and why."""
    if not isinstance(contract, ResolvedContract):
        return Queue("the contract is not a resolved dispatch contract")
    if tool_name not in contract.grant:
        return Queue(f"the contract does not grant {str(tool_name)[:40]}")
    if not isinstance(root, str) or not posixpath.isabs(root) or posixpath.normpath(root) != root or root == "/":
        return Queue("the worktree root is not an absolute, normalised directory path")
    paths = grant_paths(agent, tool_name, tool_input, root)
    if paths is None:
        return Queue(f"the {str(tool_name)[:40]} input is not read whole, so what it touches cannot be told")
    checks = []
    for p in paths:
        if posixpath.normpath(p) != p or not p.startswith(root + "/"):
            return Queue(f"{p[:120]} is not a normalised path under the worktree root")
        rel = p[len(root) + 1:]
        entry = write_list_match(rel, contract.write)
        if entry is None:
            return Queue(f"{rel[:120]} is not a file of the signed write list")
        if entry.path != rel:
            return Queue(f"{rel[:120]} is a case or form variant of write-list file {entry.path}")
        checks.append((rel, entry.mode == NEW or tool_name == "Write"))
    for rel, new_ok in checks:
        problem = no_follow_check(root, rel, new_ok=new_ok)
        if problem is not None:
            return Queue(problem)
    return Allow(paths)
