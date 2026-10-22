"""The install helper's credential verbs (S1 session 3b-5a, design 1.8 Bootstrap): ``set-credential <name>`` and
``remove-credential <name>``. Root validates the object, then FORKS; the child closes what it inherited, resets cwd,
umask and environment, drops to keys (a no-op seam here: these run as the lane uid, the real drop is 3b-8a's), and only
then opens ``L/keys``, creates ``cred/`` and writes under the lock. Root opens nothing below ``L/keys/``."""
from __future__ import annotations

import functools
import io
import json
import os
import pwd
import stat

import pytest

from levain.broker import creds as C
from levain.broker.norm import Refused as BrokerRefused
from levain.slot import Refused, install_helper as IH, records as R, runtime_helper as RH
from levain.slot.grammar import CREDENTIAL_NAME_RE, LEASE_EXEMPT, VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text, read_credential, run_credential

NAME = "anthropic"
ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
S1 = "s-0123456789abcdef"


def _fields(**over: object) -> dict:
    d = {"format": C.FORMAT, "name": NAME, "hosts": ["api.anthropic.com"],
         "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": "sk-test-one"}
    d.update(over)
    return d


def _refresh(token_url: str = "https://api.example.com/token") -> dict:
    return {"format": C.FORMAT, "name": NAME, "hosts": ["api.example.com"],
            "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "oauth2-refresh",
            "access": "at-1", "expires_at": 1, "refresh": {"token_url": token_url, "client_id": "cid",
                                                           "refresh_token": "rt-1"}}


# --- the grammar ---------------------------------------------------------------------------------------------------

def test_both_verbs_parse_and_carry_the_name():
    for verb in ("set-credential", "remove-credential"):
        assert verb in VERBS and verb in LEASE_EXEMPT
        cmd = parse_install_argv([verb, NAME])
        assert (cmd.verb, cmd.credential) == (verb, NAME)
    assert parse_install_argv(["set-credential", "a" * 48]).credential == "a" * 48


@pytest.mark.parametrize("argv,name", [
    (["set-credential"], "missing-argv"),
    (["remove-credential"], "missing-argv"),
    (["set-credential", NAME, NAME], "extra-argv"),
    (["set-credential", "Anthropic"], "bad-token"),
    (["set-credential", "a" * 49], "bad-token"),
    (["set-credential", "../x"], "bad-token"),
    (["remove-credential", "a_b"], "bad-token"),
    (["remove-credential", "a\n"], "bad-token"),
    (["set-credential", "a.json"], "bad-token"),
])
def test_grammar_refusals(argv, name):
    with pytest.raises(Refused) as e:
        parse_install_argv(argv)
    assert e.value.name == name


def test_the_grammar_name_is_the_credential_layers_name():
    # grammar cannot import creds (creds -> upstream -> h11, an optional extra; the CLI side imports grammar), so the
    # pattern is written twice and pinned equal here.
    assert CREDENTIAL_NAME_RE.pattern == C.NAME_RE.pattern
    assert CREDENTIAL_NAME_RE.flags == C.NAME_RE.flags


def test_both_verbs_are_built():
    assert {"set-credential", "remove-credential"} <= set(IH.BUILT_VERBS)
    assert not any("credential" in line for line in IH.helper_pending())


# --- root's validation, before any fork ----------------------------------------------------------------------------

def _stdin(obj) -> "callable":
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return lambda n: raw[:n]


