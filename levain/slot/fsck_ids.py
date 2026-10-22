"""The fsck message ids adopt uses, in ONE module (S1 session 5 DESIGN §4.2, S4), and the adopt git floor.

Two lists, one rule between them:

- :data:`CONFIG_IDS`: the ids the importer's root-written config promotes to ``error`` (``fsck.<id>=error``). Only ids
  the floor's git knows; seal-in and doctor check each against the resolved git (:func:`check_config_ids`), an
  unknown one refusing ``git-fsck-id-unknown:<id>`` by name, never dropped.
- :func:`reader_ids`: the ids Levain's own tree-entry reader checks in the verifier, because bare
  ``index-pack --fsck-objects`` (the ERROR-default ids only) refuses none of them. DERIVED, never recalled: every
  CONFIG id whose default severity at the floor tag is below ERROR, plus :data:`LARGE_PATHNAME`, which the floor's git
  does not know at all.

:data:`FLOOR_SEVERITIES` is the floor tag's own table for the CONFIG ids, copied from git's ``fsck.h`` at
:data:`FLOOR_TAG` (``FOREACH_FSCK_MSG_ID``); its pin is the sha256 of that file, so a re-check is one download.

The adopt floor IS the maintenance floor (S4): :data:`FLOOR`, one table, read by adopt-open's and adopt-verify's
seal-in and by doctor, with one name, ``git-below-floor:<version>``.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Callable, Sequence

from levain.slot import Refused

#: The floor git release, and the tag :data:`FLOOR_SEVERITIES` was read from.
FLOOR = (2, 39, 4)
FLOOR_TAG = "v2.39.4"
#: sha256 of ``https://raw.githubusercontent.com/git/git/v2.39.4/fsck.h``, the file the severities below come from.
FLOOR_FSCK_H_SHA256 = "bb6c36caa68818ea7a198396713a92ef7cb716d86bccb4697ebb32b2b122c664"

#: The ids the importer's config promotes to ``error``, in the config's order.
CONFIG_IDS: tuple[str, ...] = (
    "hasDotgit", "hasDotdot", "gitmodulesSymlink", "gitattributesSymlink", "nullSha1", "zeroPaddedFilemode",
    "fullPathname", "badFilemode", "gitmodulesPath", "gitmodulesUrl", "gitmodulesName",
)

#: git's default severity for each CONFIG id at :data:`FLOOR_TAG`.
FLOOR_SEVERITIES: dict[str, str] = {
    "hasDotgit": "WARN", "hasDotdot": "WARN", "gitmodulesSymlink": "ERROR", "gitattributesSymlink": "INFO",
    "nullSha1": "WARN", "zeroPaddedFilemode": "WARN", "fullPathname": "WARN", "badFilemode": "INFO",
    "gitmodulesPath": "ERROR", "gitmodulesUrl": "ERROR", "gitmodulesName": "ERROR",
}

#: A limit on ONE path component (4096 bytes), first in v2.43.0; not in the CONFIG list, since the floor lacks it.
LARGE_PATHNAME = "largePathname"

#: Ids git itself refuses by default that adopt also names by id (the r5b ruling).
DEFAULT_ERROR_NAMED: tuple[str, ...] = ("gitattributesMissing", "gitmodulesMissing")

_RANK = {"IGNORE": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}


def snake_to_id(name: str) -> str:
    """git's spelling of a message id from its ``fsck.h`` macro name: ``HAS_DOTGIT`` -> ``hasDotgit``."""
    first, *rest = name.lower().split("_")
    return first + "".join(p.capitalize() for p in rest)


def parse_fsck_h(text: str) -> dict[str, str]:
    """Every ``FUNC(<ID>, <SEVERITY>)`` in a ``fsck.h``, as ``{id: severity}`` with git's id spelling."""
    return {snake_to_id(m.group(1)): m.group(2) for m in re.finditer(r"FUNC\(([A-Z0-9_]+), ([A-Z]+)\)", text)}


