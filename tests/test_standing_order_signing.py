"""The standing order's signing entry, its card, its reader and its place in the desk brief (night desk ND-2, board
levain#9). DONE line: expired, revoked and unknown orders answer nothing; the brief quotes the signed bytes. Real logs
and real ssh-keygen signatures (the ``World`` of the graduation kind's tests)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from types import MappingProxyType
from unittest import mock

import pytest

import levain.authority as auth
from levain.cockpit.keys import KeyError_
from levain.desk import standing_orders as so

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LINE = {"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2", "php8.1-fpm"]},
        "per_incident": 1, "per_night": 2}
WORDS = "If taoserv is down overnight, bring apache back; wake me for anything else.\nNo mysql. Never."


@pytest.fixture
def world(tmp_path) -> World:
    from levain.cockpit.firejournal import CockpitDB
    w = World(tmp_path)
    CockpitDB(w.root).set_install_class("reported")
    return w


def make(world: World, **over) -> dict:
    kw = dict(words=WORDS, will=["diagnose read-only", "restart apache2 or php8.1-fpm"],
              will_not=["touch mysql", "act outside this order"], effect_lines=[LINE],
              judgement_lines=["diagnose: read the sites and the error log"],
              kills=[{"when": "signs of intrusion", "then": "stop_effects"}], now=world.clock.now)
    kw.update(over)
    return so.build_order(**kw)


def sign(world: World, g: dict, signer: str = "laptop"):
    return so.sign_order(world.root, g, signer_fp=world.fp(signer), sign=world.keys[signer][1], clock=world.clock)


def in_force(world: World) -> list:
    return so.orders_in_force(world.state(), world.clock.now)


def test_a_signed_order_is_in_force_and_what_was_signed_is_the_card_snapshot(world):
    g = make(world)
    new = sign(world, g)
    assert new.policy.grants[g["id"]]["words"] == WORDS
    assert [o["id"] for o in in_force(world)] == [g["id"]]
    assert so.order_bytes(new.policy.grants[g["id"]]) == so.order_bytes(g)


def test_the_card_shows_the_whole_restatement_and_every_line(world):
    from levain.cockpit.authority import standing_order_card

    g = make(world, judgement_lines=["j-one", "j-two"], kills=[{"when": "k-when", "then": "ping"}])
    card = standing_order_card(g)
    sheet = card["sheet"]
    for text in (*WORDS.split("\n"), *g["restatement"]["will"], *g["restatement"]["will_not"], "j-one", "j-two",
                 "k-when -> ping", g["id"], g["words_sha256"], g["expires_at"],
                 "restart_service(host in {taoserv}; service in {apache2, php8.1-fpm}): at most 1 per incident, 2 per night"):
        assert text in sheet
    assert card["fields"] == json.loads(json.dumps(g))


def test_a_card_is_refused_for_an_order_the_log_would_refuse_and_nothing_is_signed(world):
    bad = make(world, effect_lines=[{**LINE, "verb": "run_shell"}])
    before = world.state().seq
    with pytest.raises(KeyError_, match="run_shell"):
        sign(world, bad)
    assert world.state().seq == before


def test_only_an_active_laptop_key_signs_one(world):
    before = world.state().seq
    for who in ("pk", "phone"):
        with pytest.raises(KeyError_, match="laptop-presence"):
            sign(world, make(world), who)
    assert world.state().seq == before


def test_a_second_sign_of_the_same_id_is_refused(world):
    g = make(world)
    sign(world, g)
    with pytest.raises(KeyError_, match="already exists"):
        sign(world, g)


def test_an_expired_order_answers_nothing(world):
    sign(world, make(world))
    world.clock.now += timedelta(days=14, minutes=5)
    assert in_force(world) == []


def test_a_revoked_order_answers_nothing(world):
    g = make(world)
    sign(world, g)
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    assert in_force(world) == []


def test_an_order_whose_signing_key_was_revoked_answers_nothing(world):
    from levain.cockpit.keys import ssh_signer  # noqa: F401  (the second laptop key is enrolled through the World's own door)
    from tests.test_consent_queue import Key

    second = Key(world.tmp / "keys", "laptop2")
    world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key, "class": "laptop-presence",
                "custody": "passphrase"})
    world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
    g = make(world)
    sign(world, g, "laptop2")
    assert [o["id"] for o in in_force(world)] == [g["id"]]
    world.link({"op": "revoke_key", "fingerprint": second.fp})
    assert in_force(world) == []


def test_an_unknown_kind_answers_nothing(world):
    g = make(world)
    sign(world, g)
    gone = MappingProxyType({k: v for k, v in auth._GRANT_KINDS.items() if k != auth.STANDING_ORDER_KIND})
    with mock.patch.object(auth, "_GRANT_KINDS", gone):
        assert in_force(world) == []


def widen(world: World, base: dict, **over) -> dict:
    return make(world, widens=base["id"], widens_sha256=so.base_sha256(world.state(), base["id"], world.clock.now),
                **{"life": timedelta(days=1), **over})


def test_a_widening_lapses_to_its_base_and_dies_with_it(world):
    base = make(world)
    sign(world, base)
    wide = widen(world, base, effect_lines=[{**LINE, "per_night": 5}])
    sign(world, wide)
    assert [o["id"] for o in in_force(world)] == sorted([base["id"], wide["id"]])
    world.clock.now += timedelta(days=1, minutes=5)         # the widening's own expiry: the base stands
    assert [o["id"] for o in in_force(world)] == [base["id"]]
    other = widen(world, base)
    sign(world, other)
    world.link({"op": "revoke_grant", "grant_id": base["id"]}, today=True)   # the base is revoked: no widening answers
    assert in_force(world) == []


def test_a_widening_never_widens_a_base_revoked_and_signed_again_under_its_id(world):
    """L3 r1 on ND-2 (codex HIGH, complement #3): the old widening does not apply to a new order under the old id."""
    base = make(world, order_id="so-00000000000a")
    sign(world, base)
    wide = widen(world, base, life=timedelta(days=3), effect_lines=[{**LINE, "per_night": 5}])
    sign(world, wide)
    world.link({"op": "revoke_grant", "grant_id": base["id"]}, today=True)
    again = make(world, order_id=base["id"], words="another order under the same id")
    sign(world, again)
    assert [o["id"] for o in in_force(world)] == [base["id"]]


def test_a_widening_never_widens_a_byte_identical_base_signed_again(world):
    """The same bytes signed again after a revoke are a new incarnation (born after the widening): still no force."""
    base = make(world, order_id="so-00000000000b")
    sign(world, base)
    wide = widen(world, base, life=timedelta(days=3))
    sign(world, wide)
    world.link({"op": "revoke_grant", "grant_id": base["id"]}, today=True)
    sign(world, base)
    assert [o["id"] for o in in_force(world)] == [base["id"]]


def test_a_widening_never_widens_a_base_that_expired_and_was_signed_again(world):
    base = make(world, order_id="so-00000000000c", life=timedelta(days=2))
    sign(world, base)
    wide = widen(world, base, life=timedelta(days=1))
    sign(world, wide)
    world.clock.now += timedelta(days=2, minutes=5)          # the base expired (and the widening with its own clock)
    world.link({"op": "revoke_grant", "grant_id": base["id"]}, today=True)
    again = make(world, order_id=base["id"])
    sign(world, again)
    assert [o["id"] for o in in_force(world)] == [base["id"]]
    # even a widening still within its own expiry: one signed for 3 days, base expired at 2, recreated
    base2 = make(world, order_id="so-00000000000d", life=timedelta(days=1))
    sign(world, base2)
    wide2 = widen(world, base2, life=timedelta(days=3))
    sign(world, wide2)
    world.clock.now += timedelta(days=1, minutes=5)
    world.link({"op": "revoke_grant", "grant_id": base2["id"]}, today=True)
    sign(world, make(world, order_id=base2["id"]))
    assert wide2["id"] not in [o["id"] for o in in_force(world)]


def test_an_order_the_append_rules_refuse_is_not_in_force_even_when_the_log_holds_it(world, tmp_path):
    """L3 r1 on ND-2 (complement #2): a foreign or looser writer's order (unknown verb, zero budgets, no kill) replays
    (the shape is the replay's), but it is never quoted as authority, and neither is a widening of it."""
    bad = make(world, effect_lines=[{"verb": "run_shell", "args": {"cmd": ["rm"]}, "per_incident": 0,
                                     "per_night": 0}], kills=[])
    with mock.patch.object(auth, "_append_only_checks", lambda *a, **k: None):
        world.link({"op": "create_grant", "grant": bad}, today=True)
    assert bad["id"] in world.state().policy.grants
    assert in_force(world) == []
    text = "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    assert "No standing order is in force" in text and "run_shell" not in text
    assert so.order_status(world.state(), bad["id"], world.clock.now, set()) == \
        "not in force: the order rules refuse it"


def test_the_brief_fails_closed_on_any_fault_after_the_read(world, tmp_path):
    """L3 r1 on ND-2 (complement #1): a fault filtering or rendering is "no order" in words, never a launch abort."""
    sign(world, make(world))
    with mock.patch.object(so, "orders_in_force", side_effect=RuntimeError("boom")):
        text = "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    assert "Treat this as NO standing order" in text and "~~~ ORDER" not in text
    with mock.patch.object(so, "order_bytes", side_effect=TypeError("odd")):
        text = "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    assert "Treat this as NO standing order" in text and "~~~ ORDER" not in text
    text = "\n".join(so.brief_section(tmp_path, world.clock.now.replace(tzinfo=None), data_root=world.root))
    assert "Treat this as NO standing order" in text            # a naive clock is said, not read as "none"


def test_the_brief_quotes_the_signed_bytes_and_only_the_live_ones(world, tmp_path):
    live, old = make(world), make(world, words="old words", life=timedelta(days=1))
    sign(world, live)
    sign(world, old)
    world.clock.now += timedelta(days=2)
    lines = so.brief_section(tmp_path, world.clock.now, data_root=world.root)
    text = "\n".join(lines)
    signed = so.order_bytes(world.state().policy.grants[live["id"]])
    assert signed in lines and signed.startswith("{") and WORDS.split("\n")[0] in signed
    assert f"~~~ ORDER {live['id']} " in text and f"~~~ END ORDER {live['id']} " in text
    assert old["id"] not in text and "old words" not in text
    assert "answers nothing" in text


def test_the_brief_says_none_when_none_and_fails_closed_when_the_log_is_unreadable(world, tmp_path):
    text = "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    assert "No standing order is in force" in text and "~~~ ORDER" not in text
    sign(world, make(world))
    conn = sqlite3.connect(world.db)
    conn.execute("UPDATE authority_log SET signature = 'AAAA' WHERE seq = 3")
    conn.commit()
    conn.close()
    text = "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    assert "Treat this as NO standing order" in text and "~~~ ORDER" not in text
    text = "\n".join(so.brief_section(tmp_path, world.clock.now))           # no cockpit named: the same, in words
    assert "Treat this as NO standing order" in text


def test_every_desk_brief_carries_the_section_in_front_of_the_lease(world, tmp_path):
    from levain.desk import launch as launch_mod
    from levain.desk.store import CoordStore

    g = make(world)
    sign(world, g)
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir, clock=world.clock)
    store.coord_set_agent("claude", cwd=str(tmp_path))
    store.accept_drop("wake", surface="cli")
    ln = store.pending_desk()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None
    with mock.patch.object(so, "_cockpit_root", lambda _d: world.root):
        text = launch_mod._brief_text(store, ln)
    assert "## Standing orders" in text and so.order_bytes(world.state().policy.grants[g["id"]]) in text
    assert text.index("## Standing orders") < text.index("## The lease")


