"""levain#133 (S14 harness fixes from the 1118 VM sitting, project_memory/1118-vm/RESULTS.md findings B, C, D, F, I, J, L, and the
review round's classes: idempotence by content, bounded permission widening, the keys/cred installer, route ids and write paths).

Each test replays the input the sitting recorded failing: the refusal text, the cwd, the missing file. The guest itself is never
touched, and NO real chmod or chown runs: ``run`` (the one subprocess seam of ``stage_call``) is faked wherever a mode, an owner,
sudo or the guest's accounts would be touched; only ssh-keygen and git run for real, inside tmp_path."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.contracts import canon as real_canon
from levain.slot import Refused
from levain.slot.routemaps import policy_digest as real_policy_digest
from scripts.vm_harness import stage_call as SC
from scripts.vm_harness import threec10 as T


def _args(tmp_path: Path, **kw) -> argparse.Namespace:
    root = tmp_path.resolve()
    d = dict(levain_root=str(Path(__file__).resolve().parent.parent), python=sys.executable, as_user="_levain_dispatch",
             dispatch_dir="/private/var/levain/dispatch", run_dir="/private/var/run/levain",
             data_root=str(root / "scratch" / "w3" / "cockpit"),
             signing_key="/private/var/levain/dispatch/w3-key", key_pub=str(root / "key.pub"), prep_dir=str(root / "admin" / "w3"),
             custody="tpm", models=SC.DEFAULT_MODELS, backends="", cred="or-key", template="wave3-r0", write_path="wave3/hello.txt",
             verifier_id="wave3-bb", verifier_argv="python3 -m pytest -q", price_in=2_000_000, price_out=8_000_000,
             price_source="https://openrouter.ai/models", no_pin=False, dry_submit=False, repo=str(root / "repo"), git="git")
    d.update(kw)
    return argparse.Namespace(**d)


def _modes_above(path: Path) -> dict:
    return {str(p): p.stat().st_mode for p in path.resolve().parents}


# ---- C: a command run as the dispatch account inherits admin's cwd and HOME -------------------------------------------------

def test_run_pins_cwd_to_root_and_the_dispatch_argv_sets_home(tmp_path, monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(SC.subprocess, "run", fake)
    SC.run(["/usr/bin/true"])
    assert seen["cwd"] == "/"          # the recorded failure: PermissionError from pathlib's getcwd() as _levain_dispatch
    a = _args(tmp_path)
    for argv in (SC.as_dispatch(a, ["x"]), SC.as_dispatch_script(a, [])):
        assert f"HOME={a.dispatch_dir}" in argv      # the recorded failure: ~/.levain/consent/... unreadable under admin's HOME
        # the recorded failure: getcwd() PermissionError under admin's 0700 home; the command runs from the dispatch dir
        i = argv.index("/bin/sh")
        assert tuple(argv[i:i + 5]) == SC.cd_run(a.dispatch_dir)
        assert argv.index(f"HOME={a.dispatch_dir}") < i           # the env assignments come first, the command last


def test_the_two_copies_of_the_cd_words_are_equal_and_really_change_directory(tmp_path):
    """stage_call.py is copied alone into the guest, so it keeps its own cd_run; the two must not drift."""
    from scripts.vm_harness import host_profile
    d = tmp_path / "a dir with 'quotes' and $dollars"
    d.mkdir()
    assert SC.cd_run(str(d)) == host_profile.cd_run(str(d))
    r = subprocess.run([*host_profile.cd_run(str(d)), "/bin/pwd", "-P"], cwd="/", capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == os.path.realpath(d)     # the directory was a positional argument
    bad = subprocess.run([*host_profile.cd_run(str(tmp_path / "missing")), "/usr/bin/true"], capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "/usr/bin/true" not in bad.stdout          # a missing dir never runs the command


# ---- J: provider_policy read: [] refused every contract ----------------------------------------------------------------------

def test_the_policy_read_covers_the_contract_path_the_sitting_filed(tmp_path):
    from levain.dispatch.policy import ProviderPolicy
    body = SC.build_bodies(_args(tmp_path))["provider_policy"]
    assert ProviderPolicy.from_mapping(body).refusal("openrouter", ["wave3/hello.txt"], ["wave3/hello.txt"]) is None
    # the recorded failing input: an empty read list
    empty = ProviderPolicy.from_mapping({"never": SC.NEVER, "providers": {"openrouter": {"read": []}}})
    assert "outside what provider openrouter may read" in empty.refusal("openrouter", ["wave3/hello.txt"], ["wave3/hello.txt"])
    # a different write path stays covered (the glob follows --write-path)
    other = SC.build_bodies(_args(tmp_path, write_path="out/a/b.txt"))["provider_policy"]
    assert other["providers"]["openrouter"]["read"] == ["out/**"]


# ---- D (class): route ids that collapse, and glob syntax in --write-path, are refused by name -----------------------------------

@pytest.mark.parametrize("models", ["foo/a.b,foo/a-b", "Foo/A,foo/a",
                                    "org/a-very-long-model-identifier-shared-x1,org/a-very-long-model-identifier-shared-x2"])
def test_distinct_models_that_make_one_route_id_are_refused_by_name(tmp_path, capsys, models):
    with pytest.raises(SystemExit):
        SC.build_bodies(_args(tmp_path, models=models))
    out = capsys.readouterr().out
    first, second = models.split(",")
    assert out.startswith("stage-failed args:") and first in out and second in out and "route id or-" in out


def test_a_model_with_no_slug_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        SC.build_bodies(_args(tmp_path, models="///,z-ai/glm-5.2"))
    assert "no letter or digit" in capsys.readouterr().out


@pytest.mark.parametrize("wp", ["wave3/*.txt", "*", "**", "wave3/[ab].txt", "wave3/a?.txt", "wave3/{a,b}", "/abs/p",
                                "../x/a", "a//b", "./a", "wave3/", "~/a", "a\\b", "",
                                "wave3/a\nb", "wave3/a\x07b", "wave3/a​b", "wave3/a\x7fb", "wave3/a b"])
def test_a_write_path_that_would_widen_the_read_allowlist_is_refused(tmp_path, capsys, wp):
    with pytest.raises(SystemExit):
        SC.build_bodies(_args(tmp_path, write_path=wp))
    assert capsys.readouterr().out.startswith("stage-failed args:")


def test_slug_strips_dashes_after_the_truncation():
    assert SC.slug("a" * 39 + "/b") == "a" * 39           # the 40th character was a '-': it is not left trailing
    assert not SC.slug("x" * 39 + ".y").endswith("-") and SC.slug("Z-ai/GLM-5.2") == "z-ai-glm-5-2"
    assert SC.route_ids(["a" * 39 + "/b", "c/d"]) == ["or-" + "a" * 39, "or-c-d"]


def test_a_plain_write_path_is_accepted_and_its_read_pattern_is_the_top_directory(tmp_path):
    assert SC.build_bodies(_args(tmp_path, write_path="a/b/c.txt"))["provider_policy"]["providers"]["openrouter"]["read"] == ["a/**"]
    assert SC.build_bodies(_args(tmp_path, write_path="top.txt"))["provider_policy"]["providers"]["openrouter"]["read"] == ["top.txt"]


# ---- the policy mode signs a NEW provider_policy generation --------------------------------------------------------------------

def test_policy_mode_signs_provider_policy_with_the_bodys_stdin(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        calls.append((list(argv), stdin))
        return 0, json.dumps({"generation": 7}) + "\n", ""

    monkeypatch.setattr(SC, "run", fake_run)
    assert SC.mode_policy(_args(tmp_path)) == 0
    argv, stdin = calls[0]
    assert argv[:5] == ["sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env"]
    assert ["config", "sign", "provider_policy", "-"] == argv[argv.index("config"):argv.index("config") + 4]
    assert json.loads(stdin)["providers"]["openrouter"]["read"] == ["wave3/**"]
    assert "policy generation=7" in capsys.readouterr().out


# ---- F, D and B (class): prep installs the .pub; the data root is opened, BOUNDED, and every chmod is checked -----------------

def _apply_chmod(mode: str, path: str) -> None:
    """The two mode forms prep uses, applied to a path inside tmp_path (octal; ``a+x,go-w``). Anything else is a test error."""
    cur = os.stat(path).st_mode & 0o7777
    if mode == SC.DIR_MODE:
        new = (cur | 0o111) & ~0o022
    elif mode.isdigit():
        new = int(mode, 8)
    else:
        raise AssertionError(f"prep used a mode form the test does not model: {mode}")
    os.chmod(path, new)


def _prep_world(tmp_path, monkeypatch, *, fail_chmod=None, anchor=True):
    """mode_prep with every chmod/sudo/ledger call faked (recorded, never run); ssh-keygen is real, into tmp_path."""
    a = _args(tmp_path)
    monkeypatch.setattr(SC, "SCRATCH", tmp_path.resolve() / "scratch" / "w3")
    calls = []
    real = SC.run

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        calls.append(list(argv))
        if argv[0] == "/usr/bin/ssh-keygen":
            return real(argv, stdin=stdin, env=env, timeout=timeout)
        if argv[0] == "/bin/chmod":
            if fail_chmod and fail_chmod(argv):
                return 1, "", "Operation not permitted"
            _apply_chmod(argv[1], argv[2])          # inside tmp_path only (SCRATCH is patched into it)
            return 0, "", ""
        if "ledger" in argv:
            root = Path(a.data_root)
            (root / "cockpit.db").write_text("x")
            if anchor:
                (root / "anchor").mkdir()
                (root / "anchor" / "authority.anchor").write_text("a")
            (root / "private.lock").write_text("")
        return 0, "", ""

    monkeypatch.setattr(SC, "run", fake_run)
    monkeypatch.setattr(SC, "init_cockpit", lambda root: None)
    monkeypatch.setattr(SC.sys, "path", SC.sys.path[:])
    return a, calls


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_prep_installs_the_pub_and_opens_only_the_data_root_never_the_host(tmp_path, monkeypatch, capsys):
    before = _modes_above(tmp_path)
    a, calls = _prep_world(tmp_path, monkeypatch)
    assert SC.mode_prep(a) == 0
    assert _modes_above(tmp_path) == before          # the recorded harm: the test turned the host's 0700 TMPDIR into 0755
    installs = [c for c in calls if c[:3] == ["sudo", "-n", "/usr/bin/install"]]
    assert {c[-1] for c in installs} == {f"{a.dispatch_dir}/w3-key", f"{a.dispatch_dir}/w3-key.pub"}     # the recorded failure: no .pub
    mode = {c[-1]: c[c.index("-m") + 1] for c in installs}
    assert mode[f"{a.dispatch_dir}/w3-key"] == "0600" and mode[f"{a.dispatch_dir}/w3-key.pub"] == "0644"
    root, scratch = Path(a.data_root), SC.SCRATCH
    chmods = [c[1:] for c in calls if c[0] == "/bin/chmod"]
    # the ancestor prep made (scratch/w3) is made searchable; the data root is the one writable (sticky) dir; its subdirectory
    # searchable; the authority log and the anchor readable, never writable; the private lock file and the prep dir untouched
    assert chmods == [["a+x,go-w", str(scratch)], ["1777", str(root)], ["a+x,go-w", str(root / "anchor")],
                      ["0644", str(root / "cockpit.db")], ["0644", str(root / "anchor" / "authority.anchor")]]
    assert all(SC._within(Path(c[-1]), scratch) for c in chmods)       # nothing above the scratch tree is ever changed
    assert not any("-R" in c or "a+r" in c[0] for c in chmods)
    assert calls.index(["/bin/chmod", "0644", str(root / "anchor" / "authority.anchor")]) < calls.index(installs[0])
    assert capsys.readouterr().out.splitlines()[-1].startswith("prepped ")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_prep_removes_a_world_write_bit_an_earlier_mode_left(tmp_path, monkeypatch, capsys):
    """codex OPEN: ``a+r`` on a 0666 cockpit.db stayed 0666. The explicit modes end at 0644 / searchable-not-writable, and the
    result is stat-ed, so a chmod that left a write bit would fail prep."""
    a, calls = _prep_world(tmp_path, monkeypatch)
    root = Path(a.data_root)
    inner = SC.run

    def fake_run(argv, **kw):
        res = inner(argv, **kw)
        if "ledger" in argv:
            (root / "cockpit.db").chmod(0o666)
            (root / "anchor").chmod(0o777)
            (root / "anchor" / "authority.anchor").chmod(0o666)
        return res

    monkeypatch.setattr(SC, "run", fake_run)
    assert SC.mode_prep(a) == 0
    assert (root / "cockpit.db").stat().st_mode & 0o777 == 0o644
    assert (root / "anchor" / "authority.anchor").stat().st_mode & 0o777 == 0o644
    assert (root / "anchor").stat().st_mode & 0o022 == 0 and (root / "anchor").stat().st_mode & 0o001


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_prep_fails_when_a_chmod_that_exits_zero_leaves_a_write_bit(tmp_path, monkeypatch, capsys):
    a, calls = _prep_world(tmp_path, monkeypatch)
    inner = SC.run

    def fake_run(argv, **kw):
        if argv[0] == "/bin/chmod" and argv[1] == "0644" and argv[2].endswith("cockpit.db"):
            return 0, "", ""                  # claims success, changes nothing: the file stays what the ledger made it
        res = inner(argv, **kw)
        if "ledger" in argv:
            (Path(a.data_root) / "cockpit.db").chmod(0o666)
        return res

    monkeypatch.setattr(SC, "run", fake_run)
    with pytest.raises(SystemExit):
        SC.mode_prep(a)
    out = capsys.readouterr().out
    assert "stage-failed prep-chmod:" in out and "cockpit.db is mode 0666" in out and "prepped" not in out


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
@pytest.mark.parametrize("target,left", [("cockpit.db", 0o744), ("cockpit.db", 0o044), ("anchor/authority.anchor", 0o664),
                                         ("anchor", 0o750), ("anchor", 0o777)])
def test_prep_fails_on_any_mode_that_is_not_exactly_the_intended_one(tmp_path, monkeypatch, capsys, target, left):
    """The postcondition is EQUALITY, not a mask: a chmod that exits 0 but leaves 0744 / 0044 / 0664 (file) or 0750 / 0777 (directory; 0601 would hide its own children from the stat)."""
    a, calls = _prep_world(tmp_path, monkeypatch)
    inner = SC.run
    root = Path(a.data_root)

    def fake_run(argv, **kw):
        if argv[0] == "/bin/chmod" and argv[2] == str(root / target):
            (root / target).chmod(left)
            return 0, "", ""
        return inner(argv, **kw)

    monkeypatch.setattr(SC, "run", fake_run)
    with pytest.raises(SystemExit):
        SC.mode_prep(a)
    out = capsys.readouterr().out
    assert f"stage-failed prep-chmod: {root / target} is mode {left:04o}" in out and "prepped" not in out


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
@pytest.mark.parametrize("mode,ok", [(0o711, True), (0o705, True), (0o710, False), (0o700, False)])
def test_a_pre_existing_ancestor_must_have_the_other_execute_bit(tmp_path, monkeypatch, capsys, mode, ok):
    """The dispatch account is another account, not in the operator's group: traversal is other-x (read is not needed)."""
    a, calls = _prep_world(tmp_path, monkeypatch)
    SC.SCRATCH.mkdir(parents=True)
    os.chmod(SC.SCRATCH, mode)
    if ok:
        assert SC.mode_prep(a) == 0
    else:
        with pytest.raises(SystemExit):
            SC.mode_prep(a)
        assert "prep widens no directory it did not make" in capsys.readouterr().out
    assert SC.SCRATCH.stat().st_mode & 0o7777 == mode           # a pre-existing ancestor is never widened


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_failed_chmod_fails_prep_before_the_key_is_installed(tmp_path, monkeypatch, capsys):
    a, calls = _prep_world(tmp_path, monkeypatch, fail_chmod=lambda argv: argv[1] == "1777")
    with pytest.raises(SystemExit) as e:
        SC.mode_prep(a)
    out = capsys.readouterr().out
    assert e.value.code == 1 and "stage-failed prep-chmod: /bin/chmod 1777" in out and "prepped" not in out
    assert not any(c[0] == "sudo" for c in calls)


