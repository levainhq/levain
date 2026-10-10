"""levain.cockpit.results: what a provider returns, and what a row looks like going in.

A provider returns one of three typed results (design §3.4): ``Read`` (the source was read and
confirmed), ``Absent`` (the source does not exist), ``Fault`` (the read failed). Only a ``Read``
of zero rows can produce ``empty``, which is the point of the typing: "nothing waiting" can be
said only by a read that confirmed its source. An exception escaping a provider is a ``Fault``
(the engine converts it)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RowIn:
    """One row as a provider hands it to the kernel. The kernel assigns ``version``, ``band``,
    ``group``, ``badges``, ``emphasis`` (where it has a rule) and cuts the title; a provider never
    sorts. ``stored`` is the row's STORED fields (not derived, not clock facets): it is the input
    of ``Row.version`` and the engine refuses a stored field the provider neither versions nor
    names as excluded, so a new stored field cannot silently fall outside the version."""

    id: str
    title: str
    facets: dict[str, Any] = field(default_factory=dict)
    body: str | None = None
    emphasis: str = "none"
    stored: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Read:
    """The source was read. ``rows`` for the list kind, ``value`` for the others. ``skipped`` /
    ``filtered`` are ``(count, reason)`` pairs: skipped = rows the provider could not read
    (status ``partial``), filtered = rows excluded on purpose (status stays ok). ``stale`` is the
    provider's assertion that the CONTENT is past its own freshness window (a line nobody
    re-confirmed): the read itself succeeded, the engine renders ``stale``. ``as_of`` overrides
    the read time only when the source carries its own read-confirmation instant."""

    rows: tuple[RowIn, ...] | None = None
    value: Any = None
    skipped: tuple[tuple[int, str], ...] = ()
    filtered: tuple[tuple[int, str], ...] = ()
    stale: bool = False
    as_of: str | None = None
    note: str | None = None      # this read's own note line; None keeps the registration's ``note``
    empty: str | None = None     # this read's own sentence for status empty; None keeps the registration's


@dataclass(frozen=True)
class Absent:
    """The source does not exist. Legitimate only for a provider registered ``optional`` and only
    before the source was ever seen present; otherwise the engine renders ``error``."""

    reason: str


@dataclass(frozen=True)
class Fault:
    message: str


Result = Read | Absent | Fault
