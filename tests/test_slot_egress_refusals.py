"""Host grants (board levain#66 slice S2, plan hgs2_plan_v2 T20): ``egress-refusals <session> <cursor>`` answers the hosts
this session's front refused as ``host-not-allowed``/``off-list``, read from the slot's trusted ``L/gw/slot<n>/front.log``
(and its rotated ``front.log.1``) behind a cursor that survives rotation, with a per-call work cap and a host cap."""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from levain.slot import runtime_helper as RH
from tests.test_slot_egress_grant_verbs import OTHER, S, World, refused, world  # noqa: F401


@pytest.fixture
def lw(world):
    """The grant world with the front's log dir ``L/gw/slot3`` (the test user stands for the front account)."""
    d = world.L / "gw" / "slot3"
    d.mkdir(parents=True)
    d.chmod(0o750)
    (world.L / "gw").chmod(0o755)
    world.seams = dataclasses.replace(world.seams, chowned_uid=lambda uid: os.getuid())
    world.log_dir = d
    return world


def line(host, *, session=S, **over):
    rec = {"at": 1.0, "session": session, "event": "refused", "refused": "host-not-allowed", "detail": "off-list",
           "host": host}
    rec.update(over)
    return json.dumps(rec, sort_keys=True) + "\n"


def write_log(w, text, name="front.log"):
    p = w.log_dir / name
    p.write_text(text)
    p.chmod(0o640)
    return p


def ask(w, cursor="0", session=S, uid=None):
    argv = ["egress-refusals", session, cursor]
    return w.main(argv) if uid is None else w.main(argv, uid=uid)


def hosts_of(ans):
    assert ans[0] == RH.EXIT_OK and ans[1]["ok"] is True, ans
    return ans[1]["hosts"]


def test_only_this_sessions_off_list_host_refusals_are_answered_deduped_in_order(lw):
    write_log(lw, line("b.example.org") + line("a.example.org") + line("b.example.org")
              + line("other.example.org", session=OTHER) + line("c.example.org", refused="malformed")
              + line("d.example.org", detail="port") + line("e.example.org", event="tunnel")
              + json.dumps({"session": S, "event": "refused", "refused": "host-not-allowed", "detail": "off-list",
                            "host_invalid": True, "host_len": 3}) + "\n"
              + "not json\n")
    ans = ask(lw)
    assert hosts_of(ans) == ["b.example.org", "a.example.org"]
    assert ans[1]["cursor"].endswith("-%d" % (lw.log_dir / "front.log").stat().st_size)


def test_forged_extra_fields_a_bad_host_and_a_host_with_host_invalid_are_never_answered(lw):
    write_log(lw, line("UPPER.example.org") + line("bad host") + line("x" * 300 + ".org")
              + line(["not", "a", "string"]) + line("flag.example.org", host_invalid=True)
              + line("1.2.3.4") + line("good.example.org", remedy="x", extra="y"))
    assert hosts_of(ask(lw)) == ["good.example.org"]


def test_a_burst_is_capped_at_eight_hosts_and_the_cursor_resumes_after_the_eighth(lw):
    write_log(lw, "".join(line("h%d.example.org" % i) for i in range(20)))
    first = ask(lw)
    assert hosts_of(first) == ["h%d.example.org" % i for i in range(8)]
    second = ask(lw, first[1]["cursor"])
    assert hosts_of(second) == ["h%d.example.org" % i for i in range(8, 16)]
    third = ask(lw, second[1]["cursor"])
    assert hosts_of(third) == ["h%d.example.org" % i for i in range(16, 20)]
    assert hosts_of(ask(lw, third[1]["cursor"])) == []


def test_a_partial_last_line_does_not_advance_the_cursor_and_is_read_whole_next_time(lw):
    p = write_log(lw, line("a.example.org") + line("b.example.org")[:25])
    one = ask(lw)
    assert hosts_of(one) == ["a.example.org"]
    p.write_text(p.read_text() + line("b.example.org")[25:])
    assert hosts_of(ask(lw, one[1]["cursor"])) == ["b.example.org"]


def test_a_log_with_nothing_new_answers_no_hosts_and_the_same_cursor(lw):
    write_log(lw, line("a.example.org"))
    one = ask(lw)
    two = ask(lw, one[1]["cursor"])
    assert hosts_of(two) == [] and two[1]["cursor"] == one[1]["cursor"]


def test_no_log_yet_is_no_hosts_and_cursor_zero(lw):
    ans = ask(lw)
    assert ans == (RH.EXIT_OK, {"ok": True, "hosts": [], "cursor": "0"})


def test_rotation_finishes_the_old_log_from_the_offset_then_reads_the_new_one_from_zero(lw):
    p = write_log(lw, line("a.example.org"))
    one = ask(lw)
    assert hosts_of(one) == ["a.example.org"]
    ino = p.stat().st_ino
    p.write_text(p.read_text() + line("b.example.org"))          # grew past the cursor ...
    os.rename(p, lw.log_dir / "front.log.1")                     # ... then rotated (same inode now front.log.1)
    write_log(lw, line("c.example.org"))
    assert (lw.log_dir / "front.log.1").stat().st_ino == ino
    assert hosts_of(ask(lw, one[1]["cursor"])) == ["b.example.org", "c.example.org"]


def test_an_inode_matching_neither_log_restarts_at_zero_of_the_current_log(lw):
    write_log(lw, line("a.example.org"))
    assert hosts_of(ask(lw, "999999999-5")) == ["a.example.org"]


