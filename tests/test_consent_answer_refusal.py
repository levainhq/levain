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
