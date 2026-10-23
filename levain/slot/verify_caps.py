"""V30's verifier caps that BOTH sides of adopt-verify read (S1 session 5 DESIGN §6.3): the confined child
(:mod:`levain.slot.adopt_verify`, :mod:`levain.slot.fsck_reader`) enforces them, and root
(:mod:`levain.slot.runtime_helper`) sizes the verify image and its publish from them. One module, so the two can never
hold different numbers (head ruling 1022+20, adopt-s2fix Q3 revised).

The expanded-output rule (§6.3: every git output byte-capped, 256 MiB = ``preview-cap:output``) has two values here:
:data:`TREE_SCRATCH_CAP`, the delta closure's tree payloads in ``scratch/`` (checked from ``%(objectsize)`` before a
byte of them is written), and :data:`PREVIEW_OUTPUT_CAP`, ``out/preview.txt`` (root's publish cap for it). Both are
V30's one 256 MiB value [judged by s5-fold-r4, against §6.3; kept separate names so the image budget counts both].
The A10 tree reader (:mod:`levain.slot.fsck_reader`) counts its tree responses against :data:`EXPANDED_OUTPUT_CAP`
itself: they are the same rule's git output, held in the verifier's memory, not written to the image, so no image term
counts them and they need no name of their own [judged by adopt5dii-fix1, 10-22, against §6.3 and L3 r1's MED].
The §6.2 preview (:mod:`levain.slot.adopt_preview`) counts every git byte IT reads against the same value, a count of
its own (the reader's is not carried over); its memory is the sections' bounded summaries plus the collision check's
one key and path per tree entry; the file it writes is held to :data:`PREVIEW_OUTPUT_CAP` [judged by adopt5dii-s2,
10-22, against §6.3: one value, one count per pass].

Each inventory's share of the image budget (:func:`inventory_share`) is what the verifier's one bounded writer
(``adopt_verify._Inventory``) holds that file to: root reserves :func:`inventory_bytes`, the shares' sum, so a file past
its share is ``preview-cap:output`` before the image can fill (L3 r1 codex MED: ``closure`` can name absent oids).
"""
from __future__ import annotations

from typing import Mapping

#: V30's expanded-output cap (§6.3).
EXPANDED_OUTPUT_CAP = 256 << 20
#: Step 4's closure tree payloads (``closure-trees``), refused ``preview-cap:output`` past it.
TREE_SCRATCH_CAP = EXPANDED_OUTPUT_CAP
#: ``out/preview.txt``, root's publish cap.
PREVIEW_OUTPUT_CAP = EXPANDED_OUTPUT_CAP
#: A commit whose probed size passes this is refused ``preview-cap:commits`` (detail ``object-size``) before any of
#: its content is read (§6.1 2a, a value beside V30's caps, E13).
COMMIT_OBJECT_MAX = 1 << 20
#: §6.3's listed-paths cap: the preview's change lists and tip trees past it = ``preview-cap:output``.
LISTED_PATHS_CAP = 1_000_000
#: §6.2 / CONTRACT §10.3: a displayed path, author, committer or message is capped at this many bytes BEFORE
#: escaping (then its length and sha256 prefix are shown).
DISPLAY_FIELD_MAX = 4 << 10
#: §6.3: each preview section shows at most this many entries, then ``<dir>/: N more`` groupings and its count.
SECTION_LINES = 200

#: The verifier's oid inventories in ``scratch/`` that are live at once (``adopt_verify``'s ``scratch / "<name>"``
#: files, the required list and its sort temp files apart: §6.3's own ``2 x objects x (hex + 1)`` term), each with the
#: bytes one line holds beyond the oid's hex. ``tests/test_slot_s5d2_reader.py`` derives the names from
#: adopt_verify's source, so a new inventory without a row here fails it.
INVENTORY_LINE_EXTRA: Mapping[str, int] = {
    "q-types": len(" commit\n"),                 # "<oid> <type>": the longest type
    "sent": len("\n"),
    "closure": len("?\n"),                       # ``--missing=print`` marks an absent oid
    "closure-present": len("\n"),
    "required-unsorted": len("\n"),
    "closure-types": len(" commit ") + 20 + len("\n"),   # "<oid> <type> <size>": a 64-bit size has <= 20 digits
    "closure-tree-oids": len("\n"),
}


def inventory_share(name: str, objects: int, hexlen: int) -> int:
    """One inventory's bytes for ``objects`` objects (both packs' header counts, root's own sum): one line per object."""
    return objects * (hexlen + INVENTORY_LINE_EXTRA[name])


def inventory_bytes(objects: int, hexlen: int) -> int:
    """The inventories' bytes for ``objects`` objects: every :func:`inventory_share` (L3 s2 r2: about ``hex + 20`` bytes
    per object per file, which the fixed margin alone held only up to a few hundred thousand objects)."""
    return sum(inventory_share(name, objects, hexlen) for name in INVENTORY_LINE_EXTRA)


__all__ = ["COMMIT_OBJECT_MAX", "DISPLAY_FIELD_MAX", "EXPANDED_OUTPUT_CAP", "INVENTORY_LINE_EXTRA", "LISTED_PATHS_CAP",
           "PREVIEW_OUTPUT_CAP", "SECTION_LINES", "TREE_SCRATCH_CAP", "inventory_bytes", "inventory_share"]
