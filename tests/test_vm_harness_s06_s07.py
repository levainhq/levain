"""The two guest drivers the VM sitting lacked: S06 (ONE ``done`` occurrence written by the product from a real vendor exit)
and S07 (the Workspace stager's W1 real fire with his signed policy). No VM, no vendor, no network, no real key: the guest is
FAKE (the same ``guest(argv, timeout)`` / ``stdin_run`` pattern as ``test_vm_harness_journey_threec10.py``), the host-side
prep runs against hand-made SQLite files, and the one end-to-end proof of the S06 mechanics fires the REAL controller against
a launcher whose child is a real gated process (the pattern of ``test_agents_session_vendor_exit.py``). Each case names the
line whose removal turns it red.

Not proven here, by construction: that a real vendor exits 0 under the production render blob, that the guest's ssh-keygen
verifies his key's signature, that the dispatch run server stages the bundle. That is the sitting's first observation."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.vm_harness import runbook as RB
from scripts.vm_harness import s06_done_driver as S6
from scripts.vm_harness import s07_stager_fire as S7
from scripts.vm_harness.host_profile import DARWIN, LINUX
from tests.test_agents_session_launch import Stub, rig  # noqa: F401  (the pytest fixture)

HARNESS = Path(S6.__file__).resolve().parent
HOME = "/Users/admin"
PRIVATE = b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


# ======================================================================================================================
# S07 host-side prep: which files go to the guest, with what mode, and what can NEVER be selected
# ======================================================================================================================

def _make_host(tmp_path: Path, *, anchor=b"anchor-store-id seq=8 head=abc\n", log_rows=3):
    consent = tmp_path / "home" / ".levain" / "consent"
    cockpit = tmp_path / "flow" / "state" / "bridge" / "cockpit"
    (cockpit / "anchor").mkdir(parents=True)
    consent.mkdir(parents=True)
    c = sqlite3.connect(consent / "control.db")
    c.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                    "CREATE TABLE items (seq INTEGER PRIMARY KEY, kind TEXT, body TEXT);"
                    "INSERT INTO meta VALUES ('store_id', '0e1214f2');"
                    "INSERT INTO items VALUES (1, 'provider_policy', 'gen1'), (2, 'provider_policy', 'gen2');")
    c.commit()
    c.close()
    k = sqlite3.connect(cockpit / "cockpit.db")
    k.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                    "CREATE TABLE authority_log (seq INTEGER PRIMARY KEY, record TEXT);"
                    "CREATE TABLE tokens (id INTEGER PRIMARY KEY, hash TEXT);"
                    "CREATE TABLE paired_devices (id INTEGER PRIMARY KEY, token_hash TEXT);"
                    "CREATE TABLE pendings (id INTEGER PRIMARY KEY, body TEXT);"
                    "INSERT INTO meta VALUES ('store_id', 'cockpit-1');"
                    "INSERT INTO tokens VALUES (1, 'PHONE-TOKEN-HASH-1'), (2, 'PHONE-TOKEN-HASH-2');"
                    "INSERT INTO paired_devices VALUES (1, 'DEVICE-SECRET');"
                    "INSERT INTO pendings VALUES (1, 'PENDING-REQUEST');")
    for i in range(1, log_rows + 1):
        k.execute("INSERT INTO authority_log VALUES (?, ?)", (i, f"link-{i}"))
    k.commit()
    k.close()
    (cockpit / "anchor" / "authority.anchor").write_bytes(anchor)
    return consent, cockpit


def _tables(db: Path) -> set[str]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        c.close()


def test_plan_pack_is_exactly_the_four_files_with_their_modes():
    """Revert: add a fifth PackItem (a key, the whole cockpit.db) to ``plan_pack``."""
    plan = S7.plan_pack(HOME)
    files = {i.role: (i.dest, i.mode) for i in plan if i.kind == "file"}
    assert files == {"control_db": (f"{HOME}/.levain/consent/control.db", 0o600),
                     "claude_code_json": (f"{HOME}/.levain/consent/claude_code.json", 0o600),
                     "cockpit_db": (f"{HOME}/.levain-fire-cockpit/cockpit.db", 0o600),
                     "anchor": (f"{HOME}/.levain-fire-cockpit/anchor/authority.anchor", 0o600)}
    dirs = {i.dest: i.mode for i in plan if i.kind == "dir"}
    assert dirs[f"{HOME}/.levain/consent"] == 0o700          # ControlStore refuses a group/other bit
    assert dirs[f"{HOME}/.levain-fire-cockpit"] == 0o700 and dirs[f"{HOME}/.levain-fire-cockpit/anchor"] == 0o700
    order = [i.dest for i in plan]
    for i in plan:                                             # a parent dir is planned before anything under it
        parent = os.path.dirname(i.dest)
        if parent != HOME:
            assert parent in order[:order.index(i.dest)] or parent == f"{HOME}/.levain"


@pytest.mark.parametrize("bad", [
    "/Users/phillipclapham/.levain/keys/anything",
    "/Users/phillipclapham/.levain/keys",
    "/Users/phillipclapham/.levain-sign/cockpit/cockpit.db",
    "/Users/phillipclapham/.levain-sign",
    "/Users/phillipclapham/.levain/consent/queue.db",
    "/Users/phillipclapham/.levain/consent/dispatch.db",
    "/somewhere/id_ed25519", "/somewhere/id_rsa.pub.bak", "/somewhere/server.key", "/somewhere/cert.pem",
    "/Users/phillipclapham/Briefcase/flow/state/bridge/cockpit/cockpit.db",   # the WHOLE cockpit db
])
def test_forbidden_sources_are_refused(bad):
    """Revert: drop a pattern from ``FORBIDDEN_PARTS`` / ``FORBIDDEN_NAMES`` (each row here names one)."""
    assert S7.forbidden_reason(bad) is not None


@pytest.mark.parametrize("ok", [
    "/Users/phillipclapham/.levain/consent/control.db",
    "/Users/phillipclapham/Briefcase/flow/state/bridge/cockpit/anchor/authority.anchor",
])
def test_the_two_allowed_sources_are_not_forbidden(ok):
    assert S7.forbidden_reason(ok) is None


def test_a_symlink_into_a_forbidden_tree_is_forbidden(tmp_path):
    """Revert: ``os.path.realpath`` in ``forbidden_reason`` (a plain abspath misses the link)."""
    sign = tmp_path / ".levain-sign" / "cockpit"
    sign.mkdir(parents=True)
    (sign / "authority.anchor").write_bytes(b"x")
    link = tmp_path / "innocent.anchor"
    link.symlink_to(sign / "authority.anchor")
    assert S7.forbidden_reason(str(link)) is not None


def test_build_pack_copies_only_the_policy_and_the_authority_tables(tmp_path):
    """Revert: copy ``cockpit.db`` whole instead of ``trim_cockpit`` (tokens and paired_devices go to the guest)."""
    consent, cockpit = _make_host(tmp_path)
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=HOME)
    by = {i.role: i for i in pack.items if i.kind == "file"}
    assert set(by) == {"control_db", "claude_code_json", "cockpit_db", "anchor"}
    assert _tables(by["cockpit_db"].src) == {"meta", "authority_log"}
    c = sqlite3.connect(f"file:{by['cockpit_db'].src}?mode=ro", uri=True)
    assert [r[0] for r in c.execute("SELECT seq FROM authority_log ORDER BY seq")] == [1, 2, 3]
    c.close()
    blob = b"".join(i.src.read_bytes() for i in by.values())
    for secret in (b"PHONE-TOKEN-HASH", b"DEVICE-SECRET", b"PENDING-REQUEST"):
        assert secret not in blob
    assert by["anchor"].src.read_bytes() == (cockpit / "anchor" / "authority.anchor").read_bytes()
    cd = sqlite3.connect(f"file:{by['control_db'].src}?mode=ro", uri=True)
    assert cd.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    cd.close()
    S7.check_pack(pack)                                       # a freshly built pack passes its own check


def test_the_guest_claude_code_json_names_the_guest_cockpit_never_a_host_path(tmp_path):
    """Revert: copy the host ``claude_code.json`` (it names the host's signing cockpit) instead of writing the one line."""
    consent, cockpit = _make_host(tmp_path)
    (consent / "claude_code.json").write_text(json.dumps({"cockpit_data_root": "/Users/phillipclapham/.levain-sign/cockpit"}))
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=HOME)
    src = next(i for i in pack.items if i.role == "claude_code_json").src
    assert json.loads(src.read_text()) == {"cockpit_data_root": f"{HOME}/.levain-fire-cockpit"}


