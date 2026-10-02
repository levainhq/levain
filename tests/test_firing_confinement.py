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
import shlex
import shutil
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

from levain.firing.confinement import (
    ConfinementConfig,
    ConfinementError,
    ConfinementProvider,
    BwrapProvider,
    diagnose_confinement,
    BWRAP,
    _bwrap_argv,
    bwrap_available,
    CrownJewelsPolicy,
    SandboxedShell,
    SeatbeltProvider,
    _sbpl_regex,
    _sbpl_string,
    _sibling_entity_stores,
    _write_deny_ancestors,
    build_policy,
    confinement_supported,
    crown_jewel_reason,
    load_confinement_config,
    sandbox_exec_available,
    select_provider,
)

_LIVE = platform.system() == "Darwin" and sandbox_exec_available()
live = pytest.mark.skipif(not _LIVE, reason="needs macOS sandbox-exec (the SeatbeltProvider backend)")

# A REAL agent-socket SSH round-trip (slice 3, build-settle #1) needs the sandbox AND a loaded ssh-agent
# AND network AND GitHub reachability — so it is DOUBLE-gated: `@live` plus an explicit opt-in env, so a
# normal `@live` run never hits the network. Enable with `LEVAIN_LIVE_SSH=1 pytest -k live_ssh` on a box
# whose agent has a github-authorized key loaded.
_LIVE_SSH = _LIVE and bool(os.environ.get("SSH_AUTH_SOCK")) and os.environ.get("LEVAIN_LIVE_SSH") == "1"
live_ssh = pytest.mark.skipif(
    not _LIVE_SSH,
    reason="needs LEVAIN_LIVE_SSH=1 + a loaded ssh-agent + network + macOS sandbox-exec",
)


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
    while missing the real one). With no ``deny_files`` passed, no credential file is denied. The
    only entries are the SQLite sidecar names of a declared jewel (the default store), which are
    derived, not guessed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert set(policy.deny_files) == set(policy.sqlite_sidecars)
    assert {p.name for p in policy.sqlite_sidecars} <= {
        ".anneal-memory-wal", ".anneal-memory-shm", ".anneal-memory-journal"}


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


def test_render_profile_ssh_raw_omits_subtree_deny_but_keeps_authk_write_deny(
    tmp_path: Path, monkeypatch
) -> None:
    """``ssh_mode="raw"`` drops the whole-``~/.ssh`` subtree deny + its re-allows (raw reads allowed),
    but the ``authorized_keys`` WRITE-deny stays (the persistence-vector floor is ssh_mode-independent,
    slice 3)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = (tmp_path / ".ssh").resolve()
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), ssh_mode="raw"))
    assert f'(subpath "{ssh}")' not in profile                       # no whole-subtree ssh deny
    assert f'(literal "{ssh / "known_hosts"}"))' not in profile      # no re-allows either
    assert f'(literal "{ssh / "authorized_keys"}")' in profile       # but authk write-deny stays
    assert f'(literal "{ssh / "authorized_keys2"}")' in profile


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
    """⚠ `_sbpl_regex` HAS ZERO PRODUCTION CALLERS, and this test is the only thing exercising it.

    Measured: `grep -rn _sbpl_regex levain/` returns only the definition. Its sibling
    `_sbpl_string` has NINE call sites in `render_profile`. So the pair is asymmetric — one is
    load-bearing and one is reserved — and a green test beside the live ones reads as coverage of
    the SBPL layer when it covers a function no profile has ever been rendered through.

    ⛔ KEPT, NOT DELETED, AND KEPT HONEST INSTEAD. The escaper is correct for a form SBPL
    supports, and throwing away vetted escaping in a security floor to tidy a coverage number is
    the wrong direction of error. What was actually wrong is that nothing said it is unreached.
    ▶ IF YOU ARE ABOUT TO USE IT: it has never been rendered into a real profile or run under
    `sandbox-exec`, so treat it as unproven at the enforcement layer and add a live case with it.
    """
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


def test_select_provider_linux_is_bwrap() -> None:
    """K4c. Was previously the "other OS fails closed" case, with Linux as the stand-in for
    unsupported — so this line changing is the shipping of the Linux provider, not a weakened test.
    The fail-closed case it used to cover now lives in the test below, on an OS that really has no
    provider."""
    assert isinstance(select_provider("Linux"), BwrapProvider)


def test_select_provider_other_os_fails_closed_naming_the_seam() -> None:
    with pytest.raises(ConfinementError) as exc:
        select_provider("Windows")
    msg = str(exc.value).lower()
    assert "fail-closed" in msg
    # It must name what DOES ship, so the operator learns the seam exists rather than reading it as
    # "confinement is unimplemented".
    assert "bwrap" in msg and "sandbox-exec" in msg


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
def test_live_raw_mode_authorized_keys_write_denied_but_read_allowed(
    tmp_path: Path, monkeypatch
) -> None:
    """SLICE 3 (spore-322): ``ssh_mode="raw"`` allows raw ~/.ssh READS (its whole purpose — the private
    key IS readable here) but STILL denies PLANTING an authorized key. The persistence-vector floor is
    ssh_mode-independent. Uses a FAKE HOME under tmp_path so the operator's real ~/.ssh is never
    touched (the raw-mode gap this closes was first confirmed by a bare append that SUCCEEDED)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA original\n")
    (ssh / "id_ed25519").write_text("RAW PRIVATE KEY")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path), ssh_mode="raw")) as sh:
        r_key = sh.run(f"cat {ssh / 'id_ed25519'} 2>&1", timeout=10)
        assert r_key.exit_code == 0 and "RAW PRIVATE KEY" in r_key.output  # raw read allowed
        r_read = sh.run(f"cat {ssh / 'authorized_keys'} 2>&1", timeout=10)
        assert r_read.exit_code == 0 and "original" in r_read.output      # authk read allowed in raw
        r_plant = sh.run(
            f"echo 'ssh-ed25519 AAAA attacker' >> {ssh / 'authorized_keys'} 2>&1", timeout=10
        )
        assert r_plant.exit_code != 0 and "not permitted" in r_plant.output.lower()  # but not WRITE
    assert (ssh / "authorized_keys").read_text() == "ssh-ed25519 AAAA original\n"  # byte-unchanged


