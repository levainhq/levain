"""Identity gate G2 (1027-18 RULINGS IG-1): a ``login`` session whose ``confirm-identity yes`` was given REPLACES the
credential parked in ``L/slots/e<n>/keep/``. END renames the old keep/ entry (never opening it) into the session's own
``L/retained/<s>/replaced/``, which discard, restore and purge carry with the session, then parks the new login. A login
ended without a yes (D1, 1027-19 RULINGS) parks nothing: its NEW credential goes to ``L/retained/<s>/unconfirmed/``, the
parked one stays, and the session retains, so its slot is released. Any other kind still refuses ``keep-occupied``
(HELD park).

The world, the REAL CONTROL CAS and the seams are the 2c suite's (``tests/test_slot_s2c_end.py``); "confirmed" is
root's baseline file as G1's ``confirm-identity yes`` writes it (:func:`levain.slot.identity.baseline_bytes`)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from levain.slot import end as end_mod
from levain.slot import identity as ID
from levain.slot import records as R
from levain.slot import store_verbs as SV
from levain.slot.end import CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED, end_session, reconcile_session
from levain.slot.ids import SlotId
from levain.slot.root_intent import read_root_intent
from tests.test_slot_s2c_end import (CRED, GID, LEASE_SEQ, NOW, OP_UID, SESSION, UID, Control, Crash, World, _trace,
                                     mine)

OLD = b'{"claudeAiOauth":{"accessToken":"sk-ant-oat-THE-PARKED-ONE"}}'
OTHER = "s-fedcba9876543210"
UUID = "0b4c4a52-7f0e-4c39-9b8e-1d2c3e4f5a6b"
NAME = ".credentials.json"


def login_world(tmp_path: Path, *, kind: str = "login", parked: bytes | None = OLD) -> World:
    w = World(tmp_path, var_folders=False, uchg=False)
    w.facts = replace(w.facts, kind=kind)
    if parked is not None:
        (w.facts.keep / NAME).write_bytes(parked)
        os.chmod(w.facts.keep / NAME, 0o600)
    return w


def confirm(w: World, *, session: str = SESSION, vendor: str = "claude", data: bytes | None = None) -> Path:
    """Root's baseline file as ``confirm-identity yes`` writes it for ``session``."""
    p = w.facts.slot.slot_dir(w.L) / ID.BASELINE_NAME
    p.write_bytes(data if data is not None else ID.baseline_bytes(vendor=vendor, observed_id=UUID, confirmed_at=NOW,
                                                                  session=session))
    os.chmod(p, 0o600)
    return p


def replaced(w: World) -> Path:
    return w.facts.retained / end_mod.REPLACED / NAME


def unconfirmed(w: World) -> Path:
    return w.facts.retained / end_mod.UNCONFIRMED / NAME


def where(w: World) -> dict:
    """Which credential is where: each of keep/, the session tree (live, or retained once moved), replaced/,
    unconfirmed/."""
    out = {}
    for label, p in (("keep", w.facts.keep / NAME), ("replaced", replaced(w)), ("unconfirmed", unconfirmed(w)),
                     ("tree", w.S / "config" / "claude" / NAME),
                     ("retained-tree", w.facts.tree / "config" / "claude" / NAME)):
        if os.path.lexists(p):
            out[label] = {OLD: "old", CRED: "new"}.get(p.read_bytes(), "?")
    return out


# --- the build -------------------------------------------------------------------------------------------------------


def test_confirmed_relogin_replaces_the_parked_credential(tmp_path, monkeypatch):
    w = login_world(tmp_path)
    confirm(w)
    opened = _trace(monkeypatch, NAME, calls=("open",))
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert list(opened) == []                                   # both moved by rename (dir fds), never opened
    monkeypatch.undo()
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    assert where(w) == {"keep": "new", "replaced": "old"}
    assert sorted(os.listdir(w.facts.keep)) == [NAME]
    assert sorted(os.listdir(w.facts.retained)) == ["manifest.jsonl", "meta.jsonl", "object.json", "replaced",
                                                    "retained_at", "tree"]
    assert os.stat(w.facts.retained / end_mod.REPLACED).st_mode & 0o777 == 0o700
    assert end_mod.retained_whole(w.facts, w.seams(ctl))


