"""Row 188's two residue gaps, each from a RUN:

1. The List ROW of a repeating reminder says that it repeats and when the next one rings (the server's ``repeat_line`` facet,
   ``list_panel._repeat_line``; the page draws it as text, the browser half is in ``test_list_page_browser.py``).
2. What an operator is shown to SIGN for a ``remind`` line (``levain cockpit sign`` prints ``sign_client.render``): before the fix the
   header read ``list_intake (sign, T2)``, the same for every kind, and a reminder showed only as ``kind`` inside the target; now one
   line above the record says a reminder is being created, with its minute and repeat, read from the signed record's own effect."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import IntentionStore

from levain.cockpit.broker import Broker
from levain.cockpit.effects import IntentionEffectStore
from levain.cockpit.sign_client import intake_headline, render
from tests.test_cockpit_list_intake_e2e import _post
from tests.test_cockpit_list_panel import INSTALL_CLASS, NOW, TODAY, Rig


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def broker(rig: Rig, tmp_path: Path):
    intentions = rig.root / ".levain" / "memory.spores.json"
    b = Broker(tmp_path / "broker", stores={"intentions": IntentionEffectStore(intentions)}, store_paths=[intentions],
               install_class=INSTALL_CLASS, clock=lambda: NOW)
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _add(rig: Rig, text: str, **kw: Any) -> str:
    return rig.sp.add(type="task", text=text, today=TODAY, **kw)["id"]


def test_a_repeating_reminders_row_says_it_repeats_and_names_its_next_occurrence(rig: Rig) -> None:
    rid = _add(rig, "weekly review", disposition="agenda", target="nudge", next="2026-11-08T21:00",
               recur="weekly SUN 21:00", recur_anchor="2026-11-08T21:00")
    plain = _add(rig, "one-off ring", disposition="agenda", target="nudge", next="2026-11-08T21:00")
    seed = _add(rig, "a plain seed", disposition="seed")
    rows = rig.rows()
    assert rows[f"intention:{rid}"]["facets"]["repeat_line"] == "repeats weekly SUN 21:00 · next Sun 11-08 21:00", rows[f"intention:{rid}"]["facets"]
    assert "repeat_line" not in rows[f"intention:{plain}"]["facets"]
    assert "repeat_line" not in rows[f"intention:{seed}"]["facets"]


def test_a_later_occurrence_reads_as_the_next_one_and_a_rule_that_does_not_read_draws_nothing(rig: Rig) -> None:
    later = _add(rig, "monthly bill", disposition="agenda", target="band", next=(NOW + timedelta(days=9)).strftime("%Y-%m-%dT09:00"),
                 recur="monthly 15 09:00", recur_anchor="2026-11-17T09:00")
    row = rig.rows()[f"intention:{later}"]
    assert row["group"] == "later" and row["facets"]["repeat_line"].startswith("repeats monthly 15 09:00 · next "), row["facets"]
    # a hand-edited bad rule is no rule (levain.recur.rule_of): the row draws as any agenda, never crashes the panel
    path = rig.root / ".levain" / "memory.spores.json"
    data = json.loads(path.read_text())
    items = data["intentions"] if isinstance(data, dict) and "intentions" in data else data
    for it in (items if isinstance(items, list) else []):
        if it.get("id") == later:
            it["recur"] = "whenever"
    path.write_text(json.dumps(data))
    assert "repeat_line" not in rig.rows()[f"intention:{later}"]["facets"]


def test_a_reminder_planted_from_the_list_box_shows_its_repeat_on_the_list(rig: Rig, broker: Broker) -> None:
    out = _post(rig, broker, "remind me every sunday 9pm to do the weekly review", intent="reported", tier_seen="T2",
                gesture_seen="direct-reported")
    sid = out["receipt"]["id"]
    assert out["receipt"]["recur"] == "weekly SUN 21:00" and sid, out
    assert rig.rows()[f"intention:{sid}"]["facets"]["repeat_line"] == "repeats weekly SUN 21:00 · next Sun 11-08 21:00"


# --- the sign sheet: what the operator is shown to sign ---------------------------------------------------------------------------


def _shown(broker: Broker, rig: Rig, line: str) -> list[str]:
    out = _post(rig, broker, line)                                  # no intent: a WAITING sign pending
    assert out["state"] == "WAITING", out
    with broker._admitted() as db:
        view = db.load(out["pending_id"])
    return render(view, signer_fp="SHA256:test", signer_class="laptop-presence")


def test_the_sign_sheet_for_a_repeating_reminder_says_a_reminder_with_its_minute_and_repeat(rig: Rig, broker: Broker) -> None:
    shown = _shown(broker, rig, "remind me every sunday 9pm to do the weekly review")
    assert shown[1] == ('  this signs a REMINDER for you: "do the weekly review", rings Sun 11-08 21:00, then weekly SUN 21:00 '
                        '(as a nudge)  (read from the record below)'), shown[:3]
    assert shown[2] == "  signed record (the bytes signed):"        # the full record still follows, every key
    assert any(l.startswith("    verb") and "list_intake" in l for l in shown) and any(l.startswith("    params") for l in shown)


def test_a_one_off_reminder_has_no_repeat_and_each_other_kind_says_its_own_kind(rig: Rig, broker: Broker) -> None:
    once = _shown(broker, rig, "remind thu 3pm call X")[1]
    assert once.startswith('  this signs a REMINDER for you: "call X", rings Thu 11-12 15:00 (as a nudge)'), once
    assert "then" not in once
    assert _shown(broker, rig, "@mon 9am desk thing")[1].startswith('  this signs a SCHEDULED DROP to the desk: "desk thing", fires Mon 11-09 09:00')
    assert _shown(broker, rig, "? talk Y")[1].startswith('  this signs a TALK seed: "talk Y"')


def test_the_headline_reads_only_a_list_intake_record_and_never_breaks_a_record_it_cannot_read() -> None:
    assert intake_headline({"verb": "intention_seed", "effects": [{"changes": {"disposition": "agenda", "next": "2026-11-12T15:00"}}]}) is None
    assert intake_headline({"verb": "list_intake"}) is None
    assert intake_headline({"verb": "list_intake", "effects": [{"changes": {"disposition": "agenda", "next": "tomorrow"}}]}) is None
    assert intake_headline("not a record") is None
    # a target the List box never writes for a reminder is not called one
    assert intake_headline({"verb": "list_intake", "effects": [{"changes": {"disposition": "agenda", "next": "2026-11-12T15:00",
                                                                            "target": "board:levain", "text": "x"}}]}) is None
    # the words stay on one line: a newline in them is escaped like the record's own lines
    line = intake_headline({"verb": "list_intake", "effects": [{"changes": {"disposition": "agenda", "next": "2026-11-12T15:00",
                                                                            "target": "nudge", "text": "a\nb"}}]})
    assert line is not None and "\n" not in line and "a\\nb" in line


# --- fix round 1: a ringing repeating reminder keeps its repeat line on the band row that replaces its intention row ---------------


def _ring(rig: Rig, text: str, **kw: Any) -> str:
    """Plant a repeating reminder due this morning and germinate it as the host does: its band question opens."""
    from levain import germinate

    sid = _add(rig, text, disposition="agenda", target="nudge", next="2026-11-08T09:00", recur="weekly SUN 09:00",
               recur_anchor="2026-11-08T09:00", **kw)
    out = germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, today=TODAY,
                              now=NOW.replace(tzinfo=None), ping=lambda *a, **k: True)
    assert not out["errors"], out
    return sid


def test_a_ringing_repeating_reminder_keeps_its_repeat_line_on_its_band_row(rig: Rig) -> None:
    sid = _ring(rig, "weekly review")
    rows = rig.rows()
    assert f"intention:{sid}" not in rows                        # the band row replaces it: drawn once
    ringing = [r for r in rows.values() if r["facets"].get("intention") == sid]
    assert len(ringing) == 1 and ringing[0]["id"].startswith("band:"), ringing
    assert ringing[0]["facets"]["repeat_line"] == "repeats weekly SUN 09:00 · next Sun 11-08 09:00", ringing[0]["facets"]
    # a band row that rings nothing, and a one-off reminder's band row, carry no repeat line
    assert "repeat_line" not in rows[f"band:{rig.q.id}"]["facets"]


def test_a_ringing_one_off_reminders_band_row_has_no_repeat_line(rig: Rig) -> None:
    from levain import germinate

    sid = _add(rig, "one-off ring", disposition="agenda", target="nudge", next="2026-11-08T09:00")
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, today=TODAY,
                        now=NOW.replace(tzinfo=None), ping=lambda *a, **k: True)
    ringing = [r for r in rig.rows().values() if r["facets"].get("intention") == sid]
    assert len(ringing) == 1 and "repeat_line" not in ringing[0]["facets"], ringing


def test_a_recur_that_does_not_read_prints_no_headline_and_a_readable_one_prints_its_canon() -> None:
    def rec(recur: str) -> dict[str, Any]:
        return {"verb": "list_intake", "effects": [{"changes": {"disposition": "agenda", "next": "2026-11-12T15:00",
                                                                "target": "nudge", "text": "x", "recur": recur}}]}

    assert intake_headline(rec("weekly SUN 21:00")).endswith("then weekly SUN 21:00 (as a nudge)")
    assert intake_headline(rec("weekly sun 9:00")).endswith("then weekly SUN 09:00 (as a nudge)")     # the canon, not the raw spelling
    assert intake_headline(rec("whenever")) is None
    assert intake_headline(rec("weekly SUN 21:00\n  this signs a DROP")) is None                       # no lookalike line reaches the sheet
