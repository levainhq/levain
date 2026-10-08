"""Tenure design rows (tenure_design_1005.md r22 test table), lane B: merges, accept-merge, rewrites, repin, join
pinning, branch states. Each test RUNS the row's path on real git with real SSH keys through the levain CLI.

Where the built rules of 1007+25 (retired handles, first-key-only owner proposals, keyless offers) change a row, the
test runs the BUILT rule and its docstring says so. A row the build does not meet is kept as a strict xfail naming
the FINDING (expected vs observed).
"""
import json
import subprocess

import pytest

from levain.team import entry as E
from levain.team.transport import WARNINGS
from tests.test_team_tenure import (_pull_merge, clone, gl, in_force, keys, plain_clone, pytestmark,  # noqa: F401
                                    ruling, sh, team, two)


# ---- helpers --------------------------------------------------------------------------------------------------


def _signed(p, key, msg):
    sh("git", "add", "-A", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "-qm", msg, cwd=p)
    return sh("git", "rev-parse", "HEAD", cwd=p).strip()


def _line(p, author, words, key, *, fname="side.jsonl", path="src/a.py"):
    """A well-formed ruling under ``ledger/<author>/<fname>``, committed in a plain clone signed with ``key``."""
    line = json.dumps(E.seal(E.build(author, "decision", kind="ruling", owner="lead", words=words, paths=[path]), ""),
                      sort_keys=True)
    f = p / "ledger" / author / fname
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(line + "\n")
    return _signed(p, key, f"line: {words}")


def _side(tmp, name="benplain", email="ben@ex.com"):
    """A plain clone of the ledger at the current remote tip, on a local branch ``side``."""
    p = plain_clone(tmp, name, email)
    sh("git", "checkout", "-q", "-b", "side", cwd=p)
    return p


def _merge_side_onto_published(p):
    """Parent 1 = the published line (fast-forwarded to the remote tip), parent 2 = ``side``. Pushed."""
    sh("git", "checkout", "-q", "levain-team-ledger", cwd=p)
    sh("git", "pull", "-q", "--ff-only", "origin", "levain-team-ledger", cwd=p)
    sh("git", "merge", "-q", "--no-ff", "--no-edit", "side", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    return sh("git", "rev-parse", "HEAD", cwd=p).strip()


def _remote_tip(tmp):
    return sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).split()[0]


def _remote_log(tmp):
    return sh("git", "--git-dir", str(tmp / "origin.git"), "log", "--format=%H %s", "levain-team-ledger")


def _is_ancestor(repo, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo).returncode == 0


def _fresh(d):
    g = gl(d)
    g._dcache = None
    return g.derivation()


def _words(repo):
    g = gl(repo)
    g._dcache = None
    return {e.get("words") for e in g.ledger().in_force}


def _genesis(repo):
    return gl(repo).derivation().walk[0]


# ---- single-mechanism rows ------------------------------------------------------------------------------------


def test_t47_after_accept_merge_parent_2_later_owner_commits_keep_deriving(two, keys):
    """RUN: a stale-first-parent pull merge; ana `accept-merge M --parent 2`; then three owner changes on top of M
    (add cy, a ruling, remove cy), each pushed. Every one counts on ana, the clone stays full, the counted head walks
    with them; ben, accepting the same parent, derives the same team."""
    tmp, ana, ben = two
    merge = _pull_merge(tmp, ana, ben, keys)
    gl(ana).sync(push=False)
    assert team("accept-merge", merge, "--parent", "2", repo=ana) == 0
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    add = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    d = _fresh(ana)
    assert d.judged == "full" and "cy" in d.team.members and d.counted_head == add
    assert ruling(ana, "src/b.py", "ana: on top of the merge") == 0
    assert team("member", "remove", "cy", repo=ana) == 0
    rem = sh("git", "rev-parse", "levain-team-ledger", cwd=ana).strip()
    d = _fresh(ana)
    assert d.judged == "full" and "cy" not in d.team.members and d.retired("cy") and d.counted_head == rem
    assert "ana: on top of the merge" in _words(ana)
    assert _remote_tip(tmp) == rem                                        # it all went out
    g = gl(ben)
    g.sync(push=False)
    assert team("accept-merge", merge, "--parent", "2", repo=ben) == 0
    db = _fresh(ben)
    assert db.judged == "full" and db.team.members == d.team.members and db.counted_head == rem
    assert "ana: on top of the merge" in _words(ben)


def test_t45_join_accept_merge_sha_2_pins_along_the_named_parent(two, keys):
    """RUN: a stale-first-parent pull merge; a fresh clone `join` refuses; `join --accept-merge <M>:2` pins, judged
    full, its walk goes through parent 2 (the published side) and never the stale commit."""
    tmp, ana, ben = two
    merge = _pull_merge(tmp, ana, ben, keys)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", "--accept-merge", f"{merge}:2", repo=c) == 0
    g = gl(c)
    stale = sh("git", "rev-parse", f"{merge}^1", cwd=c).strip()
    published = sh("git", "rev-parse", f"{merge}^2", cwd=c).strip()
    d = _fresh(c)
    assert d.judged == "full" and g.state()["accepted"] == {merge: 2}
    assert published in d.walk and stale not in d.walk
    assert "ana: after the fork" in _words(c)


