"""Confinement tests (spore-311) — the OS-sandbox crown-jewels FLOOR for a sovereign entity's bash.

The rescope (spore-303 → spore-311): the sovereign ``levain run`` entity becomes a CC/Codex
replacement — a stateful networked shell on the operator's REAL repos, with the OS sandbox INVERTED
from a workspace jail into a crown-jewels floor (``(allow default)`` → DENY flow store + declared
creds + sibling ``.levain/`` stores + ``~/.ssh/id_*``). These prove it in two layers:

  - the PURE layer (``build_policy`` denylist assembly + ``SeatbeltProvider.render_profile`` SBPL
    text) — runs everywhere, no sandbox needed;
  - the LIVE layer (a real ``sandbox-exec``-confined persistent shell) — gated on Darwin +
    ``sandbox-exec``; these bake the L4-live proofs as regression tests: real tools run under
    default-allow, state persists, and the crown-jewel denies ENFORCE even after the shell's cwd
    wanders into ``$HOME`` (the property an in-process fence cannot give a shell).

The module is a dependency-isolated stdlib leaf (like ``levain.firing.isolation`` /
``levain.daemon``) — importing it pulls NO openhands and NO anneal.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levain.firing.confinement import (
    ConfinementConfig,
    ConfinementError,
    CrownJewelsPolicy,
    SeatbeltProvider,
    _sbpl_regex,
    _sbpl_string,
    _sibling_entity_stores,
    build_policy,
    confinement_supported,
    crown_jewel_reason,
    load_confinement_config,
    sandbox_exec_available,
    select_provider,
)

_LIVE = platform.system() == "Darwin" and sandbox_exec_available()
live = pytest.mark.skipif(not _LIVE, reason="needs macOS sandbox-exec (the SeatbeltProvider backend)")


# --- helpers ------------------------------------------------------------------------


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    """A freshly-init'd entity dir (its ``.levain/`` exists, as after ``levain init``)."""
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    return d


# =============================================================================================
# PURE LAYER — build_policy (denylist assembly)
# =============================================================================================


def test_build_policy_always_denies_the_operator_memory_store(tmp_path: Path, monkeypatch) -> None:
    """The universal floor: the operator-laptop memory store (``~/.anneal-memory/``) is ALWAYS a
    denied subtree — the identity moat in file terms (mirrors ``isolation.flow_store_dir``)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert (tmp_path / ".anneal-memory").resolve() in policy.deny_read_write


def test_build_policy_ssh_agent_mode_confines_the_ssh_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), ssh_mode="agent")
    assert policy.ssh_dir == (tmp_path / ".ssh").resolve()


def test_build_policy_ssh_raw_mode_omits_the_key_deny(tmp_path: Path, monkeypatch) -> None:
    """``ssh_mode="raw"`` is the fallback (allow raw ``~/.ssh`` read) — no ssh confinement."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    assert policy.ssh_dir is None


def test_build_policy_write_denies_jewel_ancestors(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (apparatus L2 CRITICAL): every ancestor dir of a crown jewel is write-denied so
    the jewel can't be relocated by renaming an ancestor. The cred file's parent chain appears in
    ``deny_write_dirs``; the filesystem root does not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    secret = tmp_path / "proj" / "creds" / ".env"
    secret.parent.mkdir(parents=True)
    secret.write_text("K=v")
    policy = build_policy(_entity(tmp_path), deny_files=(secret,))
    assert (tmp_path / "proj" / "creds").resolve() in policy.deny_write_dirs
    assert (tmp_path / "proj").resolve() in policy.deny_write_dirs
    assert Path("/") not in policy.deny_write_dirs


def test_build_policy_does_not_guess_cred_files(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (L4-live 2026-07-11): a generic, operator-neutral module must NOT invent a cred
    path like ``~/.env.flow`` — that is FALSE SECURITY (it "protects" a path the secret isn't at
    while missing the real one). With no ``deny_files`` passed, no credential file is denied."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert policy.deny_files == ()


def test_build_policy_denies_caller_declared_cred_files(tmp_path: Path) -> None:
    secret = tmp_path / "creds" / ".env.flow"
    secret.parent.mkdir()
    secret.write_text("KEY=x")
    policy = build_policy(_entity(tmp_path), deny_files=(secret,))
    assert secret.resolve() in policy.deny_files


def test_build_policy_enumerates_sibling_stores_excluding_own(tmp_path: Path, monkeypatch) -> None:
    """Sibling entities' ``.levain/`` stores are crown jewels; the entity's OWN store is NOT."""
    monkeypatch.setenv("HOME", str(tmp_path))
    me = _entity(tmp_path, "me")
    sib = _entity(tmp_path, "sibling")
    policy = build_policy(me)
    assert (sib / ".levain").resolve() in policy.deny_read_write
    assert (me / ".levain").resolve() not in policy.deny_read_write


def test_build_policy_extra_deny_read_write_pins_subtrees(tmp_path: Path) -> None:
    extra = tmp_path / "secrets_dir"
    extra.mkdir()
    policy = build_policy(_entity(tmp_path), extra_deny_read_write=(extra,))
    assert extra.resolve() in policy.deny_read_write


def test_build_policy_workspace_defaults_under_entity(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    policy = build_policy(ed)
    assert policy.workspace == (ed / "workspace").resolve()


def test_sibling_stores_unreadable_parent_yields_empty(tmp_path: Path) -> None:
    """A parent that can't be listed yields NO siblings (not a raise) — the fixed crown jewels stay
    the non-negotiable floor; sibling isolation is additive/best-effort."""
    ghost = tmp_path / "nope" / "entity"
    # parent (tmp_path/"nope") does not exist → iterdir raises OSError → handled → ()
    assert _sibling_entity_stores(ghost) == ()


# =============================================================================================
# PURE LAYER — render_profile (SBPL text)
# =============================================================================================


def test_render_profile_is_default_allow(tmp_path: Path) -> None:
    """The polarity flip: ``(version 1)`` + ``(allow default)`` — a FLOOR, not a jail."""
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path)))
    assert "(version 1)" in profile
    assert "(allow default)" in profile


