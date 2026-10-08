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
from levain.team import signing as S
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


def test_r2_a_merge_in_a_rewritten_history_does_not_hide_the_rewrite(two, keys):
    """Code L3 r2 complement HIGH, RUN: the host rewrites the ledger to an older prefix plus a merge. The merge loop
    overwrote the "rewritten" sentinel, so the clone judged the rewritten prefix (dropping a later ruling) and blamed
    the merge. It must freeze at its anchor as a rewrite, with the anchor's verdicts."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: keep a") == 0
    assert ruling(ana, "src/b.py", "ana: keep b") == 0
    g = gl(ana)
    g.sync(push=False)
    p = plain_clone(tmp, "hostplain", "host@ex.com")
    sh("git", "reset", "-q", "--hard", "levain-team-ledger~1", cwd=p)
    sh("git", "checkout", "-q", "-b", "side", cwd=p)
    (p / "x.txt").write_text("x\n")
    sh("git", "add", "x.txt", cwd=p)
    sh("git", "commit", "-qm", "side", cwd=p)
    sh("git", "checkout", "-q", "levain-team-ledger", cwd=p)
    sh("git", "merge", "-q", "--no-ff", "--no-edit", "side", cwd=p)
    sh("git", "push", "-q", "-f", "origin", "levain-team-ledger", cwd=p)
    g.sync(push=False)
    g._dcache = None
    d = g.derivation()
    assert d.judged == "partial" and "rewritten" in d.frozen_why, d.frozen_why
    assert {"ana: keep a", "ana: keep b"} <= {e["words"] for e in g.ledger().in_force}


def test_r2_an_anchor_kept_only_as_a_merges_second_parent_keeps_the_anchors_verdicts(two, keys):
    """The same class with the anchor still reachable: the host's first-parent chain drops a ruling and a merge brings
    the old tip in as parent 2. A freeze AT the merge judged the host's prefix; the verdicts must be the anchor's."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: keep a") == 0
    assert ruling(ana, "src/b.py", "ana: keep b") == 0
    g = gl(ana)
    g.sync(push=False)
    p = plain_clone(tmp, "hostplain", "host@ex.com")
    old = sh("git", "rev-parse", "levain-team-ledger", cwd=p).strip()
    sh("git", "reset", "-q", "--hard", "levain-team-ledger~1", cwd=p)
    (p / "x.txt").write_text("x\n")
    sh("git", "add", "x.txt", cwd=p)
    sh("git", "commit", "-qm", "host side", cwd=p)
    sh("git", "merge", "-q", "--no-ff", "--no-edit", old, cwd=p)
    sh("git", "push", "-q", "-f", "origin", "levain-team-ledger", cwd=p)
    g.sync(push=False)
    g._dcache = None
    d = g.derivation()
    assert d.judged == "partial" and "merge" in d.frozen_why, d.frozen_why
    assert {"ana: keep a", "ana: keep b"} <= {e["words"] for e in g.ledger().in_force}


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
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=ben) == 0
    g = gl(ana)
    g.sync(push=False)
    d = g.derivation()
    assert d.team.owner == "ben"
    assert {e["words"] for e in g.ledger().in_force} == {"ana: v2"}   # ana's supersede, made as owner, stays honoured


def test_t48_an_offer_dies_with_the_offerees_membership(two, keys):
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    assert team("member", "remove", "ben", repo=ana) == 0
    assert team("member", "add", "ben", "ben@ex.com", "--key", str(keys["ben"]), repo=ana) == 2   # a retired handle
    g = gl(ben)
    g.sync(push=False)
    assert g.derivation().tenure.offer is None
    assert team("accept", repo=ben) == 2


def test_owner_impersonation_a_key_the_owner_proposes_for_an_existing_member_never_comes_into_force(two, keys):
    """1007+25's traced path, RUN: the owner proposes a key she holds into ben's pending and confirms it herself.
    The CLI refuses; the same commits written past the CLI are refused by the derivation, and a line signed with that
    key under ben's folder is not enforced."""
    from levain.team import signing as S
    from levain.team import tenure as T
    tmp, ana, ben = two
    assert team("key", "add", "ben", str(keys["ana2"]), repo=ana) == 2         # the CLI refuses the owner
    g = gl(ana)
    line = Path(keys["ana2"]).read_text().strip()
    g.update_counted(lambda t, n: n.pending_keys.setdefault("ben", []).append(line),
                     "forged: propose ana2 for ben", push=False, _check=False)
    g._dcache = None
    assert S.fingerprint(line) not in {T._fp_or_none(x) for x in g.derivation().tenure.pending_keys.get("ben", [])}
    assert S.fingerprint(line) not in T.key_fps(g.derivation().tenure, "ben")


