"""S14 A3 (P9): END's verifier report fallback. END over a verifier session carries ``verifier_report_digest`` (the
file's recomputed digest) or ``verifier_report_refused: no-report:<state END started from>`` on its CAS into RETAINED or
HELD, and nothing when the record already holds a report field. Same world as test_slot_s2c_end (real files, the real
Control.cas with A0's report-field rules); only the record's claims are a verifier's."""
from __future__ import annotations

import hashlib
import os
from dataclasses import replace

import pytest

from levain.slot import end as end_mod
from levain.slot import records as R
from levain.slot.ids import SlotId
from levain.slot.end import (HELD, RETAINED, REVOKED, EndFacts, end_session, reconcile_session, report_fields,
                             report_file_digest)
from tests.test_slot_s2c_end import NOW, Control, World, mine
from tests.test_slot_s2g_end import Recorder

A = "sha256:" + "a" * 64
BODY = b'{"format":"levain-verifier-envelope/1"}\n'
BODY_DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()


def vctl(fields=None, kind="verifier") -> Control:
    """A Control whose record is a verifier's (A0 accepts a report field only there), optionally already holding
    ``fields``."""
    ctl = Control()
    rec = ctl.store.get(ctl.session)
    claims = {"operator_uid": 501, "supervisor_uid": 502, "slot": R.slot_obj(SlotId("none", 0)), "class": "none",
              "kind": kind, "vendor": "claude", "auth": "none", "lease_seq": 1, "slot_rev": 1}
    with ctl.store.lock(ctl.session):
        ctl.store.put(replace(rec, claims=claims, fields=dict(fields or {})))
    return ctl


def vworld(tmp_path, ctl, kind="verifier") -> World:
    w = World(tmp_path, uchg=False)
    w.facts = replace(w.facts, kind=kind)
    return w


def seams(w: World, ctl: Control, **kw):
    kw.setdefault("report_set", lambda: any(k in ctl.fields for k in (end_mod.REPORT_DIGEST, end_mod.REPORT_REFUSED)))
    return w.seams(ctl, **kw)


def write_report(w: World, data: bytes = BODY, mode: int = 0o640) -> str:
    d = w.facts.report_dir
    d.mkdir(parents=True)
    for p in (d.parent.parent, d.parent, d):
        os.chmod(p, 0o750)
    f = d / end_mod.REPORT_FILE
    f.write_bytes(data)
    os.chmod(f, mode)
    return str(f)


def report_of(ctl: Control) -> dict:
    return {k: v for k, v in ctl.fields.items() if k.startswith("verifier_report")}