def test_facts_from_alloc_carries_the_kind(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    alloc = {"session": SESSION, "slot": {"class": "egress", "index": 3}, "slot_uid": w.facts.slot_uid,
             "slot_gid": w.facts.slot_gid, "lease_seq": w.facts.lease_seq, "vendor": "claude",
             "auth": "passthrough", "kind": "login"}
    assert end_mod.facts_from_alloc(alloc, store=w.L).kind == "login"
    del alloc["kind"]
    assert end_mod.facts_from_alloc(alloc, store=w.L).kind is None   # no kind = never a confirmed re-login


def test_login_ended_without_a_yes_sets_the_new_login_aside_and_retains(tmp_path, monkeypatch):
    """D1: the parked login stays (same inode), the new one goes to ``unconfirmed/`` by rename (never opened), and the
    session reaches RETAINED by the same edges as any END."""
    w = login_world(tmp_path)
    ino = (w.facts.keep / NAME).stat().st_ino
    opened = _trace(monkeypatch, NAME, calls=("open",))
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert list(opened) == []
    monkeypatch.undo()
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    assert where(w) == {"keep": "old", "unconfirmed": "new"}
    assert sorted(os.listdir(w.facts.keep)) == [NAME] and (w.facts.keep / NAME).stat().st_ino == ino
    assert sorted(os.listdir(w.facts.retained)) == ["manifest.jsonl", "meta.jsonl", "object.json", "retained_at",
                                                    "tree", "unconfirmed"]
    assert os.stat(w.facts.retained / end_mod.UNCONFIRMED).st_mode & 0o777 == 0o700
    assert end_mod.retained_whole(w.facts, w.seams(ctl))


def _bad_baselines():
    good = dict(vendor="claude", observed_id=UUID, confirmed_at=NOW, session=SESSION)
    return {
        "another-session": ID.baseline_bytes(**{**good, "session": OTHER}),
        "another-vendor": ID.baseline_bytes(**{**good, "vendor": "codex"}),
        "old-vendor-only-shape": b'{"vendor":"claude","account_id":"x","confirmed_at":"' + NOW.encode() + b'"}',
        "not-json": b"yes",
        "extra-key": ID.baseline_bytes(**good)[:-1] + b',"z":1}',
    }


@pytest.mark.parametrize("why", sorted(_bad_baselines()))
def test_a_baseline_that_does_not_confirm_this_session_never_replaces(tmp_path, why):
    w = login_world(tmp_path)
    confirm(w, data=_bad_baselines()[why])
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert where(w) == {"keep": "old", "unconfirmed": "new"}


def test_a_linked_or_foreign_baseline_is_not_believed(tmp_path):
    w = login_world(tmp_path)
    real = confirm(w)
    os.rename(real, tmp_path / "elsewhere")
    os.symlink(tmp_path / "elsewhere", real)                    # never followed
    assert not end_mod.confirmed_relogin(w.facts, owner_ok=mine)
    os.unlink(real)
    ino = confirm(w).stat().st_ino
    assert end_mod.confirmed_relogin(w.facts, owner_ok=mine)    # owner_ok says no for the baseline: not root's file
    assert not end_mod.confirmed_relogin(w.facts, owner_ok=lambda st: mine(st) and st.st_ino != ino)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, owner_ok=lambda st: mine(st) and st.st_ino != ino),
                       record_state=REVOKED) == RETAINED
    assert where(w) == {"keep": "old", "unconfirmed": "new"}


@pytest.mark.parametrize("kind", ["interactive", None])
def test_other_kinds_keep_keep_occupied_even_with_a_baseline_naming_them(tmp_path, kind):
    w = login_world(tmp_path, kind=kind)
    confirm(w)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("keep-occupied" in n for n in w.notes)
    assert where(w) == {"keep": "old", "tree": "new"}


