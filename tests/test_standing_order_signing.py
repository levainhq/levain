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


def test_a_widening_lapses_to_its_base_and_dies_with_it(world):
    base = make(world)
    sign(world, base)
    wide = make(world, widens=base["id"], life=timedelta(days=1), effect_lines=[{**LINE, "per_night": 5}])
    sign(world, wide)
    assert [o["id"] for o in in_force(world)] == sorted([base["id"], wide["id"]])
    world.clock.now += timedelta(days=1, minutes=5)         # the widening's own expiry: the base stands
    assert [o["id"] for o in in_force(world)] == [base["id"]]
    other = make(world, widens=base["id"], life=timedelta(days=1))
    sign(world, other)
    world.link({"op": "revoke_grant", "grant_id": base["id"]}, today=True)   # the base is revoked: no widening answers
    assert in_force(world) == []


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