def reader_ids(severities: dict[str, str] | None = None) -> tuple[str, ...]:
    """The reader's list by the one rule: every CONFIG id below ERROR at the floor tag, in CONFIG order, then
    ``largePathname``. ``severities`` defaults to the pinned floor table; a parsed ``fsck.h`` may be passed."""
    sev = FLOOR_SEVERITIES if severities is None else severities
    missing = [i for i in CONFIG_IDS if i not in sev]
    if missing:
        raise ValueError(f"severity table lacks {missing}")
    return tuple(i for i in CONFIG_IDS if _RANK[sev[i]] < _RANK["ERROR"]) + (LARGE_PATHNAME,)


def closed_ids() -> tuple[str, ...]:
    """Every id ``fsck:<id>`` may carry: the CONFIG list, the reader's, and the named default-error ids."""
    out: list[str] = []
    for i in (*CONFIG_IDS, *reader_ids(), *DEFAULT_ERROR_NAMED):
        if i not in out:
            out.append(i)
    return tuple(out)


_VERSION = re.compile(r"^git version (\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(text.strip())
    return None if m is None else (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def check_floor(version_text: str) -> tuple[int, int, int]:
    """``git --version``'s text against :data:`FLOOR`: the parsed version, or ``git-below-floor:<version>``. A text
    that does not parse refuses as ``git-below-floor:unparsed`` (a git that cannot say its version is not trusted to
    be above the floor)."""
    v = parse_version(version_text)
    if v is None:
        raise Refused("git-below-floor:unparsed", version_text.strip()[:120])
    if v < FLOOR:
        raise Refused(f"git-below-floor:{'.'.join(map(str, v))}",
                      f"the resolved git is below the floor {'.'.join(map(str, FLOOR))}")
    return v


#: The scrubbed environment every check here runs git under: no system or global config, no home.
SCRUBBED_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "HOME": "/nonexistent", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

Run = Callable[..., subprocess.CompletedProcess]


def git_version_text(git: str, *, run: Run = subprocess.run) -> str:
    return run([git, "--version"], capture_output=True, text=True, env=SCRUBBED_ENV, cwd="/").stdout


def check_config_ids(git: str, ids: Sequence[str] = CONFIG_IDS, *, run: Run = subprocess.run,
                     scratch_parent: str | None = None) -> None:
    """Each id in ``ids`` against ``git`` on an empty scratch repository: ``git -c fsck.<id>=error fsck`` must exit 0.
    git dies on an id it does not know (``Unhandled message id``); any failure refuses ``git-fsck-id-unknown:<id>``
    for the first such id, git's own line in the detail."""
    with tempfile.TemporaryDirectory(prefix="levain-fsckids-", dir=scratch_parent) as d:
        repo = os.path.join(d, "e.git")
        r = run([git, "init", "-q", "--bare", repo], capture_output=True, text=True, env=SCRUBBED_ENV, cwd="/")
        if r.returncode != 0:
            raise Refused("git-fsck-id-unknown:" + (ids[0] if ids else "none"), "a scratch repository could not be made")
        for i in ids:
            r = run([git, f"--git-dir={repo}", "-c", f"fsck.{i}=error", "fsck", "--no-progress"],
                    capture_output=True, text=True, env=SCRUBBED_ENV, cwd="/")
            if r.returncode != 0:
                line = (r.stderr.strip().splitlines() or [""])[-1]
                raise Refused(f"git-fsck-id-unknown:{i}", f"rc {r.returncode}: {line[:200]}")


__all__ = ["CONFIG_IDS", "DEFAULT_ERROR_NAMED", "FLOOR", "FLOOR_FSCK_H_SHA256", "FLOOR_SEVERITIES", "FLOOR_TAG",
           "LARGE_PATHNAME", "SCRUBBED_ENV", "check_config_ids", "check_floor", "closed_ids", "git_version_text",
           "parse_fsck_h", "parse_version", "reader_ids", "snake_to_id"]
