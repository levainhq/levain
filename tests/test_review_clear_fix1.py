"""c4mut L3 r1 fixes (head rulings F1-F7, 1025-32/c4mut/RULINGS.md): the review clear and reopen of both surfaces go
through the cockpit's verb kernel, version-checked and device-checked inside the store's write, on a store that is never
created by them."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from levain.coordinator import review
from levain.coordinator.store import CoordError, CoordStore, StoreAbsent


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def _events(store: CoordStore, item_id: str) -> int:
    with review.read_transaction(store.open_readonly) as conn:
        return conn.execute("SELECT COUNT(*) FROM review_events WHERE item_id = ?", (item_id,)).fetchone()[0]


# --- the store (F2 `still`, F4 no store is made, F5 CoordNotFound) -------------------------------------------------

def test_a_missing_item_is_its_own_error_type(tmp_path: Path) -> None:
    store = CoordStore(_dir(tmp_path))
    review.file_item(store, "seat", {"text": "x"}, dedupe_key="a")
    for act in (review.clear_item, review.reopen_item):
        with pytest.raises(review.CoordNotFound):
            act(store, "rv-" + "0" * 32, mode="manual")
    with pytest.raises(review.CoordNotFound):
        review.get_item(store, "rv-" + "0" * 32)
    assert issubclass(review.CoordNotFound, CoordError)


def test_a_transition_never_makes_a_store(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    with pytest.raises(StoreAbsent):
        review.clear_item(store, "rv-" + "0" * 32, mode="manual")
    assert not (d / "dispatch.db").exists()
    # the dispatch store's file alone (the job store shares it) holds no coordinator tables: still absent, none made
    from levain.dispatch.jobs import JobStore

    JobStore(d)._connect().close()
    assert (d / "dispatch.db").exists()
    with pytest.raises(StoreAbsent):
        review.reopen_item(CoordStore(d), "rv-" + "0" * 32, mode="manual")
    conn = sqlite3.connect(d / "dispatch.db")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "review_items" not in tables


def test_still_runs_inside_the_write_on_the_item_as_it_is_then_and_a_raise_writes_nothing(tmp_path: Path) -> None:
    store = CoordStore(_dir(tmp_path))
    item, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="a")
    seen = []

    class No(Exception):
        pass

    def refuse(i: review.Item) -> None:
        seen.append((i.item_id, i.state))
        raise No

    with pytest.raises(No):
        review.clear_item(store, item.item_id, mode="manual", still=refuse)
    assert seen == [(item.item_id, "open")] and _events(store, item.item_id) == 0
    done = review.clear_item(store, item.item_id, mode="manual", still=lambda i: None)
    assert done.state == "cleared"


def test_the_laptop_cli_clear_still_works_on_its_envelope_store(tmp_path: Path) -> None:
    """The CLI opens the store as the envelope store (a subclass with more tables): the non-creating writer takes it."""
    from levain.coordinator.envelopes import EnvelopeStore

    d = _dir(tmp_path)
    store = EnvelopeStore(d)
    item, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="a")
    assert review.clear_item(store, item.item_id, mode="manual").state == "cleared"


# --- the queue panel (F7: a body that is not an object, older clears said; F2's one version computation) -----------

def test_an_item_whose_body_is_not_an_object_draws_its_row(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from levain.cockpit.coordinator_panels import _review_rows

    store = CoordStore(_dir(tmp_path))
    review.file_item(store, "seat", {"text": "fine"}, dedupe_key="a")
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_items (item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at) "
                     "VALUES (?, NULL, 'seat', 'b', 'null', 0, 'service', ?)", ("rv-" + "1" * 32, now))
    rows, _note = _review_rows(store, datetime.now(timezone.utc))
    assert {r.id for r in rows} == {f"review:rv-{'1' * 32}", f"review:{review.list_items(store)[0].item_id}"}


def test_the_cleared_group_says_when_older_clears_exist(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from levain.cockpit.coordinator_panels import CLEARED_SHOWN, _review_rows

    store = CoordStore(_dir(tmp_path))
    ids = [review.file_item(store, "seat", {"text": f"s{n}"}, dedupe_key=f"k{n}")[0].item_id
           for n in range(CLEARED_SHOWN + 1)]
    for i in ids[:CLEARED_SHOWN]:
        review.clear_item(store, i)
    rows, note = _review_rows(store, datetime.now(timezone.utc))
    assert "older ones" not in note                     # exactly CLEARED_SHOWN clears: all of them are listed
    review.clear_item(store, ids[-1])
    rows, note = _review_rows(store, datetime.now(timezone.utc))
    assert len(rows) == CLEARED_SHOWN and "levain coord review list --all" in note, note


def test_review_version_is_the_version_the_panel_draws(tmp_path: Path) -> None:
    from levain.cockpit.coordinator_panels import REVIEW_ID, coordinator_discoverer, review_version
    from levain.cockpit.engine import Cockpit

    d = _dir(tmp_path)
    store = CoordStore(d)
    item, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="a")
    review.clear_item(store, item.item_id)
    ck = Cockpit()
    ck.discover(coordinator_discoverer(lambda: d), retire=True)
    row = next(r for r in ck.panel(REVIEW_ID, profile="full")["rows"] if r["id"] == f"review:{item.item_id}")
    assert row["version"] == review_version(review.get_item(store, item.item_id))


# --- the cockpit: the version and the paired device are checked inside the write (F2, F3/F6) -----------------------

from levain.cockpit.coordinator_panels import REVIEW_ID  # noqa: E402
from tests.test_cockpit_k2a import _install  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY, _call, _pair, _serve  # noqa: E402


@pytest.fixture
def served(tmp_path: Path, monkeypatch):
    """test_review_clear_paths' ``served``, with the server: (port, httpd, store, plain item)."""
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    src = _install(tmp_path)[1]
    store = CoordStore(home / "consent")
    plain, _ = review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    with _serve(src) as (port, httpd):
        yield port, httpd, store, plain