def test_t38_a_fresh_clone_pins_through_an_owner_named_merge_with_root_and_accept_merge(two, keys):
    """RUN: a merge whose parent 1 is the published line and parent 2 a side carrying an ana-signed ruling. A fresh
    clone: `join --root <genesis>` alone refuses; `join --root <genesis> --accept-merge <M>:1` pins to that genesis,
    judged full, and the side's ruling (arriving only through parent 2) is not enforced."""
    tmp, ana, ben = two
    p = _side(tmp)
    _line(p, "ana", "ana: only on the side", keys["ana"])
    assert ruling(ana, "src/a.py", "ana: on the published line") == 0
    merge = _merge_side_onto_published(p)
    root = _genesis(ana)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--root", root[:12], "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None
    assert team("join", "--root", root[:12], "--accept-merge", f"{merge}:1", "--signing-key", str(keys["cy"]),
                "--no-install", repo=c) == 0
    assert gl(c).pinned_root == root
    d = _fresh(c)
    assert d.judged == "full"
    words = _words(c)
    assert "ana: on the published line" in words and "ana: only on the side" not in words
    # control: the side's line IS a valid ana line, enforced when its parent is the one followed
    c2 = clone(tmp, "cy2", "cy@ex.com")
    assert team("join", "--root", root[:12], "--accept-merge", f"{merge}:2", "--signing-key", str(keys["cy"]),
                "--no-install", repo=c2) == 0
    assert "ana: only on the side" in _words(c2)


def test_t24_accept_merge_continues_on_parent_1_reads_nothing_from_parent_2_and_join_needs_no_force_push(
        two, keys, capsys):
    """RUN: a merge (parent 1 published, parent 2 a side with an ana-signed ruling); ana syncs (frozen),
    `accept-merge M` (default parent 1): full, the side's ruling not enforced; ana records on top and pushes; a fresh
    clone joins with `--accept-merge M:1`, and the remote history still holds M (no force-push).
    BUILT RULE: a fresh `join` over the merge needs `--accept-merge` (the T10 refusal); bare `join` is not tested to
    work afterwards."""
    tmp, ana, ben = two
    p = _side(tmp)
    _line(p, "ana", "ana: only on the side", keys["ana"])
    assert ruling(ana, "src/a.py", "ana: published") == 0
    merge = _merge_side_onto_published(p)
    g = gl(ana)
    g.sync(push=False)
    assert _fresh(ana).judged == "partial"
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 0
    out = capsys.readouterr().out
    assert "following parent 1" in out
    d = _fresh(ana)
    assert d.judged == "full"
    side = sh("git", "rev-parse", f"{merge}^2", cwd=ana).strip()
    assert side not in d.walk
    assert "ana: only on the side" not in _words(ana)
    assert ruling(ana, "src/b.py", "ana: after the accept") == 0
    tip = _remote_tip(tmp)
    assert _is_ancestor(ana, merge, tip)                                   # pushed on top of M: no force-push
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", f"{merge}:1", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    words = _words(c)
    assert {"ana: published", "ana: after the accept"} <= words and "ana: only on the side" not in words
    assert _remote_tip(tmp) == tip


def test_t19_repin_anchor_past_a_merge_and_join_root_over_a_merged_chain_both_refuse(two, keys):
    """RUN: an unaccepted pull merge M and an ana line on top of it (written past levain). On ana: `repin --anchor M`
    and `repin --anchor <the commit after M>` refuse and leave the anchor as it was. A fresh clone:
    `join --root <genesis>` refuses and pins nothing."""
    tmp, ana, ben = two
    merge = _pull_merge(tmp, ana, ben, keys)
    p = plain_clone(tmp, "anaplain", "ana@ex.com")
    after = _line(p, "ana", "ana: past the merge", keys["ana"], fname="past.jsonl")
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    g = gl(ana)
    g.sync(push=False)
    before = g.state().get("anchor")
    assert before and _fresh(ana).judged == "partial"
    assert team("repin", "--anchor", merge, repo=ana) == 2
    assert team("repin", "--anchor", after, repo=ana) == 2
    assert gl(ana).state().get("anchor") == before
    assert _fresh(ana).judged == "partial"
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--root", _genesis(ana)[:12], "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None


