"""The signed guard of a graduation policy (autogrant DESIGN r72.1 I1 items 2-4, I7, D83, D97; 1026-20 RULINGS R13-4,
R13-11, R13-12): the guard is inside the bytes the laptop or passkey signs, every policy is signed with a complete one,
the replayed policy object carries exactly the terms its incarnation names, and the coverage source hands the fire the
one policy whose cover value its fence compares.

Every log here is real (``tests/test_authority_graduation_kind.World``: an ssh-keygen laptop key and passkey
stand-ins, verified by ssh-keygen through the replay). The fire-path cases run on the Journey rig
(``tests/test_graduation_journey``: ``FireDispatcher`` -> ``EfferentGate`` -> the run journal, the production
``AuthorityCoverage``). The older-binary case runs a levain checkout from before ``levain-authority/3`` in its own
interpreter; set ``LEVAIN_PRE_GUARD_CHECKOUT`` to that checkout's path."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import levain.authority as auth
from levain.autonomic import Posture
from levain.autonomic.coverage import (COVER_MOVED, GUARD_DRILL_MAX_BYTES, GUARD_KILL_MAX_BYTES, GUARD_KILL_MAX_DEPTH,
                                       GUARD_MAX_ENTRIES, GUARD_RATIONALE_MAX, Cover, CoverageSnapshot, CoverageSource,
                                       Effect, EffectiveCover, guard_problem, live, operative,
                                       peek_cover_snapshot)
from levain.autonomic.risk import ActionRisk, RiskClass
from levain.graduation_policy import graduation_policies

pytest.importorskip("cryptography")
from tests.autonomic_test_only_coverage import NO_GUARD, TEST_GUARD  # noqa: E402
from tests.test_authority_graduation_kind import (GP, GP2, V1, V3, World, consent, grad, guard_file,  # noqa: E402
                                                  renew)
from tests.test_graduation_journey import LOW_DICT, Journey  # noqa: E402
from tests.test_graduation_policy import source, write_root  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LOW = ActionRisk(RiskClass(1), reversible=True, external=False, financial=False)
APPEND = Effect("doc.append", LOW, None, None, None)
KILL = {"op": "==", "field": "dmarc", "value": "fail"}
OTHER_GUARD = ({"rationale": "a second sender is the case this could be wrong about",
                "kill_predicate": {"op": "==", "field": "from", "value": "b@y.example"},
                "kill_drill": {"fields": {"from": "b@y.example"}}},)
PRE_GUARD = os.environ.get("LEVAIN_PRE_GUARD_CHECKOUT")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def entry(**over) -> dict:
    e = {"rationale": "a spoofed sender", "kill_predicate": dict(KILL), "kill_drill": {"dmarc": "fail"}}
    e.update(over)
    return e


def nested(depth: int) -> dict:
    """A kill predicate ``depth`` nodes deep (one-clause ``and`` nodes around the leaf: the same kill)."""
    node: dict = dict(KILL)
    for _ in range(depth - 1):
        node = {"op": "and", "clauses": [node]}
    return node


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_of(world: World) -> CoverageSnapshot:
    write_root(world, "laptop", "pk")
    view = source(world).refresh()
    assert isinstance(view, CoverageSnapshot), view
    return view


# --- the format: the guard is in the signed bytes, every policy carries one, the link is /3 -------------------------

def test_a_guarded_policy_replays_with_its_incarnation_naming_its_signed_terms(world):
    """I1 item 3 and incarnation parity: on a real replayed log the policy object's incarnation is the authority log's
    own ``graduation_incarnation``, which is ``<born>:<sha256 of terms_text>``, and its guard is the one in the record
    the laptop signed."""
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    world.link(renew(world))
    state = world.state()
    [p] = graduation_policies(state)
    signed = world.record(snap.seq)["grant"]
    assert world.record(snap.seq)["format"] == V3
    assert p.incarnation == auth.graduation_incarnation(state, GP) == f"{p.born}:{sha(p.terms_text)}"
    assert p.born == state.grant_born[GP] == snap.seq
    assert p.terms_text == auth._canonical({k: v for k, v in signed.items() if k != "expires_at"})
    assert list(p.guard) == signed["guard"] == [dict(e) for e in TEST_GUARD]
    assert p.guarded


@pytest.mark.parametrize("lowest", ["ON_LOOP", "COOLING_OFF"])
def test_a_policy_with_no_guard_is_refused_at_sign_whatever_its_rung(world, lowest):
    """D83: every entry, every rung; it is an append rule, never the record's shape (I7), so the replay takes it."""
    seq = world.state().seq
    bare = grad(world, lowest_posture=lowest, guard=NO_GUARD)
    why = world.refused({"op": "create_grant", "grant": bare})
    assert f"graduation policy {GP!r} is refused at sign: it carries no guard" in why and world.state().seq == seq
    assert auth._validate_graduation(bare, "grant", None)["id"] == GP
    assert world.replay_verdict(world.plant({"op": "create_grant", "grant": bare})).startswith("ACCEPTED")


