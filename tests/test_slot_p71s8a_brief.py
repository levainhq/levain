"""p71 S8a (key 1104-8): the `brief_digest` ticket fact and the `stage_brief` / `write_brief` primitives
(DESIGN_p71_s8 D2, D3). Refusal tests first; the seams are test_slot_s2b_stage.py's."""
from __future__ import annotations

import os
import stat
import struct
import threading

import pytest

from levain.contracts import canon
from levain.slot import Refused, write_all
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import stage as st
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests.test_slot_s2b_stage import UID, GID, _fstat_hook, refused, seams, sha  # noqa: F401
from tests.test_slot_s2f2_runtime_helper import BOOT
from tests.test_routemaps import st as store  # noqa: F401 - the signed route-map store fixture
from tests.test_tickets import SESSION, Rig, Clock, mint_args, refused as ans_refused, ticket_of  # noqa: F401

BRIEF = "Fix the bug.\n\tTabbed \"quoted\" line, naïve café ✓\r\n# markdown\n"


@pytest.fixture()
def dirs(tmp_path):
    staging = tmp_path / "staging"
    dest = tmp_path / "in"
    staging.mkdir()
    dest.mkdir()
    return staging, dest


def put(staging, data: bytes, name: str = "brief.md"):
    (staging / name).write_bytes(data)
    return st.brief_digest(data)


def stage(staging, dest, digest, **kw):
    return st.stage_brief(staging, dest, staging_uid=UID, staging_gid=GID, dest_gid=1234, brief_digest=digest,
                          **seams(**kw))


def clean(dest):
    assert list(dest.iterdir()) == []


# ---- stage_brief refusals ---------------------------------------------------------------------------------------------

def test_01_no_digest_refused_before_any_fork(dirs):
    staging, dest = dirs
    put(staging, b"x")
    started = []

    def run_as(*a, **k):
        started.append(1)
        raise AssertionError("the reader was started")

    assert refused(stage, staging, dest, None, run_as=run_as).name == "brief-mismatch"
    assert started == []
    clean(dest)


def test_02_bad_digest(dirs):
    staging, dest = dirs
    put(staging, b"x")
    assert refused(stage, staging, dest, "sha256:XYZ").name == "bad-digest"
    clean(dest)


def test_03_staging_uid_root(dirs):
    staging, dest = dirs
    d = put(staging, b"x")
    e = refused(st.stage_brief, staging, dest, staging_uid=0, staging_gid=GID, dest_gid=1234, brief_digest=d,
                **seams())
    assert e.name == "staging-uid-root"
    clean(dest)


def test_04_missing(dirs):
    staging, dest = dirs
    assert refused(stage, staging, dest, st.brief_digest(b"x")).name == "staging-missing"
    clean(dest)


def test_05_symlink(dirs, tmp_path):
    staging, dest = dirs
    target = tmp_path / "real"
    target.write_bytes(b"hello")
    os.symlink(target, staging / "brief.md")
    assert refused(stage, staging, dest, st.brief_digest(b"hello")).name == "staging-not-regular"
    assert target.read_bytes() == b"hello"
    clean(dest)


def test_06_fifo_does_not_hang(dirs):
    staging, dest = dirs
    os.mkfifo(staging / "brief.md")
    box: dict = {}

    def run():
        try:
            stage(staging, dest, st.brief_digest(b"x"), deadline_s=5.0)
        except Refused as e:
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "a FIFO hung stage_brief"
    assert box["e"].name == "staging-not-regular"
    clean(dest)


def test_07_directory(dirs):
    staging, dest = dirs
    (staging / "brief.md").mkdir()
    assert refused(stage, staging, dest, st.brief_digest(b"x")).name == "staging-not-regular"
    clean(dest)


def test_08_over_cap(dirs):
    staging, dest = dirs
    d = put(staging, b"a" * (st.BRIEF_CAP + 1))
    assert refused(stage, staging, dest, d).name == "brief-cap"
    clean(dest)


