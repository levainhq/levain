"""END: the path that ends a session and RETAINS its work (CONTRACT §7.3-7.5, V1 V2 V6 V12 V14 V17).

``REVOKED -> FROZEN -> DEAD -> CREDENTIAL_PARKED -> RETAINED``. Ending a session never deletes its
work (plan §2f): the session tree is renamed, whole, into ``L/retained/<session>/tree/`` after its
manifest and metadata are durable, then neutralized in place (flags, ACLs, xattrs and setuid/setgid
stripped, owner root). One child is the exception (1105-10 endvf): macOS's per-user ``/var/folders`` dir, which SIP
pins in place, is COPIED to ``tree/.levain-var-folders`` (the bytes the manifest hashed), then emptied in place and
proven to hold only its empty skeleton (:data:`VAR_FOLDERS_SKELETON`). Any step that is not PROVEN moves the record to HELD with ``failed_step``; the
tree is never touched by a failure path, and a retry resumes idempotently.

Root acts only on alloc facts, its own intent journal, its own slot state file and a live check
(CONTRACT §7.1): every path here comes from :class:`EndFacts`, built from the alloc record, never from
the state record. Before anything is signalled, root's slot state file must show the slot leased to
THIS session at alloc's ``lease_seq``, else HELD ``failed_step:"slot-not-ours"`` with nothing signalled
(L2 M4). The state record's ``state`` / ``failed_step`` (the reaper's read, §7.7 step 3) only choose
which CAS edges :func:`end_session` sends; every proof (freeze, kill, no process left, for EVERY uid the
session holds) and every idempotent act runs on every call, so a forged state can make root send a
wrong edge (CONTROL refuses it), never skip a proof or signal another session's processes. The one
exception destroys nothing: a record already RETAINED makes END return at once, unproven, ONLY when root's
own retained object for the session is whole (:func:`retained_whole`); a RETAINED record without one is
not believed and END runs from its proofs as for REVOKED (codex r1 1), so a forged RETAINED cannot let
teardown SCRUB and release a slot whose work was never retained.

Locks, always in this order (§3.5 LOCK ORDER: pool -> slot -> session END lock): :func:`end_session` and
:func:`reconcile_session` hold the slot's ``L/slots/{e,v}<n>/lock`` (:func:`slot_lock`), then root's
per-session lock ``L/alloc/<session>.lock`` (:func:`end_lock`: a bounded wait of :data:`END_LOCK_WAIT_S`, then
``end-busy`` with the record unchanged; the reaper or helper retries). The slot lock is held across the slot
check, every proof and every act (codex r1 2): SCRUBBED takes the same lock FIRST and only TRIES the pool lock
after its kills, and prepare holds the pool lock and only TRIES slot locks, so a slot cannot be released and
re-leased between END's check and its signals and no holder ever waits slot -> pool. A CAS CONTROL does not take
is never a step failure (L2 M5): see :func:`send_cas`.

Root reads no bytes the session could not (L2 H2, codex r1 4): a file hard-linked to a path outside the
tree, and any inode a session uid does not own whatever its link count, is hashed (and, outside-linked,
copied) by a reader dropped to a session uid, and one that uid cannot read is recorded and never copied;
root never reads such an inode's metadata. ONE classification (:func:`classify`), computed by the manifest
step and stored beside it, is what the meta and tree steps read. Metadata, strip and chown address every
entry from its walk dirfd, never by a full path (L2 M2). The one session-tree file root opens itself is a login
session's vendor config, read at park for the account record (:func:`seed_from_tree`): only when it is regular, has one link and is
owned by the slot uid (so its bytes are the slot's own), opened with ``O_NOFOLLOW``, size-capped and parsed as
untrusted data.

Every root-only act is an injected seam with a real default: the freeze, kill and process proof
(the defaults here refuse, which lands HELD; the real ones are :func:`levain.slot.proc.end_seams`'s),
``chown``, the metadata reader and strip, the tree flush (``sync``), the session-uid reader, root's slot
state read, the clock, CONTROL's ``cas``. The manifest walk is real
code over dirfds with ``O_NOFOLLOW``.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Iterator, Mapping, Sequence

from levain.contracts import ContractError, canon
from levain.slot import Refused, stage, wall, write_all
from levain.slot.grammar import SESSION_RE
from levain.slot.ids import SlotId
from levain.slot.layout import RESERVE_BYTES
# The §7.3 states END sends; records owns the names (the full set and its legal edges).
from levain.slot.records import (_REASON_RE, CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED,
                                 parse_slot_state, read_record)
from levain.slot.root_intent import (INTENT_PHASES, TIMESTAMP_RE, Cas, Fault, Phase, PhaseFailed, Plan,
                                     RootIntent, atomic_write, begin, fsync_dir, intent_path, read_root_intent,
                                     reconcile, sweep_temps, temp_path, unlink_durable)

# --- values (CONTRACT §11) -------------------------------------------------------------------------

V1_INODES = 2_000_000
V2_SECONDS = 600.0
V5_FLOOR_BYTES = 8 << 30
V12_SECONDS = 10.0
V14_BYTES = 64 << 10
V17_MANIFEST_BYTES = 512 << 20
V17_META_BYTES = 64 << 20
# DECISION: the contract fixes no cap for copying a file hard-linked to a path outside the tree
# (plan §2h 8 asks only that it be known before any mutation). V4's SIGKILL-row tree bytes is the
# order of magnitude a session's own work is sized at; refused as the manifest budget past it.
LINK_COPY_BYTES = 2 << 30
# The manifest's depth cap (the head's 1021+19 ruling on the s5 design's Q5): a tree entry deeper than this many
# levels below the session root refuses ``manifest-depth-cap``, a budget refusal. The slot's code can build a chain
# of any depth; the cap bounds the walk's work and its in-memory stack, never the fds it holds (that is
# WALK_OPEN_DIRS, below the smallest default RLIMIT_NOFILE).
MANIFEST_DEPTH = 1024
# DECISION: the most directory fds one walk holds open at a time (its root's included). An ancestor past it is
# closed before the walk opens a child and reopened through the child's ``..`` when the walk climbs back, checked
# to be the same directory, so a deep chain costs reopens, not fds.
WALK_OPEN_DIRS = 32
# The most fds one walk has open at any instant: WALK_OPEN_DIRS plus one reserved for enumeration (listing a
# directory by its fd duplicates the fd for as long as the listing runs).
WALK_PEAK_FDS = WALK_OPEN_DIRS + 1
# DECISION: how long END keeps retrying a CAS CONTROL did not take (``locked``, CONTROL down) before it
# leaves the record for the next pass: V12's order, so one END never outlives a reaper pass by much.
CAS_RETRY_SECONDS = V12_SECONDS
CAS_BACKOFF_FIRST = 0.01
CAS_BACKOFF_MAX = 1.0
# DECISION: END waits for the slot lock on V23's order with a doubling backoff, as the helper's own flock
# waits do; past it, ``slot-locked`` is raised for the caller to retry.
SLOT_LOCK_WAIT_S = 10.0
SLOT_LOCK_FIRST_S = 0.02
SLOT_LOCK_MAX_S = 0.5
# DECISION: END's wait for the session END lock (a ``start`` holds it from before its clone through its ACTIVE
# CAS), on the same order and backoff; past it, ``end-busy``.
END_LOCK_WAIT_S = SLOT_LOCK_WAIT_S
# DECISION (1105-10 endfix2, H1): END's wait for START's capture writer to close a log (its drain after END's kill
# is a bounded number of reads, an fsync and a close), on the same order and backoff; past it ``capture-busy``, HELD
# at ``manifest`` (the retry resumes there).
CAPTURE_WAIT_S = SLOT_LOCK_WAIT_S

#: The slot-lock and END-lock descriptors this process holds (comp r2 L4). Every forked child (the session-uid
#: reader, macOS's signalling child) closes them before it runs anything, so a child that outlives END can never
#: keep either flock held; a closed duplicate leaves the parent's lock as it was.
_LOCK_FDS: set[int] = set()


def _close_lock_fds_in_child() -> None:
    for fd in tuple(_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _LOCK_FDS.clear()


os.register_at_fork(after_in_child=_close_lock_fds_in_child)

MANIFEST_FORMAT = "levain-end-manifest/1"
META_FORMAT = "levain-retention-meta/1"
OBJECT_FORMAT = "levain-retained-object/1"


#: The vendor credential files (plan §2e 11): vendor -> (dir under the session tree, file name), parked
#: in ``L/slots/e<n>/keep/`` under the same name, for a passthrough session of that vendor only.
CREDENTIALS: Mapping[str, tuple[tuple[bytes, ...], bytes]] = {
    "claude": ((b"config", b"claude"), b".credentials.json"),
    "codex": ((b"config", b"codex"), b"auth.json"),
}
CREDENTIAL_FILES = tuple(CREDENTIALS.values())
#: identseed (np-8645041e (a), signed by Phill 11-05): the per-vendor file in the session tree whose account
#: block a ``login`` session's END parks beside the credential, and that block's key. Claude Code 2.1.318's
#: ``auth status`` reads ``email`` / ``orgId`` / ``orgName`` from ``oauthAccount`` in
#: ``$CLAUDE_CONFIG_DIR/.claude.json`` (RETURN_c1p addendum, measured; the field mapping read from the binary's own
#: code by lane identseed), never from ``.credentials.json``, and every later session's ``.claude.json`` is a fresh
#: render without it.
IDENTITY_SEED_SOURCE: Mapping[str, tuple[tuple[bytes, ...], bytes, str]] = {
    "claude": ((b"config", b"claude"), b".claude.json", "oauthAccount"),
}
#: ``L/slots/e<n>/keep/<this>``: root's record of the parked block, root 0600, beside the credential.
IDENTITY_SEED_NAME = "identity-seed.json"
IDENTITY_SEED_FORMAT = "levain-identity-seed/1"
#: the WHITELIST: only these keys of the block are ever parked or seeded: exactly the three ``auth status`` turns into
#: ``email``, ``orgId`` and ``orgName``. Everything else in the block (or the file) is never copied.
IDENTITY_SEED_KEYS = ("emailAddress", "organizationName", "organizationUuid")
#: DECISION: the most of the slot's ``.claude.json`` END reads to find the block (a measured login file held 72 KB).
IDENTITY_SEED_SOURCE_MAX = 1 << 20
#: DECISION: a parked record is three short strings plus its frame; one past this is not root's record.
IDENTITY_SEED_MAX_BYTES = 4096
_SEED_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", re.ASCII)
_SEED_TMP = IDENTITY_SEED_NAME + ".tmp"
#: IG-1 (1027-18 RULINGS): ``L/retained/<s>/replaced/``, where END puts the credential a confirmed re-login replaces in
#: ``keep/``. It is inside the session's own retained directory, so discard, restore and purge carry it with the session.
REPLACED = "replaced"
#: IG-1 D1 (1027-19 RULINGS): ``L/retained/<s>/unconfirmed/``, where END puts a ``login`` session's NEW credential when
#: ``keep/`` is occupied and no ``confirm-identity yes`` names the session. Carried with the session as ``replaced/`` is.
UNCONFIRMED = "unconfirmed"
#: DECISION: a ``levain-baseline/1`` file is five short fields; one past this is not root's baseline.
BASELINE_MAX_BYTES = 4096
VAR_FOLDERS_NAME = b".levain-var-folders"     # CONTRACT §7.4: macOS /private/var/folders/<slot>
#: 1105-10 endvf (DESIGN_end_var_folders (f)): the skeleton dirhelper makes in a per-user var-folders dir. Measured
#: 2026-11-05 on macOS 15.7.7, SIP on (RETURN_vmsunlnk 060/061): the dir and each of these carry ``sunlnk`` +
#: ``com.apple.rootless`` from creation, and root could not clear the flag, rename, rmdir or ``rm -rf`` them; a plain
#: file a session wrote inside WAS unlinked. So END copies what it finds, empties the dir in place, and accepts only
#: these names, each an empty directory, as what is left. A dated observation of one OS version, not a law.
VAR_FOLDERS_SKELETON = frozenset({b"0", b"C", b"T", b"X"})
#: ``L/retained/<s>/vfcopy/``: the manifest step's copy of the var-folders dir (its dirs, and every regular file's
#: bytes as hashed for the manifest), renamed into ``tree/.levain-var-folders`` by the tree step.
VFCOPY = "vfcopy"
#: ``L/retained/<s>/vfkeys.jsonl``: the originals the copy holds, by (path, dev, ino), so the tree step unlinks only an
#: entry whose bytes were copied; a root working file removed by the ``commit`` step, as :data:`CLASSES` is.
VFKEYS = "vfkeys.jsonl"
#: The errnos of an unlink / rmdir under the var-folders dir that leave the entry for the skeleton proof to name
#: (``var-folders-residue``, HELD for a person): flagged (EPERM / EACCES), or a dir holding something not copied.
_VF_LEFT = frozenset({errno.EPERM, errno.EACCES, errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT})

#: The refusals that mean "past the manifest budget": HELD ``failed_step:"manifest_budget"``, whose
#: exit is END again with larger caps (§7.3 force-retire a).
BUDGET_REFUSALS = frozenset({"manifest-inode-cap", "manifest-time-cap", "manifest-byte-cap", "meta-byte-cap",
                             "link-copy-cap", "manifest-depth-cap"})

# --- 1105-17 heldbuild: which HELD reasons a redo can clear --------------------------------------------------------
# A HELD session is retried by the reaper under a backoff. A cause no redo can clear (a fact about the files or the
# install, read unchanged each time) is QUARANTINED once it has failed enough tries: the reaper stops retrying it,
# the slot stays leased and nothing is moved or deleted. Quarantine is DERIVED from the record (``held_reason`` +
# ``held_attempts``, :func:`quarantined`), never a stored state. Every refusal name END can raise is classed in one of
# the four sets below; tests/test_slot_end_held_classes.py fails when one is missing.
#: A person must act: the redo re-reads the same files or install state. ``no-credential:<tag>`` is matched by
#: :data:`PERMANENT_HELD_PREFIXES`.
PERMANENT_HELD = frozenset({
    "unowned-unreadable", "var-folders-residue", "var-folders-special", "var-folders-foreign", "var-folders-not-dir",
    "var-folders-mount", "tree-unknown-type", "tree-shared-special", "tree-mount",
    "retain-conflict", "retained-exists", "live-missing", "tree-missing", "credential-not-parked", "keep-occupied",
    "keep-return-conflict", "keep-shape", "replaced-occupied", "unconfirmed-occupied", "classes-missing",
    "slot-not-ours", "seam-unbuilt", "intent-malformed", "intent-unbuilt", "intent-pending",
    # identseed: only END's own record check of the block it just whitelisted (identity_seed_bytes); same bytes on redo
    "bad-identity-seed"})
PERMANENT_HELD_PREFIXES = ("no-credential:",)
#: A redo can pass: a race, a slow writer, a process that may still exit, an I/O fault. ``bad-root`` covers both a
#: wrong owner and an fsync / open errno, and ``kill-unproven`` a process that may yet exit: both stay transient
#: (the hourly cap bounds a misclassified one at 24 tries a day). ``var-folders-changed`` is held at ``manifest``
#: when the dir changed under the walk, and the redo re-derives the manifest and passes (test_slot_end_var_folders).
TRANSIENT_HELD = frozenset({"tree-changed", "capture-busy", "link-reader-failed", "link-reader-unset",
                            "kill-unproven", "freeze-unproven", "bad-root", "reserve-locked", "var-folders-changed"})
#: Raised with no record change and no backoff (the reaper's ``_end_one``), or before END's first act: never a
#: ``held_reason``.
NOT_AN_ATTEMPT = frozenset({"end-busy", "slot-locked", "not-ending", "alloc-malformed"})
#: The tries a PERMANENT cause gets before it is quarantined (the 0 / 60 / 180 s tries cover a racing writer or a
#: crashed redo); a BUDGET refusal gets one, since the same caps on the same tree refuse identically and each try can
#: hold the single reaper pass for up to :data:`V2_SECONDS`.
QUARANTINE_AFTER = 3
QUARANTINE_AFTER_BUDGET = 1


def quarantine_after(reason: object) -> int | None:
    """The ``held_attempts`` at which a HELD for ``reason`` is quarantined; ``None`` for a transient, unclassified or
    absent reason (retried at the backoff cap forever)."""
    if not isinstance(reason, str):
        return None
    if reason in BUDGET_REFUSALS:
        return QUARANTINE_AFTER_BUDGET
    if reason in PERMANENT_HELD or reason.startswith(PERMANENT_HELD_PREFIXES):
        return QUARANTINE_AFTER
    return None


def quarantined(fields: Mapping[str, object]) -> bool:
    """Whether a HELD record's ``fields`` (``held_reason``, ``held_attempts``) put it in quarantine. The one test the
    reaper and ``levain doctor`` both ask."""
    after = quarantine_after(fields.get("held_reason"))
    tries = fields.get("held_attempts")
    return after is not None and isinstance(tries, int) and not isinstance(tries, bool) and tries >= after


def held_reason_of(e: BaseException) -> str | None:
    """The refusal name behind a failed step, for the HELD CAS: the ``Refused`` itself or the one a failed phase
    wraps; ``None`` for anything else (an OSError, a fault) or a name the record would refuse."""
    cause = e.cause if isinstance(e, PhaseFailed) else e
    if isinstance(cause, Refused) and _REASON_RE.fullmatch(cause.name):
        return cause.name
    return None

#: ``L/retained/<s>/linkcopy/<dev>-<ino>``: a foreign inode's bytes as the session uid read them, staged by
#: the manifest step and linked into the tree by the tree step (L2 H2).
LINKCOPY = "linkcopy"
#: (size, digest) of an outside-linked file the session uid cannot read. DECISION: the manifest records it
#: as ``size:0, digest:""`` (a file's digest is never empty otherwise) and the tree holds an empty 0600
#: placeholder at its path: the entry is recorded, its bytes never copied, never root's.
UNREAD = (0, "")
#: ``L/retained/<s>/classes.jsonl``: the manifest step's ONE classification of the tree's inodes
#: (:func:`classify`), read back by the meta and tree steps (codex r1 4). DECISION: a root working file
#: beside the manifest, removed by the ``commit`` step, so the retained object stays §7.4's list.
CLASSES = "classes.jsonl"
#: A regular file some of whose links lie outside the tree: read by a session uid, copied, the copy placed.
OUTSIDE = "outside"
#: Any other inode a session uid does not own: its bytes read by a session uid only (hashed, never copied),
#: its metadata never read by root, whatever its link count. The one exception is START's own capture
#: (:data:`CAPTURE_LOGS`), which root hashes (:func:`start_capture_ok`).
UNOWNED = "unowned"
#: c7.15 D7: START's vendor capture, ``<S>/run/vendor.out`` and ``vendor.err`` (start.py's ``open_capture`` takes
#: these names from here, as END cannot import start). START makes them root : root 0700 / 0600 so no session uid can
#: read or forge them, which is also why the session-uid reader cannot hash them (1105-10 endfix, finding a).
CAPTURE_DIR = "run"
CAPTURE_LOGS = ("vendor.out", "vendor.err")
_CAPTURE_PATHS = frozenset(os.fsencode(f"{CAPTURE_DIR}/{n}") for n in CAPTURE_LOGS)
#: The session-uid reader's answer for a file its uid may not open (EACCES / EPERM), END's own name beside
#: stage's ``staging-missing`` (ENOENT) and ``staging-not-regular`` (ELOOP, not a regular file).
LINK_UNREADABLE = "link-unreadable"
#: The reader answers that mean "this uid cannot read it" and land :data:`UNREAD` (comp r1 2); every other
#: answer (stage's ``staging-open-failed`` for EMFILE, EIO, ENOMEM ... included) is ``link-reader-failed``.
UNREAD_ANSWERS = frozenset({"staging-missing", "staging-not-regular", LINK_UNREADABLE})
#: DECISION: an UNOWNED file is hashed, never copied, so the copy cap does not bound it; the V2 time budget does.
HASH_ONLY_CAP = (1 << 63) - 1

_CHUNK = 1 << 20
# a refill stops this far above the floor: extent and journal metadata are charged beyond the zeros written, and a
# free-space reading can lag the writes, so landing exactly on the floor would still tip the next pass into BRAKE
_REFILL_MARGIN = 4 << 20
_LINK_TMP = b".levain-link."
_O_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_O_ENTRY = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_O_SYMLINK = getattr(os, "O_SYMLINK", 0)       # macOS: open the link itself
_SETID = stat.S_ISUID | stat.S_ISGID


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _root_owned(st: os.stat_result) -> bool:
    return st.st_uid == 0


def _no_fault(point: str) -> None:
    return None


# --- caps ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    """The manifest budget (V1 inodes, V2 seconds, V17 manifest + meta bytes, the depth cap) and the
    outside-link copy cap. ``force-retire`` (a) is END again with a larger one. DECISION: ``scaled`` leaves
    ``depth`` as it is: depth is a shape of the tree, not an amount of work, so a scaled budget never makes
    an ordinary nested tree refuse."""

    inodes: int = V1_INODES
    seconds: float = V2_SECONDS
    manifest_bytes: int = V17_MANIFEST_BYTES
    meta_bytes: int = V17_META_BYTES
    link_copy_bytes: int = LINK_COPY_BYTES
    depth: int = MANIFEST_DEPTH

    def scaled(self, factor: float) -> "Caps":
        return Caps(int(self.inodes * factor), self.seconds * factor, int(self.manifest_bytes * factor),
                    int(self.meta_bytes * factor), int(self.link_copy_bytes * factor), self.depth)


# --- facts (from the alloc record only) ------------------------------------------------------------


@dataclass(frozen=True)
class EndFacts:
    """Everything END needs about a session, from its alloc record (CONTRACT §7.1) and the store root.
    ``var_folders``: macOS ``/private/var/folders/<slot>``, the session owner's path from alloc, ``None`` on
    Linux. ``lease_seq``: the lease root's slot state
    file must still show (§3.5 Pool). ``child_uid``: a verifier pair's child (§1, §5.6), frozen, killed and
    proven gone with the slot uid. ``vendor`` + ``auth``: a credential is parked on a passthrough session
    only. ``kind``: alloc's; only a ``login`` session can replace a parked credential (IG-1). The defaults fail
    closed: no ``lease_seq`` = ``slot-not-ours``, no ``auth`` = nothing parked, no ``kind`` = nothing replaced."""

    session: str
    slot: SlotId
    slot_uid: int
    slot_gid: int
    store: Path
    var_folders: Path | None = None
    lease_seq: int | None = None
    child_uid: int | None = None
    vendor: str | None = None
    auth: str | None = None
    kind: str | None = None

    @property
    def uids(self) -> tuple[int, ...]:
        """Every uid the session's processes run as (L2 H3)."""
        return (self.slot_uid,) if self.child_uid is None else (self.slot_uid, self.child_uid)

    @property
    def live(self) -> Path:
        return self.store / "live" / self.session

    @property
    def retained(self) -> Path:
        return self.store / "retained" / self.session

    @property
    def tree(self) -> Path:
        return self.retained / "tree"

    @property
    def alloc_dir(self) -> Path:
        return self.store / "alloc"

    @property
    def intent(self) -> Path:
        return intent_path(self.alloc_dir, self.session)

    @property
    def cloned_marker(self) -> Path:
        return cloned_path(self.store, self.session)

    @property
    def lock(self) -> Path:
        """DECISION: root's per-session END lock beside the journal (L2 M7); never unlinked."""
        return self.intent.with_name(f"{self.session}.lock")

    @property
    def keep(self) -> Path:
        return self.slot.slot_dir(self.store) / "keep"

    @property
    def replaced(self) -> Path:
        return self.retained / REPLACED

    @property
    def unconfirmed(self) -> Path:
        return self.retained / UNCONFIRMED

    @property
    def slot_state(self) -> Path:
        return self.slot.slot_dir(self.store) / "state"

    @property
    def slot_lock(self) -> Path:
        """The slot's flock, the one prepare, SCRUBBED and END take (§3.5)."""
        return self.slot.lock_path(self.store)

    @property
    def parks(self) -> bool:
        return self.slot.slot_class == "egress" and self.auth == "passthrough" and self.vendor in CREDENTIALS

    @property
    def credential(self) -> tuple[tuple[bytes, ...], bytes] | None:
        """The one credential file END parks (the session vendor's, on a passthrough session)."""
        return CREDENTIALS[self.vendor] if self.parks else None   # type: ignore[index]

    @property
    def parked_paths(self) -> frozenset[bytes]:
        c = self.credential
        return frozenset() if c is None else frozenset({b"/".join(c[0] + (c[1],))})

    @property
    def extra(self) -> dict[bytes, Path]:
        return {VAR_FOLDERS_NAME: self.var_folders} if self.var_folders is not None else {}


