"""The team-view registry and the cockpit's /team_views.json back-link. A temp HOME/LEVAIN_HOME throughout: these
never read or write the real ~/.levain."""
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levain.team import registry as R
from levain.team import view as V
from tests.test_team_view import _Stub


@pytest.fixture(autouse=True)
def levain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def live_view():
    """A real team-view server (stub ledger) on an ephemeral port, serving its own stylesheet."""
    httpd = V.make_view_server(_Stub(), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()
    httpd.server_close()


def test_home_follows_levain_home_then_HOME(monkeypatch, tmp_path):
    assert R.home() == tmp_path / "home"
    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert R.home() == tmp_path / "h" / ".levain"


def test_entry_holds_only_five_fields_and_nothing_per_person(live_view):
    path = R.register("/work/ledgerline", live_view, "ledgerline")
    raw = json.loads(path.read_text())
    assert set(raw) == {"v", "repo", "url", "project", "pid", "started"}
    assert raw["pid"] == os.getpid() and raw["url"] == live_view


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://127.0.0.1:1/", "http://example.com:80/",
                                 "http://[::1]:7450/", "http://0.0.0.0:7450/", "http://127.0.0.1/", "file:///etc/passwd",
                                 "http://u:p@127.0.0.1:7450/", None, 5])
def test_only_loopback_http_with_a_port_is_ever_registered_or_listed(bad, levain_home):
    assert R.loopback_http_url(bad) is None
    with pytest.raises(ValueError):
        R.register("/r", bad, "p")
    # a hand-written entry with that URL is never listed (and never probed)
    d = R.registry_dir()
    d.mkdir(parents=True)
    (d / "1234-1.json").write_text(json.dumps({"v": 1, "repo": "/r", "url": bad, "project": "p", "pid": os.getpid(),
                                               "started": "t"}))
    assert R.live_views() == []


def test_a_live_view_is_listed_and_a_dead_pid_or_dead_port_is_not(live_view):
    R.register("/r", live_view, "ledgerline")
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]
    # dead pid, live URL: not listed (a reused-port impostor cannot revive an entry whose process is gone)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    R.register("/r2", live_view, "ghost", pid=dead.pid)
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]
    # live pid, URL nobody answers on: not listed
    R.register("/r3", "http://127.0.0.1:9/", "nobody-home")
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]


def test_a_live_pid_whose_port_is_some_other_server_is_not_listed(tmp_path):
    import http.server

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"hello")
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        R.register("/r", f"http://127.0.0.1:{srv.server_address[1]}/", "impostor")
        assert R.live_views() == []
    finally:
        srv.shutdown(); srv.server_close()


def test_two_views_registering_at_once_do_not_clobber(live_view):
    # per-entry files named <pid>-<port>.json: distinct views are distinct files, so no lock is needed
    threads = [threading.Thread(target=R.register, args=("/r", f"http://127.0.0.1:{port}/", f"p{port}"))
               for port in (41001, 41002, 41003)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = sorted(p.name for p in R.registry_dir().iterdir())
    assert names == [f"{os.getpid()}-{p}.json" for p in (41001, 41002, 41003)]
    assert not [n for n in names if n.startswith(".")]            # no temp file left behind


def test_unregister_and_prune_dead(live_view):
    path = R.register("/r", live_view, "ledgerline")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ghost = R.register("/g", live_view, "ghost", pid=dead.pid)
    R.prune_dead()
    assert path.exists() and not ghost.exists()
    R.unregister(path)
    assert not path.exists()
    R.unregister(path)                                              # idempotent


def test_serve_registers_and_removes_its_entry_on_clean_exit(live_view, monkeypatch):
    """serve() against the stub ledger: registered while running, gone after shutdown."""
    class GL(_Stub):
        class repo:
            toplevel = Path("/work/ledgerline")
        def team(self):
            return V_TEAM
    from tests.test_team_view import TEAM as V_TEAM
    seen = {}
    real = V.make_view_server

    def make(*a, **k):
        h = real(*a, **k)
        seen["h"] = h
        def sf():
            seen["files"] = [p.name for p in R.registry_dir().iterdir()]
            raise KeyboardInterrupt
        h.serve_forever = sf
        return h
    monkeypatch.setattr(V, "make_view_server", make)
    assert V.serve(GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0
    assert len(seen["files"]) == 1
    assert list(R.registry_dir().iterdir()) == []


# ---- the cockpit route ---------------------------------------------------------------------------------------

def _cockpit(tmp_path):
    from levain.web_server import make_server
    install = tmp_path / "install"
    (install / ".levain").mkdir(parents=True)
    httpd = make_server(install, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_cockpit_lists_only_live_views_and_stays_read_only(tmp_path, live_view):
    httpd = _cockpit(tmp_path)
    try:
        port = httpd.server_address[1]
        r, body = _get(port, "/team_views.json")
        assert r.status == 200 and json.loads(body) == {"views": []}      # no view: no tab
        R.register("/work/ledgerline", live_view, "ledgerline")
        r, body = _get(port, "/team_views.json")
        assert json.loads(body) == {"views": [{"project": "ledgerline", "repo": "/work/ledgerline", "url": live_view}]}
        assert "pid" not in body.decode() and "started" not in body.decode()
        # the guards every other cockpit GET has
        assert _get(port, "/team_views.json", {"Host": "evil.example"})[0].status == 403
        assert _get(port, "/team_views.json", {"Sec-Fetch-Site": "cross-site"})[0].status == 403
        for method in ("POST", "PUT", "DELETE"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, "/team_views.json", body=b"{}", headers={"Content-Type": "application/json"})
            assert c.getresponse().status >= 400
            c.close()
        # the new script is served and the page loads it
        assert _get(port, "/dashboard_team.js")[0].status == 200
        assert b"/dashboard_team.js" in _get(port, "/")[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_cockpit_script_never_uses_innerhtml_and_checks_the_scheme():
    from levain.web_server import load_web_asset
    js = load_web_asset("dashboard_team.js")
    assert "innerHTML" not in js and "outerHTML" not in js and "document.write" not in js
    assert '"http:"' in js and "textContent" in js
