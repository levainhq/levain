"""CONTRACT §7.1-§7.3: the alloc record, the state record, the state set + legal edges, the CAS-writable
fields, and the durable write. Pure values and real files under pytest's tmp dir."""
from __future__ import annotations

import os
import stat

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import records as R
from levain.slot.ids import SlotId
from levain.slot.runtime_grammar import VerifierStep

S1 = "s-0123456789abcdef"
BOOT = "6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f"
TS = "2026-10-20T14:00:00Z"
TICKET = "A" * 42 + "A"


def _alloc(**kw) -> R.Alloc:
    base = dict(session=S1, slot=SlotId("egress", 3), lease_seq=7, slot_rev=12, slot_uid=3990, slot_gid=3990,
                kind="interactive", vendor="claude", auth="brokered", operator_uid=501, supervisor_uid=501,
                staging_uid=501, created_at=TS, boot_id=BOOT)
    base.update(kw)
    return R.Alloc(**base)


def _policy(**kw) -> R.AllocPolicy:
    base = dict(routes=("anthropic", "github"), egress_routes=("github",), queue_grant=True,
                hooks=frozenset({"levain_gate", "levain_queue"}), incarnation=1,
                front=R.Front(uid=3989, pending=True))
    base.update(kw)
    return R.AllocPolicy(**base)


def _attach_claims(**kw) -> dict:
    base = {"operator_uid": 501, "supervisor_uid": 501, "slot": {"class": "egress", "index": 3},
            "class": "egress", "kind": "interactive", "vendor": "claude", "auth": "brokered",
            "lease_seq": 7, "slot_rev": 12}
    base.update(kw)
    return base


def _mint_claims(**kw) -> dict:
    base = {"routes": ["anthropic"], "queue_grant": False, "incarnation": 1, "egress_routes": [],
            "front": {"uid": 3989, "pid": 4242, "start": 1700000000}}
    base.update(kw)
    return base


def _refused(name: str, fn, *a, **kw) -> None:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    assert ei.value.name == name, ei.value


# --- the alloc record -----------------------------------------------------------------------------------

def test_alloc_identity_round_trip_canonical():
    a = _alloc()
    data = R.alloc_bytes(a)
    assert canon.canonical_bytes(canon.parse(data)) == data
    back = R.parse_alloc(data)
    assert back == a
    obj = canon.parse(data)
    assert obj["format"] == R.ALLOC_FORMAT and obj["class"] == "egress"
    assert not (R.ALLOC_POLICY_KEYS & set(obj))


def test_alloc_rendered_round_trip():
    a = _alloc(policy=_policy(model="claude-opus-5-5", front=R.Front(3989, False, pid=4242, start=99)),
               netns="levain-e3", cgroup=R.cgroup_path(SlotId("egress", 3), S1))
    back = R.parse_alloc(R.alloc_bytes(a))
    assert back == a and back.policy.front.pid == 4242
    assert canon.parse(R.alloc_bytes(a))["hooks"] == ["levain_gate", "levain_queue"]


def test_alloc_verifier_round_trip():
    a = _alloc(slot=SlotId("none", 1), kind="verifier", auth="none", child_uid=3970, ticket=TICKET,
               ticket_facts={"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "a" * 64,
                             "kind": "verifier", "egress_routes": [], "reserved_microdollars": 0},
               policy=_policy(routes=(), egress_routes=(), queue_grant=False, hooks=frozenset(), front=None,
                              verifier_steps=(VerifierStep(("make", "test"), 60),)))
    assert R.parse_alloc(R.alloc_bytes(a)) == a


def _alloc_obj(**kw) -> dict:
    obj = canon.parse(R.alloc_bytes(_alloc(policy=_policy())))
    obj.update(kw)
    return obj


def _parse_obj(obj: dict) -> R.Alloc:
    return R.parse_alloc(canon.canonical_bytes(obj))


@pytest.mark.parametrize("key", sorted(R.ALLOC_IDENTITY_KEYS - {"child_uid", "ticket", "ticket_facts",
                                                                 "netns", "cgroup", "var_folders"}))
def test_alloc_missing_identity_key_refused_by_name(key):
    obj = _alloc_obj()
    del obj[key]
    _refused("missing-key", _parse_obj, obj)


def test_alloc_unknown_key_refused_by_name():
    _refused("unknown-key", _parse_obj, _alloc_obj(trashed_at=TS))
    obj = _alloc_obj()
    obj["front"]["extra"] = 1
    _refused("unknown-key", _parse_obj, obj)


@pytest.mark.parametrize("key", sorted(R.ALLOC_POLICY_REQUIRED))
def test_alloc_partial_policy_refused(key):
    obj = _alloc_obj()
    del obj[key]
    _refused("missing-key", _parse_obj, obj)