def test_owner_impersonation_an_accept_needs_a_key_already_the_offerees(two, keys):
    """The offer names a member, not a key: an accept signed by a key the owner chose (ana2) does not move ownership,
    and a ``keys`` list in a hand-written offer is dropped at parse."""
    from levain.team import tenure as T
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    g = gl(ana)
    g.save_state(signing_key=str(keys["ana2"]))
    assert team("accept", repo=ana) == 2                                          # ana2 holds no key of ben's

    def forged(t, n) -> None:
        t.owner = "ben"
        n.offer = None
    g.update_counted(forged, "forged: accept as ben with ana2", push=False, _check=False)
    g._dcache = None
    assert g.derivation().team.owner == "ana"
    parsed = T.parse_tenure('rules = 1\noffer = { handle = "ben", email = "b@x", keys = ["ssh-ed25519 AAAA x"] }\n')
    assert parsed.offer == {"handle": "ben", "email": "b@x"}
    assert team("accept", repo=ben) == 0                                          # ben's own in-force key: accepted
    gb = gl(ben)
    gb.sync(push=False)
    assert gb.derivation().team.owner == "ben"


def test_the_owner_proposes_only_a_members_first_key(two, keys):
    """A member with no key yet: the owner proposes it (after the add, too); once that key is in force, only the
    member proposes, and the owner's proposal written past the CLI is refused by the derivation."""
    from levain.team import signing as S
    from levain.team import tenure as T
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", repo=ana) == 0                  # no key yet
    assert team("key", "add", "cy", str(keys["cy"]), repo=ana) == 0                 # the owner: cy's first key
    cy = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=cy) == 0
    g = gl(ana)
    g.sync(push=False)
    g._dcache = None
    d = g.derivation()
    assert S.fingerprint(Path(keys["cy"]).read_text()) in T.key_fps(d.tenure, "cy") and "cy" in d.ever_keyed
    assert team("key", "add", "cy", str(keys["mal"]), repo=ana) == 2                # cy has a key now: refused
    assert team("member", "add", "dee", "dee@ex.com", "--key", str(keys["ana2"]), repo=ana) == 0
    assert team("key", "add", "dee", str(keys["mal"]), repo=ana) == 2               # dee's own key is pending
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["mal"]), repo=ana) == 2
    mal = Path(keys["mal"]).read_text().strip()
    g.update_counted(lambda t, n: n.pending_keys.setdefault("cy", []).append(mal), "forged: mal for cy", push=False, _check=False)
    g._dcache = None
    assert "cy" not in g.derivation().tenure.pending_keys


def test_a_removed_handle_is_never_re_added(two, keys):
    """remove + re-add under the same handle would give the owner a key signing as a handle that signed before (L1 W1,
    L2 A1, 1007+25): the CLI refuses it, and the same commit written past the CLI is refused by the derivation."""
    tmp, ana, ben = two
    assert team("member", "remove", "ben", repo=ana) == 0
    assert team("member", "add", "ben", "ben@ex.com", "--key", str(keys["ana2"]), repo=ana) == 2
    g = gl(ana)
    ana2 = Path(keys["ana2"]).read_text().strip()

    def readd(t, n) -> None:
        t.members["ben"] = "ben@ex.com"
        n.pending_keys["ben"] = [ana2]
    g.update_counted(readd, "forged: re-add ben", push=False, _check=False)
    g._dcache = None
    d = g.derivation()
    assert "ben" not in d.team.members and "ben" not in d.tenure.pending_keys and d.retired("ben")
    assert team("member", "add", "ben2", "ben@ex.com", "--key", str(keys["ana2"]), repo=ana) == 0   # a new handle