@pytest.mark.parametrize("bad", ["relative", "outside", "dotdot", "prep_inside", "data_inside_prep"])
def test_prep_refuses_a_data_root_it_may_not_open_before_doing_anything(tmp_path, monkeypatch, capsys, bad):
    a, calls = _prep_world(tmp_path, monkeypatch)
    root = tmp_path.resolve()
    a.data_root, a.prep_dir = {
        "relative": ("scratch/w3/cockpit", a.prep_dir),
        "outside": (str(root / "home" / "u" / "cockpit"), a.prep_dir),     # a home: the old walk opened every ancestor
        "dotdot": (str(SC.SCRATCH / ".." / "x"), a.prep_dir),
        "prep_inside": (a.data_root, str(Path(a.data_root) / "keys")),
        "data_inside_prep": (str(SC.SCRATCH / "admin" / "cockpit"), str(SC.SCRATCH / "admin")),
    }[bad]
    with pytest.raises(SystemExit):
        SC.mode_prep(a)
    assert capsys.readouterr().out.startswith("stage-failed prep:")
    assert calls == [] and not (root / "admin").exists() and not (root / "scratch").exists()


def test_prep_refuses_a_data_root_through_a_symbolic_link(tmp_path, monkeypatch, capsys):
    a, calls = _prep_world(tmp_path, monkeypatch)
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    SC.SCRATCH.parent.mkdir(parents=True)
    SC.SCRATCH.symlink_to(elsewhere)
    with pytest.raises(SystemExit):
        SC.mode_prep(a)
    assert "symbolic link" in capsys.readouterr().out and calls == []


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_prep_never_widens_an_ancestor_it_did_not_make(tmp_path, monkeypatch, capsys):
    a, calls = _prep_world(tmp_path, monkeypatch)
    SC.SCRATCH.mkdir(parents=True, mode=0o700)       # pre-existing, not searchable by others
    os.chmod(SC.SCRATCH, 0o700)
    with pytest.raises(SystemExit):
        SC.mode_prep(a)
    assert "prep widens no directory it did not make" in capsys.readouterr().out
    assert not any(c[0] == "/bin/chmod" for c in calls) and not any(c[0] == "sudo" for c in calls)