def test_render_profile_denies_each_subtree_by_subpath(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    profile = SeatbeltProvider().render_profile(policy)
    store = (tmp_path / ".anneal-memory").resolve()
    assert f'(subpath "{store}")' in profile
    assert "file-read* file-write*" in profile  # denied for BOTH read + write


def test_render_profile_denies_cred_files_by_literal(tmp_path: Path) -> None:
    secret = tmp_path / ".env.flow"
    secret.write_text("KEY=x")
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), deny_files=(secret,)))
    assert f'(literal "{secret.resolve()}")' in profile


def test_render_profile_ssh_agent_denies_subtree_and_reallows(tmp_path: Path, monkeypatch) -> None:
    """Location-based (not name-based): deny the whole ~/.ssh subtree, re-allow known_hosts (r+w)
    and config (r) AFTER the deny (last-match-wins)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = (tmp_path / ".ssh").resolve()
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), ssh_mode="agent"))
    assert f'(deny file-read* file-write* (subpath "{ssh}"))' in profile
    deny_idx = profile.index(f'(subpath "{ssh}")')
    allow_idx = profile.index(f'(allow file-read* file-write* (literal "{ssh / "known_hosts"}"))')
    assert allow_idx > deny_idx  # re-allow must come AFTER the deny to win
    assert f'(allow file-read* (literal "{ssh / "config"}"))' in profile


def test_render_profile_ssh_raw_omits_ssh_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), ssh_mode="raw"))
    assert ".ssh" not in profile


def test_render_profile_emits_ancestor_write_denies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    secret = tmp_path / "proj" / ".env"
    secret.parent.mkdir()
    secret.write_text("K=v")
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), deny_files=(secret,)))
    assert "(deny file-write*" in profile
    assert f'(literal "{(tmp_path / "proj").resolve()}")' in profile


def test_sbpl_string_rejects_control_chars() -> None:
    """A path with a newline could inject profile syntax → FAIL CLOSED (apparatus L2 #6)."""
    with pytest.raises(ConfinementError):
        _sbpl_string("/a/b\nc")


def test_render_profile_ssh_reallow_respects_caller_deny(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (apparatus L3 consensus): the ssh convenience re-allow of ``config`` must NOT
    override a caller's EXPLICIT ``deny_files`` of that same path — everywhere else a caller-declared
    jewel is final."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_config = (tmp_path / ".ssh" / "config")
    profile = SeatbeltProvider().render_profile(
        build_policy(_entity(tmp_path), ssh_mode="agent", deny_files=(ssh_config,))
    )
    # config is a caller crown jewel → its re-allow is suppressed; known_hosts (not denied) still re-allowed.
    assert f'(allow file-read* (literal "{ssh_config.resolve()}"))' not in profile
    assert f'(literal "{(tmp_path / ".ssh" / "known_hosts").resolve()}"))' in profile


def test_sbpl_string_escapes_quotes_and_backslashes() -> None:
    assert _sbpl_string(r'a"b\c') == r'a\"b\\c'


def test_sbpl_regex_escapes_metacharacters() -> None:
    out = _sbpl_regex("/a.b+c")
    assert out == r"\/a\.b\+c"  # every metachar (/, ., +) escaped to match the literal prefix


def test_render_profile_escapes_pathological_paths() -> None:
    """A crafted crown-jewel path containing a quote can't break out of the SBPL string literal."""
    policy = CrownJewelsPolicy(
        entity_dir=Path("/e"),
        workspace=Path("/e/workspace"),
        deny_read_write=(Path('/weird"quote'),),
        deny_files=(),
        deny_write_dirs=(),
        ssh_dir=None,
    )
    profile = SeatbeltProvider().render_profile(policy)
    assert r'\"quote' in profile  # the quote is escaped inside the subpath string


# =============================================================================================
# PURE LAYER — provider selection + availability
# =============================================================================================


def test_select_provider_darwin_is_seatbelt() -> None:
    assert isinstance(select_provider("Darwin"), SeatbeltProvider)


def test_select_provider_other_os_fails_closed_naming_the_seam() -> None:
    with pytest.raises(ConfinementError) as exc:
        select_provider("Linux")
    assert "bwrap" in str(exc.value).lower() or "fail-closed" in str(exc.value).lower()


def test_sandbox_exec_available_is_a_bool() -> None:
    assert isinstance(sandbox_exec_available(), bool)


def test_dependency_isolated_leaf() -> None:
    """Importing the confinement leaf pulls NO openhands + NO anneal (the same discipline as
    ``isolation`` / ``daemon``) — so the confinement core is unit-testable in complete isolation."""
    code = (
        "import levain.firing.confinement as c; import sys; "
        "assert 'openhands' not in sys.modules, 'leaked openhands'; "
        "assert not any(m.startswith('anneal') for m in sys.modules), 'leaked anneal'; "
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# =============================================================================================
# LIVE LAYER — the real sandbox-exec-confined persistent shell (Darwin + sandbox-exec only)
# =============================================================================================


@live
def test_live_real_tool_runs_under_default_allow(tmp_path: Path) -> None:
    """A default-allow profile lets a real tool load its own libs + run (the whole reason for the
    polarity flip — a default-DENY jail could not)."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        r = sh.run("python3 -c 'print(6*7)'", timeout=20)
        assert r.exit_code == 0 and r.timed_out is False
        assert r.output.strip() == "42"


@live
def test_live_state_persists_across_commands(tmp_path: Path) -> None:
    """ONE long-lived shell: an export in one command is visible in the next (real stateful shell,
    not per-command exec)."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        assert sh.run("export FOO=bar", timeout=10).exit_code == 0
        r = sh.run("echo val=$FOO", timeout=10)
        assert r.output.strip() == "val=bar"


@live
def test_live_exit_codes_propagate(tmp_path: Path) -> None:
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        assert sh.run("true", timeout=10).exit_code == 0
        assert sh.run("false", timeout=10).exit_code == 1


@live
def test_live_output_without_trailing_newline(tmp_path: Path) -> None:
    """REGRESSION for the sentinel-substring fix: a command whose output has NO trailing newline
    (``printf`` w/o ``\\n``) once concatenated the sentinel onto the last line and hung the shell.
    Now the sentinel is matched as a substring, so this returns cleanly."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        r = sh.run("printf abc", timeout=10)
        assert r.exit_code == 0 and r.timed_out is False
        assert r.output == "abc"


@live
def test_live_crown_jewel_denied_even_after_cwd_wanders(tmp_path: Path, monkeypatch) -> None:
    """THE moat proof + the load-bearing claim: the operator memory store is refused, AND stays
    refused after the shell ``cd``\\ s into ``$HOME`` — an OS sandbox fences by PATH at the syscall
    level, so a wandering cwd cannot escape it (an in-process fence could not do this)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / ".anneal-memory"
    store.mkdir()
    (store / "memory.db").write_text("SOVEREIGN MEMORY — must never be read by an entity")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        # absolute path from the workspace:
        r1 = sh.run(f"cat {store / 'memory.db'} 2>&1", timeout=10)
        assert r1.exit_code != 0 and "not permitted" in r1.output.lower()
        # cwd wanders to $HOME, then a RELATIVE read of the same denied subtree — still refused:
        r2 = sh.run(f"cd {tmp_path} && cat .anneal-memory/memory.db 2>&1", timeout=10)
        assert r2.exit_code != 0 and "not permitted" in r2.output.lower()
        assert "SOVEREIGN MEMORY" not in (r1.output + r2.output)


@live
def test_live_set_x_does_not_corrupt_protocol(tmp_path: Path) -> None:
    """REGRESSION (apparatus L3 consensus, verified live): ``set -x`` echoes the sentinel ``printf``
    line into the merged stream; with a naive token that trace was read as end-of-command and silently
    corrupted every later result. The SPLIT token makes the trace un-matchable, so the protocol holds:
    exit codes stay correct and later commands run normally even with xtrace on."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        assert sh.run("set -x", timeout=8).exit_code == 0
        r = sh.run("false", timeout=8)          # xtrace ON — exit code must still be the REAL one
        assert r.exit_code == 1
        r2 = sh.run("echo traced_ok", timeout=8)
        assert r2.exit_code == 0 and "traced_ok" in r2.output


@live
def test_live_command_channel_private_from_children(tmp_path: Path) -> None:
    """REGRESSION (apparatus L3 codex round-1+2, verified live): a child must NOT be able to read the
    command channel. Two vectors, both closed: (a) an inherited fd (``/dev/fd/N``) — the FIFO bash
    opens is close-on-exec so children don't inherit it; (b) the FIFO PATH via bash's ``$0`` — the fifo
    is UNLINKED after the startup handshake, so ``open($0)`` hits ENOENT."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        # (a) inherited-fd probe: read any fd 3..9 — must find no readable channel
        fd_probe = (
            "for fd in 3 4 5 6 7 8 9; do "
            "timeout 1 bash -c \"read -u $fd _l 2>/dev/null && echo STOLE\" 2>/dev/null; "
            "done; echo probe_done"
        )
        r = sh.run(fd_probe, timeout=15)
        assert "STOLE" not in r.output and "probe_done" in r.output
        # (b) $0-path probe: bash discloses the script path as $0; opening it must fail (unlinked).
        # If the attack SUCCEEDED, open() returns → exit 0; unlinked → FileNotFoundError → nonzero.
        r0 = sh.run('python3 -c "import sys; open(sys.argv[1])" "$0" 2>&1', timeout=10)
        assert r0.exit_code != 0 and "FileNotFoundError" in r0.output
        # the shell itself is still perfectly usable afterward:
        assert sh.run("echo ok", timeout=8).output.strip() == "ok"


def test_spawn_raises_when_shell_never_reads_the_channel(tmp_path: Path) -> None:
    """REGRESSION (apparatus L3 codex round-2 #2): if the shell process dies / never reads the command
    channel at startup, spawn must FAIL CLOSED — a dead driver must not masquerade as a live shell.
    Hermetic (no sandbox): ``/usr/bin/true`` exits immediately without reading the fifo, so the startup
    handshake gets EOF and raises."""
    from levain.firing.confinement import SandboxedShell
    ws = tmp_path / "ws"
    ws.mkdir()
    shell = SandboxedShell(argv=["/usr/bin/true"], cwd=ws, env={"PATH": "/usr/bin:/bin"})
    with pytest.raises(ConfinementError):
        shell.start()
    shell.close()  # idempotent, no raise


@live
def test_live_concurrent_run_fails_fast(tmp_path: Path) -> None:
    """REGRESSION (apparatus L3 consensus): ``run()`` is single-caller; a concurrent call must fail
    FAST rather than silently corrupt ``_pending`` + output attribution."""
    import threading as _th
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path)))
    started = _th.Event()
    def _slow() -> None:
        started.set()
        sh.run("sleep 2", timeout=6)
    t = _th.Thread(target=_slow)
    t.start()
    try:
        started.wait(2)
        time.sleep(0.3)  # ensure the slow run() has entered + holds the lock
        with pytest.raises(ConfinementError):
            sh.run("echo nope", timeout=4)
    finally:
        t.join(8)
        sh.close()