def test_t10_join_refuses_a_merged_first_parent_chain_until_the_merge_is_named(two, keys):
    """RUN: a fresh clone of a ledger whose first-parent chain holds a merge: `join` and `join --root` refuse and pin
    nothing. BUILT RULE: the unlock is `--accept-merge <M>:<n>` (the merge the owner names), not `--root` alone."""
    tmp, ana, ben = two
    merge = _pull_merge(tmp, ana, ben, keys)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert team("join", "--root", _genesis(ana)[:12], "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None
    assert team("join", "--root", _genesis(ana)[:12], "--accept-merge", f"{merge}:2", "--signing-key",
                str(keys["cy"]), "--no-install", repo=c) == 0
    assert gl(c).pinned_root == _genesis(ana)


def _orphan_first_parent_merge(tmp):
    """A merge pushed onto the ledger whose parent 1 is an ORPHAN commit (its own root) and parent 2 the ledger."""
    p = plain_clone(tmp, "orphplain", "ben@ex.com")
    sh("git", "checkout", "-q", "--orphan", "orph", cwd=p)
    sh("git", "rm", "-rqf", ".", cwd=p)
    (p / "team.toml").write_text('project = "evil"\nowner = "mal"\n[members]\nmal = "mal@ex.com"\n')
    sh("git", "add", "team.toml", cwd=p)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "orphan root", cwd=p)
    sh("git", "merge", "-q", "--allow-unrelated-histories", "--no-edit", "-X", "ours", "origin/levain-team-ledger",
       cwd=p)
    sh("git", "push", "-q", "origin", "HEAD:levain-team-ledger", cwd=p)
    return sh("git", "rev-parse", "HEAD", cwd=p).strip(), sh("git", "rev-parse", "HEAD^1", cwd=p).strip()


def test_t10_orphan_first_parent_join_refuses_and_never_pins_the_orphan(two, keys):
    """RUN: a merge whose parent 1 is an orphan root is pushed onto the ledger. A fresh clone's `join` refuses and
    pins neither root."""
    tmp, ana, ben = two
    merge, orphan = _orphan_first_parent_merge(tmp)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert team("join", "--root", orphan[:12], "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None


@pytest.mark.xfail(strict=True, reason="FINDING T10: expected `join --root <genesis> --accept-merge M:2` to pin through "
                                       "an orphan-first-parent merge; observed exit 2, 'no single team ledger' (join "
                                       "drops any ledger branch with two roots, so no fresh clone can ever join)")
def test_t10_orphan_first_parent_join_pins_with_root_naming_the_genesis(two, keys):
    """RUN: as above, then `join --root <genesis> --accept-merge <M>:2` (the genesis side): the row expects it pins."""
    tmp, ana, ben = two
    root = _genesis(ana)
    merge, orphan = _orphan_first_parent_merge(tmp)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--root", root[:12], "--accept-merge", f"{merge}:2", "--signing-key", str(keys["cy"]),
                "--no-install", repo=c) == 0
    assert gl(c).pinned_root == root


def test_t18_a_force_pushed_tenure_less_history_freezes_a_pinned_clone_and_stays_strict(two, keys):
    """RUN: a member force-pushes an orphan, tenure-less history (owner = mal) over levain-team-ledger. ana syncs:
    nothing is re-published, she is frozen on her anchor (owner ana, her ruling in force), team writes refuse, and a
    fresh clone refuses the ledger as not strict."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: strict ruling") == 0
    p = plain_clone(tmp, "malplain", "mal@ex.com")
    sh("git", "checkout", "-q", "--orphan", "x", cwd=p)
    sh("git", "rm", "-rqf", ".", cwd=p)
    (p / "team.toml").write_text('project = "demo"\nowner = "mal"\n[members]\nmal = "mal@ex.com"\n')
    sh("git", "add", "team.toml", cwd=p)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "0.6-style history", cwd=p)
    sh("git", "push", "-q", "-f", "origin", "x:levain-team-ledger", cwd=p)
    forced = _remote_tip(tmp)
    WARNINGS.clear()
    gl(ana).sync()
    assert _remote_tip(tmp) == forced                                    # nothing re-published
    assert any("REWRITTEN" in w for w in WARNINGS), WARNINGS
    d = _fresh(ana)
    assert d.judged == "partial" and "rewritten" in d.frozen_why
    assert d.team.owner == "ana" and "mal" not in d.team.members
    assert "ana: strict ruling" in _words(ana)
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 2
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert gl(c).pinned_root is None


# ---- multi-clone merge rows -----------------------------------------------------------------------------------


def _sha(repo, rev="levain-team-ledger"):
    return sh("git", "rev-parse", rev, cwd=repo).strip()


def test_t23_after_a_hand_off_and_a_merge_a_lagging_clone_honours_nothing_after_its_anchor(two, keys):
    """RUN: ana rules v1; cy (a fresh, non-member clone) joins: its anchor. Hand-off K (ana offers, ben accepts),
    then ana, now ex-owner, supersedes v1 with v2; a merge M lands on top (parent 1 = the published line). cy syncs:
    frozen at its anchor, owner still ana, v1 in force and v2 not. Control: once cy accepts M it agrees with ana."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: v1") == 0
    first = next(e["id"] for e in gl(ana).ledger().in_force if e["words"] == "ana: v1")
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    anchor = gl(c).state()["anchor"]
    p = _side(tmp)
    _line(p, "ben", "ben: on the side", keys["ben"])
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=ben) == 0
    k = _remote_tip(tmp)
    assert team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", "src/a.py", "--words",
                "ana: v2 post-K", "--supersedes", first, repo=ana) == 0
    merge = _merge_side_onto_published(p)
    gl(c).sync(push=False)
    d = _fresh(c)
    assert d.judged == "partial" and d.frozen_at == anchor and k in d.walk and merge in d.walk
    assert d.team.owner == "ana"
    words = _words(c)
    assert "ana: v1" in words and "ana: v2 post-K" not in words
    assert d.waiting >= 1
    # control: the same history accepted is judged past the anchor, and cy then agrees with ana
    assert team("accept-merge", merge, "--parent", "1", repo=c) == 0
    gl(ana).sync(push=False)
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 0
    assert _fresh(c).team.owner == "ben" and _words(c) == _words(ana)


