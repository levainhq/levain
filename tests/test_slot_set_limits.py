"""The install helper's ``set-limits`` verb (slot limits DESIGN §3, 1025-6/limits; head rulings R0 + R1 F5-F7).

The operator pipes a ``levain-limits/1`` document on stdin; root checks it whole with the one slot parser
(:func:`levain.consent.limits.slot_doc`), all before the install lock, then under the lock writes the document's
canonical bytes to ``L/policy/limits`` (0644, tmp + rename + directory fsync). The queue broker's
:class:`levain.qbroker.limits.PolicyLimits` reads what was written. These run as the lane uid in a scratch L: the
file's owner is that uid, which PolicyLimits is told stands in for root."""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import pwd
import stat
from pathlib import Path

import pytest

from levain.consent import limits as CL
from levain.contracts import canon
from levain.qbroker.limits import POLICY_LIMITS, LimitsUnavailable, PolicyLimits
from levain.slot import Refused, install_helper as IH, layout, runtime_helper as RH
from levain.slot.grammar import LEASE_EXEMPT, VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text, run_set_limits

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
DOC = {"format": CL.LIMITS_FORMAT, "default": {"calls": 500, "active_hours": 4, "repeat": 6},
       "codex": {"calls": 100}}


# --- the grammar and the built list ---------------------------------------------------------------------------------

def test_set_limits_parses_with_no_argument_and_is_lease_exempt():
    assert "set-limits" in VERBS and "set-limits" in LEASE_EXEMPT
    assert parse_install_argv(["set-limits"]).verb == "set-limits"
    for argv, name in ((["set-limits", "default"], "extra-argv"), (["set-limits", "--replace"], "extra-argv")):
        with pytest.raises(Refused) as e:
            parse_install_argv(argv)
        assert e.value.name == name


def test_set_limits_is_built():
    assert "set-limits" in IH.BUILT_VERBS
    assert not any("set-limits" in line for line in IH.helper_pending())


def test_the_policy_path_has_one_home():
    assert layout.POLICY_LIMITS_NAME == POLICY_LIMITS        # written twice (root cannot import the reader), pinned
    for host in ("darwin", "linux"):
        assert layout.policy_limits_path(host) == layout.STORE[host] / "policy" / POLICY_LIMITS


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


def _rs(L):
    return RH.Seams(host="darwin", store=L, trusted=lambda st: True)


def _seams(L, obj=DOC, *, lock=None, **kw):
    lock = lock or _Lock()
    run = functools.partial(run_set_limits, store_seams=_rs(L), **kw)

    def gather(*a):
        raise AssertionError("set-limits reached the install plan's generic path")
    return Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock, stdin=_stdin(obj), limits=run,
                 gather=gather), lock


def _main(seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["set-limits"], env=ENV, seams=seams, out=out, err=err)
    assert out.getvalue().count("\n") == 1
    return rc, json.loads(out.getvalue()), err.getvalue()


def _policy(L):
    return PolicyLimits(L / "policy", owner=ME)


# --- set, read back by the broker's reader, replace ---------------------------------------------------------------------

