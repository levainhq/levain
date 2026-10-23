"""The fsck message ids adopt uses, in ONE module (S1 session 5 DESIGN §4.2, S4), and the adopt git floor.

Two lists, one rule between them:

- :data:`CONFIG_IDS`: the ids the importer's root-written config promotes to ``error`` (``fsck.<id>=error``). Only ids
  the floor's git knows; seal-in and doctor check each against the resolved git (:func:`check_config_ids`), an
  unknown one refusing ``git-fsck-id-unknown:<id>`` by name, never dropped.
- :func:`reader_ids`: the ids Levain's own tree-entry reader checks in the verifier. DERIVED, never recalled: every
  CONFIG id whose default severity at the floor tag is below ERROR, plus :data:`LARGE_PATHNAME`, which the floor's git
  does not know at all. The measured premise (5b1 session 2, on the floor git 2.39.4 and a 2.50.1 host): index-pack's
  object checks run strict, so a bare ``index-pack --fsck-objects`` refuses every WARN-default id here (``hasDotgit``,
  ``hasDotdot``, ``nullSha1``, ``zeroPaddedFilemode``, ``fullPathname``; the newer git ``largePathname`` too) and
  passes the INFO-default ones (``badFilemode``, ``gitattributesSymlink``). The reader keeps the whole derived list
  anyway: a superset never loosens a check, and the git that runs index-pack can change.

:data:`FLOOR_SEVERITIES` is the floor tag's own table for the CONFIG ids, copied from git's ``fsck.h`` at
:data:`FLOOR_TAG` (``FOREACH_FSCK_MSG_ID``); its pin is the sha256 of that file, so a re-check is one download.

The adopt floor IS the maintenance floor (S4): :data:`FLOOR`, one table, read by adopt-open's and adopt-verify's
seal-in and by doctor, with one name, ``git-below-floor:<version>``. Every git probe here is bounded by a timeout and
answers a named refusal, never a traceback: a git that cannot be run, hangs, exits non-zero or prints a version off
git's grammar is ``git-below-floor:unparsed``; a fsck-id probe that fails for any reason other than git's own
"Unhandled message id" for that id is :data:`FSCK_PROBE_FAILED`.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Callable, NamedTuple, Sequence

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

#: A message id as git prints it (``snake_to_id`` of a ``fsck.h`` macro): camelCase, digits allowed (``nullSha1``,
#: ``badTreeSha1``). Every regex that reads an id out of git's stderr uses THIS, so an id with a digit is never read
#: as ``fsck:other`` (5d-ii S1 run: ``[A-Za-z]+`` stopped at ``nullSha``).
ID_PATTERN = r"[A-Za-z][A-Za-z0-9]*"


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


#: ``git version <v>``, ``<v>`` by git's own version grammar (``GIT-VERSION-GEN`` turns ``git describe``'s dashes
#: into dots): a three- or four-number release; a prerelease ``.rc<N>``; a development build ``.<commits>.g<hex>``;
#: ``.dirty``; Git for Windows' ``.windows.<N>``; and Apple's `` (Apple Git-<N>)``, ``<N>`` dotted or not (Xcode 14's
#: ``Apple Git-137.1``). Anything else does not parse.
_VERSION = re.compile(r"git version (?P<v>(?P<rel>\d+\.\d+\.\d+(?:\.\d+)?)(?:\.rc(?P<rc>\d+))?"
                      r"(?:\.(?P<dev>\d+)\.g[0-9a-f]+)?(?:\.dirty)?(?:\.windows\.\d+)?)"
                      r"(?: \(Apple Git-\d+(?:\.\d+)*\))?")
UNPARSED = "git-below-floor:unparsed"
#: The refusal of a fsck-id probe that failed for any reason but git not knowing the id.
FSCK_PROBE_FAILED = "git-fsck-probe-failed"
#: Seconds each probe may take.
VERSION_TIMEOUT = 10.0
FSCK_TIMEOUT = 30.0


class GitVersion(NamedTuple):
    text: str                   # the version as git printed it, without ``git version`` or Apple's suffix
    release: tuple[int, ...]    # three numbers, or four
    rc: int | None              # the prerelease number; None for a release
    dev: int                    # commits past the tag of a development build; 0 otherwise

    def key(self) -> tuple:
        """Ordering: by release, a prerelease below its release, then a development build after its tag."""
        return ((*self.release, 0)[:4], (0, self.rc) if self.rc is not None else (1, 0), self.dev)


def parse_version(text: str) -> GitVersion | None:
    """``git --version``'s whole output by the grammar above, or None."""
    m = _VERSION.fullmatch(text.strip())
    if m is None:
        return None
    return GitVersion(m.group("v"), tuple(int(x) for x in m.group("rel").split(".")),
                      None if m.group("rc") is None else int(m.group("rc")), int(m.group("dev") or 0))