def test_t29_a_removed_members_merge_is_waiting_on_a_lagging_clone_and_never_enforced_after_accept(two, keys):
    """RUN: ana adds mal (key confirmed by mal's join); ben syncs (his anchor); mal's plain clone forks; ana removes mal
    (R); mal records a ruling on the stale fork and pushes a `git pull` merge on top of R. ben syncs: frozen, mal's
    line WAITING (not enforced). ben `accept-merge M --parent 2`: R counts, mal is gone, mal's line never enforced.
    Control: a clone following parent 1 (the stale side) does enforce that line."""
    tmp, ana, ben = two
    assert team("member", "add", "mal", "mal@ex.com", "--key", str(keys["mal"]), repo=ana) == 0
    m = clone(tmp, "mal", "mal@ex.com")
    assert team("join", "--signing-key", str(keys["mal"]), "--no-install", repo=m) == 0
    gl(ben).sync(push=False)
    p = plain_clone(tmp, "malplain", "mal@ex.com")
    assert team("member", "remove", "mal", repo=ana) == 0
    _line(p, "mal", "mal: after the anchor", keys["mal"], fname="m.jsonl")
    sh("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "levain-team-ledger", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    merge = _sha(p, "HEAD")
    gl(ben).sync(push=False)
    d = _fresh(ben)
    assert d.judged == "partial" and d.waiting >= 1
    assert "mal: after the anchor" not in _words(ben)
    assert team("accept-merge", merge, "--parent", "2", repo=ben) == 0
    d = _fresh(ben)
    assert d.judged == "full" and "mal" not in d.team.members and d.retired("mal")
    assert "mal: after the anchor" not in _words(ben)
    gl(ben).sync(push=False)
    assert "mal: after the anchor" not in _words(ben)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", f"{merge}:1", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    assert "mal: after the anchor" in _words(c)                           # the line is real; only the parent decides


def test_t30_accept_merge_lists_a_second_side_removal_every_accepting_clone_agrees_and_a_reissue_counts(
        two, keys, capsys):
    """RUN: ana adds cy; ben syncs (anchor = the fork point); a plain clone forks; ana removes cy (R, published);
    the plain clone records a line and pushes a `git pull` merge M (parent 2 holds R). ben `accept-merge M --parent 1`:
    accepted, R LISTED, cy still a member. ana (anchor R, not on parent 1) is refused until she re-pins her anchor
    at the fork point the team names, then accepts parent 1 and derives the same team. Her re-issued removal counts on
    both clones."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    gl(ben).sync(push=False)
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    fork = _sha(p, "HEAD")
    assert team("member", "remove", "cy", repo=ana) == 0
    r = _remote_tip(tmp)
    _line(p, "ben", "ben: stale side", keys["ben"])
    sh("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "levain-team-ledger", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    merge = _sha(p, "HEAD")
    gl(ben).sync(push=False)
    capsys.readouterr()
    assert team("accept-merge", merge, "--parent", "1", repo=ben) == 0
    out = capsys.readouterr().out
    assert r[:7] in out and "remove member cy" in out, out
    db = _fresh(ben)
    assert db.judged == "full" and "cy" in db.team.members
    gl(ana).sync(push=False)
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 2
    assert team("repin", "--anchor", fork, repo=ana) == 0
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 0
    da = _fresh(ana)
    assert da.judged == "full" and da.team.members == db.team.members and da.team.owner == db.team.owner
    assert team("member", "remove", "cy", repo=ana) == 0
    da = _fresh(ana)
    assert da.judged == "full" and "cy" not in da.team.members
    gl(ben).sync(push=False)
    db = _fresh(ben)
    assert "cy" not in db.team.members and db.counted_head == da.counted_head


def _rewrite_with_held_delta_and_frozen_append(two, keys):
    """ana rules a, b (pushed); ana `member add cy --no-push` (local); a member force-pushes the remote back to a; ana
    syncs (REWRITTEN, frozen, the team change held); ana records a ruling while frozen."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: keep a") == 0
    assert ruling(ana, "src/b.py", "ana: keep b") == 0
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), "--no-push", repo=ana) == 0
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    before = _sha(p, "levain-team-ledger~1")
    sh("git", "push", "-q", "-f", "origin", f"{before}:levain-team-ledger", cwd=p)
    WARNINGS.clear()
    gl(ana).sync()
    assert any("REWRITTEN" in w for w in WARNINGS), WARNINGS
    d = _fresh(ana)
    assert d.judged == "partial" and "cy" not in d.team.members
    assert gl(ana).state().get("pending_ops"), "the offline team change was not held"
    WARNINGS.clear()
    assert ruling(ana, "src/c.py", "ana: while frozen") == 0
    assert any("WAITING" in w for w in WARNINGS), WARNINGS
    assert "ana: while frozen" not in _words(ana)


