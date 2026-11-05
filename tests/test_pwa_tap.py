"""1105-7 PWA defect A: a notification tap that arrives as a launcher-initiated navigation (an intent-launched Chrome
navigation was measured ``Sec-Fetch-Site: cross-site`` on an Android emulator) must land on the cockpit shell at the URL the
push payload names, not on the plain-text 403 the shared read preamble sends a cross-site read. The payload's own URLs
are built by ``center.push_url`` and the held-summary constants, so the test derives them from there and asks the
server about each one: a new payload URL the guard does not admit fails here."""
from __future__ import annotations

import http.client
from pathlib import Path

import pytest

from levain.webpush import center, held
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import _serve

NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def _get(port: int, path: str, headers: "dict[str, str] | None" = None, method: str = "GET") -> "tuple[int, bytes]":
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _payload_urls(tmp_path: Path) -> "list[str]":
    """Every tap target a push can carry, as the sender builds it (a ping with a notice row, one without, a held summary)."""
    store = PushStore(tmp_path / "pushq")
    return sorted({center.push_url(store, 7, "needs", "/"), center.push_url(store, 8, "notify", "/"),
                   center.push_url(store, 9, "summary", held.SUMMARY_URL),
                   center.push_url(store, 10, "summary", held.NOTICES_SUMMARY_URL)})


def test_a_launcher_tap_on_every_payload_url_reaches_the_cockpit_shell(tmp_path: Path) -> None:
    urls = _payload_urls(tmp_path)
    assert any("n=" in u for u in urls) and len(urls) >= 3, urls      # the rig really produced the item deep links
    src = _install(tmp_path)[1]
    with _serve(src) as (port, _):
        status, shell = _get(port, "/")
        assert status == 200
        for u in urls:
            status, body = _get(port, u, NAV)
            assert (status, body) == (200, shell), (u, status, body[:80])


def test_the_query_admission_is_the_taps_own_two_parameters_and_nothing_else(tmp_path: Path) -> None:
    src = _install(tmp_path)[1]
    with _serve(src) as (port, _):
        for ok in ("/?view=notifications", "/?view=notifications&n=n12", "/?view=notifications&n=p3", "/?view=needs"):
            assert _get(port, ok, NAV)[0] == 200, ok
        for bad in ("/?pair=abcdefgh12", "/?view=notifications&x=1", "/?view=other", "/?view=notifications&n=zz",
                    "/?view=notifications&n=n12&n=n13", "/?view=notifications&view=needs", "/?n=n12",
                    "/?view=notifications&n=n0", "/?", "/x?view=notifications"):
            assert _get(port, bad, NAV)[0] == 403, bad
        # a cross-site fetch, frame, HEAD or foreign Host of the tap URL stays refused
        tap = "/?view=notifications&n=n12"
        assert _get(port, tap, {**NAV, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})[0] == 403
        assert _get(port, tap, {**NAV, "Sec-Fetch-Dest": "iframe"})[0] == 403
        assert _get(port, tap, NAV, method="HEAD")[0] == 403
        assert _get(port, tap, {**NAV, "Host": "evil.example"})[0] == 403
