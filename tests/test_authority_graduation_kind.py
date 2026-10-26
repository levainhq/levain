"""The graduation policy in the authority log (14b DESIGN_REV3 §3, §7, §10 S1; HEAD_RULINGS_R1 R6, R7).

A ``graduation`` grant is a signed envelope (actions at their risk, the lowest rung, an expiry) whose terms are fixed
under its id: the only change is a renew. A ``graduation-phone`` grant is the opt-in naming the passkeys that may
create one; it never changes. A passkey signs, in this log, a grant revoke, a graduation policy's renew, or (named by
the live opt-in) one graduation policy's create, and nothing else. A link that needs one of those rules is written
``levain-authority/2``, so an older levain stops at it instead of misreading it. Every log here is real: an ssh-keygen
laptop key and display-phone key, and WebAuthn stand-ins answering as a phone's authenticator does
(``tests/test_passkey_custody_derived.py``), verified by ssh-keygen through the replay.

The older-binary cases run a levain checkout from before ``levain-authority/2`` in its own interpreter; set
``LEVAIN_OLDER_CHECKOUT`` to that checkout's path."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB

pytest.importorskip("cryptography")
from tests.autonomic_test_only_coverage import NO_GUARD, TEST_GUARD  # noqa: E402
from tests.test_consent_queue import Key  # noqa: E402
from tests.test_passkey_custody_derived import WebAuthnKey  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
GP = "gp-0123456789ab"
GP2 = "gp-ba9876543210"
OPT = "gpo-0123456789ab"
STORE = "store-" + "ab" * 16
LOW = {"cls": 1, "reversible": True, "external": False, "financial": False}
V1, V2, V3 = "levain-authority/1", "levain-authority/2", "levain-authority/3"
OLDER = os.environ.get("LEVAIN_OLDER_CHECKOUT")
older = pytest.mark.skipif(not OLDER, reason="set LEVAIN_OLDER_CHECKOUT to a levain checkout from before "
                                             "levain-authority/2")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class World:
    """A cockpit whose anchored log enrols a laptop-presence ssh key, a display-phone ssh key (custody unstated) and
    two passkey stand-ins (display-phone, custody passkey). The clock moves a minute before every link."""

    def __init__(self, tmp: Path) -> None:
        (tmp / "keys").mkdir()
        self.tmp = tmp
        self.root = tmp / "cockpit"
        self.root.mkdir()
        with CockpitDB(self.root).read():
            pass
        self.clock = Clock(T0)
        laptop, phone = Key(tmp / "keys", "laptop"), Key(tmp / "keys", "phone")
        self.pk, self.pk2 = WebAuthnKey(), WebAuthnKey()
        self.keys = {"laptop": (laptop.fp, lambda m: laptop.sign(m, auth.NAMESPACE)),
                     "phone": (phone.fp, lambda m: phone.sign(m, auth.NAMESPACE)),
                     "pk": (self.pk.fp, lambda m: self.pk.sign(m, auth.NAMESPACE)),
                     "pk2": (self.pk2.fp, lambda m: self.pk2.sign(m, auth.NAMESPACE))}
        self.link({"op": "enrol", "fingerprint": laptop.fp, "public_key": laptop.public_key,
                   "class": "laptop-presence", "custody": "passphrase"})
        self.link({"op": "enrol", "fingerprint": phone.fp, "public_key": phone.public_key, "class": "display-phone",
                   "custody": "unstated"})
        for pk in (self.pk, self.pk2):
            self.link({"op": "enrol", "fingerprint": pk.fp, "public_key": pk.public_key, "class": "display-phone",
                       "custody": "passkey"})

    @property
    def db(self) -> Path:
        return self.root / "cockpit.db"

    def fp(self, name: str) -> str:
        return self.keys[name][0]

    def link(self, op, signer: str = "laptop") -> auth.AuthoritySnapshot:
        self.clock.now += timedelta(minutes=1)
        fp, sign = self.keys[signer]
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            return auth.sign_and_append(conn, op, signer_fp=fp, sign=sign, clock=self.clock, cache=ReplayCache(),
                                        anchor=anchor_path(self.root))
        finally:
            conn.close()

    def refused(self, op, signer: str = "laptop") -> str:
        """The refusal of ``op`` signed by ``signer`` through the real append (the log is left as it was)."""
        with pytest.raises(auth.AuthorityError) as exc:
            self.link(op, signer)
        return str(exc.value)

    def state(self, db: Path | None = None) -> auth.AuthoritySnapshot:
        conn = sqlite3.connect(db or self.db, isolation_level=None)
        try:
            return auth.snapshot(conn, cache=ReplayCache())
        finally:
            conn.close()

    def record(self, seq: int, db: Path | None = None) -> dict:
        conn = sqlite3.connect(db or self.db)
        try:
            return json.loads(conn.execute("SELECT record FROM authority_log WHERE seq = ?", (seq,)).fetchone()[0])
        finally:
            conn.close()

    def plant(self, op, signer: str = "laptop", fmt: str = V1, db: Path | None = None) -> Path:
        """A link no append judged: the record built by hand at the head (``fmt`` its format), signed by
        ``signer``'s real key and inserted raw into ``db`` (a copy of the log unless given). Returns the db."""
        if db is None:
            db = self.tmp / f"planted-{len(list(self.tmp.glob('planted-*')))}.db"
            shutil.copy(self.db, db)
        self.clock.now += timedelta(minutes=1)
        fp, sign = self.keys[signer]
        conn = sqlite3.connect(db, isolation_level=None)
        try:
            seq, prev = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
            record = auth._canonical({**op, "format": fmt, "store_id": self.state().store_id, "seq": seq + 1,
                                      "at": utc_stamp(self.clock.now)})
            sig = sign(auth._message(prev, record))
            conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (seq + 1, prev, record, fp, sig, auth._link_hash(prev, record)))
        finally:
            conn.close()
        return db

    def replay_verdict(self, db: Path) -> str:
        try:
            return f"ACCEPTED {self.state(db).seq}"
        except auth.AuthorityError as exc:
            return str(exc)


