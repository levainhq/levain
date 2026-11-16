"""Host grants S5-0 (board levain#66, plan hgs5_plan_v2 section 2 item 1, T24 and T25): ``egress-grants-all``, root's
walk of the configured egress slots that answers only an operator's ACTIVE or BRAKED interactive or login sessions with
their live grants. Driven through :func:`runtime_helper.main` on the S2 test world (a scratch store and run dir); the
signature path is not involved (grants are seeded as root's own file).

Each test that states a fail-closed claim was run against the mutation it names (see the module's MUTATIONS note in the
return); the claims are: a not-ACTIVE/BRAKED session is never listed, an unattended or other operator's session is never
listed, a slot root cannot read is named, the verb never walks ``alloc/``."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from levain.slot import Refused
from levain.slot import egress_grants as eg
from levain.slot import egress_verbs as EV
from levain.slot import records as R
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId
from tests.test_slot_egress_grant_verbs import (DISPATCH_UID, NOW, S, SLOT_UID, STORE_ID, STRANGER_UID, UID, World,
                                                gid_for, refused)

OTHER = "s-" + "cd" * 8
BOOT = "6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f"


class AllWorld(World):
    """The S2 world plus whole slots: a subclass, so nothing is patched onto the shared ``World``."""

    def lease(self, idx, session, *, lease_seq=7):
        """Slot ``e<idx>``: its state file says ``leased`` to ``session``."""
        slot = SlotId("egress", idx)
        (self.L / "slots" / slot.tag).mkdir(exist_ok=True)
        self.put_slot_state(idx, "leased", session, lease_seq)

    def put_slot_state(self, idx, state, session=None, lease_seq=7):
        slot = SlotId("egress", idx)
        (self.L / "slots" / slot.tag / "state").write_bytes(R.slot_state_bytes(R.SlotState(
            slot=slot, rev=12, lease_seq=lease_seq, state=state, dispatch_eligible=False,
            changed_at="2026-11-16T11:00:00Z", session=session)))

    def put_alloc(self, session, slot, *, kind="interactive", operator=UID, lease_seq=7, name=None):
        a = R.Alloc(session=session, slot=slot, lease_seq=lease_seq, slot_rev=12, slot_uid=SLOT_UID, slot_gid=SLOT_UID,
                    kind=kind, vendor="claude", auth="brokered", operator_uid=operator, supervisor_uid=operator,
                    staging_uid=operator, created_at="2026-11-16T11:00:00Z", boot_id=BOOT,
                    child_uid=SLOT_UID + 1 if slot.slot_class == "none" else None)
        (self.L / "alloc" / f"{name or session}.json").write_bytes(R.alloc_bytes(a))

    def add_session(self, idx, session, *, kind="interactive", operator=UID, state=R.ACTIVE, lease_seq=7, grants=()):
        """A leased slot ``e<idx>`` with an alloc, a CONTROL state record, a grants dir and (optionally) a file, the
        slot state and the alloc at the same ``lease_seq``."""
        slot = SlotId("egress", idx)
        self.lease(idx, session, lease_seq=lease_seq)
        self.put_alloc(session, slot, kind=kind, operator=operator, lease_seq=lease_seq)
        self.set_state(state, session)
        d = self.R / "grants" / f"slot{idx}"
        if not d.exists():
            d.mkdir(parents=True)
            d.chmod(0o750)
        if grants:
            write_grants(d, session, idx, grants)


@pytest.fixture
def world(tmp_path):
    return AllWorld(tmp_path)


def write_grants(d, session, idx, hosts, *, expired=()):
    es = [eg.Entry(grant=gid_for(100 * idx + i), host=h, port=443, expires=NOW + timedelta(hours=1), choice="s")
          for i, h in enumerate(hosts)]
    es += [eg.Entry(grant=gid_for(100 * idx + 50 + i), host=h, port=443, expires=NOW - timedelta(minutes=5),
                    choice="s") for i, h in enumerate(expired)]
    p = d / "grants.json"
    p.unlink(missing_ok=True)
    p.write_bytes(eg.dump_grants(session, idx, es))
    p.chmod(0o440)
    return es


def ask(w, uid=UID):
    return w.main(["egress-grants-all"], uid=uid)


def entries_of(line, idx):
    row, = [r for r in line[1]["slots"] if r["slot"] == idx]
    return row


# --- the shape ----------------------------------------------------------------------------------------------------------

def test_no_leased_slot_answers_the_store_and_two_empty_lists(world):
    world.put_slot_state(3, "reusable")
    assert ask(world) == (RH.EXIT_OK, {"ok": True, "store": STORE_ID, "slots": [], "unread": []})


def test_an_active_session_lists_its_live_grants_and_prunes_the_expired(world):
    world.add_session(3, S, grants=("a.example.org",))
    write_grants(world.slot_dir, S, 3, ["a.example.org"], expired=["gone.example.org"])
    rc, ans = ask(world)
    assert rc == RH.EXIT_OK and ans["store"] == STORE_ID and ans["unread"] == []
    row, = ans["slots"]
    assert (row["slot"], row["session"], row["kind"], row["state"]) == (3, S, "interactive", "ACTIVE")
    assert row["entries"] == [{"grant": gid_for(300), "host": "a.example.org",
                               "expires": eg.stamp(NOW + timedelta(hours=1)), "choice": "s"}]


def test_a_braked_session_is_listed_with_its_state_and_its_grants(world):
    world.add_session(3, S, state=R.BRAKED, grants=("a.example.org",))
    row = entries_of(ask(world), 3)
    assert row["state"] == "BRAKED" and [e["host"] for e in row["entries"]] == ["a.example.org"]


def test_an_active_session_with_no_grants_dir_is_listed_with_no_entries(world):
    world.add_session(3, S)
    os.rmdir(world.slot_dir)
    assert entries_of(ask(world), 3)["entries"] == []


def test_a_login_session_is_listed_and_two_slots_list_in_slot_order(world):
    world.add_session(4, OTHER, kind="login", grants=("b.example.org",))
    world.add_session(3, S, grants=("a.example.org",))
    rc, ans = ask(world)
    assert [(r["slot"], r["session"], r["kind"]) for r in ans["slots"]] == [(3, S, "interactive"), (4, OTHER, "login")]


# --- T24: what is never listed (each is a mutation target) ----------------------------------------------------------------

@pytest.mark.parametrize("state", [s for s in R.STATES if s not in (R.ACTIVE, R.BRAKED)])
def test_a_session_in_any_other_control_state_is_omitted_even_with_a_leftover_file(world, state):
    """A dead front's leftover grants file (a failed teardown removal) is never shown live."""
    world.add_session(3, S, state=state, grants=("left.example.org",))
    assert (world.slot_dir / "grants.json").exists()
    assert ask(world)[1]["slots"] == [] and ask(world)[1]["unread"] == []


