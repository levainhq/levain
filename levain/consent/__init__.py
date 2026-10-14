"""levain.consent — the cross-agent consent queue (Claude Code and Codex ``PermissionRequest`` gates).

:mod:`levain.consent.record` is the request and the bytes an answer signs; :mod:`levain.consent.queue` is the
store, the state machine, expiry, ``await_decision`` and the answer check. The agent adapters (the hooks) are
not here: they file a request, wait, and deliver a verified decision or nothing.
"""
from levain.consent.queue import (
    AnswerRefused, ConsentQueue, Decision, QueueStoreError, authority_source, decide_by_policy, default_queue_dir,
)
from levain.consent.record import ANSWER_NAMESPACE, ExternalGateRequest, answer_message, answer_payload

__all__ = ["ANSWER_NAMESPACE", "AnswerRefused", "ConsentQueue", "Decision", "ExternalGateRequest", "QueueStoreError",
           "authority_source",
           "answer_message", "answer_payload", "decide_by_policy", "default_queue_dir"]