def test_set_writes_canonical_0644_and_the_broker_reads_it(L):
    seams, lock = _seams(L)
    rc, out, err = _main(seams)
    assert rc == 0, err
    path = L / "policy" / POLICY_LIMITS
    data = path.read_bytes()
    assert data == canon.canonical_bytes(DOC)
    assert out == {"ok": True, "verb": "set-limits", "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o644 and st.st_uid == ME
    assert not os.path.lexists(path.with_name(path.name + ".tmp"))
    assert lock.calls == ["lock", "unlock"]
    assert _policy(L)("codex", "s") == {"calls": 100.0, "active_hours": 4.0, "repeat": 6.0}
    assert _policy(L)("claude_code", "s") == {"calls": 500.0, "active_hours": 4.0, "repeat": 6.0}


def test_root_re_serialises_a_non_canonical_document(L):
    raw = b'{ "codex": {"calls": 100},\n  "format": "levain-limits/1" }\n'
    rc, out, err = _main(_seams(L, raw)[0])
    assert rc == 0, err
    assert (L / "policy" / POLICY_LIMITS).read_bytes() == b'{"codex":{"calls":100},"format":"levain-limits/1"}'


def test_a_second_set_replaces_the_whole_document_and_a_live_reader_sees_it(L):
    reader = _policy(L)
    assert _main(_seams(L)[0])[0] == 0
    assert reader("codex", "s")["calls"] == 100.0
    assert _main(_seams(L, {"format": CL.LIMITS_FORMAT, "default": {"calls": 7}})[0])[0] == 0
    assert reader("codex", "s") == {"calls": 7.0, "active_hours": CL.DEFAULTS["active_hours"],
                                    "repeat": CL.DEFAULTS["repeat"]}


def test_an_empty_document_is_explicit_defaults(L):
    assert _main(_seams(L, {"format": CL.LIMITS_FORMAT})[0])[0] == 0
    assert _policy(L)("codex", "s") == {k: float(CL.DEFAULTS[k]) for k in CL.SLOT_KINDS}


def test_set_replaces_an_unusable_file_and_its_owner_and_mode_come_back(L):
    path = L / "policy" / POLICY_LIMITS
    path.write_bytes(b"garbage")
    os.chmod(path, 0o666)
    with pytest.raises(LimitsUnavailable):
        _policy(L)("codex", "s")
    assert _main(_seams(L)[0])[0] == 0
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o644
    assert _policy(L)("codex", "s")["calls"] == 100.0


# --- refusals: by name, before the lock, the old file standing ------------------------------------------------------

@pytest.mark.parametrize("raw,name", [
    ({"format": CL.LIMITS_FORMAT, "default": {"active_hours": 1.5}}, "limits-malformed"),
    ({"format": CL.LIMITS_FORMAT, "default": {"fresh_tokens": 10}}, "limits-not-counted:fresh-tokens"),
    ({"format": CL.LIMITS_FORMAT, "codex": {"cache_read_tokens": 10}}, "limits-not-counted:cache-read-tokens"),
    ({"format": CL.LIMITS_FORMAT, "default": {"calls": 0}}, "limits-value:default.calls"),
    ({"format": CL.LIMITS_FORMAT, "default": {"repeat": 11}}, "limits-value:default.repeat"),
    ({"format": CL.LIMITS_FORMAT, "default": {"repeat": 1}}, "limits-value:default.repeat"),
    ({"format": CL.LIMITS_FORMAT, "codex": {"calls": True}}, "limits-value:codex.calls"),
    ({"format": CL.LIMITS_FORMAT, "codex": []}, "limits-value:codex"),
    ({"format": CL.LIMITS_FORMAT, "gemini": {}}, "limits-key:gemini"),
    ({"format": CL.LIMITS_FORMAT, "default": {"loops": 3}}, "limits-key:loops"),
    ({"format": CL.LIMITS_FORMAT, "codex": {"active_hours": 0}}, "limits-value:codex.active-hours"),
    ({"format": CL.LIMITS_FORMAT, "claude_code": {"active_hours": 10**9 + 1}}, "limits-value:claude-code.active-hours"),
    ({"format": CL.LIMITS_FORMAT, "__x__": {}}, "limits-key:x"),
    ({"format": CL.LIMITS_FORMAT, "été": {}}, "limits-key:t"),
    ({"format": CL.LIMITS_FORMAT, "_": {}}, "limits-key:key"),
    ({"format": CL.LIMITS_FORMAT, "a" * 300: {}}, "limits-key:" + "a" * 40),
    ({"format": "levain-limits/2"}, "limits-malformed"),
    (b'{"format":"levain-limits/1","format":"levain-limits/1"}', "limits-malformed"),
    (b"not json", "limits-malformed"),
    (b"", "limits-malformed"),
    (b" " * (CL.SLOT_LIMITS_MAX + 1), "limits-oversize"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_a_bad_document_is_refused_by_name_before_the_lock(raw, name, L):
    path = L / "policy" / POLICY_LIMITS
    path.write_bytes(canon.canonical_bytes(DOC))
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
        return canon.canonical_bytes(DOC)
    seams, _ = _seams(L)
    seams.stdin = read
    assert _main(seams)[0] == 0
    assert asked == [CL.SLOT_LIMITS_MAX + 1]


def test_a_caller_who_is_not_the_operator_is_refused_before_stdin(L):
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))

    def read(n):
        raise AssertionError("stdin was read")
    seams, lock = _seams(L)
    seams.stdin = read
    rc, out, _ = _main(seams)
    assert (rc, out) == (1, {"ok": False, "refused": "operator-mismatch"})
    assert lock.calls == []
    assert not (L / "policy" / POLICY_LIMITS).exists()


def test_not_set_up_is_refused(L):
    (L / "policy" / "operator").unlink()
    assert _main(_seams(L)[0])[1] == {"ok": False, "refused": "not-set-up"}


def test_the_operator_is_checked_again_under_the_lock(L):
    lock = _Lock(on_enter=lambda: (L / "policy" / "operator").unlink())    # an undo between the two checks
    rc, out, _ = _main(_seams(L, lock=lock)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "not-set-up"})
    assert lock.calls == ["lock", "unlock"]
    assert not (L / "policy" / POLICY_LIMITS).exists()


def test_not_root_is_refused_first(L):
    seams, lock = _seams(L)
    seams.euid = lambda: 501
    assert _main(seams)[1] == {"ok": False, "refused": "not-root"}
    assert lock.calls == []


# --- durability (F6) -------------------------------------------------------------------------------------------------

def test_a_failure_before_the_rename_refuses_and_the_old_file_stands(L):
    path = L / "policy" / POLICY_LIMITS
    path.write_bytes(canon.canonical_bytes({"format": CL.LIMITS_FORMAT}))
    before = path.read_bytes()

    def fsync(fd):
        raise OSError(5, "Input/output error")
    rc, out, err = _main(_seams(L, fsync=fsync)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "limits-write-failed"})
    assert path.read_bytes() == before
    assert not os.path.lexists(path.with_name(path.name + ".tmp"))


