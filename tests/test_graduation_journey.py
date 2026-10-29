"""The 14b journey (DESIGN_REV2 §9 S4's seven scenarios, DESIGN_REV3 §10 S4's 8 and 9, RULINGS_S1 item 2) through
the real fire path: ``FireDispatcher`` -> ``EfferentGate`` -> run journal, on a temp store, with the production
coverage source (:class:`levain.graduation_policy.AuthorityCoverage`) over a real authority log (an ssh-keygen laptop
key, WebAuthn passkey stand-ins, ``tests/test_authority_graduation_kind.World``) and a real root allowed-signers file
under the test-only owner override (``tests/conftest.py``). Policies are signed by ``levain binding policy ...``
itself where the laptop signs; a passkey's links go through the real ``sign_and_append``. Confinement is replaced as
the fire path tests replace it. Each scenario prints what the fire path answered (``-s`` shows it)."""
from __future__ import annotations

import shlex
import shutil
import sqlite3
from datetime import timedelta

import pytest

import levain.cli as cli
from levain.autonomic import ActionManifest, BindingStatus, Posture
from levain.autonomic.db import AutonomicDB
from levain.autonomic.graduation import graduate_due
from levain.cockpit.firejournal import CockpitDB
from levain.graduation_policy import AuthorityCoverage

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World, grad, guard_file, opt_in, renew  # noqa: E402
from tests.test_autonomic_gate_coverage import CWorld  # noqa: E402
from tests.test_autonomic_journal_wiring import HIGH, LOW  # noqa: E402
from tests.test_graduation_policy import write_root  # noqa: E402

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.usefixtures("test_only_confined_executor")]

MANIFEST = {"link0": LOW, "link1": HIGH, "evil": LOW}
GP = "gp-0123456789ab"
LOW_DICT = {"cls": 1, "reversible": True, "external": False, "financial": False}


class Journey:
    def __init__(self, tmp, monkeypatch) -> None:
        (tmp / "auth").mkdir()
        self.auth = World(tmp / "auth")
        assert CockpitDB(self.auth.root).set_install_class("reported") == "reported"
        self.store_dir = tmp / "fire" / "store"
        self.store_id = AutonomicDB(self.store_dir).meta("store_id")
        self.signers = write_root(self.auth, "laptop")
        self.coverage = AuthorityCoverage(self.auth.root, allowed_signers=self.signers, store_id=self.store_id)
        self.fire = CWorld(tmp / "fire", coverage=self.coverage, auto_fire=frozenset({"link0"}), manifest=MANIFEST)
        self.events = 0
        self.sync()

        def now():
            self.auth.clock.now += timedelta(minutes=1)
            return self.auth.clock.now
        monkeypatch.setattr(cli, "_utc_now", now)
        self.monkeypatch = monkeypatch

    def sync(self) -> None:
        self.fire.now = self.auth.clock.now

    def later(self, delta: timedelta) -> None:
        self.auth.clock.now += delta
        self.sync()

    def cli(self, *argv: str) -> int:
        extra = ["--data-root", str(self.auth.root)]
        if argv[:2] in (("policy", "create"), ("policy", "renew"), ("policy", "revoke")):
            extra += ["--signing-key", str(self.auth.tmp / "keys" / "laptop")]
        if argv[:2] == ("policy", "create"):
            extra += ["--store-dir", str(self.store_dir)]
            if "--guard" not in argv:
                extra += ["--guard", str(guard_file(self.auth.tmp))]
        rc = cli.main(["binding", *argv, *extra])
        self.sync()
        return rc

    def create(self, *, lowest: str = "ON_LOOP", days: int = 30, action: str = "link0") -> None:
        assert self.cli("policy", "create", "--action", action, "--lowest", lowest, "--days", str(days),
                        "--id", GP) == 0

    def dispatch(self, label: str):
        self.sync()
        self.events += 1
        d = self.fire.dispatch(f"m{self.events}")
        o = d.outcome
        print(f"  {label}: fired={o.fired} posture={o.posture.name} pending={o.pending} held={o.held} "
              f"reason={o.reason}")
        return o

    def clear(self) -> None:
        """Let the CONFIRM decision a held dispatch opened lapse: an open decision on a binding holds its later runs
        (``journal:held``) until it is decided, and CONFIRM's silence default is a drop."""
        self.later(timedelta(days=1, minutes=1))
        for s in self.fire.sweep(self.fire.now):
            print(f"  sweep: fired={s.fired} posture={s.posture.name} refused={s.refused} reason={s.reason}")
        assert self.fire.gate.open_pendings() == []

    def engine(self, apply: bool):
        return graduate_due(self.fire.store, self.coverage, clock=lambda: self.fire.now,
                            manifest=ActionManifest(MANIFEST), link_action=lambda b, i: "link0",
                            risk_resolver=lambda b: LOW, link_risk_resolver=self.fire.gate.binding_risk, apply=apply)

    def steps(self) -> list[tuple]:
        conn = sqlite3.connect(self.store_dir / "autonomic.db")
        try:
            return conn.execute("SELECT old_id, new_id, from_posture, to_posture, clean_count, fire_count, policy_id, "
                                "authority_head FROM graduation_steps").fetchall()
        finally:
            conn.close()