def check_floor(version_text: str) -> tuple[int, int, int]:
    """``git --version``'s text against :data:`FLOOR`: the release's three numbers, or ``git-below-floor:<version>``
    (a prerelease of the floor release is below it). A text off the grammar refuses as ``git-below-floor:unparsed``
    (a git that cannot say its version is not trusted to be above the floor)."""
    v = parse_version(version_text)
    if v is None:
        raise Refused(UNPARSED, version_text.strip()[:120])
    if v.key() < GitVersion("", FLOOR, None, 0).key():
        raise Refused(f"git-below-floor:{v.text}",
                      f"the resolved git is below the floor {'.'.join(map(str, FLOOR))}")
    return v.release[0], v.release[1], v.release[2]


#: The scrubbed environment every check here runs git under: no system or global config, no home.
SCRUBBED_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "HOME": "/nonexistent", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

Run = Callable[..., subprocess.CompletedProcess]


def _probe(run: Run, argv: list[str], timeout: float, name: str, what: str) -> subprocess.CompletedProcess:
    """One bounded git probe: a git that cannot run or does not answer within ``timeout`` refuses ``name``."""
    try:
        return run(argv, capture_output=True, text=True, errors="replace", env=SCRUBBED_ENV, cwd="/",
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Refused(name, f"{what}: no answer within {timeout:g} s") from None
    except (OSError, ValueError) as e:
        raise Refused(name, f"{what}: {type(e).__name__}: {e}"[:200]) from None


def git_version_text(git: str, *, run: Run = subprocess.run, timeout: float = VERSION_TIMEOUT) -> str:
    """``git --version``'s output, from a run that exited 0 within ``timeout``; otherwise ``git-below-floor:unparsed``."""
    r = _probe(run, [git, "--version"], timeout, UNPARSED, "git --version")
    if r.returncode != 0:
        raise Refused(UNPARSED, f"git --version exited {r.returncode}")
    return r.stdout


def check_config_ids(git: str, ids: Sequence[str] = CONFIG_IDS, *, run: Run = subprocess.run,
                     scratch_parent: str | None = None, timeout: float = FSCK_TIMEOUT) -> None:
    """Each id in ``ids`` against ``git`` on an empty scratch repository: ``git -c fsck.<id>=error fsck`` must exit 0.
    git dies on an id it does not know (``fatal: Unhandled message id: <id lowercased>``): that, and only that, refuses
    ``git-fsck-id-unknown:<id>`` for the first such id. Every other failure (the scratch repository not made, a fsck
    killed, timed out or failing otherwise) is :data:`FSCK_PROBE_FAILED`, git's own line in the detail."""
    try:
        with tempfile.TemporaryDirectory(prefix="levain-fsckids-", dir=scratch_parent) as d:
            repo = os.path.join(d, "e.git")
            r = _probe(run, [git, "init", "-q", "--bare", repo], timeout, FSCK_PROBE_FAILED, "scratch git init")
            if r.returncode != 0:
                raise Refused(FSCK_PROBE_FAILED, f"scratch git init rc {r.returncode}: {_last(r.stderr)}")
            for i in ids:
                r = _probe(run, [git, f"--git-dir={repo}", "-c", f"fsck.{i}=error", "fsck", "--no-progress"],
                           timeout, FSCK_PROBE_FAILED, f"fsck under fsck.{i}=error")
                if r.returncode == 0:
                    continue
                if f"fatal: Unhandled message id: {i.lower()}" in r.stderr.splitlines():
                    raise Refused(f"git-fsck-id-unknown:{i}", f"rc {r.returncode}: {_last(r.stderr)}")
                raise Refused(FSCK_PROBE_FAILED, f"fsck under fsck.{i}=error rc {r.returncode}: {_last(r.stderr)}")
    except OSError as e:
        raise Refused(FSCK_PROBE_FAILED, f"the scratch directory: {type(e).__name__}: {e}"[:200]) from None


def _last(stderr: str) -> str:
    return (stderr.strip().splitlines() or [""])[-1][:200]


__all__ = ["CONFIG_IDS", "DEFAULT_ERROR_NAMED", "ID_PATTERN", "FLOOR", "FLOOR_FSCK_H_SHA256", "FLOOR_SEVERITIES", "FLOOR_TAG",
           "FSCK_PROBE_FAILED", "FSCK_TIMEOUT", "GitVersion", "LARGE_PATHNAME", "SCRUBBED_ENV", "UNPARSED",
           "VERSION_TIMEOUT", "check_config_ids", "check_floor", "closed_ids", "git_version_text", "parse_fsck_h",
           "parse_version", "reader_ids", "snake_to_id"]
