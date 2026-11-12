"""Root's ``read-allow`` verb (row 165): the bytes of the caller's own RETAINED session's ``.claude/settings.local.json``,
and nothing else. Driven through :func:`runtime_helper.main` on the scratch store of the discard / restore tests."""
from __future__ import annotations

import base64
import dataclasses
import os

import pytest

from levain.contracts import canon
from levain.slot import end as E
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot.runtime_grammar import parse_runtime_argv
from levain.slot import Refused
from tests.test_slot_rootverbs_discard_restore import DISPATCH_UID, OTHER, S, STRANGER_UID, UID, World

RULES = b'{"permissions": {"allow": ["Bash(npm test *)"]}}'


def _as_root(uid):
    """The scratch store's files are the test user's: they stand for the root-owned files END's chown leaves."""
    return os.getuid() if uid == 0 else uid


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.seams = dataclasses.replace(w.seams, chowned_uid=_as_root)
    return w


def claude_dir(world, s=S):
    return world.L / "retained" / s / "tree" / "repo" / ".claude"


def record(world, data=RULES, s=S):
    """END's ``allow`` provenance for ``data`` in the object's ``object.json`` (what :func:`end.allow_provenance`
    writes for the session owner's file); ``data=None`` removes it (an object retained before row 165)."""
    p = world.L / "retained" / s / "object.json"
    obj = canon.parse(p.read_bytes())
    obj.pop("allow", None)
    if data is not None:
        obj["allow"] = {"size": len(data), "digest": E.digest_bytes(data)}
    p.write_bytes(canon.canonical_bytes(obj))


def put(world, data=RULES, s=S, recorded=True):
    d = claude_dir(world, s)
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.local.json").write_bytes(data)
    if recorded:
        record(world, data, s)
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


def test_a_file_not_ends_root_owned_one_is_refused(world):
    put(world)
    world.seams = dataclasses.replace(world.seams, chowned_uid=lambda uid: uid)   # the file is not root's
    refused(world, "allow-not-slots")


def test_a_file_end_recorded_no_provenance_for_is_refused_unread(world, monkeypatch):
    # An object retained before row 165, or a file END did not find the session owner's: no ``allow`` record.
    f = put(world, recorded=False)
    ino = os.lstat(f).st_ino
    reads = []
    real = os.read

    def read(fd, n):
        if os.fstat(fd).st_ino == ino:
            reads.append(fd)
        return real(fd, n)
    monkeypatch.setattr(os, "read", read)
    refused(world, "allow-not-slots")
    assert reads == []                         # refused before a byte of it was read


def test_bytes_other_than_the_recorded_ones_are_refused(world):
    put(world)
    record(world, b'{"permissions": {"allow": ["Bash(rm *)"]}}')
    refused(world, "allow-not-slots")


@pytest.mark.parametrize("bad", [{"size": 1}, {"size": -1, "digest": "sha256:" + "0" * 64},
                                 {"size": True, "digest": "sha256:" + "0" * 64}, {"size": 1, "digest": "md5:x"},
                                 {"size": RH.ALLOW_CAP + 1, "digest": "sha256:" + "0" * 64}, "yes", None])
def test_a_malformed_record_is_no_record(world, bad):
    put(world)
    p = world.L / "retained" / S / "object.json"
    p.write_bytes(canon.canonical_bytes({**canon.parse(p.read_bytes()), "allow": bad}))
    refused(world, "allow-not-slots")


def test_a_held_session_lock_refuses_session_busy_and_reads_nothing(world):
    import fcntl
    put(world)
    fd = os.open(world.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(world, "session-busy")
    finally:
        os.close(fd)
    assert world.main("read-allow")[0] == RH.EXIT_OK


def test_a_discard_racing_the_read_cannot_move_the_tree_under_it(world, monkeypatch):
    # codex L3 MED: a discard between read-allow's state checks and its read would rename the object to trash while
    # the open directory fd still reads it. Run one from inside the read, after the checks: it is session-busy.
    put(world)
    before = world.where()
    raced = []
    real = RH._allow_dir

    def allow_dir(store_fd, session):
        raced.append(world.main("discard"))
        return real(store_fd, session)
    monkeypatch.setattr(RH, "_allow_dir", allow_dir)
    rc, line = world.main("read-allow")
    assert raced == [(RH.EXIT_REFUSED, {"ok": False, "refused": "session-busy"})]
    assert rc == RH.EXIT_OK and base64.b64decode(line["b64"]) == RULES
    assert world.where() == before and before[1] == ["retained"]


def test_a_store_journal_in_flight_is_refused(world):
    put(world)
    (world.L / "alloc" / f"{S}.intent.json").write_bytes(b"{}")
    refused(world, "intent-in-flight")


def test_the_dir_walk_leaks_no_fd_when_a_close_fails(world, monkeypatch):
    # codex L3 LOW: a failing close of the parent after the child opened must not leak the child.
    put(world)
    events = []                                # ("open" | "close", fd) in order: an fd number is reused
    real_open, real_close = os.open, os.close
    state = {"failed": False}

    def op(*a, **k):
        fd = real_open(*a, **k)
        events.append(("open", fd))
        return fd

    def cl(fd):
        events.append(("close", fd))
        real_close(fd)
        if not state["failed"] and sum(1 for k, _ in events if k == "open") >= 2:
            state["failed"] = True             # the parent's close fails after the child opened
            raise OSError(5, "EIO")
    store = real_open(world.L, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(os, "open", op)
    monkeypatch.setattr(os, "close", cl)
    try:
        with pytest.raises(OSError):
            RH._allow_dir(store, S)
    finally:
        monkeypatch.undo()
        real_close(store)
    assert state["failed"]
    for i, (kind, fd) in enumerate(events):
        if kind == "open":
            assert ("close", fd) in events[i + 1:], f"fd {fd} opened at event {i} was never closed: {events}"
