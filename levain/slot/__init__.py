"""levain.slot — the S1 session-confinement layer (CONTRACT c7.1).

This package holds the install helper's PURE plan layer (`setup-isolation --agent`): every OS act is a
:class:`levain.firing.hands.Step` in a list a function returns, so the plan is unit-tested on any host
with no root, and a refusal is raised while planning, before the first step. The package is
``levain/slot/`` because ``levain/session.py`` already exists (SESSION1_PLAN seat ruling).
"""
from __future__ import annotations


class Refused(Exception):
    """A refusal BY NAME (CONTRACT §5): ``name`` is the stable, machine-readable reason; ``detail`` is
    for the human. Raised before any act."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``. ``os.write`` may write fewer bytes than it is given; a
    call that writes none is an error (``OSError``), never a quiet stop that leaves a short file."""
    import errno
    import os

    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, f"write made no progress with {len(view)} byte(s) left")
        view = view[n:]


__all__ = ["Refused", "write_all"]