def test_r2_a_revoked_key_never_comes_back_and_an_owner_key_is_never_revoked(two, keys):
    """Code L3 r2 codex HIGHs, RUN: ben's remaining key re-proposed a key the owner had revoked and it confirmed itself
    back into force; an owner's revoke of her own (stolen) key landed the key removal, lost the revocation, and printed
    success. A refused field now rolls the whole change back with an error."""
    from levain.team import signing as S
    from levain.team import tenure as T
    tmp, ana, ben = two
    gb = gl(ben)
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    gb.save_state(signing_key=str(keys["mal"]))
    assert team("key", "confirm", repo=ben) == 0
    gb.save_state(signing_key=str(keys["ben"]))
    mal_fp = S.fingerprint(Path(keys["mal"]).read_text())
    cutoff = sh("git", "rev-parse", "levain-team-ledger", cwd=gl(ana).wt).strip()
    assert team("revoke", mal_fp, "--after", cutoff, repo=ana) == 0
    gb = gl(ben)
    gb.sync(push=False)
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 2           # rolled back: it would not count
    mal = Path(keys["mal"]).read_text().strip()
    gb.update_counted(lambda t, n: n.pending_keys.setdefault("ben", []).append(mal), "forged: re-propose mal",
                      push=False, _check=False)
    gb._dcache = None
    assert "ben" not in gb.derivation().tenure.pending_keys
    assert mal_fp not in T.key_fps(gb.derivation().tenure, "ben")
    # an owner key: refused up front, nothing written
    ga = gl(ana)
    assert team("key", "add", "ana", str(keys["ana2"]), repo=ana) == 0
    ga.save_state(signing_key=str(keys["ana2"]))
    assert team("key", "confirm", repo=ana) == 0
    ga.save_state(signing_key=str(keys["ana"]))
    head = sh("git", "rev-parse", "levain-team-ledger", cwd=ga.wt).strip()
    ana2_fp = S.fingerprint(Path(keys["ana2"]).read_text())
    assert team("revoke", ana2_fp, "--after", cutoff, repo=ana) == 2
    assert sh("git", "rev-parse", "levain-team-ledger", cwd=ga.wt).strip() == head
    ga._dcache = None
    assert ana2_fp in T.key_fps(ga.derivation().tenure, "ana")


def test_r2_a_change_with_a_refused_field_writes_nothing(two, keys):
    """complement r2 LOW: `member add --key` whose key is already another handle's printed success and dropped the key."""
    tmp, ana, ben = two
    g = gl(ana)
    head = sh("git", "rev-parse", "levain-team-ledger", cwd=g.wt).strip()
    ben_line = Path(keys["ben"]).read_text().strip()

    def dup(t, n) -> None:
        t.members["cy"] = "cy@ex.com"
        n.pending_keys["cy"] = [ben_line]
    with pytest.raises(TeamError, match="would not count"):
        g.update_counted(dup, "member add cy with ben's key", push=False)
    assert sh("git", "rev-parse", "levain-team-ledger", cwd=g.wt).strip() == head
    g._dcache = None
    assert "cy" not in g.derivation().team.members


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


def test_r2_an_offline_team_change_held_by_a_freeze_re_lands_after_accept_merge(two, keys):
    """Code L3 r2 codex HIGH + complement MED, RUN: an offline owner change stripped while a merge froze the clone was
    held in pending_ops, and every later sync took a fast-forward or already-on-top return that skipped the re-land."""
    tmp, ana, ben = two
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    assert ruling(ana, "src/a.py", "ana: after the fork") == 0
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), "--no-push", repo=ana) == 0
    line = json.dumps(E.seal(E.build("ben", "finding", summary="ben on the stale side"), ""), sort_keys=True)
    f = p / "ledger" / "ben" / "stale.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(line + "\n")
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ben']}", "commit", "-S", "-qm", "stale side", cwd=p)
    sh("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "levain-team-ledger", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    merge = sh("git", "rev-parse", "HEAD", cwd=p).strip()
    g = gl(ana)
    g.sync(push=False)
    assert g.state().get("pending_ops"), "the offline member add was not held while frozen"
    assert team("accept-merge", merge, "--parent", "2", repo=ana) == 0
    g = gl(ana)
    g.sync(push=False)
    g._dcache = None
    assert "cy" in g.derivation().team.members
    assert not g.state().get("pending_ops")


def test_r2_a_symlink_pushed_at_a_members_ledger_file_is_never_followed(two, keys):
    """Code L3 r2 codex HIGH, RUN: another member pushes a symlink at ana's own ledger file; ana's next record opened
    it with "a+b" and appended ledger data to the file it points at, outside the worktree."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: first") == 0
    g = gl(ana)
    rel = g.file_for("ana").relative_to(g.wt).as_posix()
    outside = tmp / "outside.txt"
    outside.write_text("untouched\n")
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    (p / rel).unlink()
    (p / rel).symlink_to(outside)
    sh("git", "add", "-A", rel, cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ben']}", "commit", "-S", "-qm", "link", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    g.sync(push=False)
    assert (g.wt / rel).is_symlink(), "the setup did not land a symlink in ana's worktree"
    assert ruling(ana, "src/b.py", "ana: second") != 0
    assert outside.read_text() == "untouched\n"


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


# ---- re-running join on a pinned clone (code L3 r2 complement MED) -------------------------------------------


def test_rejoin_on_the_same_ledger_keeps_the_pin_the_distrust_list_and_the_accepted_merges(two):
    """RUN: re-running `join` on a pinned clone re-pinned silently and wiped the distrust list (failed on 0f48765)."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: one") == 0
    g = gl(ben)
    g.sync(push=False)
    head = sh("git", "rev-parse", "levain-team-ledger", cwd=ben).strip()
    assert team("distrust", head, repo=ben) == 0
    before = {k: g.state().get(k) for k in ("pinned_root", "distrust", "accepted", "device")}
    assert team("join", "--no-install", repo=ben) == 0
    after = {k: gl(ben).state().get(k) for k in before}
    assert after == before and head in after["distrust"]