# ---- L: the dispatch account OWNS the repo; main is read before the chown; every chmod/chown is checked -------------------------

def _repo_world(tmp_path, monkeypatch, fail=None):
    calls = []
    real = SC.run

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        calls.append(list(argv))
        if argv[0] in ("sudo", "/bin/chmod"):
            return (1, "", "denied") if fail == argv[0] else (0, "", "")
        return real(argv, stdin=stdin, env=env, timeout=timeout)        # git only, inside tmp_path

    monkeypatch.setattr(SC, "run", fake_run)
    return calls


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repo_mode_chowns_the_repo_to_the_dispatch_account_and_writes_no_system_config(tmp_path, monkeypatch, capsys):
    calls = _repo_world(tmp_path, monkeypatch)
    assert SC.mode_repo(_args(tmp_path, git=shutil.which("git"))) == 0
    repo = str(tmp_path.resolve() / "repo")
    assert [c for c in calls if c[0] == "sudo"] == [["sudo", "-n", "/usr/sbin/chown", "-R", "_levain_dispatch", repo]]
    assert not any("safe.directory" in " ".join(c) for c in calls)
    revparse = next(i for i, c in enumerate(calls) if "rev-parse" in c)
    assert revparse < calls.index(["sudo", "-n", "/usr/sbin/chown", "-R", "_levain_dispatch", repo])   # read while admin owns it
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith(f"repo ready {repo} main=") and len(line.split("main=")[1]) >= 40


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("fail", ["/bin/chmod", "sudo"])
def test_repo_mode_fails_on_a_failed_chmod_or_chown(tmp_path, monkeypatch, capsys, fail):
    _repo_world(tmp_path, monkeypatch, fail=fail)
    with pytest.raises(SystemExit):
        SC.mode_repo(_args(tmp_path, git=shutil.which("git")))
    out = capsys.readouterr().out
    assert "stage-failed" in out and "repo ready" not in out


