"""S14 piece D2: the reaper's artifact step (CONTRACT §7.7 3b; design D page §3 D2, §5 T1/T5/T6/T8).

Rows run on the s5c2c macOS seam rig (the REAL per-run image, walker, importer and publish, as this uid) with the alloc
rewritten to the dispatch class, a RETAINED record under ``L/sessions`` and a CONTROL fake that applies the CAS to that
record. VM residue, not run here: the real ``_levain_export`` drop, the importer wall, the Linux ``export.scope`` leaf,
the real chown to the ``levain-dispatch-export`` gid, and timing on a real repo.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sys
import threading
from dataclasses import replace

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import export_run as ER
from levain.slot import artifact_step as AS
from levain.slot import reaper as RP
from levain.slot import records as R
from levain.slot import runtime_helper as RH
from levain.slot.services import reaper as SR

TICKET = "A" * 43
MANIFEST = "sha256:" + "1" * 64
FILE = "dispatch-artifact.json"
# The rig below is s5c2c's, which makes the REAL per-run image with macOS hdiutil; a Linux host cannot make it, so the
# tick holds every session as export-image-unavailable. Same gate as that file's own (the marker does not travel with
# the imported Rig); the Linux image is the VM residue named in the docstring.
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("git") is None or not os.path.exists(ER.HDIUTIL),
                                reason="the seam run needs macOS hdiutil and a git")


class Control:
    """CONTROL's CAS on ``L/sessions/<s>.json``: ``from``, ``to`` and ``generation`` must match; fields are merged."""

    def __init__(self, L) -> None:
        self.L, self.calls, self.fail = L, [], []

    def call(self, op, **f):
        self.calls.append((op, f["from"], f["to"], dict(f["fields"])))
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e
        p = self.L / "sessions" / f"{f['session']}.json"
        rec = R.parse_state(p.read_bytes())
        if op != "CAS" or rec.state != f["from"] or rec.generation != f["generation"]:
            raise Refused("from-mismatch", f"{rec.state} -> {f['to']}")
        fields = {**rec.fields, **f["fields"]}
        p.write_bytes(R.state_bytes(replace(rec, generation=rec.generation + 1, event_kind="artifact", fields=fields)))
        return {"ok": True}

    def close(self) -> None:
        pass


