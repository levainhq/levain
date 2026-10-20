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
    text = run("envelope", "echo", thread, "--where", f"{repo}=.", "--land", f"{repo}=main").out
    assert "if Levain later acts on it, it may do only this, nothing outside it." in text
    assert "NOT IN FORCE YET" in text and "exactly this" not in text
    assert f"{repo}  onto main" in text and "the WHOLE repo (every file)" in text
    out = run(*run.signed("envelope", "sign", str(f), "--yes")).out
    assert f"envelope {body['envelope_id']} accepted" in out and f"{thread}: bound (not in force yet)" in out
    assert f"authority today: {NOT_IN_FORCE}" in out and "signed with --yes (no prompt)" in out
    sig = json.loads((tmp_path / "body.json.sig.json").read_text())
    assert set(sig) == {"payload", "signature", "note"} and sig["payload"]["digest"] == got["digest"]
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
    assert f"envelope {body['envelope_id']} accepted" in out and f"{thread}: bound (not in force yet)" in out
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
    got = run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "--yes", "send synthetic words"))
    assert got["envelope"]["taken"] is True and got["envelope"]["refused"] is None
    assert got["words"] == "send synthetic words"
    text = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "--yes", "other synthetic words")).out
    assert "envelope: taken (ev-" in text
    thread = run.json("drops", "split", got["drop_id"], "--thread", "work:x")[0]["thread_id"]
    shown = run("envelope", "list", "--thread", thread).out
    assert got["envelope"]["envelope_id"] in shown and f"authority today: {NOT_IN_FORCE}" in shown


def test_drop_with_envelope_refused_keeps_the_words(run, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    got = run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo, onto="other")), "--yes",
                               "keep these words"), rc=1)
    assert got["envelope"]["taken"] is False
    assert "a land from source names the branch the drop was made from" in got["envelope"]["refused"]
    assert got["words"] == "keep these words"
    text = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo, onto="other")), "--yes", "keep these too"),
               rc=1).out
    assert "envelope: refused: a land from source names the branch" in text and "your words are kept" in text
    assert "keep these words" in run("drops", "show", got["drop_id"]).out


def test_show_prints_not_in_force(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    eid = got["body"]["envelope_id"]
    out = run("envelope", "show", eid, "--data-root", run.root).out
    assert f"authority today: {NOT_IN_FORCE}" in out and "live: bound (not in force yet)" in out
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
    first = run("envelope", "kill", eid).out
    assert "already ended" in first and "(a row was written)" not in first
    out = run("envelope", "show", eid, "--data-root", run.root).out
    assert out.splitlines()[0].startswith("ENDED (killed by you at ") and "it had allowed:" in out
    assert f"authority today: {NOT_IN_FORCE}" in out
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
    assert "--stop" in run("envelope", "echo", thread, "--where", f"{repo}=.", "--stop", "25h", rc=1).err
    assert "--budget" in run("envelope", "echo", thread, "--where", f"{repo}=.", "--budget", "gold=3", rc=1).err
    run("envelope", "echo", "th-" + "0" * 32, "--where", f"{repo}=.", rc=1)          # an unknown thread: exit 1, one line


# --- review r1 fixes -------------------------------------------------------------------------------------------------
@pytest.fixture
def tty(monkeypatch):
    """A terminal whose answer the test sets; the prompts it was shown are kept."""
    from levain.dispatch import cli as dcli

    box = {"answer": "y", "prompts": []}

    def ask(args, prompt):
        box["prompts"].append(prompt)
        return box["answer"]

    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr(dcli, "_ask", ask)
    return box


def test_h1_drop_envelope_asks_y_n_and_needs_yes_without_a_tty(run, repo, tmp_path, monkeypatch, tty):
    from levain.dispatch import cli as dcli

    monkeypatch.chdir(repo)
    sp = str(spec(tmp_path, repo))
    tty["answer"] = "n"
    assert "not confirmed" in run(*run.signed("drop", "--envelope", sp, "words n"), rc=1).err
    assert "no drops" in run("drops", "list").out          # nothing stored on a no
    assert "[y/N]" in tty["prompts"][0]
    tty["answer"] = "y"
    assert run.json(*run.signed("drop", "--envelope", sp, "words y"))["envelope"]["taken"] is True
    monkeypatch.setattr(dcli, "_is_tty", lambda: False)
    err = run(*run.signed("drop", "--envelope", sp, "words none"), rc=1).err
    assert "this process has none" in err and "--yes" in err


def test_h2_signed_at_is_read_after_the_answer(run, repo, tmp_path, monkeypatch, tty):
    from levain.coordinator import cli as ccli

    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    real = ccli._tick
    clock = {"skew": timedelta(minutes=3)}          # the operator read the view 3 minutes before answering
    monkeypatch.setattr(ccli, "_tick", lambda: real() - clock["skew"])

    def slow_answer(args, prompt):
        clock["skew"] = timedelta(0)
        return "y"

    from levain.dispatch import cli as dcli

    monkeypatch.setattr(dcli, "_ask", slow_answer)
    assert "accepted" in run(*run.signed("envelope", "sign", str(f))).out
    # and the same for a drop with an envelope
    clock["skew"] = timedelta(minutes=3)
    monkeypatch.chdir(repo)
    assert run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "slow words"))["envelope"]["taken"]