# ---- A (class): stage keeps a config item only when its active BODY is this body; a failed read is fatal -------------------------

def _stage_world(tmp_path, monkeypatch, stored, *, show_rc=0, show=None):
    """mode_stage with every guest command faked: ``stored`` is the store read's answer ({kind: {generation, sha256}})."""
    a = _args(tmp_path)
    monkeypatch.setattr("levain.cockpit.keys.signing_public_key", lambda p: "ecdsa-sha2-nistp256 AAAA")
    monkeypatch.setattr("levain.cockpit.keys.fingerprint_of", lambda pub: "SHA256:x")
    if show is None:
        show = {"configured": True, "kinds": {k: {"generation": v["generation"]} for k, v in stored.items()}}
    signed = []

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        if "init" in argv:
            return 0, json.dumps({"store_id": "s1", "route_trust": {"pinned": True}}) + "\n", ""
        if "show" in argv:
            return show_rc, (json.dumps(show) + "\n") if show_rc == 0 else "", "database is locked" if show_rc else ""
        if "sign" in argv:
            signed.append(argv[argv.index("sign") + 1])
            return 0, json.dumps({"generation": 10 + len(signed)}) + "\n", ""
        if stdin == SC.STORE_SCRIPT:
            return 0, json.dumps(stored) + "\n", ""
        if stdin == SC.DISPATCH_SIDE:
            return 0, "staged gen=20\n", ""
        raise AssertionError(argv)

    monkeypatch.setattr(SC, "run", fake_run)
    monkeypatch.setattr(SC.sys, "path", SC.sys.path[:])
    return a, signed


def _stored(bodies, **override):
    out = {k: {"generation": g, "sha256": SC.body_sha(bodies[k])} for g, k in ((1, "provider_policy"), (2, "verifier"), (3, "templates"))}
    for k, body in override.items():
        out[k]["sha256"] = SC.body_sha(body)
    return out


def test_stage_keeps_every_kind_whose_active_body_is_this_body(tmp_path, monkeypatch, capsys):
    bodies = SC.build_bodies(_args(tmp_path))
    a, signed = _stage_world(tmp_path, monkeypatch, _stored(bodies))
    assert SC.mode_stage(a) == 0 and signed == []
    assert capsys.readouterr().out.count("is active with this body (kept)") == 3


