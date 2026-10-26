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
from tests.test_phone_server import _enrol
from tests.test_review_clear_signed import _mine, _phone_sign

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


def _serve(world: dict):
    from tests.test_phone_dispatch import _serve as serve

    return serve(world["qdir"], world["qdir"])


# --- F1: the phone's signed door takes the same raised-review refusal as the cockpit's kernel ------------------------

def test_a_raised_signed_clear_is_refused_on_the_phone_after_it_was_signed(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world) as (base, httpd):
        st, got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
        assert st == 200, got
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        _raise(world, "review_clear_signed")
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
    assert (st, out.get("error")) == (403, "review_raised"), out
    assert "T2" in out["message"] and f"levain coord review clear {mine.item_id}" in out["message"], out
    assert review.get_item(store, mine.item_id).state == "open"


def test_a_raised_signed_clear_files_nothing_on_the_phone(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    _raise(world, "review_clear_signed", "T3")
    with _serve(world) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
    assert (st, out.get("error")) == (403, "review_raised") and "T3" in out["message"], out
    assert _filed(world["qdir"]) == []


def test_a_raise_of_another_verb_leaves_the_phone_signed_clear_alone(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    _raise(world, "review_clear")
    with _serve(world) as (base, httpd):
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
    assert (st, out.get("mode")) == (200, "signed"), out


# --- F8: the raised signed verb names the laptop clear for what it records ------------------------------------------

def test_the_raised_signed_refusal_says_the_laptop_clear_is_unsigned(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    _raise(world, "review_clear_signed")
    with _serve(world) as (base, _):
        out = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, device_token(world["qdir"]))[1]
    assert f"levain coord review clear {mine.item_id} (recorded as a manual clear, unsigned)" in out["message"], out


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
    assert indexes() == ["review_events_one_signature"]


# --- F3: one open request per item version on the phone; none filed with no signer -----------------------------------

def test_two_sign_requests_for_one_item_file_one_request(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world) as (base, _):
        one = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
        two = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
        assert one[0] == two[0] == 200, (one, two)
        assert one[1]["request"]["id"] == two[1]["request"]["id"]
        assert len(_filed(world["qdir"])) == 1
        # the item moved: its old request no longer says what a clear would sign, so a new one is filed
        review.clear_item(store, mine.item_id)
        review.reopen_item(store, mine.item_id)
        three = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
    assert three[0] == 200 and three[1]["request"]["id"] != one[1]["request"]["id"], three
    assert len(_filed(world["qdir"])) == 2


def test_a_denied_request_is_not_reused(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world) as (base, _):
        one = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]["request"]
        assert _post(base, f"/sign/{one['id']}/deny", {"request_sha256": one["sha256"]}, dev)[0] == 200
        two = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]["request"]
    assert two["id"] != one["id"]


def test_no_enrolled_signer_files_nothing_and_says_so(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {"op": "revoke_key", "fingerprint": world["phone"].fp})
    finally:
        conn.close()
    with _serve(world) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, device_token(world["qdir"]))
    assert (st, out.get("error")) == (422, "no_passkey") and "nothing was filed" in out["message"], out
    assert _filed(world["qdir"]) == []


# --- F7: the phone's store faults are 503, as the cockpit's are ------------------------------------------------------

@pytest.mark.parametrize("where", ["file_clear_request", "clear_signed"])
@pytest.mark.parametrize("fault", [sqlite3.OperationalError("database is locked"), OSError(5, "Input/output error")])
def test_a_store_fault_on_the_phone_signed_path_is_503(world, monkeypatch, where, fault) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)

    def broken(*_a, **_k):
        raise fault

    monkeypatch.setattr(review, where, broken)
    action, body = ("sign-request", {}) if where == "file_clear_request" else ("clear", {"sign_request": "lv-" + "d" * 32})
    with _serve(world) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/{action}", body, device_token(world["qdir"]))
    assert (st, out.get("error")) == (503, "source_unavailable"), out
    assert review.get_item(store, mine.item_id).state == "open"


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

from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_review_clear_signed import _authenticator  # noqa: E402

_PICKS = "document.querySelectorAll('#review-list .pick')"
_SIGNED_BTN = "[...document.querySelectorAll('#review-list button')].find(b => b.textContent === 'Clear with passkey')"


def _wait(ms: int, then: str) -> dict:
    return {"eval": f"new Promise(r => setTimeout(() => r({then}), {ms}))"}


