"""ONESURFACE OS-4, one origin (W1a): the cockpit takes the no-port origin the phone port shared, so every old phone path
needs a landing and the signing families sign there. The families themselves (consent allow/deny, grant revoke, dispatch
sign/cancel/authorise, the drop envelope) are proven on ``https://<name>`` in their own files; these tests hold what the
move adds: the path map, the app shell without a Signer, the old link's script and the redirect listener's bare target."""
from __future__ import annotations

import json
import shutil
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.test_cockpit_consent_sheet import RP, _signer, home  # noqa: F401  (home is a fixture)
from tests.test_cockpit_sign_routes import FC, _call, _serving
from tests.test_grant_phone import world  # noqa: F401  (world is a fixture)
from tests.test_phone_redirect import RID, SECRET, V, _calls

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LEGACY = __import__("pathlib").Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "legacy_phone_link.js"
RUN = __import__("pathlib").Path(__file__).with_name("_redirect_script_run.mjs")


def _get(url: str, path: str, *, token: "str | None" = FC) -> "tuple[int, str]":
    headers = {"Host": RP}          # the name a proxy forwards, no port
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    try:
        with urlopen(Request(url + path, headers=headers), timeout=10) as r:
            return r.status, r.headers.get("Content-Type", "")
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "")


def _legacy(href: str) -> list:
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed")
    res = subprocess.run([node, str(RUN), str(LEGACY), href, ""], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_every_old_phone_path_has_a_landing_on_the_cockpits_one_origin(home, tmp_path) -> None:
    """Each old phone path asked of a cockpit pinned to ``https://<name>``, with its landing asserted."""
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        # the page itself: the old phone page's `/` is the cockpit's, and carries the script that moves an old link
        status, ctype = _get(url, "/", token=None)
        assert status == 200 and ctype.startswith("text/html")
        assert _get(url, "/legacy_phone_link.js", token=None)[0] == 200
        # the app shell both servers serve from one set of files: token-free, no Signer needed to ask
        for shell in ("/sw.js", "/app.webmanifest", "/icon-192.png", "/icon-512.png", "/badge-96.png",
                      "/apple-touch-icon.png"):
            assert _get(url, shell, token=None)[0] == 200, shell
        # the old page's own files and its data reads have no landing
        for gone in ("/phone.html", "/phone.js", "/phone.css", "/phone_app.js", "/phone_app.css", "/consent.json",
                     "/coord.json", f"/sign/{RID}/state", "/app/state.json", "/app/board.json"):
            assert _get(url, gone)[0] == 404, gone
        # the old POST routes: the cockpit answers the same families under /cockpit/ and /sign/, never at the old name
        for old in ("/app/verb", "/consent/x/challenge", "/question/1/answer", "/note/1/dismiss", "/band/answer",
                    "/passkey/key-line", f"/sign/{RID}/challenge", f"/sign/{RID}/answer", f"/sign/{RID}/deny",
                    f"/sign/{RID}/attempt", "/dispatch/dj-" + "0" * 32 + "/challenge", "/coord/drop",
                    "/coord/envelope-review", "/grant/x/revoke"):
            assert _call(url, old, {}, token=FC, host=RP)[0] == 404, old
        # the phone port's own pairing is not served here: the cockpit pairs through /pair
        for pair in ("/webpush/pair", "/webpush/pair/redeem"):
            assert _call(url, pair, {}, token=FC, host=RP)[0] == 404, pair
        # the one family of names both servers answer: web push, here behind the cockpit's own credential
        assert _get(url, "/webpush/key", token=None)[0] == 403 and _get(url, "/webpush/key")[0] == 200
        # the sign sheet's page is where an old sign link lands
        assert _get(url, f"/sign/{RID}")[0] == 200


def test_the_old_link_script_moves_a_sign_link_and_forwards_no_token() -> None:
    link = f"https://box.ts.net/#phone_token={SECRET}&sign={RID}&v={V}&choice=b"
    calls = _legacy(link)
    assert calls == [["replaceState", "/"], ["replace", f"/sign/{RID}#v={V}&choice=b"]], calls
    assert SECRET not in json.dumps(calls)
    # a pick needs its version, a bad id lands on the page, and the token alone lands on the page
    assert _legacy(f"https://box.ts.net/#phone_token={SECRET}&sign={RID}&choice=b")[-1] == ["replace", f"/sign/{RID}"]
    assert _legacy(f"https://box.ts.net/#phone_token={SECRET}&sign=bad")[-1] == ["replace", "/"]
    assert _legacy(f"https://box.ts.net/#phone_token={SECRET}")[-1] == ["replace", "/"]
    # flow's base link has no token
    assert _legacy(f"https://box.ts.net/#sign={RID}&v={V}")[-1] == ["replace", f"/sign/{RID}#v={V}"]


def test_the_old_link_script_leaves_the_cockpits_own_fragments_alone() -> None:
    for own in (f"#write_token={SECRET}", f"#write_token={SECRET}&sign={RID}", "#tab=board", "", "#v=" + V):
        assert _legacy("https://box.ts.net/" + own) == [], own


def test_the_old_listener_redirects_to_the_bare_origin_the_cockpit_now_serves() -> None:
    from levain.phone_server import make_redirect_server

    server = make_redirect_server("localhost", "https://box.ts.net", port=0)
    try:
        assert server.origin == "https://box.ts.net" and b'content="https://box.ts.net"' in server.page
    finally:
        server.server_close()
    calls = _calls(f"https://localhost/#phone_token={SECRET}&sign={RID}&v={V}", "https://box.ts.net")
    assert calls == [["replaceState", "/"], ["replace", f"https://box.ts.net/sign/{RID}#v={V}"]], calls
    assert SECRET not in json.dumps(calls)


def test_an_old_phone_link_opened_cross_site_still_reaches_the_landing_page(home, tmp_path) -> None:
    """L3 r1 (codex HIGH): a link tapped in a message or a mail page is a cross-site top-level navigation to ``/``; the
    shared read preamble refused it before the page that moves the link could load."""
    nav = {"Host": RP, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        def get(path: str, headers: "dict[str, str]") -> int:
            try:
                with urlopen(Request(url + path, headers=headers), timeout=10) as r:
                    return r.status
            except HTTPError as exc:
                return exc.code
        assert get("/", nav) == 200
        assert get("/?x=1", nav) == 403                                  # exact raw path only
        assert get("/", {**nav, "Sec-Fetch-Dest": "iframe"}) == 403      # a cross-site frame stays refused
        assert get("/cockpit/board.json", nav) == 403                    # no other path opens