def test_a_cursor_offset_past_the_end_of_the_log_it_names_is_refused(lw):
    p = write_log(lw, line("a.example.org"))
    refused(ask(lw, "%d-%d" % (p.stat().st_ino, p.stat().st_size + 1)), "egress-refusals-cursor")


def test_a_megabyte_of_other_events_stops_at_the_work_cap_with_a_cursor_and_the_next_call_goes_on(lw):
    pad = line("pad.example.org", session=OTHER)
    n = (1 << 20) // len(pad) + 1
    write_log(lw, pad * n + line("late.example.org"))
    one = ask(lw)
    assert hosts_of(one) == []
    off = int(one[1]["cursor"].split("-")[1])
    assert 0 < off <= 256 * 1024
    cur = one[1]["cursor"]
    got = []
    for _ in range(8):
        step = ask(lw, cur)
        got += hosts_of(step)
        cur = step[1]["cursor"]
    assert got == ["late.example.org"]


def test_a_line_longer_than_the_work_cap_cannot_wedge_the_cursor(lw):
    write_log(lw, "x" * (300 * 1024) + "\n" + line("after.example.org"))
    cur, got = "0", []
    for _ in range(6):
        step = ask(lw, cur)
        got += hosts_of(step)
        cur = step[1]["cursor"]
    assert got == ["after.example.org"]


def test_a_symlinked_or_hard_linked_or_foreign_log_is_refused(lw, tmp_path):
    real = tmp_path / "real.log"
    real.write_text(line("a.example.org"))
    (lw.log_dir / "front.log").symlink_to(real)
    refused(ask(lw), "egress-refusals-log")
    (lw.log_dir / "front.log").unlink()
    os.link(real, lw.log_dir / "front.log")
    refused(ask(lw), "egress-refusals-log")
    (lw.log_dir / "front.log").unlink()
    write_log(lw, line("a.example.org"))
    lw.seams = dataclasses.replace(lw.seams, chowned_uid=lambda uid: os.getuid() + 1)
    refused(ask(lw), "egress-refusals-log")


def test_a_log_dir_that_is_not_the_fronts_alone_is_refused(lw):
    write_log(lw, line("a.example.org"))
    lw.log_dir.chmod(0o770)
    refused(ask(lw), "egress-refusals-log")


def test_a_fifo_for_front_log_is_refused_not_blocked_on(lw):
    os.mkfifo(lw.log_dir / "front.log")
    refused(ask(lw), "egress-refusals-log")


def test_the_session_must_be_the_callers(lw):
    write_log(lw, line("a.example.org"))
    refused(ask(lw, session=OTHER), "no-session")


def test_an_alloc_on_a_none_class_slot_is_not_an_egress_session(lw):
    from levain.slot import records as R
    from levain.slot.ids import SlotId
    a = R.Alloc(session=S, slot=SlotId("none", 1), lease_seq=1, slot_rev=1, slot_uid=3980, slot_gid=3980,
                kind="verifier", vendor="claude", auth="none", operator_uid=os.getuid(), supervisor_uid=os.getuid(),
                staging_uid=os.getuid(), created_at="2026-11-16T11:00:00Z",
                boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f", child_uid=3979)
    (lw.L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(a))
    refused(ask(lw), "egress-grant-class")


def test_a_log_file_owned_by_neither_the_front_nor_root_is_refused(lw, monkeypatch):
    """The dir walk passes (the dir is the front's); only the FILE's owner is wrong."""
    write_log(lw, line("a.example.org"))
    from levain.slot import runtime_helper as H
    walked = {"done": False}
    real = H._open_front_log_dir

    def walk(*a, **kw):
        out = real(*a, **kw)
        walked["done"] = True
        return out

    monkeypatch.setattr(H, "_open_front_log_dir", walk)
    lw.seams = dataclasses.replace(lw.seams, chowned_uid=lambda uid: os.getuid() + 1 if walked["done"] else os.getuid())
    refused(ask(lw), "egress-refusals-log")


# --- L3 r1 -------------------------------------------------------------------------------------------------------

def test_a_refusal_line_after_the_rotated_log_used_most_of_the_budget_is_not_skipped(lw):
    """complement 2: the rotated file leaves 100 bytes of the shared budget; the first line of ``front.log`` is a whole
    refusal longer than that. It must be read on the next call, never skipped as junk (junk is a line that fills a
    WHOLE fresh budget)."""
    from levain.slot import egress_verbs as EVB
    junk = []
    left = EVB.REFUSALS_SCAN_MAX - 100
    while left:
        n = min(left, 60000)
        junk.append("y" * (n - 1) + "\n")
        left -= n
    rot = write_log(lw, "".join(junk), name="front.log.1")
    keep = line("keep.example.org", pad="z" * 60)
    assert len(keep) > 100
    write_log(lw, keep)
    cur, got = "%d-0" % rot.stat().st_ino, []
    for _ in range(3):
        step = ask(lw, cur)
        got += hosts_of(step)
        cur = step[1]["cursor"]
    assert got == ["keep.example.org"]


def test_a_refused_rotated_log_closes_the_current_log_it_already_opened(lw, tmp_path):
    """complement 3 / gemini 3: ``front.log`` opened, ``front.log.1`` refused (a hard link): no descriptor leaks."""
    write_log(lw, line("a.example.org"))
    other = tmp_path / "other.log"
    other.write_text(line("b.example.org"))
    os.link(other, lw.log_dir / "front.log.1")
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        refused(ask(lw), "egress-refusals-log")
    assert len(os.listdir("/dev/fd")) == before