def test_alloc_not_canonical_refused():
    data = R.alloc_bytes(_alloc())
    spaced = data.replace(b'","', b'", "', 1)
    assert spaced != data
    _refused("record-not-canonical", R.parse_alloc, spaced)
    _refused("record-malformed", R.parse_alloc, b"[1]")
    _refused("record-malformed", R.parse_alloc, b'{"a":1.5}')


@pytest.mark.parametrize("change,name", [
    ({"class": "none"}, "bad-value"),                     # class disagrees with slot.class
    ({"slot_gid": 3991}, "bad-value"),                    # gid == uid (§1.1)
    ({"child_uid": 3970}, "bad-value"),                   # a child only for the none class
    ({"netns": "levain-e4"}, "bad-value"),
    ({"cgroup": "/sys/fs/cgroup/x"}, "bad-value"),
    ({"verifier_steps": [{"argv": ["x"], "wall_s": 1}]}, "bad-value"),   # verifier only
    ({"hooks": ["levain_queue", "levain_gate"]}, "bad-value"),            # one spelling: sorted
    ({"hooks": ["other"]}, "bad-value"),
    ({"ticket_facts": {}}, "missing-key"),
    ({"created_at": "2026-13-01T00:00:00Z"}, "bad-value"),
    ({"boot_id": "nope"}, "bad-value"),
    ({"session": "s-XYZ"}, "bad-token"),
    ({"lease_seq": True}, "wrong-type"),
    ({"format": "levain-alloc/2"}, "bad-format"),
    ({"front": {"uid": 1, "pending": False, "pid": 3}}, "bad-value"),     # pid and start together
])
def test_alloc_field_rules(change, name):
    _refused(name, _parse_obj, _alloc_obj(**change))


def test_alloc_front_on_none_class_refused():
    obj = canon.parse(R.alloc_bytes(_alloc(slot=SlotId("none", 0), kind="verifier", auth="none",
                                           child_uid=3970)))
    obj.update(routes=[], egress_routes=[], queue_grant=False, hooks=[], incarnation=0,
               front={"uid": 1, "pending": True})
    _refused("bad-value", _parse_obj, obj)


def test_alloc_typed_build_checks():
    _refused("bad-value", _alloc, slot_gid=1)
    _refused("bad-value", _alloc, child_uid=5)


# --- claims ---------------------------------------------------------------------------------------------

def test_attach_claims_exact_keys():
    assert R.parse_attach_claims(_attach_claims()) == _attach_claims()
    assert R.parse_attach_claims(_attach_claims(ticket=TICKET))["ticket"] == TICKET
    c = _attach_claims()
    del c["lease_seq"]
    _refused("missing-key", R.parse_attach_claims, c)
    _refused("unknown-key", R.parse_attach_claims, _attach_claims(routes=[]))
    _refused("claims-mismatch", R.parse_attach_claims, _attach_claims(**{"class": "none"}))
    assert R.ATTACH_CLAIM_KEYS == {"operator_uid", "supervisor_uid", "slot", "class", "kind", "vendor", "auth",
                                   "lease_seq", "slot_rev", "ticket"}


def test_mint_claims_exact_keys():
    assert R.parse_mint_claims(_mint_claims()) == _mint_claims()
    _refused("unknown-key", R.parse_mint_claims, _mint_claims(kind="x"))
    c = _mint_claims()
    del c["incarnation"]
    _refused("missing-key", R.parse_mint_claims, c)
    _refused("unknown-key", R.parse_mint_claims, _mint_claims(front={"uid": 1, "pid": 2, "start": 3, "x": 4}))
    assert R.MINT_CLAIM_KEYS == {"routes", "model", "queue_grant", "front", "incarnation", "egress_routes"}


def test_record_claims_shapes():
    assert R.parse_claims({}) == {}
    merged = {**_attach_claims(), **_mint_claims()}
    assert R.parse_claims(merged) == merged
    partial = {**_attach_claims(), "routes": ["a"]}
    _refused("missing-key", R.parse_claims, partial)
    _refused("missing-key", R.parse_claims, _mint_claims())    # MINT claims never stand alone


# --- the state record ------------------------------------------------------------------------------------

def _state(**kw) -> R.SessionState:
    base = dict(session=S1, generation=3, state=R.ACTIVE, claims=_attach_claims(), lease_live=True,
                event_kind="state", updated_at=TS)
    base.update(kw)
    return R.SessionState(**base)


def test_state_round_trip():
    s = _state(touched=(".config/git", "CLAUDE.md"),
               fields={"manifest": {"digest": "sha256:" + "0" * 64, "entries": 3, "bytes": 10},
                       "identity": {"observed_id": "user@example.com"}, "held_attempts": 2})
    data = R.state_bytes(s)
    assert R.parse_state(data) == s
    assert canon.parse(data)["format"] == R.STATE_FORMAT


