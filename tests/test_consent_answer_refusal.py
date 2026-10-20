"""``levain.consent.queue.answer_refusal``: the consent queue's own answer check as one module-level function, so
another store holding levain consent answers (dispatch's queue broker, F10) checks them by the same code."""
from __future__ import annotations

from types import SimpleNamespace

from levain.consent.queue import ANSWER_KEYS, answer_refusal
from tests.test_consent_queue import authority, clock, keys, queue, signed  # noqa: F401 - the shared fixtures
from tests.test_consent_queue_v4 import ask


def test_it_is_the_queue_s_own_verdict(queue, keys, authority):
    request = ask(queue)
    payload, sig = signed(queue, request, keys["phone"], "deny")
    snap = authority()
    assert set(payload) == ANSWER_KEYS
    assert answer_refusal(request, queue.queue_id, payload, sig, snap) is None
    assert queue._refusal(request, queue.queue_id, payload, sig, snap) is None
    tampered = {**payload, "decision": "allow"}
    why = answer_refusal(request, queue.queue_id, tampered, sig, snap)
    assert why == queue._refusal(request, queue.queue_id, tampered, sig, snap) == "the answer's signature does not verify"
    assert answer_refusal(request, "another-queue", payload, sig, snap) == "the answer names another queue"


def test_any_object_with_key_lookup_serves_as_authority(queue, keys):
    request = ask(queue)
    payload, sig = signed(queue, request, keys["phone"], "deny")
    k = keys["phone"]
    held = {k.fp: SimpleNamespace(public_key=k.public_key, fingerprint=k.fp, active=True, key_class="display-phone",
                                  custody="file")}
    lookup = SimpleNamespace(key=held.get)
    assert answer_refusal(request, queue.queue_id, payload, sig, lookup) is None
    assert answer_refusal(request, queue.queue_id, payload, sig, SimpleNamespace(key={}.get)) \
        == "the signer is not an enrolled key"
    held[k.fp].active = False
    assert answer_refusal(request, queue.queue_id, payload, sig, lookup) == "the signer's key is revoked"


class _Flipping(dict):
    """A mapping that says "deny" to the first reads of ``decision`` and "allow" after: what codex's L3 r1 HIGH built."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads = 0

    def __getitem__(self, k):
        if k == "decision":
            self.reads += 1
            return "deny" if self.reads <= 2 else "allow"
        return super().__getitem__(k)


def test_a_mapping_that_changes_between_reads_is_judged_on_one_copy(queue, keys, authority):
    # an agent-config (T3) request: a display-phone key may deny it, never allow it
    request = ask(queue, tier="T3")
    payload, _ = signed(queue, request, keys["phone"], "allow")
    from levain.consent import answer_message
    allow_sig = keys["phone"].sign(answer_message(payload))
    flipping = _Flipping(payload)
    flipping["decision"] = "allow"          # stored value; reads say deny, then allow
    why = answer_refusal(request, queue.queue_id, flipping, allow_sig, authority())
    assert why is not None


def test_a_record_without_decision_or_signer_is_a_refusal_not_a_crash(keys, authority):
    from levain.consent import answer_message
    from levain.consent.queue import signer_refusal
    assert signer_refusal(None, {"signer_fp": keys["phone"].fp}, "sig", authority(), noun="leave",
                          encode_record=answer_message, namespace="x") == "the leave names no decision or no signer"