def test_rejoin_never_moves_a_pinned_clone_to_another_genesis_unless_root_names_it(two, keys):
    """RUN: the host deletes the pinned ledger and leaves one with ANOTHER genesis. A plain `join` re-pinned to it
    (trust on first use, a second time, silently: failed on 0f48765). Now it refuses; `join --root <it>` moves the clone
    on the person's word, says so, and keeps the distrust list."""
    tmp, ana, ben = two
    tip = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    g = gl(ben)
    g.sync(push=False)
    old_root = g.state()["pinned_root"]
    assert team("distrust", tip, repo=ben) == 0
    g.save_state(signing_key=str(keys["ben"]))
    assert team("regenesis", "--from", tip, "--owner", "ben", "--member", "ben=ben@ex.com", repo=ben) == 0
    new_branch = [r.split("refs/heads/")[-1] for r in sh("git", "ls-remote", "--heads", str(tmp / "origin.git"),
                                                         cwd=tmp).split() if "levain-team-ledger-" in r][0]
    sh("git", "--git-dir", str(tmp / "origin.git"), "update-ref", "-d", "refs/heads/levain-team-ledger", cwd=tmp)
    new_root = sh("git", "--git-dir", str(tmp / "origin.git"), "rev-list", "--max-parents=0", new_branch,
                  cwd=tmp).strip()
    assert new_root != old_root
    with pytest.raises(TeamError, match="pinned to genesis"):
        gl(ben).join()
    assert gl(ben).state()["pinned_root"] == old_root
    out = gl(ben).join(root=new_root[:12])
    assert "re-pinned" in out and old_root[:12] in out
    st = gl(ben).state()
    assert st["pinned_root"] == new_root and tip in st["distrust"]


def test_revoke_after_a_commit_before_the_owners_spell_is_refused_with_the_reason(two, keys, capsys):
    """RUN (docs L3 r1): the derivation counts a revoke only inside the current owner's spell. The CLI refused it with
    a generic "would not count"; now it names the spell start and points to veto. Nothing is written either way."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    early = sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0]
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=ben) == 0
    before = sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0]
    capsys.readouterr()
    assert team("revoke", S.fingerprint(keys["cy"].read_text()), "--after", early, repo=ben) == 2
    err = capsys.readouterr().err
    assert "spell as owner began" in err and "veto" in err, err
    assert sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0] == before


def test_status_names_a_regenesis_branch_owner_and_keys_and_calls_it_unverified(two, keys, capsys):
    """RUN (docs L3 r1 anansi): anyone who can push can make a re-genesis branch; status used to say only "to move:
    join --root". It now names the claimed owner and key fingerprints and says UNVERIFIED."""
    tmp, ana, ben = two
    tip = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    g = gl(ben)
    g.sync(push=False)
    g.save_state(signing_key=str(keys["ben"]))
    assert team("regenesis", "--from", tip, "--owner", "ben", "--member", "ben=ben@ex.com", repo=ben) == 0
    gl(ana).sync()
    capsys.readouterr()
    assert team("status", repo=ana) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "RE-GENESIS" in x)
    assert "owner ben" in line and S.fingerprint(keys["ben"].read_text()) in line and "UNVERIFIED" in line, line


