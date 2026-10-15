"""levain.consent — the cross-agent consent queue (Claude Code and Codex ``PermissionRequest`` gates).

:mod:`levain.consent.record` is the request and the bytes an answer signs; :mod:`levain.consent.queue` is the
store, the state machine, expiry, ``await_decision`` and the answer check; :mod:`levain.consent.policy` is the one
seam an adapter asks for a policy answer (a standing consent grant, :mod:`levain.consent.grants`). The agent
adapters (the hooks) are not here: they file a request, wait, and deliver a verified decision or nothing.
"""
from levain.consent.policy import PolicyNotEligible, guarded_policy
from levain.consent.queue import (
    AnswerRefused, AuthoritySource, ConsentQueue, Decision, QueueStoreError, authority_source, default_queue_dir,
)
from levain.consent.record import (
    ANSWER_NAMESPACE, GRANT_ANSWER_FORMAT, ExternalGateRequest, answer_message, answer_payload,
)

__all__ = ["ANSWER_NAMESPACE", "GRANT_ANSWER_FORMAT", "AnswerRefused", "AuthoritySource", "ConsentQueue", "Decision",
           "ExternalGateRequest", "PolicyNotEligible", "QueueStoreError", "authority_source", "answer_message",
           "answer_payload", "default_queue_dir", "guarded_policy"]