def test_build_pack_reads_the_anchor_before_the_log(tmp_path, monkeypatch):
    """The log may run ahead of the anchor, never behind: swap the two reads and the order assertion fails."""
    consent, cockpit = _make_host(tmp_path)
    seen = []
    real_trim, real_anchor = S7.trim_cockpit, S7.read_anchor
    monkeypatch.setattr(S7, "read_anchor", lambda p: (seen.append("anchor"), real_anchor(p))[1])
    monkeypatch.setattr(S7, "trim_cockpit", lambda s, d: (seen.append("log"), real_trim(s, d))[1])
    S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=HOME)
    assert seen == ["anchor", "log"]


@pytest.mark.parametrize("what,msg", [
    ("control_db", "no signed policy"),
    ("anchor", "anchor"),
    ("cockpit_db", "cockpit"),
    ("empty_log", "authority log"),
])
def test_build_pack_refuses_cleanly_when_a_source_is_absent(tmp_path, what, msg):
    """Revert the existence/emptiness checks in ``build_pack``: a pack with nothing to verify the policy against would
    go to the guest and fail there as an obscure ``workspace-policy-unavailable``."""
    consent, cockpit = _make_host(tmp_path, log_rows=0 if what == "empty_log" else 3)
    if what == "control_db":
        (consent / "control.db").unlink()
    if what == "anchor":
        (cockpit / "anchor" / "authority.anchor").unlink()
    if what == "cockpit_db":
        (cockpit / "cockpit.db").unlink()
    with pytest.raises(S7.PackRefused, match=msg):
        S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=HOME)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())     # nothing half-built is left to scp


@pytest.mark.parametrize("sub", [".levain-sign", ".levain/keys"])
def test_build_pack_refuses_a_source_directory_under_a_private_tree(tmp_path, sub):
    """Revert: the ``forbidden_reason`` call on each source in ``build_pack``."""
    base = tmp_path / sub
    consent, cockpit = _make_host(tmp_path / "h")
    moved = base / "cockpit"
    shutil.copytree(cockpit, moved)
    with pytest.raises(S7.PackRefused, match="private"):
        S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=moved, guest_home=HOME)


