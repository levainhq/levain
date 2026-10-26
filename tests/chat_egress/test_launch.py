"""CH-D2 S8: the chat launch's egress (`levain.chat_egress.launch`): start_egress / stop_egress per launch, the
listener pair, Node spawned with a closed env, the Node-major refusal, the session header and the egress part of the
containment digest, the SOCKS refuser, a real `launches` row, the test-seam refusal, and the clean end's order (the
lifeline closed first, so the glue writes the final flush, total 0 when no wall fired: P-9).

The srt-backed runs use the real glue (`levain/chat_egress/node/glue.mjs`) over srt 0.0.81 with a local recording
origin on 127.0.0.1 (plain HTTP; the glue's port stand-in and literal carve-out seams, and a resolver map seam, under
`LEVAIN_TEST_SEAMS=1`).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.chat_egress import classify, keys
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import Part

from .decider_rig import Desk, Live, Lookup, NoStore, make_launch
from .fixtures import env as envmod

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "spike_glue"))
import decider_spike4 as spike  # noqa: E402  (make_ca: an RSA CA srt's node-forge accepts)

NODE_VERSION = "v26.8.1"

#: An authority replay holding no grant: a launch whose routes are due nothing (`start_egress` requires one).
NO_GRANTS = SimpleNamespace(policy=SimpleNamespace(grants={}))


def real_launch(store: EnvelopeStore) -> str:
    """A launch the coordinator's own path wrote (lease, drop, thread, request, activate): its id and events real."""
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("chat", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "chat")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-chat-1")
    assert why is None and got.state == "active"
    return ln.launch_id


def _fake_node(tmp: Path, version: str) -> str:
    p = tmp / f"node-{version}"
    p.write_text(f"#!/bin/sh\necho {version}\n")
    p.chmod(0o755)
    return str(p)


# --- no srt needed -------------------------------------------------------------------------------------------------

def test_a_node_major_other_than_26_is_refused_naming_it(tmp_path) -> None:
    for v in ("v24.11.0", "v27.0.0", "v22.12.0"):
        with pytest.raises(lm.EgressRefused) as ei:
            lm.check_node(_fake_node(tmp_path, v))
        assert v in str(ei.value) and "26" in str(ei.value)
    assert lm.check_node(_fake_node(tmp_path, "v26.0.0")) == "v26.0.0"


def test_a_node_that_does_not_answer_a_version_is_refused(tmp_path) -> None:
    for out in ("", "node", "v26", "v26.1.0\nv24.0.0"):
        p = tmp_path / "n"
        p.write_text("#!/bin/sh\nprintf '%s' '" + out + "'\n")
        p.chmod(0o755)
        with pytest.raises(lm.EgressRefused):
            lm.check_node(str(p))
    with pytest.raises(lm.EgressRefused):
        lm.check_node(str(tmp_path / "absent"))


def test_the_session_header_is_r15_5s_words_plus_the_node_version() -> None:
    open_h = lm.session_header(open_network=True, node_version=NODE_VERSION)
    assert open_h == ("open network: network writes are not gated",
                      f"node {NODE_VERSION}",
                      "transport_confined: unmeasured",
                      f"known_credentials_scrubbed: v{lm.strip.CRED_STRIP_VERSION}",
                      "write_prompting: off")
    gated = lm.session_header(open_network=False, node_version=NODE_VERSION)
    assert gated == (f"node {NODE_VERSION}", "transport_confined: unmeasured",
                     f"known_credentials_scrubbed: v{lm.strip.CRED_STRIP_VERSION}", "write_prompting: http-shape")
    assert not any("measured" in line and "unmeasured" not in line for line in open_h + gated)


def test_transport_confined_reads_unmeasured_with_no_arm_15o_record() -> None:
    assert lm.TRANSPORT_CONFINED == "unmeasured"
    assert "transport_confined" not in lm.egress_containment(open_network=False)     # the arm's record, not the digest


def test_the_egress_digest_differs_between_open_network_and_gated() -> None:
    a, b = lm.egress_containment(open_network=True), lm.egress_containment(open_network=False)
    assert a["open_network"] is True and b["open_network"] is False
    assert a["egress"] == b["egress"] == "proxy"
    assert a["cred_strip"] == b["cred_strip"] == lm.strip.strip_policy()
    assert lm.egress_digest(open_network=True) != lm.egress_digest(open_network=False)
    assert lm.egress_digest(open_network=True) == lm.egress_digest(open_network=True)
    assert len(lm.egress_digest(open_network=False)) == 64


