"""Row 165, codex L3 HIGH: ``read-allow`` over a tree the REAL END retained.

END's tree step chowns every retained entry to root; the supervisor calls ``read-allow`` only after that. The rig runs
the real :func:`end.end_session` (s2c's world: real files, real walks, a CAS through the real CONTROL) with a chown
seam that records the uid each entry is given (root, 0) and then chowns it to this test's own uid (no root here). The
real ``read-allow`` then runs through :func:`runtime_helper.main` on the same store with the PRODUCTION default
``chowned_uid`` (identity), and ``os.fstat`` reports, for an inode END chowned, the owner that chown gave it: the
on-disk owner production would see. Before the fix this answered ``allow-not-slots`` for every file END retained."""
from __future__ import annotations

import base64
import dataclasses
import io
import os

import pytest

from levain.contracts import canon
from levain.slot import end as end_mod
from levain.slot import install_helper as IH
from levain.slot import records as R
from levain.slot import runtime_helper as RH
from levain.slot import store_verbs as SV
from levain.slot.end import RETAINED, REVOKED, end_session, reconcile_session
from levain.slot.ids import SlotId
from tests import test_slot_s2c_end as S2C

RULES = b'{"permissions": {"allow": ["Bash(npm test *)"]}}'
SLOT_UID = 3990                    # the alloc's slot account (egress): what the old check compared against
DISPATCH_UID = S2C.OP_UID + 1
SECTION_7_4 = ["manifest.jsonl", "meta.jsonl", "object.json", "retained_at", "tree"]


class Chowned:
    """END's chown seam: records the uid root gives each inode, then chowns it to this test's uid."""

    def __init__(self) -> None:
        self.given: dict = {}

    def __call__(self, dirfd, name, uid, gid):
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        self.given[(st.st_dev, st.st_ino)] = uid
        end_mod.os_chown(dirfd, name, S2C.UID, S2C.GID)

    def fstat(self, real):
        """``os.fstat`` as production sees it: an inode END chowned carries the uid END gave it."""
        def fstat(fd):
            st = real(fd)
            uid = self.given.get((st.st_dev, st.st_ino))
            if uid is None:
                return st
            t = list(st[:10])
            t[4] = uid
            return os.stat_result(t)
        return fstat


class Control:
    """CONTROL for the runtime helper's discard / restore CAS on ``L/sessions/<s>.json``."""

    def __init__(self, L) -> None:
        self.L = L

    def call(self, op, **f):
        p = self.L / "sessions" / f"{f['session']}.json"
        rec = R.parse_state(p.read_bytes())
        assert op == "CAS" and rec.state == f["from"] and rec.generation == f["generation"]
        p.write_bytes(R.state_bytes(R.SessionState(session=rec.session, generation=rec.generation + 1, state=f["to"],
                                                   claims={}, lease_live=False, event_kind="state",
                                                   updated_at="2026-10-23T15:00:00Z")))
        return {"ok": True}

    def close(self) -> None:
        pass


def operator_store(L) -> None:
    """The runtime helper's view of the session END retained: operator record, alloc (egress, slot uid 3990, this
    test's uid the operator), CONTROL's record at RETAINED."""
    for d in ("alloc", "slots/e3", "retained", "trash", "export/operator", "export/dispatch", "policy", "sessions"):
        (L / d).mkdir(parents=True, exist_ok=True)
    (L / "policy" / "operator").write_text(IH.operator_record_text("operator", S2C.OP_UID))
    a = R.Alloc(session=S2C.SESSION, slot=SlotId("egress", 3), lease_seq=S2C.LEASE_SEQ, slot_rev=12,
                slot_uid=SLOT_UID, slot_gid=SLOT_UID, kind="interactive", vendor="claude", auth="brokered",
                operator_uid=S2C.OP_UID, supervisor_uid=S2C.OP_UID, staging_uid=S2C.OP_UID,
                created_at="2026-10-01T00:00:00Z", boot_id="6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f")
    (L / "alloc" / f"{S2C.SESSION}.json").write_bytes(R.alloc_bytes(a))
    (L / "sessions" / f"{S2C.SESSION}.json").write_bytes(R.state_bytes(R.SessionState(
        session=S2C.SESSION, generation=3, state=R.RETAINED, claims={}, lease_live=False, event_kind="state",
        updated_at="2026-10-01T00:00:00Z")))


