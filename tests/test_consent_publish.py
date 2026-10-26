"""``levain publish`` (levain.consent.publish + publish_gate): what is read, what is uploaded, and who says yes.

The artifacts are built here with the stdlib (a wheel is a zip, an sdist and an npm tarball are gzip tarballs), so
the identity readers run on the real formats. The upload tool is injected: the tests assert on the argv and the
environment the executor would run, never on a real index.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.consent import publish, publish_gate
from levain.consent.queue import _FIELDS, ConsentQueue
from levain.consent.record import (
    RULING_NAMESPACE, ExternalGateRequest, redact_display, ruling_message, tool_input_digest,
)
from tests.test_consent_queue import authority, keys, signed  # noqa: F401 - the shared fixtures
from tests._h4 import h4_home

LOCAL = "http://127.0.0.1:8080/"


@pytest.fixture(autouse=True)
def operator_env(tmp_path, monkeypatch):
    """Every test runs with a scratch HOME (never the operator's ~/.pypirc or ~/.npmrc) and PyPI credentials in the
    environment; the credential tests take them away."""
    user = tmp_path / "userhome"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("TWINE_USERNAME", "__token__")
    monkeypatch.setenv("TWINE_PASSWORD", "pypi-test")
    return user


# --- artifacts ---------------------------------------------------------------------------------------------------

def metadata(name, version):
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: t\n\n".encode()


def wheel(d: Path, name="demo-pkg", version="1.0.0", *, meta=None, file_name=None, extra=()):
    stem = name.replace("-", "_")
    p = d / (file_name or f"{stem}-{version}-py3-none-any.whl")
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(f"{stem}/__init__.py", "")
        zf.writestr(f"{stem}-{version}.dist-info/METADATA", meta if meta is not None else metadata(name, version))
        for member, data in extra:
            zf.writestr(member, data)
    return p


def _tgz(p: Path, members):
    with tarfile.open(p, "w:gz") as tf:
        for m in members:
            if isinstance(m, tarfile.TarInfo):
                tf.addfile(m)
                continue
            name, data = m
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return p


def sdist(d: Path, name="demo-pkg", version="1.0.0", *, top=None, extra=()):
    top = top or f"{name.replace('-', '_')}-{version}"
    return _tgz(d / f"{name.replace('-', '_')}-{version}.tar.gz",
                [(f"{top}/PKG-INFO", metadata(name, version)), (f"{top}/setup.py", b""), *extra])


def npm_tgz(d: Path, name="demo-pkg", version="1.0.0", *, top="package", manifest=None, extra=(), file_name=None):
    body = json.dumps(manifest if manifest is not None else {"name": name, "version": version}).encode()
    return _tgz(d / (file_name or f"{name.replace('@', '').replace('/', '-')}-{version}.tgz"),
                [(f"{top}/package.json", body), (f"{top}/index.js", b""), *extra])


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def art(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    return d


def snaps(home):
    return sorted(p.name for p in (home / "publish").glob("snap-*")) if (home / "publish").is_dir() else []


# --- identity ----------------------------------------------------------------------------------------------------

def test_wheel_and_sdist_identity_from_the_copy(home, art):
    w, s = wheel(art), sdist(art)
    call, snap = publish.snapshot("pypi", [str(s), str(w)], LOCAL, home)
    assert call == {"index": "pypi", "target": LOCAL, "package": "demo-pkg", "version": "1.0.0",
                    "files": sorted([{"name": w.name, "sha256": sha(w), "size": w.stat().st_size},
                                     {"name": s.name, "sha256": sha(s), "size": s.stat().st_size}],
                                    key=lambda f: f["name"])}
    assert stat.S_IMODE(os.lstat(snap).st_mode) == 0o700 and snap.parent == home / "publish"
    assert all(stat.S_IMODE(os.lstat(snap / f["name"]).st_mode) == 0o600 for f in call["files"])


def test_npm_identity_scoped(home, art):
    t = npm_tgz(art, "@acme/thing", "2.0.0-rc.1")
    call, _ = publish.snapshot("npm", [str(t)], "https://registry.npmjs.org", home)
    assert (call["package"], call["version"], call["target"]) == ("@acme/thing", "2.0.0-rc.1",
                                                                  "https://registry.npmjs.org/")


def test_identity_comes_from_the_artifact_not_the_file_name(home, art):
    p = wheel(art, meta=metadata("other-pkg", "1.0.0"))   # the file name says demo-pkg
    with pytest.raises(publish.PublishRefused, match="does not name other-pkg"):
        publish.snapshot("pypi", [str(p)], LOCAL, home)


def _refuse(home, index, paths, match):
    with pytest.raises(publish.PublishRefused, match=match):
        publish.snapshot(index, [str(p) for p in paths], LOCAL, home)
    assert snaps(home) == []                              # nothing is left behind


def test_refused_a_symlink_and_a_directory(home, art, tmp_path):
    real = wheel(art)
    link = tmp_path / real.name
    link.symlink_to(real)
    _refuse(home, "pypi", [link], "a link is refused")
    d = tmp_path / "d" / "x-1.0.0-py3-none-any.whl"
    d.mkdir(parents=True)
    _refuse(home, "pypi", [d], "not a regular file")


def test_refused_over_the_size_cap(home, art, monkeypatch):
    p = wheel(art)
    monkeypatch.setattr(publish, "MAX_ARTIFACT", 10)
    _refuse(home, "pypi", [p], "over 10 bytes")


@pytest.mark.parametrize("bad", ["demo‮pkg", "demo​pkg"])
def test_refused_an_unshowable_name(home, art, bad):
    p = wheel(art, meta=metadata(bad, "1.0.0"), file_name="demo_pkg-1.0.0-py3-none-any.whl")
    _refuse(home, "pypi", [p], "invisible or direction-changing|does not name")


def test_refused_an_unshowable_npm_version(home, art):
    _refuse(home, "npm", [npm_tgz(art, version="1.0.0\u202e", file_name="demo.tgz")], "invisible or direction-changing")


def test_refused_escaping_members(home, art, tmp_path):
    _refuse(home, "pypi", [wheel(art, extra=[("../evil", b"x")])], "climbs out")
    s = tmp_path / "s"
    s.mkdir()
    _refuse(home, "pypi", [sdist(s, extra=[("/abs", b"x")])], "absolute")
    n = tmp_path / "n"
    n.mkdir()
    link = tarfile.TarInfo("package/l")
    link.type, link.linkname = tarfile.SYMTYPE, "../../etc/passwd"
    _refuse(home, "npm", [npm_tgz(n, extra=[link])], "link to")


def test_refused_a_tar_bomb(home, art, monkeypatch):
    p = npm_tgz(art, extra=[("package/big", b"\0" * 200_000)])
    monkeypatch.setattr(publish, "MAX_UNPACKED", 50_000)
    with pytest.raises(publish.PublishRefused, match="unpacks to more than"):
        publish._npm(p, p.name)


def test_refused_shapes(home, art, tmp_path):
    _refuse(home, "pypi", [wheel(art, extra=[("x-1.dist-info/METADATA", metadata("x", "1"))])], "2 top-level")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    _refuse(home, "pypi", [wheel(a), sdist(b, version="1.0.1")], "different projects or versions")
    c = tmp_path / "c"
    c.mkdir()
    _refuse(home, "pypi", [sdist(c, top="elsewhere-1.0.0")], "not a directory of its own name")
    e = tmp_path / "e"
    e.mkdir()
    _refuse(home, "npm", [npm_tgz(e, top="pkg")], "not package/")
    _refuse(home, "npm", [npm_tgz(e, "Bad Name", file_name="bad.tgz")], "not an npm package name")
    z = tmp_path / "z.zip"
    z.write_bytes(b"x")
    _refuse(home, "pypi", [z], "PyPI takes a .whl or a .tar.gz")
    with pytest.raises(publish.PublishRefused, match="exactly one tarball"):
        publish.snapshot("npm", [str(npm_tgz(e)), str(npm_tgz(e, version="2.0.0"))], LOCAL, home)


@pytest.mark.parametrize("url,canon", [
    ("https://upload.pypi.org/legacy/", "https://upload.pypi.org/legacy/"),
    ("HTTPS://Upload.PyPI.org:443/legacy/", "https://upload.pypi.org/legacy/"),
    ("https://registry.npmjs.org", "https://registry.npmjs.org/"),
    ("http://127.0.0.1:4873", "http://127.0.0.1:4873/"),
    ("http://localhost:8080/simple/", "http://localhost:8080/simple/"),
])
def test_canonical_target_accepts(url, canon):
    assert publish.canonical_target(url) == canon


@pytest.mark.parametrize("url,why", [
    ("http://upload.pypi.org/legacy/", "must be https"),
    ("https://u:p@upload.pypi.org/legacy/", "user name or password"),
    ("https://upload.pypi.org/legacy/?x=1", "no query"),
    ("https://upload.pypi.org/legacy/#f", "no query"),
    ("https://upload.pypi.org/le%67acy/", "no query"),
    ("https://uplоad.pypi.org/", "plain ASCII"),           # a Cyrillic o
    ("ftp://host/", "must be https"),
    ("https:///x", "no plain host"),
    ("https://h/a/../b", "not a plain path"),
])
def test_canonical_target_refuses(url, why):
    with pytest.raises(publish.PublishRefused, match=why):
        publish.canonical_target(url)


# --- the executor ------------------------------------------------------------------------------------------------

class Runner:
    """Stands in for subprocess.run: records argv, env and cwd, and what each snapshot file held at that moment."""

    def __init__(self, rc=0, out="", err="", raise_=None):
        self.calls, self.rc, self.out, self.err, self.raise_ = [], rc, out, err, raise_

    def __call__(self, argv, *, env, cwd, **kw):
        files = {a: sha(a) for a in argv if a.startswith(cwd + "/")}
        self.calls.append({"argv": argv, "env": env, "cwd": cwd, "files": files})
        if self.raise_ is not None:
            raise self.raise_
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


def ok(_call):
    return None


def _exec(call, snap, home, runner, refusal=ok, receipts=None):
    return publish.execute(call, snap=snap, levain_home=home, refusal=refusal,
                           receipt=(receipts.append if receipts is not None else lambda r: None),
                           resolve=lambda index: ["/opt/fake/" + ("twine" if index == "pypi" else "npm")], run=runner)


def journal(home, call):
    p = home / "publish" / f"{publish.target_key(call['index'], call['target'], call['package'])}.journal.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_the_upload_sends_the_snapshot_not_the_original(home, art):
    w = wheel(art)
    filed = sha(w)
    call, snap = publish.snapshot("pypi", [str(w)], LOCAL, home)
    wheel(art, meta=metadata("demo-pkg", "1.0.0") + b"Description: changed after asking\n")   # the original moves
    assert sha(w) != filed
    runner = Runner()
    result = _exec(call, snap, home, runner)
    (c,) = runner.calls
    assert result.ok and result.status == "published"
    assert c["argv"] == ["/opt/fake/twine", "upload", "--non-interactive", "--disable-progress-bar",
                         "--repository-url", LOCAL, str(snap / w.name)]
    assert c["files"] == {str(snap / w.name): filed} and c["cwd"] == str(snap)
    assert [j["state"] for j in journal(home, call)] == ["intent", "done"]


def test_a_snapshot_edited_after_filing_is_refused_before_any_upload(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    (snap / call["files"][0]["name"]).write_bytes(b"swapped")
    runner = Runner()
    with pytest.raises(publish.PublishRefused, match="not the file that was allowed"):
        _exec(call, snap, home, runner)
    assert runner.calls == [] and journal(home, call) == []


def test_a_refusal_under_the_lock_stops_before_any_upload(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    runner = Runner()
    with pytest.raises(publish.PublishRefused, match="revoked"):
        _exec(call, snap, home, runner, refusal=lambda c: "a key revoked")
    assert runner.calls == [] and journal(home, call) == []


def test_planted_redirects_do_not_move_the_target(home, art, monkeypatch):
    for k, v in {"TWINE_REPOSITORY_URL": "https://evil/", "TWINE_REPOSITORY": "evil", "TWINE_CONFIG_FILE": "/x",
                 "npm_config_registry": "https://evil/", "NPM_CONFIG_REGISTRY": "https://evil/",
                 "npm_config_@acme:registry": "https://evil/", "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil",
                 "DYLD_INSERT_LIBRARIES": "/evil.dylib", "LD_PRELOAD": "/evil.so", "VIRTUAL_ENV": "/evil",
                 "REQUESTS_CA_BUNDLE": "/evil.pem", "SSL_CERT_FILE": "/evil.pem", "NODE_OPTIONS": "--require /e",
                 "NODE_EXTRA_CA_CERTS": "/evil.pem", "PATH": "/evil/bin:/usr/bin",
                 "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": "pypi-x", "NODE_AUTH_TOKEN": "npm-x",
                 "HTTPS_PROXY": "http://proxy:3128", "HOME": str(home)}.items():
        monkeypatch.setenv(k, v)
    runner = Runner()
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    _exec(call, snap, home, runner)
    t = npm_tgz(art, "@acme/thing")
    ncall, nsnap = publish.snapshot("npm", [str(t)], "http://127.0.0.1:4873", home)
    _exec(ncall, nsnap, home, runner)
    py, npm = runner.calls
    for c in (py, npm):
        env = c["env"]
        assert not [k for k in env if k.upper().startswith(("TWINE_REPOSITORY", "TWINE_CONFIG", "NPM_CONFIG_",
                                                            "PYTHON", "DYLD_", "LD_", "NODE_OPTIONS"))]
        assert not {"VIRTUAL_ENV", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"} & set(env)
        assert env["PATH"] == publish.PUBLISH_PATH and env["HOME"] == str(home)
        assert (env["TWINE_USERNAME"], env["TWINE_PASSWORD"], env["NODE_AUTH_TOKEN"]) == ("__token__", "pypi-x",
                                                                                          "npm-x")
        # both targets are local: no proxy reaches the upload (an https target keeps them; see the m4 test)
        assert "HTTPS_PROXY" not in env and env["NO_PROXY"] == "127.0.0.1,localhost"
        assert env["TWINE_NON_INTERACTIVE"] == "1"
    assert py["argv"][py["argv"].index("--repository-url") + 1] == LOCAL
    assert npm["argv"] == ["/opt/fake/npm", "publish", str(nsnap / t.name), "--registry=http://127.0.0.1:4873/",
                           "--scope=", "--tag=latest", "--access=null", "--dry-run=false", "--strict-ssl=true",
                           "--ignore-scripts", "--proxy=null", "--https-proxy=null", "--noproxy=127.0.0.1,localhost",
                           "--@acme:registry=http://127.0.0.1:4873/"]
    assert npm["cwd"] == str(nsnap)


def test_a_failed_upload_is_reported_with_its_output(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    receipts = []
    result = _exec(call, snap, home, Runner(rc=1, err="HTTPError: 403 Forbidden"), receipts=receipts)
    assert (result.ok, result.status, result.exit, result.detail) == (False, "failed", 1, "HTTPError: 403 Forbidden")
    assert receipts[0]["status"] == "failed" and journal(home, call)[-1]["status"] == "failed"


def test_an_upload_that_does_not_report_is_unknown_and_recorded(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    receipts = []
    with pytest.raises(publish.PublishOutcomeUnknown, match="may or may not"):
        _exec(call, snap, home, Runner(raise_=subprocess.TimeoutExpired("twine", 1)), receipts=receipts)
    assert [j["state"] for j in journal(home, call)] == ["intent", "done"]
    assert journal(home, call)[-1]["status"] == "unknown" == receipts[0]["status"]


def test_no_twine_is_refused_naming_the_install(monkeypatch, tmp_path):
    monkeypatch.setattr(publish.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(publish, "PUBLISH_PATH", str(tmp_path))
    with pytest.raises(publish.PublishRefused, match="install it"):
        publish.resolve_tool("pypi")
    with pytest.raises(publish.PublishRefused, match="install npm"):
        publish.resolve_tool("npm")


def test_a_call_not_in_levains_shape_is_refused(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    for bad in ({**call, "target": "https://Evil.example/"}, {**call, "extra": 1},
                {**call, "files": [{**call["files"][0], "name": "../x"}]}):
        with pytest.raises(publish.PublishRefused):
            _exec(bad, snap, home, Runner())


# --- the gate ----------------------------------------------------------------------------------------------------

@pytest.fixture
def gate(tmp_path, authority, monkeypatch):
    queue = ConsentQueue(tmp_path / "lhome" / "consent", authority=authority, home=h4_home())
    queue.pending()
    runner = Runner()
    monkeypatch.setattr(publish_gate, "_signable", lambda settings: True)
    monkeypatch.setattr(publish_gate, "_queue", lambda qdir, settings: queue)
    monkeypatch.setattr(publish, "execute", functools.partial(
        publish.execute, resolve=lambda index: ["/opt/fake/twine"], run=runner))
    return queue, runner


def _args(queue, files, hold=1.0):
    return argparse.Namespace(index="pypi", files=[str(f) for f in files], target=LOCAL, hold=hold,
                              agent="claude_code", session="s1", queue_dir=queue.directory)


def _answer_when_filed(queue, key, decision):
    def go():
        for _ in range(200):
            pending = [r for r in queue.pending() if r.tool_name == publish_gate.PUBLISH_TOOL]
            if pending:
                payload, sig = signed(queue, pending[0], key, decision)
                queue.answer(pending[0].id, payload, sig)
                return
            time.sleep(0.05)
    t = threading.Thread(target=go)
    t.start()
    return t


def test_gate_a_signed_allow_uploads(gate, art, keys):
    queue, runner = gate
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    assert rc == publish_gate.EXIT_PUBLISHED, out.getvalue()
    assert len(runner.calls) == 1 and "published demo-pkg 1.0.0 to " + LOCAL in out.getvalue()
    receipts = (queue.directory.parent / "publish" / "receipts.jsonl").read_text().splitlines()
    assert json.loads(receipts[0])["signer_fp"] == keys["phone"].fp
    assert snaps(queue.directory.parent) == []           # the snapshot is removed after the run


def test_gate_a_signed_deny_uploads_nothing(gate, art, keys):
    queue, runner = gate
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["presence"], "deny")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    assert (rc, runner.calls) == (publish_gate.EXIT_NOT_TAKEN, []), out.getvalue()
    assert "DENIED" in out.getvalue()


def _undecided(queue, art):
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=0.5), out)
    assert rc == publish_gate.EXIT_UNDECIDED, out.getvalue()
    assert "UNDECIDED" in out.getvalue() and "gap #1" in out.getvalue()
    (g,) = [queue.request_by_gap("claude_code", "s1", 1)]
    return g.request


def test_gate_no_answer_is_undecided_and_uploads_nothing(gate, art):
    queue, runner = gate
    _undecided(queue, art)
    assert runner.calls == []


def test_gate_a_ruled_allow_is_taken_by_the_identical_publish(gate, art, keys):
    queue, runner = gate
    request = _undecided(queue, art)
    payload = queue.ruling_payload(request.id, "allow", keys["phone"].fp)
    queue.answer_late(request.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [art / "demo_pkg-1.0.0-py3-none-any.whl"]), out)
    assert rc == publish_gate.EXIT_PUBLISHED, out.getvalue()
    assert len(runner.calls) == 1 and "your ruling on gap #1" in out.getvalue()


def test_gate_a_late_deny_refuses_without_asking_again(gate, art, keys):
    queue, runner = gate
    request = _undecided(queue, art)
    payload = queue.ruling_payload(request.id, "deny", keys["phone"].fp)
    queue.answer_late(request.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [art / "demo_pkg-1.0.0-py3-none-any.whl"]), out)
    assert (rc, runner.calls) == (publish_gate.EXIT_NOT_TAKEN, []), out.getvalue()
    assert "ruled DENY" in out.getvalue() and queue.pending() == []


def test_gate_a_ruling_on_a_row_whose_display_names_another_publish_never_uploads(gate, art, keys, tmp_path):
    # the push gate's forged-row case: a raw row showing one release but carrying the digest of another
    queue, runner = gate
    real = wheel(art)
    other = tmp_path / "o"
    other.mkdir()
    shown, _ = publish.snapshot("pypi", [str(wheel(other, version="9.9.9"))], LOCAL, queue.directory.parent)
    evil, _ = publish.snapshot("pypi", [str(real)], LOCAL, queue.directory.parent)
    now = datetime.now(timezone.utc)
    forged = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s1", event_id="rand:" + os.urandom(8).hex(),
        tool_name=publish_gate.PUBLISH_TOOL, tool_input_digest=tool_input_digest(evil),
        display=redact_display(publish_gate.PUBLISH_TOOL, shown), cwd=str(art.resolve()), tier=publish_gate.PUBLISH_TIER,
        received_at=utc_stamp(now), expires_at=utc_stamp(now + timedelta(seconds=1)))
    conn = sqlite3.connect(queue.path, isolation_level=None)
    conn.execute(f"INSERT INTO requests ({', '.join(_FIELDS)}) VALUES ({', '.join('?' for _ in _FIELDS)})",
                 tuple(forged.fields()[f] for f in _FIELDS))
    conn.close()
    assert queue.mark_undecided(forged.id, halt=True).state == "undecided"
    payload = queue.ruling_payload(forged.id, "allow", keys["phone"].fp)
    queue.answer_late(forged.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [real]), out)
    assert (rc, runner.calls) == (publish_gate.EXIT_NOT_TAKEN, []), out.getvalue()
    assert "not the publish it was filed for" in out.getvalue()


def test_gate_a_refused_artifact_files_nothing(gate, art):
    queue, runner = gate
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art, meta=metadata("other", "1.0.0"))]), out)
    assert (rc, runner.calls, queue.pending()) == (publish_gate.EXIT_NOT_TAKEN, [], []), out.getvalue()
    assert "refused, nothing was published" in out.getvalue()


# --- L1/L2 round 1 (1019-q20/L1L2_r1.md) -------------------------------------------------------------------------

def _tgz_raw(p: Path, members):
    """A gzip tarball whose member names are written exactly as given (tarfile keeps them verbatim)."""
    return _tgz(p, members)


@pytest.mark.parametrize("shadow", ["package/./package.json", "package//package.json"])
def test_h1_npm_shadow_manifest_is_refused(home, art, shadow):
    # npm / pacote normalise both onto package/package.json and the later one wins: the card would show the first
    shown = json.dumps({"name": "shown-pkg", "version": "1.0.0"}).encode()
    hidden = json.dumps({"name": "hidden-pkg", "version": "9.9.9"}).encode()
    p = _tgz_raw(art / "shown-pkg-1.0.0.tgz", [("package/package.json", shown), (shadow, hidden)])
    _refuse(home, "npm", [p], "empty or '.' segment")


@pytest.mark.parametrize("shadow", ["demo_pkg-1.0.0/./PKG-INFO", "demo_pkg-1.0.0//PKG-INFO"])
def test_h1_sdist_shadow_pkg_info_is_refused(home, art, shadow):
    _refuse(home, "pypi", [sdist(art, extra=[(shadow, metadata("hidden", "6.6"))])], "empty or '.' segment")


@pytest.mark.parametrize("shadow", ["demo_pkg-1.0.0.dist-info/./x", "demo_pkg//x.py", "./demo_pkg/y.py"])
def test_h1_wheel_member_with_an_empty_or_dot_segment_is_refused(home, art, shadow):
    _refuse(home, "pypi", [wheel(art, extra=[(shadow, b"")])], "empty or '.' segment")


@pytest.mark.parametrize("shadow", ["METADATA", "0.dist-info/METADATA.x", "demo_pkg/METADATA"])
def test_m1_wheel_metadata_shadow_is_refused(home, art, shadow):
    # twine reads the shortest member path containing METADATA that holds a Metadata-Version line
    _refuse(home, "pypi", [wheel(art, extra=[(shadow, metadata("hidden", "6.6"))])], "METADATA")


def test_m2_npm_tag_and_access_default_shown_and_pinned(home, art):
    call, snap = publish.snapshot("npm", [str(npm_tgz(art))], "http://127.0.0.1:4873", home)
    assert (call["tag"], call["access"]) == ("latest", "default")
    assert '"tag": "latest"' in redact_display(publish_gate.PUBLISH_TOOL, call)
    runner = Runner()
    _exec(call, snap, home, runner)
    argv = runner.calls[0]["argv"]
    for flag in ("--tag=latest", "--access=null", "--dry-run=false", "--strict-ssl=true", "--ignore-scripts"):
        assert flag in argv, argv
    assert "dist-tag latest" in publish_gate.publish_what(call)


def test_m2_npm_publish_config_tag_and_access_shown_and_pinned(home, art):
    t = npm_tgz(art, manifest={"name": "@acme/thing", "version": "2.0.0",
                               "publishConfig": {"tag": "beta", "access": "public"}}, file_name="t.tgz")
    call, snap = publish.snapshot("npm", [str(t)], "https://registry.npmjs.org/", home)
    assert (call["tag"], call["access"]) == ("beta", "public")
    runner = Runner()
    _exec(call, snap, home, runner)
    argv = runner.calls[0]["argv"]
    assert "--tag=beta" in argv and "--access=public" in argv and "--dry-run=false" in argv


@pytest.mark.parametrize("pc", [{"tag": "bad tag"}, {"access": "everyone"}, {"tag": 3}, "public"])
def test_m2_npm_publish_config_unshowable_is_refused(home, art, pc):
    t = npm_tgz(art, manifest={"name": "demo-pkg", "version": "1.0.0", "publishConfig": pc}, file_name="t.tgz")
    _refuse(home, "npm", [t], "publishConfig")


def test_m3_multi_file_pypi_failure_is_unknown(home, art, tmp_path):
    s = tmp_path / "s"
    s.mkdir()
    call, snap = publish.snapshot("pypi", [str(wheel(art)), str(sdist(s))], LOCAL, home)
    receipts = []
    with pytest.raises(publish.PublishOutcomeUnknown, match="some files may have landed"):
        _exec(call, snap, home, Runner(rc=1, err="HTTPError: 400 Bad Request"), receipts=receipts)
    assert receipts[0]["status"] == "unknown" and journal(home, call)[-1]["status"] == "unknown"


def test_m3_gate_partial_is_exit_4_and_single_failure_says_the_index_may_hold_it(gate, art, keys, tmp_path):
    queue, runner = gate
    runner.rc, runner.err = 1, "HTTPError: 400"
    s = tmp_path / "s"
    s.mkdir()
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art), sdist(s)], hold=20.0), out)
    t.join(10)
    assert rc == publish_gate.EXIT_UNKNOWN, out.getvalue()
    one = tmp_path / "one"
    one.mkdir()
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(one, version="2.0.0")], hold=20.0), out)
    t.join(10)
    assert rc == publish_gate.EXIT_NOT_TAKEN and "may already hold" in out.getvalue(), out.getvalue()


def test_m4_a_local_target_runs_without_the_callers_proxies(home, art, monkeypatch):
    for k in ("HTTP_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://proxy.example:3128")
    runner = Runner()
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    _exec(call, snap, home, runner)
    env = runner.calls[0]["env"]
    assert not [k for k in env if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")]
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost"
    ncall, nsnap = publish.snapshot("npm", [str(npm_tgz(art))], "http://localhost:4873", home)
    _exec(ncall, nsnap, home, runner)
    assert "--proxy=null" in runner.calls[1]["argv"] and "--https-proxy=null" in runner.calls[1]["argv"]
    # an https target keeps them: the operator may need the proxy to reach the index
    hcall, hsnap = publish.snapshot("pypi", [str(art / "demo_pkg-1.0.0-py3-none-any.whl")],
                                    publish.DEFAULT_TARGETS["pypi"], home)
    _exec(hcall, hsnap, home, runner)
    assert runner.calls[2]["env"]["HTTP_PROXY"] == "http://proxy.example:3128"


def _pypirc(user: Path, text: str) -> None:
    (user / ".pypirc").write_text(text)


def test_m5_pypirc_section_for_the_target_supplies_the_credentials(home, art, operator_env, monkeypatch):
    monkeypatch.delenv("TWINE_USERNAME")
    monkeypatch.delenv("TWINE_PASSWORD")
    _pypirc(operator_env, "[distutils]\nindex-servers = pypi local\n\n[pypi]\nusername = __token__\n"
                          "password = pypi-real%x\n\n[local]\nrepository = http://127.0.0.1:8080\n"
                          "username = u\npassword = p\n")
    runner = Runner()
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    _exec(call, snap, home, runner)
    assert (runner.calls[0]["env"]["TWINE_USERNAME"], runner.calls[0]["env"]["TWINE_PASSWORD"]) == ("u", "p")
    assert "--config-file" not in runner.calls[0]["argv"]
    assert publish.pypi_credentials(publish.DEFAULT_TARGETS["pypi"]) == {"TWINE_USERNAME": "__token__",
                                                                         "TWINE_PASSWORD": "pypi-real%x"}


def test_m5_env_credentials_win_over_pypirc(home, art, operator_env):
    _pypirc(operator_env, "[pypi]\nusername = file-user\npassword = file-pass\n")
    runner = Runner()
    call, snap = publish.snapshot("pypi", [str(wheel(art))], publish.DEFAULT_TARGETS["pypi"], home)
    _exec(call, snap, home, runner)
    assert (runner.calls[0]["env"]["TWINE_USERNAME"], runner.calls[0]["env"]["TWINE_PASSWORD"]) == ("__token__",
                                                                                                   "pypi-test")


def test_m5_no_credential_source_is_refused_before_filing(gate, art, operator_env, monkeypatch):
    queue, runner = gate
    monkeypatch.delenv("TWINE_USERNAME")
    monkeypatch.delenv("TWINE_PASSWORD")
    _pypirc(operator_env, "[pypi]\nusername = __token__\npassword = pypi-x\n")   # names pypi.org, not the target
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art)]), out)
    assert (rc, runner.calls, queue.pending()) == (publish_gate.EXIT_NOT_TAKEN, [], []), out.getvalue()
    assert "TWINE_PASSWORD" in out.getvalue() and "pypi-x" not in out.getvalue()
    assert snaps(queue.directory.parent) == []


def test_m5_control_characters_are_stripped_from_the_detail(home, art):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    receipts = []
    result = _exec(call, snap, home, Runner(rc=1, err="\x1b[31mERROR\x1b[0m    403‮ Forbidden\x07"),
                   receipts=receipts)
    assert result.detail == "ERROR    403 Forbidden" == receipts[0]["detail"]


def test_l1_corrupt_and_encrypted_zips_are_refusals_not_tracebacks(home, art, tmp_path):
    p = art / "demo_pkg-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("demo_pkg-1.0.0.dist-info/METADATA", metadata("demo-pkg", "1.0.0") + b"x" * 4000)
    raw = bytearray(p.read_bytes())
    start = raw.index(b"METADATA") + len(b"METADATA")
    for i in range(start + 10, start + 40):
        raw[i] ^= 0xFF
    p.write_bytes(bytes(raw))
    _refuse(home, "pypi", [p], "not a readable wheel")
    e = tmp_path / "e"
    e.mkdir()
    q = wheel(e)
    raw = bytearray(q.read_bytes())          # set the encrypted bit (zipfile will not write one)
    for sig, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = -1
        while (i := raw.find(sig, i + 1)) != -1:
            raw[i + at] |= 0x1
    q.write_bytes(bytes(raw))
    _refuse(home, "pypi", [q], "not a readable wheel")


def test_l2_stale_snapshots_are_swept_and_fresh_ones_kept(home, art):
    (home / "publish").mkdir(mode=0o700)
    stale, fresh = home / "publish" / "snap-stale", home / "publish" / "snap-fresh"
    stale.mkdir(mode=0o700), fresh.mkdir(mode=0o700)
    old = time.time() - publish.SNAP_STALE - 60
    os.utime(stale, (old, old))
    publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    assert not stale.exists() and fresh.exists()


def test_l2_an_interrupt_while_snapshotting_publishes_nothing_and_leaves_nothing(gate, art, monkeypatch):
    queue, runner = gate
    real = publish.snapshot

    def interrupted(*a, **kw):
        real(*a, **kw)
        raise publish_gate._Interrupted("SIGTERM")
    monkeypatch.setattr(publish, "snapshot", interrupted)
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art)]), out)
    assert (rc, runner.calls, queue.pending()) == (publish_gate.EXIT_NOT_TAKEN, [], []), out.getvalue()
    assert "interrupted" in out.getvalue()


def test_l3_a_copy_that_changes_while_its_identity_is_read_is_refused(home, art, monkeypatch):
    real = publish._identity

    def racing(index, path, name):
        got = real(index, path, name)
        with open(path, "ab") as fh:
            fh.write(b"x")
        return got
    monkeypatch.setattr(publish, "_identity", racing)
    _refuse(home, "pypi", [wheel(art)], "changed")


def test_l4_an_interrupt_before_the_intent_line_is_not_published_exit_1(gate, art, keys, monkeypatch):
    queue, runner = gate

    def interrupted_resolve(index):
        raise KeyboardInterrupt
    monkeypatch.setattr(publish, "execute", functools.partial(
        publish.execute.func, resolve=interrupted_resolve, run=runner))
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    assert (rc, runner.calls) == (publish_gate.EXIT_NOT_TAKEN, []), out.getvalue()
    assert "nothing was published" in out.getvalue()


def test_l4_an_interrupt_during_the_upload_is_unknown_exit_4(gate, art, keys):
    queue, runner = gate
    runner.raise_ = KeyboardInterrupt()
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    assert rc == publish_gate.EXIT_UNKNOWN, out.getvalue()
    assert "may or may not" in out.getvalue()


def test_l5_a_file_name_with_a_trailing_newline_is_not_plain(home, art):
    p = wheel(art)
    q = art / (p.name + "\n")
    p.rename(q)
    _refuse(home, "pypi", [q], "not plain")


def test_l6_count_and_size_are_checked_before_anything_is_copied(home, art, tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_copy_in", lambda *a: pytest.fail("copied before the checks"))
    monkeypatch.setattr(publish, "MAX_FILES", 1)
    s = tmp_path / "s"
    s.mkdir()
    _refuse(home, "pypi", [wheel(art), sdist(s)], "at most 1 file")
    monkeypatch.setattr(publish, "MAX_ARTIFACT", 10)
    _refuse(home, "pypi", [art / "demo_pkg-1.0.0-py3-none-any.whl"], "over 10 bytes")


def test_l6_max_files_fits_the_display_whole(home, tmp_path):
    files = []
    for i in range(publish.MAX_FILES):
        d = tmp_path / f"d{i}"
        d.mkdir()
        files.append(str(wheel(d, name="a-very-long-project-name-for-the-card", version="10.20.30rc40",
                               file_name=f"a_very_long_project_name_for_the_card-10.20.30rc40-{i}-cp313-cp313-"
                                         "macosx_14_0_arm64.whl")))
    call, _ = publish.snapshot("pypi", files, LOCAL, home)
    assert len(redact_display(publish_gate.PUBLISH_TOOL, call)) < 2000


def test_gate_a_record_that_cannot_be_written_after_a_publish_is_still_published(gate, art, keys, monkeypatch):
    queue, runner = gate

    def broken(home, extra):
        def write(record):
            raise OSError("disk full")
        return write
    monkeypatch.setattr(publish_gate, "_receipt", broken)
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    assert rc == publish_gate.EXIT_PUBLISHED and "record is incomplete" in out.getvalue(), out.getvalue()


def test_gate_an_interrupt_in_the_hold_withdraws_the_request(gate, art, monkeypatch):
    queue, runner = gate

    def interrupted(*a, **kw):
        raise publish_gate._Interrupted("SIGTERM")
    monkeypatch.setattr(publish_gate._ADAPTERS["claude_code"], "hold_for_answer", interrupted)
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    assert (rc, runner.calls, queue.pending()) == (publish_gate.EXIT_NOT_TAKEN, [], []), out.getvalue()
    assert "withdrawn" in out.getvalue()


def test_the_lock_serialises_two_publishes_of_one_package(home, art, tmp_path):
    other = tmp_path / "o"
    other.mkdir()
    a = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    b = publish.snapshot("pypi", [str(wheel(other))], LOCAL, home)
    live, overlap = [], []

    def slow(argv, **kw):
        live.append(1)
        overlap.append(len(live))
        time.sleep(0.3)
        live.pop()
        return subprocess.CompletedProcess(argv, 0, "", "")
    threads = [threading.Thread(target=_exec, args=(call, snap, home, slow)) for call, snap in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert overlap == [1, 1]


def test_lowercase_npm_config_is_dropped(home, art, monkeypatch):
    monkeypatch.setenv("npm_config_dry_run", "true")
    monkeypatch.setenv("npm_config_tag", "evil")
    runner = Runner()
    call, snap = publish.snapshot("npm", [str(npm_tgz(art))], "http://127.0.0.1:4873", home)
    _exec(call, snap, home, runner)
    assert not [k for k in runner.calls[0]["env"] if k.lower().startswith("npm_config_")]


# --- L3 round 1 (1019-q20/L3_r1_TRIAGE.md) -----------------------------------------------------------------------

def _interrupt_after_intent(monkeypatch, exc):
    """append_line writes the intent line, then the signal lands (the seam between the intent and the upload)."""
    real = publish.append_line

    def append(path, record):
        real(path, record)
        if record.get("state") == "intent":
            raise exc
    monkeypatch.setattr(publish, "append_line", append)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), publish_gate._Interrupted("SIGTERM")])
def test_h_an_interrupt_right_after_the_intent_line_is_unknown(home, art, monkeypatch, exc):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    _interrupt_after_intent(monkeypatch, exc)
    receipts = []
    with pytest.raises(publish.PublishOutcomeUnknown, match="may or may not"):
        _exec(call, snap, home, Runner(), receipts=receipts)
    assert [j["state"] for j in journal(home, call)] == ["intent", "done"]
    assert journal(home, call)[-1]["status"] == "unknown" == receipts[0]["status"]


@pytest.mark.parametrize("seam", ["receipt", "journal"])
@pytest.mark.parametrize("exc", [KeyboardInterrupt(), publish_gate._Interrupted("SIGTERM")])
def test_h_an_interrupt_while_recording_keeps_the_result(home, art, monkeypatch, seam, exc):
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    receipts = []

    def receipt(r):
        if seam == "receipt":
            raise exc
        receipts.append(r)
    if seam == "journal":
        real = publish.append_line

        def append(path, record):
            if record.get("state") == "done":
                raise exc
            real(path, record)
        monkeypatch.setattr(publish, "append_line", append)
    with pytest.raises(publish.PublishRecordIncomplete) as got:
        publish.execute(call, snap=snap, levain_home=home, refusal=ok, receipt=receipt,
                        resolve=lambda index: ["/opt/fake/twine"], run=Runner())
    assert got.value.result.ok and got.value.result.status == "published"
    assert any("interrupted" in e.lower() for e in got.value.record_errors), got.value.record_errors
    if seam == "receipt":     # the done line was written before the signal
        assert journal(home, call)[-1]["status"] == "published"
    else:                     # the receipt is still written after the signal hit the done line
        assert receipts[0]["status"] == "published"


def _gate_run(queue, keys, art):
    out = io.StringIO()
    t = _answer_when_filed(queue, keys["phone"], "allow")
    rc = publish_gate.run(_args(queue, [wheel(art)], hold=20.0), out)
    t.join(10)
    return rc, out.getvalue()


def test_h_gate_an_interrupt_after_the_intent_line_is_exit_4(gate, art, keys, monkeypatch):
    queue, runner = gate
    _interrupt_after_intent(monkeypatch, publish_gate._Interrupted("SIGTERM"))
    rc, out = _gate_run(queue, keys, art)
    assert rc == publish_gate.EXIT_UNKNOWN and "nothing was published" not in out, out


def test_h_gate_an_interrupt_after_the_upload_reports_published(gate, art, keys, monkeypatch):
    queue, runner = gate

    def interrupted(home, extra):
        def write(record):
            raise KeyboardInterrupt
        return write
    monkeypatch.setattr(publish_gate, "_receipt", interrupted)
    rc, out = _gate_run(queue, keys, art)
    assert rc == publish_gate.EXIT_PUBLISHED and len(runner.calls) == 1, out
    assert "record is incomplete" in out and "nothing was published" not in out, out


def test_m_a_password_without_a_username_and_no_section_is_refused_before_filing(gate, art, monkeypatch):
    queue, runner = gate
    monkeypatch.delenv("TWINE_USERNAME")
    out = io.StringIO()
    rc = publish_gate.run(_args(queue, [wheel(art)]), out)
    assert (rc, runner.calls, queue.pending()) == (publish_gate.EXIT_NOT_TAKEN, [], []), out.getvalue()
    assert "TWINE_USERNAME" in out.getvalue() and "pypi-test" not in out.getvalue()


def test_m_a_password_without_a_username_takes_the_sections_username(home, art, operator_env, monkeypatch):
    monkeypatch.delenv("TWINE_USERNAME")
    _pypirc(operator_env, "[local]\nrepository = http://127.0.0.1:8080/\nusername = u\npassword = file-pass\n")
    assert publish.pypi_credentials(LOCAL) == {"TWINE_USERNAME": "u"}
    runner = Runner()
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    _exec(call, snap, home, runner)
    env = runner.calls[0]["env"]
    assert (env["TWINE_USERNAME"], env["TWINE_PASSWORD"]) == ("u", "pypi-test")
    _pypirc(operator_env, "[local]\nrepository = http://127.0.0.1:8080/\npassword = file-pass\n")   # no username
    with pytest.raises(publish.PublishRefused, match="TWINE_USERNAME"):
        publish.pypi_credentials(LOCAL)


def test_m_a_username_without_a_password_takes_the_sections_password(operator_env, monkeypatch):
    monkeypatch.delenv("TWINE_PASSWORD")
    _pypirc(operator_env, "[local]\nrepository = http://127.0.0.1:8080/\nusername = u\npassword = p\n")
    assert publish.pypi_credentials(LOCAL) == {"TWINE_PASSWORD": "p"}
    monkeypatch.delenv("TWINE_USERNAME")
    assert publish.pypi_credentials(LOCAL) == {"TWINE_USERNAME": "u", "TWINE_PASSWORD": "p"}


@pytest.mark.parametrize("repo,target", [("http://127.0.0.1:8080/simple", "http://127.0.0.1:8080/simple/"),
                                         ("http://127.0.0.1:8080/simple/", "http://127.0.0.1:8080/simple")])
def test_m_a_section_matches_only_the_exact_target(operator_env, monkeypatch, repo, target):
    monkeypatch.delenv("TWINE_USERNAME")
    monkeypatch.delenv("TWINE_PASSWORD")
    _pypirc(operator_env, f"[local]\nrepository = {repo}\nusername = u\npassword = p\n")
    with pytest.raises(publish.PublishRefused, match="no complete PyPI credential"):
        publish.pypi_credentials(publish.canonical_target(target))


def test_l_stale_is_one_hour_and_a_live_run_touches_its_snapshot(home, art):
    assert publish.SNAP_STALE == 3600
    call, snap = publish.snapshot("pypi", [str(wheel(art))], LOCAL, home)
    old = time.time() - 2 * publish.SNAP_STALE
    os.utime(snap, (old, old))
    seen = []

    def run(argv, **kw):
        seen.append(os.lstat(snap).st_mtime)
        return subprocess.CompletedProcess(argv, 0, "", "")
    _exec(call, snap, home, run)
    assert seen and seen[0] > time.time() - 60


def test_l_the_total_of_all_inputs_is_capped_before_copying(home, art, tmp_path, monkeypatch):
    s = tmp_path / "s"
    s.mkdir()
    w, sd = wheel(art), sdist(s)
    monkeypatch.setattr(publish, "_copy_in", lambda *a: pytest.fail("copied before the total was checked"))
    monkeypatch.setattr(publish, "MAX_TOTAL", w.stat().st_size + sd.stat().st_size - 1)
    _refuse(home, "pypi", [w, sd], "together")


def test_l_known_edges_a_bare_dot_entry_and_a_metadata_named_file_are_refused(home, art, tmp_path):
    for i, entry in enumerate((".", "./")):
        d = tmp_path / f"n{i}"
        d.mkdir()
        dot = tarfile.TarInfo(entry)
        dot.type = tarfile.DIRTYPE
        _refuse(home, "npm", [npm_tgz(d, extra=[dot])], "member path")
    _refuse(home, "pypi", [wheel(art, extra=[("demo_pkg/METADATA_helpers.py", b"")])], "METADATA")


def test_r2_only_the_halves_the_environment_lacks_can_conflict(operator_env, monkeypatch):
    monkeypatch.delenv("TWINE_USERNAME")
    _pypirc(operator_env, "[a]\nrepository = http://127.0.0.1:8080\nusername = u\npassword = p1\n\n"
                          "[b]\nrepository = http://127.0.0.1:8080\nusername = u\npassword = p2\n")
    assert publish.pypi_credentials(publish.canonical_target("http://127.0.0.1:8080")) == {"TWINE_USERNAME": "u"}


def test_r2_the_total_cap_counts_bytes_actually_copied(home, art, monkeypatch):
    a, b = wheel(art), art / "big.bin"
    monkeypatch.setattr(publish, "MAX_TOTAL", a.stat().st_size + 10)
    b.write_bytes(b"x" * 64)
    left = publish.MAX_TOTAL
    left -= publish._copy_in(str(a), home / "a.whl", min(publish.MAX_ARTIFACT, left))
    try:
        publish._copy_in(str(b), home / "b.bin", min(publish.MAX_ARTIFACT, left))
    except publish.PublishRefused as exc:
        assert "remaining total cap" in str(exc)
    else:
        raise AssertionError("the second file passed the remaining total")
