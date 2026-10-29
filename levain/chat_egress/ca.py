"""The chat egress CA and its custody (design §3.8.5(a) item 3, (o) item 9, R9-N5; CH-D2 S8).

:func:`new_egress_ca` mints the CA at chat-service start (never per launch, never reused across starts). The KEY NEVER
TOUCHES DISK: it lives only in the closure behind :meth:`EgressCA.glue_pem`, whose bytes `launch.start_egress` writes
into the glue's CA pipe; the glue hands them to srt in memory, and srt mints every LEAF (one per host) in its process
memory (srt 0.0.81 `mitm-leaf.js`: a cache on the CA object, no file). The CERT is written to
`<state_dir>/egress-ca/<start id>.pem` (dir 0755, file 0644, never through a symlink) so the chat env can trust it, and
older certs there are deleted at start. A service up near its CA's `not_valid_after` mints a new one at a launch
(`prune=False`: the earlier cert stays while launches started on it may run; the service removes its certs at close).

The key is RSA 2048, not the design's EC P-256: srt's node-forge refuses an EC CA (the spike's measurement, kept in the
test fixture `decider_spike4.make_ca`).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

__all__ = ["CA_DIR", "CA_VALIDITY", "TRUST_FILE_ENV", "TRUST_SET_ENV", "EgressCA", "new_egress_ca", "trust_env"]

CA_DIR = "egress-ca"
CA_VALIDITY = timedelta(days=7)
_BACKDATE = timedelta(minutes=5)

TRUST_FILE_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
                  "PIP_CERT", "CARGO_HTTP_CAINFO")
"""The chat env's trust names (design rev 15 §3.8.5(a), CHAT_EGRESS_TRUST_ENV): each is set to the CA cert's path."""

TRUST_SET_ENV = {
    # A pip that verifies through truststore (macOS's system verifier) fails TLS inside the chat sandbox under
    # PIP_CERT and --cert alike (S10 15f, pip 26.1.1; the same pip and CA pass outside it, chd2fix). `legacy-certs`
    # puts pip back on its own CA handling, where PIP_CERT is honoured. A pip without that choice refuses the value
    # ("invalid choice: 'legacy-certs'"; pip 21.2.4 measured) and runs only with this name unset.
    "PIP_USE_DEPRECATED": "legacy-certs",
    "NPM_CONFIG_AUDIT": "false",                       # design §3.8.5(a): npm's audit POST would card every install
    "NPM_CONFIG_FUND": "false",
}
"""The chat env's fixed trust settings, beside :data:`TRUST_FILE_ENV`."""


def trust_env(cert_path: Path) -> dict[str, str]:
    """The names a chat launch's env carries so its clients trust the egress CA at ``cert_path`` (and only it: the
    file holds no public root). Nothing is added to a system or login keychain."""
    return {**{name: str(cert_path) for name in TRUST_FILE_ENV}, **TRUST_SET_ENV}


@dataclass(frozen=True)
class EgressCA:
    """The CA's public half, and the closure that alone can give the key (to the glue's pipe, in memory)."""
    cert_pem: bytes
    cert_path: Path
    fingerprint_sha256: str
    start_id: str
    not_valid_after: datetime                    # the cert's own, aware UTC
    _key_pem: Callable[[], bytes] = field(repr=False, compare=False)

    def glue_pem(self) -> bytes:
        """Key + cert PEM for the glue's CA pipe (`start_egress(ca_pem=...)`); never written anywhere here."""
        return self._key_pem() + self.cert_pem

    def trust_env(self) -> dict[str, str]:
        """:func:`trust_env` of this CA's cert."""
        return trust_env(self.cert_path)


def _write_cert(d: Path, name: str, pem: bytes, *, prune: bool) -> Path:
    d.mkdir(mode=0o755, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{d} is not a directory")
    os.chmod(d, 0o755)
    for old in (d.iterdir() if prune else ()):
        if old.name.endswith(".pem") and old.name != name:
            old.unlink(missing_ok=True)                 # unlink removes a symlink itself, never its target
    path = d / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    closed = False
    try:
        os.fchmod(fd, 0o644)
        rest = memoryview(pem)
        while rest:                                     # a short write is legal: the chat env must get the whole cert
            n = os.write(fd, rest)
            if n <= 0:
                raise OSError(f"writing {path} made no progress")
            rest = rest[n:]
        closed = True
        os.close(fd)
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            path.unlink(missing_ok=True)                # O_EXCL made it ours: no partial cert is left behind
        except OSError:
            pass                                        # the error that ended the write is the one raised
        raise
    return path


def new_egress_ca(state_dir: Path, *, now: datetime, start_id: str | None = None, prune: bool = True) -> EgressCA:
    """A fresh CA for one chat-service start: the key in memory only, the cert on disk for the chat env's trust.
    ``prune`` deletes every other cert in the directory (a start's leftovers); a re-mint inside one start passes
    False, keeping the cert its earlier launches trust."""
    sid = start_id or secrets.token_hex(8)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Levain chat egress {sid}")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _BACKDATE)
            .not_valid_after(now + CA_VALIDITY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    path = _write_cert(Path(state_dir) / CA_DIR, f"{sid}.pem", pem, prune=prune)

    def key_pem() -> bytes:
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())

    return EgressCA(cert_pem=pem, cert_path=path,
                    fingerprint_sha256=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
                    start_id=sid, not_valid_after=cert.not_valid_after_utc, _key_pem=key_pem)