def helper(w, verb="read-allow", **seams):
    """``verb <session>`` through the real runtime helper, as the session's operator, PRODUCTION ``chowned_uid``;
    for ``read-allow``, ``os.fstat`` reports each inode END chowned with the uid END gave it."""
    base = dict(host="darwin", store=w.L, euid=lambda: 0, trusted=lambda st: True,
                account_uid=lambda name: DISPATCH_UID, client_factory=lambda path: Control(w.L))
    base.update(seams)
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        if verb == "read-allow":
            mp.setattr(os, "fstat", w.chown.fstat(os.fstat))
        rc = RH.main([verb, S2C.SESSION], stdin=b"", env={"SUDO_UID": str(S2C.OP_UID)}, seams=RH.Seams(**base),
                     out=out, err=err)
    return rc, canon.parse(out.getvalue().strip().encode())


def rules_back(w) -> bytes:
    rc, line = helper(w)
    assert rc == RH.EXIT_OK and line["present"] is True, line
    return base64.b64decode(line["b64"], validate=True)


def build(tmp_path, data=RULES):
    w = S2C.World(tmp_path, git=False, var_folders=False, uchg=False)
    if data is not None:
        (w.S / "repo" / ".claude").mkdir()
        (w.S / "repo" / ".claude" / "settings.local.json").write_bytes(data)
    w.chown = Chowned()
    return w


def finish(w, monkeypatch, **kw):
    ctl = S2C.Control()
    assert end_session(w.facts, w.seams(ctl, chown=w.chown, root_uid=0, root_gid=0, **kw),
                       record_state=REVOKED) == RETAINED
    operator_store(w.L)
    return w


@pytest.fixture
def ended(tmp_path, monkeypatch):
    """A session whose repo holds the dialog's file, ENDED for real; os.fstat reports root's chown."""
    return finish(build(tmp_path), monkeypatch)


def obj(w) -> dict:
    return canon.parse((w.facts.retained / "object.json").read_bytes())


# --- the HIGH ----------------------------------------------------------------------------------------------------


def test_end_gives_the_dialogs_file_to_root(ended):
    f = ended.facts.tree / "repo" / ".claude" / "settings.local.json"
    st = os.lstat(f)
    assert ended.chown.given[(st.st_dev, st.st_ino)] == 0
    fd = os.open(f, os.O_RDONLY)
    try:
        assert ended.chown.fstat(os.fstat)(fd).st_uid == 0      # what production's read-allow sees
    finally:
        os.close(fd)


def test_read_allow_returns_the_file_end_retained(ended):
    assert rules_back(ended) == RULES


def test_end_records_the_files_provenance_in_its_object_and_nothing_else_in_the_object(ended):
    o = obj(ended)
    assert o["allow"] == {"size": len(RULES), "digest": end_mod.digest_bytes(RULES)}
    line = next(canon.parse(x) for x in (ended.facts.retained / "manifest.jsonl").read_bytes().splitlines()
                if canon.parse(x)["path_b64"] == end_mod._b64(b"repo/.claude/settings.local.json"))
    assert line["digest"] == o["allow"]["digest"]
    assert sorted(os.listdir(ended.facts.retained)) == SECTION_7_4     # §7.4's list: no new file
    assert end_mod.retained_whole(ended.facts, ended.seams(S2C.Control()))


def test_no_file_no_record_and_present_false(tmp_path, monkeypatch):
    w = finish(build(tmp_path, data=None), monkeypatch)
    assert "allow" not in obj(w)
    assert helper(w) == (RH.EXIT_OK, {"ok": True, "present": False})


def test_an_old_object_without_the_record_is_refused_never_read(ended):
    # A tree retained before row 165: same shape, no ``allow``. Root reads nothing of the file.
    p = ended.facts.retained / "object.json"
    o = obj(ended)
    del o["allow"]
    os.chmod(p, 0o600)
    p.write_bytes(canon.canonical_bytes(o))
    assert helper(ended) == (RH.EXIT_REFUSED, {"ok": False, "refused": "allow-not-slots"})


# --- what END does not record ------------------------------------------------------------------------------------


def test_a_file_not_the_session_owners_gets_no_record(tmp_path, monkeypatch):
    w = build(tmp_path)
    # the session owner (egress: the slot uid) is not who owns the file
    rec = {"type": "file", "nlink": 1, "size": len(RULES), "digest": end_mod.digest_bytes(RULES)}
    assert end_mod.allow_provenance(w.facts, rec) == {"size": len(RULES), "digest": end_mod.digest_bytes(RULES)}
    assert end_mod.allow_provenance(dataclasses.replace(w.facts, slot_uid=S2C.UID + 7), rec) is None
    # a verifier session (class "none"): the owner is the CHILD uid; the slot uid owning it is not enough
    verifier = dataclasses.replace(w.facts, slot=SlotId("none", 1), child_uid=S2C.UID + 7)
    assert end_mod.allow_provenance(verifier, rec) is None
    assert end_mod.allow_provenance(dataclasses.replace(verifier, child_uid=S2C.UID, slot_uid=S2C.UID + 7),
                                    rec) is not None


