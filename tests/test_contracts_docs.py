"""docs/contracts.md: the generated vector list matches the vector files, and every F-item is documented with its
external spec URL and tag (S1_DESIGN §5 S1-b; the second-seat read before the freeze tag is a person's, T36)."""
import re
from pathlib import Path

from tests._contracts_vectors import BY_KIND_BEGIN, BY_KIND_END, FROZEN, render_by_kind

DOC = (Path(__file__).resolve().parent.parent / "docs" / "contracts.md").read_text(encoding="utf-8")


def test_the_vectors_by_kind_block_is_generated_from_the_vector_files():
    start, end = DOC.index(BY_KIND_BEGIN), DOC.index(BY_KIND_END) + len(BY_KIND_END)
    assert DOC[start:end] + "\n" == render_by_kind(), "regenerate: python -m tests._contracts_vectors"


def test_every_frozen_item_has_a_section():
    for n in range(1, 14):
        assert re.search(rf"^### F{n}\. ", DOC, re.M), f"F{n}"


def test_every_c2sp_spec_is_cited_at_its_tag():
    for spec in ("signed-note", "tlog-checkpoint", "tlog-cosignature", "tlog-witness"):
        assert f"`{spec}@v1.1.0`" in DOC and f"https://github.com/C2SP/C2SP/blob/{spec}/v1.1.0/{spec}.md" in DOC


def test_the_pinned_ed25519_row_is_in_the_doc():
    assert "| V | V | V | V | X | X | X | X | X | X | X  | V  |" in DOC
    assert "65519336fda78a3d016e947df6d82848aca0c9da" in DOC


def test_every_vector_directory_is_named():
    for d in sorted(p.parent.name for p in FROZEN.glob("*/vectors.json")):
        assert f"frozen/{d}/" in DOC, d


def test_no_frozen_vector_stands_on_fleet_doc_text():
    """C6 (S1_DESIGN rev 11): no frozen refusal cites the unreviewed fleet doc (FL) as its rationale."""
    import json
    for p in sorted(FROZEN.glob("*/vectors.json")):
        for v in json.loads(p.read_text(encoding="utf-8"))["vectors"]:
            assert not re.search(r"\bFL\b", json.dumps(v["authority"], ensure_ascii=False)), (p.parent.name, v["id"])


def test_c4_the_three_authority_formats_are_recorded_and_wait_on_the_trust_fold():
    """C4 (S1_DESIGN rev 11): the doc records the three-format conflict and that P1's envelope waits on its ruling."""
    sec = DOC[DOC.index("## Not frozen (provisional)"):DOC.index("## Vendoring the vectors")]
    for s in ("C4", "levain-authority/1", "sk-*", "ecdsa-sha2-nistp256", "trust-fold"):
        assert s in sec, s


def test_the_rev_11_rules_are_in_the_doc():
    for s in ("re.ASCII", "the file's head", "`known`", "every later leaf", "go-toolchain.sha256",
              "equals the anchors the vectors accept", "classify"):
        assert s in DOC, s