@pytest.mark.parametrize("kind,vary", [
    ("provider_policy", lambda b: {**b, "providers": {"openrouter": {"read": ["other/**"]}}}),       # the policy body
    ("verifier", lambda b: {**b, "verifiers": [{**b["verifiers"][0], "steps": [{"argv": ["true"], "wall_s": 1}]}]}),
    ("templates", lambda b: [{**b[0], "write_paths": ["elsewhere/x.txt"]}]),
])
def test_stage_signs_a_kind_whose_active_body_differs(tmp_path, monkeypatch, kind, vary):
    # the recorded class: 'any active item of a kind' was taken for the requested item, so a changed body was never signed
    bodies = SC.build_bodies(_args(tmp_path))
    a, signed = _stage_world(tmp_path, monkeypatch, _stored(bodies, **{kind: vary(bodies[kind])}))
    assert SC.mode_stage(a) == 0 and signed == [kind]


def test_stage_signs_every_kind_on_an_empty_store(tmp_path, monkeypatch):
    a, signed = _stage_world(tmp_path, monkeypatch, {})
    assert SC.mode_stage(a) == 0 and signed == ["provider_policy", "verifier", "templates"]


def test_a_freshly_initialised_empty_store_is_configured_true_with_no_kinds(tmp_path, monkeypatch, capsys):
    """gemini MED, checked against the code: ``levain dispatch config show`` answers ``configured: False`` ONLY when no control.db
    exists (levain/dispatch/cli.py _run_config, ``if not os.path.lexists(path)``). ``config init`` has just made it, so the first
    stage after prep reads ``configured: True`` with ``kinds: {}`` and the real shape below, and signs all three kinds."""
    real_shape = {"configured": True, "store": "/private/var/levain/dispatch/control.db", "store_id": "s1", "trust_root": "SHA256:x",
                  "generation": 0, "head": "", "kinds": {}}
    a, signed = _stage_world(tmp_path, monkeypatch, {}, show=real_shape)
    assert SC.mode_stage(a) == 0 and signed == ["provider_policy", "verifier", "templates"]
    assert "stage-failed" not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["rc", "unconfigured"])
def test_a_failed_config_show_is_fatal_and_signs_nothing(tmp_path, monkeypatch, capsys, case):
    bodies = SC.build_bodies(_args(tmp_path))
    a, signed = _stage_world(tmp_path, monkeypatch, _stored(bodies), show_rc=1 if case == "rc" else 0,
                             show={"configured": False} if case == "unconfigured" else None)
    with pytest.raises(SystemExit):
        SC.mode_stage(a)
    assert "stage-failed config-show" in capsys.readouterr().out and signed == []


def test_a_store_read_that_disagrees_with_config_show_is_fatal(tmp_path, monkeypatch, capsys):
    bodies = SC.build_bodies(_args(tmp_path))
    stored = _stored(bodies)
    show = {"configured": True, "kinds": {k: {"generation": v["generation"] + 1} for k, v in stored.items()}}
    a, signed = _stage_world(tmp_path, monkeypatch, stored, show=show)
    with pytest.raises(SystemExit):
        SC.mode_stage(a)
    assert "stage-failed store-read" in capsys.readouterr().out and signed == []


def test_the_store_script_digest_is_the_built_bodys_digest(tmp_path):
    """STORE_SCRIPT, run for real against a store of real items (dctl.item_bytes), reports body_sha of each kind's LATEST body."""
    from levain.dispatch import control as dctl
    bodies = SC.build_bodies(_args(tmp_path))
    d = tmp_path / "ctl"
    d.mkdir()
    con = sqlite3.connect(d / "control.db")
    con.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    con.execute("CREATE TABLE items(generation INT, kind TEXT, item TEXT, signature TEXT)")
    prev, now = "", datetime.now(timezone.utc)
    old_policy = {"never": SC.NEVER, "providers": {"openrouter": {"read": ["old/**"]}}}
    for gen, kind, body in ((1, "provider_policy", old_policy), (2, "verifier", bodies["verifier"]),
                            (3, "templates", bodies["templates"]), (4, "provider_policy", bodies["provider_policy"])):
        raw = dctl.item_bytes(kind, gen, body, now, prev=prev, store_id="a" * 32)
        con.execute("INSERT INTO items VALUES(?,?,?,?)", (gen, kind, raw.decode("utf-8"), "sig"))
        prev = dctl.parse_item(raw).digest
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, "-I", "-", _args(tmp_path).levain_root, str(d)], input=SC.STORE_SCRIPT, capture_output=True,
                       text=True)
    got = json.loads(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr
    assert got == {"provider_policy": {"generation": 4, "sha256": SC.body_sha(bodies["provider_policy"])},
                   "verifier": {"generation": 2, "sha256": SC.body_sha(bodies["verifier"])},
                   "templates": {"generation": 3, "sha256": SC.body_sha(bodies["templates"])}}


def test_the_store_script_skips_a_legacy_verifiers_item(tmp_path):
    """A 0.11 ``verifiers`` item stays in the append-only chain; its body is not one this stage builds and must not kill the read."""
    from levain.dispatch import control as dctl
    bodies = SC.build_bodies(_args(tmp_path))
    d = tmp_path / "ctl"
    d.mkdir()
    con = sqlite3.connect(d / "control.db")
    con.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    con.execute("CREATE TABLE items(generation INT, kind TEXT, item TEXT, signature TEXT)")
    prev, now = "", datetime.now(timezone.utc)
    raw = dctl.item_bytes("verifier", 1, bodies["verifier"], now, prev=prev, store_id="a" * 32).decode("utf-8")
    con.execute("INSERT INTO items VALUES(?,?,?,?)", (1, "verifier", raw, "sig"))
    # a 0.11 'verifiers' row the current parser would refuse, newest in the chain
    con.execute("INSERT INTO items VALUES(?,?,?,?)", (2, "verifiers", "not an item the current parser reads", "sig"))
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, "-I", "-", _args(tmp_path).levain_root, str(d)], input=SC.STORE_SCRIPT, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"verifier": {"generation": 1,"sha256": SC.body_sha(bodies["verifier"])}}