_SHAPE = "grant.guard must be a list of objects"     # the record's own shape refuses these before the append rule


@pytest.mark.parametrize("guard,why", [
    ([], "non-empty list"),
    ("kill", _SHAPE),
    (["x"], _SHAPE),
    ([entry(kill_drill=None)], "guard[0].kill_drill must be an object"),
    ([{"rationale": "r", "kill_predicate": dict(KILL)}], "must carry exactly"),
    ([entry(note="x")], "must carry exactly"),
    ([entry(rationale="  ")], "rationale must be non-blank"),
    ([entry(rationale="r" * (GUARD_RATIONALE_MAX + 1))], f"longer than {GUARD_RATIONALE_MAX} characters"),
    ([entry(kill_predicate={"op": "matches", "field": "x", "value": "y"})], "not a kill the gate can evaluate"),
    ([entry(kill_predicate={"op": "in", "field": "x", "value": []})], "not a kill the gate can evaluate"),
    ([entry(kill_drill={"dmarc": "pass"})], "kill_drill does not trip its kill"),
    ([entry()] * (GUARD_MAX_ENTRIES + 1), f"more than {GUARD_MAX_ENTRIES}"),
    ([entry(kill_predicate=nested(GUARD_KILL_MAX_DEPTH + 1))], f"more than {GUARD_KILL_MAX_DEPTH}"),
    ([entry(kill_predicate={"op": "in", "field": "x", "value": ["v" * 50] * 100})],
     f"longer than {GUARD_KILL_MAX_BYTES} bytes"),
    ([entry(kill_drill={"dmarc": "fail", "pad": "p" * GUARD_DRILL_MAX_BYTES})],
     f"longer than {GUARD_DRILL_MAX_BYTES} bytes"),
])
def test_an_incomplete_or_oversized_guard_is_refused_at_sign(world, guard, why):
    """D83 ("guard complete" on EVERY entry) and R13-11 (size and depth capped at sign)."""
    seq = world.state().seq
    assert why in world.refused({"op": "create_grant", "grant": grad(world, guard=guard)})
    assert world.state().seq == seq
    assert guard_problem(guard) is not None


def test_the_caps_take_what_they_name(world):
    at_cap = [entry(kill_predicate=nested(GUARD_KILL_MAX_DEPTH))] * GUARD_MAX_ENTRIES
    assert guard_problem(at_cap) is None
    world.link({"op": "create_grant", "grant": grad(world, guard=at_cap)})
    assert len(graduation_policies(world.state())[0].guard) == GUARD_MAX_ENTRIES


def test_a_policy_signed_before_the_guard_replays_and_covers_nothing(world):
    """I7 / R13-4: a /1 create of a guard-less policy (a levain from before the guard signed it) replays, is replayed
    as a policy, and covers nothing; a renew of it is refused, saying how to get coverage back."""
    db = world.plant({"op": "create_grant", "grant": grad(world, guard=NO_GUARD)}, fmt=V1, db=world.db)
    assert world.replay_verdict(db).startswith("ACCEPTED")
    [p] = graduation_policies(world.state())
    assert not p.guarded and p.guard == ()
    view = snapshot_of(world)
    assert view.policies == (p,) and not live(p, world.clock.now, view._root_keys)
    assert view.cover(APPEND, world.clock.now) == Cover(None, "none")
    assert operative(Posture.ON_LOOP, APPEND, view, world.clock.now) == (Posture.CONFIRM, "none")
    why = world.refused(renew(world))
    assert "is not renewed: it carries no guard" in why and "revoke it and create another with a guard" in why


