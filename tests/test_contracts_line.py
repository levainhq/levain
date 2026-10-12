"""F9: the device message, the (line, 1) leaf at ingest and verify, and the sidecar receipt, against frozen/line/."""
import json
from datetime import datetime, timezone

import pytest

from levain.contracts import ContractError, canon as C
from levain.contracts.leaves import team as T
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("line")


def _now(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i, want = v["input"], v["expect"]["levain"]
    if v["op"] == "device_message":
        try:
            got = T.device_message(i["origin"], i["file"], i["chain_hash"])
        except ContractError:
            assert want["verdict"] == "refuse"
        else:
            assert want == {"verdict": "accept", "bytes_hex": got.hex()}
    elif v["op"] == "line":
        now = _now(i["now"]) if i["at"] == "ingest" else None
        verdict, leaf, reason = T.judge_line(h(i["data_hex"]), origin=i["origin"], scope=i["scope"],
                                             snapshot=T.Snapshot(), at=i["at"], now=now)
        assert verdict == want["verdict"], reason
        assert (leaf is not None) == (verdict == C.ACCEPT)
    elif v["op"] == "classify":    # the registry's context-free validator alone (rev 11 Q9)
        assert C.classify_leaf(h(i["data_hex"]), i["log_kind"], at=i["at"]) == want["verdict"]
    elif v["op"] == "receipt":
        obj = json.loads(h(i["leaf_hex"]))
        leaf = T.check_line_fields(obj)
        d = i["derive"]
        derived = T.derive_receipt(leaf, n=d["n"], leaf_index=d["leaf_index"], handle=d["handle"], scope=d["scope"],
                                   team_version=d["team_version"])
        try:
            T.check_receipt(i["receipt"], derived)
        except ContractError:
            assert want["verdict"] == "refuse"
        else:
            assert want["verdict"] == "accept"
    else:
        raise AssertionError(v["op"])


def test_the_fixed_message_golden_is_the_hand_built_one():
    v = next(v for v in VECTORS if v["id"] == "message-fixed-golden")
    b = h(v["expect"]["levain"]["bytes_hex"])
    assert b[:4] == bytes.fromhex("0000002c") and b[48:52] == bytes.fromhex("0000001a")
    assert b[78:82] == bytes.fromhex("00000020") and b[82:] == bytes(31) + b"\x01" and len(b) == 114


def test_nfc_and_nfd_file_names_are_two_messages():
    by = {v["id"]: v for v in VECTORS}
    assert by["message-file-nfc"]["expect"]["levain"]["bytes_hex"] != by["message-file-nfd"]["expect"]["levain"]["bytes_hex"]


def test_the_sidecar_device_is_fp_hex_16():
    v = next(v for v in VECTORS if v["id"] == "receipt-matches-the-derived-fields")
    leaf = T.check_line_fields(json.loads(h(v["input"]["leaf_hex"])))
    assert len(T.device_hint(leaf)) == 16 and v["input"]["receipt"]["device"] == T.device_hint(leaf)


def test_check_receipt_needs_every_derived_field():
    with pytest.raises(ValueError):
        T.check_receipt({}, {"n": 1})


def test_ingest_needs_an_aware_now_and_at_is_closed():
    good = h(next(v for v in VECTORS if v["id"] == "leaf-good-at-ingest")["input"]["data_hex"])
    origin = "levain-team/" + "a" * 32
    with pytest.raises(ValueError):
        T.judge_line(good, origin=origin, scope="human", snapshot=T.Snapshot(), at="ingest")
    with pytest.raises(ValueError):
        T.judge_line(good, origin=origin, scope="human", snapshot=T.Snapshot(), at="ingest", now=datetime(2026, 10, 12))
    with pytest.raises(ValueError):
        T.judge_line(good, origin=origin, scope="human", snapshot=T.Snapshot(), at="later")


def test_any_exception_while_judging_is_a_refusal(monkeypatch):
    good = h(next(v for v in VECTORS if v["id"] == "leaf-good-at-verify")["input"]["data_hex"])

    def boom(*a, **k):
        raise MemoryError("simulated")
    monkeypatch.setattr(T.sshsig, "verify", boom)
    verdict, leaf, _ = T.judge_line(good, origin="levain-team/" + "a" * 32, scope="human", snapshot=T.Snapshot(),
                                    at="verify")
    assert verdict == C.REFUSE and leaf is None


def test_a_line_leaf_on_a_fleet_or_receipt_log_is_refused():
    good = h(next(v for v in VECTORS if v["id"] == "leaf-good-at-verify")["input"]["data_hex"])
    assert C.classify_leaf(good, "team", at="verify") == C.ACCEPT
    for kind in ("fleet", "receipt"):
        assert C.classify_leaf(good, kind, at="verify") == C.REFUSE
