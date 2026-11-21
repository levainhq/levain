"""Root's ``read-report`` verb (levain#225): the bytes of the one report file ``repo/.levain-report.md`` of the caller's
own RETAINED session, bounded, and nothing else. The file is untrusted data: no provenance, only shape. Driven through
:func:`runtime_helper.main` on the scratch store of the discard / restore and read-allow tests."""
from __future__ import annotations

import base64
import dataclasses
import os

import pytest

from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot import Refused
from levain.slot.runtime_grammar import parse_runtime_argv
from tests.test_slot_rootverbs_discard_restore import DISPATCH_UID, OTHER, S, STRANGER_UID, UID, World

REPORT = "# Done\n\nShipped the thing. Naive: café.\n".encode("utf-8")


def _as_root(uid):
    """The scratch store's files are the test user's: they stand for the root-owned files END's chown leaves."""
    return os.getuid() if uid == 0 else uid


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.seams = dataclasses.replace(w.seams, chowned_uid=_as_root)
    return w


def repo_dir(world, s=S):
    return world.L / "retained" / s / "tree" / "repo"


def put(world, data=REPORT, s=S):
    p = repo_dir(world, s) / ".levain-report.md"
    p.write_bytes(data)
    return p


def refused(world, name, uid=UID, s=S):
    rc, line = world.main("read-report", uid, s)
    assert (rc, line) == (RH.EXIT_REFUSED, {"ok": False, "refused": name})


def as_dispatch(world, s=S):
    """Make ``s`` a ticketed dispatch session whose supervisor is ``_levain_dispatch``."""
    p = world.L / "alloc" / f"{s}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(dataclasses.replace(a, kind="dispatch", supervisor_uid=DISPATCH_UID,
                                                    staging_uid=DISPATCH_UID, ticket=C.new_handle(),
                                                    ticket_facts=_FACTS)))


_FACTS = {"kind": "executor", "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
          "route": "r", "model_requested": "m-1", "egress_routes": ["github"], "reserved_microdollars": 1000,
          "routemap_generation": 3, "route_policy_digest": "sha256:" + "4" * 64,
          "brief_digest": "sha256:" + "5" * 64}


def test_the_verb_is_listed_takes_one_session_and_dispatch_may_call_it():
    assert "read-report" in RG.VERBS and "read-report" not in RG.DISPATCH_FORBIDDEN
    assert "read-report" in RH.VERB_HANDLERS and "read-report" not in RG.STDIN_VERBS
    assert "read-report" not in RG.LEASE_VERBS
    assert RG.REPORT_REL == (b"repo", b".levain-report.md") and RG.REPORT_CAP == 32 * 1024
    for caller in (RG.OPERATOR, RG.DISPATCH):
        req = parse_runtime_argv(["read-report", S], configured_slots={}, caller=caller)
        assert (req.verb, req.session, req.needs_lease) == ("read-report", S, False)
    for argv, name in ((["read-report"], "missing-argv"), (["read-report", S, S], "extra-argv"),
                       (["read-report", "../x"], "bad-token")):
        with pytest.raises(Refused) as e:
            parse_runtime_argv(argv, configured_slots={}, caller=RG.DISPATCH)
        assert e.value.name == name


def test_the_verb_is_in_the_dispatch_sudoers_rule():
    from levain.slot.sudoers import DISPATCH_VERBS
    assert "read-report" in DISPATCH_VERBS


def test_the_report_comes_back_byte_for_byte(world):
    put(world)
    rc, line = world.main("read-report")
    assert rc == RH.EXIT_OK and line["present"] is True and set(line) == {"ok", "present", "b64"}
    assert base64.b64decode(line["b64"], validate=True) == REPORT


def test_a_report_of_exactly_the_cap_comes_back(world):
    data = b"a" * RH.REPORT_CAP
    put(world, data)
    rc, line = world.main("read-report")
    assert rc == RH.EXIT_OK and base64.b64decode(line["b64"]) == data


def test_an_absent_file_answers_present_false(world):
    assert world.main("read-report") == (RH.EXIT_OK, {"ok": True, "present": False})


def test_an_absent_repo_directory_answers_present_false(world):
    import shutil
    shutil.rmtree(repo_dir(world))
    assert world.main("read-report") == (RH.EXIT_OK, {"ok": True, "present": False})


def test_the_dispatch_caller_reads_its_own_ticketed_session(world):
    as_dispatch(world)
    put(world)
    rc, line = world.main("read-report", DISPATCH_UID)
    assert rc == RH.EXIT_OK and base64.b64decode(line["b64"]) == REPORT


def test_a_dispatch_caller_is_refused_a_session_that_is_not_its_own(world):
    put(world)                                     # S's supervisor is the operator, and it carries no ticket
    refused(world, "no-session", DISPATCH_UID, S)
    as_dispatch(world, OTHER)                      # ticketed, but its supervisor is another uid than the caller's
    p = world.L / "alloc" / f"{OTHER}.json"
    p.write_bytes(R.alloc_bytes(dataclasses.replace(R.parse_alloc(p.read_bytes()), supervisor_uid=STRANGER_UID)))
    put(world, s=OTHER)
    refused(world, "no-session", DISPATCH_UID, OTHER)
    refused(world, "unknown-caller", STRANGER_UID)