# ---- I and A (class): the route map is skipped only on PROOF by content ----------------------------------------------------------

class _FakeControl:
    """CONTROL as the dispatch-side script sees it, with ROUTE-SUBMIT's generation rule (routemaps.RouteMaps.submit: above the
    head, gaps allowed, or equal with the same digest) and ROUTEMAP-ACTIVE's answer shape (control.py _op_routemap_active)."""
    head = None              # (generation, canonical bytes, item)
    calls: list = []
    active_error = None

    def __init__(self, sock):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def call(self, op, **kw):
        _FakeControl.calls.append(op)
        if op == "ROUTEMAP-ACTIVE":
            if _FakeControl.active_error is not None:
                raise _FakeControl.active_error
            if _FakeControl.head is None:
                raise Refused("no-routemap", "no route-map generation is active")
            gen, _raw, item = _FakeControl.head
            routes = item["body"]["routes"]
            return {"generation": gen,
                    "routes": {r["policy"]["route"]: {"backends": {m["requested"]: m["backend"] for m in r["models"]}} for r in routes},
                    "digests": {r["policy"]["route"]: real_policy_digest(r["policy"]) for r in routes}}
        if op == "CONFIG-SUBMIT":
            return {"generation": 2}
        if op == "ROUTE-SUBMIT":
            item = kw["map"]
            raw, gen = real_canon.canonical_bytes(item), item["generation"]
            if _FakeControl.head is not None:
                hgen, hraw, _ = _FakeControl.head
                if gen < hgen or (gen == hgen and raw != hraw):
                    raise Refused("routemap-generation", f"generation {gen} against the head's {hgen}")
            _FakeControl.head = (gen, raw, item)
            return {"generation": gen}
        raise AssertionError(op)


def _store(d: Path, extra=()):
    con = sqlite3.connect(d / "control.db")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS items(generation INT, kind TEXT, item TEXT, signature TEXT)")
    if not con.execute("SELECT 1 FROM meta").fetchone():
        con.executemany("INSERT INTO meta VALUES(?,?)", [("store_id", "s1"), ("trust_root", "FP")])
        con.executemany("INSERT INTO items VALUES(?,?,?,?)", [(1, "provider_policy", "{\"p\":1}", "s"), (2, "verifier", "{\"v\":1}", "s"),
                                                              (3, "templates", "{\"t\":1}", "s")])
    con.executemany("INSERT INTO items VALUES(?,?,?,?)", list(extra))
    con.commit()
    con.close()


def _run_dispatch_side(tmp_path, monkeypatch, capsys, body, extra=()):
    d = tmp_path / "ctl"
    d.mkdir(exist_ok=True)
    _store(d, extra)
    fake = {k: types.ModuleType(k) for k in ("levain", "levain.contracts", "levain.dispatch", "levain.cockpit", "levain.slot")}
    canon = types.ModuleType("levain.contracts.canon")
    canon.parse, canon.canonical_bytes = real_canon.parse, real_canon.canonical_bytes
    ctl = types.ModuleType("levain.dispatch.control")
    ctl.CONFIG_NAMESPACE = "ns"
    def parse_item(text):
        if text.startswith("legacy"):                     # a 0.11 'verifiers' item: the current parser refuses it
            raise ValueError("not an item the current parser reads")
        return types.SimpleNamespace(digest="sha256:" + hashlib.sha256(text.encode()).hexdigest())
    ctl.parse_item = parse_item
    ctl.item_bytes = lambda kind, gen, body, at, prev, store_id: real_canon.canonical_bytes(
        {"kind": kind, "generation": gen, "body": body, "issued_at": at.isoformat(), "prev": prev, "store_id": store_id})
    keys = types.ModuleType("levain.cockpit.keys")
    keys.ssh_signer = lambda path, namespace: ("FP", lambda raw: "sig")
    rm = types.ModuleType("levain.slot.routemaps")
    rm.policy_digest = real_policy_digest
    cc = types.ModuleType("levain.slot.control_client")
    cc.ControlClient = _FakeControl
    fake.update({"levain.contracts.canon": canon, "levain.dispatch.control": ctl, "levain.cockpit.keys": keys,
                 "levain.slot.routemaps": rm, "levain.slot.control_client": cc})
    fake["levain.contracts"].canon = canon
    fake["levain.dispatch"].control = ctl
    fake["levain.cockpit"].keys = keys
    fake["levain.slot"].routemaps, fake["levain.slot"].control_client = rm, cc
    for k, v in fake.items():
        monkeypatch.setitem(sys.modules, k, v)
    _FakeControl.calls = []
    monkeypatch.setattr(sys, "argv", ["-", "/x", str(d), "/key", "/sock", json.dumps(body), "0", "{}"])
    code = None                      # the script falls off its end (no exit call) after a submit
    try:
        exec(compile(SC.DISPATCH_SIDE, "dispatch_side", "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


@pytest.fixture
def control():
    _FakeControl.head, _FakeControl.calls, _FakeControl.active_error = None, [], None
    yield _FakeControl
    _FakeControl.head, _FakeControl.active_error = None, None


def _map(tmp_path):
    return SC.build_bodies(_args(tmp_path))["routemap"]


def test_a_legacy_verifiers_row_as_the_top_item_does_not_kill_the_dispatch_side(tmp_path, monkeypatch, capsys, control):
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, _map(tmp_path),
                                   extra=[(4, "verifiers", "legacy 0.11 item", "s")])
    assert code is None and "stage-failed" not in out and out.strip().splitlines()[-1] == "staged gen=4"


def test_the_two_guest_scripts_share_the_one_kind_filter():
    assert f"KINDS = {SC.KINDS!r}\n" in SC.DISPATCH_SIDE and f"KINDS = {SC.KINDS!r}\n" in SC.STORE_SCRIPT
    assert "__KINDS__" not in SC.DISPATCH_SIDE + SC.STORE_SCRIPT


def test_a_first_stage_submits_both_items_and_records_the_map(tmp_path, monkeypatch, capsys, control):
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, _map(tmp_path))
    assert code is None and out.strip().splitlines()[-1] == "staged gen=4"
    assert control.calls == ["ROUTEMAP-ACTIVE", "CONFIG-SUBMIT", "ROUTE-SUBMIT"]
    rec = tmp_path / "ctl" / SC.STAGED_RECORD
    assert json.loads(rec.read_text())["generation"] == 4 and rec.stat().st_mode & 0o777 == 0o600