@pytest.mark.xfail(strict=True, reason="FINDING T32: expected the frozen clone's own append LISTED as waiting; observed "
                                       "Derivation.waiting == 0 after a rewrite (the anchor fallback derives the "
                                       "anchor's walk, so status prints '0 line(s) after it are waiting')")
def test_t32_after_a_rewrite_own_appends_are_listed_as_waiting(two, keys, capsys):
    """RUN: the rewrite, the held delta and the frozen append (above): the append is not enforced (asserted in the
    setup) and it is LISTED: the derivation's waiting count and `levain team status` show it."""
    tmp, ana, ben = two
    _rewrite_with_held_delta_and_frozen_append(two, keys)
    capsys.readouterr()
    assert team("status", repo=ana) == 0
    out = capsys.readouterr().out
    assert _fresh(ana).waiting >= 1, out


def test_a_rejoin_does_not_lift_a_rewrite_freeze(two, keys):
    """RUN (docs L3 r1 anansi HIGH): a clone frozen by a host rewrite is still frozen after `levain team join`; only
    `repin` on the owner's word lifts it. Failed on 1ed15ed (the re-join reset the anchor and judged the rewritten
    history in full)."""
    tmp, ana, ben = two
    _rewrite_with_held_delta_and_frozen_append(two, keys)
    assert _fresh(ana).judged != "full"
    anchor = gl(ana).state().get("anchor")
    assert team("join", "--no-install", repo=ana) == 0
    assert _fresh(ana).judged != "full"
    assert gl(ana).state().get("anchor") == anchor
    assert "ana: while frozen" not in _words(ana)


def test_t32_held_deltas_re_land_after_the_repair(two, keys):
    """RUN: the rewrite, the held delta and the frozen append (above). The owner names the repair (`repin --anchor
    <remote tip>`): the frozen-time ruling is now enforced; then a sync re-lands the held `member add cy`."""
    tmp, ana, ben = two
    _rewrite_with_held_delta_and_frozen_append(two, keys)
    assert team("repin", "--anchor", _remote_tip(tmp), repo=ana) == 0
    assert _fresh(ana).judged == "full"
    assert "ana: while frozen" in _words(ana)
    assert "ana: keep b" not in _words(ana)                              # lost with the rewrite
    gl(ana).sync()
    assert not gl(ana).state().get("pending_ops"), gl(ana).state().get("pending_ops")
    assert "cy" in _fresh(ana).team.members                              # the held delta re-lands after the repair


def test_t33_after_a_host_repair_nothing_from_the_merge_reappears_and_no_side_team_commit_becomes_a_delta(two, keys):
    """RUN: on a side, ben signs a counted-shaped team commit (his own email change, Levain-Base = the counted head);
    it is merged (parent 2) onto the published line and pushed. ben's levain clone syncs: frozen, its HEAD contains M.
    Control: a clone following parent 2 counts that email change. The host repairs the branch to M^1; ben syncs and
    then records a ruling: M and the side commit never reach the remote again, no pending op is made, ben's email is
    unchanged."""
    from levain.team import roles as R
    tmp, ana, ben = two
    g = gl(ana)
    g.sync(push=False)
    base = _fresh(ana).counted_head
    p = _side(tmp)
    t = R.parse_team((p / "team.toml").read_text())
    t.members["ben"] = "ben@new.ex"
    (p / "team.toml").write_text(R.dump_team(t))
    side_team = _signed(p, keys["ben"], f"levain team: ben's email (side)\n\nLevain-Base: {base}")
    assert ruling(ana, "src/a.py", "ana: published") == 0
    merge = _merge_side_onto_published(p)
    gb = gl(ben)
    gb.sync(push=False)
    assert _fresh(ben).judged == "partial" and _is_ancestor(ben, merge, _sha(gb.wt, "HEAD"))
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", f"{merge}:2", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    assert _fresh(c).team.members["ben"] == "ben@new.ex"                  # the side commit is a real counted change
    repaired = _sha(p, f"{merge}^1")
    sh("git", "push", "-q", "-f", "origin", f"{repaired}:levain-team-ledger", cwd=p)
    WARNINGS.clear()
    gb = gl(ben)
    gb.sync()
    assert _remote_tip(tmp) == repaired
    assert not gb.state().get("pending_ops"), gb.state().get("pending_ops")
    assert ruling(ben, "src/b.py", "ben: after the repair") == 0
    log = _remote_log(tmp)
    assert merge not in log and side_team not in log and "ben's email (side)" not in log
    d = _fresh(ben)
    assert d.judged == "full" and d.team.members["ben"] == "ben@ex.com"
    assert not gl(ben).state().get("pending_ops")


