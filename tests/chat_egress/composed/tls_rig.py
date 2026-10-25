"""CH-D2 S9 session 2a: the TLS and keyed half of the composed rig.

`TlsLaunch` starts the production spawn (`launch.start_egress`: the real glue, decider, SOCKS refuser and stop order)
over srt 0.0.81, with a TLS recording origin whose leaf a test CA signed and the glue trusts through its `--upstream-ca`
seam, and (optionally) a keyed route whose value a `HeldStore` holds. The client side of a request is a CONNECT through
the launch's HTTP listener, then TLS to the Levain leaf (trusting the launch's CA), as a sandboxed client does.

One stand-in: the real `keys.decide_key` attaches only on port 443, which a non-root test cannot bind, so the origin's
port is presented to `decide_key` as 443 (`dataclasses.replace(parsed, port=443)`). Everything else in `decide_key`
(the liveness read, the advertisement check, the credential read through `LaunchCredentials`) is the real code.
"""

from __future__ import annotations

import dataclasses
import datetime
import socket
import ssl
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from levain.chat_egress import classify, keys
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore

from ..decider_rig import Desk, Live, Lookup
from ..fixtures import env as envmod
from ..test_launch import real_launch
from .rig import RecordingOrigin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "spike_glue"))
import decider_spike4 as spike  # noqa: E402  (its make_ca: an RSA CA srt's node-forge accepts)

KEYED = "key.s9.test"
UNKEYED = "pub.s9.test"
RECORD = "rec-s9"
SOURCE, ACCOUNT = "svc-s9", "acct-s9"