def _is_id(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def facts_from_alloc(alloc: Mapping[str, object], *, store: Path, var_folders: Path | None = None) -> EndFacts:
    """The END facts of a parsed ``levain-alloc/1`` record: ``session``, ``slot{class,index}``,
    ``slot_uid``, ``slot_gid``, ``lease_seq``, ``child_uid`` (none class), ``vendor``, ``auth``, ``kind``
    (absent = None). Anything else in it is not END's."""
    try:
        slot = alloc["slot"]
        facts = EndFacts(alloc["session"], SlotId(slot["class"], slot["index"]),  # type: ignore[index]
                         alloc["slot_uid"], alloc["slot_gid"], Path(store), var_folders,  # type: ignore[arg-type]
                         alloc["lease_seq"], alloc.get("child_uid"), alloc["vendor"],  # type: ignore[arg-type]
                         alloc["auth"], alloc.get("kind"))  # type: ignore[arg-type]
    except (KeyError, TypeError) as e:
        raise Refused("alloc-malformed", f"END facts: {e}") from None
    if not _is_id(facts.slot_uid) or not _is_id(facts.slot_gid):
        raise Refused("alloc-malformed", "slot_uid / slot_gid")
    if not isinstance(facts.lease_seq, int) or isinstance(facts.lease_seq, bool) or facts.lease_seq < 0:
        raise Refused("alloc-malformed", "lease_seq")
    if (facts.child_uid is not None) != (facts.slot.slot_class == "none") or \
            (facts.child_uid is not None and not _is_id(facts.child_uid)):
        raise Refused("alloc-malformed", "child_uid is a uid, present exactly for the none class")
    if not isinstance(facts.vendor, str) or not isinstance(facts.auth, str):
        raise Refused("alloc-malformed", "vendor / auth")
    if facts.kind is not None and not isinstance(facts.kind, str):
        raise Refused("alloc-malformed", "kind")
    facts.intent   # validates the session id
    return facts


# --- the walk --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One tree entry. ``dirfd`` + ``name`` address it without following a link (``dirfd`` is -1 for
    an ``extra`` root child, addressed by ``abspath``); both are valid only while the walk is at it.
    ``abspath`` is for messages; no metadata call uses it (L2 M2)."""

    path: bytes
    st: os.stat_result
    dirfd: int
    name: bytes
    abspath: bytes


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_dir_at(name: bytes, dirfd: int, st: os.stat_result) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=dirfd) if dirfd >= 0 else os.open(name, _DIR_FLAGS)
    if not _same(os.fstat(fd), st):
        os.close(fd)
        raise Refused("tree-changed", f"{name!r} changed under the walk")
    return fd


def _reopen_parent(child_fd: int, frame: list) -> None:
    """Reopen a closed ancestor ``frame`` through its child's ``..`` (never a link), checked to be the
    directory the walk opened before; anything else refuses ``tree-changed``."""
    fd = os.open(b"..", _DIR_FLAGS, dir_fd=child_fd)
    try:
        if not _same(os.fstat(fd), frame[7]):
            raise Refused("tree-changed", f"{frame[2]!r} changed under the walk")
    except BaseException:
        os.close(fd)
        raise
    frame[0] = fd


def walk(root: Path, extra: Mapping[bytes, Path] | None = None, *,
         depth_cap: int = MANIFEST_DEPTH, prune: Callable[[Entry], bool] | None = None) -> Iterator[Entry]:
    """Every entry under ``root`` (not ``root`` itself), depth first, a directory before its children,
    names in byte order; never follows a link (each directory opened ``O_NOFOLLOW`` from its parent's
    fd and checked against the ``lstat`` that named it). ``extra`` adds root children that live
    elsewhere (macOS ``/var/folders/<slot>``, walked as ``.levain-var-folders``, the name its copy takes in
    the retained tree); an absent one is skipped, a present one must be a real directory, and a name the tree
    already has is ``retain-conflict``. An entry on another filesystem than its directory (a mount inside
    the tree, L2 L12) refuses ``tree-mount``: root never walks, strips or chowns into one.

    An entry more than ``depth_cap`` levels below ``root`` (a root child is level 1) refuses
    ``manifest-depth-cap``. At most :data:`WALK_OPEN_DIRS` directory fds are held: the root's always, and
    the deepest ones; an ancestor is closed before a child would pass that, and reopened from its child's ``..``
    on the way back up (:func:`_reopen_parent`). The walk never has more than :data:`WALK_PEAK_FDS` open at
    once. Only the directory of the entry yielded is guaranteed open.

    ``prune``: called on a yielded directory AFTER the consumer's step returns and before the walk opens it;
    true = do not descend (the directory itself was yielded, nothing under it is opened or listed). The default
    descends everywhere. A plain call, never a ``send()``: callers iterate with ``for``."""
    extra = dict(extra or {})
    root_b = os.fsencode(root)
    root_fd = os.open(root_b, _DIR_FLAGS)
    # frame: [fd (-1 = closed), prefix, absprefix, names, i, extra, dev, the directory's stat]
    stack: list[list] = []
    low = 1   # the shallowest non-root frame that may still be open; every frame below it is closed
    try:
        names = [os.fsencode(n) for n in os.listdir(root_fd)]
        clash = set(names) & set(extra)
        if clash:
            raise Refused("retain-conflict", f"{sorted(clash)!r} already in the tree")
        live_extra = {}
        for k, p in extra.items():
            try:
                pst = os.lstat(p)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(pst.st_mode):
                raise Refused("var-folders-not-dir", str(p))
            live_extra[k] = (os.fsencode(p), pst)
        root_st = os.fstat(root_fd)
        stack.append([root_fd, b"", root_b, sorted(names + list(live_extra)), 0, live_extra, root_st.st_dev,
                      root_st])
        while stack:
            frame = stack[-1]
            fd, prefix, absprefix, names, i, fextra, dev, _dst = frame
            if i == len(names):
                stack.pop()
                if stack and stack[-1][0] < 0:
                    try:
                        _reopen_parent(fd, stack[-1])
                    finally:
                        os.close(fd)
                    low = len(stack) - 1
                else:
                    os.close(fd)
                continue
            frame[4] = i + 1
            name = names[i]
            path = prefix + name
            if len(stack) > depth_cap:
                raise Refused("manifest-depth-cap", f"{path[:200]!r}... is deeper than {depth_cap} levels")
            if name in fextra:
                absp, st = fextra[name]
                e = Entry(path, st, -1, name, absp)
                yield e
            else:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if st.st_dev != dev:
                    raise Refused("tree-mount", f"{path!r} is on another filesystem")
                absp = absprefix + b"/" + name
                e = Entry(path, st, fd, name, absp)
                yield e
                if not stat.S_ISDIR(st.st_mode):
                    continue
            if prune is not None and prune(e):
                continue
            # The open frames are the root's and stack[low:]; the child makes one more, so the shallowest goes
            # first (never the current frame: WALK_OPEN_DIRS is far above two).
            if 1 + len(stack) - low >= WALK_OPEN_DIRS:
                os.close(stack[low][0])
                stack[low][0] = -1
                low += 1
            child = _open_dir_at(absp, -1, st) if name in fextra else _open_dir_at(name, fd, st)
            try:
                cnames = sorted(os.fsencode(n) for n in os.listdir(child))   # the reserved fd: a dup of child
            except BaseException:
                os.close(child)
                raise
            stack.append([child, path + b"/", absp, cnames, 0, {}, st.st_dev, st])
    finally:
        for frame in stack:
            if frame[0] >= 0:
                try:
                    os.close(frame[0])
                except OSError:
                    pass


def entry_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    raise Refused("tree-unknown-type", f"mode {mode:o}")


def _entry_fd(e: Entry) -> int | None:
    """An fd on the entry itself, opened from its walk dirfd and never through a link: a directory, file
    or fifo ``O_NOFOLLOW`` (``O_NONBLOCK``, so a fifo cannot hang it); a macOS symlink ``O_SYMLINK``.
    ``None`` for an entry with no safe fd form here: a Linux symlink, any socket (open refuses one) and any
    device (an open as root could act on the device)."""
    m = e.st.st_mode
    if stat.S_ISDIR(m):
        flags = _DIR_FLAGS
    elif stat.S_ISREG(m) or stat.S_ISFIFO(m):
        flags = _O_ENTRY
    elif stat.S_ISLNK(m) and _O_SYMLINK:
        flags = os.O_RDONLY | _O_SYMLINK | os.O_CLOEXEC
    else:
        return None
    fd = os.open(e.name, flags, dir_fd=e.dirfd) if e.dirfd >= 0 else os.open(e.abspath, flags)
    if not _same(os.fstat(fd), e.st):
        os.close(fd)
        raise Refused("tree-changed", f"{e.path!r} changed under the walk")
    return fd


# --- the manifest (levain-end-manifest/1) ----------------------------------------------------------


@dataclass
class LinkCount:
    nlink: int
    seen: int
    size: int
    regular: bool = True


class _Budget:
    def __init__(self, caps: Caps, clock: Callable[[], float]) -> None:
        self.caps, self.clock, self.t0, self.entries = caps, clock, clock(), 0

    @property
    def deadline(self) -> float:
        return self.t0 + self.caps.seconds

    def tick(self) -> None:
        if self.clock() - self.t0 > self.caps.seconds:
            raise Refused("manifest-time-cap", f"hashing passed {self.caps.seconds:g} s")

    def entry(self) -> None:
        self.entries += 1
        if self.entries > self.caps.inodes:
            raise Refused("manifest-inode-cap", f"more than {self.caps.inodes} entries")
        self.tick()


def _hash_fd(fd: int, budget: _Budget, out: int | None = None) -> tuple[int, str]:
    """``fd``'s bytes hashed, and written to ``out`` as read when given: a copy is exactly the bytes hashed."""
    h, n = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        if out is not None:
            write_all(out, chunk)
        h.update(chunk)
        n += len(chunk)
        budget.tick()
    return n, "sha256:" + h.hexdigest()


def _hash_file(e: Entry, budget: _Budget, out: int | None = None) -> tuple[int, str]:
    fd = os.open(e.name, _O_ENTRY, dir_fd=e.dirfd)
    try:
        if not _same(os.fstat(fd), e.st):
            raise Refused("tree-changed", f"{e.path!r} changed under the walk")
        return _hash_fd(fd, budget, out)
    finally:
        os.close(fd)


def start_capture_ok(dir_st: os.stat_result, file_st: os.stat_result, root_uid: int) -> bool:
    """Whether a directory and a file in it have the shape START's ``open_capture`` gives ``<S>/run/`` and its logs:
    a directory root owns with no permission bit beyond the owner's (so no other uid can enter it, plant in it or
    rename within it), holding a regular file root owns, with ONE link and no bit beyond the owner's (no setid, no
    other reader). Anything else is not START's capture."""
    return (stat.S_ISDIR(dir_st.st_mode) and dir_st.st_uid == root_uid
            and stat.S_IMODE(dir_st.st_mode) & ~0o700 == 0
            and stat.S_ISREG(file_st.st_mode) and file_st.st_uid == root_uid and file_st.st_nlink == 1
            and stat.S_IMODE(file_st.st_mode) & ~0o700 == 0)