@pytest.mark.parametrize("obj,name", [
    (_fields(rev="0" * 32), "bad-credential:rev"),
    (_refresh("https://evil.example.net/token"), "bad-credential:refresh-token-url-host"),
    (_fields(hosts=[]), "bad-credential:hosts"),
    (_fields(hosts="api.anthropic.com"), "bad-credential:hosts"),
    (_fields(name="other"), "bad-credential:name"),
    (_fields(kind="password"), "bad-credential:kind"),
    (_fields(extra=1), "bad-credential:keys"),
    (_fields(secret="sk\nx"), "bad-credential:secret-charset"),
    (dict(_refresh(), expires_at="soon"), "bad-credential:expires-at"),
    (b'{"format": 1, "format": 2}', "bad-credential:json"),
    (b"\xff\xfe", "bad-credential:json"),
    (b"not json", "bad-credential:json"),
    (b"[" * 60000, "bad-credential:json"),                       # under the cap, past the recursion limit
    (b" " * (C.FILE_CAP + 1), "bad-credential:size"),
    ([1, 2], "bad-credential:format"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_root_refuses_a_bad_object_before_the_lock_and_before_any_fork(obj, name):
    calls = []

    def lock(host):
        calls.append("lock")
        raise AssertionError("the lock was taken")

    def fork():
        calls.append("fork")
        raise AssertionError("forked")

    seams = Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock, stdin=_stdin(obj),
                  credential=functools.partial(run_credential, fork=fork))
    out, err = io.StringIO(), io.StringIO()
    assert main(["set-credential", NAME], env=ENV, seams=seams, out=out, err=err) == RH.EXIT_REFUSED
    assert out.getvalue() == json.dumps({"ok": False, "refused": name}, separators=(",", ":")) + "\n"
    assert calls == []
    assert "sk-test-one" not in err.getvalue()          # the detail names a field, never a value


def test_read_credential_returns_the_object_without_a_rev():
    assert read_credential(NAME, _stdin(_fields())) == _fields()


# --- a scratch L, the real fork, a no-op drop ----------------------------------------------------------------------

@pytest.fixture()
def L(tmp_path):
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
    (L / "keys").mkdir(mode=0o700)
    (L / "sessions").mkdir()
    return L


class _Lock:
    def __init__(self):
        self.held = 0
        self.calls = []

    def __call__(self, host):
        lk = self

        class _Cm:
            def __enter__(self):
                lk.calls.append("lock")
                lk.held += 1

            def __exit__(self, *a):
                lk.calls.append("unlock")
                lk.held -= 1
        return _Cm()


def _seams(L, obj=None, *, drop=None, lock=None, **kw):
    rs = RH.Seams(host="darwin", store=L, trusted=lambda st: True, account_uid=lambda n: ME,
                  group_gid=lambda n: os.getgid())
    dropped = []
    run = functools.partial(run_credential, store_seams=rs,
                            drop=drop or (lambda uid, gid: dropped.append((uid, gid))), **kw)
    return Seams(euid=lambda: 0, host=lambda: "darwin", lock=lock or _Lock(),
                 stdin=_stdin(obj if obj is not None else _fields()), credential=run), dropped


def _main(argv, seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, env=ENV, seams=seams, out=out, err=err)
    assert out.getvalue().count("\n") == 1            # exactly one line: the child never writes stdout
    return rc, json.loads(out.getvalue()), err.getvalue()


def _observe(L, name=NAME):
    fd = os.open(L / "keys" / "cred", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return C.CredStore(fd).observe(name)
    finally:
        os.close(fd)


def test_set_then_set_then_remove_on_a_scratch_L(L):
    lock = _Lock()
    seams, dropped = _seams(L, lock=lock)
    rc, out, err = _main(["set-credential", NAME], seams)
    assert rc == 0, err
    assert set(out) == {"ok", "verb", "credential", "rev"} and out["verb"] == "set-credential"
    rev1 = out["rev"]
    assert C.REV_RE.fullmatch(rev1)
    cred = L / "keys" / "cred"
    assert stat.S_IMODE(os.lstat(cred).st_mode) == 0o700                    # created by the child, umask 077
    f = os.lstat(cred / f"{NAME}.json")
    assert stat.S_ISREG(f.st_mode) and stat.S_IMODE(f.st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(cred / ".lock").st_mode) == 0o600
    assert _observe(L).rev == rev1
    assert dropped == [] and lock.calls == ["lock", "unlock"]               # the drop ran in the CHILD, not here

    seams, _ = _seams(L, _fields(secret="sk-test-two"))
    rc, out, err = _main(["set-credential", NAME], seams)
    assert rc == 0, err
    assert out["rev"] != rev1
    seen = _observe(L)                                                      # D2: the observation serves the new rev
    assert (seen.rev, seen.secret) == (out["rev"], "sk-test-two")

    rc, out, err = _main(["remove-credential", NAME], _seams(L)[0])
    assert (rc, out) == (0, {"ok": True, "verb": "remove-credential", "credential": NAME, "removed": True}), err
    assert not os.path.lexists(cred / f"{NAME}.json")
    with pytest.raises(BrokerRefused) as e:
        _observe(L)
    assert e.value.name == "credential-unavailable"
    rc, out, _ = _main(["remove-credential", NAME], _seams(L)[0])
    assert (rc, out["removed"]) == (0, False)


def test_the_child_drops_to_keys_before_it_opens_anything_below_L_keys(L):
    """The drop seam runs in the child: it records into a file (the child's memory is not the parent's), and it sees
    no `cred/` yet, the cwd `/`, umask 077 and an empty environment, with root's fds closed."""
    probe = L.parent / "probe.json"
    keep = os.open(os.devnull, os.O_RDONLY)               # an inherited fd above 2 the child must have closed
    os.set_inheritable(keep, True)

    def drop(uid, gid):
        um = os.umask(0o077)
        try:
            os.fstat(keep)
            fd_open = True
        except OSError:
            fd_open = False
        probe.write_text(json.dumps({"uid": uid, "gid": gid, "cred": os.path.lexists(L / "keys" / "cred"),
                                     "cwd": os.getcwd(), "umask": um, "env": dict(os.environ), "fd": fd_open,
                                     "pid": os.getpid()}))
    try:
        rc, out, err = _main(["set-credential", NAME], _seams(L, drop=drop)[0])
    finally:
        os.close(keep)
    assert rc == 0, err
    p = json.loads(probe.read_text())
    assert (p["uid"], p["gid"]) == (ME, os.getgid())
    assert p["pid"] != os.getpid()
    assert p["cred"] is False and p["cwd"] == "/" and p["umask"] == 0o077 and p["env"] == {} and p["fd"] is False


def test_a_drop_that_fails_writes_nothing(L):
    def drop(uid, gid):
        raise Refused("drop-assert", "seteuid(0) succeeded")
    rc, out, err = _main(["set-credential", NAME], _seams(L, drop=drop)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "credential-drop-failed"})
    assert not os.path.lexists(L / "keys" / "cred")


def test_a_missing_keys_dir_is_refused_and_nothing_is_created(L):
    os.rmdir(L / "keys")
    rc, out, _ = _main(["set-credential", NAME], _seams(L)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "credential-dir-failed"})
    assert not os.path.lexists(L / "keys")


def test_a_linked_keys_dir_or_cred_dir_is_not_followed(L, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (L / "keys" / "cred").symlink_to(elsewhere)
    rc, out, _ = _main(["set-credential", NAME], _seams(L)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "credential-dir-failed"})
    assert os.listdir(elsewhere) == []
    os.unlink(L / "keys" / "cred")
    os.rmdir(L / "keys")
    (L / "keys").symlink_to(elsewhere)
    rc, out, _ = _main(["set-credential", NAME], _seams(L)[0])
    assert (rc, out) == (1, {"ok": False, "refused": "credential-dir-failed"})
    assert os.listdir(elsewhere) == []


def test_a_live_lease_does_not_refuse_them(L):
    (L / "sessions" / f"{S1}.json").write_bytes(R.state_bytes(R.SessionState(
        session=S1, generation=0, state=R.ALLOCATED, claims={}, lease_live=True, event_kind="state",
        updated_at="2026-10-22T12:00:00Z")))
    assert IH.count_live_leases(L / "sessions") == 1
    assert _main(["set-credential", NAME], _seams(L)[0])[0] == 0
    assert _main(["remove-credential", NAME], _seams(L)[0])[0] == 0


def test_only_the_operator_and_only_once_set_up(L):
    forks = []

    def fork():
        forks.append(1)
        raise AssertionError("forked")

    other = {"SUDO_UID": str(ME + 1)}
    seams = _seams(L, fork=fork)[0]
    out = io.StringIO()
    assert main(["set-credential", NAME], env=other, seams=seams, out=out, err=io.StringIO()) == 1
    assert out.getvalue() == '{"ok":false,"refused":"operator-mismatch"}\n'
    os.unlink(L / "policy" / "operator")
    assert _main(["remove-credential", NAME], seams)[1] == {"ok": False, "refused": "not-set-up"}
    assert forks == []


def test_a_child_that_dies_is_internal(L):
    def drop(uid, gid):
        os._exit(9)
    rc, out, _ = _main(["set-credential", NAME], _seams(L, drop=drop)[0])
    assert (rc, out) == (RH.EXIT_INTERNAL, {"ok": False, "refused": "internal"})


def test_a_not_durable_commit_is_success_with_a_warning(L, monkeypatch):
    def bad_dir_fsync(fd):
        raise OSError(5, "EIO")
    monkeypatch.setattr(C, "dir_fsync", bad_dir_fsync)
    monkeypatch.setattr(C.WriteSeams.__init__, "__kwdefaults__",
                        dict(C.WriteSeams.__init__.__kwdefaults__, fsync_dir=bad_dir_fsync))
    rc, out, err = _main(["set-credential", NAME], _seams(L)[0])
    assert rc == 0 and _observe(L).rev == out["rev"]
    assert "not durable" in err