@live
def test_live_stdin_consuming_child_does_not_hijack(tmp_path: Path) -> None:
    """REGRESSION (apparatus L1 HIGH, verified live): a child that reads stdin (``cat``, a bare
    ``python3`` REPL) once consumed the command channel and silently killed the protocol. With the
    dedicated command channel + ``/dev/null`` stdin, the child gets EOF + exits, and the NEXT command
    runs normally."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        assert sh.run("echo before", timeout=8).output.strip() == "before"
        sh.run("cat", timeout=8)          # reads /dev/null → EOF → exits (does not hijack)
        r = sh.run("echo after", timeout=8)
        assert r.exit_code == 0 and r.output.strip() == "after"
        # a bare interpreter that reads stdin also must not hijack:
        sh.run("python3", timeout=10)
        r2 = sh.run("echo after2", timeout=8)
        assert r2.output.strip() == "after2"


@live
def test_live_ancestor_rename_relocation_blocked(tmp_path: Path) -> None:
    """REGRESSION (apparatus L2 CRITICAL, verified live): under default-allow the entity could ``mv``
    a non-denied ANCESTOR of a crown jewel to move it out from under its deny, then read it. The
    ancestor write-deny blocks the relocation while still allowing normal file creation inside."""
    proj = tmp_path / "proj"
    proj.mkdir()
    secret = proj / "creds.env"
    secret.write_text("SUPABASE=sb_secret_relocate")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(secret,))) as sh:
        assert sh.run(f"cat {secret} 2>&1", timeout=10).exit_code != 0        # direct read denied
        r_mv = sh.run(f"mv {proj} {tmp_path / 'proj2'} 2>&1", timeout=10)     # relocate ancestor
        assert r_mv.exit_code != 0 and "not permitted" in r_mv.output.lower()
        assert not (tmp_path / "proj2").exists()                             # the mv did NOT happen
        # normal file creation INSIDE the write-denied ancestor still works:
        r_create = sh.run(f"echo hi > {proj / 'newfile'} && echo OK 2>&1", timeout=10)
        assert r_create.exit_code == 0 and "OK" in r_create.output
    assert secret.read_text() == "SUPABASE=sb_secret_relocate"  # jewel intact + never relocated


@live
def test_live_cred_file_read_and_write_denied_control_readable(tmp_path: Path) -> None:
    """A declared cred file is denied for BOTH read + write; a normal (non-jewel) file is readable
    (default-allow); the secret is never leaked + stays intact on disk."""
    secret = tmp_path / "creds.env"
    secret.write_text("SUPABASE=sb_secret_do_not_leak")
    ctrl = tmp_path / "ok.txt"
    ctrl.write_text("fine to read")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(secret,))) as sh:
        r_read = sh.run(f"cat {secret} 2>&1", timeout=10)
        assert r_read.exit_code != 0 and "not permitted" in r_read.output.lower()
        r_write = sh.run(f"echo pwned > {secret} 2>&1", timeout=10)
        assert r_write.exit_code != 0
        r_ctrl = sh.run(f"cat {ctrl} 2>&1", timeout=10)
        assert r_ctrl.exit_code == 0 and r_ctrl.output.strip() == "fine to read"
    assert "sb_secret_do_not_leak" not in (r_read.output + r_write.output)
    assert secret.read_text() == "SUPABASE=sb_secret_do_not_leak"  # intact — write never landed


@live
def test_live_ssh_keys_denied_location_based_known_hosts_usable(tmp_path: Path, monkeypatch) -> None:
    """``ssh_mode="agent"`` is LOCATION-based (apparatus L2 #4): ALL of ~/.ssh key material is
    read+write-denied — the id_* keys AND a custom-named ``deploy_key`` — while ``known_hosts`` stays
    read+APPENDable (ssh records new host keys) and ``config`` readable. ``authorized_keys`` can't be
    planted (write-denied), closing the persistence vector (L1 #8)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE ID KEY")
    (ssh / "deploy_key").write_text("PRIVATE DEPLOY KEY")  # custom-named → the name-based hole
    (ssh / "known_hosts").write_text("github.com ssh-ed25519 AAAA\n")
    (ssh / "config").write_text("Host x\n")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        r_id = sh.run(f"cat {ssh / 'id_ed25519'} 2>&1", timeout=10)
        assert r_id.exit_code != 0 and "not permitted" in r_id.output.lower()
        r_dep = sh.run(f"cat {ssh / 'deploy_key'} 2>&1", timeout=10)
        assert r_dep.exit_code != 0 and "not permitted" in r_dep.output.lower()
        r_kh = sh.run(f"cat {ssh / 'known_hosts'} 2>&1", timeout=10)
        assert r_kh.exit_code == 0 and "github.com" in r_kh.output
        r_app = sh.run(f"echo 'newhost' >> {ssh / 'known_hosts'} && echo OK 2>&1", timeout=10)
        assert r_app.exit_code == 0 and "OK" in r_app.output
        r_cfg = sh.run(f"cat {ssh / 'config'} 2>&1", timeout=10)
        assert r_cfg.exit_code == 0 and "Host x" in r_cfg.output
        r_plant = sh.run(f"echo evil > {ssh / 'authorized_keys'} 2>&1", timeout=10)
        assert r_plant.exit_code != 0
    assert "PRIVATE" not in (r_id.output + r_dep.output)


@live
def test_live_non_utf8_output_does_not_brick_the_shell(tmp_path: Path) -> None:
    """REGRESSION (apparatus L3, verified live): a command emitting non-UTF-8 bytes (binary output, a
    latin-1 tool) once crashed the reader thread with a UnicodeDecodeError, bricking the shell for
    every later command. ``errors="replace"`` keeps the shell alive."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        r1 = sh.run(r"printf '\xff\xfe binary'", timeout=8)
        assert r1.timed_out is False  # did not hang / crash the reader
        r2 = sh.run("echo still_alive", timeout=8)
        assert r2.exit_code == 0 and r2.output.strip() == "still_alive"  # shell survived


@live
def test_live_timeout_leaves_result_flagged(tmp_path: Path) -> None:
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        r = sh.run("sleep 5", timeout=1)
        assert r.timed_out is True and r.exit_code is None


@live
def test_live_shell_self_heals_after_timeout(tmp_path: Path) -> None:
    """REGRESSION: a timed-out command's sentinel fires LATE; without pending-sentinel draining, the
    next run() would consume the STALE sentinel and return the wrong result. The next command must
    resync and return ITS OWN output/exit."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path))) as sh:
        assert sh.run("sleep 2 && echo late", timeout=1).timed_out is True
        # the sleep is still running; the next command must not pick up the stale sentinel:
        r = sh.run("echo fresh_result", timeout=8)
        assert r.timed_out is False
        assert r.exit_code == 0
        assert r.output.strip() == "fresh_result"


@live
def test_live_close_reaps_child_processes(tmp_path: Path) -> None:
    """REGRESSION (verified live 2026-07-11): a persistent shell spawns children; a bare terminate()
    of bash alone ORPHANED a timed-out ``sleep`` (reparented to init, still running). close() now
    signals the whole process GROUP, so the child is reaped."""
    dur = "18237"  # a unique sleep duration = the marker for pgrep
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path)))
    assert sh.run(f"sleep {dur}", timeout=1).timed_out is True
    before = subprocess.run(["pgrep", "-f", f"sleep {dur}"], capture_output=True, text=True)
    assert before.stdout.split(), "the sleep child should be running before close()"
    sh.close()
    time.sleep(1.0)
    after = subprocess.run(["pgrep", "-f", f"sleep {dur}"], capture_output=True, text=True)
    leaked = after.stdout.split()
    for pid in leaked:  # defensive cleanup so a failed assert doesn't leave a real leak behind
        try:
            os.kill(int(pid), 9)
        except (ProcessLookupError, ValueError):
            pass
    assert not leaked, "close() must reap child processes (process-group teardown)"


