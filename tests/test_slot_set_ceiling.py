"""The install helper's ``set-ceiling`` verb (1105-32), modelled on ``test_slot_set_limits``.

The operator pipes a ``levain-dispatch-ceiling/1`` or ``/2`` document on stdin; root checks it whole with the ticket
store's own check (:func:`levain.slot.tickets.parse_ceiling`) before the install lock, then under the lock writes the
canonical bytes to ``L/policy/dispatch_ceiling`` (0644, tmp + rename + directory fsync). :func:`levain.slot.tickets.
read_ceiling` reads what was written. These run as the lane uid in a scratch L: the file's owner is that uid, which
``read_ceiling`` is told stands in for root. No real sudo and no real helper run here."""
from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import stat
import functools

import pytest

from levain.contracts import canon
from levain.slot import Refused, install_helper as IH, layout, runtime_helper as RH, tickets as T
from levain.slot.grammar import VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text, run_set_ceiling

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
NAME = layout.POLICY_DISPATCH_CEILING
DOC2 = {"format": T.CEILING_FORMAT_2, "max_reserved_microdollars_per_run": 5_000_000, "max_open_tickets": 4,
        "max_runs_per_day": 20, "max_calls_per_run": 300, "max_tunnel_bytes_per_run": 1_000_000,
        "max_deadline_s": 3600}
DOC1 = {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 1_000_000, "max_open_tickets": 2,
        "max_runs_per_day": 5}


# --- the grammar and the built list ---------------------------------------------------------------------------------

def test_set_ceiling_parses_with_no_argument():
    assert "set-ceiling" in VERBS
    assert parse_install_argv(["set-ceiling"]).verb == "set-ceiling"
    with pytest.raises(Refused) as e:
        parse_install_argv(["set-ceiling", "x"])
    assert e.value.name == "extra-argv"


def test_set_ceiling_is_built():
    assert "set-ceiling" in IH.BUILT_VERBS
    assert not any("set-ceiling" in line for line in IH.helper_pending())
    assert Seams().ceiling is run_set_ceiling


def test_the_path_has_one_home():
    for host in ("darwin", "linux"):
        assert layout.policy_dispatch_ceiling_path(host) == layout.STORE[host] / "policy" / "dispatch_ceiling"


# --- a scratch L ------------------------------------------------------------------------------------------------------

@pytest.fixture()
def L(tmp_path):
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    os.chmod(L / "policy", 0o755)
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
    return L


class _Lock:
    def __init__(self, on_enter=None):
        self.calls = []
        self.on_enter = on_enter

    def __call__(self, host):
        lk = self

        class _Cm:
            def __enter__(self):
                lk.calls.append("lock")
                if lk.on_enter:
                    lk.on_enter()

            def __exit__(self, *a):
                lk.calls.append("unlock")
        return _Cm()


def _stdin(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return lambda n: raw[:n]


def _trusted(st):
    return st.st_uid == ME and not st.st_mode & 0o022


def _rs(L, trusted=None):
    return RH.Seams(host="darwin", store=L, trusted=trusted or _trusted)


def _seams(L, obj=DOC2, *, lock=None, trusted=None, **kw):
    lock = lock or _Lock()
    run = functools.partial(run_set_ceiling, store_seams=_rs(L, trusted), **kw)

    def gather(*a):
        raise AssertionError("set-ceiling reached the install plan's generic path")
    return Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock, stdin=_stdin(obj), ceiling=run,
                 gather=gather), lock


def _main(seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["set-ceiling"], env=ENV, seams=seams, out=out, err=err)
    assert out.getvalue().count("\n") == 1
    return rc, json.loads(out.getvalue()), err.getvalue()


def _read(L):
    return T.read_ceiling(L / "policy" / NAME, owner=ME)


# --- set, read back by the ticket store's reader, replace ------------------------------------------------------------