def _hand_pull_then_repair(two, keys, *, same_key_clone):
    """ana rules S (seen by levain). Then E (ana's key, from a second clone of hers, when ``same_key_clone``) and B
    (ben's) are published. A hand `git pull` in ana's ledger worktree takes them outside levain; the host repairs the
    branch back to S; ana's levain sync runs."""
    tmp, ana, ben = two
    if same_key_clone:
        ab = clone(tmp, "ana_b", "ana@ex.com")
        assert team("join", "--signing-key", str(keys["ana"]), "--no-install", repo=ab) == 0
    assert ruling(ana, "src/a.py", "ana: S") == 0
    s = _remote_tip(tmp)
    if same_key_clone:
        assert ruling(ab, "src/b.py", "ana_b: E") == 0
    assert ruling(ben, "src/c.py", "ben: B") == 0
    wt = gl(ana).wt
    sh("git", "pull", "-q", "--ff-only", "origin", "levain-team-ledger", cwd=wt)
    assert "ben: B" in sh("git", "log", "-p", "-3", cwd=wt)
    sh("git", "push", "-q", "-f", str(tmp / "origin.git"), f"{s}:refs/heads/levain-team-ledger", cwd=ana)
    WARNINGS.clear()
    gl(ana).sync()
    remote_patch = sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", "levain-team-ledger")
    return s, remote_patch


def test_t36_a_hand_git_pull_then_a_host_repair_republishes_no_foreign_commit_and_reports_it(two, keys):
    """RUN (ben's entry only): after the hand pull and the repair, ana's sync re-publishes nothing and reports the
    commit it did not move."""
    tmp = two[0]
    s, patch = _hand_pull_then_repair(two, keys, same_key_clone=False)
    assert _remote_tip(tmp) == s and "ben: B" not in patch
    assert any("NOT moved" in w or "REWRITTEN" in w for w in WARNINGS), WARNINGS


def test_t36_a_hand_git_pull_then_a_host_repair_never_republishes_an_own_key_commit(two, keys):
    """RUN (ana has a second clone with the SAME key, ana_b, whose entry E the hand pull brought in): after the repair,
    ana's sync re-publishes neither E nor B and names E as not moved; ana_b, the device that published E, reports the
    rewrite on its own sync and re-publishes nothing either. Failed on 9c9513b (E re-signed and re-published)."""
    tmp = two[0]
    s, patch = _hand_pull_then_repair(two, keys, same_key_clone=True)
    assert "ana_b: E" not in patch and "ben: B" not in patch
    assert _remote_tip(tmp) == s
    assert any("NOT moved" in w for w in WARNINGS), WARNINGS
    WARNINGS.clear()
    gl(tmp / "ana_b").sync()
    assert any("REWRITTEN" in w for w in WARNINGS), WARNINGS
    assert _remote_tip(tmp) == s


def _accept_and_list(repo, merge, capsys, parent=1):
    capsys.readouterr()
    rc = team("accept-merge", merge, "--parent", str(parent), repo=repo)
    return rc, capsys.readouterr().out


@pytest.mark.xfail(strict=True, reason="FINDING T40: expected an uncounted side cherry-pick NOT listed by accept-merge; "
                                       "observed it listed (the list is every side commit touching team.toml/"
                                       "tenure.toml, not the side's counted decisions)")