def test_veto_names_the_commit_that_began_the_spell_or_is_refused(two, keys, capsys):
    """RUN (docs L3 r2 anansi): `veto --since <a commit that began no spell>` was counted and changed nothing, with a
    success line. Now refused, listing the spell starts; a veto at the real start still works."""
    tmp, ana, ben = two
    g = gl(ana)
    g.sync(push=False)
    d = g.derivation()
    start = next(sp.start_sha for sp in d.spells if sp.role == "member" and sp.handle == "ben")
    assert ruling(ana, "src/a.py", "ana: unrelated") == 0
    other = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    before = sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0]
    capsys.readouterr()
    assert team("veto", "ben", "--role", "member", "--since", other, repo=ana) == 2
    assert start[:10] in capsys.readouterr().err
    assert sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0] == before
    assert team("veto", "ben", "--role", "member", "--since", start, repo=ana) == 0


def test_regenesis_makes_its_runner_the_owner_and_never_files_its_key_under_another_handle(two, keys):
    """RUN (docs L3 r2 anansi): ben ran `regenesis --owner ana --member ana=...=<ana.pub>`; the genesis listed BEN's key
    as ana's owner key and dropped ana's. Both shapes are refused now, and nothing is pushed."""
    tmp, ana, ben = two
    tip = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    g = gl(ben)
    g.sync(push=False)
    heads = sh("git", "ls-remote", "--heads", str(tmp / "origin.git"), cwd=tmp)
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", f"ana=ana@ex.com={keys['ana']}.pub",
                "--member", "ben=ben@ex.com", repo=ben) == 2
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", "ben=ben@ex.com", repo=ben) == 2
    assert sh("git", "ls-remote", "--heads", str(tmp / "origin.git"), cwd=tmp) == heads


# ---- the hook halts when it cannot judge ----------------------------------------------------------------------


def _hook(repo, rel):
    payload = {"session_id": "s1", "transcript_path": "/x", "cwd": str(repo), "hook_event_name": "PreToolUse",
               "tool_name": "Edit", "tool_input": {"file_path": str(repo / rel)}}
    cp = subprocess.run([PY, "-P", "-m", "levain.team.hook", "pretooluse"], input=json.dumps(payload),
                        capture_output=True, text=True, timeout=60, cwd="/",
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout) if cp.stdout.strip() else {}


def test_r2_a_pinned_clone_that_lost_its_ledger_worktree_and_refs_denies(two):
    """Code L3 r2 codex MED, RUN: joined() went false (worktree and refs gone) and the hook allowed the edit."""
    import shutil
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: governs a") == 0
    g = gl(ben)
    g.sync(push=False)
    shutil.rmtree(g.wt)
    for ref in sh("git", "for-each-ref", "--format=%(refname)", cwd=ben).split():
        if "levain" in ref:
            sh("git", "update-ref", "-d", ref, cwd=ben)
    assert not gl(ben).joined() and gl(ben).pinned_root
    out = _hook(ben, "src/a.py")
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out


def test_r2_an_interrupted_tenure_write_is_recovered_not_wedged(two):
    """Code L3 r2 codex MED, RUN: a dirty tenure.toml (a signing failure mid-update) was refused as foreign forever."""
    tmp, ana, ben = two
    g = gl(ana)
    (g.wt / "tenure.toml").write_text((g.wt / "tenure.toml").read_text() + "\n# interrupted\n")
    assert ruling(ana, "src/a.py", "ana: after the interruption") == 0
    assert "ana: after the interruption" in in_force(ana)


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


# ---- regressions from the L1/L2 code reviews (each was RUN against the branch and failed before its fix) ---------

def test_l1_1_an_unsigned_duplicate_of_a_members_line_cannot_unenforce_it(two):
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: real") == 0
    assert "ben: real" in in_force(ana)
    p = plain_clone(tmp, "malplain", "mal@ex.com")
    f = next((p / "ledger" / "ben").glob("*.jsonl"))
    text = f.read_text()
    f.write_text(text + text.splitlines()[0] + "\n")
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "dup", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    assert "ben: real" in in_force(ana)