def test_a_planted_policy_whose_guard_the_gate_cannot_evaluate_covers_nothing_and_the_others_stand(world):
    """D97: a /3 create no append judged, its kill malformed, replays; the replay source leaves that one policy out."""
    world.link({"op": "create_grant", "grant": grad(world, GP2)})
    bad = grad(world, guard=[entry(kill_predicate={"op": "matches", "field": "x", "value": "y"})])
    db = world.plant({"op": "create_grant", "grant": bad}, fmt=V3, db=world.db)
    assert world.replay_verdict(db).startswith("ACCEPTED")
    assert GP in world.state().policy.grants
    assert [p.grant_id for p in graduation_policies(world.state())] == [GP2]


def test_a_link_bringing_a_guarded_policy_in_any_other_format_is_refused_and_a_renew_of_one_is_v1(world):
    for fmt in (V1, "levain-authority/2"):
        assert "a link like it is written levain-authority/3" in world.replay_verdict(
            world.plant({"op": "create_grant", "grant": grad(world)}, fmt=fmt))
    world.link({"op": "create_grant", "grant": grad(world)})
    again = world.link(renew(world))
    assert world.record(again.seq)["format"] == V1
    policy = again.policy.as_dict()
    policy["grants"][GP2] = grad(world, GP2)
    loosen = world.link({"op": "loosen", "policy": policy})
    assert world.record(loosen.seq)["format"] == V3


def test_renew_keeps_the_incarnation_and_revoke_and_create_with_another_guard_is_another(world):
    """probe72 lines 7 / 8: a renew moves only the cover value; a revoke and a create of the same id with another guard
    is another incarnation, so a fence decided under the first is stale."""
    first = graduation_policies(world.link({"op": "create_grant", "grant": grad(world)}))[0]
    renewed = graduation_policies(world.link(renew(world)))[0]
    assert renewed.incarnation == first.incarnation and renewed.cover_value != first.cover_value
    world.link({"op": "revoke_grant", "grant_id": GP})
    other = graduation_policies(world.link({"op": "create_grant", "grant": grad(world, guard=OTHER_GUARD)}))[0]
    assert other.incarnation != first.incarnation and other.born > first.born
    assert list(other.guard) == [dict(e) for e in OTHER_GUARD]


# --- the policy object (probe72 I1 lines, over real coverage objects) ---------------------------------------------

def test_a_policy_cannot_carry_terms_its_incarnation_does_not_name(world):
    """probe72 line 2 (the guard altered in a copy, the incarnation kept) and line 3 (altered and re-hashed: a policy,
    but not the one signed: its cover value is not the live view's)."""
    world.link({"op": "create_grant", "grant": grad(world)})
    [p] = graduation_policies(world.state())
    terms = json.loads(p.terms_text)
    terms["guard"] = [entry(kill_predicate={"op": "==", "field": "dmarc", "value": "none"},
                            kill_drill={"dmarc": "none"})]
    altered = auth._canonical(terms)
    with pytest.raises(ValueError, match="not the ones the policy's incarnation names"):
        dataclasses.replace(p, terms_text=altered)
    forged = dataclasses.replace(p, terms_text=altered, incarnation=f"{p.born}:{sha(altered)}")
    view = snapshot_of(world)
    cover = view.cover(APPEND, world.clock.now)
    assert cover.policy == p and cover.value == p.cover_value != forged.cover_value


@pytest.mark.parametrize("field,value", [
    ("lowest_posture", Posture.COOLING_OFF), ("store_id", "store-" + "cd" * 16), ("grant_id", GP2),
    ("actions", (("doc.append", '{"cls":0,"external":false,"financial":false,"reversible":true}'),)),
    ("triggers", frozenset({"email"})), ("born", 99),
])
def test_a_policy_whose_fields_are_not_its_signed_terms_is_refused(world, field, value):
    world.link({"op": "create_grant", "grant": grad(world)})
    [p] = graduation_policies(world.state())
    with pytest.raises(ValueError):
        dataclasses.replace(p, **{field: value})


def test_the_guard_read_is_a_copy(world):
    world.link({"op": "create_grant", "grant": grad(world)})
    [p] = graduation_policies(world.state())
    p.guard[0]["kill_predicate"]["value"] = "pass"
    assert p.guard[0]["kill_predicate"] == KILL