def test_another_operators_session_is_no_session_and_reads_nothing(world):
    put(world, s=OTHER)
    refused(world, "no-session", UID, OTHER)


def test_a_trashed_tree_is_not_read(world):
    put(world)
    assert world.main("discard")[0] == RH.EXIT_OK
    refused(world, "not-retained:trash")


def test_a_session_with_no_retained_tree_is_not_read(world):
    import shutil
    shutil.rmtree(world.L / "retained" / S)
    refused(world, "not-retained:absent")


def test_a_symlinked_report_is_refused_and_its_target_not_read(world, tmp_path):
    (tmp_path / "real.md").write_bytes(b"secret")
    (repo_dir(world) / ".levain-report.md").symlink_to(tmp_path / "real.md")
    refused(world, "report-unreadable")


# a linked tree is already not a whole retained tree (the state gate), so it never reaches the walk
@pytest.mark.parametrize("component,name", [("repo", "report-unreadable"), ("tree", "not-retained:absent")])
def test_a_symlinked_path_component_is_refused(world, tmp_path, component, name):
    import shutil
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    if component == "repo":
        shutil.rmtree(repo_dir(world))
        (outside / ".levain-report.md").write_bytes(b"secret")
        repo_dir(world).symlink_to(outside)
    else:
        tree = world.L / "retained" / S / "tree"
        (outside / "repo").mkdir()
        (outside / "repo" / ".levain-report.md").write_bytes(b"secret")
        shutil.rmtree(tree)
        tree.symlink_to(outside)
    refused(world, name)


def test_a_directory_in_the_files_place_is_refused(world):
    (repo_dir(world) / ".levain-report.md").mkdir()
    refused(world, "report-unreadable")


def test_a_fifo_is_refused_without_being_opened(world):
    os.mkfifo(repo_dir(world) / ".levain-report.md")
    refused(world, "report-unreadable")            # an open for reading would block forever; the verb returned


def test_a_hard_linked_report_is_refused(world, tmp_path):
    target = tmp_path / "vendor-notes.md"
    target.write_bytes(b"vendor")
    os.link(target, repo_dir(world) / ".levain-report.md")
    refused(world, "report-unreadable")


def test_a_report_not_ends_root_owned_one_is_refused(world):
    put(world)
    world.seams = dataclasses.replace(world.seams, chowned_uid=lambda uid: uid)   # the file is not root's
    refused(world, "report-unreadable")


def test_a_report_past_the_cap_is_oversize(world):
    put(world, b"a" * (RH.REPORT_CAP + 1))
    refused(world, "report-oversize")


def test_a_file_that_grows_past_the_cap_while_read_is_oversize(world, monkeypatch):
    # st_size says it fits; the bounded read stops at cap + 1 and refuses.
    p = put(world, b"a" * 10)
    ino = os.stat(p).st_ino
    real_read = os.read

    def read(fd, n):
        return b"a" * n if os.fstat(fd).st_ino == ino else real_read(fd, n)
    monkeypatch.setattr(os, "read", read)
    refused(world, "report-oversize")


@pytest.mark.parametrize("data", [b"", b"line one\n\x00line two\n", b"\xff\xfe not utf-8", b"\xc3\x28", b"\xed\xa0\x80"])
def test_an_empty_nul_or_non_utf8_report_is_malformed(world, data):
    put(world, data)
    refused(world, "report-malformed")


def test_a_held_session_lock_refuses_session_busy_and_reads_nothing(world):
    import fcntl
    put(world)
    fd = os.open(world.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(world, "session-busy")
    finally:
        os.close(fd)
    assert world.main("read-report")[0] == RH.EXIT_OK


def test_a_store_journal_in_flight_is_refused(world):
    put(world)
    (world.L / "alloc" / f"{S}.intent.json").write_bytes(b"{}")
    refused(world, "intent-in-flight")


def test_a_discard_racing_the_read_cannot_move_the_tree_under_it(world, monkeypatch):
    put(world)
    before = world.where()
    raced = []
    real = RH._allow_dir

    def allow_dir(*a, **k):
        raced.append(world.main("discard"))
        return real(*a, **k)
    monkeypatch.setattr(RH, "_allow_dir", allow_dir)
    rc, line = world.main("read-report")
    assert raced == [(RH.EXIT_REFUSED, {"ok": False, "refused": "session-busy"})]
    assert rc == RH.EXIT_OK and base64.b64decode(line["b64"]) == REPORT
    assert world.where() == before and before[1] == ["retained"]


def test_an_unmapped_os_error_at_a_directory_component_is_report_unreadable(world, monkeypatch):
    put(world)
    real_open = os.open

    def deny(path, flags, *a, **k):
        if path == "repo" and k.get("dir_fd") is not None:
            raise PermissionError(13, "denied")
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", deny)
    refused(world, "report-unreadable")


def test_read_allow_still_walks_its_own_directory_and_names(world):
    d = repo_dir(world) / ".claude"
    d.mkdir()
    (repo_dir(world) / ".claude").rmdir()
    (repo_dir(world) / ".claude").symlink_to(world.L)
    assert world.main("read-allow") == (RH.EXIT_REFUSED, {"ok": False, "refused": "allow-unreadable"})