def _await_capture_closed(fd: int, path: bytes, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    """A shared flock on ``fd``, a START capture log, within :data:`CAPTURE_WAIT_S`, else ``capture-busy``.
    START's ``open_capture`` takes an exclusive flock on each log as it creates it and only closing the fd drops it,
    so getting this one proves START's writer closed: its drain (which runs outside the session lock and may still
    be reading what the killed vendor left in its pipes) has written, fsynced and closed, and no root path opens
    the log for writing again (it was made ``O_EXCL``). A START that died closed its fds with it. No session uid can
    open the log (root : root 0600 in a root 0700 directory), so none can hold or forge this lock."""
    deadline = None
    delay = SLOT_LOCK_FIRST_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            now = clock()
            deadline = now + CAPTURE_WAIT_S if deadline is None else deadline
            if now >= deadline:
                raise Refused("capture-busy", f"{path!r}: START's writer held it past {CAPTURE_WAIT_S:g} s") from None
            sleep(delay)
            delay = min(delay * 2, SLOT_LOCK_MAX_S)


def _hash_start_capture(e: Entry, budget: _Budget, root_uid: int, clock: Callable[[], float],
                        sleep: Callable[[float], None]) -> "tuple[int, str] | None":
    """START's capture log hashed by root, or ``None`` when ``e`` is not one (the caller's session-uid read then
    stands). Only ``<S>/run/vendor.out`` / ``vendor.err`` reached by the walk's own dirfd; the directory is checked on
    that fd BEFORE root opens a name in it (once it is root's and owner-only, nothing but root can change its
    entries), and the file on the fd root then hashes (the walk's inode, :func:`start_capture_ok`), never by a path,
    and only once START's writer has closed it (:func:`_await_capture_closed`). Its size is bounded by START's own
    per-log cap (``runtime_helper.VENDOR_LOG_CAP``), and its hash is charged to the manifest's time budget like
    every other file's, so a budget refusal here is the ordinary ``manifest_budget`` HELD."""
    if e.dirfd < 0 or e.path not in _CAPTURE_PATHS:
        return None
    if not start_capture_ok(os.fstat(e.dirfd), e.st, root_uid):
        return None
    fd = os.open(e.name, _O_ENTRY, dir_fd=e.dirfd)
    try:
        st = os.fstat(fd)
        if not _same(st, e.st):
            raise Refused("tree-changed", f"{e.path!r} changed under the walk")
        if not start_capture_ok(os.fstat(e.dirfd), st, root_uid):
            return None
        _await_capture_closed(fd, e.path, clock, sleep)
        return _hash_fd(fd, budget)
    finally:
        os.close(fd)


#: ``read_foreign(entry, budget) -> (size, digest)``: an outside-linked file's bytes, read as the session.
ForeignRead = Callable[[Entry, _Budget], "tuple[int, str]"]


def iter_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic, credentials: Collection[bytes] = (),
                  skip_credentials: bool = False, foreign: Collection[tuple[int, int]] = (),
                  read_foreign: ForeignRead | None = None, budget: _Budget | None = None) -> Iterator[dict]:
    """The manifest records of the tree, in walk order: ``{path_b64, type, mode, size, nlink, digest}``
    (``mode`` = the permission bits, ``stat.S_IMODE``; a file's digest is over its bytes, a link's over
    its ``readlink`` target bytes; a dir / fifo / socket / device: size 0, digest ""). The inode, time
    and depth caps refuse by name mid-walk.

    ``credentials``: the tree paths END parks (the session vendor's credential on a passthrough session,
    plan §2g 3); one still there refuses ``credential-not-parked`` (END parks first, so the manifest never
    holds it), and ``skip_credentials`` leaves it out instead, for a snapshot of a live tree. ``foreign``:
    the inodes some of whose links lie outside the tree; root never opens one, ``read_foreign`` (the
    session-uid reader) hashes it (L2 H2), and :data:`UNREAD` is what it records for one it cannot read.
    ``budget``: the caller's (END's manifest step, whose var-folders scan already ran on its deadline); else a new
    one from ``caps`` and ``clock``."""
    if budget is None:
        budget = _Budget(caps, clock)
    creds = frozenset(credentials)
    foreign = frozenset(foreign)
    for e in walk(root, extra, depth_cap=caps.depth):
        t = entry_type(e.st.st_mode)
        if e.path in creds and t != "dir":
            if skip_credentials:
                continue
            raise Refused("credential-not-parked", e.path.decode("utf-8", "replace"))
        budget.entry()
        size, digest = 0, ""
        if t == "file":
            if (e.st.st_dev, e.st.st_ino) in foreign:
                if read_foreign is None:
                    raise Refused("link-reader-unset", e.path.decode("utf-8", "replace"))
                size, digest = read_foreign(e, budget)
            else:
                size, digest = _hash_file(e, budget)
        elif t == "link":
            target = os.readlink(e.name, dir_fd=e.dirfd)
            size, digest = len(target), digest_bytes(target)
        # DECISION: a directory's st_nlink is filesystem bookkeeping (APFS counts every entry, ext4 only
        # subdirectories) and drops when the credential is parked out of it; recorded as 1, so the
        # retained tree's manifest equals one taken before the freeze.
        yield {"path_b64": _b64(e.path), "type": t, "mode": stat.S_IMODE(e.st.st_mode), "size": size,
               "nlink": 1 if t == "dir" else e.st.st_nlink, "digest": digest}


def tree_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic, credentials: Collection[bytes] = (),
                  skip_credentials: bool = False) -> list[dict]:
    """The whole manifest in memory (a snapshot read as the caller; END streams its own to disk)."""
    return list(iter_manifest(root, extra=extra, caps=caps, clock=clock, credentials=credentials,
                              skip_credentials=skip_credentials))


def _count_link(links: dict[tuple[int, int], LinkCount], e: Entry) -> None:
    """Count ``e`` if it is a non-directory with more than one link, of ANY type (comp r1 4: a Linux symlink
    or a macOS fifo can be hard-linked outside the tree too)."""
    if not stat.S_ISDIR(e.st.st_mode) and e.st.st_nlink > 1:
        lc = links.setdefault((e.st.st_dev, e.st.st_ino),
                              LinkCount(e.st.st_nlink, 0, e.st.st_size, stat.S_ISREG(e.st.st_mode)))
        lc.seen += 1


def _count_links(root: Path, extra: Mapping[bytes, Path] | None = None, *,
                 depth_cap: int = MANIFEST_DEPTH) -> dict[tuple[int, int], LinkCount]:
    links: dict[tuple[int, int], LinkCount] = {}
    for e in walk(root, extra, depth_cap=depth_cap):
        _count_link(links, e)
    return links


def foreign_links(root: Path, extra: Mapping[bytes, Path] | None = None, *,
                  depth_cap: int = MANIFEST_DEPTH) -> dict[tuple[int, int], LinkCount]:
    """The non-directory inodes, of any type, some of whose links lie outside ``root`` (+ ``extra``):
    ``nlink`` greater than the links the walk sees. Root never strips or chowns one (comp r1 4)."""
    return {k: lc for k, lc in _count_links(root, extra, depth_cap=depth_cap).items() if lc.seen < lc.nlink}


def outside_link_bytes(links: Mapping[tuple[int, int], LinkCount]) -> int:
    """Bytes END will copy: one copy per regular file some of whose links lie outside the tree, linked into
    each of its in-tree paths."""
    return sum(lc.size for lc in links.values() if lc.regular and lc.seen < lc.nlink)


def classify(root: Path, extra: Mapping[bytes, Path] | None, uids: Collection[int], *,
             depth_cap: int = MANIFEST_DEPTH) -> tuple[dict[tuple[int, int], str], dict[tuple[int, int], LinkCount]]:
    """ONE classification of the frozen tree's inodes, by one walk, before a byte is read (codex r1 4):
    :data:`OUTSIDE` for a regular file some of whose links lie outside the tree, :data:`UNOWNED` for any
    other inode whose owner is not one of ``uids`` (the session's), whatever its link count. Returns it with
    the outside-linked counts (for the copy cap). A NON-regular inode linked outside the tree refuses
    ``tree-shared-special`` (comp r1 4): it has no copy, and root never strips or chowns a shared inode."""
    uids = frozenset(uids)
    links: dict[tuple[int, int], LinkCount] = {}
    classes: dict[tuple[int, int], str] = {}
    for e in walk(root, extra, depth_cap=depth_cap):
        _count_link(links, e)
        if e.st.st_uid not in uids:
            classes[(e.st.st_dev, e.st.st_ino)] = UNOWNED
    outside = {k: lc for k, lc in links.items() if lc.seen < lc.nlink}
    for key, lc in outside.items():
        if not lc.regular:
            raise Refused("tree-shared-special", f"inode {key[0]}-{key[1]}: a non-regular inode linked outside "
                                                 f"the tree ({lc.seen} of {lc.nlink} links in it)")
        classes[key] = OUTSIDE
    return classes, outside


def _write_classes(path: Path, classes: Mapping[tuple[int, int], str], cap: int, depth: int) -> None:
    """DECISION: under V17's meta cap (a budget refusal, ``meta-byte-cap``): it is metadata about the tree. The
    first line is the depth cap the manifest walk accepted the tree under (:func:`stored_classification`)."""
    def records() -> Iterator[dict]:
        yield {"depth": depth}
        yield from ({"dev": k[0], "ino": k[1], "class": c} for k, c in sorted(classes.items()))
    _stream_jsonl(path, records(), cap, "meta-byte-cap")


def stored_classification(facts: "EndFacts") -> tuple[dict[tuple[int, int], str], int]:
    """The manifest step's classification, as stored, and the depth cap its walk accepted the tree under:
    every later walk of the tree (meta, tree) takes that depth, never the pass's ``Caps.depth``, so a pass
    with a smaller cap cannot refuse a tree the manifest already accepted (and the tree step may already have
    renamed). Absent or not one = ``classes-missing`` (HELD at the phase that needed it), never a recompute.
    DECISION: a classification with no depth line (written before the cap existed) caps nothing: the walk
    that accepted it had none, and the walk's fds are bounded whatever the depth."""
    p = facts.retained / CLASSES
    try:
        data = _read_durable(p)
        out: dict[tuple[int, int], str] = {}
        depth = sys.maxsize
        for n, line in enumerate(data.splitlines()):
            rec = canon.parse(line)
            if n == 0 and isinstance(rec, dict) and set(rec) == {"depth"}:
                if not isinstance(rec["depth"], int) or isinstance(rec["depth"], bool) or rec["depth"] < 1:
                    raise ValueError(line[:80])
                depth = rec["depth"]
                continue
            if (not isinstance(rec, dict) or set(rec) != {"dev", "ino", "class"}
                    or rec["class"] not in (OUTSIDE, UNOWNED)):
                raise ValueError(line[:80])
            out[(int(rec["dev"]), int(rec["ino"]))] = rec["class"]   # type: ignore[arg-type]
    except (OSError, ValueError) as e:
        raise Refused("classes-missing", f"{p}: {e}") from None
    return out, depth


def read_classes(facts: "EndFacts") -> dict[tuple[int, int], str]:
    """The manifest step's classification, as stored (:func:`stored_classification`)."""
    return stored_classification(facts)[0]


def _stream_jsonl(path: Path, records: Iterator[dict], cap: int, cap_name: str) -> None:
    """``records`` as canonical JSONL at ``path``, durable (a temp unique per writer + fsync + rename +
    fsync(dir)); past ``cap`` bytes refuses ``cap_name`` with the temp removed and ``path`` untouched."""
    tmp = temp_path(path)
    fd = os.open(tmp, _O_NEW, 0o600)
    try:
        fh = os.fdopen(fd, "wb", buffering=_CHUNK)
    except BaseException:   # the file object never took the fd: close it here (glm r1 LOW)
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    ok = False
    try:
        with fh:
            total = 0
            for rec in records:
                line = canon.canonical_bytes(rec) + b"\n"
                total += len(line)
                if total > cap:
                    raise Refused(cap_name, f"{path.name} passed {cap} bytes")
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        ok = True
    finally:
        if not ok:
            tmp.unlink(missing_ok=True)
    os.replace(tmp, path)
    fsync_dir(path.parent)


# --- the session-uid reader (L2 H2) ----------------------------------------------------------------

#: ``run_as(uid, gid, fn, wfd, keep)``: run ``fn(wfd)`` as ``uid``/``gid`` with the ``keep`` fds left
#: open, owning ``wfd`` (closing it when ``fn`` ends). Returns the running reader: stage's ``Reader`` whose
#: ``wait()`` returns the reader's exit status (0 = it ended cleanly; anything else, ``None`` included, is a
#: failed reader, comp r1 1).
SessionRunAs = Callable[[int, int, Callable[[int], None], int, Sequence[int]], "stage.Reader"]


class _Forked(stage._Forked):
    """stage's forked reader, whose ``wait()`` also returns the exit status (a signal = 128 + its number). A killed
    reader's is ``None`` (failed): stage's bounded reap takes it, and one that will not die is never waited on. So is
    one still alive past the deadline after its end mark (``link-reader-failed``, never a success)."""

    code: int | None = None

    def wait(self) -> int | None:   # type: ignore[override]
        if self._killed:
            super().wait()
        elif not self._reaped:
            status = self._clean_wait()
            if status is None:
                return None
            self.code = os.waitstatus_to_exitcode(status)
            if self.code < 0:
                self.code = 128 - self.code
        return self.code


def fork_run_as(uid: int, gid: int, fn: Callable[[int], None], wfd: int, keep: Sequence[int] = ()) -> stage.Reader:
    """The real ``run_as``, :func:`levain.slot.stage.fork_run_as`'s pattern with a session uid as the target:
    fork; the child closes every fd but 0-2, ``wfd`` and ``keep``, drops with the one drop
    (:func:`levain.slot.wall.drop_and_assert`, supplementary groups exactly ``{gid}``), runs ``fn`` and
    ``_exit``s (70 if the drop or ``fn`` raised, having written nothing more). Only root can run it.
    DECISION: no RLIMIT_NPROC (the reader never forks)."""
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the child; exercised only as root (VM row)
        code = 0
        try:
            stage._close_fds_except([wfd, *keep])
            wall.drop_and_assert(uid, gid, (gid,), nproc=None)
            fn(wfd)
        except BaseException:   # noqa: BLE001 - the child never returns into the parent's stack
            code = 70
        finally:
            os._exit(code)
    os.close(wfd)
    return _Forked(pid)


def _reader_uid(facts: EndFacts, e: Entry) -> int:
    """DECISION: the session uid that owns the entry's directory (a verifier's child owns ``<S>/repo``,
    §3.4), else the slot uid. Either way a session uid, never root (§1.1: its gid is the uid)."""
    owner = os.fstat(e.dirfd).st_uid
    return owner if owner in facts.uids else facts.slot_uid


def session_reader(dirfd: int, name: bytes, cap: int, wfd: int) -> None:
    """The session-uid reader's whole job, after the drop (comp r1 2, END-side: stage.py folds every open
    errno but ENOENT / ELOOP into one ``staging-open-failed``). One probe open as the reader's uid, with
    stage's own flags, answers :data:`LINK_UNREADABLE` for EACCES / EPERM, the only "this uid may not read
    it" errnos; anything else falls to :func:`levain.slot.stage.reader`, whose open names absent and not
    regular and folds the rest into ``staging-open-failed``, which END takes as a failed reader (HELD, retried)."""
    try:
        fd = os.open(name, stage._O_READ, dir_fd=dirfd)
    except OSError as err:
        if err.errno in (errno.EACCES, errno.EPERM):
            return stage._send_refusal(wfd, LINK_UNREADABLE)
    else:
        os.close(fd)
    stage.reader(dirfd, name, cap, "link-copy-cap", wfd)   # type: ignore[arg-type]


def read_as_session(facts: EndFacts, seams: "EndSeams", e: Entry, out_fd: int | None, cap: int,
                    budget: _Budget) -> tuple[int, str]:
    """``e``'s bytes, read by :func:`session_reader` in a process dropped to a session uid and streamed into
    ``out_fd`` (``None``: hashed only): ``(bytes read, their digest)``, or :data:`UNREAD` when that uid
    cannot read it (absent, not regular, EACCES / EPERM: :data:`UNREAD_ANSWERS`). Anything else is
    ``link-reader-failed`` (HELD, retried), never UNREAD and never a kept file (comp r1 1): a reader that
    ends without an answer (its drop not proven, say), any other open errno, a short read (fewer bytes
    than its ``S`` header promised), a missing end mark, or a reader whose exit status is not 0, so neither a
    broken drop nor a transient error can turn readable work into placeholders or truncated copies.
    DECISION: what the reader read is what is recorded and copied, so a file an outside owner grows meanwhile
    keeps the bytes read (the reader's ``X`` end mark)."""
    uid = _reader_uid(facts, e)
    rfd, wfd = os.pipe()
    proc = None
    try:
        proc = seams.run_as(uid, uid, lambda w: session_reader(e.dirfd, e.name, cap, w), wfd, (e.dirfd,))
        wfd = -1   # run_as owns it now
        if isinstance(proc, stage._Forked):   # the real reader: its clean wait gets only what is left of the budget
            proc.until, proc.clock = budget.deadline, budget.clock
        pipe = stage._Pipe(rfd, budget.deadline, budget.clock, stage.POLL_SLICE_S)
        tag = pipe.exactly(1)
        if tag == b"R":
            ln = pipe.exactly(1)
            name = pipe.exactly(ln[0]).decode("ascii", "replace") if ln else ""
            if name == "link-copy-cap":
                raise Refused("link-copy-cap", f"{e.path!r} is past {cap} bytes")
            if name not in UNREAD_ANSWERS:
                raise Refused("link-reader-failed", f"{e.path!r}: {name}")
            got = UNREAD
        else:
            head = pipe.exactly(8) if tag == b"S" else b""
            if len(head) != 8:
                raise Refused("link-reader-failed", f"{e.path!r}: no answer from the reader")
            accepted = struct.unpack(">Q", head)[0]
            if accepted > cap:
                raise Refused("link-copy-cap", f"{e.path!r}: {accepted} > {cap}")
            h, n = hashlib.sha256(), 0
            while n < accepted:
                chunk = pipe.read(min(_CHUNK, accepted - n))
                if not chunk:
                    break
                if out_fd is not None:
                    write_all(out_fd, chunk)
                h.update(chunk)
                n += len(chunk)
                budget.tick()
            if n != accepted:
                raise Refused("link-reader-failed", f"{e.path!r}: a short read, {n} of {accepted} bytes")
            if pipe.exactly(1) not in (b"X", b"E"):
                raise Refused("link-reader-failed", f"{e.path!r}: the reader sent no end mark")
            got = (n, "sha256:" + h.hexdigest())
        code = proc.wait()
        if code != 0:
            raise Refused("link-reader-failed", f"{e.path!r}: the reader exited {code}")
        return got
    except Refused as err:
        if proc is not None:
            proc.kill()
        if err.name == "staging-raced":   # stage's pipe deadline, which is the manifest budget's
            raise Refused("manifest-time-cap", f"reading {e.path!r} passed the V2 budget") from None
        raise
    except BaseException:
        if proc is not None:
            proc.kill()
        raise
    finally:
        os.close(rfd)          # a reader blocked writing gets EPIPE and ends
        if proc is not None:
            proc.wait()
        if wfd >= 0:
            os.close(wfd)


def _staged_name(key: tuple[int, int]) -> bytes:
    return f"{key[0]}-{key[1]}".encode("ascii")