def test_the_cli_prints_the_whole_sheet_signs_with_a_key_file_and_lists_status(world, tmp_path, capsys):
    import argparse

    from levain.desk.cli import run_order
    from tests.test_consent_queue import Key

    key = Key(world.tmp / "keys", "operator")
    world.link({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                "custody": "passphrase"})
    spec = {"words": WORDS, "will": ["diagnose read-only"], "will_not": ["touch mysql"], "effect_lines": [LINE],
            "judgement_lines": ["read the logs"], "kills": [{"when": "intrusion", "then": "ping"}], "days": 3}
    f = tmp_path / "order.json"
    f.write_text(json.dumps(spec))
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    base = dict(queue_dir=qdir, json=False, coord_cmd="order", token=None, data_root=world.root)
    from datetime import datetime, timezone
    # the CLI reads the real clock for the order's life; the world is in the past, so build the world's link time ahead
    world.clock.now = datetime.now(timezone.utc)
    args = argparse.Namespace(order_cmd="sign", file=f, signing_key=key.path, yes=True, **base)
    assert run_order(args) == 0
    captured = capsys.readouterr()
    assert "STANDING ORDER so-" in captured.out and "No mysql. Never." in captured.out and "signed: so-" in captured.out
    assert run_order(argparse.Namespace(order_cmd="list", **base)) == 0
    assert "in force" in capsys.readouterr().out
    bad = dict(spec, effect_lines=[{**LINE, "verb": "run_shell"}])
    f.write_text(json.dumps(bad))
    assert run_order(argparse.Namespace(order_cmd="sign", file=f, signing_key=key.path, yes=True, **base)) == 1
    assert "nothing was signed" in capsys.readouterr().err