def grad(world: World, gid: str = GP, days: int = 30, **over) -> dict:
    """A graduation policy record, signed with :data:`TEST_GUARD` (``guard=NO_GUARD``: one from before the guard)."""
    g = {"id": gid, "kind": "graduation", "actions": {"doc.append": dict(LOW)}, "lowest_posture": "ON_LOOP",
         "expires_at": utc_stamp(world.clock.now + timedelta(days=days)), "triggers": None, "families": None,
         "store_id": STORE, "min_clean_fires": 20, "guard": [dict(e) for e in TEST_GUARD]}
    g.update(over)
    if g["guard"] is NO_GUARD:
        del g["guard"]
    return g


def guard_file(tmp: Path, guard=TEST_GUARD) -> Path:
    """``levain binding policy create --guard``'s file, holding ``guard``."""
    path = tmp / "guard.json"
    path.write_text(json.dumps([dict(e) for e in guard]))
    return path


def opt_in(world: World, *names: str, gid: str = OPT) -> dict:
    return {"id": gid, "kind": "graduation-phone", "passkeys": sorted(world.fp(n) for n in names)}


def renew(world: World, gid: str = GP, days: int = 30) -> dict:
    # the stamp is of the time the link will carry (World.link moves the clock a minute first)
    at = world.clock.now + timedelta(minutes=1)
    return {"op": "renew", "grant_id": gid, "expires_at": utc_stamp(at + timedelta(days=days)),
            "signed_at": utc_stamp(at)}


def consent(world: World, gid: str = "cg-0123456789ab") -> dict:
    ws = Path(os.path.realpath(world.tmp / "proj"))
    ws.mkdir(exist_ok=True)
    return {"id": gid, "kind": "consent", "agent": "claude_code", "tools": ["Write"], "workspace": str(ws),
            "postures": ["default"], "expires_at": utc_stamp(world.clock.now + timedelta(days=1))}


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --- the constants -------------------------------------------------------------------------------------------------

