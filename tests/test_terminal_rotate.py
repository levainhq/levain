"""``levain band terminal-key rotate`` (levain#191 slice 3, levain/consent/terminal_rotate.py).

ZERO TOUCH: every key directory and authority root is under tmp_path; the helper is the stub from
test_se_integration (a software P-256 key signing as the Secure Enclave helper would, flag 0x01), the old key's
signature is made the same way, the creator, signers and the CTK deleter are injected. Nothing here reads
~/.levain, reaches the real key or shows a sheet.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import authority as auth
from levain.cockpit import custody
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import CreatedKey
from levain.cockpit.firejournal import CockpitDB
from levain.consent import terminal_rotate as tr
from levain.consent import terminal_sign as ts
from levain.trust_anchor import DEFAULT_SSH_KEYGEN
from tests.cockpit_authority_rig import link
from tests.test_se_integration import STUB_SOURCE, _soft_key

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
STAMP = "20261121T120000Z"
APPLE = custody.APPLE_PROVIDER
CTK = "A" * 40


@pytest.fixture(scope="module")
def stub(tmp_path_factory) -> Path:
    pytest.importorskip("cryptography")
    p = tmp_path_factory.mktemp("rotstub") / "stub-helper"
    p.write_text(f"#!{sys.executable}\n" + STUB_SOURCE)
    p.chmod(0o755)
    return p


class Rig:
    """A key directory with an OLD (Apple-provider) key enrolled as the authority's genesis, an authority root, and
    the injected doors, each recording what it was asked."""

    def __init__(self, tmp: Path, stub: Path, monkeypatch) -> None:
        monkeypatch.setattr("levain.cockpit.install.marker_dir", lambda: tmp / "markers")
        self.stub = stub
        self.d = tmp / "keys"
        self.d.mkdir(mode=0o700)
        self.d.chmod(0o700)
        self.root = tmp / "cockpit"
        self.root.mkdir()
        CockpitDB(self.root).set_install_class("reported")
        self.old = _soft_key(self.d, label="levain-terminal", provider=APPLE, ctk_hash=CTK, signer=None)
        # the stub helper stands in for the Apple provider when it signs the enrolment as the old key, and it wants a meta file;
        # a real Apple key has none, so the rotate move must cope with both (this one travels to retired-<stamp>/)
        Path(str(self.old) + ".meta.json").write_text(json.dumps({"format": "levain-sign-key/1", "presence": True}))
        self.old_pub = self._pub(self.old)
        self.old_fp = ckeys.fingerprint_of(self.old_pub)
        self.signs: list[str] = []          # the key file name of every signature asked for
        self.deleted: list[str] = []
        self.made = 0
        self.fail: dict[str, object] = {}   # step name -> exception to raise once
        gen = self._signer(self.old)
        link(CockpitDB(self.root), {"op": "enrol", "fingerprint": self.old_fp, "public_key": self.old_pub,
                                    "class": "laptop-presence", "custody": "touch-id"},
             SimpleNamespace(fingerprint=self.old_fp, sign_link=gen[1]), now=NOW)
        self.signs.clear()

    @staticmethod
    def _pub(key: Path) -> str:
        return " ".join(Path(str(key) + ".pub").read_text().split()[:2])

    def _signer(self, key: Path):
        pub = self._pub(key)
        fp = ckeys.fingerprint_of(pub)
        inner = ckeys._levain_sign_signer(key, pub, fp, auth.NAMESPACE, 20.0, DEFAULT_SSH_KEYGEN, self.stub)

        def sign(message: bytes) -> str:
            self._maybe_fail("sign:" + key.name)       # a cancelled Touch ID is not a signature
            self.signs.append(key.name)
            return inner(message)
        return fp, sign

    def _maybe_fail(self, step: str) -> None:
        exc = self.fail.pop(step, None)
        if exc is not None:
            raise exc  # type: ignore[misc]

    def creator(self, d: Path, label: str) -> CreatedKey:
        self._maybe_fail("create")
        self.made += 1
        key = _soft_key(d, label=label, signer=custody.SIGNER_FORMAT)
        return CreatedKey(key, Path(str(key) + ".pub"), self._pub(key), "touch-id", label, None)

    def delete(self, digest: str, sc_auth) -> str | None:
        self._maybe_fail("ctk")
        self.deleted.append(digest)
        return None

    def run(self, **over) -> tr.RotateReport:
        kw = dict(key_dir=self.d, data_root=self.root, creator=self.creator,
                  signer_factory=lambda p: self._signer(p), in_agent_session=lambda: False,
                  delete_identity=self.delete, clock=lambda: NOW,
                  declare=partial(custody.declared_custody, platform="linux"))
        kw.update(over)
        return tr.rotate(**kw)

    def rows(self) -> dict[str, object]:
        rows, _ = ckeys.keys(self.root, now=NOW)
        return {r.fingerprint: r for r in rows}

    def journal(self) -> dict:
        return json.loads((self.d / tr.JOURNAL).read_text())


@pytest.fixture
def rig(tmp_path, stub, monkeypatch) -> Rig:
    return Rig(tmp_path, stub, monkeypatch)


def _assert_rotated(rig: Rig, rep: tr.RotateReport) -> None:
    assert rep.state == "done", rep.text
    rows = rig.rows()
    new_fp = rig.journal()["new_fp"]
    assert rows[new_fp].revoked_seq is None and rows[new_fp].key_class == "laptop-presence"
    assert rows[new_fp].custody == "touch-id"
    assert rows[rig.old_fp].revoked_seq is not None
    assert rows[new_fp].enrolled_seq == rig.journal()["steps"]["enrolled"]
    assert rows[rig.old_fp].revoked_seq == rig.journal()["steps"]["revoked"]
    assert rows[new_fp].enrolled_seq < rows[rig.old_fp].revoked_seq
    # one enrol (signed by the old key) and one revoke (signed by the new key), no more
    assert rig.signs == ["levain-terminal", "levain-terminal-next"]
    # the files: the terminal label is the new key, the old three are retired, the next label is gone
    d = rig.d
    assert ckeys.fingerprint_of(rig._pub(d / "levain-terminal")) == new_fp
    retired = d / f"retired-{STAMP}"
    # (the rig's old key has a meta file because its stub signs through the helper's door; a real Apple key has none)
    assert sorted(p.name for p in retired.iterdir()) == ["levain-terminal", "levain-terminal.levain-key.json",
                                                         "levain-terminal.meta.json", "levain-terminal.pub"]
    assert ckeys.fingerprint_of(rig._pub(retired / "levain-terminal")) == rig.old_fp
    assert not [p for p in d.iterdir() if p.name.startswith("levain-terminal-next") and not p.name.endswith(".calls")]
    # the new key's meta file (the helper reads presence from it) and its counter travelled with its blob: it can sign
    assert json.loads((d / "levain-terminal.meta.json").read_text())["presence"] is True
    assert rig.deleted == [CTK]
    assert stat.S_IMODE(os.lstat(d / tr.JOURNAL).st_mode) == 0o600
    assert rig.journal()["complete"] is True
    assert ts.find_key(d) is not None
    assert custody.read_sidecar(d / "levain-terminal").provider == custody.LEVAIN_SIGN


def test_the_happy_path_rotates_every_step(rig):
    rep = rig.run()
    _assert_rotated(rig, rep)
    assert rig.made == 1
    for word in ("one Touch ID", "Levain's helper", str(rig.d / "sign-log.jsonl"), "needs_phill_signers"):
        assert word in rep.text
    assert rep.code == 0


def test_a_terminal_answer_signed_by_the_new_key_verifies_after_rotation(rig):
    rig.run()
    key = ts.find_key(rig.d)
    fp, sign = ckeys.ssh_signer(key.path, namespace=ts.NAMESPACE, helper=rig.stub)
    assert fp == rig.journal()["new_fp"]
    msg = b"levain-terminal-answer/1\nanswer: yes\n"
    res = ts.verify(msg, sign(msg), ts.signers_line(key.public_key), "phill", ctk=lambda pk: (True, "stub"))
    assert res.ok and res.fingerprint == fp and res.user_present is True


def test_already_rotated_is_a_no_op(rig):
    rig.run()
    before = (rig.rows(), sorted(p.name for p in rig.d.iterdir()), rig.journal())
    rep = rig.run()
    assert rep.state == "noop" and rep.code == 0 and rep.text == tr.NOOP_TEXT
    assert "nothing to rotate" in rep.text and "Levain's helper" in rep.text
    assert before == (rig.rows(), sorted(p.name for p in rig.d.iterdir()), rig.journal())
    assert rig.signs == ["levain-terminal", "levain-terminal-next"] and rig.made == 1


def test_a_key_that_already_signs_through_the_helper_with_no_journal_is_a_no_op(rig):
    # a key made by `terminal-key create` on 1.0: levain-sign sidecar, nothing journalled
    for s in ("", ".pub", custody.SIDECAR_SUFFIX):
        (rig.d / ("levain-terminal" + s)).unlink()
    _soft_key(rig.d, label="levain-terminal", signer=custody.SIGNER_FORMAT)
    rep = rig.run()
    assert rep.state == "noop" and rep.text == tr.NOOP_TEXT
    assert rig.made == 0 and not (rig.d / tr.JOURNAL).exists()


def test_no_terminal_key_refuses_and_points_at_create(rig):
    for s in ("", ".pub", custody.SIDECAR_SUFFIX):
        (rig.d / ("levain-terminal" + s)).unlink()
    rep = rig.run()
    assert rep.state == "refused" and rep.code == 2 and "terminal-key create" in rep.text
    assert rig.made == 0 and not (rig.d / tr.JOURNAL).exists()


def test_an_agent_session_is_refused_in_create_s_words_and_nothing_is_touched(rig):
    rep = rig.run(in_agent_session=lambda: True)
    assert rep.state == "refused" and rep.code == 2 and ts.session_refusal() in rep.text
    assert rig.made == 0 and not (rig.d / tr.JOURNAL).exists() and rig.signs == []


def test_a_detector_that_raises_is_a_refusal(rig):
    def boom():
        raise RuntimeError("no ps")
    rep = rig.run(in_agent_session=boom)
    assert rep.state == "refused" and "cannot be told" in rep.text and rig.made == 0


def test_the_real_key_directory_gate_applies(rig, monkeypatch):
    def refuse(path, what="the key directory"):
        raise ckeys.RealKeyUnmarked("stub: the real key directory")
    monkeypatch.setattr(ckeys, "refuse_real_key_dir_unless_live", refuse)
    rep = rig.run()
    assert rep.state == "refused" and "real key directory" in rep.text and rig.made == 0


def test_an_old_key_the_authority_does_not_hold_refuses_before_a_key_is_made(rig, tmp_path):
    other = tmp_path / "cockpit2"
    other.mkdir()
    CockpitDB(other).set_install_class("reported")
    from tests.cockpit_authority_rig import RealKey, enrol
    enrol(CockpitDB(other), RealKey(tmp_path, "someone"), "laptop-presence")
    rep = rig.run(data_root=other)
    assert rep.state == "refused" and "not enrolled" in rep.text and rig.made == 0
    assert not (rig.d / tr.JOURNAL).exists()


def test_the_journal_is_written_before_any_step_and_a_new_run_with_a_bad_one_refuses(rig):
    (rig.d / tr.JOURNAL).write_text("not json")
    rep = rig.run()
    assert rep.state == "refused" and "journal" in rep.text and rig.made == 0


# ---------------------------------------------------------------------------------------------------
# resume after an interruption at each step

INTERRUPTS = [
    # (where, what is done when it stops: made, enrolled, revoked, moved)
    ("create", (False, False, False, False)),
    ("sign:levain-terminal", (True, False, False, False)),            # Touch ID cancelled on the old key
    ("sign:levain-terminal-next", (True, True, False, False)),        # cancelled on the new key
]


@pytest.mark.parametrize("where, done", INTERRUPTS)
@pytest.mark.parametrize("exc", [ckeys.KeyError_("Touch ID was cancelled"), KeyboardInterrupt()],
                         ids=["cancelled", "ctrl-c"])
def test_resume_after_a_stop_at_each_signing_step(rig, where, done, exc):
    rig.fail[where] = exc
    first = rig.run()
    assert first.state == "stopped" and first.code == 1 and "rotate" in first.text and "again" in first.text
    made, enrolled, revoked, moved = done
    rows = rig.rows()
    steps = rig.journal()["steps"]
    assert bool(steps.get("made")) is made and ("enrolled" in steps) is enrolled
    assert ("revoked" in steps) is revoked and not steps.get("moved")
    if enrolled:      # the journal and the authority agree
        assert rows[rig.journal()["new_fp"]].enrolled_seq == steps["enrolled"]
    assert rows[rig.old_fp].revoked_seq is None
    # nothing was moved: the old key is still the terminal key
    assert ckeys.fingerprint_of(rig._pub(rig.d / "levain-terminal")) == rig.old_fp
    second = rig.run()
    _assert_rotated(rig, second)
    assert rig.made == 1          # one key, whatever happened first
    assert "resuming" in second.text


def test_a_cancelled_new_key_sign_never_re_enrols_on_resume(rig):
    rig.fail["sign:levain-terminal-next"] = ckeys.KeyError_("Touch ID was cancelled")
    rig.run()
    enrolled_before = rig.journal()["steps"]["enrolled"]
    rig.run()
    # the old key signed exactly once across both runs (the enrol), the new key once (the revoke; one was cancelled)
    assert rig.signs == ["levain-terminal", "levain-terminal-next"]
    assert rig.rows()[rig.journal()["new_fp"]].enrolled_seq == enrolled_before


def test_the_authority_wins_over_a_journal_that_missed_the_enrol(rig):
    """Killed after the enrol link landed and before the journal said so: the next run sees the authority holds the
    key, records its seq, and signs nothing with the old key."""
    real_write = tr._write_journal
    state = {"drop": True}

    def write(d, journal):
        if state["drop"] and "enrolled" in journal["steps"]:
            state["drop"] = False
            raise KeyboardInterrupt()           # the write that would have recorded the enrol never happens
        real_write(d, journal)
    tr._write_journal = write
    try:
        first = rig.run()
    finally:
        tr._write_journal = real_write
    assert first.state == "stopped"
    assert "enrolled" not in rig.journal()["steps"]
    new_fp = rig.journal()["new_fp"]
    assert rig.rows()[new_fp].enrolled_seq is not None
    second = rig.run()
    _assert_rotated(rig, second)
    assert rig.signs == ["levain-terminal", "levain-terminal-next"]     # the old key signed once, in the first run


def test_the_authority_wins_over_a_journal_that_missed_the_revoke(rig):
    real_write = tr._write_journal
    state = {"drop": True}

    def write(d, journal):
        if state["drop"] and "revoked" in journal["steps"]:
            state["drop"] = False
            raise KeyboardInterrupt()
        real_write(d, journal)
    tr._write_journal = write
    try:
        first = rig.run()
    finally:
        tr._write_journal = real_write
    assert first.state == "stopped" and "revoked" not in rig.journal()["steps"]
    assert rig.rows()[rig.old_fp].revoked_seq is not None
    second = rig.run()
    _assert_rotated(rig, second)
    assert rig.signs == ["levain-terminal", "levain-terminal-next"]     # no second revoke signature


@pytest.mark.parametrize("nth", [1, 2, 3, 4, 5, 6])
def test_resume_after_a_rename_fails_at_each_of_the_six(rig, monkeypatch, nth):
    real = tr._rename
    count = {"n": 0}

    def flaky(src, dst):
        count["n"] += 1
        if count["n"] == nth:
            raise OSError("disk went away")
        real(src, dst)
    monkeypatch.setattr(tr, "_rename", flaky)
    first = rig.run()
    assert first.state == "stopped" and "disk went away" in first.text
    steps = rig.journal()["steps"]
    assert "enrolled" in steps and "revoked" in steps and not steps.get("moved")
    monkeypatch.setattr(tr, "_rename", real)
    second = rig.run()
    _assert_rotated(rig, second)


def test_resume_after_the_ctk_delete_fails_is_reported_not_fatal(rig):
    rig.fail["ctk"] = OSError("sc_auth gone")
    # an exception from the deleter is a stop; a returned reason is not fatal: check both
    first = rig.run()
    assert first.state == "stopped" and not rig.journal()["steps"].get("ctk_deleted")
    second = rig.run()
    _assert_rotated(rig, second)


def test_a_failing_ctk_delete_is_reported_and_the_rotation_still_completes(rig):
    rep = rig.run(delete_identity=lambda digest, sc: "sc_auth could not delete CTK identity (exit 1)")
    assert rep.state == "done" and "could not delete the old keychain identity" in rep.text
    assert "sc_auth delete-ctk-identity" in rep.text
    assert rig.journal()["steps"]["ctk_deleted"] is False and rig.journal()["complete"] is True
    assert rig.rows()[rig.old_fp].revoked_seq is not None
    # and a re-run is the no-op
    assert rig.run().state == "noop"


def test_a_key_made_by_a_run_killed_before_its_journal_entry_is_adopted(rig):
    _soft_key(rig.d, label=tr.NEXT_LABEL, signer=custody.SIGNER_FORMAT)
    rep = rig.run()
    _assert_rotated(rig, rep)
    assert rig.made == 0


def test_half_a_next_key_is_a_stop_not_an_overwrite(rig):
    (rig.d / (tr.NEXT_LABEL + ".pub")).write_text("junk\n")
    rep = rig.run()
    assert rep.state == "stopped" and "half there" in rep.text and rig.made == 0
    assert (rig.d / (tr.NEXT_LABEL + ".pub")).read_text() == "junk\n"


def test_a_rename_never_overwrites(rig):
    (rig.d / f"retired-{STAMP}").mkdir()
    (rig.d / f"retired-{STAMP}" / "levain-terminal.pub").write_text("an unrelated file\n")
    # a retired file that is already there while the same name is still at the terminal label is a conflict, found
    # before any file moves: nothing is overwritten and the old key stays whole
    rep = rig.run()
    assert rep.state == "stopped" and "already exists" in rep.text
    assert ckeys.fingerprint_of(rig._pub(rig.d / "levain-terminal")) == rig.old_fp
    assert (rig.d / "levain-terminal").exists() and (rig.d / ("levain-terminal" + custody.SIDECAR_SUFFIX)).exists()
    assert (rig.d / f"retired-{STAMP}" / "levain-terminal.pub").read_text() == "an unrelated file\n"


def test_the_cli_verb_is_registered_and_a_test_run_never_reaches_the_real_key_directory(tmp_path, capsys):
    import argparse

    from levain import band_view
    top = argparse.ArgumentParser()
    band_view.add_parser(top.add_subparsers(dest="cmd"))
    args = top.parse_args(["band", "terminal-key", "rotate", "--data-root", str(tmp_path)])
    assert args.tk_verb == "rotate" and args.data_root == tmp_path and args.func is band_view.run_band_terminal_key
    assert args.func(args) == 2                       # refused (an agent session or the real-key gate), nothing touched
    assert "refused" in capsys.readouterr().err


def test_the_new_keys_counter_and_meta_travel_and_the_old_keys_counter_is_retired(rig):
    Path(str(rig.old) + ".counter").write_text("41\n")                  # a helper-era old key's counter, retired with it
    inner = rig.creator

    def creator(d, label):
        made = inner(d, label)
        Path(str(made.path) + ".counter").write_text("1\n")             # the first sign made one
        return made
    rep = rig.run(creator=creator)
    assert rep.state == "done", rep.text
    d, retired = rig.d, rig.d / f"retired-{STAMP}"
    assert (d / "levain-terminal.counter").read_text() == "1\n" and (d / "levain-terminal.meta.json").is_file()
    assert (retired / "levain-terminal.counter").read_text() == "41\n"
    assert not (d / "levain-terminal-next.counter").exists() and not (d / "levain-terminal-next.meta.json").exists()


def test_a_whole_next_key_without_its_meta_file_is_half_a_key(rig):
    key = _soft_key(rig.d, label=tr.NEXT_LABEL, signer=custody.SIGNER_FORMAT)
    Path(str(key) + ".meta.json").unlink()
    rep = rig.run()
    assert rep.state == "stopped" and "half there" in rep.text


# levain#191 review round 2 (serot-fix2, F8): a failure before the first journal write is a refusal, nothing started


def test_a_data_root_that_raises_is_a_refusal_not_a_stop(rig, monkeypatch):
    def boom(path, data_root):
        raise RuntimeError("the install config is unreadable")
    monkeypatch.setattr(tr, "resolve_data_root", boom)
    rep = rig.run()
    assert rep.state == "refused" and rep.code == tr.EXIT_REFUSED and "nothing was started" in rep.text
    assert "steps above are done" not in rep.text and rig.made == 0 and not (rig.d / tr.JOURNAL).exists()


def test_a_fresh_start_check_that_raises_is_a_refusal_not_a_stop(rig, monkeypatch):
    def boom(path):
        raise OSError("sidecar unreadable")
    monkeypatch.setattr(tr, "read_sidecar", boom)
    rep = rig.run()
    assert rep.state == "refused" and rep.code == tr.EXIT_REFUSED and "nothing was started" in rep.text
    assert "steps above are done" not in rep.text and rig.made == 0 and not (rig.d / tr.JOURNAL).exists()


def test_the_ctk_step_docstring_says_an_exception_stops_the_run():
    doc = tr._step_ctk.__doc__ or ""
    assert "never fatal" not in doc and "stops the run" in doc and "resumable" in doc
