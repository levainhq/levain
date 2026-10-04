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
    assert {c["owner"] for c in m["orphaned"]} == {"carl"}              # carl is not in team.toml
    assert [c["summary"] for c in m["overdue"]] == ["no callers"]       # 60 days > 30
    assert {g["path"] for g in m["in_force"]} == {"billing.py", "tax/**", "infra/**", "legacy/**"}
    assert m["you"] == "ana"
    mb = V.build_model(TEAM, ledger, "ben", None, "x", now=NOW)
    assert [c["type"] for c in mb["waiting"]] == ["tension"]


def test_no_count_is_keyed_by_a_person():
    ledger, _ = _ledger()
    m = V.build_model(TEAM, ledger, "ana", None, "x", now=NOW)
    blob = json.dumps(m["stopped"])
    assert "ben" not in blob and "author" not in blob and "session" not in blob
    page = V.render_html(m)
    # the acker never appears in pane 2
    pane2 = page.split('id="pane2"')[1].split('id="pane3"')[0]
    assert "ben" not in pane2


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
    assert _req(server, "GET", "/view.css")[0].status == 200
    assert json.loads(_req(server, "GET", "/view.json")[1])["project"] == "ledgerline"
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "FOO"):
        r, _ = _req(server, method)
        assert r.status == 405, method
        assert r.getheader("Allow") == "GET"
        assert "frame-ancestors" in (r.getheader("Content-Security-Policy") or "")


def test_page_has_no_form_and_no_external_request(server):
    _, body = _req(server, "GET")
    text = body.decode()
    assert "<form" not in text and "<input" not in text and "<button" not in text
    assert "http://" not in text.replace("http-equiv", "") and "https://" not in text


def test_host_guard_and_unknown_route(server):
    assert _req(server, "GET", headers={"Host": "evil.example"})[0].status == 403
    assert _req(server, "GET", "/nope")[0].status == 404


def test_refuses_a_non_loopback_bind():
    with pytest.raises(ValueError):
        V.make_view_server(_Stub(), host="0.0.0.0", port=0)
