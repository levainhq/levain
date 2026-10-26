"""14b S5a: the install helper's root signer verbs, ``enrol-authority-signer`` and ``remove-authority-signer``
(DESIGN_REV2 S5, DESIGN_REV3 §7 and §10 S5, HEAD_RULINGS_R1 R6 H1).

Real ``ssh-keygen`` keys and WebAuthn passkey stand-ins; a scratch L standing for the store, under the account home
(``tests/autonomic_confirm_keys.anchor_scratch``) so the written file passes ``trust_anchor_problem`` under the
test-only owner override (``tests/conftest.py``). The journey at the end is S4's (``tests/test_graduation_journey``)
with the file this verb writes as the gate's signers file."""
from __future__ import annotations

import atexit
import functools
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from levain import authority
from levain.autonomic import confirm
from levain.autonomic.coverage import AUTHORITY_NAMESPACE, authority_keys, parse_root_signers
from levain.slot import Refused, install_helper as IH, root_tree
from levain.slot import runtime_helper as RH
from levain.autonomic import coverage
from levain.slot.grammar import KEY_TYPE_NAMES, LEASE_EXEMPT, MAX_PASSKEYS, PRINCIPAL_RE, VERBS, parse_install_argv
from levain.slot.install_helper import Seams, main, operator_record_text
from levain.trust_anchor import trust_anchor_problem

pytest.importorskip("cryptography")
from tests.autonomic_confirm_keys import anchor_scratch  # noqa: E402
from tests.test_passkey_custody_derived import WebAuthnKey  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
ENV = {"SUDO_UID": str(ME)}
ENROL, REMOVE = "enrol-authority-signer", "remove-authority-signer"
BOTH = f'"{AUTHORITY_NAMESPACE},{confirm.NAMESPACE}"'


def _scratch() -> Path:
    d = anchor_scratch("s5a-root-")
    atexit.register(shutil.rmtree, d, True)
    return d


@pytest.fixture()
def L():
    L = _scratch() / "L"
    (L / "policy").mkdir(parents=True)
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))
    return L


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("k")
    out = {}
    for name, typ in (("a", "ed25519"), ("b", "ed25519"), ("ec", "ecdsa"), ("rsa", "rsa")):
        subprocess.run(["ssh-keygen", "-q", "-t", typ, "-N", "", "-C", "c", "-f", str(d / name)], check=True)
        line = (d / f"{name}.pub").read_text().split()
        fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(d / f"{name}.pub")], check=True,
                            capture_output=True, text=True).stdout.split()[1]
        out[name] = (f"{line[0]} {line[1]}", fp)
    for name in ("pk", "pk2"):
        k = WebAuthnKey()
        out[name] = (" ".join(k.public_key.split()[:2]), k.fp)
    return out


class _Lock:
    def __init__(self, on_enter=None):
        self.calls = []
        self.on_enter = on_enter

    def __call__(self, host=None):
        lk = self

        class _Cm:
            def __enter__(self):
                lk.calls.append("lock")
                if lk.on_enter is not None:
                    lk.on_enter()

            def __exit__(self, *a):
                lk.calls.append("unlock")
        return _Cm()


def _rs(L):
    return RH.Seams(host="darwin", store=L, trusted=lambda st: True)


def _path(L) -> Path:
    return L / root_tree.authority_signers_path("darwin").relative_to(root_tree.STORE["darwin"])


def _enrol_argv(principal, key, *passkeys, confirm_ns=False):
    argv = [ENROL, "--principal", principal, "--public-key", key]
    for pk in passkeys:
        argv += ["--passkey", pk]
    return argv + (["--confirm"] if confirm_ns else [])


def _act(L, argv, lock=None, **kw):
    return IH.run_authority_signer(parse_install_argv(argv), ME, "darwin", lock=lock or _Lock(), store_seams=_rs(L),
                                   **kw)


def _main(argv, seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, env=ENV, seams=seams, out=out, err=err)
    assert out.getvalue().count("\n") == 1
    return rc, json.loads(out.getvalue()), err.getvalue()


