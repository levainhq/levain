"""levain.writes — Levain v2 Slice 2: the governed write layer (Class A + Class B).

The inverse of ``dashboard.py``'s read path. Two governed edit classes, both
*inputs* to the entity's cognition — never its consolidated conclusions:

**Class A — operator-input (direct file edit).** Declarative, human-is-fan-in,
safe; written straight to the file/field with backup + audit + atomic write +
undo. Slice 2a: the operator's profile (``seed/world.md`` sections), thinking
style (``activation/posture.md`` / ``recency_directives.md``), the operator-set
entity name (``.levain/config.json``). Slice 2b-i: the neocortex ``State``
section — the one Class-A section of the consolidated-cognition file (live-state,
last-writer-wins), confined to ``State`` alone (``_apply_state_edit``).

**Class B — lifecycle data (verb-mediated).** Slice 2b-ii: the operator's open
loops (**spores** — ``touch`` / ``descend`` / ``ascend``) and raw inputs
(**episodes** — ``tombstone``), mutated ONLY through anneal's own *validated*
verbs against a WRITABLE anneal handle (the dashboard read path opens everything
``read_only=True``; this is the one governed writable path). The lifecycle
invariants — kind-validation, tombstones, id integrity — live in anneal, not
here (``thinness_is_the_architecture``); this layer locates the store, calls the
verb, and records the change. A *destructive* verb (resolving a spore /
tombstoning an episode) requires an explicit ``confirm: true`` — the per-write
confirm gate, enforced server-side (a raw client without it is refused too).

What is NOT writable here, by construction: the five felt-layer neocortex
sections + the Hebbian/limbic/crystal layers (Class C — the consolidate's own
conclusions), and the Class C-view seed docs. **Crystals (graduated patterns)
are deliberately Class-B-EXCLUDED**: a crystal is the entity's own consolidated
wisdom, not an operator input — retiring one would overwrite the entity's
cognition, so it routes through anneal's crystal *decision channel* at
consolidate time, never an operator button (scope §1, Fork 1 = A). That is the
whole point — the operator governs inputs, never the entity's cognition.

The governance model (load-bearing — this is the seam the moat is built on):

1. **The writable set is the read layer's Class-A tagging, re-validated server-side
   on EVERY write.** ``_assert_class_a_target`` re-derives the allowlist from
   ``dashboard._read_config_docs`` (the single source of truth for which surface is
   which edit-class) and refuses anything that is not a Class-A target. The frontend
   enabling an "edit" affordance is cosmetic; this refusal is the enforcement. A
   request for ``seed/origin.md`` (Class C-view), the constitution, or a neocortex
   section structurally cannot reach a write — there is no Class-A target to match.
   (``structural_invariants_beat_discipline``: the guard is a refusal, not a contract
   we trust the caller to honor.)

2. **Govern at the input, never overwrite cognition** (scope §1). Everything writable
   here is an *input* to consolidation (operator facts, thinking-style, a name), never
   a *conclusion* the consolidate produced. The felt layer stays the AI's own.

3. **Every write is reversible + audited.** Before a write the prior file content is
   copied to ``.levain/backups/<edit-id>/<source>`` and an append-only record lands in
   ``.levain/edits.jsonl``; the file itself is written atomically (tmp + ``os.replace``
   — a crash mid-write never leaves a half-written file). ``apply_edit`` with
   ``kind="undo"`` restores an edit's backup.

4. **Untrusted inbound.** ``apply_edit`` receives a JSON dict off the network (even on
   localhost) and validates every field defensively — type, presence, bounds, and a
   per-section optimistic stale-check (the ``expected`` body the operator saw must
   still be the current body, else 409: no silent lost update).

The allowlist re-validation (point 1) fronts ``config`` edits — the only kind that
takes a caller-supplied path. ``entity_name`` targets the one fixed, confined
``.levain/config.json``; ``undo`` re-confines the audit-recorded source. So every
kind's write target is confined, by allowlist or by construction.

This module holds NO methodology and NO substrate logic of its own (scope §3) — it
locates a file, checks a class, replaces a span, and records the change. Single-writer:
every mutation runs under ``_WRITE_LOCK``, so the server's concurrent request threads
serialize through the read→stale-check→backup→audit→write critical section (no lost
updates, no torn audit lines). Not safe across separate processes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
import warnings
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast

from anneal_memory import (  # the SHARED cross-process continuity lock
    ContinuityLockUnavailable,
    continuity_lock,
)

from levain.spores import (  # the operator-I/O disposition vocabulary (anneal stays blind)
    LOOP_DISPOSITION,
    NON_COGNITION_DISPOSITIONS,
    VALID_DISPOSITIONS,
    disposition_of,
    is_loop,
    is_note,
    is_tray,
)

if TYPE_CHECKING:
    from levain.dashboard import AnnealPaths

__all__ = [
    "EditError",
    "WriteScope",
    "apply_edit",
    "recent_edits",
    "MAX_BODY_BYTES",
    "MAX_NAME_LEN",
]

# Upper bounds. A Class-A config edit is human-authored prose; cap it so a runaway
# request can't write an arbitrarily large file (the server caps the request body
# too — this is the data-layer backstop). The name is a short display label.
MAX_BODY_BYTES = 256 * 1024
MAX_NAME_LEN = 120
# A spore is a one-line-ish open loop, not a document; a Tray dump may be a short
# paragraph. Bound the captured text so a runaway request can't persist an arbitrarily
# large spore (the server caps the request body too — this is the data-layer backstop).
MAX_SPORE_TEXT_BYTES = 8 * 1024
# Strict YYYY-MM-DD shape — anchored \d so a space-padded "2026-06- 1" (which len==10 +
# datetime.strptime would wrongly accept) is rejected; a real-date check follows.
_ISO_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# Ledger artifacts live UNDER a WriteScope.ledger_root (a Levain install's `.levain/`,
# or any explicit dir — e.g. flow's `state/bridge/`). These names are ledger-RELATIVE;
# the `.levain/` location is just `WriteScope.from_install_root`'s choice of ledger_root.
_AUDIT_LOG_NAME = "edits.jsonl"
_BACKUPS_NAME = "backups"
# The stable, install-INDEPENDENT audit/backup label for a State edit. The target is
# `scope.anneal.continuity_md` (an explicit trusted path, never a request path), so the
# label is a logical id, not a filesystem-relative path. Undo recognizes a State edit by
# `kind == "state"`, not by this label — so the label is display + backup-filename only.
_CONTINUITY_SOURCE = "continuity"

# Serializes the whole read→stale-check→backup→audit→write critical section across
# the server's request threads (ThreadingHTTPServer runs writes concurrently). This
# is what MAKES "single-writer" true: without it, two edits to the same section both
# pass the optimistic stale-check and the last writer silently clobbers the first
# (and concurrent audit appends could tear a line → an unreachable backup). Writes
# are rare + human-driven, so a global lock costs nothing; with it, a second edit to
# a section just-changed by the first re-reads under the lock and gets a clean 409.
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class WriteScope:
    """WHERE + WHETHER a substrate is writable — the write-path peer of the read
    path's :class:`~levain.dashboard.SubstrateSource`. Decouples the governed write
    surface from the ``.levain/`` install convention so a non-install substrate (e.g.
    flow's own store — anneal at ``~/.anneal-memory``, spores in its repo, NO
    ``.levain/``) is governed through the SAME seam a Levain install uses.

    Three explicit fields, each the trusted source for one class of write:

    - ``anneal`` — the explicit store paths (``continuity_md`` for the State edit,
      ``spores_json`` for the Class-B spore verbs, ``episodic_db`` for the tombstone).
      These are TRUSTED (never request-supplied) → confined by construction.
    - ``ledger_root`` — the dir holding the append-only ``edits.jsonl`` audit trail +
      the ``backups/`` tree (reversibility for Class-A file edits).
    - ``install_root`` — OPTIONAL. Locates the Class-A *seed/config* surface
      (``seed/*`` / ``activation/*`` / ``.levain/config.json``) for ``config`` /
      ``entity_name`` edits, which are request-path-confined UNDER it. ``None`` (a
      non-install substrate) → those kinds are refused; State + the Class-B verbs do
      not need it.

    ``from_install_root`` reproduces the pre-WriteScope BEHAVIOR (store paths by the
    ``.levain/memory.db`` convention, ledger physical location at ``.levain/``, undo
    correctness), so the existing Levain web + TUI write surfaces are unchanged. One
    intentional non-identity [codex L3 MED]: a NEW install-scope audit record's ``backup``
    ref is ledger-relative (``backups/<id>/...``) vs the old install-relative
    (``.levain/backups/<id>/...``) — same physical file, and the undo shim reads both
    forms — so it is behavior-preserving, not byte-identical in the audit-log text."""

    anneal: AnnealPaths
    ledger_root: Path
    install_root: Path | None = None

    @classmethod
    def from_install_root(cls, install_root: str | Path) -> "WriteScope":
        """The Levain-install scope: store paths derived from the
        ``<root>/.levain/memory.db`` convention, ledger + backups under
        ``<root>/.levain/``, seed/config edits confined under ``<root>``. Behavior-
        preserving vs pre-WriteScope (targets, ledger physical location, undo); the audit
        ``backup``-ref string is now ledger-relative — see the class docstring."""
        from levain.dashboard import AnnealPaths  # lazy: avoid the writes↔dashboard cycle

        root = Path(install_root)
        return cls(
            anneal=AnnealPaths.from_db(root / ".levain" / "memory.db"),
            ledger_root=root / ".levain",
            install_root=root,
        )


class EditError(Exception):
    """A write was refused. ``http_status`` is the code the server maps it to;
    ``code`` is a stable machine token the frontend can branch on."""

    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@contextmanager
def _governed_continuity_lock(path: Path) -> Iterator[bool]:
    """Take anneal's shared cross-process continuity lock with ``require=True`` —
    Levain's governed continuity writes FAIL CLOSED (spore-091 #2). On a lock-less
    filesystem / non-POSIX volume the lock can't serialize against the anneal
    consolidate; Levain has no 2PC / recovery oracle of its own (anneal's save
    does, so anneal stays best-effort), so editing State unserialized risks a
    silent lost update. We refuse with a clean 503 instead of degrading to the
    best-effort CAS. Maps :class:`ContinuityLockUnavailable` → ``EditError`` so the
    call sites stay simple."""
    try:
        with continuity_lock(path, require=True) as held:
            yield held
    except ContinuityLockUnavailable as exc:
        raise EditError(
            "lock_unavailable", 503,
            "the continuity file's cross-process lock is unavailable (a lock-less "
            "filesystem / non-POSIX volume) — refusing to edit consolidated-cognition "
            "State unserialized; edit by hand or retry on a POSIX-locking volume",
        ) from exc


# ---------------------------------------------------------------------------
# Class-A allowlist — the authoritative writable set, re-derived from the read
# layer so the write surface CANNOT drift from what the read layer classes A.
# ---------------------------------------------------------------------------

def _assert_class_a_target(install_root: Path, source: str, heading: str | None) -> None:
    """Refuse unless ``(source, heading)`` names a Class-A editable doc.

    Re-derives the config docs from the read layer (the one place edit-classes are
    assigned) and requires a doc with this exact ``source`` + ``heading`` whose
    ``edit_class`` is A. Class C-view docs (origin, constitution), Class-C neocortex
    sections, and any path not emitted by the read layer have no Class-A match → 403.

    NB this reads the seed files to derive the classes; ``_apply_config_edit`` then
    reads the target file again to do the replacement. The window between is benign:
    edit-class is a function of WHICH file/section, not its content, so a content
    change can't flip a target's class, and a section that vanishes between the reads
    surfaces as a clean ``section_not_found`` at replacement time."""
    from levain.dashboard import CLASS_A, _read_config_docs  # lazy: avoid import cycle

    docs = _read_config_docs(install_root)
    for d in docs:
        if d.source == source and d.heading == heading:
            if d.edit_class == CLASS_A:
                return
            raise EditError(
                "not_editable",
                403,
                f"{source!r} (section {heading!r}) is {d.edit_class}-class, not "
                "editable — the operator governs inputs, never the entity's cognition",
            )
    raise EditError(
        "not_editable",
        403,
        f"{source!r} (section {heading!r}) is not a Class-A editable target",
    )


def _resolve_inside(install_root: Path, source: str) -> Path:
    """Resolve ``source`` (a relative path) under ``install_root`` and refuse any
    result that escapes the install (``..``, absolute, or via a symlink). Belt-and-
    suspenders atop the allowlist: the allowlist already constrains ``source`` to a
    known relative path, but a write must never be able to climb out of the tree."""
    root = install_root.resolve()
    candidate = (root / source).resolve()
    if candidate != root and root not in candidate.parents:
        raise EditError("path_escape", 403, f"{source!r} escapes the install root")
    return candidate


# ---------------------------------------------------------------------------
# Markdown section surgery — the precise inverse of dashboard._split_sections.
# Operates on the RAW file text (never a parsed/stripped round-trip), replacing
# exactly one section's body span and preserving every other byte: the H1, the
# preamble, sibling sections, and order.
# ---------------------------------------------------------------------------

def _locate_section(lines: list[str], heading: str) -> int:
    """Index of the lone ``## <heading>`` line. Raises if absent (404-ish 422) or
    ambiguous (the same heading twice → refuse rather than guess which one).

    NB the read layer (``dashboard._split_sections`` → ``dict(...)``) silently keeps the
    LAST duplicate; the write deliberately diverges and REFUSES (a write that guessed
    which of two ``## State`` blocks to replace could clobber the wrong one). Refuse
    beats guess at a mutation boundary — the divergence is safe (it can only refuse,
    never escape)."""
    matches = [
        i for i, ln in enumerate(lines)
        if ln.startswith("## ") and ln[3:].strip() == heading
    ]
    if not matches:
        raise EditError("section_not_found", 422, f"no '## {heading}' section found")
    if len(matches) > 1:
        raise EditError(
            "section_ambiguous",
            409,
            f"'## {heading}' appears {len(matches)}× — ambiguous; edit by hand",
        )
    return matches[0]


def _section_end(lines: list[str], heading_idx: int) -> int:
    """Exclusive end index of the section body: the next ``## `` line, else EOF."""
    for j in range(heading_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            return j
    return len(lines)


def _current_section_body(raw: str, heading: str) -> str:
    """The section's current body, extracted the SAME way the read layer renders it —
    so the optimistic stale-check compares like-with-like against the body the operator
    saw and round-tripped as ``expected_body``.

    Uses ``str.splitlines()`` to match ``dashboard._split_sections`` EXACTLY. This is
    load-bearing, not cosmetic: ``splitlines()`` breaks on Unicode line separators
    (``\\u2028``/``\\u2029``/``\\x0b``/``\\x0c``/``\\x85``/…) that a plain ``split("\\n")``
    keeps literal, and ``read_text`` normalizes ``\\r\\n``/``\\r`` but NOT those — so a
    State body containing one (paste-prone, free-text section) would render one way and
    compare another, a permanent unclearable 409. Both layers split the same way →
    symmetry is structural, not disciplinary. [L1+L2 convergent MEDIUM]

    NB ``_replace_section`` deliberately keeps ``split("\\n")`` (byte-preserving span
    surgery) so an edit never silently normalizes a separator in a SIBLING section (the
    felt layer must stay byte-identical). The two functions intentionally use different
    line models for their different jobs: compare-as-rendered here, write-as-bytes
    there. They agree on heading LOCATION for all normal input; the lone residual is a
    pathological separator-adjacent-to-heading (``foo\\u2028## State``), where this
    finds a body but the surgery 422s — a refusal, never an escape."""
    lines = raw.splitlines()
    idx = _locate_section(lines, heading)
    end = _section_end(lines, idx)
    return "\n".join(lines[idx + 1 : end]).strip()


def _replace_section(raw: str, heading: str, new_body: str) -> str:
    """Return ``raw`` with the body of ``## <heading>`` replaced by ``new_body``,
    preserving the heading line and everything outside the section's body span.

    Frames the new body with one blank line on each side (so the heading and the next
    section stay separated — valid markdown). An empty new body collapses to a single
    blank line under the heading (a deliberately-cleared section)."""
    lines = raw.split("\n")
    idx = _locate_section(lines, heading)
    end = _section_end(lines, idx)
    content = new_body.strip("\n")
    new_lines = [""] if content == "" else ["", *content.split("\n"), ""]
    rebuilt = lines[: idx + 1] + new_lines + lines[end:]
    return "\n".join(rebuilt)


def _edit_one_section(raw: str, heading: str, expected_lf: str, new_lf: str) -> str:
    """The shared Class-A section-edit core: break-guard → stale-check → span-replace.
    Used by BOTH a ``world.md`` config section edit and the neocortex ``State`` edit,
    so the two write paths share exactly one section-surgery implementation. Both
    arguments are LF-normalized by the caller. Returns ``raw`` with only the section's
    body span replaced; raises ``EditError`` (section_break 422 / stale 409)."""
    # A section body must not itself contain a `## ` line — the read layer would
    # parse it as a NEW section, silently fragmenting the operator's one panel.
    # Split with splitlines() — the SAME boundary `dashboard._split_sections` re-parses
    # with — NOT split("\n"): a Unicode line separator ( /\x0c/\x85/…) keeps a
    # `## State` literal hidden from split("\n") but the read layer breaks on it and
    # parses a SECOND `## State`, injecting a duplicate section that bricks State
    # editing forever (section_ambiguous on every future edit). The guard must see what
    # the parser will. [codex L3 HIGH — confinement break the compare-side fix missed]
    if any(ln.startswith("## ") for ln in new_lf.splitlines()):
        raise EditError(
            "section_break", 422,
            "a section body can't contain a '## ' heading line — it would split "
            "the section into two",
        )
    # Stale-check the section body against what the operator saw (per-section
    # optimistic concurrency): if it changed underneath (a hand-edit, or — for the
    # continuity file — a harness wrap landed since load), refuse rather than clobber.
    current = _current_section_body(raw, heading)
    if current != expected_lf:
        raise EditError(
            "stale", 409,
            f"'## {heading}' changed since you loaded it — reload and re-edit",
        )
    return _replace_section(raw, heading, new_lf)


def _normalize_file_body(new_body: str) -> str:
    """Whole-file write: exactly one trailing newline (markdown convention), no other
    reshaping of the operator's content."""
    return new_body.rstrip("\n") + "\n"


def _to_lf(s: str) -> str:
    """Normalize any line endings to LF. The read layer + seed templates are LF; only
    an untrusted non-browser client could send \\r\\n (a textarea's .value is already
    LF), and a stray \\r in a written body would create mixed endings."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# Reversibility primitives — backup, audit, atomic write.
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: a uuid-suffixed tmp in the same dir,
    fsync, then ``os.replace`` (atomic on POSIX). A crash never leaves a partial
    file; the tmp is cleaned up on any failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        data = text.encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            # os.write may short-write (POSIX); loop until every byte lands so an
            # interrupted/ENOSPC write can't leave a truncated tmp that we then
            # fsync + replace into place. [L3 MED]
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        # fsync the containing dir so the rename itself is durable, not just the file
        # bytes (best-effort: some platforms/filesystems refuse a dir fsync). [L3 MED]
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _backup(ledger_root: Path, source: str, prior_text: str | None, edit_id: str) -> str | None:
    """Copy ``prior_text`` to ``<ledger_root>/backups/<edit-id>/<source>`` (subdirs
    preserved) and return the backup path RELATIVE TO ``ledger_root``. ``None`` prior
    (the file did not exist before this edit — e.g. first ``config.json``) backs up
    nothing and the audit records ``backup: null`` so undo knows to restore-to-absent."""
    if prior_text is None:
        return None
    rel = Path(_BACKUPS_NAME) / edit_id / source
    # Self-confine (L2 MED): the backup is the one mutating primitive that folds
    # `source` into its write path. Confine it structurally — physically unable to
    # escape — rather than trusting every caller to pre-validate source
    # (structural_invariants_beat_discipline). Confine to THIS edit's subdir, not just
    # the backups/ root: a `source` containing `..` would otherwise stay inside backups/
    # yet clobber a SIBLING edit's backup. (source is allowlist-constrained / a fixed
    # label today, so this is belt-and-suspenders — confine at the write anyway.) [L2 LOW]
    edit_dir = (ledger_root / _BACKUPS_NAME / edit_id).resolve()
    bpath = (ledger_root / rel).resolve()
    if bpath != edit_dir and edit_dir not in bpath.parents:
        raise EditError("path_escape", 403, f"backup path for {source!r} escapes the edit's backup dir")
    bpath.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes (not write_text) → byte-exact backup; write_text would CRLF-
    # translate on Windows, so the backup wouldn't match the bytes we read.
    bpath.write_bytes(prior_text.encode("utf-8"))
    return str(rel)


def _append_audit(ledger_root: Path, record: dict[str, Any]) -> None:
    """Append one JSON record to the append-only ``<ledger_root>/edits.jsonl`` trail.
    Single-writer (the server serializes writes) so a plain append is sufficient."""
    p = ledger_root / _AUDIT_LOG_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _now_iso(now: str | None) -> str:
    return now if now is not None else datetime.now(timezone.utc).isoformat()


def recent_edits(ledger_root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """The most-recent audit records, newest first (for the dashboard's edit log /
    undo surface). Fail-soft: a missing log is ``[]``; a malformed line is skipped."""
    p = ledger_root / _AUDIT_LOG_NAME
    try:
        if not p.is_file():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# The public entry — apply_edit, routed by kind.
# ---------------------------------------------------------------------------

def _require_install_root(scope: WriteScope, kind_label: str) -> Path:
    """A ``config`` / ``entity_name`` edit targets the install's seed/config surface —
    which only exists when the scope has an ``install_root``. A non-install substrate
    (``install_root is None`` — e.g. flow's own store) refuses these cleanly; State +
    the Class-B verbs never reach here (they run off ``scope.anneal`` + the ledger)."""
    if scope.install_root is None:
        raise EditError(
            "no_install", 422,
            f"{kind_label} edits need a Levain install (the seed/config surface) — this "
            "substrate has none (it's governed through explicit store paths only)",
        )
    return scope.install_root


def apply_edit(scope: WriteScope, req: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    """Apply one governed edit described by ``req`` (an untrusted JSON dict) against
    ``scope`` (the explicit write surface). Returns a result dict on success; raises
    ``EditError`` (carrying an HTTP status) on refusal.

    Kinds: ``config`` (a world.md section or a whole posture/recency file) +
    ``entity_name`` (the .levain/config.json name) — Class-A seed/config, require a
    ``scope.install_root``; ``state`` (the neocortex ``State`` section, Class A, targets
    ``scope.anneal.continuity_md``); the Class-B verbs (``spore_touch`` /
    ``spore_descend`` / ``spore_ascend`` / ``episode_tombstone``, off ``scope.anneal``);
    the Slice-3b Tray operator-I/O kinds (``spore_seed`` capture / ``spore_set_disposition``
    re-route / ``spore_surface_at`` schedule, off ``scope.anneal`` — non-destructive);
    ``undo`` (restore a Class-A edit's backup)."""
    if not isinstance(req, dict):
        raise EditError("bad_request", 400, "request must be a JSON object")
    kind = req.get("kind")
    # Serialize the entire mutation (read→check→backup→audit→write) across the
    # server's request threads — this is what makes single-writer true (L1 HIGH).
    with _WRITE_LOCK:
        if kind == "config":
            return _apply_config_edit(scope, req, now)
        if kind == "state":
            return _apply_state_edit(scope, req, now)
        if kind == "entity_name":
            return _apply_entity_name(scope, req, now)
        if kind == "spore_touch":
            return _apply_spore_verb(scope, req, now, "touch")
        if kind == "spore_descend":
            return _apply_spore_verb(scope, req, now, "descend")
        if kind == "spore_ascend":
            return _apply_spore_verb(scope, req, now, "ascend")
        if kind == "spore_seed":
            return _apply_spore_seed(scope, req, now)
        if kind == "spore_set_disposition":
            return _apply_spore_set_disposition(scope, req, now)
        if kind == "spore_surface_at":
            return _apply_spore_surface_at(scope, req, now)
        if kind == "spore_update":
            return _apply_spore_update(scope, req, now)
        if kind == "episode_tombstone":
            return _apply_episode_tombstone(scope, req, now)
        if kind == "undo":
            return _apply_undo(scope, req, now)
    raise EditError("bad_kind", 400, f"unknown edit kind {kind!r}")


def _require_str(req: dict[str, Any], field: str) -> str:
    val = req.get(field)
    if not isinstance(val, str):
        raise EditError("bad_request", 400, f"{field!r} must be a string")
    return val


def _apply_config_edit(scope: WriteScope, req: dict[str, Any], now: str | None) -> dict[str, Any]:
    install_root = _require_install_root(scope, "config")
    source = _require_str(req, "source")
    heading = req.get("heading")
    if heading is not None and not isinstance(heading, str):
        raise EditError("bad_request", 400, "'heading' must be a string or null")
    expected = _require_str(req, "expected_body")
    new_body = _require_str(req, "new_body")
    if len(new_body.encode("utf-8")) > MAX_BODY_BYTES:
        raise EditError("too_large", 413, f"body exceeds {MAX_BODY_BYTES} bytes")

    # 1. Refuse anything that is not a Class-A target (the enforcement boundary).
    _assert_class_a_target(install_root, source, heading)
    # 2. Resolve + confine the path (defense-in-depth atop the allowlist).
    path = _resolve_inside(install_root, source)
    if not path.is_file():
        raise EditError("not_found", 404, f"{source!r} does not exist")

    # 3. Read the target once; this content is authoritative for both the stale-check
    #    and the replacement (no TOCTOU between checking and writing).
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise EditError("unreadable", 422, f"cannot read {source!r}: {exc}") from exc

    # Line endings: `path.read_text` (here AND in the dashboard read layer) does
    # universal-newline translation, so `raw` is always LF and we write LF — editing
    # normalizes a file to CONSISTENT LF (the seed templates ship LF; this never
    # produces mixed endings — verified empirically, NOT the diff-review's hand-built-
    # CRLF claim). Only the UNTRUSTED inputs can still carry \r\n (a non-browser
    # client like curl; a browser textarea's .value is already LF), so normalize them
    # to LF for the comparison + the write. CRLF-preservation, if ever needed, is a
    # read_bytes path — out of scope for 2a.
    exp = _to_lf(expected)
    new_lf = _to_lf(new_body)

    if heading is not None:
        # Section edit (world.md): the shared Class-A section core (break-guard,
        # stale-check, span-replace) — the same path the State write uses.
        new_text = _edit_one_section(raw, heading, exp, new_lf)
    else:
        # Whole-file edit (posture.md / recency_directives.md): stale-check the whole
        # file (both sides LF-normalized).
        if raw != exp:
            raise EditError(
                "stale", 409,
                f"{source!r} changed since you loaded it — reload and re-edit",
            )
        new_text = _normalize_file_body(new_lf)

    return _commit(
        scope, source=source, heading=heading, path=path,
        prior_text=raw, new_text=new_text, action="edit", kind="config", now=now,
    )


def _apply_state_edit(scope: WriteScope, req: dict[str, Any], now: str | None) -> dict[str, Any]:
    """Apply a Class-A edit to the neocortex **State** section — the ONE operator-
    editable section of the consolidated-cognition file. State is live-state
    (last-writer-wins; flow's own "quick update" treats it as a direct targeted edit,
    no consolidate needed) — an INPUT to cognition, never a conclusion the consolidate
    produced (scope §1). Confined two ways, by construction:

    1. **Target.** Always ``scope.anneal.continuity_md`` — the explicit, TRUSTED
       continuity path carried on the scope, NEVER a request-supplied path. A ``state``
       edit structurally cannot be REDIRECTED by the request: the target is taken from the
       scope, not parsed from ``req``, so no network ``req`` input can escape to another
       file — that is the confinement that matters here. NB this deliberately trades away
       one *incidental* defense the pre-WriteScope install-relative ``_resolve_inside`` had
       (it would have refused a ``.levain/memory.continuity.md`` symlinked OUTSIDE the
       install): the target is now an operator/installer-trusted store path — and for a
       non-install substrate the continuity legitimately lives outside any tree (flow's
       ``~/.anneal-memory/``) — so a store-owned symlink is the operator's choice, not a
       request-injection vector. For a Levain install this is
       ``<root>/.levain/memory.continuity.md``; for a non-install substrate (flow) it is
       wherever that substrate's anneal store lives.
    2. **Section.** The heading must be the lone Class-A section per the read layer's
       OWN rule (``_section_edit_class``). The five felt-layer sections (Patterns /
       Decisions / Context / Understanding / Active Threads) are Class C → refused 403
       here, server-side, no matter what the client claims. Re-deriving from the read
       rule (not a hardcoded name) means the writable set provably matches what the
       dashboard tags editable.

    **Arrow-of-time (the Slice-2b design frame).** A State write is temporal/causal,
    not declarative plumbing. Its reversibility net is the SAME hash-guarded undo every
    Class-A edit gets — and that guard is exactly right here: ``_apply_undo`` undoes a
    State edit ONLY while it is still the file's latest change, so once the harness
    wraps (rewrites the continuity file) the undo cleanly 409s instead of FORKING the
    thread (restoring an old State into a post-wrap file the linear history never held).
    Whole-consolidation time-travel/restore is a separate anneal-backed slice; this is
    the per-edit net, and it refuses to reverse across a consolidation boundary."""
    from levain.dashboard import (  # lazy: avoid the writes↔dashboard import cycle
        CLASS_A,
        STATE_HEADING,
        _section_edit_class,
    )

    heading = _require_str(req, "heading")
    if _section_edit_class(heading) != CLASS_A:
        raise EditError(
            "not_editable", 403,
            f"the {heading!r} section is consolidated cognition (Class C) — the "
            "operator governs inputs, never the entity's conclusions; only the "
            f"{STATE_HEADING!r} section is operator-editable",
        )
    expected = _require_str(req, "expected_body")
    new_body = _require_str(req, "new_body")
    if len(new_body.encode("utf-8")) > MAX_BODY_BYTES:
        raise EditError("too_large", 413, f"body exceeds {MAX_BODY_BYTES} bytes")

    # The target is the explicit continuity path from the scope (never the request);
    # `source` is a stable logical label (`_CONTINUITY_SOURCE`), not a filesystem path.
    source = _CONTINUITY_SOURCE
    path = scope.anneal.continuity_md
    if not path.is_file():
        raise EditError(
            "not_found", 404,
            "no neocortex continuity file yet — the entity hasn't wrapped a session, "
            f"so there is no {heading!r} to edit",
        )
    # AM-CONTLOCK: hold anneal's SHARED cross-process continuity lock across the
    # ENTIRE read→edit→CAS→os.replace. The continuity file is also written by the
    # anneal CONSOLIDATE in another process; `_WRITE_LOCK` (threading) only
    # serializes Levain's own request threads, so without this a wrap landing
    # between our read and our write is the lost-update codex L3 flagged in 2b-i.
    # `_commit`'s CAS still runs inside the lock as the no-op-degradation fallback
    # (and to catch a wrap that landed between the operator loading the page and
    # this read). Lock is INNER to `_WRITE_LOCK`; it is the only cross-process
    # lock, so the order can't deadlock. require=True (via _governed_continuity_lock)
    # → fail CLOSED (503) on a lock-less FS rather than edit cognition unserialized
    # [spore-091 #2].
    with _governed_continuity_lock(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
            raise EditError("unreadable", 422, f"cannot read the continuity file: {exc}") from exc

        new_text = _edit_one_section(raw, heading, _to_lf(expected), _to_lf(new_body))
        return _commit(
            scope, source=source, heading=heading, path=path,
            prior_text=raw, new_text=new_text, action="edit", kind="state", now=now,
        )


def _apply_entity_name(scope: WriteScope, req: dict[str, Any], now: str | None) -> dict[str, Any]:
    from levain.dashboard import LEVAIN_CONFIG_REL, _read_levain_config  # lazy

    install_root = _require_install_root(scope, "entity_name")
    value = _require_str(req, "value").strip()
    if len(value) > MAX_NAME_LEN:
        raise EditError("too_long", 422, f"name exceeds {MAX_NAME_LEN} chars")
    if any(ord(c) < 32 for c in value):  # no newlines/control chars in a display name
        raise EditError("bad_value", 422, "name may not contain control characters")

    expected = req.get("expected")  # the name the operator saw (str or null)
    if expected is not None and not isinstance(expected, str):
        raise EditError("bad_request", 400, "'expected' must be a string or null")

    config_path = install_root.joinpath(*LEVAIN_CONFIG_REL)
    prior_text = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    )
    current = _read_levain_config(install_root)
    current_name = current.get("entity_name")
    current_name = current_name if isinstance(current_name, str) else ""
    if expected is not None and current_name != expected:
        raise EditError(
            "stale", 409,
            "the entity name changed since you loaded it — reload and re-edit",
        )

    new_config = dict(current)
    if value:
        new_config["entity_name"] = value
    else:
        new_config.pop("entity_name", None)  # empty value clears the name
    new_text = json.dumps(new_config, indent=2, ensure_ascii=False) + "\n"
    source = str(Path(*LEVAIN_CONFIG_REL))

    return _commit(
        scope, source=source, heading=None, path=config_path,
        prior_text=prior_text, new_text=new_text,
        action="create" if prior_text is None else "edit",
        kind="entity_name", now=now,
    )


# ---------------------------------------------------------------------------
# Class B — lifecycle data (verb-mediated). The operator's INPUTS (open loops,
# raw episodes), mutated ONLY through anneal's validated verbs against a WRITABLE
# handle. No file backup is taken: a Class-B verb is not a file edit, and its
# reversibility is anneal's (a composted spore stays in the `resolved` set; a
# tombstoned episode keeps a tombstone row) — so `_apply_undo` REFUSES these.
# ---------------------------------------------------------------------------

# Verb-mediated kinds: recorded in the audit trail but NOT file-undoable (the
# mutation is canonical in anneal — the resolved set / a tombstone row / the live
# spore fields — so undo routes through anneal/the verb, never a file restore).
_VERB_KINDS = frozenset(
    {
        "spore_touch", "spore_descend", "spore_ascend", "episode_tombstone",
        # Slice 3b — the operator-I/O write kinds (capture + disposition + schedule +
        # the forming-workbench metadata edit: text / reclassify-type / re-tier):
        "spore_seed", "spore_set_disposition", "spore_surface_at", "spore_update",
    }
)


def _require_confirm(req: dict[str, Any], what: str) -> None:
    """A destructive verb must carry ``confirm: true``. The frontend renders the
    confirmation UI; THIS refusal is the enforcement — a client that omits it
    (curl, a script) is refused too (``structural_invariants_beat_discipline``)."""
    if req.get("confirm") is not True:
        raise EditError("confirm_required", 409, what)


def _audit_verb(
    ledger_root: Path,
    *,
    kind: str,
    action: str,
    target: str,
    now: str | None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Append a verb-mediated audit record AFTER the (anneal-atomic, canonical) verb
    has committed. ``source`` is a ``spore:<id>`` / ``episode:<id>`` label (the edit-log
    renders it); ``undoable: False`` marks it not-file-undoable (the frontend gates the
    undo button on this, and ``_apply_undo`` refuses it server-side regardless).

    **BEST-EFFORT by design [codex L3 HIGH].** Unlike the Class-A ``_commit`` path —
    which audits BEFORE the (reversible) file write so a crash can't leave a changed
    file with no record — a Class-B verb commits in anneal FIRST (its own atomic
    ``_transaction`` / ``_db_boundary``), and that committed state IS the canonical
    record (the resolved-spore set / the tombstone row). So a failure to append the
    SECONDARY Levain edit-log line must NOT collapse a committed, possibly destructive,
    op into a reported failure that makes the operator retry an already-done action. On
    an append failure we ``warn`` (surfaces in the server log) and return ``""`` — the
    op still succeeded, and the dashboard re-reads anneal's canonical state."""
    edit_id = uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "id": edit_id,
        "ts": _now_iso(now),
        "kind": kind,
        "action": action,
        "source": target,
        "heading": None,
        "undoable": False,
    }
    if extra:
        record.update(extra)
    try:
        _append_audit(ledger_root, record)
    except OSError as exc:
        warnings.warn(
            f"verb {kind} on {target} committed in anneal but its edit-log audit "
            f"append failed ({exc}); the mutation is canonical in anneal (resolved "
            f"set / tombstone row) — reporting success, edit-log line skipped",
            RuntimeWarning, stacklevel=2,
        )
        return ""
    return edit_id


def _apply_spore_verb(
    scope: WriteScope, req: dict[str, Any], now: str | None,
    verb: Literal["touch", "descend", "ascend"],
) -> dict[str, Any]:
    """A Class-B spore lifecycle verb: ``touch`` (engage — non-destructive),
    ``descend`` (compost downward) or ``ascend`` (transmute upward). The two
    resolving verbs are destructive (the loop leaves the open set) → confirm-gated
    and carry a ``kind`` anneal validates against the spore's type; ``ascend`` also
    requires a ``ref`` (what the loop became). anneal owns the validation + the
    atomic write (its own ``_transaction`` flock); this records the audit entry."""
    from anneal_memory.spores import SporeError, SporeStore  # lazy

    spore_id = _require_str(req, "spore_id")
    spore_kind: str | None = None
    ref: str | None = None
    if verb == "descend":
        spore_kind = _require_str(req, "spore_kind")
        _require_confirm(
            req,
            f"compost spore {spore_id} as '{spore_kind}'? this resolves the loop "
            "(it stays recoverable in anneal's resolved set)",
        )
    elif verb == "ascend":
        spore_kind = _require_str(req, "spore_kind")
        ref = _require_str(req, "ref").strip()
        if not ref:
            raise EditError("bad_verb_arg", 422, "ascend requires a non-empty ref")
        _require_confirm(
            req,
            f"promote spore {spore_id} → '{ref}' as '{spore_kind}'? this resolves "
            "the loop",
        )

    paths = scope.anneal
    if not paths.spores_json.is_file():
        raise EditError("not_found", 404, "no spore store yet — the entity has no open loops")
    store = SporeStore(paths.spores_json)
    if verb == "ascend":
        # A note is durable operator REFERENCE, not a prospective loop that BECAME memory —
        # ascending it would falsely claim reference material as the entity's own graduated
        # cognition (a pattern/essay/project lineage). Reference is REMOVED (descended), never
        # transmuted. Disposition-aware policy → enforced here; anneal stays blind. (descend
        # stays allowed — it's how the Keep "remove" works.) KNOWN RESIDUAL (complement L3,
        # LOW): the disposition read and `store.ascend` are separate transactions, so a
        # cross-process writer flipping a loop→note in that window can slip an ascend past this
        # guard. Sound single-process; closing it cross-process needs an `expect_disposition`
        # CAS on anneal's `ascend` (it only exists on `update` today) — a future anneal harden,
        # not grown here for a rare timing window.
        current = store.get(spore_id)
        if current is not None and is_note(current):
            raise EditError(
                "not_ascendable", 409,
                "a Keep note is durable reference, not a prospective loop — it can't be "
                "promoted/ascended; remove it (descend) instead",
            )
    try:
        if verb == "touch":
            store.touch(spore_id)
        elif verb == "descend":
            assert spore_kind is not None  # set in the descend validation branch above
            store.descend(spore_id, kind=spore_kind)
        else:  # ascend
            assert spore_kind is not None and ref is not None  # set in the ascend branch
            store.ascend(spore_id, kind=spore_kind, ref=ref)
    except ValueError as exc:  # bad kind for the spore's type (anneal arg validation)
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:  # unknown id / already resolved / store drift
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:  # raw IO from SporeStore._transaction (ENOLCK on a lock-less
        # FS, permission, fsync/replace) — not wrapped by anneal; map to a clean
        # retryable 503 instead of leaking a generic internal 500 [codex L3 MED].
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    extra: dict[str, Any] = {}
    if spore_kind is not None:
        extra["verb_kind"] = spore_kind
    if ref is not None:
        extra["ref"] = ref
    edit_id = _audit_verb(
        scope.ledger_root, kind=f"spore_{verb}", action=verb,
        target=f"spore:{spore_id}", now=now, extra=extra,
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}", "action": verb}


# --- Slice 3b: the Tray operator-I/O write kinds -----------------------------
# Capture (dump→seed), re-route (set_disposition), schedule (surface_at). The
# disposition is the Levain/flow Tray taxonomy (anneal stays blind — it persists the
# opaque tag; see ``levain.spores`` + the anneal ``add``/``update`` passthrough). All
# three are NON-destructive (a mis-capture is composted, a re-route/schedule is just
# re-applied) → no confirm gate; audit-recorded, NOT file-undoable (the live spore is
# canonical in anneal). The "human dumps, AI sorts" capture-UX: ``spore_seed`` is the
# human's freeform dump (always a Tray disposition); ``spore_set_disposition`` /
# ``spore_surface_at`` are the AI's triage verbs.


def _apply_spore_seed(
    scope: WriteScope, req: dict[str, Any], now: str | None
) -> dict[str, Any]:
    """Capture a freeform operator-I/O item (the in-GUI dump). Creates a NEW spore carrying
    an operator-I/O ``disposition`` — a Tray inbox item (default ``seed``) OR a Keep
    durable reference (``note``) — so it lands in the operator surface and is held OUT of
    the entity's cognition (``is_loop``=False) regardless of tier. Never a plain cognition
    loop: loops are born from a Tray item metabolizing, or the CLI. Creates the store if
    absent.

    ``type`` defaults to ``thought`` (the catch-all for an un-sorted dump — the freeform
    drop supplies no type), settable when known (the type dropdown / an AI capture). A Tray
    item is EXCEPTIONAL: while forming it is type-PLASTIC — ``spore_update`` can reclassify
    it (the lock binds at metabolize, not at drop). A ``note`` still CARRIES a type for
    storage uniformity but it is lifecycle-INERT — a note never ascends (refused) and is
    removed via descend, so its type is never consulted for resolution."""
    from anneal_memory.spores import SporeError, SporeStore, VALID_TYPES  # lazy

    text = _require_str(req, "text").strip()
    if not text:
        raise EditError("bad_verb_arg", 422, "a capture needs non-empty text")
    if len(text.encode("utf-8")) > MAX_SPORE_TEXT_BYTES:
        raise EditError("too_large", 413,
                        f"spore text exceeds the {MAX_SPORE_TEXT_BYTES}-byte cap")
    stype = req.get("type", "thought")
    if stype not in VALID_TYPES:
        raise EditError(
            "bad_verb_arg", 422,
            f"type must be one of {list(VALID_TYPES)} (got {stype!r})",
        )
    disposition = req.get("disposition", "seed")
    if disposition not in NON_COGNITION_DISPOSITIONS:
        raise EditError(
            "bad_verb_arg", 422,
            f"a captured item's disposition must be operator I/O "
            f"({list(NON_COGNITION_DISPOSITIONS)}; got {disposition!r}) — a Tray seed or a "
            f"Keep note, never a plain cognition loop (those come from metabolize or the CLI)",
        )

    store = SporeStore(scope.anneal.spores_json)
    try:
        # No explicit tier → anneal's default (`warm`). Harmless: an operator-I/O disposition
        # is cognition-EXCLUDED (is_loop=False) regardless of tier, so tier has no
        # salience/germination effect until a Tray item metabolizes to a loop — at which
        # point `warm` is the right neutral default for a freshly-promoted loop.
        created = store.add(type=stype, text=text, disposition=disposition)
    except ValueError as exc:  # anneal arg validation (e.g. empty text)
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:  # corrupt/ambiguous store (symmetry with the sibling verbs)
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:  # raw IO from SporeStore._transaction → retryable 503
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    spore_id = created["id"]
    edit_id = _audit_verb(
        scope.ledger_root, kind="spore_seed", action="seed",
        target=f"spore:{spore_id}", now=now,
        extra={"disposition": disposition, "type": stype, "text": text[:120]},
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}",
            "action": "seed", "spore_id": spore_id}


def _apply_spore_set_disposition(
    scope: WriteScope, req: dict[str, Any], now: str | None
) -> dict[str, Any]:
    """Re-route an OPEN spore across the operator-I/O classes (the AI's triage verb).
    ``disposition`` ∈ loop / seed / handoff / agenda / note: ``loop`` METABOLIZES an
    operator-I/O item into a cognition loop (clears the tag → key-free); a Tray value moves
    it within / into the Tray inbox; ``note`` routes it to durable Keep reference.

    Two boundaries are guarded. (1) A Keep ``note`` is TERMINAL — re-routing it OUT (note→loop
    / note→Tray) is REFUSED (409), because it would defeat the note-ascend lock; a note is
    repurposed by ``descend`` + creating a fresh loop, never by mutation. (2) **Demoting a LIVE
    cognition loop into operator I/O** (current = loop, target ∈ NON_COGNITION — Tray OR note)
    removes it from salience / digest / Top of Mind with no other signal — the silent-
    cognition-loss the fail-open read-predicate exists to prevent — so it is confirm-gated, like
    ``descend``/``ascend``. The gate reads only CAS-/transaction-guarded fields (disposition +
    status), NOT tier (that was a TOCTOU). Everything else (loop→loop, Tray↔Tray, filing INTO
    Keep) is non-destructive → no confirm. The prior disposition is always audited so any move
    stays reconstructable."""
    from anneal_memory.spores import SporeError, SporeStore  # lazy

    spore_id = _require_str(req, "spore_id")
    disposition = req.get("disposition")
    if disposition not in VALID_DISPOSITIONS:
        raise EditError(
            "bad_verb_arg", 422,
            f"disposition must be one of {list(VALID_DISPOSITIONS)} (got {disposition!r})",
        )
    paths = scope.anneal
    if not paths.spores_json.is_file():
        raise EditError("not_found", 404, "no spore store yet — the entity has no open loops")
    store = SporeStore(paths.spores_json)

    # Read current state to (a) record the prior disposition in the audit, (b) gate the one
    # silent-loss direction, and (c) seed the optimistic CAS below. `is_loop` (the SAME
    # predicate cognition surfaces use) decides "currently in cognition" — NOT `== "loop"` —
    # so an unknown/typo'd disposition (which cognition renders as a loop, fail-open) is also
    # guarded, and the guard can't drift from the render boundary [codex L3 HIGH]. Absent
    # `status` reads as open (anneal treats it so), so a malformed-but-open record is gated.
    current = store.get(spore_id)
    # raw stored value (None ≡ a key-free loop) — disposition is an unmodeled SporeDict key
    prior_raw = cast("str | None", current.get("disposition")) if current else None
    prior = disposition_of(current) if current else LOOP_DISPOSITION  # normalized, for audit
    # A Keep note is a TERMINAL, lifecycle-inert durable-reference disposition (ascend REFUSED,
    # resolve-exempt). Re-routing it OUT (note→loop / note→Tray) DEFEATS that lock: note→loop
    # clears the tag below, and the now-"loop" item sails past the note-ascend guard in
    # `_apply_spore_verb` → a reference resolves into a false pattern/project lineage [codex L3
    # HIGH; kimi/complement concur]. To repurpose a note, `descend` composts THIS object and a
    # fresh loop is created (a NEW object — not "a reference that became memory"). Filing INTO
    # Keep (loop→note / Tray→note) still flows; only LEAVING the note state is closed.
    if current is not None and is_note(current) and disposition != "note":
        raise EditError(
            "note_terminal", 409,
            f"spore {spore_id} is a Keep note — durable reference is lifecycle-inert and "
            "cannot be re-routed into a loop or the Tray. descend it to remove, or edit in place.",
        )
    # Gate ANY demotion of a live cognition loop into operator I/O — the Tray inbox OR a Keep
    # note (NON_COGNITION, not just TRAY). loop→note is the same silent-cognition-loss and is
    # even STICKIER (a note is durable + resolve-exempt), so it must confirm too [L1+L2 L3].
    # loop→loop / I/O→anywhere stay un-gated (no loss, or a gain into cognition). The gate reads
    # ONLY CAS-guarded `disposition` + transaction-guarded `status` — NOT `tier`: a `tier`-based
    # exemption was a TOCTOU (the CAS at the write doesn't cover tier, so a concurrent re-tier
    # between this read and the write defeated the confirm — codex L3 reproduced `warm seed`).
    demoting_live_loop = (
        current is not None
        and current.get("status", "open") == "open"
        and is_loop(current)
        and disposition in NON_COGNITION_DISPOSITIONS
    )
    if demoting_live_loop:
        dest = "a Keep note" if disposition in ("note",) else "the Tray"
        _require_confirm(
            req,
            f"move loop {spore_id} into {dest}? it leaves the entity's active cognition "
            "(salience / digest / Top of Mind) until it's re-triaged",
        )

    # ``loop`` → clear the tag (metabolize to a key-free loop); a Tray value → set it.
    anneal_disposition = None if disposition == LOOP_DISPOSITION else disposition
    try:
        # CAS on the disposition we just read closes the read→write window against a
        # concurrent (cross-process) writer — flow's CLI on the same store [codex L3 HIGH].
        # If it changed since `get`, anneal raises and nothing is written; the operator
        # re-reads and the confirm decision is re-made on fresh state.
        store.update(spore_id, disposition=anneal_disposition, expect_disposition=prior_raw)
    except ValueError as exc:  # anneal arg validation
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:  # unknown id / already resolved / store drift / CAS mismatch
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    edit_id = _audit_verb(
        scope.ledger_root, kind="spore_set_disposition", action="set_disposition",
        target=f"spore:{spore_id}", now=now,
        extra={"disposition": disposition, "prior_disposition": prior},
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}",
            "action": "set_disposition"}


def _apply_spore_surface_at(
    scope: WriteScope, req: dict[str, Any], now: str | None
) -> dict[str, Any]:
    """Schedule WHEN a spore re-surfaces — the operator-input twin of a spore's ``next:``
    alarm: author now, time the surfacing for a future session-open. ``surface_at`` is
    ``YYYY-MM-DD`` (or ``null``/``''`` to clear). Reversible → non-destructive."""
    from anneal_memory.spores import SporeError, SporeStore  # lazy

    spore_id = _require_str(req, "spore_id")
    # A MISSING key must NOT silently clear the alarm — that's almost always a client bug
    # (typo'd/omitted field) destroying a scheduled surfacing with no signal [complement L3
    # LOW]. Clearing is explicit: pass null or "". Presence is required; value may be null.
    if "surface_at" not in req:
        raise EditError("bad_verb_arg", 422,
                        "surface_at is required (use null or '' to clear the alarm)")
    surface_at = req.get("surface_at")
    if surface_at not in (None, ""):
        # Strict YYYY-MM-DD: the anchored \d regex rejects a space-padded "2026-06- 1" that
        # len==10 + strptime would wrongly accept [codex L3 MED]; strptime then confirms a
        # REAL date (not 2026-13-45). The operator always sees the user-facing "surface_at"
        # message, never anneal's internal "next" field name.
        if not isinstance(surface_at, str) or not _ISO_DATE_RE.match(surface_at):
            raise EditError("bad_verb_arg", 422,
                            "surface_at must be YYYY-MM-DD (or null/'' to clear)")
        try:
            datetime.strptime(surface_at, "%Y-%m-%d")
        except ValueError:
            raise EditError("bad_verb_arg", 422,
                            "surface_at must be YYYY-MM-DD (or null/'' to clear)") from None
    paths = scope.anneal
    if not paths.spores_json.is_file():
        raise EditError("not_found", 404, "no spore store yet — the entity has no open loops")
    store = SporeStore(paths.spores_json)
    try:
        store.update(spore_id, next=surface_at or None)  # anneal re-validates the date
    except ValueError as exc:
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    edit_id = _audit_verb(
        scope.ledger_root, kind="spore_surface_at", action="surface_at",
        target=f"spore:{spore_id}", now=now, extra={"surface_at": surface_at or None},
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}",
            "action": "surface_at"}


def _apply_spore_update(
    scope: WriteScope, req: dict[str, Any], now: str | None
) -> dict[str, Any]:
    """A governed metadata edit of an OPEN spore — the operator's forming-workbench levers:
    edit ``text`` (refine in place), reclassify ``type`` (ONLY while forming — a Tray item;
    the type LOCKS once it metabolizes into a loop or is a Keep note), re-``tier`` (e.g.
    un-park a Keep loop → active). At least one of text/type/tier required; all
    non-destructive (reversible by re-edit) → no confirm.

    The reclassify gate is the immune-property-at-the-cognition-boundary: ``type`` is
    plastic while forming, locked once committed. Levain owns that policy (disposition-aware);
    anneal allows the retype mechanically. A CAS on the disposition guards the type gate's
    read→write window against a concurrent (cross-process) writer."""
    from anneal_memory.spores import (  # lazy
        VALID_TIERS,
        VALID_TYPES,
        SporeError,
        SporeStore,
    )

    spore_id = _require_str(req, "spore_id")
    has_text, has_type, has_tier = "text" in req, "type" in req, "tier" in req
    if not (has_text or has_type or has_tier):
        raise EditError("bad_verb_arg", 422,
                        "spore_update needs at least one of: text, type, tier")

    # Validate every provided field up-front (before touching the store).
    new_text: str | None = None
    if has_text:
        new_text = _require_str(req, "text").strip()
        if not new_text:
            raise EditError("bad_verb_arg", 422, "text cannot be cleared to empty")
        if len(new_text.encode("utf-8")) > MAX_SPORE_TEXT_BYTES:
            raise EditError("too_large", 413,
                            f"spore text exceeds the {MAX_SPORE_TEXT_BYTES}-byte cap")
    new_type = req.get("type") if has_type else None
    if has_type and new_type not in VALID_TYPES:
        raise EditError("bad_verb_arg", 422,
                        f"type must be one of {list(VALID_TYPES)} (got {new_type!r})")
    new_tier = req.get("tier") if has_tier else None
    if has_tier and new_tier not in VALID_TIERS:
        raise EditError("bad_verb_arg", 422,
                        f"tier must be one of {list(VALID_TIERS)} (got {new_tier!r})")

    paths = scope.anneal
    if not paths.spores_json.is_file():
        raise EditError("not_found", 404, "no spore store yet — the entity has no open loops")
    store = SporeStore(paths.spores_json)
    current = store.get(spore_id)
    prior_raw = cast("str | None", current.get("disposition")) if current else None

    # RECLASSIFY GATE — type is plastic ONLY while forming (a Tray item). A committed loop
    # or a Keep note has a LOCKED type; the immune property binds at the cognition boundary,
    # not at capture. (A missing id falls through to update's own SporeError, not 409.)
    if has_type and current is not None and not is_tray(current):
        raise EditError(
            "type_locked", 409,
            "type is only reclassifiable while an item is forming in the Tray; a committed "
            "loop / a Keep note has a locked type (dismiss + re-capture to change it)",
        )

    kwargs: dict[str, Any] = {}
    if has_text:
        kwargs["text"] = new_text
    if has_type:
        kwargs["type"] = new_type
    if has_tier:
        kwargs["tier"] = new_tier
    # CAS only when the type gate is in play — a text/tier-only edit is disposition-
    # independent, so guarding it on the disposition would spuriously reject under a benign
    # concurrent re-route. The reclassify, whose gate read the disposition, must be sound.
    if has_type:
        kwargs["expect_disposition"] = prior_raw
    try:
        store.update(spore_id, **kwargs)
    except ValueError as exc:  # anneal arg validation
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:  # unknown id / resolved / drift / CAS mismatch
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    fields = [f for f, h in (("text", has_text), ("type", has_type), ("tier", has_tier)) if h]
    extra: dict[str, Any] = {"fields": fields}
    if has_type:
        extra["type"] = new_type
    if has_tier:
        extra["tier"] = new_tier
    edit_id = _audit_verb(
        scope.ledger_root, kind="spore_update", action="update",
        target=f"spore:{spore_id}", now=now, extra=extra,
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}", "action": "update"}


def _apply_episode_tombstone(
    scope: WriteScope, req: dict[str, Any], now: str | None
) -> dict[str, Any]:
    """Tombstone (delete) a raw episode — a Class-B verb on the operator's own
    INPUT layer (principle #2: deleting data you fed in is not rewriting a
    conclusion; the consolidate re-derives without it, and anneal keeps a tombstone
    row). Destructive → confirm-gated. Opens the episodic Store WRITABLE (the
    governed writable handle; the dashboard read path opens it read_only)."""
    from anneal_memory import AnnealMemoryError, Store  # lazy

    episode_id = _require_str(req, "episode_id")
    _require_confirm(
        req,
        f"tombstone episode {episode_id}? its content is PERMANENTLY erased (only an "
        "audit tombstone row — id/timestamp/type/hash — remains); the consolidate "
        "re-derives without it",
    )

    paths = scope.anneal
    if not paths.episodic_db.is_file():
        raise EditError("not_found", 404, "no episodic store yet")
    # Cross-process safety [L1 H1 — intentional, documented]: a tombstone touches the
    # EPISODIC DB, NOT the continuity file — a DIFFERENT resource than spore-091's
    # `continuity_lock` guards, so it deliberately takes NO continuity lock (that would
    # serialize the wrong thing). The episodic Store is WAL-mode and every mutation runs
    # inside anneal's `_db_boundary`, which makes the DELETE + tombstone-insert
    # row-atomic AND wraps a concurrent-writer SQLite busy/locked error as
    # StoreDatabaseError (→ AnnealMemoryError → the clean 422 below) — never a torn
    # write. A wrap consolidating at the same instant reads a FROZEN episode snapshot
    # (anneal's wrap-token TOCTOU guard) and tolerates a vanished source row as a soft
    # citation-miss (a warning), not corruption. So this is correctly lock-free; the
    # asymmetry with the fail-CLOSED State write is intentional — different resource,
    # different serialization.
    try:
        with Store(str(paths.episodic_db), read_only=False) as store:
            existed = store.delete(episode_id)
    except AnnealMemoryError as exc:  # incl. StoreDatabaseError (busy/locked, wrapped)
        raise EditError("verb_failed", 422, f"tombstone failed: {exc}") from exc
    except OSError as exc:  # raw IO opening the db (permission, etc.) — not wrapped;
        # clean retryable 503 over a generic internal 500 [codex L3 MED].
        raise EditError("store_unavailable", 503, f"episodic store unavailable: {exc}") from exc
    if not existed:
        raise EditError("not_found", 404, f"no episode {episode_id!r} to tombstone")
    edit_id = _audit_verb(
        scope.ledger_root, kind="episode_tombstone", action="tombstone",
        target=f"episode:{episode_id}", now=now,
    )
    return {"ok": True, "id": edit_id, "source": f"episode:{episode_id}", "action": "tombstone"}


def _apply_undo(scope: WriteScope, req: dict[str, Any], now: str | None) -> dict[str, Any]:
    edit_id = _require_str(req, "edit_id")
    record = next(
        (r for r in recent_edits(scope.ledger_root, limit=10_000) if r.get("id") == edit_id),
        None,
    )
    if record is None:
        raise EditError("not_found", 404, f"no edit {edit_id!r} in the audit log")
    if record.get("action") == "undo":
        raise EditError("bad_request", 400, "cannot undo an undo")
    # Class-B verbs are verb-mediated, not file edits — they carry no backup and
    # their reversibility is anneal's, not this layer's. Refuse server-side
    # (the frontend also hides the undo button via `undoable: False`, but this is
    # the enforcement — a stale/hand-built undo request can't no-op its way through
    # the file-restore path against a `spore:`/`episode:` pseudo-source).
    if record.get("kind") in _VERB_KINDS or record.get("undoable") is False:
        raise EditError(
            "not_undoable", 400,
            "verb-mediated edits aren't file-undoable from the plane: a composted / "
            "promoted spore is recoverable from anneal's resolved set, but a tombstoned "
            "episode's content is PERMANENTLY erased (only an audit tombstone row "
            "remains) — there is nothing to restore",
        )

    source = record.get("source")
    if not isinstance(source, str):
        raise EditError("corrupt_record", 422, "audit record has no source")

    # Resolve the TARGET file being restored. A State edit targets the explicit
    # continuity path on the scope (its `source` is the logical `_CONTINUITY_SOURCE`
    # label, NOT a filesystem path); every other Class-A file edit (config / entity_name)
    # is request-path-confined under the install root. Recognize a State edit by `kind`
    # (not the source label) so undo finds the right file on a non-install substrate
    # (flow), where the continuity lives outside any install root.
    if record.get("kind") == "state":
        path = scope.anneal.continuity_md
        is_continuity = True
    else:
        if scope.install_root is None:
            raise EditError(
                "no_install", 422,
                "cannot undo this edit — it targets an install seed/config file, but "
                "this substrate has no install root",
            )
        path = _resolve_inside(scope.install_root, source)
        is_continuity = False

    # AM-CONTLOCK: a State undo has the SAME cross-process writer (the anneal
    # consolidate) as a State edit, so hold the shared lock across the whole
    # read→CAS→restore — a wrap landing between read_bytes and _atomic_write is the
    # lost-update codex flagged in 2b-i. (`_commit`'s CAS+lock close the same window on
    # the forward State-edit path.) spore-091 #2: the continuity case uses
    # _governed_continuity_lock (require=True) so undo of State FAILS CLOSED (503) on a
    # lock-less FS too — undo's CAS→write window (read_bytes → backup → audit →
    # _atomic_write) is WIDER than _commit's, so a degraded best-effort lock here would
    # be the least airtight path of all; refusing is correct. Other Class-A targets
    # (world.md / posture / config.json) are Levain-only — `_WRITE_LOCK` already
    # serializes them — so they take a nullcontext (no needless cross-process lock).
    undo_lock = _governed_continuity_lock(path) if is_continuity else nullcontext()

    with undo_lock:
        # [L3 HIGH] Only the edit whose result is STILL the file's current content can be
        # undone — i.e. the most-recent edit to that file. Otherwise a stale id would
        # restore an old backup and silently discard every newer edit to the same file.
        # Compare the live file's hash to the edit's recorded result; a mismatch (a newer
        # edit landed, or the file changed) is a clean 409. This also blocks a second undo
        # of the same edit (post-undo the file no longer matches the edit's result).
        current_bytes = path.read_bytes() if path.is_file() else None
        current_sha = hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None
        if current_sha != record.get("new_sha256"):
            raise EditError(
                "stale", 409,
                "only the most-recent edit to a file can be undone — it has changed since "
                "(a newer edit or a consolidation/wrap landed, or it was already undone)",
            )
        prior_text = current_bytes.decode("utf-8") if current_bytes is not None else None

        # Resolve the restore target BEFORE mutating, so a missing backup fails clean.
        backup_rel = record.get("backup")
        if backup_rel is None:
            restored = None  # the edit created the file → undo removes it
        else:
            rel_str = str(backup_rel)
            # The backup rel MUST name a file under the backups tree: new records store it
            # relative to `ledger_root` (`backups/<id>/...`); pre-WriteScope records stored
            # it relative to the install root (`.levain/backups/<id>/...`). Validate the
            # prefix structurally and reject anything else — a corrupt/forged record naming
            # e.g. the audit log itself (`edits.jsonl`) must not become a restore source.
            # `norm` folds a Windows-separator legacy rel for the check. [L1 LOW]
            norm = rel_str.replace("\\", "/")
            if norm.startswith(".levain/backups/"):
                # legacy: relative to the install root (only on a from_install_root scope,
                # where ledger_root == install_root/.levain). [back-compat shim]
                legacy_base = scope.install_root or scope.ledger_root.parent
                backup_path = _resolve_inside(legacy_base, rel_str)
                backups_root = (legacy_base / ".levain" / _BACKUPS_NAME).resolve()
            elif norm.startswith("backups/"):
                backup_path = _resolve_inside(scope.ledger_root, rel_str)
                backups_root = (scope.ledger_root / _BACKUPS_NAME).resolve()
            else:
                raise EditError(
                    "corrupt_record", 422,
                    f"backup {backup_rel!r} is not a backups-tree path",
                )
            # [codex L3 HIGH] A prefix check alone is BYPASSABLE: `backups/../edits.jsonl`
            # passes startswith() but `_resolve_inside` only confines to the BASE, not the
            # backups subdir — a `..` after the prefix escapes to a SIBLING ledger file
            # (e.g. the audit log itself) and would be restored as content. Confine the
            # RESOLVED path under the resolved backups root, not the string
            # (structural_invariants_beat_discipline: check the real location).
            if backup_path == backups_root or backups_root not in backup_path.parents:
                raise EditError(
                    "corrupt_record", 422,
                    f"backup {backup_rel!r} escapes the backups tree",
                )
            if not backup_path.is_file():
                raise EditError("backup_missing", 422, f"backup {backup_rel!r} is gone")
            restored = backup_path.read_bytes().decode("utf-8")

        # [L3 HIGH] Back up the current content + append the undo audit BEFORE mutating —
        # same ordering as _commit, so a crash mid-restore leaves a backup + a record, not
        # a changed file with no way back.
        edit_id_new = uuid.uuid4().hex[:12]
        audit = {
            "id": edit_id_new,
            "ts": _now_iso(now),
            "kind": "undo",
            "action": "undo",
            "source": source,
            "heading": record.get("heading"),
            "undid": edit_id,
            "backup": _backup(scope.ledger_root, source, prior_text, edit_id_new),
            "restored_to": backup_rel if backup_rel is not None else "<absent>",
        }
        _append_audit(scope.ledger_root, audit)

        if restored is None:
            if path.is_file():
                path.unlink()
        else:
            _atomic_write(path, restored)
        return {"ok": True, "id": edit_id_new, "undid": edit_id, "source": source}


def _commit(
    scope: WriteScope,
    *,
    source: str,
    heading: str | None,
    path: Path,
    prior_text: str | None,
    new_text: str,
    action: str,
    kind: str,
    now: str | None,
) -> dict[str, Any]:
    """Shared tail for every mutating edit: CAS-check the live file, mint an id, back
    up the prior content, append the audit record, then atomically write the new
    content. The backup + audit land BEFORE the write so a crash can't leave a changed
    file with no record of how to reverse it."""
    # Cross-WRITER compare-and-swap: re-read the live file (the SAME way the caller
    # read `prior_text` — read_text, universal-newline) and refuse if it changed since.
    # `_WRITE_LOCK` only serializes Levain's own threads; the continuity file is also
    # written by the harness CONSOLIDATE in ANOTHER process, so a wrap landing between
    # the caller's read and this write would otherwise be silently CLOBBERED by our
    # stale snapshot [codex L3 HIGH]. This CAS fails safe (refuse, no clobber, before
    # any backup/audit so a refusal leaves no trace) and the refusal happens here, just
    # before the mutation, shrinking the window to the atomic os.replace. For the seed
    # files (no external writer) the live content always equals `prior_text` under the
    # lock, so this never false-fires there.
    #
    # AM-CONTLOCK (the 2b-i residual, now CLOSED for the continuity path): the
    # continuity-targeting callers (`_apply_state_edit`, undo-of-State) hold anneal's
    # SHARED `continuity_lock` across their whole read→here→os.replace, so the
    # sub-replace race against the anneal save path is gone — anneal takes the same
    # lock around its Phase-3 rename. This CAS now stays as (a) the no-op-degradation
    # fallback on non-POSIX / lock-less filesystems where the flock can't serialize,
    # and (b) the catch for a wrap that landed before the caller acquired the lock
    # (the operator-loaded-then-wrapped window). The seed-file callers take no
    # continuity lock (Levain is their only writer); `_WRITE_LOCK` + this CAS suffice.
    try:
        live = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, ValueError):
        live = None  # unreadable now (e.g. a wrap mid-rename) → treat as drifted
    if live != prior_text:
        raise EditError(
            "stale", 409,
            f"{source!r} changed on disk since you loaded it (a newer edit or a "
            "consolidation/wrap landed) — reload and re-edit",
        )
    edit_id = uuid.uuid4().hex[:12]
    backup_rel = _backup(scope.ledger_root, source, prior_text, edit_id)
    record = {
        "id": edit_id,
        "ts": _now_iso(now),
        "kind": kind,
        "action": action,
        "source": source,
        "heading": heading,
        "backup": backup_rel,
        "prev_sha256": _sha256(prior_text) if prior_text is not None else None,
        "new_sha256": _sha256(new_text),
        "prev_len": len(prior_text) if prior_text is not None else 0,
        "new_len": len(new_text),
    }
    _append_audit(scope.ledger_root, record)
    _atomic_write(path, new_text)
    return {"ok": True, "id": edit_id, "source": source, "heading": heading}
