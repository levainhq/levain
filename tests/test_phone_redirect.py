"""ONESURFACE OS-4: the old phone origin as a redirect only (``levain consent phone --redirect-to <origin>``).

A link the phone already holds (``https://<name>/#phone_token=<t>&sign=<id>&v=<16 hex>&choice=<k>``, in a stored
flowConnect notification or a message) must land on the cockpit's sheet, ``<origin>/sign/<id>#v=<v>&choice=<k>``, and
the token in it must go nowhere: not to the cockpit, not into the bar, not into the history entry, not as a referrer.
The redirect server mints no token and serves no data. Browsers: headless Chrome with its own profile only."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from levain.phone_server import make_redirect_server, redirect_origin
from tests.test_cockpit_flip_parity import _drive, needs_browser

SECRET = "OLDRUNTOKEN" + "x" * 32
RID = "np-run0001-c9f2a1"
V = "0123456789abcdef"


class _Target(BaseHTTPRequestHandler):
    """The cockpit's stand-in: records every request line and header it is sent, and serves an empty page."""

    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append(self.requestline + "\n" + str(self.headers))
        body = b"<!doctype html><title>sheet</title><p>sheet</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:
        pass


@contextmanager
def _serving(server: Any) -> Iterator[Any]:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _pair() -> Iterator[tuple[str, str, Any]]:
    """(the old phone page, the cockpit origin, the cockpit stand-in's handler class)."""
    seen: list[str] = []
    handler = type("_T", (_Target,), {"seen": seen})
    target = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    origin = f"http://127.0.0.1:{target.server_address[1]}"
    redirect = make_redirect_server("localhost", origin, port=0)
    with _serving(target), _serving(redirect):
        yield f"http://localhost:{redirect.server_address[1]}", origin, handler


def _get(base: str, path: str, headers: "dict[str, str] | None" = None, method: str = "GET") -> tuple[int, dict, bytes]:
    host, port = base.split("//", 1)[1].split(":")
    conn = HTTPConnection("127.0.0.1", int(port), timeout=10)
    conn.request(method, path, headers={"Host": f"{host}:{port}", **(headers or {})})
    r = conn.getresponse()
    out = r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    conn.close()
    return out


# -- the configured origin ------------------------------------------------------------------------------------------

def test_the_target_is_a_bare_origin_and_nothing_else() -> None:
    assert redirect_origin("https://Box.tail1.ts.net:8443/") == "https://box.tail1.ts.net:8443"
    assert redirect_origin("https://box.tail1.ts.net") == "https://box.tail1.ts.net"
    assert redirect_origin("http://127.0.0.1:7777") == "http://127.0.0.1:7777"
    assert redirect_origin("http://localhost:7777") == "http://localhost:7777"
    for bad in ("https://box/sign/x", "https://box/?q=1", "https://box/#f", "https://u:p@box", "http://box.ts.net",
                "javascript:alert(1)", "box.ts.net", "https://", "https://bo x", "https://box:99999",
                "https://box\n.net", "//box.ts.net"):
        with pytest.raises(ValueError):
            redirect_origin(bad)


# -- the server: one page, one script, no data, no token -----------------------------------------------------------

def test_it_serves_the_page_and_its_script_and_nothing_else() -> None:
    with _pair() as (page, origin, _):
        for path in ("/", "/phone.html", "/?view=grants"):
            status, hdrs, body = _get(page, path)
            assert status == 200 and hdrs["content-type"].startswith("text/html"), (path, status)
            assert f'<meta name="levain-redirect-to" content="{origin}">' in body.decode(), body
            assert hdrs["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in hdrs["content-security-policy"]
            assert "script-src 'self'" in hdrs["content-security-policy"]
            assert body.count(b"<script") == 1 and b'src="/phone_redirect.js"' in body
        status, hdrs, body = _get(page, "/phone_redirect.js")
        assert status == 200 and hdrs["content-type"].startswith("text/javascript") and b"location.replace" in body
        for path in ("/consent.json", "/coord.json", f"/sign/{RID}/state", "/webpush/key", "/sw.js",
                     "/app.webmanifest", "/phone.js", "/app/state.json", "/passkey/key-line"):
            status, _, _ = _get(page, path, {"X-Levain-Phone-Token": SECRET})
            assert status == 404, path
        status, _, _ = _get(page, "/consent/x/answer", {"Content-Type": "application/json"}, method="POST")
        assert status in (404, 405, 501)


def test_it_mints_no_token() -> None:
    server = make_redirect_server("localhost", "http://127.0.0.1:1", port=0)
    try:
        assert not hasattr(server, "phone_token")
    finally:
        server.server_close()


def test_a_foreign_host_is_refused_and_a_link_opened_from_another_app_is_not() -> None:
    with _pair() as (page, _, _):
        port = page.rsplit(":", 1)[1]
        assert _get(page, "/", {"Host": f"evil.example:{port}"})[0] == 403
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        assert _get(page, "/", nav)[0] == 200
        # a cross-site fetch of the page (not a navigation) and of its script stay refused
        assert _get(page, "/", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})[0] == 403
        assert _get(page, "/phone_redirect.js", {**nav, "Sec-Fetch-Dest": "script"})[0] == 403