def test_h3_view_says_not_in_force_and_what_levain_may_do(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    text = run("envelope", "echo", thread, "--where", f"{repo}=.").out
    assert ("NOT IN FORCE YET: until Levain's separate-user setup is in place, signing this lets nothing run by "
            "itself; every land, push and publish still asks you separately.") in text
    assert text.index("NOT IN FORCE YET") < text.index("where it may work")
    out = run(*run.signed("envelope", "sign", str(f), "--yes"))
    assert "NOT IN FORCE YET" in out.out and f"{thread}: live" not in out.out


def test_m1_signer_unavailable_is_a_one_line_exit_1(run, repo, tmp_path, monkeypatch):
    from levain.dispatch import cli as dcli

    _drop, thread = make_thread(run, repo, monkeypatch)
    f, _got = echo(run, repo, thread, tmp_path)

    def broken(sign, message):
        raise dcli.SignerUnavailable("ssh-keygen would not run")

    monkeypatch.setattr(dcli, "_sign_bytes", broken)
    err = run(*run.signed("envelope", "sign", str(f), "--yes"), rc=1).err
    assert err.startswith("levain envelope sign:") and "ssh-keygen would not run" in err and "Traceback" not in err


def test_m3_echo_and_sign_check_the_body_before_a_file_or_a_prompt(run, repo, tmp_path, monkeypatch, tty):
    import shutil

    _drop, thread = make_thread(run, repo, monkeypatch)
    notgit = tmp_path / "plain"
    notgit.mkdir()
    out = tmp_path / "never.json"
    err = run("envelope", "echo", thread, "--where", f"{notgit}=.", "-o", str(out), rc=1).err
    assert "levain envelope echo:" in err and not out.exists()
    f, _got = echo(run, repo, thread, tmp_path)
    monkeypatch.chdir(tmp_path)
    shutil.rmtree(repo)
    err = run(*run.signed("envelope", "sign", str(f)), rc=1).err
    assert "levain envelope sign:" in err and tty["prompts"] == []          # refused before the prompt
    assert not (tmp_path / "body.json.sig.json").exists()


def test_m4_drop_envelope_checks_the_words_before_signing(run, repo, tmp_path, monkeypatch, tty):
    monkeypatch.chdir(repo)
    err = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), " "), rc=1).err
    assert "a drop needs words" in err and tty["prompts"] == []


