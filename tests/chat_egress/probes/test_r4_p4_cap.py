"""ROUND 4 probe 4 (R15-17-2): the spooled-body cap. Run E: cap 2,097,152.

Recorded (ROUND4.md probe 4): (a) advertisement GET, then a CHUNKED 5 MiB upload-pack POST: srt reads to the cap
(`cut=True`, 2,097,152 B), the decider cards it, POST upstream 0 B and no DNS lookup for it; (b) same with Content-Length
5 MiB; (c) 1,900,000 decoded bytes as ONE-BYTE CHUNKS (11,400,005 B on the wire) are ALLOWED (`cut` false) and the origin
got 11,400,005 wire bytes in 1,900,000 chunks: **FAIL on "WIRE bytes"**, srt's `need_body` counts DECODED bytes; (d) a
600,000 B body with the decider holding its ALLOW 3 s after the body: nothing upstream before the verdict; (e) chunk
EXTENSIONS are not forwarded (origin body section 15 B, 0 EXTSECRET bytes)."""

from __future__ import annotations

import time

import pytest

from ..fixtures import clients
from ._git import ADV_Q, GH, origin_reqs
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt
CAP = 2097152
BODY_CARD = "card: upload-pack body over the cap"
UP = "application/x-git-upload-pack-request"


@pytest.fixture(scope="module")
def adv_url(r4):
    return f"https://git.rb.test:{r4.ports.tls}/declared.git/{ADV_Q}"


def _adv(r4, g, adv_url):
    res = clients.curl(g.port, g.ca, adv_url, "-o", "/dev/null", include=False)
    assert res.status == 200, res
    return res


@pytest.fixture(scope="module")
def blobs(tmp_path_factory):
    d = tmp_path_factory.mktemp("blobs")
    (d / "five.bin").write_bytes(bytes(5 * 1024 * 1024))
    (d / "p4d.bin").write_bytes(bytes(600000))
    return d


@pytest.mark.parametrize("pid,extra", [("R4 P4.a chunked 5 MiB", ["-H", "Transfer-Encoding: chunked"]), ("R4 P4.b Content-Length 5 MiB", [])])
def test_p4_ab_body_over_the_cap_is_cut_and_carded(r4, r4_glue, blobs, adv_url, verdict, pid, extra):
    m = r4.mark(r4_glue)
    _adv(r4, r4_glue, adv_url)
    res = clients.curl(r4_glue.port, r4_glue.ca, f"https://git.rb.test:{r4.ports.tls}/declared.git/git-upload-pack", "-X", "POST", *GH, *extra,
                       "--data-binary", f"@{blobs / 'five.bin'}")
    seen = r4.since(m, r4_glue, settle=1.5)
    b = seen.bodies[-1]
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    a_q = [d for d in seen.dns if d["type"] == "A" and d["name"] == "git.rb.test"]
    verdict(pid, f"PASS: srt read to the cap (cut=True, {CAP} B), decider card `upload-pack body over the cap`; POST 0 B upstream, no DNS lookup for it",
            f"{client(res)} card={BODY_CARD in res.out} body received={b['received']} cut={b['cut']} origin POSTs={len(posts)} A-queries={len(a_q)}")
    assert b["cut"] is True and b["received"] == CAP and BODY_CARD in res.out and res.status == 403
    assert posts == [] and len(a_q) == 1            # the only lookup is the advertisement GET's