def test_test_seams_are_refused_unless_the_test_flag_is_set(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(lm.TEST_SEAMS_ENV, raising=False)
    seams = lm.TestSeams(glue_args=("--http-ports", "80,9999"))
    with pytest.raises(lm.EgressRefused) as ei:
        lm.refuse_seams(seams)
    assert "--http-ports" in str(ei.value)
    with pytest.raises(lm.EgressRefused):
        lm.refuse_seams(lm.TestSeams(lookup=lambda h, p: ["127.0.0.1"]))
    lm.refuse_seams(None)
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    lm.refuse_seams(seams)


def test_a_glue_seam_outside_the_known_list_is_refused_even_under_the_flag(monkeypatch) -> None:
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    with pytest.raises(lm.EgressRefused):
        lm.refuse_seams(lm.TestSeams(glue_args=("--events", "/tmp/x")))


def test_the_socks_listener_refuses_every_connect_with_0x02(tmp_path) -> None:
    from levain.chat_egress import socks
    r = socks.SocksRefuser()
    r.start()
    try:
        s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        s.sendall(b"\x05\x01\x00\x03\x0bexample.com\x01\xbb")
        rep = s.recv(10)
        assert rep[:2] == b"\x05\x02", rep
        assert s.recv(1) == b""                                     # then closed
        s.close()
        s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
        s.sendall(b"\x04\x01\x01\xbb\x7f\x00\x00\x01\x00")            # SOCKS4: closed, nothing answered
        assert s.recv(16) == b""
        s.close()
    finally:
        r.close()
    assert r.served == 2


# --- srt-backed: the real glue -------------------------------------------------------------------------------------

class Origin:
    def __init__(self) -> None:
        self.reqs: list[tuple[str, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.reqs.append((self.command, self.path))
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _start(tmp: Path, origin: Origin, monkeypatch, *, open_network: bool = False, launch_id: str | None = None,
           store: EnvelopeStore | None = None, node: str | None = None, mode=None, cards=None, events=None,
           srt_prefix: Path | None = None):
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    store = store or EnvelopeStore(tmp / "dispatch")
    lid = launch_id or real_launch(store)
    P = origin.port
    seams = lm.TestSeams(glue_args=("--http-ports", f"80,{P}", "--carve", f"127.0.0.1:{P}",
                                    "--resolve-map", "pub.s8.test=127.0.0.1"),
                         lookup=Lookup())
    key_pem, cert_pem = spike.make_ca()
    return lm.start_egress(store=store, launch_id=lid, node=node or envmod.node_bin(),
                           srt_prefix=srt_prefix or envmod.srt_prefix(),
                           events=events or tmp / "events.jsonl",
                           ca_pem=key_pem + cert_pem, routes=keys.RouteSet.of([]), liveness=Live(),
                           credentials=keys.LaunchCredentials(NoStore()), cards=cards or Desk(),
                           mode=mode or dm.Fixed(classify.OPEN_NETWORK if open_network else classify.GATED),
                           limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0),
                           authority=NO_GRANTS, seams=seams)


def _get(e, port: int, path: str) -> tuple[int, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", e.http_port, timeout=20)
    try:
        c.request("GET", f"http://pub.s8.test:{port}{path}", headers={"Host": f"pub.s8.test:{port}"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.fixture
def origin():
    o = Origin()
    yield o
    o.close()


@pytest.mark.chat_egress_srt
def test_run_one_launch_one_allowed_get_then_stop_writes_the_final_flush(tmp_path, origin, monkeypatch) -> None:
    e = _start(tmp_path, origin, monkeypatch)
    try:
        st, body = _get(e, origin.port, "/s8")
    finally:
        out = lm.stop_egress(e)
    row = e.store.launch(e.launch_id)
    events = (tmp_path / "events.jsonl").read_text()
    print(f"RUN S8: GET -> {st} {body!r}; origin saw {origin.reqs}")
    print(f"RUN S8: launches row: launch_id={row.launch_id} state={row.state} harness={row.harness}")
    print(f"RUN S8: header: {e.header}")
    print(f"RUN S8: stop: {out}")
    print(f"RUN S8: events file raw:\n{events}")
    assert (st, body) == (200, b"ok") and origin.reqs == [("GET", "/s8")]
    assert out.glue_exit == 0 and out.decider_clean and out.order == ("socks", "lifeline", "glue", "decider")
    last = json.loads(events.strip().splitlines()[-1])
    assert last["t"] == "summary" and last["final"] is True and last["total"] == 0
    assert out.final_flush == last
    assert dm.service_threads() == []


@pytest.mark.chat_egress_srt
def _env_dumping_node(tmp: Path, dump: Path) -> str:
    """A `node` that appends the env it was given to `dump`, then execs the real node with its argv."""
    w = tmp / "node-env-dump"
    w.write_text(f"#!/bin/sh\n/usr/bin/env >> '{dump}'\necho '--' >> '{dump}'\nexec '{envmod.node_bin()}' \"$@\"\n")
    w.chmod(0o755)
    return str(w)


@pytest.mark.chat_egress_srt
def test_the_spawned_glue_env_is_closed_whatever_the_parent_holds(tmp_path, origin, monkeypatch) -> None:
    hostile = ("NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "HTTPS_PROXY", "NODE_OPTIONS")
    for k, v in zip(hostile, (str(tmp_path / "hostile.pem"), "0", "http://127.0.0.1:9", "--require /nonexist.js")):
        monkeypatch.setenv(k, v)
    dump = tmp_path / "env.dump"
    node = _env_dumping_node(tmp_path, dump)
    # the control: the same dump records an env the child was given (else the absence below proves nothing)
    subprocess.run([node, "--version"], env={"LEVAIN_PROBE_CONTROL": "1"}, capture_output=True, timeout=10)
    assert "LEVAIN_PROBE_CONTROL=1" in dump.read_text()
    dump.write_text("")
    e = _start(tmp_path, origin, monkeypatch, node=node)
    try:
        st, _ = _get(e, origin.port, "/env")
    finally:
        lm.stop_egress(e)
    got = dump.read_text()
    print(f"RUN S8: env given to node (check_node's run, then the glue's), raw:\n{got}")
    assert got.count("--\n") == 2                         # the version check and the glue
    for name in hostile:
        assert name not in got
    assert st == 200


@pytest.mark.chat_egress_srt
def test_a_launch_id_with_no_launches_row_is_refused_and_nothing_spawns(tmp_path, origin, monkeypatch) -> None:
    before = {p.pid for p in _node_children()}
    with pytest.raises(lm.EgressRefused) as ei:
        _start(tmp_path, origin, monkeypatch, launch_id="ln-" + "0" * 32)
    assert "launches" in str(ei.value)
    assert {p.pid for p in _node_children()} == before


@pytest.mark.chat_egress_srt
def test_a_hand_inserted_launches_row_without_its_events_is_refused(tmp_path, origin, monkeypatch) -> None:
    store = EnvelopeStore(tmp_path / "dispatch")
    with pytest.raises(lm.EgressRefused):
        _start(tmp_path, origin, monkeypatch, store=store, launch_id=make_launch(store))


@pytest.mark.chat_egress_srt
def test_production_launch_refuses_seams_before_spawning(tmp_path, origin, monkeypatch) -> None:
    store = EnvelopeStore(tmp_path / "dispatch")
    lid = real_launch(store)
    monkeypatch.delenv(lm.TEST_SEAMS_ENV, raising=False)
    key_pem, cert_pem = spike.make_ca()
    with pytest.raises(lm.EgressRefused):
        lm.start_egress(store=store, launch_id=lid, node=envmod.node_bin(),
                        srt_prefix=envmod.srt_prefix(), events=tmp_path / "ev.jsonl",
                        ca_pem=key_pem + cert_pem, routes=keys.RouteSet.of([]), liveness=Live(),
                        credentials=keys.LaunchCredentials(NoStore()), cards=Desk(), mode=dm.Fixed(classify.GATED),
                        authority=NO_GRANTS, seams=lm.TestSeams(glue_args=("--carve", "127.0.0.1:1")))
    assert not (tmp_path / "ev.jsonl").exists()


@pytest.mark.chat_egress_srt
def test_open_network_launch_carries_the_opt_out_header_and_its_digest(tmp_path, origin, monkeypatch) -> None:
    e = _start(tmp_path, origin, monkeypatch, open_network=True)
    try:
        assert e.header[0] == "open network: network writes are not gated"
        assert e.header[1] == f"node {e.node_version}" and e.node_version.startswith("v26.")
        assert e.egress_digest == lm.egress_digest(open_network=True)
    finally:
        out = lm.stop_egress(e)
    assert out.glue_exit == 0 and out.final_flush["total"] == 0


@pytest.mark.chat_egress_srt
def test_the_header_and_digest_follow_the_mode_one_home(tmp_path, origin, monkeypatch) -> None:
    """RULINGS_R1 item 1: the mode is the one home of open network; the header and digest are read from it."""
    for name, mode in (("fixed", dm.Fixed(classify.OPEN_NETWORK)),
                       ("live", dm.LiveSetting(lambda: classify.OPEN_NETWORK))):
        (tmp_path / name).mkdir()
        e = _start(tmp_path / name, origin, monkeypatch, mode=mode)
        try:
            assert e.header[0] == lm.OPEN_NETWORK_LINE and e.header[-1] == "write_prompting: off", (name, e.header)
            assert e.egress_digest == lm.egress_digest(open_network=True), name
        finally:
            lm.stop_egress(e)
    (tmp_path / "gated").mkdir()
    e = _start(tmp_path / "gated", origin, monkeypatch, mode=dm.Fixed(classify.GATED))
    try:
        assert lm.OPEN_NETWORK_LINE not in e.header and e.egress_digest == lm.egress_digest(open_network=False)
    finally:
        lm.stop_egress(e)


@pytest.mark.chat_egress_srt
def test_the_decider_is_capped_at_the_mode_the_header_and_digest_were_read_from(tmp_path, origin, monkeypatch) -> None:
    """Q-R15-9 (chd2fix): the decider's cap is start_egress's one read, so a loosening after it never applies and a
    tightening does."""
    standing = {"mode": classify.GATED}
    e = _start(tmp_path, origin, monkeypatch, mode=dm.LiveSetting(lambda: standing["mode"]))
    try:
        assert lm.OPEN_NETWORK_LINE not in e.header and e.decider.mode.launched == classify.GATED
        standing["mode"] = classify.OPEN_NETWORK
        assert e.decider.mode.get() == classify.GATED
    finally:
        lm.stop_egress(e)
    standing["mode"] = classify.OPEN_NETWORK
    (tmp_path / "open").mkdir()
    e = _start(tmp_path / "open", origin, monkeypatch, mode=dm.LiveSetting(lambda: standing["mode"]))
    try:
        assert e.header[0] == lm.OPEN_NETWORK_LINE and e.decider.mode.launched == classify.OPEN_NETWORK
        standing["mode"] = classify.GATED
        assert e.decider.mode.get() == classify.GATED
        standing["mode"] = classify.OPEN_NETWORK             # the revoke undone: a loosening, next launch only
        assert e.decider.mode.get() == classify.GATED
    finally:
        lm.stop_egress(e)


@pytest.mark.chat_egress_srt
def test_a_mode_outside_the_known_modes_is_refused_before_anything_spawns(tmp_path, origin, monkeypatch) -> None:
    before = {p.pid for p in _node_children()}
    (tmp_path / "x").mkdir()
    with pytest.raises(lm.EgressRefused) as ei:
        _start(tmp_path / "x", origin, monkeypatch, mode=dm.Fixed("open"))
    assert "'open'" in str(ei.value)
    assert {p.pid for p in _node_children()} == before and not (tmp_path / "x" / "events.jsonl").exists()


@pytest.mark.chat_egress_srt
def test_a_bare_node_name_is_found_on_the_services_path(tmp_path, origin, monkeypatch) -> None:
    """The glue runs under env={}, where a bare `node` would be looked up on the default path only."""
    monkeypatch.setenv("PATH", f"{Path(envmod.node_bin()).parent}:/usr/bin:/bin")
    e = _start(tmp_path, origin, monkeypatch, node="node")
    try:
        assert e.node_version.startswith("v26.") and e.proc.args[0] == str(Path(envmod.node_bin()))
    finally:
        assert lm.stop_egress(e).glue_exit == 0


def _node_children():
    class P:
        def __init__(self, pid):
            self.pid = pid
    out = subprocess.run(["/usr/bin/pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout
    return [P(int(x)) for x in out.split()]


def test_the_node_lock_pins_srt_exact_with_an_integrity_hash() -> None:
    node_dir = Path(lm.__file__).resolve().parent / "node"
    pkg = json.loads((node_dir / "package.json").read_text())
    lock = json.loads((node_dir / "package-lock.json").read_text())
    assert pkg["dependencies"] == {"@anthropic-ai/sandbox-runtime": lm.SRT_VERSION}
    assert lock["packages"][""]["dependencies"] == pkg["dependencies"]
    entry = lock["packages"]["node_modules/@anthropic-ai/sandbox-runtime"]
    assert entry["version"] == lm.SRT_VERSION and entry["integrity"].startswith("sha512-")
    for path, p in lock["packages"].items():
        if path:
            assert p["resolved"].startswith("https://registry.npmjs.org/") and p["integrity"].startswith("sha512-")
