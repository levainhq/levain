"""CH-D2 S8 (second slice): the egress CA's and the leaves' custody (R9-N5, design §3.8.5(a) item 3, (o) item 9).

The CA is minted by `ca.new_egress_ca` (key in memory only, cert on disk) and handed to the real glue over its pipe.
After 20 hosts' leaves are minted through the glue (a TLS handshake each, the leaf checked as signed by the CA for
that host), no PEM private key exists under the state dir, in srt's trust-bundle dir, or in any file the service or
the glue holds open. The scanner's control finds a planted key, so the absence below can fail. srt's trust-bundle
dir is removed at the glue's clean end.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography import x509

from levain.chat_egress import ca as cam
from levain.chat_egress import classify, keys
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore

from .decider_rig import Desk, Live, Lookup, NoStore
from .fixtures import env as envmod
from .test_launch import NO_GRANTS, real_launch

NOW = datetime.datetime.now(datetime.timezone.utc)
HOSTS = [f"h{i}.s8.test" for i in range(20)]
PRIVATE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def private_key_files(root: Path) -> list[Path]:
    """Every regular file under `root` (no symlink followed) holding a PEM private key."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in filenames:
            p = Path(dirpath) / n
            try:
                if stat.S_ISREG(os.lstat(p).st_mode) and PRIVATE.search(p.read_bytes()):
                    out.append(p)
            except OSError:
                pass
    return out


def open_regular_files(pid: int) -> list[Path]:
    """The regular files ``pid`` holds open: through procfs where this host has it (Linux has no
    ``/usr/sbin/lsof`` on a stock runner), else ``lsof``. On procfs each result is the ``/proc/<pid>/fd/N``
    link itself, so reading it reads the open inode: a key unlinked while held open is still found. A pid
    with no fd directory on a procfs host raises (an exited process fails the test, never skips it)."""
    if Path("/proc/self/fd").is_dir():
        held = []
        for fd in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                if stat.S_ISREG(fd.stat().st_mode):
                    held.append(fd)
            except OSError:
                pass                                   # closed between the listing and the stat
        return held
    lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else shutil.which("lsof")
    if lsof is None:
        pytest.skip("neither /proc/<pid>/fd nor lsof is here")
    out = subprocess.run([lsof, "-n", "-P", "-Fn", "-a", "-p", str(pid), "-d", "^txt"],
                         capture_output=True, text=True, timeout=30).stdout
    paths = [Path(line[1:]) for line in out.splitlines() if line.startswith("n/")]
    return [p for p in paths if p.is_file()]


def _held_bytes(p: Path) -> bytes:
    """A held file's bytes; b"" for one closed since the listing (a closed descriptor holds no key)."""
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return b""