def make_pki(d: Path, hosts: list[str]) -> tuple[Path, Path, Path]:
    """A test CA and one leaf it signs for `hosts`: (ca.pem, leaf.pem, leaf.key)."""
    now = datetime.datetime.now(datetime.timezone.utc)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    ck = key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "S9 test origin CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ck.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ck.public_key()), critical=False)
          .sign(ck, hashes.SHA256()))
    lk = key()
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca.subject).public_key(lk.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ck.public_key()), critical=False)
            .sign(ck, hashes.SHA256()))
    d.mkdir(parents=True, exist_ok=True)
    (d / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (d / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (d / "leaf.key").write_bytes(lk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return d / "ca.pem", d / "leaf.pem", d / "leaf.key"


class HeldStore:
    """A credential store holding one value per (source, account); counts its reads."""

    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self.values = dict(values)
        self.reads = 0

    def read(self, source: str, account: str) -> str:
        self.reads += 1
        try:
            return self.values[(source, account)]
        except KeyError:
            raise keys.CredentialUnreadable("no such item") from None


def read_response(s) -> bytes:
    """Reads to the end of the first response head, then whatever else is already waiting (a peer close ends it)."""
    out = b""
    try:
        while b"\r\n\r\n" not in out:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except (OSError, ssl.SSLError):
        pass
    return out


class TlsLaunch:
    """One production-spawned launch over a TLS recording origin. `secret` is the held key's value (None: an unkeyed
    launch). `dir` is the one directory a run writes under (events, glue stderr, the dispatch store)."""

    def __init__(self, tmp: Path, monkeypatch, *, secret: str | None = None, mode: str = classify.GATED,
                 reflect=None, content_type=None, liveness=None, answer: str | None = None) -> None:
        self.dir = tmp
        self.secret = secret
        self.desk = Desk()
        self.answer = answer
        ca, leaf, key = make_pki(tmp / "pki", [KEYED, UNKEYED])
        self.origin = RecordingOrigin(reflect=reflect, content_type=content_type, tls=(str(leaf), str(key)))
        self.store = HeldStore({(SOURCE, ACCOUNT): secret} if secret is not None else {})
        P = self.origin.port
        self.origin_port = P
        real_decide = keys.decide_key

        def decide(route, parsed, **kw):
            if parsed.port == P:
                parsed = dataclasses.replace(parsed, port=443)
            return real_decide(route, parsed, **kw)
        monkeypatch.setattr(keys, "decide_key", decide)
        monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
        route = keys.RouteSnapshot(record_id=RECORD, canonical_host=KEYED, credential_source=SOURCE,
                                   declared_repos=frozenset(), attach=True, credential_account=ACCOUNT,
                                   incarnation="1:s9")
        estore = EnvelopeStore(tmp / "dispatch")
        key_pem, cert_pem = spike.make_ca()
        self.ca_pem = cert_pem
        self.e = lm.start_egress(
            store=estore, launch_id=real_launch(estore), node=envmod.node_bin(), srt_prefix=envmod.srt_prefix(),
            events=tmp / "events.jsonl", ca_pem=key_pem + cert_pem,
            routes=keys.RouteSet.of([route] if secret is not None else []), liveness=liveness or Live(),
            credentials=keys.LaunchCredentials(self.store), cards=self.desk, mode=dm.Fixed(mode),
            limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0),
            seams=lm.TestSeams(glue_args=("--tls-ports", f"443,{P}", "--carve", f"127.0.0.1:{P}",
                                          "--resolve-map", f"{KEYED}=127.0.0.1;{UNKEYED}=127.0.0.1",
                                          "--upstream-ca", str(ca)), lookup=Lookup()))
        self.http_port = self.e.http_port
        self.socks_port = self.e.socks_port
        self.stopped: lm.StopResult | None = None

    # --- the client's side ---------------------------------------------------------------------------------------

    def tunnel(self, connect_host: str, sni: str | None, *, port: int | None = None, timeout: float = 20.0):
        """CONNECT `connect_host:port` through the listener, then TLS with `sni` (None: no SNI sent), trusting the
        launch's CA. Returns (tls socket, the CONNECT reply head)."""
        s = socket.create_connection(("127.0.0.1", self.http_port), timeout=timeout)
        p = port or self.origin_port
        s.sendall(f"CONNECT {connect_host}:{p} HTTP/1.1\r\nHost: {connect_host}:{p}\r\n\r\n".encode())
        head = read_response(s)
        ctx = ssl.create_default_context(cadata=self.ca_pem.decode())
        ctx.check_hostname = False                       # the name checked is the leaf's, by CA; SNI is the test's variable
        t = ctx.wrap_socket(s, server_hostname=sni) if sni else self._no_sni(ctx, s)
        return t, head

    @staticmethod
    def _no_sni(ctx: ssl.SSLContext, s: socket.socket):
        """A handshake that sends no server_name extension (the client library refuses an empty name, so an
        SSLObject over memory BIOs is driven by hand)."""
        inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
        obj = ctx.wrap_bio(inc, out, server_side=False)
        while True:
            try:
                obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                s.sendall(out.read())
                inc.write(s.recv(65536))
        s.sendall(out.read())
        return _BioSocket(obj, inc, out, s)

    def raw(self, request: bytes, *, connect_host: str = UNKEYED, sni: str | None = UNKEYED) -> bytes:
        t, _ = self.tunnel(connect_host, sni)
        try:
            t.sendall(request)
            return read_response(t)
        finally:
            t.close()

    def get(self, path: str, *, host: str = UNKEYED, headers: list[tuple[str, str]] | None = None) -> bytes:
        h = "".join(f"{k}: {v}\r\n" for k, v in (headers or []))
        return self.raw(f"GET {path} HTTP/1.1\r\nHost: {host}:{self.origin_port}\r\n{h}Connection: close\r\n\r\n".encode(),
                        connect_host=host, sni=host)

    # --- teardown ------------------------------------------------------------------------------------------------

    def stop(self) -> lm.StopResult:
        if self.stopped is None:
            self.stopped = lm.stop_egress(self.e)
        return self.stopped

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self.origin.close()


class _BioSocket:
    """Just enough socket for `read_response` / `sendall` / `close` over a hand-driven SSLObject."""

    def __init__(self, obj, inc, out, s) -> None:
        self.obj, self.inc, self.out, self.s = obj, inc, out, s

    def sendall(self, data: bytes) -> None:
        self.obj.write(data)
        self.s.sendall(self.out.read())

    def recv(self, n: int) -> bytes:
        while True:
            try:
                return self.obj.read(n)
            except ssl.SSLWantReadError:
                self.s.sendall(self.out.read())
                d = self.s.recv(65536)
                if not d:
                    return b""
                self.inc.write(d)
            except ssl.SSLZeroReturnError:
                return b""

    def close(self) -> None:
        self.s.close()
