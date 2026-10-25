"""ROUND 4 probe 6 (R15-17-8): no query value (and no long path segment) in any file the glue or decider writes.

Recorded (ROUND4.md probe 6): requests `?token=SECRET123TOK` (card), a signed-URL `...&sig=SECRET123SAS` (allowed), an AWS
presign `X-Amz-Credential=SECRET123AKID&X-Amz-Signature=SECRET123AMZ` (card), paths `/dl/SECRET123SHORT/f` and
`/dl/SECRET123LONGaaaa.../f` (42-char segment), and an ERROR path (an allowed request to a name that does not resolve, query
`sig=SECRET123ERR`, srt 502). Lines containing SECRET123 per file: BEFORE (run F, round-3 logging) events 6, frames 6,
obs 1, bodies 0, proxy.stderr 0; AFTER (run E, R4-c) events 1, frames 1, obs 1 (only `/dl/SECRET123SHORT/f`, a 14-character
plain segment), bodies 0, proxy.stderr 0. The 42-character segment is masked (`<seg:42>`).
Port note: the error path used `nx-p6.invalid` (a lookup that leaves the machine); here it is `nx-p6.rb.test`, a name the
local responder answers with no data, so srt fails the same way (502) without leaving 127.0.0.1."""

from __future__ import annotations

import pytest

from ..fixtures import clients

pytestmark = pytest.mark.chat_egress_srt
LONG = "SECRET123LONG" + "a" * 29       # 42 characters


def _drive(r4, g):
    p, c = r4.ports, lambda url: clients.curl(g.port, g.ca, url, include=False, max_time=20)
    res = [c(f"http://s.rb.test:{p.http}/p6a?token=SECRET123TOK"),
           c(f"https://s.rb.test:{p.tls}/p6b?sv=2024-08-04&sp=r&se=2026-10-25&sig=SECRET123SAS"),
           c(f"https://s.rb.test:{p.tls}/p6c?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=SECRET123AKID&X-Amz-Signature=SECRET123AMZ"),
           c(f"https://s.rb.test:{p.tls}/dl/SECRET123SHORT/f"),
           c(f"https://s.rb.test:{p.tls}/dl/{LONG}/f"),
           c(f"https://nx-p6.rb.test:{p.tls}/p6e?sig=SECRET123ERR")]
    import time
    time.sleep(1.5)
    return res


def _grep(g) -> dict[str, int]:
    out = {}
    for name in ("events.jsonl", "frames.jsonl", "obs.jsonl", "bodies.jsonl", "proxy.stderr"):
        f = g.dir / name
        out[name.split(".")[0]] = sum("SECRET123" in ln for ln in f.read_text(errors="replace").splitlines()) if f.exists() else 0
    return out


def test_p6_before_round3_logging_leaks_every_query_NEGATIVE_CONTROL(r4, r4_repos, verdict):
    g = r4.start_glue("F", log="raw", scheme_check="off")
    try:
        res = _drive(r4, g)
        counts = _grep(g)
        verdict("R4 P6 BEFORE (run F, round-3 logging)", "events 6, frames 6, obs 1, bodies 0, proxy.stderr 0",
                f"events {counts['events']}, frames {counts['frames']}, obs {counts['obs']}, bodies {counts['bodies']}, proxy.stderr {counts['proxy']}")
        assert counts == {"events": 6, "frames": 6, "obs": 1, "bodies": 0, "proxy": 0}, counts
    finally:
        g.stop()


def test_p6_after_no_query_value_in_any_file_error_path_included(r4, r4_repos, verdict):
    g = r4.start_glue("E-p6", log="safe", scheme_check="on")
    try:
        res = _drive(r4, g)
        counts = _grep(g)
        hits = [ln for f in ("events.jsonl", "frames.jsonl", "obs.jsonl") for ln in (g.dir / f).read_text().splitlines() if "SECRET123" in ln]
        statuses = [r.status for r in res]
        verdict("R4 P6 AFTER (run E, R4-c)", "events 1, frames 1, obs 1 (only /dl/SECRET123SHORT/f), bodies 0, proxy.stderr 0; 42-char segment masked <seg:42>; error path 502",
                f"events {counts['events']}, frames {counts['frames']}, obs {counts['obs']}, bodies {counts['bodies']}, proxy.stderr {counts['proxy']}; "
                f"statuses={statuses}; masked={'<seg:42>' in ' '.join(ln for f in ('events.jsonl','frames.jsonl','obs.jsonl') for ln in (g.dir / f).read_text().splitlines())}")
        assert counts == {"events": 1, "frames": 1, "obs": 1, "bodies": 0, "proxy": 0}, counts
        assert all("/dl/SECRET123SHORT/f" in ln for ln in hits)
        assert all(v not in " ".join(hits) for v in ("TOK", "SAS", "AKID", "AMZ", "ERR", "LONG"))
        assert statuses[0] == 403 and statuses[2] == 403 and statuses[5] == 502          # cards and the srt error path
        assert "<seg:42>" in " ".join((g.dir / "events.jsonl").read_text().splitlines())
    finally:
        g.stop()
