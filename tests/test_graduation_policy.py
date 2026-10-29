"""The gate's coverage source over a real authority log and a real root file (14b DESIGN_REV3 §3, §5.3, §10 S4;
DESIGN_REV3_1 D2, D3; HEAD_RULINGS_R1 R6 H1).

Every log here is real (``tests/test_authority_graduation_kind.World``: an ssh-keygen laptop key and WebAuthn passkey
stand-ins, verified by ssh-keygen through the replay). Root's allowed-signers file is a file this uid owns, under the
test-only owner override (``tests/conftest.py``)."""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import levain.graduation_policy as gp
from levain.autonomic.coverage import COVER_MOVED, COVER_NONE, CoverageSnapshot, Effect, Uncovered
from levain.autonomic.risk import ActionRisk, RiskClass
from levain.graduation_policy import AuthorityCoverage

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import GP, GP2, STORE, T0, World, grad, opt_in, renew  # noqa: E402
from tests.autonomic_confirm_keys import anchor_scratch  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LOW = ActionRisk(RiskClass(1), reversible=True, external=False, financial=False)
APPEND = Effect("doc.append", LOW, None, None, None)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def key(world: World, name: str) -> str:
    return world.state().keys[world.fp(name)].public_key


_ROOT_DIRS: dict[Path, Path] = {}


def root_dir(tmp: Path) -> Path:
    """The directory of root's allowed-signers file for the test whose temp dir is ``tmp``: a private dir under the
    account home (``tests/autonomic_confirm_keys.anchor_scratch``), never under ``tmp``, whose ancestors on Linux
    include the world-writable ``/tmp`` that ``trust_anchor_problem`` refuses. Removed at exit."""
    d = _ROOT_DIRS.get(tmp)
    if d is None:
        d = _ROOT_DIRS[tmp] = anchor_scratch("grad-root-")
        atexit.register(shutil.rmtree, d, True)
    return d


def write_root(world: World, *names: str, namespaces: str = "levain-authority") -> Path:
    d = root_dir(world.tmp)
    path = d / "allowed_signers"
    tmp = d / ".allowed_signers.new"
    tmp.write_text("".join(f'{n} namespaces="{namespaces}" {key(world, n)}\n' for n in names))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def source(world: World, **kw) -> AuthorityCoverage:
    return AuthorityCoverage(world.root, allowed_signers=root_dir(world.tmp) / "allowed_signers", store_id=STORE, **kw)


def create(world: World, signer: str = "laptop", **over):
    return world.link({"op": "create_grant", "grant": grad(world, **over)}, signer)


def covers(src: AuthorityCoverage, world: World, effect: Effect = APPEND) -> str:
    """The cover value after a fresh refresh, checked against the peek that follows it."""
    view = src.refresh()
    value = view.cover(effect, world.clock.now).value
    assert src.peek(effect, world.clock.now) == value
    return value


def test_a_laptop_policy_its_signer_root_lists_covers_and_its_value_names_the_incarnation(world):
    snap = create(world)
    write_root(world, "laptop")
    src = source(world)
    view = src.refresh()
    assert isinstance(view, CoverageSnapshot) and view.head == (snap.seq, snap.head_hash)
    cover = view.cover(APPEND, world.clock.now)
    assert cover.posture.name == "ON_LOOP"
    assert cover.value.startswith(str(snap.grant_born[GP]) + ":")
    assert src.peek(APPEND, world.clock.now) == cover.value


def test_an_unlisted_signer_another_namespace_or_no_root_file_covers_nothing(world):
    create(world)
    src = source(world)
    assert isinstance(src.refresh(), Uncovered)                       # no root file at all
    assert src.peek(APPEND, world.clock.now) == COVER_NONE            # the peek matches the failed refresh
    write_root(world, "phone")
    assert covers(src, world) == COVER_NONE
    write_root(world, "laptop", namespaces="levain-confirm")
    assert covers(src, world) == COVER_NONE
    write_root(world, "laptop")
    assert covers(src, world) != COVER_NONE


def test_a_root_file_another_uid_could_write_covers_nothing(world):
    create(world)
    path = write_root(world, "laptop")
    os.chmod(path, 0o666)
    assert covers(source(world), world) == COVER_NONE


def test_an_action_at_another_risk_another_action_or_another_store_is_not_covered(world):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world, Effect("doc.append", ActionRisk(RiskClass(0), reversible=True, external=False,
                                                              financial=False), None, None, None)) == COVER_NONE
    assert covers(src, world, Effect("mail.send", LOW, None, None, None)) == COVER_NONE
    other = AuthorityCoverage(world.root, allowed_signers=root_dir(world.tmp) / "allowed_signers",
                              store_id="store-" + "cd" * 16)
    assert covers(other, world) == COVER_NONE


