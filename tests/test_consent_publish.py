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

LOCAL = "http://127.0.0.1:8080/"


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
        assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["TWINE_NON_INTERACTIVE"] == "1"
    assert py["argv"][py["argv"].index("--repository-url") + 1] == LOCAL
    assert npm["argv"] == ["/opt/fake/npm", "publish", str(nsnap / t.name), "--registry=http://127.0.0.1:4873/",
                           "--scope=", "--ignore-scripts", "--@acme:registry=http://127.0.0.1:4873/"]
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
    queue = ConsentQueue(tmp_path / "lhome" / "consent", authority=authority)
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