@needs_browser
def test_the_phone_picker_stays_until_a_pick_or_a_cancel(world, tmp_path: Path) -> None:
    from tests.test_coordinator_phone import device_token
    from tests.test_dispatch_phone import SoftPasskey

    other = SoftPasskey()
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {
            "op": "enrol", "fingerprint": other.fp, "public_key": other.line, "class": "display-phone",
            "custody": "passkey"})
    finally:
        conn.close()
    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    pick = (f"[...{_PICKS}].find(b => b.textContent === {json.dumps(world['phone'].fp[:18] + '…')})")
    with _serve(world) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, _authenticator(world["phone"].priv),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-review').click() || 1"},
            {"until": f"{_SIGNED_BTN} && 1"},
            {"eval": f"{_SIGNED_BTN}.click() || 1"},
            {"until": f"{_PICKS}.length === 3 && 1"},
            # past the 300 ms refresh after the filing and past the review tab's 10 s tick
            _wait(11500, f"[...{_PICKS}].map(b => b.textContent)"),
            {"eval": f"[...{_PICKS}].find(b => b.textContent === 'Cancel').click() || 1"},
            {"until": f"{_PICKS}.length === 0 && 1"},
            {"until": f"{_SIGNED_BTN} && !{_SIGNED_BTN}.disabled && 1"},
            {"eval": "document.getElementById('review-list').innerText.includes('nothing was signed')"},
            {"eval": f"{_SIGNED_BTN}.click() || 1"},
            {"until": f"{pick} && 1"},
            {"eval": f"{pick}.click() || 1"},
            {"until": "document.querySelectorAll('#review-cleared .review-item').length === 1 && 1", "ms": 30000},
        ])
    kept = next(v for v in out["values"] if isinstance(v, list))
    assert len(kept) == 3 and kept[-1] == "Cancel", out["values"]
    assert [v for v in out["values"] if isinstance(v, bool)] == [True], out["values"]     # the cancel said so
    posts = [p.split(" ")[0] for p in out["posts"] if "/coord/review/" in p or "/sign/" in p]
    assert [p.rsplit("/", 1)[-1] for p in posts] == ["sign-request", "sign-request", "challenge", "answer", "clear"]
    assert len(_filed(world["qdir"])) == 1
    assert review.get_item(store, mine.item_id).state == "cleared"


# --- G1: a second card's clear never wipes an open picker ------------------------------------------------------------

@needs_browser
def test_a_second_cards_clear_leaves_an_open_picker_alone(world, tmp_path: Path) -> None:
    from tests.test_coordinator_phone import device_token
    from tests.test_dispatch_phone import SoftPasskey

    other = SoftPasskey()
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {
            "op": "enrol", "fingerprint": other.fp, "public_key": other.line, "class": "display-phone",
            "custody": "passkey"})
    finally:
        conn.close()
    store = CoordStore(world["qdir"])
    _mine(store, "a")
    _mine(store, "b")
    dev = device_token(world["qdir"])
    all_btns = "[...document.querySelectorAll('#review-list button')].filter(b => b.textContent === 'Clear with passkey')"
    with _serve(world) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, _authenticator(world["phone"].priv),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-review').click() || 1"},
            {"until": f"{all_btns}.length === 2 && 1"},
            {"eval": f"{all_btns}[0].click() || 1"},
            {"until": f"{_PICKS}.length === 3 && 1"},
            {"eval": f"{all_btns}[1].click() || 1"},
            _wait(1500, f"{_PICKS}.length"),
        ])
    assert out["values"][-1] == 3, out["values"]
    posts = [p.split(" ")[0].rsplit("/", 1)[-1] for p in out["posts"] if "/coord/review/" in p or "/sign/" in p]
    assert posts == ["sign-request"], posts      # the second tap filed nothing


# --- G2: a missing or unclearable item is its own 404 / 409 before the no-signer 422 ---------------------------------

def _revoke_phone(world: dict) -> None:
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {"op": "revoke_key", "fingerprint": world["phone"].fp})
    finally:
        conn.close()


def test_with_no_signer_a_missing_item_is_404_and_a_plain_one_409(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    plain = review.file_item(store, "doubt", {"text": "plain"}, dedupe_key="p", authority_relevant=False)[0]
    _revoke_phone(world)
    dev = device_token(world["qdir"])
    with _serve(world) as (base, _):
        missing = _post(base, "/coord/review/rv-" + "0" * 32 + "/sign-request", {}, dev)
        plain_r = _post(base, f"/coord/review/{plain.item_id}/sign-request", {}, dev)
        open_r = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)
    assert missing[0] == 404, missing
    assert plain_r[0] == 409 and "not yours to clear" in plain_r[1]["message"], plain_r
    assert (open_r[0], open_r[1].get("error")) == (422, "no_passkey"), open_r      # a real open item still says so
    assert _filed(world["qdir"]) == []