def test_the_ca_cert_is_on_disk_and_its_key_is_not(tmp_path) -> None:
    a = cam.new_egress_ca(tmp_path, now=NOW, start_id="s1")
    assert a.cert_path == tmp_path / "egress-ca" / "s1.pem" and a.cert_path.read_bytes() == a.cert_pem
    assert stat.S_IMODE(os.stat(a.cert_path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(tmp_path / "egress-ca").st_mode) == 0o755
    assert private_key_files(tmp_path) == []
    assert PRIVATE.search(a.glue_pem()) and "PRIVATE" not in repr(a)
    cert = x509.load_pem_x509_certificate(a.cert_pem)
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    assert cert.not_valid_after_utc - cert.not_valid_before_utc == cam.CA_VALIDITY + datetime.timedelta(minutes=5)
    b = cam.new_egress_ca(tmp_path, now=NOW, start_id="s2")      # a restart: a new CA, the old cert deleted
    assert sorted(p.name for p in (tmp_path / "egress-ca").iterdir()) == ["s2.pem"]
    assert b.fingerprint_sha256 != a.fingerprint_sha256


def test_the_trust_env_names_the_cert_and_puts_pip_on_legacy_certs(tmp_path) -> None:
    """chd2fix P1: a truststore pip fails TLS inside the chat sandbox (S10 15f, pip 26.1.1); the env
    carries PIP_USE_DEPRECATED=legacy-certs beside PIP_CERT. No name points at anything but this start's cert."""
    a = cam.new_egress_ca(tmp_path, now=NOW, start_id="t1")
    env = a.trust_env()
    assert env == cam.trust_env(a.cert_path)
    assert env["PIP_USE_DEPRECATED"] == "legacy-certs" and env["PIP_CERT"] == str(a.cert_path)
    assert {n for n, v in env.items() if v == str(a.cert_path)} == set(cam.TRUST_FILE_ENV)
    assert {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"} <= set(env)
    assert env["NPM_CONFIG_AUDIT"] == env["NPM_CONFIG_FUND"] == "false"
    assert set(env) == set(cam.TRUST_FILE_ENV) | set(cam.TRUST_SET_ENV)


def test_the_ca_carries_its_expiry_and_a_re_mint_keeps_the_earlier_cert(tmp_path) -> None:
    """chatdriver-2 (`1025-23/chatdriver/RULINGS_OBS.md` 1): the CA names its own not_valid_after (aware UTC, read
    from the cert), and a re-mint inside one service start (`prune=False`) keeps the cert its earlier launches trust."""
    a = cam.new_egress_ca(tmp_path, now=NOW, start_id="r1")
    cert = x509.load_pem_x509_certificate(a.cert_pem)
    assert a.not_valid_after == cert.not_valid_after_utc and a.not_valid_after.utcoffset() == datetime.timedelta(0)
    b = cam.new_egress_ca(tmp_path, now=NOW, start_id="r2", prune=False)
    assert sorted(p.name for p in (tmp_path / "egress-ca").iterdir()) == ["r1.pem", "r2.pem"]
    assert b.fingerprint_sha256 != a.fingerprint_sha256


def test_the_cert_is_never_written_through_a_symlink(tmp_path) -> None:
    target = tmp_path / "elsewhere.pem"
    target.write_bytes(b"untouched")
    (tmp_path / "egress-ca").mkdir()
    (tmp_path / "egress-ca" / "s3.pem").symlink_to(target)
    with pytest.raises(OSError):
        cam.new_egress_ca(tmp_path, now=NOW, start_id="s3")
    assert target.read_bytes() == b"untouched"


# --- S8D r1 fix item 3: a short os.write never leaves a truncated cert ------------------------------------------------

def _short_writes(monkeypatch, *, fail_after: int | None = None) -> list[int]:
    """os.write writing at most 7 bytes a call (a legal short write) to any fd but 0-2; after `fail_after` calls it
    raises OSError(EIO). The counts written are returned."""
    real, counts = os.write, []

    def write(fd, data):
        if fd in (0, 1, 2):
            return real(fd, data)
        if fail_after is not None and len(counts) >= fail_after:
            raise OSError(5, "Input/output error")
        counts.append(real(fd, bytes(data[:7])))
        return counts[-1]
    monkeypatch.setattr(os, "write", write)
    return counts


def test_a_short_write_still_lands_the_whole_cert(tmp_path, monkeypatch) -> None:
    counts = _short_writes(monkeypatch)
    a = cam.new_egress_ca(tmp_path, now=NOW, start_id="s4")
    monkeypatch.undo()
    print(f"RUN S8D fix 3 short write: {len(counts)} writes, {sum(counts)} of {len(a.cert_pem)} bytes")
    assert a.cert_path.read_bytes() == a.cert_pem and len(counts) > 1
    x509.load_pem_x509_certificate(a.cert_path.read_bytes())


def test_a_write_that_fails_part_way_leaves_no_partial_cert(tmp_path, monkeypatch) -> None:
    _short_writes(monkeypatch, fail_after=3)
    with pytest.raises(OSError):
        cam.new_egress_ca(tmp_path, now=NOW, start_id="s5")
    monkeypatch.undo()
    assert sorted(p.name for p in (tmp_path / "egress-ca").iterdir()) == []


def test_control_the_scanner_finds_a_planted_key(tmp_path) -> None:
    a = cam.new_egress_ca(tmp_path, now=NOW, start_id="c")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "planted").write_bytes(a.glue_pem())
    assert private_key_files(tmp_path) == [tmp_path / "deep" / "planted"]
    with open(tmp_path / "deep" / "planted", "rb"):
        assert tmp_path.resolve() / "deep" / "planted" in [p.resolve() for p in open_regular_files(os.getpid())]


def _handshake(port: int, host: str, ca_pem: bytes) -> x509.Certificate:
    ctx = ssl.create_default_context(cadata=ca_pem.decode())
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            assert chunk, head
            head += chunk
        assert head.startswith(b"HTTP/1.1 200"), head
        with ctx.wrap_socket(s, server_hostname=host) as t:
            return x509.load_der_x509_certificate(t.getpeercert(binary_form=True))
    finally:
        s.close()


@pytest.mark.chat_egress_srt
def test_twenty_leaves_minted_leave_no_private_key_on_disk_or_open(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    a = cam.new_egress_ca(state, now=NOW)
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    store = EnvelopeStore(state / "dispatch")
    e = lm.start_egress(store=store, launch_id=real_launch(store), node=envmod.node_bin(),
                        srt_prefix=envmod.srt_prefix(), events=state / "events.jsonl",
                        ca_pem=a.glue_pem(), routes=keys.RouteSet.of([]), liveness=Live(),
                        credentials=keys.LaunchCredentials(NoStore()), cards=Desk(), mode=dm.Fixed(classify.GATED),
                        authority=NO_GRANTS, limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0),
                        seams=lm.TestSeams(glue_args=("--resolve-map", ";".join(f"{h}=127.0.0.1" for h in HOSTS)),
                                           lookup=Lookup()))
    bundle = None
    try:
        issuer = x509.load_pem_x509_certificate(a.cert_pem).subject
        for h in HOSTS:
            leaf = _handshake(e.http_port, h, a.cert_pem)
            assert leaf.issuer == issuer
            assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
                .get_values_for_type(x509.DNSName) == [h]
        bundle = Path(re.search(r"bundle=(\S+)", e.stderr.read_text()).group(1)).parent
        held = open_regular_files(os.getpid()) + open_regular_files(e.proc.pid)
        found = (private_key_files(state) + private_key_files(bundle)
                 + [p for p in held if PRIVATE.search(_held_bytes(p))])
        print(f"RUN S8B custody: 20 leaves minted by {issuer.rfc4514_string()}; scanned {state}, {bundle} and "
              f"{len(held)} open files; private keys found: {found}")
        assert found == []
    finally:
        out = lm.stop_egress(e)
    assert out.glue_exit == 0
    assert bundle is not None and not bundle.exists()                 # srt's trust-bundle dir removed at the end