def test_confirmed_login_on_an_empty_keep_parks_as_before(tmp_path):
    w = login_world(tmp_path, parked=None)
    confirm(w)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert where(w) == {"keep": "new"}
    assert not (w.facts.retained / end_mod.REPLACED).exists()


def test_confirmed_login_that_wrote_no_credential_keeps_the_old_one_parked(tmp_path):
    w = login_world(tmp_path)
    (w.S / "config" / "claude" / NAME).unlink()                 # a login that exited without writing one
    confirm(w)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert where(w) == {"keep": "old"}
    assert not (w.facts.retained / end_mod.REPLACED).exists()


def test_replaced_already_holding_a_file_is_never_overwritten(tmp_path):
    w = login_world(tmp_path)
    confirm(w)
    (w.facts.retained / end_mod.REPLACED).mkdir(parents=True)
    replaced(w).write_bytes(b"SOMETHING ELSE")
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("replaced-occupied" in n for n in w.notes)
    assert replaced(w).read_bytes() == b"SOMETHING ELSE"
    assert where(w)["keep"] == "old" and where(w)["tree"] == "new"


def test_a_planted_link_at_replaced_is_not_followed(tmp_path):
    w = login_world(tmp_path)
    confirm(w)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    w.facts.retained.mkdir()
    os.symlink(decoy, w.facts.retained / end_mod.REPLACED)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert os.listdir(decoy) == []
    assert where(w)["keep"] == "old" and where(w)["tree"] == "new"


# --- crash points (each one first RUN as a SIGKILL by the lane's driver, RETURN "Crash table") -------------------------

#: Every fault point a park of a confirmed re-login passes, in execution order, with where each credential is when the
#: process dies there.
CRASH_POINTS = {
    "park:before:move": {"keep": "old", "tree": "new"},
    "park:after:move": {"keep": "old", "tree": "new"},
    "park:keep:made": {"keep": "old", "tree": "new"},
    "park:replace:retained-made": {"keep": "old", "tree": "new"},
    "park:replace:dest-made": {"keep": "old", "tree": "new"},
    "park:replace:dirs": {"keep": "old", "tree": "new"},
    "park:replace:renamed": {"replaced": "old", "tree": "new"},
    "park:replace:source-synced": {"replaced": "old", "tree": "new"},
    "park:replace:aside": {"replaced": "old", "tree": "new"},
    "park:keep:renamed": {"replaced": "old", "keep": "new"},
    "park:keep:source-synced": {"replaced": "old", "keep": "new"},
    "park:before:commit": {"replaced": "old", "keep": "new"},
    "park:after:commit": {"replaced": "old", "keep": "new"},
    "park:before:cas": {"replaced": "old", "keep": "new"},
    "park:after:cas": {"replaced": "old", "keep": "new"},
}


@pytest.mark.parametrize("point", list(CRASH_POINTS))
def test_a_crash_at_each_park_point_leaves_one_credential_parked_or_in_the_tree(tmp_path, point):
    w = login_world(tmp_path)
    confirm(w)
    ctl = Control()
    fired = []

    def fault(p):
        if p == point and not fired:
            fired.append(p)
            raise Crash(p)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
    assert fired == [point]
    at = where(w)
    assert at == CRASH_POINTS[point]
    assert {"keep", "tree"} & set(at)                           # never neither
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    assert where(w) == {"keep": "new", "replaced": "old"}
    assert not w.facts.intent.exists()


def test_the_fault_points_are_the_ones_a_park_passes(tmp_path):
    w = login_world(tmp_path)
    confirm(w)
    seen: list = []
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, fault=seen.append), record_state=REVOKED) == RETAINED
    assert [p for p in seen if p.startswith("park:")] == list(CRASH_POINTS)


_KILL_CHILD = """
import os, signal, sys
from pathlib import Path
from tests.test_slot_idg2_relogin import confirm, login_world
from tests.test_slot_s2c_end import Control
from levain.slot.end import REVOKED, end_session
tmp, point = Path(sys.argv[1]), sys.argv[2]
w = login_world(tmp)
confirm(w)
ctl = Control()
def fault(p):
    if p == point:
        os.kill(os.getpid(), signal.SIGKILL)
end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
"""


