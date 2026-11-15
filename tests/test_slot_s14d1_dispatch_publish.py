"""S14 piece D1: the sealed run's publish carries root's record (CONTRACT §2.1, §10.2; design D page §3 D1).

Rows run on the s5c2c macOS seam rig (the REAL per-run image, walker, importer and publish, as this uid) with the
alloc rewritten to the dispatch class, driven by hand in the reaper's own ctx (``caller="reaper"``, uid 0 seams).
VM residue, not run here: the real chown to the ``levain-dispatch-export`` gid (the rig answers ``publish_gid`` None),
the real ``_levain_export`` drop, and timing on a real repo.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import export_run as ER
from levain.slot import records as R
from levain.slot import runtime_helper as RH

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists(ER.HDIUTIL),
                                reason="the rows run the REAL macOS per-run image (hdiutil)")

TICKET = "A" * 43
MANIFEST = "sha256:" + "1" * 64
FILE = "dispatch-artifact.json"


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


def _dispatch_rig(rigs, kind="delta", *, manifest=True, **kw):
    from tests.test_slot_s5c2c_adopt_open import S, UID
    rig = rigs(kind, **kw)
    p = rig.L / "alloc" / f"{S}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(replace(a, kind="dispatch", ticket=TICKET, supervisor_uid=UID + 1)))
    obj_path = rig.L / "retained" / S / "object.json"
    obj = canon.parse(obj_path.read_bytes())
    if manifest:
        obj["manifest_digest"] = MANIFEST
        os.chmod(obj_path, 0o600)
        obj_path.write_bytes(canon.canonical_bytes(obj))
    return rig, S, obj


def _seal(rig, S, **run_kw):
    """Steps 3-12 by hand as the reaper's ctx; returns ``(answer, front)``."""
    seams = rig.seams()
    ctx = RH.Ctx(seams=seams, store=RH.Store(seams), sudo_uid=0, caller="reaper", operator_uid=-1, env={})
    a = R.parse_alloc((rig.L / "alloc" / f"{S}.json").read_bytes())
    adopt = RH.AdoptSeams()
    try:
        with RH.session_lock(ctx, S):
            front = RH.adopt_step3_root_facts(ctx, a, adopt, deadline=seams.monotonic() + adopt.lock_wait_s)
            with RH.adopt_lock_wait(ctx, front.deadline):
                RH.adopt_step5_precaps(ctx, front, adopt)
                ans = RH.adopt_sealed_run(ctx, front, adopt, seams.adopt_run, **run_kw)
    finally:
        ctx.close()
        ctx.store.close()
    return ans, front


def _listing(rig, S):
    return sorted(os.listdir(rig.L / "export" / "dispatch" / S))


