"""The one ``levain-limits/1`` parser (:func:`levain.consent.limits.parse_limits_doc`): Q13's local file (lenient) and
a slot's ``L/policy/limits`` (strict, :func:`levain.consent.limits.slot_doc`) read by the same code."""
from __future__ import annotations

import json

import pytest

from levain.consent import limits as L
from levain.contracts.canon import canonical_bytes

FMT = L.LIMITS_FORMAT


def doc(**blocks):
    return {"format": FMT, **blocks}


def test_one_home_local_and_slot_read_a_valid_document_alike(tmp_path):
    d = doc(default={"calls": 40, "active_hours": 3}, codex={"calls": 20, "repeat": 5})
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    (q / L.LIMITS_NAME).write_text(json.dumps(d))
    for agent in L.AGENTS:
        local, src = L.load_limits(q, agent)
        slot, skipped = L.parse_limits_doc(d, agent, slot=True)
        assert skipped == [] and src == L.LIMITS_NAME
        assert set(slot) == set(L.SLOT_KINDS)
        assert {k: local[k] for k in L.SLOT_KINDS} == slot      # the slot form drops only the token kinds


def test_slot_reads_exactly_the_slot_kinds_defaults_under_the_blocks():
    got, _ = L.parse_limits_doc(doc(), "codex", slot=True)
    assert got == {k: L.DEFAULTS[k] for k in L.SLOT_KINDS}
    got, _ = L.parse_limits_doc(doc(default={"calls": 9}, claude_code={"calls": 7}), "codex", slot=True)
    assert got["calls"] == 9
    got, _ = L.parse_limits_doc(doc(default={"calls": 9}, claude_code={"calls": 7}), "claude_code", slot=True)
    assert got["calls"] == 7
    got, _ = L.parse_limits_doc(doc(default={"calls": 9}, codex={"calls": 7}), None, slot=True)
    assert got["calls"] == 9                                     # a writer's view: the default block only


@pytest.mark.parametrize("d,code", [
    ([], "limits-malformed"),
    ({"format": "levain-limits/2"}, "limits-malformed"),
    (doc(extra={}), "limits-key:extra"),
    (doc(default={"fresh_tokens": 5}), "limits-not-counted:fresh_tokens"),
    (doc(codex={"cache_read_tokens": 5}), "limits-not-counted:cache_read_tokens"),
    (doc(default={"dollars": 5}), "limits-key:dollars"),
    (doc(default=[]), "limits-value:default"),
    (doc(default={"calls": 0}), "limits-value:default.calls"),
    (doc(default={"calls": 10**9 + 1}), "limits-value:default.calls"),
    (doc(default={"calls": True}), "limits-value:default.calls"),
    (doc(default={"active_hours": 1.5}), "limits-value:default.active_hours"),
    (doc(codex={"repeat": 1}), "limits-value:codex.repeat"),
    (doc(codex={"repeat": L.REPEAT_WINDOW + 1}), "limits-value:codex.repeat"),   # R1 F2: never reachable
    (doc(claude_code={"calls": "5"}), "limits-value:claude_code.calls"),
])
def test_slot_refuses_by_name(d, code):
    with pytest.raises(L.LimitsDocError) as e:
        L.parse_limits_doc(d, "claude_code", slot=True)
    assert e.value.code == code


def test_slot_checks_every_block_whatever_the_agent():
    with pytest.raises(L.LimitsDocError) as e:
        L.parse_limits_doc(doc(codex={"repeat": 1}), "claude_code", slot=True)
    assert e.value.code == "limits-value:codex.repeat"


def test_repeat_range_is_two_to_the_window():
    assert L.SLOT_RANGE["repeat"] == (2, L.REPEAT_WINDOW)
    for v in (2, L.REPEAT_WINDOW):
        assert L.parse_limits_doc(doc(default={"repeat": v}), "codex", slot=True)[0]["repeat"] == v


def test_the_local_reading_is_still_lenient():
    got, bad = L.parse_limits_doc(doc(default={"calls": 0, "active_hours": 1.5}, codex=[]), "codex")
    assert got["calls"] == L.DEFAULTS["calls"] and got["active_hours"] == 1.5 and bad == ["default.calls", "codex"]
    with pytest.raises(L.LimitsDocError):
        L.parse_limits_doc({"format": "x"}, "codex")


def test_slot_doc_bytes():
    good = doc(default={"calls": 5})
    assert L.slot_doc(canonical_bytes(good)) == good
    spaced = json.dumps(good).encode()
    with pytest.raises(L.LimitsDocError) as e:
        L.slot_doc(spaced)
    assert e.value.code == "limits-malformed" and "canonical" in str(e.value)
    assert L.slot_doc(spaced, canonical=False) == good           # the writer re-serialises what it accepts
    for raw in (b'{"format":"levain-limits/1","format":"levain-limits/1"}', b'{"format":"levain-limits/1",'
                b'"default":{"active_hours":1.0}}', b"\xff", b"{nope"):
        with pytest.raises(L.LimitsDocError) as e:
            L.slot_doc(raw)
        assert e.value.code == "limits-malformed"
    with pytest.raises(L.LimitsDocError) as e:
        L.slot_doc(b" " * (L.SLOT_LIMITS_MAX + 1))
    assert e.value.code == "limits-oversize"
    with pytest.raises(L.LimitsDocError) as e:
        L.slot_doc(canonical_bytes(doc(default={"fresh_tokens": 1})))
    assert e.value.code == "limits-not-counted:fresh_tokens"
