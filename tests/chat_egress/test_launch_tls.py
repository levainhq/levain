"""CH-D2 S8 (second slice): the glue's upstream trust (R15-16-Q2, R15-12-B6 cert half, R15-10-A10).

srt's upstream trust is set to `tls.rootCertificates` explicitly beside the closed env (B6): two independent guards
against an inherited CA. Each red runs through `start_egress` (the production spawn) to a local TLS origin whose
leaf a BAD CA signed:
  - roots alone: a `node` wrapper puts the bad CA in NODE_EXTRA_CA_CERTS (the closed env defeated) -> refused;
  - env alone: the glue's `--system-roots off` seam unsets the explicit roots under the closed env -> refused;
  - the control, both guards off (the wrapper AND the seam) -> 200, so the probe can fail.
"""

from __future__ import annotations

import datetime
import http.client
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from levain.chat_egress import classify, keys
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore

from .decider_rig import Desk, Live, Lookup, NoStore
from .fixtures import env as envmod
from .test_launch import real_launch

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "spike_glue"))
import decider_spike4 as spike  # noqa: E402

HOST = "bad.s8.test"
_NOW = datetime.datetime.now(datetime.timezone.utc)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_key(k) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def bad_pki(d: Path) -> tuple[Path, Path, Path]:
    """A BAD CA and the leaf it signs for HOST: (bad-ca.pem, leaf.pem, leaf.key)."""
    ck = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "S8 BAD CA (hostile NODE_EXTRA_CA_CERTS)")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ck.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(_NOW - datetime.timedelta(minutes=5))
          .not_valid_after(_NOW + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ck.public_key()), critical=False)
          .sign(ck, hashes.SHA256()))
    lk = _key()
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
            .issuer_name(ca.subject).public_key(lk.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_NOW - datetime.timedelta(minutes=5)).not_valid_after(_NOW + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ck.public_key()), critical=False)
            .sign(ck, hashes.SHA256()))
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad-ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (d / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (d / "leaf.key").write_bytes(_pem_key(lk))
    return d / "bad-ca.pem", d / "leaf.pem", d / "leaf.key"


class TlsOrigin:
    def __init__(self, cert: Path, key: Path) -> None:
        self.reqs: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.reqs.append(self.path)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _hostile_node(tmp: Path, bad_ca: Path) -> str:
    """A `node` that hands the real node an inherited NODE_EXTRA_CA_CERTS (the closed env defeated)."""
    w = tmp / "node-hostile-ca"
    w.write_text(f"#!/bin/sh\nNODE_EXTRA_CA_CERTS='{bad_ca}' exec '{envmod.node_bin()}' \"$@\"\n")
    w.chmod(0o755)
    return str(w)


def _start(tmp: Path, port: int, monkeypatch, *, node: str, extra: tuple[str, ...] = ()):
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    store = EnvelopeStore(tmp / "dispatch")
    seams = lm.TestSeams(glue_args=("--tls-ports", f"443,{port}", "--carve", f"127.0.0.1:{port}",
                                    "--resolve-map", f"{HOST}=127.0.0.1", *extra), lookup=Lookup())
    key_pem, cert_pem = spike.make_ca()
    e = lm.start_egress(store=store, launch_id=real_launch(store), node=node,
                        srt_prefix=envmod.srt_prefix(), events=tmp / "events.jsonl",
                        ca_pem=key_pem + cert_pem, routes=keys.RouteSet.of([]), liveness=Live(),
                        credentials=keys.LaunchCredentials(NoStore()), cards=Desk(), mode=dm.Fixed(classify.GATED),
                        limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0), seams=seams)
    return e, cert_pem


def _get_tls(e, egress_ca_pem: bytes, port: int) -> tuple[int, bytes]:
    ctx = ssl.create_default_context(cadata=egress_ca_pem.decode())
    c = http.client.HTTPSConnection("127.0.0.1", e.http_port, timeout=20, context=ctx)
    c.set_tunnel(HOST, port)
    try:
        c.request("GET", "/q2")
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.fixture
def pki(tmp_path):
    bad_ca, leaf, key = bad_pki(tmp_path / "pki")
    o = TlsOrigin(leaf, key)
    yield bad_ca, o
    o.close()


def _run(tmp_path, monkeypatch, origin, *, node: str, extra: tuple[str, ...] = ()) -> tuple[int, bytes]:
    e, ca = _start(tmp_path, origin.port, monkeypatch, node=node, extra=extra)
    try:
        return _get_tls(e, ca, origin.port)
    finally:
        out = lm.stop_egress(e)
        assert out.glue_exit == 0, (out, e.stderr.read_text())


@pytest.mark.chat_egress_srt
def test_the_explicit_roots_alone_refuse_a_bad_cert_under_an_inherited_hostile_ca(tmp_path, monkeypatch, pki) -> None:
    bad_ca, origin = pki
    st, body = _run(tmp_path, monkeypatch, origin, node=_hostile_node(tmp_path, bad_ca))
    print(f"RUN S8B Q2 roots alone: {st} {body[:120]!r}; origin saw {origin.reqs}")
    assert st != 200 and origin.reqs == []


@pytest.mark.chat_egress_srt
def test_the_closed_env_alone_refuses_a_bad_cert_with_the_roots_unset(tmp_path, monkeypatch, pki) -> None:
    _bad_ca, origin = pki
    st, body = _run(tmp_path, monkeypatch, origin, node=envmod.node_bin(), extra=("--system-roots", "off"))
    print(f"RUN S8B Q2 env alone: {st} {body[:120]!r}; origin saw {origin.reqs}")
    assert st != 200 and origin.reqs == []


@pytest.mark.chat_egress_srt
def test_control_both_guards_off_the_bad_cert_is_accepted(tmp_path, monkeypatch, pki) -> None:
    bad_ca, origin = pki
    st, body = _run(tmp_path, monkeypatch, origin, node=_hostile_node(tmp_path, bad_ca),
                    extra=("--system-roots", "off"))
    print(f"RUN S8B Q2 control: {st} {body!r}; origin saw {origin.reqs}")
    assert (st, body) == (200, b"ok") and origin.reqs == ["/q2"]