@pytest.mark.parametrize("change", [{"nlink": 2}, {"type": "link"}, {"size": end_mod.ALLOW_CAP + 1},
                                    {"size": len(RULES) + 1}, {"size": True}, {"digest": None}])
def test_a_manifest_line_not_a_lone_capped_file_gets_no_record(tmp_path, change):
    w = build(tmp_path)
    rec = {"type": "file", "nlink": 1, "size": len(RULES), "digest": end_mod.digest_bytes(RULES), **change}
    assert end_mod.allow_provenance(w.facts, rec) is None
    assert end_mod.allow_provenance(w.facts, None) is None


def test_a_hard_linked_file_inside_the_tree_gets_no_record(tmp_path, monkeypatch):
    w = build(tmp_path)
    os.link(w.S / "repo" / ".claude" / "settings.local.json", w.S / "repo" / "twin.json")
    finish(w, monkeypatch)
    assert "allow" not in obj(w)
    assert helper(w) == (RH.EXIT_REFUSED, {"ok": False, "refused": "allow-unreadable"})   # nlink 2, never read


def test_a_symlinked_claude_dir_gets_no_record(tmp_path, monkeypatch):
    w = build(tmp_path, data=None)
    (w.S / "elsewhere").mkdir()
    (w.S / "elsewhere" / "settings.local.json").write_bytes(RULES)
    os.symlink("../elsewhere", w.S / "repo" / ".claude")
    finish(w, monkeypatch)
    assert "allow" not in obj(w)
    assert helper(w) == (RH.EXIT_REFUSED, {"ok": False, "refused": "allow-unreadable"})


# --- the object's lifecycle: redo, discard, restore, purge --------------------------------------------------------


@pytest.mark.parametrize("point", [p for p in S2C.RETAIN_POINTS if not p.startswith("retain:before:init")])
def test_a_crash_anywhere_in_retain_then_the_redo_still_records_it(tmp_path, monkeypatch, point):
    w = build(tmp_path)
    ctl = S2C.Control()
    fired = []

    def fault(p):
        if p == point and not fired:
            fired.append(p)
            raise S2C.Crash(p)
    with pytest.raises(S2C.Crash):
        end_session(w.facts, w.seams(ctl, chown=w.chown, root_uid=0, root_gid=0, fault=fault),
                    record_state=REVOKED)
    seams = w.seams(ctl, chown=w.chown, root_uid=0, root_gid=0)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    operator_store(w.L)
    assert obj(w)["allow"] == {"size": len(RULES), "digest": end_mod.digest_bytes(RULES)}
    assert rules_back(w) == RULES


def test_discard_and_restore_carry_the_record_and_trash_is_never_read(ended):
    before = obj(ended)
    assert helper(ended, "discard") == (RH.EXIT_OK, {"ok": True, "state": R.TRASH})
    assert helper(ended) == (RH.EXIT_REFUSED, {"ok": False, "refused": "not-retained:trash"})
    assert helper(ended, "restore") == (RH.EXIT_OK, {"ok": True, "state": R.RETAINED})
    # restore's own stamp joins the list (store_verbs' ``_mark``); the object END wrote is unchanged
    assert obj(ended) == before and sorted(os.listdir(ended.facts.retained)) == sorted(SECTION_7_4 + ["restored_at"])
    assert rules_back(ended) == RULES


def test_purge_takes_the_object_whole(ended):
    assert helper(ended, "discard")[0] == RH.EXIT_OK
    facts = SV.facts_from_alloc(R.parse_alloc((ended.L / "alloc" / f"{S2C.SESSION}.json").read_bytes()), ended.L)
    seams = SV.StoreSeams(cas=lambda target, fields: None, owner_ok=lambda st: True)
    assert SV.purge(facts, seams, forced=True) == R.PURGED
    assert not (ended.L / "trash" / S2C.SESSION).exists() and not (ended.L / "retained" / S2C.SESSION).exists()
    assert helper(ended) == (RH.EXIT_REFUSED, {"ok": False, "refused": "not-retained:absent"})
