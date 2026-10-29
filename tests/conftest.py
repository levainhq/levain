"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# TEST ONLY: the per-test seam that lets a binding's effect run on a test executor (see the module).
from tests.autonomic_test_only_confinement import test_only_confined_executor  # noqa: F401
from tests.autonomic_test_only_coverage import test_only_every_effect_covered  # noqa: F401


@pytest.fixture(autouse=True)
def _vendor_installed(monkeypatch):
    """Every test that reaches ``start``'s ``vendor`` step runs against a store with no vendor prefix, so the step
    passes here; tests/test_slot_vendorbin.py tests the real check (and re-patches ``vendor_ready`` where it runs
    ``start`` against it). A test that blocks the module's import (``sys.modules`` entry None) never reaches it."""
    try:
        import levain.slot.start as _start
    except ImportError:
        return
    monkeypatch.setattr(_start, "vendor_ready", lambda host, vendor: None)


LIVE_SEATBELT_ENV = "LEVAIN_LIVE_SEATBELT"
LIVE_SEATBELT_REASON = (
    f"applies a LIVE macOS Seatbelt profile on this host, off unless {LIVE_SEATBELT_ENV}=1: a hand-written Seatbelt "
    "profile kernel-panicked a Mac on 2026-10-07, so a broad pytest run must never apply one to the host kernel. "
    "Run these in a disposable macOS VM with the variable set; no CI job sets it."
)


# Read once, when this module loads: a test's own setenv/monkeypatch cannot open the guard for the run.
LIVE_SEATBELT_OPT_IN = os.environ.get(LIVE_SEATBELT_ENV) == "1"
_DRIVER = "sandbox" + "-exec"


def _spawns_seatbelt_driver(args, executable=None) -> bool:
    """True iff a spawn names the Seatbelt driver: any argv element by basename (the hands path puts it after
    sudo/env, and its options vary: -p, -f, -n, -D), or ``executable=``. Deliberately broad: a command that only
    names the driver as an operand is skipped too, and a skip costs less than a profile on the host."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        if executable is not None and os.path.basename(os.fsdecode(executable)) == _DRIVER:
            return True
        return any(os.path.basename(os.fsdecode(a)) == _DRIVER for a in args)
    except TypeError:
        return False


@pytest.fixture(autouse=True, scope="session")
def _no_live_seatbelt_profile_unless_opted_in():
    """TEST ONLY: keeps a broad pytest run from applying a live Seatbelt profile to the host through this process.

    The product launches the driver through ``subprocess.Popen`` (``subprocess.run`` included), so the guard sits
    there rather than on each test: such a test is SKIPPED at the spawn (with the reason above) unless
    ``LEVAIN_LIVE_SEATBELT=1`` was set when the run started, and a NEW live test cannot apply one by forgetting a
    marker. Nothing is spawned when it skips. Known edges, closed as constructions no test here writes: a child
    process's own spawn, ``os.exec*``/``posix_spawn``, a shell string, ``executable`` passed by position, a
    test that sets ``LIVE_SEATBELT_OPT_IN`` itself, and a spawn on a worker thread (the skip ends
    that thread, not the test). Beneath all of them the product refuses a kernel-mask rule before every launch
    (``levain.firing.seatbelt_guard``)."""
    real_init = subprocess.Popen.__init__

    def guarded(self, args, *a, **kw):
        args = list(args) if not isinstance(args, (str, bytes, os.PathLike)) else args   # an iterator is read once
        if not LIVE_SEATBELT_OPT_IN and _spawns_seatbelt_driver(args, kw.get("executable")):
            pytest.skip(LIVE_SEATBELT_REASON)
        return real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = guarded
    try:
        yield
    finally:
        if subprocess.Popen.__init__ is guarded:   # never clobber a replacement someone else left in place
            subprocess.Popen.__init__ = real_init


@pytest.fixture(autouse=True)
def _no_operator_git_config_reads(monkeypatch):
    """TEST ONLY: ``build_policy`` asks git which files the operator's global config makes it read, which in a
    test would be the real config of whoever runs the suite. Every test gets "no git"; a test of that derivation
    sets its own runner."""
    from levain.firing import confinement
    monkeypatch.setattr(confinement, "_run_git_config", lambda args, environ: None)


@pytest.fixture(autouse=True)
def _no_input_idle_source(monkeypatch):
    """The consent hook reads this Mac's real input idle (`levain.consent.auto_away.input_idle_seconds`), so a
    suite run while nobody touches the machine would hold requests no test meant to hold. Every in-process test
    gets "no idle source"; a test of input idle passes its own reader."""
    import levain.consent.auto_away as auto_away
    monkeypatch.setattr(auto_away, "input_idle_seconds", lambda: None)


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
    by running the real `claude` / `codex`; doctor, init and update also ask the real `codex app-server` about hook
    trust. Every test gets its own queue directory (made on first use), no agent version and no codex to ask (a
    trust write is taken as confirmed). A test that means to exercise either sets its own. A call
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

    real_write = cx.write_hook_trust

    def codex_confirms(hooks_path, expected_text, *, home=None, codex_bin=None, timeout=cx.TRUST_TIMEOUT):
        # init and update write Codex's hook trust through the codex on PATH; with none named, the file check is
        # real and Codex's confirmation is assumed (a test of the trust write itself names its codex)
        if codex_bin:
            return real_write(hooks_path, expected_text, home=home, codex_bin=codex_bin, timeout=timeout)
        try:
            if hooks_path.read_text(encoding="utf-8") != expected_text:
                return [], f"{hooks_path} is not the file levain rendered, so no hook in it is trusted"
            mine = cx.hook_identities(json.loads(expected_text))
        except (OSError, ValueError) as exc:
            return [], f"{hooks_path} could not be read as levain's hooks ({exc})"
        return [cx.HookTrust(f"{hooks_path}:{cx._event_label(e)}:{g}:{h}", e, d, "trusted")
                for e, g, h, d in mine], None

    real_gate_trust = cx.gate_trust

    def no_ambient_gate_codex(*args, codex_bin=None, **kwargs):
        return real_gate_trust(*args, codex_bin=codex_bin or "/nonexistent/levain-test-codex", **kwargs)

    monkeypatch.setattr(install, "_consent_queue_dir", queue_dir)
    monkeypatch.setattr(canary, "agent_version", no_ambient_version)
    monkeypatch.setattr(cx, "trust_status", no_ambient_codex)
    monkeypatch.setattr(cx, "write_hook_trust", codex_confirms)
    monkeypatch.setattr(cx, "gate_trust", no_ambient_gate_codex)


@pytest.fixture(autouse=True)
def _isolate_levain_home(monkeypatch, tmp_path_factory):
    """TEST ONLY: every place levain resolves "its home" (`default_queue_dir`, the key dir, the autonomic store, the
    coordinator record) reads `$LEVAIN_HOME` first and falls back to `~/.levain`, so a test that reached one without
    setting it wrote the operator's REAL home (the coordinator launch tests did, 10-21). Every test gets its own
    `$LEVAIN_HOME`, replacing whatever the reviewer's shell holds; a test that means another sets it itself. The
    coordinator record is also read and written under the account's home from the password database (never
    `$HOME`), so that lookup is pointed at a per-test directory too."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path_factory.mktemp("levain-home")))
    own = tmp_path_factory.mktemp("account-home")
    monkeypatch.setattr("levain.coordinator.door._own_home", lambda: own)


