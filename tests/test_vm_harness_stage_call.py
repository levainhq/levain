"""levain#133 (S14 harness fixes from the 1118 VM sitting, project_memory/1118-vm/RESULTS.md findings B, C, D, F, I, J, L).

Each test replays the input the sitting recorded failing: the refusal text, the cwd, the missing file. The guest itself is never
touched; ``run`` (the one subprocess seam of ``stage_call``) is faked where sudo or the guest's accounts would be needed."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts.vm_harness import stage_call as SC
from scripts.vm_harness import threec10 as T


def _args(tmp_path: Path, **kw) -> argparse.Namespace:
    d = dict(levain_root=str(Path(__file__).resolve().parent.parent), python=sys.executable, as_user="_levain_dispatch",
             dispatch_dir="/private/var/levain/dispatch", run_dir="/private/var/run/levain", data_root=str(tmp_path / "w3" / "cockpit"),
             signing_key="/private/var/levain/dispatch/w3-key", key_pub=str(tmp_path / "key.pub"), prep_dir=str(tmp_path / "w3"),
             custody="tpm", models=SC.DEFAULT_MODELS, backends="", cred="or-key", template="wave3-r0", write_path="wave3/hello.txt",
             verifier_id="wave3-bb", verifier_argv="python3 -m pytest -q", price_in=2_000_000, price_out=8_000_000,
             price_source="https://openrouter.ai/models", no_pin=False, dry_submit=False, repo=str(tmp_path / "repo"), git="git")
    d.update(kw)
    return argparse.Namespace(**d)


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


# ---- J: provider_policy read: [] refused every contract ----------------------------------------------------------------------

def test_the_policy_read_covers_the_contract_path_the_sitting_filed(tmp_path):
    from levain.dispatch.policy import ProviderPolicy
    a = _args(tmp_path)
    body = SC.build_bodies(a)["provider_policy"]
    pol = ProviderPolicy.from_mapping(body) if hasattr(ProviderPolicy, "from_mapping") else ProviderPolicy.parse(body)
    assert pol.refusal("openrouter", ["wave3/hello.txt"], ["wave3/hello.txt"]) is None
    # the recorded failing input: an empty read list
    empty = ProviderPolicy.from_mapping({"never": SC.NEVER, "providers": {"openrouter": {"read": []}}}) \
        if hasattr(ProviderPolicy, "from_mapping") else ProviderPolicy.parse({"never": SC.NEVER, "providers": {"openrouter": {"read": []}}})
    assert "outside what provider openrouter may read" in empty.refusal("openrouter", ["wave3/hello.txt"], ["wave3/hello.txt"])
    # a different write path stays covered (the glob follows --write-path)
    other = SC.build_bodies(_args(tmp_path, write_path="out/a/b.txt"))["provider_policy"]
    assert other["providers"]["openrouter"]["read"] == ["out/**"]


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


# ---- F and D: prep installs the .pub beside the private key, and the data root is writable to the dispatch account --------------

@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_prep_installs_the_pub_and_opens_the_data_root_for_writing(tmp_path, monkeypatch):
    a = _args(tmp_path)
    calls = []
    real = SC.run

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        calls.append(list(argv))
        if argv[0] == "/usr/bin/ssh-keygen" or argv[0] == "/bin/chmod":
            return real(argv, stdin=stdin, env=env, timeout=timeout)
        if "ledger" in argv:
            (Path(a.data_root) / "cockpit.db").write_text("x")
        return 0, "", ""

    monkeypatch.setattr(SC, "run", fake_run)
    monkeypatch.setattr(SC, "init_cockpit", lambda root: None)
    sc_path = SC.sys.path[:]
    try:
        assert SC.mode_prep(a) == 0
    finally:
        SC.sys.path[:] = sc_path
    installs = [c for c in calls if c[:3] == ["sudo", "-n", "/usr/bin/install"]]
    dests = {c[-1] for c in installs}
    assert dests == {f"{a.dispatch_dir}/w3-key", f"{a.dispatch_dir}/w3-key.pub"}      # the recorded failure: no .pub
    mode = {c[-1]: c[c.index("-m") + 1] for c in installs}
    assert mode[f"{a.dispatch_dir}/w3-key"] == "0600" and mode[f"{a.dispatch_dir}/w3-key.pub"] == "0644"
    data = Path(a.data_root)
    assert data.stat().st_mode & 0o777 & 0o022 == 0o022 or data.stat().st_mode & 0o002   # world-writable directory (sqlite -shm)
    assert (data / "cockpit.db").stat().st_mode & 0o006 == 0o006
    assert data.parent.stat().st_mode & 0o005 == 0o005                                    # searchable by the dispatch account


# ---- L: the dispatch account OWNS the repo -----------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repo_mode_chowns_the_repo_to_the_dispatch_account_and_writes_no_system_config(tmp_path, monkeypatch):
    calls = []
    real = SC.run

    def fake_run(argv, *, stdin=None, env=None, timeout=300):
        calls.append(list(argv))
        if argv[0] == "sudo":
            return 0, "", ""
        return real(argv, stdin=stdin, env=env, timeout=timeout)

    monkeypatch.setattr(SC, "run", fake_run)
    assert SC.mode_repo(_args(tmp_path, git=shutil.which("git"))) == 0
    sudo = [c for c in calls if c[0] == "sudo"]
    assert sudo == [["sudo", "-n", "/usr/sbin/chown", "-R", "_levain_dispatch", str(tmp_path / "repo")]]
    assert not any("safe.directory" in " ".join(c) for c in calls)


# ---- I: stage is idempotent ---------------------------------------------------------------------------------------------------------

class _FakeControl:
    """What the dispatch-side script sees of CONTROL; ``active`` is the ROUTEMAP-ACTIVE answer (None: no map active)."""
    active = None
    calls: list = []

    def __init__(self, sock):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def call(self, op, **kw):
        _FakeControl.calls.append(op)
        if op == "ROUTEMAP-ACTIVE":
            if _FakeControl.active is None:
                raise RuntimeError("no-routemap")
            return _FakeControl.active
        return {"generation": 4}


def _run_dispatch_side(tmp_path, monkeypatch, capsys, active):
    d = tmp_path / "ctl"
    d.mkdir(exist_ok=True)
    if not (d / "control.db").exists():
        con = sqlite3.connect(d / "control.db")
        con.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        con.execute("CREATE TABLE items(generation INT, kind TEXT, item TEXT, signature TEXT)")
        con.executemany("INSERT INTO meta VALUES(?,?)", [("store_id", "s1"), ("trust_root", "FP")])
        con.executemany("INSERT INTO items VALUES(?,?,?,?)", [(1, "provider_policy", "p", "s"), (2, "verifier", "v", "s"),
                                                              (3, "templates", "t", "s")])
        con.commit()
        con.close()
    fake = {
        "levain": types.ModuleType("levain"), "levain.contracts": types.ModuleType("levain.contracts"),
        "levain.dispatch": types.ModuleType("levain.dispatch"), "levain.cockpit": types.ModuleType("levain.cockpit"),
        "levain.slot": types.ModuleType("levain.slot"),
    }
    canon = types.ModuleType("levain.contracts.canon")
    canon.parse = lambda b: {"raw": 1}
    ctl = types.ModuleType("levain.dispatch.control")
    ctl.CONFIG_NAMESPACE = "ns"
    ctl.parse_item = lambda text: types.SimpleNamespace(digest="d")
    ctl.item_bytes = lambda *a, **k: b"raw"
    keys = types.ModuleType("levain.cockpit.keys")
    keys.ssh_signer = lambda path, namespace: ("FP", lambda raw: "sig")
    cc = types.ModuleType("levain.slot.control_client")
    cc.ControlClient = _FakeControl
    fake.update({"levain.contracts.canon": canon, "levain.dispatch.control": ctl, "levain.cockpit.keys": keys,
                 "levain.slot.control_client": cc})
    fake["levain.contracts"].canon = canon
    fake["levain.dispatch"].control = ctl
    fake["levain.cockpit"].keys = keys
    fake["levain.slot"].control_client = cc
    for k, v in fake.items():
        monkeypatch.setitem(sys.modules, k, v)
    _FakeControl.active, _FakeControl.calls = active, []
    body = {"format": "levain-dispatch-routemap/3", "routes": [{"policy": {"route": "or-a"}}, {"policy": {"route": "or-b"}}]}
    monkeypatch.setattr(sys, "argv", ["-", "/x", str(d), "/key", "/sock", json.dumps(body), "0", "{}"])
    code = None                      # the script falls off its end (no exit call) on a fresh submit
    try:
        exec(compile(SC.DISPATCH_SIDE, "dispatch_side", "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_a_second_stage_finds_the_generation_active_and_submits_nothing(tmp_path, monkeypatch, capsys):
    # the recorded failure: the second run's ROUTE-SUBMIT was refused (CONTROL already held generation 4, one above the store's top 3)
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, {"generation": 4, "routes": {"or-a": {}, "or-b": {}}})
    assert code == 0 and "staged gen=4" in out and "skipped" in out
    assert _FakeControl.calls == ["ROUTEMAP-ACTIVE"]


def test_a_first_stage_still_submits_both_items(tmp_path, monkeypatch, capsys):
    code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, None)
    assert code is None and out.strip().splitlines()[-1] == "staged gen=4"
    assert _FakeControl.calls == ["ROUTEMAP-ACTIVE", "CONFIG-SUBMIT", "ROUTE-SUBMIT"]


def test_another_active_map_is_not_mistaken_for_this_one(tmp_path, monkeypatch, capsys):
    for act in ({"generation": 3, "routes": {"or-a": {}, "or-b": {}}}, {"generation": 4, "routes": {"or-a": {}}}):
        _code, out = _run_dispatch_side(tmp_path, monkeypatch, capsys, act)
        assert "skipped" not in out and "ROUTE-SUBMIT" in _FakeControl.calls


# ---- B: a fresh install has no keys/cred ------------------------------------------------------------------------------------------

def test_the_credential_installer_makes_keys_cred_0700_when_it_is_absent(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    fb = tmp_path / "fakebin"
    fb.mkdir()
    (fb / "chown").write_text("#!/bin/sh\necho \"$@\" >> " + str(tmp_path / "chown.log") + "\n")
    os.chmod(fb / "chown", 0o755)
    r = subprocess.run(["/bin/sh", "-c", T.INSTALL_SCRIPT, "sh", str(store), "or-key", "new"], input='{"a":1}', capture_output=True,
                       text=True, env=dict(os.environ, PATH=f"{fb}:{os.environ['PATH']}"))
    d = store / "keys" / "cred"
    assert r.returncode == 0 and r.stdout.strip() == "cred-installed or-key", r.stderr       # recorded: .json.new "No such file"
    assert (d.stat().st_mode & 0o777) == 0o700 and (d / "or-key.json").read_text() == '{"a":1}'
    assert f"{T.KEYS_USER}:{T.KEYS_USER} {d}" in (tmp_path / "chown.log").read_text()