def test_scenario_8_a_passkey_born_policy_covers_only_with_its_root_line(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    create(world, "pk")
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) == COVER_NONE
    write_root(world, "laptop", "pk")
    assert covers(src, world) != COVER_NONE


def test_scenario_9_a_renew_by_a_key_root_does_not_list_leaves_the_policy_uncovered(world):
    create(world)
    world.link(renew(world), "pk")                         # enrolled in the log, not root-listed
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) == COVER_NONE
    write_root(world, "laptop", "pk")
    assert covers(src, world) != COVER_NONE


def test_without_renew_anchoring_only_the_born_signer_must_be_listed(world, monkeypatch):
    create(world)
    world.link(renew(world), "pk")
    write_root(world, "laptop")
    monkeypatch.setattr(gp, "RENEW_NEEDS_ANCHOR", False)
    assert covers(source(world), world) != COVER_NONE


def test_a_revoke_moves_the_head_so_the_peek_is_moved_and_the_next_refresh_uncovers(world):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    world.link({"op": "revoke_grant", "grant_id": GP}, "pk")
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED
    assert covers(src, world) == COVER_NONE


def test_a_root_file_edit_with_no_head_move_is_moved(world):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    write_root(world, "laptop", "pk")
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED


def test_a_lapse_with_no_head_move_uncovers_at_the_peek(world):
    create(world, days=1)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    assert src.peek(APPEND, world.clock.now + timedelta(days=1, seconds=1)) == COVER_NONE


def test_d2_a_revoke_committed_while_refresh_is_between_replay_and_store_is_moved_at_the_next_peek(world):
    create(world)
    write_root(world, "laptop")
    src = source(world, on_refresh_read=lambda: world.link({"op": "revoke_grant", "grant_id": GP}))
    view = src.refresh()
    assert view.cover(APPEND, world.clock.now).value != COVER_NONE   # what the replay it read said
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED            # never covered after the revoke


def test_d2_a_root_file_replaced_while_refresh_is_between_read_and_store_is_moved_at_the_next_peek(world):
    create(world)
    write_root(world, "laptop")
    src = source(world, on_refresh_read=lambda: write_root(world, "pk"))
    view = src.refresh()
    assert view.cover(APPEND, world.clock.now).value != COVER_NONE
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED


def test_a_log_that_does_not_replay_covers_nothing_and_the_peek_matches(world):
    create(world)
    write_root(world, "laptop")
    conn = sqlite3.connect(world.db, isolation_level=None)
    try:
        conn.execute("UPDATE authority_log SET signature = 'x' WHERE seq = (SELECT MAX(seq) FROM authority_log)")
    finally:
        conn.close()
    src = source(world, cache=None)
    import levain.authority as auth
    auth.PROCESS_CACHE.clear()
    assert isinstance(src.refresh(), Uncovered)
    assert src.peek(APPEND, world.clock.now) == COVER_NONE