def test_check_pack_refuses_a_pack_that_gained_a_file_a_table_or_a_private_key(tmp_path):
    """Revert any of the three checks in ``check_pack`` (the item set, the table set, the private-key scan)."""
    consent, cockpit = _make_host(tmp_path)
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=HOME)
    extra = S7.Pack(pack.items + (S7.PackItem("file", f"{HOME}/.levain/keys/id_ed25519", 0o600, "key", tmp_path / "k"),))
    with pytest.raises(S7.PackRefused, match="not in the plan"):
        S7.check_pack(extra)
    whole = tmp_path / "whole.db"
    shutil.copy2(cockpit / "cockpit.db", whole)               # the full cockpit.db standing in for the trimmed one
    swapped = S7.Pack(tuple(S7.PackItem(i.kind, i.dest, i.mode, i.role, whole) if i.role == "cockpit_db" else i
                            for i in pack.items))
    with pytest.raises(S7.PackRefused, match="tables"):
        S7.check_pack(swapped)
    keyed = tmp_path / "keyed.json"
    keyed.write_bytes(b'{"cockpit_data_root": "x"}' + PRIVATE)
    leaked = S7.Pack(tuple(S7.PackItem(i.kind, i.dest, i.mode, i.role, keyed) if i.role == "claude_code_json" else i
                           for i in pack.items))
    with pytest.raises(S7.PackRefused, match="private key"):
        S7.check_pack(leaked)
    modes = S7.Pack(tuple(S7.PackItem(i.kind, i.dest, 0o644, i.role, i.src) if i.role == "control_db" else i
                          for i in pack.items))
    with pytest.raises(S7.PackRefused, match="mode"):
        S7.check_pack(modes)


# ---- installing the pack into a (fake) guest --------------------------------------------------------------------------

class FakeGuest:
    """Runs the install scripts for real, locally, against a temp 'guest home': the script, not a mock of it, is graded."""

    def __init__(self, home: Path):
        self.home = home
        self.argvs: list[list[str]] = []
        self.stdins: list[str] = []

    def guest(self, argv, timeout=60):
        assert list(argv[:4]) == list(RB.CAPTURE_ARGV)
        inner = list(argv[4:])
        self.argvs.append(inner)
        r = subprocess.run(["/bin/sh", "-c", RB.CAPTURE_ARGV[2], "sh", *inner], capture_output=True, text=True, timeout=30)
        return r.stdout

    def stdin_run(self, argv, text, timeout=60):
        self.argvs.append(list(argv))
        self.stdins.append(text)
        argv = [sys.executable if a == DARWIN.python else a for a in argv]
        r = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=30)
        if r.returncode:
            from scripts.vm_harness.driver import GuestError
            raise GuestError(r.returncode, r.stderr[-200:])
        return r.stdout


def test_install_pack_writes_the_files_with_the_planned_modes(tmp_path):
    """Revert: ``os.chmod``/the ``mode`` argument in ``INSTALL_SCRIPT`` (a umask-default 0644 control.db makes
    ``ControlStore`` refuse it in the guest)."""
    consent, cockpit = _make_host(tmp_path / "h")
    home = tmp_path / "guesthome"
    home.mkdir()
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=str(home))
    g = FakeGuest(home)
    os.umask(0o022)
    S7.install_pack(g.guest, g.stdin_run, DARWIN, pack)
    for it in pack.items:
        st = os.lstat(it.dest)
        assert (st.st_mode & 0o777) == it.mode, it.dest
        if it.kind == "file":
            assert Path(it.dest).read_bytes() == it.src.read_bytes()


def test_install_pack_refuses_when_the_guest_already_has_a_store_and_writes_nothing(tmp_path):
    """Revert: the ``exists`` probe in ``install_pack`` (a guest-side ``levain init`` store would be overwritten, and
    ``ControlStore`` pins its trust root once)."""
    consent, cockpit = _make_host(tmp_path / "h")
    home = tmp_path / "guesthome"
    (home / ".levain" / "consent").mkdir(parents=True)
    (home / ".levain" / "consent" / "control.db").write_bytes(b"theirs")
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=str(home))
    g = FakeGuest(home)
    with pytest.raises(S7.PackRefused, match="already exists"):
        S7.install_pack(g.guest, g.stdin_run, DARWIN, pack)
    assert (home / ".levain" / "consent" / "control.db").read_bytes() == b"theirs"
    assert g.stdins == []                                      # not one byte of the pack was sent


def test_a_second_install_of_the_same_pack_refuses_never_overwrites(tmp_path):
    """Revert: ``O_EXCL`` in the file script."""
    consent, cockpit = _make_host(tmp_path / "h")
    home = tmp_path / "guesthome"
    home.mkdir()
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=str(home))
    g = FakeGuest(home)
    S7.install_pack(g.guest, g.stdin_run, DARWIN, pack)
    with pytest.raises(S7.PackRefused, match="already exists"):
        S7.install_pack(g.guest, g.stdin_run, DARWIN, pack)


def test_the_pack_contents_travel_on_stdin_never_in_an_argv(tmp_path):
    """Revert: pass the payload as an argument instead of stdin."""
    consent, cockpit = _make_host(tmp_path / "h")
    home = tmp_path / "guesthome"
    home.mkdir()
    pack = S7.build_pack(tmp_path / "out", host_consent=consent, host_cockpit=cockpit, guest_home=str(home))
    g = FakeGuest(home)
    S7.install_pack(g.guest, g.stdin_run, DARWIN, pack)
    flat = "\n".join(" ".join(a) for a in g.argvs)
    for it in pack.items:
        if it.kind == "file":
            assert base64.b64encode(it.src.read_bytes()).decode()[:40] not in flat
    assert g.stdins                                            # the content did go somewhere: stdin


