"""`levain doctor`'s ``queue answers`` row: the answers directory must be the operator's, in the broker's group, with
no write bit for any other principal. Only the exact old shape warns; every other shape fails."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from levain import doctor

from levain.slot.install_helper import operator_record_text

ME, GID, OLD = 501, 4321, 8765
OPERATOR = "opname"


def st(mode=0o2750, uid=ME, gid=GID, kind=stat.S_IFDIR):
    return os.stat_result((kind | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))


@pytest.fixture
def store(tmp_path):
    """A store as setup leaves it: ``policy/operator`` names the operator, whose uid is ``ME``."""
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "operator").write_text(operator_record_text(OPERATOR, ME))
    return tmp_path


def row(store, stat_result=None, *, uid=ME, missing=False, gid=GID, host="darwin"):
    def lstat(p):
        assert Path(p) == store / "queue" / "answers"
        if missing:
            raise FileNotFoundError(p)
        return stat_result
    rows = doctor._check_queue_answers(host=host, store=store, uid=uid, lstat=lstat, qbroker_gid=lambda: gid,
                                     legacy_gid=lambda: OLD)
    assert len(rows) == 1
    return rows[0]


def test_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_queue_answers(host="darwin", store=tmp_path, uid=ME) == []


@pytest.mark.parametrize("mode", [0o2750, 0o750])
def test_the_right_shape_passes(store, mode):
    r = row(store, st(mode=mode), host="darwin")
    assert r.ok and not r.warn and not r.neutral


def test_the_exact_old_shape_warns_and_does_not_fail(store):
    r = row(store, st(gid=OLD, mode=0o2770))
    assert r.ok and r.warn
    assert "dispatch can still create files" in r.detail and "levain upgrade" in r.hint


@pytest.mark.parametrize("what,kw", [
    ("dispatch-owned 2750", {"uid": ME + 1}),
    ("root:root 0700", {"uid": 0, "gid": 0, "mode": 0o700}),
    ("a regular file", {"kind": stat.S_IFREG}),
    ("a symlink", {"kind": stat.S_IFLNK}),
    ("legacy group, mode 0750", {"gid": OLD, "mode": 0o750}),
    ("legacy group, 2750", {"gid": OLD, "mode": 0o2750}),
    ("an unknown group", {"gid": GID + 1}),
    ("world writable", {"mode": 0o2757}),
    ("group writable in the right group", {"mode": 0o2770}),
    ("operator cannot write", {"mode": 0o2550}),
    ("broker cannot read", {"mode": 0o2700}),
    ("old owner not the operator", {"gid": OLD, "mode": 0o2770, "uid": ME + 1}),
])
def test_any_other_shape_fails(store, what, kw):
    r = row(store, st(**kw))
    assert not r.ok and not r.warn, what
    assert "levain upgrade" in r.hint


def test_0750_is_right_on_macos_only(store):
    assert row(store, st(mode=0o750), host="darwin").ok
    r = row(store, st(mode=0o750), host="linux")
    assert not r.ok


def test_2750_passes_on_linux(store):
    r = row(store, st(mode=0o2750), host="linux")
    assert r.ok and not r.warn


def test_the_ok_row_says_acls_were_not_read(store):
    r = row(store, st())
    assert "ACL" in r.detail and "not read" in r.detail


def test_a_missing_directory_is_a_failure(store):
    r = row(store, missing=True)
    assert not r.ok and "levain upgrade" in r.hint


def test_root_reads_a_healthy_directory_as_the_recorded_operators(store):
    r = row(store, st(), uid=0)
    assert r.ok and not r.neutral and not r.warn


@pytest.mark.parametrize("what,kw", [
    ("world writable", {"mode": 0o2757}),
    ("dispatch-owned", {"uid": ME + 1}),
    ("root-owned", {"uid": 0}),
    ("the old group", {"gid": OLD, "mode": 0o2750}),
])
def test_root_fails_a_bad_directory_instead_of_passing_it_neutral(store, what, kw):
    r = row(store, st(**kw), uid=0)
    assert not r.ok and not r.neutral, what


def test_root_sees_the_old_shape_warn(store):
    r = row(store, st(gid=OLD, mode=0o2770), uid=0)
    assert r.ok and r.warn and not r.neutral


@pytest.mark.parametrize("uid", [0, ME + 7])
def test_owner_is_the_recorded_operator_for_every_invoker(store, uid):
    """An admin who is not the operator reads a correct directory as correct."""
    r = row(store, st(), uid=uid)
    assert r.ok and not r.warn and not r.neutral


def test_owning_the_directory_does_not_make_an_admin_the_operator(store):
    r = row(store, st(uid=ME + 7), uid=ME + 7)
    assert not r.ok and OPERATOR in r.detail


def test_an_owner_mismatch_names_the_operator_not_you(store):
    r = row(store, st(uid=ME + 1), uid=ME)
    assert not r.ok
    assert f"owner {OPERATOR}" in r.detail and "owner you" not in r.detail


MALFORMED = [b"", b"not an operator record", b'{"format":"levain-operator/1","name":"x","uid":0}']


@pytest.mark.parametrize("write", [None, *MALFORMED])
def test_a_missing_or_malformed_operator_record_fails_for_every_invoker(tmp_path, write):
    """No record, no operator to name as owner: a FAIL (a neutral row hides its hint and exits 0, and nothing else
    reads the record). A record naming uid 0 is rejected by the parser, so it is the malformed case."""
    (tmp_path / "policy").mkdir()
    if write is not None:
        (tmp_path / "policy" / "operator").write_bytes(write)
    for uid in (0, ME):
        r = row(tmp_path, st(), uid=uid)
        assert not r.ok and not r.neutral and not r.warn
        assert str(tmp_path / "policy" / "operator") in r.detail, r.detail
        assert "no one can be named as owner" in r.detail, r.detail


HELPERS = (("darwin", "/usr/local/libexec/levain/install-helper"), ("linux", "/usr/libexec/levain/install-helper"))


def _break(tmp_path, case):
    """The four states a record can be in once it is not a usable file: absent, malformed bytes, an entry that is not
    a regular file (a directory; a dangling link), and a regular file that cannot be read."""
    (tmp_path / "policy").mkdir()
    rec = tmp_path / "policy" / "operator"
    if case == "missing":
        pass
    elif case == "malformed":
        rec.write_bytes(b"not an operator record")
    elif case == "directory":
        rec.mkdir()
    elif case == "link":
        rec.symlink_to(tmp_path / "nowhere")
    return rec


@pytest.mark.parametrize("case", ["missing", "malformed", "directory", "link"])
def test_the_operator_record_hint_never_prints_a_pasteable_setup_command(tmp_path, case):
    """`setup` parses an absent --root as none and rewrites policy/roots from its arguments (grammar.py, then
    install_helper's roots step), so a printed command that omits the roots can erase the install's configured roots.
    The remedy is words that send the person to read the stored values first, never a command line."""
    _break(tmp_path, case)
    for host, helper in HELPERS:
        r = row(tmp_path, st(), host=host)
        assert f"{helper} setup --slots" not in r.hint and "sudo" not in r.hint, r.hint
        assert "roots" in r.hint, r.hint
        assert f"{tmp_path}/policy" in r.hint, r.hint
        assert "levain upgrade" not in r.hint, r.hint
        assert helper in r.hint and "setup" in r.hint, r.hint


@pytest.mark.parametrize("case", ["missing", "malformed", "directory", "link"])
def test_the_hint_names_every_setting_setup_rewrites(tmp_path, case):
    _break(tmp_path, case)
    h = row(tmp_path, st()).hint
    for word in ("slots", "verifier slots", "roots", "BEFORE running it", "--operator", "--root"):
        assert word in h, (word, h)


def test_the_hint_says_what_to_do_first_for_each_kind_of_entry(tmp_path):
    hints = {}
    for case in ("missing", "malformed", "directory", "link"):
        d = tmp_path / case
        d.mkdir()
        _break(d, case)
        hints[case] = row(d, st()).hint
    assert "remove" not in hints["missing"].lower() and "re-creates the record" in hints["missing"]
    assert "malformed file must be removed as root first" in hints["malformed"]
    assert "nothing else restores it" in hints["malformed"]
    assert "directory, not a regular file" in hints["directory"] and "remove it as root first" in hints["directory"]
    assert "link, not a regular file" in hints["link"] and "remove it as root first" in hints["link"]
    assert len(set(hints.values())) == 4, hints


@pytest.mark.parametrize("code, errname", [(errno.EIO, "EIO"), (errno.ENOTDIR, "ENOTDIR")])
def test_a_record_read_failure_names_the_errno_and_never_setup(store, monkeypatch, code, errname):
    """EIO / ENOTDIR on a record that is there: setup cannot fix the path, so it is not named; the person is sent to
    fix the path and run doctor again."""
    real = Path.read_bytes

    def fail(self):
        if self.name == "operator":
            raise OSError(code, os.strerror(code), str(self))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", fail)
    r = row(store, st())
    assert not r.ok and not r.neutral
    assert f"could not be read ({errname})" in r.detail, r.detail
    assert f"could not be read ({errname}); fix the path and run `levain doctor` again" in r.hint, r.hint
    assert "setup" not in r.hint and "levain upgrade" not in r.hint and "roots" not in r.hint, r.hint


def test_an_unknown_host_falls_back_to_the_generic_helper_name(tmp_path):
    """`layout.install_helper_path` indexes a table: a host it does not know must not raise inside doctor."""
    _break(tmp_path, "missing")
    rows = doctor._check_queue_answers(host="plan9", store=tmp_path, uid=ME, lstat=lambda p: st(),
                                       qbroker_gid=lambda: GID, legacy_gid=lambda: OLD)
    assert len(rows) == 1 and not rows[0].ok
    assert "The install helper `setup`" in rows[0].hint and "/" not in rows[0].hint.split("`setup`")[0], rows[0].hint


def test_an_unreadable_operator_record_is_neutral_and_the_detail_says_who_to_run_as(store, monkeypatch):
    real = Path.read_bytes

    def deny(self):
        if self.name == "operator":
            raise PermissionError(13, "denied", str(self))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", deny)
    r = row(store, st(), uid=ME + 1)
    assert r.ok and r.neutral and not r.warn
    assert "Run `levain doctor` as the operator or root." in r.detail, r.detail


def test_an_operator_record_that_is_not_a_file_fails(store):
    (store / "policy" / "operator").unlink()
    (store / "policy" / "operator").mkdir()
    r = row(store, st())
    assert not r.ok and "is a directory, not a regular file" in r.detail, r.detail


def test_a_wrong_directory_says_the_operators_answers_not_yours(store):
    r = row(store, st(uid=ME + 1))
    assert not r.ok
    assert "the operator's answers" in r.detail and "your answers" not in r.detail


def test_the_missing_group_hint_is_the_upgrade_verb(store):
    def gone():
        raise KeyError("_levain_qbroker")
    rows = doctor._check_queue_answers(host="darwin", store=store, uid=ME, lstat=lambda p: st(),
                                       qbroker_gid=gone, legacy_gid=lambda: OLD)
    assert len(rows) == 1 and not rows[0].ok
    assert "levain upgrade" in rows[0].hint
