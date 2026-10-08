"""Tenure design rows, lane A (tenure_design_1005.md r22 test table): offers, keys, owner/member races, distrust, re-land.

Each test RUNS the row's path on real git with real SSH keys through the levain CLI (``team(...)``), the way
tests/test_team_tenure.py does. Where a row assumes re-adding a removed handle, the built rule (a removed handle is
retired: the re-add is refused or uncounted) is what is tested, and the docstring says so.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure, as the brief prescribes)
import json
import subprocess

import pytest

from levain.team import entry as E
from levain.team import signing as S
from levain.team import tenure as T
from levain.team.transport import WARNINGS
from tests.test_team_tenure import clone, gl, in_force, keys, plain_clone, ruling, sh, team, two  # noqa: F401

LB = "levain-team-ledger"


def ruling_np(repo, path, words):
    """A ruling recorded locally only (offline: not pushed)."""
    return team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", path, "--words", words,
                "--no-push", repo=repo)


def fresh(repo):
    g = gl(repo)
    g.sync(push=False)
    g._dcache = None
    return g.derivation()


def at_remote(repo):
    """The derivation of the remote's ledger tip on this clone, with no levain sync (for a clone whose sync fails)."""
    sh("git", "fetch", "-q", "origin", f"+refs/heads/{LB}:refs/lane-a/remote", cwd=repo)
    g = gl(repo)
    g._dcache = None
    return g.derivation(sh("git", "rev-parse", "refs/lane-a/remote", cwd=repo).strip())


def tip(repo, ref=LB):
    return sh("git", "rev-parse", ref, cwd=gl(repo).wt).strip()


def is_ancestor(a, b, cwd):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=cwd).returncode == 0


def remote_tip(tmp):
    return sh("git", "ls-remote", str(tmp / "origin.git"), f"refs/heads/{LB}", cwd=tmp).split()[0]


def second_owner_clone(tmp, ana, keys, name="anaB"):
    """ana's second machine: ana proposes ana2 for herself, the machine holding ana2 joins and confirms it."""
    assert team("key", "add", "ana", str(keys["ana2"]), repo=ana) == 0
    b = clone(tmp, name, "ana@ex.com")
    assert team("join", "--signing-key", str(keys["ana2"]), "--no-install", repo=b) == 0
    d = fresh(ana)
    assert S.fingerprint(keys["ana2"].read_text()) in T.key_fps(d.tenure, "ana")
    return b


def sockpuppet(tmp, thief, keys):
    """A thief holding ana's (stolen) owner key adds mal with mal's key as first key; mal's machine confirms it."""
    assert team("member", "add", "mal", "mal@ex.com", "--key", str(keys["mal"]), repo=thief) == 0
    m = clone(tmp, "malc", "mal@ex.com")
    assert team("join", "--signing-key", str(keys["mal"]), "--no-install", repo=m) == 0
    return m


def stolen_owner_clone(tmp, keys):
    th = clone(tmp, "thief", "thief@ex.com")
    assert team("join", "--signing-key", str(keys["ana"]), "--no-install", repo=th) == 0
    return th


def signed_commit(cwd, key, msg):
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "-qm", msg, cwd=cwd)


# ---- T14: an accept needs the offeree's own key; the owner's own email and keys ----------------------------------


def test_t14_an_accept_not_signed_by_the_offerees_key_is_uncounted_and_the_owner_edits_her_own_email_and_keys(
        two, keys):
    """RUN: ana adds her own second key ana2 by `key add` + confirm (from the ana2 machine), offers to ben; the ana2
    machine's `accept` is refused, and the same accept written past the CLI with ana2 is uncounted; ana changes her
    own email through the counted write path; ben's own accept then counts."""
    tmp, ana, ben = two
    b = second_owner_clone(tmp, ana, keys)
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=b) == 2                       # ana2 is in force, but for ana, not for ben
    g = gl(b)
    g.sync(push=False)
    g._dcache = None

    def forged(t, n):
        t.owner = "ben"
        n.offer = None
    g.update_counted(forged, "forged: accept as ben with the owner's second key", _check=False)
    d = fresh(ben)
    # the owner change is refused; clearing the offer was the owner key's own to make (a cancel), so it counts
    assert d.team.owner == "ana" and d.tenure.offer is None
    assert any("not in force" in p and "owner" in p for p in d.problems), d.problems
    assert team("accept", repo=ben) == 2                     # the offer is gone
    assert team("owner", "ben", repo=ana) == 0
    # the owner's own email: an ordinary counted field, written by her
    gl(ana).update_counted(lambda t, n: t.members.__setitem__("ana", "ana@new.example"), "levain team: ana's email")
    d = fresh(ben)
    assert d.team.members["ana"] == "ana@new.example" and d.team.owner == "ana"
    assert team("accept", repo=ben) == 0
    d = fresh(ana)
    assert d.team.owner == "ben" and d.tenure.offer is None


# ---- T15: distrust and repin act on an unchanged tip ------------------------------------------------------------


