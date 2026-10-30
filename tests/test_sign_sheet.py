"""ONESURFACE OS-1 (levain project_memory/1025-32/onesurface/DESIGN.md §3, §4A.3, §5; PLAN.md row OS-1): the sign sheet
``GET /sign/<id>`` on the cockpit server. It is a static shell (no data, no token, no inline script) whose one script is
the cockpit's own ``/sign.js``; the request it shows and the ceremony it runs are the existing ``/cockpit/sign/<id>...``
routes. It alone may be framed, and only by its own origin (Q2 RULED OVERLAY): every other response keeps
``frame-ancestors 'none'`` and ``X-Frame-Options: DENY``."""
from __future__ import annotations

import http.client
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import pytest

from levain import web_server
from levain.consent.sign import Signer
from levain.http_guards import _CSP

from tests.test_cockpit_sign_origin import NS, PINNED, RP, _file, _Passkey
from tests.test_cockpit_sign_routes import RID, TOK, _serving

SHEET = f"/sign/{RID}"
SELF_CSP = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
FRAMER_CSP = _CSP + "; frame-src 'self'"      # the cockpit page, which shows the sheet over itself


def _raw(url: str, path: str, *, method: str = "GET", headers: "dict[str, str] | None" = None,
         body: "bytes | None" = None, conn: "http.client.HTTPConnection | None" = None,
         read: bool = True) -> "tuple[int, dict[str, str], bytes]":
    """One request; ``read=False`` takes the status and headers only (the change stream never ends its body)."""
    own = conn is None
    c = conn or http.client.HTTPConnection(urlsplit(url).hostname, urlsplit(url).port, timeout=15)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read() if read else b""
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        if own:
            c.close()


