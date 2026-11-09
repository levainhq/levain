"""Root's ``read-allow`` verb (row 165): the bytes of the caller's own RETAINED session's ``.claude/settings.local.json``,
and nothing else. Driven through :func:`runtime_helper.main` on the scratch store of the discard / restore tests."""
from __future__ import annotations

import base64
import dataclasses
import os

import pytest

from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot.runtime_grammar import parse_runtime_argv
from levain.slot import Refused
from tests.test_slot_rootverbs_discard_restore import DISPATCH_UID, OTHER, S, STRANGER_UID, UID, World

RULES = b'{"permissions": {"allow": ["Bash(npm test *)"]}}'


SLOT_UID = 3990


def _as_slot(uid):
    """The scratch store's files are the test user's: they stand for files the slot account owns."""
    return os.getuid() if uid == SLOT_UID else uid


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.seams = dataclasses.replace(w.seams, chowned_uid=_as_slot)
    return w


def claude_dir(world, s=S):
    return world.L / "retained" / s / "tree" / "repo" / ".claude"


def put(world, data=RULES, s=S):
    d = claude_dir(world, s)
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.local.json").write_bytes(data)
    return d / "settings.local.json"


def refused(world, name, uid=UID, s=S):
    rc, line = world.main("read-allow", uid, s)
    assert (rc, line) == (RH.EXIT_REFUSED, {"ok": False, "refused": name})


def test_the_verb_is_listed_takes_one_session_and_dispatch_may_not_use_it():
    assert "read-allow" in RG.VERBS and "read-allow" in RG.DISPATCH_FORBIDDEN
    assert "read-allow" in RH.VERB_HANDLERS and "read-allow" not in RG.STDIN_VERBS
    req = parse_runtime_argv(["read-allow", S], configured_slots={}, caller=RG.OPERATOR)
    assert (req.verb, req.session, req.needs_lease) == ("read-allow", S, False)
    for argv, name in ((["read-allow"], "missing-argv"), (["read-allow", S, S], "extra-argv"),
                       (["read-allow", "../x"], "bad-token")):
        with pytest.raises(Refused) as e:
            parse_runtime_argv(argv, configured_slots={}, caller=RG.OPERATOR)
        assert e.value.name == name
    with pytest.raises(Refused) as e:
        parse_runtime_argv(["read-allow", S], configured_slots={}, caller=RG.DISPATCH)
    assert e.value.name == "verb-not-allowed-for-caller"


def test_the_file_comes_back_byte_for_byte(world):
    put(world)
    rc, line = world.main("read-allow")
    assert rc == RH.EXIT_OK and line["present"] is True
    assert base64.b64decode(line["b64"], validate=True) == RULES


def test_a_file_of_exactly_the_cap_comes_back(world):
    data = b" " * RH.ALLOW_CAP
    put(world, data)
    rc, line = world.main("read-allow")
    assert rc == RH.EXIT_OK and base64.b64decode(line["b64"]) == data


def test_an_absent_file_or_directory_answers_present_false(world):
    assert world.main("read-allow") == (RH.EXIT_OK, {"ok": True, "present": False})
    claude_dir(world).mkdir()
    assert world.main("read-allow") == (RH.EXIT_OK, {"ok": True, "present": False})


def test_another_operators_session_is_no_session_and_reads_nothing(world):
    put(world, s=OTHER)
    refused(world, "no-session", UID, OTHER)
    refused(world, "unknown-caller", STRANGER_UID)
    refused(world, "verb-not-allowed-for-caller", DISPATCH_UID)


def test_a_trashed_tree_is_not_read(world):
    put(world)
    assert world.main("discard")[0] == RH.EXIT_OK
    refused(world, "not-retained:trash")


def test_a_session_with_no_retained_tree_is_not_read(world):
    import shutil
    shutil.rmtree(world.L / "retained" / S)
    refused(world, "not-retained:absent")


def test_a_symlinked_claude_dir_is_refused_and_its_target_not_read(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.local.json").write_bytes(b"secret")
    claude_dir(world).symlink_to(outside)
    refused(world, "allow-unreadable")


def test_a_symlinked_file_is_refused(world, tmp_path):
    (tmp_path / "real.json").write_bytes(b"secret")
    d = claude_dir(world)
    d.mkdir()
    (d / "settings.local.json").symlink_to(tmp_path / "real.json")
    refused(world, "allow-unreadable")


def test_a_symlinked_tree_component_is_refused(world, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / ".claude").mkdir(parents=True)
    (outside / ".claude" / "settings.local.json").write_bytes(b"secret")
    repo = world.L / "retained" / S / "tree" / "repo"
    import shutil
    shutil.rmtree(repo)
    repo.symlink_to(outside)
    refused(world, "allow-unreadable")


def test_a_fifo_is_refused_without_being_opened(world):
    d = claude_dir(world)
    d.mkdir()
    os.mkfifo(d / "settings.local.json")
    refused(world, "allow-unreadable")          # an open for reading would block forever; the verb returned


def test_a_hard_linked_file_is_refused(world, tmp_path):
    target = tmp_path / "vendor-config.json"
    target.write_bytes(b"vendor")
    d = claude_dir(world)
    d.mkdir()
    os.link(target, d / "settings.local.json")
    refused(world, "allow-unreadable")


def test_a_file_past_the_cap_is_refused(world):
    put(world, b" " * (RH.ALLOW_CAP + 1))
    refused(world, "allow-unreadable")


def test_a_file_the_slot_account_does_not_own_is_refused(world):
    put(world)
    world.seams = dataclasses.replace(world.seams, chowned_uid=lambda uid: uid)   # the file is not the slot's
    refused(world, "allow-not-slots")
