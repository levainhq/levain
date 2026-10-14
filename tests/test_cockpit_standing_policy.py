"""Trust-tiering S4a (DESIGN §4.1, §4.2): the standing policy is the replay of the signed policy links,
read by every verb from the request's one snapshot, and the authority head is hashed into every etag.

(a)-(c) use only what 56876a3 already had (the broker rig, a signed link, dispatch, the render), so they
run unchanged on the base, where they FAIL: a signed raise in the log changed nothing a verb showed or
enforced (``verbs.POLICY`` was a frozen module global) and no etag moved (``POLICY_REVISION`` was a
constant). The rest import what S4a adds, inside each test.

(d) is held in ``test_authority.py`` (``test_loosen_by_phone_refused``: an under-tier loosen) and
``test_cockpit_authority_s3a2.py`` (``test_a_directly_inserted_unsigned_link_refuses_the_whole_log``);
a pending issued before any append ending STALE is ``test_an_append_stales_a_waiting_signed_pending``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

import levain
import levain.authority as authority
from levain.authority import anchor_path
from levain.cockpit.verbs import dispatch
from levain.writes import EditError
from tests.cockpit_authority_rig import link
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import Key, home, keydir, spores  # noqa: F401 - fixtures
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture

LEVAIN = Path(levain.__file__).parent
RAISE = {"op": "raise", "verb": "spore_touch", "tier": "T2"}


@pytest.fixture
def stack(rig_broker, keydir):  # noqa: F811 - fixture
    """The kernel's cockpit over a real install, served authority by an ACTIVE reported broker as
    `levain serve` wires it, its log holding a laptop-presence and a display-phone key."""
    rig, b, _db = rig_broker
    rig.ck.attach_authority(b.capture)
    laptop = Key(keydir, "laptop").enroll(b, "laptop-presence")
    phone = Key(keydir, "phone").enroll(b, "display-phone")
    return rig, b, laptop, phone


def _touch(rig: Any) -> dict[str, Any]:
    """The seed row's rendered ``spore_touch`` action, as the tray panel shows it."""
    rid = f"spore:{rig.ids['seed']}"
    return next(a for r in rig.ck.panel("tray", credential_class="token")["rows"] if r["id"] == rid
                for a in r["actions"] if a["verb"] == "spore_touch")


def _fire(rig: Any, b: Any = None, **env: Any) -> dict[str, Any]:
    row = rig.row("tray", "seed")
    req = {"verb": "spore_touch", "panel_id": "tray", "params": {}, "row_id": row["id"],
           "row_version": row["version"], **env}
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req, credential=CRED,
                    install_class=INSTALL_CLASS, route="cockpit", broker=b)


# --- (a) a signed raise moves a verb's displayed and enforced tier -------------------------------------

def test_a_signed_raise_moves_the_displayed_and_the_enforced_tier(stack):
    rig, b, _laptop, phone = stack
    assert rig.tier("spore_touch", "tray", "seed") == "C1"
    assert _touch(rig)["tier"] == "C1"
    assert rig.ck.manifest(CRED)["verbs"]["spore_touch"]["tier"] == "C1"
    link(b.db, RAISE, phone)                      # T2's op, signed by a display-phone key
    assert rig.tier("spore_touch", "tray", "seed") == "T2"                        # enforced (dispatch)
    shown = _touch(rig)
    assert (shown["tier"], shown["gesture"]) == ("T2", "direct-reported")         # displayed (the row)
    assert rig.ck.manifest(CRED)["verbs"]["spore_touch"]["tier"] == "T2"          # displayed (the manifest)


# --- (b) the manifest and verb-view etags move on that append -------------------------------------------

def test_a_signed_raise_moves_the_etag_of_a_panel_that_carries_no_labels(stack):
    """The State line carries no labels, so before S4a nothing in its etag read the log (a labelled
    panel's etag already moved on any append, through the labeller's ``label_rev``): the verb view's
    revision is now the authority head, so a raise of its verb is a new validator with the value
    unchanged."""
    rig, b, _laptop, phone = stack
    p1 = rig.ck.panel("state", credential_class="token")
    assert rig.ck.panel("state", credential_class="token")["etag"] == p1["etag"]   # stable with no append
    link(b.db, {"op": "raise", "verb": "operator_state", "tier": "T2"}, phone)
    p2 = rig.ck.panel("state", credential_class="token")
    assert p2["value"] == p1["value"]
    assert p2["etag"] != p1["etag"]
    assert [a["tier"] for a in p1["actions"]] == ["C1"] and [a["tier"] for a in p2["actions"]] == ["T2"]