def test_with_no_signer_a_cleared_item_is_409(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    review.clear_item(store, mine.item_id)
    _revoke_phone(world)
    with _serve(world) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, device_token(world["qdir"]))
    assert st == 409 and "already cleared" in out["message"], (st, out)


# --- G3 / N2: only the signature index refusing a replay is a stale 409; any other integrity fault is a 503 ----------

def _occupy(store: CoordStore, rid: str) -> None:
    """Another item already cleared with ``rid``: the unique index on the signature now holds it."""
    other = _mine(store, "other")
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) VALUES "
                     "(?, 1, 'cleared', 'signed', 'operator', ?, ?)", (other.item_id, now, rid))


def test_a_signature_already_used_is_409_stale_on_the_phone(world) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    with _serve(world) as (base, httpd):
        got = _post(base, f"/coord/review/{mine.item_id}/sign-request", {}, dev)[1]
        _phone_sign(base, httpd.server_address[1], world, got["request"], dev)
        _occupy(store, got["request"]["id"])
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": got["request"]["id"]}, dev)
    assert (st, out.get("error")) == (409, "stale"), out
    assert review.get_item(store, mine.item_id).state == "open"


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


def test_another_integrity_fault_on_the_signed_clear_is_503(world, monkeypatch) -> None:
    from tests.test_coordinator_phone import _post, device_token

    store = CoordStore(world["qdir"])
    mine = _mine(store)

    def fault(*_a, **_k):
        raise sqlite3.IntegrityError("CHECK constraint failed: review_events_mode")

    monkeypatch.setattr(review, "clear_signed", fault)
    with _serve(world) as (base, _):
        st, out = _post(base, f"/coord/review/{mine.item_id}/clear", {"sign_request": "lv-" + "d" * 32},
                        device_token(world["qdir"]))
    assert (st, out.get("error")) == (503, "source_unavailable"), out


# --- N1: a queue read in flight when the picker opens never wipes it ------------------------------------------------

_HOLD = """(() => {
  const real = window.fetch; window.__held = []; window.__hold = true;
  window.fetch = (u, o) => (window.__hold && String(u).startsWith('/coord.json'))
    ? new Promise((res) => window.__held.push(() => res(real(u, o)))) : real(u, o);
  window.__release = () => { window.__hold = false; const h = window.__held; window.__held = []; h.forEach((f) => f()); };
  return 1; })()"""


@needs_browser
def test_a_queue_read_in_flight_does_not_wipe_a_picker_that_opened_meanwhile(world, tmp_path: Path) -> None:
    from tests.test_coordinator_phone import device_token
    from tests.test_dispatch_phone import SoftPasskey

    other = SoftPasskey()
    root = world["root"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        _enrol(conn, root, root.parent / "laptop", world["laptop_fp"], {
            "op": "enrol", "fingerprint": other.fp, "public_key": other.line, "class": "display-phone",
            "custody": "passkey"})
    finally:
        conn.close()
    store = CoordStore(world["qdir"])
    mine = _mine(store)
    dev = device_token(world["qdir"])
    pick = (f"[...{_PICKS}].find(b => b.textContent === {json.dumps(world['phone'].fp[:18] + '…')})")
    with _serve(world) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, _authenticator(world["phone"].priv),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-review').click() || 1"},
            {"until": f"{_SIGNED_BTN} && 1"},
            {"eval": _HOLD},
            {"eval": "window.levainViewChanged('review') || 1"},                  # a read goes out and is held
            _wait(400, "'held=' + window.__held.length"),
            {"eval": f"{_SIGNED_BTN}.click() || 1"},
            {"until": f"{_PICKS}.length === 3 && 1"},
            {"eval": "window.__release() || 1"},                                  # the old read lands over the picker
            _wait(1500, f"'picks=' + {_PICKS}.length"),
            {"eval": f"[...{_PICKS}].find(b => b.textContent === 'Cancel').click() || 1"},
            {"until": f"{_PICKS}.length === 0 && 1"},
            {"until": f"{_SIGNED_BTN} && !{_SIGNED_BTN}.disabled && 1"},
            {"eval": f"{_SIGNED_BTN}.click() || 1"},                              # a later clear still works
            {"until": f"{pick} && 1"},
            {"eval": f"{pick}.click() || 1"},
            {"until": "document.querySelectorAll('#review-cleared .review-item').length === 1 && 1", "ms": 30000},
        ])
    tagged = [v for v in out["values"] if isinstance(v, str)]
    assert tagged == ["held=1", "picks=3"], out["values"]   # a read was in flight; its landing left the picker as it was
    assert review.get_item(store, mine.item_id).state == "cleared"