def _no_gather(*a):
    raise AssertionError("reached the install plan's generic path")


# --- the grammar --------------------------------------------------------------------------------------------------------

def test_both_verbs_are_in_the_grammar_built_and_lease_exempt():
    for verb in (ENROL, REMOVE):
        assert verb in VERBS and verb in IH.BUILT_VERBS and verb in LEASE_EXEMPT
    assert not any(ENROL in line or REMOVE in line for line in IH.helper_pending())


def test_enrol_parses_its_flags(keys):
    cmd = parse_install_argv(_enrol_argv("phill", keys["a"][0], keys["pk"][0], keys["pk2"][0], confirm_ns=True))
    assert (cmd.verb, cmd.principal, cmd.public_key) == (ENROL, "phill", keys["a"][0])
    assert cmd.passkeys == (keys["pk"][0], keys["pk2"][0]) and cmd.confirm is True
    bare = parse_install_argv(_enrol_argv("phill", keys["a"][0]))
    assert bare.passkeys == () and bare.confirm is False


def test_remove_parses_its_key(keys):
    cmd = parse_install_argv([REMOVE, "--key", keys["a"][1]])
    assert (cmd.verb, cmd.key_fingerprint) == (REMOVE, keys["a"][1])


@pytest.mark.parametrize("argv,name", [
    ([ENROL], "missing-argv"),
    ([ENROL, "--principal", "phill"], "missing-argv"),
    ([ENROL, "--public-key", "ssh-ed25519 AAAA"], "missing-argv"),
    ([ENROL, "--principal"], "missing-argv"),
    ([ENROL, "--principal", "phill", "--principal", "x", "--public-key", "ssh-ed25519 AAAA"], "duplicate-flag"),
    ([ENROL, "--principal", "phill", "--public-key", "ssh-ed25519 AAAA", "--confirm", "--confirm"], "duplicate-flag"),
    ([ENROL, "--principal", "phill", "--public-key", "ssh-ed25519 AAAA", "--root", "/x"], "extra-argv"),
    ([ENROL, "--principal", "-phill", "--public-key", "ssh-ed25519 AAAA"], "bad-token"),
    ([ENROL, "--principal", "ph ill", "--public-key", "ssh-ed25519 AAAA"], "bad-token"),
    ([ENROL, "--principal", "ssh-ed25519", "--public-key", "ssh-ed25519 AAAA"], "bad-token"),
    ([ENROL, "--principal", "phill", "--public-key", "ssh-ed25519 AAAA comment"], "bad-token"),
    ([ENROL, "--principal", "phill", "--public-key", 'command="x" ssh-ed25519 AAAA'], "bad-token"),
    ([ENROL, "--principal", "phill", "--public-key", "ssh-ed25519  AAAA"], "bad-token"),
    ([ENROL, "--principal", "phill", "--public-key", "ssh-ed25519 AAAA", "--passkey", "x\ny z"], "bad-token"),
    ([REMOVE], "missing-argv"),
    ([REMOVE, "--key", "SHA256:short"], "bad-token"),
    ([REMOVE, "--key", "SHA256:" + "A" * 43, "--key", "SHA256:" + "B" * 43], "extra-argv"),
    ([REMOVE, "--fingerprint", "SHA256:" + "A" * 43], "extra-argv"),
])
def test_a_bad_argv_is_refused_by_name(argv, name):
    with pytest.raises(Refused) as e:
        parse_install_argv(argv)
    assert e.value.name == name


def test_the_same_key_twice_is_refused(keys):
    with pytest.raises(Refused) as e:
        parse_install_argv(_enrol_argv("phill", keys["pk"][0], keys["pk"][0]))
    assert e.value.name == "duplicate-key"


def test_at_most_max_passkeys(keys):
    many = [f"sk-ecdsa-sha2-nistp256@openssh.com {'A' * 4 * (i + 1)}" for i in range(MAX_PASSKEYS + 1)]
    with pytest.raises(Refused) as e:
        parse_install_argv(_enrol_argv("phill", keys["a"][0], *many))
    assert e.value.name == "out-of-range"


