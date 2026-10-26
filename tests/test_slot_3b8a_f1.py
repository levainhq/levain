"""S1 3b-8a fix round 1 (head rulings T5-T9 on the s3b8a L3 r1 triage; T6 builds ruling T1, the front.log home).

T5: a TICKETED session (dispatch / planner) is refused ``route-policy-unavailable`` at render until route pinning is built
(S2): the base map's entry is never handed to a ticket's front, not even under a colliding id. T6: the front's tunnel log
lives in ``L/gw/slot<n>/`` (it survives a reboot), is opened ``O_NOFOLLOW`` with an explicit 0640 whatever the umask,
is bounded (a size cap and one rotated file, rotated by the front), and render pre-creates it ``front account : root``
0640 (never the slot's group). T7: the front's dirs are refused when group- or other-writable, and an unexpected object
where the policy file goes is a named refusal, never an internal error. T8 / T9 are verify-only (pinned here)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from levain.broker import front as FR
from levain.contracts import canon
from levain.slot import control as C
from levain.slot import layout
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import vendor_routes as VR
from levain.slot.ids import SlotId
from tests.test_slot_3b8a_front_wiring import BASE_DOC, GITHUB, _arg, _world
from tests.test_slot_s2f2_runtime_helper import BOOT, DISPATCH_UID, OP_UID, _blob, _prepared

GW0 = 898          # _levain_gw0 in the World's account table


# --- T5: a ticketed session is refused until route pinning ------------------------------------------------------------

def _ticketed(w, route: str, session: str = "s-00000000000000e1") -> str:
    """A dispatch (executor) session on egress slot 1, its alloc carrying a ticket whose route is ``route``."""
    slot = SlotId("egress", 1)
    facts = {"kind": "executor", "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
             "route": route, "model_requested": "m-1", "egress_routes": ["github"], "reserved_microdollars": 1000,
             "routemap_generation": 3, "route_policy_digest": "sha256:" + "4" * 64}
    a = R.Alloc(session=session, slot=slot, lease_seq=1, slot_rev=1, slot_uid=897, slot_gid=897, kind="dispatch",
                vendor="claude", auth="brokered", operator_uid=OP_UID, supervisor_uid=DISPATCH_UID,
                staging_uid=DISPATCH_UID, created_at="2026-10-20T00:00:00Z", boot_id=BOOT, ticket=C.new_handle(),
                netns=slot.netns, cgroup=R.cgroup_path(slot, session), ticket_facts=facts)
    (w.L / "alloc" / f"{session}.json").write_bytes(R.alloc_bytes(a))
    return session


@pytest.mark.parametrize("route", ["openrouter-glm", "anthropic"])
def test_ticketed_session_refused_route_policy_unavailable(tmp_path, route):
    """``openrouter-glm``: a genuine dispatch-map route, absent from the base map. ``anthropic``: a dispatch route whose
    id COLLIDES with a base-map id: before T5 render handed the ticket's front the base entry's bytes."""
    w = _world(tmp_path)
    s = _ticketed(w, route)
    code, line = w.main(["render", s], _blob(routes=[route, "github"], model="m-1"), uid=DISPATCH_UID)
    assert line == {"ok": False, "refused": "route-policy-unavailable"}, w.last_err
    assert w.fronts.procs == {} and w.calls == []                       # before step (1)'s CAS: nothing acted on
    assert not (w.R / "gw" / "slot1" / "session.json").exists()


def test_front_policy_bytes_refuses_a_ticketed_alloc(tmp_path):
    """The point-of-use guard: the base map is read for a session with no ticket only."""
    w = _world(tmp_path)
    s = _ticketed(w, "anthropic")
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as e:
            H.front_policy_bytes(store, w.alloc(s), ["anthropic", "github"])
    finally:
        store.close()
    assert e.value.name == "route-policy-unavailable"


# --- T6: the log's home, mode and bound -------------------------------------------------------------------------------

def test_render_points_the_front_log_at_the_store_and_precreates_it(tmp_path):
    w = _world(tmp_path)
    s = _prepared(w)
    assert w.main(["render", s], _blob())[1] == {"ok": True}, w.last_err
    (p,) = w.fronts.procs.values()
    argv = p["spec"]["argv"]
    log = Path(_arg(argv[argv.index("levain.broker.front") + 1:], "--log"))
    assert log == w.L / "gw" / "slot0" / "front.log"                    # off R/: it survives a reboot
    st = os.lstat(log)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o640
    assert w.chown_of("gw/slot0/front.log") == (GW0, 0)                 # the front account : root, never the slot's


def test_render_keeps_an_existing_log_and_resets_its_mode(tmp_path):
    w = _world(tmp_path)
    log = w.L / "gw" / "slot0" / "front.log"
    log.write_bytes(b'{"event":"tunnel"}\n')
    os.chmod(log, 0o664)
    s = _prepared(w)
    assert w.main(["render", s], _blob())[1] == {"ok": True}, w.last_err
    assert log.read_bytes() == b'{"event":"tunnel"}\n'                  # an audit trail: never truncated by render
    assert stat.S_IMODE(os.lstat(log).st_mode) == 0o640
    assert w.chown_of("gw/slot0/front.log") == (GW0, 0)


@pytest.mark.parametrize("plant", ["symlink", "dir", "hardlink"])
def test_render_refuses_a_log_that_is_not_the_fronts_file(tmp_path, plant):
    w = _world(tmp_path)
    log = w.L / "gw" / "slot0" / "front.log"
    target = tmp_path / "elsewhere"
    target.write_bytes(b"untouched")
    if plant == "symlink":
        os.symlink(target, log)
    elif plant == "dir":
        log.mkdir()
    else:
        os.link(target, log)
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"}, w.last_err
    assert w.fronts.procs == {} and target.read_bytes() == b"untouched"
    assert all(rel != "gw/slot0/front.log" for rel, _u, _g in w.chowns)   # nothing chowned through the plant


def test_render_refuses_a_missing_log_dir_by_name(tmp_path):
    w = _world(tmp_path)
    os.rmdir(w.L / "gw" / "slot0")
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "store-missing"} and w.fronts.procs == {}


def _front(policy_session: str, log: Path, cap: int) -> FR.Front:
    pol = FR.load_policy(canon.canonical_bytes({"format": FR.POLICY_FORMAT, "kind": "interactive",
                                                "session": policy_session, "incarnation": 1,
                                                "routes": [GITHUB]}))
    return FR.Front(pol, log_path=log, log_cap=cap, slot_uid=os.getuid())


def test_refusal_flood_leaves_at_most_two_caps_on_disk(tmp_path):
    """Ten caps' worth of refused CONNECTs through a real socket (the first record's size sets how many): the front
    rotates at the cap and keeps one rotated file, so the dir holds at most two caps and fewer records than were sent."""
    cap = 1024
    log = tmp_path / "front.log"
    front = _front("s-1", log, cap)

    async def connect(sock: str, i: int) -> None:
        r, wr = await asyncio.open_unix_connection(sock)
        host = "evil-%06d.example" % i                  # one length, so every refusal record is one size
        wr.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        await wr.drain()
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 403")
        wr.close()

    async def body(sock: str) -> int:
        server = await front.serve_unix(sock)
        try:
            await connect(sock, 0)
            sent = 10 * cap // log.stat().st_size + 1
            for i in range(1, sent):
                await connect(sock, i)
        finally:
            server.close()
            await server.wait_closed()
            front.close_live()
            front.unbind()
        return sent

    with tempfile.TemporaryDirectory(prefix="s3b8af2", dir="/tmp") as d:
        sent = asyncio.run(body(os.path.join(d, "gw.sock")))
    files = sorted(p.name for p in tmp_path.glob("front.log*"))
    assert files == ["front.log", "front.log.1"]
    total = sum((tmp_path / n).stat().st_size for n in files)
    kept = [json.loads(ln) for n in files for ln in (tmp_path / n).read_text().splitlines()]
    assert total <= 2 * cap and len(kept) < sent, (sent, total, len(kept))
    assert [stat.S_IMODE(os.lstat(tmp_path / n).st_mode) for n in files] == [0o640, 0o640]
    assert all(r["event"] == "refused" and r["session"] == "s-1" for r in kept)


def test_log_mode_is_0640_whatever_the_umask(tmp_path):
    log = tmp_path / "front.log"
    old = os.umask(0o002)
    try:
        _front("s-1", log, 1 << 20)._log({"event": "refused", "refused": "host-not-allowed", "host": "x.example"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.lstat(log).st_mode) == 0o640


def test_log_mode_reset_on_a_file_the_front_finds(tmp_path):
    log = tmp_path / "front.log"
    log.write_bytes(b"")
    os.chmod(log, 0o666)
    _front("s-1", log, 1 << 20)._log({"event": "status-end"})
    assert stat.S_IMODE(os.lstat(log).st_mode) == 0o640


def test_log_never_follows_a_link(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"untouched")
    log = tmp_path / "front.log"
    os.symlink(target, log)
    _front("s-1", log, 1 << 20)._log({"event": "status-end"})
    assert target.read_bytes() == b"untouched" and "log write failed" in capsys.readouterr().err


def test_one_line_past_the_cap_is_replaced_not_written(tmp_path):
    """A record longer than the whole cap would break the two-cap bound: it is replaced by a short record."""
    log = tmp_path / "front.log"
    _front("s-1", log, 512)._log({"event": "refused", "detail": "x" * 2000})
    (rec,) = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert rec["event"] == "log-record-dropped" and rec["bytes"] > 512 and log.stat().st_size <= 512


def test_layout_rows_make_the_log_dir_front_owned_root_group():
    for host in ("linux", "darwin"):
        L = layout.STORE[host]
        rows = {r.path: r for r in layout.base_store_rows(host, "op")
                + layout.slot_store_rows(host, SlotId("egress", 3))}
        assert rows[L / "gw"] == layout.Row(L / "gw", "root", layout.ROOT_GROUP[host], 0o755)
        assert rows[L / "gw" / "slot3"] == layout.Row(L / "gw" / "slot3", "_levain_gw3", layout.ROOT_GROUP[host],
                                                     0o2750)
        assert not any(r.path.parent == L / "gw" for r in layout.slot_store_rows(host, SlotId("none", 0)))


# --- T7: the front's dirs and the policy file name ---------------------------------------------------------------------

@pytest.mark.parametrize("where", ["R", "L"])
def test_group_writable_front_dir_refused(tmp_path, where):
    w = _world(tmp_path)
    os.chmod((w.R if where == "R" else w.L) / "gw" / "slot0", 0o770)
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"} and w.fronts.procs == {}


def test_a_directory_named_session_json_is_a_named_refusal(tmp_path):
    w = _world(tmp_path)
    (w.R / "gw" / "slot0" / "session.json").mkdir()
    s = _prepared(w)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": False, "refused": "bad-root"} and code == H.EXIT_REFUSED, w.last_err
    assert w.fronts.procs == {}


def test_a_name_reappearing_before_the_exclusive_open_is_a_named_refusal(tmp_path, monkeypatch):
    """complement LOW-MED 1: a leftover front recreating session.json between root's unlink and its ``O_EXCL`` open."""
    w = _world(tmp_path)
    s = _prepared(w)
    real = os.unlink

    def unlink(name, *, dir_fd=None):
        if name != H.FRONT_POLICY_NAME:
            return real(name, dir_fd=dir_fd)
        os.symlink("/nonexistent", name, dir_fd=dir_fd)      # the leftover front's link, in place of root's unlink
        raise FileNotFoundError(name)

    monkeypatch.setattr(H.os, "unlink", unlink)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": False, "refused": "bad-root"} and code == H.EXIT_REFUSED, w.last_err