def test_t15_distrust_and_repin_take_effect_on_an_unchanged_tip(two, keys):
    """RUN: one GitLedger object derives, then `distrust` (and `--clear`) and `repin --anchor` change its verdicts
    with no new commit on the tip."""
    tmp, ana, ben = two
    assert ruling(ben, "src/a.py", "ben: distrust me") == 0
    g = gl(ana)
    g.sync(push=False)
    head = tip(ana)
    assert "ben: distrust me" in {e["words"] for e in g.ledger().in_force}
    assert team("distrust", head, repo=ana) == 0
    assert tip(ana) == head
    assert "ben: distrust me" not in {e["words"] for e in g.ledger().in_force}     # same object, same tip
    assert team("distrust", "--clear", repo=ana) == 0
    assert "ben: distrust me" in {e["words"] for e in g.ledger().in_force}
    # repin: a rewrite froze this clone; the owner names the new tip; the same object judges it in full
    assert ruling(ana, "src/b.py", "ana: rewound away") == 0
    p = plain_clone(tmp, "benplain", "ben@ex.com")
    before = sh("git", "rev-parse", f"{LB}~1", cwd=p).strip()
    sh("git", "push", "-q", "-f", "origin", f"{before}:{LB}", cwd=p)
    g = gl(ana)
    g.sync(push=False)
    head = tip(ana)
    d = g.derivation()
    assert d.judged == "partial"
    assert team("repin", "--anchor", before, repo=ana) == 0
    assert tip(ana) == head
    assert g.derivation().judged == "full"


# ---- T21: G = false, the owner changes her own email ------------------------------------------------------------


def test_t21_past_owner_links_false_and_an_owner_email_change_keeps_her_supersedes(two, keys):
    """RUN: G = false, ana supersedes her own ruling, then changes her own email (counted); the supersede stays
    honoured and her ownership is still one spell."""
    tmp, ana, ben = two
    gl(ana).update_counted(lambda t, n: setattr(n, "past_owner_links", False), "levain team: G = false")
    assert ruling(ana, "src/a.py", "ana: v1") == 0
    first = next(e["id"] for e in gl(ana).ledger().in_force if e["words"] == "ana: v1")
    assert team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", "src/a.py", "--words",
                "ana: v2", "--supersedes", first, repo=ana) == 0
    gl(ana).update_counted(lambda t, n: t.members.__setitem__("ana", "ana@new.example"), "levain team: ana's email")
    sh("git", "config", "user.email", "ana@new.example", cwd=ana)
    for repo in (ana, ben):
        d = fresh(repo)
        assert d.tenure.past_owner_links is False and d.team.members["ana"] == "ana@new.example"
        assert len([s for s in d.spells if s.role == "owner" and s.handle == "ana"]) == 1
        assert {e["words"] for e in gl(repo).ledger().in_force} == {"ana: v2"}


# ---- T13: offline commits, a derivation, a replaying sync: no freeze ------------------------------------------------


def test_t13_offline_commits_a_derivation_then_a_replaying_sync_does_not_freeze(two, keys):
    """RUN: ana records two rulings offline and derives; ben pushes; ana's sync replays her commits onto ben's: the
    clone is judged in full, nothing is reported rewritten, and every line is in force on both clones."""
    tmp, ana, ben = two
    assert ruling_np(ana, "src/a.py", "ana: offline 1") == 0
    assert ruling_np(ana, "src/b.py", "ana: offline 2") == 0
    g = gl(ana)
    assert g.derivation().judged == "full"
    assert {"ana: offline 1", "ana: offline 2"} <= {e["words"] for e in g.ledger().in_force}
    assert ruling(ben, "src/c.py", "ben: pushed meanwhile") == 0
    WARNINGS.clear()
    assert team("sync", repo=ana) == 0
    assert not any("REWRITTEN" in w for w in WARNINGS), WARNINGS
    assert remote_tip(tmp) == tip(ana)
    for repo in (ana, ben):
        d = fresh(repo)
        assert d.judged == "full", d.frozen_why
        assert {"ana: offline 1", "ana: offline 2", "ben: pushed meanwhile"} <= in_force(repo)


# ---- T11: an ex-owner's stale-fork commit, replayed -----------------------------------------------------------------


def test_t11_an_ex_owners_stale_fork_commit_replayed_by_sync_is_uncounted(two, keys):
    """RUN: ana hands off to ben; ana's clone, not yet fetched, adds cy offline (a stale fork); ana's sync replays
    it; then a hand cherry-pick of the same commit, re-signed with ana's key, is pushed: cy is a member nowhere."""
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", repo=ben) == 0
    assert gl(ana).derivation().team.owner == "ana"          # ana has not fetched the accept
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), "--no-push", repo=ana) == 0
    stale = tip(ana)
    WARNINGS.clear()
    team("sync", repo=ana)               # its exit status is test_t11_t31_a_refused_re_land_..., below
    for d in (at_remote(ana), fresh(ben)):
        assert d.team.owner == "ben" and "cy" not in d.team.members and "cy" not in d.tenure.pending_keys
    # the raw stale commit replayed by hand, re-signed with the ex-owner's real key, on top of the tip
    # (its team.toml and tenure.toml, owner = ana, carried onto the tip with its own message and Levain-Base)
    p = plain_clone(tmp, "anaplain", "ana@ex.com")
    sh("git", "fetch", "-q", str(ana), f"{stale}:refs/stale", cwd=p)
    sh("git", "checkout", "-q", stale, "--", "team.toml", "tenure.toml", cwd=p)
    msg = sh("git", "log", "-1", "--format=%B", stale, cwd=p)
    assert "Levain-Base:" in msg and 'owner = "ana"' in (p / "team.toml").read_text()
    sh("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keys['ana']}", "commit", "-S", "-qm", msg, cwd=p)
    sh("git", "push", "-q", "origin", LB, cwd=p)
    for d in (at_remote(ana), fresh(ben)):
        assert d.team.owner == "ben" and "cy" not in d.team.members
        assert any("Levain-Base" in p_ or "not in force" in p_ for p_ in d.problems), d.problems


