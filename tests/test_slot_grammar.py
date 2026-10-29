"""The install helper's argv grammar (CONTRACT §5 preamble, §5.2): exact patterns, refusal by name."""
from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from levain.slot import Refused
from levain.slot.grammar import VERBS, check_roots, parse_install_argv

SETUP = ["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "alice"]
SESSION = "s-0123456789abcdef"


def _refused(argv) -> str:
    with pytest.raises(Refused) as ei:
        parse_install_argv(argv)
    return ei.value.name


def test_every_verb_parses():
    good = {
        "setup": SETUP + ["--root", "/Users/alice/code", "--root", "/Users/alice/notes"],
        "repair": ["repair"], "undo": ["undo"], "replenish": ["replenish"],
        "set-retention": ["set-retention", "7"], "set-dispatch-retain": ["set-dispatch-retain", "1"],
        "set-ceiling": ["set-ceiling"], "set-signers": ["set-signers"],
        "purge": ["purge", SESSION],
        "force-retire": ["force-retire", "egress", "63", "--max-inodes", "100", "--max-seconds", "30"],
        "set-credential": ["set-credential", "anthropic"], "remove-credential": ["remove-credential", "anthropic"],
        "set-limits": ["set-limits"], "routes-base": ["routes-base"], "routes-trust": ["routes-trust"],
        "routes-egress": ["routes-egress"],
        "enrol-authority-signer": ["enrol-authority-signer", "--principal", "alice", "--public-key",
                                   "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="],
        "remove-authority-signer": ["remove-authority-signer", "--key", "SHA256:" + "A" * 43],
    }
    assert set(good) == set(VERBS)
    for verb, argv in good.items():
        assert parse_install_argv(argv).verb == verb
    cmd = parse_install_argv(good["setup"])
    assert (cmd.slots, cmd.verifier_slots, cmd.operator) == (8, 2, "alice")
    assert cmd.roots == ("/Users/alice/code", "/Users/alice/notes")
    fr = parse_install_argv(good["force-retire"])
    assert (fr.slot_class, fr.index, fr.max_inodes, fr.max_seconds) == ("egress", 63, 100, 30)
    assert parse_install_argv(["force-retire", "none", "0"]).max_inodes is None
    assert parse_install_argv(["setup", "--operator", "bob", "--verifier-slots", "16", "--slots", "32"]).slots == 32


@pytest.mark.parametrize("argv,name", [
    # unknown verb / no verb
    (["frobnicate"], "unknown-verb"),
    (["Setup"] + SETUP[1:], "unknown-verb"),
    (["../setup"], "unknown-verb"),
    ([], "missing-argv"),
    # extra argv
    (["undo", "now"], "extra-argv"),
    (["repair", "--force"], "extra-argv"),
    (["purge", SESSION, SESSION], "extra-argv"),
    (["set-retention", "7", "8"], "extra-argv"),
    (SETUP + ["--extra", "x"], "extra-argv"),
    (["force-retire", "egress", "1", "--max-inodes", "5", "junk"], "extra-argv"),
    (["force-retire", "egress", "1", "--max-bytes", "5"], "extra-argv"),
    # missing argv
    (["setup", "--slots", "8", "--verifier-slots", "2"], "missing-argv"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator"], "missing-argv"),
    (["purge"], "missing-argv"),
    (["force-retire", "egress"], "missing-argv"),
    (["force-retire", "egress", "1", "--max-seconds"], "missing-argv"),
    # duplicates
    (SETUP + ["--slots", "4"], "duplicate-flag"),
    (SETUP + ["--root", "/a", "--root", "/a"], "duplicate-root"),
    (["force-retire", "none", "1", "--max-inodes", "1", "--max-inodes", "2"], "duplicate-flag"),
    # out of range
    (["setup", "--slots", "33", "--verifier-slots", "2", "--operator", "alice"], "out-of-range"),
    (["setup", "--slots", "0", "--verifier-slots", "2", "--operator", "alice"], "out-of-range"),
    (["setup", "--slots", "8", "--verifier-slots", "17", "--operator", "alice"], "out-of-range"),
    (["force-retire", "egress", "64"], "out-of-range"),
    (["set-retention", "6"], "out-of-range"),
    (["set-retention", "0"], "out-of-range"),
    (["set-dispatch-retain", "0"], "out-of-range"),
    # bad tokens
    (["setup", "--slots", "08", "--verifier-slots", "2", "--operator", "alice"], "bad-token"),
    (["setup", "--slots", "+8", "--verifier-slots", "2", "--operator", "alice"], "bad-token"),
    (["setup", "--slots", "8 ", "--verifier-slots", "2", "--operator", "alice"], "bad-token"),
    (["setup", "--slots", "999999999999999999999", "--verifier-slots", "2", "--operator", "alice"], "bad-token"),
    (["force-retire", "egress", "-1"], "bad-token"),
    (["force-retire", "egress", "1e1"], "bad-token"),
    (["force-retire", "verifier", "1"], "bad-token"),
    (["force-retire", "../egress", "1"], "bad-token"),
    (["force-retire", "egress", "1", "--max-inodes", "0"], "out-of-range"),
    (["purge", "../s-0123456789abcdef"], "bad-token"),
    (["purge", "s-0123456789ABCDEF"], "bad-token"),
    (["purge", "s-0123456789abcde"], "bad-token"),
    (["purge", SESSION + "\n"], "bad-token"),
    (["purge", "s-0123456789abcdef0"], "bad-token"),
    (["purge", "s-0123\x0056789abcdef"], "bad-token"),
    (["undo", ""], "bad-token"),
    (["set-retention", "7.5"], "bad-token"),
    # operator name
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "al ice"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "root"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "_levain_s0"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "ALL"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "ADMINS"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "Alice"], None),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "#0"], "bad-operator"),
    (["setup", "--slots", "8", "--verifier-slots", "2", "--operator", "a,b"], "bad-operator"),
    # --root syntax
    (SETUP + ["--root", "../x"], "bad-root"),
    (SETUP + ["--root", "relative/x"], "bad-root"),
    (SETUP + ["--root", "/Users/alice/../bob"], "bad-root"),
    (SETUP + ["--root", "/Users/alice/./x"], "bad-root"),
    (SETUP + ["--root", "/" + "a" * 1100], "bad-root"),
])
def test_grammar_refusal_corpus(argv, name):
    if name is None:   # a positive control: the grammar accepts it
        parse_install_argv(argv)
        return
    assert _refused(argv) == name