def test_a_session_with_no_control_record_is_omitted(world):
    """Plan v2 section 4 "any other state": an alloc with no CONTROL record is prepare's window between its alloc and
    ATTACH (the reaper's ``revoke_dead_holders`` no-record case). Such a session was never ACTIVE (``egress-grant``
    needs the record), and nothing removes a record once written, so it has no grants of its own to show."""
    world.add_session(3, S, grants=("left.example.org",))
    (world.L / "sessions" / f"{S}.json").unlink()
    assert ask(world)[1] == {"ok": True, "store": STORE_ID, "slots": [], "unread": []}


@pytest.mark.parametrize("state", ["reusable", "scrubbed", "retired"])
def test_a_slot_that_is_not_leased_is_omitted_even_with_a_leftover_file(world, state):
    world.add_session(3, S, grants=("left.example.org",))
    world.put_slot_state(3, state)
    assert ask(world)[1]["slots"] == []


@pytest.mark.parametrize("kind", ["dispatch", "planner", "verifier"])
def test_an_unattended_kind_is_omitted_and_not_named_unread(world, kind):
    world.add_session(3, S, kind=kind, grants=("a.example.org",))
    assert ask(world)[1] == {"ok": True, "store": STORE_ID, "slots": [], "unread": []}


def test_another_operators_session_is_omitted_and_not_named_unread(world):
    world.add_session(3, S, operator=STRANGER_UID, grants=("a.example.org",))
    assert ask(world)[1] == {"ok": True, "store": STORE_ID, "slots": [], "unread": []}


def test_a_grants_file_that_names_another_session_lists_no_entries(world):
    world.add_session(3, S)
    write_grants(world.slot_dir, OTHER, 3, ["x.example.org"])
    assert entries_of(ask(world), 3)["entries"] == []


# --- T24: a slot root cannot read is named, the others still list -----------------------------------------------------------

def test_a_slot_dir_that_fails_its_trust_walk_is_a_named_unread_row_and_the_other_slot_lists(world):
    world.add_session(3, S, grants=("a.example.org",))
    world.add_session(4, OTHER, grants=("b.example.org",))
    (world.R / "grants" / "slot4").chmod(0o755)                 # not exactly 0750
    rc, ans = ask(world)
    assert rc == RH.EXIT_OK
    assert ans["unread"] == [{"slot": 4, "name": "egress-grant-dir-untrusted"}]
    assert [r["slot"] for r in ans["slots"]] == [3]