@pytest.mark.parametrize("kind", ["delta", "nothing-new"])
def test_the_dispatch_publish_is_the_bundle_and_roots_record_only(rigs, kind):
    from levain.dispatch.export import read_artifact
    rig, S, obj = _dispatch_rig(rigs, kind)
    ans, _front = _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    pub = rig.L / "export" / "dispatch" / S
    # report.json is checked and NOT copied (needs-phill 1, ruled: no)
    assert _listing(rig, S) == (["bundle", FILE] if kind == "delta" else [FILE])
    assert "report_digest" not in ans
    assert oct(os.lstat(pub).st_mode & 0o777) == "0o750"
    for n in os.listdir(pub):
        assert oct(os.lstat(pub / n).st_mode & 0o777) == "0o640", n
    art = read_artifact(rig.L / "export" / "dispatch", S)
    data = (pub / FILE).read_bytes()
    assert ans["artifact_digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert (art.session, art.ticket, art.manifest_digest) == (S, TICKET, MANIFEST)
    assert (art.files, art.bytes) == (obj["entries"], obj["bytes"])      # root's counts: object.json's
    if kind == "delta":
        assert art.bundle == "bundle"
        assert art.bundle_digest == "sha256:" + hashlib.sha256((pub / "bundle").read_bytes()).hexdigest()
        assert art.bundle_digest == ans["bundle_digest"]
    else:
        assert (art.bundle, art.bundle_digest) == (None, None) and ans["bundle_digest"] is None
    assert canon.parse(data)["format"] == "levain-dispatch-artifact/1"


def test_the_counts_are_object_jsons_and_not_the_importers_walk(rigs):
    # T1: make object.json's counts differ from what the importer's report.json walk would say; the record says
    # object.json's. The precaps read the same two numbers, so they stay inside V7.
    rig, S, obj = _dispatch_rig(rigs)
    obj["entries"], obj["bytes"] = obj["entries"] + 7, obj["bytes"] + 13
    p = rig.L / "retained" / S / "object.json"
    p.write_bytes(canon.canonical_bytes(obj))
    _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    rec = canon.parse((rig.L / "export" / "dispatch" / S / FILE).read_bytes())
    assert (rec["files"], rec["bytes"]) == (obj["entries"], obj["bytes"])


@pytest.mark.parametrize("point", ["fsync:dispatch-artifact.json", "copy:dispatch-artifact.json", "commit",
                                   "fsync:staged"])
def test_a_fault_before_the_commit_publishes_nothing_and_leaves_no_staging(rigs, point):
    def fault(p):
        if p == point:
            raise OSError(errno.EIO, "seam")
    rig, S, _ = _dispatch_rig(rigs, fault=fault)
    with pytest.raises(Refused) as ei:
        _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    assert ei.value.name == "importer-failed:publish"
    cls = rig.L / "export" / "dispatch"
    assert not (cls / S).exists() and not (cls / (S + ".new")).exists()


def test_a_refusing_record_builder_publishes_nothing(rigs):
    rig, S, _ = _dispatch_rig(rigs, manifest=False)       # an object with no manifest_digest
    with pytest.raises(Refused) as ei:
        _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    assert ei.value.name == "not-retained:absent"
    cls = rig.L / "export" / "dispatch"
    assert not (cls / S).exists() and not (cls / (S + ".new")).exists()


def test_a_record_builder_that_raises_anything_fails_closed(rigs):
    rig, S, _ = _dispatch_rig(rigs)

    def boom(front, digests):
        raise KeyError("x")
    with pytest.raises(Refused) as ei:
        _seal(rig, S, export_class="dispatch", artifact=boom)
    assert ei.value.name == "importer-failed:publish"
    cls = rig.L / "export" / "dispatch"
    assert not (cls / S).exists() and not (cls / (S + ".new")).exists()


@pytest.mark.parametrize("export_class,artifact", [("dispatch", None), ("operator", RH.artifact_record_bytes)])
def test_the_class_and_the_record_go_together(rigs, export_class, artifact):
    rig, S, _ = _dispatch_rig(rigs)
    with pytest.raises(Refused) as ei:
        _seal(rig, S, export_class=export_class, artifact=artifact)
    assert ei.value.name == "artifact-record:class"
    assert not (rig.L / "export" / "dispatch" / S).exists()


def test_the_operator_class_still_publishes_bundle_and_report(rigs):
    # the operator path is byte-for-byte what it was: bundle + report.json, no record
    rig, S, _ = _dispatch_rig(rigs)
    ans, _ = _seal(rig, S)
    assert sorted(os.listdir(rig.L / "export" / "operator" / S)) == ["bundle", "report.json"]
    assert ans["report_digest"].startswith("sha256:") and "artifact_digest" not in ans


def test_a_replay_exchanges_over_the_older_tree(rigs):
    # D6: the next tick after a crash between commit and CAS publishes again, and the exchange leaves one whole tree
    rig, S, _ = _dispatch_rig(rigs)
    first, _ = _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    second, _ = _seal(rig, S, export_class="dispatch", artifact=RH.artifact_record_bytes)
    assert _listing(rig, S) == ["bundle", FILE]
    assert not (rig.L / "export" / "dispatch" / (S + ".new")).exists()
    assert first["result"] == second["result"] == "delta"


# --- the pieces, without the rig ---------------------------------------------------------------------------------

def test_the_record_bytes_are_canonical_root_facts(tmp_path):
    a = _alloc(tmp_path)
    front = _bare_front(a, manifest_digest=MANIFEST, entries=5, nbytes=99)
    data = RH.artifact_record_bytes(front, {"bundle": "ab" * 32})
    assert data == canon.canonical_bytes(canon.parse(data))
    assert canon.parse(data) == {"format": "levain-dispatch-artifact/1", "session": a.session, "ticket": TICKET,
                                 "manifest_digest": MANIFEST, "bundle": "bundle",
                                 "bundle_digest": "sha256:" + "ab" * 32, "files": 5, "bytes": 99}
    nn = canon.parse(RH.artifact_record_bytes(front, {}))
    assert (nn["bundle"], nn["bundle_digest"]) == (None, None)


@pytest.mark.parametrize("what", ["no-manifest", "no-ticket", "verifier-kind", "bad-digest"])
def test_the_record_bytes_fail_closed(tmp_path, what):
    a = _alloc(tmp_path)
    kw = {"manifest_digest": MANIFEST}
    digests = {"bundle": "ab" * 32}
    if what == "no-manifest":
        kw["manifest_digest"] = None
    elif what == "no-ticket":
        a = replace(a, ticket=None)
    elif what == "verifier-kind":
        a = replace(a, kind="verifier")
    elif what == "bad-digest":
        digests = {"bundle": "not-hex"}
    with pytest.raises(Refused):
        RH.artifact_record_bytes(_bare_front(a, **kw), digests)


def test_step3_reads_manifest_digest_and_maps_a_malformed_one_to_none(rigs):
    for given, want in ((MANIFEST, MANIFEST), ("sha256:zz", None)):
        rig, S, obj = _dispatch_rig(rigs)
        obj["manifest_digest"] = given
        (rig.L / "retained" / S / "object.json").write_bytes(canon.canonical_bytes(obj))
        seams = rig.seams()
        ctx = RH.Ctx(seams=seams, store=RH.Store(seams), sudo_uid=0, caller="reaper", operator_uid=-1, env={})
        try:
            a = R.parse_alloc((rig.L / "alloc" / f"{S}.json").read_bytes())
            with RH.session_lock(ctx, S):
                front = RH.adopt_step3_root_facts(ctx, a, RH.AdoptSeams(), deadline=seams.monotonic() + 60)
        finally:
            ctx.close()
            ctx.store.close()
        assert front.manifest_digest == want


def test_the_records_bound_is_the_readers(tmp_path):
    from levain.dispatch import export
    assert RH.ARTIFACT_MAX_BYTES == export.MAX_BYTES
    assert RH.ARTIFACT_FILE == export.FILE and RH.ARTIFACT_FORMAT == export.FORMAT


def test_adopt_lock_try_never_waits_and_never_reads_the_clock(rigs):
    import fcntl
    rig, S, _ = _dispatch_rig(rigs)
    seams = rig.seams()
    ticks = []
    seams.monotonic = lambda: ticks.append(1) or 0.0          # type: ignore[assignment]
    seams.sleep = lambda s: ticks.append("slept")             # type: ignore[assignment]
    ctx = RH.Ctx(seams=seams, store=RH.Store(seams), sudo_uid=0, caller="reaper", operator_uid=-1, env={})
    try:
        with RH.adopt_lock_try(ctx):
            held = RH._ADOPT_LOCK_FDS.copy()
            other = os.open(rig.L / "export" / "adopt.lock", os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with pytest.raises(Refused) as ei:
                    with RH.adopt_lock_try(ctx):
                        pass
                assert ei.value.name == "adopt-busy"
            finally:
                os.close(other)
        assert not (RH._ADOPT_LOCK_FDS & held)                  # released and deregistered
        with RH.adopt_lock_try(ctx):                           # free again
            pass
    finally:
        ctx.close()
        ctx.store.close()
    assert ticks == []


# --- export_run: transact(root_files=) and publish_if_success(copy=) --------------------------------------------

def _transact(tmp_path, files, *, fault=None, gid=None):
    cls = tmp_path / "cls"
    cls.mkdir(exist_ok=True)
    src = tmp_path / "src"
    src.write_bytes(b"payload")
    log = []
    seams = ER.PublishSeams(log=log.append, fault=fault or (lambda p: None))
    dfd = os.open(cls, os.O_RDONLY | os.O_DIRECTORY)
    sfd = os.open(src, os.O_RDONLY)
    try:
        seen = {}

        def rf(digests):
            seen.update(digests)
            return files
        try:
            pub = ER.transact(dfd, "s-x", [("blob", sfd)], ER.IMPORTER, seams, root_files=rf,
                              bundle_check=None)
        except Refused as e:
            return cls, log, seen, e
        return cls, log, seen, pub
    finally:
        os.close(dfd)
        os.close(sfd)


def test_transact_writes_root_files_before_fsync_staged_with_the_copies_digests(tmp_path):
    cls, log, seen, pub = _transact(tmp_path, [("rec.json", b"{}")])
    assert not isinstance(pub, Refused), pub
    assert seen == {"blob": hashlib.sha256(b"payload").hexdigest()}
    assert sorted(os.listdir(cls / "s-x")) == ["blob", "rec.json"]
    assert oct(os.lstat(cls / "s-x" / "rec.json").st_mode & 0o777) == "0o640"
    assert log.index("fsync rec.json") < log.index("fsync staged") < log.index("noreplace")


@pytest.mark.parametrize("files", [[("blob", b"x")], [("a/b", b"x")], [("", b"x")], [("..", b"x")],
                                   [("r", b"x"), ("r", b"y")], [("r", "text")], [("nul\0", b"x")]])
def test_transact_refuses_a_bad_root_file_and_removes_staging(tmp_path, files):
    cls, log, _seen, err = _transact(tmp_path, files)
    assert isinstance(err, Refused) and err.name == "importer-failed:publish"
    assert os.listdir(cls) == []


# --- helpers for the bare rows ----------------------------------------------------------------------------------

def _alloc(tmp_path) -> R.Alloc:
    from tests.test_slot_s5c2c_adopt_open import S, make_store
    L = make_store(tmp_path / "bare", "delta")
    a = R.parse_alloc((L / "alloc" / f"{S}.json").read_bytes())
    return replace(a, kind="dispatch", ticket=TICKET)


def _bare_front(a: R.Alloc, *, manifest_digest, entries=1, nbytes=1) -> RH.AdoptFront:
    return RH.AdoptFront(alloc=a, base={}, retained=Path("/nonexistent"), depth=1, legacy_depth=False,
                         entries=entries, nbytes=nbytes, manifest_bytes=0, deepest_git_path=0, deadline=0.0,
                         manifest_digest=manifest_digest)
