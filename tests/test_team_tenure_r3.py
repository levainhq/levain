"""Code L3 round 3 on the strict signed ledgers (e41d466..f5f0751): each test is a run that failed on f5f0751.

H1 a re-genesis guard judged against the wrong ledger · H2 held entries replayed across a genesis move · H3 pending
team ops re-landed on another genesis · H4 a replay authorised by a frozen (anchor-fallback) derivation.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
from levain.team import signing as S
from levain.team import tenure as T
from levain.team.transport import WARNINGS
from tests.test_team_tenure import clone, gl, in_force, keys, ruling, sh, team, two  # noqa: F401
from tests.test_team_tenure_rows_a import LB, fresh, ruling_np


def _heads(tmp):
    return sh("git", "ls-remote", "--heads", str(tmp / "origin.git"), cwd=tmp)


def _new_genesis(tmp, before):
    """The genesis of the one ledger branch that appeared on the remote since ``before``."""
    name = next(r.split("refs/heads/")[-1] for r in _heads(tmp).split() if "refs/heads/" in r and r not in before)
    return sh("git", "--git-dir", str(tmp / "origin.git"), "rev-list", "--max-parents=0", name, cwd=tmp).strip(), name


def test_h1_regenesis_judges_its_identity_guard_on_the_pinned_ledger_only(two, keys):
    """RUN (code L3 r3 codex 1): ana's re-genesis B names alice; ben, pinned to A (no alice), ran `regenesis --from
    <B's tip> --owner alice`. The guard derived B's tip against A's pin, fell back to A's anchor, saw no alice and filed
    ben's key as alice's owner key. Now `--from` must descend from the pinned genesis and derive in full."""
    tmp, ana, ben = two
    tip = sh("git", "rev-parse", LB, cwd=ana).strip()
    before = _heads(tmp)
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", f"alice=alice@ex.com={keys['cy']}", repo=ana) == 0
    b_root, b_name = _new_genesis(tmp, before)
    gl(ben).sync(push=False)
    b_tip = sh("git", "rev-parse", f"refs/remotes/origin/{b_name}", cwd=ben).strip()
    heads = _heads(tmp)
    assert team("regenesis", "--from", b_tip, "--owner", "alice", "--member", "alice=alice@ex.com", repo=ben) == 2
    assert _heads(tmp) == heads


def test_h2_held_entries_are_never_replayed_onto_another_genesis(two, keys):
    """RUN (code L3 r3 codex 2): ben's old-key entry is held while his new key is pending; ana re-genesises with ben's
    new key pending; ben `join --root <it>`. The join's confirm replayed the held entry into the NEW ledger, re-signed
    and in force. Now held entries are bound to the genesis they were held on: kept, not replayed, and said so."""
    tmp, ana, ben = two
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    assert ruling_np(ben, "src/a.py", "ben: old ledger entry") == 0
    assert ruling(ana, "src/c.py", "ana: meanwhile") == 0
    g = gl(ben)
    g.save_state(signing_key=str(keys["mal"]))
    g.sync()
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=g.wt).strip()
    gl(ana).sync(push=False)
    tip = sh("git", "rev-parse", LB, cwd=gl(ana).wt).strip()
    before = _heads(tmp)
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", f"ben=ben@ex.com={keys['mal']}", repo=ana) == 0
    b_root, b_name = _new_genesis(tmp, before)
    WARNINGS.clear()
    assert team("join", "--root", b_root[:12], "--no-install", repo=ben) == 0
    assert "ben: old ledger entry" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", b_name)
    assert "ben: old ledger entry" not in in_force(ben)
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=gl(ben).wt).strip(), "the held entry was dropped"
    assert any("held entr" in w and "another genesis" in w for w in WARNINGS), WARNINGS


def test_h3_pending_team_ops_never_re_land_on_another_genesis(two, keys):
    """RUN (code L3 r3 codex 3): ana's offline `member add cy` is stripped into pending ops by a host rewrite (frozen,
    so not re-landed); ana re-genesises WITHOUT cy and joins it. The next sync re-added cy and cy's key to the new
    ledger. Now ops are bound to the genesis they were made on: never re-landed elsewhere, kept and said so."""
    tmp, ana, ben = two
    base = sh("git", "rev-parse", LB, cwd=gl(ana).wt).strip()
    assert ruling(ben, "src/b.py", "ben: seen then rewritten away") == 0
    gl(ana).sync(push=False)
    seen = sh("git", "rev-parse", LB, cwd=gl(ana).wt).strip()
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), "--no-push", repo=ana) == 0
    sh("git", "--git-dir", str(tmp / "origin.git"), "update-ref", f"refs/heads/{LB}", base, cwd=tmp)
    gl(ana).sync(push=False)
    assert gl(ana).state().get("pending_ops"), "the offline member add was not held"
    before = _heads(tmp)
    assert team("regenesis", "--from", seen, "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", "ben=ben@ex.com", repo=ana) == 0
    b_root, _ = _new_genesis(tmp, before)
    WARNINGS.clear()
    assert team("join", "--root", b_root[:12], "--no-install", repo=ana) == 0
    assert any("offline team change" in w and "another genesis" in w for w in WARNINGS), WARNINGS
    assert team("sync", repo=ana) == 0
    assert "cy" not in fresh(ana).team.members
    assert gl(ana).state().get("pending_ops"), "the old ledger's ops were dropped, not kept"


def test_h4_a_frozen_derivation_never_authorises_a_replay(two, keys):
    """RUN (code L3 r3 codex 4 + glm 1): ben rotates to a new key and confirms it (his anchor); records offline; the
    host force-pushes back to before the confirm. His sync judged the rewritten remote by the anchor's tenure (new key
    in force), re-signed the entry with a key that is only PENDING there and published it: in force on no fresh
    clone. Now a replay needs a derivation judged in full: the entry is held, not published."""
    tmp, ana, ben = two
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    before_confirm = sh("git", "rev-parse", LB, cwd=gl(ben).wt).strip()
    g = gl(ben)
    g.save_state(signing_key=str(keys["mal"]))
    assert team("key", "confirm", repo=ben) == 0
    assert S.fingerprint(keys["mal"].read_text()) in T.key_fps(fresh(ben).tenure, "ben")
    assert ruling_np(ben, "src/a.py", "ben: after the confirm") == 0
    sh("git", "--git-dir", str(tmp / "origin.git"), "update-ref", f"refs/heads/{LB}", before_confirm, cwd=tmp)
    WARNINGS.clear()
    try:
        gl(ben).sync()
    except Exception as exc:       # a refusal is fine; publishing is not
        WARNINGS.append(str(exc))
    assert "ben: after the confirm" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", LB)
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=gl(ben).wt).strip(), "the entry was dropped, not held"
    assert any("kept back" in w and "frozen" in w for w in WARNINGS), WARNINGS


def test_codex6_a_join_that_fails_after_choosing_a_ledger_restores_the_whole_state(two, keys):
    """RUN (code L3 r3 codex 6): a pinned clone runs `join --remote evil --root <a malformed genesis> --new-device`.
    The derivation refuses it, and the rollback restored the pin but left ``remote`` pointing at evil and a new device
    id (so this clone's unpublished commits would read as foreign). Now any failure restores the whole state."""
    tmp, ana, ben = two
    sh("git", "init", "-q", "--bare", "evil.git", cwd=tmp)
    junk = tmp / "junk"
    sh("git", "init", "-q", str(junk), cwd=tmp)
    (junk / "team.toml").write_text("not a team\n")
    sh("git", "add", ".", cwd=junk)
    sh("git", "-c", "user.email=x@ex.com", "-c", "user.name=x", "-c", "commit.gpgsign=false", "commit", "-qm", "junk",
       cwd=junk)
    sh("git", "push", "-q", str(tmp / "evil.git"), f"HEAD:refs/heads/{LB}-evil", cwd=junk)
    root = sh("git", "rev-parse", "HEAD", cwd=junk).strip()
    sh("git", "remote", "add", "evil", str(tmp / "evil.git"), cwd=ben)
    before = gl(ben).state()
    assert team("join", "--remote", "evil", "--root", root[:12], "--new-device", "--no-install", repo=ben) == 2
    assert gl(ben).state() == before


def test_r4_a_host_rewind_before_a_removal_never_re_authorises_a_held_entry(two, keys):
    """RUN (code L3 r4 codex HIGH + complement MED): ben records E offline; ana removes ben; ben syncs (E held, the
    removal is his anchor); the host force-pushes to before the removal; ben syncs. The anchor-less view saw ben's
    key in force again and E was re-signed and published. Now a frozen replay needs the key in force in BOTH views."""
    tmp, ana, ben = two
    assert ruling_np(ben, "src/a.py", "ben: before removal") == 0
    before = sh("git", "ls-remote", str(tmp / "origin.git"), f"refs/heads/{LB}", cwd=tmp).split()[0]
    assert team("member", "remove", "ben", repo=ana) == 0
    gl(ben).sync()
    assert "ben: before removal" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", LB)
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=gl(ben).wt).strip(), "E was not held"
    sh("git", "--git-dir", str(tmp / "origin.git"), "update-ref", f"refs/heads/{LB}", before, cwd=tmp)
    try:
        gl(ben).sync()
    except Exception:       # a refusal is fine; publishing is not
        pass
    assert "ben: before removal" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", LB)