@live
def test_live_raw_mode_ssh_dir_relocation_blocked(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (apparatus L3 codex, non-replaceable — verified live): raw-mode must block relocating
    the ~/.ssh dir ANCHOR (``mv ~/.ssh``), else ``mv ~/.ssh ~/.ssh.bak; ln -s ~/evil ~/.ssh`` plants a
    key at the symlink target — NOT the denied literal — and sshd's realpath() honours it. The dir-anchor
    pin (deny_write_dirs) blocks the rename WITHOUT re-jailing raw file access inside ~/.ssh."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("RAW KEY")
    with select_provider().spawn_shell(build_policy(_entity(tmp_path), ssh_mode="raw")) as sh:
        r_mv = sh.run(f"mv {ssh} {tmp_path / '.ssh.bak'} 2>&1; echo rc=$?", timeout=10)
        assert "not permitted" in r_mv.output.lower() and "rc=0" not in r_mv.output  # relocation blocked
        # raw-mode file access inside ~/.ssh is NOT re-jailed:
        r_read = sh.run(f"cat {ssh / 'id_ed25519'} 2>&1", timeout=10)
        assert r_read.exit_code == 0 and "RAW KEY" in r_read.output                 # raw read works
        r_child = sh.run(f"echo host >> {ssh / 'known_hosts'} 2>&1; echo rc=$?", timeout=10)
        assert "rc=0" in r_child.output                                             # child write works
    assert ssh.is_dir()  # ~/.ssh un-relocated


@live_ssh
def test_live_agent_ssh_roundtrip_authenticates_and_denies_raw_keys(tmp_path: Path) -> None:
    """SLICE 3 build-settle #1, the LIVE proof: with ``ssh_mode="agent"`` a real ssh round-trip
    AUTHENTICATES to github.com via the forwarded agent socket while the raw ~/.ssh subtree stays
    read-denied. Uses the operator's REAL ~/.ssh + agent + network (double-gated by LEVAIN_LIVE_SSH),
    so HOME is NOT monkeypatched. Settles that agent-mode is tight AND functional (no allow-set polish
    needed) — the recommended default."""
    with select_provider().spawn_shell(build_policy(_entity(tmp_path), ssh_mode="agent")) as sh:
        r_auth = sh.run(
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1",
            timeout=30,
        )
        assert "successfully authenticated" in r_auth.output  # agent auth reached GitHub
        r_ls = sh.run("ls ~/.ssh 2>&1", timeout=10)
        assert "not permitted" in r_ls.output.lower()         # raw subtree denied (dir stat too)
        r_kh = sh.run("head -c 1 ~/.ssh/known_hosts >/dev/null 2>&1; echo rc=$?", timeout=10)
        assert "rc=0" in r_kh.output                           # known_hosts re-allow works


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
    # ssh_mode="raw" leaves ssh_dir None → the predicate does not deny raw ~/.ssh keys (matches profile)
    assert crown_jewel_reason(policy, tmp_path / ".ssh" / "id_rsa") is None
    # ...but authorized_keys stays denied to the file editor even in raw mode (persistence vector, the
    # deny_write_files carve-out — the file editor never touches ssh files).
    assert crown_jewel_reason(policy, tmp_path / ".ssh" / "authorized_keys") is not None
    assert crown_jewel_reason(policy, tmp_path / ".ssh" / "authorized_keys2") is not None


def test_crown_jewel_reason_own_store_writable_except_memory_files(tmp_path: Path, monkeypatch) -> None:
    # The entity's OWN .levain/ is its working space — NOT a crown jewel — EXCEPT the three store files
    # (memory.continuity.md / crystal.json / db), which are write-protected from the HANDS since spore-359
    # folds the neocortex into the always-loaded frame (only the host-process wrap composes them).
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    policy = build_policy(ent)
    # a scratch file in .levain is still the entity's own to touch
    assert crown_jewel_reason(policy, ent / ".levain" / "scratch.txt") is None
    # the three store files are now write-protected (the poison-the-always-loaded-memory floor)
    for name in ("memory.continuity.md", "memory.crystal.json", "memory.db"):
        assert crown_jewel_reason(policy, ent / ".levain" / name) is not None


def test_crown_jewel_reason_fails_closed_on_an_unresolvable_path(tmp_path: Path) -> None:
    # A NUL-byte path can't be proven safe → denied (fail-closed), not silently allowed.
    assert crown_jewel_reason(build_policy(_entity(tmp_path)), "a\x00b") is not None


# =============================================================================================
# SLICE 3 — floor expansion: authorized_keys write-floor (always) + deny_standard_creds (opt-in)
# =============================================================================================


def test_build_policy_always_write_denies_ssh_exec_vectors(tmp_path: Path, monkeypatch) -> None:
    """All the ssh persistence/exec vectors (authorized_keys(2), config, rc) are in ``deny_write_files``
    in BOTH ssh_modes — the floor is ssh_mode-independent (slice 3, spore-322; config/rc added after
    apparatus L1 caught the ProxyCommand/rc gap)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    for mode in ("agent", "raw"):
        policy = build_policy(ent, ssh_mode=mode)  # type: ignore[arg-type]
        for name in ("authorized_keys", "authorized_keys2", "config", "rc"):
            assert (tmp_path / ".ssh" / name).resolve() in policy.deny_write_files


def test_build_policy_ssh_dir_anchor_pinned_but_not_read_jailed(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (apparatus L3 codex, non-replaceable — verified live): the ssh write-vector files pin
    their dir ANCHOR (~/.ssh IS in deny_write_dirs → can't be renamed/relocated to dodge the literal
    deny, closing the ``mv ~/.ssh``+symlink bypass) WITHOUT re-jailing raw reads (~/.ssh is NOT a
    read+write subtree in raw mode, so file access inside it still works)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    assert (tmp_path / ".ssh").resolve() in policy.deny_write_dirs         # anchor pinned (blocks relocate)
    assert (tmp_path / ".ssh" / "authorized_keys").resolve() in policy.deny_write_files
    assert (tmp_path / ".ssh").resolve() not in policy.deny_read_write     # raw reads NOT jailed


def test_build_policy_pins_lexical_ssh_anchor_even_when_ssh_is_a_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    """REGRESSION (apparatus L3 codex RE-VERIFY, non-replaceable): a PRE-EXISTING ``~/.ssh -> ~/realssh``
    symlink must not let the entity replace the anchor. ``deny_write_files`` uses ``.resolve()`` (content
    protection — it follows the symlink and pins ~/realssh), so the LEXICAL ~/.ssh must be pinned
    SEPARATELY, or ``rm ~/.ssh; ln -s ~/evil ~/.ssh`` re-points the anchor and a key planted at ~/evil
    dodges the resolved-target deny (verified live)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    realssh = tmp_path / "realssh"
    realssh.mkdir()
    (tmp_path / ".ssh").symlink_to(realssh)
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    assert (tmp_path.resolve() / ".ssh") in policy.deny_write_dirs   # LEXICAL anchor pinned (blocks rm/mv)
    assert realssh.resolve() in policy.deny_write_dirs               # resolved target also pinned
    # ...AND the FILES inside it, at BOTH paths (2026-08-21 — the assertion this test stopped one level
    # short of: it set up the exact symlink scenario and only ever looked at ``deny_write_dirs``).
    for name in ("authorized_keys", "authorized_keys2", "config", "rc"):
        assert (tmp_path.resolve() / ".ssh" / name) in policy.deny_write_files
        assert (realssh.resolve() / name) in policy.deny_write_files


def test_build_policy_denies_lexical_vector_when_the_FILE_is_a_preexisting_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    """REGRESSION (Diogenes HIGH, 2026-08-21, verified by repro): a PRE-EXISTING
    ``~/.ssh/authorized_keys -> ~/evil/authorized_keys`` symlink defeated the write-floor entirely.

    ``deny_write_files`` was built with ``.resolve()`` ONLY, so the deny named ~/evil/authorized_keys —
    while the entity writes the LEXICAL ~/.ssh/authorized_keys, which sshd honours via ``realpath()``.
    ⛔ The (2) dir-anchor pin does NOT cover it: pinning the ~/.ssh LITERAL blocks rename/replace but
    deliberately still allows "file creation/reads INSIDE it", which is exactly the write used here.
    The fix denies BOTH paths for every vector.

    ⛔⛔ **THE MECHANISM NAMED ABOVE IS REFUTED. THE FIX IS CORRECT; ITS STATED REASON WAS NOT.**
    (Diogenes levain-HIGH 2026-08-22, measured live with sandbox-exec on Darwin 25.5; independently
    re-measured 2026-08-23 with a minimal profile and both controls.)

    "the entity writes the LEXICAL path" DOES NOT HAPPEN — seatbelt CANONICALISES a requested path
    before matching ``(literal ...)``. Against a minimal ``(version 1)(allow default)(deny
    file-write* (literal <RESOLVED>))``, appending at the lexical path returns **"Operation not
    permitted"** and the real file is untouched. A converse profile denying only the LEXICAL path
    lets the same append through at exit 0, which is what proves canonicalisation rather than luck.

    ▶ **THE ACTUAL BYPASS IS UNLINK-THEN-RECREATE.** ``unlink``/``rename``/``ln`` operate on the
    LINK, and seatbelt matches THOSE lexically. Re-measured: under a profile denying the resolved
    path, ``rm -f <lexical>`` SUCCEEDS and the link is gone — after which a fresh file at that name
    is a new object nothing denies, and sshd reads it.

    ⚠ **WHY THIS IS FILED AS HIGH AGAINST A CORRECT FIX:** denying both spellings closes
    unlink-then-recreate too, so the CODE is right and stays. But the wrong model was written into
    a comment, this docstring and a commit message, and then generalised into a design rule — and a
    threat model is reused far more often than the line it justifies. The next guard reasoned from
    "seatbelt matches writes lexically" would be built on a measurement that says the opposite.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    evil = tmp_path / "evil"
    evil.mkdir()
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (evil / "authorized_keys").write_text("ssh-ed25519 AAAA attacker\n")
    (ssh / "authorized_keys").symlink_to(evil / "authorized_keys")

    policy = build_policy(_entity(tmp_path), ssh_mode="raw")

    lexical = tmp_path.resolve() / ".ssh" / "authorized_keys"
    resolved = (evil / "authorized_keys").resolve()
    assert lexical != resolved, "test is inert unless the two paths actually diverge"
    assert lexical in policy.deny_write_files    # THE FIX: the path the entity writes / sshd reads
    assert resolved in policy.deny_write_files   # content protection, unchanged

    # ⚠ NON-DISCRIMINATING, AND LABELLED AS SUCH (codex L3 2026-08-21, which RECONSTRUCTED the
    # predecessor policy and confirmed this line passes WITH the bug). `crown_jewel_reason` resolves
    # its argument FIRST, so it matches the already-denied TARGET whether or not the lexical entry
    # exists. It is kept because both-hands-agree is worth stating, NOT as evidence of the fix.
    # ⛔ THE DISCRIMINATING ASSERTIONS ARE `lexical in policy.deny_write_files` above and the profile
    # literal below — both mutation-checked red against the one-line `.resolve()`-only construction.
    assert crown_jewel_reason(policy, lexical) is not None

    # And the seatbelt hand renders the lexical literal, not only the target.
    profile = SeatbeltProvider().render_profile(policy)  # type: ignore[arg-type]
    assert f'(literal "{lexical}")' in profile


def test_build_policy_denies_the_RAW_home_spelling_when_HOME_is_a_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    """REGRESSION (apparatus L3 codex, 2026-08-21): the first cut of the lexical fix used ONLY
    ``home.resolve() / ".ssh" / n``, matching ``ssh_anchor``'s convention — but that convention is
    itself incomplete. With HOME a symlink (``/tmp/linkhome -> /tmp/realhome``) BOTH of the two
    original spellings collapse onto realhome, so the path the entity actually writes and sshd
    actually reads — ``linkhome/.ssh/authorized_keys`` — was named by nothing.

    ⚠ NOT THEORETICAL ON macOS: codex observed ``/var/folders/... -> /private/var/folders/...``
    locally, i.e. ``Path.home() != Path.home().resolve()`` on an ordinary box.
    ⛔ IT DOES NOT NARROW ANYTHING, AND THIS DOCSTRING USED TO SAY IT DID (Diogenes levain-MEDIUM,
    live-measured with controls on Darwin 25.5). The RAW spelling differs from the other two ONLY
    when an ancestor component is a symlink, and seatbelt CANONICALISES ancestor components for
    every operation — a profile whose only rule names the un-canonicalised spelling allows append,
    `rm -f` and `mv` (all exit 0), while the control naming the canonical spelling denies all
    three. On the in-process hand `crown_jewel_reason` resolves first, so a lexical entry can
    never match. The entry is unreachable on BOTH hands.

    ⚠ THIS TEST IS THEREFORE NOT EVIDENCE THAT A VECTOR IS CLOSED — it asserts tuple membership
    in `policy.deny_write_files` and never renders a profile or runs the sandbox, so a
    permanently-unreachable deny reads to it as a live mitigation. That is the same shape this
    commit's predecessor was written to indict ("SET UP THIS EXACT SCENARIO AND LOOKED ONLY AT
    deny_write_dirs"), reproduced one layer over in the fix for it.

    ▶ IT IS KEPT, and so is the line it pins, as DEFENCE-IN-DEPTH against a canonicalisation
    change — seatbelt is Apple-deprecated and this behaviour is undocumented. The *replaceable*
    HOME symlink remains KNOWN-OPEN in build_policy's own block and is not addressed by this
    entry."""
    real = tmp_path / "realhome"
    (real / ".ssh").mkdir(parents=True)
    link = tmp_path / "linkhome"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))

    policy = build_policy(_entity(real), ssh_mode="raw")

    denied = set(policy.deny_write_files)
    assert (link / ".ssh" / "authorized_keys") in denied, (
        "the RAW Path.home() spelling must be PRESENT in the denylist as defence-in-depth. "
        "⚠ Presence is all this asserts: the entry is unreachable on both enforcing hands "
        "today (seatbelt canonicalises ancestors; crown_jewel_reason resolves first), so do "
        "not read this as proof the symlinked-HOME vector is closed — it is not.")
    assert (real.resolve() / ".ssh" / "authorized_keys") in denied  # resolved target still denied
    assert link.resolve() != link, "test is inert unless HOME actually diverges from its target"


def test_build_policy_denies_the_RESOLVED_HOME_LEXICAL_spelling(
    tmp_path: Path, monkeypatch
) -> None:
    """⛔ THE SPELLING THAT CLOSES THE VULNERABILITY WAS THE ONE SPELLING NO TEST COVERED, AND
    DELETING IT LEFT THE SUITE FULLY GREEN (Diogenes levain-MEDIUM 2026-08-22, confirmed by
    mutation 2026-08-23: removing ``ssh_home_lexical / n`` from ``build_policy`` gave 91 passed,
    0 failed — while removing the RAW spelling beside it failed a dedicated regression test).
    Coverage was exactly INVERTED against security value.

    ⚠ THE REASON IT LOOKED COVERED: in the ordinary symlinked-HOME case all three spellings
    collapse. ``Path("link/.ssh/ak").resolve()`` resolves its existing prefix even when the leaf
    does not exist, so the RESOLVED spelling already yields ``real/.ssh/ak`` and hides the gap.
    This spelling is uniquely load-bearing in exactly ONE geometry — **HOME diverges AND the
    vector file is itself a pre-existing symlink** — which is the attack ``build_policy``'s own
    comment (3) describes:

        link/.ssh/authorized_keys  ->  evil/authorized_keys      and  link -> real

        RAW      ssh_home / n              -> link/.ssh/authorized_keys
        LEXICAL  ssh_home_lexical / n      -> real/.ssh/authorized_keys   <- ONLY this one
        RESOLVED (ssh_home / n).resolve()  -> evil/authorized_keys

    ⛔ THE PARAGRAPH THAT STOOD HERE WAS WRONG ABOUT WHICH ENTRY CATCHES WHAT, AND THE ERROR WAS A
    CONFLATION OF TWO OPERATION CLASSES (Diogenes levain-MEDIUM). It said "both enforcing hands
    resolve the requested path before matching, so a write aimed at the lexical file lands on
    ``real/.ssh/authorized_keys``: named by the LEXICAL entry and by neither of the others."
    MEASURED: a write to any of these spellings resolves through BOTH symlinks — HOME and the
    final component — and lands on ``evil/authorized_keys``, i.e. the RESOLVED entry. Not the
    lexical one. For a DATA WRITE the lexical entry is not what matches.

    ▶ THE LEXICAL ENTRY IS STILL LOAD-BEARING, FOR A DIFFERENT OPERATION. `unlink` and `rename`
    do NOT follow the final component, so seatbelt matches them on the canonical-directory +
    lexical-final-component spelling — which is exactly this entry, and exactly what stops the
    live `rm ~/.ssh/authorized_keys; recreate` bypass (measured: with this entry removed, rm exit
    0, create exit 0, and the attacker key is readable). So:

        DATA WRITE to the vector      -> resolves to evil/  -> caught by the RESOLVED entry
        UNLINK / RENAME of the vector -> not resolved       -> caught by the LEXICAL entry

    Both are needed, for different verbs. Saying one of them catches everything is what made the
    other look redundant."""
    real = tmp_path / "realhome"
    (real / ".ssh").mkdir(parents=True)
    evil = tmp_path / "evil"
    evil.mkdir()
    # PRE-EXISTING vector symlink — the shape comment (3) of build_policy describes.
    (real / ".ssh" / "authorized_keys").symlink_to(evil / "authorized_keys")
    link = tmp_path / "linkhome"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))

    policy = build_policy(_entity(real), ssh_mode="raw")
    denied = set(policy.deny_write_files)

    assert link.resolve() != link, "test is inert unless HOME actually diverges from its target"
    assert (real / ".ssh" / "authorized_keys").resolve() != (real / ".ssh" / "authorized_keys"), (
        "test is inert unless the vector file is really a symlink")
    assert (real / ".ssh" / "authorized_keys") in denied, (
        "the resolved-HOME LEXICAL spelling must be denied — it is the path a resolve-first hand "
        "lands on, and neither the raw nor the fully-resolved entry names it")


def test_build_policy_ssh_vectors_do_not_double_up_without_symlinks(
    tmp_path: Path, monkeypatch
) -> None:
    """CONTROL for the fix above: with no symlink anywhere, lexical == resolved, so ``_dedup`` must
    collapse the pair and the ordinary case gets exactly ONE entry per vector — not two."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    for name in ("authorized_keys", "authorized_keys2", "config", "rc"):
        hits = [p for p in policy.deny_write_files if p.name == name]
        assert len(hits) == 1, f"{name} duplicated in the no-symlink case: {hits}"
    # ⚠ SCOPED TO THE SSH VECTORS, not to the whole list (2026-09-04). This read
    # `len(policy.deny_write_files) == 4` and broke when spore-725 put the container-daemon
    # sockets through the same write-deny machinery. The assertion's subject was the ENTIRE
    # list; the claim its name makes is about the SSH vectors — so it failed the moment the
    # list gained a second, unrelated population, while the property it exists to protect
    # (lexical == resolved collapses to one entry per vector) was never in question. Counting
    # only what the test is named for keeps it exact under a third population too.
    ssh_names = {"authorized_keys", "authorized_keys2", "config", "rc"}
    assert len([p for p in policy.deny_write_files if p.name in ssh_names]) == 4


def test_build_policy_deny_standard_creds_expands_known_locations(tmp_path: Path, monkeypatch) -> None:
    """The opt-in folds gh (subtree) + aws credentials + netrc (files) into the floor."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), deny_standard_creds=True)
    assert (tmp_path / ".config" / "gh").resolve() in policy.deny_read_write
    assert (tmp_path / ".aws" / "credentials").resolve() in policy.deny_files
    assert (tmp_path / ".netrc").resolve() in policy.deny_files


def test_build_policy_default_does_not_deny_standard_creds(tmp_path: Path, monkeypatch) -> None:
    """Default OFF (operational-fit): the entity keeps its own gh/aws/curl hands unless opted in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert (tmp_path / ".config" / "gh").resolve() not in policy.deny_read_write
    assert (tmp_path / ".aws" / "credentials").resolve() not in policy.deny_files


_VECTOR_MARKER = ";; ssh persistence/exec vectors"


def test_render_profile_ssh_exec_vector_block_is_write_only(tmp_path: Path, monkeypatch) -> None:
    """Both ssh_modes render the ssh exec vectors (authk*, config, rc) in a WRITE-ONLY deny block — read
    stays allowed on the seatbelt hand so raw-mode ~/.ssh reads work. Pins the block's polarity by its
    header (complement L3: a bare ``"(deny file-write*" in profile`` was ALSO satisfied by the unrelated
    ancestor-write-deny block, so it couldn't catch a regression to read+write on the vector block)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = (tmp_path / ".ssh").resolve()
    ent = _entity(tmp_path)
    for mode in ("agent", "raw"):
        profile = SeatbeltProvider().render_profile(build_policy(ent, ssh_mode=mode))  # type: ignore[arg-type]
        for name in ("authorized_keys", "authorized_keys2", "config", "rc"):
            assert f'(literal "{ssh / name}")' in profile
        assert _VECTOR_MARKER in profile
        after = profile[profile.index(_VECTOR_MARKER):]
        deny_line = next(ln for ln in after.splitlines() if ln.startswith("(deny "))
        assert deny_line == "(deny file-write*"   # write-ONLY, not "(deny file-read* file-write*"


def test_render_profile_exec_vector_deny_after_ssh_reallows(tmp_path: Path, monkeypatch) -> None:
    """ORDERING GUARD (apparatus L1): the exec-vector write-deny must render AFTER the ssh re-allows so
    SBPL last-match-wins keeps it denied. Locks the invariant a future re-allow reorder could silently
    break (currently non-load-bearing only because no re-allow touches these files)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = (tmp_path / ".ssh").resolve()
    profile = SeatbeltProvider().render_profile(build_policy(_entity(tmp_path), ssh_mode="agent"))
    reallow_idx = profile.index(f'(allow file-read* file-write* (literal "{ssh / "known_hosts"}"))')
    assert profile.index(_VECTOR_MARKER) > reallow_idx


def test_render_profile_deny_standard_creds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = SeatbeltProvider().render_profile(
        build_policy(_entity(tmp_path), deny_standard_creds=True)
    )
    assert f'(subpath "{(tmp_path / ".config" / "gh").resolve()}")' in profile
    assert f'(literal "{(tmp_path / ".aws" / "credentials").resolve()}")' in profile
    assert f'(literal "{(tmp_path / ".netrc").resolve()}")' in profile


def test_crown_jewel_reason_denies_authorized_keys_both_modes(tmp_path: Path, monkeypatch) -> None:
    """The file-editor twin denies authorized_keys(2) in BOTH ssh_modes (write-protected vector; the
    file editor never touches ssh files) — no two-enforcer split on the persistence vector."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    for mode in ("agent", "raw"):
        policy = build_policy(ent, ssh_mode=mode)  # type: ignore[arg-type]
        assert crown_jewel_reason(policy, tmp_path / ".ssh" / "authorized_keys") is not None
        assert crown_jewel_reason(policy, tmp_path / ".ssh" / "authorized_keys2") is not None


def test_crown_jewel_reason_deny_standard_creds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    on = build_policy(ent, deny_standard_creds=True)
    assert crown_jewel_reason(on, tmp_path / ".config" / "gh" / "hosts.yml") is not None
    assert crown_jewel_reason(on, tmp_path / ".aws" / "credentials") is not None
    assert crown_jewel_reason(on, tmp_path / ".netrc") is not None
    # default OFF → the same paths are reachable (the entity's own gh/aws hands work)
    off = build_policy(ent)
    assert crown_jewel_reason(off, tmp_path / ".config" / "gh" / "hosts.yml") is None
    assert crown_jewel_reason(off, tmp_path / ".netrc") is None


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
    # ABSENT is now its OWN state (K4a): None means "derive from the drive mode", which is what
    # lets an unattended seat default to denied while an explicit `false` stays an operator
    # opt-IN. Collapsing absent into False here is precisely what made that impossible.
    assert cfg.deny_standard_creds is None  # absent → derive from the drive


def test_load_confinement_config_parses_deny_standard_creds(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_standard_creds": true}')
    assert load_confinement_config(ent).deny_standard_creds is True


def test_load_confinement_config_bad_deny_standard_creds_fails_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    # a JSON number is not a bool → fail-closed (an ambiguous cred-floor flag must not mis-parse).
    (ent / ".levain" / "confinement.json").write_text('{"deny_standard_creds": 1}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


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


# ⚡ THESE THREE WERE macOS-ASSUMING AND FAILED THE FIRST TIME THE SUITE WAS RUN ON LINUX — the
# same "one platform, hardcoded" defect K4c fixed in `confinement_supported` itself, living in the
# tests that were supposed to guard it. They are now platform-gated and each has a Linux twin, so
# the suite states the truth on BOTH hosts instead of encoding one of them as the definition.
_darwin_only = pytest.mark.skipif(platform.system() != "Darwin", reason="states a fact about macOS")
_linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="states a fact about Linux")


@_darwin_only
def test_confinement_supported_matches_platform() -> None:
    # On macOS the floor is available iff the seatbelt driver is.
    assert confinement_supported() == sandbox_exec_available()


@_linux_only
def test_confinement_supported_matches_platform_on_linux() -> None:
    """The twin, and the whole point of the K4c seam fix: on Linux the answer tracks BWRAP's
    availability, not sandbox-exec's. Before that fix this was False on every Linux host, including
    ones where the floor genuinely works."""
    assert confinement_supported() == bwrap_available()


@_darwin_only
def test_confinement_supported_false_off_darwin() -> None:
    """⚠ THE ASSERTION IS UNCHANGED AND ITS REASON IS NOT. Before K4c this was False because no
    Linux PROVIDER existed. Now :class:`BwrapProvider` exists and is selected, and this is False
    because that provider's driver — ``/usr/bin/bwrap`` — is not on a Mac. A test whose meaning
    silently moves under it is worth a sentence; the delegation itself is pinned by the two
    stub-provider tests below."""
    assert confinement_supported("Linux") is False


# --- the driver check must travel with the PROVIDER, not be hardcoded to seatbelt (K4c) ------
# These two pin the delegation in BOTH directions. Without them the pre-K4c shape — select
# polymorphically, then call sandbox_exec_available() unconditionally — passes every test above
# while being wrong the moment a second provider exists: on Linux a working bwrap floor is never
# offered (macOS driver absent), and on a Mac asked about Linux it answers True from a host that
# cannot know. A single-provider suite cannot see that; a stub provider can.


class _StubProvider(ConfinementProvider):
    """Minimal concrete provider whose ``available()`` is the only thing under test."""

    def __init__(self, available: bool) -> None:
        self._available = available

    def available(self) -> bool:
        return self._available

    def render_profile(self, policy) -> str:  # pragma: no cover - not exercised here
        return ""

    def _spawn_shell_impl(self, policy, *, env=None, default_timeout: float = 120.0):  # pragma: no cover
        raise NotImplementedError


def test_confinement_supported_uses_the_providers_driver_check_not_seatbelts(monkeypatch) -> None:
    """A provider that reports its driver ABSENT makes the floor unsupported — even on a Mac where
    ``sandbox_exec_available()`` is True. Pins that the seatbelt driver is not consulted for a
    provider that does not use it."""
    monkeypatch.setattr(
        "levain.firing.confinement.select_provider", lambda system=None: _StubProvider(False)
    )
    monkeypatch.setattr("levain.firing.confinement.sandbox_exec_available", lambda: True)
    assert confinement_supported() is False


def test_confinement_supported_true_when_provider_available_without_seatbelt(monkeypatch) -> None:
    """The other direction, which is the one that would silently drop bash on a real Linux box: a
    provider reporting its OWN driver present is supported even with no ``sandbox-exec`` anywhere."""
    monkeypatch.setattr(
        "levain.firing.confinement.select_provider", lambda system=None: _StubProvider(True)
    )
    monkeypatch.setattr("levain.firing.confinement.sandbox_exec_available", lambda: False)
    assert confinement_supported() is True


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
    # ...and since spore-359 the entity's own MEMORY store files are write-protected too (only the
    # host-process wrap composes them); a non-store .levain file stays the entity's own.
    assert crown_jewel_reason(policy, ed / ".levain" / "memory.db") is not None
    assert crown_jewel_reason(policy, ed / ".levain" / "scratch.txt") is None


def test_render_profile_denies_the_confinement_config(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    policy = build_policy(ed)
    profile = SeatbeltProvider().render_profile(policy)
    cfg = (ed / ".levain" / "confinement.json").resolve()
    assert f'(deny file-read* file-write* (literal "{cfg}"))' in profile


def test_build_policy_write_denies_own_memory_store(tmp_path: Path, monkeypatch) -> None:
    """spore-359 floor: the entity's own continuity/crystal/episodic files are in ``own_memory_files``
    (write-denied to the hands) so a self-write can't poison the always-loaded memory the wrap alone
    composes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    assert {p.name for p in policy.own_memory_files} == {
        "memory.continuity.md", "memory.crystal.json", "memory.db",
        "memory.db-wal", "memory.db-shm", "memory.db-journal",  # WAL sidecars (L3 codex)
    }
    levain_dir = (tmp_path / "coyote" / ".levain").resolve()
    assert all(p.parent == levain_dir for p in policy.own_memory_files)
    # the WAL sidecar is denied too: in WAL mode committed frames live in memory.db-wal, so denying
    # only the main file would leave the episodic source corruptible/poisonable through it (L3 codex).
    assert crown_jewel_reason(policy, tmp_path / "coyote" / ".levain" / "memory.db-wal") is not None


def test_render_profile_write_denies_own_memory_but_allows_read(tmp_path: Path, monkeypatch) -> None:
    """The store files render as WRITE-only denies (bash may still ``cat`` its own memory) — NOT the
    read+write deny the config gets. Read stays allowed so spore-359's build-time read + the entity's
    own inspection work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path))
    profile = SeatbeltProvider().render_profile(policy)
    cont = next(p for p in policy.own_memory_files if p.name == "memory.continuity.md")
    assert f'(literal "{cont}")' in profile                                    # denied…
    assert f'(deny file-read* file-write* (literal "{cont}"))' not in profile  # …but NOT read-denied


def test_build_policy_fails_closed_on_symlinked_own_memory_file(tmp_path: Path, monkeypatch) -> None:
    """A symlinked store file is anomalous (anneal writes plain files) and an `rm`+recreate swap vector
    — refuse to grant hands rather than deny a resolved target while the lexical path stays writable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    (ent / ".levain" / "memory.continuity.md").symlink_to(tmp_path / "elsewhere.md")
    with pytest.raises(ConfinementError, match="symlink"):
        build_policy(ent)


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
    # ⛔ @live DOES NOT GATE THIS IMPORT, WHICH IS WHAT THE OLD COMMENT CLAIMED.
    # `live` is `skipif(not _LIVE)` where `_LIVE = platform.system() == "Darwin" and
    # sandbox_exec_available()` — it gates on the macOS SANDBOX and says nothing about the
    # openhands extra. So on any Darwin box WITH sandbox-exec and WITHOUT `levain[firing]`
    # this test RAN and died on the next line with ModuleNotFoundError. A comment naming a
    # gate that does not gate the thing it is attached to (Diogenes 2026-08-07, HIGH-adjacent
    # MEDIUM, open 27 days).
    # ⚠ WHY IT IS NOT COSMETIC: this is the confinement suite — the file whose subject is an
    # entity being unable to disarm its own floor — and a permanent red there is
    # indistinguishable from a real regression. It also makes "the suite is green"
    # unavailable as evidence for any fix in this repo.
    pytest.importorskip("openhands.tools.terminal", reason="openhands extra not installed")
    from levain.firing.openhands.tools import SandboxedBashExecutor
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


def test_every_production_caller_of_build_policy_declares_the_cred_floor() -> None:
    """SOURCE-LEVEL invariant: no shipped code may call ``build_policy`` without saying what the
    SECURITY floors are — both ``deny_standard_creds`` AND ``deny_localhost_outbound``.

    Both default to ``False`` because this function is the MECHANISM and the POLICY lives one layer
    up (``resolve_cred_floor`` for creds; ``policy_for_conv_state`` for the localhost deny). That
    split is right, but it leaves a real hole (glm L3 for creds; codex L3 HIGH#1 2026-09-13 for the
    localhost deny): a future production path that builds a floor directly and forgets an argument
    would silently ship the PERMISSIVE default — allowing ~/.config/gh on an unattended seat, or
    leaving the spore-755 self-sshd bypass open. Rather than make the mechanism opinionated
    (duplicating policy into two places that can disagree) or churn 70+ test call sites that
    legitimately do not care, pin the thing that matters: PRODUCTION callers decide explicitly.
    Tests may omit it; shipped code may not.
    """
    import ast
    from pathlib import Path

    # AST, not a substring/paren scan (codex L3 MED, fix-pass): the old regex passed a call that
    # merely MENTIONED the arg name in a comment, string, or a DIFFERENT argument — e.g.
    # build_policy(entity, extra_deny_read_write=(Path("deny_localhost_outbound"),)) — while runtime
    # used the permissive default. We now require the names as actual top-level keyword arguments of
    # the real build_policy call node. A **kwargs unpack (kw.arg is None) does NOT count as declaring
    # them — production must pass them explicitly.
    required = ("deny_standard_creds", "deny_localhost_outbound")
    pkg = Path(__file__).resolve().parent.parent / "levain"
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        if "templates" in path.parts:      # shipped template scripts, not the confinement core
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name != "build_policy":
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
            missing = [a for a in required if a not in kwargs]
            if missing:
                offenders.append(f"{path.relative_to(pkg.parent)}:{node.lineno} (missing {'+'.join(missing)})")
    assert not offenders, (
        "production call(s) to build_policy() omit a security floor argument — it would silently "
        "default to PERMISSIVE (deny_standard_creds → ~/.config/gh readable; deny_localhost_outbound "
        "→ the spore-755 self-sshd bypass open). Declare both explicitly "
        "(resolve_cred_floor(...) for the creds; `not cfg.allow_localhost_outbound` for the deny): "
        + ", ".join(offenders)
    )


def test_the_ssh_anchor_is_redundant_because_the_ancestor_walk_already_names_it(tmp_path):
    """The `ssh_anchor` line in `build_policy` is a BELT, not the only strap — and this pins it.

    ⛔ WHAT THIS REPLACES. A 2026-08-22 finding said the anchor was redundant; a 2026-08-23
    rebuttal in the source rejected it, claiming the three spellings "deny lexical FILES inside
    ~/.ssh and never the lexical DIRECTORY", so replacing the directory is "named by this anchor
    and by nothing else." The rebuttal was FALSE, and `confinement.py` already contradicted it
    thirty lines earlier ("the files are added to ``all_jewels`` below, so ~/.ssh (their ancestor
    DIR) is write-denied"). Two comments, one file, opposite answers — and the wrong one was the
    one that closed a finding. A wrong rebuttal outranks a wrong finding because it tells the next
    reader the question is settled.

    ⚡ WHY A TEST RATHER THAN A BETTER SENTENCE. The redundancy is a property of
    `_write_deny_ancestors` NOT resolving. If a future edit teaches it to resolve, the anchor stops
    being redundant and becomes load-bearing again — and that is exactly the day someone needs to
    be told, rather than the day they read a comment written in 2026 and believe it.

    The two spellings below are the ones `build_policy` actually appends, and the second is
    `ssh_anchor` itself, so this holds whether or not HOME is a symlink.
    """
    # ⛔⛔ THE SYMLINK MUST BE REAL, ON DISK. The first cut of this test used two paths that were
    # never created, and `Path.resolve()` on a NON-EXISTENT path is a no-op — so the mutation this
    # test exists to catch (teaching `_write_deny_ancestors` to resolve) left it GREEN. A guard that
    # passes without exercising the thing it is named for, inside the test written to close an
    # instance of exactly that. Mutation-checked after the fix, not before.
    real_home = tmp_path / "real"
    (real_home / ".ssh").mkdir(parents=True)
    home_lexical = tmp_path / "home"
    home_lexical.symlink_to(real_home)          # HOME itself is a symlink — the rebuttal's own case
    assert home_lexical.resolve() == real_home  # the premise of this test, asserted not assumed

    for n in ("authorized_keys", "config"):
        (real_home / ".ssh" / n).write_text("x")
        # The two LEXICAL spellings `build_policy` actually appends (the third, fully-resolved one
        # is not at issue): raw `Path.home()`, and `home.resolve()` + un-deref'd `.ssh` — the
        # second of which IS `ssh_anchor`.
        jewels = [home_lexical / ".ssh" / n, home_lexical.resolve() / ".ssh" / n]
        ancestors = _write_deny_ancestors(jewels)

        assert home_lexical / ".ssh" in ancestors, (
            "the RAW Path.home() spelling's parent must already be write-denied"
        )
        assert real_home / ".ssh" in ancestors, (
            "the resolved-HOME spelling's parent — which IS `ssh_anchor` — must already be "
            "write-denied by the ancestor walk, so the anchor line adds nothing. If this FAILS, "
            "`_write_deny_ancestors` has started resolving: the anchor is load-bearing again and "
            "its justification must be restored rather than this test deleted."
        )


# --- container/VM daemon sockets (spore-725) ------------------------------------------
#
# ⛔ THE DEFECT THESE PIN IS NOT "THE SOCKET WAS NOT DENIED" — it is that the OBVIOUS deny does
# not work. Adding the socket paths to `deny_files` (the shape the spore originally proposed)
# emits correct-looking literals into the profile, reads clean in review, and leaves the exploit
# working, because a seatbelt `file-read* file-write*` rule does not block `connect()`. Every
# test below exists because a version of the fix WITHOUT it was measured and lost.


def _sock_policy(tmp_path: Path, monkeypatch, sock: Path, **kw):
    """Build a real policy whose container-socket list is `sock`, so the live arms can be
    exercised against a socket the test owns rather than the operator's docker daemon."""
    from levain.firing import confinement as _conf
    monkeypatch.setattr(_conf, "_CONTAINER_DAEMON_SOCKETS", (str(sock),))
    return build_policy(tmp_path / "ent", **kw)


def test_container_sockets_are_denied_by_default_no_opt_in_required(tmp_path, monkeypatch) -> None:
    """The socket floor is DEFAULT-ON, unlike `deny_standard_creds`. Denying a daemon socket costs
    the entity nothing it can legitimately do inside a floor, so there is no operational-fit reason
    to make the operator ask for it."""
    sock = tmp_path / "run" / "docker.sock"
    pol = _sock_policy(tmp_path, monkeypatch, sock)
    assert sock.resolve() in pol.deny_sockets


def test_allow_container_sockets_removes_every_arm_not_just_the_connect(tmp_path, monkeypatch) -> None:
    """The opt-out must clear the write-deny too. Leaving the socket write-denied while allowing the
    connect would half-break the operator who deliberately opted in, for no security gain."""
    sock = tmp_path / "run" / "docker.sock"
    pol = _sock_policy(tmp_path, monkeypatch, sock, allow_container_sockets=True)
    assert pol.deny_sockets == ()
    assert not any("docker.sock" in str(p) for p in pol.deny_write_files)


@live
def test_socket_deny_renders_as_network_outbound_and_not_as_a_file_rule(tmp_path, monkeypatch) -> None:
    """THE CLASS GUARD. If a future edit folds `deny_sockets` back into the `deny_files` block —
    which looks like a tidy simplification and passes every other test in this file — the profile
    loses its only `network-outbound` rule and the bypass silently reopens. Assert the VERB."""
    sock = tmp_path / "run" / "docker.sock"
    pol = _sock_policy(tmp_path, monkeypatch, sock)
    profile = SeatbeltProvider().render_profile(pol)
    assert "(deny network-outbound" in profile, (
        "the socket deny must render as network-outbound; a file-read*/file-write* deny does NOT "
        "block connect() to a unix socket (measured 2026-09-04) and would be a no-op"
    )
    net_block = profile.split("(deny network-outbound", 1)[1].split(")\n\n", 1)[0]
    assert str(sock.resolve()) in net_block


def test_socket_is_write_denied_at_both_spellings_and_its_ancestors(tmp_path, monkeypatch) -> None:
    """Arms (ii) and (iii). The connect-deny ALONE loses to `mv`: after renaming the socket the
    canonical path is one nothing names, and the daemon keeps serving the same listening inode.
    Both spellings, because link operations are matched LEXICALLY."""
    sock = tmp_path / "run" / "docker.sock"
    pol = _sock_policy(tmp_path, monkeypatch, sock)
    assert sock in pol.deny_write_files            # lexical — blocks mv/rm of the socket
    assert sock.resolve() in pol.deny_write_files  # resolved — content target
    # (iii) the parent dir is write-denied, or the whole dir can be relocated instead.
    assert sock.resolve().parent in pol.deny_write_dirs


def test_crown_jewel_reason_calls_a_socket_a_socket_not_an_ssh_vector(tmp_path, monkeypatch) -> None:
    """Sockets ride the same `deny_write_files` list as the ssh vectors, so without its own branch
    the in-process hand refuses a docker socket with the words "ssh persistence/exec vector
    (authorized_keys/config/rc)" — a false reason in the surface an operator reads to understand
    their own floor. The denial was always right; only the explanation would have lied."""
    sock = tmp_path / "run" / "docker.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.touch()
    pol = _sock_policy(tmp_path, monkeypatch, sock)
    reason = crown_jewel_reason(pol, sock)
    assert reason is not None
    assert "socket" in reason
    assert "authorized_keys" not in reason


@live
def test_live_container_socket_is_unreachable_and_cannot_be_renamed_out_of_its_deny(
    tmp_path, monkeypatch
) -> None:
    """THE ONLY TEST HERE THAT CAN FAIL WHEN THE FIX IS WRONG RATHER THAN MERELY ABSENT.

    Every assertion above reads the policy or the rendered profile — so all of them still pass if
    the socket paths are moved into ``deny_files`` and the profile stops denying the connect. This
    one stands up a REAL unix socket, renders the REAL profile, and drives a REAL ``sandbox-exec``,
    which is the only way to observe that a file-verb deny does not stop ``connect()``.

    Three arms, each of which was measured failing on its own before the arm was added:
      · CONNECT      — refused by the ``network-outbound`` rule
      · RENAME       — the socket cannot be moved to a path nothing denies
      · RELOCATION   — nor can its parent directory
    Plus a liveness control: the server is still serving throughout, so a refusal is a refusal and
    not a dead socket (an attack that fails must be shown to fail for the RIGHT reason)."""
    import socket as _socket
    import shutil
    import subprocess
    import sys
    import tempfile
    import threading

    # AF_UNIX paths are capped near 104 bytes on macOS and pytest's tmp_path blows past it, so the
    # socket lives in a SHORT dir. /tmp is a symlink to /private/tmp, which makes this stricter
    # rather than weaker: the deny is rendered at the RESOLVED path and the client connects via the
    # LEXICAL one, so the test also proves seatbelt canonicalises for network-outbound.
    short_root = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvsock"))
    sock_dir = short_root / "run"
    sock_dir.mkdir(parents=True)
    sock = sock_dir / "docker.sock"

    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(5)

    # A short accept timeout rather than a blocking accept: `srv.close()` in the finally block
    # runs while this thread may be inside `accept()` on the same fd, and cross-thread
    # close-under-blocking-accept is platform- and timing-dependent (usually OSError, not
    # guaranteed). Polling a flag makes the shutdown deterministic instead of relying on the
    # close to raise. Test-only, but a flaky security test gets muted, and a muted security test
    # is worse than no test. (complement L3, 2026-09-04.)
    srv.settimeout(0.2)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                conn.sendall(b"JEWEL-VIA-SOCKET")
            finally:
                conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    try:
        pol = _sock_policy(tmp_path, monkeypatch, sock)
        profile = tmp_path / "p.sb"
        profile.write_text(SeatbeltProvider().render_profile(pol))

        client = tmp_path / "client.py"
        client.write_text(
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)\n"
            "s.connect(sys.argv[1]); print(s.recv(64).decode())\n"
        )

        def _in_sandbox(*argv: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["sandbox-exec", "-f", str(profile), *argv],
                capture_output=True, text=True, timeout=30,
            )

        # LIVENESS CONTROL FIRST: prove the server answers, so a later refusal is the sandbox's
        # doing and not a socket that was never listening.
        ctrl = subprocess.run([sys.executable, str(client), str(sock)],
                              capture_output=True, text=True, timeout=30)
        assert "JEWEL-VIA-SOCKET" in ctrl.stdout, f"server not serving: {ctrl.stderr}"

        # ARM 1 — connect refused.
        r = _in_sandbox(sys.executable, str(client), str(sock))
        assert "JEWEL-VIA-SOCKET" not in r.stdout, (
            "the confined hand reached the daemon socket — the network-outbound deny is not in "
            "force; a file-read*/file-write* deny does NOT block connect()"
        )

        # ARM 2 — the socket cannot be renamed to a path nothing denies.
        moved = sock_dir / "escaped.sock"
        r = _in_sandbox("/bin/mv", str(sock), str(moved))
        assert r.returncode != 0 and not moved.exists(), (
            "the socket was renamed out from under its deny; connecting to the new path would "
            "reach the same listening inode"
        )

        # ARM 3 — nor can its parent directory be relocated.
        r = _in_sandbox("/bin/mv", str(sock_dir), str(short_root / "escaped_dir"))
        assert r.returncode != 0 and sock.exists()

        # The server is STILL serving — none of the above broke it, so all three refusals are
        # the floor's doing.
        after = subprocess.run([sys.executable, str(client), str(sock)],
                               capture_output=True, text=True, timeout=30)
        assert "JEWEL-VIA-SOCKET" in after.stdout
    finally:
        stop.set()
        srv.close()
        shutil.rmtree(short_root, ignore_errors=True)