@live
def test_live_close_unlinks_profile_and_is_idempotent(tmp_path: Path) -> None:
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path)))
    profile = sh._profile_path  # type: ignore[attr-defined]
    assert profile.exists()
    sh.close()
    assert not profile.exists()  # the temp seatbelt profile is cleaned up
    sh.close()  # idempotent — no raise


@live
def test_live_run_after_close_refuses(tmp_path: Path) -> None:
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path)))
    sh.close()
    with pytest.raises(ConfinementError):
        sh.run("echo nope")


def test_spawn_shell_fails_closed_without_sandbox(tmp_path: Path, monkeypatch) -> None:
    """If ``sandbox-exec`` is unavailable, ``spawn_shell`` REFUSES rather than fall through to an
    unconfined host shell (the honesty floor). Simulated by forcing availability False."""
    monkeypatch.setattr("levain.firing.confinement.sandbox_exec_available", lambda: False)
    with pytest.raises(ConfinementError):
        SeatbeltProvider().spawn_shell(build_policy(_entity(tmp_path)))


# =============================================================================================
# crown_jewel_reason — the IN-PROCESS twin (for the file-editor hand, not under sandbox-exec)
# =============================================================================================


def test_crown_jewel_reason_denies_the_flow_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert crown_jewel_reason(policy, tmp_path / ".anneal-memory" / "memory.db") is not None
    assert crown_jewel_reason(policy, tmp_path / ".anneal-memory") is not None  # the dir itself


