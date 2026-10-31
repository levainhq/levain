"""c4mut combined, L3 r1 (1025-40/c4mutc/RULINGS_L3.md F1-F3, F7-F9): the phone's signed clear tiered by the signed
standing policy as the cockpit's is; a row version and a signed message that move with every transition; one signature
clears once; the phone's sign request reused, never filed per tap; the phone's store faults answered as 503."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit.coordinator_panels import review_version
from levain.coordinator import review
from levain.coordinator.store import CoordStore
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)
from tests._phone_fixtures import _enrol
from tests.test_review_clear_signed import _mine

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

_LV = re.compile(r"lv-[0-9a-f]{32}\.json")


def _filed(qdir: Path) -> list[str]:
    d = qdir / "sign"
    return sorted(p.name for p in d.iterdir() if _LV.fullmatch(p.name)) if d.is_dir() else []


def _raise(world: dict, verb: str, tier: str = "T2") -> None:
    """A signed raise of ``verb`` in the world's authority log, by its laptop key (the fixture's tmp_path/laptop)."""
    root = world["root"]
    laptop = root.parent / "laptop"
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, laptop, world["laptop_fp"], {"op": "raise", "verb": verb, "tier": tier})
    finally:
        conn.close()




# --- F1: the phone's signed door takes the same raised-review refusal as the cockpit's kernel ------------------------







# --- F8: the raised signed verb names the laptop clear for what it records ------------------------------------------



# --- F2: the version and the signed message move with every transition, whatever the clock --------------------------

def test_two_transitions_inside_one_clock_tick_change_the_version_and_the_message(tmp_path: Path) -> None:
    tick = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    store = CoordStore(tmp_path, clock=lambda: tick)
    mine = _mine(store)
    review.clear_item(store, mine.item_id)
    once = review.reopen_item(store, mine.item_id)
    review.clear_item(store, mine.item_id)
    twice = review.reopen_item(store, mine.item_id)
    assert once.state == twice.state == "open" and once.last[3] == twice.last[3]
    assert review_version(once) != review_version(twice)
    assert review.clear_message(once) != review.clear_message(twice)
    # the grammar levain files its own clear requests in still takes it
    from levain.contracts.namespaces import LEVAIN_SIGN_REQUESTS, REVIEW_CLEAR_NAMESPACE

    for item in (mine, once, twice):
        assert re.fullmatch(LEVAIN_SIGN_REQUESTS[REVIEW_CLEAR_NAMESPACE], review.clear_message(item))


def test_one_signature_clears_at_most_once(tmp_path: Path) -> None:
    store = CoordStore(tmp_path)
    mine = _mine(store)
    rid = "lv-" + "c" * 32
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) VALUES "
                     "(?, 1, 'cleared', 'signed', 'operator', ?, ?)", (mine.item_id, now, rid))
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES "
                     "(?, 2, 'reopened', 'manual', 'operator', ?)", (mine.item_id, now))
    with pytest.raises(sqlite3.IntegrityError):
        with store._write() as (conn, now):
            conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) VALUES "
                         "(?, 3, 'cleared', 'signed', 'operator', ?, ?)", (mine.item_id, now, rid))
    assert review.get_item(store, mine.item_id).state == "open"


def test_a_store_made_before_the_index_is_read_and_gains_it_at_its_first_writer(tmp_path: Path, monkeypatch) -> None:
    from tests.test_review_clear_combined import _pre_r4_store

    d, items = _pre_r4_store(tmp_path, monkeypatch)

    def indexes() -> list[str]:
        with sqlite3.connect(d / "dispatch.db") as conn:
            return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND "
                                               "tbl_name = 'review_events' AND sql IS NOT NULL")]

    assert indexes() == []
    with review.read_transaction(CoordStore(d).open_readonly) as conn:
        assert review.queue_page(conn, open_limit=10, cleared_limit=10).open_total == 1
    review.reopen_item(CoordStore(d), items[0].item_id)
    assert sorted(indexes()) == ["review_events_gseq", "review_events_one_signature"]      # R4's, then S0's


# --- F3: one open request per item version on the phone; none filed with no signer -----------------------------------







# --- F7: the phone's store faults are 503, as the cockpit's are ------------------------------------------------------



# --- F9: the CLI's list takes an item whose stored body is not an object ---------------------------------------------

def test_the_cli_review_list_draws_an_item_whose_body_is_not_an_object(tmp_path: Path, capsys) -> None:
    from levain.cli import main

    store = CoordStore(tmp_path)
    review.file_item(store, "seat", {"text": "fine"}, dedupe_key="a")
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_items (item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at) "
                     "VALUES (?, NULL, 'seat', 'b', '[1, 2]', 0, 'service', ?)", ("rv-" + "1" * 32, now))
    assert main(["coord", "review", "list", "--json", "--queue-dir", str(tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)["items"]
    assert sorted(r["text"] for r in rows) == ["", "fine"]



# --- F4: the phone page keeps a multi-passkey picker until a pick or a cancel (headless Chrome, its own profile) ------


_PICKS = "document.querySelectorAll('#review-list .pick')"
_SIGNED_BTN = "[...document.querySelectorAll('#review-list button')].find(b => b.textContent === 'Clear with passkey')"


def _wait(ms: int, then: str) -> dict:
    return {"eval": f"new Promise(r => setTimeout(() => r({then}), {ms}))"}




# --- G1: a second card's clear never wipes an open picker ------------------------------------------------------------



# --- G2: a missing or unclearable item is its own 404 / 409 before the no-signer 422 ---------------------------------

def _revoke_phone(world: dict) -> None:
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {"op": "revoke_key", "fingerprint": world["phone"].fp})
    finally:
        conn.close()






# --- G3 / N2: only the signature index refusing a replay is a stale 409; any other integrity fault is a 503 ----------

def _occupy(store: CoordStore, rid: str) -> None:
    """Another item already cleared with ``rid``: the unique index on the signature now holds it."""
    other = _mine(store, "other")
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) VALUES "
                     "(?, 1, 'cleared', 'signed', 'operator', ?, ?)", (other.item_id, now, rid))




def test_clear_signed_names_a_used_signature_as_a_stale_refusal(tmp_path: Path) -> None:
    from tests.test_review_clear_signed import ORIGIN, _clear, _q, _sign, _signer  # noqa: F401
    from tests.test_cockpit_sign_origin import _Passkey

    qdir, store = _q(tmp_path)
    pk = _Passkey()
    mine = _mine(store)
    rid = review.file_clear_request(store, mine.item_id, queue_dir=qdir)
    _sign(qdir, pk, rid)
    _occupy(store, rid)
    with pytest.raises(review.SignedClearRefused) as e:
        _clear(store, qdir, mine, rid, pk)
    assert (e.value.status, e.value.error) == (409, "stale")




# --- N1: a queue read in flight when the picker opens never wipes it ------------------------------------------------

_HOLD = """(() => {
  const real = window.fetch; window.__held = []; window.__hold = true;
  window.fetch = (u, o) => (window.__hold && String(u).startsWith('/coord.json'))
    ? new Promise((res) => window.__held.push(() => res(real(u, o)))) : real(u, o);
  window.__release = () => { window.__hold = false; const h = window.__held; window.__held = []; h.forEach((f) => f()); };
  return 1; })()"""


