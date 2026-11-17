"""The arming gate wired into the effect verbs (ND-11, board levain#20). DONE line: the gate refuses an order with effect
lines while any of ND-7's ruling, ND-8's done or ND-9's read is open. Real signed orders in a real log; the executors
are the stubs of tests/test_effect_verbs.py. tests/conftest.py opens the facts for every other test; here each test
sets what it means."""
from __future__ import annotations

import ast
import itertools
import shutil
from pathlib import Path

import pytest

from levain.desk import arming_gate as ag
from levain.desk import effects as fx
from levain.desk import effects_mcp as mcp

pytest.importorskip("cryptography")
from tests.test_effect_verbs import ACK_LINE, D, EVR, EVT, Rig, call, signed, world  # noqa: E402,F401
from tests.test_standing_order_signing import LINE, make, sign  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

FULL = {"nd7_ruled": "np-0123abcd", "nd8_done": "np-1123abcd", "nd9_read": "np-2123abcd"}
SUBSETS = [keys for n in (1, 2, 3) for keys in itertools.combinations(FULL, n)]
ITEM = {"nd7_ruled": "the ND-7 ruling", "nd8_done": "ND-8 done", "nd9_read": "the ND-9 read"}


def restart(world, rig, g):
    return rig.effects().restart_service(g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256=D(g))


@pytest.mark.parametrize("open_keys", SUBSETS, ids="+".join)
def test_an_order_with_effect_lines_is_refused_naming_exactly_the_open_items(world, monkeypatch, open_keys):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", {k: v for k, v in FULL.items() if k not in open_keys})
    g = signed(world)
    rig = Rig(world)
    for call_it in (lambda: restart(world, rig, g),
                    lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))):
        with pytest.raises(fx.EffectRefused) as err:
            call_it()
        text = fx.refusal_text(err.value)
        assert text == fx.R_ARMING[open_keys]
        assert all((ITEM[k] in text) == (k in open_keys) for k in FULL)       # names the open ones, and only those
    assert rig.untouched()                                                    # refused before any event read or process


def test_every_open_item_is_a_refusal_of_its_own():
    assert len(fx.R_ARMING) == 7 and len(set(fx.R_ARMING.values())) == 7
    assert set(fx.R_ARMING.values()) <= fx.REFUSALS and fx.R_ARMING_TWIN in fx.REFUSALS


def test_with_all_three_closed_the_effect_runs(world, monkeypatch):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", dict(FULL))
    g = signed(world)
    rig = Rig(world)
    assert restart(world, rig, g).ok and len(rig.runs) == 1
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).ok and len(rig.sent) == 1


def test_the_gate_reads_the_facts_at_every_call_not_at_import(world, monkeypatch):
    g = signed(world)
    rig = Rig(world)
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", ag.CLOSED_FACTS)
    with pytest.raises(fx.EffectRefused):
        restart(world, rig, g)
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", dict(FULL))
    assert restart(world, rig, g).ok
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", ag.CLOSED_FACTS)
    with pytest.raises(fx.EffectRefused):                                     # closed again: a recorded effect is gated too
        restart(world, rig, g)


@pytest.mark.parametrize("facts", [None, "yes", [], {"nd7_ruled": True, "nd8_done": 1, "nd9_read": "np-xyz"}])
def test_a_garbled_fact_store_is_closed(world, monkeypatch, facts):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", facts)
    g = signed(world)
    rig = Rig(world)
    with pytest.raises(fx.EffectRefused) as err:
        restart(world, rig, g)
    assert fx.refusal_text(err.value) == fx.R_ARMING[tuple(FULL)] and rig.untouched()


def test_one_np_id_for_two_items_is_refused(world, monkeypatch):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", dict.fromkeys(FULL, "np-0123abcd"))
    g = signed(world)
    rig = Rig(world)
    with pytest.raises(fx.EffectRefused) as err:
        restart(world, rig, g)
    assert fx.refusal_text(err.value) == fx.R_ARMING_TWIN and rig.untouched()


def test_an_order_without_effect_lines_is_never_blocked_by_the_gate(world, monkeypatch):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", ag.CLOSED_FACTS)
    g = make(world, effect_lines=[])
    sign(world, g)
    rig = Rig(world)
    order, lines = None, None
    with pytest.raises(fx.EffectRefused) as err:
        order, lines = rig.effects().gate(g["id"], "restart_service", expect_sha256=D(g))
    assert fx.refusal_text(err.value) == fx.R_LINE                            # "no line covers this", never "not armed"
    assert fx.refusal_text(err.value) not in fx.R_ARMING.values()
    # and with the facts open the same order answers the same: the gate changed nothing for it
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", dict(FULL))
    with pytest.raises(fx.EffectRefused) as again:
        rig.effects().gate(g["id"], "restart_service", expect_sha256=D(g))
    assert fx.refusal_text(again.value) == fx.R_LINE and rig.untouched()


def test_an_order_in_force_without_the_asked_version_is_still_a_version_refusal_when_closed(world, monkeypatch):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", ag.CLOSED_FACTS)
    g = signed(world)
    with pytest.raises(fx.EffectRefused) as err:
        Rig(world).effects().restart_service(g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256="0" * 64)
    assert fx.refusal_text(err.value) == fx.R_VERSION


# ---- no way to arm around it ---------------------------------------------------------------------------------------
def test_the_effect_server_refuses_an_effect_while_closed_and_its_command_line_has_no_way_to_open_it(world, monkeypatch, tmp_path):
    monkeypatch.setattr(fx, "NIGHT_ARMING_FACTS", ag.CLOSED_FACTS)
    g = signed(world)
    rig = Rig(world)
    res = call(rig.effects(), "restart_service", {"order_id": g["id"], "order_sha256": D(g), "host": "taoserv",
                                                  "service": "apache2", "event_id": EVR})
    assert res["isError"] is True and res["content"][0]["text"] == fx.R_ARMING[tuple(FULL)] and rig.untouched()
    # the only option the server's command line takes is the cockpit root: any other is a usage error, and no
    # argument reaches a fact (nor a tool argument: the schema is closed, a stray one is refused in words)
    for extra in (["--facts", "x"], ["--armed"], ["--nd7-ruled", "np-0123abcd"], ["--night-arming-cleared"]):
        with pytest.raises(SystemExit):
            mcp.main(["--data-root", str(tmp_path), *extra])
    stray = call(rig.effects(), "restart_service", {"order_id": g["id"], "order_sha256": D(g), "host": "taoserv",
                                                    "service": "apache2", "event_id": EVR, "armed": True})
    assert stray["content"][0]["text"] == fx.R_ARGS and rig.untouched()


def test_effects_takes_no_facts_and_nothing_else_writes_them():
    import dataclasses
    assert not {f.name for f in dataclasses.fields(fx.Effects)} & {"facts", "arming", "armed", "night_arming_facts"}
    root = Path(fx.__file__).resolve().parents[1]
    holders = sorted(str(p.relative_to(root)) for p in root.rglob("*.py") if "NIGHT_ARMING_FACTS" in p.read_text())
    assert holders == ["desk/arming_gate.py", "desk/effects.py"]              # the gate's own docstring names it; no caller does


def test_the_shipped_facts_are_the_closed_ones():
    tree = ast.parse(Path(fx.__file__).read_text())
    values = [n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "NIGHT_ARMING_FACTS"]
    assert len(values) == 1 and ast.unparse(values[0]) == "arming_gate.CLOSED_FACTS" and dict(ag.CLOSED_FACTS) == {}
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "NIGHT_ARMING_FACTS" for t in n.targets)]
    assert not writes                                                          # nothing in the module rebinds it