def test_load_confinement_config_allow_container_sockets_defaults_to_denied(tmp_path: Path) -> None:
    """ABSENT means DENIED, and absence collapsing to the default is only safe BECAUSE the default
    is the safe one — the opposite of ``deny_standard_creds``, whose absence must stay a live
    sentinel. An operator who never heard of this key keeps the socket fenced."""
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_files": []}')
    assert load_confinement_config(ent).allow_container_sockets is False


def test_load_confinement_config_parses_allow_container_sockets(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"allow_container_sockets": true}')
    assert load_confinement_config(ent).allow_container_sockets is True


def test_load_confinement_config_bad_allow_container_sockets_fails_closed(tmp_path: Path) -> None:
    """A null is REFUSED rather than read as absence: an explicit null is an operator typing
    something and meaning it, and on a security floor we do not guess which thing they meant."""
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"allow_container_sockets": null}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)
    (ent / ".levain" / "confinement.json").write_text('{"allow_container_sockets": "yes"}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


def test_load_confinement_config_allow_localhost_outbound_defaults_to_denied(tmp_path: Path) -> None:
    """spore-755. ABSENT means the connect-to-self deny stays ON (in agent mode) — same safe-default
    treatment as ``allow_container_sockets``."""
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_files": []}')
    assert load_confinement_config(ent).allow_localhost_outbound is False


def test_load_confinement_config_parses_allow_localhost_outbound(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"allow_localhost_outbound": true}')
    assert load_confinement_config(ent).allow_localhost_outbound is True


def test_load_confinement_config_bad_allow_localhost_outbound_fails_closed(tmp_path: Path) -> None:
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"allow_localhost_outbound": null}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)
    (ent / ".levain" / "confinement.json").write_text('{"allow_localhost_outbound": "yes"}')
    with pytest.raises(ConfinementError):
        load_confinement_config(ent)