def test_t40_an_uncounted_side_cherry_pick_is_accepted_and_not_listed(two, keys, capsys):
    """RUN: ana adds cy (C1) and removes cy (C2); a plain clone forks AFTER both, cherry-picks C1 onto its side (stale
    Levain-Base: uncounted), and it is merged as parent 2. ana `accept-merge M`: accepted, cy not a member, and the
    cherry-pick is NOT listed (it never counted)."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    c1 = _remote_tip(tmp)
    assert team("member", "remove", "cy", repo=ana) == 0
    p = _side(tmp)
    sh("git", "cherry-pick", c1, cwd=p)
    pick = _sha(p, "HEAD")
    assert ruling(ana, "src/a.py", "ana: published") == 0
    merge = _merge_side_onto_published(p)
    gl(ana).sync(push=False)
    rc, out = _accept_and_list(ana, merge, capsys)
    assert rc == 0
    d = _fresh(ana)
    assert d.judged == "full" and "cy" not in d.team.members
    assert pick[:7] not in out, out


def test_t40b_a_side_copy_forked_before_the_owner_commit_is_listed_and_the_reversal_stands(two, keys, capsys):
    """RUN: a plain clone forks at X; ana adds cy (C1) then removes cy (C2) on the published line. On the side, C1 is
    cherry-picked and SIGNED WITH THE OWNER KEY, so on the side's own chain it is a counted copy (its Levain-Base is
    X's counted head). BUILT RULE: a member cannot make a counted copy (a cherry-pick drops the signature), so the
    copy is the owner key's, as on a stale owner clone. Merged as parent 2; ana and ben `accept-merge M`: accepted,
    the copy LISTED, cy not a member on either clone. Control: a clone following parent 2 counts cy."""
    tmp, ana, ben = two
    p = _side(tmp)
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    c1 = _remote_tip(tmp)
    assert team("member", "remove", "cy", repo=ana) == 0
    sh("git", "fetch", "-q", "origin", cwd=p)
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ana']}", "cherry-pick", "-S", c1, cwd=p)
    pick = _sha(p, "HEAD")
    merge = _merge_side_onto_published(p)
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", f"{merge}:2", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    assert "cy" in _fresh(c).team.members                                # the side copy counts on its own chain
    for repo in (ana, ben):
        gl(repo).sync(push=False)
        rc, out = _accept_and_list(repo, merge, capsys)
        assert rc == 0 and pick[:7] in out, out
        d = _fresh(repo)
        assert d.judged == "full" and "cy" not in d.team.members


def test_t43_built_rule_remove_then_readd_is_refused_and_a_second_side_removal_is_listed_then_reissued(
        two, keys, capsys):
    """BUILT RULE (retired handles): the row's 'remove, re-add, remove bo' becomes: add cy, remove cy, re-add cy
    REFUSED (cy stays retired); add dee (the re-invite under a new handle); ben syncs (anchor); a plain clone forks;
    ana removes dee (published, parent 2 of a `git pull` merge). ben `accept-merge M --parent 1`: accepted, the dee
    removal LISTED, dee a member, cy still retired. ana re-pins at the fork, accepts parent 1, re-issues the removal:
    it counts on both clones."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    assert team("member", "remove", "cy", repo=ana) == 0
    assert team("member", "add", "cy", "cy@ex.com", repo=ana) == 2
    assert team("member", "add", "dee", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    gl(ben).sync(push=False)
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    fork = _sha(p, "HEAD")
    assert team("member", "remove", "dee", repo=ana) == 0
    r = _remote_tip(tmp)
    _line(p, "ben", "ben: stale side", keys["ben"])
    sh("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "levain-team-ledger", cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    merge = _sha(p, "HEAD")
    gl(ben).sync(push=False)
    rc, out = _accept_and_list(ben, merge, capsys)
    assert rc == 0 and r[:7] in out, out
    db = _fresh(ben)
    assert "dee" in db.team.members and "cy" not in db.team.members and db.retired("cy")
    gl(ana).sync(push=False)
    assert team("repin", "--anchor", fork, repo=ana) == 0
    assert team("accept-merge", merge, "--parent", "1", repo=ana) == 0
    assert team("member", "add", "cy", "cy@ex.com", repo=ana) == 2      # still retired along the followed side
    assert team("member", "remove", "dee", repo=ana) == 0
    for repo in (ana, ben):
        gl(repo).sync(push=False)
        d = _fresh(repo)
        assert d.judged == "full" and "dee" not in d.team.members and d.retired("dee")


def test_t44_an_accept_on_the_second_side_of_a_cancelled_offer_is_listed_and_the_owner_is_unchanged(
        two, keys, capsys):
    """RUN: ana offers ownership to ben (O, before the merge-base); ben's clone syncs and accepts with --no-push (A,
    local); ana cancels (published). A plain clone merges A as parent 2 onto the published line and pushes. ana and
    ben `accept-merge M`: accepted, A LISTED, owner ana, no offer. Control: a clone following parent 2 has owner ben."""
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    gl(ben).sync(push=False)
    assert team("accept", "--no-push", repo=ben) == 0
    a = _sha(gl(ben).wt, "HEAD")
    assert team("owner", "--cancel", repo=ana) == 0
    p = plain_clone(tmp, "plain", "ana@ex.com")
    sh("git", "fetch", "-q", str(gl(ben).wt), a, cwd=p)
    sh("git", "merge", "-q", "--no-ff", "--no-edit", a, cwd=p)
    sh("git", "push", "-q", "origin", "levain-team-ledger", cwd=p)
    merge = _sha(p, "HEAD")
    assert _sha(p, f"{merge}^2") == a
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", f"{merge}:2", "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 0
    assert _fresh(c).team.owner == "ben"                                  # the side's accept is a real counted change
    for repo in (ana, ben):
        gl(repo).sync(push=False)
        rc, out = _accept_and_list(repo, merge, capsys)
        assert rc == 0 and a[:7] in out, out
        d = _fresh(repo)
        assert d.judged == "full" and d.team.owner == "ana" and d.tenure.offer is None


# ---- branch states --------------------------------------------------------------------------------------------


def _legacy_remote(tmp):
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp)
    a = clone(tmp, "ana", "ana@ex.com")
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=a)
    sh("git", "checkout", "-q", "--orphan", "levain-ledger", cwd=a)
    (a / "team.toml").write_text('project = "old"\nowner = "ana"\n[members]\nana = "ana@ex.com"\n')
    sh("git", "add", "team.toml", cwd=a)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "0.6.x ledger", cwd=a)
    sh("git", "push", "-q", "origin", "levain-ledger", cwd=a)
    sh("git", "checkout", "-q", "main", cwd=a)
    return a


