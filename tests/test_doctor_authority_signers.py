"""The doctor's authority-signers rows (14b DESIGN_REV2 S5 rows 1 and 2): root's signers file and the graduation
policies it anchors, over a real authority log (``tests/test_authority_graduation_kind.World``) and a real file this
uid owns under the test-only owner override (``tests/conftest.py``)."""
from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path

import pytest

from levain import doctor
from levain.doctor import (AUTHORITY_SIGNER_REFUSALS, _check_authority_signers, authority_signer_sudo_line)
from levain.slot import layout
from levain.slot.grammar import parse_install_argv

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World, renew  # noqa: E402
from tests.test_graduation_policy import create, key, root_dir, write_root  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def install_of(world: World) -> Path:
    """An install whose ``.levain/cockpit`` is the world's cockpit."""
    inst = world.tmp / "inst"
    (inst / ".levain").mkdir(parents=True)
    (inst / ".levain" / "cockpit").symlink_to(world.root)
    return inst


def signers(world: World) -> Path:
    return root_dir(world.tmp) / "allowed_signers"


def rows(world: World, **kw):
    return _check_authority_signers(install_of(world), signers=signers(world), now=world.clock.now, **kw)


def test_no_journal_gives_no_row(tmp_path):
    assert _check_authority_signers(tmp_path / "nothing-here") == []


def test_a_missing_file_warns_with_the_one_sudo_line_and_every_refusal_name(world):
    [r] = rows(world)
    assert r.name == "authority signers" and r.ok and r.warn
    assert r.detail.startswith("nothing can graduate: no enrolled authority signer")
    assert authority_signer_sudo_line("darwin" if doctor.sys.platform == "darwin" else "linux") in r.hint
    assert all(n in r.hint for n in AUTHORITY_SIGNER_REFUSALS)
    assert "remove-authority-signer --key SHA256:" in r.hint


def test_a_file_another_account_can_write_is_unsafe_and_the_hint_names_the_refusal(world):
    path = write_root(world, "laptop")
    path.chmod(0o666)
    [r] = rows(world)
    assert r.warn and r.detail.startswith("nothing can graduate:")
    assert "authority-signers-unsafe" in r.hint


def test_a_file_listing_no_authority_key_warns(world):
    write_root(world, "laptop", namespaces="levain-confirm")
    [r] = rows(world)
    assert r.warn and "lists no key for levain-authority" in r.detail


def test_a_listed_key_is_a_plain_pass(world):
    write_root(world, "laptop")
    [r] = rows(world)
    assert r.ok and not r.warn and r.detail.startswith("1 key(s) root lists for levain-authority")


def test_a_live_policy_its_signer_root_lists_gives_no_policy_row(world):
    create(world)
    write_root(world, "laptop")
    assert [r.name for r in rows(world)] == ["authority signers"]


def test_a_live_policy_no_listed_key_signed_warns_and_names_it(world):
    create(world)
    write_root(world, "phone")
    r_signers, r_pol = rows(world)
    assert r_signers.ok and not r_signers.warn
    assert r_pol.name == "graduation policies" and r_pol.ok and r_pol.warn
    assert "gp-0123456789ab" in r_pol.detail and "covers nothing" in r_pol.detail
    assert "born signer" in r_pol.detail, "a policy created by an unlisted key says its BORN signer is the gap"
    assert "enrol" in r_pol.hint and "revoke" in r_pol.hint


def test_a_policy_whose_current_signer_is_unlisted_is_renewed_not_re_enrolled(world):
    create(world)
    world.link(renew(world), "pk")                         # enrolled in the log, not root-listed
    write_root(world, "laptop")
    r_signers, r_pol = rows(world)
    assert r_signers.ok and not r_signers.warn
    assert "current signer" in r_pol.detail and "born signer" not in r_pol.detail
    assert "sign it again" in r_pol.hint


def test_with_no_file_the_policies_row_says_the_file_could_not_be_read(world):
    create(world)
    r_signers, r_pol = rows(world)
    assert r_signers.warn and "gp-0123456789ab" in r_pol.detail
    assert "could not be read" in r_pol.detail and "covers nothing" not in r_pol.detail


def test_a_policy_past_the_grant_ceiling_is_not_live_by_the_gates_own_predicate(world, monkeypatch):
    from datetime import timedelta

    from levain.autonomic import coverage as cov
    create(world)                                          # 30 days out: unexpired, ahead of the ceiling set below
    monkeypatch.setattr(cov, "GRADUATION_GRANT_MAX", timedelta(days=1))
    write_root(world, "phone")
    assert [r.name for r in rows(world)] == ["authority signers"]


def test_a_lapsed_policy_is_not_reported(world):
    create(world, days=1)
    write_root(world, "phone")
    from datetime import timedelta
    world.clock.now += timedelta(days=2)
    assert [r.name for r in rows(world)] == ["authority signers"]