def test_confinement_config_field_order_is_append_only() -> None:
    """⛔ codex L3 HIGH#3, 2026-09-13 — the ``own_memory_files`` positional-shift class, recurring.
    ``ConfinementConfig`` is EXPORTED and NOT keyword-only, so inserting a field anywhere but the END
    silently reassigns every later positional argument. ``allow_localhost_outbound`` was first
    inserted BEFORE ``efferent_gate``; a positional ``ConfinementConfig((), (), "agent", None, False,
    "gated")`` then landed "gated" in ``allow_localhost_outbound`` (truthy → the connect-to-self deny
    SILENTLY OFF) and left ``efferent_gate="auto"``. This pins BOTH the invariant (new fields append)
    and the concrete construction that broke.
    """
    from dataclasses import fields

    names = [f.name for f in fields(ConfinementConfig)]
    # The FROZEN historical prefix (positional indices existing callers depend on). New fields may be
    # appended AFTER this prefix — that is correct append-only evolution and must NOT fail the test
    # (codex L3 LOW#3); what must fail is INSERTING before it, which shifts every later index.
    # Freeze the ENTIRE current positional prefix, allow_localhost_outbound INCLUDED (codex L3 LOW,
    # fix-pass): freezing only the 6 fields before it would still permit inserting a field BETWEEN
    # efferent_gate and allow_localhost_outbound — the 6-prefix would match and membership would pass,
    # yet the current 7-position constructor `ConfinementConfig((), (), "raw", True, False, "gated",
    # True)` would silently land True in the inserted field. Future fields may follow position 7.
    frozen_prefix = [
        "deny_files", "deny_subtrees", "ssh_mode", "deny_standard_creds",
        "allow_container_sockets", "efferent_gate", "allow_localhost_outbound",
    ]
    assert names[: len(frozen_prefix)] == frozen_prefix, (
        f"the frozen positional prefix of the exported ConfinementConfig changed: {names}. "
        f"Append new fields AFTER position {len(frozen_prefix)} (or make the class keyword-only in a "
        f"deliberate break) — inserting anywhere in the prefix silently reassigns every later "
        f"positional field (the own_memory_files class)."
    )
    # the historical positional construction must still land each value in its intended field
    cfg = ConfinementConfig((), (), "raw", True, False, "gated")
    assert cfg.ssh_mode == "raw"
    assert cfg.deny_standard_creds is True
    assert cfg.allow_container_sockets is False
    assert cfg.efferent_gate == "gated"  # NOT shifted into a later field
    assert cfg.allow_localhost_outbound is False  # the default, not a positional spillover


def test_every_confinement_provider_must_consume_deny_sockets(tmp_path, monkeypatch) -> None:
    """⛔ A TRIPWIRE ON THE PROVIDER ROSTER. Written for the K4c merge, when `deny_sockets` was
    enforced only by `SeatbeltProvider.render_profile`'s `network-outbound` rule; it fired as
    designed when the port made `BwrapProvider` concrete (2026-09-30). `build_policy` populates the
    field unconditionally and this module's prose calls it the UNIVERSAL floor, which stays true only
    while every provider consumes it.
    A policy field that one provider honours and another silently ignores is a floor that reports
    itself armed on a platform where the flagship bypass still works.

    ⚠ THE FAILURE MODE THIS CATCHES IS A MERGE, WHICH IS WHY IT IS SHAPED LIKE THIS. Nothing in a
    Linux provider's own diff would look wrong; the defect is an ABSENCE, in a different file,
    relative to a field added on another branch. Nobody re-reads `confinement.py`'s socket arms
    while writing a mount-namespace renderer. So the guard is a hard assertion on the provider
    ROSTER: add a provider and this fails until you have decided, explicitly, what it does about
    sockets.

    This file already states the convention it is enforcing — ``bwrap`` and a container backend
    are PURE ADDITIONS that "MUST re-prove" the floor's properties rather than inherit the claim.

    ⛔ DO NOT SATISFY THIS BY EDITING THE SET. Wire the provider, or — if a connect-deny is
    genuinely impossible on that platform — make the honesty surfaces say so per-platform and
    change this docstring to record that ruling. Raised by complement at L3, 2026-09-04, which
    read the "universal floor" wording against the single enforcer and called the gap correctly.
    """
    from levain.firing import confinement as _conf

    def _concrete(cls) -> set:
        out = set()
        for sub in cls.__subclasses__():
            # The roster is the module's SHIPPED providers; a stub a test defines is not one.
            if not getattr(sub, "__abstractmethods__", None) and sub.__module__ == _conf.__name__:
                out.add(sub.__name__)
            out |= _concrete(sub)
        return out

    # 2026-09-30, K4c landing: BwrapProvider consumes `deny_sockets` in `_bwrap_argv` step (6)
    # (pinned by the bwrap socket tests below and measured live), and enforces `deny_localhost_outbound`
    # with `--unshare-net` (option B, 2026-10-01). Both are asserted by name in the tests below.
    providers = _concrete(_conf.ConfinementProvider)
    assert providers == {"SeatbeltProvider", "BwrapProvider"}, (
        f"the ConfinementProvider roster changed to {sorted(providers)}. Every provider MUST "
        f"enforce (or, for the localhost deny, refuse to spawn without) the connect-denies — "
        f"CrownJewelsPolicy.deny_sockets (spore-725, a reachable container daemon is a total "
        f"crown-jewels bypass) AND CrownJewelsPolicy.deny_localhost_outbound (spore-755, a local "
        f"sshd reached via the forwarded agent socket is the same class). Both are connect-denies "
        f"with NO in-process twin, so a provider that ignores either fails OPEN silently. Wire the "
        f"new provider for BOTH and update this assertion, or the floor is macOS-only while the "
        f"code and the run banner both call it universal."
    )


def test_no_shipped_provider_overrides_spawn_shell() -> None:
    """The refresh lives in the base `spawn_shell`; a provider that overrides it skips the refresh
    (spore-768). The K4c branch did exactly that before the port."""
    from levain.firing import confinement as _conf

    shipped = [c for c in _conf.ConfinementProvider.__subclasses__() if c.__module__ == _conf.__name__]
    assert {c.__name__ for c in shipped} >= {"SeatbeltProvider", "BwrapProvider"}
    for cls in shipped:
        assert "spawn_shell" not in vars(cls), f"{cls.__name__} overrides spawn_shell"
        assert "_spawn_shell_impl" in vars(cls)


def test_bwrap_enforces_the_localhost_deny_by_removing_the_network(tmp_path, monkeypatch) -> None:
    """Option B (2026-10-01, under Phill's go; until then the deny refused bash on Linux): bwrap has
    no per-destination connect deny, so the deny renders as `--unshare-net`. Measured on argushub:
    under it a 127.0.0.1 listener and an abstract-socket bus are refused, bash still works."""
    from levain.firing import confinement as _conf

    monkeypatch.setenv("HOME", str(tmp_path))
    assert _conf.BwrapProvider.enforces_localhost_deny is True
    assert _conf.BwrapProvider.localhost_deny_removes_network is True
    assert _conf.SeatbeltProvider.enforces_localhost_deny is True
    assert _conf.SeatbeltProvider.localhost_deny_removes_network is False
    on = _bwrap_argv(build_policy(_entity(tmp_path, "on"), deny_localhost_outbound=True))
    off = _bwrap_argv(build_policy(_entity(tmp_path, "off"), deny_localhost_outbound=False))
    assert "--unshare-net" in on and "--unshare-net" not in off


def _tmpfs_then_ro(argv: list[str], d: str) -> bool:
    """A read-only tmpfs over ``d``: mounted, then remounted read-only LATER (remounts are deferred to
    the end of the argv so mountpoints inside the tmpfs can still be created)."""
    pairs = [argv[k:k + 2] for k in range(len(argv))]
    if ["--tmpfs", d] not in pairs or ["--remount-ro", d] not in pairs:
        return False
    return pairs.index(["--remount-ro", d]) > pairs.index(["--tmpfs", d])


def _bwrap_socket_policy(tmp_path, monkeypatch, *socks):
    from levain.firing import confinement as _conf

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_conf, "_CONTAINER_DAEMON_SOCKETS", tuple(str(s) for s in socks))
    return build_policy(_entity(tmp_path))


def test_bwrap_hides_a_socket_in_a_daemon_dir_with_a_readonly_tmpfs(tmp_path, monkeypatch) -> None:
    """A ``--ro-bind S S`` does NOT stop connect() (measured: the daemon answered), and a
    ``/dev/null`` over-mount detaches when the daemon recreates its socket (measured). The
    directory tmpfs held across the restart, so a daemon-owned dir gets that form."""
    from levain.firing.confinement import _bwrap_argv

    d = tmp_path / ".colima" / "default"
    d.mkdir(parents=True)
    sock = d / "docker.sock"
    sock.touch()
    argv = _bwrap_argv(_bwrap_socket_policy(tmp_path, monkeypatch, sock))
    assert _tmpfs_then_ro(argv, str(d.resolve()))
    assert ["--ro-bind", str(sock.resolve()), str(sock.resolve())] not in [
        argv[k:k + 3] for k in range(len(argv))
    ], "a read-only self-bind of a socket stops nothing and must not be rendered as a deny"


def test_bwrap_uses_a_dev_null_mount_for_a_socket_in_a_shared_dir(tmp_path, monkeypatch) -> None:
    from levain.firing.confinement import _bwrap_argv

    sock = tmp_path / "docker.sock"   # its parent is $HOME, which must never be hidden
    sock.touch()
    argv = _bwrap_argv(_bwrap_socket_policy(tmp_path, monkeypatch, sock))
    assert ["--ro-bind", "/dev/null", str(sock.resolve())] in [argv[k:k + 3] for k in range(len(argv))]
    assert "--tmpfs" not in argv or str(tmp_path.resolve()) not in [
        argv[k + 1] for k, a in enumerate(argv) if a == "--tmpfs"
    ]


def test_bwrap_absent_daemon_dirs_cost_nothing_and_mount_nothing(tmp_path, monkeypatch) -> None:
    """THE BRICK, measured 2026-09-30: every absent daemon dir in the socket roster was self-bound as
    an ancestor and bwrap aborted ("Can't find source path /run/containerd"), so no Linux host
    without containerd, CRI-O and colima could spawn a shell. An absent dir under a parent this
    user cannot create in gets nothing at all."""
    from levain.firing.confinement import _bwrap_argv

    if os.geteuid() == 0:
        pytest.skip("root can create inside a 0555 dir, so the 'cannot create here' case cannot be built")
    ro = tmp_path / "rootowned"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        sock = ro / "containerd" / "containerd.sock"
        argv = _bwrap_argv(_bwrap_socket_policy(tmp_path, monkeypatch, sock))
    finally:
        ro.chmod(0o755)
    for spelling in {str(ro / "containerd"), str((ro / "containerd").resolve()), str(sock),
                     str(sock.resolve())}:
        assert spelling not in argv, spelling


def test_bwrap_hides_an_absent_daemon_dir_the_entity_could_start_a_daemon_in(tmp_path, monkeypatch) -> None:
    """`systemctl --user start podman.socket` works from inside the sandbox, so a rootless daemon dir
    that is absent at spawn is still mounted (bwrap creates it empty) — otherwise the socket the
    entity starts afterwards would be visible."""
    from levain.firing.confinement import _bwrap_argv

    run = tmp_path / "xdg"
    run.mkdir()
    sock = run / "podman" / "podman.sock"
    argv = _bwrap_argv(_bwrap_socket_policy(tmp_path, monkeypatch, sock))
    assert _tmpfs_then_ro(argv, str((run / "podman").resolve()))


def test_xdg_runtime_socket_entries_follow_the_variable_and_the_linux_default(monkeypatch) -> None:
    """The env dir when it is set and absolute, and on Linux ALSO /run/user/<uid>, where a
    `systemctl --user` socket lives whatever the launching shell says (complement, L3 r1)."""
    from levain.firing import confinement as _conf
    from levain.firing.confinement import _expand_socket_source

    default = Path(f"/run/user/{os.getuid()}/docker.sock")
    monkeypatch.setattr(_conf.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nix/run")
    assert _expand_socket_source("$XDG_RUNTIME_DIR/docker.sock") == [Path("/nix/run/docker.sock"), default]
    for unset in (None, "", "relative/dir"):
        if unset is None:
            monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        else:
            monkeypatch.setenv("XDG_RUNTIME_DIR", unset)
        monkeypatch.setattr(_conf.platform, "system", lambda: "Linux")
        assert _expand_socket_source("$XDG_RUNTIME_DIR/docker.sock") == [default]
        monkeypatch.setattr(_conf.platform, "system", lambda: "Darwin")
        assert _expand_socket_source("$XDG_RUNTIME_DIR/docker.sock") == []


def test_crown_jewel_reason_fails_CLOSED_on_a_tilde_user_path_not_by_raising(tmp_path) -> None:
    """⛔ `expanduser()` raises RuntimeError — not OSError, not ValueError — for a `~user` with no
    passwd entry, and `path` here is supplied BY THE ENTITY. Before this clause caught it, a
    confabulating model emitting `~unknownuser/...` reached a function whose docstring promises
    FAIL-CLOSED and got neither a denial nor an allow: it got an exception out of the security
    predicate.

    ⚠ THE SAME RELEASE FIXED THIS CLASS IN `doctor.py` AND DID NOT REACH THE CONFINEMENT TWIN —
    `guard_scoped_by_symptom_misses_the_class`, with the class named in the release that missed it.
    """
    policy = build_policy(_entity(tmp_path))
    reason = crown_jewel_reason(policy, "~nosuchuser42/.ssh/authorized_keys")
    assert reason is not None, "a path that cannot be resolved must be REFUSED, not allowed"
    assert "could not be resolved" in reason


def test_the_tilde_user_path_actually_raises_RuntimeError(tmp_path) -> None:
    """THE PREMISE, PINNED. If a future CPython stops raising RuntimeError here — or starts raising
    something else — the test above would pass for the wrong reason (no exception to catch). This
    one fails loudly instead, so the guard above never becomes decorative."""
    with pytest.raises(RuntimeError):
        Path("~nosuchuser42/.ssh/authorized_keys").expanduser()


def test_the_refresh_is_upstream_so_no_provider_can_skip_it(tmp_path, monkeypatch) -> None:
    """⛔⛔ THE INVARIANT THAT REPLACED A METACLASS GUARD DEFEATED SEVEN TIMES ACROSS FIVE VERSIONS
    (Phill ruled 2026-09-04, on codex L3 round 7).

    The guard tried to make it IMPOSSIBLE TO SKIP a call, and Python does not support making a class
    hierarchy tamper-proof against its own subclasses — v1 `cls.__dict__` fell to a mixin, v2 the
    module global fell to a reload, v3 `parents[0]` over-refused valid multiple inheritance, v4
    `cls.__mro__[1:]` fell to the mixin again, v5's inherited marker fell to marker poisoning, a
    derived metaclass, and multiple marked roots. Post-definition assignment was never closed at all.

    ⚡ Moving the refresh UPSTREAM makes the call not exist at that layer. A provider that overrides
    `spawn_shell` **entirely** — the exact thing five guards tried to forbid — still receives an
    ALREADY-REFRESHED policy, because `_ensure_shell` refreshed it before calling. There is nothing
    to skip. That is what this test pins, and it is why the guard could be deleted rather than
    rewritten a sixth time."""
    # ⛔ SKIPPING HERE IS NOT NEUTRAL, WHICH IS WHY THIS REASON IS NOT THE SIBLING'S.
    # This is the test that pins the invariant the deleted `spawn_shell` guard was
    # traded for (five versions, seven bypasses — fd653cd). `openhands` is an EXTRA:
    # it is absent from a base install, absent from the `dev` extra, and this repo has
    # NO CI. It therefore does NOT run under `pip install levain` or `pip install -e
    # '.[dev]'`, which is how a bare `python3 -m pytest` reads this repo — and a plain
    # "1 skipped" would report that absence as health.
    # ⚠ IT DOES RUN where the extra is present, and that is not hypothetical: this
    # clone's `.venv` carries it, and `scripts/hooks/pre-push` prefers `.venv/bin/python`,
    # so the push gate exercises this test while a bare `python3` run skips it. Executed
    # green there 2026-09-05 (`.venv/bin/python -m pytest -k ...` -> 1 passed). Which
    # interpreter you use decides whether this invariant is checked at all.
    pytest.importorskip(
        "openhands.tools.terminal",
        reason="openhands extra absent — the post-guard-deletion invariant is NOT "
               "exercised in this environment (pip install -e '.[openhands]' to run it)",
    )
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import build_policy

    ent = tmp_path / "ent"
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)

    listed = tmp_path / "run" / "docker.sock"
    listed.parent.mkdir(parents=True)
    from levain.firing import confinement as _conf
    monkeypatch.setattr(_conf, "_CONTAINER_DAEMON_SOCKETS", (str(listed),))
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))

    # The socket appears AFTER the policy was built — the whole point of the refresh.
    unlisted = tmp_path / "elsewhere" / "real.sock"
    unlisted.parent.mkdir(parents=True)
    unlisted.touch()
    listed.symlink_to(unlisted)

    seen: list = []

    class _RogueProvider:
        """Overrides spawn_shell completely and never refreshes anything — what the guard forbade."""

        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            seen.append(policy)
            sh = SandboxedShell(argv=["/bin/true"], cwd=ws, env={})
            sh.effective_policy = policy
            return sh

    import levain.firing.openhands.tools as _t
    monkeypatch.setattr(_t, "select_provider", lambda: _RogueProvider())
    ex._ensure_shell()

    assert len(seen) == 1
    assert unlisted.resolve() in seen[0].deny_sockets, (
        "a provider that skips the seam entirely still got a refreshed policy — that is the "
        "invariant; if this fails, deleting the guard was wrong"
    )