def test_08b_root_rechecks_cap_the_reader_sent(dirs):
    staging, dest = dirs
    d = put(staging, b"a")

    def liar(uid, gid, fn, wfd, keep=()):
        write_all(wfd, b"S" + struct.pack(">Q", st.BRIEF_CAP + 1))
        os.close(wfd)
        t = threading.Thread(target=lambda: None)
        t.start()
        return st._Threaded(t, threading.Event())

    assert refused(stage, staging, dest, d, run_as=liar).name == "brief-cap"
    clean(dest)


def test_09_empty(dirs):
    staging, dest = dirs
    d = put(staging, b"")
    e = refused(stage, staging, dest, d)
    assert (e.name, e.detail) == ("brief-invalid", "empty")
    clean(dest)


def test_10_wrong_digest(dirs):
    staging, dest = dirs
    put(staging, b"hello")
    assert refused(stage, staging, dest, st.brief_digest(b"hellp")).name == "brief-mismatch"
    clean(dest)


def test_11_nul(dirs):
    staging, dest = dirs
    d = put(staging, b"ab\x00cd")
    e = refused(stage, staging, dest, d)
    assert (e.name, e.detail) == ("brief-invalid", "NUL")
    clean(dest)


@pytest.mark.parametrize("bad", [b"\xff\xfe", b"\xc0\xaf", b"\xed\xa0\x80"], ids=["ff-fe", "overlong", "surrogate"])
def test_12_not_utf8(dirs, bad):
    staging, dest = dirs
    d = put(staging, bad)
    e = refused(stage, staging, dest, d)
    assert (e.name, e.detail) == ("brief-invalid", "utf-8")
    clean(dest)


def test_13_order_digest_before_validate(dirs):
    staging, dest = dirs
    put(staging, b"ab\x00cd")
    assert refused(stage, staging, dest, st.brief_digest(b"other")).name == "brief-mismatch"
    clean(dest)


def test_14_destination_exists(dirs):
    staging, dest = dirs
    d = put(staging, b"hello")
    (dest / "brief.md").write_bytes(b"mine")
    assert refused(stage, staging, dest, d).name == "staging-dest-exists"
    assert (dest / "brief.md").read_bytes() == b"mine"


def test_15_raced_grow(dirs, monkeypatch):
    staging, dest = dirs
    p = staging / "brief.md"
    d = put(staging, b"hello")

    def grow():
        with open(p, "ab") as f:
            f.write(b"!")

    _fstat_hook(monkeypatch, p, grow)
    assert refused(stage, staging, dest, d).name == "staging-raced"
    clean(dest)


def test_15b_raced_shrink(dirs, monkeypatch):
    staging, dest = dirs
    p = staging / "brief.md"
    d = put(staging, b"hello world")
    _fstat_hook(monkeypatch, p, lambda: os.truncate(p, 3))
    assert refused(stage, staging, dest, d).name == "staging-raced"
    clean(dest)


# ---- stage_brief success ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data", [st.brief_bytes(BRIEF), b"a" * st.BRIEF_CAP], ids=["mixed", "exactly-cap"])
def test_16_success(dirs, data):
    staging, dest = dirs
    d = put(staging, data)
    chowned: list = []
    r = stage(staging, dest, d, chowned=chowned)
    out = dest / "brief.md"
    assert out.read_bytes() == data
    assert stat.S_IMODE(out.stat().st_mode) == 0o440
    assert chowned == [(0, 1234)]
    assert (r.size, r.digest) == (len(data), st.brief_digest(data))


def test_16b_chown_called_with_fd_root_dest_gid(dirs):
    staging, dest = dirs
    d = put(staging, b"hi")
    calls: list = []
    stage(staging, dest, d, chown=lambda fd, u, g: calls.append((type(fd), u, g)))
    assert calls == [(int, 0, 1234)]


def test_constants():
    assert st.BRIEF_CAP == 256 * 1024 and st.BRIEF_NAME == "brief.md"
    assert "brief-cap" in st._READER_REFUSALS


# ---- the pure helpers -------------------------------------------------------------------------------------------------