@pytest.mark.parametrize("point", ["park:replace:renamed", "park:replace:aside", "park:before:commit"])
def test_a_sigkilled_end_converges(tmp_path, point):
    """The crash as a real process death (SIGKILL, no unwinding) between and after the two renames, then a fresh END
    in this process from the record the child left (DEAD, the park journal at ``move``)."""
    child = tmp_path / "child.py"
    child.write_text(_KILL_CHILD)
    wt = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(child), str(tmp_path / "w"), point], capture_output=True, timeout=120,
                       cwd=wt, env={**os.environ, "PYTHONPATH": str(wt)})
    assert r.returncode == -signal.SIGKILL, r.stderr.decode()[-2000:]
    w = World.__new__(World)
    w.tmp, w.L, w.vf = tmp_path / "w", tmp_path / "w" / "L", None
    w.S = w.L / "live" / SESSION
    w.facts = replace(end_mod.EndFacts(SESSION, SlotId("egress", 3), UID, GID, w.L, None, lease_seq=LEASE_SEQ,
                                       vendor="claude", auth="passthrough"), kind="login")
    assert where(w) == CRASH_POINTS[point]
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "move"
    ctl = Control(state=DEAD)
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    assert where(w) == {"keep": "new", "replaced": "old"}


# --- the replaced credential follows THIS session's retention ----------------------------------------------------------


def test_the_replaced_credential_follows_discard_restore_and_purge(tmp_path):
    w = login_world(tmp_path)
    confirm(w)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    for d in ("trash", "export/operator"):
        (w.L / d).mkdir(parents=True, exist_ok=True)
    sf = SV.StoreFacts(session=SESSION, store=w.L, kind="login", operator_uid=OP_UID)
    clock = [NOW]
    ss = SV.StoreSeams(cas=ctl.cas, owner_ok=mine, now=lambda: clock[0])
    assert SV.discard(sf, ss) == R.TRASH
    assert (sf.trash / end_mod.REPLACED / NAME).read_bytes() == OLD and not replaced(w).exists()
    assert SV.restore(sf, ss) == R.RETAINED
    assert replaced(w).read_bytes() == OLD
    assert SV.discard(sf, ss) == R.TRASH
    assert SV.purge(sf, ss, forced=True) == R.PURGED
    assert not sf.trash.exists() and not replaced(w).exists()
    assert (w.facts.keep / NAME).read_bytes() == CRED            # the new login stays parked


# --- through the runtime helper: G1's confirm-identity yes, then teardown ---------------------------------------------


def test_a_codex_relogin_confirmed_yes_then_torn_down_replaces_the_parked_login(tmp_path):
    """The whole path on lane 2f3's harness (the REAL CONTROL, fake launches and process table): a login session on a
    slot whose keep/ already holds a Codex login, its ``confirm-identity yes`` (G1's verb, which writes the baseline
    END reads), then ``teardown``: RETAINED, the new login parked, the old one in ``replaced/``. Without the yes the
    same teardown is RETAINED with the new login in ``unconfirmed/`` and the parked one untouched (D1)."""
    from tests.test_slot_idg1_identity import E0, GW0, _login
    from tests.test_slot_s2f3_start import _S, _world
    results = {}
    for answer in ("yes", "no"):
        w, la, table = _world(tmp_path / answer)
        keep = E0.slot_dir(w.L) / "keep"
        (keep / "auth.json").write_bytes(b"THE PARKED CODEX LOGIN")
        s = _login(w, la, "codex")
        for pid in w.fronts.alive():
            table.add(pid, GW0)
        (_S(w, s) / "config" / "codex" / "auth.json").write_bytes(b"THE NEW CODEX LOGIN")   # what the login wrote
        assert w.main(["confirm-identity", s, answer])[1]["ok"]
        results[answer] = (w.main(["teardown", s])[1], (keep / "auth.json").read_bytes(),
                           w.L / "retained" / s / end_mod.REPLACED / "auth.json",
                           w.L / "retained" / s / end_mod.UNCONFIRMED / "auth.json", w.state(s))
    line, parked, aside, unconf, _ = results["yes"]
    assert line == {"ok": True, "state": R.RETAINED}
    assert parked == b"THE NEW CODEX LOGIN" and aside.read_bytes() == b"THE PARKED CODEX LOGIN"
    assert not unconf.exists()
    line, parked, aside, unconf, rec = results["no"]
    assert line == {"ok": True, "state": R.RETAINED} and rec.state == R.RETAINED
    assert parked == b"THE PARKED CODEX LOGIN" and not aside.exists()
    assert unconf.read_bytes() == b"THE NEW CODEX LOGIN"


