"""C4 mutation slice (c4mut): clearing and reopening a review item from the cockpit and the phone.

The ruling (head 1025+25, c4b RULINGS_OBS item 1): a plain item clears and reopens on a PAIRED DEVICE's credential,
mode manual, never on the shared surface token; a "yours to clear" (authority-relevant) item is never cleared on a
device token at all (its clear is signed)."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.coordinator import review
from levain.coordinator.store import CoordError, CoordStore


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_a_plain_clear_refuses_a_yours_to_clear_item_in_the_store(tmp_path: Path) -> None:
    store = CoordStore(_dir(tmp_path))
    mine, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="m", authority_relevant=True)
    with pytest.raises(CoordError, match="yours to clear"):
        review.clear_item(store, mine.item_id, mode="manual", plain_only=True)
    assert review.get_item(store, mine.item_id).state == "open"
    # the laptop's own `levain coord review clear` (no plain_only) is unchanged
    assert review.clear_item(store, mine.item_id, mode="manual").state == "cleared"


def test_a_plain_clear_and_reopen_of_a_plain_item_write_manual_by_the_operator(tmp_path: Path) -> None:
    store = CoordStore(_dir(tmp_path))
    item, _ = review.file_item(store, "seat", {"text": "seat died"}, dedupe_key="s")
    done = review.clear_item(store, item.item_id, mode="manual", plain_only=True)
    assert done.state == "cleared" and done.last[:3] == ("cleared", "manual", "operator")
    again = review.reopen_item(store, item.item_id, mode="manual", plain_only=True)
    assert again.state == "open" and again.last[:3] == ("reopened", "manual", "operator")
    with pytest.raises(CoordError, match="already open"):
        review.reopen_item(store, item.item_id, mode="manual", plain_only=True)


def test_a_plain_reopen_of_a_yours_to_clear_item_is_allowed(tmp_path: Path) -> None:
    """Reopening only puts an item back in front of you: it clears nothing, so a device token may do it."""
    store = CoordStore(_dir(tmp_path))
    mine, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="m", authority_relevant=True)
    review.clear_item(store, mine.item_id, mode="manual")
    assert review.reopen_item(store, mine.item_id, mode="manual", plain_only=True).state == "open"


# --- the cockpit: kernel row verbs review_clear / review_reopen, on a paired device's credential only ---------------

from levain.cockpit.coordinator_panels import REVIEW_ID  # noqa: E402
from tests.test_cockpit_k2a import _install  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve  # noqa: E402
FC = "review-paths-surface-token-0123456789"      # a named surface (the shared token that never clears), honoured proxied (W1b L-1)


@pytest.fixture
def served(tmp_path: Path, monkeypatch):
    """A real make_server over a temp install whose consent directory (LEVAIN_HOME/consent) holds a coordinator store
    with one plain item and one "yours to clear" item: (port, store, plain item, authority item)."""
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    src = _install(tmp_path)[1]
    store = CoordStore(home / "consent")
    plain, _ = review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    mine, _ = review.file_item(store, "doubt", {"text": "the receipt does not match"}, dedupe_key="b",
                               authority_relevant=True)
    with _serve(src, surface_tokens={"browser": T, "flowconnect": FC}) as (port, _httpd):
        yield port, store, plain, mine


def _rows(port: int, token: str) -> dict[str, dict]:
    st, panel, _ = _call(port, "GET", f"/cockpit/panel/{REVIEW_ID}.json?profile=full", token=token, headers=PROXY)
    assert st == 200, panel
    return {r["id"]: r for r in panel["rows"]}


def _verbs(row: dict) -> set[str]:
    return {a["verb"] for a in row.get("actions") or ()}


def _fire(port: int, token: str, verb: str, row: dict) -> tuple[int, dict]:
    st, out, _ = _call(port, "POST", "/cockpit/verb", {"verb": verb, "panel_id": REVIEW_ID, "row_id": row["id"],
                                                       "row_version": row["version"], "params": {}},
                       token=token, headers=PROXY)
    return st, out


def test_the_review_rows_name_their_state_and_offer_clear_only_on_a_plain_open_item(served) -> None:
    port, _store, plain, mine = served
    _dev, tok = _pair(port, proxied=True)
    rows = _rows(port, tok)
    p, m = rows[f"review:{plain.item_id}"], rows[f"review:{mine.item_id}"]
    assert p["facets"]["review_state"] == "open" and p["facets"]["review_authority"] is False
    assert m["facets"]["review_authority"] is True
    assert _verbs(p) == {"review_clear"}
    assert _verbs(m) == {"review_clear_signed"}   # "yours to clear": no device-token Clear, only the signed one


def test_the_shared_surface_token_never_clears_and_says_why(served) -> None:
    port, store, plain, _mine = served
    row = _rows(port, FC)[f"review:{plain.item_id}"]
    st, out = _fire(port, FC, "review_clear", row)
    assert (st, out.get("error")) == (403, "device_required"), out
    assert "paired device" in out["message"]
    assert review.get_item(store, plain.item_id).state == "open"


def test_a_paired_device_clears_then_reopens_a_plain_item_as_you_in_manual_mode(served) -> None:
    port, store, plain, _mine = served
    _dev, tok = _pair(port, proxied=True)
    st, out = _fire(port, tok, "review_clear", _rows(port, tok)[f"review:{plain.item_id}"])
    assert (st, out.get("review_state")) == (200, "cleared"), out
    assert review.get_item(store, plain.item_id).last[:3] == ("cleared", "manual", "operator")
    row = _rows(port, tok)[f"review:{plain.item_id}"]          # the cleared item stays listed, to be reopened
    assert row["facets"]["review_state"] == "cleared" and _verbs(row) == {"review_reopen"}
    st, out = _fire(port, tok, "review_reopen", row)
    assert (st, out.get("review_state")) == (200, "open"), out
    assert review.get_item(store, plain.item_id).state == "open"


def test_a_row_cleared_elsewhere_is_a_409_and_nothing_is_written_twice(served) -> None:
    port, store, plain, _mine = served
    _dev, tok = _pair(port, proxied=True)
    row = _rows(port, tok)[f"review:{plain.item_id}"]
    review.clear_item(store, plain.item_id)                    # the laptop CLI cleared it after the page drew it
    st, out = _fire(port, tok, "review_clear", row)
    assert st == 409, out
    with review.read_transaction(store.open_readonly) as conn:
        n = conn.execute("SELECT COUNT(*) FROM review_events WHERE item_id = ?", (plain.item_id,)).fetchone()[0]
    assert n == 1


def test_a_forced_device_clear_of_a_yours_to_clear_item_is_refused_and_writes_nothing(served) -> None:
    port, store, _plain, mine = served
    _dev, tok = _pair(port, proxied=True)
    row = _rows(port, tok)[f"review:{mine.item_id}"]
    st, out = _fire(port, tok, "review_clear", row)
    assert st == 409, out
    assert review.get_item(store, mine.item_id).state == "open"


# --- the phone port: POST /coord/review/<item_id>/clear|reopen, a paired device only -------------------------------
# (over a real `levain init` install: the phone's review writes go through the phone app's kernel write, which needs
# the install its consent hook names; test_review_clear_fix1 holds the fixture)

from tests.test_review_clear_fix1 import _drawn as _version, install  # noqa: E402,F401  (fixtures)


def _drawn(base, token, item_id):
    """The body a Clear or Reopen sends: the version the phone's queue drew for ``item_id``."""
    return {"version": _version(base, token, item_id)}