# ---- T6: spells, rejoin (built: a retired handle), chains, email -----------------------------------------------


def test_t6_spells_rejoin_is_a_new_handle_chains_hold_and_an_email_change_changes_no_verdict(two, keys):
    """RUN. BUILT RULE: a removed handle is retired, so the row's "rejoin is a second spell" is tested as: the re-add
    of cy is refused, and the re-invite under a new handle cy2 is a new spell. cy's lines from before cy joined are not
    enforced; cy's spell lines stay enforced after the removal and after an email change; a line filed under cy after
    the removal is not; cy's file chain is intact; cy2's lines are enforced."""
    tmp, ana, ben = two
    # a line under ledger/cy/, signed by cy's key, BEFORE cy is a member
    p = plain_clone(tmp, "cyplain", "cy@ex.com")
    pre = json.dumps(E.seal(E.build("cy", "decision", kind="ruling", owner="lead", words="cy before joining", paths=["src/x.py"]), ""), sort_keys=True)
    f = p / "ledger" / "cy" / "early.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(pre + "\n")
    sh("git", "add", ".", cwd=p)
    signed_commit(p, keys["cy"], "cy writes before joining")
    sh("git", "push", "-q", "origin", LB, cwd=p)
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    cy = clone(tmp, "cy", "cy@ex.com")
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=cy) == 0
    assert ruling(cy, "src/a.py", "cy: spell 1, line 1") == 0
    # an email change: display only (by the owner), and cy's git email changes too
    gl(ana).update_counted(lambda t, n: t.members.__setitem__("cy", "cy@new.example"), "levain team: cy's email")
    sh("git", "config", "user.email", "cy@new.example", cwd=cy)
    assert ruling(cy, "src/b.py", "cy: spell 1, line 2") == 0
    words = in_force(ana)
    assert {"cy: spell 1, line 1", "cy: spell 1, line 2"} <= words
    d = fresh(ana)
    assert len([s for s in d.spells if s.handle == "cy"]) == 1            # the email change began no spell
    assert "cy before joining" not in words
    # removal; the re-add is refused (built rule) and the same add written past the CLI is uncounted
    assert team("member", "remove", "cy", repo=ana) == 0
    assert team("member", "add", "cy", "cy@ex.com", repo=ana) == 2
    gl(ana).update_counted(lambda t, n: t.members.__setitem__("cy", "cy@ex.com"), "forged: re-add cy", _check=False)
    d = fresh(ana)
    assert "cy" not in d.team.members and d.retired("cy")
    assert {"cy: spell 1, line 1", "cy: spell 1, line 2"} <= in_force(ana)     # the spell's lines stand
    # cy, removed, files another line under ledger/cy/ with its own (still valid) key
    p2 = plain_clone(tmp, "cyplain2", "cy@ex.com")
    late = json.dumps(E.seal(E.build("cy", "decision", kind="ruling", owner="lead", words="cy after removal", paths=["src/y.py"]), ""), sort_keys=True)
    f2 = p2 / "ledger" / "cy" / "late.jsonl"
    f2.parent.mkdir(parents=True, exist_ok=True)
    f2.write_text(late + "\n")
    sh("git", "add", ".", cwd=p2)
    signed_commit(p2, keys["cy"], "cy writes after removal")
    sh("git", "push", "-q", "origin", LB, cwd=p2)
    # the re-invite: a new handle, cy's key as cy2's first key
    assert team("member", "add", "cy2", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    assert team("key", "confirm", repo=cy) == 0                            # cy's key, now pending for cy2
    assert gl(cy).handle() == "cy2"
    assert ruling(cy, "src/c.py", "cy2: spell 2") == 0
    d = fresh(ana)
    assert [s.handle for s in d.spells if s.handle in ("cy", "cy2")] == ["cy", "cy2"]
    led = gl(ana).ledger()
    words = {e.get("words") for e in led.in_force}
    assert {"cy: spell 1, line 1", "cy: spell 1, line 2", "cy2: spell 2"} <= words
    assert "cy after removal" not in words and "cy before joining" not in words
    # no chain break: cy's own device file (two lines, either side of the email change) has no chain problem
    cyfile = next(r for r in d.files if r.startswith("cy/") and r.endswith(f"{gl(cy).device}.jsonl"))
    assert len(d.files[cyfile]) == 2
    assert not [p_ for p_ in led.problems if cyfile in p_ and ("chain" in p_ or "prev" in p_)], led.problems


# ---- T28, T31: stripped commits re-land ----------------------------------------------------------------------------


def test_t28_an_offline_member_remove_stripped_by_a_hook_sync_is_re_landed(two, keys):
    """RUN: ana removes cy offline; ben pushes; ana's hook-path sync (``fetch_if_due``, push=False, what the hooks
    call) strips the remove into a pending delta and re-lands it from the counted state; ana's next sync publishes it."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    assert team("member", "remove", "cy", "--no-push", repo=ana) == 0
    removed = tip(ana)
    assert ruling(ben, "src/a.py", "ben: meanwhile") == 0
    WARNINGS.clear()
    g = gl(ana)
    assert g.fetch_if_due(0) is None
    new = tip(ana)
    assert not is_ancestor(removed, new, g.wt)                 # stripped, not replayed as text
    assert "re-land" in sh("git", "log", "-1", "--format=%s", new, cwd=g.wt)
    assert not g.state().get("pending_ops")
    g._dcache = None
    d = g.derivation()
    assert "cy" not in d.team.members and d.retired("cy") and "cy" not in d.tenure.pending_keys
    assert team("sync", repo=ana) == 0
    d = fresh(ben)
    assert "cy" not in d.team.members and "ben: meanwhile" in in_force(ben)


@pytest.mark.parametrize("cancelled", [False, True], ids=["live", "cancelled"])
def test_t31_an_offerees_stripped_accept_re_lands_only_while_the_offer_is_live(two, keys, cancelled):
    """RUN: ana offers to ben; ben accepts offline; ana pushes (a ruling, or a cancel); ben's sync strips the accept and
    re-lands it: it counts while the offer is live, and is dropped (reported) once the offer was cancelled."""
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    assert team("accept", "--no-push", repo=ben) == 0
    accepted = tip(ben)
    if cancelled:
        assert team("owner", "--cancel", repo=ana) == 0
    else:
        assert ruling(ana, "src/a.py", "ana: meanwhile") == 0
    WARNINGS.clear()
    rc = team("sync", repo=ben)
    if not cancelled:                    # the cancelled case's exit status is test_t11_t31_a_refused_re_land_..., below
        assert rc == 0
    assert not is_ancestor(accepted, tip(ben), gl(ben).wt)    # stripped, not replayed as text
    for d in (fresh(ana), at_remote(ben) if cancelled else fresh(ben)):
        assert d.tenure.offer is None
        assert d.team.owner == ("ana" if cancelled else "ben")
    if cancelled:
        assert any("NOT re-applied" in w or "not in force" in w for w in WARNINGS), WARNINGS


@pytest.mark.parametrize("shape", ["stale_owner_add", "accept_of_cancelled_offer"])
def test_t11_t31_a_refused_re_land_is_dropped_and_sync_keeps_working(two, keys, shape):
    """RUN: T11's shape (an ex-owner's offline member add) and T31's (an offline accept of an offer cancelled since):
    sync drops the re-land with a message, exits 0, clears the pending op, and the next sync exits 0 too."""
    tmp, ana, ben = two
    assert team("owner", "ben", repo=ana) == 0
    if shape == "stale_owner_add":
        assert team("accept", repo=ben) == 0
        assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), "--no-push", repo=ana) == 0
        who = ana
    else:
        assert team("accept", "--no-push", repo=ben) == 0
        assert team("owner", "--cancel", repo=ana) == 0
        who = ben
    assert team("sync", repo=who) == 0
    assert not gl(who).state().get("pending_ops")
    assert team("sync", repo=who) == 0


# ---- T34, T37, T39: history-keyed re-land ------------------------------------------------------------------------


def test_t34_an_offline_offer_does_not_re_land_over_another_machines_offer_and_cancel(two, keys):
    """RUN: on A (ana) an offer to ben, offline; on B (ana's second key) an offer to ben then a cancel, pushed; A syncs:
    A's offer does not re-land (a counted commit touched `offer` since), though its VALUE equals B's old offer."""
    tmp, ana, ben = two
    b = second_owner_clone(tmp, ana, keys)
    assert team("owner", "ben", "--no-push", repo=ana) == 0
    assert team("owner", "ben", repo=b) == 0
    assert team("owner", "--cancel", repo=b) == 0
    WARNINGS.clear()
    assert team("sync", repo=ana) == 0
    for repo in (ana, b, ben):
        assert fresh(repo).tenure.offer is None
    assert any("NOT re-applied" in w and "offer" in w for w in WARNINGS), WARNINGS
    assert team("accept", repo=ben) == 2


def test_t37_a_stale_offline_remove_does_not_re_land_after_another_machines_remove(two, keys):
    """RUN. BUILT RULE: the row's "on B remove then re-add bo" is tested as: B removes ben, B's re-add of ben is
    refused by the CLI and uncounted past it, and B re-invites him as ben2. A's offline remove of ben then syncs: it
    does not re-land (a counted commit touched the field since) and nothing new is published for it."""
    tmp, ana, ben = two
    b = second_owner_clone(tmp, ana, keys)
    assert team("member", "remove", "ben", "--no-push", repo=ana) == 0
    assert team("member", "remove", "ben", repo=b) == 0
    assert team("member", "add", "ben", "ben@ex.com", repo=b) == 2
    gb = gl(b)
    gb.update_counted(lambda t, n: t.members.__setitem__("ben", "ben@ex.com"), "forged: re-add ben", _check=False)
    assert team("member", "add", "ben2", "ben@ex.com", repo=b) == 0
    before = remote_tip(tmp)
    WARNINGS.clear()
    assert team("sync", repo=ana) == 0
    assert "re-land" not in sh("git", "log", "--format=%s", f"{before}..{remote_tip(tmp)}", cwd=gl(ana).wt)
    assert any("NOT re-applied" in w and "member.ben" in w for w in WARNINGS), WARNINGS
    for repo in (ana, b):
        d = fresh(repo)
        assert "ben" not in d.team.members and d.retired("ben") and "ben2" in d.team.members


def test_t39_offline_offer_cancel_and_remove_reinvite_end_in_the_owners_last_state(two, keys):
    """RUN. BUILT RULE: "remove then re-add bo" offline is tested as remove cy, the re-add refused, cy2 invited. With
    an offline offer then cancel, all in one sync after ben pushes: each field ends in the owner's LAST state, and the
    net no-op offer is dropped silently."""
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0
    assert team("owner", "ben", "--no-push", repo=ana) == 0
    assert team("owner", "--cancel", "--no-push", repo=ana) == 0
    assert team("member", "remove", "cy", "--no-push", repo=ana) == 0
    assert team("member", "add", "cy", "cy@ex.com", "--no-push", repo=ana) == 2
    assert team("member", "add", "cy2", "cy@ex.com", "--no-push", repo=ana) == 0
    assert ruling(ben, "src/a.py", "ben: meanwhile") == 0
    WARNINGS.clear()
    assert team("sync", repo=ana) == 0
    assert not any("offer" in w for w in WARNINGS), WARNINGS
    assert remote_tip(tmp) == tip(ana)
    for repo in (ana, ben):
        d = fresh(repo)
        assert d.tenure.offer is None
        assert "cy" not in d.team.members and d.retired("cy") and "cy" not in d.tenure.pending_keys
        assert d.team.members.get("cy2") == "cy@ex.com" and d.team.owner == "ana"
        assert "ben: meanwhile" in in_force(repo)


# ---- T12, T35: a stolen owner key; distrust ---------------------------------------------------------------------------


def test_t12_a_stolen_owner_key_hand_off_counts_distrust_restores_one_clone_and_the_14_day_line_shows(two, keys):
    """RUN: a thief holding ana's key adds a sockpuppet mal and offers him ownership; mal accepts: counted on every
    clone (the key is the owner's). ben distrusts the offer: ben's clone has ana as owner again; ana's clone, without
    it, has mal and its status shows the 14-day role-change line."""
    from levain.team.cli_tenure import status_lines
    tmp, ana, ben = two
    th = stolen_owner_clone(tmp, keys)
    m = sockpuppet(tmp, th, keys)
    assert team("owner", "mal", repo=th) == 0
    offer = remote_tip(tmp)
    assert team("accept", repo=m) == 0
    for repo in (ana, ben):
        assert fresh(repo).team.owner == "mal"
    assert team("distrust", offer, repo=ben) == 0
    d = fresh(ben)
    assert d.team.owner == "ana" and d.tenure.offer is None
    assert fresh(ana).team.owner == "mal"
    lines = status_lines(gl(ana))
    assert any(ln.startswith("role change ") and "owner: ana -> mal" in ln for ln in lines), lines
    assert not any("owner: ana -> mal" in ln for ln in status_lines(gl(ben)))


def test_t12_a_hand_off_with_the_owners_email_but_no_owner_signature_is_uncounted_everywhere(two, keys):
    """RUN: in a plain clone with ana's EMAIL, an offer to ben signed by mal's key, then an owner flip unsigned, each
    naming the right Levain-Base: uncounted on every clone; ben's accept is refused."""
    from levain.team import roles as R
    tmp, ana, ben = two
    p = plain_clone(tmp, "forger", "ana@ex.com")
    head = fresh(ana).counted_head
    ten = T.parse_tenure((p / "tenure.toml").read_text())
    ten.offer = {"handle": "ben", "email": "ben@ex.com"}
    (p / "tenure.toml").write_text(T.dump_tenure(ten))
    sh("git", "add", ".", cwd=p)
    signed_commit(p, keys["mal"], f"levain team: offer ownership to ben\n\nLevain-Base: {head}")
    t = R.parse_team((p / "team.toml").read_text())
    t.owner = "ben"
    (p / "team.toml").write_text(R.dump_team(t))
    sh("git", "add", ".", cwd=p)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", f"levain team: ben accepts\n\nLevain-Base: {head}", cwd=p)
    sh("git", "push", "-q", "origin", LB, cwd=p)
    for repo in (ana, ben):
        d = fresh(repo)
        assert d.team.owner == "ana" and d.tenure.offer is None and d.counted_head == head
        assert any("not in force" in p_ for p_ in d.problems) and any("not validly signed" in p_ for p_ in d.problems), \
            d.problems
    assert team("accept", repo=ben) == 2


@pytest.mark.parametrize("distrust_accept", [False, True], ids=["F", "F+A"])
def test_t35_after_a_distrusted_stolen_key_offer_the_real_owners_commits_count_and_a_replay_does_not(
        two, keys, distrust_accept):
    """RUN: stolen-key offer F to the thief's mal, mal's accept A; ana's clone distrusts F (and A); ana writes R1
    (G false), R2 (G true), R3 (remove mal): all three count on her clone and the base advances through them; then R1
    is replayed after R3, re-signed with the stolen key itself: uncounted."""
    tmp, ana, ben = two
    th = stolen_owner_clone(tmp, keys)
    m = sockpuppet(tmp, th, keys)
    assert team("owner", "mal", repo=th) == 0
    F = remote_tip(tmp)
    assert team("accept", repo=m) == 0
    A = remote_tip(tmp)
    assert fresh(ana).team.owner == "mal"
    assert team("distrust", F, repo=ana) == 0
    if distrust_accept:
        assert team("distrust", A, repo=ana) == 0
    assert fresh(ana).team.owner == "ana"
    g = gl(ana)
    g.update_counted(lambda t, n: setattr(n, "past_owner_links", False), "levain team: R1 G false")
    r1 = tip(ana)
    g.update_counted(lambda t, n: setattr(n, "past_owner_links", True), "levain team: R2 G true")
    r2 = tip(ana)
    assert team("member", "remove", "mal", repo=ana) == 0
    r3 = tip(ana)
    assert remote_tip(tmp) == r3
    msg = lambda c: sh("git", "log", "-1", "--format=%B", c, cwd=g.wt)   # noqa: E731
    assert f"Levain-Base: {r1}" in msg(r2) and f"Levain-Base: {r2}" in msg(r3)
    d = fresh(ana)
    # the trailer chain R1 <- R2 <- R3 ending at the counted head means each counted in turn (a skipped one would
    # stale the next one's Levain-Base); and none of them is reported
    assert d.counted_head == r3 and d.team.owner == "ana" and "mal" not in d.team.members
    assert d.tenure.past_owner_links is True and d.touched[("past_owner_links",)] == r2
    assert not [p_ for p_ in d.problems if any(c[:10] in p_ for c in (r1, r2, r3))], d.problems
    # replay R1 on top (its files, its message and stale Levain-Base), signed by the stolen owner key
    p = plain_clone(tmp, "replay", "ana@ex.com")
    sh("git", "checkout", "-q", r1, "--", "team.toml", "tenure.toml", cwd=p)
    signed_commit(p, keys["ana"], msg(r1))
    sh("git", "push", "-q", "origin", LB, cwd=p)
    d = fresh(ana)
    assert d.counted_head == r3 and d.tenure.past_owner_links is True and "mal" not in d.team.members


# ---- T16, T20, T22: owner changes racing member/non-owner team.toml pushes ------------------------------------------


def _junk_team_push(tmp, name, email, key=None, mutate=None):
    """A plain-clone push of team.toml/tenure.toml edits that do not count (not the owner's), naming the right base."""
    from levain.team import roles as R
    p = plain_clone(tmp, name, email)
    head = sh("git", "log", "-1", "--format=%H", "--", "team.toml", "tenure.toml", cwd=p).strip()
    t = R.parse_team((p / "team.toml").read_text())
    n = T.parse_tenure((p / "tenure.toml").read_text())
    (mutate or (lambda t, n: t.members.__setitem__("mal", "mal@ex.com")))(t, n)
    (p / "team.toml").write_text(R.dump_team(t))
    (p / "tenure.toml").write_text(T.dump_tenure(n))
    sh("git", "add", ".", cwd=p)
    msg = f"junk\n\nLevain-Base: {head}"
    if key:
        signed_commit(p, key, msg)
    else:
        sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", msg, cwd=p)
    sh("git", "push", "-q", "origin", LB, cwd=p)
    return p


def _t16_setup(tmp, ana, ben, keys):
    _junk_team_push(tmp, "junk", "mal@ex.com")
    gl(ana).sync(push=False)                     # ana's tip file now differs from the counted team
    gb = gl(ben)
    gb.sync(push=False)
    gb.update_counted(lambda t, n: t.members.__setitem__("ben", "ben@new.example"), "levain team: ben's email")
    assert fresh(ben).team.members["ben"] == "ben@new.example"
    return remote_tip(tmp)


def test_t16_a_conflicting_member_push_drops_the_owners_remove_and_the_restore_goes_with_it(two, keys):
    """RUN: an uncounted push makes ana's `member remove ben` a restore + change pair; ben's own counted email change
    lands first; ana's sync strips the pair: neither half is published, and ben is still a member."""
    tmp, ana, ben = two
    before = _t16_setup(tmp, ana, ben, keys)
    WARNINGS.clear()
    team("member", "remove", "ben", repo=ana)
    local = sh("git", "log", "--format=%s", f"{before}..{tip(ana)}", cwd=gl(ana).wt)
    published = sh("git", "log", "--format=%s", f"{before}..{remote_tip(tmp)}", cwd=gl(ana).wt)
    assert "restore the counted team" not in published and "remove member ben" not in published, published
    assert "restore the counted team" not in local and "remove member ben" not in local, local
    assert any("NOT re-applied" in w and "member.ben" in w for w in WARNINGS), WARNINGS
    for repo in (ana, ben):
        assert "ben" in fresh(repo).team.members


def test_t16_the_cli_exits_non_zero_when_its_change_is_not_in_force(two, keys):
    """RUN: the same race; design §3b: "After its sync, the CLI re-derives and exits non-zero if its change is not in
    force"."""
    tmp, ana, ben = two
    _t16_setup(tmp, ana, ben, keys)
    rc = team("member", "remove", "ben", repo=ana)
    assert "ben" in fresh(ana).team.members                 # the remove is not in force
    assert rc != 0


def test_t16_a_dropped_remove_is_dropped_whole_and_the_member_keeps_his_key(two, keys):
    """RUN: the same race; the owner's `member remove ben` is per-field compare-and-swapped at re-land: either the whole
    removal lands or none of it does. A member left with no key in force is a state nobody chose."""
    tmp, ana, ben = two
    _t16_setup(tmp, ana, ben, keys)
    team("member", "remove", "ben", repo=ana)
    d = fresh(ana)
    assert "ben" in d.team.members
    assert S.fingerprint(keys["ben"].read_text()) in T.key_fps(d.tenure, "ben")


def test_t20_a_non_owner_team_toml_push_racing_the_owners_remove_is_regenerated_over(two, keys):
    """RUN: ben's plain clone pushes team.toml flips (owner = ben, mal added), signed by ben's key; ana's
    `member remove cy`, written on her stale clone, syncs: regenerated from the counted state, it lands, and the tip's
    team.toml equals the counted team again."""
    from levain.team import roles as R
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", "--key", str(keys["cy"]), repo=ana) == 0

    def flip(t, n):
        t.owner = "ben"
        t.members["mal"] = "mal@ex.com"
    _junk_team_push(tmp, "benplain", "ben@ex.com", key=keys["ben"], mutate=flip)
    assert gl(ana).derivation().team.members.get("cy")             # ana has not fetched the flip
    assert team("member", "remove", "cy", repo=ana) == 0
    assert remote_tip(tmp) == tip(ana)
    for repo in (ana, ben):
        d = fresh(repo)
        assert "cy" not in d.team.members and "mal" not in d.team.members and d.team.owner == "ana"
        tipteam = R.parse_team(sh("git", "show", f"{LB}:team.toml", cwd=gl(repo).wt))
        assert tipteam.owner == "ana" and set(tipteam.members) == set(d.team.members)


def test_t22_a_duplicate_key_commit_racing_the_owners_remove_and_cancel_plants_nothing(two, keys):
    """RUN: ana offers to ben (pushed), then offline removes cy and cancels the offer. N1, from ben's plain clone,
    duplicates keys so its own team.toml and tenure.toml do not parse, around a planted mal, an offer to mal and a
    member veto. A scratch probe first confirms git three-way merges ana's commits onto N1 cleanly into files that DO
    parse with mal and the planted offer (what a text replay would have signed). Then ana syncs: nothing planted
    counts, cy is removed, and the offer is cancelled."""
    import tomllib
    tmp, ana, ben = two
    assert team("member", "add", "cy", "cy@ex.com", repo=ana) == 0
    assert team("owner", "ben", repo=ana) == 0
    genesis = fresh(ana).walk[0]
    assert team("member", "remove", "cy", "--no-push", repo=ana) == 0
    assert team("owner", "--cancel", "--no-push", repo=ana) == 0
    ana_tip = tip(ana)
    p = plain_clone(tmp, "n1", "ben@ex.com")
    head = sh("git", "log", "-1", "--format=%H", "--", "team.toml", "tenure.toml", cwd=p).strip()
    team_txt = (p / "team.toml").read_text()
    assert '"cy" = "cy@ex.com"\n' in team_txt
    # members: a duplicate cy (unparseable) plus mal, two lines away from the cy line ana deletes
    team_txt = team_txt.replace('"ana" = "ana@ex.com"\n', '"ana" = "ana@ex.com"\n"cy" = "cy@ex.com"\n"mal" = "mal@ex.com"\n')
    ten_txt = (p / "tenure.toml").read_text()
    assert ten_txt.splitlines()[2].startswith("offer = ")
    # a duplicate offer (to mal) above past_owner_links; ana's cancel deletes the original offer line
    ten_txt = ten_txt.replace("rules = 1\n", 'rules = 1\noffer = { handle = "mal", email = "mal@ex.com" }\n', 1)
    ten_txt += f'\n[[veto]]\nhandle = "ben"\nrole = "member"\nsince = "{genesis}"\nlinks = false\n'
    (p / "team.toml").write_text(team_txt)
    (p / "tenure.toml").write_text(ten_txt)
    for txt in (team_txt, ten_txt):
        with pytest.raises(tomllib.TOMLDecodeError):
            tomllib.loads(txt)
    sh("git", "add", ".", cwd=p)
    signed_commit(p, keys["ben"], f"N1\n\nLevain-Base: {head}")
    sh("git", "push", "-q", "origin", LB, cwd=p)
    # the scratch probe: git merges ana's offline commits onto N1 cleanly, into files that parse and carry the plant
    sh("git", "fetch", "-q", str(ana), f"{ana_tip}:refs/ana", cwd=p)
    tree = sh("git", "merge-tree", "--write-tree", "HEAD", "refs/ana", cwd=p).split()[0]
    merged_team = tomllib.loads(sh("git", "show", f"{tree}:team.toml", cwd=p))
    merged_ten = tomllib.loads(sh("git", "show", f"{tree}:tenure.toml", cwd=p))
    assert "mal" in merged_team["members"] and merged_ten["offer"]["handle"] == "mal" and merged_ten["veto"]
    # the real path
    WARNINGS.clear()
    assert team("sync", repo=ana) == 0
    for repo in (ana, ben):
        d = fresh(repo)
        assert "mal" not in d.team.members and "cy" not in d.team.members
        assert d.tenure.offer is None and not d.tenure.vetoes
        assert d.team.owner == "ana"


# ---- T42: a key rotation with unpushed entries signed by the old key ---------------------------------------------------


def _t42_rotate(tmp, ana, ben, keys):
    """ben proposes his new key (mal's key file stands in for ben's new key), records two rulings offline with the old
    key, has an UNSIGNED local commit with his email; ana pushes meanwhile; ben switches to the new key and confirms
    it (the confirm's sync replays his old-key entries), then removes the old key."""
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    assert ruling_np(ben, "src/a.py", "ben: old key 1") == 0
    assert ruling_np(ben, "src/b.py", "ben: old key 2") == 0
    g = gl(ben)
    f = g.wt / "ledger" / "ben" / "planted.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(E.seal(E.build("ben", "finding", summary="planted"), ""), sort_keys=True) + "\n")
    sh("git", "add", ".", cwd=g.wt)
    sh("git", "-c", "commit.gpgsign=false", "commit", "-qm", "planted, unsigned", cwd=g.wt)
    assert ruling(ana, "src/c.py", "ana: meanwhile") == 0
    g.save_state(signing_key=str(keys["mal"]))
    WARNINGS.clear()
    assert team("key", "confirm", repo=ben) == 0
    new_fp = S.fingerprint(keys["mal"].read_text())
    old_fp = S.fingerprint(keys["ben"].read_text())
    assert new_fp in T.key_fps(fresh(ben).tenure, "ben")
    assert team("key", "remove", "ben", old_fp, repo=ben) == 0
    return new_fp


def test_t42_a_key_rotation_publishes_old_key_entries_and_never_moves_an_unsigned_commit(two, keys):
    """RUN (_t42_rotate): the old-key entries are moved and published (not stranded locally); the unsigned commit is
    never moved, re-signed or published, and is reported."""
    tmp, ana, ben = two
    new_fp = _t42_rotate(tmp, ana, ben, keys)
    assert any("NOT moved or re-signed" in w for w in WARNINGS), WARNINGS
    published = sh("git", "--git-dir", str(tmp / "origin.git"), "log", "--format=%s", LB)
    assert "planted" not in published
    assert published.count("levain team: decision ben-") == 2
    assert remote_tip(tmp) == tip(ben)
    for repo in (ana, ben):
        assert T.key_fps(fresh(repo).tenure, "ben") == {new_fp}


def test_t42_old_key_entries_moved_by_a_rotation_stay_in_force(two, keys):
    """RUN (_t42_rotate): "not stranded" means the moved entries still count: both are in force on every clone.
    Failed on 9c9513b (re-signed with the still-pending key, published, in force nowhere)."""
    tmp, ana, ben = two
    _t42_rotate(tmp, ana, ben, keys)
    for repo in (ana, ben):
        words = in_force(repo)
        assert "ana: meanwhile" in words
        assert {"ben: old key 1", "ben: old key 2"} <= words, sorted(words)


def test_t42_a_sync_with_a_pending_key_holds_the_entries_and_the_confirm_publishes_them_in_force(two, keys):
    """RUN: ben switches to his still-PENDING new key and syncs BEFORE confirming. His old-key entry is held back
    (never published unenforced) and reported; `key confirm` then publishes it after the confirm, in force on every
    clone, and the held ref is gone."""
    tmp, ana, ben = two
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    assert ruling_np(ben, "src/a.py", "ben: old key 1") == 0
    assert ruling(ana, "src/c.py", "ana: meanwhile") == 0
    g = gl(ben)
    g.save_state(signing_key=str(keys["mal"]))
    WARNINGS.clear()
    g.sync()
    assert "ben: old key 1" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", LB)
    assert any("refs/levain/held/" in w for w in WARNINGS), WARNINGS
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=g.wt).strip()
    assert team("key", "confirm", repo=ben) == 0
    assert not sh("git", "for-each-ref", "refs/levain/held/", cwd=gl(ben).wt).strip()
    for repo in (ana, ben):
        assert {"ana: meanwhile", "ben: old key 1"} <= in_force(repo)


_PACK = """[pack]
name = "heldpack"
version = "1.0"
[[rule]]
id = "keep-batch"
paths = ["src/settlement.py"]
owner = "lead"
words = "heldpack: never rename write_batch."
"""


def test_a_pack_line_is_never_replayed_by_a_clone_that_stopped_being_owner(two, keys, tmp_path):
    """RUN (docs L3 r1 anansi): ana syncs a pack offline on one machine; from her second machine ownership moves to ben.
    Her first machine's sync must not re-sign and publish the pack line (pack lines count only for the owner): it is
    held and reported. Failed on 6a3c2e5 (the replay checked her key in force for SOME member, so it published)."""
    tmp, ana, ben = two
    b = second_owner_clone(tmp, ana, keys)
    pack = tmp_path / "heldpack"
    pack.mkdir()
    (pack / "judgment.toml").write_text(_PACK)
    assert team("pack-sync", str(pack), "--no-push", repo=ana) == 0
    assert team("owner", "ben", repo=b) == 0
    assert team("accept", repo=ben) == 0
    assert fresh(ben).team.owner == "ben"
    WARNINGS.clear()
    gl(ana).sync()
    assert "heldpack: never rename" not in sh("git", "--git-dir", str(tmp / "origin.git"), "log", "-p", LB)
    assert any("refs/levain/held/" in w for w in WARNINGS), WARNINGS
