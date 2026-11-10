"""THE LIST's intake, end to end (levain 64 integration of L1 + L2): ``list_intake`` posted through the cockpit's own
``dispatch`` on the List panel (wire id ``tray``), with a real broker, a real spore store and a real desk store in
tmp dirs. Nothing here touches an operator's live apps."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain import intake
from levain.cockpit import list_panel
from levain.cockpit.broker import Broker
from levain.cockpit.effects import SporeEffectStore
from levain.cockpit.verbs import dispatch
from levain.desk.store import CoordStore
from levain.cockpit.verbs import _intake_outbox_id
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, NOW, Rig


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def broker(rig: Rig, tmp_path: Path):
    spores = rig.root / ".levain" / "memory.spores.json"
    b = Broker(tmp_path / "broker", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class=INSTALL_CLASS, clock=lambda: NOW)
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _key(line: str) -> str:
    return "e2e-" + str(sum(map(ord, line)))


def _post(rig: Rig, broker: Broker, line: str, **extra: Any) -> dict[str, Any]:
    req = {"verb": "list_intake", "panel_id": list_panel.LIST_PANEL_ID, "params": {"line": line},
           "idempotency_key": _key(line), **extra}
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS, req=req, broker=broker)


def test_the_list_panel_offers_list_intake_and_the_wire_id_is_still_tray(rig: Rig) -> None:
    assert list_panel.LIST_PANEL_ID == "tray"
    assert "list_intake" in {a["verb"] for a in rig.panel()["actions"]}


def test_a_plain_line_is_sorted_to_the_desk_and_says_so(rig: Rig, broker: Broker) -> None:
    out = _post(rig, broker, "look at the sticky note")
    assert out["receipt_line"] == "sent to the desk to sort"
    outbox = _intake_outbox_id("browser", _key("look at the sticky note"))
    drop = CoordStore(rig.qdir).drop_by_outbox(outbox)
    assert drop is not None and drop.words == "look at the sticky note"


def test_a_talk_line_plants_a_talk_seed_through_the_panel(rig: Rig, broker: Broker) -> None:
    out = _post(rig, broker, "? flow's future", intent="reported", tier_seen="T2", gesture_seen="direct-reported")
    sid = out["receipt"]["id"]
    assert out["receipt"]["kind"] == "talk" and sid
    assert out["receipt_line"] == f"talk {sid}"
    row = SporeStore(rig.root / ".levain" / "memory.spores.json").get(sid)
    assert (row["disposition"], row.get("attention")) == ("seed", "talk")


def test_a_timed_drop_plants_an_agenda_for_the_desk_through_the_panel(rig: Rig, broker: Broker) -> None:
    out = _post(rig, broker, "@+2h call the dentist", intent="reported", tier_seen="T2", gesture_seen="direct-reported")
    fires = (NOW + timedelta(hours=2)).astimezone().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
    sid = out["receipt"]["id"]
    assert out["receipt"] == {"kind": "drop", "id": sid, "fires": fires, "text": "call the dentist"}
    assert out["receipt_line"] == intake.receipt_line(out["receipt"])
    assert out["receipt_line"].startswith(f"scheduled drop {sid} fires ")
    row = SporeStore(rig.root / ".levain" / "memory.spores.json").get(sid)
    assert (row["disposition"], row["target"], row["next"]) == ("agenda", "desk", fires)


def test_a_signed_drop_waits_and_claims_no_fire(rig: Rig, broker: Broker) -> None:
    out = _post(rig, broker, "@mon 9am call X")          # no intent: sign
    assert out["state"] == "WAITING" and out["receipt"]["id"] is None
    assert out["receipt_line"].endswith(", awaiting your signature")
    assert json.loads(out["record"])["target"]["intake"]["when"] == out["receipt"]["fires"]
