"""Coordinator C2b: ``levain envelope`` and ``levain drop --envelope``, driven in-process through the real argparse entry
(``levain.cli.main``) the way C1's CLI tests drive ``levain drop``. Keys are real ssh-keygen keys enrolled in a real signed
authority log at the ``--data-root`` the CLI reads; repos are real scratch git repos. Synthetic words."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.coordinator import envelope as env
from levain.coordinator.envelopes import NOT_IN_FORCE
from tests.test_consent_queue import Key, Log


def git(*a: str) -> str:
    return subprocess.run(["git", "-c", "user.email=r@x", "-c", "user.name=r", *a], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("envcli-keys")
    return {n: Key(d, n) for n in ("laptop", "phone", "presence", "revoked", "stranger")}


@pytest.fixture(scope="module")
def data_root(keys, tmp_path_factory):
    root = tmp_path_factory.mktemp("envcli-cockpit")
    log = Log(root, keys)
    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone")):
        log.enrol(name, cls)
    return root


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    real = os.path.realpath(r)
    git("init", "-q", "-b", "main", real)
    (r / "README").write_text("scratch\n")
    git("-C", real, "add", "README")
    git("-C", real, "commit", "-qm", "init")
    git("-C", real, "branch", "other")
    return real


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return str(d)


class Run:
    def __init__(self, capsys, qdir, data_root, keys):
        self.capsys, self.q, self.root, self.keys = capsys, qdir, str(data_root), keys

    def __call__(self, *argv, rc=0):
        got = main([*argv, "--queue-dir", self.q])
        out = self.capsys.readouterr()
        assert got == rc, (argv, out.out, out.err)
        return out

    def json(self, *argv, **kw):
        return json.loads(self(*argv, "--json", **kw).out)

    def signed(self, *argv):
        return [*argv, "--signing-key", str(self.keys["phone"].path), "--data-root", self.root]


@pytest.fixture
def run(capsys, qdir, data_root, keys):
    return Run(capsys, qdir, data_root, keys)


def make_thread(run, repo, monkeypatch, words="synthetic words one"):
    monkeypatch.chdir(repo)
    drop_id = run.json("drop", words)["drop_id"]
    thread = run.json("drops", "split", drop_id, "--thread", "work:fix the thing")[0]["thread_id"]
    return drop_id, thread


def echo(run, repo, thread, tmp_path, *extra):
    f = tmp_path / "body.json"
    got = run.json("envelope", "echo", thread, "--where", f"{repo}=README,docs/", "--land", f"{repo}=main",
                   "--stop", "2h", "--budget", "tool_calls=50,hours=1", "-o", str(f), *extra)
    return f, got


def test_echo_sign_yes_accepted_and_live(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    body = env.parse_body(f.read_text().rstrip("\n"))
    assert body == got["body"] and body["origin"] == "worded" and body["supersedes"] is None
    assert body["binds"][0]["thread_id"] == thread and body["where"] == [{"repo": repo, "paths": ["README", "docs/"]}]
    assert body["lands"] == [{"repo": repo, "onto": "main", "from": "named", "onto_from": "inferred", "criteria": []}]
    assert body["budget"] == {"tool_calls": 50, "tokens": 0, "dollars_cents": 0, "hours": 1}
    text = run("envelope", "echo", thread, "--where", repo, "--land", f"{repo}=main").out
    assert "signing this lets the coordinator do exactly this; nothing outside it." in text
    assert f"{repo}  onto main" in text and "paths: ." in text
    out = run(*run.signed("envelope", "sign", str(f), "--yes")).out
    assert f"envelope {body['envelope_id']} accepted" in out and f"{thread}: live" in out
    assert f"authority today: {NOT_IN_FORCE}" in out
    sig = json.loads((tmp_path / "body.json.sig.json").read_text())
    assert set(sig) == {"payload", "signature"} and sig["payload"]["digest"] == got["digest"]
    # the exact retry is `accept` with the sig file; a second `sign` makes a new signature, which the store refuses
    sigfile = tmp_path / "body.json.sig.json"
    assert run.json("envelope", "accept", str(f), str(sigfile), "--data-root", run.root)["created"] is False
    assert "is already accepted; there is nothing to sign" in run(*run.signed("envelope", "sign", str(f), "--yes"), rc=1).err


def test_sign_without_yes_on_a_non_tty_refuses(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    out = run(*run.signed("envelope", "sign", str(f)), rc=1)
    assert "this process has none" in out.err and "--yes" in out.err
    assert not (tmp_path / "body.json.sig.json").exists()
    show = run("envelope", "list", "--thread", thread).out
    assert "no envelopes" in show


def test_sign_asks_y_n_on_a_terminal(run, repo, tmp_path, monkeypatch):
    from levain.dispatch import cli as dcli

    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr(dcli, "_ask", lambda args, prompt: "n")
    assert "not confirmed" in run(*run.signed("envelope", "sign", str(f)), rc=1).err
    assert "no envelopes" in run("envelope", "list", "--thread", thread).out
    monkeypatch.setattr(dcli, "_ask", lambda args, prompt: "y")
    assert "accepted" in run(*run.signed("envelope", "sign", str(f))).out


def test_accept_from_a_sig_file(run, repo, tmp_path, monkeypatch, keys):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    body = got["body"]
    payload = env.sign_payload(body, signer_fp=keys["phone"].fp, signed_at=datetime.now(timezone.utc))
    sig = keys["phone"].sign(env.sign_message(payload), env.SIGN_NAMESPACE)
    s = tmp_path / "elsewhere.sig.json"
    s.write_text(json.dumps({"payload": payload, "signature": sig}))
    out = run("envelope", "accept", str(f), str(s), "--data-root", run.root).out
    assert f"envelope {body['envelope_id']} accepted" in out and f"{thread}: live" in out
    bad = tmp_path / "bad.sig.json"
    bad.write_text(json.dumps({"payload": payload, "signature": "garbage"}))
    got2 = run("envelope", "accept", str(f), str(bad), "--data-root", run.root, rc=1)
    assert got2.err.startswith("levain envelope accept:")


def spec(tmp_path, repo, **land):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"where": [{"repo": repo, "paths": ["README"]}], "lands": [{"from": "source", **land}],
                             "budget": {"tool_calls": 10}, "stop": "90m"}))
    return p


def test_drop_with_envelope_taken(run, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    got = run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "send synthetic words"))
    assert got["envelope"]["taken"] is True and got["envelope"]["refused"] is None
    assert got["words"] == "send synthetic words"
    text = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "other synthetic words")).out
    assert "envelope: taken (ev-" in text
    thread = run.json("drops", "split", got["drop_id"], "--thread", "work:x")[0]["thread_id"]
    shown = run("envelope", "list", "--thread", thread).out
    assert got["envelope"]["envelope_id"] in shown and f"authority today: {NOT_IN_FORCE}" in shown


def test_drop_with_envelope_refused_keeps_the_words(run, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    got = run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo, onto="other")), "keep these words"))
    assert got["envelope"]["taken"] is False
    assert "a land from source names the branch the drop was made from" in got["envelope"]["refused"]
    assert got["words"] == "keep these words"
    text = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo, onto="other")), "keep these too")).out
    assert "envelope: refused: a land from source names the branch" in text
    assert "keep these words" in run("drops", "show", got["drop_id"]).out


def test_show_prints_not_in_force(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    eid = got["body"]["envelope_id"]
    out = run("envelope", "show", eid, "--data-root", run.root).out
    assert f"authority today: {NOT_IN_FORCE}" in out and "live: live" in out
    assert "land identity: lands:sha256:" in out
    data = run.json("envelope", "show", eid, "--data-root", run.root)
    assert data["threads"][0]["authority_today"] == NOT_IN_FORCE and data["threads"][0]["live"] == "live"
    nokey = run("envelope", "show", eid).out          # no cockpit named: the live line says so, the rest still prints
    assert "live: not checked" in nokey and f"authority today: {NOT_IN_FORCE}" in nokey


def test_kill_twice_written_then_not(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    eid = got["body"]["envelope_id"]
    assert run.json("envelope", "kill", eid)["written"] is True
    assert run.json("envelope", "kill", eid)["written"] is False
    assert "nothing was written" in run("envelope", "kill", eid).out
    out = run("envelope", "show", eid, "--data-root", run.root).out
    assert "was killed" in out and f"authority today: {NOT_IN_FORCE}" in out
    run("envelope", "kill", "ev-" + "0" * 32, rc=1)


def test_narrow_stop_earlier_and_drop_effect(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    body = got["body"]
    eid = body["envelope_id"]
    sooner = utc_stamp(datetime.now(timezone.utc) + timedelta(minutes=30))
    assert run.json("envelope", "narrow", eid, "--stop-earlier", sooner)["written"] is True
    assert run.json("envelope", "narrow", eid, "--stop-earlier", sooner)["written"] is False
    later = utc_stamp(datetime.now(timezone.utc) + timedelta(hours=5))
    assert "widening" in run("envelope", "narrow", eid, "--stop-earlier", later, rc=1).err
    shown = run.json("envelope", "show", eid, "--data-root", run.root)
    assert shown["threads"][0]["effective"]["stop_at"] == sooner
    ident = env.effect_identity("lands", body["lands"][0])
    assert run.json("envelope", "narrow", eid, "--drop-effect", ident, "--thread", thread)["written"] is True
    assert run.json("envelope", "show", eid, "--data-root", run.root)["threads"][0]["effective"]["lands"] == []
    assert "identity the signed envelope holds" in run("envelope", "narrow", eid, "--drop-effect", "lands:sha256:0",
                                                       rc=1).err


def test_echo_refuses_bad_input(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    assert "--stop" in run("envelope", "echo", thread, "--where", repo, "--stop", "25h", rc=1).err
    assert "--budget" in run("envelope", "echo", thread, "--where", repo, "--budget", "gold=3", rc=1).err
    run("envelope", "echo", "th-" + "0" * 32, "--where", repo, rc=1)          # an unknown thread: exit 1, one line