def test_the_manifest_etag_moves_with_the_authority_head_and_nothing_else(tmp_path):
    """A cockpit with no labeller, so no panel etag reads a journal revision: only the authority head
    (DESIGN §4.2: the one counter) can move the manifest's etag here. The same rows, a new head with a
    raise: a new etag."""
    from levain.authority import AuthoritySnapshot, genesis_policy
    from levain.cockpit.authority import RequestAuthority
    from tests.test_cockpit_k2a import Rig

    rig = Rig(tmp_path)
    policy = type(genesis_policy()).from_dict({"raise_to": {"spore_touch": "T2"}, "timeouts": {}, "grants": {}})
    heads = {"now": AuthoritySnapshot("store0001", 1, "a" * 64, {}, genesis_policy(), ("a" * 64,))}
    rig.ck.attach_authority(lambda now: RequestAuthority(now, authority=heads["now"]))
    m1 = rig.ck.manifest(CRED)
    assert rig.ck.manifest(CRED)["etag"] == m1["etag"]
    heads["now"] = AuthoritySnapshot("store0001", 2, "b" * 64, {}, policy, ("a" * 64, "b" * 64))
    m2 = rig.ck.manifest(CRED)
    assert m2["etag"] != m1["etag"]
    assert m2["verbs"]["spore_touch"]["tier"] == "T2"


# --- (c) a fire made against the pre-raise render is refused ----------------------------------------------

def test_a_fire_carrying_the_pre_raise_preview_is_refused_and_fires_nothing(stack):
    """Only a fire that carries the preview's ``tier_seen``/``gesture_seen`` is refused ``tier_moved``.
    An ordinary rendered C1 action carries neither, so one clicked after a raise is NOT refused: it
    becomes a WAITING pending at the new tier, or ``not_built`` for a verb with no adapter (the next
    test; codex 4, L3 r1, a known edge in the CHANGELOG)."""
    rig, b, _laptop, phone = stack
    before = rig.spore("seed")
    preview = rig.post("spore_touch", "tray", "seed", dry_run=True)
    assert (preview["tier"], preview["gesture"]) == ("C1", "direct")
    link(b.db, RAISE, phone)
    with pytest.raises(EditError) as ei:
        _fire(rig, b, intent="reported", tier_seen=preview["tier"], gesture_seen=preview["gesture"])
    assert ei.value.code == "tier_moved"
    assert rig.spore("seed") == before


def test_after_a_raise_a_pre_raise_c1_action_becomes_a_pending_at_the_new_tier(stack):
    """What really happens to an ordinary C1 action rendered BEFORE a raise and clicked after it (codex
    4): it is not refused; it becomes a WAITING pending at the new tier, the safe direction, and the row
    is untouched until it is signed. A raised verb the broker has no adapter for (``spore_touch``) answers
    ``not_built``: it never fires direct either."""
    rig, b, _laptop, phone = stack
    params = {"surface_at": "2026-10-20"}
    assert rig.tier("spore_surface_at", "tray", "seed", params) == "C1"
    rendered = next(a for r in rig.ck.panel("tray", credential_class="token")["rows"]
                    if r["id"] == f"spore:{rig.ids['seed']}" for a in r["actions"] if a["verb"] == "spore_surface_at")
    assert rendered["tier"] == "C1" and "tier_seen" not in rendered    # the click carries no seen tier
    before = rig.spore("seed")
    link(b.db, {"op": "raise", "verb": "spore_surface_at", "tier": "T2"}, phone)
    out = _fire(rig, b, verb="spore_surface_at", params=params)       # no intent: the sign path
    assert out.get("pending_id") and out.get("state") == "WAITING", out
    assert rig.spore("seed") == before
    link(b.db, RAISE, phone)
    with pytest.raises(EditError, match="no EffectStore adapter"):
        _fire(rig, b)
    assert rig.spore("seed") == before