def test_r4_a_join_whose_destination_branch_is_checked_out_elsewhere_leaves_the_old_ledger_working(two, keys):
    """RUN (code L3 r4 codex MED): the re-genesis branch is checked out in another worktree of ben's repository;
    `join --root <it>` removed ben's private ledger worktree, then failed to add the new one, and the state roll-back
    could not bring the worktree back: ben's clone was no longer joined. Now it is refused before anything moves."""
    tmp, ana, ben = two
    gl(ana).sync(push=False)
    tip = sh("git", "rev-parse", LB, cwd=gl(ana).wt).strip()
    before = _heads(tmp)
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", "ana=ana@ex.com",
                "--member", f"ben=ben@ex.com={keys['ben']}", repo=ana) == 0
    b_root, b_name = _new_genesis(tmp, before)
    gl(ben).sync(push=False)
    sh("git", "branch", b_name, f"refs/remotes/origin/{b_name}", cwd=ben)
    sh("git", "worktree", "add", "-q", str(tmp / "ben_other"), b_name, cwd=ben)
    state = gl(ben).state()
    assert team("join", "--root", b_root[:12], "--no-install", repo=ben) == 2
    assert gl(ben).joined() and gl(ben).state() == state
    # the persisted half (code L3 r4 ruling): with the destination free and the remote refusing pushes, the join pins
    # and keeps B, but its key confirm cannot publish, so the command must NOT exit as a success
    sh("git", "worktree", "remove", "--force", str(tmp / "ben_other"), cwd=ben)
    refs = tmp / "origin.git" / "refs" / "heads"
    refs.chmod(0o555)          # the remote refuses every ref update (a server hook would be skipped: levain runs git
    try:                       # with hooks disabled, and a local push carries that config to the receiving side)
        assert team("join", "--root", b_root[:12], "--no-install", repo=ben) == 2
    finally:
        refs.chmod(0o755)
    assert gl(ben).joined() and gl(ben).state()["pinned_root"] == b_root


