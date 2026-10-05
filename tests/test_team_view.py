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
    def snapshot(self):
        ledger, _ = _ledger()
        return "sha", TEAM, ledger

    def handle(self, team):
        return "ana"

    def read_canon(self, sha):
        return None

    def state_hash(self, ledger, team):
        return "x"


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
        assert _req(port, "GET", "/team_view.css")[0].status == 200   # an asset is not queued behind the ledger read
        stub.release.set()
        for t in ts:
            t.join(10)
        assert results == [200, 200]
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
    httpd.model_lock_timeout = 0.3
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
