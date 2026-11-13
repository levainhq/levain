"""The queue's ``answers/`` directory: only the operator may create a file in it.

Dispatch is the suspect principal. It is in the queue group (it reads run state in ``requests/``), so the answers
directory must not belong to that group. The check computes, from the layout rows and the account table, whether a
principal can create a file in each directory under the queue; it does not run any two-user probe.
"""
from __future__ import annotations

import pytest

from levain.slot.accounts import OPERATOR_GROUPS, supplementary_groups
from levain.slot.layout import STORE, base_store_rows

HOSTS = ("darwin", "linux")


def _groups(name: str) -> set[str]:
    return (set(OPERATOR_GROUPS) if name == "op" else set(supplementary_groups(name))) | {name}


def _can_create(row, name: str) -> bool:
    groups = _groups(name)
    if row.owner == name and row.mode & 0o300 == 0o300:
        return True
    if row.group in groups and row.mode & 0o030 == 0o030:
        return True
    return bool(row.mode & 0o003 == 0o003)


def _can_read(row, name: str) -> bool:
    groups = _groups(name)
    if row.owner == name:
        return bool(row.mode & 0o500 == 0o500)
    if row.group in groups:
        return bool(row.mode & 0o050 == 0o050)
    return bool(row.mode & 0o005 == 0o005)


@pytest.mark.parametrize("host", HOSTS)
def test_dispatch_cannot_create_in_any_queue_directory(host):
    L = STORE[host]
    rows = [r for r in base_store_rows(host, "op") if L / "queue" in r.path.parents]
    assert {r.path.name for r in rows} == {"requests", "answers"}
    for r in rows:
        assert not _can_create(r, "_levain_dispatch"), r.path


@pytest.mark.parametrize("host", HOSTS)
def test_the_mode_is_set_after_the_owner_so_the_setgid_bit_survives(host):
    """A chown clears the setgid bit of a directory it changes (Linux), so the mode step that sets 2750 must come
    after the owner step; the other order leaves 0750 and a new answer file takes the operator's own group."""
    from levain.slot.layout import row_steps

    answers = STORE[host] / "queue" / "answers"
    row = next(r for r in base_store_rows(host, "op") if r.path == answers)
    why = [s.why for s in row_steps(host, [row])]
    own = [i for i, w in enumerate(why) if w.startswith(f"own {answers} ")]
    mode = [i for i, w in enumerate(why) if w.startswith(f"set {answers} to ")]
    assert len(own) == 1 and len(mode) == 1, why
    assert own[0] < mode[0], why


@pytest.mark.parametrize("host", HOSTS)
def test_answers_is_the_operators_to_write_and_the_brokers_to_read(host):
    row = next(r for r in base_store_rows(host, "op") if r.path == STORE[host] / "queue" / "answers")
    assert row.owner == "op"
    assert _can_create(row, "op")
    assert not _can_create(row, "_levain_qbroker")
    assert _can_read(row, "_levain_qbroker")
    assert not _can_read(row, "_levain_dispatch")
    assert row.mode & 0o2000, "setgid, so a new file takes the broker's group on Linux"
