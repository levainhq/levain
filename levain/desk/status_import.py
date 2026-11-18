"""RETIRED (T7, levain#101): a status markdown file's lane-menu rows are no longer imported into the v1 ready queue, which
takes no more rows. :func:`import_status` raises before it reads the file. The parser that read the rows (``parse_rows``)
had no caller left and is deleted; the board's items are made with ``levain board add``. The name stays so a caller that
still imports it gets the refusal, not an ``ImportError``."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levain.desk import ready
from levain.desk.store import CoordStore

__all__ = ["ImportResult", "default_id", "import_status"]


@dataclass
class ImportResult:
    """What an import would have reported; kept so a caller's annotation still binds. Nothing makes one now."""
    added: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    unparsed: list[int] = field(default_factory=list)


def default_id(n: int) -> str:
    """The id the retired importer gave row ``n``."""
    return f"levain:row-{n}"


def import_status(store: CoordStore, token: int, path: str | Path, *, who: str = "status-import",
                  id_for: Callable[[int], str] = default_id, repo: str = "levain") -> ImportResult:
    """RETIRED: raises :class:`levain.desk.ready.V1Retired` before the file is read; nothing is written."""
    raise ready.V1Retired(ready.RETIRED)