# --- D1: a login with no yes on an occupied keep/ (1027-19 RULINGS "Identity gate G2" D1) -----------------------------


def test_unconfirmed_occupied_is_never_overwritten(tmp_path):
    w = login_world(tmp_path)
    (w.facts.retained / end_mod.UNCONFIRMED).mkdir(parents=True)
    unconfirmed(w).write_bytes(b"SOMETHING ELSE")
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("unconfirmed-occupied" in n for n in w.notes)
    assert unconfirmed(w).read_bytes() == b"SOMETHING ELSE"
    assert where(w)["keep"] == "old" and where(w)["tree"] == "new"


def test_a_planted_link_at_unconfirmed_is_not_followed(tmp_path):
    w = login_world(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    w.facts.retained.mkdir()
    os.symlink(decoy, w.facts.retained / end_mod.UNCONFIRMED)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("bad-root" in n for n in w.notes)
    assert os.listdir(decoy) == []
    assert where(w)["keep"] == "old" and where(w)["tree"] == "new"


def test_unconfirmed_login_that_wrote_no_credential_retains_and_moves_nothing(tmp_path):
    w = login_world(tmp_path)
    (w.S / "config" / "claude" / NAME).unlink()
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert where(w) == {"keep": "old"}
    assert not (w.facts.retained / end_mod.UNCONFIRMED).exists()


def held_before_d1(tmp_path: Path) -> tuple[World, Control]:
    """r3c10 slot 0's state, made the way it came about: a ``login`` session, keep/ occupied, ended from REVOKED (the
    helper's teardown) by an END that does not read the alloc's kind (the code before G2: no ``EndFacts.kind``, so
    ``None``): HELD ``failed_step:"park"``, ``held_attempts`` 1, the park journal left at ``move``, the new login in the
    live tree, the note the VM printed. The facts are then the ones :func:`facts_from_alloc` reads today (kind
    ``login``). The lane's driver built the same state with the base commit's own code, then re-ended it with this
    one (RETURN "Ran")."""
    w = login_world(tmp_path, kind=None)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.fields == {"failed_step": "park", "held_attempts": 1}
    assert any("park not proven: move: keep-occupied" in n for n in w.notes)
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "move"
    assert where(w) == {"keep": "old", "tree": "new"}
    w.facts = replace(w.facts, kind="login")
    w.notes.clear()
    return w, ctl


def test_a_held_login_from_before_d1_releases_on_the_reapers_next_pass(tmp_path):
    """The reaper's step 2 (journal reconciliation) finishes the park journal; its step 3 runs END from the record."""
    w, ctl = held_before_d1(tmp_path)
    seams = w.seams(ctl)
    assert reconcile_session(w.facts, seams, held_attempts=1).phase == "move"
    assert ctl.state == CREDENTIAL_PARKED
    assert end_session(w.facts, seams, record_state=ctl.state) == RETAINED
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, HELD, CREDENTIAL_PARKED, RETAINED]
    assert where(w) == {"keep": "old", "unconfirmed": "new"}
    assert not w.facts.intent.exists() and end_mod.retained_whole(w.facts, seams)


def test_a_held_login_from_before_d1_releases_on_an_end_retry_alone(tmp_path):
    """END's HELD retry from ``failed_step:"park"`` (no step 2 first) takes the same path."""
    w, ctl = held_before_d1(tmp_path)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="park", held_attempts=1) == RETAINED
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, HELD, CREDENTIAL_PARKED, RETAINED]
    assert where(w) == {"keep": "old", "unconfirmed": "new"}
    assert not w.facts.intent.exists()