def test_crown_jewel_reason_denies_declared_files_and_ssh_but_allows_broad_reach(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    secret = tmp_path / "app.env"
    secret.write_text("x")
    policy = build_policy(_entity(tmp_path), deny_files=(secret,))
    assert crown_jewel_reason(policy, secret) is not None
    assert crown_jewel_reason(policy, tmp_path / ".ssh" / "id_rsa") is not None  # whole ~/.ssh denied
    # a non-jewel path (a real repo) is ALLOWED — the floor is default-allow-minus-jewels
    assert crown_jewel_reason(policy, tmp_path / "repo" / "main.py") is None


def test_crown_jewel_reason_raw_ssh_mode_does_not_deny_ssh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    # ssh_mode="raw" leaves ssh_dir None → the predicate does not deny ~/.ssh (matches the profile)
    assert crown_jewel_reason(policy, tmp_path / ".ssh" / "id_rsa") is None


def test_crown_jewel_reason_own_store_is_not_a_jewel(tmp_path: Path, monkeypatch) -> None:
    # The entity's OWN .levain/ store is its memory to read/write — NOT a crown jewel.
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    assert crown_jewel_reason(build_policy(ent), ent / ".levain" / "memory.db") is None


def test_crown_jewel_reason_fails_closed_on_an_unresolvable_path(tmp_path: Path) -> None:
    # A NUL-byte path can't be proven safe → denied (fail-closed), not silently allowed.
    assert crown_jewel_reason(build_policy(_entity(tmp_path)), "a\x00b") is not None


# =============================================================================================
# load_confinement_config — the operator-declared half (optional, fail-closed on malformed)
# =============================================================================================


def test_load_confinement_config_missing_file_is_the_default(tmp_path: Path) -> None:
    assert load_confinement_config(_entity(tmp_path)) == ConfinementConfig()


def test_load_confinement_config_parses_and_expands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(
        '{"deny_files": ["~/x.env"], "deny_subtrees": ["~/secrets"], "ssh_mode": "raw"}'
    )
    cfg = load_confinement_config(ent)
    assert cfg.deny_files == (tmp_path / "x.env",)
    assert cfg.deny_subtrees == (tmp_path / "secrets",)
    assert cfg.ssh_mode == "raw"


def test_load_confinement_config_ignores_unknown_keys(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"future_key": 1, "deny_files": []}')
    assert load_confinement_config(ent) == ConfinementConfig()


def test_load_confinement_config_malformed_json_fails_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text("{ not json")
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


def test_load_confinement_config_wrong_types_fail_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_files": "not-a-list"}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


def test_load_confinement_config_bad_ssh_mode_fails_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"ssh_mode": "nope"}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