# --- what S4a adds ---------------------------------------------------------------------------------------

def test_a_refused_log_tiers_every_verb_t3_and_fires_no_c1(stack):
    """A log that was asked and refused may hold a raise this request cannot see: every verb is T3
    (``UNKNOWN_POLICY``), never the shipped default; the log restored, the replay's policy is back."""
    rig, b, _laptop, _phone = stack
    anchor = anchor_path(b.data_root)
    good = anchor.read_bytes()
    at = authority.read_anchor(anchor)
    anchor.write_text(json.dumps({"format": authority.ANCHOR_FORMAT, "store_id": at.store_id,
                                  "seq": at.seq + 1, "head_hash": "0" * 64}))     # the log is behind it
    before = rig.spore("seed")
    assert rig.tier("spore_touch", "tray", "seed") == "T3"
    assert _touch(rig)["tier"] == "T3"
    with pytest.raises(EditError):
        _fire(rig)
    assert rig.spore("seed") == before
    anchor.write_bytes(good)
    assert rig.tier("spore_touch", "tray", "seed") == "C1"


def test_the_policy_and_revision_of_each_capture_kind():
    from datetime import datetime, timezone

    from levain.cockpit.authority import UNKNOWN_POLICY, RequestAuthority

    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    genesis = authority.genesis_policy()
    assert RequestAuthority(now).policy == genesis                        # no authority attached
    assert RequestAuthority(now, log_empty=True, authority_error="not initialised").policy == genesis
    inactive = RequestAuthority(now, authority_error="the broker is not active", no_log=True)
    # a broker that is not ACTIVE cannot read a log that may hold a raise (FIX1 H1): never genesis
    assert inactive.policy is UNKNOWN_POLICY and inactive.revision == ["unknown"] and inactive.fence is None
    never = RequestAuthority(now, authority_error="the broker is not active", no_log=True, log_empty=True)
    assert never.policy == genesis and never.revision == ["genesis"]       # no trace of a log on the root
    refused = RequestAuthority(now, authority_error="the authority log is refused: x")
    assert refused.policy is UNKNOWN_POLICY and refused.revision == ["unknown"]
    assert UNKNOWN_POLICY.apply("anything", "C1") == "T3"


def test_a_policy_cannot_lower_a_tier_whatever_it_answers():
    from levain.cockpit.verbs import _raised

    class Lowers:
        def apply(self, verb: str, tier: str) -> str:
            return "C1"

    assert _raised(Lowers(), "v", "T2") == "T2"

    class Garbage:
        def apply(self, verb: str, tier: str) -> str:
            return "T9"

    with pytest.raises(ValueError):
        _raised(Garbage(), "v", "C1")


def test_the_cli_raise_signs_through_the_log_and_the_classifier_refuses_a_presence_key(tmp_path):
    from datetime import datetime, timezone

    from levain.cockpit.firejournal import CockpitDB
    from levain.cockpit.keys import KeyError_, policy_raise
    from tests.cockpit_authority_rig import RealKey, enrol

    now = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)     # before the CLI's own clock
    root = tmp_path / "cockpit"
    root.mkdir()
    db = CockpitDB(root)
    db.set_install_class("reported")
    laptop, phone, presence = (RealKey(tmp_path, n) for n in ("laptop", "phone", "presence"))
    enrol(db, laptop, "laptop-presence", now=now)
    enrol(db, phone, "display-phone", by=laptop, now=now)
    enrol(db, presence, "presence", by=laptop, now=now)

    def by(k: RealKey) -> dict[str, Any]:
        return {"signer_fp": k.fingerprint, "sign": k.sign_link, "clock": lambda: now}

    with pytest.raises(KeyError_, match="presence key cannot sign this raise"):
        policy_raise(root, "spore_touch", "T2", **by(presence))
    assert policy_raise(root, "spore_touch", "T2", **by(phone))[:2] == ("T2", 4)
    with pytest.raises(KeyError_, match="a raise link computes to a loosen change"):
        policy_raise(root, "spore_touch", "C1", **by(laptop))
    with pytest.raises(KeyError_, match="changes nothing"):
        policy_raise(root, "spore_touch", "T2", **by(phone))

    from levain import cli
    from levain.cockpit.keys import keys

    argv = ["cockpit", "policy", "raise", "spore_touch", "T3", "--signing-key", str(phone.path), "--data-root",
            str(root)]
    assert cli.main(argv) == 0
    with db.read() as conn:
        assert authority.snapshot(conn).policy.raise_to["spore_touch"] == "T3"
    assert keys(root, now=now)[0]        # the store still opens
    with pytest.raises(SystemExit):
        cli.main(["cockpit", "policy", "raise", "spore_touch", "C1", "--signing-key", str(phone.path)])