def test_an_unlisted_signer_covers_nothing_and_two_covering_policies_name_the_strictest_rungs_least_value(world):
    """probe72 line 6 (a policy whose signer root does not list) and line 12 (two policies at the strictest rung: the
    cover names the least value, and that policy is ``Cover.policy``, the one whose guard applies)."""
    world.link({"op": "create_grant", "grant": grad(world)})
    write_root(world, "pk")
    assert source(world).refresh().cover(APPEND, world.clock.now) == Cover(None, "none")
    world.link({"op": "create_grant", "grant": grad(world, GP2, guard=OTHER_GUARD)})
    world.link({"op": "create_grant", "grant": grad(world, "gp-" + "1" * 12, lowest_posture="COOLING_OFF")})
    world.link({"op": "create_grant", "grant": grad(world, "gp-" + "2" * 12, lowest_posture="COOLING_OFF",
                                                    guard=OTHER_GUARD)})
    view = snapshot_of(world)
    cover = view.cover(APPEND, world.clock.now)
    strict = [p for p in view.policies if p.lowest_posture is Posture.COOLING_OFF]
    assert cover.posture is Posture.COOLING_OFF and len(strict) == 2
    assert cover.value == min(p.cover_value for p in strict) and cover.policy.cover_value == cover.value
    assert cover.policy in strict and cover.policy.guard == tuple(json.loads(cover.policy.terms_text)["guard"])


# --- the peek the fire takes its policy from (R13-11, I8) ---------------------------------------------------------

def test_peek_cover_is_the_peeks_value_with_the_policy_of_the_one_cached_view(world, monkeypatch):
    world.link({"op": "create_grant", "grant": grad(world)})
    write_root(world, "laptop")
    src = source(world)
    view = src.refresh()
    refreshes = []
    monkeypatch.setattr(src, "refresh", lambda: refreshes.append(1))
    ec = src.peek_cover(APPEND, world.clock.now)
    assert isinstance(ec, EffectiveCover) and ec.value == src.peek(APPEND, world.clock.now) == ec.policy.cover_value
    assert ec.posture is Posture.ON_LOOP and ec.policy is view.policies[0] and refreshes == []
    assert isinstance(src, CoverageSource) and "peek_cover" in CoverageSource.__dict__
    world.link({"op": "create_grant", "grant": grad(world, GP2)})        # the head moves: the cached view is stale
    assert src.peek_cover(APPEND, world.clock.now) == EffectiveCover(COVER_MOVED, None, None)


def test_a_covered_value_no_policy_of_the_view_gives_reaches_the_fire_with_no_policy(world):
    world.link({"op": "create_grant", "grant": grad(world)})
    view = snapshot_of(world)
    real = view.cover(APPEND, world.clock.now)
    other = graduation_policies(world.link({"op": "create_grant", "grant": grad(world, GP2)}))[1]

    class View:
        token = view.token

        def __init__(self, cover):
            self._cover = cover

        def cover(self, effect, now):
            return self._cover

    for cover in (Cover(real.posture, real.value), Cover(real.posture, real.value, other),
                  Cover(Posture.COOLING_OFF, real.value, real.policy)):
        ec = peek_cover_snapshot(View(cover), view.token, APPEND, world.clock.now)
        assert ec.value == cover.value and ec.policy is None
    assert peek_cover_snapshot(View(real), view.token, APPEND, world.clock.now).policy is real.policy


# --- R13-4 on the real fire path ------------------------------------------------------------------------------------

def _legacy_policy(j: Journey) -> None:
    """A /1 ON_LOOP policy with no guard, as a levain from before the guard's CLI signs it (V bb9713aa's
    ``levain binding policy create`` writes exactly this record; the run on V is in the lane's RETURN)."""
    rec = grad(j.auth, actions={"link0": dict(LOW_DICT)}, store_id=j.store_id, guard=NO_GUARD)
    j.auth.plant({"op": "create_grant", "grant": rec}, fmt=V1, db=j.auth.db)
    j.sync()