@pytest.fixture
def rigs(tmp_path):
    from tests.test_slot_s5c2c_adopt_open import Rig
    made = []

    def make(kind="delta", **kw):
        d = tmp_path / f"r{len(made)}"
        d.mkdir()
        r = Rig(d, kind, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        r.cleanup()


class World:
    def __init__(self, rig, S, *, adopt=None, seal=None):
        self.rig, self.S, self.L = rig, S, rig.L
        self.control = Control(rig.L)
        self.logs: list = []
        self.seals = 0
        base = rig.seams()
        run = base.adopt_run
        if seal is not None:
            run = replace(run, seal=seal)
        else:
            inner = run.seal

            def counting(facts):
                self.seals += 1
                return inner(facts)
            run = replace(run, seal=counting)
        self.seams = replace(base, adopt_run=run, adopt=adopt, client_factory=lambda path: self.control)
        self.worker = AS.ArtifactWorker(self.seams, log=self.logs.append)

    @property
    def pub(self):
        return self.L / "export" / "dispatch" / self.S

    def listing(self):
        return sorted(os.listdir(self.pub)) if self.pub.exists() else []

    def record(self):
        return R.parse_state((self.L / "sessions" / f"{self.S}.json").read_bytes())


def _world(rigs, kind="delta", *, alloc_kind="dispatch", manifest=True, adopt=None, seal=None, **kw):
    from tests.test_slot_s5c2c_adopt_open import S, UID
    rig = rigs(kind, **kw)
    p = rig.L / "alloc" / f"{S}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(replace(a, kind=alloc_kind, ticket=TICKET, supervisor_uid=UID + 1)))
    obj_path = rig.L / "retained" / S / "object.json"
    obj = canon.parse(obj_path.read_bytes())
    if manifest:
        obj["manifest_digest"] = MANIFEST
        os.chmod(obj_path, 0o600)
        obj_path.write_bytes(canon.canonical_bytes(obj))
    (rig.L / "sessions").mkdir()
    (rig.L / "sessions" / f"{S}.json").write_bytes(R.state_bytes(R.SessionState(
        session=S, generation=3, state=R.RETAINED, claims={}, lease_live=False, event_kind="state",
        updated_at="2026-10-01T00:00:00Z")))
    return World(rig, S, adopt=adopt, seal=seal), obj


def _digest(path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# --- the red the design recorded: the pass named 3b unbuilt ---------------------------------------------------------

def test_the_pass_no_longer_names_3b_unbuilt_and_still_names_5(rigs):
    w, _ = _world(rigs)
    rep = RP.Reaper(w.seams, log=w.logs.append).run_pass()
    assert "step3b:seam-unbuilt" not in rep["skipped"]
    assert "step5:seam-unbuilt" in rep["skipped"]
    assert w.listing() == [] and w.seals == 0               # 3b is the worker's, never a pass step (T9)
    assert not [c for c in w.control.calls if "artifact_digest" in c[3] or "artifact_failed" in c[3]]


# --- the tick -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["delta", "nothing-new"])
def test_one_tick_publishes_roots_record_and_cas_names_its_digest(rigs, kind):
    w, _ = _world(rigs, kind)
    rep = w.worker.tick()
    assert rep == {"exported": [w.S], "artifact_failed": [], "skipped": [], "held": []}
    assert w.listing() == (["bundle", FILE] if kind == "delta" else [FILE])
    assert w.control.calls == [("CAS", R.RETAINED, R.RETAINED, {"artifact_digest": _digest(w.pub / FILE)})]
    assert w.record().fields["artifact_digest"] == _digest(w.pub / FILE)
    n = w.seals
    assert w.worker.tick() == AS.new_report() and w.seals == n and len(w.control.calls) == 1   # done: nothing more


def test_a_planner_is_selected_and_a_verifier_never_is(rigs):
    w, _ = _world(rigs, alloc_kind="planner")
    assert w.worker.tick()["exported"] == [w.S]
    v, _ = _world(rigs, alloc_kind="verifier")
    assert v.worker.tick() == AS.new_report() and v.listing() == [] and v.control.calls == [] and v.seals == 0


def test_a_record_that_is_not_retained_or_already_names_a_result_is_not_selected(rigs):
    w, _ = _world(rigs)
    p = w.L / "sessions" / f"{w.S}.json"
    rec = w.record()
    for variant in (replace(rec, state=R.TRASH), replace(rec, fields={"artifact_failed": "adopt-cap:memory"}),
                    replace(rec, fields={"artifact_digest": MANIFEST})):
        p.write_bytes(R.state_bytes(variant))
        assert w.worker.tick() == AS.new_report()
    assert w.seals == 0 and w.listing() == [] and w.control.calls == []


# --- the locks: T6 --------------------------------------------------------------------------------------------------

def test_a_held_adopt_lock_is_a_skip_with_no_wait_and_no_count(rigs):
    w, _ = _world(rigs)
    fd = os.open(w.L / "export" / "adopt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rep = w.worker.tick()
    finally:
        os.close(fd)
    assert rep["skipped"] == [f"{w.S}:adopt-busy"] and rep["exported"] == [] and rep["held"] == []
    assert w.worker.tries == {} and w.control.calls == [] and w.listing() == [] and w.seals == 0
    assert w.worker.tick()["exported"] == [w.S]                  # the lock freed: the next tick imports


def test_a_held_session_lock_is_a_skip(rigs):
    w, _ = _world(rigs)
    fd = os.open(w.L / "alloc" / RH.session_lock_name(w.S), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rep = w.worker.tick()
    finally:
        os.close(fd)
    assert rep["skipped"] == [f"{w.S}:session-busy"] and w.worker.tries == {}
    assert w.control.calls == [] and w.listing() == [] and w.seals == 0


# --- the refusals: D5 -----------------------------------------------------------------------------------------------

def test_a_walker_cap_is_final_at_once_and_the_next_tick_does_nothing(rigs):
    w, _ = _world(rigs, adopt=RH.AdoptSeams(max_bytes=1))
    rep = w.worker.tick()
    assert rep["artifact_failed"] == [f"{w.S}:walker-cap:bytes"] and rep["exported"] == []
    assert w.control.calls == [("CAS", R.RETAINED, R.RETAINED, {"artifact_failed": "walker-cap:bytes"})]
    assert w.record().fields == {"artifact_failed": "walker-cap:bytes"} and w.listing() == []
    assert w.worker.tick() == AS.new_report() and len(w.control.calls) == 1


def test_a_pre_row_165_object_with_no_manifest_digest_is_final_at_once(rigs):
    w, _ = _world(rigs, manifest=False)
    rep = w.worker.tick()
    assert rep["artifact_failed"] == [f"{w.S}:not-retained:absent"]
    assert w.control.calls[0][3] == {"artifact_failed": "not-retained:absent"} and w.listing() == []


def test_three_image_failures_fail_the_artifact_on_the_third(rigs):
    def seal(facts):
        raise Refused("export-image-unavailable", "seam")
    w, _ = _world(rigs, seal=seal)
    r1, r2 = w.worker.tick(), w.worker.tick()
    assert r1["held"] == r2["held"] == [f"{w.S}:export-image-unavailable"] and w.control.calls == []
    r3 = w.worker.tick()
    assert r3["artifact_failed"] == [f"{w.S}:export-image-unavailable"]
    assert [c[3] for c in w.control.calls] == [{"artifact_failed": "export-image-unavailable"}]
    assert w.listing() == [] and w.worker.tick() == AS.new_report()


def test_a_slot_not_proven_scrubbed_holds_and_publishes_nothing(rigs):
    w, S = _world(rigs)[0], None
    from tests.test_slot_s5c2c_adopt_open import S
    a = R.parse_alloc((w.L / "alloc" / f"{S}.json").read_bytes())
    (w.L / "slots" / a.slot.tag / "state").write_bytes(R.slot_state_bytes(R.SlotState(
        slot=a.slot, rev=12, lease_seq=a.lease_seq, state="leased", session=S, dispatch_eligible=False,
        changed_at="2026-10-01T00:00:00Z")))
    rep = w.worker.tick()
    assert rep["held"] == [f"{S}:not-scrubbed"] and w.control.calls == [] and w.listing() == [] and w.seals == 0


def test_the_importers_lying_report_does_not_reach_the_record(rigs):
    # T1: the counts are object.json's, not the walk's in report.json (the publish never copies report.json).
    w, obj = _world(rigs)
    obj["entries"], obj["bytes"] = obj["entries"] + 7, obj["bytes"] + 13
    (w.L / "retained" / w.S / "object.json").write_bytes(canon.canonical_bytes(obj))
    w.worker.tick()
    rec = canon.parse((w.pub / FILE).read_bytes())
    assert (rec["files"], rec["bytes"]) == (obj["entries"], obj["bytes"]) and "report.json" not in w.listing()


# --- a crash between the commit and the CAS: D6 ---------------------------------------------------------------------

def test_a_crash_between_the_commit_and_the_cas_runs_again_and_names_the_new_file(rigs):
    w, _ = _world(rigs)
    w.control.fail = [OSError(5, "seam: the CAS never answered")]
    rep = w.worker.tick()
    assert rep["exported"] == [] and w.listing() == ["bundle", FILE]        # published, not yet named
    assert "artifact_digest" not in w.record().fields
    rep = w.worker.tick()                                                   # the next tick runs it again (EXCHANGE)
    assert rep["exported"] == [w.S] and w.seals == 2
    assert [c[3] for c in w.control.calls] == [{"artifact_digest": _digest(w.pub / FILE)}] * 2
    assert w.listing() == ["bundle", FILE] and not (w.L / "export" / "dispatch" / (w.S + ".new")).exists()
    assert w.record().fields["artifact_digest"] == _digest(w.pub / FILE)


# --- the CAS refusals -----------------------------------------------------------------------------------------------

def test_a_stale_cas_is_logged_and_the_next_tick_names_it(rigs):
    w, _ = _world(rigs)
    w.control.fail = [Refused("stale-generation", "seam")]
    rep = w.worker.tick()
    assert rep["skipped"] == [f"{w.S}:stale-generation"] and w.worker.tries == {} and w.worker.parked == set()
    assert w.worker.tick()["exported"] == [w.S]


def test_a_cas_control_will_not_take_parks_the_session_after_the_attempts(rigs):
    w, _ = _world(rigs)
    w.control.fail = [Refused("artifact-not-executor", "seam")] * 3
    for _i in range(3):
        w.worker.tick()
    assert w.worker.parked == {w.S} and w.seals == 3 and len(w.control.calls) == 3
    assert w.worker.tick() == AS.new_report() and w.seals == 3 and len(w.control.calls) == 3


def test_a_refused_artifact_failed_cas_counts_once_per_tick_not_twice(rigs):
    """S14 L3 F2: a permanent refusal (walker-cap:bytes) whose `artifact_failed` CAS CONTROL refuses (non-stale) is ONE
    count per tick, so the session is parked only after ARTIFACT_ATTEMPTS refused CASes. Asserts: not parked after two
    ticks (two CAS calls), parked after the third, and the import refusal's own count stays in `tries`. RED on the old
    code: `_refused` and `_set` bumped the same `tries`, so tick 1 counted 2 and tick 2 counted 4, parking the session
    after two CAS calls."""
    w, _ = _world(rigs, adopt=RH.AdoptSeams(max_bytes=1))
    w.control.fail = [Refused("artifact-not-executor", "seam")] * 3
    w.worker.tick()
    w.worker.tick()
    assert w.worker.parked == set() and len(w.control.calls) == 2 and w.worker.cas_refusals == {w.S: 2}
    w.worker.tick()
    assert w.worker.parked == {w.S} and len(w.control.calls) == 3 and w.worker.tries == {w.S: 3}
    assert "artifact_failed" not in w.record().fields


def test_a_session_no_longer_wanted_or_gone_loses_both_counts(rigs):
    """S14 L3 round 2 (two lineages): `tries` and `cas_refusals` are dropped once a tick sees the session no longer
    wanted (another worker moved it out of RETAINED) or absent (its record removed), so the long-lived artifact child
    never keeps an entry for a session it will not see again. Asserts: a machine refusal's `tries` entry is gone the
    tick after the record turns TRASH, and a non-stale CAS refusal's `cas_refusals` entry is gone the tick after the
    record is removed. RED on the old code: both dicts were popped only by a CAS CONTROL took, so `tries == {S: 1}` and
    `cas_refusals == {S: 1}` stayed for ever."""
    def seal(facts):
        raise Refused("export-image-unavailable", "seam")
    w, _ = _world(rigs, seal=seal)
    assert w.worker.tick()["held"] == [f"{w.S}:export-image-unavailable"] and w.worker.tries == {w.S: 1}
    (w.L / "sessions" / f"{w.S}.json").write_bytes(R.state_bytes(replace(w.record(), state=R.TRASH)))
    assert w.worker.tick() == AS.new_report() and w.worker.tries == {} and w.worker.cas_refusals == {}
    c, _ = _world(rigs)
    c.control.fail = [Refused("artifact-not-executor", "seam")]
    c.worker.tick()
    assert c.worker.cas_refusals == {c.S: 1} and c.worker.parked == set()
    (c.L / "sessions" / f"{c.S}.json").unlink()
    assert c.worker.tick() == AS.new_report() and c.worker.cas_refusals == {} and c.worker.tries == {}


def test_control_down_runs_no_import(rigs):
    w, _ = _world(rigs)

    def down(path):
        raise Refused("control-down", "seam")
    w.seams.client_factory = down
    rep = w.worker.tick()
    assert rep["skipped"] == ["control-down:control-down"] and w.seals == 0 and w.listing() == []


# --- the classification table ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,want", [
    ("walker-cap:bytes", "permanent"), ("walker-cap:entries", "permanent"), ("adopt-cap:memory", "permanent"),
    ("adopt-work-cap", "permanent"), ("adopt-path-too-long", "permanent"), ("bundle-header-mismatch", "permanent"),
    ("export-output-shape:bundle", "permanent"), ("importer-failed:exit", "permanent"),
    ("importer-failed:time", "permanent"), ("importer-failed:result-malformed", "permanent"),
    ("not-retained:absent", "permanent"), ("base-unrecorded", "permanent"), ("artifact-record:size", "permanent"),
    ("importer-failed:publish", "machine"), ("export-image-unavailable", "machine"),
    ("export-domain-unproven", "machine"), ("adopt-space", "machine"), ("adopt-space-exhausted", "machine"),
    ("not-scrubbed", "machine"), ("intent-in-flight", "skip"), ("OSError", "machine"),
    ("git-below-floor:2.1", "machine"), ("a-name-nobody-listed", "machine"),
    ("session-busy", "skip"), ("adopt-busy", "skip")])
def test_classify(name, want):
    assert AS.classify(name) == want


# --- the service: the third child -----------------------------------------------------------------------------------

class _FakeWorker:
    def __init__(self, raises=()):
        self.n, self.raises = 0, list(raises)
        self.seams = type("S", (), {"monotonic": staticmethod(lambda: 0.0)})()

    def tick(self):
        self.n += 1
        if self.raises:
            e = self.raises.pop(0)
            if e is not None:
                raise e
        return AS.new_report()


def test_the_artifact_loop_runs_its_ticks_and_survives_a_bad_one():
    r, w = os.pipe()
    os.set_blocking(r, False)
    try:
        fake = _FakeWorker(raises=[RuntimeError("seam"), None])
        logs: list = []
        assert SR.artifact_loop(fake, r, ticks=3, tick_s=0.0, log=logs.append) == 0
        assert fake.n == 3 and any("artifact tick failed" in m for m in logs)
    finally:
        os.close(r)
        os.close(w)


def test_the_artifact_loop_ends_when_the_main_process_is_gone():
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.close(w)
    try:
        fake = _FakeWorker()
        assert SR.artifact_loop(fake, r, tick_s=0.0, log=lambda m: None) == 0 and fake.n == 0
    finally:
        os.close(r)


from tests.test_slot_r2_fix1_service import _serve, world  # noqa: E402,F401  (the service's fork-and-fake fixture)


def test_serve_supervises_the_artifact_child_beside_the_other_two(world, monkeypatch):
    seams, log, logf = world

    class Fake:
        def __init__(self, seams, **_kw):
            self.seams = seams

        def tick(self):
            log("fake artifact tick")
            return AS.new_report()
    monkeypatch.setattr(AS, "ArtifactWorker", Fake)
    assert _serve(seams, log, worker_passes=1, revoker_wakes=1, artifact_ticks=2) == 0
    text = logf.read_text()
    assert text.count("started artifact") == 1 and text.count("fake artifact tick") == 2
    assert "restarting" not in text


def test_a_dead_artifact_child_is_restarted_like_the_others(world, monkeypatch):
    seams, log, logf = world

    class Dies:
        def __init__(self, seams, **_kw):
            self.seams = seams

        def tick(self):
            os._exit(3)
    monkeypatch.setattr(AS, "ArtifactWorker", Dies)
    monkeypatch.setattr(SR, "RESTART_S", 0.05)
    # the worker's bound ends the run after a restart has been seen
    import time
    t0 = time.monotonic()
    assert _serve(seams, log, worker_passes=1, revoker_wakes=1) == 0
    assert time.monotonic() - t0 < 20
    assert "artifact pid" in logf.read_text() and "died (3); restarting" in logf.read_text()


# --- the head's ruling: a discard intent in flight is a skip, a slot not proven scrubbed is still a counted failure ---

def _intent_path(w):
    from levain.slot import store_verbs as SV
    a = R.parse_alloc((w.L / "alloc" / f"{w.S}.json").read_bytes())
    return SV.facts_from_alloc(a, w.rig.L).intent


def test_a_long_lived_discard_intent_is_a_skip_and_never_fails_the_artifact(rigs):
    w, _ = _world(rigs)
    _intent_path(w).write_bytes(b"{}")
    for _ in range(AS.ARTIFACT_ATTEMPTS + 3):
        rep = w.worker.tick()
        assert rep["skipped"] == [f"{w.S}:intent-in-flight"] and rep["held"] == [] and rep["artifact_failed"] == []
    assert w.control.calls == [] and w.listing() == [] and w.seals == 0 and w.worker.tries == {}
    _intent_path(w).unlink()
    assert w.worker.tick()["exported"] == [w.S]


def test_not_scrubbed_still_counts_and_fails_on_the_third(rigs):
    w, _ = _world(rigs)
    from tests.test_slot_s5c2c_adopt_open import S
    a = R.parse_alloc((w.L / "alloc" / f"{S}.json").read_bytes())
    (w.L / "slots" / a.slot.tag / "state").write_bytes(R.slot_state_bytes(R.SlotState(
        slot=a.slot, rev=12, lease_seq=a.lease_seq, state="leased", session=S, dispatch_eligible=False,
        changed_at="2026-10-01T00:00:00Z")))
    assert w.worker.tick()["held"] == [f"{S}:not-scrubbed"] and w.worker.tick()["held"] == [f"{S}:not-scrubbed"]
    assert w.worker.tick()["artifact_failed"] == [f"{S}:not-scrubbed"]