#: Every fault point a park of an unconfirmed login on an occupied keep/ passes, in execution order, with where each
#: credential is when the process dies there (first RUN as SIGKILLs by the lane's driver, RETURN "Crash table").
D1_POINTS = {
    "park:before:move": {"keep": "old", "tree": "new"},
    "park:after:move": {"keep": "old", "tree": "new"},
    "park:keep:made": {"keep": "old", "tree": "new"},
    "park:unconfirmed:retained-made": {"keep": "old", "tree": "new"},
    "park:unconfirmed:dest-made": {"keep": "old", "tree": "new"},
    "park:unconfirmed:dirs": {"keep": "old", "tree": "new"},
    "park:unconfirmed:renamed": {"keep": "old", "unconfirmed": "new"},
    "park:unconfirmed:source-synced": {"keep": "old", "unconfirmed": "new"},
    "park:unconfirmed:aside": {"keep": "old", "unconfirmed": "new"},
    "park:before:commit": {"keep": "old", "unconfirmed": "new"},
    "park:after:commit": {"keep": "old", "unconfirmed": "new"},
    "park:before:cas": {"keep": "old", "unconfirmed": "new"},
    "park:after:cas": {"keep": "old", "unconfirmed": "new"},
}
#: The HELD retry starts from the journal at ``move``, so it never passes ``begin``'s two points.
D1_HELD_POINTS = {p: at for p, at in D1_POINTS.items() if p not in ("park:before:move", "park:after:move")}


def test_the_d1_fault_points_are_the_ones_a_park_passes(tmp_path):
    w = login_world(tmp_path)
    seen: list = []
    assert end_session(w.facts, w.seams(Control(), fault=seen.append), record_state=REVOKED) == RETAINED
    assert [p for p in seen if p.startswith("park:")] == list(D1_POINTS)
    w, ctl = held_before_d1(tmp_path / "held")
    seen.clear()
    reconcile_session(w.facts, w.seams(ctl, fault=seen.append), held_attempts=1)
    assert [p for p in seen if p.startswith("park:")] == list(D1_HELD_POINTS)


def _crash_once(point):
    fired = []

    def fault(p):
        if p == point and not fired:
            fired.append(p)
            raise Crash(p)
    return fault, fired


@pytest.mark.parametrize("point", list(D1_POINTS))
def test_a_crash_at_each_d1_point_converges(tmp_path, point):
    w = login_world(tmp_path)
    ctl = Control()
    fault, fired = _crash_once(point)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
    assert fired == [point]
    at = where(w)
    assert at == D1_POINTS[point]
    assert at["keep"] == "old"                                  # keep/ is never touched
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    assert where(w) == {"keep": "old", "unconfirmed": "new"}
    assert not w.facts.intent.exists()


@pytest.mark.parametrize("point", list(D1_HELD_POINTS))
def test_a_crash_at_each_point_of_the_held_retry_converges(tmp_path, point):
    w, ctl = held_before_d1(tmp_path)
    fault, fired = _crash_once(point)
    with pytest.raises(Crash):
        reconcile_session(w.facts, w.seams(ctl, fault=fault), held_attempts=1)
    assert fired == [point]
    assert where(w) == D1_HELD_POINTS[point]
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams, held_attempts=ctl.fields.get("held_attempts"))
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step,
                       held_attempts=ctl.fields.get("held_attempts")) == RETAINED
    assert where(w) == {"keep": "old", "unconfirmed": "new"}
    assert not w.facts.intent.exists()


_KILL_CHILD_D1 = """
import os, signal, sys
from pathlib import Path
from tests.test_slot_idg2_relogin import login_world
from tests.test_slot_s2c_end import Control
from levain.slot.end import REVOKED, end_session
tmp, point = Path(sys.argv[1]), sys.argv[2]
w = login_world(tmp)
ctl = Control()
def fault(p):
    if p == point:
        os.kill(os.getpid(), signal.SIGKILL)
end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
"""


