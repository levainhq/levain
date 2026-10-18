"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# TEST ONLY: the per-test seam that lets a binding's effect run on a test executor (see the module).
from tests.autonomic_test_only_confinement import test_only_confined_executor  # noqa: F401


LIVE_SEATBELT_ENV = "LEVAIN_LIVE_SEATBELT"
LIVE_SEATBELT_REASON = (
    f"applies a LIVE macOS Seatbelt profile on this host, off unless {LIVE_SEATBELT_ENV}=1: a hand-written Seatbelt "
    "profile kernel-panicked a Mac on 2026-10-07, so a broad pytest run must never apply one to the host kernel. "
    "Run these in a disposable VM (CI runners are), with the variable set."
)


def _spawns_seatbelt_driver(args) -> bool:
    """True iff a spawn's argv names the Seatbelt driver (the hands path puts it after sudo/env, so any element)."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        return any(os.path.basename(os.fsdecode(a)) == "sandbox-exec" for a in args)
    except TypeError:
        return False


@pytest.fixture(autouse=True, scope="session")
def _no_live_seatbelt_profile_unless_opted_in():
    """TEST ONLY: the one place that keeps a broad pytest run from applying a live Seatbelt profile to the host.

    Every real launch of the driver goes through ``subprocess.Popen`` (``subprocess.run`` included), so the
    guard sits there rather than on each test: a test that would apply a profile is SKIPPED at the spawn (with
    the reason above) unless ``LEVAIN_LIVE_SEATBELT=1``, and a NEW live test cannot apply one by forgetting a
    marker. Nothing is spawned when it skips. Text-rendering tests never spawn the driver and keep running; a
    test that replaces ``subprocess.Popen`` with a fake never reaches this. The guard in product code
    (``seatbelt_guard``) stays the floor beneath it."""
    real_init = subprocess.Popen.__init__

    def guarded(self, args, *a, **kw):
        if os.environ.get(LIVE_SEATBELT_ENV) != "1" and _spawns_seatbelt_driver(args):
            pytest.skip(LIVE_SEATBELT_REASON)
        return real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = guarded
    try:
        yield
    finally:
        subprocess.Popen.__init__ = real_init


@pytest.fixture(autouse=True)
def _test_uid_may_own_the_test_signers_file():
    """TEST ONLY: the throwaway key the tests enrol (``tests/autonomic_confirm_keys.py``) is in a file this
    process's uid owns, where a deployment's allowed-signers file must be root's
    (``levain.autonomic.confirm._SIGNERS_FILE_OWNERS``; the verifier binary stays root's). A test of the root-only rule itself sets it back."""
    import levain.autonomic.confirm as confirm
    with pytest.MonkeyPatch.context() as own:   # its own, so a test's ``monkeypatch.undo()`` keeps it
        own.setattr(confirm, "_SIGNERS_FILE_OWNERS", frozenset({0, os.getuid()}))
        yield


@pytest.fixture(autouse=True)
def _isolate_activation_env(monkeypatch):
    """`LEVAIN_SCOPE` and `CLAUDE_CONFIG_DIR` are AMBIENT INPUTS to doctor, so a
    test that does not clear them is graded by the reviewer's shell.

    Both were measured breaking this suite in opposite directions (Diogenes
    2026-09-03). `LEVAIN_SCOPE=global` — the value `doctor`'s own hint and the
    0.4.2 notes tell operators to set — turned
    `TestActivationScopeCheck::test_the_dark_configuration_FAILS` RED, because
    `_check_activation_scope` correctly declines to fail an install whose gate
    the environment has opened. `CLAUDE_CONFIG_DIR` was worse for being quiet:
    `_user_level_wiring` prefers it over `Path.home()` while every test in
    `TestUserLevelWiring` monkeypatches `Path.home` only, so the four cases
    asserting `== []` went on PASSING for the wrong reason and the
    two-installs-on-one-machine discrimination stopped being exercised at all.

    A guard that passes vacuously under the configuration the docs prescribe is
    not a guard. Tests that MEAN to exercise either variable set it explicitly
    (see `TestActivationScopeEnvOverride`, `TestUserLevelWiringHonorsConfigDir`)
    — this fixture removes the ambient value, it does not forbid the deliberate
    one.
    """
    monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Same shape for the floor: `ANNEAL_MEMORY_DERIVE_TRUST` adds write-denies to every policy
    # build_policy assembles (spore-1308), so a shell that sets it would grade the floor tests.
    monkeypatch.delenv("ANNEAL_MEMORY_DERIVE_TRUST", raising=False)
    # A test that runs the template session_start.py in place would otherwise spawn a detached
    # auto-memory sweep rooted at levain/templates/ (measured: it wrote .levain/ state there).
    # tests/test_automemory_mirror.py builds its own env and sets this deliberately.
    monkeypatch.setenv("LEVAIN_AUTOMEMORY_MIRROR", "off")
    # And the host's gnupg and grants: gpgconf's default pinentry/scdaemon (`_gpg_components`) and the
    # extra and ssh sockets it names (`_gpg_extra_sockets`, `_gpg_ssh_sockets`), and the operator's
    # grant file every Linux floor and every run/doctor socket line. Tests of either set them explicitly
    # (tests/test_operator_grants.py).
    import levain.firing.confinement as conf
    monkeypatch.setattr(conf, "_gpg_components", lambda: [])
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {})
    monkeypatch.setattr(conf, "_gpg_ssh_sockets", lambda: [])
    monkeypatch.setattr(conf, "grants_file", lambda: Path("/nonexistent/levain-test/grants.json"))
    # The scoped ssh agents' directory (`ssh_agent.run_root`): the run/doctor ssh lines and a session's
    # agent start read the host's otherwise. tests/test_ssh_agent.py points it at its own.
    import levain.firing.ssh_agent as ssh_agent
    monkeypatch.setattr(ssh_agent, "run_root", lambda: Path("/nonexistent/levain-test/run"))