def test_a_same_input_rerun_submits_nothing(tmp_path, monkeypatch, capsys, control):
    # the recorded failure (I): the second run's ROUTE-SUBMIT was refused (CONTROL already held generation 4)
    body = _map(tmp_path)
    _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    assert code == 0 and out.strip().splitlines()[-1] == "staged gen=4" and "skipped" in out
    assert control.calls == ["ROUTEMAP-ACTIVE"]


def _route(body, i=0):
    return body["routes"][i]


VARIATIONS = {
    "backend": lambda b: _route(b)["models"][0].__setitem__("backend", "other/backend-x"),
    "credential": lambda b: [r["policy"].__setitem__("credential", "or-key-2") for r in b["routes"]],
    "price": lambda b: _route(b)["models"][0]["price"].__setitem__("in", 3_000_000),
    "body_policy": lambda b: _route(b).__setitem__("body_policy", {"drop": ["metadata"]}),
    "upstream": lambda b: _route(b)["policy"]["upstream"].__setitem__("base_path", "/api/v2"),
    "retention": lambda b: _route(b).__setitem__("retention_claim", "zero retention, claimed"),
}


@pytest.mark.parametrize("what", sorted(VARIATIONS))
def test_a_changed_map_is_submitted_again_above_the_active_generation(tmp_path, monkeypatch, capsys, control, what):
    # the recorded class (L1 HIGH-1, all four L3 lineages): same generation + same route ids was taken for the same map
    body = _map(tmp_path)
    _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    changed = json.loads(json.dumps(body))
    VARIATIONS[what](changed)
    assert changed != body
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, changed)
    assert code is None and "skipped" not in out and out.strip().splitlines()[-1] == "staged gen=5"
    assert control.calls == ["ROUTEMAP-ACTIVE", "CONFIG-SUBMIT", "ROUTE-SUBMIT"]
    assert control.head[2]["body"] == changed                      # CONTROL now enforces the changed map
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, changed)     # and the same changed input is then skipped
    assert code == 0 and "skipped" in out and control.calls == ["ROUTEMAP-ACTIVE"]


def test_a_changed_verifier_item_is_submitted_again(tmp_path, monkeypatch, capsys, control):
    body = _map(tmp_path)
    _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    con = sqlite3.connect(tmp_path / "ctl" / "control.db")
    con.execute("UPDATE items SET item = ? WHERE kind = 'verifier'", ('{"v":2}',))
    con.commit()
    con.close()
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    assert "skipped" not in out and control.calls == ["ROUTEMAP-ACTIVE", "CONFIG-SUBMIT", "ROUTE-SUBMIT"]


def test_a_store_item_newer_than_the_active_map_resubmits(tmp_path, monkeypatch, capsys, control):
    body = _map(tmp_path)
    _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    _store(tmp_path / "ctl", [(4, "provider_policy", "{\"p\":2}", "s")])   # e.g. `policy` mode signed a new generation
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    assert "skipped" not in out and out.strip().splitlines()[-1] == "staged gen=5"


def test_an_active_map_this_stage_has_no_record_of_is_not_trusted(tmp_path, monkeypatch, capsys, control):
    body = _map(tmp_path)
    _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    (tmp_path / "ctl" / SC.STAGED_RECORD).unlink()
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, body)
    assert "skipped" not in out and out.strip().splitlines()[-1] == "staged gen=5"


def test_a_routemap_active_failure_other_than_none_active_submits_nothing(tmp_path, monkeypatch, capsys, control):
    control.active_error = Refused("control-down", "the socket closed")
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, _map(tmp_path))
    assert code == 1 and "stage-failed submit: Refused: control-down" in out and control.calls == ["ROUTEMAP-ACTIVE"]


# ---- B and C (class): the keys/cred installer (run as root in the guest) -----------------------------------------------------------

def _install(tmp_path, *, make_keys=True, stdin='{"a":1}', mode="new"):
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    if make_keys:
        (store / "keys").mkdir(exist_ok=True)
    fb = tmp_path / "fakebin"
    fb.mkdir(exist_ok=True)
    (fb / "chown").write_text("#!/bin/sh\necho \"$@\" >> " + str(tmp_path / "chown.log") + "\n")      # never a real chown
    (fb / "id").write_text(f"#!/bin/sh\necho {os.getuid()}\n")                                          # the keys account = us
    for f in ("chown", "id"):
        os.chmod(fb / f, 0o755)
    r = subprocess.run(["/bin/sh", "-c", T.INSTALL_SCRIPT, "sh", str(store), "or-key", mode], input=stdin, capture_output=True,
                       text=True, env=dict(os.environ, PATH=f"{fb}:{os.environ['PATH']}"))
    log = tmp_path / "chown.log"
    return r, store / "keys" / "cred", log.read_text() if log.exists() else ""


