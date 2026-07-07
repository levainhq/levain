"""levain.reconcile — pack-drift reconcile: re-compose a pulled pack layer into an
EXISTING install.

The DOWNSTREAM half of the compat-manifest pack axis (``levain.manifest``). A pack
is UPSTREAM (the operator ``git pull``s updates into its source dir); the install
is DOWNSTREAM (it grows independently). When ``compute_pack_drift`` reports a pack's
source changed, this brings the changes into the install — including interview-layer
changes — and never clobbers operator edits.

- **verbatim seed** files — copied byte-exact at init, so the install file 3-way
  compares against the recorded source hash: unedited -> fast-forward to the new
  source; operator-edited -> back up the edit, apply the new doctrine, report.
- **render seed** files — a template rendered from the operator's interview answers.
  On a template change: parse the new template, TARGETED re-prompt for only the slots
  it ADDED (never the whole interview, never blank-fill), re-render with the persisted
  + new answers, and 3-way against the recorded rendered-output hash: an UNEDITED
  install file fast-forwards to the new render; an EDITED one is NEVER clobbered — the
  new version lands at ``<file>.new`` for a hand-merge. (No persisted answers, or a
  non-interactive run with new slots -> surface for re-onboard, never blank-fill.)
- **new / dropped** seeds are added / removed (edits backed up).
- **docs** are rebuilt wholesale by the caller (``_copy_pack_docs``).
- **activation / pack.toml** changes are surfaced for review (activation is
  substituted at install, so a re-layer is the ``apply_init`` path, not a hash merge).
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from levain.install import (
    InitError,
    _copy_pack_docs,
    _timestamped_backup_path,
    _write_brand_config,
    read_answers,
    write_answers,
)
from levain.manifest import (
    PackDrift,
    PackProvenance,
    _sha256_file,
    compute_pack_drift,
    read_pack_locks_status,
)
from levain.packs import PackError, PackManifest, compose_brand, load_pack_manifest

# A prompter asks the operator for the pack's NEWLY-ADDED render slots. It returns
# ``{slot: answer}`` for the given InterviewFields, or ``None`` when it cannot prompt
# (a non-interactive run) — in which case the reconcile surfaces the change for a
# re-onboard rather than blank-filling. Injectable so the apparatus/tests drive it.
Prompter = Callable[[list], "dict[str, str] | None"]


def tty_prompter(fields: list) -> dict[str, str] | None:
    """The default: ask each new field on the terminal. ``None`` if there is no TTY
    (so a scripted ``levain update`` never blocks on input / blank-fills)."""
    if not sys.stdin.isatty():
        return None
    out: dict[str, str] = {}
    for f in fields:
        guide = f" — {f.guidance}" if getattr(f, "guidance", None) else ""
        try:
            out[f.slot] = input(f"  new field '{f.slot}'{guide}\n    > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
    return out


@dataclass
class PackReconcile:
    """The outcome of reconciling ONE pack's drift into the install."""

    name: str
    status: str  # "unchanged" | "reconciled" | "needs_review" | "source_missing"
    updated: list[str] = field(default_factory=list)    # files fast-forwarded to new source
    backed_up: list[str] = field(default_factory=list)  # operator-edited -> .bak + applied
    added: list[str] = field(default_factory=list)      # new seeds copied in
    removed: list[str] = field(default_factory=list)    # dropped seeds removed
    review: list[str] = field(default_factory=list)     # surfaced for the operator
    # New rendered-output hashes for render seeds this run wrote (folded into the
    # recorded `rendered` map so a fast-forwarded render file stops re-drifting).
    render_written: dict[str, str] = field(default_factory=dict)
    new_provenance: PackProvenance | None = None


def _load_manifest(source: Path):
    """The current source pack's parsed manifest, or None if pack.toml can't be
    parsed (the pack is then unreconcilable -> needs_review)."""
    try:
        return load_pack_manifest(source)
    except PackError:
        return None


