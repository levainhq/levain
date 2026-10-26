"""The doctor's authority-signers rows (14b DESIGN_REV2 S5 rows 1 and 2): root's signers file and the graduation
policies it anchors, over a real authority log (``tests/test_authority_graduation_kind.World``) and a real file this
uid owns under the test-only owner override (``tests/conftest.py``)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from levain import doctor
from levain.doctor import (AUTHORITY_SIGNER_REFUSALS, _check_authority_signers, authority_signer_sudo_line)
from levain.slot import layout
from levain.slot.grammar import parse_install_argv

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World  # noqa: E402
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
    snap = create(world)
    write_root(world, "phone")
    r_signers, r_pol = rows(world)
    assert r_signers.ok and not r_signers.warn
    assert r_pol.name == "graduation policies" and r_pol.ok and r_pol.warn
    assert "gp-0123456789ab" in r_pol.detail and "covers nothing" in r_pol.detail
    assert snap is not None


def test_with_no_file_every_live_policy_is_named(world):
    create(world)
    r_signers, r_pol = rows(world)
    assert r_signers.warn and "gp-0123456789ab" in r_pol.detail


def test_a_lapsed_policy_is_not_reported(world):
    create(world, days=1)
    write_root(world, "phone")
    from datetime import timedelta
    world.clock.now += timedelta(days=2)
    assert [r.name for r in rows(world)] == ["authority signers"]


def test_the_refusal_table_is_exactly_the_names_the_helper_raises():
    src = (REPO / "levain" / "slot" / "install_helper.py").read_text()
    raised = set(re.findall(r'Refused\(\s*"(authority-signer[a-z-]*)"', src))
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