@pytest.fixture
def j(tmp_path, monkeypatch) -> Journey:
    return Journey(tmp_path, monkeypatch)


def test_scenario_1_no_policy_holds_at_confirm(j):
    print("\nSCENARIO 1: no policy")
    j.fire.mint(Posture.ON_LOOP)
    o = j.dispatch("ON_LOOP binding")
    assert not o.fired and o.posture is Posture.CONFIRM and j.fire.outbox() == []


def test_scenario_2_policy_create_fires_on_loop(j):
    print("\nSCENARIO 2: levain binding policy create, then a fire")
    j.fire.mint(Posture.ON_LOOP)
    assert not j.dispatch("before the policy").fired
    j.clear()
    j.create()
    o = j.dispatch("after the policy")
    assert o.fired and o.posture is Posture.ON_LOOP and j.fire.outbox() == ["link0"]


def test_scenario_3_a_cooling_off_pending_then_revoke_then_the_sweep_does_not_fire(j):
    print("\nSCENARIO 3: COOLING_OFF pending, policy revoke, sweep after the window")
    j.create(lowest="COOLING_OFF")
    j.fire.mint(Posture.COOLING_OFF)
    o = j.dispatch("COOLING_OFF binding")
    assert o.pending and o.posture is Posture.COOLING_OFF
    assert j.cli("policy", "revoke", GP) == 0
    j.later(timedelta(days=1))
    out = j.fire.sweep(j.fire.now)
    for s in out:
        print(f"  sweep: fired={s.fired} posture={s.posture.name} refused={s.refused} reason={s.reason}")
    print(f"  open pendings after: {[(p.action_name, p.posture) for p in j.fire.gate.open_pendings()]}")
    assert out and not any(s.fired for s in out) and j.fire.outbox() == []


def test_scenario_4_a_planted_hold_of_an_action_no_policy_signs_never_fires(j):
    print("\nSCENARIO 4: a planted cooling-off hold whose pending runs an unsigned action")
    j.create()                                   # signs link0 only
    b = j.fire.mint(Posture.ON_LOOP)
    j.fire.plant_hold(b, "evil")
    j.later(timedelta(days=1))
    out = j.fire.sweep(j.fire.now)
    for s in out:
        print(f"  sweep: fired={s.fired} posture={s.posture.name} refused={s.refused} reason={s.reason}")
    assert out and not any(s.fired for s in out) and j.fire.outbox() == []