def _reconcile_render_file(
    install: Path,
    drift: PackDrift,
    rel: str,
    source: Path,
    *,
    answers: dict[str, str],
    prompter: Prompter | None,
    apply: bool,
    emit: Callable[[str], None],
    r: PackReconcile,
) -> None:
    """The render-slot 3-way for one changed render seed: targeted re-prompt for
    added slots -> re-render with persisted+new answers -> fast-forward an unedited
    install file, or side-by-side a ``.new`` for an edited one (never clobber)."""
    from levain.interview import build_field_plan, parse_template, render_template

    src_tpl = source / rel
    try:
        spec = parse_template(src_tpl)
        fields = build_field_plan([spec])
    except Exception as e:  # noqa: BLE001 — a malformed template surfaces, never crashes update
        r.review.append(rel)
        emit(f"  pack {drift.name!r}: {rel} render template is malformed ({e}) — review.")
        return

    slots = {f.slot for f in fields}
    new_slots = sorted(slots - set(answers))
    dst = install / rel

    merged = dict(answers)
    if new_slots:
        if not answers:
            r.review.append(rel)
            emit(f"  pack {drift.name!r}: {rel} added field(s) {new_slots} but this "
                 f"install has no persisted answers — re-onboard (`levain init`) to "
                 f"capture them.")
            return
        if not apply:
            emit(f"  pack {drift.name!r}: {rel} would re-prompt for new field(s) "
                 f"{new_slots} (dry-run — nothing asked or written).")
            return
        new_fields = [f for f in fields if f.slot in new_slots]
        # Frame the re-prompt with the pack's CHANGELOG context (the WHY) so the
        # operator answers a field that didn't exist at onboarding well — CHANGELOG.md
        # is optional but load-bearing for re-prompt quality (absent -> just the
        # template's own field guidance).
        _emit_changelog_context(source, drift.name, emit)
        got = prompter(new_fields) if prompter is not None else None
        # A prompter that can't answer (None) OR returns a partial dict (a custom
        # prompter that skipped a field) must SURFACE, never blank-fill — the contract
        # is all-or-surface (L1 #6). The shipped tty_prompter already returns all-or-None.
        if got is None or not all(f.slot in got for f in new_fields):
            r.review.append(rel)
            emit(f"  pack {drift.name!r}: {rel} added field(s) {new_slots} — run "
                 f"`levain update` interactively to answer them (or `levain init` to "
                 f"re-onboard); nothing written, so it re-surfaces until then.")
            return
        for f in new_fields:
            merged[f.slot] = got[f.slot]
        # Fold the new answers back into the shared set so they persist for good.
        answers.update({f.slot: merged[f.slot] for f in new_fields})

    new_rendered = render_template(spec, merged)
    # Guard the render-file FS ops the same way the verbatim path is guarded — an
    # OSError on one render file must SURFACE + continue, never propagate and crash the
    # whole `levain update` (which would lose any answer collected earlier this run,
    # persisted only after the loop) (complement L3 #4).
    try:
        dst_text = dst.read_text(encoding="utf-8") if dst.is_file() else None

        if dst_text == new_rendered:
            # Already at the target render — operator hand-merged the .new, or an
            # unedited file re-renders identically. ADVANCE + drop any stale .new so it
            # stops re-surfacing (L1 #3: else a hand-merged file .news forever).
            if apply:
                r.render_written[rel] = _sha256_file(dst)
                _clear_side(dst)
            r.updated.append(rel)
            return

        rec_rendered = drift.recorded.rendered.get(rel)
        edited = dst.is_file() and _sha256_file(dst) != rec_rendered
        if not edited:
            # Unedited (matches the recorded render) but the template changed -> fast-forward.
            if apply:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(new_rendered, encoding="utf-8")
                r.render_written[rel] = _sha256_file(dst)  # hash the FILE (init parity; Windows-safe, L2)
            r.updated.append(rel)
        else:
            # Operator edited this render file AND the template changed — never clobber.
            if apply:
                (dst.with_suffix(dst.suffix + ".new")).write_text(new_rendered, encoding="utf-8")
            r.review.append(rel)
            emit(f"  pack {drift.name!r}: {rel} template changed AND you edited it — the "
                 f"updated version (your answers + any new field) is at {rel}.new; merge "
                 f"by hand. Your {rel} is untouched.")
    except OSError as e:
        emit(f"  pack {drift.name!r}: could not write {rel} ({e}) — review.")
        r.review.append(rel)


