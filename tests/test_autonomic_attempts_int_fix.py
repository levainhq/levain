"""O-p2-1 integration fix round 1 (levain project_memory ``1027-9/op21int/FIX_int_r1.md``, from the L3 in
``1027-9/op21int/L3_int.txt``): each test reproduces a reviewer's probe on the B1 + B2 integration and is RED before
its fix.

I1 the seal's top-level actor, posture and grant are tied to the decision of a held link (codex HIGH 1) · I2 an
unknown or non-executing posture and an unknown actor are refused (codex HIGH 2) · I3 attempt 1 of a held link carries
its decision (codex MED 3) · I4 an empty run id is refused (codex MED 4) · I6 a hold-less retry re-uses its
predecessor's grant, not only its cover (deepseek MED 2) · the not_started ``why`` is checked up front (complement
LOW 1).

/3 is unwired (R15-5). Effect rows are written through :func:`journal.intent_in` (the real seal builder) wherever the
probe has a caller path; a probe of the store's own refusal inserts a hand-edited seal."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic.gate import receipt_by
from levain.autonomic.journal import Receipt, RunJournal, hold_id_for

GUARD = [{"rationale": "spoofed sender", "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
          "kill_drill": {"dmarc": "fail"}}]


def grant(gid: str, inc: str = "1:inc") -> dict:
    return {"born": 1, "id": gid, "incarnation": inc,
            "terms_text": json.dumps({"kind": "graduation", "id": gid, "guard": GUARD})}


GRANT = grant("g1")
COVER = "1:inc@c1"
H = hold_id_for("r", "link-0")
HELD = "S1: an attempt on a held link"
TIED = "int-I1: a held attempt carries the actor, posture and grant of its decision"
KNOWN = "int-I2: an effect is sealed by a known actor at an executing posture"
AUTHORITY = "R3-1/R3-2: a retry carries"


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


def rec(rid, subject, ts, *, grant_guard=None) -> dict:
    return {"id": rid, "kind": "gate", "subject": subject, "fired": ts == "fired",
            "action_face": {"terminal_state": ts, "guard": None, "grant_guard": grant_guard}}


def receipt(rid, subject, ts, **kw) -> Receipt:
    return Receipt(rid, rec(rid, subject, ts, **kw))


def run(c, links=1, run_id="r"):
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b', 0, ?)",
              (run_id, canon({"context_id": "c", "confirm_class": False, "guard": [], "links": links})))
    c.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run_id,))


def open_hold(c):
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
              "VALUES (?, 'r', 'link-0', 'b', 'd', 1)", (H,))


def silence_approve(c, posture="COOLING_OFF"):
    out = J.decide_in(c, H, approve=True, digest="d", by=J.UNSIGNED_DECIDER, posture=posture)
    assert out.ok, out


def human_approve(c, posture="CONFIRM"):
    out = J.decide_in(c, H, approve=True, digest="d", by="human", posture=posture, signer="k", signature="sig",
                      challenge="ch", fence="f")
    assert out.ok, out


def intent(c, *, by, posture, grant=None, cover=None, digest="d"):
    return J.intent_in(c, "r", "link-0", digest=digest, by=by, posture=posture, grant=grant, cover=cover)


def binding_silence_intent(c):
    # the gate's own attempt for a binding's silence-approved, grant-covered effect (receipt_by maps the decider)
    return intent(c, by=receipt_by(J.UNSIGNED_DECIDER, "binding"), posture="COOLING_OFF", grant=GRANT, cover=COVER)


def not_started(c, n, *, grant_guard=GUARD, why="spawn"):
    subj = s.effect_subject("r", "link-0", n)
    return J.close_not_started_in(c, "r", "link-0", n, why=why,
                                  receipt=receipt(f"ns{n}", subj, "refused", grant_guard=grant_guard),
                                  run_receipt=lambda reason: receipt(f"run{n}", "run:r", "refused"))


def rows(c):
    return c.execute("SELECT attempt, state FROM effects ORDER BY attempt").fetchall()


def silence_attempt_one(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        assert binding_silence_intent(c) == 1
        not_started(c, 1)


# ---- I1: a held attempt's top-level actor, posture and grant come from its decision ----------------------------------
def test_i1_codex_probe_a_human_actor_at_a_lower_rung_on_a_silence_decision_is_refused(store):
    # codex HIGH 1 verbatim: the retry claims a human at ON_LOOP with no grant on a binding's silence approval
    silence_attempt_one(store)
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by="human", posture="ON_LOOP", grant=None, cover=None)
    with store.read() as c:
        assert rows(c) == [(1, "not_started")]


@pytest.mark.parametrize("by, posture, g, cover", [
    ("binding", "ON_LOOP", GRANT, COVER),                    # the rung lowered below the decision's
    ("on-loop", "COOLING_OFF", GRANT, COVER),                # the actor swapped inside one decision
    ("binding", "COOLING_OFF", grant("g2", "1:inc2"), "1:inc2@c1"),   # another grant, same decision
    ("binding", "COOLING_OFF", GRANT, "1:inc@c2"),            # another cover, same decision
])
def test_i1_a_retry_that_changes_the_decisions_actor_rung_or_grant_is_refused(store, by, posture, g, cover):
    silence_attempt_one(store)
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by=by, posture=posture, grant=g, cover=cover)


def test_i1_attempt_one_claiming_a_human_on_a_silence_decision_is_refused(store):
    # the same class at attempt 1: the forged actor would skip effects_guarded's grant guard just the same
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by="human", posture="ON_LOOP")


def test_i1_a_non_human_actor_on_a_human_decision_is_refused(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c)
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by="on-loop", posture="CONFIRM", grant=GRANT, cover=COVER)


def test_i1_a_human_decision_fires_at_or_above_its_rung_and_retries_there(store):
    # gate._decided_rung fires a signed approval at max(the proposal's rung, the decided rung)
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c, posture="CONFIRM")
        assert intent(c, by="human", posture="CONFIRM_ELEVATED") == 1
        not_started(c, 1, grant_guard=None)
    with store.write() as c:
        assert intent(c, by="human", posture="CONFIRM_ELEVATED") == 2
    with store.read() as c:
        assert rows(c) == [(1, "not_started"), (2, "intent")]


def test_i1_a_human_decision_below_its_rung_is_refused(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c, posture="CONFIRM")
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by="human", posture="COOLING_OFF")


def test_i1_a_retry_after_a_re_decision_runs_on_the_new_decision(store):
    # R3-5's known edge stays legal: human at CONFIRM, not_started, reopen, a binding's silence re-approval; the retry
    # carries the NEW decision's actor, rung and grant, not attempt 1's
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c)
        assert intent(c, by="human", posture="CONFIRM") == 1
        not_started(c, 1, grant_guard=None)
    with store.write() as c:
        RunJournal._reopen_in(c, H)
        silence_approve(c)
        assert binding_silence_intent(c) == 2
    with store.read() as c:
        assert json.loads(c.execute("SELECT seal FROM effects WHERE attempt = 2").fetchone()[0])["decision"] == \
            {"generation": 2, "by": "on-loop", "posture": "COOLING_OFF"}


def test_i1_a_decision_with_no_recorded_rung_admits_any_executing_rung(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c, posture=None)
        assert intent(c, by="on-loop", posture="ON_LOOP") == 1


def test_i1_the_codex_probe_seal_inserted_by_hand_is_refused_by_the_store(store):
    # the refusal is the store's, not only the builder's: the forged seal written directly
    silence_attempt_one(store)
    with store.write() as c:
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="human", posture="ON_LOOP"))
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 2, 'd', 'intent', ?)", (canon(sl),))


# ---- I2: a known actor at an executing posture --------------------------------------------------------------------
@pytest.mark.parametrize("by, posture", [
    ("on-loop", "REFUSE_ESCALATE"), ("on-loop", "SIDEWAYS"), ("on-loop", None), ("garbage", "ON_LOOP"),
    (None, "ON_LOOP"), ("", "ON_LOOP"),
])
def test_i2_intent_in_refuses_an_unknown_actor_or_a_non_executing_posture_and_writes_nothing(store, by, posture):
    with store.write() as c:
        run(c)
        with pytest.raises(ValueError, match="int-I2"):
            J.intent_in(c, "r", "link-0", digest="d", by=by, posture=posture)
        assert rows(c) == []


@pytest.mark.parametrize("field, value", [
    ("posture", "REFUSE_ESCALATE"), ("posture", "SIDEWAYS"), ("posture", None), ("posture", 1),
    ("by", "garbage"), ("by", None), ("by", 1),
])
def test_i2_the_store_refuses_a_seal_with_an_unknown_actor_or_a_non_executing_posture(store, field, value):
    with store.write() as c:
        run(c)
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="on-loop", posture="ON_LOOP"))
        sl[field] = value
        with pytest.raises(sqlite3.IntegrityError, match=KNOWN):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 1, 'd', 'intent', ?)", (canon(sl),))


@pytest.mark.parametrize("field", ["by", "posture"])
def test_i2_the_store_refuses_a_seal_missing_its_actor_or_posture(store, field):
    with store.write() as c:
        run(c)
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="on-loop", posture="ON_LOOP"))
        del sl[field]
        with pytest.raises(sqlite3.IntegrityError, match=KNOWN):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 1, 'd', 'intent', ?)", (canon(sl),))


# ---- I3: attempt 1 of a held link carries its decision -------------------------------------------------------------
def test_i3_codex_probe_attempt_one_on_an_approved_hold_without_a_decision_is_refused(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c)
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="human", posture="CONFIRM"))
        del sl["decision"]
        with pytest.raises(sqlite3.IntegrityError, match=HELD):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 1, 'd', 'intent', ?)", (canon(sl),))


def test_i3_attempt_one_carrying_another_generation_is_refused(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        human_approve(c)
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="human", posture="CONFIRM"))
        sl["decision"]["generation"] = 7
        with pytest.raises(sqlite3.IntegrityError, match=HELD):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 1, 'd', 'intent', ?)", (canon(sl),))


# ---- I4: a run id is never empty -------------------------------------------------------------------------------------
@pytest.mark.parametrize("run_id", ["", None])
def test_i4_the_store_refuses_a_run_with_an_empty_or_null_id(store, run_id):
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match="D56/D68/D71/I2"):
            c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b', 0, ?)",
                      (run_id, canon({"context_id": "c", "confirm_class": False, "guard": [], "links": 1})))


def test_i4_the_admission_helper_refuses_an_empty_run_id_before_the_store(store):
    with store.write() as c:
        with pytest.raises(ValueError, match="int-I4"):
            RunJournal._start_in(c, "", "b", 0, seal=canon({"context_id": "c", "confirm_class": False, "guard": [],
                                                             "links": 1}))
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# ---- I6: a hold-less retry re-uses its predecessor's grant ---------------------------------------------------------
def test_i6_deepseek_probe_a_holdless_retry_with_the_same_cover_and_another_grant_is_refused(store):
    with store.write() as c:
        run(c)
        assert intent(c, by="binding", posture="ON_LOOP", grant=GRANT, cover=COVER) == 1
        not_started(c, 1)
    swapped = grant("g2")   # same incarnation, so the same cover passes effects_grant_shape
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
            intent(c, by="binding", posture="ON_LOOP", grant=swapped, cover=COVER)
        assert intent(c, by="binding", posture="ON_LOOP", grant=GRANT, cover=COVER) == 2


# ---- complement LOW 1: the not_started reason is checked before anything is written ----------------------------------
@pytest.mark.parametrize("why", ["", None, 3])
def test_low1_a_not_started_close_without_a_reason_is_refused_and_the_attempt_stays_live(store, why):
    with store.write() as c:
        run(c)
        assert intent(c, by="on-loop", posture="ON_LOOP") == 1
        with pytest.raises(ValueError, match="why"):
            not_started(c, 1, grant_guard=None, why=why)
        assert rows(c) == [(1, "intent")]
