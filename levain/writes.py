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
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, cast

from anneal_memory import (  # the SHARED cross-process continuity lock
    ContinuityLockUnavailable,
    continuity_lock,
)

from levain.idempotency import StoreCorruptError  # the at-most-once dedup keystore's corrupt sentinel
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
    from levain.idempotency import IdempotencyStore
    from levain.jobs import JobRuntime

__all__ = [
    "EditError",
    "WriteScope",
    "ActionVerb",
    "apply_edit",
    "apply_action",
    "recent_edits",
    "MAX_BODY_BYTES",
    "MAX_NAME_LEN",
]

# Upper bounds. A Class-A config edit is human-authored prose; cap it so a runaway
# request can't write an arbitrarily large file (the server caps the request body
# too — this is the data-layer backstop). The name is a short display label.
MAX_BODY_BYTES = 256 * 1024
MAX_NAME_LEN = 120
# A client-supplied idempotency key is an opaque token (a UUID is 36 chars); bound it so a
# runaway request can't persist an arbitrarily large key into the dedup store.
MAX_IDEMPOTENCY_KEY_LEN = 200
# A spore is a one-line-ish open loop, not a document; a Tray dump may be a short
# paragraph. Bound the captured text so a runaway request can't persist an arbitrarily
# large spore (the server caps the request body too — this is the data-layer backstop).
MAX_SPORE_TEXT_BYTES = 8 * 1024
# A focus is a single-line "what I'm on now", rendered on the cockpit masthead — bound it
# so a runaway request can't persist a novel-length focus (the server body cap is the
# outer backstop). Measured AFTER whitespace-collapse, the shape `write_focus` stores.
MAX_FOCUS_TEXT_LEN = 500
# Provenance for a focus set: who authored it. A small allowlist — anything else (a
# spoofed HTTP value) falls back to the honest default for the only HTTP caller ("web").
_FOCUS_SOURCE_ALLOWLIST = frozenset({"web", "tui", "cli", "app"})
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

    Four explicit fields, each the trusted source for one class of write:

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
    - ``context_json`` — OPTIONAL. The operator's live-context contract
      (``{focus, focus_set_at, focus_source}``) — the WRITE-peer of the read path's
      ``SubstrateSource.context_json`` (the same path, mirrored read/write like
      ``anneal`` is). Locates the target for the Class-A ``focus`` edit (operator
      live-state, last-writer-wins — no lock/backup/undo, distinct from the
      consolidated-cognition State edit). ``None`` (a substrate with no live-context
      file, or a read-only source) → the ``focus`` kind is refused. TRUSTED (never
      request-supplied). NB in flow's N-of-1 case this points at the sensor-written
      SUPERSET (``state/context_state.json``), a foreign-multi-writer file — so the focus
      write is a lost-update race with the sensor drains (``os.replace`` prevents a torn
      read, but a concurrent whole-object writer can revert a just-set focus, which —
      unlike a resampled sensor key — does NOT self-heal). Accepted low-stakes; the full
      rationale + the proper-fix pointer live in ``dashboard.write_focus``'s CONCURRENCY note.

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
    context_json: Path | None = None

    @classmethod
    def from_install_root(cls, install_root: str | Path) -> "WriteScope":
        """The Levain-install scope: store paths derived from the
        ``<root>/.levain/memory.db`` convention, ledger + backups under
        ``<root>/.levain/``, seed/config edits confined under ``<root>``, the live
        operator-context at ``<root>/.levain/context.json`` (mirrors
        ``SubstrateSource.from_install_root``). Behavior-preserving vs pre-WriteScope
        (targets, ledger physical location, undo); the audit ``backup``-ref string is
        now ledger-relative — see the class docstring."""
        from levain.dashboard import AnnealPaths  # lazy: avoid the writes↔dashboard cycle

        root = Path(install_root)
        return cls(
            anneal=AnnealPaths.from_db(root / ".levain" / "memory.db"),
            ledger_root=root / ".levain",
            install_root=root,
            context_json=root / ".levain" / "context.json",
        )