def test_a_missing_run_front_dir_is_a_named_refusal(tmp_path):
    """deepseek MED 3: production makes R/gw/slot<n>/ in setup and in the reaper's boot pass (``layout.slot_run_rows``,
    pinned below); a missing one is ``store-missing``, never an internal error."""
    w = _world(tmp_path)
    os.rmdir(w.R / "gw" / "slot0")
    s = _prepared(w)
    assert w.main(["render", s], _blob())[1] == {"ok": False, "refused": "store-missing"}


def test_setup_and_boot_make_the_run_front_dir():
    for host in ("linux", "darwin"):
        row = layout.Row(layout.RUN[host] / "gw" / "slot2", "_levain_gw2", "_levain_s2", 0o2750)
        assert row in layout.slot_run_rows(host, SlotId("egress", 2))     # the reaper's boot pass (``run_rows``)
        rows = layout.slot_rows(host, [SlotId("egress", 2)])                 # setup's and repair's
        assert row in rows and layout.STORE[host] / "gw" / "slot2" in {r.path for r in rows}
    # Linux: systemd-tmpfiles makes R/ at boot from these lines
    assert f"d {layout.RUN['linux']}/gw/slot2 2750 _levain_gw2 _levain_s2 -" in \
        layout.tmpfiles_text("linux", [SlotId("egress", 2)]).splitlines()


# --- T8 / T9: verify-only ----------------------------------------------------------------------------------------------

def test_t9_a_priced_base_entry_reaches_the_front():
    """deepseek MED 2: the shipped base map's brokered route is a priced (`/2`) entry: ``prices``, no per-call cap.
    Dropping ``prices`` leaves an entry the front's priced reading takes."""
    (anth,) = [r for r in BASE_DOC["routes"] if r["route"] == "anthropic"]
    assert "prices" in anth and "max_call_microdollars" not in anth and BASE_DOC["format"] == VR.BASE_FORMAT
    view = H.front_session_routes(BASE_DOC, ["anthropic"])
    doc = {"format": FR.POLICY_FORMAT, "kind": "interactive", "session": "s-1", "incarnation": 1, "routes": view}
    pol = FR.load_policy(canon.canonical_bytes(doc))
    assert pol.brokered["anthropic"].max_call_microdollars is None
