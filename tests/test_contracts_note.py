"""F2 signed notes against the frozen vectors, and F13's ssh-agent round trip through a throwaway agent."""
import hashlib
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from levain.contracts import ContractError, keys as K, note as N
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("note")
BY_ID = {v["id"]: v for v in VECTORS}


def _note(i):
    return h(i["note_hex"]) if "note_hex" in i else i["note"]


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i = v["input"]
    known = [N.parse_vkey(k) for k in i["known"]]
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            N.verify(_note(i), known)
    else:
        _, good = N.verify(_note(i), known)
        assert [k.name for k in good] == want["verified"]


def test_key_id_and_vkey_of_the_spec_example():
    k = N.parse_vkey("example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k")
    assert k.key_id.hex() == "530d903a" and k.vkey() == "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k"
    assert N.key_id("example.com/foo", 1, k.pub) == hashlib.sha256(b"example.com/foo\n\x01" + k.pub).digest()[:4]


@pytest.mark.parametrize("bad", [
    "example.com/foo+530d903b+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k",   # ID does not match
    "example.com/foo+530D903A+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k",   # uppercase hex
    "example.com/foo+530d903a+AukyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k",   # type 0x02
    "example com+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k",
    "example.com/foo+530d903a",
])
def test_parse_vkey_refusals(bad):
    with pytest.raises(ContractError):
        N.parse_vkey(bad)


def test_sign_round_trips_and_refuses_bad_text():
    seed = bytes(range(32))
    signed = N.sign("hello\n", "golden.example/s", seed)
    key = N.make_key("golden.example/s", __import__("levain.contracts.ed25519", fromlist=["x"]).public_from_seed(seed))
    assert N.verify(signed, [key])[0].text == "hello\n"
    for text in ("no newline", "cr\r\n", "tab\t\n"):
        with pytest.raises(ContractError):
            N.sign(text, "golden.example/s", seed)


@pytest.mark.parametrize("name", ["", "a b", "a b", "a　b", "a+b", "a b"])
def test_key_names_refused(name):
    assert not N.valid_key_name(name)


# ---- F13: the throwaway ssh-agent round trip ----------------------------------------------------------------


def _agent_request(sock_path: str, key_blob: bytes, data: bytes) -> bytes:
    body = bytes([13]) + K.ssh_string(key_blob) + K.ssh_string(data) + struct.pack(">I", 0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(sock_path)
        s.sendall(struct.pack(">I", len(body)) + body)
        head = b""
        while len(head) < 4:
            head += s.recv(4 - len(head))
        (n,) = struct.unpack(">I", head)
        resp = b""
        while len(resp) < n:
            resp += s.recv(n - len(resp))
    assert resp[0] == 14, f"agent answered type {resp[0]}"
    sig_blob, off = K.read_string(resp, 1)
    assert off == len(resp)
    return sig_blob


@pytest.mark.skipif(not (shutil.which("ssh-agent") and shutil.which("ssh-add")), reason="no OpenSSH agent tools")
def test_ssh_agent_note_round_trip_equals_the_frozen_vector():
    """Sign the vector's text with the seed key held by a throwaway ssh-agent; the note bytes must be the vector's.

    The agent is started for this test alone (its socket in a fresh directory), and killed after. The operator's
    own agent is never addressed: SSH_AUTH_SOCK is set only in the environment of the ``ssh-add`` child.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    v = BY_ID["agent-roundtrip"]
    name = "golden.example/agent"
    seed = hashlib.sha256(b"levain-contracts/golden/agent-ed25519").digest()
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    text = v["input"]["note"].split("\n\n")[0] + "\n"

    base = os.environ.get("LEVAIN_TEST_AGENT_DIR") or tempfile.gettempdir()
    work = Path(tempfile.mkdtemp(prefix="lvag-", dir=base))
    sock = str(work / "a.sock")
    keyfile = work / "id_ed25519"
    keyfile.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                           serialization.NoEncryption()))
    keyfile.chmod(0o600)
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        env = {k: x for k, x in os.environ.items() if k != "SSH_AGENT_PID"}
        env["SSH_AUTH_SOCK"] = sock
        subprocess.run(["ssh-add", str(keyfile)], env=env, check=True, capture_output=True, timeout=30)
        sig_blob = _agent_request(sock, K.ed25519_blob(pub), text.encode())
    finally:
        agent.kill()
        agent.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)

    sig = K.unwrap_agent_signature(sig_blob)
    note = text + "\n" + N.signature_line(name, N.key_id(name, 1, pub), sig)
    assert note == v["input"]["note"]
    N.verify(note, [N.parse_vkey(v["input"]["known"][0])])


@pytest.mark.parametrize("blob", [
    K.ssh_string(b"ssh-rsa") + K.ssh_string(bytes(64)),                              # another algorithm
    K.ssh_string(b"ssh-ed25519") + K.ssh_string(bytes(63)),                          # short signature
    K.ssh_string(b"ssh-ed25519") + K.ssh_string(bytes(64)) + b"\x00",                # trailing byte
    K.ssh_string(b"ssh-ed25519")[:-1],                                               # truncated
])
def test_agent_signature_unwrap_refusals(blob):
    with pytest.raises(ContractError):
        K.unwrap_agent_signature(blob)


@pytest.mark.parametrize("sig_type", [2, 0, -1, 256, True, 1.0])
def test_make_key_refuses_every_type_but_ed25519_and_cosignature_v1(sig_type):
    with pytest.raises(ContractError):
        N.make_key("a.example/k", bytes(32), sig_type)


def test_make_key_accepts_both_types_and_parse_vkey_reads_them_back():
    for t in (N.SIG_TYPE_ED25519, N.SIG_TYPE_COSIG_V1):
        k = N.make_key("a.example/k", bytes(32), t)
        assert N.parse_vkey(k.vkey()) == k


@pytest.mark.parametrize("note", ["x\n\n— a\ud800 AAAAAAA=\n", "x\ud800\n\n— a AAAAAAA=\n"])
def test_parse_refuses_a_str_that_is_not_utf8_encodable(note):
    with pytest.raises(ContractError):
        N.parse(note)


def test_sign_refuses_text_that_is_not_utf8_encodable():
    with pytest.raises(ContractError):
        N.sign("x\ud800\n", "a.example/k", bytes(32))