def test_a_malformed_slot_state_file_is_a_named_unread_row(world):
    world.add_session(3, S, grants=("a.example.org",))
    world.add_session(4, OTHER)
    (world.L / "slots" / "e4" / "state").write_bytes(b"{not json")
    rc, ans = ask(world)
    assert ans["unread"] == [{"slot": 4, "name": "slot-state-malformed"}]
    assert [r["slot"] for r in ans["slots"]] == [3]


def test_a_malformed_control_record_is_a_named_unread_row_never_an_omission(world):
    world.add_session(3, S, grants=("a.example.org",))
    (world.L / "sessions" / f"{S}.json").write_bytes(b"{not json")
    rc, ans = ask(world)
    assert ans["slots"] == [] and len(ans["unread"]) == 1 and ans["unread"][0]["slot"] == 3


def test_an_alloc_of_another_lease_is_a_named_unread_row(world):
    """The slot's state names S at lease 8; S's alloc says lease 7: not a record to list from."""
    world.add_session(3, S, grants=("a.example.org",))
    world.put_slot_state(3, "leased", S, 8)
    assert ask(world)[1]["unread"] == [{"slot": 3, "name": "egress-grants-slot-mismatch"}]
    assert ask(world)[1]["slots"] == []


# --- L3 r1: a leased slot whose alloc or CONTROL record is corrupt is unread, never omitted ----------------------------------

def _two_slots(world):
    world.add_session(3, S, grants=("a.example.org",))
    world.add_session(4, OTHER, grants=("b.example.org",))


def _unread_4(world):
    """Slot 4 is the only unread row and slot 3 still lists."""
    rc, ans = ask(world)
    assert rc == RH.EXIT_OK and [r["slot"] for r in ans["slots"]] == [3], ans
    (row,) = ans["unread"]
    assert row["slot"] == 4
    return row["name"]


def test_a_missing_alloc_under_a_control_record_is_unread(world):
    """Only a clean prepare failure unlinks an alloc, and only while CONTROL holds no record: an alloc gone under a
    record is damage, so the slot is named."""
    _two_slots(world)
    (world.L / "alloc" / f"{OTHER}.json").unlink()
    assert _unread_4(world) == EV.ERR_ALL_UNREAD


def test_a_leased_slot_with_no_alloc_and_no_record_is_the_reapers_orphan_and_omitted(world):
    """prepare between its lease and its alloc, or a crashed clean failure: never ACTIVE, no grants of its own."""
    _two_slots(world)
    (world.L / "alloc" / f"{OTHER}.json").unlink()
    (world.L / "sessions" / f"{OTHER}.json").unlink()
    assert ask(world)[1]["unread"] == [] and [r["slot"] for r in ask(world)[1]["slots"]] == [3]


@pytest.mark.parametrize("raw", [b"{not json", b"", b'{"format":"levain-alloc/1"}'])
def test_a_corrupt_alloc_is_unread(world, raw):
    _two_slots(world)
    (world.L / "alloc" / f"{OTHER}.json").write_bytes(raw)
    assert _unread_4(world) == EV.ERR_ALL_UNREAD


def test_an_alloc_that_names_a_none_class_slot_is_a_mismatch_not_an_omission(world):
    _two_slots(world)
    world.put_alloc(OTHER, SlotId("none", 4))
    assert _unread_4(world) == EV.ERR_ALL_MISMATCH


def test_an_alloc_whose_own_session_field_is_another_session_is_a_mismatch(world):
    _two_slots(world)
    world.put_alloc(S, SlotId("egress", 4), name=OTHER)        # OTHER.json holds S's alloc on slot 4
    assert _unread_4(world) == EV.ERR_ALL_MISMATCH


def test_a_control_record_whose_own_session_field_is_another_session_is_a_mismatch_never_listed(world):
    """codex: a slot leased to OTHER whose ``sessions/OTHER.json`` is S's ACTIVE record must not list OTHER's file."""
    _two_slots(world)
    (world.L / "sessions" / f"{OTHER}.json").write_bytes((world.L / "sessions" / f"{S}.json").read_bytes())
    assert _unread_4(world) == EV.ERR_ALL_MISMATCH


def test_a_structurally_wrong_but_canonical_control_record_is_a_named_unread_row(world):
    """deepseek: a well-formed JSON record missing its keys refuses by name (records' strict parse), never a KeyError."""
    _two_slots(world)
    (world.L / "sessions" / f"{OTHER}.json").write_bytes(b'{"format":"levain-session-state/1"}')
    assert _unread_4(world) == "missing-key"