def test_the_principal_pattern_is_the_confirm_gates_signer_and_the_key_types_the_readers():
    assert PRINCIPAL_RE.pattern == confirm._SIGNER.pattern
    assert KEY_TYPE_NAMES == coverage.KEY_TYPES


# --- enrol --------------------------------------------------------------------------------------------------------------

def test_enrol_writes_the_lines_root_0644_and_the_reader_lists_them(L, keys):
    lock = _Lock()
    got = _act(L, _enrol_argv("phill", keys["a"][0], keys["pk"][0], confirm_ns=True), lock=lock)
    assert lock.calls == ["lock", "unlock"]
    p = _path(L)
    assert p.read_text() == (f'phill namespaces={BOTH} {keys["a"][0]}\n'
                             f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["pk"][0]}\n')
    st = os.lstat(p)
    assert st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o644
    signers = parse_root_signers(p.read_bytes())
    assert authority_keys(signers) == {keys["a"][0], keys["pk"][0]}
    assert [sorted(s.namespaces) for s in signers] == [sorted([AUTHORITY_NAMESPACE, confirm.NAMESPACE]),
                                                       [AUTHORITY_NAMESPACE]]
    assert got["verb"] == ENROL and got["written"] is True
    assert got["signers"] == [{"fingerprint": keys["a"][1], "namespaces": f"{AUTHORITY_NAMESPACE},{confirm.NAMESPACE}"},
                              {"fingerprint": keys["pk"][1], "namespaces": AUTHORITY_NAMESPACE}]
    import hashlib
    assert got["digest"] == "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    assert trust_anchor_problem(p, what="allowed-signers file", owners=confirm._SIGNERS_FILE_OWNERS)[0] is None


def test_without_confirm_the_laptop_line_is_authority_only(L, keys):
    _act(L, _enrol_argv("phill", keys["a"][0]))
    assert _path(L).read_text() == f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n'


def test_enrol_again_writes_nothing(L, keys):
    argv = _enrol_argv("phill", keys["a"][0], keys["pk"][0])
    _act(L, argv)
    ino = os.lstat(_path(L)).st_ino
    got = _act(L, argv)
    assert got["written"] is False and os.lstat(_path(L)).st_ino == ino


def test_a_wrong_mode_is_written_again(L, keys):
    argv = _enrol_argv("phill", keys["a"][0])
    _act(L, argv)
    os.chmod(_path(L), 0o664)
    assert _act(L, argv)["written"] is True and stat.S_IMODE(os.lstat(_path(L)).st_mode) == 0o644


def test_enrol_updates_a_keys_line_in_place_and_keeps_every_other_line(L, keys):
    p = _path(L)
    hand = (f"# root's own note\n"
            f'other namespaces="{confirm.NAMESPACE}" {keys["b"][0]}\n'
            f'old namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n'
            f"not a line at all\n")
    p.write_text(hand)
    os.chmod(p, 0o644)
    _act(L, _enrol_argv("phill", keys["a"][0], keys["pk"][0], confirm_ns=True))
    assert p.read_text() == (f"# root's own note\n"
                             f'other namespaces="{confirm.NAMESPACE}" {keys["b"][0]}\n'
                             f'phill namespaces={BOTH} {keys["a"][0]}\n'
                             f"not a line at all\n"
                             f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["pk"][0]}\n')


def test_a_second_line_for_the_same_key_is_dropped(L, keys):
    p = _path(L)
    p.write_text(f'x namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n'
                 f'y,z namespaces="{confirm.NAMESPACE}",cert-authority {keys["a"][0]} trailing\n')
    _act(L, _enrol_argv("phill", keys["a"][0]))
    assert p.read_text() == f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n'