def test_l1_2_a_void_commits_revocation_has_no_effect_and_it_stays_a_base_link(two, keys):
    tmp, ana, ben = two
    assert team("key", "add", "ana", str(keys["ana2"]), repo=ana) == 0
    a2 = clone(tmp, "ana2", "ana@ex.com")
    GitLedger(Repo.discover(a2)).join(remote="origin", signing_key=str(keys["ana2"]))
    assert ruling(ben, "src/a.py", "ben: legit") == 0
    cut = sh("git", "rev-parse", "levain-team-ledger", cwd=gl(ben).wt).strip()
    genesis = gl(ana).derivation().walk[0]
    ben_fp = T.key_fps(gl(ben).derivation().tenure, "ben").pop()
    gl(a2).sync(push=False)
    assert team("revoke", ben_fp, "--after", genesis, repo=a2) == 0          # a thief holding ana2
    assert "ben: legit" not in in_force(ana)
    g = gl(ana)
    g.sync(push=False)
    ana2_fp = (T.key_fps(g.derivation().tenure, "ana") - {g.own_fingerprint()}).pop()
    assert team("key", "remove", "ana", ana2_fp, repo=ana) == 0
    assert team("revoke", ana2_fp, "--after", cut, repo=ana) == 0           # the owner voids the thief's commit
    assert "ben: legit" in in_force(ana)
    g = gl(ana)
    g._dcache = None
    assert T.key_fps(g.derivation().tenure, "ben") == {ben_fp}


def test_l1_3_a_member_cannot_propose_another_members_key_and_a_confirm_still_counts(two, keys):
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    assert team("key", "add", "cy", str(keys["mal"]), repo=ben) == 2
    cy = clone(tmp, "cy", "cy@ex.com")
    GitLedger(Repo.discover(cy)).join(remote="origin", signing_key=str(keys["cy"]))
    g = gl(ana)
    g.sync(push=False)
    g._dcache = None
    assert T.key_fps(g.derivation().tenure, "cy")


def test_l1_4_fetch_prune_never_lets_a_pinned_clone_recreate_a_deleted_ledger(two):
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: x") == 0
    sh("git", "push", "-q", str(tmp / "origin.git"), ":levain-team-ledger", cwd=ana)
    sh("git", "fetch", "-q", "--prune", "origin", cwd=ben)
    ruling(ben, "src/b.py", "ben: y")
    assert not sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).strip()


def test_l1_5_an_unsigned_local_commit_is_never_pushed_even_when_the_remote_has_not_moved(two):
    tmp, ana, ben = two
    g = gl(ben)
    f = g.wt / "ledger" / "ben" / "planted.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(E.seal(E.build("ben", "finding", summary="planted"), ""), sort_keys=True) + "\n")
    sh("git", "add", ".", cwd=g.wt)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "planted, unsigned", cwd=g.wt)
    g.sync()
    assert "planted" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "--format=%s", "levain-team-ledger")


def test_l2_f2_a_signature_moved_before_parent_is_not_a_signature(two, keys):
    from levain.team import signing as S
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    wt = gl(ana).wt
    c = sh("git", "rev-parse", "levain-team-ledger", cwd=wt).strip()
    raw = subprocess.run(["git", "cat-file", "commit", c], cwd=wt, capture_output=True, check=True).stdout
    head, body = raw.split(b"\n\n", 1)
    lines = head.split(b"\n")
    a = next(i for i, ln in enumerate(lines) if ln.startswith(b"gpgsig "))
    b = a + 1
    while b < len(lines) and lines[b].startswith(b" "):
        b += 1
    rest = lines[:a] + lines[b:]
    raw2 = b"\n".join([rest[0]] + lines[a:b] + rest[1:]) + b"\n\n" + body
    c2 = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin"], cwd=wt, input=raw2,
                        capture_output=True, check=True).stdout.decode().strip()
    assert S.verify_commit(Path(wt), c2).kind == "unsigned"


def test_l2_f1_a_revocation_reaching_before_the_revokers_own_ownership_does_not_count(two, keys):
    from levain.team import signing as S
    tmp, ana, ben = two
    cutoff = sh("git", "rev-parse", "levain-team-ledger", cwd=gl(ben).wt).strip()
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=ben) == 0
    g = gl(ben)
    g.sync(push=False)
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    g.save_state(signing_key=str(keys["mal"]))
    assert team("key", "confirm", repo=ben) == 0
    ben_fp = S.fingerprint(Path(keys["ben"]).read_text())
    assert team("key", "remove", "ben", ben_fp, repo=ben) == 0
    team("revoke", ben_fp, "--after", cutoff, repo=ben)        # reaches back before ben's own accept
    g = gl(ben)
    g.sync(push=False)
    g._dcache = None
    d = g.derivation()                                          # stable, judged, never a cap-picked answer
    assert d.team.owner == "ben" and d.judged == "full"
