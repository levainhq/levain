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
