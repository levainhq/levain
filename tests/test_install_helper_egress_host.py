"""Host grants (board levain#66 slice S2, plan hgs2_plan_v2 T21 and T29's install half): ``enrol-authority-signer
--egress-host`` adds ``levain-egress-host`` to every line it writes and is the one root act that makes the grant store's
root-drawn identity: ``L/routes/grants/`` (root 0700) and ``L/routes/grants/store_id`` (32 lowercase hex, root 0600),
create-if-absent and never rewritten. No runtime verb makes either."""
from __future__ import annotations

import os
import re
import stat

import pytest

from levain.slot import Refused
from levain.slot import egress_ledger as EL
from levain.slot import install_helper as IH
from levain.slot.grammar import parse_install_argv
from tests.test_install_helper_authority_signer import (  # noqa: F401 - the module's scratch L, real keys and helpers
    AUTHORITY_NAMESPACE, L, _act, _enrol_argv, _path, keys)

EGRESS = "levain-egress-host"
STATE = ("routes", "grants")


@pytest.fixture
def LR(L):
    (L / "routes").mkdir(mode=0o755)
    (L / "routes").chmod(0o755)
    return L


def sid_path(L):
    return L / "routes" / "grants" / "store_id"


def egress_argv(*a, **kw):
    return _enrol_argv(*a, **kw) + ["--egress-host"]


def test_the_flag_parses_once_and_is_off_by_default(keys):
    assert parse_install_argv(_enrol_argv("phill", keys["a"][0])).egress_host is False
    assert parse_install_argv(egress_argv("phill", keys["a"][0])).egress_host is True
    with pytest.raises(Refused) as e:
        parse_install_argv(egress_argv("phill", keys["a"][0]) + ["--egress-host"])
    assert e.value.name == "duplicate-flag"


def test_every_line_written_names_the_egress_namespace_laptop_and_passkey_alike(LR, keys):
    got = _act(LR, egress_argv("phill", keys["a"][0], keys["pk"][0], confirm_ns=True))
    text = _path(LR).read_text().splitlines()
    assert text == [f'phill namespaces="{AUTHORITY_NAMESPACE},levain-confirm,{EGRESS}" {keys["a"][0]}',
                    f'phill namespaces="{AUTHORITY_NAMESPACE},{EGRESS}" {keys["pk"][0]}']
    assert [s["namespaces"] for s in got["signers"]] == [f"{AUTHORITY_NAMESPACE},levain-confirm,{EGRESS}",
                                                         f"{AUTHORITY_NAMESPACE},{EGRESS}"]


def test_the_flag_makes_the_ledger_dir_and_a_root_drawn_store_id_once(LR, keys):
    assert not (LR / "routes" / "grants").exists()
    got = _act(LR, egress_argv("phill", keys["a"][0]))
    d = LR / "routes" / "grants"
    st = os.lstat(d)
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.geteuid()
    f = sid_path(LR)
    fst = os.lstat(f)
    assert stat.S_ISREG(fst.st_mode) and stat.S_IMODE(fst.st_mode) == 0o600 and fst.st_uid == os.geteuid()
    assert re.fullmatch(r"[0-9a-f]{32}\n", f.read_text())
    assert got["egress_store_id"] == f.read_text().strip()
    assert sorted(p.name for p in d.iterdir()) == ["store_id"]           # no temp left behind


