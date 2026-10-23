"""Row 33 R1b undo half, FIX2 (L3 r2 on seat/1022-18-r1b; HEAD 1022-20 rulings).

* codex HIGH: undo retires a recorded account's uid when that account is Levain's at the uid (live, journal-present or
  our tombstone) OR absent; it leaves the uid only when the name is present at another uid or under a foreign label, or
  another account holds the uid (FIX1 L1, complement r1 LOW, stays closed).
* codex MED: undo's journal is parsed back with the journal reader before step one; a set that does not round-trip
  (over ``generation.MAX_MANAGED``) refuses ``undo-journal-unreadable`` with nothing removed.
* complement LOW 1: a pf token file is exactly one line of digits; a one-id retired-ids file is NOT_OURS.
* complement LOW 2: undo's preflight refusal names each Levain-written drop-in visudo flagged and the command that
  removes it.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from levain.slot import Refused, pf
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import removal as RM
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, RETIRED_IDS_PATH
from levain.slot.sudoers import RUNTIME_SUDOERS

from tests.test_repair_remove import SUDOERS_DIR, _Proc, _host, _put
from tests.test_undo_fix1 import FOREIGN, _walk
from tests.test_undo_remove import OP, OP_UID, S0, _darwin_host, _facts, _shape

S5 = S0 - 5
assert S5 in RANGES["darwin"].slots


def _retired(tmp: Path, users: dict, *, tombs: dict | None = None, accounts: dict | None = None) -> frozenset:
    """Plan undo over a record naming ``_levain_s5`` at ``S5``; ``users``: every user the host holds, name -> uid. The
    name ``_levain_s5`` stays in ``existing_names`` throughout: its own group outlives a hand-deleted account."""
    h, live, shared = _darwin_host(tmp)
    names = frozenset(users) | frozenset(shared) | {"_levain_s5"}
    facts = replace(_facts("darwin", _shape(("account/1", ("_levain_s5", S5)))), user_ids=users,
                    levain_tombstones=tombs or {}, existing_names=names)
    plan = IH.plan_undo(parse_install_argv(["undo"]), facts, accounts=accounts or {"_levain_s0": S0},
                        shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    return plan.retired_after


BASE = {"root": 0, OP: OP_UID, "_levain_s0": S0}


def test_a_recorded_account_live_at_its_uid_is_retired(tmp_path: Path) -> None:
    got = _retired(tmp_path, {**BASE, "_levain_s5": S5}, accounts={"_levain_s0": S0, "_levain_s5": S5})
    assert {S0, S5} <= got


def test_a_recorded_account_now_absent_is_retired(tmp_path: Path) -> None:
    """codex HIGH: the account was deleted by hand (its files may remain at the uid): the uid is never reissued."""
    assert S5 in _retired(tmp_path, BASE)


def test_our_tombstone_at_the_recorded_uid_is_retired(tmp_path: Path) -> None:
    assert S5 in _retired(tmp_path, {**BASE, "_levain_s5": S5}, tombs={"_levain_s5": S5})


@pytest.mark.parametrize("users,tombs", [
    ({**BASE, "_levain_s5": S5 - 100}, {}),                     # the name at another uid
    ({**BASE, "_levain_s5": S5}, {}),                           # the name at the uid under a foreign label
    ({**BASE, "_levain_s5": S5 - 100}, {"_levain_s5": S5 - 100}),   # our tombstone, but at another uid
    ({**BASE, "someone": S5}, {}),                              # the name absent, another account at the uid
])
def test_a_recorded_uid_another_account_may_hold_is_not_retired(tmp_path: Path, users, tombs) -> None:
    got = _retired(tmp_path, users, tombs=tombs)
    assert S0 in got and S5 not in got


def test_gather_reports_every_user_and_its_uid(tmp_path: Path) -> None:
    from tests.test_slot_s2h_install_main import OP as FX_OP, OP_UID as FX_UID, _darwin_fixture, _store_seams

    run, L, state, ids = _darwin_fixture(tmp_path)
    g = IH.gather_facts(parse_install_argv(["undo"]), "darwin", FX_UID, run=run, store_seams=_store_seams(L),
                        state_dir=state, user_name=lambda uid: FX_OP)
    assert g.facts.user_ids["_levain_s1"] == ids["s1"] and g.facts.user_ids["_levain_s3"] == ids["s3"]
    assert g.facts.user_ids[FX_OP] == FX_UID


# --- codex MED: the undo journal round-trips before step one ---------------------------------------------------------

def test_an_undo_set_over_the_journal_bound_refuses_before_any_act(tmp_path: Path) -> None:
    h, live, shared = _darwin_host(tmp_path)
    many = tuple(("unit/1", f"x{i}") for i in range(G.MAX_MANAGED))     # a full record, plus undo's desired set
    assert len(many) == G.MAX_MANAGED
    with pytest.raises(Refused) as e:
        IH.plan_undo(parse_install_argv(["undo"]), _facts("darwin", _shape(*many)), accounts={"_levain_s0": S0},
                     shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    assert e.value.name == "undo-journal-unreadable"
    assert not h.acts() and not live.at(IH.INTENT_PATH["darwin"]).exists()


# --- complement LOW 1: a pf token is one line -------------------------------------------------------------------------

@pytest.mark.parametrize("data,want", [
    (b"12345\n", "ours"), (b"12345", "ours"),
    (b"3999\n", "not-ours"),                                    # a one-id retired-ids file, at a recorded token path
    (b"3999\n4000\n", "not-ours"), (b" 12345\n", "not-ours"), (b"12345\n\n", "not-ours"), (b"", "not-ours"),
])
def test_a_pf_token_is_exactly_one_line_of_digits(tmp_path: Path, data, want) -> None:
    h, live = _host(tmp_path, "darwin")
    path = RETIRED_IDS_PATH["darwin"] if data == b"3999\n" else pf.PF_TOKEN
    _put(live, path, data)
    assert pf.enable_state(str(path), live) == want


def test_a_one_id_retired_ids_file_at_a_recorded_token_path_is_left(tmp_path: Path) -> None:
    h, live = _host(tmp_path, "darwin")
    path = RETIRED_IDS_PATH["darwin"]
    _put(live, path, b"3999\n")
    record = ("pf-enable/1", str(path))
    for ph in IH.REMOVE_PHASES["pf-enable/1"]:
        for s in IH.removal_steps("darwin", [record], ph, live=live, held=frozenset(), end=lambda u, n: (True, "")):
            assert s.call()[0], s.why
    assert live.at(path).read_bytes() == b"3999\n" and live.skips == {record}
    assert not [a for a in h.acts() if a[0].endswith("pfctl") or a[0].endswith("/rm")]


# --- complement LOW 2: the preflight refusal names Levain's own broken drop-ins ---------------------------------------

def test_the_preflight_refusal_names_each_levain_drop_in_visudo_flagged(tmp_path: Path) -> None:
    h, live, shared = _darwin_host(tmp_path)
    _put(live, SUDOERS_DIR / "admin", FOREIGN)
    out = (f"/etc/sudoers: parsed OK\n{SUDOERS_DIR / 'admin'}: parsed OK\n"
           f">>> {RUNTIME_SUDOERS}: syntax error near line 3 <<<\n")
    err = f"parse error in {SUDOERS_DIR / 'admin'} near line 9\n"         # flagged, but not Levain's: never named

    def run(argv, **kw):
        if argv[0].endswith("visudo"):
            p = _Proc(1, out)
            p.stderr = err
            return p
        return h.run(argv, **kw)
    live = RM.Live(run=run, root=live.root, fsync=h.fsync)
    plan = IH.plan_undo(parse_install_argv(["undo"]), _facts("darwin", _shape()), accounts={"_levain_s0": S0},
                        shared_groups=shared, live=live, end=lambda u, n: (True, ""))
    i, why = _walk(plan, h)
    assert i == 0, (i, why)
    assert f"sudo /bin/rm -f {RUNTIME_SUDOERS}" in why, why
    assert "/bin/rm -f /etc/sudoers.d/admin" not in why and "nothing" in why
    assert live.at(RUNTIME_SUDOERS).exists()