# ======================================================================================================================
# S07 guest ops (the pieces that run on the host too: the stager is the product's, the policy is monkeypatched)
# ======================================================================================================================

def _tiny_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=t", *a],  # noqa: E731
                                    capture_output=True, text=True, check=True).stdout.strip()
    run("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    (repo / "app.yaml").write_text("k: v\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return repo, run("rev-parse", "HEAD")


def _policy(never=("**/*.pem",)):
    from levain.dispatch.policy import ProviderPolicy
    return ProviderPolicy(never=never, providers={})


def test_guest_policy_line_reports_the_signed_never_list_and_hash(monkeypatch):
    """Revert: the ``_control_policy`` call in ``guest_policy`` (a stand-in list would print a different count)."""
    from levain.dispatch import stager
    pol = _policy(("a", "b", "c"))
    monkeypatch.setattr(stager, "_control_policy", lambda: pol)
    line = S7.guest_policy()
    assert line == f"S07-POLICY never=3 hash={pol.content_hash()}"


def test_guest_policy_refuses_in_words_when_the_policy_is_absent(monkeypatch):
    from levain.dispatch import stager

    def boom():
        raise stager.StageRefused("workspace-policy-unavailable", "no dispatch control store holds a provider policy")
    monkeypatch.setattr(stager, "_control_policy", boom)
    assert S7.guest_policy() == "S07-POLICY refused=workspace-policy-unavailable"


def test_guest_judge_runs_the_real_stager_against_the_repo(tmp_path, monkeypatch):
    """Revert: ``build_upload`` in ``guest_judge`` (the line would carry no manifest/commit/bundle numbers)."""
    from levain.dispatch import stager
    repo, head = _tiny_repo(tmp_path)
    monkeypatch.setattr(stager, "_control_policy", lambda: _policy())
    line = S7.guest_judge(str(repo), git="git")
    m = re.fullmatch(r"S07-JUDGE head=([0-9a-f]{40}) clean=yes manifest=(\d+) objects=(\d+) commit=([0-9a-f]{40}) bundle=(\d+)", line)
    assert m and m.group(1) == head and int(m.group(2)) > 0 and int(m.group(3)) > 0 and int(m.group(5)) > 0


def test_guest_judge_says_dirty_for_a_dirty_repo(tmp_path, monkeypatch):
    from levain.dispatch import stager
    repo, _ = _tiny_repo(tmp_path)
    (repo / "scratch.txt").write_text("x")
    monkeypatch.setattr(stager, "_control_policy", lambda: _policy())
    assert " clean=no " in S7.guest_judge(str(repo), git="git")


def test_guest_judge_names_the_stagers_refusal_never_passes(tmp_path, monkeypatch):
    from levain.dispatch import stager
    repo, _ = _tiny_repo(tmp_path)
    (repo / "x.pem").write_text("NOT A KEY")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "pem"], check=True)
    monkeypatch.setattr(stager, "_control_policy", lambda: _policy(("**/*.pem",)))
    line = S7.guest_judge(str(repo), git="git")
    assert line.startswith("S07-JUDGE refused=") and "manifest=" not in line


def test_the_fire_launcher_keeps_the_products_workspace_build_and_the_signed_sources(rig):
    """The 11-06 driver replaced ``build_workspace`` with a stand-in policy: that deletion IS the real fire. Revert: put the
    ``launcher.build_workspace = ...`` override back and this goes red. Also: without the signed-role sources ``spawn``
    refuses ``link-proof-unavailable``."""
    launcher = S7.make_launcher(rig, "/nonexistent/run.sock")
    assert "build_workspace" not in vars(launcher)
    assert launcher._signed_of is not None and launcher._link_of is not None and launcher._feed is not None
    src = (HARNESS / "s07_stager_fire.py").read_text()
    assert "ProviderPolicy(never=" not in src and "launcher.build_workspace =" not in src


def test_the_fire_workspace_member_is_the_resolved_head_with_the_roles_withhold(rig, tmp_path):
    from levain.agents.role import role_withhold
    from tests import test_agents_session_launch as TL
    repo, head = _tiny_repo(tmp_path)
    ws = S7.workspace_for(str(repo), TL.ROLE)
    assert (ws.repo, ws.head, tuple(ws.withhold)) == (str(repo), head, tuple(role_withhold(TL.ROLE)))


def test_guest_fire_refuses_without_the_run_socket_and_fires_nothing(rig, tmp_path, monkeypatch, capsys):
    """Revert: the socket check at the top of ``guest_fire``."""
    called = []
    monkeypatch.setattr(S7, "make_launcher", lambda *a, **k: called.append(1))
    rc = S7.guest_main(["fire", "--repo", str(tmp_path), "--bundle-size", "1", "--sock", str(tmp_path / "nope.sock")])
    out = capsys.readouterr().out
    assert rc == 2 and out.startswith("S07-FIRE refused=") and "run socket" in out and not called


# ======================================================================================================================
# S07 host runner against a fake guest: EXPECTED first, one verdict per step, no fire past a failed G3
# ======================================================================================================================

HEAD = S7.Expect().head
POL = S7.Expect().policy_hash
GOOD = {
    "policy": [f"S07-POLICY never=13 hash={POL}"],
    "judge": [f"S07-JUDGE head={HEAD} clean=yes manifest=1669 objects=28 commit=fdfa5ab0aa75449f8e4d594762909f381f324946 bundle=5770"],
    "fire": ["S07-LAUNCH launched=true session=s-0a1b2c3d4e5f6789",
             f"S07-RECORD state=allocated head={HEAD} manifest_digest=sha256:{'a' * 64} withhold_digest=sha256:{'b' * 64} auth=passthrough",
             "S07-BUNDLE size=5770 stager=5770",
             "S07-VENDOR wait_rc=0 tunnel_bytes=1234",
             "S07-END terminated=true"],
}


