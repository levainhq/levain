"""``levain update`` — update the known-good set together.

The reconcile half of the compatibility manifest. Where :mod:`levain.manifest`
DECLARES the known-good set and DETECTS drift, this module ACTS on the drift —
in one ordered, fail-safe operation:

1. **anneal version** -> bring anneal-memory to the declared known-good
   (``pip install 'anneal-memory==X'``). This is the one step that mutates the
   operator's Python environment, so it is gated: it prints the exact command
   and runs it only on confirmation (``--yes`` to auto-confirm, ``--no-pip`` to
   skip and reconcile only the store-side steps). Declining is first-class — the
   command is always printed for the operator's own package manager.
2. **store schema** -> re-run ``set-schema partnership`` if the store drifted
   (memory content is preserved; this only rewrites the schema metadata).
3. **methodology-content** -> SURFACE anneal's ``migrate check`` proposals for
   the operator to apply under review. We never auto-apply (anneal's never-clobber
   contract) and never auto-ack; ``--ack`` advances the marker AFTER the operator
   has applied the edits, as a separate deliberate intent.
4. **record the lock** -> write ``.levain/manifest.json`` with the
   actually-composed versions (reality after reconcile, not the intended target).

``--dry-run`` shows the full plan and changes NOTHING — the honesty floor: a plan
is not a result. The default (no ``--yes``) still PROMPTS before the env-mutating
pip step.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from levain import manifest
from levain.manifest import AxisVerdict, CompatSet, InstalledSet

Emit = Callable[[str], None]


def run_update(
    path: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    no_pip: bool = False,
    ack: bool = False,
    emit: Emit = print,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """Reconcile the install's set to the declared known-good. Returns 0 when
    the set is at (or was brought to) known-good, 1 when actionable drift
    remains (so it composes with shell pipelines, like ``levain doctor``).

    ``confirm`` is the yes/no gate for the env-mutating pip step; it defaults to
    an interactive ``input`` prompt (auto-yes under ``--yes``). Injectable so the
    apparatus / tests can drive it without a TTY."""
    install = Path(str(path)).expanduser().resolve()
    store = install / ".levain" / "memory.db"
    import shutil

    anneal_path = shutil.which("anneal-memory") or "anneal-memory"

    if confirm is None:
        confirm = _make_confirm(yes)

    declared = manifest.declared_set()
    installed = manifest.discover_installed_set(store, anneal_path)
    lock = manifest.read_lock(install)
    drift = manifest.compute_drift(declared, installed, lock)

    emit(f"levain update — reconciling {install} to the known-good set\n")
    emit(_format_set("declared known-good", declared))
    emit(_format_installed("installed now", installed))
    if lock is not None:
        emit(_format_set("last composed (lock)", lock))
    emit("")
    emit("drift:")
    for v in drift.verdicts:
        emit(_format_verdict(v))
    emit("")

    # --dry-run short-circuits FIRST, BEFORE any write — including the lock
    # refresh in the clean/advisory early-return below (codex final: that
    # early-return ran before this check, so a dry-run on a clean install wrote
    # the lockfile, violating "a plan is not a result").
    if dry_run:
        if not drift.has_actionable_drift and not drift.has_unknown:
            emit("--dry-run: nothing to reconcile mechanically. Nothing was changed.")
            return 0
        emit("--dry-run: the plan above is what `levain update` WOULD do. "
             "Nothing was changed (a plan is not a result).")
        # An UNKNOWN axis is a fail-closed condition too: a plan built on an
        # unverified read is not a clean bill of health.
        return 1

    if not drift.has_actionable_drift and not drift.has_unknown:
        if drift.in_sync:
            emit("Already at the known-good set — nothing to reconcile.")
        else:
            # Only advisory verdicts remain (e.g. anneal AHEAD of known-good) —
            # nothing `levain update` can act on mechanically; the table above
            # says what to review.
            emit("Nothing for `levain update` to reconcile mechanically — see the "
                 "advisories above (they need your review, not a pip step).")
        # Refresh a missing/stale lock so a pre-manifest install gets recorded.
        # has_unknown is False here, so anneal/schema are known — _record_lock can
        # only return False on a write FAULT; don't claim success without the
        # recorded baseline (codex re-verify: the early path missed this).
        if not _record_lock(install, installed, emit):
            emit("Could not record the compatibility lock — re-run `levain update`.")
            return 1
        return 0

    # -- 1. anneal version (the env-mutating step, gated) --
    # ONLY a `behind` anneal triggers a pip reconcile. The `anneal-lock` drift
    # (installed != lock) must NOT drive the pip step: when installed is behind,
    # `behind` already covers it; when installed == declared it is a no-op (the
    # end-of-run lock refresh re-stamps the stale lock); when installed is AHEAD,
    # acting would DOWNGRADE a working newer anneal — the exact bug L1+L2 caught,
    # where the lock-drift term hijacked this and left the `elif ahead` guard
    # dead. The lock-drift verdict stays for REPORTING (the table above); it
    # never pips.
    anneal_v = drift.of("anneal")
    needs_anneal = anneal_v is not None and anneal_v.status == "behind"
    if needs_anneal:
        _reconcile_anneal(
            declared.anneal, no_pip=no_pip, confirm=confirm, emit=emit
        )
        # Re-discover so downstream steps + the lock see the new version.
        installed = manifest.discover_installed_set(store, anneal_path)
    elif anneal_v is not None and anneal_v.status == "ahead":
        emit("• anneal is AHEAD of this levain release's known-good — NOT "
             "downgrading. Upgrade levain (`pip install -U levain`) so the "
             "methodology catches up, then re-run `levain doctor`.")

    # -- 2. store schema (drive from the CURRENT installed, NOT the pre-pip drift
    #    snapshot — a schema only readable AFTER the anneal upgrade would otherwise
    #    be skipped until a second run; codex L3 stale-snapshot) --
    if installed.schema is not None and installed.schema != declared.schema:
        emit(f"\n• reconciling store schema -> {declared.schema} "
             f"(memory content preserved)...")
        ok, out = _run_anneal(store, anneal_path, ["set-schema", declared.schema])
        emit(f"  {'done.' if ok else 'FAILED: ' + out}")
        if ok:
            installed = manifest.discover_installed_set(store, anneal_path)

    # -- 3. methodology-content (surface proposals; never auto-apply / auto-ack) --
    if installed.pending_count:
        emit(f"\n• {installed.pending_count} anneal migration proposal(s) — "
             f"review and apply these to your instruction files (anneal never "
             f"edits them for you):\n")
        shown_ok, out = _run_anneal(store, anneal_path, ["migrate", "check"])
        emit(_indent(out if shown_ok else "(could not read proposals)"))
        if ack and not shown_ok:
            # NEVER ack proposals the operator did not actually see THIS run
            # (codex L3 HIGH) — the entire meaning of --ack is "I reviewed these".
            emit("\n• --ack SKIPPED — could not read the proposals to review; "
                 "marker unchanged.")
        elif ack and installed.anneal is None:
            # pending was read from a partial migrate-check JSON (pending present
            # but installed_version absent) — the ack target is undeterminable, so
            # acking would fall back to known-good and mutate the marker on an
            # unknown installed version (codex re-verify). Skip.
            emit("\n• --ack SKIPPED — the installed anneal version is unknown, so "
                 "the marker cannot be set safely; marker unchanged.")
        elif ack:
            # Ack to the INSTALLED version (what `migrate check` just showed and
            # the operator reviewed), never past it (complement L3: this sidesteps
            # the pre-release `_cmp`-equality quirk, and anneal refuses an ack
            # ahead of the installed version anyway).
            ack_target = _ack_target(installed.anneal, declared.anneal)
            emit(f"\n• --ack: recording instruction files reconciled up to "
                 f"anneal {ack_target}...")
            ack_ok, ack_out = _run_anneal(
                store, anneal_path, ["migrate", "ack", ack_target]
            )
            emit(f"  {'acknowledged up to ' + ack_target + '.' if ack_ok else 'FAILED: ' + ack_out}")
            if ack_ok:
                # Re-discover so the final verdict reflects the now-cleared pending
                # set — else `--ack` returns 1 even on a successful ack (codex L3).
                installed = manifest.discover_installed_set(store, anneal_path)
        else:
            emit("\n  After applying the edits, run `levain update --ack` (or "
                 "`anneal-memory migrate ack`) to record them as reconciled.")
    elif ack:
        # --ack requested but nothing readable to acknowledge — say so rather than
        # silently no-op the operator's flag.
        emit("\n• --ack: no migration proposals to acknowledge "
             "(none pending, or they could not be read) — marker unchanged.")

    # -- 4. record the lock (reality after reconcile) --
    lock_written = _record_lock(install, installed, emit)

    # -- final verdict --
    final = manifest.compute_drift(
        declared, installed, manifest.read_lock(install)
    )
    emit("")
    if final.has_actionable_drift:
        remaining = [v.axis for v in final.verdicts if v.status in manifest._ACTIONABLE]
        emit(f"Reconcile incomplete — still drifting: {', '.join(remaining)}. "
             f"Re-run after addressing the steps above.")
        return 1
    if final.has_unknown:
        # Honesty floor: a failed read is not a clean read. Never claim the set
        # is at known-good on an axis we could not verify (L1 HIGH).
        unknown = [v.axis for v in final.verdicts if v.status == "unknown"]
        emit(f"Could not VERIFY the set — UNKNOWN: {', '.join(unknown)}. A failed "
             f"read is not a clean read, so this is NOT confirmed at known-good. "
             f"Check that anneal-memory is installed and the store is reachable.")
        return 1
    if not lock_written:
        # Runtime may be reconciled, but the provenance baseline was NOT recorded
        # (a write fault) — don't claim full success on a missing lock (codex L3).
        emit("Runtime reconciled, but the compatibility lock could NOT be "
             "recorded — re-run `levain update` so the baseline is written.")
        return 1
    if final.in_sync:
        emit("Set reconciled to known-good.")
    else:
        # No actionable drift and no unknowns, but not fully in_sync = only
        # advisories remain (e.g. anneal ahead of known-good). Don't overstate it.
        emit("Reconciled what `levain update` can — advisories remain above "
             "(e.g. anneal ahead of known-good → upgrade levain when convenient).")
    return 0


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _reconcile_anneal(
    target: str, *, no_pip: bool, confirm: Callable[[str], bool], emit: Emit
) -> None:
    cmd = [sys.executable, "-m", "pip", "install", f"anneal-memory=={target}"]
    printable = " ".join(cmd)
    if no_pip:
        emit(f"\n• anneal needs updating to {target}. --no-pip set — update it "
             f"with YOUR package manager (pin anneal-memory=={target}). If you use "
             f"pip directly:\n    {printable}\n"
             f"  (under poetry / uv / conda / pipx, pin it THERE — a raw pip "
             f"install can be reverted by your manager's next sync.)")
        return
    emit(f"\n• anneal-memory needs updating to the known-good {target}.")
    if not confirm(f"  Run `{printable}`?"):
        emit(f"  Declined. Run it yourself when ready:\n    {printable}")
        return
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        emit(f"  pip failed to launch: {exc}\n    Run it yourself: {printable}")
        return
    if result.returncode == 0:
        emit(f"  anneal-memory updated to {target}.")
    else:
        tail = (result.stderr or result.stdout).strip()[-500:]
        emit(f"  pip FAILED:\n{_indent(tail)}\n    Run it yourself: {printable}")


def _record_lock(install: Path, installed: InstalledSet, emit: Emit) -> bool:
    """Write the lock from the actually-installed values; return True iff a
    VERIFIED lock was written.

    If a core field (anneal / schema) could not be discovered, SKIP the write
    rather than record an unverified baseline — a poisoned lock would read as a
    clean compose next session (honesty floor, L1 HIGH) — and return False. A
    write fault (OSError) also returns False so the caller can refuse to claim
    full success on a missing baseline (codex L3). `levain` is always known."""
    if installed.anneal is None or installed.schema is None:
        emit("\n• could not verify the installed set (anneal/schema unread) — "
             "lock NOT written (a lock must record a verified compose).")
        return False
    composed = CompatSet(
        levain=installed.levain,
        anneal=installed.anneal,
        schema=installed.schema,
    )
    try:
        manifest.write_lock(install, composed)
        emit(f"\n• recorded the composed set -> {manifest.lock_path(install)}")
        return True
    except OSError as exc:
        emit(f"\n• could not write the lock ({exc}); the set is unchanged on disk.")
        return False


def _ack_target(installed_anneal: str | None, known_good: str) -> str:
    """The version to ack to: the INSTALLED version (what `migrate check` showed
    and the operator reviewed), never past it. Acking to known-good when the
    installed anneal is a pre-release of it (e.g. installed ``0.9.5rc1`` vs
    known-good ``0.9.5``) would advance the marker past the running code's
    migration set and suppress proposals the operator never saw — the naive
    version compare treats them as equal (complement L3). anneal refuses an ack
    ahead of the installed version anyway. Falls back to known-good only when the
    installed version is unknown."""
    return installed_anneal if installed_anneal is not None else known_good


def _run_anneal(
    store: Path, anneal_path: str, sub_args: list[str], *, timeout: float = 60.0
) -> tuple[bool, str]:
    """Run an anneal subcommand (text output), trying the console script then the
    module form. Returns (ok, stdout-or-error-tail)."""
    candidates = [
        [anneal_path, "--db", str(store), *sub_args],
        [sys.executable, "-m", "anneal_memory", "--db", str(store), *sub_args],
    ]
    last = ""
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"{cmd[0]}: timed out"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            last = f"{cmd[0]}: {exc}"
            continue
        if r.returncode == 0:
            return True, r.stdout
        last = (r.stderr or r.stdout).strip()[:500]
    return False, last


# ---------------------------------------------------------------------------
# Confirm + formatting
# ---------------------------------------------------------------------------

def _make_confirm(yes: bool) -> Callable[[str], bool]:
    if yes:
        return lambda _prompt: True

    def _prompt(prompt: str) -> bool:
        try:
            return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    return _prompt


def _format_set(label: str, s: CompatSet) -> str:
    return f"  {label:>22}: levain {s.levain} · anneal {s.anneal} · schema {s.schema}"


def _format_installed(label: str, s: InstalledSet) -> str:
    anneal = s.anneal or "?"
    schema = s.schema or "?"
    acked = s.migrate_acked or "none"
    return (f"  {label:>22}: levain {s.levain} · anneal {anneal} · schema "
            f"{schema} · migrate-acked {acked}")


_STATUS_BADGE = {
    "in_sync": "ok ",
    "behind": "!! ",
    "ahead": "!! ",
    "drift": "!! ",
    "pending": " . ",
    "unknown": " ? ",
}


def _format_verdict(v: AxisVerdict) -> str:
    badge = _STATUS_BADGE.get(v.status, "   ")
    line = f"  [{badge}] {v.axis}: {v.detail}"
    if v.hint and v.status != "in_sync":
        line += f"\n        -> {v.hint}"
    return line


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + ln for ln in text.splitlines())