def test_load_confinement_config_non_object_fails_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('["a", "list"]')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


# =============================================================================================
# confinement_supported + SandboxedShell.closed
# =============================================================================================


def test_confinement_supported_matches_platform() -> None:
    # macOS with sandbox-exec → True; any non-Darwin (no provider) → False.
    assert confinement_supported() == (platform.system() == "Darwin" and sandbox_exec_available())


def test_confinement_supported_false_off_darwin() -> None:
    assert confinement_supported("Linux") is False


@live
def test_sandboxed_shell_closed_reflects_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    shell = SeatbeltProvider().spawn_shell(build_policy(_entity(tmp_path)))
    try:
        assert shell.closed is False
        shell.close()
        assert shell.closed is True
    finally:
        shell.close()


@live
def test_sandboxed_shell_closed_after_a_command_runs_exit(tmp_path: Path, monkeypatch) -> None:
    # A command that ends the shell (`exit`) closes it → `.closed` becomes True (the signal the bash
    # executor uses to respawn a fresh shell for the next command).
    monkeypatch.setenv("HOME", str(tmp_path))
    shell = SeatbeltProvider().spawn_shell(build_policy(_entity(tmp_path)))
    try:
        shell.run("exit 0")
        assert shell.closed is True
    finally:
        shell.close()