def test_nothing_written_gives_no_report_of_the_state_end_started_from(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    assert end_session(w.facts, seams(w, ctl), record_state=REVOKED, report_from="ACTIVE") == RETAINED
    assert report_of(ctl) == {"verifier_report_refused": "no-report:ACTIVE"}


def test_the_default_start_state_is_the_record_state(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    end_session(w.facts, seams(w, ctl), record_state=REVOKED)
    assert report_of(ctl) == {"verifier_report_refused": "no-report:REVOKED"}


def test_crash_after_the_file_was_written_before_the_cas_gives_the_digest(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    write_report(w)
    end_session(w.facts, seams(w, ctl), record_state=REVOKED, report_from="ACTIVE")
    assert report_of(ctl) == {"verifier_report_digest": BODY_DIGEST}


def test_a_preset_field_gives_no_second_field_and_end_is_not_wedged(tmp_path):
    for preset in ({"verifier_report_digest": A}, {"verifier_report_refused": "driver-timeout"}):
        sub = tmp_path / next(iter(preset))
        sub.mkdir()
        ctl = vctl(preset)
        w = vworld(sub, ctl)
        write_report(w)                         # a different digest than the one the record holds
        sm = seams(w, ctl)
        assert end_session(w.facts, sm, record_state=REVOKED) == RETAINED
        assert report_of(ctl) == preset
        assert all(end_mod.REPORT_DIGEST not in f and end_mod.REPORT_REFUSED not in f for _, f in ctl.sent)


def test_a_seam_that_is_never_read_sends_none(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED      # the default report_set: set
    assert report_of(ctl) == {}


@pytest.mark.parametrize("kind", ["dispatch", "login", None])
def test_another_kind_carries_nothing(tmp_path, kind):
    ctl = vctl(kind=kind or "dispatch")
    w = vworld(tmp_path, ctl, kind=kind)
    write_report(w)
    end_session(w.facts, seams(w, ctl), record_state=REVOKED)
    assert report_of(ctl) == {} and all("verifier_report_refused" not in f for _, f in ctl.sent)


def test_the_field_rides_only_the_retained_or_held_cas(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    end_session(w.facts, seams(w, ctl), record_state=REVOKED)
    carrying = [t for t, f in ctl.sent if "verifier_report_refused" in f]
    assert carrying == [RETAINED]


def test_a_held_landing_carries_it_and_the_retry_converges(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    rec = Recorder(no_process=lambda uid: False)
    assert end_session(w.facts, seams(w, ctl, **rec.kw()), record_state=REVOKED, report_from="ACTIVE") == HELD
    assert ctl.state == HELD and report_of(ctl) == {"verifier_report_refused": "no-report:ACTIVE"}
    # the file shows up later and a second END starts from HELD: the record's field stands, none is sent
    write_report(w)
    end_session(w.facts, seams(w, ctl), record_state=HELD, failed_step=ctl.failed_step,
                held_attempts=ctl.fields.get("held_attempts"))
    assert ctl.state == RETAINED and report_of(ctl) == {"verifier_report_refused": "no-report:ACTIVE"}


def test_reconcile_session_carries_it_too(tmp_path):
    from levain.slot.root_intent import read_root_intent
    from tests.test_slot_s2fx2_end import _crash_at
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    _crash_at(w, ctl, "retain:after:init")
    assert report_of(ctl) == {}
    reconcile_session(w.facts, seams(w, ctl), report_from="DEAD")
    assert ctl.state == RETAINED and report_of(ctl) == {"verifier_report_refused": "no-report:DEAD"}


# --- the file: root's own regular file, opened without following a link ------------------------------------------


def test_a_symlinked_report_is_no_report(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    write_report(w)
    real = w.facts.report_dir / end_mod.REPORT_FILE
    other = tmp_path / "elsewhere"
    other.write_bytes(b"x")
    real.unlink()
    real.symlink_to(other)
    assert report_file_digest(w.facts, seams(w, ctl)) is None


def test_a_symlinked_directory_component_is_no_report(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    real = tmp_path / "real-export"
    (real / "dispatch" / w.facts.session).mkdir(parents=True)
    (real / "dispatch" / w.facts.session / end_mod.REPORT_FILE).write_bytes(BODY)
    (w.facts.store / "export").symlink_to(real)
    assert report_file_digest(w.facts, seams(w, ctl)) is None


@pytest.mark.parametrize("mode", [0o660, 0o646, 0o4640])
def test_a_report_others_can_write_or_with_a_setid_bit_is_no_report(tmp_path, mode):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    write_report(w, mode=mode)
    assert report_file_digest(w.facts, seams(w, ctl)) is None


def test_a_hard_linked_report_is_no_report(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    f = write_report(w)
    os.link(f, tmp_path / "second-name")
    assert report_file_digest(w.facts, seams(w, ctl)) is None


def test_a_report_not_owned_by_root_is_no_report(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    write_report(w)
    assert report_file_digest(w.facts, seams(w, ctl, owner_ok=lambda st: False)) is None


def test_a_directory_in_place_of_the_file_and_a_missing_tree_are_no_report(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    assert report_file_digest(w.facts, seams(w, ctl)) is None
    (w.facts.report_dir / end_mod.REPORT_FILE).mkdir(parents=True)
    assert report_file_digest(w.facts, seams(w, ctl)) is None


def test_a_state_name_that_is_not_a_token_is_named_unknown(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    assert report_fields(w.facts, seams(w, ctl), "x/../y") == {"verifier_report_refused": "no-report:unknown"}


# --- END over a verifier tree that holds step<k>.* (A2a's known gap) --------------------------------------------


def test_end_retains_a_verifier_tree_holding_the_step_captures(tmp_path):
    ctl = vctl()
    w = vworld(tmp_path, ctl)
    run = w.S / "run"
    run.mkdir()
    os.chmod(run, 0o750)
    files = {"step1.out": b"hi\n", "step1.err": b"", "step1.exit": b'{"exit":0}', "step2.out": b"x" * 5000,
             "driver.err": b"d"}
    for n, b in files.items():
        (run / n).write_bytes(b)
        os.chmod(run / n, 0o640)
    assert end_session(w.facts, seams(w, ctl), record_state=REVOKED) == RETAINED
    import base64
    import json
    man = {base64.b64decode(r["path_b64"]).decode(): r
           for r in map(json.loads, (w.facts.retained / "manifest.jsonl").read_text().splitlines())}
    for n, b in files.items():
        assert man[f"run/{n}"]["digest"] == "sha256:" + hashlib.sha256(b).hexdigest()
    assert report_of(ctl) == {"verifier_report_refused": "no-report:REVOKED"}