def test_the_constants_are_pinned_to_the_rules_they_name():
    assert auth.FORMAT_V2 == V2 and auth.FORMAT == V1
    assert auth.GRADUATION_CREATE_RANK == auth._DELTA_RANK[auth.LOOSEN] == 3
    assert auth.GRADUATION_GRANT_MAX == auth._CONSENT_GRANT_MAX == timedelta(days=30)
    # the risk dict and the rungs are the autonomic layer's own (read here, never imported by the replay)
    from levain.autonomic.gate import _risk_dict
    from levain.autonomic.posture import Posture
    from levain.autonomic.risk import ActionRisk, RiskClass
    assert set(_risk_dict(ActionRisk(RiskClass.LOW, True, False, False))) == auth._GRADUATION_RISK_FIELDS
    assert auth._GRADUATION_RISK_CEILING == int(RiskClass.LOW)
    assert set(auth._GRADUATION_POSTURES) == {p.name for p in Posture if Posture.ABOVE_LOOP < p < Posture.CONFIRM}


# --- §7 row 3: the laptop creates (rank 3; /3, the policy carrying its guard) ----------------------------------------

def test_the_laptop_creates_a_graduation_policy_in_a_v3_link(world):
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    assert world.record(snap.seq)["format"] == V3
    assert snap.policy.grants[GP]["kind"] == "graduation" and not auth.inert_grant(snap.policy.grants[GP])
    assert auth.graduation_links(snap, GP) == (world.fp("laptop"), world.fp("laptop"))
    assert auth.graduation_incarnation(snap, GP).startswith(f"{snap.seq}:")


def test_a_display_phone_key_cannot_create_one(world):
    assert "a display-phone key cannot sign this create_grant" in world.refused(
        {"op": "create_grant", "grant": grad(world)}, "phone")


@pytest.mark.parametrize("over, why", [
    ({"actions": {}}, "actions"),
    ({"actions": {"doc.append": {**LOW, "cls": 2}}}, ".cls"),
    ({"actions": {"doc.append": {**LOW, "reversible": False}}}, ".reversible"),
    ({"actions": {"doc.append": {**LOW, "external": True}}}, ".external"),
    ({"actions": {"doc.append": {**LOW, "financial": True}}}, ".financial"),
    ({"actions": {"doc.append": {**LOW, "extra": True}}}, "risk dict"),
    ({"actions": {"doc.append": {**LOW, "cls": True}}}, "risk dict"),
    ({"lowest_posture": "ABOVE_LOOP"}, "lowest_posture"),
    ({"lowest_posture": "CONFIRM"}, "lowest_posture"),
    ({"triggers": []}, "triggers"),
    ({"families": []}, "families"),
    ({"families": ["fam-x"]}, "families"),
    ({"store_id": "store-x"}, "store_id"),
    ({"min_clean_fires": -1}, "min_clean_fires"),
    ({"id": "gp-nothex"}, "id"),
    ({"tools": ["x"]}, "exactly"),
])
def test_a_malformed_graduation_policy_is_refused(world, over, why):
    assert why in world.refused({"op": "create_grant", "grant": grad(world, **over)})


def test_a_policy_lasts_at_most_30_days_past_its_link(world):
    assert "at most 30 days past its link's time" in world.refused(
        {"op": "create_grant", "grant": grad(world, days=31)})
    # and the replay holds it too: a hand-built link past the cap
    db = world.plant({"op": "create_grant", "grant": grad(world, days=31)})
    assert "at most 30 days past its link's time" in world.replay_verdict(db)


# --- §7 row 2: renew (any enrolled passkey, or the laptop; /2 for a passkey) ----------------------------------------

def test_a_passkey_renews_a_graduation_policy_in_a_v2_link_and_the_incarnation_stays(world):
    born = world.link({"op": "create_grant", "grant": grad(world)})
    inc = auth.graduation_incarnation(born, GP)
    snap = world.link(renew(world), "pk")
    assert world.record(snap.seq)["format"] == V2
    assert auth.graduation_links(snap, GP) == (world.fp("laptop"), world.fp("pk"))
    assert auth.graduation_incarnation(snap, GP) == inc
    again = world.link(renew(world), "laptop")
    assert world.record(again.seq)["format"] == V1
    assert auth.graduation_links(again, GP) == (world.fp("laptop"), world.fp("laptop"))
    assert world.state().seq == again.seq                          # a fresh replay takes all three


def test_a_passkey_renew_needs_no_opt_in_but_never_renews_another_kind(world):
    world.link({"op": "create_grant", "grant": consent(world)})
    assert "a passkey key renews a graduation policy only, never grant 'cg-0123456789ab'" in world.refused(
        renew(world, "cg-0123456789ab"), "pk")


