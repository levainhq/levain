"""levain.cockpit: the shared cockpit contract, read half (design: flow
``projects/levain/reference/cockpit_manifest_DESIGN_1009.md`` §3, §6, §9 K1).

One manifest and one panel route, served by the kernel under the existing read gate. Writes,
verbs and tiers are K2a/K2b and are not here. Embedding it (the provider trust assumption and the
interrupt contract): ``docs/cockpit-engine.md``."""

from levain.cockpit.engine import (
    HEADER,
    SCHEMA,
    Cockpit,
    ProviderSpec,
    ReadContext,
    RowNotFound,
)
from levain.cockpit.registry import (
    FACETS,
    ORDERINGS,
    CockpitRegistrationError,
    Ordering,
    register_ordering,
)
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

__all__ = [
    "Absent", "Cockpit", "CockpitRegistrationError", "FACETS", "Fault", "HEADER", "ORDERINGS",
    "Ordering", "ProviderSpec", "Read", "ReadContext", "Result", "RowIn", "RowNotFound",
    "SCHEMA", "register_ordering",
]