@pytest.fixture(autouse=True)
def _isolate_consent_hook_wiring(monkeypatch, tmp_path_factory):
    """`levain init` creates the consent adapter's settings file in levain's consent queue directory, which
    defaults to the REAL `~/.levain/consent` of whoever runs the suite, and reports the installed agent's version
    by running the real `claude` / `codex`; doctor also asks the real `codex app-server` about hook trust. Every
    test gets its own queue directory (made on first use), no agent version and no codex to ask. A test that means to exercise either sets its own. A call
    that names a binary, or finds one a test put on PATH under the pytest tmp dir, still runs it."""
    import levain.consent.canary as canary
    import levain.install as install

    made: list = []

    def queue_dir():
        if not made:
            made.append(tmp_path_factory.mktemp("consent-home") / "consent")
        return made[0]

    real_version = canary.agent_version

    base = str(tmp_path_factory.getbasetemp())

    def no_ambient_version(agent, *, binary=None, timeout=30.0):
        # a fake agent a test put on PATH under its tmp dir is still asked; the real one never is
        found = binary or shutil.which(canary.AGENT_BINS[agent])
        if found and (binary or os.path.realpath(found).startswith(os.path.realpath(base))):
            return real_version(agent, binary=found, timeout=timeout)
        return None

    import levain.consent.adapters.codex as cx

    real_trust = cx.trust_status

    def no_ambient_codex(*args, codex_bin=None, **kwargs):
        # doctor asks the codex on PATH; a test that means to ask one passes it
        return real_trust(*args, codex_bin=codex_bin or "/nonexistent/levain-test-codex", **kwargs)

    monkeypatch.setattr(install, "_consent_queue_dir", queue_dir)
    monkeypatch.setattr(canary, "agent_version", no_ambient_version)
    monkeypatch.setattr(cx, "trust_status", no_ambient_codex)


@pytest.fixture(autouse=True)
def _reset_entity_process_latch():
    """`$LEVAIN_ENTITY_PROCESS` is a one-way latch by design (spore-438): opening an entity sets it
    and nothing in levain clears it, because nothing should. A test process opens many entities, so
    without this every later test would run as an entity process. Both halves (the in-process flag
    and the env var) are reset here, and only here. Popped by hand both sides:
    `monkeypatch.delenv` records nothing for an ABSENT var, so a latch set mid-test would survive."""
    import levain.firing.binding as _binding

    os.environ.pop("LEVAIN_ENTITY_PROCESS", None)
    _binding._LATCHED = False
    yield
    os.environ.pop("LEVAIN_ENTITY_PROCESS", None)
    _binding._LATCHED = False