def test_a_lapsed_policy_is_not_renewed(world):
    world.link({"op": "create_grant", "grant": grad(world, days=1)})
    world.clock.now += timedelta(days=2)
    assert "has expired" in world.refused(renew(world), "pk")


# --- the renew cap (R7, complement's MED-4): bound by the kind at append and at replay ------------------------------

def test_a_renew_past_30_days_is_refused_at_append_and_at_replay(world):
    world.link({"op": "create_grant", "grant": grad(world)})
    assert "at most 30 days" in world.refused(renew(world, days=31), "pk")
    at = world.clock.now + timedelta(minutes=1)
    db = world.plant({"op": "renew", "grant_id": GP, "expires_at": utc_stamp(at + timedelta(days=31)),
                      "signed_at": utc_stamp(at)}, "laptop", V1)
    assert "at most 30 days" in world.replay_verdict(db)


def test_the_kind_binds_the_renew_cap_whatever_the_generic_renew_bound(world, monkeypatch):
    """A graduation policy's cap is its own (GRADUATION_GRANT_MAX), not the generic renew bound: with that bound
    widened, a 45-day renew is still refused at append and at replay."""
    world.link({"op": "create_grant", "grant": grad(world)})
    monkeypatch.setattr(auth, "_RENEW_MAX", timedelta(days=60))
    assert "at most 30 days" in world.refused(renew(world, days=45), "laptop")
    at = world.clock.now + timedelta(minutes=1)
    db = world.plant({"op": "renew", "grant_id": GP, "expires_at": utc_stamp(at + timedelta(days=45)),
                      "signed_at": utc_stamp(at)}, "laptop", V1)
    assert "at most 30 days" in world.replay_verdict(db)


# --- immutable terms: a loose or tight change, or a kind swap, is refused at append and at replay -------------------

@pytest.mark.parametrize("change", [
    {"actions": {"doc.append": dict(LOW), "doc.delete": dict(LOW)}},        # loose
    {"lowest_posture": "COOLING_OFF"},                                       # tight
    {"families": ["fam-" + "0" * 32]},                                       # tight
    {"min_clean_fires": 40},
])
def test_a_graduation_policy_terms_change_is_refused(world, change):
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    policy = snap.policy.as_dict()
    policy["grants"][GP] = {**policy["grants"][GP], **change}
    assert "a graduation policy's terms are fixed: revoke it and create another" in world.refused(
        {"op": "loosen", "policy": policy})
    db = world.plant({"op": "loosen", "policy": policy})
    assert "a graduation policy's terms are fixed" in world.replay_verdict(db)


def test_a_kind_swap_under_a_graduation_id_is_refused_both_ways(world):
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    policy = snap.policy.as_dict()
    policy["grants"][GP] = {"id": GP, "kind": "push", "expires_at": policy["grants"][GP]["expires_at"]}
    assert "a graduation policy's terms are fixed" in world.replay_verdict(world.plant({"op": "loosen",
                                                                                       "policy": policy}))
    # and into the kind: an inert grant of another kind turned into a graduation policy under its id
    (world.tmp / "w2").mkdir()
    snap2 = World(world.tmp / "w2")
    db = snap2.plant({"op": "create_grant", "grant": {"id": GP2, "kind": "push",
                                                      "expires_at": utc_stamp(snap2.clock.now + timedelta(days=1))}},
                     db=snap2.db)
    pol = snap2.state(db).policy.as_dict()
    pol["grants"][GP2] = grad(snap2, GP2)
    assert "a graduation policy's terms are fixed" in snap2.replay_verdict(snap2.plant({"op": "loosen",
                                                                                       "policy": pol}, db=db))


# --- the opt-in (laptop, rank 3, /2; no change, one live, names active passkeys) ------------------------------------

def test_the_opt_in_is_a_v2_laptop_link_naming_active_passkeys(world):
    snap = world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    assert world.record(snap.seq)["format"] == V2
    assert "a display-phone key cannot sign this create_grant" in world.refused(
        {"op": "create_grant", "grant": opt_in(world, "pk2", gid="gpo-ba9876543210")}, "phone")
    assert "one graduation act per link" in world.refused(
        {"op": "create_grant", "grant": opt_in(world, "pk2", gid="gpo-ba9876543210")}, "pk")
    assert "at most one graduation opt-in" in world.refused(
        {"op": "create_grant", "grant": opt_in(world, "pk2", gid="gpo-ba9876543210")})