_SESSION_HOME: list = []


def _install_session_home() -> None:
    """TEST ONLY: ``$HOME`` is one scratch directory for the whole run, set before any test module is collected, so
    a module-level constant bound from ``Path.home()`` at import (``levain.firing.anneal.DEFAULT_CRYSTAL_PATH``, a
    test's own ``REAL_CRYSTAL``) and a module-scoped fixture see it too, and a test's ``monkeypatch.undo()``
    restores THIS home, never the operator's. ``GIT_CONFIG_GLOBAL``, ``XDG_CONFIG_HOME`` and ``CODEX_HOME`` are
    removed from the process environment (removed, not set: the confinement floor reads ``GIT_CONFIG_GLOBAL`` only
    when it is set). The scratch home carries a minimal ``.gitconfig``: tests that commit need an identity. A test
    that wants its own HOME still monkeypatches it."""
    import tempfile
    home = tempfile.mkdtemp(prefix="levain-test-home-")
    Path(home, ".gitconfig").write_text("[user]\n\tname = levain test\n\temail = test@levain.invalid\n", encoding="utf-8")
    _SESSION_HOME.append(home)
    os.environ["HOME"] = home
    for name in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "CODEX_HOME"):
        os.environ.pop(name, None)


def pytest_configure(config):
    if not _SESSION_HOME:
        _install_session_home()


def pytest_unconfigure(config):
    while _SESSION_HOME:
        shutil.rmtree(_SESSION_HOME.pop(), ignore_errors=True)


@pytest.fixture(autouse=True)
def _per_test_home(monkeypatch, tmp_path_factory):
    """TEST ONLY: on top of the session scratch home, every test gets its own ``$HOME`` (with the minimal
    ``.gitconfig``), so state one test writes under HOME (enrolled keys, verifier and journal stores, a gpg home)
    never reaches the next. The monkeypatch undo returns to the SESSION scratch home, never the operator's. A test
    that wants a different HOME still sets it itself. The home sits beside the test's ``tmp_path``, never in it:
    tests make ``tmp_path / "home"`` themselves and some assert ``tmp_path``'s exact contents."""
    home = tmp_path_factory.mktemp("home")
    (home / ".gitconfig").write_text("[user]\n\tname = levain test\n\temail = test@levain.invalid\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _fresh_floor_path_cache():
    """``levain.session._floor_path`` caches the expanded credential stores for the process, built from ``$HOME``;
    a test that sets its own HOME would otherwise read a pattern built from another test's."""
    from levain.session import _floor_path
    _floor_path.cache_clear()
    yield
    _floor_path.cache_clear()


@pytest.fixture(autouse=True)
def _test_team_keys_may_be_file_keys():
    """TEST ONLY: the team tests sign with throwaway passphrase-less ed25519 keys, where a deployment's `levain team
    record` needs a FIDO key's touch unless the team allows file keys (``levain.team.tenure._REQUIRE_PRESENCE``).
    The tests of the presence rule itself set it back."""
    import levain.team.tenure as tenure
    with pytest.MonkeyPatch.context() as own:   # its own, so a test's ``monkeypatch.undo()`` keeps it
        own.setattr(tenure, "_REQUIRE_PRESENCE", False)
        yield


@pytest.fixture(autouse=True)
def _no_real_routes_trust_helper():
    """TEST ONLY: ``levain dispatch config init`` / ``config trust`` hand the route trust to root through
    ``sudo <install helper> routes-trust`` (``levain.dispatch.cli.ROUTES_TRUST_HELPER``), which on a host with the
    helper installed would ask for the operator's password. Every test gets a stub answering as a host with no
    helper; a test of the pin sets its own helper, and the test of the default reads the source."""
    import levain.dispatch.cli as dcli
    with pytest.MonkeyPatch.context() as own:   # its own, so a test's ``monkeypatch.undo()`` keeps it
        own.setattr(dcli, "ROUTES_TRUST_HELPER",
                    lambda data: (127, "", "the install helper is not installed (the test suite's stub)"))
        yield


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
