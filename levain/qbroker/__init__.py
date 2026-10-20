"""levain.qbroker — the queue broker (F10): a slot's consent hook files a request and reads its answer here.

A hook inside a slot holds no ledger. It reaches this broker on ``R/qb/slot<n>/q.sock`` with the queue cap CONTROL
minted for its session (env ``LEVAIN_QUEUE_SOCKET`` + ``LEVAIN_QUEUE_CAP``). The broker learns WHO it is from
CONTROL (``QUEUE-RESOLVE``, by the listener's slot and the cap's digest), never from the hook, writes the request
row under ``L/queue/requests/``, and accepts an answer only by its signature against the answer-signer snapshot
(``L/policy/allowed_signers.json``). Placement and wire: CONTRACT c3 §2.1, §2.2, §3.2, §3.3, §3.7; rows: SCHEMAS
§4. Stdlib only.
"""
from __future__ import annotations