def test_the_origin_in_the_page_is_escaped() -> None:
    # redirect_origin refuses every character that could leave the attribute; the page escapes it anyway
    from levain.phone_server import _redirect_page

    assert '"' not in _redirect_page('x"><script>').split('content="', 1)[1].split('"', 1)[0]


# -- the link file --------------------------------------------------------------------------------------------------

def test_the_link_file_holds_the_page_only(tmp_path: Path, monkeypatch) -> None:
    from levain import phone_server

    link = tmp_path / "link.json"
    got: dict = {}

    def serve(self: Any) -> None:
        got.update(json.loads(link.read_text()))
        assert oct(link.stat().st_mode & 0o777) == "0o600"
        raise KeyboardInterrupt

    monkeypatch.setattr(phone_server._RedirectServer, "serve_forever", serve)
    rc = phone_server.run_phone_redirect("https://box.tail1.ts.net:8443", name="localhost", tailscale=False, port=0,
                                         link_file=link)
    assert rc == 0
    assert got == {"page": "https://box.tail1.ts.net:8443/", "pid": os.getpid()}
    assert not link.exists()              # removed when the run stops


def test_a_failed_link_file_write_keeps_the_file_another_run_wrote(tmp_path: Path, monkeypatch) -> None:
    from levain import phone_server
    from levain.consent import sign

    link = tmp_path / "link.json"
    theirs = json.dumps({"page": "https://box.tail1.ts.net:8443/", "pid": os.getpid() + 1}) + "\n"
    link.write_text(theirs)

    def refuse(*a: Any, **k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sign, "_write_over", refuse)
    monkeypatch.setattr(phone_server._RedirectServer, "serve_forever", lambda self: pytest.fail("served"))
    rc = phone_server.run_phone_redirect("https://box.tail1.ts.net:8443", name="localhost", tailscale=False, port=0,
                                         link_file=link)
    assert rc == 1 and link.read_text() == theirs


def test_the_script_reads_a_sign_id_by_the_servers_own_rule() -> None:
    from levain.sign_request_id import ID

    script = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "phone_redirect.js").read_text()
    assert f"/^{ID.pattern}$/.test(id)" in script


def test_the_cli_runs_the_redirect_mode(monkeypatch) -> None:
    from levain import phone_server
    from levain.cli import main

    calls: list = []
    monkeypatch.setattr(phone_server, "run_phone_redirect", lambda *a, **kw: calls.append((a, kw)) or 0)
    assert main(["consent", "phone", "--name", "box.ts.net", "--redirect-to", "https://box.ts.net:8443"]) == 0
    (args, kw), = calls
    assert args == ("https://box.ts.net:8443",) and kw["name"] == "box.ts.net"


def test_the_old_run_token_is_refused_consent_phone_needs_redirect_to(capsys) -> None:
    """W1b: the phone port's run token is gone. `levain consent phone` without --redirect-to refuses before binding
    anything, and --token-file no longer parses."""
    from levain.cli import main

    assert main(["consent", "phone", "--name", "b.ts.net"]) == 2
    err = capsys.readouterr().err
    assert "--redirect-to" in err and "levain cockpit pair" in err
    with pytest.raises(SystemExit) as parsed:
        main(["consent", "phone", "--name", "b.ts.net", "--token-file", "/tmp/x"])
    assert parsed.value.code == 2
    capsys.readouterr()


def test_the_cli_refuses_redirect_with_the_full_ports_options(capsys) -> None:
    from levain.cli import main

    for extra in (["--sign-namespace", "flow-np"], ["--queue-dir", "/tmp"]):
        assert main(["consent", "phone", "--name", "b.ts.net", "--redirect-to", "https://b.ts.net:8443", *extra]) == 2
        assert "--redirect-to" in capsys.readouterr().err


# -- the redirect in a browser --------------------------------------------------------------------------------------

LANDED = "location.port === {port!r} && location.pathname === {path!r}"