@pytest.mark.parametrize("names, why", [(("laptop",), "not an active passkey key"),
                                        (("phone",), "not an active passkey key")])
def test_the_opt_in_names_passkeys_only(world, names, why):
    assert why in world.refused({"op": "create_grant", "grant": opt_in(world, *names)})


def test_the_opt_in_names_a_revoked_passkey_never(world):
    world.link({"op": "revoke_key", "fingerprint": world.fp("pk2")})
    assert "not an active passkey key" in world.refused({"op": "create_grant", "grant": opt_in(world, "pk2")})


def test_the_opt_in_has_no_change(world):
    snap = world.link({"op": "create_grant", "grant": opt_in(world, "pk", "pk2")})
    policy = snap.policy.as_dict()
    policy["grants"][OPT] = opt_in(world, "pk")                         # narrower: still refused
    assert "the graduation opt-in has no change: revoke it and create another" in world.refused(
        {"op": "loosen", "policy": policy})
    assert "the graduation opt-in has no change" in world.replay_verdict(
        world.plant({"op": "loosen", "policy": policy}, fmt=V2))
    assert "never renewed" in world.refused({"op": "renew", "grant_id": OPT, "expires_at": utc_stamp(T0),
                                             "signed_at": utc_stamp(T0)})


# --- §7 row 4: a passkey creates under the opt-in (rank 2 by the opt-in, /2) -----------------------------------------