# --- the two pages, driven in headless Chrome --------------------------------------------------------------------

from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402




REVIEW_BTN = ("[...document.querySelectorAll('[data-ck-verb]')].find(b => b.textContent === 'clear' "
              "&& (b.closest('.row') || {}).textContent.includes('seat lane-a died'))")


@needs_browser
def test_the_cockpit_page_clears_from_a_paired_browser_and_says_why_on_the_shared_token(tmp_path: Path,
                                                                                        monkeypatch) -> None:
    import os

    from tests.test_cockpit_flip_parity import _serve as _page_serve

    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    src = _install(tmp_path)[1]
    store = CoordStore(home / "consent")
    plain, _ = review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    review.file_item(store, "doubt", {"text": "the receipt does not match"}, dedupe_key="b", authority_relevant=True)
    with _page_serve(src, surface_tokens={"browser": T}, install_class="reported") as (url, _httpd):
        port = int(url.rsplit(":", 1)[1])
        shared = _drive(tmp_path, f"{url}/#write_token={T}", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1, "mobile": False}},
            {"until": f"{REVIEW_BTN} && 1"},
            {"shot": str(shots / "c4mut_cockpit_before.png"), "full": True},
            {"eval": f"{REVIEW_BTN}.click() || 1"},
            {"posts": 1},
            {"until": "(document.body.innerText.match(/[^\\n]*paired device[^\\n]*/) || [null])[0]", "ms": 4000,
             "may_timeout": True},
            {"eval": "[...document.querySelectorAll('[data-ck-verb]')].filter(b => b.textContent === 'clear').map(b => b.closest('.row').textContent)"},
        ])
        assert review.get_item(store, plain.item_id).state == "open"
        _dev, tok = _pair(port, proxied=True)
        paired = _drive(tmp_path, f"{url}/#write_token={tok}", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1, "mobile": False}},
            {"until": f"{REVIEW_BTN} && 1"},
            {"eval": f"{REVIEW_BTN}.click() || 1"},
            {"until": "[...document.querySelectorAll('[data-ck-verb]')].some(b => b.textContent === 'reopen') && 1"},
            {"shot": str(shots / "c4mut_cockpit_after.png"), "full": True},
            {"eval": "[...document.querySelectorAll('.review-mine')].map(m => m.textContent)"},
        ])
    assert "paired device" in str(shared["values"][-2]), (shared["values"], shared["posts"])
    assert review.get_item(store, plain.item_id).last[:3] == ("cleared", "manual", "operator")
    assert any("levain coord review clear" in t for t in paired["values"][-1]), paired["values"]