# --- the fence: no module-level policy, no fallback to the shipped default -------------------------------

def test_no_module_level_policy_survives_in_the_cockpit():
    import levain.cockpit.engine as engine
    import levain.cockpit.verbs as verbs

    for mod, names in ((verbs, ("POLICY", "Policy")), (engine, ("POLICY_REVISION",))):
        for name in names:
            assert not hasattr(mod, name), f"{mod.__name__}.{name} is back"


def test_only_the_request_authority_reaches_the_genesis_policy():
    """A ``genesis_policy()`` call anywhere in the cockpit but ``RequestAuthority.policy`` would be a
    fallback to the shipped default that a signed raise cannot reach."""
    hits = []
    for path in sorted((LEVAIN / "cockpit").glob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                if name == "genesis_policy":
                    hits.append((path.name, node.lineno))
    assert [h[0] for h in hits] == ["authority.py"], hits


# --- FIX1 H1: a broker that is not ACTIVE never tiers under the shipped default -------------------------

def test_a_standby_broker_tiers_every_verb_t3_fires_nothing_and_no_genesis_capture_survives(stack):
    """codex 1 + complement 1 (L3 r1, HIGH): a second server on the same data root holds a STANDBY
    broker. It cannot read the log, which here holds a signed raise, so it tiers under UNKNOWN_POLICY
    (every verb T3) and refuses the write, never the shipped default's direct C1. When it turns ACTIVE,
    a capture taken while it stood by is still unknown, and a fresh one reads the raise."""
    from levain.cockpit.authority import UNKNOWN_POLICY
    from levain.cockpit.broker import Broker, Lifecycle
    from tests.test_cockpit_k2b_broker import _wait

    rig, b, _laptop, phone = stack
    params = {"surface_at": "2026-10-20"}
    link(b.db, {"op": "raise", "verb": "spore_surface_at", "tier": "T2"}, phone)
    second = Broker(b.data_root, stores=dict(b.stores), store_paths=list(b.store_paths), install_class="reported",
                    tier_of=b.tier_of, ssh_keygen=b.ssh_keygen, lock_poll=0.05)
    second.start()
    try:
        assert second.state is Lifecycle.STANDBY
        rig.ck.attach_authority(second.capture)
        stood_by = second.capture()
        assert stood_by.policy is UNKNOWN_POLICY and stood_by.revision == ["unknown"]
        before = rig.spore("seed")
        assert rig.tier("spore_surface_at", "tray", "seed", params) == "T3"
        assert rig.ck.manifest(CRED)["verbs"]["spore_surface_at"]["tier"] == "T3"
        with pytest.raises(EditError):
            _fire(rig, second, verb="spore_surface_at", params=params)
        assert rig.spore("seed") == before
        b.stop()
        assert _wait(lambda: second.state is Lifecycle.ACTIVE, 5.0)
        assert stood_by.policy is UNKNOWN_POLICY                          # no genesis capture survives
        assert second.capture().policy.raise_to["spore_surface_at"] == "T2"
        assert rig.tier("spore_surface_at", "tray", "seed", params) == "T2"
    finally:
        second.stop()


# --- FIX1 H2: pending_cancel honours its standing floor, on every path ----------------------------------

@pytest.fixture
def canceller(home, spores, keydir):  # noqa: F811 - fixtures
    from tests.test_cockpit_k2b_broker import _broker

    b = _broker(home, spores)
    try:
        Key(keydir, "laptop").enroll(b, "laptop-presence")
        phone = Key(keydir, "phone").enroll(b, "display-phone")
        yield b, phone
    finally:
        b.stop()


def test_a_raised_pending_cancel_lifts_a_foreign_cancellation_and_the_render(canceller, spores):  # noqa: F811 - fixture
    """codex 2 (L3 r1, HIGH): with ``pending_cancel`` raised to T3, another token's reported T2
    cancellation of a T2 pending is refused (T3 on the token alone), never committed below the floor;
    the decisions row offers the cancel at T3."""
    from levain.cockpit.broker import BrokerError
    from levain.cockpit.verbs import pending_actions
    from tests.test_cockpit_k2b_broker import PHONE, _move

    b, phone = canceller
    link(b.db, {"op": "raise", "verb": "pending_cancel", "tier": "T3"}, phone)
    pid = _move(b, spores)["pending_id"]
    with pytest.raises(BrokerError) as ei:
        b.cancel(pid, credential=PHONE, intent="reported", idempotency_key="c1")
    assert ei.value.code == "refused" and "T3" in str(ei.value)
    assert b.outcome(pid)["state"] == "WAITING"
    signed = b.cancel(pid, credential=PHONE, idempotency_key="c2")
    assert b.db.load(signed["pending_id"]).record["tier"] == "T3"
    row = {"id": f"pend:{pid}", "facets": {"pending_tier": "T2"}}
    policy = b.capture().policy
    assert [a["tier"] for a in pending_actions(row, "token", "reported", policy=policy)] == ["T3"]


def test_a_raised_pending_cancel_lifts_an_own_cancellation_off_c1_and_recompute_agrees(canceller, spores):  # noqa: F811 - fixture
    """The canceller's own pending: C1 at genesis, so with ``pending_cancel`` raised to T2 it is a T2
    pending (the target stays WAITING), and a reported one of a T3 target commits: the decide's
    recompute reproduces the floor over C1, never the target's T3 (which would end it STALE)."""
    b, phone = canceller
    from tests.test_cockpit_k2b_broker import BROWSER, _move

    link(b.db, {"op": "raise", "verb": "pending_cancel", "tier": "T2"}, phone)
    pid = _move(b, spores)["pending_id"]
    out = b.cancel(pid, credential=BROWSER)
    assert out["pending_id"] != pid and out["state"] == "WAITING"
    assert b.outcome(pid)["state"] == "WAITING"
    t3 = _move(b, spores, "k.seed2", tier="T3")["pending_id"]
    done = b.cancel(t3, credential=BROWSER, intent="reported", idempotency_key="c3")
    assert done["state"] == "COMMITTED", done
    assert b.outcome(t3)["state"] == "CANCELLED"


def test_under_a_refused_log_an_own_cancellation_is_refused_and_cancels_nothing(canceller, spores):  # noqa: F811 - fixture
    """codex 2: the own-cancel branch mutated the pending as C1 without capturing authority. Under a
    refused log the policy is unknown, so the cancellation is T3 and refused for want of a fence."""
    from levain.cockpit.broker import BrokerError
    from tests.test_cockpit_k2b_broker import BROWSER, _move

    b, _phone = canceller
    pid = _move(b, spores)["pending_id"]
    anchor = anchor_path(b.data_root)
    at = authority.read_anchor(anchor)
    anchor.write_text(json.dumps({"format": authority.ANCHOR_FORMAT, "store_id": at.store_id,
                                  "seq": at.seq + 1, "head_hash": "0" * 64}))
    with pytest.raises(BrokerError) as ei:
        b.cancel(pid, credential=BROWSER)
    assert ei.value.code == "authority_unavailable"
    assert b.db.load(pid).state == "WAITING"


def test_the_cli_raise_refuses_a_verb_nothing_honours(tmp_path):
    """complement 2/3 (L3 r1, LOW): a misspelt verb, or an internal verb with no floor, is refused
    before anything is signed; ``pending_cancel`` (an internal verb that honours a floor) is accepted."""
    from levain.cockpit.keys import KeyError_, _raisable

    for verb, why in (("spore_tuch", "not a cockpit verb"), ("token_rotate", "honours no standing-policy floor")):
        with pytest.raises(KeyError_, match=why):
            _raisable(verb)
    _raisable("pending_cancel")
    _raisable("spore_touch")


def test_a_downstream_verb_raises_against_the_deployments_registry_or_by_declaration(tmp_path):
    """codex (L3 r2, MED): an embedder's ``deploy`` stayed at its base T2 because the raise checked an
    empty kernel registry. An embedder raises it against the registry it serves; the standalone CLI,
    which cannot load that registry, raises it only by declaring it ``--downstream``. Either way the
    replayed policy lifts the downstream verb's tier to T3."""
    from datetime import date, datetime, timezone

    from levain import cli
    from levain.cockpit.firejournal import CockpitDB
    from levain.cockpit.keys import KeyError_, _raisable, policy_raise
    from levain.cockpit.verbs import VerbRegistry
    from levain.kernel import ActionVerb
    from tests.cockpit_authority_rig import RealKey, enrol

    now = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
    root = tmp_path / "cockpit"
    root.mkdir()
    db = CockpitDB(root)
    db.set_install_class("reported")
    laptop, phone = (RealKey(tmp_path, n) for n in ("laptop", "phone"))
    enrol(db, laptop, "laptop-presence", now=now)
    enrol(db, phone, "display-phone", by=laptop, now=now)
    served = VerbRegistry({"deploy": ActionVerb(handler=lambda p: {}, label="deploy"),
                           "publish": ActionVerb(handler=lambda p: {}, label="publish")})
    by_phone = {"signer_fp": phone.fingerprint, "sign": phone.sign_link, "clock": lambda: now}

    # the bare kernel check still refuses it, before anything is signed
    with pytest.raises(KeyError_, match="raised with --downstream"):
        policy_raise(root, "deploy", "T3", **by_phone)
    # against the registry the deployment serves: accepted, and the tier it renders is lifted
    assert policy_raise(root, "deploy", "T3", registry=served, **by_phone)[0] == "T3"
    with db.read() as conn:
        policy = authority.snapshot(conn).policy
    spec = served.get("deploy")
    assert spec is not None and spec.rendered_tier(None, "", date(2026, 10, 13), policy=policy) == "T3"
    with pytest.raises(KeyError_, match="not a verb this deployment registers"):
        policy_raise(root, "deplyo", "T3", registry=served, **by_phone)

    # the standalone CLI: refused without the declaration, signed with it
    base = ["cockpit", "policy", "raise", "publish", "T3", "--signing-key", str(phone.path), "--data-root",
            str(root)]
    assert cli.main(base) == 1
    assert cli.main([*base, "--downstream"]) == 0
    with db.read() as conn:
        assert authority.snapshot(conn).policy.raise_to["publish"] == "T3"

    # what a declaration never reaches: a kernel or internal verb, a broker head, a near spelling of a
    # kernel verb, a name no registry takes, or a declaration beside a registry
    for verb, why in (("spore_touch", "not a downstream verb"), ("pending_cancel", "not a downstream verb"),
                      ("harness_approve", "not a downstream verb"), ("token_rotate", "honours no standing"),
                      ("spore_tuch", "near spelling of the kernel verb 'spore_touch'"),
                      ("a:b", "can register under")):
        with pytest.raises(KeyError_, match=why):
            _raisable(verb, downstream=True)
    with pytest.raises(KeyError_, match="not both"):
        _raisable("deploy", registry=served, downstream=True)
    with pytest.raises(KeyError_, match="honours no standing"):
        _raisable("token_rotate", registry=served)


def test_an_inactive_broker_keeps_the_shipped_tiers_only_where_no_log_was_ever_kept(home, spores):  # noqa: F811 - fixture
    """The one inactive case that is positively empty: no journal, no WAL, no anchor on the data root (by
    ``lstat``, no opener). Any trace of a log, and the policy is unknown."""
    from levain.cockpit.authority import UNKNOWN_POLICY
    from tests.test_cockpit_k2b_broker import _broker

    b = _broker(home, spores, install_class=None)
    try:
        assert not b.enabled
        fresh = b.capture()
        assert fresh.policy == authority.genesis_policy() and fresh.revision == ["genesis"]
        anchor = anchor_path(b.data_root)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_text("{}")
        traced = b.capture()
        assert traced.policy is UNKNOWN_POLICY and traced.revision == ["unknown"]
    finally:
        b.stop()
