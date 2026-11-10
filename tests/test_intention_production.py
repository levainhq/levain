"""Intention controls on the PRODUCTION wiring: the server ``make_server`` builds (its own QuestionStore over the consent
directory, its own cockpit, its own verb route), not the test Rig. A native agenda fires into a band row; the List's band
row is tapped over HTTP. Temp dirs only."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from levain import germinate as g
from levain.consent.questions import QuestionStore
from levain.spores import guarded_spore_store
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)


def _get(base: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, headers={"X-Levain-Write-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Levain-Write-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_a_list_intention_row_acts_on_the_served_cockpit(tmp_path: Path, qdir) -> None:  # noqa: F811
    root, src, _ids = _install(tmp_path)
    spores = root / ".levain" / "memory.spores.json"
    today = date.today()
    sid = guarded_spore_store(spores).add(type="task", text="renew the thing", disposition="agenda",
                                          next=(today - timedelta(days=1)).isoformat())["id"]
    res = g.germinate(spores, qdir, install=root, today=today)
    assert not res["errors"], res
    (q,) = QuestionStore(qdir).open_questions()
    with _serve(src, write_token=TOKEN) as (base, _httpd):
        status, panel = _get(base, "/cockpit/panel/tray.json")
        assert status == 200, panel
        row = next(r for r in panel["rows"] if r["id"] == f"band:{q.id}")
        assert row["facets"]["intention"] == sid
        assert {a["verb"] for a in row["actions"]} >= {"intention_done", "intention_clear"}
        status, out = _post(base, "/cockpit/verb", {"verb": "intention_done", "panel_id": "tray", "row_id": row["id"],
                                                     "row_version": row["version"], "params": {}})
    assert status == 200, out
    assert guarded_spore_store(spores).get(sid)["status"] != "open"
    assert QuestionStore(qdir).get(q.id).answer is not None