def _cli(world, tmp_path):
    import argparse
    from datetime import datetime, timezone

    from levain.desk.cli import run_order
    from tests.test_consent_queue import Key

    key = Key(world.tmp / "keys", "operator")
    world.link({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                "custody": "passphrase"})
    world.clock.now = datetime.now(timezone.utc)
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    base = dict(queue_dir=qdir, json=False, coord_cmd="order", token=None, data_root=world.root)
    f = tmp_path / "order.json"

    def run(cmd, spec=None):
        if spec is not None:
            f.write_text(json.dumps(spec))
        extra = dict(file=f, signing_key=key.path, yes=True) if cmd == "sign" else {}
        return run_order(argparse.Namespace(order_cmd=cmd, **extra, **base))
    return run


SPEC = {"words": WORDS, "will": ["diagnose read-only"], "will_not": ["touch mysql"], "effect_lines": [LINE],
        "judgement_lines": ["read the logs"], "kills": [{"when": "intrusion", "then": "ping"}], "days": 3}


@pytest.mark.parametrize("over", [{"effect_lines": [{}]}, {"effect_lines": [{**LINE, "args": "host"}]},
                                  {"effect_lines": [{**LINE, "args": {"host": "taoserv"}}]}, {"will": "abc"},
                                  {"kills": ["x"]}, {"effect_lines": [None]}, {"widens": 7}, {"words": ["x"]}])