def test_a_covered_snapshot_whose_head_cannot_be_read_is_moved(world, monkeypatch):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    monkeypatch.setattr(gp, "_connect", lambda db, timeout: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED


def test_the_constants_are_the_rulings(world):
    assert gp.RENEW_NEEDS_ANCHOR is True                    # H1: anchored renew
    assert gp.PEEK_BUSY_TIMEOUT == 0.05                    # DESIGN_REV3 §5.3: 50 ms


def test_a_policy_whose_renew_signer_the_log_revoked_covers_nothing_while_root_still_lists_it(world):
    # RULINGS_S1 item 2: fail closed on a revoked signer, whatever root's file lists
    create(world)
    world.link(renew(world), "pk")
    write_root(world, "laptop", "pk")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    world.link({"op": "revoke_key", "fingerprint": world.fp("pk")})
    assert covers(src, world) == COVER_NONE


def test_a_policy_whose_born_signer_the_log_revoked_covers_nothing_while_root_still_lists_it(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    create(world, "pk")
    write_root(world, "laptop", "pk")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    world.link({"op": "revoke_key", "fingerprint": world.fp("pk")})
    assert covers(src, world) == COVER_NONE


def test_the_replayed_policy_carries_its_signed_min_clean_fires(world):
    # RULINGS_R1 item 2: the engine walks the policy's own term
    create(world, min_clean_fires=100)
    write_root(world, "laptop")
    [p] = source(world).refresh().policies
    assert isinstance(p, gp.TermedGraduationPolicy) and p.min_clean_fires == 100


def test_min_clean_fires_has_one_source_the_signed_terms(world):
    """G1-5 (1026-21 RULINGS "G1 L3 r1 triage"): the term is read from the policy's signed ``terms_text``; a caller
    value that disagrees is refused, never carried beside the signed one."""
    create(world, min_clean_fires=100)
    [p] = gp.graduation_policies(world.state())
    assert gp.TermedGraduationPolicy.of(p).min_clean_fires == 100
    with pytest.raises(TypeError):
        gp.TermedGraduationPolicy.of(p, min_clean_fires=5)


def test_terms_that_carry_no_min_clean_fires_are_refused_whatever_a_caller_passes():
    """O-g1f1-3 (1026-22 RULINGS "G1"): the signed terms are the one source; there is no passed-value fallback for
    terms that carry none (the authority log requires the term, so only a policy built outside it can lack one)."""
    from tests.autonomic_test_only_coverage import policy as built
    bare = built(GP, {"doc.append": LOW}, expires_at=T0 + timedelta(days=7))
    with pytest.raises(ValueError, match="no signed min_clean_fires"):
        gp.TermedGraduationPolicy.of(bare)
    with pytest.raises(TypeError):
        gp.TermedGraduationPolicy.of(bare, min_clean_fires=20)
    signed = built(GP, {"doc.append": LOW}, expires_at=T0 + timedelta(days=7), min_clean_fires=20)
    assert gp.TermedGraduationPolicy.of(signed).min_clean_fires == 20


def test_an_ordinary_grant_without_a_birth_does_not_warn(world, caplog):
    """deepseek r2 LOW (1026-22 RULINGS "G1"): the unborn warning is for graduation grants only; an ordinary grant the
    replay holds without a ``grant_born`` entry is not a graduation policy and says nothing."""
    import dataclasses
    import logging
    from levain.cockpit.consent import utc_stamp
    create(world)
    # planted, as test_authority_graduation_kind does: this levain replays a grant of another kind, never creates one
    db = world.plant({"op": "create_grant", "grant": {"id": "push-0123456789ab", "kind": "push",
                                                      "expires_at": utc_stamp(world.clock.now + timedelta(days=1))}},
                     db=world.db)
    state = world.state(db)
    assert "push-0123456789ab" in state.policy.grants
    unborn = dataclasses.replace(state, grant_born={})
    gp._UNBORN_WARNED.discard(GP)
    gp._UNBORN_WARNED.discard("push-0123456789ab")
    with caplog.at_level(logging.WARNING, logger="levain.graduation_policy"):
        assert gp.graduation_policies(unborn) == ()
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 1 and GP in said[0], said


def test_a_graduation_grant_the_replay_has_no_birth_for_warns_once(world, caplog):
    """G1-8 (deepseek #4): a graduation grant present in the policy but absent from ``grant_born`` covers nothing, and
    says so once per grant, so a replay defect is not read as a revoke."""
    import dataclasses
    import logging
    create(world)
    state = world.state()
    unborn = dataclasses.replace(state, grant_born={})
    gp._UNBORN_WARNED.discard(GP)
    with caplog.at_level(logging.WARNING, logger="levain.graduation_policy"):
        assert gp.graduation_policies(unborn) == () and gp.graduation_policies(unborn) == ()
    said = [r.getMessage() for r in caplog.records if GP in r.getMessage()]
    assert len(said) == 1 and "grant_born" in said[0], said
    assert [q.grant_id for q in gp.graduation_policies(state)] == [GP]


# --- S4 slice 1, L3 r1 fixes (1025-25/14b_s4/RULINGS_S1_R1.md) ---------------------------------------------------

def copy_db(src: Path, dst: Path) -> None:
    """``src``'s whole database into ``dst`` through SQLite's backup API (WAL pages included)."""
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def head_on_disk(world: World) -> tuple[int, str]:
    conn = sqlite3.connect(world.db)
    try:
        return gp._auth.log_head(conn)
    finally:
        conn.close()


def test_fix1_a_restored_log_behind_its_anchor_is_moved_at_the_peek_and_uncovered_at_the_refresh(world):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    view = src.refresh()
    assert view.cover(APPEND, world.clock.now).value != COVER_NONE
    saved = world.tmp / "backup.db"
    copy_db(world.db, saved)
    world.link({"op": "revoke_grant", "grant_id": GP}, "pk")         # the anchor advances past the backup
    copy_db(saved, world.db)                                        # cockpit.db restored; the anchor is not
    assert head_on_disk(world) == view.head                         # the ABA: the head the snapshot was built at
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED
    assert isinstance(src.refresh(), Uncovered)                     # check_anchor refuses the rollback
    assert src.peek(APPEND, world.clock.now) == COVER_NONE


def test_fix1_a_covered_peek_whose_anchor_cannot_be_read_is_moved(world):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    gp._auth.anchor_path(world.root).write_text("not an anchor\n")
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED


def test_fix2_an_empty_or_missing_log_covers_nothing_and_the_peek_answers_none_not_moved(tmp_path):
    from levain.cockpit.firejournal import CockpitDB
    signers = root_dir(tmp_path) / "allowed_signers"
    signers.write_text('laptop namespaces="levain-authority" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\n')
    os.chmod(signers, 0o644)
    root = tmp_path / "cockpit"
    root.mkdir()
    src = AuthorityCoverage(root, allowed_signers=signers, store_id=STORE)
    assert isinstance(src.refresh(), Uncovered)                     # no cockpit.db at all
    assert src.peek(APPEND, T0) == COVER_NONE
    with CockpitDB(root).read():                                    # the journal's schema, an empty authority_log
        pass
    view = src.refresh()
    assert isinstance(view, Uncovered) and view.head == (0, "")
    assert src.peek(APPEND, T0) == COVER_NONE


def test_fix3_a_signature_mutation_uncovers_with_the_replay_cache_kept(world):
    """No cache is cleared: the cache keys a chain by every row's digest (signature included)."""
    create(world)
    write_root(world, "laptop")
    for cache in (None, gp._auth.ReplayCache()):
        src = source(world, cache=cache)
        assert covers(src, world) != COVER_NONE                     # the verified chain is now in the cache
        conn = sqlite3.connect(world.db, isolation_level=None)
        try:
            (sig,) = conn.execute("SELECT signature FROM authority_log WHERE seq = (SELECT MAX(seq) FROM "
                                  "authority_log)").fetchone()
            conn.execute("UPDATE authority_log SET signature = ? WHERE seq = (SELECT MAX(seq) FROM authority_log)",
                         ("x" + sig[1:],))
            assert isinstance(src.refresh(), Uncovered)
            assert src.peek(APPEND, world.clock.now) == COVER_NONE
            conn.execute("UPDATE authority_log SET signature = ? WHERE seq = (SELECT MAX(seq) FROM authority_log)",
                         (sig,))
        finally:
            conn.close()
        assert covers(src, world) != COVER_NONE                     # restored: it verifies again


def test_fix4_a_policy_that_raises_covers_nothing_and_the_others_still_cover(world, monkeypatch):
    create(world)
    second = create(world, gid=GP2)
    write_root(world, "laptop")
    real = gp._auth.graduation_incarnation

    def broken(state, gid):
        if gid == GP:
            raise KeyError("a malformed policy")
        return real(state, gid)

    monkeypatch.setattr(gp._auth, "graduation_incarnation", broken)
    assert [p.grant_id for p in gp.graduation_policies(world.state())] == [GP2]
    value = covers(source(world), world)
    assert value.startswith(str(second.grant_born[GP2]) + ":")
    monkeypatch.setattr(gp._auth, "graduation_incarnation", real)
    real_from = gp.GraduationPolicy.from_record

    def refuses(record, **kw):
        if record["id"] == GP2:
            raise TypeError("not a policy")
        return real_from(record, **kw)

    monkeypatch.setattr(gp.GraduationPolicy, "from_record", refuses)
    assert [p.grant_id for p in gp.graduation_policies(world.state())] == [GP]


def test_fix5_refresh_never_raises_when_the_root_check_or_the_head_read_raises(world, monkeypatch):
    create(world)
    write_root(world, "laptop")
    src = source(world)

    def nul(*a, **kw):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(gp, "trust_anchor_problem", nul)
    view = src.refresh()
    assert isinstance(view, Uncovered) and view.signers_digest == gp.UNREADABLE_DIGEST
    assert src.peek(APPEND, world.clock.now) == COVER_NONE
    monkeypatch.setattr(gp._auth, "log_head", lambda conn: ("seven", "h"))
    view = src.refresh()
    assert isinstance(view, Uncovered) and view.token == (gp.UNREADABLE_HEAD, gp.UNREADABLE_DIGEST, STORE)
    assert src.peek(APPEND, world.clock.now) == COVER_NONE


def test_fix5_the_fallback_view_is_built_from_the_sentinels_when_its_token_is_malformed(world, monkeypatch):
    create(world)
    src = source(world)                                             # no root file: the refresh fails
    monkeypatch.setattr(AuthorityCoverage, "_head", lambda self: (True, "a bool is no seq"))
    view = src.refresh()
    assert isinstance(view, Uncovered) and view.token == (gp.UNREADABLE_HEAD, gp.UNREADABLE_DIGEST, STORE)


def test_fix6_a_replay_failure_is_logged_as_itself_and_rolled_back(world, monkeypatch, caplog):
    create(world)
    write_root(world, "laptop")

    def fails(conn, **kw):
        conn.execute("ROLLBACK")                                    # the transaction ends inside the failure
        raise gp._auth.ReplayError(7, "the marker failure")

    monkeypatch.setattr(gp._auth, "snapshot", fails)
    with caplog.at_level("WARNING", logger="levain.graduation_policy"):
        assert isinstance(source(world).refresh(), Uncovered)
    failed = [r.getMessage() for r in caplog.records if "refresh failed" in r.getMessage()]
    assert len(failed) == 1 and "ReplayError" in failed[0] and "the marker failure" in failed[0], failed
    statements: list[str] = []
    real_connect = gp._connect

    def traced(db, timeout):
        conn = real_connect(db, timeout)
        conn.set_trace_callback(statements.append)
        return conn

    def raises(conn, **kw):
        raise gp._auth.ReplayError(7, "the transaction is still open")

    monkeypatch.setattr(gp, "_connect", traced)
    monkeypatch.setattr(gp._auth, "snapshot", raises)
    assert isinstance(source(world).refresh(), Uncovered)
    assert "ROLLBACK" in statements and "COMMIT" not in statements, statements


# --- S4 integration, L3 r1 fixes (1025-32/s4int/RULINGS_R1.md) --------------------------------------------------

def test_r1_1_an_anchor_advance_and_restore_between_the_anchor_check_and_the_head_read_is_moved(world, monkeypatch):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    view = src.refresh()
    assert view.cover(APPEND, world.clock.now).value != COVER_NONE
    saved = world.tmp / "backup.db"
    copy_db(world.db, saved)
    real_head = AuthorityCoverage._head

    def interleaved(self):
        # the barrier: the peek has sampled the anchor; a revoke advances it and cockpit.db is restored behind it
        monkeypatch.setattr(AuthorityCoverage, "_head", real_head)
        world.link({"op": "revoke_grant", "grant_id": GP}, "pk")
        copy_db(saved, world.db)
        return real_head(self)

    monkeypatch.setattr(AuthorityCoverage, "_head", interleaved)
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED
    assert head_on_disk(world) == view.head                         # the head read matched the snapshot's token


def test_r1_1_an_anchor_that_cannot_be_read_again_after_the_head_read_is_moved(world, monkeypatch):
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    real_head = AuthorityCoverage._head

    def interleaved(self):
        gp._auth.anchor_path(world.root).write_text("not an anchor\n")
        return real_head(self)

    monkeypatch.setattr(AuthorityCoverage, "_head", interleaved)
    assert src.peek(APPEND, world.clock.now) == COVER_MOVED


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == "levain.graduation_policy"]


def test_r1_4_a_link_since_the_refresh_is_moved_without_a_warning_and_a_forked_anchor_warns(world, caplog):
    import json
    create(world)
    write_root(world, "laptop")
    src = source(world)
    assert covers(src, world) != COVER_NONE
    world.link({"op": "revoke_grant", "grant_id": GP}, "pk")         # a legitimate link: the anchor advances
    with caplog.at_level("DEBUG", logger="levain.graduation_policy"):
        assert src.peek(APPEND, world.clock.now) == COVER_MOVED
    assert _warnings(caplog) == []
    assert any("past the cached replay" in r.getMessage() for r in caplog.records)
    create(world, gid=GP2)
    assert covers(src, world) != COVER_NONE                         # GP2 covers at the new head
    path = gp._auth.anchor_path(world.root)
    data = json.loads(path.read_text())
    data["head_hash"] = "0" * 64                                    # another hash at the replay's own seq
    path.write_text(json.dumps(data))
    caplog.clear()
    with caplog.at_level("DEBUG", logger="levain.graduation_policy"):
        assert src.peek(APPEND, world.clock.now) == COVER_MOVED
    assert len(_warnings(caplog)) == 1 and "AnchorError" in _warnings(caplog)[0], _warnings(caplog)
