from __future__ import annotations

import json
import os
from pathlib import Path

from levain.slot.runtime_helper import read_passthrough_total as rd
from tests.test_broker_front_passthrough_limits import _records, _run, _settled, policy, client_hello


def _w(p: Path, *lines: str) -> Path:
    p.write_text("".join(ln + "\n" for ln in lines))
    return p


def _t(total, **kw) -> str:
    return json.dumps({"event": "tunnel", "passthrough_total": total, **kw})


def test_last_tunnel_record_wins(tmp_path):
    assert rd(_w(tmp_path / "f", _t(10), '{"event": "other", "passthrough_total": 99}', _t(30), _t(20))) == 20


def test_torn_and_foreign_lines_are_skipped(tmp_path):
    p = tmp_path / "f"
    p.write_bytes((_t(7) + "\n" + "garbage\n" + '[1,2]\n' + "\xff\xfe\n".encode("latin-1").decode("latin-1")).encode("latin-1")
                  + b'{"event": "tunnel", "passthrough_to')
    assert rd(p) == 7


def test_no_tunnel_record_is_none(tmp_path):
    assert rd(_w(tmp_path / "f", '{"event": "passthrough-bytes-exhausted", "total": 5}')) is None
    assert rd(_w(tmp_path / "e")) is None


def test_missing_or_unreadable_is_none(tmp_path):
    assert rd(tmp_path / "nope") is None
    assert rd(tmp_path) is None
    assert rd(None) is None


def test_bad_values_are_refused(tmp_path):
    for bad in (True, -1, "5", 1.5, None):
        assert rd(_w(tmp_path / "f", _t(bad))) is None
    assert rd(_w(tmp_path / "g", _t(4), _t(True), _t(-3), _t("9"))) == 4
    assert rd(_w(tmp_path / "h", _t(0))) == 0


def test_reads_a_real_front_log():
    import asyncio

    async def body(open_tunnel, front, log: Path) -> None:
        assert rd(log) is None
        status, _, r, w = await open_tunnel()
        assert status == 200
        n = len(client_hello("api.anthropic.com"))
        w.write(b"x" * 5000)
        await w.drain()
        got = b""
        while len(got) < n + 5000:
            got += await r.read(65536)
        w.close()
        await _settled(front)
        assert rd(log) == front.pt_total == 2 * (n + 5000)
        assert rd(log) == [x for x in _records(log) if x.get("event") == "tunnel"][-1]["passthrough_total"]

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9), body))