def test_the_cli_refuses_a_malformed_order_file_in_words(world, tmp_path, capsys, over):
    """L3 r1 on ND-2 (codex #4, complement #6): malformed nested JSON is refused cleanly, never a traceback, and a
    string is never read as a list of one-character lines."""
    run = _cli(world, tmp_path)
    before = world.state().seq
    assert run("sign", {**SPEC, **over}) == 1
    assert "levain desk order sign: " in capsys.readouterr().err
    assert world.state().seq == before


def test_the_sheet_shows_the_whole_order_digest_the_brief_prints_and_signs_that_snapshot(world, tmp_path, capsys):
    """L3 r1 on ND-2 (codex #3, complement #5): one canonical snapshot is shown, hashed and signed."""
    run = _cli(world, tmp_path)
    assert run("sign", SPEC) == 0
    out = capsys.readouterr().out
    [gid] = [g for g in world.state().policy.grants if so.standing_order(world.state().policy.grants[g])]
    digest = so.order_sha256(world.state().policy.grants[gid])
    assert f"The whole order's sha256 (the desk brief prints it beside the order): {digest}" in out
    assert f"(sha256 {digest})" in "\n".join(so.brief_section(tmp_path, world.clock.now, data_root=world.root))
    with pytest.raises(KeyError_, match="not the one shown"):
        so.sign_order(world.root, make(world), signer_fp=world.fp("laptop"), sign=world.keys["laptop"][1],
                      clock=world.clock, expect_sha256="0" * 64)


def test_the_cli_signs_a_widening_against_its_base_and_lists_a_lapsed_one_by_why(world, tmp_path, capsys):
    """L3 r1 on ND-2 (codex #5, complement #4): a widening whose base is not in force is named so, not "expired"."""
    run = _cli(world, tmp_path)
    assert run("sign", SPEC) == 0
    grants = world.state().policy.grants
    [base] = [g for g in grants if so.standing_order(grants[g])]
    assert run("sign", {**SPEC, "widens": base, "days": 1, "effect_lines": [{**LINE, "per_night": 4}]}) == 0
    held = world.state().policy.grants
    [wide] = [g for g in held if held[g].get("widens") == base]
    assert held[wide]["widens_sha256"] == so.order_sha256(held[base])
    capsys.readouterr()
    world.link({"op": "revoke_grant", "grant_id": base}, today=True)
    assert run("list") == 0
    assert f"{wide}  widening lapsed: base not in force" in capsys.readouterr().out
    assert run("sign", {**SPEC, "widens": base, "days": 1}) == 1          # a base not in force: refused before a sheet
    err = capsys.readouterr().err
    assert "is not in force" in err
