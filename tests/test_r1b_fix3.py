"""Row 33 R1b FIX3 (HEAD 1022-20 rulings on r1b-fix2's Q1 sweep hit).

* Repair's ``account/1`` step retired a recorded uid only when the account read OURS, so a recorded account deleted by
  hand (ABSENT) left its uid reissuable: codex r2's HIGH class, on the repair side. Repair's retire decision now goes
  through the same predicate undo uses (:func:`levain.slot.accounts.uid_retires`), read from the live directory at the
  act. Rows, for repair: absent, ours at the uid, our tombstone at the uid, foreign at the uid, the name at another uid,
  and the name absent with another account at the uid. Each row's repair outcome equals undo's plan-time answer for
  the same directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.slot import accounts as A
from levain.slot import install_helper as IH
from levain.slot.accounts import SLOT_MARKER, SLOT_RETIRED_MARKER
from levain.slot.ids import RETIRED_IDS_PATH, read_retired_ids

from tests.test_repair_remove import _host, _run_all, _steps

UID = 3990
NAME = "_levain_s7"


def _rows(host: str):
    """(id, users to add: name -> [uid, label], retired?, skipped?)."""
    return [
        ("absent", {}, True, False),
        ("ours-at-uid", {NAME: [UID, SLOT_MARKER]}, True, False),
        ("our-tombstone-at-uid", {NAME: [UID, SLOT_RETIRED_MARKER[host]]}, True, False),
        ("foreign-at-uid", {NAME: [UID, "someone else's"]}, False, True),
        ("name-at-another-uid", {NAME: [UID + 1, SLOT_MARKER]}, False, True),
        ("absent-another-at-uid", {"someone": [UID, ""]}, False, False),
    ]


def _cases():
    return [(host, *row) for host in ("darwin", "linux") for row in _rows(host)]


@pytest.mark.parametrize("host,row,users,retired,skipped", _cases(), ids=lambda v: v if isinstance(v, str) else None)
def test_repair_retires_a_recorded_uid_by_undos_rule(tmp_path: Path, host, row, users, retired, skipped) -> None:
    h, live = _host(tmp_path, host)
    h.users.update({n: list(v) for n, v in users.items()})
    if host == "darwin" and row == "absent":
        h.groups[NAME] = [UID, "", set()]      # a hand-deleted macOS account leaves its primary group behind
    ended = []
    steps = [s for ph in IH.REMOVE_PHASES["account/1"]
             for s in IH.removal_steps(host, [("account/1", (NAME, UID))], ph, live=live,
                                       held=frozenset({("account/1", (NAME, UID))}),
                                       end=lambda uid, name: (ended.append(uid), (True, ""))[1])]
    assert all(ok for ok, _ in _run_all(steps)), h.log
    path = live.at(RETIRED_IDS_PATH[host])
    got = UID in read_retired_ids(path) if path.exists() else False
    assert got == retired, (row, h.log)
    assert (live.skips == {("account/1", (NAME, UID))}) == skipped, live.skips
    assert bool(ended) == (row == "ours-at-uid")              # only a live Levain account has processes to end

    # undo's plan-time answer from the same directory (gather_facts's split of it) is the same
    dir_users = {n: v[0] for n, v in h.users.items()}
    tombs = {n: v[0] for n, v in h.users.items() if v[1] == SLOT_RETIRED_MARKER[host]}
    accts = {n: v[0] for n, v in h.users.items() if v[1] == SLOT_MARKER}
    facts = IH.HostFacts(host=host, caller_uid=0, caller_name="root", operator_uid=None, operator_record=None,
                         live_leases=0, live_sessions=(), used_ids=frozenset(), existing_names=frozenset(),
                         retired=frozenset(), levain_tombstones=tombs, user_ids=dir_users)
    assert IH._uid_is_undos(facts, accts, NAME, UID) == retired


def test_undo_and_repair_decide_through_one_function(tmp_path: Path, monkeypatch) -> None:
    """Two things that should be one: both callers reach :func:`accounts.uid_retires`, and nothing else decides."""
    assert IH.uid_retires is A.uid_retires
    calls = []
    real = A.uid_retires

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(A, "uid_retires", spy)
    monkeypatch.setattr(IH, "uid_retires", spy)
    h, live = _host(tmp_path / "r", "linux")
    _run_all(_steps("linux", "account/1", (NAME, UID), live))
    facts = IH.HostFacts(host="linux", caller_uid=0, caller_name="root", operator_uid=None, operator_record=None,
                         live_leases=0, live_sessions=(), used_ids=frozenset(), existing_names=frozenset(),
                         retired=frozenset())
    IH._uid_is_undos(facts, {}, NAME, UID)
    assert [c[:2] for c in calls] == [(NAME, UID), (NAME, UID)]


def test_undo_passes_retire_false_and_retires_nothing_at_the_step(tmp_path: Path) -> None:
    """Undo retires uids itself (``retired_after``); its account step, retire=False, writes no retired-ids file."""
    h, live = _host(tmp_path, "linux")
    steps = [s for ph in IH.REMOVE_PHASES["account/1"]
             for s in IH.removal_steps("linux", [("account/1", (NAME, UID))], ph, live=live, retire=False,
                                       held=frozenset(), end=lambda uid, name: (True, ""))]
    assert all(ok for ok, _ in _run_all(steps))
    assert not live.at(RETIRED_IDS_PATH["linux"]).exists()