@pytest.mark.parametrize("point", ["park:unconfirmed:dirs", "park:unconfirmed:renamed", "park:unconfirmed:aside"])
def test_a_sigkilled_unconfirmed_end_converges(tmp_path, point):
    """A real process death before and after the set-aside's rename, then a fresh END in this process from the record
    the child left (DEAD, the park journal at ``move``)."""
    child = tmp_path / "child.py"
    child.write_text(_KILL_CHILD_D1)
    wt = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(child), str(tmp_path / "w"), point], capture_output=True, timeout=120,
                       cwd=wt, env={**os.environ, "PYTHONPATH": str(wt)})
    assert r.returncode == -signal.SIGKILL, r.stderr.decode()[-2000:]
    w = World.__new__(World)
    w.tmp, w.L, w.vf = tmp_path / "w", tmp_path / "w" / "L", None
    w.S = w.L / "live" / SESSION
    w.facts = replace(end_mod.EndFacts(SESSION, SlotId("egress", 3), UID, GID, w.L, None, lease_seq=LEASE_SEQ,
                                       vendor="claude", auth="passthrough"), kind="login")
    assert where(w) == D1_POINTS[point]
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "move"
    ctl = Control(state=DEAD)
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    assert where(w) == {"keep": "old", "unconfirmed": "new"}


def test_the_unconfirmed_credential_follows_discard_restore_and_purge(tmp_path):
    w = login_world(tmp_path)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    for d in ("trash", "export/operator"):
        (w.L / d).mkdir(parents=True, exist_ok=True)
    sf = SV.StoreFacts(session=SESSION, store=w.L, kind="login", operator_uid=OP_UID)
    ss = SV.StoreSeams(cas=ctl.cas, owner_ok=mine, now=lambda: NOW)
    assert SV.discard(sf, ss) == R.TRASH
    assert (sf.trash / end_mod.UNCONFIRMED / NAME).read_bytes() == CRED and not unconfirmed(w).exists()
    assert SV.restore(sf, ss) == R.RETAINED
    assert unconfirmed(w).read_bytes() == CRED
    assert SV.discard(sf, ss) == R.TRASH
    assert SV.purge(sf, ss, forced=True) == R.PURGED
    assert not sf.trash.exists() and not unconfirmed(w).exists()
    assert (w.facts.keep / NAME).read_bytes() == OLD             # the parked login is untouched throughout


# --- replay finishes every durability step the original run owed (1027-19 RULINGS "G2 L3 r1", H1 + H2) ----------------


def _redo_points(points: dict) -> list:
    """The points a crash at which leaves the park journal at ``move``, so the restart redoes :func:`park_move`
    (from ``park:after:commit`` on, the move finished whole before the journal advanced)."""
    out = []
    for p in points:
        if p == "park:after:commit":
            return out
        out.append(p)
    return out


def _ino(p: Path) -> tuple:
    st = os.lstat(p)
    return st.st_dev, st.st_ino


def _trace_sync(monkeypatch) -> list:
    """Every ``os.fsync`` (as the synced inode) and every rename of the credential, in call order."""
    log: list = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        st = os.fstat(fd)
        log.append(("sync", (st.st_dev, st.st_ino)))
        return real_fsync(fd)

    def rename(src, dst, *a, **k):
        if os.fsdecode(src) == NAME:
            log.append(("rename", None))
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    return log


def _owed(w: World) -> list:
    """Every directory a rename or mkdir of the crashed run may have left unsynced: the credential's tree directory,
    keep/ and its parent, and, when it exists, replaced/ or unconfirmed/ with retained/<s>/ and L/retained/."""
    out = [w.S / "config" / "claude", w.facts.keep, w.facts.keep.parent]
    for d in (w.facts.retained / end_mod.REPLACED, w.facts.retained / end_mod.UNCONFIRMED):
        if os.path.lexists(d):
            out += [d, w.facts.retained, w.facts.retained.parent]
    return out


@pytest.mark.parametrize("path,point", [("confirmed", p) for p in _redo_points(CRASH_POINTS)]
                         + [("unconfirmed", p) for p in _redo_points(D1_POINTS)])
