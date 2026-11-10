"""Diogenes eb8a4744: a status row whose landed word leads the line OUTSIDE the bold title parses, and enters ``done``."""
from levain.desk import status_import

_TEXT = (
    "## LANE MENU\n"
    "149. ON MAIN c3ccbc2d (Landing 3, levain 14, 11-10 00:12; rebased: ok): **CREDS FIX** (tier: sonnet) DONE: fixed it.\n"
    "150. **A READY ROW** (tier: sonnet) the work.\n"
    "151. **ON MAIN inside the title** the old shape.\n"
    "152. ON MAIN no bold title here\n"
)


def test_the_landed_prefix_shape_parses_and_a_new_item_enters_done() -> None:
    rows, unparsed = status_import.parse_rows(_TEXT)
    by = {r.n: r for r in rows}
    assert unparsed == [152], unparsed
    assert by[149].category == "done" and by[149].title == "CREDS FIX" and by[149].tier == "sonnet", by[149]
    assert by[150].category == "ready", by[150]
    assert by[151].category == "done", by[151]
