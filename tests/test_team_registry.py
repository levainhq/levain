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


N = "a" * 32


class _Url(str):
    nonce = ""


@pytest.fixture
def live_view():
    """A real team-view server (stub ledger) on an ephemeral port, serving its own stylesheet."""
    httpd = V.make_view_server(_Stub(), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = _Url(f"http://127.0.0.1:{httpd.server_address[1]}/")
    url.nonce = httpd.nonce
    yield url
    httpd.shutdown()
    httpd.server_close()


def test_home_follows_levain_home_then_HOME(monkeypatch, tmp_path):
    assert R.home() == tmp_path / "home"
    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert R.home() == tmp_path / "h" / ".levain"


def test_entry_holds_only_five_fields_and_nothing_per_person(live_view):
    path = R.register("/work/ledgerline", live_view, "ledgerline", nonce=live_view.nonce)
    raw = json.loads(path.read_text())
    assert set(raw) == {"v", "repo", "url", "project", "pid", "started", "nonce"}
    assert raw["pid"] == os.getpid() and raw["url"] == live_view


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://127.0.0.1:1/", "http://example.com:80/",
                                 "http://[::1]:7450/", "http://0.0.0.0:7450/", "http://127.0.0.1/", "file:///etc/passwd",
                                 "http://u:p@127.0.0.1:7450/", None, 5])
def test_only_loopback_http_with_a_port_is_ever_registered_or_listed(bad, levain_home):
    assert R.loopback_http_url(bad) is None
    with pytest.raises(ValueError):
        R.register("/r", bad, "p", nonce=N)
    # a hand-written entry with that URL is never listed (and never probed)
    d = R.registry_dir()
    d.mkdir(parents=True)
    (d / "1234-1.json").write_text(json.dumps({"v": 1, "repo": "/r", "url": bad, "project": "p", "pid": os.getpid(),
                                               "started": "t", "nonce": N}))
    assert R.live_views() == []


def test_a_live_view_is_listed_and_a_dead_pid_or_dead_port_is_not(live_view):
    R.register("/r", live_view, "ledgerline", nonce=live_view.nonce)
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]
    # dead pid, live URL: not listed (a reused-port impostor cannot revive an entry whose process is gone)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    R.register("/r2", live_view, "ghost", pid=dead.pid, nonce=live_view.nonce)
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]
    # live pid, URL nobody answers on: not listed
    R.register("/r3", "http://127.0.0.1:9/", "nobody-home", nonce=N)
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
        R.register("/r", f"http://127.0.0.1:{srv.server_address[1]}/", "impostor", nonce=N)
        assert R.live_views() == []
    finally:
        srv.shutdown(); srv.server_close()


