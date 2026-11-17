"""tests/ci/LINK_INVARIANTS is the fixed list every stack link tip runs: it must stay real and keep its core."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = ("tests/test_seam_classes_census.py", "tests/test_live_marker.py",
        "tests/test_ci_root_closure.py", "tests/test_slot_egress_cards.py")


def _listed():
    out = []
    for line in (REPO / "tests" / "ci" / "LINK_INVARIANTS").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_the_list_is_non_empty_unique_and_every_file_exists():
    paths = _listed()
    assert paths
    assert len(paths) == len(set(paths)), "duplicate entries"
    assert [p for p in paths if not (REPO / p).is_file()] == []


def test_each_named_invariant_is_in_the_list():
    assert [c for c in CORE if c not in _listed()] == []