@pytest.mark.usefixtures("test_only_confined_executor")
def test_r13_4_a_legacy_guard_less_on_loop_policy_no_longer_lowers_an_on_loop_binding(tmp_path, monkeypatch):
    """R13-4's probe: (i) an ON_LOOP binding with no policy and (ii) the same binding with a legacy /1 guard-less
    ON_LOOP policy, one event. On V (bb9713aa) (ii) FIRED at ON_LOOP where (i) held at CONFIRM: the policy lowered a
    confirm-class posture, so it covers nothing until it is signed anew with a guard (iii)."""
    arms = {}
    for name in ("i", "ii", "iii"):
        (tmp_path / name).mkdir()
        j = Journey(tmp_path / name, monkeypatch)
        if name == "ii":
            _legacy_policy(j)
        if name == "iii":
            j.create(lowest="ON_LOOP")
        j.fire.mint(Posture.ON_LOOP)
        o = j.dispatch(f"R13-4 arm {name}")
        arms[name] = (o.fired, o.posture, j.fire.outbox())
        print(f"[R13-4 ({name})] fired={o.fired} posture={o.posture.name} outbox={j.fire.outbox()}")
    assert arms["i"] == arms["ii"] == (False, Posture.CONFIRM, [])
    assert arms["iii"] == (True, Posture.ON_LOOP, ["link0"])


@pytest.mark.usefixtures("test_only_confined_executor")
def test_the_cli_refuses_a_guard_it_could_not_sign_before_signing_and_prints_the_guard_back(tmp_path, monkeypatch,
                                                                                           capsys):
    j = Journey(tmp_path, monkeypatch)
    seq = j.auth.state().seq
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([entry(kill_drill={"dmarc": "pass"})]))
    assert j.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", GP, "--guard", str(bad)) == 1
    assert "kill_drill does not trip its kill" in capsys.readouterr().err and j.auth.state().seq == seq
    assert j.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--id", GP, "--guard",
                 str(guard_file(tmp_path))) == 0
    out = capsys.readouterr().out
    assert TEST_GUARD[0]["rationale"] in out and json.dumps(TEST_GUARD[0]["kill_predicate"], sort_keys=True) in out
    assert out.index(TEST_GUARD[0]["rationale"]) < out.index("signed:")
    assert list(graduation_policies(j.auth.state())[0].guard) == [dict(e) for e in TEST_GUARD]


# --- R13-12: a levain from before /3 stops at the link that brings the guard in -------------------------------------

_OLDER_SCRIPT = '''
import json, sqlite3, sys
import levain.authority as a
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
out = {"file": a.__file__, "has_v3": hasattr(a, "FORMAT_V3")}
try:
    out["seq"] = a.snapshot(conn, cache=a.ReplayCache()).seq
except a.AuthorityError as exc:
    out["refused"] = str(exc)
print(json.dumps(out))
'''


def pre_guard_replay(tmp: Path, db: Path) -> dict:
    script = tmp / "pre_guard_replay.py"
    script.write_text(_OLDER_SCRIPT)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = PRE_GUARD
    got = subprocess.run([sys.executable, str(script), str(db)], capture_output=True, text=True, env=env,
                         cwd=str(tmp), timeout=120)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    assert out["file"].startswith(str(Path(PRE_GUARD).resolve())) and not out["has_v3"], out
    return out


@pytest.mark.skipif(not PRE_GUARD, reason="set LEVAIN_PRE_GUARD_CHECKOUT to a levain checkout from before "
                                          "levain-authority/3")
def test_a_levain_from_before_v3_stops_at_the_guarded_create_and_never_reads_its_renew_or_a_loosen(world):
    legacy = world.plant({"op": "create_grant", "grant": grad(world, GP2, guard=NO_GUARD)}, fmt=V1, db=world.db)
    head = world.state(legacy).seq
    assert pre_guard_replay(world.tmp, world.db)["seq"] == head
    created = world.link({"op": "create_grant", "grant": grad(world)})
    renewed = world.link(renew(world))
    policy = renewed.policy.as_dict()
    policy["grants"]["cg-0123456789ab"] = consent(world)
    loosen = world.link({"op": "loosen", "policy": policy})     # carries the guarded policy, unchanged, in a /1 link
    formats = [world.record(s)["format"] for s in (created.seq, renewed.seq, loosen.seq)]
    out = pre_guard_replay(world.tmp, world.db)
    print(f"[R13-12 formats create/renew/loosen] {formats} | [older reader] {out}")
    assert formats == [V3, V1, V1]
    assert "seq" not in out and out["refused"] == (f"authority log link {created.seq}: the record's format is "
                                                   "not levain-authority/1 or levain-authority/2")