def test_a_passkey_the_opt_in_names_creates_one_graduation_policy(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    snap = world.link({"op": "create_grant", "grant": grad(world)}, "pk")
    assert world.record(snap.seq)["format"] == V3      # a guarded policy is /3, whoever signs it
    assert auth.graduation_links(snap, GP) == (world.fp("pk"), world.fp("pk"))


def test_a_passkey_create_is_refused_without_the_opt_in_naming_it(world):
    assert "the live graduation opt-in names it" in world.refused({"op": "create_grant", "grant": grad(world)}, "pk")
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    assert "the live graduation opt-in names it" in world.refused({"op": "create_grant", "grant": grad(world)}, "pk2")
    assert "one graduation act per link" in world.refused(
        {"op": "create_grant", "grant": consent(world)}, "pk")


def test_a_revoked_opt_in_admits_nothing_and_a_revoked_passkey_signs_nothing(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk", "pk2")})
    world.link({"op": "revoke_key", "fingerprint": world.fp("pk2")})
    assert "was revoked" in world.refused({"op": "create_grant", "grant": grad(world)}, "pk2")
    world.link({"op": "revoke_grant", "grant_id": OPT}, "pk")
    assert "the live graduation opt-in names it" in world.refused({"op": "create_grant", "grant": grad(world)}, "pk")


def test_a_display_phone_ssh_key_gets_no_opt_in_rank(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    assert "a display-phone key cannot sign this create_grant" in world.refused(
        {"op": "create_grant", "grant": grad(world)}, "phone")


# --- §7 row 1: revoke (any enrolled key, rank 1, /1) ----------------------------------------------------------------

@pytest.mark.parametrize("signer", ["pk", "phone", "laptop"])
def test_any_enrolled_key_revokes_a_graduation_policy_and_the_opt_in_in_v1_links(world, signer):
    world.link({"op": "create_grant", "grant": grad(world)})
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    a = world.link({"op": "revoke_grant", "grant_id": GP}, signer)
    b = world.link({"op": "revoke_grant", "grant_id": OPT}, signer)
    assert world.record(a.seq)["format"] == world.record(b.seq)["format"] == V1
    assert GP not in b.policy.grants and OPT not in b.policy.grants
    assert auth.graduation_links(b, GP) is None and auth.graduation_incarnation(b, GP) is None


def test_a_passkey_still_never_revokes_an_egress_grant(world):
    egress = {"id": "eg-1", "entity": "e1", "remote_url": "git@github.com:x/y.git", "deploy_key_sha256": "ab" * 32,
              "refspecs": ["refs/heads/main"], "verbs": ["push"],
              "expires_at": utc_stamp(world.clock.now + timedelta(days=1))}
    world.link({"op": "create_grant", "grant": egress})
    assert "a passkey key revokes a consent grant or an inert one only, never grant 'eg-1'" in world.refused(
        {"op": "revoke_grant", "grant_id": "eg-1"}, "pk")


def test_a_recreated_policy_is_a_new_incarnation(world):
    first = world.link({"op": "create_grant", "grant": grad(world)})
    world.link({"op": "revoke_grant", "grant_id": GP})
    again = world.link({"op": "create_grant", "grant": grad(world)})
    assert auth.graduation_incarnation(again, GP) != auth.graduation_incarnation(first, GP)


# --- the allow-list: every other passkey act, a multi-op link included, is refused ----------------------------------

def test_every_other_passkey_act_is_refused(world):
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    bundled = snap.policy.as_dict()                                      # one link, two acts: a renew and a create
    bundled["grants"][GP] = {**bundled["grants"][GP],
                             "expires_at": utc_stamp(world.clock.now + timedelta(days=29))}
    bundled["grants"][GP2] = grad(world, GP2)
    bundled["grants"][OPT] = opt_in(world, "pk")
    for op in ({"op": "loosen", "policy": bundled},
               {"op": "raise", "verb": "spore_touch", "tier": "T3"},
               {"op": "timeouts", "timeouts": {"x": 1}},
               {"op": "revoke_key", "fingerprint": world.fp("pk2")}):
        assert "one graduation act per link" in world.refused(op, "pk")


# --- the format: one form per link, held by the replay ---------------------------------------------------------------

def test_a_link_in_the_wrong_format_is_refused_by_the_replay(world):
    world.link({"op": "create_grant", "grant": grad(world)})
    # a passkey renew written /1, a laptop create of a guarded policy written /2 or /1, and of one without a guard
    # (a link from before the guard) written /2
    assert "written levain-authority/2" in world.replay_verdict(world.plant(renew(world), "pk", V1))
    for fmt in (V1, V2):
        assert "written levain-authority/3" in world.replay_verdict(
            world.plant({"op": "create_grant", "grant": grad(world, GP2)}, "laptop", fmt))
    assert "written levain-authority/1" in world.replay_verdict(
        world.plant({"op": "create_grant", "grant": grad(world, GP2, guard=NO_GUARD)}, "laptop", V2))
    assert "written levain-authority/1" in world.replay_verdict(
        world.plant({"op": "revoke_grant", "grant_id": GP}, "pk", V2))
    assert world.replay_verdict(world.plant(renew(world), "pk", V2)).startswith("ACCEPTED")


def test_a_v1_inert_path_loosen_of_a_graduation_policy_is_refused_by_this_replay(world):
    """C Task 3: an older levain reads a graduation grant as inert, and its inert change rule takes a terms change as
    a loosen. Such a link (laptop-signed, /1, built by hand: the older levain's own append refuses to write a change
    to a kind it does not know) is refused here."""
    snap = world.link({"op": "create_grant", "grant": grad(world)})
    policy = snap.policy.as_dict()
    policy["grants"][GP]["actions"] = {"doc.append": dict(LOW), "deliver": dict(LOW)}
    policy["raise_to"] = {"spore_touch": "T3"}
    assert "a graduation policy's terms are fixed" in world.replay_verdict(world.plant({"op": "loosen",
                                                                                       "policy": policy}))


# --- the older binary ------------------------------------------------------------------------------------------------

_OLDER_SCRIPT = '''
import json, sqlite3, sys
import levain.authority as a
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
out = {"file": a.__file__, "has_v2": hasattr(a, "FORMAT_V2")}
try:
    out["seq"] = a.snapshot(conn, cache=a.ReplayCache()).seq
except a.AuthorityError as exc:
    out["refused"] = str(exc)
print(json.dumps(out))
'''


def older_replay(tmp: Path, db: Path) -> dict:
    script = tmp / "older_replay.py"
    script.write_text(_OLDER_SCRIPT)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = OLDER
    got = subprocess.run([sys.executable, str(script), str(db)], capture_output=True, text=True, env=env,
                         cwd=str(tmp), timeout=120)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    assert out["file"].startswith(str(Path(OLDER).resolve())) and not out["has_v2"], out
    return out


@older
def test_the_older_levain_replays_a_v1_only_log_whole_and_stops_at_the_first_v2_link(world):
    # a policy and its renew as a levain from before the guard signed them (this levain signs neither: a /3 create,
    # and no renew of a guard-less policy), planted into the log
    world.plant({"op": "create_grant", "grant": grad(world, guard=NO_GUARD)}, db=world.db)
    world.plant(renew(world), db=world.db)
    world.link({"op": "create_grant", "grant": consent(world)})
    head = world.link({"op": "revoke_grant", "grant_id": GP}, "pk")
    assert older_replay(world.tmp, world.db)["seq"] == head.seq
    v2 = world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    world.link({"op": "create_grant", "grant": grad(world, GP2)}, "pk")
    out = older_replay(world.tmp, world.db)
    assert "seq" not in out and out["refused"] == (f"authority log link {v2.seq}: the record's format is not "
                                                   "levain-authority/1")


@older
def test_the_older_levain_takes_the_planted_inert_loosen_this_replay_refuses(world):
    world.plant({"op": "create_grant", "grant": grad(world, guard=NO_GUARD)}, db=world.db)    # from before the guard
    snap = world.state()
    policy = snap.policy.as_dict()
    policy["grants"][GP]["actions"] = {"doc.append": dict(LOW), "deliver": dict(LOW)}
    policy["raise_to"] = {"spore_touch": "T3"}
    db = world.plant({"op": "loosen", "policy": policy})
    assert older_replay(world.tmp, db)["seq"] == snap.seq + 1
    assert "a graduation policy's terms are fixed" in world.replay_verdict(db)


# --- S1 L3 r1 fixes (1025+16 S1_RULINGS_R1) --------------------------------------------------------------------------

@pytest.mark.parametrize("lapsed", [True, False])
def test_a_graduation_expiry_moves_only_by_its_renew_never_inside_a_loosen(world, lapsed):
    """Ruling 1 (codex MED): a loosen that extends a lapsed GP and adds GP2 computes to LOOSEN (the add), so the renew's
    lapse rule never ran and the lapsed policy came back live under its old incarnation. Refused at append and at
    replay, lapsed or live."""
    snap = world.link({"op": "create_grant", "grant": grad(world, days=1 if lapsed else 10)})
    if lapsed:
        world.clock.now += timedelta(days=2)
    policy = snap.policy.as_dict()
    policy["grants"][GP] = {**policy["grants"][GP], "expires_at": utc_stamp(world.clock.now + timedelta(days=20))}
    policy["grants"][GP2] = grad(world, GP2)
    why = "a graduation policy's expiry moves only by its own renew link"
    assert why in world.refused({"op": "loosen", "policy": policy})
    assert why in world.replay_verdict(world.plant({"op": "loosen", "policy": policy}))


def test_a_display_phone_key_signs_no_loosen_carrying_a_renew_and_an_opt_in(world):
    """complement LOW 4: the renew and the opt-in together compute to LOOSEN, which needs rank 3."""
    snap = world.link({"op": "create_grant", "grant": grad(world, days=10)})
    policy = snap.policy.as_dict()
    policy["grants"][GP] = {**policy["grants"][GP], "expires_at": utc_stamp(world.clock.now + timedelta(days=20))}
    policy["grants"][OPT] = opt_in(world, "pk")
    assert "a display-phone key cannot sign this loosen" in world.refused({"op": "loosen", "policy": policy}, "phone")


def test_the_opt_in_is_never_renewed_by_either_gate(world):
    """Ruling 2 (deepseek MED 1+2): the append-only gate read the opt-in's expires_at, which it has none of
    (KeyError). One rule says which kind is never renewed, and both gates call it."""
    snap = world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    rec = renew(world, OPT)
    assert "has no expiry, so it is never renewed" in world.refused(rec)
    with pytest.raises(auth.AuthorityError, match="has no expiry, so it is never renewed"):
        auth._append_only_checks(snap, rec, world.clock.now + timedelta(minutes=1))


def test_the_laptop_cli_and_a_passkey_revoke_a_graduation_policy_and_the_opt_in(world):
    """Ruling 3 (complement MED): the revoke path the CLI and the phone share took consent and inert grants only, so
    the passkey revoke of a graduation policy the log accepts was unreachable. An egress grant stays refused."""
    from levain.consent.grants import append_revoke, prepare_revoke, revoke_grant

    CockpitDB(world.root).set_install_class("reported")
    world.link({"op": "create_grant", "grant": grad(world)})
    world.link({"op": "create_grant", "grant": grad(world, GP2)})
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    fp, sign = world.keys["laptop"]
    revoke_grant(world.root, GP, signer_fp=fp, sign=sign, clock=world.clock)
    for gid in (GP2, OPT):
        link = prepare_revoke(world.root, gid, signer_fp=world.fp("pk"), clock=world.clock)
        append_revoke(world.root, link, world.keys["pk"][1](link.message), clock=world.clock)
    assert not {GP, GP2, OPT} & set(world.state().policy.grants)
    egress = {"id": "eg-1", "entity": "e1", "remote_url": "git@github.com:x/y.git", "deploy_key_sha256": "ab" * 32,
              "refspecs": ["refs/heads/main"], "verbs": ["push"],
              "expires_at": utc_stamp(world.clock.now + timedelta(days=1))}
    world.link({"op": "create_grant", "grant": egress})
    from levain.cockpit.keys import KeyError_
    # the refusal keeps its prefix (test_grant_phone.py: the phone maps it to not_found)
    for gid in ("eg-1", GP):
        with pytest.raises(KeyError_) as exc:
            prepare_revoke(world.root, gid, signer_fp=world.fp("pk"), clock=world.clock)
        assert str(exc.value).startswith("no consent grant")
    with pytest.raises(KeyError_, match="no consent grant"):
        revoke_grant(world.root, "eg-1", signer_fp=fp, sign=sign, clock=world.clock)


def test_prepare_refuses_an_op_that_is_not_a_mapping(world):
    """Ruling 4 (complement LOW 2): a list holding "op" passed the field check and raised AttributeError."""
    conn = sqlite3.connect(world.db, isolation_level=None)
    try:
        with pytest.raises(auth.AuthorityError, match="an op is a mapping"):
            auth.prepare(conn, ["op"], signer_fp=world.fp("laptop"), now=world.clock.now)
    finally:
        conn.close()


def test_graduation_links_needs_one_signer_per_link(world):
    """Ruling 5 (complement LOW 3): a snapshot whose signers do not line up with its links answers None, never the
    signer of another link."""
    import dataclasses

    world.link({"op": "create_grant", "grant": grad(world)})
    snap = world.link({"op": "create_grant", "grant": consent(world)})
    assert auth.graduation_links(snap, GP) == (world.fp("laptop"), world.fp("laptop"))
    assert auth.graduation_links(dataclasses.replace(snap, link_signers=snap.link_signers[1:]), GP) is None


def test_a_grant_change_without_an_expiry_is_an_authority_error():
    """Ruling 6 (deepseek LOW 3): an inert grant with no expires_at raised TypeError from the comparison."""
    with_exp = {"id": "x-1", "kind": "push", "expires_at": utc_stamp(T0)}
    without = {"id": "x-1", "kind": "push"}
    for o, n in ((with_exp, without), (without, with_exp)):
        with pytest.raises(auth.AuthorityError, match="carries no expires_at"):
            auth._grant_change(o, n)


def test_a_passkey_act_on_no_grant_names_the_passkey_rule_and_the_missing_grant(world):
    """Ruling 6 (deepseek LOW 4)."""
    for op in (renew(world, "gp-ffffffffffff"), {"op": "revoke_grant", "grant_id": "gp-ffffffffffff"}):
        why = world.refused(op, "pk")
        assert "a passkey key" in why and "no grant 'gp-ffffffffffff'" in why


def test_a_graduation_policy_requires_its_store_id_and_the_opt_in_carries_none(world):
    """Ruling 7 (S2 obs 2): coverage binds a policy to its store, so the log never takes one without a store_id, at
    append or at replay; the opt-in's field set has no store_id either."""
    bare = grad(world)
    del bare["store_id"]
    assert "must carry exactly" in world.refused({"op": "create_grant", "grant": bare})
    assert "must carry exactly" in world.replay_verdict(world.plant({"op": "create_grant", "grant": bare}))
    assert "store_id must be store-" in world.refused({"op": "create_grant", "grant": grad(world, store_id=None)})
    assert "must carry exactly" in world.refused({"op": "create_grant",
                                                  "grant": {**opt_in(world, "pk"), "store_id": STORE}})