def test_state_keys_refused_by_name():
    obj = canon.parse(R.state_bytes(_state()))
    obj["intent"] = "retain"           # c2's field, GONE in c3 (§7.2)
    _refused("unknown-key", R.parse_state, canon.canonical_bytes(obj))
    obj = canon.parse(R.state_bytes(_state()))
    obj["trashed_at"] = TS             # root's file only (§7.2)
    _refused("unknown-key", R.parse_state, canon.canonical_bytes(obj))
    for key in sorted(R.STATE_REQUIRED):
        obj = canon.parse(R.state_bytes(_state()))
        del obj[key]
        _refused("missing-key", R.parse_state, canon.canonical_bytes(obj))


def test_state_exclusive_pairs():
    _refused("bad-value", _state, fields={"verifier_report_digest": "sha256:" + "1" * 64,
                                          "verifier_report_refused": "too-big"})
    _refused("bad-value", _state, fields={"artifact_digest": "sha256:" + "1" * 64,
                                          "artifact_failed": "cap"})


def test_state_field_values_checked():
    _refused("bad-value", _state, fields={"identity": {"observed_id": "has space"}})
    _refused("bad-value", _state, state="NOPE")
    _refused("bad-value", _state, event_kind="pool")
    _refused("bad-value", _state, touched=("a/b",))


# --- states, edges, CAS fields -------------------------------------------------------------------------------

#: §7.3, written out by hand from the contract text (the module's table is checked AGAINST this).
CONTRACT_EDGES = {
    ("ALLOCATED", "RENDERING"), ("RENDERING", "RENDERED"), ("RENDERED", "ACTIVE"),
    ("ACTIVE", "BRAKED"), ("BRAKED", "ACTIVE"),
    ("ALLOCATED", "REVOKED"), ("RENDERING", "REVOKED"), ("RENDERED", "REVOKED"), ("ACTIVE", "REVOKED"),
    ("BRAKED", "REVOKED"),
    ("REVOKED", "FROZEN"), ("FROZEN", "DEAD"), ("DEAD", "CREDENTIAL_PARKED"), ("CREDENTIAL_PARKED", "RETAINED"),
    ("RETAINED", "TRASH"), ("TRASH", "PURGED"), ("TRASH", "RETAINED"),
    ("REVOKED", "HELD"), ("FROZEN", "HELD"), ("DEAD", "HELD"), ("CREDENTIAL_PARKED", "HELD"),
    ("HELD", "FROZEN"), ("HELD", "DEAD"), ("HELD", "CREDENTIAL_PARKED"), ("HELD", "RETAINED"),
}


def test_edge_table_is_the_contracts():
    assert set(R.EDGES) == CONTRACT_EDGES
    assert set(R.STATES) == {a for a, _ in CONTRACT_EDGES} | {b for _, b in CONTRACT_EDGES}
    assert R.LIVE_STATES == {"ALLOCATED", "RENDERING", "RENDERED", "ACTIVE", "BRAKED"}
    assert not any(a == "PURGED" for a, _ in R.EDGES)              # terminal
    assert not any(b in ("ALLOCATED",) for _, b in R.EDGES)        # only ATTACH creates ALLOCATED


def test_cas_fields_exact():
    assert R.CAS_FIELDS == {"failed_step", "held_attempts", "brake_reason", "touched", "manifest",
                            "artifact_digest", "artifact_failed", "verifier_report_digest",
                            "verifier_report_refused", "identity"}
    for never in ("claims", "artifact_acked", "lease_live", "state", "generation", "intent", "trashed_at"):
        assert never not in R.CAS_FIELDS


# --- the durable write ----------------------------------------------------------------------------------------

def test_write_durably_replaces_whole(tmp_path):
    p = tmp_path / "x.json"
    R.write_durably(p, b"one")
    R.write_durably(p, b"two")
    assert p.read_bytes() == b"two"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert sorted(os.listdir(tmp_path)) == ["x.json"]


def test_write_durably_fsync_failure_leaves_old(tmp_path):
    p = tmp_path / "x.json"
    R.write_durably(p, b"old")

    def boom(fd):
        raise OSError(5, "EIO")

    with pytest.raises(OSError):
        R.write_durably(p, b"new", fsync=boom)
    assert p.read_bytes() == b"old"
    assert sorted(os.listdir(tmp_path)) == ["x.json"]


def test_write_durably_fsyncs_file_and_dir(tmp_path):
    seen = []
    R.write_durably(tmp_path / "x.json", b"d", fsync=lambda fd: seen.append(stat.S_ISDIR(os.fstat(fd).st_mode)))
    assert seen == [False, True]


def test_read_record_refuses_link(tmp_path):
    (tmp_path / "real").write_bytes(b"{}")
    os.symlink(tmp_path / "real", tmp_path / "link")
    _refused("bad-root", R.read_record, tmp_path / "link")
    assert R.read_record(tmp_path / "absent") is None