def test_the_credential_installer_refuses_a_directory_or_link_named_cred_json(tmp_path):
    for kind in ("dir", "link"):
        base = tmp_path / kind
        base.mkdir()
        d = base / "store" / "keys" / "cred"
        d.mkdir(parents=True)
        os.chmod(d, 0o700)
        victim = base / "victim"
        victim.write_text("ROOT FILE")
        if kind == "dir":
            (d / "or-key.json").mkdir()
        else:
            (d / "or-key.json").symlink_to(victim)
        r, d, _ = _install(base, mode="replace")
        assert r.returncode == 0 and r.stdout.startswith("cred-refused or-key:") and "directory or a link" in r.stdout, r.stderr
        assert victim.read_text() == "ROOT FILE" and not (d / "or-key.json.new").exists()
        assert (d / "or-key.json").is_dir() == (kind == "dir") and (d / "or-key.json").is_symlink() == (kind == "link")


def test_the_credential_installer_answers_a_failed_chown_with_a_refusal_not_a_bare_exit(tmp_path):
    for stage in ("cred", "new"):
        base = tmp_path / stage
        store = base / "store"
        (store / "keys").mkdir(parents=True)
        if stage == "new":                                    # cred exists and is ours: only the credential file's chown fails
            (store / "keys" / "cred").mkdir()
            os.chmod(store / "keys" / "cred", 0o700)
        fb = base / "fakebin"
        fb.mkdir()
        (fb / "chown").write_text("#!/bin/sh\nexit 1\n")
        (fb / "id").write_text(f"#!/bin/sh\necho {os.getuid()}\n")
        for f in ("chown", "id"):
            os.chmod(fb / f, 0o755)
        r = subprocess.run(["/bin/sh", "-c", T.INSTALL_SCRIPT, "sh", str(store), "or-key", "new"], input='{"a":1}', capture_output=True,
                           text=True, env=dict(os.environ, PATH=f"{fb}:{os.environ['PATH']}"))
        assert r.returncode == 0 and r.stdout.startswith("cred-refused or-key:") and "chown" in r.stdout, (stage, r.stdout, r.stderr)
        assert "cred-installed" not in r.stdout
        assert not (store / "keys" / "cred" / "or-key.json.new").exists() and not (store / "keys" / "cred" / "or-key.json").exists()


def test_the_credential_installer_makes_keys_cred_0700_when_it_is_absent(tmp_path):
    r, d, chowns = _install(tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "cred-installed or-key", r.stderr       # recorded: .json.new "No such file"
    assert (d.stat().st_mode & 0o777) == 0o700 and (d / "or-key.json").read_text() == '{"a":1}'
    assert f"-h {T.KEYS_USER}:{T.KEYS_USER} {d}\n" in chowns and f"-h {T.KEYS_USER}:{T.KEYS_USER} {d}/or-key.json.new" in chowns


def test_the_credential_installer_never_makes_keys_itself(tmp_path):
    r, d, chowns = _install(tmp_path, make_keys=False)
    assert r.returncode == 0 and r.stdout.startswith("cred-refused or-key:") and "absent or a link" in r.stdout
    assert not (tmp_path / "store" / "keys").exists() and chowns == ""


def test_the_credential_installer_refuses_a_linked_cred_and_leaves_the_target(tmp_path):
    target = tmp_path / "root-owned"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    (tmp_path / "store" / "keys").mkdir(parents=True)
    (tmp_path / "store" / "keys" / "cred").symlink_to(target)
    r, d, chowns = _install(tmp_path)
    assert r.stdout.startswith("cred-refused or-key:") and "not a 0700 directory" in r.stdout
    assert target.stat().st_mode & 0o777 == 0o755 and list(target.iterdir()) == [] and chowns == ""


def test_the_credential_installer_rechecks_an_existing_cred_and_refuses_a_wrong_mode(tmp_path):
    (tmp_path / "store" / "keys" / "cred").mkdir(parents=True)
    os.chmod(tmp_path / "store" / "keys" / "cred", 0o755)
    r, d, _ = _install(tmp_path)
    assert r.stdout.startswith("cred-refused or-key:") and "not a 0700 directory" in r.stdout
    assert d.stat().st_mode & 0o777 == 0o755 and not (d / "or-key.json").exists()


def test_the_credential_installer_refuses_a_wrong_owner(tmp_path):
    (tmp_path / "store" / "keys" / "cred").mkdir(parents=True)
    os.chmod(tmp_path / "store" / "keys" / "cred", 0o700)
    fb = tmp_path / "fakebin"
    fb.mkdir()
    (fb / "id").write_text(f"#!/bin/sh\necho {os.getuid() + 1}\n")       # the keys account is someone else
    os.chmod(fb / "id", 0o755)
    r = subprocess.run(["/bin/sh", "-c", T.INSTALL_SCRIPT, "sh", str(tmp_path / "store"), "or-key", "new"], input="{}",
                       capture_output=True, text=True, env=dict(os.environ, PATH=f"{fb}:{os.environ['PATH']}"))
    assert r.stdout.startswith("cred-refused or-key:") and "not a 0700 directory" in r.stdout


def test_the_credential_installer_refuses_a_planted_half_written_link(tmp_path):
    d = tmp_path / "store" / "keys" / "cred"
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    victim = tmp_path / "victim"
    victim.write_text("ROOT FILE")
    (d / "or-key.json.new").symlink_to(victim)
    r, d, _ = _install(tmp_path)
    assert r.stdout.startswith("cred-refused or-key:") and victim.read_text() == "ROOT FILE"
