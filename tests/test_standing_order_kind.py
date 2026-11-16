"""The ``standing-order`` grant kind in the authority log (night desk ND-2, board levain#9). Real logs: ssh-keygen keys and
WebAuthn stand-ins through the replay (the ``World`` of the graduation kind's tests). Append rules are in force here
(``today=True``): the kind is new, so no earlier levain wrote one."""
from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from types import MappingProxyType
from unittest import mock

import pytest

import levain.authority as auth
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

WORDS = "If taoserv is down overnight, look at it and bring apache back; wake me for anything else.\nNo mysql."


def order(world: World, n: int = 1, days: int = 14, **over) -> dict:
    g = {"id": f"so-{n:012x}", "kind": "standing-order", "words": WORDS,
         "words_sha256": hashlib.sha256(WORDS.encode()).hexdigest(),
         "restatement": {"will": ["diagnose read-only", "restart apache2 or php8.3-fpm, budgeted"],
                         "will_not": ["touch mysql", "act on anything outside this order"]},
         "effect_lines": [{"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2", "php8.3-fpm"]},
                           "per_incident": 1, "per_night": 2}],
         "judgement_lines": ["diagnose: read the sites, service status, the error log"],
         "kills": [{"when": "signs of intrusion", "then": "stop_effects"}],
         "expires_at": utc_stamp(world.clock.now + timedelta(days=days))}
    g.update(over)
    return g


def create(world: World, g: dict, signer: str = "laptop"):
    return world.link({"op": "create_grant", "grant": g}, signer, today=True)


def refused(world: World, g: dict, signer: str = "laptop") -> str:
    return world.refused({"op": "create_grant", "grant": g}, signer, today=True)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def test_an_order_is_signed_replays_as_its_kind_and_is_neither_consent_nor_inert(world):
    g = order(world)
    create(world, g)
    held = world.state().policy.grants[g["id"]]
    assert held["kind"] == auth.STANDING_ORDER_KIND == "standing-order"
    assert auth.standing_order(held) and not auth.consent_grant(held) and not auth.inert_grant(held)


def test_a_verb_outside_the_closed_list_is_refused_at_append(world):
    line = {"verb": "run_shell", "args": {"cmd": ["ls"]}, "per_incident": 1, "per_night": 1}
    assert "run_shell" in refused(world, order(world, effect_lines=[line]))


@pytest.mark.parametrize("line,said", [
    ({"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["mysql"]}, "per_incident": 1, "per_night": 2},
     "outside apache2"),
    ({"verb": "restart_service", "args": {"host": ["taoserv"]}, "per_incident": 1, "per_night": 2}, "takes exactly"),
    ({"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2"], "cmd": ["x"]},
      "per_incident": 1, "per_night": 2}, "takes exactly"),
    ({"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2"]}, "per_incident": 0,
      "per_night": 2}, "budgets"),
    ({"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2"]}, "per_incident": 3,
      "per_night": 2}, "budgets"),
    ({"verb": "send_ack", "args": {"voice": ["somebody"], "known_from": ["a@b.com"]}, "per_incident": 1,
      "per_night": 1}, "outside stand_in"),
    ({"verb": "send_ack", "args": {"voice": ["flow"], "known_from": ["not an address"]}, "per_incident": 1,
      "per_night": 1}, "lowercase address"),
])
def test_a_malformed_effect_line_is_refused_at_append(world, line, said):
    assert said in refused(world, order(world, effect_lines=[line]))


def test_a_valid_send_ack_line_is_taken(world):
    line = {"verb": "send_ack", "args": {"voice": ["flow"], "known_from": ["heather@example.com"]},
            "per_incident": 1, "per_night": 1}
    create(world, order(world, effect_lines=[line]))


def test_an_order_with_no_kill_or_no_will_not_or_nothing_to_do_is_refused(world):
    assert "kill" in refused(world, order(world, kills=[]))
    assert "will-not" in refused(world, order(world, restatement={"will": ["a"], "will_not": []}))
    assert "says nothing" in refused(world, order(world, effect_lines=[], judgement_lines=[]))


def test_a_15_day_expiry_is_refused_and_14_is_taken(world):
    assert "14 days" in refused(world, order(world, days=15))
    create(world, order(world, days=14))


@pytest.mark.parametrize("over,said", [
    ({"words_sha256": "0" * 64}, "hex digits) of words"),
    ({"words": WORDS + "!"}, "hex digits) of words"),
    ({"id": "so-XYZ"}, "so- and 12"),
    ({"agent": "x"}, "exactly"),
    ({"restatement": {"will": ["a"]}}, "exactly will, will_not"),
    ({"kills": [{"when": "x", "then": "shrug"}]}, "then must be one of"),
    ({"effect_lines": [{"verb": "restart_service", "args": {}, "per_incident": True, "per_night": 1}]}, "integer"),
    ({"judgement_lines": ["bad‮line"]}, "showable"),
])
def test_a_malformed_order_is_refused(world, over, said):
    assert said in refused(world, order(world, **over))


