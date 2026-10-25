"""ROUND 3 B12 (plain-HTTP keys and strip).

Recorded (ROUND3.md B12, patched column): a KEYED host over plain http: 200, and the origin got the request with host /
user-agent / accept / connection ONLY (no key, no client Authorization / Cookie / X-Api-Key / X_Api_Key); the strip applies
on plain HTTP to an unkeyed host too."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt

HDRS = ["-H", "Authorization: Bearer CLIENT-SECRET", "-H", "Cookie: c=CLIENT-COOKIE", "-H", "X-Api-Key: CLIENT-APIKEY",
        "-H", "X_Api_Key: CLIENT-APIKEY2"]


@pytest.mark.parametrize("pid,url_fmt,recorded", [
    ("R3 B12.1 keyed host on http", "http://api.rb.test:{p}/b12-keyed", "PASS (patched): 200; origin GET /b12-keyed with host/user-agent/accept/connection ONLY"),
    ("R3 B12.2 strip on http", "http://s.rb.test:{p}/b12-strip?allow=1", "PASS: 200; no Authorization/Cookie/X-Api-Key/X_Api_Key at the origin"),
])
def test_b12_no_key_and_no_client_credential_on_plain_http(r3, r3_glue, verdict, pid, url_fmt, recorded):
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, url_fmt.format(p=r3.ports.http), *HDRS)
    seen = r3.since(m, r3_glue)
    reqs = [d for d in seen.origin if d.get("bytes")]
    assert len(reqs) == 1
    head = reqs[0]["raw"].lower()
    names = sorted({ln.split(":")[0] for ln in head.replace("\\r\\n", "\n").split("\n")[1:] if ":" in ln})
    verdict(pid, recorded, f"status={res.status} origin header names={[n.strip(chr(39)) for n in names]}")
    assert res.status == 200 and "origin-ok" in res.out
    for needle in ("authorization", "cookie", "api-key", "api_key", "client-", "spike-key"):
        assert needle not in head, needle
    assert set(n.strip("'\\ ") for n in names) <= {"host", "user-agent", "accept", "connection"}
