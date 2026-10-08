"""levain.team.signing against real git and real ssh-keygen: fingerprints, the signed payload, the three verdicts.

Every key is generated fresh; every signed commit is made by git itself, so the payload this module reconstructs is
checked against the bytes git actually signed.
"""
import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from levain.team import signing as S

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")


def run(*args, cwd=None, input=None, env=None):
    full = None if env is None else {**os.environ, **env}
    return subprocess.run(list(args), cwd=cwd, input=input, capture_output=True, check=True, env=full,
                          stdin=None if input is not None else subprocess.DEVNULL)


def git(*args, cwd, input=None, env=None) -> str:
    return run("git", *args, cwd=cwd, input=input, env=env).stdout.decode()


def keygen(d: Path, name: str, *typeargs: str, passphrase: str = "") -> Path:
    path = d / name
    run("ssh-keygen", "-q", *typeargs, "-N", passphrase, "-C", f"{name}@test", "-f", str(path))
    return path


def keygen_fp(pub: Path) -> str:
    return run("ssh-keygen", "-lf", str(pub)).stdout.decode().split()[1]


def sstr(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    return {
        "ed25519": keygen(d, "ed", "-t", "ed25519"),
        "ecdsa": keygen(d, "ec", "-t", "ecdsa", "-b", "256"),
        "rsa": keygen(d, "rsa", "-t", "rsa", "-b", "3072"),
        "bo": keygen(d, "bo", "-t", "ed25519"),
    }


def pub_line(key: Path) -> str:
    return Path(str(key) + ".pub").read_text()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-q", "--initial-branch=main", cwd=r)
    git("config", "user.email", "ana@ex.com", cwd=r)
    git("config", "user.name", "ana", cwd=r)
    git("config", "commit.gpgsign", "false", cwd=r)
    return r


def commit(repo: Path, msg: str, *, key: Path | None = None, author: str | None = None) -> str:
    (repo / "f.txt").write_text(msg)
    git("add", "f.txt", cwd=repo)
    pre, env = [], None
    if key is not None:
        pre = S.sign_config(str(key) + ".pub")
        env = S.signing_env()
    extra = ["--author", author] if author else []
    git(*pre, "commit", "-q", "-m", msg, *extra, cwd=repo, env=env)
    return git("rev-parse", "HEAD", cwd=repo).strip()


def write_object(repo: Path, raw: bytes) -> str:
    return git("hash-object", "-t", "commit", "-w", "--stdin", cwd=repo, input=raw).strip()


# ---- fingerprint ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["ed25519", "ecdsa"])
def test_fingerprint_matches_ssh_keygen(keys, kind):
    key = keys[kind]
    assert S.fingerprint(pub_line(key)) == keygen_fp(Path(str(key) + ".pub"))


def test_fingerprint_ignores_comment_and_whitespace(keys):
    typ, b64 = pub_line(keys["ed25519"]).split()[:2]
    assert S.fingerprint(f"  {typ}   {b64}  \n") == S.fingerprint(f"{typ} {b64} a comment with spaces")


def _ed_blob(keys) -> bytes:
    return base64.b64decode(pub_line(keys["ed25519"]).split()[1])


@pytest.mark.parametrize("line_fn, needle", [
    (lambda k: "sk-ssh-ed25519@openssh.com " + base64.b64encode(
        sstr(b"sk-ssh-ed25519@openssh.com") + sstr(b"\x01" * 32) + sstr(b"ssh:")).decode(), "security-key"),
    (lambda k: "ssh-ed25519-cert-v01@openssh.com " + pub_line(k["ed25519"]).split()[1], "certificates"),
    (lambda k: "ssh-dss " + pub_line(k["ed25519"]).split()[1], "unknown"),
    (lambda k: "ssh-ed25519 AAAA!!!!", "base64"),
    (lambda k: "ssh-ed25519 " + pub_line(k["ed25519"]).split()[1].rstrip("=") + "x", "base64"),
    (lambda k: "ssh-ed25519 " + base64.b64encode(_ed_blob(k) + b"\x00").decode(), "trailing"),
    (lambda k: "ssh-ed25519 " + base64.b64encode(_ed_blob(k) + sstr(b"extra")).decode(), "trailing"),
    (lambda k: pub_line(k["rsa"]), "unaccepted"),
    (lambda k: "ecdsa-sha2-nistp384 " + pub_line(k["ecdsa"]).split()[1], "blob says"),
    (lambda k: "ssh-ed25519 " + base64.b64encode(sstr(b"ssh-ed25519") + sstr(b"\x01" * 31)).decode(), "32 bytes"),
    (lambda k: "ssh-ed25519 " + base64.b64encode(sstr(b"ssh-ed25519") + b"\x00\x00").decode(), "truncated"),
    (lambda k: "ssh-ed25519", "<type> <base64>"),
    (lambda k: "", "<type> <base64>"),
])
def test_fingerprint_refusals(keys, line_fn, needle):
    with pytest.raises(S.SigningError, match=needle):
        S.fingerprint(line_fn(keys))


def test_fingerprint_refuses_ecdsa_curve_mismatch(keys):
    blob = base64.b64decode(pub_line(keys["ecdsa"]).split()[1])
    typ, off = S._read_string(blob, 0, "type")
    _, off2 = S._read_string(blob, off, "curve")
    forged = sstr(typ) + sstr(b"nistp384") + blob[off2:]
    with pytest.raises(S.SigningError, match="curve"):
        S.fingerprint("ecdsa-sha2-nistp256 " + base64.b64encode(forged).decode())


def test_signing_error_is_a_value_error():
    assert issubclass(S.SigningError, ValueError)


# ---- split_signed -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["ed25519", "ecdsa"])
def test_split_signed_round_trip(repo, keys, kind):
    sha = commit(repo, f"signed by {kind}", key=keys[kind])
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    payload, sig = S.split_signed(raw)
    assert sig is not None and sig.startswith(b"-----BEGIN SSH SIGNATURE-----")
    assert sig.endswith(b"-----END SSH SIGNATURE-----")
    head, _, body = payload.partition(b"\n\n")
    rebuilt = head + b"\ngpgsig " + sig.replace(b"\n", b"\n ") + b"\n\n" + body
    assert rebuilt == raw
    assert b"gpgsig" not in payload


def test_sha256_repository_signature_verifies(tmp_path, keys):
    """A SHA-256 repository writes ``gpgsig-sha256``; reading only ``gpgsig`` made every valid signature "no signature"
    (L3 r2 codex, RAN on git 2.50.1)."""
    r = tmp_path / "r256"
    r.mkdir()
    git("init", "-q", "--object-format=sha256", "--initial-branch=main", cwd=r)
    git("config", "user.email", "ana@ex.com", cwd=r)
    git("config", "user.name", "ana", cwd=r)
    sha = commit(r, "signed in sha256", key=keys["ed25519"])
    assert len(sha) == 64
    v = S.verify_commit(r, sha)
    assert v.kind == "signed" and v.fingerprint == keygen_fp(Path(str(keys["ed25519"]) + ".pub")), v
    assert S.verify_commit(r, commit(r, "plain")).kind == "unsigned"


def test_replace_objects_are_ignored(repo, keys):
    """``git replace U S`` must not make the unsigned U verify as S's signer (L3 r2 codex, measured)."""
    u = commit(repo, "unsigned")
    s_ = commit(repo, "signed", key=keys["ed25519"])
    git("replace", u, s_, cwd=repo)
    assert S.verify_commit(repo, u).kind == "unsigned"
    assert S.verify_commit(repo, s_).kind == "signed"


def test_split_signed_unsigned_is_identity(repo):
    sha = commit(repo, "plain")
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    assert S.split_signed(raw) == (raw, None)


def test_split_signed_only_reads_headers_and_only_gpgsig():
    raw = (b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
           b"author a <a@x> 1 +0000\ncommitter a <a@x> 1 +0000\n"
           b"mergetag object abc\n type commit\n"
           b"gpgsig-sha256 -----BEGIN SSH SIGNATURE-----\n AAAA\n -----END SSH SIGNATURE-----\n"
           b"gpgsig line1\n \n line3\n"
           b"\nmessage\ngpgsig not a header\n second\n")
    payload, sig = S.split_signed(raw)
    assert sig == b"line1\n\nline3"
    assert payload == raw.replace(b"gpgsig line1\n \n line3\n", b"")
    assert b"mergetag object abc\n type commit\n" in payload and b"gpgsig-sha256" in payload


def test_split_signed_cr_is_content():
    raw = b"tree t\ngpgsig a\r\n b\nauthor x\n\nmsg\r\n"
    payload, sig = S.split_signed(raw)
    assert sig == b"a\r\nb"
    assert payload == b"tree t\nauthor x\n\nmsg\r\n"


# ---- verify_commit ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["ed25519", "ecdsa"])
def test_signed_commit_names_its_key(repo, keys, kind):
    sha = commit(repo, f"signed by {kind}", key=keys[kind])
    v = S.verify_commit(repo, sha)
    assert v.kind == "signed", v
    assert v.fingerprint == keygen_fp(Path(str(keys[kind]) + ".pub")) == S.fingerprint(pub_line(keys[kind]))


def test_unsigned_commit(repo):
    v = S.verify_commit(repo, commit(repo, "plain"))
    assert (v.kind, v.fingerprint, v.reason) == ("unsigned", None, "no signature")


def test_tampered_commit_is_a_bad_signature(repo, keys):
    sha = commit(repo, "the original message", key=keys["ed25519"])
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    tampered = raw.replace(b"the original message", b"the 0riginal message")
    assert tampered != raw
    bad = write_object(repo, tampered)
    assert bad != sha
    v = S.verify_commit(repo, bad)
    assert v.kind == "unsigned" and v.fingerprint is None and "incorrect signature" in v.reason, v


def test_signer_is_the_key_not_the_email(repo, keys):
    sha = commit(repo, "ana's name, bo's key", key=keys["bo"], author="ana <ana@ex.com>")
    assert "ana@ex.com" in git("log", "-1", "--format=%ae %ce", sha, cwd=repo)
    v = S.verify_commit(repo, sha)
    assert v.kind == "signed"
    assert v.fingerprint == S.fingerprint(pub_line(keys["bo"]))
    assert v.fingerprint != S.fingerprint(pub_line(keys["ed25519"]))


def test_non_ssh_signature_is_unsigned(repo):
    sha = commit(repo, "plain")
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    head, _, body = raw.partition(b"\n\n")
    forged = head + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\n" + body
    v = S.verify_commit(repo, write_object(repo, forged))
    assert (v.kind, v.reason) == ("unsigned", "not an SSH signature")


def test_wrong_namespace_is_not_signed(repo, keys, tmp_path):
    """A signature made for another namespace is a definitive ``unsigned``: ssh-keygen ran and refused it (exit 255).
    Reading it as indeterminate would let anyone who can push halt every clone's judgement."""
    sha = commit(repo, "plain")
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    msg = tmp_path / "payload"
    msg.write_bytes(raw)
    run("ssh-keygen", "-q", "-Y", "sign", "-n", "file", "-f", str(keys["ed25519"]), str(msg))
    sig = (tmp_path / "payload.sig").read_bytes().rstrip(b"\n")
    head, _, body = raw.partition(b"\n\n")
    v = S.verify_commit(repo, write_object(repo, head + b"\ngpgsig " + sig.replace(b"\n", b"\n ") + b"\n\n" + body))
    assert v.kind == "unsigned" and v.definitive, v


@pytest.mark.parametrize("mangle", ["truncate", "garbage", "huge"])
def test_malformed_signature_is_definitively_unsigned_and_cached(repo, keys, tmp_path, mangle):
    """A pushed commit whose gpgsig is truncated, garbage or oversized is unsigned and cached, never indeterminate
    (L3 r22-2 complement H3: indeterminate halts every clone, so a non-member must not be able to cause it)."""
    sha = commit(repo, "signed", key=keys["ed25519"])
    raw = run("git", "cat-file", "commit", sha, cwd=repo).stdout
    payload, sig = S.split_signed(raw)
    lines = sig.split(b"\n")
    body = {"truncate": lines[:2] + [lines[-1]],
            "garbage": [lines[0], b"AAAAgarbage", lines[-1]],
            "huge": [lines[0]] + [base64.b64encode(os.urandom(48))] * 400 + [lines[-1]]}[mangle]
    head, _, rest = payload.partition(b"\n\n")
    bad = write_object(repo, head + b"\ngpgsig " + b"\n ".join(body) + b"\n\n" + rest)
    v = S.verify_commit(repo, bad)
    assert v.kind == "unsigned" and v.definitive, v
    cache = S.SigCache(tmp_path / "c.json")
    assert cache.verify(repo, [bad])[bad].kind == "unsigned"
    assert cache.get(bad) is not None


def test_missing_object_is_indeterminate(repo):
    commit(repo, "plain")
    v = S.verify_commit(repo, "0" * 40)
    assert v.kind == "indeterminate" and "cat-file" in v.reason


def test_bad_object_id_is_refused(repo):
    for bad in ["HEAD", "--help", "abc", "A" * 40, "0" * 41]:
        with pytest.raises(S.SigningError):
            S.verify_commit(repo, bad)


def _without_ssh_keygen(monkeypatch, exc):
    real = subprocess.run

    def fake(argv, *a, **kw):
        if argv and argv[0] == "ssh-keygen":
            raise exc
        return real(argv, *a, **kw)

    monkeypatch.setattr(S.subprocess, "run", fake)


@pytest.mark.parametrize("exc", [FileNotFoundError("ssh-keygen"), subprocess.TimeoutExpired("ssh-keygen", 10)])
def test_no_ssh_keygen_is_indeterminate_and_never_cached(repo, keys, tmp_path, monkeypatch, exc):
    sha = commit(repo, "signed", key=keys["ed25519"])
    plain = commit(repo, "plain")
    cache_path = tmp_path / "cache" / "sigcache.json"
    _without_ssh_keygen(monkeypatch, exc)
    got = S.SigCache(cache_path).verify(repo, [sha, plain])
    assert got[sha].kind == "indeterminate" and got[sha].fingerprint is None
    assert got[plain].kind == "unsigned"  # needs no ssh-keygen, so it is definitive and stored
    stored = json.loads(cache_path.read_text())
    assert stored["schema"] == S.CACHE_SCHEMA
    assert sha not in stored["entries"] and plain in stored["entries"]
    leftovers = [p for p in Path(tempfile.gettempdir()).glob("levain-sig-*")]
    monkeypatch.undo()
    got2 = S.SigCache(cache_path).verify(repo, [sha])
    assert got2[sha].kind == "signed" and got2[sha].fingerprint == S.fingerprint(pub_line(keys["ed25519"]))
    assert json.loads(cache_path.read_text())["entries"][sha] == {"kind": "signed", "fp": got2[sha].fingerprint}
    assert leftovers == []


def test_unrecognised_ssh_keygen_output_is_indeterminate(repo, keys, monkeypatch):
    sha = commit(repo, "signed", key=keys["ed25519"])
    real = subprocess.run

    def fake(argv, *a, **kw):
        if argv and argv[0] == "ssh-keygen":
            return subprocess.CompletedProcess(argv, 0, b"Good-ish signature\n", b"")
        return real(argv, *a, **kw)

    monkeypatch.setattr(S.subprocess, "run", fake)
    v = S.verify_commit(repo, sha)
    assert v.kind == "indeterminate" and "Good-ish" in v.reason


@pytest.mark.parametrize("config", [
    [],
    [("gpg.ssh.allowedSignersFile", "/nonexistent/allowed_signers")],
    [("log.showSignature", "true")],
    [("gpg.ssh.allowedSignersFile", "/nonexistent/allowed_signers"), ("log.showSignature", "true"),
     ("gpg.ssh.program", "/nonexistent/ssh-keygen"), ("gpg.format", "openpgp")],
])
def test_verdicts_ignore_repo_config(repo, keys, config):
    signed = commit(repo, "signed", key=keys["ecdsa"])
    plain = commit(repo, "plain")
    before = {s: S.verify_commit(repo, s) for s in (signed, plain)}
    for k, val in config:
        git("config", k, val, cwd=repo)
    after = {s: S.verify_commit(repo, s) for s in (signed, plain)}
    assert after == before
    assert before[signed].kind == "signed" and before[plain].kind == "unsigned"


# ---- SigCache ---------------------------------------------------------------------------------------------


def test_cache_hits_skip_verification(repo, keys, tmp_path, monkeypatch):
    sha = commit(repo, "signed", key=keys["ecdsa"])
    path = tmp_path / "c.json"
    first = S.SigCache(path).verify(repo, [sha, sha])
    assert first[sha].kind == "signed"
    monkeypatch.setattr(S, "verify_commit", lambda *a, **k: pytest.fail("verified a cached commit"))
    again = S.SigCache(path).verify(repo, [sha])
    assert (again[sha].kind, again[sha].fingerprint, again[sha].reason) == ("signed", first[sha].fingerprint, "cached")


@pytest.mark.parametrize("content", [
    json.dumps({"schema": "sigcache-v0", "entries": {}}),
    json.dumps({"entries": {}}),
    "not json",
    json.dumps([S.CACHE_SCHEMA]),
])
def test_cache_with_wrong_or_missing_tag_reads_empty(repo, keys, tmp_path, content):
    sha = commit(repo, "signed", key=keys["ed25519"])
    path = tmp_path / "c.json"
    data = json.loads(content) if content.startswith(("{", "[")) else None
    if isinstance(data, dict):
        data["entries"] = {sha: {"kind": "unsigned", "fp": None}}  # a lie the tag check must not believe
        content = json.dumps(data)
    path.write_text(content)
    cache = S.SigCache(path)
    assert cache.get(sha) is None
    assert cache.verify(repo, [sha])[sha].kind == "signed"
    assert json.loads(path.read_text())["schema"] == S.CACHE_SCHEMA


def test_cache_drops_malformed_and_indeterminate_entries(tmp_path):
    path = tmp_path / "c.json"
    a, b, c, d = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    path.write_text(json.dumps({"schema": S.CACHE_SCHEMA, "entries": {
        a: {"kind": "indeterminate", "fp": None},
        b: {"kind": "signed", "fp": None},
        c: {"kind": "signed", "fp": "SHA256:abc/+9"},
        d: {"kind": "unsigned", "fp": None},
        "HEAD": {"kind": "unsigned", "fp": None},
    }}))
    cache = S.SigCache(path)
    assert set(cache.entries) == {c, d}
    assert cache.get(a) is None and cache.get(b) is None


def test_cache_write_is_atomic_and_merges(repo, keys, tmp_path):
    s1 = commit(repo, "one", key=keys["ed25519"])
    s2 = commit(repo, "two")
    path = tmp_path / "c.json"
    c1, c2 = S.SigCache(path), S.SigCache(path)
    c1.verify(repo, [s1])
    c2.verify(repo, [s2])  # loaded nothing before c1 wrote; must still keep c1's entry
    entries = json.loads(path.read_text())["entries"]
    assert set(entries) == {s1, s2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json", "repo"]


# ---- signing config ---------------------------------------------------------------------------------------


def test_sign_config_and_env():
    assert S.sign_config("/k.pub") == ["-c", "gpg.format=ssh", "-c", "user.signingkey=/k.pub",
                                       "-c", "commit.gpgsign=true"]
    for bad in ["", "a\nb", "a\rb"]:
        with pytest.raises(S.SigningError):
            S.sign_config(bad)
    env = S.signing_env()
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert os.path.isabs(env["SSH_ASKPASS"]) and os.access(env["SSH_ASKPASS"], os.X_OK)


def test_signing_with_a_passphrase_fails_instead_of_prompting(repo, tmp_path):
    key = keygen(tmp_path, "locked", "-t", "ed25519", passphrase="hunter22")
    (repo / "f.txt").write_text("x")
    git("add", "f.txt", cwd=repo)
    env = {**os.environ, **S.signing_env()}
    env.pop("SSH_AUTH_SOCK", None)  # no agent: the key file itself must be unlocked, and that must not prompt
    cp = subprocess.run(["git", *S.sign_config(str(key) + ".pub"), "commit", "-q", "-m", "x"], cwd=repo, env=env,
                        capture_output=True, stdin=subprocess.DEVNULL, timeout=30, start_new_session=True)
    assert cp.returncode != 0
    assert run("git", "rev-list", "--all", cwd=repo).stdout == b""
