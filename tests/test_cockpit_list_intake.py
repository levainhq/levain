"""THE LIST's intake write path (levain 64 L1; flow task_surfaces_1107.md THE LIST): the three create fields on
``intention_seed`` (a timed ``next``, the germinate ``target``, ``attention``), the ``list_intake`` verb's tier table, its
broker build (one reading of the line, at issue), its receipt, and the C1 sort drop. Real anneal IntentionStores and a
real desk store in tmp dirs; nothing is mocked."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import IntentionStore

from levain import intake
from levain.cockpit.broker import Broker, BrokerError
from levain.cockpit.effects import (
    INTAKE_TIERS,
    INTAKE_VERB,
    INTENTION_AUTHORITY_FIELDS,
    REMIND_TARGET,
    REMIND_TIER,
    EffectStatus,
    IntentionEffectStore,
    canonical_effect,
    create_origin_key,
    intake_kind_of,
    intake_seed_params,
    settle,
    intention_resource_key,
)
from levain.cockpit.verbs import DEVICE_VERBS, KERNEL_VERBS, VerbRegistry, _intake_fire
from levain.writes import EditError
from tests.autonomic_confirm_keys import anchor_scratch

PENDING = "cp" + "cd" * 16
BROWSER = {"class": "token", "name": "browser"}
DEVICE = {"class": "device", "fingerprint": "SHA256:" + "B" * 43, "transport": "run"}
# a fixed instant: Sat 2026-11-07 22:00 UTC (the broker's clock); the intake reads it as local time
NOW_UTC = datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)
NOW_LOCAL = NOW_UTC.astimezone().replace(tzinfo=None)


@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("list-intake-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def intentions(home: Path) -> Path:
    p = home / "spores.json"
    IntentionStore(p, id_prefix="intention").add(type="task", text="a loop", origin_key="k.loop")
    return p


@pytest.fixture
def broker(home: Path, intentions: Path) -> Iterator[Broker]:
    now = [NOW_UTC]
    b = Broker(home / "data", stores={"intentions": IntentionEffectStore(intentions)}, store_paths=[intentions],
               install_class="reported", clock=lambda: now[0])
    b.start()
    b._test_now = now  # type: ignore[attr-defined]
    try:
        yield b
    finally:
        b.stop()


def _seed(es: IntentionEffectStore, params: dict[str, Any]):
    ok = create_origin_key(PENDING, 0)
    return canonical_effect("intention_seed", params, es.snapshot(intention_resource_key(ok)).row, origin_key=ok)


def _row(path: Path, key: str) -> dict | None:
    return IntentionStore(path, id_prefix="intention").get_by_origin_key(key)


# --- 1: intention_seed's three new fields, applied and refused ---------------------------------------------------------

def test_a_drop_seed_stores_its_timed_next_whole_and_its_desk_target(intentions):
    es = IntentionEffectStore(intentions)
    eff = _seed(es, {"text": "call X", "type": "task", "disposition": "agenda", "target": "desk",
                     "next": "2026-11-09T09:00"})
    assert eff.changes["next"] == "2026-11-09T09:00" and eff.changes["target"] == "desk"
    (unit,) = es.units([(0, eff)])
    assert settle(unit, es, {}).status is EffectStatus.APPLIED
    row = _row(intentions, create_origin_key(PENDING, 0))
    # the minute is stored as given: never cut to its date anywhere on the write path
    assert (row["disposition"], row["target"], row["next"]) == ("agenda", "desk", "2026-11-09T09:00")
    assert "attention" not in row or row["attention"] is None


def test_a_talk_seed_carries_attention_talk_and_no_target(intentions):
    es = IntentionEffectStore(intentions)
    eff = _seed(es, {"text": "flow's future", "disposition": "someday", "attention": "talk"})
    (unit,) = es.units([(0, eff)])
    assert settle(unit, es, {}).status is EffectStatus.APPLIED
    row = _row(intentions, create_origin_key(PENDING, 0))
    assert (row["disposition"], row.get("attention"), row.get("target")) == ("someday", "talk", None)


def test_the_new_fields_are_signed_in_changes_but_labelled_only_on_the_authority_fields(intentions):
    """Authority-field decision (PLAN 1a): target/next/attention are not in INTENTION_AUTHORITY_FIELDS. They are in the
    effect's changes, so in the signed record bytes; the label projection stays text/domain/disposition."""
    eff = _seed(IntentionEffectStore(intentions), {"text": "x", "disposition": "agenda", "target": "desk",
                                           "next": "2026-11-09T09:00"})
    assert INTENTION_AUTHORITY_FIELDS == ("text", "domain", "disposition")
    assert set(eff.projection) == set(INTENTION_AUTHORITY_FIELDS)
    assert {"target", "next"} <= set(eff.changes)