def _clear_side(dst: Path) -> None:
    """Remove a stale ``<file>.new`` (best-effort) once the operator has merged it and
    the render file is back at the reconciled target."""
    side = dst.with_suffix(dst.suffix + ".new")
    try:
        if side.is_file():
            side.unlink()
    except OSError:
        pass


def _emit_changelog_context(source: Path, name: str, emit: Callable[[str], None]) -> None:
    """Surface the pack's ``CHANGELOG.md`` head (its latest entry) before a re-prompt,
    so the operator understands WHY a field they never answered appeared. Optional —
    silent when the pack ships no CHANGELOG (the template's own field guidance still
    frames each question)."""
    try:
        text = (source / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return
    head: list[str] = []
    seen_heading = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            if seen_heading:
                break
            seen_heading = True
        head.append(ln)
        if len(head) >= 20:
            break
    body = "\n".join(head).strip()
    if body:
        emit(f"  pack {name!r} added interview field(s). From its CHANGELOG:")
        for ln in body.splitlines():
            emit(f"    {ln}")


def reconcile_pack(
    install: Path,
    drift: PackDrift,
    *,
    answers: dict[str, str],
    prompter: Prompter | None,
    apply: bool,
    emit: Callable[[str], None],
) -> PackReconcile:
    """Reconcile a single pack's drift. ``apply=False`` reports only (dry-run).
    ``answers`` is the shared, MUTABLE persisted-answer set (a render re-prompt adds
    to it in place); the caller persists it once after all packs."""
    r = PackReconcile(name=drift.name, status="unchanged", new_provenance=drift.recorded)

    if drift.status == "source_missing":
        r.status = "source_missing"
        emit(f"  pack {drift.name!r}: source {drift.source} is gone — cannot reconcile; "
             f"the install keeps the last-composed doctrine.")
        return r
    if drift.status == "unchanged":
        return r

    source = Path(drift.source)
    cur_manifest = _load_manifest(source)
    if cur_manifest is None:
        r.status = "needs_review"
        r.review.append("pack.toml")
        emit(f"  pack {drift.name!r}: pack.toml is missing or malformed at the source — "
             f"review before reconciling.")
        return r
    render_names = set(cur_manifest.render)

    current = drift.current_files or {}
    recorded = drift.recorded.files
    acted = False

    def _surface(rel: str, why: str) -> None:
        r.review.append(rel)
        emit(f"  pack {drift.name!r}: {rel} {why} — review (run `levain init` "
             f"to re-onboard, or reconcile by hand).")

    # A render-MEMBERSHIP flip (a file goes verbatim<->render via pack.toml `render`,
    # possibly with UNCHANGED seed bytes) is invisible to a source-hash diff — the file
    # would silently stay literal `{{SLOT}}` (or a stale render). Surface it for a
    # re-onboard, which re-composes it in the correct mode (codex L3 #1).
    for name in sorted(render_names ^ set(drift.recorded.render)):
        rel = f"seed/{name}"
        _surface(rel, "render-mode changed (verbatim <-> render)")

    for rel in sorted(set(drift.added) | set(drift.modified)):
        if rel == "pack.toml":
            # Metadata (version / order / render). It ADVANCES in provenance below —
            # it must NOT trap the install: a version-only bump is documented as
            # harmless sugar (L1 #2 — surfacing it forced permanent exit-1). A render
            # change drives the reconcile through its own seed files; an order change
            # (cross-pack override) is a rare v1 limitation, not a permanent trap.
            continue
        if rel.startswith("activation/"):
            _surface(rel, "activation changed")
            continue
        if rel.startswith("docs/"):
            continue  # docs rebuilt wholesale by the caller
        if not rel.startswith("seed/"):
            _surface(rel, "changed")
            continue
        fname = rel[len("seed/"):]
        if fname in render_names:
            before = len(r.review)
            _reconcile_render_file(
                install, drift, rel, source,
                answers=answers, prompter=prompter, apply=apply, emit=emit, r=r,
            )
            if len(r.review) == before:  # a render file we actually reconciled
                acted = True
            continue
        # verbatim seed — 3-way merge
        src_file = source / rel
        dst = install / "seed" / fname
        new_hash = current.get(rel)
        if new_hash is None or not src_file.is_file():
            continue
        is_add = rel not in recorded
        try:
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if apply:
                    shutil.copy2(src_file, dst)
                (r.added if is_add else r.updated).append(rel)
                acted = True
                continue
            inst_hash = _sha256_file(dst)
            if inst_hash == new_hash:
                continue  # already current
            old_hash = recorded.get(rel)
            if inst_hash == old_hash:
                if apply:
                    shutil.copy2(src_file, dst)
                r.updated.append(rel)  # unedited -> fast-forward
            else:
                if apply:
                    shutil.copy2(dst, _timestamped_backup_path(dst))
                    shutil.copy2(src_file, dst)
                r.backed_up.append(rel)  # operator-edited -> backup + apply
            acted = True
        except OSError as e:
            emit(f"  pack {drift.name!r}: could not reconcile {rel} ({e}) — review.")
            r.review.append(rel)

    # dropped seed files: the pack authoritatively removed the doctrine. Back up any
    # operator-diverged copy before unlinking (a RENDERED file always diverges from
    # the recorded SOURCE hash, so its answers are never lost). activation/manifest
    # drops are surfaced, not auto-removed.
    for rel in sorted(drift.removed):
        if not rel.startswith("seed/"):
            _surface(rel, "removed at source")
            continue
        dst = install / "seed" / rel[len("seed/"):]
        if not dst.exists():
            continue
        try:
            if _sha256_file(dst) != recorded.get(rel) and apply:
                shutil.copy2(dst, _timestamped_backup_path(dst))
            if apply:
                dst.unlink()
            r.removed.append(rel)
            acted = True
        except OSError as e:
            emit(f"  pack {drift.name!r}: could not remove dropped {rel} ({e}) — review.")
            r.review.append(rel)

    # An importable-seed SET change means the adapter @import block needs regenerating
    # — surface it (v1 does not surgically edit the rendered adapter file).
    seed_added = [rel for rel in drift.added if rel.startswith("seed/")]
    seed_removed = [rel for rel in drift.removed if rel.startswith("seed/")]
    if seed_added or seed_removed:
        emit(f"  pack {drift.name!r}: the seed set changed (added {len(seed_added)}, "
             f"removed {len(seed_removed)}) — the adapter @import list is NOT regenerated "
             f"by update, so an added seed is on disk but INERT (not loaded) and a removed "
             f"one leaves a dangling import. Re-onboard (`levain init`) to regenerate it.")
        # Gate to review (exit 1) + keep these files' provenance at the OLD hash so
        # `pack_drift` + the non-zero exit keep flagging until the operator re-onboards
        # — never report success over doctrine that isn't actually active (L1 #1 / M2).
        for rel in seed_added + seed_removed:
            if rel not in r.review:
                r.review.append(rel)

    # Advance provenance PER-FILE: every source file moves to its new hash EXCEPT the
    # ones surfaced for review (they keep the OLD hash so ONLY they re-surface next
    # run). The `rendered` map advances for fast-forwarded render files (r.render_written).
    files = dict(current)
    for rel in r.review:
        if rel in recorded:
            files[rel] = recorded[rel]
        else:
            files.pop(rel, None)
    # Docs are rebuilt WHOLESALE and separately (run_pack_reconcile), so HOLD docs/* at
    # the recorded hash here — run_pack_reconcile advances them only AFTER the rebuild
    # actually succeeds. Else a failed/skipped rebuild leaves docs provenance advanced
    # while the docs are stale/gone and never retried (complement L3 #2).
    for rel in [x for x in list(files) if x.startswith("docs/")]:
        if rel in recorded:
            files[rel] = recorded[rel]
        else:
            files.pop(rel, None)
    # Render files: review ones keep their old rendered hash (start from recorded);
    # fast-forwarded ones advance to the freshly-written render's hash.
    new_rendered_map = dict(drift.recorded.rendered)
    new_rendered_map.update(r.render_written)
    # Advance the recorded version to the CURRENT pack.toml's (L1 #2 — else a version
    # bump never clears; the notice would print the stale version forever).
    r.new_provenance = PackProvenance(
        name=drift.name, source=drift.source, version=cur_manifest.version,
        files=files, rendered=new_rendered_map, render=tuple(cur_manifest.render),
    )
    r.status = "needs_review" if r.review else ("reconciled" if acted else "unchanged")
    return r


def reconcile_packs(
    install: Path,
    drifts: Sequence[PackDrift],
    *,
    answers: dict[str, str],
    prompter: Prompter | None,
    apply: bool,
    emit: Callable[[str], None],
) -> list[PackReconcile]:
    """Reconcile every drifted pack; return per-pack outcomes."""
    return [
        reconcile_pack(install, d, answers=answers, prompter=prompter, apply=apply, emit=emit)
        for d in drifts
    ]


def run_pack_reconcile(
    install: Path,
    *,
    dry_run: bool,
    emit: Callable[[str], None],
    prompter: Prompter | None = None,
) -> tuple[list[PackProvenance] | None, bool, bool]:
    """The ``levain update`` pack phase. Detects pack drift, reconciles the changes
    into the install (verbatim + render, targeted re-prompt for new render slots),
    rebuilds the composed docs, persists any newly-answered slots, and returns
    ``(provenance_to_record, drifted, needs_review)``.

    ``provenance_to_record`` is ``None`` when the lock's pack provenance is CORRUPT —
    the caller must then NOT rewrite the packs (that would erase the baseline); it is
    ``[]`` only when packs are genuinely absent. ``prompter`` defaults to the TTY
    prompter; a non-interactive run (no TTY) surfaces render-slot additions for
    re-onboard rather than blank-filling. On ``dry_run`` it reports the plan + writes
    nothing."""
    recorded, status = read_pack_locks_status(install)
    if status == "corrupt":
        emit("\n• pack provenance in the lock is UNREADABLE — cannot reconcile pack "
             "drift, and the lock is NOT rewritten (that would erase the baseline). "
             "Restore .levain/manifest.json or re-onboard (`levain init`).")
        return None, True, True
    if not recorded:  # absent — genuinely no packs
        return [], False, False
    drifts = compute_pack_drift(recorded)
    if not any(d.drifted for d in drifts):
        return recorded, False, False

    emit("\n• pack-layer drift:")
    for d in drifts:
        if not d.drifted:
            continue
        if d.status == "source_missing":
            emit(f"  {d.name}: source is gone ({d.source})")
        else:
            emit(f"  {d.name}: changed (+{len(d.added)} ~{len(d.modified)} -{len(d.removed)})")

    if prompter is None:
        prompter = tty_prompter
    answers = read_answers(install)
    before = dict(answers)
    outcomes = reconcile_packs(
        install, drifts, answers=answers, prompter=prompter, apply=not dry_run, emit=emit
    )
    # A source_missing pack CANNOT be reconciled — it is a needs_review / exit-1
    # condition, not a silent success (codex L3 #2): the source dir is gone, so the
    # operator must restore or drop it. The hook keeps warning; the exit code must too.
    any_missing = any(o.status == "source_missing" for o in outcomes)
    needs_review = any_missing or any(o.review for o in outcomes)
    if dry_run:
        emit("  --dry-run: the pack reconcile above is what WOULD happen. Nothing changed.")
        return recorded, True, needs_review

    # Persist newly-answered render slots BEFORE advancing provenance — a failed write
    # must NOT leave the manifest advanced while answers.json lacks the new slot (codex
    # L3 #3). Hold the recorded provenance so the change re-surfaces next run.
    if answers != before and not write_answers(install, answers, emit):
        emit("  ! could not persist the new answers — pack provenance NOT advanced so "
             "the change re-surfaces; fix the write and re-run `levain update`.")
        return recorded, True, True

    # Rebuild the composed docs from the pulled sources — but NOT destructively while a
    # recorded source is MISSING: `_copy_pack_docs` wipes the docs root first, so a
    # rebuild that omits a vanished pack would DELETE its previously-composed docs,
    # contradicting "the install keeps the last-composed doctrine" (codex L3 #2). Skip
    # the rebuild until the missing pack is restored or dropped.
    docs_ok = False
    if any_missing:
        emit("  note: a pack source is missing — docs NOT rebuilt (keeping the "
             "last-composed manual) until it is restored or removed.")
    else:
        pairs: list[tuple[object, Path]] = []
        manifests: list[PackManifest] = []
        for d in drifts:
            src = Path(d.source)
            try:
                mf = load_pack_manifest(src)
            except PackError:
                continue
            pairs.append((mf, src))
            manifests.append(mf)
        try:
            _copy_pack_docs(install, pairs)  # type: ignore[arg-type]
            # Re-bake the white-label alongside the docs rebuild — brand tracks docs on
            # EVERY drift event, not just init (a pack.toml `[brand]` edit IS drift; it is
            # in the hashed set — manifest._pack_source_hashes). Same IP-boundary discipline
            # as the docs wipe: a changed brand re-bakes, a REMOVED `[brand]` clears the
            # stale chrome (compose_brand → None → _write_brand_config clears). Without this
            # the docstring's "same discipline as _copy_pack_docs" parity was a false claim —
            # the config brand would go stale on update (kimi/complement L2). Best-effort.
            _write_brand_config(install, compose_brand(manifests), emit)
            docs_ok = True
        except (OSError, InitError) as e:
            emit(f"  note: could not rebuild pack docs ({e}); `levain docs` may be stale "
                 f"— docs kept flagged so the next `levain update` retries.")

    # Advance each pack's docs/* provenance ONLY now that the rebuild actually
    # succeeded (reconcile_pack held them at recorded). A failed/skipped rebuild leaves
    # docs re-drifting so the next run retries, never a silent "resolved" over stale/
    # gone docs (complement L3 #2).
    new_prov: list[PackProvenance] = []
    for o, d in zip(outcomes, drifts):
        if o.new_provenance is None:
            continue
        new_prov.append(_advance_docs(o.new_provenance, d.current_files) if docs_ok
                        else o.new_provenance)
    return new_prov, True, needs_review


def _advance_docs(prov: PackProvenance, current_files: dict[str, str] | None) -> PackProvenance:
    """Fold the pack's current ``docs/*`` source hashes into its provenance — called
    only after a successful docs rebuild, so docs drift is marked resolved iff the
    rebuild really happened."""
    docs = {r: h for r, h in (current_files or {}).items() if r.startswith("docs/")}
    if not docs:
        return prov
    files = dict(prov.files)
    files.update(docs)
    return replace(prov, files=files)