def test_scenario_5_a_substituted_cockpit_db_signed_by_another_key_covers_nothing(j, tmp_path):
    print("\nSCENARIO 5: cockpit.db replaced by a fresh genesis whose policy another laptop signed")
    (tmp_path / "other").mkdir()
    other = World(tmp_path / "other")
    other.clock.now = j.auth.clock.now
    other.link({"op": "create_grant", "grant": grad(other, actions={"link0": dict(LOW_DICT)},
                                                       store_id=j.store_id)})
    src, dst = sqlite3.connect(other.db), sqlite3.connect(j.auth.db)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    view = j.coverage.refresh()
    print(f"  refresh: {type(view).__name__}")
    j.sync()
    j.fire.mint(Posture.ON_LOOP)
    o = j.dispatch("ON_LOOP binding, the anchor in place")
    assert not o.fired and o.posture is Posture.CONFIRM
    j.clear()
    anchor = j.auth.root / "anchor" / "authority.anchor"
    anchor.rename(anchor.with_name("authority.anchor.aside"))   # the substituted log now replays
    view = j.coverage.refresh()
    print(f"  refresh with the anchor moved aside: {type(view).__name__}, {len(view.policies)} policy, root lists "
          "only this cockpit's laptop")
    o = j.dispatch("ON_LOOP binding, the anchor aside")
    assert not o.fired and o.posture is Posture.CONFIRM


def test_scenario_6_twenty_clean_fires_graduate_through_the_card_line_with_an_audit_row(j, capsys):
    print("\nSCENARIO 6: 20 clean fires, levain binding graduate: a card, its line signed, then the step")
    b = j.fire.mint(Posture.COOLING_OFF)
    for i in range(20):
        j.fire.store.record_fire(b.binding_id, clean=True, fired_at=f"2026-10-25T00:00:{i:02d}")
    j.monkeypatch.setattr(cli, "_graduation_engine", lambda args: j.engine)
    assert cli.main(["binding", "graduate"]) == 0
    out = capsys.readouterr().out
    with capsys.disabled():
        print("  graduate (no policy):\n    " + out.strip().replace("\n", "\n    "))
    line = next(x.strip() for x in out.splitlines() if x.strip().startswith("levain binding policy create"))
    # RULINGS_R1 item 5: the line as printed, only the key filled in: it names this engine's store and cockpit
    assert f"--store-dir {shlex.quote(str(j.store_dir))}" in line
    assert f"--data-root {shlex.quote(str(j.auth.root))}" in line
    argv = shlex.split(line.replace("<key>", shlex.quote(str(j.auth.tmp / "keys" / "laptop")))
                       .replace("<guard.json>", shlex.quote(str(guard_file(j.auth.tmp)))))[1:]
    assert cli.main(argv) == 0
    j.sync()
    capsys.readouterr()
    assert cli.main(["binding", "graduate"]) == 0
    out = capsys.readouterr().out
    rows = j.steps()
    with capsys.disabled():
        print("  graduate (after the card's line):\n    " + out.strip().replace("\n", "\n    "))
        print(f"  graduation_steps: {rows}")
    [(old, new, frm, to, clean, fires, pid, head)] = rows
    assert (old, frm, to, clean, fires) == (b.binding_id, "COOLING_OFF", "ON_LOOP", 20, 20) and pid.startswith("gp-")
    assert j.fire.store.get(b.binding_id).status is BindingStatus.REVOKED
    with capsys.disabled():
        o = j.dispatch("the promoted binding")
    assert o.fired and o.posture is Posture.ON_LOOP


def test_scenario_7_a_lapse_holds_at_confirm_and_leaves_the_binding(j):
    print("\nSCENARIO 7: lapse past expires_at")
    j.create(days=1)
    b = j.fire.mint(Posture.ON_LOOP)
    assert j.dispatch("inside the day").fired
    j.later(timedelta(days=1, minutes=1))
    o = j.dispatch("past the expiry")
    assert not o.fired and o.posture is Posture.CONFIRM
    assert j.fire.store.get(b.binding_id).status is BindingStatus.ACTIVE


