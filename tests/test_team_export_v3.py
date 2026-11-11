"""Export contract v3, strict profile (seam design spore1344_seam_design_1005.md §3a items 2-7 + prev_root), on real git
with real SSH keys, read back by anneal-memory's own v3 reader.

The anneal side is the reader on anneal-memory origin/main (not released when this was written), so the tests that
need it read ``ANNEAL_V3_SRC``: a checkout of that reader, put on PYTHONPATH for the ``python -P`` subprocesses only.
Without it those tests skip and say why; nothing here imports anneal into the test process.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain.cli import main as levain_main
from levain.team import entry as E
from levain.team import export as X
from levain.team import hook as H
from levain.team.transport import WARNINGS, GitLedger, Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")
PY = sys.executable
ANNEAL = os.environ.get("ANNEAL_V3_SRC", "")
needs_anneal = pytest.mark.skipif(not (ANNEAL and (Path(ANNEAL) / "anneal_memory" / "team.py").is_file()),
                                  reason="set ANNEAL_V3_SRC to a checkout of anneal-memory with the v3 team reader")


# ---- helpers, copied from tests/test_team_tenure.py (not imported from it) ----------------------------------------

def sh(*args, cwd=None, check=True):
    return subprocess.run(list(args), cwd=cwd, check=check, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    out = {}
    for n in ("ana", "ben", "mal"):
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", n, "-f", str(d / n))
        out[n] = d / f"{n}.pub"
    return out


def team(*args, repo):
    return levain_main(["team", *args, "--repo", str(repo)])


def clone(tmp, name, email):
    sh("git", "clone", "-q", str(tmp / "origin.git"), name, cwd=tmp)
    d = tmp / name
    sh("git", "config", "user.email", email, cwd=d)
    sh("git", "config", "user.name", name, cwd=d)
    return d


def gl(repo) -> GitLedger:
    return GitLedger(Repo.discover(repo))


@pytest.fixture
def two(tmp_path, keys):
    """origin + ana (owner) + ben (joined, key confirmed)."""
    WARNINGS.clear()
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    ana = clone(tmp_path, "ana", "ana@ex.com")
    (ana / "src").mkdir()
    (ana / "src" / "a.py").write_text("x = 1\n")
    sh("git", "add", ".", cwd=ana)
    sh("git", "commit", "-qm", "init", cwd=ana)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=ana)
    assert team("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", f"ben=ben@ex.com={keys['ben']}", "--signing-key", str(keys["ana"]), "--no-install",
                repo=ana) == 0
    ben = clone(tmp_path, "ben", "ben@ex.com")
    assert team("join", "--signing-key", str(keys["ben"]), "--no-install", repo=ben) == 0
    return tmp_path, ana, ben


def ruling(repo, path, words, *more):
    return team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", path, "--words", words,
                *more, repo=repo)


def plain_clone(tmp, name, email):
    sh("git", "clone", "-q", "-b", "levain-team-ledger", str(tmp / "origin.git"), name, cwd=tmp)
    d = tmp / name
    sh("git", "config", "user.email", email, cwd=d)
    sh("git", "config", "user.name", name, cwd=d)
    return d


# ---- stream helpers -----------------------------------------------------------------------------------------------

def stream(repo):
    g = gl(repo)
    g.sync(push=False)
    g._dcache = None
    rows = [json.loads(x) for x in X.export_v3(g)]
    return rows[0], rows[1:-1], rows[-1]


def by_words(envs, words):
    got = [e for e in envs if json.loads(e["line"]).get("words") == words]
    assert len(got) == 1, (words, envs)
    return got[0]


def anneal(*args, stdin=None):
    env = dict(os.environ, PYTHONPATH=ANNEAL)
    return subprocess.run([PY, "-P", "-m", "anneal_memory", *args], input=stdin, capture_output=True, text=True,
                          env=env, timeout=120)


def anneal_sets():
    code = "import json, anneal_memory.team as t; print(json.dumps([sorted(t._V3_HEADER), sorted(t._V3_ENVELOPE), " \
           "t._STREAM_END]))"
    cp = subprocess.run([PY, "-P", "-c", code], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=ANNEAL))
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


# ---- the contract's shape -----------------------------------------------------------------------------------------

@needs_anneal
def test_header_envelope_and_trailer_keys_are_exactly_anneals(two):
    _, ana, _ = two
    assert ruling(ana, "src/a.py", "ana: v1") == 0
    head, envs, end = stream(ana)
    header, envelope, end_key = anneal_sets()
    assert sorted(head) == header == sorted(X.V3_HEADER)
    assert envs and all(sorted(e) == envelope for e in envs)
    assert list(end) == [end_key] and end[end_key] == len(envs)
    assert head["anneal_team_stream"] == 3 and head["judged"] == "full"
    for k in ("repin_n", "pos", "seq"):
        assert type(head[k]) is int and 0 <= head[k] < 2 ** 63
    assert head["pos"] >= 1


def test_key_is_clone_local_and_seq_never_regresses(two):
    _, ana, _ = two
    g = gl(ana)
    h1, _, _ = stream(ana)
    g.save_state(anneal_seq=2 ** 62)          # a clock stepped back: seq still moves forward
    h2, _, _ = stream(ana)
    assert h1["key"] == h2["key"] == g.state()["anneal_key"]
    assert h2["seq"] == 2 ** 62 + 1


def test_epoch_is_content_and_repin_n_is_read_from_state(two):
    _, ana, _ = two
    g = gl(ana)
    h1, _, _ = stream(ana)
    assert h1["repin_n"] == 0
    g.save_state(repin_n=2, repair_point=g.state()["anchor"])
    h2, _, _ = stream(ana)
    assert h2["repin_n"] == 2 and h2["epoch"] != h1["epoch"]
    g.save_state(repair_point=None)
    h3, _, _ = stream(ana)
    assert h3["epoch"] == h1["epoch"]          # the same trust content gives the same epoch, whatever the count


def test_prev_root_is_root_except_the_first_export_after_a_repin_root(two):
    _, ana, _ = two
    g = gl(ana)
    head, _, _ = stream(ana)
    assert head["prev_root"] == head["root"] == g.pinned_root
    # `levain team repin --root` moved this clone's pin since its last export under OLD (simulated in state)
    old = "0" * 40
    g.save_state(anneal_prev_root=old)
    moved, _, _ = stream(ana)
    assert moved["root"] == g.pinned_root and moved["prev_root"] == old
    again, _, _ = stream(ana)
    assert again["prev_root"] == old           # still the old one until an export anneal accepted
    snap = X.snapshot(g)
    X.record_exported(g, snap)
    after, _, _ = stream(ana)
    assert after["prev_root"] == after["root"]


# ---- the verdict ----------------------------------------------------------------------------------------------------

def test_a_forged_line_exports_unenforced_with_no_honours(two, keys):
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: real") == 0
    real = next(e for e in gl(ben).ledger().entries if e.get("words") == "ben: real")
    # mal signs a line filed under ben, superseding ben's real ruling (ben's own line would be honoured)
    p = plain_clone(tmp, "malplain", "ben@ex.com")
    line = json.dumps(E.seal(E.build("ben", "decision", kind="ruling", owner="lead", words="forged as ben",
                                     paths=["src/a.py"], supersedes=[real["id"]]), ""), sort_keys=True)
    f = p / "ledger" / "ben" / "forged.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(line + "\n")
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['mal']}", "commit", "-S", "-qm", "forge", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    _, envs, _ = stream(ana)
    forged = by_words(envs, "forged as ben")
    assert forged["enforced"] is False and forged["honours"] == []
    kept = by_words(envs, "ben: real")
    assert kept["enforced"] is True and kept["honours"] == []


def test_an_owner_supersede_of_another_authors_ruling_exports_its_honours(two):
    _, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: v1") == 0
    gl(ana).sync(push=False)
    target = next(e for e in gl(ana).ledger().entries if e.get("words") == "ben: v1")
    assert ruling(ana, "src/a.py", "ana: replaces ben", "--supersedes", target["id"]) == 0
    _, envs, _ = stream(ana)
    linker = by_words(envs, "ana: replaces ben")
    assert linker["enforced"] is True and linker["honours"] == [target["id"]]
    assert by_words(envs, "ben: v1")["honours"] == []
    led = gl(ana).ledger()
    assert led.honoured_pairs == {(target["id"], json.loads(linker["line"])["id"])}
    assert led.superseded == {t for t, _ in led.honoured_pairs}


def test_in_force_view_still_works(two):
    _, ana, _ = two
    assert ruling(ana, "src/a.py", "ana: v1") == 0
    out = X.export_stream(gl(ana).ledger(), in_force=True)
    assert [json.loads(x)["words"] for x in out] == ["ana: v1"]


# ---- round trip through anneal's own reader --------------------------------------------------------------------------

@needs_anneal
def test_round_trip_replaces_for_a_fresh_key(two, tmp_path):
    _, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: v1") == 0
    gl(ana).sync(push=False)
    target = next(e for e in gl(ana).ledger().entries if e.get("words") == "ben: v1")
    assert ruling(ana, "src/a.py", "ana: replaces ben", "--supersedes", target["id"]) == 0
    db = tmp_path / "store.db"
    assert anneal("--db", str(db), "init").returncode == 0
    g = gl(ana)
    g._dcache = None
    body = "".join(X.export_v3(g))
    cp = anneal("--db", str(db), "team-import", "--json", "-", stdin=body)
    data = json.loads(cp.stdout)
    print("ROUND TRIP:", cp.returncode, json.dumps({k: data.get(k) for k in (
        "framing", "snapshot", "imported", "links_added", "links_added_legacy", "links_removed", "unmappable",
        "chain_problems")}))
    assert cp.returncode == 0, cp.stderr
    assert data["framing"] == "v3" and data["snapshot"] == "replaced"
    assert len(data["links_added"]) + len(data["links_added_legacy"]) == 1
    assert data["chain_problems"] == [] and data["unmappable"] == []
    # a replay of the same stream (same key, same pos and seq) changes nothing: stale
    cp2 = anneal("--db", str(db), "team-import", "--json", "-", stdin=body)
    assert json.loads(cp2.stdout)["snapshot"] == "stale_stream"


@needs_anneal
def test_the_sessionstart_seam_imports_once_per_change_and_names_the_counts(two, tmp_path, monkeypatch):
    _, ana, ben = two
    monkeypatch.setenv("PYTHONPATH", ANNEAL)     # the hook's own `python -P` subprocesses see anneal's v3 reader
    assert ruling(ben, "src/a.py", "ben: v1") == 0
    db = tmp_path / "store.db"
    assert anneal("--db", str(db), "init").returncode == 0
    g = gl(ana)
    g.sync(push=False)
    g.save_state(anneal_db=str(db))
    target = next(e for e in g.ledger().entries if e.get("words") == "ben: v1")

    def run():
        g2 = gl(ana)
        _, team_, led = g2.snapshot()
        return H.anneal_import(g2, led, g2.state_hash(led, team_), team_.owner)

    first = run()
    print("SEAM 1:", first)
    assert first is not None and "now match the ledger: 0 added, 0 removed" in first
    assert run() is None                          # unchanged verdict + unchanged store = no import
    assert ruling(ana, "src/a.py", "ana: replaces ben", "--supersedes", target["id"]) == 0
    second = run()
    print("SEAM 2:", second)
    assert second is not None and "1 added, 0 removed" in second
    assert run() is None
    st = json.loads(anneal("--db", str(db), "team-status", "--json").stdout)
    assert [k["key"] for k in st["keys"]] == [gl(ana).state()["anneal_key"]] and st["keys"][0]["owned"] == 1


def test_an_anneal_without_the_v3_reader_keeps_the_v2_path_and_says_so(two, tmp_path, monkeypatch):
    _, ana, _ = two
    monkeypatch.setattr(H, "_anneal_probe", lambda: {"version": "0.9.39", "v3": False})
    sent = {}

    def fake_run(argv, **kw):
        sent["argv"], sent["body"] = argv, kw.get("input", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    db = tmp_path / "store.db"
    db.write_text("")
    g = gl(ana)
    g.save_state(anneal_db=str(db))
    _, team_, led = g.snapshot()
    tree = g.state_hash(led, team_)
    monkeypatch.setattr(H.subprocess, "run", fake_run)     # after every git call: only the import is faked
    note = H.anneal_import(g, led, tree, team_.owner)
    assert "--link-authority" in sent["argv"]
    assert json.loads(sent["body"].splitlines()[0]) == {"anneal_team_stream": 2}
    assert note is not None and "anneal-memory 0.9.39 keeps links as first imported; upgrade to" in note