def test_m5_the_view_shows_the_request_and_refuses_other_words(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch, words="please fix the synthetic thing")
    f, got = echo(run, repo, thread, tmp_path)
    shown = run(*run.signed("envelope", "sign", str(f), "--yes")).out
    assert 'for your words: "please fix the synthetic thing"' in shown and "(from the store, not signed)" in shown
    _drop2, thread2 = make_thread(run, repo, monkeypatch, words="other synthetic ask")
    h = tmp_path / "forged.json"
    body = dict(run.json("envelope", "echo", thread2, "--where", f"{repo}=.")["body"])
    body["words_sha256"] = "sha256:" + "0" * 64
    h.write_text(env_canonical(body) + "\n")
    err = run(*run.signed("envelope", "sign", str(h), "--yes"), rc=1).err
    assert "other words" in err


def env_canonical(body):
    from levain.contracts.canon import canonical

    return canonical(body)


def test_m6_the_prompt_names_what_is_signed(run, repo, tmp_path, monkeypatch, tty, keys):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f)))
    p = tty["prompts"][0]
    assert p.startswith(f"sign {got['body']['envelope_id']} (digest …{got['digest'][-8:]}, 1 thread(s), 1 land(s), stops ")
    assert f"as {keys['phone'].fp} (display-phone)? [y/N] " in p