def test_scenario_8_a_passkey_born_policy_covers_only_with_its_root_line(j):
    print("\nSCENARIO 8: a passkey-born policy (opt-in, stand-in key)")
    j.auth.link({"op": "create_grant", "grant": opt_in(j.auth, "pk")})
    j.auth.link({"op": "create_grant", "grant": grad(j.auth, actions={"link0": dict(LOW_DICT)}, store_id=j.store_id)},
                "pk")
    j.fire.mint(Posture.ON_LOOP)
    assert not j.dispatch("root lists the laptop only").fired
    j.clear()
    write_root(j.auth, "laptop", "pk")
    assert j.dispatch("root lists the passkey too").fired


def test_scenario_9_a_renew_by_a_key_root_does_not_list_uncovers_and_10_a_revoked_signer_too(j):
    print("\nSCENARIO 9: K2 (enrolled, not root-listed) renews K1's policy")
    j.create()
    j.fire.mint(Posture.ON_LOOP)
    assert j.dispatch("K1's policy").fired
    j.auth.link(renew(j.auth), "pk")
    assert not j.dispatch("after K2's renew, root lists K1 only").fired
    j.clear()
    write_root(j.auth, "laptop", "pk")
    assert j.dispatch("root lists K2 too").fired
    print("SCENARIO 10 (RULINGS_S1 item 2): K2 revoked in the log, root still lists it")
    j.auth.link({"op": "revoke_key", "fingerprint": j.auth.fp("pk")})
    assert not j.dispatch("after the key revoke").fired


def test_a_card_line_copied_to_another_directory_signs_the_engines_store(j, tmp_path, capsys):
    # RULINGS_R2 item 3 (codex item 5): the engine runs on relative store and cockpit paths from directory A; its
    # printed line, run from directory B, signs a policy for A's store into A's log
    from pathlib import Path

    from levain.autonomic import BindingStore

    print("\nRELATIVE PATHS: the engine in A, the card's line run in B")
    b = j.fire.mint(Posture.COOLING_OFF)
    for i in range(20):
        j.fire.store.record_fire(b.binding_id, clean=True, fired_at=f"2026-10-25T00:00:{i:02d}")
    j.monkeypatch.chdir(tmp_path)
    store = BindingStore(Path(j.store_dir.relative_to(tmp_path)))
    coverage = AuthorityCoverage(Path(j.auth.root.relative_to(tmp_path)), allowed_signers=j.signers,
                                 store_id=j.store_id)

    def engine(apply):
        return graduate_due(store, coverage, clock=lambda: j.fire.now, manifest=ActionManifest(MANIFEST),
                            link_action=lambda b, i: "link0", risk_resolver=lambda b: LOW,
                            link_risk_resolver=j.fire.gate.binding_risk, apply=apply)
    j.monkeypatch.setattr(cli, "_graduation_engine", lambda args: engine)
    assert cli.main(["binding", "graduate"]) == 0
    out = capsys.readouterr().out
    line = next(x.strip() for x in out.splitlines() if x.strip().startswith("levain binding policy create"))
    with capsys.disabled():
        print(f"  the line: {line}")
    assert f"--store-dir {shlex.quote(str(j.store_dir.resolve()))}" in line
    assert f"--data-root {shlex.quote(str(j.auth.root.resolve()))}" in line
    (tmp_path / "B").mkdir()
    j.monkeypatch.chdir(tmp_path / "B")
    argv = shlex.split(line.replace("<key>", shlex.quote(str(j.auth.tmp / "keys" / "laptop")))
                       .replace("<guard.json>", shlex.quote(str(guard_file(j.auth.tmp)))))[1:]
    assert cli.main(argv) == 0
    j.sync()
    capsys.readouterr()
    # the engine on A's absolute paths (a relative store handle reads relative to the process's directory): the
    # policy the line signed from B covers A's binding
    j.monkeypatch.setattr(cli, "_graduation_engine", lambda args: j.engine)
    assert cli.main(["binding", "graduate"]) == 0
    assert capsys.readouterr().out.startswith("graduated: ")
    assert [r[0] for r in j.steps()] == [b.binding_id]