def test_a_v2_document_is_written_canonical_0644_and_the_reader_accepts_it(L):
    seams, lock = _seams(L)
    rc, out, err = _main(seams)
    assert rc == 0, err
    path = L / "policy" / NAME
    data = path.read_bytes()
    assert data == canon.canonical_bytes(DOC2)
    assert out == {"ok": True, "verb": "set-ceiling", "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                   "old": None, "new": DOC2}
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o644 and st.st_uid == ME
    assert not os.path.lexists(path.with_name(path.name + ".tmp"))
    assert lock.calls == ["lock", "unlock"]
    assert _read(L) == T.Ceiling(5_000_000, 4, 20, 300, 1_000_000, 3600)


def test_a_v1_document_is_accepted_as_the_reader_accepts_it(L):
    rc, out, err = _main(_seams(L, DOC1)[0])
    assert rc == 0, err
    assert _read(L) == T.Ceiling(1_000_000, 2, 5)
    assert out["new"] == DOC1


def test_the_answer_reports_the_old_and_the_new_values(L):
    assert _main(_seams(L, DOC1)[0])[0] == 0
    rc, out, _ = _main(_seams(L, DOC2)[0])
    assert rc == 0 and out["old"] == DOC1 and out["new"] == DOC2


def test_root_re_serialises_a_non_canonical_document(L):
    raw = b'{ "max_runs_per_day": 5,\n  "format": "levain-dispatch-ceiling/1",\n "max_open_tickets": 2,' \
          b' "max_reserved_microdollars_per_run": 1000000 }\n'
    rc, out, err = _main(_seams(L, raw)[0])
    assert rc == 0, err
    assert (L / "policy" / NAME).read_bytes() == canon.canonical_bytes(DOC1)


def test_set_replaces_an_unusable_file_and_its_mode_comes_back(L):
    path = L / "policy" / NAME
    path.write_bytes(b"garbage")
    os.chmod(path, 0o666)
    with pytest.raises(Refused):
        _read(L)
    rc, out, _ = _main(_seams(L)[0])
    assert rc == 0 and out["old"] is None
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o644
    assert _read(L).max_open_tickets == 4


def test_no_default_is_written_by_anything_but_a_document(L):
    assert not (L / "policy" / NAME).exists()
    _main(_seams(L, {"format": T.CEILING_FORMAT})[0])
    assert not (L / "policy" / NAME).exists()


# --- refusals: by name, before the lock, the old file standing ------------------------------------------------------

def _with(**kw):
    d = dict(DOC2)
    d.update(kw)
    return d


def _without(key):
    d = dict(DOC2)
    del d[key]
    return d


@pytest.mark.parametrize("raw,name", [
    (_with(max_open_tickets=-1), "ceiling-malformed"),
    (_with(max_open_tickets=1.5), "ceiling-malformed"),
    (_with(max_open_tickets="4"), "ceiling-malformed"),
    (_with(max_open_tickets=True), "ceiling-malformed"),
    (_with(max_open_tickets=None), "ceiling-malformed"),
    (_with(max_runs_per_day=2 ** 53), "ceiling-malformed"),
    (_with(max_calls_per_run=T.CALLS_MAX + 1), "ceiling-malformed"),
    (_with(extra=1), "ceiling-malformed"),
    (_without("max_deadline_s"), "ceiling-malformed"),
    (_with(format="levain-dispatch-ceiling/3"), "ceiling-malformed"),
    ({**DOC1, "max_calls_per_run": 1}, "ceiling-malformed"),
    ([], "ceiling-malformed"),
    (b'{"format":"levain-dispatch-ceiling/1","format":"levain-dispatch-ceiling/1"}', "ceiling-malformed"),
    (b"not json", "ceiling-malformed"),
    (b"", "ceiling-malformed"),
    (b" " * (T.CEILING_MAX + 1), "ceiling-oversize"),
    (json.dumps(_with(pad="x" * T.CEILING_MAX)).encode(), "ceiling-oversize"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_a_bad_document_is_refused_by_name_before_the_lock(raw, name, L):
    path = L / "policy" / NAME
    path.write_bytes(canon.canonical_bytes(DOC1))
    before = path.read_bytes()
    seams, lock = _seams(L, raw)
    rc, out, err = _main(seams)
    assert (rc, out) == (1, {"ok": False, "refused": name}), err
    assert lock.calls == []
    assert path.read_bytes() == before


def test_stdin_is_read_once_bounded_to_the_cap_plus_one(L):
    asked = []

    def read(n):
        asked.append(n)
        return canon.canonical_bytes(DOC2)
    seams, _ = _seams(L)
    seams.stdin = read
    assert _main(seams)[0] == 0
    assert asked == [T.CEILING_MAX + 1]


def test_a_caller_who_is_not_the_operator_is_refused_before_stdin(L):
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))

    def read(n):
        raise AssertionError("stdin was read")
    seams, lock = _seams(L)
    seams.stdin = read
    rc, out, _ = _main(seams)
    assert (rc, out) == (1, {"ok": False, "refused": "operator-mismatch"})
    assert lock.calls == []
    assert not (L / "policy" / NAME).exists()


def test_not_set_up_is_refused(L):
    (L / "policy" / "operator").unlink()
    assert _main(_seams(L)[0])[1] == {"ok": False, "refused": "not-set-up"}


def test_the_operator_is_checked_again_under_the_lock(L):
    lock = _Lock(on_enter=lambda: (L / "policy" / "operator").unlink())
    rc, out, _ = _main(_seams(L, lock=lock)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "not-set-up"})
    assert lock.calls == ["lock", "unlock"]
    assert not (L / "policy" / NAME).exists()


def test_not_root_is_refused_first(L):
    seams, lock = _seams(L)
    seams.euid = lambda: 501
    assert _main(seams)[1] == {"ok": False, "refused": "not-root"}
    assert lock.calls == []


# --- the directory and the owner ---------------------------------------------------------------------------------------

def test_a_group_writable_policy_dir_is_refused_and_the_old_file_stands(L):
    path = L / "policy" / NAME
    path.write_bytes(canon.canonical_bytes(DOC1))
    before = path.read_bytes()
    os.chmod(L / "policy", 0o775)
    rc, out, _ = _main(_seams(L)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "bad-root"})
    assert path.read_bytes() == before