def test_m7_plain_words(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    err = run("envelope", "echo", thread, "--where", repo, rc=1).err
    assert "REPO=." in err
    text = run("envelope", "echo", thread, "--where", f"{repo}=.", "--land", f"{repo}=main", "--budget",
               "tool_calls=50,hours=1").out
    assert "the WHOLE repo (every file)" in text and "may land with no conditions" in text
    assert "tool calls: 50 allowed" in text and "hours: 1 allowed" in text and "tokens: 0 allowed" in text
    assert text.count("the budget is a gauge") == 1
    assert "stops in 1h 59m" in text or "stops in 2h 0m" in text
    assert " UTC)" in text and "paths: ." not in text


def test_m8_ended_envelopes_and_narrowing_words(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    eid = got["body"]["envelope_id"]
    sooner = utc_stamp(datetime.now(timezone.utc) + timedelta(minutes=30))
    assert "narrowed" in run("envelope", "narrow", eid, "--stop-earlier", sooner).out
    assert "already narrowed" in run("envelope", "narrow", eid, "--stop-earlier", sooner).out
    assert run("envelope", "kill", eid).out.strip().endswith(": ended")
    assert run("envelope", "kill", eid).out.strip().endswith(": already ended")
    shown = run("envelope", "show", eid, "--data-root", run.root).out
    assert shown.splitlines()[0].startswith("ENDED (killed by you") and "it had allowed:" in shown
    assert "(a row was written)" not in shown


def test_m8_a_done_thread_ends_its_envelope(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    run("thread", "state", thread, "working", "--by", "coordinator")
    run("thread", "state", thread, "done", "--by", "coordinator")
    shown = run("envelope", "show", got["body"]["envelope_id"], "--data-root", run.root).out
    assert shown.splitlines()[0].startswith("ENDED (its thread is done, at ")


def test_m9_a_supersede_marks_each_effect_and_says_wider(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    first = got["body"]["envelope_id"]
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    wider = tmp_path / "wider.json"
    out = run("envelope", "echo", thread, "--where", f"{repo}=README,docs/,src/", "--land", f"{repo}=main",
              "--land", f"{repo}=other", "--stop", "2h", "-o", str(wider)).out
    assert f"= unchanged: {repo}  onto main" in out and f"+ added: {repo}  onto other" in out
    assert f"WIDER than {first}: that is why it needs your signature" in out
    narrower = tmp_path / "narrower.json"
    out = run("envelope", "echo", thread, "--where", f"{repo}=README", "--stop", "1h", "-o", str(narrower)).out
    assert f"- removed: {repo}  onto main" in out and f"not wider than {first}" in out
    # the sign view carries the same marks
    assert f"+ added: {repo}  onto other" in run(*run.signed("envelope", "sign", str(wider), "--yes")).out


def test_l1_sign_refuses_a_body_about_to_stop_and_leaves_nothing_on_refusal(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f = tmp_path / "short.json"
    run("envelope", "echo", thread, "--where", f"{repo}=.", "--stop", "4m", "-o", str(f))
    err = run(*run.signed("envelope", "sign", str(f), "--yes"), rc=1).err
    assert "echo again" in err and not (tmp_path / "short.json.sig.json").exists()
    g, _got = echo(run, repo, thread, tmp_path)
    out = run(*run.signed("envelope", "sign", str(g), "--yes"))
    assert (tmp_path / "body.json.sig.json").exists() and "accepted" in out.out


def test_l1_a_refused_acceptance_writes_no_sig_file(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, _got = echo(run, repo, thread, tmp_path)
    err = run("envelope", "sign", str(f), "--yes", "--signing-key", str(run.keys["stranger"].path), "--data-root",
              run.root, rc=1).err
    assert "levain envelope sign:" in err and not (tmp_path / "body.json.sig.json").exists()


def test_l2_output_refuses_an_existing_file_without_force(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f = tmp_path / "keep.json"
    f.write_text("mine")
    assert "already exists" in run("envelope", "echo", thread, "--where", f"{repo}=.", "-o", str(f), rc=1).err
    assert f.read_text() == "mine"
    run("envelope", "echo", thread, "--where", f"{repo}=.", "-o", str(f), "--force")
    assert f.read_text().startswith("{")


@pytest.mark.parametrize("bad", [{"where": 3}, {"lands": 5}, {"pushes": "x"}, {"publishes": {"a": 1}},
                                 {"budget": []}, {"where": [{"repo": "/x", "paths": "README"}]},
                                 {"lands": [{"from": "named", "repo": "/x", "onto": "main", "criteria": 4}]}])
def test_l3_malformed_spec_types_are_refusals_not_tracebacks(run, repo, tmp_path, monkeypatch, bad):
    monkeypatch.chdir(repo)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    err = run(*run.signed("drop", "--envelope", str(p), "--yes", "words"), rc=1).err
    assert err.startswith("levain drop:") and "Traceback" not in err


def test_l6_accept_prints_the_view_and_the_signer(run, repo, tmp_path, monkeypatch, keys):
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    payload = env.sign_payload(got["body"], signer_fp=keys["phone"].fp, signed_at=datetime.now(timezone.utc))
    s = tmp_path / "elsewhere.sig.json"
    s.write_text(json.dumps({"payload": payload, "signature": keys["phone"].sign(env.sign_message(payload),
                                                                                  env.SIGN_NAMESPACE)}))
    out = run("envelope", "accept", str(f), str(s), "--data-root", run.root).out
    assert "NOT IN FORCE YET" in out and f"signed by {keys['phone'].fp} (display-phone)" in out


# C2 --final L3 (g2) -------------------------------------------------------------------------------------------------
@pytest.fixture
def own_log(keys, tmp_path, capsys, qdir):
    """A cockpit of this test's own (the module's is shared), so a key can be revoked mid-prompt."""
    root = tmp_path / "own-cockpit"
    root.mkdir()
    log = Log(root, keys)
    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone"), ("stranger", "display-phone")):
        log.enrol(name, cls)
    return log, Run(capsys, qdir, root, keys)


def test_final_a_key_revoked_while_the_prompt_waits_signs_nothing_into_force(repo, tmp_path, monkeypatch, own_log):
    from levain.dispatch import cli as dcli

    log, run = own_log
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, _got = echo(run, repo, thread, tmp_path)

    revoke = {"key": "phone"}

    def revoke_then_yes(args, prompt):
        log.revoke(revoke["key"])
        return "y"

    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr(dcli, "_ask", revoke_then_yes)
    err = run(*run.signed("envelope", "sign", str(f)), rc=1).err
    assert "revoked" in err
    assert "no envelopes" in run("envelope", "list", "--thread", thread).out
    revoke["key"] = "stranger"          # a key of its own, enrolled when the prompt opened
    got = run.json("drop", "--envelope", str(spec(tmp_path, repo)), "--signing-key", str(run.keys["stranger"].path),
                   "--data-root", run.root, "revoked mid-prompt", rc=1)
    assert got["envelope"]["taken"] is False and "revoked" in got["envelope"]["refused"]
    assert got["words"] == "revoked mid-prompt"


def test_final_drop_envelope_runs_the_disk_checks_before_the_prompt(run, repo, tmp_path, monkeypatch, tty):
    monkeypatch.chdir(repo)
    notrepo = tmp_path / "not-a-repo"
    notrepo.mkdir()
    p = tmp_path / "spec-bad.json"
    p.write_text(json.dumps({"where": [{"repo": str(notrepo), "paths": ["."]}],
                             "lands": [{"repo": str(notrepo), "onto": "main"}]}))          # parses; only disk refuses
    err = run(*run.signed("drop", "--envelope", str(p), "never asked"), rc=1).err
    assert "is not a git work tree's top" in err and tty["prompts"] == []
    assert "no drops" in run("drops", "list").out


def test_final_a_resend_with_a_new_envelope_is_refused_before_signing(run, repo, tmp_path, monkeypatch, tty):
    monkeypatch.chdir(repo)
    first = run.json(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "--outbox-id", "ob-retry-0001",
                                 "same words"))
    assert first["envelope"]["taken"] is True
    tty["prompts"].clear()
    err = run(*run.signed("drop", "--envelope", str(spec(tmp_path, repo)), "--outbox-id", "ob-retry-0001",
                          "same words"), rc=1).err
    assert first["drop_id"] in err and "already delivered" in err and tty["prompts"] == []


def test_final_ctrl_c_at_the_prompt_is_a_refusal(run, repo, tmp_path, monkeypatch):
    from levain.dispatch import cli as dcli

    _drop, thread = make_thread(run, repo, monkeypatch)
    f, _got = echo(run, repo, thread, tmp_path)

    def interrupt(args, prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr(dcli, "_ask", interrupt)
    assert "not confirmed" in run(*run.signed("envelope", "sign", str(f)), rc=1).err


def test_final_budget_takes_ascii_digits_only(run, repo, tmp_path, monkeypatch):
    _drop, thread = make_thread(run, repo, monkeypatch)
    err = run("envelope", "echo", thread, "--where", f"{repo}=.", "--budget", "hours=²", rc=1).err
    assert "--budget is" in err


def test_final_stop_earlier_takes_a_time_a_person_types_or_a_duration(run, repo, tmp_path, monkeypatch):
    # the residue run: `--stop-earlier 2026-10-20T23:06:36Z` was refused, the store wanting its own microsecond form
    _drop, thread = make_thread(run, repo, monkeypatch)
    f, got = echo(run, repo, thread, tmp_path)
    run(*run.signed("envelope", "sign", str(f), "--yes"))
    eid = got["body"]["envelope_id"]
    typed = (datetime.now(timezone.utc) + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run.json("envelope", "narrow", eid, "--stop-earlier", typed)["written"] is True
    assert run.json("envelope", "narrow", eid, "--stop-earlier", "30m")["written"] is True
    stop = run.json("envelope", "show", eid, "--data-root", run.root)["threads"][0]["effective"]["stop_at"]
    left = datetime.fromisoformat(stop) - datetime.now(timezone.utc)
    assert timedelta(minutes=28) < left <= timedelta(minutes=30)
    assert "a time with its zone" in run("envelope", "narrow", eid, "--stop-earlier", "2026-10-20T23:06:36", rc=1).err