def test_the_runtime_reads_what_the_install_wrote(LR, keys):
    _act(LR, egress_argv("phill", keys["a"][0]))
    seams = type("S", (), {"chowned_uid": staticmethod(lambda uid: os.geteuid() if uid == 0 else uid)})()
    ctx = type("C", (), {"seams": seams})()
    fd = os.open(LR / "routes" / "grants", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert EL.read_store_id(ctx, fd) == sid_path(LR).read_text().strip()
    finally:
        os.close(fd)


def test_a_second_run_and_a_rerun_with_other_keys_leave_the_store_id_byte_identical(LR, keys):
    _act(LR, egress_argv("phill", keys["a"][0]))
    first, ino = sid_path(LR).read_bytes(), os.lstat(sid_path(LR)).st_ino
    again = _act(LR, egress_argv("phill", keys["a"][0]))
    assert again["written"] is False and again["egress_store_id"] == first.decode().strip()
    _act(LR, egress_argv("phill", keys["b"][0]))
    assert sid_path(LR).read_bytes() == first and os.lstat(sid_path(LR)).st_ino == ino


def test_without_the_flag_nothing_of_the_grant_store_is_made_and_no_egress_namespace_is_added(LR, keys):
    got = _act(LR, _enrol_argv("phill", keys["a"][0]))
    assert not (LR / "routes" / "grants").exists() and "egress_store_id" not in got
    assert EGRESS not in _path(LR).read_text()


def test_the_flag_upgrades_an_enrolled_line_in_place_and_the_reverse_needs_a_remove(LR, keys):
    _act(LR, _enrol_argv("phill", keys["a"][0]))
    got = _act(LR, egress_argv("phill", keys["a"][0]))
    assert got["written"] is True
    assert _path(LR).read_text() == f'phill namespaces="{AUTHORITY_NAMESPACE},{EGRESS}" {keys["a"][0]}\n'
    before = _path(LR).read_bytes()
    with pytest.raises(Refused) as e:
        _act(LR, _enrol_argv("phill", keys["a"][0]))
    assert e.value.name == "authority-signer-changed" and _path(LR).read_bytes() == before


@pytest.mark.parametrize("how", ["not-hex", "mode", "symlink", "directory", "empty", "long"])
def test_a_store_id_that_is_not_root_0600_hex_refuses_and_writes_no_signer_line(LR, keys, how, tmp_path):
    d = LR / "routes" / "grants"
    d.mkdir(mode=0o700)
    f = d / "store_id"
    if how == "not-hex":
        f.write_text("zz" * 16 + "\n")
        f.chmod(0o600)
    elif how == "mode":
        f.write_text("ab" * 16 + "\n")
        f.chmod(0o644)
    elif how == "symlink":
        real = tmp_path / "real"
        real.write_text("ab" * 16 + "\n")
        real.chmod(0o600)
        f.symlink_to(real)
    elif how == "directory":
        f.mkdir()
    elif how == "empty":
        f.write_text("")
        f.chmod(0o600)
    else:
        f.write_text("ab" * 40 + "\n")
        f.chmod(0o600)
    with pytest.raises(Refused) as e:
        _act(LR, egress_argv("phill", keys["a"][0]))
    assert e.value.name == "egress-state-exists"
    assert not _path(LR).exists()                       # the state is judged before the signers file is touched


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o750, 0o711])
def test_a_ledger_dir_that_is_not_0700_refuses(LR, keys, mode):
    d = LR / "routes" / "grants"
    d.mkdir()
    d.chmod(mode)
    with pytest.raises(Refused) as e:
        _act(LR, egress_argv("phill", keys["a"][0]))
    assert e.value.name == "egress-state-exists"


def test_a_ledger_dir_that_is_a_link_refuses_and_nothing_is_followed(LR, keys, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (LR / "routes" / "grants").symlink_to(elsewhere)
    with pytest.raises(Refused) as e:
        _act(LR, egress_argv("phill", keys["a"][0]))
    assert e.value.name in ("egress-state-exists", "egress-state-not-ready")
    assert list(elsewhere.iterdir()) == []


def test_a_routes_dir_that_is_not_there_refuses_not_ready_and_makes_nothing(L, keys):
    with pytest.raises(Refused) as e:
        _act(L, egress_argv("phill", keys["a"][0]))
    assert e.value.name == "egress-state-not-ready" and not (L / "routes").exists() and not _path(L).exists()


def test_a_group_writable_routes_dir_is_not_trusted(LR, keys):
    (LR / "routes").chmod(0o775)
    with pytest.raises(Refused) as e:
        _act(LR, egress_argv("phill", keys["a"][0]))
    assert e.value.name == "egress-state-not-ready" and not (LR / "routes" / "grants").exists()


def test_a_write_failure_before_the_link_leaves_no_store_id_and_no_temp(LR, keys, monkeypatch):
    real = os.link

    def boom(*a, **kw):
        raise OSError(28, "no space")

    monkeypatch.setattr(IH.os, "link", boom)
    with pytest.raises(Refused) as e:
        _act(LR, egress_argv("phill", keys["a"][0]))
    monkeypatch.setattr(IH.os, "link", real)
    assert e.value.name == "egress-state-write-failed"
    assert list((LR / "routes" / "grants").iterdir()) == []
    assert not _path(LR).exists()
    _act(LR, egress_argv("phill", keys["a"][0]))                # a re-run finishes it
    assert sid_path(LR).exists()


def test_a_store_id_made_by_a_racing_enrolment_wins_and_is_returned(LR, keys, monkeypatch):
    real = os.link
    won = {}

    def racing(src, dst, *a, **kw):
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kw["dst_dir_fd"])
        os.write(fd, b"ab" * 16 + b"\n")
        os.close(fd)
        won["id"] = "ab" * 16
        return real(src, dst, *a, **kw)                          # raises FileExistsError

    monkeypatch.setattr(IH.os, "link", racing)
    got = _act(LR, egress_argv("phill", keys["a"][0]))
    assert got["egress_store_id"] == won["id"] and sid_path(LR).read_text() == won["id"] + "\n"
    assert sorted(p.name for p in (LR / "routes" / "grants").iterdir()) == ["store_id"]