class RunnerGuest:
    def __init__(self, tmp_path, outputs=None):
        self.out = {k: list(v) for k, v in (outputs or GOOD).items()}
        self.calls = []
        self.ev = tmp_path / "ev"
        self.first_call_saw_expected = None

    def guest(self, argv, timeout=60):
        assert list(argv[:4]) == list(RB.CAPTURE_ARGV)
        inner = list(argv[4:])
        if self.first_call_saw_expected is None:
            p = self.ev / "EXPECTED.txt"
            self.first_call_saw_expected = p.is_file() and "sha256:" in p.read_text()
        assert inner[:2] == [DARWIN.python, "-I"] and inner[2].endswith("s07_stager_fire.py") and inner[3] == "guest", inner
        op = inner[4]
        self.calls.append((op, inner[5:]))
        lines = self.out[op]
        if lines and lines[0].startswith("RAISE"):
            raise RB.RunbookError("transport lost")
        return "\n".join(lines) + f"\n{RB.RC_MARK} 0\n"


def go7(tmp_path, outputs=None, **kw):
    g = RunnerGuest(tmp_path, outputs)
    g.ev.mkdir()
    cfg = S7.S07Config(DARWIN, repo=kw.pop("repo", f"{HOME}/stager-testrepo"), **kw)
    status, steps, path = S7.run_s07(g.guest, g.ev, cfg)
    return g, {s.id: s for s in steps}, status, path


def test_s07_all_good_passes_with_a_released_vendor(tmp_path):
    g, steps, status, path = go7(tmp_path, release=True)
    assert status == "PASS", [s.line() for s in steps.values()]
    assert [s for s, st in ((k, v.status) for k, v in steps.items()) if st != "PASS"] == []
    assert [c[0] for c in g.calls] == ["policy", "judge", "fire"]
    assert "--release" in g.calls[2][1] and "5770" in g.calls[2][1]       # the bundle size the stager judged rides to the fire
    assert "S07: PASS" in path.read_text()


def test_expected_is_written_before_the_first_guest_call_and_result_names_its_digest(tmp_path):
    """Revert: ``ev.write_expected`` moved after the first ``guest`` call."""
    g, steps, status, path = go7(tmp_path, release=True)
    assert g.first_call_saw_expected is True
    text = (g.ev / "EXPECTED.txt").read_text()
    digest = re.search(r"sha256: ([0-9a-f]{64})", text).group(1)
    assert hashlib.sha256(text.split("\nsha256:")[0].encode()).hexdigest() == digest
    assert digest in path.read_text()
    assert "never=13" in text and S7.Expect().policy_hash in text and "1669" in text


def test_a_generation_1_policy_fails_g3_and_the_fire_is_never_made(tmp_path):
    """Revert: the ``if not g3`` stop in ``run_s07`` (G3 is the cheap R1 test: stop, do not fire)."""
    out = {**GOOD, "policy": ["S07-POLICY never=12 hash=sha256:" + "9" * 64]}
    g, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["G3"].status == "FAIL"
    assert [c[0] for c in g.calls] == ["policy"]
    assert {steps[i].status for i in ("J1", "F1", "F2", "F3", "F4")} == {"UNBOUND"}


def test_an_unavailable_policy_is_a_fail_naming_the_copy_not_a_pass(tmp_path):
    out = {**GOOD, "policy": ["S07-POLICY refused=workspace-policy-unavailable"]}
    g, steps, status, _ = go7(tmp_path, out)
    assert status == "FAIL" and "policy copy" in steps["G3"].detail
    assert [c[0] for c in g.calls] == ["policy"]


def test_a_stager_refusal_at_judge_stops_before_the_fire(tmp_path):
    out = {**GOOD, "judge": ["S07-JUDGE refused=snapshot-never"]}
    g, steps, status, _ = go7(tmp_path, out)
    assert status == "FAIL" and steps["J1"].status == "FAIL"
    assert [c[0] for c in g.calls] == ["policy", "judge"]


@pytest.mark.parametrize("name,cause", [
    ("workspace-policy-unavailable", "policy copy"), ("link-proof-unavailable", "driver"),
    ("staging-missing", "unwired"), ("hook-template-missing", "11-05"), ("ticket-routes-mismatch", "11-05"),
])
def test_a_named_launch_refusal_fails_f1_and_maps_to_its_cause(tmp_path, name, cause):
    out = {**GOOD, "fire": [f"S07-LAUNCH refused=session_refused:{name}", "S07-END terminated=true"]}
    g, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["F1"].status == "FAIL" and cause in steps["F1"].detail
    assert steps["F2"].status == "UNBOUND"                       # no record read from a launch that did not happen


def test_a_record_with_no_server_digest_fails(tmp_path):
    """Revert: the ``manifest_digest=sha256:`` requirement (null = no server ever verified the upload)."""
    out = {**GOOD, "fire": [GOOD["fire"][0],
                            f"S07-RECORD state=allocated head={HEAD} manifest_digest=null withhold_digest=sha256:{'b' * 64} auth=passthrough",
                            *GOOD["fire"][2:]]}
    _, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["F2"].status == "FAIL"