def test_no_unreachable_statements_in_the_confinement_surface() -> None:
    """⛔⛔ THE GATE MY GATES DID NOT HAVE. On 2026-09-04 a rewrite of `floor_for_conv_state` left
    SIX LINES OF DEAD CODE after its `return` — and **2,481 tests, ruff and mypy all passed and it
    was COMMITTED.** Both review seats flagged it (glm HIGH, complement MED) and nothing mechanical
    did.

    ⚡ It was not inert, either: the dead tail stored the WRONG dict-value type for a registry whose
    every reader unpacks a 2-tuple, and it re-created the publish-before-finalize ordering the live
    code above it exists to eliminate. A merge, a partial revert, or a careless edit making it
    reachable would have reintroduced the exact fail-open this release closes.
    ▶ So this is a real gate, not tidiness: unreachable code in a security surface is a defect that
    every existing instrument was blind to."""
    import ast
    import pathlib

    offenders: list[str] = []
    for name in ("levain/firing/confinement.py", "levain/firing/openhands/tools.py"):
        path = pathlib.Path(__file__).resolve().parents[1] / name
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for i, stmt in enumerate(body[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    offenders.append(f"{name}:{body[i + 1].lineno} is unreachable")
    assert not offenders, "unreachable code in the confinement surface: " + "; ".join(offenders)


def test_spawn_shell_refreshes_so_every_consumer_gets_it_not_just_one_call_site(
    tmp_path, monkeypatch
) -> None:
    """⛔ complement MED + glm-5.2 MED, CONVERGENT (2026-09-04), and glm's sentence is the correction:
    *"the metaclass was correctly deleted, but the template-method refresh it was protecting was
    deleted alongside it. ONLY THE METACLASS NEEDED TO GO."*

    I conflated a GUARD that tried to make the refresh unskippable (impossible in Python; seven
    bypasses across five versions; correctly deleted) with the REFRESH ITSELF at the layer that owns
    it. Removing the refresh left the invariant on exactly ONE call site, so any second consumer —
    `BwrapProvider` on the held branch, a debug harness, a test — rendering `spawn_shell(policy)`
    without refreshing first would get the BUILD-TIME `deny_sockets`: the spore-768 regression,
    reintroduced by the fix for the guard protecting against it.

    Call `spawn_shell` DIRECTLY, with no executor involved, and assert the refresh still happened."""
    listed = tmp_path / "run" / "docker.sock"
    listed.parent.mkdir(parents=True)
    pol = _sock_policy(tmp_path, monkeypatch, listed)

    unlisted = tmp_path / "elsewhere" / "real.sock"
    unlisted.parent.mkdir(parents=True)
    unlisted.touch()
    listed.symlink_to(unlisted)          # appears AFTER the policy was built

    from levain.firing.confinement import ConfinementProvider

    class _Direct(ConfinementProvider):
        def available(self):
            return True

        def render_profile(self, policy):
            return ""

        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0):
            return SandboxedShell(argv=["/bin/true"], cwd=tmp_path, env={})

    shell = _Direct().spawn_shell(pol)   # no _ensure_shell anywhere in this path
    assert shell.effective_policy is not None
    assert unlisted.resolve() in shell.effective_policy.deny_sockets, (
        "a direct consumer of spawn_shell got an UNREFRESHED policy — the invariant is back on one "
        "call site only"
    )


def test_a_seatbelt_shell_unlinks_its_profile_even_if_the_base_close_raises(tmp_path) -> None:
    """⛔ complement L3 LOW. `_SeatbeltShell.close` ran `super().close()` and THEN unlinked its temp
    SBPL profile, so a raising base teardown leaked one profile file per failed shell — and nothing
    upstream can compensate, because a generic fallback has never heard of that file.
    ⚡ The general form: a subclass's ADDITIVE cleanup must be robust to its parent's failure."""
    from levain.firing.confinement import SandboxedShell, _SeatbeltShell

    prof = tmp_path / "levain-seatbelt-test.sb"
    prof.write_text("(version 1)")
    sh = _SeatbeltShell(argv=["/bin/true"], cwd=tmp_path, env={}, profile_path=prof)

    def _boom(self):
        raise OSError("base teardown failed")

    original = SandboxedShell.close
    try:
        SandboxedShell.close = _boom  # type: ignore[method-assign]
        with pytest.raises(OSError):
            sh.close()
    finally:
        SandboxedShell.close = original  # type: ignore[method-assign]

    assert not prof.exists(), "the temp seatbelt profile leaked when the base close raised"


# =============================================================================================
# CONNECT-TO-SELF (spore-755) — the loopback/local-interface sshd bypass and its deny
# =============================================================================================


def test_localhost_outbound_deny_is_gated_by_the_flag(tmp_path: Path, monkeypatch) -> None:
    """RENDER-LAYER (always on): the ``deny_localhost_outbound`` flag is the ONLY thing that emits
    the ``(deny network-outbound (remote ip "localhost:*"))`` rule — off by default, on when asked.
    This grades the profile STRING; the actual closure is graded by the live attack test below
    (a render assertion cannot prove a connect() is refused — spore-938)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    prov = SeatbeltProvider()
    line = '(deny network-outbound (remote ip "localhost:*"))'
    off = prov.render_profile(build_policy(_entity(tmp_path, "off"), ssh_mode="agent"))
    on = prov.render_profile(
        build_policy(_entity(tmp_path, "on"), ssh_mode="agent", deny_localhost_outbound=True)
    )
    assert line not in off  # default off: behaviour unchanged
    assert line in on  # opted in: the self-connect deny is rendered


@live
def test_live_localhost_outbound_deny_blocks_the_self_sshd_bypass(tmp_path: Path) -> None:
    """⛔ spore-755, THE LIVE PROOF (reproduced end to end 2026-09-13, ruled by Phill). A sshd this
    host runs reads, as unsandboxed root, a file the floor denies — the container-daemon-socket class
    (spore-725). Fully self-contained: a THROWAWAY non-root sshd on a spare port authorising a FRESH
    key held by a DEDICATED agent (never the operator's real sshd / Remote Login / agent).

    The attack is agent-ONLY — no ``-i``, ``IdentitiesOnly=yes`` + ``IdentityAgent`` pointed at the
    dedicated socket — AND the key FILE is floor-denied, so the FORWARDED AGENT is provably the sole
    carrier (codex L3 HIGH#4: an ``-i`` attack could authenticate off a readable key file and would
    not prove that). Three arms:
      · CONTROL (agent OFF, deny OFF): attack must FAIL "Permission denied" — no agent, key unreadable,
        so nothing authenticates. This is what proves the agent, not the key file, carries the leak.
      · deny OFF (agent on): the bypass LEAKS (so a pass with it on is the deny working, not a dead
        channel).
      · deny ON (agent on): REFUSED by EVERY local-interface address — 127.0.0.1, ::1, and the LAN IP
        when present (the coverage the whole fix rests on) — while the direct read stays denied.
    """
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not (Path(sshd).exists() and shutil.which("ssh") and shutil.which("ssh-keygen")):
        pytest.skip("needs sshd + ssh + ssh-keygen on PATH")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    rig = tmp_path / "rig"
    rig.mkdir()
    key = rig / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    host = rig / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host)], check=True)
    authorized = rig / "authorized_keys"
    authorized.write_text((key.with_suffix(".pub")).read_text())
    authorized.chmod(0o600)
    known = rig / "known_hosts"
    sshd_conf = rig / "sshd_config"
    # bind ALL interfaces so the attack can be aimed at loopback AND the machine's own LAN address —
    # the fix's load-bearing claim is that seatbelt's "localhost" covers every LOCAL-INTERFACE
    # address, not loopback only, and a 127.0.0.1-only test would never exercise that (L1 W2).
    sshd_conf.write_text(
        f"Port {port}\nListenAddress 0.0.0.0\nListenAddress ::\nHostKey {host}\nPidFile {rig / 'pid'}\n"
        f"AuthorizedKeysFile {authorized}\nPubkeyAuthentication yes\nPasswordAuthentication no\n"
        f"KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\n"
    )

    canary_dir = rig / "jewel"
    canary_dir.mkdir()
    canary = canary_dir / "secret.txt"
    token = "CANARY-" + os.urandom(8).hex()
    canary.write_text(token)

    agent_pid = None
    sshd_proc = None
    try:
        # a DEDICATED agent holding only the fresh key (started INSIDE the try so a failure here still
        # cleans up — codex L3 LOW / glm / complement: an agent started before the try orphans)
        agent = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, check=True).stdout
        auth_sock = next(
            ln.split("=", 1)[1].split(";", 1)[0] for ln in agent.splitlines() if "SSH_AUTH_SOCK=" in ln
        )
        agent_pid = next(
            ln.split("=", 1)[1].split(";", 1)[0] for ln in agent.splitlines() if "SSH_AGENT_PID=" in ln
        )
        agent_env = {**os.environ, "SSH_AUTH_SOCK": auth_sock}
        subprocess.run(["ssh-add", str(key)], env=agent_env, check=True, capture_output=True)

        # capture sshd stderr so a rig failure is DIAGNOSABLE, not silent (codex L3 MED#2). The
        # tool-absence skip is preflight (above); once we have decided to start sshd, a failure to
        # come up is a TEST FAILURE with diagnostics — a broken rig must NOT quietly delete the only
        # end-to-end enforcement proof while CI stays green.
        sshd_err = rig / "sshd.stderr"
        with open(sshd_err, "wb") as errf:
            sshd_proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(sshd_conf)], stderr=errf)
        for _ in range(50):
            if sshd_proc.poll() is not None:  # the daemon exited before listening
                pytest.fail(
                    f"throwaway sshd exited rc={sshd_proc.returncode} before listening:\n"
                    f"{sshd_err.read_text(errors='replace')}"
                )
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            pytest.fail(
                f"throwaway sshd did not accept a connection on :{port} within the deadline; "
                f"stderr:\n{sshd_err.read_text(errors='replace')}"
            )

        # every LOCAL address the host answers on: loopback v4/v6 always, plus the LAN IP when this
        # box has one. ::1 is the always-present NON-v4 coverage; the LAN case is opportunistic and is
        # reported as skipped rather than silently dropped (glm L3) so a LAN-less run cannot masquerade
        # as having exercised the LAN path.
        lan = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True
        ).stdout.strip() or subprocess.run(
            ["ipconfig", "getifaddr", "en1"], capture_output=True, text=True
        ).stdout.strip()
        targets = [("127.0.0.1", "127.0.0.1"), ("::1", "::1")] + ([("LAN", lan)] if lan else [])
        if not lan:
            warnings.warn("no en0/en1 LAN address — the LAN-interface coverage was NOT exercised")

        # agent-only: no -i, and `-F /dev/null` ignores the operator's ~/.ssh/config (whose `Host *
        # IdentityFile` would otherwise, under IdentitiesOnly, pin a key FILE instead of the agent),
        # so the forwarded agent socket is the ONLY identity source. use_agent=False points
        # IdentityAgent at nothing → no identities at all.
        def atk(host: str, *, use_agent: bool) -> str:
            ident = f"-o IdentityAgent={auth_sock}" if use_agent else "-o IdentityAgent=none"
            return (
                f"ssh -F /dev/null -p {port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                f"-o UserKnownHostsFile={known} -o ConnectTimeout=6 {ident} "
                f"{host} 'cat {canary}' 2>&1"
            )

        entity = _entity(tmp_path)

        def run(deny: bool, host: str, *, use_agent: bool = True):
            pol = build_policy(
                entity,
                deny_files=(canary, key),  # the KEY FILE is floor-denied → agent is the only carrier
                extra_deny_read_write=(canary_dir,),
                ssh_mode="agent",
                deny_localhost_outbound=deny,
            )
            with select_provider().spawn_shell(
                pol, env={**agent_env, "HOME": str(Path.home())}
            ) as sh:
                direct = sh.run(f"cat {canary} 2>&1", timeout=15)
                attacked = sh.run(atk(host, use_agent=use_agent), timeout=25)
            return direct, attacked

        # CONTROL: agent OFF, deny OFF — key file denied, no agent → cannot authenticate at all.
        # This is the proof that the AGENT is the carrier (not the readable key file).
        _direct, atk_noagent = run(False, "127.0.0.1", use_agent=False)
        assert token not in atk_noagent.output, "leaked without the agent — key file was the carrier"
        assert "permission denied" in atk_noagent.output.lower(), atk_noagent.output

        # deny OFF (agent on) — the bypass is real (else an ON pass proves nothing)
        direct_off, atk_off = run(False, "127.0.0.1")
        assert "not permitted" in direct_off.output.lower()  # floor denies the direct read
        assert token in atk_off.output  # ... the forwarded agent carries the leak

        # deny ON — closed by EVERY local-interface address; direct read still denied. A future macOS
        # change to how "localhost" resolves must break this loudly, per address.
        for label, host in targets:
            direct_on, atk_on = run(True, host)
            assert "not permitted" in direct_on.output.lower(), label
            assert token not in atk_on.output, f"{label}: token leaked with deny ON"
            assert "not permitted" in atk_on.output.lower(), (
                f"{label}: expected a sandbox refusal, got {atk_on.output!r}"
            )
    finally:
        if sshd_proc is not None:
            sshd_proc.terminate()
            try:
                sshd_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sshd_proc.kill()
                sshd_proc.wait(timeout=5)  # reap after SIGKILL so it can't linger as a zombie (codex L3 #4)
        if agent_pid is not None:
            subprocess.run(["kill", agent_pid], capture_output=True)
# =============================================================================================
# BwrapProvider — the K4c Linux floor. THE MEASURED EQUIVALENCE TABLE, PINNED AS A CONTRACT.
#
# Every assertion below corresponds to a behaviour MEASURED on Ubuntu 24.04 / bubblewrap 0.9.0
# (2026-09-03), not to a reading of the bwrap man page. These tests are PURE — they check the
# rendered invocation — so they run on macOS, where the enforcement itself cannot be exercised.
# ⚠ THAT IS THE LIMIT OF THIS FILE AND IT IS DELIBERATE: a green run here proves the argv says
# what we measured to be correct, NEVER that the kernel does it. The enforcement half is owed on
# a real Linux host (see `_bwrap_argv`'s module comment).
# =============================================================================================


def _lin_policy(tmp_path: Path, monkeypatch, **kw) -> CrownJewelsPolicy:
    monkeypatch.setenv("HOME", str(tmp_path))
    return build_policy(_entity(tmp_path), **kw)


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every (flag, target) occurrence for a two-argument bwrap op."""
    return [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def _triples(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every (src, dest) for a three-argument bwrap op like ``--ro-bind SRC DEST``."""
    return [
        (argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag and i + 2 < len(argv)
    ]


def test_bwrap_base_flags_are_the_default_allow_polarity(tmp_path, monkeypatch) -> None:
    """``--bind / /`` IS the polarity flip in one flag: the whole filesystem present and writable,
    with the floor then subtracted. ``--proc``/``--dev`` are MEASURED as required, not defensive —
    with ``--bind / /`` alone, /proc is missing and /dev/null is unopenable, which breaks ordinary
    tooling and silently breaks shell redirections."""
    argv = _bwrap_argv(_lin_policy(tmp_path, monkeypatch))
    assert argv[0] == BWRAP
    assert argv[1:4] == ["--bind", "/", "/"]
    assert ("--proc", "/proc") in _pairs(argv, "--proc")
    assert ("--dev", "/dev") in _pairs(argv, "--dev")
    assert "--die-with-parent" in argv


def test_bwrap_never_uses_a_try_variant(tmp_path, monkeypatch) -> None:
    """⛔ THE FAIL-OPEN BAN. ``--ro-bind-try``/``--bind-try`` START THE SANDBOX WITH NOTHING
    PROTECTING THE PATH when the source is missing (measured). A security-surface generator that
    degrades to a pass is the shape of the defect this whole module is written against, so the try
    variants may never appear — including as a future "fix" for a missing-source crash."""
    argv = _bwrap_argv(_lin_policy(tmp_path, monkeypatch))
    assert not [a for a in argv if a.endswith("-try")]


def test_bwrap_subtrees_are_tmpfs_AND_remount_ro(tmp_path, monkeypatch) -> None:
    """A BARE tmpfs hides the jewel but lets writes into it SILENTLY SUCCEED and vanish — the entity
    is told its write worked when nothing was written. ``--remount-ro`` keeps the hiding and makes
    the refusal honest (EROFS). Both measured; the pairing is the contract."""
    policy = _lin_policy(tmp_path, monkeypatch)
    assert policy.deny_read_write, "fixture must produce at least one crown-jewel subtree"
    argv = _bwrap_argv(policy)
    for sub in policy.deny_read_write:
        assert ("--tmpfs", str(sub)) in _pairs(argv, "--tmpfs")
        assert ("--remount-ro", str(sub)) in _pairs(argv, "--remount-ro")


def test_bwrap_every_tmpfs_is_remounted_ro_except_the_ssh_dir(tmp_path, monkeypatch) -> None:
    """The invariant behind the previous test, stated so a NEW tmpfs added later cannot quietly skip
    the pairing. The ssh dir is the ONE deliberate exception: files are bound back INTO it (known_hosts
    must stay writable so ssh can record host keys), so it cannot be read-only."""
    policy = _lin_policy(tmp_path, monkeypatch)
    argv = _bwrap_argv(policy)
    tmpfs = {t for _, t in _pairs(argv, "--tmpfs")}
    ro = {t for _, t in _pairs(argv, "--remount-ro")}
    exempt = {str(policy.ssh_dir)} if policy.ssh_dir is not None else set()
    assert tmpfs - ro == exempt


def test_bwrap_ancestor_dirs_are_self_bound_parents_before_children(tmp_path, monkeypatch) -> None:
    """THE FINDING THAT NEARLY WENT THE OTHER WAY. Inside one sandbox, renaming a jewel's ancestor
    does not expose it (the over-mount travels with the dentry) — which makes the ancestor pin look
    unnecessary on Linux. It is not: the rename PERSISTS TO THE HOST, the next sandbox over-mounts
    the ORIGINAL path, bwrap silently CREATES that missing path, and the tmpfs lands on a decoy while
    the real jewel sits readable at the new location. Measured: it printed the jewel.

    ``--bind P P`` is the exact analogue of macOS's ``(deny file-write* (literal <dir>))`` — the dir
    becomes a mountpoint, so renaming it is EBUSY while creating files INSIDE it still works.

    Parent-before-child is a HARD ordering requirement: a child over-mount must land inside an
    already-established parent bind."""
    policy = _lin_policy(tmp_path, monkeypatch)
    assert policy.deny_write_dirs, "fixture must produce ancestor dirs"
    argv = _bwrap_argv(policy)
    self_binds = [(s, d) for s, d in _triples(argv, "--bind") if s == d]
    bound = [d for _, d in self_binds]
    for anc in policy.deny_write_dirs:
        # Pinned when it exists (at its RESOLVED path: a mount cannot land on a symlink, /var/run,
        # macOS's /var) or when the argv will CREATE something under it; an absent ancestor nothing
        # lands under is skipped (self-binding a missing source aborted bwrap, 2026-09-30).
        made = [d for k, a in enumerate(argv) if a in ("--tmpfs", "--dir") for d in [argv[k + 1]]]
        made += [d for _, d in _triples(argv, "--ro-bind")] + [d for _, d in _triples(argv, "--bind")]
        expected = anc.is_dir() or any(m == str(anc) or m.startswith(str(anc) + "/") for m in made)
        assert (str(anc.resolve() if anc.is_dir() else anc) in bound) == expected, anc
    for i, a in enumerate(bound):
        for b in bound[i + 1:]:
            assert not Path(b) in Path(a).parents, f"{b} is a parent of {a} but is mounted after it"


def test_bwrap_cred_files_and_config_deny_both_directions(tmp_path, monkeypatch) -> None:
    """``--ro-bind /dev/null`` denies READ (EACCES) as well as write — the closest analogue of macOS's
    ``(deny file-read* file-write* (literal ...))``. Measured to hold with AND without ``--dev``, so
    it does not depend on the device tree."""
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN")
    policy = _lin_policy(tmp_path, monkeypatch, deny_files=(secret,))
    argv = _bwrap_argv(policy)
    devnull_targets = [d for s, d in _triples(argv, "--ro-bind") if s == "/dev/null"]
    assert str(secret.resolve()) in devnull_targets
    if policy.config_file is not None:
        # An ABSENT config gets no mount (a /dev/null mountpoint would leave an empty JSON stub the
        # next session refuses); the read-only store dir is what stops the shell creating it.
        assert (str(policy.config_file) in devnull_targets) == policy.config_file.exists()


def _store_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    lv = ent / ".levain"
    lv.mkdir(parents=True, exist_ok=True)
    (lv / "memory.db").write_text("")
    (lv / "context.json").write_text("{}")
    (lv / "docs").mkdir()
    return build_policy(ent), lv


def test_bwrap_creates_and_pins_an_absent_ssh_dir_before_planting_anything_in_it(tmp_path, monkeypatch) -> None:
    """L1 H1 / L2 (2026-09-30): raw mode with no ~/.ssh. The argv makes bwrap CREATE ~/.ssh to hold
    the vector mounts; unpinned, the entity could rename it away, make a fresh one and plant
    ``authorized_keys`` on the host. It must be created and pinned BEFORE any mount inside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not (tmp_path / ".ssh").exists()
    from levain.firing.confinement import _bwrap_plan

    argv, create_first = _bwrap_plan(build_policy(_entity(tmp_path), ssh_mode="raw"))
    ssh = str(tmp_path / ".ssh")
    assert ssh in create_first, "bwrap cannot pin a dir it creates; the provider must create it first"
    i_pin = [argv[k:k + 3] for k in range(len(argv))].index(["--bind", ssh, ssh])
    inside = [k for k, a in enumerate(argv) if a.startswith(ssh + "/")]
    assert inside and min(inside) > i_pin, "a mount inside ~/.ssh landed before ~/.ssh was pinned"


def test_bwrap_refuses_a_replaceable_symlinked_jewel_ancestor(tmp_path, monkeypatch) -> None:
    """A mount cannot pin a symlink; pinning its target leaves the link swappable. Fail closed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "dotfiles-ssh"
    real.mkdir()
    (tmp_path / ".ssh").symlink_to(real)
    with pytest.raises(ConfinementError, match="symlink"):
        _bwrap_argv(build_policy(_entity(tmp_path), ssh_mode="raw"))


def test_bwrap_refuses_a_replaceable_symlinked_protected_file(tmp_path, monkeypatch) -> None:
    """Measured 2026-09-30: a stow-style symlinked ~/.ssh/config made bwrap abort with an opaque
    startup error. It is refused now with the reason, before bwrap runs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    real = tmp_path / "dotfiles-config"
    real.write_text("Host *\n")
    (ssh / "config").symlink_to(real)
    with pytest.raises(ConfinementError, match="symlink"):
        _bwrap_argv(build_policy(_entity(tmp_path), ssh_mode="raw"))


def test_bwrap_a_file_denied_both_ways_is_not_re_exposed_by_the_write_only_pass(tmp_path, monkeypatch) -> None:
    """L2 (2026-09-30): an operator pins ~/.ssh/config in `deny_files` in raw mode. Step (4) masks it
    with /dev/null; step (5) used to add ``--ro-bind config config`` on top, re-exposing the read."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    cfg = ssh / "config"
    cfg.write_text("Host *\n")
    argv = _bwrap_argv(build_policy(_entity(tmp_path), ssh_mode="raw", deny_files=(cfg,)))
    triples = [argv[k:k + 3] for k in range(len(argv))]
    assert ["--ro-bind", "/dev/null", str(cfg)] in triples
    assert ["--ro-bind", str(cfg), str(cfg)] not in triples


def test_bwrap_mounts_the_entity_store_read_only_and_binds_ordinary_entries_back(tmp_path, monkeypatch) -> None:
    """The plant of a NOT-YET-EXISTING store file (a first continuity, a SQLite sidecar) can only be
    refused by making the dir read-only; ordinary existing entries stay writable as on macOS."""
    policy, lv = _store_policy(tmp_path, monkeypatch)
    argv = _bwrap_argv(policy)
    triples = [argv[k:k + 3] for k in range(len(argv))]
    assert ["--ro-bind", str(lv), str(lv)] in triples
    assert ["--bind-try", str(lv / "docs"), str(lv / "docs")] in triples
    # A top-level FILE is not bound back: a per-file bind pins its inode, and the host writes these
    # by rename, so the shell would read and write an orphan (complement, L3 r1).
    assert ["--bind", str(lv / "context.json"), str(lv / "context.json")] not in triples
    assert ["--bind", str(lv / "memory.db"), str(lv / "memory.db")] not in triples


def test_bwrap_a_jewel_inside_the_store_stays_hidden_under_the_store_mount(tmp_path, monkeypatch) -> None:
    """complement + codex, L3 r1: the store bind takes its source from the real tree, so emitted
    AFTER a jewel tmpfs under .levain it re-exposed that jewel. The tmpfs must come later."""
    policy, lv = _store_policy(tmp_path, monkeypatch)
    secret = lv / "docs"
    policy = build_policy(lv.parent, extra_deny_read_write=(secret,))
    argv = _bwrap_argv(policy)
    pairs = [argv[k:k + 2] for k in range(len(argv))]
    i_store = [argv[k:k + 3] for k in range(len(argv))].index(["--ro-bind", str(lv), str(lv)])
    i_tmpfs = pairs.index(["--tmpfs", str(secret)])
    assert i_store < i_tmpfs
    rebinds = [k for k in range(len(argv) - 2) if argv[k:k + 3] == ["--bind", str(secret), str(secret)]]
    assert all(k < i_tmpfs for k in rebinds), "the real dir was bound back over the jewel tmpfs"


def test_bwrap_nested_deny_roots_given_child_first_emit_only_the_parent(tmp_path, monkeypatch) -> None:
    """codex, L3 r1: a parent tmpfs after its child hid the child's mountpoint, and the deferred
    remount of the child aborted bwrap."""
    monkeypatch.setenv("HOME", str(tmp_path))
    parent = tmp_path / "secret"
    child = parent / "private"
    child.mkdir(parents=True)
    argv = _bwrap_argv(build_policy(_entity(tmp_path), extra_deny_read_write=(child, parent)))
    tmpfs = [argv[k + 1] for k, a in enumerate(argv) if a == "--tmpfs"]
    assert str(parent.resolve()) in tmpfs or str(parent) in tmpfs
    assert str(child) not in tmpfs and str(child.resolve()) not in tmpfs


def test_bwrap_distinct_roots_differing_only_in_case_both_keep_their_tmpfs(tmp_path, monkeypatch) -> None:
    """complement + codex, L3 r2: the de-dup used a case-folding matcher, so on ext4 /x/Secret
    swallowed the distinct /x/secret/inner and left it readable. Containment must be exact."""
    monkeypatch.setenv("HOME", str(tmp_path))
    a, b = tmp_path / "Secret", tmp_path / "secret" / "inner"
    argv = _bwrap_argv(build_policy(_entity(tmp_path), extra_deny_read_write=(a, b)))
    tmpfs = [argv[k + 1] for k, x in enumerate(argv) if x == "--tmpfs"]
    assert str(a) in tmpfs and str(b) in tmpfs


def test_bwrap_refuses_a_dangling_symlinked_protected_file(tmp_path, monkeypatch) -> None:
    """complement, L3 r2: masking a dangling link's target makes bwrap create a stub there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").symlink_to(tmp_path / "nowhere" / "x")
    with pytest.raises(ConfinementError, match="dangling"):
        _bwrap_argv(build_policy(_entity(tmp_path), ssh_mode="raw"))


def test_bwrap_render_carries_the_network_unshare_like_spawn(tmp_path, monkeypatch) -> None:
    """The rendered command is the floor that runs (codex, L3 r1): it carries --unshare-net too."""
    monkeypatch.setenv("HOME", str(tmp_path))
    pol = build_policy(_entity(tmp_path), deny_localhost_outbound=True)
    assert "--unshare-net" in BwrapProvider().render_profile(pol)


def test_bwrap_leaves_no_stub_for_an_absent_store_file(tmp_path, monkeypatch) -> None:
    """THE STUB DEFECT (measured 2026-09-30): a /dev/null mountpoint for an absent file is left on the
    host as a 0-byte 0444 file, and anneal cannot open a 0444 memory.db. No absent store file, and
    no absent confinement.json, may be named as a mount target at all."""
    policy, lv = _store_policy(tmp_path, monkeypatch)
    argv = _bwrap_argv(policy)
    for name in ("memory.continuity.md", "memory.db-wal", "memory.db-shm", "memory.db-journal",
                 "confinement.json"):
        assert not (lv / name).exists()
        assert str(lv / name) not in argv, name


def test_bwrap_write_only_files_are_ro_self_binds_when_they_exist(tmp_path, monkeypatch) -> None:
    """Read stays allowed, write returns EROFS — matching macOS, where raw-mode ``~/.ssh`` reads
    still work and the entity may ``cat`` its own memory. Measured additionally: ``rm`` of such a
    file is EBUSY, and so is ``rm -rf`` of its PARENT directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    mem = ent / ".levain" / "memory.continuity.md"
    mem.write_text("REAL MEMORY")
    policy = build_policy(ent, ssh_mode="raw")
    argv = _bwrap_argv(policy)
    existing = [p for p in policy.own_memory_files if p.exists()]
    assert existing, "fixture must produce at least one existing own-memory file"
    self_ro = [d for s, d in _triples(argv, "--ro-bind") if s == d]
    for p in existing:
        assert str(p) in self_ro


def test_bwrap_blocks_planting_a_vector_that_does_not_exist_yet(tmp_path, monkeypatch) -> None:
    """⚠ THE ssh PERSISTENCE VECTOR IS ABOUT A FILE THAT DOES NOT EXIST YET — planting an
    authorized_keys that was never there (spore-322). macOS denies a path STRING, so it covers future
    creation for free; a MOUNT needs a mountpoint. A missing vector therefore gets
    ``--ro-bind /dev/null``, which blocks the plant (measured against a control run that plants
    successfully without it) at the cost of bwrap creating a 0-byte ``-r--r--r--`` file on the host.
    Skipping it instead is ``--ro-bind-try`` by another name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    policy = build_policy(_entity(tmp_path), ssh_mode="raw")
    sockets = set(policy.socket_spellings) | set(policy.deny_sockets)  # step (6) owns those
    missing = [p for p in policy.deny_write_files if not p.exists() and p not in sockets]
    assert missing, "fixture must produce at least one not-yet-existing ssh vector"
    argv = _bwrap_argv(policy)
    devnull_targets = [d for s, d in _triples(argv, "--ro-bind") if s == "/dev/null"]
    for p in missing:
        assert str(p) in devnull_targets


def test_bwrap_agent_mode_hides_keys_and_binds_the_two_ssh_files_back(tmp_path, monkeypatch) -> None:
    """The mount-namespace analogue of SBPL's last-match-wins re-allow: tmpfs the directory, then
    bind the two files ssh actually needs back INTO it. Measured — key material reads ENOENT, the
    known_hosts bind is READ-WRITE and a write inside the sandbox appears in the REAL host file (so
    ssh can still record new host keys), and config is read-only, matching macOS where its WRITE
    stays denied in BOTH modes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVKEY")
    (ssh / "known_hosts").write_text("HOSTS")
    (ssh / "config").write_text("CFG")
    policy = build_policy(_entity(tmp_path), ssh_mode="agent")
    argv = _bwrap_argv(policy)
    assert ("--tmpfs", str(policy.ssh_dir)) in _pairs(argv, "--tmpfs")
    assert (str(ssh / "known_hosts"), str(ssh / "known_hosts")) in _triples(argv, "--bind")
    assert (str(ssh / "config"), str(ssh / "config")) in _triples(argv, "--ro-bind")
    # the key itself is never bound back — it is simply gone inside the tmpfs
    assert str(ssh / "id_ed25519") not in [d for _, d in _triples(argv, "--bind")]


def test_bwrap_never_binds_a_vector_back_into_a_tmpfs_that_hid_it(tmp_path, monkeypatch) -> None:
    """⛔ THE RE-EXPOSE REGRESSION, AND IT IS WHY ``_shadowed_by`` IS NOT AN OPTIMIZATION. In agent
    mode every ssh persistence vector lives inside the tmpfs'd ``~/.ssh``. A self-bind of
    ``authorized_keys`` there would restore READ access to material the agent mode exists to deny —
    the sandbox would render cleanly, start cleanly, and quietly undo its own floor.

    ⚠ SCOPED TO THE SELF-BIND FORM ON PURPOSE. This originally asserted the path never appeared as
    ANY bind target, which was right while a shadowed vector was skipped outright. It is now
    ``--ro-bind /dev/null``-ed (to make the write refusal honest — see the test below), and a blanket
    "never bound" assertion would have failed for the RIGHT change. What must never happen is the
    path being bound to ITSELF, which is what re-exposes the content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("KEYS")
    policy = build_policy(_entity(tmp_path), ssh_mode="agent")
    argv = _bwrap_argv(policy)
    ak = str(ssh / "authorized_keys")
    self_bound = [d for s, d in _triples(argv, "--bind") + _triples(argv, "--ro-bind") if s == d]
    assert ak not in self_bound
    # nothing else may be bound FROM the real path either (that would copy content in)
    assert ak not in [s for s, _ in _triples(argv, "--bind") + _triples(argv, "--ro-bind")]


def test_bwrap_denies_writes_to_a_vector_hidden_inside_a_tmpfs(tmp_path, monkeypatch) -> None:
    """⚡ FOUND BY THE LIVE FLOOR TEST, INVISIBLE TO EVERY PURE TEST ABOVE. Under agent mode the ssh
    vectors sit inside the tmpfs'd ``~/.ssh``, so a plant used to return rc=0 while the host file
    stayed absent — the ATTACK failed but the REPORT lied. The ssh tmpfs cannot be ``--remount-ro``
    (known_hosts must stay writable), so honesty is restored per-file with ``--ro-bind /dev/null`` on
    the enumerable vector list. Verified live afterwards: the plant returns Permission denied and the
    host file is still absent, at zero host mutation (the mountpoint lands inside the tmpfs).

    ⛔ The form matters: a SELF-bind here would re-expose the key material the tmpfs hid, which is
    why this is not simply "bind it like any other vector"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("KEYS")
    policy = build_policy(_entity(tmp_path), ssh_mode="agent")
    argv = _bwrap_argv(policy)
    ak = ssh / "authorized_keys"
    devnull_targets = [d for s, d in _triples(argv, "--ro-bind") if s == "/dev/null"]
    assert str(ak) in devnull_targets
    # and NOT self-bound, which would undo the tmpfs
    assert (str(ak), str(ak)) not in _triples(argv, "--ro-bind")


def test_bwrap_does_not_overmount_the_ssh_files_it_deliberately_rebound(tmp_path, monkeypatch) -> None:
    """``config`` is in ``deny_write_files`` AND is deliberately re-allowed for READ. Its ``--ro-bind``
    already refuses writes honestly (EROFS), so a later ``/dev/null`` over-mount would undo the read
    re-allow and break ssh — the two rules meeting at one path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("CFG")
    (ssh / "known_hosts").write_text("HOSTS")
    policy = build_policy(_entity(tmp_path), ssh_mode="agent")
    argv = _bwrap_argv(policy)
    devnull_targets = [d for s, d in _triples(argv, "--ro-bind") if s == "/dev/null"]
    assert str(ssh / "config") not in devnull_targets
    assert str(ssh / "known_hosts") not in devnull_targets
    assert (str(ssh / "config"), str(ssh / "config")) in _triples(argv, "--ro-bind")


def test_bwrap_respects_an_operator_deny_of_known_hosts(tmp_path, monkeypatch) -> None:
    """Same rule, same reason as the seatbelt provider: the ssh convenience re-allow must NEVER
    silently override a path the CALLER explicitly declared a crown jewel."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    kh = ssh / "known_hosts"
    kh.write_text("HOSTS")
    policy = build_policy(_entity(tmp_path), ssh_mode="agent", deny_files=(kh,))
    argv = _bwrap_argv(policy)
    assert (str(kh), str(kh)) not in _triples(argv, "--bind")


def test_bwrap_render_profile_is_the_same_argv_it_spawns(tmp_path, monkeypatch) -> None:
    """ONE computation, two views. bwrap has no profile FILE — the policy IS the argv — so rendering
    and spawning must never be two builders that can drift (the class fired on this repo the day
    before this was written). shlex-quoted so the rendered form is pasteable into a terminal to
    reproduce a floor by hand, which is how an equivalence claim gets re-checked by someone who does
    not trust the docstring."""
    policy = _lin_policy(tmp_path, monkeypatch)
    rendered = BwrapProvider().render_profile(policy)
    # A directory the floor must create before pinning is rendered as a leading `mkdir ... &&`.
    assert shlex.split(rendered.split(" && ")[-1]) == _bwrap_argv(policy)


def test_bwrap_spawn_shell_fails_closed_and_names_the_apparmor_cause(tmp_path, monkeypatch) -> None:
    """Fail-closed with an error that TEACHES. The Ubuntu 23.10+ cause is not guessable from
    "permission denied", and the message must say that bwrap being installed with
    ``unprivileged_userns_clone=1`` can BOTH be true on a host where it still fails — because that is
    exactly the pair that read GREEN on argushub while every invocation failed."""
    monkeypatch.setattr("levain.firing.confinement.bwrap_available", lambda: False)
    with pytest.raises(ConfinementError) as exc:
        BwrapProvider().spawn_shell(_lin_policy(tmp_path, monkeypatch))
    msg = str(exc.value)
    assert "fail-closed" in msg.lower()
    assert "apparmor_restrict_unprivileged_userns" in msg


@_darwin_only
def test_bwrap_available_is_false_on_a_mac_and_never_raises() -> None:
    """The probe EXECUTES bwrap rather than stat-ing it — the argushub finding. On a Mac there is no
    ``/usr/bin/bwrap``, so it short-circuits False. It must never raise: an exception escaping an
    availability check would read as a crash where the honest answer is "no floor here"."""
    assert bwrap_available() is False


@_linux_only
def test_bwrap_available_is_a_bool_on_linux() -> None:
    """⚠ DELIBERATELY NOT ``is True``. A Linux host with bwrap installed can still be unable to
    create a namespace (Ubuntu 23.10+ AppArmor), and asserting True here would make the suite fail on
    a correctly-fail-closed box — turning an honest "no floor available" into a red build."""
    assert isinstance(bwrap_available(), bool)


# =============================================================================================
# LINUX LIVE — the ENFORCEMENT half of the K4c equivalence proof.
#
# Everything above this line is PURE: it proves the rendered argv says what we measured to be
# correct. It CANNOT prove the kernel does it. These tests spawn a REAL confined bash through the
# REAL BwrapProvider and attack the floor, so they only run where that is possible — a Linux host
# with a working bwrap. They SKIP on macOS and on a Linux box whose kernel refuses unprivileged
# user namespaces (which is the common case on Ubuntu 23.10+; see `bwrap_available`).
#
# ⚠ EVERY CHECK IS A CONTROL/ATTACK PAIR. The controls come first and must PASS — if a neutral file
# is unreadable then the "jewel unreadable" result proves nothing but a broken sandbox. This is the
# lesson from the first pass of these experiments, where an ancestor-rename "denial" turned out to be
# ordinary directory permissions rather than anything the floor did.
# =============================================================================================

linux_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


def test_linux_live_cannot_skip_where_it_is_required() -> None:
    """The `linux_live` tests SKIP wherever bwrap cannot run, which is right on a laptop and wrong in
    the image built to run them: there a skip is a green suite that never touched the floor.
    `tests/linux/Dockerfile` sets ``LEVAIN_REQUIRE_LINUX_LIVE=1``, so in that image a refused
    namespace FAILS here instead of skipping eight tests quietly."""
    if os.environ.get("LEVAIN_REQUIRE_LINUX_LIVE") != "1":
        pytest.skip("only enforced where LEVAIN_REQUIRE_LINUX_LIVE=1 (the Linux test image)")
    assert platform.system() == "Linux" and bwrap_available(), (
        "LEVAIN_REQUIRE_LINUX_LIVE=1 but bwrap cannot establish a namespace here, so every "
        "linux_live test would skip. In Docker, run with --security-opt seccomp=unconfined "
        "--security-opt apparmor=unconfined --security-opt systempaths=unconfined "
        "(see tests/linux/Dockerfile)."
    )


@pytest.fixture()
def linux_floor(tmp_path: Path, monkeypatch):
    """A real confined shell over a realistic crown-jewels layout."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    (ent / ".levain" / "memory.continuity.md").write_text("REAL ENTITY MEMORY\n")
    jewel = tmp_path / ".anneal-memory"
    jewel.mkdir()
    (jewel / "secret.txt").write_text("JEWEL-CONTENT\n")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE-KEY\n")
    (ssh / "known_hosts").write_text("HOSTS\n")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "ok.txt").write_text("NORMAL\n")
    cred = tmp_path / "secret.env"
    cred.write_text("TOKEN=abc\n")
    policy = build_policy(ent, deny_files=(cred,), ssh_mode="agent")
    shell = BwrapProvider().spawn_shell(policy)
    try:
        yield shell, tmp_path, ent, jewel, ssh, cred
    finally:
        shell.close()


@linux_live
def test_linux_live_controls_a_working_filesystem(linux_floor) -> None:
    """THE CONTROLS. Without these passing, every denial below is unfalsifiable."""
    sh, home, _ent, _j, _s, _c = linux_floor
    assert sh.run(f"cat {home}/work/ok.txt").exit_code == 0
    assert sh.run(f"echo x > {home}/work/new.txt").exit_code == 0
    assert sh.run("python3 -c 'print(1)'").exit_code == 0


@linux_live
def test_linux_live_crown_jewel_is_unreadable_and_unwritable(linux_floor) -> None:
    sh, _home, _ent, jewel, _s, _c = linux_floor
    assert sh.run(f"cat {jewel}/secret.txt").exit_code != 0
    assert sh.run(f'test -n "$(ls -A {jewel})"').exit_code != 0
    # ⚠ EROFS, not a silent success: the `--remount-ro` half of the pairing is what makes this fail
    # loudly instead of writing into an ephemeral tmpfs and reporting rc=0.
    assert sh.run(f"echo POISON > {jewel}/x").exit_code != 0
    assert (jewel / "secret.txt").read_text().strip() == "JEWEL-CONTENT"


@linux_live
def test_linux_live_jewel_cannot_be_relocated(linux_floor) -> None:
    """apparatus L2 CRITICAL, re-proved in the mount model. EBUSY, because it is a mountpoint."""
    sh, home, _ent, jewel, _s, _c = linux_floor
    assert sh.run(f"mv {jewel} {home}/stolen").exit_code != 0
    assert (jewel / "secret.txt").exists()


@linux_live
def test_linux_live_write_denied_file_cannot_be_hardlinked_or_renamed_out(linux_floor) -> None:
    """⛔ THE STANDING REQUIREMENT FROM THE MODULE DOCSTRING, which named this as the thing a Linux
    backend MUST re-prove and doubted a ``--ro-bind`` would give. Read stays allowed by design; the
    file cannot be written, renamed out, hardlinked out, or removed."""
    sh, home, ent, _j, _s, _c = linux_floor
    mem = ent / ".levain" / "memory.continuity.md"
    assert sh.run(f"cat {mem}").exit_code == 0
    assert sh.run(f"echo POISON > {mem}").exit_code != 0
    assert sh.run(f"mv {mem} {home}/out.md").exit_code != 0
    assert sh.run(f"ln {mem} {home}/hard.md").exit_code != 0
    assert sh.run(f"rm -f {mem}").exit_code != 0
    assert mem.read_text().strip() == "REAL ENTITY MEMORY"


@linux_live
def test_linux_live_credential_file_denies_both_directions(linux_floor) -> None:
    sh, _home, _ent, _j, _s, cred = linux_floor
    assert sh.run(f"cat {cred}").exit_code != 0
    assert sh.run(f"echo x > {cred}").exit_code != 0
    assert cred.read_text().strip() == "TOKEN=abc"


@linux_live
def test_linux_live_ssh_agent_mode(linux_floor) -> None:
    """Key material gone; known_hosts read AND write, with the write reaching the REAL host file —
    that last assertion is what distinguishes a live bind from a tmpfs copy that silently discards."""
    sh, _home, _ent, _j, ssh, _c = linux_floor
    assert sh.run(f"cat {ssh}/id_ed25519").exit_code != 0
    assert sh.run(f"cat {ssh}/known_hosts").exit_code == 0
    assert sh.run(f"echo NEWHOST >> {ssh}/known_hosts").exit_code == 0
    assert "NEWHOST" in (ssh / "known_hosts").read_text()


@linux_live
def test_linux_live_planting_authorized_keys_is_refused_not_silently_discarded(linux_floor) -> None:
    """⚡ THE REGRESSION THIS FILE EXISTS FOR. The first live run had this returning rc=0 while the
    host file stayed absent — attack defeated, report false — and NO pure test could see it. Both
    halves are asserted: the write must be REFUSED, and the host must stay clean."""
    sh, _home, _ent, _j, ssh, _c = linux_floor
    assert sh.run(f"echo PLANTED > {ssh}/authorized_keys").exit_code != 0
    assert not (ssh / "authorized_keys").exists()


def _unix_daemon(path: Path):
    """A real listening unix socket that answers every connect — the stand-in for dockerd."""
    import threading

    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(8)

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.sendall(b"DAEMON-ANSWERED\n")
            conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    return srv


_CONNECT = (
    "python3 -c \"import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
    "print(s.recv(64).decode().strip())\" "
)


@linux_live
def test_linux_live_the_default_floor_spawns_on_a_host_without_container_daemons(
    tmp_path, monkeypatch
) -> None:
    """THE BRICK (measured 2026-09-30): the real socket roster, on a host where /run/containerd and
    friends do not exist, aborted bwrap before bash started. A floor that cannot spawn is not a
    floor; it is "no bash on Linux"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sh = BwrapProvider().spawn_shell(build_policy(_entity(tmp_path)))
    try:
        r = sh.run("echo SPAWNED")
        assert r.exit_code == 0 and "SPAWNED" in r.output
    finally:
        sh.close()


@linux_live
def test_linux_live_a_daemon_socket_is_unreachable_and_stays_so_across_a_restart(
    tmp_path, monkeypatch
) -> None:
    """CONTROL (host reaches the daemon) / ATTACK (the entity connects, before AND after the daemon
    recreates its socket). The restart is the case that defeated a /dev/null over-mount when
    measured: the host-side unlink detached it."""
    from levain.firing import confinement as _conf

    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".colima" / "default"
    d.mkdir(parents=True)
    sock = d / "docker.sock"
    srv = _unix_daemon(sock)
    monkeypatch.setattr(_conf, "_CONTAINER_DAEMON_SOCKETS", (str(sock),))
    control = subprocess.run(["bash", "-c", _CONNECT + str(sock)], capture_output=True, text=True)
    assert "DAEMON-ANSWERED" in control.stdout, "control: the host must reach the daemon"
    sh = BwrapProvider().spawn_shell(build_policy(_entity(tmp_path)))
    try:
        assert "DAEMON-ANSWERED" not in sh.run(_CONNECT + str(sock)).output
        srv.close()
        sock.unlink()
        srv = _unix_daemon(sock)   # the daemon restarts, on the HOST
        assert "DAEMON-ANSWERED" in subprocess.run(
            ["bash", "-c", _CONNECT + str(sock)], capture_output=True, text=True
        ).stdout, "control: the restarted daemon must be live"
        assert "DAEMON-ANSWERED" not in sh.run(_CONNECT + str(sock)).output
    finally:
        sh.close()
        srv.close()


@linux_live
def test_linux_live_the_store_refuses_a_plant_and_leaves_the_host_store_usable(tmp_path, monkeypatch) -> None:
    """CONTROL: an ordinary existing store entry stays writable. ATTACK: planting a first continuity
    or a SQLite sidecar is refused. AND THE HOST: after the shell, no 0-byte stub is left behind, so
    the next session's config load and the anneal store still work (both failed with the stub)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    lv = ent / ".levain"
    lv.mkdir(parents=True, exist_ok=True)
    (lv / "docs").mkdir()
    sh = BwrapProvider().spawn_shell(build_policy(ent))
    try:
        ok = sh.run(f"echo note > {lv}/docs/n.txt && echo WROTE")
        assert "WROTE" in ok.output, "control: an existing store SUBDIRECTORY must stay writable"
        for name in ("memory.continuity.md", "memory.db-wal", "confinement.json"):
            r = sh.run(f"echo planted > {lv}/{name} && echo CREATED || echo REFUSED")
            assert "REFUSED" in r.output, name
    finally:
        sh.close()
    for name in ("memory.continuity.md", "memory.db-wal", "memory.db-shm", "memory.db-journal",
                 "confinement.json"):
        assert not (lv / name).exists(), f"{name} was left on the host"
    assert load_confinement_config(ent) is not None


@linux_live
def test_linux_live_an_ssh_dir_the_sandbox_creates_cannot_be_swapped_for_a_planted_one(
    tmp_path, monkeypatch
) -> None:
    """L1 H1 / L2 (2026-09-30), the attack as written: raw mode, no ~/.ssh at spawn. Rename the dir
    the sandbox created, make a fresh one, plant authorized_keys. The rename must be refused and
    nothing planted may reach the host."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    assert not ssh.exists()
    sh = BwrapProvider().spawn_shell(build_policy(_entity(tmp_path), ssh_mode="raw"))
    try:
        r = sh.run(f"mv {ssh} {tmp_path}/moved && echo MOVED || echo REFUSED")
        assert "REFUSED" in r.output
        sh.run(f"echo PLANTED-KEY > {ssh}/authorized_keys")
    finally:
        sh.close()
    assert not (tmp_path / "moved").exists()
    ak = ssh / "authorized_keys"
    assert not ak.exists() or "PLANTED-KEY" not in ak.read_text()


@linux_live
def test_linux_live_a_mount_inside_a_jewel_tmpfs_does_not_cost_bash(tmp_path, monkeypatch) -> None:
    """L2 (2026-09-30): a deny file under a jewel subtree needs its mountpoint created INSIDE that
    tmpfs, which aborted bwrap while the tmpfs was already read-only. Remounts now come last."""
    monkeypatch.setenv("HOME", str(tmp_path))
    gh = tmp_path / ".config" / "gh"
    sh = BwrapProvider().spawn_shell(
        build_policy(_entity(tmp_path), deny_standard_creds=True, deny_files=(gh / "hosts.yml",))
    )
    try:
        assert "UP" in sh.run("echo UP").output
        assert "REFUSED" in sh.run(f"echo x > {gh}/new && echo WROTE || echo REFUSED").output
    finally:
        sh.close()


@linux_live
def test_linux_live_a_readonly_self_bind_does_not_stop_a_connect(tmp_path) -> None:
    """Why step (6) exists, kept as a live fact: the socket arm K4c shipped (``--ro-bind S S``)
    lets the connect through. If this ever starts FAILING, the kernel changed and the arm choice
    should be re-measured, not assumed."""
    sock = tmp_path / "d.sock"
    srv = _unix_daemon(sock)
    try:
        r = subprocess.run(
            [BWRAP, "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
             "--ro-bind", str(sock), str(sock), "bash", "-c", _CONNECT + str(sock)],
            capture_output=True, text=True, timeout=20,
        )
        assert "DAEMON-ANSWERED" in r.stdout
    finally:
        srv.close()


@linux_live
def test_linux_live_floor_is_inherited_by_descendants(linux_floor) -> None:
    """A mount namespace is inherited structurally, so this is per-PROCESS-TREE, not per-command —
    the property that makes an OS sandbox work for a long-lived shell whose cwd wanders."""
    sh, _home, _ent, jewel, _s, _c = linux_floor
    assert sh.run(f"bash -c 'bash -c \"cat {jewel}/secret.txt\"'").exit_code != 0


# =============================================================================================
# diagnose_confinement — ONE explanation, shared by the run banner and `levain doctor`.
# =============================================================================================


def test_diagnose_no_provider_for_the_os_has_no_remedy() -> None:
    """Nothing the operator can do about an unported OS, so offering a fix would be noise. The
    THREE outcomes matter: no provider / provider present but host refuses / working."""
    d = diagnose_confinement("Plan9")
    assert d.supported is False
    assert d.provider is None
    assert d.remedy is None


@_darwin_only
def test_diagnose_macos_reports_the_seatbelt_floor() -> None:
    d = diagnose_confinement()
    assert d.supported is True
    assert "sandbox-exec" in (d.provider or "")


def test_diagnose_linux_missing_bwrap_says_install_it(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "isfile", lambda p: False if p == BWRAP else os.path.isfile(p))
    d = diagnose_confinement("Linux")
    assert d.supported is False
    assert "not installed" in d.reason
    assert "bubblewrap" in (d.remedy or "")


def test_diagnose_linux_apparmor_names_the_cause_and_the_official_profile(monkeypatch) -> None:
    """⚡ THE CASE THIS WHOLE SURFACE EXISTS FOR, and the remedy must be UBUNTU'S OWN profile
    (`bwrap-userns-restrict`, shipped in `apparmor-profiles`), not one we hand-roll. It must also
    carry the constraint the profile imposes. ⚠ That constraint is NOT "children cannot create
    namespaces" — measured on a real host, a child CAN create a plain user namespace; what it cannot
    do is map root into one (no capabilities in the stacked child profile), which is what a nested
    bwrap, rootless docker/podman, flatpak and browser sandboxes all need. The remedy text says the
    accurate thing, so this asserts the accurate thing."""
    monkeypatch.setattr("levain.firing.confinement.os.path.isfile", lambda p: True)
    monkeypatch.setattr("levain.firing.confinement.os.access", lambda p, m: True)
    monkeypatch.setattr("levain.firing.confinement.bwrap_available", lambda: False)
    monkeypatch.setattr("levain.firing.confinement._apparmor_restricts_userns", lambda: True)
    d = diagnose_confinement("Linux")
    assert d.supported is False
    assert "AppArmor" in d.reason
    assert "bwrap-userns-restrict" in (d.remedy or "")
    assert "cannot map root" in (d.remedy or "")
    assert "rootless docker/podman" in (d.remedy or "")


def test_diagnose_linux_namespace_denied_without_apparmor_is_a_different_sentence(monkeypatch) -> None:
    """Not every namespace refusal is AppArmor (a container's seccomp does it too, which is how the
    Linux branches were exercised). A remedy naming the wrong cause is worse than a generic one."""
    monkeypatch.setattr("levain.firing.confinement.os.path.isfile", lambda p: True)
    monkeypatch.setattr("levain.firing.confinement.os.access", lambda p, m: True)
    monkeypatch.setattr("levain.firing.confinement.bwrap_available", lambda: False)
    monkeypatch.setattr("levain.firing.confinement._apparmor_restricts_userns", lambda: False)
    d = diagnose_confinement("Linux")
    assert "AppArmor" not in d.reason
    assert "seccomp" in (d.remedy or "")


def test_apparmor_probe_is_diagnostic_only_and_never_gates(monkeypatch) -> None:
    """⛔ THE LESSON OF K4c, PINNED. `spore-418` specified a sysctl read as the capability check and
    it reported GREEN on a host where bwrap could not run. `bwrap_available` must therefore depend
    on EXECUTING bwrap, never on this probe — so flipping the probe must not move the gate."""
    calls = []
    monkeypatch.setattr("levain.firing.confinement._apparmor_restricts_userns",
                        lambda: calls.append(1) or True)
    before = bwrap_available()
    assert bwrap_available() == before
    assert calls == [], "bwrap_available consulted the AppArmor sysctl — that is the defect"


def test_a_pinned_deny_file_under_ssh_is_bound_not_skipped(tmp_path, monkeypatch) -> None:
    """⛔ THE SKIP DROPPED THE **STRICTER** DENY CLASS WHILE THE WEAKER ONE STAYED PROTECTED.

    Step (4) used to `continue` on any path already shadowed by a tmpfs, reasoning that "binding it
    back would re-expose it". False for the form in use: the bind is `--ro-bind /dev/null <path>`,
    which binds /dev/null and re-exposes nothing — which is exactly why step (5) uses that same form
    under identical shadowing.

    The cost is scoped to the ssh tmpfs, and only because of a property that is correct for its own
    reason: it is the ONE tmpfs not paired with `--remount-ro`, since `known_hosts` must stay
    writable. So a caller-pinned `deny_files` path landing under ~/.ssh in agent mode got no mount at
    all, and a WRITE to it silently SUCCEEDED into the ephemeral tmpfs and evaporated — a dishonest
    refusal, and a divergence from macOS, where every `deny_files` entry gets an unconditional
    `(deny file-read* file-write*`.

    ⚠ REACHABLE BY THE MODULE'S OWN INSTRUCTIONS: the docstring's custom `AuthorizedKeysFile` limit
    tells the operator to pin it via `deny_files`, and a non-default `AuthorizedKeysFile` normally
    lives under ~/.ssh.

    Falsified against a control when written: pre-fix the pinned path is ABSENT from the argv and
    the step-(5) vector is present; post-fix both are present."""
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    (home / ".ssh").mkdir()
    (home / ".ssh" / "authorized_keys").write_text("k")
    custom = home / ".ssh" / "authorized_keys_custom"
    custom.write_text("k2")
    policy = build_policy(_entity(home), ssh_mode="agent", deny_files=(custom,))
    argv = _bwrap_argv(policy)

    dests = [dest for _src, dest in _triples(argv, "--ro-bind")]
    assert str(custom.resolve()) in dests, (
        "a caller-pinned deny_files path under ~/.ssh got NO bwrap mount, so a write to it "
        "silently succeeds into the ssh tmpfs and evaporates"
    )
    # and it is bound from /dev/null — the form that denies without re-exposing
    assert ("/dev/null", str(custom.resolve())) in _triples(argv, "--ro-bind")
    # the step-(5) vector is unaffected either way; it is the CONTROL for this test
    assert str((home / ".ssh" / "authorized_keys").resolve()) in dests


@linux_live
def test_linux_live_a_deny_root_strictly_inside_ssh_does_not_brick_the_shell(tmp_path, monkeypatch) -> None:
    """Diogenes MEDIUM diogenes-20261001-022136, reproduced on a Linux kernel and then on argushub:
    a deny root strictly inside ~/.ssh in agent mode queued a --remount-ro on a path the ssh tmpfs
    then hid, and bwrap exited 1 ("Can't remount readonly on .../.ssh/keys") before bash started.
    The equal case (~/.ssh itself) keeps its remount; this is the strictly nested one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    ssh = tmp_path / ".ssh"
    (ssh / "keys").mkdir(parents=True)
    (ssh / "keys" / "id").write_text("PRIVATE-KEY\n")
    (ssh / "known_hosts").write_text("HOSTS\n")
    policy = build_policy(ent, ssh_mode="agent", extra_deny_read_write=(ssh / "keys",))
    with BwrapProvider().spawn_shell(policy) as sh:
        assert sh.run(f"cat {ssh}/known_hosts").exit_code == 0
        assert sh.run(f"cat {ssh}/keys/id").exit_code != 0
        # ...and a write under it is REFUSED, not swallowed by the bare ssh tmpfs (L3, 3 lineages)
        # mkdir first: without it the write fails on ENOENT and proves nothing (measured: it
        # passed on the code this test was written against)
        assert sh.run(f"mkdir -p {ssh}/keys; echo POISON > {ssh}/keys/new").exit_code != 0
        assert sh.run(f"echo NEWHOST >> {ssh}/known_hosts").exit_code == 0   # the re-allow holds
    assert not (ssh / "keys" / "new").exists()


@linux_live
def test_linux_live_a_deny_subtree_that_is_a_file_does_not_brick_the_shell(tmp_path, monkeypatch) -> None:
    """Found 2026-10-01 on argushub, a real host: its ~/.anneal-memory is a regular FILE (a SQLite
    db), not the usual directory. Step (2) put `--tmpfs` over it, bwrap answered "Can't mkdir
    ~/.anneal-memory: Not a directory", and the bash hand could not start at all. A subtree root
    that exists as a file is denied the way a deny file is."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    (tmp_path / ".anneal-memory").write_text("A STORE THAT IS A FILE\n")
    with BwrapProvider().spawn_shell(build_policy(ent, ssh_mode="agent")) as sh:
        assert sh.run("echo alive").exit_code == 0
        assert sh.run(f"cat {tmp_path}/.anneal-memory").exit_code != 0
        assert sh.run(f"echo POISON > {tmp_path}/.anneal-memory").exit_code != 0
    assert (tmp_path / ".anneal-memory").read_text() == "A STORE THAT IS A FILE\n"


@pytest.mark.skipif(not (_LIVE or (platform.system() == "Linux" and bwrap_available())),
                    reason="needs a live provider: macOS sandbox-exec or a Linux host where bwrap runs")
def test_live_a_file_jewels_sqlite_sidecars_are_denied(tmp_path, monkeypatch) -> None:
    """Reproduced 2026-10-02 by Diogenes on BOTH providers: a WAL-mode SQLite store at
    ~/.anneal-memory (a FILE, as on argushub) had its main file denied while `<db>-wal` still held the
    uncheckpointed rows, readable from the confined shell, and `<db>-shm` was writable. On Linux the
    shell could also CREATE `<db>-wal` while the store was closed, and the host replayed it (L1,
    reproduced end to end), so there bash is refused for a SQLite jewel in a writable directory."""
    import sqlite3

    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    db = tmp_path / ".anneal-memory"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('JEWEL-1002-SECRET')")
        conn.commit()
        wal, shm = Path(f"{db}-wal"), Path(f"{db}-shm")
        assert b"JEWEL-1002-SECRET" in wal.read_bytes()   # the precondition: the row is in the WAL
        policy = build_policy(ent, ssh_mode="agent")
        if not _LIVE:
            with pytest.raises(ConfinementError, match="is a SQLite database"):
                BwrapProvider().spawn_shell(policy)
            return
        with SeatbeltProvider().spawn_shell(policy) as sh:
            assert sh.run("echo alive").exit_code == 0
            assert sh.run(f"cat {db}").exit_code != 0
            assert "JEWEL-1002-SECRET" not in sh.run(f"cat {wal}").output
            assert sh.run(f"grep -a -c JEWEL-1002-SECRET {wal}").exit_code != 0
            assert sh.run(f"printf X >> {shm}").exit_code != 0
            assert sh.run(f"printf X > {db}-journal").exit_code != 0   # the plant
        assert conn.execute("SELECT v FROM t").fetchall() == [("JEWEL-1002-SECRET",)]
    finally:
        conn.close()


def test_bwrap_a_file_subtree_root_is_not_rebound_readable_by_the_write_floor(tmp_path, monkeypatch) -> None:
    """L1 on 252f4b9, reproduced from the plan argv: a subtree root that is a FILE got
    `--ro-bind /dev/null F`, and when F was also a write-deny vector (raw mode,
    ~/.ssh/authorized_keys) step (5) then stacked `--ro-bind F F` on top, so the real file read
    again. Before 252f4b9 the same input aborted bwrap (fail closed)."""
    from levain.firing.confinement import _bwrap_plan

    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    ak = ssh / "authorized_keys"
    ak.write_text("ssh-ed25519 AAAA\n")
    argv, _ = _bwrap_plan(build_policy(_entity(tmp_path), ssh_mode="raw",
                                       extra_deny_read_write=(ak,)))
    binds = [argv[i + 1] for i, a in enumerate(argv[:-2])
             if a == "--ro-bind" and argv[i + 2] == str(ak)]
    assert binds == ["/dev/null"], binds


@pytest.mark.parametrize("mode,setting,expected", [
    ("interactive", None, False), ("headless", None, False), ("unattended", None, True),
    ("unattended", False, False),   # the per-entity opt-out keeps the Keychain for that seat
    ("interactive", True, True),    # and an operator can pin it on while driving
])
def test_seatbelt_keychain_rule_follows_the_cred_floor(tmp_path, mode, setting, expected) -> None:
    """spore-1245, ruled by Phill 2026-10-01 (deny for autonomous ops, not while a human drives).
    Measured before the rule: from inside the confined shell `security find-generic-password -w`
    and `git credential-osxkeychain get` read secrets. Measured after, on the real provider:
    interactive/headless read, unattended refused (rc 44, helper empty), https unaffected."""
    from levain.firing.drive import resolve_cred_floor

    deny = resolve_cred_floor(setting, mode=mode)
    policy = build_policy(_entity(tmp_path), deny_standard_creds=deny)
    assert policy.deny_keychain is expected
    profile = SeatbeltProvider().render_profile(policy)
    for svc in ("com.apple.SecurityServer", "com.apple.securityd.xpc",
                "com.apple.securityd.systemkeychain"):
        assert (f'(deny mach-lookup (global-name "{svc}"))' in profile) is expected


@pytest.mark.parametrize("mode,expected", [("interactive", False), ("unattended", True)])
def test_the_tools_path_policy_carries_the_keychain_deny_too(tmp_path, monkeypatch, mode, expected):
    """complement L3: policy_for_conv_state (the file editor's / bash executor's policy, built at
    tool-creation time from the process channel) is where the two enforcers once disagreed."""
    from types import SimpleNamespace

    from levain.firing.openhands.tools import policy_for_conv_state

    ent = _entity(tmp_path)
    ws = ent / "workspace"
    ws.mkdir()
    monkeypatch.setenv("LEVAIN_ENTITY_DIR", str(ent))
    monkeypatch.setenv("LEVAIN_DRIVE_MODE", mode)
    state = SimpleNamespace(workspace=SimpleNamespace(working_dir=str(ws)))
    assert policy_for_conv_state(state).deny_keychain is expected


def test_cred_floor_label_names_the_keychain_on_macos_only() -> None:
    from levain.firing.confinement import cred_floor_label

    assert cred_floor_label("Darwin").endswith("· the Keychain")
    assert "Keychain" not in cred_floor_label("Linux")