def test_r4_accept_merge_persists_nothing_when_the_chosen_history_cannot_be_judged(tmp_path, keys):
    """RUN (code L3 r4 codex MED): on a local-only ledger (no anchor) an unrelated orphan is merged in;
    `accept-merge M --parent 2` saved {M: 2}, then failed to judge the walk (it no longer begins at the pinned
    genesis), leaving the bad acceptance installed. Now the acceptance is judged on a prospective clone first."""
    repo = tmp_path / "solo"
    sh("git", "init", "-q", str(repo), cwd=tmp_path)
    sh("git", "config", "user.email", "ana@ex.com", cwd=repo)
    sh("git", "config", "user.name", "ana", cwd=repo)
    (repo / "f").write_text("x\n")
    sh("git", "add", "f", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    assert team("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(keys["ana"]), "--no-install", repo=repo) == 0
    assert not gl(repo).state().get("anchor")
    wt = gl(repo).wt
    sh("git", "checkout", "-q", "--orphan", "stray", cwd=wt)
    sh("git", "rm", "-rq", "--cached", ".", cwd=wt)
    (wt / "stray.txt").write_text("x\n")
    sh("git", "add", "stray.txt", cwd=wt)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "stray", cwd=wt)
    sh("git", "checkout", "-q", "-f", LB, cwd=wt)
    sh("git", "clean", "-qfd", cwd=wt)
    sh("git", "-c", "commit.gpgsign=false", "merge", "-q", "--allow-unrelated-histories", "--no-edit", "stray",
       cwd=wt)
    merge = sh("git", "rev-parse", "HEAD", cwd=wt).strip()
    assert team("accept-merge", merge, "--parent", "2", repo=repo) == 2
    assert merge not in (gl(repo).state().get("accepted") or {})