@pytest.fixture
def sheet(tmp_path: Path) -> Iterator[dict[str, Any]]:
    pk = _Passkey()
    qdir = tmp_path / "q"
    signer = Signer(qdir, (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    _file(qdir, RID)
    with _serving(tmp_path / "inst", signer=signer, extra_hosts=[RP]) as (url, httpd):
        yield {"url": url, "httpd": httpd, "qdir": qdir}


def test_the_sheet_is_a_static_shell_whose_one_script_is_the_cockpits_own(sheet) -> None:
    status, hdrs, body = _raw(sheet["url"], SHEET)
    assert status == 200 and hdrs["content-type"].startswith("text/html")
    page = body.decode()
    assert re.findall(r"<script\b[^>]*>", page) == ['<script src="/card_format.js">', '<script src="/sign.js">']   # no inline script
    assert re.search(r"<script src=\"/card_format.js\">\s*</script>\s*<script src=\"/sign.js\">\s*</script>", page)
    assert TOK not in page and RID not in page          # no token, no request data: those come from the gated read
    status, hdrs, js = _raw(sheet["url"], "/sign.js")
    assert status == 200 and hdrs["content-type"].startswith("text/javascript") and b"/cockpit/sign/" in js
    status, hdrs, js = _raw(sheet["url"], "/card_format.js")
    assert status == 200 and hdrs["content-type"].startswith("text/javascript") and b"parseMessage" in js


def test_only_the_sheet_may_be_framed_and_only_by_its_own_origin(sheet) -> None:
    status, hdrs, _ = _raw(sheet["url"], SHEET)
    assert status == 200
    assert hdrs["content-security-policy"] == SELF_CSP
    assert hdrs["x-frame-options"] == "SAMEORIGIN"
    assert hdrs["x-content-type-options"] == "nosniff"
    status, hdrs, _ = _raw(sheet["url"], SHEET, method="HEAD")
    assert status == 200 and hdrs["content-security-policy"] == SELF_CSP and hdrs["x-frame-options"] == "SAMEORIGIN"


def test_the_sheets_policy_differs_from_the_cockpits_in_frame_ancestors_alone() -> None:
    assert _CSP.count("frame-ancestors 'none'") == 1 and "frame-src" not in _CSP and "child-src" not in _CSP
    assert web_server._SHEET_CSP == SELF_CSP != _CSP
    assert web_server._COCKPIT_PAGE_CSP == FRAMER_CSP


def _route_table() -> "list[tuple[str, str]]":
    """Every route the server knows by name, plus a prefix route under each reserved prefix, a 404, the sheet's own
    near misses, and the write routes as POSTs."""
    paths = sorted(set(web_server._ASSETS) | set(web_server._RESERVED_PATHS))
    paths += [p + "x/y.json" for p in web_server._RESERVED_PREFIXES]
    paths += [f"/cockpit/sign/{RID}.json", f"/cockpit/sign/{RID}/challenge", "/nope", "/sign", "/sign/",
              "/sign/short", f"/sign/{RID}/", f"/sign/{RID}/state", f"/sign/{RID}.json", "/sign.js",
              "/pair"]
    out = [("GET", p) for p in paths] + [("HEAD", p) for p in paths]
    out += [("POST", p) for p in ("/edit", "/action", "/cockpit/verb", f"/cockpit/sign/{RID}/challenge", SHEET)]
    out += [("OPTIONS", SHEET), ("PUT", SHEET)]
    return out


def test_every_other_route_keeps_frame_ancestors_none(sheet) -> None:
    """...and only the cockpit page itself may frame anything (its own origin): the sheet."""
    seen = set()
    for method, path in _route_table():
        body = b"{}" if method == "POST" else None
        hdrs_in = {"X-Levain-Write-Token": TOK, "Content-Type": "application/json"} if method == "POST" else {}
        status, hdrs, _ = _raw(sheet["url"], path, method=method, headers=hdrs_in, body=body, read=False)
        seen.add(status)
        # the drop sheet is a second framed sheet: the cockpit page frames it, as it frames the sign sheet
        drop_sheet = path == web_server._COORD_SHEET and method in ("GET", "HEAD") and status == 200
        want = FRAMER_CSP if path == "/" and method in ("GET", "HEAD") and status == 200 else _CSP
        assert hdrs.get("content-security-policy") == (SELF_CSP if drop_sheet else want), (method, path, status, hdrs)
        assert hdrs.get("x-frame-options") == ("SAMEORIGIN" if drop_sheet else "DENY"), (method, path, status)
    assert {200, 404} <= seen      # the walk reached served routes and unserved ones


def test_a_kept_alive_connection_after_the_sheet_is_back_to_none(sheet) -> None:
    u = urlsplit(sheet["url"])
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    try:
        assert _raw(sheet["url"], SHEET, conn=conn)[1]["x-frame-options"] == "SAMEORIGIN"
        for path in ("/sign.js", "/nope", "/"):
            _s, hdrs, _ = _raw(sheet["url"], path, conn=conn)
            assert hdrs["content-security-policy"] == (FRAMER_CSP if path == "/" else _CSP), path
            assert hdrs["x-frame-options"] == "DENY", path
        assert _raw(sheet["url"], SHEET, conn=conn)[1]["content-security-policy"] == SELF_CSP
    finally:
        conn.close()


def test_without_a_signer_the_sheet_is_404(tmp_path: Path) -> None:
    with _serving(tmp_path) as (url, _httpd):
        status, hdrs, _ = _raw(url, SHEET)
        assert status == 404 and hdrs["x-frame-options"] == "DENY"


def test_the_shell_is_token_free_off_box_while_its_data_is_not(sheet) -> None:
    proxied = {"Host": RP}          # the https name a proxy forwards: the off-box read gate applies
    assert _raw(sheet["url"], SHEET, headers=proxied)[0] == 200
    assert _raw(sheet["url"], "/sign.js", headers=proxied)[0] == 200
    assert _raw(sheet["url"], f"/cockpit/sign/{RID}.json", headers=proxied)[0] == 403
    assert _raw(sheet["url"], f"/cockpit/sign/{RID}.json", headers={**proxied, "X-Levain-Write-Token": TOK})[0] == 200


def test_a_link_opened_from_another_site_reaches_the_sheet_and_nothing_else(sheet) -> None:
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert _raw(sheet["url"], SHEET, headers=nav)[0] == 200
    # a cross-site fetch, frame, HEAD or a near path of it stays refused, and the data it would read always is
    assert _raw(sheet["url"], SHEET, headers={**nav, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})[0] == 403
    assert _raw(sheet["url"], SHEET, headers={**nav, "Sec-Fetch-Dest": "iframe"})[0] == 403
    assert _raw(sheet["url"], SHEET, method="HEAD", headers=nav)[0] == 403
    assert _raw(sheet["url"], "/sign/short", headers=nav)[0] == 403
    assert _raw(sheet["url"], "/sign.js", headers=nav)[0] == 403
    assert _raw(sheet["url"], f"/cockpit/sign/{RID}.json", headers={**nav, "X-Levain-Write-Token": TOK})[0] == 403
    assert _raw(sheet["url"], SHEET, headers={**nav, "Host": "evil.example"})[0] == 403


def test_a_cross_site_link_with_a_query_after_the_id_reaches_the_sheet_and_a_query_on_a_near_path_does_not(sheet) -> None:
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert _raw(sheet["url"], SHEET + "?utm=x", headers=nav)[0] == 200      # the id is read past the query
    assert _raw(sheet["url"], SHEET + "/?utm=x", headers=nav)[0] == 403   # a longer path is not the sheet
    assert _raw(sheet["url"], "/pair?x", headers=nav)[0] == 403            # the pair page keeps its exact path
    assert _raw(sheet["url"], SHEET + "?x", headers={**nav, "Sec-Fetch-Dest": "iframe"})[0] == 403


def test_the_cockpit_pages_frame_src_is_appended_to_a_policy_that_names_none() -> None:
    assert "frame-src" not in _CSP
    assert web_server._COCKPIT_PAGE_CSP == _CSP + "; frame-src 'self'"


def test_a_malformed_id_is_not_a_sheet(sheet) -> None:
    for path in ("/sign/short", "/sign/a b c d e f g", "/sign/" + "a" * 65, "/sign/np-sign..route", f"{SHEET}/"):
        assert _raw(sheet["url"], path.replace(" ", "%20"))[0] == 404, path


def test_the_one_request_read_names_the_rp_id_the_sheet_keys_its_passkey_record_on(sheet) -> None:
    """H6: the sheet remembers which passkey this browser used under the rpId and reads it back under the same one; that
    value is the read's ``rp_id`` (the Signer's), not the page's hostname, which can differ from it."""
    import json as _json
    status, _h, body = _raw(sheet["url"], f"/cockpit/sign/{RID}.json", headers={"X-Levain-Write-Token": TOK})
    assert status == 200
    assert _json.loads(body)["rp_id"] == RP