def test_the_refusal_table_is_exactly_the_names_the_helper_raises():
    # Covers the functions whose name holds "signer" or "authority": every Refused(...) in them names itself with a
    # string literal (a name built any other way would escape this guard, so one that is not a literal fails here).
    # Refused calls elsewhere in the helper may be built (f-strings), and are outside this table.
    tree = ast.parse((REPO / "levain" / "slot" / "install_helper.py").read_text())
    fns = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and ("signer" in f.name or "authority" in f.name)]
    firsts = [c.args[0] for f in fns for c in ast.walk(f)
              if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "Refused" and c.args]
    assert firsts, "the authority-signer functions raise Refused"
    assert all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in firsts)
    raised = {a.value for a in firsts if a.value.startswith("authority-signer")}
    assert raised == set(AUTHORITY_SIGNER_REFUSALS)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_sudo_line_parses_as_the_helper_takes_it(host, world):
    pub = key(world, "laptop")
    pk = key(world, "pk")
    line = authority_signer_sudo_line(host)
    helper = str(layout.install_helper_path(host))
    assert line.startswith(f"sudo {helper} enrol-authority-signer ")
    filled = line.replace("--public-key '<type> <base64>'", f"--public-key '{pub}'")
    filled = filled.replace("[--passkey '<type> <base64>' ...] [--confirm]", f"--passkey '{pk}' --confirm")
    argv = filled.replace(f"sudo {helper} ", "").replace("YOUR_NAME", "phill")
    import shlex
    cmd = parse_install_argv(shlex.split(argv))
    assert (cmd.verb, cmd.principal, cmd.public_key, cmd.passkeys, cmd.confirm) == (
        "enrol-authority-signer", "phill", pub, (pk,), True)
    fp = "SHA256:" + "A" * 43
    assert parse_install_argv(["remove-authority-signer", "--key", fp]).key_fingerprint == fp


def _warn_one(world):
    [r, *rest] = rows(world)
    assert r.name == "authority signers" and r.warn and r.ok
    return r, rest


def test_an_oversized_file_warns_unreadable_and_is_never_parsed(world):
    path = write_root(world, "laptop")
    path.write_text(path.read_text() + "# " + "x" * (300 * 1024) + "\n")
    r, _ = _warn_one(world)
    assert "authority-signers-unreadable" in r.detail and "key(s) root lists" not in r.detail


def test_a_file_with_one_bad_utf8_line_warns_unreadable_not_a_pass(world):
    path = write_root(world, "laptop")
    path.write_bytes(path.read_bytes() + b"\xff\xfe not utf8\n")
    r, _ = _warn_one(world)
    assert "authority-signers-unreadable" in r.detail


def test_a_file_that_cannot_be_opened_warns_and_does_not_abort(world):
    path = write_root(world, "laptop")
    path.chmod(0)
    try:
        r, _ = _warn_one(world)
    finally:
        path.chmod(0o644)
    assert "authority-signers-unreadable" in r.detail


def test_a_dangling_link_there_warns_and_does_not_abort(world):
    d = signers(world).parent
    signers(world).symlink_to(d / "nowhere")
    r, _ = _warn_one(world)
    assert "nothing can graduate" in r.detail


def test_a_link_to_a_good_file_warns_with_the_helpers_bad_root(world):
    path = write_root(world, "laptop")
    real = path.parent / "real_signers"
    path.rename(real)
    path.symlink_to(real)
    r, _ = _warn_one(world)
    assert "bad-root" in r.detail


def test_a_corrupt_authority_log_is_a_warning_not_a_healthy_signers_row(world):
    create(world)
    write_root(world, "laptop")
    (world.root / "journal.db" if (world.root / "journal.db").exists() else
     next(world.root.glob("*.db"))).write_bytes(b"not a database" * 100)
    out = rows(world)
    assert [r.name for r in out] == ["authority signers", "authority log"]
    assert out[1].warn and out[1].ok and out[1].detail.startswith("could not check the authority log:")


def test_a_file_gone_between_the_stat_and_the_read_is_the_does_not_exist_row(world, monkeypatch):
    from levain.slot import install_helper

    write_root(world, "laptop")
    monkeypatch.setattr(install_helper, "_read_signers", lambda *a, **k: None)
    [r] = rows(world)
    assert r.name == "authority signers" and r.warn and "does not exist" in r.detail


def test_a_policy_with_both_signers_unlisted_names_both_gaps(world):
    create(world)
    world.link(renew(world), "pk")
    write_root(world, "phone")
    _, r_pol = rows(world)
    assert "born signer" in r_pol.detail and "current signer" in r_pol.detail
    assert "enrol" in r_pol.hint and "sign it again" in r_pol.hint


def test_a_policy_with_no_signer_key_is_skipped_not_an_exception(world, monkeypatch):
    from levain.autonomic import coverage as cov

    create(world)
    write_root(world, "phone")
    real = cov._key_of
    monkeypatch.setattr(cov, "_key_of", lambda k: None)
    try:
        out = rows(world)
    finally:
        monkeypatch.setattr(cov, "_key_of", real)
    assert [r.name for r in out][0] == "authority signers" and all("could not check" not in r.detail for r in out)


def test_nothing_in_the_check_can_abort_doctor(world, monkeypatch):
    write_root(world, "laptop")
    from levain.autonomic import coverage as cov

    def boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(cov, "authority_keys", boom)
    [r] = rows(world)
    assert r.name == "authority signers" and r.warn and "could not check" in r.detail and "boom" in r.detail


def test_a_journal_path_with_uri_metacharacters_still_reads_the_policies(world, tmp_path):
    create(world)
    write_root(world, "phone")
    inst = tmp_path / "in?st#x"
    (inst / ".levain").mkdir(parents=True)
    (inst / ".levain" / "cockpit").mkdir()
    for f in world.root.iterdir():
        os.symlink(f, inst / ".levain" / "cockpit" / f.name)
    out = _check_authority_signers(inst, signers=signers(world), now=world.clock.now)
    assert [r.name for r in out] == ["authority signers", "graduation policies"]


def test_a_missing_anchor_on_an_initialised_log_is_a_warning_not_silence(world, monkeypatch):
    from levain import authority as auth

    create(world)
    write_root(world, "laptop")

    def gone(*a, **k):
        raise auth.NotInitialised("anchor gone")
    monkeypatch.setattr(auth, "check_anchor", gone)
    out = rows(world)
    assert out and any(r.warn and "anchor" in r.detail for r in out)
