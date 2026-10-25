"""Every fixture starts something and stops it: no listener, no process left (an `lsof` check beside socket probes).

The base pieces need nothing external; the glue test needs node + srt 0.0.81 (`chat_egress_srt`)."""

from __future__ import annotations

import pytest

from .fixtures import clients
from .fixtures.procs import lsof_listeners, tcp_listening, udp_bound, pid_alive
from .fixtures.repos import build_bare, write_streams
from .fixtures.stack import Rig


def test_base_fixtures_start_and_stop_clean(tmp_path):
    rig = Rig(tmp_path / "rig")
    try:
        rig.start_pki()
        rig.start_dns()
        rig.start_origin("bytes")
        raw = rig.start_raw_listener()
        # while up: every piece answers
        for p in (rig.ports.http, rig.ports.tls, rig.ports.off_http, rig.ports.off_tls, raw):
            assert tcp_listening(p), f"{p} should be listening"
        assert udp_bound(rig.ports.dns)
        up = lsof_listeners(rig.ports.tcp(), [rig.ports.dns])
        if up is not None:
            assert len(up) >= 6, up      # 4 origin ports + raw + dns, python processes
        pids = [m.pgid for m in rig.managed]
    finally:
        rig.stop()
    assert rig.leftovers() == []
    assert not any(pid_alive(p) for p in pids)
    # and the lsof view agrees (None = lsof absent; the socket probes above already said so)
    assert lsof_listeners(rig.ports.tcp(), [rig.ports.dns]) in (None, [])


def test_stop_is_idempotent_and_survives_a_half_started_rig(tmp_path):
    rig = Rig(tmp_path / "rig")
    rig.stop()                       # nothing started
    rig.start_pki()
    rig.start_dns()
    rig.stop()
    rig.stop()
    assert rig.leftovers() == []


def test_git_origin_serves_an_advertisement_and_stops_clean(tmp_path):
    rig = Rig(tmp_path / "rig")
    try:
        rig.start_pki()
        rig.start_dns()
        rig.start_origin("git")
        fi = tmp_path / "fi"
        write_streams(fi)
        build_bare(rig.repo_root, "declared", fi / "small.fi", tmp_path / "home")
        r = clients.curl(0, rig.pki / "origin-ca.pem", f"https://s.rb.test:{rig.ports.tls}/declared.git/info/refs?service=git-upload-pack",
                         "--resolve", f"s.rb.test:{rig.ports.tls}:127.0.0.1", "-H", "Git-Protocol: version=2", proxy=False)
        assert r.status == 200 and "application/x-git-upload-pack-advertisement" in r.out.lower(), r.out[:400]
    finally:
        rig.stop()
    assert rig.leftovers() == []


@pytest.mark.chat_egress_srt
def test_glue_starts_serves_and_stops_clean(tmp_path):
    rig = Rig(tmp_path / "rig")
    try:
        rig.start_pki()
        rig.start_dns()
        rig.start_origin("bytes")
        g = rig.start_glue("teardown")
        assert tcp_listening(g.port) and pid_alive(g.node_pid)
        m = rig.mark(g)
        r = clients.curl(g.port, g.ca, f"https://s.rb.test:{rig.ports.tls}/ctl?allow=1")
        seen = rig.since(m, g)
        assert r.status == 200 and "origin-ok" in r.out, (r.out, r.err)
        assert any(e["t"] == "onResponse" for e in seen.events)
        pids = list(g.proc.group_pids())
        assert g.node_pid in pids
        port, node_pid = g.port, g.node_pid
    finally:
        rig.stop()
    assert not tcp_listening(port) and not pid_alive(node_pid)
    assert rig.leftovers() == []