def test_a_refused_record_state_or_a_brokered_auth_fails(tmp_path):
    for n, rec in enumerate((f"S07-RECORD state=refused head={HEAD} manifest_digest=sha256:{'a' * 64} withhold_digest=sha256:{'b' * 64} auth=passthrough",
                f"S07-RECORD state=allocated head={HEAD} manifest_digest=sha256:{'a' * 64} withhold_digest=sha256:{'b' * 64} auth=brokered")):
        out = {**GOOD, "fire": [GOOD["fire"][0], rec, *GOOD["fire"][2:]]}
        (tmp_path / str(n)).mkdir()
        _, steps, status, _ = go7(tmp_path / str(n), out, release=True)
        assert status == "FAIL" and steps["F2"].status == "FAIL"


def test_a_bundle_whose_size_differs_from_the_stagers_fails(tmp_path):
    out = {**GOOD, "fire": [*GOOD["fire"][:2], "S07-BUNDLE size=100 stager=5770", *GOOD["fire"][3:]]}
    _, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["F3"].status == "FAIL"


def test_without_release_the_vendor_step_is_skipped_and_the_result_says_so(tmp_path):
    """Revert: the ``release`` branch: a vendor step that is not run must read SKIP with the words, never PASS."""
    out = {**GOOD, "fire": [*GOOD["fire"][:3], "S07-END terminated=true"]}
    g, steps, status, path = go7(tmp_path, out, release=False)
    assert steps["F4"].status == "SKIP" and "--release" not in g.calls[2][1]
    assert status == "PASS" and "vendor not released" in path.read_text()


def test_a_released_vendor_with_no_tunnel_bytes_fails(tmp_path):
    out = {**GOOD, "fire": [*GOOD["fire"][:3], "S07-VENDOR wait_rc=0 tunnel_bytes=0", "S07-END terminated=true"]}
    _, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["F4"].status == "FAIL"


def test_a_lost_transport_is_an_error_and_the_run_is_incomplete_not_a_pass(tmp_path):
    out = {**GOOD, "fire": ["RAISE"]}
    _, steps, status, _ = go7(tmp_path, out, release=True)
    assert steps["F1"].status == "ERROR" and status == "INCOMPLETE"


def test_a_fire_that_printed_nothing_is_a_fail_not_a_pass(tmp_path):
    out = {**GOOD, "fire": []}
    _, steps, status, _ = go7(tmp_path, out, release=True)
    assert status == "FAIL" and steps["F1"].status == "FAIL"


def test_a_missing_repo_refuses_before_any_guest_call(tmp_path):
    g = RunnerGuest(tmp_path)
    g.ev.mkdir()
    with pytest.raises(RB.RunbookError, match="repo"):
        S7.run_s07(g.guest, g.ev, S7.S07Config(DARWIN, repo=""))
    assert g.calls == [] and not (g.ev / "EXPECTED.txt").exists()


def test_a_relative_repo_refuses(tmp_path):
    g = RunnerGuest(tmp_path)
    g.ev.mkdir()
    with pytest.raises(RB.RunbookError, match="absolute"):
        S7.run_s07(g.guest, g.ev, S7.S07Config(DARWIN, repo="stager-testrepo"))


def test_main_refuses_a_vm_that_is_not_a_clone_before_any_transport(tmp_path, capsys):
    with pytest.raises(SystemExit, match="vend-"):
        S7.main(["run", "my-real-vm", str(tmp_path / "ev"), "--repo", "/x/y"])
    with pytest.raises(SystemExit, match="vend-"):
        S7.main(["install", "my-real-vm", str(tmp_path)])


def test_main_pack_refuses_when_the_host_has_no_policy_and_says_which(tmp_path, capsys):
    rc = S7.main(["pack", str(tmp_path / "out"), "--consent", str(tmp_path / "nope"), "--cockpit", str(tmp_path / "nope2")])
    assert rc == 2 and "no signed policy" in capsys.readouterr().err


def test_guest_argv_runs_the_operator_never_sudo():
    """The fire runs as the guest operator (ControlStore wants the running uid to own its dir): no sudo, no root."""
    argv = S7.guest_argv(DARWIN, "policy")
    assert argv == (DARWIN.python, "-I", f"{DARWIN.tools_dir}/s07_stager_fire.py", "guest", "policy")
    assert "sudo" not in S7.guest_argv(LINUX, "fire", "--repo", "/x")


# ======================================================================================================================
# S06: ONE `done` from a real vendor exit
# ======================================================================================================================

def _show(*, state="done", reason="completed", ended="2026-11-11T10:00:00Z", launched="2026-11-11T09:59:00Z", n=1, episodes=1, oid="oc-1"):
    occs = [{"occurrence_id": oid if i == 0 else f"oc-{i}", "state": state, "reason": reason, "ended_at": ended,
             "launched_at": launched} for i in range(n)]
    eps = [{"content": f"session run {oid} of agent ag-1 ended {state}: {reason}"}] * episodes
    return {"agent": {"agent_id": "ag-1"}, "occurrences": occs, "episodes": eps}


@pytest.mark.parametrize("doc,rc,ok", [
    (_show(), 0, True),
    (_show(state="launched", reason=None, ended=None), 76, False),          # END unproven: the row stays launched
    (_show(state="failed", reason="win_unverified"), 1, False),
    (_show(state="failed", reason="session_refused:workspace"), 1, False),
    (_show(n=2), 0, False),                                                  # not ONE occurrence
    (_show(launched=None), 0, False),
    (_show(episodes=0), 0, False),
    (_show(), 1, False),                                                     # the fire exited nonzero
])
def test_judge_show_is_one_verdict_and_only_the_full_shape_passes(doc, rc, ok):
    """Revert: any one clause of ``judge_show`` (each row above drops exactly one)."""
    raw = S6.judge_show(doc, "oc-1", rc, episodes=True)
    matched, _ = RB.judge([raw], rc, S6.verdict_for(episodes=True))      # the exit code AND the one raw line
    assert matched is ok, raw