def test_a_foreign_operators_alloc_on_a_slot_is_still_omitted_whatever_its_lease(world):
    """The ownership check comes before the slot and lease checks: another operator's slot is never named."""
    _two_slots(world)
    world.put_alloc(OTHER, SlotId("egress", 4), operator=STRANGER_UID, lease_seq=99)
    assert ask(world)[1]["unread"] == []


def test_an_unreadable_slot_dir_oserror_is_named_unread(world, monkeypatch):
    world.add_session(3, S, grants=("a.example.org",))
    world.add_session(4, OTHER)

    def boom(ctx, slot):
        if slot.index == 4:
            raise PermissionError(13, "denied")
        return real(ctx, slot)

    real = EV.ES.open_slot_dir
    monkeypatch.setattr(EV.ES, "open_slot_dir", boom)
    ans = ask(world)[1]
    assert ans["unread"] == [{"slot": 4, "name": EV.ERR_ALL_UNREAD}] and [r["slot"] for r in ans["slots"]] == [3]


# --- the answer is bounded: the walk is the configured slots, never every alloc ever prepared -------------------------------

def test_three_hundred_historical_allocs_on_two_slots_give_two_rows_and_read_no_other_alloc(world, monkeypatch):
    world.add_session(3, S, grants=("a.example.org",))
    world.add_session(4, OTHER, grants=("b.example.org",))
    for i in range(300):
        old = "s-%016x" % (0x1000 + i)
        a = R.Alloc(session=old, slot=SlotId("egress", 3 + i % 2), lease_seq=1, slot_rev=1, slot_uid=SLOT_UID,
                    slot_gid=SLOT_UID, kind="interactive", vendor="claude", auth="brokered", operator_uid=UID,
                    supervisor_uid=UID, staging_uid=UID, created_at="2026-10-01T11:00:00Z", boot_id=BOOT)
        (world.L / "alloc" / f"{old}.json").write_bytes(R.alloc_bytes(a))
    seen = []
    real = RH.Store.read

    def spy(self, parts, name, **kw):
        if tuple(parts) == ("alloc",):
            seen.append(name)
        return real(self, parts, name, **kw)

    monkeypatch.setattr(RH.Store, "read", spy)
    monkeypatch.setattr(RH, "status", lambda *a, **k: pytest.fail("the verb must not call status"))
    ans = ask(world)[1]
    assert [r["session"] for r in ans["slots"]] == [S, OTHER]
    assert sorted(seen) == sorted([f"{S}.json", f"{OTHER}.json"])


def test_the_walk_is_the_configured_egress_slots_only_a_none_slot_is_not_visited(world):
    world.add_session(3, S, grants=("a.example.org",))
    (world.L / "slots" / "v1").mkdir()
    assert [r["slot"] for r in ask(world)[1]["slots"]] == [3]


# --- T25: operator only ---------------------------------------------------------------------------------------------------

def test_a_dispatch_caller_and_a_stranger_are_refused_before_any_work(world):
    world.add_session(3, S, grants=("a.example.org",))
    refused(ask(world, uid=DISPATCH_UID), "verb-not-allowed-for-caller")
    refused(ask(world, uid=STRANGER_UID), "unknown-caller")


def test_the_handler_itself_refuses_a_non_operator_caller_rather_than_answer_empty(world, monkeypatch):
    """Defence beside the grammar: the ownership check would skip every slot for a dispatch caller, an empty answer.
    With the grammar's rule lifted (monkeypatch, restored), the dispatch caller runs the real ``main`` dispatch."""
    world.add_session(3, S, grants=("a.example.org",))
    monkeypatch.setattr(RG, "DISPATCH_FORBIDDEN", RG.DISPATCH_FORBIDDEN - {"egress-grants-all"})
    refused(ask(world, uid=DISPATCH_UID), "verb-not-allowed-for-caller")


def test_the_verb_takes_no_argument_and_no_stdin(world):
    refused(world.main(["egress-grants-all", S]), "extra-argv")
    refused(world.main(["egress-grants-all"], b'{"a": 1}'), "unexpected-stdin")
    assert not RH.wants_stdin(["egress-grants-all"])


def test_an_install_without_the_grant_store_refuses_not_ready_never_an_empty_list(world):
    world.add_session(3, S, grants=("a.example.org",))
    (world.L / "routes" / "grants" / "store_id").unlink()
    refused(ask(world), "egress-grant-not-ready")


def test_the_new_verb_is_registered_everywhere_the_siblings_are():
    v = "egress-grants-all"
    assert v in RG.VERBS and v in RG.EGRESS_VERBS and v in RG.DISPATCH_FORBIDDEN and v in RH.VERB_HANDLERS
    assert v not in RG.LEASE_VERBS and v not in RG.STDIN_VERBS