def test_argv_must_be_a_sequence_of_strings():
    assert _refused("undo") == "bad-token"
    assert _refused(["undo", 1]) == "bad-token"


def _st(mode: int, uid: int):
    return SimpleNamespace(st_mode=mode, st_uid=uid)


def test_check_roots_resolves_and_requires_operator_dir():
    tree = {"/real/code": _st(stat.S_IFDIR | 0o755, 501), "/real/file": _st(stat.S_IFREG | 0o644, 501),
            "/real/bobs": _st(stat.S_IFDIR | 0o755, 502)}
    links = {"/Users/alice/code": "/real/code", "/Users/alice/link": "/real/code"}
    rp = lambda p: links.get(p, p)   # noqa: E731

    def ls(p):
        if p not in tree:
            raise FileNotFoundError(p)
        return tree[p]

    assert check_roots(["/Users/alice/code"], 501, realpath=rp, lstat=ls) == ("/real/code",)
    for roots, name in ((["/real/file"], "root-not-directory"), (["/real/bobs"], "root-not-operators"),
                        (["/real/none"], "root-missing"),
                        (["/Users/alice/code", "/Users/alice/link"], "duplicate-root"),
                        (["../x"], "bad-root")):
        with pytest.raises(Refused) as ei:
            check_roots(roots, 501, realpath=rp, lstat=ls)
        assert ei.value.name == name
    with pytest.raises(Refused) as ei:
        check_roots(["/x"], 501, realpath=lambda p: "/", lstat=ls)
    assert ei.value.name == "bad-root"


def test_check_roots_default_stat_on_a_real_dir(tmp_path):
    assert check_roots([str(tmp_path)], os.getuid()) == (os.path.realpath(tmp_path),)