def _clear_linkcopy(facts: EndFacts, seams: "EndSeams") -> None:
    """Remove a staging dir an earlier attempt left (root's own copies, never work)."""
    d = facts.retained / LINKCOPY
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode) or not seams.owner_ok(st):
        raise Refused("bad-root", f"{d} is not a directory with the expected owner")
    fd = os.open(d, _DIR_FLAGS)
    try:
        for n in os.listdir(fd):
            os.unlink(n, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rmdir(d)
    fsync_dir(facts.retained)


# --- the var-folders dir (1105-10 endvf, DESIGN_end_var_folders (e)+(f)+(g)) ---------------------------------------


def var_folders_preflight(facts: EndFacts) -> os.stat_result | None:
    """(g) The var-folders dir as END may act on it, checked before anything moves (the manifest step, and the tree
    step BEFORE its first rename, so a refusal leaves no half-moved tree): ``None`` when there is none (Linux, or
    absent); else a real directory (``var-folders-not-dir``: a symlink included, never followed) that one of the
    session's uids owns (``var-folders-foreign``), on its parent's filesystem (``var-folders-mount``)."""
    vf = facts.var_folders
    if vf is None:
        return None
    try:
        st = os.lstat(vf)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("var-folders-not-dir", str(vf))
    if st.st_uid not in facts.uids:
        raise Refused("var-folders-foreign", f"{vf} is owned by uid {st.st_uid}")
    if st.st_dev != os.lstat(vf.parent).st_dev:
        raise Refused("var-folders-mount", f"{vf} is on another filesystem than its parent")
    return st


def _scan_var_folders(vf: Path, depth_cap: int, budget: "_Budget") -> list[tuple[bytes, os.stat_result]]:
    """Every entry under ``vf`` (relative path, lstat), in walk order. Only directories and single-link regular files
    may be there: a symlink, a hard link, a device, fifo or socket refuses ``var-folders-special``, named, before a
    byte is copied or anything moves. END never follows, opens or copies one; a person looks.

    L3 r1 (codex 3): each entry counts against the manifest step's ``budget`` AS it is scanned, so a scratch tree past
    the inode or time cap refuses ``manifest-inode-cap`` / ``manifest-time-cap`` with at most ``caps.inodes`` entries
    held, never the whole tree in memory first."""
    out: list[tuple[bytes, os.stat_result]] = []
    for e in walk(vf, depth_cap=depth_cap):
        budget.entry()
        m = e.st.st_mode
        if stat.S_ISDIR(m) or (stat.S_ISREG(m) and e.st.st_nlink == 1):
            out.append((e.path, e.st))
            continue
        what = "hard link" if stat.S_ISREG(m) else entry_type(m)
        raise Refused("var-folders-special", f"{what} {e.path.decode('utf-8', 'replace')}")
    return out


def _clear_vfcopy(facts: EndFacts, seams: "EndSeams") -> None:
    """Remove the copy and key list an earlier attempt's manifest step left (root's own working files, never work:
    the originals are untouched until the tree step)."""
    d = facts.retained / VFCOPY
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISDIR(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{d} is not a directory with the expected owner")
        shutil.rmtree(d)
        fsync_dir(facts.retained)
    unlink_durable(facts.retained / VFKEYS)


def _read_vfkeys(facts: EndFacts) -> frozenset[tuple[bytes, int, int]]:
    try:
        raw = _read_durable(facts.retained / VFKEYS)
    except FileNotFoundError:
        return frozenset()
    out = set()
    for line in raw.splitlines():
        rec = canon.parse(line)
        out.add((base64.b64decode(rec["path_b64"]), rec["dev"], rec["ino"]))   # type: ignore[index]
    return frozenset(out)


def _empty_var_folders(facts: EndFacts, depth_cap: int) -> None:
    """(f) Unlink every original the copy holds (by its walk dirfd, never through a link), then rmdir the dirs the
    session made, deepest first; the skeleton's own dirs are never touched. An entry that is not in the key list (it
    appeared after the manifest, so no copy holds it) is left. An unlink / rmdir refused for good (:data:`_VF_LEFT`)
    leaves the entry for :func:`prove_var_folders_skeleton` to name; any other error raises (HELD, redone)."""
    vf = facts.var_folders
    keys = _read_vfkeys(facts)
    dirs: list[bytes] = []
    for e in walk(vf, depth_cap=depth_cap):   # type: ignore[arg-type]
        if (e.path, e.st.st_dev, e.st.st_ino) not in keys:
            continue
        if stat.S_ISDIR(e.st.st_mode):
            if b"/" in e.path or e.path not in VAR_FOLDERS_SKELETON:
                dirs.append(e.path)
        elif stat.S_ISREG(e.st.st_mode):
            # KNOWN EDGE (L3 r1 codex 1, closed by the head): unlink by name after the inode match; a daemon swap between loses scratch cache only.
            try:
                os.unlink(e.name, dir_fd=e.dirfd)
            except OSError as err:
                if err.errno not in _VF_LEFT:
                    raise
    vfd = os.open(vf, _DIR_FLAGS)   # type: ignore[arg-type]
    try:
        # DECISION: rmdir by a path relative to the dir's fd: every session uid is dead, the walk above saw no link
        # (the manifest step refused any), and rmdir removes only an empty directory.
        for p in reversed(dirs):
            try:
                os.rmdir(p, dir_fd=vfd)
            except OSError as err:
                if err.errno not in _VF_LEFT:
                    raise
    finally:
        os.close(vfd)


def prove_var_folders_skeleton(vf: Path) -> None:
    """(f) The proof: ``vf`` holds only :data:`VAR_FOLDERS_SKELETON` names, each a real, EMPTY directory, else
    ``var-folders-residue`` naming the first entry left (HELD: a writer outside the sandbox left something root
    could not remove, which a person should look at; the next session on the uid never sees it)."""
    fd = os.open(vf, _DIR_FLAGS)
    try:
        for n in sorted(os.fsencode(x) for x in os.listdir(fd)):
            st = os.stat(n, dir_fd=fd, follow_symlinks=False)
            if n not in VAR_FOLDERS_SKELETON or not stat.S_ISDIR(st.st_mode):
                raise Refused("var-folders-residue", n.decode("utf-8", "replace"))
            cfd = _open_dir_at(n, fd, st)
            try:
                left = sorted(os.fsencode(x) for x in os.listdir(cfd))
            finally:
                os.close(cfd)
            if left:
                raise Refused("var-folders-residue", (n + b"/" + left[0]).decode("utf-8", "replace"))
    finally:
        os.close(fd)


#: The manifest's ``path_b64`` of every var-folders entry begins with these characters (the first 18 bytes of
#: :data:`VAR_FOLDERS_NAME`, a whole number of base64 groups): a cheap filter before a line is parsed.
_VF_B64_PREFIX = _b64(VAR_FOLDERS_NAME[:18]).encode("ascii")


def _manifest_var_folders_paths(facts: EndFacts) -> set[bytes]:
    """The var-folders paths the stored manifest names (the dir itself and every entry under it), streamed line by
    line; only lines the base64 prefix may match are parsed."""
    out: set[bytes] = set()
    sub = VAR_FOLDERS_NAME + b"/"
    fd = os.open(facts.retained / "manifest.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        for line in fh:
            if _VF_B64_PREFIX not in line:
                continue
            p = base64.b64decode(canon.parse(line.rstrip(b"\n"))["path_b64"])   # type: ignore[index]
            if p == VAR_FOLDERS_NAME or p.startswith(sub):
                out.add(p)
    return out


def check_var_folders_copy(facts: EndFacts, dest: Path | None = None) -> None:
    """L3 r1 (codex 2): the invariant between the stored manifest and the var-folders copy. The manifest names
    ``.levain-var-folders`` exactly when the copy exists (``vfcopy`` in ``retained/``, or ``dest`` once the tree step
    renamed it there), and then names exactly the paths the copy holds (the key list: every scanned dir, every copied
    file). The scan, the classification and the manifest are separate walks of a dir an outside daemon can still write,
    so a dir it made between them (an absent dir's skeleton, a skeleton dir) is in the manifest and not the copy, and a
    scanned entry gone before the manifest walk is in the copy and not the manifest. Any mismatch refuses
    ``var-folders-changed``, with nothing moved: the manifest step checks it last (its redo re-scans, re-copies and
    re-hashes), the tree step before its first rename."""
    copy = os.path.lexists(facts.retained / VFCOPY) or (dest is not None and os.path.lexists(dest))
    want: set[bytes] = set()
    if copy:
        want = {VAR_FOLDERS_NAME} | {VAR_FOLDERS_NAME + b"/" + p for p, _dev, _ino in _read_vfkeys(facts)}
    if _manifest_var_folders_paths(facts) != want:
        raise Refused("var-folders-changed", "the var-folders copy does not match the manifest")


def _var_folders_tree_step(facts: EndFacts, tree: Path, depth: int, vroot: os.stat_result | None) -> None:
    """The tree step's var-folders sub-step: the manifest step's copy renamed into ``tree/.levain-var-folders`` (one
    rename inside root's own store; a redo whose rename happened fsyncs both parents), then the dir emptied in place
    and the skeleton proven. The dir itself is never moved: SIP pins it (:data:`VAR_FOLDERS_SKELETON`).

    ``vroot`` is :func:`var_folders_preflight`'s answer from the top of :func:`_tree_step` (every redo runs it, before
    the first rename); L3 r1 (deepseek 2) dropped a second preflight here, which ran with ``live`` already moved. A dir
    gone between that preflight and the walk is "none" (complement 2), never a spurious HELD; one still there whose
    walk hit a vanished entry raises as before (HELD, redone)."""
    vcopy = facts.retained / VFCOPY
    dest = tree / os.fsdecode(VAR_FOLDERS_NAME)
    if os.path.lexists(vcopy):
        if os.path.lexists(dest):
            raise Refused("retain-conflict", f"{vcopy} and {dest} both exist")
        _rename_durable(vcopy, dest)
    elif os.path.isdir(dest) and not os.path.islink(dest):
        fsync_dir(facts.retained)
        fsync_dir(tree)
    if vroot is None:
        return
    vf: Path = facts.var_folders   # type: ignore[assignment]
    try:
        _empty_var_folders(facts, depth - 1)
        prove_var_folders_skeleton(vf)
    except FileNotFoundError:
        if os.path.lexists(vf):
            raise
        return


# --- metadata (levain-retention-meta/1): per-OS readers and strips, every call on an fd --------------

MetaReader = Callable[[Entry, Callable[[str], None]], dict]
Strip = Callable[[Entry, Callable[[str], None]], None]

_ACL_TYPE_EXTENDED = 0x00000100
_libc = None


def _darwin_libc():
    global _libc
    if _libc is None:
        c = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        c.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        c.flistxattr.restype = ctypes.c_ssize_t
        c.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.c_uint32, ctypes.c_int]
        c.fgetxattr.restype = ctypes.c_ssize_t
        c.fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        c.fremovexattr.restype = ctypes.c_int
        c.fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
        c.fchflags.restype = ctypes.c_int
        c.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        c.acl_get_fd_np.restype = ctypes.c_void_p
        c.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        c.acl_to_text.restype = ctypes.c_void_p
        c.acl_free.argtypes = [ctypes.c_void_p]
        c.acl_init.argtypes = [ctypes.c_int]
        c.acl_init.restype = ctypes.c_void_p
        c.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        c.acl_set_fd_np.restype = ctypes.c_int
        _libc = c
    return _libc


def _oserr(what: str) -> OSError:
    n = ctypes.get_errno()
    return OSError(n, f"{what}: {os.strerror(n)}")


def _darwin_xattr_names(fd: int) -> list[bytes]:
    c = _darwin_libc()
    for _ in range(4):   # the list can grow between the size call and the read
        n = c.flistxattr(fd, None, 0, 0)
        if n < 0:
            raise _oserr("flistxattr")
        if n == 0:
            return []
        buf = ctypes.create_string_buffer(n)
        m = c.flistxattr(fd, buf, n, 0)
        if m >= 0:
            return [x for x in buf.raw[:m].split(b"\0") if x]
        if ctypes.get_errno() != errno.ERANGE:
            raise _oserr("flistxattr")
    raise OSError(errno.ERANGE, "flistxattr kept growing")


def _darwin_getxattr(fd: int, name: bytes) -> bytes:
    c = _darwin_libc()
    n = c.fgetxattr(fd, name, None, 0, 0, 0)
    if n < 0:
        raise _oserr("fgetxattr")
    buf = ctypes.create_string_buffer(max(n, 1))
    m = c.fgetxattr(fd, name, buf, n, 0, 0)
    if m < 0:
        raise _oserr("fgetxattr")
    return buf.raw[:m]


def _darwin_acl_text(fd: int) -> str | None:
    c = _darwin_libc()
    acl = c.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in (errno.ENOENT, errno.EOPNOTSUPP, errno.EINVAL, 0):
            return None
        raise _oserr("acl_get_fd_np")
    try:
        ln = ctypes.c_ssize_t()
        txt = c.acl_to_text(acl, ctypes.byref(ln))
        if not txt:
            raise _oserr("acl_to_text")
        try:
            return ctypes.string_at(txt, ln.value).decode("utf-8")
        finally:
            c.acl_free(txt)
    finally:
        c.acl_free(acl)


def _xattrs_record(names: list[bytes], get: Callable[[bytes], bytes], where: str,
                   note: Callable[[str], None]) -> list[dict]:
    out = []
    for nm in sorted(names):
        try:
            sname = nm.decode("utf-8")
        except UnicodeDecodeError:
            note(f"meta {where}: xattr name {nm!r} is not UTF-8, not recorded")
            continue
        try:
            out.append({"name": sname, "value_b64": _b64(get(nm))})
        except OSError as e:
            note(f"meta {where}: xattr {sname} unreadable ({e.strerror or e}), not recorded")
    return out


def _where(e: Entry) -> str:
    return e.path.decode("utf-8", "replace")


def _open_for(e: Entry, what: str, note: Callable[[str], None]) -> int | None:
    """:func:`_entry_fd`, with an entry that has no fd form here noted (a bound, L2 M2: such an entry's
    ACL / xattrs are neither recorded nor stripped; on macOS that is a socket or a device)."""
    fd = _entry_fd(e)
    if fd is None:
        note(f"{what} {_where(e)}: a {entry_type(e.st.st_mode)} has no fd form here; ACL and xattrs left as is")
    return fd


def darwin_meta(e: Entry, note: Callable[[str], None]) -> dict:
    """macOS: the extended ACL (``acl_to_text``), the BSD flags (``uchg`` included), and every xattr
    (``com.apple.FinderInfo``, ``com.apple.ResourceFork`` included), all on an fd of the entry itself.
    What cannot be read is noted and left out, never a wedge (plan §2i 4)."""
    rec: dict = {}
    where = _where(e)
    flags = getattr(e.st, "st_flags", 0)
    if flags:
        rec["flags"] = flags
    try:
        fd = _open_for(e, "meta", note)
    except OSError as err:
        note(f"meta {where}: not opened ({err.strerror or err}); ACL and xattrs not recorded")
        return rec
    if fd is None:
        return rec
    try:
        try:
            acl = _darwin_acl_text(fd)
            if acl:
                rec["acl"] = acl
        except OSError as err:
            note(f"meta {where}: ACL unreadable ({err.strerror or err})")
        try:
            names = _darwin_xattr_names(fd)
        except OSError as err:
            note(f"meta {where}: xattrs unlistable ({err.strerror or err})")
            names = []
        xs = _xattrs_record(names, lambda nm: _darwin_getxattr(fd, nm), where, note)
        if xs:
            rec["xattrs"] = xs
    finally:
        os.close(fd)
    return rec


def _proc_path(e: Entry) -> bytes:
    """Linux: the entry by its walk dirfd (``/proc/self/fd/<dirfd>/<name>``, used not following the last
    component), for the types with no fd form; short whatever the tree's depth."""
    return b"/proc/self/fd/%d/" % e.dirfd + e.name


def _lx(call: Callable, target: int | bytes, *args):
    return call(target, *args) if isinstance(target, int) else call(target, *args, follow_symlinks=False)


def linux_meta(e: Entry, note: Callable[[str], None]) -> dict:
    """Linux: every xattr (POSIX ACLs are the ``system.posix_acl_*`` xattrs), on an fd of the entry, or
    by its walk dirfd's ``/proc`` path for a symlink / socket / device."""
    where = _where(e)
    try:
        fd = _entry_fd(e)
    except OSError as err:
        note(f"meta {where}: not opened ({err.strerror or err}); xattrs not recorded")
        return {}
    target: int | bytes = fd if fd is not None else _proc_path(e)
    try:
        try:
            names = [os.fsencode(n) for n in _lx(os.listxattr, target)]
        except OSError as err:
            if err.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                note(f"meta {where}: xattrs unlistable ({err.strerror or err})")
            return {}
        xs = _xattrs_record(names, lambda nm: _lx(os.getxattr, target, nm), where, note)
        return {"xattrs": xs} if xs else {}
    finally:
        if fd is not None:
            os.close(fd)


def _clear_setid(fd: int, e: Entry) -> None:
    """L2 M1: setuid / setgid off a file or directory BEFORE root's chown (macOS keeps them on a root
    chown). Raises when it cannot: a set-id entry is never handed to the chown."""
    m = e.st.st_mode
    if (stat.S_ISREG(m) or stat.S_ISDIR(m)) and m & _SETID:
        os.fchmod(fd, stat.S_IMODE(m) & ~_SETID)


#: SIP's marker on the system-made var-folders dirs: root cannot remove it (EPERM on every END [measured:
#: levain-memory runs/s2-macos/RESULT_D1D2.md, NEW findings, macOS 26.6.2]) and it grants the slot nothing, so
#: the strip skips this one name rather than note its failure every time.
_ROOTLESS_XATTR = b"com.apple.rootless"


def darwin_strip(e: Entry, note: Callable[[str], None]) -> None:
    """Flags first (``uchg`` blocks the rest), then setuid / setgid (a gate: raises), then the ACL (an
    empty one set), then each xattr, all on an fd of the entry. A flag, ACL or xattr failure is noted and
    the strip carries on: it is hygiene, not a gate (plan §2i 4)."""
    where = _where(e)
    c = _darwin_libc()
    fd = _open_for(e, "strip", note)
    if fd is None:
        return
    try:
        if getattr(e.st, "st_flags", 0) and c.fchflags(fd, 0) != 0:
            note(f"strip {where}: flags not cleared ({_oserr('fchflags').strerror})")
        _clear_setid(fd, e)
        try:
            if _darwin_acl_text(fd):
                empty = c.acl_init(1)
                try:
                    if c.acl_set_fd_np(fd, empty, _ACL_TYPE_EXTENDED) != 0:
                        note(f"strip {where}: ACL not cleared ({_oserr('acl_set_fd_np').strerror})")
                finally:
                    c.acl_free(empty)
        except OSError as err:
            note(f"strip {where}: ACL unreadable ({err.strerror or err})")
        try:
            names = _darwin_xattr_names(fd)
        except OSError as err:
            note(f"strip {where}: xattrs unlistable ({err.strerror or err})")
            return
        for nm in names:
            if nm == _ROOTLESS_XATTR:
                continue
            if c.fremovexattr(fd, nm, 0) != 0:
                note(f"strip {where}: xattr {nm!r} not removed ({_oserr('fremovexattr').strerror})")
    finally:
        os.close(fd)


def linux_strip(e: Entry, note: Callable[[str], None]) -> None:
    where = _where(e)
    fd = _entry_fd(e)
    target: int | bytes = fd if fd is not None else _proc_path(e)
    try:
        if fd is not None:
            _clear_setid(fd, e)
        try:
            names = _lx(os.listxattr, target)
        except OSError as err:
            if err.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                note(f"strip {where}: xattrs unlistable ({err.strerror or err})")
            return
        for nm in names:
            try:
                _lx(os.removexattr, target, nm)
            except OSError as err:
                note(f"strip {where}: xattr {nm!r} not removed ({err.strerror or err})")
    finally:
        if fd is not None:
            os.close(fd)


def default_meta_reader() -> MetaReader:
    return darwin_meta if sys.platform == "darwin" else linux_meta


def default_strip() -> Strip:
    return darwin_strip if sys.platform == "darwin" else linux_strip


def iter_meta(root: Path, *, extra: Mapping[bytes, Path] | None = None, reader: MetaReader,
              note: Callable[[str], None], skip: Collection[tuple[int, int]] = (),
              depth_cap: int = MANIFEST_DEPTH) -> Iterator[dict]:
    """``{path_b64, acl?, flags?, xattrs?}`` for every entry that carries any of the three. ``skip``: the
    classified inodes (:func:`classify`, any type: outside-linked, or not a session uid's), whose metadata
    root never reads. DECISION: an entry with none gets no line, so meta stays within V17's 64 MiB beside a
    full manifest."""
    skip = frozenset(skip)
    for e in walk(root, extra, depth_cap=depth_cap):
        if (e.st.st_dev, e.st.st_ino) in skip:
            continue
        rec = reader(e, note)
        if rec:
            yield {"path_b64": _b64(e.path), **rec}


# --- chown + the outside-link copies ---------------------------------------------------------------

Chown = Callable[[int, bytes, int, int], None]

#: The errnos of a placement the filesystem refuses for good (an immutable entry or directory: EPERM / EACCES;
#: a link count at its limit: EMLINK): the one class of placement failure that is noted, not raised.
_PLACE_REFUSED = frozenset({errno.EPERM, errno.EACCES, errno.EMLINK})


def os_chown(dirfd: int, name: bytes, uid: int, gid: int) -> None:
    """``fchownat(dirfd, name, uid, gid, AT_SYMLINK_NOFOLLOW)``: a link's own owner, never its target's."""
    os.chown(name, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def _place_link_copies(facts: EndFacts, tree: Path, note: Callable[[str], None], depth_cap: int) -> None:
    """Each in-tree link of an outside-linked inode becomes a hard link to that inode's staged copy (the
    manifest step's: the bytes the session uid read and hashed), so an in-tree link set stays one inode
    and the outside file is no longer shared (root never chowns a shared inode, plan §2g 2). Redone after a
    crash: an entry already placed (its inode is a staged copy's) is left and its directory fsynced before
    any staging copy is unlinked, a temp link this step made and did not rename is removed, and the staging
    dir goes last. Only a placement the filesystem REFUSES
    (:data:`_PLACE_REFUSED`: an immutable directory or file, a link count at its limit) is noted, and that
    entry stays shared: unstripped, unchowned. Every other failure (ENOSPC, EIO, a failed fsync) propagates,
    so the tree phase stays journaled and the reserve's retry runs (codex r1 7); a temp link whose rename
    failed is removed first, so none is left in the retained tree."""
    sdir = facts.retained / LINKCOPY
    try:
        sfd = os.open(sdir, _DIR_FLAGS)
    except FileNotFoundError:
        return
    try:
        staged: dict[bytes, os.stat_result] = {}
        for n in os.listdir(sfd):
            nb = os.fsencode(n)
            st = os.stat(nb, dir_fd=sfd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                staged[nb] = st
        copies = {(st.st_dev, st.st_ino) for st in staged.values()}
        synced: set[bytes] = set()   # the tree dirs (by path) fsynced since their last placement
        for e in walk(tree, depth_cap=depth_cap):
            if not stat.S_ISREG(e.st.st_mode):
                continue
            key = (e.st.st_dev, e.st.st_ino)
            parent = os.path.dirname(e.path)
            if key in copies:
                if e.name.startswith(_LINK_TMP):
                    os.unlink(e.name, dir_fd=e.dirfd)
                    os.fsync(e.dirfd)
                    synced.add(parent)
                elif parent not in synced:
                    # Placed by an earlier pass whose dir fsync may have failed (comp r2 N1): made durable
                    # here, before the staging copies it links to are unlinked below.
                    os.fsync(e.dirfd)
                    synced.add(parent)
                continue
            src = _staged_name(key)
            if src not in staged:
                continue
            tmp = _LINK_TMP + secrets.token_hex(8).encode("ascii")
            try:
                os.link(src, tmp, src_dir_fd=sfd, dst_dir_fd=e.dirfd)
            except OSError as err:
                if err.errno not in _PLACE_REFUSED:
                    raise
                note(f"link {e.abspath!r}: not replaced by its copy ({err.strerror or err}); left unstripped, "
                     f"unchowned")
                continue
            try:
                os.rename(tmp, e.name, src_dir_fd=e.dirfd, dst_dir_fd=e.dirfd)
            except OSError as err:
                try:
                    os.unlink(tmp, dir_fd=e.dirfd)
                except FileNotFoundError:   # comp r2 L3: never masks the rename's errno
                    pass
                if err.errno not in _PLACE_REFUSED:
                    raise
                note(f"link {e.abspath!r}: not replaced by its copy ({err.strerror or err}); left unstripped, "
                     f"unchowned")
                continue
            os.fsync(e.dirfd)
            synced.add(parent)
        for nb in staged:
            os.unlink(nb, dir_fd=sfd)
        os.fsync(sfd)
    finally:
        os.close(sfd)
    os.rmdir(sdir)
    fsync_dir(facts.retained)


# --- seams -----------------------------------------------------------------------------------------


def unbuilt_freeze(uid: int, bound_s: float) -> bool:
    """Freeze every process of ``uid`` within ``bound_s`` (V12); ``True`` only when that is PROVEN, and
    past the bound is not proven. END calls it once per uid the session holds. This default refuses, which
    lands HELD; the real seam is :func:`levain.slot.proc.end_seams` ``(host, cgroup)``'s, and how it
    proves the freeze is proc's to say."""
    raise Refused("seam-unbuilt", "freeze: pass levain.slot.proc.end_seams(host, cgroup)")


def unbuilt_kill(uid: int, bound_s: float) -> bool:
    """SIGKILL every process of ``uid`` within ``bound_s`` (V12); ``True`` only when that is PROVEN. This
    default refuses (HELD); the real seam is :func:`levain.slot.proc.end_seams`'s."""
    raise Refused("seam-unbuilt", "kill: pass levain.slot.proc.end_seams(host, cgroup)")


def unbuilt_no_process(uid: int) -> bool:
    """The proof END rests on: ``True`` only when no process of ``uid`` exists. This default refuses
    (HELD); the real seam is :func:`levain.slot.proc.end_seams`'s."""
    raise Refused("seam-unbuilt", "no_process: pass levain.slot.proc.end_seams(host, cgroup)")


def sync_tree(tree: Path) -> None:
    """The tree step's metadata (setid bits cleared, xattrs / ACLs / flags stripped, owners root) durable
    before the journal leaves ``tree`` (codex r1 5). DECISION: one filesystem-wide flush rather than an fsync
    per mutated inode (V1 allows 2 M). Linux: ``syncfs(2)`` on the tree's filesystem (libc's; ``sync(2)`` where
    libc has none). macOS: ``sync(2)``, then ``F_FULLFSYNC`` on the tree directory, which also asks the
    device to flush its write cache. A failure raises (HELD at ``tree``, redone)."""
    fd = os.open(tree, _DIR_FLAGS)
    try:
        if sys.platform == "darwin":
            os.sync()
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)   # type: ignore[attr-defined]
            return
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        syncfs = getattr(libc, "syncfs", None)
        if syncfs is None:
            os.sync()
            return
        syncfs.argtypes = [ctypes.c_int]
        if syncfs(fd) != 0:
            raise _oserr("syncfs")
    finally:
        os.close(fd)


def read_slot_state(path: Path) -> bytes | None:
    """Root's own slot state file (§3.5 Pool, root 0644 in a root dir), ``None`` when absent; a link or a
    non-regular file refuses."""
    return read_record(path, max_bytes=4096)


@dataclass
class EndSeams:
    cas: Cas
    freeze: Callable[[int, float], bool] = unbuilt_freeze
    kill: Callable[[int, float], bool] = unbuilt_kill
    no_process: Callable[[int], bool] = unbuilt_no_process
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], str] = utc_now
    meta_reader: MetaReader = field(default_factory=default_meta_reader)
    strip: Strip = field(default_factory=default_strip)
    chown: Chown = os_chown
    sync: Callable[[Path], None] = sync_tree
    run_as: SessionRunAs = fork_run_as
    slot_state: Callable[[Path], "bytes | None"] = read_slot_state
    root_uid: int = 0
    root_gid: int = 0
    fault: Fault = _no_fault
    note: Callable[[str], None] = lambda msg: None
    reserve: "Reserve | None" = None
    owner_ok: Callable[[os.stat_result], bool] = _root_owned
    #: identseed: the owner rule for the session's config file the seed is read from; None = ``st_uid`` is alloc's slot
    #: uid (tests whose files the slot never owns inject theirs).
    seed_owner_ok: "Callable[[os.stat_result], bool] | None" = None

    @property
    def retry(self) -> Callable[[Callable[[], None]], None]:
        return self.reserve.retry if self.reserve is not None else (lambda fn: fn())


# --- CONTROL's CAS (L2 M5) --------------------------------------------------------------------------


class CasNotSent(Exception):
    """A CAS CONTROL did not take: never a step failure and never HELD. The record stays where it is; every
    proof and act END runs is idempotent, so the next pass redoes them and sends it again."""

    def __init__(self, target: str, cause: BaseException) -> None:
        self.target, self.cause = target, cause
        super().__init__(f"CAS {target} not sent: {cause}")


def send_cas(seams: EndSeams, target: str, fields: Mapping[str, object]) -> None:
    """``seams.cas(target, fields)`` under §3.3's client rule: ``locked`` (a try-lock collision) and a
    CONTROL that is down (``OSError``) are retried with backoff for up to :data:`CAS_RETRY_SECONDS`;
    ``stale-generation`` is retried once (the seam reads the record's generation on every call). Anything
    else, or the retries spent, raises :class:`CasNotSent`. A CAS out of HELD (any target but HELD) also
    clears ``failed_step``, ``held_attempts`` and ``held_reason`` (L1-F4: CONTROL merges, so an unnamed field
    survives). A CONTROL that predates ``held_reason`` refuses it ``field-not-casable``: the optional classification
    is dropped and the CAS sent again once, so a mixed-version window never keeps a session from ending."""
    if target != HELD:
        fields = {"failed_step": None, "held_attempts": None, "held_reason": None, **fields}
    t0 = seams.clock()
    delay = CAS_BACKOFF_FIRST
    stale_retried = False
    reason_dropped = False
    while True:
        try:
            seams.cas(target, fields)
            return
        except Refused as e:
            if e.name == "stale-generation" and not stale_retried:
                stale_retried = True
                continue
            if e.name == "field-not-casable" and "held_reason" in fields and not reason_dropped:
                reason_dropped = True
                fields = {k: v for k, v in fields.items() if k != "held_reason"}
                continue
            if e.name != "locked":
                raise CasNotSent(target, e) from None
            err: BaseException = e
        except OSError as e:
            err = e
        if seams.clock() - t0 + delay > CAS_RETRY_SECONDS:
            raise CasNotSent(target, err) from None
        seams.sleep(delay)
        delay = min(delay * 2, CAS_BACKOFF_MAX)


# --- the retain intent's steps ---------------------------------------------------------------------


def _init_step(facts: EndFacts, seams: EndSeams) -> None:
    """A fresh ``L/retained/<s>/`` (root 0700). Redone after a crash: an existing one is accepted only
    when it is a real directory with the right owner holding no ``tree`` yet. The parent's fsync runs on a redo
    too, and before the ``retained-exists`` refusal: the crashed run may have made ``retained/<s>/`` and died before
    syncing its entry."""
    try:
        os.mkdir(facts.retained, 0o700)
    except FileExistsError:
        st = os.lstat(facts.retained)
        if not stat.S_ISDIR(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{facts.retained} is not a directory with the expected owner") from None
        fsync_dir(facts.retained.parent)
        if os.path.lexists(facts.tree):
            raise Refused("retained-exists", str(facts.tree)) from None
    fsync_dir(facts.retained.parent)
    os.chmod(facts.retained, 0o700)   # a real directory, checked above or just made
    if not os.path.isdir(facts.live):
        raise Refused("live-missing", str(facts.live))


def _manifest_step(facts: EndFacts, seams: EndSeams, caps: Caps) -> None:
    """The manifest of the frozen tree, durable, before any mutation. First the ONE classification
    (:func:`classify`), stored as ``classes.jsonl``. An :data:`OUTSIDE` inode (L2 H2) is read once, by the
    session-uid reader, into ``L/retained/<s>/linkcopy/<dev>-<ino>`` (the copy keeps its mode less
    setuid/setgid; an UNREAD one is an empty 0600 placeholder); an :data:`UNOWNED` file is read once by it and
    hashed, never copied (codex r1 4); that read is what the manifest records, and an UNOWNED file it cannot
    read refuses ``unowned-unreadable`` (codex r2 N2: its real bytes stay in the tree, so UNREAD's size 0
    would be a false record). The exception: START's capture logs (:func:`_hash_start_capture`), root's own
    files no session uid may read by design, are hashed by root; their class stays UNOWNED, so the meta and tree
    steps treat them as before. The copy cap is checked
    before anything is read, and is ONE budget across every reader: each gets only what the reads before it
    left (codex r1 8), so files an outside holder grows after the sum cannot stage past it.

    1105-10 endvf (DESIGN_end_var_folders (f)): the var-folders dir is checked (:func:`var_folders_preflight`,
    :func:`_scan_var_folders`: a special entry refuses here, before anything moves) and copied into
    ``L/retained/<s>/vfcopy/`` as it is hashed: a session-owned file by root, an UNOWNED one by the session-uid reader
    (never root's bytes for a file no session uid can read). Its bytes count against the same copy cap. The copied
    originals are listed in ``vfkeys.jsonl`` for the tree step, which alone unlinks them. The scan counts against the
    manifest's own inode / time budget as it goes (L3 r1 codex 3), and the step ends by checking the copy against the
    manifest it wrote (:func:`check_var_folders_copy`, codex 2): a mismatch is HELD here, and the redo re-derives both."""
    budget = _Budget(caps, seams.clock)
    vroot = var_folders_preflight(facts)
    scan: list[tuple[bytes, os.stat_result]] = []
    if vroot is not None:
        try:
            scan = _scan_var_folders(facts.var_folders, caps.depth - 1, budget)  # type: ignore[arg-type]
        except FileNotFoundError:
            if os.path.lexists(facts.var_folders):   # type: ignore[arg-type]
                raise
            vroot = None   # gone since the preflight (complement 2): none; the copy check below sees the manifest's
    # The manifest walk counts these entries again, with the rest of the tree; the deadline keeps running.
    budget.entries = 0
    classes, outside = classify(facts.live, facts.extra, facts.uids, depth_cap=caps.depth)
    copy = outside_link_bytes(outside) + sum(st.st_size for _, st in scan if stat.S_ISREG(st.st_mode))
    if copy > caps.link_copy_bytes:
        raise Refused("link-copy-cap", f"{copy} bytes to copy (outside links, var-folders) > {caps.link_copy_bytes}")
    _clear_linkcopy(facts, seams)
    _clear_vfcopy(facts, seams)
    _write_classes(facts.retained / CLASSES, classes, caps.meta_bytes, caps.depth)
    vcopy = os.fsencode(facts.retained / VFCOPY)
    vf_files: dict[tuple[int, int], bytes] = {}
    staged: dict[tuple[int, int], tuple[int, str]] = {}
    copied = 0

    def stage_var_folders(e: Entry, key: tuple[int, int], budget: _Budget) -> tuple[int, str]:
        nonlocal copied
        rel = vf_files[key]
        if e.path != VAR_FOLDERS_NAME + b"/" + rel:
            raise Refused("tree-changed", f"{e.abspath!r} is not the var-folders entry scanned")
        out = os.open(vcopy + b"/" + rel, _O_NEW, 0o600)
        try:
            if classes.get(key) == UNOWNED:
                got = read_as_session(facts, seams, e, out, caps.link_copy_bytes - copied, budget)
                if got == UNREAD:
                    raise Refused("unowned-unreadable", f"{e.abspath!r}: no session uid can read it, so it is "
                                                        f"not copied")
            else:
                got = _hash_file(e, budget, out)
            os.fchmod(out, stat.S_IMODE(e.st.st_mode) & ~_SETID)
            os.fsync(out)
        finally:
            os.close(out)
        copied += got[0]
        if copied > caps.link_copy_bytes:
            raise Refused("link-copy-cap", f"{e.abspath!r}: the copies passed {caps.link_copy_bytes} bytes")
        return got

    def read_foreign(e: Entry, budget: _Budget) -> tuple[int, str]:
        nonlocal copied
        key = (e.st.st_dev, e.st.st_ino)
        if key not in staged:
            if key in vf_files:
                staged[key] = stage_var_folders(e, key, budget)
                return staged[key]
            if classes[key] == OUTSIDE:
                out = os.open(_staged_name(key), _O_NEW, 0o600, dir_fd=sfd)
                try:
                    got = read_as_session(facts, seams, e, out, caps.link_copy_bytes - copied, budget)
                    os.fchmod(out, 0o600 if got == UNREAD else stat.S_IMODE(e.st.st_mode) & ~_SETID)
                    os.fsync(out)
                finally:
                    os.close(out)
                copied += got[0]
            else:
                # 1105-10 endfix (a): START's own root-only capture is hashed by root; every other UNOWNED file
                # still goes to the session uid, and refuses below when that uid cannot read it.
                capture = _hash_start_capture(e, budget, seams.root_uid, seams.clock, seams.sleep)
                got = capture if capture is not None else read_as_session(facts, seams, e, None, HASH_ONLY_CAP,
                                                                          budget)
                if got == UNREAD:
                    # codex r2 N2: an UNOWNED inode has no placeholder; it stays in the tree, so a size-0,
                    # digest-"" line would misdescribe the bytes retained. Fail closed: HELD, named.
                    raise Refused("unowned-unreadable",
                                  f"{e.abspath!r}: no session uid can read it, and it is not copied")
            if got == UNREAD:
                seams.note(f"{classes[key]} {e.abspath!r}: the session uid cannot read it; recorded, not copied")
            staged[key] = got
        return staged[key]

    # L3 r1 (codex 5): everything after ``sfd`` opens is inside the ``finally`` that closes it, so a failing mkdir of
    # the copy (ENAMETOOLONG, ENOSPC, EACCES) on a ~31 s retry cannot leak one fd per attempt.
    sfd = -1
    try:
        if outside:
            os.mkdir(facts.retained / LINKCOPY, 0o700)
            fsync_dir(facts.retained)
            sfd = os.open(facts.retained / LINKCOPY, _DIR_FLAGS)
        if vroot is not None:
            os.mkdir(vcopy, 0o700)
            for rel, st in scan:
                if stat.S_ISDIR(st.st_mode):
                    os.mkdir(vcopy + b"/" + rel, 0o700)   # 0700 while it fills; its own mode once full
                else:
                    vf_files[(st.st_dev, st.st_ino)] = rel
        _stream_jsonl(facts.retained / "manifest.jsonl",
                      iter_manifest(facts.live, extra=facts.extra, caps=caps, clock=seams.clock,
                                    credentials=facts.parked_paths, foreign=set(classes) | set(vf_files),
                                    read_foreign=read_foreign, budget=budget),
                      caps.manifest_bytes, "manifest-byte-cap")
        if sfd >= 0:
            os.fsync(sfd)
    finally:
        if sfd >= 0:
            os.close(sfd)
    if vroot is not None:
        _seal_vfcopy(facts, vcopy, vroot, scan, {k for k in vf_files if k in staged}, caps)
    check_var_folders_copy(facts)
    seams.fault("retain:manifest:written")


def _seal_vfcopy(facts: EndFacts, vcopy: bytes, vroot: os.stat_result, scan: list[tuple[bytes, os.stat_result]],
                 copied: Collection[tuple[int, int]], caps: Caps) -> None:
    """The copy's dirs durable and given their originals' modes (less setuid / setgid, deepest first, the root
    last), then the key list: every scanned dir and every file whose bytes were copied, by (path, dev, ino). The key
    list is capped by the manifest's byte cap (``manifest-byte-cap``, L3 r1 complement 3): it lists a subset of the
    manifest's entries, never metadata. The scan is in walk order (a dir before its children), so its reverse is
    deepest first, and the root goes after all of it (L3 r1 deepseek 3: it used to go first)."""
    for rel, st in reversed([(b"", vroot)] + [(r, s) for r, s in scan if stat.S_ISDIR(s.st_mode)]):
        p = vcopy + b"/" + rel if rel else vcopy
        fd = os.open(p, _DIR_FLAGS)
        try:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) & ~_SETID)
            os.fsync(fd)
        finally:
            os.close(fd)
    fsync_dir(facts.retained)
    keep = [(rel, st) for rel, st in scan if stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) in copied]
    _stream_jsonl(facts.retained / VFKEYS,
                  iter({"path_b64": _b64(rel), "dev": st.st_dev, "ino": st.st_ino} for rel, st in keep),
                  caps.manifest_bytes, "manifest-byte-cap")