def test_the_park_redo_resyncs_every_directory_before_the_credential_moves(tmp_path, monkeypatch, path, point):
    """A crash at the point, then the restart (reaper step 2, then END from the record): before it renames the
    credential at all, the redo has fsynced every directory the crashed run may have left unsynced. The power loss
    that would make an unsynced entry vanish cannot be simulated in a test; what is asserted is the fsync calls."""
    w = login_world(tmp_path)
    if path == "confirmed":
        confirm(w)
    ctl = Control()
    fault, fired = _crash_once(point)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
    assert fired == [point]
    owed = _owed(w)
    want = {p: _ino(p) for p in owed}
    log = _trace_sync(monkeypatch)
    seams = w.seams(ctl)
    reconcile_session(w.facts, seams)
    assert end_session(w.facts, seams, record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    monkeypatch.undo()
    first = next((i for i, (k, _) in enumerate(log) if k == "rename"), len(log))
    synced = {v for k, v in log[:first] if k == "sync"}
    missing = " ".join(os.path.relpath(p, w.L) for p, i in want.items() if i not in synced)
    assert not missing, f"not fsynced before the credential moved: {missing}"
    assert where(w) == ({"keep": "new", "replaced": "old"} if path == "confirmed"
                        else {"keep": "old", "unconfirmed": "new"})


# --- the set-aside's directories and source (1027-19 RULINGS "G2 L3 r1", M1, L1, L2) ------------------------------------


@pytest.mark.parametrize("how", ["missing", "a-file"])
@pytest.mark.parametrize("path", ["confirmed", "unconfirmed"])
def test_a_broken_retained_root_is_bad_root_never_an_unnamed_error(tmp_path, path, how):
    """M1: ``L/retained/`` missing or not a directory: the set-aside's mkdir fails, and END ends HELD ``bad-root``
    with both credentials where they were."""
    w = login_world(tmp_path)
    if path == "confirmed":
        confirm(w)
    os.rmdir(w.facts.retained.parent)
    if how == "a-file":
        w.facts.retained.parent.write_bytes(b"")
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("bad-root" in n for n in w.notes)
    assert where(w) == {"keep": "old", "tree": "new"}


@pytest.mark.parametrize("path,sub", [("confirmed", end_mod.REPLACED), ("unconfirmed", end_mod.UNCONFIRMED)])
def test_an_existing_set_aside_dir_is_made_0700(tmp_path, path, sub):
    """L1: an existing ``replaced/`` or ``unconfirmed/`` with the right type and owner is chmodded 0700."""
    w = login_world(tmp_path)
    if path == "confirmed":
        confirm(w)
    (w.facts.retained / sub).mkdir(parents=True)
    os.chmod(w.facts.retained / sub, 0o755)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert os.stat(w.facts.retained / sub).st_mode & 0o777 == 0o700


def _not_a_file(at: Path, what: str, tmp_path: Path) -> None:
    at.unlink()
    if what == "dir":
        at.mkdir()
    else:
        (tmp_path / "target").write_bytes(OLD)
        os.symlink(tmp_path / "target", at)


@pytest.mark.parametrize("what", ["dir", "link"])
def test_a_confirmed_relogin_never_sets_aside_a_non_file_at_keep(tmp_path, what):
    """L2: only a regular file at keep/<name> is set aside; anything else refuses ``keep-occupied`` as before IG-1."""
    w = login_world(tmp_path)
    confirm(w)
    _not_a_file(w.facts.keep / NAME, what, tmp_path)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("keep-occupied" in n for n in w.notes)
    assert os.path.lexists(w.facts.keep / NAME) and not os.path.lexists(replaced(w))
    assert (w.S / "config" / "claude" / NAME).read_bytes() == CRED


def test_an_unconfirmed_login_never_sets_aside_a_link_from_the_tree(tmp_path):
    """L2, D1's source: a link at the tree's credential path is not set aside into ``unconfirmed/``."""
    w = login_world(tmp_path)
    _not_a_file(w.S / "config" / "claude" / NAME, "link", tmp_path)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("keep-occupied" in n for n in w.notes)
    assert not os.path.lexists(unconfirmed(w)) and where(w)["keep"] == "old"