@needs_browser
def test_an_old_sign_link_lands_on_the_sheet_and_its_token_goes_nowhere(tmp_path: Path) -> None:
    with _pair() as (page, origin, target):
        port = origin.rsplit(":", 1)[1]
        old = f"{page}/#phone_token={SECRET}&sign={RID}&v={V}&choice=b"
        out = _drive(tmp_path, old, [
            {"until": LANDED.format(port=port, path=f"/sign/{RID}"), "ms": 20000},
            {"eval": "location.href"},
            {"eval": "document.referrer"},
            {"eval": "history.length"},
        ])
        control = _drive(tmp_path, f"{origin}/sign/{RID}#v={V}&choice=b", [{"eval": "history.length"}])
    v = out["values"]
    assert v[1] == f"{origin}/sign/{RID}#v={V}&choice=b", v
    assert v[2] == ""                                   # no referrer reached the cockpit's page
    # as many entries as opening the sheet directly (Chrome replaces a navigation made during load either way, so the
    # script's own use of location.replace is proven by test_the_script_scrubs_the_bar_first_and_replaces_its_own_entry)
    assert v[3] == control["values"][0], (v[3], control)
    assert target.seen and all(SECRET not in r for r in target.seen), target.seen
    assert all("phone_token" not in r for r in target.seen)


@needs_browser
def test_every_old_shape_lands_where_it_should(tmp_path: Path) -> None:
    with _pair() as (page, origin, target):
        port = origin.rsplit(":", 1)[1]
        cases = [
            # (old link, the landing the cockpit must see)
            (f"/#phone_token={SECRET}", f"{origin}/"),
            (f"/#sign={RID}&v={V}", f"{origin}/sign/{RID}#v={V}"),              # flow's base link, no token
            (f"/#v={V}&sign={RID}&phone_token={SECRET}&choice=b", f"{origin}/sign/{RID}#v={V}&choice=b"),  # any order
            (f"/#phone_token={SECRET}&sign={RID}&choice=b", f"{origin}/sign/{RID}"),       # a pick needs its version
            (f"/#sign={RID}&v=XYZ&choice=b", f"{origin}/sign/{RID}"),
            (f"/#sign={RID}&v={V}&choice=B!", f"{origin}/sign/{RID}#v={V}"),
            (f"/#sign=bad&phone_token={SECRET}", f"{origin}/"),
            (f"/#sign=..%2F..%2Fx&v={V}", f"{origin}/"),
            (f"/phone.html#phone_token={SECRET}&sign={RID}&v={V}", f"{origin}/sign/{RID}#v={V}"),
            (f"/?view=grants&pair=abcdefgh12#phone_token={SECRET}", f"{origin}/"),
            (f"/#sign={RID}&sign=np-other0001-aaaa&v={V}", f"{origin}/sign/{RID}#v={V}"),   # the first one counts
        ]
        steps: list[dict] = []
        for old, want in cases:
            # the mark is on the page being left, so a landing equal to the last one cannot pass before it navigates
            steps += [{"eval": f"(() => {{ window.__left = 1; location.href = {json.dumps(page + old)}; return 1; }})()"},
                      {"until": f"!window.__left && location.port === {port!r} && location.href === {json.dumps(want)}"
                                f" && location.href",
                       "ms": 20000, "may_timeout": True}]
        out = _drive(tmp_path, f"{origin}/start", steps)
    got = out["values"][1::2]
    assert got == [w for _, w in cases], list(zip([c for c, _ in cases], got))
    assert all(SECRET not in r and "phone_token" not in r for r in target.seen), target.seen


# -- the script's own order: Chrome replaces a navigation made during load anyway, so the browser tests above cannot
# tell location.replace from an href set; this runs the script with a recording location and history

SCRIPT = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "phone_redirect.js"
RUN = Path(__file__).with_name("_redirect_script_run.mjs")


def _calls(href: str, meta: str = "https://box.ts.net:8443") -> list:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed")
    res = subprocess.run([node, str(RUN), str(SCRIPT), href, meta], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_the_script_scrubs_the_bar_first_and_replaces_its_own_entry() -> None:
    calls = _calls(f"https://box.ts.net/?pair=abcdefgh12#phone_token={SECRET}&sign={RID}&v={V}&choice=b")
    assert calls == [["replaceState", "/"], ["replace", f"https://box.ts.net:8443/sign/{RID}#v={V}&choice=b"]], calls


def test_the_script_goes_nowhere_without_a_configured_origin() -> None:
    for meta in ("", "javascript:alert(1)", "https://box.ts.net/sign/x", "https://evil.example@box.ts.net"):
        assert _calls(f"https://box.ts.net/#phone_token={SECRET}&sign={RID}", meta) == [["replaceState", "/"]], meta