# =============================================================================================
# crown_jewel_reason — case + normalization insensitivity (apparatus L2 HIGH regression)
# =============================================================================================


def test_crown_jewel_reason_denies_case_variant_of_a_jewel(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (apparatus L2 HIGH): macOS APFS is case-insensitive and ``Path.resolve()`` does NOT
    fold case, so a case-variant of a crown jewel (``~/.Anneal-Memory``) points at the SAME on-disk
    file the seatbelt denies. A case-SENSITIVE compare let the file editor (not under the sandbox)
    read/corrupt the jewel through a one-character case change. The predicate must match case- (and
    normalization-) insensitively."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    store = tmp_path / ".anneal-memory"
    assert crown_jewel_reason(policy, store / "memory.db") is not None  # canonical
    variant = str(tmp_path) + "/.Anneal-Memory/memory.db"
    assert crown_jewel_reason(policy, variant) is not None  # case-variant — MUST be denied
    ssh_variant = str(tmp_path) + "/.SSH/id_rsa"
    assert crown_jewel_reason(policy, ssh_variant) is not None  # case-variant of ssh key material


def test_crown_jewel_reason_case_insensitivity_does_not_over_match_siblings(
    tmp_path: Path, monkeypatch
) -> None:
    # Separator-anchored: a sibling like ``.anneal-memory-backup`` is NOT wrongly denied.
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert crown_jewel_reason(policy, tmp_path / ".anneal-memory-backup" / "x") is None


# =============================================================================================
# the confinement config is itself floor-protected (apparatus L1 — can't rewrite your own jail)
# =============================================================================================


def test_build_policy_sets_the_config_file(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    policy = build_policy(ed)
    assert policy.config_file == (ed / ".levain" / "confinement.json").resolve()


def test_crown_jewel_reason_denies_the_confinement_config(tmp_path: Path) -> None:
    # The entity cannot read OR write the config that defines its floor (else it could author
    # ssh_mode="raw" / empty deny_files to weaken its NEXT session's operator-declared floor).
    ed = _entity(tmp_path)
    policy = build_policy(ed)
    cfg = ed / ".levain" / "confinement.json"
    assert crown_jewel_reason(policy, cfg) is not None
    # ...but the entity's OWN memory store stays writable (its memory is its own).
    assert crown_jewel_reason(policy, ed / ".levain" / "memory.db") is None


def test_render_profile_denies_the_confinement_config(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    policy = build_policy(ed)
    profile = SeatbeltProvider().render_profile(policy)
    cfg = (ed / ".levain" / "confinement.json").resolve()
    assert f'(deny file-read* file-write* (literal "{cfg}"))' in profile


@live
def test_live_config_write_denied_but_memory_writable(tmp_path: Path, monkeypatch) -> None:
    # The real sandbox: bash can write its own memory dir but NOT the confinement config.
    monkeypatch.setenv("HOME", str(tmp_path))
    ed = _entity(tmp_path)
    cfg = ed / ".levain" / "confinement.json"
    cfg.write_text('{"ssh_mode": "agent"}')
    before = cfg.read_text()
    shell = SeatbeltProvider().spawn_shell(build_policy(ed))
    try:
        shell.run(f"echo hi > '{ed / '.levain' / 'note.txt'}'")
        assert (ed / ".levain" / "note.txt").exists()  # own store writable
        shell.run(f"printf CORRUPT > '{cfg}'")
        assert cfg.read_text() == before  # config write REFUSED — byte-unchanged
    finally:
        shell.close()


# =============================================================================================
# L3 codex findings — symlinked config, _caller_denies case parity
# =============================================================================================


def test_build_policy_fails_closed_on_a_symlinked_config(tmp_path: Path) -> None:
    """apparatus L3 codex HIGH: a SYMLINKED confinement.json would let ``.resolve()`` store the target
    as the denied path, leaving the LEXICAL ``.levain/confinement.json`` (what load reads) unprotected
    — the entity could ``rm`` + recreate it to weaken the next session. Refuse a symlinked config."""
    ed = _entity(tmp_path)
    (ed / "workspace").mkdir()
    target = ed / "workspace" / "floor.json"
    target.write_text('{"ssh_mode": "raw"}')
    (ed / ".levain" / "confinement.json").symlink_to(target)
    with pytest.raises(ConfinementError):
        build_policy(ed)


def test_build_policy_plain_file_config_is_fine(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text("{}")
    policy = build_policy(ed)  # a plain file (lexical == resolved) builds cleanly
    assert policy.config_file == (ed / ".levain" / "confinement.json").resolve()


@live
def test_live_plain_config_cannot_be_unlinked_or_relocated(tmp_path: Path, monkeypatch) -> None:
    # The real sandbox: bash can't `rm` the plain-file config, nor `mv` its .levain parent (rename-
    # denied) — so the entity can't disarm its own floor for the next session.
    monkeypatch.setenv("HOME", str(tmp_path))
    ed = _entity(tmp_path)
    cfg = ed / ".levain" / "confinement.json"
    cfg.write_text("{}")
    from levain.firing.openhands.tools import SandboxedBashExecutor  # openhands leaf, gated by @live
    from openhands.tools.terminal.definition import TerminalAction

    ex = SandboxedBashExecutor(build_policy(ed))
    try:
        ex(TerminalAction(command=f"rm -f '{cfg}'"))
        assert cfg.exists()  # unlink refused
        ex(TerminalAction(command=f"mv '{ed / '.levain'}' '{ed / '.levain.bak'}' 2>&1"))
        assert (ed / ".levain").exists()  # rename refused
    finally:
        ex.close()


def test_caller_denies_is_case_insensitive_for_ssh_convenience_allow(
    tmp_path: Path, monkeypatch
) -> None:
    """apparatus L3 codex MED: an operator deny declared as a case-variant (``~/.SSH/config``) must
    suppress the ssh convenience-allow for canonical ``~/.ssh/config`` — else bash would allow a path
    the operator explicitly denied while the (case-insensitive) file editor denies it (a two-enforcer
    split). ``_caller_denies`` matches case-insensitively, consistently with ``crown_jewel_reason``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    variant = tmp_path / ".SSH" / "config"  # operator declares the deny with variant case
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), deny_files=(variant,)))
    canonical = (tmp_path / ".ssh" / "config").resolve()
    assert f'(allow file-read* (literal "{canonical}"))' not in profile