def test_t46_init_from_legacy_only_needs_replace_legacy_through_the_cli(tmp_path, keys):
    """RUN: a remote with only levain-ledger. `levain team init` refuses; with `--replace-legacy` it creates the strict
    ledger and the 0.6.x branch is untouched."""
    WARNINGS.clear()
    a = _legacy_remote(tmp_path)
    old = sh("git", "ls-remote", str(tmp_path / "origin.git"), "refs/heads/levain-ledger", cwd=tmp_path).split()[0]
    args = ("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com", "--signing-key",
            str(keys["ana"]), "--no-install")
    assert team(*args, repo=a) == 2
    assert gl(a).pinned_root is None
    assert team(*args, "--replace-legacy", repo=a) == 0
    assert _remote_tip(tmp_path) and gl(a).pinned_root
    assert sh("git", "ls-remote", str(tmp_path / "origin.git"), "refs/heads/levain-ledger", cwd=tmp_path).split()[0] == old


@pytest.mark.xfail(strict=True, reason="FINDING T46: expected a warning in BOTH (levain-ledger beside the strict "
                                       "ledger); observed none in join, sync or status output")
def test_t46_both_reads_only_the_strict_branch_and_warns(tmp_path, keys, capsys):
    """RUN: BOTH (levain-ledger and levain-team-ledger on the remote). A joiner pins the strict ledger and reads only
    it (the 0.6.x member 'old' is never a member); join, sync or status says the 0.6.x branch is there."""
    WARNINGS.clear()
    a = _legacy_remote(tmp_path)
    assert team("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com", "--member",
                f"ben=ben@ex.com={keys['ben']}", "--signing-key", str(keys["ana"]), "--no-install", "--replace-legacy",
                repo=a) == 0
    b = clone(tmp_path, "ben", "ben@ex.com")
    capsys.readouterr()
    assert team("join", "--signing-key", str(keys["ben"]), "--no-install", repo=b) == 0
    g = gl(b)
    assert g.branch == "levain-team-ledger" and _fresh(b).team.owner == "ana"
    g.sync()
    assert team("status", repo=b) == 0
    said = capsys.readouterr().out + capsys.readouterr().err + "\n".join(WARNINGS)
    assert "levain-ledger" in said.replace("levain-team-ledger", ""), said


def test_t46_a_pinned_clone_whose_strict_ledger_is_deleted_beside_a_legacy_one_lands_in_lost_not_the_legacy_refusal(
        two, keys):
    """RUN: a pinned clone (ben); someone deletes levain-team-ledger on the remote and pushes a 0.6.x levain-ledger.
    ben's sync refuses with the 'no longer has' (lost) message, never the 0.6.x refusal, and recreates nothing."""
    from levain.team.transport import TeamError
    tmp, ana, ben = two
    p = plain_clone(tmp, "malplain", "mal@ex.com")
    sh("git", "checkout", "-q", "--orphan", "levain-ledger", cwd=p)
    sh("git", "rm", "-rqf", ".", cwd=p)
    (p / "team.toml").write_text('project = "old"\nowner = "mal"\n[members]\nmal = "mal@ex.com"\n')
    sh("git", "add", "team.toml", cwd=p)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "0.6.x", cwd=p)
    sh("git", "push", "-q", "origin", "levain-ledger", ":levain-team-ledger", cwd=p)
    with pytest.raises(TeamError) as exc:
        gl(ben).sync()
    assert "no longer has" in str(exc.value) and "predates strict mode" not in str(exc.value)
    assert not sh("git", "ls-remote", str(tmp / "origin.git"), "refs/heads/levain-team-ledger", cwd=tmp).strip()


def test_the_merge_parent_is_always_named_never_defaulted(two, keys):
    """RUN (docs L3 r1): `accept-merge` without `--parent`, and `join --accept-merge SHA` without `:N`, are refused:
    which parent to follow is the owner's trust decision. Both silently followed parent 1 on de2a4de."""
    tmp, ana, ben = two
    head = sh("git", "rev-parse", "HEAD", cwd=ana).strip()
    try:
        rc = team("accept-merge", head, repo=ana)
    except SystemExit as exc:      # argparse refuses a missing required option before levain runs
        rc = exc.code
    assert rc == 2
    c = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--accept-merge", head, "--signing-key", str(keys["cy"]), "--no-install", repo=c) == 2
    assert not gl(c).state().get("pinned_root")