def test_brief_bytes_exact_and_surrogate():
    assert st.brief_bytes("a\n") == b"a\n" and st.brief_bytes("é") == b"\xc3\xa9"
    assert st.brief_bytes("﻿x") == b"\xef\xbb\xbfx"                      # no BOM handling either way
    e = refused(st.brief_bytes, "a\ud800b")
    assert (e.name, e.detail) == ("brief-invalid", "utf-8")


def test_validate_allows_tabs_cr_markdown():
    st.validate_brief(b"\t\r\n# x\n\xc3\xa9")


# ---- write_brief ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "a\x00b"], ids=["empty", "nul"])
def test_17_invalid_text_never_written(dirs, bad):
    staging, _ = dirs
    assert refused(st.write_brief, staging, bad).name == "brief-invalid"
    assert list(staging.iterdir()) == []


def test_17b_surrogate_never_written(dirs):
    staging, _ = dirs
    assert refused(st.write_brief, staging, "\ud800").name == "brief-invalid"
    assert list(staging.iterdir()) == []


def test_18_existing_name_not_overwritten(dirs):
    staging, _ = dirs
    (staging / "brief.md").write_bytes(b"old")
    assert refused(st.write_brief, staging, "new").name == "staging-dest-exists"
    assert (staging / "brief.md").read_bytes() == b"old"


def test_19_symlink_at_the_name(dirs, tmp_path):
    staging, _ = dirs
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    os.symlink(target, staging / "brief.md")
    assert refused(st.write_brief, staging, "new").name == "staging-dest-exists"
    assert target.read_bytes() == b"keep"
    os.unlink(staging / "brief.md")
    os.symlink(tmp_path / "dangling", staging / "brief.md")
    assert refused(st.write_brief, staging, "new").name == "staging-dest-exists"
    assert not (tmp_path / "dangling").exists()


def test_20_round_trip(dirs):
    staging, dest = dirs
    d = st.write_brief(staging, BRIEF)
    assert stat.S_IMODE((staging / "brief.md").stat().st_mode) == 0o600
    assert d == st.brief_digest(st.brief_bytes(BRIEF))
    r = stage(staging, dest, d)
    assert (dest / "brief.md").read_bytes() == st.brief_bytes(BRIEF) and r.digest == d


def test_20b_dirfd_form(dirs):
    staging, _ = dirs
    fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st.write_brief(fd, "x")
        os.fstat(fd)                                                           # the caller's fd stays open
    finally:
        os.close(fd)
    assert (staging / "brief.md").read_bytes() == b"x"


# ---- tickets ----------------------------------------------------------------------------------------------------------

BD = "sha256:" + "a" * 64
EX = mint_args("a-1")
EX.pop("attempt_id")
PT = {"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64, "kind": "executor",
      "auth": "passthrough", "vendor": "claude", "egress_routes": [], "reserved_microdollars": 0, "max_calls": 400,
      "max_tunnel_bytes": 400 * 2**20, "deadline_s": 7200}
VER = {"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64, "kind": "verifier",
       "egress_routes": [], "reserved_microdollars": 0, "verifier_generation": 1, "verifier_id": "pytest-report",
       "verifier_steps": [{"argv": ["pytest", "-q"], "wall_s": 60}], "input_bundle_digest": "sha256:" + "2" * 64,
       "driver_input_digest": "sha256:" + "3" * 64}


def field(name="brief_digest"):
    return "ticket-field:" + name


def test_21_verifier_forbidden():
    with pytest.raises(Refused) as ei:
        T.check_facts(dict(VER, brief_digest=BD))
    assert ei.value.name == field() and "verifier" in ei.value.detail


@pytest.mark.parametrize("base", [EX, PT], ids=["brokered", "passthrough"])
def test_22_planner_forbidden(base):
    with pytest.raises(Refused) as ei:
        T.check_facts(dict(base, kind="planner", brief_digest=BD))
    assert ei.value.name == field()