def test_a_passkey_create_is_refused_and_a_passkey_revoke_lands(world):
    from levain.cockpit.firejournal import CockpitDB
    from levain.consent.grants import append_revoke, prepare_revoke

    CockpitDB(world.root).set_install_class("reported")
    assert "never a renew and never a create" in refused(world, order(world, n=9), "pk")
    g = order(world)
    create(world, g)
    assert auth.passkey_revocable(world.state().policy.grants[g["id"]])
    link = prepare_revoke(world.root, g["id"], signer_fp=world.fp("pk"), clock=world.clock)
    append_revoke(world.root, link, world.keys["pk"][1](link.message), clock=world.clock)
    assert g["id"] not in world.state().policy.grants


def test_a_display_phone_ssh_key_cannot_create_one(world):
    assert "cannot sign" in refused(world, order(world), "phone")


def test_terms_are_fixed_and_a_renew_is_refused_at_append(world):
    g = order(world, days=1)
    create(world, g)
    now = world.clock.now + timedelta(minutes=1)
    renew = {"op": "renew", "grant_id": g["id"], "expires_at": utc_stamp(now + timedelta(days=10)),
             "signed_at": utc_stamp(now)}
    assert "terms are fixed" in world.refused(renew, today=True)
    other = {**world.state().policy.grants[g["id"]], "kills": [{"when": "x", "then": "ping"}]}
    assert world.refused({"op": "loosen", "policy": {"grants": {g["id"]: other}}}, today=True)


def test_an_unknown_kind_is_refused_at_append_and_inert_on_replay(world):
    assert "does not know" in refused(world, {**order(world), "kind": "standing-order-v2"})
    with mock.patch.object(auth, "_GRANT_KINDS", MappingProxyType(
            {k: v for k, v in auth._GRANT_KINDS.items() if k != "standing-order"})):
        held = {"kind": "standing-order"}
        assert auth.inert_grant(held) and not auth.standing_order(held)


def test_a_log_written_under_a_laxer_append_rule_still_replays(world):
    """The shape is the replay's, the verbs and budgets are append rules: an order a looser append let in (here, an
    unlisted verb) replays, so a later tightening never refuses an old log."""
    kinds = dict(auth._GRANT_KINDS)
    kinds["standing-order"] = auth._GrantKind(**{**auth._GRANT_KINDS["standing-order"].__dict__, "append_checks": ()})
    line = {"verb": "future_verb", "args": {"x": ["y"]}, "per_incident": 0, "per_night": 0}
    g = order(world, effect_lines=[line], kills=[])
    with mock.patch.object(auth, "_GRANT_KINDS", MappingProxyType(kinds)):
        create(world, g)
    assert g["id"] in world.state().policy.grants


def widening(world: World, n: int, base: dict, days: int = 1, **over) -> dict:
    return order(world, n, widens=base["id"], widens_sha256=auth.standing_order_sha256(base), days=days, **over)


def test_a_widening_names_a_live_order_that_widens_nothing(world):
    base = order(world, 1)
    create(world, base)
    wide = widening(world, 2, base)
    create(world, wide)
    assert "which is not a standing order that widens nothing" in refused(
        world, order(world, 3, widens="so-" + "f" * 12, widens_sha256="0" * 64))
    assert "widens nothing" in refused(world, widening(world, 4, wide))
    assert "cannot widen itself" in refused(world, order(world, 5, widens="so-" + "0" * 11 + "5",
                                                         widens_sha256="0" * 64))


def test_a_widening_of_an_expired_base_is_refused(world):
    base = order(world, 1, days=1)
    create(world, base)
    world.clock.now += timedelta(days=2)
    assert "not live" in refused(world, widening(world, 2, base))


def test_a_widening_carries_its_base_digest_and_one_naming_another_record_is_refused(world):
    """L3 r1 on ND-2 (codex HIGH): a widening binds to its base's whole record, not its id alone."""
    base = order(world, 1)
    create(world, base)
    assert "widens_sha256" in refused(world, order(world, 2, widens=base["id"], days=1))      # no digest: the shape
    assert "its sha256 differs" in refused(world, order(world, 3, widens=base["id"], widens_sha256="0" * 64, days=1))
    create(world, widening(world, 4, base))


def test_a_widening_of_a_stopped_base_is_refused_at_append(world):
    """L3 r1 on ND-2 (codex #2): a base whose signing key was revoked answers nothing, so no widening of it is signed."""
    from tests.test_consent_queue import Key

    second = Key(world.tmp / "keys", "laptop2")
    world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key, "class": "laptop-presence",
                "custody": "passphrase"})
    world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
    base = order(world, 1)
    create(world, base, "laptop2")
    world.link({"op": "revoke_key", "fingerprint": second.fp})
    assert "answers nothing" in refused(world, widening(world, 2, base))