@pytest.mark.chat_egress_slow
def test_p4_c_one_byte_chunks_are_counted_decoded_not_wire_FAILS_THE_RULING(r4, r4_glue, adv_url, verdict):
    """The recorded FAIL, reproduced: the cap counts decoded bytes, and the origin receives the client's chunk framing."""
    raw = clients.Raw(r4_glue.port, r4_glue.ca, timeout=120)
    _adv(r4, r4_glue, adv_url)
    m = r4.mark(r4_glue)
    t, _ = raw.tunnel("git.rb.test", r4.ports.tls)
    n = 1_900_000
    head = (f"POST /declared.git/git-upload-pack HTTP/1.1\r\nHost: git.rb.test:{r4.ports.tls}\r\nContent-Type: {UP}\r\n"
            "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode()
    body = b"1\r\nX\r\n" * n + b"0\r\n\r\n"
    t0 = time.time()
    t.sendall(head + body)
    reply = clients.recv_all(t)
    seen = r4.since(m, r4_glue, settle=2)
    b = seen.bodies[-1]
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    verdict("R4 P4.c one-byte chunks", "FAIL on WIRE bytes: srt delivered 1,900,000 B, cut false, ALLOWED; origin got 11,400,005 wire bytes in 1,900,000 chunks",
            f"reply={clients.status_line(reply)!r} in {time.time() - t0:.1f}s; body received={b['received']} cut={b['cut']}; origin POST wire={[d['body_wire_bytes'] for d in posts]} "
            f"chunks={[d['chunks'] for d in posts]} decoded={[d['body_decoded_len'] for d in posts]}")
    assert b["received"] == n and b["cut"] is False
    assert len(posts) == 1 and posts[0]["body_wire_bytes"] == len(body) == 11400005 and posts[0]["chunks"] == n


def test_p4_d_nothing_upstream_before_the_verdict(r4, r4_glue, blobs, adv_url, verdict):
    _adv(r4, r4_glue, adv_url)
    m = r4.mark(r4_glue)
    t0 = time.time()
    res = clients.curl(r4_glue.port, r4_glue.ca, f"https://git.rb.test:{r4.ports.tls}/declared.git/git-upload-pack?bodyhold=3", "-X", "POST", *GH,
                       "--data-binary", f"@{blobs / 'p4d.bin'}", include=False)
    seen = r4.since(m, r4_glue)
    allow = [f for f in seen.frames if f.get("t") == "verdict" and f["action"] == "allow"][-1]
    body_at = seen.bodies[-1]["at"]
    dns_at = [d["at"] for d in seen.dns][-1]
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    # frames carry their own wall time under the Link log's "at"; read it from the raw log
    allow_at = [d["at"] for d in r4_glue.frames.since(m.frames) if d["msg"].get("t") == "verdict" and d["msg"]["action"] == "allow"][-1]
    verdict("R4 P4.d spool before allow", "PASS: body at .859, allow +3.005 s later, srt's DNS lookup 2 ms after the allow; nothing before the allow; then 600,000 B",
            f"status={res.status} client {time.time() - t0:.1f}s; allow-body={allow_at - body_at:.2f}s dns-after-allow={dns_at - allow_at:+.3f}s origin POST body={[d['body_decoded_len'] for d in posts]}")
    assert res.status == 200 and 2.8 < allow_at - body_at < 4.5 and dns_at >= allow_at - 0.05
    assert [d["body_decoded_len"] for d in posts] == [600000] and posts[0]["at"] >= allow_at


def test_p4_e_chunk_extensions_are_not_forwarded(r4, r4_glue, adv_url, verdict):
    raw = clients.Raw(r4_glue.port, r4_glue.ca)
    _adv(r4, r4_glue, adv_url)
    m = r4.mark(r4_glue)
    t, _ = raw.tunnel("git.rb.test", r4.ports.tls)
    ext = b";x=" + b"EXTSECRET" * 44 + b"EXTS"
    reply = raw.exchange(t, (f"POST /declared.git/git-upload-pack HTTP/1.1\r\nHost: git.rb.test:{r4.ports.tls}\r\nContent-Type: {UP}\r\n"
                             "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode() + b"5" + ext + b"\r\nhello\r\n0\r\n\r\n")
    seen = r4.since(m, r4_glue)
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    verdict("R4 P4.e chunk extensions", "origin body section 15 B (`5\\r\\nhello\\r\\n0\\r\\n\\r\\n`); 0 EXTSECRET bytes",
            f"origin POST wire={[d['body_wire_bytes'] for d in posts]} EXTSECRET-in-origin-head={any('EXTSECRET' in d['raw_head'] for d in posts)}")
    assert len(posts) == 1 and posts[0]["body_wire_bytes"] == 15 and "EXTSECRET" not in posts[0]["raw_head"]
