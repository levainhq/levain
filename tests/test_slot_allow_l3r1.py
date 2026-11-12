"""Row 165, L3 round 1 (1112-18 q6): regressions for the findings the review-fix lane closed in slot_allow."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import band_view as bv
from levain.consent import terminal_sign as ts
from levain.slot import slot_allow as SA
from levain.slot import supervisor as sup
from tests.test_slot_allow import Rig, _carried_one, _launch, _rows, qdir  # noqa: F401 - qdir is a fixture


@pytest.fixture
def rig(tmp_path, qdir):  # noqa: F811
    return Rig(tmp_path, qdir)


# --- 1. a verifier or evidence-store outage keeps the signed yes --------------------------------------------------

@pytest.mark.parametrize("outage", ["verifier", "key-read"])
def test_a_signed_yes_outlives_a_verifier_outage_and_carries_once_it_is_back(rig, qdir, monkeypatch, outage):
    from levain.authority import VerifierUnavailable

    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    rig.yes(q)
    real = ts.verify

    def down(*a, **k):
        if outage == "verifier":
            raise VerifierUnavailable("ssh-keygen timed out")
        raise ts.KeyError_("cannot read the key directory")
    monkeypatch.setattr(ts, "verify", down)
    refused: list = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert refused == [] and "WebSearch" in SA._pending(qdir)
    assert SA._pending_doc(qdir)["refused"] == {}
    (why,) = SA.not_carried(qdir, store=rig.qs, judge=rig.judge)
    assert why[0] == "WebSearch" and "kept for the next launch" in why[1]
    # the launch says it, carries nothing, and keeps the record
    settings, _said = _launch(rig, qdir)
    assert settings.get("permissions", {}).get("allow", []) == [] and "WebSearch" in SA._pending(qdir)
    monkeypatch.setattr(ts, "verify", real)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.rules_of(allow) == ["WebSearch"] and "WebSearch" not in SA._pending(qdir)


def test_a_bad_signature_is_still_refused_and_its_record_dropped(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    other = rig.enrol("stranger", "laptop-presence")
    rig.authority.keys.pop(other.fp)              # a key the authority log never held
    msg = rig.message(q)
    sig = rig.sign(msg, key=other)
    sign = bv.Signing("signed", message=msg.decode(), sig=sig, fp=rig.laptop.fp, versions={q.id: bv.subject_version(q)})
    bv._answer_signed(rig.qs, [{"ref": q.id, "option": "yes", "via": "cli", "viewer": "t", "sign": sign}],
                      channel="cli", deadline=None)
    refused: list = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert [r for r, _ in refused] == ["WebSearch"] and "WebSearch" not in SA._pending(qdir)


def test_a_phone_countersign_that_cannot_be_judged_now_keeps_the_yes(rig, qdir, monkeypatch):
    from levain.consent import answer_countersign as ac

    allow = qdir / SA.FILE_NAME
    q = rig.file("Bash(make *)")[SA.asked_text("Bash(make *)")]
    (r,) = bv._answer_signed(rig.qs, [{"ref": q.id, "option": "yes", "via": "cli", "viewer": "t",
                                       "sign": bv.Signing("unsigned", reason="remote")}], channel="cli", deadline=None)
    assert not isinstance(r, Exception)

    def down(store, qid, **kw):
        raise ac.EvidenceUnavailable("the signature could not be verified now")
    monkeypatch.setattr(ac, "countersign_evidence", down)
    judge = SA.Judge(authority=lambda: rig.authority, sign_queue=Path("/q"))
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=judge) == []
    assert "Bash(make *)" in SA._pending(qdir) and SA._pending_doc(qdir)["refused"] == {}


# --- 2. the lock has a deadline -----------------------------------------------------------------------------------

def test_a_held_lock_skips_the_apply_after_its_deadline_and_the_launch_goes_on(rig, qdir, monkeypatch):
    monkeypatch.setattr(SA, "LOCK_WAIT_S", 0.3)
    _carried_one(rig, qdir, "WebSearch")
    fd = os.open(qdir / SA.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)              # another launch, hung inside its apply
    out: list = []
    t = threading.Thread(target=lambda: out.append(_launch(rig, qdir)), daemon=True)
    try:
        t.start()
        t.join(10)
        assert not t.is_alive(), "the launch waited on the lock with no deadline"
    finally:
        os.close(fd)
        t.join(10)
    (settings, said) = out[0]
    assert any("busy" in m for m in said), said
    assert settings["permissions"]["allow"] == ["WebSearch"]      # the stored rule still carries this launch
    with pytest.raises(SA.AllowRefused, match="busy"):
        fd = os.open(qdir / SA.LOCK_NAME, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            SA.file_rows(qdir, "S1", ["WebSearch"], store=rig.qs)
        finally:
            os.close(fd)


# --- 3. _save -----------------------------------------------------------------------------------------------------

def test_save_refuses_a_document_its_reader_would_refuse_and_keeps_the_previous_file(qdir, monkeypatch):
    path = qdir / SA.FILE_NAME
    SA._save(path, SA._empty())
    before = path.read_bytes()
    monkeypatch.setattr(SA, "FILE_CAP", 400)
    big = {**SA._empty(), "revoked": [{"rule": "WebSearch", "session": "x" * 500, "revoked_at": "t"}]}
    with pytest.raises(SA.AllowRefused, match="over"):
        SA._save(path, big)
    assert path.read_bytes() == before and SA.load(path) == SA._empty()
    assert [p.name for p in qdir.iterdir() if p.name.endswith(".tmp")] == []


def test_save_writes_all_of_a_short_write_and_syncs_the_directory(qdir, monkeypatch):
    path = qdir / SA.FILE_NAME
    doc = {**SA._empty(), "revoked": [{"rule": "WebSearch", "session": "s" * 300, "revoked_at": "t"}]}
    real_write, real_fsync = os.write, os.fsync
    synced: list = []
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data)[:7]))
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)) or real_fsync(fd))
    SA._save(path, doc)
    monkeypatch.setattr(os, "write", real_write)
    assert SA.load(path) == doc
    assert synced == [False, True]               # the file, then the directory its rename lives in


def test_a_failed_save_leaves_no_temp_and_the_previous_file(qdir, monkeypatch):
    path = qdir / SA.FILE_NAME
    SA._save(path, SA._empty())
    before = path.read_bytes()

    def boom(fd):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        SA._save(path, {**SA._empty(), "revoked": [{"rule": "WebSearch", "session": "s", "revoked_at": "t"}]})
    assert path.read_bytes() == before
    assert [p.name for p in qdir.iterdir() if p.name.endswith(".tmp")] == []


# --- 4. merge_into_frozen ------------------------------------------------------------------------------------------

def test_merge_touches_only_permissions_allow():
    fz = {"permissions": {"allow": ["Bash(make)"], "deny": ["Read(./.env)"], "defaultMode": "acceptEdits"}}
    out = SA.merge_into_frozen(fz, ["WebSearch"])
    assert out["permissions"] == {"allow": ["Bash(make)", "WebSearch"], "deny": ["Read(./.env)"],
                                  "defaultMode": "acceptEdits"}
    assert fz["permissions"]["allow"] == ["Bash(make)"]          # the frozen layer itself is not changed


# --- 7. the consent directory cannot be named -----------------------------------------------------------------------

def test_run_session_goes_on_without_the_rules_when_the_consent_dir_cannot_be_named(monkeypatch):
    from levain.consent import queue as cq

    def refuse():
        raise RuntimeError("the consent directory: refusing the real home in a test run")
    monkeypatch.setattr(cq, "default_queue_dir", refuse)
    seen: list = []
    said: list = []
    monkeypatch.setattr(sup.Supervisor, "run", lambda self: seen.append(self.s.allow_dir) or 0)
    monkeypatch.setattr(sup.Supervisor, "say", lambda self, m: said.append(m))
    assert sup.run_session(SimpleNamespace()) == 0
    assert seen == [None] and any("slot allow" in m and "RuntimeError" in m for m in said)


def test_the_verb_says_a_consent_dir_it_cannot_name_without_a_traceback(monkeypatch):
    import io

    from levain.consent import queue as cq

    def refuse():
        raise RuntimeError("no home")
    monkeypatch.setattr(cq, "default_queue_dir", refuse)
    out = io.StringIO()
    assert SA.run(SimpleNamespace(queue_dir=None, slot_allow_verb="list"), out=out) == 1
    assert "consent directory" in out.getvalue()


# --- 8. a fresh consent directory --------------------------------------------------------------------------------

def test_first_use_creates_the_consent_dir_private(tmp_path):
    fresh = tmp_path / "home" / "consent"
    assert SA.file_rows(fresh, "S1", ["WebSearch"]) == ["WebSearch"]
    st = os.stat(fresh)
    assert stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.getuid()


def test_a_consent_dir_others_can_reach_is_refused_before_the_lock(tmp_path):
    loose = tmp_path / "consent"
    loose.mkdir(mode=0o700)
    os.chmod(loose, 0o755)
    with pytest.raises(SA.AllowRefused, match="0700"):
        SA.file_rows(loose, "S1", ["WebSearch"])
    assert not (loose / SA.LOCK_NAME).exists()


def test_rows_survive_json_round_trip(rig, qdir):  # sanity: the pending file stays readable after the new _save
    rig.file("WebSearch")
    assert json.loads((qdir / SA.PENDING_NAME).read_text())["format"] == SA.PENDING_FORMAT
    assert len(_rows(rig, "WebSearch")) == 1