def test_judge_show_without_episodes_does_not_ask_for_one():
    raw = S6.judge_show(_show(episodes=0), "oc-1", 0, episodes=False)
    assert re.fullmatch(S6.verdict_for(episodes=False).pattern, raw)


@pytest.mark.parametrize("state,reason,ended,rc,word", [
    ("launched", None, None, 76, "END"),
    ("failed", "win_unverified", "t", 1, "win"),
    ("failed", "session_refused:workspace", "t", 1, "refus"),
    ("failed", "session_exit_1", "t", 1, "vendor"),
    ("launched", None, None, 4, "identity"),
])
def test_diagnose_names_the_cause_of_a_non_pass(state, reason, ended, rc, word):
    assert word.lower() in S6.diagnose(state, reason, ended, rc).lower()


def _facts(**kw):
    base = dict(run_socket=True, euid=501, vendor_ready=True, policy_ok=True, repo_ok=True, tests_importable=True,
                anneal_importable=True, world_fresh=True, details={})
    base.update(kw)
    return S6.Facts(**base)


@pytest.mark.parametrize("field,value,words", [
    ("run_socket", False, "run socket"),
    ("euid", 0, "operator"),
    ("vendor_ready", False, "vendor"),
    ("policy_ok", False, "policy"),
    ("repo_ok", False, "repo"),
    ("tests_importable", False, "tests"),
    ("world_fresh", False, "fire world"),
])
def test_each_absent_precondition_is_a_clean_refusal_naming_it(field, value, words):
    """Revert the matching line in ``preconditions``: the run would go on to fire without it."""
    problems = S6.preconditions(_facts(**{field: value}))
    assert problems and words in " ".join(problems).lower()


def test_all_preconditions_met_is_no_problem():
    assert S6.preconditions(_facts()) == []


def _cfg(tmp_path, **kw):
    return S6.S06Config(workspace_repo="/Users/admin/stager-testrepo", evidence=tmp_path / "ev", vendor_ready=True, **kw)


def test_run_refuses_without_firing_and_still_wrote_expected_first(tmp_path):
    """Revert: the ``problems`` early return in ``run``. A refusal writes EXPECTED (the table is fixed before anything
    runs) and a RESULT of REFUSED; ``fire_once`` is never called."""
    fired = []
    (tmp_path / "ev").mkdir()
    status, path = S6.run(_cfg(tmp_path), _facts(policy_ok=False), fire_once=lambda c: fired.append(1))
    assert status == "REFUSED" and fired == []
    assert (tmp_path / "ev" / "EXPECTED.txt").is_file() and "REFUSED" in path.read_text() and "policy" in path.read_text()


def test_run_writes_expected_before_the_fire_and_passes_on_a_done_row(tmp_path):
    (tmp_path / "ev").mkdir()
    seen = {}

    def fire_once(cfg):
        seen["expected_first"] = (tmp_path / "ev" / "EXPECTED.txt").is_file()
        return 0, "oc-1", _show()
    status, path = S6.run(_cfg(tmp_path), _facts(), fire_once=fire_once)
    assert status == "PASS" and seen["expected_first"]
    text = (tmp_path / "ev" / "EXPECTED.txt").read_text()
    digest = re.search(r"sha256: ([0-9a-f]{64})", text).group(1)
    assert digest in path.read_text() and "S06: PASS" in path.read_text()


def test_run_fails_on_a_launched_row_and_says_end_was_not_proven(tmp_path):
    (tmp_path / "ev").mkdir()
    status, path = S6.run(_cfg(tmp_path), _facts(),
                          fire_once=lambda c: (76, "oc-1", _show(state="launched", reason=None, ended=None)))
    assert status == "FAIL" and "END" in path.read_text()


def test_run_is_incomplete_when_the_fire_raises_not_a_pass(tmp_path):
    (tmp_path / "ev").mkdir()

    def boom(cfg):
        raise TimeoutError("the fire did not return")
    status, path = S6.run(_cfg(tmp_path), _facts(), fire_once=boom)
    assert status == "INCOMPLETE" and "TimeoutError" in path.read_text()


def test_main_refuses_in_words_off_a_guest_and_exits_2(tmp_path, capsys):
    """On this host there is no dispatch run socket, no vendor flag: ``main`` refuses, exit 2, fires nothing."""
    rc = S6.main(["--workspace-repo", "/Users/admin/stager-testrepo", "--evidence", str(tmp_path / "ev")])
    err = capsys.readouterr()
    assert rc == 2 and "refusing" in (err.err + err.out)


# ---- the mechanics, end to end, against the REAL controller with a real gated child instead of a real vendor ------------

class VendorLauncher(Stub):
    def __init__(self, code):
        super().__init__(auth="passthrough")       # what the real DispatchSessionLauncher is: a subscription link's billing
        self.code = code

    def spawn(self, launch):
        from levain.agents import controller as ctl
        self.launches.append(launch)
        child = ctl.GatedChild([sys.executable, "-c", self.code])
        self.children.append(child)
        return child


@pytest.fixture
def _wired(rig, monkeypatch):
    from levain.agents import controller as ctl, gauges, session_wire as W
    monkeypatch.setattr(W, "LAUNCHERS", {})
    monkeypatch.setattr(gauges, "_CHECKS", {})
    monkeypatch.setattr(ctl, "SESSION_FACTORY", lambda s, o, e: W.fire_factory(s, o, e, clock=rig.now))
    return W


