"""Tenure on real git with real SSH keys: strict ledgers, signed identity, counted team, sync recovery.

Each test names the design row (tenure_design_1005.md r22 "T…", signing doc "S…") or the run that produced it. The
force-push test is the 1007+19 residue run's defect, reproduced before it was fixed.
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
from levain.team import roles as R
from levain.team import tenure as T
from levain.team.transport import WARNINGS, GitLedger, Repo, TeamError

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")
PY = sys.executable


def sh(*args, cwd=None, check=True):
    return subprocess.run(list(args), cwd=cwd, check=check, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    out = {}
    for n in ("ana", "ben", "cy", "mal", "ana2"):
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


def ruling(repo, path, words):
    return team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", path, "--words", words,
                repo=repo)


def in_force(repo):
    g = gl(repo)
    g.sync(push=False)
    return {e["words"] for e in g.ledger().in_force}


def plain_clone(tmp, name, email):
    """A member's PLAIN git clone of the ledger branch: what someone does outside levain."""
    sh("git", "clone", "-q", "-b", "levain-team-ledger", str(tmp / "origin.git"), name, cwd=tmp)
    d = tmp / name
    sh("git", "config", "user.email", email, cwd=d)
    sh("git", "config", "user.name", name, cwd=d)
    return d


# ---- B-1, the branch, the genesis ---------------------------------------------------------------------------


def test_t17_a_0_6_ledger_is_refused_and_init_needs_replace_legacy(tmp_path, keys):
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    a = clone(tmp_path, "a", "ana@ex.com")
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=a)
    sh("git", "checkout", "-q", "--orphan", "levain-ledger", cwd=a)
    (a / "team.toml").write_text('project = "old"\nowner = "ana"\n[members]\nana = "ana@ex.com"\n')
    sh("git", "add", "team.toml", cwd=a)
    sh("git", "commit", "-qm", "0.6.x ledger", cwd=a)
    sh("git", "push", "-q", "origin", "levain-ledger", cwd=a)
    sh("git", "checkout", "-q", "main", cwd=a)
    g = gl(a)
    with pytest.raises(TeamError, match="predates strict mode"):
        g.require_joined()
    with pytest.raises(TeamError, match="predates strict mode"):
        g.init(R.Team("p", "ana", {"ana": "ana@ex.com"}), signing_key=str(keys["ana"]))
    out = g.init(R.Team("p", "ana", {"ana": "ana@ex.com"}), signing_key=str(keys["ana"]), replace_legacy=True)
    assert "strict ledger created" in out
    # the 0.6.x branch is untouched: nothing converted
    assert sh("git", "show", "levain-ledger:team.toml", cwd=a).startswith('project = "old"')
    # BOTH: a joiner reads only the strict ledger (no member key yet, so not a member, but pinned to it)
    b = clone(tmp_path, "b", "ben@ex.com")
    out = GitLedger(Repo.discover(b)).join(remote="origin", signing_key=str(keys["ben"]))
    assert "levain-team-ledger" in out and gl(b).branch == "levain-team-ledger"


def test_s5_a_genesis_not_signed_by_its_own_owner_key_is_refused(tmp_path, keys):
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    a = clone(tmp_path, "a", "ana@ex.com")
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=a)
    sh("git", "checkout", "-q", "--orphan", "levain-team-ledger", cwd=a)
    ten = T.Tenure(keys={"ana": [keys["ana"].read_text().strip()]})
    (a / "team.toml").write_text(R.dump_team(R.Team("p", "ana", {"ana": "ana@ex.com"})))
    (a / "tenure.toml").write_text(T.dump_tenure(ten))
    sh("git", "add", ".", cwd=a)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['mal']}", "commit", "-S", "-qm", "forged genesis",
       cwd=a)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=a)
    b = clone(tmp_path, "b", "ben@ex.com")
    with pytest.raises(TeamError, match="not signed by its own owner"):
        gl(b).join(signing_key=str(keys["ben"]))
    assert gl(b).pinned_root is None


# ---- identity is the key ---------------------------------------------------------------------------------


def test_s2_t3_a_member_signing_a_team_change_with_the_owner_email_is_uncounted(two, keys):
    tmp, ana, ben = two
    p = plain_clone(tmp, "benplain", "ana@ex.com")          # the owner's EMAIL
    gl(ana).sync(push=False)
    t = R.parse_team((p / "team.toml").read_text())
    t.members["mal"] = "mal@ex.com"
    t.owner = "ben"
    (p / "team.toml").write_text(R.dump_team(t))
    g0 = gl(ana)
    g0._dcache = None
    head = g0.derivation().counted_head
    sh("git", "add", "team.toml", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ben']}", "commit", "-S", "-qm",
       f"flip\n\nLevain-Base: {head}", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    for repo in (ana, ben):
        g = gl(repo)
        g.sync(push=False)
        d = g.derivation()
        assert d.team.owner == "ana" and "mal" not in d.team.members
        assert any("not in force" in p_ for p_ in d.problems), d.problems


def test_s3_a_line_is_enforced_by_its_signing_key_not_its_email(two, keys):
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: real") == 0
    p = plain_clone(tmp, "malplain", "ben@ex.com")          # ben's EMAIL, mal's key
    line = json.dumps(E.seal(E.build("ben", "decision", kind="ruling", owner="lead", words="forged as ben",
                                     paths=["src/a.py"]), ""), sort_keys=True)
    f = p / "ledger" / "ben" / "forged.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(line + "\n")
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['mal']}", "commit", "-S", "-qm", "forge", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    words = in_force(ana)
    assert "ben: real" in words and "forged as ben" not in words


