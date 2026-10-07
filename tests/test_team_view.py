"""levain team view: the read-only team view. Pane contents against a seeded ledger, the GET-only contract,
and the privacy line (no count keyed by a person). Behaviour reproduced against the live demo server first."""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from levain.team import entry as E
from levain.team import index as I
from levain.team import roles as R
from levain.team import view as V

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
TEAM = R.Team(project="ledgerline", owner="ana", members={"ana": "a@x.io", "ben": "b@x.io"},
              client_owners=["Dana"])


@pytest.fixture(autouse=True)
def _isolated_levain_home(tmp_path, monkeypatch):
    # autouse: nothing in this file may reach the real ~/.levain (the view registers there when serve() runs)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))


def _seal(chains, author, ts, type_, **kw):
    e = E.build(author, type_, **kw)
    e["ts"] = ts
    sealed = E.seal(e, chains.get(author, ""))
    chains[author] = sealed["hash"]
    return sealed


def _ledger():
    chains: dict = {}
    old = (NOW - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ruling = _seal(chains, "ana", new, "decision", kind="ruling", owner="client:Dana", paths=["billing.py"],
                   words="Round per line.", reason="AP")
    q = _seal(chains, "ana", new, "question", owner="ana", paths=["billing.py"], summary="half-even?")
    t = _seal(chains, "ana", new, "tension", owner="ben", paths=["tax/**"], summary="vat")
    gone = _seal(chains, "ana", new, "decision", kind="ruling", owner="carl", paths=["infra/**"], words="No infra.")
    stale = _seal(chains, "ana", old, "finding", paths=["legacy/**"], summary="no callers", recheck="grep x")
    acks = [_seal(chains, "ben", new, "ack", refs=[ruling["id"]], session=f"s{i}") for i in range(3)]
    entries = [ruling, q, t, gone, stale]
    ana_lines = [json.dumps(e, sort_keys=True) for e in entries]
    ben_lines = [json.dumps(e, sort_keys=True) for e in acks]
    ledger = I.build([("ana/a.jsonl", ana_lines), ("ben/b.jsonl", ben_lines)], owner="ana")
    return ledger, ruling


def test_panes_from_a_seeded_ledger():
    ledger, ruling = _ledger()
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    assert [c["type"] for c in m["waiting"]] == ["question"]            # ben's tension is not ana's to answer
    assert [(r["path"], r["acks"], r["review"]) for r in m["stopped"]] == [("billing.py", 3, True)]
    assert {c["owner"] for c in m["held"] if "not on team.toml" in c["why"]} == {"carl"}   # carl left team.toml
    assert [c["summary"] for c in m["held"] if "recheck" in c["why"]] == ["no callers"]   # 60 days > 30
    assert {g["path"] for g in m["in_force"]} == {"billing.py", "tax/**", "infra/**", "legacy/**"}
    assert m["you"] == "ana"
    mb = V.build_model(TEAM, ledger, "ben", None, "x", now=NOW)
    assert [c["type"] for c in mb["waiting"]] == ["tension"]


def _walk(o):
    """Every key and every string value in a JSON-like structure."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
    elif isinstance(o, str):
        yield o


def test_no_count_is_keyed_by_a_person():
    # the acker's handle cannot appear as a word anywhere, so a substring hit means the person leaked
    chains: dict = {}
    ts = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    team = R.Team(project="p", owner="ana", members={"ana": "a@x.io", "zq9wv": "z@x.io"})
    r = _seal(chains, "ana", ts, "decision", kind="ruling", owner="ana", paths=["a.py"], words="Keep it.")
    acks = [_seal(chains, "zq9wv", ts, "ack", refs=[r["id"]], session=f"sess{i}") for i in range(3)]
    ledger = I.build([("ana/a.jsonl", [json.dumps(r, sort_keys=True)]),
                      ("zq9wv/b.jsonl", [json.dumps(e, sort_keys=True) for e in acks])], owner="ana")
    m = V.build_model(team, ledger, "ana", None, "x", now=NOW)
    assert m["stopped"] and m["stopped"][0]["acks"] == 3
    for token in _walk(m["stopped"]):
        assert "zq9wv" not in token and not token.startswith("sess")
    assert not ({"author", "session", "by", "who"} & {t for t in _walk(m["stopped"])})
    page = V.render_html(m)
    pane2 = page.split('id="pane2"')[1].split('id="pane3"')[0]
    assert "zq9wv" not in pane2 and "sess" not in pane2


def test_page_escapes_recorded_text():
    ledger, _ = _ledger()
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    m["waiting"][0]["summary"] = "<script>alert(1)</script>"
    assert "<script>alert(1)" not in V.render_html(m)


class _Stub:
    remote = None           # a clone with no remote: the view has nothing to fetch

    def __init__(self):
        self.warnings: list[str] = []

    def snapshot(self):
        ledger, _ = _ledger()
        return "sha", TEAM, ledger

    def handle(self, team):
        return "ana"

    def read_canon(self, sha):
        return None

    def state_hash(self, ledger, team):
        return "x"

    def team_history_problems(self, team, rev=None):
        return []


@pytest.fixture
def server():
    httpd = V.make_view_server(_Stub(), port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path="/", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_get_routes_serve_and_every_other_method_is_405(server):
    r, body = _req(server, "GET")
    assert r.status == 200 and b"Waiting on you" in body and b"carl" in body
    assert _req(server, "GET", "/team_view.css")[0].status == 200
    assert json.loads(_req(server, "GET", "/view.json")[1])["project"] == "ledgerline"
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "FOO"):
        r, _ = _req(server, method)
        assert r.status == 405, method
        assert r.getheader("Allow") == "GET"
        assert "frame-ancestors" in (r.getheader("Content-Security-Policy") or "")


def test_page_has_no_form_no_write_control_and_loads_only_own_assets(server):
    import re
    text = _req(server, "GET")[1].decode()
    assert "<form" not in text and "<textarea" not in text and 'method="' not in text.lower()
    assert set(re.findall(r'<input[^>]*type="(\w+)"', text)) == {"search"}      # filters only
    assert "<script>" not in text and " style=" not in text                       # CSP: no inline script or style
    srcs = re.findall(r'(?:src|href)="([^"]+)"', text)
    external = [u for u in srcs if u.startswith("http") and u != V.DEFAULT_COCKPIT_URL]
    assert external == []                                                         # only the cockpit nav link leaves
    assert {"/dashboard.css", "/team_view.css", "/team_view.js"} <= set(srcs)
    assert "data-clamp" in text and 'id="inforce-q"' in text


def test_cockpit_stylesheet_and_assets_are_served(server):
    for path, ctype in (("/dashboard.css", "text/css"), ("/team_view.css", "text/css"),
                        ("/team_view.js", "text/javascript")):
        r, body = _req(server, "GET", path)
        assert r.status == 200 and r.getheader("Content-Type").startswith(ctype) and body
    assert b".pbody.clamped" in _req(server, "GET", "/dashboard.css")[1]


def test_path_filter_narrows_every_pane():
    ledger, _ = _ledger()
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW, path_filter="billing.py")
    assert [c["type"] for c in m["waiting"]] == ["question"]
    assert [r["path"] for r in m["stopped"]] == ["billing.py"]
    assert m["held"] == []
    assert [g["path"] for g in m["in_force"]] == ["billing.py"]
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW, path_filter="tax/handler.py")
    assert [g["path"] for g in m["in_force"]] == ["tax/**"]                       # a glob governs the file
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW, path_filter="infra")
    assert [c["owner"] for c in m["held"]] == ["carl"]                        # substring of the glob


def test_path_filter_over_http(server):
    r, body = _req(server, "GET", "/view.json?path=legacy")
    assert [g["path"] for g in json.loads(body)["in_force"]] == ["legacy/**"]
    assert b"narrowed to what governs" in _req(server, "GET", "/?path=legacy")[1]


def test_host_guard_and_unknown_route(server):
    assert _req(server, "GET", headers={"Host": "evil.example"})[0].status == 403
    assert _req(server, "GET", "/nope")[0].status == 404


def test_refuses_a_non_loopback_bind():
    with pytest.raises(ValueError):
        V.make_view_server(_Stub(), host="0.0.0.0", port=0)


# ---- L3 round 1 fixes: each test was run against the code before the fix and failed -----------------------------

def _build(specs, *, team=TEAM, extra_members=()):
    """specs: [(author, type, ts_offset_hours, kwargs)] in order; returns (ledger, [sealed entries])."""
    chains: dict = {}
    files: dict = {}
    out = []
    for author, type_, hours, kw in specs:
        ts = (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        e = E.build(author, type_, **{k: v for k, v in kw.items() if k not in ("force_paths",)})
        if "force_paths" in kw:
            e["paths"] = kw["force_paths"]
        e["ts"] = ts
        e = E.seal(e, chains.get(author, ""))
        chains[author] = e["hash"]
        files.setdefault(author, []).append(json.dumps(e, sort_keys=True))
        out.append(e)
    ledger = I.build([(f"{a}/f.jsonl", l) for a, l in files.items()], owner=team.owner)
    return ledger, out


def test_an_ack_counts_once_per_distinct_path_and_only_for_rulings():
    # ack_flag is PER PATH. One ack entry adds at most 1 to a path however many refs or duplicate globs reach it;
    # an ack of a non-ruling adds nothing; the header total is the number of distinct ack entries that counted.
    specs = [
        ("ana", "decision", 9, dict(kind="ruling", owner="ana", paths=["a.py"], words="A.", force_paths=["a.py", "a.py"])),
        ("ana", "decision", 8, dict(kind="ruling", owner="ana", paths=["a.py", "b.py"], words="AB.")),
        ("ana", "question", 7, dict(owner="ana", paths=["a.py"], summary="q?")),
    ]
    ledger, ents = _build(specs)
    r1, r2, q = (e["id"] for e in ents)
    chains: dict = {}
    more = []
    for i, refs in enumerate(([r1, r1, r2], [r2], [q])):
        e = E.build("ben", "ack", refs=refs, session=f"s{i}")
        e["ts"] = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        e = E.seal(e, chains.get("ben", ""))
        chains["ben"] = e["hash"]
        more.append(json.dumps(e, sort_keys=True))
    files = [(f.rel, [json.dumps(x, sort_keys=True) for x in f.entries]) for f in ledger.files] + [("ben/f.jsonl", more)]
    ledger = I.build(files, owner="ana")
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    rows = {r["path"]: r["acks"] for r in m["stopped"]}
    assert rows == {"a.py": 2, "b.py": 2}        # ack0 (refs r1,r1,r2) once on a.py and once on b.py; ack1 likewise
    assert m["ack_total"] == 2                    # ack2 acknowledged a question: not counted anywhere
    assert [r["review"] for r in m["stopped"]] == [False, False]
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW, ack_flag=2)
    assert all(r["review"] for r in m["stopped"])  # the threshold is compared per path
    assert "<span class=\"src\">2</span>" in V.render_html(m).split('id="pane2"')[1].split('id="pane3"')[0]


def test_ipv6_and_non_ipv4_loopback_are_refused_cleanly():
    for host in ("::1", "[::1]", "0.0.0.0", "evil.example"):
        with pytest.raises(ValueError, match="127.0.0.1"):
            V.make_view_server(_Stub(), host=host, port=0)
    V.make_view_server(_Stub(), host="localhost", port=0).server_close()


def test_a_bad_port_is_a_clean_error():
    for port in (-1, 65536, 99999):
        with pytest.raises(ValueError, match="port"):
            V.make_view_server(_Stub(), port=port)


def test_cli_reports_bad_host_and_port_without_a_traceback(capsys, tmp_path):
    from levain.cli import main
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(SystemExit) as ei:
        main(["team", "view", "--repo", str(tmp_path), "--port", "99999"])
    assert ei.value.code == 2 and "port" in capsys.readouterr().err


class _SlowStub(_Stub):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.set()
        self.release.wait(5)
        with self.lock:
            self.active -= 1
        return super().snapshot()


def test_model_generation_is_serialized_and_assets_are_not_gated():
    stub = _SlowStub()
    stub.snapshot_called = False
    httpd = V.make_view_server(_Stub(), port=0)
    httpd.ledger_reader = stub
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        results = []
        ts = [threading.Thread(target=lambda: results.append(_req(port, "GET")[0].status)) for _ in range(2)]
        ts[0].start()
        assert stub.entered.wait(5)
        ts[1].start()
        ts[1].join(10)                                                 # answered while the first is still reading
        assert _req(port, "GET", "/team_view.css")[0].status == 200   # an asset is not queued behind the ledger read
        stub.release.set()
        ts[0].join(10)
        assert results == [503, 200]
        assert stub.peak == 1                                          # never two snapshots at once
    finally:
        stub.release.set()
        httpd.shutdown()
        httpd.server_close()


def test_a_ledger_failure_does_not_echo_the_error_to_the_client(capfd):
    class Boom(_Stub):
        def snapshot(self):
            if getattr(self, "armed", False):
                raise RuntimeError("fatal: not a git repository: /Users/secret/path/.git")
            return super().snapshot()
    stub = Boom()
    httpd = V.make_view_server(stub, port=0)
    stub.armed = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r, body = _req(httpd.server_address[1], "GET")
        assert r.status == 503 and b"secret" not in body and b"fatal" not in body
        assert b"ledger unavailable" in body
        assert "/Users/secret/path" in capfd.readouterr().err          # the detail goes to the operator's stderr
    finally:
        httpd.shutdown()
        httpd.server_close()


def _plus(ledger, author, items):
    """The ledger with extra sealed entries appended to one author's file. items: [(type, kwargs, mutate)]; returns
    (new ledger, [sealed ids])."""
    chains = {author: ""}
    lines, ids = [], []
    for typ, kw, mutate in items:
        e = E.build(author, typ, **kw)
        e["ts"] = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if mutate:
            mutate(e)
        e = E.seal(e, chains[author])
        chains[author] = e["hash"]
        lines.append(json.dumps(e, sort_keys=True))
        ids.append(e["id"])
    files = [(f.rel, [json.dumps(x, sort_keys=True) for x in f.entries]) for f in ledger.files]
    return I.build(files + [(f"{author}/g.jsonl", lines)], owner=ledger.owner), ids


def test_pane_one_is_exactly_the_questions_and_tensions_the_viewer_owns():
    # a supersede refused by authority leaves BOTH entries in force: a ledger semantic (`team verify` reports it),
    # not something the view narrates. So there is no "proposed replacement" half to this pane, and no such field.
    ledger, ents = _build([("ana", "decision", 9, dict(kind="ruling", owner="ana", paths=["a.py"], words="Original."))])
    rid = ents[0]["id"]
    led, _ = _plus(ledger, "ben", [("decision", dict(kind="ruling", owner="ana", paths=["a.py"],
                                                      words="Banker's rounding.", supersedes=[rid]), None),
                                   ("question", dict(owner="ana", paths=["a.py"], summary="why?"), None)])
    m = V.build_model(TEAM, led, "ana", None, "x", now=NOW)
    assert "awaiting_words" not in m
    assert [c["type"] for c in m["waiting"]] == ["question"]
    page = V.render_html(m)
    pane1 = page.split('id="pane1"')[1].split('id="pane2"')[0]
    assert "proposed replacements" not in pane1 and "proposes to replace" not in pane1


def test_a_busy_ledger_answers_503_instead_of_hanging():
    httpd = V.make_view_server(_Stub(), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        httpd.model_lock.acquire()          # a cold history read is holding the lock
        r, body = _req(httpd.server_address[1], "GET")
        assert r.status == 503 and b"busy" in body
        assert _req(httpd.server_address[1], "GET", "/team_view.css")[0].status == 200
    finally:
        httpd.model_lock.release()
        httpd.shutdown()
        httpd.server_close()


def test_the_503_diagnostic_is_best_effort_and_one_line(capfd, monkeypatch):
    class Boom(_Stub):
        def snapshot(self):
            if getattr(self, "armed", False):
                raise RuntimeError("fatal: first line\n[levain] FORGED log line\nthird")
            return super().snapshot()
    stub = Boom()
    httpd = V.make_view_server(stub, port=0)
    stub.armed = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r, body = _req(httpd.server_address[1], "GET")
        assert r.status == 503
        err = capfd.readouterr().err
        assert "\n[levain] FORGED" not in err and "FORGED log line" in err    # one line, newlines escaped
        # a closed stderr must not stop the 503 from being sent
        import sys

        class Dead:
            def write(self, *a):
                raise ValueError("I/O operation on closed file")
            flush = write
        monkeypatch.setattr(sys, "stderr", Dead())
        assert _req(httpd.server_address[1], "GET")[0].status == 503
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_port_zero_is_accepted_and_help_agrees():
    from levain.team.cli import _port
    assert _port("0") == 0 and _port("65535") == 65535
    h = V.make_view_server(_Stub(), port=0)          # 0 = ephemeral
    assert h.server_address[1] > 0
    h.server_close()
    import argparse
    from levain.team import cli
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="c")
    cli.register(sub)
    helptext = [a for a in sub.choices["team"]._subparsers._group_actions[0].choices["view"]._actions
                if "--port" in a.option_strings][0].help
    assert "0..65535" in helptext and "ephemeral" in helptext


def test_a_held_entry_appears_once_with_both_reasons_and_paths_are_deduped():
    old = 90 * 24
    ledger, ents = _build([
        ("ana", "decision", old, dict(kind="ruling", owner="carl", paths=["x/**"], words="Carl's.", recheck="grep x",
                                       force_paths=["x/**", "x/**"])),
    ])
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    assert len(m["held"]) == 1 and m["held_count"] == 1
    why = m["held"][0]["why"]
    assert "carl is not on team.toml" in why and "recheck is" in why
    assert [(g["path"], len(g["entries"])) for g in m["in_force"]] == [("x/**", 1)]
    assert m["in_force"][0]["entries"][0]["paths"] == ["x/**"]
    assert m["in_force_count"] == 1


def test_a_request_during_a_cold_read_is_answered_503_at_once_and_the_page_asks_again():
    # codex 10-07: a 10 s timed wait parked one handler thread per request behind a cold history read. Nobody waits
    # now: the answer is immediate, says when to retry, and a browser on the page retries by itself.
    import time
    httpd = V.make_view_server(_Stub(), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        httpd.model_lock.acquire()          # a cold history read is holding the lock
        t0 = time.monotonic()
        r, body = _req(port, "GET")
        assert time.monotonic() - t0 < 1.0
        assert r.status == 503 and r.getheader("Retry-After") == str(V.BUSY_RETRY)
        assert r.getheader("Content-Type").startswith("text/html")
        assert f'http-equiv="refresh" content="{V.BUSY_RETRY}"'.encode() in body and b"<script" not in body
        assert "frame-ancestors" in (r.getheader("Content-Security-Policy") or "")
        r, body = _req(port, "GET", "/view.json")
        assert r.status == 503 and r.getheader("Retry-After") and b"busy" in body and b"<html" not in body
    finally:
        httpd.model_lock.release()
        httpd.shutdown()
        httpd.server_close()


def test_a_path_filter_never_matches_the_project_wide_label():
    # codex + glm 10-07: pane 2 tested the "(project-wide)" label against the filter, so ?path=project showed a
    # project-wide ruling's acks while every other pane dropped the ruling.
    chains: dict = {}
    ts = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ruling = _seal(chains, "ana", ts, "decision", kind="ruling", owner="ana", words="Everywhere: no floats.")
    ack = _seal(chains, "ben", ts, "ack", refs=[ruling["id"]], session="s1")
    ledger = I.build([("ana/a.jsonl", [json.dumps(ruling, sort_keys=True)]),
                      ("ben/b.jsonl", [json.dumps(ack, sort_keys=True)])], owner="ana")
    for pf in ("project", "project-wide", "(project-wide)", "wide"):
        m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW, path_filter=pf)
        assert m["stopped"] == [] and m["ack_total"] == 0 and m["in_force"] == [], pf
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    assert [(r["path"], r["acks"]) for r in m["stopped"]] == [("(project-wide)", 1)]   # unfiltered, it still shows


def _git(*args, cwd):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _two_clones(tmp_path):
    """Two real clones of one team ledger: ana's (where the view runs) and ben's (who pushes)."""
    from levain.cli import main as levain_main
    _git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    clones = {}
    for who in ("ana", "ben"):
        _git("clone", "-q", str(tmp_path / "origin.git"), who, cwd=tmp_path)
        _git("config", "user.email", f"{who}@ex.com", cwd=tmp_path / who)
        _git("config", "user.name", who, cwd=tmp_path / who)
        clones[who] = tmp_path / who
    (clones["ana"] / "a.txt").write_text("x\n")
    _git("add", ".", cwd=clones["ana"])
    _git("commit", "-qm", "init", cwd=clones["ana"])
    _git("push", "-q", "origin", "HEAD:main", cwd=clones["ana"])
    assert levain_main(["team", "init", "--project", "ledgerline", "--owner", "ana", "--member", "ana=ana@ex.com",
                        "--member", "ben=ben@ex.com", "--repo", str(clones["ana"]), "--no-install"]) == 0
    assert levain_main(["team", "join", "--repo", str(clones["ben"]), "--no-install"]) == 0
    return clones["ana"], clones["ben"]


@pytest.fixture
def two_clone_view(tmp_path, monkeypatch):
    from levain.team.transport import GitLedger, Repo
    monkeypatch.setattr(V, "FETCH_FLOOR", 0.0, raising=False)   # the button's pace floor, so the test need not wait it out
    ana, ben = _two_clones(tmp_path)
    httpd = V.make_view_server(GitLedger(Repo.discover(ana)), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ana, ben, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _words(port, path):
    m = json.loads(_req(port, "GET", path)[1])
    return m, [e["words"] for g in m["in_force"] for e in g["entries"]]


def _local_tip(clone):
    return _git("rev-parse", "refs/heads/levain-ledger", cwd=clone).strip()


def test_the_fetch_button_brings_in_a_ruling_another_clone_pushed_without_moving_this_clone(two_clone_view):
    # codex 10-07: the server never fetched, so the "sync" button reloaded an unchanged local ref. L1/L2 10-07 + the
    # head's ruling: the fix may only FETCH. The panes come from the fetched remote-tracking ref; this clone's branch
    # is never rebased or moved by a GET, and how it differs is shown instead.
    from levain.cli import main as levain_main
    ana, ben, port = two_clone_view
    before = _local_tip(ana)
    assert levain_main(["team", "record", "decision", "--kind", "ruling", "--owner", "ben", "--paths", "tax/**",
                        "--words", "VAT rounds half-even.", "--repo", str(ben)]) == 0
    m, words = _words(port, "/view.json")
    assert words == []                     # within team.toml's fetch_interval of the last fetch: not fetched yet
    m, words = _words(port, "/view.json?fetch=1")
    assert words == ["VAT rounds half-even."]
    assert m["fetch"]["source"] == "remote" and m["fetch"]["error"] == "" and m["fetch"]["last_ok"]
    assert (m["fetch"]["unpushed"], m["fetch"]["unfetched"]) == (0, 1)
    assert _local_tip(ana) == before                                   # the GET moved nothing of ana's
    page = _req(port, "GET", "/")[1].decode()
    assert "fetch now" in page and f"remote, fetched {m['fetch']['last_ok']}" in page
    assert "1 from the remote not yet in it" in page and "fetch-error" not in page


def test_an_unpushed_local_entry_is_counted_not_hidden_or_pushed(two_clone_view, tmp_path):
    from levain.cli import main as levain_main
    ana, _ben, port = two_clone_view
    assert levain_main(["team", "record", "finding", "--summary", "local only", "--paths", "x.py", "--no-push",
                        "--repo", str(ana)]) == 0
    tip = _local_tip(ana)
    m, words = _words(port, "/view.json?fetch=1")
    assert m["fetch"]["source"] == "remote" and m["fetch"]["unpushed"] == 1
    assert _local_tip(ana) == tip
    remote_tip = _git("rev-parse", "refs/heads/levain-ledger", cwd=tmp_path / "origin.git").strip()
    assert remote_tip != tip                                            # and nothing was pushed
    assert "1 commit not pushed" in _req(port, "GET", "/")[1].decode()


def test_a_failed_fetch_is_shown_on_the_page_without_git_detail(two_clone_view, tmp_path, capfd):
    ana, _ben, port = two_clone_view
    gone = tmp_path / "gone-remote.git"
    _git("remote", "set-url", "origin", str(gone), cwd=ana)
    m, _ = _words(port, "/view.json?fetch=1")
    assert m["fetch"]["error"].startswith("the last fetch from the remote failed")
    page = _req(port, "GET", "/")[1].decode()
    assert 'class="warn fetch-error">⚠ the last fetch from the remote failed' in page
    assert "gone-remote" not in page and "gone-remote" not in json.dumps(m)
    assert "git fetch failed" in capfd.readouterr().err                # the detail is in the terminal


def test_with_no_remote_the_button_only_redraws_and_says_so(server):
    page = _req(server, "GET")[1].decode()
    assert "this clone only: no remote" in page and 'data-fetch="0">⟳ reload<' in page and "fetch now" not in page


def test_the_integrity_warning_counts_what_team_verify_counts(two_clone_view):
    # codex 10-07: the view counted only what the ledger build found, so a hash-valid ack naming an id the ledger does
    # not hold showed no warning while `levain team verify` failed. Both now read verify.problems. The write path
    # refuses such an ack, so it is planted the way a buggy or hostile clone would: a sealed line, committed, pushed.
    import io
    import contextlib
    from levain.team import cli as team_cli
    from levain.team import entry as E
    from levain.team import verify as VF
    from levain.team.transport import GitLedger, Repo
    ana, ben, port = two_clone_view
    gl = GitLedger(Repo.discover(ben))
    f = gl.file_for("ben")
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = f.read_text().splitlines() if f.exists() else []
    sealed = E.seal(E.build("ben", "ack", refs=["d-000000000000"], session="planted"),
                    json.loads(lines[-1])["hash"] if lines else "")
    with f.open("a") as fh:
        fh.write(json.dumps(sealed, sort_keys=True) + "\n")
    toml = gl.wt / "team.toml"                                         # and a team.toml change by a non-owner,
    toml.write_text(toml.read_text().replace('mode = "ask-once"', 'mode = "surface"'))   # which only history shows
    _git("add", "-A", cwd=gl.wt)
    _git("commit", "-qm", "planted", cwd=gl.wt)
    _git("push", "-q", "origin", "levain-ledger", cwd=gl.wt)
    m, _ = _words(port, "/view.json?fetch=1")
    assert m["fetch"]["source"] == "remote" and m["fetch"]["error"] == ""
    ga = GitLedger(Repo.discover(ana))
    rsha = ga.remote_ref()
    want = VF.problems(ga, rsha, ga.team(rsha), ga.judge_remote(rsha).ledger)
    assert any("d-000000000000" in p for p in want) and any("not the owner" in p for p in want)
    assert m["problems"] == len(want) >= 1
    assert f"{len(want)} integrity problem(s): run levain team verify".encode() in _req(port, "GET")[1]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert team_cli.cmd_verify(type("A", (), {"repo": str(ben)})()) == 1   # verify fails on the same ledger
    assert "d-000000000000" in buf.getvalue()


class _RemoteStub(_Stub):
    """A clone with a remote whose fetch is recorded, not run; the panes come from the local snapshot because this
    stub has no accepted remote tip."""
    remote = "origin"

    def __init__(self, state=None, note=None):
        super().__init__()
        self.calls: list[float] = []
        self._state = state if state is not None else {"last_fetch_ok": 1.0e9}
        self._note = note
        self.repo = type("Repo", (), {"toplevel": "/nonexistent"})()

    def fetch_only(self, *, interval, timeout):
        self.calls.append(interval)
        return self._note

    def remote_ref(self):
        return None                     # no accepted remote tip: the panes come from this clone's copy

    def state(self):
        if isinstance(self._state, Exception):
            raise self._state
        return self._state


def _serve(stub):
    httpd = V.make_view_server(_Stub(), port=0)
    httpd.ledger_reader = stub
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_no_request_fetches_more_often_than_the_floor_even_when_team_toml_says_always(monkeypatch):
    # L1/L2 10-07: min(fetch_interval, floor) let fetch_interval = 0 fetch on every request, the 2 s busy retries
    # included, and the button's "at most every 10 s" was false.
    stub = _RemoteStub()
    httpd = _serve(stub)
    httpd.fetch_interval = 0.0
    try:
        port = httpd.server_address[1]
        _req(port, "GET", "/view.json")
        _req(port, "GET", "/view.json?fetch=1")
        httpd.fetch_interval = 600.0
        _req(port, "GET", "/view.json")
        _req(port, "GET", "/view.json?fetch=1")
        assert stub.calls == [V.FETCH_FLOOR, V.FETCH_FLOOR, 600.0, V.FETCH_FLOOR]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("state", [OSError("read-only .git"), {"last_fetch_ok": float("inf")},
                                   {"last_fetch_ok": 1e20}, {"last_fetch_ok": "x"}])
def test_a_broken_fetch_record_is_shown_and_the_panes_still_draw(monkeypatch, state, capfd):
    # L1 10-07: an unguarded fetch path turned an unreadable state.json (or one holding Infinity) into a whole-page
    # 503 "ledger unavailable" while the ledger itself was fine.
    httpd = _serve(_RemoteStub(state=state))
    try:
        r, body = _req(httpd.server_address[1], "GET")
        assert r.status == 200 and b"Waiting on you" in body
        if isinstance(state, Exception):
            assert b"could not fetch from the remote" in body and "read-only .git" in capfd.readouterr().err
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_failed_fetch_shows_a_fixed_message_and_keeps_git_detail_in_the_terminal(monkeypatch, capfd):
    # L1 10-07: git's stderr reached the page and /view.json; it can carry a remote URL with credentials.
    secret = "fatal: unable to access 'https://ana:hunter2@git.example/x.git/': /home/ana/.netrc"
    httpd = _serve(_RemoteStub(note=secret))
    try:
        port = httpd.server_address[1]
        page = _req(port, "GET", "/?fetch=1")[1].decode()
        js = _req(port, "GET", "/view.json")[1].decode()
        for out in (page, js):
            assert "hunter2" not in out and "netrc" not in out and "git.example" not in out
        assert "the last fetch from the remote failed" in page and "hunter2" in capfd.readouterr().err
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_transport_warnings_are_shown_once_and_taken_off_the_process_list():
    # L1/L2 10-07: the view never showed transport.WARNINGS, and in a long-lived view the list only grew.
    class Warns(_Stub):
        def snapshot(self):
            self.warnings.append("team.toml at the tip is unusable; using an older one")
            self.warnings.append("team.toml at the tip is unusable; using an older one")
            return super().snapshot()
    stub = Warns()
    httpd = _serve(stub)
    try:
        port = httpd.server_address[1]
        for _ in range(3):
            page = _req(port, "GET")[1].decode()
            assert page.count("team.toml at the tip is unusable") == 1
        assert stub.warnings == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_connections_past_the_worker_bound_are_closed_without_a_thread():
    # codex 10-07 + L2: ThreadingHTTPServer starts a thread per connection; an idle keep-alive connection keeps its
    # thread for the 30 s socket timeout, so threads grew with connections. They are bounded now.
    import socket
    httpd = V.make_view_server(_Stub(), port=0)
    httpd.workers = threading.BoundedSemaphore(1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    idle = socket.create_connection(("127.0.0.1", port))          # holds the one slot, sends nothing
    try:
        import time
        time.sleep(0.2)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra.sendall(b"GET /team_view.css HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            got = extra.recv(100)
        except ConnectionResetError:                               # macOS reports the unanswered close as a reset
            got = b""
        assert got == b""                                          # closed unanswered: no slot, no thread
        extra.close()
        idle.close()
        for _ in range(50):                                        # the slot comes back when its thread ends
            try:
                if _req(port, "GET", "/team_view.css")[0].status == 200:
                    break
            except (ConnectionError, OSError):
                time.sleep(0.1)
        else:
            raise AssertionError("the slot never came back")
    finally:
        idle.close()
        httpd.shutdown()
        httpd.server_close()
