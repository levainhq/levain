# rename-intention: never-touch
"""H2 of the spore -> intention rename: the cockpit journal an OLD release left on disk is replayed by the new code.

``tests/fixtures/rename_h2/`` holds what the release that still said "spore" wrote (``make_old_journal.py`` drives that
release's own broker; no byte of the fixture comes from the code under test): a COMMITTED op with a label, a
COMMITTED_INCOMPLETE op, four ops left DECIDED at a kill point (an update, a create whose store write was already
durable, a resolve, a promote) and a reported op still WAITING. The signed records name the old effect store and the old
resource-key prefix, so the new broker has to read both as history: an op in flight across the update completes,
exactly once, and a terminal row renders in the new words. New writes use the new spelling.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import IntentionStore

from levain.cockpit.broker import Broker, Lifecycle
from levain.cockpit.consent import parse_stamp
from levain.cockpit.effects import (
    INTENTION_KEY_PREFIX,
    INTENTIONS,
    Effect,
    IntentionEffectStore,
)
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.providers import _edit_target, plain_edit_title
from levain.intention_legacy import OLD_EFFECT_STORE, OLD_KIND_TO_NEW, OLD_ROW_PREFIX
from tests.autonomic_confirm_keys import anchor_scratch

FIXTURE = Path(__file__).with_name("fixtures") / "rename_h2"
OPS: dict[str, dict[str, str]] = json.loads((FIXTURE / "old_journal.json").read_text(encoding="utf-8"))
BROWSER = {"class": "token", "name": "browser"}


@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("h2-replay-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def old(home: Path) -> tuple[Path, Path]:
    """The old release's journal and store, laid down as it left them: ``(data root, intention store)``."""
    data = home / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "cockpit.db")
    conn.executescript((FIXTURE / "old_journal.sql").read_text(encoding="utf-8"))
    conn.close()
    store = home / "intentions.json"
    shutil.copyfile(FIXTURE / "old_store.json", store)
    return data, store


def _sql(data: Path, sql: str, *args: Any) -> list[tuple]:
    conn = sqlite3.connect(data / "cockpit.db")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _states(data: Path) -> dict[str, str]:
    by_id = {v["pending_id"]: k for k, v in OPS.items()}
    return {by_id[pid]: state for pid, state in _sql(data, "SELECT pending_id, state FROM pendings")}


def _broker(data: Path, store: Path, **kw: Any) -> Broker:
    b = Broker(data, stores={INTENTIONS: IntentionEffectStore(store)}, store_paths=[store], install_class="reported", **kw)
    b.start()
    return b