class EditError(Exception):
    """A write was refused. ``http_status`` is the code the server maps it to;
    ``code`` is a stable machine token the frontend can branch on."""

    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ActionVerb:
    """One registered governed action verb for the ``POST /action`` seam — a downstream
    control plane (the flow Bridge) registers a ``name → ActionVerb`` so the kernel can
    GOVERN dispatch (auth → confirm → audit) while the handler does the domain work. The
    write-peer of the read-only ``extra_panels`` seam: the kernel stays domain-agnostic;
    the verb operates flow's N-of-1 channels (inbox / relay / consult), which never live
    in the kernel (``dogfood_discriminator``). Action verbs require a WRITABLE source —
    they are mutations, so the off-box write-token governance already covers ``/action``
    with no second auth path (``structural_invariants_beat_discipline``).

    - ``handler`` — ``(params: dict) -> dict``. The domain action (e.g. send an inbox
      message). Called ONLY after the kernel's auth + confirm gate. Returns a JSON-able
      result dict (a ``summary: str`` is recorded in the audit trail). Error signalling:
      raise an ``EditError`` for a TYPED pre-execution refusal (e.g. bad input → it picks
      its own 4xx status, and — by this contract, raised BEFORE any side effect — nothing is
      audited); raise ANY OTHER exception for an execution failure (the kernel audits the
      attempt + surfaces a clean 502, never a traceback). The handler must NOT return content
      that shouldn't be logged via ``summary``, and SHOULD bound its own I/O (it runs inside
      the server's concurrency gate; a long action must be async/job-based, not a blocking call).
    - ``confirm_required`` — when True (the safe default) the kernel REFUSES (409, NO
      execution, NO audit — nothing happened) a request lacking ``confirm: true``; the
      structural fat-finger / speak-FOR-not-AS guard for an outbound or destructive verb.
      NOTE this is a FAT-FINGER guard, NOT an at-most-once guard: the seam does NOT dedupe a
      replayed ``confirm: true`` request (a tailnet retry of a non-idempotent POST re-runs the
      handler). For ``send_inbox`` a replay is a harmless duplicate message.
    - ``idempotent`` — when True, the verb is IRREVERSIBLE / not replay-tolerant (relay /
      consult), so the kernel routes its dispatch through the at-most-once
      :class:`~levain.idempotency.IdempotencyStore`: the request MUST carry a client-supplied
      ``idempotency_key`` (a non-empty string), recorded with the request's content fingerprint
      and deduped within a window. A replayed ``idempotency_key`` returns the ORIGINAL response
      WITHOUT re-firing (``replayed: true``); a key reused for a DIFFERENT request is a 422
      collision. This is the same at-most-once discipline the vagus efferent gate uses
      (``vagus.efferent.pending``). The default (False) keeps the legacy replay-tolerant
      behavior — a handler that omits ``idempotent`` must itself be idempotent / replay-tolerant.

      HARD CONTRACT for an idempotent handler (the at-most-once guarantee rests on it — L1/L2
      flagged this is discipline, not yet a structural invariant): an ``EditError`` MUST be raised
      ONLY BEFORE any side effect (it triggers RELEASE → a same-key retry re-fires). The instant a
      side effect MIGHT have started, every failure path must raise a NON-``EditError`` (it
      triggers POISON → the key is never re-fired). A handler that fires its side effect and THEN
      raises ``EditError`` would release a key whose action already happened → a replay re-fires →
      a double-send. (``send_relay`` complies: all its ``EditError``s are input validation before
      the POST; a transport fault is a ``RelayError``.)
    - ``job`` — when True, the verb is I/O-BOUND (the flow Bridge's ``consult`` — 30s–3min,
      spawns reviewer subprocesses) and MUST NOT run inline in the request-gate slot (it would
      pin a request thread for minutes). The kernel routes a ``job`` verb through the async
      :mod:`levain.jobs` runtime: the propose creates a job + submits ``handler`` to a bounded
      executor + returns a HANDLE (``{job_id, status:"pending"}``) immediately; the handler runs
      OUT OF BAND in a worker thread; the operator polls ``GET /job.json?id=<job_id>`` for the
      terminal result. ``apply_action`` REQUIRES a ``job_runtime`` for a job verb (500 if absent —
      fail-closed). The confirm + idempotency + audit envelope still applies to the PROPOSE; a
      ``job`` verb is naturally ``idempotent`` too (the idempotency key keeps a retried propose
      from launching a DUPLICATE expensive job). The handler runs in the worker, so its input
      validation surfaces via poll as a ``failed`` job (the frontend validates required fields
      client-side before the propose — server-side validation is defense-in-depth).
    - ``label`` — a short human label for the audit trail + the operator affordance."""

    handler: Callable[[dict[str, Any]], dict[str, Any]]
    confirm_required: bool = True
    idempotent: bool = False
    job: bool = False
    label: str = ""


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
    ``scope.anneal.continuity_md``); ``focus`` (the operator's live-context focus, Class A,
    targets ``scope.context_json`` — live-state, last-writer-wins, no lock/backup/undo,
    distinct from the consolidated-cognition State edit); the Class-B verbs (``spore_touch`` /
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
        if kind == "focus":
            return _apply_focus_edit(scope, req, now)
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


def _action_record(edit_id: str, verb: str, label: str, now: str | None,
                   *, outcome: str, detail: str | None,
                   idempotency_key: str | None = None) -> dict[str, Any]:
    """The receipt-shaped audit record for a governed action — the 'Bridge write outcome'
    trace face of the DecisionInfluenceReceipt. ``actor: operator`` because a Bridge action
    is HUMAN-initiated (the operator clicked it; no autonomous trust ladder, unlike the
    vagus efferent gate); ``kind: action`` + ``outcome`` bind the record to what ACTUALLY
    happened (anti-spoofing). Same shape family as ``_audit_verb``'s record so the existing
    ``recent_edits`` reader renders it (``undoable: False`` — a sent message has no undo).

    ``idempotency_key`` (when the verb is idempotent) is RECORDED ON THE RECEIPT (spore-188):
    the trace carries the at-most-once key, so the receipt corpus shows which fire each retry
    deduped against. ``outcome="replay"`` marks a deduped retry that did NOT re-fire."""
    record: dict[str, Any] = {
        "id": edit_id,
        "ts": _now_iso(now),
        "kind": "action",
        "action": verb,
        "source": f"action:{verb}",
        "label": label,
        "actor": "operator",
        "outcome": outcome,
        "detail": detail,
        "heading": None,
        "undoable": False,
    }
    if idempotency_key is not None:
        record["idempotency_key"] = idempotency_key
    return record


def _best_effort_action_audit(ledger_root: Path, record: dict[str, Any]) -> None:
    """Append the action receipt, single-writer. BEST-EFFORT by design: the handler has
    already run by the time we audit (the side effect happened or definitively failed), so
    a failed ledger append must NOT change the reported outcome / make the operator retry an
    already-sent message — warn (surfaces in the server log) and move on. Mirrors the
    ``_audit_verb`` best-effort discipline, one layer out."""
    with _WRITE_LOCK:  # hold the lock only for the brief append, NOT the (possibly slow) handler
        try:
            _append_audit(ledger_root, record)
        except OSError as exc:
            warnings.warn(
                f"action {record.get('action')!r} ran ({record.get('outcome')}) but its "
                f"audit append failed ({exc}); the receipt line is skipped, the outcome stands",
                RuntimeWarning, stacklevel=2,
            )


def apply_action(scope: WriteScope, registry: dict[str, "ActionVerb"],
                 req: dict[str, Any], *, now: str | None = None,
                 job_runtime: "JobRuntime | None" = None) -> dict[str, Any]:
    """Dispatch ONE governed operator action described by ``req`` against a registered verb.
    The kernel GOVERNS (validate → confirm-gate → execute → audit-receipt); the verb's
    ``handler`` does the domain work. Returns a result dict; raises ``EditError`` (HTTP-
    mapped, so the server's existing ``except EditError`` handles it) on refusal or failure.

    ``req``: ``{"verb": str, "params": dict?, "confirm": bool?, "idempotency_key": str?}``.
    Unknown verb → 404; a ``confirm_required`` verb without ``confirm: true`` → 409 (NO execution,
    NO audit — nothing happened); a handler that raises → the attempt is AUDITED (outcome "error")
    then surfaced as 502 (the receipt binds to what actually happened — NO-THEATER; a rendered
    confirmation never stands in for a real outcome). The handler runs OUTSIDE the write lock
    (it may be slow / network-bound — a long action must not block substrate edits); only the
    audit append is serialized.

    IDEMPOTENT verbs (``spec.idempotent``): the dispatch routes through the at-most-once
    :class:`~levain.idempotency.IdempotencyStore` — a client ``idempotency_key`` is REQUIRED (400
    if missing); a REPLAY returns the original response without re-firing (``replayed: true``); a
    key reused for a different request → 422; a concurrent in-flight duplicate → 409. The key is
    reserved BEFORE the handler fires: a pre-side-effect ``EditError`` RELEASES it (a corrected
    retry re-attempts); an execution fault KEEPS it reserved (at-most-once — a replay must not
    re-fire an action that may have happened mid-fire); success FINALIZES it with the response.

    JOB verbs (``spec.job``): an I/O-BOUND verb (30s–3min) must NOT run inline (it would pin a
    request thread). It requires a ``job_runtime`` (:mod:`levain.jobs`); the propose creates a job +
    submits the handler to the runtime's bounded executor + returns a HANDLE
    (``{ok, job_id, status:"pending"}``) immediately, and the operator polls ``GET /job.json`` for
    the result. The confirm + idempotency envelope governs the PROPOSE: the idempotency record
    caches the HANDLE (not the eventual result), so a deduped retry replays the same ``job_id``
    WITHOUT launching a duplicate job; a full executor → 503 (pre-fire, releases the key); a missing
    ``job_runtime`` → 500 (fail-closed, releases the key). The worker writes the completion audit."""
    if not isinstance(req, dict):
        raise EditError("bad_request", 400, "request must be a JSON object")
    verb = req.get("verb")
    if not isinstance(verb, str) or verb not in registry:
        raise EditError("unknown_verb", 404, f"no such action verb: {verb!r}")
    spec = registry[verb]
    if spec.confirm_required and req.get("confirm") is not True:
        # NO execution, NO audit — the action did not happen; the operator must confirm.
        raise EditError("confirm_required", 409, f"action {verb!r} requires confirm:true")
    # params: distinguish OMITTED (→ {}) from PRESENT-but-malformed (→ 400). A mutation seam must
    # not silently coerce a malformed params to {} — for a future verb with optional/default
    # params that would turn malformed JSON into a valid default action [codex L3].
    if "params" not in req:
        params: dict[str, Any] = {}
    elif isinstance(req["params"], dict):
        params = req["params"]
    else:
        raise EditError("bad_request", 400, "'params' must be a JSON object")

    now_iso = _now_iso(now)   # one timestamp shared by the dedup store + the audit receipt
    # The at-most-once gate (irreversible verbs only). A replay short-circuits here WITHOUT
    # re-firing; a fresh claim returns the store + key so success/failure can finalize/release it.
    idem_store: "IdempotencyStore | None" = None
    idem_key: str | None = None
    if spec.idempotent:
        replay, idem_store, idem_key = _idempotency_claim(scope, verb, spec, params, req, now_iso)
        if replay is not None:
            return replay

    if spec.job:
        return _dispatch_job(scope, verb, spec, params, now, now_iso,
                             job_runtime, idem_store, idem_key)

    edit_id = uuid.uuid4().hex[:12]
    try:
        result = spec.handler(params)   # lock-free: may be slow / network-bound
    except EditError:
        # A TYPED refusal the handler chose (e.g. bad input → 400). By contract a handler raises
        # EditError BEFORE any side effect (input validation), so — like the confirm-required
        # refusal — nothing happened and there is nothing to audit. RELEASE the idempotency
        # reservation (nothing fired → a corrected retry with the same key may re-attempt), then
        # re-raise as-is; the handler picked its own status (a 4xx, distinct from a 502 fault).
        # Best-effort: a release fault (a corrupt store fails CLOSED) must not mask the handler's
        # EditError — the reservation stays in_flight, which 409s (safe; the operator re-composes).
        _best_effort_release(idem_store, idem_key)
        raise
    except Exception as exc:  # noqa: BLE001 — any OTHER handler fault → audit the attempt + 502
        # Execution fault — AMBIGUOUS (the side effect may have happened mid-fire). At-most-once:
        # POISON the reservation (mark_faulted, never re-firable, never expires) so a replay does
        # not re-fire. Audit + 502.
        _best_effort_action_audit(scope.ledger_root, _action_record(
            edit_id, verb, spec.label, now_iso, outcome="error", detail=type(exc).__name__,
            idempotency_key=idem_key))
        _best_effort_mark_faulted(idem_store, idem_key, now_iso)
        raise EditError("action_failed", 502,
                        f"action {verb!r} failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict):
        # The handler RAN (it returned something) → a side effect may have happened → also the
        # ambiguous case: POISON the reservation (do not release). Audit + 502.
        _best_effort_action_audit(scope.ledger_root, _action_record(
            edit_id, verb, spec.label, now_iso, outcome="error", detail="bad_handler_result",
            idempotency_key=idem_key))
        _best_effort_mark_faulted(idem_store, idem_key, now_iso)
        raise EditError("action_failed", 502, f"action {verb!r} returned a non-dict result")
    summary = result.get("summary")
    _best_effort_action_audit(scope.ledger_root, _action_record(
        edit_id, verb, spec.label, now_iso, outcome="ok",
        detail=summary if isinstance(summary, str) else None, idempotency_key=idem_key))
    response = {"ok": True, "id": edit_id, "verb": verb, "outcome": "ok", "result": result}
    if idem_store is not None and idem_key is not None:
        # Record the response so a within-window replay returns it verbatim (no re-fire). BEST-
        # EFFORT (mirrors _best_effort_action_audit, one layer out — L1 MED-1): the send already
        # SUCCEEDED, so a finalize write-fault must NOT turn a real 200 into a false 500 (which the
        # operator would read as "it failed" → recompose → a NEW key → DOUBLE-SEND). The kept
        # in_flight record still 409s a same-key replay (no re-fire); the operator gets the truthful
        # 200, and the cache simply isn't populated (a within-window replay 409s instead of 200).
        # `_now_iso(now)` stamps the REAL completion time (fresh wall-clock in prod; the fixed test
        # `now`) — created_at was the claim time, so the done record's completed_at is accurate
        # for a network-bound handler (L3 complement LOW-2).
        try:
            idem_store.finalize(idem_key, response, _now_iso(now))
        except (OSError, TypeError, ValueError, StoreCorruptError) as exc:
            warnings.warn(
                f"action {verb!r} succeeded but its idempotency finalize failed ({exc}); the "
                f"response stands — a same-key replay will 409 until the window prunes",
                RuntimeWarning, stacklevel=2,
            )
    return response


def _best_effort_mark_faulted(
    store: "IdempotencyStore | None", key: str | None, now_iso: str,
) -> None:
    """Poison an idempotency reservation after an execution fault — best-effort. If the mark write
    fails the record stays ``in_flight``, which ALSO 409s a same-key retry (no re-fire), so the
    at-most-once guarantee holds either way; the only loss is the clearer ``faulted`` retry message."""
    if store is None or key is None:
        return
    try:
        store.mark_faulted(key, now_iso)
    except (OSError, TypeError, ValueError, StoreCorruptError) as exc:
        warnings.warn(f"idempotency mark_faulted failed ({exc}); the reservation stays in_flight "
                      "(still refuses a re-fire)", RuntimeWarning, stacklevel=2)


def _best_effort_release(store: "IdempotencyStore | None", key: str | None) -> None:
    """Release an idempotency reservation after a PROVABLY pre-side-effect refusal (nothing fired —
    a corrected retry with the same key may re-attempt). Best-effort: a release fault (a corrupt
    store fails CLOSED) leaves the record ``in_flight``, which 409s a same-key retry — the safe
    direction (no re-fire), the operator re-composes. Used by both the inline-handler EditError path
    and the job-dispatch pre-fire refusals (no runtime / store fault / executor full)."""
    if store is None or key is None:
        return
    try:
        store.release(key)
    except (OSError, TypeError, ValueError, StoreCorruptError) as exc:
        warnings.warn(f"idempotency release failed ({exc}); the reservation stays in_flight",
                      RuntimeWarning, stacklevel=2)


def _dispatch_job(
    scope: WriteScope, verb: str, spec: "ActionVerb", params: dict[str, Any],
    now: str | None, now_iso: str, job_runtime: "JobRuntime | None",
    idem_store: "IdempotencyStore | None", idem_key: str | None,
) -> dict[str, Any]:
    """The async (``spec.job``) dispatch path of :func:`apply_action`. Create a job + submit the
    handler to the bounded runtime + return a HANDLE (``{ok, job_id, status:"pending"}``); the
    handler runs OUT OF BAND in a worker. Every pre-fire refusal (no runtime / a job-store fault at
    create / the executor full) RELEASES the idempotency key (nothing fired → a corrected retry may
    re-attempt). On accept the idempotency record caches the HANDLE, so a deduped retry replays the
    SAME ``job_id`` without launching a duplicate job. The propose audits ``outcome="queued"`` (NOT
    a success — NO-THEATER); the WORKER writes the terminal ``ok``/``error`` audit on completion."""
    if job_runtime is None:
        # Fail-closed: a job verb registered but the server built no job runtime. Pre-fire (no job)
        # → release the reservation so a fixed-config retry can re-attempt, then 500.
        _best_effort_release(idem_store, idem_key)
        raise EditError("job_unavailable", 500,
                        f"action {verb!r} is a job verb but the server has no job runtime")
    job_id = uuid.uuid4().hex

    def _on_finish(status: str, result: dict[str, Any] | None, error: str | None) -> None:
        # The REAL outcome audit, written by the worker when the job finishes (the propose only
        # audited "queued"). Best-effort, single-writer (the shared _WRITE_LOCK serializes it
        # against concurrent worker/request audits).
        _best_effort_action_audit(scope.ledger_root, _action_record(
            job_id, verb, spec.label, _now_iso(None),
            outcome=("ok" if status == "done" else "error"),
            detail=(result.get("summary") if status == "done" and isinstance(result, dict)
                    else error),
            idempotency_key=idem_key))

    try:
        accepted = job_runtime.submit(
            job_id, verb, lambda: spec.handler(params), _on_finish, now_iso)
    except Exception as exc:  # noqa: BLE001 — a job-store fault at create (corrupt / IO); pre-fire
        _best_effort_release(idem_store, idem_key)
        raise EditError("job_store_error", 500,
                        f"could not enqueue {verb!r}: {type(exc).__name__}") from exc
    if not accepted:
        # Back-pressure: the executor is full → NO job was created (pre-fire) → release + 503.
        _best_effort_release(idem_store, idem_key)
        raise EditError("busy", 503,
                        f"the {verb!r} job queue is full ({job_runtime.max_concurrent} running) — "
                        "retry shortly")
    _best_effort_action_audit(scope.ledger_root, _action_record(
        job_id, verb, spec.label, now_iso, outcome="queued", detail=None,
        idempotency_key=idem_key))
    response = {"ok": True, "job_id": job_id, "status": "pending", "verb": verb}
    if idem_store is not None and idem_key is not None:
        # Cache the HANDLE (not the eventual result) so a within-window deduped retry returns the
        # SAME job_id WITHOUT launching a duplicate job. Best-effort (mirrors the inline path): the
        # job already launched, so a finalize fault must not turn a real 200 into a false 500. The
        # kept in_flight record 409s a same-key replay (no duplicate job); the operator polls the
        # job_id they already received.
        try:
            idem_store.finalize(idem_key, response, _now_iso(now))
        except (OSError, TypeError, ValueError, StoreCorruptError) as exc:
            warnings.warn(
                f"job {verb!r} launched but its idempotency finalize failed ({exc}); the handle "
                f"stands — a same-key replay will 409 until the window prunes",
                RuntimeWarning, stacklevel=2)
    return response


def _idempotency_claim(
    scope: WriteScope, verb: str, spec: "ActionVerb", params: dict[str, Any],
    req: dict[str, Any], now_iso: str,
) -> tuple[dict[str, Any] | None, "IdempotencyStore | None", str | None]:
    """Run the at-most-once gate for an idempotent verb. Returns ``(replay_response, store, key)``:
    a non-None ``replay_response`` ⇒ RETURN it immediately (a deduped retry — no re-fire);
    otherwise ``(None, store, key)`` and the caller OWNS executing the action (then finalize on
    success / mark_faulted on an execution fault / release on a pre-fire refusal). Raises
    ``EditError`` for the terminal refusals: a missing/oversize key (400), a key reused for a
    different request (422), a concurrent in-flight duplicate (409), a prior FAULTED attempt
    (409 — ambiguous, poisoned), or an unreadable store/record (500 — fail closed, never re-fire)."""
    from levain.idempotency import IdempotencyStore, request_fingerprint

    key = req.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise EditError("bad_request", 400, f"action {verb!r} is idempotent — an "
                        "'idempotency_key' (a non-empty string) is required")
    key = key.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise EditError("bad_request", 400,
                        f"'idempotency_key' exceeds {MAX_IDEMPOTENCY_KEY_LEN} characters")
    store = IdempotencyStore(scope.ledger_root / "idempotency.json")
    try:
        fingerprint = request_fingerprint(verb, params)
    except ValueError as exc:
        # params that can't be canonically fingerprinted (non-string dict key / non-finite float —
        # L3 codex LOW). Unreachable via HTTP (JSON keys are strings); a clean 400 for the API path.
        raise EditError("bad_request", 400, f"params cannot be fingerprinted: {exc}") from exc
    outcome = store.claim_or_replay(key, fingerprint, now_iso)
    if outcome.kind == "fresh":
        return None, store, key
    if outcome.kind == "replay":
        # The original already fired + audited; this retry was deduped. Record a best-effort
        # replay line (honest telemetry that a dedup happened — NO re-fire) and return the
        # ORIGINAL response verbatim plus a ``replayed`` marker.
        prior = outcome.result or {}
        _best_effort_action_audit(scope.ledger_root, _action_record(
            str(prior.get("id", "")), verb, spec.label, now_iso,
            outcome="replay", detail=None, idempotency_key=key))
        return {**prior, "replayed": True}, None, None
    if outcome.kind == "in_flight":
        # A concurrent duplicate with this key is still executing — the first owns the fire.
        raise EditError("in_flight", 409,
                        f"a request with this idempotency_key is already executing — "
                        f"not re-firing {verb!r}")
    if outcome.kind == "faulted":
        # A prior attempt with this key faulted (it may have partially fired) → AMBIGUOUS, so the
        # key is poisoned (never re-fired). The operator re-composes (a fresh key) to retry IF they
        # confirm it did not go through.
        raise EditError("faulted", 409,
                        f"a prior attempt with this idempotency_key faulted (it may have partially "
                        f"fired) — not re-firing {verb!r}; re-compose to retry")
    if outcome.kind == "collision":
        raise EditError("idempotency_key_reuse", 422,
                        f"this idempotency_key was already used for a different {verb!r} request")
    # corrupt → fail closed (never re-fire an action whose record we cannot trust)
    raise EditError("idempotency_corrupt", 500,
                    f"the idempotency record for this key is unreadable — refusing to re-fire {verb!r}")


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


def _apply_focus_edit(scope: WriteScope, req: dict[str, Any], now: str | None) -> dict[str, Any]:
    """Set (or clear) the operator's live focus — the Class-A ``focus`` edit kind.

    Distinct from the ``state`` edit (consolidated-cognition, the consolidate's
    single-writer territory, lock + backup + undo): focus is OPERATOR LIVE-STATE
    (last-writer-wins, no deference-risk, no machine interpretation — the cleanest
    Class-A input), so it needs NEITHER the continuity lock NOR a backup/undo. The set
    IS the record (``write_focus`` stamps ``focus_set_at`` + ``focus_source``); a blank
    ``text`` (or an omitted one) CLEARS it (``dashboard._read_focus`` reads an empty
    focus as unset). Runs under the shared ``_WRITE_LOCK`` (serializes cockpit
    focus-writes against each other + sibling edits); ``write_focus`` is merge-preserving
    + atomic, so a FOREIGN sensor writer of the same superset file is handled by that
    file's established last-writer-wins contract, not serialized here.

    Refuses 422 ``no_focus_target`` when the scope carries no ``context_json`` — a
    read-only source, or a substrate with no live-context file, has no writable focus.
    (``now`` is unused — ``write_focus`` self-stamps a tz-aware time, as the CLI does.)"""
    ctx = scope.context_json
    if ctx is None:
        raise EditError(
            "no_focus_target", 422,
            "this substrate has no writable operator-context (focus); nothing to set",
        )
    text = req.get("text")
    if text is None:
        text = ""  # an omitted 'text' is an explicit clear (blank reads back as unset)
    if not isinstance(text, str):
        raise EditError("bad_focus", 400, "focus 'text' must be a string")
    # Cap the whitespace-collapsed length — the exact shape `write_focus` will store
    # (it collapses internal whitespace on write; measure the same thing).
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_FOCUS_TEXT_LEN:
        raise EditError("focus_too_long", 422, f"focus exceeds {MAX_FOCUS_TEXT_LEN} chars")
    # Provenance: the TUI passes source="tui"; the web is the only HTTP caller, so a
    # spoofed non-allowlisted source over HTTP falls back to the honest "web" (harmless —
    # it's the operator's own self-report either way).
    raw_source = req.get("source")
    source = raw_source if raw_source in _FOCUS_SOURCE_ALLOWLIST else "web"
    from levain.dashboard import write_focus  # lazy: avoid the writes↔dashboard import cycle

    write_focus(ctx, collapsed, source=source)
    return {"ok": True, "kind": "focus", "cleared": collapsed == ""}


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


def _normalize_expect_disposition(value: Any) -> str | None:
    """Map a client SNAPSHOT disposition (the rendered row's value) → anneal's RAW
    compare-and-set value. A plain loop RENDERS as ``LOOP_DISPOSITION`` ('loop') but is
    STORED key-absent (``None``), so 'loop'/null/'' → ``None``; an operator-I/O tag passes
    through. Mirrors ``anneal_disposition = None if d == LOOP_DISPOSITION else d`` in
    ``_apply_spore_set_disposition`` — one translation point so the wire→store mapping can't
    drift. The CALLER owns PRESENCE: it threads the CAS ONLY when the request carries a
    snapshot (``"expect_disposition" in req``); absent ⇒ no CAS (back-compat for callers that
    don't render-snapshot, e.g. the flow CLI, which is already single-flock-atomic).

    PRECONDITION (the loop sentinel is KEY-ABSENT, owned by THIS vocabulary layer — anneal
    stays blind to the taxonomy): every governed writer clears ``loop``→``None`` at write time
    (``_apply_spore_seed`` refuses it; ``_apply_spore_set_disposition`` maps it), so the store
    never holds a literal ``'loop'`` string. A raw-anneal-API caller that BYPASSES this layer and
    stores ``'loop'`` verbatim would make this mapping (``'loop'``→``None``) mismatch the raw
    stored ``'loop'`` → the CAS fails CLOSED (a false REJECT on a valid loop, the safe direction;
    never a false match). [L3 converged LOW: codex/complement/nemotron, spore-173.]"""
    if value in (None, "", LOOP_DISPOSITION):
        return None
    if not isinstance(value, str):
        raise EditError(
            "bad_verb_arg", 422,
            f"expect_disposition must be a string or null (got {value!r})",
        )
    return value


def _apply_spore_verb(
    scope: WriteScope, req: dict[str, Any], now: str | None,
    verb: Literal["touch", "descend", "ascend"],
) -> dict[str, Any]:
    """A Class-B spore lifecycle verb: ``touch`` (engage — non-destructive),
    ``descend`` (compost downward) or ``ascend`` (transmute upward). The two
    resolving verbs are destructive (the loop leaves the open set) → confirm-gated
    and carry a ``kind`` anneal validates against the spore's type; ``ascend`` also
    requires a ``ref`` (what the loop became). anneal owns the validation + the
    atomic write (its own ``_transaction`` flock); this records the audit entry.

    Both resolving verbs CAS the disposition against the operator's render-time SNAPSHOT
    (spore-173): ``ascend`` reads it server-side for its own ``is_note`` refusal gate (so a
    server-fresh read is correct — the gate asks "can this CURRENTLY ascend"); ``descend``
    has NO server gate (any disposition descends — it's how Keep "remove", Tray "dismiss",
    and loop "compost" all work), so its kind/FACE is chosen client-side off the rendered
    snapshot, and the CAS must verify THAT snapshot (an absent key ⇒ no CAS, back-compat)."""
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
    ascend_expect: str | None = None  # the raw disposition read for the ascend CAS (below)
    # The descend CAS uses the CLIENT render-time SNAPSHOT (spore-173) — validated + normalized up
    # front (a non-string raises bad_verb_arg BEFORE the store touch). Presence is the gate: an
    # absent key ⇒ no CAS (back-compat). Hoisted so the audit trail can record it too [L1 #5].
    descend_has_snapshot = verb == "descend" and "expect_disposition" in req
    descend_expect = (
        _normalize_expect_disposition(req["expect_disposition"]) if descend_has_snapshot else None
    )
    if verb == "ascend":
        # A note is durable operator REFERENCE, not a prospective loop that BECAME memory —
        # ascending it would falsely claim reference material as the entity's own graduated
        # cognition (a pattern/essay/project lineage). Reference is REMOVED (descended), never
        # transmuted. Disposition-aware policy → enforced here; anneal stays blind. (descend
        # stays allowed — it's how the Keep "remove" works.) The read-then-resolve TOCTOU is
        # CLOSED cross-process (was a documented complement-L3 residual; codex L3 HIGH
        # 2026-06-23): the disposition we read here is passed as `expect_disposition` to
        # `store.ascend`, which CAS-checks it INSIDE the resolve transaction — so a concurrent
        # writer flipping loop→note between this read and the resolve makes the ascend FAIL
        # CLOSED (SporeError → verb_failed) rather than transmute a now-note. The CAS mirrors
        # the one `update` already uses (anneal `AM-SPORE-CAS`).
        current = store.get(spore_id)
        if current is not None and is_note(current):
            raise EditError(
                "not_ascendable", 409,
                "a Keep note is durable reference, not a prospective loop — it can't be "
                "promoted/ascended; remove it (descend) instead",
            )
        ascend_expect = cast("str | None", current.get("disposition")) if current is not None else None
    try:
        if verb == "touch":
            store.touch(spore_id)
        elif verb == "descend":
            assert spore_kind is not None  # set in the descend validation branch above
            # CAS the operator's render-time SNAPSHOT disposition (the resolve FACE — Keep
            # "remove" vs Tray "dismiss" vs loop "compost" — was chosen against it client-side):
            # a cross-process re-route in the render→confirm→resolve window now FAILS CLOSED
            # rather than compost a now-live loop under a Keep-"remove" intent [codex L3 HIGH,
            # spore-173]. The snapshot must be the CLIENT's (a server-fresh read would already
            # reflect the re-route and pass vacuously). Absent key ⇒ no CAS (back-compat).
            descend_kwargs: dict[str, Any] = {"kind": spore_kind}
            if descend_has_snapshot:
                descend_kwargs["expect_disposition"] = descend_expect
            store.descend(spore_id, **descend_kwargs)
        else:  # ascend
            assert spore_kind is not None and ref is not None  # set in the ascend branch
            # expect_disposition = the (non-note) value we just read → fail-closed if a
            # concurrent writer flipped it to a note (or anything else) before this resolve.
            store.ascend(spore_id, kind=spore_kind, ref=ref, expect_disposition=ascend_expect)
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
    # Record the disposition that was RESOLVED — the forensic "which Keep note / Tray item / loop
    # did this remove" the sibling set_disposition audit already keeps [L1 #5]. descend: the client
    # snapshot (present when the surface render-snapshots); ascend: the server-read value. Normalize
    # None→LOOP_DISPOSITION for a readable trail (matches set_disposition's `prior_disposition`).
    if verb == "ascend":
        extra["prior_disposition"] = ascend_expect or LOOP_DISPOSITION
    elif descend_has_snapshot:
        extra["prior_disposition"] = descend_expect or LOOP_DISPOSITION
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
    """Re-route an OPEN spore across the operator-I/O classes (the AI's triage verb, AND the
    Keep-note promote). ``disposition`` ∈ loop / seed / handoff / agenda / note: ``loop``
    METABOLIZES an operator-I/O item into a cognition loop (clears the tag → key-free); a Tray
    value moves it within / into the Tray inbox; ``note`` routes it to durable Keep reference.
    An optional ``surface_at`` (``YYYY-MM-DD`` / null / '') rides the re-route atomically — the
    "remind me" tickler: promote a note to a Tray seed AND schedule its resurface in one write.

    The KEEP-NOTE PROMOTE lifecycle (2026-06-23): a Keep ``note`` CAN be re-routed OUT
    (note→loop / note→Tray) — the former note-terminal refusal is RELAXED (it was right for a
    true reference, wrong for a deferred someday/maybe that had no path out but delete+recreate).
    The note-ascend lock is preserved as a deliberate TWO-STEP, not lost: a note still can't
    ``ascend`` DIRECTLY (``_apply_spore_verb``'s guard), so a reference can't resolve into a false
    lineage in one move — you activate it to a loop first, then ascend the loop. Lineage is
    preserved (same spore; mutation, not re-create).

    ONE boundary is confirm-guarded: **demoting a LIVE cognition loop into operator I/O**
    (current = loop, target ∈ NON_COGNITION — Tray OR note) removes it from salience / digest /
    Top of Mind with no other signal — the silent-cognition-loss the fail-open read-predicate
    exists to prevent — so it is confirm-gated, like ``descend``/``ascend``. The gate reads only
    CAS-/transaction-guarded fields (disposition + status), NOT tier (that was a TOCTOU).
    Everything else — loop→loop, Tray↔Tray, filing INTO Keep, AND promoting a note OUT (a gain
    into cognition / a lateral move into the Tray) — is non-destructive → no confirm. The prior
    disposition is always audited so any move stays reconstructable."""
    from anneal_memory.spores import SporeError, SporeStore  # lazy

    spore_id = _require_str(req, "spore_id")
    disposition = req.get("disposition")
    if disposition not in VALID_DISPOSITIONS:
        raise EditError(
            "bad_verb_arg", 422,
            f"disposition must be one of {list(VALID_DISPOSITIONS)} (got {disposition!r})",
        )
    # Optional surface_at — the "remind me" tickler: promote a note to a Tray seed AND schedule
    # its resurface in ONE atomic op. Two sequential GUI commits is impossible — a successful
    # commit re-renders, destroying the form's DOM — so the schedule must ride the promote.
    # ABSENT key ⇒ leave `next` untouched (the plain reroutes don't reschedule); present ⇒ set it
    # (null/'' clears). Validated via the SAME helper as spore_surface_at (no drift).
    has_surface_at = "surface_at" in req
    surface_at = _validate_surface_at(req.get("surface_at")) if has_surface_at else None
    # A Keep note has NO temporal dimension — it does not resurface on a schedule (promoting it to
    # the Tray via "remind me" is what scheduling a note MEANS). germination_tier + surface_at_reached
    # both document "notes carry no surface_at"; enforce that on the RESULTING STATE, not just an
    # explicit set (codex L3 MED + complement L3 HIGH 2026-06-23):
    #   - explicit non-empty date + note destination → user error, refuse loud (point at the Tray);
    #   - ANY →note transition → force `next` = None, which CLEARS a date CARRIED IN from a demoted
    #     scheduled loop/seed. Without this, a dated item routed into a note kept its date, and the
    #     new promote-out edge (note→Tray) then hid it until that future date — vanishing from BOTH
    #     Keep and the Tray (silent loss), and note{past-date}→loop landed instantly `dormant`.
    # Pre-read decision (no `current` needed — the target disposition + request determine it):
    #   →note ⇒ write next=None · →non-note + surface_at present ⇒ write surface_at · else leave untouched.
    # The flow CLI twin (cmd_update) enforces the identical rule — cross-repo parity (L2 2026-06-23).
    if surface_at is not None and disposition == "note":
        raise EditError(
            "bad_verb_arg", 422,
            "a Keep note doesn't resurface on a schedule — promote it to the Tray "
            "(disposition 'seed'/'handoff'/'agenda') to set a reminder. Clearing surface_at on a "
            "note is allowed.",
        )
    if disposition == "note":
        set_next, next_value = True, None            # invariant: a note carries no `next` — clear any carried alarm
    elif has_surface_at:
        set_next, next_value = True, surface_at       # the "remind me" schedule (or an explicit clear)
    else:
        set_next, next_value = False, None            # absent ⇒ leave `next` untouched (plain reroute)
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
    # The KEEP-NOTE PROMOTE lifecycle (2026-06-23): a Keep note CAN now be re-routed OUT —
    # note→loop (activate) / note→Tray (promote) — the mirror of the Tray's seed→loop
    # metabolize. The former note-terminal guard here was the OVER-correction: terminal is right
    # for a true reference, WRONG for a deferred someday/maybe (which had no path out but
    # delete+recreate — friction + lost lineage). The note-ascend lock is NOT lost — it MOVES to
    # the deliberate two-step: a note still can't `ascend` DIRECTLY (`_apply_spore_verb`'s H2
    # guard STAYS), so promoting note→loop here does NOT let a reference ascend in one move — the
    # now-loop must be ascended as a SECOND explicit, audited verb. A pure reference note simply
    # never gets promoted (reference-vs-someday stays emergent). LINEAGE preserved (same spore id
    # + history; mutation, not re-create). Promotion is a GAIN into cognition (or a lateral move
    # into the Tray), never the silent-loss direction, so it needs no confirm — `demoting_live_loop`
    # below is False for a note (is_loop(note) is False); only DEMOTING a live loop confirms.
    #
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
    update_kwargs: dict[str, Any] = {
        "disposition": anneal_disposition, "expect_disposition": prior_raw,
    }
    if set_next:
        update_kwargs["next"] = next_value  # None ⇒ clear (incl. the →note force-clear); a date ⇒ set — SAME atomic write
    try:
        # CAS on the disposition we just read closes the read→write window against a
        # concurrent (cross-process) writer — flow's CLI on the same store [codex L3 HIGH].
        # If it changed since `get`, anneal raises and nothing is written; the operator
        # re-reads and the confirm decision is re-made on fresh state. The disposition + the
        # optional surface_at land in ONE store.update (one transaction, one CAS) — the promote
        # and its schedule never half-apply.
        store.update(spore_id, **update_kwargs)
    except ValueError as exc:  # anneal arg validation
        raise EditError("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:  # unknown id / already resolved / store drift / CAS mismatch
        raise EditError("verb_failed", 422, str(exc)) from exc
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from exc

    edit_id = _audit_verb(
        scope.ledger_root, kind="spore_set_disposition", action="set_disposition",
        target=f"spore:{spore_id}", now=now,
        extra={"disposition": disposition, "prior_disposition": prior,
               **({"surface_at": next_value} if set_next else {})},
    )
    return {"ok": True, "id": edit_id, "source": f"spore:{spore_id}",
            "action": "set_disposition"}


def _validate_surface_at(value: Any) -> str | None:
    """Validate a surface_at date value → the normalized result (a ``YYYY-MM-DD`` string, or
    ``None`` for a clear). Shared by ``spore_surface_at`` AND the optional schedule carried on a
    ``spore_set_disposition`` promote (the "remind me" tickler) so the date contract can't drift
    between them. Caller owns the key-PRESENCE policy (surface_at requires the key present;
    set_disposition treats an absent key as "leave next alone"); this validates only the VALUE.

    Strict YYYY-MM-DD: the anchored \\d regex rejects a space-padded "2026-06- 1" that len==10 +
    strptime would wrongly accept [codex L3 MED]; strptime then confirms a REAL date (not
    2026-13-45). The operator always sees the user-facing "surface_at" name, never anneal's
    internal "next" field."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise EditError("bad_verb_arg", 422,
                        "surface_at must be YYYY-MM-DD (or null/'' to clear)")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise EditError("bad_verb_arg", 422,
                        "surface_at must be YYYY-MM-DD (or null/'' to clear)") from None
    return value


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
    surface_at = _validate_surface_at(req.get("surface_at"))
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
    # CAS the disposition when a disposition-DEPENDENT decision is in play:
    #  - ``type`` (reclassify): the gate ("plastic only while forming") read the disposition
    #    SERVER-side just above, so CAS the server-read ``prior_raw`` — closes the server's own
    #    get→update window (the gate re-evaluates fresh, so the client snapshot isn't needed).
    #  - ``tier`` (park / un-park): the ROUTE (park a loop vs un-park a parked LOOP, as opposed
    #    to promoting a NOTE) is chosen client-side off the rendered SNAPSHOT; with no CAS,
    #    tier=warm lands on a now-NOTE and reports "reactivated" while it stays a note [codex MED
    #    + complement MED-1, spore-173]. CAS the CLIENT snapshot (a server-fresh read would pass
    #    vacuously). anneal.update already supports the param — no anneal change for this half.
    # A text-only edit is disposition-independent → no CAS (a benign concurrent re-route mustn't
    # spuriously reject a text refine). Absent snapshot on a tier edit ⇒ no CAS (back-compat).
    client_snapshot_present = "expect_disposition" in req
    client_snapshot = (
        _normalize_expect_disposition(req["expect_disposition"]) if client_snapshot_present else None
    )
    if has_type:
        kwargs["expect_disposition"] = prior_raw
        # Combined type+tier edit (no surface emits one — type-only reclassify vs tier-only park).
        # The type gate CASes the server-read prior_raw, which closes the read→write window for
        # BOTH fields; but a client tier-snapshot would otherwise be silently dropped, leaving the
        # tier route's render→submit window unguarded [L1/L2 #4]. If a snapshot was sent and
        # disagrees with the server read, a re-route already happened → fail closed, don't half-guard.
        if has_tier and client_snapshot_present and client_snapshot != prior_raw:
            raise EditError(
                "verb_failed", 422,
                f"disposition changed since read (expected {client_snapshot!r}, "
                f"found {prior_raw!r}); re-read the spore and retry.",
            )
    elif has_tier and client_snapshot_present:
        kwargs["expect_disposition"] = client_snapshot
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