def test_two_views_registering_at_once_do_not_clobber(live_view):
    # per-entry files named <pid>-<port>.json: distinct views are distinct files, so no lock is needed
    threads = [threading.Thread(target=R.register, args=("/r", f"http://127.0.0.1:{port}/", f"p{port}"), kwargs={"nonce": N})
               for port in (41001, 41002, 41003)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = sorted(p.name for p in R.registry_dir().iterdir())
    assert names == [f"{os.getpid()}-{p}.json" for p in (41001, 41002, 41003)]
    assert not [n for n in names if n.startswith(".")]            # no temp file left behind


def test_unregister_and_prune_dead(live_view):
    path = R.register("/r", live_view, "ledgerline", nonce=live_view.nonce)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ghost = R.register("/g", live_view, "ghost", pid=dead.pid, nonce=live_view.nonce)
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
        R.register("/work/ledgerline", live_view, "ledgerline", nonce=live_view.nonce)
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


# ---- L3 round 1 fixes ------------------------------------------------------------------------------------------

import socket
import time


def _listener(handler):
    """A tiny raw TCP server: handler(conn) runs per connection in its own thread. Returns (port, stop)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=handler, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()

    def close():
        stop.set()
        srv.close()
    return srv.getsockname()[1], close


def test_a_slow_drip_server_cannot_hold_the_probe(levain_home):
    def drip(conn):
        try:
            conn.recv(2048)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nContent-Type: text/css\r\n\r\n")
            for _ in range(60):
                conn.sendall(b"x")
                time.sleep(0.3)
        except OSError:
            pass
        finally:
            conn.close()
    port, close = _listener(drip)
    try:
        R.register("/r", f"http://127.0.0.1:{port}/", "dripper", nonce=N)
        t = time.monotonic()
        assert R.live_views() == []
        assert time.monotonic() - t < 3.5          # one overall deadline, not per-read timeouts
    finally:
        close()


def test_a_redirect_is_not_followed(live_view):
    def redirect(conn):
        try:
            conn.recv(2048)
            conn.sendall(f"HTTP/1.1 302 Found\r\nLocation: {live_view}team_view.css\r\nContent-Length: 0\r\n\r\n".encode())
        except OSError:
            pass
        finally:
            conn.close()
    port, close = _listener(redirect)
    try:
        R.register("/r", f"http://127.0.0.1:{port}/", "redirector", nonce=N)
        assert R.live_views() == []                # a plain 200 from THIS address only
    finally:
        close()


def test_junk_files_before_a_valid_entry_do_not_hide_it(live_view):
    R.register("/r", live_view, "ledgerline", nonce=live_view.nonce)
    d = R.registry_dir()
    for i in range(1, 25):                          # sorts before the real <pid>-<port>.json
        (d / f"{i:07d}-1.json").write_text("not json")
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]


def test_prune_never_deletes_an_entry_it_cannot_parse_or_a_newer_version(live_view):
    d = R.registry_dir()
    d.mkdir(parents=True)
    newer = d / f"{os.getpid()}-7000.json"
    newer.write_text(json.dumps({"v": 2, "something": "new"}))
    garbled = d / f"{os.getpid()}-7001.json"
    garbled.write_text("{half")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = d / f"{dead.pid}-7002.json"
    gone.write_text("{half")                       # the pid in the FILENAME is dead: this one is stale
    R.prune_dead()
    assert newer.exists() and garbled.exists()
    assert not gone.exists()


def test_a_failed_write_leaves_no_temp_file_and_prune_clears_stale_ones(live_view, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(R.os, "replace", boom)
        with pytest.raises(OSError):
            R.register("/r", live_view, "ledgerline", nonce=live_view.nonce)
    assert [p.name for p in R.registry_dir().iterdir()] == []
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = R.registry_dir() / f".{dead.pid}-7003.tmp"
    stale.write_text("{}")
    R.prune_dead()
    assert not stale.exists()


def test_register_refuses_an_ipv6_url(levain_home):
    with pytest.raises(ValueError):
        R.register("/r", "http://[::1]:7450/", "p", nonce=N)


def test_live_views_does_not_use_the_cockpit_request_gate(tmp_path, live_view):
    httpd = _cockpit(tmp_path)
    try:
        R.register("/work/l", live_view, "ledgerline", nonce=live_view.nonce)
        gate = httpd.request_gate
        taken = 0
        while gate.acquire(blocking=False):         # the substrate gate is exhausted
            taken += 1
        try:
            assert _get(httpd.server_address[1], "/team_views.json")[0].status == 200
        finally:
            for _ in range(taken):
                gate.release()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_team_views_is_404_unless_the_cockpit_and_the_peer_are_loopback(tmp_path, live_view, monkeypatch):
    import levain.web_server as W
    httpd = _cockpit(tmp_path)
    try:
        R.register("/work/secret-repo", live_view, "secret-project", nonce=live_view.nonce)
        port = httpd.server_address[1]
        assert b"secret-project" in _get(port, "/team_views.json")[1]
        from types import SimpleNamespace
        real_source = httpd.levain_source
        httpd.levain_source = SimpleNamespace(write_scope=None)   # a read-only mesh bind
        httpd.is_loopback_bind = False               # an off-box bind
        r, body = _get(port, "/team_views.json")
        assert r.status == 404 and b"secret" not in body
        httpd.is_loopback_bind = True
        httpd.levain_source = real_source
        monkeypatch.setattr(W, "_peer_is_loopback", lambda addr: False)   # a non-loopback peer
        r, body = _get(port, "/team_views.json")
        assert r.status == 404 and b"secret" not in body
    finally:
        httpd.shutdown(); httpd.server_close()


def test_sigterm_handler_is_restored_and_a_real_view_exits_clean(tmp_path):
    import signal
    # (a) serve() restores the previous SIGTERM handler
    from tests.test_team_view import TEAM as T
    class GL(_Stub):
        class repo:
            toplevel = Path("/work/ledgerline")
        def team(self):
            return T
    sentinel = lambda *a: None  # noqa: E731
    prev = signal.signal(signal.SIGTERM, sentinel)
    real = V.make_view_server
    def make(*a, **k):
        h = real(*a, **k)
        def sf():
            raise KeyboardInterrupt
        h.serve_forever = sf
        return h
    try:
        V.make_view_server = make
        V.serve(GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3)
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        V.make_view_server = real
        signal.signal(signal.SIGTERM, prev)
    # (b) a real process: SIGTERM -> exit 0, no traceback, entry removed
    script = tmp_path / "run.py"
    script.write_text("import sys\nfrom pathlib import Path\nfrom tests.test_team_view import _Stub, TEAM\nfrom levain.team import view as V\n"
                      "class GL(_Stub):\n    class repo:\n        toplevel = Path('/work/l')\n    def team(self):\n        return TEAM\n"
                      "V.serve(GL(), host='127.0.0.1', port=0, recheck_days=30, ack_flag=3)\n")
    env = dict(os.environ, PYTHONPATH=os.getcwd(), LEVAIN_HOME=str(tmp_path / "home"))
    p = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert "team view ->" in p.stdout.readline()
        for _ in range(50):
            if list((tmp_path / "home" / "team-views").glob("*.json")):
                break
            time.sleep(0.1)
        assert list((tmp_path / "home" / "team-views").glob("*.json"))
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=10)
    finally:
        p.kill()
    assert p.returncode == 0 and "Traceback" not in err
    assert list((tmp_path / "home" / "team-views").glob("*.json")) == []


def test_a_live_pid_whose_port_is_answered_by_a_different_team_view_is_not_listed(live_view):
    other = V.make_view_server(_Stub(), port=0)           # a second team view: its own nonce
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert other.nonce != live_view.nonce
        # this entry claims the OTHER view's address but carries the first view's nonce (a reused port/pid)
        R.register("/r", f"http://127.0.0.1:{other.server_address[1]}/", "wrong-view", nonce=live_view.nonce)
        assert R.live_views() == []
        R.register("/r", live_view, "right-view", nonce=live_view.nonce)
        assert [v["project"] for v in R.live_views()] == ["right-view"]
    finally:
        other.shutdown(); other.server_close()


def test_the_nonce_never_reaches_the_cockpit_response(tmp_path, live_view):
    httpd = _cockpit(tmp_path)
    try:
        R.register("/work/l", live_view, "ledgerline", nonce=live_view.nonce)
        assert live_view.nonce.encode() not in _get(httpd.server_address[1], "/team_views.json")[1]
        assert _get(httpd.server_address[1], "/dashboard.css")[0].status == 200
    finally:
        httpd.shutdown(); httpd.server_close()


def test_the_script_ignores_a_response_older_than_the_latest_request():
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const pending = [], made = [];
let fire = null;
global.window = { location: {} };
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false,
  addEventListener: (ev, fn) => { fire = fn; },
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = () => new Promise((res) => pending.push(res));
%s
const ok = (v) => ({ ok: true, json: async () => ({ views: v }) });
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  fire();                                   // a second load while the first is still in flight
  pending[1](ok([{ project: "p", repo: "/r", url: "http://127.0.0.1:7463/" }]));   // the newest answers first
  await tick();
  pending[0](ok([]));                       // the OLDER response arrives late, with no views: must be ignored
  await tick();
  console.log(JSON.stringify({ shown: made.filter((c) => c.alive).length }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"shown": 1}
