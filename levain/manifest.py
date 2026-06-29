"""Levain compatibility manifest — the declared known-good version SET.

Levain composes a stack across TWO version axes that ``pip`` cannot keep in
sync:

- **anneal-memory** — separately versioned on PyPI; ``pip`` pins its *library
  API* (a range, e.g. ``anneal-memory>=0.9.5,<0.10``).
- **methodology-core + adapters + interview** — ride *inside* the levain wheel,
  so they are levain-versioned.

``pip``'s solver is structurally blind to the *methodology-content* alignment: a
new anneal feature (e.g. the ``spores`` prospective layer) can land as a
**conflict** with an operator's stale, hand-tuned methodology instructions even
when the library API is fully compatible. That is the drift that broke an early
adopter's install — the library upgraded, the methodology did not, and a feature
read as a contradiction instead of an addition.

This module is the fix — a methodology-aware *lockfile* for the composed stack:

1. **DECLARED set** — the exact composition THIS levain wheel was tested against
   (:func:`declared_set`). It PINS versions; it never VENDORS — anneal stays a
   normal pip dependency.
2. **INSTALLED set** — what is actually present on this machine right now
   (:func:`discover_installed_set`), read from anneal's own JSON CLI.
3. **LOCK** — what an install last composed, recorded at ``.levain/manifest.json``
   (:func:`read_lock` / :func:`write_lock`), written by ``levain init`` and
   ``levain update``.
4. **DRIFT** — the comparison of the three (:func:`compute_drift`), classified
   per axis so ``levain doctor`` can report it and ``levain update`` can act on
   it.

It **composes with anneal's own** ``migrate check`` machinery rather than
reinventing it: anneal owns the per-version instruction-edit *proposals* (the
methodology-content reconciliation, ``anneal_memory.migration``); this module
owns the version SET and *delegates* the content reconciliation to
``anneal-memory migrate check`` / ``ack``. The manifest pins the VERSIONS;
migrate-check carries the instruction-content edits.

The honesty floor is load-bearing throughout: a discovery that FAILS yields an
``UNKNOWN`` verdict, never a false "in sync" — a failed read is not a clean read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from levain import __version__

# ---------------------------------------------------------------------------
# The DECLARED known-good set — reviewed constants, bumped per release.
# ---------------------------------------------------------------------------

# The EXACT anneal-memory version this levain release is tested against. This is
# a deliberate, human-reviewed assertion (NOT silently derived from the pip pin),
# bumped in lockstep with the pyproject dependency floor at each release. The
# pip-pin consistency check (:func:`pip_floor_verdict`) flags it if the two ever
# drift, which is the release-gate that keeps the manifest honest.
KNOWN_GOOD_ANNEAL = "0.9.5"

# The anneal store section-schema a partnership entity runs on (anneal
# AM-INITSCHEMA). A store left on the default silently runs the 4-section ops
# schema — the exact invariant `levain init` protects; the manifest re-checks it.
KNOWN_GOOD_SCHEMA = "partnership"

# The highest anneal-memory version whose migration-manifest entries the SEED
# TEMPLATES already incorporate. `levain init` acks a fresh install's migrate
# marker up to this (capped at the installed anneal) so a freshly-rendered,
# CURRENT adopter sees no false "pending drift" — while NEVER suppressing a
# proposal the templates don't yet cover. A reviewed release-checklist
# assertion: bump it only when the seed templates are reconciled to that
# version's manifest entries. Invariant (release-gate, test-locked): must be
# <= KNOWN_GOOD_ANNEAL — you cannot reconcile templates to a version you don't ship.
#
# Capped at 0.4.8 (the last entry before the crystal tier): the seed now carries
# the spores boundary (0.4.7), the `levain update` upgrade habit (0.4.7), and the
# co-citation/linkgate discipline (0.8.3) — BUT the apparatus (L1+L2) caught that
# the crystal-tier entries 0.7.1 (AM-CRYSTAL) + 0.8.2 (AM-MCP-CRYSTAL) are NOT
# genuinely reconciled: the activation hook never fires crystal recall (memory.md
# line 7's "automatic per-turn crystal recall" is aspirational, not shipped), so
# acking past them would silently suppress real guidance. They stay HONEST pending
# advisories until the crystal-recall slice lands (wire the UserPromptSubmit hook
# at the crystal tier + teach wrap-time crystallization routing), which raises this
# to 0.8.3 — the linkgate guidance is already in place and rides up with it.
TEMPLATES_RECONCILED_ANNEAL = "0.4.8"

# The install's recorded-set lockfile, a sibling of the anneal store under the
# machine-managed `.levain/` dir (NOT operator-facing — `config.json` is that).
MANIFEST_LOCK_REL = (".levain", "manifest.json")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

AxisStatus = Literal[
    "in_sync",  # reality matches the target
    "behind",   # installed is OLDER than known-good — update forward
    "ahead",    # installed is NEWER than this release was tested against
    "drift",    # reality changed underneath the lock / schema mismatch
    "pending",  # unreviewed anneal migration proposals (methodology-content drift)
    "unknown",  # a read failed — honesty floor: NOT "in sync"
]

# Statuses that mean "the set is not at its declared known-good and something
# should be done." `unknown` is deliberately NOT actionable on its own (it means
# "could not determine," e.g. no store yet) — but it is never reported as green.
# `ahead` is also NOT actionable: an installed anneal NEWER than this release's
# known-good was allowed by the pip pin and works; `levain update` cannot (and
# must not) downgrade it, so the only remedy is "upgrade levain" — advisory, not
# a reconcile action. (`pending` IS actionable: `levain update` surfaces the
# proposals, so it must not early-return on a pending-only set.)
_ACTIONABLE: frozenset[AxisStatus] = frozenset({"behind", "drift", "pending"})


@dataclass(frozen=True)
class CompatSet:
    """A composed version set — the DECLARED known-good, or a recorded LOCK."""

    levain: str
    anneal: str
    schema: str


@dataclass(frozen=True)
class InstalledSet:
    """What is actually installed right now. Any field is ``None`` when its read
    failed — the honesty floor: an absent value is UNKNOWN, never a default."""

    levain: str
    anneal: str | None
    schema: str | None
    migrate_acked: str | None
    pending_count: int | None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AxisVerdict:
    """One axis of the drift comparison."""

    axis: str
    status: AxisStatus
    detail: str
    hint: str | None = None

    @property
    def ok(self) -> bool:
        """True only when this axis is genuinely at known-good. `unknown` is
        NOT ok (honesty floor); the actionable-drift states are not ok."""
        return self.status == "in_sync"


@dataclass(frozen=True)
class SetDrift:
    """The full per-axis comparison of declared / installed / lock."""

    verdicts: list[AxisVerdict]

    @property
    def in_sync(self) -> bool:
        """True only when EVERY axis is in_sync."""
        return all(v.status == "in_sync" for v in self.verdicts)

    @property
    def has_actionable_drift(self) -> bool:
        """True when at least one axis is an actionable-drift state (something
        `levain update` would fix). Distinct from `unknown`, which is reported
        but is not, on its own, a call to update."""
        return any(v.status in _ACTIONABLE for v in self.verdicts)

    @property
    def has_unknown(self) -> bool:
        return any(v.status == "unknown" for v in self.verdicts)

    def of(self, axis: str) -> AxisVerdict | None:
        for v in self.verdicts:
            if v.axis == axis:
                return v
        return None


# ---------------------------------------------------------------------------
# Version comparison (tolerant — mirrors anneal's `_version_tuple`)
# ---------------------------------------------------------------------------

def version_tuple(version: str) -> tuple[int, ...]:
    """Parse a version into a comparable tuple of its leading numeric dotted
    components, stopping at the first non-numeric character (so a pre-release /
    build suffix can't leak spurious trailing components): ``"0.9.5"`` ->
    ``(0, 9, 5)``, ``"0.9.5rc1"`` -> ``(0, 9, 5)``. A version that starts
    non-numeric yields ``()`` (sorts before every real version) rather than
    raising. Deliberately matches ``anneal_memory.migration._version_tuple`` so
    the two layers order versions identically."""
    parts: list[int] = []
    for chunk in version.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(digits) != len(chunk):
            break
    return tuple(parts)


def declared_set() -> CompatSet:
    """The known-good set THIS levain wheel declares.

    ``levain`` is the SOURCE ``__version__`` (not ``importlib.metadata`` — an
    editable/source install's dist metadata can lag the moved source; the
    constant is the truth). ``anneal`` / ``schema`` are the reviewed constants."""
    return CompatSet(
        levain=__version__,
        anneal=KNOWN_GOOD_ANNEAL,
        schema=KNOWN_GOOD_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Discovery — read the INSTALLED set from anneal's own JSON CLI
# ---------------------------------------------------------------------------

def _run_anneal_json(
    store: Path,
    anneal_path: str,
    sub_args: list[str],
    *,
    timeout: float = 10.0,
) -> object | None:
    """Run an anneal-memory subcommand (which emits JSON on stdout) against
    ``store`` and return the parsed JSON, or ``None`` on ANY failure.

    Tries the resolved console-script path first, then ``python -m
    anneal_memory`` (the same two-candidate fallback `install._run_anneal_cmd`
    uses). A ``TimeoutExpired`` ABORTS the candidate loop — the candidates
    invoke the SAME anneal via different entry points, so if one hangs the other
    would too; each query costs at most one ``timeout``. The store is pinned with
    ``--db`` so anneal's machine-global default is never touched."""
    db = str(store)
    candidates = [
        [anneal_path, "--db", db, *sub_args],
        [sys.executable, "-m", "anneal_memory", "--db", db, *sub_args],
    ]
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            return json.loads(result.stdout)
        except ValueError:
            continue
    return None


def discover_installed_set(
    store: Path, anneal_path: str, *, timeout: float = 10.0
) -> InstalledSet:
    """Read the actually-installed set from anneal's own JSON output.

    Two anneal calls:

    - ``migrate check --json`` -> ``installed_version`` (the live anneal
      version, reported by the running anneal itself — robust to dist-metadata
      lag), ``acknowledged_version`` (the migrate marker), and ``pending`` (the
      unreviewed instruction-edit proposals — the methodology-content drift).
    - ``status --json`` -> ``schema`` (the store's persisted section schema).

    Every field is ``None`` on its read failing (honesty floor). ``errors``
    records which reads failed, for a loud-but-non-crashing report."""
    errors: list[str] = []

    anneal: str | None = None
    migrate_acked: str | None = None
    pending_count: int | None = None
    migrate = _run_anneal_json(
        store, anneal_path, ["migrate", "check", "--json"], timeout=timeout
    )
    if isinstance(migrate, dict):
        iv = migrate.get("installed_version")
        if isinstance(iv, str) and iv.strip():
            anneal = iv
        av = migrate.get("acknowledged_version")
        if isinstance(av, str) and av.strip():
            migrate_acked = av
        pend = migrate.get("pending")
        if isinstance(pend, list):
            pending_count = len(pend)
    else:
        errors.append(
            "could not read `anneal-memory migrate check --json` "
            "(anneal missing, too old, no store, or a fault)"
        )

    schema: str | None = None
    status = _run_anneal_json(store, anneal_path, ["status", "--json"], timeout=timeout)
    if isinstance(status, dict):
        sc = status.get("schema")
        if isinstance(sc, str) and sc.strip():
            schema = sc
    else:
        errors.append("could not read `anneal-memory status --json` (no store, or a fault)")

    return InstalledSet(
        levain=__version__,
        anneal=anneal,
        schema=schema,
        migrate_acked=migrate_acked,
        pending_count=pending_count,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Drift — the pure comparison (declared / installed / lock)
# ---------------------------------------------------------------------------

def compute_drift(
    declared: CompatSet,
    installed: InstalledSet,
    lock: CompatSet | None,
) -> SetDrift:
    """Compare reality against the declared known-good set (and the recorded
    lock). PURE — no I/O — so the whole state machine is unit-testable.

    Axes:

    - ``levain`` — the running levain vs the lock (did the wheel upgrade without
      a reconcile?). Skipped when there is no lock (a pre-manifest install).
    - ``anneal`` — installed vs declared known-good (behind / ahead / in_sync).
    - ``anneal-lock`` — installed vs the lock's anneal (did anneal change
      out-of-band, e.g. ``pip install -U anneal-memory``?). Emitted only on a
      genuine difference; this is the exact drift that broke the early adopter.
    - ``schema`` — the store schema vs known-good.
    - ``migrate`` — unreviewed anneal migration proposals (methodology-content
      drift), delegated to anneal's ``migrate check``.
    """
    verdicts: list[AxisVerdict] = []

    # -- levain (running wheel vs last-composed lock) --
    if lock is not None and lock.levain:
        cmp = _cmp(installed.levain, lock.levain)
        if cmp == 0:
            verdicts.append(AxisVerdict(
                "levain", "in_sync", f"levain {installed.levain} (matches last composed)"
            ))
        else:
            direction = "upgraded" if cmp > 0 else "downgraded"
            verdicts.append(AxisVerdict(
                "levain", "drift",
                f"levain {direction} {lock.levain} -> {installed.levain} since the "
                f"set was last composed",
                "Run `levain update` to reconcile anneal + schema + migrations to "
                "the new known-good set.",
            ))

    # -- anneal (installed vs declared known-good) --
    if installed.anneal is None:
        verdicts.append(AxisVerdict(
            "anneal", "unknown",
            "could not determine the installed anneal-memory version",
            "Check `anneal-memory --version`; install/repair with "
            "`pip install -U anneal-memory`.",
        ))
    else:
        cmp = _cmp(installed.anneal, declared.anneal)
        if cmp == 0:
            verdicts.append(AxisVerdict(
                "anneal", "in_sync",
                f"anneal-memory {installed.anneal} == known-good {declared.anneal}",
            ))
        elif cmp < 0:
            verdicts.append(AxisVerdict(
                "anneal", "behind",
                f"anneal-memory {installed.anneal} is BEHIND known-good "
                f"{declared.anneal}",
                f"Run `levain update` (or `pip install 'anneal-memory=="
                f"{declared.anneal}'`).",
            ))
        else:
            verdicts.append(AxisVerdict(
                "anneal", "ahead",
                f"anneal-memory {installed.anneal} is AHEAD of this levain "
                f"release's known-good {declared.anneal} — untested together",
                "Run `levain doctor` after upgrading levain "
                "(`pip install -U levain`); review `anneal-memory migrate check` "
                "for instruction edits the newer anneal implies.",
            ))

    # -- anneal vs lock (out-of-band change underneath the install) --
    if (
        lock is not None
        and lock.anneal
        and installed.anneal is not None
        and _cmp(installed.anneal, lock.anneal) != 0
    ):
        verdicts.append(AxisVerdict(
            "anneal-lock", "drift",
            f"anneal-memory changed {lock.anneal} -> {installed.anneal} since this "
            f"install was last composed (an out-of-band upgrade)",
            "Run `levain update` so the methodology layer reconciles with the new "
            "anneal (this is the exact drift that lands a new feature as a conflict "
            "with stale instructions).",
        ))

    # -- schema (the store's section schema) --
    if installed.schema is None:
        verdicts.append(AxisVerdict(
            "schema", "unknown",
            "could not determine the store's section schema",
            "Check `anneal-memory --db <store> status --json`.",
        ))
    elif installed.schema == declared.schema:
        verdicts.append(AxisVerdict(
            "schema", "in_sync", f"store schema is {installed.schema}"
        ))
    else:
        verdicts.append(AxisVerdict(
            "schema", "drift",
            f"store schema is {installed.schema}, known-good {declared.schema}",
            f"Run `levain update` (re-runs `set-schema {declared.schema}`; memory "
            f"content is preserved).",
        ))

    # -- migrate (unreviewed methodology-content proposals) --
    if installed.pending_count is None:
        verdicts.append(AxisVerdict(
            "migrate", "unknown",
            "could not read anneal migration proposals",
        ))
    elif installed.pending_count == 0:
        verdicts.append(AxisVerdict(
            "migrate", "in_sync", "no unreviewed anneal migration proposals"
        ))
    else:
        n = installed.pending_count
        verdicts.append(AxisVerdict(
            "migrate", "pending",
            f"{n} unreviewed anneal migration proposal(s) — your instruction "
            f"files may have drifted from the substrate",
            "Run `anneal-memory migrate check` to review, apply the edits that "
            "fit (under operator review — anneal never clobbers), then "
            "`anneal-memory migrate ack`. `levain update` walks this.",
        ))

    return SetDrift(verdicts=verdicts)


def _cmp(a: str, b: str) -> int:
    """-1 / 0 / 1 comparing two versions by their numeric tuples."""
    ta, tb = version_tuple(a), version_tuple(b)
    return (ta > tb) - (ta < tb)


def template_ack_target(installed_anneal: str | None) -> str | None:
    """The version a fresh ``levain init`` acks its migrate marker to: the version
    the seed templates are reconciled against (:data:`TEMPLATES_RECONCILED_ANNEAL`),
    CAPPED at the installed anneal (anneal refuses an ack ahead of the installed
    version). Returns ``None`` when the installed version is unknown — then skip
    the ack entirely (a missing read is UNKNOWN, never a default; and the cap is
    unverifiable)."""
    if installed_anneal is None:
        return None
    if _cmp(installed_anneal, TEMPLATES_RECONCILED_ANNEAL) < 0:
        return installed_anneal
    return TEMPLATES_RECONCILED_ANNEAL


# ---------------------------------------------------------------------------
# The pip-pin consistency check (the release-gate)
# ---------------------------------------------------------------------------

# A Requires-Dist line begins with the package name (+ optional [extras]) glued
# to the FIRST specifier, e.g. `anneal-memory<0.10,>=0.9.5`. Strip the name so
# the specifier scan is order-independent (the old `clause.startswith(">=")` over
# a raw `,`-split only matched when `>=` was NOT first — it worked only because
# this build backend happened to sort `<` first; L2 HIGH).
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*")


def _anneal_spec_clauses(req: str) -> list[str]:
    """The specifier clauses (e.g. ``[">=0.9.5", "<0.10"]``) from a Requires-Dist
    string for anneal-memory, with the name / extras / PEP 508 parens stripped.
    Empty for any other requirement. Order-independent; tolerant of the four pin
    forms (`name>=x,<y` / `name<y,>=x` / `name>=x` / `name (>=x,<y)`)."""
    head = req.split(";", 1)[0].strip()  # drop environment markers ('; extra == ...')
    m = _REQ_NAME_RE.match(head)
    if not m:
        return []
    if m.group(1).replace("_", "-").lower() != "anneal-memory":
        return []
    rest = head[m.end():].strip()
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()  # PEP 508 parenthesized specifier
    return [c.strip() for c in rest.split(",") if c.strip()]


def _levain_requires() -> list[str]:
    try:
        import importlib.metadata as md

        return list(md.requires("levain") or [])
    except Exception:
        return []


def pip_floor() -> str | None:
    """The anneal-memory lower-bound (``>=``) from this levain's declared
    dependencies, or ``None`` if it can't be determined. Read from the installed
    distribution metadata — for a real pip wheel this is exactly what pip will
    resolve against."""
    for req in _levain_requires():
        for clause in _anneal_spec_clauses(req):
            if clause.startswith(">="):
                return clause[2:].strip()
    return None


def pip_floor_verdict() -> AxisVerdict:
    """Cross-check the reviewed ``KNOWN_GOOD_ANNEAL`` constant against the actual
    pip dependency floor — the release-gate that catches the manifest constant
    and the ``pyproject`` pin drifting apart. For a correctly-cut release they
    agree silently; the check earns its keep when one is bumped without the
    other (and it makes a shadow-ship skew — main ahead of the published pin —
    explicit instead of a silent install failure)."""
    floor = pip_floor()
    if floor is None:
        return AxisVerdict(
            "pip-pin", "unknown",
            "could not read the levain dependency floor for anneal-memory",
        )
    cmp = _cmp(KNOWN_GOOD_ANNEAL, floor)
    if cmp == 0:
        return AxisVerdict(
            "pip-pin", "in_sync",
            f"known-good anneal {KNOWN_GOOD_ANNEAL} matches the pip floor >={floor}",
        )
    return AxisVerdict(
        "pip-pin", "drift",
        f"known-good anneal {KNOWN_GOOD_ANNEAL} != the pip floor >={floor} — the "
        f"manifest constant and the pyproject pin have drifted",
        "Reconcile KNOWN_GOOD_ANNEAL and the pyproject anneal-memory floor before "
        "release; they must name the same version.",
    )


# ---------------------------------------------------------------------------
# Lock I/O — `.levain/manifest.json` (atomic, fail-soft, mirrors anneal's marker)
# ---------------------------------------------------------------------------

def lock_path(install: Path) -> Path:
    """The recorded-set lockfile path for an install."""
    return Path(install).expanduser().joinpath(*MANIFEST_LOCK_REL)


LockStatus = Literal["absent", "ok", "corrupt"]


def read_lock_status(install: Path) -> tuple[CompatSet | None, LockStatus]:
    """Read the recorded set + distinguish the THREE lock states.

    - ``(None, "absent")`` — no lock file (a pre-manifest install). Benign:
      drift reports against the declared set alone.
    - ``(CompatSet, "ok")`` — a valid recorded set.
    - ``(None, "corrupt")`` — the file EXISTS but is unreadable / malformed /
      missing fields. This is NOT the same as absent (honesty floor — a failed
      read of an existing provenance file is an UNKNOWN to surface, not a clean
      "nothing recorded"). Never raises."""
    path = lock_path(install)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError):
        return None, "corrupt"  # exists but unreadable (perms, decode) — not absent
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    levain = data.get("levain")
    anneal = data.get("anneal")
    schema = data.get("schema")
    # Per-field isinstance (not all-over-a-tuple) so each name narrows to str.
    if not (
        isinstance(levain, str) and levain.strip()
        and isinstance(anneal, str) and anneal.strip()
        and isinstance(schema, str) and schema.strip()
    ):
        return None, "corrupt"
    return CompatSet(levain=levain, anneal=anneal, schema=schema), "ok"


def read_lock(install: Path) -> CompatSet | None:
    """The recorded set, or ``None`` if absent OR corrupt. A thin wrapper over
    :func:`read_lock_status` for callers that only need the value; doctor uses
    the status form to surface a corrupt lock (an absent one is benign)."""
    value, _status = read_lock_status(install)
    return value


def write_lock(install: Path, composed: CompatSet) -> None:
    """Atomically record the composed set to ``.levain/manifest.json``.

    Unique-tmp + file fsync + ``os.replace`` + directory fsync — the full
    atomic-write invariant anneal's marker / spore stores use, so a concurrent
    writer or a crash mid-write cannot leave a torn lock. Stamps ``recorded_at``
    (UTC ISO-8601) for provenance."""
    path = lock_path(install)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "levain": composed.levain,
            "anneal": composed.anneal,
            "schema": composed.schema,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    ) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.", suffix=".json.tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort POSIX directory fsync so the rename is durable. No-ops where a
    directory fd can't be opened/synced (e.g. Windows)."""
    try:
        fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
