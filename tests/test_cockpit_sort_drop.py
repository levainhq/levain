"""A phone's plain List line (a *sort*), signed, becomes one desk drop (levain 67, key 1108-18): the
``coordinator_drop`` effect on ``CoordDropEffectStore``, through a real Broker, a real passkey-signed device pending and
a real ``CoordStore`` in a tmp dir. Nothing here touches an operator's live apps."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

pytest.importorskip("cryptography")
from anneal_memory.spores import SporeStore  # noqa: E402

from levain import intake  # noqa: E402
from levain.cockpit.broker import Broker, BrokerError  # noqa: E402
from levain.cockpit.desk_effects import (  # noqa: E402
    COORD_CANONICAL,
    DESK,
    CoordDropEffectStore,
    desk_drop_effect,
    drop_resource_key,
)
from levain.cockpit.effects import (  # noqa: E402
    INTAKE_TIERS,
    INTAKE_VERB,
    ApplyOutcome,
    ApplyUnit,
    EffectStatus,
    SporeEffectStore,
    StoreGone,
    create_origin_key,
    registration_refusals,
    settle,
)
from levain.desk.store import CoordStore  # noqa: E402
from tests.autonomic_confirm_keys import anchor_scratch  # noqa: E402
from tests.cockpit_authority_rig import RealKey, enrol, enrol_any  # noqa: E402
from tests.test_cockpit_k8a_phone_authority import SoftPasskey  # noqa: E402

PID = "cp" + "ab" * 16
BROWSER = {"class": "token", "name": "browser"}
WORDS = "look at the sticky note on the fridge"


@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("sortdrop-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def world(home: Path) -> Iterator[SimpleNamespace]:
    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    qdir = home / "dispatch"
    qdir.mkdir(mode=0o700)
    where: list[Path | None] = [qdir]
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores),
                                      DESK: CoordDropEffectStore(lambda: where[0])},
               store_paths=[spores], install_class="reported")
    b.start()
    keys = home / "keys"
    keys.mkdir(mode=0o700)
    laptop = RealKey(keys, "laptop")
    assert b.db is not None
    enrol_any(b.db, laptop, "laptop-presence")
    phone = SoftPasskey()
    enrol(b.db, phone, "display-phone", by=laptop, custody="passkey")    # type: ignore[arg-type]
    try:
        yield SimpleNamespace(b=b, phone=phone, qdir=qdir, where=where, spores=spores,
                              dev={"class": "device", "fingerprint": phone.fingerprint, "transport": "paired-a"})
    finally:
        b.stop()


def _issue(w: SimpleNamespace, line: str = WORDS) -> dict[str, Any]:
    return w.b.issue(verb=INTAKE_VERB, params={"line": line}, target={"kind": "create", "panel_id": "tray"},
                     tier="C1", credential=w.dev, intent="sign")


def _drops(qdir: Path) -> list[Any]:
    return CoordStore(qdir).drops()


# --- the effect itself ------------------------------------------------------------------------------------------

def test_the_effect_is_one_create_of_the_words_and_refuses_anything_else():
    ok = create_origin_key(PID, 0)
    eff = desk_drop_effect({"text": WORDS}, None, origin_key=ok)
    assert (eff.store, eff.resource_key, eff.is_create) == (DESK, drop_resource_key(ok), True)
    assert dict(eff.changes) == {"text": WORDS} and eff.writes == ("text",)
    assert desk_drop_effect({"text": WORDS}, None, origin_key=ok) == eff       # pure
    for bad in ({}, {"text": ""}, {"text": 3}, {"text": "x", "when": "y"}):
        with pytest.raises(ValueError):
            desk_drop_effect(bad, None, origin_key=ok)
    with pytest.raises(ValueError):
        desk_drop_effect({"text": "x"}, {"drop_id": "d"}, origin_key=ok)
    with pytest.raises(ValueError):
        desk_drop_effect({"text": "x"}, None, origin_key=None)
    assert registration_refusals(CoordDropEffectStore(lambda: None), COORD_CANONICAL) == []


def test_the_create_origin_key_fits_the_desks_outbox_rule(home):
    ok = create_origin_key(PID, 0)
    assert ok == "ck." + "ab" * 16 + ".0"
    drop, created = CoordStore(home / "d").accept_drop("words", surface="cockpit", outbox_id=ok)
    assert created and drop.outbox_id == ok


def test_applying_twice_is_one_drop_and_the_second_is_already(home):
    st = CoordDropEffectStore(lambda: home / "d")
    eff = desk_drop_effect({"text": WORDS}, None, origin_key=create_origin_key(PID, 0))
    unit = ApplyUnit(DESK, (0,), (eff,))
    first, second = st.apply_unit(unit), st.apply_unit(unit)
    assert (first.outcome, second.outcome) == (ApplyOutcome.APPLIED, ApplyOutcome.ALREADY)
    assert first.members[0].native_id == second.members[0].native_id
    assert [d.words for d in _drops(home / "d")] == [WORDS]
    # a recovery's settle after the write (a crash before the record) reads it as APPLIED, with the label
    res = settle(unit, st, {})
    assert res.status is EffectStatus.APPLIED and res.members[0].label_fields == ("text",)
    assert st.snapshot(eff.resource_key).row["text"] == WORDS


def test_other_words_under_the_same_key_are_refused_not_retried(home):
    st = CoordDropEffectStore(lambda: home / "d")
    ok = create_origin_key(PID, 0)
    st.apply_unit(ApplyUnit(DESK, (0,), (desk_drop_effect({"text": "one"}, None, origin_key=ok),)))
    other = desk_drop_effect({"text": "two"}, None, origin_key=ok)
    res = settle(ApplyUnit(DESK, (0,), (other,)), st, {})
    assert (res.status, res.reason) == (EffectStatus.UNAPPLIED, "refused")
    assert [d.words for d in _drops(home / "d")] == ["one"]


def test_no_directory_is_store_gone_and_a_read_makes_nothing(home):
    st = CoordDropEffectStore(lambda: None)
    eff = desk_drop_effect({"text": WORDS}, None, origin_key=create_origin_key(PID, 0))
    with pytest.raises(StoreGone):
        st.apply_unit(ApplyUnit(DESK, (0,), (eff,)))
    res = settle(ApplyUnit(DESK, (0,), (eff,)), st, {})
    assert (res.status, res.reason) == (EffectStatus.UNAPPLIED, "store gone")
    absent = CoordDropEffectStore(lambda: home / "nowhere")
    assert absent.snapshot(eff.resource_key).row is None
    assert not (home / "nowhere").exists()


# --- through the broker -----------------------------------------------------------------------------------------

def test_the_tier_table_is_unchanged_a_sort_is_still_c1():
    assert INTAKE_TIERS == {"drop": "T2", "talk": "T2", "sort": "C1"}


def test_a_signed_sort_writes_exactly_one_drop_with_the_words_verbatim(world):
    w = world
    out = _issue(w, f"  {WORDS}  ")
    assert out["state"] == "WAITING" and _drops(w.qdir) == []      # nothing before the signature
    rec = json.loads(out["record"])
    ok = create_origin_key(out["pending_id"], 0)
    assert rec["effects"][0]["store"] == DESK and rec["effects"][0]["changes"] == {"text": WORDS}
    assert rec["target"]["intake"] == {"kind": "sort", "text": WORDS}
    assert out["receipt_line"].endswith("awaiting your signature") and "desk to sort" in out["receipt_line"]
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert (done["state"], done["decision_kind"]) == ("COMMITTED", "ALLOWED")
    (drop,) = _drops(w.qdir)
    assert (drop.words, drop.surface, drop.outbox_id) == (WORDS, "cockpit", ok)
    assert done["receipt"] == {"kind": "sort", "id": None, "fires": None, "text": WORDS}
    assert done["receipt_line"] == "sent to the desk to sort" and done["drop_id"] == drop.drop_id
    # accept_drop wrote the delivered receipt in the same transaction
    conn = CoordStore(w.qdir).open_readonly()
    try:
        n = conn.execute("SELECT COUNT(*) FROM replies WHERE kind = 'drop_receipt'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    # a second submit of the settled pending is a replay: still one drop
    again = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert again["state"] == "COMMITTED" and len(_drops(w.qdir)) == 1


def test_a_sort_with_no_desk_directory_is_a_501_at_issue_and_writes_nothing(world):
    w = world
    w.where[0] = None
    with pytest.raises(BrokerError) as e:
        _issue(w)
    assert (e.value.code, e.value.status) == ("not_built", 501)
    assert "no desk store" in str(e.value) and _drops(w.qdir) == []


def test_a_sort_with_no_desk_store_registered_is_a_501(home):
    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported")
    b.start()
    try:
        with pytest.raises(BrokerError) as e:
            b.issue(verb=INTAKE_VERB, params={"line": "plain words"}, target={"kind": "create", "panel_id": "tray"},
                    tier="C1", credential={"class": "device", "fingerprint": "SHA256:" + "B" * 43,
                                           "transport": "run"}, intent="sign")
        assert e.value.status == 501
    finally:
        b.stop()


def test_the_store_going_away_between_issue_and_decide_is_unapplied_store_gone(world):
    w = world
    out = _issue(w)
    w.where[0] = None
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert done["state"] != "COMMITTED"
    assert _drops(w.qdir) == []
    assert "store gone" in json.dumps(w.b.outcome(out["pending_id"]))


def test_a_tampered_reading_does_not_rebuild_the_signed_effect(world):
    w = world
    out = _issue(w)
    rec = json.loads(out["record"])
    signed = rec["effects"][0]
    target = rec["target"]
    same = w.b._build_intake({"line": WORDS}, target, out["pending_id"], at_issue=False)
    assert same.effects[0].to_record() == signed
    # changed words: the rebuild differs from the signed effect, so the decision's compare refuses it
    other = w.b._build_intake({"line": WORDS}, {**target, "intake": {"kind": "sort", "text": "other"}},
                              out["pending_id"], at_issue=False)
    assert other.effects[0].to_record() != signed
    # changed kind: a drop reading carries no minute, so it does not build at all
    with pytest.raises(ValueError):
        w.b._build_intake({"line": WORDS}, {**target, "intake": {"kind": "drop", "text": WORDS}},
                          out["pending_id"], at_issue=False)
    # no reading at all
    with pytest.raises(ValueError):
        w.b._build_intake({"line": WORDS}, {"kind": "create"}, out["pending_id"], at_issue=False)


def test_a_malformed_body_is_still_refused(world):
    w = world
    with pytest.raises(BrokerError) as e:
        w.b.issue(verb=INTAKE_VERB, params={"line": "x", "extra": 1}, target={"kind": "create", "panel_id": "tray"},
                  tier="C1", credential=w.dev, intent="sign")
    assert e.value.status == 400
    assert intake.receipt({"kind": "sort", "text": "x"})[0] == "sent to the desk to sort"


# --- the fix lane's pins (L1/L2 on 5ba2ea10) ---------------------------------------------------------------------

def _sql(w: SimpleNamespace, sql: str, *args: Any) -> list[tuple]:
    import sqlite3

    conn = sqlite3.connect(w.b.data_root / "cockpit.db")
    try:
        conn.commit()
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("name,line", [("over length", "x" * 20_001), ("NUL", "a\x00b"),
                                       ("lone surrogate", "a\ud800b")])
def test_a_line_the_store_would_refuse_is_refused_at_issue_before_any_signature(world, name, line):
    w = world
    with pytest.raises(BrokerError) as e:
        _issue(w, line)
    assert (e.value.code, e.value.status) == ("refused", 409)
    assert _drops(w.qdir) == [] and _sql(w, "SELECT COUNT(*) FROM pendings") == [(0,)]
    with pytest.raises(ValueError):
        desk_drop_effect({"text": line}, None, origin_key=create_origin_key(PID, 0))


def test_a_line_at_the_limit_still_issues(world):
    assert _issue(world, "x" * 20_000)["state"] == "WAITING"


def test_the_receipt_says_in_the_operators_words_why_a_sort_was_not_sent(world):
    rec = {"target": {"intake": {"kind": "sort", "text": "x"}}}
    line = lambda st: world.b._intake_receipt(rec, PID, st)["receipt_line"]   # noqa: E731
    assert line("COMMITTED_INCOMPLETE") == "not sent to the desk: the desk's store refused it"
    assert line("STALE") == "not sent to the desk: another drop already holds its place"
    for other in ("REFUSED", "EXPIRED", "CANCELLED"):
        assert line(other) == "not sent to the desk"
    for st in ("COMMITTED_INCOMPLETE", "STALE", "REFUSED"):
        assert st.lower() not in line(st)


def test_recent_edits_read_a_sort_as_a_line_sent_to_the_desk():
    from levain.cockpit.providers import _edit_target, plain_edit_title

    assert plain_edit_title("list_intake", _edit_target(drop_resource_key("ck.ab.0")), "device:ab12") == \
        "added a line, sent to the desk to sort, from a signed device"
    assert plain_edit_title("list_intake", _edit_target("spore:spore-017"), "token:browser:1") == \
        "added a line item spore-017, from the browser"


def test_a_desk_directory_that_is_not_0700_is_refused_not_retried(home):
    from levain.cockpit.effects import PermanentApplyRefusal

    d = home / "open"
    d.mkdir(mode=0o700)
    d.chmod(0o755)
    st = CoordDropEffectStore(lambda: d)
    eff = desk_drop_effect({"text": WORDS}, None, origin_key=create_origin_key(PID, 0))
    unit = ApplyUnit(DESK, (0,), (eff,))
    with pytest.raises(PermanentApplyRefusal):
        st.apply_unit(unit)
    res = settle(unit, st, {})
    assert (res.status, res.reason) == (EffectStatus.UNAPPLIED, "refused")


def test_a_pending_whose_stored_reading_was_altered_is_refused_and_writes_nothing(world):
    """The reading and the signed effect must agree: a stored record whose intake text differs from its own effect
    (signed as it stands) rebuilds to a different record with every ``expect`` equal, which the journal classes REFUSED
    "normaliser" (not STALE: nothing moved, the record is inconsistent). Nothing is written."""
    import sqlite3

    from levain.cockpit.consent import encode, record_sha256

    w = world
    out = _issue(w)
    rec = json.loads(out["record"])
    rec["target"]["intake"]["text"] = "other words than the signed effect"
    raw = encode(rec)
    conn = sqlite3.connect(w.b.data_root / "cockpit.db")
    try:
        conn.execute("DROP TRIGGER pendings_authority_immutable")
        conn.execute("UPDATE pendings SET record = ?, record_sha256 = ? WHERE pending_id = ?",
                     (raw, record_sha256(raw), out["pending_id"]))
        conn.commit()
    finally:
        conn.close()
    done = w.b.submit(out["pending_id"], w.phone.sign(raw))
    assert done["state"] == "REFUSED", done
    assert _sql(w, "SELECT reason FROM pendings WHERE pending_id = ?", out["pending_id"]) == [("normaliser",)]
    assert _drops(w.qdir) == [] and done["receipt_line"] == "not sent to the desk"


def test_a_sort_whose_key_is_already_taken_before_the_decision_ends_stale_and_writes_nothing_more(world):
    w = world
    out = _issue(w)
    CoordStore(w.qdir).accept_drop("somebody else's words", surface="cockpit",
                                   outbox_id=create_origin_key(out["pending_id"], 0))
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert done["state"] == "STALE", done
    assert [d.words for d in _drops(w.qdir)] == ["somebody else's words"]
    assert done["receipt_line"] == "not sent to the desk: another drop already holds its place"


def test_a_crash_between_the_write_and_its_record_recovers_to_exactly_one_drop(world):
    w = world
    out = _issue(w)
    armed = [True]
    real = w.b._kp

    def crash(name: str) -> None:
        if armed[0] and name == "applied:0":
            raise RuntimeError("the process died between the write and its record")
        real(name)

    w.b._kp = crash                                   # type: ignore[method-assign]
    try:
        w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    except Exception:                                 # noqa: BLE001 - the simulated death
        pass
    assert len(_drops(w.qdir)) == 1                                           # written ...
    assert _sql(w, "SELECT state FROM pendings WHERE pending_id = ?", out["pending_id"]) == [("DECIDED",)]
    assert _sql(w, "SELECT status FROM effects WHERE operation_id = ?", out["pending_id"]) == [("PLANNED",)]  # ... unrecorded
    armed[0] = False
    w.b.recover()
    assert _sql(w, "SELECT state FROM pendings WHERE pending_id = ?", out["pending_id"]) == [("COMMITTED",)]
    assert _sql(w, "SELECT status FROM effects WHERE operation_id = ?", out["pending_id"]) == [("APPLIED",)]
    (drop,) = _drops(w.qdir)
    assert drop.words == WORDS and drop.outbox_id == create_origin_key(out["pending_id"], 0)


# --- the fix lane's pins (L3 r1 on 41bfeff6) ----------------------------------------------------------------------

def test_the_server_built_broker_writes_a_signed_sort_into_the_consent_readers_directory(home, monkeypatch):
    """``web_server._build_broker``'s ``coord_dir`` closure, driven end to end: the consent reader is attached AFTER
    the broker is built (as ``make_server`` does) and the sort lands in its directory."""
    from levain import web_server

    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    anneal = SimpleNamespace(spores_json=spores, episodic_db=home / "episodes.db", continuity_md=home / "other.md")
    source = SimpleNamespace(write_scope=SimpleNamespace(ledger_root=home / "ledger", anneal=anneal))
    (home / "ledger").mkdir(mode=0o700)
    httpd = SimpleNamespace(verb_registry=None, get_cockpit=lambda: None)         # no consent_reader yet
    monkeypatch.setattr(web_server, "retier", lambda *a, **k: "C1")
    b = web_server._build_broker(source, "reported", httpd)
    assert b is not None
    qdir = home / "dispatch"
    qdir.mkdir(mode=0o700)
    httpd.consent_reader = SimpleNamespace(directory=qdir)                         # attached after, as the server does
    b.start()
    try:
        keys = home / "keys"
        keys.mkdir(mode=0o700)
        laptop = RealKey(keys, "laptop")
        enrol_any(b.db, laptop, "laptop-presence")
        phone = SoftPasskey()
        enrol(b.db, phone, "display-phone", by=laptop, custody="passkey")          # type: ignore[arg-type]
        cred = {"class": "device", "fingerprint": phone.fingerprint, "transport": "paired-a"}
        out = b.issue(verb=INTAKE_VERB, params={"line": WORDS}, target={"kind": "create", "panel_id": "tray"},
                      tier="C1", credential=cred, intent="sign")
        done = b.submit(out["pending_id"], phone.sign(out["record"].encode()))
        assert (done["state"], done["decision_kind"]) == ("COMMITTED", "ALLOWED")
        (drop,) = _drops(qdir)
        assert (drop.words, drop.surface) == (WORDS, "cockpit")
    finally:
        b.stop()


def test_a_registered_store_without_a_directory_is_the_same_typed_501(home):
    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    bare = SimpleNamespace(name=DESK)               # registered under "desk", no directory()
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores),
                                      DESK: CoordDropEffectStore(lambda: None)},
               store_paths=[spores], install_class="reported")      # registration needs a real adapter; swapped below
    b.start()
    try:
        for cs in (bare, SimpleNamespace(name=DESK, directory=lambda: None)):
            b.stores[DESK] = cs                      # type: ignore[assignment]
            with pytest.raises(BrokerError) as e:
                b.issue(verb=INTAKE_VERB, params={"line": "plain words"}, target={"kind": "create", "panel_id": "tray"},
                        tier="C1", credential={"class": "device", "fingerprint": "SHA256:" + "B" * 43,
                                               "transport": "run"}, intent="sign")
            assert (e.value.code, e.value.status) == ("not_built", 501)
    finally:
        b.stop()


def test_a_desk_store_that_cannot_be_read_at_issue_is_a_503_not_a_500(world, monkeypatch):
    from levain.cockpit.effects import TransientApplyFault

    w = world

    def locked(self, resource_key):
        raise TransientApplyFault("OperationalError", "database is locked")

    monkeypatch.setattr(CoordDropEffectStore, "snapshot", locked)
    with pytest.raises(BrokerError) as e:
        _issue(w)
    assert (e.value.code, e.value.status) == ("source_unavailable", 503)
    assert _drops(w.qdir) == []


def test_a_fault_reading_the_drop_row_is_transient_like_the_open(home, monkeypatch):
    from levain.desk.store import CoordError
    from levain.cockpit.effects import TransientApplyFault

    d = home / "d"
    CoordStore(d).accept_drop("words", surface="cockpit", outbox_id=create_origin_key(PID, 0))

    def boom(self, conn, drop_id):
        raise CoordError("the row would not read")

    monkeypatch.setattr(CoordStore, "_drop", boom)
    with pytest.raises(TransientApplyFault):
        CoordDropEffectStore(lambda: d).snapshot(drop_resource_key(create_origin_key(PID, 0)))


def test_a_non_string_edit_target_does_not_take_the_title_down():
    from levain.cockpit.providers import plain_edit_title

    assert plain_edit_title("list_intake", None, "device:ab12") == "added a line, from a signed device"  # type: ignore[arg-type]
    # a truthy non-string target too (L3 r2: codex + deepseek), not only None
    assert plain_edit_title("list_intake", 7, "device:ab12") == "added a line, from a signed device"  # type: ignore[arg-type]


def test_the_store_assigned_fields_are_the_row_the_store_emits():
    from levain.cockpit.desk_effects import DROP_FIELDS, DROP_STORE_ASSIGNED

    drop = SimpleNamespace(words="w", drop_id="d", outbox_id="o", surface="cockpit", words_sha256="s")
    emitted = set(CoordDropEffectStore._row(drop)) - set(DROP_FIELDS)
    assert emitted == set(DROP_STORE_ASSIGNED)