def test_23_passthrough_codex_forbidden():
    with pytest.raises(Refused) as ei:
        T.check_facts(dict(PT, vendor="codex", brief_digest=BD))
    assert ei.value.name == field() and "codex" in ei.value.detail
    assert "brief_digest" not in T.check_facts(dict(PT, vendor="codex"))


@pytest.mark.parametrize("bad", ["sha256:XYZ", "sha256:" + "A" * 64, "", None, 7, "sha256:" + "a" * 63],
                         ids=lambda b: repr(b)[:12])
@pytest.mark.parametrize("base", [EX, PT], ids=["brokered", "passthrough"])
def test_24_malformed_refused(base, bad):
    with pytest.raises(Refused) as ei:
        T.check_facts(dict(base, brief_digest=bad))
    assert ei.value.name == field()


@pytest.mark.parametrize("base", [EX, PT], ids=["brokered", "passthrough"])
def test_25_optional_present_or_absent(base):
    assert T.check_facts(dict(base, brief_digest=BD))["brief_digest"] == BD
    assert "brief_digest" not in T.check_facts(dict(base))


@pytest.mark.parametrize("base", [EX, PT], ids=["brokered", "passthrough"])
def test_26_recheck_is_identity(base):
    facts = T.check_facts(dict(base, brief_digest=BD))
    assert T.check_facts(facts) == facts


def test_27_request_digest_covers_it():
    a = T.request_digest(T.check_facts(dict(EX, brief_digest=BD)))
    b = T.request_digest(T.check_facts(dict(EX, brief_digest="sha256:" + "b" * 64)))
    assert a != b != T.request_digest(T.check_facts(dict(EX)))


def test_27b_same_attempt_different_brief_conflicts(store):
    rig = Rig(store, Clock())
    ticket_of(rig.mint("a-7", brief_digest=BD))
    assert ans_refused(rig.mint("a-7", brief_digest="sha256:" + "b" * 64)) == "attempt-conflict"
    assert ans_refused(rig.mint("a-7")) == "attempt-conflict"


def test_28_journal_line_rebuilds(store):
    rig = Rig(store, Clock())
    first = rig.mint("a-8", brief_digest=BD)
    ticket_of(first)
    assert rig.lines()[0]["facts"]["brief_digest"] == BD
    rig.restart()                                                              # the journal is rebuilt
    assert rig.mint("a-8", brief_digest=BD) == first
    assert rig.tickets.counts() == {"open": 1, "today": 1}


def test_28b_old_line_without_it_rebuilds(store):
    rig = Rig(store, Clock())
    first = rig.mint("a-9")
    assert "brief_digest" not in rig.lines()[0]["facts"]
    rig.restart()
    assert rig.mint("a-9") == first


def test_28c_wire_refuses_brief_on_verifier_and_codex(store):
    rig = Rig(store, Clock())
    assert ans_refused(rig.mint("a-10", kind="planner", brief_digest=BD)) == field()


# ---- alloc ------------------------------------------------------------------------------------------------------------

def _alloc(facts):
    return R.Alloc(session=SESSION,
                   slot=SlotId("egress", 1), lease_seq=1, slot_rev=1, slot_uid=893, slot_gid=893, kind="dispatch",
                   vendor="claude", auth="brokered", operator_uid=501, supervisor_uid=502, staging_uid=502,
                   created_at="2026-10-20T00:00:00Z", boot_id=BOOT, child_uid=None, ticket=C.new_handle(),
                   ticket_facts=facts)


def test_29_alloc_round_trip_and_malformed():
    kept = T.check_facts(dict(EX, brief_digest=BD))
    a = _alloc(kept)
    assert R.parse_alloc(R.alloc_bytes(a)).ticket_facts == kept
    assert "brief_digest" in R.TICKET_FACT_KEYS and "brief_digest" not in R.TICKET_FACT_REQUIRED
    raw = canon.parse(R.alloc_bytes(a))
    raw["ticket_facts"]["brief_digest"] = "sha256:XYZ"
    with pytest.raises(Refused) as ei:
        R.parse_alloc(canon.canonical_bytes(raw))
    assert ei.value.name == "bad-value"                                        # `_match`'s own name for a malformed digest