def _settled(data: Path, timeout: float = 15.0) -> dict[str, str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = _states(data)
        if "DECIDED" not in got.values():
            return got
        time.sleep(0.05)
    return _states(data)


def _row(store: Path, key: str) -> dict[str, Any] | None:
    return IntentionStore(store, id_prefix="intention").get_by_origin_key(key)


def _origin_count(store: Path, key: str) -> int:
    raw = json.loads(store.read_text())
    return sum(1 for r in (*raw.get("spores", []), *raw.get("resolved", [])) if r.get("origin_key") == key)


def _create_key(data: Path) -> str:
    """The origin key the old create (op d) planned: read off its journalled effect, never typed here."""
    return _sql(data, "SELECT resource_key FROM effects WHERE operation_id = ?", OPS["d"]["pending_id"])[0][0]


def _fixture_clock() -> datetime:
    """A minute after the WAITING op's issue: inside its signed lifetime."""
    return parse_stamp(json.loads(_sql_record("g"))["issued_at"]) + timedelta(minutes=1)


def _sql_record(name: str) -> str:
    conn = sqlite3.connect(":memory:")
    conn.executescript((FIXTURE / "old_journal.sql").read_text(encoding="utf-8"))
    rec = conn.execute("SELECT record FROM pendings WHERE pending_id = ?", (OPS[name]["pending_id"],)).fetchone()[0]
    conn.close()
    return rec.decode("utf-8")


# --- the fixture is what it claims to be ------------------------------------------------------------

def test_the_fixture_is_the_old_release_s_spelling(old: tuple[Path, Path]) -> None:
    """A control: the journal names the old store and key prefix and the old verbs, in the states the tests rely on, so
    a fixture rebuilt from the new code (which would pass everything below vacuously) fails here."""
    data, _ = old
    assert _states(data) == {"a": "COMMITTED", "b": "COMMITTED_INCOMPLETE", "c": "DECIDED", "d": "DECIDED",
                             "e": "DECIDED", "f": "DECIDED", "g": "WAITING"}
    assert {s for (s,) in _sql(data, "SELECT DISTINCT store FROM effects")} == {OLD_EFFECT_STORE}
    assert all(k.startswith(OLD_ROW_PREFIX) for (k,) in _sql(data, "SELECT resource_key FROM effects"))
    assert {v["verb"] for v in OPS.values()} <= set(OLD_KIND_TO_NEW)
    assert INTENTIONS != OLD_EFFECT_STORE and INTENTION_KEY_PREFIX != OLD_ROW_PREFIX


# --- H2: an op in flight across the update completes ------------------------------------------------

def test_ops_decided_under_the_old_release_complete_under_the_new_broker(old: tuple[Path, Path]) -> None:
    data, store = old
    b = _broker(data, store)
    try:
        assert b.state is Lifecycle.ACTIVE
        got = _settled(data)
    finally:
        b.stop()
    assert {k: got[k] for k in "cdef"} == {"c": "COMMITTED", "d": "COMMITTED", "e": "COMMITTED", "f": "COMMITTED"}, got
    for name in "cdef":
        effects = _sql(data, "SELECT status, reason FROM effects WHERE operation_id = ?", OPS[name]["pending_id"])
        assert effects == [("APPLIED", None)], (name, effects)
    assert _row(store, "k.c")["text"] == "gamma edited"                           # an update landed
    resolved_e = _row(store, "k.e")
    assert resolved_e["status"] == "resolved" and resolved_e["resolution"]["kind"] == "done"    # a resolve landed
    resolved_f = _row(store, "k.f")
    assert resolved_f["resolution"]["kind"] == "project" and resolved_f["resolution"]["ref"] == "commit abc"


def test_a_create_whose_store_write_was_durable_is_not_made_twice(old: tuple[Path, Path]) -> None:
    """Op d died after anneal wrote the row and before the journal said so: recovery finds the row, marks the effect
    APPLIED with the row's id, and the store holds one row for the key, before and after a second recovery."""
    data, store = old
    key = _create_key(data).removeprefix(OLD_ROW_PREFIX)
    assert _origin_count(store, key) == 1, "control: the fixture's store already holds the created row"
    for _ in range(2):
        b = _broker(data, store)
        try:
            b.recover()
            assert _settled(data)["d"] == "COMMITTED"
        finally:
            b.stop()
    assert _origin_count(store, key) == 1
    (native,) = _sql(data, "SELECT native_id FROM effects WHERE operation_id = ?", OPS["d"]["pending_id"])
    assert native[0] == _row(store, key)["id"]


def test_terminal_ops_are_left_exactly_as_they_were(old: tuple[Path, Path]) -> None:
    data, store = old
    before = {n: (_sql(data, "SELECT * FROM effects WHERE operation_id = ?", OPS[n]["pending_id"]),
                  _sql(data, "SELECT record, record_sha256, state, reason, terminal_at FROM pendings "
                             "WHERE pending_id = ?", OPS[n]["pending_id"])) for n in "ab"}
    b = _broker(data, store)
    try:
        _settled(data)
    finally:
        b.stop()
    for n in "ab":
        assert (_sql(data, "SELECT * FROM effects WHERE operation_id = ?", OPS[n]["pending_id"]),
                _sql(data, "SELECT record, record_sha256, state, reason, terminal_at FROM pendings "
                           "WHERE pending_id = ?", OPS[n]["pending_id"])) == before[n], n
    assert _states(data)["b"] == "COMMITTED_INCOMPLETE"
    assert _row(store, "k.b")["text"] == "beta moved under it" and _row(store, "k.b")["disposition"] == "seed"


def test_an_effect_read_from_the_old_record_names_the_new_store_and_key(old: tuple[Path, Path]) -> None:
    from levain.cockpit.effects import read_recorded_key, read_recorded_store

    data, _ = old
    rec = json.loads(_sql_record("c"))
    old_effect = rec["effects"][0]
    assert old_effect["store"] == OLD_EFFECT_STORE and old_effect["resource_key"].startswith(OLD_ROW_PREFIX)
    read = Effect.from_record(old_effect)
    assert read.store == INTENTIONS and read.resource_key == INTENTION_KEY_PREFIX + "k.c"
    # a reader of history accepts nothing new in the old spelling: the store and key a NEW effect carries are untouched
    assert (read_recorded_store(INTENTIONS), read_recorded_key("intention:k.c")) == (INTENTIONS, "intention:k.c")
    # a key of another store is not an intention key and is left alone
    assert read_recorded_key("pend:cp1") == "pend:cp1" and read_recorded_store("cockpit") == "cockpit"


# --- H2: new writes use the new spelling ------------------------------------------------------------

def test_new_labels_and_a_new_write_use_the_new_prefix(old: tuple[Path, Path]) -> None:
    data, store = old
    b = _broker(data, store)
    try:
        _settled(data)
        row = _row(store, "k.a")
        target = {"kind": "row", "panel_id": "tray", "row_id": f"intention:{row['id']}",
                  "row_version": IntentionEffectStore(store).snapshot("intention:k.a").version}
        out = b.issue(verb="intention_update", params={"text": "alpha again"}, target=target, tier="T2",
                      credential=BROWSER, intent="reported", idempotency_key="new-1")
        assert out["state"] == "COMMITTED", out
    finally:
        b.stop()
    new_pid = out["pending_id"]
    rec = json.loads(_sql(data, "SELECT record FROM pendings WHERE pending_id = ?", new_pid)[0][0])
    assert {(e["store"], e["resource_key"]) for e in rec["effects"]} == {(INTENTIONS, "intention:k.a")}
    assert _sql(data, "SELECT store, resource_key FROM effects WHERE operation_id = ?", new_pid) == [
        (INTENTIONS, "intention:k.a")]
    # every label the new code wrote, for an old op it completed or a new one, is keyed by the new prefix; the one the old
    # release wrote (op a) keeps its key: a label row is immutable
    new_labels = _sql(data, "SELECT resource_key FROM labels WHERE operation_id != ?", OPS["a"]["pending_id"])
    assert new_labels and all(k.startswith(INTENTION_KEY_PREFIX) for (k,) in new_labels), new_labels
    # op c was signed under the old key and completed here: its label is the new code's write
    assert _sql(data, "SELECT resource_key FROM labels WHERE operation_id = ?", OPS["c"]["pending_id"]) == [
        (INTENTION_KEY_PREFIX + "k.c",)]
    assert _sql(data, "SELECT resource_key FROM labels WHERE operation_id = ?", OPS["a"]["pending_id"]) == [
        (OLD_ROW_PREFIX + "k.a",)]


# --- H2: terminal rows render in the new words ------------------------------------------------------

def test_the_edits_panel_reads_old_terminal_rows_in_the_new_words(old: tuple[Path, Path]) -> None:
    data, store = old
    b = _broker(data, store)
    try:
        _settled(data)
        page = b.edits(50)
    finally:
        b.stop()
    assert page is not None and page.skipped == 0
    by_op = {r.operation_id: r for r in page.rows}
    a = by_op[OPS["a"]["pending_id"]]
    assert (a.verb, a.store, a.resource_key) == ("intention_update", INTENTIONS, "intention:k.a")
    assert (a.target or {}).get("row_id", "").startswith("intention:")
    assert plain_edit_title(a.verb or "", _edit_target(a.resource_key), a.credential) == "edited item k.a, from the browser"
    # the ops recovery completed render the same way: a promote and a resolve in their new verb names
    assert by_op[OPS["e"]["pending_id"]].verb == "intention_close"
    assert by_op[OPS["f"]["pending_id"]].verb == "intention_promote"
    for r in page.rows:
        assert r.store == INTENTIONS and r.resource_key.startswith(INTENTION_KEY_PREFIX), r
        assert r.verb not in OLD_KIND_TO_NEW, r


def test_the_decisions_panel_reads_the_old_incomplete_op_in_the_new_words(old: tuple[Path, Path]) -> None:
    data, store = old
    b = _broker(data, store)
    try:
        _settled(data)
        now = parse_stamp(_sql(data, "SELECT terminal_at FROM pendings WHERE pending_id = ?",
                               OPS["b"]["pending_id"])[0][0]) + timedelta(minutes=1)
        rows = b.decision_rows(now)
    finally:
        b.stop()
    assert rows is not None and rows.skipped == 0
    out = {o.operation_id: o for o in rows.outcomes}[OPS["b"]["pending_id"]]
    assert out.outcome == "COMMITTED-INCOMPLETE"
    assert out.verb == "intention_set_disposition"
    assert out.unapplied == ("intention:k.b",)
    # the recorded reason is a sentence the old release wrote: history, kept as recorded
    assert out.reason == f"{OLD_ROW_PREFIX}k.b: changed"


def test_an_old_op_s_receipt_names_the_new_verb(old: tuple[Path, Path]) -> None:
    data, store = old
    b = _broker(data, store)
    try:
        _settled(data)
        got = b.outcome(OPS["a"]["pending_id"])
    finally:
        b.stop()
    assert got["state"] == "COMMITTED" and got["verb"] == "intention_update"


@pytest.mark.parametrize("old_kind,new_kind", sorted(OLD_KIND_TO_NEW.items()))
def test_a_log_row_recorded_under_an_old_kind_renders_with_the_new_kind_s_label(old_kind: str, new_kind: str) -> None:
    """``Cockpit._log_sentences``: a row an older release recorded names its verb by the old id; the manifest only knows
    the new one, and an unknown verb would render raw ("spore_touch · spore-12")."""
    from levain.cockpit import Cockpit, ProviderSpec, Read

    when = "2026-11-11T20:00:00+00:00"
    value = {"rows": [{"verb": old_kind, "target": "spore-12", "actor": "phill", "at": when},
                      {"verb": new_kind, "target": "intention-13", "actor": "phill", "at": when},
                      {"verb": "mystery", "target": "x", "actor": "phill", "at": when}]}
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="log", title="P", priority="feed", read=lambda c: Read(value=value)))

    class Verbs:
        def verbs_map(self, cred: str, **k: Any) -> dict[str, Any]:
            return {new_kind: {"label": f"label of {new_kind}"}}

        def revision(self, auth: Any) -> str:
            return "r1"

        def panel_actions(self, *a: Any, **k: Any) -> list[Any]:
            return []

    ck._verb_view = Verbs()    # type: ignore[assignment]
    rows = ck.panel("p")["value"]["rows"]
    assert [r["sentence"] for r in rows] == [f"label of {new_kind} · spore-12", f"label of {new_kind} · intention-13",
                                             "mystery · x"]


# --- an old WAITING pending cannot be rebuilt: it ends stale, loudly, never a 501 ------------------------

def test_an_old_waiting_pending_ends_stale_when_its_page_retries_it(old: tuple[Path, Path]) -> None:
    """Signed under the old verb name, its effects cannot be rebuilt by the new code (the decision recomputes and compares
    them), so a retry of that very request after the update closes it STALE: the page reissues. It is never decided and
    never raises a 'no adapter' 501 that leaves it WAITING until it expires."""
    data, store = old
    rec = json.loads(_sql_record("g"))
    b = _broker(data, store, clock=_fixture_clock)
    try:
        out = b.issue(verb=rec["verb"], params=rec["params"], target=rec["target"], tier=rec["tier"],
                      credential=BROWSER, intent="reported", idempotency_key="idem-g")
    finally:
        b.stop()
    assert out["pending_id"] == OPS["g"]["pending_id"]
    assert out["state"] == "STALE", out
    assert _row(store, "k.g")["text"] == "eta"                       # nothing was written for it