# ---- owner reversal, cherry-pick (the residue run's step 5) ------------------------------------------------


def test_t26_a_member_cherry_pick_of_a_reversed_owner_commit_is_uncounted(two, keys):
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    add = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    assert team("member", "remove", "cy", repo=ana) == 0
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    sh("git", "fetch", "-q", str(ana), "levain-team-ledger:refs/remotes/ana/l", cwd=p)
    sh("git", "cherry-pick", add, cwd=p)                    # keeps ana as author; the signature does not survive
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    for repo in (ana, ben):
        g = gl(repo)
        g.sync(push=False)
        assert "cy" not in g.derivation().team.members


# ---- the force-push: the residue run's defect ----------------------------------------------------------------


def test_force_push_is_never_republished_and_the_clone_freezes(two, keys):
    """RESIDUE RUN 1007+19 (residue_run1.txt): after a force-push rewound the remote, ana's sync printed "pushed"
    and the removed history went back up. It must stay removed, be reported, and freeze ana at her anchor."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: keep a") == 0
    assert ruling(ana, "src/b.py", "ana: keep b") == 0
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    before = sh("git", "rev-parse", "levain-team-ledger~1", cwd=p).strip()
    sh("git", "push", "-q", "-f", "origin", f"{before}:levain-team-ledger", cwd=p)
    WARNINGS.clear()
    g = gl(ana)
    g.sync()
    remote = sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0]
    assert remote == before, "the rewound history was re-published"
    assert any("REWRITTEN" in w for w in WARNINGS), WARNINGS
    g._dcache = None
    d = g.derivation()
    assert d.judged == "partial" and "rewritten" in d.frozen_why
    assert {"ana: keep a", "ana: keep b"} <= {e["words"] for e in g.ledger().in_force}   # the anchor's verdicts stand
    g.sync()                                                    # a second sync re-publishes nothing either
    assert sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0] == before


def test_a_pinned_clone_never_recreates_a_deleted_remote_ledger(two):
    tmp, ana, ben = two
    sh("git", "push", "-q", str(tmp / "origin.git"), ":levain-team-ledger", cwd=ana)
    with pytest.raises(TeamError, match="no longer has"):
        gl(ben).sync()
    assert not sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).strip()


# ---- sync moves only own SIGNED commits ----------------------------------------------------------------------


def test_s12_an_unsigned_local_commit_with_my_email_is_never_moved_or_re_signed(two):
    tmp, ana, ben = two
    g = gl(ben)
    f = g.wt / "ledger" / "ben" / "planted.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(E.seal(E.build("ben", "finding", summary="planted"), ""), sort_keys=True) + "\n")
    sh("git", "add", ".", cwd=g.wt)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "planted, unsigned", cwd=g.wt)
    assert ruling(ana, "src/a.py", "ana: newer") == 0      # the remote moves on
    WARNINGS.clear()
    g.sync()
    assert any("NOT moved or re-signed" in w for w in WARNINGS), WARNINGS
    assert "planted" not in sh("git", "log", "--format=%s", "origin/levain-team-ledger", cwd=ben)


# ---- hand-off, offers, G ------------------------------------------------------------------------------------


def test_t1_hand_off_by_offer_and_signed_accept_keeps_the_old_owners_links(two, keys):
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: v1") == 0
    first = gl(ana).ledger().in_force[0]["id"]
    assert team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", "src/a.py", "--words",
                "ana: v2", "--supersedes", first, repo=ana) == 0
    assert team("owner", "ben", "--key", str(keys["ben"]), repo=ana) == 0
    assert team("accept", repo=ben) == 0
    g = gl(ana)
    g.sync(push=False)
    d = g.derivation()
    assert d.team.owner == "ben"
    assert {e["words"] for e in g.ledger().in_force} == {"ana: v2"}   # ana's supersede, made as owner, stays honoured


def test_t48_an_offer_dies_with_the_offerees_membership(two, keys):
    tmp, ana, ben = two
    assert team("owner", "ben", "--key", str(keys["ben"]), repo=ana) == 0
    assert team("member", "remove", "ben", repo=ana) == 0
    assert team("member", "add", "ben", "ben@ex.com", "--key", str(keys["ben"]), repo=ana) == 0
    g = gl(ben)
    g.sync(push=False)
    assert g.derivation().tenure.offer is None
    assert team("accept", repo=ben) == 2


# ---- merges: frozen, then accept-merge --parent --------------------------------------------------------------


def _pull_merge(tmp, ana, ben, keys):
    """ben's plain clone falls behind, records a line, then `git pull --no-rebase` and pushes the merge."""
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    assert ruling(ana, "src/a.py", "ana: after the fork") == 0
    line = json.dumps(E.seal(E.build("ben", "finding", summary="ben on the stale side"), ""), sort_keys=True)
    f = p / "ledger" / "ben" / "stale.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(line + "\n")
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ben']}", "commit", "-S", "-qm", "stale side", cwd=p)
    sh("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "levain-team-ledger", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    return sh("git", "rev-parse", "HEAD", cwd=p).strip()


def test_t9_an_unaccepted_merge_freezes_and_t27_accept_merge_parent_2_follows_the_published_side(two, keys):
    tmp, ana, ben = two
    merge = _pull_merge(tmp, ana, ben, keys)
    g = gl(ana)
    g.sync(push=False)
    d = g.derivation()
    assert d.judged == "partial" and "merge" in d.frozen_why
    # parent 1 is the STALE side (git pull): this clone's anchor is on parent 2 only, so parent 1 is refused
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 2
    assert team("accept-merge", merge, "--parent", "2", repo=ana) == 0
    g._dcache = None
    d = g.derivation()
    assert d.judged == "full"
    assert "ana: after the fork" in {e["words"] for e in g.ledger().in_force}
    # T47: a new owner team change on top of the merge counts (its Levain-Base is computed along walk())
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    g._dcache = None
    assert "cy" in g.derivation().team.members


# ---- revocation ---------------------------------------------------------------------------------------------


def test_s13_a_revoked_key_voids_what_it_signed_after_the_cutoff_on_every_clone(two, keys):
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: before the theft") == 0
    cutoff = sh("git", "rev-parse", "levain-team-ledger", cwd=gl(ben).wt).strip()
    assert ruling(ben, "src/b.py", "thief: after the theft") == 0
    fp = T.key_fps(gl(ben).derivation().tenure, "ben").pop()
    gl(ana).sync(push=False)
    assert team("revoke", fp, "--after", cutoff, repo=ana) == 0
    for repo in (ana, ben):
        words = in_force(repo)
        assert "ben: before the theft" in words and "thief: after the theft" not in words


# ---- re-genesis (Option 2) ---------------------------------------------------------------------------------


def test_s14_s15_regenesis_is_a_fresh_genesis_on_a_new_branch_and_join_makes_you_choose(two, keys):
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: old ruling") == 0
    tip = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    # ben, with a new owner key, re-genesises the team
    g = gl(ben)
    g.sync(push=False)
    g.save_state(signing_key=str(keys["ben"]))
    assert team("regenesis", "--from", tip, "--owner", "ben", "--member", "ben=ben@ex.com",
                "--member", f"ana=ana@ex.com={keys['ana2']}", repo=ben) == 0
    branches = sh("git", "ls-remote", "--heads", str(tmp / "origin.git"), cwd=tmp)
    assert "levain-team-ledger-" in branches
    # the old ledger is untouched
    assert sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0] == tip
    c = clone(tmp, "cy", "cy@ex.com")
    with pytest.raises(TeamError, match="team ledgers"):
        gl(c).join(signing_key=str(keys["cy"]))
    new = [r.split("/")[-1] for r in branches.split() if "levain-team-ledger-" in r][0].rsplit("-", 1)[-1]
    out = gl(c).join(signing_key=str(keys["cy"]), root=new)
    assert "owner in force ben" in out
    # the old history is a READ-ONLY record: nothing from it is enforced on the new ledger
    assert "ana: old ruling" not in {e.get("words") for e in gl(c).ledger().in_force}


# ---- the hook halts when it cannot judge ----------------------------------------------------------------------


def _hook(repo, rel):
    payload = {"session_id": "s1", "transcript_path": "/x", "cwd": str(repo), "hook_event_name": "PreToolUse",
               "tool_name": "Edit", "tool_input": {"file_path": str(repo / rel)}}
    cp = subprocess.run([PY, "-P", "-m", "levain.team.hook", "pretooluse"], input=json.dumps(payload),
                        capture_output=True, text=True, timeout=60, cwd="/",
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout) if cp.stdout.strip() else {}


def test_t8_unjudgeable_denies_and_the_override_allows(two, monkeypatch):
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: governs a") == 0
    g = gl(ana)
    g.save_state(pinned_root="0" * 40)       # this clone can no longer judge (its pin is not the genesis)
    out = _hook(ana, "src/a.py")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "cannot judge" in out["hookSpecificOutput"]["permissionDecisionReason"]
    monkeypatch.setenv("LEVAIN_TEAM_UNJUDGED", "allow")
    out = _hook(ana, "src/a.py")
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