def _repo_for(tmp_path):
    repo, head = _tiny_repo(tmp_path)
    return str(repo), head


def test_prepare_makes_one_launched_occurrence_with_the_pong_role_the_win_override_and_the_install(rig, tmp_path, _wired):
    """Revert: the ``add_role`` call (``fire_factory`` raises InstallRefused), the win override (the spec keeps
    ``compute_anomalies``), or the ``workspaces`` entry (no workspace reaches ``spawn``)."""
    from levain.agents import spec as spec_mod, session_wire as W
    from tests.test_agents_launch import SPECS
    before = json.dumps(SPECS["job_feeder_downstream"], sort_keys=True)
    repo, head = _repo_for(tmp_path)
    W.register_launcher("vendor", lambda store: VendorLauncher("import sys; sys.exit(0)"))
    oid, agent = S6.prepare(rig, repo, tmp_path / "episodes.db", launcher="vendor")
    assert rig.row(oid)["state"] == "launched"
    assert spec_mod.occurrence_spec(rig.store, oid)["win"]["check"] == W.SESSION_WIN_CHECK
    assert json.dumps(SPECS["job_feeder_downstream"], sort_keys=True) == before      # the shared fixture is put back
    kw = W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"], clock=rig.now)
    assert kw["role"]["brief"]["template"] == S6.PONG_BRIEF and kw["role"]["harness"] == "claude"
    assert kw["workspace"].repo == repo and kw["workspace"].head == head
    assert agent == rig.row(oid)["agent_id"]


def test_a_vendor_exit_0_through_the_drivers_own_prepare_is_written_done_and_judged_pass(rig, tmp_path, _wired, monkeypatch, capsys):
    """The whole point of S06 on the host's fake vendor: the occurrence the driver prepares ends ``done``/``completed``
    by the product's own write, and the driver's judge reads it as the one PASS line. Revert the win override and it
    ends ``failed``/``win_unverified``."""
    from levain.agents import controller as ctl, session_wire as W
    from levain.consent import delivery
    from levain.cli import main
    pytest.importorskip("anneal_memory")
    repo, _ = _repo_for(tmp_path)
    launcher = VendorLauncher("import sys; sys.exit(0)")
    W.register_launcher("vendor", lambda store: launcher)
    oid, agent = S6.prepare(rig, repo, tmp_path / "episodes.db", launcher="vendor")
    monkeypatch.setattr(delivery, "process_identity", lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    code = ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now)
    capsys.readouterr()
    assert main(["agents", "--store-dir", str(rig.store.directory), "show", agent, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    raw = S6.judge_show(doc, oid, code, episodes=True)
    assert code == 0 and re.fullmatch(S6.verdict_for(episodes=True).pattern, raw), raw
    assert len(launcher.launches) == 1 and launcher.launches[0].brief.startswith("Reply with exactly the word: pong")


def test_a_vendor_exit_1_through_prepare_is_failed_and_judged_fail(rig, tmp_path, _wired, monkeypatch, capsys):
    from levain.agents import controller as ctl, session_wire as W
    from levain.consent import delivery
    from levain.cli import main
    pytest.importorskip("anneal_memory")
    repo, _ = _repo_for(tmp_path)
    W.register_launcher("vendor", lambda store: VendorLauncher("import sys; sys.exit(1)"))
    oid, agent = S6.prepare(rig, repo, tmp_path / "episodes.db", launcher="vendor")
    monkeypatch.setattr(delivery, "process_identity", lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    code = ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now)
    capsys.readouterr()
    main(["agents", "--store-dir", str(rig.store.directory), "show", agent, "--json"])
    doc = json.loads(capsys.readouterr().out)
    raw = S6.judge_show(doc, oid, code, episodes=True)
    assert code == 1 and re.fullmatch(S6.verdict_for(episodes=True).pattern, raw) is None


def test_the_real_fire_is_a_process_whose_command_line_names_the_occurrence(rig, tmp_path):
    """``FIRE_NO_IDENTITY`` otherwise: the identity check reads the occurrence id off the command line."""
    argv = S6.fire_command(rig.store.directory, "oc-" + "ab" * 16, python=sys.executable)
    assert "oc-" + "ab" * 16 in argv and "_fire" in argv and argv[0] == sys.executable


def test_show_command_is_the_products_own_reader():
    argv = S6.show_command("/s", "ag-1", python="py")
    assert argv == ["py", "-m", "levain", "agents", "--store-dir", "/s", "show", "ag-1", "--json"]


# ======================================================================================================================
# both files: no host-bound literal (they take every path from the profile)
# ======================================================================================================================

def test_no_host_bound_literal_in_the_two_new_files():
    from tests.test_vm_harness_profile import HOST_BOUND, _offenders
    found = {n: _offenders((HARNESS / n).read_text()) for n in ("s06_done_driver.py", "s07_stager_fire.py")}
    assert found == {n: [] for n in found}, found


def test_neither_file_names_a_private_key_path_as_a_source():
    """The guest copy list is built by ``plan_pack`` from two host dirs the CALLER names; no file hard-codes a key dir as
    something to read (the words only appear in the forbidden table)."""
    tree = ast.parse((HARNESS / "s07_stager_fire.py").read_text())
    fn = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("plan_pack", "build_pack"):
        lits = [c.value for c in ast.walk(fn[name]) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        assert not any(".levain-sign" in v or "/keys" in v for v in lits), name