def _row(port: int, token: str, item_id: str) -> dict:
    st, panel, _ = _call(port, "GET", f"/cockpit/panel/{REVIEW_ID}.json?profile=full", token=token, headers=PROXY)
    assert st == 200, panel
    return next(r for r in panel["rows"] if r["id"] == f"review:{item_id}")


def _fire(port: int, token: str, verb: str, row: dict) -> tuple[int, dict]:
    st, out, _ = _call(port, "POST", "/cockpit/verb", {"verb": verb, "panel_id": REVIEW_ID, "row_id": row["id"],
                                                       "row_version": row["version"], "params": {}},
                       token=token, headers=PROXY)
    return st, out


def _after_the_read(monkeypatch, act) -> None:
    """Run ``act`` once, right after the kernel's fresh read of the row and before the fire's write: the window the
    version check at the read cannot see."""
    import levain.cockpit.coordinator_panels as cp

    real, done = cp._review_one, []

    def one(store, row_id, now):
        got = real(store, row_id, now)
        if not done:
            done.append(1)
            act()
        return got
    monkeypatch.setattr(cp, "_review_one", one)


def test_a_clear_and_reopen_between_the_read_and_the_write_refuses_the_stale_clear(served, monkeypatch) -> None:
    port, _httpd, store, plain = served
    _dev, tok = _pair(port, proxied=True)
    row = _row(port, tok, plain.item_id)
    _after_the_read(monkeypatch, lambda: (review.clear_item(store, plain.item_id),
                                          review.reopen_item(store, plain.item_id)))
    st, out = _fire(port, tok, "review_clear", row)
    assert (st, out.get("error")) == (409, "stale"), out
    assert review.get_item(store, plain.item_id).state == "open" and _events(store, plain.item_id) == 2


def test_a_device_revoked_after_its_request_was_authenticated_clears_nothing(served, monkeypatch) -> None:
    from datetime import datetime, timezone

    port, httpd, store, plain = served
    dev, tok = _pair(port, proxied=True)
    row = _row(port, tok, plain.item_id)
    _after_the_read(monkeypatch, lambda: httpd.broker.active_db().revoke_devices(
        [dev], by="test", now=datetime.now(timezone.utc)))
    st, out = _fire(port, tok, "review_clear", row)
    assert (st, out.get("error")) == (403, "device_revoked"), out
    assert review.get_item(store, plain.item_id).state == "open"


def test_a_missing_item_at_the_write_is_404_on_the_cockpit(served, monkeypatch) -> None:
    """The store gone between the read and the write: 404 not_found (F5: by type), and no store is made (F4)."""
    import shutil

    port, _httpd, store, plain = served
    _dev, tok = _pair(port, proxied=True)
    row = _row(port, tok, plain.item_id)
    _after_the_read(monkeypatch, lambda: shutil.rmtree(store.directory))
    st, out = _fire(port, tok, "review_clear", row)
    assert (st, out.get("error")) == (404, "not_found"), out
    assert not store.directory.exists()


# --- the phone: a transport into the phone app's one kernel write (F1), the version it drew (F2), the device asked
# again (F3) -----------------------------------------------------------------------------------------------------

from tests.test_phone_app import _raise_on_the_laptop, install  # noqa: E402,F401  (fixture)




def _drawn(base: str, token: str, item_id: str) -> "str | None":
    """The version the phone's queue drew for ``item_id`` (None when the listing carries none)."""
    import json
    import urllib.request

    req = urllib.request.Request(base + "/coord.json", headers={"X-Levain-Phone-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        rv = json.loads(r.read())["coord_review"]
    return next(i.get("version") for i in rv["open"] + rv["cleared"] if i["item_id"] == item_id)


def _body(version: "str | None") -> dict:
    return {"version": version} if version is not None else {}










