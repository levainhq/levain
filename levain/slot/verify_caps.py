"""V30's verifier caps that BOTH sides of adopt-verify read (S1 session 5 DESIGN §6.3): the confined child
(:mod:`levain.slot.adopt_verify`, :mod:`levain.slot.fsck_reader`) enforces them, and root
(:mod:`levain.slot.runtime_helper`) sizes the verify image and its publish from them. One module, so the two can never
hold different numbers (head ruling 1022+20, adopt-s2fix Q3 revised).

The expanded-output rule (§6.3: every git output byte-capped, 256 MiB = ``preview-cap:output``) has two values here:
:data:`TREE_SCRATCH_CAP`, the delta closure's tree payloads in ``scratch/`` (checked from ``%(objectsize)`` before a
byte of them is written), and :data:`PREVIEW_OUTPUT_CAP`, ``out/preview.txt`` (root's publish cap for it). Both are
V30's one 256 MiB value [judged by s5-fold-r4, against §6.3; kept separate names so the image budget counts both].
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


def inventory_bytes(objects: int, hexlen: int) -> int:
    """The inventories' bytes for ``objects`` objects: one line per object in each (L3 s2 r2: about ``hex + 20`` bytes
    per object per file, which the fixed margin alone held only up to a few hundred thousand objects)."""
    return objects * sum(hexlen + extra for extra in INVENTORY_LINE_EXTRA.values())


__all__ = ["COMMIT_OBJECT_MAX", "EXPANDED_OUTPUT_CAP", "INVENTORY_LINE_EXTRA", "PREVIEW_OUTPUT_CAP", "TREE_SCRATCH_CAP",
           "inventory_bytes"]
