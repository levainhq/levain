"""levain.coordinator — the coordinator's records: your drops, the threads they become, and the replies to them.

Design: levain project_memory ``1020-coordinator/C0_DESIGN.md`` (rev 6, read by Phill 10-20). This package holds C1, the
drop and thread store (:mod:`levain.coordinator.store`) and its CLI (:mod:`levain.coordinator.cli`). Envelopes, launches
and the lease are later sessions (C2, C3).
"""