def test_a_file_with_no_final_newline_gains_one(L, keys):
    p = _path(L)
    p.write_text(f'other namespaces="{confirm.NAMESPACE}" {keys["b"][0]}')
    _act(L, _enrol_argv("phill", keys["a"][0]))
    assert p.read_text() == (f'other namespaces="{confirm.NAMESPACE}" {keys["b"][0]}\n'
                             f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n')


@pytest.mark.parametrize("laptop,passkey", [
    ("rsa", None),          # a type no laptop custody admits
    ("pk", None),           # a passkey's material as the laptop key
    ("a", "b"),             # an ssh key as a passkey
    ("a", "ec"),            # an ecdsa ssh key as a passkey (not an sk key)
    ("a", "sshsk"),         # an OpenSSH FIDO key (application ssh:) as a passkey
])
def test_a_key_custody_problem_refuses_is_refused_before_any_write(L, keys, laptop, passkey):
    pk = _ssh_sk_key(b"ssh:") if passkey == "sshsk" else keys[passkey][0] if passkey else None
    argv = _enrol_argv("phill", keys[laptop][0], *([pk] if pk else []))
    lock = _Lock()
    with pytest.raises(Refused) as e:
        _act(L, argv, lock=lock)
    assert e.value.name == "authority-signer-key" and lock.calls == [] and not _path(L).exists()


def _ssh_sk_key(application: bytes) -> str:
    """An ``sk-ecdsa`` key line with ``application`` (an OpenSSH FIDO key's is ``ssh:...``), on a real P-256 point."""
    import base64
    import struct

    def string(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    point = base64.b64decode(" ".join(WebAuthnKey().public_key.split()[:2]).split()[1])
    # the passkey stand-in's own curve and point, its application replaced
    w = IH._Wire(point)
    w.string()
    curve, q = w.string(), w.string()
    blob = string(IH.PASSKEY_KEY_TYPE.encode()) + string(curve) + string(q) + string(application)
    return f"{IH.PASSKEY_KEY_TYPE} {base64.b64encode(blob).decode()}"


def test_the_custody_rule_root_holds_is_the_authority_logs(keys):
    """Root imports no authority store, so :func:`IH.authority_key_problem` is written there; it admits exactly what
    ``levain.authority.custody_problem`` admits: a laptop key some stated laptop custody takes, a passkey that is
    custody ``passkey`` for a display-phone key with a passkey's material."""
    samples = [keys[n][0] for n in ("a", "ec", "rsa", "pk", "pk2")] + [
        _ssh_sk_key(b"ssh:"), _ssh_sk_key(b"ssh:levain"), "sk-ssh-ed25519@openssh.com AAAA", "ecdsa-sha2-nistp384 AAAA",
        "ecdsa-sha2-nistp256 AAAA", "ssh-dss AAAA"]
    for key in samples:
        t = key.split()[0]
        laptop = any(authority.custody_problem(authority.LAPTOP, c, t, public_key=key) is None
                     for c in authority.CUSTODIES if c != authority.UNSTATED)
        phone = (authority.custody_problem(authority.DISPLAY_PHONE, authority.PASSKEY, t, public_key=key) is None
                 and authority.custody_mismatch(key, authority.PASSKEY) is None)
        assert (IH.authority_key_problem(key, passkey=False) is None) == laptop, key
        assert (IH.authority_key_problem(key, passkey=True) is None) == phone, key
        assert IH.is_passkey_material(key) == authority.is_passkey_material(key), key
    assert IH.authority_key_problem(keys["pk"][0], passkey=True) is None
    assert IH.authority_key_problem(_ssh_sk_key(b"ssh:"), passkey=False) is None


def test_the_namespaces_and_passkey_type_are_the_readers():
    assert IH.AUTHORITY_NAMESPACE == AUTHORITY_NAMESPACE and IH.CONFIRM_NAMESPACE == confirm.NAMESPACE
    assert authority._CUSTODY_KEY_TYPES[authority.PASSKEY] == {IH.PASSKEY_KEY_TYPE}


def test_an_ecdsa_laptop_key_enrols(L, keys):
    got = _act(L, _enrol_argv("phill", keys["ec"][0]))
    assert got["signers"][0]["fingerprint"] == keys["ec"][1]


def test_a_key_ssh_keygen_cannot_read_is_refused(L):
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="))
    assert e.value.name == "authority-signer-verifier" and not _path(L).exists()


def test_enrol_refuses_a_caller_who_is_not_the_operator(L, keys):
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", keys["a"][0]))
    assert e.value.name == "operator-mismatch" and not _path(L).exists()


def test_enrol_refuses_before_setup(L, keys):
    (L / "policy" / "operator").unlink()
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", keys["a"][0]))
    assert e.value.name == "not-set-up"


def test_the_operator_is_checked_again_under_the_lock(L, keys):
    def undo_between():
        (L / "policy" / "operator").unlink()
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", keys["a"][0]), lock=_Lock(on_enter=undo_between))
    assert e.value.name == "not-set-up" and not _path(L).exists()


def test_a_failed_write_refuses_and_the_earlier_file_stands(L, keys, monkeypatch):
    _act(L, _enrol_argv("phill", keys["a"][0]))
    before = _path(L).read_bytes()

    def fail(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(IH.R, "write_durably", fail)
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", keys["b"][0]))
    assert e.value.name == "authority-signers-write-failed" and _path(L).read_bytes() == before


def test_a_rename_that_is_not_durable_answers_ok_and_warns(L, keys):
    def no_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
    err = io.StringIO()
    got = _act(L, _enrol_argv("phill", keys["a"][0]), err=err, fsync=no_dir_fsync)
    assert got["written"] is True and "not durable" in err.getvalue()
    assert authority_keys(parse_root_signers(_path(L).read_bytes())) == {keys["a"][0]}


def test_a_link_at_the_files_path_is_refused(L, keys, tmp_path):
    (tmp_path / "elsewhere").write_text("")
    os.symlink(tmp_path / "elsewhere", _path(L))
    with pytest.raises(Refused) as e:
        _act(L, _enrol_argv("phill", keys["a"][0]))
    assert e.value.name == "bad-root" and (tmp_path / "elsewhere").read_text() == ""


# --- remove -------------------------------------------------------------------------------------------------------------

def test_remove_takes_out_every_line_of_that_key_and_keeps_the_rest(L, keys):
    _act(L, _enrol_argv("phill", keys["a"][0], keys["pk"][0], keys["pk2"][0]))
    p = _path(L)
    p.write_text(p.read_text() + f'hand,made namespaces="{confirm.NAMESPACE}",cert-authority {keys["pk"][0]}\n')
    got = IH.run_authority_signer(parse_install_argv([REMOVE, "--key", keys["pk"][1]]), ME, "darwin", lock=_Lock(),
                                  store_seams=_rs(L))
    assert got == {"verb": REMOVE, "fingerprint": keys["pk"][1], "removed": 2, "written": True,
                   "digest": got["digest"]}
    assert p.read_text() == (f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["a"][0]}\n'
                             f'phill namespaces="{AUTHORITY_NAMESPACE}" {keys["pk2"][0]}\n')
    assert stat.S_IMODE(os.lstat(p).st_mode) == 0o644


def test_remove_of_a_key_not_listed_writes_nothing(L, keys):
    _act(L, _enrol_argv("phill", keys["a"][0]))
    ino = os.lstat(_path(L)).st_ino
    got = IH.run_authority_signer(parse_install_argv([REMOVE, "--key", keys["b"][1]]), ME, "darwin", lock=_Lock(),
                                  store_seams=_rs(L))
    assert (got["removed"], got["written"]) == (0, False) and os.lstat(_path(L)).st_ino == ino


def test_remove_with_no_file_is_ok_and_makes_none(L, keys):
    got = IH.run_authority_signer(parse_install_argv([REMOVE, "--key", keys["a"][1]]), ME, "darwin", lock=_Lock(),
                                  store_seams=_rs(L))
    assert (got["removed"], got["written"], got["digest"]) == (0, False, None) and not _path(L).exists()


def test_remove_of_the_last_key_leaves_a_file_that_lists_none(L, keys):
    _act(L, _enrol_argv("phill", keys["a"][0]))
    IH.run_authority_signer(parse_install_argv([REMOVE, "--key", keys["a"][1]]), ME, "darwin", lock=_Lock(),
                            store_seams=_rs(L))
    assert _path(L).read_bytes() == b"" and authority_keys(parse_root_signers(b"")) == frozenset()


def test_remove_refuses_a_caller_who_is_not_the_operator(L, keys):
    _act(L, _enrol_argv("phill", keys["a"][0]))
    (L / "policy" / "operator").write_text(operator_record_text("someone-else", ME + 1))
    with pytest.raises(Refused) as e:
        IH.run_authority_signer(parse_install_argv([REMOVE, "--key", keys["a"][1]]), ME, "darwin", lock=_Lock(),
                                store_seams=_rs(L))
    assert e.value.name == "operator-mismatch" and keys["a"][0] in _path(L).read_text()


# --- through main -------------------------------------------------------------------------------------------------------

def test_the_seams_default_is_the_real_verb():
    assert Seams().authority_signer is IH.run_authority_signer


def test_both_verbs_through_main_print_the_fingerprint(L, keys):
    run = functools.partial(IH.run_authority_signer, store_seams=_rs(L))
    seams = Seams(euid=lambda: 0, host=lambda: "darwin", lock=_Lock(), authority_signer=run, gather=_no_gather)
    rc, out, err = _main(_enrol_argv("phill", keys["a"][0], keys["pk"][0]), seams)
    assert rc == 0, err
    assert out["ok"] is True and [s["fingerprint"] for s in out["signers"]] == [keys["a"][1], keys["pk"][1]]
    assert keys["a"][1] in err and keys["pk"][1] in err and "levain cockpit keys" in err
    rc, out, err = _main([REMOVE, "--key", keys["pk"][1]], seams)
    assert (rc, out["removed"]) == (0, 1) and keys["pk"][1] in err
    rc, out, _ = _main(_enrol_argv("phill", keys["rsa"][0]), seams)
    assert (rc, out) == (1, {"ok": False, "refused": "authority-signer-key"})


def test_main_refuses_a_caller_who_is_not_root(L, keys):
    seams = Seams(euid=lambda: 501, host=lambda: "darwin", lock=_Lock(), gather=_no_gather)
    rc, out, _ = _main(_enrol_argv("phill", keys["a"][0]), seams)
    assert (rc, out) == (1, {"ok": False, "refused": "not-root"})


def test_the_production_path_is_beside_the_qbrokers_signers():
    for host in ("darwin", "linux"):
        p = root_tree.authority_signers_path(host)
        assert p.parent == root_tree.STORE[host] / "policy" and p.name == "authority_signers"


# --- the RUN: S4's journey with this verb's file as the gate's signers file ---------------------------------------------

@pytest.fixture
def jv(tmp_path, monkeypatch, test_only_confined_executor):
    """S4's :class:`tests.test_graduation_journey.Journey` whose root file is the one this verb writes: the journey's
    ``write_root(world, *names)`` becomes ``enrol-authority-signer`` with the first name as ``--public-key`` and the
    rest as ``--passkey`` (the act under the owner override, never sudo). Confinement is replaced as the journey's own
    tests replace it."""
    pytest.importorskip("cryptography")
    import tests.test_graduation_journey as GJ
    L = _scratch() / "L"
    (L / "policy").mkdir(parents=True)
    (L / "policy" / "operator").write_text(operator_record_text(MY_NAME, ME))

    def by_the_verb(world, *names, namespaces=AUTHORITY_NAMESPACE):
        assert namespaces == AUTHORITY_NAMESPACE
        pub = [world.state().keys[world.fp(n)].public_key for n in names]
        got = _act(L, _enrol_argv("laptop", pub[0], *pub[1:]))
        assert [s["fingerprint"] for s in got["signers"]] == [world.fp(n) for n in names]
        return _path(L)
    monkeypatch.setattr(GJ, "write_root", by_the_verb)
    j = GJ.Journey(tmp_path, monkeypatch)
    assert j.signers == _path(L)
    j.L = L
    return j


def test_run_a_policy_the_enrolled_key_signed_covers(jv):
    from levain.autonomic import Posture
    print("\nS5a RUN: the verb's file as the gate's signers file; policy create, then a fire")
    jv.fire.mint(Posture.ON_LOOP)
    jv.create()
    o = jv.dispatch("enrolled laptop key")
    assert o.fired and o.posture is Posture.ON_LOOP


def test_run_remove_authority_signer_uncovers(jv):
    from levain.autonomic import Posture
    print("\nS5a RUN: policy create, then remove-authority-signer of its signer, then a fire")
    jv.fire.mint(Posture.ON_LOOP)
    jv.create()
    assert jv.dispatch("enrolled").fired
    _act(jv.L, [REMOVE, "--key", jv.auth.fp("laptop")])
    o = jv.dispatch("its signer removed by root")
    assert not o.fired and o.posture is Posture.CONFIRM


def test_run_a_policy_an_unlisted_key_signed_covers_nothing(jv, tmp_path):
    """Another laptop key (enrolled in its own log, never in root's file) signs the same policy into a fresh log that
    replaces the cockpit's (S4 scenario 5's substitution): it covers nothing until the verb enrols that key."""
    import sqlite3
    from levain.autonomic import Posture
    from tests.test_authority_graduation_kind import World, grad
    from tests.test_graduation_journey import LOW_DICT
    print("\nS5a RUN: a policy signed by a key root's file does not list")
    jv.fire.mint(Posture.ON_LOOP)
    (tmp_path / "other").mkdir()
    other = World(tmp_path / "other")
    other.clock.now = jv.auth.clock.now
    other.link({"op": "create_grant", "grant": grad(other, actions={"link0": dict(LOW_DICT)}, store_id=jv.store_id)})
    src, dst = sqlite3.connect(other.db), sqlite3.connect(jv.auth.db)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    anchor = authority.anchor_path(jv.auth.root)
    anchor.rename(anchor.with_name(anchor.name + ".aside"))     # the substituted log now replays
    view = jv.coverage.refresh()
    assert len(view.policies) == 1                               # the other laptop's policy is in the replay
    o = jv.dispatch("policy signed by an unlisted key")
    assert not o.fired and o.posture is Posture.CONFIRM
    jv.clear()
    # the control: the same policy covers once root lists its signer (so the CONFIRM above was the root file's)
    _act(jv.L, _enrol_argv("other", other.state().keys[other.fp("laptop")].public_key))
    assert jv.dispatch("its signer enrolled by the verb").fired


def test_run_a_passkey_born_policy_covers_only_with_its_passkey_line(jv):
    """S4 scenario 8 through the verb: ``write_root(laptop, pk)`` is ``--passkey`` here."""
    import tests.test_graduation_journey as GJ
    GJ.test_scenario_8_a_passkey_born_policy_covers_only_with_its_root_line(jv)


@pytest.mark.parametrize("name", ["a", "ec", "rsa", "pk"])
def test_the_blob_fingerprint_is_ssh_keygens(keys, name):
    assert IH.blob_fingerprint(keys[name][0]) == keys[name][1] == IH.key_fingerprint(keys[name][0])


def test_the_confirm_gate_verifies_against_a_confirm_line_and_not_an_authority_only_one(L, tmp_path):
    """``--confirm``'s line through the confirm gate's own verifier (``ssh-keygen -Y verify``); a passkey-style
    authority-only line never verifies a ``levain-confirm`` signature."""
    k = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "c", "-f", str(k)], check=True)
    pub = " ".join((tmp_path / "k.pub").read_text().split()[:2])
    msg = b"levain-confirm challenge"
    sig = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(k), "-n", confirm.NAMESPACE], input=msg,
                         capture_output=True, check=True).stdout.decode()
    _act(L, _enrol_argv("phill", pub))
    assert confirm.verify_signature(msg, signature=sig, signer="phill", allowed_signers=_path(L)) is False
    _act(L, _enrol_argv("phill", pub, confirm_ns=True))
    assert confirm.verify_signature(msg, signature=sig, signer="phill", allowed_signers=_path(L)) is True
    assert confirm.verify_signature(msg, signature=sig, signer="other", allowed_signers=_path(L)) is False
