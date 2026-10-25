"""Round 3 test PKI (all throwaway, under <out>/pki): an ORIGIN CA that signs the good local origin's *.rb.test leaf,
and a BAD CA that signs bad.rb.test's leaf (the B6 bad-certificate probe). usage: mkcerts.py <out-dir>"""
import datetime
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

NOW = datetime.datetime.now(datetime.timezone.utc)


def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ca(cn):
    k = key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(NOW - datetime.timedelta(minutes=5))
         .not_valid_after(NOW + datetime.timedelta(days=2))
         .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                      key_encipherment=False, data_encipherment=False, key_agreement=False,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
         .sign(k, hashes.SHA256()))
    return k, c


def leaf(ca_k, ca_c, names):
    k = key()
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
         .issuer_name(ca_c.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - datetime.timedelta(minutes=5)).not_valid_after(NOW + datetime.timedelta(days=2))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
         .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_k.public_key()), critical=False)
         .sign(ca_k, hashes.SHA256()))
    return k, c


def dump(d, stem, k, c):
    (d / f"{stem}.key").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    (d / f"{stem}.pem").write_bytes(c.public_bytes(serialization.Encoding.PEM))


def main():
    d = Path(sys.argv[1]) / "pki"
    d.mkdir(parents=True, exist_ok=True)
    ok_k, ok_c = ca("round3 ORIGIN CA")
    bad_k, bad_c = ca("round3 BAD CA (hostile NODE_EXTRA_CA_CERTS)")
    dump(d, "origin-ca", ok_k, ok_c)
    dump(d, "bad-ca", bad_k, bad_c)
    lk, lc = leaf(ok_k, ok_c, ["api.rb.test", "a.rb.test", "b.rb.test", "s.rb.test", "b7.rb.test", "pin2.rb.test",
                               "*.rb.test"])
    dump(d, "good-leaf", lk, lc)
    bk, bc = leaf(bad_k, bad_c, ["bad.rb.test"])
    dump(d, "bad-leaf", bk, bc)
    print(f"pki in {d}")


main()