def test_a_policy_dir_of_another_owner_is_refused(L):
    path = L / "policy" / NAME
    rc, out, _ = _main(_seams(L, trusted=lambda st: st.st_ino != os.stat(L / "policy").st_ino)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "bad-root"})
    assert not path.exists()


def test_a_policy_dir_that_is_a_link_is_refused(L, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.chmod(real, 0o755)
    (real / "operator").write_text(operator_record_text(MY_NAME, ME))
    os.rename(L / "policy", tmp_path / "moved")
    os.symlink(real, L / "policy")
    rc, out, _ = _main(_seams(L)[0])
    assert rc == 1 and out["ok"] is False
    assert not (real / NAME).exists()


# --- durability -------------------------------------------------------------------------------------------------------

def test_a_failure_before_the_rename_refuses_and_the_old_file_stands(L):
    path = L / "policy" / NAME
    path.write_bytes(canon.canonical_bytes(DOC1))
    before = path.read_bytes()

    def fsync(fd):
        raise OSError(5, "Input/output error")
    rc, out, err = _main(_seams(L, fsync=fsync)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "ceiling-write-failed"})
    assert path.read_bytes() == before
    assert not os.path.lexists(path.with_name(path.name + ".tmp"))


def test_a_directory_fsync_failure_is_ok_with_a_not_durable_warning(L):
    seen = []

    def fsync(fd):
        seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        if seen[-1]:
            raise OSError(5, "Input/output error")
    rc, out, err = _main(_seams(L, fsync=fsync)[0])
    assert rc == 0 and out["verb"] == "set-ceiling", err
    assert seen == [False, True]
    assert "warning" in err and "not durable" in err and "run it again" in err
    assert (L / "policy" / NAME).read_bytes() == canon.canonical_bytes(DOC2)


# --- the run order: its own branch, a live lease never refuses it ----------------------------------------------------

def test_set_ceiling_never_reaches_the_generic_path_or_the_lease_count(L):
    (L / "sessions").mkdir()
    rc, out, err = _main(_seams(L)[0])      # _seams' gather raises: the generic path gathers under the lock
    assert rc == 0, err


def test_run_set_ceiling_writes_under_the_store_seams_root(L):
    got = run_set_ceiling(ME, "darwin", read=_stdin(DOC2), lock=lambda: _Lock()("darwin"), store_seams=_rs(L),
                          err=io.StringIO())
    assert got["verb"] == "set-ceiling" and (L / "policy" / NAME).exists()
