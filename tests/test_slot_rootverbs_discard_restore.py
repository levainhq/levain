"""Root's ``discard`` / ``restore`` verbs (DESIGN §4.6, §4.7; CONTRACT §5.1, §7.4), driven through
:func:`runtime_helper.main` on a scratch store. Before lane rootverbs (1023+8) both answered ``verb-unbuilt:<verb>``
though :func:`store_verbs.discard` / :func:`store_verbs.restore` existed; the operator CLI (``levain session
discard|restore``) routes to them and prints the answer's ``state``."""
from __future__ import annotations

import fcntl
import io
import os
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import install_helper as IH
from levain.slot import records as R
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId

S = "s-" + "9d" * 8
OTHER = "s-" + "9e" * 8
UID = os.getuid()
DISPATCH_UID = UID + 1
STRANGER_UID = UID + 2


class Control:
    """CONTROL's CAS on ``L/sessions/<s>.json``: ``from`` and ``generation`` must match the record; ``to`` is written."""

    def __init__(self, L: Path) -> None:
        self.L, self.calls = L, []

    def call(self, op, **f):
        self.calls.append((op, f["from"], f["to"]))
        p = self.L / "sessions" / f"{f['session']}.json"
        rec = R.parse_state(p.read_bytes())
        if op != "CAS" or rec.state != f["from"] or rec.generation != f["generation"]:
            raise Refused("from-mismatch", f"{rec.state} -> {f['to']}")
        p.write_bytes(R.state_bytes(R.SessionState(session=rec.session, generation=rec.generation + 1, state=f["to"],
                                                   claims={}, lease_live=False, event_kind="state",
                                                   updated_at="2026-10-23T15:00:00Z")))
        return {"ok": True}

    def close(self) -> None:
        pass


class World:
    def __init__(self, tmp: Path) -> None:
        L = self.L = tmp / "L"
        for d in ("alloc", "slots/e3", "retained", "trash", "export/operator", "export/dispatch", "policy",
                  "sessions"):
            (L / d).mkdir(parents=True)
        (L / "policy" / "operator").write_text(IH.operator_record_text("operator", UID))
        for s, op in ((S, UID), (OTHER, STRANGER_UID)):     # OTHER: an alloc whose operator is not the caller
            r = L / "retained" / s
            (r / "tree" / "repo").mkdir(parents=True)
            (r / "tree" / "repo" / "a.txt").write_bytes(b"alpha\n")
            (r / "object.json").write_bytes(canon.canonical_bytes({"format": "levain-retained-object/1",
                                                                   "session": s}))
            (r / "retained_at").write_bytes(b"2026-10-01T00:00:00Z\n")
            a = R.Alloc(session=s, slot=SlotId("egress", 3), lease_seq=7, slot_rev=12, slot_uid=3990, slot_gid=3990,
                        kind="interactive", vendor="claude", auth="brokered", operator_uid=op, supervisor_uid=op,
                        staging_uid=op, created_at="2026-10-01T00:00:00Z",
                        boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
            (L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))
            (L / "sessions" / f"{s}.json").write_bytes(R.state_bytes(R.SessionState(
                session=s, generation=3, state=R.RETAINED, claims={}, lease_live=False, event_kind="state",
                updated_at="2026-10-01T00:00:00Z")))
        self.control = Control(L)
        self.seams = RH.Seams(host="darwin", store=L, euid=lambda: 0, trusted=lambda st: True,
                              account_uid=lambda name: DISPATCH_UID, client_factory=lambda path: self.control)

    def main(self, verb: str, uid: int = UID, s: str = S):
        out, err = io.StringIO(), io.StringIO()
        rc = RH.main([verb, s], stdin=b"", env={"SUDO_UID": str(uid)}, seams=self.seams, out=out, err=err)
        return rc, canon.parse(out.getvalue().strip().encode())

    def where(self, s: str = S):
        return (R.parse_state((self.L / "sessions" / f"{s}.json").read_bytes()).state,
                [d for d in ("retained", "trash") if (self.L / d / s).exists()])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize("verb", ["discard", "restore"])
@pytest.mark.parametrize("uid,s,refused", [(STRANGER_UID, S, "unknown-caller"),
                                           (DISPATCH_UID, S, "verb-not-allowed-for-caller"),
                                           (UID, OTHER, "no-session")])
def test_a_caller_who_is_not_the_sessions_operator_is_refused_and_nothing_moves(world, verb, uid, s, refused):
    if verb == "restore" and s == S:
        assert world.main("discard")[0] == RH.EXIT_OK
    before = world.where(s)
    rc, line = world.main(verb, uid, s)
    assert (rc, line) == (RH.EXIT_REFUSED, {"ok": False, "refused": refused})
    assert world.where(s) == before


def test_discard_then_restore_round_trips_and_a_second_of_each_refuses_per_the_store_verb(world):
    assert world.main("discard") == (RH.EXIT_OK, {"ok": True, "state": R.TRASH})
    assert world.where() == (R.TRASH, ["trash"]) and (world.L / "trash" / S / "trashed_at").exists()
    assert world.main("discard") == (RH.EXIT_REFUSED, {"ok": False, "refused": "not-retained:trash"})
    assert world.main("restore") == (RH.EXIT_OK, {"ok": True, "state": R.RETAINED})
    assert world.where() == (R.RETAINED, ["retained"]) and not (world.L / "retained" / S / "trashed_at").exists()
    assert world.main("restore") == (RH.EXIT_REFUSED, {"ok": False, "refused": "not-trash:retained"})
    assert (world.L / "retained" / S / "tree" / "repo" / "a.txt").read_bytes() == b"alpha\n"
    assert world.control.calls == [("CAS", R.RETAINED, R.TRASH), ("CAS", R.TRASH, R.RETAINED)]
    assert not (world.L / "alloc" / f"{S}.intent.json").exists()


@pytest.mark.parametrize("verb", ["discard", "restore"])
def test_a_held_session_lock_refuses_session_busy(world, verb):
    # CONTRACT §5.1's T:5/test_discard_during_adopt_refused: adopt-open holds L/alloc/<s>.lock for its whole run.
    if verb == "restore":
        assert world.main("discard")[0] == RH.EXIT_OK
    before = world.where()
    fd = os.open(world.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert world.main(verb) == (RH.EXIT_REFUSED, {"ok": False, "refused": "session-busy"})
    finally:
        os.close(fd)
    assert world.where() == before