def _meta_step(facts: EndFacts, seams: EndSeams, caps: Caps) -> None:
    """The metadata of every entry the manifest step's classification left to root: the STORED one, never a
    recompute (codex r1 4: an outside link removed between the two steps cannot hand root the inode), walked
    under the depth the manifest step accepted."""
    classes, depth = stored_classification(facts)
    _stream_jsonl(facts.retained / "meta.jsonl",
                  iter_meta(facts.live, extra=facts.extra, reader=seams.meta_reader, note=seams.note,
                            skip=classes, depth_cap=depth),
                  caps.meta_bytes, "meta-byte-cap")


def _read_durable(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


#: Row 165: the one file a slot's "don't ask again" dialog writes, by its path below the session root (the tree), and
#: the most of it root ever hands back (``read-allow``).
ALLOW_REL = (b"repo", b".claude", b"settings.local.json")
ALLOW_CAP = 64 * 1024
_ALLOW_PATH_B64 = _b64(b"/".join(ALLOW_REL))


def session_owner(facts: EndFacts) -> int | None:
    """The uid the session's vendor dirs are owned by (§2.1): egress the slot uid, verifier the child uid."""
    return facts.slot_uid if facts.slot.slot_class == "egress" else facts.child_uid


def _live_allow_stat(facts: EndFacts) -> os.stat_result | None:
    """The live dialog file's own lstat, reached one directory at a time with none followed; None when any
    component is absent or not a real directory."""
    try:
        cur = os.open(facts.live, _DIR_FLAGS)
    except OSError:
        return None
    try:
        for part in ALLOW_REL[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=cur)
            except OSError:
                return None
            prev, cur = cur, nxt
            os.close(prev)
        try:
            return os.stat(ALLOW_REL[-1], dir_fd=cur, follow_symlinks=False)
        except OSError:
            return None
    finally:
        os.close(cur)


def allow_provenance(facts: EndFacts, rec: Mapping[str, object] | None) -> dict | None:
    """Row 165 (codex L3 HIGH: the tree step gives every retained entry to root, so ``read-allow`` cannot tell from
    the owner whose file it was): the dialog file's provenance, taken while the tree is still the session's and every
    session uid is proven dead, and kept in root's ``object.json`` as ``allow: {size, digest}``. ``rec`` is the
    manifest's line for :data:`ALLOW_REL` (its digest root's own hash of the bytes, :func:`_hash_file`). Recorded only
    for a regular file of one link, within :data:`ALLOW_CAP`, owned by EXACTLY the session owner
    (:func:`session_owner`) and still the inode the manifest described (size, link count); anything else, an
    UNOWNED or outside-linked file included, gets no record, so ``read-allow`` never reads it. Deterministic on a
    redo: the object step runs before the tree step, on the frozen live tree."""
    if rec is None or rec.get("type") != "file" or rec.get("nlink") != 1:
        return None
    size, digest = rec.get("size"), rec.get("digest")
    if type(size) is not int or not 0 <= size <= ALLOW_CAP or type(digest) is not str:
        return None
    owner = session_owner(facts)
    st = _live_allow_stat(facts)
    if (owner is None or st is None or not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_nlink != 1
            or st.st_size != size):
        return None
    return {"size": size, "digest": digest}


def object_record(facts: EndFacts, created_at: str) -> dict:
    """``levain-retained-object/1`` from the two durable inputs and the frozen live tree, so a redo writes the same
    bytes. ``entries`` = manifest lines; ``bytes`` = the sum of their ``size``; ``created_at`` = the retain
    journal's ``started_at`` (DECISION: deterministic across a redo); ``allow`` (row 165, only when there is one) =
    :func:`allow_provenance`. An object without ``allow`` (none recorded, or retained before row 165) has nothing
    ``read-allow`` will read."""
    manifest = _read_durable(facts.retained / "manifest.jsonl")
    meta = _read_durable(facts.retained / "meta.jsonl")
    entries = total = 0
    allow_rec = None
    for line in manifest.splitlines():
        rec = canon.parse(line)
        entries += 1
        total += rec["size"]   # type: ignore[index]
        if rec["path_b64"] == _ALLOW_PATH_B64:   # type: ignore[index]
            allow_rec = rec
    out = {"format": OBJECT_FORMAT, "session": facts.session, "manifest_digest": digest_bytes(manifest),
           "meta_digest": digest_bytes(meta), "entries": entries, "bytes": total, "created_at": created_at}
    allow = allow_provenance(facts, allow_rec)   # type: ignore[arg-type]
    if allow is not None:
        out["allow"] = allow
    return out


def _object_step(facts: EndFacts, created_at: str) -> None:
    atomic_write(facts.retained / "object.json", canon.canonical_bytes(object_record(facts, created_at)), 0o600)


def _rename_durable(src: Path, dst: Path) -> None:
    os.rename(src, dst)
    fsync_dir(src.parent)
    fsync_dir(dst.parent)


def _tree_step(facts: EndFacts, seams: EndSeams) -> None:
    """Rename the session's work into ``tree/`` (and place the copy of macOS ``/var/folders/<slot>`` at
    ``tree/.levain-var-folders/``), link the staged copies over the outside-linked files, strip (setuid /
    setgid included), then chown root, then flush it all (:attr:`EndSeams.sync`) before the journal may
    leave ``tree`` (codex r1 5). Each sub-step is idempotent; a redo whose rename already happened still
    fsyncs both parents before going on (codex r1 6). Every session uid is proven dead, so nothing changes
    under the walks, and every call is on a walk dirfd or an fd of the entry (L2 M2). An inode a path outside
    the tree still shares (a copy that could not be placed) is never stripped or chowned, and a shared
    non-regular one refuses ``tree-shared-special`` (comp r1 4). An :data:`UNOWNED` inode has only its
    setuid / setgid cleared, by fd: root reads none of its metadata (codex r1 4). Every walk takes the depth the
    manifest step accepted, so no depth refusal can land on a renamed tree; the classification is read before
    the rename, so a missing one refuses with nothing moved.

    1105-10 endvf: the var-folders dir is NOT renamed (SIP pins it, :data:`VAR_FOLDERS_SKELETON`). Its checks
    (:func:`var_folders_preflight`, :func:`check_var_folders_copy`) run before the first rename, so a refusal leaves
    ``live/<s>`` unmoved; after it, :func:`_var_folders_tree_step` places the manifest step's copy, empties the dir in
    place and proves the skeleton."""
    classes, depth = stored_classification(facts)
    vroot = var_folders_preflight(facts)
    tree = facts.tree
    check_var_folders_copy(facts, tree / os.fsdecode(VAR_FOLDERS_NAME))
    if os.path.lexists(facts.live):
        if os.path.lexists(tree):
            raise Refused("retain-conflict", f"{facts.live} and {tree} both exist")
        _rename_durable(facts.live, tree)
    elif not (os.path.isdir(tree) and not os.path.islink(tree)):
        raise Refused("tree-missing", str(tree))
    else:
        fsync_dir(facts.live.parent)   # the rename happened; its durability may not have
        fsync_dir(tree.parent)
    seams.fault("retain:tree:renamed")

    _var_folders_tree_step(facts, tree, depth, vroot)
    seams.fault("retain:tree:var-folders")

    # DECISION: the outside-link copies are placed before the strip (§7.4 names rename, strip, fchown),
    # so the strip never clears flags or xattrs on an inode a path outside the tree still shares.
    _place_link_copies(facts, tree, seams.note, depth)
    seams.fault("retain:tree:links")

    shared = foreign_links(tree, depth_cap=depth)
    special = [k for k, lc in shared.items() if not lc.regular]
    if special:
        raise Refused("tree-shared-special", f"inode {special[0][0]}-{special[0][1]} is linked outside the tree")
    for e in walk(tree, depth_cap=depth):
        key = (e.st.st_dev, e.st.st_ino)
        if key in shared:
            continue
        if classes.get(key) == UNOWNED:
            _clear_setid_only(e)
        else:
            seams.strip(e, seams.note)
    seams.fault("retain:tree:stripped")

    rfd = os.open(facts.retained, _DIR_FLAGS)
    try:
        seams.chown(rfd, b"tree", seams.root_uid, seams.root_gid)
    finally:
        os.close(rfd)
    for e in walk(tree, depth_cap=depth):
        if (e.st.st_dev, e.st.st_ino) not in shared:
            seams.chown(e.dirfd, e.name, seams.root_uid, seams.root_gid)
    fsync_dir(tree)
    seams.sync(tree)


def _clear_setid_only(e: Entry) -> None:
    """An :data:`UNOWNED` entry's strip: setuid / setgid off by fd before root's chown (a gate, as in the
    full strip), no xattr, ACL or flag read. DECISION: its other metadata stays as found."""
    m = e.st.st_mode
    if not ((stat.S_ISREG(m) or stat.S_ISDIR(m)) and m & _SETID):
        return
    fd = _entry_fd(e)
    if fd is None:
        return
    try:
        _clear_setid(fd, e)
    finally:
        os.close(fd)


def _commit_step(facts: EndFacts, seams: EndSeams) -> None:
    """Root's ``retained_at`` (0644; the dispatch retention age clock, §3.5), written once: a redo
    keeps the first one, so HELD or crash time never moves it. First the classification goes: the journal is
    past ``tree`` (forward only), so no step that reads it can run again, and the retained object is §7.4's
    list exactly."""
    unlink_durable(facts.retained / CLASSES)
    unlink_durable(facts.retained / VFKEYS)
    p = facts.retained / "retained_at"
    if not os.path.lexists(p):
        atomic_write(p, (seams.now() + "\n").encode("ascii"), 0o644)


def retain_plan(facts: EndFacts, seams: EndSeams, ri: RootIntent | None = None, *, caps: Caps = Caps(),
                started_at: str | None = None) -> Plan:
    created = ri.started_at if ri is not None else started_at
    if created is None:
        raise ValueError("retain_plan needs the journal or its started_at")

    def fields() -> dict:
        obj = canon.parse(_read_durable(facts.retained / "object.json"))
        return {"manifest": {"digest": obj["manifest_digest"], "entries": obj["entries"],  # type: ignore[index]
                             "bytes": obj["bytes"]}}                                     # type: ignore[index]

    return Plan("retain", (
        Phase("init", lambda: _init_step(facts, seams)),
        Phase("manifest", lambda: _manifest_step(facts, seams, caps)),
        Phase("meta", lambda: _meta_step(facts, seams, caps)),
        Phase("object", lambda: _object_step(facts, created)),
        Phase("tree", lambda: _tree_step(facts, seams)),
        Phase("commit", lambda: _commit_step(facts, seams)),
    ), fields)


# --- park (the credential, plan §2e 11, §2f 7) -----------------------------------------------------


def _open_subdir(base: Path, parts: tuple[bytes, ...]) -> int | None:
    """``base`` then each part, ``O_NOFOLLOW`` from its parent's fd; ``None`` when a part is absent or
    not a real directory (a slot-made link is never followed)."""
    fd = os.open(base, _DIR_FLAGS)
    for part in parts:
        try:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                return None
            raise
        os.close(fd)
        fd = nfd
    return fd


def confirmed_relogin(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> bool:
    """IG-1: whether this session is a ``login`` whose ``confirm-identity yes`` was given. Root's facts only: alloc's
    kind ``login`` on a session that parks, and root's baseline file (a regular file, not followed, the expected owner,
    at most :data:`BASELINE_MAX_BYTES`) passing :func:`levain.slot.identity.check_baseline` whole and naming THIS
    session and vendor. ``confirm-identity yes`` writes it under ``L/alloc/<s>.lock``, the lock END holds, and only
    while the record is running, so no yes can land while END reads it. Anything else is False."""
    if facts.kind != "login" or not facts.parks:
        return False
    # identity imports runtime_helper, which imports this module: a top-level import here would be a cycle.
    from levain.slot import identity as ID
    try:
        fd = os.open(facts.slot.slot_dir(facts.store) / ID.BASELINE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not owner_ok(st) or st.st_size > BASELINE_MAX_BYTES:
            return False
        data = os.read(fd, BASELINE_MAX_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        obj = ID.check_baseline(canon.parse(data))
    except (ContractError, Refused):
        return False
    return obj["session"] == facts.session and obj["vendor"] == facts.vendor


def _own_dir(path: Path, owner_ok: Callable[[os.stat_result], bool], fault: Fault, point: str) -> None:
    """``path`` made root 0700 or, when it exists, checked a real directory with the expected owner (a planted link is
    never followed) and chmodded 0700; then its parent fsynced, made in this run or not, since a run that died between
    the mkdir and that fsync left it owed. Any failure is ``bad-root``, the step HELD."""
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or not owner_ok(st):
                raise Refused("bad-root", f"{path} is not a directory with the expected owner") from None
            os.chmod(path, 0o700)   # a real directory, checked above
        fault(point)
        fsync_dir(path.parent)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror or e}") from None


def _resync(facts: EndFacts, dfd: int, owner_ok: Callable[[os.stat_result], bool], fault: Fault) -> None:
    """Every directory an earlier run of :func:`park_move` may have renamed into or out of, or made, without the fsync
    that followed: the credential's tree directory, ``keep/`` and its parent, and each of ``replaced/`` and
    ``unconfirmed/`` that exists (checked as :func:`_own_dir` checks it, with ``retained/<s>/`` and its parent).
    Fsyncs are idempotent, so every run does them, a first run as well as a redo, before any credential moves."""
    os.fsync(dfd)
    try:
        kst = os.lstat(facts.keep)
    except FileNotFoundError:
        kst = None
    except OSError as e:
        raise Refused("bad-root", f"{facts.keep}: {e.strerror or e}") from None
    if kst is not None:
        if not stat.S_ISDIR(kst.st_mode):
            raise Refused("bad-root", f"{facts.keep} is not a real directory")
        try:
            fsync_dir(facts.keep.parent)
            fsync_dir(facts.keep)
        except OSError as e:
            raise Refused("bad-root", f"{facts.keep}: {e.strerror or e}") from None
    for dest in (facts.replaced, facts.unconfirmed):
        if os.path.lexists(dest):
            _own_dir(facts.retained, owner_ok, fault, "park:resync:retained")
            _own_dir(dest, owner_ok, fault, "park:resync:dest")
            fsync_dir(dest)


def _set_aside(facts: EndFacts, sfd: int, src: Path, name: bytes, dest: Path, occupied: str,
               owner_ok: Callable[[os.stat_result], bool], fault: Fault, point: str) -> None:
    """IG-1: a credential renamed (never opened) from the directory ``sfd`` into ``dest``, a directory inside
    ``L/retained/<s>/`` (both made root 0700 if missing), then both directories fsynced: the parked credential a
    confirmed re-login replaces (``keep/`` -> ``replaced/``), or the new credential of a login with no yes (the
    session tree -> ``unconfirmed/``, D1); ``src`` is the directory ``sfd`` is open on, named in a refusal.
    Only a regular file is moved (``lstat``, never followed): anything else
    refuses ``keep-occupied``, as before IG-1. An entry already at ``dest`` is never replaced (``occupied``)."""
    st = os.stat(name, dir_fd=sfd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise Refused("keep-occupied", f"{src}/{name.decode('ascii')} already holds a credential")
    _own_dir(facts.retained, owner_ok, fault, f"{point}:retained-made")
    _own_dir(dest, owner_ok, fault, f"{point}:dest-made")
    fault(f"{point}:dirs")
    rfd = os.open(dest, _DIR_FLAGS)
    try:
        try:
            os.stat(name, dir_fd=rfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused(occupied, f"{dest}/{name.decode('ascii')} already holds a file")
        os.rename(name, name, src_dir_fd=sfd, dst_dir_fd=rfd)
        fault(f"{point}:renamed")
        os.fsync(sfd)
        fault(f"{point}:source-synced")
        os.fsync(rfd)
    finally:
        os.close(rfd)
    fault(f"{point}:aside")


# --- the identity seed (identseed, np-8645041e (a)) -------------------------------------------------------------------
# The claim string stays "local-data" (identity_child.CLAIM). The parked record is a LABEL: it names the account the
# login recorded; it does not tell which account the credential belongs to, and it does not see an expired login.

def _utf8(s: str) -> bool:
    """``s`` encodes as UTF-8 (no lone surrogate: ``json.loads`` admits ``"\\ud800"``, the record's canonical bytes
    cannot hold it)."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_seed_block(block: object) -> dict[str, object] | None:
    """The whitelisted account block (:data:`IDENTITY_SEED_KEYS` only) when ``block`` is a dict whose
    ``organizationUuid`` is a UUID string, whose ``emailAddress`` is a string of 1-320 characters holding ``@`` with no
    NUL and no edge whitespace (the identity child's own email rule), and whose ``organizationName``, when present, is
    a string of at most 256 characters with no NUL, or null; the email and the name encode as UTF-8 (identfix F-A: a
    lone surrogate the slot wrote would otherwise pass here and fail the record's encoding at park); else None. Every
    other key is dropped, never checked."""
    if type(block) is not dict:
        return None
    org, email = block.get("organizationUuid"), block.get("emailAddress")
    if not (type(org) is str and _SEED_UUID_RE.fullmatch(org)):    # ASCII by the pattern: no surrogate passes
        return None
    if not (type(email) is str and 0 < len(email) <= 320 and "@" in email and "\0" not in email
            and email.strip() == email and _utf8(email)):
        return None
    out: dict[str, object] = {"emailAddress": email, "organizationUuid": org}
    if "organizationName" in block:
        name = block["organizationName"]
        if not (name is None or (type(name) is str and len(name) <= 256 and "\0" not in name and _utf8(name))):
            return None
        out["organizationName"] = name
    return out


def check_identity_seed(obj: object) -> dict[str, object]:
    """A :data:`IDENTITY_SEED_FORMAT` record checked whole (exact keys; vendor; the login session; the block exactly
    what :func:`check_seed_block` keeps), else ``bad-identity-seed``."""
    if type(obj) is not dict or set(obj) != {"format", "vendor", "session", "account"} \
            or obj["format"] != IDENTITY_SEED_FORMAT:
        raise Refused("bad-identity-seed", "keys or format")
    if type(obj["vendor"]) is not str or obj["vendor"] not in IDENTITY_SEED_SOURCE:
        raise Refused("bad-identity-seed", "vendor")
    if type(obj["session"]) is not str or not SESSION_RE.fullmatch(obj["session"]):
        raise Refused("bad-identity-seed", "session")
    block = check_seed_block(obj["account"])
    if block is None or block != obj["account"]:
        raise Refused("bad-identity-seed", "account")
    return dict(obj)


def identity_seed_bytes(*, vendor: str, session: str, account: Mapping[str, object]) -> bytes:
    obj = {"format": IDENTITY_SEED_FORMAT, "vendor": vendor, "session": session, "account": dict(account)}
    check_identity_seed(obj)
    return canon.canonical_bytes(obj)


def seed_from_tree(dfd: int, name: bytes, key: str, slot_uid: int,
                   owner_ok: "Callable[[os.stat_result], bool] | None" = None) -> dict[str, object] | None:
    """The whitelisted block of the session's own config file ``name`` below ``dfd`` (the session's vendor config dir),
    or None. Read by root at park time, after DEAD (no process of the slot is alive to race it), and only a file that
    is regular, has one link and is owned by the SLOT uid (a link to a file of anyone else's is never read), at most
    :data:`IDENTITY_SEED_SOURCE_MAX` bytes, valid JSON with ``key`` passing :func:`check_seed_block`. The content is
    the slot's: it is parsed as untrusted data and only the three whitelisted strings leave this function."""
    try:
        fd = os.open(name, _O_ENTRY, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        owned = owner_ok(st) if owner_ok is not None else st.st_uid == slot_uid
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or not owned or st.st_size > IDENTITY_SEED_SOURCE_MAX:
            return None
        buf = bytearray()
        while len(buf) <= IDENTITY_SEED_SOURCE_MAX:
            chunk = os.read(fd, min(1 << 16, IDENTITY_SEED_SOURCE_MAX + 1 - len(buf)))
            if not chunk:
                break
            buf += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(buf) > IDENTITY_SEED_SOURCE_MAX:
        return None
    try:
        obj = json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return check_seed_block(obj.get(key)) if type(obj) is dict else None


def _park_seed(facts: EndFacts, dfd: int, kfd: int, fault: Fault,
               seed_owner_ok: "Callable[[os.stat_result], bool] | None" = None) -> None:
    """A ``login`` session's park, just before its credential is renamed into ``keep/``: ``keep/``'s seed record made
    to describe THIS login. The whitelisted block from the session's config (:func:`seed_from_tree`) written root
    0600 by temp + fsync + rename + directory fsync; with no usable block, any earlier record is unlinked, so a
    record never outlives the credential it was parked with (a later session then refuses ``identity-seed-missing``
    and asks for a new login). Idempotent: a redo writes the same bytes again."""
    src = IDENTITY_SEED_SOURCE.get(facts.vendor) if facts.kind == "login" else None
    if src is None:
        return
    _, cfg_name, key = src
    block = seed_from_tree(dfd, cfg_name, key, facts.slot_uid, seed_owner_ok)
    try:
        os.unlink(_SEED_TMP, dir_fd=kfd)
    except FileNotFoundError:
        pass
    if block is None:
        try:
            os.unlink(IDENTITY_SEED_NAME, dir_fd=kfd)
        except FileNotFoundError:
            pass
        os.fsync(kfd)
        fault("park:seed:dropped")
        return
    data = identity_seed_bytes(vendor=facts.vendor, session=facts.session, account=block)
    fd = os.open(_SEED_TMP, _O_NEW, 0o600, dir_fd=kfd)
    try:
        os.fchmod(fd, 0o600)
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(_SEED_TMP, IDENTITY_SEED_NAME, src_dir_fd=kfd, dst_dir_fd=kfd)
    os.fsync(kfd)
    fault("park:seed:written")


def park_move(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned,
              fault: Fault = _no_fault, seed_owner_ok: "Callable[[os.stat_result], bool] | None" = None) -> None:
    """Move the session vendor's credential file to ``keep/`` by rename, never opening the credential, on a
    passthrough session only (L1-F1): on any other, a file at a credential path is ordinary work, retained.
    A ``login`` session's park first opens and reads one other file as root, the session's vendor config
    (:func:`_park_seed` -> :func:`seed_from_tree`: no-follow, regular, one link, slot-owned, size-capped, parsed as
    untrusted data), and writes or unlinks ``keep/``'s account record from it, after any set-aside and BEFORE the credential
    rename: a death between the two leaves the new record in ``keep/`` and the new credential still in the session
    tree, and the redo writes the same record again and parks.
    Only a non-directory is moved; absent = already parked or never made. A ``keep/`` entry already there
    is never replaced (``keep-occupied``, HELD park), so a planted file cannot overwrite a parked login;
    ``keep/`` is root-only, so the check and the rename cannot race. The one exception is a
    :func:`confirmed_relogin` (IG-1): the parked entry is first set aside into ``replaced/`` (:func:`_set_aside`),
    then the new login is parked. In that order, a death at any point leaves the new login in the session tree or
    in ``keep/`` (the old one in ``keep/`` or ``replaced/``); a redo after the set-aside finds ``keep/`` empty and
    parks, and one after the park finds the source gone. A ``login`` session that is not confirmed (D1), with
    ``keep/`` already occupied, parks nothing: its new credential is set aside into ``unconfirmed/`` and ``keep/``
    is not touched, so the session still retains and its slot is released; with ``keep/`` empty it parks as any
    first login does. Only another kind refuses ``keep-occupied``.
    keep/ (root 0700, install's) is made if it is missing (DECISION), its parent fsynced. Every run first fsyncs
    every directory an earlier run may have left unsynced (:func:`_resync`), so a redo finishes the durability steps
    the run it replays owed (codex r1 6, r1 G2 H1 + H2)."""
    cred = facts.credential
    if cred is None or not os.path.isdir(facts.live):
        return
    parts, name = cred
    dfd = _open_subdir(facts.live, parts)
    if dfd is None:
        return
    try:
        _resync(facts, dfd, owner_ok, fault)
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            return
        try:
            os.mkdir(facts.keep, 0o700)
        except FileExistsError:
            pass
        fault("park:keep:made")
        fsync_dir(facts.keep.parent)
        kfd = os.open(facts.keep, _DIR_FLAGS)
        try:
            try:
                os.stat(name, dir_fd=kfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if confirmed_relogin(facts, owner_ok=owner_ok):
                    _set_aside(facts, kfd, facts.keep, name, facts.replaced, "replaced-occupied", owner_ok, fault,
                               "park:replace")
                elif facts.kind == "login":
                    tree_dir = facts.live.joinpath(*(x.decode("ascii") for x in parts))
                    _set_aside(facts, dfd, tree_dir, name, facts.unconfirmed, "unconfirmed-occupied", owner_ok, fault,
                               "park:unconfirmed")
                    return
                else:
                    raise Refused("keep-occupied",
                                  f"{facts.keep}/{name.decode('ascii')} already holds a credential")
            # identseed: the seed record first, then the credential; a death between them leaves the credential in
            # the tree, and the redo writes the same record again before it parks.
            _park_seed(facts, dfd, kfd, fault, seed_owner_ok)
            os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=kfd)
            fault("park:keep:renamed")
            os.fsync(dfd)
            fault("park:keep:source-synced")
            os.fsync(kfd)
        finally:
            os.close(kfd)
    finally:
        os.close(dfd)


def park_plan(facts: EndFacts, seams: "EndSeams | None" = None) -> Plan:
    # DECISION: park's `commit` is its CAS; the step itself re-syncs keep/ so a redo is durable.
    owner_ok = seams.owner_ok if seams is not None else _root_owned
    fault = seams.fault if seams is not None else _no_fault
    seed_ok = seams.seed_owner_ok if seams is not None else None
    return Plan("park", (Phase("move", lambda: park_move(facts, owner_ok=owner_ok, fault=fault,
                                                         seed_owner_ok=seed_ok)),
                         Phase("commit", lambda: fsync_dir(facts.keep) if os.path.isdir(facts.keep) else None)))


def keep_shape_ok(keep: Path, name: str, slot_uid: int, *, cap: int = V14_BYTES) -> tuple[bool, str]:
    """The return path's shape check (plan §2f 7, V14): ``lstat`` = a regular file, one link, owned by
    the slot uid, at most ``cap`` bytes. Any other shape is deleted by the caller, never moved."""
    try:
        st = os.lstat(Path(keep) / name)
    except FileNotFoundError:
        return False, "absent"
    if not stat.S_ISREG(st.st_mode):
        return False, "not a regular file"
    if st.st_nlink != 1:
        return False, f"{st.st_nlink} links"
    if st.st_uid != slot_uid:
        return False, f"owner {st.st_uid}, not the slot's {slot_uid}"
    if st.st_size > cap:
        return False, f"{st.st_size} bytes > {cap}"
    return True, ""


def _shape_at(dirfd: int, name: bytes, slot_owner_ok: Callable[[os.stat_result], bool], cap: int = V14_BYTES) -> str:
    """:func:`keep_shape_ok`'s rule by ``fstatat`` on ``dirfd`` (no link followed): "" when it holds, else the reason."""
    st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_nlink != 1:
        return f"{st.st_nlink} links"
    if not slot_owner_ok(st):
        return f"owner {st.st_uid}, not the slot's"
    if st.st_size > cap:
        return f"{st.st_size} bytes > {cap}"
    return ""


def _present(dirfd: int | None, name: bytes) -> bool:
    """``name`` exists in ``dirfd`` (no link followed); any error but ENOENT is ``bad-root``."""
    if dirfd is None:
        return False
    try:
        os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise Refused("bad-root", f"{name.decode('ascii')}: {e.strerror or e}") from None
    return True


def return_move(facts: EndFacts, *, slot_owner_ok: Callable[[os.stat_result], bool] | None = None,
                owner_ok: Callable[[os.stat_result], bool] = _root_owned, fault: Fault = _no_fault) -> str:
    """G3: the session vendor's parked credential renamed (never opened) from ``keep/`` into the session tree's
    credential directory, for an interactive passthrough session. Returns ``moved`` or ``already`` (keep/ empty, the
    tree holds it: a retry; the fsyncs are redone). Refuses, with nothing moved or deleted: ``keep-shape``
    (:func:`keep_shape_ok`'s rule applied by dirfd; refused, never deleted), ``keep-return-conflict`` (both
    present), ``no-credential:<tag>`` (both absent). Refuses ``bad-root`` for ``keep/`` not a ``owner_ok``
    directory, the credential directory missing, a presence check that errors, or an error of the rename or of
    either fsync. NOT "nothing moved": an error (an ``OSError`` fault included) after the rename leaves the one copy
    in the tree, so the caller parks back on every ``bad-root`` (:func:`park_move` finds the source absent when
    nothing moved). ``slot_owner_ok`` defaults to ``st_uid == facts.slot_uid``.
    Order: rename, fsync ``keep/``, fsync the tree directory, at the faults ``return:checked``, ``return:renamed``,
    ``return:source-synced``, ``return:done``. The caller holds the session END lock. The baseline and the gate are
    the caller's."""
    if facts.kind not in ("interactive", "dispatch", "planner") or not facts.parks:
        # c7.14: a ticketed passthrough session (kind dispatch or planner) is returned its credential as an
        # interactive one is; a `login` or `verifier` session never is
        raise Refused("bad-root", "return_move: not an interactive or ticketed passthrough session of a credential "
                                  "vendor")
    if slot_owner_ok is None:
        slot_owner_ok = lambda st: st.st_uid == facts.slot_uid   # noqa: E731
    parts, name = facts.credential   # type: ignore[misc]
    tfd = _open_subdir(facts.live, parts) if os.path.isdir(facts.live) else None
    try:
        kfd: int | None = os.open(facts.keep, _DIR_FLAGS)
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            if tfd is not None:
                os.close(tfd)
            raise Refused("bad-root", f"{facts.keep}: {e.strerror or e}") from None
        kfd = None
    try:
        if kfd is not None:
            try:
                kst = os.fstat(kfd)
            except OSError as e:
                raise Refused("bad-root", f"{facts.keep}: {e.strerror or e}") from None
            if not (stat.S_ISDIR(kst.st_mode) and owner_ok(kst)):
                raise Refused("bad-root", f"{facts.keep} is not a directory with the expected owner")
        in_keep, in_tree = _present(kfd, name), _present(tfd, name)
        if in_keep and in_tree:
            raise Refused("keep-return-conflict", f"{name.decode('ascii')} is in keep/ and in the session tree")
        if not in_keep and not in_tree:
            raise Refused(f"no-credential:{facts.slot.tag}", f"{name.decode('ascii')} is in neither keep/ nor the tree")
        if tfd is None:
            raise Refused("bad-root", f"{facts.live}: the credential directory is missing")
        try:
            if in_tree:
                if kfd is not None:
                    os.fsync(kfd)
                os.fsync(tfd)
                bad = _shape_at(tfd, name, slot_owner_ok)
                if bad:
                    raise Refused("keep-shape", bad)
                return "already"
            bad = _shape_at(kfd, name, slot_owner_ok)   # type: ignore[arg-type]
            if bad:
                raise Refused("keep-shape", bad)
            fault("return:checked")
            os.rename(name, name, src_dir_fd=kfd, dst_dir_fd=tfd)
            fault("return:renamed")
            os.fsync(kfd)   # type: ignore[arg-type]
            fault("return:source-synced")
            os.fsync(tfd)
            fault("return:done")
        except OSError as e:
            raise Refused("bad-root", f"return: {e.strerror or e}") from None
        return "moved"
    finally:
        if kfd is not None:
            os.close(kfd)
        if tfd is not None:
            os.close(tfd)


# --- the reserve (CONTRACT §7.5) -------------------------------------------------------------------


@dataclass
class Reserve:
    """``L/reserve``: ``size`` (V6) bytes of WRITTEN zeros, never fallocate (APFS). A transition write
    that hits ENOSPC truncates it one ``size // 8`` step and retries; the reaper refills it above V5."""

    path: Path
    size: int = RESERVE_BYTES
    #: how long a release waits for the reaper's refill lock before it refuses (a test shortens it).
    lock_wait_s: float = SLOT_LOCK_WAIT_S

    @property
    def step(self) -> int:
        return max(1, self.size // 8)

    def current(self) -> int:
        try:
            return os.lstat(self.path).st_size
        except FileNotFoundError:
            return 0

    def release_step(self) -> int:
        """Shrink by one step; returns the bytes released (0 = nothing left to give). Never call it from a
        process holding the reaper's reserve lock: flock is per open file description, so it would wait on
        itself (now a bounded ``reserve-locked`` refusal)."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return 0
        try:
            # The reaper's refill holds a flock on this same file across its fstat, write and fsync; taking
            # it here means a release never truncates under a refill (the unlock is the close below). The
            # wait is bounded like END's other lock waits, else a stalled reaper would pin the transition.
            deadline = time.monotonic() + self.lock_wait_s
            delay = SLOT_LOCK_FIRST_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Refused("reserve-locked", f"{self.path} held past {self.lock_wait_s:g} s") from None
                    time.sleep(delay)
                    delay = min(delay * 2, SLOT_LOCK_MAX_S)
            cur = os.fstat(fd).st_size
            new = max(0, cur - self.step)
            os.ftruncate(fd, new)
            os.fsync(fd)
        finally:
            os.close(fd)
        return cur - new

    def retry(self, fn: Callable[[], None]) -> None:
        while True:
            try:
                fn()
                return
            except OSError as e:
                if e.errno != errno.ENOSPC or self.release_step() == 0:
                    raise

    def refill_headroom(self, free_bytes: Callable[[], int], *, floor: int = V5_FLOOR_BYTES) -> int:
        """Bytes a refill may write: free space less the floor and :data:`_REFILL_MARGIN`. At or below zero nothing may be
        opened or created for the reserve, since the file's own metadata is charged to the host too. The one place the
        arithmetic lives, for :meth:`refill` and the reaper's lock."""
        return free_bytes() - floor - _REFILL_MARGIN

    def refill(self, free_bytes: Callable[[], int], *, floor: int = V5_FLOOR_BYTES) -> bool:
        """Back to ``size`` with written zeros when free space is above ``floor``; returns whether it
        is full. The headroom (``free_bytes() - floor - _REFILL_MARGIN``) is read once at entry and each write is
        subtracted from it; a fresh reading only shrinks it. So the refill writes at most the entry headroom even when
        the filesystem's free-space reading lags the writes, and stops sooner when the reading drops faster. What it
        does not guarantee: space taken by other writers after entry, or by metadata beyond the margin."""
        if self.current() >= self.size:
            return True
        start = self.refill_headroom(free_bytes, floor=floor)
        if start <= 0:     # nothing is opened or created here
            return False
        created = not os.path.lexists(self.path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            cur = os.fstat(fd).st_size
            os.lseek(fd, cur, os.SEEK_SET)
            zeros = bytes(_CHUNK)
            head = min(start, self.refill_headroom(free_bytes, floor=floor))
            try:
                while cur < self.size:
                    # never write past the floor: a refill that starves the host would make the next pass BRAKE
                    head = min(head, self.refill_headroom(free_bytes, floor=floor))    # a reading may shrink it, never grow it
                    n = min(_CHUNK, self.size - cur, head)
                    if n <= 0:
                        break
                    write_all(fd, zeros[:n])
                    cur += n
                    head -= n
            except OSError as e:
                if e.errno != errno.ENOSPC:
                    raise
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:   # the new name is durable too, or a crash leaves no reserve to give (glm r1 LOW)
            fsync_dir(self.path.parent)
        return cur >= self.size


# --- root's facts beside alloc: the clone marker, the slot check, the END lock ---------------------


def cloned_path(store: Path, session: str) -> Path:
    """DECISION: ``L/alloc/<session>.cloned`` (root 0600), beside the alloc record."""
    return intent_path(Path(store) / "alloc", session).with_name(f"{session}.cloned")


def mark_cloned(store: Path, session: str, *, now: Callable[[], str] = utc_now) -> None:
    """``start``'s root fact that the clone ran (L2 H1, L1-F3; §5.4 3: a failed clone refuses
    ``clone-failed``), written only after the clone succeeded. It holds the time it was written."""
    atomic_write(cloned_path(store, session), (now() + "\n").encode("ascii"), 0o600)


def repo_cloned(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> bool:
    """Whether the session ever cloned: root's marker (:func:`mark_cloned`) exists. Never inferred from
    ``<S>/repo``, which the slot can empty: with the marker, whatever is there is retained, an empty repo
    included; without it the session never cloned and retains nothing (CONTRACT §7.3)."""
    try:
        st = os.lstat(facts.cloned_marker)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or not owner_ok(st):
        raise Refused("bad-root", f"{facts.cloned_marker} is not a regular file with the expected owner")
    return True


def check_slot_ours(facts: EndFacts, seams: EndSeams) -> None:
    """L2 M4, before anything is signalled: root's own slot state file (§3.5 Pool) shows this slot
    ``leased`` to THIS session at alloc's ``lease_seq``. Absent, unreadable, malformed, another session,
    another lease or not leased = ``slot-not-ours``."""
    try:
        data = seams.slot_state(facts.slot_state)
        st = parse_slot_state(data) if data is not None else None
    except (Refused, OSError) as e:
        raise Refused("slot-not-ours", f"{facts.slot_state} unreadable ({e})") from None
    if st is None:
        raise Refused("slot-not-ours", f"no {facts.slot_state}")
    if (st.slot != facts.slot or st.state != "leased" or st.session != facts.session
            or facts.lease_seq is None or st.lease_seq != facts.lease_seq):
        raise Refused("slot-not-ours", f"{facts.slot.tag} is {st.state} to {st.session} at lease {st.lease_seq}")


@contextmanager
def slot_lock(facts: EndFacts, seams: EndSeams) -> Iterator[bool]:
    """The slot's ``L/slots/{e,v}<n>/lock`` flock (§3.5), taken BEFORE :func:`end_lock` (the order pool ->
    slot -> session END lock; END never holds the pool lock), and held across the slot check, the freeze,
    kill and no-process proofs and every act after them (codex r1 2). SCRUBBED takes the same lock FIRST,
    re-checks the lease under it and only TRIES the pool lock after its kills (a lost TRY releases the slot lock and
    leaves the slot leased), never pool -> slot: a SCRUB waiting on the slot lock while holding the pool lock would
    stall every ``prepare`` behind END (reaper DESIGN Q2). So a slot END proved ours cannot be released and re-leased
    while END signals its uids. prepare only TRIES it under the pool lock, so a slot END holds is passed over
    and prepare never waits on END. Waited for up to :data:`SLOT_LOCK_WAIT_S`, else ``slot-locked`` (raised,
    never HELD: the caller retries). Yields whether the slot exists: DECISION no slot directory means no slot
    state either, so END lands ``slot-not-ours`` with nothing signalled. A lock file missing from a slot
    directory is made (root 0600, the file install makes)."""
    path = facts.slot_lock
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileNotFoundError:
        yield False
        return
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    _LOCK_FDS.add(fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        deadline = None
        delay = SLOT_LOCK_FIRST_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = seams.clock()
                deadline = now + SLOT_LOCK_WAIT_S if deadline is None else deadline
                if now >= deadline:
                    raise Refused("slot-locked", f"{facts.slot.tag} held past {SLOT_LOCK_WAIT_S:g} s") from None
                seams.sleep(delay)
                delay = min(delay * 2, SLOT_LOCK_MAX_S)
        yield True
    finally:
        _LOCK_FDS.discard(fd)
        os.close(fd)   # closing the only descriptor releases the flock


def retained_whole(facts: EndFacts, seams: EndSeams) -> bool:
    """Whether root's OWN retained object for the session exists and is whole: ``L/retained/<s>/`` a real
    directory, ``tree/`` a real directory, ``object.json`` a regular file that parses as THIS session's
    ``levain-retained-object/1``, ``retained_at`` a regular file holding one contract timestamp, each with
    the expected owner (root). Only this lets END take a RETAINED record at its word (codex r1 1)."""
    r = facts.retained
    try:
        for p, ok in ((r, stat.S_ISDIR), (r / "tree", stat.S_ISDIR), (r / "object.json", stat.S_ISREG),
                      (r / "retained_at", stat.S_ISREG)):
            st = os.lstat(p)
            if not ok(st.st_mode) or not seams.owner_ok(st):
                return False
        obj = canon.parse(_read_durable(r / "object.json"))
        stamp = _read_durable(r / "retained_at")
    except (OSError, ContractError):
        return False
    return (isinstance(obj, dict) and obj.get("format") == OBJECT_FORMAT and obj.get("session") == facts.session
            and stamp.endswith(b"\n") and TIMESTAMP_RE.fullmatch(stamp[:-1].decode("ascii", "replace")) is not None)


@contextmanager
def end_lock(facts: EndFacts, seams: EndSeams) -> Iterator[None]:
    """Root's per-session lock (L2 M7): one END or reconciliation per session at a time. Waited for up to
    :data:`END_LOCK_WAIT_S`, else ``end-busy`` with the record UNCHANGED, never HELD (the reaper's next pass or the
    helper retries): ``start`` holds this lock across every spawn and every kill it makes, so an END that waits here
    cannot prove while a ``start`` is still launching or abandoning in the slot (reaper DESIGN RH1, Q-F1). Its
    holder may also be a second END of the session, which a timed-out END must not write over. The open is
    ``O_CLOEXEC``, so no exec'd child keeps a copy of it. Under it, a dead writer's temp files of what END writes
    (the journal's ``L/alloc/.<s>.intent.json.*.tmp``, ``L/retained/<s>/.*.tmp``) are swept: no live writer
    can own one. ``start``'s clone-marker temp is not END's to sweep."""
    path = facts.lock
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    _LOCK_FDS.add(fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        deadline = None
        delay = SLOT_LOCK_FIRST_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = seams.clock()
                deadline = now + END_LOCK_WAIT_S if deadline is None else deadline
                if now >= deadline:
                    raise Refused("end-busy", f"{facts.session} held past {END_LOCK_WAIT_S:g} s") from None
                seams.sleep(delay)
                delay = min(delay * 2, SLOT_LOCK_MAX_S)
        sweep_temps(facts.alloc_dir, facts.intent.name, owner_ok=seams.owner_ok)
        if os.path.isdir(facts.retained) and not os.path.islink(facts.retained):
            sweep_temps(facts.retained, "", owner_ok=seams.owner_ok)
        yield
    finally:
        _LOCK_FDS.discard(fd)
        os.close(fd)   # closing the only descriptor releases the flock


# --- the END driver --------------------------------------------------------------------------------

END_STEPS = ("freeze", "kill", "park", "retain")
_START = {REVOKED: 0, FROZEN: 1, DEAD: 2, CREDENTIAL_PARKED: 3}
_RESUME = {"slot-not-ours": 0, "freeze": 0, "kill": 1, "park": 2, "manifest_budget": 3,
           **{p: 3 for p in INTENT_PHASES["retain"]}}


#: The retain phases that run before root's first act on the tree: only a budget refusal raised in one of them
#: is ``manifest_budget``, whose exit (END again with larger caps) assumes the tree as the session left it.
_BUDGET_PHASES = frozenset({"manifest", "meta"})


def _held_name(intent: str, e: PhaseFailed) -> str:
    if intent == "park":
        return "park"
    if isinstance(e.cause, Refused) and e.cause.name in BUDGET_REFUSALS and e.phase in _BUDGET_PHASES:
        return "manifest_budget"
    return e.phase


def _plan_for(facts: EndFacts, seams: EndSeams, caps: Caps) -> Callable[[RootIntent], Plan]:
    def plan_for(ri: RootIntent) -> Plan:
        return retain_plan(facts, seams, ri, caps=caps) if ri.intent == "retain" else park_plan(facts, seams)
    return plan_for


def _reconcile(facts: EndFacts, seams: EndSeams, caps: Caps) -> RootIntent | None:
    return reconcile(facts.intent, _plan_for(facts, seams, caps), cas=lambda t, f: send_cas(seams, t, f),
                     fault=seams.fault, retry=seams.retry, owner_ok=seams.owner_ok)


#: Per intent, the journal phases from which reconciling will run a step that reads or changes the session's
#: ``live/``: a journal at one of them is finished only after :func:`reconcile_session` proves the slot ours
#: and every session uid dead again (comp r1 3). The reconciler runs every LATER phase too, so the test is
#: the phases still to run, not the one named: a retain ``init`` journal goes on to the manifest and the
#: tree (codex r2 O3), and park's ``move`` renames the credential out of ``live/`` (comp r2 L2). Only
#: ``commit`` is left out (retain's writes root's ``retained_at``, park's syncs ``keep/``; both then CAS):
#: a crash between the CAS and the journal delete leaves a ``commit`` journal that SCRUBBED may already
#: have outlived, and finishing it must not need a slot that is no longer the session's.
PROVEN_PHASES: Mapping[str, tuple[str, ...]] = {
    "retain": tuple(p for p in INTENT_PHASES["retain"] if p != "commit"),
    "park": tuple(p for p in INTENT_PHASES["park"] if p != "commit"),
}


def reconcile_session(facts: EndFacts, seams: EndSeams, *, caps: Caps = Caps(),
                      held_attempts: int | None = None) -> RootIntent | None:
    """The reaper's step 2 / helper start for one session: finish its journal, from the journal and
    the filesystem only, under the slot lock then root's per-session lock (:func:`slot_lock`). A journal
    at one of its intent's :data:`PROVEN_PHASES` is finished only after the proofs END runs (the slot ours,
    then freeze, kill and no process for EVERY session uid): the journal outlives END's own proofs (HELD
    at ``tree``, a later pass), and a process of a session uid could have appeared since (comp r1 3). A proof
    that fails CASes HELD with that step as ``failed_step`` (``held_attempts`` one past the record's), as
    :func:`end_session` does, and raises its refusal: nothing is reconciled. Raises :class:`PhaseFailed`
    (journal left at the phase), ``Refused`` (``end-busy`` and ``slot-locked`` included) or
    :class:`CasNotSent`."""
    with slot_lock(facts, seams) as locked, end_lock(facts, seams):
        ri = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if ri is not None and ri.phase in PROVEN_PHASES.get(ri.intent, ()):
            step = "slot-not-ours"
            try:
                if not locked:
                    raise Refused("slot-not-ours", f"no {facts.slot.slot_dir(facts.store)}")
                check_slot_ours(facts, seams)
                step = "freeze"
                _prove_frozen(facts, seams)     # best-effort (H1): the kill and its proof are the test
                step = "kill"
                _prove_dead(facts, seams)
            except Exception as e:   # noqa: BLE001 - an unproven step is HELD
                seams.note(f"reconcile {facts.session}: {step} not proven: {e}")
                attempts = held_attempts if isinstance(held_attempts, int) and held_attempts >= 0 else 0
                send_cas(seams, HELD, {"failed_step": step, "held_attempts": attempts + 1,
                                       "held_reason": held_reason_of(e)})
                raise
        try:
            return _reconcile(facts, seams, caps)
        except PhaseFailed as e:
            if isinstance(e.cause, CasNotSent):
                raise e.cause from None
            raise


def _prove(ok: Callable[[], bool], what: str, seams: EndSeams, bound: float | None) -> None:
    t0 = seams.clock()
    if not ok():
        raise Refused(f"{what}-unproven", f"the {what} seam did not prove it")
    if bound is not None and seams.clock() - t0 > bound:
        raise Refused(f"{what}-unproven", f"past the {bound:g} s bound")


def _prove_frozen(facts: EndFacts, seams: EndSeams) -> bool:
    """The freeze of every session uid, BEST-EFFORT (reaper H1): each bounded to a quarter of V12, and whatever it
    answers the kill runs next. Processes of a uid can resume each other (``SIGCONT`` from the uid itself), so a
    freeze that never proves must not be what keeps them alive; the kill and the no-process proof are END's test.
    Returns whether every freeze proved; a freeze that did not is noted."""
    bound = V12_SECONDS / 4
    ok = True
    for uid in facts.uids:
        try:
            _prove(lambda u=uid: seams.freeze(u, bound), "freeze", seams, bound)
        except Exception as e:   # noqa: BLE001 - the kill runs whatever the freeze says
            seams.note(f"END {facts.session}: the freeze of uid {uid} did not prove ({e}); the kill runs")
            ok = False
    return ok


def _prove_dead(facts: EndFacts, seams: EndSeams) -> None:
    for uid in facts.uids:
        _prove(lambda u=uid: seams.kill(u, V12_SECONDS), "kill", seams, V12_SECONDS)
    for uid in facts.uids:
        _prove(lambda u=uid: seams.no_process(u), "kill", seams, None)


def end_session(facts: EndFacts, seams: EndSeams, *, record_state: str, failed_step: str | None = None,
                held_attempts: int | None = None, caps: Caps = Caps()) -> str:
    """Drive one session through END from ``record_state`` (and ``failed_step`` for HELD); returns the
    state it leaves the record in (RETAINED or HELD). Every proof and idempotent act runs every call;
    the record's state only chooses which CAS edges are sent (see the module docstring). A landing in
    HELD sends ``held_attempts`` one past the record's (§7.3: the reaper's backoff and doctor read it).
    Raises ``end-busy`` (another END or reconciliation holds the session), ``slot-locked`` and
    :class:`CasNotSent`. A RETAINED record is taken at its word only with root's own retained object whole
    (:func:`retained_whole`); otherwise END starts as from REVOKED (codex r1 1): the proofs run, and CONTROL
    refuses the FROZEN edge out of RETAINED (:class:`CasNotSent`), so teardown never reaches SCRUBBED."""
    if record_state == RETAINED:
        if retained_whole(facts, seams):
            return RETAINED
        seams.note(f"END {facts.session}: the record says RETAINED and root holds no whole retained object")
        start: int | None = _START[REVOKED]
    else:
        start = _RESUME.get(failed_step or "") if record_state == HELD else _START.get(record_state)
    if start is None:
        raise Refused("not-ending", f"{record_state} / {failed_step}")
    attempts = held_attempts if isinstance(held_attempts, int) and held_attempts >= 0 else 0
    with slot_lock(facts, seams) as locked, end_lock(facts, seams):
        return _end_locked(facts, seams, start, attempts + 1, caps, locked)


def _end_locked(facts: EndFacts, seams: EndSeams, start: int, attempts: int, caps: Caps, locked: bool) -> str:
    def cas(target: str, fields: Mapping[str, object]) -> None:
        send_cas(seams, target, fields)

    step = "slot-not-ours"
    try:
        if not locked:
            raise Refused("slot-not-ours", f"no {facts.slot.slot_dir(facts.store)}")
        check_slot_ours(facts, seams)
        step = "freeze"
        frozen = _prove_frozen(facts, seams)
        if frozen and start <= 0:
            cas(FROZEN, {})
        # H1: an unproven freeze does not stop the kill. Until FROZEN is sent a failure is still named "freeze" (the
        # retry resumes from the FROZEN edge); once the kill and its proof pass, every process of the session is gone,
        # which also answers what FROZEN records, so the edge goes then, before DEAD.
        step = "kill" if frozen else "freeze"
        _prove_dead(facts, seams)
        if not frozen and start <= 0:
            cas(FROZEN, {})
        step = "kill"
        if start <= 1:
            cas(DEAD, {})

        step = "park"
        pending = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if pending is not None:
            try:
                _reconcile(facts, seams, caps)
            except PhaseFailed as e:
                step = _held_name(pending.intent, e)
                raise
            if pending.intent == "retain":
                return RETAINED
        if pending is None and start <= 2:
            begin(facts.intent, park_plan(facts, seams), started_at=seams.now(), cas=cas,
                  fault=seams.fault, retry=seams.retry)
        elif start > 2:
            # idempotent; no journal and no CAS: the record is already past it
            park_move(facts, owner_ok=seams.owner_ok, fault=seams.fault)

        step = "init"
        if os.path.lexists(facts.tree):
            # A tree and no journal = a retain that finished (its CAS precedes the journal delete):
            # the filesystem says RETAINED, so that is the edge sent, never a second retain.
            cas(RETAINED, dict(retain_plan(facts, seams, caps=caps, started_at=seams.now()).fields()))
            return RETAINED
        if not repo_cloned(facts, owner_ok=seams.owner_ok):
            cas(RETAINED, {})
            return RETAINED
        started = seams.now()
        try:
            begin(facts.intent, retain_plan(facts, seams, caps=caps, started_at=started),
                  started_at=started, cas=cas, fault=seams.fault, retry=seams.retry)
        except PhaseFailed as e:
            step = _held_name("retain", e)
            raise
        return RETAINED
    except CasNotSent:
        raise
    except PhaseFailed as e:
        if isinstance(e.cause, CasNotSent):
            raise e.cause from None
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held, reason = step, held_reason_of(e)
    except Exception as e:   # noqa: BLE001 - an unproven step is HELD, never a crash past the tree
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held, reason = step, held_reason_of(e)
    # ``held_reason`` None (an OSError, an unnamed fault) clears a reason an earlier try left, so a stale permanent
    # reason never outlives a transient failure.
    cas(HELD, {"failed_step": held, "held_attempts": attempts, "held_reason": reason})
    return HELD


__all__ = ["BUDGET_REFUSALS", "NOT_AN_ATTEMPT", "PERMANENT_HELD", "PERMANENT_HELD_PREFIXES", "QUARANTINE_AFTER",
           "QUARANTINE_AFTER_BUDGET", "TRANSIENT_HELD", "held_reason_of", "quarantine_after", "quarantined",
           "CAS_RETRY_SECONDS", "CLASSES", "CREDENTIALS", "CREDENTIAL_FILES", "CREDENTIAL_PARKED",
           "IDENTITY_SEED_FORMAT", "IDENTITY_SEED_KEYS", "IDENTITY_SEED_MAX_BYTES", "IDENTITY_SEED_NAME",
           "IDENTITY_SEED_SOURCE", "check_identity_seed", "check_seed_block", "identity_seed_bytes", "seed_from_tree",
           "Caps", "HASH_ONLY_CAP", "LINK_UNREADABLE", "OUTSIDE", "PROVEN_PHASES", "SLOT_LOCK_WAIT_S", "UNOWNED",
           "UNREAD_ANSWERS", "WALK_OPEN_DIRS", "WALK_PEAK_FDS", "classify", "read_classes", "retained_whole",
           "stored_classification", "session_reader", "slot_lock", "sync_tree",
           "CasNotSent", "DEAD", "END_STEPS", "EndFacts", "EndSeams", "Entry", "FROZEN", "HELD", "LINKCOPY",
           "LINK_COPY_BYTES", "MANIFEST_FORMAT", "META_FORMAT", "OBJECT_FORMAT", "REVOKED", "RETAINED", "Reserve",
           "UNREAD", "V12_SECONDS", "V14_BYTES", "V17_MANIFEST_BYTES", "V17_META_BYTES", "V1_INODES", "V2_SECONDS",
           "V5_FLOOR_BYTES", "VAR_FOLDERS_NAME", "VAR_FOLDERS_SKELETON", "VFCOPY", "VFKEYS",
           "prove_var_folders_skeleton", "var_folders_preflight", "check_slot_ours", "cloned_path", "darwin_meta", "darwin_strip",
           "default_meta_reader", "default_strip", "digest_bytes", "end_lock", "end_session", "entry_type",
           "facts_from_alloc", "foreign_links", "fork_run_as", "iter_manifest", "iter_meta", "keep_shape_ok",
           "linux_meta", "linux_strip", "mark_cloned", "object_record", "os_chown", "outside_link_bytes",
           "REPLACED", "UNCONFIRMED", "confirmed_relogin", "park_move", "park_plan", "read_as_session", "read_slot_state",
           "reconcile_session", "repo_cloned",
           "retain_plan", "return_move", "send_cas", "tree_manifest", "unbuilt_freeze", "unbuilt_kill", "unbuilt_no_process",
           "utc_now", "walk", "ALLOW_CAP", "ALLOW_REL", "allow_provenance", "session_owner"]