REFUSALS = [
    ("desk on a someday", {"disposition": "someday", "target": "desk"}, "AGENDA"),
    ("talk on an agenda bound for the desk", {"disposition": "agenda", "target": "desk", "attention": "talk"},
     "desk target files a drop"),
    ("an unknown target", {"disposition": "agenda", "target": "moon"}, "target must be"),
    ("an empty board", {"disposition": "agenda", "target": "board:"}, "target must be"),
    ("attention other than talk on a someday", {"disposition": "someday", "attention": "none"}, "AGENDA"),
    ("an unknown attention", {"disposition": "agenda", "attention": "loud"}, "attention must be"),
    ("a spaced timed next", {"disposition": "agenda", "next": "2026-11-09 09:00"}, "next must be"),
    ("a next with seconds", {"disposition": "agenda", "next": "2026-11-09T09:00:00"}, "next must be"),
    ("an impossible clock", {"disposition": "agenda", "next": "2026-11-09T25:00"}, "next must be"),
    ("an impossible date", {"disposition": "agenda", "next": "2026-13-01"}, "next must be"),
    ("a non-string next", {"disposition": "agenda", "next": 20261109}, "next must be"),
]


@pytest.mark.parametrize("name,extra,why", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_steering_is_refused_never_coerced(intentions, name, extra, why):
    with pytest.raises(ValueError) as e:
        _seed(IntentionEffectStore(intentions), {"text": "words", **extra})
    assert why in str(e.value)


# --- 2: the tier table ---------------------------------------------------------------------------------------------

def test_the_intake_tier_table():
    """THE TIER TABLE (PLAN section 3, decided by L1): drop T2 (signed: it wakes the desk at its minute with his words),
    talk T2 (an intention agents read, as every intention_seed), remind T2 (REMIND_TIER, an open call of Phill's: the
    signature is the confirm step), sort C1 (the room composer's own desk drop, which is never authority). intention_seed
    stays T2 whatever it carries."""
    assert INTAKE_TIERS == {"drop": "T2", "talk": "T2", "remind": "T2", "sort": "C1"}
    assert INTAKE_TIERS["remind"] == REMIND_TIER
    reg = VerbRegistry()
    spec = reg.get(INTAKE_VERB)
    assert spec is not None and spec.binding == "create" and spec.fields == ("line",) and spec.floor == "T2"
    assert spec.device_ok and INTAKE_VERB in DEVICE_VERBS
    today = NOW_LOCAL.date()
    tier = lambda line: spec.tier_fn({"line": line}, None, "unverified", today)  # noqa: E731
    assert [tier(l) for l in ("@mon 9am call X", "  @+2h x", "? talk about y", "plain words", "", 7,
                              "remind me thu 3pm call", "remind me every sun 9pm bins", "remind me to call Jim")] == \
        ["T2", "T2", "T2", "C1", "C1", "T2", "T2", "T2", "C1"]
    seed = reg.get("intention_seed")
    assert seed.fields == ("text", "type", "disposition", "next", "target", "attention", "recur")
    assert seed.tier_fn({"text": "x", "disposition": "agenda", "target": "desk"}, None, "verified", today) == "T2"
    assert {v.name for v in KERNEL_VERBS}.issuperset({"intention_seed", INTAKE_VERB})


def test_intake_kind_and_seed_params():
    assert [intake_kind_of(x) for x in ("@x", "?x", "x", None, "remind me thu 3pm x", "remind me to x")] == \
        ["drop", "talk", "sort", None, "remind", "sort"]
    assert intake_seed_params({"kind": "drop", "when": "2026-11-09T09:00", "text": "call X"}) == {
        "type": "task", "text": "call X", "disposition": "agenda", "target": "desk", "next": "2026-11-09T09:00"}
    assert intake_seed_params({"kind": "talk", "text": "y"}) == {
        "type": "thought", "text": "y", "disposition": "someday", "attention": "talk"}
    assert intake_seed_params({"kind": "remind", "when": "2026-11-12T15:00", "text": "call"}) == {
        "type": "task", "text": "call", "disposition": "agenda", "target": REMIND_TARGET, "next": "2026-11-12T15:00"}
    assert intake_seed_params({"kind": "remind", "when": "2026-11-15T21:00", "text": "bins", "recur": "weekly SUN 21:00"}) == {
        "type": "task", "text": "bins", "disposition": "agenda", "target": REMIND_TARGET, "next": "2026-11-15T21:00",
        "recur": "weekly SUN 21:00"}
    with pytest.raises(ValueError):
        intake_seed_params({"kind": "sort", "text": "z"})


# --- 3: list_intake through the broker ----------------------------------------------------------------------------

def _issue(b: Broker, line: Any, *, intent: str = "reported", credential: dict = BROWSER, tier: str = "T2"):
    return b.issue(verb=INTAKE_VERB, params={"line": line}, target={"kind": "create", "panel_id": "list"},
                   tier=tier, credential=credential, intent=intent)


def test_a_scheduled_drop_plants_an_agenda_for_the_desk_at_the_resolved_minute(broker, intentions):
    out = _issue(broker, "@+2h call the dentist")
    want = (NOW_LOCAL + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    assert out["state"] == "COMMITTED"
    row = _row(intentions, "ck." + out["pending_id"][2:] + ".0")
    assert (row["text"], row["disposition"], row["target"], row["next"]) == \
        ("call the dentist", "agenda", "desk", want)
    assert out["receipt"] == {"kind": "drop", "id": row["id"], "fires": want, "text": "call the dentist"}
    assert out["receipt_line"] == intake.receipt_line(out["receipt"])
    assert out["receipt_line"].startswith(f"scheduled drop {row['id']} fires ")


def test_a_talk_line_plants_a_talk_seed(broker, intentions):
    out = _issue(broker, "?  flow's future, with Som out of it ")
    row = _row(intentions, "ck." + out["pending_id"][2:] + ".0")
    assert (row["disposition"], row.get("attention"), row.get("target"), row["text"]) == \
        ("someday", "talk", None, "flow's future, with Som out of it")
    assert out["receipt"] == {"kind": "talk", "id": row["id"], "fires": None, "text": row["text"]}
    assert out["receipt_line"] == f"talk {row['id']}"


def test_a_signed_drop_waits_and_its_receipt_claims_no_fire(broker, intentions):
    out = _issue(broker, "@mon 9am call X", intent="sign")
    assert out["state"] == "WAITING"
    assert out["receipt"]["id"] is None and out["receipt"]["kind"] == "drop"
    assert out["receipt_line"].endswith(", awaiting your signature") and "intention-" not in out["receipt_line"]
    rec = json.loads(out["record"])
    # the signed bytes carry the reading twice: the target's intake and the effect's changes
    assert rec["target"]["intake"]["when"] == out["receipt"]["fires"]
    assert rec["effects"][0]["changes"]["next"] == out["receipt"]["fires"]
    assert rec["effects"][0]["changes"]["target"] == "desk"
    assert out["receipt"]["fires"].endswith("T09:00")


def test_the_decision_rebuilds_from_the_recorded_reading_never_the_line_again(broker, intentions):
    out = _issue(broker, "@+30m x", intent="sign")
    target = json.loads(out["record"])["target"]
    # a minute later "+30m" reads another minute: the rebuild ignores the line and keeps the signed one
    broker._test_now[0] = NOW_UTC + timedelta(minutes=1)
    built = broker._build_intake({"line": "@+30m x"}, target, out["pending_id"], at_issue=False)
    assert built.effects[0].changes["next"] == target["intake"]["when"]
    assert built.target == target
    # signed after its minute: it no longer builds, so the op ends STALE (typed again, never fired late)
    broker._test_now[0] = NOW_UTC + timedelta(minutes=31)
    with pytest.raises(ValueError, match="passed before it was signed"):
        broker._build_intake({"line": "@+30m x"}, target, out["pending_id"], at_issue=False)


INTAKE_REFUSALS = [
    ("past", "@2020-01-01 9am x", "not in the future"),
    ("unparseable", "@someday x", "cannot read"),
    ("bad clock", "@25:00 x", "does not read as a time"),
    ("no words after the time", "@tomorrow 9am", "no words after the time"),
    ("no time after @", "@", "no time after"),
    ("empty talk", "?   ", "no words after '?'"),
    ("empty line", "   ", "an empty line"),
]


@pytest.mark.parametrize("name,line,why", INTAKE_REFUSALS, ids=[r[0] for r in INTAKE_REFUSALS])
def test_a_line_that_does_not_read_is_refused_with_its_reason_and_writes_nothing(broker, intentions, name, line, why):
    before = intentions.read_bytes()
    with pytest.raises(BrokerError) as e:
        _issue(broker, line)
    assert (e.value.code, e.value.status) == ("intake_refused", 422) and why in str(e.value)
    assert intentions.read_bytes() == before


def test_the_broker_builds_a_sort_as_one_desk_drop_and_refuses_a_malformed_body(broker, intentions, home):
    from levain.cockpit.desk_effects import DESK, CoordDropEffectStore

    # no desk store registered: the sort keeps its 501
    with pytest.raises(BrokerError) as e:
        _issue(broker, "plain words", credential=DEVICE, tier="C1", intent="sign")
    assert e.value.status == 501
    broker.stores[DESK] = CoordDropEffectStore(lambda: home / "dispatch")
    out = _issue(broker, "plain words", credential=DEVICE, tier="C1", intent="sign")
    assert out["state"] == "WAITING"
    (eff,) = json.loads(out["record"])["effects"]
    assert (eff["store"], eff["changes"]) == (DESK, {"text": "plain words"})
    with pytest.raises(BrokerError) as e:
        broker.issue(verb=INTAKE_VERB, params={"line": "@+1h x", "next": "2026-11-09T09:00"},
                     target={"kind": "create", "panel_id": "list"}, tier="T2", credential=BROWSER, intent="reported")
    assert e.value.status == 400


# --- 4: the C1 sort -----------------------------------------------------------------------------------------------

def test_a_plain_line_is_a_cockpit_drop_for_the_desk_to_sort(home):
    from levain.desk.store import CoordStore

    d = home / "dispatch"
    out = _intake_fire(None, {"line": "  look at the sticky note  "},  # type: ignore[arg-type]
                       {"dispatch_dir": d, "credential": "browser", "idempotency_key": "k-1"}, False)
    assert out["receipt"] == {"kind": "sort", "id": None, "fires": None, "text": "look at the sticky note"}
    assert out["receipt_line"] == "sent to the desk to sort"
    drop = CoordStore(d).drop(out["drop_id"])
    assert (drop.surface, drop.words) == ("cockpit", out["receipt"]["text"])


def test_an_unkeyed_sort_is_refused_before_anything_is_written(home):
    """codex L3 r2: a retry of an unkeyed sort would write the drop twice, so the key is required (as the room's
    composer requires its outbox id)."""
    from levain.desk.store import CoordStore

    d = home / "dispatch"
    for tgt in ({"dispatch_dir": d}, {"dispatch_dir": d, "idempotency_key": ""}):
        with pytest.raises(EditError) as e:
            _intake_fire(None, {"line": "words"}, tgt, False)  # type: ignore[arg-type]
        assert (e.value.code, e.value.http_status) == ("bad_request", 400)
    assert not d.exists() or CoordStore(d).drops() == []


@pytest.mark.parametrize("params,code,status", [
    ({"line": "@+1h x"}, "bad_request", 400),        # a drop is signed, never written here
    ({"line": "? y"}, "bad_request", 400),
    ({"line": ""}, "intake_refused", 422),
    ({"line": 3}, "bad_request", 400),
    ({"line": "x", "target": "desk"}, "bad_request", 400),
])
def test_the_sort_fire_refuses_everything_but_a_plain_line(home, params, code, status):
    with pytest.raises(EditError) as e:
        _intake_fire(None, params, {"dispatch_dir": home / "dispatch"}, False)  # type: ignore[arg-type]
    assert (e.value.code, e.value.http_status) == (code, status)


def test_the_sort_fire_needs_a_desk_store():
    with pytest.raises(EditError) as e:
        _intake_fire(None, {"line": "x"}, {}, False)  # type: ignore[arg-type]
    assert e.value.http_status == 501


def test_a_keyed_sort_resent_after_a_lost_reply_writes_one_drop(home):
    from levain.desk.store import CoordStore

    d = home / "dispatch"
    tgt = {"dispatch_dir": d, "credential": "browser", "idempotency_key": "intake-1"}
    first = _intake_fire(None, {"line": "the sticky note"}, tgt, False)  # type: ignore[arg-type]
    again = _intake_fire(None, {"line": "the sticky note"}, tgt, False)  # type: ignore[arg-type]
    assert again["drop_id"] == first["drop_id"] and (first["replay"], again["replay"]) == (False, True)
    assert [x.words for x in CoordStore(d).drops()] == ["the sticky note"]
    # the same key with other words is a conflict, never a second drop; another credential's key is its own
    with pytest.raises(EditError) as e:
        _intake_fire(None, {"line": "other words"}, tgt, False)  # type: ignore[arg-type]
    assert e.value.code == "idempotency_conflict"
    other = _intake_fire(None, {"line": "the sticky note"}, {**tgt, "credential": "phone"}, False)  # type: ignore[arg-type]
    assert other["drop_id"] != first["drop_id"]


class _Raise:
    """A standing policy that raises the named verbs to a tier (the Protocol's one method)."""

    def __init__(self, **floors: str) -> None:
        self.floors = floors

    def apply(self, verb: str, tier: str) -> str:
        return self.floors.get(verb, tier)


def test_a_raise_of_intention_seed_governs_a_drop_and_a_talk_never_a_sort():
    """codex L3 r1 HIGH: list_intake plants intention_seed's effect, so intention_seed's standing raise is not stepped around."""
    spec = VerbRegistry().get(INTAKE_VERB)
    today = NOW_LOCAL.date()
    pol = _Raise(intention_seed="T3")
    assert [spec.tier({"line": l}, None, "verified", today, policy=pol) for l in ("@+1h x", "? y", "z", "remind me +1h x")] == \
        ["T3", "T3", "C1", "T3"]
    assert spec.rendered_tier(None, "verified", today, policy=pol) == "T3"
    assert spec.head("sign", policy=pol)["tier"] == "T3"
    # its own raise still governs every kind
    assert spec.tier({"line": "z"}, None, "verified", today, policy=_Raise(list_intake="T2")) == "T2"
    assert spec.idempotent and spec.policy_also is not None


def test_a_drop_decided_inside_its_own_minute_still_builds_and_a_committed_receipt_never_says_not_written(broker):
    out = _issue(broker, "@+1m x", intent="sign")
    target = json.loads(out["record"])["target"]
    broker._test_now[0] = NOW_UTC + timedelta(minutes=1, seconds=30)      # inside the drop's own minute
    built = broker._build_intake({"line": "@+1m x"}, target, out["pending_id"], at_issue=False)
    assert built.effects[0].changes["next"] == target["intake"]["when"]
    got = broker._intake_receipt({"target": target}, "cp" + "ee" * 16, "COMMITTED")   # no row under that key
    assert got["receipt"]["id"] is None and got["receipt_line"].endswith(", written")
    assert "not written" not in got["receipt_line"]


# --- 5: the reminder (row 188): `remind [me] <when|every ...> words` plants an agenda that rings for HIM ----------------------

def test_a_timed_reminder_plants_an_agenda_for_him_at_its_minute(broker, intentions):
    out = _issue(broker, "remind me +2h to call the dentist")
    want = (NOW_LOCAL + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    assert out["state"] == "COMMITTED"
    row = _row(intentions, "ck." + out["pending_id"][2:] + ".0")
    assert (row["text"], row["disposition"], row["target"], row["next"]) == \
        ("call the dentist", "agenda", REMIND_TARGET, want)
    assert not row.get("recur") and not row.get("recur_anchor")
    assert out["receipt"] == {"kind": "remind", "id": row["id"], "fires": want, "recur": None, "text": "call the dentist"}
    assert out["receipt_line"] == intake.receipt_line(out["receipt"])
    assert out["receipt_line"].startswith(f"reminder {row['id']} rings ") and "then" not in out["receipt_line"]


def test_a_repeating_reminder_stores_its_canon_rule_and_anchors_it_on_its_first_minute(broker, intentions):
    out = _issue(broker, "remind me every sun 9pm bins")
    row = _row(intentions, "ck." + out["pending_id"][2:] + ".0")
    first = intake.parse_intake("remind me every sun 9pm bins", NOW_LOCAL)["when"]
    assert (row["target"], row["next"], row["recur"], row["recur_anchor"]) == (REMIND_TARGET, first, "weekly SUN 21:00", first)
    assert out["receipt"]["recur"] == "weekly SUN 21:00"
    assert out["receipt_line"].endswith(", then weekly SUN 21:00")


def test_a_signed_reminder_waits_and_its_receipt_names_the_repeat_before_the_signature(broker, intentions):
    out = _issue(broker, "remind me every weekday 8am meds", intent="sign")
    assert out["state"] == "WAITING" and out["receipt"]["id"] is None and out["receipt"]["kind"] == "remind"
    assert out["receipt_line"].startswith("reminds you ")
    assert out["receipt_line"].endswith(", then weekly MON,TUE,WED,THU,FRI 08:00, awaiting your signature")
    rec = json.loads(out["record"])
    # the reading and the rule ride in the signed bytes twice, and the anchor is the first minute
    assert rec["target"]["intake"]["recur"] == "weekly MON,TUE,WED,THU,FRI 08:00"
    ch = rec["effects"][0]["changes"]
    assert (ch["recur"], ch["recur_anchor"], ch["target"], ch["next"]) == \
        ("weekly MON,TUE,WED,THU,FRI 08:00", ch["next"], REMIND_TARGET, rec["target"]["intake"]["when"])


def test_a_reminder_signed_after_its_first_minute_no_longer_builds(broker, intentions):
    out = _issue(broker, "remind me +30m x", intent="sign")
    target = json.loads(out["record"])["target"]
    broker._test_now[0] = NOW_UTC + timedelta(minutes=31)
    with pytest.raises(ValueError, match="reminder's minute .* passed before it was signed"):
        broker._build_intake({"line": "remind me +30m x"}, target, out["pending_id"], at_issue=False)


@pytest.mark.parametrize("line,why", [
    ("remind me thu 3 call", "without am/pm"),
    ("remind me next thu call", "read two ways"),
    ("remind me every week x", "which day"),
    ("remind me tomorrow 9am", "no words after the time"),
    ("remind me every ² days x", "not a repeat this can store"),
])
def test_a_reminder_that_does_not_read_is_refused_with_its_reason_and_writes_nothing(broker, intentions, line, why):
    before = intentions.read_bytes()
    with pytest.raises(BrokerError) as e:
        _issue(broker, line)
    assert (e.value.code, e.value.status) == ("intake_refused", 422) and why in str(e.value)
    assert intentions.read_bytes() == before


def test_prose_that_starts_with_remind_still_sorts_to_the_desk(broker):
    """`remind me to call Jim` has no time word right after it: it is the sort it always was (a 501 here, no desk store)."""
    with pytest.raises(BrokerError) as e:
        _issue(broker, "remind me to call Jim", credential=DEVICE, tier="C1", intent="sign")
    assert e.value.status == 501


@pytest.mark.parametrize("extra,why", [
    ({"recur": "weekly SUN 21:00", "disposition": "agenda", "target": "desk", "next": "2026-11-15T21:00"}, "repeating reminder"),
    ({"recur": "weekly SUN 21:00", "disposition": "agenda", "target": "nudge"}, "repeating reminder"),
    ({"recur": "weekly SUN 21:00", "disposition": "someday"}, "repeating reminder"),
    ({"recur": "hourly 5", "disposition": "agenda", "target": "nudge", "next": "2026-11-15T21:00"}, "bad recurrence rule"),
])
def test_a_repeat_the_store_cannot_ring_is_refused_never_stored_without_its_series(intentions, extra, why):
    with pytest.raises(ValueError) as e:
        _seed(IntentionEffectStore(intentions), {"text": "x", **extra})
    assert why in str(e.value)


def test_the_remind_tier_is_one_named_constant_the_tier_table_reads():
    """The open call (P2): T2 now, behind REMIND_TIER. A lighter tier is NOT built: the constant alone does not switch it."""
    assert REMIND_TIER == "T2" and INTAKE_TIERS["remind"] is REMIND_TIER