def test_a_directory_fsync_failure_is_ok_with_a_not_durable_warning(L):
    seen = []

    def fsync(fd):
        seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        if seen[-1]:
            raise OSError(5, "Input/output error")
    rc, out, err = _main(_seams(L, fsync=fsync)[0])
    assert rc == 0 and out["verb"] == "set-limits", err
    assert seen == [False, True]
    assert "warning" in err and "not durable" in err and "run it again" in err
    assert (L / "policy" / POLICY_LIMITS).read_bytes() == canon.canonical_bytes(DOC)    # in force now


# --- the run order (F5): its own branch, a live lease never refuses it ------------------------------------------------

def test_set_limits_never_reaches_the_generic_path_or_the_lease_count(L):
    # _seams' gather raises: the generic path gathers the host's facts (and counts live leases) under the lock
    (L / "sessions").mkdir()
    rc, out, err = _main(_seams(L)[0])
    assert rc == 0, err


def test_the_seams_default_is_the_real_verb():
    assert Seams().limits is run_set_limits


def test_run_set_limits_writes_under_the_store_seams_root(L):
    got = run_set_limits(ME, "darwin", read=_stdin(DOC), lock=lambda: _Lock()("darwin"), store_seams=_rs(L),
                         err=io.StringIO())
    assert got["verb"] == "set-limits" and (L / "policy" / POLICY_LIMITS).exists()


# --- undo (R1 F7): the file goes by path and owner, whatever its bytes ------------------------------------------------

def _run_undo(tmp_path, put=None):
    from levain.slot import install_helper as IH2
    from levain.slot.grammar import parse_install_argv as parse
    from tests.test_undo_remove import RECORD, S0, _darwin_host, _facts, _shape

    h, live, shared = _darwin_host(tmp_path)
    target = live.at(layout.policy_limits_path("darwin"))
    if put is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        put(target)
    plan = IH2.plan_undo(parse(["undo"]), _facts("darwin", _shape(*RECORD)), accounts={"_levain_s0": S0},
                         shared_groups=shared, live=live, end=lambda uid, name: (True, ""))
    whys = [s.why for s in plan.steps]
    out = {}
    for s in plan.steps:
        if s.call is not None and s.write is None:
            ok, why = s.call()
            assert ok, (s.why, why)
            out[s.why] = why
    return target, whys, out, live


@pytest.mark.parametrize("data", [canon.canonical_bytes(DOC), b"garbage, not a document", b""])
def test_undo_removes_policy_limits_usable_or_not(tmp_path, data):
    target, whys, _, _ = _run_undo(tmp_path, lambda p: p.write_bytes(data))
    assert not os.path.lexists(target)
    mine = f"remove the slot limits {layout.policy_limits_path('darwin')}"
    pool = next(w for w in whys if w.startswith(f"remove {layout.pool_config_path('darwin')}"))
    assert whys.index(pool) < whys.index(mine) < whys.index("remove the install record")


def test_undo_with_no_policy_limits_runs_nothing_for_it(tmp_path):
    target, _, out, _ = _run_undo(tmp_path)
    assert not os.path.lexists(target)
    assert out[f"remove the slot limits {layout.policy_limits_path('darwin')}"] == ""


def test_undo_leaves_a_link_at_policy_limits_in_place(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"x")
    target, _, out, live = _run_undo(tmp_path, lambda p: os.symlink(elsewhere, p))
    assert os.path.islink(target) and elsewhere.exists()
    assert "left in place" in out[f"remove the slot limits {layout.policy_limits_path('darwin')}"]
    assert ("policy-limits", str(layout.policy_limits_path("darwin"))) in live.skips


@pytest.mark.parametrize("host", ("darwin", "linux"))
def test_setup_writes_no_policy_limits(host, monkeypatch):
    """No file is the defaults (DESIGN §3): setup never writes one, so a re-run never resets the operator's."""
    from levain.firing import hands
    from tests.test_slot_install_plan import _facts as setup_facts, _setup

    real = hands.shutil.which          # the other host's tools resolve, as test_slot_install_plan's fixture does
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")

    plan = IH.plan_setup(_setup(1, 1), setup_facts(host))
    target = str(layout.policy_limits_path(host))
    assert plan.steps
    for s in plan.steps:
        assert not (s.write and str(s.write[0]) == target), s.why
        assert target not in s.argv, s.why
